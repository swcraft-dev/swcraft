"""The needs mapping file: how a requirements team's needs map to SWCraft's requirement fields.

``swcraft import requirements --mapping FILE`` reads one such file, a YAML file
of the user's own, never a key of ``swcraft.yaml``. :func:`read_mapping` turns it
into a :class:`NeedsMapping` or one :class:`MappingError` naming the YAML key
path it refuses (``fields/asil``, ``ignore/2``) and why. With no file,
:data:`DEFAULT_MAPPING` (SWCraft's own names) applies.

**The form.** Four top-level keys, each optional:

- ``types`` maps the team's need type to ``req``, ``spec`` or ``ignore``; absent,
  ``req`` and ``spec`` map to themselves, present, it is the whole list.
- ``fields`` names, per SWCraft field, the need key it is read from
  (:class:`FieldSources`); a field left out keeps its default source.
- ``removed_status``: the statuses that remove a requirement, ``[rejected]`` when absent.
- ``ignore``: the team's own need keys SWCraft does not hold.

**What it refuses.** An unknown key at any level, a wrong type, a blank name, an
empty ``types`` or ``removed_status``, a name listed twice, a ``fields`` value or
an ``ignore`` entry naming ``id``, ``type``, ``version`` or ``source`` or ending in
``_back``, a key both read and ignored, a key YAML reads as no string (``on``,
``yes``, a number), an alias anywhere, a key written twice in one mapping, and a
file that cannot be read, is not UTF-8 or is not YAML. The generated editor schema
does not state the duplicate key, the blank name, the read-and-ignored key or the
forbidden names.

It does not judge where the file lies (a mapping inside the model root is the
command's refusal) nor whether a name occurs in any needs file, and a key a merge
key (``<<``) brings in is not compared with the keys beside it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from swcraft_tools.yaml_load import compose_yaml_document, load_yaml_document

__all__ = [
    "BACKLINK_SUFFIX",
    "DEFAULT_MAPPING",
    "FORBIDDEN_NAMES",
    "FieldSources",
    "MappingError",
    "NeedsMapping",
    "parse_mapping_text",
    "read_mapping",
]

#: Why each forbidden name cannot be used, as the refusal says it.
_FORBIDDEN_REASONS = {
    "id": "a need's id is its identity, read from the key 'id' and never translated",
    "type": "a need's type is read from the key 'type' and mapped under 'types'",
    "version": "SWCraft keeps its own change counter, and a need's 'version' is refused",
    "source": "every imported requirement is written 'source: external', and a need's 'source' is refused",
}

#: The need keys a mapping may neither read a field from nor ignore.
FORBIDDEN_NAMES: tuple[str, ...] = tuple(_FORBIDDEN_REASONS)

#: The suffix of a backlink key: the build's mirror of a link, never a field.
BACKLINK_SUFFIX = "_back"


class MappingError(ValueError):
    """One refusal of the mapping file: the YAML key path (``None`` for the file itself) and why."""

    def __init__(self, path: str | None, sentence: str) -> None:
        self.path = path
        self.sentence = sentence
        super().__init__(sentence if path is None else f"{path or '(top level)'}: {sentence}")


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _usable_key(value: str) -> str:
    """A need key a mapping may read or ignore: not blank, not a forbidden name, not a backlink."""
    _not_blank(value)
    if value in _FORBIDDEN_REASONS:
        raise ValueError(f"'{value}' cannot be named here: {_FORBIDDEN_REASONS[value]}")
    if value.endswith(BACKLINK_SUFFIX):
        raise ValueError(
            f"'{value}' cannot be named here: a key ending in '{BACKLINK_SUFFIX}' is the build's mirror "
            "of a link and never a field; list the link's own name instead"
        )
    return value


_Name = Annotated[str, AfterValidator(_not_blank)]
_NeedKey = Annotated[str, AfterValidator(_usable_key)]


class FieldSources(BaseModel):
    """Per SWCraft field, the key of the team's need it is read from."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    title: _NeedKey = Field(default="title", description="The need key the requirement's `title` is read from.")
    description: _NeedKey = Field(
        default="content",
        description=(
            "The need key the requirement's `description` is read from: Sphinx-Needs writes the directive "
            "body as `content`, taken as written."
        ),
    )
    asil: _NeedKey = Field(default="asil", description="The need key the requirement's `asil` is read from.")
    document: _NeedKey = Field(
        default="document", description="The need key the requirement's `document` is read from."
    )
    revision: _NeedKey = Field(
        default="revision",
        description="The need key the requirement's `revision` (the source tool's own revision) is read from.",
    )
    derived_from: _NeedKey = Field(
        default="derived_from",
        description=(
            "The need key the requirement's parents are read from. A link field's part and condition are "
            'dropped, so `REQ-1.p1[status=="open"]` names `REQ-1`.'
        ),
    )
    status: _NeedKey = Field(
        default="status",
        description="The need key the status is read from. It is not stored; it decides removal (`removed_status`).",
    )

    @model_validator(mode="before")
    @classmethod
    def _id_and_type_are_not_fields(cls, data: Any) -> Any:
        # Before the model, so the reason is named rather than "unknown key".
        if isinstance(data, dict):
            for name in ("id", "type"):
                if name in data:
                    raise _PlacedError(name, f"'{name}' cannot be mapped: {_FORBIDDEN_REASONS[name]}")
        return data


