"""The event rules and the identifier conformance of declared and generated names.

Checks ``identifier.declared`` and ``identifier.generated`` over every name that
reaches ARXML (:func:`check_identifier_conformance`), the operation-invoked events
(:func:`check_operation_events`), the data-received events
(:func:`check_data_events`) and the asynchronous calls with their call-returned
events and result points (:func:`async_call_records`). Holds the affixes of the
names the writer derives and a restatement of its event-name derivation
(:func:`derived_event_names`); the validator does not import the writer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple, Protocol

from swcraft_tools.arxml_version import DEFAULT_ARXML_VERSION, ArxmlVersion
from swcraft_tools.data_description import APPLICATION_DATA_TYPE_CLASSES, DataDescription
from swcraft_tools.diagnostics import Subject, SubjectStep, field_step, member_step
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH, check_identifier
from swcraft_tools.model_registry import current_registry
from swcraft_tools.rendering import render_model_scalar
from swcraft_tools.schema import (
    INTERFACE_KIND_PROSE,
    STUB_MEMBER_DEST,
    ArrayDataType,
    BaseDataType,
    CallReturnedEvent,
    Composition,
    Connector,
    CSArgument,
    CSInterface,
    CSOperation,
    DataEvent,
    DataType,
    EnumDataType,
    ExternalDataType,
    ExternalInterface,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    InitEvent,
    Interface,
    MappingSet,
    OperationCallRef,
    OperationEvent,
    Port,
    RecordDataType,
    Runnable,
    SRInterface,
    SWComponent,
    TextTable,
    TimingEvent,
)
from swcraft_tools.validator.compatibility import ResolvedType, resolve_type, type_pair_difference
from swcraft_tools.validator.compositions import (
    STUB_MEMBER_LISTS,
    stub_member_list,
    stub_member_miss,
    stub_member_names,
)
from swcraft_tools.validator.data_types import (
    APPLICATION_FORM_RULES,
    GENERATED_COMPU_METHOD_SUFFIX,
    MAP_APPLICATION_KINDS,
    application_type_pairings,
    declared_type,
    prototype_type_ref,
)
from swcraft_tools.validator.description_elements import duplicates
from swcraft_tools.validator.generated_names import (
    GENERATED_ACCESS_PREFIXES,
    GENERATED_INIT_CONSTANT_SUFFIX,
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
from swcraft_tools.validator.init_values import quoted_list
from swcraft_tools.validator.model_view import ModelInput, as_view
from swcraft_tools.validator.results import ConsistencyIssue, ConsistencyResult, consistency_result
from swcraft_tools.validator.scheduling import event_identity

GENERATED_INTERNAL_BEHAVIOR_SUFFIX = "_InternalBehavior"


GENERATED_TIMING_EVENT_PREFIX = "TMT_"


GENERATED_INIT_EVENT_PREFIX = "IT_"


#: ``OIT_{port}_{operation}``: derived from **two** declared names, which get 123
#: characters *between them* before any collision suffix. Not reserved: the event
#: is nested inside its own component's behaviour.
GENERATED_OPERATION_EVENT_PREFIX = "OIT_"


#: ``DRT_{port}_{element}``: two declared names, 123 characters *between them*
#: before any collision suffix. It can equal a stated event name, never another
#: derived one.
GENERATED_DATA_EVENT_PREFIX = "DRT_"


#: ``ASRT_{port}_{operation}``: two declared names, 122 characters *between them*
#: before any collision suffix.
GENERATED_RETURNS_EVENT_PREFIX = "ASRT_"


#: The word a finding calls each event kind by, keyed by its ``type``
#: discriminator; ``data`` takes the standard's name, *data-received*.
EVENT_KIND_WORDS: dict[str, str] = {
    "timing": "timing",
    "init": "init",
    "operation": "operation",
    "data": "data-received",
    "call_returned": "call-returned",
}


#: What :data:`GENERATED_INIT_CONSTANT_SUFFIX` and the separating ``_`` cost of
#: the 128, and therefore the number the two declared names share.
GENERATED_INIT_CONSTANT_ADDED_LENGTH = len(GENERATED_INIT_CONSTANT_SUFFIX) + 1


#: ``Result_{port}_{operation}``, the ``ASYNCHRONOUS-SERVER-CALL-RESULT-POINT`` of a
#: polled asynchronous call or of a ``call_returned`` event that states no
#: ``result``: two declared names, 120 characters between them, in the runnable's
#: access point name space. The writer's ``RESULT_POINT_PREFIX`` must equal it.
GENERATED_RESULT_POINT_PREFIX = "Result_"


def _generated_access_added_length(prefix: str) -> int:
    """What *prefix* and the separating ``_`` cost of the 128; must equal the writer's."""
    return len(prefix) + 1


#: The suffix of the ``EXECUTION-TIME-CONSTRAINT`` the writer emits for a
#: runnable declaring a ``wcet``; it reserves no package-level name. Must equal
#: ``arxml_generation.BUDGET_SUFFIX``.
GENERATED_BUDGET_SUFFIX = "_Budget"


def suggest_identifier(name: str, *, max_length: int = MAX_IDENTIFIER_LENGTH) -> str:
    """A conforming name derived from *name*, for the message's "for example".

    A suggestion, never an action: renaming is a multi-file edit a human performs.
    Every character outside ``[A-Za-z0-9_]`` becomes an underscore, the leading
    run of non-letters is dropped (AUTOSAR requires a letter first) and the result
    is truncated to *max_length*, the budget left for the *declared* name.

    Returns:
        A name :func:`~swcraft_tools.identifier.check_identifier` accepts.
    """
    cleaned = "".join(char if char.isascii() and (char.isalnum() or char == "_") else "_" for char in name)
    cleaned = cleaned.lstrip("_0123456789")
    if not cleaned:
        cleaned = "Element"
    return cleaned[:max_length]


def _impact_hint(impact_target: str | None) -> str:
    """The ``swcraft impact`` pointer, for the names that command can resolve.

    ``impact`` resolves top-level elements only, so a nested name (a port, a
    runnable, a member) passes None. The name is quoted, since a refused name may
    hold a space.
    """
    return f" ('swcraft impact \"{impact_target}\"' lists what references it)" if impact_target else ""


def _identifier_issue(
    subject: str,
    name: str,
    layer: str,
    reason: str,
    *,
    impact_target: str | None = None,
) -> str:
    """Word the report for a *declared* name that is not a legal Identifier.

    Args:
        subject: what the name names, e.g. ``"SWC 'My SWC'"``.
        name: the offending name.
        layer: the layer declaring it, e.g. ``"the swc layer"`` — never a file.
        reason: :data:`~swcraft_tools.identifier.REASON_PATTERN` or
            :data:`~swcraft_tools.identifier.REASON_LENGTH`.
        impact_target: the element to point ``swcraft impact`` at, or None for a
            nested name.
    """
    return (
        f"{subject} in {layer} is not a legal AUTOSAR Identifier: {reason}. "
        f"Rename '{name}' — for example to '{suggest_identifier(name)}'"
        f"{_impact_hint(impact_target)}."
    )


#: The repair for a generated-form overflow a model can state the name for.
_WRITTEN_IN_PLACE = "a stated name is written in place of the generated one."


def _stated_repair(field: str | None) -> str:
    """The sentence an overflow ends with where *field* can state the name, else nothing."""
    return f" Or state {field}: {_WRITTEN_IN_PLACE}" if field is not None else ""


def _generated_identifier_issue(
    subject: str,
    name: str,
    layer: str,
    emitted: str,
    added_length: int,
    reason: str,
    *,
    impact_target: str | None = None,
    stated_field: str | None = None,
) -> str:
    """Word the report for a legal declared name whose *emitted* form is not.

    Args:
        subject: what the name names, e.g. ``"SWC 'A'"``.
        name: the declared name, which the suggestion derives from.
        layer: the layer declaring it, as :func:`_identifier_issue` takes it.
        emitted: the generated ``SHORT-NAME``, in its final shape.
        added_length: how many characters the writer adds to *name* (a count,
            since an event's collision counter adds a prefix **and** a suffix).
        reason: the rule half that failed.
        impact_target: the element to point ``swcraft impact`` at, or None.
        stated_field: the field that can state the emitted name instead, or None
            for a form with no stated twin.
    """
    budget = MAX_IDENTIFIER_LENGTH - added_length
    return (
        f"{subject} in {layer} generates the SHORT-NAME '{emitted}' "
        f"({len(emitted)} characters), which is not a legal AUTOSAR Identifier: {reason}. "
        f"Rename '{name}' to at most {budget} characters — for example to "
        f"'{suggest_identifier(name, max_length=budget)}'"
        f"{_impact_hint(impact_target)}.{_stated_repair(stated_field)}"
    )


def _generated_pair_identifier_issue(
    subject: str,
    first: str,
    second: str,
    layer: str,
    emitted: str,
    added_length: int,
    reason: str,
    *,
    impact_target: str | None = None,
    stated_field: str | None = None,
) -> str:
    """The :func:`_generated_identifier_issue` twin for a form derived from **two** names.

    The two names have no budget each, only *added_length* characters fewer than
    the 128 **between them**, so the message states that joint budget and points
    the rename at the **longer** name; on a tie the second, the member, which a
    designer changes with less blast radius. The allowance is clamped to at least
    1, since two long names can overflow by more than either is long.

    Args:
        subject: what the pair names, e.g. ``"Interface 'I' data element 'e'"``.
        first: the first declared name.
        second: the second declared name; the one a tie points the rename at.
        layer: the layer declaring them, as :func:`_identifier_issue` takes it.
        emitted: the generated ``SHORT-NAME``, in its final shape.
        added_length: what the writer adds to *first* and *second* together.
        reason: the rule half that failed.
        impact_target: the element to point ``swcraft impact`` at.
        stated_field: as :func:`_generated_identifier_issue` takes it.
    """
    budget = MAX_IDENTIFIER_LENGTH - added_length
    longer = first if len(first) > len(second) else second
    allowance = max(1, len(longer) - max(0, len(emitted) - MAX_IDENTIFIER_LENGTH))
    return (
        f"{subject} in {layer} generates the SHORT-NAME '{emitted}' "
        f"({len(emitted)} characters), which is not a legal AUTOSAR Identifier: {reason}. "
        f"'{first}' and '{second}' get {budget} characters between them; rename "
        f"'{longer}' to at most {allowance} characters — for example to "
        f"'{suggest_identifier(longer, max_length=allowance)}'"
        f"{_impact_hint(impact_target)}.{_stated_repair(stated_field)}"
    )


