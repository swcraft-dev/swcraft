"""The requirements interchange format: the hand-authored schema against the reader.

Two independent statements of one profile -- ``requirements_interchange.schema.json``
(package data, Draft 2020-12) and the strict Pydantic reader
``swcraft_tools.requirements_interchange`` -- are held to the vectors under
``packages/tools/tests/fixtures/contracts/requirements-interchange/``: a structural vector is judged alike
by both, and a reader-only vector is accepted by the schema and refused by the
reader, the rules JSON Schema cannot state, and a schema-stricter vector is
refused by the schema and accepted by the reader, which reads the selected
version alone. Each refusal's key path is pinned, so a refusal moving to another
key is a failure here rather than a message nobody reads. The committed
Sphinx-Needs 9.2.0 build under ``sphinx-build/`` is read through its mapping and
checked need by need: a file SWCraft did not write.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any, get_args

import pytest
from conftest import REPO_ROOT
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from swcraft_tools import requirements_interchange as interchange
from swcraft_tools.requirements_interchange import (
    SPHINX_NEEDS_BOOKKEEPING_KEYS,
    IgnoredNeed,
    InterchangeError,
    Need,
    RemovedNeed,
    parse_needs_text,
    read_needs_file,
)
from swcraft_tools.requirements_mapping import DEFAULT_MAPPING, parse_mapping_text, read_mapping
from swcraft_tools.schema import AsilValue, Requirement, requirement_id_problem

CONTRACT_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "contracts" / "requirements-interchange"
EXAMPLES = sorted((CONTRACT_DIR / "examples").glob("*.json"), key=lambda p: p.as_posix())
NEGATIVES = sorted((CONTRACT_DIR / "negative").glob("*.json"), key=lambda p: p.as_posix())
READER_ONLY = sorted((CONTRACT_DIR / "negative-reader-only").glob("*.json"), key=lambda p: p.as_posix())
SCHEMA_STRICTER = sorted((CONTRACT_DIR / "schema-stricter").glob("*.json"), key=lambda p: p.as_posix())
SPHINX_BUILD = CONTRACT_DIR / "sphinx-build"

_NEED = "/versions/SRS-1/needs/SWR-1"

#: Every refusal vector's key path: the reader's pointer, decided per file.
EXPECTED_POINTERS = {
    "bad-asil.json": f"{_NEED}/asil",
    "blank-title.json": f"{_NEED}/title",
    "creator-not-object.json": "/versions/SRS-1/creator",
    "empty-current-version-naming-no-version.json": "/current_version",
    "format-version-2.json": "/format_version",
    "id-with-at.json": "/versions/SRS-1/needs/SWR-1@2/id",
    "id-with-colon.json": "/versions/SRS-1/needs/hardware:Core0/id",
    "missing-type.json": f"{_NEED}/type",
    "need-source.json": f"{_NEED}/source",
    "need-version.json": f"{_NEED}/version",
    "needs-amount-boolean.json": "/versions/SRS-1/needs_amount",
    "needs-amount-fraction.json": "/versions/SRS-1/needs_amount",
    "needs-defaults-removed.json": "/versions/SRS-1/needs_defaults_removed",
    "revision-not-string.json": f"{_NEED}/revision",
    "status-not-string.json": f"{_NEED}/status",
    "unknown-envelope-key.json": "/generator",
    "unknown-need-key.json": f"{_NEED}/priority",
    "unknown-version-key.json": "/versions/SRS-1/needs_count",
    # reader-only
    "backlink-of-unknown-key.json": f"{_NEED}/verifies_back",
    "current-version-lone-surrogate.json": "/current_version",
    "current-version-naming-no-version.json": "/current_version",
    "duplicate-key.json": f"{_NEED}/title",
    "id-differs-from-key.json": "/versions/SRS-1/needs/SWR-2/id",
    "id-lone-surrogate.json": "/versions/SRS-1/needs/SWR-\ud800/id",
    "link-parent-dotted-need.json": f"{_NEED}/derived_from/0",
    "link-parent-id-rule.json": f"{_NEED}/derived_from/0",
    "needs-amount-wrong.json": "/versions/SRS-1/needs_amount",
    "parent-lone-surrogate.json": f"{_NEED}/derived_from/0",
    "parent-with-at.json": f"{_NEED}/derived_from/0",
}


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads((resource_files("swcraft_tools") / interchange.SCHEMA_RESOURCE).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _refusal(path: Path) -> InterchangeError:
    with pytest.raises(InterchangeError) as caught:
        read_needs_file(path)
    return caught.value


class TestTheVectors:
    def test_every_directory_holds_vectors(self) -> None:
        assert EXAMPLES and NEGATIVES and READER_ONLY and SCHEMA_STRICTER

    def test_every_refusal_vector_has_one_pinned_pointer_and_every_pin_a_vector(self) -> None:
        assert sorted(p.name for p in NEGATIVES + READER_ONLY) == sorted(EXPECTED_POINTERS)

    @pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
    def test_an_example_is_accepted_by_both(self, path: Path, validator: Draft202012Validator) -> None:
        assert list(validator.iter_errors(_load(path))) == []
        read_needs_file(path)

    @pytest.mark.parametrize("path", NEGATIVES, ids=lambda p: p.name)
    def test_a_structural_negative_is_refused_by_both(self, path: Path, validator: Draft202012Validator) -> None:
        assert not validator.is_valid(_load(path))
        assert _refusal(path).pointer == EXPECTED_POINTERS[path.name]

    @pytest.mark.parametrize("path", READER_ONLY, ids=lambda p: p.name)
    def test_a_reader_only_negative_is_accepted_by_the_schema_and_refused_by_the_reader(
        self, path: Path, validator: Draft202012Validator
    ) -> None:
        # The duplicate key is invisible to the schema: `json.loads` keeps the last value.
        assert list(validator.iter_errors(_load(path))) == []
        assert _refusal(path).pointer == EXPECTED_POINTERS[path.name]

    @pytest.mark.parametrize("path", SCHEMA_STRICTER, ids=lambda p: p.name)
    def test_a_schema_stricter_vector_is_refused_by_the_schema_and_accepted_by_the_reader(
        self, path: Path, validator: Draft202012Validator
    ) -> None:
        # JSON Schema cannot select a version, so it judges every version alike; the reader reads one.
        assert not validator.is_valid(_load(path))
        read_needs_file(path)

    # The contract's index, a closed record, is held to these vectors by a closed test.


class TestWhatTheReaderReturns:
    def test_the_profile_example_reads_the_baseline_and_every_key(self) -> None:
        selected = read_needs_file(CONTRACT_DIR / "examples" / "profile-example.json")
        assert selected.baseline == "SRS-2.3"
        assert selected.in_file_order == (
            Need(
                id="SWR-12",
                title="Wheel speed plausibility",
                description="The software shall reject a wheel speed above 400 km/h.",
                req_type="req",
                asil="B(D)",
                document="SRS Brake Control",
                revision="7",
                derived_from=("SG-1",),
            ),
        )

    def test_the_selected_version_is_read_in_file_order_and_the_other_is_not(self) -> None:
        selected = read_needs_file(CONTRACT_DIR / "examples" / "two-versions.json")
        assert selected.baseline == "SRS-2.3"
        assert [(need.id, need.req_type) for need in selected.needs] == [("SG-1", "req"), ("SWR-12", "req")]

    def test_the_empty_version_name_states_no_baseline(self) -> None:
        selected = read_needs_file(CONTRACT_DIR / "examples" / "empty-current-version.json")
        assert selected.baseline is None
        assert [need.id for need in selected.needs] == ["SWR-1"]

    def test_null_and_absent_are_the_same(self) -> None:
        first, second = read_needs_file(CONTRACT_DIR / "examples" / "rejected.json").in_file_order
        assert isinstance(first, RemovedNeed)
        assert first.status == "rejected"
        assert isinstance(second, Need)
        assert asdict(second) | {"id": "", "title": "", "description": ""} == {
            "id": "",
            "title": "",
            "description": "",
            "req_type": "req",
            "asil": None,
            "document": None,
            "revision": None,
            "derived_from": (),
        }

    def test_a_byte_order_mark_is_skipped(self, tmp_path: Path) -> None:
        source = (CONTRACT_DIR / "examples" / "minimal.json").read_bytes()
        bommed = tmp_path / "bom.json"
        bommed.write_bytes(b"\xef\xbb\xbf" + source)
        assert read_needs_file(bommed) == read_needs_file(CONTRACT_DIR / "examples" / "minimal.json")


def _document(need: dict[str, Any], **version: Any) -> str:
    return json.dumps({"current_version": "SRS-1", "versions": {"SRS-1": {"needs": {need["id"]: need}, **version}}})


_PLAIN = {"id": "SWR-1", "title": "T", "content": "C", "type": "req"}


#: Who refuses a text: both statements, the reader alone (JSON Schema cannot state the rule), or
#: the JSON parser before either (the schema has no document to judge).
_BOTH, _READER, _UNPARSED = "both", "reader", "unparsed"


class TestTheRefusalSentences:
    """Each sentence a user reads, with the key path before it, and which statement refuses it."""

    @pytest.mark.parametrize(
        ("text", "message", "judge"),
        [
            (
                _document({**_PLAIN, "version": 2}),
                f"{_NEED}/version: 'version' is refused: SWCraft keeps its own",
                _BOTH,
            ),
            (
                _document({**_PLAIN, "source": "x"}),
                f"{_NEED}/source: 'source' is refused: every imported requirement",
                _BOTH,
            ),
            (
                _document({**_PLAIN, "priority": 1}),
                f"{_NEED}/priority: unknown key: map it under 'fields' in --mapping, or list it under 'ignore'; "
                "a need is read from id, type, title, content, asil, document, revision, derived_from, status",
                _BOTH,
            ),
            (
                _document({**_PLAIN, "priority_back": []}),
                f"{_NEED}/priority_back: unknown key: it mirrors the link 'priority', which is neither read nor "
                "ignored; map 'priority' under 'fields' in --mapping, or list it under 'ignore'",
                _READER,
            ),
            (
                _document({**_PLAIN, "version_back": []}),
                f"{_NEED}/version_back: unknown key: it mirrors the link 'version'",
                _READER,
            ),
            (_document({**_PLAIN, "title": " "}), f"{_NEED}/title: must not be blank", _BOTH),
            (_document({**_PLAIN, "content": "\t\n"}), f"{_NEED}/content: must not be blank", _BOTH),
            (_document({**_PLAIN, "content": "\x1c\x85"}), f"{_NEED}/content: must not be blank", _BOTH),
            (_document({**_PLAIN, "title": 7}), f"{_NEED}/title: must be a string", _BOTH),
            (
                _document({**_PLAIN, "type": "story"}),
                f"{_NEED}/type: type 'story' is not SWCraft's; map it under 'types' in --mapping",
                _BOTH,
            ),
            (_document({**_PLAIN, "type": 1}), f"{_NEED}/type: must be a string", _BOTH),
            (
                _document({**_PLAIN, "derived_from": ["a/b"]}),
                f"{_NEED}/derived_from/0: a parent is a bare requirement id: id must not contain path separators",
                _READER,
            ),
            (
                _document({**_PLAIN, "derived_from": [" "]}),
                f"{_NEED}/derived_from/0: a parent is a bare requirement id: id must not be empty",
                _BOTH,
            ),
            (_document({**_PLAIN, "derived_from": [1]}), f"{_NEED}/derived_from/0: must be a string", _BOTH),
            (_document({**_PLAIN, "content": ""}), f"{_NEED}/content: must not be blank", _BOTH),
            (
                json.dumps({"format_version": 2, "current_version": "S", "versions": {"S": {"needs": {}}}}),
                "/format_version: must be 1: format 1 is the one needs.json format this reader knows",
                _BOTH,
            ),
            (
                json.dumps({"format_version": True, "current_version": "S", "versions": {"S": {"needs": {}}}}),
                "/format_version: must be an integer",
                _BOTH,
            ),
            (_document(_PLAIN, needs_amount=True), "/versions/SRS-1/needs_amount: must be an integer", _BOTH),
            (_document(_PLAIN, needs_amount=-1), "/versions/SRS-1/needs_amount: must be >= 0", _BOTH),
            (
                _document(_PLAIN, needs_amount=None),
                "/versions/SRS-1/needs_amount: must not be null: omit the key instead",
                _BOTH,
            ),
            (_document(_PLAIN, filters_amount=True), "/versions/SRS-1/filters_amount: must be an integer", _BOTH),
            (_document(_PLAIN, filters_amount=0.5), "/versions/SRS-1/filters_amount: must be an integer", _BOTH),
            (_document(_PLAIN, filters_amount=-1), "/versions/SRS-1/filters_amount: must be >= 0", _BOTH),
            (
                _document(_PLAIN, filters_amount=None),
                "/versions/SRS-1/filters_amount: must not be null: omit the key instead",
                _BOTH,
            ),
            (
                _document(_PLAIN, needs_amount=2),
                "/versions/SRS-1/needs_amount: states 2 needs and the version holds 1",
                _READER,
            ),
            (_document(_PLAIN, created=None), "/versions/SRS-1/created: must not be null: omit the key instead", _BOTH),
            (
                json.dumps({"current_version": "S", "versions": {"S": {"needs": {}}}, "created": None}),
                "/created: must not be null: omit the key instead",
                _BOTH,
            ),
            (
                json.dumps({"current_version": "S", "versions": {"S": {"needs": {}}}, "project": None}),
                "/project: must not be null: omit the key instead",
                _BOTH,
            ),
            (
                _document(_PLAIN, needs_defaults_removed=True),
                "/versions/SRS-1/needs_defaults_removed: must be false: a file written with its defaults removed",
                _BOTH,
            ),
            (
                _document({**_PLAIN, "id": "SWR-2"}).replace('"SWR-2": {', '"SWR-1": {'),
                f"{_NEED}/id: 'SWR-2' is filed under the key 'SWR-1'",
                _READER,
            ),
            ('{"current_version": "X", "versions": {}}', "/current_version: 'X' names no key of 'versions'", _READER),
            (
                '{"current_version": "S", "versions": {"S": {"needs": {"SWR-1": 1}}}}',
                "/versions/S/needs/SWR-1: must be a JSON object",
                _BOTH,
            ),
            (
                '{"current_version": "S", "versions": {"S": {"needs": {}}, "T": 1}}',
                "/versions/T: must be a JSON object",
                _BOTH,
            ),
            (
                '{"current_version": "S", "versions": {"S": {"needs": {}}, "T": {"needs": []}}}',
                "/versions/T/needs: must be a JSON object",
                _BOTH,
            ),
            (
                '{"current_version": "S", "versions": {"S": {"needs": {}}, "T": {}}}',
                "/versions/T/needs: is required",
                _BOTH,
            ),
            ("[]", "(top level): must be a JSON object", _BOTH),
            (
                '{"current_version": "S", "current_version": "S", "versions": {"S": {"needs": {}}}}',
                "/current_version: the key appears twice",
                _READER,
            ),
            (
                "{",
                "not a JSON document (Expecting property name enclosed in double quotes at line 1, column 2)",
                _UNPARSED,
            ),
            ('{"a": NaN}', "not a JSON document (NaN is not a JSON value)", _UNPARSED),
            ('{"a": Infinity}', "not a JSON document (Infinity is not a JSON value)", _UNPARSED),
            ('{"a": -Infinity}', "not a JSON document (-Infinity is not a JSON value)", _UNPARSED),
            # The two payloads carry short ids: pytest writes a test's id into PYTEST_CURRENT_TEST, and an
            # id built from 200 000 brackets exceeds Windows' 32 767-character limit for an environment
            # variable.
            pytest.param(
                '{"a": ' + "1" * 5000 + "}",
                "not a JSON document (Exceeds the limit (4300 digits)",
                _UNPARSED,
                id="integer-of-5000-digits",
            ),
            pytest.param(
                "[" * 100_000 + "]" * 100_000,
                "not a JSON document (maximum recursion depth exceeded",
                _UNPARSED,
                id="arrays-nested-100000-deep",
            ),
        ],
    )
    def test_the_sentence(self, text: str, message: str, judge: str, validator: Draft202012Validator) -> None:
        with pytest.raises(InterchangeError) as caught:
            parse_needs_text(text)
        assert str(caught.value).startswith(message), str(caught.value)
        if judge != _UNPARSED:
            # A duplicate key is invisible to the schema: `json.loads` keeps the last value.
            assert validator.is_valid(json.loads(text)) == (judge == _READER)

    @pytest.mark.parametrize(
        ("need", "message"),
        [
            ({"priority": 1}, f"{_NEED}/priority: unknown key"),
            ({"title": " "}, f"{_NEED}/title: must not be blank"),
            ({"id": "SWR-1@2"}, "/versions/SRS-1/needs/SWR-1@2/id: id must not contain '@'"),
            (
                {"derived_from": ["bad@1"]},
                f"{_NEED}/derived_from/0: a parent is a bare requirement id: id must not contain '@'",
            ),
            (
                {"derived_from": [""]},
                f"{_NEED}/derived_from/0: a parent is a bare requirement id: id must not be empty",
            ),
            ({"derived_from": ["\ud800"]}, f"{_NEED}/derived_from/0: holds a lone surrogate (U+D800)"),
        ],
        ids=["unknown-key", "blank-title", "id-with-at", "parent-with-at", "blank-parent", "surrogate-parent"],
    )
    def test_a_need_its_status_removes_is_validated_whole(self, need: dict[str, Any], message: str) -> None:
        # A removed need is read as any other before its status is looked at: a file that breaks
        # the format is refused whatever its status says.
        with pytest.raises(InterchangeError) as caught:
            parse_needs_text(_document({**_PLAIN, "status": "rejected", **need}))
        assert str(caught.value).startswith(message), str(caught.value)

    def test_a_need_its_status_removes_filed_under_another_key_is_refused(self) -> None:
        text = _document({**_PLAIN, "status": "rejected", "id": "SWR-2"}).replace('"SWR-2": {', '"SWR-1": {')
        with pytest.raises(InterchangeError) as caught:
            parse_needs_text(text)
        assert str(caught.value).startswith(f"{_NEED}/id: 'SWR-2' is filed under the key 'SWR-1'")

    def test_an_ignored_need_s_id_holding_a_lone_surrogate_is_refused(self) -> None:
        text = '{"current_version": "S", "versions": {"S": {"needs": {"SWR-\\ud800": '
        text += '{"id": "SWR-\\ud800", "type": "test"}}}}}'
        with pytest.raises(InterchangeError) as caught:
            parse_needs_text(text, parse_mapping_text("types: {req: req, test: ignore}\n"))
        assert caught.value.pointer == "/versions/S/needs/SWR-\ud800/id"
        assert caught.value.sentence == ("holds a lone surrogate (U+D800), which no UTF-8 text can carry")

    @pytest.mark.parametrize("blank", [" ", "\t "])
    def test_a_blank_version_name_states_no_baseline(self, blank: str) -> None:
        text = json.dumps({"current_version": blank, "versions": {blank: {"needs": {"SWR-1": _PLAIN}}}})
        assert parse_needs_text(text).baseline is None

    @pytest.mark.parametrize("count", [0, 1.0])
    def test_an_integral_filters_amount_is_a_count(self, count: float) -> None:
        assert parse_needs_text(_document(_PLAIN, filters_amount=count)).needs

    def test_a_need_id_is_refused_in_the_models_own_words(self) -> None:
        with pytest.raises(InterchangeError) as caught:
            parse_needs_text(_document({**_PLAIN, "id": "SWR-1@2"}))
        assert caught.value.sentence == requirement_id_problem("SWR-1@2")

    def test_non_utf8_bytes_are_refused_with_the_offset(self, tmp_path: Path) -> None:
        latin = tmp_path / "latin.json"
        latin.write_bytes(b'{"x": "\xe9"}')
        refusal = _refusal(latin)
        assert refusal.pointer is None
        assert str(refusal) == "not UTF-8 text (invalid continuation byte at byte 7)"

    def test_an_unreadable_file_is_refused_without_a_pointer(self, tmp_path: Path) -> None:
        refusal = _refusal(tmp_path / "absent.json")
        assert refusal.pointer is None
        assert str(refusal).startswith("cannot read the file (")

    def test_a_pointer_escapes_tilde_and_slash(self) -> None:
        text = json.dumps({"current_version": "a/b~c", "versions": {"a/b~c": {"needs": {}, "zz": 1}}})
        with pytest.raises(InterchangeError) as caught:
            parse_needs_text(text)
        assert caught.value.pointer == "/versions/a~1b~0c/zz"

    @pytest.mark.parametrize(
        ("document", "pointer"),
        [
            ({"current_version": "S", "versions": {"S": {"needs": {}}}, "z\ud800": 1}, "/z\ud800"),
            ({"current_version": "S", "versions": {"S": {"needs": {}, "z\ud800": 1}}}, "/versions/S/z\ud800"),
            (
                {"current_version": "S", "versions": {"S": {"needs": {"SWR-\ud800": {**_PLAIN, "z": 1}}}}},
                "/versions/S/needs/SWR-\ud800/z",
            ),
            ({"current_version": "S", "versions": {"V-\ud800": {}, "S": {"needs": {}}}}, "/versions/V-\ud800/needs"),
        ],
        ids=["top-level", "version-object", "need", "other-version"],
    )
    def test_a_key_holding_a_lone_surrogate_is_named_as_the_file_spells_it(
        self, document: dict[str, Any], pointer: str
    ) -> None:
        with pytest.raises(InterchangeError) as caught:
            parse_needs_text(json.dumps(document))
        assert caught.value.pointer == pointer

    @pytest.mark.parametrize(
        "keys",
        [
            # A refused key beside the key that is its lossy form (each surrogate as U+FFFD).
            {"z\ud800": 1, "z\ufffd\ufffd\ufffd": 1},
            # Two different lone surrogates, one lossy form (built from pairs: ruff's F601 reads the two
            # literals in that same lossy form and calls them one key).
            dict([("z\ud800", 1), ("z\udfff", 1)]),
        ],
        ids=["beside-its-replacement-spelling", "two-surrogates"],
    )
    def test_keys_one_lossy_form_cannot_tell_apart_are_told_apart(self, keys: dict[str, Any]) -> None:
        text = json.dumps({"current_version": "S", "versions": {"S": {"needs": {}, **keys}}})
        with pytest.raises(InterchangeError) as caught:
            parse_needs_text(text)
        assert caught.value.pointer == "/versions/S/z\ud800"
        assert caught.value.sentence.startswith("unknown key: a version object holds only needs,")

    def test_a_lone_surrogate_is_read_and_left_to_the_import(self) -> None:
        (need,) = parse_needs_text(_document({**_PLAIN, "title": "\ud800"})).needs
        assert need.title == "\ud800"


class TestTheTwoStatementsAgree:
    """The hand-authored schema and the reader share no code, so their vocabularies are compared."""

    def test_the_schema_asil_enum_is_asil_value_and_null(self, validator: Draft202012Validator) -> None:
        enum = validator.schema["$defs"]["need"]["properties"]["asil"]["enum"]
        assert enum == [*get_args(AsilValue), "", None]

    def test_the_schema_key_sets_are_the_readers(self, validator: Draft202012Validator) -> None:
        """The envelope keys are the models'; a need's are the default read keys, then the bookkeeping list."""
        schema = validator.schema
        assert list(schema["properties"]) == list(interchange.NeedsDocument.model_fields)
        assert list(schema["$defs"]["version"]["properties"]) == list(interchange.NeedsVersion.model_fields)
        need = schema["$defs"]["need"]
        read = ["id", "title", "content", "type", "status", "asil", "document", "revision", "derived_from"]
        assert set(read) == {"id", "type", *DEFAULT_MAPPING.read_keys()}
        assert list(need["properties"]) == read + [key for key in SPHINX_NEEDS_BOOKKEEPING_KEYS if key not in read]
        assert all(need["properties"][key] is True for key in SPHINX_NEEDS_BOOKKEEPING_KEYS if key not in read)
        assert need["patternProperties"] == {"_back$": True}
        assert need["additionalProperties"] is False

    @pytest.mark.parametrize(
        "value",
        [
            "SWR-1",
            "a.b",
            " x ",
            "SWR 1",
            "",
            " ",
            "\t",
            "a@b",
            "a:b",
            "a/b",
            "a\\b",
            "a..b",
            "..",
            "x\n..",
            "ä",
            # where the `\\s` of Python and of ECMA-262 differ: the schema spells the class out instead
            "\x1c",
            "\x1d",
            "\x1e",
            "\x1f",
            "\x85",
            "\ufeff",
            "\x1cSWR-1\x85",
            "\u3000",
            "\u2028",
            "SWR\ufeff",
        ],
    )
    def test_the_id_pattern_is_the_models_rule(self, value: str, validator: Draft202012Validator) -> None:
        id_schema = {**validator.schema["$defs"]["requirementId"]}
        assert Draft202012Validator(id_schema).is_valid(value) == (requirement_id_problem(value) is None)

    @pytest.mark.parametrize("value", ["x", " x", "", " ", "\t\n", "\x1c\x1d\x1e\x1f", "\x85", "\ufeff", "\u3000"])
    def test_the_text_pattern_is_the_readers_blank_rule(self, value: str, validator: Draft202012Validator) -> None:
        text_schema = {**validator.schema["$defs"]["text"]}
        assert Draft202012Validator(text_schema).is_valid(value) == bool(value.strip())

    @pytest.mark.parametrize("definition", ["requirementId", "text"])
    def test_the_whitespace_class_is_spelled_out_and_is_pythons(
        self, definition: str, validator: Draft202012Validator
    ) -> None:
        """The class reads alike under ECMA-262 and Python: explicit code points, no ``\\s`` or ``\\S``.

        ``[\\s\\S]`` (any character, under both engines) is the one use admitted. The class is
        then held to ``str.isspace()`` -- the set ``str.strip()`` removes -- over the whole BMP;
        every whitespace character is in the BMP.
        """
        pattern = validator.schema["$defs"][definition]["pattern"]
        assert "\\s" not in pattern.replace("[\\s\\S]", "") and "\\S" not in pattern.replace("[\\s\\S]", "")
        # The class runs from U+0009 to the bracket, or to the '/' the id pattern writes last in it.
        (whitespace,) = set(re.findall(r"\\u0009[^\]/]*", pattern))
        spelled = re.compile(f"[{whitespace}]")
        assert all(
            bool(spelled.fullmatch(chr(code))) == chr(code).isspace()
            for code in range(0x10000)
            if not 0xD800 <= code <= 0xDFFF
        )


#: The id pattern as the schema wrote it before the slash moved to the end of each class: its
#: ``[^@:/\\]`` put a token of ``/`` and backslashes in the published text, which a host reading
#: ``/`` and ``\`` alike resolves to a filesystem root. Kept here only as the equivalence oracle.
_ID_PATTERN_WITH_THE_SLASH_FIRST = (
    "^(?![\\s\\S]*\\.\\.)[^@:/\\\\]*[^@:/\\\\\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680"
    "\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000][^@:/\\\\]*$"
)

#: Ids that reach every branch of the class: each separator alone, both, neither, each in front of,
#: inside and behind an id, the other refused characters, blanks, '..', and characters outside ASCII.
_EQUIVALENCE_IDS = (
    "",
    "/",
    "\\",
    "/\\",
    "\\/",
    "//",
    "\\\\",
    "SWR-1",
    "/SWR-1",
    "SWR-1/",
    "SWR/1",
    "\\SWR-1",
    "SWR-1\\",
    "SWR\\1",
    "a/b\\c",
    " / ",
    " \\ ",
    "\u3000/",
    "/\u3000",
    "a@b",
    "a:b",
    "a..b",
    "a.b",
    " x ",
    "\t",
    "\x1c",
    "\x85",
    "\ufeff",
    "ä",
    "äöü/",
    "需求-1",
    "需求\\1",
    "\U0001f600",
    "\U0001f600/",
)


class TestTheIdPatternsSlashMove:
    """The id pattern with the slash written last in each class accepts and refuses what the old one did."""

    def test_the_schema_writes_the_slash_last_and_no_slash_led_separator_run(
        self, validator: Draft202012Validator
    ) -> None:
        pattern = validator.schema["$defs"]["requirementId"]["pattern"]
        assert pattern == _ID_PATTERN_WITH_THE_SLASH_FIRST.replace("[^@:/\\\\]", "[^@:\\\\/]").replace(
            "[^@:/\\\\\\u0009", "[^@:\\\\\\u0009"
        ).replace("\\u3000]", "\\u3000/]")
        assert not re.search(r"/[/\\\\]", pattern)

    @pytest.mark.parametrize("value", _EQUIVALENCE_IDS)
    def test_both_patterns_judge_an_id_alike(self, value: str, validator: Draft202012Validator) -> None:
        pattern = validator.schema["$defs"]["requirementId"]["pattern"]
        old = Draft202012Validator({"type": "string", "pattern": _ID_PATTERN_WITH_THE_SLASH_FIRST})
        new = Draft202012Validator({"type": "string", "pattern": pattern})
        assert old.is_valid(value) == new.is_valid(value)
        assert bool(re.search(_ID_PATTERN_WITH_THE_SLASH_FIRST, value)) == bool(re.search(pattern, value))

    def test_both_patterns_judge_every_character_alike(self, validator: Draft202012Validator) -> None:
        """Every BMP code point alone and between two letters: the classes are sets, so this is all of them."""
        old = re.compile(_ID_PATTERN_WITH_THE_SLASH_FIRST)
        new = re.compile(validator.schema["$defs"]["requirementId"]["pattern"])
        for code in range(0x10000):
            if 0xD800 <= code <= 0xDFFF:
                continue
            for value in (chr(code), f"a{chr(code)}b"):
                assert bool(old.search(value)) == bool(new.search(value)), repr(value)


class TestTheExtractedIdRule:
    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("", "id must not be empty"),
            ("a@b", "id must not contain '@': '@' separates a link's version and ':' an element reference"),
            ("a:b", "id must not contain ':': '@' separates a link's version and ':' an element reference"),
            ("a/../b", "id must not contain path separators or '..'"),
        ],
    )
    def test_the_requirement_raises_the_shared_sentence(self, value: str, message: str) -> None:
        assert requirement_id_problem(value) == message
        with pytest.raises(ValidationError, match="1 validation error") as caught:
            Requirement(id=value, title="t", description="d", version=1, source="external")
        assert caught.value.errors()[0]["msg"] == f"Value error, {message}"

    def test_a_good_id_has_no_problem(self) -> None:
        assert requirement_id_problem("SWR-12") is None


def test_the_reader_imports_no_jsonschema() -> None:
    tree = ast.parse(Path(interchange.__file__).read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not {name for name in imported if name.split(".")[0] in {"jsonschema", "referencing"}}


def test_the_schema_is_package_data_beside_the_reader() -> None:
    resource = resource_files("swcraft_tools") / interchange.SCHEMA_RESOURCE
    assert resource.is_file()
    assert Path(interchange.__file__).with_name(interchange.SCHEMA_RESOURCE).is_file()


#: The bookkeeping list, written out rather than imported, so that the reader's tuple is held to it.
_BOOKKEEPING_FIELDS = (
    "id, docname, lineno, lineno_content, title, status, tags, collapse, hide, layout, style, arch, is_import, "
    "is_external, external_url, external_css, type, type_name, type_prefix, type_color, type_style, is_modified, "
    "modifications, is_need, is_part, parts, id_parent, id_complete, jinja_content, template, pre_template, "
    "post_template, content, pre_content, post_content, has_dead_links, has_forbidden_dead_links, constraints, "
    "constraints_results, constraints_passed, constraints_error, doctype, sections, section_name, signature, "
    "parent_need, links, parent_needs, duration, completion, url, user, avatar, service, created_at, updated_at, "
    "closed_at, query, specific, max_amount, max_content_lines, id_prefix"
).split(", ")


def _team_mapping() -> Any:
    return read_mapping(SPHINX_BUILD / "team.needs-mapping.yaml")


class TestTheSphinxNeedsBuild:
    """The committed ``sphinx-build -b needs`` output of Sphinx-Needs 9.2.0, read through the team's mapping."""

    def test_every_need_is_read_field_by_field(self) -> None:
        selected = read_needs_file(SPHINX_BUILD / "needs.json", _team_mapping())
        assert selected.baseline == "SYS-2.1"
        assert selected.in_file_order == (
            # A link to a part names its need.
            Need(
                id="SW_001",
                title="Brake request latency",
                description="The software shall forward a brake request within 10 ms, as :need:`SYS_001` requires.",
                req_type="req",
                asil="B(C)",
                document="index",
                revision="2",
                derived_from=("SYS_001",),
            ),
            # A conditional link loses its condition; the build wrote the links in its own order.
            Need(
                id="SW_002",
                title="Brake request plausibility",
                description="The software shall reject a brake request outside 0 to 100 %.",
                req_type="req",
                document="index",
                derived_from=("SW_001", "SYS_001"),
            ),
            # A condition holding `]` inside doubled brackets, on a part, beside the plain link: one parent.
            Need(
                id="SW_003",
                title="Brake light",
                description="The software shall switch the brake light on with every brake request.",
                req_type="req",
                asil="QM",
                document="index",
                derived_from=("SYS_001",),
            ),
            # The content is the RST source, markup and part role included, taken as written.
            Need(
                id="SYS_001",
                title="Braking distance",
                description=(
                    "The vehicle **shall** stop within 40 m from 100 km/h.\n"
                    ":np:`(p1) The brake request reaches the actuator within 50 ms.`"
                ),
                req_type="req",
                asil="C",
                document="index",
                revision="3",
            ),
            RemovedNeed(id="SYS_002", status="obsolete"),
            # A `test` need is listed by id; its `verifies` links are ignored.
            IgnoredNeed(id="TC_001", type="test"),
        )

    def test_the_build_holds_what_the_test_relies_on(self) -> None:
        """The build itself carries a part link, a condition, a `]` in a condition and the team's keys."""
        data = _load(SPHINX_BUILD / "needs.json")
        version = data["versions"][data["current_version"]]
        assert version["creator"] == {"program": "sphinx_needs", "version": "9.2.0"}
        needs = version["needs"]
        assert needs["SW_001"]["parent"] == ["SYS_001.p1"]
        assert 'SYS_001[status=="approved"]' in needs["SW_002"]["parent"]
        assert 'SYS_001.p1[[tags[0]=="brake"]]' in needs["SW_003"]["parent"]
        assert needs["TC_001"]["verifies"] == ["SW_001", "SW_002"]
        assert version["needs_schema"]["properties"]["parent"]["field_type"] == "links"
        assert {"parent_back", "verifies_back", "links_back", "safety", "rev", "reviewer"} <= set(needs["SYS_001"])

    def test_with_no_mapping_the_teams_type_is_refused(self) -> None:
        with pytest.raises(InterchangeError) as caught:
            read_needs_file(SPHINX_BUILD / "needs.json")
        assert str(caught.value) == (
            "/versions/SYS-2.1/needs/SW_001/type: type 'swreq' is not SWCraft's; map it under 'types' in --mapping"
        )

    def test_a_type_absent_from_types_is_refused(self) -> None:
        mapping = parse_mapping_text("types: {sysreq: req, test: ignore}\nfields: {derived_from: parent}\n")
        with pytest.raises(InterchangeError) as caught:
            read_needs_file(SPHINX_BUILD / "needs.json", mapping)
        assert caught.value.pointer == "/versions/SYS-2.1/needs/SW_001/type"
        assert caught.value.sentence == "type 'swreq' is not SWCraft's; map it under 'types' in --mapping"

    def test_a_team_key_neither_mapped_nor_ignored_is_refused_naming_the_mapping(self) -> None:
        mapping = parse_mapping_text(
            "types: {sysreq: req, swreq: req, test: ignore}\n"
            "fields: {asil: safety, derived_from: parent, revision: rev}\nignore: [verifies]\n"
        )
        with pytest.raises(InterchangeError) as caught:
            read_needs_file(SPHINX_BUILD / "needs.json", mapping)
        assert caught.value.pointer == "/versions/SYS-2.1/needs/SW_001/reviewer"
        assert caught.value.sentence.startswith("unknown key: map it under 'fields' in --mapping, or list it under")


