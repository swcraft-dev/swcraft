"""Every integer field of ``protocol.schema.json`` reads an integral number as the schema does.

JSON Schema 2020-12's ``"type": "integer"`` is a number with no fractional part, so ``2.0`` is an
integer and ``2.5`` is not: a client whose JSON library writes every number as a float must not be
refused. Every such field is parsed through one validator, :func:`swcraft_tools.query.integral`.

The fields are the schema's, never hand-picked: :data:`INTEGER_FIELDS` walks the schema and each
field is placed on a seed integer whose fraction the schema refuses at exactly that node;
``test_every_integer_field_is_placed`` fails when a new field has no seed. On each field an
integral float is accepted by both the schema and the parser and comes back out of ``to_json`` as
the ``int``; a fraction and ``true`` are refused by both.

Does not reach: an integer the schema does not type as one (a change operation's ``value`` is
``$defs/value``; ``confirm_links``' version is checked by
:func:`~swcraft_tools.editing.protocol.is_positive_int`); ``vector.schema.json``, the harness's
shape; the MCP tools' typed arguments, which FastMCP validates first
(``packages/mcp/tests/test_protocol_integers.py``).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource
from swcraft_tools.editing.passes import pass_a
from swcraft_tools.editing.protocol import ProtocolError, integral, parse_response
from test_editing_protocol_schema_parity import PROTOCOL_PATH, REQUEST_SEEDS, RESPONSE_SEEDS

SCHEMA: dict[str, Any] = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))


def _integer_nodes(node: Any, pointer: str = "#") -> Iterator[tuple[str, dict[str, Any]]]:
    """Every schema node whose ``type`` is ``integer`` or a list holding it, with its JSON pointer."""
    if isinstance(node, dict):
        declared = node.get("type")
        if declared == "integer" or (isinstance(declared, list) and "integer" in declared):
            yield pointer, node
        for key, child in node.items():
            yield from _integer_nodes(child, f"{pointer}/{key}")
    elif isinstance(node, list):
        for index, child in enumerate(node):
            yield from _integer_nodes(child, f"{pointer}/{index}")


#: The schema's integer fields, by JSON pointer: the list this module tests, derived and never typed.
INTEGER_FIELDS: dict[str, dict[str, Any]] = dict(_integer_nodes(SCHEMA))
_POINTER_OF: dict[int, str] = {id(node): pointer for pointer, node in INTEGER_FIELDS.items()}

_REGISTRY = Registry().with_resources([(SCHEMA["$id"], Resource.from_contents(SCHEMA))])
_VALIDATORS = {
    side: Draft202012Validator({"$ref": f"{SCHEMA['$id']}#/$defs/{side}"}, registry=_REGISTRY)
    for side in ("request", "response")
}

_ADDRESS = {"layer": "swc", "element": "ExampleSWC"}
_TARGET = {"kind": "swc", "owner": None, "name": "ExampleSWC"}
_SCORE = {"n": 1, "d": 2}

#: Response shapes no shipped vector carries an integer of: the trace search's
#: ``neighbours`` and ``similar`` evidence (both spellings of ``share``), a
#: stated link version, and the two ``more`` counts of ``find``.
SYNTHETIC_RESPONSES: dict[str, dict[str, Any]] = {
    "find-for-requirement-evidence": {
        "response": "find",
        "baseline": "rev1:9f4c2a7be3d18056",
        "for_requirement": "REQ_EXAMPLE_002",
        "neighbours": 3,
        "places": [
            {
                "group": {"kind": "swc", "name": "ExampleSWC"},
                "score": _SCORE,
                "evidence": [
                    {"kind": "neighbours", "share": 1, "seeded": 2, "of": 3, "examples": ["REQ_EXAMPLE_001"]},
                    {"kind": "neighbours", "share": _SCORE, "seeded": 1, "of": 2, "examples": []},
                    {"kind": "similar", "score": _SCORE, "count": 4, "examples": ["REQ_EXAMPLE_001"]},
                ],
                "rows": [
                    {"address": _ADDRESS, "target": _TARGET, "score": _SCORE, "band": "weak", "evidence": []},
                ],
            }
        ],
        "existing_links": [
            {"address": _ADDRESS, "target": _TARGET, "member": "REQ_EXAMPLE_002@3", "version": 3, "confirmed": True}
        ],
    },
    "find-names-more": {
        "response": "find",
        "baseline": "rev1:9f4c2a7be3d18056",
        "results": [{"name": "Example", "candidates": "none", "more": 2}],
    },
    "find-text-more": {
        "response": "find",
        "baseline": "rev1:9f4c2a7be3d18056",
        "text": "example",
        "candidates": [{"address": {"layer": "requirements", "element": "R"}, "identity": "R", "words": ["example"]}],
        "more": 2,
    },
}


def _integers(document: Any, prefix: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    """The path of every integer (never a ``bool``) *document* holds, at any depth."""
    if isinstance(document, dict):
        for key, value in document.items():
            yield from _integers(value, (*prefix, key))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            yield from _integers(value, (*prefix, index))
    elif isinstance(document, int) and not isinstance(document, bool):
        yield prefix


def _at(document: Any, path: tuple[Any, ...]) -> Any:
    for step in path:
        document = document[step]
    return document


def _with(document: Any, path: tuple[Any, ...], value: Any) -> Any:
    clone = copy.deepcopy(document)
    _at(clone, path[:-1])[path[-1]] = value
    return clone


def _type_errors(errors: Iterator[ValidationError]) -> Iterator[ValidationError]:
    """Every ``type`` error, the ones a ``oneOf``/``anyOf`` keeps in its ``context`` included."""
    for error in errors:
        if error.validator == "type":
            yield error
        yield from _type_errors(iter(error.context or ()))


def _seeds() -> Iterator[tuple[str, str, dict[str, Any]]]:
    for name, seed in REQUEST_SEEDS:
        yield "request", name, seed
    for name, seed in [*RESPONSE_SEEDS, *SYNTHETIC_RESPONSES.items()]:
        yield "response", name, seed


def _placements() -> dict[str, tuple[str, str, dict[str, Any], tuple[Any, ...]]]:
    """Per schema integer field, the first seed and path that carry an integer the field types."""
    placed: dict[str, tuple[str, str, dict[str, Any], tuple[Any, ...]]] = {}
    for side, name, seed in _seeds():
        for path in _integers(seed):
            mutant = _with(seed, path, _at(seed, path) + 0.5)
            for error in _type_errors(_VALIDATORS[side].iter_errors(mutant)):
                pointer = _POINTER_OF.get(id(error.schema))
                if pointer is not None and tuple(error.absolute_path) == path:
                    placed.setdefault(pointer, (side, name, seed, path))
    return placed


PLACEMENTS = _placements()


def _parsed(side: str, document: Any) -> dict[str, Any] | None:
    """The parser's reading of *document* written back out, or ``None`` when it refuses it.

    A ``ValueError`` out of the request side (whose refusal is :class:`ProtocolError`) is a defect and fails.
    """
    if side == "request":
        try:
            return pass_a(document).to_json()
        except ProtocolError:
            return None
    try:
        return parse_response(document).to_json()
    except ValueError:
        return None


def test_the_schema_declares_integer_fields() -> None:
    assert len(INTEGER_FIELDS) >= 20, sorted(INTEGER_FIELDS)


def test_every_integer_field_is_placed() -> None:
    assert sorted(set(INTEGER_FIELDS) - set(PLACEMENTS)) == []


@pytest.mark.parametrize("pointer", sorted(INTEGER_FIELDS))
class TestEveryIntegerField:
    def test_an_integral_float_is_the_integer(self, pointer: str) -> None:
        side, name, seed, path = PLACEMENTS[pointer]
        number = _at(seed, path)
        document = _with(seed, path, float(number))
        assert _VALIDATORS[side].is_valid(document), name
        written = _parsed(side, document)
        assert written is not None, f"{name}: {path} = {float(number)!r} refused by the parser"
        value = _at(written, path)
        assert type(value) is int and value == number

    @pytest.mark.parametrize("wrong", ["fraction", "bool"])
    def test_a_non_integer_is_refused_by_both(self, pointer: str, wrong: str) -> None:
        side, name, seed, path = PLACEMENTS[pointer]
        document = _with(seed, path, _at(seed, path) + 0.5 if wrong == "fraction" else True)
        assert not _VALIDATORS[side].is_valid(document), name
        assert _parsed(side, document) is None, f"{name}: {path} accepted by the parser"


@pytest.mark.parametrize(
    ("value", "minimum", "expected"),
    [
        (2, None, 2),
        (2.0, None, 2),
        (-3.0, None, -3),
        (0.0, 0, 0),
        (0.0, 1, None),
        (2.5, None, None),
        (float("inf"), None, None),
        (float("nan"), None, None),
        (True, None, None),
        (False, 0, None),
        ("2", None, None),
        (None, None, None),
    ],
)
def test_the_one_validator(value: Any, minimum: int | None, expected: int | None) -> None:
    result = integral(value, minimum)
    assert result == expected and (result is None or type(result) is int)
