"""Safe component import into an existing SWCraft project.

:func:`import_component` merges one component-scoped ARXML export into an existing, possibly
non-empty project. The pipeline:

1. **Shape** -- exactly one SWC and no composition/connector content.
2. **Mapping with skip safety** -- a behavioural skip aborts (:class:`LossyComponentImportError`);
   a descriptive one is a *Not carried* line. A port on an interface the file declares nowhere is
   answered by the late pass after the target read (:mod:`.dependencies`).
3. **Mapping sets** -- each set the file declares, and the behaviour's references to them.
3a. **Name policy** -- fails closed: this path never renames (:class:`ReservedNameError`).
4. **Target ambiguity** -- one ``name`` in two target files of a touched layer aborts.
5. **Conflict policy** -- by name and equality on the ARXML-representable projection
   (:mod:`.divergence`): absent is created, identical is reused, a divergent shared interface or data
   type is always a conflict, a divergent SWC is replaced only with ``update=True``. A shared mapping
   set may be extended by a map it lacks; a set pairing one application type with two implementation
   types, or an import that newly makes a component do so, is a conflict. 5a checks reserved names
   over the merged model. Any conflict aborts with nothing written.
6. **Atomic apply** -- staged write-then-rename with full rollback; ``dry_run`` writes nothing.

Steps 1-5 are :func:`plan_component_import`.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from swcraft_tools.arxml_import import map_data_types, map_interfaces, map_swcs
from swcraft_tools.arxml_import.merge import ELEMENT_LAYERS, ELEMENT_TAG_BY_LAYER
from swcraft_tools.arxml_import.reconcile import (
    PlacementMember,
    data_type_key,
    home_package,
    kept_package,
    place_case_variants,
)
from swcraft_tools.arxml_import.registry import PathRegistry
from swcraft_tools.arxml_import.report import (
    DEFERRED_ELEMENT_TAGS,
    PLACEMENT_WHAT,
    SKIP_SECTION_ORDER,
    allocated_file,
    deferred_tag_skips,
    generated_name_losses,
    interface_to_dict,
    package_placement_line,
    reserved_device_name_skips,
    skip_section_heading,
)
from swcraft_tools.arxml_import.swc_mapping import ImportedSwc, report_port_init_labels, settle_stated_names
from swcraft_tools.arxml_import.type_mapping import (
    HeldUnreadInit,
    MappedSets,
    map_mapping_sets,
    map_variable_types,
)
from swcraft_tools.arxml_import.unresolved import UnresolvedReferences
from swcraft_tools.arxml_reader import ABORT, ImportModel, SkipEntry, SkipLog, StandardRule, cite, read_arxml
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.capability_markers import DATA_TYPES
from swcraft_tools.data_description import DataDescription, read_data_description
from swcraft_tools.diagnostics import Subject
from swcraft_tools.differ import diff_layer, format_layer
from swcraft_tools.model_registry import current_registry
from swcraft_tools.reader import (
    read_compositions,
    read_interfaces,
    read_mapping_sets,
    read_swc_model,
    swc_placement,
)
from swcraft_tools.schema import (
    BaseDataType,
    Composition,
    DataDescriptionElement,
    DataType,
    ExternalDataType,
    ExternalInterface,
    Interface,
    MappingSet,
    SWComponent,
)
from swcraft_tools.validator import (
    NAME_SPACE_LEVEL_ELEMENT,
    NAME_SPACE_LEVEL_PACKAGE,
    data_type_layer,
    duplicate_event_names,
    name_space_collisions,
    root_package_members,
    runnable_event_collisions,
)
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.writer.yaml_documents import data_type_to_dict, swc_to_dict

from .capabilities import (
    ACCESS_LISTS,
    CAPABILITIES,
    NONE,
    VARIABLES,
    CarryContext,
    Decision,
    Signal,
    carried_variable_mappings,
    carry,
    decide,
    notes,
    read_signal,
)
from .closure import ComponentExchangeError
from .dependencies import MissingReference, needs_late_pass, resolve_against_target
from .divergence import (
    CONTRADICTING_SET_NOTE,
    INIT_ASYMMETRY_NOTE,
    SHARED_DEFINITION_NOTE,
    compare_carried_set,
    double_mappings_after_import,
    incoming_init_disagreements,
    projected_data_type,
    projected_element,
    projected_interface,
    projected_swc,
)
from .name_policies import (
    access_point_collisions,
    illegal_identifiers,
    reserved_name_violations,
    shape_violations,
)
from .report import (
    ComponentImportPlan,
    ComponentImportReport,
    PlannedTarget,
)
from .resulting_behaviour import (
    CallFindings,
    area_counts,
    behaviour_overlay,
    call_finding_lines,
    kept_behaviour_breaks,
    withhold_behaviour,
    without_withheld_lines,
)
from .target_carry import (
    carry_target_costs,
    carry_target_descriptions,
    carry_target_links,
    carry_target_measures,
    incoming_descriptions_differ,
    keep_target_com_spec_spelling,
    kept_termination_handling,
    untaken_description_lines,
)
from .target_project import (
    Ordered,
    component_stub_conflict,
    external_stub_conflict,
    layer_directory,
    scan_named_files,
    target_bare_pairs,
    target_first,
    target_interface_or_stub,
    target_type_or_stub,
)

# The `group` placement value a newly created SWC lands under: an import must choose one, and
# the ARXML package name is a writer layout detail, not a project directory decision. Plural,
# unlike export.py's throwaway `_EXPORT_TEMP_SWC_GROUP`, on purpose.
_IMPORT_TARGET_SWC_GROUP = "components"


class InvalidComponentArxmlError(ComponentExchangeError):
    """The input violates the component-export shape contract; see :attr:`violations`."""

    def __init__(self, message: str, violations: list[str]) -> None:
        super().__init__(message)
        self.violations: list[str] = list(violations)


class LossyComponentImportError(ComponentExchangeError):
    """A behavioural skip touched the component's own closure, so importing it would be lossy.

    The skip lines are on :attr:`skip_lines`; the late pass's verdicts on :attr:`missing_references`
    (each also one of the lines), empty for every other refusal.
    """

    def __init__(
        self, message: str, skip_lines: list[str], missing_references: Sequence[MissingReference] = ()
    ) -> None:
        super().__init__(message)
        self.skip_lines: list[str] = list(skip_lines)
        self.missing_references: list[MissingReference] = list(missing_references)


class AmbiguousTargetProjectError(ComponentExchangeError):
    """The target project declares one ``name`` in two or more YAML files; nothing is written.

    By-name matching would silently pick or overwrite one of them. :attr:`duplicate_lines` name each
    file by its storage name, and :meth:`render` in full for a local terminal. A create whose file
    name is taken is not this error: it takes a collision-suffixed file, reported.
    """

    def __init__(self, message: str, duplicate_lines: list[str], full_message: str | None = None) -> None:
        super().__init__(message)
        self.duplicate_lines: list[str] = list(duplicate_lines)
        self.full_message = message if full_message is None else full_message

    def render(self, *, full_path: bool = False) -> str:
        """Render the refusal with each file by its storage name, or in full for a local terminal."""
        return self.full_message if full_path else str(self)


class ComponentImportConflictError(ComponentExchangeError):
    """A shared definition diverges from the target's, or the SWC diverges without ``update=True``.

    Nothing is written; the entries are on :attr:`conflicts`. :attr:`update_resolves` is the
    refusal's category, read by the CLI's exit code and never by its text: true exactly when the run
    was made without ``update=True`` and the one conflict is the SWC's own divergence. With any other
    entry it is false, since one unresolved conflict aborts an update too.
    """

    def __init__(self, message: str, conflicts: list[str], *, update_resolves: bool = False) -> None:
        super().__init__(message)
        self.conflicts: list[str] = list(conflicts)
        self.update_resolves = update_resolves


class ReservedNameError(ComponentExchangeError):
    """The resulting model would carry a name the ARXML writer refuses to emit.

    Five families: a **reserved** name of the writer's generated elements (over the merged model),
    a name or generated form that is **not a legal AUTOSAR** ``Identifier`` (over the incoming closure
    alone), an access-point ``SHORT-NAME`` **derived twice**, a name **taken by a generated event**,
    and a name **already renamed** by the shared mapping pipeline. The import never renames. The
    violations are on :attr:`violations`.
    """

    def __init__(self, message: str, violations: list[str]) -> None:
        super().__init__(message)
        self.violations: list[str] = list(violations)


def _planned_create_path(
    root: Path,
    layer: str,
    name: str,
    label: str,
    planned: list[str],
    suffix_lines: list[str],
    *,
    placement: Mapping[str, str] | None = None,
) -> Path:
    """Return the file a planned "create" of *name* is written to, decided now and reserved.

    *planned* holds the paths this plan already placed, compared ignoring case; the decided path is
    appended to it, a suffix line to *suffix_lines*.

    Raises:
        AmbiguousTargetProjectError: the plain file already declares *name* -- the fail-closed twin
            of the conflict policy, which plans a create only for a name no file declares.
    """
    free, suffix_line = allocated_file(root, layer, name, f"{label} '{name}'", placement=placement, reserved=planned)
    if free.occupied_by_self:
        line = (
            f"cannot create {label} '{name}': the target project's file {free.path} already declares it, "
            "but the project was not read as holding it"
        )
        raise AmbiguousTargetProjectError(f"component import aborted with nothing written: {line}", [line])
    planned.append(free.path)
    if suffix_line is not None:
        suffix_lines.append(suffix_line)
    return root / free.path


def _format_skip(entry: SkipEntry, release: ArxmlVersion | None) -> str:
    """Format one skip entry's line: ``TAG path: reason``.

    A defect of the export carries its citation after the path, in the release the file declares
    (``None`` cites the fallback release): ``TAG path (R4.3.0 [constr_2530]): reason``.
    """
    cited = "" if entry.rule is None else f" ({cite(entry.rule, release)})"
    return f"{entry.tag} {entry.path}{cited}: {entry.reason}"


def _format_skips(entries: list[SkipEntry], release: ArxmlVersion | None) -> list[str]:
    """Format one line per skip entry, in the entries' own order (:func:`_format_skip`)."""
    return [_format_skip(entry, release) for entry in entries]


def _descriptive(entries: Iterable[SkipEntry]) -> list[SkipEntry]:
    """Return the entries the preview prints under *Not carried from the file*: every non-deferred ``REPORT``."""
    return [e for e in entries if e.tag not in DEFERRED_ELEMENT_TAGS and e.effect != ABORT]


