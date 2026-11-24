"""``run_import()``: the ``import model`` pipeline over one ARXML export, step by step.

Read the whole export (a ``--packages`` selection narrows after mapping, never during the read) -> map every
layer and the composition hierarchy -> mint the external interface and type stubs -> apply the ``--packages``
dependency closure -> reconcile every name the ARXML writer reserves -> build the report -> write the YAML tree
(unless ``dry_run``). The passes between and after the mappers are ``orchestrator.py``'s. It writes no
requirement.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from swcraft_tools.arxml_import.connectors import (
    FinishedStubPorts,
    MappedCompositions,
    composition_index,
    finish_stub_ports,
    map_compositions,
    raw_swc_index,
)
from swcraft_tools.arxml_import.merge import (
    LAYER_APPLICATION_TYPES,
    LAYER_BASE_TYPES,
    LAYER_COMPOSITIONS,
    LAYER_CONNECTORS,
    LAYER_IMPLEMENTATION_TYPES,
    LAYER_INTERFACES,
    LAYER_MAPPING_SETS,
    LAYER_SWC,
    MergeDecisions,
    MergeGroup,
    narrow_to_selection,
)
from swcraft_tools.arxml_import.merge_verdict import decide_merges, record_dropped_nested_descriptions
from swcraft_tools.arxml_import.orchestrator import (
    ImportPlacement,
    PortOwner,
    ReservedSkip,
    StubContext,
    access_collapse_lines,
    async_call_finding_skips,
    held_port_init_labels,
    incompatible_map_lines,
    is_wired,
    member_type_key,
    name_equivalent_port_inits_through_a_stub,
    place_final_packages,
    pruned_compositions,
    refuse_package_level_collisions,
    remove_empty_root_compositions,
    skip_reserved_device_names,
    with_package,
    with_stubbed_references,
    without_inits_through_a_stub,
    without_port_inits_through_a_stub,
    without_variable_inits_through_a_stub,
)
from swcraft_tools.arxml_import.reconcile import (
    ReconciledModel,
    composition_with_renamed_interfaces,
    data_type_key,
    identifier_violations,
    reconcile_reserved_names,
    renamed_interface,
    swc_with_renamed_interfaces,
    swc_with_renamed_variable_types,
)
from swcraft_tools.arxml_import.registry import InitRadixNormalization, PathRegistry
from swcraft_tools.arxml_import.report import (
    ImportReport,
    closure_data_type_names,
    closure_element_keys,
    data_event_counts,
    deferred_tag_skips,
    exclusive_area_counts,
    generated_name_losses,
    imported_file_path,
    in_selected_package,
    interface_to_dict,
    select_swc_names,
    selection_reach,
    variable_counts,
    write_yaml,
)
from swcraft_tools.arxml_import.swc_mapper import map_swcs
from swcraft_tools.arxml_import.swc_mapping import (
    ImportedSwc,
    finish_stub_members,
    finish_stub_port_com_specs,
    settle_stated_names,
)
from swcraft_tools.arxml_import.type_mappers import map_data_types, map_interfaces
from swcraft_tools.arxml_import.type_mapping import (
    MappedSets,
    map_mapping_sets,
    map_variable_types,
)
from swcraft_tools.arxml_import.unresolved import (
    DestDisagreement,
    ExternalStub,
    MintedStubs,
    UnresolvedReference,
    UnresolvedReferences,
    disagreements_narrowed,
    owned_by,
    stubs_narrowed,
    without_owners,
)
from swcraft_tools.arxml_reader import (
    CompositionInfo,
    ImportModel,
    SkipEntry,
    SkipLog,
    SwcInfo,
    read_arxml,
)
from swcraft_tools.data_description import read_data_description
from swcraft_tools.model_registry import ModelRegistry, current_registry
from swcraft_tools.model_state import stored_element_files
from swcraft_tools.project import SwcraftProject, require_project
from swcraft_tools.reader import (
    read_compositions,
    read_connectors,
    read_interfaces,
    read_mapping_sets,
    read_requirements,
    read_sequences,
    read_swc_model,
    read_tasks,
)
from swcraft_tools.schema import (
    BaseDataType,
    Composition,
    Connector,
    DataConstr,
    DataDescriptionElement,
    DataType,
    ExternalDataType,
    ExternalInterface,
    Interface,
    Linear,
    MappingSet,
    TextTable,
    Unit,
)

# The derived serializer, not `yaml_documents.composition_to_dict`: it walks `model_fields`, so a field added to a
# composition kind is emitted with no edit, where a hand-written dump silently erases what it does not name.
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.writer.yaml_documents import (
    connector_to_dict,
    data_type_to_dict,
    swc_to_dict,
)

#: The two unresolved-reference roles a mapping set's own maps make. A record made only by these is about one map,
#: and a ``--packages`` narrowing can drop that map while keeping the set.
_MAP_REFERENCE_FIELDS = frozenset({"mapping_sets.map.application", "mapping_sets.map.implementation"})


def run_import(
    source: str,
    project_manifest: str,
    packages: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> ImportReport:
    """Import one ARXML export into the fresh SWCraft project named by `project_manifest`.

    No requirement is written and no component carries `satisfies`. The export is always read whole and
    `--packages` narrows after every layer is mapped, so reference resolution sees the whole file.

    Args:
        source: filesystem path to the ARXML export.
        project_manifest: path to the target project's swcraft.yaml. The project must already exist (`swcraft init`
            first); its model layers must be empty unless `force`.
        packages: optional package selection (bare short-names or full AUTOSAR package paths); `None` imports
            everything.
        dry_run: compute and report without writing anything.
        force: bypass the empty-project guard (still no merge protection: existing files with colliding names are
            overwritten).

    Returns:
        ImportReport. `report.total_imported == 0` is the CLI's exit-non-zero signal; a non-empty skip list alone is
        not failure.

    Raises:
        FileNotFoundError: `project_manifest` or `source` does not exist.
        ValueError: the target project model is non-empty and `force` was not passed; a malformed ARXML `source`; a
            `--packages` selection that matches no component or composition package; a source `SHORT-NAME` that is
            not a legal AUTOSAR `Identifier` (`reconcile.identifier_violations`); names equal ignoring case in one
            package that package placement could not keep apart.
        NameCollisionError: one ARXML path declared twice (a malformed export; a bare-name collision between
            distinct paths is always resolved, never refused), or a reserved-name reconciliation that did not
            settle (`reconcile.py`).
        pydantic.ValidationError: a mapped element fails schema.py validation.
    """
    project = require_project(project_manifest)
    model_root = project.resolved_model_root

    if not force:
        _refuse_existing_project(model_root)
    decided = _read_and_decide(source, packages)
    layers, results = _map_layers(decided)
    layers, minted, dropped_inits = _apply_stubs(decided, results, layers)
    records = _initial_records(results.registry, decided.unresolved, minted, dropped_inits)
    if packages is not None:
        layers, records = _narrow_to_packages(source, packages, decided, results, layers, records)
    layers, records, placed = _reconcile_and_place(source, decided, results, layers, records)
    layers, finished = _finish_stubs_and_names(decided, results, layers, placed)
    skip_entries = _collect_skips(project, decided, results, layers, placed, finished)

    # `PathRegistry.source_paths` builds a fresh dict on every access, so it is read once: rebuilt per item inside
    # a comprehension it cost 61 s on 200 unread-physics entries against 3 000 data types.
    type_source_paths = results.registry.source_paths

    report = _build_report(
        source,
        packages,
        dry_run,
        project,
        decided,
        results,
        layers,
        records,
        placed,
        finished,
        skip_entries,
        type_source_paths,
    )

    if report.total_imported == 0:
        # As write-free as dry_run: a stray file would trip the non-empty-project guard on a legitimate retry.
        return report

    root = Path(model_root)
    kernel = current_registry()
    batches = _plan_batches(results, layers, placed, type_source_paths)
    _write_batches(root, kernel, batches, layers, dry_run, report)
    return report


@dataclass(frozen=True)
class _Decided:
    """The export read whole, its two path indices, and the two policies every mapper is handed."""

    model: ImportModel
    raw_swc_by_path: dict[str, SwcInfo]
    compositions_by_path: dict[str, CompositionInfo]
    merges: MergeDecisions
    merge_skips: SkipLog
    unresolved: UnresolvedReferences


@dataclass(frozen=True)
class _Layers:
    """The seven layer lists of one import, each step's input and output."""

    data_types: list[DataType]
    elements: list[DataDescriptionElement]
    interfaces: list[Interface]
    swcs: list[ImportedSwc]
    compositions: list[Composition]
    connectors: list[Connector]
    mapping_sets: list[MappingSet]


