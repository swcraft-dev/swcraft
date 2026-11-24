"""The independent oracle of `rule:model.unknown-key`: the committed editor schemas.

The rule and the editor's ``additionalProperties: false`` read one declared key set, ``KindFacts.file_fields``;
this module holds the two to the same answer from the schema's side, through ``jsonschema`` and never through the
rule's own walk.

**The planted oracle.** The corpus is every element file of ``examples/basic``, of the fixture trees of
inter-runnable variables, exclusive areas and data events, the editor schemas' valid elements and
``fixtures/unknown_key_oracle/model`` (one element of each kind none of those reaches). Each document first
validates clean against its layer's committed schema; then ``zz_undeclared`` is planted into every mapping of it,
one at a time:

* where the leaf predicate finds the key, it finds it once, and the schema gives exactly one error naming
  ``zz_undeclared`` alone (directly, or as the only error of exactly one ``oneOf``/``anyOf`` branch, found by that
  property and never by a discriminator), at the predicate's locator; an open kind then gives one finding, and a
  closed kind (``extra="forbid"``) is refused at load;
* where the predicate finds nothing (a ``map`` field's entries, a ``value`` such as ``init``), no schema error
  names it.

Every kind of the registry is planted and found at least once; a kind the corpus misses fails by name (the fix is
a fixture element, never a skip). Every serializer's dict for every corpus element validates clean against the
same schema, so no serializer writes a key ``check-model`` refuses.

**What this does not reach:** each *position* a kind can take; a field the contract and Pydantic both lack (the
schemas are the registry's rendering); and the YAML 1.1 key spellings and merge keys, which are
``test_undeclared_keys.py``'s.
"""

from __future__ import annotations

import copy
import json
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from conftest import REPO_ROOT
from pydantic import TypeAdapter, ValidationError
from swcraft_tools.arxml_import.report import interface_to_dict
from swcraft_tools.editor_schema import artifact_path
from swcraft_tools.model_registry import ModelRegistry, current_registry
from swcraft_tools.model_state import scan_layer
from swcraft_tools.undeclared_keys import undeclared_keys
from swcraft_tools.validator import unknown_key_records
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.writer.yaml_documents import (
    composition_to_dict,
    connector_to_dict,
    data_type_to_dict,
    hardware_to_dict,
    mapping_set_to_dict,
    requirement_to_dict,
    sequence_to_dict,
    swc_to_dict,
    task_to_dict,
)
from swcraft_tools.yaml_load import load_yaml_document

PLANTED_KEY = "zz_undeclared"
PLANTED_VALUE = "planted"

_FIXTURES = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures"
_SCHEMAS = REPO_ROOT / "vscode_extension" / "schemas"

#: The corpus's model roots, by a short label the test ids carry.
CORPUS_ROOTS: dict[str, Path] = {
    "basic": REPO_ROOT / "examples" / "basic" / "model",
    "inter_runnable_variables": _FIXTURES / "inter_runnable_variables" / "model",
    "exclusive_areas": _FIXTURES / "exclusive_areas" / "model",
    "data_events": _FIXTURES / "data_events" / "model",
    "editor_schema_valid": _FIXTURES / "editor_schema" / "valid",
    "unknown_key_oracle": _FIXTURES / "unknown_key_oracle" / "model",
}

#: A position in a constructed document: a mapping's key or a list's index, outermost first.
Position = tuple[str | int, ...]


@dataclass(frozen=True)
class CorpusFile:
    root: str
    layer: str
    storage: str
    text: str

    @property
    def label(self) -> str:
        return f"{self.root}:{self.storage}"


@cache
def _registry() -> ModelRegistry:
    return current_registry()


@cache
def _corpus() -> tuple[CorpusFile, ...]:
    registry = _registry()
    files: list[CorpusFile] = []
    for label, root in CORPUS_ROOTS.items():
        for layer in registry.layer_names:
            for entry in scan_layer(root, layer, registry=registry).files:
                files.append(
                    CorpusFile(
                        root=label,
                        layer=layer,
                        storage=entry.path.relative_to(root).as_posix(),
                        text=entry.path.read_text(encoding="utf-8"),
                    )
                )
    return tuple(files)


@cache
def _validator(layer: str) -> jsonschema.Draft7Validator:
    path = _SCHEMAS / artifact_path(_registry().layer(layer))
    return jsonschema.Draft7Validator(json.loads(path.read_text(encoding="utf-8")))


@cache
def _adapter(layer: str) -> TypeAdapter[Any]:
    return TypeAdapter(_registry().layer(layer).element_annotation)


