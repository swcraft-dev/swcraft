"""The requirement traceability rules.

Holds :func:`check_requirement_traceability`, which mints the ``trace.*`` rules
other than ``trace.link-unknown-requirement`` (the composer's own), and
:func:`requirement_allocated`, the allocation classification it shares with the
``allocation`` computed value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.rendering import render_model_scalar
from swcraft_tools.safety import parse_asil
from swcraft_tools.schema import Composition, Hardware, Interface, Requirement, SWComponent, Task
from swcraft_tools.trace import (
    LINKABLE_LAYERS,
    DuplicateLink,
    duplicate_links,
    duplicate_parents,
    element_link_sites,
    is_element_form,
    is_version,
    link_site_where,
    link_sites,
    parse_element_reference,
    parse_link,
    requirement_stubs,
    requirement_versions,
    satisfying_elements_of,
    stale_link,
    unpublishable_link_id,
)
from swcraft_tools.validator.model_view import ModelInput, as_view
from swcraft_tools.validator.results import ConsistencyIssue, ConsistencyResult, consistency_result, deduplicated

# ---------------------------------------------------------------------------
# check_requirement_traceability
# ---------------------------------------------------------------------------


def _derived_from_problem(
    value: str, versions: Mapping[str, Sequence[int]], element_names: Mapping[str, AbstractSet[str]]
) -> tuple[str, str] | None:
    """What a ``derived_from`` member names wrongly, as ``(problem, target)``; ``None`` when it resolves.

    *versions* maps every declared requirement id to its files' versions and
    *element_names* every linkable layer to its top-level names. The target is
    the member itself for an element reference and the id for every other form.
    """
    reference = parse_element_reference(value)
    if reference is not None:
        layer, name = reference
        if name in element_names[layer]:
            return None
        return f"no element of the '{layer}' layer is named '{name}'", value
    requirement_id, _version, suffix = parse_link(value)
    if is_element_form(value):
        return f"'{value.partition(':')[0]}' is the token of no linkable layer", requirement_id
    if requirement_id not in versions:
        return f"no requirement declares '{requirement_id}'", requirement_id
    if suffix is not None and not is_version(suffix):
        return f"'{suffix}' after '@' is not a version", requirement_id
    return None


def _derivation_cycles(graph: Mapping[str, list[str]]) -> list[tuple[str, ...]]:
    """The cycles an iterative DFS over *graph* meets, one per back edge.

    Each cycle is rotated to start at its smallest id, in first-met order, each once. *graph* maps a requirement id
    to the declared ids it derives from. Iterative, so a chain deeper than the recursion limit is walked like any
    other.
    """
    state: dict[str, str] = {}
    cycles: dict[tuple[str, ...], None] = {}
    for root in sorted(graph):
        if root in state:
            continue
        state[root] = "gray"
        path = [root]
        pending = [iter(graph[root])]
        while pending:
            neighbor = next(pending[-1], None)
            if neighbor is None:
                state[path.pop()] = "black"
                pending.pop()
            elif state.get(neighbor) == "gray":
                members = path[path.index(neighbor) :]
                first = members.index(min(members))
                cycles.setdefault(tuple(members[first:] + members[:first]), None)
            elif neighbor not in state:
                state[neighbor] = "gray"
                path.append(neighbor)
                pending.append(iter(graph[neighbor]))
    return list(cycles)


def check_requirement_traceability(model_root: ModelInput) -> ConsistencyResult:
    """Check the requirement traceability rules other than the composer's ``trace.link-unknown-requirement``.

    Reads the five linkable layers and ``requirements`` and mints ``trace.requirement-unallocated``,
    ``trace.requirement-stub``, ``trace.link-version-not-stated``, ``trace.link-needs-confirmation``,
    ``trace.derived-from-unknown-target``, ``trace.derived-from-cycle``, ``trace.decomposition-context-mismatch``,
    ``trace.duplicate-link``, ``trace.link-unpublishable-id`` and ``trace.requirement-blank-text``; the model
    contract describes each. The six warnings are the first two, the two version rules and the last two.

    Beyond the contract:

    * the two requirement rules give one finding per id, in id order; a stub is never unallocated;
    * the version rules abstain on an id resolving to no requirement or to two files, and on a non-version suffix;
    * a cycle is one finding per back edge of a DFS, named from its smallest id; one sharing a requirement with a
      reported cycle may surface only once that is broken, but a model with a cycle is never reported clean;
    * the decomposition rule abstains on an element, unlevelled or unresolved parent and on a plain child;
    * a duplicate link is reported at every member after the first;
    * the unpublishable-id rule skips an element-form member and an incoming requirement's ``derived_from``, and an
      incoming requirement's text is not judged.

    The decomposition itself (the pair a level was split into and its independence) is checked nowhere, and an
    element-form member is resolved by name in its layer, never below the top level.

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult; the six warning rules' findings are in ``warnings``.
    """
    view = as_view(model_root)
    index = _build_trace_index(
        view.requirements, view.swcs, view.interfaces, view.compositions, view.tasks, view.hardware
    )
    records = _allocation_records(index)
    records += _link_version_records(index)
    records += _derived_from_target_records(index)
    records += _derivation_cycle_records(index)
    records += _decomposition_context_records(index)
    records += _duplicate_link_records(index)
    records += _unpublishable_link_records(index)
    records += _blank_text_records(index.requirements)
    return consistency_result(deduplicated(records))


@dataclass(frozen=True)
class _TraceIndex:
    """The layers :func:`check_requirement_traceability` reads, and what it builds of them before its first finding.

    Attributes:
        by_id: every file declaring an id, in reader order.
        versions: every declared requirement id to its files' versions.
        element_names: every linkable layer to its top-level names.
    """

    requirements: Sequence[Requirement]
    swcs: Sequence[SWComponent]
    interfaces: Sequence[Interface]
    compositions: Sequence[Composition]
    tasks: Sequence[Task]
    hardware: Sequence[Hardware]
    by_id: Mapping[str, Sequence[Requirement]]
    versions: Mapping[str, Sequence[int]]
    element_names: Mapping[str, AbstractSet[str]]


def _build_trace_index(
    requirements: Sequence[Requirement],
    swcs: Sequence[SWComponent],
    interfaces: Sequence[Interface],
    compositions: Sequence[Composition],
    tasks: Sequence[Task],
    hardware: Sequence[Hardware],
) -> _TraceIndex:
    """The :class:`_TraceIndex` over the six layers :func:`check_requirement_traceability` read."""
    by_id: dict[str, list[Requirement]] = {}
    for requirement in requirements:
        by_id.setdefault(requirement.id, []).append(requirement)
    element_names: dict[str, set[str]] = {
        "swc": {swc.name for swc in swcs},
        "interfaces": {interface.name for interface in interfaces},
        "compositions": {composition.name for composition in compositions},
        "tasks": {task.name for task in tasks},
        "hardware": {element.name for element in hardware},
    }
    return _TraceIndex(
        requirements=requirements,
        swcs=swcs,
        interfaces=interfaces,
        compositions=compositions,
        tasks=tasks,
        hardware=hardware,
        by_id=by_id,
        versions=requirement_versions(requirements),
        element_names=element_names,
    )


def _allocation_records(index: _TraceIndex) -> list[ConsistencyIssue]:
    """The two allocation warnings of :func:`check_requirement_traceability`: an unsatisfied and a stub requirement."""
    requirements = index.requirements
    swcs = index.swcs
    interfaces = index.interfaces
    compositions = index.compositions
    tasks = index.tasks
    hardware = index.hardware
    versions = index.versions
    records: list[ConsistencyIssue] = []

    # A stub abstains from the allocation rule: nothing satisfies a placeholder.
    stubs = requirement_stubs(requirements)

    satisfied = satisfying_elements_of(swcs, interfaces, compositions, tasks, hardware).keys()
    for requirement_id in sorted(versions):
        if requirement_allocated(requirement_id, satisfied, stubs) is not False:
            continue
        stated = versions[requirement_id]
        # Rendered, never `str()`: a hexadecimal `version: 0x…` loads as an integer too wide for a decimal string.
        version = render_model_scalar(stated[0]) if len(stated) == 1 else "<version>"
        records.append(
            ConsistencyIssue(
                rule="trace.requirement-unallocated",
                message=(
                    f"Requirement '{requirement_id}' is satisfied by no element: add `satisfies: "
                    f"[{requirement_id}@{version}]` on the component, port, runnable, composition, interface, task "
                    "or hardware element that realizes it."
                ),
                subjects=(Subject("requirements", requirement_id),),
            )
        )

    for requirement_id in sorted(stubs):
        records.append(
            ConsistencyIssue(
                rule="trace.requirement-stub",
                message=f"Requirement '{requirement_id}' is a stub: named as a parent, never imported.",
                subjects=(Subject("requirements", requirement_id),),
            )
        )
    return records


def _version_findings(
    subject: Subject, where: str, verb: str, value: str, versions: Mapping[str, Sequence[int]]
) -> list[ConsistencyIssue]:
    """The two version warnings on one requirement-form member: its finding, or none.

    The classification is :func:`stale_link`'s: it abstains on a member whose id
    is declared by no file or by two, and on a suffix that is not a version.
    """
    stale = stale_link(value, versions)
    if stale is None:
        return []
    requirement_id = stale.requirement_id
    shown = render_model_scalar(stale.current)
    if stale.stated is None:
        message = (
            f"{where} {verb} '{value}' without a version: requirement '{requirement_id}' is at version "
            f"{shown}; write '{requirement_id}@{shown}' once the link is confirmed against it."
        )
        return [
            ConsistencyIssue(
                rule="trace.link-version-not-stated", message=message, subjects=(subject,), target=requirement_id
            )
        ]
    message = (
        f"{where} {verb} '{value}', but requirement '{requirement_id}' is at version {shown}: confirm "
        f"the link against version {shown} and write '{requirement_id}@{shown}'."
    )
    return [
        ConsistencyIssue(
            rule="trace.link-needs-confirmation", message=message, subjects=(subject,), target=requirement_id
        )
    ]


def _link_version_records(index: _TraceIndex) -> list[ConsistencyIssue]:
    """The version warnings of :func:`check_requirement_traceability`, per site in the contract's layer order."""
    requirements = index.requirements
    swcs = index.swcs
    interfaces = index.interfaces
    compositions = index.compositions
    tasks = index.tasks
    hardware = index.hardware
    versions = index.versions
    records: list[ConsistencyIssue] = []
    for swc in swcs:
        for site in link_sites(swc):
            where = link_site_where("SWC", swc.name, site)
            records += _version_findings(Subject("swc", swc.name, site.steps), where, "satisfies", site.value, versions)
    for interface in interfaces:
        for site in element_link_sites(interface):
            subject = Subject("interfaces", interface.name, site.steps)
            where = link_site_where("Interface", interface.name, site)
            records += _version_findings(subject, where, "satisfies", site.value, versions)
    for requirement in requirements:
        for value in requirement.derived_from:
            if parse_element_reference(value) is not None:
                continue
            subject = Subject("requirements", requirement.id, (field_step("derived_from"), member_step(value)))
            records += _version_findings(subject, f"Requirement '{requirement.id}'", "derives from", value, versions)
    for composition in compositions:
        for site in element_link_sites(composition):
            subject = Subject("compositions", composition.name, site.steps)
            where = link_site_where("Composition", composition.name, site)
            records += _version_findings(subject, where, "satisfies", site.value, versions)
    for task in tasks:
        for site in element_link_sites(task):
            subject = Subject("tasks", task.name, site.steps)
            records += _version_findings(
                subject, link_site_where("Task", task.name, site), "satisfies", site.value, versions
            )
    for element in hardware:
        for site in element_link_sites(element):
            subject = Subject("hardware", element.name, site.steps)
            where = link_site_where("Hardware element", element.name, site)
            records += _version_findings(subject, where, "satisfies", site.value, versions)
    return records


