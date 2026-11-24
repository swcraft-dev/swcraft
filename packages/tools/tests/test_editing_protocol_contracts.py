"""Consistency gate over the editing-protocol contract artifacts.

The schemas and vectors under ``packages/tools/tests/fixtures/contracts/editing-protocol/`` are
the protocol's executable acceptance inputs, so they must not drift from the decisions they encode:

* ``protocol.schema.json`` and ``vector.schema.json`` are valid Draft 2020-12 schemas and every
  ``$ref``, cross-file ones included, resolves;
* the vector envelope is really conditional: a ``protocol-schema`` negative may carry an input the
  protocol schema rejects, the same input under a ``semantic`` negative is refused;
* every shipped vector is a valid envelope, every accepted ``input`` is a well-formed request,
  every ``protocol-schema`` negative's is rejected, and the negatives cover all four failure
  classes with each code's assigned class, so a mislabelled vector fails;
* the change-set examples keep the staged/committed contract: which baseline token comes back and
  which status carries an ``audit_ref``.

It imports nothing from MCP (``packages/tools/`` may never know about MCP); the half that needs the
live FastMCP registration is ``packages/mcp/tests/test_editing_protocol_decision_table.py``.

Not checked here but in the closed half: the error-code enum and class map against the decision
record's error-code block, its counts against the decision table, the published immutable fields
against its enumeration, and the documents' cited vectors.
:class:`TestErrorCodes` only compares the class map with ``failureClass`` and the ``allOf`` branches.
"""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any, get_args

import pytest
from conftest import REPO_ROOT
from jsonschema import Draft202012Validator
from pydantic import BaseModel
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from swcraft_tools import schema as model_schema

#: The vectors and the two schemas are test data beside the tests.
CONTRACT_FIXTURES_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "contracts" / "editing-protocol"
PROTOCOL_PATH = CONTRACT_FIXTURES_DIR / "protocol.schema.json"
VECTOR_PATH = CONTRACT_FIXTURES_DIR / "vector.schema.json"
EXAMPLES_DIR = CONTRACT_FIXTURES_DIR / "examples"
NEGATIVE_DIR = CONTRACT_FIXTURES_DIR / "negative"

#: Registry layer -> the ``schema.py`` annotation that layer's elements are declared by: the only
#: way this gate can tell a payload field name from a typo.
LAYER_MODELS: dict[str, Any] = {
    "swc": model_schema.SWComponent,
    "interfaces": model_schema.Interface,
    "connectors": model_schema.Connector,
    # `schema.py`'s `DataType` union carries every kind of the three data-type layers, so each layer
    # is its own part of that union: a field of one layer's kind at another layer's address is a typo.
    "application_types": model_schema.EnumDataType
    | model_schema.RecordDataType
    | model_schema.ArrayDataType
    | model_schema.ApplicationDataType,
    "implementation_types": model_schema.ValueDataType
    | model_schema.ReferenceDataType
    | model_schema.ExternalDataType
    | model_schema.ImplementationRecordDataType
    | model_schema.ImplementationArrayDataType
    | model_schema.PointerDataType,
    "base_types": model_schema.BaseDataType,
    "requirements": model_schema.Requirement,
    "compositions": model_schema.Composition,
    "tasks": model_schema.Task,
    "sequences": model_schema.Sequence,
    "hardware": model_schema.Hardware,
    "mapping_sets": model_schema.MappingSet,
    # Contract `8.0.0`'s elements: the three layers of the forms a type names by reference.
    "units": model_schema.Unit,
    "compu_methods": model_schema.CompuMethod,
    "data_constraints": model_schema.DataConstr,
}

#: Field names a payload may carry that ``schema.py`` does NOT declare, per layer. The one entry,
#: `swc.group`, is a placement field persisted as the element's directory and kept off
#: `SWComponent` so there is no second representation to disagree with the file system.
PLACEMENT_FIELDS: dict[str, frozenset[str]] = {"swc": frozenset({"group"})}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _model_classes(annotation: Any) -> list[type[BaseModel]]:
    """Every Pydantic class *annotation* can resolve to.

    A union contributes the fields of every member, because a payload is written before anyone knows
    which member it is. The generic-alias case is checked first: ``isinstance(list[int], type)`` is
    true, so a bare ``issubclass`` guard would raise.
    """
    arguments = get_args(annotation)
    if arguments:
        found: list[type[BaseModel]] = []
        for argument in arguments:
            found.extend(_model_classes(argument))
        return found
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return []


def _alias_names(classes: list[type[BaseModel]]) -> set[str]:
    """The serialization-alias field names any of *classes* declares (``Connector.from_`` is ``from``)."""
    return {
        field_info.alias or field_name
        for model_class in classes
        for field_name, field_info in model_class.model_fields.items()
    }


def _undeclared_fields(classes: list[type[BaseModel]], payload: Any, exempt: frozenset[str]) -> list[str]:
    """Field names in *payload* that none of *classes* declares, recursively (a typo is likelier in a port or event)."""
    if not isinstance(payload, dict):
        return []
    undeclared = sorted(set(payload) - _alias_names(classes) - exempt)
    if undeclared:
        return undeclared
    problems: list[str] = []
    for key, value in payload.items():
        members: list[type[BaseModel]] = []
        for model_class in classes:
            for field_name, field_info in model_class.model_fields.items():
                if (field_info.alias or field_name) == key:
                    members.extend(_model_classes(field_info.annotation))
        if not members:
            # A scalar, or a container of scalars (`SWComponent.memory`'s keys are resource
            # identities, not field names).
            continue
        if not isinstance(value, (dict, list)):
            # A declared collection answered as a count (a `summary` publishes each collection as
            # an integer under its own name): the name was checked above, no member to descend into.
            continue
        children = [value] if isinstance(value, dict) else [item for item in value or [] if isinstance(item, dict)]
        for child in children:
            problems.extend(f"{key}.{name}" for name in _undeclared_fields(members, child, frozenset()))
    return problems


def _violation_layer(path: Path) -> str:
    """A vector's ``expected.violation_layer``, or a sentinel if it has none.

    Read at collection time, where a ``KeyError`` would take the whole module down without naming the
    file; ``TestShippedVectors`` reports a malformed file properly.
    """
    return _load(path).get("expected", {}).get("violation_layer", "<no violation_layer>")


def _vector_id(path: Path) -> str:
    """The parametrize id of a vector file: its name, so a failure names the file."""
    return path.name


def _vector_files(directory: Path) -> list[Path]:
    """The vector files of *directory*, sorted, so parametrized ids are stable."""
    return sorted(directory.glob("*.json"), key=lambda path: path.as_posix())


EXAMPLE_FILES = _vector_files(EXAMPLES_DIR)
NEGATIVE_FILES = _vector_files(NEGATIVE_DIR)
ALL_VECTOR_FILES = EXAMPLE_FILES + NEGATIVE_FILES

#: Negatives split by the discriminator the envelope keys its conditional branches on, at collection
#: time so a mislabelled file fails under its own test id.
SEMANTIC_NEGATIVE_FILES = [p for p in NEGATIVE_FILES if _violation_layer(p) == "semantic"]
PROTOCOL_SCHEMA_NEGATIVE_FILES = [p for p in NEGATIVE_FILES if _violation_layer(p) == "protocol-schema"]

#: The examples whose request is a change set, held to the staged/committed rules.
CHANGE_SET_EXAMPLE_FILES = [p for p in EXAMPLE_FILES if _load(p).get("input", {}).get("operation") == "change_set"]

#: The examples whose request is a `discover` — the ones carrying an inventory.
DISCOVERY_EXAMPLE_FILES = [p for p in EXAMPLE_FILES if _load(p).get("input", {}).get("operation") == "discover"]


def _discovered_layers() -> list[dict[str, Any]]:
    """Every `layerDescriptor` any discovery example publishes."""
    return [layer for path in DISCOVERY_EXAMPLE_FILES for layer in _load(path)["expected"]["response"]["layers"]]


#: `input.operation` -> the `response` discriminator the answer must carry. The three change-set
#: operations never appear here: they are reachable only inside the `change_set` envelope.
REQUEST_TO_RESPONSE = {"discover": "discover", "read": "read", "find": "find", "change_set": "change_set"}


def _response_to(request: dict[str, Any]) -> str:
    """The response shape *request* is answered in: a ``read`` by its shape — one address, several, none, a count."""
    if request["operation"] == "read" and request.get("form") == "count":
        return "count"
    if request["operation"] == "read" and "addresses" in request:
        return "reads"
    if request["operation"] == "read" and "address" not in request:
        return "map"
    return REQUEST_TO_RESPONSE[request["operation"]]


def _iter_refs(node: Any) -> list[str]:
    """Every ``$ref`` value in *node*, at any depth."""
    refs: list[str] = []
    if isinstance(node, dict):
        for key, child in node.items():
            if key == "$ref" and isinstance(child, str):
                refs.append(child)
            else:
                refs.extend(_iter_refs(child))
    elif isinstance(node, list):
        for child in node:
            refs.extend(_iter_refs(child))
    return refs


