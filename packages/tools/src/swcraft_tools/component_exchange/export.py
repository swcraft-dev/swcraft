"""Single-component ARXML export.

:func:`export_component` materializes one SWC's forward dependency closure as a YAML model tree
in a temp directory and runs the whole-model writer on it. :func:`validate_component_closure` runs
only the checks whose subject a closure holds, so a broken connector or requirement link never
blocks a component export; a malformed or schema-invalid file in ``swc/``, ``interfaces/`` or
``data_types/`` still fails it.

A failure never leaves a partial file. Both entry points take a keyword-only ``version`` (default
4.2.2); this module never loads the manifest.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from swcraft_tools.arxml_import.report import interface_to_dict, write_yaml
from swcraft_tools.arxml_version import DEFAULT_ARXML_VERSION, ArxmlVersion
from swcraft_tools.model_registry import compose_storage_path, current_registry
from swcraft_tools.model_state import ModelRootNotFoundError, free_storage_path, parse_element, parse_label
from swcraft_tools.rendering import path_free
from swcraft_tools.schema import ExternalDataType, ExternalInterface, SWComponent
from swcraft_tools.validator import (
    check_application_type_double_mapping,
    check_application_type_mapping,
    check_async_calls,
    check_conversion_units,
    check_data_description_elements,
    check_data_events,
    check_data_type_cycles,
    check_data_type_references,
    check_disabled_modes,
    check_exclusive_areas,
    check_implicit_reads,
    check_inter_runnable_variables,
    check_mapping_set_references,
    check_name_space_collisions,
    check_name_uniqueness,
    check_operation_events,
    check_port_com_specs,
    check_queued_reads,
    check_queued_receivers,
    check_runnable_symbols,
    check_termination_and_restart,
    check_xml_representable_text,
    data_type_layer,
)
from swcraft_tools.writer.arxml_generation import generate_component_arxml
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.writer.yaml_documents import data_type_to_dict, swc_to_dict
from swcraft_tools.yaml_load import load_yaml_document

from .closure import ComponentClosure, ComponentExchangeError, compute_closure
from .report import (
    EXPORT_SUMMARY_FILENAME,
    STATUS_FAILED,
    STATUS_OK,
    ComponentExportReport,
    render_export_summary,
)

# The `group` placement value of the SWC in the throwaway temp tree. The original group is not
# part of SWComponent, so an export invents one; a fixed value keeps exports byte-identical.
# Singular, unlike import_.py's plural _IMPORT_TARGET_SWC_GROUP: this directory exists only in a
# deleted temp tree, so the names are deliberately distinct.
_EXPORT_TEMP_SWC_GROUP = "component"


#: The rules of ``check_name_uniqueness`` the closure check keeps: each judges the closure's
#: one component alone.
_CLOSURE_NAME_UNIQUENESS_RULES = frozenset(
    {"name.runnable-event-collision", "name.duplicate-event", "swc.access.name-collision"}
)


class ClosureValidationError(ComponentExchangeError):
    """The materialized closure failed scoped validation (:func:`validate_component_closure`).

    The validator issues are on :attr:`issues`, and the rule id of each, at the same position,
    on :attr:`rules`, so ``len(rules) == len(issues)`` whichever check refused.
    """

    def __init__(self, message: str, issues: list[str], rules: Sequence[str]) -> None:
        super().__init__(message)
        self.issues: list[str] = list(issues)
        self.rules: tuple[str, ...] = tuple(rules)


def validate_component_closure(model_root: str, *, version: ArxmlVersion | str = DEFAULT_ARXML_VERSION) -> None:
    """Validate a materialized component closure tree — closure scope only.

    A warning refuses nothing, as in ``check-model``.

    * ``check_application_type_mapping``: a component whose own port speaks an application
      type or an enumeration that no set it names pairs (``[constr_1004]``).
    * ``check_application_type_double_mapping``: ``[constr_1004]``'s *exactly one* half, asked
      of the **narrowed** sets.
    * ``check_conversion_units``: a ``conversion`` is written as a ``LINEAR`` compu method that
      must refer to the type's unit (``[constr_1175]``).
    * ``check_async_calls`` is asked at *version*, the version the export declares: it
      includes ``[constr_2006]`` of R4.2.2 (an asynchronous call with no result point), which
      the writer judges at that version.

    Not composed: ``check_cross_layer_consistency``, ``rule:composition.port-service-interface``,
    ``check_init_conformance``, the other ``check_name_uniqueness`` rules and
    ``check_identifier_conformance``. What those rules do not reach: the members of a composite, and a
    mapping declared and never used.

    Raises:
        ClosureValidationError: any composed check reported issues.
    """
    target_version = ArxmlVersion.from_string(version)
    issues: list[str] = []
    rules: list[str] = []
    for check in (
        check_data_type_references,
        check_data_type_cycles,
        check_data_description_elements,
        check_mapping_set_references,
        check_application_type_mapping,
        check_application_type_double_mapping,
        check_name_space_collisions,
        check_termination_and_restart,
        check_conversion_units,
        check_port_com_specs,
        check_runnable_symbols,
        check_queued_receivers,
        check_disabled_modes,
        check_queued_reads,
        check_xml_representable_text,
        check_inter_runnable_variables,
        check_exclusive_areas,
        check_operation_events,
        check_data_events,
        check_implicit_reads,
        check_async_calls,
    ):
        # The version-aware member is the one handed the version, as in `check-model`.
        result = check(model_root, target_version) if check is check_async_calls else check(model_root)
        if not result.consistent:
            issues.extend(result.issues)
            rules.extend(record.rule for record in result.records)
    # Only these rules: the other shapes are the writer twin's to refuse.
    kept = [
        record for record in check_name_uniqueness(model_root).records if record.rule in _CLOSURE_NAME_UNIQUENESS_RULES
    ]
    issues.extend(record.message for record in kept)
    rules.extend(record.rule for record in kept)
    if issues:
        raise ClosureValidationError(
            "component closure validation failed: " + "; ".join(issues),
            issues,
            rules,
        )


def _materialize_closure(closure: ComponentClosure, root: Path) -> None:
    """Write the closure as a mini YAML model tree under ``root``.

    Uses ``import model``'s dict/write helpers, so files match the writer-pattern dump shape;
    closure lists are name-sorted, so the tree is deterministic. The SWC's path is composed
    from the SWC layer's ``storage`` record.

    The flat layers ask :func:`~swcraft_tools.model_state.free_storage_path`, so two names
    differing only in case (``Float32`` beside ``float32``) become two files here as in the
    project; a hand-joined name would merge them on a case-insensitive filesystem and the
    closure check would report a false undeclared type. A ``~N`` here reaches nothing emitted:
    every ``SHORT-NAME`` is read from the element's content.
    """
    written: list[str] = []
    registry = current_registry()
    for dt in closure.data_types:
        free = free_storage_path(root, data_type_layer(dt), dt.name, registry=registry)
        target = root / free.path
        write_yaml(target.parent, target.name, data_type_to_dict(dt), written)
    # Dumped with the generic `element_to_dict` `import model` uses for the same layer, so one
    # element has one storage form whichever writer produced it.
    for mapping_set in closure.mapping_sets:
        free = free_storage_path(root, "mapping_sets", mapping_set.name, registry=registry)
        target = root / free.path
        write_yaml(target.parent, target.name, element_to_dict(mapping_set), written)
    for element in closure.elements:
        free = free_storage_path(root, data_type_layer(element), element.name, registry=registry)
        target = root / free.path
        write_yaml(target.parent, target.name, element_to_dict(element), written)
    for iface in closure.interfaces:
        free = free_storage_path(root, "interfaces", iface.name, registry=registry)
        target = root / free.path
        write_yaml(target.parent, target.name, interface_to_dict(iface), written)
    record = registry.storage("swc")
    swc_file = root / compose_storage_path(record, closure.swc.name, {record.nesting: _EXPORT_TEMP_SWC_GROUP})
    write_yaml(swc_file.parent, swc_file.name, swc_to_dict(closure.swc), written)


def export_component(
    model_root: str,
    swc_name: str,
    output_path: str,
    *,
    version: ArxmlVersion | str = DEFAULT_ARXML_VERSION,
) -> ComponentExportReport:
    """Export one SWC's dependency closure as a self-contained ARXML file.

    The same inputs produce byte-identical ARXML. On every failure ``output_path`` is untouched and
    no ``.tmp`` is left. An external stub is refused on the ``external`` flag alone, saying nothing
    about whether the type behind ``external_ref`` exists or matches the ports.

    Args:
        output_path: where to write the ARXML (parent dirs are created).
        version: the AUTOSAR version the file declares; parsed before the model is read.

    Returns:
        A ``status="ok"`` report; failures always raise.

    Raises:
        ComponentNotFoundError: no SWC declares ``swc_name``.
        AmbiguousComponentError: 2+ SWC files declare ``swc_name``.
        AmbiguousReferenceError: 2+ files declare a referenced name.
        MissingReferenceError: the closure references an undeclared name.
        ClosureValidationError: the materialized closure fails scoped validation.
        ComponentExchangeError: ``swc_name`` names an ``external: true`` stub, or the closure
            reaches an external interface or type stub (:func:`_refuse_reached_stubs`).
        ValueError: an unsupported *version*, and every ARXML generation failure.
    """
    target_version = ArxmlVersion.from_string(version)
    closure = compute_closure(model_root, swc_name)
    if closure.swc.external:
        raise ComponentExchangeError(
            f"SWC '{closure.swc.name}' is an external stub (external: true): it is a reference to a "
            f"component type this project does not declare. The exporter writes its external_ref "
            f"({closure.swc.external_ref!r}) verbatim on a prototype and emits no "
            f"APPLICATION-SW-COMPONENT-TYPE, so there is no component type to export. Nothing was written."
        )
    _refuse_reached_stubs(closure)
    tmp_output = f"{output_path}.tmp"
    with tempfile.TemporaryDirectory(prefix="swcraft-component-export-") as temp_root:
        _materialize_closure(closure, Path(temp_root))
        validate_component_closure(temp_root, version=target_version)
        try:
            generate_component_arxml(temp_root, tmp_output, version=target_version)
            os.replace(tmp_output, output_path)
        except BaseException:
            Path(tmp_output).unlink(missing_ok=True)
            raise
    return ComponentExportReport(
        swc=closure.swc.name,
        output_path=output_path,
        status=STATUS_OK,
        interfaces=[iface.name for iface in closure.interfaces],
        data_types=[dt.name for dt in closure.data_types],
        error=None,
    )


def _scan_swc_names(model_root: str) -> tuple[list[str], list[ComponentExportReport]]:
    """Return every declared SWC ``name`` under the SWC layer, plus a failed report per unreadable file.

    A file that fails to parse or validate becomes a failed report instead of raising, so the
    batch is never aborted at the listing step; it is keyed by its raw ``name`` when readable,
    else by its path. An external stub yields neither a name nor a report; the decision is made
    on the *validated* element, so a non-boolean ``external`` is a failed report, never a skip.
    """
    root = Path(model_root)
    if not root.exists():
        raise ModelRootNotFoundError(root)
    swc_dir = root / current_registry().storage("swc").directory
    names: list[str] = []
    failed: list[ComponentExportReport] = []
    if not swc_dir.exists():
        return names, failed
    label = parse_label(current_registry().layer("swc"))
    for yaml_file in sorted(swc_dir.glob("**/*.yaml"), key=lambda path: path.as_posix()):
        try:
            element = parse_element(yaml_file, label, SWComponent.model_validate, root)
        except Exception as exc:  # noqa: BLE001 -- batch: an unreadable file is one failed report, the scan goes on
            raw = _loaded_or_none(yaml_file)
            pseudo_name = raw["name"] if isinstance(raw, dict) and isinstance(raw.get("name"), str) else None
            failed.append(
                ComponentExportReport(
                    swc=pseudo_name or yaml_file.relative_to(root).as_posix(),
                    output_path=None,
                    status=STATUS_FAILED,
                    error=path_free(exc, root),  # the reader's refusal, or an OSError, by storage name
                )
            )
        else:
            if element.external:
                continue
            names.append(element.name)
    return names, failed


def export_all_components(
    model_root: str,
    output_dir: str,
    *,
    version: ArxmlVersion | str = DEFAULT_ARXML_VERSION,
) -> list[ComponentExportReport]:
    """Export every SWC to ``<output_dir>/<name>.arxml``, plus ``export-summary.json``.

    One SWC's failure is a ``status="failed"`` entry and never aborts the batch. Names differing
    only in case are both exported, the later one as ``<name>~N.arxml``. An external stub is
    skipped and appears nowhere, neither in the reports nor in the summary.

    Args:
        output_dir: created if needed.
        version: the AUTOSAR version every exported file declares; parsed before ``output_dir`` is
            created.

    Returns:
        One :class:`ComponentExportReport` per exportable SWC name, sorted by name, as in the
        summary file. The summary carries no version; each exported file declares it.

    Raises:
        ValueError: an unsupported *version*.
    """
    target_version = ArxmlVersion.from_string(version)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    valid_names, scan_failures = _scan_swc_names(model_root)
    swc_names = sorted(set(valid_names))
    file_names = _batch_file_names(swc_names)
    reports: list[ComponentExportReport] = list(scan_failures)
    for name in swc_names:
        target = out_dir / file_names[name]
        try:
            reports.append(export_component(model_root, name, str(target), version=target_version))
        # Everything per SWC: YAML, Pydantic, writer and lxml errors can surface inside one
        # SWC's closure, and the batch contract is one report per SWC, never a mid-loop abort.
        except Exception as exc:  # noqa: BLE001 -- batch: one report per SWC, never a mid-loop abort
            reports.append(
                ComponentExportReport(
                    swc=name,
                    output_path=None,
                    status=STATUS_FAILED,
                    error=path_free(exc, Path(model_root), out_dir),
                )
            )
    # Scan failures were prepended; re-sort so "sorted by name" holds.
    reports.sort(key=lambda report: report.swc)
    # Machine-readable and compared across runs, so `\n` on every platform.
    (out_dir / EXPORT_SUMMARY_FILENAME).write_text(
        render_export_summary(reports, str(out_dir)), encoding="utf-8", newline="\n"
    )
    return reports


def _batch_file_names(names: list[str]) -> dict[str, str]:
    """Return the output file name of each of *names*: ``<name>.arxml``, or the nearest free ``<name>~N.arxml``.

    *names* in planning order; a name is held when one planned before it is equal ``str.casefold()``-wise.
    **Does not reach:** a file already in the output directory (replaced on a case-insensitive match,
    stale files never removed); Unicode normalisation; nothing parses a suffix back out of a file name.
    """
    held: set[str] = set()
    planned: dict[str, str] = {}
    for name in names:
        candidate = f"{name}.arxml"
        n = 0
        while candidate.casefold() in held:
            n += 1
            candidate = f"{name}~{n}.arxml"
        held.add(candidate.casefold())
        planned[name] = candidate
    return planned


def _loaded_or_none(yaml_file: Path) -> Any:
    """Return *yaml_file*'s document, or ``None`` when it cannot be read or parsed.

    Read only to label a file the reader refused by its declared ``name``.
    """
    try:
        return load_yaml_document(yaml_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def _refuse_reached_stubs(closure: ComponentClosure) -> None:
    """Refuse a closure that reaches an external interface or type stub.

    The writer emits a stub's ``external_ref`` and no element for it, so the file would
    reference a definition it does not contain and ``import component`` could not take it
    back whole. :func:`export_all_components` records the refusal as that component's
    ``failed`` row: unlike a component stub, the component itself is real.

    What it does **not** reach: decided on the class alone, it says nothing about whether the
    definition behind ``external_ref`` exists or matches the ports around it.
    """
    reached = [
        f"external interface '{iface.name}' ({iface.external_ref!r})"
        for iface in closure.interfaces
        if isinstance(iface, ExternalInterface)
    ]
    reached += [
        f"external type '{data_type.name}' ({data_type.external_ref!r})"
        for data_type in closure.data_types
        if isinstance(data_type, ExternalDataType)
    ]
    if reached:
        raise ComponentExchangeError(
            f"SWC '{closure.swc.name}' reaches {', '.join(reached)}: a stub is a reference to a definition this "
            f"project does not declare. The exporter writes a stub's external_ref verbatim and emits no element "
            f"for it, so the exported file would reference a definition it does not contain, which swcraft import "
            f"component could not import back whole. Nothing was written."
        )


def default_component_file_name(model_root: str, swc_name: str) -> str:
    """Return the file name a single-component export of *swc_name* takes when the caller names no output path.

    The batch's own name for it (:func:`_batch_file_names`); a name the plan does not hold gets
    ``<name>.arxml``. **Does not reach:** the directory (the plan is the model's); a twin added to the
    model later can move the default name; an explicit output path is not checked against any other file.
    """
    valid_names, _ = _scan_swc_names(model_root)
    return _batch_file_names(sorted(set(valid_names))).get(swc_name, f"{swc_name}.arxml")


#: The directory under ``dist_root`` both component exports default to.
COMPONENTS_DIR = "components"


def default_component_output(model_root: str, dist_root: str, swc_name: str) -> str:
    """Return the path a single-component export of *swc_name* writes when the caller names none.

    The one composition of that default, shared by the CLI and the MCP tool so they cannot
    differ; an explicit output path never passes through here.
    """
    return os.path.join(dist_root, COMPONENTS_DIR, default_component_file_name(model_root, swc_name))