def _derived_from_target_records(index: _TraceIndex) -> list[ConsistencyIssue]:
    """``trace.derived-from-unknown-target`` of :func:`check_requirement_traceability`."""
    versions = index.versions
    element_names = index.element_names
    records: list[ConsistencyIssue] = []
    grammar = (
        "A member is '<id>@<version>', the version a positive integer, the bare '<id>', or '<token>:<name>' naming "
        f"a top-level element, the token one of {', '.join(LINKABLE_LAYERS)}."
    )
    for requirement in index.requirements:
        for value in requirement.derived_from:
            found = _derived_from_problem(value, versions, element_names)
            if found is None:
                continue
            problem, target = found
            records.append(
                ConsistencyIssue(
                    rule="trace.derived-from-unknown-target",
                    message=f"Requirement '{requirement.id}' derives from '{value}': {problem}. {grammar}",
                    subjects=(
                        Subject("requirements", requirement.id, (field_step("derived_from"), member_step(value))),
                    ),
                    target=target,
                )
            )
    return records


def _derivation_cycle_records(index: _TraceIndex) -> list[ConsistencyIssue]:
    """``trace.derived-from-cycle`` of :func:`check_requirement_traceability`."""
    versions = index.versions
    records: list[ConsistencyIssue] = []
    graph: dict[str, list[str]] = {}
    for requirement in index.requirements:
        edges = graph.setdefault(requirement.id, [])
        for value in requirement.derived_from:
            if parse_element_reference(value) is None:
                parent = parse_link(value).id
                if parent in versions and parent not in edges:
                    edges.append(parent)
    for cycle in _derivation_cycles(graph):
        records.append(
            ConsistencyIssue(
                rule="trace.derived-from-cycle",
                message=(
                    f"Requirements derive from each other in a cycle: {' -> '.join((*cycle, cycle[0]))}. Remove "
                    "one `derived_from` member of the cycle."
                ),
                # Every requirement on the cycle: no single one is the offender.
                subjects=tuple(
                    Subject("requirements", requirement_id, (field_step("derived_from"),))
                    for requirement_id in sorted(cycle)
                ),
            )
        )
    return records


