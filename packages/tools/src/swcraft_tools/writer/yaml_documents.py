"""Each model element's YAML document: the dict a writer dumps for it.

One ``*_to_dict`` function per element kind and the small helpers they share. The key order each function builds
is the key order of the written file, stated here once, by hand, for every writer that dumps the element:
``mutations.py``'s create and add functions, the importers and the component exchange.

Two rules hold throughout. Keys follow the schema's ``model_fields`` order, so the result equals what the generic
:func:`~swcraft_tools.writer.elements.write_element` writes. An optional field is omitted at its default (never
written as ``null``: an absent value and a written one are different statements), so a file written before the
field existed round-trips byte-identically. Because the shapes are hand-written, a field a function forgets is
dropped in silence by every caller; each field therefore has one visible site here for readers and the coverage
oracle.

This module imports only ``schema`` and ``typing``; it has no dependency inside the writer package.
"""

from __future__ import annotations

from typing import Any

from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    CallReturnedEvent,
    Composition,
    CompositionMember,
    Connector,
    ConnectorEndpoint,
    Core,
    CSOperation,
    DataElement,
    DataEvent,
    DataReadRef,
    DataWriteRef,
    EnumDataType,
    ExclusiveArea,
    ExternalDataType,
    Hardware,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    InitEvent,
    MappingSet,
    Memory,
    OperationCallRef,
    OperationEvent,
    Partition,
    PointerDataType,
    PortComSpec,
    RecordDataType,
    ReferenceDataType,
    Requirement,
    Runnable,
    Sequence,
    SWComponent,
    Task,
    TimingEvent,
    ValueDataType,
    Variable,
)


def data_element_to_dict(element: DataElement) -> dict[str, Any]:
    """Return one sender-receiver data element in this module's dump shape."""
    d: dict[str, Any] = {"name": element.name}
    if element.application_type is not None:
        d["application_type"] = element.application_type
    else:
        d["implementation_type"] = element.implementation_type
    if element.init is not None:
        d["init"] = element.init
    # `None` is not `QM`: an absent level is omitted.
    if element.asil is not None:
        d["asil"] = element.asil
    if element.description:
        d["description"] = element.description
    # An absent policy is the standard's `standard` and a written one is a
    # statement, so `null` is never written for either.
    if element.sw_impl_policy is not None:
        d["sw_impl_policy"] = element.sw_impl_policy
    if element.sw_calibration_access is not None:
        d["sw_calibration_access"] = element.sw_calibration_access
    return d


def cs_operation_to_dict(operation: CSOperation) -> dict[str, Any]:
    """Return one client-server operation with its arguments in this module's dump shape."""
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


#: The class a partition declaring no ``trust`` holds, read off the schema rather
#: than copied: what :func:`hardware_to_dict` leaves unwritten.
_DEFAULT_PARTITION_TRUST: str = Partition.model_fields["trust"].default


def swc_to_dict(swc: SWComponent) -> dict[str, Any]:
    """Convert SWComponent to a YAML-serialisable dict."""
    d: dict[str, Any] = {"name": swc.name, "type": swc.type}
    if swc.description:
        d["description"] = swc.description
    # `external_ref` and `external_dest` are written whenever they carry a value,
    # not only when `external` is true, so a half-filled stub survives a
    # read/write cycle instead of being silently completed or emptied.
    if swc.external:
        d["external"] = True
    if swc.external_ref:
        d["external_ref"] = swc.external_ref
    if swc.external_dest:
        d["external_dest"] = swc.external_dest
    d["ports"] = [
        {
            "name": p.name,
            "direction": p.direction,
            # An external stub's port declares no interface.
            **({"interface": p.interface} if p.interface is not None else {}),
            **({"observed_interface": p.observed_interface} if p.observed_interface is not None else {}),
            **({"description": p.description} if p.description else {}),
            # Copied rather than aliased: every caller feeds this dict straight
            # back through `SWComponent.model_validate`.
            **({"measures": dict(p.measures)} if p.measures else {}),
            **({"com_specs": [_port_com_spec_to_dict(entry) for entry in p.com_specs]} if p.com_specs else {}),
            **({"satisfies": list(p.satisfies)} if p.satisfies else {}),
        }
        for p in swc.ports
    ]
    d["runnables"] = [runnable_to_dict(r) for r in swc.runnables]
    if swc.variables:
        d["variables"] = [_variable_to_dict(variable) for variable in swc.variables]
    if swc.exclusive_areas:
        d["exclusive_areas"] = [_exclusive_area_to_dict(area) for area in swc.exclusive_areas]
    if swc.memory:
        d["memory"] = dict(swc.memory)
    # An absent level is a different statement from a written `QM`.
    if swc.asil is not None:
        d["asil"] = swc.asil
    if swc.handle_termination_and_restart is not None:
        d["handle_termination_and_restart"] = swc.handle_termination_and_restart
    if swc.data_type_mappings:
        d["data_type_mappings"] = list(swc.data_type_mappings)
    # `None` is *derived*.
    if swc.behavior_name is not None:
        d["behavior_name"] = swc.behavior_name
    return _linked(placed(d, swc.package), swc.satisfies)