class TestTheOldExamplesReadAsBefore:
    """A converter's file in SWCraft's names reads with no mapping exactly as the nine-key profile read it."""

    @pytest.mark.parametrize(
        ("example", "baseline", "entries"),
        [
            ("minimal.json", "SRS-1", [("need", "SWR-1", "req", ())]),
            ("needs-amount-integral-number.json", "SRS-1", [("need", "SWR-1", "req", ())]),
            ("empty-current-version.json", None, [("need", "SWR-1", "req", ())]),
            ("two-versions.json", "SRS-2.3", [("need", "SG-1", "req", ()), ("need", "SWR-12", "req", ("SG-1",))]),
            ("rejected.json", "SRS-1", [("removed", "SWR-1", "rejected", ()), ("need", "SWR-2", "req", ())]),
            ("profile-example.json", "SRS-2.3", [("need", "SWR-12", "req", ("SG-1",))]),
        ],
    )
    def test_the_entries(self, example: str, baseline: str | None, entries: list[tuple[Any, ...]]) -> None:
        selected = read_needs_file(CONTRACT_DIR / "examples" / example)
        assert selected.baseline == baseline
        read = [
            ("removed", entry.id, entry.status, ())
            if isinstance(entry, RemovedNeed)
            else ("need", entry.id, entry.req_type, entry.derived_from)
            for entry in selected.in_file_order
            if not isinstance(entry, IgnoredNeed)
        ]
        assert read == entries
        assert selected.ignored == ()

    def test_a_dotted_parent_with_no_needs_schema_is_taken_whole(self) -> None:
        (need,) = parse_needs_text(_document({**_PLAIN, "derived_from": ["SR.3.1", "SR[x]"]})).needs
        assert need.derived_from == ("SR.3.1", "SR[x]")

    def test_a_dotted_parent_under_a_schema_not_labelling_it_links_is_taken_whole(self) -> None:
        schema = {"properties": {"derived_from": {"field_type": "extra"}}}
        (need,) = parse_needs_text(_document({**_PLAIN, "derived_from": ["SR.3.1"]}, needs_schema=schema)).needs
        assert need.derived_from == ("SR.3.1",)

    def test_a_dotted_need_id_is_never_split(self) -> None:
        (need,) = parse_needs_text(_document({**_PLAIN, "id": "SR.3.1"})).needs
        assert need.id == "SR.3.1"


