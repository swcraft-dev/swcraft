"""``ImportReport`` and the import's lossiness and skip rendering.

Covers the deferred-tag skip sweep (``deferred_tag_skips``); the ``--packages`` selection and its
dependency closure (``in_selected_package``, ``selection_reach``, ``select_swc_names``,
``closure_data_type_names``), which keeps every element a selected one depends on; the YAML dump shape of
an interface (``interface_to_dict``); ``ImportReport`` itself and its one text renderer; the file
allocation both importers share; and the report lines for generated names the writer does not write back.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from swcraft_tools.arxml_import.connectors import UNOWNED_TARGET_PORT
from swcraft_tools.arxml_import.merge import (
    ELEMENT_NOUN_BY_LAYER,
    LAYER_APPLICATION_TYPES,
    LAYER_BASE_TYPES,
    LAYER_IMPLEMENTATION_TYPES,
    MergeDecisions,
    MergeGroup,
    reachable_mapping_targets,
)
from swcraft_tools.arxml_import.reconcile import PlacedMember, data_type_key
from swcraft_tools.arxml_import.swc_mapping import ImportedSwc, access_slots, emitted_access_names, result_slots
from swcraft_tools.arxml_import.unresolved import (
    CAUSE_CONFLICT,
    CAUSE_SELECTION,
    DestDisagreement,
    ExternalStub,
    UnresolvedReference,
    referrer_count,
)
from swcraft_tools.arxml_reader import (
    EXECUTION_TIME_CONSTRAINT,
    FALLBACK_JUDGING_RELEASE,
    REPORT,
    CompositionInfo,
    DataTypeMappingSetInfo,
    ImportModel,
    SkipCategory,
    SkipEntry,
    SkipLog,
    StandardRule,
    SwcInfo,
    cite,
    duplicate_name_reason,
)
from swcraft_tools.arxml_version import ArxmlVersion, release_of_schema_filename
from swcraft_tools.model_registry import compose_storage_path, current_registry, reserved_device_name
from swcraft_tools.model_state import FreePath, LayerFile, free_storage_path
from swcraft_tools.schema import (
    ApplicationDataType,
    CSArgument,
    CSInterface,
    CSOperation,
    DataDescriptionElement,
    DataElement,
    DataEvent,
    DataType,
    EnumDataType,
    ExternalInterface,
    Interface,
    MappingSet,
    SRInterface,
    SWComponent,
    ValueDataType,
    Variable,
)
from swcraft_tools.validator import (
    data_description_references,
    data_type_references,
    emitted_behavior_name,
    emitted_event_names,
    emitted_result_point_names,
)
from swcraft_tools.writer.arxml_generation import (
    BUDGET_SUFFIX,
    COMPU_METHOD_SUFFIX,
    COMPU_SUFFIX,
    RANGE_SUFFIX,
    SERVER_QUEUE_LENGTH,
    TIMING_SUFFIX,
    UNIT_SUFFIX,
    generated_data_type_forms,
)

# Known tags the reader indexes but no mapper turns into a SWCraft element. They are reported here, at
# the final aggregation point, rather than teaching every mapper about tags it never produces output for.
DEFERRED_ELEMENT_TAGS = frozenset({"SWC-IMPLEMENTATION", "CONSTANT-SPECIFICATION", "SW-ADDR-METHOD", "SYSTEM"})


def deferred_tag_skips(model: ImportModel) -> list[SkipEntry]:
    """One `SkipEntry` per `DEFERRED_ELEMENT_TAGS` element present in `model.path_index`.

    Does not cover `ADMIN-DATA`/`SDG` vendor metadata: it has no SHORT-NAME, so the path index never holds it.
    """
    return [
        SkipEntry(
            path=raw.path,
            tag=raw.tag,
            reason=_deferred_tag_reason(raw.tag),
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        for raw in model.path_index.values()
        if raw.tag in DEFERRED_ELEMENT_TAGS
    ]


def _deferred_tag_reason(tag: str) -> str:
    """The skip reason for one deferred tag.

    A CONSTANT-SPECIFICATION's value is imported onto the ``init`` of a data element that references it, so
    its sentence says so.
    """
    if tag == "CONSTANT-SPECIFICATION":
        return (
            "CONSTANT-SPECIFICATION is not an SWCraft element; where a data element's "
            "INIT-VALUE references it, its value is imported onto that element's `init`"
        )
    return f"{tag} has no SWCraft representation"


#: The import's one sentence about traceability: no requirement was written, and how to link what was --
#: never a warning, since a component without a link is no finding. Rendered only when a component was
#: imported.
NO_REQUIREMENT_SENTENCE = (
    "No requirement was written: link each imported component to the requirements it realizes with `satisfies`."
)


def in_selected_package(path: str, selection: set[str]) -> bool:
    """True if `path` (e.g. "/Acme/Chassis/Components/SensorSwc") lies inside a package `selection` matches.

    The importer's one ``--packages`` predicate: a bare package short name at any depth or a full package
    path, sticky downward through descendants. Only ancestor packages are checked, never the element's own
    name. The reader always reads the whole file, so the dependency closure can see every reference.
    """
    segments = path.strip("/").split("/")
    prefix = ""
    for segment in segments[:-1]:
        prefix = f"{prefix}/{segment}"
        if segment in selection or prefix in selection:
            return True
    return False


def _mapping_set_targets(mapping_set: DataTypeMappingSetInfo) -> tuple[str, ...]:
    """What a ``DATA-TYPE-MAPPING-SET`` declaration reaches: **nothing**.

    A map whose target the import does not carry is dropped with a line and the set is written without it,
    and following the maps would re-admit every type one global set names from a single selected component.
    A function rather than a literal, so the answer can be replaced in a test.
    """
    del mapping_set  # the answer is the kind's, not this declaration's
    return ()


@dataclass
class _SelectionWalk:
    """What :func:`selection_reach` collects.

    Each declaration's references, the paths inside the selected packages and the roots of the walk.
    """

    selection: set[str]
    references: dict[str, list[str]] = field(default_factory=dict)
    inside: set[str] = field(default_factory=set)
    roots: list[str] = field(default_factory=list)


def _declare(walk: _SelectionWalk, path: str, targets: Iterable[str | None], *, root_kind: bool = False) -> None:
    """Record *path* with its non-empty *targets*.

    A path inside a selected package is also ``inside``, and a root kind there is a root.
    """
    walk.references[path] = [target for target in targets if target]
    if in_selected_package(path, walk.selection):
        walk.inside.add(path)
        if root_kind:
            walk.roots.append(path)


def _declare_implementation_types(model: ImportModel, walk: _SelectionWalk) -> None:
    """Declare the base types and the implementation types with the references each rests on."""
    for base in model.sw_base_types:
        _declare(walk, base.path, ())
    for impl in model.impl_data_types:
        # A STRUCTURE's and an ARRAY's sub-element references and a DATA_REFERENCE's target are types the
        # written composite names.
        _declare(
            walk,
            impl.path,
            (
                impl.base_type_ref,
                impl.impl_type_ref,
                *(element.impl_type_ref for element in impl.sub_elements),
                *(element.base_type_ref for element in impl.sub_elements),
                impl.pointer_target_ref,
                impl.pointer_target_base_type_ref,
            ),
        )


def _declare_application_types(
    model: ImportModel, walk: _SelectionWalk, reachable_target_by_app_path: Mapping[str, tuple[str, ...]]
) -> None:
    """Declare the application primitives, records and arrays with the implementation types each reaches."""
    for primitive in model.app_primitive_data_types:
        # Every implementation type the primitive can make the project need
        # (`merge.reachable_mapping_targets`), which is wider than what the duplicate-name verdict compares:
        # the walk asks what must stay selected for the primitive to map at all, and the twin collapse's
        # registration is decided only after the selection is narrowed. Where both candidates are members
        # of one group it names the member the layer comes to rest on (the merged group's primary, the
        # member the packages keep, else the one the declarations say registers), because declaring a
        # member of a conflicting group excludes the others.
        _declare(walk, primitive.path, reachable_target_by_app_path.get(primitive.path, ()))
    # An application record or array reaches every implementation composite a map pairs it with: the map
    # is written, so the composite has to survive the selection.
    composite_stores: dict[str, list[str]] = {}
    for mapping_set in model.data_type_mapping_sets:
        for entry in mapping_set.maps:
            composite_stores.setdefault(entry.application_ref, []).append(entry.implementation_ref)
    for record in model.app_record_data_types:
        _declare(
            walk,
            record.path,
            (*(element.type_ref for element in record.elements), *composite_stores.get(record.path, ())),
        )
    for array in model.app_array_data_types:
        _declare(walk, array.path, (array.element_type_ref, *composite_stores.get(array.path, ())))


def _declare_interfaces(model: ImportModel, walk: _SelectionWalk) -> None:
    """Declare the sender-receiver and client-server interfaces with their element and argument types."""
    for sr in model.sr_interfaces:
        _declare(walk, sr.path, (element.type_ref for element in sr.data_elements))
    for cs in model.cs_interfaces:
        _declare(
            walk,
            cs.path,
            (argument.type_ref for operation in cs.operations for argument in operation.arguments),
        )


def _declare_components(model: ImportModel, walk: _SelectionWalk) -> None:
    """Declare the component types, mapping sets, compositions and connectors.

    The component types and the compositions are the roots of the walk.
    """
    for swc in model.swcs:
        _declare(
            walk,
            swc.path,
            (
                *(port.interface_ref for port in swc.ports),
                *swc.data_type_mapping_refs,
                *(variable.type_ref for variable in swc.variables),
            ),
            root_kind=True,
        )
    for mapping_set in model.data_type_mapping_sets:
        _declare(walk, mapping_set.path, _mapping_set_targets(mapping_set))
    for composition in model.compositions:
        _declare(
            walk,
            composition.path,
            (
                *(port.interface_ref for port in composition.ports),
                *(prototype.type_ref for prototype in composition.prototypes),
                # Containment, not a reference: a connector is reached when its composition is.
                *(connector.path for connector in composition.assembly_connectors),
            ),
            root_kind=True,
        )
        for connector in composition.assembly_connectors:
            # Declared so a conflicting connector group can be narrowed; no target: its endpoints reach
            # sideways into the enclosing composition.
            _declare(walk, connector.path, ())


def _reachable_targets_by_application_path(
    model: ImportModel, selection: set[str], decisions: MergeDecisions
) -> Mapping[str, tuple[str, ...]]:
    """The implementation types each application primitive can make the project need."""
    # The one edge that reads the verdict and the selection: a merged group's kept member is its primary,
    # and a conflicting one's is decided by the packages before the walk. This is `inside`'s
    # implementation-type half, computed one loop early.
    package_selected_impl_types = frozenset(
        impl.path for impl in model.impl_data_types if in_selected_package(impl.path, selection)
    )
    reachable_target_by_app_path = reachable_mapping_targets(
        model, decisions=decisions, package_selected=package_selected_impl_types
    )
    return reachable_target_by_app_path


def _reached_paths(walk: _SelectionWalk) -> frozenset[str]:
    """Every path reachable from the roots of *walk* along its references."""
    reached: set[str] = set()
    pending = [*walk.roots]
    while pending:
        path = pending.pop()
        if path in reached:
            continue
        reached.add(path)
        pending.extend(walk.references.get(path, ()))
    return frozenset(reached)


def selection_reach(
    model: ImportModel, selection: set[str], decisions: MergeDecisions
) -> tuple[frozenset[str], frozenset[str]]:
    """What a ``--packages`` `selection` keeps, as **declaration paths**: ``(inside, reached)``.

    The two answers :func:`~swcraft_tools.arxml_import.merge.narrow_to_selection` needs to narrow a
    conflicting duplicate-name group: the declarations inside a selected package, over every kind that takes
    a verdict, and the declarations the selection **reaches** from the component types and compositions
    inside one. The reference graph mirrors the edges the verdict compares
    (:func:`~swcraft_tools.arxml_import.merge_verdict._signature`), because a group is narrowed exactly when
    the selection reaches one of its members; a connector is reached by containment, when its composition
    is. Two edges are wider than the comparison: an application primitive's map targets (see the call site)
    and the implementation composites a map pairs a record or array with, since a written map's store must
    survive the selection.

    Not followed, each by decision: a ``COMPU-METHOD``, ``UNIT`` or ``DATA-CONSTR`` reference (the kept
    types pull these in afterwards, ``closure_element_keys``); a ``CONSTANT-SPECIFICATION`` reference (the
    verdict inlines its value); a mapping set's maps (see :func:`_mapping_set_targets`); a runnable access,
    a local variable access, an event's runnable and a COM-SPEC's references (each inside what the component
    already reaches); a delegation connector; and a connector's or delegation's endpoint, which reaches
    sideways into the enclosing composition. Nor does a connector's admission decide whether it is written:
    :func:`~swcraft_tools.arxml_import.pipeline._narrow_closure` still drops one whose endpoint type fell
    outside the selection, with a line.
    """
    walk = _SelectionWalk(selection)
    reachable_target_by_app_path = _reachable_targets_by_application_path(model, selection, decisions)
    _declare_implementation_types(model, walk)
    _declare_application_types(model, walk, reachable_target_by_app_path)
    _declare_interfaces(model, walk)
    _declare_components(model, walk)
    return frozenset(walk.inside), _reached_paths(walk)


def select_swc_names(
    model: ImportModel,
    imported_swcs: list[ImportedSwc],
    selection: set[str],
    raw_swc_by_path: dict[str, SwcInfo],
    compositions_by_path: dict[str, CompositionInfo],
    merges: MergeDecisions | None = None,
) -> tuple[set[str], set[str]]:
    """The imported SWC names and composition paths inside ``--packages`` `selection`'s dependency closure.

    A component type declared inside a selected package is in, and so is every leaf type a composition
    declared inside one instantiates, however deeply nested; the compositions the walk visits are the ones
    the import keeps. What this does not reach: an external stub is in no package, so `run_import` keeps
    exactly the stubs the surviving compositions still place.

    `merges`: a selected declaration is looked up by the path its element was mapped at, which for a member
    of a merged duplicate-name group is the group's primary. ``None`` is the identity.

    Returns:
        ``(selected SWC names, selected composition ARXML paths)``.
    """
    selected_type_paths: set[str] = {s.path for s in model.swcs if in_selected_package(s.path, selection)}
    selected_composition_paths: set[str] = set()

    def _collect_leaf_types(comp_path: str, seen: set[str]) -> None:
        if comp_path in seen:
            return
        seen.add(comp_path)
        comp = compositions_by_path.get(comp_path)
        if comp is None:
            return
        selected_composition_paths.add(comp_path)
        for proto in comp.prototypes:
            if proto.type_ref in raw_swc_by_path:
                selected_type_paths.add(proto.type_ref)
            elif proto.type_ref in compositions_by_path:
                _collect_leaf_types(proto.type_ref, seen)

    for comp in model.compositions:
        if in_selected_package(comp.path, selection):
            _collect_leaf_types(comp.path, set())

    imported_name_by_path = {imported.path: imported.swc.name for imported in imported_swcs}
    names = {
        imported_name_by_path[mapped]
        for path in selected_type_paths
        if (mapped := merges.primary_path(path) if merges is not None else path) in imported_name_by_path
    }
    return names, selected_composition_paths


def closure_data_type_names(
    interfaces: list[Interface],
    data_types: list[DataType],
    mapping_sets: Sequence[MappingSet] = (),
    *,
    swcs: Sequence[SWComponent] = (),
) -> set[tuple[str, str]]:
    """The ``(layer, name)`` keys of every data type the kept `interfaces` and `swcs` need, transitively.

    The roots are the types of the interfaces' data elements and arguments and of the components'
    inter-runnable variables; every type a reached type references follows, in the layer its field targets.
    A kept application type also pulls in the implementation type each mapping set pairs it with, or the
    written map would name a type the project lacks; a mapped implementation type does not pull its
    application side back in.
    """
    by_key = {data_type_key(dt): dt for dt in data_types}
    # Every set is read: the sets a narrowed import keeps are decided after this closure runs.
    stored_by: dict[str, list[str]] = {}
    for mapping_set in mapping_sets:
        for entry in mapping_set.maps:
            stored_by.setdefault(entry.application, []).append(entry.implementation)
    required: set[tuple[str, str]] = set()

    def _walk(key: tuple[str, str | None]) -> None:
        layer, name = key
        if name is None or (layer, name) in required:
            return
        dt = by_key.get((layer, name))
        if dt is None:
            return
        required.add((layer, name))
        target = (
            LAYER_BASE_TYPES
            if isinstance(dt, ValueDataType)
            else LAYER_APPLICATION_TYPES
            if layer == LAYER_APPLICATION_TYPES
            else LAYER_IMPLEMENTATION_TYPES
        )
        for referenced in data_type_references(dt):
            _walk((target, referenced))
        if layer == LAYER_APPLICATION_TYPES:
            for stored in stored_by.get(name, ()):
                _walk((LAYER_IMPLEMENTATION_TYPES, stored))

    for iface in interfaces:
        if isinstance(iface, SRInterface):
            for de in iface.data_elements:
                _walk(_prototype_key(de))
        elif isinstance(iface, CSInterface):
            for op in iface.operations:
                for arg in op.arguments:
                    _walk(_prototype_key(arg))
        # An external interface stub lists members by name only and declares no type.
    for swc in swcs:
        for variable in swc.variables:
            _walk(_prototype_key(variable))

    return required


def variable_counts(swcs: Iterable[SWComponent]) -> dict[str, tuple[int, int]]:
    """``name -> (variables, local accesses)`` of every component in *swcs* that declares any."""
    counts: dict[str, tuple[int, int]] = {}
    for swc in swcs:
        accesses = sum(len(runnable.reads_variables) + len(runnable.writes_variables) for runnable in swc.runnables)
        if swc.variables or accesses:
            counts[swc.name] = (len(swc.variables), accesses)
    return counts


def exclusive_area_counts(swcs: Iterable[SWComponent]) -> dict[str, tuple[int, int]]:
    """``name -> (areas, references)`` of every component in *swcs* that declares any.

    A reference is one ``can_enter`` or ``runs_inside`` entry, after the collapse of a repeated one.
    """
    counts: dict[str, tuple[int, int]] = {}
    for swc in swcs:
        references = sum(len(runnable.can_enter) + len(runnable.runs_inside) for runnable in swc.runnables)
        if swc.exclusive_areas or references:
            counts[swc.name] = (len(swc.exclusive_areas), references)
    return counts


def closure_element_keys(
    data_types: Sequence[DataType], elements: Sequence[DataDescriptionElement]
) -> set[tuple[str, str]]:
    """The units, compu methods and data constraints the kept *data_types* name, ``(layer, name)`` each.

    A step after the type closure and no part of ``selection_reach``, so a conflicting group of these stays
    conflicting under any selection, and an element no kept type names is excluded with the closure's line.
    """
    by_key = {data_type_key(element): element for element in elements}
    required: set[tuple[str, str]] = set()

    def need(key: tuple[str, str]) -> None:
        if key in required or key not in by_key:
            return
        required.add(key)
        for referenced in data_description_references(by_key[key]):
            need(referenced)

    for dt in data_types:
        for referenced in data_description_references(dt):
            need(referenced)
    return required


def _prototype_key(prototype: DataElement | CSArgument | Variable) -> tuple[str, str | None]:
    """The ``(layer, name)`` the one type field *prototype* sets names."""
    if prototype.application_type is not None:
        return (LAYER_APPLICATION_TYPES, prototype.application_type)
    return (LAYER_IMPLEMENTATION_TYPES, prototype.implementation_type)


def _data_element_to_dict(element: DataElement) -> dict[str, Any]:
    """One sender-receiver data element, in the writer's dump shape.

    Mirrors ``writer/yaml_documents.py``'s ``data_element_to_dict`` key for key, each optional field omitted
    when unset. Hand-written, so a field missing here is erased silently.
    """
    d: dict[str, Any] = {"name": element.name}
    if element.application_type is not None:
        d["application_type"] = element.application_type
    else:
        d["implementation_type"] = element.implementation_type
    if element.init is not None:
        d["init"] = element.init
    if element.asil is not None:
        d["asil"] = element.asil
    if element.description:
        d["description"] = element.description
    if element.sw_impl_policy is not None:
        d["sw_impl_policy"] = element.sw_impl_policy
    if element.sw_calibration_access is not None:
        d["sw_calibration_access"] = element.sw_calibration_access
    return d


def _cs_operation_to_dict(operation: CSOperation) -> dict[str, Any]:
    """One client-server operation with its arguments, in the writer's dump shape.

    Mirrors ``writer/yaml_documents.py``'s ``cs_operation_to_dict`` key for key.
    """
    arguments: list[dict[str, Any]] = []
    for arg in operation.arguments:
        a: dict[str, Any] = {"name": arg.name, "direction": arg.direction}
        if arg.application_type is not None:
            a["application_type"] = arg.application_type
        else:
            a["implementation_type"] = arg.implementation_type
        if arg.description:
            a["description"] = arg.description
        arguments.append(a)
    d: dict[str, Any] = {"name": operation.name, "arguments": arguments}
    if operation.description:
        d["description"] = operation.description
    return d


def interface_to_dict(iface: Interface) -> dict[str, Any]:
    """Convert a mapped `Interface` to a YAML-serialisable dict, in the writer's dump shape.

    The SR and CS arms mirror the writer's interface dumps key for key; the external stub arm follows the
    derived serializer's key order and omission rule (``writer.elements.element_to_dict``)."""
    if isinstance(iface, ExternalInterface):
        d: dict[str, Any] = {"name": iface.name, "kind": iface.kind}
        if iface.external_ref:
            d["external_ref"] = iface.external_ref
        if iface.external_dest:
            d["external_dest"] = iface.external_dest
        if iface.operations:
            d["operations"] = list(iface.operations)
        if iface.data_elements:
            d["data_elements"] = list(iface.data_elements)
        if iface.description:
            d["description"] = iface.description
        if iface.satisfies:
            d["satisfies"] = list(iface.satisfies)
        return d
    if isinstance(iface, SRInterface):
        d = {
            "name": iface.name,
            "kind": iface.kind,
            "data_elements": [_data_element_to_dict(el) for el in iface.data_elements],
        }
        if iface.description:
            d["description"] = iface.description
        if iface.package is not None:
            d["package"] = iface.package
        if iface.is_service:
            d["is_service"] = True
        if iface.satisfies:
            d["satisfies"] = list(iface.satisfies)
    else:
        d = {
            "name": iface.name,
            "kind": iface.kind,
            "operations": [_cs_operation_to_dict(op) for op in iface.operations],
        }
        if iface.description:
            d["description"] = iface.description
        if iface.package is not None:
            d["package"] = iface.package
        if iface.is_service:
            d["is_service"] = True
        if iface.satisfies:
            d["satisfies"] = list(iface.satisfies)
    return d