def derived_event_names(swc: SWComponent) -> list[str]:
    """The name the writer derives for every event *swc* emits, in emission order, stated or not.

    A restatement of ``arxml_generation._unique_event_names`` (the writer imports
    the validator, not the reverse); the two must agree. The collision counter is
    **retroactive**: the first event with a base becomes ``{base}_0`` when a
    second wants it, so a name's length depends on its siblings. It runs over
    every event, a stated one included, so stating a name moves no other event's
    derived name; what an event is *written* as is :func:`emitted_event_names`.

    Returns:
        One pairwise distinct name per ``(runnable, event)`` pair, in emission order.
    """
    return unique_names([_event_base_name(runnable, event) for runnable in swc.runnables for event in runnable.events])


def _event_base_name(
    runnable: Runnable, event: TimingEvent | InitEvent | OperationEvent | DataEvent | CallReturnedEvent
) -> str:
    """The ``SHORT-NAME`` the writer derives for one event, before the collision counter.

    A timing and an init event are named for their runnable; an operation,
    data-received and call-returned event for the port and member it binds, so
    the name is stable under insertion. An unknown kind raises.
    """
    if isinstance(event, TimingEvent):
        return f"{GENERATED_TIMING_EVENT_PREFIX}{runnable.name}"
    if isinstance(event, InitEvent):
        return f"{GENERATED_INIT_EVENT_PREFIX}{runnable.name}"
    if isinstance(event, OperationEvent):
        return f"{GENERATED_OPERATION_EVENT_PREFIX}{event.port}_{event.operation}"
    if isinstance(event, DataEvent):
        return f"{GENERATED_DATA_EVENT_PREFIX}{event.port}_{event.element}"
    if isinstance(event, CallReturnedEvent):
        return f"{GENERATED_RETURNS_EVENT_PREFIX}{event.port}_{event.operation}"
    raise TypeError(f"an event kind the name derivation has not been taught: {type(event).__name__}")


def unique_names(base_names: list[str]) -> list[str]:
    """*base_names* made pairwise distinct by ``arxml_generation._unique_event_names``' rule."""
    names: list[str] = []
    taken: set[str] = set()

    def _free(base: str, start: int) -> str:
        index = start
        while f"{base}_{index}" in taken:
            index += 1
        return f"{base}_{index}"

    for base in base_names:
        prefix = f"{base}_"
        highest = 0
        has_index = False
        collision: int | None = None
        for position, existing in enumerate(names):
            if existing == base:
                collision = position
            elif existing.startswith(prefix) and existing[len(prefix) :].isdigit():
                has_index = True
                highest = max(highest, int(existing[len(prefix) :]))
        if collision is not None:
            taken.discard(names[collision])
            names[collision] = _free(base, 0)
            taken.add(names[collision])
        name = _free(base, highest + 1) if (has_index or collision is not None) else base
        names.append(name)
        taken.add(name)
    return names


def check_identifier_conformance(model_root: ModelInput) -> ConsistencyResult:
    """Report every model name that is not a legal AUTOSAR ``Identifier``.

    A ``SHORT-NAME`` and every ``*-REF`` path segment must match ``[a-zA-Z][a-zA-Z0-9_]*``, at most 128 characters
    (``IDENTIFIER--SIMPLE`` in both licensed schemas); without the licensed schema ``swcraft validate`` checks
    structure only, so this check is where such a name is reported.

    **Scope is the ARXML-bound names only**: components with their ports, runnables, variables and exclusive areas,
    compositions with their boundary ports, members and delegations, connectors, interfaces and their data elements /
    operations / arguments, data types and their enum literals / record fields, mapping sets, units, compu methods
    (and a texttable's entries), data constraints, and a declared ``package``. Requirements, tasks, sequences and
    hardware are out.

    **``identifier.generated`` covers the emitted form**: ``{enum}_CompuMethod``, ``{swc}_Implementation``,
    ``{swc}_InternalBehavior``, ``TMT_{runnable}``, ``IT_{runnable}``, ``{Interface}_{Element}_Init``,
    ``R_{port}_{element}``, ``W_{port}_{element}``, ``Call_{port}_{operation}`` and ``{app}_Unit``, ``{app}_Compu``,
    ``{app}_Range`` (a value type's ``_Range`` too), plus ``{runnable}_Budget`` where ``wcet`` is stated and
    ``RV_``/``WV_{variable}`` (budget 125). ``{swc}_Timing`` is never charged: the longer ``{swc}_Implementation``
    fails first. The data type forms are charged only where the field producing them is declared; the event names in
    their final, counter-suffixed shape. A form is checked only when its declared names conform, and only where no
    name is stated (``behavior_name``, an event's or an access entry's ``name``): the stated name is
    ``identifier.declared``'s. The init constant and the three access forms join two names with a *joint* budget
    (122 for the init constant, 125 for ``R_``/``W_``, 122 for ``Call_``), reserved whether or not the data element
    carries an ``init`` and whether or not an access entry resolves to a port.

    Returns:
        ConsistencyResult with consistent=True only when every emitted ``SHORT-NAME`` — declared or generated — is a
        legal AUTOSAR ``Identifier``.
    """
    view = as_view(model_root)
    records: list[ConsistencyIssue] = []
    report = _identifier_reporters(records, {layer.token: layer.name for layer in current_registry().layers})
    for swc in view.swcs:
        _swc_identifier_names(swc, report)
    _composition_identifier_names(view.compositions, report)
    _connector_identifier_names(view.connectors, report)
    for iface in view.interfaces:
        _interface_identifier_names(iface, report)
    description = view.data_description
    for dt in description.data_types:
        _data_type_identifier_names(dt, report)
    _mapping_set_identifier_names(view.mapping_sets, report)
    _description_form_identifier_names(description, report)
    return consistency_result(records)


class _DeclaredReport(Protocol):
    """Reports a declared *name* unless it conforms, and answers True when it does."""

    def __call__(self, subject: str, name: str, layer: str, at: Subject, *, impact_target: str | None = None) -> bool:
        """Report *name*, declared at *at* in *layer*, unless it is an AUTOSAR Identifier."""
        ...


class _GeneratedReport(Protocol):
    """Reports a form the writer derives from one conforming name unless it conforms."""

    def __call__(
        self,
        subject: str,
        name: str,
        layer: str,
        emitted: str,
        added_length: int,
        at: Subject,
        *,
        impact_target: str | None = None,
        stated_field: str | None = None,
    ) -> None:
        """Report *emitted*, derived from *name* by adding *added_length* characters, unless it conforms."""
        ...


class _GeneratedPairReport(Protocol):
    """Reports a form the writer derives from two conforming names unless it conforms."""

    def __call__(
        self,
        subject: str,
        first: str,
        second: str,
        layer: str,
        emitted: str,
        added_length: int,
        at: Subject,
        *,
        impact_target: str | None = None,
        stated_field: str | None = None,
    ) -> None:
        """Report *emitted*, derived from *first* and *second*, unless it conforms."""
        ...


class _IdentifierReport(NamedTuple):
    """The four report functions :func:`_identifier_reporters` returns, each appending to one record list.

    Attributes:
        declared: reports a declared name unless it conforms, and answers True when it does.
        generated: reports a form derived from one conforming name unless it conforms.
        generated_pair: reports a form derived from two conforming names unless it conforms.
        package: reports an element's declared ``package`` unless it is absent or conforms.
    """

    declared: _DeclaredReport
    generated: _GeneratedReport
    generated_pair: _GeneratedPairReport
    package: Callable[[str, str | None, str, Subject], None]


def _layer_of(layer_names: Mapping[str, str], layer: str) -> str:
    """The layer a message names for *layer*'s token — never a file or a directory.

    A message reaches the editing protocol's response, where a file path would
    expose the private staging directory. A token the registry does not declare
    names "the model" rather than raising.
    """
    if layer not in layer_names:
        return "the model"
    return f"the {layer_names[layer]} layer"


def _member_name(element: Subject, collection: str, name: str) -> Subject:
    """The address of one ``field``-identified member's own ``name``."""
    return element.at(field_step(collection), member_step(name), field_step("name"))


