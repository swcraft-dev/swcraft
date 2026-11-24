"""The requirements import: one interchange file compared by id with the model's external requirements.

:func:`plan_requirements_import` decides everything first (reads the model, compares, versions and
validates every requirement the run would write, allocates every new file) and returns an
:class:`ImportPlan`, or raises one :class:`RequirementsImportRefusal` before anything is written,
naming every problem of the first stage that finds any (an architecture id or an id two files
declare first, then the storage and read-back problems). :func:`render_report` turns the plan into
the report the command prints, and :func:`apply_requirements_import` writes it, all or nothing.

- A need is compared **by id**; only ``source: external`` requirements are touched. An incoming or
  status-removed id an architecture requirement holds refuses the run, and so does an id two files
  declare that the run would rewrite, turn into a stub or remove.
- A delivery is a **complete set**: a held external requirement it does not import is removed by a
  ``removed_status``; else a non-stub one becomes a stub at ``+1`` while a surviving ``derived_from``
  names it, a held stub stays while that or a ``satisfies`` does, and each is removed otherwise.
- SWCraft numbers the versions: ``1`` for a new requirement, ``+1`` when ``title``, ``description``,
  ``asil``, ``req_type`` or the set of ``derived_from`` parents changes, not when a parent's version
  alone moves; ``document``, ``baseline`` and ``revision`` are overwritten (an absent one clears)
  without a bump. A new id is written above the highest ``@N`` a link the run leaves in the model
  names for it, skipping each ``@N`` a held link of an unchanged incoming need names.
- A parent is written at its version after the import; a parent found nowhere becomes a stub at its
  first version; a parent this run removes, and every ``satisfies`` and ``derived_from`` naming a
  removed id, is reported dangling.

Not reached: it runs no ``check-model``, writes no audit record, detects no hand edit and never
reads an element-form link (``hardware:Core0``) as a requirement. It assumes one requirements source
per model: a second source's delivery removes the first's requirements. Of an id two files declare,
the first file decides its case. The read-back proves only that each written text loads as the
requirement it was built from.
"""

from __future__ import annotations

import difflib
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import yaml
from pydantic import ValidationError

from swcraft_tools.exclusive_create import create_unique_sibling
from swcraft_tools.markdown_cells import heading_text, paragraph_start
from swcraft_tools.model_registry import ModelRegistry, StorageRecord, current_registry, path_refusal
from swcraft_tools.model_state import (
    ModelRootNotFoundError,
    PlannedStorage,
    StorageNameRefusal,
    parse_element,
    parse_label,
    refuse_retired_storage,
    scan_layer,
)
from swcraft_tools.reader import read_layer
from swcraft_tools.requirements_interchange import IgnoredNeed, Need, SelectedNeeds
from swcraft_tools.schema import Requirement, SWComponent
from swcraft_tools.trace import (
    LINKABLE_LABELS,
    LINKABLE_LAYERS,
    LinkedElement,
    element_link_sites,
    is_element_form,
    link_site_where,
    link_sites,
    parse_link,
)
from swcraft_tools.trace_terms import is_reported_term, numbers, words
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.yaml_load import load_yaml_document

#: The layer every file this import writes or removes belongs to.
_LAYER = "requirements"

#: The reasons a held external requirement leaves the model or becomes a stub, in the report's words.
ABSENT_REASON = "absent from this delivery"
STATUS_REASON = "status '{status}'"
IGNORED_TYPE_REASON = "type '{type}' not imported"
STUB_NOT_NAMED_REASON = "stub no longer named"

#: What the report says of a new id written above 1 because a link in the model still names it.
RETURNING_NOTE = "links from before its removal stay unconfirmed"

#: A stub's description, the parent's children sorted and joined by ", ".
STUB_DESCRIPTION = "Placeholder - named as a parent by {children}, not in the import."

#: The refusal of a run that has no delivery version to record: no ``--baseline`` and an empty
#: ``current_version``. Every delivery records its version, so a run without one writes nothing.
NO_DELIVERY_VERSION = (
    "the delivery records its version: pass --baseline (the requirements team's commit or baseline); "
    "the file's current_version is empty"
)


class RequirementsImportRefusal(ValueError):
    """The import refused whole: every problem found, each naming the id or the file it is about."""

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = tuple(problems)
        super().__init__(
            "the requirements import is refused and nothing was written:\n"
            + "\n".join(f"  {problem}" for problem in self.problems)
        )


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


class DeliveryVersion(NamedTuple):
    """The delivery's version the run records in every requirement the delivery holds, and its origin.

    ``given`` is ``True`` for a value given with ``--baseline``, which wins over
    the file, and ``False`` for the file's own ``current_version``.
    """

    text: str
    given: bool


def delivery_version(selected: SelectedNeeds, given: str | None = None) -> DeliveryVersion | None:
    """The delivery's version: *given* (``--baseline``), else the file's version, else ``None``."""
    if given is not None:
        return DeliveryVersion(given, given=True)
    if selected.baseline is not None:
        return DeliveryVersion(selected.baseline, given=False)
    return None


class PlannedFile(NamedTuple):
    """One file the apply writes: its model-root-relative POSIX target and its whole text."""

    target: str
    text: str


class Changed(NamedTuple):
    """A requirement whose compared fields or parent set moved: ``old`` -> ``new``, the fields named.

    ``detail`` is what moved, one report line each (:func:`_change_detail`);
    a replaced stub has none.
    """

    id: str
    old: int
    new: int
    fields: tuple[str, ...]
    detail: tuple[str, ...] = ()


class Stub(NamedTuple):
    """A stub this run creates, its version, and the children naming it."""

    id: str
    version: int
    children: tuple[str, ...]


class Removed(NamedTuple):
    """A requirement this run removes: its id, its last version, and why, in the report's words."""

    id: str
    version: int
    reason: str