@dataclass
class ImportReport:
    """Totals, skip reasons and lossy-transformation lines for one `run_import()` call.

    :meth:`render` is the one text renderer.
    """

    source: str
    packages: list[str] | None
    dry_run: bool
    imported_data_types: int
    imported_interfaces: int
    imported_swcs: int
    imported_connectors: int
    #: The compositions this import wrote, counted in :attr:`total_imported` like every content layer.
    imported_compositions: int
    skipped_by_tag: dict[str, int]
    #: Every element the import skipped, in the order met: the reader's skip log, then each mapper's, then
    #: the deferred tags. :meth:`render` groups it by reason; :attr:`skip_lines` is the per-element form.
    skip_entries: list[SkipEntry]
    reduction_lines: list[str]
    #: One line per rename, from every policy that mints a name (the bare-name collision rename, the
    #: reserved-name reconciliation, `reconcile.py`, and the external stubs' `_ext<X>` names): one category,
    #: since to a reader each says "this element is not called what the export called it".
    rename_lines: list[str]
    #: One line per written data element whose `init` was read from a hexadecimal, octal or binary spelling
    #: and written in decimal: the value survives, the spelling does not.
    init_radix_lines: list[str]
    closure_lines: list[str]
    written_paths: list[str] = field(default_factory=list)
    #: One line per access point `map_swcs` collapsed into an earlier entry of the same runnable naming the
    #: same `(port, member)` pair under another SHORT-NAME. Not a skip entry: a collapse loses no model
    #: content, and a skip would abort `import component`.
    access_collapse_lines: list[str] = field(default_factory=list)
    #: The mapping sets this import wrote; rendered only when it counts something.
    imported_mapping_sets: int = 0
    #: The units, compu methods and data constraints this import wrote; each rendered only when it counts.
    imported_units: int = 0
    imported_compu_methods: int = 0
    imported_data_constraints: int = 0
    #: ``name -> (variables, local accesses)`` per written component that declares any.
    imported_variables: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: ``name -> (exclusive areas, references)`` per written component that declares any.
    imported_exclusive_areas: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: ``name -> data-received events`` per written component that declares any.
    imported_data_events: dict[str, int] = field(default_factory=dict)
    #: One line per external component stub this import created: a prototype whose type the export does not
    #: carry, written with the source's `TYPE-TREF` and `DEST` verbatim and only the ports its connectors
    #: reach. Not a skip entry: the prototype is imported; the type behind it is what is lost.
    external_stub_lines: list[str] = field(default_factory=list)
    #: One line per written external stub that declares a port: its name, reference and port counts.
    external_stub_port_lines: list[str] = field(default_factory=list)
    #: One line per stub port name the export states with contradicting directions: no port is minted and
    #: every connector reaching it is skipped. Narrowed to the stubs written.
    stub_port_contradiction_lines: list[str] = field(default_factory=list)
    #: One line per written stub port whose direct neighbours name different interfaces; no hint is written.
    stub_port_ambiguity_lines: list[str] = field(default_factory=list)
    #: The ARXML path of every assembly connector and delegation skipped because an end on an external stub
    #: names a target port path the context component's type does not own ([TPS_GST_00162], cited in the
    #: file's own release), narrowed to the compositions written. Paths, not a count: the round-trip gate
    #: asserts the set.
    unowned_stub_port_paths: list[str] = field(default_factory=list)
    #: One line per standing empty `root: true` composition (the shape `swcraft init` scaffolds) this import
    #: removed, or found with no file to remove, to put its own root in place. Filled by the write loop, so
    #: empty on a `dry_run`. A line names the file relative to the model root, never where the project is
    #: stored.
    replaced_scaffold_lines: list[str] = field(default_factory=list)
    #: One line per element written under a collision-suffixed file name (``float32`` beside ``Float32`` on
    #: a case-insensitive filesystem). A fact about the file only: the name, its references and every
    #: generated artifact are unchanged. Filled wherever a file name is decided, so ``--dry-run`` reports the
    #: same lines, unlike :attr:`replaced_scaffold_lines`.
    file_suffix_lines: list[str] = field(default_factory=list)
    #: One line per element package placement moved into a sub-package, because its home would otherwise
    #: hold a name differing from it only in case (``[constr_2508]``). Not a skip: nothing is lost.
    package_placement_lines: list[str] = field(default_factory=list)
    #: One line per written base type whose ``SW-BASE-TYPE`` carried no (or an empty) ``NATIVE-DECLARATION``,
    #: which the standard permits. Nothing is invented and nothing skipped; ``check-model`` reports each.
    native_declaration_missing_lines: list[str] = field(default_factory=list)
    #: One line per physical property a written ``APPLICATION-PRIMITIVE-DATA-TYPE`` declared and this import
    #: did not carry: a conversion the importer does not read, a compu method category ``conversion`` cannot
    #: hold, a ``UNIT`` or ``DATA-CONSTR`` the export references but does not declare. The type is imported.
    unread_physics_lines: list[str] = field(default_factory=list)
    #: One line per value the export spells in a form the model spells otherwise and the import carries (an
    #: infinite ``PHYS-CONSTRS`` limit carried as the absent bound). Narrowed to the written elements.
    represented_lines: list[str] = field(default_factory=list)
    #: One record per `(layer, kind, SHORT-NAME)` the export declared more than once and the duplicate-name
    #: verdict **merged** into one element carrying every member path. Empty for `import component`, which
    #: takes no verdict.
    merged_groups: list[MergeGroup] = field(default_factory=list)
    #: The same for every group decided **conflicting** -- neither declaration
    #: imported, the name still claimed, and every reference into the group left
    #: unresolved. Each record carries the first property on which the members
    #: differ, or the group whose conflict it inherits, or that it is cyclic.
    conflicting_groups: list[MergeGroup] = field(default_factory=list)
    #: One record per `(target path, cause)` whose referring element the import kept with the unresolved
    #: path in its field; ``check-model`` reports each. On a ``--packages`` import the records are narrowed
    #: to the kept referrers
    #: (:meth:`~swcraft_tools.arxml_import.unresolved.UnresolvedReferences.narrowed`). What this does **not**
    #: reach: a record is narrowed, never re-caused; ``CAUSE_SELECTION`` is minted where the reference is
    #: made, never here.
    unresolved_references: list[UnresolvedReference] = field(default_factory=list)
    #: One record per external interface stub this import wrote: a target the export declares nowhere, named
    #: by a port, carried with its path and ``DEST`` verbatim. Its references are no longer in
    #: :attr:`unresolved_references`. Narrowed to the stubs and referrers the project receives.
    external_interface_stubs: list[ExternalStub] = field(default_factory=list)
    #: The same for every external type stub -- a data element's or argument's
    #: type, or a reference type's base.
    external_type_stubs: list[ExternalStub] = field(default_factory=list)
    #: Every ``CAUSE_ABSENT`` path left without a stub because its occurrences disagree on ``DEST``; its
    #: occurrences stay in :attr:`unresolved_references`. Narrowed like them, still naming every ``DEST``
    #: the export wrote.
    dest_disagreements: list[DestDisagreement] = field(default_factory=list)
    #: One line per ``SERVER-COM-SPEC`` of a written component's provide port whose source ``QUEUE-LENGTH``
    #: is not 1: the export writes every one with 1, so the value is not carried. The port is imported.
    queue_length_lines: list[str] = field(default_factory=list)
    #: The structured twin of :attr:`queue_length_lines`, one ``({port}/PROVIDED-COM-SPECS, QUEUE-LENGTH)``
    #: key per line; never rendered.
    queue_length_keys: list[tuple[str, str]] = field(default_factory=list)
    #: One line per written ``DATA-TYPE-MAP`` SWCraft's supported subset refuses: a composite pair
    #: ``validator.composite_map_difference`` finds different, or a map reaching a pointer
    #: (``[constr_1066]``). The map is kept as declared; ``check-model`` reports it and ``generate arxml``
    #: refuses until it is repaired. Not a claim that the source breaks a constraint: the composite check is
    #: SWCraft's partial reading of ``[TPS_SWCT_01191]``.
    incompatible_map_lines: list[str] = field(default_factory=list)
    #: The schema file name the source's ``xsi:schemaLocation`` declares, as written, or ``None``. It decides
    #: the release every section-1 citation names (:attr:`declared_release`).
    declared_schema: str | None = None
    #: The target project's ``arxml_version``, or ``None`` when the report was built without a project.
    #: ``import model`` never changes the manifest; :attr:`version_line` names a difference.
    project_arxml_version: ArxmlVersion | None = None

    @property
    def declared_release(self) -> ArxmlVersion | None:
        """The release SWCraft writes that :attr:`declared_schema` names, else ``None``."""
        return release_of_schema_filename(self.declared_schema)

    @property
    def version_line(self) -> str | None:
        """The line naming the source's version when the project's differs, else ``None``.

        Path-free: the schema file name is the only file name it carries.
        """
        source = self.declared_release
        project = self.project_arxml_version
        if source is None or project is None or source == project:
            return None
        return (
            f"ARXML version: the file declares AUTOSAR {source} ({source.schema_filename}); this project's "
            f"arxml_version is {project}, and import model does not change it -- set arxml_version: {source} "
            f"in swcraft.yaml, or check-model applies {project}'s rules."
        )

    @property
    def total_imported(self) -> int:
        """Sum of imported elements across the content layers.

        Zero is the CLI's "nothing was imported" non-zero exit; a non-empty skip list alone is not a failure.
        """
        return (
            self.imported_data_types
            + self.imported_interfaces
            + self.imported_swcs
            + self.imported_connectors
            + self.imported_compositions
            + self.imported_mapping_sets
            + self.imported_units
            + self.imported_compu_methods
            + self.imported_data_constraints
        )

    @property
    def total_skipped(self) -> int:
        return sum(self.skipped_by_tag.values())

    @property
    def skip_lines(self) -> list[str]:
        """One line per skipped element, ``TAG PATH: reason``, in :attr:`skip_entries` order.

        Uncited: readers split each line at the first ``": "`` to recover the path.
        """
        return [f"{entry.tag} {entry.path}: {entry.reason}" for entry in self.skip_entries]

    def render(self, *, verbose: bool = False) -> str:
        """Return the report as text.

        The sections follow in a fixed order: the header and the counts, the
        summary lines, the stub sections, the value sections, the verdicts and
        the unresolved references, the skipped elements, and last the sentence
        that the import writes no requirement. A block that counts nothing
        prints nothing.

        Args:
            verbose: list every referring element of an unresolved reference and
                every skipped element under its line, instead of a cut list and
                the counts alone.
        """
        release = self.declared_release
        lines = self._render_header_and_counts()
        lines.extend(self._render_summary_lines())
        lines.extend(self._render_stub_sections(release))
        lines.extend(self._render_value_sections())
        lines.extend(self._render_verdicts_and_references(verbose))
        lines.extend(self._render_skip_entries(verbose, release))
        if self.imported_swcs:
            lines.append("")
            lines.append(NO_REQUIREMENT_SENTENCE)
        return "\n".join(lines)

    def _render_header_and_counts(self) -> list[str]:
        """The header and the IMPORTED counts, then the SKIPPED count by tag."""
        lines = [f"Import report: {self.source}"]
        if self.dry_run:
            lines.append("(DRY RUN -- no files were written)")
        if self.packages:
            lines.append(f"--packages selection: {', '.join(self.packages)}")
        if self.version_line is not None:
            lines.append(self.version_line)
        lines.append("")
        lines.append(f"IMPORTED {self.imported_data_types} data types")
        lines.append(f"IMPORTED {self.imported_interfaces} interfaces")
        lines.append(f"IMPORTED {self.imported_swcs} SWCs")
        lines.append(f"IMPORTED {self.imported_connectors} connectors")
        lines.append(f"IMPORTED {self.imported_compositions} compositions")
        if self.imported_mapping_sets:
            lines.append(f"IMPORTED {self.imported_mapping_sets} mapping sets")
        for count, noun in (
            (self.imported_units, "units"),
            (self.imported_compu_methods, "compu methods"),
            (self.imported_data_constraints, "data constraints"),
        ):
            if count:
                lines.append(f"IMPORTED {count} {noun}")
        if self.imported_variables:
            variables = sum(count for count, _accesses in self.imported_variables.values())
            accesses = sum(count for _variables, count in self.imported_variables.values())
            lines.append(f"IMPORTED {variables} inter-runnable variables and {accesses} local variable accesses")
            for name in sorted(self.imported_variables):
                count, access_count = self.imported_variables[name]
                lines.append(f"  {name}: {count} variables, {access_count} accesses")
        if self.imported_exclusive_areas:
            areas = sum(count for count, _references in self.imported_exclusive_areas.values())
            references = sum(count for _areas, count in self.imported_exclusive_areas.values())
            lines.append(f"IMPORTED {areas} exclusive areas and {references} exclusive area references")
            for name in sorted(self.imported_exclusive_areas):
                count, reference_count = self.imported_exclusive_areas[name]
                lines.append(f"  {name}: {count} exclusive areas, {reference_count} references")
        if self.imported_data_events:
            lines.append(f"IMPORTED {sum(self.imported_data_events.values())} data-received events")
            for name in sorted(self.imported_data_events):
                lines.append(f"  {name}: {self.imported_data_events[name]} data-received events")
        lines.append(_skipped_summary_line(self.total_skipped, self.skip_entries))
        if self.skipped_by_tag:
            for tag in sorted(self.skipped_by_tag):
                lines.append(f"  {tag}: {self.skipped_by_tag[tag]}")
        return lines

    def _render_summary_lines(self) -> list[str]:
        """The verdict, stub and unresolved counts, then the closure, reduction and rename lines."""
        lines: list[str] = []
        if self.merged_groups:
            lines.append(f"MERGED {len(self.merged_groups)} duplicate name group(s)")
        if self.conflicting_groups:
            lines.append(f"CONFLICTING {len(self.conflicting_groups)} duplicate name group(s)")
        # Before the unresolved count: the references a stub answers are no longer in it.
        if self.external_interface_stubs or self.external_type_stubs:
            lines.append(
                f"STUBBED {len(self.external_interface_stubs)} interface(s), {len(self.external_type_stubs)} type(s)"
            )
        if self.unresolved_references:
            lines.append(
                f"UNRESOLVED {referrer_count(self.unresolved_references)} reference(s) "
                f"to {len(self.unresolved_references)} target(s)"
            )

        if self.closure_lines:
            lines.append("")
            lines.append("Package-selection dependency closure:")
            for line in self.closure_lines:
                lines.append(f"  - {line}")

        if self.reduction_lines:
            lines.append("")
            lines.append("Type-name reductions (app/impl twin collapse):")
            for line in self.reduction_lines:
                lines.append(f"  - {line}")

        if self.rename_lines:
            lines.append("")
            lines.append("Type/prototype/reserved-name renames:")
            for line in self.rename_lines:
                lines.append(f"  - {line}")
        return lines

    def _render_stub_sections(self, release: ArxmlVersion | None) -> list[str]:
        """The external stub sections; *release* is the release the unowned-port citation names."""
        lines: list[str] = []
        if self.external_stub_lines:
            lines.append("")
            lines.append(
                f"External component stubs ({len(self.external_stub_lines)}; the reference is written "
                "back verbatim, the type behind it is not in this export):"
            )
            for line in self.external_stub_lines:
                lines.append(f"  - {line}")

        if self.external_stub_port_lines:
            lines.append("")
            lines.append(
                f"External component stub ports ({len(self.external_stub_port_lines)} stub(s); name and "
                "direction as the connector ends state them, no interface is declared -- an observed "
                "interface is the direct neighbours' hint and is never judged):"
            )
            for line in self.external_stub_port_lines:
                lines.append(f"  - {line}")
        if self.stub_port_contradiction_lines:
            lines.append("")
            lines.append(
                f"Stub ports not imported ({len(self.stub_port_contradiction_lines)}; the export states "
                "contradicting directions, and every connector reaching one is skipped):"
            )
            for line in self.stub_port_contradiction_lines:
                lines.append(f"  - {line}")
        if self.stub_port_ambiguity_lines:
            lines.append("")
            lines.append(
                f"Stub ports with no observed interface ({len(self.stub_port_ambiguity_lines)}; the direct "
                "neighbours name different interfaces):"
            )
            for line in self.stub_port_ambiguity_lines:
                lines.append(f"  - {line}")

        # A block of its own: the stub lines and these skips are narrowed independently by `--packages`.
        if self.unowned_stub_port_paths:
            lines.append("")
            lines.append(
                f"{len(self.unowned_stub_port_paths)} connector(s)/delegation(s) reaching an external "
                f"stub were skipped because the {UNOWNED_TARGET_PORT} "
                f"({cite(StandardRule.INSTANCE_REF_PATH, release)}) "
                f'-- a defect of the export; see "{DEFECTS_SECTION_TITLE}" below.'
            )

        for heading, stubs in (
            (EXTERNAL_INTERFACE_STUBS_HEADING, self.external_interface_stubs),
            (EXTERNAL_TYPE_STUBS_HEADING, self.external_type_stubs),
        ):
            if stubs:
                lines.append("")
                lines.append(f"{heading} ({len(stubs)}; {EXTERNAL_STUBS_HEADING_TAIL}):")
                lines.extend(_external_stub_line(stub) for stub in stubs)
        return lines

    def _render_value_sections(self) -> list[str]:
        """The sections of values the import re-spelled, collapsed, placed or could not carry."""
        lines: list[str] = []
        if self.init_radix_lines:
            lines.append("")
            lines.append(
                f"Init values re-spelled from hexadecimal, octal or binary to decimal ({len(self.init_radix_lines)}; "
                "the value is preserved, the radix is not):"
            )
            for line in self.init_radix_lines:
                lines.append(f"  - {line}")

        if self.access_collapse_lines:
            lines.append("")
            lines.append("Collapsed duplicate access points:")
            for line in self.access_collapse_lines:
                lines.append(f"  - {line}")

        if self.replaced_scaffold_lines:
            lines.append("")
            lines.append("Replaced scaffolded compositions:")
            for line in self.replaced_scaffold_lines:
                lines.append(f"  - {line}")

        if self.file_suffix_lines:
            lines.append("")
            lines.append(file_suffix_heading(len(self.file_suffix_lines)))
            for line in self.file_suffix_lines:
                lines.append(f"  - {line}")

        if self.package_placement_lines:
            lines.append("")
            lines.append(package_placement_heading(len(self.package_placement_lines)))
            for line in self.package_placement_lines:
                lines.append(f"  - {line}")

        if self.native_declaration_missing_lines:
            lines.append("")
            lines.append(NATIVE_DECLARATION_MISSING_HEADING + ":")
            for line in self.native_declaration_missing_lines:
                lines.append(f"  - {line}")

        if self.unread_physics_lines:
            lines.append("")
            lines.append(UNREAD_PHYSICS_HEADING + ":")
            for line in self.unread_physics_lines:
                lines.append(f"  - {line}")

        if self.represented_lines:
            lines.append("")
            lines.append(REPRESENTED_HEADING + ":")
            for line in self.represented_lines:
                lines.append(f"  - {line}")

        if self.queue_length_lines:
            lines.append("")
            lines.append(f"{QUEUE_LENGTH_HEADING} ({len(self.queue_length_lines)}; {QUEUE_LENGTH_HEADING_TAIL}):")
            for line in self.queue_length_lines:
                lines.append(f"  - {line}")

        if self.incompatible_map_lines:
            lines.append("")
            lines.append(f"{INCOMPATIBLE_MAP_HEADING} ({len(self.incompatible_map_lines)}):")
            for line in self.incompatible_map_lines:
                lines.append(f"  - {line}")
        return lines

    def _render_verdicts_and_references(self, verbose: bool) -> list[str]:
        """The merged and conflicting groups, the DEST disagreements and the unresolved references.

        Each unresolved reference names every referring element when *verbose*, else a cut list.
        """
        lines: list[str] = []
        # Before the references: a reference left unresolved by a conflict names that conflict's line.
        if self.merged_groups:
            lines.append("")
            lines.append(_merged_heading(len(self.merged_groups)))
            for group in self.merged_groups:
                lines.extend(_merged_lines(group))

        if self.conflicting_groups:
            lines.append("")
            lines.append(_conflicting_heading(len(self.conflicting_groups)))
            for group in self.conflicting_groups:
                lines.append(_conflicting_line(group))

        # Before the references too: each line explains why its path's references are listed, not stubbed.
        if self.dest_disagreements:
            lines.append("")
            lines.append(_dest_disagreement_heading(len(self.dest_disagreements)))
            lines.extend(_dest_disagreement_line(record) for record in self.dest_disagreements)

        if self.unresolved_references:
            lines.append("")
            lines.append(_unresolved_heading(self.unresolved_references))
            for record in self.unresolved_references:
                lines.extend(_unresolved_lines(record, verbose=verbose))
        return lines

    def _render_skip_entries(self, verbose: bool, release: ArxmlVersion | None) -> list[str]:
        """The skipped elements, in three sections.

        Defects of the export, not supported by SWCraft, and outside the file, in that order, each printed
        only when it counts something and decided by each entry's ``category``, never by its text. Within a
        section one line per distinct ``(category, rule, tag, reason)`` with its count, most frequent first,
        a tie in first-met order. A defect's line carries its citation after the tag, in *release*. When
        *verbose*, every skipped element's path follows its line.
        """
        lines: list[str] = []
        if self.skip_entries:
            lines.append("")
            lines.append(SKIPPED_VERBOSE_HEADING if verbose else SKIPPED_HEADING)
            for category, groups in _skips_by_section(self.skip_entries):
                lines.append(
                    skip_section_heading(category, sum(len(paths) for _key, paths in groups), self.declared_schema)
                )
                for (_category, rule, tag, reason), paths in groups:
                    cited = "" if rule is None else f" ({cite(rule, release)})"
                    lines.append(f"  - {len(paths)} × {tag}{cited}: {reason}")
                    if verbose:
                        lines.extend(f"      {path}" for path in paths)
        return lines


