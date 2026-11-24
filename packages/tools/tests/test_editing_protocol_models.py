"""The protocol models and pass A, held against the contract they mirror.

``swcraft_tools.editing.protocol`` is a hand-written mirror of
``packages/tools/tests/fixtures/contracts/editing-protocol/protocol.schema.json``,
which is not shipped in the wheel. This module holds the mirror to it:

* **the parity gate**: every shipped vector's input, plus a corpus of hand-built
  requests, is accepted by :func:`pass_a` exactly when ``jsonschema`` accepts it
  against ``#/$defs/request``;
* **the checked denormalization**: ``ERROR_CLASS_MAP`` is pinned against
  ``protocol.schema.json``'s ``error_class_map``, and :class:`Violation` has no
  constructor parameter for ``failure_class``;
* **the security invariants**: no request shape accepts a file path, raw YAML or
  raw text; there is no commit request, transaction handle, force flag,
  skip-validation parameter or partial-commit mode.

The response models are exercised against the responses the engine produces.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
from conftest import AGENT_TASK_BENCH_MANIFEST
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from swcraft_tools import query
from swcraft_tools.editing import read
from swcraft_tools.editing.address import Address
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.passes import SCHEMA_VIOLATION, pass_a
from swcraft_tools.editing.protocol import (
    ABSENT,
    CHANGE_OPERATION_NAMES,
    ERROR_CLASS_MAP,
    ERROR_CODES,
    FAILURE_CLASSES,
    OVER_BUDGET,
    REQUEST_OPERATIONS,
    RESERVED_JOIN_ALIASES,
    ChangeSetRequest,
    ChangeSetResponse,
    CreateOperation,
    DeltaEntry,
    DiscoverRequest,
    DiscoverResponse,
    EnvelopeError,
    ErrorResult,
    JoinCondition,
    JoinedRead,
    JoinEntry,
    Measured,
    Page,
    ProtocolError,
    ReadRequest,
    ReadResponse,
    ReadsCapability,
    RemoveOperation,
    UpdateOperation,
    Violation,
    WhereClause,
    failure_class,
    parse_response,
)
from test_editing_protocol_contracts import (
    ALL_VECTOR_FILES,
    PROTOCOL_PATH,
    VECTOR_PATH,
    _load,
)

# The jsonschema half of the parity gate


@pytest.fixture(scope="module")
def protocol_schema() -> dict[str, Any]:
    return _load(PROTOCOL_PATH)


@pytest.fixture(scope="module")
def request_validator(protocol_schema: dict[str, Any]) -> Draft202012Validator:
    """A validator for ``#/$defs/request`` alone — never the response union."""
    schemas = [protocol_schema, _load(VECTOR_PATH)]
    registry = Registry().with_resources([(schema["$id"], Resource.from_contents(schema)) for schema in schemas])
    return Draft202012Validator({"$ref": f"{protocol_schema['$id']}#/$defs/request"}, registry=registry)


def _pass_a_accepts(request: Any) -> bool:
    """Whether pass A accepts *request*; every refusal is an addressless ``invalid`` ``schema-violation``."""
    try:
        pass_a(request)
    except ProtocolError as refusal:
        body = refusal.result.to_json()
        assert body["response"] == "error"
        assert body["error"] == SCHEMA_VIOLATION
        assert body["failure_class"] == "invalid"
        assert "addresses" not in body
        return False
    return True


#: Hand-built requests that probe the boundary the vectors do not reach. Each
#: entry states only that the two validators agree, not which side is right.
_VALID_ADDRESS = {"layer": "swc", "element": "ExampleSWC"}
_VALID_FIELD_ADDRESS = {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "type"}]}
_VALID_TOKEN = "rev1:AAAAAAAAAAAAAAAAAAAAAA"

#: Built from the read module's own constants, so it stays well-formed if the
#: budget or the default table moves.
_VALID_READS = read.reads_capability().to_json()


def _change_set(*operations: Any, **overrides: Any) -> dict[str, Any]:
    return {"operation": "change_set", "baseline": _VALID_TOKEN, "operations": list(operations)} | overrides


PARITY_CORPUS: list[tuple[str, Any]] = [
    # Accepted
    ("discover-bare", {"operation": "discover"}),
    ("discover-narrowed", {"operation": "discover", "layer": "data_types"}),
    ("read-layer", {"operation": "read", "address": {"layer": "resources"}}),
    ("read-element", {"operation": "read", "address": _VALID_ADDRESS}),
    ("read-member-step", {"operation": "read", "address": {**_VALID_ADDRESS, "path": [{"member": "pOut"}]}}),
    ("read-null-member-step", {"operation": "read", "address": {**_VALID_ADDRESS, "path": [{"member": None}]}}),
    ("read-key-step", {"operation": "read", "address": {**_VALID_ADDRESS, "path": [{"key": "RAM"}]}}),
    ("change-set-create", _change_set({"operation": "create", "address": _VALID_ADDRESS, "value": {}})),
    ("change-set-remove", _change_set({"operation": "remove", "address": _VALID_ADDRESS})),
    ("change-set-update-null", _change_set({"operation": "update", "address": _VALID_FIELD_ADDRESS, "value": None})),
    ("change-set-with-intent", _change_set({"operation": "remove", "address": _VALID_ADDRESS}, intent="why")),
    ("create-with-path", _change_set({"operation": "create", "address": _VALID_FIELD_ADDRESS, "value": 1})),
    # Refused: the envelope
    ("request-is-a-string", "model/swc/example/ExampleSWC.yaml"),
    ("request-is-a-list", [{"operation": "discover"}]),
    ("request-is-null", None),
    ("no-operation", {"layer": "swc"}),
    ("unknown-operation-rename", {"operation": "rename", "address": _VALID_ADDRESS, "value": "X"}),
    ("unknown-operation-commit", {"operation": "commit", "baseline": _VALID_TOKEN}),
    ("bare-create-operation", {"operation": "create", "address": _VALID_ADDRESS, "value": {}}),
    ("discover-extra-key", {"operation": "discover", "layer": "swc", "force": True}),
    ("discover-null-layer", {"operation": "discover", "layer": None}),
    ("discover-bad-layer", {"operation": "discover", "layer": "Swc"}),
    ("read-without-address", {"operation": "read"}),
    ("read-string-address", {"operation": "read", "address": "model/swc/example/ExampleSWC.yaml"}),
    ("read-traversal-identity", {"operation": "read", "address": {"layer": "swc", "element": "../../etc/passwd"}}),
    ("read-dot-identity", {"operation": "read", "address": {"layer": "swc", "element": "."}}),
    ("read-empty-identity", {"operation": "read", "address": {"layer": "swc", "element": ""}}),
    ("read-path-without-element", {"operation": "read", "address": {"layer": "swc", "path": [{"field": "type"}]}}),
    ("read-empty-path", {"operation": "read", "address": {**_VALID_ADDRESS, "path": []}}),
    ("read-ordinal-step", {"operation": "read", "address": {**_VALID_ADDRESS, "path": [{"index": 0}]}}),
    ("read-empty-step", {"operation": "read", "address": {**_VALID_ADDRESS, "path": [{}]}}),
    ("read-two-form-step", {"operation": "read", "address": {**_VALID_ADDRESS, "path": [{"field": "a", "key": "b"}]}}),
    ("read-empty-field-step", {"operation": "read", "address": {**_VALID_ADDRESS, "path": [{"field": ""}]}}),
    ("read-address-extra-key", {"operation": "read", "address": {**_VALID_ADDRESS, "file": "x.yaml"}}),
    ("read-extra-key", {"operation": "read", "address": _VALID_ADDRESS, "yaml": "name: X"}),
    # Refused: the change-set envelope
    (
        "change-set-without-baseline",
        {"operation": "change_set", "operations": [{"operation": "remove", "address": _VALID_ADDRESS}]},
    ),
    ("change-set-short-baseline", _change_set({"operation": "remove", "address": _VALID_ADDRESS}, baseline="rev1:abc")),
    (
        "change-set-unframed-baseline",
        _change_set({"operation": "remove", "address": _VALID_ADDRESS}, baseline="deadbeefcafe"),
    ),
    ("change-set-null-baseline", _change_set({"operation": "remove", "address": _VALID_ADDRESS}, baseline=None)),
    ("change-set-empty", _change_set()),
    ("change-set-operations-not-array", {"operation": "change_set", "baseline": _VALID_TOKEN, "operations": {}}),
    ("change-set-empty-intent", _change_set({"operation": "remove", "address": _VALID_ADDRESS}, intent="")),
    ("change-set-null-intent", _change_set({"operation": "remove", "address": _VALID_ADDRESS}, intent=None)),
    ("change-set-force-flag", _change_set({"operation": "remove", "address": _VALID_ADDRESS}, force=True)),
    (
        "change-set-skip-validation",
        _change_set({"operation": "remove", "address": _VALID_ADDRESS}, skip_validation=True),
    ),
    ("change-set-partial-commit", _change_set({"operation": "remove", "address": _VALID_ADDRESS}, partial_commit=True)),
    (
        "change-set-transaction-handle",
        _change_set({"operation": "remove", "address": _VALID_ADDRESS}, transaction="t1"),
    ),
    # Refused: one operation inside a well-formed change set
    ("operation-is-a-string", _change_set("remove ExampleSWC")),
    ("operation-without-name", _change_set({"address": _VALID_ADDRESS})),
    ("operation-rename", _change_set({"operation": "rename", "address": _VALID_ADDRESS, "value": "X"})),
    ("create-without-value", _change_set({"operation": "create", "address": _VALID_ADDRESS})),
    ("create-layer-address", _change_set({"operation": "create", "address": {"layer": "swc"}, "value": {}})),
    (
        "create-traversal-identity",
        _change_set(
            {
                "operation": "create",
                "address": {"layer": "data_types", "element": "../../../.ssh/authorized_keys"},
                "value": {},
            }
        ),
    ),
    ("update-element-address", _change_set({"operation": "update", "address": _VALID_ADDRESS, "value": 1})),
    ("update-without-value", _change_set({"operation": "update", "address": _VALID_FIELD_ADDRESS})),
    ("remove-with-value", _change_set({"operation": "remove", "address": _VALID_ADDRESS, "value": None})),
    ("remove-field-address-is-fine", _change_set({"operation": "remove", "address": _VALID_FIELD_ADDRESS})),
    ("operation-extra-key", _change_set({"operation": "remove", "address": _VALID_ADDRESS, "force": True})),
    # The sender-receiver pilot's keys: a members path, `derived`, `join`
    ("read-members-path", {"operation": "read", "address": {"layer": "swc"}, "members": "runnables.writes"}),
    ("read-members-undeclared-shape", {"operation": "read", "address": {"layer": "swc"}, "members": "foo-bar"}),
    ("read-members-lone-dot", {"operation": "read", "address": {"layer": "swc"}, "members": "."}),
    ("read-members-leading-dot", {"operation": "read", "address": {"layer": "swc"}, "members": ".ports"}),
    ("read-members-trailing-dot", {"operation": "read", "address": {"layer": "swc"}, "members": "ports."}),
    ("read-members-inner-empty-step", {"operation": "read", "address": {"layer": "swc"}, "members": "a..b"}),
    ("read-derived", {"operation": "read", "address": {"layer": "connectors"}, "form": "index", "derived": ["x"]}),
    ("read-derived-without-form", {"operation": "read", "address": {"layer": "connectors"}, "derived": ["x"]}),
    (
        "read-derived-at-summary",
        {"operation": "read", "address": {"layer": "connectors"}, "form": "summary", "derived": ["x"]},
    ),
    ("read-derived-at-element", {"operation": "read", "address": _VALID_ADDRESS, "form": "index", "derived": ["x"]}),
    (
        "read-derived-at-field",
        {"operation": "read", "address": _VALID_FIELD_ADDRESS, "form": "index", "derived": ["x"]},
    ),
    ("read-derived-empty", {"operation": "read", "address": {"layer": "swc"}, "form": "index", "derived": []}),
    (
        "read-derived-repeated",
        {"operation": "read", "address": {"layer": "swc"}, "form": "index", "derived": ["x", "x"]},
    ),
    ("read-derived-empty-name", {"operation": "read", "address": {"layer": "swc"}, "form": "index", "derived": [""]}),
]


