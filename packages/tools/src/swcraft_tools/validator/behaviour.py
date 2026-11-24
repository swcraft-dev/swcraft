"""The behavioural value rules of a component and its runnables.

Termination handling (``swc.undeclared-termination-and-restart``), com-specs, runnable symbols, queued
receivers and reads, disabled modes, service composition ports, conversion units, inter-runnable
variables, exclusive areas, implicit reads and asynchronous calls. Each rule has an in-memory
``*_records`` core taking the layers already read, shared by ``check-model`` and the ARXML writer's
guard so the two cannot disagree, and a ``check_*`` wrapper over a model root.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.data_description import DataDescription
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.identifier import check_c_identifier
from swcraft_tools.rendering import render_model_scalar
from swcraft_tools.schema import (
    ApplicationDataType,
    Composition,
    CSInterface,
    DataEvent,
    DisabledMode,
    ExternalInterface,
    Interface,
    MappingSet,
    PointerDataType,
    Port,
    Runnable,
    SRInterface,
    SWComponent,
    TimingEvent,
    Variable,
)
from swcraft_tools.validator.access import accessing_runnables
from swcraft_tools.validator.compatibility import interface_index, is_queued
from swcraft_tools.validator.data_types import (
    application_type_pairings,
    declared_type,
    prototype_type_field,
    prototype_type_ref,
)
from swcraft_tools.validator.description_elements import chain_text, duplicates, implementation_end, stated_unit_text
from swcraft_tools.validator.events import async_call_records
from swcraft_tools.validator.generated_names import variable_access_entries
from swcraft_tools.validator.init_values import first_elements, port_com_spec_records, quoted_list
from swcraft_tools.validator.model_view import ModelInput, as_view
from swcraft_tools.validator.results import ConsistencyIssue, ConsistencyResult, consistency_result
from swcraft_tools.validator.scheduling import event_identity

# ---------------------------------------------------------------------------
# The termination handling
# ---------------------------------------------------------------------------


def components_without_termination_handling(swcs: Sequence[SWComponent]) -> list[SWComponent]:
    """Every component whose behaviour is emitted and that sets no ``handle_termination_and_restart``.

    The predicate of ``rule:swc.undeclared-termination-and-restart``.
    ``SwcInternalBehavior.handleTerminationAndRestart`` has multiplicity 1 in the
    template at R4.2.2 (SWCT ``:22932``) and R4.3.0 (SWCT ``:25469``), the only two
    versions SWCraft writes, so the rule reads no version. The behaviour is emitted
    for every component that is not ``external``; an external stub declaring the
    field is ``rule:swc.external-with-ports``' finding.

    Returns:
        The offending components, in read order.
    """
    return [swc for swc in swcs if not swc.external and swc.handle_termination_and_restart is None]


def termination_handling_message(name: str) -> str:
    """The one sentence every reader of ``rule:swc.undeclared-termination-and-restart`` prints."""
    return (
        f"SWC '{name}' sets no handle_termination_and_restart: every behaviour SWCraft writes carries "
        "HANDLE-TERMINATION-AND-RESTART, which AUTOSAR 4.2.2 and 4.3.0 require (multiplicity 1). Set it to "
        "no_support, can_be_terminated or can_be_terminated_and_restarted."
    )


def check_termination_and_restart(model_root: ModelInput) -> ConsistencyResult:
    """``rule:swc.undeclared-termination-and-restart``: one finding per component.

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with one record per offending component.
    """
    view = as_view(model_root)
    return consistency_result(
        [
            ConsistencyIssue(
                rule="swc.undeclared-termination-and-restart",
                message=termination_handling_message(swc.name),
                subjects=(Subject("swc", swc.name, (field_step("handle_termination_and_restart"),)),),
            )
            for swc in components_without_termination_handling(view.swcs)
        ]
    )


# ---------------------------------------------------------------------------
# The behavioural values
# ---------------------------------------------------------------------------

#: The ``external_dest`` of the stub a disabled mode's port must be typed by:
#: SWCraft declares no mode-switch interface.
MODE_SWITCH_INTERFACE_DEST = "MODE-SWITCH-INTERFACE"


def _declared_sr_interfaces(interfaces: Sequence[Interface]) -> dict[str, SRInterface]:
    """Declared sender/receiver interface name -> interface, **first** declaration winning."""
    index: dict[str, SRInterface] = {}
    for name, interface in interface_index(list(interfaces)).items():
        if isinstance(interface, SRInterface):
            index[name] = interface
    return index


def check_port_com_specs(model_root: ModelInput) -> ConsistencyResult:
    """:func:`port_com_spec_records` over the model at *model_root*."""
    view = as_view(model_root)
    return consistency_result(port_com_spec_records(view.swcs, view.interfaces))


def runnable_symbol_records(swcs: Sequence[SWComponent]) -> list[ConsistencyIssue]:
    """``rule:swc.runnable.symbol-not-c-identifier`` and ``rule:name.duplicate-runnable-symbol``.

    A runnable is written with its ``symbol`` (AUTOSAR's ``RunnableEntity.symbol``,
    its C entry point) when stated, else its ``name``.

    * **not a C identifier** -- a stated ``symbol`` must match
      :data:`~swcraft_tools.identifier.C_IDENTIFIER_PATTERN`, the ``CIdentifier``
      primitive's pattern (GST R4.2.2 Table 4.31, R4.3.0 Table 4.49) that both
      schemas give ``SYMBOL``. One finding per runnable.
    * **duplicate written symbol** -- no two runnables of one component may be
      written with one symbol, compared case-sensitively. ``[constr_2025]``
      (System Template R4.2.2 ``:26114-26118``, R4.3.0 ``:30512-30516``) requires
      the combinations of a runnable's symbol and its component's symbol (the
      System Template's ``AtomicSwComponentType.symbol``, the SWCT's
      ``symbolProps``) to be unique within one ECU instance, and runnables of one
      component share its symbol. One finding per group; a group in which no
      runnable states a symbol is ``rule:name.duplicate-runnable``'s alone.

    Not reached: two components whose runnables share a symbol (the model does
    not carry the component's symbol), and any extent beyond one component.
    External stubs are skipped.
    """
    records: list[ConsistencyIssue] = []
    for swc in swcs:
        if swc.external:
            continue
        element = Subject("swc", swc.name)
        for runnable in swc.runnables:
            if runnable.symbol is None:
                continue
            reason = check_c_identifier(runnable.symbol)
            if reason is not None:
                records.append(
                    ConsistencyIssue(
                        rule="swc.runnable.symbol-not-c-identifier",
                        message=(
                            f"SWC '{swc.name}' runnable '{runnable.name}' states symbol "
                            f"{render_model_scalar(runnable.symbol)}, which {reason}: the symbol is the name of the "
                            "runnable's entry point in C."
                        ),
                        subjects=(
                            element.at(field_step("runnables"), member_step(runnable.name), field_step("symbol")),
                        ),
                    )
                )
        groups: dict[str, list[Runnable]] = {}
        for runnable in swc.runnables:
            written = runnable.symbol if runnable.symbol is not None else runnable.name
            groups.setdefault(written, []).append(runnable)
        for written, group in groups.items():
            if len(group) < 2 or all(runnable.symbol is None for runnable in group):
                continue
            records.append(
                ConsistencyIssue(
                    rule="name.duplicate-runnable-symbol",
                    message=(
                        f"SWC '{swc.name}' runnables {quoted_list([runnable.name for runnable in group])} are "
                        f"written with one symbol {render_model_scalar(written)} (a runnable that states no symbol "
                        "is written with its name): two runnables of one component share the component's symbol, "
                        "so one runnable symbol gives both one entry point, which [constr_2025] forbids. Give each "
                        "runnable a symbol of its own."
                    ),
                    subjects=tuple(
                        element.at(field_step("runnables"), member_step(runnable.name), field_step("symbol"))
                        if runnable.symbol is not None
                        else element.at(field_step("runnables"), member_step(runnable.name))
                        for runnable in group
                    ),
                )
            )
    return records


def check_runnable_symbols(model_root: ModelInput) -> ConsistencyResult:
    """:func:`runnable_symbol_records` over the model at *model_root*."""
    view = as_view(model_root)
    return consistency_result(runnable_symbol_records(view.swcs))


def queued_receiver_records(swcs: Sequence[SWComponent], interfaces: Sequence[Interface]) -> list[ConsistencyIssue]:
    """``rule:swc.port.queued-receiver-without-queue-length``: one finding per (port, queued element).

    A component's ``require`` port whose declared sender/receiver interface
    declares a data element with ``sw_impl_policy: queued``, and whose
    ``com_specs`` give that element no ``queue_length``. ``[TPS_SWCT_01219]``
    requires a ``QueuedReceiverComSpec`` for a queued element, and its
    ``queueLength`` has multiplicity 1 with no default in the standard or the
    XSD (Table 4.62 of R4.2.2), so the writer cannot write one without inventing
    it. Not reached: an external stub's port (it declares no interface) and a
    composition's port (it states no queue behaviour).
    """
    declared = _declared_sr_interfaces(interfaces)
    records: list[ConsistencyIssue] = []
    for swc in swcs:
        if swc.external:
            continue
        for port in swc.ports:
            if port.direction != "require" or port.interface not in declared:
                continue
            lengths = {entry.data_element: entry.queue_length for entry in port.com_specs}
            for element in first_elements(declared[port.interface]).values():
                if not is_queued(element) or lengths.get(element.name) is not None:
                    continue
                records.append(
                    ConsistencyIssue(
                        rule="swc.port.queued-receiver-without-queue-length",
                        message=(
                            f"SWC '{swc.name}' require port '{port.name}' receives queued data element "
                            f"'{element.name}' of interface '{port.interface}' and states no queue_length for "
                            "it: a queued receiver com-spec carries QUEUE-LENGTH (multiplicity 1), which neither "
                            "AUTOSAR nor SWCraft defaults. Add a com_specs entry for the element with a "
                            "queue_length."
                        ),
                        subjects=(
                            Subject("swc", swc.name, (field_step("ports"), member_step(port.name))),
                            Subject(
                                "interfaces",
                                port.interface,
                                (field_step("data_elements"), member_step(element.name), field_step("sw_impl_policy")),
                            ),
                        ),
                    )
                )
    return records


def check_queued_receivers(model_root: ModelInput) -> ConsistencyResult:
    """:func:`queued_receiver_records` over the model at *model_root*."""
    view = as_view(model_root)
    return consistency_result(queued_receiver_records(view.swcs, view.interfaces))


def _disabled_mode_problem(swc: SWComponent, mode: DisabledMode, interfaces: Mapping[str, Interface]) -> str | None:
    """Why *mode* cannot be written as an instance reference into *swc*, or ``None`` when it can."""
    port = next((candidate for candidate in swc.ports if candidate.name == mode.port), None)
    if port is None:
        return f"the component declares no port '{mode.port}'"
    if port.direction != "require":
        return f"port '{mode.port}' is a provide port, and a mode arrives through a require port"
    interface = interfaces.get(port.interface) if port.interface is not None else None
    if not isinstance(interface, ExternalInterface) or interface.external_dest != MODE_SWITCH_INTERFACE_DEST:
        named = "declares no interface" if port.interface is None else f"is typed by '{port.interface}'"
        return (
            f"port '{mode.port}' {named}, which is not an external interface stub whose external_dest is "
            f"{MODE_SWITCH_INTERFACE_DEST}"
        )
    parent = mode.mode_group_ref.rpartition("/")[0]
    if parent != interface.external_ref:
        return (
            f"mode_group_ref '{mode.mode_group_ref}' does not lie directly under the external_ref "
            f"'{interface.external_ref}' of interface '{port.interface}', which aggregates the mode declaration "
            "group prototype"
        )
    return None


def _disabled_event_phrase(event: TimingEvent | DataEvent) -> str:
    """How a disabled-mode finding names the event its mode disables: one arm per kind that declares the field."""
    if isinstance(event, TimingEvent):
        return f"timing event (period {event.period}s)"
    if isinstance(event, DataEvent):
        return f"data-received event (port '{event.port}' element '{event.element}')"
    raise TypeError(f"an event kind the disabled-mode phrase has not been taught: {type(event).__name__}")


def disabled_mode_records(swcs: Sequence[SWComponent], interfaces: Sequence[Interface]) -> list[ConsistencyIssue]:
    """``rule:swc.timing_event.disabled-mode-port``: one finding per disabled mode that cannot be written.

    Despite its id, the rule reaches every event that declares ``disabled_modes``:
    a timing event and a data-received event (``RTE-EVENT``'s
    ``DISABLED-MODE-IREFS`` is every event's place).

    ``RTEEvent.disabledMode`` is an ``R-MODE-IN-ATOMIC-SWC-INSTANCE-REF`` whose
    ``CONTEXT-PORT-REF`` is a required port (XSD 4.2.2 ``:51822``). The port must
    be a ``require`` port of the same component typed by an external interface
    stub whose ``external_dest`` is ``MODE-SWITCH-INTERFACE``, and
    ``mode_group_ref`` must lie directly under that stub's ``external_ref``. The
    references' grammar is ``rule:swc.disabled_mode.reference-form``'s, at load.

    Not reached: ``mode_ref`` is written verbatim and never resolved (no model
    element holds the ``MODE-DECLARATION-GROUP``'s path).
    """
    declared = interface_index(list(interfaces))
    records: list[ConsistencyIssue] = []
    for swc in swcs:
        if swc.external:
            continue
        for runnable in swc.runnables:
            for event in runnable.events:
                if not isinstance(event, (TimingEvent, DataEvent)):
                    continue  # an init or an operation event declares no disabled mode
                for mode in event.disabled_modes:
                    problem = _disabled_mode_problem(swc, mode, declared)
                    if problem is None:
                        continue
                    records.append(
                        ConsistencyIssue(
                            rule="swc.timing_event.disabled-mode-port",
                            message=(
                                f"SWC '{swc.name}' runnable '{runnable.name}' {_disabled_event_phrase(event)} "
                                f"is disabled in mode '{mode.mode_ref}' on port '{mode.port}', but {problem}."
                            ),
                            subjects=(
                                Subject(
                                    "swc",
                                    swc.name,
                                    (
                                        field_step("runnables"),
                                        member_step(runnable.name),
                                        field_step("events"),
                                        member_step(event_identity(event)),
                                    ),
                                ),
                            ),
                        )
                    )
    return records


def check_disabled_modes(model_root: ModelInput) -> ConsistencyResult:
    """:func:`disabled_mode_records` over the model at *model_root*."""
    view = as_view(model_root)
    return consistency_result(disabled_mode_records(view.swcs, view.interfaces))


def queued_read_records(swcs: Sequence[SWComponent], interfaces: Sequence[Interface]) -> list[ConsistencyIssue]:
    """``rule:swc.access.queued-read-by-value``: one finding per ``by: value`` read of a queued element.

    ``[constr_1277]`` (SWCT R4.2.2 ``:25448``, R4.3.0 ``:28306``): a data
    receive point by value may not refer to a queued variable data prototype. A
    read that does not resolve is skipped: the ``swc.access.*`` rules report it.
    """
    declared = _declared_sr_interfaces(interfaces)
    records: list[ConsistencyIssue] = []
    for swc in swcs:
        if swc.external:
            continue
        ports: dict[str, Port] = {}
        for declared_port in swc.ports:
            ports.setdefault(declared_port.name, declared_port)
        for runnable in swc.runnables:
            for read in runnable.reads:
                if read.by != "value":
                    continue
                port = ports.get(read.port)
                if port is None or port.direction != "require" or port.interface not in declared:
                    continue
                element = first_elements(declared[port.interface]).get(read.element)
                if element is None or not is_queued(element):
                    continue
                records.append(
                    ConsistencyIssue(
                        rule="swc.access.queued-read-by-value",
                        message=(
                            f"SWC '{swc.name}' runnable '{runnable.name}' reads queued data element "
                            f"'{read.element}' through port '{read.port}' by value; a queued element is received "
                            "by argument only. Remove `by: value` from the read."
                        ),
                        subjects=(
                            Subject(
                                "swc",
                                swc.name,
                                (
                                    field_step("runnables"),
                                    member_step(runnable.name),
                                    field_step("reads"),
                                    member_step({"port": read.port, "element": read.element}),
                                ),
                            ),
                            Subject(
                                "interfaces",
                                port.interface,
                                (field_step("data_elements"), member_step(element.name), field_step("sw_impl_policy")),
                            ),
                        ),
                    )
                )
    return records


def check_queued_reads(model_root: ModelInput) -> ConsistencyResult:
    """:func:`queued_read_records` over the model at *model_root*."""
    view = as_view(model_root)
    return consistency_result(queued_read_records(view.swcs, view.interfaces))


def service_composition_port_records(
    compositions: Sequence[Composition], interfaces: Sequence[Interface]
) -> list[ConsistencyIssue]:
    """``rule:composition.port-service-interface``: one finding per composition port on a service interface.

    ``[constr_1174]`` (SWCT R4.2.2 ``:4016``, R4.3.0 ``:4369``): a composition
    shall not own a port typed by a port interface whose ``isService`` is
    ``true``. Skipped: an external interface stub (no ``is_service``) and an
    undeclared interface (``composition.port-undeclared-interface``'s).
    """
    declared = interface_index(list(interfaces))
    records: list[ConsistencyIssue] = []
    for composition in compositions:
        for port in composition.ports:
            interface = declared.get(port.interface)
            if not isinstance(interface, (SRInterface, CSInterface)) or not interface.is_service:
                continue
            records.append(
                ConsistencyIssue(
                    rule="composition.port-service-interface",
                    message=(
                        f"Composition '{composition.name}' port '{port.name}' is typed by interface "
                        f"'{port.interface}', which declares is_service: true; a composition may not own a port "
                        "typed by a service interface."
                    ),
                    subjects=(
                        Subject(
                            "compositions",
                            composition.name,
                            (field_step("ports"), member_step(port.name), field_step("interface")),
                        ),
                        Subject("interfaces", port.interface, (field_step("is_service"),)),
                    ),
                )
            )
    return records


def check_service_composition_ports(model_root: ModelInput) -> ConsistencyResult:
    """:func:`service_composition_port_records` over the model at *model_root*."""
    view = as_view(model_root)
    return consistency_result(service_composition_port_records(view.compositions, view.interfaces))


def conversion_unit_records(description: DataDescription) -> list[ConsistencyIssue]:
    """``rule:data-type.conversion-without-unit``: one finding per application type with a conversion and no unit.

    ``[constr_1175]`` (SWCT R4.2.2 ``:16954``, R4.3.0 ``:18398``): a compu
    method shall refer to a unit unless its category is ``TEXTTABLE``,
    ``BITFIELD_TEXTTABLE`` or ``IDENTICAL``. The writer writes every declared
    ``conversion`` (``conversion: {}`` included) as a ``LINEAR`` method, so it
    needs the type's unit, which the writer cannot invent: its inline ``unit`` or its ``unit_ref``
    (:func:`stated_unit_text`). A blank ``unit`` is no unit. Not reached: a
    ``unit_ref`` naming no unit (``rule:data-type.unknown-unit``'s) and a
    conversion by reference, whose compu method names its own unit.
    """
    records: list[ConsistencyIssue] = []
    for data_type in description.data_types:
        if not isinstance(data_type, ApplicationDataType) or data_type.conversion is None:
            continue
        # The writer emits no `UNIT` for a blank `unit`, so the compu method would refer to none.
        if stated_unit_text(data_type, description) != "":
            continue
        records.append(
            ConsistencyIssue(
                rule="data-type.conversion-without-unit",
                message=(
                    f"Application type '{data_type.name}' declares a conversion and no unit: SWCraft writes "
                    "the conversion as a LINEAR compu method, which must refer to a unit. Declare the type's unit."
                ),
                subjects=(
                    Subject("application_types", data_type.name, (field_step("unit"),)),
                    Subject("application_types", data_type.name, (field_step("conversion"),)),
                ),
            )
        )
    return records


def check_conversion_units(model_root: ModelInput) -> ConsistencyResult:
    """:func:`conversion_unit_records` over the model at *model_root*."""
    view = as_view(model_root)
    return consistency_result(conversion_unit_records(view.data_description))


# ---------------------------------------------------------------------------
# check_inter_runnable_variables
# ---------------------------------------------------------------------------


def _variable_pointer_chain(
    swc: SWComponent,
    variable: Variable,
    type_map: DataDescription,
    set_map: Mapping[str, MappingSet],
    duplicated_sets: frozenset[str],
) -> tuple[tuple[str, ...], str | None] | None:
    """``(chain, application)`` when *variable* is typed by a pointer, else ``None``.

    ``[constr_1296]``: the variable is not typed by, or mapped to, an
    implementation type of category ``DATA_REFERENCE`` *after potential
    indirections via TYPE_REFERENCE are resolved*. An application-typed variable
    is judged through the one implementation type its component's mapping sets
    pair it with, and *application* is then its application type. Not judged
    (another rule owns it, or the category is unknown): an unresolved type or a
    broken or cyclic chain; an application type paired with none or several
    implementation types; a component naming a duplicated or undeclared set; an
    external data type stub.
    """
    if variable.implementation_type is not None:
        end = implementation_end(variable.implementation_type, type_map)
        return (end[0], None) if end is not None and isinstance(end[1], PointerDataType) else None
    if any(name in duplicated_sets or name not in set_map for name in swc.data_type_mappings):
        return None
    onto = application_type_pairings(swc.data_type_mappings, set_map).get(variable.application_type or "", {})
    if len(onto) != 1:
        return None
    end = implementation_end(next(iter(onto)), type_map)
    if end is None or not isinstance(end[1], PointerDataType):
        return None
    return end[0], variable.application_type


def inter_runnable_variable_records(
    swcs: Sequence[SWComponent], type_map: DataDescription, mapping_sets: Sequence[MappingSet]
) -> list[ConsistencyIssue]:
    """The four rules over the inter-runnable variables.

    Per component, in this order:

    1. ``swc.variable.undeclared-type`` -- a variable's ``application_type``
       names no declared application type, or its ``implementation_type`` no
       declared implementation type (an external data type stub counts as
       declared).
    2. ``swc.variable.pointer-type`` -- ``[constr_1296]``, judged by
       :func:`_variable_pointer_chain`.
    3. ``swc.variable-access.unknown-variable`` -- ``[constr_2026]``: an entry of
       a runnable's ``reads_variables`` or ``writes_variables`` naming no
       variable of **its own** component. One finding per missing name, carrying
       every entry of every runnable that names it.
    4. ``swc.variable.read-without-init`` -- a **warning**: a variable some
       runnable of its component reads and that states no ``init``
       (``[TPS_SWCT_01522]`` leaves such a read undefined when it comes before
       every write). One finding per variable, carrying every reading entry.

    External component stubs are skipped; a duplicated name or entry is refused
    at load. The order of execution is not read, so the warning is a bound, not a
    proof: a provably earlier write, or a variable typed by an external data type
    stub, is still warned about.

    Returns:
        One record per finding; the warning is classed by :func:`consistency_result`.
    """
    set_map: dict[str, MappingSet] = {}
    for mapping_set in mapping_sets:
        set_map.setdefault(mapping_set.name, mapping_set)
    duplicated_sets = frozenset(duplicates([mapping_set.name for mapping_set in mapping_sets]))

    records: list[ConsistencyIssue] = []
    for swc in swcs:
        if swc.external:
            continue
        owner = Subject("swc", swc.name)
        declared = {variable.name: variable for variable in swc.variables}

        for variable in swc.variables:
            variable_type, application = prototype_type_ref(variable)
            type_field = prototype_type_field(variable)
            at = owner.at(field_step("variables"), member_step(variable.name))
            if declared_type(type_map, variable_type, application=application) is None:
                records.append(
                    ConsistencyIssue(
                        rule="swc.variable.undeclared-type",
                        message=(
                            f"SWC '{swc.name}' variable '{variable.name}' references undeclared "
                            f"{'application' if application else 'implementation'} type '{variable_type}'."
                        ),
                        subjects=(at.at(field_step(type_field)),),
                        target=variable_type,
                    )
                )
                continue
            found = _variable_pointer_chain(swc, variable, type_map, set_map, duplicated_sets)
            if found is not None:
                chain, mapped_from = found
                reached = (
                    f"is typed by '{chain[0]}', which"
                    + (" is a pointer" if len(chain) == 1 else f" resolves through {chain_text(chain)} to a pointer")
                    if mapped_from is None
                    else f"is typed by application type '{mapped_from}', which the component's mapping sets pair "
                    f"with '{chain[0]}'"
                    + ("" if len(chain) == 1 else f", resolving through {chain_text(chain)}")
                    + ", a pointer"
                )
                records.append(
                    ConsistencyIssue(
                        rule="swc.variable.pointer-type",
                        message=(
                            f"SWC '{swc.name}' variable '{variable.name}' {reached}; an inter-runnable variable "
                            "is never typed by, or mapped to, an implementation type of category DATA_REFERENCE "
                            "([constr_1296]). Type the variable by a value, a record or an array."
                        ),
                        subjects=(
                            at.at(field_step(type_field)),
                            Subject("implementation_types", chain[-1]),
                        ),
                    )
                )

        unresolved: dict[str, list[tuple[str, str]]] = {}
        readers: dict[str, list[str]] = {}
        for runnable in swc.runnables:
            for list_name, variable_name, _stated in variable_access_entries(runnable):
                if variable_name not in declared:
                    unresolved.setdefault(variable_name, []).append((runnable.name, list_name))
                elif list_name == "reads_variables":
                    readers.setdefault(variable_name, []).append(runnable.name)

        for variable_name, referrers in unresolved.items():
            runnable_names = list(dict.fromkeys(name for name, _list in referrers))
            count = len(referrers)
            records.append(
                ConsistencyIssue(
                    rule="swc.variable-access.unknown-variable",
                    message=(
                        f"SWC '{swc.name}' declares no variable '{variable_name}', which "
                        f"{accessing_runnables(runnable_names)} ({count} {'entry' if count == 1 else 'entries'}); "
                        "a runnable accesses only a variable of its own component ([constr_2026])."
                    ),
                    subjects=tuple(
                        owner.at(
                            field_step("runnables"),
                            member_step(runnable_name),
                            field_step(list_name),
                            member_step(variable_name),
                            field_step("variable"),
                        )
                        for runnable_name, list_name in referrers
                    ),
                    target=f"{swc.name}.{variable_name}",
                )
            )

        for variable in swc.variables:
            reading = readers.get(variable.name)
            if variable.init is not None or not reading:
                continue
            records.append(
                ConsistencyIssue(
                    rule="swc.variable.read-without-init",
                    message=(
                        f"SWC '{swc.name}' variable '{variable.name}' states no init, and "
                        f"{'runnable' if len(reading) == 1 else 'runnables'} {quoted_list(reading)} "
                        f"{'reads' if len(reading) == 1 else 'read'} it: AUTOSAR leaves the value undefined when a "
                        "read comes before every write ([TPS_SWCT_01522]), and the model states no order of "
                        "execution. State the variable's init."
                    ),
                    subjects=(
                        owner.at(field_step("variables"), member_step(variable.name), field_step("init")),
                        *(
                            owner.at(
                                field_step("runnables"),
                                member_step(runnable_name),
                                field_step("reads_variables"),
                                member_step(variable.name),
                            )
                            for runnable_name in reading
                        ),
                    ),
                )
            )
    return records


def check_inter_runnable_variables(model_root: ModelInput) -> ConsistencyResult:
    """Check every component's inter-runnable variables and its runnables' accesses to them.

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult; the read-without-init warning is in ``warnings``.
    """
    view = as_view(model_root)
    return consistency_result(inter_runnable_variable_records(view.swcs, view.data_description, view.mapping_sets))


# ---------------------------------------------------------------------------
# The exclusive areas
# ---------------------------------------------------------------------------


def exclusive_area_entries(runnable: Runnable) -> list[tuple[str, str]]:
    """``(list, area)`` for every exclusive area entry of *runnable*.

    ``can_enter`` first, then ``runs_inside`` -- the order the XSD's ``group
    EXECUTABLE-ENTITY`` gives their wrappers. Each list is read by its own
    attribute, never ``getattr``, so the coverage oracle sees the read.
    """
    return [
        *(("can_enter", entry.area) for entry in runnable.can_enter),
        *(("runs_inside", entry.area) for entry in runnable.runs_inside),
    ]


def exclusive_area_records(swcs: Sequence[SWComponent]) -> list[ConsistencyIssue]:
    """``rule:swc.exclusive-area.unknown-area``, the exclusive areas' one cross-element rule.

    An entry of a runnable's ``can_enter`` or ``runs_inside`` naming no
    exclusive area of **its own** component: the InternalBehavior's
    ``exclusiveArea`` *is local to the component* (its class table, R4.2.2 and
    R4.3.0). One finding per missing name, carrying every entry naming it, its
    target ``{swc}.{area}``.

    Silence by design: an unused area, an area one runnable uses, one area in
    both roles on one runnable (no ``constr_`` of either TPS edition forbids it),
    and all of an area's runnables in one task. External stubs are skipped. A
    duplicated name or entry is refused at load.

    Not reached: whether an area protects any datum, the order in which areas
    nest, and which runnables can block which. That an area's runnables never
    span cores rests on ``task.instance-in-multiple-partitions``.
    """
    records: list[ConsistencyIssue] = []
    for swc in swcs:
        if swc.external:
            continue
        owner = Subject("swc", swc.name)
        declared = {area.name for area in swc.exclusive_areas}
        unresolved: dict[str, list[tuple[str, str]]] = {}
        for runnable in swc.runnables:
            for list_name, area_name in exclusive_area_entries(runnable):
                if area_name not in declared:
                    unresolved.setdefault(area_name, []).append((runnable.name, list_name))
        for area_name, referrers in unresolved.items():
            runnable_names = list(dict.fromkeys(name for name, _list in referrers))
            count = len(referrers)
            naming = accessing_runnables(runnable_names, ("names", "name"))
            records.append(
                ConsistencyIssue(
                    rule="swc.exclusive-area.unknown-area",
                    message=(
                        f"SWC '{swc.name}' declares no exclusive area '{area_name}', which {naming} in "
                        f"can_enter or runs_inside ({count} {'entry' if count == 1 else 'entries'}); an exclusive "
                        "area is local to its component, so a runnable enters or runs inside only an area its "
                        "own component declares in exclusive_areas."
                    ),
                    subjects=tuple(
                        owner.at(
                            field_step("runnables"),
                            member_step(runnable_name),
                            field_step(list_name),
                            member_step(area_name),
                            field_step("area"),
                        )
                        for runnable_name, list_name in referrers
                    ),
                    target=f"{swc.name}.{area_name}",
                )
            )
    return records


def check_exclusive_areas(model_root: ModelInput) -> ConsistencyResult:
    """Check that every ``can_enter`` and ``runs_inside`` entry names an area of its component.

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True only when every entry resolves.
    """
    view = as_view(model_root)
    return consistency_result(exclusive_area_records(view.swcs))


# ---------------------------------------------------------------------------
# Implicit access and asynchronous calls
# ---------------------------------------------------------------------------


def implicit_read_records(swcs: Sequence[SWComponent], interfaces: Sequence[Interface]) -> list[ConsistencyIssue]:
    """The implicit read's two rules: one finding per offending ``by: implicit`` read.

    1. ``swc.access.queued-implicit-read`` (``[constr_2020]``): the element is
       queued. A read that does not resolve is skipped: the ``swc.access.*``
       rules report it.
    2. ``swc.access.implicit-read-enable-update`` (``[constr_1103]``): the
       port's ``com_specs`` entry for the element states ``enable_update:
       true``, whatever the port's interface.

    Not reached: an implicit **write** (no rule of either edition forbids one of
    a queued element). External stubs are skipped.

    Returns:
        One record per finding, the queued finding before the com-spec one of a
        read that meets both.
    """
    declared = _declared_sr_interfaces(interfaces)
    records: list[ConsistencyIssue] = []
    for swc in swcs:
        if swc.external:
            continue
        ports: dict[str, Port] = {}
        for declared_port in swc.ports:
            ports.setdefault(declared_port.name, declared_port)
        for runnable in swc.runnables:
            for read in runnable.reads:
                if read.by != "implicit":
                    continue
                read_subject = Subject(
                    "swc",
                    swc.name,
                    (
                        field_step("runnables"),
                        member_step(runnable.name),
                        field_step("reads"),
                        member_step({"port": read.port, "element": read.element}),
                    ),
                )
                port = ports.get(read.port)
                if port is None:
                    continue
                if port.direction == "require" and port.interface in declared:
                    element = first_elements(declared[port.interface]).get(read.element)
                    if element is not None and is_queued(element):
                        records.append(
                            ConsistencyIssue(
                                rule="swc.access.queued-implicit-read",
                                message=(
                                    f"SWC '{swc.name}' runnable '{runnable.name}' reads queued data element "
                                    f"'{read.element}' through port '{read.port}' implicitly; an implicit read "
                                    "takes one value before the runnable starts, and a queued element has no one "
                                    "value to take ([constr_2020]). Remove `by: implicit` from the read."
                                ),
                                subjects=(
                                    read_subject.at(field_step("by")),
                                    Subject(
                                        "interfaces",
                                        port.interface,
                                        (
                                            field_step("data_elements"),
                                            member_step(element.name),
                                            field_step("sw_impl_policy"),
                                        ),
                                    ),
                                ),
                            )
                        )
                for entry in port.com_specs:
                    if entry.data_element != read.element or entry.enable_update is not True:
                        continue
                    records.append(
                        ConsistencyIssue(
                            rule="swc.access.implicit-read-enable-update",
                            message=(
                                f"SWC '{swc.name}' runnable '{runnable.name}' reads data element "
                                f"'{read.element}' through port '{read.port}' implicitly, and the port's com_specs "
                                "entry for it states `enable_update: true`; an implicit read has no update flag "
                                "to ask ([constr_1103]). Remove `by: implicit` from the read or `enable_update` "
                                "from the com_specs entry."
                            ),
                            subjects=(
                                read_subject.at(field_step("by")),
                                Subject(
                                    "swc",
                                    swc.name,
                                    (
                                        field_step("ports"),
                                        member_step(port.name),
                                        field_step("com_specs"),
                                        member_step(entry.data_element),
                                        field_step("enable_update"),
                                    ),
                                ),
                            ),
                        )
                    )
                    break
    return records


def check_implicit_reads(model_root: ModelInput) -> ConsistencyResult:
    """Check every ``by: implicit`` read against its element and its port's com-spec.

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True only when no implicit read is of a queued element or
        of an element its port's com-spec states ``enable_update: true`` for.
    """
    view = as_view(model_root)
    return consistency_result(implicit_read_records(view.swcs, view.interfaces))


def check_async_calls(model_root: ModelInput, arxml_version: ArxmlVersion | str | None = None) -> ConsistencyResult:
    """Check every asynchronous call and ``call_returned`` event of each component.

    Args:
        model_root: The model root, or a view of it.
        arxml_version: the AUTOSAR version the project generates (default 4.2.2).

    Returns:
        ConsistencyResult over the seven rules at that version.
    """
    view = as_view(model_root)
    return consistency_result(async_call_records(view.swcs, arxml_version))


#: The one version-aware rule of :func:`async_call_records`, which both importers single out.
ASYNC_WITHOUT_RESULT = "swc.call.async-without-result"


def async_call_subject(record: ConsistencyIssue) -> tuple[str, str, str]:
    """``(runnable, port, operation)`` of the one call an :data:`ASYNC_WITHOUT_RESULT` *record* is about."""
    (subject,) = record.subjects
    _runnables, runnable, _calls, call = (step.value for step in subject.path)
    # The call's identity is the `{port, operation}` member step `_call_path` builds, in that order.
    port, operation = call.values()
    return runnable, port, operation