@dataclass(frozen=True)
class _MapperResults:
    """What the mappers return beside the layers: the registry, the sets' and the hierarchy's records, the skip logs."""

    registry: PathRegistry
    mapped_sets: MappedSets
    mapped: MappedCompositions
    dt_skips: SkipLog
    mapping_set_skips: SkipLog
    iface_skips: SkipLog
    swc_skips: SkipLog
    composition_skips: SkipLog


@dataclass(frozen=True)
class _Records:
    """The records taken over the whole export, narrowed with the layers before the report reads them."""

    init_radix: list[InitRadixNormalization]
    unresolved_records: list[UnresolvedReference]
    stub_records: list[ExternalStub]
    disagreement_records: list[DestDisagreement]
    closure_lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Selection:
    """The layers a `--packages` selection keeps, and what it dropped from each."""

    layers: _Layers
    selected_packages: set[str]
    selected_swc_names: set[str]
    kept_interface_names: set[str]
    dropped_members: list[str]
    dropped_interfaces: list[Interface]
    dropped_types: list[DataType]
    dropped_elements: list[DataDescriptionElement]
    dropped_mapping_sets: list[MappingSet]
    dropped_maps: int
    dropped_connectors: list[Connector]


@dataclass(frozen=True)
class _Placed:
    """The reserved-name reconciliation, the skipped elements and the package placement."""

    reconciled: ReconciledModel
    reserved_skips: list[ReservedSkip]
    placement: ImportPlacement


@dataclass(frozen=True)
class _Finished:
    """The finished stub ports, the written names the report narrows to, and the names left unstated."""

    stub_ports: FinishedStubPorts
    written_stub_paths: set[str]
    written_interface_names: set[str]
    written_type_names: set[str]
    written_composition_names: set[str]
    names_not_stated: dict[str, str]


@dataclass(frozen=True)
class _ImportedFile:
    """One element `run_import()` writes: its identity, its full source path, its YAML mapping, its placement."""

    identity: str
    source: str
    data: dict[str, Any]
    placement: Mapping[str, str] | None = None
    #: The layer the element is written to when it is not its batch's (a base type, written in the data types'
    #: batch into `base_types`).
    layer: str | None = None


def _refuse_existing_project(model_root: str) -> None:
    """Refuse an import into a project whose model already holds content.

    Every layer the reader covers is read, not only the ones this importer
    writes, apart from `hardware` and the empty scaffolded root composition.

    Raises:
        ValueError: the model at *model_root* is not empty.
    """
    # Every new reader-covered layer must be added here. `hardware` is excluded because `swcraft init` scaffolds
    # it in every project and this importer never writes there. `compositions` is read by content: `init`
    # scaffolds an empty root, and only a composition declaring a port, a member, a delegation or `satisfies`
    # refuses the import -- the only shape `orchestrator.remove_empty_root_compositions` may remove.
    description = read_data_description(model_root)
    existing = (
        read_swc_model(model_root)
        or read_interfaces(model_root)
        or description.data_types
        or description.units
        or description.compu_methods
        or description.data_constraints
        or read_connectors(model_root)
        or read_requirements(model_root)
        or read_tasks(model_root)
        or read_sequences(model_root)
        or read_mapping_sets(model_root)
        # A root the architect linked is not the scaffold `init` writes.
        or [c for c in read_compositions(model_root) if c.ports or c.members or c.delegations or c.satisfies]
    )
    if existing:
        raise ValueError(
            f"Target project model at '{model_root}' is not empty. Import "
            "into a non-empty project is refused -- pass --force to import anyway "
            "(no merge protection: existing files with colliding names "
            "will be overwritten)."
        )


def _read_and_decide(source: str, packages: list[str] | None) -> _Decided:
    """Read the whole export, index it, and decide its merges and its unresolved-reference policy.

    Both decisions are taken once, before any mapping, and handed to every
    mapper. Under a `--packages` selection the merge verdict is narrowed to it
    first: a group conflicting over the whole file of which the selection keeps
    exactly one member is re-admitted as that member.
    """
    model = read_arxml(source)

    raw_swc_by_path = raw_swc_index(model)
    compositions_by_path = composition_index(model)

    # Two declarations of one `(layer, kind, SHORT-NAME)` are one element when nothing SWCraft can read
    # distinguishes them and none when something does. A policy handed to each mapper rather than a filter
    # afterwards: a merged group must be mapped once and a conflicting one not at all, and a post-hoc filter would
    # have to undo the registry's name reservations.
    merges = decide_merges(model)
    if packages is not None and merges.conflicting:
        # Narrowed here, not after the closure, because a re-admitted member has to be mapped; `selection_reach`
        # reconstructs the closure over declaration paths. It takes the un-narrowed verdict, because narrowing is
        # what its answer decides (asking the narrowed one would be a cycle).
        inside, reached = selection_reach(model, set(packages), merges)
        merges = narrow_to_selection(merges, selected=inside, reached=reached)
    # A merged group keeps its primary's members, so every nested description of a later member is one skip
    # line, never dropped without a word.
    merge_skips = SkipLog()
    record_dropped_nested_descriptions(merges, merge_skips)
    # An element whose reference names nothing this export declares is kept with the full path in the field, and
    # the fact recorded; a policy for the same reason as the merges: it is decided where the element is built,
    # while the registry is handing out names.
    unresolved = UnresolvedReferences(model, merges)
    return _Decided(model, raw_swc_by_path, compositions_by_path, merges, merge_skips, unresolved)


def _map_layers(decided: _Decided) -> tuple[_Layers, _MapperResults]:
    """Map every layer, in dependency order, and the composition hierarchy over the components kept.

    The data types first, then the mapping sets, the interfaces, the
    inter-runnable variables' types, the components and the compositions with
    their connectors and external component stubs.

    Returns:
        The mapped layers, and the mappers' registry, records and skip logs.
    """
    model = decided.model
    merges = decided.merges
    unresolved = decided.unresolved
    # Every unit, compu method and data constraint the model can hold is created as an element the types name;
    # `elements` is filled by the call and threaded beside `data_types` through every later pass.
    elements: list[DataDescriptionElement] = []
    data_types, registry, dt_skips = map_data_types(model, merges=merges, unresolved=unresolved, elements=elements)
    # Before `map_swcs`, whose `data_type_mappings` are the set names decided here; after `map_data_types`,
    # through whose registry both sides of every map resolve.
    mapped_sets, mapping_set_skips = map_mapping_sets(model, registry, data_types, merges=merges, unresolved=unresolved)
    mapping_sets = mapped_sets.sets
    interfaces, iface_skips = map_interfaces(
        model, registry, data_types, merges=merges, unresolved=unresolved, elements=elements
    )
    # Every inter-runnable variable's type and value, decided as a data element's are, and spent by `map_swcs`.
    registry.variable_types = map_variable_types(model, registry, data_types, elements=elements)
    imported_swcs, swc_skips = map_swcs(
        model,
        interfaces,
        iface_skip_log=iface_skips,
        merges=merges,
        unresolved=unresolved,
        mapping_set_name_by_path=mapped_sets.name_by_path,
        port_inits=registry.port_inits,
        variable_types=registry.variable_types,
    )
    # After `map_swcs`: whether a prototype becomes a member or an external stub depends on what `map_swcs` kept,
    # not on what the export declares.
    mapped, composition_skips = map_compositions(
        model,
        imported_swcs,
        interfaces,
        iface_skips,
        registry,
        merges=merges,
        unresolved=unresolved,
        swc_skip_log=swc_skips,
    )
    compositions = mapped.compositions
    connectors = mapped.connectors
    layers = _Layers(
        data_types=data_types,
        elements=elements,
        interfaces=interfaces,
        swcs=imported_swcs,
        compositions=compositions,
        connectors=connectors,
        mapping_sets=mapping_sets,
    )
    results = _MapperResults(
        registry=registry,
        mapped_sets=mapped_sets,
        mapped=mapped,
        dt_skips=dt_skips,
        mapping_set_skips=mapping_set_skips,
        iface_skips=iface_skips,
        swc_skips=swc_skips,
        composition_skips=composition_skips,
    )
    return layers, results