@dataclass(frozen=True)
class ImportPlan:
    """Everything one import would do, decided; nothing written.

    ``writes`` and ``removals`` are what the apply touches, in plan order (every
    new file in the file's need order, then the stubs). The other fields are the
    report's sections, each in the order :func:`render_report` prints it.
    """

    model_root: Path
    baseline: DeliveryVersion
    #: The type of each need of a type the mapping ignores, in file order: the header's *Not imported*.
    ignored_types: tuple[str, ...]
    writes: tuple[PlannedFile, ...]
    removals: tuple[str, ...]
    added: tuple[tuple[str, int], ...]
    stubs: tuple[Stub, ...]
    changed: tuple[Changed, ...]
    removed: tuple[Removed, ...]
    #: ``(id, status)`` of each need removed by its status that the model does not hold, in file order.
    removed_by_status_not_in_model: tuple[tuple[str, str], ...]
    source_updated: tuple[tuple[str, tuple[str, ...]], ...]
    parent_versions_updated: tuple[tuple[str, tuple[str, ...]], ...]
    links_needing_confirmation: tuple[str, ...]
    links_confirmed: tuple[str, ...]
    links_dangling: tuple[str, ...]

    @property
    def changes_nothing(self) -> bool:
        """Whether the run writes and removes no file."""
        return not self.writes and not self.removals


class _Held(NamedTuple):
    """One requirement file of the model: its target and its element."""

    target: str
    requirement: Requirement


def _read_requirements(root: Path, registry: ModelRegistry) -> dict[str, list[_Held]]:
    """``id -> the files declaring it``, in file order -- one scan, each file parsed and validated."""
    label = parse_label(registry.layer(_LAYER))
    held: dict[str, list[_Held]] = {}
    for entry in scan_layer(root, _LAYER, registry=registry).files:
        requirement = parse_element(entry.path, label, Requirement.model_validate, root)
        held.setdefault(requirement.id, []).append(_Held(entry.path.relative_to(root).as_posix(), requirement))
    return held


def _parent_members(requirement: Requirement) -> set[str]:
    """The model's ``derived_from`` as a set of members.

    A requirement reference reduces to its id (``SG-1``, ``SG-1@3`` and ``SG-1@x`` are one parent);
    an element-form member stays as written.
    """
    return {value if is_element_form(value) else parse_link(value).id for value in requirement.derived_from}


def _serialize(requirement: Requirement) -> str:
    """The file text :func:`~swcraft_tools.writer.elements.write_element` writes for *requirement*."""
    return yaml.safe_dump(element_to_dict(requirement), default_flow_style=False, sort_keys=False)


def _read_back_problem(requirement: Requirement, text: str, *, need: bool) -> str | None:
    """Why *text* does not load back as *requirement*, naming the requirement and the field, or ``None``.

    Pydantic accepts a lone surrogate the dump escapes and the YAML reader
    refuses, so the import never writes a file the loader would refuse. The
    field is found by reading each key's dump on its own.
    """
    try:
        if Requirement.model_validate(load_yaml_document(text)) == requirement:
            return None
    except (yaml.YAMLError, ValidationError):
        pass
    what = f"need '{requirement.id}'" if need else f"stub '{requirement.id}'"
    for key, value in element_to_dict(requirement).items():
        try:
            if load_yaml_document(yaml.safe_dump({key: value}, default_flow_style=False)) == {key: value}:
                continue
        except yaml.YAMLError:
            pass
        # SWCraft's field, never a need key: under --mapping the key it was read from is the team's own.
        field = f"SWCraft's field '{key}'" if need else f"'{key}'"
        return f"{what}: {field} holds text the model's YAML reader cannot read back (a lone surrogate?)"
    return f"{what}: its file would not read back as the requirement it was built from"


def _requirement_from_need(need: Need, *, version: int, derived_from: list[str], baseline: str | None) -> Requirement:
    return Requirement.model_validate(
        {
            "id": need.id,
            "title": need.title,
            "description": need.description,
            "req_type": need.req_type,
            "version": version,
            "source": "external",
            "derived_from": derived_from,
            "asil": need.asil,
            "document": need.document,
            "baseline": baseline,
            "revision": need.revision,
        }
    )


def _compared(requirement: Requirement) -> dict[str, object]:
    """The compared fields by name, in ``model_fields`` order: a change of one is ``+1``."""
    return {
        "title": requirement.title,
        "description": requirement.description,
        "req_type": requirement.req_type,
        "asil": requirement.asil,
    }


def _provenance(requirement: Requirement) -> dict[str, object]:
    """The provenance fields by name: a change is written without a bump."""
    return {"document": requirement.document, "baseline": requirement.baseline, "revision": requirement.revision}


def _moved_compared(held: Requirement, candidate: Requirement) -> list[str]:
    """The compared fields that moved, the parent set among them: a non-empty list is ``+1``."""
    old, new = _compared(held), _compared(candidate)
    compared = [name for name in old if old[name] != new[name]]
    if "asil" in compared and candidate.asil is None:
        compared[compared.index("asil")] = "ASIL cleared"
    if _parent_members(held) != _parent_members(candidate):
        compared.append("derived_from")
    return compared


def _moved_provenance(held: Requirement, candidate: Requirement) -> list[str]:
    """The provenance fields that moved: written without a bump."""
    old, new = _provenance(held), _provenance(candidate)
    return [name for name in old if old[name] != new[name]]


def _term_key(term: str) -> tuple[str, str]:
    """The order a report lists terms and parents in: case-folded, then exact."""
    return (term.casefold(), term)


def _numbers_moved(old: list[str], new: list[str]) -> list[str]:
    """The numbers of two texts compared as sequences, one piece per run that differs."""
    pieces: list[str] = []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        before, after = old[old_start:old_end], new[new_start:new_end]
        if operation == "replace" and len(before) == len(after):
            pieces.append(", ".join(f"{a} → {b}" for a, b in zip(before, after, strict=True)))
        elif operation == "replace":
            pieces.append(f"{', '.join(before)} → {', '.join(after)}")
        elif operation == "delete":
            pieces.append(f"removed {', '.join(before)}")
        elif operation == "insert":
            pieces.append(f"added {', '.join(after)}")
    return pieces


def _digit_words(found: list[str]) -> Counter[str]:
    """The digit words of the numbers *found*, counted: ``-40`` and ``1.5`` give ``40``, and ``1`` and ``5``."""
    return Counter(digits for number in found for digits in re.findall(r"\d+", number))