def _find_keys(node: Any, wanted: str, where: str = "$") -> list[str]:
    """Every location in *node* carrying the key *wanted*, at any depth.

    A whole-document walk, because a removed key is as wrong in a scope entry, a delta entry or a nested
    address as in a request.
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, child in node.items():
            if key == wanted:
                found.append(f"{where}.{key}")
            found.extend(_find_keys(child, wanted, f"{where}.{key}"))
    elif isinstance(node, list):
        for position, child in enumerate(node):
            found.extend(_find_keys(child, wanted, f"{where}[{position}]"))
    return found


@pytest.fixture(scope="module")
def protocol_schema() -> dict[str, Any]:
    return _load(PROTOCOL_PATH)


@pytest.fixture(scope="module")
def vector_schema() -> dict[str, Any]:
    return _load(VECTOR_PATH)


@pytest.fixture(scope="module")
def registry(protocol_schema: dict[str, Any], vector_schema: dict[str, Any]) -> Registry:
    """The two local schemas under their declared ``$id``s; nothing is fetched over the network."""
    return Registry().with_resources(
        [(schema["$id"], Resource.from_contents(schema)) for schema in (protocol_schema, vector_schema)]
    )


@pytest.fixture(scope="module")
def vector_validator(vector_schema: dict[str, Any], registry: Registry) -> Draft202012Validator:
    return Draft202012Validator(vector_schema, registry=registry)


@pytest.fixture(scope="module")
def request_validator(protocol_schema: dict[str, Any], registry: Registry) -> Draft202012Validator:
    """A validator for the request shapes alone: the whole ``exchange`` union would accept a response."""
    return Draft202012Validator({"$ref": f"{protocol_schema['$id']}#/$defs/request"}, registry=registry)


class TestSchemaValidity:
    """Both documents are usable JSON Schema, and every reference resolves."""

    @pytest.mark.parametrize("path", [PROTOCOL_PATH, VECTOR_PATH], ids=lambda p: p.name)
    def test_is_a_valid_draft_2020_12_schema(self, path: Path) -> None:
        schema = _load(path)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        Draft202012Validator.check_schema(schema)

    @pytest.mark.parametrize("path", [PROTOCOL_PATH, VECTOR_PATH], ids=lambda p: p.name)
    def test_id_last_segment_is_the_file_name(self, path: Path) -> None:
        # A relative `protocol.schema.json#/...` resolves against the referring document's $id, so
        # the $id must keep agreeing with the on-disk layout.
        assert _load(path)["$id"].rsplit("/", 1)[-1] == path.name

    def test_every_reference_resolves(
        self, protocol_schema: dict[str, Any], vector_schema: dict[str, Any], registry: Registry
    ) -> None:
        for schema in (protocol_schema, vector_schema):
            resolver = registry.resolver(base_uri=schema["$id"])
            refs = _iter_refs(schema)
            # A document that lost its `$ref`s would pass the loop below vacuously.
            assert refs, f"{schema['$id']} carries no $ref at all; the resolution check would be vacuous"
            for ref in refs:
                try:
                    resolver.lookup(ref)
                except Unresolvable as error:  # pragma: no cover - the assertion is the report
                    pytest.fail(f"{schema['$id']}: '{ref}' does not resolve ({error})")

    def test_the_cross_file_reference_is_exercised(self, vector_schema: dict[str, Any]) -> None:
        # An all-local vector schema would resolve trivially and prove nothing about the shared $id scheme.
        cross_file = [ref for ref in _iter_refs(vector_schema) if ref.startswith("protocol.schema.json#")]
        assert cross_file, "vector.schema.json no longer references protocol.schema.json"


class TestErrorCodes:
    """The class map covers the failure classes and the schema's branches reproduce it."""

    def test_all_four_failure_classes_are_covered(self, protocol_schema: dict[str, Any]) -> None:
        # All four classes must leave the real model unchanged; a class no code maps to could never
        # be reached by a vector.
        assert set(protocol_schema["error_class_map"].values()) == set(protocol_schema["$defs"]["failureClass"]["enum"])

    @pytest.mark.parametrize("definition", ["errorDetail", "errorResult"])
    def test_the_class_branches_reproduce_the_map(self, protocol_schema: dict[str, Any], definition: str) -> None:
        """The ``allOf`` branches of *definition* say exactly what ``error_class_map`` says.

        Guards a schema that typed ``failure_class`` as a bare enum, so ``stale-baseline`` paired with
        ``invalid`` validated clean while every SWCraft parser refused it.
        """
        branches = protocol_schema["$defs"][definition]["allOf"]
        stated: dict[str, str] = {}
        for branch in branches:
            failure_class = branch["then"]["properties"]["failure_class"]["const"]
            for code in branch["if"]["properties"]["error"]["enum"]:
                assert code not in stated, f"{code} is given a class twice by $defs/{definition}"
                stated[code] = failure_class
            assert branch["if"]["required"] == ["error"], (
                "without `required`, the branch also fires on a document that omits `error` entirely"
            )
        assert stated == protocol_schema["error_class_map"], definition


class TestOperationNames:
    """Rename is not an operation of the schema.

    That the decision table's operation list equals the schema enum is the closed half's check.
    """

    def test_rename_is_not_an_operation(self, protocol_schema: dict[str, Any]) -> None:
        # Identity is fixed at `create`; a rename is `remove` + `create` + the reference updates in
        # one change set.
        assert "rename" not in protocol_schema["$defs"]["operationName"]["enum"]


class TestVectorEnvelopeIsConditional:
    """`violation_layer` really decides how strictly `input` is read.

    One input under both discriminators tells a working conditional from an `input: true` that
    constrains nothing. The instances are test-local; ``TestShippedVectors`` covers the shipped files.
    """

    #: A change set without a baseline, addressing a storage path as a string: both inexpressible.
    MALFORMED_INPUT = {"operation": "change_set", "address": "model/swc/Foo.yaml"}

    #: The positive control: without it an envelope rejecting every input would pass the refusals.
    WELL_FORMED_INPUT = {"operation": "read", "address": {"layer": "implementation_types", "element": "float32"}}

    def _negative(self, violation_layer: str) -> dict[str, Any]:
        # `deepcopy`: three tests mutate the vectors they are handed.
        return {
            "name": f"conditional-probe-{violation_layer}",
            "description": "Test-local probe of the envelope's conditional input contract.",
            "kind": "negative",
            "context": {"summary": "Any baseline; the probe is about the envelope, not the model."},
            "input": deepcopy(self.MALFORMED_INPUT),
            "expected": {
                "expected_error": "schema-violation",
                "failure_class": "invalid",
                "violation_layer": violation_layer,
            },
        }

    def _example(self) -> dict[str, Any]:
        """A valid `example` vector: a well-formed request and a success response."""
        return {
            "name": "conditional-probe-example",
            "description": "Test-local probe of the example branch of the envelope.",
            "kind": "example",
            "context": {"summary": "A model holding the addressed data type."},
            "input": deepcopy(self.WELL_FORMED_INPUT),
            "expected": {
                "response": {
                    "response": "read",
                    "address": deepcopy(self.WELL_FORMED_INPUT["address"]),
                    "form": "full",
                    "value": {"name": "float32", "kind": "base_type"},
                }
            },
        }

    def test_protocol_schema_negative_admits_arbitrary_input(self, vector_validator: Draft202012Validator) -> None:
        vector_validator.validate(self._negative("protocol-schema"))

    def test_semantic_negative_holds_input_to_the_request_shapes(self, vector_validator: Draft202012Validator) -> None:
        assert not vector_validator.is_valid(self._negative("semantic"))

    def test_semantic_negative_accepts_a_well_formed_request(self, vector_validator: Draft202012Validator) -> None:
        vector = self._negative("semantic")
        vector["input"] = self.WELL_FORMED_INPUT
        vector["expected"]["expected_error"] = "unknown-field"
        vector_validator.validate(vector)

    def test_an_example_carries_a_well_formed_request_and_a_response(
        self, vector_validator: Draft202012Validator
    ) -> None:
        vector_validator.validate(self._example())

    def test_an_example_may_not_carry_a_malformed_request(self, vector_validator: Draft202012Validator) -> None:
        # The valid example with ONLY `input` replaced, so the refusal can come from nothing else (a
        # negative relabelled `example` is refused by its expectation shape instead).
        vector = self._example()
        vector["input"] = deepcopy(self.MALFORMED_INPUT)
        assert not vector_validator.is_valid(vector), (
            "the example branch must hold `input` to protocol.schema.json's request shapes"
        )

    def test_an_example_may_not_expect_an_error_result(self, vector_validator: Draft202012Validator) -> None:
        # `expectedResponse.response` is narrowed to the SUCCESS shapes: `$defs/response` also admits
        # `errorResult`, which would let an example state a rejection.
        vector = self._example()
        vector["expected"]["response"] = {
            "response": "error",
            "error": "unknown-field",
            "failure_class": "invalid",
            "message": "swc has no field 'safety_level'",
        }
        assert not vector_validator.is_valid(vector), "an example may not carry an errorResult as its response"

    def test_the_admitted_input_really_is_rejected_by_the_protocol_schema(
        self, protocol_schema: dict[str, Any], registry: Registry
    ) -> None:
        # Otherwise the case above would prove only that the envelope is lax. A jsonschema
        # ValidationError has no stable mapping to an error code, so only rejection is asserted.
        request = Draft202012Validator(
            {"$ref": f"{protocol_schema['$id']}#/$defs/request"},
            registry=registry,
        )
        assert not request.is_valid(self.MALFORMED_INPUT)

    def test_a_protocol_schema_negative_must_expect_schema_violation(
        self, vector_validator: Draft202012Validator
    ) -> None:
        vector = self._negative("protocol-schema")
        vector["expected"]["expected_error"] = "unknown-field"
        vector["expected"]["failure_class"] = "invalid"
        assert not vector_validator.is_valid(vector)