class _PlacedError(ValueError):
    """A refusal placed on the key path it names, below the location Pydantic reports it at."""

    def __init__(self, path: str, sentence: str) -> None:
        self.path = path
        super().__init__(sentence)


def _raise_placed_repeat(key: str, entries: list[str]) -> None:
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if entry in seen:
            raise _PlacedError(f"{key}/{index}", f"'{entry}' is listed twice")
        seen.add(entry)


_TypeTarget = Literal["req", "spec", "ignore"]


def _default_types() -> dict[str, _TypeTarget]:
    """The type map of a mapping file that names none: `req` and `spec` map to themselves."""
    return {"req": "req", "spec": "spec"}


class NeedsMapping(BaseModel):
    """How the requirements team's needs map to SWCraft's requirements (``--mapping FILE``)."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    types: dict[_Name, _TypeTarget] = Field(
        default_factory=_default_types,
        description=(
            "The team's need type (the directive name, the need's `type`) mapped to `req`, `spec` or `ignore`. "
            "A need of an `ignore` type is not imported. When present, this is the whole list: a type it "
            "does not name is refused. When absent, `req` and `spec` map to themselves."
        ),
        json_schema_extra={"minProperties": 1},
    )
    fields: FieldSources = Field(
        default_factory=FieldSources,
        description=(
            "Per SWCraft field, the key of the team's need it is read from. A field left out keeps its "
            "default source. The id and the type are never mapped."
        ),
    )
    removed_status: list[_Name] = Field(
        default_factory=lambda: ["rejected"],
        description=(
            "The statuses that remove a requirement from the model, each listed once. When absent, `rejected` alone."
        ),
        json_schema_extra={"minItems": 1, "uniqueItems": True},
    )
    ignore: list[_NeedKey] = Field(
        default_factory=list,
        description=(
            "The team's own need keys SWCraft does not hold, each listed once. Listing a link type ignores "
            "its `_back` mirror with it. Any other key SWCraft does not read is refused."
        ),
        json_schema_extra={"uniqueItems": True},
    )

    @model_validator(mode="after")
    def _the_lists_hold_sense(self) -> NeedsMapping:
        if not self.types:
            raise _PlacedError("types", "must not be empty: an empty list of types would refuse every need")
        if not self.removed_status:
            raise _PlacedError(
                "removed_status",
                "must not be empty: list a status the team never uses to mean that no status removes",
            )
        _raise_placed_repeat("removed_status", self.removed_status)
        _raise_placed_repeat("ignore", self.ignore)
        read = set(self.read_keys())
        for index, key in enumerate(self.ignore):
            if key in read:
                raise _PlacedError(f"ignore/{index}", f"'{key}' is both read and ignored")
        return self

    def read_keys(self) -> tuple[str, ...]:
        """The need keys the mapped fields are read from, in :class:`FieldSources` order, each once."""
        # Annotated, so the field oracle reads the dump as no model element's.
        sources: FieldSources = self.fields
        return tuple(dict.fromkeys(sources.model_dump().values()))


#: The mapping SWCraft's own names make: what an import with no ``--mapping`` reads through.
DEFAULT_MAPPING = NeedsMapping()


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _keys(model: type[NeedsMapping | FieldSources]) -> tuple[str, ...]:
    """A mapping model's keys, in declaration order."""
    return tuple(model.model_fields)


#: The keys each level holds, named by an unknown-key refusal.
_LEVEL_KEYS: dict[tuple[int | str, ...], tuple[str, tuple[str, ...]]] = {
    (): ("the mapping", _keys(NeedsMapping)),
    ("fields",): ("'fields'", _keys(FieldSources)),
}

#: Pydantic's type errors in YAML's words.
_TYPE_SENTENCES = {
    "model_type": "must be a mapping of keys to values",
    "dict_type": "must be a mapping of keys to values",
    "list_type": "must be a list",
    "string_type": "must be a string",
    "literal_error": "must be req, spec or ignore",
}

