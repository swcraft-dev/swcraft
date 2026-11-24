"""The needs mapping file: the reader against its generated editor schema and the vectors.

``swcraft_tools.requirements_mapping`` reads the file ``import requirements
--mapping`` takes; ``editor_schema.mapping_schema`` renders its editor schema
from the same Pydantic class. Both are held to the vectors under
``packages/tools/tests/fixtures/contracts/requirements-interchange/mapping/``: every example is read and
accepted by the schema; every negative is refused by the reader at its pinned
YAML key path with its pinned sentence, and by the schema where JSON Schema can
state the rule. The rules the schema cannot state -- a key written twice, a
blank name, a key both read and ignored, the names a mapping may not use -- are
asserted as an asymmetry: the schema accepts those files.

What this does not reach: where the mapping lies (the command refuses one
inside the model root) and what the needs reader does with a mapping; both are
the import's tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT
from jsonschema import Draft7Validator
from swcraft_tools import requirements_mapping
from swcraft_tools.editor_schema import mapping_schema
from swcraft_tools.requirements_mapping import (
    DEFAULT_MAPPING,
    FORBIDDEN_NAMES,
    FieldSources,
    MappingError,
    NeedsMapping,
    parse_mapping_text,
    read_mapping,
)
from swcraft_tools.yaml_load import load_yaml_document

MAPPING_DIR = (
    REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "contracts" / "requirements-interchange" / "mapping"
)
EXAMPLES = sorted((MAPPING_DIR / "examples").glob("*.yaml"), key=lambda p: p.as_posix())
NEGATIVES = sorted((MAPPING_DIR / "negative").glob("*.yaml"), key=lambda p: p.as_posix())

#: Every negative's refusal: the YAML key path (``None`` for the file itself) and the sentence.
EXPECTED_REFUSALS: dict[str, tuple[str | None, str]] = {
    "blank-value.yaml": ("fields/asil", "must not be blank"),
    "duplicate-key.yaml": ("types/sysreq", "the key is written twice in one mapping"),
    "empty.yaml": (None, "the mapping file is empty: a mapping that says nothing is no mapping"),
    "field-id.yaml": (
        "fields/id",
        "'id' cannot be mapped: a need's id is its identity, read from the key 'id' and never translated",
    ),
    "field-reads-version.yaml": (
        "fields/revision",
        "'version' cannot be named here: SWCraft keeps its own change counter, and a need's 'version' is refused",
    ),
    "field-type.yaml": (
        "fields/type",
        "'type' cannot be mapped: a need's type is read from the key 'type' and mapped under 'types'",
    ),
    "ignore-backlink.yaml": (
        "ignore/1",
        "'parent_back' cannot be named here: a key ending in '_back' is the build's mirror of a link and never "
        "a field; list the link's own name instead",
    ),
    "ignore-read-key.yaml": ("ignore/1", "'parent' is both read and ignored"),
    "ignore-source.yaml": (
        "ignore/0",
        "'source' cannot be named here: every imported requirement is written 'source: external', and a "
        "need's 'source' is refused",
    ),
    "not-utf8.yaml": (None, "not UTF-8 text (invalid continuation byte at byte 26)"),
    "removed-status-empty.yaml": (
        "removed_status",
        "must not be empty: list a status the team never uses to mean that no status removes",
    ),
    "removed-status-repeat.yaml": ("removed_status/2", "'rejected' is listed twice"),
    "type-mapped-to-requirement.yaml": ("types/sysreq", "must be req, spec or ignore"),
    "types-empty.yaml": ("types", "must not be empty: an empty list of types would refuse every need"),
    "unknown-field.yaml": (
        "fields/parents",
        "unknown key: 'fields' holds only title, description, asil, document, revision, derived_from, status",
    ),
    "unknown-top-level-key.yaml": ("type", "unknown key: the mapping holds only types, fields, removed_status, ignore"),
}

#: The negatives the generated schema accepts: rules only the reader holds.
READER_ONLY = frozenset(
    {
        "blank-value.yaml",  # blank is not empty; the schema states no blank rule
        "duplicate-key.yaml",  # a YAML loader keeps the last of two keys
        "field-reads-version.yaml",  # a name a mapping may not use
        "ignore-backlink.yaml",
        "ignore-source.yaml",
        "ignore-read-key.yaml",  # both read and ignored: a rule across two keys
    }
)

#: The negatives no schema can judge: the file is no YAML value at all.
NOT_A_DOCUMENT = frozenset({"not-utf8.yaml"})


@pytest.fixture(scope="module")
def validator() -> Draft7Validator:
    schema = mapping_schema()
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def _document(path: Path) -> Any:
    return load_yaml_document(path.read_text(encoding="utf-8"))


def _example(name: str) -> NeedsMapping:
    return read_mapping(MAPPING_DIR / "examples" / name)


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


def test_the_vector_directories_hold_what_this_module_pins() -> None:
    assert [path.name for path in EXAMPLES] == [
        "description-from-title.yaml",
        "fields-only.yaml",
        "ignore-list-key.yaml",
        "team.needs-mapping.yaml",
        "types-only.yaml",
    ]
    assert {path.name for path in NEGATIVES} == set(EXPECTED_REFUSALS)


def test_the_team_example_is_read_as_written() -> None:
    mapping = _example("team.needs-mapping.yaml")
    assert mapping.types == {"sysreq": "req", "swreq": "req", "spec": "spec", "test": "ignore"}
    assert mapping.fields == FieldSources(
        title="title",
        description="content",
        asil="safety",
        derived_from="parent",
        document="docname",
        revision="rev",
        status="state",
    )
    assert mapping.removed_status == ["rejected", "obsolete"]
    assert mapping.ignore == ["verifies", "component", "reviewer"]
    assert mapping.read_keys() == ("title", "content", "safety", "docname", "rev", "parent", "state")


def test_a_types_only_mapping_keeps_every_default_source() -> None:
    mapping = _example("types-only.yaml")
    assert mapping.types == {"sysreq": "req", "test": "ignore"}, "the types are the whole list: no req added"
    assert mapping.fields == DEFAULT_MAPPING.fields
    assert mapping.removed_status == ["rejected"]
    assert mapping.ignore == []


def test_a_fields_only_mapping_keeps_the_default_types() -> None:
    mapping = _example("fields-only.yaml")
    assert mapping.types == {"req": "req", "spec": "spec"}
    assert (mapping.fields.asil, mapping.fields.derived_from) == ("safety", "parent")
    assert mapping.fields.title == "title" and mapping.fields.description == "content"


def test_two_fields_may_read_one_key() -> None:
    mapping = _example("description-from-title.yaml")
    assert mapping.fields.title == mapping.fields.description == "title"
    assert mapping.read_keys().count("title") == 1, "a key read twice is one read key"
    assert "content" not in mapping.read_keys()


def test_a_list_key_is_ignored_by_its_forward_name() -> None:
    assert _example("ignore-list-key.yaml").ignore == ["verifies"]


def test_the_default_mapping_is_swcraft_s_own_names() -> None:
    """With no mapping, SWCraft's own names apply."""
    assert DEFAULT_MAPPING.types == {"req": "req", "spec": "spec"}
    assert DEFAULT_MAPPING.fields.model_dump() == {
        "title": "title",
        "description": "content",
        "asil": "asil",
        "document": "document",
        "revision": "revision",
        "derived_from": "derived_from",
        "status": "status",
    }
    assert DEFAULT_MAPPING.removed_status == ["rejected"]
    assert DEFAULT_MAPPING.ignore == []
    assert NeedsMapping() == DEFAULT_MAPPING


