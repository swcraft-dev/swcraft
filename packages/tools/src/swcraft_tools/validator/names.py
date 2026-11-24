"""The name uniqueness, name space and generated-name collision rules.

:func:`check_name_uniqueness` reports duplicate and reserved names (``name.duplicate-*``,
``name.*-collision``, ``swc.access.name-collision``,
``interface.init-constant-name-collision``, ``name.duplicate-requirement``), and
:func:`check_name_space_collisions` reports ``name.name-space-collision``. The module
also holds the emitted-name functions and predicates the ARXML writer, the write path
and the importers share, so every reader prints one sentence per finding.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from swcraft_tools.arxml_version import DATA_DESCRIPTION_PACKAGE_NAMES, ROOT_PACKAGE_NAMES
from swcraft_tools.data_description import APPLICATION_DATA_TYPE_CLASSES
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.identifier import short_name_key
from swcraft_tools.schema import (
    BaseDataType,
    CallReturnedEvent,
    Composition,
    Connector,
    CSInterface,
    DataDescriptionElement,
    DataEvent,
    DataType,
    EnumDataType,
    ExternalDataType,
    ExternalInterface,
    ImplementationRecordDataType,
    InitEvent,
    Interface,
    MappingSet,
    OperationEvent,
    RecordDataType,
    Requirement,
    Runnable,
    SRInterface,
    SWComponent,
    TimingEvent,
)
from swcraft_tools.validator.connectors import COMPOSITION_LAYER
from swcraft_tools.validator.data_types import GENERATED_COMPU_METHOD_SUFFIX, prototype_type_ref
from swcraft_tools.validator.description_elements import (
    DATA_DESCRIPTION_LAYER_OF_CLASS,
    data_description_package_path,
    data_type_layer,
    duplicates,
)
from swcraft_tools.validator.events import (
    GENERATED_INTERNAL_BEHAVIOR_SUFFIX,
    call_returned_events,
    derived_event_names,
    derived_result_point_name,
    runnable_event_subject,
)
from swcraft_tools.validator.generated_names import (
    GENERATED_ACCESS_PREFIXES,
    GENERATED_VARIABLE_ACCESS_PREFIXES,
    access_entries,
    generated_access_point,
    generated_data_type_forms,
    generated_init_constant,
    generates_compu_method,
    stated_access_point_names,
    variable_access_entries,
)
from swcraft_tools.validator.hardware import RESERVED_IMPLEMENTATION_SUFFIX
from swcraft_tools.validator.model_view import ModelInput, ModelView, as_view
from swcraft_tools.validator.results import ConsistencyIssue, ConsistencyResult, consistency_result
from swcraft_tools.validator.scheduling import event_identity


def emitted_variable_access_names(runnable: Runnable) -> list[str]:
    """The ``SHORT-NAME`` each variable access of *runnable* is written with, in :func:`variable_access_entries` order.

    The entry's stated ``name`` where it states one, else ``RV_{variable}`` or
    ``WV_{variable}`` (:func:`stated_or_derived`).
    """
    entries = variable_access_entries(runnable)
    return stated_or_derived(
        [f"{GENERATED_VARIABLE_ACCESS_PREFIXES[list_name]}{variable}" for list_name, variable, _stated in entries],
        [stated for _list, _variable, stated in entries],
    )


def emitted_access_point_names(runnable: Runnable) -> list[str]:
    """The ``SHORT-NAME`` every access point of *runnable* is written with.

    One per entry, in :func:`access_entries` order: the stated ``name``, else the derived form.
    Not pairwise distinct: a repeat is ``rule:swc.access.name-collision`` (exactly) or
    ``rule:name.name-space-collision`` (ignoring case).
    """
    derived = [
        generated_access_point(GENERATED_ACCESS_PREFIXES[list_name], port_name, member)
        for list_name, port_name, _member_field, member in access_entries(runnable)
    ]
    return stated_or_derived(derived, stated_access_point_names(runnable))


#: The suffix of the ``SWC-TIMING`` the writer emits where :func:`emits_swc_timing`, reserved
#: there only. Restated rather than imported (``writer`` imports this package) and held equal to
#: ``arxml_generation.TIMING_SUFFIX`` by a test. Never charged by ``identifier.generated``: the
#: longer ``{swc}_Implementation`` always fails first.
RESERVED_TIMING_SUFFIX = "_Timing"


def emits_swc_timing(swc: SWComponent) -> bool:
    """Whether the writer emits ``{swc}_Timing`` for *swc*: non-external, a runnable declaring a ``wcet``."""
    return not swc.external and any(runnable.wcet is not None for runnable in swc.runnables)


#: One claimant of an access point name: ``(list, port, member field, member, stated name or
#: None)``; a variable access has port ``None`` and member field ``variable``.
_AccessClaim = tuple[str, str | None, str, str, str | None]


def _access_claim_text(list_name: str, port_name: str | None, member_field: str, member: str) -> str:
    """One claimant as the sentences name it: ``reads port 'p' element 'e'``, or ``reads_variables variable 'v'``."""
    if list_name in RESULT_POINT_CLAIM_WORDS:  # a result point, of a poll or of an event
        return f"{RESULT_POINT_CLAIM_WORDS[list_name]} port '{port_name}' {member_field} '{member}'"
    if port_name is None:
        return f"{list_name} {member_field} '{member}'"
    return f"{list_name} port '{port_name}' {member_field} '{member}'"


def _access_claim_identity(port_name: str | None, member_field: str, member: str) -> str | dict[str, str]:
    """The member identity the registry publishes for one claimant's list: composite, or the bare variable."""
    return member if port_name is None else {"port": port_name, member_field: member}


def _access_point_claimants(runnable: Runnable) -> dict[str, list[_AccessClaim]]:
    """Every access-point ``SHORT-NAME`` *runnable* emits, with the entries emitting it.

    The scope is one runnable: the AUTOSAR access containers are not path segments, so every
    access point of one runnable is a ``SHORT-NAME`` sibling of every other. The port lists come
    first, then the variable lists (``READ-LOCAL-VARIABLES``, ``WRITTEN-LOCAL-VARIABLES``), then
    the result points. Claimants are keyed on the entry, so a repeat is one claimant.

    Returns:
        ``{emitted SHORT-NAME: [claimant, ...]}`` (:data:`_AccessClaim`) in emitted-name order,
        each list in reader order.
    """
    claimants: dict[str, dict[_AccessClaim, None]] = {}
    entries = zip(access_entries(runnable), stated_access_point_names(runnable), strict=True)
    for ((list_name, port_name, member_field, member), stated), emitted in zip(
        entries, emitted_access_point_names(runnable), strict=True
    ):
        claimants.setdefault(emitted, {})[(list_name, port_name, member_field, member, stated)] = None
    for (list_name, variable, stated), emitted in zip(
        variable_access_entries(runnable), emitted_variable_access_names(runnable), strict=True
    ):
        claimants.setdefault(emitted, {})[(list_name, None, "variable", variable, stated)] = None
    for (claim_list, port_name, operation, stated), emitted in zip(  # the result points
        result_point_entries(runnable), emitted_result_point_names(runnable), strict=True
    ):
        claimants.setdefault(emitted, {})[(claim_list, port_name, "operation", operation, stated)] = None
    return {emitted: list(claims) for emitted, claims in sorted(claimants.items())}


def shared_access_point_names(runnable: Runnable) -> list[tuple[str, str, bool]]:
    """Every access-point ``SHORT-NAME`` more than one entry of *runnable* is written with.

    One ``(name, claimants, stated)`` per such name, in name order: *claimants* names each
    entry by its list, port and member (a stating one followed by ``(its stated name)``),
    joined with ``and``; *stated* is whether any of them states the name.
    """
    return [
        (
            emitted,
            " and ".join(
                _access_claim_text(list_name, port_name, member_field, member)
                + (" (its stated name)" if stated is not None else "")
                for list_name, port_name, member_field, member, stated in claims
            ),
            any(stated is not None for *_entry, stated in claims),
        )
        for emitted, claims in _access_point_claimants(runnable).items()
        if len(claims) > 1
    ]