def _variable_to_dict(variable: Variable) -> dict[str, Any]:
    """Return one inter-runnable variable in ``schema.Variable`` order.

    Only the one type field that is set is written: a ``null`` other would be refused by the exactly-one validator
    on the way back in.
    """
    d: dict[str, Any] = {"name": variable.name, "access": variable.access}
    if variable.application_type is not None:
        d["application_type"] = variable.application_type
    if variable.implementation_type is not None:
        d["implementation_type"] = variable.implementation_type
    if variable.init is not None:
        d["init"] = variable.init
    if variable.description:
        d["description"] = variable.description
    if variable.sw_calibration_access is not None:
        d["sw_calibration_access"] = variable.sw_calibration_access
    return d


def _exclusive_area_to_dict(area: ExclusiveArea) -> dict[str, Any]:
    """Return one exclusive area in ``schema.ExclusiveArea`` order."""
    d: dict[str, Any] = {"name": area.name}
    if area.description:
        d["description"] = area.description
    return d


def _port_com_spec_to_dict(entry: PortComSpec) -> dict[str, Any]:
    """Return one ``com_specs`` entry in ``schema.PortComSpec`` order.

    ``None`` is *the model is silent*, a different statement from a written ``false`` or ``0``, so it is never
    written. Each attribute is spelled out rather than looped over by name, so every read is visible.
    """
    d: dict[str, Any] = {"data_element": entry.data_element}
    if entry.enable_update is not None:
        d["enable_update"] = entry.enable_update
    if entry.alive_timeout is not None:
        d["alive_timeout"] = entry.alive_timeout
    if entry.handle_never_received is not None:
        d["handle_never_received"] = entry.handle_never_received
    if entry.handle_timeout_type is not None:
        d["handle_timeout_type"] = entry.handle_timeout_type
    if entry.handle_out_of_range is not None:
        d["handle_out_of_range"] = entry.handle_out_of_range
    if entry.uses_end_to_end_protection is not None:
        d["uses_end_to_end_protection"] = entry.uses_end_to_end_protection
    if entry.queue_length is not None:
        d["queue_length"] = entry.queue_length
    if entry.init is not None:
        d["init"] = entry.init
    return d


def composition_to_dict(composition: Composition) -> dict[str, Any]:
    """Convert a Composition to a YAML-serialisable dict.

    Held to an equality: it must produce **exactly** what
    :func:`~swcraft_tools.writer.elements.write_element` produces for the same element, which
    ``test_writer_compositions.py`` compares over a fully populated composition.

    The contract's omissions are restated here because this serializer does not read it: ``description`` at ``""``,
    ``root`` at ``False`` and ``satisfies`` while empty are dropped, while ``ports``, ``members`` and ``delegations``
    are written **at their empty default** (the ``Task.runnables`` rule, not a falsy one).
    """
    d: dict[str, Any] = {"name": composition.name, "title": composition.title}
    if composition.description:
        d["description"] = composition.description
    if composition.root:
        d["root"] = True
    d["ports"] = [
        {
            "name": port.name,
            "direction": port.direction,
            "interface": port.interface,
            **({"description": port.description} if port.description else {}),
        }
        for port in composition.ports
    ]
    d["members"] = [_composition_member_to_dict(member) for member in composition.members]
    d["delegations"] = [
        {
            "name": delegation.name,
            "port": delegation.port,
            "member": delegation.member,
            "member_port": delegation.member_port,
            **({"description": delegation.description} if delegation.description else {}),
        }
        for delegation in composition.delegations
    ]
    return _linked(placed(d, composition.package), composition.satisfies)


