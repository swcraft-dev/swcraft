"""Apply for the trace link search, ``swcraft experiment trace --apply``: the decisions become links.

``swcraft experiment trace`` writes ``proposals.json`` and nothing into the model; the architect decides in
that file; this module carries the decisions out. :func:`plan_apply` decides **everything** first and writes
nothing: it re-checks every decided item against a fresh read of the model by the fingerprints the search
recorded, builds every planned file's text and reads it back, and computes the report and the plan digest. The
staged write (:func:`apply_plan`) is a separate step, and a dry run stops before it.

**The re-check**, for each decided item not yet ``applied`` and not ``stale``, the first that holds deciding:
the requirement is declared by no file or by two; its fingerprint differs; the target resolves to no element or
to two; an accept or a ``chosen`` entry names an external stub; a row's target fingerprint differs; the link
or the reject record is already there (*already done*); a confirm finds neither its member nor the current form
(stale); otherwise it is planned. The proposals file records a stale reason (``stale``) or ``applied``.

**What one Apply writes**: each target's declaring file, edited in its ``satisfies`` lines by
:mod:`swcraft_tools.trace_satisfies_text` and read back equal; ``trace-decisions.yaml`` when a record is new or
changed; and the proposals file with its marks. It imports neither ``editing``, ``allocation`` nor ``scipy``.

**What it does not reach.** A ``chosen`` entry and a row's neighbour evidence carry no fingerprint; a link
already present is marked applied and recorded nowhere; a hand-formatted model file is rewritten in the
writer's form (the report lists it). A target file edited between the two model reads is checked against the
first and written from the second. The staged write holds no lock, and a run killed between two renames leaves
the renames done so far (the next Apply marks those links applied).
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import yaml
from pydantic import TypeAdapter, ValidationError

from swcraft_tools import experiment_frame
from swcraft_tools.markdown_cells import heading_text, inline_literal, table
from swcraft_tools.model_registry import ModelRegistry, current_registry
from swcraft_tools.model_state import ModelState, load_model_state, stored_element_files
from swcraft_tools.project import SwcraftProject, project_relative_name
from swcraft_tools.requirements_import import create_staging, visible
from swcraft_tools.safety import asil_rank, max_asil
from swcraft_tools.schema import ExternalInterface, Requirement, SWComponent
from swcraft_tools.trace import (
    LINKABLE_LABELS,
    LinkSite,
    component_requirement_ids,
    is_element_form,
    link_site_where,
    parse_link,
)
from swcraft_tools.trace_records import (
    ACCEPT,
    CONFIRM,
    DECISIONS_FILE,
    DECISIONS_FORMAT_VERSION,
    REJECT,
    Candidate,
    Chosen,
    DecisionRecord,
    Decisions,
    Linked,
    decision_of,
    decisions_text,
    entry_items,
    merge_decisions,
    read_decisions_bytes,
    read_proposals,
)
from swcraft_tools.trace_satisfies_text import HolderChange, edit_satisfies
from swcraft_tools.trace_search import (
    TargetAddress,
    TraceExperimentError,
    address_order_key,
    dump_json,
    exact_decimal,
    id_order_key,
    proposals_json,
    requirement_fingerprint,
    target_fingerprint,
)
from swcraft_tools.yaml_load import load_yaml_document

__all__ = [
    "ACCEPT",
    "APPLY_SCHEMA_VERSION",
    "CONFIRM",
    "DECISIONS_FILE",
    "DECISIONS_FORMAT_VERSION",
    "REJECT",
    "ApplyPlan",
    "ApplyRun",
    "Item",
    "PlannedText",
    "RaisedLevel",
    "TraceApplyError",
    "apply_plan",
    "plan_apply",
    "render_report",
    "render_report_markdown",
    "report_document",
    "run_apply",
    "run_time_text",
    "summary_line",
]

#: The JSON report's own version.
APPLY_SCHEMA_VERSION = 1

#: The layer that declares each target kind's element (a port's and a runnable's: its component's).
_LAYER_OF_KIND: Mapping[str, str] = {
    "swc": "swc",
    "port": "swc",
    "runnable": "swc",
    "interface": "interfaces",
    "composition": "compositions",
    "task": "tasks",
    "processor": "hardware",
    "core": "hardware",
    "memory": "hardware",
    "partition": "hardware",
}

#: The kind of the top-level element each linkable layer but ``hardware`` declares (a hardware element names its own).
_KIND_OF_LAYER: Mapping[str, str] = {
    "swc": "swc",
    "interfaces": "interface",
    "compositions": "composition",
    "tasks": "task",
}

#: The outcome of one decided item.
PLANNED = "planned"
ALREADY_DONE = "already done"
STALE = "stale"
#: A planned link whose holder's ``satisfies`` is in a form Apply does not edit: reported, neither written nor
#: marked, so it stays decided and pending until the file is edited by hand.
NOT_WRITTEN = "not written"

#: The reason an item marked ``applied`` by an earlier Apply is counted *already done*.
APPLIED_EARLIER = "applied by an earlier Apply"
#: The reason a reject whose record the decisions file already holds is counted *already done*: another
#: proposals file, or an Apply of this one, recorded it.
ALREADY_REJECTED = "already rejected"


class TraceApplyError(TraceExperimentError):
    """Apply refused, nothing written.

    A file outside the project, a planned text that does not read back, a plan that differs from
    ``--expect-plan``, or a file changed while Apply ran.
    """


# ---------------------------------------------------------------------------
# The plan's values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One decided item of the proposals file and what the re-check made of it.

    ``slot`` is ``candidates``, ``chosen`` or ``linked``; ``pointer`` is the item's RFC 6901 pointer in the file.
    ``member`` is the member an accept writes or a confirm writes in place of ``replaced``; ``reason`` is why an
    item is stale or already done; ``form`` is the ``satisfies`` form Apply does not edit, on a not-written item
    only.
    """

    requirement: str
    version: int
    target: TargetAddress
    slot: str
    pointer: str
    decision: str
    outcome: str
    member: str | None = None
    replaced: str | None = None
    reason: str | None = None
    preselected: bool = False
    form: str | None = None

    @property
    def marked(self) -> bool:
        """Whether this run marks the item in the proposals file: every item but one an earlier Apply marked."""
        return self.reason != APPLIED_EARLIER and self.outcome != NOT_WRITTEN