class TestWhatTheReaderNowAccepts:
    def test_a_repeated_parent_is_one(self) -> None:
        (need,) = read_needs_file(CONTRACT_DIR / "examples" / "duplicate-parent.json").needs
        assert need.derived_from == ("SG-1",)

    def test_a_link_field_drops_a_part_and_a_condition(self) -> None:
        (need,) = read_needs_file(CONTRACT_DIR / "examples" / "link-parent-part-and-condition.json").needs
        assert need.derived_from == ("SG-1", "SG-2")

    @pytest.mark.parametrize(
        ("value", "target"),
        [
            ("REQ_1", "REQ_1"),
            ("REQ_1.p1", "REQ_1"),
            ('REQ_1[status=="open"]', "REQ_1"),
            ('REQ_1.p1[status=="open"]', "REQ_1"),
            ('REQ_1[[tags[0]=="x"]]', "REQ_1"),
            ("REQ_1.a.b", "REQ_1"),
            # An unclosed condition falls back to the whole text, as Sphinx-Needs' own parser does.
            ("REQ_1[open", "REQ_1[open"),
            ("REQ_1.p1[open", "REQ_1"),
            # Text after the closing bracket: the address before the bracket, as Sphinx-Needs keeps it.
            ("REQ_1[a]b", "REQ_1"),
            # A `[` first opens no condition.
            ("[x]", "[x]"),
        ],
    )
    def test_the_link_grammar_is_sphinx_needs(self, value: str, target: str) -> None:
        assert interchange._link_target(value, frozenset()) == target

    @pytest.mark.parametrize(
        ("value", "target"),
        [
            # A value that is itself a need of the file, holding no `.`, is that need's id whole.
            ("SYS_REQ[12]", "SYS_REQ[12]"),
            ("REQ_1[[tags[0]]]", "REQ_1[[tags[0]]]"),
            # Not a need of the file: Sphinx-Needs' default grammar, a condition dropped.
            ("SYS_REQ[13]", "SYS_REQ"),
            # A `.` is never read whole, a need of the file or not: no configuration links it.
            ("SYS.REQ.12", "SYS"),
            ("A.B[1]", "A"),
        ],
    )
    def test_a_need_of_the_file_holding_a_bracket_is_read_whole(self, value: str, target: str) -> None:
        """A team's link type may set ``"parse_conditions": False``, which the file does not record."""
        need_ids = frozenset({"SYS_REQ[12]", "REQ_1[[tags[0]]]", "SYS.REQ.12", "A.B[1]"})
        assert interchange._link_target(value, need_ids) == target

    @staticmethod
    def _linked(*needs: dict[str, Any]) -> str:
        schema = {"properties": {"derived_from": {"field_type": "links"}}}
        version = {"needs": {need["id"]: need for need in needs}, "needs_schema": schema}
        return json.dumps({"current_version": "SRS-1", "versions": {"SRS-1": version}})

    @pytest.mark.parametrize("value", ["SYS.REQ.12", "SYS.REQ.12[status=='open']"])
    def test_a_link_field_parent_naming_a_dotted_need_of_the_file_is_refused(self, value: str) -> None:
        """No link can reach it in Sphinx-Needs 9.2.0: refused, never read as ``SYS`` nor made a stub."""
        text = self._linked({**_PLAIN, "id": "SYS.REQ.12"}, {**_PLAIN, "id": "SWR-1", "derived_from": ["SG-0", value]})

        with pytest.raises(InterchangeError) as caught:
            parse_needs_text(text)

        assert caught.value.pointer == "/versions/SRS-1/needs/SWR-1/derived_from/1"
        assert caught.value.sentence == (
            f"'{value}' names the need 'SYS.REQ.12', whose id holds '.': Sphinx-Needs 9.2.0 reads a link to it as "
            "the need 'SYS' and a part, under every configuration, so it cannot link such an id and SWCraft does "
            "not support it as a parent"
        )

    def test_a_whole_value_naming_a_dotted_need_of_the_file_is_refused_before_its_address(self) -> None:
        """``A.B[1]`` is a need of the file and ``A.B`` is not: refused by the whole value."""
        text = self._linked({**_PLAIN, "id": "A.B[1]"}, {**_PLAIN, "id": "SWR-1", "derived_from": ["A.B[1]"]})

        with pytest.raises(InterchangeError) as caught:
            parse_needs_text(text)

        assert caught.value.sentence.startswith("'A.B[1]' names the need 'A.B[1]', whose id holds '.': ")

    def test_a_dotted_link_field_parent_that_is_no_need_of_the_file_keeps_the_part_reading(self) -> None:
        """``SG-1.p1`` is a part of ``SG-1`` or a need of another delivery: the reader cannot tell, so the grammar."""
        text = self._linked({**_PLAIN, "id": "SG-1"}, {**_PLAIN, "id": "SWR-1", "derived_from": ["SG-1.p1", "OTHER.2"]})

        needs = {need.id: need for need in parse_needs_text(text).needs}

        assert needs["SWR-1"].derived_from == ("SG-1", "OTHER")

    def test_a_link_field_parent_naming_a_bracketed_need_of_the_file_is_read_whole(self) -> None:
        schema = {"properties": {"derived_from": {"field_type": "links"}}}
        parent = {**_PLAIN, "id": "SYS_REQ[12]"}
        child = {**_PLAIN, "id": "SYS_REQ[13]", "derived_from": ["SYS_REQ[12]", "SYS_REQ[14]"]}
        text = json.dumps(
            {
                "current_version": "SRS-1",
                "versions": {"SRS-1": {"needs": {"SYS_REQ[12]": parent, "SYS_REQ[13]": child}, "needs_schema": schema}},
            }
        )
        needs = {need.id: need for need in parse_needs_text(text).needs}
        # `SYS_REQ[14]` is no need of the file, so its `[14]` is a condition, as Sphinx-Needs reads it by default.
        assert needs["SYS_REQ[13]"].derived_from == ("SYS_REQ[12]", "SYS_REQ")

    def test_empty_text_reads_as_absent(self) -> None:
        (need,) = read_needs_file(CONTRACT_DIR / "examples" / "sphinx-needs-keys.json").needs
        assert (need.asil, need.document, need.revision) == (None, None, None)

    def test_a_backlink_without_its_link_is_ignored(self) -> None:
        raw = _load(CONTRACT_DIR / "examples" / "sphinx-needs-keys.json")["versions"]["SRS-1"]["needs"]["SWR-1"]
        assert "links_back" in raw and "links" not in raw
        assert read_needs_file(CONTRACT_DIR / "examples" / "sphinx-needs-keys.json").needs

    def test_an_older_version_in_another_vocabulary_is_not_read(self) -> None:
        old = {"needs": {"S1": {"id": "S1", "type": "sysreq", "safety": "B", "content": ""}}, "needs_amount": 9}
        text = json.dumps({"current_version": "SRS-2", "versions": {"SRS-1": old, "SRS-2": {"needs": {}}}})
        assert parse_needs_text(text).in_file_order == ()

    def test_format_version_1_is_accepted(self) -> None:
        assert read_needs_file(CONTRACT_DIR / "examples" / "format-version-1.json").needs

    def test_the_list_holds_exactly_the_62_names(self) -> None:
        assert len(SPHINX_NEEDS_BOOKKEEPING_KEYS) == 62
        assert list(SPHINX_NEEDS_BOOKKEEPING_KEYS) == _BOOKKEEPING_FIELDS