def _apply_stubs(
    decided: _Decided, results: _MapperResults, layers: _Layers
) -> tuple[_Layers, MintedStubs, frozenset[str]]:
    """Mint the external interface and type stubs and re-point every layer at them.

    Every reference that names a minted stub follows it; an interface stub lists
    the members the import's constructs name under it; an `init` whose type
    reaches a type stub is dropped; and the external component stubs join the
    component list, appended after the mapped components.

    Returns:
        The re-pointed layers, the minted stubs, and the paths of the data
        elements whose `init` was dropped.
    """
    unresolved = decided.unresolved
    registry = results.registry
    mapped = results.mapped
    iface_skips = results.iface_skips
    swc_skips = results.swc_skips
    composition_skips = results.composition_skips
    data_types = layers.data_types
    interfaces = layers.interfaces
    imported_swcs = layers.swcs
    compositions = layers.compositions
    # Every layer is mapped, so every staged unresolved occurrence is committed or abandoned and every name the
    # import writes or claims is known. A stub reserves only those names (a conflicting group's stays claimed, as
    # the model keeps references to it); a stub's `_ext<X>` number is ranked here by sorted path, so a later drop
    # leaves a gap. Before `--packages` and phase B, so a selection prunes a stub like any unreferenced element,
    # and phase B sees the stubs' names as taken.
    minted = unresolved.mint_stubs(
        registry,
        interface_names={interface.name for interface in interfaces} | set(registry.interface_source_paths),
        # A type stub is an implementation type: names are reserved per layer, so one only an application or a
        # base type holds is free.
        type_names={dt.name for dt in data_types if data_type_key(dt)[0] == LAYER_IMPLEMENTATION_TYPES}
        | registry.claimed_names(LAYER_IMPLEMENTATION_TYPES),
    )
    stub_renames = {(LAYER_IMPLEMENTATION_TYPES, path): name for path, name in minted.type_names.items()}
    interfaces = [
        *(renamed_interface(interface, stub_renames) for interface in interfaces),
        *minted.interfaces,
    ]
    data_types = [*(with_stubbed_references(dt, minted.type_names) for dt in data_types), *minted.data_types]
    imported_swcs = [swc_with_renamed_interfaces(imported, minted.interface_names) for imported in imported_swcs]
    # A variable kept typed by an unresolved path follows the type stub minted for it, as a data element does.
    imported_swcs = [swc_with_renamed_variable_types(imported, stub_renames) for imported in imported_swcs]
    compositions = [
        composition_with_renamed_interfaces(composition, minted.interface_names) for composition in compositions
    ]
    # An interface stub lists the members the import's constructs name under it, so every event, access and
    # com-spec held for this decision is kept once its member is listed. Here, before the selection, the
    # reserved-name pass and `settle_stated_names`.
    imported_swcs = [finish_stub_members(imported, unresolved, swc_skips) for imported in imported_swcs]
    for held in mapped.held_stub_com_specs.values():
        finish_stub_port_com_specs(held, unresolved, composition_skips)
    # A data element typed by a reference chain that now ends at a type stub meets
    # `rule:interface.init-on-external-type`, which `map_interfaces` could not see: its `init` is dropped, and its
    # radix line with it, since a report line must not name an init in no written file.
    interfaces, dropped_inits = without_inits_through_a_stub(
        interfaces, data_types, registry, unresolved, iface_skips, minted
    )
    # A port's own value for such an element goes the same way.
    stub_context = StubContext.of(interfaces, data_types, minted)
    imported_swcs = without_port_inits_through_a_stub(imported_swcs, stub_context, unresolved, swc_skips)
    # And an inter-runnable variable's.
    imported_swcs = without_variable_inits_through_a_stub(imported_swcs, stub_context, unresolved, swc_skips)
    port_owners = [
        *(
            PortOwner(imported.swc.ports, imported.port_init_equivalents, imported.port_init_labels, swc_skips)
            for imported in imported_swcs
        ),
        *(
            PortOwner(
                composition.ports,
                mapped.port_init_equivalents.get(composition.name, []),
                mapped.port_init_labels.get(composition.name, []),
                composition_skips,
            )
            for composition in compositions
        ),
    ]
    # A port's own value carried by equivalence with the element's is named.
    name_equivalent_port_inits_through_a_stub(port_owners, stub_context, unresolved)
    # A port's own labelled value's SHORT-LABEL line is recorded only where the output still holds an INIT-VALUE at
    # that com-spec.
    held_port_init_labels(port_owners, stub_context)
    # Joined here so `--packages`, the reserved-name pass and the write loop treat them as mapped components.
    imported_swcs = [*imported_swcs, *mapped.stubs]
    layers = replace(
        layers, data_types=data_types, interfaces=interfaces, swcs=imported_swcs, compositions=compositions
    )
    return layers, minted, dropped_inits


def _initial_records(
    registry: PathRegistry, unresolved: UnresolvedReferences, minted: MintedStubs, dropped_inits: frozenset[str]
) -> _Records:
    """The radix, unresolved-reference, stub and disagreement records, before any narrowing."""
    # These records are taken over the whole export and narrowed with the layers (`_narrow_records`): a report
    # line must not name an element in no written file.
    init_radix = [entry for entry in registry.init_radix_normalizations if entry.element_path not in dropped_inits]
    unresolved_records = list(unresolved.references)
    stub_records = list(minted.stubs)
    disagreement_records = list(minted.disagreements)
    return _Records(init_radix, unresolved_records, stub_records, disagreement_records)


def _narrow_to_packages(
    source: str,
    packages: list[str],
    decided: _Decided,
    results: _MapperResults,
    layers: _Layers,
    records: _Records,
) -> tuple[_Layers, _Records]:
    """Narrow the layers and the records to a `--packages` selection's dependency closure.

    Returns:
        The narrowed layers, and the narrowed records carrying the closure lines.
    """
    narrowed = _narrow_layers(source, packages, decided, results, layers)
    registry = results.registry
    compositions = narrowed.layers.compositions
    data_type_source_paths = registry.source_paths
    kept_composition_names = {comp.name for comp in compositions}
    records = _narrow_records(decided, results, narrowed, records, data_type_source_paths, kept_composition_names)
    closure_lines = _selection_lines(decided, results, narrowed, data_type_source_paths, kept_composition_names)
    return narrowed.layers, replace(records, closure_lines=closure_lines)


def _narrow_layers(
    source: str, packages: list[str], decided: _Decided, results: _MapperResults, layers: _Layers
) -> _Selection:
    """The layers a `--packages` selection keeps: its components and compositions, and what they need.

    The components and compositions the selection names, a kept composition
    losing every member whose type fell outside it, and the interfaces their
    ports name; then the rest of the closure (`_narrow_closure`).

    Raises:
        ValueError: the selection matches no component or composition package.
    """
    model = decided.model
    merges = decided.merges
    raw_swc_by_path = decided.raw_swc_by_path
    compositions_by_path = decided.compositions_by_path
    mapped = results.mapped
    interfaces = layers.interfaces
    imported_swcs = layers.swcs
    compositions = layers.compositions
    selection = set(packages)
    selected_swc_names, selected_composition_paths = select_swc_names(
        model,
        imported_swcs,
        selection,
        raw_swc_by_path,
        compositions_by_path,
        merges,
    )
    if not selected_swc_names and not selected_composition_paths:
        raise ValueError(
            f"--packages selection {sorted(selection)} matched no SWC or "
            f"composition package in '{source}' -- unresolvable closure."
        )
    selected_composition_names = {
        compositions_by_path[path].name for path in selected_composition_paths if path in compositions_by_path
    }
    stub_names = {stub.swc.name for stub in mapped.stubs}
    # A kept composition loses every member whose type fell outside, and every delegation naming one, or
    # `rule:composition.member-unknown-type` would refuse the project. An external stub is in no package, so it
    # survives as long as a kept composition places it.
    compositions, dropped_members = pruned_compositions(
        compositions, selected_composition_names, selected_swc_names | stub_names
    )
    placed_names = {member.type for comp in compositions for member in comp.members if member.type is not None}
    imported_swcs = [
        imported
        for imported in imported_swcs
        if (imported.swc.name in placed_names if imported.swc.external else imported.swc.name in selected_swc_names)
    ]

    required_interface_names: set[str] = set()
    for imported in imported_swcs:
        for port in imported.swc.ports:
            # An external stub's port declares no interface.
            if port.interface is not None:
                required_interface_names.add(port.interface)
    for comp in compositions:
        for composition_port in comp.ports:
            required_interface_names.add(composition_port.interface)
    dropped_interfaces = [i for i in interfaces if i.name not in required_interface_names]
    interfaces = [i for i in interfaces if i.name in required_interface_names]
    # `reconcile_reserved_names` renames but drops no interface, so the surviving set is decided here.
    kept_interface_names = {interface.name for interface in interfaces}
    return _narrow_closure(
        results,
        replace(layers, interfaces=interfaces, swcs=imported_swcs, compositions=compositions),
        selection,
        selected_swc_names=selected_swc_names,
        kept_interface_names=kept_interface_names,
        dropped_members=dropped_members,
        dropped_interfaces=dropped_interfaces,
    )