#: The segment Pydantic adds to a location when a mapping's key, not its value, is refused.
_KEY_SEGMENT = "[key]"


def _path(*segments: object) -> str:
    return "/".join(str(segment) for segment in segments)


def _from_validation_error(exc: ValidationError) -> MappingError:
    """The first Pydantic error as a refusal at its YAML key path."""
    error = exc.errors(include_url=False)[0]
    loc = tuple(segment for segment in error["loc"] if segment != _KEY_SEGMENT)
    cause = error.get("ctx", {}).get("error")
    if isinstance(cause, _PlacedError):
        return MappingError(_path(*loc, cause.path), str(cause))
    if len(loc) < len(error["loc"]) and error["type"] == "string_type":
        # Pydantic names a non-string key by its value, so the refusal goes on the holding mapping.
        return MappingError(
            _path(*loc[:-1]),
            f"the key read as {_yaml_scalar(error['input'])} is not a string: quote it, "
            "since YAML reads on, off, yes, no, true, false, null and a number as no string",
        )
    if error["type"] == "extra_forbidden":
        level, keys = _LEVEL_KEYS[tuple(loc[:-1])]
        return MappingError(_path(*loc), f"unknown key: {level} holds only {', '.join(keys)}")
    if error["type"] == "value_error":
        return MappingError(_path(*loc), str(cause))
    return MappingError(_path(*loc), _TYPE_SENTENCES.get(error["type"], error["msg"]))


def _yaml_scalar(value: object) -> str:
    """A scalar as YAML spells it back: ``true``, ``null``, ``3``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return "null" if value is None else str(value)


#: The refusal of an alias: a chain of merge keys (``<<``) fed by aliases grows exponentially.
_ALIAS_SENTENCE = "an alias is not read in a mapping file: write the value out where it is used"


def _refuse_aliases_and_duplicate_keys(
    node: yaml.Node | None, *segments: object, visited: set[int] | None = None
) -> None:
    """Raise at the first alias or the first key written twice in one YAML mapping, depth first in file order.

    libyaml composes an alias to its anchor's own node, so an alias is a node met
    a second time (*visited*, keys included); refusing it before construction
    bounds the load to the text's size.
    """
    visited = set() if visited is None else visited
    if node is None:
        return
    if id(node) in visited:
        raise MappingError(_path(*segments) or None, _ALIAS_SENTENCE)
    visited.add(id(node))
    if isinstance(node, yaml.MappingNode):
        seen: set[tuple[str, str]] = set()
        for key, value in node.value:
            segment = key.value if isinstance(key, yaml.ScalarNode) else "?"
            if id(key) in visited:
                raise MappingError(_path(*segments, segment), _ALIAS_SENTENCE)
            visited.add(id(key))
            if isinstance(key, yaml.ScalarNode):
                if (key.tag, key.value) in seen:
                    raise MappingError(_path(*segments, key.value), "the key is written twice in one mapping")
                seen.add((key.tag, key.value))
            _refuse_aliases_and_duplicate_keys(value, *segments, segment, visited=visited)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            _refuse_aliases_and_duplicate_keys(item, *segments, index, visited=visited)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def parse_mapping_text(text: str) -> NeedsMapping:
    """The mapping one YAML text declares, or the :class:`MappingError` refusing it."""
    try:
        _refuse_aliases_and_duplicate_keys(compose_yaml_document(text))
        document = load_yaml_document(text)
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None) or "it does not parse"
        raise MappingError(None, f"not a YAML document ({problem})") from None
    except RecursionError:  # the needs reader's guard: no input this reader knows of reaches it
        raise MappingError(None, "not a YAML document (nested too deep)") from None
    if document is None:
        raise MappingError(None, "the mapping file is empty: a mapping that says nothing is no mapping")
    if not isinstance(document, dict):
        raise MappingError(None, "the mapping file must be a mapping of keys to values")
    try:
        return NeedsMapping.model_validate(document)
    except ValidationError as exc:
        raise _from_validation_error(exc) from None


def read_mapping(path: Path) -> NeedsMapping:
    """Read one mapping file: UTF-8, a leading byte order mark skipped, then :func:`parse_mapping_text`.

    A refusal of the file itself (unreadable, not UTF-8, not YAML, empty)
    carries no path; the caller names the file as the user spelled it.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MappingError(None, f"cannot read the file ({exc.strerror or exc})") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MappingError(None, f"not UTF-8 text ({exc.reason} at byte {exc.start})") from exc
    return parse_mapping_text(text)