def access_point_collision_repair(stated: bool) -> str:
    """The closing sentences of ``swc.access.name-collision``'s message, by whether a claimant states the name."""
    if stated:
        return (
            "The access points of one runnable are SHORT-NAME siblings ([constr_2508]). Change a stated name, "
            "or rename one of the conflicting names."
        )
    return (
        "A port name and a member name may both contain '_', so the derivation is not injective. Rename one of "
        "the conflicting names, or state an entry's name."
    )


def _access_point_collision_message(
    swc_name: str, runnable_name: str, emitted: str, entries: Sequence[_AccessClaim]
) -> str:
    """The one sentence ``swc.access.name-collision`` reports, naming the emitted name and every claimant.

    Every claimant is named because the repair renames one of two declared names that no
    single element holds (the port on the component, the member in the interface).
    """
    if all(stated is None for *_entry, stated in entries):
        derivations = " and ".join(
            _access_claim_text(list_name, port_name, member_field, member)
            for list_name, port_name, member_field, member, _stated in entries
        )
        return (
            f"SWC '{swc_name}' runnable '{runnable_name}' derives generated access point SHORT-NAME "
            f"'{emitted}' from more than one access entry: {derivations}. {access_point_collision_repair(False)}"
        )
    claims = " and ".join(
        f"{_access_claim_text(list_name, port_name, member_field, member)} "
        f"({'stated' if stated is not None else 'derived'})"
        for list_name, port_name, member_field, member, stated in entries
    )
    return (
        f"SWC '{swc_name}' runnable '{runnable_name}' emits access point SHORT-NAME '{emitted}' for more "
        f"than one access entry: {claims}. {access_point_collision_repair(True)}"
    )


def _init_constant_claimants(interfaces: Sequence[Interface]) -> dict[str, list[tuple[str, str]]]:
    """Every ``{Interface}_{Element}_Init`` name, with the pairs that derive it.

    The walk covers every sender-receiver pair, with or without an ``init``: otherwise adding
    the first initial value to one interface would start failing a file nobody touched.
    Claimants are keyed on the pair, so a pair declared twice stays the duplicate it is.

    Returns:
        ``{derived SHORT-NAME: [(interface, data element), ...]}`` in derived-name order, each
        list in reader order.
    """
    claimants: dict[str, dict[tuple[str, str], None]] = {}
    for iface in interfaces:
        if not isinstance(iface, SRInterface):
            continue
        for data_element in iface.data_elements:
            pair = (iface.name, data_element.name)
            claimants.setdefault(generated_init_constant(*pair), {})[pair] = None
    return {emitted: list(pairs) for emitted, pairs in sorted(claimants.items())}


def _init_constant_collision_message(emitted: str, pairs: Sequence[tuple[str, str]], *, declared: bool) -> str:
    """The sentence both halves of ``interface.init-constant-name-collision`` share.

    *declared* is whether an interface is also declared under the name *emitted*.
    """
    derivations = " and ".join(
        f"interface '{interface_name}' data element '{element_name}'" for interface_name, element_name in pairs
    )
    reasons = [f"auto-generated for {derivations}"]
    if declared:
        reasons.append(f"declared as interface '{emitted}'")
    ambiguity = (
        " An interface name and a data element name may both contain '_', so the derivation is not injective."
        if len(pairs) > 1
        else ""
    )
    return (
        f"Generated CONSTANT-SPECIFICATION name '{emitted}' has more than one claimant in the generated "
        f"PortInterfaces package: it is {' and it is '.join(reasons)}.{ambiguity} "
        "Rename one of the conflicting names."
    )


def check_name_uniqueness(model_root: ModelInput) -> ConsistencyResult:
    """Report every name the ARXML writer would refuse to emit, and a requirement id declared twice.

    Reports duplicate SWC, interface and nested names (ports, runnables, events, data elements,
    operations, arguments), the writer's reserved names and generated-name collisions, all in
    one run. AUTOSAR requires a ``SHORT-NAME`` to be unique among its siblings; a repeat makes
    every ``*-REF`` naming it ambiguous (``PORT-REF``, ``TARGET-P-PORT-REF``,
    ``START-ON-EVENT-REF``, ``DATA-ELEMENT-REF``, ``OPERATION-REF``).

    Components and compositions are one ``SHORT-NAME`` namespace: an
    ``APPLICATION-SW-COMPONENT-TYPE``, its ``SWC-IMPLEMENTATION`` and a
    ``COMPOSITION-SW-COMPONENT-TYPE`` are siblings in one ``AR-PACKAGE``. A requirement ``id``
    declared by two files is ``name.duplicate-requirement``: a trace link names a requirement
    by its ``id`` alone.

    What this does not reach: a duplicate composition name across two files
    (``composition.duplicate-name``); a duplicate boundary port, member or delegation inside
    one composition (:func:`check_composition_consistency`); and every emitted name below the
    package level, which the emitter's ``ShortNameScope`` guards.

    Args:
        model_root: The model root, or a view of it.

    Returns:
        ConsistencyResult with consistent=True only when every emitted ``SHORT-NAME`` is unique
        among its siblings and none is reserved.
    """
    view = as_view(model_root)
    index = _build_name_index(view)
    records = _swc_name_records(index)
    records.extend(_composition_type_name_records(index))
    for swc in index.swcs:
        records.extend(_component_member_name_records(swc))
    records.extend(_interface_name_records(index.interfaces))
    records.extend(_init_constant_collision_records(index))
    records.extend(_duplicate_requirement_records(view.requirements))
    return consistency_result(records)


@dataclass(frozen=True)
class _NameIndex:
    """What :func:`check_name_uniqueness`'s phases compare against, built before its first finding.

    Attributes:
        swcs: the ``swc`` layer, in reader order.
        interfaces: the ``interfaces`` layer, in reader order.
        compositions: the ``compositions`` layer, in reader order.
        swc_names: every component's name, in reader order, repeats kept.
        emitting_swcs: the components that emit an ``SWC-IMPLEMENTATION``: every one but an external stub.
        timing_bases: the components that emit an ``SWC-TIMING`` (:func:`emits_swc_timing`).
        declared_interfaces: the interfaces that emit a ``SHORT-NAME`` into ``PortInterfaces``.
    """

    swcs: Sequence[SWComponent]
    interfaces: Sequence[Interface]
    compositions: Sequence[Composition]
    swc_names: Sequence[str]
    emitting_swcs: AbstractSet[str]
    timing_bases: AbstractSet[str]
    declared_interfaces: AbstractSet[str]


def _build_name_index(view: ModelView) -> _NameIndex:
    """Read the three layers :func:`check_name_uniqueness` starts from and index them."""
    swcs = view.swcs
    interfaces = view.interfaces
    compositions = view.compositions
    # Only what the writer emits reserves a name: an external stub emits no element, so
    # refusing `Stub_Implementation` would be stricter than the writer.
    emitting_swcs = {swc.name for swc in swcs if not swc.external}
    timing_bases = {swc.name for swc in swcs if emits_swc_timing(swc)}
    declared_interfaces = {iface.name for iface in interfaces if not isinstance(iface, ExternalInterface)}
    return _NameIndex(
        swcs=swcs,
        interfaces=interfaces,
        compositions=compositions,
        swc_names=[swc.name for swc in swcs],
        emitting_swcs=emitting_swcs,
        timing_bases=timing_bases,
        declared_interfaces=declared_interfaces,
    )