def _narrow_closure(
    results: _MapperResults,
    layers: _Layers,
    selection: set[str],
    *,
    selected_swc_names: set[str],
    kept_interface_names: set[str],
    dropped_members: list[str],
    dropped_interfaces: list[Interface],
) -> _Selection:
    """The rest of a selection's closure over the kept components, compositions and interfaces.

    A data type, a unit, compu method or data constraint element is kept when
    something kept references it; a mapping set when a kept component names it
    or its declaration is inside a selected package, with its maps narrowed to
    the types kept; a connector when both its ends are still members of its
    composition.
    """
    mapped_sets = results.mapped_sets
    data_types = layers.data_types
    elements = layers.elements
    interfaces = layers.interfaces
    imported_swcs = layers.swcs
    compositions = layers.compositions
    connectors = layers.connectors
    mapping_sets = layers.mapping_sets
    # The sets are read by the closure, not narrowed before it: a kept application type pulls in the
    # implementation type that stores it, or `rule:data-type.application-type-unmapped` would fire. The kept
    # components' variables are roots too.
    required_type_keys = closure_data_type_names(
        interfaces, data_types, mapping_sets, swcs=[imported.swc for imported in imported_swcs]
    )
    dropped_types = [dt for dt in data_types if data_type_key(dt) not in required_type_keys]
    data_types = [dt for dt in data_types if data_type_key(dt) in required_type_keys]
    # The elements the kept types name, and no other.
    required_element_keys = closure_element_keys(data_types, elements)
    dropped_elements = [element for element in elements if data_type_key(element) not in required_element_keys]
    elements = [element for element in elements if data_type_key(element) in required_element_keys]

    # A set nothing references but declared inside a selected package is kept: dropping it would lose the mapping
    # of an application type the selection kept. Its maps are narrowed to the types kept, or
    # `rule:mapping-set.unknown-*` would fire; one entry it cannot carry is no reason to lose the others.
    named_set_names = {name for imported in imported_swcs for name in imported.swc.data_type_mappings}

    dropped_mapping_sets = [
        mapping_set
        for mapping_set in mapping_sets
        if not _set_kept(mapping_set, named_set_names, mapped_sets, selection)
    ]
    mapping_sets = [
        mapping_set for mapping_set in mapping_sets if _set_kept(mapping_set, named_set_names, mapped_sets, selection)
    ]

    dropped_maps = 0
    narrowed_sets: list[MappingSet] = []
    for mapping_set in mapping_sets:
        # Each side in the layer it names.
        kept_maps = [
            entry
            for entry in mapping_set.maps
            if _side_kept((LAYER_APPLICATION_TYPES, entry.application), required_type_keys)
            and _side_kept((LAYER_IMPLEMENTATION_TYPES, entry.implementation), required_type_keys)
        ]
        dropped_maps += len(mapping_set.maps) - len(kept_maps)
        narrowed_sets.append(mapping_set.model_copy(update={"maps": kept_maps}))
    mapping_sets = narrowed_sets

    # A connector whose composition survived but whose endpoint type did not would be reported by
    # `rule:composition.connector-endpoint-not-a-member`.
    placed_by_composition = {comp.name: {member_type_key(member) for member in comp.members} for comp in compositions}
    kept_connectors = [c for c in connectors if is_wired(c, placed_by_composition)]
    dropped_connectors = [c for c in connectors if c not in kept_connectors]
    connectors = kept_connectors
    return _Selection(
        layers=_Layers(
            data_types=data_types,
            elements=elements,
            interfaces=interfaces,
            swcs=imported_swcs,
            compositions=compositions,
            connectors=connectors,
            mapping_sets=mapping_sets,
        ),
        selected_packages=selection,
        selected_swc_names=selected_swc_names,
        kept_interface_names=kept_interface_names,
        dropped_members=dropped_members,
        dropped_interfaces=dropped_interfaces,
        dropped_types=dropped_types,
        dropped_elements=dropped_elements,
        dropped_mapping_sets=dropped_mapping_sets,
        dropped_maps=dropped_maps,
        dropped_connectors=dropped_connectors,
    )


def _set_kept(mapping_set: MappingSet, named_set_names: set[str], mapped_sets: MappedSets, selection: set[str]) -> bool:
    """Whether a mapping set survives a selection: a kept component names it, or it is declared inside one."""
    return mapping_set.name in named_set_names or in_selected_package(
        mapped_sets.source_paths[mapping_set.name], selection
    )


def _side_kept(side: tuple[str, str], required_type_keys: set[tuple[str, str]]) -> bool:
    """Whether one side of a written map survives the selection.

    An emitted SWCraft name is an `Identifier` and never begins with `/`; the AUTOSAR **path** an unresolved
    reference carries verbatim always does. Such a side is kept: the reference is kept and `check-model` reports
    it, and a selection does not withdraw that.
    """
    return side in required_type_keys or side[1].startswith("/")


def _narrow_records(
    decided: _Decided,
    results: _MapperResults,
    narrowed: _Selection,
    records: _Records,
    data_type_source_paths: dict[tuple[str, str], str],
    kept_composition_names: set[str],
) -> _Records:
    """The records narrowed to the declarations a `--packages` selection kept.

    A radix line to an interface kept; an unresolved-reference record to the referrers a kept declaration made and
    the maps a kept set still carries; a stub's and a disagreement's referrers to those a kept declaration made.
    """
    unresolved = decided.unresolved
    compositions_by_path = decided.compositions_by_path
    registry = results.registry
    mapped_sets = results.mapped_sets
    data_types = narrowed.layers.data_types
    interfaces = narrowed.layers.interfaces
    imported_swcs = narrowed.layers.swcs
    mapping_sets = narrowed.layers.mapping_sets
    kept_interface_names = narrowed.kept_interface_names
    stub_records = records.stub_records
    disagreement_records = records.disagreement_records
    init_radix = [entry for entry in records.init_radix if entry.interface in kept_interface_names]

    # Every unresolved-reference referrer is a declaration or a port one segment below it, so the kept
    # declarations' paths are what `narrowed` needs.
    kept_declaration_paths = {imported.path for imported in imported_swcs}
    kept_declaration_paths |= {
        path for path, info in compositions_by_path.items() if info.name in kept_composition_names
    }
    kept_declaration_paths |= {
        registry.interface_source_paths[interface.name]
        for interface in interfaces
        if interface.name in registry.interface_source_paths
    }
    kept_declaration_paths |= {
        data_type_source_paths[data_type_key(data_type)]
        for data_type in data_types
        if data_type_key(data_type) in data_type_source_paths
    }
    # Unguarded: every set keeps its mapped name, and this runs before phase B's renames.
    kept_declaration_paths |= {mapped_sets.source_paths[mapping_set.name] for mapping_set in mapping_sets}
    unresolved_records = list(unresolved.narrowed(frozenset(kept_declaration_paths)))
    # A kept set may have lost the map that made a reference, and a record about a row the set does not carry says
    # the project is open when it is clean; keyed on the paths the written maps hold.
    written_map_paths = {
        side
        for mapping_set in mapping_sets
        for entry in mapping_set.maps
        for side in (entry.application, entry.implementation)
    }
    kept_records: list[UnresolvedReference] = []
    for record in unresolved_records:
        if record.target in written_map_paths:
            kept_records.append(record)
            continue
        referrers = [referrer for referrer in record.referrers if referrer.field not in _MAP_REFERENCE_FIELDS]
        if len(referrers) == len(record.referrers):
            kept_records.append(record)
        elif referrers:
            kept_records.append(replace(record, referrers=referrers))
    unresolved_records = kept_records
    kept_owner = owned_by(frozenset(kept_declaration_paths))
    stub_records = stubs_narrowed(stub_records, kept_owner)
    disagreement_records = disagreements_narrowed(disagreement_records, kept_owner)
    return replace(
        records,
        init_radix=init_radix,
        unresolved_records=unresolved_records,
        stub_records=stub_records,
        disagreement_records=disagreement_records,
    )