def _decomposition_context_records(index: _TraceIndex) -> list[ConsistencyIssue]:
    """``trace.decomposition-context-mismatch`` of :func:`check_requirement_traceability`."""
    by_id = index.by_id
    records: list[ConsistencyIssue] = []
    for requirement in index.requirements:
        if requirement.asil is None:
            continue
        _level, context = parse_asil(requirement.asil)
        if context is None:
            continue
        for value in requirement.derived_from:
            if parse_element_reference(value) is not None:
                continue
            parent_id = parse_link(value).id
            for parent in by_id.get(parent_id, ()):
                if parent.asil is None:
                    continue
                parent_level, parent_context = parse_asil(parent.asil)
                expected = parent_context or parent_level
                if context == expected:
                    continue
                records.append(
                    ConsistencyIssue(
                        rule="trace.decomposition-context-mismatch",
                        message=(
                            f"Requirement '{requirement.id}' is rated '{requirement.asil}' and derives from "
                            f"requirement '{parent_id}', rated '{parent.asil}': the level in parentheses is the level "
                            f"before decomposition, which is '{expected}' for that parent, not '{context}'. Write "
                            f"'{expected}' in the parentheses, or re-point `derived_from` at the requirement "
                            f"'{requirement.id}' was decomposed from."
                        ),
                        subjects=(
                            Subject("requirements", requirement.id, (field_step("asil"),)),
                            Subject("requirements", requirement.id, (field_step("derived_from"), member_step(value))),
                        ),
                    )
                )
    return records