def _swc_name_records(index: _NameIndex) -> list[ConsistencyIssue]:
    """A component name declared twice, or equal to another component's generated implementation or timing."""
    records: list[ConsistencyIssue] = []
    swc_names = index.swc_names
    emitting_swcs = index.emitting_swcs
    timing_bases = index.timing_bases
    for name in duplicates(swc_names):
        records.append(
            ConsistencyIssue(
                rule="name.duplicate-swc",
                message=f"Duplicate SWC name '{name}' declared in multiple files.",
                subjects=(Subject("swc", name),),
            )
        )

    declared_swcs = set(swc_names)
    for name in sorted(declared_swcs):
        if name.endswith(RESERVED_IMPLEMENTATION_SUFFIX) and name[: -len(RESERVED_IMPLEMENTATION_SUFFIX)] in (
            emitting_swcs
        ):
            records.append(
                ConsistencyIssue(
                    rule="name.swc-implementation-collision",
                    message=(
                        f"SWC name '{name}' conflicts with the auto-generated SWC-IMPLEMENTATION name for "
                        f"'{name[: -len(RESERVED_IMPLEMENTATION_SUFFIX)]}'."
                    ),
                    subjects=(Subject("swc", name),),
                )
            )
    for name in sorted(declared_swcs):
        if name.endswith(RESERVED_TIMING_SUFFIX) and name[: -len(RESERVED_TIMING_SUFFIX)] in timing_bases:
            records.append(
                ConsistencyIssue(
                    rule="name.swc-timing-collision",
                    message=(
                        f"SWC name '{name}' conflicts with the auto-generated SWC-TIMING name for "
                        f"'{name[: -len(RESERVED_TIMING_SUFFIX)]}', one of whose runnables declares a wcet."
                    ),
                    subjects=(Subject("swc", name),),
                )
            )
    return records


def _composition_type_name_records(index: _NameIndex) -> list[ConsistencyIssue]:
    """A composition name equal to a component's, or to a component's generated implementation or timing."""
    records: list[ConsistencyIssue] = []
    declared_swcs = set(index.swc_names)
    emitting_swcs = index.emitting_swcs
    timing_bases = index.timing_bases
    # The subject is the composition: a component's name is what connectors, task mappings and
    # requirement links name, so the composition is the side a repair moves.
    for composition in index.compositions:
        if composition.name in declared_swcs:
            records.append(
                ConsistencyIssue(
                    rule="name.duplicate-component-type",
                    message=(
                        f"Composition name '{composition.name}' is also declared as an SWC; a composition "
                        "and a component are siblings in the ComponentTypes package and cannot share one "
                        "SHORT-NAME."
                    ),
                    subjects=(Subject(COMPOSITION_LAYER, composition.name),),
                    target=composition.name,
                )
            )
        elif composition.name.endswith(RESERVED_IMPLEMENTATION_SUFFIX) and (
            composition.name[: -len(RESERVED_IMPLEMENTATION_SUFFIX)] in emitting_swcs
        ):
            records.append(
                ConsistencyIssue(
                    rule="name.composition-implementation-collision",
                    message=(
                        f"Composition name '{composition.name}' conflicts with the auto-generated "
                        "SWC-IMPLEMENTATION name for "
                        f"'{composition.name[: -len(RESERVED_IMPLEMENTATION_SUFFIX)]}'."
                    ),
                    subjects=(Subject(COMPOSITION_LAYER, composition.name),),
                    target=composition.name[: -len(RESERVED_IMPLEMENTATION_SUFFIX)],
                )
            )
        elif composition.name.endswith(RESERVED_TIMING_SUFFIX) and (
            composition.name[: -len(RESERVED_TIMING_SUFFIX)] in timing_bases
        ):
            records.append(
                ConsistencyIssue(
                    rule="name.composition-timing-collision",
                    message=(
                        f"Composition name '{composition.name}' conflicts with the auto-generated SWC-TIMING "
                        f"name for '{composition.name[: -len(RESERVED_TIMING_SUFFIX)]}', one of whose runnables "
                        "declares a wcet."
                    ),
                    subjects=(Subject(COMPOSITION_LAYER, composition.name),),
                    target=composition.name[: -len(RESERVED_TIMING_SUFFIX)],
                )
            )
    return records


def _component_member_name_records(swc: SWComponent) -> list[ConsistencyIssue]:
    """One component's repeated port, runnable and event names and its access-point collisions."""
    records: list[ConsistencyIssue] = []
    element = Subject("swc", swc.name)
    for name in duplicates([port.name for port in swc.ports]):
        records.append(
            ConsistencyIssue(
                rule="name.duplicate-port",
                message=(
                    f"SWC '{swc.name}' has duplicate port name '{name}'; port names must be unique "
                    "within a component (a provide port and a require port of one name are siblings)."
                ),
                subjects=(element.at(field_step("ports"), member_step(name)),),
            )
        )
    for name in duplicates([runnable.name for runnable in swc.runnables]):
        records.append(
            ConsistencyIssue(
                rule="name.duplicate-runnable",
                message=(
                    f"SWC '{swc.name}' has duplicate runnable name '{name}'; runnable names must be "
                    "unique within a component."
                ),
                subjects=(element.at(field_step("runnables"), member_step(name)),),
            )
        )
    # `RUNNABLES` and `EVENTS` are no `Identifiable`s, so runnables and events share the
    # behaviour's name space. The subject is the runnable: an event's name may be derived.
    records.extend(
        ConsistencyIssue(
            rule="name.runnable-event-collision",
            message=collision.message,
            subjects=(element.at(field_step("runnables"), member_step(collision.runnable)),),
        )
        for collision in runnable_event_collisions(swc)
    )
    # Reachable only through a stated name: the derived event names are pairwise distinct.
    records.extend(
        ConsistencyIssue(
            rule="name.duplicate-event",
            message=duplicate.text,
            subjects=tuple(
                element.at(field_step("runnables"), member_step(runnable), field_step("events"), member_step(value))
                for runnable, value, _phrase in duplicate.events
            ),
        )
        for duplicate in duplicate_event_names(swc)
    )

    # Port and member names may both contain `_`, so port `a_b` reading `c` and port `a`
    # reading `b_c` both give `R_a_b_c`; a stated name can collide too. One finding per shared
    # name, nothing renamed: a declared name is the model's to change. The subject is the
    # runnable, since no single element holds both the port and the member name.
    for runnable in swc.runnables:
        for emitted, entries in _access_point_claimants(runnable).items():
            if len(entries) == 1:
                continue
            records.append(
                ConsistencyIssue(
                    rule="swc.access.name-collision",
                    message=_access_point_collision_message(swc.name, runnable.name, emitted, entries),
                    subjects=(element.at(field_step("runnables"), member_step(runnable.name)),),
                )
            )
    return records


def _interface_name_records(interfaces: Sequence[Interface]) -> list[ConsistencyIssue]:
    """An interface name declared twice, and a repeated data element, operation or argument name."""
    records: list[ConsistencyIssue] = []
    for name in duplicates([iface.name for iface in interfaces]):
        records.append(
            ConsistencyIssue(
                rule="name.duplicate-interface",
                message=f"Duplicate interface name '{name}' declared in multiple files.",
                subjects=(Subject("interfaces", name),),
            )
        )

    for iface in interfaces:
        element = Subject("interfaces", iface.name)
        if isinstance(iface, SRInterface):
            for name in duplicates([data_element.name for data_element in iface.data_elements]):
                records.append(
                    ConsistencyIssue(
                        rule="name.duplicate-data-element",
                        message=(
                            f"Interface '{iface.name}' has duplicate data element name '{name}'; data "
                            "element names must be unique within an interface."
                        ),
                        subjects=(element.at(field_step("data_elements"), member_step(name)),),
                    )
                )
        elif isinstance(iface, CSInterface):
            for name in duplicates([operation.name for operation in iface.operations]):
                records.append(
                    ConsistencyIssue(
                        rule="name.duplicate-operation",
                        message=(
                            f"Interface '{iface.name}' has duplicate operation name '{name}'; operation "
                            "names must be unique within an interface."
                        ),
                        subjects=(element.at(field_step("operations"), member_step(name)),),
                    )
                )
            for operation in iface.operations:
                for name in duplicates([argument.name for argument in operation.arguments]):
                    records.append(
                        ConsistencyIssue(
                            rule="name.duplicate-argument",
                            message=(
                                f"Interface '{iface.name}' operation '{operation.name}' has duplicate "
                                f"argument name '{name}'; argument names must be unique within an operation."
                            ),
                            subjects=(
                                element.at(
                                    field_step("operations"),
                                    member_step(operation.name),
                                    field_step("arguments"),
                                    member_step(name),
                                ),
                            ),
                        )
                    )
    return records


