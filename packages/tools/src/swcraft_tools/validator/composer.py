"""The cross-layer composer: every check of the package, run over one model.

:func:`check_cross_layer_consistency` builds one :class:`ModelView`, mints the
``trace.link-unknown-requirement`` findings itself, runs every other check of the
package over the same view in a fixed order, and returns one deduplicated
:class:`ConsistencyResult`.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet

from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.diagnostics import Subject
from swcraft_tools.trace import (
    LinkSite,
    element_link_sites,
    is_element_form,
    is_version,
    link_site_where,
    link_sites,
    parse_link,
)
from swcraft_tools.validator.access import check_runnable_access
from swcraft_tools.validator.asil import check_component_asil, check_safety_coexistence
from swcraft_tools.validator.behaviour import (
    check_async_calls,
    check_conversion_units,
    check_disabled_modes,
    check_exclusive_areas,
    check_implicit_reads,
    check_inter_runnable_variables,
    check_port_com_specs,
    check_queued_reads,
    check_queued_receivers,
    check_runnable_symbols,
    check_service_composition_ports,
    check_termination_and_restart,
)
from swcraft_tools.validator.compositions import check_composition_consistency, check_port_interface_references
from swcraft_tools.validator.connectors import check_connector_consistency
from swcraft_tools.validator.data_types import (
    check_application_type_double_mapping,
    check_application_type_mapping,
    check_base_type_native_declarations,
    check_data_type_cycles,
    check_data_type_references,
    check_mapping_set_references,
)
from swcraft_tools.validator.description_elements import check_data_description_elements
from swcraft_tools.validator.events import check_data_events, check_identifier_conformance, check_operation_events
from swcraft_tools.validator.hardware import (
    check_hardware_capability,
    check_hardware_topology,
    check_resource_budgets,
    check_runnable_costs,
)
from swcraft_tools.validator.init_values import check_init_conformance
from swcraft_tools.validator.model_view import ModelInput, ModelView, as_view
from swcraft_tools.validator.names import check_name_space_collisions, check_name_uniqueness
from swcraft_tools.validator.representable_text import check_xml_representable_text
from swcraft_tools.validator.results import ConsistencyIssue, ConsistencyResult, consistency_result, deduplicated
from swcraft_tools.validator.scheduling import check_sequence_consistency, check_task_consistency
from swcraft_tools.validator.storage import check_storage_portability, check_unknown_keys
from swcraft_tools.validator.traceability import check_requirement_traceability

# ---------------------------------------------------------------------------
# check_cross_layer_consistency
# ---------------------------------------------------------------------------


def check_cross_layer_consistency(
    model_root: ModelInput, arxml_version: ArxmlVersion | str | None = None
) -> ConsistencyResult:
    """Run every cross-layer consistency check over the model and combine the findings.

    Check 1 is the ``satisfies`` link check (``trace.link-unknown-requirement``); the others run in
    the order of ``_composed_check_records``' tuple. The findings come in that order, which the
    shipped vectors freeze; a check that abstains on another's finding runs after it, so a broken
    model is reported once.

    Args:
        model_root: The model root, or a view of it.
        arxml_version: The AUTOSAR version the project generates; ``None`` means 4.2.2. Read by
            ``check_async_calls`` alone (``[constr_2006]``, judged per edition).

    Returns:
        ConsistencyResult with consistent=True only when every error check passes; warning
        findings are in ``warnings`` and leave ``consistent`` and ``issues`` alone.
    """
    view = as_view(model_root)
    records = _unknown_requirement_link_records(view)
    records += _composed_check_records(view, arxml_version)
    # Both data type checks report a duplicate type name; the first occurrence wins.
    return consistency_result(deduplicated(records))


def _unknown_requirement_link_records(view: ModelView) -> list[ConsistencyIssue]:
    """Check 1 of :func:`check_cross_layer_consistency`, over every linkable element in layer order.

    An element that satisfies nothing is no finding.
    """
    swcs = view.swcs
    requirements = view.requirements
    declared_ids = {requirement.id for requirement in requirements}
    records: list[ConsistencyIssue] = []
    for swc in swcs:
        for site in link_sites(swc):
            records += _unknown_requirement_link(Subject("swc", swc.name, site.steps), "SWC", site, declared_ids)
    for interface in view.interfaces:
        for site in element_link_sites(interface):
            subject = Subject("interfaces", interface.name, site.steps)
            records += _unknown_requirement_link(subject, "Interface", site, declared_ids)
    for composition in view.compositions:
        for site in element_link_sites(composition):
            subject = Subject("compositions", composition.name, site.steps)
            records += _unknown_requirement_link(subject, "Composition", site, declared_ids)
    for task in view.tasks:
        for site in element_link_sites(task):
            records += _unknown_requirement_link(Subject("tasks", task.name, site.steps), "Task", site, declared_ids)
    for element in view.hardware:
        for site in element_link_sites(element):
            subject = Subject("hardware", element.name, site.steps)
            records += _unknown_requirement_link(subject, "Hardware element", site, declared_ids)
    return records


def _unknown_requirement_link(
    subject: Subject, label: str, site: LinkSite, declared_ids: AbstractSet[str]
) -> list[ConsistencyIssue]:
    """Check 1 of :func:`check_cross_layer_consistency` on one ``satisfies`` member: its finding, or none.

    A member is refused when it holds a ``:`` (the element form only ``derived_from`` takes), when
    its id names no declared requirement, or when its text after the first ``@`` is not a version.
    *subject* is built at each call site with its layer written out, so the contract's
    ``appliesTo`` can be checked against it.
    """
    if subject.element is None:
        return []  # never: each call site builds *subject* on the named element that holds the member
    value = site.value
    requirement_id, _version, suffix = parse_link(value)
    where = link_site_where(label, subject.element, site)
    bad_suffix = suffix is not None and not is_version(suffix)
    # A member breaks the grammar by its suffix or by the element form, never by an absent id.
    malformed = bad_suffix or is_element_form(value)
    if is_element_form(value):
        problem = "a member holding ':' names no requirement (':' is the element form only `derived_from` takes)"
    elif requirement_id not in declared_ids:
        problem = f"no requirement declares '{requirement_id}'"
    elif bad_suffix:
        problem = f"'{suffix}' after '@' is not a version"
    else:
        return []
    # The grammar only for a member that breaks it, not for one naming an absent requirement.
    grammar = " A member is '<id>@<version>', the version a positive integer, or the bare '<id>'."
    message = f"{where} satisfies '{value}': {problem}.{grammar if malformed else ''}"
    return [
        ConsistencyIssue(
            rule="trace.link-unknown-requirement", message=message, subjects=(subject,), target=requirement_id
        )
    ]


def _composed_check_records(view: ModelView, arxml_version: ArxmlVersion | str | None) -> list[ConsistencyIssue]:
    """The checks after the first of :func:`check_cross_layer_consistency`, run over *view* in the tuple's order."""
    records: list[ConsistencyIssue] = []
    # The finding order the shipped vectors freeze: insert a check only where it moves no
    # frozen finding, and after any check whose finding it abstains on.
    for check in (
        check_connector_consistency,
        # Abstains on an unresolvable endpoint, which the connector pass reports.
        check_composition_consistency,
        check_data_type_references,
        check_data_type_cycles,
        # Before `check_init_conformance`, which abstains on a reference these report.
        check_data_description_elements,
        check_task_consistency,
        check_sequence_consistency,
        # Before the budget pass, which assumes every reference into `hardware/` resolves.
        check_hardware_topology,
        check_resource_budgets,
        check_name_uniqueness,
        check_port_interface_references,
        # Abstains wherever a type does not resolve, which the reference pass reports.
        check_init_conformance,
        # After the port-interface pass, whose finding it defers to for a port with an undeclared interface.
        check_runnable_access,
        check_identifier_conformance,
        check_component_asil,
        check_safety_coexistence,
        check_hardware_capability,
        check_storage_portability,
        check_base_type_native_declarations,
        check_mapping_set_references,
        # Abstains on a mapping set name that resolves to no set.
        check_application_type_mapping,
        check_application_type_double_mapping,  # [constr_1004]'s other half.
        check_operation_events,  # Includes the partial [constr_2000] comparison.
        # In the order the ARXML writer runs their twins, before emission.
        check_port_com_specs,
        check_runnable_symbols,
        check_queued_receivers,
        check_disabled_modes,
        check_queued_reads,
        check_service_composition_ports,
        check_conversion_units,
        # The writer refuses such a text during emission.
        check_xml_representable_text,
        # The writer runs its twin after emission and after its connector check.
        check_name_space_collisions,  # [constr_2508]
        # After the name-space rule, where the writer runs its twin; AUTOSAR 4.2.2 and 4.3.0 require
        # the attribute on every behaviour.
        check_termination_and_restart,
        check_runnable_costs,
        check_requirement_traceability,
        check_inter_runnable_variables,
        check_exclusive_areas,
        check_data_events,
        check_unknown_keys,
        check_implicit_reads,
        check_async_calls,
    ):
        # Both halves, so `consistency_result` is the one place the severity is applied.
        composed = check(view, arxml_version) if check is check_async_calls else check(view)
        records.extend(composed.records)
        records.extend(composed.warnings)
    return records
