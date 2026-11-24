"""The vendored passport schema, the strict reader and the interpreter.

Every vector is also judged by `jsonschema` (a development dependency), an independent reading of
the same schema.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tomllib
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError
from swcraft_specimens.errors import GenerationError, SpecimenGeneratorError
from swcraft_specimens.passport import (
    CONTRACT_MAJOR,
    PASSPORT_VERSION,
    SCHEMA_SHA256,
    PassportRefused,
    PassportSchemaError,
    Refusal,
    _check_schema,
    _Interpreter,
    parse_passport_json,
    read_passport,
    schema_bytes,
    schema_document,
    validate_passport,
)

ROOT = Path(__file__).parents[1]
VECTORS = ROOT / "tests/fixtures/passport"
SCHEMA_FILE = ROOT / "src/swcraft_specimens/passport_schema/model_passport.schema.json"
EXAMPLES = sorted((VECTORS / "examples").glob("*.json"), key=lambda p: p.as_posix())
NEGATIVES = sorted((VECTORS / "negative").glob("*.json"), key=lambda p: p.as_posix())

#: SWCraft's pinned refusal of every negative vector, as a dotted path.
EXPECTED_LEAVES = {
    "count-as-string.json": "components.unconnected.total",
    "missing-passport-version.json": "passport_version",
    "negative-count.json": "connectors.links",
    "unknown-key.json": "components.names",
    "unknown-layer-key.json": "layers.data_types",
}


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def minimal() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(load_text(VECTORS / "examples/minimal.json"))
    return document


def refused_leaves(document: object) -> set[str]:
    with pytest.raises(PassportRefused) as caught:
        validate_passport(document)
    return {refusal.leaf for refusal in caught.value.refusals}


def jsonschema_leaves(document: object) -> set[str]:
    """SWCraft's own pointer rule (`_pointer` in its test), written as dotted paths."""
    validator = Draft202012Validator(json.loads(schema_bytes().decode("utf-8")))

    def leaf(error: ValidationError) -> str:
        parts = [str(part) for part in error.absolute_path]
        if error.validator == "required":
            assert isinstance(error.instance, dict)
            assert isinstance(error.validator_value, list)
            parts.extend(key for key in error.validator_value if key not in error.instance)
        elif error.validator == "additionalProperties":
            assert isinstance(error.instance, dict)
            declared = error.schema.get("properties", {}) if isinstance(error.schema, dict) else {}
            parts.extend(key for key in error.instance if key not in declared)
        return ".".join(parts)

    return {leaf(error) for error in validator.iter_errors(document)}


def interpreter_leaves(document: object) -> set[str]:
    try:
        validate_passport(document)
    except PassportRefused as refused:
        return {refusal.leaf for refusal in refused.refusals}
    return set()


class TestTheVendoredSchema:
    def test_its_bytes_are_pinned(self) -> None:
        assert hashlib.sha256(SCHEMA_FILE.read_bytes()).hexdigest() == SCHEMA_SHA256
        assert hashlib.sha256(schema_bytes()).hexdigest() == SCHEMA_SHA256

    def test_its_two_x_values_are_pinned(self) -> None:
        document = schema_document()
        assert document["x-passport-version"] == "1.0.0" == PASSPORT_VERSION
        assert document["x-contract-major"] == 12 == CONTRACT_MAJOR

    def test_it_is_a_valid_draft_2020_12_schema(self) -> None:
        Draft202012Validator.check_schema(schema_document())

    def test_the_package_data_declaration_carries_it(self) -> None:
        pyproject = tomllib.loads(load_text(ROOT / "pyproject.toml"))
        wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
        assert wheel["packages"] == ["src/swcraft_specimens"]
        relative = SCHEMA_FILE.relative_to(ROOT).as_posix()
        assert any(fnmatch(relative, pattern) for pattern in wheel["include"])