def _text_detail(held: Requirement, candidate: Requirement) -> str | None:
    """The ``text:`` line: the numbers and the words that moved between the two texts, title first."""
    old_text, new_text = f"{held.title}\n{held.description}", f"{candidate.title}\n{candidate.description}"
    if old_text == new_text:
        return None
    parts: list[str] = []
    old_numbers, new_numbers = numbers(old_text), numbers(new_text)
    moved = _numbers_moved(old_numbers, new_numbers)
    if moved:
        parts.append(f"numbers {'; '.join(moved)}")
    # A digit word inside a number the number rule read is dropped once per occurrence, so a number is
    # reported under *numbers* and not twice; any other (the ``600`` of ``SR600``) stays a word.
    old_words = Counter(words(old_text)) - _digit_words(old_numbers)
    new_words = Counter(words(new_text)) - _digit_words(new_numbers)
    if added := sorted(filter(is_reported_term, new_words - old_words), key=_term_key):
        parts.append(f"terms added: {', '.join(added)}")
    if removed := sorted(filter(is_reported_term, old_words - new_words), key=_term_key):
        parts.append(f"terms removed: {', '.join(removed)}")
    if not parts:
        return (
            "text: wording only (the same terms and numbers; order, case, spacing, punctuation or common words changed)"
        )
    return f"text: {'; '.join(parts)}"


def _change_detail(held: Requirement, candidate: Requirement) -> tuple[str, ...]:
    """What moved in a changed requirement, one report line each: text, type, level, parents, each if moved."""
    lines: list[str] = []
    if (text := _text_detail(held, candidate)) is not None:
        lines.append(text)
    if held.req_type != candidate.req_type:
        lines.append(f"type: {held.req_type} → {candidate.req_type}")
    if held.asil != candidate.asil:
        if held.asil is None:
            lines.append(f"ASIL set: {candidate.asil}")
        elif candidate.asil is None:
            lines.append(f"ASIL cleared (was {held.asil})")
        else:
            lines.append(f"ASIL: {held.asil} → {candidate.asil}")
    old_parents, new_parents = _parent_members(held), _parent_members(candidate)
    parents: list[str] = []
    if added := sorted(new_parents - old_parents, key=_term_key):
        parents.append(f"added: {', '.join(added)}")
    if removed := sorted(old_parents - new_parents, key=_term_key):
        parents.append(f"removed: {', '.join(removed)}")
    if parents:
        lines.append(f"parents {'; '.join(parents)}")
    return tuple(lines)


class _CompleteSet(NamedTuple):
    """Step 4's outcome: what the run does to each held external requirement the delivery does not import."""

    #: Each id the run removes -> its reason: the status removals in file order, then the model's order.
    removed: dict[str, str]
    #: Each non-stub id the run turns into a stub -> its reason and the surviving requirements naming it.
    stubbed: dict[str, tuple[str, tuple[str, ...]]]


def _complete_set(
    held: dict[str, list[_Held]],
    incoming: list[Need],
    status_of: dict[str, str],
    ignored_type: dict[str, str],
    satisfied: set[str],
) -> _CompleteSet:
    """Step 4: the complete set's four cases, decided once against the requirements that survive the run.

    The surviving referrers are the delivery's needs, the architecture
    requirements (``derived_from``) and every element (``satisfies``, in
    *satisfied*). The held stubs are decided after the non-stub cases, since a
    stub names nothing. An architecture requirement is never a candidate, and
    only the first file of an id decides its case.
    """
    imported = {need.id for need in incoming}
    named_by: dict[str, set[str]] = {}
    for need in incoming:
        for parent in need.derived_from:
            named_by.setdefault(parent, set()).add(need.id)
    for files in held.values():
        for entry in files:
            if entry.requirement.source == "architecture":
                for parent in _parent_members(entry.requirement):
                    if not is_element_form(parent):
                        named_by.setdefault(parent, set()).add(entry.requirement.id)

    def candidate(requirement_id: str) -> Requirement | None:
        files = held.get(requirement_id)
        if requirement_id in imported or not files or files[0].requirement.source != "external":
            return None
        return files[0].requirement

    removed: dict[str, str] = {}
    stubbed: dict[str, tuple[str, tuple[str, ...]]] = {}
    for requirement_id, status in status_of.items():  # case 1, in file order
        if candidate(requirement_id) is not None:
            removed[requirement_id] = STATUS_REASON.format(status=status)
    for requirement_id in held:  # cases 2 and 3
        requirement = candidate(requirement_id)
        if requirement is None or requirement.stub or requirement_id in status_of:
            continue
        if requirement_id in ignored_type:
            reason = IGNORED_TYPE_REASON.format(type=ignored_type[requirement_id])
        else:
            reason = ABSENT_REASON
        if requirement_id in named_by:
            stubbed[requirement_id] = (reason, tuple(sorted(named_by[requirement_id])))
        else:
            removed[requirement_id] = reason
    for requirement_id in held:  # case 4
        requirement = candidate(requirement_id)
        if requirement is None or not requirement.stub or requirement_id in status_of:
            continue
        if requirement_id not in named_by and requirement_id not in satisfied:
            removed[requirement_id] = STUB_NOT_NAMED_REASON
    return _CompleteSet(removed, stubbed)


def _stub_requirement(requirement_id: str, children: Iterable[str], *, version: int) -> Requirement:
    """A stub of *requirement_id* at *version*, its description naming *children*, sorted, each once."""
    return Requirement.model_validate(
        {
            "id": requirement_id,
            "title": requirement_id,
            "description": STUB_DESCRIPTION.format(children=", ".join(sorted(set(children)))),
            "version": version,
            "source": "external",
            "stub": True,
        }
    )


#: The most digits a version this import writes may have: CPython's smallest settable limit on an
#: integer's decimal digits, so the text reads back under any ``PYTHONINTMAXSTRDIGITS``. A new id
#: above a larger ``@N`` a link names is refused.
_VERSION_DIGITS = sys.int_info.str_digits_check_threshold


def _named_versions(sites: Iterable[tuple[str, str]]) -> dict[str, int]:
    """The highest ``@N`` each requirement id is named at by *sites*; an element-form or bare member names none."""
    named: dict[str, int] = {}
    for _, value in sites:
        if is_element_form(value):
            continue
        link = parse_link(value)
        if link.version is not None:
            named[link.id] = max(link.version, named.get(link.id, 0))
    return named


