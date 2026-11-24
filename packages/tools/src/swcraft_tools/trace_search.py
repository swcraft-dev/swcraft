"""The trace link search, ``swcraft experiment trace``: which elements a requirement may be linked to.

For every requirement in scope it proposes a short ranked list of the elements that can carry
``satisfies``, each with the evidence an architect can read, and writes ``report.md``, ``result.json`` and a
versioned ``proposals.json`` under the report directory. It **writes nothing into the model**: Apply is
:mod:`swcraft_tools.trace_apply`'s. A pair rejected in the project's ``trace-decisions.yaml`` at the
requirement's current version is never a row, and a ``proposals.json`` holding decisions not yet applied is
not overwritten unless the caller discards them.

The module holds the configuration (:class:`TraceConfig`, :func:`load_config`, :func:`check_config_names`); the
targets and their groups, the four BM25 corpora, the seeds, the evidence (E1 the target's name in the text, E2
BM25 over the target's text, E3 the document's neighbours, E5b similar linked requirements), the score, the
rows, the places and the bands; the passes (documents, review, labelling, the self-measurement on the links the
model already holds); the findings (:data:`STOPPING_RULES`); and the run (:func:`run_trace`).

It never imports ``writer``, ``editing``, ``allocation``, ``scipy`` nor ``trace_apply``: a search needs no
mutation surface, and a second experiment kind never depends on the first. It re-exports
:mod:`swcraft_tools.trace_address`'s names.

**What it does not reach:** one fixed combination fitted on research data and never checked on held-out data,
no stemming, stubs never proposed, and a score that may round differently at a millionth's boundary on another
platform's ``math.log``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from swcraft_tools import experiment_frame, trace_records
from swcraft_tools._version import __version__
from swcraft_tools.bm25 import (
    NO_REMOVAL,
    Bm25Stats,
    Corpus,
    Removal,
    bm25_scores,
    bm25_stats,
    scores_of,
    term_contributions,
)
from swcraft_tools.model_state import ModelState, load_model_state
from swcraft_tools.project import (
    EXPERIMENT_REPORT_FILENAME,
    EXPERIMENT_RESULT_FILENAME,
    SwcraftProject,
    experiment_default_directory,
    experiment_output_directory,
    project_relative_name,
)
from swcraft_tools.schema import (
    CSInterface,
    ExternalInterface,
    Port,
    Requirement,
    SRInterface,
    SWComponent,
)
from swcraft_tools.trace import LinkedElement, LinkSite, is_element_form, link_sites_of_state, parse_link, site_kind
from swcraft_tools.trace_address import MEMBER_RANK, SAFE_INTEGER_BITS, group_order_key
from swcraft_tools.trace_address import GroupKey as GroupKey
from swcraft_tools.trace_address import TargetAddress as TargetAddress
from swcraft_tools.trace_address import TraceError as TraceError
from swcraft_tools.trace_address import TraceExperimentError as TraceExperimentError
from swcraft_tools.trace_address import address_order_key as address_order_key
from swcraft_tools.trace_address import exact_decimal as exact_decimal
from swcraft_tools.trace_address import id_order_key as id_order_key
from swcraft_tools.trace_terms import tokens
from swcraft_tools.validator import ConsistencyResult, check_cross_layer_consistency

# ---------------------------------------------------------------------------
# The errors
# ---------------------------------------------------------------------------


class TraceConfigUnreadableError(TraceExperimentError):
    """The configuration file cannot be read at all; the sentence carries the reason, the caller names the file."""


# ---------------------------------------------------------------------------
# The configuration file
# ---------------------------------------------------------------------------

#: The kind, and the name a configuration that names none takes.
DEFAULT_NAME = "trace"

TargetKind = Literal[
    "swc", "port", "runnable", "interface", "composition", "task", "processor", "core", "memory", "partition"
]

#: The kinds of target, in the order the configuration and the report list them.
TARGET_KINDS: tuple[TargetKind, ...] = (
    "swc",
    "port",
    "runnable",
    "interface",
    "composition",
    "task",
    "processor",
    "core",
    "memory",
    "partition",
)

#: The labelling value that names every document and the no-document group.
LABELLING_ALL = "all"


class TraceSearch(BaseModel):
    """The configuration's ``search`` section: how many places, and which documents get the labelling pass.

    A misspelt key is refused naming it, and a ``"5"`` or a ``true`` is not a count.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    #: The places per requirement, each one target group.
    candidates: int = Field(default=5, ge=1)
    #: The documents given the labelling pass: a list of document values, or ``all``. Every
    #: other document in scope is reviewed, so the default reviews every document.
    labelling: Literal["all"] | list[str] = []
    #: The labelling budget per document, a share of its unlinked requirements in scope, in ``(0, 1]``.
    labelling_share: float = Field(default=0.05, gt=0, le=1, allow_inf_nan=False)

    @field_validator("labelling")
    @classmethod
    def _labelling_lists_each_document_once(cls, value: Literal["all"] | list[str]) -> Literal["all"] | list[str]:
        if isinstance(value, list):
            repeated = sorted({item for item in value if value.count(item) > 1})
            if repeated:
                raise ValueError(f"labelling names a document more than once: {', '.join(repeated)}")
        return value


class TraceConstraints(BaseModel):
    """The configuration's ``constraints`` section: which requirements are searched, and which targets proposed."""

    model_config = ConfigDict(extra="forbid", strict=True)

    #: Only these documents; every document when absent.
    documents: list[str] | None = None
    #: Whether the requirements with no document are in scope.
    without_document: bool = True
    #: Only these requirement ids, intersected with the documents; every requirement when absent.
    requirements: list[str] | None = None
    #: The kinds of target proposed.
    target_kinds: list[TargetKind] = list(TARGET_KINDS)
    #: Element names never proposed; a component's name excludes its ports and runnables too.
    exclude: list[str] = []


class TraceConfig(BaseModel):
    """One trace link search, as its configuration file declares it -- every key optional.

    Unknown keys are refused, each by its dotted key; an explicit ``name: null`` is refused, not read as the
    default. ``kind`` is the class's, never the file's: the command line names the kind.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    #: The kind, from the class: a ``kind`` key in the file is an unknown key.
    kind: ClassVar[str] = DEFAULT_NAME

    #: The report directory's name: one directory segment, never a device name Windows reserves.
    name: str = DEFAULT_NAME
    #: The report directory, relative to the project directory; ``<dist_root>/experiments/<name>/`` when absent.
    output: str | None = None
    search: TraceSearch = TraceSearch()
    constraints: TraceConstraints = TraceConstraints()

    @field_validator("name")
    @classmethod
    def _name_is_one_directory_segment(cls, value: str) -> str:
        refusal = experiment_frame.report_name_refusal(value)
        if refusal is not None:
            raise ValueError(refusal)
        return value


def parse_config(document: object) -> TraceConfig:
    """The configuration *document* (an already-parsed YAML value) declares, or :class:`TraceExperimentError`.

    ``None`` -- an empty file, or one of comments only -- is the defaults, as no file at all.
    """
    if document is None:
        return TraceConfig()
    if not isinstance(document, dict):
        raise TraceExperimentError("the configuration file must be a mapping of keys to values")
    try:
        return TraceConfig.model_validate(document)
    except ValidationError as exc:
        raise TraceExperimentError(
            "the configuration file is refused: " + "; ".join(experiment_frame.refusal_lines(exc))
        ) from None


def load_config(path: str) -> tuple[TraceConfig, str]:
    """Read and validate the configuration file at *path*: ``(configuration, "sha256:<hex>")``.

    Raises:
        TraceConfigUnreadableError: the file cannot be read, or its bytes are not UTF-8.
        TraceExperimentError: the text is not YAML, not a mapping, or the mapping is refused.
    """
    try:
        document, digest = experiment_frame.read_config_document(path)
    except OSError as exc:
        raise TraceConfigUnreadableError(
            f"the configuration file cannot be read: {exc.strerror or type(exc).__name__}"
        ) from None
    except UnicodeDecodeError:
        raise TraceConfigUnreadableError("the configuration file cannot be read: it is not UTF-8 text") from None
    except experiment_frame.ConfigDocumentError as exc:
        raise TraceExperimentError(str(exc)) from None
    return parse_config(document), digest


def document_of(requirement: Requirement) -> str | None:
    """The requirement's document: ``document.strip()``, an empty result being **no document**.

    Otherwise exact, case included: two spellings are two documents, so the Documents table shows a converter's
    inconsistency.
    """
    if requirement.document is None:
        return None
    value = requirement.document.strip()
    return value or None


def _target_names(state: ModelState) -> set[str]:
    """Every name a target of any kind carries, stubs included: what an ``exclude`` entry may name."""
    names: set[str] = set()
    for swc in state["swc"]:
        component: SWComponent = swc
        names.add(component.name)
        names.update(port.name for port in component.ports)
        names.update(runnable.name for runnable in component.runnables)
    for layer in ("interfaces", "compositions", "tasks", "hardware"):
        names.update(element.name for element in state[layer])
    return names


def check_config_names(config: TraceConfig, state: ModelState, labelling_key: str = "search.labelling") -> None:
    """Refuse a document, a requirement or an element name *config* uses that the model does not declare.

    A document value is compared exactly with the requirements' normalised documents (:func:`document_of`), so a
    value with surrounding white space names no document.

    Raises:
        TraceExperimentError: the first refused name by its dotted key, in the order
            ``constraints.documents``, ``search.labelling``, ``constraints.requirements``,
            ``constraints.exclude`` -- ``search.labelling`` named *labelling_key*, ``--labelling`` when the
            command line gave the value.
    """
    requirements: list[Requirement] = list(state["requirements"])
    documents = {document for document in map(document_of, requirements) if document is not None}

    def unknown(key: str, value: str) -> TraceExperimentError:
        if value != value.strip():
            return TraceExperimentError(
                f"'{key}' names document '{value}' with surrounding white space: a requirement's document is "
                f"compared with its surrounding white space removed, so write '{value.strip()}'"
            )
        return TraceExperimentError(f"'{key}' names document '{value}', which no requirement of the model carries")

    for value in config.constraints.documents or ():
        if value not in documents:
            raise unknown("constraints.documents", value)
    if isinstance(config.search.labelling, list):
        for value in config.search.labelling:
            if value not in documents:
                raise unknown(labelling_key, value)
            if config.constraints.documents is not None and value not in config.constraints.documents:
                raise TraceExperimentError(
                    f"'{labelling_key}' names document '{value}', which 'constraints.documents' leaves out"
                )
    identifiers = {requirement.id for requirement in requirements}
    for value in config.constraints.requirements or ():
        if value not in identifiers:
            raise TraceExperimentError(
                f"'constraints.requirements' names requirement '{value}', which the model does not declare"
            )
    names = _target_names(state)
    for value in config.constraints.exclude:
        if value not in names:
            raise TraceExperimentError(
                f"'constraints.exclude' names element '{value}', which no linkable layer of the model declares"
            )


# ---------------------------------------------------------------------------
# The constants of the combination
# ---------------------------------------------------------------------------

#: E3's weight: the document's neighbours.
WEIGHT_NEIGHBOURS = 2.0
#: E1's weight beside E2: one rule for a name found in the text.
WEIGHT_NAME = 0.25
#: E5b's weight at full strength.
WEIGHT_SIMILAR = 2.0
#: The number of other linked requirements in the document from which E5b and E3 weigh fully.
SIMILAR_FULL_AT = 5
#: Every score is rounded to an integer number of millionths before any comparison.
SCORE_DENOMINATOR = 10**6
#: A component's ports and runnables never fill a place: at most this many of them are rows.
MEMBERS_PER_GROUP = 3
#: The band ``strong`` by neighbours: at least this many other linked requirements ...
STRONG_MIN_NEIGHBOURS = 5
#: ... and at least this share of them landing on the row's target.
STRONG_SHARE = Fraction(1, 2)
#: The band ``medium`` by neighbours alone: at least this many other linked requirements; fewer
#: need the row's own text or similarity evidence beside them (one linked neighbour is not a consensus).
MEDIUM_MIN_NEIGHBOURS = 3
#: A name shorter than this is never matched in the text (E1).
MIN_NAME_LENGTH = 3
#: At most this many requirement ids illustrate a neighbour or a similarity record.
EVIDENCE_EXAMPLES = 3
#: At most this many shared terms illustrate a text record.
EVIDENCE_TERMS = 5

BAND_STRONG = "strong"
BAND_MEDIUM = "medium"
BAND_WEAK = "weak"
#: The bands, strongest first.
BANDS: tuple[str, ...] = (BAND_STRONG, BAND_MEDIUM, BAND_WEAK)


def millionths(value: float) -> int:
    """*value* as an integer number of millionths, Python's round-half-to-even."""
    return round(value * SCORE_DENOMINATOR)


