"""The change report's facts: what a change does to the architecture, from the model at two revisions.

:func:`compute_change_report` resolves the two revisions ``swcraft diff`` compares (a git revision is
materialised into a temporary directory removed before it returns, the working tree is read in place) and runs
the existing engines over both: the differ, ``check-model`` (:func:`all_findings`), the resource analysis,
``impact``, the trace link sites, the ARXML writer's in-memory document and the overview renderer. It returns a
:class:`ChangeReport`, plain data. Nothing is computed by AI and nothing is written: no model file, no ARXML
file (``serialize()``, never ``save()``), no diagram file. Its sections: *What changed*, the verdict's counts,
the requirement links gained, lost and re-confirmed, the introduced ``check-model`` findings, the cores and
memories before and after, the consumers of each changed interface, the ARXML elements and the overviews moved.

**What it does not reach.**

- *The findings* are compared as a multiset of ``(rule, message)``: a finding whose message names a renamed
  element, or a count or a name the change moved, is listed as introduced although its like was there before.
- *A link* is one ``satisfies`` member at one site; a requirement's own ``derived_from`` is no link here.
- *The ARXML* is built only for a side whose ``check-model`` run has no error and compares serialized elements
  by their canonical bytes, never AUTOSAR semantics.
- *The manifest* is the working tree's for both revisions: its ``arxml_version`` builds both documents.
- *The diagrams* need Node.js; a layout that cannot run is a note, never a refusal of the report.

It reads ``writer.arxml_generation._build_document`` across a module boundary. The CLI loads this module inside
``change-report``'s command only, so ``generate arxml`` and ``validate`` load neither it nor the diagram module.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, NamedTuple

from swcraft_tools.arxml_elements import ArxmlElementChange, compare_arxml_elements, parse_arxml
from swcraft_tools.diagnostics import field_step
from swcraft_tools.differ import ChangedItem, CollectionRule, ItemKey, ModelDiff, diff_snapshots
from swcraft_tools.impact import impact_of
from swcraft_tools.model_registry import ModelRegistry, current_registry
from swcraft_tools.model_state import (
    ElementParseError,
    ModelState,
    RevisionReadError,
    git_environment,
    load_model_state,
    materialize_revision,
    refuse_retired_storage,
)
from swcraft_tools.project import SwcraftProject
from swcraft_tools.rendering import render_model_scalar
from swcraft_tools.resource_analysis import (
    ResourceUsage,
    analyze_resource_budgets,
    budget_status,
    format_bytes,
    format_percent,
    format_whole_bytes,
    is_core,
)
from swcraft_tools.snapshot import ModelSnapshot, SnapshotReadError
from swcraft_tools.trace import (
    LINKABLE_LABELS,
    element_link_sites,
    is_element_form,
    link_site_where,
    link_sites,
    parse_link,
)
from swcraft_tools.validator import SEVERITY_ERROR, SEVERITY_WARNING, ConsistencyIssue, check_cross_layer_consistency
from swcraft_tools.writer import arxml_generation
from swcraft_tools.writer.diagrams import DiagramGenerationError
from swcraft_tools.writer.diagrams import change as diagram_change
from swcraft_tools.writer.diagrams.layout import DiagramLayoutError

if TYPE_CHECKING:
    from swcraft_tools.writer.diagrams.change import ChangeOverview

__all__ = [
    "FAIL_ON_LEVELS",
    "WORKING_TREE",
    "ArxmlComparison",
    "ArxmlElementChange",
    "ChangeReport",
    "ChangedElementRow",
    "ConsumerGroup",
    "Dependent",
    "RequirementRow",
    "ResourceRow",
    "RevisionParseError",
    "Side",
    "all_findings",
    "compute_change_report",
    "introduced_findings",
    "open_side",
    "revisions_from_arguments",
]

#: The label of the working tree, where a revision is labelled ``<rev> (<short sha>)``.
WORKING_TREE = "working tree"

#: ``--fail-on``'s three levels, the default first.
FAIL_ON_LEVELS = ("never", "warning", "error")

#: The two trace rules whose introduced findings about a ``satisfies`` site make a
#: requirement row "needs confirmation"; every other rule is a finding only.
_CONFIRMATION_RULES = frozenset({"trace.link-needs-confirmation", "trace.link-version-not-stated"})

#: The subject step every ``satisfies`` site's path carries (``trace.link_sites``).
_SATISFIES_STEP = field_step("satisfies")

#: The minus sign of a removed collection item and of a negative delta's prefix in prose.
_MINUS = "−"
_ARROW = "→"


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangedElementRow:
    """One row of *What changed*: an element added, removed or changed.

    ``leaves`` are the changed element's leaves in the differ's order, each one line
    (``data_elements.value.init: 0 → 5``); ``rename_of`` is the removed element an added one may rename (the
    differ's hint, never a verdict).
    """

    layer: str
    kind: str
    element: str
    change: str
    leaves: tuple[str, ...] = ()
    rename_of: str | None = None


@dataclass(frozen=True)
class RequirementRow:
    """One row of *Requirements*: a link gained, lost or re-confirmed, or a requirement needing confirmation.

    ``change`` is ``gained`` / ``lost`` / ``re-confirmed`` / ``needs-confirmation``.
    A link row names its ``holder`` (``SWC 'Brake' (port 'pOut')``) and the
    version suffixes as written (``None`` for a bare link); a needs-confirmation
    row names no holder and carries the gate's messages instead.
    """

    requirement: str
    holder: str | None
    change: str
    old_version: str | None = None
    new_version: str | None = None
    messages: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResourceRow:
    """One declared core or memory, before and after, every figure already rendered.

    ``None`` stands for a side that does not declare the resource; ``status`` is ``budget_status`` of the after
    side, ``None`` for a removed resource.
    """

    name: str
    kind: str
    budget: _Budget
    before: str | None
    after: str | None
    change: str | None
    status: str | None


@dataclass(frozen=True)
class Dependent:
    """One transitive dependent of an interface: its layer name, that layer's label and its name."""

    layer: str
    label: str
    name: str


@dataclass(frozen=True)
class ConsumerGroup:
    """One changed or removed interface and its dependents, in the impact walk's order."""

    interface: str
    change: str
    dependents: tuple[Dependent, ...]


@dataclass(frozen=True)
class ArxmlComparison:
    """The ARXML section: compared or not, why not, and the elements that differ."""

    compared: bool
    reason: str | None
    elements: tuple[ArxmlElementChange, ...] = ()


@dataclass(frozen=True)
class ChangeReport:
    """Everything the change report says, computed.

    ``diagrams`` is ``None`` when the pictures were not drawn (``--no-diagrams``,
    or a layout that could not run), and ``diagram_note`` then says why;
    ``compositions`` is the shown set either way, which the verdict counts.
    """

    project_name: str
    old_label: str
    new_label: str
    diff: ModelDiff
    changed_elements: tuple[ChangedElementRow, ...]
    change_count: int
    requirement_rows: tuple[RequirementRow, ...]
    findings: tuple[ConsistencyIssue, ...]
    resources: tuple[ResourceRow, ...]
    consumers: tuple[ConsumerGroup, ...]
    arxml: ArxmlComparison
    compositions: tuple[str, ...]
    diagrams: tuple[ChangeOverview, ...] | None
    diagram_note: str | None
    #: The layout's path-free refusal when the pictures were asked for and could not be drawn; ``None``
    #: otherwise, ``--no-diagrams`` included.
    layout_refusal: str | None = None
    #: The findings of class ``error`` / ``warning`` the model holds after the change that the change
    #: did not introduce: the verdict's "already in the model".
    existing_errors: int = 0
    existing_warnings: int = 0

    @property
    def element_count(self) -> int:
        """``E``: the elements added, removed or changed."""
        return len(self.changed_elements)

    @property
    def introduced_errors(self) -> int:
        """The introduced findings of class ``error``."""
        return sum(1 for finding in self.findings if finding.severity == SEVERITY_ERROR)

    @property
    def introduced_warnings(self) -> int:
        """The introduced findings of class ``warning``."""
        return sum(1 for finding in self.findings if finding.severity == SEVERITY_WARNING)

    @property
    def is_empty(self) -> bool:
        """No change to the model at all."""
        return not self.changed_elements

    def fails_on(self, level: str) -> bool:
        """Whether ``--fail-on <level>`` refuses this report.

        ``warning`` on any introduced finding, ``error`` on an introduced error, ``never`` never.

        Raises:
            ValueError: *level* is none of :data:`FAIL_ON_LEVELS`.
        """
        if level not in FAIL_ON_LEVELS:
            raise ValueError(f"unknown --fail-on level '{level}': one of {', '.join(FAIL_ON_LEVELS)}")
        if level == "warning":
            return bool(self.findings)
        if level == "error":
            return self.introduced_errors > 0
        return False


class RevisionParseError(ValueError):
    """A model file that does not parse at a materialised revision, named by its storage name alone.

    Its absolute path would be the temporary extraction directory, removed by the time anyone reads it.
    """

    def __init__(self, revision: str, cause: ElementParseError) -> None:
        self.revision, self.cause = revision, cause
        super().__init__(self.render())

    def render(self, *, full_path: bool = False) -> str:
        """The refusal; *full_path* changes nothing (the only location is a deleted temporary tree)."""
        return f"revision '{self.revision}': {self.cause}"


def revisions_from_arguments(rev_a: str | None, rev_b: str | None) -> tuple[str, str | None]:
    """``swcraft diff``'s argument rules, as the two revisions to compare.

    None → ``HEAD`` against the working tree; one → it against the working tree; two → the two revisions.
    ``None`` on the new side is the working tree.
    """
    if rev_a is None:
        return "HEAD", None
    return rev_a, rev_b


# ---------------------------------------------------------------------------
# One side
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Side:
    """One revision, read: its label, the directory its model root is at, its state and snapshot."""

    label: str
    root: str
    state: ModelState
    snapshot: ModelSnapshot


def _short_sha(model_root: str, revision: str) -> str:
    """The revision's commit, abbreviated, by one ``git rev-parse`` run in the model root, in git's environment.

    ``--end-of-options`` guards an option-like revision a second time. A refusal keeps git's text."""
    result = subprocess.run(
        ["git", "rev-parse", "--short", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
        cwd=model_root,
        env=git_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RevisionReadError(f"revision '{revision}' names no commit", "git found no commit", result.stderr.strip())
    return result.stdout.strip()


def open_side(stack: contextlib.ExitStack, model_root: str, revision: str | None) -> Side:
    """Read the model at *revision* (``None``: the working tree), its temporary tree removed by *stack*."""
    if revision is None:
        try:
            state = load_model_state(model_root)
        except FileNotFoundError as exc:
            raise SnapshotReadError(exc, model_root=model_root) from exc
        return Side(WORKING_TREE, model_root, state, ModelSnapshot.from_state(state))

    try:
        tmpdir, extracted = materialize_revision(model_root, revision)
    except (FileNotFoundError, NotADirectoryError) as exc:
        # `load_snapshot`'s reason: a model root not on disk to run git in.
        raise SnapshotReadError(exc, model_root=model_root) from exc
    stack.callback(tmpdir.cleanup)
    try:
        # Before the loader, as `load_snapshot` does it, so the sentence names the revision.
        refuse_retired_storage(extracted, revision=revision)
        state = load_model_state(extracted)
    except FileNotFoundError as exc:
        raise SnapshotReadError(exc, model_root=model_root, revision=revision) from exc
    except ElementParseError as exc:
        raise RevisionParseError(revision, exc) from exc
    label = f"{revision} ({_short_sha(model_root, revision)})"
    return Side(label, extracted, state, ModelSnapshot.from_state(state))


# ---------------------------------------------------------------------------
# What changed and the verdict's K
# ---------------------------------------------------------------------------


def _key_text(key: ItemKey, rule: CollectionRule | None) -> str:
    """One identity key as ``swcraft diff`` prints it (``differ._render_key``'s rule, restated).

    Restated rather than imported: ``differ`` keeps ``_render_key`` its own, and the report reads no private name of
    another module but the writer's; ``test_change_report.py`` holds the two equal. A composite rule's key is its
    set parts joined by ``.``; any other tuple a parenthesised list; a name itself.
    """
    if isinstance(key, tuple):
        parts = [part if isinstance(part, str) else render_model_scalar(part) for part in key]
        if rule is not None and rule.strategy == "composite":
            return ".".join(part for part, raw in zip(parts, key, strict=True) if raw is not None)
        return "(" + ", ".join(parts) + ")"
    return str(key)


def _leaves(changed: ChangedItem, prefix: str) -> Iterator[tuple[str, bool]]:
    """Every leaf of one changed element or item, ``(text, counts)``: a rename hint is a leaf that counts nothing."""
    detailed = {entry.field: entry for entry in changed.collection_changes}
    for field_name, old_value, new_value in changed.changed_fields:
        path = f"{prefix}{field_name}"
        collection = detailed.get(field_name)
        if collection is None:
            yield f"{path}: {render_model_scalar(old_value)} {_ARROW} {render_model_scalar(new_value)}", True
            continue
        item_rule = collection.rule
        for item in collection.added:
            yield f"{path}: + {_key_text(item_rule.key(item), item_rule)}", True
        for item in collection.removed:
            yield f"{path}: {_MINUS} {_key_text(item_rule.key(item), item_rule)}", True
        for item in collection.changed:
            yield from _leaves(item, f"{path}.{_key_text(item.key, item_rule)}.")
        if collection.reordered:
            yield f"{path}: order changed", True
        for hint in collection.rename_hints:
            removed, added = _key_text(hint.removed_key, item_rule), _key_text(hint.added_key, item_rule)
            yield f"{path}: possibly a rename of {removed} to {added}", False


def _changed_elements(diff: ModelDiff, registry: ModelRegistry) -> tuple[tuple[ChangedElementRow, ...], int]:
    """The rows of *What changed* in ``displayOrder``, added → removed → changed, and the verdict's ``K``."""
    rows: list[ChangedElementRow] = []
    count = 0
    by_layer = diff.by_layer
    for layer in registry.display_order:
        if layer not in by_layer:
            continue
        facts = registry.layer(layer)
        layer_diff = by_layer[layer]
        identity = facts.identity_field
        renames = {str(hint.added_key): str(hint.removed_key) for hint in layer_diff.rename_hints}
        for element in layer_diff.added:
            name = str(getattr(element, identity))
            rows.append(ChangedElementRow(layer, facts.display_label, name, "added", rename_of=renames.get(name)))
            count += 1
        for element in layer_diff.removed:
            rows.append(ChangedElementRow(layer, facts.display_label, str(getattr(element, identity)), "removed"))
            count += 1
        for changed in layer_diff.changed:
            leaves = list(_leaves(changed, ""))
            count += sum(1 for _, counts in leaves if counts)
            rows.append(
                ChangedElementRow(
                    layer, facts.display_label, str(changed.key), "changed", tuple(text for text, _ in leaves)
                )
            )
    return tuple(rows), count


# ---------------------------------------------------------------------------
# Findings and requirements
# ---------------------------------------------------------------------------


def introduced_findings(
    before: Sequence[ConsistencyIssue], after: Sequence[ConsistencyIssue]
) -> tuple[ConsistencyIssue, ...]:
    """The findings *after* holds and *before* does not, as a multiset of ``(rule, message)``.

    Errors first, then warnings, each in *after*'s emission order; a finding the model had at both revisions
    is consumed once per occurrence, so a second copy of one message is still introduced.
    """
    remaining = Counter((finding.rule, finding.message) for finding in before)
    introduced: list[ConsistencyIssue] = []
    for finding in after:
        key = (finding.rule, finding.message)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            introduced.append(finding)
    errors = [finding for finding in introduced if finding.severity == SEVERITY_ERROR]
    warnings = [finding for finding in introduced if finding.severity != SEVERITY_ERROR]
    return (*errors, *warnings)


def _count(findings: Sequence[ConsistencyIssue], severity: str) -> int:
    return sum(1 for finding in findings if finding.severity == severity)


def all_findings(root: str, arxml_version: str | None = None) -> tuple[tuple[ConsistencyIssue, ...], int]:
    """``check-model``'s whole run at *root*: every finding, errors then warnings, and the error count.

    *arxml_version* is the project's manifest version (``None``: the default, 4.2.2), the one
    ``[constr_2006]`` is judged at, as ``check-model`` judges it.
    """
    result = check_cross_layer_consistency(root, arxml_version)
    return (*result.records, *result.warnings), len(result.records)


def _link_versions(state: ModelState) -> dict[tuple[str, str], tuple[str | None, ...]]:
    """``(requirement id, holder) → the version suffixes written there`` over every ``satisfies`` site.

    An element-form member names no requirement and is skipped. Each distinct suffix once, in model order;
    ``None`` is a bare link.
    """
    pairs: dict[tuple[str, str], list[str | None]] = {}
    for layer, label in LINKABLE_LABELS.items():
        for element in state.layers.get(layer, []):
            sites = link_sites(element) if layer == "swc" else element_link_sites(element)
            for site in sites:
                if is_element_form(site.value):
                    continue
                parsed = parse_link(site.value)
                versions = pairs.setdefault((parsed.id, link_site_where(label, element.name, site)), [])
                if parsed.suffix not in versions:
                    versions.append(parsed.suffix)
    return {pair: tuple(versions) for pair, versions in pairs.items()}


def _version_text(versions: tuple[str | None, ...]) -> str | None:
    """The suffixes as one text (``1``, ``1, 2``), ``None`` for a bare link alone."""
    written = [version for version in versions if version is not None]
    return ", ".join(written) if written else None


def _requirement_rows(
    before: ModelState, after: ModelState, introduced: Iterable[ConsistencyIssue]
) -> tuple[RequirementRow, ...]:
    """Links gained, lost and re-confirmed, then the requirements needing confirmation, by requirement id.

    Needs confirmation is the introduced findings of the two trace rules whose subject is a ``satisfies`` site,
    grouped by their ``target``; a finding about a ``derived_from`` member stays among the findings only.
    """
    old_links, new_links = _link_versions(before), _link_versions(after)
    rows: list[RequirementRow] = []
    for pair in sorted(old_links.keys() | new_links.keys()):
        requirement, holder = pair
        old, new = old_links.get(pair), new_links.get(pair)
        if old is None and new is not None:
            rows.append(RequirementRow(requirement, holder, "gained", new_version=_version_text(new)))
        elif new is None and old is not None:
            rows.append(RequirementRow(requirement, holder, "lost", old_version=_version_text(old)))
        elif old is not None and new is not None and set(old) != set(new):
            # Compared as sets: reordering the members of one `satisfies` moves no link.
            rows.append(
                RequirementRow(
                    requirement, holder, "re-confirmed", old_version=_version_text(old), new_version=_version_text(new)
                )
            )
    messages: dict[str, list[str]] = {}
    for finding in introduced:
        if finding.rule not in _CONFIRMATION_RULES or finding.target is None:
            continue
        if any(_SATISFIES_STEP in subject.path for subject in finding.subjects):
            messages.setdefault(finding.target, []).append(finding.message)
    rows.extend(
        RequirementRow(requirement, None, "needs-confirmation", messages=tuple(texts))
        for requirement, texts in messages.items()
    )
    order = {"gained": 0, "lost": 1, "re-confirmed": 2, "needs-confirmation": 3}
    return tuple(sorted(rows, key=lambda row: (row.requirement, order[row.change], row.holder or "")))


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


def _figure(usage: ResourceUsage, value: Fraction) -> str:
    """A core's figure as a percentage, a memory's in bytes -- ``resource_analysis``' own renderers."""
    return format_percent(value) if is_core(usage.resource) else f"{format_bytes(value)} bytes"


def _signed(usage: ResourceUsage, delta: Fraction) -> str:
    """``after − before`` with its sign (``+3.00%``, ``-1024 bytes``, ``~+0.33%``), ``0`` when equal."""
    if delta == 0:
        return "0"
    text = _figure(usage, delta)
    if delta < 0:
        return text
    return f"~+{text[1:]}" if text.startswith("~") else f"+{text}"


def _resource_rows(before: Sequence[ResourceUsage], after: Sequence[ResourceUsage]) -> tuple[ResourceRow, ...]:
    """One row per declared core and memory: the after side's order, then the removed ones in theirs."""
    old_by_name = {usage.name: usage for usage in before}
    new_names = {usage.name for usage in after}
    rows: list[ResourceRow] = []
    for usage in after:
        kind = "core" if is_core(usage.resource) else "memory"
        old = old_by_name.get(usage.name)
        comparable = old is not None and is_core(old.resource) == is_core(usage.resource)
        rows.append(
            ResourceRow(
                name=usage.name,
                kind=kind,
                budget=_budget(usage),
                before=_figure(old, old.consumption) if old is not None else None,
                after=_figure(usage, usage.consumption),
                change=_signed(usage, usage.consumption - old.consumption) if comparable and old else None,
                status=budget_status(usage),
            )
        )
    for usage in before:
        if usage.name in new_names:
            continue
        rows.append(
            ResourceRow(
                name=usage.name,
                kind="core" if is_core(usage.resource) else "memory",
                budget=_budget(usage),
                before=_figure(usage, usage.consumption),
                after=None,
                change=None,
                status=None,
            )
        )
    return tuple(rows)


# ---------------------------------------------------------------------------
# Interface consumers
# ---------------------------------------------------------------------------


def _consumers(diff: ModelDiff, old: Side, new: Side, registry: ModelRegistry) -> tuple[ConsumerGroup, ...]:
    """Each removed interface's dependents at the before model, then each changed one's at the after model."""
    facts = registry.layer("interfaces")
    layer_by_token = {registry.layer(name).token: registry.layer(name) for name in registry.layer_names}
    layer_diff = diff.by_layer["interfaces"]
    asked: list[tuple[str, str, Side]] = [
        *((str(getattr(element, facts.identity_field)), "removed", old) for element in layer_diff.removed),
        *((str(changed.key), "changed", new) for changed in layer_diff.changed),
    ]
    groups: list[ConsumerGroup] = []
    for name, change, side in asked:
        found = impact_of(side.snapshot, name).get(facts.token, [])
        dependents = []
        for dependent in found:
            layer = layer_by_token[dependent.ref.layer]
            dependents.append(Dependent(layer.name, layer.display_label, dependent.ref.name))
        groups.append(ConsumerGroup(name, change, tuple(dependents)))
    return tuple(groups)


# ---------------------------------------------------------------------------
# ARXML
# ---------------------------------------------------------------------------


def _model_at(label: str) -> str:
    """``the model in the working tree`` / ``the model at HEAD (2137383)``: one side named in a sentence."""
    return "the model in the working tree" if label == WORKING_TREE else f"the model at {label}"


def _compare_arxml(project: SwcraftProject, old: Side, new: Side, errors: tuple[int, int]) -> ArxmlComparison:
    """The two documents' elements compared, or the sentence saying why they were not.

    Built only where ``check-model`` found no error; a ``ValueError`` the writer still raises is named without
    the writer's text, which may name a file of the temporary tree.
    """
    refusals = [
        f"Not compared: {_model_at(side.label)} has {count} error(s); `swcraft validate` refuses it."
        for side, count in zip((old, new), errors, strict=True)
        if count
    ]
    if refusals:
        return ArxmlComparison(False, " ".join(refusals))
    texts: list[str] = []
    for side in (old, new):
        try:
            texts.append(arxml_generation._build_document(side.root, project.resolved_arxml_version).serialize())
        except ValueError:
            return ArxmlComparison(False, f"Not compared: the writer refused {_model_at(side.label)}.")
    return ArxmlComparison(True, None, compare_arxml_elements(parse_arxml(texts[0]), parse_arxml(texts[1])))


# ---------------------------------------------------------------------------
# The whole report
# ---------------------------------------------------------------------------


def _changed_compositions(rows: Sequence[ChangedElementRow]) -> tuple[str, ...]:
    """The compositions-layer names *What changed* reports added, removed or changed, in its order."""
    return tuple(row.element for row in rows if row.layer == "compositions")


def compute_change_report(
    project: SwcraftProject, old_revision: str, new_revision: str | None = None, *, diagrams: bool = True
) -> ChangeReport:
    """The change report of the model between *old_revision* and *new_revision* (``None``: the working tree).

    The temporary trees are removed before this returns, whether it returns or raises.

    Raises (each a ``ValueError``, an exit-1 case; nothing is written by this function):
        SnapshotReadError: the model root is not on disk, or a revision does not contain it.
        RetiredStorageError: either side keeps its elements in a retired layout.
        ElementParseError: a working-tree model file does not parse (naming it).
        RevisionParseError: a model file does not parse at a revision.
        ValueError: a revision that starts with ``-``, is not a commit or cannot be archived;
            the project is no git repository; or a **duplicate identity** at either revision
            (the differ's refusal: the report cannot pair elements it cannot tell apart).

    ``DiagramLayoutError`` never propagates, nor does ``DiagramGenerationError`` (a model text
    holding a character no XML document holds): a picture that cannot be drawn is the
    diagrams section's note (its path-free ``str()``).
    """
    registry = current_registry()
    model_root = project.resolved_model_root
    with contextlib.ExitStack() as stack:
        old = open_side(stack, model_root, old_revision)
        new = open_side(stack, model_root, new_revision)

        diff = diff_snapshots(old.snapshot, new.snapshot)
        changed_elements, change_count = _changed_elements(diff, registry)

        old_findings, old_errors = all_findings(old.root, project.resolved_arxml_version)
        new_findings, new_errors = all_findings(new.root, project.resolved_arxml_version)
        findings = introduced_findings(old_findings, new_findings)
        requirement_rows = _requirement_rows(old.state, new.state, findings)

        resources = _resource_rows(
            analyze_resource_budgets(old.root).resources, analyze_resource_budgets(new.root).resources
        )
        consumers = _consumers(diff, old, new, registry)
        arxml = _compare_arxml(project, old, new, (old_errors, new_errors))

        changed = _changed_compositions(changed_elements)
        overviews: tuple[ChangeOverview, ...] | None = None
        note: str | None = None
        refusal: str | None = None
        if not diagrams:
            note = "Not drawn: `--no-diagrams` was given."
        else:
            try:
                overviews = tuple(diagram_change.change_overviews(old.root, new.root, changed))
            except (DiagramLayoutError, DiagramGenerationError) as exc:
                refusal = str(exc)
                note = f"Not drawn: {refusal}"
        if overviews is None:
            compositions = tuple(diagram_change.shown_compositions(old.root, new.root, changed))
        else:
            compositions = tuple(overview.composition for overview in overviews)

        return ChangeReport(
            project_name=project.name,
            old_label=old.label,
            new_label=new.label,
            diff=diff,
            changed_elements=changed_elements,
            change_count=change_count,
            requirement_rows=requirement_rows,
            findings=findings,
            resources=resources,
            consumers=consumers,
            arxml=arxml,
            compositions=compositions,
            diagrams=overviews,
            diagram_note=note,
            layout_refusal=refusal,
            existing_errors=_count(new_findings, SEVERITY_ERROR) - _count(findings, SEVERITY_ERROR),
            existing_warnings=_count(new_findings, SEVERITY_WARNING) - _count(findings, SEVERITY_WARNING),
        )


class _Budget(NamedTuple):
    """A budget's two spellings: ``exact`` for the JSON, ``shown`` for the Markdown, a memory's in whole bytes."""

    exact: str
    shown: str


def _budget(usage: ResourceUsage) -> _Budget:
    """*usage*'s limit in both spellings."""
    exact = _figure(usage, usage.limit)
    return _Budget(exact, exact if is_core(usage.resource) else f"{format_whole_bytes(usage.limit)} bytes")
