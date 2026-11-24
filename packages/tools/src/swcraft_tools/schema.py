"""Pydantic v2 models — single source of truth for YAML schema."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import TypeAliasType

from swcraft_tools.execution_budget import parse_budget


class ModelElement(BaseModel):
    """The base every model element inherits, for one reason: `allow_inf_nan=False`.

    Pydantic accepts `NaN` and `±Infinity` (YAML `.nan` / `.inf`) into a `float`
    field by default, and nothing in an architecture model has a meaning for
    them. Set on the base rather than per field because a hand-rolled bound such
    as `v < 0` passes `NaN`.
    """

    model_config = ConfigDict(allow_inf_nan=False)


def _reject_path_traversal(v: str, label: str) -> str:
    """Refuse a name or id that would leave its file's directory under `model_root`.

    `label` names the field in the raised error message.
    """
    if "/" in v or "\\" in v or ".." in v:
        raise ValueError(f"{label} must not contain path separators or '..'")
    return v


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


#: An absolute ARXML reference path of ``Identifier`` segments: the grammar of
#: ``validator.REF_PATH`` with the leading ``/`` required, because a disabled
#: mode's references are written verbatim as ``DEST``-typed reference text and a
#: relative one resolves against nothing. Restated because ``validator`` imports
#: this module.
_ABSOLUTE_REF_PATH = re.compile(r"/[a-zA-Z][a-zA-Z0-9_]{0,127}(?:/[a-zA-Z][a-zA-Z0-9_]{0,127})*")


class DisabledMode(ModelElement):
    """One mode in which a timing or data-received event does not start its runnable.

    AUTOSAR's ``disabledMode``, a feature of every ``RTEEvent``, is an
    ``R-MODE-IN-ATOMIC-SWC-INSTANCE-REF``: the component's own ``port`` the
    mode arrives on, and the two mode references, held **verbatim** because the
    model declares no mode-switch interface to resolve them against. Refused here: a reference that is not an absolute
    path. Whether ``port`` is a ``require`` port typed by a mode-switch
    interface stub, and whether ``mode_group_ref`` sits directly under that
    stub's path, is ``check-model``'s question.
    """

    port: str
    mode_group_ref: str
    mode_ref: str

    @field_validator("mode_group_ref", "mode_ref")
    @classmethod
    def references_must_be_absolute_paths(cls, v: str) -> str:
        if not _ABSOLUTE_REF_PATH.fullmatch(v):
            raise ValueError(f"disabled mode reference '{v}' must be an absolute path of AUTOSAR identifiers")
        return v


class TimingEvent(ModelElement):
    type: Literal["timing"]
    period: float  # seconds; must be > 0
    # Part of the whole-value identity only while non-empty, so an event with
    # none keeps its `{type, period}` address.
    disabled_modes: list[DisabledMode] = []
    # The event's SHORT-NAME as the source stated it; `None` is derived
    # (`TMT_{runnable}`). Part of the identity only while stated; whether it is
    # a legal, unique identifier is `check-model`'s.
    name: str | None = None

    @field_validator("period")
    @classmethod
    def period_must_be_positive(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError("period must be > 0")
        return v


class InitEvent(ModelElement):
    type: Literal["init"]
    # The event's stated SHORT-NAME, `TimingEvent.name`'s rule.
    name: str | None = None


class OperationEvent(ModelElement):
    """A server trigger: a client's call of ``operation`` through the component's own ``port`` starts the runnable.

    A whole value; ``(type, port, operation)`` is its identity. Refused here: a
    blank name. ``check-model`` owns the rest: the port is the component's own,
    faces ``provide`` and carries a client-server interface declaring the
    operation; no pair is bound twice; one runnable's operations are compatible.
    """

    type: Literal["operation"]
    port: str
    operation: str
    # The event's stated SHORT-NAME, `TimingEvent.name`'s rule.
    name: str | None = None

    @field_validator("port", "operation")
    @classmethod
    def ref_fields_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("operation event ref fields must not be empty")
        return v


class DataEvent(ModelElement):
    """A data-received trigger: the reception of ``element`` through the component's own ``port`` starts the runnable.

    AUTOSAR's ``DataReceivedEvent`` (``[TPS_SWCT_01337]``), whose ``DATA-IREF``
    names a required port of the event's own component and a data element of
    that port's sender-receiver interface.

    A whole value; ``(type, port, element, disabled_modes, name)`` is its
    identity, a field at its default left out of it. Refused here: a blank
    name. ``check-model`` owns the rest: the port is the component's own, faces
    ``require`` and carries a sender-receiver interface declaring the element
    or a stub listing it.
    """

    type: Literal["data"]
    port: str
    element: str
    # The timing event's field and rule.
    disabled_modes: list[DisabledMode] = []
    # The event's stated SHORT-NAME, `TimingEvent.name`'s rule (`DRT_{port}_{element}` derived).
    name: str | None = None

    @field_validator("port", "element")
    @classmethod
    def ref_fields_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("data event ref fields must not be empty")
        return v


class CallReturnedEvent(ModelElement):
    """A returns trigger: the runnable starts when an asynchronous call of ``operation`` through ``port`` returns.

    AUTOSAR's ``AsynchronousServerCallReturnsEvent`` (``[TPS_SWCT_01346]``),
    sourced by an ``AsynchronousServerCallResultPoint`` of this runnable that
    references the component's asynchronous call point of the pair.

    A whole value; ``(type, port, operation, result, name)`` is its identity, a
    field at its default left out of it. Refused here: a blank name. That
    exactly one runnable of the component calls the pair asynchronously is
    ``check-model``'s.
    """

    type: Literal["call_returned"]
    port: str
    operation: str
    # The result point's stated SHORT-NAME, `DataReadRef.name`'s rule
    # (`Result_{port}_{operation}` derived).
    result: str | None = None
    # The event's stated SHORT-NAME, `TimingEvent.name`'s rule (`ASRT_{port}_{operation}` derived).
    name: str | None = None

    @field_validator("port", "operation")
    @classmethod
    def ref_fields_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("call-returned event ref fields must not be empty")
        return v


# ---------------------------------------------------------------------------
# Runnable data access
# ---------------------------------------------------------------------------


class DataReadRef(ModelElement):
    """One data element a runnable reads, named by port and element.

    `by` is the receive form: `None` is by-argument, the AUTOSAR default;
    `value` is by-value; `implicit` is AUTOSAR's ``dataReadAccess``, whose
    value the RTE takes before the runnable starts. The identity is the
    `(port, element)` pair.
    """

    port: str
    element: str
    by: Literal["value", "implicit"] | None = None
    # The access point's stated SHORT-NAME; `None` is derived (`R_{port}_{element}`). Not part of the identity.
    name: str | None = None

    @field_validator("port", "element")
    @classmethod
    def ref_fields_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("data read ref fields must not be empty")
        return v


class DataWriteRef(ModelElement):
    """One data element a runnable writes, named by port and element.

    `by` is the send form: `None` is the explicit send, `implicit` AUTOSAR's
    ``dataWriteAccess``, whose value the RTE publishes after the runnable ends.
    Not part of the identity.
    """

    port: str
    element: str
    by: Literal["implicit"] | None = None
    # The stated SHORT-NAME, `DataReadRef.name`'s rule (`W_{port}_{element}` derived).
    name: str | None = None

    @field_validator("port", "element")
    @classmethod
    def ref_fields_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("data write ref fields must not be empty")
        return v


class OperationCallRef(ModelElement):
    """One operation a runnable calls, named by port and operation.

    ``asynchronous`` is AUTOSAR's ``AsynchronousServerCallPoint`` (the caller
    does not wait). Only an asynchronous call takes ``poll`` (the runnable owns
    a result point and fetches the result itself), ``result`` (that result
    point's stated ``SHORT-NAME``, only with ``poll``) and ``timeout`` (the
    call point's ``TIMEOUT`` in seconds, written only when stated). The
    identity is the ``(port, operation)`` pair.
    """

    port: str
    operation: str
    asynchronous: bool = False
    poll: bool = False
    result: str | None = None
    timeout: float | None = None  # seconds; must be >= 0
    # The stated SHORT-NAME, `DataReadRef.name`'s rule (`Call_{port}_{operation}` derived).
    name: str | None = None

    @field_validator("port", "operation")
    @classmethod
    def ref_fields_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("operation call ref fields must not be empty")
        return v

    @field_validator("timeout")
    @classmethod
    def timeout_non_negative(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("timeout must be >= 0")
        return v

    @model_validator(mode="after")
    def asynchronous_fields(self) -> OperationCallRef:
        """``poll``, ``result`` and ``timeout`` only on an asynchronous call, and ``result`` only with ``poll``."""
        if not self.asynchronous:
            stated = [
                key
                for key, held in (
                    ("poll", self.poll),
                    ("result", self.result is not None),
                    ("timeout", self.timeout is not None),
                )
                if held
            ]
            if stated:
                raise ValueError(
                    f"call '{self.port}'.'{self.operation}' states {', '.join(stated)} without 'asynchronous: true': "
                    "a synchronous call has no result point and its timeout is not stated"
                )
        if self.result is not None and not self.poll:
            raise ValueError(
                f"call '{self.port}'.'{self.operation}' states a result without 'poll: true': 'result' names "
                "the result point a poll gives the calling runnable"
            )
        return self


class VariableReadRef(ModelElement):
    """One inter-runnable variable a runnable reads, named by the variable.

    The variable is the entry's identity; that it names a variable of the
    runnable's own component is ``check-model``'s (``[constr_2026]``).
    """

    variable: str
    # The stated SHORT-NAME, `DataReadRef.name`'s rule (`RV_{variable}` derived).
    name: str | None = None

    @field_validator("variable")
    @classmethod
    def variable_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("variable read must name a non-blank variable")
        return v


class VariableWriteRef(ModelElement):
    """One inter-runnable variable a runnable writes, named by the variable."""

    variable: str
    # The stated SHORT-NAME, `DataReadRef.name`'s rule (`WV_{variable}` derived).
    name: str | None = None

    @field_validator("variable")
    @classmethod
    def variable_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("variable write must name a non-blank variable")
        return v


class CanEnterArea(ModelElement):
    """One exclusive area a runnable can enter, named by the area.

    AUTOSAR's ``ExecutableEntity.canEnterExclusiveArea`` (``[TPS_SWCT_01051]``):
    the runnable enters and leaves the area by explicit RTE calls. The area is
    the entry's identity; that it names an area of the runnable's own component
    is ``check-model``'s (``swc.exclusive-area.unknown-area``).
    """

    area: str

    @field_validator("area")
    @classmethod
    def area_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("can_enter entry must name a non-blank exclusive area")
        return v


class RunsInsideArea(ModelElement):
    """One exclusive area a runnable runs inside, named by the area.

    AUTOSAR's ``ExecutableEntity.runsInsideExclusiveArea`` (``[TPS_SWCT_01050]``):
    the runnable's whole execution is guarded by the area. ``CanEnterArea``'s
    rules otherwise.
    """

    area: str

    @field_validator("area")
    @classmethod
    def area_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("runs_inside entry must name a non-blank exclusive area")
        return v


#: What each of a runnable's three access lists needs of the port an entry names:
#: the interface kind, then the port direction. Stated once because the
#: validator and the importer decide by it and must not drift apart.
ACCESS_LIST_NEEDS: dict[str, tuple[str, str]] = {
    "reads": ("sender_receiver", "require"),
    "writes": ("sender_receiver", "provide"),
    "calls": ("client_server", "require"),
}

#: How a message spells an interface kind: the YAML token is a field value, the
#: prose is what a reader of `check-model` or of an import report recognises.
INTERFACE_KIND_PROSE: dict[str, str] = {"sender_receiver": "sender/receiver", "client_server": "client/server"}


def _reject_duplicate_access(
    entries: list[DataReadRef] | list[DataWriteRef] | list[OperationCallRef], list_name: str, member: str
) -> None:
    """Refuse a `(port, <member>)` pair listed twice in one of a runnable's three access lists.

    The pair is the whole identity, so a by-value and a by-argument read of one
    pair are the same entry twice.
    """
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry.port, getattr(entry, member))
        if key in seen:
            raise ValueError(f"duplicate {list_name} entry in runnable: {key[0]}.{key[1]}")
        seen.add(key)


def _reject_duplicate_variable_access(entries: list[VariableReadRef] | list[VariableWriteRef], list_name: str) -> None:
    """Refuse one variable listed twice in one of a runnable's two variable access lists.

    The comparison is exact: a case-only repeat names a different variable.
    """
    seen: set[str] = set()
    for entry in entries:
        if entry.variable in seen:
            raise ValueError(f"duplicate {list_name} entry in runnable: {entry.variable}")
        seen.add(entry.variable)


def _reject_duplicate_area_entry(entries: list[CanEnterArea] | list[RunsInsideArea], list_name: str) -> None:
    """Refuse one area listed twice in one of a runnable's two exclusive area lists.

    The comparison is exact: a case-only repeat names a different area. One
    area in both lists is not refused.
    """
    seen: set[str] = set()
    for entry in entries:
        if entry.area in seen:
            raise ValueError(f"duplicate {list_name} entry in runnable: {entry.area}")
        seen.add(entry.area)


# ---------------------------------------------------------------------------
# Runnable
# ---------------------------------------------------------------------------


def _retired_wcet_sentence(value: object, *, runnable: bool) -> str:
    """The refusal of a `wcet_us` key: the new spelling, with the file's number when it has one.

    A file carrying both `wcet_us` and `wcet` is refused by the same sentence.
    The number is quoted only when it is a positive whole number, since any
    other would be refused on the next load; otherwise it reads `<value>`.
    """
    positive = isinstance(value, int) and not isinstance(value, bool) and value > 0
    number = str(value) if positive else "<value>"
    sentence = (
        f"'wcet_us' was renamed 'wcet' by contract 10.0.0 and now carries its unit: write "
        f"'wcet: {number}us' for 'wcet_us: {number}'"
    )
    if runnable:
        sentence += ", and give the budget a 'wcet_type' (gross or net)"
    return sentence


def _budget_as_written(value: object) -> object:
    """The body of `wcet.format`, the runnable's and the task's: the text is a budget `parse_budget` reads.

    Runs before the type check, so a number without a unit gets `parse_budget`'s sentence.
    """
    if value is not None:
        parse_budget(value)
    return value


def _positive_budget(value: str | None) -> str | None:
    """The body of `wcet.positive`, the runnable's and the task's: a stated budget is above zero."""
    if value is not None and parse_budget(value).factor <= 0:
        raise ValueError("wcet must be > 0")
    return value


class Runnable(ModelElement):
    """One runnable entity of an SWC: its events, and what it touches.

    `reads`, `writes` and `calls` name a port of the owning SWC and a member of
    the port's interface; they say *what* is touched, never in what order.
    Refused here: a blank name, and one pair listed twice. `check-model` owns
    the rest: the port is the SWC's, its interface carries the member, a read is
    on a require port, a write on a provide port, a call on a client-server port.
    """

    name: str
    min_start_interval: float = 0.0  # seconds; must be >= 0
    # AUTOSAR's canBeInvokedConcurrently.
    concurrent: bool = False
    events: list[TimingEvent | InitEvent | OperationEvent | DataEvent | CallReturnedEvent] = []
    reads: list[DataReadRef] = []
    writes: list[DataWriteRef] = []
    calls: list[OperationCallRef] = []
    # Inter-runnable variables of the runnable's own component, by name; that
    # each exists is `check-model`'s (`swc.variable-access.unknown-variable`).
    reads_variables: list[VariableReadRef] = []
    writes_variables: list[VariableWriteRef] = []
    # Exclusive areas of the runnable's own component (`canEnterExclusiveArea`,
    # `runsInsideExclusiveArea`), `can_enter` first as the XSD has it; that
    # each exists is `check-model`'s (`swc.exclusive-area.unknown-area`).
    can_enter: list[CanEnterArea] = []
    runs_inside: list[RunsInsideArea] = []
    # The runnable's `DESC`.
    description: str = ""
    # AUTOSAR's `symbol`, the entry point's C name; `None` means the symbol is
    # the name. A stated one's validity and uniqueness are `check-model`'s.
    symbol: str | None = None
    # The execution-time budget allotted to one execution, an upper bound and
    # never a measurement, held as written (`850ns`, `400us`). `None` when not
    # declared, > 0 when present, and paired with `wcet_type`. That a task's
    # runnables fit the task's budget is `check-model`'s
    # (`task.runnable-cost-exceeds-task`).
    wcet: str | None = None
    # AUTOSAR's `executionTimeType`: `gross` includes the runnable's external
    # calls, `net` excludes them. Required with `wcet` and refused without it.
    wcet_type: Literal["gross", "net"] | None = None
    # The trace link, as `SWComponent.satisfies` states it.
    satisfies: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def wcet_us_is_retired(cls, data: object) -> object:
        """Refuse the retired `wcet_us` spelling, naming the new one.

        Without it the key would load as an unknown key with its budget dropped.
        """
        if isinstance(data, Mapping) and "wcet_us" in data:
            raise ValueError(_retired_wcet_sentence(data["wcet_us"], runnable=True))
        return data

    @field_validator("min_start_interval")
    @classmethod
    def min_start_interval_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("min_start_interval must be >= 0")
        return v

    @field_validator("reads")
    @classmethod
    def reads_must_not_have_duplicates(cls, v: list[DataReadRef]) -> list[DataReadRef]:
        _reject_duplicate_access(v, "reads", "element")
        return v

    @field_validator("writes")
    @classmethod
    def writes_must_not_have_duplicates(cls, v: list[DataWriteRef]) -> list[DataWriteRef]:
        _reject_duplicate_access(v, "writes", "element")
        return v

    @field_validator("calls")
    @classmethod
    def calls_must_not_have_duplicates(cls, v: list[OperationCallRef]) -> list[OperationCallRef]:
        _reject_duplicate_access(v, "calls", "operation")
        return v

    @field_validator("reads_variables")
    @classmethod
    def reads_variables_must_not_have_duplicates(cls, v: list[VariableReadRef]) -> list[VariableReadRef]:
        _reject_duplicate_variable_access(v, "reads_variables")
        return v

    @field_validator("writes_variables")
    @classmethod
    def writes_variables_must_not_have_duplicates(cls, v: list[VariableWriteRef]) -> list[VariableWriteRef]:
        _reject_duplicate_variable_access(v, "writes_variables")
        return v

    @field_validator("can_enter")
    @classmethod
    def can_enter_must_not_have_duplicates(cls, v: list[CanEnterArea]) -> list[CanEnterArea]:
        _reject_duplicate_area_entry(v, "can_enter")
        return v

    @field_validator("runs_inside")
    @classmethod
    def runs_inside_must_not_have_duplicates(cls, v: list[RunsInsideArea]) -> list[RunsInsideArea]:
        _reject_duplicate_area_entry(v, "runs_inside")
        return v

    @field_validator("wcet", mode="before")
    @classmethod
    def wcet_is_written_as_a_budget(cls, v: object) -> object:
        return _budget_as_written(v)

    @field_validator("wcet")
    @classmethod
    def wcet_must_be_positive(cls, v: str | None) -> str | None:
        return _positive_budget(v)

    @model_validator(mode="after")
    def wcet_type_is_paired(self) -> Runnable:
        if self.wcet is not None and self.wcet_type is None:
            raise ValueError(
                f"runnable '{self.name}' declares wcet without a wcet_type: add 'wcet_type: gross' "
                "(time including external calls) or 'wcet_type: net' (without them)"
            )
        if self.wcet is None and self.wcet_type is not None:
            raise ValueError(
                f"runnable '{self.name}' declares a wcet_type and no wcet: a type without a budget "
                "states nothing; remove it or add the budget"
            )
        return self


#: The YAML shape an `init` value may take, recursively: a scalar, a list or a
#: map. It does not relate the value to the element's declared type; that is
#: `check-model`'s.
#:
#: * It must be a **named** alias: the bare recursive union raises
#:   ``RecursionError`` in Pydantic's schema generation.
#: * ``TypeAliasType`` from `typing_extensions`, because PEP 695's ``type``
#:   statement needs 3.12 and the declared floor is 3.11.
#: * ``bool`` before ``int`` is for reading only; the smart union keeps
#:   ``init: true`` a `bool`.
#:
#: Non-finite floats are refused at every depth by `ModelElement`'s
#: ``allow_inf_nan=False``. Declared before the port because a com-spec entry's
#: ``init`` takes it too, and an unresolved forward reference would be
#: published by the editing registry as ``"string"`` rather than ``value``.
InitValue = TypeAliasType("InitValue", "bool | int | float | str | list[InitValue] | dict[str, InitValue]")


# ---------------------------------------------------------------------------
# Port
# ---------------------------------------------------------------------------


class PortComSpec(ModelElement):
    """One data element's com-spec attributes on one component port.

    AUTOSAR's sender/receiver com-spec, recorded only where it differs from the
    writer's silent value: ``None`` means *the model is silent*, never ``false``
    or ``0`` stated. ``data_element`` is the entry's identity. Everything that
    needs the port's interface is ``check-model``'s.
    """

    data_element: str
    enable_update: bool | None = None
    alive_timeout: float | None = None  # seconds; must be >= 0
    handle_never_received: bool | None = None
    handle_timeout_type: Literal["none", "replace"] | None = None
    handle_out_of_range: Literal["none", "default", "ignore", "saturate"] | None = None
    uses_end_to_end_protection: bool | None = None
    queue_length: int | None = None  # must be >= 1
    # The port's own `initValue`, superseding the data element's `init` on this
    # port; `None` means the port repeats the element's.
    init: InitValue | None = None

    @field_validator("alive_timeout")
    @classmethod
    def alive_timeout_non_negative(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("alive_timeout must be >= 0")
        return v

    @field_validator("queue_length")
    @classmethod
    def queue_length_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("queue_length must be >= 1")
        return v


class Port(ModelElement):
    name: str
    direction: Literal["provide", "require"]
    # The declared interface, optional because an external stub's port has
    # none. `None`, never `""`: an empty string would draw an impact edge to an
    # interface named "". A declared component's port without one loads and is
    # `port.undeclared-interface`'s finding.
    interface: str | None = None
    # A hint, not a declaration: the interface of the stub port's known
    # neighbours when they agree. Never a reference, so it draws no edge.
    observed_interface: str | None = None
    description: str = ""
    # The measures answering a coexistence finding about this port, keyed by a
    # data element's name, valued by free text naming the mechanism. No
    # validator on purpose: a map on a `provide` port
    # (`safety.measure-on-provide-port`) or a blank text (`safety.measure-blank`)
    # is a finding, not a load failure, so the project stays readable.
    measures: dict[str, str] = {}
    # Com-spec attributes per data element, one entry per element; whether an
    # entry resolves on the port's interface is `check-model`'s.
    com_specs: list[PortComSpec] = []
    # The trace link, as `SWComponent.satisfies` states it.
    satisfies: list[str] = []

    @field_validator("com_specs")
    @classmethod
    def com_specs_must_not_have_duplicates(cls, v: list[PortComSpec]) -> list[PortComSpec]:
        seen: set[str] = set()
        for entry in v:
            if entry.data_element in seen:
                raise ValueError(f"duplicate com_specs entry for data element '{entry.data_element}'")
            seen.add(entry.data_element)
        return v


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class ConnectorEndpoint(ModelElement):
    # An endpoint names a type: a software component or a composition (AUTOSAR's
    # `TYPE-TREF` `DEST`). Exactly one of the two fields is set.
    swc: str | None = None
    composition: str | None = None
    port: str

    @field_validator("swc", "composition", "port")
    @classmethod
    def endpoint_fields_must_not_be_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("connector endpoint fields must not be empty")
        return v

    @model_validator(mode="after")
    def exactly_one_endpoint_type_field(self) -> ConnectorEndpoint:
        if (self.swc is None) == (self.composition is None):
            raise ValueError("a connector endpoint must declare exactly one of 'swc' and 'composition'")
        return self


class Connector(ModelElement):
    name: str
    from_: ConnectorEndpoint = Field(alias="from")
    to: ConnectorEndpoint
    # The composition this connector is wired inside. `None` means the root
    # composition, resolved where the model is in hand because the root's name
    # is a property of the model.
    composition: str | None = None
    description: str = ""

    @field_validator("name")
    @classmethod
    def name_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be empty")
        return _reject_path_traversal(v, "name")


#: The eighteen values a rated `asil` field may hold: the five plain levels and
#: the thirteen decomposed spellings `X(Y)` of `[TPS_SAFEX_00201]`, in
#: `safety.ASIL_VALUES`' order. The `asil_capability` fields keep the five plain
#: levels: a capability is not decomposed.
AsilValue = Literal[
    "QM",
    "A",
    "B",
    "C",
    "D",
    "QM(A)",
    "QM(B)",
    "QM(C)",
    "QM(D)",
    "A(B)",
    "A(C)",
    "A(D)",
    "B(B)",
    "B(C)",
    "B(D)",
    "C(C)",
    "C(D)",
    "D(D)",
]


# ---------------------------------------------------------------------------
# Inter-runnable variable
# ---------------------------------------------------------------------------


class Variable(ModelElement):
    """One inter-runnable variable of a component.

    AUTOSAR's ``VariableDataPrototype`` in the role
    ``explicitInterRunnableVariable`` or ``implicitInterRunnableVariable`` of
    the component's ``SwcInternalBehavior`` (``[TPS_SWCT_01052]``): a datum
    the component's runnables share without a port. ``access`` chooses the
    role and has no default, because the standard gives none. The type and the
    ``init`` take ``DataElement``'s shape (exactly one of the two type fields).
    Whether the type is declared, is not a pointer, and holds the ``init`` is
    ``check-model``'s.
    """

    name: str
    access: Literal["implicit", "explicit"]
    application_type: str | None = None
    implementation_type: str | None = None
    init: InitValue | None = None
    description: str = ""
    # AUTOSAR's swCalibrationAccess, `DataElement.sw_calibration_access`'s
    # literals; absent is the standard's `notAccessible` and writes nothing.
    sw_calibration_access: Literal["not_accessible", "read_only", "read_write"] | None = None

    @field_validator("application_type")
    @classmethod
    def application_type_must_not_be_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("variable application_type must not be blank")
        return v

    @field_validator("implementation_type")
    @classmethod
    def implementation_type_must_not_be_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("variable implementation_type must not be blank")
        return v

    @model_validator(mode="after")
    def exactly_one_type_field(self) -> Variable:
        if (self.application_type is None) == (self.implementation_type is None):
            raise ValueError(
                f"variable '{self.name}' must declare exactly one of 'application_type' and 'implementation_type'"
            )
        return self


# ---------------------------------------------------------------------------
# Exclusive area
# ---------------------------------------------------------------------------


class ExclusiveArea(ModelElement):
    """One exclusive area of a component.

    AUTOSAR's ``ExclusiveArea``, aggregated by the component's
    ``InternalBehavior`` (``exclusiveArea``, *local to the component*): the
    runnables that use one area do not preempt each other inside it
    (``[TPS_SWCT_01031]``). Which runnables use it is stated on the runnables,
    ``can_enter`` and ``runs_inside``.
    """

    name: str
    description: str = ""

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("exclusive area name must not be blank")
        return v


# ---------------------------------------------------------------------------
# SWComponent
# ---------------------------------------------------------------------------


class SWComponent(ModelElement):
    name: str
    type: Literal["application", "service", "ecu_abstraction"] = "application"
    description: str = ""
    # An external stub: a component the ARXML import found a prototype for and
    # no type declaration. The writer emits the source's `TYPE-TREF` and its
    # `DEST` verbatim instead of generating a component type.
    external: bool = False
    external_ref: str = ""
    external_dest: str = ""
    ports: list[Port] = []
    runnables: list[Runnable] = []
    # The inter-runnable variables the component's runnables share, keyed by
    # `name`; a name declared twice is refused here.
    variables: list[Variable] = []
    # The exclusive areas of the component's internal behaviour, keyed by `name`;
    # a name declared twice is refused here, a collision in the behaviour's name
    # space is `check-model`'s.
    exclusive_areas: list[ExclusiveArea] = []
    # Memory name -> bytes consumed. Keys name `kind: memory` hardware
    # elements; that reference is `check-model`'s.
    memory: dict[str, int] = {}
    # The safety integrity level this component declares. `None` means it
    # declares none and takes the highest level of the requirements allocated
    # to it; a written `QM` declares that no ASIL is allocated to it. No
    # default, so the two states stay distinguishable. Rules compare by `X`
    # (`safety.asil_rank`); a decomposed `(Y)` passes through.
    asil: AsilValue | None = None
    # AUTOSAR's `SwcInternalBehavior.handleTerminationAndRestart` in lower
    # snake case. `None` means the model says nothing, which check-model
    # reports and the writer refuses; no default, as for `asil`. An external
    # stub may not declare it.
    handle_termination_and_restart: (
        Literal["no_support", "can_be_terminated", "can_be_terminated_and_restarted"] | None
    ) = None
    # The mapping sets the internal behaviour maps its data types with, in the
    # model's order, emitted as DATA-TYPE-MAPPING-REFS. A set named twice is
    # refused because it would emit a duplicate ref.
    data_type_mappings: list[str] = []
    # The internal behaviour's SHORT-NAME as the source stated it; `None` is
    # derived (`{name}_InternalBehavior`). Every reference to the behaviour
    # follows it; its validity is `check-model`'s.
    behavior_name: str | None = None
    # The one sub-package of the element's home package (a root package or, for
    # a data description layer, its layer package `/DataTypes/{LayerPackage}`)
    # it is emitted in, a single SHORT-NAME segment; `None` is the home itself.
    # Loaded as any string so a bad value stays readable: `rule:identifier.declared`
    # reports it, and the writer's name guards and the editing protocol's pass D
    # refuse it. Carried by each of the nineteen kinds emitted directly into a
    # home package; the external kinds and every member kind have none. An external component stub loads
    # the key but emits no component type (`rule:swc.external-with-ports`).
    package: str | None = None
    # The requirements this element satisfies, each the requirement's `id` and,
    # after `@`, the version the link was confirmed for (`REQ-12@3`). The
    # grammar and the resolution are `check-model`'s.
    satisfies: list[str] = []

    @field_validator("name")
    @classmethod
    def name_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be empty")
        return _reject_path_traversal(v, "name")

    @field_validator("data_type_mappings")
    @classmethod
    def mapping_sets_must_not_repeat(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        for name in v:
            if not name.strip():
                raise ValueError("data_type_mappings entries must not be blank")
            if name in seen:
                raise ValueError(f"mapping set named twice in data_type_mappings: {name}")
            seen.add(name)
        return v

    @field_validator("variables")
    @classmethod
    def variables_must_not_have_duplicates(cls, v: list[Variable]) -> list[Variable]:
        seen: set[str] = set()
        for variable in v:
            if variable.name in seen:
                raise ValueError(f"variable named twice in variables: {variable.name}")
            seen.add(variable.name)
        return v

    @field_validator("exclusive_areas")
    @classmethod
    def exclusive_areas_must_not_have_duplicates(cls, v: list[ExclusiveArea]) -> list[ExclusiveArea]:
        seen: set[str] = set()
        for area in v:
            if area.name in seen:
                raise ValueError(f"exclusive area named twice in exclusive_areas: {area.name}")
            seen.add(area.name)
        return v

    @field_validator("memory")
    @classmethod
    def memory_budgets_must_be_named_and_non_negative(cls, v: dict[str, int]) -> dict[str, int]:
        for memory, size in v.items():
            if not memory.strip():
                raise ValueError("memory name must not be empty")
            if size < 0:
                raise ValueError(f"memory budget for '{memory}' must be >= 0")
        return v


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


class DataElement(ModelElement):
    name: str
    # The side of the type the element carries: an `application_types` element
    # or an `implementation_types` one; exactly one is set.
    application_type: str | None = None
    implementation_type: str | None = None
    init: InitValue | None = None
    # The safety integrity level of the information this element carries, on
    # the element because one interface may carry a rated element beside an
    # unrated one. No default: an element that declares none is unrated, not
    # `QM`, and no port rule compares against it. Rules compare by `X`.
    asil: AsilValue | None = None
    # The element's `DESC`.
    description: str = ""
    # AUTOSAR's swImplPolicy and swCalibrationAccess. Absent is the standard's
    # default (`standard`, `notAccessible`) and never the same statement as a
    # written value, which records what the source states.
    sw_impl_policy: Literal["standard", "queued"] | None = None
    sw_calibration_access: Literal["not_accessible", "read_only", "read_write"] | None = None

    @field_validator("application_type")
    @classmethod
    def application_type_must_not_be_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("data element application_type must not be blank")
        return v

    @field_validator("implementation_type")
    @classmethod
    def implementation_type_must_not_be_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("data element implementation_type must not be blank")
        return v

    @model_validator(mode="after")
    def exactly_one_type_field(self) -> DataElement:
        if (self.application_type is None) == (self.implementation_type is None):
            raise ValueError(
                f"data element '{self.name}' must declare exactly one of 'application_type' and 'implementation_type'"
            )
        return self

    @model_validator(mode="after")
    def calibration_access_must_be_supported(self) -> DataElement:
        """``[constr_1017]``: a queued element supports ``notAccessible`` only."""
        if self.sw_impl_policy == "queued" and self.sw_calibration_access in ("read_only", "read_write"):
            raise ValueError(
                f"data element '{self.name}' is queued and declares sw_calibration_access "
                f"'{self.sw_calibration_access}'; a queued element supports not_accessible only"
            )
        return self


class SRInterface(ModelElement):
    name: str
    kind: Literal["sender_receiver"]
    data_elements: list[DataElement]
    description: str = ""
    # Package placement, `SWComponent.package`'s terms.
    package: str | None = None
    # AUTOSAR's isService.
    is_service: bool = False
    # The trace link, as `SWComponent.satisfies` states it.
    satisfies: list[str] = []

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "name")

    @field_validator("data_elements")
    @classmethod
    def data_elements_must_not_be_empty(cls, v: list[DataElement]) -> list[DataElement]:
        if not v:
            raise ValueError("data_elements must not be empty")
        return v


class CSArgument(ModelElement):
    name: str
    direction: Literal["in", "out", "inout"]
    # `DataElement`'s two type fields, one set.
    application_type: str | None = None
    implementation_type: str | None = None
    description: str = ""

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("CSArgument name must not be blank")
        return v

    @field_validator("implementation_type")
    @classmethod
    def implementation_type_must_not_be_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("CSArgument implementation_type must not be blank")
        return v

    @field_validator("application_type")
    @classmethod
    def application_type_must_not_be_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("CSArgument application_type must not be blank")
        return v

    @model_validator(mode="after")
    def exactly_one_type_field(self) -> CSArgument:
        if (self.application_type is None) == (self.implementation_type is None):
            raise ValueError(
                f"argument '{self.name}' must declare exactly one of 'application_type' and 'implementation_type'"
            )
        return self


class CSOperation(ModelElement):
    name: str
    arguments: list[CSArgument] = []
    description: str = ""

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("CSOperation name must not be blank")
        return v


class CSInterface(ModelElement):
    name: str
    kind: Literal["client_server"]
    operations: list[CSOperation]
    description: str = ""
    # Package placement, `SWComponent.package`'s terms.
    package: str | None = None
    # AUTOSAR's isService.
    is_service: bool = False
    # The trace link, as `SWComponent.satisfies` states it.
    satisfies: list[str] = []

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "name")

    @field_validator("operations")
    @classmethod
    def operations_must_not_be_empty(cls, v: list[CSOperation]) -> list[CSOperation]:
        if not v:
            raise ValueError("operations must not be empty")
        return v


#: The config both stub kinds share: ``extra="forbid"``, because a stub declares
#: nothing of its target beyond its fields, so an undeclared key is a mistake to
#: refuse rather than drop. ``allow_inf_nan=False`` is restated so the intent
#: reads in one place.
_CLOSED_STUB_CONFIG = ConfigDict(allow_inf_nan=False, extra="forbid")


class ExternalInterface(ModelElement):
    """``kind:interfaces.external_interface`` — a stub for a port interface declared outside the project.

    The stub holds the source's reference pair — ``external_ref``, the full
    AUTOSAR path, and ``external_dest``, its ``DEST`` — and the **names** of the
    members its ports use: ``operations`` on a client-server stub,
    ``data_elements`` on a sender-receiver one, each the last segment of a
    member path directly under ``external_ref``. Nothing of a member's content
    is held: the export does not say it. A mode-switch stub lists nothing.

    Refused here: a blank or repeated member name (case-insensitively,
    AUTOSAR's ``[constr_2508]``), and a list the ``DEST`` does not admit. A
    blank or malformed pair loads and is
    ``rule:interface.external-incomplete-reference``'s finding.
    """

    model_config = _CLOSED_STUB_CONFIG

    name: str
    kind: Literal["external_interface"]
    external_ref: str = ""
    external_dest: str = ""
    operations: list[str] = []
    data_elements: list[str] = []
    description: str = ""
    # The trace link, as `SWComponent.satisfies` states it.
    satisfies: list[str] = []

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "name")

    @field_validator("operations")
    @classmethod
    def operations_must_not_repeat(cls, v: list[str]) -> list[str]:
        return _reject_repeated_member_names(v, "operations")

    @field_validator("data_elements")
    @classmethod
    def data_elements_must_not_repeat(cls, v: list[str]) -> list[str]:
        return _reject_repeated_member_names(v, "data_elements")

    @model_validator(mode="after")
    def members_must_match_the_dest(self) -> ExternalInterface:
        for field, dest in STUB_MEMBER_DEST.items():
            if getattr(self, field) and self.external_dest != dest:
                raise ValueError(
                    f"an external interface stub lists {field} only when its external_dest is {dest}, "
                    f"not '{self.external_dest}'"
                )
        return self


#: Which stub ``DEST`` admits which member list; every other ``DEST``, a
#: mode-switch interface's included, admits neither.
STUB_MEMBER_DEST: dict[str, str] = {
    "operations": "CLIENT-SERVER-INTERFACE",
    "data_elements": "SENDER-RECEIVER-INTERFACE",
}


def _reject_repeated_member_names(names: list[str], field: str) -> list[str]:
    """A stub's member list: no blank entry, and no name twice, ignoring case (``[constr_2508]``).

    Compared by ``casefold()``, ``identifier.short_name_key``'s key, restated
    rather than imported: the kernel chain runs ``schema`` -> ``identifier``
    and each module imports only from its left.
    """
    seen: set[str] = set()
    for name in names:
        if not name.strip():
            raise ValueError(f"{field} entries must not be blank")
        key = name.casefold()
        if key in seen:
            raise ValueError(f"member named twice in {field}, ignoring case: {name}")
        seen.add(key)
    return names


Interface = Annotated[SRInterface | CSInterface | ExternalInterface, Field(discriminator="kind")]


# ---------------------------------------------------------------------------
# Requirement
# ---------------------------------------------------------------------------


class Requirement(ModelElement):
    """One requirement or specification item.

    The trace link lives in the architecture element's own ``satisfies`` list,
    never here, because a re-import overwrites an imported requirement file.
    The retired ``links`` and ``status`` keys are refused with the new shape
    named rather than dropped on load.
    """

    id: str
    title: str
    description: str
    req_type: Literal["req", "spec"] = "req"
    # SWCraft's own change counter, a positive integer, no default; the import
    # raises it when an external requirement's text, level, type or parents
    # change, the author for an architecture one. `REQ-12@3` pins version 3.
    # Strict: YAML's `true`, `'3'` or `2.0` is no version.
    version: int = Field(strict=True)
    # Who owns the file: `external` (an importer writes it; a re-import
    # overwrites it, or removes it once the delivery no longer holds it) or
    # `architecture` (authored here). No default: provenance is stated.
    source: Literal["external", "architecture"]
    # What this requirement derives from: a requirement (`REQ-1@2`, the link
    # grammar) or a top-level element (`<token>:<name>`). Resolution is `check-model`'s.
    derived_from: list[str] = []
    # The safety integrity level allocated to this requirement, where
    # ISO 26262-9:2018 § 5.2 puts it. `None` means no level is allocated; a
    # written `QM` declares that none is. Every component satisfying it is
    # judged against it. Rules rank by `X` (`safety.asil_rank`).
    asil: AsilValue | None = None
    # Provenance of an external requirement: the source document, the tool's
    # own revision, and `baseline`, the delivery version it was last imported
    # from. No check reads them and a change never moves `version`.
    document: str | None = None
    baseline: str | None = None
    revision: str | None = None
    # A placeholder the import created for a parent that `derived_from` names
    # and nothing holds; replaced when an import brings it, removed once nothing
    # names it. `trace.requirement-stub` warns on each and abstains from
    # `trace.requirement-unallocated`. Strict, like `version` (a gate keys on it):
    # `'true'`/`1` are refused, but YAML 1.1 reads an unquoted `yes`/`on`/`no`/`off` as a bool.
    stub: bool = Field(default=False, strict=True)

    @model_validator(mode="before")
    @classmethod
    def status_is_retired(cls, data: object) -> object:
        """Refuse the retired `status` key.

        Declared before `links_is_retired` because Pydantic runs `before`
        validators last-declared first, so a file carrying both is refused with
        the `links` sentence, the one that names the move.
        """
        if isinstance(data, Mapping) and "status" in data:
            raise ValueError(_RETIRED_STATUS_SENTENCE)
        return data

    @model_validator(mode="before")
    @classmethod
    def links_is_retired(cls, data: object) -> object:
        """Refuse the retired `links` key, naming where the link lives now."""
        if isinstance(data, Mapping) and "links" in data:
            raise ValueError(_retired_links_sentence(data.get("id")))
        return data

    @field_validator("id")
    @classmethod
    def id_must_not_be_empty(cls, v: str) -> str:
        problem = requirement_id_problem(v)
        if problem is not None:
            raise ValueError(problem)
        return v

    @field_validator("version")
    @classmethod
    def version_must_be_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("version must be >= 1")
        return v


#: The two characters a requirement ``id`` may not hold: ``@`` separates a
#: link's version (``REQ-12@3``) and ``:`` an element reference (``hardware:Core0``).
_REQUIREMENT_ID_SEPARATORS = ("@", ":")


def requirement_id_problem(value: str) -> str | None:
    """Why ``value`` cannot be a requirement ``id``, or ``None`` when it can.

    Shared so the requirements import file and the model refuse the same ids in the same words.
    """
    if not value.strip():
        return "id must not be empty"
    for separator in _REQUIREMENT_ID_SEPARATORS:
        if separator in value:
            return f"id must not contain '{separator}': '@' separates a link's version and ':' an element reference"
    try:
        _reject_path_traversal(value, "id")
    except ValueError as exc:
        return str(exc)
    return None


#: The refusal of a requirement file still carrying ``status``.
_RETIRED_STATUS_SENTENCE = "'status' is retired by contract 11.0.0: an external tool owns the lifecycle; delete the key"


def _retired_links_sentence(requirement_id: object) -> str:
    """The refusal of a requirement file still carrying ``links``, with the file's own id when it has one."""
    shown = requirement_id if isinstance(requirement_id, str) and requirement_id.strip() else "REQ-12"
    return (
        "'links' moved to the elements by contract 11.0.0: write "
        f"'satisfies: [{shown}@N]' on the component, port, runnable, composition, interface, task or "
        "hardware element that realizes it"
    )


# ---------------------------------------------------------------------------
# Compositions
# ---------------------------------------------------------------------------
#
# The hierarchy is by reference, not by nesting, as AUTOSAR carries it: every
# `COMPOSITION-SW-COMPONENT-TYPE` is a sibling top-level element and a member
# points at its type through a `TYPE-TREF`.


class CompositionPort(ModelElement):
    """One boundary port of a composition.

    The same four fields as :class:`Port`, but a class of its own because an
    element kind cannot be a member of two layers' elements.
    """

    name: str
    direction: Literal["provide", "require"]
    interface: str
    description: str = ""

    @field_validator("name", "interface")
    @classmethod
    def boundary_port_fields_must_not_be_empty(cls, v: str) -> str:
        # A blank name is an emitted `SHORT-NAME` of nothing, and a blank
        # `interface` a reference reported with an empty target.
        if not v.strip():
            raise ValueError("composition port fields must not be empty")
        return v


class CompositionMember(ModelElement):
    """One member of a composition -- an AUTOSAR ``SW-COMPONENT-PROTOTYPE``.

    ``name`` is the member's own address and is written even when it equals the
    type it instantiates: it is the ``SHORT-NAME`` the export already carries
    for every prototype, and it is what a delegation names.

    ``type`` and ``composition`` are the model's spelling of the ``TYPE-TREF``'s
    ``DEST``; exactly one is set. Two fields because a reference names one layer.
    """

    name: str
    type: str | None = None
    composition: str | None = None
    description: str = ""

    @field_validator("name", "type", "composition")
    @classmethod
    def member_fields_must_not_be_empty(cls, v: str | None) -> str | None:
        # Otherwise `type: ""` passes `exactly_one_type_field` and is reported against an empty target.
        if v is not None and not v.strip():
            raise ValueError("composition member fields must not be empty")
        return v

    @model_validator(mode="after")
    def exactly_one_type_field(self) -> CompositionMember:
        if (self.type is None) == (self.composition is None):
            raise ValueError("a composition member must declare exactly one of 'type' and 'composition'")
        return self


class Delegation(ModelElement):
    """One delegation of a composition -- an AUTOSAR ``DELEGATION-SW-CONNECTOR``.

    Named rather than identified by the ``(port, member, member_port)`` triple,
    because a tool export's delegation carries a ``SHORT-NAME`` a round trip keeps.
    """

    name: str
    port: str
    member: str
    member_port: str
    description: str = ""

    @field_validator("name", "port", "member", "member_port")
    @classmethod
    def delegation_fields_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("delegation fields must not be empty")
        return v


class Composition(ModelElement):
    name: str
    title: str
    description: str = ""
    root: bool = False
    ports: list[CompositionPort] = []
    members: list[CompositionMember] = []
    delegations: list[Delegation] = []
    # Package placement, `SWComponent.package`'s terms.
    package: str | None = None
    # The trace link, as `SWComponent.satisfies` states it.
    satisfies: list[str] = []

    @field_validator("name")
    @classmethod
    def name_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be empty")
        return _reject_path_traversal(v, "name")


# ---------------------------------------------------------------------------
# DataType variants
# ---------------------------------------------------------------------------


#: The ``model_config`` the data type kinds and their member kinds share.
#:
#: ``extra="forbid"`` makes the wrong field on the wrong kind a load failure —
#: a ``base`` on a ``base_type``, a ``unit`` or a range on a ``reference_type``
#: — where Pydantic's default drops an unknown key without a word.
#: ``allow_inf_nan=False`` is restated so the intent reads in one place. Every
#: kind closed neither by it nor by ``_CLOSED_STUB_CONFIG`` drops an unknown
#: key, and ``check-model`` reports it as
#: ``model.unknown-key``; what that rule does not reach is stated in
#: :mod:`swcraft_tools.undeclared_keys`.
_CLOSED_DATA_TYPE_CONFIG = ConfigDict(allow_inf_nan=False, extra="forbid")


class BaseDataType(ModelElement):
    """``kind:base_types.base_type`` — how the platform stores a value (``SW-BASE-TYPE``).

    Never what a data prototype is typed by: a ``value_type`` names it as its
    ``base``, and data elements, arguments, record fields and array elements name
    the ``value_type``.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    name: str
    kind: Literal["base_type"]
    size: int
    encoding: str
    native_declaration: str = ""
    description: str = ""
    # Package placement, `SWComponent.package`'s terms.
    package: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Data type name")

    @field_validator("size")
    @classmethod
    def size_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("size must be > 0")
        return v

    @field_validator("encoding")
    @classmethod
    def encoding_must_not_be_blank(cls, v: str) -> str:
        # AUTOSAR's BASE-TYPE-ENCODING is a required enumerated token (NONE,
        # 2C, IEEE754, ...): `BaseTypeDirectDefinition.baseTypeEncoding` has
        # multiplicity [1] in R4.2.2 and R4.3.0. A blank one would not round-trip:
        # the importer reads an empty <BASE-TYPE-ENCODING> as missing and skips
        # the type.
        if not v.strip():
            raise ValueError(
                "encoding must not be blank (AUTOSAR BASE-TYPE-ENCODING is a required token, e.g. NONE, 2C, IEEE754)"
            )
        return v


class ValueDataType(ModelElement):
    """``kind:implementation_types.value_type`` — an ``IMPLEMENTATION-DATA-TYPE`` of category ``VALUE``.

    Its ``base`` must name a declared ``base_type`` (``check-model``'s to say).
    ``unit`` is documentation only (a ``VALUE`` implementation type has no ARXML
    place for one); ``value_min`` / ``value_max`` are written as the type's
    ``{name}_Range`` ``DATA-CONSTR``, which makes ``{name}_Range`` a reserved
    name of a bounded value type.

    A bound is ``int | float`` with no coercion: a 64-bit type's limit
    (``18446744073709551615``) is above 2**53 and a float would round it. A
    value written with a fraction or an exponent is a float.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    name: str
    kind: Literal["value_type"]
    base: str
    unit: str = ""
    value_min: int | float | None = None
    value_max: int | float | None = None
    # A `texttable` compu method, by name: the one way an implementation type states literals.
    compu_method_ref: str | None = None
    description: str = ""
    # Package placement, `SWComponent.package`'s terms.
    package: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Data type name")

    @field_validator("compu_method_ref")
    @classmethod
    def compu_method_ref_must_not_be_blank(cls, v: str | None) -> str | None:
        # Absent is the field's default; a blank name names no element.
        if v is not None and not v.strip():
            raise ValueError("compu_method_ref must not be blank")
        return v

    @model_validator(mode="after")
    def validate_value_type(self) -> ValueDataType:
        if not self.base.strip():
            raise ValueError("value type 'base' must not be blank or whitespace")
        # No self-reference check: `base` names an element of the `base_types`
        # layer, so a value type and its base type may share a name.
        if self.value_min is not None and self.value_max is not None:
            if self.value_min > self.value_max:
                raise ValueError("value_min must be <= value_max")
        return self


class ReferenceDataType(ModelElement):
    """``kind:implementation_types.reference_type`` — an ``IMPLEMENTATION-DATA-TYPE`` of category ``TYPE_REFERENCE``.

    Its ``base`` must name a declared ``value_type`` or another ``reference_type``.
    It declares no ``unit`` and no range: those belong to the ``value_type`` at
    the bottom of the chain. A ``base`` naming the type itself is refused here;
    a longer cycle spans files and is ``rule:data-type.cycle``'s.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    name: str
    kind: Literal["reference_type"]
    base: str
    # A `texttable` compu method, by name.
    compu_method_ref: str | None = None
    description: str = ""
    # Package placement, `SWComponent.package`'s terms.
    package: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Data type name")

    @field_validator("compu_method_ref")
    @classmethod
    def compu_method_ref_must_not_be_blank(cls, v: str | None) -> str | None:
        # Absent is the field's default; a blank name names no element.
        if v is not None and not v.strip():
            raise ValueError("compu_method_ref must not be blank")
        return v

    @model_validator(mode="after")
    def validate_reference_type(self) -> ReferenceDataType:
        if not self.base.strip():
            raise ValueError("reference type 'base' must not be blank or whitespace")
        if self.base == self.name:
            raise ValueError("reference type 'base' must not equal 'name' (self-referential reference type)")
        return self


class EnumLiteral(ModelElement):
    name: str
    value: int
    # Written as the `COMPU-SCALE`'s DESC.
    description: str = ""

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("EnumLiteral name must not be blank")
        return v


class EnumDataType(ModelElement):
    """``kind:application_types.enum`` — an application primitive whose values are named.

    It states its literals in exactly one of two forms: inline, as
    ``literals``, from which the writer generates the enumeration's own
    ``{name}_CompuMethod``; or as ``compu_method_ref``, naming a ``texttable``
    element of the ``compu_methods`` layer (``check-model`` resolves it).
    """

    name: str
    kind: Literal["enum"]
    literals: list[EnumLiteral] | None = None
    compu_method_ref: str | None = None
    description: str = ""
    # Package placement, `SWComponent.package`'s terms.
    package: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Data type name")

    @field_validator("literals")
    @classmethod
    def literals_must_not_be_empty(cls, v: list[EnumLiteral] | None) -> list[EnumLiteral] | None:
        if v is not None and not v:
            raise ValueError("literals must not be empty")
        return v

    @field_validator("compu_method_ref")
    @classmethod
    def compu_method_ref_must_not_be_blank(cls, v: str | None) -> str | None:
        # Absent is the field's default; a blank name names no element.
        if v is not None and not v.strip():
            raise ValueError("compu_method_ref must not be blank")
        return v

    @model_validator(mode="after")
    def literal_forms_must_be_exactly_one(self) -> EnumDataType:
        if (self.literals is None) == (self.compu_method_ref is None):
            raise ValueError(
                "an enum states exactly one of 'literals' (inline) and 'compu_method_ref' (a texttable compu method)"
            )
        return self


class RecordField(ModelElement):
    name: str
    type: str
    description: str = ""

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("RecordField name must not be blank")
        return v

    @field_validator("type")
    @classmethod
    def type_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("RecordField type must not be blank")
        return v


class RecordDataType(ModelElement):
    name: str
    kind: Literal["record"]
    fields: list[RecordField]
    description: str = ""
    # Package placement, `SWComponent.package`'s terms.
    package: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Data type name")

    @field_validator("fields")
    @classmethod
    def fields_must_not_be_empty(cls, v: list[RecordField]) -> list[RecordField]:
        if not v:
            raise ValueError("fields must not be empty")
        return v


class ArrayDataType(ModelElement):
    name: str
    kind: Literal["array"]
    element_type: str
    length: int
    description: str = ""
    # Package placement, `SWComponent.package`'s terms.
    package: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Data type name")

    @field_validator("element_type")
    @classmethod
    def element_type_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("element_type must not be blank or whitespace")
        return v

    @field_validator("length")
    @classmethod
    def length_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("length must be > 0")
        return v


def _limits_incoherence(
    lower: ConversionLimitBound | LinearLimitBound | None, upper: ConversionLimitBound | LinearLimitBound | None
) -> str | None:
    """Why a scale's two limits state an empty interval, or ``None`` where they are coherent.

    The one judgement of ``rule:compu_methods.linear.limits-coherent`` and
    ``rule:application_types.conversion.limits-coherent``: the lower value is
    not above the upper one, and two equal values are coherent only when both
    are ``closed`` -- the single point a ``[TPS_SWCT_01289]`` interval of two
    closed limits is. An absent bound is coherent.
    """
    if lower is None or upper is None:
        return None
    low_value, low_interval = lower.value, lower.interval
    high_value, high_interval = upper.value, upper.interval
    if low_value > high_value:
        return "limits 'lower' must be <= 'upper'"
    if low_value == high_value and (low_interval != "closed" or high_interval != "closed"):
        return "limits 'lower' and 'upper' may be equal only when both are 'closed'"
    return None


class ConversionLimitBound(ModelElement):
    """``kind:application_types.conversion_limit_bound`` — one limit of a conversion's scale.

    ``value`` is ``int | float`` with no coercion, :class:`ValueRange`'s reason; an
    infinite limit is no ``value`` (``allow_inf_nan=False``) but an absent bound.
    ``interval`` is the ``LIMIT``'s ``INTERVAL-TYPE`` in lower case, ``closed``
    when not declared -- the attribute's own default: *"If the attribute is
    missing the interval shall be considered as CLOSED"* (XSD 4.2.2 ``:41118``,
    4.3.0 ``:47486``).
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    value: int | float
    interval: Literal["closed", "open"] = "closed"


class ConversionLimits(ModelElement):
    """``kind:application_types.conversion_limits`` — a conversion's scale limits, on the stored (internal) side.

    ``[TPS_SWCT_01548]`` (SWCT R4.2.2 lines 16830-16833, R4.3.0 lines 18289-18292):
    a scale's limits bound the values *before* the conversion is applied, so
    they are the scope of the conversion -- what an MCD tool may convert at
    all (``[TPS_SWCT_01280]``, R4.2.2 lines 16950-16953, R4.3.0 lines 18394-18397)
    and what decides whether an ``invalidValue`` is written as an application
    value or as a number (``[constr_1283]``, R4.2.2 lines 11246-11250, R4.3.0
    lines 11854-11858) --
    not a second copy of the type's physical range. Either bound may be absent.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    lower: ConversionLimitBound | None = None
    upper: ConversionLimitBound | None = None


class Conversion(ModelElement):
    """``kind:application_types.conversion`` — one linear conversion, physical = internal * factor + offset.

    A nested object with no identity of its own, not a map, so a third key is
    refused. Both coefficients default, so ``conversion: {}`` is the identity
    conversion. Each is ``int | float`` with no coercion: an integer above
    2**53 has no float, and the writer states the number the model holds.

    ``limits`` is the scale's ``LOWER-LIMIT`` / ``UPPER-LIMIT`` on the stored
    side (:class:`ConversionLimits`); absent, the scale states none.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    factor: int | float = 1.0
    offset: int | float = 0.0
    limits: ConversionLimits | None = None

    @model_validator(mode="after")
    def limits_must_be_coherent(self) -> Conversion:
        if self.limits is not None and (reason := _limits_incoherence(self.limits.lower, self.limits.upper)):
            raise ValueError(f"conversion {reason}")
        return self


class ValueRange(ModelElement):
    """``kind:application_types.range`` — the physical limits an application type is declared within.

    Named ``ValueRange`` because the contract's kind is ``range``; the class
    name is the binding's alone. Either bound may be absent, as ``value_type``'s
    ``value_min`` / ``value_max`` may, under the same coherence rule. An integer
    bound is held as an ``int``, never rounded through a float.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    min: int | float | None = None
    max: int | float | None = None

    @model_validator(mode="after")
    def bounds_must_be_coherent(self) -> ValueRange:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("range 'min' must be <= 'max'")
        return self


class ApplicationDataType(ModelElement):
    """``kind:application_types.application_type`` — an ``APPLICATION-PRIMITIVE-DATA-TYPE`` of category ``VALUE``.

    The physical view of a signal: its unit, its conversion from the internal
    value and its declared range. It names no ``base``, so it is in no cycle.

    ``unit``, ``conversion`` and ``range`` are each optional and emitted only
    when declared; with none of the three it has an empty
    ``SW-DATA-DEF-PROPS``. Each may instead be stated by reference:
    ``unit_ref`` names a ``unit`` element, ``compu_method_ref`` a ``linear``
    compu method and ``data_constr_ref`` a ``data_constr`` element. A property
    is stated in at most one of its two forms; a blank ``unit`` is no unit.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    name: str
    kind: Literal["application_type"]
    unit: str = ""
    conversion: Conversion | None = None
    range: ValueRange | None = None
    unit_ref: str | None = None
    compu_method_ref: str | None = None
    data_constr_ref: str | None = None
    description: str = ""
    # Package placement, `SWComponent.package`'s terms.
    package: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Data type name")

    @field_validator("unit_ref")
    @classmethod
    def unit_ref_must_not_be_blank(cls, v: str | None) -> str | None:
        # Absent is the field's default; a blank name names no element.
        if v is not None and not v.strip():
            raise ValueError("unit_ref must not be blank")
        return v

    @field_validator("compu_method_ref")
    @classmethod
    def compu_method_ref_must_not_be_blank(cls, v: str | None) -> str | None:
        # Absent is the field's default; a blank name names no element.
        if v is not None and not v.strip():
            raise ValueError("compu_method_ref must not be blank")
        return v

    @field_validator("data_constr_ref")
    @classmethod
    def data_constr_ref_must_not_be_blank(cls, v: str | None) -> str | None:
        # Absent is the field's default; a blank name names no element.
        if v is not None and not v.strip():
            raise ValueError("data_constr_ref must not be blank")
        return v

    @model_validator(mode="after")
    def unit_forms_must_be_exclusive(self) -> ApplicationDataType:
        if self.unit.strip() and self.unit_ref is not None:
            raise ValueError(
                "an application type states its unit inline ('unit') or by reference ('unit_ref'), not both"
            )
        return self

    @model_validator(mode="after")
    def conversion_forms_must_be_exclusive(self) -> ApplicationDataType:
        if self.conversion is not None and self.compu_method_ref is not None:
            raise ValueError(
                "an application type states its conversion inline ('conversion') or by reference "
                "('compu_method_ref'), not both"
            )
        return self

    @model_validator(mode="after")
    def range_forms_must_be_exclusive(self) -> ApplicationDataType:
        if self.range is not None and self.data_constr_ref is not None:
            raise ValueError(
                "an application type states its range inline ('range') or by reference ('data_constr_ref'), not both"
            )
        return self


class ExternalDataType(ModelElement):
    """``kind:implementation_types.external_type`` — a stub for an implementation type declared elsewhere.

    The type is declared outside the project.

    The data-type twin of :class:`ExternalInterface`: an identity and the
    source's reference pair, nothing of the target's content. Which
    ``external_dest`` the layer admits (``IMPLEMENTATION-DATA-TYPE`` alone) and
    whether a blank pair is well formed are
    ``rule:data-type.external-incomplete-reference``'s, so a malformed stub loads.
    """

    model_config = _CLOSED_STUB_CONFIG

    name: str
    kind: Literal["external_type"]
    external_ref: str = ""
    external_dest: str = ""
    description: str = ""

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Data type name")


class ImplementationElement(ModelElement):
    """``kind:implementation_types.implementation_element`` — one member of an ``implementation_record``.

    Emitted as one ``IMPLEMENTATION-DATA-TYPE-ELEMENT`` of category
    ``TYPE_REFERENCE``: its ``name`` is the sub-element's ``SHORT-NAME`` and its
    ``type`` names the implementation type it carries; which kinds it may name
    is ``check-model``'s. Closed, so a typo is a load failure.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    name: str
    type: str
    description: str = ""

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("implementation element name must not be blank")
        return v

    @field_validator("type")
    @classmethod
    def type_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("implementation element type must not be blank")
        return v


class ImplementationRecordDataType(ModelElement):
    """``kind:implementation_types.implementation_record`` — an implementation type of category ``STRUCTURE``.

    It is written as an ``IMPLEMENTATION-DATA-TYPE``.

    Its ``elements`` are the sub-elements in declaration order; an empty list
    is refused. Resolution, duplicate names and self-containment are
    ``check-model``'s.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    name: str
    kind: Literal["implementation_record"]
    elements: list[ImplementationElement]
    description: str = ""
    # Package placement, `SWComponent.package`'s terms.
    package: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Data type name")

    @field_validator("elements")
    @classmethod
    def elements_must_not_be_empty(cls, v: list[ImplementationElement]) -> list[ImplementationElement]:
        if not v:
            raise ValueError("elements must not be empty")
        return v


class ImplementationArrayDataType(ModelElement):
    """``kind:implementation_types.implementation_array`` — an ``IMPLEMENTATION-DATA-TYPE`` of category ``ARRAY``.

    Fixed-size semantics only: ``length`` elements of one ``element_type``, the
    one sub-element named ``element_name`` — the source's own ``SHORT-NAME``,
    required so that no export invents it.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    name: str
    kind: Literal["implementation_array"]
    element_type: str
    length: int
    element_name: str
    description: str = ""
    # Package placement, `SWComponent.package`'s terms.
    package: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Data type name")

    @field_validator("element_type")
    @classmethod
    def element_type_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("element_type must not be blank or whitespace")
        return v

    @field_validator("length")
    @classmethod
    def length_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("length must be > 0")
        return v

    @field_validator("element_name")
    @classmethod
    def element_name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("element_name must not be blank or whitespace")
        return v


class PointerDataType(ModelElement):
    """``kind:implementation_types.pointer`` — an ``IMPLEMENTATION-DATA-TYPE`` of category ``DATA_REFERENCE``.

    Its ``target`` names the implementation type it points to; a pointer to a
    pointer names another ``pointer``. A ``target`` naming the pointer itself is
    refused here; a longer cycle is ``rule:data-type.cycle``'s.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    name: str
    kind: Literal["pointer"]
    target: str
    description: str = ""
    # Package placement, `SWComponent.package`'s terms.
    package: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Data type name")

    @model_validator(mode="after")
    def validate_pointer(self) -> PointerDataType:
        if not self.target.strip():
            raise ValueError("pointer 'target' must not be blank or whitespace")
        if self.target == self.name:
            raise ValueError("pointer 'target' must not equal 'name' (self-referential pointer)")
        return self


DataType = Annotated[
    BaseDataType
    | ValueDataType
    | ReferenceDataType
    | EnumDataType
    | RecordDataType
    | ArrayDataType
    | ApplicationDataType
    | ExternalDataType
    | ImplementationRecordDataType
    | ImplementationArrayDataType
    | PointerDataType,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Mapping sets
# ---------------------------------------------------------------------------


class DataTypeMap(ModelElement):
    """``kind:mapping_sets.map`` — one application type paired with the implementation type that stores it.

    Both sides name a declared data type; which kinds each may name is
    ``check-model``'s. A blank side is refused here.
    """

    application: str
    implementation: str

    @field_validator("application", "implementation")
    @classmethod
    def reference_fields_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("data type map fields must not be blank")
        return v


class MappingSet(ModelElement):
    """``kind:mapping_sets.mapping_set`` — a ``DATA-TYPE-MAPPING-SET``.

    Maps each application type onto the implementation type that stores it.
    One application type appears at most once in one set, or the set would be
    ambiguous. An empty ``maps`` list is legal.
    """

    name: str
    maps: list[DataTypeMap] = []
    description: str = ""
    # Package placement, `SWComponent.package`'s terms.
    package: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Mapping set name")

    @field_validator("maps")
    @classmethod
    def applications_must_not_repeat(cls, v: list[DataTypeMap]) -> list[DataTypeMap]:
        seen: set[str] = set()
        for entry in v:
            if entry.application in seen:
                raise ValueError(f"application type mapped twice in one mapping set: {entry.application}")
            seen.add(entry.application)
        return v


# ---------------------------------------------------------------------------
# Units, compu methods and data constraints
# ---------------------------------------------------------------------------
#
# Three layers under `data_types/` holding the elements a type names by
# reference: `units`, `compu_methods` (discriminated by `kind`) and
# `data_constraints`. Each kind is sized to `[constr_1375]`'s category table
# and refuses an unknown key.


class Unit(ModelElement):
    """``kind:units.unit`` — a ``UNIT``: the unit a physical value is measured in.

    ``display_name`` is the text the unit is shown as (its ``DISPLAY-NAME``); a
    unit's SI relation and physical dimension are not held.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    name: str
    display_name: str = ""
    description: str = ""
    package: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Unit name")


class TableEntry(ModelElement):
    """``kind:compu_methods.table_entry`` — one value of a ``texttable``, the enum literal's shape.

    Its ``name`` is the scale's ``SHORT-LABEL`` and its ``VT``, and its ``value``
    the scale's single-point interval, exactly as an enum literal's.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    name: str
    value: int
    description: str = ""

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("TableEntry name must not be blank")
        return v


class TextTable(ModelElement):
    """``kind:compu_methods.texttable`` — a ``COMPU-METHOD`` of category ``TEXTTABLE``.

    One or more entries (``[constr_1375]``: a ``TEXTTABLE`` holds one or more
    scales). An enumeration and an implementation value or reference type may
    name one by their ``compu_method_ref``.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    name: str
    kind: Literal["texttable"]
    entries: list[TableEntry]
    description: str = ""
    package: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Compu method name")

    @field_validator("entries")
    @classmethod
    def entries_must_not_be_empty(cls, v: list[TableEntry]) -> list[TableEntry]:
        if not v:
            raise ValueError("entries must not be empty")
        return v


class LinearLimitBound(ModelElement):
    """``kind:compu_methods.linear_limit_bound`` — one limit of a ``linear``'s scale.

    :class:`ConversionLimitBound`'s shape and reading.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    value: int | float
    interval: Literal["closed", "open"] = "closed"


class LinearLimits(ModelElement):
    """``kind:compu_methods.linear_limits`` — a ``linear``'s scale limits, on the stored (internal) side.

    :class:`ConversionLimits`' shape and reading.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    lower: LinearLimitBound | None = None
    upper: LinearLimitBound | None = None


class Linear(ModelElement):
    """``kind:compu_methods.linear`` — a ``COMPU-METHOD`` of category ``LINEAR``: physical = internal × factor + offset.

    It names the ``unit`` element it refers to (``[constr_1175]``: a compu method
    other than ``TEXTTABLE``, ``BITFIELD_TEXTTABLE`` or ``IDENTICAL`` refers to a
    unit), and holds no scale label: ``[constr_1375]`` makes ``CompuScale.shortLabel``
    not applicable for ``LINEAR``. Coefficients as in :class:`Conversion`.
    ``limits`` is the scale's ``LOWER-LIMIT`` / ``UPPER-LIMIT`` on the stored
    side (``[TPS_SWCT_01548]``: the scope of the conversion).
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    name: str
    kind: Literal["linear"]
    unit: str
    factor: int | float = 1.0
    offset: int | float = 0.0
    limits: LinearLimits | None = None
    description: str = ""
    package: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Compu method name")

    @model_validator(mode="after")
    def limits_must_be_coherent(self) -> Linear:
        if self.limits is not None and (reason := _limits_incoherence(self.limits.lower, self.limits.upper)):
            raise ValueError(f"linear {reason}")
        return self

    @field_validator("unit")
    @classmethod
    def unit_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("unit must not be blank")
        return v


#: The ``compu_methods`` layer's two kinds, discriminated by ``kind``.
CompuMethod = Annotated[TextTable | Linear, Field(discriminator="kind")]


class DataConstr(ModelElement):
    """``kind:data_constraints.data_constr`` — a ``DATA-CONSTR`` of one rule of physical limits.

    ``min`` and ``max`` are the rule's ``PHYS-CONSTRS`` limits, either or both
    absent, held and judged as :class:`ValueRange`'s (the lower not above the
    upper, ``rule:data_constraints.data_constr.bounds-coherent``); ``level`` is
    the rule's ``CONSTR-LEVEL``. A constraint stating neither limit is legal.
    """

    model_config = _CLOSED_DATA_TYPE_CONFIG

    name: str
    min: int | float | None = None
    max: int | float | None = None
    level: int | None = None
    description: str = ""
    package: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Data constraint name")

    @model_validator(mode="after")
    def bounds_must_be_coherent(self) -> DataConstr:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("data constraint 'min' must be <= 'max'")
        return self


#: Any element of the three layers of the forms a type names by reference.
DataDescriptionElement = Unit | TextTable | Linear | DataConstr


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


class TaskRunnableRef(ModelElement):
    """One entry of a task's ``runnables``: a runnable of one component.

    The component is named by exactly one of ``swc`` (a type) and ``instance``
    (a ``/``-joined path of composition member names from the root composition
    down); ``check-model`` resolves the occurrence.
    """

    swc: str | None = None
    instance: str | None = None
    runnable: str

    @field_validator("swc", "instance", "runnable")
    @classmethod
    def ref_fields_must_not_be_empty(cls, v: str | None) -> str | None:
        # `None` is an absent selector, judged by `exactly_one_selector`.
        if v is not None and not v.strip():
            raise ValueError("task runnable ref fields must not be empty")
        return v

    @model_validator(mode="after")
    def exactly_one_selector(self) -> TaskRunnableRef:
        if (self.swc is None) == (self.instance is None):
            raise ValueError("a task runnable entry must declare exactly one of 'swc' and 'instance'")
        return self


class Task(ModelElement):
    name: str
    period: float  # seconds; must be > 0 (same unit as TimingEvent.period)
    priority: int  # >= 0, higher = more urgent (SWCraft convention)
    # The name of a hardware `partition`; a task's core is its partition's,
    # read through `core_of`. A file still carrying the retired `core` key fails
    # as `partition: Field required`; with both keys `core` is `model.unknown-key`.
    partition: str
    # The task's execution-time budget, an upper bound and never a measurement,
    # held as written with its unit (`1200us`); > 0 when present. `period` is
    # float seconds, the standard's `TimeValue`; a budget is the standard's
    # `MultidimensionalTime`, an integer and a unit, and
    # `execution_budget.parse_budget` is the one conversion to seconds.
    wcet: str | None = None
    description: str = ""
    runnables: list[TaskRunnableRef] = []
    # The argument for scheduling components of different safety integrity
    # levels in this task: free text, a declaration nothing verifies. Legal on
    # a task whose components are rated alike, so it may be written ahead of a
    # re-rating.
    coexistence: str = ""
    # The trace link, as `SWComponent.satisfies` states it.
    satisfies: list[str] = []

    @field_validator("name")
    @classmethod
    def name_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be empty")
        return v

    @field_validator("period")
    @classmethod
    def period_must_be_positive(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError("period must be > 0")
        return v

    @field_validator("priority")
    @classmethod
    def priority_must_be_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("priority must be >= 0")
        return v

    @field_validator("partition")
    @classmethod
    def partition_must_not_be_blank(cls, v: str) -> str:
        """Refuse a blank `partition`; resolution is `check-model`'s (`hardware.undeclared-partition`)."""
        if not v.strip():
            raise ValueError("partition must not be blank")
        return v

    @model_validator(mode="before")
    @classmethod
    def wcet_us_is_retired(cls, data: object) -> object:
        """Refuse the retired `wcet_us` spelling, naming the new one."""
        if isinstance(data, Mapping) and "wcet_us" in data:
            raise ValueError(_retired_wcet_sentence(data["wcet_us"], runnable=False))
        return data

    @field_validator("wcet", mode="before")
    @classmethod
    def wcet_is_written_as_a_budget(cls, v: object) -> object:
        return _budget_as_written(v)

    @field_validator("wcet")
    @classmethod
    def wcet_must_be_positive(cls, v: str | None) -> str | None:
        return _positive_budget(v)

    @field_validator("runnables")
    @classmethod
    def runnables_must_not_have_duplicates(cls, v: list[TaskRunnableRef]) -> list[TaskRunnableRef]:
        # Syntactic over the triple: one runnable reached through two selectors
        # is `task.resolved-duplicate-runnable`, at `check-model`.
        seen: set[tuple[str | None, str | None, str]] = set()
        for ref in v:
            key = (ref.swc, ref.instance, ref.runnable)
            if key in seen:
                raise ValueError(f"duplicate runnable mapping in task: {ref.swc or ref.instance}.{ref.runnable}")
            seen.add(key)
        return v


# ---------------------------------------------------------------------------
# Hardware
# ---------------------------------------------------------------------------


def _validate_threshold(v: float) -> float:
    """Refuse a `threshold` outside `(0, 1]`, the fraction of the declared capacity a budget may use."""
    if not math.isfinite(v) or v <= 0 or v > 1:
        raise ValueError("threshold must be a finite fraction in (0, 1]")
    return v


class Processor(ModelElement):
    """The one processor the software runs on."""

    name: str
    kind: Literal["processor"]
    description: str = ""
    # The trace link, as `SWComponent.satisfies` states it.
    satisfies: list[str] = []

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Hardware name")


class Core(ModelElement):
    """One schedulable core of the processor, and the CPU-load budget for it."""

    name: str
    kind: Literal["core"]
    threshold: float  # fraction in (0, 1]
    enforcement: Literal["strict", "advisory"] = "strict"
    # The highest ASIL this core is declared able to host. `None` is unrated,
    # not QM: nothing is compared against it. Not on `Processor`, because
    # nothing in the model is placed on a processor.
    asil_capability: Literal["QM", "A", "B", "C", "D"] | None = None
    hw_id: int | None = None
    description: str = ""
    # The trace link, as `SWComponent.satisfies` states it.
    satisfies: list[str] = []

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Hardware name")

    @field_validator("threshold")
    @classmethod
    def threshold_must_be_a_fraction(cls, v: float) -> float:
        return _validate_threshold(v)


class Memory(ModelElement):
    """One memory of the processor, the cores allowed to use it, and its budget."""

    name: str
    kind: Literal["memory"]
    type: Literal["RAM", "ROM", "EEPROM", "Flash"]
    size: int  # bytes; must be > 0
    accessible: list[str]
    threshold: float  # fraction in (0, 1]
    enforcement: Literal["strict", "advisory"] = "strict"
    # The highest ASIL this memory is declared able to host; `None` is unrated,
    # as on `Core`. Only a non-zero budget is judged against it.
    asil_capability: Literal["QM", "A", "B", "C", "D"] | None = None
    description: str = ""
    # The trace link, as `SWComponent.satisfies` states it.
    satisfies: list[str] = []

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Hardware name")

    @field_validator("size")
    @classmethod
    def size_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("size must be > 0")
        return v

    @field_validator("accessible")
    @classmethod
    def accessible_must_name_distinct_cores(cls, v: list[str]) -> list[str]:
        """At least one core, no repeats, and no blank entry."""
        if not v:
            raise ValueError("accessible must name at least one core")
        seen: set[str] = set()
        for name in v:
            if not name.strip():
                raise ValueError("accessible entries must not be blank")
            if name in seen:
                raise ValueError(f"duplicate core in accessible: {name}")
            seen.add(name)
        return v

    @field_validator("threshold")
    @classmethod
    def threshold_must_be_a_fraction(cls, v: float) -> float:
        return _validate_threshold(v)


class Partition(ModelElement):
    """One partition of the software on a core: AUTOSAR's OS-Application.

    The tasks that name it run on its ``core``. ``asil`` is the level the
    partition is, like ``SWComponent.asil`` and unlike a core's
    ``asil_capability``; ``None`` is unrated, not ``QM``. ``coexistence`` is
    the partition's memory-mixing argument, the task's being the
    execution-time one. Whether ``core`` resolves is ``check-model``'s
    (``hardware.partition-undeclared-core`` / ``hardware.partition-core-not-a-core``).

    ``trust`` is the OS-Application's class: ``non_trusted`` (confined by
    memory protection, the default), ``trusted_with_protection`` (trusted,
    confined for memory) or ``trusted`` (unrestricted for memory). Any other
    value is refused at load, with no rule id.
    """

    name: str
    kind: Literal["partition"]
    core: str
    asil: AsilValue | None = None
    trust: Literal["non_trusted", "trusted_with_protection", "trusted"] = "non_trusted"
    coexistence: str = ""
    description: str = ""
    # The trace link, as `SWComponent.satisfies` states it.
    satisfies: list[str] = []

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return _reject_path_traversal(v, "Hardware name")

    @field_validator("core")
    @classmethod
    def core_must_not_be_blank(cls, v: str) -> str:
        """Refuse a blank ``core``, which would otherwise surface as a broken reference to an empty name."""
        if not v.strip():
            raise ValueError("core must not be blank")
        return v


Hardware = Annotated[Processor | Core | Memory | Partition, Field(discriminator="kind")]


def hardware_by_name(elements: Iterable[Processor | Core | Memory | Partition]) -> dict[str, Hardware]:
    """Every hardware element by name, first file wins -- the map the two helpers below read.

    Not filtered by kind, so a name declared twice resolves to the element the
    topology check judged (``hardware.duplicate-name`` reports the second).
    """
    by_name: dict[str, Hardware] = {}
    for element in elements:
        by_name.setdefault(element.name, element)
    return by_name


def partition_of(task: Task, by_name: Mapping[str, Hardware]) -> Partition | None:
    """The partition *task* runs in, or ``None`` when ``partition`` names no partition.

    *by_name* is :func:`hardware_by_name`'s map. ``None`` covers both an
    undeclared name and one naming another kind (``hardware.undeclared-partition``
    / ``hardware.partition-not-a-partition`` tell them apart).
    """
    target = by_name.get(task.partition)
    return target if isinstance(target, Partition) else None


def core_of_partition(partition: Partition, by_name: Mapping[str, Hardware]) -> Core | None:
    """The core *partition* is on, or ``None`` when its ``core`` names no core.

    The second hop of :func:`core_of`. ``None`` covers both an undeclared name
    and one naming another kind (``hardware.partition-undeclared-core`` /
    ``hardware.partition-core-not-a-core`` tell them apart).
    """
    target = by_name.get(partition.core)
    return target if isinstance(target, Core) else None


def core_of(task: Task, by_name: Mapping[str, Hardware]) -> Core | None:
    """The core *task* runs on: its partition's, the one derivation of a task's core.

    ``None`` when the task's partition does not resolve (:func:`partition_of`) or
    the partition's ``core`` names no core (:func:`core_of_partition`). Every
    reader of a task's placement resolves it through these two helpers, except
    the topology pass, which must tell an undeclared name from a mis-kinded one.
    """
    partition = partition_of(task, by_name)
    if partition is None:
        return None
    return core_of_partition(partition, by_name)


# ---------------------------------------------------------------------------
# Sequence
# ---------------------------------------------------------------------------

#: The grammar of a sequence's ``name``, which names its diagram files. Not the
#: AUTOSAR identifier rule (a leading underscore, no length limit), because no
#: sequence is emitted into ARXML (``rule:sequences.sequence.name.identifier``).
_SEQUENCE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SequenceStep(ModelElement):
    """One step of a sequence: a runnable of one component, as ``TaskRunnableRef``.

    The component is named by exactly one of ``swc`` and ``instance``.
    """

    swc: str | None = None
    instance: str | None = None
    runnable: str
    note: str = ""

    @field_validator("swc", "instance", "runnable")
    @classmethod
    def ref_fields_must_not_be_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("sequence step ref fields must not be empty")
        return v

    @model_validator(mode="after")
    def exactly_one_selector(self) -> SequenceStep:
        if (self.swc is None) == (self.instance is None):
            raise ValueError("a sequence step must declare exactly one of 'swc' and 'instance'")
        return self


class Sequence(ModelElement):
    name: str
    task: str
    title: str = ""
    steps: list[SequenceStep]

    @field_validator("name")
    @classmethod
    def name_must_not_be_empty_and_must_be_identifier(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be empty")
        if not _SEQUENCE_NAME_RE.match(v):
            raise ValueError(
                "name must be an identifier, because it names the sequence's diagram files "
                r"(match ^[A-Za-z_][A-Za-z0-9_]*$; no spaces, hyphens, "
                "leading digits, or other special characters)"
            )
        return v

    @field_validator("task")
    @classmethod
    def task_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("task must not be empty")
        return v

    @field_validator("steps")
    @classmethod
    def steps_must_not_be_empty_or_have_duplicate_pairs(cls, v: list[SequenceStep]) -> list[SequenceStep]:
        if not v:
            raise ValueError("steps must not be empty")
        # Syntactic over the triple, as `Task.runnables_must_not_have_duplicates`.
        seen: set[tuple[str | None, str | None, str]] = set()
        for step in v:
            key = (step.swc, step.instance, step.runnable)
            if key in seen:
                raise ValueError(
                    f"duplicate {{swc, instance, runnable}} step in sequence steps: "
                    f"{step.swc or step.instance}.{step.runnable}"
                )
            seen.add(key)
        return v