def _join_entry(alias: str = "other", **joined: Any) -> dict[str, Any]:
    """One `join` item over the `connectors` layer, its joined read widened by *joined*."""
    return {
        "with": {"address": {"layer": "connectors"}} | joined,
        "on": [{"left": "element", "right": "element"}],
        "as": alias,
    }


def _joined(*entries: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return {"operation": "read", "address": {"layer": "swc"}, "form": "index", "join": list(entries)} | overrides


PARITY_CORPUS += [
    ("read-join", _joined(_join_entry())),
    ("read-join-two-entries", _joined(_join_entry("a"), _join_entry("b"))),
    ("read-join-prefix-sharing-aliases", _joined(_join_entry("reader"), _join_entry("reader2"))),
    ("read-join-without-form", {"operation": "read", "address": {"layer": "swc"}, "join": [_join_entry()]}),
    ("read-join-at-element", _joined(_join_entry(), address=_VALID_ADDRESS)),
    ("read-join-empty", _joined()),
    ("read-join-dotted-alias", _joined(_join_entry("a.b"))),
    ("read-join-leading-digit-alias", _joined(_join_entry("1a"))),
    ("read-join-empty-on", _joined(_join_entry() | {"on": []})),
    ("read-join-empty-left", _joined(_join_entry() | {"on": [{"left": "", "right": "element"}]})),
    ("read-join-condition-extra-key", _joined(_join_entry() | {"on": [{"left": "a", "right": "b", "op": "<"}]})),
    ("read-join-entry-without-as", _joined({key: value for key, value in _join_entry().items() if key != "as"})),
    ("read-joined-read-at-element", _joined(_join_entry() | {"with": {"address": _VALID_ADDRESS}})),
    ("read-joined-read-members-path", _joined(_join_entry(members="runnables.reads"))),
    ("read-joined-read-where-and-derived", _joined(_join_entry(where=[{"field": "a", "equals": 1}], derived=["x"]))),
    # Malformed keys inside a joined read: the mutation sweep stops four steps
    # deep, above `join.N.with.where[i]`, so these reach the joined read's parsers by hand.
    (
        "read-joined-where-equals-and-exists",
        _joined(_join_entry(where=[{"field": "a", "equals": 1, "exists": True}])),
    ),
    ("read-joined-where-non-bool-exists", _joined(_join_entry(where=[{"field": "a", "exists": "yes"}]))),
    ("read-joined-where-empty-field", _joined(_join_entry(where=[{"field": "", "equals": 1}]))),
    ("read-joined-members-empty-step", _joined(_join_entry(members="a..b"))),
    ("read-joined-derived-empty-name", _joined(_join_entry(derived=[""]))),
    ("read-joined-derived-repeated", _joined(_join_entry(derived=["x", "x"]))),
    *[
        (f"read-joined-read-carrying-{key}", _joined(_join_entry(**{key: value})))
        for key, value in (("form", "index"), ("limit", 5), ("after", "x"), ("join", [_join_entry()]))
    ],
    *[(f"read-join-reserved-alias-{alias}", _joined(_join_entry(alias))) for alias in sorted(RESERVED_JOIN_ALIASES)],
]


_VERSION_ADDRESS = {"layer": "requirements", "element": "REQ_A", "path": [{"field": "version"}]}

#: `confirm_links` is an update's key alone, a boolean (a stated `false` is accepted and omitted
#: on the way back, so it is no round-trip entry: `TestTheRefusalsNameTheAcceptedShape` holds it).
PARITY_CORPUS += [
    (
        "update-confirm-links",
        _change_set({"operation": "update", "address": _VERSION_ADDRESS, "value": 2, "confirm_links": True}),
    ),
    (
        "update-confirm-links-not-a-boolean",
        _change_set({"operation": "update", "address": _VERSION_ADDRESS, "value": 2, "confirm_links": "yes"}),
    ),
    (
        "create-confirm-links",
        _change_set({"operation": "create", "address": _VALID_ADDRESS, "value": {}, "confirm_links": True}),
    ),
    ("remove-confirm-links", _change_set({"operation": "remove", "address": _VALID_ADDRESS, "confirm_links": True})),
    ("remove-carrying-a-value", _change_set({"operation": "remove", "address": _VALID_ADDRESS, "value": "x"})),
    ("create-missing-its-value", _change_set({"operation": "create", "address": _VALID_ADDRESS})),
    ("update-missing-its-value", _change_set({"operation": "update", "address": _VALID_FIELD_ADDRESS})),
]


class TestPassAAgreesWithTheSchema:
    """The parity gate: the mirror and ``protocol.schema.json`` accept the same requests."""

    @pytest.mark.parametrize("path", ALL_VECTOR_FILES, ids=lambda p: p.stem)
    def test_every_shipped_vector_input(self, path: Path, request_validator: Draft202012Validator) -> None:
        request = _load(path)["input"]
        assert _pass_a_accepts(request) == request_validator.is_valid(request)

    @pytest.mark.parametrize(("name", "request_body"), PARITY_CORPUS, ids=[name for name, _ in PARITY_CORPUS])
    def test_the_boundary_corpus(self, name: str, request_body: Any, request_validator: Draft202012Validator) -> None:
        assert _pass_a_accepts(request_body) == request_validator.is_valid(request_body), name

    def test_the_corpus_states_both_answers(self, request_validator: Draft202012Validator) -> None:
        # A corpus that only ever refuses would pass the parity check with an
        # implementation that refuses everything.
        verdicts = {request_validator.is_valid(body) for _name, body in PARITY_CORPUS}
        assert verdicts == {True, False}

    def test_the_seven_pass_a_vectors_are_the_ones_the_schema_refuses(
        self, request_validator: Draft202012Validator
    ) -> None:
        # A read carrying `join` without `form: "index"` is refused by `$defs/rowForm`;
        # nine join entries exceed `$defs/join`'s `maxItems`.
        refused = sorted(
            _load(path)["name"] for path in ALL_VECTOR_FILES if not request_validator.is_valid(_load(path)["input"])
        )
        assert refused == [
            "envelope-change-set-without-baseline",
            "envelope-storage-path-as-address",
            "envelope-unknown-rename-operation",
            "schema-violation-join-over-max-entries",
            "schema-violation-join-without-index-form",
            "traversal-identity-create-address",
            "traversal-identity-read-address",
        ]


class TestPassAReturnsTheParsedRequest:
    """Acceptance is not a boolean: pass A hands the dispatcher a typed request."""

    def test_discover_narrowed(self) -> None:
        assert pass_a({"operation": "discover", "layer": "tasks"}) == DiscoverRequest(layer="tasks")

    def test_read(self) -> None:
        parsed = pass_a({"operation": "read", "address": _VALID_ADDRESS})
        assert parsed == ReadRequest(address=Address(layer="swc", element="ExampleSWC"))

    def test_change_set_operations_keep_their_order_and_kind(self) -> None:
        parsed = pass_a(
            _change_set(
                {"operation": "create", "address": _VALID_ADDRESS, "value": {"name": "ExampleSWC"}},
                {"operation": "update", "address": _VALID_FIELD_ADDRESS, "value": "application"},
                {"operation": "remove", "address": _VALID_ADDRESS},
                intent="a round trip",
            )
        )
        assert isinstance(parsed, ChangeSetRequest)
        assert parsed.baseline == _VALID_TOKEN
        assert parsed.intent == "a round trip"
        assert [type(operation) for operation in parsed.operations] == [
            CreateOperation,
            UpdateOperation,
            RemoveOperation,
        ]

    def test_a_request_round_trips_through_its_model(self) -> None:
        # Through ``pass_a``, the production dispatcher: a test-only parse helper
        # would be a second dispatcher to keep correct.
        for _name, body in PARITY_CORPUS:
            if not _pass_a_accepts(body):
                continue
            assert pass_a(body).to_json() == body


class TestPassAReportsEveryMalformedOperation:
    """One round trip, every violation — but only where enumerating is sound."""

    def test_two_malformed_operations_are_reported_in_operation_order(self) -> None:
        with pytest.raises(ProtocolError) as caught:
            pass_a(
                _change_set(
                    {"operation": "update", "address": _VALID_ADDRESS, "value": 1},
                    {"operation": "remove", "address": _VALID_ADDRESS},
                    {"operation": "rename", "address": _VALID_ADDRESS, "value": "X"},
                )
            )
        body = caught.value.result.to_json()
        assert "operation 0" in body["message"]
        assert [entry["error"] for entry in body["additional"]] == [SCHEMA_VIOLATION]
        assert "operation 2" in body["additional"][0]["message"]
        assert body["additional"][0]["failure_class"] == "invalid"

    def test_one_malformed_operation_carries_no_additional_block(self) -> None:
        with pytest.raises(ProtocolError) as caught:
            pass_a(_change_set({"operation": "rename", "address": _VALID_ADDRESS, "value": "X"}))
        assert "additional" not in caught.value.result.to_json()

    def test_two_operations_malformed_the_same_way_are_still_two_violations(self) -> None:
        """Two operations malformed identically inside their address are two violations.

        Guards a defect: refusals raised in ``_address`` named no position, so two
        identical violations made ``ErrorResult`` raise a bare ``ValueError`` out of pass A.
        """
        traversal = {"layer": "data_types", "element": "../../../etc/passwd"}
        with pytest.raises(ProtocolError) as caught:
            pass_a(
                _change_set(
                    {"operation": "create", "address": traversal, "value": {}},
                    {"operation": "create", "address": traversal, "value": {}},
                )
            )
        body = caught.value.result.to_json()
        assert body["error"] == SCHEMA_VIOLATION
        assert body["message"].startswith("operation 0")
        assert [entry["error"] for entry in body["additional"]] == [SCHEMA_VIOLATION]
        assert body["additional"][0]["message"].startswith("operation 1")

    def test_a_malformed_envelope_is_reported_fail_fast(self) -> None:
        # `operations` is not an array, so walking it would report noise.
        with pytest.raises(ProtocolError) as caught:
            pass_a({"operation": "change_set", "baseline": _VALID_TOKEN, "operations": "everything"})
        assert "additional" not in caught.value.result.to_json()


class TestTheRefusalsNameTheAcceptedShape:
    """At ``parse_change_operation``'s call sites a missing or extra ``value`` says what to send.

    The closed-keys message every envelope object shares is unchanged, and so is every other refusal.
    """

    @staticmethod
    def _message(operation: dict[str, Any]) -> str:
        with pytest.raises(ProtocolError) as caught:
            pass_a(_change_set(operation))
        body = caught.value.result.to_json()
        assert body["error"] == SCHEMA_VIOLATION
        return body["message"]

    def test_a_remove_carrying_a_value(self) -> None:
        message = self._message({"operation": "remove", "address": _VALID_ADDRESS, "value": "x"})
        assert message == (
            "operation 0 carries no ['value'] (additionalProperties: false); a remove carries no value: to replace "
            "a list member, remove it and create the new one"
        )

    def test_a_create_missing_its_value(self) -> None:
        message = self._message({"operation": "create", "address": _VALID_ADDRESS})
        assert message == "operation 0 requires ['value']; a create carries the new element or member in `value`"

    def test_an_update_missing_its_value(self) -> None:
        message = self._message({"operation": "update", "address": _VALID_FIELD_ADDRESS})
        assert message == (
            "operation 0 requires ['value']; an update carries the field's whole new value in `value`; to confirm "
            "every link to a requirement at a new version, update its `version` with `confirm_links: true`"
        )

    @pytest.mark.parametrize(
        ("operation", "expected"),
        [
            pytest.param(
                {"operation": "remove", "address": _VALID_ADDRESS, "force": True},
                "operation 0 carries no ['force'] (additionalProperties: false)",
                id="remove-another-key",
            ),
            pytest.param(
                {"operation": "create", "address": _VALID_ADDRESS, "force": True},
                "operation 0 carries no ['force'] (additionalProperties: false)",
                id="create-another-key-and-no-value",
            ),
            pytest.param(
                {"operation": "update", "value": 1},
                "operation 0 requires ['address']",
                id="update-missing-its-address",
            ),
        ],
    )
    def test_every_other_refusal_is_the_closed_keys_message_unchanged(
        self, operation: dict[str, Any], expected: str
    ) -> None:
        assert self._message(operation) == expected

    def test_the_read_requests_message_is_unchanged(self) -> None:
        """``test_cli_query.py`` pins the read request's own refusal; the sentences are the change set's alone."""
        for key in ("operation_", "value"):
            with pytest.raises(ProtocolError) as caught:
                pass_a({"operation": "read", "address": _VALID_ADDRESS, key: 1})
            message = caught.value.result.to_json()["message"]
            assert message == f"a read request carries no [{key!r}] (additionalProperties: false)"

    def test_confirm_links_parses_on_an_update_and_is_omitted_when_false(self) -> None:
        parsed = pass_a(
            _change_set({"operation": "update", "address": _VERSION_ADDRESS, "value": 2, "confirm_links": True})
        )
        assert isinstance(parsed, ChangeSetRequest)
        (operation,) = parsed.operations
        assert isinstance(operation, UpdateOperation) and operation.confirm_links is True
        unflagged = pass_a(
            _change_set({"operation": "update", "address": _VERSION_ADDRESS, "value": 2, "confirm_links": False})
        )
        assert "confirm_links" not in unflagged.to_json()["operations"][0]


# The checked denormalization


class TestErrorCodesAndFailureClasses:
    """``failure_class`` is derived from one map, and that map has one source."""

    # Not checked here: the map against the documented error-code block (a test outside this tree).

    def test_the_map_equals_the_schemas_annotation(self, protocol_schema: dict[str, Any]) -> None:
        assert dict(ERROR_CLASS_MAP) == protocol_schema["error_class_map"]

    def test_the_code_enum_and_its_order_are_the_schemas(self, protocol_schema: dict[str, Any]) -> None:
        assert list(ERROR_CODES) == protocol_schema["$defs"]["errorCode"]["enum"]

    def test_the_four_classes_are_the_schemas(self, protocol_schema: dict[str, Any]) -> None:
        assert FAILURE_CLASSES == set(protocol_schema["$defs"]["failureClass"]["enum"])

    @pytest.mark.parametrize("code", ERROR_CODES)
    def test_failure_class_is_the_maps_answer(self, code: str) -> None:
        assert failure_class(code) == ERROR_CLASS_MAP[code]

    def test_an_undeclared_code_has_no_default_class(self) -> None:
        with pytest.raises(ValueError, match="not one of the protocol error codes"):
            failure_class("almost-right")

    def test_a_violation_cannot_be_given_a_failure_class(self) -> None:
        # "It can never disagree": there is no parameter.
        assert "failure_class" not in inspect.signature(Violation).parameters

    @pytest.mark.parametrize("code", ERROR_CODES)
    def test_a_violation_publishes_the_derived_class(self, code: str) -> None:
        # `over-budget` requires `measured` and every other code refuses it, so
        # the smallest legal violation of that code carries the block.
        extra = {"measured": Measured(wire_bytes=1, budget=0, elements=1)} if code == OVER_BUDGET else {}
        assert Violation(error=code, message="x", **extra).to_json()["failure_class"] == ERROR_CLASS_MAP[code]

    def test_an_undeclared_code_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="not one of the protocol error codes"):
            Violation(error="not-a-code", message="x")

    def test_a_violation_needs_a_message(self) -> None:
        with pytest.raises(ValueError, match="non-empty human-readable message"):
            Violation(error="broken-reference", message="")

    def test_parsing_a_disagreeing_failure_class_is_refused(self) -> None:
        with pytest.raises(EnvelopeError, match="contradicts error_class_map"):
            ErrorResult.from_json(
                {
                    "response": "error",
                    "error": "stale-baseline",
                    "failure_class": "invalid",
                    "message": "the map is the source of truth",
                }
            )