class _Versions(NamedTuple):
    """Step 6's outcome: every version the plan writes or compares, worked out once."""

    #: Each incoming need's own version, from its compared fields alone.
    own: dict[str, int]
    #: The compared fields that moved, per incoming need held as a non-stub requirement.
    moved: dict[str, list[str]]
    #: Each parent found neither in the file nor in the model -> the needs naming it.
    stub_children: dict[str, list[str]]
    #: Every held id at its version before the import.
    before: dict[str, int]
    #: Every id the model holds after the import, at its version then.
    after: dict[str, int]

    def parent_member(self, parent: str) -> str:
        """*parent* as a ``derived_from`` member, at the version it names.

        Its version after the import, else its last (a parent this run removes), else bare (removed
        by status and never held: no version to name).
        """
        version = self.after.get(parent, self.before.get(parent))
        return parent if version is None else f"{parent}@{version}"


def _versions(
    incoming: list[Need],
    status_of: dict[str, str],
    complete: _CompleteSet,
    held_one: dict[str, _Held],
    named: dict[str, int],
    baseline: str,
) -> _Versions:
    """Step 6: each incoming need's own version, then each parent at its post-import version.

    A new id -- a need the model does not hold, or a new stub -- is written at the
    first version above the highest ``@N`` *named* holds for it (or from 1) that no
    link of an incoming need the run would otherwise leave unwritten names it at; a
    requirement the run turns into a stub is raised by one.
    """
    own: dict[str, int] = {}
    # The compared fields read no version, baseline or parent version, so step 7 reuses this list.
    moved: dict[str, list[str]] = {}
    # The versions each id is named at by a held incoming need with no compared or provenance change.
    # A new id written at one would leave that link unwritten and read as confirmed against a text it
    # was not made against; skipping these versions makes the member differ, so the need is rewritten.
    kept_at: dict[str, set[int]] = {}
    for need in incoming:
        entry = held_one.get(need.id)
        if entry is None:
            continue  # a new id: its version is chosen below, once every kept link is known
        if entry.requirement.stub:
            own[need.id] = entry.requirement.version + 1
            continue
        probe = _requirement_from_need(
            need, version=entry.requirement.version, derived_from=list(need.derived_from), baseline=baseline
        )
        moved[need.id] = _moved_compared(entry.requirement, probe)
        own[need.id] = entry.requirement.version + (1 if moved[need.id] else 0)
        if not moved[need.id] and not _moved_provenance(entry.requirement, probe):
            for value in entry.requirement.derived_from:
                link = None if is_element_form(value) else parse_link(value)
                if link is not None and link.version is not None:
                    kept_at.setdefault(link.id, set()).add(link.version)

    def first_free(requirement_id: str) -> int:
        version = named.get(requirement_id, 0) + 1
        while version in kept_at.get(requirement_id, ()):
            version += 1
        return version

    for need in incoming:
        if need.id not in held_one:
            own[need.id] = first_free(need.id)

    stub_children: dict[str, list[str]] = {}
    for need in incoming:
        for parent in need.derived_from:
            if parent not in own and parent not in held_one and parent not in status_of:
                stub_children.setdefault(parent, []).append(need.id)

    before: dict[str, int] = {requirement_id: entry.requirement.version for requirement_id, entry in held_one.items()}
    after: dict[str, int] = dict(before)
    after.update(own)
    after.update({parent: first_free(parent) for parent in stub_children})
    after.update({requirement_id: before[requirement_id] + 1 for requirement_id in complete.stubbed})
    for requirement_id in complete.removed:
        del after[requirement_id]
    return _Versions(own, moved, stub_children, before, after)


class _ReadModel(NamedTuple):
    """What step 1 reads of the model."""

    #: The model's requirements by id, with the files declaring each.
    held: dict[str, list[_Held]]
    swcs: list[SWComponent]
    #: Every element of the other linkable layers, with its layer.
    elements: list[tuple[str, LinkedElement]]


def _read_model(root: Path, registry: ModelRegistry) -> _ReadModel:
    """Step 1: the model's requirements with their files, its components, and the other linkable layers' elements."""
    held = _read_requirements(root, registry)
    swcs: list[SWComponent] = read_layer(str(root), LINKABLE_LAYERS["swc"], registry=registry)
    elements: list[tuple[str, LinkedElement]] = [
        (layer, element)
        for layer in LINKABLE_LAYERS.values()
        if layer != LINKABLE_LAYERS["swc"]
        for element in read_layer(str(root), layer, registry=registry)
    ]
    return _ReadModel(held, swcs, elements)


def _declaration_conflicts(selected: SelectedNeeds, held: dict[str, list[_Held]]) -> list[str]:
    """Step 3: an id two files declare, or an architecture requirement holds, that the file names."""
    problems: list[str] = []
    for need in (entry for entry in selected.in_file_order if not isinstance(entry, IgnoredNeed)):
        files = held.get(need.id, [])
        if len(files) > 1:
            problems.append(
                f"'{need.id}' is declared by {len(files)} files of the model "
                f"({', '.join(entry.target for entry in files)}): the import would update one and leave the other"
            )
        elif files and files[0].requirement.source == "architecture":
            problems.append(
                f"'{need.id}' is the id of an architecture requirement: the import changes only external "
                "requirements, and that requirement is left untouched"
            )
    return problems


def _complete_set_conflicts(
    held: dict[str, list[_Held]], complete: _CompleteSet, status_of: dict[str, str]
) -> list[str]:
    """Step 4's refusals: an id two files declare that the complete set would remove or turn into a stub."""
    problems: list[str] = []
    for requirement_id in [*complete.removed, *complete.stubbed]:
        files = held[requirement_id]
        if requirement_id not in status_of and len(files) > 1:
            what = "remove one" if requirement_id in complete.removed else "turn one into a stub"
            problems.append(
                f"'{requirement_id}' is declared by {len(files)} files of the model "
                f"({', '.join(entry.target for entry in files)}): the import would {what} and leave the other"
            )
    return problems


def _version_digit_problems(versions: _Versions) -> list[str]:
    """Step 6's refusals: an id the plan would write at a version no requirement file can hold."""
    return [
        f"'{requirement_id}' would be written at a version of more than {_VERSION_DIGITS} digits, which no "
        "requirement file holds: correct the links in the model that name it at such a version"
        for requirement_id, after in versions.after.items()
        if after != versions.before.get(requirement_id) and after >= 10**_VERSION_DIGITS
    ]