@dataclass(frozen=True)
class PlannedText:
    """One file the apply writes: its project-relative target, the bytes the plan read (``None``: absent), its text."""

    target: str
    path: Path
    prior: bytes | None
    text: str


@dataclass(frozen=True)
class RaisedLevel:
    """A component whose maximum allocated level the plan raises; ``declared`` is its own ``asil``."""

    component: str
    before: str
    after: str
    declared: str | None


@dataclass(frozen=True)
class ApplyPlan:
    """Everything one Apply would do, decided; nothing written.

    ``model_files`` are in the address order of the element each declares; ``decisions`` and ``proposals`` are
    ``None`` when the run writes no such file. ``proposals_prior`` and ``decisions_prior`` are the two files' bytes
    as read (``None``: the decisions file is absent), which the staged write compares even when the run does not
    rewrite them. ``consulted`` is every model file the re-check read, ``(project-relative target, path, bytes)``
    in the target order -- every file whose content decided an outcome -- which the staged write compares too.
    """

    project_root: Path
    name: str
    model_changed: bool
    proposals_file: str
    proposals_target: str
    proposals_location: Path
    proposals_prior: bytes
    decisions_location: Path
    decisions_prior: bytes | None
    consulted: tuple[tuple[str, Path, bytes], ...]
    items: tuple[Item, ...]
    undecided: int
    model_files: tuple[PlannedText, ...]
    decisions: PlannedText | None
    proposals: PlannedText | None
    records: tuple[DecisionRecord, ...]
    raised_levels: tuple[RaisedLevel, ...]
    digest: str

    def _of(self, decision: str | None, outcome: str) -> list[Item]:
        return [
            item for item in self.items if item.outcome == outcome and (decision is None or item.decision == decision)
        ]

    @property
    def writes(self) -> list[Item]:
        """The planned accepts, a row's or a ``chosen`` entry's."""
        return self._of(ACCEPT, PLANNED)

    @property
    def confirms(self) -> list[Item]:
        return self._of(CONFIRM, PLANNED)

    @property
    def rejects(self) -> list[Item]:
        return self._of(REJECT, PLANNED)

    @property
    def already_done(self) -> list[Item]:
        return self._of(None, ALREADY_DONE)

    @property
    def stale(self) -> list[Item]:
        return self._of(None, STALE)

    @property
    def not_written(self) -> list[Item]:
        """The planned links whose file form Apply does not edit."""
        return self._of(None, NOT_WRITTEN)

    @property
    def marks(self) -> int:
        """The marks this run writes into the proposals file: every item planned, found done or found stale now."""
        return sum(1 for item in self.items if item.marked)

    @property
    def changes_nothing(self) -> bool:
        """Whether the plan holds no operation: no model file, no decisions record and no mark."""
        return not self.model_files and not self.records and self.marks == 0

    def files(self) -> tuple[PlannedText, ...]:
        """Every file the apply writes, in the rename order: the model files, the decisions file, the proposals file."""
        return (
            *self.model_files,
            *((self.decisions,) if self.decisions is not None else ()),
            *((self.proposals,) if self.proposals is not None else ()),
        )


# ---------------------------------------------------------------------------
# Reading the model's files: a target found by its declaration
# ---------------------------------------------------------------------------


@dataclass
class _Declared:
    """One file declaring a top-level element: its location, the bytes read and the element parsed from them."""

    path: Path
    prior: bytes
    layer: str
    element: Any


@dataclass
class _Match:
    """A target found in one file: the file and the element holding ``satisfies`` (the top-level one or a member)."""

    declared: _Declared
    holder: Any


class _ModelFiles:
    """The linkable layers' files, listed once per layer and each read and parsed at most once.

    A file is the one :func:`~swcraft_tools.model_state.stored_element_files` says declares the identity --
    whatever its basename or ``~N`` suffix -- parsed from the bytes read here, which the staged write compares
    before it renames.
    """

    def __init__(self, model_root: Path, registry: ModelRegistry) -> None:
        self.model_root = model_root
        self.registry = registry
        self._stored: dict[str, dict[str, list[Any]]] = {}
        self._read: dict[Path, _Declared] = {}

    def _declared(self, layer: str, path: Path) -> _Declared:
        found = self._read.get(path)
        if found is None:
            data = path.read_bytes()
            adapter = TypeAdapter(self.registry.layer(layer).element_annotation)
            element = adapter.validate_python(load_yaml_document(data.decode("utf-8")))
            found = self._read[path] = _Declared(path, data, layer, element)
        return found

    def consulted(self) -> list[_Declared]:
        """Every file read so far, in the order read."""
        return list(self._read.values())

    def matches(self, address: TargetAddress) -> list[_Match]:
        """Every element *address* names, stubs included, one per declaring file."""
        layer = _LAYER_OF_KIND[address.kind]
        stored = self._stored.get(layer)
        if stored is None:
            stored = self._stored[layer] = stored_element_files(self.model_root, layer, registry=self.registry)
        identity = address.owner if address.owner is not None else address.name
        found: list[_Match] = []
        for entry in stored.get(identity, ()):
            declared = self._declared(layer, entry.path)
            element = declared.element
            if layer == "hardware" and element.kind != address.kind:
                continue
            if address.owner is None:
                found.append(_Match(declared, element))
                continue
            found.extend(_Match(declared, member) for member in _members(element, address))
        return found