# ---------------------------------------------------------------------------
# The targets and their groups
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """One target in scope: its address, the element it names and the terms of its member bag."""

    address: TargetAddress
    element: Any
    bag: tuple[str, ...]


def _interface_text(interface: str | None, interfaces: Mapping[str, Any]) -> list[str]:
    """A port's interface text, by what ``port.interface`` resolves to."""
    if interface is None:
        return []
    resolved = interfaces.get(interface)
    if resolved is None:
        return [interface]
    parts = [resolved.name]
    if isinstance(resolved, SRInterface):
        for element in resolved.data_elements:
            parts += [element.name, element.description]
    elif isinstance(resolved, CSInterface):
        for operation in resolved.operations:
            parts += [operation.name, operation.description]
    elif isinstance(resolved, ExternalInterface):
        parts += [*resolved.data_elements, *resolved.operations]
    return parts


def _bag(parts: Sequence[str]) -> tuple[str, ...]:
    return tuple(tokens(" ".join(part for part in parts if part)))


def _first_by_name(elements: Iterable[Any]) -> dict[str, Any]:
    named: dict[str, Any] = {}
    for element in elements:
        named.setdefault(element.name, element)
    return named


def interfaces_by_name(state: ModelState) -> dict[str, Any]:
    """Interface name -> the declared interface (an external stub included), the first file's on a duplicate."""
    return _first_by_name(state["interfaces"])


def _excluded(address: TargetAddress, exclude: frozenset[str]) -> bool:
    return address.name in exclude or (address.owner is not None and address.owner in exclude)


def scope_targets(state: ModelState, config: TraceConfig) -> list[Target]:
    """The scope's targets, in the address order: not stubs, of a kind in ``target_kinds``, not ``exclude``d."""
    kinds = frozenset(config.constraints.target_kinds)
    exclude = frozenset(config.constraints.exclude)
    interfaces = interfaces_by_name(state)
    found: list[Target] = []

    def keep(address: TargetAddress, element: Any, parts: Sequence[str]) -> None:
        if address.kind in kinds and not _excluded(address, exclude):
            found.append(Target(address, element, _bag(parts)))

    for swc in state["swc"]:
        component: SWComponent = swc
        if component.external:
            continue
        keep(TargetAddress("swc", None, component.name), component, [component.name, component.description])
        for port in component.ports:
            parts = [port.name, port.description, *_interface_text(port.interface, interfaces)]
            keep(TargetAddress("port", component.name, port.name), port, parts)
        for runnable in component.runnables:
            parts = [runnable.name, runnable.description]
            for read in runnable.reads:
                parts += [read.port, read.element]
            for write in runnable.writes:
                parts += [write.port, write.element]
            for call in runnable.calls:
                parts += [call.port, call.operation]
            keep(TargetAddress("runnable", component.name, runnable.name), runnable, parts)
    for interface in state["interfaces"]:
        if isinstance(interface, ExternalInterface):
            continue
        parts = [interface.name, interface.description]
        members = interface.data_elements if isinstance(interface, SRInterface) else interface.operations
        for member in members:
            parts += [member.name, member.description]
        keep(TargetAddress("interface", None, interface.name), interface, parts)
    for layer, kind in (("compositions", "composition"), ("tasks", "task")):
        for element in state[layer]:
            keep(TargetAddress(kind, None, element.name), element, [element.name, element.description])
    for element in state["hardware"]:
        keep(TargetAddress(element.kind, None, element.name), element, [element.name, element.description])
    return sorted(found, key=lambda target: address_order_key(target.address))


# ---------------------------------------------------------------------------
# The links: seeds, existing links and the corpora
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExistingLink:
    """One link a requirement already has: the target it is on, the version it names, whether it is a seed.

    ``member`` is the ``satisfies`` member exactly as written (``SR-12@3``, ``SR-12``, ``SR-12@x``): a confirm
    names the one member it replaces, and ``version`` alone cannot tell a bare member from a malformed one.
    """

    target: TargetAddress
    version: int | None
    confirmed: bool
    member: str

    def to_json(self) -> dict[str, Any]:
        return {
            "target": self.target.to_json(),
            "member": self.member,
            "version": self.version,
            "confirmed": self.confirmed,
        }


def site_target(layer: str, element: SWComponent | LinkedElement, site: LinkSite) -> TargetAddress:
    """The address of what holds *site*, *element*'s ``satisfies`` member in *layer*.

    A port's or a runnable's site names its component as the owner; an element's own list names the element.
    The ``kind`` is :func:`swcraft_tools.trace.site_kind`'s.
    """
    kind = site_kind(layer, element, site)
    if site.owner is not None:
        return TargetAddress(kind, element.name, site.owner[1])
    return TargetAddress(kind, None, element.name)


def _all_link_sites(state: ModelState) -> Iterator[tuple[TargetAddress, str]]:
    """Every ``satisfies`` member of the model, stubs included, with the address of the element holding it."""
    for layer, element, site in link_sites_of_state(state.layers):
        yield site_target(layer, element, site), site.value


def existing_links(state: ModelState) -> dict[str, list[ExistingLink]]:
    """Requirement id -> its links, confirmed or not, in the address order, one per target and member.

    A seed is a member naming the requirement at its **current** version. A bare id, a stale version and a
    malformed suffix are links that are not seeds; an element-form member and an id that names no requirement are
    no link of any requirement. A link on an external interface stub is kept with the kind ``interface``: it is
    never a target, but it is a link.
    """
    versions = {requirement.id: requirement.version for requirement in state["requirements"]}
    found: dict[str, set[ExistingLink]] = {}
    for address, value in _all_link_sites(state):
        if is_element_form(value):
            continue
        parsed = parse_link(value)
        current = versions.get(parsed.id)
        if current is None:
            continue
        found.setdefault(parsed.id, set()).add(ExistingLink(address, parsed.version, parsed.version == current, value))
    return {
        identifier: sorted(
            links,
            key=lambda link: (address_order_key(link.target), link.version is None, link.version or 0, link.member),
        )
        for identifier, links in found.items()
    }


def seeds_of(links: Sequence[ExistingLink]) -> tuple[TargetAddress, ...]:
    """The distinct seeded targets of one requirement's links, in the address order."""
    return tuple(dict.fromkeys(link.target for link in links if link.confirmed))


def requirement_terms(requirement: Requirement, name_words: frozenset[str] = frozenset()) -> list[str]:
    """The requirement's text as terms: its ``title`` and its ``description`` (the query, and its E5b text).

    Without the **name words**: the words of the project's name (*name_words*) and of the requirement's own
    ``baseline``, which name the release or the project, not the element asked for, and would draw every element
    whose description carries them. **What this does not reach:** a word of the name that is also the
    requirement's subject is dropped as well, and a name the text spells differently is not.
    """
    dropped = name_words | frozenset(tokens(requirement.baseline or ""))
    return [term for term in tokens(f"{requirement.title} {requirement.description}") if term not in dropped]


# ---------------------------------------------------------------------------
# The index: everything built once per run
# ---------------------------------------------------------------------------


@dataclass
class SearchIndex:
    """The scope's targets, their groups, the four corpora and the links, built once per run."""

    targets: dict[TargetAddress, Target]
    #: Group -> its scope targets, in the address order.
    groups: dict[GroupKey, tuple[TargetAddress, ...]]
    group_text: Corpus
    member_text: Corpus
    group_similar: Corpus
    member_similar: Corpus
    #: Requirement id -> its links (:func:`existing_links`).
    links: dict[str, list[ExistingLink]]
    #: Requirement id -> its seeded scope targets.
    seeds: dict[str, tuple[TargetAddress, ...]]
    #: Requirement id -> its text's terms.
    terms: dict[str, list[str]]
    #: Group or target -> the ids of the requirements whose text is in its E5b bag.
    similar_members: dict[Any, frozenset[str]]
    #: Case-folded name -> the scope targets carrying it.
    names: dict[str, tuple[TargetAddress, ...]]
    #: Document (``None``: no document) -> the ids of its linked requirements, every requirement of the model.
    linked_by_document: dict[str | None, frozenset[str]]
    requirements: dict[str, Requirement]
    #: Requirement id -> the targets rejected for it at its current version: never a row.
    rejected: dict[str, frozenset[TargetAddress]] = field(default_factory=dict)


def rejected_at_current_version(
    requirements: Mapping[str, Requirement], rejected: Iterable[tuple[str, int, TargetAddress]]
) -> dict[str, frozenset[TargetAddress]]:
    """Requirement id -> its rejected targets, of the ``(requirement, version, target)`` pairs at its current version.

    A reject made at another version re-opens the pair: the judgement was made on another text. A pair naming no
    requirement of the model is dropped.
    """
    found: dict[str, set[TargetAddress]] = {}
    for identifier, version, address in rejected:
        requirement = requirements.get(identifier)
        if requirement is not None and requirement.version == version:
            found.setdefault(identifier, set()).add(address)
    return {identifier: frozenset(addresses) for identifier, addresses in found.items()}