class TestTheErrorResultShape:
    """``additional`` is ordered, never repeats the primary, and is never empty."""

    PRIMARY = Violation(error="unknown-field", message="swc has no field 'safety_level'")
    SECOND = Violation(error="immutable-field", message="cpu_core.core is immutable")
    THIRD = Violation(error="identity-conflict", message="uint16 is already declared")

    def test_a_lone_primary_omits_additional(self) -> None:
        assert "additional" not in ErrorResult(primary=self.PRIMARY).to_json()

    def test_additional_keeps_the_order_it_was_given(self) -> None:
        body = ErrorResult(primary=self.PRIMARY, additional=(self.SECOND, self.THIRD)).to_json()
        assert [entry["error"] for entry in body["additional"]] == ["immutable-field", "identity-conflict"]

    def test_additional_never_repeats_the_primary(self) -> None:
        with pytest.raises(ValueError, match="never repeats the primary"):
            ErrorResult(primary=self.PRIMARY, additional=(self.PRIMARY,))

    def test_the_primary_is_hoisted_to_the_top_level(self) -> None:
        body = ErrorResult(primary=self.PRIMARY, additional=(self.SECOND,)).to_json()
        assert body["error"] == "unknown-field"
        assert body["failure_class"] == "invalid"
        assert body["message"] == self.PRIMARY.message

    def test_addresses_are_carried_when_the_failure_has_one(self) -> None:
        address = Address(layer="swc", element="ExampleSWC")
        body = ErrorResult(primary=Violation(error="broken-reference", message="gone", addresses=(address,))).to_json()
        assert body["addresses"] == [{"layer": "swc", "element": "ExampleSWC"}]

    def test_an_error_result_round_trips(self) -> None:
        result = ErrorResult(primary=self.PRIMARY, additional=(self.SECOND,))
        assert ErrorResult.from_json(result.to_json()) == result

    def test_an_empty_additional_array_is_refused_on_parse(self) -> None:
        with pytest.raises(EnvelopeError, match="omitted entirely"):
            ErrorResult.from_json(
                {
                    "response": "error",
                    "error": "unknown-field",
                    "failure_class": "invalid",
                    "message": "x",
                    "additional": [],
                }
            )