def test_the_mapping_is_frozen() -> None:
    with pytest.raises(Exception, match="frozen"):
        DEFAULT_MAPPING.ignore = ["x"]  # type: ignore[misc]


def test_a_leading_byte_order_mark_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "bom.needs-mapping.yaml"
    path.write_bytes(b"\xef\xbb\xbfignore: [verifies]\n")
    assert read_mapping(path).ignore == ["verifies"]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.name)
def test_the_schema_accepts_every_example(path: Path, validator: Draft7Validator) -> None:
    assert list(validator.iter_errors(_document(path))) == []


def _descriptions(node: Any, at: str) -> list[str]:
    """Every property under *node* that carries no description, by its path."""
    missing: list[str] = []
    for name, prop in node.get("properties", {}).items():
        if not prop.get("description"):
            missing.append(f"{at}/{name}")
    return missing


def test_every_key_of_the_schema_has_hover_text() -> None:
    schema = mapping_schema()
    assert schema["description"]
    missing = _descriptions(schema, "")
    for name, definition in schema.get("definitions", {}).items():
        assert definition.get("description"), name
        missing.extend(_descriptions(definition, name))
    assert missing == []
    assert list(schema["properties"]) == list(NeedsMapping.model_fields)
    assert list(schema["definitions"]["FieldSources"]["properties"]) == list(FieldSources.model_fields)


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", NEGATIVES, ids=lambda path: path.name)
def test_the_reader_refuses_every_negative_at_its_pinned_path(path: Path) -> None:
    expected_path, expected_sentence = EXPECTED_REFUSALS[path.name]
    with pytest.raises(MappingError) as caught:
        read_mapping(path)
    assert (caught.value.path, caught.value.sentence) == (expected_path, expected_sentence)


@pytest.mark.parametrize(
    "path",
    [path for path in NEGATIVES if path.name not in NOT_A_DOCUMENT],
    ids=lambda path: path.name,
)
def test_the_schema_judges_each_negative_it_can_state(path: Path, validator: Draft7Validator) -> None:
    errors = list(validator.iter_errors(_document(path)))
    if path.name in READER_ONLY:
        assert errors == [], "the asymmetry: a rule only the reader holds"
    else:
        assert errors != [], "a rule JSON Schema states is refused by both"


@pytest.mark.parametrize("name", FORBIDDEN_NAMES)
def test_every_forbidden_name_is_refused_as_a_source_and_as_an_ignored_key(name: str) -> None:
    with pytest.raises(MappingError) as as_source:
        parse_mapping_text(f"fields:\n  asil: {name}\n")
    assert as_source.value.path == "fields/asil"
    assert as_source.value.sentence.startswith(f"'{name}' cannot be named here: ")
    with pytest.raises(MappingError) as as_ignored:
        parse_mapping_text(f"ignore: [{name}]\n")
    assert as_ignored.value.path == "ignore/0"


