"""The trace link search's two files of decisions: ``proposals.json`` read back, and ``trace-decisions.yaml``.

``swcraft experiment trace`` writes ``proposals.json``; the architect fills its decision slots;
``swcraft experiment trace --apply`` carries them out. This module is the one reader of that file and the one
reader and writer of the project's decisions file. It decides nothing about the model.

**The proposals file.** :func:`read_proposals` reads the bytes once and digests them, decodes strict UTF-8,
parses with a hook that refuses a repeated key, and validates strictly with no extra key at every level; then
the reader-only rules -- a requirement id in two entries, one ``(requirement, target)`` pair twice (two rows, a
row and a ``chosen`` entry, or two ``chosen`` entries), one target and ``member`` in two ``linked`` entries, and
a ``linked`` entry whose ``member`` names another requirement, is in the element form, or disagrees with its
``version`` and ``confirmed``; and an integer written with a fraction (``1.0``). The first problem is refused,
naming the file as the caller gave it and an RFC 6901 pointer. A model digest that differs from the model's is
not this module's question. The JSON Schema ``trace_proposals.schema.json`` (package data) states the same
format but for those reader-only rules, which JSON Schema cannot compare across objects (or, for ``1.0``,
cannot tell from ``1``). :func:`pending` is the count of decisions not yet carried out.

**The decisions file.** ``trace-decisions.yaml`` in the project directory, validated strictly; absent reads as
no decisions. :func:`merge_decisions` keeps one record per ``(requirement, version, target)``, the later
replacing the earlier, and :func:`decisions_text` writes the records in the id, version and address order, so
two runs that record the same decisions write the same bytes.

It takes the target address from the leaf :mod:`swcraft_tools.trace_address`, never from
:mod:`swcraft_tools.trace_search`, which imports this module, and imports neither ``writer``, ``editing``,
``allocation`` nor ``scipy``.

**What it does not reach.** A key the YAML file repeats (libyaml keeps the last, silently, as for every model
file); an id's grammar beyond being non-empty (a decision on an id no requirement has is stale at Apply); and
whether any decision still holds against the model.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from swcraft_tools.project import SwcraftProject
from swcraft_tools.trace import is_element_form, parse_link
from swcraft_tools.trace_address import (
    TargetAddress,
    TraceExperimentError,
    address_order_key,
    exact_decimal,
    id_order_key,
)
from swcraft_tools.yaml_load import load_yaml_document

#: The schema's package-data file name, beside this module.
SCHEMA_RESOURCE = "trace_proposals.schema.json"

#: The decisions file's one name, in the project directory beside ``swcraft.yaml``.
DECISIONS_FILE = "trace-decisions.yaml"
DECISIONS_FORMAT_VERSION: Final = 1

ACCEPT = "accept"
REJECT = "reject"
CONFIRM = "confirm"

#: The kinds whose address names an owner: a component's port and runnable.
_OWNED_KINDS = ("port", "runnable")


class TraceRecordsError(TraceExperimentError):
    """One refusal of a proposals or a decisions file: the file as named, the RFC 6901 pointer, and why.

    The file is named as the caller gave it (the proposals file) or by its project-relative name (the decisions
    file), never by a resolved location.
    """

    def __init__(self, file: str, pointer: str, sentence: str) -> None:
        self.file = file
        self.pointer = pointer
        self.sentence = sentence
        super().__init__(f"{file}: {pointer or '(top level)'}: {sentence}")


# ---------------------------------------------------------------------------
# The proposals file's models
# ---------------------------------------------------------------------------

_STRICT = ConfigDict(strict=True, extra="forbid", frozen=True)


def _exactly(expected: object) -> BeforeValidator:
    """A constant that refuses an equal value of another JSON type (Pydantic's ``Literal`` takes ``true`` for ``1``)."""

    def check(value: object) -> object:
        if type(value) is not type(expected) or value != expected:
            raise ValueError(f"must be {json.dumps(expected)}")
        return value

    return BeforeValidator(check)


_One = Annotated[Literal[1], _exactly(1)]
_True = Annotated[Literal[True], _exactly(True)]
_Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
_Text = Annotated[str, Field(min_length=1)]
_Kind = Literal[
    "swc", "port", "runnable", "interface", "composition", "task", "processor", "core", "memory", "partition"
]


class Address(BaseModel):
    """A target's address: ``{kind, owner, name}``, ``owner`` naming the component of a port or a runnable."""

    model_config = _STRICT

    kind: _Kind
    owner: str | None
    name: _Text

    @model_validator(mode="after")
    def _owner_matches_kind(self) -> Address:
        if self.kind in _OWNED_KINDS and not self.owner:
            raise ValueError(f"a {self.kind} names its component in 'owner'")
        if self.kind not in _OWNED_KINDS and self.owner is not None:
            raise ValueError(f"a {self.kind} has no owner: 'owner' is null")
        return self

    def target(self) -> TargetAddress:
        return TargetAddress(self.kind, self.owner, self.name)


class _Model(BaseModel):
    model_config = _STRICT

    digest: _Digest
    files: Annotated[int, Field(ge=0)]
    swcraftVersion: _Text  # noqa: N815 -- the file's own key


class _Configuration(BaseModel):
    model_config = _STRICT

    file: _Text
    insideProject: bool  # noqa: N815
    sha256: _Digest


class _Fraction(BaseModel):
    model_config = _STRICT

    n: int
    d: Annotated[int, Field(ge=2)]


class _Score(BaseModel):
    model_config = _STRICT

    n: Annotated[int, Field(ge=1)]
    d: Annotated[Literal[1000000], _exactly(1000000)]


_Examples = Annotated[list[str], Field(max_length=3)]


class _NameEvidence(BaseModel):
    model_config = _STRICT

    kind: Literal["name"]
    phrase: _Text


class _NeighboursEvidence(BaseModel):
    model_config = _STRICT

    kind: Literal["neighbours"]
    share: int | _Fraction
    seeded: Annotated[int, Field(ge=1)]
    of: Annotated[int, Field(ge=1)]
    examples: _Examples


class _SimilarEvidence(BaseModel):
    model_config = _STRICT

    kind: Literal["similar"]
    score: _Score
    count: Annotated[int, Field(ge=1)]
    examples: _Examples


class _TextEvidence(BaseModel):
    model_config = _STRICT

    kind: Literal["text"]
    terms: Annotated[list[str], Field(min_length=1, max_length=5)]


_Evidence = Annotated[
    _NameEvidence | _NeighboursEvidence | _SimilarEvidence | _TextEvidence, Field(discriminator="kind")
]


class _Group(BaseModel):
    model_config = _STRICT

    kind: Literal["swc", "composition", "interface", "task", "partition", "core", "processor", "memory"]
    name: _Text
    score: _Score
    evidence: list[_Evidence]


def _marks_need_a_decision(decision: str | None, applied: bool | None, stale: str | None) -> None:
    """``applied`` or ``stale`` only on a decided item, and never both on one."""
    if applied is not None and stale is not None:
        raise ValueError("an item is either applied or stale, never both")
    if decision is None and (applied is not None or stale is not None):
        raise ValueError("'applied' and 'stale' mark a decision: an undecided item carries neither")


class _Marked(BaseModel):
    """An item with optional slots that are present with a value or absent, never ``null``."""

    model_config = _STRICT

    @model_validator(mode="before")
    @classmethod
    def _optional_slots_are_not_null(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in ("preselected", "applied", "stale", "from"):
                if key in data and data[key] is None:
                    raise ValueError(f"'{key}' is never null: omit the key instead")
        return data


class Candidate(_Marked):
    """One proposed row: the search's evidence, and the architect's ``decision`` with Apply's marks."""

    place: Annotated[int, Field(ge=1)]
    group: _Group
    target: Address
    fingerprint: _Digest
    band: Literal["strong", "medium", "weak"]
    score: _Score
    evidence: list[_Evidence]
    decision: Literal["accept", "reject"] | None
    preselected: _True | None = None
    applied: _True | None = None
    stale: _Text | None = None

    @model_validator(mode="after")
    def _marks(self) -> Candidate:
        if self.preselected is not None and self.decision != ACCEPT:
            raise ValueError("'preselected' marks an accept made from a preselection: a row not accepted carries none")
        _marks_need_a_decision(self.decision, self.applied, self.stale)
        return self


class Linked(_Marked):
    """One link the requirement already has, its ``member`` as written; a confirm only while unconfirmed."""

    target: Address
    member: _Text
    version: Annotated[int, Field(ge=1)] | None
    confirmed: bool
    decision: Literal["confirm"] | None = None
    applied: _True | None = None
    stale: _Text | None = None

    @model_validator(mode="before")
    @classmethod
    def _decision_slot(cls, data: Any) -> Any:
        """A confirmed link carries no ``decision`` slot; a link awaiting confirmation always carries one."""
        if isinstance(data, dict) and isinstance(data.get("confirmed"), bool):
            if data["confirmed"] and any(key in data for key in ("decision", "applied", "stale")):
                raise ValueError("a confirmed link carries no decision: it names its requirement's version already")
            if not data["confirmed"] and "decision" not in data:
                raise ValueError("a link awaiting confirmation carries 'decision': null or \"confirm\"")
        return data

    @model_validator(mode="after")
    def _marks(self) -> Linked:
        _marks_need_a_decision(self.decision, self.applied, self.stale)
        return self


class Chosen(_Marked):
    """An accept of a target outside the rows; it carries no fingerprint."""

    target: Address
    applied: _True | None = None
    stale: _Text | None = None

    @model_validator(mode="after")
    def _marks(self) -> Chosen:
        _marks_need_a_decision(ACCEPT, self.applied, self.stale)
        return self


class Entry(BaseModel):
    """One requirement's entry."""

    model_config = _STRICT

    id: _Text
    #: The requirement's title at the search, for display only: Apply never reads it.
    title: str
    #: Its description at the search, for display only: Apply never reads it.
    description: str
    version: Annotated[int, Field(ge=1)]
    fingerprint: _Digest
    document: _Text | None
    pass_: Literal["review", "labelling"] = Field(alias="pass")
    chosenBecause: str | None  # noqa: N815
    lowAccuracy: bool  # noqa: N815
    linked: list[Linked]
    candidates: list[Candidate]
    chosen: list[Chosen]


class ProposalsFile(BaseModel):
    """The whole file, ``formatVersion 1``."""

    model_config = _STRICT

    formatVersion: _One  # noqa: N815
    kind: Literal["trace"]
    name: _Text
    model: _Model
    configuration: _Configuration | None
    requirements: list[Entry]


@dataclass(frozen=True)
class Proposals:
    """A read proposals file.

    Its bytes as read and their digest, the document as parsed (rewritten by Apply) and its validated model.
    """

    file: str
    data: bytes
    digest: str
    document: dict[str, Any]
    model: ProposalsFile


# ---------------------------------------------------------------------------
# Reading the proposals file
# ---------------------------------------------------------------------------


def _pointer(*segments: object) -> str:
    """An RFC 6901 JSON pointer to ``segments`` (``~`` and ``/`` escaped)."""
    return "".join("/" + str(segment).replace("~", "~0").replace("/", "~1") for segment in segments)


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


def _repeated_key(value: object, *segments: object) -> str | None:
    """The pointer of the first repeated key, depth first in file order, or ``None``."""
    if isinstance(value, _Object):
        if value.duplicate is not None:
            return _pointer(*segments, value.duplicate)
        children: Any = value.items()
    elif isinstance(value, list):
        children = enumerate(value)
    else:
        return None
    for key, child in children:
        found = _repeated_key(child, *segments, key)
        if found is not None:
            return found
    return None


def _plain(value: object) -> Any:
    """*value* with every :class:`_Object` turned into a plain ``dict``, so a dump or a copy sees no subclass."""
    if isinstance(value, dict):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_plain(child) for child in value]
    return value


def _integer(text: str) -> int:
    """A JSON integer at any width: converted in chunks, so ``int()``'s digit limit never applies."""
    sign, digits = (-1, text[1:]) if text.startswith("-") else (1, text)
    value = 0
    for start in range(0, len(digits), 600):
        chunk = digits[start : start + 600]
        value = value * 10 ** len(chunk) + int(chunk)
    return sign * value


def _refuse_constant(name: str) -> float:
    raise ValueError(f"{name} is not a JSON value")


#: Pydantic's type errors in JSON's words.
_JSON_TYPE_SENTENCES = {
    "model_type": "must be a JSON object",
    "dict_type": "must be a JSON object",
    "list_type": "must be a JSON array",
    "string_type": "must be a string",
    "int_type": "must be an integer",
    "bool_type": "must be true or false",
    "missing": "is required",
    "extra_forbidden": "is not a key of the format",
}


def _file_loc(data: object, loc: tuple[int | str, ...]) -> list[int | str]:
    """*loc* as the file's key path: a segment the data does not hold is a union branch Pydantic inserted.

    Walking the parsed document keeps each segment it can follow and drops the others, but always keeps the last
    one, which may name a key that is missing.
    """
    path: list[int | str] = []
    for position, segment in enumerate(loc):
        if isinstance(data, dict) and isinstance(segment, str) and segment in data:
            data = data[segment]
        elif isinstance(data, list) and isinstance(segment, int) and 0 <= segment < len(data):
            data = data[segment]
        elif position != len(loc) - 1:
            continue
        path.append(segment)
    return path


def _validation_refusal(file: str, exc: ValidationError, data: object) -> TraceRecordsError:
    """The first Pydantic error as a refusal at its key path in the file."""
    error = exc.errors(include_url=False)[0]
    if error["type"] == "value_error":
        sentence = str(error["ctx"]["error"])
    else:
        sentence = _JSON_TYPE_SENTENCES.get(error["type"], error["msg"])
    return TraceRecordsError(file, _pointer(*_file_loc(data, error["loc"])), sentence)


def _refuse_reader_only(file: str, proposals: ProposalsFile) -> None:
    """Raise the first reader-only rule the file breaks, in file order."""
    seen_ids: dict[str, int] = {}
    for number, entry in enumerate(proposals.requirements):
        where = ("requirements", number)
        if entry.id in seen_ids:
            raise TraceRecordsError(
                file,
                _pointer(*where, "id"),
                f"'{entry.id}' has an entry already (requirements/{seen_ids[entry.id]}): one entry per requirement",
            )
        seen_ids[entry.id] = number
        links: dict[tuple[TargetAddress, str], int] = {}
        for index, link in enumerate(entry.linked):
            problem = _member_problem(entry, link)
            if problem is not None:
                raise TraceRecordsError(file, _pointer(*where, "linked", index, *problem[0]), problem[1])
            key = (link.target.target(), link.member)
            if key in links:
                raise TraceRecordsError(
                    file,
                    _pointer(*where, "linked", index, "member"),
                    f"the link '{link.member}' on this target is in linked/{links[key]} already: "
                    "one linked entry per target and member",
                )
            links[key] = index
        pairs: dict[TargetAddress, str] = {}
        targeted: tuple[tuple[str, Sequence[Candidate | Chosen]], ...] = (
            ("candidates", entry.candidates),
            ("chosen", entry.chosen),
        )
        for slot, items in targeted:
            for index, item in enumerate(items):
                address = item.target.target()
                if address in pairs:
                    raise TraceRecordsError(
                        file,
                        _pointer(*where, slot, index, "target"),
                        f"the pair of '{entry.id}' and this target is in {pairs[address]} already: "
                        "one row or chosen entry per requirement and target",
                    )
                pairs[address] = f"{slot}/{index}"


def _member_problem(entry: Entry, link: Linked) -> tuple[tuple[str, ...], str] | None:
    """Why a ``linked`` entry's member does not name its own requirement as its slots say, or ``None``."""
    member = link.member
    if is_element_form(member):
        return ("member",), f"'{member}' is in the element form: a link of '{entry.id}' names the requirement"
    parsed = parse_link(member)
    if parsed.id != entry.id:
        return ("member",), f"'{member}' names '{parsed.id}', not this entry's requirement '{entry.id}'"
    if parsed.version != link.version:
        return (
            "version",
        ), f"the member '{member}' names version {_version_words(parsed.version)}, not {_version_words(link.version)}"
    if link.confirmed != (parsed.version == entry.version):
        state = "names" if parsed.version == entry.version else "does not name"
        return ("confirmed",), (
            f"the member '{member}' {state} the requirement's version {exact_decimal(entry.version)}, "
            f"so 'confirmed' is {str(parsed.version == entry.version).lower()}"
        )
    return None


def _version_words(version: int | None) -> str:
    return "none" if version is None else exact_decimal(version)


def parse_proposals(data: bytes, file: str) -> Proposals:
    """The proposals file of *data*, or the :class:`TraceRecordsError` refusing it, naming *file*."""
    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TraceRecordsError(file, "", f"is not UTF-8 text (byte {exc.start})") from None
    try:
        parsed = json.loads(text, object_pairs_hook=_object_pairs, parse_int=_integer, parse_constant=_refuse_constant)
    except json.JSONDecodeError as exc:
        raise TraceRecordsError(file, "", f"is not JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})") from None
    except ValueError as exc:
        raise TraceRecordsError(file, "", f"is not JSON: {exc}") from None
    repeated = _repeated_key(parsed)
    if repeated is not None:
        raise TraceRecordsError(file, repeated, "the key appears twice in one object")
    document = _plain(parsed)
    if not isinstance(document, dict):
        raise TraceRecordsError(file, "", "must be a JSON object")
    try:
        model = ProposalsFile.model_validate(document)
    except ValidationError as exc:
        raise _validation_refusal(file, exc, document) from None
    _refuse_reader_only(file, model)
    return Proposals(file, data, digest, document, model)


def read_proposals(path: str | Path, file: str | None = None) -> Proposals:
    """Read the proposals file at *path*, refusing it whole on the first problem.

    *file* is how a refusal names it, the path as the caller gave it by default; an unreadable file is refused
    the same way.
    """
    name = str(path) if file is None else file
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise TraceRecordsError(name, "", f"cannot be read ({exc.strerror or type(exc).__name__})") from None
    return parse_proposals(data, name)


#: The three slots of an entry that hold items, in the file's order.
ITEM_SLOTS = ("candidates", "chosen", "linked")


def entry_items(proposals: ProposalsFile) -> Iterator[tuple[Entry, int, str, int, Candidate | Chosen | Linked]]:
    """``(entry, entry number, slot, index, item)`` for every item of the file, in the file's order."""
    for number, entry in enumerate(proposals.requirements):
        for slot in ITEM_SLOTS:
            for index, item in enumerate(getattr(entry, slot)):
                yield entry, number, slot, index, item


def decision_of(item: Candidate | Chosen | Linked) -> str | None:
    """An item's decision: a ``chosen`` entry is an accept; a confirmed link has none, as an undecided item."""
    return ACCEPT if isinstance(item, Chosen) else item.decision


def pending(proposals: ProposalsFile) -> int:
    """The decisions not yet carried out: decided rows, confirms and ``chosen`` entries, neither applied nor stale.

    A stale item is terminal: its decision can never be applied as recorded.
    """
    return sum(
        1
        for *_, item in entry_items(proposals)
        if decision_of(item) is not None and item.applied is None and item.stale is None
    )


# ---------------------------------------------------------------------------
# The decisions file
# ---------------------------------------------------------------------------


class DecisionRecord(_Marked):
    """One decision: the requirement at its version, the target, and how the decision was made."""

    requirement: _Text
    version: Annotated[int, Field(ge=1)]
    target: Address
    decision: Literal["accept", "reject", "confirm"]
    preselected: _True | None = None
    from_: _Text | None = Field(default=None, alias="from")

    @model_validator(mode="after")
    def _slots(self) -> DecisionRecord:
        if self.preselected is not None and self.decision != ACCEPT:
            raise ValueError("'preselected' is recorded on an accept only")
        if self.decision == CONFIRM:
            if self.from_ is None:
                raise ValueError("a confirm records the member it replaced in 'from'")
            if is_element_form(self.from_) or parse_link(self.from_).id != self.requirement:
                raise ValueError(f"'from' is '{self.from_}', a member that does not name '{self.requirement}'")
        elif self.from_ is not None:
            raise ValueError("'from' is recorded on a confirm only")
        return self

    @property
    def key(self) -> tuple[str, int, TargetAddress]:
        return (self.requirement, self.version, self.target.target())

    def order_key(self) -> tuple[Any, ...]:
        return (id_order_key(self.requirement), self.version, address_order_key(self.target.target()))

    def to_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "requirement": self.requirement,
            "version": self.version,
            "target": self.target.target().to_json(),
            "decision": self.decision,
        }
        if self.preselected:
            document["preselected"] = True
        if self.from_ is not None:
            document["from"] = self.from_
        return document