def _identifier_reporters(records: list[ConsistencyIssue], layer_names: Mapping[str, str]) -> _IdentifierReport:
    """The report functions of :func:`check_identifier_conformance`, each appending its finding to *records*.

    Args:
        records: the list every report appends to, in call order.
        layer_names: the registry's layer names by token, which :func:`_layer_of` reads.
    """
    # `binding.json` binds both identifier rules here: each emission literal stays in this function.

    def _report(subject: str, name: str, layer: str, at: Subject, *, impact_target: str | None = None) -> bool:
        """Report a *declared* name unless it conforms; True when it does.

        The generated-form branches read the answer: a form is checked only when
        its declared name conforms.

        Args:
            subject: what the name names, e.g. ``"SWC 'My SWC'"``.
            name: the declared name to check.
            layer: the layer's singular token (``layerDecl.token``).
            at: the model place the name is declared at — the element for its
                own name, the member's ``name`` field for a nested one.
            impact_target: passed through to :func:`_identifier_issue`.
        """
        reason = check_identifier(name)
        if reason is None:
            return True
        records.append(
            ConsistencyIssue(
                rule="identifier.declared",
                message=_identifier_issue(
                    subject, name, _layer_of(layer_names, layer), reason, impact_target=impact_target
                ),
                subjects=(at,),
            )
        )
        return False

    def _report_generated(
        subject: str,
        name: str,
        layer: str,
        emitted: str,
        added_length: int,
        at: Subject,
        *,
        impact_target: str | None = None,
        stated_field: str | None = None,
    ) -> None:
        """The :func:`_report` twin for a form the writer derives from a conforming *name*.

        Args:
            subject, name, layer, impact_target: as in :func:`_report`.
            emitted, added_length, stated_field: as :func:`_generated_identifier_issue` takes them.
        """
        reason = check_identifier(emitted)
        if reason is None:
            return
        records.append(
            ConsistencyIssue(
                rule="identifier.generated",
                message=_generated_identifier_issue(
                    subject,
                    name,
                    _layer_of(layer_names, layer),
                    emitted,
                    added_length,
                    reason,
                    impact_target=impact_target,
                    stated_field=stated_field,
                ),
                subjects=(at,),
            )
        )

    def _report_generated_pair(
        subject: str,
        first: str,
        second: str,
        layer: str,
        emitted: str,
        added_length: int,
        at: Subject,
        *,
        impact_target: str | None = None,
        stated_field: str | None = None,
    ) -> None:
        """The :func:`_report_generated` twin for a form derived from **two** names.

        Args:
            subject, layer, impact_target: as in :func:`_report`.
            first, second: the two declared names the form is derived from.
            emitted, added_length, stated_field: as
                :func:`_generated_pair_identifier_issue` takes them.
            at: the place that holds **both** names: the interface element for
                the init constant, the access entry for the access forms.
        """
        reason = check_identifier(emitted)
        if reason is None:
            return
        records.append(
            ConsistencyIssue(
                rule="identifier.generated",
                message=_generated_pair_identifier_issue(
                    subject,
                    first,
                    second,
                    _layer_of(layer_names, layer),
                    emitted,
                    added_length,
                    reason,
                    impact_target=impact_target,
                    stated_field=stated_field,
                ),
                subjects=(at,),
            )
        )

    def _report_package(owner: str, package: str | None, layer: str, element: Subject) -> None:
        """Report an element's declared ``package`` unless it is absent or conforms.

        The sub-package is emitted as a ``SHORT-NAME`` of its own, so it gets the
        full Identifier rule.
        """
        if package is not None:
            _report(f"{owner} package '{package}'", package, layer, element.at(field_step("package")))

    return _IdentifierReport(
        declared=_report,
        generated=_report_generated,
        generated_pair=_report_generated_pair,
        package=_report_package,
    )


def _swc_identifier_names(swc: SWComponent, report: _IdentifierReport) -> None:
    """Report one component's declared, stated and generated names, in the order the writer emits them."""
    swc_element = Subject("swc", swc.name)
    _swc_declared_names(swc, swc_element, report)
    _swc_stated_names(swc, swc_element, report)
    _swc_variable_access_names(swc, swc_element, report)
    _swc_access_point_names(swc, swc_element, report)
    _swc_result_point_names(swc, swc_element, report)
    _swc_event_names(swc, swc_element, report)


def _swc_declared_names(swc: SWComponent, swc_element: Subject, report: _IdentifierReport) -> None:
    """The component's package and name with its two forms, its ports, runnables, variables and exclusive areas."""
    report.package(f"SWC '{swc.name}'", swc.package, "swc", swc_element)
    if report.declared(f"SWC '{swc.name}'", swc.name, "swc", swc_element, impact_target=swc.name) and not (
        # An external stub emits no element, so neither generated form is charged.
        swc.external
    ):
        # A stated `behavior_name` replaces `_InternalBehavior`, so only an unstated one is charged.
        suffixes = (RESERVED_IMPLEMENTATION_SUFFIX,) + (
            (GENERATED_INTERNAL_BEHAVIOR_SUFFIX,) if swc.behavior_name is None else ()
        )
        for suffix in suffixes:
            report.generated(
                f"SWC '{swc.name}'",
                swc.name,
                "swc",
                f"{swc.name}{suffix}",
                len(suffix),
                swc_element,
                impact_target=swc.name,
                # `{swc}_Implementation` has no stated twin.
                stated_field=(
                    "the component's behavior_name" if suffix == GENERATED_INTERNAL_BEHAVIOR_SUFFIX else None
                ),
            )

    # A stub's port name is emitted as the last segment of every connector or
    # delegation end on it, and a reference path segment is an Identifier ([TPS_GST_00169]).
    for port in swc.ports:
        report.declared(
            f"SWC '{swc.name}' port '{port.name}'",
            port.name,
            "swc",
            _member_name(swc_element, "ports", port.name),
        )
    for runnable in swc.runnables:
        runnable_subject = _member_name(swc_element, "runnables", runnable.name)
        if (
            report.declared(f"SWC '{swc.name}' runnable '{runnable.name}'", runnable.name, "swc", runnable_subject)
            and runnable.wcet is not None
            and not swc.external
        ):
            # Charged only where `wcet` is stated, so a long runnable name is legal without one.
            report.generated(
                f"SWC '{swc.name}' runnable '{runnable.name}'",
                runnable.name,
                "swc",
                f"{runnable.name}{GENERATED_BUDGET_SUFFIX}",
                len(GENERATED_BUDGET_SUFFIX),
                runnable_subject,
            )
    for variable in swc.variables:
        report.declared(
            f"SWC '{swc.name}' variable '{variable.name}'",
            variable.name,
            "swc",
            _member_name(swc_element, "variables", variable.name),
        )
    # A `can_enter` or `runs_inside` entry mints no SHORT-NAME, so it is not judged.
    for area in swc.exclusive_areas:
        report.declared(
            f"SWC '{swc.name}' exclusive area '{area.name}'",
            area.name,
            "swc",
            _member_name(swc_element, "exclusive_areas", area.name),
        )


def _swc_stated_names(swc: SWComponent, swc_element: Subject, report: _IdentifierReport) -> None:
    """The names a component states: its behaviour's, an event's, an access point's."""
    if swc.behavior_name is not None:
        report.declared(
            f"SWC '{swc.name}' internal behaviour name",
            swc.behavior_name,
            "swc",
            swc_element.at(field_step("behavior_name")),
        )
    for runnable in swc.runnables:
        for event in runnable.events:
            if event.name is not None:
                report.declared(
                    f"SWC '{swc.name}' runnable '{runnable.name}' {EVENT_KIND_WORDS[event.type]} event name",
                    event.name,
                    "swc",
                    runnable_event_subject(swc_element, runnable.name, event_identity(event)),
                )
        for (list_name, port_name, member_field, member), stated in zip(
            access_entries(runnable), stated_access_point_names(runnable), strict=True
        ):
            if stated is None:
                continue
            report.declared(
                f"SWC '{swc.name}' runnable '{runnable.name}' {list_name} entry '{port_name}'.'{member}' name",
                stated,
                "swc",
                swc_element.at(
                    field_step("runnables"),
                    member_step(runnable.name),
                    field_step(list_name),
                    member_step({"port": port_name, member_field: member}),
                    field_step("name"),
                ),
            )


def _swc_variable_access_names(swc: SWComponent, swc_element: Subject, report: _IdentifierReport) -> None:
    """A variable access's stated name, or its derived ``RV_``/``WV_`` form."""
    # An illegal variable name is its own finding, or, for a name no variable
    # declares, `swc.variable-access.unknown-variable`'s.
    for runnable in swc.runnables:
        for list_name, variable_name, stated in variable_access_entries(runnable):
            access_entry = swc_element.at(
                field_step("runnables"),
                member_step(runnable.name),
                field_step(list_name),
                member_step(variable_name),
            )
            subject = f"SWC '{swc.name}' runnable '{runnable.name}' {list_name} entry '{variable_name}'"
            if stated is not None:
                report.declared(f"{subject} name", stated, "swc", access_entry.at(field_step("name")))
                continue
            if check_identifier(variable_name) is not None:
                continue  # one root cause, one issue
            prefix = GENERATED_VARIABLE_ACCESS_PREFIXES[list_name]
            report.generated(
                subject,
                variable_name,
                "swc",
                f"{prefix}{variable_name}",
                len(prefix),
                access_entry,
                impact_target=swc.name,
                stated_field="the entry's name",
            )


def _swc_access_point_names(swc: SWComponent, swc_element: Subject, report: _IdentifierReport) -> None:
    """The derived ``R_``/``W_``/``Call_`` access-point forms, a joint budget each."""
    # Checked for every declared entry, resolved or not: the entry itself makes the name exist.
    for runnable in swc.runnables:
        for (list_name, port_name, member_field, member), stated in zip(
            access_entries(runnable), stated_access_point_names(runnable), strict=True
        ):
            if stated is not None:
                continue
            if check_identifier(port_name) is not None or check_identifier(member) is not None:
                continue  # already reported as a declared name; one root cause, one issue
            prefix = GENERATED_ACCESS_PREFIXES[list_name]
            access_entry = swc_element.at(
                field_step("runnables"),
                member_step(runnable.name),
                field_step(list_name),
                # The composite identity holds *both* names, which a pair's repair has to reach.
                member_step({"port": port_name, member_field: member}),
            )
            report.generated_pair(
                f"SWC '{swc.name}' runnable '{runnable.name}' {list_name} entry '{port_name}'.'{member}'",
                port_name,
                member,
                "swc",
                generated_access_point(prefix, port_name, member),
                _generated_access_added_length(prefix),
                access_entry,
                impact_target=swc.name,
                stated_field="the entry's name",
            )