def _init_constant_collision_records(index: _NameIndex) -> list[ConsistencyIssue]:
    """The ``{Interface}_{Element}_Init`` constants that collide with each other or with an interface."""
    records: list[ConsistencyIssue] = []
    declared_interfaces = index.declared_interfaces
    # The constants are siblings of the interfaces in `PortInterfaces`: one collides with an
    # interface named like it, or two pairs derive one name (`A_B` + `C` and `A` + `B_C` give
    # `A_B_C_Init`). One finding per shared name, nothing renamed; the subject is the interface,
    # which holds both declared names.
    for emitted, pairs in _init_constant_claimants(index.interfaces).items():
        declared_too = emitted in declared_interfaces
        if len(pairs) == 1 and not declared_too:
            continue
        claimant_names = list(dict.fromkeys([interface_name for interface_name, _ in pairs]))
        if declared_too and emitted not in claimant_names:
            claimant_names.append(emitted)
        records.append(
            ConsistencyIssue(
                rule="interface.init-constant-name-collision",
                message=_init_constant_collision_message(emitted, pairs, declared=declared_too),
                subjects=tuple(Subject("interfaces", name) for name in claimant_names),
            )
        )

    return records


def _duplicate_requirement_records(requirements: Sequence[Requirement]) -> list[ConsistencyIssue]:
    """A requirement ``id`` declared by two files."""
    records: list[ConsistencyIssue] = []
    # A link names a requirement by its `id` alone, so a declared-twice id has no one version
    # to be confirmed against. Appended last, so no finding of the earlier checks moves.
    for requirement_id in duplicates([requirement.id for requirement in requirements]):
        records.append(
            ConsistencyIssue(
                rule="name.duplicate-requirement",
                message=(
                    f"Duplicate requirement id '{requirement_id}' declared in multiple files; a link names a "
                    "requirement by its id, so the id must be unique across the requirements layer."
                ),
                subjects=(Subject("requirements", requirement_id),),
            )
        )

    return records


def stated_or_derived(derived: Sequence[str], stated: Sequence[str | None]) -> list[str]:
    """A stated name where one is set, else the derived one, per element of two parallel lists.

    A stated name takes its element's slot and never moves another element's derived name.
    """
    return [name if given is None else given for name, given in zip(derived, stated, strict=True)]


def emitted_event_names(swc: SWComponent) -> list[str]:
    """The ``SHORT-NAME`` every ``(runnable, event)`` pair of *swc* is written with, in emission order.

    The stated ``name``, else :func:`derived_event_names`'. Not pairwise distinct: an exact
    repeat is ``rule:name.duplicate-event``, a case-only one ``rule:name.name-space-collision``.
    """
    stated = [event.name for runnable in swc.runnables for event in runnable.events]
    return stated_or_derived(derived_event_names(swc), stated)


def emitted_behavior_name(swc: SWComponent) -> str:
    """The ``SHORT-NAME`` *swc*'s ``SWC-INTERNAL-BEHAVIOR`` is written with: stated, or ``{swc}_InternalBehavior``."""
    return swc.behavior_name if swc.behavior_name is not None else f"{swc.name}{GENERATED_INTERNAL_BEHAVIOR_SUFFIX}"


@dataclass(frozen=True)
class RunnableEventCollision:
    """One runnable whose ``SHORT-NAME`` an emitted event of its component also takes.

    The unit of ``rule:name.runnable-event-collision``: one per colliding runnable, naming every
    event it collides with.

    Args:
        swc: the component's name.
        runnable: the colliding runnable's declared name, the one the repair names.
        events: one phrase per colliding event, in emission order.
        stated: whether any of those events states its name.
    """

    swc: str
    runnable: str
    events: tuple[str, ...]
    stated: bool = False

    @property
    def message(self) -> str:
        """The one sentence ``check-model``, the writer, the closure check and ``import component`` all print."""
        repair = f"rename runnable '{self.runnable}'"
        repair += " or change the event's stated name" if self.stated else " or state the event's name"
        return (
            f"SWC '{self.swc}' runnable '{self.runnable}' shares its SHORT-NAME with the "
            + " and the ".join(self.events)
            + "; a runnable and an event are siblings in one SWC-INTERNAL-BEHAVIOR, whose SHORT-NAMEs must "
            f"be unique case-insensitively ([constr_2508]); {repair}."
        )


def _event_phrase(
    runnable: Runnable, event: TimingEvent | InitEvent | OperationEvent | DataEvent | CallReturnedEvent, emitted: str
) -> str:
    """What one emitted event is and what it was derived from, or that it is stated.

    Raises ``TypeError`` for an event kind it does not know, so a new kind is never misnamed.
    """
    source = "stated for" if event.name is not None else "the writer generates for"
    if isinstance(event, OperationEvent):
        return (
            f"OPERATION-INVOKED-EVENT '{emitted}' {source} port '{event.port}' operation "
            f"'{event.operation}' (on runnable '{runnable.name}')"
        )
    if isinstance(event, DataEvent):
        return (
            f"DATA-RECEIVED-EVENT '{emitted}' {source} port '{event.port}' element "
            f"'{event.element}' (on runnable '{runnable.name}')"
        )
    if isinstance(event, CallReturnedEvent):
        return (
            f"ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT '{emitted}' {source} port '{event.port}' operation "
            f"'{event.operation}' (on runnable '{runnable.name}')"
        )
    if isinstance(event, TimingEvent):
        tag = "TIMING-EVENT"
    elif isinstance(event, InitEvent):
        tag = "INIT-EVENT"
    else:
        raise TypeError(f"an event kind the event phrase has not been taught: {type(event).__name__}")
    if event.name is not None:
        return f"{tag} '{emitted}' stated on runnable '{runnable.name}'"
    return f"{tag} '{emitted}' the writer generates for runnable '{runnable.name}'"


def runnable_event_collisions(swc: SWComponent) -> list[RunnableEventCollision]:
    """Every runnable of *swc* whose name an emitted event also takes, case-insensitively.

    The one predicate of ``rule:name.runnable-event-collision``. ``RUNNABLES`` and ``EVENTS``
    are XML containers, not ``Identifiable``s, so a runnable and an event share the name space
    of their ``SWC-INTERNAL-BEHAVIOR`` (``[TPS_GST_00018]``), and ``[constr_2508]`` requires a
    ``SHORT-NAME`` to be unique there case-insensitively. Events are compared by their emitted
    names (:func:`emitted_event_names`); every runnable-against-event pair, exact or case-only,
    is this rule's. ``short_name_key`` agrees with R22-11's ASCII-only *case insensitive* over
    every name the identifier rule admits.

    What it does not reach: the behaviour's other sibling scopes
    (``rule:name.name-space-collision``, ``rule:name.duplicate-event``,
    ``rule:name.duplicate-runnable``). An ``external: true`` stub is skipped: it emits no
    behaviour.

    Returns:
        One entry per colliding runnable name, in the order of its first declaration.
    """
    if swc.external:
        return []
    pairs = [(runnable, event) for runnable in swc.runnables for event in runnable.events]
    by_folded: dict[str, list[tuple[str, bool]]] = {}
    for (runnable, event), emitted in zip(pairs, emitted_event_names(swc), strict=True):
        by_folded.setdefault(short_name_key(emitted), []).append(
            (_event_phrase(runnable, event, emitted), event.name is not None)
        )
    # A name declared twice is one finding here (and `rule:name.duplicate-runnable`'s).
    colliding = {runnable.name: None for runnable in swc.runnables if short_name_key(runnable.name) in by_folded}
    return [
        RunnableEventCollision(
            swc.name,
            name,
            tuple(phrase for phrase, _stated in by_folded[short_name_key(name)]),
            any(stated for _phrase, stated in by_folded[short_name_key(name)]),
        )
        for name in colliding
    ]