# Security invariants, as tests


class TestNoRequestShapeTakesStorageOrText:
    """No request shape takes a storage path or raw text: the absence of a branch."""

    STORAGE_STRINGS = (
        "model/swc/example/ExampleSWC.yaml",
        "/etc/passwd",
        "../../etc/passwd",
        "name: ExampleSWC\ntype: application\n",
        "",
    )

    @pytest.mark.parametrize("value", STORAGE_STRINGS)
    def test_a_string_is_never_a_request(self, value: str) -> None:
        assert not _pass_a_accepts(value)

    @pytest.mark.parametrize("value", STORAGE_STRINGS)
    def test_a_string_is_never_an_address(self, value: str) -> None:
        assert not _pass_a_accepts({"operation": "read", "address": value})

    @pytest.mark.parametrize(
        "key", ["path", "file", "filename", "model_root", "dist_root", "yaml", "text", "content", "source", "raw"]
    )
    def test_no_request_shape_accepts_a_storage_or_text_key(self, key: str) -> None:
        for body in (
            {"operation": "discover", key: "x"},
            {"operation": "read", "address": _VALID_ADDRESS, key: "x"},
            _change_set({"operation": "remove", "address": _VALID_ADDRESS}, **{key: "x"}),
        ):
            assert not _pass_a_accepts(body), f"{key} reached a request shape"

    @pytest.mark.parametrize("key", ["path", "file", "yaml", "group", "directory"])
    def test_no_address_accepts_a_storage_key_beside_its_own(self, key: str) -> None:
        # `path` is an address's own key, a list of steps; a string there is
        # still refused, and no other storage key exists.
        assert not _pass_a_accepts({"operation": "read", "address": {**_VALID_ADDRESS, key: "swc/example"}})

    def test_a_create_identity_is_held_to_one_segment(self) -> None:
        # The one moment a client token contributes a path component.
        for identity in ("../../../.ssh/authorized_keys", "a/b", "..", ".", "a\\b", "a\x00b"):
            body = _change_set(
                {"operation": "create", "address": {"layer": "data_types", "element": identity}, "value": {}}
            )
            assert not _pass_a_accepts(body), identity


class TestThereIsNoCommitRequestAndNoHandle:
    """There is no commit request and no transaction handle."""

    def test_the_request_discriminator_is_closed_to_four(self, protocol_schema: dict[str, Any]) -> None:
        declared = [ref["$ref"].rsplit("/", 1)[-1] for ref in protocol_schema["$defs"]["request"]["oneOf"]]
        # `read` has four shapes (one address, a batch, the map, a count); none commits.
        assert declared == [
            "discoverRequest",
            "readRequest",
            "readManyRequest",
            "modelMapRequest",
            "findRequest",
            "countRequest",
            "changeSetRequest",
        ]
        assert REQUEST_OPERATIONS == ("discover", "read", "find", "change_set")

    @pytest.mark.parametrize("operation", ["commit", "abandon", "rename", "stage", "apply", "create", "update"])
    def test_no_other_operation_is_a_request(self, operation: str) -> None:
        assert not _pass_a_accepts({"operation": operation, "baseline": _VALID_TOKEN, "address": _VALID_ADDRESS})

    @pytest.mark.parametrize("key", ["transaction", "transaction_id", "handle", "change_set_id", "audit_ref"])
    def test_no_request_carries_a_transaction_handle(self, key: str) -> None:
        assert not _pass_a_accepts(_change_set({"operation": "remove", "address": _VALID_ADDRESS}, **{key: "t1"}))

    def test_a_staged_response_may_not_carry_an_audit_ref(self) -> None:
        # The nearest thing to a handle the protocol has, refused where it would read as one.
        with pytest.raises(ValueError, match="commit handle"):
            ChangeSetResponse(status="staged", baseline=_VALID_TOKEN, audit_ref="cs-20260815T102241Z-4f19c2")

    def test_parsing_a_staged_response_with_an_audit_ref_is_refused(self) -> None:
        with pytest.raises(EnvelopeError, match="commit handle"):
            ChangeSetResponse.from_json(
                {
                    "response": "change_set",
                    "status": "staged",
                    "baseline": _VALID_TOKEN,
                    "delta": [],
                    "audit_ref": "cs-20260815T102241Z-4f19c2",
                }
            )

    def test_a_committed_response_may_carry_one(self) -> None:
        response = ChangeSetResponse(status="committed", baseline=_VALID_TOKEN, audit_ref="cs-20260815T102241Z-4f19c2")
        assert response.to_json()["audit_ref"] == "cs-20260815T102241Z-4f19c2"


class TestThereIsNoEscapeHatch:
    """No force flag, no skip-validation parameter, no partial-commit mode."""

    ESCAPE_HATCHES = (
        "force",
        "force_commit",
        "skip_validation",
        "skip_checks",
        "no_validate",
        "partial",
        "partial_commit",
        "allow_partial",
        "dry_run",
        "overwrite",
    )

    @pytest.mark.parametrize("key", ESCAPE_HATCHES)
    def test_no_change_set_request_accepts_one(self, key: str) -> None:
        assert not _pass_a_accepts(_change_set({"operation": "remove", "address": _VALID_ADDRESS}, **{key: True}))

    @pytest.mark.parametrize("key", ESCAPE_HATCHES)
    def test_no_change_operation_accepts_one(self, key: str) -> None:
        assert not _pass_a_accepts(_change_set({"operation": "remove", "address": _VALID_ADDRESS, key: True}))

    @pytest.mark.parametrize("key", ESCAPE_HATCHES)
    def test_no_read_or_discover_accepts_one(self, key: str) -> None:
        assert not _pass_a_accepts({"operation": "discover", key: True})
        assert not _pass_a_accepts({"operation": "read", "address": _VALID_ADDRESS, key: True})

    def test_a_change_set_status_is_one_of_three(self) -> None:
        with pytest.raises(ValueError, match="status must be one of"):
            ChangeSetResponse(status="partially-committed", baseline=_VALID_TOKEN)


# The four response shapes


class TestResponseModels:
    """The models are a checked mirror of the responses the engine produces."""

    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        return fixtures.build_fixture(tmp_path / "project", "read-swc-element")

    def test_a_discover_response_round_trips(self, project: Path) -> None:
        response = handle_request({"operation": "discover"}, project=project)
        assert DiscoverResponse.from_json(response).to_json() == response

    def test_a_read_response_round_trips(self, project: Path) -> None:
        response = handle_request({"operation": "read", "address": _VALID_ADDRESS}, project=project)
        assert ReadResponse.from_json(response).to_json() == response

    def test_parse_response_dispatches_on_the_discriminator(self, project: Path) -> None:
        discover_body = handle_request({"operation": "discover"}, project=project)
        read_body = handle_request({"operation": "read", "address": _VALID_ADDRESS}, project=project)
        error_body = handle_request({"operation": "rename"}, project=project)
        assert isinstance(parse_response(discover_body), DiscoverResponse)
        assert isinstance(parse_response(read_body), ReadResponse)
        assert isinstance(parse_response(error_body), ErrorResult)

    def test_a_passport_carrying_a_storage_root_is_refused(self, project: Path) -> None:
        response = handle_request({"operation": "discover"}, project=project)
        response["project"]["model_root"] = "model"
        with pytest.raises(EnvelopeError, match="additionalProperties"):
            DiscoverResponse.from_json(response)

    def test_the_engine_answers_a_refused_request_with_an_error_result(self, project: Path) -> None:
        body = handle_request({"operation": "read", "address": "model/swc/example/ExampleSWC.yaml"}, project=project)
        assert body["response"] == "error"
        assert body["error"] == SCHEMA_VIOLATION
        assert body["failure_class"] == "invalid"

    def test_a_change_set_response_round_trips(self) -> None:
        response = ChangeSetResponse(
            status="committed",
            baseline=_VALID_TOKEN,
            delta=(
                DeltaEntry(
                    operation="create",
                    address=Address(layer="data_types", element="VehicleSpeed_kph"),
                    after={"name": "VehicleSpeed_kph"},
                    summary="Added the alias.",
                ),
            ),
            audit_ref="cs-20260815T102241Z-4f19c2",
        )
        assert ChangeSetResponse.from_json(response.to_json()) == response

    def test_an_absent_before_is_not_a_null_before(self) -> None:
        # `$defs/value` admits null, so "no before" and "before: null" are
        # different statements and the model keeps them apart.
        address = Address(layer="swc", element="ExampleSWC")
        cleared = DeltaEntry(operation="update", address=address, before="x", after=None)
        silent = DeltaEntry(operation="update", address=address, before="x")
        assert "after" in cleared.to_json() and cleared.to_json()["after"] is None
        assert "after" not in silent.to_json()
        assert silent.after is ABSENT

    def test_a_delta_entry_names_an_element(self) -> None:
        with pytest.raises(ValueError, match="names an element"):
            DeltaEntry(operation="remove", address=Address(layer="swc"))

    @pytest.mark.parametrize("operation", CHANGE_OPERATION_NAMES)
    def test_every_change_operation_name_is_a_delta_operation(self, operation: str) -> None:
        entry = DeltaEntry(operation=operation, address=Address(layer="swc", element="ExampleSWC"))
        assert DeltaEntry.from_json(entry.to_json()) == entry

    def test_a_response_shape_outside_the_union_is_refused(self) -> None:
        with pytest.raises(EnvelopeError, match="not a protocol response shape"):
            parse_response({"response": "commit", "baseline": _VALID_TOKEN})

    def test_a_response_without_a_discriminator_is_refused(self) -> None:
        with pytest.raises(EnvelopeError, match="requires"):
            parse_response({"baseline": _VALID_TOKEN})