def _selection_lines(
    decided: _Decided,
    results: _MapperResults,
    narrowed: _Selection,
    data_type_source_paths: dict[tuple[str, str], str],
    kept_composition_names: set[str],
) -> list[str]:
    """A `--packages` selection's report lines, the closure lines.

    The re-admitted conflicting groups, written or excluded, then the
    selection's count and its exclusions (`_exclusion_lines`).
    """
    merges = decided.merges
    compositions_by_path = decided.compositions_by_path
    registry = results.registry
    mapped = results.mapped
    mapped_sets = results.mapped_sets
    data_types = narrowed.layers.data_types
    imported_swcs = narrowed.layers.swcs
    connectors = narrowed.layers.connectors
    mapping_sets = narrowed.layers.mapping_sets
    kept_interface_names = narrowed.kept_interface_names
    closure_lines: list[str] = []
    # The re-admission line names what the import wrote: a re-admitted declaration nothing kept references is
    # pruned by the closure, so it takes the second line rather than none, since its conflict line is suppressed by
    # the re-admission. Asked per layer, because a declaration and the element it lands in are not one-to-one (a
    # twin collapse writes two declarations as one element).
    kept_type_keys = {data_type_key(data_type) for data_type in data_types}
    kept_swc_paths = {imported.path for imported in imported_swcs}
    kept_connector_names = {connector.name for connector in connectors}
    interface_name_by_path = {path: name for name, path in registry.interface_source_paths.items()}
    connector_name_by_path = {path: name for name, path in mapped.connector_source_paths.items()}

    reselected_written = [
        group
        for group in merges.reselected
        if _reselection_was_written(
            group,
            registry=registry,
            kept_type_keys=kept_type_keys,
            interface_name_by_path=interface_name_by_path,
            kept_interface_names=kept_interface_names,
            kept_swc_paths=kept_swc_paths,
            compositions_by_path=compositions_by_path,
            kept_composition_names=kept_composition_names,
            connector_name_by_path=connector_name_by_path,
            kept_connector_names=kept_connector_names,
            mapped_sets=mapped_sets,
            mapping_sets=mapping_sets,
        )
    ]
    reselected_pruned = [group for group in merges.reselected if group not in reselected_written]
    if reselected_written:
        closure_lines.append(
            f"{len(reselected_written)} conflicting name group(s) re-admitted (the selection "
            "keeps one declaration of each): "
            + ", ".join(sorted(f"{group.key} at {group.primary}" for group in reselected_written))
        )
    if reselected_pruned:
        closure_lines.append(
            f"{len(reselected_pruned)} conflicting name group(s) re-admitted and then excluded "
            "(the selection keeps one declaration of each, and nothing it kept references that "
            "declaration): " + ", ".join(sorted(f"{group.key} at {group.primary}" for group in reselected_pruned))
        )
    closure_lines.extend(_exclusion_lines(narrowed, registry, data_type_source_paths))
    return closure_lines


def _exclusion_lines(
    narrowed: _Selection,
    registry: PathRegistry,
    data_type_source_paths: dict[tuple[str, str], str],
) -> list[str]:
    """The selection's count and one line per kind of element it excluded.

    An element declared outside the selection is told apart from one inside it
    that nothing kept references; an external stub takes no line.
    """
    data_types = narrowed.layers.data_types
    interfaces = narrowed.layers.interfaces
    selection = narrowed.selected_packages
    selected_swc_names = narrowed.selected_swc_names
    dropped_interfaces = narrowed.dropped_interfaces
    dropped_types = narrowed.dropped_types
    dropped_elements = narrowed.dropped_elements
    dropped_connectors = narrowed.dropped_connectors
    dropped_members = narrowed.dropped_members
    dropped_mapping_sets = narrowed.dropped_mapping_sets
    dropped_maps = narrowed.dropped_maps
    closure_lines = [
        f"selected {len(selected_swc_names)} SWC(s) -> closure pulled in "
        f"{len(interfaces)} interface(s), {len(data_types)} data type(s)"
    ]

    # An external stub is no declaration of this export, neither outside nor inside a selected package; one
    # nothing kept references is dropped with no line of its own.
    dropped_interfaces = [i for i in dropped_interfaces if not isinstance(i, ExternalInterface)]
    dropped_types = [dt for dt in dropped_types if not isinstance(dt, ExternalDataType)]
    outside_interfaces = [
        i for i in dropped_interfaces if _declared_outside(registry.interface_source_paths.get(i.name), selection)
    ]
    unreferenced_interfaces = [i for i in dropped_interfaces if i not in outside_interfaces]
    outside_types = [
        dt for dt in dropped_types if _declared_outside(data_type_source_paths.get(data_type_key(dt)), selection)
    ]
    unreferenced_types = [dt for dt in dropped_types if dt not in outside_types]
    if outside_interfaces:
        closure_lines.append(
            f"{len(outside_interfaces)} interface(s) outside the selection excluded: "
            + ", ".join(sorted(i.name for i in outside_interfaces))
        )
    if unreferenced_interfaces:
        closure_lines.append(
            f"{len(unreferenced_interfaces)} interface(s) excluded (inside the selection, but "
            "nothing the selection kept references them): " + ", ".join(sorted(i.name for i in unreferenced_interfaces))
        )
    if outside_types:
        closure_lines.append(
            f"{len(outside_types)} data type(s) outside the selection excluded: "
            + ", ".join(sorted(dt.name for dt in outside_types))
        )
    if unreferenced_types:
        closure_lines.append(
            f"{len(unreferenced_types)} data type(s) excluded (inside the selection, but "
            "nothing the selection kept references them): " + ", ".join(sorted(dt.name for dt in unreferenced_types))
        )
    if dropped_elements:
        closure_lines.append(
            f"{len(dropped_elements)} unit, compu method or data constraint element(s) excluded (no data type "
            "the selection kept names them): " + ", ".join(sorted(element.name for element in dropped_elements))
        )
    if dropped_connectors:
        closure_lines.append(
            f"{len(dropped_connectors)} connector(s) excluded (an endpoint fell "
            "outside the selection): " + ", ".join(sorted(c.name for c in dropped_connectors))
        )
    if dropped_members:
        closure_lines.append(
            f"{len(dropped_members)} composition member(s) excluded (the type fell outside "
            "the selection): " + ", ".join(sorted(dropped_members))
        )
    if dropped_mapping_sets:
        closure_lines.append(
            f"{len(dropped_mapping_sets)} mapping set(s) excluded (outside the selection, and no "
            "component it kept names them): " + ", ".join(sorted(s.name for s in dropped_mapping_sets))
        )
    if dropped_maps:
        closure_lines.append(f"{dropped_maps} data type map(s) excluded (one side fell outside the selection)")
    return closure_lines


def _reselection_was_written(
    group: MergeGroup,
    *,
    registry: PathRegistry,
    kept_type_keys: set[tuple[str, str]],
    interface_name_by_path: dict[str, str],
    kept_interface_names: set[str],
    kept_swc_paths: set[str],
    compositions_by_path: dict[str, CompositionInfo],
    kept_composition_names: set[str],
    connector_name_by_path: dict[str, str],
    kept_connector_names: set[str],
    mapped_sets: MappedSets,
    mapping_sets: list[MappingSet],
) -> bool:
    """Whether the declaration a re-admitted *group* kept lands in an element the selection writes.

    Asked per layer, through each layer's own path-to-name relation; a layer
    with none answers written.
    """
    layer = group.key.layer
    if layer in (LAYER_APPLICATION_TYPES, LAYER_IMPLEMENTATION_TYPES, LAYER_BASE_TYPES):
        resolved = registry.resolve(group.primary)
        return resolved is not None and (registry.layer_of(group.primary), resolved) in kept_type_keys
    if layer == LAYER_INTERFACES:
        return interface_name_by_path.get(group.primary) in kept_interface_names
    if layer == LAYER_SWC:
        return group.primary in kept_swc_paths
    if layer == LAYER_COMPOSITIONS:
        info = compositions_by_path.get(group.primary)
        return info is not None and info.name in kept_composition_names
    if layer == LAYER_CONNECTORS:
        return connector_name_by_path.get(group.primary) in kept_connector_names
    if layer == LAYER_MAPPING_SETS:
        return mapped_sets.name_by_path.get(group.primary) in {s.name for s in mapping_sets}
    return True


def _declared_outside(source_path: str | None, selection: set[str]) -> bool:
    """Whether a dropped declaration lies outside every selected package.

    *Outside the selection* is a statement about the package the declaration
    sits in; an element inside a selected package that nothing the selection
    kept references is excluded for another reason. A path this import cannot
    place counts as outside.
    """
    return source_path is None or not in_selected_package(source_path, selection)


