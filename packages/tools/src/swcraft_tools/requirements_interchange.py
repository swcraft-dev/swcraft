"""The requirements interchange file: a Sphinx-Needs ``needs.json``, read as the build wrote it.

:func:`read_needs_file` reads one such file through a :class:`~swcraft_tools.requirements_mapping.NeedsMapping` into
the baseline and the entries of the selected version, in file order, or into one :class:`InterchangeError` naming
the refused JSON key path (an RFC 6901 pointer) and why. It decides nothing about the model.

- **The envelope**: ``current_version`` and ``versions`` (required), ``created``, ``project``, ``format_version``
  (``1``); a version holds ``needs`` (required), ``created``, ``creator``, ``needs_amount``, ``filters``,
  ``filters_amount``, ``needs_schema`` and ``needs_defaults_removed`` (``false`` only). Any other key is refused;
  the models are strict. ``current_version`` names a key of ``versions``; an empty or blank name selects a version
  with no baseline. Only the selected version is validated; any other must be an object whose ``needs`` is an
  object.
- **A need's keys**, in this order: *read* (``id``, ``type``, the mapped source keys); *refused with a reason*
  (``version``, ``source``); *ignored*, value unchecked (the :data:`SPHINX_NEEDS_BOOKKEEPING_KEYS`, the mapping's
  ``ignore``, ``<k>_back`` when ``<k>`` is read or ignored); *refused* (anything else, naming ``--mapping``). A type
  the mapping does not name is refused; an ``ignore`` type yields an :class:`IgnoredNeed` (its ``id`` alone read), a
  status in ``removed_status`` a :class:`RemovedNeed`, any other need a :class:`Need`. ``""`` in ``asil``,
  ``document`` or ``revision`` is absent.
- **Parents** of a ``field_type: "links"`` property follow Sphinx-Needs 9.2.0's link grammar (the ``[condition]``
  and the ``.part`` dropped), except that a value without ``.`` naming a need of the file is taken whole; a parent
  naming a need of the file whose id holds ``.`` is refused. Other parents are taken whole, kept once each. Every id
  is judged by ``schema.requirement_id_problem``.

Refused besides: a need's ``id`` other than its key, a ``needs_amount`` other than the count of needs, a JSON object
repeating a key, and a lone surrogate in an id or in ``current_version``. Not reached: a lone surrogate in another
text (a ``title``, a description, a ``document``), which only the planner's read-back refuses; any id against the
model. When the file holds both ``X`` and ``X[c]``, ``X[c]`` is read. The bookkeeping list is pinned to Sphinx-Needs
9.2.0: a key a later release adds is refused unless the mapping reads or ignores it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError, field_validator

from swcraft_tools.requirements_mapping import BACKLINK_SUFFIX, DEFAULT_MAPPING, NeedsMapping
from swcraft_tools.schema import AsilValue, requirement_id_problem

#: The schema's package-data file name, beside this module.
SCHEMA_RESOURCE = "requirements_interchange.schema.json"

#: The need keys Sphinx-Needs writes itself, ignored by the reader unless a mapping reads one.
#:
#: Pinned to Sphinx-Needs 9.2.0's sources, never read from a file's own ``needs_schema`` (a key a
#: later release labels ``core`` must not slip through unrefused): the 46 keys of ``data.py``'s
#: ``NeedsCoreFields`` (equal in 7.0.0 and 8.5.0); the built-in link types; the ``needgantt`` extras;
#: and the twelve GitHub service keys 8.x and 9.0 wrote as ``null`` on every need.
SPHINX_NEEDS_BOOKKEEPING_KEYS: tuple[str, ...] = (
    # NeedsCoreFields
    "id",
    "docname",
    "lineno",
    "lineno_content",
    "title",
    "status",
    "tags",
    "collapse",
    "hide",
    "layout",
    "style",
    "arch",
    "is_import",
    "is_external",
    "external_url",
    "external_css",
    "type",
    "type_name",
    "type_prefix",
    "type_color",
    "type_style",
    "is_modified",
    "modifications",
    "is_need",
    "is_part",
    "parts",
    "id_parent",
    "id_complete",
    "jinja_content",
    "template",
    "pre_template",
    "post_template",
    "content",
    "pre_content",
    "post_content",
    "has_dead_links",
    "has_forbidden_dead_links",
    "constraints",
    "constraints_results",
    "constraints_passed",
    "constraints_error",
    "doctype",
    "sections",
    "section_name",
    "signature",
    "parent_need",
    # the built-in link types
    "links",
    "parent_needs",
    # the needgantt extras
    "duration",
    "completion",
    # the GitHub service keys of 8.x and 9.0
    "url",
    "user",
    "avatar",
    "service",
    "created_at",
    "updated_at",
    "closed_at",
    "query",
    "specific",
    "max_amount",
    "max_content_lines",
    "id_prefix",
)


class InterchangeError(ValueError):
    """One refusal of the interchange file: the JSON key path (``None`` for the file itself) and why."""

    def __init__(self, pointer: str | None, sentence: str) -> None:
        self.pointer = pointer
        self.sentence = sentence
        super().__init__(sentence if pointer is None else f"{pointer or '(top level)'}: {sentence}")


@dataclass(frozen=True)
class Need:
    """One need to import, in SWCraft's names: what the planner compares with the model."""

    id: str
    title: str
    description: str
    req_type: Literal["req", "spec"]
    asil: AsilValue | None = None
    document: str | None = None
    # The source tool's own revision as written, never parsed as a number.
    revision: str | None = None
    # The parent ids, each once, in file order by first occurrence.
    derived_from: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemovedNeed:
    """A need whose status is in the mapping's ``removed_status``: its id and that status."""

    id: str
    status: str