def _late_refusal(
    blocking: list[SkipEntry], missing: Sequence[MissingReference], model: ImportModel
) -> LossyComponentImportError:
    """Build the late pass's refusal: its blocking lines, then every missing reference.

    A missing reference never refuses on its own: on a create it withholds the behaviour, and for a
    component the target holds the project's own behaviour is kept. Called only with a blocking line.
    """
    assert blocking, "a late refusal needs a blocking line"
    lines = [*_format_skips(blocking, model.declared_release), *(record.line() for record in missing)]
    parts = [_lossy_import_message(blocking, model)]
    if missing:
        parts.append(
            f"References the project does not have ({len(missing)}): " + "; ".join(record.line() for record in missing)
        )
    return LossyComponentImportError("; ".join(parts), lines, missing)


def _lossy_import_message(blocking: list[SkipEntry], model: ImportModel) -> str:
    """Build the abort message of :class:`LossyComponentImportError`: the blocking lines by section.

    Each section is the ``import model`` report's heading and its lines, in the order defects of the
    export, not supported by SWCraft, outside the file. One string: a category is not a severity.
    """
    release = model.declared_release
    sections: list[str] = []
    for category in SKIP_SECTION_ORDER:
        entries = [entry for entry in blocking if entry.category is category]
        if entries:
            heading = skip_section_heading(category, len(entries), model.declared_schema)
            sections.append(f"{heading} " + "; ".join(_format_skips(entries, release)))
    return (
        "component import aborted: the following content could not be mapped, and a component "
        "export contains nothing but the component's own closure, so importing without it "
        "would be lossy: " + "; ".join(sections)
    )


def _swc_target(swc: SWComponent, path: Path, root: Path) -> PlannedTarget:
    """Return one planned SWC write, in both the storage and the model vocabulary.

    The ``group`` placement value is read back off the chosen path, relative to the SWC layer's
    storage directory, with the reader's own function, so the plan and a later read agree.
    """
    return PlannedTarget(
        layer="swc",
        element=swc,
        placement=swc_placement(path, root / current_registry().storage("swc").directory),
        path=path,
        payload=swc_to_dict(swc),
    )