class TestTheStrictReader:
    def test_a_repeated_key_is_refused_at_its_path(self) -> None:
        text = '{"layers": {"swc": 1, "swc": 2}}'
        with pytest.raises(PassportRefused) as caught:
            parse_passport_json(text)
        assert caught.value.refusals == (Refusal("layers.swc", "reading", "the key is repeated"),)

    @pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
    def test_a_non_finite_constant_is_refused_at_its_path(self, constant: str) -> None:
        text = '{"identifiers": {"swc": {"mean_length": ' + constant + "}}}"
        with pytest.raises(PassportRefused) as caught:
            parse_passport_json(text)
        assert [r.leaf for r in caught.value.refusals] == ["identifiers.swc.mean_length"]
        assert caught.value.refusals[0].refusal_class == "reading"

    def test_an_overflowing_number_is_refused_as_non_finite(self) -> None:
        with pytest.raises(PassportRefused) as caught:
            parse_passport_json('{"layers": {"swc": 1e400}}')
        assert [r.leaf for r in caught.value.refusals] == ["layers.swc"]

    def test_text_that_is_not_json_is_refused(self) -> None:
        with pytest.raises(PassportRefused) as caught:
            parse_passport_json("{")
        assert [r.leaf for r in caught.value.refusals] == ["(document)"]

    def test_every_refusal_is_collected(self) -> None:
        text = '{"a": 1, "a": 2, "b": NaN}'
        with pytest.raises(PassportRefused) as caught:
            parse_passport_json(text)
        assert [r.leaf for r in caught.value.refusals] == ["a", "b"]

    @pytest.mark.parametrize("depth", [3000, 100000])
    @pytest.mark.parametrize(("opening", "closing"), [("[", "]"), ('{"a":', "}")])
    def test_json_nested_deeper_than_the_reader_follows_is_a_reading_refusal(
        self, depth: int, opening: str, closing: str
    ) -> None:
        # Valid JSON nested this deep once raised RecursionError and the CLI printed a traceback.
        text = opening * depth + ("1" if closing == "}" else "") + closing * depth
        for reader in (parse_passport_json, read_passport):
            with pytest.raises(PassportRefused) as caught:
                reader(text)
            assert caught.value.refusals == (
                Refusal(
                    "(document)",
                    "reading",
                    "the text nests arrays or objects deeper than the reader follows",
                ),
            )

    def test_the_refusal_prints_one_line_per_leaf(self) -> None:
        refused = PassportRefused([Refusal("layers.swc", "schema", "x"), Refusal("connectors.links", "schema", "y")])
        assert str(refused) == ("refused: layers.swc (schema): x\nrefused: connectors.links (schema): y")
        assert isinstance(refused, SpecimenGeneratorError)
        assert not isinstance(refused, GenerationError)

    def test_read_passport_reads_and_validates(self) -> None:
        assert read_passport(load_text(VECTORS / "examples/minimal.json")) == minimal()
        with pytest.raises(PassportRefused):
            read_passport(load_text(VECTORS / "negative/negative-count.json"))