@dataclass(frozen=True)
class IgnoredNeed:
    """A need of a type the mapping maps to ``ignore``: its id and that type; nothing else is read."""

    id: str
    type: str


@dataclass(frozen=True)
class SelectedNeeds:
    """The selected version of a read file: its name as the baseline, and its entries in file order.

    ``baseline`` is ``None`` when the selected version's name is empty or blank:
    such a name states no baseline, as a blank ``--baseline`` is none.
    """

    baseline: str | None
    in_file_order: tuple[Need | RemovedNeed | IgnoredNeed, ...]

    @property
    def needs(self) -> tuple[Need, ...]:
        """The needs to import, in file order."""
        return tuple(entry for entry in self.in_file_order if isinstance(entry, Need))

    @property
    def removed(self) -> tuple[RemovedNeed, ...]:
        """The needs removed by their status, in file order."""
        return tuple(entry for entry in self.in_file_order if isinstance(entry, RemovedNeed))

    @property
    def ignored(self) -> tuple[IgnoredNeed, ...]:
        """The needs of an ignored type, in file order."""
        return tuple(entry for entry in self.in_file_order if isinstance(entry, IgnoredNeed))


# ---------------------------------------------------------------------------
# Field rules
# ---------------------------------------------------------------------------


def _requirement_id(value: str) -> str:
    """The requirement id rule as a field validator."""
    problem = requirement_id_problem(value)
    if problem is not None:
        raise ValueError(problem)
    return value


def no_lone_surrogate(value: str) -> str:
    """Refuse a text no UTF-8 encoder can write: one the report prints (an id, a parent, the baseline)."""
    for character in value:
        if 0xD800 <= ord(character) <= 0xDFFF:
            raise ValueError(f"holds a lone surrogate (U+{ord(character):04X}), which no UTF-8 text can carry")
    return value


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _count(value: object) -> int:
    """A JSON count: an integer, or an integral number such as ``1.0`` (JSON Schema's ``integer``)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("must be an integer")
    if isinstance(value, float):
        if not (math.isfinite(value) and value.is_integer()):
            raise ValueError("must be an integer")
        value = int(value)
    if value < 0:
        raise ValueError("must be >= 0")
    return value


def _refuse_null(value: object) -> object:
    if value is None:
        raise ValueError("must not be null: omit the key instead")
    return value


def _empty_is_absent(value: object) -> object:
    """``""`` as ``null``: Sphinx-Needs writes an unset extra as either, depending on the release."""
    return None if value == "" else value


_NeedId = Annotated[str, AfterValidator(_requirement_id), AfterValidator(no_lone_surrogate)]
_Text = Annotated[str, AfterValidator(_not_blank)]


class _ReadValues(BaseModel):
    """A need's read values under SWCraft's names, each validated as the model's field is."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    id: _NeedId
    title: _Text
    description: _Text
    # Read, never stored: a status in the mapping's `removed_status` removes the requirement.
    status: str | None = None
    asil: AsilValue | None = None
    document: str | None = None
    revision: str | None = None
    derived_from: list[str] | None = None

    _empty_values_are_absent = field_validator("asil", "document", "revision", mode="before")(_empty_is_absent)