def _reconcile_and_place(
    source: str, decided: _Decided, results: _MapperResults, layers: _Layers, records: _Records
) -> tuple[_Layers, _Records, _Placed]:
    """Reconcile the names the writer reserves, skip the elements no file can hold, and place the rest.

    The reserved-name pass renames and rewrites every reference to what moved;
    a source `SHORT-NAME` that is no AUTOSAR `Identifier` refuses the import;
    an element named for a Windows device and the second of two declarations of
    one package differing only in case are skipped (`_drop_reserved`); the
    package placement is recorded and checked (`_record_placement`).

    Returns:
        The layers renamed, narrowed and placed, the records narrowed with them,
        and the reconciliation, the skipped elements and the placement.

    Raises:
        ValueError: a declared `SHORT-NAME` is not a legal AUTOSAR
            `Identifier`, or placement leaves names equal ignoring case in one
            package.
    """
    model = decided.model
    registry = results.registry
    mapped_sets = results.mapped_sets
    mapped = results.mapped
    data_types = layers.data_types
    elements = layers.elements
    interfaces = layers.interfaces
    imported_swcs = layers.swcs
    compositions = layers.compositions
    connectors = layers.connectors
    mapping_sets = layers.mapping_sets
    # Phase B of the name policy: resolve every collision with a name the ARXML writer reserves and rewrite every
    # reference to what moved. After `--packages`, so it never renames over a collision the selection dropped;
    # before the write loop, so a failure leaves no partial project. A pass here rather than in `_reserve()`: the
    # writer's reservations are kind-scoped, and keeping it out of the shared mapping layer keeps
    # `import component` rename-free.
    reconciled = reconcile_reserved_names(
        data_types=data_types,
        interfaces=interfaces,
        swcs=imported_swcs,
        connectors=connectors,
        compositions=compositions,
        registry=registry,
        mapping_sets=mapping_sets,
    )
    data_types = reconciled.data_types
    interfaces = reconciled.interfaces
    imported_swcs = reconciled.swcs
    connectors = reconciled.connectors
    compositions = reconciled.compositions
    mapping_sets = reconciled.mapping_sets

    # Every minting policy assumes a source SHORT-NAME is an IDENTIFIER--SIMPLE, which `arxml_reader` does not
    # enforce. A declared name breaking the pattern or the length is refused with nothing written: only names this
    # package mints are shortened (`fit_identifier`), and no repair exists for a declared one -- malformed input,
    # not lossiness.
    conformance = identifier_violations(reconciled, elements)
    if conformance:
        raise ValueError(
            f"Import aborted with nothing written: '{source}' declares a SHORT-NAME that is not a "
            "legal AUTOSAR Identifier, so the imported project could not be generated or validated "
            "-- " + "; ".join(conformance)
        )

    # After the reserved-name pass, which could rename an element onto a Windows device name. An element referring
    # to a skipped one keeps the name it spelled.
    reserved_skips = skip_reserved_device_names(model, reconciled, registry, mapped_sets, mapped, elements)
    # Package placement's one skip -- the second of two declarations of one source package differing only in case
    # -- joins the device-name skips, so `_drop_reserved` drops both alike.
    placement = place_final_packages(model, reconciled, registry, mapped_sets, mapped, reserved_skips, elements)
    reserved_skips = [*reserved_skips, *placement.skips]
    layers = replace(
        layers,
        data_types=data_types,
        interfaces=interfaces,
        swcs=imported_swcs,
        compositions=compositions,
        connectors=connectors,
        mapping_sets=mapping_sets,
    )
    if reserved_skips:
        layers, records = _drop_reserved(layers, records, reserved_skips, reconciled)
    layers = _record_placement(source, layers, placement)
    return layers, records, _Placed(reconciled, reserved_skips, placement)


def _drop_reserved(
    layers: _Layers, records: _Records, reserved_skips: list[ReservedSkip], reconciled: ReconciledModel
) -> tuple[_Layers, _Records]:
    """The layers and the records without the elements *reserved_skips* names, and without the stubs they orphan.

    Returns:
        The narrowed layers and the narrowed records.
    """
    data_types = layers.data_types
    elements = layers.elements
    mapping_sets = layers.mapping_sets
    interfaces = layers.interfaces
    imported_swcs = layers.swcs
    connectors = layers.connectors
    compositions = layers.compositions
    init_radix = records.init_radix
    unresolved_records = records.unresolved_records
    skipped = {(entry.layer, entry.name) for entry in reserved_skips}
    data_types = [dt for dt in data_types if data_type_key(dt) not in skipped]
    elements = [element for element in elements if data_type_key(element) not in skipped]
    mapping_sets = [s for s in mapping_sets if (LAYER_MAPPING_SETS, s.name) not in skipped]
    interfaces = [i for i in interfaces if (LAYER_INTERFACES, i.name) not in skipped]
    imported_swcs = [imported for imported in imported_swcs if (LAYER_SWC, imported.swc.name) not in skipped]
    connectors = [c for c in connectors if (LAYER_CONNECTORS, c.name) not in skipped]
    compositions = [c for c in compositions if (LAYER_COMPOSITIONS, c.name) not in skipped]
    # Narrowed as `_narrow_records` narrows them: a report line must not name a file this import does not write.
    # A radix entry carries the name the export spelled, which the reserved-name pass may have renamed.
    kept_source_names = {
        reconciled.interface_source_names.get(interface.name, interface.name) for interface in interfaces
    }
    init_radix = [entry for entry in init_radix if entry.interface in kept_source_names]
    dropped_paths = frozenset(entry.skip.path for entry in reserved_skips if entry.skip.path)
    unresolved_records = without_owners(unresolved_records, dropped_paths)
    dropped_owner = owned_by(dropped_paths)
    layers = _Layers(
        data_types=data_types,
        elements=elements,
        interfaces=interfaces,
        swcs=imported_swcs,
        compositions=compositions,
        connectors=connectors,
        mapping_sets=mapping_sets,
    )
    records = replace(records, init_radix=init_radix, unresolved_records=unresolved_records)
    return _drop_orphaned_stubs(layers, records, dropped_owner)


def _drop_orphaned_stubs(
    layers: _Layers, records: _Records, dropped_owner: Callable[[str], bool]
) -> tuple[_Layers, _Records]:
    """Narrow the stub and disagreement records to the referrers that survive, and drop every stub left with none.

    Returns:
        The layers without the orphaned stubs, and the narrowed records.
    """
    interfaces = layers.interfaces
    data_types = layers.data_types
    stub_records = records.stub_records
    disagreement_records = records.disagreement_records

    def survives(referrer_path: str) -> bool:
        """Whether a referrer was made by an element the reserved-name skip kept."""
        return not dropped_owner(referrer_path)

    stub_records = stubs_narrowed(stub_records, survives)
    disagreement_records = disagreements_narrowed(disagreement_records, survives)
    # A stub exists because a kept element names it, so one whose every referrer the skip dropped is not written.
    orphaned = {(stub.layer, stub.name) for stub in stub_records if not stub.referrers}
    if orphaned:
        interfaces = [i for i in interfaces if (LAYER_INTERFACES, i.name) not in orphaned]
        data_types = [dt for dt in data_types if data_type_key(dt) not in orphaned]
        stub_records = [stub for stub in stub_records if (stub.layer, stub.name) not in orphaned]
    layers = replace(layers, interfaces=interfaces, data_types=data_types)
    records = replace(records, stub_records=stub_records, disagreement_records=disagreement_records)
    return layers, records


def _record_placement(source: str, layers: _Layers, placement: ImportPlacement) -> _Layers:
    """Every element with its recorded package, after a fail-closed check of the package name spaces.

    Raises:
        ValueError: names equal ignoring case remain in one package.
    """
    data_types = layers.data_types
    elements = layers.elements
    mapping_sets = layers.mapping_sets
    interfaces = layers.interfaces
    imported_swcs = layers.swcs
    compositions = layers.compositions
    # The placement recorded, then checked fail-closed before anything is written.
    data_types = [with_package(dt, placement.packages.get(data_type_key(dt))) for dt in data_types]
    elements = [with_package(element, placement.packages.get(data_type_key(element))) for element in elements]
    mapping_sets = [with_package(s, placement.packages.get((LAYER_MAPPING_SETS, s.name))) for s in mapping_sets]
    interfaces = [with_package(i, placement.packages.get((LAYER_INTERFACES, i.name))) for i in interfaces]
    imported_swcs = [
        replace(imported, swc=with_package(imported.swc, placement.packages.get((LAYER_SWC, imported.swc.name))))
        for imported in imported_swcs
    ]
    compositions = [with_package(c, placement.packages.get((LAYER_COMPOSITIONS, c.name))) for c in compositions]
    refuse_package_level_collisions(source, data_types, mapping_sets, interfaces, imported_swcs, compositions, elements)
    return replace(
        layers,
        data_types=data_types,
        elements=elements,
        mapping_sets=mapping_sets,
        interfaces=interfaces,
        swcs=imported_swcs,
        compositions=compositions,
    )