def _swc_result_point_names(swc: SWComponent, swc_element: Subject, report: _IdentifierReport) -> None:
    """A result point's stated name, or its derived ``Result_`` form."""
    for runnable in swc.runnables:
        for subject, port_name, operation, stated, steps, stated_field in result_point_sites(swc.name, runnable):
            result_at = swc_element.at(*steps)
            if stated is not None:
                report.declared(f"{subject} name", stated, "swc", result_at)
            elif check_identifier(port_name) is None and check_identifier(operation) is None:
                report.generated_pair(
                    subject,
                    port_name,
                    operation,
                    "swc",
                    derived_result_point_name(port_name, operation),
                    _generated_access_added_length(GENERATED_RESULT_POINT_PREFIX),
                    result_at,
                    impact_target=swc.name,
                    stated_field=stated_field,
                )


def _swc_event_names(swc: SWComponent, swc_element: Subject, report: _IdentifierReport) -> None:
    """The derived event names, in their final shape after the writer's counter."""
    pairs = [(runnable, event) for runnable in swc.runnables for event in runnable.events]
    for (runnable, event), emitted in zip(pairs, derived_event_names(swc), strict=True):
        if event.name is not None:
            continue
        if isinstance(event, OperationEvent | DataEvent | CallReturnedEvent):
            # The overhead is read off the FINAL name: two pairs can derive one
            # base (`(A_B, C)` and `(A, B_C)`), and the counter lengthens both.
            if isinstance(event, OperationEvent):
                member, words = event.operation, "operation event"
            elif isinstance(event, CallReturnedEvent):
                member, words = event.operation, "call-returned event"
            else:
                member, words = event.element, "data-received event"
            if check_identifier(event.port) is not None or check_identifier(member) is not None:
                continue  # the declared name is the one to fix; one root cause, one issue
            report.generated_pair(
                f"SWC '{swc.name}' runnable '{runnable.name}' {words} '{event.port}'.'{member}'",
                event.port,
                member,
                "swc",
                emitted,
                len(emitted) - len(event.port) - len(member),
                runnable_event_subject(swc_element, runnable.name, event_identity(event)),
                impact_target=swc.name,
                stated_field="the event's name",
            )
            continue
        if check_identifier(runnable.name) is not None:
            continue  # already reported as a runnable name; one root cause, one issue
        report.generated(
            f"SWC '{swc.name}' runnable '{runnable.name}'",
            runnable.name,
            "swc",
            emitted,
            len(emitted) - len(runnable.name),
            # The runnable's name: the event's SHORT-NAME is derived from it.
            _member_name(swc_element, "runnables", runnable.name),
            stated_field="the event's name",
        )


def _composition_identifier_names(compositions: Sequence[Composition], report: _IdentifierReport) -> None:
    """Each composition's name and package, and its boundary ports', members' and delegations' names."""
    # No generated form lengthens a composition's names, so `rule:identifier.generated`
    # must not name this layer. A member's `type` and a delegation's references are
    # `*-REF` paths into names other files declare, checked where they are declared.
    for composition in compositions:
        composition_element = Subject("compositions", composition.name)
        report.declared(
            f"Composition '{composition.name}'",
            composition.name,
            "composition",
            composition_element,
            impact_target=composition.name,
        )
        report.package(f"Composition '{composition.name}'", composition.package, "composition", composition_element)
        for boundary_port in composition.ports:
            report.declared(
                f"Composition '{composition.name}' port '{boundary_port.name}'",
                boundary_port.name,
                "composition",
                _member_name(composition_element, "ports", boundary_port.name),
            )
        for member in composition.members:
            report.declared(
                f"Composition '{composition.name}' member '{member.name}'",
                member.name,
                "composition",
                _member_name(composition_element, "members", member.name),
            )
        for delegation in composition.delegations:
            report.declared(
                f"Composition '{composition.name}' delegation '{delegation.name}'",
                delegation.name,
                "composition",
                _member_name(composition_element, "delegations", delegation.name),
            )


def _connector_identifier_names(connectors: Sequence[Connector], report: _IdentifierReport) -> None:
    """Each connector's name."""
    for connector in connectors:
        report.declared(
            f"Connector '{connector.name}'",
            connector.name,
            "connector",
            Subject("connectors", connector.name),
            impact_target=connector.name,
        )


def _interface_identifier_names(iface: Interface, report: _IdentifierReport) -> None:
    """An interface's name and package, its stub members, and its data elements, operations and arguments."""
    interface_element = Subject("interfaces", iface.name)
    report.declared(
        f"Interface '{iface.name}'",
        iface.name,
        "interface",
        interface_element,
        impact_target=iface.name,
    )
    if isinstance(iface, (SRInterface, CSInterface)):  # an external stub has no `package`
        report.package(f"Interface '{iface.name}'", iface.package, "interface", interface_element)
    if isinstance(iface, ExternalInterface):
        # A stub's listed member names are written as the last segment of a member path.
        for member_field, (collection, noun, _dest) in STUB_MEMBER_LISTS.items():
            for member in stub_member_list(iface, member_field):
                report.declared(
                    f"External interface stub '{iface.name}' {noun} '{member}'",
                    member,
                    "interface",
                    interface_element.at(field_step(collection), member_step(member)),
                )
    if isinstance(iface, SRInterface):
        interface_conforms = check_identifier(iface.name) is None
        for data_element in iface.data_elements:
            element_conforms = report.declared(
                f"Interface '{iface.name}' data element '{data_element.name}'",
                data_element.name,
                "interface",
                _member_name(interface_element, "data_elements", data_element.name),
            )
            # Reserved whether or not this element carries an `init`.
            if interface_conforms and element_conforms:
                report.generated_pair(
                    f"Interface '{iface.name}' data element '{data_element.name}'",
                    iface.name,
                    data_element.name,
                    "interface",
                    generated_init_constant(iface.name, data_element.name),
                    GENERATED_INIT_CONSTANT_ADDED_LENGTH,
                    interface_element,
                    impact_target=iface.name,
                )
    elif isinstance(iface, CSInterface):
        for operation in iface.operations:
            report.declared(
                f"Interface '{iface.name}' operation '{operation.name}'",
                operation.name,
                "interface",
                _member_name(interface_element, "operations", operation.name),
            )
            for argument in operation.arguments:
                report.declared(
                    f"Interface '{iface.name}' operation '{operation.name}' argument '{argument.name}'",
                    argument.name,
                    "interface",
                    interface_element.at(
                        field_step("operations"),
                        member_step(operation.name),
                        field_step("arguments"),
                        member_step(argument.name),
                        field_step("name"),
                    ),
                )


def _data_type_identifier_names(dt: DataType, report: _IdentifierReport) -> None:
    """A data type's name, package and generated forms, and its literals', fields' and elements' names."""
    if isinstance(dt, BaseDataType):
        base_type_element = Subject("base_types", dt.name)
        report.declared(f"Data type '{dt.name}'", dt.name, "base_type", base_type_element, impact_target=dt.name)
        report.package(f"Data type '{dt.name}'", dt.package, "base_type", base_type_element)
        return
    application_side = isinstance(dt, APPLICATION_DATA_TYPE_CLASSES)
    data_type_element = Subject("application_types" if application_side else "implementation_types", dt.name)
    # One literal call per token, because the scope scan reads the literal.
    token = "application_type" if application_side else "implementation_type"
    if application_side:
        reported = report.declared(
            f"Data type '{dt.name}'", dt.name, "application_type", data_type_element, impact_target=dt.name
        )
    else:
        reported = report.declared(
            f"Data type '{dt.name}'", dt.name, "implementation_type", data_type_element, impact_target=dt.name
        )
    if reported:
        suffixes: list[str] = []
        if generates_compu_method(dt):
            suffixes.append(GENERATED_COMPU_METHOD_SUFFIX)
        # Reserved per declared FIELD, not per kind: a type with no `unit` emits no
        # `{name}_Unit`. Ordered unit, conversion, range whatever the declaration order.
        generated = generated_data_type_forms(dt)
        suffixes.extend(suffix for suffix in APPLICATION_FORM_RULES if suffix in generated)
        for suffix in suffixes:
            report.generated(
                f"Data type '{dt.name}'",
                dt.name,
                token,
                f"{dt.name}{suffix}",
                len(suffix),
                data_type_element,
                impact_target=dt.name,
            )
    if not isinstance(dt, ExternalDataType):  # an external stub has no `package`
        report.package(f"Data type '{dt.name}'", dt.package, token, data_type_element)

    if isinstance(dt, EnumDataType):
        for literal in dt.literals or ():
            report.declared(
                f"Enum '{dt.name}' literal '{literal.name}'",
                literal.name,
                token,
                _member_name(data_type_element, "literals", literal.name),
            )
    elif isinstance(dt, RecordDataType):
        for record_field in dt.fields:
            report.declared(
                f"Record '{dt.name}' field '{record_field.name}'",
                record_field.name,
                token,
                _member_name(data_type_element, "fields", record_field.name),
            )
    elif isinstance(dt, ImplementationRecordDataType):
        for implementation_element in dt.elements:
            report.declared(
                f"Implementation record '{dt.name}' element '{implementation_element.name}'",
                implementation_element.name,
                token,
                _member_name(data_type_element, "elements", implementation_element.name),
            )
    elif isinstance(dt, ImplementationArrayDataType):
        # The array's one sub-element carries `element_name` as its SHORT-NAME.
        report.declared(
            f"Implementation array '{dt.name}' element name '{dt.element_name}'",
            dt.element_name,
            token,
            data_type_element.at(field_step("element_name")),
        )