class NeedsVersion(BaseModel):
    """One version object; only the selected one is validated and read."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    needs: dict[str, Any]
    created: str | None = None
    creator: dict[str, Any] | None = None
    needs_amount: int | None = None
    filters: dict[str, Any] | None = None
    filters_amount: int | None = None
    needs_schema: dict[str, Any] | None = None
    needs_defaults_removed: bool | None = None

    _optional_keys_are_not_null = field_validator(
        "created",
        "creator",
        "needs_amount",
        "filters",
        "filters_amount",
        "needs_schema",
        "needs_defaults_removed",
        mode="before",
    )(_refuse_null)

    @field_validator("needs_amount", "filters_amount", mode="before")
    @classmethod
    def counts_are_integral(cls, value: object) -> int:
        # Pydantic runs the last-declared before-validator first, so this one meets a null
        # ahead of `_optional_keys_are_not_null` and refuses it in that rule's words.
        return _count(_refuse_null(value))

    @field_validator("needs_defaults_removed")
    @classmethod
    def defaults_are_kept(cls, value: bool | None) -> bool | None:
        if value:
            raise ValueError(
                "must be false: a file written with its defaults removed cannot tell an omitted key from an absent one"
            )
        return value


class NeedsDocument(BaseModel):
    """The top level: ``current_version`` selects one of ``versions``."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    current_version: Annotated[str, AfterValidator(no_lone_surrogate)]
    versions: dict[str, Any]
    created: str | None = None
    project: str | None = None
    format_version: int | None = None

    _optional_keys_are_not_null = field_validator("created", "project", mode="before")(_refuse_null)

    @field_validator("format_version", mode="before")
    @classmethod
    def format_is_one(cls, value: object) -> int:
        if _count(_refuse_null(value)) != 1:
            raise ValueError("must be 1: format 1 is the one needs.json format this reader knows")
        return 1


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _pointer(*segments: object) -> str:
    """An RFC 6901 JSON pointer to ``segments`` (``~`` and ``/`` escaped)."""
    return "".join("/" + str(segment).replace("~", "~0").replace("/", "~1") for segment in segments)


#: Why a need refuses the two keys of a requirement the import owns.
_REFUSED_NEED_KEYS = {
    "version": "'version' is refused: SWCraft keeps its own change counter and raises it on every change",
    "source": "'source' is refused: every imported requirement is written 'source: external'",
}


def _keys(model: type[NeedsDocument | NeedsVersion]) -> tuple[str, ...]:
    """An envelope model's keys, in declaration order."""
    return tuple(model.model_fields)


#: The level an unknown envelope key is refused at, named with the keys it holds.
_LEVELS = {
    NeedsDocument: ("the top level", _keys(NeedsDocument)),
    NeedsVersion: ("a version object", _keys(NeedsVersion)),
}

#: Pydantic's type errors in JSON's words (its own name a model class or a Python type).
_JSON_TYPE_SENTENCES = {
    "model_type": "must be a JSON object",
    "dict_type": "must be a JSON object",
    "list_type": "must be a JSON array",
    "string_type": "must be a string",
    "bool_type": "must be true or false",
    "missing": "is required",
}


def _sentence(error: Any) -> str:
    """One Pydantic error's sentence: a field rule's own, or a type error in JSON's words."""
    if error["type"] == "value_error":
        return str(error["ctx"]["error"])
    return _JSON_TYPE_SENTENCES.get(error["type"], error["msg"])