def _composition_member_to_dict(member: CompositionMember) -> dict[str, Any]:
    """Return one member, carrying whichever of the two type fields is set.

    Writing both would put a ``null`` into the file that :meth:`CompositionMember.exactly_one_type_field` refuses on
    the way back in.
    """
    typed = {"type": member.type} if member.type is not None else {"composition": member.composition}
    d: dict[str, Any] = {"name": member.name, **typed}
    if member.description:
        d["description"] = member.description
    return d


def requirement_to_dict(req: Requirement) -> dict[str, Any]:
    """Convert a Requirement to a YAML-serialisable dict.

    ``version`` and ``source`` are required and always written; ``derived_from``, ``asil``, the provenance
    (``document``, ``baseline``, ``revision``) and ``stub`` only when set.
    """
    d: dict[str, Any] = {
        "id": req.id,
        "title": req.title,
        "description": req.description,
        "req_type": req.req_type,
        "version": req.version,
        "source": req.source,
    }
    if req.derived_from:
        d["derived_from"] = list(req.derived_from)
    if req.asil is not None:
        d["asil"] = req.asil
    if req.document is not None:
        d["document"] = req.document
    if req.baseline is not None:
        d["baseline"] = req.baseline
    if req.revision is not None:
        d["revision"] = req.revision
    if req.stub:
        d["stub"] = True
    return d


def _endpoint_to_dict(endpoint: ConnectorEndpoint) -> dict[str, Any]:
    """Return one endpoint, carrying whichever of ``swc`` and ``composition`` is set.

    Writing both would put a ``null`` into the file that the exactly-one validator refuses on the way back in.
    """
    named = {"swc": endpoint.swc} if endpoint.swc is not None else {"composition": endpoint.composition}
    return {**named, "port": endpoint.port}


def connector_to_dict(connector: Connector) -> dict[str, Any]:
    """Convert a Connector to a YAML-serialisable dict."""
    d: dict[str, Any] = {
        "name": connector.name,
        "from": _endpoint_to_dict(connector.from_),
        "to": _endpoint_to_dict(connector.to),
    }
    # Absent means the root composition, whose name is a property of the model,
    # not a constant this serializer could substitute.
    if connector.composition is not None:
        d["composition"] = connector.composition
    if connector.description:
        d["description"] = connector.description
    return d


def task_to_dict(task: Task) -> dict[str, Any]:
    """Convert a Task to a YAML-serialisable dict."""
    d: dict[str, Any] = {
        "name": task.name,
        "period": task.period,
        "priority": task.priority,
        "partition": task.partition,
    }
    if task.wcet is not None:
        d["wcet"] = task.wcet
    if task.description:
        d["description"] = task.description
    d["runnables"] = [_selector(r.swc, r.instance) | {"runnable": r.runnable} for r in task.runnables]
    if task.coexistence:
        d["coexistence"] = task.coexistence
    return _linked(d, task.satisfies)


def hardware_to_dict(element: Hardware) -> dict[str, Any]:
    """Convert a Hardware variant to a YAML-serialisable dict.

    ``enforcement`` is always emitted for the two budgeted kinds: it selects between "a breach fails the build" and
    "a breach is a report-only line", too consequential to leave implicit. ``hw_id`` is informational, so a
    ``hw_id: null`` line would be noise. A partition writes ``asil``, ``trust`` and ``coexistence`` only when they
    differ from the defaults (unrated, ``non_trusted``, unargued).
    """
    d: dict[str, Any] = {"name": element.name, "kind": element.kind}
    if isinstance(element, Partition):
        d["core"] = element.core
        if element.asil is not None:
            d["asil"] = element.asil
        if element.trust != _DEFAULT_PARTITION_TRUST:
            d["trust"] = element.trust
        if element.coexistence:
            d["coexistence"] = element.coexistence
    if isinstance(element, Memory):
        d["type"] = element.type
        d["size"] = element.size
        d["accessible"] = list(element.accessible)
    if isinstance(element, Core | Memory):
        d["threshold"] = element.threshold
        d["enforcement"] = element.enforcement
        if element.asil_capability is not None:
            d["asil_capability"] = element.asil_capability
    if isinstance(element, Core) and element.hw_id is not None:
        d["hw_id"] = element.hw_id
    if element.description:
        d["description"] = element.description
    return _linked(d, element.satisfies)


def sequence_to_dict(sequence: Sequence) -> dict[str, Any]:
    """Convert a Sequence to a YAML-serialisable dict."""
    d: dict[str, Any] = {"name": sequence.name, "task": sequence.task}
    if sequence.title:
        d["title"] = sequence.title
    steps: list[dict[str, Any]] = []
    for step in sequence.steps:
        step_dict: dict[str, Any] = _selector(step.swc, step.instance) | {"runnable": step.runnable}
        if step.note:
            step_dict["note"] = step.note
        steps.append(step_dict)
    d["steps"] = steps
    return d