def build_index(
    state: ModelState,
    config: TraceConfig,
    rejected: Iterable[tuple[str, int, TargetAddress]] = (),
    name_words: frozenset[str] = frozenset(),
) -> SearchIndex:
    """Build the search's index over the scope's targets and every requirement of the model.

    The corpora hold the scope's targets alone; the seeds and the E5b bags read every non-stub requirement of the
    model, in scope or not: a neighbour's link is the model's knowledge whatever the run asks about. *rejected*
    is the decisions file's rejected pairs; the ones at a requirement's current version are kept. *name_words*
    are the project name's terms, left out of every requirement's (:func:`requirement_terms`).
    """
    requirements = {requirement.id: requirement for requirement in state["requirements"] if not requirement.stub}
    targets = {target.address: target for target in scope_targets(state, config)}
    groups: dict[GroupKey, list[TargetAddress]] = {}
    for address in targets:
        groups.setdefault(address.group, []).append(address)
    ordered_groups = {key: tuple(groups[key]) for key in sorted(groups, key=group_order_key)}

    group_text, member_text = Corpus(), Corpus()
    for key, members in ordered_groups.items():
        group_text.add(key, [term for address in members for term in targets[address].bag])
    for address, target in targets.items():
        member_text.add(address, target.bag)

    links = existing_links(state)
    seeds = {
        identifier: tuple(address for address in seeds_of(links.get(identifier, [])) if address in targets)
        for identifier in requirements
    }
    terms = {identifier: requirement_terms(requirement, name_words) for identifier, requirement in requirements.items()}
    similar: dict[Any, set[str]] = {}
    for identifier in sorted(requirements, key=id_order_key):
        for address in seeds[identifier]:
            similar.setdefault(address, set()).add(identifier)
            similar.setdefault(address.group, set()).add(identifier)
    group_similar, member_similar = Corpus(), Corpus()
    for key in ordered_groups:
        if key in similar:
            group_similar.add(
                key, [t for identifier in sorted(similar[key], key=id_order_key) for t in terms[identifier]]
            )
    for address in targets:
        if address in similar:
            member_similar.add(
                address, [t for identifier in sorted(similar[address], key=id_order_key) for t in terms[identifier]]
            )

    names: dict[str, list[TargetAddress]] = {}
    for address in targets:
        names.setdefault(address.name.casefold(), []).append(address)

    linked: dict[str | None, set[str]] = {}
    for identifier, requirement in requirements.items():
        if seeds_of(links.get(identifier, [])):
            linked.setdefault(document_of(requirement), set()).add(identifier)

    return SearchIndex(
        targets=targets,
        groups=ordered_groups,
        group_text=group_text,
        member_text=member_text,
        group_similar=group_similar,
        member_similar=member_similar,
        links=links,
        seeds=seeds,
        terms=terms,
        similar_members={key: frozenset(value) for key, value in similar.items()},
        names={name: tuple(addresses) for name, addresses in names.items()},
        linked_by_document={document: frozenset(ids) for document, ids in linked.items()},
        requirements=requirements,
        rejected=rejected_at_current_version(requirements, rejected),
    )


def rejected_earlier(index: SearchIndex, identifier: str) -> int:
    """How many targets in scope the requirement's rejects keep out of its rows: *k rejected earlier, not proposed*.

    A rejected target outside the scope, or already linked, is not counted. **What this does not reach:** a
    counted target is one the reject made ineligible, not one that would have scored a row; the search does not
    rank it to find out.
    """
    linked = {link.target for link in index.links.get(identifier, ())}
    return sum(
        1 for address in index.rejected.get(identifier, ()) if address in index.targets and address not in linked
    )


# ---------------------------------------------------------------------------
# The ranking of one requirement: evidence, rows, places and bands
# ---------------------------------------------------------------------------


def score_figure(value: int) -> dict[str, int]:
    """A score in millionths as ``result.json`` carries it: ``{"n": value, "d": 1000000}``, never reduced."""
    return {"n": value, "d": SCORE_DENOMINATOR}


def share_figure(value: Fraction) -> int | dict[str, int]:
    """An exact share: an integer, or a reduced ``{"n", "d"}``."""
    return value.numerator if value.denominator == 1 else {"n": value.numerator, "d": value.denominator}


@dataclass(frozen=True)
class Evidence:
    """The raw evidence of one group or one target for one requirement, before it is combined."""

    #: E1: the matched target's name, or ``None``.
    name: str | None
    #: E2, raw BM25.
    text: float
    #: E3's numerator: the other linked requirements of the document with a seed here.
    seeded: tuple[str, ...]
    #: E5b, raw BM25 after the leave-one-out.
    similar: float


@dataclass(frozen=True)
class Row:
    """One proposed target of a place: its own score, band and evidence (member level)."""

    address: TargetAddress
    score: int
    band: str
    evidence: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Place:
    """One ranked group with its rows: the group's score and evidence (group level).

    A group ranked past the ``candidates`` places keeps its score, its rows and their bands, and carries no
    evidence record: nothing shows it.
    """

    group: GroupKey
    score: int
    evidence: tuple[dict[str, Any], ...]
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class Ranking:
    """One requirement's full ranking: every group with a row, best first; the places are its first ``candidates``."""

    requirement: str
    #: The other linked requirements of the requirement's document (``n``; 0 without a document).
    neighbours: int
    ranked: tuple[Place, ...]
    candidates: int

    @property
    def places(self) -> tuple[Place, ...]:
        return self.ranked[: self.candidates]

    @property
    def margin(self) -> int | None:
        """The first group's score minus the second's (or 0), before the cut to the places; ``None`` with no row."""
        if not self.ranked:
            return None
        second = self.ranked[1].score if len(self.ranked) > 1 else 0
        return self.ranked[0].score - second

    def best_row(self) -> Row | None:
        """The strongest-band row of the places, then the higher score, then the earlier place."""
        best: tuple[tuple[int, int, int], Row] | None = None
        for index, place in enumerate(self.places):
            for row in place.rows:
                key = (BANDS.index(row.band), -row.score, index)
                if best is None or key < best[0]:
                    best = (key, row)
        return None if best is None else best[1]


_NAME_RUNS = re.compile(r"\w+")

#: A test's brute-force switch: when set, every group it holds is retrieved besides the ones the query reaches.
_retrieve_every_group: frozenset[GroupKey] | None = None


def _name_runs(text: str) -> frozenset[str]:
    """The query's maximal runs of letters, digits and ``_``, case-folded: what E1 looks a name up in."""
    return frozenset(run.casefold() for run in _NAME_RUNS.findall(text))


def neighbours_of(index: SearchIndex, requirement: Requirement) -> tuple[str, ...]:
    """The other linked requirements of the requirement's document, in the id order; none without a document."""
    document = document_of(requirement)
    if document is None:
        return ()
    others = index.linked_by_document.get(document, frozenset()) - {requirement.id}
    return tuple(sorted(others, key=id_order_key))


def _records(
    evidence: Evidence,
    *,
    neighbours: int,
    e2_stats: Bm25Stats | None,
    key: Any,
    similar_normalised: float,
    similar_examples: Sequence[str],
    similar_count: int,
) -> tuple[dict[str, Any], ...]:
    """The closed evidence records, in their fixed order, each only when it adds to the score."""
    records: list[dict[str, Any]] = []
    if evidence.name is not None:
        records.append({"kind": "name", "phrase": evidence.name})
    if evidence.seeded:
        records.append(
            {
                "kind": "neighbours",
                "share": share_figure(Fraction(len(evidence.seeded), neighbours)),
                "seeded": len(evidence.seeded),
                "of": neighbours,
                "examples": list(evidence.seeded[:EVIDENCE_EXAMPLES]),
            }
        )
    if neighbours > 0 and millionths(similar_normalised) > 0:
        records.append(
            {
                "kind": "similar",
                "score": score_figure(millionths(similar_normalised)),
                "count": similar_count,
                "examples": list(similar_examples[:EVIDENCE_EXAMPLES]),
            }
        )
    if evidence.text > 0:
        contributions = term_contributions(e2_stats, key)
        ordered = sorted(contributions, key=lambda term: (-millionths(contributions[term]), term))
        records.append({"kind": "text", "terms": ordered[:EVIDENCE_TERMS]})
    return tuple(records)


def rank(index: SearchIndex, requirement: Requirement, candidates: int, *, hide_own_links: bool = False) -> Ranking:
    """The requirement's ranking, every score in millionths.

    *hide_own_links* is the self-measurement's mode: none of the requirement's own targets is excluded as already
    linked, and no rejected one either. Its own text is out of every E5b bag in either mode (the leave-one-out).
    Otherwise a target rejected at the requirement's current version is excluded exactly as an already linked one
    is: never a row, while its group's evidence still counts.
    """
    identifier = requirement.id
    query = index.terms[identifier]
    query_terms = frozenset(query)
    neighbours = neighbours_of(index, requirement)
    n = len(neighbours)
    already = (
        frozenset()
        if hide_own_links
        else frozenset(link.target for link in index.links.get(identifier, ()))
        | index.rejected.get(identifier, frozenset())
    )

    # 1. the retrieved groups, with the leave-one-out removals
    own_seeds = index.seeds.get(identifier, ())
    own_counts = Counter(query)
    member_removal = Removal(frozenset(own_seeds), own_counts) if own_seeds else NO_REMOVAL
    group_removal = Removal(frozenset(address.group for address in own_seeds), own_counts) if own_seeds else NO_REMOVAL
    group_e2_stats = bm25_stats(index.group_text, query)
    group_e2 = scores_of(group_e2_stats)
    group_e5 = bm25_scores(index.group_similar, query, group_removal)

    seeded_groups: dict[GroupKey, list[str]] = {}
    seeded_targets: dict[TargetAddress, list[str]] = {}
    for neighbour in neighbours:
        seeds = index.seeds.get(neighbour, ())
        for group in dict.fromkeys(address.group for address in seeds):
            seeded_groups.setdefault(group, []).append(neighbour)
        for address in seeds:
            seeded_targets.setdefault(address, []).append(neighbour)

    named: dict[TargetAddress, str] = {}
    for run in sorted(_name_runs(f"{requirement.title} {requirement.description}")):
        for address in index.names.get(run, ()):
            if len(address.name) >= MIN_NAME_LENGTH and address not in already:
                named[address] = address.name

    retrieved = set(group_e2) | set(group_e5) | set(seeded_groups) | {address.group for address in named}
    if _retrieve_every_group is not None:
        retrieved |= _retrieve_every_group
    retrieved_groups = sorted((group for group in retrieved if group in index.groups), key=group_order_key)

    # 2. the raw evidence at member level, for the eligible targets of the retrieved groups
    eligible = [address for group in retrieved_groups for address in index.groups[group] if address not in already]
    member_e2_stats = bm25_stats(index.member_text, query)
    member_e2 = scores_of(member_e2_stats)
    member_e5 = bm25_scores(index.member_similar, query, member_removal)

    # 3. the normalisation maxima, which never depend on rows or places
    group_e2_max = max((group_e2.get(group, 0.0) for group in retrieved_groups), default=0.0)
    group_e5_max = max((group_e5.get(group, 0.0) for group in retrieved_groups), default=0.0)
    member_e2_max = max((member_e2.get(address, 0.0) for address in eligible), default=0.0)
    member_e5_max = max((member_e5.get(address, 0.0) for address in eligible), default=0.0)
    # One confidence ramp for both document-wide signals: one linked neighbour must not weigh fully.
    confidence = min(1.0, n / SIMILAR_FULL_AT)
    neighbour_weight = WEIGHT_NEIGHBOURS * confidence
    similar_weight = WEIGHT_SIMILAR * confidence

    def normalised(value: float, maximum: float) -> float:
        return value / maximum if maximum > 0 else 0.0

    def combine(evidence: Evidence, e2_max: float, e5_max: float) -> int:
        neighbour_share = len(evidence.seeded) / n if n else 0.0
        name = 1.0 if evidence.name is not None else 0.0
        value = (
            neighbour_weight * neighbour_share
            + (normalised(evidence.text, e2_max) + WEIGHT_NAME * name)
            + similar_weight * normalised(evidence.similar, e5_max)
        )
        return millionths(value)

    def similar_examples(key: Any) -> tuple[list[str], int]:
        members = sorted(index.similar_members.get(key, frozenset()) - {identifier}, key=id_order_key)
        sharing = [other for other in members if query_terms & frozenset(index.terms[other])]
        return sharing, len(members)

    best_text = millionths(member_e2_max)

    # 4. the member scores, then each group's rows
    scored: list[tuple[GroupKey, int, Evidence, list[tuple[Row, Evidence]]]] = []
    for group in retrieved_groups:
        rows: list[tuple[tuple[int, int, str, str], Row, Evidence]] = []
        for address in index.groups[group]:
            if address in already:
                continue
            evidence = Evidence(
                name=named.get(address),
                text=member_e2.get(address, 0.0),
                seeded=tuple(seeded_targets.get(address, ())),
                similar=member_e5.get(address, 0.0),
            )
            score = combine(evidence, member_e2_max, member_e5_max)
            if score <= 0:
                continue
            # E3 >= STRONG_SHARE, compared in integers: k x d >= n x p for the share p/d.
            strong_share = len(evidence.seeded) * STRONG_SHARE.denominator >= n * STRONG_SHARE.numerator
            if evidence.name is not None or (n >= STRONG_MIN_NEIGHBOURS and strong_share):
                band = BAND_STRONG
            elif (
                (evidence.seeded and (n >= MEDIUM_MIN_NEIGHBOURS or evidence.text > 0 or evidence.similar > 0))
                or (evidence.similar > 0 and n > 0)
                or (evidence.text > 0 and millionths(evidence.text) == best_text)
            ):
                band = BAND_MEDIUM
            else:
                band = BAND_WEAK
            order = (-score, MEMBER_RANK.get(address.kind, 0), address.name.casefold(), address.name)
            rows.append((order, Row(address, score, band, ()), evidence))
        rows.sort(key=lambda item: item[0])
        if group.kind == "swc":
            members = [row.address for _order, row, _evidence in rows if row.address.kind != "swc"]
            kept = set(members[:MEMBERS_PER_GROUP]) | {row.address for _o, row, _e in rows if row.address.kind == "swc"}
            chosen = [(row, evidence) for _order, row, evidence in rows if row.address in kept]
        else:
            chosen = [(row, evidence) for _order, row, evidence in rows]
        if not chosen:
            continue

        # 5. the group's score; a group with a row and a score above 0 takes a place
        group_name = next((named[address] for address in index.groups[group] if address in named), None)
        group_evidence = Evidence(
            name=group_name,
            text=group_e2.get(group, 0.0),
            seeded=tuple(seeded_groups.get(group, ())),
            similar=group_e5.get(group, 0.0),
        )
        group_score = combine(group_evidence, group_e2_max, group_e5_max)
        if group_score > 0:
            scored.append((group, group_score, group_evidence, chosen))
    scored.sort(key=lambda item: (-item[1], *group_order_key(item[0])))

    # 6. the evidence records, for the places shown alone: a place past the cut keeps its scores and bands
    ranked: list[Place] = []
    for position, (group, group_score, group_evidence, chosen) in enumerate(scored):
        if position >= candidates:
            ranked.append(Place(group, group_score, (), tuple(row for row, _evidence in chosen)))
            continue
        shown: list[Row] = []
        for row, evidence in chosen:
            examples, count = similar_examples(row.address)
            records = _records(
                evidence,
                neighbours=n,
                e2_stats=member_e2_stats,
                key=row.address,
                similar_normalised=normalised(evidence.similar, member_e5_max),
                similar_examples=examples,
                similar_count=count,
            )
            shown.append(Row(row.address, row.score, row.band, records))
        examples, count = similar_examples(group)
        records = _records(
            group_evidence,
            neighbours=n,
            e2_stats=group_e2_stats,
            key=group,
            similar_normalised=normalised(group_evidence.similar, group_e5_max),
            similar_examples=examples,
            similar_count=count,
        )
        ranked.append(Place(group, group_score, records, tuple(shown)))
    return Ranking(identifier, n, tuple(ranked), candidates)