_Envelope = TypeVar("_Envelope", NeedsDocument, NeedsVersion)


def _validated(model: type[_Envelope], data: object, *prefix: object) -> _Envelope:
    """*data* as *model*, or its first refusal at its key path as the file spells it.

    An unknown key is refused here, in file order, because Pydantic cannot name
    an extra key holding a lone surrogate.
    """
    if isinstance(data, dict):
        level, keys = _LEVELS[model]
        for key in data:
            if key not in keys:
                raise InterchangeError(_pointer(*prefix, key), f"unknown key: {level} holds only {', '.join(keys)}")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        error = exc.errors(include_url=False)[0]
        raise InterchangeError(_pointer(*prefix, *error["loc"]), _sentence(error)) from None


# ---------------------------------------------------------------------------
# A need, through the mapping
# ---------------------------------------------------------------------------


def _link_target(value: str, need_ids: AbstractSet[str]) -> str:
    """The need id a link field's value names, by Sphinx-Needs 9.2.0's own grammar.

    ``NeedLink.from_string_with_warnings`` drops the condition, then
    ``NeedLink.parse_address`` splits the address at its first ``.``. A value
    holding no ``.`` that is one of *need_ids*, the selected version's need ids,
    is that need's id whole.
    """
    if "." not in value and value in need_ids:
        return value
    return _link_address(value).split(".", maxsplit=1)[0]


def _link_address(value: str) -> str:
    """A link field's value without its condition, by ``NeedLink.from_string_with_warnings``' bracket rule."""
    start = value.find("[")
    if start > 0:
        rest = value[start:]
        depth = len(rest) - len(rest.lstrip("["))
        if rest[depth:].find("]" * depth) >= 0:
            return value[:start]
    return value


def _unlinkable_need(value: str, need_ids: AbstractSet[str]) -> str | None:
    """The need of *need_ids* a link field's *value* names with a ``.`` in its id, which no link can reach.

    Sphinx-Needs 9.2.0 splits the address at its first ``.`` under every
    configuration (``NeedLink.parse_address``), so a link to such a need names
    another need or none. The value is judged whole first, then its address
    without a condition. ``None`` when neither holds a ``.`` and is a need of
    the file: that value keeps the grammar's reading.
    """
    for candidate in (value, _link_address(value)):
        if "." in candidate and candidate in need_ids:
            return candidate
    return None


def _is_link_field(needs_schema: Mapping[str, Any] | None, key: str) -> bool:
    """Whether the version's ``needs_schema`` labels the need key *key* ``field_type: "links"``."""
    properties = needs_schema.get("properties") if needs_schema is not None else None
    labelled = properties.get(key) if isinstance(properties, dict) else None
    return isinstance(labelled, dict) and labelled.get("field_type") == "links"