@pytest.fixture(scope="module")
def delta_entry_validator(protocol_schema: dict[str, Any]) -> Draft202012Validator:
    """A validator for ``#/$defs/deltaEntry`` alone."""
    registry = Registry().with_resources([(protocol_schema["$id"], Resource.from_contents(protocol_schema))])
    return Draft202012Validator({"$ref": f"{protocol_schema['$id']}#/$defs/deltaEntry"}, registry=registry)


class TestTheBeforeFormRule:
    """``before_form``: a ``remove`` at an element address alone, and ``summary`` its one value.

    Stated twice (``DeltaEntry`` and ``$defs/deltaEntry``), so each case is put to both.
    """

    _SUMMARISED = {
        "operation": "remove",
        "address": {"layer": "swc", "element": "ExampleSWC"},
        "before": {"name": "ExampleSWC", "group": "example", "ports": 1},
        "before_form": "summary",
    }

    def test_a_summarised_element_remove_round_trips_and_the_schema_accepts_it(
        self, delta_entry_validator: Draft202012Validator
    ) -> None:
        entry = DeltaEntry.from_json(self._SUMMARISED)

        assert entry.before_form == "summary"
        assert entry.to_json() == self._SUMMARISED
        assert DeltaEntry.from_json(entry.to_json()) == entry
        assert delta_entry_validator.is_valid(self._SUMMARISED)

    @pytest.mark.parametrize(
        "change",
        [
            {"operation": "create", "after": {"name": "ExampleSWC"}},
            {"operation": "update", "after": {"name": "ExampleSWC"}},
            {"address": {"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}]}},
            {"before_form": "whole"},
        ],
        ids=["on-a-create", "on-an-update", "on-a-member-remove", "an-unknown-value"],
    )
    def test_both_sides_refuse_it_anywhere_else(
        self, change: dict[str, Any], delta_entry_validator: Draft202012Validator
    ) -> None:
        body = {**self._SUMMARISED, **change}

        assert not delta_entry_validator.is_valid(body)
        with pytest.raises(ValueError, match="before_form"):
            DeltaEntry.from_json(body)
        with pytest.raises(ValueError, match="before_form"):
            DeltaEntry(
                operation=body["operation"],
                address=Address.from_json(body["address"]),
                before=body["before"],
                before_form=body["before_form"],
            )


class TestResponseParsingRefusals:
    """Each response shape refuses what its ``$def`` refuses, key by key."""

    def test_a_wrong_discriminator_is_not_a_shape_mismatch_to_guess_at(self) -> None:
        with pytest.raises(EnvelopeError, match="must be 'read'"):
            ReadResponse.from_json({"response": "discover", "address": _VALID_ADDRESS, "form": "full", "value": None})

    def test_a_read_response_naming_an_undeclared_form_is_refused(self) -> None:
        # The response side refuses an unknown form as the request side does, so a
        # client parsing a stale server's answer is not left to decide what "wide" means.
        with pytest.raises(EnvelopeError):
            ReadResponse.from_json({"response": "read", "address": _VALID_ADDRESS, "form": "wide", "value": None})

    @pytest.mark.parametrize(
        "detail",
        [
            {"hints": [{"operation": "read", "address": _VALID_ADDRESS, "form": "index"}]},
            {"measured": {"wire_bytes": 1, "budget": 1, "elements": 1}},
        ],
        ids=["hints", "measured"],
    )
    def test_a_code_that_is_not_over_budget_may_not_carry_the_chain_keys(self, detail: dict) -> None:
        """The over-budget pairing, refused on the way in as an ``EnvelopeError``, not a bare ``ValueError``.

        ``test_editing_budget.py`` holds the schema's half of the same rule.
        """
        with pytest.raises(EnvelopeError, match="over-budget"):
            Violation.from_json({"error": "unknown-field", "failure_class": "invalid", "message": "x"} | detail)

    @pytest.mark.parametrize("key", ["hints", "measured"])
    def test_a_null_optional_key_is_not_an_omitted_one(self, key: str) -> None:
        # `null` on the wire is a producer that thinks it is saying something:
        # refused, as for `addresses` and `after`.
        detail = {"error": "unknown-field", "failure_class": "invalid", "message": "x", key: None}
        with pytest.raises(EnvelopeError):
            Violation.from_json(detail)

    @pytest.mark.parametrize(
        "bad",
        [
            {"forms": "summary", "default_form": {}, "budget_wire_bytes": 1},
            {"forms": ["summary", "wide"], "default_form": {}, "budget_wire_bytes": 1},
            {"forms": ["summary"], "default_form": {"layer": "wide"}, "budget_wire_bytes": 1},
            {"forms": ["summary"], "default_form": {"layer": "summary"}, "budget_wire_bytes": 1},
            {"forms": ["summary"], "default_form": None, "budget_wire_bytes": 1},
            {"forms": ["summary"], "default_form": {}, "budget_wire_bytes": -1},
            {"forms": ["summary"], "default_form": {}, "budget_wire_bytes": "80000"},
            {"forms": ["summary"], "default_form": {}, "budget_wire_bytes": True},
            {"forms": ["summary"], "default_form": {}},
            {"forms": ["summary"], "default_form": {}, "budget_wire_bytes": 1, "extra": 1},
            # `$defs/readsCapability.forms` says `minItems: 1`: a server
            # publishing no form accepts no read.
            {"forms": [], "default_form": {}, "budget_wire_bytes": 1},
        ],
        ids=[
            "forms-not-an-array",
            "forms-with-an-undeclared-member",
            "default-form-naming-an-undeclared-form",
            "default-form-missing-shapes",
            "default-form-not-an-object",
            "negative-budget",
            "budget-as-a-string",
            "budget-as-a-bool",
            "missing-budget",
            "an-extra-key",
            "no-forms-at-all",
        ],
    )
    def test_a_malformed_reads_capability_is_refused(self, bad: dict) -> None:
        """Every refusal branch of the ``$defs/readsCapability`` parser, one case each.

        `default_form` is a closed table over the four address shapes, so a partial one is refused.
        """
        with pytest.raises(EnvelopeError):
            ReadsCapability.from_json(bad)

    def test_a_reads_capability_round_trips(self) -> None:
        published = read.reads_capability()
        assert ReadsCapability.from_json(published.to_json()) == published

    def test_a_violation_with_an_undeclared_code_is_refused_on_parse(self) -> None:
        with pytest.raises(EnvelopeError, match="not one of the protocol error codes"):
            Violation.from_json({"error": "almost-right", "failure_class": "invalid", "message": "x"})

    def test_an_error_result_exposes_the_primarys_class(self) -> None:
        result = ErrorResult(primary=Violation(error="stale-baseline", message="the model moved"))
        assert result.error == "stale-baseline"
        assert result.failure_class == "stale"

    def test_an_empty_addresses_array_is_refused(self) -> None:
        with pytest.raises(EnvelopeError, match="omitted entirely"):
            Violation.from_json({"error": "unknown-field", "failure_class": "invalid", "message": "x", "addresses": []})

    def test_addresses_round_trip_through_a_violation(self) -> None:
        violation = Violation(
            error="broken-reference",
            message="IExample is gone",
            addresses=(Address(layer="swc", element="ExampleSWC"), Address(layer="interfaces")),
        )
        assert Violation.from_json(violation.to_json()) == violation

    def test_layers_must_be_an_array(self) -> None:
        with pytest.raises(EnvelopeError, match="layers must be an array"):
            DiscoverResponse.from_json(
                {
                    "response": "discover",
                    "project": {"name": "p", "arxml_version": "4.2.2", "swcraft_version": "0.1.0"},
                    "baseline": _VALID_TOKEN,
                    "reads": _VALID_READS,
                    "form": "summary",
                    "layers": {},
                }
            )

    def test_a_passport_states_a_declared_arxml_version(self) -> None:
        with pytest.raises(EnvelopeError, match="arxml_version"):
            DiscoverResponse.from_json(
                {
                    "response": "discover",
                    "project": {"name": "p", "arxml_version": "4.4.0", "swcraft_version": "0.1.0"},
                    "baseline": _VALID_TOKEN,
                    "reads": _VALID_READS,
                    "form": "summary",
                    "layers": [],
                }
            )

    def test_an_unframed_baseline_is_refused(self) -> None:
        with pytest.raises(EnvelopeError, match="revisionToken"):
            ChangeSetResponse.from_json(
                {"response": "change_set", "status": "abandoned", "baseline": "deadbeef", "delta": []}
            )

    def test_an_undeclared_status_is_refused(self) -> None:
        with pytest.raises(EnvelopeError, match="status must be one of"):
            ChangeSetResponse.from_json(
                {"response": "change_set", "status": "half-done", "baseline": _VALID_TOKEN, "delta": []}
            )

    def test_a_delta_that_is_not_an_array_is_refused(self) -> None:
        with pytest.raises(EnvelopeError, match="delta must be an array"):
            ChangeSetResponse.from_json(
                {"response": "change_set", "status": "staged", "baseline": _VALID_TOKEN, "delta": {}}
            )

    def test_a_delta_entry_operation_is_one_of_three(self) -> None:
        with pytest.raises(ValueError, match="deltaEntry operation"):
            DeltaEntry(operation="rename", address=Address(layer="swc", element="ExampleSWC"))
        with pytest.raises(EnvelopeError, match="deltaEntry operation"):
            DeltaEntry.from_json({"operation": "rename", "address": _VALID_ADDRESS})


class TestNullIsNeverAnOmittedKey:
    """Every optional key of the response side refuses an explicit ``null``, as the request side does.

    ``before`` and ``after`` are not here: ``null`` is a model value at those keys,
    which is why they use the ``ABSENT`` sentinel to say "omitted".
    """

    def test_addresses(self) -> None:
        with pytest.raises(EnvelopeError, match="omitted entirely"):
            Violation.from_json(
                {"error": "unknown-field", "failure_class": "invalid", "message": "x", "addresses": None}
            )

    def test_additional(self) -> None:
        with pytest.raises(EnvelopeError, match="omitted entirely"):
            ErrorResult.from_json(
                {
                    "response": "error",
                    "error": "unknown-field",
                    "failure_class": "invalid",
                    "message": "x",
                    "additional": None,
                }
            )

    def test_audit_ref(self) -> None:
        with pytest.raises(EnvelopeError, match="omitted entirely"):
            ChangeSetResponse.from_json(
                {
                    "response": "change_set",
                    "status": "committed",
                    "baseline": _VALID_TOKEN,
                    "delta": [],
                    "audit_ref": None,
                }
            )

    def test_a_delta_entrys_summary(self) -> None:
        with pytest.raises(EnvelopeError, match="omitted entirely"):
            DeltaEntry.from_json({"operation": "create", "address": _VALID_ADDRESS, "summary": None})

    def test_a_before_or_after_may_still_be_null(self) -> None:
        entry = DeltaEntry.from_json({"operation": "update", "address": _VALID_ADDRESS, "before": None, "after": 1})
        assert entry.before is None and entry.after == 1


class TestAClosedVocabularyRefusesAnUnhashableValue:
    """A closed vocabulary refuses an unhashable value with an ``EnvelopeError``.

    Guards a defect: membership in a set hashed ``{}`` or ``[]`` and raised a bare ``TypeError``.
    """

    @pytest.mark.parametrize("value", [{}, [], {"a": 1}, [1]], ids=["object", "array", "full-object", "full-array"])
    def test_the_response_discriminator(self, value: Any) -> None:
        with pytest.raises(EnvelopeError, match="not a protocol response shape"):
            parse_response({"response": value})

    @pytest.mark.parametrize("value", [{}, []], ids=["object", "array"])
    def test_a_change_set_status(self, value: Any) -> None:
        with pytest.raises(EnvelopeError, match="status must be one of"):
            ChangeSetResponse.from_json(
                {"response": "change_set", "status": value, "baseline": _VALID_TOKEN, "delta": []}
            )

    @pytest.mark.parametrize("value", [{}, []], ids=["object", "array"])
    def test_an_error_code(self, value: Any) -> None:
        with pytest.raises(EnvelopeError, match="not one of the protocol error codes"):
            Violation.from_json({"error": value, "failure_class": "invalid", "message": "x"})

    @pytest.mark.parametrize("value", [{}, []], ids=["object", "array"])
    def test_an_arxml_version(self, value: Any) -> None:
        with pytest.raises(EnvelopeError, match="arxml_version"):
            DiscoverResponse.from_json(
                {
                    "response": "discover",
                    "project": {"name": "p", "arxml_version": value, "swcraft_version": "0.1.0"},
                    "baseline": _VALID_TOKEN,
                    "reads": _VALID_READS,
                    "form": "summary",
                    "layers": [],
                }
            )


class TestThePageShapes:
    """``$defs/page`` and the two request keys that produce it."""

    def test_a_paged_read_request_round_trips_through_pass_a(self) -> None:
        body = {
            "operation": "read",
            "address": {"layer": "data_types"},
            "form": "index",
            "limit": 4,
            "after": "float64",
        }
        parsed = pass_a(body)
        assert parsed == ReadRequest(address=Address(layer="data_types"), form="index", limit=4, after="float64")
        assert parsed.to_json() == body

    def test_an_omitted_page_is_not_an_absent_cursor_key(self) -> None:
        # `ABSENT` and not `None`: `$defs/value` admits `null`, so a cursor naming
        # `null` and no cursor are two statements.
        assert pass_a({"operation": "read", "address": {"layer": "swc"}}).after is ABSENT
        with_null = pass_a({"operation": "read", "address": {"layer": "swc"}, "limit": 2, "after": None})
        assert with_null.after is None
        assert with_null.to_json()["after"] is None

    @pytest.mark.parametrize("limit", [0, -1, 3.5, True, "4", None, [], {}])
    def test_a_limit_that_is_not_a_page_size_is_refused(self, limit: Any) -> None:
        # An explicit `null` is refused rather than read as "no page"; `true` is
        # there because `bool` is an `int` subclass.
        with pytest.raises(EnvelopeError, match="page size"):
            ReadRequest.from_json({"operation": "read", "address": {"layer": "swc"}, "limit": limit})

    def test_a_cursor_without_a_page_size_is_refused_on_both_sides(self) -> None:
        # `dependentRequired` in the schema and `__post_init__` here: refused in the same words.
        with pytest.raises(EnvelopeError, match="bounded window"):
            ReadRequest.from_json({"operation": "read", "address": {"layer": "swc"}, "after": "X"})
        with pytest.raises(EnvelopeError, match="bounded window"):
            ReadRequest(address=Address(layer="swc"), after="X")
        # And pass A answers both with the code every envelope refusal carries.
        with pytest.raises(ProtocolError, match="bounded window"):
            pass_a({"operation": "read", "address": {"layer": "swc"}, "after": "X"})

    def test_a_page_block_round_trips_and_omits_an_absent_cursor(self) -> None:
        last = Page(total=12, returned=2, truncated=True)
        assert last.to_json() == {"total": 12, "returned": 2, "truncated": True}
        assert Page.from_json(last.to_json()) == last
        more = Page(total=12, returned=4, truncated=True, next="int8")
        assert Page.from_json(more.to_json()) == more

    @pytest.mark.parametrize(
        "bad",
        [
            {"total": 12, "returned": 4},
            {"total": 12, "returned": 4, "truncated": "yes"},
            {"total": -1, "returned": 4, "truncated": True},
            {"total": 12, "returned": True, "truncated": True},
            {"total": 12, "returned": 3.5, "truncated": True},
            {"total": 12, "returned": 4, "truncated": True, "extra": 1},
        ],
        ids=["missing-truncated", "truncated-not-boolean", "negative-total", "bool-returned", "float", "extra-key"],
    )
    def test_a_malformed_page_block_is_refused(self, bad: dict) -> None:
        with pytest.raises(EnvelopeError):
            Page.from_json(bad)

    def test_the_cross_key_arithmetic_is_deliberately_not_re_derived_here(self) -> None:
        """The parser does not re-derive cross-key arithmetic: Draft 2020-12 cannot state it.

        A check here and none in ``$defs/page`` would be a schema/parser divergence;
        the arithmetic is :func:`~swcraft_tools.editing.read.paginate`'s.
        """
        assert Page.from_json({"total": 1, "returned": 9, "truncated": False}).returned == 9

    def test_a_read_response_carries_the_page_only_when_there_was_one(self) -> None:
        address = Address(layer="data_types")
        assert "page" not in ReadResponse(address=address, value=[], form="summary").to_json()
        paged = ReadResponse(address=address, value=[], form="summary", page=Page(12, 0, True)).to_json()
        assert paged["page"] == {"total": 12, "returned": 0, "truncated": True}
        assert ReadResponse.from_json(paged).page == Page(12, 0, True)

    def test_an_explicit_null_page_is_not_an_omitted_one(self) -> None:
        with pytest.raises(EnvelopeError, match="omitted entirely"):
            ReadResponse.from_json(
                {"response": "read", "address": {"layer": "swc"}, "form": "summary", "page": None, "value": []}
            )

    def test_the_reads_capability_publishes_the_hint_page_size(self) -> None:
        published = read.reads_capability()
        assert published.hint_page_size == read.HINT_PAGE_SIZE
        assert published.to_json()["hint_page_size"] == read.HINT_PAGE_SIZE

    @pytest.mark.parametrize("size", [0, -1, 3.5, True, "50", None])
    def test_a_reads_capability_with_a_page_size_that_is_not_one_is_refused(self, size: Any) -> None:
        block = read.reads_capability().to_json() | {"hint_page_size": size}
        with pytest.raises(EnvelopeError, match="hint_page_size"):
            ReadsCapability.from_json(block)

    def test_the_reads_capability_publishes_the_join_row_ceiling(self) -> None:
        """``reads.join.row_ceiling`` is ``read.JOIN_ROW_CEILING``, read at the call, and it round-trips."""
        block = read.reads_capability().to_json()
        assert block["join"] == {"row_ceiling": read.JOIN_ROW_CEILING, "max_entries": query.JOIN_MAX_ENTRIES}
        assert ReadsCapability.from_json(block).to_json() == block

    @pytest.mark.parametrize(
        "join",
        [
            None,
            {},
            {"row_ceiling": 0, "max_entries": 8},
            {"row_ceiling": -1, "max_entries": 8},
            {"row_ceiling": 2.5, "max_entries": 8},
            {"row_ceiling": True, "max_entries": 8},
            {"row_ceiling": 10, "max_entries": 8, "extra": 1},
            {"row_ceiling": 10},
            {"row_ceiling": 10, "max_entries": 0},
            {"row_ceiling": 10, "max_entries": -1},
            {"row_ceiling": 10, "max_entries": 2.5},
            {"row_ceiling": 10, "max_entries": True},
            {"row_ceiling": 10, "max_entries": "8"},
            {"row_ceiling": 10, "max_entries": None},
        ],
    )
    def test_a_reads_capability_with_a_join_block_that_is_not_one_is_refused(self, join: Any) -> None:
        block = read.reads_capability().to_json() | {"join": join}
        with pytest.raises(EnvelopeError):
            ReadsCapability.from_json(block)

    def test_a_reads_capability_without_its_join_block_is_refused(self) -> None:
        block = read.reads_capability().to_json()
        del block["join"]
        with pytest.raises(EnvelopeError):
            ReadsCapability.from_json(block)

    def test_the_reads_capability_publishes_the_computed_value_vocabulary(self) -> None:
        """``reads.derived`` is ``validator.DERIVED`` as published, and it round-trips the parser."""
        from swcraft_tools.validator import DERIVED

        block = read.reads_capability().to_json()
        assert block["derived"] == [entry.as_json() for entry in DERIVED]
        assert ReadsCapability.from_json(block).to_json() == block
        block["derived"][0]["shape"]["provider"]["swc"] = "mutated"
        assert read.reads_capability().to_json()["derived"][0]["shape"]["provider"]["swc"] == "string"

    @pytest.mark.parametrize(
        ("mutate", "match"),
        [
            (lambda block: block.pop("derived"), "derived"),
            (lambda block: block.update(derived={}), "derived must be an array"),
            (lambda block: block["derived"][0].pop("shape"), "shape"),
            (lambda block: block["derived"][0].update(extra=1), "extra"),
            (lambda block: block["derived"][0].update(name=""), "non-empty string"),
            (lambda block: block["derived"][0].update(rows="many"), "rows is one of"),
            (lambda block: block["derived"][0]["at"].update(layer="Not A Layer"), "layer name"),
            (lambda block: block["derived"][1]["at"].update(members=[]), "member paths"),
            (lambda block: block["derived"][1]["at"].update(members=["runnables."]), "member paths"),
            (lambda block: block["derived"][1]["at"].update(where=[]), "where"),
            (lambda block: block["derived"][1].update(shape={}), "at least one key"),
            (lambda block: block["derived"][1]["shape"].update(base="number"), "leaf is one of"),
            (lambda block: block["derived"][0]["shape"]["provider"].update(swc=["string"]), "shape"),
        ],
        ids=[
            "absent",
            "not-an-array",
            "entry-without-shape",
            "entry-with-extra-key",
            "empty-name",
            "unknown-rows",
            "bad-layer",
            "empty-members",
            "empty-step",
            "at-with-extra-key",
            "empty-shape",
            "unknown-leaf",
            "array-leaf",
        ],
    )
    def test_a_reads_capability_with_a_malformed_vocabulary_is_refused(self, mutate: Any, match: str) -> None:
        block = read.reads_capability().to_json()
        mutate(block)
        with pytest.raises(EnvelopeError, match=match):
            ReadsCapability.from_json(block)


class TestTheMemberSliceShapes:
    """``members`` on both halves of the envelope.

    The envelope owns only that it is a non-empty name; an undeclared collection is
    the engine's ``unknown-field``. Pinned: ``null`` is not an omitted key, and the
    echo is absent exactly when there was no slice.
    """

    def test_a_sliced_read_request_round_trips_through_pass_a(self) -> None:
        body = {
            "operation": "read",
            "address": {"layer": "swc"},
            "members": "ports",
            "where": [{"field": "interface", "equals": "IExample"}],
            "limit": 2,
        }
        parsed = pass_a(body)
        assert parsed.members == "ports"
        assert parsed.to_json() == body

    @pytest.mark.parametrize("members", ["", 42, True, [], {}], ids=["empty", "int", "bool", "list", "obj"])
    def test_a_members_that_is_not_a_field_name_is_refused_on_both_sides(self, members: Any) -> None:
        match = "names one declared collection field"
        with pytest.raises(EnvelopeError, match=match):
            ReadRequest.from_json({"operation": "read", "address": {"layer": "swc"}, "members": members})
        with pytest.raises(EnvelopeError, match=match):
            ReadRequest(address=Address(layer="swc"), members=members)
        # `read()` builds a `ReadResponse` directly, so its guard is the only
        # thing between a bad slice name and the wire.
        with pytest.raises(EnvelopeError, match=match):
            ReadResponse(address=Address(layer="swc"), value=[], form="full", members=members)

    @pytest.mark.parametrize(
        "kwargs",
        [{}, {"equals": "x", "exists": True}],
        ids=["neither", "both"],
    )
    def test_a_where_clause_carrying_neither_or_both_keys_is_refused_on_both_sides(self, kwargs: Any) -> None:
        """A where clause with neither or both of ``equals``/``exists`` is refused on both sides.

        Otherwise ``to_json`` would silently emit the ``equals`` half alone.
        """
        match = "exactly one of `equals` and `exists`"
        with pytest.raises(EnvelopeError, match=match):
            WhereClause(field="unit", **kwargs)
        body = {"field": "unit", **{key: value for key, value in kwargs.items()}}
        with pytest.raises(EnvelopeError, match=match):
            WhereClause.from_json(body)

    @pytest.mark.parametrize("exists", ["true", 1, 0, None, []], ids=["str", "one", "zero", "null", "list"])
    def test_an_exists_that_is_not_a_boolean_is_refused(self, exists: Any) -> None:
        """``exists`` must be a boolean, as ``$defs/whereClause`` types it; ``1`` and ``0`` are refused."""
        with pytest.raises(EnvelopeError, match="`exists` is true or false"):
            WhereClause.from_json({"field": "unit", "exists": exists})

    @pytest.mark.parametrize("limit", [0, -1, True, 1.5, "2"], ids=["zero", "negative", "bool", "fraction", "str"])
    def test_a_limit_that_is_not_a_page_size_is_refused_on_both_sides(self, limit: Any) -> None:
        """A limit that is not a page size is refused on both sides.

        ``read._refuse_over_budget`` builds ``ReadRequest(limit=HINT_PAGE_SIZE)``, so a bad
        ``HINT_PAGE_SIZE`` would hand a client a hint the schema refuses.
        """
        match = "page size of at least one"
        with pytest.raises(EnvelopeError, match=match):
            ReadRequest(address=Address(layer="swc"), limit=limit)
        with pytest.raises(EnvelopeError, match=match):
            ReadRequest.from_json({"operation": "read", "address": {"layer": "swc"}, "limit": limit})

    def test_an_explicit_null_members_is_refused_by_the_parser_alone(self) -> None:
        """``members: null`` is refused by the parser alone.

        On the wire a key a producer sent is a key it meant; in Python ``None`` is the
        omitted argument, and no sentinel is needed to tell them apart.
        """
        with pytest.raises(EnvelopeError, match="omitted entirely"):
            ReadRequest.from_json({"operation": "read", "address": {"layer": "swc"}, "members": None})
        assert ReadRequest(address=Address(layer="swc"), members=None).members is None

    def test_a_read_response_carries_the_slice_only_when_there_was_one(self) -> None:
        address = Address(layer="swc")
        assert "members" not in ReadResponse(address=address, value=[], form="summary").to_json()
        sliced = ReadResponse(address=address, value=[], form="summary", members="ports").to_json()
        assert sliced["members"] == "ports"
        assert ReadResponse.from_json(sliced).members == "ports"

    def test_an_explicit_null_members_is_not_an_omitted_one(self) -> None:
        with pytest.raises(EnvelopeError, match="omitted entirely"):
            ReadResponse.from_json(
                {"response": "read", "address": {"layer": "swc"}, "form": "summary", "members": None, "value": []}
            )


#: A members path, a computed value, and a two-entry join whose second `left`
#: reaches the first entry's `as`.
PILOT_REQUEST: dict[str, Any] = {
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
            "with": {"address": {"layer": "swc"}, "members": "runnables.reads"},
            "on": [
                {"left": "connection.derived.connection_ends.requester.swc", "right": "element"},
                {"left": "connection.derived.connection_ends.requester.port", "right": "member.port"},
                {"left": "member.element", "right": "member.element"},
            ],
            "as": "reader",
        },
    ],
}