def _mapping_set_identifier_names(mapping_sets: Sequence[MappingSet], report: _IdentifierReport) -> None:
    """Each mapping set's name and package."""
    # A map's two sides are `*-REF` paths, checked where they are declared. No
    # generated form lengthens the set's name, so `rule:identifier.generated`
    # does not name this layer.
    for mapping_set in mapping_sets:
        report.declared(
            f"Mapping set '{mapping_set.name}'",
            mapping_set.name,
            "mapping_set",
            Subject("mapping_sets", mapping_set.name),
            impact_target=mapping_set.name,
        )
        report.package(
            f"Mapping set '{mapping_set.name}'",
            mapping_set.package,
            "mapping_set",
            Subject("mapping_sets", mapping_set.name),
        )


def _description_form_identifier_names(description: DataDescription, report: _IdentifierReport) -> None:
    """Each unit's, compu method's (and texttable entry's) and data constraint's name and package."""
    # A texttable's entry name is emitted as its scale's `SHORT-LABEL` and `VT`,
    # as an enumeration literal's. No generated form lengthens these names, so
    # `rule:identifier.generated` does not name the three layers.
    for unit in description.units:
        unit_element = Subject("units", unit.name)
        report.declared(f"Unit '{unit.name}'", unit.name, "unit", unit_element, impact_target=unit.name)
        report.package(f"Unit '{unit.name}'", unit.package, "unit", unit_element)
    for compu_method in description.compu_methods:
        compu_method_element = Subject("compu_methods", compu_method.name)
        report.declared(
            f"Compu method '{compu_method.name}'",
            compu_method.name,
            "compu_method",
            compu_method_element,
            impact_target=compu_method.name,
        )
        report.package(
            f"Compu method '{compu_method.name}'", compu_method.package, "compu_method", compu_method_element
        )
        if isinstance(compu_method, TextTable):
            for entry in compu_method.entries:
                report.declared(
                    f"Compu method '{compu_method.name}' entry '{entry.name}'",
                    entry.name,
                    "compu_method",
                    _member_name(compu_method_element, "entries", entry.name),
                )
    for data_constr in description.data_constraints:
        data_constr_element = Subject("data_constraints", data_constr.name)
        report.declared(
            f"Data constraint '{data_constr.name}'",
            data_constr.name,
            "data_constraint",
            data_constr_element,
            impact_target=data_constr.name,
        )
        report.package(
            f"Data constraint '{data_constr.name}'", data_constr.package, "data_constraint", data_constr_element
        )


def runnable_event_subject(owner: Subject, runnable_name: str, value: Mapping[str, object]) -> Subject:
    """One event of *owner*'s runnable, of any kind, addressed as the whole value it is (:func:`event_identity`)."""
    return owner.at(field_step("runnables"), member_step(runnable_name), field_step("events"), member_step(value))


def _completely_resolved(resolved: ResolvedType) -> bool:
    """Whether :func:`_types_compatible` can judge *resolved* all the way down.

    ``_types_compatible`` answers ``None`` for *compatible* and for an
    abstention alike, so this is asked first: only an **implementation**
    structure is judged. An ``application_type`` or an ``enum`` is never
    complete, at the top or nested: ``[constr_2000]`` compares implementation
    data types, and a physical type reaches one only through a mapping. A
    record, array or pointer is complete when every member, the element or the
    target is; an application composite's members are application kinds, so it
    is never complete.
    """
    if resolved.kind == "value_type":
        return True
    if resolved.kind in {"record", "implementation_record"}:
        return all(_completely_resolved(field_type) for _name, _type_name, field_type in resolved.fields)
    if resolved.kind in {"array", "implementation_array"}:
        return resolved.element is not None and _completely_resolved(resolved.element[1])
    if resolved.kind == "pointer":
        return resolved.target is not None and _completely_resolved(resolved.target[1])
    return False


def _signature_position(
    argument: CSArgument,
    pairings: Mapping[str, Mapping[str, Sequence[str]]] | None,
    data_types: DataDescription,
    memo: dict[tuple[bool, str], ResolvedType | None],
) -> tuple[str, ResolvedType] | None:
    """One argument's implementation type, resolved completely — or ``None``, *not judged*.

    An application type or an enumeration is replaced by the one implementation
    type the component's mapping sets pair it with; *pairings* is ``None`` when
    those sets cannot be read whole (a set the model does not declare, or one
    declared twice), and then no such argument is judged. A record or an array
    with a map resolves through it; one without is compared by its own shape,
    so an unmapped application composite is not judged. Missing, ambiguous,
    unresolved or incomplete all answer ``None``: never *compatible*.
    """
    type_name, application = prototype_type_ref(argument)
    declared = declared_type(data_types, type_name, application=application)
    if declared is None or type_name is None:
        return None
    name = type_name
    if isinstance(declared, (RecordDataType, ArrayDataType)):
        onto = None if pairings is None else pairings.get(type_name)
        if onto is not None:
            if len(onto) != 1:
                return None
            (name,) = onto
            application = False
    elif isinstance(declared, MAP_APPLICATION_KINDS):
        onto = None if pairings is None else pairings.get(type_name)
        if onto is None or len(onto) != 1:
            return None
        (name,) = onto
        application = False
    resolved = resolve_type(name, data_types, memo, application=application)
    if resolved is None or not _completely_resolved(resolved):
        return None
    return name, resolved


def _signature_difference(
    first: CSOperation,
    second: CSOperation,
    pairings: Mapping[str, Mapping[str, Sequence[str]]] | None,
    data_types: DataDescription,
    memo: dict[tuple[bool, str], ResolvedType | None],
) -> str | None:
    """The first way *first* and *second* differ, as far as the comparison judges, or ``None``.

    The ordered argument count, then position by position the direction and the
    structure of the resolved implementation type (:func:`_types_compatible`,
    asked only after :func:`_signature_position` found both sides complete). A
    position that is not judged is passed over, and a later judged one is still
    compared. ``None`` therefore means *no difference the comparison judges*,
    which is weaker than *compatible*: possible errors, the return value,
    ``errorHandling``, ``serverArgumentImplPolicy`` and
    ``PortDefinedArgumentValues`` are not read at all.
    """
    if len(first.arguments) != len(second.arguments):
        return f"argument count {len(first.arguments)} vs {len(second.arguments)}"
    for position, (argument_a, argument_b) in enumerate(zip(first.arguments, second.arguments), start=1):
        if argument_a.direction != argument_b.direction:
            return f"argument {position} direction {argument_a.direction} vs {argument_b.direction}"
        side_a, side_b = (
            _signature_position(argument, pairings, data_types, memo) for argument in (argument_a, argument_b)
        )
        if side_a is None or side_b is None or side_a[0] == side_b[0]:
            continue  # not judged, or one declaration on both sides
        difference = type_pair_difference(side_a[0], side_a[1], side_b[0], side_b[1])
        if difference is not None:
            return f"argument {position} {difference}"
    return None


def _event_runnables(names: list[str]) -> str:
    """``the operation event of runnable 'A' names`` / ``the operation events of runnables 'A' and 'B' name``."""
    noun = "event" if len(names) == 1 else "events"
    verb = "names" if len(names) == 1 else "name"
    runnables = "runnable" if len(set(names)) == 1 else "runnables"
    return f"the operation {noun} of {runnables} {quoted_list(names)} {verb}"


def operation_event_records(
    swcs: Sequence[SWComponent],
    interfaces: Sequence[Interface],
    type_map: DataDescription,
    mapping_sets: Sequence[MappingSet],
) -> list[ConsistencyIssue]:
    """Every operation event against the port and operation it names.

    An operation event says a client's call of *operation* through the component's own *port* starts the runnable.
    Mints ``swc.operation-event.unknown-port``, ``swc.operation-event.interface-kind-mismatch``,
    ``swc.operation-event.direction-mismatch``, ``swc.operation-event.unknown-operation``,
    ``swc.operation-event.duplicate-binding`` and ``swc.operation-event.incompatible-signatures``; the model
    contract describes each.

    Beyond the contract: an unknown port is one finding per port, carrying every event naming it, and no further
    finding for any of them; a kind mismatch is reported instead of an operation finding, a direction mismatch
    beside it; a stub's operation list is decided before the kind; a duplicate binding is decided on the names,
    resolved or not. The signature rule (``[constr_2000]``, partial) compares each operation with every earlier one
    no finding names (:func:`_signature_difference`), one finding per operation that differs, naming the earliest;
    only events the first four rules leave silent take part, and an operation a stub lists takes none.

    Not judged: mixed event kinds, a provided operation no runnable serves, and the queue length. A port whose
    ``interface`` is undeclared is ``port.undeclared-interface``'s and its events are skipped; a duplicated port name
    resolves to the first declared.

    Args:
        swcs: The components whose operation events are judged.
        interfaces: The interfaces their ports name; the first declaration of a name wins.
        type_map: The data description the argument types resolve in.
        mapping_sets: The mapping sets the components' application types are paired through.

    Returns:
        One record per finding.
    """
    # The first declaration wins; `name.duplicate-interface` owns the second.
    declared_interfaces: dict[str, Interface] = {}
    for interface in interfaces:
        declared_interfaces.setdefault(interface.name, interface)
    set_map: dict[str, MappingSet] = {mapping_set.name: mapping_set for mapping_set in mapping_sets}
    duplicated_sets = frozenset(duplicates([mapping_set.name for mapping_set in mapping_sets]))
    memo: dict[tuple[bool, str], ResolvedType | None] = {}

    records: list[ConsistencyIssue] = []
    for swc in swcs:
        walk_records, walk = _operation_event_walk_records(swc, declared_interfaces)
        records.extend(walk_records)
        records.extend(_operation_event_port_records(swc, walk.unresolved))
        records.extend(_operation_event_binding_records(swc, walk.bindings))
        # An undeclared or duplicated set is another rule's finding; then no mapped position is judged.
        readable = not any(name in duplicated_sets or name not in set_map for name in swc.data_type_mappings)
        pairings = application_type_pairings(swc.data_type_mappings, set_map) if readable else None
        records.extend(_operation_signature_records(swc, walk.served, pairings, type_map, memo))

    return records