def _members(element: Any, address: TargetAddress) -> list[Any]:
    """The ports or the runnables of the component *element* that the port's or the runnable's *address* names."""
    members = element.ports if address.kind == "port" else element.runnables
    return [member for member in members if member.name == address.name]


def _is_stub(match: _Match) -> bool:
    """Whether the target is an external stub: a stub component (its ports and runnables too) or an interface stub."""
    element = match.declared.element
    return isinstance(element, ExternalInterface) or (isinstance(element, SWComponent) and element.external)


# ---------------------------------------------------------------------------
# The words the report prints
# ---------------------------------------------------------------------------


def _kind_label(address: TargetAddress) -> str:
    """``Interface``, ``SWC``, ``Core``: a top-level target's kind as the report names it."""
    layer = _LAYER_OF_KIND[address.kind]
    return address.kind.capitalize() if layer == "hardware" else LINKABLE_LABELS[layer]


def _element_words(address: TargetAddress) -> str:
    """``port 'pBrakeTemp' of SWC 'BrakeController'``, ``Interface 'IBrake'``, ``Core 'Core0'``: a target named."""
    if address.owner is not None:
        return f"{address.kind} '{address.name}' of SWC '{address.owner}'"
    return f"{_kind_label(address)} '{address.name}'"


def _element_cell(address: TargetAddress) -> str:
    """The Markdown form's *Element (kind)* cell: ``pBrake (port of SWC Brake)``, ``IBrake (Interface)``."""
    if address.owner is not None:
        return f"{address.name} ({address.kind} of SWC {address.owner})"
    return f"{address.name} ({_kind_label(address)})"


def _site_words(address: TargetAddress) -> str:
    """The link site as :func:`~swcraft_tools.trace.link_site_where` names it."""
    layer = _LAYER_OF_KIND[address.kind]
    if address.owner is not None:
        site = LinkSite((), "", (address.kind, address.name))
        return link_site_where(LINKABLE_LABELS[layer], address.owner, site)
    return link_site_where(LINKABLE_LABELS[layer], address.name, LinkSite((), ""))


def _top_address(declared: _Declared) -> TargetAddress:
    """The address of the top-level element a file declares."""
    element = declared.element
    kind = element.kind if declared.layer == "hardware" else _KIND_OF_LAYER[declared.layer]
    return TargetAddress(kind, None, element.name)


def _member(identifier: str, version: int) -> str:
    return f"{identifier}@{exact_decimal(version)}"


def _linked(item: Candidate | Chosen | Linked, identifier: str) -> Linked:
    """*item* as the linked pair a confirm decision is read off.

    Raises:
        TypeError: *item* is another kind of item.
    """
    if not isinstance(item, Linked):
        raise TypeError(f"a confirm decision is read off a linked pair; {identifier} is not one")
    return item


# ---------------------------------------------------------------------------
# The re-check
# ---------------------------------------------------------------------------


@dataclass
class _Context:
    state: ModelState
    files: _ModelFiles
    requirements: dict[str, list[Requirement]] = field(default_factory=dict)


def _classify(
    context: _Context,
    entry: Any,
    slot: str,
    item: Candidate | Chosen | Linked,
    held_rejects: frozenset[tuple[str, int, TargetAddress]],
) -> tuple[str, str | None]:
    """``(outcome, reason)`` of one decided item, by the re-check's table in its order.

    *held_rejects* are the keys of the rejections the decisions file holds. A reject whose key it holds -- keyed by
    the version of the requirement the model holds, never the entry's own ``version`` -- is *already done*, after
    the *already linked* check: a link that stands still wins. A reject held at another version is not (a version
    re-opens the pair), nor is a held accept.
    """
    identifier = entry.id
    held = context.requirements.get(identifier, [])
    if not held:
        return STALE, f"{identifier} is no longer in the model"
    if len(held) > 1:
        return STALE, f"{identifier} is declared twice"
    requirement = held[0]
    if requirement_fingerprint(requirement) != entry.fingerprint:
        when = (
            "same version"
            if requirement.version == entry.version
            else f"now version {exact_decimal(requirement.version)}"
        )
        return STALE, f"{identifier} changed since the search ({when})"
    address = item.target.target()
    found = context.files.matches(address)
    if not found:
        return STALE, f"{_element_words(address)} is no longer in the model"
    if len(found) > 1:
        return STALE, f"{_element_words(address)} is declared twice"
    match = found[0]
    decision = decision_of(item)
    if decision == ACCEPT and _is_stub(match):
        return STALE, "an external stub is never linked"
    if isinstance(item, Candidate) and target_fingerprint(context.state, address) != item.fingerprint:
        return STALE, f"{_element_words(address)} changed since the search"
    held_members = list(match.holder.satisfies)
    if decision in (ACCEPT, REJECT):  # a reject of a pair linked since the search judges a link that stands
        for value in held_members:
            if not is_element_form(value) and parse_link(value).id == identifier:
                return ALREADY_DONE, f"already linked as {value}"
    if decision == REJECT and (identifier, requirement.version, address) in held_rejects:
        return ALREADY_DONE, ALREADY_REJECTED
    if decision == CONFIRM:
        if _linked(item, identifier).member not in held_members:
            if _member(identifier, requirement.version) in held_members:
                return ALREADY_DONE, "already confirmed"
            return STALE, "the link changed since the search"
    return PLANNED, None


# ---------------------------------------------------------------------------
# The planned texts
# ---------------------------------------------------------------------------