class TestTheInterpreter:
    @pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
    def test_an_example_is_accepted_unchanged(self, path: Path) -> None:
        document = json.loads(load_text(path))
        assert validate_passport(document) == document

    def test_every_negative_has_a_pinned_leaf(self) -> None:
        assert sorted(p.name for p in NEGATIVES) == sorted(EXPECTED_LEAVES)

    @pytest.mark.parametrize("path", NEGATIVES, ids=lambda p: p.name)
    def test_a_negative_is_refused_at_swcrafts_leaf(self, path: Path) -> None:
        assert refused_leaves(json.loads(load_text(path))) == {EXPECTED_LEAVES[path.name]}

    def test_the_refusal_class_is_schema(self) -> None:
        with pytest.raises(PassportRefused) as caught:
            validate_passport(json.loads(load_text(VECTORS / "negative/negative-count.json")))
        assert {r.refusal_class for r in caught.value.refusals} == {"schema"}

    @pytest.mark.parametrize("value", [True, False])
    def test_a_boolean_is_never_a_count(self, value: bool) -> None:
        document = minimal()
        document["layers"]["swc"] = value
        assert refused_leaves(document) == {"layers.swc"}

    def test_a_boolean_is_never_a_number(self) -> None:
        document = minimal()
        document["identifiers"]["swc"]["mean_length"] = True
        assert refused_leaves(document) == {"identifiers.swc.mean_length"}

    def test_an_integral_float_is_accepted_and_normalised(self) -> None:
        document = minimal()
        document["layers"]["swc"] = 2.0
        document["components"]["hubs"] = [3.0]
        result = validate_passport(document)
        assert result["layers"]["swc"] == 2 and type(result["layers"]["swc"]) is int
        assert type(result["components"]["hubs"][0]) is int
        assert type(document["layers"]["swc"]) is float, "the input is not mutated"

    def test_a_number_keeps_its_float(self) -> None:
        result = validate_passport(minimal())
        assert type(result["identifiers"]["swc"]["mean_length"]) is float

    def test_a_fractional_float_is_not_a_count(self) -> None:
        document = minimal()
        document["layers"]["swc"] = 2.5
        assert refused_leaves(document) == {"layers.swc"}

    def test_a_list_item_is_refused_at_its_index(self) -> None:
        document = minimal()
        document["components"]["ports"]["top"] = [1, -1]
        assert refused_leaves(document) == {"components.ports.top.1"}

    def test_a_top_longer_than_eight_is_refused(self) -> None:
        document = minimal()
        document["interfaces"]["fan_out"]["top"] = [1] * 9
        assert refused_leaves(document) == {"interfaces.fan_out.top"}

    def test_a_version_outside_the_pattern_is_refused(self) -> None:
        document = minimal()
        document["passport_version"] = "2.0.0"
        assert refused_leaves(document) == {"passport_version"}

    def test_every_refusal_is_collected(self) -> None:
        document = minimal()
        del document["layers"]["swc"]
        document["connectors"]["links"] = -1
        document["extra"] = 0
        assert refused_leaves(document) == {"layers.swc", "connectors.links", "extra"}

    def test_a_document_that_is_not_an_object_is_refused(self) -> None:
        assert refused_leaves([]) == {"(document)"}

    @pytest.mark.parametrize("text", [".nan", ".inf", "-.inf"])
    def test_a_non_finite_number_from_yaml_is_refused(self, text: str) -> None:
        document = minimal()
        document["identifiers"]["swc"]["mean_length"] = yaml.safe_load(text)
        assert refused_leaves(document) == {"identifiers.swc.mean_length"}

    def test_a_non_finite_count_from_yaml_is_refused(self) -> None:
        document = minimal()
        document["layers"]["swc"] = yaml.safe_load(".inf")
        assert refused_leaves(document) == {"layers.swc"}

    def test_a_non_string_key_from_yaml_is_refused(self) -> None:
        document = minimal()
        document["layers"][1] = 0
        assert refused_leaves(document) == {"layers.1"}