@dataclass
class _Staging:
    """Step 7's accumulator: the files the plan writes and the report's lists, filled by the staging steps in order.

    ``problems`` collects the storage and read-back refusals.
    """

    problems: list[str]
    writes: list[PlannedFile]
    storage: PlannedStorage
    added: list[tuple[str, int]]
    changed: list[Changed]
    source_updated: list[tuple[str, tuple[str, ...]]]
    parent_versions: list[tuple[str, tuple[str, ...]]]
    dangling_children: list[tuple[str, str]]  # (the child, the site)
    rewritten: set[str]  # the held ids this run writes from the file or turns into a stub
    record: StorageRecord
    stubs: list[Stub]

    def allocate(self, requirement_id: str) -> str | None:
        """The storage path for a new *requirement_id*, or ``None`` after recording why it has none."""
        refusal = path_refusal(self.record, requirement_id, layer=_LAYER)
        if refusal is not None:
            self.problems.append(f"'{requirement_id}' cannot be stored: {refusal}")
            return None
        try:
            return self.storage.allocate(_LAYER, requirement_id).path
        except StorageNameRefusal as exc:
            self.problems.append(str(exc))
            return None

    def stage(self, target: str | None, requirement: Requirement, *, need: bool) -> None:
        """Serialize *requirement*, read it back, and plan its write to *target* when both hold."""
        text = _serialize(requirement)
        problem = _read_back_problem(requirement, text, need=need)
        if problem is not None:
            self.problems.append(problem)
        elif target is not None:
            self.writes.append(PlannedFile(target, text))


def _begin_staging(root: Path, registry: ModelRegistry) -> _Staging:
    """An empty :class:`_Staging` over *root*'s requirements directory."""
    writes: list[PlannedFile] = []
    # One listing of the requirements directory, each planned file reserved in it, keeps allocation linear.
    storage = PlannedStorage(root, registry=registry)
    return _Staging([], writes, storage, [], [], [], [], [], set(), registry.storage(_LAYER), [])


def _stage_incoming(
    staged: _Staging,
    incoming: list[Need],
    status_of: dict[str, str],
    held_one: dict[str, _Held],
    versions: _Versions,
    baseline: str,
) -> None:
    """Step 7, the file's needs: stage each new or changed one and record how it differs from the model."""
    for need in incoming:
        members = [versions.parent_member(parent) for parent in need.derived_from]
        for parent, member in zip(need.derived_from, members, strict=True):
            if parent in status_of:
                staged.dangling_children.append((need.id, _derived_from_site(need.id, member)))
        candidate = _requirement_from_need(need, version=versions.own[need.id], derived_from=members, baseline=baseline)
        entry = held_one.get(need.id)
        if entry is None:
            staged.stage(staged.allocate(need.id), candidate, need=True)
            staged.added.append((need.id, candidate.version))
            continue
        before = entry.requirement
        if before.stub:
            staged.changed.append(Changed(need.id, before.version, candidate.version, ("stub replaced",)))
        elif compared := versions.moved[need.id]:
            staged.changed.append(
                Changed(
                    need.id,
                    before.version,
                    candidate.version,
                    (*compared, *_moved_provenance(before, candidate)),
                    _change_detail(before, candidate),
                )
            )
        elif provenance := _moved_provenance(before, candidate):
            staged.source_updated.append((need.id, tuple(provenance)))
        elif sorted(candidate.derived_from) != sorted(before.derived_from):
            # Same parents, a member's text moved (`SG-1@2` -> `SG-1@3`, `SG-1@x` -> `SG-1@2`, a parent
            # listed twice): compared as a multiset, so an order alone is no change.
            staged.parent_versions.append((need.id, tuple(members)))
        else:
            continue  # no write: its links are read from the model in step 9, like any other's
        staged.stage(entry.target, candidate, need=True)
        staged.rewritten.add(need.id)


def _stage_new_stubs(staged: _Staging, versions: _Versions) -> None:
    """Step 7, the parents found neither in the file nor in the model: a stub requirement each."""
    for parent, children in versions.stub_children.items():
        stub = _stub_requirement(parent, children, version=versions.after[parent])
        staged.stage(staged.allocate(parent), stub, need=False)
        staged.stubs.append(Stub(parent, stub.version, tuple(sorted(set(children)))))


def _stage_dropped_as_stubs(
    staged: _Staging, complete: _CompleteSet, held_one: dict[str, _Held], versions: _Versions
) -> None:
    """Step 7, the complete set's second case: a dropped held requirement a survivor still derives from."""
    for requirement_id, (reason, survivors) in complete.stubbed.items():
        entry = held_one[requirement_id]
        stub = _stub_requirement(requirement_id, survivors, version=versions.after[requirement_id])
        staged.stage(entry.target, stub, need=False)
        staged.changed.append(
            Changed(requirement_id, entry.requirement.version, stub.version, (f"replaced by a stub: {reason}",))
        )
        staged.rewritten.add(requirement_id)