#: One operation event as the walk keeps it: its runnable and its whole-value identity.
_EventOccurrence = tuple[str, Mapping[str, object]]

#: One cleanly resolved operation event a runnable serves: its identity, its interface, its operation.
_ServedOperation = tuple[Mapping[str, object], str, CSOperation]


@dataclass(frozen=True)
class _OperationEventWalk:
    """What one component's walk of its operation events hands the later phases of :func:`operation_event_records`.

    Attributes:
        unresolved: port name -> every (runnable, event) naming it, in walk order (rule 1).
        bindings: (port, operation) -> every (runnable, event) naming the pair (rule 5).
        served: runnable -> every cleanly resolved (event, interface, operation) (rule 6).
    """

    unresolved: dict[str, list[_EventOccurrence]]
    bindings: dict[tuple[str, str], list[_EventOccurrence]]
    served: dict[str, list[_ServedOperation]]


def _operation_event_walk_records(
    swc: SWComponent, declared_interfaces: Mapping[str, Interface]
) -> tuple[list[ConsistencyIssue], _OperationEventWalk]:
    """Rules 2-4 of :func:`operation_event_records` over *swc*'s operation events, and the walk the later rules read."""
    owner = Subject("swc", swc.name)
    ports: dict[str, Port] = {}
    for declared_port in swc.ports:
        ports.setdefault(declared_port.name, declared_port)
    walk = _OperationEventWalk(unresolved={}, bindings={}, served={})
    records: list[ConsistencyIssue] = []

    for runnable in swc.runnables:
        for event in runnable.events:
            if not isinstance(event, OperationEvent):
                continue  # timing and init events: nothing here to resolve
            value = event_identity(event)
            walk.bindings.setdefault((event.port, event.operation), []).append((runnable.name, value))
            port = ports.get(event.port)
            if port is None:
                walk.unresolved.setdefault(event.port, []).append((runnable.name, value))
                continue
            if port.interface is None or port.interface not in declared_interfaces:
                continue  # port.undeclared-interface
            declared = declared_interfaces[port.interface]
            event_subject = runnable_event_subject(owner, runnable.name, value)
            port_subject = owner.at(field_step("ports"), member_step(port.name))
            # A stub's kind is never compared: the event resolves against its `operations`.
            stub = declared if isinstance(declared, ExternalInterface) else None
            if isinstance(declared, SRInterface):
                records.append(
                    ConsistencyIssue(
                        rule="swc.operation-event.interface-kind-mismatch",
                        message=(
                            f"SWC '{swc.name}' runnable '{runnable.name}' has an operation event through port "
                            f"'{port.name}', whose interface '{port.interface}' is "
                            f"{INTERFACE_KIND_PROSE[declared.kind]}; an operation event needs a "
                            f"{INTERFACE_KIND_PROSE['client_server']} port."
                        ),
                        # The two legal repairs: the event names another port, or the port names a
                        # client/server interface.
                        subjects=(event_subject, port_subject.at(field_step("interface"))),
                    )
                )
                continue
            provides = port.direction == "provide"
            if not provides:
                records.append(
                    ConsistencyIssue(
                        rule="swc.operation-event.direction-mismatch",
                        message=(
                            f"SWC '{swc.name}' runnable '{runnable.name}' has an operation event through port "
                            f"'{port.name}', which is a {port.direction} port; an operation event needs a "
                            "provide port: a call starts a runnable of the component that provides the operation."
                        ),
                        subjects=(event_subject, port_subject.at(field_step("direction"))),
                    )
                )
            if stub is not None and event.operation in stub_member_names(stub, "operation"):
                # Resolves, but joins no signature comparison: the stub holds no arguments.
                continue
            operation = (
                None
                if isinstance(declared, ExternalInterface)
                else next((entry for entry in declared.operations if entry.name == event.operation), None)
            )
            if operation is None:
                naming = (
                    f"SWC '{swc.name}' runnable '{runnable.name}' has an operation event naming operation "
                    f"'{event.operation}' of port '{port.name}'"
                )
                if stub is not None:
                    message = f"{naming}, but {stub_member_miss(stub, 'operation', event.operation)}."
                else:
                    message = f"{naming}, which interface '{port.interface}' does not declare."
                records.append(
                    ConsistencyIssue(
                        rule="swc.operation-event.unknown-operation",
                        message=message,
                        subjects=(event_subject,),
                        target=f"{port.interface}.{event.operation}",
                    )
                )
                continue
            if provides:
                walk.served.setdefault(runnable.name, []).append((value, port.interface, operation))
    return records, walk


def _operation_event_port_records(
    swc: SWComponent, unresolved: Mapping[str, Sequence[_EventOccurrence]]
) -> list[ConsistencyIssue]:
    """Rule 1 of :func:`operation_event_records`: one finding per port of *swc* its operation events name undeclared."""
    owner = Subject("swc", swc.name)
    records: list[ConsistencyIssue] = []
    for port_name, referrers in unresolved.items():
        count = len(referrers)
        records.append(
            ConsistencyIssue(
                rule="swc.operation-event.unknown-port",
                message=(
                    f"SWC '{swc.name}' declares no port '{port_name}', which "
                    f"{_event_runnables([name for name, _value in referrers])} "
                    f"({count} {'event' if count == 1 else 'events'})."
                ),
                # One repair, so one finding with every referring event on it.
                subjects=tuple(runnable_event_subject(owner, name, value) for name, value in referrers),
                target=f"{swc.name}.{port_name}",
            )
        )
    return records


def _operation_event_binding_records(
    swc: SWComponent, bindings: Mapping[tuple[str, str], Sequence[_EventOccurrence]]
) -> list[ConsistencyIssue]:
    """Rule 5 of :func:`operation_event_records`: one finding per ``(port, operation)`` pair *swc* binds twice."""
    owner = Subject("swc", swc.name)
    records: list[ConsistencyIssue] = []
    for (port_name, operation_name), occurrences in bindings.items():
        if len(occurrences) < 2:
            continue
        runnable_names = list(dict.fromkeys(name for name, _value in occurrences))
        where = (
            f"twice on runnable '{runnable_names[0]}'"
            if len(runnable_names) == 1
            else f"on runnables {quoted_list(runnable_names)}"
        )
        records.append(
            ConsistencyIssue(
                rule="swc.operation-event.duplicate-binding",
                message=(
                    f"SWC '{swc.name}' binds operation '{operation_name}' of port '{port_name}' in "
                    f"{len(occurrences)} operation events, {where}; a call starts one runnable, so each "
                    "(port, operation) pair is named once."
                ),
                subjects=tuple(runnable_event_subject(owner, name, value) for name, value in occurrences),
            )
        )
    return records


def _operation_signature_records(
    swc: SWComponent,
    served: Mapping[str, Sequence[_ServedOperation]],
    pairings: Mapping[str, Mapping[str, Sequence[str]]] | None,
    type_map: DataDescription,
    memo: dict[tuple[bool, str], ResolvedType | None],
) -> list[ConsistencyIssue]:
    """Rule 6 of :func:`operation_event_records` over the operations each runnable of *swc* serves."""
    owner = Subject("swc", swc.name)
    records: list[ConsistencyIssue] = []
    for runnable_name, operations in served.items():
        # Two ports on one interface reach one signature.
        compared: dict[tuple[str, str], _ServedOperation] = {}
        for value, interface_name, entry in operations:
            compared.setdefault((interface_name, entry.name), (value, interface_name, entry))
        # Compared against every earlier agreeing operation, not one fixed reference,
        # so an unjudged position of the first cannot shield two later operations.
        agreeing: list[_ServedOperation] = []
        for value, interface_name, entry in compared.values():
            found = next(
                (
                    (earlier, difference)
                    for earlier in agreeing
                    if (difference := _signature_difference(earlier[2], entry, pairings, type_map, memo)) is not None
                ),
                None,
            )
            if found is None:
                agreeing.append((value, interface_name, entry))
                continue
            (first_value, first_interface, first_operation), difference = found
            records.append(
                ConsistencyIssue(
                    rule="swc.operation-event.incompatible-signatures",
                    message=(
                        f"SWC '{swc.name}' runnable '{runnable_name}' is started by operation events whose "
                        f"operations differ: '{first_interface}.{first_operation.name}' vs "
                        f"'{interface_name}.{entry.name}' — {difference}. One runnable serves every operation "
                        "that starts it through one signature, so their arguments must agree position by "
                        "position ([constr_2000]); possible errors and the return value are not compared."
                    ),
                    subjects=(
                        runnable_event_subject(owner, runnable_name, first_value),
                        runnable_event_subject(owner, runnable_name, value),
                    ),
                )
            )
    return records


def check_operation_events(model_root: ModelInput) -> ConsistencyResult:
    """Check every operation event against the port and operation it names.

    :func:`operation_event_records` over the model; it documents the six ``swc.operation-event.*`` rules.

    Returns:
        ConsistencyResult with consistent=True only when no rule fires.
    """
    view = as_view(model_root)
    return consistency_result(
        operation_event_records(
            view.swcs,
            view.interfaces,
            view.data_description,
            view.mapping_sets,
        )
    )


# ---------------------------------------------------------------------------
# check_data_events
# ---------------------------------------------------------------------------