class TestTheIdentityRowShapes:
    """The ``members`` path, ``derived`` and ``join`` request keys on both halves of the envelope.

    The envelope owns their shapes, the two ``$defs/rowForm`` combination rules and
    one rule the schema cannot state: no join entry repeats an earlier ``as``. Which
    names are declared is the engine's answer.
    """

    def test_the_pilot_request_parses_and_round_trips(self) -> None:
        parsed = pass_a(PILOT_REQUEST)
        assert isinstance(parsed, ReadRequest)
        assert parsed.members == "runnables.writes"
        assert parsed.derived == ("base_type",)
        assert [entry.alias for entry in parsed.join] == ["connection", "reader"]
        assert parsed.join[0].read == JoinedRead(address=Address(layer="connectors"), derived=("connection_ends",))
        assert parsed.join[1].on[2] == JoinCondition(left="member.element", right="member.element")
        assert parsed.to_json() == PILOT_REQUEST
        assert ReadRequest.from_json(parsed.to_json()) == parsed

    def test_a_python_built_request_equals_the_parsed_one(self) -> None:
        entry = JoinEntry(
            read=JoinedRead(address=Address(layer="connectors"), derived=["connection_ends"]),
            on=[JoinCondition(left="element", right="element")],
            alias="connection",
        )
        built = ReadRequest(address=Address(layer="swc"), form="index", derived=["base_type"], join=[entry])
        assert built.join == (entry,)
        assert ReadRequest.from_json(built.to_json()) == built

    def test_the_response_echoes_derived_and_join(self) -> None:
        parsed = pass_a(PILOT_REQUEST)
        response = ReadResponse(
            address=parsed.address,
            value={"count": 0, "identities": []},
            form="index",
            members=parsed.members,
            derived=parsed.derived,
            join=parsed.join,
        )
        body = response.to_json()
        assert body["derived"] == PILOT_REQUEST["derived"]
        assert body["join"] == PILOT_REQUEST["join"]
        assert list(body) == ["response", "address", "form", "members", "derived", "join", "value"]
        assert ReadResponse.from_json(body) == response

    def test_a_response_without_the_keys_carries_neither(self) -> None:
        body = ReadResponse(address=Address(layer="swc"), value=[], form="summary").to_json()
        assert "derived" not in body
        assert "join" not in body

    @pytest.mark.parametrize("members", ["foo-bar", "ports", "runnables.writes", "a.b.c"])
    def test_a_members_value_with_no_empty_step_is_the_engines_question(self, members: str) -> None:
        # An undeclared name such as `foo-bar` stays the engine's `unknown-field`:
        # the pattern refuses an empty step and nothing else.
        assert pass_a({"operation": "read", "address": {"layer": "swc"}, "members": members}).members == members

    @pytest.mark.parametrize("members", [".", ".ports", "ports.", "a..b"])
    def test_an_empty_step_is_refused_on_every_side(self, members: str) -> None:
        match = "no step of the path is empty"
        with pytest.raises(EnvelopeError, match=match):
            ReadRequest.from_json({"operation": "read", "address": {"layer": "swc"}, "members": members})
        with pytest.raises(EnvelopeError, match=match):
            ReadRequest(address=Address(layer="swc"), members=members)
        with pytest.raises(EnvelopeError, match=match):
            ReadResponse(address=Address(layer="swc"), value=[], form="summary", members=members)
        with pytest.raises(EnvelopeError, match=match):
            JoinedRead(address=Address(layer="swc"), members=members)
        assert not _pass_a_accepts({"operation": "read", "address": {"layer": "swc"}, "members": members})

    @pytest.mark.parametrize("form", [None, "summary", "full"], ids=["omitted", "summary", "full"])
    @pytest.mark.parametrize("key", ["derived", "join"])
    def test_rows_without_an_explicit_index_form_are_refused(self, form: str | None, key: str) -> None:
        body = {key: PILOT_REQUEST[key]} | ({} if form is None else {"form": form})
        with pytest.raises(EnvelopeError, match='states `form: "index"`'):
            ReadRequest.from_json({"operation": "read", "address": {"layer": "swc"}} | body)
        typed = {"derived": ("base_type",), "join": pass_a(PILOT_REQUEST).join}[key]
        with pytest.raises(EnvelopeError, match='states `form: "index"`'):
            ReadRequest(address=Address(layer="swc"), form=form, **{key: typed})
        with pytest.raises(EnvelopeError, match='states `form: "index"`'):
            ReadResponse(address=Address(layer="swc"), value=[], form=form or "summary", **{key: typed})

    @pytest.mark.parametrize(
        "address",
        [_VALID_ADDRESS, _VALID_FIELD_ADDRESS, {**_VALID_ADDRESS, "path": [{"field": "ports"}, {"member": "pOut"}]}],
        ids=["element", "field", "member"],
    )
    @pytest.mark.parametrize("key", ["derived", "join"])
    def test_rows_at_an_address_that_is_not_a_layer_are_refused(self, address: dict, key: str) -> None:
        body = {"operation": "read", "address": address, "form": "index", key: PILOT_REQUEST[key]}
        with pytest.raises(EnvelopeError, match="names a layer and no element"):
            ReadRequest.from_json(body)
        assert not _pass_a_accepts(body)

    def test_a_joined_read_at_an_element_is_refused(self) -> None:
        with pytest.raises(EnvelopeError, match="names a layer and no element"):
            JoinedRead(address=Address(layer="swc", element="ExampleSWC"))

    @pytest.mark.parametrize(("key", "value"), [("form", "index"), ("limit", 5), ("after", "x"), ("join", [])])
    def test_a_joined_read_carries_no_form_page_or_join(self, key: str, value: Any) -> None:
        with pytest.raises(EnvelopeError, match="additionalProperties"):
            JoinedRead.from_json({"address": {"layer": "swc"}, key: value})

    @pytest.mark.parametrize("alias", sorted(RESERVED_JOIN_ALIASES))
    def test_a_reserved_alias_is_refused(self, alias: str) -> None:
        with pytest.raises(EnvelopeError, match="a key every row carries"):
            JoinEntry(read=JoinedRead(address=Address(layer="swc")), on=[JoinCondition("a", "b")], alias=alias)

    @pytest.mark.parametrize("alias", ["a.b", "1a", "", "a-b", 42, "a\n"])
    def test_an_alias_outside_the_pattern_is_refused(self, alias: Any) -> None:
        with pytest.raises(EnvelopeError, match="dot-free"):
            JoinEntry(read=JoinedRead(address=Address(layer="swc")), on=[JoinCondition("a", "b")], alias=alias)

    def test_a_repeated_alias_is_refused_by_the_envelope(self) -> None:
        repeated = PILOT_REQUEST | {"join": [PILOT_REQUEST["join"][0], PILOT_REQUEST["join"][0]]}
        with pytest.raises(EnvelopeError, match="already an earlier entry's `as`"):
            ReadRequest.from_json(repeated)
        assert not _pass_a_accepts(repeated)

    def test_prefix_sharing_aliases_are_two_aliases(self) -> None:
        both = PILOT_REQUEST | {
            "join": [PILOT_REQUEST["join"][1], PILOT_REQUEST["join"][1] | {"as": "reader2"}],
        }
        assert [entry.alias for entry in pass_a(both).join] == ["reader", "reader2"]

    @pytest.mark.parametrize(
        "derived",
        [[], [""], ["x", "x"], "base_type", [42], None],
        ids=["empty", "blank", "twice", "str", "int", "null"],
    )
    def test_a_malformed_derived_is_refused(self, derived: Any) -> None:
        body = {"operation": "read", "address": {"layer": "swc"}, "form": "index", "derived": derived}
        assert not _pass_a_accepts(body)

    @pytest.mark.parametrize("join", [[], None, {}, "x"], ids=["empty", "null", "obj", "str"])
    def test_a_malformed_join_is_refused(self, join: Any) -> None:
        body = {"operation": "read", "address": {"layer": "swc"}, "form": "index", "join": join}
        assert not _pass_a_accepts(body)

    def test_an_empty_on_is_refused_on_both_sides(self) -> None:
        with pytest.raises(EnvelopeError, match="at least one condition"):
            JoinEntry(read=JoinedRead(address=Address(layer="swc")), on=[], alias="a")
        body = PILOT_REQUEST | {"join": [PILOT_REQUEST["join"][0] | {"on": []}]}
        assert not _pass_a_accepts(body)

    @pytest.mark.parametrize(("left", "right"), [("", "a"), ("a", ""), (1, "a")])
    def test_a_malformed_condition_is_refused(self, left: Any, right: Any) -> None:
        with pytest.raises(EnvelopeError, match="non-empty row path"):
            JoinCondition(left=left, right=right)

    def test_a_request_valid_before_serializes_to_the_same_bytes(self) -> None:
        """Every shipped read vector's input, re-serialized: no new key appears and the old ones keep their order."""
        reads = [_load(path)["input"] for path in ALL_VECTOR_FILES]
        # A vector carrying `derived`, `join`, `addresses` or no `address` is not a
        # request of the earlier shape.
        reads = [
            body
            for body in reads
            if isinstance(body, dict)
            and body.get("operation") == "read"
            and "address" in body
            and not {"derived", "join"} & set(body)
        ]
        legacy_order = ["operation", "address", "members", "form", "where", "limit", "after"]
        compared = 0
        for body in reads:
            if not _pass_a_accepts(body):
                continue
            serialized = pass_a(body).to_json()
            assert "derived" not in serialized and "join" not in serialized
            assert json.dumps(serialized, sort_keys=True) == json.dumps(body, sort_keys=True)
            assert list(serialized) == [key for key in legacy_order if key in serialized]
            compared += 1
        assert compared > 10, "the shipped read vectors are the corpus"

    def test_the_engine_answers_the_join_it_was_sent(self, tmp_path: Path) -> None:
        """A read carrying ``join`` is answered with it, even when it answers no row."""
        project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
        answer = handle_request(PILOT_REQUEST, project=project)
        assert answer["response"] == "read", answer
        assert answer["join"] == PILOT_REQUEST["join"]
        assert answer["derived"] == PILOT_REQUEST["derived"]
        assert answer["value"] == {"count": 0, "identities": []}

    @pytest.mark.parametrize("alias", sorted(RESERVED_JOIN_ALIASES))
    def test_every_reserved_alias_is_refused_by_the_schema_and_both_parsers(
        self, alias: str, request_validator: Draft202012Validator
    ) -> None:
        body = PILOT_REQUEST | {"join": [PILOT_REQUEST["join"][0] | {"as": alias}]}
        assert not request_validator.is_valid(body)
        assert not _pass_a_accepts(body)
        kernel = {key: value for key, value in body.items() if key != "operation"}
        with pytest.raises(query.QueryError, match="a key every row carries"):
            query.parse_request(kernel)