def _selector(swc: str | None, instance: str | None) -> dict[str, Any]:
    """Return the selector a task entry or a sequence step writes.

    Exactly one of the two is set on a loaded entry (``exactly_one_selector``); the other is left out.
    """
    entry: dict[str, Any] = {}
    if swc is not None:
        entry["swc"] = swc
    if instance is not None:
        entry["instance"] = instance
    return entry


def _described(member: dict[str, Any], description: str) -> dict[str, Any]:
    """Append a member's ``description`` last, and only when set."""
    if description:
        member["description"] = description
    return member


def placed(document: dict[str, Any], package: str | None) -> dict[str, Any]:
    """Append an element's ``package`` last, and only when set.

    Every hand-written dump of a kind emitted directly into a root package ends with this call. ``None`` (the root
    package) writes no key; a value is written as given, and whether it is a legal Identifier is ``check-model``'s
    and the ARXML writer's question.
    """
    if package is not None:
        document["package"] = package
    return document


def _linked(document: dict[str, Any], satisfies: list[str]) -> dict[str, Any]:
    """Append an element's ``satisfies`` last, and only when non-empty.

    Every hand-written dump of a linkable kind ends with this call; without it a mutation that rewrites the file
    (``add_port``, ``update_resource``, ...) would erase the links it holds. The list is copied rather than aliased.
    """
    if satisfies:
        document["satisfies"] = list(satisfies)
    return document


def data_type_to_dict(dt: Any) -> dict[str, Any]:
    """Convert a DataType variant to a YAML-serialisable dict.

    One arm per kind, written by hand. ``writer-mutations`` is a ``total`` consumer, so a kind this function does
    not name is invisible to every coverage gate and stops on the ``TypeError`` at the end.

    The application arm omits ``unit``, ``conversion`` and ``range`` at their defaults. The reference fields
    (``unit_ref``, ``compu_method_ref``, ``data_constr_ref``) are written before ``description``, only when stated;
    an enumeration that names a texttable writes no ``literals``. ``test_data_description_schema.py`` holds each arm
    equal to the derived serializer.
    """
    if isinstance(dt, BaseDataType):
        d: dict[str, Any] = {"name": dt.name, "kind": dt.kind, "size": dt.size, "encoding": dt.encoding}
        if dt.native_declaration:
            d["native_declaration"] = dt.native_declaration
        if dt.description:
            d["description"] = dt.description
        return placed(d, dt.package)
    elif isinstance(dt, ValueDataType):
        d = {"name": dt.name, "kind": dt.kind, "base": dt.base}
        if dt.unit:
            d["unit"] = dt.unit
        if dt.value_min is not None:
            d["value_min"] = dt.value_min
        if dt.value_max is not None:
            d["value_max"] = dt.value_max
        if dt.compu_method_ref is not None:
            d["compu_method_ref"] = dt.compu_method_ref
        if dt.description:
            d["description"] = dt.description
        return placed(d, dt.package)
    elif isinstance(dt, ReferenceDataType):
        d = {"name": dt.name, "kind": dt.kind, "base": dt.base}
        if dt.compu_method_ref is not None:
            d["compu_method_ref"] = dt.compu_method_ref
        if dt.description:
            d["description"] = dt.description
        return placed(d, dt.package)
    elif isinstance(dt, ApplicationDataType):
        d = {"name": dt.name, "kind": dt.kind}
        if dt.unit:
            d["unit"] = dt.unit
        if dt.conversion is not None:
            d["conversion"] = {"factor": dt.conversion.factor, "offset": dt.conversion.offset}
            if dt.conversion.limits is not None:
                # No `limits` at all where neither bound is declared, which states
                # what an absent `limits` states.
                limits = {
                    side: {"value": bound.value, "interval": bound.interval}
                    for side, bound in (("lower", dt.conversion.limits.lower), ("upper", dt.conversion.limits.upper))
                    if bound is not None
                }
                if limits:
                    d["conversion"]["limits"] = limits
        if dt.range is not None:
            bounds: dict[str, Any] = {}
            if dt.range.min is not None:
                bounds["min"] = dt.range.min
            if dt.range.max is not None:
                bounds["max"] = dt.range.max
            d["range"] = bounds
        if dt.unit_ref is not None:
            d["unit_ref"] = dt.unit_ref
        if dt.compu_method_ref is not None:
            d["compu_method_ref"] = dt.compu_method_ref
        if dt.data_constr_ref is not None:
            d["data_constr_ref"] = dt.data_constr_ref
        if dt.description:
            d["description"] = dt.description
        return placed(d, dt.package)
    elif isinstance(dt, EnumDataType):
        d = {"name": dt.name, "kind": dt.kind}
        if dt.literals is not None:
            d["literals"] = [_described({"name": lit.name, "value": lit.value}, lit.description) for lit in dt.literals]
        if dt.compu_method_ref is not None:
            d["compu_method_ref"] = dt.compu_method_ref
        if dt.description:
            d["description"] = dt.description
        return placed(d, dt.package)
    elif isinstance(dt, RecordDataType):
        d = {
            "name": dt.name,
            "kind": dt.kind,
            "fields": [_described({"name": f.name, "type": f.type}, f.description) for f in dt.fields],
        }
        if dt.description:
            d["description"] = dt.description
        return placed(d, dt.package)
    elif isinstance(dt, ArrayDataType):
        d = {
            "name": dt.name,
            "kind": dt.kind,
            "element_type": dt.element_type,
            "length": dt.length,
        }
        if dt.description:
            d["description"] = dt.description
        return placed(d, dt.package)
    elif isinstance(dt, ExternalDataType):
        # `swcraft import model` writes the external type stubs it mints through here.
        d = {"name": dt.name, "kind": dt.kind}
        if dt.external_ref:
            d["external_ref"] = dt.external_ref
        if dt.external_dest:
            d["external_dest"] = dt.external_dest
        if dt.description:
            d["description"] = dt.description
        return d
    elif isinstance(dt, ImplementationRecordDataType):
        # The implementation kinds of category STRUCTURE, ARRAY and DATA_REFERENCE.
        d = {
            "name": dt.name,
            "kind": dt.kind,
            "elements": [
                _described({"name": element.name, "type": element.type}, element.description) for element in dt.elements
            ],
        }
        if dt.description:
            d["description"] = dt.description
        return placed(d, dt.package)
    elif isinstance(dt, ImplementationArrayDataType):
        d = {
            "name": dt.name,
            "kind": dt.kind,
            "element_type": dt.element_type,
            "length": dt.length,
            "element_name": dt.element_name,
        }
        if dt.description:
            d["description"] = dt.description
        return placed(d, dt.package)
    elif isinstance(dt, PointerDataType):
        d = {"name": dt.name, "kind": dt.kind, "target": dt.target}
        if dt.description:
            d["description"] = dt.description
        return placed(d, dt.package)
    else:
        raise TypeError(f"Unknown DataType variant: {type(dt)}")