def _finish_stubs_and_names(
    decided: _Decided, results: _MapperResults, layers: _Layers, placed: _Placed
) -> tuple[_Layers, _Finished]:
    """Finish the external stubs' ports, name the stubs written, and settle the stated names, over the final sets.

    A stub port is kept only while a kept connector or delegation still
    reaches it, and given its one-hop interface hint off the final names; every
    behaviour, event and access point keeps the source's name where it differs
    from the derived one.

    Returns:
        The layers with the finished components, and the finished stub ports,
        the written names and the names left unstated.
    """
    model = decided.model
    unresolved = decided.unresolved
    reconciled = placed.reconciled
    data_types = layers.data_types
    interfaces = layers.interfaces
    imported_swcs = layers.swcs
    connectors = layers.connectors
    compositions = layers.compositions
    # Last: `--packages` and the reserved device-name skip can each drop a stub port's last connector, and the
    # hint is read off the final interface names.
    stub_ports = finish_stub_ports(
        imported_swcs, connectors, compositions, {interface.name for interface in interfaces}
    )
    imported_swcs = stub_ports.swcs

    # Off the final element sets, so no report line names something the project never receives.
    # `map_compositions`' keys are pre-rename names, so the composition names go back through the source names.
    written_stub_paths = {imported.path for imported in imported_swcs if imported.swc.external}
    written_interface_names = {interface.name for interface in interfaces}
    # The implementation layer's names: a type stub is an element of it.
    written_type_names = {dt.name for dt in data_types if data_type_key(dt)[0] == LAYER_IMPLEMENTATION_TYPES}
    # A chained skip line names a stub only when this import writes it.
    unresolved.name_written_stubs(interface_names=written_interface_names, type_names=written_type_names)
    written_composition_names = {
        reconciled.swc_source_names.get(composition.name, composition.name) for composition in compositions
    }

    # Settled over the final components: the reserved-name pass can rename a component and so its derived
    # behaviour name.
    imported_swcs, names_not_stated = settle_stated_names(model, imported_swcs)
    finished = _Finished(
        stub_ports=stub_ports,
        written_stub_paths=written_stub_paths,
        written_interface_names=written_interface_names,
        written_type_names=written_type_names,
        written_composition_names=written_composition_names,
        names_not_stated=names_not_stated,
    )
    return replace(layers, swcs=imported_swcs), finished


def _collect_skips(
    project: SwcraftProject,
    decided: _Decided,
    results: _MapperResults,
    layers: _Layers,
    placed: _Placed,
    finished: _Finished,
) -> list[SkipEntry]:
    """Every skip entry of the import, in report order.

    The losses of the names SWCraft generates itself and the asynchronous call
    findings the file states are taken here, over the final element sets.
    """
    model = decided.model
    merge_skips = decided.merge_skips
    registry = results.registry
    dt_skips = results.dt_skips
    mapping_set_skips = results.mapping_set_skips
    iface_skips = results.iface_skips
    swc_skips = results.swc_skips
    composition_skips = results.composition_skips
    reserved_skips = placed.reserved_skips
    data_types = layers.data_types
    imported_swcs = layers.swcs
    names_not_stated = finished.names_not_stated
    # Over the final element sets, after every rename.
    generated_skips = SkipLog()
    generated_name_losses(
        model, data_types, imported_swcs, registry.source_paths, generated_skips, not_stated=names_not_stated
    )
    # Every asynchronous call finding the file itself states, imported as stated, at the project's version.
    async_call_skips = async_call_finding_skips(model, imported_swcs, project.resolved_arxml_version)

    return [
        # The reader's lines about what this import carries are left out.
        *registry.uncarried(model.skip_log),
        *merge_skips,
        *dt_skips,
        *mapping_set_skips,
        *iface_skips,
        *swc_skips,
        *composition_skips,
        *(entry.skip for entry in reserved_skips),
        *deferred_tag_skips(model),
        *generated_skips,
        *async_call_skips,
    ]


def _build_report(
    source: str,
    packages: list[str] | None,
    dry_run: bool,
    project: SwcraftProject,
    decided: _Decided,
    results: _MapperResults,
    layers: _Layers,
    records: _Records,
    placed: _Placed,
    finished: _Finished,
    all_skip_entries: list[SkipEntry],
    type_source_paths: dict[tuple[str, str], str],
) -> ImportReport:
    """The import's report, every count and line taken over the final element sets."""
    model = decided.model
    merges = decided.merges
    registry = results.registry
    mapped = results.mapped
    placement = placed.placement
    written_interface_names = finished.written_interface_names
    written_type_names = finished.written_type_names
    # Per tag, in the order each tag first occurs.
    skipped_by_tag = dict(Counter(entry.tag for entry in all_skip_entries))
    data_types = layers.data_types
    elements = layers.elements
    interfaces = layers.interfaces
    imported_swcs = layers.swcs
    compositions = layers.compositions
    connectors = layers.connectors
    mapping_sets = layers.mapping_sets
    init_radix = records.init_radix
    unresolved_records = records.unresolved_records
    stub_records = records.stub_records
    disagreement_records = records.disagreement_records
    closure_lines = records.closure_lines
    written_type_source_paths = {type_source_paths[data_type_key(dt)] for dt in data_types}
    written_element_source_paths = {type_source_paths[data_type_key(element)] for element in elements}
    stub_ports = finished.stub_ports
    written_stub_paths = finished.written_stub_paths

    # The keyword values are computed left to right, in keyword order.
    return ImportReport(
        source=source,
        packages=list(packages) if packages else None,
        dry_run=dry_run,
        imported_data_types=len(data_types),
        imported_interfaces=len(interfaces),
        imported_swcs=len(imported_swcs),
        imported_connectors=len(connectors),
        imported_compositions=len(compositions),
        imported_mapping_sets=len(mapping_sets),
        imported_units=sum(isinstance(element, Unit) for element in elements),
        imported_compu_methods=sum(isinstance(element, (TextTable, Linear)) for element in elements),
        imported_data_constraints=sum(isinstance(element, DataConstr) for element in elements),
        imported_variables=variable_counts([imported.swc for imported in imported_swcs]),
        imported_exclusive_areas=exclusive_area_counts([imported.swc for imported in imported_swcs]),
        imported_data_events=data_event_counts([imported.swc for imported in imported_swcs]),
        skipped_by_tag=skipped_by_tag,
        skip_entries=all_skip_entries,
        # The release every cited defect in the skipped list names: the one
        # the source itself declares.
        declared_schema=model.declared_schema,
        # The manifest is never changed; a source version differing from it is one line of the report.
        project_arxml_version=project.resolved_arxml_version,
        # Narrowed to the final element sets: a line here is about an element this import writes, and a line about
        # an element in no written file would be a claim with nothing behind it.
        reduction_lines=registry.carried_reductions({data_type_key(dt) for dt in data_types}),
        rename_lines=_carried_rename_lines(registry, layers),
        init_radix_lines=_init_radix_lines(init_radix, imported_swcs),
        closure_lines=closure_lines,
        # Not narrowed to the selection, unlike `unresolved_records`: a group's verdict is a finding about the
        # export, and it tells a user that the name they narrowed away is declared twice. What this does not
        # reach: a narrowed import still prints `MERGED n` / `CONFLICTING n` for groups it kept no member of.
        merged_groups=list(merges.merged),
        conflicting_groups=list(merges.conflicting),
        unresolved_references=unresolved_records,
        # Narrowed to the stubs written; a stub is never renamed after it is minted, so its minted name is the
        # written one.
        external_interface_stubs=_written_interface_stubs(stub_records, written_interface_names),
        external_type_stubs=_written_type_stubs(stub_records, written_type_names),
        dest_disagreements=disagreement_records,
        access_collapse_lines=access_collapse_lines(imported_swcs),
        # Off the final sets, after every skip.
        package_placement_lines=list(placement.lines),
        # Off the final component set; an external stub has no provide port.
        queue_length_lines=[line for imported in imported_swcs for line in imported.queue_length_losses],
        queue_length_keys=[key for imported in imported_swcs for key in imported.queue_length_keys],
        # Off the final sets and types, so a line names the map as the project carries it.
        incompatible_map_lines=incompatible_map_lines(mapping_sets, data_types),
        # Off the FINAL data type set, in source-path order -- the order the
        # layer is written in -- so no line names a base type the project never
        # receives. `.strip()`, as the rule itself reads it.
        native_declaration_missing_lines=[
            f"{dt.name} ({type_source_paths[data_type_key(dt)]})"
            for dt in sorted(data_types, key=lambda element: type_source_paths[data_type_key(element)])
            if isinstance(dt, BaseDataType) and not dt.native_declaration.strip()
        ],
        # Narrowed to the written types and data constraints, keyed on the source path, which phase B never renames.
        unread_physics_lines=[
            entry.line for entry in registry.unread_physics if entry.source_path in written_type_source_paths
        ],
        represented_lines=[
            *(
                entry.line
                for entry in registry.represented
                if entry.source_path in written_type_source_paths or entry.source_path in written_element_source_paths
            ),
            # A budget carried from another CSE code, off the final component set.
            *(entry.line for imported in imported_swcs for entry in imported.budget_normalizations),
        ],
        external_stub_lines=_written_stub_lines(mapped, written_stub_paths),
        # Off the finished stubs, so a port no kept connector reaches is in neither line.
        external_stub_port_lines=stub_ports.port_lines,
        stub_port_ambiguity_lines=stub_ports.ambiguous_lines,
        # Narrowed to the stubs written, as `external_stub_lines` is.
        stub_port_contradiction_lines=_written_stub_contradiction_lines(mapped, written_stub_paths),
        unowned_stub_port_paths=_written_unowned_port_paths(mapped, finished.written_composition_names),
    )