def _duplicate_link_finding(
    subjects: tuple[Subject, ...], where: str, verb: str, duplicate: DuplicateLink
) -> ConsistencyIssue:
    """``trace.duplicate-link`` on one list's repeated target.

    *subjects* hold one per member after the first naming the target, so
    removing one occurrence per subject leaves the list naming it once.
    """
    members = [site.value for site in duplicate.sites]
    named = (
        f"'{members[0]}' {len(members)} times"
        if len(set(members)) == 1
        else ", ".join(f"'{member}'" for member in members)
    )
    if duplicate.names_an_element:
        repair = f"one list names element '{duplicate.target}' more than once. Keep one and remove the others."
    else:
        repair = (
            f"one list names requirement '{duplicate.target}' more than once. Keep the one link confirmed "
            "against the requirement and remove the others."
        )
    return ConsistencyIssue(
        rule="trace.duplicate-link",
        message=f"{where} {verb} {named}: {repair}",
        subjects=subjects,
        target=duplicate.target,
    )


def _duplicate_link_records(index: _TraceIndex) -> list[ConsistencyIssue]:
    """``trace.duplicate-link`` of :func:`check_requirement_traceability`, minted after the rules above."""
    requirements = index.requirements
    swcs = index.swcs
    interfaces = index.interfaces
    compositions = index.compositions
    tasks = index.tasks
    hardware = index.hardware
    records: list[ConsistencyIssue] = []
    # Minted after the other rules, so none of their findings moves in the order.
    for swc in swcs:
        for duplicate in duplicate_links(swc):
            subjects = tuple(Subject("swc", swc.name, steps) for steps in duplicate.repeats)
            where = link_site_where("SWC", swc.name, duplicate.sites[0])
            records.append(_duplicate_link_finding(subjects, where, "satisfies", duplicate))
    for interface in interfaces:
        for duplicate in duplicate_links(interface):
            subjects = tuple(Subject("interfaces", interface.name, steps) for steps in duplicate.repeats)
            where = link_site_where("Interface", interface.name, duplicate.sites[0])
            records.append(_duplicate_link_finding(subjects, where, "satisfies", duplicate))
    for composition in compositions:
        for duplicate in duplicate_links(composition):
            subjects = tuple(Subject("compositions", composition.name, steps) for steps in duplicate.repeats)
            where = link_site_where("Composition", composition.name, duplicate.sites[0])
            records.append(_duplicate_link_finding(subjects, where, "satisfies", duplicate))
    for task in tasks:
        for duplicate in duplicate_links(task):
            subjects = tuple(Subject("tasks", task.name, steps) for steps in duplicate.repeats)
            where = link_site_where("Task", task.name, duplicate.sites[0])
            records.append(_duplicate_link_finding(subjects, where, "satisfies", duplicate))
    for element in hardware:
        for duplicate in duplicate_links(element):
            subjects = tuple(Subject("hardware", element.name, steps) for steps in duplicate.repeats)
            where = link_site_where("Hardware element", element.name, duplicate.sites[0])
            records.append(_duplicate_link_finding(subjects, where, "satisfies", duplicate))
    for requirement in requirements:
        for duplicate in duplicate_parents(requirement):
            subjects = tuple(Subject("requirements", requirement.id, steps) for steps in duplicate.repeats)
            records.append(
                _duplicate_link_finding(subjects, f"Requirement '{requirement.id}'", "derives from", duplicate)
            )
    return records