#: The two stub categories' headings, and the clause both carry after the count.
EXTERNAL_INTERFACE_STUBS_HEADING = "External interface stubs"
EXTERNAL_TYPE_STUBS_HEADING = "External type stubs"
EXTERNAL_STUBS_HEADING_TAIL = (
    "declared by no element of this export -- each stub carries the path and DEST verbatim, the "
    "reference is written back as the export spelled it, and nothing behind it is imported"
)


def _external_stub_line(stub: ExternalStub) -> str:
    """One stub: the path it stands for, its ``DEST``, and the name the referrers now carry."""
    return f"  - {stub.target} (DEST {stub.dest}) -- imported as external stub '{stub.name}'"


def _dest_disagreement_heading(count: int) -> str:
    """The heading over the ``DEST`` disagreement records."""
    return (
        f"DEST disagreements ({count}; a path this export declares nowhere and references with more than "
        "one DEST -- no external stub is imported for it, and every reference to it is listed under "
        "Unresolved references):"
    )


def _dest_disagreement_line(record: DestDisagreement) -> str:
    """One path left without a stub, naming every ``DEST`` the export wrote for it."""
    return f"  - {record.target} referenced with DEST {', '.join(record.dests)} -- not imported as an external stub"


#: An unresolved-reference line names at most this many referring elements and counts the rest; the count
#: on the line is the whole of them. ``--verbose`` lists them all.
UNRESOLVED_REFERRERS_NAMED = 3