@dataclass(frozen=True)
class DuplicateEventName:
    """One ``SHORT-NAME`` two or more events of one component are written with, exactly.

    The unit of ``rule:name.duplicate-event``; a group always holds a stated name, since
    derived names never repeat.

    Args:
        swc: the component's name.
        name: the repeated emitted ``SHORT-NAME``.
        events: one ``(runnable name, event identity, phrase)`` per event emitting it, in
            emission order; the identity is the event's whole value.
    """

    swc: str
    name: str
    events: tuple[tuple[str, Mapping[str, object], str], ...]

    @property
    def text(self) -> str:
        """The one sentence ``check-model``, the writer and ``import component`` print.

        Not ``message``: the emission scan refuses two functions of one name in a module.
        """
        return (
            f"SWC '{self.swc}' writes the event SHORT-NAME '{self.name}' for more than one event: the "
            + " and the ".join(phrase for _runnable, _identity, phrase in self.events)
            + "; events are siblings in one SWC-INTERNAL-BEHAVIOR, whose SHORT-NAMEs must be unique "
            "([constr_2508]); change the stated name of all but one."
        )


def duplicate_event_names(swc: SWComponent) -> list[DuplicateEventName]:
    """Every emitted event ``SHORT-NAME`` of *swc* that two or more of its events take exactly.

    The one predicate of ``rule:name.duplicate-event``. What it does not reach: a case-only
    pair (``rule:name.name-space-collision``) and an event against a runnable
    (``rule:name.runnable-event-collision``). An ``external: true`` stub is skipped.

    Returns:
        One entry per repeated name, in the order of its first emission.
    """
    if swc.external:
        return []
    pairs = [(runnable, event) for runnable in swc.runnables for event in runnable.events]
    by_name: dict[str, list[tuple[str, Mapping[str, object], str]]] = {}
    for (runnable, event), emitted in zip(pairs, emitted_event_names(swc), strict=True):
        by_name.setdefault(emitted, []).append(
            (runnable.name, event_identity(event), _event_phrase(runnable, event, emitted))
        )
    return [DuplicateEventName(swc.name, name, tuple(events)) for name, events in by_name.items() if len(events) > 1]


# ---------------------------------------------------------------------------
# The name-space predicate
# ---------------------------------------------------------------------------

#: The two levels a :class:`NameSpaceCollision` is found at: below the package level (an
#: element owns the name space), or in a root package or one of its sub-packages.
NAME_SPACE_LEVEL_ELEMENT = "element"


NAME_SPACE_LEVEL_PACKAGE = "package"


#: ``name_space_label``'s owner kinds, each with the word its label starts with.
_NAME_SPACE_OWNER_WORDS: dict[str, str] = {
    "package": "package",
    "swc": "SWC",
    "behaviour": "behaviour",
    "runnable": "runnable",
    "interface": "interface",
    "operation": "operation",
    "record": "record",
    "implementation_record": "implementation record",
    "composition": "composition",
}


#: The lists a colliding name is reported with where the list is no declared field: the two
#: generated names under a component, and a root package's sub-packages.
_BEHAVIOUR_LIST = "internal behaviour (generated)"


_EVENTS_LIST = "events (generated)"


_SUB_PACKAGES_LIST = "sub-packages"


#: The lists a stated behaviour or event name is reported with. A stated event is still judged
#: in :data:`_EVENTS_LIST`'s container, so an exact repeat stays ``rule:name.duplicate-event``'s.
_STATED_BEHAVIOUR_LIST = "behavior_name"


_STATED_EVENTS_LIST = "events"


#: The one container every access point of a runnable is judged in for an exact repeat, which
#: is ``rule:swc.access.name-collision``'s, never this one's.
_ACCESS_POINTS_CONTAINER = "access points"


def name_space_label(kind: str, owner: str) -> str:
    """The label a :class:`NameSpaceCollision` names its name space by: the owner, never a container.

    E.g. ``package '/DataTypes'``, ``SWC 'Brake'``, ``runnable 'Brake/Run'``.

    Args:
        kind: one of ``package``, ``swc``, ``behaviour``, ``runnable``, ``interface``,
            ``operation``, ``record``, ``implementation_record``, ``composition``.
        owner: a package's absolute path, a behaviour's generated name, ``{swc}/{runnable}``
            or ``{interface}/{operation}``, the element's name otherwise.

    Raises:
        ValueError: for a kind that owns no name space this predicate walks.
    """
    word = _NAME_SPACE_OWNER_WORDS.get(kind)
    if word is None:
        raise ValueError(f"no name space is owned by a {kind!r}; the kinds are {sorted(_NAME_SPACE_OWNER_WORDS)}")
    return f"{word} '{owner}'"


@dataclass(frozen=True)
class NameSpaceCollision:
    """One group of names equal ignoring case inside one AUTOSAR name space.

    The unit of ``rule:name.name-space-collision``: one per group, whatever its size.

    Args:
        name_space: the owner's label, from :func:`name_space_label` alone.
        level: :data:`NAME_SPACE_LEVEL_ELEMENT` or :data:`NAME_SPACE_LEVEL_PACKAGE`.
        names: every colliding ``(name, list it is declared in)``, sorted, a
            repeat of one name in one list given once.
        subjects: the model places the names are declared at, in the order of
            *names*; a generated name's is its owner's.
    """

    name_space: str
    level: str
    names: tuple[tuple[str, str], ...]
    subjects: tuple[Subject, ...]

    @property
    def sentence(self) -> str:
        """The one sentence every reader prints (not ``message``: one function name per module)."""
        listed = ", ".join(f"'{name}' ({list_name})" for name, list_name in self.names)
        lists = {list_name for _, list_name in self.names}
        additions: list[str] = []
        if _EVENTS_LIST in lists:
            additions.append(
                " (generated event names; rename a port, an operation or a runnable, or state an event's name)"
            )
        if _BEHAVIOUR_LIST in lists:
            additions.append(" (the behaviour's generated name; rename the port, or state behavior_name)")
        if self.level == NAME_SPACE_LEVEL_PACKAGE:
            additions.append(" (set package on one of them to place it in a sub-package, or rename it)")
        return (
            f"{self.name_space} holds names equal ignoring case: {listed} -- AUTOSAR requires a SHORT-NAME "
            "to be unique ignoring case within its name space ([constr_2508]); rename all but one"
            + "".join(additions)
            + "."
        )


@dataclass(frozen=True)
class _NameClaim:
    """One ``SHORT-NAME`` a name space holds: the name, its list, its container and where it is declared.

    ``container`` is what an exact repeat is judged by (a repeat inside one container is the
    per-list duplicate rule's); ``list_name`` is what the sentence names. ``generated`` marks a
    form the writer emits beside a declared root-level element: it takes its owner's package,
    places no sub-package and is no :class:`RootPackageMember`.
    """

    name: str
    list_name: str
    container: str
    subject: Subject
    generated: bool = False


def _claim(
    name: str, list_name: str, subject: Subject, container: str | None = None, *, generated: bool = False
) -> _NameClaim:
    return _NameClaim(name, list_name, list_name if container is None else container, subject, generated)


def _left_to_another_rule(first: _NameClaim, second: _NameClaim) -> bool:
    """Whether the pair is another rule's: an exact repeat in one container, or a runnable against an event."""
    if first.name == second.name and first.container == second.container:
        return True
    return {first.container, second.container} == {"runnables", _EVENTS_LIST}


def _grouped(claims: Sequence[_NameClaim]) -> list[list[_NameClaim]]:
    """*claims* grouped by :func:`~swcraft_tools.identifier.short_name_key`, groups in key order."""
    groups: dict[str, list[_NameClaim]] = {}
    for claim in claims:
        groups.setdefault(short_name_key(claim.name), []).append(claim)
    return [groups[key] for key in sorted(groups)]