def _holder_in(element: Any, address: TargetAddress) -> Any:
    """The element of *address* inside the top-level *element* (itself, or its port or runnable)."""
    if address.owner is None:
        return element
    return _members(element, address)[0]


def _member_of(address: TargetAddress) -> tuple[str, str] | None:
    """The holder's place in its file for :mod:`~swcraft_tools.trace_satisfies_text`: ``None`` for the top level."""
    if address.owner is None:
        return None
    return ("ports" if address.kind == "port" else "runnables", address.name)


def _planned_texts(
    context: _Context, items: Sequence[Item], registry: ModelRegistry
) -> tuple[list[tuple[_Declared, str, Any]], dict[int, str]]:
    """``[(file, planned text, planned element)]`` in the address order, and the items whose holder is refused.

    Each file's text is edited in place -- only its ``satisfies`` lines change. An item whose holder's list is in a
    form :func:`~swcraft_tools.trace_satisfies_text.edit_satisfies` does not edit is returned in the second value
    (its index in *items*, the reason) and left out of the file and of the planned element. The edited text is
    then **read back** and compared with the planned element -- the element the file held with exactly the planned
    members added -- and any difference refuses the whole run.
    """
    planned_items = [
        (number, item)
        for number, item in enumerate(items)
        if item.outcome == PLANNED and item.decision in (ACCEPT, CONFIRM)
    ]
    by_file: dict[Path, tuple[_Declared, dict[TargetAddress, list[tuple[int, Item]]]]] = {}
    for number, item in planned_items:
        declared = context.files.matches(item.target)[0].declared
        holders = by_file.setdefault(declared.path, (declared, {}))[1]
        holders.setdefault(item.target, []).append((number, item))

    refused: dict[int, str] = {}
    planned: list[tuple[_Declared, str, Any]] = []
    for declared, holders in sorted(by_file.values(), key=lambda pair: address_order_key(_top_address(pair[0]))):
        addresses = sorted(holders, key=address_order_key)
        changes = []
        for address in addresses:
            change = HolderChange(_member_of(address))
            for _number, item in holders[address]:
                if item.decision == CONFIRM:
                    assert item.replaced is not None and item.member is not None
                    if (item.replaced, item.member) not in change.confirms:  # one member confirmed twice: once
                        change.confirms.append((item.replaced, item.member))
            for _number, item in sorted(
                (pair for pair in holders[address] if pair[1].decision == ACCEPT),
                key=lambda pair: id_order_key(pair[1].requirement),
            ):
                assert item.member is not None
                change.appends.append(item.member)
            changes.append(change)
        text, refused_holders = edit_satisfies(declared.prior.decode("utf-8"), changes)
        for position, reason in refused_holders.items():
            for number, _item in holders[addresses[position]]:
                refused[number] = reason
        kept = [address for position, address in enumerate(addresses) if position not in refused_holders]
        if not kept:
            continue
        element = declared.element.model_copy(deep=True)
        for address in kept:
            satisfies: list[str] = _holder_in(element, address).satisfies
            for change_old, change_new in changes[addresses.index(address)].confirms:
                # the old member goes; the current one takes its place unless present (the module refuses two copies)
                position = satisfies.index(change_old)
                del satisfies[position]
                if change_new not in satisfies:
                    satisfies.insert(position, change_new)
            satisfies.extend(changes[addresses.index(address)].appends)
        adapter = TypeAdapter(registry.layer(declared.layer).element_annotation)
        where = _element_words(_top_address(declared))
        try:
            read_back = adapter.validate_python(load_yaml_document(text))
        except (ValidationError, yaml.YAMLError) as exc:
            raise TraceApplyError(f"{where} would not read back as planned: {type(exc).__name__}") from None
        if read_back.model_dump() != element.model_dump():
            raise TraceApplyError(f"{where}: its planned file would not read back as the element it was built from")
        planned.append((declared, text, read_back))
    return planned, refused


def _raised_levels(context: _Context, planned: Sequence[tuple[_Declared, str, Any]]) -> list[RaisedLevel]:
    """Each component whose maximum allocated level rises under the plan (``trace.component_requirement_ids``)."""
    levels = {identifier: held[0].asil for identifier, held in context.requirements.items() if held and held[0].asil}

    def allocated(component: SWComponent) -> str:
        return max_asil(
            levels[identifier] for identifier in component_requirement_ids(component) if identifier in levels
        )

    raised: list[RaisedLevel] = []
    for declared, _, element in planned:
        if not isinstance(element, SWComponent):
            continue
        before, after = allocated(declared.element), allocated(element)
        if asil_rank(after) > asil_rank(before):
            raised.append(RaisedLevel(element.name, before, after, element.asil))
    return raised


def _records(items: Sequence[Item]) -> list[DecisionRecord]:
    """The decisions records the planned items make: each accept, reject and confirm, at the current version.

    One record per ``(requirement, version, target)``, the later replacing the earlier: two confirms of one
    requirement's two links on one target make one record.
    """
    records: dict[tuple[str, int, TargetAddress], DecisionRecord] = {}
    for item in items:
        if item.outcome != PLANNED:
            continue
        document: dict[str, Any] = {
            "requirement": item.requirement,
            "version": item.version,
            "target": item.target.to_json(),
            "decision": item.decision,
        }
        if item.preselected:
            document["preselected"] = True
        if item.decision == CONFIRM:
            document["from"] = item.replaced
        record = DecisionRecord.model_validate(document)
        records[record.key] = record
    return list(records.values())


def _new_records(held: Decisions, records: Sequence[DecisionRecord]) -> list[DecisionRecord]:
    """The records that are new to *held* or replace a different one: the ones that move the file."""
    present = {record.key: record.to_document() for record in held.decisions}
    return [record for record in records if present.get(record.key) != record.to_document()]