def _unresolved_heading(records: Sequence[UnresolvedReference]) -> str:
    """The heading over the unresolved-reference records, with both the reference and the target count."""
    return (
        f"Unresolved references ({referrer_count(records)} reference(s) to "
        f"{len(records)} target(s); the element that made each reference is kept, the path is "
        "written as the export spelled it, and check-model reports every one):"
    )


def _unresolved_cause_sentence(record: UnresolvedReference) -> str:
    """Why *record*'s target did not resolve, in the user's own terms.

    One sentence per cause: declared nowhere, outside the ``--packages`` selection, or declared twice with
    different definitions. Each has a different repair.
    """
    if record.cause == CAUSE_SELECTION:
        return "is outside the selected packages"
    if record.cause == CAUSE_CONFLICT:
        group = record.conflict_group or "the conflicting declaration above"
        return (
            "was declared more than once with different definitions and was not imported "
            f"-- see the conflicting declaration {group}"
        )
    return "is declared by no element of this export"


def _unresolved_lines(record: UnresolvedReference, *, verbose: bool) -> list[str]:
    """One record: its line, then its referring elements, cut unless *verbose*; the skipped list's shape."""
    lines = [
        f"  - {record.target} (DEST {record.dest or 'unstated'}) "
        f"{_unresolved_cause_sentence(record)}; {len(record.referrers)} referring element(s):"
    ]
    shown = record.referrers if verbose else record.referrers[:UNRESOLVED_REFERRERS_NAMED]
    lines.extend(f"      {referrer}" for referrer in shown)
    if len(shown) < len(record.referrers):
        lines.append(f"      … and {len(record.referrers) - len(shown)} more")
    return lines