class TestTheCandidatesVocabularyIsTheSearchs:
    """``findCandidatesResponse``'s closed sets are the trace search's own, never a third copy left to drift."""

    def test_the_bands_are_the_searchs_and_the_schemas(self) -> None:
        from swcraft_tools.editing.protocol import FIND_CANDIDATE_BANDS
        from swcraft_tools.trace_search import BANDS

        schema = json.loads(
            (
                fixtures.REPO_ROOT
                / "packages"
                / "tools"
                / "tests"
                / "fixtures"
                / "contracts"
                / "editing-protocol"
                / "protocol.schema.json"
            ).read_text(encoding="utf-8")
        )
        rows = schema["$defs"]["tracePlace"]["properties"]["rows"]
        assert FIND_CANDIDATE_BANDS == BANDS
        assert tuple(rows["items"]["properties"]["band"]["enum"]) == BANDS

    def test_the_evidence_keys_are_the_records_the_search_writes(self) -> None:
        """Every evidence record the search writes for the small bench's requirements, by kind: the parser's keys."""
        from swcraft_tools.editing.protocol import _EVIDENCE_KEYS
        from swcraft_tools.model_state import load_model_state
        from swcraft_tools.project import require_project
        from swcraft_tools.trace_records import read_decisions, rejected_pairs
        from swcraft_tools.trace_search import TraceConfig, build_index, project_name_words, rank

        project = require_project(str(AGENT_TASK_BENCH_MANIFEST))
        state = load_model_state(project.resolved_model_root)
        index = build_index(state, TraceConfig(), rejected_pairs(read_decisions(project)), project_name_words(project))
        written: dict[str, set[frozenset[str]]] = {}
        for requirement in index.requirements.values():
            if requirement.stub:
                continue
            for place in rank(index, requirement, 10).places:
                for record in (*place.evidence, *(record for row in place.rows for record in row.evidence)):
                    written.setdefault(record["kind"], set()).add(frozenset(record))
        assert written == {kind: {keys} for kind, keys in _EVIDENCE_KEYS.items()}