class TestShippedVectors:
    """Every file under ``examples/`` and ``negative/`` is a usable envelope."""

    @pytest.mark.parametrize("directory", [EXAMPLES_DIR, NEGATIVE_DIR], ids=lambda p: p.name)
    def test_the_directory_ships_at_least_one_vector(self, directory: Path) -> None:
        # The parametrized cases are collected from these globs, so an emptied directory would make
        # them vacuous rather than red.
        assert directory.is_dir(), f"{directory} does not exist"
        assert _vector_files(directory), f"{directory} ships no vector"

    @pytest.mark.parametrize("path", ALL_VECTOR_FILES, ids=_vector_id)
    def test_validates_against_the_vector_envelope(self, path: Path, vector_validator: Draft202012Validator) -> None:
        vector_validator.validate(_load(path))

    @pytest.mark.parametrize("path", ALL_VECTOR_FILES, ids=_vector_id)
    def test_kind_matches_its_directory(self, path: Path) -> None:
        # The envelope's branches key on `kind`: a negative under examples/ would read as accepted.
        expected = "example" if path.parent == EXAMPLES_DIR else "negative"
        assert _load(path)["kind"] == expected

    def test_vector_names_are_unique_across_both_directories(self) -> None:
        # `name` is the stable handle a vector is cited by; the file may be renamed, the name may not.
        names = [_load(path)["name"] for path in ALL_VECTOR_FILES]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        assert not duplicates, f"vector names used more than once: {duplicates}"


class TestVectorInputsAreRequests:
    """The `violation_layer` label is checked against the protocol schema itself, per file."""

    @pytest.mark.parametrize("path", EXAMPLE_FILES, ids=_vector_id)
    def test_example_input_is_a_well_formed_request(self, path: Path, request_validator: Draft202012Validator) -> None:
        request_validator.validate(_load(path)["input"])

    @pytest.mark.parametrize("path", SEMANTIC_NEGATIVE_FILES, ids=_vector_id)
    def test_semantic_negative_input_is_a_well_formed_request(
        self, path: Path, request_validator: Draft202012Validator
    ) -> None:
        # A semantic violation is one only the engine can find, so the request must reach it.
        request_validator.validate(_load(path)["input"])

    @pytest.mark.parametrize("path", PROTOCOL_SCHEMA_NEGATIVE_FILES, ids=_vector_id)
    def test_protocol_schema_negative_input_is_rejected(
        self, path: Path, request_validator: Draft202012Validator
    ) -> None:
        # A jsonschema ValidationError has no stable mapping to an error code.
        assert not request_validator.is_valid(_load(path)["input"]), (
            f"{path.name} is filed as a `protocol-schema` negative, but protocol.schema.json accepts its "
            "input; an input the schema accepts is a `semantic` negative"
        )

    def test_both_violation_layers_are_exercised(self) -> None:
        # Either list being empty would make one of the cases above vacuous.
        assert SEMANTIC_NEGATIVE_FILES, "no `semantic` negative vectors"
        assert PROTOCOL_SCHEMA_NEGATIVE_FILES, "no `protocol-schema` negative vectors"


class TestNegativeFailureClasses:
    """A code and a class cannot be paired freely; the schema's map decides."""

    @pytest.mark.parametrize("path", NEGATIVE_FILES, ids=_vector_id)
    def test_failure_class_is_the_one_the_class_map_assigns(self, path: Path, protocol_schema: dict[str, Any]) -> None:
        expected = _load(path)["expected"]
        code = expected["expected_error"]
        assert protocol_schema["error_class_map"][code] == expected["failure_class"]

    def test_the_four_mandated_classes_are_all_covered(self, protocol_schema: dict[str, Any]) -> None:
        # Each class must leave the real model unchanged, so each needs a vector.
        covered = {_load(path)["expected"]["failure_class"] for path in NEGATIVE_FILES}
        assert covered == set(protocol_schema["$defs"]["failureClass"]["enum"])


class TestChangeSetResponseContract:
    """The staged/committed distinctions, pinned on the vectors that state them.

    A `committed` response returns the token the commit produced, a `staged` one the token the request
    carried; `audit_ref` is present on `committed` and omitted on `staged`, where it would read as a
    commit handle the protocol does not offer.
    """

    @pytest.mark.parametrize("path", CHANGE_SET_EXAMPLE_FILES, ids=_vector_id)
    def test_baseline_and_audit_ref_follow_the_status(self, path: Path) -> None:
        vector = _load(path)
        request = vector["input"]
        response = vector["expected"]["response"]
        status = response["status"]
        if status == "committed":
            assert response["baseline"] != request["baseline"], (
                "a committed response returns the token the commit produced, not the one the request carried"
            )
            assert response.get("audit_ref"), "a committed change set is identified by its audit_ref"
        else:
            assert response["baseline"] == request["baseline"], (
                f"a {status} response leaves the model byte-identical, so it returns the request's own token"
            )
            if status == "staged":
                assert "audit_ref" not in response, (
                    "audit_ref is omitted on `staged`: there is no commit request for the client to name it in"
                )

    def test_both_terminal_states_reached_by_a_request_are_exercised(self) -> None:
        # Without a vector of each, the branches above would never both run.
        statuses = {_load(path)["expected"]["response"]["status"] for path in CHANGE_SET_EXAMPLE_FILES}
        assert {"staged", "committed"} <= statuses, f"change-set examples cover only {sorted(statuses)}"


class TestDataTypeAuthoringGap:
    """The data-type authoring path, pinned by an example.

    ``server.py`` registers no `data_type` tool, so the decision table cannot show the path exists; a
    change-set `create` at an `implementation_types` address is the evidence and must keep validating.
    """

    @staticmethod
    def _data_type_create_examples() -> list[tuple[Path, dict[str, Any]]]:
        found: list[tuple[Path, dict[str, Any]]] = []
        for path in EXAMPLE_FILES:
            request = _load(path)["input"]
            if request.get("operation") != "change_set":
                continue
            for operation in request["operations"]:
                if operation["operation"] == "create" and operation["address"].get("layer") == "implementation_types":
                    found.append((path, request))
                    break
        return found

    def test_an_example_creates_a_data_type_through_the_generic_change_set(
        self, request_validator: Draft202012Validator
    ) -> None:
        found = self._data_type_create_examples()
        assert found, (
            "no example under examples/ carries a change-set `create` at an `implementation_types` address; "
            "that vector is what shows the protocol can author a data type"
        )
        for _path, request in found:
            request_validator.validate(request)