def _element_level_groups(claims: Sequence[_NameClaim]) -> list[list[_NameClaim]]:
    """The groups below the package level this rule reports: any two claims not left to another rule."""
    return [
        group
        for group in _grouped(claims)
        if any(
            not _left_to_another_rule(first, second)
            for index, first in enumerate(group)
            for second in group[index + 1 :]
        )
    ]


def _package_level_groups(claims: Sequence[_NameClaim]) -> list[list[_NameClaim]]:
    """The groups in one package name space this rule reports.

    Every case-only group, and every group holding a sub-package's name, exact or not: a
    sub-package's ``SHORT-NAME`` shares its root's name space with the root's elements
    (``[TPS_GST_00018]``, *"nearest ancestor (not only parent)"*). An exact repeat between two
    elements or generated forms is a model-identity finding (``name.duplicate-swc``,
    ``data-type.duplicate-name``, …), not this rule's.
    """
    return [
        group
        for group in _grouped(claims)
        if len({claim.name for claim in group}) > 1
        or (
            any(claim.list_name == _SUB_PACKAGES_LIST for claim in group)
            and any(claim.list_name != _SUB_PACKAGES_LIST for claim in group)
        )
    ]


def _collision(name_space: str, level: str, group: Sequence[_NameClaim]) -> NameSpaceCollision:
    """One :class:`NameSpaceCollision` from one reported group: names sorted, a repeat given once."""
    subjects_by_name: dict[tuple[str, str], list[Subject]] = {}
    for claim in group:
        subjects_by_name.setdefault((claim.name, claim.list_name), []).append(claim.subject)
    names = tuple(sorted(subjects_by_name))
    # By equality, not by hash: an event's or an access entry's identity can be a dict.
    subjects: list[Subject] = []
    for name in names:
        subjects.extend(subject for subject in subjects_by_name[name] if subject not in subjects)
    return NameSpaceCollision(name_space, level, names, tuple(subjects))


def _component_name_spaces(swc: SWComponent) -> Iterator[tuple[str, list[_NameClaim]]]:
    """A component's own name space, its behaviour's, and each runnable's.

    Every name is the one the writer emits. A stated name is claimed in the list that states it
    and addressed where it is stated, a derived one at what it is derived from. The behaviour's
    name space also holds the variables and exclusive areas; a runnable's holds its port and
    variable accesses and its result points.
    """
    owner = Subject("swc", swc.name)
    behaviour = emitted_behavior_name(swc)
    behaviour_claim = (
        _claim(behaviour, _BEHAVIOUR_LIST, owner.at(field_step("name")))
        if swc.behavior_name is None
        else _claim(behaviour, _STATED_BEHAVIOUR_LIST, owner.at(field_step("behavior_name")))
    )
    yield (
        name_space_label("swc", swc.name),
        [_claim(port.name, "ports", owner.at(field_step("ports"), member_step(port.name))) for port in swc.ports]
        + [behaviour_claim],
    )
    # A derived event is addressed by its runnable's `events` list: a timing event's identity
    # is its `period`, which names no SHORT-NAME.
    pairs = [(runnable, event) for runnable in swc.runnables for event in runnable.events]
    yield (
        name_space_label("behaviour", behaviour),
        [
            _claim(runnable.name, "runnables", owner.at(field_step("runnables"), member_step(runnable.name)))
            for runnable in swc.runnables
        ]
        + [
            _claim(
                emitted,
                _EVENTS_LIST,
                owner.at(field_step("runnables"), member_step(runnable.name), field_step("events")),
            )
            if event.name is None
            else _claim(
                emitted,
                _STATED_EVENTS_LIST,
                runnable_event_subject(owner, runnable.name, event_identity(event)),
                container=_EVENTS_LIST,
            )
            for (runnable, event), emitted in zip(pairs, emitted_event_names(swc), strict=True)
        ]
        # Variables and exclusive areas are children of the behaviour too.
        + [
            _claim(variable.name, "variables", owner.at(field_step("variables"), member_step(variable.name)))
            for variable in swc.variables
        ]
        + [
            _claim(area.name, "exclusive_areas", owner.at(field_step("exclusive_areas"), member_step(area.name)))
            for area in swc.exclusive_areas
        ],
    )
    for runnable in swc.runnables:
        yield (
            name_space_label("runnable", f"{swc.name}/{runnable.name}"),
            [
                _claim(
                    emitted,
                    list_name,
                    owner.at(
                        field_step("runnables"),
                        member_step(runnable.name),
                        field_step(list_name),
                        member_step(_access_claim_identity(port_name, member_field, member)),
                        *((field_step("name"),) if stated is not None else ()),
                    ),
                    container=_ACCESS_POINTS_CONTAINER,
                )
                if list_name not in RESULT_POINT_CLAIM_WORDS
                else _result_point_claim(owner, runnable, emitted, list_name, port_name, member, stated)
                for emitted, entries in _access_point_claimants(runnable).items()
                for list_name, port_name, member_field, member, stated in entries
            ],
        )


def _interface_name_spaces(iface: Interface) -> Iterator[tuple[str, list[_NameClaim]]]:
    """An interface's name space, and each of its operations'. A stub has none.

    The owner is built inside each arm: the rule's ``appliesTo`` does not list the stub.
    """
    if isinstance(iface, SRInterface):
        owner = Subject("interfaces", iface.name)
        yield (
            name_space_label("interface", iface.name),
            [
                _claim(element.name, "data_elements", owner.at(field_step("data_elements"), member_step(element.name)))
                for element in iface.data_elements
            ],
        )
    elif isinstance(iface, CSInterface):
        owner = Subject("interfaces", iface.name)
        yield (
            name_space_label("interface", iface.name),
            [
                _claim(operation.name, "operations", owner.at(field_step("operations"), member_step(operation.name)))
                for operation in iface.operations
            ],
        )
        for operation in iface.operations:
            at_operation = owner.at(field_step("operations"), member_step(operation.name))
            yield (
                name_space_label("operation", f"{iface.name}/{operation.name}"),
                [
                    _claim(
                        argument.name, "arguments", at_operation.at(field_step("arguments"), member_step(argument.name))
                    )
                    for argument in operation.arguments
                ],
            )


def _record_name_spaces(dt: DataType) -> Iterator[tuple[str, list[_NameClaim]]]:
    """A record's or an implementation record's name space. Every other kind has none."""
    if isinstance(dt, RecordDataType):
        owner = Subject("application_types", dt.name)
        yield (
            name_space_label("record", dt.name),
            [
                _claim(record_field.name, "fields", owner.at(field_step("fields"), member_step(record_field.name)))
                for record_field in dt.fields
            ],
        )
    elif isinstance(dt, ImplementationRecordDataType):
        owner = Subject("implementation_types", dt.name)
        yield (
            name_space_label("implementation_record", dt.name),
            [
                _claim(element.name, "elements", owner.at(field_step("elements"), member_step(element.name)))
                for element in dt.elements
            ],
        )


def _composition_name_spaces(
    compositions: Sequence[Composition], connectors: Sequence[Connector]
) -> Iterator[tuple[str, list[_NameClaim]]]:
    """Each composition's one name space: its ports, members, delegations and the connectors drawn inside it.

    A connector without ``composition`` is drawn inside the one root. With no root or several
    it is judged nowhere: that is ``rule:composition.no-root`` / ``multiple-roots``' finding.
    """
    roots = [composition.name for composition in compositions if composition.root]
    root = roots[0] if len(roots) == 1 else None
    for composition in compositions:
        owner = Subject(COMPOSITION_LAYER, composition.name)
        yield (
            name_space_label("composition", composition.name),
            [
                _claim(port.name, "ports", owner.at(field_step("ports"), member_step(port.name)))
                for port in composition.ports
            ]
            + [
                _claim(member.name, "members", owner.at(field_step("members"), member_step(member.name)))
                for member in composition.members
            ]
            + [
                _claim(
                    delegation.name, "delegations", owner.at(field_step("delegations"), member_step(delegation.name))
                )
                for delegation in composition.delegations
            ]
            + [
                _claim(connector.name, "connectors", Subject("connectors", connector.name))
                for connector in connectors
                if (connector.composition if connector.composition is not None else root) == composition.name
            ],
        )