def test_a_default_source_not_remapped_is_read_and_cannot_be_ignored() -> None:
    with pytest.raises(MappingError) as caught:
        parse_mapping_text("ignore: [content]\n")
    assert (caught.value.path, caught.value.sentence) == ("ignore/0", "'content' is both read and ignored")
    # Remapped away, the default source is no longer read and may be ignored.
    assert parse_mapping_text("fields:\n  description: title\nignore: [content]\n").ignore == ["content"]


#: The refusal of a key YAML reads as no string, after the value it names.
_NOT_A_STRING_KEY = (
    "is not a string: quote it, since YAML reads on, off, yes, no, true, false, null and a number as no string"
)


@pytest.mark.parametrize(
    ("text", "path", "sentence"),
    [
        ("- types\n", None, "the mapping file must be a mapping of keys to values"),
        ("types: [\n", None, "not a YAML document (did not find expected node content)"),
        ("types:\n", "types", "must be a mapping of keys to values"),
        ("fields: [asil]\n", "fields", "must be a mapping of keys to values"),
        ("ignore: verifies\n", "ignore", "must be a list"),
        ("types:\n  1: req\n", "types", f"the key read as 1 {_NOT_A_STRING_KEY}"),
        ("types:\n  on: req\n", "types", f"the key read as true {_NOT_A_STRING_KEY}"),
        ("types:\n  no: req\n", "types", f"the key read as false {_NOT_A_STRING_KEY}"),
        ("types:\n  null: req\n", "types", f"the key read as null {_NOT_A_STRING_KEY}"),
        ("types:\n  ' ': req\n", "types/ ", "must not be blank"),
        ("types:\n  test: no\n", "types/test", "must be req, spec or ignore"),
        ("removed_status: [1]\n", "removed_status/0", "must be a string"),
        ("fields:\n  asil: x\n  asil: y\n", "fields/asil", "the key is written twice in one mapping"),
    ],
)
def test_a_wrong_shape_is_refused_at_its_key_path(text: str, path: str | None, sentence: str) -> None:
    with pytest.raises(MappingError) as caught:
        parse_mapping_text(text)
    assert (caught.value.path, caught.value.sentence) == (path, sentence)


_ALIAS = "an alias is not read in a mapping file: write the value out where it is used"


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ("ignore: &x [*x]\n", "ignore/0"),
        ("fields: &x {title: *x}\n", "fields/title"),
        ("removed_status: &s [obsolete]\nignore: *s\n", "ignore"),
        ("removed_status: [&s obsolete]\nignore: [*s]\n", "ignore/0"),
        ("types:\n  &k req: req\nfields:\n  *k : title\n", "fields/req"),
        ("removed_status: [x]\nfields: &f {title: t}\ntypes: {<<: *f}\n", "types/<<"),
    ],
    ids=["a-list-holding-itself", "a-mapping-holding-itself", "a-repeat", "a-scalar", "a-key", "a-merge-key"],
)
def test_an_alias_is_refused_where_it_stands(text: str, path: str) -> None:
    with pytest.raises(MappingError) as caught:
        parse_mapping_text(text)
    assert (caught.value.path, caught.value.sentence) == (path, _ALIAS)


def test_a_chain_of_merge_keys_is_refused_before_it_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    # Eight merges on each of twelve levels: loaded, the innermost pair would be copied 8**11 times.
    lines = ["a0: &a0 {k: x}"]
    lines += [f"a{level}: &a{level} {{<<: [{', '.join([f'*a{level - 1}'] * 8)}]}}" for level in range(1, 12)]
    text = "\n".join(lines) + "\n"

    def never(_: str) -> object:
        raise AssertionError("the mapping was loaded")

    monkeypatch.setattr(requirements_mapping, "load_yaml_document", never)
    with pytest.raises(MappingError) as caught:
        parse_mapping_text(text)
    assert (caught.value.path, caught.value.sentence) == ("a1/<</0", _ALIAS)


def test_a_merge_key_written_in_place_and_an_anchor_nothing_names_are_read() -> None:
    assert parse_mapping_text("fields: {<<: {title: summary}}\n").fields.title == "summary"
    assert parse_mapping_text("ignore: &a [x]\n").ignore == ["x"]


def test_an_unreadable_file_is_refused_without_a_path(tmp_path: Path) -> None:
    with pytest.raises(MappingError) as caught:
        read_mapping(tmp_path / "absent.needs-mapping.yaml")
    assert caught.value.path is None
    assert caught.value.sentence.startswith("cannot read the file (")


def test_the_refusal_text_is_the_path_then_the_sentence() -> None:
    """The command prints ``Error: <mapping as spelled>: <path>: why``; this is the ``<path>: why``."""
    assert str(MappingError("fields/asil", "must not be blank")) == "fields/asil: must not be blank"
    assert str(MappingError(None, "the mapping file is empty")) == "the mapping file is empty"