def _hashable(value: Any) -> Any:
    """*value* as something a set can hold, order-insensitively for objects.

    A member step's value may be an object (a `composite` or a `whole_value` identity), which a set
    cannot hold.
    """
    if isinstance(value, dict):
        return tuple(sorted((key, _hashable(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_hashable(item) for item in value)
    return value


def _address_key(address: dict[str, Any]) -> tuple[Any, ...]:
    """An address as a comparable tuple: layer, element, then the path steps."""
    steps = tuple(tuple(sorted((k, _hashable(v)) for k, v in step.items()))[0] for step in address.get("path", []))
    return (address["layer"], address.get("element"), steps)


def _multiset_collection_fields() -> frozenset[str]:
    """The field names discovery declares as ``whole_value`` **list** collections.

    Derived from the shipped descriptors, so a collection that changes its identity strategy takes
    this set with it. A ``whole_value`` map (``SWComponent.memory``) is excluded: its key is unique by
    construction, so it is not a multiset.
    """
    return frozenset(
        field["name"]
        for layer in _discovered_layers()
        for element_kind in layer["element_kinds"]
        for field in element_kind["fields"]
        if field.get("collection", {}).get("member_identity") == "whole_value"
        and field["collection"].get("kind") == "list"
    )


def _addresses_a_multiset_member(address: dict[str, Any]) -> bool:
    """True when *address* ends at a member of a ``whole_value`` list collection.

    Such a member is counted, not identified: a duplicate is no ``identity-conflict``, and a ``remove`` +
    ``create`` of one value is a multiset operation, not a same-identity replacement.
    """
    path = address.get("path") or []
    if len(path) < 2 or "member" not in path[-1]:
        return False
    return path[-2].get("field") in _multiset_collection_fields()


def _optional_identity_fields() -> dict[tuple[str, str], tuple[tuple[str, ...], frozenset[str]]]:
    """``(layer, collection field)`` -> its declared identity fields, and those a member may leave out.

    An identity field of a ``composite`` collection that its member kind does not declare required
    (``swc`` and ``instance`` of ``task.runnables`` and ``sequence.steps``) reads as ``null`` when left
    out, so a two-key and a three-key spelling name one member. Keyed by layer too: ``runnables`` is
    ``field``-identified in ``swc`` and ``composite`` in ``tasks``. A ``summary`` omits ``required`` at
    ``false``.
    """
    optional: dict[tuple[str, str], tuple[tuple[str, ...], frozenset[str]]] = {}
    for layer in _discovered_layers():
        kinds = {kind["kind"]: kind for kind in layer["element_kinds"]}
        for element_kind in layer["element_kinds"]:
            for field in element_kind["fields"]:
                collection = field.get("collection", {})
                if collection.get("member_identity") != "composite":
                    continue
                names = {
                    member_field["name"]
                    for member_kind in collection.get("member_kinds", [])
                    for member_field in kinds[member_kind]["fields"]
                    if member_field["name"] in collection["identity_fields"] and not member_field.get("required", False)
                }
                optional[(layer["layer"], field["name"])] = (tuple(collection["identity_fields"]), frozenset(names))
    return optional


def _canonical_path(layer: str, path: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*path* with every composite member step spelled with all its keys, the absent ones ``null``.

    The harness-side statement of ``editing/address.py``'s ``_walk_steps``, written from the published
    descriptors rather than imported, so a disagreement shows as a vector one side covers and the other
    does not. A step is completed only when every key it carries is a declared identity field and
    every one it leaves out is declared optional; any other step is compared as written.
    """
    optional = _optional_identity_fields()
    canonical: list[dict[str, Any]] = []
    for position, step in enumerate(path):
        member = step.get("member")
        owner = path[position - 1].get("field") if position else None
        declared, may_omit = (
            optional.get((layer, owner), ((), frozenset())) if isinstance(owner, str) else ((), frozenset())
        )
        if isinstance(member, dict) and declared:
            absent = set(declared) - set(member)
            if absent and set(member) <= set(declared) and absent <= may_omit:
                step = {"member": {name: member.get(name) for name in declared}}
        canonical.append(step)
    return canonical


def _scope_covers(scope_entry: dict[str, Any], operation: str, address: dict[str, Any]) -> bool:
    """The prefix-coverage rule, in code.

    The operation matches exactly (approving an update never authorizes a removal), the layers match,
    and the scope's address is a prefix of the target's in canonical spelling: omitting ``element``
    covers the layer, omitting ``path`` the element.
    """
    if scope_entry["operation"] != operation:
        return False
    scope_address = scope_entry["address"]
    if scope_address["layer"] != address["layer"]:
        return False
    if "element" in scope_address and scope_address["element"] != address.get("element"):
        return False
    scope_path = scope_address.get("path")
    if scope_path is None:
        return True
    # A scope written with two keys covers a change written with three, and the other way round.
    scope_path = _canonical_path(address["layer"], scope_path)
    target_path = _canonical_path(address["layer"], address.get("path", []))
    return len(scope_path) <= len(target_path) and all(a == b for a, b in zip(scope_path, target_path))


def _uncovered_operations(vector: dict[str, Any]) -> list[dict[str, Any]]:
    """The change-set operations no entry of the vector's approved scope covers."""
    scope = vector["context"]["authorization"]["approved_scope"]
    return [
        operation
        for operation in vector["input"]["operations"]
        if not any(_scope_covers(entry, operation["operation"], operation["address"]) for entry in scope)
    ]


def test_the_harness_coverage_completes_a_selector_and_never_resolves_one() -> None:
    """A two-key ``swc`` scope covers a three-key ``swc`` address, not an ``instance`` one.

    A step with an undeclared key is compared as written.
    """

    def at(member: dict[str, Any]) -> dict[str, Any]:
        return {"layer": "tasks", "element": "T", "path": [{"field": "runnables"}, {"member": member}]}

    scope = {"operation": "update", "address": at({"swc": "A", "runnable": "Run"})}
    assert _scope_covers(scope, "update", at({"swc": "A", "instance": None, "runnable": "Run"}))
    assert not _scope_covers(scope, "update", at({"swc": None, "instance": "A", "runnable": "Run"}))
    assert not _scope_covers(
        {"operation": "update", "address": at({"instance": "A", "runnable": "Run"})},
        "update",
        at({"swc": "A", "instance": None, "runnable": "Run"}),
    )
    assert not _scope_covers(
        {"operation": "update", "address": at({"swc": "A", "runnable": "Run", "core": "C0"})},
        "update",
        at({"swc": "A", "instance": None, "runnable": "Run", "core": "C0"}),
    )


#: Vectors whose change set reached the authorization gate: a `protocol-schema` negative is refused
#: at pass A first.
AUTHORIZED_CHANGE_SET_FILES = [
    path
    for path in EXAMPLE_FILES + NEGATIVE_FILES
    if _violation_layer(path) in ("semantic", "<no violation_layer>")
    and _load(path).get("input", {}).get("operation") == "change_set"
    and "authorization" in _load(path).get("context", {})
]


class TestErrorCodeCoverageByVector:
    """Every code in the closed enum is pinned by a vector, not only declared."""

    def test_every_declared_code_has_a_negative_vector(self, protocol_schema: dict[str, Any]) -> None:
        # The codes are API: adding one means adding its vectors in the same change.
        covered = {_load(path)["expected"]["expected_error"] for path in NEGATIVE_FILES}
        declared = set(protocol_schema["$defs"]["errorCode"]["enum"])
        assert covered == declared, (
            f"codes with no negative vector: {sorted(declared - covered)}; "
            f"vectors claiming an undeclared code: {sorted(covered - declared)}"
        )


class TestOperationCoverageByExample:
    """Each request shape is exercised by an example, and answered in kind."""

    def test_every_request_operation_has_an_example(self) -> None:
        # Includes the only vectors that encode the absent-layer semantics discovery turns on.
        operations = {_load(path)["input"]["operation"] for path in EXAMPLE_FILES}
        assert operations == set(REQUEST_TO_RESPONSE), (
            f"request shapes with no example: {sorted(set(REQUEST_TO_RESPONSE) - operations)}; "
            f"examples of an undeclared shape: {sorted(operations - set(REQUEST_TO_RESPONSE))}"
        )

    @pytest.mark.parametrize("path", EXAMPLE_FILES, ids=_vector_id)
    def test_the_response_answers_the_request_it_was_sent(self, path: Path) -> None:
        # The envelope validates request and response independently, so a `read` request could ship
        # beside a `discover` response.
        vector = _load(path)
        operation = vector["input"]["operation"]
        response = vector["expected"]["response"]["response"]
        expected = _response_to(vector["input"])
        assert response == expected, f"a `{operation}` request is answered by a `{expected}` response, not `{response}`"

    def test_discovery_pins_both_presence_states_of_the_hardware_layer(self) -> None:
        # No layer is optional, so `optional` is `false` everywhere, while `present` keeps its meaning:
        # a project can miss a mandatory layer (`check-model` reports `hardware.layer-absent`) and a
        # client must still see it and author its first element. One vector of each state.
        states: dict[bool, list[str]] = {}
        optional_layers: set[str] = set()
        for path in EXAMPLE_FILES:
            vector = _load(path)
            if vector["input"]["operation"] != "discover":
                continue
            for layer in vector["expected"]["response"]["layers"]:
                # Absent means `false`: a `summary` omits the key at its default.
                if layer.get("optional", False):
                    optional_layers.add(layer["layer"])
                if layer["layer"] != "hardware":
                    continue
                states.setdefault(layer["present"], []).append(path.name)
        assert not optional_layers, (
            f"discovery publishes {sorted(optional_layers)} as optional; contract `2.0.0` declares no optional "
            "layer, and a layer that became one again would need its own absent-state vector"
        )
        assert set(states) == {True, False}, (
            f"discovery examples cover `hardware` only as present={sorted(states)}; both states are needed, "
            "because the pair is exactly what an empty element list cannot express"
        )


class TestNegativePreconditionsAreMachineChecked:
    """A negative's precondition lives in `context`, and must actually hold."""

    def test_stale_is_claimed_by_exactly_the_stale_baseline_vector(self) -> None:
        # Otherwise the `expected_error` label alone says the model moved.
        stale = {path.name for path in ALL_VECTOR_FILES if _load(path)["context"].get("baseline_state") == "stale"}
        claiming = {
            path.name for path in NEGATIVE_FILES if _load(path)["expected"]["expected_error"] == "stale-baseline"
        }
        assert claiming, "no vector pins `stale-baseline`"
        assert stale == claiming, (
            f'vectors expecting `stale-baseline` without `baseline_state: "stale"`: {sorted(claiming - stale)}; '
            f"vectors stating a stale baseline and expecting something else: {sorted(stale - claiming)}"
        )

    @pytest.mark.parametrize("path", AUTHORIZED_CHANGE_SET_FILES, ids=_vector_id)
    def test_the_approved_scope_and_the_expected_verdict_agree(self, path: Path) -> None:
        # The `authorization` block must actually decide the verdict, not decorate it.
        vector = _load(path)
        uncovered = _uncovered_operations(vector)
        if vector["expected"].get("expected_error") == "unauthorized-delta":
            assert uncovered, (
                "this vector expects `unauthorized-delta`, but every operation it states is covered by its own "
                "approved scope under the exact-operation + address-prefix rule"
            )
        else:
            assert not uncovered, (
                "the approved scope does not cover "
                f"{[(op['operation'], op['address']) for op in uncovered]}, so this exchange would be refused as "
                "`unauthorized-delta` before ever reaching the outcome it states"
            )

    def test_the_unauthorized_vector_names_every_uncovered_address(self) -> None:
        # Every uncovered entry is reported, not the first (as `check_delta` does).
        checked = 0
        for path in NEGATIVE_FILES:
            vector = _load(path)
            if vector["expected"]["expected_error"] != "unauthorized-delta":
                continue
            checked += 1
            named = {_address_key(address) for address in vector["expected"]["addresses"]}
            uncovered = {_address_key(operation["address"]) for operation in _uncovered_operations(vector)}
            assert named == uncovered, f"{path.name}: reported {sorted(named)} but {sorted(uncovered)} are uncovered"
        assert checked, "no `unauthorized-delta` vector to check"


class TestContextIsAFixtureSpec:
    """`layers_present` and `existing_elements` are inputs, not decoration.

    A harness builds a baseline model from them, so a vector whose context contradicts its own
    expectation describes a model in which the expectation is wrong.
    """

    @pytest.mark.parametrize("path", ALL_VECTOR_FILES, ids=_vector_id)
    def test_existing_elements_live_in_a_present_layer(self, path: Path) -> None:
        context = _load(path)["context"]
        if "layers_present" not in context:
            pytest.skip("the vector does not state which layers exist")
        present = set(context["layers_present"])
        stated = (*context.get("existing_elements", []), *context.get("occupied_targets", []))
        missing = {element["layer"] for element in stated} - present
        assert not missing, f"elements are stated in layers the fixture does not have: {sorted(missing)}"

    @pytest.mark.parametrize("path", ALL_VECTOR_FILES, ids=_vector_id)
    def test_a_create_collides_exactly_when_the_identity_is_already_held(self, path: Path) -> None:
        vector = _load(path)
        if vector["input"].get("operation") != "change_set":
            pytest.skip("only a change set can carry a create")
        context = vector["context"]
        # Only an IDENTITY collides. An `occupied_targets` file name with a free identity is the
        # accepted case: the store gives the new file the nearest free collision suffix.
        held = {_address_key(element) for element in context.get("existing_elements", [])}
        occupied = {_address_key(element) for element in context.get("occupied_targets", [])}
        # A `whole_value` list is a multiset, so a `create` at a held value adds an occurrence; every
        # other collection, element and map key is uniquely identified and collides.
        colliding = [
            operation["address"]
            for operation in vector["input"]["operations"]
            if operation["operation"] == "create"
            and _address_key(operation["address"]) in held
            and not _addresses_a_multiset_member(operation["address"])
        ]
        expected = vector["expected"]
        reported = {expected.get("expected_error")} | {
            entry["expected_error"] for entry in expected.get("additional", [])
        }
        suffixed = [
            operation["address"]
            for operation in vector["input"]["operations"]
            if operation["operation"] == "create"
            and _address_key(operation["address"]) in occupied
            and _address_key(operation["address"]) not in held
        ]
        if colliding:
            assert "identity-conflict" in reported, (
                f"creates at already-held identities {colliding}, but the vector expects "
                f"{sorted(code for code in reported if code)}"
            )
        else:
            assert "identity-conflict" not in reported, (
                "expects `identity-conflict`, but `existing_elements` holds none of its create addresses"
                + (
                    f" — {suffixed} are only `occupied_targets`, whose create takes a collision suffix"
                    if suffixed
                    else ""
                )
            )


class TestAdditionalViolations:
    """`errorResult.additional` is a contract, and one vector exercises it."""

    def test_a_negative_reports_more_than_one_violation(self) -> None:
        # Otherwise nothing states that one round trip reports every violation a check-model run would.
        carriers = [path.name for path in NEGATIVE_FILES if _load(path)["expected"].get("additional")]
        assert carriers, (
            "no negative vector carries `expected.additional`, so the multi-violation half of the error "
            "result — and `$defs/errorDetail` with it — is pinned by nothing"
        )

    @pytest.mark.parametrize("path", NEGATIVE_FILES, ids=_vector_id)
    def test_additional_never_repeats_the_primary(self, path: Path, protocol_schema: dict[str, Any]) -> None:
        expected = _load(path)["expected"]
        primary = (expected["expected_error"], tuple(_address_key(a) for a in expected.get("addresses", [])))
        for entry in expected.get("additional", []):
            secondary = (entry["expected_error"], tuple(_address_key(a) for a in entry.get("addresses", [])))
            assert secondary != primary, "`additional` restates the primary violation"
            assert protocol_schema["error_class_map"][entry["expected_error"]] == entry["failure_class"], (
                "a secondary violation's failure class is the one the class map assigns it, exactly as the primary's is"
            )


class TestImmutableFieldsAgreeAcrossTheContractSet:
    """The discovery vectors publish an immutable field for each of the four immutability grounds below."""

    @staticmethod
    def _published_immutables() -> set[tuple[str, str, str]]:
        """``(layer, kind, field)`` for every field the discovery vectors flag."""
        found: set[tuple[str, str, str]] = set()
        for path in EXAMPLE_FILES:
            vector = _load(path)
            if vector["input"]["operation"] != "discover":
                continue
            for layer in vector["expected"]["response"]["layers"]:
                for element_kind in layer["element_kinds"]:
                    for field in element_kind["fields"]:
                        # Absent means `false`: a `summary` omits the key at its default.
                        if field.get("immutable", False):
                            found.add((layer["layer"], element_kind["kind"], field["name"]))
        return found

    def test_discovery_publishes_immutable_fields_at_all(self) -> None:
        assert self._published_immutables(), "no discovery vector publishes an immutable field"

    def test_each_immutability_ground_is_actually_exercised(self) -> None:
        # Each ground must be carried by a vector or its check is vacuous: the placement field
        # (`swc.group`, the only plain-field ground), a kind discriminator, a `whole_value` member
        # field and a composite member identity.
        published = self._published_immutables()
        assert ("swc", "swc", "group") in published, "the placement field is no longer published immutable"
        assert ("hardware", "memory", "kind") in published, "no kind discriminator is published immutable"
        assert ("swc", "timing_event", "period") in published, (
            "no `whole_value` member field is published immutable; the member-has-no-interior rule is unexercised"
        )
        assert ("tasks", "task_runnable_ref", "swc") in published, (
            "no composite member identity is published immutable; the composite branch is unexercised"
        )


class TestVectorPayloadsUseDeclaredSchemaFields:
    """A payload field name is checked against `schema.py`, not only spelled.

    ``protocol.schema.json`` leaves `value` unconstrained (the registry validates the staged result),
    so only `schema.py` can tell `min_start_interval` from `min_start_intervall`. Only ``examples/``
    are checked: a negative may carry an undeclared field deliberately.
    """

    @staticmethod
    def _element_payloads(vector: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """``(layer, payload)`` for every whole-element object the vector states."""
        found: list[tuple[str, dict[str, Any]]] = []

        def add(address: dict[str, Any], value: Any) -> None:
            # An address with a `path` names a field, member or map entry, whose value is not a model.
            if address.get("element") and not address.get("path") and isinstance(value, dict):
                found.append((address["layer"], value))

        request = vector["input"]
        if request["operation"] == "change_set":
            for operation in request["operations"]:
                if operation["operation"] in ("create", "update"):
                    add(operation["address"], operation.get("value"))
        response = vector["expected"]["response"]
        if response["response"] == "read":
            value = response["value"]
            if response.get("members") is not None:
                # A member slice answers rows, not elements; their members are checked by
                # `_member_slice_payloads`.
                pass
            elif isinstance(value, list) and not response["address"].get("element"):
                for member in value:
                    if isinstance(member, dict):
                        found.append((response["address"]["layer"], member))
            else:
                add(response["address"], value)
        elif response["response"] == "change_set":
            for entry in response["delta"]:
                add(entry["address"], entry.get("after"))
                add(entry["address"], entry.get("before"))
        return found

    @staticmethod
    def _member_slice_payloads(vector: dict[str, Any]) -> list[tuple[list[type[BaseModel]], dict[str, Any]]]:
        """``(member classes, payload)`` for every row of a member-slice read.

        The classes are those the sliced collection's annotation resolves to (a port for ``swc.ports``);
        a dotted path is followed step by step through the classes each step resolved to.
        """
        response = vector["expected"]["response"]
        if response.get("response") != "read" or response.get("members") is None:
            return []
        classes = _model_classes(LAYER_MODELS[response["address"]["layer"]])
        for step in response["members"].split("."):
            classes = [
                member
                for model_class in classes
                for field_name, field_info in model_class.model_fields.items()
                if (field_info.alias or field_name) == step
                for member in _model_classes(field_info.annotation)
            ]
        rows = response["value"] if isinstance(response["value"], list) else []
        return [
            (classes, row["member"]) for row in rows if isinstance(row, dict) and isinstance(row.get("member"), dict)
        ]

    @pytest.mark.parametrize("path", EXAMPLE_FILES, ids=_vector_id)
    def test_every_member_slice_payload_field_is_declared_by_schema_py(self, path: Path) -> None:
        for classes, payload in self._member_slice_payloads(_load(path)):
            assert classes, "the sliced collection resolves to no model class, so nothing was checked"
            undeclared = _undeclared_fields(classes, payload, frozenset())
            assert not undeclared, f"a sliced member carries fields schema.py does not declare: {undeclared}"

    def test_the_member_slice_check_sees_something(self) -> None:
        # The parametrized case above passes vacuously while no example ships a member slice.
        total = sum(len(self._member_slice_payloads(_load(path))) for path in EXAMPLE_FILES)
        assert total >= 2, f"only {total} member-slice rows found across the examples"

    @pytest.mark.parametrize("path", EXAMPLE_FILES, ids=_vector_id)
    def test_every_payload_field_is_declared_by_schema_py(self, path: Path) -> None:
        vector = _load(path)
        for layer, payload in self._element_payloads(vector):
            assert layer in LAYER_MODELS, f"unknown layer {layer!r}"
            undeclared = _undeclared_fields(
                _model_classes(LAYER_MODELS[layer]), payload, PLACEMENT_FIELDS.get(layer, frozenset())
            )
            assert not undeclared, f"payload at layer {layer!r} carries fields schema.py does not declare: {undeclared}"

    def test_the_payload_check_sees_something(self) -> None:
        # An empty payload list everywhere would make the parametrized case compare nothing.
        total = sum(len(self._element_payloads(_load(path))) for path in EXAMPLE_FILES)
        assert total >= 5, f"only {total} whole-element payloads found across the examples"

    def test_the_one_undeclared_field_the_vectors_may_carry_is_the_placement_field(self) -> None:
        # Pinning the one exemption keeps it from becoming an "unknown fields are fine" allowance.
        assert set(PLACEMENT_FIELDS) == {"swc"} and PLACEMENT_FIELDS["swc"] == frozenset({"group"})
        assert "group" not in {
            field_info.alias or name for name, field_info in model_schema.SWComponent.model_fields.items()
        }, "`group` is now a declared SWComponent field; the placement-field design has changed"
        assert any(
            "group" in payload
            for path in EXAMPLE_FILES
            for layer, payload in self._element_payloads(_load(path))
            if layer == "swc"
        ), "no example carries `swc.group`, so the exemption is unused and should be removed"


class TestDiscoveryDescriptorsAreSelfConsistent:
    """The inventory is readable on its own: every link inside one response resolves.

    A collection names member kinds, a member kind names where it appears, an object-typed field names
    the kind describing it; a client must never correlate them by reading ``schema.py``.
    """

    @pytest.mark.parametrize("path", DISCOVERY_EXAMPLE_FILES, ids=_vector_id)
    def test_every_collection_member_kind_is_a_declared_member_only_kind(self, path: Path) -> None:
        # A member-only kind has no file and no layer-level address; without `member_only` a client
        # cannot tell that a top-level `create` at one is a `broken-reference`.
        for layer in _load(path)["expected"]["response"]["layers"]:
            by_kind = {element_kind["kind"]: element_kind for element_kind in layer["element_kinds"]}
            for element_kind in layer["element_kinds"]:
                for field in element_kind["fields"]:
                    for member_kind in field.get("collection", {}).get("member_kinds", []):
                        assert member_kind in by_kind, (
                            f"{layer['layer']}.{element_kind['kind']}.{field['name']} collects `{member_kind}`, "
                            f"which the layer does not declare as an element kind"
                        )
                        assert by_kind[member_kind].get("member_only") is True, (
                            f"`{member_kind}` is only ever a collection member, so its descriptor must say "
                            "`member_only: true`"
                        )

    @pytest.mark.parametrize("path", DISCOVERY_EXAMPLE_FILES, ids=_vector_id)
    def test_every_collection_publishes_a_usable_member_identity(self, path: Path) -> None:
        # With no ordinal step form, a client must know a collection's identity strategy to build a
        # member address; each strategy carries exactly the extra key it needs. A layer may hold no
        # collection, so the vacuity guard is the aggregate case below.
        for layer in _load(path)["expected"]["response"]["layers"]:
            for element_kind in layer["element_kinds"]:
                for field in element_kind["fields"]:
                    collection = field.get("collection")
                    if collection is None:
                        continue
                    where = f"{layer['layer']}.{element_kind['kind']}.{field['name']}"
                    strategy = collection.get("member_identity")
                    assert strategy in {"field", "composite", "whole_value"}, (
                        f"{where} publishes no usable member identity strategy ({strategy!r}); a client cannot "
                        "build a member address without one"
                    )
                    if strategy == "field":
                        assert collection.get("identity_field"), f"{where} is `field`-identified but names no field"
                    elif strategy == "composite":
                        assert len(collection.get("identity_fields", [])) >= 2, (
                            f"{where} is `composite`-identified but names fewer than two fields"
                        )
                    else:
                        assert "identity_field" not in collection and "identity_fields" not in collection, (
                            f"{where} is `whole_value`-identified: the member IS the identity, so naming a field "
                            "would be a second, contradictory answer"
                        )
                    if len(collection.get("member_kinds", [])) > 1:
                        assert collection.get("discriminator_field"), (
                            f"{where} collects a union of member kinds but names no discriminator, so a client "
                            "cannot tell which kind a member is"
                        )

    def test_all_three_identity_strategies_are_published_somewhere(self) -> None:
        # Otherwise an inventory using only the easy strategy satisfies the branches above.
        strategies = {
            field["collection"]["member_identity"]
            for layer in _discovered_layers()
            for element_kind in layer["element_kinds"]
            for field in element_kind["fields"]
            if "collection" in field
        }
        assert strategies == {"field", "composite", "whole_value"}, (
            f"the discovery vectors publish only {sorted(strategies)}; all three member-identity forms "
            "need an acceptance input"
        )

    @pytest.mark.parametrize("path", DISCOVERY_EXAMPLE_FILES, ids=_vector_id)
    def test_a_member_only_kind_states_where_it_appears(self, path: Path) -> None:
        # Both directions: the owning kind declares the field, and the field really reaches this kind
        # (as a collection member or as the object an `element_kind` link names).
        for layer in _load(path)["expected"]["response"]["layers"]:
            by_kind = {element_kind["kind"]: element_kind for element_kind in layer["element_kinds"]}
            for element_kind in layer["element_kinds"]:
                if not element_kind.get("member_only"):
                    assert "member_of" not in element_kind, (
                        f"`{element_kind['kind']}` is top-level addressable, so `member_of` would be misleading"
                    )
                    continue
                assert element_kind.get("member_of"), (
                    f"`{element_kind['kind']}` is `member_only` but never says which field reaches it"
                )
                for owner in element_kind["member_of"]:
                    assert owner["kind"] in by_kind, (
                        f"`{element_kind['kind']}` claims to be a member of `{owner['kind']}`, which "
                        f"{layer['layer']} does not declare"
                    )
                    fields = {field["name"]: field for field in by_kind[owner["kind"]]["fields"]}
                    assert owner["field"] in fields, (
                        f"`{owner['kind']}` declares no field `{owner['field']}` for `{element_kind['kind']}`"
                    )
                    field = fields[owner["field"]]
                    reached = (
                        element_kind["kind"] in field.get("collection", {}).get("member_kinds", [])
                        or field.get("element_kind") == element_kind["kind"]
                    )
                    assert reached, (
                        f"`{owner['kind']}.{owner['field']}` does not name `{element_kind['kind']}` as a member "
                        "kind or as its object's element kind, so the two halves of the link disagree"
                    )

    @pytest.mark.parametrize("path", DISCOVERY_EXAMPLE_FILES, ids=_vector_id)
    def test_an_object_typed_field_links_to_the_kind_describing_it(self, path: Path) -> None:
        # `Connector.from`/`to` are the case: nothing in the descriptor says what is inside until
        # `element_kind` says it.
        for layer in _load(path)["expected"]["response"]["layers"]:
            by_kind = {element_kind["kind"]: element_kind for element_kind in layer["element_kinds"]}
            for element_kind in layer["element_kinds"]:
                for field in element_kind["fields"]:
                    linked = field.get("element_kind")
                    if linked is None:
                        continue
                    assert "collection" not in field, (
                        f"{element_kind['kind']}.{field['name']} is a collection; its member kinds belong in "
                        "`collection.member_kinds`, not in `element_kind`"
                    )
                    assert linked in by_kind and by_kind[linked].get("member_only") is True, (
                        f"{element_kind['kind']}.{field['name']} links to `{linked}`, which the layer does not "
                        "declare as a member-only element kind"
                    )

    def test_an_object_typed_field_link_is_exercised(self) -> None:
        assert any(
            "element_kind" in field
            for layer in _discovered_layers()
            for element_kind in layer["element_kinds"]
            for field in element_kind["fields"]
        ), "no discovery vector publishes an object-typed field link; the B-7 third case has no acceptance input"


class TestDiscoveryIsFullFidelity:
    """A discovery response publishes the whole inventory, never an abridgement.

    What a layer publishes for its top-level kinds is exactly what ``schema.py`` declares, plus the one
    placement field.
    """

    @pytest.mark.parametrize("path", DISCOVERY_EXAMPLE_FILES, ids=_vector_id)
    def test_top_level_fields_equal_what_schema_py_declares(self, path: Path) -> None:
        for layer in _load(path)["expected"]["response"]["layers"]:
            name = layer["layer"]
            assert name in LAYER_MODELS, f"unknown layer {name!r}"
            published = {
                field["name"]
                for element_kind in layer["element_kinds"]
                if not element_kind.get("member_only")
                for field in element_kind["fields"]
            }
            declared = _alias_names(_model_classes(LAYER_MODELS[name])) | PLACEMENT_FIELDS.get(name, frozenset())
            assert published == declared, (
                f"{path.name}: layer `{name}` publishes {sorted(published - declared)} that schema.py does not "
                f"declare and omits {sorted(declared - published)} that it does"
            )

    def test_every_layer_publishes_at_least_one_element_kind_of_its_own(self) -> None:
        # A layer holding only `member_only` kinds would compare two empty sets and pass.
        for layer in _discovered_layers():
            top_level = [kind for kind in layer["element_kinds"] if not kind.get("member_only")]
            assert top_level, f"layer `{layer['layer']}` publishes no top-level element kind"


class TestNoPositionalAddressing:
    """`pathStep` has three forms, and none of them is an ordinal.

    An approved `{index: 0}` denotes a different item once anything is inserted before it; these cases
    stop the form coming back through a vector.
    """

    def test_the_schema_declares_exactly_three_step_forms(self, protocol_schema: dict[str, Any]) -> None:
        forms = {tuple(branch["required"])[0] for branch in protocol_schema["$defs"]["pathStep"]["oneOf"]}
        assert forms == {"field", "member", "key"}, f"pathStep declares {sorted(forms)}"

    def test_the_member_step_admits_a_composite_or_whole_value_identity(
        self, protocol_schema: dict[str, Any], registry: Registry
    ) -> None:
        # `Task.runnables` and `Sequence.steps` are identified by a composite and `Runnable.events`
        # by its whole value, so a string-only `member` would leave them unaddressable.
        step = Draft202012Validator({"$ref": f"{protocol_schema['$id']}#/$defs/pathStep"}, registry=registry)
        step.validate({"member": "pOut"})
        step.validate({"member": {"swc": "ExampleSWC", "runnable": "ExampleRun"}})
        step.validate({"member": {"type": "timing", "period": 0.01}})
        assert not step.is_valid({"index": 0}), "an ordinal step is still accepted by $defs/pathStep"

    @pytest.mark.parametrize("path", ALL_VECTOR_FILES, ids=_vector_id)
    def test_no_vector_addresses_anything_positionally(self, path: Path) -> None:
        offenders = _find_keys(_load(path), "index")
        assert not offenders, f"{path.name} still uses ordinal addressing at {offenders}"


class TestWholeValueMembersAreCarriedWholeAndCounted:
    """`whole_value` identity: the step carries any JSON, and the collection is counted, not keyed.

    The registry, not the protocol schema, decides which values a collection admits. A duplicate in
    `Runnable.events` or a `satisfies` list is never an identity collision; where a validator rule
    refuses one (`trace.duplicate-link`), the refusal is a finding, not an identity check.
    """

    #: The identities a member step must admit: a string, an object, and any other JSON value.
    IDENTITIES: list[Any] = ["ExampleSWC", {"type": "timing", "period": 0.01}, 3, 0.5, True, ["a", "b"], None]

    @staticmethod
    def _step(protocol_schema: dict[str, Any], registry: Registry) -> Draft202012Validator:
        return Draft202012Validator({"$ref": f"{protocol_schema['$id']}#/$defs/pathStep"}, registry=registry)

    @pytest.mark.parametrize("identity", IDENTITIES, ids=lambda value: type(value).__name__)
    def test_a_member_step_admits_any_json_identity(
        self, identity: Any, protocol_schema: dict[str, Any], registry: Registry
    ) -> None:
        step = self._step(protocol_schema, registry)
        assert step.is_valid({"member": identity}), (
            f"a `whole_value` identity of type {type(identity).__name__} is rejected by $defs/pathStep; the "
            "protocol schema must not decide which values a collection admits — that is a registry fact "
            "checked against schema.py, exactly as an operation's `value` payload is"
        )

    def test_a_member_step_still_admits_nothing_else(self, protocol_schema: dict[str, Any], registry: Registry) -> None:
        # Widening the identity may not widen the STEP: one form per step, no ordinal one.
        step = self._step(protocol_schema, registry)
        for malformed in ({"index": 0}, {"member": "pOut", "field": "events"}, {}):
            assert not step.is_valid(malformed), f"$defs/pathStep accepts {malformed!r}"

    def test_a_shipped_example_exercises_a_legal_duplicate(self) -> None:
        multiset_fields = _multiset_collection_fields()
        assert multiset_fields, (
            "discovery publishes no `whole_value` list collection, so the multiset rule has nothing to apply "
            "to and the cases above are vacuous"
        )
        # The two halves of the counting rule need separate carriers: a repeated `remove` says
        # nothing about a `create` at an address the fixture already holds.
        recreating: list[str] = []
        repeating: list[str] = []
        for path in CHANGE_SET_EXAMPLE_FILES:
            vector = _load(path)
            held = {_address_key(element) for element in vector["context"].get("existing_elements", [])}
            multiset_operations = [
                operation
                for operation in vector["input"]["operations"]
                if _addresses_a_multiset_member(operation["address"])
            ]
            occurrences = Counter(_address_key(operation["address"]) for operation in multiset_operations)
            recreated = [
                operation
                for operation in multiset_operations
                if operation["operation"] == "create" and _address_key(operation["address"]) in held
            ]
            if recreated:
                recreating.append(path.name)
            if any(count > 1 for count in occurrences.values()):
                repeating.append(path.name)
        assert recreating, (
            "no example `create`s a value a `whole_value` collection already holds, so 'a `create` adds one "
            "occurrence' has no acceptance input. A repeated `remove` does not stand in for it: only a "
            "`create` at an address named in `existing_elements` shows that the duplicate is not an "
            "`identity-conflict`, and 19.2 would otherwise have to guess that half of the counting rule"
        )
        assert repeating, (
            "no example addresses one member of a `whole_value` list more than once, so 'a `remove` removes "
            "one occurrence' has no acceptance input and 19.2 would have to guess the counting rule"
        )


class TestPassportCarriesNoStorageRoots:
    """The passport publishes identity and versions; storage stays inside.

    `model_root`, `dist_root` and `views_root` are consumed by no operation, and a storage path must
    never cross the protocol boundary.
    """

    ROOTS = ("model_root", "dist_root", "views_root")

    def test_the_schema_declares_no_root(self, protocol_schema: dict[str, Any]) -> None:
        passport = protocol_schema["$defs"]["projectPassport"]
        assert passport["additionalProperties"] is False, "a passport that admits extra keys admits a root"
        declared = set(passport["properties"])
        assert not declared.intersection(self.ROOTS), (
            f"the passport still declares {sorted(declared & set(self.ROOTS))}"
        )
        assert "swcraft_version" in declared, (
            "`swcraft_version` is what makes the refusal of a protocol_version negotiation "
            "operable; dropping the roots must not take it with them"
        )

    @pytest.mark.parametrize("path", DISCOVERY_EXAMPLE_FILES, ids=_vector_id)
    def test_no_discovery_vector_publishes_a_root(self, path: Path) -> None:
        passport = _load(path)["expected"]["response"]["project"]
        assert not set(passport).intersection(self.ROOTS), (
            f"{path.name} publishes {sorted(set(passport) & set(self.ROOTS))}"
        )
        assert passport["swcraft_version"], "the passport must still carry the lockstep version"


class TestSameIdentityRemoveAndCreateIsRefused:
    """A `remove` and a `create` at the same identity is refused, globally.

    The differ represents neither a relocation nor an in-place change of an immutable field as a
    delta, so the pair is refused as `identity-conflict` for every uniquely identified element and
    member (the `swc.group` move, the `kind` discriminators, renaming in place). Members of a
    `whole_value` list are outside the ban: they are counted, not identified.
    """

    @staticmethod
    def _cancelling_addresses(vector: dict[str, Any]) -> list[tuple[Any, ...]]:
        request = vector["input"]
        if not isinstance(request, dict) or request.get("operation") != "change_set":
            return []
        by_operation: dict[str, set[tuple[Any, ...]]] = {"create": set(), "remove": set()}
        for operation in request["operations"]:
            # A multiset member: one occurrence out and one in, which the ban does not reach.
            if operation["operation"] in by_operation and not _addresses_a_multiset_member(operation["address"]):
                by_operation[operation["operation"]].add(_address_key(operation["address"]))
        return sorted(by_operation["create"] & by_operation["remove"])

    @pytest.mark.parametrize("path", ALL_VECTOR_FILES, ids=_vector_id)
    def test_a_cancelling_pair_is_expected_to_be_refused(self, path: Path) -> None:
        vector = _load(path)
        cancelling = self._cancelling_addresses(vector)
        if not cancelling:
            return
        assert vector["kind"] == "negative", (
            f"{path.name} is an example, but it removes and creates the same identity at {cancelling}, which "
            "the protocol refuses"
        )
        reported = {vector["expected"]["expected_error"]} | {
            entry["expected_error"] for entry in vector["expected"].get("additional", [])
        }
        assert "identity-conflict" in reported, (
            f"{path.name} removes and creates the same identity at {cancelling} but expects {sorted(reported)}"
        )

    def test_the_banned_shape_ships_as_a_negative(self) -> None:
        carriers = [path.name for path in NEGATIVE_FILES if self._cancelling_addresses(_load(path))]
        assert carriers, (
            "no negative vector states the same-identity remove + create; the shape the protocol bans "
            "globally has no acceptance input, and the engine would have to invent the refusal"
        )

    def test_the_rename_case_stays_legal(self) -> None:
        # A remove and a create at DIFFERENT identities is a rename and is accepted, so the rule is
        # stated over the identity, not the operation pair.
        renames = [
            path.name
            for path in EXAMPLE_FILES
            if _load(path)["input"].get("operation") == "change_set"
            and {op["operation"] for op in _load(path)["input"]["operations"]} >= {"create", "remove"}
        ]
        assert renames, "no example pairs a remove with a create, so the legal half of the rule is unexercised"


#: A shipped `full` and a shipped `summary` discovery, the seeds the schema checks below mutate (a
#: negative vector is a request, so none can carry a malformed response).
FULL_DISCOVERY_SEED = EXAMPLES_DIR / "discover-layer-interfaces-full.json"
SUMMARY_DISCOVERY_SEED = EXAMPLES_DIR / "discover-layer-hardware.json"

#: The first field of the first kind of the first layer — the one the checks mutate.
FIRST_FIELD_PATH = ("layers", 0, "element_kinds", 0, "fields", 0)


class TestDescriptorBooleansFollowTheDiscoveryForm:
    """The three descriptor booleans are required in `full`, and `true` where a `summary` has one.

    The descriptors do not require ``required``/``immutable``/``optional``; ``$defs/discoverResponse``'s
    ``allOf`` requires them under ``form: full`` and makes each ``const: true`` where present under
    ``form: summary``, so a summary cannot publish a default it omits by rule.
    """

    @staticmethod
    def _validator(protocol_schema: dict[str, Any], registry: Registry) -> Draft202012Validator:
        return Draft202012Validator({"$ref": f"{protocol_schema['$id']}#/$defs/discoverResponse"}, registry=registry)

    @staticmethod
    def _response(path: Path) -> dict[str, Any]:
        response = deepcopy(_load(path)["expected"]["response"])
        assert response["response"] == "discover", path.name
        return response

    @staticmethod
    def _refusals_at(errors: list[Any], where: tuple[Any, ...]) -> list[Any]:
        return [error for error in errors if tuple(error.absolute_path) == where and error.validator == "required"]

    @pytest.mark.parametrize("key", ["required", "immutable"])
    def test_a_full_response_missing_a_field_boolean_fails_at_that_field(
        self, key: str, protocol_schema: dict[str, Any], registry: Registry
    ) -> None:
        response = self._response(FULL_DISCOVERY_SEED)
        assert response["form"] == "full"
        del response["layers"][0]["element_kinds"][0]["fields"][0][key]
        errors = list(self._validator(protocol_schema, registry).iter_errors(response))
        at_field = self._refusals_at(errors, FIRST_FIELD_PATH)
        assert at_field, [(list(error.absolute_path), error.message) for error in errors]
        # The refusal comes from the `full` overlay, not the descriptor's own `required` list.
        assert all("allOf" in error.absolute_schema_path for error in at_field), [
            list(error.absolute_schema_path) for error in at_field
        ]

    def test_a_full_response_missing_a_layer_optional_fails_at_that_layer(
        self, protocol_schema: dict[str, Any], registry: Registry
    ) -> None:
        response = self._response(FULL_DISCOVERY_SEED)
        del response["layers"][0]["optional"]
        errors = list(self._validator(protocol_schema, registry).iter_errors(response))
        at_layer = self._refusals_at(errors, ("layers", 0))
        assert at_layer, [(list(error.absolute_path), error.message) for error in errors]
        assert all("allOf" in error.absolute_schema_path for error in at_layer), [
            list(error.absolute_schema_path) for error in at_layer
        ]

    @pytest.mark.parametrize("key", ["required", "immutable"])
    def test_a_summary_carrying_a_false_field_boolean_fails_at_that_field(
        self, key: str, protocol_schema: dict[str, Any], registry: Registry
    ) -> None:
        response = self._response(SUMMARY_DISCOVERY_SEED)
        assert response["form"] == "summary"
        response["layers"][0]["element_kinds"][0]["fields"][0][key] = False
        errors = list(self._validator(protocol_schema, registry).iter_errors(response))
        at_key = [
            error
            for error in errors
            if tuple(error.absolute_path) == (*FIRST_FIELD_PATH, key) and error.validator == "const"
        ]
        assert at_key, [(list(error.absolute_path), error.message) for error in errors]
        assert all("allOf" in error.absolute_schema_path for error in at_key), [
            list(error.absolute_schema_path) for error in at_key
        ]

    def test_a_summary_carrying_optional_false_fails_at_that_layer(
        self, protocol_schema: dict[str, Any], registry: Registry
    ) -> None:
        response = self._response(SUMMARY_DISCOVERY_SEED)
        response["layers"][0]["optional"] = False
        errors = list(self._validator(protocol_schema, registry).iter_errors(response))
        at_key = [
            error
            for error in errors
            if tuple(error.absolute_path) == ("layers", 0, "optional") and error.validator == "const"
        ]
        assert at_key, [(list(error.absolute_path), error.message) for error in errors]

    def test_a_summary_may_carry_true_at_each_position(
        self, protocol_schema: dict[str, Any], registry: Registry
    ) -> None:
        # The overlay constrains a present key to `true`, it does not forbid it; `optional: true` is set
        # by hand because no shipped layer is optional.
        response = self._response(SUMMARY_DISCOVERY_SEED)
        response["layers"][0]["optional"] = True
        field = response["layers"][0]["element_kinds"][0]["fields"][0]
        assert field.get("required") is True and field.get("immutable") is True, field
        self._validator(protocol_schema, registry).validate(response)

    def test_the_unmutated_seeds_validate(self, protocol_schema: dict[str, Any], registry: Registry) -> None:
        # A seed that failed on its own would make the refusals above pass for the wrong reason.
        validator = self._validator(protocol_schema, registry)
        for path in (FULL_DISCOVERY_SEED, SUMMARY_DISCOVERY_SEED):
            validator.validate(self._response(path))

    def test_a_summary_response_may_omit_the_three_keys(
        self, protocol_schema: dict[str, Any], registry: Registry
    ) -> None:
        response = self._response(SUMMARY_DISCOVERY_SEED)
        assert response["form"] == "summary"
        for layer in response["layers"]:
            layer.pop("optional", None)
            for element_kind in layer["element_kinds"]:
                for field in element_kind["fields"]:
                    field.pop("required", None)
                    field.pop("immutable", None)
        self._validator(protocol_schema, registry).validate(response)

    def test_every_shipped_discovery_vector_still_validates(
        self, protocol_schema: dict[str, Any], registry: Registry
    ) -> None:
        validator = self._validator(protocol_schema, registry)
        assert DISCOVERY_EXAMPLE_FILES, "no discovery vector ships"
        for path in DISCOVERY_EXAMPLE_FILES:
            validator.validate(self._response(path))

    def test_the_schema_uses_no_default_keyword_on_the_three_keys(self, protocol_schema: dict[str, Any]) -> None:
        # Absence means `false` by the protocol's rule, not a JSON Schema `default`, which a validator
        # ignores and a client might mistake for a filled-in value.
        definitions = protocol_schema["$defs"]
        declarations = (
            definitions["layerDescriptor"]["properties"]["optional"],
            definitions["fieldDescriptor"]["properties"]["required"],
            definitions["fieldDescriptor"]["properties"]["immutable"],
        )
        for declaration in declarations:
            assert "default" not in declaration, declaration
            assert "Absent means `false`" in declaration.get("description", ""), declaration
        assert _find_keys(definitions["discoverResponse"], "default") == [], "an overlay uses `default`"

    def test_the_descriptors_declare_the_keys_without_requiring_them(self, protocol_schema: dict[str, Any]) -> None:
        # `additionalProperties: false` is evaluated against `properties` alone and
        # does not see into a `then`, so the keys must stay declared on the object.
        definitions = protocol_schema["$defs"]
        for name, keys in (("layerDescriptor", ("optional",)), ("fieldDescriptor", ("required", "immutable"))):
            for key in keys:
                assert key not in definitions[name]["required"], (name, key)
                assert key in definitions[name]["properties"], (name, key)