def data_event_records(swcs: Sequence[SWComponent], interfaces: Sequence[Interface]) -> list[ConsistencyIssue]:
    """Every data-received event against the port and data element it names.

    A data-received event says the arrival of a value of *element* through the component's own *port* starts the
    runnable. **One finding per unresolved event**, decided in this order and stopping at the first:

    1. ``swc.data-event.unknown-port`` — the port is not declared on the event's **own** component
       (``[constr_2530]``).
    2. ``swc.data-event.direction-mismatch`` — the port is a provide port (``CONTEXT-R-PORT-REF`` admits only a
       required port prototype).
    3. ``swc.data-event.interface-kind-mismatch`` — the port's interface is client/server, or an external interface
       stub whose ``external_dest`` is not ``SENDER-RECEIVER-INTERFACE`` (a mode-switch stub included).
    4. ``swc.data-event.unknown-element`` — the interface declares no such data element, or the sender/receiver stub
       does not list it in its ``data_elements``.

    A port whose ``interface`` is absent or undeclared is ``port.undeclared-interface``'s finding, its events skipped
    after rule 2; a duplicated port name resolves to the first declared. Queued reception across runnables
    (``[SWS_Rte_07403]``) is not judged. Silent by design: an event beside another event, one repeated on one
    runnable, one on an element its runnable does not read, and one on a require port no connector feeds. An
    external component stub declares no runnable. The in-memory core of :func:`check_data_events`.

    Returns:
        One record per finding.
    """
    declared_interfaces: dict[str, Interface] = {}
    for interface in interfaces:
        declared_interfaces.setdefault(interface.name, interface)
    sender_receiver_dest = STUB_MEMBER_DEST["data_elements"]

    records: list[ConsistencyIssue] = []
    for swc in swcs:
        if swc.external:
            continue  # a component stub declares no runnable (swc.external-with-ports)
        owner = Subject("swc", swc.name)
        ports: dict[str, Port] = {}
        for declared_port in swc.ports:
            ports.setdefault(declared_port.name, declared_port)
        for runnable in swc.runnables:
            for event in runnable.events:
                if not isinstance(event, DataEvent):
                    continue  # timing, init and operation events: nothing here to resolve
                event_subject = runnable_event_subject(owner, runnable.name, event_identity(event))
                naming = f"SWC '{swc.name}' runnable '{runnable.name}' has a data-received event"
                port = ports.get(event.port)
                if port is None:
                    records.append(
                        ConsistencyIssue(
                            rule="swc.data-event.unknown-port",
                            message=(
                                f"{naming} naming port '{event.port}', which the component does not declare; the "
                                "port of a data-received event is one of its own component's ports."
                            ),
                            subjects=(event_subject.at(field_step("port")),),
                            target=f"{swc.name}.{event.port}",
                        )
                    )
                    continue
                port_subject = owner.at(field_step("ports"), member_step(port.name))
                if port.direction != "require":
                    records.append(
                        ConsistencyIssue(
                            rule="swc.data-event.direction-mismatch",
                            message=(
                                f"{naming} through port '{port.name}', which is a {port.direction} port; a "
                                "data-received event needs a require port: a value arrives through a require port, "
                                "and the event's CONTEXT-R-PORT-REF names a required port."
                            ),
                            # The two legal repairs: the event names another port, or the port faces the other way.
                            subjects=(event_subject, port_subject.at(field_step("direction"))),
                        )
                    )
                    continue
                declared = declared_interfaces.get(port.interface) if port.interface is not None else None
                if declared is None:
                    continue  # port.undeclared-interface
                if isinstance(declared, ExternalInterface):
                    if declared.external_dest != sender_receiver_dest:
                        records.append(
                            ConsistencyIssue(
                                rule="swc.data-event.interface-kind-mismatch",
                                message=(
                                    f"{naming} through port '{port.name}', whose interface '{port.interface}' is an "
                                    f"external interface stub with external_dest '{declared.external_dest}'; a "
                                    f"data-received event needs a {INTERFACE_KIND_PROSE['sender_receiver']} port."
                                ),
                                subjects=(event_subject, port_subject.at(field_step("interface"))),
                            )
                        )
                        continue
                    if event.element in stub_member_names(declared, "element"):
                        continue
                    records.append(
                        ConsistencyIssue(
                            rule="swc.data-event.unknown-element",
                            message=(
                                f"{naming} naming element '{event.element}' of port '{port.name}', but "
                                f"{stub_member_miss(declared, 'element', event.element)}."
                            ),
                            subjects=(event_subject.at(field_step("element")),),
                            target=f"{port.interface}.{event.element}",
                        )
                    )
                    continue
                if not isinstance(declared, SRInterface):
                    records.append(
                        ConsistencyIssue(
                            rule="swc.data-event.interface-kind-mismatch",
                            message=(
                                f"{naming} through port '{port.name}', whose interface '{port.interface}' is "
                                f"{INTERFACE_KIND_PROSE[declared.kind]}; a data-received event needs a "
                                f"{INTERFACE_KIND_PROSE['sender_receiver']} port."
                            ),
                            # The two legal repairs: the event names another port, or the port names a
                            # sender/receiver interface.
                            subjects=(event_subject, port_subject.at(field_step("interface"))),
                        )
                    )
                    continue
                if any(element.name == event.element for element in declared.data_elements):
                    continue
                records.append(
                    ConsistencyIssue(
                        rule="swc.data-event.unknown-element",
                        message=(
                            f"{naming} naming element '{event.element}' of port '{port.name}', which interface "
                            f"'{port.interface}' does not declare."
                        ),
                        subjects=(event_subject.at(field_step("element")),),
                        target=f"{port.interface}.{event.element}",
                    )
                )
    return records


def check_data_events(model_root: ModelInput) -> ConsistencyResult:
    """Check every data-received event against the port and data element it names.

    :func:`data_event_records` over the model; it documents the four ``swc.data-event.*`` rules.

    Returns:
        ConsistencyResult with consistent=True only when no rule fires.
    """
    view = as_view(model_root)
    return consistency_result(data_event_records(view.swcs, view.interfaces))


def _call_path(runnable_name: str, call: OperationCallRef) -> tuple[SubjectStep, ...]:
    """The path of one ``calls`` entry under its component, addressed by its ``{port, operation}`` identity."""
    return (
        field_step("runnables"),
        member_step(runnable_name),
        field_step("calls"),
        member_step({"port": call.port, "operation": call.operation}),
    )


def _call_subject(owner: Subject, runnable_name: str, call: OperationCallRef) -> Subject:
    """One ``calls`` entry of *owner*'s runnable (:func:`_call_path`)."""
    return owner.at(*_call_path(runnable_name, call))


def call_returned_events(runnable: Runnable) -> list[CallReturnedEvent]:
    """*runnable*'s ``call_returned`` events, in event order."""
    return [event for event in runnable.events if isinstance(event, CallReturnedEvent)]


def runnables_phrase(names: Sequence[str]) -> str:
    """``runnable 'A'`` or ``runnables 'A' and 'B'`` -- the runnables a finding names, in walk order."""
    quoted = [f"'{name}'" for name in names]
    if len(quoted) == 1:
        return f"runnable {quoted[0]}"
    return f"runnables {', '.join(quoted[:-1])} and {quoted[-1]}"


def async_call_records(
    swcs: Sequence[SWComponent], arxml_version: ArxmlVersion | str | None = None
) -> list[ConsistencyIssue]:
    """The asynchronous call's seven rules, over every runnable of each component.

    Per component, in this order: ``swc.call.mixed-synchrony`` (``[constr_2022]``), ``swc.call.timeout-mismatch``
    (``[constr_2023]``), ``swc.call.several-results`` (``[constr_2006]``), ``swc.call.async-without-result``
    (``[constr_2006]`` of R4.2.2 alone, so only when *arxml_version* is 4.2.2),
    ``swc.call-returned-event.unknown-call``, ``swc.call-returned-event.ambiguous-call`` and
    ``swc.call.init-runnable-result`` (``[constr_1259]``); the model contract describes each.

    A pair is one ``(port, operation)``; its asynchronous callers are the runnables whose ``calls`` hold it with
    ``asynchronous: true``, each one ``ASYNCHRONOUS-SERVER-CALL-POINT``. A call point's result points are its own
    poll and, when it is the pair's one asynchronous caller, each ``call_returned`` event naming the pair. The first
    two rules give one finding per pair, the result rules one per call point, the event rules one per event (an
    unknown call at its ``operation``), and the init rule one per result point. A pair a ``call_returned`` event
    names and two or more runnables call asynchronously is ``ambiguous-call``'s, never ``async-without-result``'s.
    A ``call_returned`` event's port and operation are judged only as the asynchronous call's, by the
    ``swc.access.*`` rules. An external component stub is skipped. Not reached: a wait point (none is modelled),
    which runnable fetches a result at run time, and whether the server's operation is reachable (the connectors').

    Args:
        swcs: the components whose calls and events are judged.
        arxml_version: the AUTOSAR version the project generates; ``None`` is the default, 4.2.2.

    Returns:
        One record per finding.
    """
    version = DEFAULT_ARXML_VERSION if arxml_version is None else ArxmlVersion.from_string(arxml_version)
    records: list[ConsistencyIssue] = []
    for swc in swcs:
        if swc.external:
            continue
        calls, events = _async_call_index(swc)
        records.extend(_call_pair_records(swc, calls, events, version))
        records.extend(_call_returned_event_records(swc, calls, events))
        records.extend(_init_runnable_result_records(swc))
    return records


#: Pair -> every (runnable, call) holding it, in walk order.
_CallsByPair = dict[tuple[str, str], list[tuple[Runnable, OperationCallRef]]]