class TestTheMapping:
    """What the mapping changes in the reading, over small documents."""

    def test_a_remapped_field_reads_the_teams_key_and_refuses_the_default_one(self) -> None:
        mapping = parse_mapping_text("fields: {asil: safety}\n")
        (need,) = parse_needs_text(_document({**_PLAIN, "safety": "B"}), mapping).needs
        assert need.asil == "B"
        with pytest.raises(InterchangeError) as caught:
            parse_needs_text(_document({**_PLAIN, "asil": "B"}), mapping)
        assert caught.value.pointer == f"{_NEED}/asil"

    def test_a_refusal_is_placed_on_the_teams_key(self) -> None:
        mapping = parse_mapping_text("fields: {asil: safety}\n")
        with pytest.raises(InterchangeError) as caught:
            parse_needs_text(_document({**_PLAIN, "safety": "E"}), mapping)
        assert caught.value.pointer == f"{_NEED}/safety"

    def test_a_status_in_removed_status_removes(self) -> None:
        mapping = parse_mapping_text("removed_status: [obsolete]\n")
        (entry,) = parse_needs_text(_document({**_PLAIN, "status": "obsolete"}), mapping).in_file_order
        assert entry == RemovedNeed("SWR-1", "obsolete")
        # `rejected` is no longer in the list, so it is read and not stored.
        assert parse_needs_text(_document({**_PLAIN, "status": "rejected"}), mapping).needs

    def test_an_ignored_need_reads_only_its_id(self) -> None:
        mapping = parse_mapping_text("types: {req: req, test: ignore}\n")
        ignored = {"id": "SWR-1", "type": "test", "anything": [1], "title": 7}
        assert parse_needs_text(_document(ignored), mapping).in_file_order == (IgnoredNeed("SWR-1", "test"),)

    def test_an_ignored_need_whose_id_differs_from_its_key_is_refused(self) -> None:
        mapping = parse_mapping_text("types: {req: req, test: ignore}\n")
        text = _document({"id": "SWR-2", "type": "test"}).replace('"SWR-2": {', '"SWR-1": {')
        with pytest.raises(InterchangeError) as caught:
            parse_needs_text(text, mapping)
        assert str(caught.value) == (
            f"{_NEED}/id: 'SWR-2' is filed under the key 'SWR-1': a need's id is the key it is filed under"
        )

    @pytest.mark.parametrize(("need_id", "sentence"), [(None, "is required"), (7, "must be a string")])
    def test_an_ignored_need_without_a_string_id_is_refused(self, need_id: object, sentence: str) -> None:
        mapping = parse_mapping_text("types: {req: req, test: ignore}\n")
        need = {"type": "test"} if need_id is None else {"type": "test", "id": need_id}
        text = json.dumps({"current_version": "SRS-1", "versions": {"SRS-1": {"needs": {"SWR-1": need}}}})
        with pytest.raises(InterchangeError) as caught:
            parse_needs_text(text, mapping)
        assert str(caught.value) == f"{_NEED}/id: {sentence}"

    def test_an_ignored_key_ignores_its_backlink_and_its_value_is_not_checked(self) -> None:
        mapping = parse_mapping_text("ignore: [verifies]\n")
        assert parse_needs_text(_document({**_PLAIN, "verifies": 7, "verifies_back": None}), mapping).needs

    def test_the_description_may_come_from_the_title(self) -> None:
        mapping = parse_mapping_text("fields: {description: title}\n")
        (need,) = parse_needs_text(_document({**_PLAIN, "content": ""}), mapping).needs
        assert need.description == need.title == "T"

    def test_a_mapping_cannot_make_version_read(self) -> None:
        mapping = parse_mapping_text("ignore: [verifies]\n")
        with pytest.raises(InterchangeError) as caught:
            parse_needs_text(_document({**_PLAIN, "version": 1}), mapping)
        assert caught.value.sentence.startswith("'version' is refused")