def _package_claims(
    swcs: Sequence[SWComponent],
    interfaces: Sequence[Interface],
    data_types: Sequence[DataType],
    compositions: Sequence[Composition],
    mapping_sets: Sequence[MappingSet],
    elements: Sequence[DataDescriptionElement],
) -> dict[str, list[tuple[str | None, _NameClaim]]]:
    """Every name each home package's elements emit, with the ``package`` it is placed in.

    Keyed by the home's path below the document root: each data description layer's package
    (:data:`DATA_DESCRIPTION_PACKAGE_PATHS`), then ``PortInterfaces`` and ``ComponentTypes``.

    The declared non-external root-level elements claim their names, and so do the generated
    forms the writer emits beside them, in the owner's package: ``{enum}_CompuMethod`` only
    for an enumeration stating ``literals``, an
    application type's ``_Unit`` / ``_Compu`` / ``_Range`` (:func:`generated_data_type_forms`),
    ``{swc}_Implementation``, ``{swc}_Timing`` where :func:`emits_swc_timing`, and ``{I}_{E}_Init``
    for a data element that sets ``init`` and whose ``type`` resolves to a non-stub type.
    External stubs claim nothing.
    """
    _, port_interfaces_root, component_types_root = ROOT_PACKAGE_NAMES
    claims: dict[str, list[tuple[str | None, _NameClaim]]] = {
        home: [] for home in (*DATA_DESCRIPTION_PACKAGE_PATHS, port_interfaces_root, component_types_root)
    }
    resolving_types = {
        (isinstance(dt, APPLICATION_DATA_TYPE_CLASSES), dt.name)
        for dt in data_types
        if not isinstance(dt, (ExternalDataType, BaseDataType))
    }
    for dt in data_types:
        if isinstance(dt, ExternalDataType):
            continue
        owner = Subject(
            "base_types"
            if isinstance(dt, BaseDataType)
            else "application_types"
            if isinstance(dt, APPLICATION_DATA_TYPE_CLASSES)
            else "implementation_types",
            dt.name,
        )
        home = data_description_package_path(dt)
        claims[home].append((dt.package, _claim(dt.name, owner.layer, owner)))
        label = f"generated for data type '{dt.name}'"
        if isinstance(dt, EnumDataType):
            if generates_compu_method(dt):
                claims[home].append(
                    (dt.package, _claim(f"{dt.name}{GENERATED_COMPU_METHOD_SUFFIX}", label, owner, generated=True))
                )
        else:
            claims[home].extend(
                (dt.package, _claim(f"{dt.name}{suffix}", label, owner, generated=True))
                for suffix in sorted(generated_data_type_forms(dt))
            )
    for element in elements:
        layer = data_type_layer(element)
        claims[data_description_package_path(element)].append(
            (element.package, _claim(element.name, layer, Subject(layer, element.name)))
        )
    for mapping_set in mapping_sets:
        claims[data_description_package_path(mapping_set)].append(
            (mapping_set.package, _claim(mapping_set.name, "mapping_sets", Subject("mapping_sets", mapping_set.name)))
        )
    for iface in interfaces:
        if isinstance(iface, ExternalInterface):
            continue
        owner = Subject("interfaces", iface.name)
        claims[port_interfaces_root].append((iface.package, _claim(iface.name, "interfaces", owner)))
        if isinstance(iface, SRInterface):
            claims[port_interfaces_root].extend(
                (
                    iface.package,
                    _claim(
                        generated_init_constant(iface.name, element.name),
                        f"generated for interface '{iface.name}'",
                        owner.at(field_step("data_elements"), member_step(element.name)),
                        generated=True,
                    ),
                )
                for element in iface.data_elements
                if element.init is not None and prototype_type_ref(element)[::-1] in resolving_types
            )
    for swc in swcs:
        if swc.external:
            continue
        owner = Subject("swc", swc.name)
        claims[component_types_root].append((swc.package, _claim(swc.name, "swc", owner)))
        claims[component_types_root].append(
            (
                swc.package,
                _claim(
                    f"{swc.name}{RESERVED_IMPLEMENTATION_SUFFIX}",
                    f"generated for SWC '{swc.name}'",
                    owner,
                    generated=True,
                ),
            )
        )
        if emits_swc_timing(swc):
            claims[component_types_root].append(
                (
                    swc.package,
                    _claim(
                        f"{swc.name}{RESERVED_TIMING_SUFFIX}", f"generated for SWC '{swc.name}'", owner, generated=True
                    ),
                )
            )
    for composition in compositions:
        claims[component_types_root].append(
            (
                composition.package,
                _claim(composition.name, "compositions", Subject(COMPOSITION_LAYER, composition.name)),
            )
        )
    return claims


def _package_name_spaces(
    placed: Mapping[str, Sequence[tuple[str | None, _NameClaim]]],
) -> Iterator[tuple[str, list[_NameClaim]]]:
    """Every package name space, as ``(label, claims)``: each home, then its sub-packages by name.

    A home holds its own elements and the ``SHORT-NAME`` of every sub-package it creates (a
    sub-package named by several elements is one name); a sub-package holds the elements placed
    in it. Two ``package`` values differing only in case are two sub-packages, as the writer
    creates them, and a case-only group of the home's.
    """
    for root, claims in placed.items():
        sub_packages: dict[str, list[Subject]] = {}
        for package, claim in claims:
            if package is not None and not claim.generated:
                sub_packages.setdefault(package, []).append(claim.subject.at(field_step("package")))
        yield (
            name_space_label("package", f"/{root}"),
            [claim for package, claim in claims if package is None]
            + [
                _claim(name, _SUB_PACKAGES_LIST, subject)
                for name, subjects in sorted(sub_packages.items())
                for subject in subjects
            ],
        )
        for name in sorted(sub_packages):
            yield (
                name_space_label("package", f"/{root}/{name}"),
                [claim for package, claim in claims if package == name],
            )


def name_space_collisions(
    swcs: Sequence[SWComponent],
    interfaces: Sequence[Interface],
    data_types: Sequence[DataType],
    compositions: Sequence[Composition],
    connectors: Sequence[Connector],
    mapping_sets: Sequence[MappingSet],
    elements: Sequence[DataDescriptionElement],
) -> list[NameSpaceCollision]:
    """Every group of names equal ignoring case in one AUTOSAR name space of the generated ARXML.

    The one predicate of ``rule:name.name-space-collision``, pure over its arguments (stubs
    among them are skipped). ``[constr_2508]``: *"The content of shortName needs to be unique
    (case insensitive) within a given Identifiable"* (GST R4.2.2 ``:2635``, R4.3.0 ``:2711``),
    the name space being the one *"established by the nearest ancestor (not only parent)
    element which is an Identifiable"* (``[TPS_GST_00018]``, R4.3.0 ``:2682``). So the XML
    containers between (``PORTS``, ``RUNNABLES``, ``EVENTS`` …) are merged, and a root
    package's sub-package names share its name space with its elements. Names are compared by
    :func:`~swcraft_tools.identifier.short_name_key`.

    Reported: below the package level, every group spelled differently and every exact group
    spanning two containers; in a package, every case-only group and every group holding a
    sub-package's name. Left to other rules: an exact repeat inside one container, a runnable
    against an event (``rule:name.runnable-event-collision``), and an exact repeat between two
    root-level elements or generated forms.

    What it does not reach: enum literals (a ``COMPU-SCALE``'s ``SHORT-LABEL`` is no
    ``SHORT-NAME``), and a connector that names no composition in a model without exactly one
    root.

    Returns:
        One :class:`NameSpaceCollision` per reported group: the element-level name spaces in
        model read order, then the package name spaces (each home followed by its
        sub-packages by name); groups of one name space in ``short_name_key`` order.
    """
    below: list[tuple[str, list[_NameClaim]]] = []
    for swc in swcs:
        if not swc.external:
            below.extend(_component_name_spaces(swc))
    for iface in interfaces:
        below.extend(_interface_name_spaces(iface))
    for dt in data_types:
        below.extend(_record_name_spaces(dt))
    below.extend(_composition_name_spaces(compositions, connectors))
    collisions = [
        _collision(label, NAME_SPACE_LEVEL_ELEMENT, group)
        for label, claims in below
        for group in _element_level_groups(claims)
    ]
    placed = _package_claims(swcs, interfaces, data_types, compositions, mapping_sets, elements)
    collisions.extend(
        _collision(label, NAME_SPACE_LEVEL_PACKAGE, group)
        for label, claims in _package_name_spaces(placed)
        for group in _package_level_groups(claims)
    )
    return collisions