def _carried_rename_lines(registry: PathRegistry, layers: _Layers) -> list[str]:
    """The report's rename lines, about the elements the project is written with."""
    data_types = layers.data_types
    imported_swcs = layers.swcs
    compositions = layers.compositions
    return registry.carried_renames(
        {
            **{
                layer: {dt.name for dt in data_types if data_type_key(dt)[0] == layer}
                for layer in (LAYER_APPLICATION_TYPES, LAYER_IMPLEMENTATION_TYPES, LAYER_BASE_TYPES)
            },
            LAYER_INTERFACES: {i.name for i in layers.interfaces},
            # The compositions share the component types' namespace, so a renamed one is recorded under `swc`.
            LAYER_SWC: {imported.swc.name for imported in imported_swcs} | {c.name for c in compositions},
            LAYER_MAPPING_SETS: {s.name for s in layers.mapping_sets},
        }
    )


def _init_radix_lines(init_radix: list[InitRadixNormalization], imported_swcs: list[ImportedSwc]) -> list[str]:
    """The report's radix lines, about the elements the project is written with."""
    # A port's own value read from a hexadecimal spelling, off the final component set.
    return [
        *(entry.line for entry in init_radix),
        *(line for imported in imported_swcs for _port, _element, line in imported.port_init_radix),
        *(line for imported in imported_swcs for _variable, line in imported.variable_init_radix),
    ]


def _written_interface_stubs(stub_records: list[ExternalStub], written_interface_names: set[str]) -> list[ExternalStub]:
    """The interface stub records of the stubs the project is written with."""
    return [stub for stub in stub_records if stub.layer == LAYER_INTERFACES and stub.name in written_interface_names]


def _written_type_stubs(stub_records: list[ExternalStub], written_type_names: set[str]) -> list[ExternalStub]:
    """The type stub records of the stubs the project is written with."""
    return [
        stub for stub in stub_records if stub.layer == LAYER_IMPLEMENTATION_TYPES and stub.name in written_type_names
    ]


def _written_stub_lines(mapped: MappedCompositions, written_stub_paths: set[str]) -> list[str]:
    """The report's lines about the external component stubs written."""
    return [line for type_ref, line in mapped.stub_lines.items() if type_ref in written_stub_paths]


def _written_stub_contradiction_lines(mapped: MappedCompositions, written_stub_paths: set[str]) -> list[str]:
    """The report's port-contradiction lines about the external component stubs written."""
    return [
        line
        for type_ref, lines in mapped.stub_port_contradictions.items()
        if type_ref in written_stub_paths
        for line in lines
    ]


def _written_unowned_port_paths(mapped: MappedCompositions, written_composition_names: set[str]) -> list[str]:
    """The paths of the stub ports skipped inside the compositions written."""
    # A count inside a composition the project never receives would be a claim with nothing behind it. The skip
    # line itself stands: the connector really was skipped.
    return [
        path
        for composition_name, paths in mapped.unowned_port_paths.items()
        if composition_name in written_composition_names
        for path in paths
    ]


def _plan_batches(
    results: _MapperResults, layers: _Layers, placed: _Placed, type_source_paths: dict[tuple[str, str], str]
) -> tuple[tuple[str, list[_ImportedFile]], ...]:
    """The files the import writes, layer by layer, each layer in its elements' source-path order."""
    registry = results.registry
    mapped_sets = results.mapped_sets
    mapped = results.mapped
    reconciled = placed.reconciled
    data_types = layers.data_types
    elements = layers.elements
    mapping_sets = layers.mapping_sets
    interfaces = layers.interfaces
    imported_swcs = layers.swcs
    connectors = layers.connectors
    compositions = layers.compositions
    # Each layer is written in its elements' full AUTOSAR source-path order: the first writer of a name another
    # matches case-insensitively gets the plain file and the next `~1`, so which is first must be a property of the
    # export, identical on every platform. The names go through the reconciliation's `*_source_names` maps because
    # that pass renames after the mappers recorded their paths; no pass renames a mapping set, which lies in its
    # own package.
    set_paths = {mapping_set.name: mapped_sets.source_paths[mapping_set.name] for mapping_set in mapping_sets}
    interface_paths = {
        name: registry.interface_source_paths[reconciled.interface_source_names.get(name, name)]
        for name in (iface.name for iface in interfaces)
    }
    composition_paths = {
        composition.name: mapped.composition_source_paths[
            reconciled.swc_source_names.get(composition.name, composition.name)
        ]
        for composition in compositions
    }
    # Each data type is written into its own layer's directory, all three layers in one source-path order.
    batches: tuple[tuple[str, list[_ImportedFile]], ...] = (
        (
            "implementation_types",
            [
                _ImportedFile(
                    dt.name, type_source_paths[data_type_key(dt)], data_type_to_dict(dt), layer=data_type_key(dt)[0]
                )
                for dt in sorted(data_types, key=lambda element: type_source_paths[data_type_key(element)])
            ],
        ),
        (
            # The units, compu methods and data constraints, each into its own layer's directory.
            "units",
            [
                _ImportedFile(
                    element.name,
                    type_source_paths[data_type_key(element)],
                    element_to_dict(element),
                    layer=data_type_key(element)[0],
                )
                for element in sorted(elements, key=lambda element: type_source_paths[data_type_key(element)])
            ],
        ),
        (
            "mapping_sets",
            [
                _ImportedFile(
                    mapping_set.name,
                    set_paths[mapping_set.name],
                    element_to_dict(mapping_set),
                )
                for mapping_set in sorted(mapping_sets, key=lambda element: set_paths[element.name])
            ],
        ),
        (
            "interfaces",
            [
                _ImportedFile(iface.name, interface_paths[iface.name], interface_to_dict(iface))
                for iface in sorted(interfaces, key=lambda element: interface_paths[element.name])
            ],
        ),
        (
            "swc",
            [
                _ImportedFile(imported.swc.name, imported.path, swc_to_dict(imported.swc), {"group": imported.group})
                for imported in sorted(imported_swcs, key=lambda element: element.path)
            ],
        ),
        (
            "connectors",
            [
                _ImportedFile(
                    connector.name, mapped.connector_source_paths[connector.name], connector_to_dict(connector)
                )
                for connector in sorted(connectors, key=lambda element: mapped.connector_source_paths[element.name])
            ],
        ),
        (
            "compositions",
            [
                _ImportedFile(composition.name, composition_paths[composition.name], element_to_dict(composition))
                for composition in sorted(compositions, key=lambda element: composition_paths[element.name])
            ],
        ),
    )
    return batches


def _write_batches(
    root: Path,
    kernel: ModelRegistry,
    batches: tuple[tuple[str, list[_ImportedFile]], ...],
    layers: _Layers,
    dry_run: bool,
    report: ImportReport,
) -> None:
    """Allocate every file of *batches* in order and, unless *dry_run*, write it; record both on *report*.

    Every element asks for its file in write order, with the paths already decided reserved, whether or not
    anything is written, so a preview lists exactly the file-suffix lines the import reports. Before the
    compositions are written, a scaffolded empty root composition is removed when the import writes a root.
    """
    compositions = layers.compositions
    suffix_lines: list[str] = []

    written: list[str] = []
    # One allocation walk; `dry_run` suppresses the write and nothing else, so a preview lists exactly the suffix
    # lines the import reports. What this does not reach: nothing is removed on a dry run, so an imported
    # composition whose name differs from the scaffolded root's only in case is previewed with a suffix the write
    # does not take.
    planned: list[str] = []
    for layer, files in batches:
        if not dry_run and layer == "compositions" and any(composition.root for composition in compositions):
            # Leaving the scaffolded root beside the imported one would be refused by
            # `rule:composition.multiple-roots`; only an empty `root: true` composition is removed.
            report.replaced_scaffold_lines = remove_empty_root_compositions(
                root, {composition.name for composition in compositions}
            )
        # Listed after the removal above, so a removed scaffolded root is no file this loop could be pointed at.
        stored = {
            item_layer: stored_element_files(root, item_layer, registry=kernel)
            for item_layer in dict.fromkeys(item.layer or layer for item in files)
        }
        for item in files:
            target = imported_file_path(
                root,
                item.layer or layer,
                item.identity,
                suffix_lines,
                source=item.source,
                stored=stored[item.layer or layer],
                placement=item.placement,
                reserved=planned,
            )
            planned.append(target.relative_to(root).as_posix())
            if not dry_run:
                write_yaml(target.parent, target.name, item.data, written)

    report.file_suffix_lines = suffix_lines
    if not dry_run:
        report.written_paths = written