def _merged_heading(count: int) -> str:
    """The heading over the merged duplicate-name groups."""
    return (
        f"Merged declarations ({count}; one name declared more than once and the declarations "
        "identical in everything SWCraft reads -- one element kept, every path resolving to it):"
    )


def _merged_lines(group: MergeGroup) -> list[str]:
    """One merged group: the kind and name, every source path, then what was not carried.

    Not carried and named with its path: a dropped member's description, a composition's ``LONG-NAME``,
    and a base type's ``BASE-TYPE-ENCODING`` or ``NATIVE-DECLARATION`` spelled with other surrounding
    whitespace than the kept one's.
    """
    lines = [f"  - {group.key} declared at {', '.join(group.paths)} -- kept as one element"]
    for path, description in group.dropped_descriptions:
        lines.append(f"      description not carried, from {path}: {' '.join(description.split())}")
    for path, long_name in group.dropped_long_names:
        lines.append(f"      title not carried, from {path}: {' '.join(long_name.split())}")
    for path, encoding in group.dropped_encodings:
        # Quoted with its escapes, never collapsed: the whitespace IS the difference.
        lines.append(f"      encoding spelling not carried, from {path}: {encoding!r}")
    for path, native_declaration in group.dropped_native_declarations:
        lines.append(f"      native declaration spelling not carried, from {path}: {native_declaration!r}")
    return lines