@dataclass(frozen=True)
class RootPackageMember:
    """One declared element emitted into a root package, with the names it generates there."""

    layer: str
    name: str
    package: str | None
    generated: tuple[str, ...]


def root_package_members(
    swcs: Sequence[SWComponent],
    interfaces: Sequence[Interface],
    data_types: Sequence[DataType],
    compositions: Sequence[Composition],
    mapping_sets: Sequence[MappingSet],
    elements: Sequence[DataDescriptionElement],
) -> dict[str, list[RootPackageMember]]:
    """Every declared element each home package receives, keyed by the home's path, in :func:`_package_claims`' order.

    Read off the same claims :func:`name_space_collisions` judges at package level, so the
    importers place by exactly the names the rule reads. External stubs are absent.
    """
    members: dict[str, list[RootPackageMember]] = {}
    for root, claims in _package_claims(swcs, interfaces, data_types, compositions, mapping_sets, elements).items():
        generated: dict[tuple[str, str | None], list[str]] = {}
        declared: list[tuple[str | None, _NameClaim]] = []
        for package, claim in claims:
            if not claim.generated:
                declared.append((package, claim))
            else:
                generated.setdefault((claim.subject.layer, claim.subject.element), []).append(claim.name)
        members[root] = [
            RootPackageMember(
                claim.subject.layer,
                claim.name,
                package,
                tuple(generated.get((claim.subject.layer, claim.subject.element), ())),
            )
            for package, claim in declared
        ]
    return members


def _name_space_subject(subject: Subject) -> Subject:
    """*subject*, re-stated at its own layer by a literal, one arm per layer the rule reports at.

    The contract's emission scan cannot follow subjects inside :class:`NameSpaceCollision`, so
    the literal arms are the layers it compares with the rule's ``appliesTo``.

    Raises:
        ValueError: for a subject in any other layer.
    """
    if subject.layer == "swc":
        return Subject("swc", subject.element, subject.path)
    if subject.layer == "interfaces":
        return Subject("interfaces", subject.element, subject.path)
    if subject.layer == "implementation_types":
        return Subject("implementation_types", subject.element, subject.path)
    if subject.layer == "base_types":
        return Subject("base_types", subject.element, subject.path)
    if subject.layer == "application_types":
        return Subject("application_types", subject.element, subject.path)
    if subject.layer == COMPOSITION_LAYER:
        return Subject(COMPOSITION_LAYER, subject.element, subject.path)
    if subject.layer == "connectors":
        return Subject("connectors", subject.element, subject.path)
    if subject.layer == "mapping_sets":
        return Subject("mapping_sets", subject.element, subject.path)
    if subject.layer == "units":
        return Subject("units", subject.element, subject.path)
    if subject.layer == "compu_methods":
        return Subject("compu_methods", subject.element, subject.path)
    if subject.layer == "data_constraints":
        return Subject("data_constraints", subject.element, subject.path)
    raise ValueError(f"name-space subject in an unexpected layer {subject.layer!r}")


def check_name_space_collisions(model_root: ModelInput) -> ConsistencyResult:
    """``rule:name.name-space-collision``: one finding per group :func:`name_space_collisions` reports.

    ``[constr_2508]`` (GST R4.2.2 ``:2635``, R4.3.0 ``:2711``): a ``SHORT-NAME`` is unique
    ignoring case within the name space of its nearest ``Identifiable`` (``[TPS_GST_00018]``).
    The subjects are the declaration places, in the order of the names.
    """
    view = as_view(model_root)
    description = view.data_description
    collisions = name_space_collisions(
        view.swcs,
        view.interfaces,
        description.data_types,
        view.compositions,
        view.connectors,
        view.mapping_sets,
        description.elements,
    )
    return consistency_result(
        [
            ConsistencyIssue(
                rule="name.name-space-collision",
                message=collision.sentence,
                subjects=tuple(_name_space_subject(subject) for subject in collision.subjects),
            )
            for collision in collisions
        ]
    )


#: The package path ``DataTypes/{LayerPackage}`` of every data description layer the
#: model declares elements of, in :data:`DATA_DESCRIPTION_LAYER_OF_CLASS`' order.
DATA_DESCRIPTION_PACKAGE_PATHS: tuple[str, ...] = tuple(
    f"{ROOT_PACKAGE_NAMES[0]}/{DATA_DESCRIPTION_PACKAGE_NAMES[layer]}" for _, layer in DATA_DESCRIPTION_LAYER_OF_CLASS
)


#: The words a result point's claimant is named by in the access point sentences, keyed by
#: :func:`result_point_entries`' claim list.
RESULT_POINT_CLAIM_WORDS: dict[str, str] = {
    "poll": "the polled result point of calls",
    "call_returned": "the result point of the call_returned event on",
}


def result_point_entries(runnable: Runnable) -> list[tuple[str, str, str, str | None]]:
    """``(claim list, port, operation, stated result)`` for each result point *runnable* owns.

    The writer's order: one per ``poll: true`` call, then one per ``call_returned`` event.
    """
    return [
        *(("poll", call.port, call.operation, call.result) for call in runnable.calls if call.poll),
        *(
            ("call_returned", event.port, event.operation, event.result)
            for event in runnable.events
            if isinstance(event, CallReturnedEvent)
        ),
    ]


def emitted_result_point_names(runnable: Runnable) -> list[str]:
    """The ``SHORT-NAME`` each result point of *runnable* is written with, in :func:`result_point_entries` order.

    The stated ``result``, else ``Result_{port}_{operation}``. They share the runnable's access
    point name space and collide only through a stated name, or a poll and an event of one pair.
    """
    entries = result_point_entries(runnable)
    return stated_or_derived(
        [derived_result_point_name(port, operation) for _claim, port, operation, _stated in entries],
        [stated for *_entry, stated in entries],
    )


def _result_point_claim(
    owner: Subject,
    runnable: Runnable,
    emitted: str,
    claim_list: str,
    port_name: str | None,
    operation: str,
    stated: str | None,
) -> _NameClaim:
    """One result point's claim in its runnable's name space, in the list that states it.

    A poll is addressed at the call entry (its ``result`` field when stated); an event at the
    event when it states its ``result``, else at the runnable's ``events`` list.
    """
    at_runnable = owner.at(field_step("runnables"), member_step(runnable.name))
    if claim_list == "poll":
        entry = at_runnable.at(field_step("calls"), member_step({"port": port_name, "operation": operation}))
        return _claim(
            emitted,
            "calls",
            entry.at(field_step("result")) if stated is not None else entry,
            container=_ACCESS_POINTS_CONTAINER,
        )
    stating = next(
        (
            event
            for event in call_returned_events(runnable)
            if (event.port, event.operation, event.result) == (port_name, operation, stated)
        ),
        None,
    )
    at = (
        runnable_event_subject(owner, runnable.name, event_identity(stating))
        if stated is not None and stating is not None
        else at_runnable.at(field_step("events"))
    )
    return _claim(emitted, "events", at, container=_ACCESS_POINTS_CONTAINER)