def _is_closed(kind: str) -> bool:
    return _registry().kind(kind).model.model_config.get("extra") == "forbid"


def _mappings(value: Any, position: Position = ()) -> Iterator[Position]:
    """Every mapping of a constructed document, outermost first, in document order."""
    if isinstance(value, dict):
        yield position
        for key, item in value.items():
            yield from _mappings(item, (*position, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _mappings(item, (*position, index))


def _planted(document: Any, position: Position) -> Any:
    planted = copy.deepcopy(document)
    target = planted
    for step in position:
        target = target[step]
    target[PLANTED_KEY] = PLANTED_VALUE
    return planted


def _names_only_the_planted_key(error: jsonschema.ValidationError) -> bool:
    if error.validator != "additionalProperties" or not isinstance(error.instance, dict):
        return False
    declared = error.schema.get("properties", {})
    return [key for key in error.instance if key not in declared] == [PLANTED_KEY]


def _located(errors: list[jsonschema.ValidationError]) -> Position | None:
    """The position of the planted key when *errors* are exactly its one error, else ``None``.

    One ``additionalProperties`` error naming the planted key alone gives its
    absolute path (its parents' paths joined). One ``oneOf`` error -- or one
    ``anyOf`` error, the rendering of an optional ``element_kind`` field as the kind
    or ``null`` -- gives the position its one such branch gives: the branches are
    grouped by the first step of each context error's schema path, and exactly one
    must locate the key.
    """
    if len(errors) != 1:
        return None
    (error,) = errors
    if _names_only_the_planted_key(error):
        return tuple(error.absolute_path)
    if error.validator not in ("oneOf", "anyOf"):
        return None
    branches: dict[Any, list[jsonschema.ValidationError]] = defaultdict(list)
    for inner in error.context or ():
        branches[inner.relative_schema_path[0]].append(inner)
    found = [position for position in map(_located, branches.values()) if position is not None]
    return found[0] if len(found) == 1 else None


def _names_the_planted_key_anywhere(errors: list[jsonschema.ValidationError]) -> bool:
    for error in errors:
        if error.validator == "additionalProperties" and isinstance(error.instance, dict):
            if PLANTED_KEY in error.instance and PLANTED_KEY not in error.schema.get("properties", {}):
                return True
        if _names_the_planted_key_anywhere(list(error.context or ())):
            return True
    return False


def _locator_position(locator: tuple[tuple[str, str | int], ...]) -> Position:
    return tuple(name for _, name in locator)


@dataclass(frozen=True)
class Planting:
    """What one planting gave: the predicate's kind there (``None`` when it found nothing), or a problem."""

    position: Position
    kind: str | None
    problem: str | None


def _plant_one(file: CorpusFile, document: Any, position: Position) -> Planting:
    planted = _planted(document, position)
    text = yaml.safe_dump(planted, default_flow_style=False, sort_keys=False, allow_unicode=True)
    reread = load_yaml_document(text)
    errors = sorted(_validator(file.layer).iter_errors(reread), key=lambda error: list(error.absolute_path).__repr__())
    found = [key for key in undeclared_keys(text, reread, file.layer, registry=_registry()) if key.key == PLANTED_KEY]
    if not found:
        if _names_the_planted_key_anywhere(errors):
            return Planting(position, None, "the schema refuses the key where the predicate finds none")
        return Planting(position, None, None)
    if len(found) != 1:
        return Planting(position, None, f"the predicate finds the key {len(found)} times")
    (key,) = found
    located = _located(errors)
    if located is None:
        messages = [error.message for error in errors]
        return Planting(position, key.kind, f"the schema does not locate exactly one planted key: {messages}")
    if located != position or _locator_position(key.locator) != position:
        return Planting(
            position, key.kind, f"schema at {list(located)}, predicate at {list(_locator_position(key.locator))}"
        )
    if _is_closed(key.kind):
        try:
            _adapter(file.layer).validate_python(reread)
        except ValidationError:
            return Planting(position, key.kind, None)
        return Planting(position, key.kind, f"closed kind `{key.kind}` loads the planted key")
    element = _adapter(file.layer).validate_python(reread)
    records = unknown_key_records({file.layer: [(element, text)]}, registry=_registry())
    if len(records) != 1:
        return Planting(position, key.kind, f"the rule gives {len(records)} findings")
    (subject,) = records[0].subjects
    last = subject.path[-1].to_json() if subject.path else None
    if subject.layer != file.layer or last != {"key": PLANTED_KEY}:
        return Planting(position, key.kind, f"the finding's subject ends at {last} in layer {subject.layer}")
    return Planting(position, key.kind, None)


@cache
def _plantings(file: CorpusFile) -> tuple[Planting, ...]:
    document = load_yaml_document(file.text)
    return tuple(_plant_one(file, document, position) for position in _mappings(document))


def _corpus_ids() -> list[str]:
    return [file.label for file in _corpus()]


class TestTheCorpus:
    def test_every_corpus_root_holds_element_files(self) -> None:
        held = {file.root for file in _corpus()}
        assert held == set(CORPUS_ROOTS)

    @pytest.mark.parametrize("file", _corpus(), ids=_corpus_ids())
    def test_the_unplanted_file_validates_clean_against_its_committed_schema(self, file: CorpusFile) -> None:
        errors = [error.message for error in _validator(file.layer).iter_errors(load_yaml_document(file.text))]
        assert errors == []

    @pytest.mark.parametrize("file", _corpus(), ids=_corpus_ids())
    def test_the_unplanted_file_holds_no_undeclared_key(self, file: CorpusFile) -> None:
        assert undeclared_keys(file.text, load_yaml_document(file.text), file.layer, registry=_registry()) == ()


class TestThePlantedKey:
    @pytest.mark.parametrize("file", _corpus(), ids=_corpus_ids())
    def test_the_rule_and_the_schema_agree_at_every_mapping(self, file: CorpusFile) -> None:
        plantings = _plantings(file)
        assert plantings, "every element file is a mapping"
        problems = [f"at {list(planting.position)}: {planting.problem}" for planting in plantings if planting.problem]
        assert problems == []

    def test_every_kind_of_the_registry_is_planted_and_found(self) -> None:
        found = {planting.kind for file in _corpus() for planting in _plantings(file) if planting.kind}
        missed = sorted(kind.kind for kind in _registry().kinds.values() if kind.kind not in found)
        assert missed == [], "add a fixture element of each to fixtures/unknown_key_oracle/model"

    def test_every_closed_kind_is_planted_and_refused_at_load(self) -> None:
        closed = sorted(kind.kind for kind in _registry().kinds.values() if _is_closed(kind.kind))
        assert closed, "the closed kinds are read off the registry's bound classes"
        refused = {
            planting.kind
            for file in _corpus()
            for planting in _plantings(file)
            if planting.kind and _is_closed(planting.kind) and planting.problem is None
        }
        assert sorted(set(closed) - refused) == []


# ---------------------------------------------------------------------------
# Every serializer writes declared keys only
# ---------------------------------------------------------------------------


def _data_type_layers() -> set[str]:
    return {"application_types", "implementation_types", "base_types"}


#: The hand-built serializers by layer, beside the derived one every layer has.
_HAND_BUILT: dict[str, tuple[Callable[[Any], dict[str, Any]], ...]] = {
    "swc": (swc_to_dict,),
    "interfaces": (interface_to_dict,),
    "connectors": (connector_to_dict,),
    "requirements": (requirement_to_dict,),
    "compositions": (composition_to_dict,),
    "tasks": (task_to_dict,),
    "sequences": (sequence_to_dict,),
    "hardware": (hardware_to_dict,),
    "mapping_sets": (mapping_set_to_dict,),
    **dict.fromkeys(_data_type_layers(), (data_type_to_dict,)),
}


def _serializers(layer: str) -> list[tuple[str, Callable[[Any], dict[str, Any]]]]:
    return [("element_to_dict", element_to_dict)] + [
        (serializer.__name__, serializer) for serializer in _HAND_BUILT.get(layer, ())
    ]


class TestTheSerializersWriteDeclaredKeysOnly:
    def test_every_layer_has_a_corpus_file(self) -> None:
        assert {file.layer for file in _corpus()} == set(_registry().layer_names)

    @pytest.mark.parametrize("file", _corpus(), ids=_corpus_ids())
    def test_every_serializer_s_dict_validates_clean(self, file: CorpusFile) -> None:
        element = _adapter(file.layer).validate_python(load_yaml_document(file.text))
        problems: list[str] = []
        for name, serializer in _serializers(file.layer):
            # Dumped and read back as a file would be, so the schema sees what a file holds.
            text = yaml.safe_dump(serializer(element), default_flow_style=False, sort_keys=False, allow_unicode=True)
            reread = load_yaml_document(text)
            problems += [f"{name}: {error.message}" for error in _validator(file.layer).iter_errors(reread)]
        assert problems == []