def plan_requirements_import(
    model_root: str | Path,
    selected: SelectedNeeds,
    *,
    baseline: DeliveryVersion | None = None,
    registry: ModelRegistry | None = None,
) -> ImportPlan:
    """Decide everything importing *selected* into the model at *model_root* would do; write nothing.

    *baseline* is the delivery's version the caller decided (the CLI: ``--baseline``,
    else the file's); without one, the file's version is taken, and a file with
    none is refused. It is written into every requirement the delivery holds.

    Raises:
        RequirementsImportRefusal: every problem the plan found -- no delivery
            version (alone, before the model is read), an incoming or
            status-removed id an architecture requirement holds, an id two files
            declare that the run would rewrite, turn into a stub or remove, a
            version past :data:`_VERSION_DIGITS` digits (a new id a left link pins
            that high), an id the store cannot hold, a text that would not read back.
        ElementParseError, RetiredStorageError, ModelRootNotFoundError: the model
            cannot be read; each names the file or the directory it refuses.
    """
    registry = registry or current_registry()
    version = baseline or delivery_version(selected)
    if version is None:
        raise RequirementsImportRefusal([NO_DELIVERY_VERSION])
    root = Path(model_root)
    if not root.exists():
        raise ModelRootNotFoundError(root)
    refuse_retired_storage(root, registry=registry)

    # Step 1: the model -- the requirements with their files, and the linkable layers.
    model = _read_model(root, registry)

    # Step 2: the file, partitioned.
    status_of: dict[str, str] = {entry.id: entry.status for entry in selected.removed}
    ignored_type: dict[str, str] = {entry.id: entry.type for entry in selected.ignored}
    incoming: list[Need] = list(selected.needs)

    # Step 3: only external requirements are touched, and only one file per id.
    problems = _declaration_conflicts(selected, model.held)

    # Step 4: the delivery is a complete set -- every held external requirement it does not import.
    satisfied = {
        parse_link(value).id
        for _, value in _link_sites(model.swcs, model.elements, {}, set())
        if not is_element_form(value)
    }
    complete = _complete_set(model.held, incoming, status_of, ignored_type, satisfied)
    problems.extend(_complete_set_conflicts(model.held, complete, status_of))
    if problems:
        raise RequirementsImportRefusal(problems)

    # Step 5: the highest version each id is still named at, by every link the run leaves in the model:
    # none of a requirement it removes or turns into a stub, nor an incoming need's held parents,
    # which it rewrites from the delivery.
    leaving = {*complete.removed, *complete.stubbed, *(need.id for need in incoming)}
    named = _named_versions(_link_sites(model.swcs, model.elements, model.held, leaving))

    # Step 6: every version, worked out once, and none written past what YAML text can carry.
    held_one = {requirement_id: files[0] for requirement_id, files in model.held.items()}
    versions = _versions(incoming, status_of, complete, held_one, named, version.text)
    problems.extend(_version_digit_problems(versions))
    if problems:
        raise RequirementsImportRefusal(problems)

    # Step 7: every candidate, validated, read back and classified.
    staged = _begin_staging(root, registry)
    _stage_incoming(staged, incoming, status_of, held_one, versions, version.text)
    _stage_new_stubs(staged, versions)
    _stage_dropped_as_stubs(staged, complete, held_one, versions)
    if staged.problems:
        raise RequirementsImportRefusal(staged.problems)

    removed = tuple(
        Removed(requirement_id, held_one[requirement_id].requirement.version, reason)
        for requirement_id, reason in complete.removed.items()
    )
    removals = tuple(held_one[entry.id].target for entry in removed)
    removed_by_status_not_in_model = tuple(
        (entry.id, entry.status) for entry in selected.removed if entry.id not in held_one
    )

    # Step 9: the link sections, over the model before and the plan after. Every requirement the
    # run neither rewrites nor removes is read from the model, an unwritten incoming need included;
    # of a rewritten one only its removed parents are reported, from step 7. A site naming any id
    # the run removes is listed as naming no requirement.
    sites = _link_sites(model.swcs, model.elements, model.held, staged.rewritten | complete.removed.keys())
    unconfirmed, confirmed, dangling = _link_sections(sites, versions, complete.removed)
    dangling.extend(
        site for child, site in staged.dangling_children if child not in held_one or child in staged.rewritten
    )

    return ImportPlan(
        model_root=root,
        baseline=version,
        ignored_types=tuple(entry.type for entry in selected.ignored),
        writes=tuple(staged.writes),
        removals=removals,
        added=tuple(staged.added),
        stubs=tuple(staged.stubs),
        changed=tuple(staged.changed),
        removed=removed,
        removed_by_status_not_in_model=removed_by_status_not_in_model,
        source_updated=tuple(staged.source_updated),
        parent_versions_updated=tuple(staged.parent_versions),
        links_needing_confirmation=tuple(unconfirmed),
        links_confirmed=tuple(confirmed),
        links_dangling=tuple(dangling),
    )


def _link_sections(
    sites: Iterable[tuple[str, str]], versions: _Versions, removed: dict[str, str]
) -> tuple[list[str], list[str], list[str]]:
    """Step 9: the sites needing confirmation, confirmed by this import, and naming a removed id.

    A member without ``@N`` is neither confirmed nor unconfirmed, and an
    element-form member is not read as a requirement.
    """
    unconfirmed: list[str] = []
    confirmed: list[str] = []
    dangling: list[str] = []
    for site, value in sites:
        if is_element_form(value):
            continue
        link = parse_link(value)
        if link.id in removed:
            dangling.append(site)
            continue
        old, new = versions.before.get(link.id), versions.after.get(link.id)
        if link.version is None or new is None:
            continue
        if old is not None and link.version == old and link.version != new:
            unconfirmed.append(site)
        elif link.version != old and link.version == new:
            confirmed.append(site)
    return unconfirmed, confirmed, dangling


def _derived_from_site(requirement_id: str, value: str) -> str:
    """A ``derived_from`` member as the report names it, in the validator's words."""
    return f"Requirement '{requirement_id}' derives from '{value}'"


def _link_sites(
    swcs: list[SWComponent],
    elements: list[tuple[str, LinkedElement]],
    held: dict[str, list[_Held]],
    rewritten: set[str],
) -> Iterator[tuple[str, str]]:
    """``(the site as the report names it, the member)`` for every link the import does not rewrite.

    ``satisfies`` on the five linkable layers -- a component's own, its ports'
    and its runnables' -- and ``derived_from`` on every requirement the run
    neither writes from the file nor removes. A site is named as the
    validator's trace findings name it (:func:`~swcraft_tools.trace.link_site_where`).
    """
    for swc in swcs:
        for site in link_sites(swc):
            where = link_site_where(LINKABLE_LABELS[LINKABLE_LAYERS["swc"]], swc.name, site)
            yield f"{where} satisfies '{site.value}'", site.value
    for layer, element in elements:
        for site in element_link_sites(element):
            yield f"{link_site_where(LINKABLE_LABELS[layer], element.name, site)} satisfies '{site.value}'", site.value
    for requirement_id, files in held.items():
        if requirement_id in rewritten:
            continue
        for entry in files:
            for value in entry.requirement.derived_from:
                yield _derived_from_site(requirement_id, value), value


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def summary_line(plan: ImportPlan) -> str:
    """The header's last line, a fixed contract: the five counts, always present, in this order, ASCII only."""
    return (
        f"Summary: {len(plan.added)} added, {len(plan.changed)} changed, {len(plan.removed)} removed, "
        f"{len(plan.stubs)} stubs created, {len(plan.source_updated)} source updates"
    )