@dataclass(frozen=True)
class _NeedReader:
    """Reads the needs of one selected version through one mapping."""

    mapping: NeedsMapping
    version: str
    parents_are_links: bool
    #: The selected version's need keys: a link-field parent equal to one is read whole (:func:`_link_target`).
    need_ids: frozenset[str] = frozenset()

    def pointer(self, key: str, *segments: object) -> str:
        return _pointer("versions", self.version, "needs", key, *segments)

    def read(self, key: str, raw: object) -> Need | RemovedNeed | IgnoredNeed:
        if not isinstance(raw, dict):
            raise InterchangeError(self.pointer(key), "must be a JSON object")
        need_type = self.type_of(key, raw)
        req_type = self.mapping.types[need_type]
        if req_type == "ignore":
            return IgnoredNeed(self.ignored_id(key, raw), need_type)
        self.classify_keys(key, raw)
        values = self.values(key, raw)
        # Judged before the status: a removed need is validated whole, its parents included.
        parents = self.parents(key, values.derived_from or [])
        if values.status is not None and values.status in self.mapping.removed_status:
            return RemovedNeed(values.id, values.status)
        return Need(
            id=values.id,
            title=values.title,
            description=values.description,
            req_type=req_type,
            asil=values.asil,
            document=values.document,
            revision=values.revision,
            derived_from=parents,
        )

    def type_of(self, key: str, raw: dict[str, Any]) -> str:
        if "type" not in raw:
            raise InterchangeError(self.pointer(key, "type"), "is required")
        need_type = raw["type"]
        if not isinstance(need_type, str):
            raise InterchangeError(self.pointer(key, "type"), "must be a string")
        if need_type not in self.mapping.types:
            raise InterchangeError(
                self.pointer(key, "type"), f"type '{need_type}' is not SWCraft's; map it under 'types' in --mapping"
            )
        return need_type

    def ignored_id(self, key: str, raw: dict[str, Any]) -> str:
        """An ignored need's id: read only so that the planner can name it."""
        if "id" not in raw:
            raise InterchangeError(self.pointer(key, "id"), "is required")
        need_id = raw["id"]
        if not isinstance(need_id, str):
            raise InterchangeError(self.pointer(key, "id"), "must be a string")
        self.refuse_misfiled(key, need_id)
        try:
            return no_lone_surrogate(need_id)
        except ValueError as exc:
            raise InterchangeError(self.pointer(key, "id"), str(exc)) from None

    def refuse_misfiled(self, key: str, need_id: str) -> None:
        if need_id != key:
            raise InterchangeError(
                self.pointer(key, "id"),
                f"'{need_id}' is filed under the key '{key}': a need's id is the key it is filed under",
            )

    def classify_keys(self, key: str, raw: dict[str, Any]) -> None:
        """Refuse the first key, in file order, that is neither read nor ignored."""
        read = ("id", "type", *self.mapping.read_keys())
        known = {*read, *SPHINX_NEEDS_BOOKKEEPING_KEYS, *self.mapping.ignore}
        for name in raw:
            if name in known:
                continue
            if name in _REFUSED_NEED_KEYS:
                raise InterchangeError(self.pointer(key, name), _REFUSED_NEED_KEYS[name])
            base = name.removesuffix(BACKLINK_SUFFIX)
            if base == name:
                raise InterchangeError(
                    self.pointer(key, name),
                    "unknown key: map it under 'fields' in --mapping, or list it under 'ignore'; "
                    f"a need is read from {', '.join(read)}",
                )
            if base not in known:
                raise InterchangeError(
                    self.pointer(key, name),
                    f"unknown key: it mirrors the link '{base}', which is neither read nor ignored; "
                    f"map '{base}' under 'fields' in --mapping, or list it under 'ignore'",
                )

    def values(self, key: str, raw: dict[str, Any]) -> _ReadValues:
        """The read values under SWCraft's names, a refusal placed on the source key it read."""
        sources = {"id": "id", **self.mapping.fields.model_dump()}
        data = {field: raw[source] for field, source in sources.items() if source in raw}
        try:
            values = _ReadValues.model_validate(data)
        except ValidationError as exc:
            error = exc.errors(include_url=False)[0]
            field, *rest = error["loc"]
            raise InterchangeError(self.pointer(key, sources[str(field)], *rest), _sentence(error)) from None
        self.refuse_misfiled(key, values.id)
        return values

    def parents(self, key: str, values: list[str]) -> tuple[str, ...]:
        """The parent ids, each judged by the id rule, kept once each in file order."""
        parents: list[str] = []
        for index, value in enumerate(values):
            unlinkable = _unlinkable_need(value, self.need_ids) if self.parents_are_links else None
            if unlinkable is not None:
                raise InterchangeError(
                    self.pointer(key, self.mapping.fields.derived_from, index),
                    f"'{value}' names the need '{unlinkable}', whose id holds '.': Sphinx-Needs 9.2.0 reads a link "
                    f"to it as the need '{unlinkable.split('.', maxsplit=1)[0]}' and a part, under every "
                    "configuration, so it cannot link such an id and SWCraft does not support it as a parent",
                )
            parent = _link_target(value, self.need_ids) if self.parents_are_links else value
            problem = requirement_id_problem(parent)
            if problem is None:
                try:
                    parents.append(no_lone_surrogate(parent))
                    continue
                except ValueError as exc:
                    problem = str(exc)
            elif self.parents_are_links:
                problem = f"'{value}' names the need '{parent}': {problem}"
            else:
                problem = f"a parent is a bare requirement id: {problem}"
            raise InterchangeError(self.pointer(key, self.mapping.fields.derived_from, index), problem)
        return tuple(dict.fromkeys(parents))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