class Decisions(BaseModel):
    """The decisions file: ``formatVersion 1`` and its records."""

    model_config = _STRICT

    formatVersion: _One  # noqa: N815
    decisions: list[DecisionRecord]


def no_decisions() -> Decisions:
    """The decisions of a project with no decisions file."""
    return Decisions(formatVersion=DECISIONS_FORMAT_VERSION, decisions=[])


def parse_decisions(text: str, file: str = DECISIONS_FILE) -> Decisions:
    """The decisions of one file's *text*, or the :class:`TraceRecordsError` refusing it, naming *file*."""
    try:
        document = load_yaml_document(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        place = "" if mark is None else f" (line {mark.line + 1}, column {mark.column + 1})"
        raise TraceRecordsError(file, "", f"is not YAML{place}") from None
    if not isinstance(document, dict):
        raise TraceRecordsError(file, "", "must be a mapping with 'formatVersion' and 'decisions'")
    try:
        decisions = Decisions.model_validate(document)
    except ValidationError as exc:
        raise _validation_refusal(file, exc, document) from None
    seen: dict[tuple[str, int, TargetAddress], int] = {}
    for number, record in enumerate(decisions.decisions):
        if record.key in seen:
            raise TraceRecordsError(
                file,
                _pointer("decisions", number),
                f"'{record.requirement}' at version {exact_decimal(record.version)} has a record for this target "
                f"already (decisions/{seen[record.key]}): one record per requirement, version and target",
            )
        seen[record.key] = number
    return decisions


def decisions_path(project: SwcraftProject) -> Path:
    """The decisions file's location: the project directory, beside ``swcraft.yaml``."""
    return Path(project.resolved_project_root) / DECISIONS_FILE


def read_decisions_bytes(project: SwcraftProject) -> tuple[Path, bytes | None, Decisions]:
    """The decisions file's location, its bytes as read (``None``: absent) and its records.

    An absent file is no decisions; an unreadable, a non-UTF-8 or a malformed one is refused naming its place.
    """
    path = decisions_path(project)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return path, None, no_decisions()
    except OSError as exc:
        raise TraceRecordsError(DECISIONS_FILE, "", f"cannot be read ({exc.strerror or type(exc).__name__})") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise TraceRecordsError(DECISIONS_FILE, "", "cannot be read (it is not UTF-8 text)") from None
    return path, data, parse_decisions(text)


def read_decisions(project: SwcraftProject) -> Decisions:
    """The project's decisions, or none when the file is absent (:func:`read_decisions_bytes`' records)."""
    return read_decisions_bytes(project)[2]


def merge_decisions(held: Decisions, new: Iterable[DecisionRecord]) -> Decisions:
    """*held* with *new* recorded: one record per ``(requirement, version, target)``, the later one replacing."""
    records: dict[tuple[str, int, TargetAddress], DecisionRecord] = {record.key: record for record in held.decisions}
    for record in new:
        records[record.key] = record
    return Decisions(
        formatVersion=DECISIONS_FORMAT_VERSION, decisions=sorted(records.values(), key=DecisionRecord.order_key)
    )


def decisions_text(decisions: Decisions) -> str:
    """The file's text: its records in the id, version and address order."""
    ordered = sorted(decisions.decisions, key=DecisionRecord.order_key)
    document = {
        "formatVersion": DECISIONS_FORMAT_VERSION,
        "decisions": [record.to_document() for record in ordered],
    }
    return yaml.safe_dump(document, default_flow_style=False, sort_keys=False)


def rejected_pairs(decisions: Decisions) -> frozenset[tuple[str, int, TargetAddress]]:
    """Every ``(requirement, version, target)`` the architect rejected: not proposed while the version stands."""
    return frozenset(record.key for record in decisions.decisions if record.decision == REJECT)