#: The characters :func:`visible` escapes: the C0 controls, DEL, the C1 controls and the two
#: Unicode separators -- every character ``str.splitlines`` breaks a line at among them.
_CONTROL_CHARACTERS = frozenset([*map(chr, range(0x20)), *map(chr, range(0x7F, 0xA0)), "\u2028", "\u2029"])
_NAMED_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def visible(text: str) -> str:
    """*text* with every control character written as a visible escape (``\\n``, ``\\x1b``, ``\\u2028``).

    Escaped, a value from the file or the model never starts a line of its own,
    so it cannot print a second ``Summary:`` line. A backslash the value already
    holds is printed as written: the escape is for reading, not reversing.
    """
    if not _CONTROL_CHARACTERS.intersection(text):
        return text
    return "".join(
        _NAMED_ESCAPES.get(char) or (f"\\x{ord(char):02x}" if ord(char) < 0x100 else f"\\u{ord(char):04x}")
        if char in _CONTROL_CHARACTERS
        else char
        for char in text
    )


def _plain(texts: Iterable[str]) -> list[tuple[str, tuple[str, ...]]]:
    """Report entries with no detail lines."""
    return [(text, ()) for text in texts]


def _first(version: int) -> str:
    """A new id's version as the report prints it: above 1 only when a link in the model still named the id."""
    return f"v{version}" if version == 1 else f"v{version}; {RETURNING_NOTE}"


def _path_as_spelled(path: str) -> str:
    """A path as the user spelled it, for the report: :func:`visible`, and a lone surrogate as ``\\udcXX``.

    POSIX argv decodes an undecodable byte as a lone surrogate, which no UTF-8
    text can carry; escaped, the ``--report`` file can still be written.
    """
    return visible(path).encode("utf-8", "backslashreplace").decode("utf-8")


def mapping_as_named(spelled: str, project_root: Path) -> str:
    """The mapping file as the report names it: relative to the project when it lies inside it.

    *spelled* is ``--mapping`` as given. Containment is decided by **filesystem
    identity** (:func:`os.path.samefile`), not by spelling, which fails on a
    case-insensitive volume: the first ancestor that is *project_root* gives the
    POSIX suffix below it. The ancestors of the path as spelled (absolute, no
    link followed) are tried first, then those of the resolved file; a suffix
    holding ``..`` is not taken, and an ``OSError`` ends that path's comparison.
    No such ancestor gives *spelled* unchanged: the report never fails on naming.

    **What it does not reach.** The refusal lines (``Error: <mapping>: ...``)
    name the mapping as spelled and never call this; a hard link to a file
    inside the project from outside it is named as spelled, since only the
    spelled and the resolved path's own ancestors are compared; and a spelling
    whose ``..`` steps out of a linked folder is named by its resolved path or
    as spelled.
    """
    for resolved in (False, True):
        try:
            candidate = Path(spelled).resolve() if resolved else Path(spelled).absolute()
            named = _named_below(candidate, project_root)
        except OSError:
            continue  # this path cannot be compared; the next one may
        if named is not None:
            return named
    return spelled


def _named_below(candidate: Path, project_root: Path) -> str | None:
    """*candidate* below the first of its ancestors that is *project_root* (by identity), as a POSIX path.

    ``None`` when no ancestor is, or when the suffix below it holds ``..``. An
    ``OSError`` of a check propagates to :func:`mapping_as_named`.
    """
    for ancestor in candidate.parents:
        if os.path.samefile(ancestor, project_root):
            suffix = candidate.relative_to(ancestor)
            return None if ".." in suffix.parts else suffix.as_posix()
    return None


class _ReportParts(NamedTuple):
    """The report's facts in order, each value already :func:`visible`: the one list both spellings take."""

    title: str
    #: The header's lines below the title: the mapping line, the not-imported line when the mapping
    #: ignored any need, the dry-run line, the summary line, and "No change.", each with whether it
    #: is the summary line, so the Markdown form bolds it by its place.
    header: list[tuple[str, bool]]
    #: Each section that counts: its title and its entries, an entry its line and its detail lines.
    sections: list[tuple[str, list[tuple[str, tuple[str, ...]]]]]


def _report_parts(plan: ImportPlan, *, dry_run: bool, mapping: str | None = None) -> _ReportParts:
    """The facts :func:`render_report` and :func:`render_report_markdown` both print, in their order.

    The header names the delivery's version -- followed by ``(from the file)`` when it is the
    file's ``current_version``, bare when it was given -- the mapping
    file as the user spelled it (*mapping*; ``None`` for SWCraft's own names) and,
    when the mapping ignored any, how many needs were not imported and their
    types; then ``Dry run``, then ``Summary:``, whose grammar is a fixed contract.
    """
    origin = "" if plan.baseline.given else " (from the file)"
    title = f"Requirements import: baseline {visible(plan.baseline.text)}{origin}"
    header: list[tuple[str, bool]] = [
        ("Mapping: none (SWCraft's own names)" if mapping is None else f"Mapping: {_path_as_spelled(mapping)}", False)
    ]
    if plan.ignored_types:
        types = ", ".join(visible(name) for name in sorted(set(plan.ignored_types)))
        count = len(plan.ignored_types)
        of_types = "of a type" if len(set(plan.ignored_types)) == 1 else "of types"
        needs = f"1 need {of_types}" if count == 1 else f"{count} needs {of_types}"
        header.append((f"Not imported: {needs} the mapping ignores ({types})", False))
    if dry_run:
        header.append(("Dry run: nothing was written.", False))
    header.append((summary_line(plan), True))
    if plan.changes_nothing:
        header.append(("No change.", False))
    sections: list[tuple[str, list[tuple[str, tuple[str, ...]]]]] = [
        ("Added", _plain(f"{requirement_id} ({_first(version)})" for requirement_id, version in plan.added)),
        (
            "Stubs created",
            _plain(f"{stub.id} ({_first(stub.version)}), named by {', '.join(stub.children)}" for stub in plan.stubs),
        ),
        ("Changed", [(f"{c.id} v{c.old} → v{c.new} ({', '.join(c.fields)})", c.detail) for c in plan.changed]),
        ("Removed", _plain(f"{entry.id} (was v{entry.version}; {entry.reason})" for entry in plan.removed)),
        (
            "Removed by status, not in the model",
            _plain(
                f"{rid} ({STATUS_REASON.format(status=status)})" for rid, status in plan.removed_by_status_not_in_model
            ),
        ),
        ("Source information updated", _plain(f"{rid} ({', '.join(fields)})" for rid, fields in plan.source_updated)),
        (
            "Parent versions updated",
            _plain(f"{rid} ({', '.join(members)})" for rid, members in plan.parent_versions_updated),
        ),
        ("Links now needing confirmation", _plain(plan.links_needing_confirmation)),
        ("Links confirmed by this import", _plain(plan.links_confirmed)),
        ("Links now naming no requirement", _plain(plan.links_dangling)),
    ]
    shown = [
        (name, [(visible(text), tuple(visible(line) for line in detail)) for text, detail in entries])
        for name, entries in sections
        if entries
    ]
    return _ReportParts(title, header, shown)