#: Pair -> every (runnable, event) naming it, in walk order.
_EventsByPair = dict[tuple[str, str], list[tuple[Runnable, CallReturnedEvent]]]


def _async_call_index(swc: SWComponent) -> tuple[_CallsByPair, _EventsByPair]:
    """*swc*'s calls and ``call_returned`` events by ``(port, operation)`` pair, for :func:`async_call_records`."""
    calls: _CallsByPair = {}
    events: _EventsByPair = {}
    for runnable in swc.runnables:
        for call in runnable.calls:
            calls.setdefault((call.port, call.operation), []).append((runnable, call))
        for event in call_returned_events(runnable):
            events.setdefault((event.port, event.operation), []).append((runnable, event))
    return calls, events


def _call_pair_records(
    swc: SWComponent, calls: _CallsByPair, events: _EventsByPair, version: ArxmlVersion
) -> list[ConsistencyIssue]:
    """Rules 1-4 of :func:`async_call_records`, per pair *swc* calls, in walk order."""
    owner = Subject("swc", swc.name)
    records: list[ConsistencyIssue] = []
    for (port_name, operation), entries in calls.items():
        asynchronous = [(runnable, call) for runnable, call in entries if call.asynchronous]
        synchronous = [(runnable, call) for runnable, call in entries if not call.asynchronous]
        if asynchronous and synchronous:
            records.append(
                ConsistencyIssue(
                    rule="swc.call.mixed-synchrony",
                    message=(
                        f"SWC '{swc.name}' calls operation '{operation}' of port '{port_name}' asynchronously "
                        f"from {runnables_phrase([r.name for r, _ in asynchronous])} and synchronously from "
                        f"{runnables_phrase([r.name for r, _ in synchronous])}; the calls of one operation "
                        "of one port in one component are all synchronous or all asynchronous ([constr_2022]). "
                        "State the same `asynchronous` on every call of it."
                    ),
                    subjects=tuple(_call_subject(owner, runnable.name, call) for runnable, call in entries),
                )
            )
        timeouts = list(dict.fromkeys(call.timeout for _runnable, call in asynchronous))
        if len(timeouts) > 1:
            stated = ", ".join(
                f"'{runnable.name}' {'no timeout' if call.timeout is None else render_model_scalar(call.timeout)}"
                for runnable, call in asynchronous
            )
            records.append(
                ConsistencyIssue(
                    rule="swc.call.timeout-mismatch",
                    message=(
                        f"SWC '{swc.name}' calls operation '{operation}' of port '{port_name}' asynchronously "
                        f"with different timeouts ({stated}); the call points of one operation state one "
                        "timeout ([constr_2023]), an absent `timeout` counting as a value of its own. State "
                        "the same `timeout` on every asynchronous call of it, or none on any."
                    ),
                    subjects=tuple(
                        _call_subject(owner, runnable.name, call).at(field_step("timeout"))
                        for runnable, call in asynchronous
                    ),
                )
            )
        named_by = events.get((port_name, operation), [])
        for runnable, call in asynchronous:
            results: list[str] = []
            if call.poll:
                results.append(f"its own poll in runnable '{runnable.name}'")
            if len(asynchronous) == 1:
                results.extend(f"the call_returned event of runnable '{r.name}'" for r, _event in named_by)
            if len(results) > 1:
                records.append(
                    ConsistencyIssue(
                        rule="swc.call.several-results",
                        message=(
                            f"SWC '{swc.name}' runnable '{runnable.name}' calls operation '{operation}' of "
                            f"port '{port_name}' asynchronously, and the call has {len(results)} result points: "
                            f"{' and '.join(results)}; a call point is referenced by at most one result point "
                            "([constr_2006]). Keep one: remove `poll` from the call or a `call_returned` event."
                        ),
                        subjects=(
                            _call_subject(owner, runnable.name, call),
                            *(
                                runnable_event_subject(owner, r.name, event_identity(event))
                                for r, event in (named_by if len(asynchronous) == 1 else ())
                            ),
                        ),
                    )
                )
            elif not results and version == ArxmlVersion.V4_2_2 and not (len(asynchronous) > 1 and named_by):
                records.append(
                    ConsistencyIssue(
                        rule="swc.call.async-without-result",
                        message=(
                            f"SWC '{swc.name}' runnable '{runnable.name}' calls operation '{operation}' of "
                            f"port '{port_name}' asynchronously, and nothing fetches its result; at AUTOSAR "
                            "4.2.2 an asynchronous call point is referenced by exactly one result point "
                            "([constr_2006]), where 4.3.0 allows none. State `poll: true` on the call, add a "
                            "`call_returned` event naming it to the runnable that takes the result, or "
                            "generate at 4.3.0."
                        ),
                        subjects=(_call_subject(owner, runnable.name, call),),
                    )
                )

    return records


def _call_returned_event_records(
    swc: SWComponent, calls: _CallsByPair, events: _EventsByPair
) -> list[ConsistencyIssue]:
    """Rules 5-6 of :func:`async_call_records`, per ``call_returned`` event of *swc*."""
    owner = Subject("swc", swc.name)
    records: list[ConsistencyIssue] = []
    for (port_name, operation), named_by in events.items():
        callers = [runnable.name for runnable, call in calls.get((port_name, operation), []) if call.asynchronous]
        for runnable, event in named_by:
            event_at = runnable_event_subject(owner, runnable.name, event_identity(event))
            if not callers:
                records.append(
                    ConsistencyIssue(
                        rule="swc.call-returned-event.unknown-call",
                        message=(
                            f"SWC '{swc.name}' runnable '{runnable.name}' has a call_returned event naming "
                            f"operation '{operation}' of port '{port_name}', which no runnable of the component "
                            "calls asynchronously; the event is started by the return of the component's own "
                            "asynchronous call. Add the call with `asynchronous: true` to the runnable that "
                            "makes it, or remove the event."
                        ),
                        subjects=(event_at.at(field_step("operation")),),
                        target=f"{swc.name}.{port_name}.{operation}",
                    )
                )
            elif len(callers) > 1:
                records.append(
                    ConsistencyIssue(
                        rule="swc.call-returned-event.ambiguous-call",
                        message=(
                            f"SWC '{swc.name}' runnable '{runnable.name}' has a call_returned event naming "
                            f"operation '{operation}' of port '{port_name}', which {runnables_phrase(callers)} "
                            "call asynchronously; its result point references one call point, and SWCraft "
                            "cannot say which. Keep one asynchronous caller of the operation, or poll the "
                            "result on each call instead."
                        ),
                        subjects=(event_at,),
                    )
                )

    return records


def _init_runnable_result_records(swc: SWComponent) -> list[ConsistencyIssue]:
    """Rule 7 of :func:`async_call_records`, per result point an init-started runnable of *swc* owns."""
    owner = Subject("swc", swc.name)
    records: list[ConsistencyIssue] = []
    for runnable in swc.runnables:
        if not any(isinstance(event, InitEvent) for event in runnable.events):
            continue
        # Each result point as (phrase, its path under the component): the poll's field, or the event.
        owned = [
            (
                f"polls the result of its call of '{call.port}'.'{call.operation}'",
                (*_call_path(runnable.name, call), field_step("poll")),
            )
            for call in runnable.calls
            if call.poll
        ] + [
            (
                f"has a call_returned event naming '{event.port}'.'{event.operation}'",
                (
                    field_step("runnables"),
                    member_step(runnable.name),
                    field_step("events"),
                    member_step(event_identity(event)),
                ),
            )
            for event in call_returned_events(runnable)
        ]
        for phrase, steps in owned:
            records.append(
                ConsistencyIssue(
                    rule="swc.call.init-runnable-result",
                    message=(
                        f"SWC '{swc.name}' runnable '{runnable.name}' is started by an init event and "
                        f"{phrase}; a runnable an init event starts may call asynchronously but owns no result "
                        "point ([constr_1259]). Fetch the result in another runnable."
                    ),
                    subjects=(owner.at(*steps),),
                )
            )
    return records


def result_point_sites(
    swc_name: str, runnable: Runnable
) -> list[tuple[str, str, str, str | None, tuple[SubjectStep, ...], str]]:
    """Every result point of *runnable* as the identifier rules judge it.

    ``(subject words, port, operation, stated result or None, path under the
    component, stated field)``, in :func:`result_point_entries` order. The path
    is the call's ``result`` field for a stated poll, the call entry for a
    derived one, and the event for an event.
    """
    sites: list[tuple[str, str, str, str | None, tuple[SubjectStep, ...], str]] = []
    for call in runnable.calls:
        if not call.poll:
            continue
        entry = _call_path(runnable.name, call)
        sites.append(
            (
                f"SWC '{swc_name}' runnable '{runnable.name}' calls entry '{call.port}'.'{call.operation}' "
                "result point",
                call.port,
                call.operation,
                call.result,
                (*entry, field_step("result")) if call.result is not None else entry,
                "the call's result",
            )
        )
    for event in call_returned_events(runnable):
        sites.append(
            (
                f"SWC '{swc_name}' runnable '{runnable.name}' call-returned event "
                f"'{event.port}'.'{event.operation}' result point",
                event.port,
                event.operation,
                event.result,
                (
                    field_step("runnables"),
                    member_step(runnable.name),
                    field_step("events"),
                    member_step(event_identity(event)),
                ),
                "the event's result",
            )
        )
    return sites


def derived_result_point_name(port: str, operation: str) -> str:
    """``Result_{port}_{operation}``: the ``SHORT-NAME`` a result point stating no ``result`` derives.

    The one spelling of this form outside the writer, whose ``RESULT_POINT_PREFIX`` must equal
    :data:`GENERATED_RESULT_POINT_PREFIX`.
    """
    return f"{GENERATED_RESULT_POINT_PREFIX}{port}_{operation}"