# ---------------------------------------------------------------------------
# The fingerprints
# ---------------------------------------------------------------------------


def _without_satisfies(value: Any) -> Any:
    """*value* with every ``satisfies`` key removed, at every depth."""
    if isinstance(value, dict):
        return {key: _without_satisfies(item) for key, item in value.items() if key != "satisfies"}
    if isinstance(value, list):
        return [_without_satisfies(item) for item in value]
    return value


def dump_json(value: Any, **options: Any) -> str:
    """``json.dumps`` of *value* with every integer written exactly, whatever its width.

    ``json`` refuses an integer wider than the process's digit limit; such an integer is swapped for a placeholder
    before the dump and its exact digits put back after.
    """
    wide: list[int] = []

    def swap(item: Any) -> Any:
        if isinstance(item, bool):
            return item
        if isinstance(item, int) and item.bit_length() >= SAFE_INTEGER_BITS:
            wide.append(item)
            return f"\x00wide-integer-{len(wide) - 1}\x00"
        if isinstance(item, Mapping):
            return {key: swap(entry) for key, entry in item.items()}
        if isinstance(item, (list, tuple)):
            return [swap(entry) for entry in item]
        return item

    text = json.dumps(swap(value), **options)
    for number, item in enumerate(wide):
        text = text.replace(f'"\\u0000wide-integer-{number}\\u0000"', exact_decimal(item))
    return text