def _unpublishable_findings(subject: Subject, where: str, verb: str, value: str) -> list[ConsistencyIssue]:
    """``trace.link-unpublishable-id`` on one requirement-form member: its finding, or none.

    The predicate is :func:`swcraft_tools.trace.unpublishable_link_id`, the one
    the Sphinx-Needs export refuses by. An element-form member names no id and
    is skipped.
    """
    if is_element_form(value):
        return []
    requirement_id = parse_link(value).id
    if not unpublishable_link_id(requirement_id):
        return []
    return [
        ConsistencyIssue(
            rule="trace.link-unpublishable-id",
            message=(
                f"{where} {verb} '{value}': the id '{requirement_id}' holds '.', which Sphinx-Needs reads as an "
                "id and a part, so `swcraft generate needs` cannot publish this link. The model may keep it; "
                "SWCraft does not support such an id in a published link."
            ),
            subjects=(subject,),
            target=requirement_id,
        )
    ]


def _unpublishable_link_records(index: _TraceIndex) -> list[ConsistencyIssue]:
    """``trace.link-unpublishable-id`` of :func:`check_requirement_traceability`, last but one."""
    requirements = index.requirements
    swcs = index.swcs
    interfaces = index.interfaces
    compositions = index.compositions
    tasks = index.tasks
    hardware = index.hardware
    records: list[ConsistencyIssue] = []
    # After every rule above, so none of their findings moves.
    for swc in swcs:
        for site in link_sites(swc):
            where = link_site_where("SWC", swc.name, site)
            records += _unpublishable_findings(Subject("swc", swc.name, site.steps), where, "satisfies", site.value)
    for interface in interfaces:
        for site in element_link_sites(interface):
            subject = Subject("interfaces", interface.name, site.steps)
            records += _unpublishable_findings(
                subject, link_site_where("Interface", interface.name, site), "satisfies", site.value
            )
    for requirement in (requirement for requirement in requirements if requirement.source == "architecture"):
        for value in requirement.derived_from:
            subject = Subject("requirements", requirement.id, (field_step("derived_from"), member_step(value)))
            records += _unpublishable_findings(subject, f"Requirement '{requirement.id}'", "derives from", value)
    for composition in compositions:
        for site in element_link_sites(composition):
            subject = Subject("compositions", composition.name, site.steps)
            where = link_site_where("Composition", composition.name, site)
            records += _unpublishable_findings(subject, where, "satisfies", site.value)
    for task in tasks:
        for site in element_link_sites(task):
            subject = Subject("tasks", task.name, site.steps)
            records += _unpublishable_findings(
                subject, link_site_where("Task", task.name, site), "satisfies", site.value
            )
    for element in hardware:
        for site in element_link_sites(element):
            subject = Subject("hardware", element.name, site.steps)
            where = link_site_where("Hardware element", element.name, site)
            records += _unpublishable_findings(subject, where, "satisfies", site.value)
    return records