def _conflicting_heading(count: int) -> str:
    """The heading over the conflicting duplicate-name groups."""
    return (
        f"Conflicting declarations ({count}; one name declared more than once with different "
        "definitions -- nothing of the group is imported and the name stays claimed):"
    )


def _conflicting_line(group: MergeGroup) -> str:
    """One conflicting group, ending in the reason its verdict was reached.

    The group's own properties differ, a group it references conflicts (no property named: the difference
    is in that group), or the verdict would depend on itself.
    """
    if group.cyclic:
        reason = "the verdict depends on itself through a reference cycle"
    elif group.inherited_from is not None:
        reason = (
            f"it references {group.inherited_from}, which is itself declared more than once with different definitions"
        )
    elif group.difference is not None:
        reason = f"the declarations differ in {group.difference}"
    else:  # pragma: no cover -- a conflicting group always carries one of the three
        reason = "the declarations differ"
    return f"  - {group.key} declared at {', '.join(group.paths)} -- not imported: {reason}"


#: The heading over the grouped skipped list, and the one ``--verbose`` renders.
SKIPPED_HEADING = "Skipped elements, one line per reason (--verbose lists each element):"
SKIPPED_VERBOSE_HEADING = "Skipped elements, one line per reason:"


#: The three sections of the skipped list, in the order they print, and each
#: section's title. The heading line adds the count and, for the first and the
#: third, what the section means (:func:`skip_section_heading`).
SKIP_SECTION_ORDER = (SkipCategory.EXPORT_DEFECT, SkipCategory.NOT_SUPPORTED, SkipCategory.OUTSIDE_FILE)
DEFECTS_SECTION_TITLE = "Defects of the export"
NOT_SUPPORTED_SECTION_TITLE = "Not supported by SWCraft"
OUTSIDE_FILE_SECTION_TITLE = "Outside the file"

#: What the summary line calls each section's count.
_SUMMARY_LABELS = {
    SkipCategory.EXPORT_DEFECT: "export defects",
    SkipCategory.NOT_SUPPORTED: "not supported by SWCraft",
    SkipCategory.OUTSIDE_FILE: "outside the file",
}

#: One line of the skipped list: ``(category, rule, tag, reason)``.
SkipGroupKey = tuple[SkipCategory, StandardRule | None, str, str]


def _skipped_summary_line(total: int, entries: Sequence[SkipEntry]) -> str:
    """``SKIPPED N elements``, with all three sections' counts (zeros included) when *total* is not zero."""
    if not total:
        return f"SKIPPED {total} elements"
    counts = dict.fromkeys(SKIP_SECTION_ORDER, 0)
    for entry in entries:
        counts[entry.category] += 1
    split = ", ".join(f"{_SUMMARY_LABELS[category]} {counts[category]}" for category in SKIP_SECTION_ORDER)
    return f"SKIPPED {total} elements ({split})"


def skip_section_heading(category: SkipCategory, count: int, declared_schema: str | None) -> str:
    """One section's heading line, with its count; shared with the component import's abort message.

    Section 1 names the release its citations come from only when *declared_schema* names no release SWCraft
    writes: then every citation is :data:`~swcraft_tools.arxml_reader.FALLBACK_JUDGING_RELEASE`'s, and the
    clause says what the file declared instead.
    """
    if category is SkipCategory.EXPORT_DEFECT:
        tail = f"{count}; the file breaks a rule of the standard, cited"
        if release_of_schema_filename(declared_schema) is None:
            declared = (
                f"the file declares {declared_schema}" if declared_schema is not None else "the file declares no schema"
            )
            tail += f"; cited from R{FALLBACK_JUDGING_RELEASE} — {declared}"
        return f"{DEFECTS_SECTION_TITLE} ({tail}):"
    if category is SkipCategory.OUTSIDE_FILE:
        return f"{OUTSIDE_FILE_SECTION_TITLE} ({count}; the file references content it does not declare):"
    return f"{NOT_SUPPORTED_SECTION_TITLE} ({count}):"