def render_report(plan: ImportPlan, *, dry_run: bool, mapping: str | None = None) -> str:
    """The report: the header ending in the summary line, then each section that counts, in a fixed order.

    Ids and element names are printed as written, except that every control
    character of a value from the file or the model is printed as a visible
    escape (:func:`visible`); no storage name is printed. The `→` is kept
    because the CLI forces UTF-8 streams.
    """
    parts = _report_parts(plan, dry_run=dry_run, mapping=mapping)
    lines = [parts.title, *(line for line, _ in parts.header)]
    for title, entries in parts.sections:
        lines.extend(["", f"{title}:"])
        for text, detail in entries:
            lines.append(f"  {text}")
            lines.extend(f"    {line}" for line in detail)
    return "\n".join(lines) + "\n"


def render_report_markdown(plan: ImportPlan, *, dry_run: bool, mapping: str | None = None) -> str:
    """The same report as Markdown (``import requirements --report PATH.md``), from the same facts.

    The title is the ``#`` heading; the mapping line, the not-imported line, the dry-run line,
    the summary line (in bold) and "No change." are paragraphs; each section that counts is a
    ``##`` heading over a list, a *Changed* entry's detail lines a list nested under it. Every
    text is literal Markdown, after :func:`visible`, so an id renders as written. Nothing is
    added to the text form and nothing of it left out. Newline-terminated.
    """
    parts = _report_parts(plan, dry_run=dry_run, mapping=mapping)
    lines = [f"# {heading_text(parts.title)}"]
    for line, is_summary in parts.header:
        text = paragraph_start(line)
        lines += ["", f"**{text}**" if is_summary else text]
    for title, entries in parts.sections:
        lines += ["", f"## {heading_text(title)}", ""]
        for text, detail in entries:
            lines.append(f"- {paragraph_start(text)}")
            lines.extend(f"  - {paragraph_start(line)}" for line in detail)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The apply
# ---------------------------------------------------------------------------


def create_staging(target: Path) -> Path:
    """Create an empty ``<target name>.<random>.tmp`` beside *target* that no other caller holds, and return it.

    A sibling, because ``os.replace`` is atomic only within one filesystem;
    created exclusively, following no link, with mode ``0o666`` less the umask
    (never ``0o600``, which the rename would carry onto the model file). The
    ``FileExistsError`` of the last retry propagates.
    """
    return create_unique_sibling(target.parent, target.name, ".tmp")


def apply_requirements_import(plan: ImportPlan) -> list[str]:
    """Write every planned file and remove every planned one, all or nothing; audit nothing.

    Every planned path's prior bytes are recorded; each file is written to a
    ``.tmp`` sibling of this call's own and ``os.replace``d onto its target; then
    each removal is unlinked. On any failure every replaced or removed path gets
    its bytes back, every created file, remaining ``.tmp`` and created directory
    is removed, and the original error is re-raised, with a note naming each
    model-root-relative path the rollback could not restore. Each file is byte
    for byte what ``write_element`` writes for the same requirement.

    **What it does not reach.** The rollback runs inside this process: a run
    killed between two renames, or a machine that loses power, leaves the
    renames done so far and a ``.tmp`` sibling or two -- git shows both. A
    restored file gets its bytes back and not its timestamps or permissions,
    and the apply holds no lock: a file changed by another process between the
    plan and the apply is overwritten as planned, and two concurrent imports
    race on the targets, the last rename winning.

    Returns:
        The model-root-relative targets written and then removed, in plan order.
    """
    root = plan.model_root
    writes = [(root / planned.target, planned.text) for planned in plan.writes]
    removals = [root / target for target in plan.removals]
    prior: dict[Path, bytes | None] = {
        path: path.read_bytes() if path.exists() else None for path in [*(path for path, _ in writes), *removals]
    }

    created_dirs: list[Path] = []  # directories the staging created, outermost first
    staged: list[tuple[Path, Path]] = []  # (the .tmp sibling, the target)
    done: list[Path] = []  # targets replaced or removed so far
    try:
        for path, text in writes:
            missing: list[Path] = []
            ancestor = path.parent
            while not ancestor.exists():
                missing.append(ancestor)
                ancestor = ancestor.parent
            path.parent.mkdir(parents=True, exist_ok=True)
            created_dirs.extend(reversed(missing))
            tmp_path = create_staging(path)
            # recorded before the write, so a partial file a failing write leaves is removed too
            staged.append((tmp_path, path))
            tmp_path.write_text(text, encoding="utf-8", newline="\n")
        for tmp_path, path in staged:
            os.replace(tmp_path, path)
            done.append(path)
        for path in removals:
            path.unlink()
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
        for directory in reversed(created_dirs):  # no entry is an ancestor of an earlier one
            try:
                directory.rmdir()
            except OSError:
                pass  # not empty, or already gone: never break a rollback
        if unrestored:
            exc.add_note(f"the rollback could not restore: {', '.join(unrestored)}")
        raise
    return [*(planned.target for planned in plan.writes), *plan.removals]