def _blank_text_records(requirements: Sequence[Requirement]) -> list[ConsistencyIssue]:
    """``trace.requirement-blank-text`` of :func:`check_requirement_traceability`."""
    records: list[ConsistencyIssue] = []
    # Blank by the requirements import's own test, `not value.strip()`.
    for requirement in requirements:
        if requirement.source != "architecture":
            continue
        texts = (("title", requirement.title), ("description", requirement.description))
        blank = [name for name, text in texts if not text.strip()]
        if not blank:
            continue
        records.append(
            ConsistencyIssue(
                rule="trace.requirement-blank-text",
                message=(
                    f"Architecture requirement '{requirement.id}' has an empty {' and an empty '.join(blank)}: "
                    "`swcraft generate needs` publishes it as it stands, and the requirements import refuses it. "
                    f"Write the requirement's {' and '.join(blank)}."
                ),
                subjects=tuple(Subject("requirements", requirement.id, (field_step(name),)) for name in blank),
            )
        )
    return records


# ---------------------------------------------------------------------------
# The trace rules' classification, shared with the requirements layer's
# computed values: these answer data and construct no finding.
# ---------------------------------------------------------------------------


def requirement_allocated(requirement_id: str, satisfied: AbstractSet[str], stubs: AbstractSet[str]) -> bool | None:
    """Whether a requirement is allocated: the classification of ``trace.requirement-unallocated``.

    ``None`` for a stub (reported as ``trace.requirement-stub`` instead),
    ``False`` when no ``satisfies`` member names *requirement_id* (*satisfied*
    never holds a ``derived_from`` member's id), ``True`` otherwise.
    """
    if requirement_id in stubs:
        return None
    return requirement_id in satisfied