class _Object(dict[str, Any]):
    """A parsed JSON object that remembers the first key it saw twice (``json`` keeps the last silently)."""

    duplicate: str | None = None


def _object_pairs(pairs: list[tuple[str, Any]]) -> _Object:
    built = _Object()
    for key, value in pairs:
        if key in built and built.duplicate is None:
            built.duplicate = key
        built[key] = value
    return built


def _refuse_duplicates(value: object, *segments: object) -> None:
    """Raise at the first object, depth first in file order, that repeats a key."""
    if isinstance(value, _Object):
        if value.duplicate is not None:
            raise InterchangeError(_pointer(*segments, value.duplicate), "the key appears twice in one object")
        children: Any = value.items()
    elif isinstance(value, list):
        children = enumerate(value)
    else:
        return
    for key, child in children:
        _refuse_duplicates(child, *segments, key)


def _refuse_constant(name: str) -> float:
    raise ValueError(f"{name} is not a JSON value")


def _refuse_unread_version(name: str, version: object) -> None:
    """A version the file does not select: an object whose ``needs`` is an object, and nothing more."""
    if not isinstance(version, dict):
        raise InterchangeError(_pointer("versions", name), "must be a JSON object")
    if "needs" not in version:
        raise InterchangeError(_pointer("versions", name, "needs"), "is required")
    if not isinstance(version["needs"], dict):
        raise InterchangeError(_pointer("versions", name, "needs"), "must be a JSON object")


def parse_needs_text(text: str, mapping: NeedsMapping = DEFAULT_MAPPING) -> SelectedNeeds:
    """The selected entries of one interchange document's text, or the :class:`InterchangeError` refusing it."""
    try:
        data = json.loads(text, object_pairs_hook=_object_pairs, parse_constant=_refuse_constant)
    except json.JSONDecodeError as exc:
        raise InterchangeError(
            None, f"not a JSON document ({exc.msg} at line {exc.lineno}, column {exc.colno})"
        ) from exc
    except (ValueError, RecursionError) as exc:  # a NaN/Infinity constant, an over-long integer, nesting too deep
        raise InterchangeError(None, f"not a JSON document ({exc})") from exc
    _refuse_duplicates(data)
    document = _validated(NeedsDocument, data)
    # The empty name is a name: the Sphinx-Needs builder writes "" for a project with no version.
    if document.current_version not in document.versions:
        raise InterchangeError(_pointer("current_version"), f"'{document.current_version}' names no key of 'versions'")
    name = document.current_version
    for other, unread in document.versions.items():
        if other != name:
            _refuse_unread_version(other, unread)
    version = _validated(NeedsVersion, document.versions[name], "versions", name)
    links = _is_link_field(version.needs_schema, mapping.fields.derived_from)
    reader = _NeedReader(mapping, name, parents_are_links=links, need_ids=frozenset(version.needs))
    entries = tuple(reader.read(key, raw) for key, raw in version.needs.items())
    if version.needs_amount is not None and version.needs_amount != len(version.needs):
        raise InterchangeError(
            _pointer("versions", name, "needs_amount"),
            f"states {version.needs_amount} needs and the version holds {len(version.needs)}",
        )
    return SelectedNeeds(baseline=name if name.strip() else None, in_file_order=entries)


def read_needs_file(path: Path, mapping: NeedsMapping = DEFAULT_MAPPING) -> SelectedNeeds:
    """Read one interchange file: UTF-8, a leading byte order mark skipped, then :func:`parse_needs_text`.

    A refusal of the file itself (unreadable, not UTF-8) carries no pointer;
    the caller names the file as the user spelled it.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InterchangeError(None, f"cannot read the file ({exc.strerror or exc})") from exc
    try:
        # RFC 8259 § 8.1 lets a parser ignore a leading byte order mark.
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InterchangeError(None, f"not UTF-8 text ({exc.reason} at byte {exc.start})") from exc
    return parse_needs_text(text, mapping)