def mapping_set_to_dict(mapping_set: MappingSet) -> dict[str, Any]:
    """Convert a MappingSet to a YAML-serialisable dict.

    ``maps`` is always written (an empty set is an empty list). Each pair is written from its two declared fields
    rather than by dumping the member model, so a field added to ``kind:mapping_sets.map`` fails the contract's
    gates here instead of silently riding along.
    """
    document: dict[str, Any] = {
        "name": mapping_set.name,
        "maps": [
            {"application": entry.application, "implementation": entry.implementation} for entry in mapping_set.maps
        ],
    }
    if mapping_set.description:
        document["description"] = mapping_set.description
    return placed(document, mapping_set.package)


def _named(entry: dict[str, Any], name: str | None) -> dict[str, Any]:
    """Return *entry* with its stated ``name`` last, left out while it is ``None`` (*derived*)."""
    if name is not None:
        entry["name"] = name
    return entry


def _event_to_dict(event: TimingEvent | InitEvent | OperationEvent | DataEvent | CallReturnedEvent) -> dict[str, Any]:
    """Return one event, written as the kind it is: one arm per kind and no ``else``.

    A kind this function has not been taught is a ``TypeError``, never a silently different event (an ``else``
    once rewrote every operation event as ``{"type": "init"}``).
    """
    if isinstance(event, TimingEvent):
        timing: dict[str, Any] = {"type": "timing", "period": event.period}
        modes = event.disabled_modes
        if modes:
            timing["disabled_modes"] = [
                {"port": mode.port, "mode_group_ref": mode.mode_group_ref, "mode_ref": mode.mode_ref} for mode in modes
            ]
        return _named(timing, event.name)
    if isinstance(event, InitEvent):
        return _named({"type": "init"}, event.name)
    if isinstance(event, OperationEvent):
        return _named({"type": "operation", "port": event.port, "operation": event.operation}, event.name)
    if isinstance(event, DataEvent):
        data: dict[str, Any] = {"type": "data", "port": event.port, "element": event.element}
        if event.disabled_modes:
            data["disabled_modes"] = [
                {"port": mode.port, "mode_group_ref": mode.mode_group_ref, "mode_ref": mode.mode_ref}
                for mode in event.disabled_modes
            ]
        return _named(data, event.name)
    if isinstance(event, CallReturnedEvent):
        # `result` is the result point's SHORT-NAME, left out while derived.
        returned: dict[str, Any] = {"type": "call_returned", "port": event.port, "operation": event.operation}
        if event.result is not None:
            returned["result"] = event.result
        return _named(returned, event.name)
    raise TypeError(f"an event kind the serializer has not been taught: {type(event).__name__}")