def _atomic_apply(writes: list[tuple[Path, dict[str, Any]]]) -> list[str]:
    """Apply every planned write all-or-nothing (pipeline step 6).

    Records every path's prior bytes (or ``None``), writes each file to a ``.tmp`` sibling, then
    ``os.replace()``s each onto its final path. If a rename fails, every replaced path is rolled back
    (prior bytes restored, new files deleted), every remaining ``.tmp`` and every directory staging
    created and left empty is removed, and the error is re-raised.

    Returns the final paths written, as strings, in plan order.
    """
    pre_state: dict[Path, bytes | None] = {
        final_path: final_path.read_bytes() if final_path.exists() else None for final_path, _ in writes
    }

    created_dirs: list[Path] = []  # dirs the staging phase created, root-first

    def _remove_created_dirs() -> None:
        """Remove the staging-created directories left empty, deepest first; best effort."""
        for directory in sorted(set(created_dirs), key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass  # not empty / already gone -- never break a rollback

    staged: list[tuple[Path, Path]] = []  # (tmp sibling, final path)
    try:
        for final_path, data in writes:
            ancestor = final_path.parent
            missing_ancestors: list[Path] = []
            while not ancestor.exists():
                missing_ancestors.append(ancestor)
                ancestor = ancestor.parent
            final_path.parent.mkdir(parents=True, exist_ok=True)
            created_dirs.extend(reversed(missing_ancestors))
            tmp_path = final_path.with_name(f"{final_path.name}.tmp")
            # recorded before the write, so a partial file left by a
            # mid-write failure is cleaned up too
            staged.append((tmp_path, final_path))
            # `newline="\n"`: model YAML, written under a `.tmp` name and then
            # renamed, so the bytes here are the bytes that land in the
            # architect's repository. `elements.py`'s `write_element` carries
            # the argument for every model-YAML write in SWCraft.
            tmp_path.write_text(
                yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
            )
    except BaseException:
        for tmp_path, _ in staged:
            tmp_path.unlink(missing_ok=True)
        _remove_created_dirs()
        raise

    replaced: list[Path] = []
    try:
        for tmp_path, final_path in staged:
            os.replace(tmp_path, final_path)
            replaced.append(final_path)
    except BaseException:
        for final_path in replaced:
            prior = pre_state[final_path]
            if prior is None:
                final_path.unlink(missing_ok=True)
            else:
                final_path.write_bytes(prior)
        for tmp_path, _ in staged[len(replaced) :]:
            tmp_path.unlink(missing_ok=True)
        _remove_created_dirs()
        raise
    return [str(final_path) for _, final_path in staged]


def import_component(
    source: str,
    model_root: str,
    update: bool = False,
    dry_run: bool = False,
    replace_access_lists: bool = False,
    arxml_version: ArxmlVersion | None = None,
) -> ComponentImportReport:
    """Import one component-scoped ARXML export into an existing project.

    :func:`plan_component_import` plus the staged write-then-rename apply.

    Args:
        model_root: the target project's **resolved model root directory**.
        update: allow replacing the target's own divergent definition of this SWC (only its file),
            with a preview on ``swc_diff_preview``. No effect on a divergent shared interface or data
            type, which always aborts.
        dry_run: compute and report, write nothing.
        replace_access_lists: with ``update``, take an unmarked export's runnable access lists as
            the truth. Ignored without ``update``.
        arxml_version: the target's ``arxml_version``, at which the asynchronous call rule judges an
            update's kept behaviour; ``None`` means 4.2.2.

    Returns:
        A :class:`ComponentImportReport` (``written_paths`` empty for ``dry_run`` and a no-op).

    Raises:
        FileNotFoundError: ``source`` missing.
        InvalidComponentArxmlError: the input violates the export shape.
        LossyComponentImportError: a behavioural skip touched the component's closure.
        ReservedNameError: an illegal ``Identifier`` or a reserved name; never renamed.
        AmbiguousTargetProjectError: one name in 2+ target files of a touched layer.
        ComponentImportConflictError: a shared definition diverges, or the SWC without ``update``.
        ElementParseError: a scanned target YAML file is not parseable.
    """
    plan = plan_component_import(
        source,
        model_root,
        update=update,
        dry_run=dry_run,
        replace_access_lists=replace_access_lists,
        arxml_version=arxml_version,
    )
    # File names belong in the CLI's report only: the MCP tool hands the protocol elements, never
    # paths, and the protocol decides each file at its commit.
    plan.report.file_suffix_lines = list(plan.file_suffix_lines)
    if dry_run:
        return plan.report

    plan.report.written_paths = _atomic_apply(plan.writes)
    return plan.report


def plan_component_import(
    source: str,
    model_root: str,
    update: bool = False,
    dry_run: bool = False,
    replace_access_lists: bool = False,
    arxml_version: ArxmlVersion | None = None,
) -> ComponentImportPlan:
    """Decide everything one component import would do, and write nothing.

    Steps 1-5 of the module pipeline. Every refusal :func:`import_component` raises is raised here.
    The arguments are :func:`import_component`'s; *dry_run* only sets the report's flag.

    Returns:
        A :class:`~swcraft_tools.component_exchange.report.ComponentImportPlan`: the report and the
        ordered targets. A conflict-free no-op re-import plans no targets and is a success.

    Raises:
        Everything :func:`import_component` documents.
    """
    root = Path(model_root)
    model, signals = _read_source(source, update, replace_access_lists)
    closure = _map_source(root, model)
    _check_identifiers(closure, model)
    files = _scan_target_files(root)
    target = _read_target(model_root)
    closure, late = _late_pass(closure, model, target)
    closure, kept = _carry_and_check_reserved_names(closure, target, late.missing_references)
    closure, placement_lines = _place(closure, target)

    # The SWC's own write first, so the rename loop touches the one possibly pre-existing file
    # first and a mid-loop failure exercises the restore-prior-bytes rollback, not only deletions.
    writes = _PlannedWrites()
    capabilities = _decide_capabilities(closure, signals, target, kept.existing_swc)
    closure, outcome = _plan_swc_write(
        closure=closure,
        kept=kept,
        capabilities=capabilities,
        target=target,
        late=late,
        writes=writes,
        files=files,
        root=root,
        update=update,
        arxml_version=arxml_version,
    )
    call_findings = call_finding_lines(
        swc_action=outcome.action,
        swc=closure.swc,
        existing_swc=kept.existing_swc,
        arxml_version=arxml_version,
        behaviour_kept=kept.behaviour_kept,
        stated_swc=closure.imported_swcs[0].swc,
        target_interfaces=target.interfaces,
        interfaces=closure.interfaces,
    )

    # The creates in the export's SOURCE-PATH order, as `run_import` writes, so a case-only pair
    # takes the plain file and `~1` the same way round in both importers.
    type_paths = closure.registry.source_paths
    interface_paths = closure.registry.interface_source_paths
    type_creates = _plan_type_creates(closure, target, type_paths, writes, root)
    element_creates = _plan_element_creates(closure, target, type_paths, writes, root)
    interface_creates = _plan_interface_creates(closure, target, interface_paths, late.reused_stubs, writes, root)
    set_creates = _plan_mapping_set_creates(closure, kept.existing_swc, target, files, writes, root)
    _raise_on_conflicts(writes, outcome.divergence)
    report = _build_report(
        source=source,
        dry_run=dry_run,
        closure=closure,
        kept=kept,
        outcome=outcome,
        signals=signals,
        interface_creates=interface_creates,
        type_creates=type_creates,
        element_creates=element_creates,
        set_creates=set_creates,
        lines=_report_lines(closure, type_paths, type_creates, element_creates),
        writes=writes,
        placement_lines=placement_lines,
        late=late,
        call_findings=call_findings,
    )
    return ComponentImportPlan(
        report=report,
        targets=tuple(writes.targets),
        file_suffix_lines=tuple(writes.suffix_lines),
        missing_references=tuple(late.missing_references),
    )


@dataclass(frozen=True)
class _Closure:
    """The incoming closure as the planner holds it; a phase that changes part of it returns a new one."""

    swc: SWComponent
    imported_swcs: list[ImportedSwc]
    mapped_swcs: list[ImportedSwc]
    mapped_entries: list[SkipEntry]
    data_types: list[DataType]
    elements: list[DataDescriptionElement]
    interfaces: list[Interface]
    registry: PathRegistry
    mapped_sets: MappedSets
    skip_lines: list[str]
    access_collapses: list[str]
    not_carried: list[str]
    held_unread_inits: list[HeldUnreadInit]
    unresolved: UnresolvedReferences


@dataclass(frozen=True)
class _TargetFiles:
    """The exact files of the target's components and mapping sets, by ``name``, as the ambiguity scan found them."""

    swc_files: dict[str, list[Path]]
    mapping_set_files: dict[str, list[Path]]


@dataclass(frozen=True)
class _TargetModel:
    """The target project's validated model, as the conflict policy reads it."""

    swcs: dict[str, SWComponent]
    interfaces: dict[str, Interface]
    description: DataDescription
    types: dict[tuple[str, str], DataType]
    elements: dict[tuple[str, str], DataDescriptionElement]
    compositions: list[Composition]
    composition_names: set[str]
    mapping_sets: dict[str, MappingSet]


@dataclass(frozen=True)
class _LateResult:
    """What the late pass answered: the verdicts, the project the import would leave, the reused target stubs."""

    missing_references: list[MissingReference]
    resulting_description: DataDescription
    reused_stubs: list[str]


@dataclass(frozen=True)
class _Kept:
    """The target's component of the incoming name, and what step 5a carried from it before the comparison."""

    existing_swc: SWComponent | None
    behaviour_kept: bool
    preserved_termination: str | None
    carried_costs: dict[str, str]
    carried_descriptions: list[str]


@dataclass(frozen=True)
class _Capabilities:
    """Every capability's decision and a builder of the mapping sets the import would leave (built on first call).

    ``variable_mappings``: the target's set entries only the carried variables need, left out of the comparison.
    """

    decisions: dict[str, Decision]
    sets_after_import: Callable[[], dict[str, MappingSet]]
    variable_mappings: frozenset[str]


@dataclass
class _PlannedWrites:
    """The ordered lists every write-planning phase appends to; the order of the appends is the plan's order.

    ``planned_paths`` are the model-root-relative paths already given to a planned create, handed to the
    kernel as ``reserved`` because nothing is written until the atomic apply; ``suffix_lines`` report
    the creates that took a collision suffix; ``descriptions_not_taken`` is the report-only block of
    :func:`untaken_description_lines`.
    """

    targets: list[PlannedTarget] = field(default_factory=list)
    planned_paths: list[str] = field(default_factory=list)
    suffix_lines: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    descriptions_not_taken: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _SwcOutcome:
    """The component's own planned action, its update preview, and what an update kept or dropped of the target's."""

    action: str
    diff_preview: str
    preserved_memory: dict[str, int]
    preserved_asil: str | None
    preserved_measures: dict[str, dict[str, str]]
    dropped_measures: list[str]
    preserved_links: dict[str, list[str]]
    dropped_links: list[str]
    preserved_descriptions: list[str]
    preserved_costs: dict[str, str]
    preserved_termination: str | None
    family_fields: dict[str, Any]
    divergence: str | None


@dataclass(frozen=True)
class _TypeCreates:
    """The data types the import creates (by name and by ``(layer, name)``) and the ones it reuses."""

    created: list[str]
    created_keys: set[tuple[str, str]]
    reused: list[str]


@dataclass(frozen=True)
class _SetCreates:
    """The mapping sets the import creates, reuses and extends, and the components that share an extended one."""

    created: list[str]
    reused: list[str]
    extended: dict[str, list[str]]
    extended_components: dict[str, list[str]]


@dataclass(frozen=True)
class _ElementCreates:
    """The data description elements the import creates and reuses, each a list of names per layer."""

    created: dict[str, list[str]]
    reused: dict[str, list[str]]


@dataclass(frozen=True)
class _InterfaceCreates:
    """The interfaces the import creates and reuses, and the stubs among them."""

    created: list[str]
    reused: list[str]
    stubs: list[str]


@dataclass(frozen=True)
class _ReportLines:
    """The report's lines about the created definitions (:func:`_report_lines`)."""

    native_declaration_missing: list[str]
    unread_physics: list[str]
    represented: list[str]


def _read_source(source: str, update: bool, replace_access_lists: bool) -> tuple[ImportModel, dict[str, Signal]]:
    """Read the export at *source* and every capability's signal, refusing a file that is no component export.

    Raises :class:`InvalidComponentArxmlError` for a shape violation or a capability value the writer does
    not mint, and :class:`EarlierReleaseExportError` for an earlier release's export.
    """
    model = read_arxml(source)
    violations = shape_violations(model)
    if violations:
        raise InvalidComponentArxmlError(
            f"'{source}' is not a valid component export: " + "; ".join(violations),
            violations,
        )
    _refuse_an_earlier_release_export(model)
    # Every capability's signal, a property of the file alone. A value the writer does not mint is
    # refused here, before mapping, by every family but the access lists, which read it as unmarked
    # and report it. The caller's flag counts only with `update`.
    signals: dict[str, Signal] = {}
    for capability in CAPABILITIES:
        signal = read_signal(capability, model, replace_flag_given=replace_access_lists and update)
        if isinstance(signal, str):
            raise InvalidComponentArxmlError(f"'{source}' is not a valid component export: {signal}", [signal])
        signals[capability.key] = signal
    return model, signals


def _map_source(root: Path, model: ImportModel) -> _Closure:
    """Map *model* into the incoming closure, with the target at *root* read only for its bare type pairs.

    Raises :class:`LossyComponentImportError` when a skip entry is behavioural.
    """
    # Every unit, compu method and data constraint of the file is an element of its layer, while
    # SWCraft's own private form reads back into its type's inline field, so a project's own export
    # reads back as written. `elements` is filled by the call.
    elements: list[DataDescriptionElement] = []
    # The tolerant target scan never raises, so no refusal moves ahead of step 3a.
    kept_pairs = target_bare_pairs(root)
    data_types, registry, dt_skips = map_data_types(model, elements=elements, kept_pairs=kept_pairs)
    # A port com-spec's init under an interface the file declares nowhere is held; the late pass
    # records it once the target has answered the path.
    held_unread_inits: list[HeldUnreadInit] = []
    interfaces, iface_skips = map_interfaces(
        model, registry, data_types, elements=elements, held_unread_inits=held_unread_inits
    )
    registry.variable_types = map_variable_types(model, registry, data_types, elements=elements)
    # Between the two mappers, as in `run_import`: both sides of a map resolve through the
    # registry `map_data_types` filled, and `map_swcs` needs the set names. No merge verdict: a
    # component export holds one closure, where merging does not arise. The unresolved-reference
    # policy is filtered to a port's interface and resolved late: such a port is kept with the path
    # and everything through it held. Every other role keeps this path's own refusal.
    unresolved = UnresolvedReferences.for_component_import(model)
    mapped_sets, set_skips = map_mapping_sets(model, registry, data_types)
    mapped_swcs, swc_skips = map_swcs(
        model,
        interfaces,
        iface_skip_log=iface_skips,
        unresolved=unresolved,
        mapping_set_name_by_path=mapped_sets.name_by_path,
        port_inits=registry.port_inits,
        variable_types=registry.variable_types,
    )
    # The SHORT-LABEL lines of the ports' own init values, held by the mapper until no later pass
    # can take their INIT-VALUE away. A port this path may stub holds no such line.
    for imported in mapped_swcs:
        report_port_init_labels(imported.port_init_labels, swc_skips)
    # A duplicated access pair collapses to one entry as a report line, never a skip entry: a
    # non-deferred skip aborts, and the collapse loses no model content.
    access_collapses = [line for imported in mapped_swcs for line in imported.access_collapses]
    all_entries = [
        # The reader's lines about content the import carries are left out, as `run_import` does.
        *registry.uncarried(model.skip_log),
        *dt_skips,
        *set_skips,
        *iface_skips,
        *swc_skips,
        *_reserved_device_name_skips(model, registry, data_types, mapped_sets, interfaces, mapped_swcs, elements),
    ]
    # The source's stated names of the behaviour, its events and access points.
    imported_swcs, names_not_stated = settle_stated_names(model, mapped_swcs)
    # The names the writer generates itself: descriptive, so they join the preview's block.
    generated_skips = SkipLog()
    generated_name_losses(
        model, data_types, imported_swcs, registry.source_paths, generated_skips, not_stated=names_not_stated
    )
    mapped_entries = list(all_entries)
    all_entries.extend(generated_skips)
    # Only a behavioural entry aborts. A descriptive one (a value that documents or annotates)
    # is printed under *Not carried from the file* and the import goes past it.
    blocking = _blocking(all_entries)
    not_carried = _format_skips(_descriptive(all_entries), model.declared_release)
    if blocking:
        raise LossyComponentImportError(
            _lossy_import_message(blocking, model), _format_skips(blocking, model.declared_release)
        )
    # What is left is deferred-tag content, deduplicated by (tag, path): a mapper's entry may
    # also surface via `deferred_tag_skips`' sweep.
    seen_skips: set[tuple[str, str]] = set()
    report_only: list[SkipEntry] = []
    for entry in [*(e for e in all_entries if e.tag in DEFERRED_ELEMENT_TAGS), *deferred_tag_skips(model)]:
        key = (entry.tag, entry.path)
        if key not in seen_skips:
            seen_skips.add(key)
            report_only.append(entry)
    skip_lines = _format_skips(report_only, model.declared_release)
    swc = imported_swcs[0].swc  # exactly one, guaranteed by steps 1-2
    return _Closure(
        swc=swc,
        imported_swcs=imported_swcs,
        mapped_swcs=mapped_swcs,
        mapped_entries=mapped_entries,
        data_types=data_types,
        elements=elements,
        interfaces=interfaces,
        registry=registry,
        mapped_sets=mapped_sets,
        skip_lines=skip_lines,
        access_collapses=access_collapses,
        not_carried=not_carried,
        held_unread_inits=held_unread_inits,
        unresolved=unresolved,
    )


def _check_identifiers(closure: _Closure, model: ImportModel) -> None:
    """Refuse a name the import would write that is no legal AUTOSAR ``Identifier``, or would need a rename.

    Raises :class:`ReservedNameError` (or the name-defect refusals' own errors); returns nothing.
    """
    # A property of the incoming closure alone, so it runs before the target is read (an illegal
    # SHORT-NAME is broken for every target) and before the reserved-name check (an illegal name
    # gives schema-invalid ARXML whatever else is true).
    registry = closure.registry
    _refuse_incoming_name_defects(
        closure.swc,
        closure.interfaces,
        closure.data_types,
        registry.source_names,
        closure.mapped_sets.sets,
        closure.elements,
    )

    # The check's blind spot: the shared `PathRegistry` renames on a bare-name collision, right
    # for a bulk import into a fresh project and wrong in a populated one, and a `_app` suffix is a
    # legal Identifier.
    if registry.renames:
        raise ReservedNameError(
            "component import aborted with nothing written: importing this export would require "
            "renaming an element, and component import never renames -- fix the name in "
            "the exporting project and re-export: " + "; ".join(registry.renames),
            list(registry.renames),
        )

    # Two declarations of ONE source package whose names differ only in case: the file's own
    # `[constr_2508]` defect, a property of the closure alone.
    _refuse_case_only_pairs_in_one_package(
        model,
        closure.swc,
        closure.imported_swcs[0].path,
        closure.data_types,
        closure.interfaces,
        registry,
        closure.mapped_sets,
        closure.elements,
    )


def _scan_target_files(root: Path) -> _TargetFiles:
    """Scan every layer the import matches by name under *root*; refuse a name two files declare.

    Raises :class:`AmbiguousTargetProjectError`.
    """
    swc_files = scan_named_files(layer_directory(root, "swc"), "**/*.yaml", root, "swc")
    iface_files = scan_named_files(layer_directory(root, "interfaces"), "*.yaml", root, "interfaces")
    dt_files = scan_named_files(layer_directory(root, "implementation_types"), "*.yaml", root, "implementation_types")
    base_type_files = scan_named_files(layer_directory(root, "base_types"), "*.yaml", root, "base_types")
    application_type_files = scan_named_files(
        layer_directory(root, "application_types"), "*.yaml", root, "application_types"
    )
    # Compositions claim the `/ComponentTypes` namespace the reserved-name check compares against,
    # so an ambiguous compositions layer makes that comparison unsafe.
    composition_files = scan_named_files(layer_directory(root, "compositions"), "*.yaml", root, "compositions")
    mapping_set_files = scan_named_files(layer_directory(root, "mapping_sets"), "*.yaml", root, "mapping_sets")
    element_files = {
        layer: scan_named_files(layer_directory(root, layer), "*.yaml", root, layer) for layer in ELEMENT_LAYERS
    }
    duplicates = [
        (layer, name, paths)
        for layer, groups in (
            ("SWC", swc_files),
            ("interface", iface_files),
            ("implementation type", dt_files),
            ("application type", application_type_files),
            ("base type", base_type_files),
            ("composition", composition_files),
            ("mapping set", mapping_set_files),
            *((PLACEMENT_WHAT[layer], element_files[layer]) for layer in ELEMENT_LAYERS),
        )
        for name, paths in sorted(groups.items())
        if len(paths) > 1
    ]
    if duplicates:
        raise _ambiguous_target(duplicates, root)
    return _TargetFiles(swc_files, mapping_set_files)


def _read_target(model_root: str) -> _TargetModel:
    """Read the target project's validated model at *model_root*, the conflict policy's other side."""
    target_swcs = {s.name: s for s in read_swc_model(model_root)}
    target_interfaces = {i.name: i for i in read_interfaces(model_root)}
    target_description = read_data_description(model_root)
    target_types = {(data_type_layer(d), d.name): d for d in target_description.data_types}
    target_elements: dict[tuple[str, str], DataDescriptionElement] = {
        data_type_key(element): element for element in target_description.elements
    }
    # Compositions are read only for the component-type SHORT-NAMEs they claim; none is written.
    target_compositions = read_compositions(model_root)
    target_composition_names = {c.name for c in target_compositions}
    target_mapping_sets = {s.name: s for s in read_mapping_sets(model_root)}
    return _TargetModel(
        target_swcs,
        target_interfaces,
        target_description,
        target_types,
        target_elements,
        target_compositions,
        target_composition_names,
        target_mapping_sets,
    )


def _late_pass(closure: _Closure, model: ImportModel, target: _TargetModel) -> tuple[_Closure, _LateResult]:
    """Answer what the file cannot answer itself against the project the import would leave.

    Returns the closure with the resolved component, and the verdicts. Raises the late refusal for a
    blocking entry, and the name-defect refusals over the resolved component.
    """
    # After 3a, so an illegal name keeps its precedence over the target, and before every later
    # step, so the conflict policy, the carries, the preview, 5a and 5b see the resolved component.
    # A mapping-set reference onto a set the file lacks takes the target's set, else a verdict; a
    # set the file declares and this import did not write already aborted in `_map_source`.
    missing_references: list[MissingReference] = []
    reused_stubs: list[str] = []
    # The project the import would leave: the target's description with the file's types and
    # elements over it. Placement changes no name, so one build serves the late pass and the overlay.
    resulting_description = DataDescription.last_declared(
        [*target.description.data_types, *target.description.elements, *closure.data_types, *closure.elements]
    )
    registry, mapped_sets = closure.registry, closure.mapped_sets
    if needs_late_pass(closure.mapped_swcs[0], model, mapped_sets.name_by_path) or closure.held_unread_inits:
        late = resolve_against_target(
            closure.mapped_swcs[0],
            model=model,
            unresolved=closure.unresolved,
            target_interfaces=target.interfaces,
            target_mapping_sets=target.mapping_sets,
            mapping_set_name_by_path=mapped_sets.name_by_path,
            # A minted stub takes no name already held, so a same-named stub of another `external_ref`
            # takes the next `_ext` name.
            registry=registry,
            claimed_interface_names={iface.name for iface in closure.interfaces}
            | set(registry.interface_source_paths)
            | set(target.interfaces),
            held_unread_inits=closure.held_unread_inits,
            data_description=resulting_description,
        )
        # A minted stub is written by this import; a reused one is the target's.
        interfaces = [*closure.interfaces, *late.minted]
        reused_stubs = [stub.name for stub in late.reused]
        late_entries = list(late.skip_log)
        late_blocking = _blocking(late_entries)
        # A verdict withholds the behaviour and refuses nothing; an unsupported or unreadable line
        # still refuses the whole import. A component the target holds keeps its own behaviour.
        if late_blocking:
            raise _late_refusal(late_blocking, late.missing, model)
        missing_references = list(late.missing)
        # Stated names are settled again over the resolved component (a late-admitted entry is built
        # unnamed) before any name is judged. With a verdict, the runnables are withheld; then the lines
        # are recomputed on the component as it will be written and step 3a is asked again.
        imported_swcs, names_not_stated = settle_stated_names(model, [late.imported])
        mapped_entries = closure.mapped_entries
        if missing_references:
            imported_swcs = [withhold_behaviour(imported_swcs[0])]
            mapped_entries = without_withheld_lines(mapped_entries, model, imported_swcs[0])
            late_entries = without_withheld_lines(late_entries, model, imported_swcs[0])
        swc = imported_swcs[0].swc
        generated_skips = SkipLog()
        generated_name_losses(
            model,
            closure.data_types,
            imported_swcs,
            registry.source_paths,
            generated_skips,
            not_stated=names_not_stated,
        )
        not_carried = _format_skips(
            _descriptive([*mapped_entries, *late_entries, *generated_skips]), model.declared_release
        )
        access_collapses = [line for imported in imported_swcs for line in imported.access_collapses]
        _refuse_incoming_name_defects(
            swc, interfaces, closure.data_types, registry.source_names, mapped_sets.sets, closure.elements
        )
        closure = replace(
            closure,
            swc=swc,
            imported_swcs=imported_swcs,
            interfaces=interfaces,
            mapped_entries=mapped_entries,
            not_carried=not_carried,
            access_collapses=access_collapses,
        )
    return closure, _LateResult(missing_references, resulting_description, reused_stubs)


def _carry_and_check_reserved_names(
    closure: _Closure, target: _TargetModel, missing_references: Sequence[MissingReference]
) -> tuple[_Closure, _Kept]:
    """Carry what an existing component keeps onto the incoming one, then refuse a name the writer reserves.

    Returns the closure with the component as it will be judged, and what was kept. Raises
    :class:`ReservedNameError`.
    """
    # Reserved names, over the MERGED model: a `{base}_Implementation` collision is only visible
    # once both sides are known, and the import path never passes `create_swc`'s guard.
    swc = closure.swc
    existing_swc = target.swcs.get(swc.name)
    behaviour_kept = existing_swc is not None and not existing_swc.external and bool(missing_references)
    # A component the target holds whose incoming behaviour was withheld keeps the project's own
    # behaviour whole, before every carry and the comparison, so a re-import compares the descriptive
    # part alone. Never over an `external: true` stub: it has no behaviour, and its empty fields copied
    # over would write a component `check-model` and the writer refuse.
    if existing_swc is not None and behaviour_kept:
        swc = behaviour_overlay(existing_swc, swc)
    # The termination value, descriptions and budgets: an absent value does not request clearing
    # the target's, so each is carried once before the comparison and is then no difference.
    preserved_termination: str | None = None
    carried_descriptions: list[str] = []
    carried_costs: dict[str, str] = {}
    if existing_swc is not None:
        preserved_termination = kept_termination_handling(existing_swc, swc)
        if preserved_termination is not None:
            swc = swc.model_copy(update={"handle_termination_and_restart": preserved_termination})
        swc, carried_descriptions = carry_target_descriptions(existing_swc, swc)
        swc, carried_costs = carry_target_costs(existing_swc, swc)
    # The component AS WRITTEN is judged: its runnables decide whether it emits `{name}_Timing`.
    reserved_violations = reserved_name_violations(
        swc,
        closure.data_types,
        target.swcs,
        target.composition_names,
        target.types,
        closure.interfaces,
        target.interfaces,
    )
    if reserved_violations:
        raise ReservedNameError(
            "component import aborted: the resulting model would use a name the ARXML writer "
            "reserves, so it could not be generated or validated -- " + "; ".join(reserved_violations),
            reserved_violations,
        )
    kept = _Kept(existing_swc, behaviour_kept, preserved_termination, carried_costs, carried_descriptions)
    return replace(closure, swc=swc), kept


def _place(closure: _Closure, target: _TargetModel) -> tuple[_Closure, list[str]]:
    """Place the creates in their packages; return the placed closure and the report's placement lines.

    Raises :class:`ReservedNameError` for a case-only pair placement would put in one package.
    """
    # After the reserved names and before anything is planned, so every payload carries the
    # recorded `package`.
    placed = _place_closure(
        closure.swc,
        closure.imported_swcs[0].path,
        closure.data_types,
        closure.interfaces,
        closure.registry,
        closure.mapped_sets,
        target.swcs,
        target.interfaces,
        target.types,
        target.compositions,
        target.mapping_sets,
        closure.elements,
        target.elements,
    )
    closure = replace(
        closure,
        swc=placed.swc,
        data_types=placed.data_types,
        interfaces=placed.interfaces,
        elements=placed.elements,
        mapped_sets=replace(closure.mapped_sets, sets=placed.mapping_sets),
    )
    return closure, placed.lines


def _decide_capabilities(
    closure: _Closure, signals: Mapping[str, Signal], target: _TargetModel, existing_swc: SWComponent | None
) -> _Capabilities:
    """Decide every capability about the placed component, and return what the comparison needs of the sets."""
    # Every capability's decision, asked after the overlay and the carries: whether the family is
    # compared, and the regime an update's report names. The access lists are compared under `marked`
    # and `replace-flag`; under `carried` the runnables that speak about operation events are compared,
    # not carried. The variables, exclusive areas and data-received events are compared under `marked`,
    # or under no marker when the file states one; otherwise left out on both sides.
    swc = closure.swc
    decisions = {capability.key: decide(capability, signals[capability.key], swc) for capability in CAPABILITIES}
    # Built once, on first use, so only where the variables are not compared.
    sets_after_import = functools.cache(
        lambda: {s.name: s for s in target_first(target.mapping_sets, closure.mapped_sets.sets)}
    )
    variable_mappings = (
        frozenset(carried_variable_mappings(existing_swc, swc, sets_after_import()))
        if existing_swc is not None and not decisions[VARIABLES.key].compared
        else frozenset()
    )
    return _Capabilities(decisions, sets_after_import, variable_mappings)


def _plan_swc_write(
    *,
    closure: _Closure,
    kept: _Kept,
    capabilities: _Capabilities,
    target: _TargetModel,
    late: _LateResult,
    writes: _PlannedWrites,
    files: _TargetFiles,
    root: Path,
    update: bool,
    arxml_version: ArxmlVersion | None,
) -> tuple[_Closure, _SwcOutcome]:
    """Decide the component's own action and plan its write first, appending to *writes*.

    The first conflict it may append is a target stub of another name standing for the component's
    own path. Returns the closure with the component as it will be written, and the outcome. Raises
    the collision refusals an update's carries can reach.
    """
    writes.conflicts.extend(component_stub_conflict(closure.swc, target.swcs, closure.imported_swcs[0].path))
    swc, existing_swc, preserved_termination = closure.swc, kept.existing_swc, kept.preserved_termination
    decisions, variable_mappings = capabilities.decisions, capabilities.variable_mappings
    swc_divergence: str | None = None
    swc_diff_preview = ""
    preserved_memory: dict[str, int] = {}
    preserved_asil: str | None = None
    preserved_measures: dict[str, dict[str, str]] = {}
    dropped_measures: list[str] = []
    preserved_links: dict[str, list[str]] = {}
    dropped_links: list[str] = []
    preserved_descriptions: list[str] = []
    preserved_costs: dict[str, str] = {}
    # Each family's regime governs only an `updated` SWC; otherwise the report says `none`
    # rather than naming a signal that did no work.
    family_fields: dict[str, Any] = {capability.regime_field: NONE for capability in CAPABILITIES}
    if existing_swc is None:
        swc_action = "created"
        record = current_registry().storage("swc")
        swc_path = _planned_create_path(
            root,
            "swc",
            swc.name,
            "SWC",
            writes.planned_paths,
            writes.suffix_lines,
            placement={record.nesting: _IMPORT_TARGET_SWC_GROUP},
        )
        writes.targets.append(_swc_target(swc, swc_path, root))
    elif projected_swc(existing_swc, decisions, variable_mappings) == projected_swc(swc, decisions, variable_mappings):
        swc_action = "unchanged"
    elif update:
        # Replaces the exact file step 4 recorded, never a re-derived path.
        swc_action = "updated"
        # An ARXML export carries no `memory` and no `asil`, so their absence means "unknown":
        # carry the target's. Erasing `asil` would also be invisible to the safety gate, since an
        # undeclared level is derived from the allocated requirements.
        preserved_memory = dict(existing_swc.memory)
        swc = swc.model_copy(update={"memory": preserved_memory})
        preserved_asil = existing_swc.asil
        swc = swc.model_copy(update={"asil": preserved_asil})
        # An export's path is not a request to move the component.
        swc = swc.model_copy(update={"package": existing_swc.package})
        preserved_costs = kept.carried_costs
        # Measures, links and the com-spec spelling, all carried before the preview so it advertises
        # no removal that is not happening. An erased link would lower the allocated level as invisibly
        # as an erased `asil`.
        swc, preserved_measures, dropped_measures = carry_target_measures(existing_swc, swc)
        swc, preserved_links, dropped_links = carry_target_links(existing_swc, swc)
        swc = keep_target_com_spec_spelling(existing_swc, swc)
        preserved_descriptions = kept.carried_descriptions
        # Each family's carry runs under the `carried` regime alone. Under the other regimes nothing
        # is carried and the preview names every removed declaration, so the loss is visible BEFORE the
        # write on `--dry-run`. With the project's behaviour kept whole no carry has anything to act on.
        if not kept.behaviour_kept:
            family_fields.update(
                {capability.regime_field: decisions[capability.key].regime for capability in CAPABILITIES}
            )
            swc, carried_fields = carry(
                decisions,
                existing_swc,
                swc,
                CarryContext(
                    mapping_sets=capabilities.sets_after_import,
                    refuse_event_collisions=_refuse_runnable_event_collisions,
                ),
            )
            family_fields.update(carried_fields)
        # Again after ALL the carries: a carried access list or operation event derives a name that
        # can land beside an incoming name of the same key.
        _refuse_element_name_space_collisions(
            swc, closure.interfaces, closure.data_types, closure.mapped_sets.sets, closure.elements
        )
        if kept.behaviour_kept:
            # The kept behaviour is asked over the overlay with the validator's predicates. A reference
            # the update breaks is a conflict; a collision the project's component already had is
            # `check-model`'s to report.
            _refuse_runnable_event_collisions(swc, already=existing_swc)
            writes.conflicts.extend(
                kept_behaviour_breaks(
                    existing_swc,
                    swc,
                    target_interfaces=target.interfaces,
                    interfaces=closure.interfaces,
                    target_description=target.description,
                    description=late.resulting_description,
                    target_mapping_sets=target.mapping_sets,
                    mapping_sets=closure.mapped_sets.sets,
                    arxml_version=arxml_version,
                )
            )
        preview = diff_layer([existing_swc], [swc], "name", "swcs")
        swc_diff_preview = format_layer(preview, "SWCs", "name")
        writes.targets.append(_swc_target(swc, files.swc_files[swc.name][0], root))
    else:
        swc_action = "conflict"
        preserved_termination = None
        differing_descriptions = incoming_descriptions_differ(existing_swc, swc)
        description_note = (
            "; the description of "
            + ", ".join(differing_descriptions)
            + " differs from the target's -- a non-empty description the file states is compared"
            if differing_descriptions
            else ""
        )
        swc_divergence = (
            f"SWC '{swc.name}' already exists in the target project and diverges "
            f"(pass --update to replace the component's own definition; "
            f"{notes(decisions, existing_swc, swc)}{description_note})"
        )
        writes.conflicts.append(swc_divergence)
    outcome = _SwcOutcome(
        action=swc_action,
        diff_preview=swc_diff_preview,
        preserved_memory=preserved_memory,
        preserved_asil=preserved_asil,
        preserved_measures=preserved_measures,
        dropped_measures=dropped_measures,
        preserved_links=preserved_links,
        dropped_links=dropped_links,
        preserved_descriptions=preserved_descriptions,
        preserved_costs=preserved_costs,
        preserved_termination=preserved_termination,
        family_fields=family_fields,
        divergence=swc_divergence,
    )
    return replace(closure, swc=swc), outcome


def _plan_type_creates(
    closure: _Closure,
    target: _TargetModel,
    type_paths: Mapping[tuple[str, str], str],
    writes: _PlannedWrites,
    root: Path,
) -> _TypeCreates:
    """Plan a create for each incoming data type the target lacks, in source-path order.

    Appends to *writes* a target per created type, a conflict per type the target holds as a stub or
    defines otherwise, and the descriptions a reused type does not take. Returns what it created and
    reused.
    """
    created_types: list[str] = []
    # Two created types may share a name in two layers.
    created_type_keys: set[tuple[str, str]] = set()
    reused_types: list[str] = []
    for dt in _in_source_path_order(closure.data_types, type_paths, data_type_key):
        existing_dt = target_type_or_stub(target.types, data_type_key(dt), type_paths.get(data_type_key(dt)))
        if existing_dt is None:
            created_types.append(dt.name)
            created_type_keys.add(data_type_key(dt))
            dt_path = _planned_create_path(
                root, data_type_layer(dt), dt.name, "data type", writes.planned_paths, writes.suffix_lines
            )
            writes.targets.append(
                PlannedTarget(
                    layer=data_type_layer(dt),
                    element=dt,
                    placement={},
                    path=dt_path,
                    payload=data_type_to_dict(dt),
                )
            )
        elif isinstance(existing_dt, ExternalDataType):
            writes.conflicts.append(external_stub_conflict("data type", dt.name, existing_dt))
        elif projected_data_type(existing_dt) == projected_data_type(dt):
            reused_types.append(dt.name)
            writes.descriptions_not_taken.extend(untaken_description_lines(existing_dt, dt))
        else:
            writes.conflicts.append(
                f"shared data type '{dt.name}' diverges from the target project's "
                f"existing definition ({SHARED_DEFINITION_NOTE})"
            )
    return _TypeCreates(created=created_types, created_keys=created_type_keys, reused=reused_types)


def _plan_element_creates(
    closure: _Closure,
    target: _TargetModel,
    type_paths: Mapping[tuple[str, str], str],
    writes: _PlannedWrites,
    root: Path,
) -> _ElementCreates:
    """Plan a create for each incoming data description element the target lacks, in source-path order.

    Appends to *writes* as :func:`_plan_type_creates` does. Returns what it created and reused.
    """
    # A unit, compu method or data constraint is a shared definition like a data type: absent is
    # created, equal reused, divergent a conflict with no override.
    created_elements: dict[str, list[str]] = {}
    reused_elements: dict[str, list[str]] = {}
    for element in _in_source_path_order(closure.elements, type_paths, data_type_key):
        layer = data_type_layer(element)
        existing_element = target.elements.get((layer, element.name))
        if existing_element is None:
            created_elements.setdefault(layer, []).append(element.name)
            element_path = _planned_create_path(
                root, layer, element.name, PLACEMENT_WHAT[layer], writes.planned_paths, writes.suffix_lines
            )
            writes.targets.append(
                PlannedTarget(
                    layer=layer, element=element, placement={}, path=element_path, payload=element_to_dict(element)
                )
            )
        elif projected_element(existing_element) == projected_element(element):
            reused_elements.setdefault(layer, []).append(element.name)
            writes.descriptions_not_taken.extend(untaken_description_lines(existing_element, element))
        else:
            writes.conflicts.append(
                f"shared {PLACEMENT_WHAT[layer]} '{element.name}' diverges from the target project's "
                f"existing definition ({SHARED_DEFINITION_NOTE})"
            )
    return _ElementCreates(created=created_elements, reused=reused_elements)


def _plan_interface_creates(
    closure: _Closure,
    target: _TargetModel,
    interface_paths: Mapping[str, str],
    reused_stubs: Sequence[str],
    writes: _PlannedWrites,
    root: Path,
) -> _InterfaceCreates:
    """Plan a create for each incoming interface the target lacks, in source-path order.

    Appends to *writes* as :func:`_plan_type_creates` does, and a conflict for a reused interface
    whose incoming ``init`` the target contradicts. *reused_stubs* are the target stubs the late pass
    re-pointed a port at. Returns what it created and reused, and the stubs among them.
    """
    created_interfaces: list[str] = []
    reused_interfaces: list[str] = []
    stub_interfaces: list[str] = list(reused_stubs)
    for iface in _in_source_path_order(closure.interfaces, interface_paths):
        existing_iface = target_interface_or_stub(target.interfaces, iface.name, interface_paths.get(iface.name))
        if existing_iface is None:
            created_interfaces.append(iface.name)
            if isinstance(iface, ExternalInterface):
                stub_interfaces.append(iface.name)
            iface_path = _planned_create_path(
                root, "interfaces", iface.name, "interface", writes.planned_paths, writes.suffix_lines
            )
            writes.targets.append(
                PlannedTarget(
                    layer="interfaces",
                    element=iface,
                    placement={},
                    path=iface_path,
                    payload=interface_to_dict(iface),
                )
            )
        elif isinstance(existing_iface, ExternalInterface):
            writes.conflicts.append(external_stub_conflict("interface", iface.name, existing_iface))
        elif projected_interface(existing_iface) == projected_interface(iface):
            # The projection blanked `init` on both sides, so an incoming value the target contradicts is
            # still to be decided; a plain reuse would drop it silently.
            init_disagreements = incoming_init_disagreements(existing_iface, iface)
            if init_disagreements:
                writes.conflicts.append(
                    f"shared interface '{iface.name}' diverges from the target project's "
                    f"existing definition: {'; '.join(init_disagreements)} ({INIT_ASYMMETRY_NOTE})"
                )
            else:
                reused_interfaces.append(iface.name)
                writes.descriptions_not_taken.extend(untaken_description_lines(existing_iface, iface))
        else:
            writes.conflicts.append(
                f"shared interface '{iface.name}' diverges from the target project's "
                f"existing definition ({SHARED_DEFINITION_NOTE})"
            )
    # A target stub the late pass re-pointed a port at is reused as it stands.
    reused_interfaces.extend(name for name in reused_stubs if name not in reused_interfaces)
    return _InterfaceCreates(created=created_interfaces, reused=reused_interfaces, stubs=stub_interfaces)


def _plan_mapping_set_creates(
    closure: _Closure,
    existing_swc: SWComponent | None,
    target: _TargetModel,
    files: _TargetFiles,
    writes: _PlannedWrites,
    root: Path,
) -> _SetCreates:
    """Plan a create for each incoming mapping set the target lacks, and an extension of each it holds in part.

    Appends to *writes* a target per created or extended set, rewriting an extended one at the exact
    file the target scan recorded in *files*, a conflict per set whose maps contradict the target's, and
    then one for the double mappings the whole import would leave (*existing_swc* is the target's component
    of the incoming name). Returns what it created, reused and extended.
    """
    created_mapping_sets: list[str] = []
    reused_mapping_sets: list[str] = []
    extended_mapping_sets: dict[str, list[str]] = {}
    extended_mapping_set_components: dict[str, list[str]] = {}
    # Every set as it will stand after the WHOLE import, read once the loop has planned them all.
    sets_after: dict[str, MappingSet] = dict(target.mapping_sets)
    for mapping_set in _in_source_path_order(closure.mapped_sets.sets, closure.mapped_sets.source_paths):
        existing_set = target.mapping_sets.get(mapping_set.name)
        if existing_set is None:
            created_mapping_sets.append(mapping_set.name)
            sets_after[mapping_set.name] = mapping_set
            set_path = _planned_create_path(
                root, "mapping_sets", mapping_set.name, "mapping set", writes.planned_paths, writes.suffix_lines
            )
            writes.targets.append(
                PlannedTarget(
                    layer="mapping_sets",
                    element=mapping_set,
                    placement={},
                    path=set_path,
                    payload=element_to_dict(mapping_set),
                )
            )
        else:
            added, contradictions = compare_carried_set(existing_set, mapping_set)
            if contradictions:
                writes.conflicts.append(
                    f"shared mapping set '{mapping_set.name}' diverges from the target project's "
                    f"existing definition: {'; '.join(contradictions)} ({CONTRADICTING_SET_NOTE})"
                )
            elif not added:
                reused_mapping_sets.append(mapping_set.name)
            else:
                # The target's set gains the maps it lacks, after its own, and is rewritten at the exact file
                # the scan recorded, so the atomic apply restores its prior bytes on a failure.
                extended = existing_set.model_copy(update={"maps": [*existing_set.maps, *added]})
                sets_after[mapping_set.name] = extended
                extended_mapping_sets[mapping_set.name] = [
                    f"{entry.application} -> {entry.implementation}" for entry in added
                ]
                extended_mapping_set_components[mapping_set.name] = [
                    name
                    for name in sorted(target.swcs)
                    if name != closure.swc.name and mapping_set.name in target.swcs[name].data_type_mappings
                ]
                writes.targets.append(
                    PlannedTarget(
                        layer="mapping_sets",
                        element=extended,
                        placement={},
                        path=files.mapping_set_files[mapping_set.name][0],
                        payload=element_to_dict(extended),
                    )
                )
    # After the loop, over the merged view: a map one incoming set adds can contradict one another
    # set adds, or one a reused set holds.
    double_mappings = double_mappings_after_import(
        closure.swc, existing_swc, target.swcs, target.mapping_sets, sets_after
    )
    if double_mappings:
        writes.conflicts.append(
            "mapping sets would pair one application type with two implementation types: "
            f"{'; '.join(double_mappings)} ({CONTRADICTING_SET_NOTE})"
        )
    return _SetCreates(
        created=created_mapping_sets,
        reused=reused_mapping_sets,
        extended=extended_mapping_sets,
        extended_components=extended_mapping_set_components,
    )


def _raise_on_conflicts(writes: _PlannedWrites, divergence: str | None) -> None:
    """Refuse the import if any conflict of *writes* stands.

    *divergence* is the component's own conflict line, the one ``--update`` resolves. Raises
    :class:`ComponentImportConflictError` naming every conflict of *writes* in order.
    """
    if writes.conflicts:
        # A divergent shared definition has no override: one unresolved conflict aborts everything,
        # an otherwise update-eligible SWC replacement included.
        raise ComponentImportConflictError(
            "component import aborted with nothing written: " + "; ".join(writes.conflicts),
            writes.conflicts,
            update_resolves=divergence is not None and writes.conflicts == [divergence],
        )


def _report_lines(
    closure: _Closure,
    type_paths: Mapping[tuple[str, str], str],
    type_creates: _TypeCreates,
    element_creates: _ElementCreates,
) -> _ReportLines:
    """Build the report's lines about the definitions the import creates.

    Returns the lines of a created base type with no native declaration, the unread physics of a created
    type, and what the reader represented for a created type or element (with the budget normalisations).
    """
    # One line per CREATED base type with no NATIVE-DECLARATION, in source-path order; a reused one
    # writes nothing. `type_paths` is indexed directly: every type here was registered, so a
    # `KeyError` would be the registry disagreeing with itself.
    created_type_keys = type_creates.created_keys
    native_declaration_missing = [
        f"{dt.name} ({type_paths[data_type_key(dt)]})"
        for dt in sorted(closure.data_types, key=lambda element: type_paths[data_type_key(element)])
        if data_type_key(dt) in created_type_keys and isinstance(dt, BaseDataType) and not dt.native_declaration.strip()
    ]

    # Narrowed to created types (a note about a type this plan does not write is about someone
    # else's file), keyed on the source PATH, which does not move.
    created_type_source_paths = {
        type_paths[data_type_key(dt)] for dt in closure.data_types if data_type_key(dt) in created_type_keys
    }
    unread_physics = [
        entry.line for entry in closure.registry.unread_physics if entry.source_path in created_type_source_paths
    ]
    created_source_paths = created_type_source_paths | {
        type_paths[data_type_key(element)]
        for element in closure.elements
        if element.name in element_creates.created.get(data_type_layer(element), [])
    }
    represented = [entry.line for entry in closure.registry.represented if entry.source_path in created_source_paths]
    # A budget carried from another CSE code.
    represented.extend(entry.line for entry in closure.imported_swcs[0].budget_normalizations)
    return _ReportLines(native_declaration_missing, unread_physics, represented)


def _build_report(
    *,
    source: str,
    dry_run: bool,
    closure: _Closure,
    kept: _Kept,
    outcome: _SwcOutcome,
    signals: Mapping[str, Signal],
    interface_creates: _InterfaceCreates,
    type_creates: _TypeCreates,
    element_creates: _ElementCreates,
    set_creates: _SetCreates,
    lines: _ReportLines,
    writes: _PlannedWrites,
    placement_lines: list[str],
    late: _LateResult,
    call_findings: CallFindings,
) -> ComponentImportReport:
    """Build the report of the planned import from every phase's result, with an empty ``written_paths``."""
    return ComponentImportReport(
        source=source,
        dry_run=dry_run,
        swc=closure.swc.name,
        swc_action=outcome.action,
        behaviour_kept=kept.behaviour_kept,
        swc_diff_preview=outcome.diff_preview,
        preserved_memory=outcome.preserved_memory,
        preserved_asil=outcome.preserved_asil,
        preserved_handle_termination_and_restart=outcome.preserved_termination,
        preserved_measures=outcome.preserved_measures,
        dropped_measures=outcome.dropped_measures,
        preserved_links=outcome.preserved_links,
        dropped_links=outcome.dropped_links,
        preserved_descriptions=outcome.preserved_descriptions,
        preserved_costs=outcome.preserved_costs,
        # An unrecognized access-marker value is read as unmarked, never refused, and kept so the
        # report names what was found.
        access_marker_value=signals[ACCESS_LISTS.key].unrecognized,
        created_interfaces=interface_creates.created,
        reused_interfaces=interface_creates.reused,
        stub_interfaces=interface_creates.stubs,
        created_data_types=type_creates.created,
        reused_data_types=type_creates.reused,
        created_elements=element_creates.created,
        reused_elements=element_creates.reused,
        created_mapping_sets=set_creates.created,
        reused_mapping_sets=set_creates.reused,
        extended_mapping_sets=set_creates.extended,
        extended_mapping_set_components=set_creates.extended_components,
        skip_lines=closure.skip_lines,
        access_collapse_lines=closure.access_collapses,
        native_declaration_missing_lines=lines.native_declaration_missing,
        unread_physics_lines=lines.unread_physics,
        represented_lines=lines.represented,
        written_exclusive_areas=area_counts(closure.swc) if outcome.action in ("created", "updated") else (0, 0),
        **outcome.family_fields,
        queue_length_lines=list(closure.imported_swcs[0].queue_length_losses),
        descriptions_not_taken_lines=writes.descriptions_not_taken,
        not_carried_lines=closure.not_carried,
        package_placement_lines=placement_lines,
        missing_references=[record.line() for record in late.missing_references],
        async_without_result_lines=call_findings.async_without_result,
        async_call_finding_lines=call_findings.async_findings,
        implicit_access_finding_lines=call_findings.implicit_findings,
    )


def _ambiguous_target(duplicates: list[tuple[str, str, list[Path]]], root: Path) -> AmbiguousTargetProjectError:
    """Build the refusal for a name two or more target files declare, in its two forms.

    The shared form, which reaches an MCP client, names each file by its storage name inside *root*,
    never a directory above the model root; the full form, for a local terminal, names each file as
    the scan found it. One function composes both.
    """

    def refusal(name_of: Callable[[Path], str]) -> tuple[str, list[str]]:
        lines = [
            f"{layer} name '{name}' is declared by {len(paths)} files: {', '.join(name_of(path) for path in paths)}"
            for layer, name, paths in duplicates
        ]
        return "component import aborted: the target project is ambiguous -- " + "; ".join(lines), lines

    shared, shared_lines = refusal(lambda path: path.relative_to(root).as_posix())
    full, _full_lines = refusal(str)
    return AmbiguousTargetProjectError(shared, shared_lines, full)


class EarlierReleaseExportError(ComponentExchangeError):
    """A component file an earlier SWCraft writer produced, refused before anything is mapped or compared.

    That writer emitted every primitive ``B`` as an ``SW-BASE-TYPE`` ``B`` plus an
    ``IMPLEMENTATION-DATA-TYPE`` ``{B}_impl`` of category ``VALUE`` over it. The current model has no
    such twin: imported, the file would create a value type the model never declared, and a project
    holding the component would abort on a divergence it does not have. Nothing is converted; the
    file is exported again with this release. The sentence names the twin and no location or release
    number. **A file from another tool that declares such a twin and carries no SWCraft marker is
    refused with the same sentence.**
    """

    def __init__(self, twins: list[str]) -> None:
        self.twins = list(twins)
        named = ", ".join(f"`{twin}`" for twin in self.twins)
        noun = "implementation-type twin" if len(self.twins) == 1 else "implementation-type twins"
        super().__init__(
            f"this component file was written by an earlier SWCraft release (it carries the {named} {noun} "
            "that release invented and no data-types marker); re-export it with this release"
        )


def _refuse_an_earlier_release_export(model: ImportModel) -> None:
    """Raise :class:`EarlierReleaseExportError` for an unmarked file carrying the earlier writer's ``{B}_impl`` twin.

    Two conditions, both read from the raw file: the behaviour carries no ``SD GID="DATA-TYPES"``
    in its ``SWCRAFT`` group (the marker's **key** decides, whatever text it holds), and the file
    declares a ``VALUE`` ``IMPLEMENTATION-DATA-TYPE`` ``{B}_impl`` whose ``BASE-TYPE-REF`` names an
    ``SW-BASE-TYPE`` ``B`` **in the same file**. The bytes alone could not decide: a current model
    declaring that shape exports it with the marker and imports unchanged.

    **Does not reach:** a twin over a base type the file does not declare imports as the value type
    it is; a marker written into a file by hand is believed.
    """
    if any(DATA_TYPES.sd_gid in swc.swcraft_markers for swc in model.swcs):
        return
    base_names_by_path = {base.path: base.name for base in model.sw_base_types}
    twins = sorted(
        impl.name
        for impl in model.impl_data_types
        if impl.category == "VALUE"
        and impl.base_type_ref in base_names_by_path
        and impl.name == f"{base_names_by_path[impl.base_type_ref]}_impl"
    )
    if twins:
        raise EarlierReleaseExportError(twins)


def _in_source_path_order(
    elements: Sequence[Ordered], paths: Mapping[Any, str], key: Callable[[Ordered], Any] | None = None
) -> list[Ordered]:
    """Return *elements* ordered by the full AUTOSAR path each was declared at.

    An element *paths* does not record sorts after every recorded one, by name. *key* is what *paths*
    is keyed by, the name by default. **Does not reach:** the allocation rule itself; an element written
    by an earlier run (never re-ordered); a target file renamed by hand.
    """
    key_of = key if key is not None else (lambda element: element.name)
    return sorted(
        elements, key=lambda element: (key_of(element) not in paths, paths.get(key_of(element), ""), element.name)
    )


def _reserved_device_name_skips(
    model: ImportModel,
    registry: PathRegistry,
    data_types: Sequence[DataType],
    mapped_sets: MappedSets,
    interfaces: Sequence[Interface],
    imported_swcs: Sequence[ImportedSwc],
    elements: Sequence[DataDescriptionElement],
) -> list[SkipEntry]:
    """Return a skip entry for every closure element whose name Windows reserves for a device.

    ``rule:storage.reserved-device-name``, decided by the whole-export importer's
    :func:`~swcraft_tools.arxml_import.report.reserved_device_name_skip` so the reason reads alike.
    Here the entry aborts the import with nothing written, never a rename.

    **Does not reach:** it asks about every element the file declares, not only the creates, so an
    identical element the target already stores under such a name still aborts; ``check-model``
    already reports such a target, and refusing is the conservative answer.
    """
    type_paths = registry.source_paths
    candidates = [
        *((dt.name, type_paths.get(data_type_key(dt), ""), "DATA-TYPE") for dt in data_types),
        *(
            (mapping_set.name, mapped_sets.source_paths.get(mapping_set.name, ""), "DATA-TYPE-MAPPING-SET")
            for mapping_set in mapped_sets.sets
        ),
        *((iface.name, registry.interface_source_paths.get(iface.name, ""), "PORT-INTERFACE") for iface in interfaces),
        *((imported.swc.name, imported.path, "SW-COMPONENT-TYPE") for imported in imported_swcs),
        *(
            (element.name, type_paths[data_type_key(element)], ELEMENT_TAG_BY_LAYER[data_type_layer(element)])
            for element in elements
        ),
    ]
    return [skip for _, skip in reserved_device_name_skips(model, candidates)]


# ---------------------------------------------------------------------------
# Name refusals of the incoming closure
# ---------------------------------------------------------------------------


def _refuse_incoming_name_defects(
    swc: SWComponent,
    interfaces: list[Interface],
    data_types: list[DataType],
    source_names: Mapping[tuple[str, str], str],
    mapping_sets: Sequence[MappingSet],
    elements: Sequence[DataDescriptionElement],
) -> None:
    """Run step 3a's name judgements of the incoming closure, each raising its own refusal.

    Asked before the target is read and again after the late pass, over the resolved component. Each
    check returns findings or raises, so a re-run over a clean component duplicates no line.
    """
    identifier_violations = illegal_identifiers(swc, interfaces, data_types, source_names, mapping_sets, elements)
    if identifier_violations:
        raise ReservedNameError(
            "component import aborted with nothing written: the export declares a name that is not a "
            "legal AUTOSAR Identifier, so the resulting model could not be generated or validated -- "
            + "; ".join(identifier_violations),
            identifier_violations,
        )

    # The access-point collision is a property of the incoming closure too. Its own sentence:
    # every name involved is a legal Identifier.
    access_collisions = access_point_collisions(swc)
    if access_collisions:
        raise ReservedNameError(
            "component import aborted with nothing written: two access entries of one runnable are written "
            "with one access point SHORT-NAME, which the ARXML writer refuses to emit -- "
            + "; ".join(access_collisions),
            access_collisions,
        )

    # A runnable named like a generated event: a foreign export's event `TE_Foo` still makes the
    # writer emit `TMT_Foo` beside a runnable `TMT_Foo`. The `--update` path asks again after its
    # carried events, the one place target state takes part.
    _refuse_runnable_event_collisions(swc)

    # Names equal ignoring case in one name space below the package level; the package level is
    # asked once, later, over what the import leaves (`_place_closure`).
    _refuse_element_name_space_collisions(swc, interfaces, data_types, mapping_sets, elements)


def _refuse_runnable_event_collisions(swc: SWComponent, *, already: SWComponent | None = None) -> None:
    """Raise :class:`ReservedNameError` when a runnable of *swc* takes an event's name, or two events take one.

    ``rule:name.runnable-event-collision`` and ``rule:name.duplicate-event`` through the validator's
    predicates, asked at step 3a and again of the component an ``--update`` carry built. Over the
    update overlay *already* is the project's component: a finding it already has is the project's
    own and refuses nothing; only one the overlay introduces does.
    """
    known_collisions = {collision.message for collision in runnable_event_collisions(already)} if already else set()
    known_duplicates = {duplicate.text for duplicate in duplicate_event_names(already)} if already else set()
    event_collisions = [
        collision.message for collision in runnable_event_collisions(swc) if collision.message not in known_collisions
    ]
    if event_collisions:
        raise ReservedNameError(
            "component import aborted with nothing written: a runnable takes the SHORT-NAME of an event the "
            "ARXML writer generates for the same component, which it refuses to emit -- " + "; ".join(event_collisions),
            event_collisions,
        )
    # Two events written with one SHORT-NAME, which only a stated name produces.
    event_duplicates = [
        duplicate.text for duplicate in duplicate_event_names(swc) if duplicate.text not in known_duplicates
    ]
    if event_duplicates:
        raise ReservedNameError(
            "component import aborted with nothing written: two events of one component would be written with "
            "one SHORT-NAME, which the ARXML writer refuses to emit -- " + "; ".join(event_duplicates),
            event_duplicates,
        )


def _refuse_element_name_space_collisions(
    swc: SWComponent,
    interfaces: Sequence[Interface],
    data_types: Sequence[DataType],
    mapping_sets: Sequence[MappingSet],
    elements: Sequence[DataDescriptionElement],
) -> None:
    """Raise :class:`ReservedNameError` for a name-space group below the package level in the incoming closure.

    ``rule:name.name-space-collision`` through the validator's predicate, kept at
    :data:`~swcraft_tools.validator.NAME_SPACE_LEVEL_ELEMENT`, over the component and every incoming
    shared element (its target copy was judged when written). Asked at step 3a and, on ``--update``,
    again after every carry. Fails closed: this path renames nothing.
    """
    collisions = [
        collision.sentence
        for collision in name_space_collisions([swc], interfaces, data_types, [], [], mapping_sets, elements)
        if collision.level == NAME_SPACE_LEVEL_ELEMENT
    ]
    if collisions:
        raise ReservedNameError(
            "component import aborted with nothing written: the export holds names equal ignoring case in one "
            "name space of the generated ARXML, which the ARXML writer refuses to emit -- " + "; ".join(collisions),
            collisions,
        )


# ---------------------------------------------------------------------------
# The reserved-name fold and package placement.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PlacedClosure:
    """The incoming closure after package placement, and the report's placement lines."""

    swc: SWComponent
    data_types: list[DataType]
    interfaces: list[Interface]
    mapping_sets: list[MappingSet]
    lines: list[str]
    elements: list[DataDescriptionElement]


def _closure_paths(
    swc: SWComponent,
    swc_path: str,
    registry: PathRegistry,
    mapped_sets: MappedSets,
) -> dict[tuple[str, str], str]:
    """Return each incoming element's full source path, by ``(layer, name)``."""
    return {
        **registry.source_paths,
        **{("interfaces", name): path for name, path in registry.interface_source_paths.items()},
        **{("mapping_sets", name): path for name, path in mapped_sets.source_paths.items()},
        ("swc", swc.name): swc_path,
    }


def _refuse_case_only_pairs_in_one_package(
    model: ImportModel,
    swc: SWComponent,
    swc_path: str,
    data_types: list[DataType],
    interfaces: list[Interface],
    registry: PathRegistry,
    mapped_sets: MappedSets,
    elements: Sequence[DataDescriptionElement],
) -> None:
    """Refuse a closure declaring two names of one package that differ only in case.

    ``[constr_2508]`` makes a ``SHORT-NAME`` unique ignoring case within its package, so two such
    declarations in **one** source package are the file's own defect, which ``import model`` skips and
    this path refuses with nothing written. Two spellings from **two** source packages are not a
    defect: placement keeps them apart after the target is read.

    Raises:
        ReservedNameError: at least one such pair.
    """
    paths = _closure_paths(swc, swc_path, registry, mapped_sets)
    lines: list[str] = []
    for root, members in root_package_members([swc], interfaces, data_types, [], mapped_sets.sets, elements).items():
        decided = place_case_variants(
            root,
            (PlacementMember(member.layer, member.name, paths[(member.layer, member.name)]) for member in members),
        )
        lines.extend(
            f"{PLACEMENT_WHAT[pair.dropped.layer]} '{pair.dropped.name}' ({pair.dropped.source_path}) beside "
            f"'{pair.kept.name}' ({pair.kept.source_path})"
            for pair in decided.same_package
        )
    if lines:
        raise ReservedNameError(
            "component import aborted with nothing written: the export declares two names differing only in case "
            "in one package, which AUTOSAR forbids -- a SHORT-NAME is unique ignoring case within its package "
            f"({cite(StandardRule.SHORT_NAME_UNIQUE, model.declared_release)}): " + "; ".join(lines),
            lines,
        )


def _place_closure(
    swc: SWComponent,
    swc_path: str,
    data_types: list[DataType],
    interfaces: list[Interface],
    registry: PathRegistry,
    mapped_sets: MappedSets,
    target_swcs: Mapping[str, SWComponent],
    target_interfaces: Mapping[str, Interface],
    target_types: Mapping[tuple[str, str], DataType],
    target_compositions: Sequence[Composition],
    target_mapping_sets: Mapping[str, MappingSet],
    elements: Sequence[DataDescriptionElement],
    target_elements: Mapping[tuple[str, str], DataDescriptionElement],
) -> _PlacedClosure:
    """Place the incoming definitions this import **creates** in their packages.

    Over the model the import will leave: the target's elements **never move**, and an incoming
    definition of a name the target declares is the conflict policy's, not placed here. A created
    element at ``/{Root}/{package}/{name}`` keeps that ``package``; one differing only in case from a
    target element or another created one takes the last segment of its source package. Then the
    package level is checked once: every ``rule:name.name-space-collision`` group a created element
    takes part in is refused; a group wholly inside the target is not this import's.

    Raises:
        ReservedNameError: a created element must move but sits in the root package itself, so
            there is no source package to take (the message names the target element to place by
            hand); or the placed model still holds a package-level group.
    """
    created = {
        **({} if swc.name in target_swcs else {("swc", swc.name): swc}),
        **{
            (data_type_layer(dt), dt.name): dt
            for dt in data_types
            if (data_type_layer(dt), dt.name) not in target_types and not isinstance(dt, ExternalDataType)
        },
        **{
            ("interfaces", iface.name): iface
            for iface in interfaces
            if iface.name not in target_interfaces and not isinstance(iface, ExternalInterface)
        },
        **{("mapping_sets", s.name): s for s in mapped_sets.sets if s.name not in target_mapping_sets},
        **{data_type_key(e): e for e in elements if data_type_key(e) not in target_elements},
    }
    paths = _closure_paths(swc, swc_path, registry, mapped_sets)

    def after(
        packages: Mapping[tuple[str, str], str],
    ) -> tuple[list[Any], list[Any], list[Any], list[Any], list[Any]]:
        def placed(layer: str, element: Any) -> Any:
            package = packages.get((layer, element.name))
            return element if package is None else element.model_copy(update={"package": package})

        return (
            [*target_swcs.values(), *(placed("swc", e) for (layer, _), e in created.items() if layer == "swc")],
            [
                *target_interfaces.values(),
                *(placed("interfaces", e) for (layer, _), e in created.items() if layer == "interfaces"),
            ],
            [
                *target_types.values(),
                *(
                    placed(layer, e)
                    for (layer, _), e in created.items()
                    if layer in ("implementation_types", "base_types", "application_types")
                ),
            ],
            [
                *target_mapping_sets.values(),
                *(placed("mapping_sets", e) for (layer, _), e in created.items() if layer == "mapping_sets"),
            ],
            [
                *target_elements.values(),
                *(placed(layer, e) for (layer, _), e in created.items() if layer in ELEMENT_LAYERS),
            ],
        )

    kept: dict[tuple[str, str], str] = {}
    for (layer, name), element in created.items():
        package = kept_package(home_package(layer, element), name, paths[(layer, name)])
        if package is not None:
            kept[(layer, name)] = package

    swcs_now, interfaces_now, types_now, sets_now, elements_now = after(kept)
    packages = dict(kept)
    lines: list[str] = []
    refusals: list[str] = []
    for root, members in root_package_members(
        swcs_now, interfaces_now, types_now, target_compositions, sets_now, elements_now
    ).items():
        decided = place_case_variants(
            root,
            (
                PlacementMember(
                    member.layer,
                    member.name,
                    # A created element's path is indexed, so a miss is a KeyError and never a
                    # package called "" (``import model``'s rule); a target's is "" and unused,
                    # because a fixed member never moves.
                    paths[(member.layer, member.name)] if (member.layer, member.name) in created else "",
                    member.generated,
                    member.package,
                    fixed=(member.layer, member.name) not in created,
                )
                for member in members
            ),
        )
        for twin in decided.unplaceable:
            refusals.append(
                f"the incoming {PLACEMENT_WHAT[twin.declaration.layer]} '{twin.declaration.name}' "
                f"({twin.declaration.source_path}) differs only in case from the target's "
                f"{PLACEMENT_WHAT[twin.twin.layer]} '{twin.twin.name}', and the file declares it in /{root} "
                "itself, so it has no source package to be placed in -- set package on the target's "
                f"'{twin.twin.name}' to place it in a sub-package first, or rename one"
            )
        for placed in decided.placed:
            packages[(placed.declaration.layer, placed.declaration.name)] = placed.package
            lines.append(package_placement_line(placed, root))
    if refusals:
        raise ReservedNameError(
            "component import aborted with nothing written: " + "; ".join(refusals),
            refusals,
        )

    swcs_after, interfaces_after, types_after, sets_after, elements_after = after(packages)
    # A finding's subject is the element itself or a place inside it; compared without the path.
    created_subjects = {Subject(layer, name) for layer, name in created}
    collisions = [
        collision.sentence
        for collision in name_space_collisions(
            swcs_after, interfaces_after, types_after, target_compositions, [], sets_after, elements_after
        )
        if collision.level == NAME_SPACE_LEVEL_PACKAGE
        and any(replace(subject, path=()) in created_subjects for subject in collision.subjects)
    ]
    if collisions:
        raise ReservedNameError(
            "component import aborted with nothing written: the resulting model would hold names equal ignoring "
            "case in one package of the generated ARXML -- " + "; ".join(collisions),
            collisions,
        )

    def own(layer: str, element: Any) -> Any:
        package = packages.get((layer, element.name))
        if package is None or (layer, element.name) not in created:
            return element
        return element.model_copy(update={"package": package})

    return _PlacedClosure(
        swc=own("swc", swc),
        data_types=[own(data_type_layer(dt), dt) for dt in data_types],
        interfaces=[own("interfaces", iface) for iface in interfaces],
        mapping_sets=[own("mapping_sets", s) for s in mapped_sets.sets],
        lines=lines,
        elements=[own(data_type_layer(e), e) for e in elements],
    )


def _blocking(entries: Iterable[SkipEntry]) -> list[SkipEntry]:
    """Return the entries of *entries* that refuse the import: every non-deferred ``ABORT``.

    The complement of :func:`_descriptive`.
    """
    return [e for e in entries if e.tag not in DEFERRED_ELEMENT_TAGS and e.effect == ABORT]