class TestTheKeywordLists:
    @pytest.mark.parametrize(
        ("edit", "message"),
        [
            (lambda s: s["$defs"].update(count=[]), "node /\\$defs/count is not an object"),
            (
                lambda s: s["properties"]["layers"].update(properties=[]),
                "properties is not an object",
            ),
            (lambda s: s["$defs"]["count"].update(type="boolean"), "type at .* is not one"),
            (lambda s: s["properties"]["layers"].update(required="swc"), "required at .* list"),
            (lambda s: s["$defs"]["count"].update(minimum="0"), "minimum at .* not a number"),
            (lambda s: s["$defs"]["count"].update(minimum=True), "minimum at .* not a number"),
            (
                lambda s: s["properties"]["components"]["properties"]["hubs"].update(maxItems="8"),
                "maxItems at .* not a number",
            ),
            (lambda s: s["$defs"]["count"].update(pattern=1), "pattern at .* not a string"),
        ],
    )
    def test_a_malformed_keyword_form_is_refused(self, edit: Callable[[dict[str, Any]], object], message: str) -> None:
        schema = copy.deepcopy(schema_document())
        edit(schema)
        with pytest.raises(PassportSchemaError, match=message):
            _check_schema(schema)

    def test_an_unknown_assertion_keyword_is_refused(self) -> None:
        schema = copy.deepcopy(schema_document())
        schema["$defs"]["count"]["maximum"] = 10
        with pytest.raises(PassportSchemaError, match="maximum"):
            _check_schema(schema)
        assert issubclass(PassportSchemaError, GenerationError)

    def test_an_unknown_keyword_is_refused_on_a_path_no_document_reaches(self) -> None:
        schema = copy.deepcopy(schema_document())
        schema["$defs"]["unused"] = {"type": "string", "minLength": 1}
        with pytest.raises(PassportSchemaError, match="minLength"):
            _check_schema(schema)

    def test_additional_properties_other_than_false_is_refused(self) -> None:
        schema = copy.deepcopy(schema_document())
        schema["properties"]["tasks"]["additionalProperties"] = True
        with pytest.raises(PassportSchemaError, match="additionalProperties"):
            _check_schema(schema)

    def test_a_ref_outside_defs_is_refused(self) -> None:
        schema = copy.deepcopy(schema_document())
        schema["properties"]["layers"]["properties"]["swc"] = {"$ref": "other.json#/x"}
        with pytest.raises(PassportSchemaError, match=r"\$ref"):
            _check_schema(schema)

    def test_the_annotation_keywords_are_ignored(self) -> None:
        schema = copy.deepcopy(schema_document())
        schema["$defs"]["count"]["title"] = "a count"
        schema["$defs"]["count"]["description"] = "never negative"
        schema["properties"]["layers"]["$id"] = "https://example.invalid/layers"
        schema["properties"]["layers"]["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["properties"]["tasks"]["x-passport-version"] = "1.0.0"
        schema["properties"]["tasks"]["x-contract-major"] = 12
        _check_schema(schema)
        interpreter = _Interpreter(schema)
        assert interpreter.validate(schema, minimal(), []) == minimal()
        assert interpreter.refusals == []
        document = minimal()
        document["layers"]["swc"] = -1
        assert interpreter_leaves(document) == {"layers.swc"}


def cross_check_vectors() -> list[tuple[str, object]]:
    """Every SWCraft vector, and the interpreter's own cases jsonschema can judge."""
    vectors: list[tuple[str, object]] = [(path.name, json.loads(load_text(path))) for path in [*EXAMPLES, *NEGATIVES]]

    def mutated(name: str, change: Any) -> None:
        document = minimal()
        change(document)
        vectors.append((name, document))

    mutated("boolean-count", lambda d: d["layers"].update(swc=True))
    mutated("boolean-number", lambda d: d["identifiers"]["swc"].update(mean_length=False))
    mutated("integral-float", lambda d: d["layers"].update(swc=2.0))
    mutated("fractional-count", lambda d: d["layers"].update(swc=2.5))
    mutated("negative-mean", lambda d: d["identifiers"]["swc"].update(mean_length=-0.5))
    mutated("long-top", lambda d: d["components"]["ports"].update(top=[1] * 9))
    mutated("negative-item", lambda d: d["components"].update(hubs=[3, -1]))
    mutated("bad-version", lambda d: d.update(passport_version="1.01.0"))
    mutated("version-as-number", lambda d: d.update(contract_version=12))
    mutated("list-as-object", lambda d: d["components"].update(hubs={}))
    mutated("missing-two", lambda d: (d.pop("tasks"), d["hardware"].pop("cores")))
    mutated("extra-two", lambda d: (d.update(a=1), d["layers"].update(b=2)))
    vectors.append(("not-an-object", [1]))
    return vectors


@pytest.mark.parametrize(("name", "document"), cross_check_vectors(), ids=[n for n, _ in cross_check_vectors()])
def test_jsonschema_agrees_on_every_vector(name: str, document: object) -> None:
    expected = jsonschema_leaves(document)
    if name == "not-an-object":
        expected = {"(document)"} if expected == {""} else expected
    assert interpreter_leaves(document) == expected


class TestNothingPrintedCarriesAStringValue:
    SECRET = "/secret/path/SwcName"

    @pytest.mark.parametrize(
        "place",
        [
            lambda d, v: d["layers"].update(swc=v),
            lambda d, v: d["components"].update(hubs=[v]),
            lambda d, v: d.update(passport_version=v),
            lambda d, v: d.update(contract_version=v),
        ],
    )
    def test_a_string_value_is_never_echoed(self, place: Callable[[dict[str, Any], str], object]) -> None:
        document = minimal()
        place(document, self.SECRET)
        with pytest.raises(PassportRefused) as refused:
            validate_passport(document)
        assert self.SECRET not in str(refused.value)
        assert "secret" not in str(refused.value)

    def test_a_version_string_is_never_echoed_by_the_feasibility_check(self) -> None:
        from swcraft_specimens.specimen.feasibility import check_feasibility

        document = minimal()
        document["passport_version"] = "1.0.0\n"
        document["contract_version"] = "12.0.0-secret"
        text = "\n".join(r.sentence for r in check_feasibility(document))
        assert "secret" not in text


class TestOverlongIntegers:
    def test_an_integer_of_more_digits_than_python_converts_is_a_reading_refusal(self) -> None:
        text = json.dumps(minimal()).replace('"swc": ', '"swc": ' + "9" * 5000 + ', "x": ', 1)
        with pytest.raises(PassportRefused) as refused:
            parse_passport_json(text)
        assert [(r.leaf, r.refusal_class) for r in refused.value.refusals] == [("layers.swc", "reading")]