def _canonical_digest(value: Any) -> str:
    """``sha256:`` over the canonical JSON of *value*: sorted keys, no spaces, non-ASCII kept, UTF-8."""
    text = dump_json(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _whole_dump(element: Any) -> dict[str, Any]:
    """*element*'s ``model_dump(mode="json", exclude_defaults=True)``, the fingerprints' one read.

    A field at its default, given or not, is left out, so a field the contract adds with a default moves no
    fingerprint and a value off its default does.
    """
    return element.model_dump(mode="json", exclude_defaults=True)


def requirement_fingerprint(requirement: Requirement) -> str:
    """The requirement's content fingerprint: its dump, ``document`` normalised; an edit of it is a change."""
    dump = _whole_dump(requirement)
    dump["document"] = document_of(requirement)
    return _canonical_digest(dump)


def content_fingerprint(element: Any, interfaces: Mapping[str, Any | None]) -> str:
    """A target's content fingerprint, pure: the element and the interfaces its bag reads.

    The envelope is ``{"target": <dump>, "interfaces": [{"name", "dump"}, ...]}``, every ``satisfies`` removed at
    every depth, each interface name once and sorted by ``(casefold, exact)``, and ``dump`` ``null`` for a name no
    interface declares. So a link, written by Apply or by hand, moves no fingerprint, and a rename or an edit of
    the element or of an interface its bag read does. **What this does not reach:** the neighbour and similarity
    evidence read other requirements, whose changes move no fingerprint.
    """
    names = sorted(interfaces, key=lambda name: (name.casefold(), name))
    envelope = {
        "target": _without_satisfies(_whole_dump(element)),
        "interfaces": [
            {
                "name": name,
                "dump": None if interfaces[name] is None else _without_satisfies(_whole_dump(interfaces[name])),
            }
            for name in names
        ],
    }
    return _canonical_digest(envelope)


def _bag_interfaces(element: Any) -> list[str]:
    """The interface names a target's bag reads: a port's own, or the distinct ones of a component's ports."""
    if isinstance(element, Port):
        return [] if element.interface is None else [element.interface]
    if isinstance(element, SWComponent):
        return list(dict.fromkeys(port.interface for port in element.ports if port.interface is not None))
    return []


def resolve_target(state: ModelState, address: TargetAddress) -> Any | None:
    """The element *address* names in *state*, or ``None``; the first file's on a duplicate."""
    if address.owner is not None:
        component = _first_by_name(state["swc"]).get(address.owner)
        if component is None:
            return None
        members = component.ports if address.kind == "port" else component.runnables
        return _first_by_name(members).get(address.name)
    layer = {"swc": "swc", "interface": "interfaces", "composition": "compositions", "task": "tasks"}.get(
        address.kind, "hardware"
    )
    for element in state[layer]:
        if element.name != address.name:
            continue
        if layer == "hardware" and element.kind != address.kind:
            continue
        if layer == "interfaces" and isinstance(element, ExternalInterface):
            continue
        return element
    return None


def target_fingerprint(state: ModelState, address: TargetAddress) -> str | None:
    """The fingerprint of the target *address* names, its interfaces resolved from *state*; ``None`` when it is gone."""
    element = resolve_target(state, address)
    if element is None:
        return None
    declared = interfaces_by_name(state)
    return content_fingerprint(element, {name: declared.get(name) for name in _bag_interfaces(element)})


# ---------------------------------------------------------------------------
# The passes, the documents and the self-measurement
# ---------------------------------------------------------------------------

PASS_REVIEW = "review"
PASS_LABELLING = "labelling"


def format_score(value: int) -> str:
    """A score in millionths as the report writes it: ``1.25``, ``0.5``, ``2`` -- exact, no trailing zero."""
    sign = "-" if value < 0 else ""
    whole, fraction = divmod(abs(value), SCORE_DENOMINATOR)
    text = f"{whole}.{fraction:06d}".rstrip("0").rstrip(".")
    return sign + text


def document_order_key(document: str | None) -> tuple[int, str, str]:
    """The document order: ``(casefold, exact)``, the no-document group last."""
    if document is None:
        return (1, "", "")
    return (0, document.casefold(), document)


def requirements_in_scope(state: ModelState, config: TraceConfig) -> list[Requirement]:
    """The requirements the run searches, in the id order.

    Not stubs, and passing ``documents``, ``without_document`` and ``requirements``.
    """
    constraints = config.constraints
    documents = None if constraints.documents is None else frozenset(constraints.documents)
    identifiers = None if constraints.requirements is None else frozenset(constraints.requirements)
    found: list[Requirement] = []
    for requirement in state["requirements"]:
        if requirement.stub:
            continue
        document = document_of(requirement)
        if document is None:
            if not constraints.without_document:
                continue
        elif documents is not None and document not in documents:
            continue
        if identifiers is not None and requirement.id not in identifiers:
            continue
        found.append(requirement)
    return sorted(found, key=lambda requirement: id_order_key(requirement.id))


def pass_of(document: str | None, config: TraceConfig) -> str:
    """The pass a document is in.

    Labelled when ``labelling`` names it or is ``all``; the no-document group only under ``all``; reviewed
    otherwise.
    """
    labelling = config.search.labelling
    if labelling == LABELLING_ALL:
        return PASS_LABELLING
    if document is not None and document in labelling:
        return PASS_LABELLING
    return PASS_REVIEW


def labelling_budget(unlinked: int, share: float) -> int:
    """``min(U, max(1, ceil(share x U)))``, the product exact: ``ceil(0.07 x 100)`` is 7, never binary64's 8."""
    if unlinked == 0:
        return 0
    return min(unlinked, max(1, math.ceil(Fraction(str(share)) * unlinked)))


@dataclass(frozen=True)
class Selection:
    """One requirement the labelling pass chose, and why."""

    requirement: str
    reason: str


def select_for_labelling(rankings: Sequence[Ranking], budget: int) -> list[Selection]:
    """The labelling selection over a document's unlinked requirements, in the order chosen.

    Step 1, one per predicted group: groups by how many requirements predict them (more first), then the group
    order; for each, the requirement predicting it with the largest margin, ties in the id order. Step 2, the least
    certain: the rest by ``(has rows, margin, id)``, so a requirement with no row comes first.
    """
    predicted: dict[GroupKey, list[Ranking]] = {}
    for ranking in rankings:
        if ranking.ranked:
            predicted.setdefault(ranking.ranked[0].group, []).append(ranking)
    chosen: list[Selection] = []
    taken: set[str] = set()
    for group in sorted(predicted, key=lambda key: (-len(predicted[key]), *group_order_key(key))):
        if len(chosen) == budget:
            break
        candidates = predicted[group]
        best = min(candidates, key=lambda ranking: (-(ranking.margin or 0), *id_order_key(ranking.requirement)))
        reason = (
            f"the clearest of {len(candidates)} requirement{'' if len(candidates) == 1 else 's'} "
            f"whose first candidate is {group.kind} {group.name}"
        )
        chosen.append(Selection(best.requirement, reason))
        taken.add(best.requirement)
    rest = sorted(
        (ranking for ranking in rankings if ranking.requirement not in taken),
        key=lambda ranking: (bool(ranking.ranked), ranking.margin or 0, *id_order_key(ranking.requirement)),
    )
    for ranking in rest[: max(0, budget - len(chosen))]:
        if ranking.ranked:
            reason = f"least certain: margin {format_score(ranking.margin or 0)}"
        else:
            reason = "no candidate found"
        chosen.append(Selection(ranking.requirement, reason))
    return chosen


def review_order(rankings: Sequence[Ranking]) -> tuple[list[Ranking], list[Ranking]]:
    """A reviewed document's requirements: ``(with rows, by best band, score, id; nothing found, by id)``."""
    found: list[tuple[tuple[int, int, str, str], Ranking]] = []
    nothing: list[Ranking] = []
    for ranking in rankings:
        best = ranking.best_row()
        if best is None:
            nothing.append(ranking)
        else:
            found.append(((BANDS.index(best.band), -best.score, *id_order_key(ranking.requirement)), ranking))
    found.sort(key=lambda item: item[0])
    return [ranking for _key, ranking in found], sorted(nothing, key=lambda r: id_order_key(r.requirement))


def _link_state(index: SearchIndex, identifier: str) -> str:
    """``linked`` (a seed), ``awaiting`` (links, none confirmed) or ``none``."""
    links = index.links.get(identifier, ())
    if any(link.confirmed for link in links):
        return "linked"
    return "awaiting" if links else "none"


def documents_table(
    state: ModelState, index: SearchIndex, scope: Sequence[Requirement], config: TraceConfig
) -> list[dict[str, Any]]:
    """The Documents table: per document of the model and the no-document group, in the document order.

    ``inScope`` counts the document's requirements the run searches, ``stubs`` the requirement stubs left out of
    every run, and ``linked``, ``awaitingOnly`` and ``withoutLink`` partition the ones in scope.
    ``rejectedEarlier`` sums :func:`rejected_earlier` over the ones in scope. ``pass`` is ``null`` for a document
    none of whose requirements is in scope.
    """
    rows: dict[str | None, dict[str, Any]] = {}

    def row(document: str | None) -> dict[str, Any]:
        return rows.setdefault(
            document,
            {
                "document": document,
                "inScope": 0,
                "stubs": 0,
                "linked": 0,
                "awaitingOnly": 0,
                "withoutLink": 0,
                "rejectedEarlier": 0,
            },
        )

    for requirement in state["requirements"]:
        entry = row(document_of(requirement))
        if requirement.stub:
            entry["stubs"] += 1
    for requirement in scope:
        entry = row(document_of(requirement))
        entry["inScope"] += 1
        key = {"linked": "linked", "awaiting": "awaitingOnly", "none": "withoutLink"}[
            _link_state(index, requirement.id)
        ]
        entry[key] += 1
        entry["rejectedEarlier"] += rejected_earlier(index, requirement.id)
    table = []
    for document in sorted(rows, key=document_order_key):
        entry = rows[document]
        entry["pass"] = pass_of(document, config) if entry["inScope"] else None
        table.append(entry)
    return table


@dataclass(frozen=True)
class DocumentOutcome:
    """One document in scope: its pass, its listed requirements, its nothing-found ones and its unselected count."""

    document: str | None
    pass_: str
    #: The listed requirements in the pass's order, each with its reason under labelling: every requirement of
    #: a labelled document, the ones to label first carrying a reason.
    listed: tuple[tuple[Ranking, str | None], ...]
    nothing_found: tuple[Ranking, ...]
    unselected: int
    budget: int | None

    @property
    def chosen(self) -> int:
        """The requirements to label first: the listed ones carrying a reason."""
        return sum(1 for _ranking, reason in self.listed if reason is not None)


def run_passes(index: SearchIndex, scope: Sequence[Requirement], config: TraceConfig) -> list[DocumentOutcome]:
    """Rank every requirement in scope and run its document's pass, the documents in the document order."""
    by_document: dict[str | None, list[Requirement]] = {}
    for requirement in scope:
        by_document.setdefault(document_of(requirement), []).append(requirement)
    outcomes: list[DocumentOutcome] = []
    for document in sorted(by_document, key=document_order_key):
        requirements = by_document[document]
        the_pass = pass_of(document, config)
        if the_pass == PASS_REVIEW:
            rankings = [rank(index, requirement, config.search.candidates) for requirement in requirements]
            found, nothing = review_order(rankings)
            outcomes.append(
                DocumentOutcome(
                    document, the_pass, tuple((ranking, None) for ranking in found), tuple(nothing), 0, None
                )
            )
            continue
        # Every requirement is listed: the budget's choice first with its reasons, then the rest of the unlinked
        # ones in the order the same selection continues, then the linked ones in the id order. A selection with
        # budget U starts with the selection with any smaller budget, so its head is the budget's own choice.
        unlinked = [requirement for requirement in requirements if _link_state(index, requirement.id) != "linked"]
        linked = [requirement for requirement in requirements if _link_state(index, requirement.id) == "linked"]
        by_id = {requirement.id: rank(index, requirement, config.search.candidates) for requirement in requirements}
        budget = labelling_budget(len(unlinked), config.search.labelling_share)
        selection = select_for_labelling([by_id[requirement.id] for requirement in unlinked], len(unlinked))
        listed = tuple(
            (by_id[chosen.requirement], chosen.reason if number < budget else None)
            for number, chosen in enumerate(selection)
        ) + tuple((by_id[requirement.id], None) for requirement in sorted(linked, key=lambda r: id_order_key(r.id)))
        outcomes.append(DocumentOutcome(document, the_pass, listed, (), len(unlinked) - budget, budget))
    return outcomes


def recovery(index: SearchIndex, scope: Sequence[Requirement], config: TraceConfig) -> dict[str, Any]:
    """*Recovering the existing links*: each linked requirement's own links hidden in turn, then ranked again.

    The gold pairs are the distinct ``(requirement, target)`` pairs of its seeds; a pair on a target outside the
    scope cannot be recovered and is counted apart, in no denominator. **What this does not reach:** it measures
    the recovery of links an architect already set -- a biased sample, since a requirement someone linked tends to
    be an easy one -- and never the accuracy on unlinked requirements.
    """
    requirements = 0
    first = anywhere = 0
    pairs = owner_level = exact = 0
    outside_pairs = outside_requirements = 0
    for requirement in scope:
        gold = seeds_of(index.links.get(requirement.id, ()))
        if not gold:
            continue
        recoverable = [address for address in gold if address in index.targets]
        outside_pairs += len(gold) - len(recoverable)
        if not recoverable:
            outside_requirements += 1
            continue
        requirements += 1
        ranking = rank(index, requirement, config.search.candidates, hide_own_links=True)
        places = ranking.places
        groups = {address.group for address in recoverable}
        if places and places[0].group in groups:
            first += 1
        if any(place.group in groups for place in places):
            anywhere += 1
        shown_groups = {place.group for place in places}
        shown_rows = {row.address for place in places for row in place.rows}
        for address in recoverable:
            pairs += 1
            owner_level += address.group in shown_groups
            exact += address in shown_rows
    return {
        "requirements": requirements,
        "firstPlace": first,
        "inThePlaces": anywhere,
        "pairs": pairs,
        "pairsOwnerLevel": owner_level,
        "pairsExactTarget": exact,
        "outsideScopePairs": outside_pairs,
        "outsideScopeRequirements": outside_requirements,
    }


def low_accuracy(ranking: Ranking, requirement: Requirement) -> bool:
    """True for a requirement with no document, or whose document has no other linked requirement (``n = 0``)."""
    return document_of(requirement) is None or ranking.neighbours == 0


# ---------------------------------------------------------------------------
# The findings: what stops the search and what is only reported
# ---------------------------------------------------------------------------

#: The error findings that stop the search: the duplicate identities a target address or a
#: requirement id is built from. With two components of one name, an address names neither.
STOPPING_RULES: frozenset[str] = frozenset(
    {
        "name.duplicate-swc",
        "name.duplicate-port",
        "name.duplicate-runnable",
        "name.duplicate-interface",
        "name.duplicate-component-type",
        "name.duplicate-requirement",
        "composition.duplicate-name",
        "task.duplicate-name",
        "hardware.duplicate-name",
    }
)


class AmbiguousIdentityError(TraceExperimentError):
    """The model has a duplicate identity a target address or a requirement id is built from."""

    def __init__(self, rules: Sequence[str], count: int) -> None:
        self.rules = tuple(rules)
        self.count = count
        super().__init__(
            f"the search is refused: the model has {count} ambiguous identit{'y' if count == 1 else 'ies'} "
            f"({', '.join(self.rules)}), so a proposal could name more than one element "
            "(run swcraft check-model to see them)"
        )


def findings_record(result: ConsistencyResult) -> dict[str, Any]:
    """The Model findings: the count per rule and class, and every ``trace.*`` finding by its message.

    The search never refuses for these: a link search exists to repair a model whose traces do not pass the gate.
    **What this does not reach:** a model whose other errors make an element's text misleading is still searched;
    the count tells the architect, and the search never fixes or hides a finding.
    """
    findings = [*result.records, *result.warnings]
    counts = Counter((finding.rule, finding.severity) for finding in findings)
    trace_findings = sorted(
        {(finding.rule, finding.severity, finding.message) for finding in findings if finding.rule.startswith("trace.")}
    )
    return {
        "counts": [
            {"rule": rule, "class": severity, "count": counts[(rule, severity)]} for rule, severity in sorted(counts)
        ],
        "trace": [{"rule": rule, "class": severity, "message": message} for rule, severity, message in trace_findings],
    }


def refuse_ambiguous_identities(result: ConsistencyResult) -> None:
    """Raise :class:`AmbiguousIdentityError` when an error finding of a :data:`STOPPING_RULES` rule is present."""
    stopping = [record for record in result.records if record.rule in STOPPING_RULES]
    if stopping:
        raise AmbiguousIdentityError(sorted({record.rule for record in stopping}), len(stopping))


# ---------------------------------------------------------------------------
# The documents: result.json, proposals.json and report.md
# ---------------------------------------------------------------------------

RESULT_FILENAME = EXPERIMENT_RESULT_FILENAME
PROPOSALS_FILENAME = "proposals.json"
REPORT_FILENAME = EXPERIMENT_REPORT_FILENAME
RESULT_SCHEMA_VERSION = 1
PROPOSALS_FORMAT_VERSION = 1

SECTION_FINGERPRINT = "Fingerprint"
SECTION_DOCUMENTS = "Documents"
SECTION_FINDINGS = "Model findings"
SECTION_LABELLING = "Labelling"
SECTION_REVIEW = "Review"
SECTION_NOTHING_FOUND = "Nothing found"
SECTION_RECOVERY = "Recovering the existing links"
SECTION_LIMITS = "What this search does not reach"
#: The report's sections, in order.
REPORT_SECTIONS: tuple[str, ...] = (
    SECTION_FINGERPRINT,
    SECTION_DOCUMENTS,
    SECTION_FINDINGS,
    SECTION_LABELLING,
    SECTION_REVIEW,
    SECTION_NOTHING_FOUND,
    SECTION_RECOVERY,
    SECTION_LIMITS,
)

#: The sentence every report carries under its title: a band claims no accuracy.
BAND_NOTE = (
    "A band names the strength of a candidate's evidence: it is not a probability of being right, "
    "and no accuracy is claimed for it."
)
LOW_ACCURACY_NOTE = "low accuracy: text evidence only"
NO_DOCUMENT = "No document"

#: The standing limits every report states.
STANDING_LIMITS: tuple[str, ...] = (
    "one fixed combination of evidence, fitted on research data and never checked on held-out data",
    "no stemming and no lemmatisation: 'brakes' and 'brake' are different terms",
    "external stubs and their ports and runnables are never proposed",
    "a score at a millionth's boundary may round differently on another platform; ties are broken by name either way",
    "the neighbour and similarity evidence reads other requirements' links: a neighbour's change moves no fingerprint",
    "the recovery figures measure links an architect already set, a biased sample, never the accuracy on "
    "unlinked requirements",
)


@dataclass(frozen=True)
class SearchOutcome:
    """Everything a run computed, before it is rendered: the input of :func:`result_document`."""

    scope: tuple[Requirement, ...]
    documents: list[dict[str, Any]]
    findings: dict[str, Any]
    outcomes: list[DocumentOutcome]
    recovery: dict[str, Any]
    #: Requirement id -> its entry (the proposals entry without the decision slots), in the passes' order.
    entries: list[dict[str, Any]]


def _candidate_entries(ranking: Ranking, fingerprints: Mapping[TargetAddress, str]) -> list[dict[str, Any]]:
    entries = []
    for number, place in enumerate(ranking.places, start=1):
        group = {
            **place.group.to_json(),
            "score": score_figure(place.score),
            "evidence": [dict(record) for record in place.evidence],
        }
        for row in place.rows:
            entries.append(
                {
                    "place": number,
                    "group": group,
                    "target": row.address.to_json(),
                    "fingerprint": fingerprints[row.address],
                    "band": row.band,
                    "score": score_figure(row.score),
                    "evidence": [dict(record) for record in row.evidence],
                }
            )
    return entries


def _entry(
    index: SearchIndex,
    ranking: Ranking,
    the_pass: str,
    reason: str | None,
    fingerprints: Mapping[TargetAddress, str],
) -> dict[str, Any]:
    requirement = index.requirements[ranking.requirement]
    return {
        "id": requirement.id,
        "title": requirement.title,
        "description": requirement.description,
        "version": requirement.version,
        "fingerprint": requirement_fingerprint(requirement),
        "document": document_of(requirement),
        "pass": the_pass,
        "chosenBecause": reason,
        "lowAccuracy": low_accuracy(ranking, requirement),
        "rejectedEarlier": rejected_earlier(index, requirement.id),
        "linked": [link.to_json() for link in index.links.get(requirement.id, ())],
        "candidates": _candidate_entries(ranking, fingerprints),
    }


def search(
    state: ModelState,
    config: TraceConfig,
    consistency: ConsistencyResult,
    rejected: Iterable[tuple[str, int, TargetAddress]] = (),
    name_words: frozenset[str] = frozenset(),
) -> SearchOutcome:
    """The search itself, writing nothing: the index, the passes, the documents and the recovery.

    *rejected* is the decisions file's rejected pairs; the recovery ignores them. *name_words* are the
    project name's terms (:func:`project_name_words`).
    """
    index = build_index(state, config, rejected, name_words)
    scope = requirements_in_scope(state, config)
    outcomes = run_passes(index, scope, config)
    declared = interfaces_by_name(state)
    fingerprints: dict[TargetAddress, str] = {}

    def fingerprint(address: TargetAddress) -> str:
        if address not in fingerprints:
            element = index.targets[address].element
            fingerprints[address] = content_fingerprint(
                element, {name: declared.get(name) for name in _bag_interfaces(element)}
            )
        return fingerprints[address]

    entries: list[dict[str, Any]] = []
    for outcome in outcomes:
        listed = [(ranking, reason) for ranking, reason in outcome.listed]
        listed += [(ranking, None) for ranking in outcome.nothing_found]
        for ranking, reason in listed:
            for place in ranking.places:
                for row in place.rows:
                    fingerprint(row.address)
            entries.append(_entry(index, ranking, outcome.pass_, reason, fingerprints))
    return SearchOutcome(
        scope=tuple(scope),
        documents=documents_table(state, index, scope, config),
        findings=findings_record(consistency),
        outcomes=outcomes,
        recovery=recovery(index, scope, config),
        entries=entries,
    )


def _parameters(config: TraceConfig, output: str) -> dict[str, Any]:
    """The effective configuration in the file's own shape, every default filled in, every key spelled here."""
    return {
        "name": config.name,
        "output": output,
        "search": {
            "candidates": config.search.candidates,
            "labelling": config.search.labelling
            if isinstance(config.search.labelling, str)
            else list(config.search.labelling),
            "labelling_share": share_figure(Fraction(str(config.search.labelling_share))),
        },
        "constraints": {
            "documents": None if config.constraints.documents is None else list(config.constraints.documents),
            "without_document": config.constraints.without_document,
            "requirements": None if config.constraints.requirements is None else list(config.constraints.requirements),
            "target_kinds": list(config.constraints.target_kinds),
            "exclude": list(config.constraints.exclude),
        },
    }


def summary_line(outcome: SearchOutcome) -> str:
    """The run's one-line summary: the scope, the review's counts and the labelling's."""
    if not outcome.scope:
        return "trace: no requirement in scope"
    named = {document_of(requirement) for requirement in outcome.scope} - {None}
    without = sum(1 for requirement in outcome.scope if document_of(requirement) is None)
    total = len(outcome.scope)
    line = f"trace: {total} requirement{'' if total == 1 else 's'} in scope in {len(named)} document"
    line += "" if len(named) == 1 else "s"
    if without:
        line += f" and {without} without a document"
    reviewed = [outcome_ for outcome_ in outcome.outcomes if outcome_.pass_ == PASS_REVIEW]
    found = sum(len(item.listed) for item in reviewed)
    nothing = sum(len(item.nothing_found) for item in reviewed)
    line += f"; reviewed {found + nothing} ({found} with candidates, {nothing} nothing found)"
    labelled = [outcome_ for outcome_ in outcome.outcomes if outcome_.pass_ == PASS_LABELLING]
    if labelled:
        chosen = sum(item.chosen for item in labelled)
        documents = sum(1 for item in labelled if item.document is not None)
        line += f"; labelling: {chosen} chosen in {documents} document{'' if documents == 1 else 's'}"
        if any(item.document is None for item in labelled):
            line += " and among the requirements without a document"
    return line


def result_document(
    config: TraceConfig,
    outcome: SearchOutcome,
    *,
    model: tuple[str, int],
    output: str,
    configuration: dict[str, Any] | None,
    command_line: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``result.json``'s document: the one content ``report.md`` and ``proposals.json`` are made from.

    Every figure is an integer or a ``{"n", "d"}`` rational, never a float; its only paths are the record's two,
    both project-relative, and it carries no path to a model file. *command_line* records the keys the command
    line set for this run (``--labelling``), under ``fingerprint.commandLine``, absent otherwise.
    """
    digest, files = model
    document: dict[str, Any] = {
        "schemaVersion": RESULT_SCHEMA_VERSION,
        "kind": config.kind,
        "name": config.name,
        "summary": summary_line(outcome),
        "fingerprint": {
            "model": digest,
            "files": files,
            "swcraft": __version__,
            "configuration": configuration,
            "parameters": _parameters(config, output),
        },
        "documents": outcome.documents,
        "findings": outcome.findings,
        "labelling": [
            {
                "document": item.document,
                "budget": item.budget,
                "chosen": item.chosen,
                "unselected": item.unselected,
            }
            for item in outcome.outcomes
            if item.pass_ == PASS_LABELLING
        ],
        "nothingFound": [
            {"document": item.document, "requirements": [ranking.requirement for ranking in item.nothing_found]}
            for item in outcome.outcomes
            if item.nothing_found
        ],
        "requirements": outcome.entries,
        "recovery": outcome.recovery,
    }
    if command_line is not None:
        document["fingerprint"]["commandLine"] = command_line
    return document


#: The keys of a ``result.json`` entry ``proposals.json`` does not carry: a report figure, not a decision's input.
_RESULT_ONLY_ENTRY_KEYS = frozenset({"rejectedEarlier"})


def proposals_document(result: Mapping[str, Any]) -> dict[str, Any]:
    """``proposals.json``'s document, a projection of *result*: its entries with the decision slots added.

    ``decision`` is ``null`` on every candidate and on every ``linked`` entry awaiting confirmation, and ``chosen``
    an empty list on every requirement: the search never writes a decision. A confirmed ``linked`` entry carries no
    ``decision`` slot.
    """
    fingerprint = result["fingerprint"]
    return {
        "formatVersion": PROPOSALS_FORMAT_VERSION,
        "kind": result["kind"],
        "name": result["name"],
        "model": {
            "digest": fingerprint["model"],
            "files": fingerprint["files"],
            "swcraftVersion": fingerprint["swcraft"],
        },
        "configuration": fingerprint["configuration"],
        "requirements": [
            {
                **{key: value for key, value in entry.items() if key not in _RESULT_ONLY_ENTRY_KEYS},
                "linked": [link if link["confirmed"] else {**link, "decision": None} for link in entry["linked"]],
                "candidates": [{**candidate, "decision": None} for candidate in entry["candidates"]],
                "chosen": [],
            }
            for entry in result["requirements"]
        ],
    }


def result_json(document: Mapping[str, Any]) -> str:
    """``result.json``'s text: sorted keys, two-space indentation, non-ASCII kept, one final newline."""
    return dump_json(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def proposals_json(document: Mapping[str, Any]) -> str:
    """``proposals.json``'s text, in :func:`result_json`'s form."""
    return dump_json(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# report.md
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportLinks:
    """Where ``report.md`` links a name to, per layer: element name -> its model file, relative to the report directory.

    A port or a runnable links to its component's file. ``result.json`` and ``proposals.json`` carry none of it.
    """

    layers: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def target(self, layer: str, name: str) -> str | None:
        return self.layers.get(layer, {}).get(name)


_NO_LINKS = ReportLinks()

_KIND_LAYER = {
    "swc": "swc",
    "port": "swc",
    "runnable": "swc",
    "interface": "interfaces",
    "composition": "compositions",
    "task": "tasks",
}


def read_report_links(model_root: str | Path, directory: Path) -> ReportLinks:
    """The links ``report.md`` written under *directory* carries, for the model as it is now."""
    layers = ("swc", "interfaces", "compositions", "tasks", "hardware", "requirements")
    return ReportLinks({layer: experiment_frame.layer_link_targets(model_root, layer, directory) for layer in layers})


_MARKDOWN_LABEL = re.compile(r"([\\\[\]])")


def _label(text: str) -> str:
    """*text* with Markdown's link delimiters escaped: a requirement id may hold ``[``, ``]`` or ``\\``."""
    return _MARKDOWN_LABEL.sub(r"\\\1", text)


def _linked(text: str, target: str | None) -> str:
    """*text*, its delimiters escaped, as ``[text](target)`` when there is a target."""
    label = _label(text)
    return label if target is None else f"[{label}]({target})"


def _target_text(target: Mapping[str, Any], links: ReportLinks) -> str:
    kind, owner, name = target["kind"], target["owner"], target["name"]
    layer = _KIND_LAYER.get(kind, "hardware")
    shown = name if owner is None else f"{owner}.{name}"
    return f"{kind} {_linked(shown, links.target(layer, owner or name))}"


def _figure_text(figure: int | Mapping[str, int]) -> str:
    if isinstance(figure, int):
        return str(figure)
    return str(Fraction(figure["n"], figure["d"]))


def _evidence_text(records: Sequence[Mapping[str, Any]]) -> str:
    """The evidence in words, one fixed sentence per record."""
    parts: list[str] = []
    for record in records:
        kind = record["kind"]
        if kind == "name":
            parts.append(f"the name {record['phrase']} is in the text")
        elif kind == "neighbours":
            examples = f" (e.g. {', '.join(record['examples'])})" if record["examples"] else ""
            noun = "requirement" if record["of"] == 1 else "requirements"
            parts.append(f"{record['seeded']} of the {record['of']} linked {noun} of this document link here{examples}")
        elif kind == "similar":
            examples = f" (e.g. {', '.join(record['examples'])})" if record["examples"] else ""
            noun = "requirement" if record["count"] == 1 else "requirements"
            parts.append(f"similar to {record['count']} linked {noun}{examples}")
        else:
            parts.append(f"shares the terms {', '.join(record['terms'])}")
    return "; ".join(parts) if parts else "no evidence"


def _score_text(figure: Mapping[str, int]) -> str:
    return format_score(figure["n"])


def _requirement_lines(entry: Mapping[str, Any], links: ReportLinks) -> list[str]:
    """One requirement's block: its line, its existing links, its places and their rows."""
    identifier = _linked(entry["id"], links.target("requirements", entry["id"]))
    line = f"- **{identifier}** (version {exact_decimal(entry['version'])})"
    if entry["chosenBecause"] is not None:
        line += f": {entry['chosenBecause']}"
    if entry["lowAccuracy"]:
        line += f" — *{LOW_ACCURACY_NOTE}*"
    lines = [line]
    if entry["rejectedEarlier"]:
        count = entry["rejectedEarlier"]
        lines.append(f"  - {count} candidate{'' if count == 1 else 's'} rejected earlier, not proposed")
    if entry["linked"]:
        shown = []
        for link in entry["linked"]:
            state = "confirmed" if link["confirmed"] else "awaiting confirmation"
            version = "no version" if link["version"] is None else f"@{exact_decimal(link['version'])}"
            shown.append(f"{_target_text(link['target'], links)} ({version}, {state})")
        lines.append(f"  - Linked: {'; '.join(shown)}")
    if not entry["candidates"]:
        lines.append("  - No candidate found.")
        return lines
    current = None
    for candidate in entry["candidates"]:
        if candidate["place"] != current:
            current = candidate["place"]
            group = candidate["group"]
            group_target = {"kind": group["kind"], "owner": None, "name": group["name"]}
            lines.append(
                f"  {current}. {_target_text(group_target, links)} — {_score_text(group['score'])} — "
                f"{_evidence_text(group['evidence'])}"
            )
        lines.append(
            f"     - {_target_text(candidate['target'], links)} — **{candidate['band']}** — "
            f"{_score_text(candidate['score'])} — {_evidence_text(candidate['evidence'])}"
        )
    return lines


def _heading(section: str) -> list[str]:
    return [f"## {section}", ""]


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(_one_line(str(cell)).replace("|", "\\|") for cell in row) + " |" for row in rows]
    lines.append("")
    return lines


def _configuration_text(configuration: Mapping[str, Any] | None) -> str:
    if configuration is None:
        return "defaults, no configuration file"
    where = "" if configuration["insideProject"] else " (outside the project directory)"
    return f"`{configuration['file']}`{where}, {configuration['sha256']}"


def _one_line(text: str) -> str:
    """*text* with every line break a space: a document keeps its inner breaks, a table row or a heading cannot."""
    return " ".join(text.splitlines())


def _document_title(document: str | None) -> str:
    return NO_DOCUMENT if document is None else _one_line(document)


def render_markdown(document: Mapping[str, Any], report_links: ReportLinks | None = None) -> str:
    """``report.md``, rendered from ``result.json``'s *document* -- one content.

    With *report_links* each requirement id and target name links to its model file; every other line is the same
    text. Without it, nothing links.
    """
    links = _NO_LINKS if report_links is None else report_links
    fingerprint = document["fingerprint"]
    parameters = fingerprint["parameters"]
    lines = [f"# Trace link search `{document['name']}`", "", document["summary"], "", BAND_NOTE, ""]

    lines += _heading(SECTION_FINGERPRINT)
    lines.append(f"- Kind: {document['kind']}")
    lines.append(f"- Configuration: {_configuration_text(fingerprint['configuration'])}")
    lines.append(f"- Output: `{parameters['output']}`")
    lines.append(f"- Model: `{fingerprint['model']}` over {fingerprint['files']} layer files")
    lines.append(f"- SWCraft: {fingerprint['swcraft']}")
    for section in ("search", "constraints"):
        for key, value in parameters[section].items():
            shown = _figure_text(value) if key == "labelling_share" else dump_json(value, ensure_ascii=False)
            if key in fingerprint.get("commandLine", {}) and section == "search":
                shown += " (from the command line)"
            lines.append(f"- {section}.{key}: {shown}")
    lines.append("")

    lines += _heading(SECTION_DOCUMENTS)
    rows = [
        [
            _document_title(row["document"]),
            row["inScope"],
            row["linked"],
            row["awaitingOnly"],
            row["withoutLink"],
            row["rejectedEarlier"],
            row["stubs"],
            row["pass"] or "—",
        ]
        for row in document["documents"]
    ]
    if rows:
        lines += _table(
            (
                "Document",
                "In scope",
                "Linked",
                "Awaiting confirmation only",
                "Without a link",
                "Rejected earlier",
                "Stubs",
                "Pass",
            ),
            rows,
        )
    else:
        lines += ["The model declares no requirement.", ""]

    lines += _heading(SECTION_FINDINGS)
    findings = document["findings"]
    if findings["counts"]:
        lines.append("The search refuses none of these: it reports them, and `swcraft check-model` is the gate.")
        lines.append("")
        lines += _table(("Rule", "Class", "Count"), [[c["rule"], c["class"], c["count"]] for c in findings["counts"]])
        for finding in findings["trace"]:
            lines.append(f"- `{finding['rule']}` ({finding['class']}): {finding['message']}")
        if findings["trace"]:
            lines.append("")
    else:
        lines += ["No finding.", ""]

    entries = document["requirements"]

    def blocks(the_pass: str, title: str) -> list[str]:
        out: list[str] = []
        documents = [row["document"] for row in document["documents"] if row["pass"] == the_pass]
        for document_value in documents:
            listed = [
                entry
                for entry in entries
                if entry["pass"] == the_pass
                and entry["document"] == document_value
                and (the_pass == PASS_LABELLING or entry["candidates"])
            ]
            out.append(f"### {_document_title(document_value)}")
            out.append("")
            if the_pass == PASS_LABELLING:
                record = next(item for item in document["labelling"] if item["document"] == document_value)
                after = len(listed) - record["chosen"] - record["unselected"]
                out.append(
                    f"Budget {record['budget']}: {record['chosen']} to label first, listed first; after them "
                    f"{record['unselected']} more unlinked requirement{'' if record['unselected'] == 1 else 's'} "
                    f"and {after} linked one{'' if after == 1 else 's'}."
                )
                out.append("")
            if not listed:
                out += [f"No requirement {title}.", ""]
                continue
            for entry in listed:
                out += _requirement_lines(entry, links)
            out.append("")
        return out

    lines += _heading(SECTION_LABELLING)
    labelled = blocks(PASS_LABELLING, "chosen")
    lines += labelled or ["No document is labelled in this run.", ""]

    lines += _heading(SECTION_REVIEW)
    reviewed = blocks(PASS_REVIEW, "with a candidate")
    lines += reviewed or ["No document is reviewed in this run.", ""]

    lines += _heading(SECTION_NOTHING_FOUND)
    if document["nothingFound"]:
        for item in document["nothingFound"]:
            shown = ", ".join(_linked(rid, links.target("requirements", rid)) for rid in item["requirements"])
            lines.append(f"- {_document_title(item['document'])}: {shown}")
        lines.append("")
    else:
        lines += ["Every reviewed requirement has a candidate.", ""]

    lines += _heading(SECTION_RECOVERY)
    figures = document["recovery"]
    outside = figures["outsideScopeRequirements"]
    if figures["requirements"] == 0 and outside == 0:
        lines.append("No linked requirement in scope, nothing to recover.")
    elif figures["requirements"] == 0:
        lines.append("No linked requirement in scope has a link the search could recover.")
    else:
        count, pairs = figures["requirements"], figures["pairs"]
        lines.append(
            "Each linked requirement's own links were hidden in turn and the requirement ranked again. "
            "This measures the recovery of links an architect already set, a biased sample, and not the "
            "accuracy on unlinked requirements."
        )
        lines.append("")
        lines.append(f"- First place holds a linked group: {figures['firstPlace']} of the {count} linked requirements.")
        lines.append(f"- A place holds a linked group: {figures['inThePlaces']} of the {count} linked requirements.")
        lines.append(
            f"- The linked target's group is in the places: {figures['pairsOwnerLevel']} of the {pairs} links."
        )
        lines.append(f"- The linked target itself is a row: {figures['pairsExactTarget']} of the {pairs} links.")
    links = figures["outsideScopePairs"]
    if links:
        verb = "link points" if links == 1 else "links point"
        sentence = f"- {links} {verb} at targets outside the search's scope"
        if outside:
            sentence += f" ({outside} requirement{'' if outside == 1 else 's'} with no other link)"
        lines.append(sentence + "; they cannot be recovered and are in no figure.")
    lines.append("")

    lines += _heading(SECTION_LIMITS)
    lines += [f"- {limit}" for limit in STANDING_LIMITS]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


class PendingDecisionsError(TraceExperimentError):
    """The previous ``proposals.json`` holds decisions not applied, or cannot be read to tell.

    ``count`` is the pending decisions, ``None`` when the file cannot be read as the format. The search writes
    nothing; ``--discard-pending`` overwrites.
    """

    def __init__(self, file: str, count: int | None, reason: str | None = None) -> None:
        self.file = file
        self.count = count
        remedy = "Apply {} with `--apply`, or search again with `--discard-pending`."
        if count is None:
            message = (
                f"`{file}` cannot be read as a proposals file ({reason}), so it may hold decisions not applied. "
                + remedy.format("it")
            )
        else:
            noun = "decision" if count == 1 else "decisions"
            message = f"`{file}` holds {count} {noun} not applied. {remedy.format('them')}"
        super().__init__(message)


class ProposalsChangedError(TraceExperimentError):
    """The previous ``proposals.json`` changed while the search ran; nothing is written."""

    def __init__(self, file: str) -> None:
        self.file = file
        super().__init__(
            f"`{file}` changed while the search ran, so it may hold decisions not applied; nothing was written. "
            "Apply it with `--apply`, or search again (with `--discard-pending` to overwrite it)."
        )


def _proposals_bytes(path: Path) -> bytes | None:
    """The bytes of the file at *path*, ``None`` when it is absent; any other failure to read propagates."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def refuse_pending(path: Path, file: str) -> bytes | None:
    """Refuse when the proposals file at *path* holds pending work or cannot be read as the format; *file* names it.

    An absent file holds nothing. Pending is :func:`~swcraft_tools.trace_records.pending`: a decided item neither
    applied nor stale. Returns the bytes it judged (``None`` for an absent file), which
    :func:`refuse_changed_proposals` compares again before the replacement.
    """
    try:
        read = _proposals_bytes(path)
    except OSError as exc:
        reason = f"top level: cannot be read ({exc.strerror or type(exc).__name__})"
        raise PendingDecisionsError(file, None, reason) from None
    if read is None:
        return None
    try:
        proposals = trace_records.parse_proposals(read, file)
    except trace_records.TraceRecordsError as exc:
        raise PendingDecisionsError(file, None, f"{exc.pointer or 'top level'}: {exc.sentence}") from None
    count = trace_records.pending(proposals.model)
    if count:
        raise PendingDecisionsError(file, count)
    return read


def refuse_changed_proposals(path: Path, file: str, judged: bytes | None) -> None:
    """Refuse when the file at *path* no longer holds the bytes :func:`refuse_pending` judged (*judged*).

    Called after the new files are written under temporary names and before the first rename. **What this does
    not reach:** there is no lock, so an edit saved between this comparison and the rename of ``proposals.json`` is
    still overwritten.
    """
    try:
        current = _proposals_bytes(path)
    except OSError:
        raise ProposalsChangedError(file) from None
    if current != judged:
        raise ProposalsChangedError(file)


@dataclass(frozen=True)
class _Prepared:
    """What a search and ``--documents`` read before either searches (:func:`_prepare`)."""

    state: ModelState
    consistency: ConsistencyResult
    config: TraceConfig
    #: The configuration file's record in the result, ``None`` without one.
    configuration: dict[str, Any] | None
    rejected: frozenset[tuple[str, int, TargetAddress]]
    directory: Path
    #: The report directory, project-relative and POSIX.
    output: str


def _prepare(project: SwcraftProject, config_path: str | Path | None, labelling: str | None = None) -> _Prepared:
    """The search's run order up to its pending guard, refusing as :func:`run_trace` documents, writing nothing.

    The one prologue of :func:`run_trace` and :func:`run_trace_documents`, so the two refuse alike. *labelling*
    is ``--labelling``'s text, which replaces the configuration's ``search.labelling`` before its names are
    checked.
    """
    model_root = project.resolved_model_root
    state = load_model_state(model_root)
    consistency = check_cross_layer_consistency(str(model_root), project.resolved_arxml_version)
    refuse_ambiguous_identities(consistency)
    configuration: dict[str, Any] | None = None
    if config_path is None:
        config = TraceConfig()
    else:
        config, config_digest = load_config(str(config_path))
        relative = project_relative_name(project, config_path)
        configuration = {
            "file": Path(config_path).name if relative is None else relative,
            "insideProject": relative is not None,
            "sha256": config_digest,
        }
    if labelling is not None:
        config = labelling_override(config, labelling)
    check_config_names(config, state, "search.labelling" if labelling is None else "--labelling")
    rejected = trace_records.rejected_pairs(trace_records.read_decisions(project))
    try:
        if config.output is None:
            directory = experiment_default_directory(project, f"{experiment_frame.EXPERIMENTS_DIRECTORY}/{config.name}")
        else:
            directory = experiment_output_directory(project, config.output)
    except ValueError as exc:
        raise TraceExperimentError(str(exc)) from None
    output = directory.relative_to(project.resolved_project_root).as_posix()
    return _Prepared(state, consistency, config, configuration, rejected, directory, output)


@dataclass(frozen=True)
class TraceRun:
    """One completed run: its document and the three files it wrote."""

    document: dict[str, Any]
    report_path: Path
    result_path: Path
    proposals_path: Path

    @property
    def summary(self) -> str:
        return str(self.document["summary"])


def project_name_words(project: SwcraftProject) -> frozenset[str]:
    """The terms of the project's ``name``, which no requirement's query carries."""
    return frozenset(tokens(project.name))


def run_trace(
    project: SwcraftProject,
    config_path: str | Path | None,
    *,
    discard_pending: bool = False,
    labelling: str | None = None,
) -> TraceRun:
    """Read the model, refuse an ambiguous identity, read and check the configuration and the decisions, search, write.

    In that order, and **nothing is written** before the last step: a project that cannot be read raises the
    reader's own exception; an ambiguous identity (:data:`STOPPING_RULES`), a configuration file, a name it uses or
    an ``output`` refused raise :class:`TraceExperimentError` (the unreadable file its subclass, which the caller
    names); a malformed ``trace-decisions.yaml`` raises :class:`~swcraft_tools.trace_records.TraceRecordsError`
    naming its place; and a previous ``proposals.json`` in the report directory holding decisions not applied, or
    one that cannot be read as the format, raises :class:`PendingDecisionsError` unless *discard_pending*; the bytes
    that guard judged are compared again once the new files are written under temporary names, and a file changed
    meanwhile raises :class:`ProposalsChangedError`. Every other finding is reported and refuses nothing. The report
    directory is ``<dist_root>/experiments/<name>/`` or the directory ``output`` names. The three files are renamed
    in the order ``result.json``, ``proposals.json``, ``report.md``; a failed write leaves the previous run's files.
    **What this does not reach:** a rename refused between two of them leaves a mixed set; there is no lock, so an
    edit saved between that last comparison and the rename is overwritten; a proposals file under another ``name``
    or ``output`` is another file and is not read; and ``clean-outputs`` or a build removes the report directory
    without asking when it lies in ``dist_root`` or ``views_root``, the two directories it removes.
    """
    prepared = _prepare(project, config_path, labelling)
    state, consistency, config, directory, output = (
        prepared.state,
        prepared.consistency,
        prepared.config,
        prepared.directory,
        prepared.output,
    )
    model = experiment_frame.model_digest(project.resolved_model_root)
    result_path = directory / RESULT_FILENAME
    proposals_path = directory / PROPOSALS_FILENAME
    report_path = directory / REPORT_FILENAME
    proposals_file = f"{output}/{PROPOSALS_FILENAME}"
    last_look = None
    if not discard_pending:
        judged = refuse_pending(proposals_path, proposals_file)

        def last_look() -> None:
            refuse_changed_proposals(proposals_path, proposals_file, judged)

    outcome = search(state, config, consistency, prepared.rejected, project_name_words(project))
    command_line = None if labelling is None else {"labelling": _parameters(config, output)["search"]["labelling"]}
    document = result_document(
        config, outcome, model=model, output=output, configuration=prepared.configuration, command_line=command_line
    )
    links = read_report_links(project.resolved_model_root, directory)
    experiment_frame.write_report_files(
        directory,
        (
            (result_path, result_json(document)),
            (proposals_path, proposals_json(proposals_document(document))),
            (report_path, render_markdown(document, links)),
        ),
        last_look,
    )
    return TraceRun(document=document, report_path=report_path, result_path=result_path, proposals_path=proposals_path)


# ---------------------------------------------------------------------------
# --documents: the panel's hint before a search
# ---------------------------------------------------------------------------

#: The ``--documents`` answer's version; a reader refuses another one by name.
DOCUMENTS_SCHEMA_VERSION = 1
#: The ``--documents`` answer's kind.
DOCUMENTS_KIND = "trace-documents"


def run_trace_documents(project: SwcraftProject, config_path: str | Path | None) -> dict[str, Any]:
    """The Documents table a search would start from, and its proposals file, **writing nothing**.

    It refuses what a search refuses before it searches -- an unreadable project, an ambiguous identity, a
    configuration, a name or an ``output`` refused, a malformed ``trace-decisions.yaml`` -- and never the pending
    guard, since nothing is overwritten. ``documents`` is :func:`documents_table`, ``pass`` the one the
    configuration gives each document; ``proposals`` is the project-relative POSIX name of the proposals file the
    search would write, ``None`` when none is on disk. **What this does not reach:** the model is read once; an
    edit before the search changes the counts the search records, and its report's Documents table is the record.
    """
    prepared = _prepare(project, config_path)
    index = build_index(prepared.state, prepared.config, prepared.rejected)
    scope = requirements_in_scope(prepared.state, prepared.config)
    proposals_path = prepared.directory / PROPOSALS_FILENAME
    return {
        "schemaVersion": DOCUMENTS_SCHEMA_VERSION,
        "kind": DOCUMENTS_KIND,
        "proposals": f"{prepared.output}/{PROPOSALS_FILENAME}" if proposals_path.is_file() else None,
        "documents": documents_table(prepared.state, index, scope, prepared.config),
    }


def documents_text(answer: Mapping[str, Any]) -> str:
    """``--documents``'s text form: one line per document of the model, then the proposals file."""
    lines = []
    for row in answer["documents"]:
        if row["pass"] is None:
            state = "not in scope"
        else:
            state = (
                f"{row['pass']}: {row['inScope']} in scope, {row['linked']} linked, "
                f"{row['awaitingOnly']} awaiting confirmation, {row['withoutLink']} without a link, "
                f"{row['rejectedEarlier']} rejected earlier"
            )
        stubs = f"; {row['stubs']} stub{'' if row['stubs'] == 1 else 's'}" if row["stubs"] else ""
        lines.append(f"{_document_title(row['document'])}: {state}{stubs}")
    proposals = answer["proposals"]
    lines.append(f"Proposals file: {proposals}" if proposals is not None else "Proposals file: none yet")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# --labelling: the panel's passes for one run
# ---------------------------------------------------------------------------


def labelling_override(config: TraceConfig, text: str) -> TraceConfig:
    """*config* with ``search.labelling`` replaced by ``--labelling``'s JSON *text*, for this run only.

    The value is ``"all"`` or a list of document values, validated by the same field as the configuration file's
    key; a refusal names ``--labelling``. The configuration file is never written: the effective value is recorded
    in the run's own files (``fingerprint.commandLine`` and the parameters), so the run stays reproducible from
    the report.
    """
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TraceExperimentError(f'--labelling is not JSON ({exc.msg}): give "all" or a list of documents') from None
    except ValueError:  # an integer past Python's digit limit
        raise TraceExperimentError(
            '--labelling is refused: a number too long to read; give "all" or a list of documents'
        ) from None
    try:
        search = TraceSearch.model_validate({**config.search.model_dump(), "labelling": value})
    except ValidationError as exc:
        raise TraceExperimentError(
            "--labelling is refused: " + "; ".join(experiment_frame.refusal_lines(exc))
        ) from None
    return config.model_copy(update={"search": search})