def _read_to_dict(read: DataReadRef) -> dict[str, Any]:
    """Return one ``reads`` entry: the pair, a stated ``by`` while set (absent: by argument), then a stated ``name``."""
    entry: dict[str, Any] = {"port": read.port, "element": read.element}
    if read.by is not None:
        entry["by"] = read.by
    return _named(entry, read.name)


def _write_to_dict(write: DataWriteRef) -> dict[str, Any]:
    """Return one ``writes`` entry: the pair, a stated ``by`` while set, then a stated ``name``."""
    entry: dict[str, Any] = {"port": write.port, "element": write.element}
    if write.by is not None:
        entry["by"] = write.by
    return _named(entry, write.name)


def _call_to_dict(call: OperationCallRef) -> dict[str, Any]:
    """Return one ``calls`` entry in ``schema.OperationCallRef``'s field order.

    ``asynchronous`` and ``poll`` are written only when ``true``. Without these arms every writer through
    :func:`swc_to_dict` would turn an asynchronous call synchronous and drop its poll and timeout silently.
    """
    entry: dict[str, Any] = {"port": call.port, "operation": call.operation}
    if call.asynchronous:
        entry["asynchronous"] = True
    if call.poll:
        entry["poll"] = True
    if call.result is not None:
        entry["result"] = call.result
    if call.timeout is not None:
        entry["timeout"] = call.timeout
    return _named(entry, call.name)


def runnable_to_dict(runnable: Runnable) -> dict[str, Any]:
    """Convert a Runnable to a YAML-serialisable dict.

    In ``schema.py`` declaration order; ``concurrent`` and the access lists are omitted at their defaults, so a
    runnable file written before them round-trips byte-identically through every mutation sharing
    :func:`swc_to_dict`. A read's ``by`` is written only when set: absent is the by-argument form.
    ``test_writer_elements.py`` pins this against the generic ``elements.element_to_dict``.
    """
    d: dict[str, Any] = {"name": runnable.name}
    if runnable.min_start_interval != 0.0:
        d["min_start_interval"] = runnable.min_start_interval
    if runnable.concurrent:
        d["concurrent"] = True
    d["events"] = [_event_to_dict(ev) for ev in runnable.events]
    if runnable.reads:
        d["reads"] = [_read_to_dict(read) for read in runnable.reads]
    if runnable.writes:
        d["writes"] = [_write_to_dict(write) for write in runnable.writes]
    if runnable.calls:
        d["calls"] = [_call_to_dict(call) for call in runnable.calls]
    if runnable.reads_variables:
        d["reads_variables"] = [_named({"variable": read.variable}, read.name) for read in runnable.reads_variables]
    if runnable.writes_variables:
        d["writes_variables"] = [
            _named({"variable": write.variable}, write.name) for write in runnable.writes_variables
        ]
    # `can_enter` first, the XSD's wrapper order.
    if runnable.can_enter:
        d["can_enter"] = [{"area": entry.area} for entry in runnable.can_enter]
    if runnable.runs_inside:
        d["runs_inside"] = [{"area": entry.area} for entry in runnable.runs_inside]
    if runnable.description:
        d["description"] = runnable.description
    # `None` is *the symbol is the name*.
    if runnable.symbol is not None:
        d["symbol"] = runnable.symbol
    if runnable.wcet is not None:
        d["wcet"] = runnable.wcet
    if runnable.wcet_type is not None:
        d["wcet_type"] = runnable.wcet_type
    return _linked(d, runnable.satisfies)