def _marked_document(document: Mapping[str, Any], items: Sequence[Item]) -> dict[str, Any]:
    """The proposals document with ``applied: true`` or ``stale: <reason>`` on each item this run marks."""
    marked = copy.deepcopy(dict(document))
    for item in items:
        if not item.marked:
            continue
        _, _, number, slot, index = item.pointer.split("/")
        target = marked["requirements"][int(number)][slot][int(index)]
        if item.outcome == STALE:
            target["stale"] = item.reason
        else:
            target["applied"] = True
    return marked


# ---------------------------------------------------------------------------
# The digest
# ---------------------------------------------------------------------------


def _canonical(value: Any) -> str:
    return dump_json(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def plan_digest(model_files: Sequence[PlannedText], records: Sequence[DecisionRecord], items: Sequence[Item]) -> str:
    """``sha256:`` over the canonical JSON of the planned operations: the files, the records and the marks."""
    operations = {
        "files": [
            {"target": planned.target, "sha256": hashlib.sha256(planned.text.encode("utf-8")).hexdigest()}
            for planned in model_files
        ],
        "records": [record.to_document() for record in records],
        "marks": [
            {"pointer": item.pointer, **({"stale": item.reason} if item.outcome == STALE else {"applied": True})}
            for item in items
            if item.marked
        ],
    }
    return f"sha256:{hashlib.sha256(_canonical(operations).encode('utf-8')).hexdigest()}"


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def plan_apply(project: SwcraftProject, proposals_path: str | Path, *, file: str | None = None) -> ApplyPlan:
    """Decide everything one Apply of the proposals file at *proposals_path* would write; write nothing.

    In this order: refuse a file outside the project before reading it (Apply marks that file); read and validate
    it; read the decisions file or its absence; read the model; classify every decided item; build every planned
    text and read it back; compute the digest. *file* is how a refusal names the proposals file, the path as given
    by default.

    Raises:
        TraceApplyError: the file lies outside the project, or a planned text does not read back.
        TraceRecordsError: the proposals or the decisions file is malformed or cannot be read.
    """
    name = str(proposals_path) if file is None else file
    relative = project_relative_name(project, proposals_path)
    if relative is None:
        raise TraceApplyError(
            f"{name} lies outside the project: Apply marks the decisions it carries out in that file, "
            "so it must be inside the project directory"
        )
    project_root = Path(project.resolved_project_root)
    location = project_root / relative
    proposals = read_proposals(location, name)
    held_path, held_prior, held = read_decisions_bytes(project)

    model_root = Path(project.resolved_model_root)
    registry = current_registry()
    state = load_model_state(model_root, registry=registry)
    context = _Context(state, _ModelFiles(model_root, registry))
    for requirement in state["requirements"]:
        context.requirements.setdefault(requirement.id, []).append(requirement)
    held_rejects = frozenset(record.key for record in held.decisions if record.decision == REJECT)

    items: list[Item] = []
    undecided = 0
    for entry, number, slot, index, item in entry_items(proposals.model):
        if isinstance(item, Linked) and item.confirmed:
            continue  # a link at its requirement's version: nothing to decide, so not undecided either
        decision = decision_of(item)
        if decision is None:
            undecided += 1
            continue
        pointer = f"/requirements/{number}/{slot}/{index}"
        address = item.target.target()
        base: dict[str, Any] = {
            "requirement": entry.id,
            "target": address,
            "slot": slot,
            "pointer": pointer,
            "decision": decision,
            "preselected": decision == ACCEPT and isinstance(item, Candidate) and item.preselected is True,
        }
        if item.stale is not None:
            continue  # terminal: reported by the Apply that marked it
        if item.applied is not None:
            items.append(Item(version=entry.version, outcome=ALREADY_DONE, reason=APPLIED_EARLIER, **base))
            continue
        outcome, reason = _classify(context, entry, slot, item, held_rejects)
        held_requirements = context.requirements.get(entry.id, [])
        current = held_requirements[0].version if len(held_requirements) == 1 else entry.version
        member = replaced = None
        if outcome == PLANNED and decision != REJECT:
            member = _member(entry.id, current)
            if decision == CONFIRM:
                replaced = _linked(item, entry.id).member
        items.append(Item(version=current, outcome=outcome, reason=reason, member=member, replaced=replaced, **base))

    planned, refused = _planned_texts(context, items, registry)
    for number, form in refused.items():
        unwritten = items[number]
        items[number] = dataclasses.replace(
            unwritten,
            outcome=NOT_WRITTEN,
            form=form,
            reason=_not_written_sentence(unwritten, form, _element_words(unwritten.target)),
        )
    model_files = tuple(
        PlannedText(declared.path.relative_to(project_root).as_posix(), declared.path, declared.prior, text)
        for declared, text, _ in planned
    )
    new_records = _new_records(held, _records(items))
    decisions = None
    if new_records:
        text = decisions_text(merge_decisions(held, new_records))
        decisions = PlannedText(DECISIONS_FILE, held_path, held_prior, text)
    proposals_text = None
    if any(item.marked for item in items):
        proposals_text = PlannedText(
            relative, location, proposals.data, proposals_json(_marked_document(proposals.document, items))
        )
    digest = plan_digest(model_files, new_records, items)
    return ApplyPlan(
        project_root=project_root,
        name=proposals.model.name,
        model_changed=experiment_frame.model_digest(model_root)[0] != proposals.model.model.digest,
        proposals_file=name,
        proposals_target=relative,
        proposals_location=location,
        proposals_prior=proposals.data,
        decisions_location=held_path,
        decisions_prior=held_prior,
        consulted=tuple(
            sorted(
                (
                    (declared.path.relative_to(project_root).as_posix(), declared.path, declared.prior)
                    for declared in context.files.consulted()
                ),
                key=lambda read: read[0],
            )
        ),
        items=tuple(items),
        undecided=undecided,
        model_files=model_files,
        decisions=decisions,
        proposals=proposals_text,
        records=tuple(new_records),
        raised_levels=tuple(_raised_levels(context, planned)),
        digest=digest,
    )


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def summary_line(plan: ApplyPlan) -> str:
    """The fixed line the panel parses: the seven counts and the file count, always present, in this order, ASCII."""
    return (
        f"Summary: {len(plan.writes)} written, {len(plan.confirms)} confirmed, {len(plan.rejects)} rejected, "
        f"{len(plan.already_done)} already done, {len(plan.stale)} stale, {len(plan.not_written)} not written, "
        f"{plan.undecided} undecided; "
        f"{len(plan.model_files)} files"
    )


def _level_text(raised: RaisedLevel) -> str:
    text = f"SWC '{raised.component}': allocated level {raised.before} → {raised.after}"
    return text if raised.declared is None else f"{text} (it declares {raised.declared})"


class _Section(NamedTuple):
    """One section of the report, one item per entry: its titles, the text's entries, the table's rows.

    A section has as many table rows as printed lines; neither is escaped yet. The section's row count is its line
    of the Markdown form's *Result* table unless ``counted`` is false.
    """

    title: str
    columns: tuple[str, ...]
    printed: list[str]
    rows: list[tuple[str, ...]]
    #: The Markdown form's title for a dry run (``None``: the text form's) and for an applied run (``None``: the
    #: dry run's).
    preview_title: str | None = None
    applied_title: str | None = None
    counted: bool = True

    def heading(self, *, dry_run: bool) -> str:
        """The Markdown form's title of this section, and its *Result* table label."""
        preview = self.title if self.preview_title is None else self.preview_title
        if dry_run or self.applied_title is None:
            return preview
        return self.applied_title


class _ReportParts(NamedTuple):
    """The report's facts in order beyond what the plan itself names: the one list both spellings take."""

    #: The text form's header lines below its title: the dry-run line, the summary line, "No change." and the plan
    #: line.
    header: list[str]
    #: The rows of the Markdown form's *Result* table, each with its two labels (dry run, applied), in order.
    counts: list[tuple[str, str, int]]
    #: Each section that counts, in the report's order.
    sections: list[_Section]


def _by_hand(item: Item, quote: str) -> str:
    """What to do by hand for a not-written *item*: add its member, or replace the member it confirms.

    Each member stands between *quote*s.
    """
    if item.decision == ACCEPT:
        return f"add {quote}{item.member}{quote} by hand"
    return f"replace {quote}{item.replaced}{quote} with {quote}{item.member}{quote} by hand"


def _not_written_sentence(item: Item, form: str, element: str) -> str:
    """Why *item* is not written, naming *element*: the sentence the text form and the JSON document carry."""
    todo = _by_hand(item, "'")
    return f"the satisfies list of {element} is in a form Apply does not edit ({form}); {todo}"


def _not_written_cell(item: Item, form: str) -> str:
    """Why *item* is not written, as the Markdown form's cell, whose row names the element already."""
    return f"its satisfies list is {form}, a form Apply does not edit; {_by_hand(item, '')}"


def _short_reason(item: Item) -> str:
    """A not-written item's reason without the element its row names: the form, then what to do by hand."""
    if item.form is None:
        return item.reason or ""
    return _not_written_cell(item, item.form)


def _version_text(member: str | None) -> str:
    """The version a ``satisfies`` member names, as written: ``REQ@2`` -> ``2``.

    ``none`` for a member that names none (a bare ``REQ``), so a malformed suffix (``REQ@x``) prints as it stands
    and never as the identifier.
    """
    if member is None:
        return "none"
    suffix = parse_link(member).suffix
    return "none" if suffix is None else suffix


def _report_parts(plan: ApplyPlan, *, dry_run: bool) -> _ReportParts:
    """The facts :func:`render_report` and :func:`render_report_markdown` both print, in the report's order.

    No storage name and no path is among them. The ``Summary:`` and ``Plan:`` lines are the text form's and keep
    their exact text, because the panel parses stdout; the Markdown form states the same counts in its *Result*
    table.
    """
    header: list[str] = []
    if dry_run:
        header.append("Dry run: nothing was written.")
    header.append(summary_line(plan))
    if plan.changes_nothing:
        header.append("No change.")
    header.append(f"Plan: {plan.digest}")
    linked = ("Element (kind)", "Requirement", "Version")
    reasoned = ("Element (kind)", "Requirement", "Reason")
    sections = [
        _Section(
            "Links to write",
            linked,
            [f"{_site_words(item.target)} satisfies '{item.member}'" for item in plan.writes],
            [(_element_cell(item.target), item.requirement, exact_decimal(item.version)) for item in plan.writes],
            applied_title="Links written",
        ),
        _Section(
            "Links to confirm",
            linked,
            [f"{_site_words(item.target)} satisfies '{item.replaced}' → '{item.member}'" for item in plan.confirms],
            [
                (
                    _element_cell(item.target),
                    item.requirement,
                    f"{_version_text(item.replaced)} → {_version_text(item.member)}",
                )
                for item in plan.confirms
            ],
            applied_title="Links confirmed",
        ),
        _Section(
            "Rejections to record",
            ("Element (kind)", "Requirement"),
            [
                f"{_member(item.requirement, item.version)} rejected for {_site_words(item.target)}"
                for item in plan.rejects
            ],
            [(_element_cell(item.target), item.requirement) for item in plan.rejects],
            applied_title="Rejections recorded",
        ),
        _Section(
            "Already done",
            reasoned,
            [f"{item.requirement} on {_site_words(item.target)}: {item.reason}" for item in plan.already_done],
            [(_element_cell(item.target), item.requirement, item.reason or "") for item in plan.already_done],
        ),
        _Section(
            "Stale",
            reasoned,
            [f"{item.requirement} on {_site_words(item.target)}: {item.reason}; search again" for item in plan.stale],
            [(_element_cell(item.target), item.requirement, f"{item.reason}; search again") for item in plan.stale],
            preview_title="Stale (skipped)",
        ),
        _Section(
            "Allocated levels raised",
            ("Component", "Allocated level before", "After", "Declared"),
            [_level_text(raised) for raised in plan.raised_levels],
            [(raised.component, raised.before, raised.after, raised.declared or "—") for raised in plan.raised_levels],
            counted=False,
        ),
        _Section(
            "Links not written (edit by hand)",
            reasoned,
            [f"{item.requirement} on {_site_words(item.target)}: {item.reason}" for item in plan.not_written],
            [(_element_cell(item.target), item.requirement, _short_reason(item)) for item in plan.not_written],
            preview_title="Not written (edit by hand)",
        ),
    ]
    counts = [
        (section.heading(dry_run=True), section.heading(dry_run=False), len(section.rows))
        for section in sections
        if section.counted
    ]
    counts += [
        ("Still undecided", "Still undecided", plan.undecided),
        ("Files to change", "Files changed", len(plan.model_files)),
    ]
    return _ReportParts(header=header, counts=counts, sections=[section for section in sections if section.printed])


def render_report(plan: ApplyPlan, *, dry_run: bool) -> str:
    """The report: the header ending in the summary and plan lines, then each section that counts, in order.

    Every value taken from the file or the model is printed through the requirements import's visible-escape rule,
    so only the report's own lines start a line; no storage name and no path is printed.
    """
    parts = _report_parts(plan, dry_run=dry_run)
    title = f"Trace apply: proposals of the search '{plan.name}'"
    if plan.model_changed:
        title += " (the model has changed since the search)"
    lines = [visible(title), *parts.header]
    for section in parts.sections:
        lines.extend(["", f"{section.title}:"])
        lines.extend(f"  {visible(entry)}" for entry in section.printed)
    return "\n".join(lines) + "\n"


def run_time_text(now: datetime) -> str:
    """*now* as the Markdown report's time line names it: ``2026-11-09 13:06 (UTC+01:00)``, local time and offset."""
    offset = now.strftime("%z")
    if not offset:
        raise ValueError("the run's time carries its UTC offset")
    return f"{now.strftime('%Y-%m-%d %H:%M')} (UTC{offset[:3]}:{offset[3:5]})"


def _cell(value: str) -> str:
    return inline_literal(visible(value))


def render_report_markdown(plan: ApplyPlan, *, dry_run: bool, now: datetime) -> str:
    """The report document (``experiment trace --apply FILE --report PATH.md``), from the same facts.

    The title *Trace Apply Report — search '<name>'*; the time line -- *Applied <time>.* or *Preview made <time> —
    nothing was written.*, *now* being the run's own clock (:func:`run_time_text`), the one fact the text form
    does not print; *The model has changed since the search.* when it has; *No change.* when nothing would be
    written; the *Result* table of the summary's counts, in the future tense for a dry run and the past for an
    applied run; then a table per section that counts, one row per entry the text form prints; the plan digest
    last, small, for ``--expect-plan``. Every value taken from the file or the model is :func:`visible`, then
    literal Markdown. The text form's ``Summary:`` and ``Plan:`` lines, which the panel reads from stdout, are not
    repeated here. Newline-terminated.
    """
    parts = _report_parts(plan, dry_run=dry_run)
    when = run_time_text(now)
    title = f"Trace Apply Report — search '{plan.name}'"
    lines = [f"# {heading_text(visible(title))}", ""]
    lines.append(f"Preview made {when} — nothing was written." if dry_run else f"Applied {when}.")
    if plan.model_changed:
        lines += ["", "The model has changed since the search."]
    if plan.changes_nothing:
        lines += ["", "No change."]
    counts = [(preview if dry_run else applied, str(count)) for preview, applied, count in parts.counts]
    lines += ["", "## Result", "", *table(("Result", "Count"), counts)]
    for section in parts.sections:
        rows = [tuple(_cell(value) for value in row) for row in section.rows]
        lines += ["", f"## {section.heading(dry_run=dry_run)}", "", *table(section.columns, rows)]
    lines += ["", f"<small>Plan digest, for --expect-plan: {plan.digest}</small>"]
    return "\n".join(lines) + "\n"


def _item_document(item: Item) -> dict[str, Any]:
    document: dict[str, Any] = {
        "requirement": item.requirement,
        "version": item.version,
        "target": item.target.to_json(),
    }
    if item.replaced is not None:
        document["from"] = item.replaced
    if item.member is not None:
        document["member"] = item.member
    if item.reason is not None:
        document["reason"] = item.reason
    if item.decision == ACCEPT and item.preselected:
        document["preselected"] = True
    return document


def report_document(plan: ApplyPlan, *, dry_run: bool) -> dict[str, Any]:
    """The ``--format json`` document: the summary's counts and every section's items; no path."""
    return {
        "schemaVersion": APPLY_SCHEMA_VERSION,
        "kind": "trace-apply",
        "dryRun": dry_run,
        "plan": plan.digest,
        "summary": {
            "written": len(plan.writes),
            "confirmed": len(plan.confirms),
            "rejected": len(plan.rejects),
            "alreadyDone": len(plan.already_done),
            "stale": len(plan.stale),
            "notWritten": len(plan.not_written),
            "undecided": plan.undecided,
            "files": len(plan.model_files),
            "noChange": plan.changes_nothing,
        },
        "write": [_item_document(item) for item in plan.writes],
        "confirm": [_item_document(item) for item in plan.confirms],
        "reject": [_item_document(item) for item in plan.rejects],
        "alreadyDone": [_item_document(item) for item in plan.already_done],
        "stale": [_item_document(item) for item in plan.stale],
        "raisedLevels": [
            {"component": raised.component, "before": raised.before, "after": raised.after, "declared": raised.declared}
            for raised in plan.raised_levels
        ],
        "notWritten": [_item_document(item) for item in plan.not_written],
    }


# ---------------------------------------------------------------------------
# The staged write and the run
# ---------------------------------------------------------------------------


def apply_plan(plan: ApplyPlan) -> list[str]:
    """Write every file of *plan*, all or nothing; audit nothing.

    Every text is written to a ``.tmp`` sibling created exclusively; then the current bytes of every file the plan
    read -- each model file the re-check read, the decisions file or its absence, the proposals file -- are
    compared with the bytes the plan was computed from, and a difference refuses the run (*the file X changed
    while Apply ran; run it again*); then each staging file is renamed onto its target in :meth:`ApplyPlan.files`'
    order. On any failure every replaced path gets its recorded bytes back (a created one is removed), every
    ``.tmp`` is removed, and the original error is re-raised with a note naming each project-relative path the
    rollback could not restore.

    **What it does not reach.** Nothing is locked: a write between the comparison and the renames is overwritten;
    the requirement files the plan read are not compared, since it rewrites none of them; a target found in no file
    is stale, and a file declaring it saved after the re-check is not seen; and the rollback runs inside this
    process, so a run killed between two renames leaves the renames done so far and a ``.tmp`` or two, which git
    shows.

    Returns:
        The project-relative targets written, in the rename order.
    """
    files = plan.files()
    if not files:
        return []
    root = plan.project_root
    read: list[tuple[str, Path, bytes | None]] = [
        *plan.consulted,
        (DECISIONS_FILE, plan.decisions_location, plan.decisions_prior),
        (plan.proposals_target, plan.proposals_location, plan.proposals_prior),
    ]
    prior = {planned.path: planned.prior for planned in files}

    staged: list[tuple[Path, Path]] = []
    done: list[Path] = []
    try:
        for planned in files:
            tmp_path = create_staging(planned.path)
            # recorded before the write, so a partial file a failing write leaves is removed too
            staged.append((tmp_path, planned.path))
            tmp_path.write_text(planned.text, encoding="utf-8", newline="\n")
        for target, path, expected in read:
            try:
                current: bytes | None = path.read_bytes()
            except FileNotFoundError:
                current = None
            if current != expected:
                raise TraceApplyError(f"the file {target} changed while Apply ran; run it again")
        for tmp_path, path in staged:
            os.replace(tmp_path, path)
            done.append(path)
    except BaseException as exc:
        unrestored: list[str] = []
        for path in done:
            bytes_before = prior[path]
            try:
                if bytes_before is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(bytes_before)
            except OSError:  # one failed restore never stops the others
                unrestored.append(path.relative_to(root).as_posix())
        for tmp_path, _ in staged:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                unrestored.append(tmp_path.relative_to(root).as_posix())
        if unrestored:
            exc.add_note(f"the rollback could not restore: {', '.join(unrestored)}")
        raise
    return [planned.target for planned in files]


@dataclass(frozen=True)
class ApplyRun:
    """One Apply: its plan, its report in three forms, and the targets written.

    The report is rendered before the write, with its JSON form; its Markdown form is rendered before the write
    too when the run was asked for it (``--report PATH.md``), else ``None``.
    """

    plan: ApplyPlan
    dry_run: bool
    report: str
    document: dict[str, Any]
    written: tuple[str, ...]
    markdown: str | None


def _clock() -> datetime:
    """The run's own clock: local time with its UTC offset."""
    return datetime.now().astimezone()


def run_apply(
    project: SwcraftProject,
    proposals_path: str | Path,
    *,
    dry_run: bool,
    expect_plan: str | None = None,
    file: str | None = None,
    now: datetime | None = None,
    markdown: bool = False,
) -> ApplyRun:
    """Plan, render, check ``--expect-plan``, and write unless *dry_run*, in that order.

    The report is rendered **before** the write and handed back for the caller to print **after** it, so a
    rolled-back run prints no report of a change that did not happen. *file* is how a refusal names the proposals
    file. *markdown* asks for the Markdown form as well (``--report PATH.md``), rendered before the write too;
    without it :attr:`ApplyRun.markdown` is ``None``. *now* is the time the Markdown form names, read only with
    *markdown*; ``None`` reads the run's own clock once, as the run starts.

    Raises:
        TraceApplyError: a refusal of :func:`plan_apply`, a plan whose digest differs from *expect_plan*, or a file
            changed while Apply ran.
        TraceRecordsError: the proposals or the decisions file is malformed.
        OSError: a failed write, rolled back (its note names what was not restored).
    """
    when = (now if now is not None else _clock()) if markdown else None
    plan = plan_apply(project, proposals_path, file=file)
    report = render_report(plan, dry_run=dry_run)
    markdown_text = None if when is None else render_report_markdown(plan, dry_run=dry_run, now=when)
    document = report_document(plan, dry_run=dry_run)
    if expect_plan is not None and expect_plan != plan.digest:
        raise TraceApplyError("the plan changed since the dry run; run the dry run again")
    written: list[str] = [] if dry_run else apply_plan(plan)
    return ApplyRun(plan, dry_run, report, document, tuple(written), markdown_text)