def _skips_by_reason(entries: Iterable[SkipEntry]) -> list[tuple[SkipGroupKey, list[str]]]:
    """*entries* grouped by ``(category, rule, tag, reason)``, most frequent first, a tie in first-met order."""
    groups: dict[SkipGroupKey, list[str]] = {}
    for entry in entries:
        groups.setdefault((entry.category, entry.rule, entry.tag, entry.reason), []).append(entry.path)
    return sorted(groups.items(), key=lambda item: -len(item[1]))


def _skips_by_section(
    entries: Iterable[SkipEntry],
) -> list[tuple[SkipCategory, list[tuple[SkipGroupKey, list[str]]]]]:
    """:func:`_skips_by_reason`'s groups split into the non-empty sections, in :data:`SKIP_SECTION_ORDER`."""
    grouped = _skips_by_reason(entries)
    sections: list[tuple[SkipCategory, list[tuple[SkipGroupKey, list[str]]]]] = []
    for category in SKIP_SECTION_ORDER:
        groups = [group for group in grouped if group[0][0] is category]
        if groups:
            sections.append((category, groups))
    return sections


def write_yaml(dir_path: Path, filename: str, data: dict[str, Any], written: list[str]) -> None:
    """Write one mapped element's dict to `dir_path/filename`, creating `dir_path`, and record the path on `written`.

    It decides no file name: the caller passes the file :func:`imported_file_path` decided, or one of a temporary
    tree it composes itself."""
    dir_path.mkdir(parents=True, exist_ok=True)
    target = dir_path / filename
    target.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")
    written.append(str(target))


#: The report heading over the base types written without a native declaration.
NATIVE_DECLARATION_MISSING_HEADING = "Base types written without a native declaration (check-model reports each)"

#: The report heading over the physical properties an application type declared and this import did not
#: carry: a conversion, a unit or a range.
UNREAD_PHYSICS_HEADING = "Physical properties not imported (the application type is written without them)"

#: The heading over the values the import carries in the model's own spelling of them; nothing is lost.
#: Shared by ``import component``.
REPRESENTED_HEADING = "Represented in another form (the value is carried, spelled as the model spells it)"

#: The heading of :attr:`ImportReport.queue_length_lines`.
QUEUE_LENGTH_HEADING = "Server queue lengths not carried"
#: The heading over :attr:`ImportReport.incompatible_map_lines`.
INCOMPATIBLE_MAP_HEADING = (
    "Data type maps kept that SWCraft's composite check refuses (check-model reports each; "
    "the map is written as the export declares it)"
)
QUEUE_LENGTH_HEADING_TAIL = f"every SERVER-COM-SPEC is exported with QUEUE-LENGTH {SERVER_QUEUE_LENGTH}"


def file_suffix_heading(count: int) -> str:
    """The report heading over the file-suffix lines -- shared by both importers."""
    return (
        f"Files suffixed for a case-insensitive collision ({count}; the element names and every reference "
        "are unchanged):"
    )


def _file_suffix_line(subject: str, written: str, plain: str) -> str:
    """One file-suffix report line: what was written where, and which plain file name was taken.

    Names the plain file name, not the entry holding it, which would take a second directory read. Shared by
    both importers.
    """
    return (
        f"{subject} written as {written} -- its plain file name {plain} is already taken, compared "
        "case-insensitively; the name and its references are unchanged"
    )


def allocated_file(
    model_root: Path,
    layer: str,
    identity: str,
    subject: str,
    *,
    placement: Mapping[str, str] | None = None,
    reserved: Iterable[str] = (),
) -> tuple[FreePath, str | None]:
    """The file *identity* takes in *layer*, and the report line a collision suffix costs.

    The one decision both importers make: :func:`~swcraft_tools.model_state.free_storage_path` over the
    tree as it stands, with the paths the caller already decided passed as *reserved*, and one
    :func:`_file_suffix_line` naming *subject* when a suffix was taken. Both importers ask in the export's
    source-path order, so which of a case-only pair keeps the plain file is the same in both. What this does
    not reach: an element an earlier run stored is found by the identity its file declares and never
    re-ordered. :attr:`~swcraft_tools.model_state.FreePath.occupied_by_self` is the caller's to refuse.
    """
    registry = current_registry()
    free = free_storage_path(model_root, layer, identity, placement, registry=registry, reserved=reserved)
    if not free.suffixed:
        return free, None
    plain = compose_storage_path(registry.storage(layer), identity, placement)
    return free, _file_suffix_line(subject, free.path, plain)


def imported_file_path(
    model_root: Path,
    layer: str,
    identity: str,
    suffix_lines: list[str],
    *,
    source: str,
    stored: Mapping[str, Sequence[LayerFile]],
    placement: Mapping[str, str] | None = None,
    reserved: Iterable[str] = (),
) -> Path:
    """The file one mapped element is written to, decided now.

    An element *layer* already stores (found in *stored* by its declared identity in its own placement
    directory, the first in listing order when two declare it) goes back into that file, so ``--force``
    overwrites it without forking a second file. Any other element takes the plain file name or the nearest
    free ``~N``, and a suffix appends one line to *suffix_lines* naming the element and its *source* ARXML
    path. A dry run passes the paths it already decided as *reserved*, so it takes the suffixes the write
    would.

    Raises:
        ValueError: the plain file name is held by a file that already declares
            *identity* and that no reader of the layer lists (``Foo.YAML``
            beside no ``Foo.yaml``: the ``*.yaml`` glob does not match it).
            Writing there would report the element written and lose it from the
            model, so it is refused -- the fail-closed twin ``create_*`` and
            ``import component`` already have.
    """
    wanted = dict(placement or {})
    for entry in stored.get(identity, ()):
        if dict(entry.placement) == wanted:
            return entry.path
    free, suffix_line = allocated_file(
        model_root, layer, identity, f"{identity} ({source})", placement=placement, reserved=reserved
    )
    if free.occupied_by_self:
        raise ValueError(
            f"cannot import '{identity}' into layer '{layer}': the file {free.path} already declares it, but no "
            "reader of the layer lists that file, so writing there would lose the element from the model; rename "
            "or remove that file and import again"
        )
    if suffix_line is not None:
        suffix_lines.append(suffix_line)
    return model_root / free.path


def reserved_device_name_skip(identity: str, source_path: str, tag: str) -> SkipEntry | None:
    """The skip entry an element whose name Windows reserves for a device takes, or ``None``.

    ``rule:storage.reserved-device-name``, shared by both importers. The element is not imported and not
    renamed (``import component`` renames nothing). ``import model`` reports the entry and goes on;
    ``import component`` aborts with nothing written. The reason names the device, not the identity, so the
    report folds every element skipped for one device into one line.

    An element referring to a skipped one keeps the name it spelled, so ``check-model`` reports the written
    project and ``generate arxml`` refuses it until the name is changed at the source.
    """
    device = reserved_device_name(identity)
    if device is None:
        return None
    return SkipEntry(
        path=source_path,
        tag=tag,
        reason=(
            f"its name before the first '.' is {device}, a device name Windows reserves, so no file of that name "
            "can exist on Windows; not imported (rule:storage.reserved-device-name)"
        ),
        category=SkipCategory.NOT_SUPPORTED,
        effect="abort",
    )


def reserved_device_name_skips(
    model: ImportModel, candidates: Iterable[tuple[str, str, str]]
) -> list[tuple[int, SkipEntry]]:
    """:func:`reserved_device_name_skip` over *candidates*, each ``(name, source path, fallback tag)``.

    The tag is the declaration's own from *model*'s path index; *fallback tag* is used for an element the
    export does not declare. Each result carries the candidate's index in *candidates*.
    """
    skips: list[tuple[int, SkipEntry]] = []
    for index, (name, path, fallback_tag) in enumerate(candidates):
        raw = model.path_index.get(path)
        skip = reserved_device_name_skip(name, path, raw.tag if raw is not None else fallback_tag)
        if skip is not None:
            skips.append((index, skip))
    return skips


def package_placement_heading(count: int) -> str:
    """The heading over the package-placement lines -- shared by both importers."""
    return (
        f"Placed in a sub-package to keep a case-insensitive name unique ({count}; the name and every reference "
        "are unchanged, only the element's ARXML path moves):"
    )


#: How a package-placement line, and each importer's placement refusal, names an element of each layer.
#: Joined by ``|``, not a ``**`` unpacking, which the coverage field oracle cannot bind.
PLACEMENT_WHAT = (
    {
        "implementation_types": "data type",
        "application_types": "data type",
        "base_types": "data type",
    }
    | ELEMENT_NOUN_BY_LAYER
    | {
        "mapping_sets": "mapping set",
        "interfaces": "interface",
        "swc": "SWC",
        "compositions": "composition",
    }
)


