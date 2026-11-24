"""The model passport's schema against its vectors, and its two tripwires.

The package's ``model_passport.schema.json`` (Draft 2020-12, hand-authored) is held to the vectors
under ``packages/tools/tests/fixtures/contracts/model-passport/``: every ``examples/`` file validates,
and every ``negative/`` file is refused at the one key path pinned here, so a refusal moving to another
key is a failure rather than a message nobody reads.

The two tripwires are decisions to take, never numbers to update:

- the schema records the model contract major it was written for (``x-contract-major``), and the
  test fails when the running contract's major moves;
- the schema's ``layers`` keys, and the per-layer keys of ``identifiers``, equal the registry's
  layer names, so a new layer fails here and is a schema minor to take.

What this does not reach: the rules the schema cannot state -- a total equal to the sum of its
parts, a list in descending order, a repeated key or NaN in the file -- are the pass's and the
reader's, and are tested with them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT
from jsonschema import Draft202012Validator, ValidationError
from passport_support import PASSPORT_VECTORS_DIR
from swcraft_tools.model_registry import current_registry
from swcraft_tools.passport import PASSPORT_VERSION, schema_text

#: The functional scenarios' schema: a scenario names its ``specimen``, whose committed passport the
#: specimen gate holds to this schema, and no longer carries a passport configuration of its own.
SCENARIO_SCHEMA = REPO_ROOT / "packages" / "mcp" / "tests" / "functional" / "scenario.schema.json"
EXAMPLES = sorted((PASSPORT_VECTORS_DIR / "examples").glob("*.json"), key=lambda p: p.as_posix())
NEGATIVES = sorted((PASSPORT_VECTORS_DIR / "negative").glob("*.json"), key=lambda p: p.as_posix())

#: Every negative vector's refused key path, as a JSON pointer to the offending key.
EXPECTED_POINTERS = {
    "count-as-string.json": "/components/unconnected/total",
    "missing-passport-version.json": "/passport_version",
    "negative-count.json": "/connectors/links",
    "unknown-key.json": "/components/names",
    "unknown-layer-key.json": "/layers/data_types",
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    document = json.loads(schema_text())
    Draft202012Validator.check_schema(document)
    return document


@pytest.fixture(scope="module")
def validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema)


def _pointer(error: ValidationError) -> str:
    """The JSON pointer of the key an error refuses: the missing or unexpected key itself."""
    parts = [str(part) for part in error.absolute_path]
    if error.validator == "required":
        (missing,) = [key for key in error.validator_value if key not in error.instance]
        parts.append(missing)
    elif error.validator == "additionalProperties":
        (extra,) = [key for key in error.instance if key not in error.schema.get("properties", {})]
        parts.append(extra)
    return "".join(f"/{part}" for part in parts)


class TestTheVectors:
    def test_every_directory_holds_vectors(self) -> None:
        assert EXAMPLES and NEGATIVES

    def test_every_negative_has_one_pinned_pointer_and_every_pin_a_vector(self) -> None:
        assert sorted(p.name for p in NEGATIVES) == sorted(EXPECTED_POINTERS)

    @pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
    def test_an_example_validates(self, path: Path, validator: Draft202012Validator) -> None:
        assert [error.message for error in validator.iter_errors(_load(path))] == []

    @pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
    def test_an_example_carries_the_schemas_passport_version(self, path: Path, schema: dict[str, Any]) -> None:
        assert _load(path)["passport_version"] == schema["x-passport-version"]

    @pytest.mark.parametrize("path", NEGATIVES, ids=lambda p: p.name)
    def test_a_negative_is_refused_at_its_key(self, path: Path, validator: Draft202012Validator) -> None:
        pointers = {_pointer(error) for error in validator.iter_errors(_load(path))}
        assert pointers == {EXPECTED_POINTERS[path.name]}


class TestThePackageResource:
    """The schema the wheel carries is the one the pass writes and the specimens' passports follow."""

    def test_it_carries_the_passport_version_the_pass_writes(self) -> None:
        document = json.loads(schema_text())
        assert document["x-passport-version"] == "1.0.0"
        assert document["x-passport-version"] == PASSPORT_VERSION

    def test_a_scenario_names_a_specimen_and_carries_no_configuration(self) -> None:
        # The model's shape is the specimen's committed passport, held to this schema by the specimen
        # gate, so the scenario schema neither carries a configuration nor references the passport schema.
        scenario = _load(SCENARIO_SCHEMA)
        assert "specimen" in scenario["required"]
        assert scenario["properties"]["specimen"]["$ref"] == "#/$defs/name"
        assert {"configuration", "configuration_because"}.isdisjoint(scenario["properties"])
        assert json.loads(schema_text())["$id"] not in SCENARIO_SCHEMA.read_text(encoding="utf-8")


class TestTheShape:
    def test_the_schema_is_closed_at_every_level(self, schema: dict[str, Any]) -> None:
        def walk(node: Any, where: str) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node.get("additionalProperties") is False, where
                for key, value in node.items():
                    walk(value, f"{where}/{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{where}/{index}")

        walk(schema, "")

    def test_the_only_string_leaves_are_the_two_versions(self, schema: dict[str, Any]) -> None:
        strings = sorted(key for key, value in schema["properties"].items() if value.get("type") == "string")
        assert strings == ["contract_version", "passport_version"]

    def test_a_configuration_needs_only_the_controlled_leaves(self, validator: Draft202012Validator) -> None:
        configuration = _load(PASSPORT_VECTORS_DIR / "examples" / "configuration-only-controlled.json")
        assert "ports" not in configuration["components"]
        assert validator.is_valid(configuration)


class TestTheTripwires:
    def test_the_contract_major_is_the_one_the_schema_was_written_for(self, schema: dict[str, Any]) -> None:
        running_major = int(current_registry().contract_version.split(".")[0])
        assert schema["x-contract-major"] == running_major, (
            "the model contract's major moved: decide what the passport schema owes it "
            "(a passport_version bump, a field renamed or added), then move x-contract-major"
        )

    def test_the_layer_keys_are_the_registrys_layer_names(self, schema: dict[str, Any]) -> None:
        registry_layers = set(current_registry().layer_names)
        assert set(schema["properties"]["layers"]["properties"]) == registry_layers
        identifier_layers = set(schema["properties"]["identifiers"]["properties"]) - {"max_length"}
        assert identifier_layers == registry_layers