def package_placement_line(placed: PlacedMember, root: str) -> str:
    """One package-placement line: the element, its source path and the package it is emitted in.

    A member whose source segment is no legal ``Identifier`` names that segment, explaining ``CaseVariant``.
    """
    member = placed.declaration
    line = f"{PLACEMENT_WHAT[member.layer]} '{member.name}' ({member.source_path}) placed in /{root}/{placed.package}"
    if placed.unused_segment is not None:
        line += f" -- its source package name '{placed.unused_segment}' is not a legal AUTOSAR Identifier"
    return line


def case_only_duplicate_skip(path: str, tag: str, kept: str, kept_path: str, *, variant_rich: bool) -> SkipEntry:
    """The skip entry of the second of two declarations of one package whose names differ only in case.

    ``[constr_2508]`` makes a ``SHORT-NAME`` unique ignoring case within its package, so this is the file's
    own defect; the first by full source path is kept. The category is
    :func:`~swcraft_tools.arxml_reader.duplicate_name_reason`'s, as for a duplicate full path.
    """
    reason = duplicate_name_reason(
        f"its SHORT-NAME differs only in case from '{kept}' ({kept_path}), declared in the same package "
        "-- this declaration is dropped",
        variant_rich=variant_rich,
    )
    return SkipEntry(path=path, tag=tag, reason=reason.text, category=reason.category, rule=reason.rule, effect="abort")


# ---------------------------------------------------------------------------
# The names SWCraft generates itself
# ---------------------------------------------------------------------------


#: The prose noun of each generated data type form, beside its tag in the line.
_GENERATED_FORM_NOUNS = {"COMPU-METHOD": "a compu method", "UNIT": "a unit", "DATA-CONSTR": "a data constraint"}


def generated_name_losses(
    model: ImportModel,
    data_types: Sequence[DataType],
    swcs: Sequence[ImportedSwc],
    type_source_paths: Mapping[tuple[str, str], str],
    skip_log: SkipLog,
    *,
    not_stated: Mapping[str, str],
) -> None:
    """Record, into *skip_log*, every generated name the writer will not write back.

    The writer names an internal behaviour, an event, an access point, a result point, an execution-time
    budget, a component's ``SWC-TIMING``, a compu method, a unit and a constraint itself, unless the model
    states the name (behaviour, event, access point and result point only). Recorded, each descriptive:

    - a source element whose ``SHORT-NAME`` differs from the name the writer emits, keyed
      ``({element}, SHORT-NAME)``; *not_stated* holds, by path, the clause saying why the name was not
      stated;
    - a compu method, unit or constraint more than one carried referrer references, keyed at the element:
      the writer writes one per type;
    - a source ``SWC-TIMING`` outside the component's package, or a component's budgets split over several.

    The names come from the writer's and the validator's own functions over the **final** model: *data_types*
    and *swcs* after every rename, *type_source_paths* each type's source path keyed ``(layer, name)``. A
    SWCraft-written file records nothing.

    What this does not reach: an element the writer names from a value not carried (the unread-physics
    line), and an access point collapsed into an earlier one (the collapse line).
    """
    losses: list[tuple[str, str, str]] = []
    placements: list[tuple[str, str]] = []
    raw_by_path = {raw.path: raw for raw in model.swcs}
    for imported in swcs:
        raw = raw_by_path.get(imported.path)
        if imported.swc.external or raw is None:
            continue
        swc = imported.swc
        if raw.behavior_name is not None and raw.behavior_name != emitted_behavior_name(swc):
            losses.append((f"{raw.path}/{raw.behavior_name}", "SWC-INTERNAL-BEHAVIOR", "an internal behaviour"))
        pairs = [(runnable, index) for runnable in swc.runnables for index, _event in enumerate(runnable.events)]
        event_names = dict(
            zip(
                ((runnable.name, index) for runnable, index in pairs),
                emitted_event_names(swc),
                strict=True,
            )
        )
        access_names: dict[tuple[str, str, int], str] = {}
        for runnable in swc.runnables:
            for (collection, index), name in zip(access_slots(runnable), emitted_access_names(runnable), strict=True):
                access_names[(runnable.name, collection, index)] = name
            # The result points: a poll's at its call, an event's at the event.
            for (collection, index), name in zip(
                result_slots(runnable), emitted_result_point_names(runnable), strict=True
            ):
                access_names[(runnable.name, collection, index)] = name
        for source in imported.generated_sources:
            if source.collection == "events":
                written = event_names.get((source.runnable_name, source.index))
                what = "an event"
            elif source.collection.endswith(".result"):
                written = access_names.get((source.runnable_name, source.collection, source.index))
                what = "a result point"
            else:
                written = access_names.get((source.runnable_name, source.collection, source.index))
                what = "an access point"
            if written is not None and source.name != written:
                losses.append((source.path, source.tag, what))
        # The writer writes one SWC-TIMING beside the component, so a source package or split is not carried.
        timing_names = {source.timing_path: source.timing_name for source in imported.budget_sources}
        timing_written = f"{swc.name}{TIMING_SUFFIX}"
        for budget in imported.budget_sources:
            if budget.constraint_name != f"{budget.runnable_name}{BUDGET_SUFFIX}":
                losses.append((budget.constraint_path, EXECUTION_TIME_CONSTRAINT, "an execution-time budget"))
        component_package = imported.path.rsplit("/", 1)[0]
        for timing_path, timing_name in timing_names.items():
            if timing_name != timing_written:
                losses.append((timing_path, "SWC-TIMING", "a component's timing"))
            if timing_path.rsplit("/", 1)[0] != component_package:
                placements.append(
                    (
                        timing_path,
                        f"package of the SWC-TIMING not carried: SWCraft writes {timing_written} beside the "
                        f"component's SWC-IMPLEMENTATION, in the component's own package",
                    )
                )
            if len(timing_names) > 1:
                placements.append(
                    (
                        timing_path,
                        f"split of a component's budgets over {len(timing_names)} SWC-TIMINGs not carried: "
                        f"SWCraft writes them as one {timing_written}",
                    )
                )

    app_by_path = {app.path: app for app in model.app_primitive_data_types}
    impl_by_path = {impl.path: impl for impl in model.impl_data_types}
    forms: dict[str, tuple[str, str, list[str]]] = {}
    names_by_path = {
        form.path: (tag, form.name)
        for tag, collection in (
            ("COMPU-METHOD", model.compu_methods),
            ("UNIT", model.units),
            ("DATA-CONSTR", model.data_constrs),
        )
        for form in collection
    }

    def refer(path: str | None, written: str) -> None:
        if path and path in names_by_path:
            tag, name = names_by_path[path]
            forms.setdefault(path, (tag, name, []))[2].append(written)

    for dt in data_types:
        if isinstance(dt, ValueDataType):
            # A value type's `{name}_Range` is emitted exactly where a bound is declared.
            impl = impl_by_path.get(type_source_paths.get(data_type_key(dt), ""))
            if impl is not None and RANGE_SUFFIX in generated_data_type_forms(dt):
                refer(impl.data_constr_ref, f"{dt.name}{RANGE_SUFFIX}")
            continue
        app = app_by_path.get(type_source_paths.get(data_type_key(dt), ""))
        if app is None:
            continue
        if isinstance(dt, EnumDataType):
            # Generated from `literals` alone; an enumeration naming a table keeps the table's name.
            if dt.literals is not None:
                refer(app.compu_method_ref, f"{dt.name}{COMPU_METHOD_SUFFIX}")
        elif isinstance(dt, ApplicationDataType):
            # The writer's own presence rule: a form is emitted exactly where the
            # field that produces it is declared.
            emitted = generated_data_type_forms(dt)
            for suffix, ref in (
                (UNIT_SUFFIX, app.unit_ref),
                (COMPU_SUFFIX, app.compu_method_ref),
                (RANGE_SUFFIX, app.data_constr_ref),
            ):
                if suffix in emitted:
                    refer(ref, f"{dt.name}{suffix}")
    for path, (tag, name, written_names) in forms.items():
        if name not in written_names:
            losses.append((path, tag, _GENERATED_FORM_NOUNS[tag]))
        if len(written_names) > 1:
            skip_log.record(
                path,
                tag,
                f"{tag} shared by {len(written_names)} data types not carried as one: SWCraft writes one per type",
                category=SkipCategory.NOT_SUPPORTED,
                effect=REPORT,
            )
    for path, reason in placements:
        skip_log.record(path, "SWC-TIMING", reason, category=SkipCategory.NOT_SUPPORTED, effect=REPORT)
    for path, tag, what in losses:
        clause = f", {not_stated[path]}" if path in not_stated else ""
        skip_log.record(
            path,
            "SHORT-NAME",
            f"SHORT-NAME of {what} ({tag}) not carried: SWCraft names it itself{clause}",
            category=SkipCategory.NOT_SUPPORTED,
            effect=REPORT,
        )


def data_event_counts(swcs: Iterable[SWComponent]) -> dict[str, int]:
    """``name -> data-received events`` of every component in *swcs* that declares any."""
    counts: dict[str, int] = {}
    for swc in swcs:
        events = sum(isinstance(event, DataEvent) for runnable in swc.runnables for event in runnable.events)
        if events:
            counts[swc.name] = events
    return counts
