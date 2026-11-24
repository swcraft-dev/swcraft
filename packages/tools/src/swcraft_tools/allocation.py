"""The allocation experiment (CARMA): where a component can move, and what the move buys.

- **The configuration**: :class:`AllocationConfig`, :func:`load_config` and the model-dependent
  :func:`check_config_names`.
- **The projections** (:func:`project`): each component type's partition and core, and whether it
  is free to move or why not; the freedom table (every free component against every other
  partition, *admissible* or every reason with its class, the row's class the highest by
  :data:`CLASS_PRECEDENCE`); the link graph (one link per sender-receiver data element, costed
  ``size x frequency`` in bytes x Hz as an exact ``Fraction``, the uncostable links excluded and
  listed, client-server calls counted, never costed); and the current state (core loads and
  thresholds, inter-partition volume and calls).
- **The search** (:func:`solve_curve`): ``scipy.optimize.milp`` (HiGHS). Every move set it proposes
  is checked by ``check_cross_layer_consistency`` on a scratch copy of the model before it is
  reported; only an error finding refuses a move.
- **The report** (:func:`result_document`, :func:`render_markdown`, :func:`run_allocation`):
  ``result.json`` and the ``report.md`` rendered from it, names only, every figure an integer or a
  rational, written under ``<dist_root>/experiments/<name>/``.

Every load and share is a ``Fraction`` from :mod:`swcraft_tools.resource_analysis`. The module never
imports ``writer`` nor ``editing``: an experiment writes nothing into the model. It does not reach:

- a type placed more than once, or nowhere, which is not free (the unit of a move is a type);
- a per-core cost: a runnable costs the same on every core, and a moved periodic runnable joins an
  existing task of its period in the target partition (no task is created, moved or re-budgeted);
- an initialization or server runnable, never moved nor costed; a free caller of a server may move,
  its call then counted as crossing;
- a physical size: a size is bits over eight, and an application type is sized only through the
  implementation type its sender's mapping sets pair it with.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
import os
import shutil
import tempfile
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator

from swcraft_tools._version import __version__
from swcraft_tools.data_description import DataDescription
from swcraft_tools.exclusive_create import create_unique_sibling
from swcraft_tools.execution_budget import (
    MAX_FACTOR_DIGITS,
    SECONDS_PER_UNIT,
    Budget,
    finest,
    format_exact,
    parse_budget,
)
from swcraft_tools.model_registry import current_registry, reserved_device_name, segment_refusal
from swcraft_tools.model_state import ModelState, load_model_state, scan_layer, stored_element_files
from swcraft_tools.project import (
    EXPERIMENT_REPORT_FILENAME,
    EXPERIMENT_RESULT_FILENAME,
    SwcraftProject,
    experiment_default_directory,
    experiment_output_directory,
    project_relative_name,
)
from swcraft_tools.resource_analysis import (
    ResourceAnalysis,
    analyze_resource_budgets,
    exact,
    format_decimal,
    format_percent,
    is_core,
    runnable_utilization,
)
from swcraft_tools.safety import asil_rank
from swcraft_tools.scheduling_identity import SchedulingIdentities, identities_from
from swcraft_tools.schema import (
    BaseDataType,
    CallReturnedEvent,
    Composition,
    Connector,
    Core,
    CSInterface,
    DataElement,
    DataEvent,
    ExternalDataType,
    ExternalInterface,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    InitEvent,
    Interface,
    MappingSet,
    Memory,
    OperationEvent,
    Partition,
    PointerDataType,
    ReferenceDataType,
    Runnable,
    SRInterface,
    SWComponent,
    Task,
    TimingEvent,
    ValueDataType,
)
from swcraft_tools.validator import check_cross_layer_consistency, component_ends, effective_asil_levels
from swcraft_tools.yaml_load import load_yaml_document

if TYPE_CHECKING:
    from swcraft_tools.arxml_version import ArxmlVersion

# ---------------------------------------------------------------------------
# The errors
# ---------------------------------------------------------------------------


class AllocationError(Exception):
    """Every refusal and failure of the experiment: ``swcraft experiment allocation`` prints its sentence and exits 1.

    Each sentence names a key, a component, a partition or a count -- never a path.
    """


class ExperimentError(AllocationError, ValueError):
    """A configuration file, or a name it uses, the experiment refuses -- the sentence names the key, never a path."""


class ConfigFileUnreadableError(ExperimentError):
    """The configuration file cannot be read at all; the sentence carries the reason, the caller names the file."""


class ModelRefusedError(ExperimentError):
    """The model has ``check-model`` error findings: the table explains, the gate is the gate.

    The count is the error findings'. A warning refuses nothing here, as in ``check-model``.
    """

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(
            f"the model is refused: check-model reports {count} error{'' if count == 1 else 's'} "
            "(run swcraft check-model to see them), and an experiment runs only on a model the gate passes"
        )


class SolverMissingError(ExperimentError):
    """The solver ``scipy`` cannot be imported, so the allocation search cannot run."""


class SolverUnloadableError(SolverMissingError):
    """``scipy`` is installed, but importing it fails other than by its absence: its native libraries will not load."""


class NotRepresentableError(ExperimentError):
    """A constraint, the objective or the tolerance bound does not fit the solver's integers."""


class SolverExecutionError(AllocationError, RuntimeError):
    """The solver answered an error status, or a solution that is no exact 0/1 assignment: never a curve status."""


# ---------------------------------------------------------------------------
# The configuration file
# ---------------------------------------------------------------------------

#: ``time_limit_s``' default: the smallest of 10, 30 and 60 seconds that is at least
#: five times the slowest time to a proved optimum measured on a 160-component model
#: with 30 free (0.0139 s).
DEFAULT_TIME_LIMIT_S = 10.0

#: The experiment's kind, and the name a configuration that names none takes: the
#: report of a run with no ``name`` goes to ``<dist_root>/experiments/allocation/``.
DEFAULT_NAME = "allocation"


class AllocationSearch(BaseModel):
    """The configuration's ``search`` section: how far the search goes and how much it lists.

    ``extra="forbid"`` and ``strict``, as :class:`AllocationConfig`.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    max_moves: int = Field(default=3, ge=1)
    candidates: int = Field(default=3, ge=1)
    #: The curve point the candidates are enumerated at; ``max_moves`` when omitted.
    candidates_at: int | None = Field(default=None, ge=1, validate_default=True)
    #: A fraction of the point's optimum, in ``[0, 1]``, taken exactly (its shortest decimal).
    tolerance: float = Field(default=0.0, ge=0, le=1, allow_inf_nan=False)
    #: One deadline per curve point, in seconds: finite and above zero (``.inf`` would pass ``gt=0``).
    time_limit_s: float = Field(default=DEFAULT_TIME_LIMIT_S, gt=0, allow_inf_nan=False)

    @field_validator("candidates_at")
    @classmethod
    def _candidates_at_within_the_curve(cls, value: int | None, info: ValidationInfo) -> int | None:
        # `max_moves` is absent from `info.data` when it is itself refused.
        if "max_moves" not in info.data:
            return value
        max_moves = info.data["max_moves"]
        if value is None:
            return max_moves
        if value > max_moves:
            raise ValueError(
                f"candidates_at ({value}) must be at most max_moves ({max_moves}): "
                "the candidates are enumerated at a point of the curve"
            )
        return value


class AllocationConstraints(BaseModel):
    """The configuration's ``constraints`` section: what the search may not move, or not move there."""

    model_config = ConfigDict(extra="forbid", strict=True)

    locked: list[str] = []
    #: Per component, the only partitions it may move to.
    allowed_partitions: dict[str, list[str]] = {}


class AllocationConfig(BaseModel):
    """One allocation experiment, as its configuration file declares it -- every key optional.

    Unknown keys are refused (``extra="forbid"``), each named by its dotted key.
    ``strict``: a ``"3"`` or a ``true`` is not a count, and an explicit
    ``name: null`` is refused, not read as the default (an absent key is).
    ``kind`` is the class's, never the file's: the command line names the kind.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    #: The kind, from the class: a ``kind`` key in the file is an unknown key.
    kind: ClassVar[str] = DEFAULT_NAME

    #: The report directory's name: one directory segment, never a device name Windows reserves.
    name: str = DEFAULT_NAME
    #: The report directory, relative to the project directory; ``<dist_root>/experiments/<name>/`` when absent.
    output: str | None = None
    search: AllocationSearch = AllocationSearch()
    constraints: AllocationConstraints = AllocationConstraints()

    @field_validator("name")
    @classmethod
    def _name_is_one_directory_segment(cls, value: str) -> str:
        refusal = segment_refusal(value, "name", directory=True)
        if refusal is not None:
            raise ValueError(refusal)
        # The directory form does not ask the device-name question; the report
        # directory is created here, so it is asked on every platform.
        device = reserved_device_name(value)
        if device is not None:
            raise ValueError(
                f"name '{value}' names a device Windows reserves ({device}): the report directory "
                "could not exist on Windows"
            )
        if value.endswith((" ", ".")):
            raise ValueError(
                f"name '{value}' ends in a space or a dot, which Windows strips from a directory name: "
                "the report directory would not carry the name"
            )
        return value


_PYDANTIC_VALUE_ERROR = "Value error, "


def _refusal_lines(error: ValidationError) -> list[str]:
    """One line per pydantic error, each naming its key and never the input's location on disk."""
    lines: list[str] = []
    for item in error.errors():
        message = str(item["msg"])
        if message.startswith(_PYDANTIC_VALUE_ERROR):
            message = message[len(_PYDANTIC_VALUE_ERROR) :]
        key = ".".join(str(part) for part in item["loc"])
        lines.append(f"'{key}': {message}" if key else message)
    return lines


def parse_config(document: object) -> AllocationConfig:
    """The configuration *document* (an already-parsed YAML value) declares, or :class:`ExperimentError`.

    ``None`` -- an empty file, or one of comments only -- is the defaults, the
    same configuration as no file at all.
    """
    if document is None:
        return AllocationConfig()
    if not isinstance(document, dict):
        raise ExperimentError("the configuration file must be a mapping of keys to values")
    try:
        return AllocationConfig.model_validate(document)
    except ValidationError as exc:
        raise ExperimentError("the configuration file is refused: " + "; ".join(_refusal_lines(exc))) from None


def load_config(path: str | Path) -> tuple[AllocationConfig, str]:
    """Read and validate the configuration file at *path*: ``(configuration, "sha256:<hex>")``.

    The digest is of the bytes exactly (a CRLF copy digests differently and
    parses equal); they are decoded as strict UTF-8. A refusal names the key and
    never the path.

    Raises:
        OSError: the file cannot be read.
        UnicodeDecodeError: the bytes are not UTF-8.
        ExperimentError: the text is not YAML, not a mapping, or the mapping is refused.
    """
    data = Path(path).read_bytes()
    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    text = data.decode("utf-8")
    try:
        document = load_yaml_document(text)
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None) or "it does not parse"
        raise ExperimentError(f"the configuration file is not valid YAML: {problem}") from None
    return parse_config(document), digest


def check_config_names(experiment: AllocationConfig, state: ModelState) -> None:
    """Refuse a component or partition name *experiment* uses that the model does not declare.

    Raises:
        ExperimentError: the first undeclared name by its dotted key, ``locked`` before ``allowed_partitions``.
    """
    components = {swc.name for swc in state["swc"]}
    partitions = {element.name for element in state["hardware"] if isinstance(element, Partition)}
    for name in experiment.constraints.locked:
        if name not in components:
            raise ExperimentError(f"'constraints.locked' names component '{name}', which the model does not declare")
    for name, allowed in experiment.constraints.allowed_partitions.items():
        if name not in components:
            raise ExperimentError(
                f"'constraints.allowed_partitions' names component '{name}', which the model does not declare"
            )
        for partition in allowed:
            if partition not in partitions:
                raise ExperimentError(
                    f"'constraints.allowed_partitions' of '{name}' names partition '{partition}', "
                    "which the model does not declare"
                )


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

#: The status of a component no move is offered for -- why is each entry of its ``reasons``.
NOT_FREE_UNDEFINED_WALK = SchedulingIdentities.undefined_reason
NOT_FREE_PLACED_NOWHERE = "placed nowhere (no occurrence below the root composition)"
NOT_FREE_EXTERNAL = "an external component stub"
NOT_FREE_NO_RUNNABLE = "declares no runnable, so it runs in no partition"
NOT_FREE_TWO_PARTITIONS = "its runnables run in tasks of more than one partition"
NOT_FREE_NO_TASKED_RUNNABLE = (
    "no runnable runs in a task, so its partition is not established (initialization and server runnables only)"
)
#: The data gaps among the reasons: the move is refused for want of a declaration, not by the model.
DATA_GAP_PREFIX = "data gap: "


def _placed_n_times(count: int) -> str:
    return f"placed {count} times (a move takes a component placed exactly once)"


def _runnable_in_no_task(runnable: str) -> str:
    return f"runnable '{runnable}' runs in no task"


def _server_in_no_task(runnable: str) -> str:
    return (
        f"server runnable '{runnable}' runs in its callers' tasks (a direct call), "
        "whose cost the experiment does not model"
    )


def _initialization_in_task(runnable: str, task: str) -> str:
    return (
        f"initialization runnable '{runnable}' runs in task '{task}'; moving an initialization mapping is not modelled"
    )


def _server_in_task(runnable: str, task: str) -> str:
    return (
        f"server runnable '{runnable}' runs in task '{task}' at its callers' rate, which the experiment does not model"
    )


def _data_received_in_task(runnable: str, task: str) -> str:
    return (
        f"data-received runnable '{runnable}' runs in task '{task}' at its data's arrival rate, "
        "which the model does not hold"
    )


def _call_returned_in_task(runnable: str, task: str) -> str:
    return (
        f"call-returned runnable '{runnable}' runs in task '{task}' when an asynchronous call returns, "
        "at a rate the model does not hold"
    )


def _initialization_assumption(component: str, runnable: str) -> str:
    return (
        f"initialization runnable '{runnable}' of '{component}' runs in no task: activated once at start "
        "(`Rte_Init` or an initialization task), outside the steady-state load"
    )


def _runnable_without_timing(runnable: str) -> str:
    return f"runnable '{runnable}' has no timing event, so its period is not defined"


def _runnable_without_cost(runnable: str) -> str:
    return f"{DATA_GAP_PREFIX}runnable '{runnable}' declares no wcet"


def _task_without_budget(task: str) -> str:
    return f"{DATA_GAP_PREFIX}task '{task}' declares no budget (wcet)"


@dataclass(frozen=True)
class RunnablePlacement:
    """One runnable of a component, in the task that runs it."""

    runnable: str
    task: str
    #: The task's period, seconds -- the period the runnable runs at, and the one its target task must share.
    period: float
    #: The runnable's declared ``wcet``, parsed; ``None`` without one.
    budget: Budget | None
    #: The runnable's declared ``wcet_type``; ``None`` without a budget.
    wcet_type: Literal["gross", "net"] | None
    #: ``runnable_utilization(budget, period)``, or ``None`` without a declared budget.
    share: Fraction | None


@dataclass(frozen=True)
class ComponentPlacement:
    """One component type: where it runs, its effective level, and whether it is free to move."""

    component: str
    #: The effective ASIL (``validator.effective_asil_levels``: ``QM`` when nothing rates it).
    level: str
    #: The partition its task-mapped runnables run in, or ``None`` when that is not established.
    partition: str | None
    core: str | None
    #: The runnables a move carries and the share counts: initialization and server runnables are never among them.
    runnables: tuple[RunnablePlacement, ...]
    #: Why the component is not free; empty when it is.
    reasons: tuple[str, ...]
    #: The assumptions the placement made -- an initialization runnable in no task -- reported, never a reason.
    assumptions: tuple[str, ...]

    @property
    def free(self) -> bool:
        return not self.reasons

    @property
    def share(self) -> Fraction:
        """The sum of the runnables' declared shares (exact)."""
        return sum((r.share for r in self.runnables if r.share is not None), Fraction(0))


# ---------------------------------------------------------------------------
# The freedom table
# ---------------------------------------------------------------------------

CLASS_HARD = "hard"
CLASS_SAFETY = "needs a safety decision"
CLASS_REMOVABLE = "removable"
CLASS_LOCKED = "locked"
ADMISSIBLE = "admissible"

#: The row's class is the highest of its reasons' by this order: what a hardware
#: change would open is never offered as an edit of the experiment.
CLASS_PRECEDENCE: tuple[str, ...] = (CLASS_HARD, CLASS_SAFETY, CLASS_REMOVABLE, CLASS_LOCKED)


@dataclass(frozen=True)
class Reason:
    """One reason a target partition is not admissible, with what would open it."""

    klass: str
    #: The model rule the move would break, or ``None`` for a condition of the experiment's own.
    rule: str | None
    text: str
    opens: str


@dataclass(frozen=True)
class TargetRow:
    """One other partition, for one free component."""

    partition: str
    core: str | None
    reasons: tuple[Reason, ...]
    #: Runnable -> the task it would join (the first by name of the period, with a budget).
    target_tasks: Mapping[str, str]
    #: The partition declares no ``asil``: admissible for level purposes, listed as an assumption.
    unrated: bool

    @property
    def klass(self) -> str:
        if not self.reasons:
            return ADMISSIBLE
        present = {reason.klass for reason in self.reasons}
        return next(klass for klass in CLASS_PRECEDENCE if klass in present)

    @property
    def admissible(self) -> bool:
        return not self.reasons


@dataclass(frozen=True)
class FreedomRow:
    """One component of the table: its placement and, when it is free, every other partition."""

    placement: ComponentPlacement
    targets: tuple[TargetRow, ...]

    def target(self, partition: str) -> TargetRow:
        return next(row for row in self.targets if row.partition == partition)

    @property
    def domain(self) -> tuple[str, ...]:
        """The partitions the search may place the component in: its own, then the admissible ones, by name."""
        if self.placement.partition is None or not self.placement.free:
            return ()
        return (self.placement.partition, *(row.partition for row in self.targets if row.admissible))


# ---------------------------------------------------------------------------
# The link graph
# ---------------------------------------------------------------------------

UNSIZED_SHAPE = "unsized (a type shape the projection does not size)"
UNSIZED_NO_MAPPING = "unsized (no implementation mapping)"
UNSIZED_UNDECLARED = "unsized (the type is not declared)"
UNSIZED_EXTERNAL_INTERFACE = "unsized (the interface is an external stub)"
FREQUENCY_NOT_ESTABLISHED = (
    "frequency not established (no writer and no runnable of the sending component with a timing event)"
)


def _end_not_established(component: str, why: str) -> str:
    return f"the partition of '{component}' is not established ({why})"


@dataclass(frozen=True)
class Link:
    """One data element of one sender-receiver connection between two components."""

    connector: str
    sender: str
    sender_port: str
    receiver: str
    receiver_port: str
    interface: str
    element: str
    #: The logical size in bits, or ``None`` when unsized.
    size_bits: int | None
    #: The link's period in seconds (the shortest of its writers'), or ``None``.
    period: float | None
    #: No runnable of the sender with a timing event writes the element: the period is the sender's shortest
    #: timing event's.
    estimated: bool
    #: Why the link is excluded from the objective; empty for a costed link.
    exclusions: tuple[str, ...]

    @property
    def costed(self) -> bool:
        return not self.exclusions

    @property
    def size(self) -> Fraction | None:
        """Bytes, exactly: the bits over eight, never rounded."""
        return None if self.size_bits is None else Fraction(self.size_bits, 8)

    @property
    def frequency(self) -> Fraction | None:
        """Hz, exactly: one over the period taken as its shortest decimal (``resource_analysis.exact``)."""
        return None if self.period is None else 1 / exact(self.period)

    @property
    def cost(self) -> Fraction:
        """``size x frequency`` in bytes x Hz; zero for an excluded link (it is never costed)."""
        if not self.costed or self.size is None or self.frequency is None:
            return Fraction(0)
        return self.size * self.frequency


@dataclass(frozen=True)
class CallLink:
    """One ``calls`` entry of a client runnable on a client-server connection: counted, never costed."""

    connector: str
    client: str
    client_port: str
    runnable: str
    operation: str
    server: str
    server_port: str
    exclusions: tuple[str, ...]

    @property
    def counted(self) -> bool:
        return not self.exclusions


# ---------------------------------------------------------------------------
# The current state, and the projection
# ---------------------------------------------------------------------------


#: The enforcement under which a core's threshold is a constraint of the search; every other is reported only.
ENFORCEMENT_STRICT = "strict"


@dataclass(frozen=True)
class CoreLoad:
    """One core's load and threshold, ``analyze_resource_budgets``' exact figures."""

    core: str
    enforcement: str
    load: Fraction
    threshold: Fraction

    @property
    def breached(self) -> bool:
        return self.load > self.threshold

    @property
    def advisory_breached(self) -> bool:
        """Over its threshold on a core that is not :data:`ENFORCEMENT_STRICT`: reported, never constrained."""
        return self.enforcement != ENFORCEMENT_STRICT and self.breached


@dataclass(frozen=True)
class Projection:
    """Everything the search and the report read about the model, computed once."""

    #: Partition -> its core's name (``None`` when the core does not resolve), every partition by name.
    partitions: Mapping[str, str | None]
    table: tuple[FreedomRow, ...]
    links: tuple[Link, ...]
    calls: tuple[CallLink, ...]
    #: Every core's load and threshold now; the volume and the calls now are :attr:`current_volume` and
    #: :attr:`current_calls`.
    loads: tuple[CoreLoad, ...]
    #: Every data gap and assumption the projection met, in a stable order.
    data_gaps: tuple[str, ...]
    #: The occurrences the placement was derived through, kept for the per-candidate copies.
    identities: SchedulingIdentities

    def row(self, component: str) -> FreedomRow:
        return next(row for row in self.table if row.placement.component == component)

    @property
    def placements(self) -> dict[str, ComponentPlacement]:
        """Component -> its placement."""
        return {row.placement.component: row.placement for row in self.table}

    @functools.cached_property
    def sets(self) -> LinkSets:
        """The costed links split by the domains (:func:`link_sets`), computed once."""
        return link_sets(self)

    @property
    def current_volume(self) -> Fraction:
        """The inter-partition volume now, bytes x Hz: ``C0`` plus the VARIABLE links that cross."""
        return self.sets.objective({})

    @property
    def current_calls(self) -> int:
        """The counted ``calls`` entries whose two ends run in different partitions now."""
        return _crossing_calls(self, self.sets, {})


# ---------------------------------------------------------------------------
# Building the projection
# ---------------------------------------------------------------------------


class _Named(Protocol):
    @property
    def name(self) -> str: ...


_N = TypeVar("_N", bound=_Named)


def _first_by_name(elements: Sequence[_N]) -> dict[str, _N]:
    index: dict[str, _N] = {}
    for element in elements:
        index.setdefault(element.name, element)
    return index


def _timing_period(runnable: Runnable) -> float | None:
    """The shortest of the runnable's timing events' periods, or ``None`` without one."""
    periods = [event.period for event in runnable.events if isinstance(event, TimingEvent)]
    return min(periods, key=exact) if periods else None


RunnableKind = Literal["periodic", "initialization", "server", "data-received", "call-returned", "event-less"]


def runnable_kind(runnable: Runnable) -> RunnableKind:
    """The runnable's kind, read from its events with ``isinstance``.

    *periodic*: at least one ``TimingEvent``; *initialization*: at least one
    event and every event an ``InitEvent``; *server*: no ``TimingEvent`` and at
    least one ``OperationEvent`` (an ``InitEvent``, a ``DataEvent`` or a
    ``CallReturnedEvent`` beside it does not change the class); *data-received*:
    no ``TimingEvent``, no ``OperationEvent`` and at least one ``DataEvent`` (an
    ``InitEvent`` or a ``CallReturnedEvent`` beside it does not change the
    class); *call-returned*: no ``TimingEvent``, ``OperationEvent`` or
    ``DataEvent`` and at least one ``CallReturnedEvent`` (an ``InitEvent``
    beside it does not change the class); *event-less*: no event at all. A new
    event kind would fall into *event-less* undecided, so it must be classified
    here before it is relied on.
    """
    events = runnable.events
    if any(isinstance(event, TimingEvent) for event in events):
        return "periodic"
    if events and all(isinstance(event, InitEvent) for event in events):
        return "initialization"
    if any(isinstance(event, OperationEvent) for event in events):
        return "server"
    if any(isinstance(event, DataEvent) for event in events):
        return "data-received"
    if any(isinstance(event, CallReturnedEvent) for event in events):
        return "call-returned"
    return "event-less"


@dataclass(frozen=True)
class _RunnablesOfAComponent:
    """What :func:`_runnable_placements` reads from one component's runnables."""

    #: The periodic and event-less runnables in a task, the ones the share counts (a move carries periodic only).
    placed: tuple[RunnablePlacement, ...]
    #: The partitions the tasks of every task-mapped runnable name (every task, in order).
    partition_names: tuple[str, ...]
    #: A runnable in no task, an initialization, server, data-received or call-returned runnable the experiment
    #: does not model, the data gaps.
    reasons: tuple[str, ...]
    assumptions: tuple[str, ...]
    #: Whether every periodic, data-received, call-returned or event-less runnable runs in a task.
    runs_in_tasks: bool
    #: Whether any runnable runs in a task at all.
    any_in_task: bool


def _runnable_placements(swc: SWComponent, running: Mapping[tuple[str, str], list[Task]]) -> _RunnablesOfAComponent:
    """Each runnable of *swc* in the first task that runs it.

    An initialization or server runnable in no task neither erases nor
    establishes the partition; the caller decides the component-level case of
    no runnable in a task. A data-received or call-returned runnable needs a
    task, as a periodic one does: in no task it is a *runnable in no task*; in a
    task it is never moved and never costed, as a server is.
    """
    placed: list[RunnablePlacement] = []
    partition_names: list[str] = []
    reasons: list[str] = []
    assumptions: list[str] = []
    runs_in_tasks = True
    any_in_task = False
    for runnable in swc.runnables:
        kind = runnable_kind(runnable)
        in_tasks = running.get((swc.name, runnable.name), [])
        if not in_tasks:
            if kind == "initialization":
                assumptions.append(_initialization_assumption(swc.name, runnable.name))
            elif kind == "server":
                reasons.append(_server_in_no_task(runnable.name))
            else:
                reasons.append(_runnable_in_no_task(runnable.name))
                runs_in_tasks = False
            continue
        any_in_task = True
        task = in_tasks[0]
        partition_names.extend(t.partition for t in in_tasks)
        # Never moved, never costed.
        unmodelled = kind in ("initialization", "server", "data-received", "call-returned")
        if kind == "initialization":
            reasons.append(_initialization_in_task(runnable.name, task.name))
        elif kind == "server":
            reasons.append(_server_in_task(runnable.name, task.name))
        elif kind == "data-received":
            reasons.append(_data_received_in_task(runnable.name, task.name))
        elif kind == "call-returned":
            reasons.append(_call_returned_in_task(runnable.name, task.name))
        else:
            if kind == "event-less":
                reasons.append(_runnable_without_timing(runnable.name))
            if runnable.wcet is None:
                reasons.append(_runnable_without_cost(runnable.name))
        for each in in_tasks:
            if each.wcet is None and _task_without_budget(each.name) not in reasons:
                reasons.append(_task_without_budget(each.name))
        if unmodelled:
            continue
        budget = None if runnable.wcet is None else parse_budget(runnable.wcet)
        share = None if budget is None else runnable_utilization(budget, task.period)
        placed.append(RunnablePlacement(runnable.name, task.name, task.period, budget, runnable.wcet_type, share))
    return _RunnablesOfAComponent(
        tuple(placed), tuple(partition_names), tuple(reasons), tuple(assumptions), runs_in_tasks, any_in_task
    )


def _placements(
    swcs: Sequence[SWComponent],
    tasks: Sequence[Task],
    partitions: Mapping[str, Partition],
    cores: Mapping[str, Core],
    identities: SchedulingIdentities,
    levels: Mapping[str, str],
) -> dict[str, ComponentPlacement]:
    # (component, runnable) -> the tasks running it, in task-name order.
    running: dict[tuple[str, str], list[Task]] = {}
    for task in sorted(tasks, key=lambda t: t.name):
        for ref in task.runnables:
            component = identities.type_of(ref.swc, ref.instance)
            if component is None:
                continue
            bucket = running.setdefault((component, ref.runnable), [])
            if task not in bucket:
                bucket.append(task)
    placements = identities.placements
    answer: dict[str, ComponentPlacement] = {}
    for swc in sorted(swcs, key=lambda s: s.name):
        if swc.name in answer:
            continue  # a duplicate name: check-model refuses the model first
        reasons: list[str] = []
        established = True
        if swc.external:
            reasons.append(NOT_FREE_EXTERNAL)
            established = False
        elif placements is None:
            reasons.append(NOT_FREE_UNDEFINED_WALK)
            established = False
        elif swc.name not in placements:
            reasons.append(NOT_FREE_PLACED_NOWHERE)
            established = False
        elif placements[swc.name].count > 1:
            reasons.append(_placed_n_times(placements[swc.name].count))
            established = False
        if not swc.runnables and not swc.external:
            reasons.append(NOT_FREE_NO_RUNNABLE)
            established = False
        of_runnables = _runnable_placements(swc, running)
        reasons.extend(of_runnables.reasons)
        if swc.runnables and not of_runnables.any_in_task and of_runnables.runs_in_tasks and not swc.external:
            # Every runnable is an initialization or server runnable in no task: no partition evidence at all.
            reasons.append(NOT_FREE_NO_TASKED_RUNNABLE)
        established = established and of_runnables.runs_in_tasks
        distinct = list(dict.fromkeys(of_runnables.partition_names))
        if len(distinct) > 1:
            reasons.append(NOT_FREE_TWO_PARTITIONS)
            established = False
        partition = distinct[0] if established and len(distinct) == 1 else None
        core = None
        if partition is not None:
            element = partitions.get(partition)
            if element is not None and element.core in cores:
                core = element.core
        answer[swc.name] = ComponentPlacement(
            component=swc.name,
            level=levels.get(swc.name, "QM"),
            partition=partition,
            core=core,
            runnables=of_runnables.placed,
            reasons=tuple(reasons),
            assumptions=of_runnables.assumptions,
        )
    return answer


def _task_levels(task: Task, identities: SchedulingIdentities, levels: Mapping[str, str]) -> dict[str, str]:
    """Component -> effective level, for every component *task* runs."""
    answer: dict[str, str] = {}
    for ref in task.runnables:
        component = identities.type_of(ref.swc, ref.instance)
        if component is not None:
            answer.setdefault(component, levels.get(component, "QM"))
    return answer


def _hard_reasons(level: str, core: Core | None, budgeted: Sequence[Memory]) -> list[Reason]:
    """*hard*: what only a change of hardware would open -- the core's capability, a memory it cannot reach."""
    reasons: list[Reason] = []
    if core is None:
        return reasons
    # The core's capability -- CARMA's own condition, not a model rule.
    if core.asil_capability is not None and asil_rank(core.asil_capability) < asil_rank(level):
        reasons.append(
            Reason(
                CLASS_HARD,
                None,
                f"core '{core.name}' is declared able to host ASIL {core.asil_capability}, "
                f"below the component's ASIL {level}",
                "a change of hardware or of the allocation of requirements",
            )
        )
    for memory in budgeted:
        if core.name not in memory.accessible:
            reasons.append(
                Reason(
                    CLASS_HARD,
                    "rule:hardware.unreachable-memory",
                    f"memory '{memory.name}', which the component budgets, is not accessible from core '{core.name}'",
                    "re-placing the budget -- a separate change",
                )
            )
    return reasons


def _partition_level_reasons(level: str, partition: Partition, budgeted: Sequence[Memory]) -> list[Reason]:
    """*needs a safety decision*: the partition's level against the component's, rated partitions only."""
    reasons: list[Reason] = []
    if partition.asil is None:
        return reasons
    rank = asil_rank(level)
    declared = asil_rank(partition.asil)
    if declared < rank:
        reasons.append(
            Reason(
                CLASS_SAFETY,
                "rule:safety.component-above-partition",
                f"partition '{partition.name}' is rated ASIL {partition.asil}, below the component's ASIL {level}",
                "a new partition at that level -- content is chosen for a level, a level is never raised to fit",
            )
        )
    elif declared > rank and not partition.coexistence:
        reasons.append(
            Reason(
                CLASS_SAFETY,
                "rule:safety.component-below-partition",
                f"partition '{partition.name}' is rated ASIL {partition.asil}, above the component's "
                f"ASIL {level}, and declares no coexistence argument",
                "the partition's coexistence argument -- never proposed here",
            )
        )
    for memory in budgeted:
        if memory.asil_capability is not None and asil_rank(memory.asil_capability) < declared:
            reasons.append(
                Reason(
                    CLASS_SAFETY,
                    "rule:safety.partition-memory-below-level",
                    f"memory '{memory.name}', which the component budgets, is declared able to host "
                    f"ASIL {memory.asil_capability}, below the partition's ASIL {partition.asil}",
                    "raising the memory's capability or dropping the budget -- a separate change",
                )
            )
    return reasons


def _removable_reasons(
    placement: ComponentPlacement, partition_name: str, tasks: Sequence[Task]
) -> tuple[list[Reason], dict[str, str]]:
    """*removable*: a task of each runnable's period, declaring a budget, among the partition's *tasks*.

    Returns the reasons and runnable -> the task it would join (the first by
    name of its period with a budget).
    """
    target_tasks: dict[str, str] = {}
    missing: list[float] = []
    for runnable in placement.runnables:
        period = exact(runnable.period)
        candidates = [task for task in tasks if task.wcet is not None and exact(task.period) == period]
        if not candidates:
            if runnable.period not in missing:
                missing.append(runnable.period)
            continue
        target_tasks[runnable.runnable] = min(candidates, key=lambda t: t.name).name
    reasons = [
        Reason(
            CLASS_REMOVABLE,
            None,
            f"partition '{partition_name}' has no task of period {period_value} s declaring a budget",
            "declaring the task, or its budget",
        )
        for period_value in missing
    ]
    return reasons, target_tasks


def _task_level_reasons(
    component: str,
    level: str,
    tasks: Sequence[Task],
    target_tasks: Mapping[str, str],
    identities: SchedulingIdentities,
    levels: Mapping[str, str],
) -> list[Reason]:
    """*needs a safety decision*: a target task that would mix levels without its argument."""
    rank = asil_rank(level)
    by_task = {task.name: task for task in tasks}
    reasons: list[Reason] = []
    for task_name in dict.fromkeys(target_tasks.values()):
        task = by_task[task_name]
        if task.coexistence:
            continue
        others = {name: other for name, other in _task_levels(task, identities, levels).items() if name != component}
        mixed = sorted(name for name, other in others.items() if asil_rank(other) != rank)
        if mixed:
            named = ", ".join(f"'{name}' (ASIL {others[name]})" for name in mixed)
            reasons.append(
                Reason(
                    CLASS_SAFETY,
                    "rule:safety.task-mixed-asil",
                    f"task '{task.name}' runs {named}, of another level than the component's ASIL {level}, "
                    "and declares no coexistence argument",
                    "the task's coexistence argument -- never proposed here",
                )
            )
    return reasons


def _locked_reasons(component: str, partition_name: str, experiment: AllocationConfig) -> list[Reason]:
    """*locked*: the experiment's own -- ``locked`` and ``allowed_partitions``."""
    reasons: list[Reason] = []
    if component in experiment.constraints.locked:
        reasons.append(
            Reason(CLASS_LOCKED, None, "the component is named in the experiment's locked", "editing the experiment")
        )
    allowed = experiment.constraints.allowed_partitions.get(component)
    if allowed is not None and partition_name not in allowed:
        reasons.append(
            Reason(
                CLASS_LOCKED,
                None,
                "the partition is outside the experiment's allowed_partitions for the component",
                "editing the experiment",
            )
        )
    return reasons


def _target_row(
    swc: SWComponent,
    placement: ComponentPlacement,
    partition: Partition,
    cores: Mapping[str, Core],
    memories: Mapping[str, Memory],
    tasks_in: Mapping[str, list[Task]],
    identities: SchedulingIdentities,
    levels: Mapping[str, str],
    experiment: AllocationConfig,
) -> TargetRow:
    """One other *partition* for the free component *swc*: every reason, one family at a time.

    The reasons are listed in the order the families are asked -- hard, the
    partition's level, removable, the target tasks' levels, locked -- and the
    row's class is the highest of theirs by :data:`CLASS_PRECEDENCE`, not the
    first listed.
    """
    level = placement.level
    budgeted = [memories[name] for name, budget in swc.memory.items() if budget != 0 and name in memories]
    tasks = tasks_in.get(partition.name, [])
    removable, target_tasks = _removable_reasons(placement, partition.name, tasks)
    reasons = [
        *_hard_reasons(level, cores.get(partition.core), budgeted),
        *_partition_level_reasons(level, partition, budgeted),
        *removable,
        *_task_level_reasons(swc.name, level, tasks, target_tasks, identities, levels),
        *_locked_reasons(swc.name, partition.name, experiment),
    ]
    return TargetRow(
        partition=partition.name,
        core=partition.core if partition.core in cores else None,
        reasons=tuple(reasons),
        target_tasks=target_tasks,
        unrated=partition.asil is None,
    )


def _size_bits(name: str | None, data: DataDescription, seen: frozenset[str] = frozenset()) -> tuple[int | None, str]:
    """The logical size in bits of the implementation type *name*, or ``(None, reason)``.

    Five shapes are sized and no other: a base type (reached as a value type's
    ``base``) its ``size``; a value type its base's; a reference type its
    base's; an implementation record the sum of its elements; an
    implementation array ``length x element``.
    """
    if name is None or name in seen:
        return None, UNSIZED_UNDECLARED
    element = data.implementation(name)
    if element is None:
        return None, UNSIZED_UNDECLARED
    seen = seen | {name}
    if isinstance(element, ValueDataType):
        base = data.base(element.base)
        if base is None:
            return None, UNSIZED_UNDECLARED
        assert isinstance(base, BaseDataType)  # the base_types layer holds base types and nothing else
        return base.size, ""
    if isinstance(element, ReferenceDataType):
        return _size_bits(element.base, data, seen)
    if isinstance(element, ImplementationRecordDataType):
        total = 0
        for member in element.elements:
            bits, reason = _size_bits(member.type, data, seen)
            if bits is None:
                return None, reason
            total += bits
        return total, ""
    if isinstance(element, ImplementationArrayDataType):
        bits, reason = _size_bits(element.element_type, data, seen)
        return (None, reason) if bits is None else (element.length * bits, "")
    if isinstance(element, (PointerDataType, ExternalDataType)):
        # The same answer as the fall-through, kept so the two kinds are read explicitly.
        return None, UNSIZED_SHAPE
    return None, UNSIZED_SHAPE


def _element_size(
    element: DataElement, sender: SWComponent, data: DataDescription, mapping_sets: Mapping[str, MappingSet]
) -> tuple[int | None, str]:
    """The data element's logical size in bits, sized through its implementation type."""
    if element.implementation_type is not None:
        return _size_bits(element.implementation_type, data)
    mapped = {
        entry.implementation
        for set_name in sender.data_type_mappings
        if (mapping_set := mapping_sets.get(set_name)) is not None
        for entry in mapping_set.maps
        if entry.application == element.application_type
    }
    if len(mapped) != 1:
        return None, UNSIZED_NO_MAPPING
    return _size_bits(next(iter(mapped)), data)


def _link_period(sender: SWComponent, port: str, element: str) -> tuple[float | None, bool]:
    """The link's period and whether it is estimated: the shortest writer's, else the sender's shortest timing."""
    writers = [
        period
        for runnable in sender.runnables
        if any(entry.port == port and entry.element == element for entry in runnable.writes)
        and (period := _timing_period(runnable)) is not None
    ]
    if writers:
        return min(writers, key=exact), False
    timings = [period for runnable in sender.runnables if (period := _timing_period(runnable)) is not None]
    if timings:
        return min(timings, key=exact), True
    return None, False


def _end_exclusion(placement: ComponentPlacement | None, component: str) -> str | None:
    if placement is None:
        return _end_not_established(component, "the component is not declared")
    if placement.partition is not None:
        return None
    structural = [reason for reason in placement.reasons if not reason.startswith(DATA_GAP_PREFIX)]
    return _end_not_established(component, "; ".join(structural) or "no partition")


@dataclass(frozen=True)
class _Ends:
    """One provider / requirer pairing of a connector's two ends, by name, with what excludes them."""

    connector: str
    provider: str
    provider_port: str
    requirer: str
    requirer_port: str
    #: Why an end's partition is not established: every link and call through these ends is excluded for each.
    exclusions: tuple[str, ...]


def _call_links(ends: _Ends, client: SWComponent) -> list[CallLink]:
    """Every ``calls`` entry of the *client*'s runnables through its port of *ends*: counted, never costed."""
    return [
        CallLink(
            connector=ends.connector,
            client=client.name,
            client_port=ends.requirer_port,
            runnable=runnable.name,
            operation=call.operation,
            server=ends.provider,
            server_port=ends.provider_port,
            exclusions=ends.exclusions,
        )
        for runnable in client.runnables
        for call in runnable.calls
        if call.port == ends.requirer_port
    ]


def _data_links(
    ends: _Ends,
    sender: SWComponent,
    interface: str,
    elements: Sequence[tuple[str, DataElement | None]],
    data: DataDescription,
    mapping_sets: Mapping[str, MappingSet],
) -> list[Link]:
    """One link per data element of *interface*, sized and timed through *sender* (``None``: an external stub's)."""
    links: list[Link] = []
    for element_name, element in elements:
        exclusions = list(ends.exclusions)
        if element is None:
            bits, size_reason = None, UNSIZED_EXTERNAL_INTERFACE
        else:
            bits, size_reason = _element_size(element, sender, data, mapping_sets)
        if bits is None:
            exclusions.append(size_reason)
        period, estimated = _link_period(sender, ends.provider_port, element_name)
        if period is None:
            exclusions.append(FREQUENCY_NOT_ESTABLISHED)
        links.append(
            Link(
                connector=ends.connector,
                sender=ends.provider,
                sender_port=ends.provider_port,
                receiver=ends.requirer,
                receiver_port=ends.requirer_port,
                interface=interface,
                element=element_name,
                size_bits=bits,
                period=period,
                estimated=estimated,
                exclusions=tuple(exclusions),
            )
        )
    return links


def _links(
    connectors: Sequence[Connector],
    swc_map: Mapping[str, SWComponent],
    composition_map: Mapping[str, Composition],
    interfaces: Mapping[str, Interface],
    data: DataDescription,
    mapping_sets: Mapping[str, MappingSet],
    placements: Mapping[str, ComponentPlacement],
) -> tuple[list[Link], list[CallLink]]:
    links: list[Link] = []
    calls: list[CallLink] = []
    for connector in sorted(connectors, key=lambda c: c.name):
        for left, left_port in component_ends(connector.from_, swc_map, composition_map):
            for right, right_port in component_ends(connector.to, swc_map, composition_map):
                if left_port.direction == "provide" and right_port.direction == "require":
                    provider, provider_port, requirer, requirer_port = left, left_port, right, right_port
                elif left_port.direction == "require" and right_port.direction == "provide":
                    provider, provider_port, requirer, requirer_port = right, right_port, left, left_port
                else:
                    continue  # two ends of one direction: the connector rules' finding
                if provider.name == requirer.name:
                    continue  # a link within one component never crosses and costs nothing
                interface = interfaces.get(provider_port.interface or "")
                ends = _Ends(
                    connector=connector.name,
                    provider=provider.name,
                    provider_port=provider_port.name,
                    requirer=requirer.name,
                    requirer_port=requirer_port.name,
                    exclusions=tuple(
                        exclusion
                        for component in (provider.name, requirer.name)
                        if (exclusion := _end_exclusion(placements.get(component), component)) is not None
                    ),
                )
                if isinstance(interface, CSInterface):
                    calls.extend(_call_links(ends, requirer))
                elif isinstance(interface, SRInterface):
                    elements = [(element.name, element) for element in interface.data_elements]
                    links.extend(_data_links(ends, provider, interface.name, elements, data, mapping_sets))
                elif isinstance(interface, ExternalInterface):
                    external = [(name, None) for name in interface.data_elements]
                    links.extend(_data_links(ends, provider, interface.name, external, data, mapping_sets))
    return links, calls


def project(state: ModelState, analysis: ResourceAnalysis, experiment: AllocationConfig) -> Projection:
    """The placement, the freedom table, the link graph and the current state of the model *state* holds.

    *analysis* is ``analyze_resource_budgets`` of the same model (the loads are
    its figures, never recomputed here); *experiment* supplies ``locked`` and
    ``allowed_partitions``, the table's *locked* class. Everything is built from
    sorted inputs -- components, partitions, tasks and connectors by name -- so
    identical inputs give an identical projection.
    """
    swcs = list(state["swc"])
    swc_map = _first_by_name(swcs)
    tasks = sorted(state["tasks"], key=lambda t: t.name)
    hardware = state["hardware"]
    first_hardware = _first_by_name(hardware)
    partitions: dict[str, Partition] = {
        name: element for name, element in sorted(first_hardware.items()) if isinstance(element, Partition)
    }
    cores: dict[str, Core] = {name: element for name, element in first_hardware.items() if isinstance(element, Core)}
    memories: dict[str, Memory] = {
        name: element for name, element in first_hardware.items() if isinstance(element, Memory)
    }
    compositions: list[Composition] = list(state["compositions"])
    identities = identities_from(compositions)
    levels = effective_asil_levels(swcs, state["requirements"])
    placements = _placements(swcs, tasks, partitions, cores, identities, levels)

    tasks_in: dict[str, list[Task]] = {}
    for task in tasks:
        tasks_in.setdefault(task.partition, []).append(task)

    table: list[FreedomRow] = []
    gaps: list[str] = []
    for name, placement in placements.items():
        targets: list[TargetRow] = []
        if placement.free:
            for partition in partitions.values():
                if partition.name == placement.partition:
                    continue
                targets.append(
                    _target_row(
                        swc_map[name], placement, partition, cores, memories, tasks_in, identities, levels, experiment
                    )
                )
        table.append(FreedomRow(placement, tuple(targets)))
        for reason in placement.reasons:
            gaps.append(f"component '{name}' is not free: {reason}")
        gaps.extend(placement.assumptions)
        for row in targets:
            if row.admissible and row.unrated:
                gaps.append(f"moving '{name}' into partition '{row.partition}' is admissible: an unrated partition")

    data = DataDescription([*state["application_types"], *state["implementation_types"], *state["base_types"]])
    mapping_sets: dict[str, MappingSet] = _first_by_name(state["mapping_sets"])
    interfaces: dict[str, Interface] = _first_by_name(state["interfaces"])
    composition_map = _first_by_name(compositions)
    links, calls = _links(state["connectors"], swc_map, composition_map, interfaces, data, mapping_sets, placements)
    for link in links:
        label = f"link '{link.sender}.{link.sender_port}' -> '{link.receiver}.{link.receiver_port}' ({link.element})"
        for exclusion in link.exclusions:
            gaps.append(f"{label} is excluded: {exclusion}")
        if link.costed and link.estimated:
            gaps.append(
                f"{label}: frequency estimated (no runnable of the sender with a timing event writes the element)"
            )
    for call in calls:
        for exclusion in call.exclusions:
            gaps.append(
                f"call '{call.client}.{call.runnable}' -> '{call.server}' ({call.operation}) "
                f"is not counted: {exclusion}"
            )

    return Projection(
        partitions={name: (p.core if p.core in cores else None) for name, p in partitions.items()},
        table=tuple(table),
        links=tuple(links),
        calls=tuple(calls),
        loads=_loads_of(analysis),
        data_gaps=tuple(gaps),
        identities=identities,
    )


def read_projection(
    model_root: str | Path, experiment: AllocationConfig, state: ModelState | None = None
) -> Projection:
    """:func:`project` over the model under *model_root*, its names checked first (:func:`check_config_names`).

    *state* is the model already loaded from *model_root*, when the caller has it.
    """
    if state is None:
        state = load_model_state(model_root)
    check_config_names(experiment, state)
    return project(state, analyze_resource_budgets(str(model_root)), experiment)


# ---------------------------------------------------------------------------
# The search
# ---------------------------------------------------------------------------

#: The four curve statuses -- the only ones a point carries.
STATUS_OPTIMAL = "optimal"
STATUS_FEASIBLE = "feasible"
STATUS_UNKNOWN = "unknown"
STATUS_INFEASIBLE = "infeasible"
CURVE_STATUSES: tuple[str, ...] = (STATUS_OPTIMAL, STATUS_FEASIBLE, STATUS_UNKNOWN, STATUS_INFEASIBLE)

#: How a candidate enumeration ended -- never a curve status.
ENUMERATION_AT_K = "at K"
ENUMERATION_EXHAUSTED = "exhausted: no further assignment within the tolerance"
ENUMERATION_CUT_SHORT = "cut short by the time limit"

#: Why a checked move set was dropped.
ZERO_RULE_REASON = "the source budget is kept (zero rule)"
TABLE_MISSED_REASON = "a rule the freedom table did not foresee"
#: An implied task budget whose exact finest-unit text the model could not load: the candidate is
#: refused and listed, never the experiment, and the budget is never rounded to fit.
BUDGET_CAP_REASON = f"an implied task budget exceeds {MAX_FACTOR_DIGITS} digits of its finest unit"

NOT_REPRESENTABLE = "the experiment is not representable in the solver's integers"

#: Every coefficient, right-hand side and absolute activity sum stays at or below this: the
#: bound up to which every integer is exactly a binary64 float, the number HiGHS computes in,
#: so no integer the model states is rounded on its way to the solver.
INTEGER_BOUND = 2**53

#: How far a solver value may lie from 0 or 1 and still be read as that integer (HiGHS's
#: default MIP feasibility tolerance). A value farther away is an execution failure, never rounded.
INTEGRALITY_TOLERANCE = 1e-6

#: The solver's one fixed setting besides its log (off) and the per-solve ``time_limit``: a
#: proved optimum at zero relative gap. ``milp`` forwards no thread count and no seed, so
#: HiGHS's own defaults apply to both.
SOLVER_MIP_REL_GAP = 0.0

#: What ``milp``'s status-2 message starts with when HiGHS proved the model infeasible. The
#: same status also carries HiGHS's *model error*, which is an execution failure, never a status.
INFEASIBLE_MESSAGE = "The problem is infeasible."

#: The requirement the missing-solver refusal names.
SOLVER_DISTRIBUTION = "scipy>=1.17.1"


#: The clock every deadline reads; a test replaces it rather than waiting.
_monotonic = time.monotonic


@dataclass(frozen=True)
class Move:
    """One component moved to another partition, each runnable it carries into a task of its period there."""

    component: str
    source: str
    target: str
    #: ``(runnable, source task, target task)`` for each runnable it carries (periodic only).
    runnables: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class TaskEdit:
    """The ``wcet`` edit a move set takes on one touched task, aggregated over every move of the set.

    ``original - outgoing + incoming`` is the new budget, computed exactly in
    seconds. When it is exactly zero the budget is **kept** (the zero rule:
    ``wcet: 0us`` is refused at load and an omitted one is a missing budget on
    a ``strict`` core), and the statement is the budget the moves free. Every
    figure is written in :attr:`unit`, the finest among the task's own unit and
    the unit of every runnable budget moving into or out of it, which holds the
    sum exactly because each term is a whole number of its own, coarser or
    equal, unit. ``original`` stays the
    task's text as written, and so does :attr:`written` whenever the value does
    not change.
    """

    task: str
    partition: str
    #: The task's ``wcet`` as written.
    original: str
    #: The moved budgets, exact seconds.
    outgoing: Fraction
    incoming: Fraction
    #: The finest unit among the task's and the moved runnables' budgets.
    unit: str

    @functools.cached_property
    def original_seconds(self) -> Fraction:
        """:attr:`original` parsed once, exact seconds."""
        return parse_budget(self.original).seconds

    @functools.cached_property
    def computed(self) -> Fraction:
        """The new budget, exact seconds."""
        return self.original_seconds - self.outgoing + self.incoming

    @property
    def kept_by_zero_rule(self) -> bool:
        """The moves would leave the task no budget at all, so it keeps its original one."""
        return self.computed == 0

    @property
    def unchanged(self) -> bool:
        """The moves in and out cancel."""
        return self.computed == self.original_seconds

    @property
    def written(self) -> str:
        """The ``wcet`` the applied copy carries: the original text unless the value changes."""
        if self.unchanged or self.kept_by_zero_rule:
            return self.original
        return format_exact(self.computed, self.unit)

    @property
    def outgoing_text(self) -> str:
        """The budget the moves take out of the task, exact, in :attr:`unit`."""
        return format_exact(self.outgoing, self.unit)

    @property
    def incoming_text(self) -> str:
        """The budget the moves bring into the task, exact, in :attr:`unit`."""
        return format_exact(self.incoming, self.unit)

    @property
    def frees(self) -> str:
        """The budget a zero-rule task keeps with no declared runnable budget left in it, in :attr:`unit`."""
        return format_exact(self.original_seconds if self.kept_by_zero_rule else Fraction(0), self.unit)

    @property
    def over_cap(self) -> bool:
        """Whether :attr:`written` has more than :data:`MAX_FACTOR_DIGITS` digits, which the copy would refuse.

        Decided on the integer count of :attr:`unit`, never on the formatted text; an unchanged or
        zero-rule edit writes the original, which the model loaded, so it is never over the cap.
        """
        if self.unchanged or self.kept_by_zero_rule:
            return False
        return abs(self.computed / SECONDS_PER_UNIT[self.unit]) >= 10**MAX_FACTOR_DIGITS


class BudgetCapExceeded(Exception):
    """A move set whose implied budgets include one the model could not load (over the factor cap)."""

    def __init__(self, edits: tuple[TaskEdit, ...]) -> None:
        super().__init__("an implied task budget exceeds the factor cap")
        #: The edits over the cap, and only those.
        self.edits = edits


@dataclass(frozen=True)
class RefusedSet:
    """A move set the per-candidate ``check-model`` run refused: dropped, cut by a no-good and listed."""

    moves: tuple[Move, ...]
    #: The rule ids the copy was refused under, ``rule:``-prefixed, sorted.
    rules: tuple[str, ...]
    #: :data:`ZERO_RULE_REASON` when every error finding is a threshold breach the zero rule caused,
    #: :data:`TABLE_MISSED_REASON` otherwise -- or :data:`BUDGET_CAP_REASON` when an implied task
    #: budget is over the factor cap: no copy is written, so no ``check-model`` runs, :attr:`rules`
    #: is empty and :attr:`messages` names each task over the cap.
    reason: str
    messages: tuple[str, ...]


@dataclass(frozen=True)
class Solution:
    """A move set that passed ``check-model`` on its scratch copy, with every figure the report states."""

    moves: tuple[Move, ...]
    #: Every search component's partition, the unmoved ones included.
    assignment: Mapping[str, str]
    #: ``C0 + variable_sum`` in bytes x Hz, exact -- never the solver's scaled integer.
    objective: Fraction
    edits: tuple[TaskEdit, ...]
    #: ``analyze_resource_budgets`` of the scratch copy (the project's own for the empty move set),
    #: never the solver's internal figures.
    loads: tuple[CoreLoad, ...]
    #: The cores a move leaves or enters, by name.
    touched_cores: tuple[str, ...]
    #: ``advisory`` cores over their threshold on the copy: reported, never constrained.
    advisory_breaches: tuple[str, ...]
    #: Costed links that become inter-partition, and those that cease to be.
    links_split: tuple[Link, ...]
    links_joined: tuple[Link, ...]
    #: Counted calls whose two ends run in different partitions after the moves.
    calls: int


@dataclass(frozen=True)
class CurvePoint:
    """One point of the curve: the best checked move set of at most ``n`` moves, and how sure the search is."""

    n: int
    status: str
    #: The checked move set; ``None`` at an ``unknown`` or ``infeasible`` point.
    solution: Solution | None
    #: A time limit cut the search short: the status and the move set may differ on another run.
    cut_short: bool
    refused: tuple[RefusedSet, ...]
    #: Timing -- the one kind of field two runs of a completed search may differ in.
    seconds: float


@dataclass(frozen=True)
class Candidate:
    solution: Solution
    #: Within ``tolerance`` of the proved optimum; ``False`` at a ``feasible`` point, where no optimum is proved.
    within_tolerance: bool


@dataclass(frozen=True)
class Curve:
    """The search's answer: the curve over ``N = 1 ... last_solved`` and the candidates at ``candidates_at``.

    ``points`` ends at ``last_solved = min(max_moves, max(1, |F|))``: a point above
    it would be the last one again, so none is built (:func:`solve_curve`).
    """

    #: ``C0``: the cost of the costed links whose crossing no assignment the domains allow changes, that cross.
    constant: Fraction
    #: ``N = 0``, the current state's objective (``C0`` included), computed directly.
    current: Fraction
    points: tuple[CurvePoint, ...]
    #: The ``max_moves`` the experiment asked for; ``points`` stops at :attr:`last_solved`, which may be below it.
    max_moves: int
    #: The curve point the candidates were enumerated at: the requested ``candidates_at``, or
    #: :attr:`last_solved` when that is lower -- always a point of ``points``.
    candidates_at: int
    candidates: tuple[Candidate, ...]
    #: How the enumeration ended; ``None`` where none ran (a point that is not ``optimal``, or whose volume is
    #: not below the current one, so that it moves nothing).
    enumeration: str | None
    #: The tolerance bound's scaled right-hand side, when one was added: ``floor(s x (V* x (1 + tolerance) - C0))``
    #: capped at ``s x (current - C0) - 1``, strictly below the current volume.
    tolerance_bound: int | None
    #: The move sets the enumeration dropped.
    refused: tuple[RefusedSet, ...]
    solver_version: str
    #: Timing.
    seconds: float

    @property
    def last_solved(self) -> int:
        """The last ``N`` solved; every ``N`` above it up to ``max_moves`` has the same answer."""
        return len(self.points)

    def point(self, n: int) -> CurvePoint:
        """The point at *n*, which is at most :attr:`last_solved`."""
        if not 1 <= n <= self.last_solved:
            raise IndexError(f"N = {n} is not a solved point of this curve (1 ... {self.last_solved})")
        return self.points[n - 1]


# -- the formulation ---------------------------------------------------------


@dataclass(frozen=True)
class LinkSets:
    """The costed links in two disjoint sets, decided by the domains alone, with the components' domains."""

    #: Component -> its partitions (its current one first), for every component whose domain holds more than one.
    domains: Mapping[str, tuple[str, ...]]
    #: Component -> its constant partition, for every other component whose partition is established.
    fixed: Mapping[str, str]
    constant: tuple[Link, ...]
    variable: tuple[Link, ...]

    def partition_of(self, component: str, assignment: Mapping[str, str]) -> str:
        """*component*'s partition under *assignment* (its current one where the assignment is silent)."""
        if component in self.domains:
            return assignment.get(component, self.domains[component][0])
        return self.fixed[component]

    @property
    def c0(self) -> Fraction:
        """The constant term: the cost of the CONSTANT links that cross, each counted once.

        A CONSTANT link with a free end always crosses (its other end's partition
        is outside the free end's domain, or the two domains are disjoint); a
        fixed / fixed one crosses when its two partitions differ.
        """
        return sum(
            (
                link.cost
                for link in self.constant
                if link.sender in self.domains
                or link.receiver in self.domains
                or self.fixed[link.sender] != self.fixed[link.receiver]
            ),
            Fraction(0),
        )

    def variable_sum(self, assignment: Mapping[str, str]) -> Fraction:
        """``sum cost x cross`` over the VARIABLE links for *assignment*, exact."""
        return sum(
            (
                link.cost
                for link in self.variable
                if self.partition_of(link.sender, assignment) != self.partition_of(link.receiver, assignment)
            ),
            Fraction(0),
        )

    def objective(self, assignment: Mapping[str, str]) -> Fraction:
        """``C0 + variable_sum``: every objective the report states."""
        return self.c0 + self.variable_sum(assignment)


def link_sets(projection: Projection) -> LinkSets:
    """Put every costed link in exactly one of the CONSTANT and VARIABLE sets.

    A component is a variable of the search when it is free and its domain
    holds more than its current partition; every other component whose
    partition is established is fixed there. A link is CONSTANT when its
    crossing takes the same value under every assignment the domains allow --
    fixed / fixed; free / fixed with the fixed partition outside the free end's
    domain (it always crosses); free / free with disjoint domains (it always
    crosses) -- and VARIABLE otherwise. The solver sees the VARIABLE links only.
    """
    domains: dict[str, tuple[str, ...]] = {}
    fixed: dict[str, str] = {}
    for row in projection.table:
        placement = row.placement
        if placement.partition is None:
            continue
        if len(row.domain) > 1:
            domains[placement.component] = row.domain
        else:
            fixed[placement.component] = placement.partition
    constant: list[Link] = []
    variable: list[Link] = []
    for link in projection.links:
        if not link.costed:
            continue
        sender, receiver = link.sender, link.receiver
        if sender not in domains and receiver not in domains:
            constant.append(link)
        elif sender in domains and receiver in domains:
            (variable if set(domains[sender]) & set(domains[receiver]) else constant).append(link)
        else:
            free, other = (sender, receiver) if sender in domains else (receiver, sender)
            (variable if fixed[other] in domains[free] else constant).append(link)
    return LinkSets(domains=domains, fixed=fixed, constant=tuple(constant), variable=tuple(variable))


def _scaled(values: Sequence[Fraction]) -> tuple[list[int], Fraction]:
    """*values* as integers -- times the lcm of their denominators, over the gcd of the result -- and that factor."""
    if not values:
        return [], Fraction(1)
    multiple = math.lcm(*(value.denominator for value in values))
    integers = [int(value * multiple) for value in values]
    divisor = math.gcd(*integers) or 1
    return [integer // divisor for integer in integers], Fraction(multiple, divisor)


def _check_range(integers: Sequence[int], what: str) -> None:
    """Refuse *what* when a coefficient, its right-hand side or its absolute activity sum exceeds the bound.

    The bound is :data:`INTEGER_BOUND`, ``2**53``: every integer up to it is exactly a binary64 float.
    """
    if any(abs(integer) > INTEGER_BOUND for integer in integers) or sum(abs(i) for i in integers) > INTEGER_BOUND:
        raise NotRepresentableError(
            f"{NOT_REPRESENTABLE}: the {what} needs a coefficient, a right-hand side or an activity bound "
            "beyond 2**53 once its exact rationals are scaled to integers"
        )


@dataclass(frozen=True)
class _Row:
    """One linear constraint ``at_least <= sum coefficient x variable <= at_most`` in integers; ``None``: unbounded."""

    terms: tuple[tuple[int, int], ...]
    at_least: int | None
    at_most: int | None

    def holds(self, values: Sequence[int]) -> bool:
        activity = sum(coefficient * values[index] for index, coefficient in self.terms)
        return (self.at_least is None or activity >= self.at_least) and (
            self.at_most is None or activity <= self.at_most
        )


class SearchModel:
    """The 0/1 linear model of one curve point, built from inputs sorted by name, an exact no-good per *no_goods* entry.

    The model is held in exact integers and handed to ``scipy.optimize.milp`` (HiGHS) by
    :meth:`solve`. *solver* is the ``scipy`` module, with ``scipy.optimize`` and
    ``scipy.sparse`` imported.

    **The objective is lexicographic, in one solve.** It minimises
    ``s x variable_sum x (|F| + 1) + sum moved``, where ``sum moved`` counts the
    search components away from their current partition: the volume first and,
    among assignments of equal volume, the one that moves fewest. ``sum moved``
    never exceeds ``|F|``, so no number of moves outweighs one step of the
    scaled volume, and a move that changes no volume is never part of a point's
    answer. The tie-break does not change what a point answers, the best volume
    with at most ``n`` moves.

    Raises:
        NotRepresentableError: a constraint or the objective does not fit the solver's integers.
    """

    def __init__(
        self, solver: Any, projection: Projection, sets: LinkSets, n: int, no_goods: Sequence[Mapping[str, str]] = ()
    ) -> None:
        self.solver = solver
        self.n = n
        self.sets = sets
        #: The variables' names by index, ``x[component,partition]`` and ``same[link,partition]``.
        self.names: list[str] = []
        self.rows: list[_Row] = []
        self.components: tuple[str, ...] = tuple(sorted(sets.domains))
        self.current: dict[str, str] = {name: sets.domains[name][0] for name in self.components}
        self.x: dict[tuple[str, str], int] = {}
        for name in self.components:
            for partition in sorted(sets.domains[name]):
                self.x[(name, partition)] = self._variable(f"x[{name},{partition}]")
            self._add({self.x[(name, p)]: 1 for p in sorted(sets.domains[name])}, 1, 1, "placement")
        #: The crossing of each VARIABLE link, in ``sets.variable``'s order, as ``1 + sum(sign x variable)``.
        self.cross: list[list[tuple[int, int]]] = [
            self._crossing(index, link) for index, link in enumerate(sets.variable)
        ]
        self._objective()
        self._core_constraints(projection)
        if self.components:
            # sum moved <= n, i.e. sum x[c, current(c)] >= |F| - n.
            self._add(
                {self.x[(name, self.current[name])]: 1 for name in self.components},
                len(self.components) - n,
                None,
                "move budget",
            )
        for assignment in no_goods:
            self.add_no_good(assignment)

    def _variable(self, name: str) -> int:
        self.names.append(name)
        return len(self.names) - 1

    def _add(self, terms: Mapping[int, int], at_least: int | None, at_most: int | None, what: str) -> None:
        bounds = [bound for bound in (at_least, at_most) if bound is not None]
        _check_range([*terms.values(), *bounds], what)
        self.rows.append(_Row(tuple(sorted(terms.items())), at_least, at_most))

    def _crossing(self, index: int, link: Link) -> list[tuple[int, int]]:
        domains, fixed = self.sets.domains, self.sets.fixed
        sender, receiver = link.sender, link.receiver
        if sender in domains and receiver in domains:
            # free / free: same[l, p] <=> x[c, p] and x[d, p], so cross = 1 - sum_p same[l, p].
            terms: list[tuple[int, int]] = []
            for partition in sorted(set(domains[sender]) & set(domains[receiver])):
                same = self._variable(f"same[{index},{partition}]")
                left, right = self.x[(sender, partition)], self.x[(receiver, partition)]
                self._add({same: 1, left: -1}, None, 0, "crossing")  # same <= left
                self._add({same: 1, right: -1}, None, 0, "crossing")  # same <= right
                self._add({same: 1, left: -1, right: -1}, -1, None, "crossing")  # same >= left + right - 1
                terms.append((same, -1))
            return terms
        # free / fixed: cross = 1 - x[c, q].
        free, other = (sender, receiver) if sender in domains else (receiver, sender)
        return [(self.x[(free, fixed[other])], -1)]

    def cross_value(self, index: int, values: Sequence[int]) -> int:
        """The VARIABLE link *index*'s crossing under the 0/1 *values* (1 when the ends' partitions differ)."""
        return 1 + sum(values[variable] * sign for variable, sign in self.cross[index])

    def _objective(self) -> None:
        constant = Fraction(0)
        coefficients: dict[int, Fraction] = {}
        for terms, link in zip(self.cross, self.sets.variable, strict=True):
            constant += link.cost
            for variable, sign in terms:
                coefficients[variable] = coefficients.get(variable, Fraction(0)) + link.cost * sign
        keys = list(coefficients)
        integers, scale = _scaled([*(coefficients[key] for key in keys), constant])
        weight = len(self.components) + 1
        # The volume's integers times |F| + 1, then sum moved: |F| unit coefficients and the constant |F|.
        _check_range(
            [*(integer * weight for integer in integers), *[1] * len(self.components), len(self.components)],
            "objective",
        )
        #: The objective's scale ``s``: ``s x variable_sum`` has integer coefficients, an integer for every assignment.
        self.objective_scale = scale
        #: The volume part's terms, ``(variable, integer)``: what ``s x variable_sum`` sums besides its constant.
        self.objective_terms: list[tuple[int, int]] = [
            (key, integer) for key, integer in zip(keys, integers[:-1], strict=True) if integer != 0
        ]
        #: ``s x variable_sum``'s constant term; the tolerance bound moves it right.
        self.volume_constant = integers[-1]
        #: The objective the solver minimises, by variable index, its constant dropped:
        #: ``(|F| + 1) x s x variable_sum - sum_c x[c, current(c)]``.
        self.objective: dict[int, int] = {}
        for variable, integer in self.objective_terms:
            self.objective[variable] = self.objective.get(variable, 0) + integer * weight
        for name in self.components:
            key = self.x[(name, self.current[name])]
            self.objective[key] = self.objective.get(key, 0) - 1

    def _core_constraints(self, projection: Projection) -> None:
        shares = {row.placement.component: row.placement.share for row in projection.table}
        #: The ``strict`` cores a constraint was built for, by name.
        self.constrained_cores: list[str] = []
        for load in sorted(projection.loads, key=lambda each: each.core):
            if load.enforcement != ENFORCEMENT_STRICT:
                continue  # an advisory core's breach is reported per point and per candidate, never constrained
            on_core = [
                (name, partition)
                for name in self.components
                for partition in sorted(self.sets.domains[name])
                if projection.partitions.get(partition) == load.core
            ]
            if not on_core:
                continue
            base = load.load - sum(
                (
                    shares[name]
                    for name in self.components
                    if projection.partitions.get(self.current[name]) == load.core
                ),
                Fraction(0),
            )
            integers, _scale = _scaled([*(shares[name] for name, _partition in on_core), load.threshold - base])
            _check_range(integers, f"constraint of core '{load.core}'")
            terms: dict[int, int] = {}
            for key, integer in zip(on_core, integers[:-1], strict=True):
                terms[self.x[key]] = terms.get(self.x[key], 0) + integer
            self._add(terms, None, integers[-1], f"constraint of core '{load.core}'")
            self.constrained_cores.append(load.core)

    def add_no_good(self, assignment: Mapping[str, str]) -> None:
        """Exclude exactly *assignment*: ``sum_c x[c, a(c)] <= |F| - 1`` over every search component."""
        if self.components:
            self._add(
                {self.x[(name, assignment[name])]: 1 for name in self.components},
                None,
                len(self.components) - 1,
                "no-good",
            )

    def add_superset_no_good(self, assignment: Mapping[str, str]) -> None:
        """Exclude *assignment*'s move set and every superset: ``sum_{c moved} x[c, a(c)] <= |moved| - 1``.

        Added only for an accepted candidate (a refused set keeps the exact
        no-good: a superset of a refused set may pass the check). The argument is
        :func:`_enumerate`'s.
        """
        moved = [name for name in self.components if assignment[name] != self.current[name]]
        if moved:
            self._add({self.x[(name, assignment[name])]: 1 for name in moved}, None, len(moved) - 1, "no-good")

    def add_tolerance_bound(self, optimum: Fraction, tolerance: Fraction, current: Fraction) -> int:
        """``s x variable_sum <= min(floor(s x (V* x (1 + tolerance) - C0)), s x (current - C0) - 1)``.

        On the volume part alone: ``C0`` is moved right before scaling, and the
        move count, the objective's tie-break, takes no part in the bound. The
        second term caps the bound strictly below the *current* volume, whatever
        the tolerance: a move set that does not improve on the current state is
        no answer, so it is never a candidate. ``s x (current - C0)`` is the
        volume expression at the current assignment, an integer.
        """
        bound = min(
            math.floor(self.objective_scale * (optimum * (1 + tolerance) - self.sets.c0)),
            math.ceil(self.objective_scale * (current - self.sets.c0)) - 1,
        )
        _check_range([bound], "tolerance bound")
        if self.objective_terms:
            self._add(dict(self.objective_terms), None, bound - self.volume_constant, "tolerance bound")
        return bound

    def solve(self, time_limit: float) -> Any:
        """One ``scipy.optimize.milp`` run of the model within *time_limit* seconds: its ``OptimizeResult``, unread."""
        optimize, sparse = self.solver.optimize, self.solver.sparse
        width = len(self.names)
        objective = [0] * width
        for variable, coefficient in self.objective.items():
            objective[variable] = coefficient
        rows: list[int] = []
        columns: list[int] = []
        data: list[float] = []
        floors: list[float] = []
        ceilings: list[float] = []
        for number, row in enumerate(self.rows):
            for variable, coefficient in row.terms:
                rows.append(number)
                columns.append(variable)
                data.append(float(coefficient))
            floors.append(-math.inf if row.at_least is None else float(row.at_least))
            ceilings.append(math.inf if row.at_most is None else float(row.at_most))
        matrix = sparse.csr_array((data, (rows, columns)), shape=(len(self.rows), width))
        return optimize.milp(
            [float(coefficient) for coefficient in objective],
            integrality=[1] * width,
            bounds=optimize.Bounds(0, 1),
            constraints=optimize.LinearConstraint(matrix, floors, ceilings),
            options={"mip_rel_gap": SOLVER_MIP_REL_GAP, "disp": False, "time_limit": time_limit},
        )

    def exact_values(self, x: Any) -> list[int] | None:
        """*x* as exact 0/1 integers, or ``None`` when it is no such assignment of this model.

        Every value must be finite and within :data:`INTEGRALITY_TOLERANCE` of 0 or 1; the
        rounded values must then satisfy every row exactly, in integers -- the solver's own
        feasibility is toleranced, this check is not.
        """
        if x is None or len(x) != len(self.names):
            return None
        values: list[int] = []
        for value in (float(each) for each in x):
            if not math.isfinite(value):
                return None
            rounded = round(value)
            if rounded not in (0, 1) or abs(value - rounded) > INTEGRALITY_TOLERANCE:
                return None
            values.append(rounded)
        return values if all(row.holds(values) for row in self.rows) else None


def _run_solver(search: SearchModel, max_time_in_seconds: float) -> tuple[str, dict[str, str] | None]:
    """One solve of *search* within *max_time_in_seconds*: the status's name and, with a solution, its assignment.

    The names are ``OPTIMAL``, ``FEASIBLE`` (the limit cut the solve after a solution),
    ``UNKNOWN`` (before one) and ``INFEASIBLE``; any other name describes an execution
    failure, which the caller raises. ``milp``'s status 0 is ``OPTIMAL``, 1 (a time or
    iteration limit) ``FEASIBLE`` with a solution and ``UNKNOWN`` without, 2 ``INFEASIBLE``
    only when its message says HiGHS proved it; every solution is read through
    :meth:`SearchModel.exact_values` first, and one it refuses is a failure.
    """
    if not search.names:
        return "OPTIMAL", {}  # nothing to decide: the empty assignment is the one answer
    result = search.solve(max_time_in_seconds)
    status = int(result.status)
    if status == 2:
        if str(result.message).startswith(INFEASIBLE_MESSAGE):
            return "INFEASIBLE", None
        return f"status 2 without a proof of infeasibility ({result.message})", None
    if status not in (0, 1):
        return f"status {status} ({result.message})", None
    if result.x is None:
        return ("UNKNOWN", None) if status == 1 else ("status 0 without a solution", None)
    values = search.exact_values(result.x)
    if values is None:
        return "a solution that is not an exact 0/1 assignment satisfying every constraint", None
    assignment = {key[0]: key[1] for key, variable in search.x.items() if values[variable] == 1}
    return ("OPTIMAL" if status == 0 else "FEASIBLE"), assignment


# -- the per-candidate check -------------------------------------------------


def moves_of(projection: Projection, sets: LinkSets, assignment: Mapping[str, str]) -> tuple[Move, ...]:
    """The moves *assignment* makes, by component name, each periodic runnable a move carries into its target task."""
    moves: list[Move] = []
    for name in sorted(sets.domains):
        source = sets.domains[name][0]
        target = assignment.get(name, source)
        if target == source:
            continue
        row = projection.row(name)
        tasks = row.target(target).target_tasks
        runnables = tuple((each.runnable, each.task, tasks[each.runnable]) for each in row.placement.runnables)
        moves.append(Move(component=name, source=source, target=target, runnables=runnables))
    return tuple(moves)


def write_move_set(
    model_root: str | Path, copy_root: Path, projection: Projection, moves: Sequence[Move]
) -> tuple[TaskEdit, ...]:
    """Copy the model under *model_root* to *copy_root* and apply *moves* there: the model the architect would get.

    The moved entries leave the source task's ``runnables`` and join the target
    task's in the form the source entry used; the implied ``wcet`` edits are
    aggregated per touched task (``original - outgoing + incoming``, exact, in
    the finest unit involved; a final zero kept unchanged). Each touched task
    file is parsed and dumped with the writers' settings -- never through
    ``writer`` -- and every other file of the copy is byte-identical to its
    source.

    Every edit is computed before anything is written: when an implied budget's
    text has more than :data:`MAX_FACTOR_DIGITS` digits -- which the copy would
    refuse at load -- nothing is copied or written and :class:`BudgetCapExceeded`
    carries the edits over the cap, so the caller refuses the candidate rather than the run.
    """
    identities = projection.identities
    files = stored_element_files(Path(model_root), "tasks", registry=current_registry())
    documents: dict[str, dict[str, Any]] = {}

    def document(task: str) -> dict[str, Any]:
        if task not in documents:
            parsed = load_yaml_document(files[task][0].path.read_text(encoding="utf-8"))
            # The copy is of a model the reader loaded and check-model passed: every task file is a mapping.
            assert isinstance(parsed, dict)
            documents[task] = parsed
        return documents[task]

    costs = {
        (row.placement.component, each.runnable): each.budget
        for row in projection.table
        for each in row.placement.runnables
    }
    outgoing: dict[str, Fraction] = {}
    incoming: dict[str, Fraction] = {}
    units: dict[str, set[str]] = {}
    for move in moves:
        for runnable_name, source_task, target_task in move.runnables:
            source_document, target_document = document(source_task), document(target_task)
            kept: list[Any] = []
            moved: list[Any] = []
            for entry in source_document.get("runnables") or []:
                matches = (
                    isinstance(entry, dict)
                    and entry.get("runnable") == runnable_name
                    and identities.type_of(entry.get("swc"), entry.get("instance")) == move.component
                )
                (moved if matches else kept).append(entry)
            source_document["runnables"] = kept
            target_document["runnables"] = [*(target_document.get("runnables") or []), *moved]
            cost = costs[(move.component, runnable_name)]
            seconds = Fraction(0) if cost is None else cost.seconds
            outgoing[source_task] = outgoing.get(source_task, Fraction(0)) + seconds
            incoming[target_task] = incoming.get(target_task, Fraction(0)) + seconds
            if cost is not None:
                units.setdefault(source_task, set()).add(cost.unit)
                units.setdefault(target_task, set()).add(cost.unit)
    edits: list[TaskEdit] = []
    for task in sorted(documents):
        task_document = documents[task]
        original = str(task_document["wcet"])
        edits.append(
            TaskEdit(
                task=task,
                partition=str(task_document["partition"]),
                original=original,
                outgoing=outgoing.get(task, Fraction(0)),
                incoming=incoming.get(task, Fraction(0)),
                unit=finest({parse_budget(original).unit, *units.get(task, set())}),
            )
        )
    over_cap = tuple(edit for edit in edits if edit.over_cap)
    if over_cap:
        raise BudgetCapExceeded(over_cap)
    shutil.copytree(model_root, copy_root)
    copied = stored_element_files(copy_root, "tasks", registry=current_registry())
    for edit in edits:
        task_document = documents[edit.task]
        task_document["wcet"] = edit.written
        copied[edit.task][0].path.write_text(
            yaml.safe_dump(task_document, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
        )
    return tuple(edits)


def _loads_of(analysis: ResourceAnalysis) -> tuple[CoreLoad, ...]:
    return tuple(
        CoreLoad(usage.name, usage.resource.enforcement, usage.consumption, usage.limit)
        for usage in sorted(analysis.resources, key=lambda u: u.name)
        if is_core(usage.resource)
    )


def _crossing_links(projection: Projection, sets: LinkSets, assignment: Mapping[str, str]) -> set[int]:
    """The indices of the costed links whose ends run in different partitions under *assignment*."""
    return {
        index
        for index, link in enumerate(projection.links)
        if link.costed and sets.partition_of(link.sender, assignment) != sets.partition_of(link.receiver, assignment)
    }


def _crossing_calls(projection: Projection, sets: LinkSets, assignment: Mapping[str, str]) -> int:
    return sum(
        1
        for call in projection.calls
        if call.counted and sets.partition_of(call.client, assignment) != sets.partition_of(call.server, assignment)
    )


class _Checker:
    """Applies a move set to a scratch copy outside the project, runs the gate, and remembers each answer."""

    def __init__(
        self, model_root: str | Path, projection: Projection, arxml_version: ArxmlVersion | None = None
    ) -> None:
        self.model_root = model_root
        self.arxml_version = arxml_version
        self.projection = projection
        self.sets = projection.sets
        self.answers: dict[tuple[tuple[str, str], ...], Solution | RefusedSet] = {}
        self.current_crossing = _crossing_links(projection, self.sets, {})

    def __call__(self, assignment: Mapping[str, str]) -> Solution | RefusedSet:
        key = tuple(sorted(assignment.items()))
        if key not in self.answers:
            self.answers[key] = self._check(assignment)
        return self.answers[key]

    def _check(self, assignment: Mapping[str, str]) -> Solution | RefusedSet:
        moves = moves_of(self.projection, self.sets, assignment)
        if not moves:
            # The model itself, which check-model passed before any solve.
            return self._solution(assignment, moves, (), self.projection.loads)
        scratch = Path(tempfile.mkdtemp(prefix="swcraft-allocation-"))
        try:
            copy_root = scratch / "model"
            try:
                edits = write_move_set(self.model_root, copy_root, self.projection, moves)
            except BudgetCapExceeded as exceeded:
                return RefusedSet(
                    moves=moves,
                    rules=(),
                    reason=BUDGET_CAP_REASON,
                    messages=tuple(
                        f"task '{edit.task}' would need wcet {edit.written}, more than {MAX_FACTOR_DIGITS} digits"
                        for edit in exceeded.edits
                    ),
                )
            result = check_cross_layer_consistency(str(copy_root), self.arxml_version)
            loads = _loads_of(analyze_resource_budgets(str(copy_root)))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if result.consistent:
            return self._solution(assignment, moves, edits, loads)
        zero_rule_cores = {self.projection.partitions.get(edit.partition) for edit in edits if edit.kept_by_zero_rule}
        zero_rule = all(
            record.rule == "hardware.threshold-breach"
            and any(subject.layer == "hardware" and subject.element in zero_rule_cores for subject in record.subjects)
            for record in result.records
        )
        return RefusedSet(
            moves=moves,
            rules=tuple(sorted({f"rule:{record.rule}" for record in result.records})),
            reason=ZERO_RULE_REASON if zero_rule else TABLE_MISSED_REASON,
            messages=tuple(record.message for record in result.records),
        )

    def _solution(
        self,
        assignment: Mapping[str, str],
        moves: tuple[Move, ...],
        edits: tuple[TaskEdit, ...],
        loads: tuple[CoreLoad, ...],
    ) -> Solution:
        partitions = self.projection.partitions
        touched = {core for move in moves for core in (partitions.get(move.source), partitions.get(move.target))}
        full = {name: self.sets.partition_of(name, assignment) for name in sorted(self.sets.domains)}
        crossing = _crossing_links(self.projection, self.sets, full)
        links = self.projection.links
        return Solution(
            moves=moves,
            assignment=full,
            objective=self.sets.objective(full),
            edits=edits,
            loads=loads,
            touched_cores=tuple(sorted(core for core in touched if core is not None)),
            advisory_breaches=tuple(load.core for load in loads if load.advisory_breached),
            links_split=tuple(links[index] for index in sorted(crossing - self.current_crossing)),
            links_joined=tuple(links[index] for index in sorted(self.current_crossing - crossing)),
            calls=_crossing_calls(self.projection, self.sets, full),
        )


# -- the curve ---------------------------------------------------------------


def move_set_key(moves: Sequence[Move]) -> tuple[tuple[str, str, str], ...]:
    """A move set's names, ``(component, source, target)`` by component: its identity in the report and in the order."""
    return tuple((move.component, move.source, move.target) for move in moves)


def candidate_order(solution: Solution) -> tuple[Fraction, int, tuple[tuple[str, str, str], ...]]:
    """The enumerated candidates' order: by volume, then by the number of moves, then by the move set's names."""
    return solution.objective, len(solution.moves), move_set_key(solution.moves)


#: The answers of :func:`_run_solver` that are statuses; any other is an execution failure.
_SOLVER_ANSWERS = frozenset({"OPTIMAL", "FEASIBLE", "UNKNOWN", "INFEASIBLE"})


@dataclass
class _Context:
    solver: Any
    projection: Projection
    experiment: AllocationConfig
    checker: _Checker
    #: Every assignment the check refused, at any point: a set refused at one N is refused at every N.
    refused: list[dict[str, str]]
    #: ``N = 0``'s volume: a point improves only on a volume strictly below it.
    current: Fraction


@dataclass
class _Enumeration:
    candidates: tuple[Candidate, ...] = ()
    end: str | None = None
    bound: int | None = None
    refused: tuple[RefusedSet, ...] = ()


def _remaining(deadline: float, context: _Context) -> float:
    """The seconds left before *deadline*, never above ``time_limit_s``.

    ``(started + limit) - now`` rounds in floating point: when the clock has not
    advanced since ``started`` -- Windows' ``time.monotonic`` ticks about every
    16 ms -- the difference can exceed the limit by one unit in the last place,
    and the solver would be handed more time than the user allowed.
    """
    return min(deadline - _monotonic(), context.experiment.search.time_limit_s)


def _solve_point(context: _Context, n: int, enumerate_candidates: bool) -> tuple[CurvePoint, _Enumeration]:
    """Curve point *n* under one deadline, shared by its first solve, its re-solves and the enumeration."""
    started = _monotonic()
    deadline = started + context.experiment.search.time_limit_s
    search = SearchModel(context.solver, context.projection, context.projection.sets, n, context.refused)
    refused: list[RefusedSet] = []
    status, solution, cut_short = STATUS_UNKNOWN, None, True
    while (remaining := _remaining(deadline, context)) > 0:
        name, assignment = _run_solver(search, remaining)
        if name not in _SOLVER_ANSWERS:
            raise SolverExecutionError(f"the solver answered {name} at N = {n}: an execution failure")
        if name == "INFEASIBLE":
            status, cut_short = STATUS_INFEASIBLE, False
            break
        if assignment is None:
            break  # the limit cut the solve short before any solution: unknown
        checked = context.checker(assignment)
        if isinstance(checked, Solution):
            status = STATUS_OPTIMAL if name == "OPTIMAL" else STATUS_FEASIBLE
            solution, cut_short = checked, name != "OPTIMAL"
            break
        refused.append(checked)
        context.refused.append(assignment.copy())
        search.add_no_good(assignment)
    if solution is not None and solution.moves and solution.objective >= context.current:
        # A move set that buys no volume is no part of the answer: the point moves nothing. The lexicographic
        # objective already answers so at a proved optimum; a feasible incumbent is held to the same rule.
        empty = context.checker({})
        assert isinstance(empty, Solution)  # the empty move set is the model itself, which check-model passed
        solution = empty
    enumeration = _Enumeration()
    if enumerate_candidates and solution is not None and solution.objective < context.current:
        if status == STATUS_OPTIMAL:
            enumeration = _enumerate(context, search, solution, deadline)
        else:
            # A feasible point: its checked incumbent, without a tolerance claim (no optimum is proved).
            enumeration = _Enumeration(candidates=(Candidate(solution, within_tolerance=False),))
    point = CurvePoint(
        n=n,
        status=status,
        solution=solution,
        cut_short=cut_short,
        refused=tuple(refused),
        seconds=_monotonic() - started,
    )
    return point, enumeration


def _enumerate(context: _Context, search: SearchModel, optimum: Solution, deadline: float) -> _Enumeration:
    """Up to ``candidates`` checked move sets within the tolerance of the proved *optimum*, in the point's time.

    Every candidate's volume is strictly below the current one (the bound's cap).

    **The superset no-good.** After each accepted candidate ``S`` -- the optimum
    first -- the search adds ``sum_{c moved in S} x[c, target_c] <= |S| - 1``, so
    no later candidate contains ``S``'s moves. Candidates come in objective order,
    so such a superset would be volume-neutral padding or dominated by ``S``. A
    ``FEASIBLE`` answer means the deadline cut the solve: its set is checked and
    kept if accepted, without a superset no-good, and the enumeration ends cut
    short, so that list is not the ``K`` best. A set the check *refuses* keeps the
    exact-assignment no-good alone, because a superset of a refused set may pass
    the check. *What it does not reach*: two candidates that overlap without one
    containing the other are both kept.
    """
    bound = search.add_tolerance_bound(optimum.objective, exact(context.experiment.search.tolerance), context.current)
    # The optimum is the first candidate: it, and every superset of its move set, is excluded.
    search.add_superset_no_good(optimum.assignment)
    search.add_no_good(search.current)  # the empty move set is never a candidate (the bound excludes it too)
    kept = [optimum]
    refused: list[RefusedSet] = []
    end = ENUMERATION_AT_K
    while len(kept) < context.experiment.search.candidates:
        remaining = _remaining(deadline, context)
        if remaining <= 0:
            end = ENUMERATION_CUT_SHORT
            break
        name, assignment = _run_solver(search, remaining)
        if name not in _SOLVER_ANSWERS:
            raise SolverExecutionError(
                f"the solver answered {name} enumerating the candidates at N = {search.n}: an execution failure"
            )
        if name == "INFEASIBLE":
            end = ENUMERATION_EXHAUSTED
            break
        if assignment is None:
            end = ENUMERATION_CUT_SHORT
            break
        search.add_no_good(assignment)
        checked = context.checker(assignment)
        if isinstance(checked, Solution):
            kept.append(checked)
        else:
            refused.append(checked)
            context.refused.append(assignment.copy())
        if name == "FEASIBLE":
            # The deadline cut this solve: its set is not proved next in the order, so no superset no-good may
            # follow from it and the enumeration ends here, cut short.
            end = ENUMERATION_CUT_SHORT
            break
        if isinstance(checked, Solution):
            search.add_superset_no_good(assignment)
    # The point's proved optimum first; the enumerated ones by volume, then by their number of moves, then
    # by their move sets' names -- never in the order the solver found them.
    ordered = [optimum, *sorted(kept[1:], key=candidate_order)]
    return _Enumeration(
        candidates=tuple(Candidate(each, within_tolerance=True) for each in ordered),
        end=end,
        bound=bound,
        refused=tuple(refused),
    )


def solve_curve(
    model_root: str | Path,
    projection: Projection,
    experiment: AllocationConfig,
    arxml_version: ArxmlVersion | None = None,
) -> Curve:
    """The curve over ``N = 1 ... min(max_moves, max(1, |F|))`` and its candidates, every move set checked.

    ``scipy`` is imported here alone, so a ``swcraft`` without it runs every other command. Every curve
    point's move set and every candidate is applied to a scratch copy of *model_root* and run through
    ``check_cross_layer_consistency``; one refused on an error finding (a warning cuts nothing) is
    dropped, excluded at every later ``N`` and listed. Nothing is written into the model.

    No point above ``max(1, |F|)`` is built (``|F|`` the search components): ``last_solved`` is the highest
    ``N`` solved, and candidates asked for above it are enumerated at ``last_solved`` (``candidates_at``).

    Two runs give the same curve, up to the timing fields, only when the inputs, the solver's version
    and settings are the same and every search completed before its limit; a point or a candidate list
    a time limit cut short is marked so and may differ. A point is ``optimal`` when HiGHS proves an
    optimum at zero relative gap, re-verified exactly; HiGHS works in floating point with tolerances,
    so a better assignment its bound pruned would be missed undetected.

    Raises:
        SolverMissingError: ``scipy`` cannot be imported.
        SolverUnloadableError: ``scipy`` is installed but its native libraries cannot be loaded.
        NotRepresentableError: a constraint, the objective or the tolerance bound does not fit the solver's integers.
        SolverExecutionError: the solver answered an error status, or a solution that is no exact 0/1 assignment.
    """
    try:
        import scipy.optimize  # noqa: PLC0415 -- optional: scipy, needed by the allocation solver alone
        import scipy.sparse  # noqa: PLC0415 -- optional: scipy, needed by the allocation solver alone
    except ModuleNotFoundError:
        raise SolverMissingError(
            "the allocation search needs the solver 'scipy', which cannot be imported: install the "
            f"swcraft-tools wheel's dependency with pip install '{SOLVER_DISTRIBUTION}'"
        ) from None
    except (ImportError, OSError):
        raise SolverUnloadableError(
            "the allocation search's solver 'scipy' is installed but its native libraries cannot be loaded "
            f"on this machine: reinstall it with pip install --force-reinstall '{SOLVER_DISTRIBUTION}'"
        ) from None
    started = _monotonic()
    sets = projection.sets
    candidates_at = experiment.search.candidates_at
    assert candidates_at is not None  # its field_validator (validate_default) fills it with max_moves when omitted
    last = min(experiment.search.max_moves, max(1, len(sets.domains)))
    enumerate_at = min(candidates_at, last)
    # Built once before any solve, so an experiment the solver's integers cannot represent is refused first.
    SearchModel(scipy, projection, sets, last)
    # Each move set is judged at the project's ARXML version, as `check-model` judges it.
    checker = _Checker(model_root, projection, arxml_version)
    context = _Context(scipy, projection, experiment, checker, [], projection.current_volume)
    points: list[CurvePoint] = []
    enumeration = _Enumeration()
    for n in range(1, last + 1):
        point, answer = _solve_point(context, n, enumerate_candidates=n == enumerate_at)
        points.append(point)
        if n == enumerate_at:
            enumeration = answer
    return Curve(
        constant=sets.c0,
        current=projection.current_volume,
        points=tuple(points),
        max_moves=experiment.search.max_moves,
        candidates_at=enumerate_at,
        candidates=enumeration.candidates,
        enumeration=enumeration.end,
        tolerance_bound=enumeration.bound,
        refused=enumeration.refused,
        solver_version=str(getattr(scipy, "__version__", "unknown")),
        seconds=_monotonic() - started,
    )


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

#: The version of ``result.json``'s shape; a change a reader of the file must know about moves it.
RESULT_SCHEMA_VERSION = 1

#: The two files of a run, written under ``<dist_root>/experiments/<name>/``.
EXPERIMENTS_DIRECTORY = "experiments"
REPORT_FILENAME = EXPERIMENT_REPORT_FILENAME
RESULT_FILENAME = EXPERIMENT_RESULT_FILENAME

#: The report's sections, each heading named once and rendered from here.
SECTION_FINGERPRINT = "Fingerprint"
SECTION_CURRENT_STATE = "Current state"
SECTION_FREEDOM_TABLE = "Freedom table"
SECTION_CURVE = "Curve"
SECTION_CANDIDATES = "Candidates"
SECTION_DROPPED = "Dropped move sets"
SECTION_DATA_GAPS = "Data gaps and assumptions"
#: The sections in the report's order.
REPORT_SECTIONS: tuple[str, ...] = (
    SECTION_FINGERPRINT,
    SECTION_CURRENT_STATE,
    SECTION_FREEDOM_TABLE,
    SECTION_CURVE,
    SECTION_CANDIDATES,
    SECTION_DROPPED,
    SECTION_DATA_GAPS,
)

#: The unit of every volume, link cost and objective the report states.
VOLUME_UNIT = "bytes x Hz"
#: A table cell or a figure with no value.
ABSENT_CELL = "—"

TOLERANCE_CLAIM = "within tolerance of the proved optimum"
NO_TOLERANCE_CLAIM = "not proved — no tolerance claim"

#: The assumptions every report states, whatever the model.
STANDING_ASSUMPTIONS: tuple[str, ...] = (
    "a runnable costs the same on every core: its declared wcet is not scaled by the target core",
    f"{VOLUME_UNIT} is not a transport cost: it is the logical volume a link carries, never a bus or a "
    "communication-stack cost",
    "sizes are logical: bits from the base type's size over 8; alignment, padding and every transport "
    "overhead (headers, serialisation, protection) are excluded",
    "client-server calls are counted, never costed",
    "memory stays where it is: a memory only bounds where a component may move",
    "no task is created, moved whole or re-budgeted: each candidate states the wcet edit of every task "
    "it touches and applies none",
    "an advisory core's breach is reported and never constrained",
)


@dataclass(frozen=True)
class Fingerprint:
    """What a report was computed from: the model's layer files and the two versions (the parameters beside them).

    The digest is taken right after ``load_model_state`` reads the model, before
    the gate, the projection and the solve. **What it does not reach:** the run
    reads the project's files again after it (the gate, the budget analysis,
    every candidate's scratch copy), so a file changed while the run is under
    way can enter a figure the digest does not describe. The digest is a record
    of the files at the read, not a lock on them.
    """

    #: ``sha256:`` over the framed ``(storage name, length, sha256)`` records of every layer file read.
    model_digest: str
    files: int
    swcraft_version: str
    solver_version: str


def model_digest(model_root: str | Path) -> tuple[str, int]:
    """A framed sha256 over every layer file under *model_root*, and the number of files.

    The files are the ones the readers list (``scan_layer`` per declared
    layer), each recorded as its model-root-relative POSIX name, its length and
    its content's sha256, length-framed so file names and boundaries take part.
    A stray file beside the model is not read, so it takes no part.
    """
    root = Path(model_root)
    registry = current_registry()
    records: set[tuple[str, int, bytes]] = set()
    for layer in registry.layer_names:
        for entry in scan_layer(root, layer, registry=registry).files:
            content = entry.path.read_bytes()
            records.add((entry.path.relative_to(root).as_posix(), len(content), hashlib.sha256(content).digest()))
    digest = hashlib.sha256()
    for name, length, content_digest in sorted(records):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(length.to_bytes(8, "big"))
        digest.update(content_digest)
    return f"sha256:{digest.hexdigest()}", len(records)


def _figure(value: Fraction | int) -> int | dict[str, int]:
    """An exact figure for ``result.json``: an integer, or a rational ``{"n": ..., "d": ...}`` -- never a float."""
    value = Fraction(value)
    if value.denominator == 1:
        return value.numerator
    return {"n": value.numerator, "d": value.denominator}


def _exact_figure(value: int | Mapping[str, int]) -> Fraction:
    """The exact value of a ``result.json`` figure (:func:`_figure`'s inverse)."""
    return Fraction(value) if isinstance(value, int) else Fraction(value["n"], value["d"])


def _shown(value: int | Mapping[str, int] | None) -> str:
    """A volume or a parameter of ``result.json`` as ``report.md`` shows it: ``format_decimal``'s rule.

    Two decimals truncated downward, never rounded, with ``~`` before a value two
    decimals cannot express, so a shown figure never reads higher than the exact
    one ``result.json`` holds.
    """
    return ABSENT_CELL if value is None else format_decimal(_exact_figure(value))


def _shown_share(value: int | Mapping[str, int]) -> str:
    """A core's load or threshold, a share of the core, as a percentage: ``format_percent``, the budget report's."""
    return format_percent(_exact_figure(value))


def _milliseconds(seconds: float) -> int:
    return round(seconds * 1000)


def _load_entry(load: CoreLoad) -> dict[str, Any]:
    return {
        "core": load.core,
        "enforcement": load.enforcement,
        "load": _figure(load.load),
        "threshold": _figure(load.threshold),
        "breached": load.breached,
    }


def _link_entry(link: Link) -> dict[str, Any]:
    return {
        "connector": link.connector,
        "sender": link.sender,
        "senderPort": link.sender_port,
        "receiver": link.receiver,
        "receiverPort": link.receiver_port,
        "element": link.element,
        "cost": _figure(link.cost),
    }


def _move_entry(move: Move, partitions: Mapping[str, str | None]) -> dict[str, Any]:
    return {
        "component": move.component,
        "source": move.source,
        "sourceCore": partitions.get(move.source),
        "target": move.target,
        "targetCore": partitions.get(move.target),
        "runnables": [
            {"runnable": runnable_name, "sourceTask": source_task, "targetTask": target_task}
            for runnable_name, source_task, target_task in move.runnables
        ],
    }


def _edit_entry(edit: TaskEdit) -> dict[str, Any]:
    return {
        "task": edit.task,
        "partition": edit.partition,
        "original": edit.original,
        "outgoing": edit.outgoing_text,
        "incoming": edit.incoming_text,
        "written": edit.written,
        "unchanged": edit.unchanged,
        "keptByZeroRule": edit.kept_by_zero_rule,
        "frees": edit.frees,
    }


def _solution_entry(solution: Solution, projection: Projection, curve: Curve) -> dict[str, Any]:
    touched = set(solution.touched_cores)
    return {
        "objective": _figure(solution.objective),
        "moves": [_move_entry(move, projection.partitions) for move in solution.moves],
        "loads": {
            "before": [_load_entry(load) for load in projection.loads if load.core in touched],
            "after": [_load_entry(load) for load in solution.loads if load.core in touched],
        },
        "volume": {"before": _figure(curve.current), "after": _figure(solution.objective)},
        "linksSplit": [_link_entry(link) for link in solution.links_split],
        "linksJoined": [_link_entry(link) for link in solution.links_joined],
        "calls": {"before": projection.current_calls, "after": solution.calls},
        "edits": [_edit_entry(edit) for edit in solution.edits],
        "advisoryBreaches": list(solution.advisory_breaches),
    }


def _table_entry(row: FreedomRow) -> dict[str, Any]:
    placement = row.placement
    return {
        "component": placement.component,
        "level": placement.level,
        "partition": placement.partition,
        "core": placement.core,
        "free": placement.free,
        "notFree": list(placement.reasons),
        "targets": [
            {
                "partition": target.partition,
                "core": target.core,
                "class": target.klass,
                "unrated": target.unrated,
                "reasons": [
                    {"class": reason.klass, "rule": reason.rule, "text": reason.text, "opens": reason.opens}
                    for reason in target.reasons
                ],
                "targetTasks": dict(sorted(target.target_tasks.items())),
            }
            for target in row.targets
        ],
    }


def _dropped(curve: Curve) -> list[tuple[int, RefusedSet]]:
    """Every dropped move set, once each, with the curve point it was found at."""
    seen: set[tuple[tuple[str, str, str], ...]] = set()
    dropped: list[tuple[int, RefusedSet]] = []
    found = [(point.n, refused) for point in curve.points for refused in point.refused]
    found.extend((curve.candidates_at, refused) for refused in curve.refused)
    for n, refused in found:
        key = move_set_key(refused.moves)
        if key not in seen:
            seen.add(key)
            dropped.append((n, refused))
    return dropped


def summary_line(experiment: AllocationConfig, projection: Projection, curve: Curve) -> str:
    """The one line the command prints and the report opens with: the volume now, each point, the candidates.

    Three runs end without a candidate and are still successful runs: *no free
    component* (nothing can move), *no improvement at any N* (no point's checked
    move set brings the volume below the current one -- decided on the volume,
    never on whether anything moved) and *no checked move set at any N* (no
    point carries one: every point ``unknown`` or ``infeasible``, which claims
    no finding about the model).
    """
    head = f"Experiment '{experiment.name}': inter-partition volume {format_decimal(curve.current)} {VOLUME_UNIT} now"
    if not any(row.placement.free for row in projection.table):
        return f"{head}; no free component, so nothing can move."
    points = "; ".join(
        f"N = {point.n}: "
        + (format_decimal(point.solution.objective) if point.solution is not None else "no move set")
        + f" ({point.status})"
        for point in curve.points
    )
    if all(point.solution is None for point in curve.points):
        return f"{head}; no checked move set at any N ({points})."
    if not any(point.solution is not None and point.solution.objective < curve.current for point in curve.points):
        return f"{head}; no improvement at any N ({points})."
    count = len(curve.candidates)
    return f"{head}; {points}; {count} candidate{'' if count == 1 else 's'} at N = {curve.candidates_at}."


def _parameters(experiment: AllocationConfig, output: str) -> dict[str, Any]:
    """The effective configuration in the file's own shape, every default filled in.

    *output* is the report directory as resolved, made project-relative -- never
    the ``output`` the file wrote, which may spell the same directory otherwise.
    Every key is spelled here, never dumped from the model.
    """
    return {
        "name": experiment.name,
        "output": output,
        "search": {
            "max_moves": experiment.search.max_moves,
            "candidates": experiment.search.candidates,
            "candidates_at": experiment.search.candidates_at,
            "tolerance": _figure(exact(experiment.search.tolerance)),
            "time_limit_s": _figure(exact(experiment.search.time_limit_s)),
        },
        "constraints": {
            "locked": sorted(experiment.constraints.locked),
            "allowed_partitions": {
                name: list(allowed) for name, allowed in sorted(experiment.constraints.allowed_partitions.items())
            },
        },
    }


def _gaps(projection: Projection, curve: Curve) -> list[str]:
    gaps = list(STANDING_ASSUMPTIONS)
    gaps.append(
        f"the constant C0 = {format_decimal(curve.constant)} {VOLUME_UNIT} is the cost of the crossing links "
        "whose crossing no admissible move changes; every objective this report states includes it once"
    )
    gaps.extend(projection.data_gaps)
    net = [
        f"{row.placement.component}.{each.runnable}"
        for row in projection.table
        for each in row.placement.runnables
        if each.budget is not None and each.wcet_type == "net"
    ]
    if net:
        gaps.append(f"budgets declared net, whose external calls no load counts: {', '.join(net)}")
    gaps.extend(
        f"a time limit cut the search at N = {point.n} short: its status and move set may differ on another run "
        "or another machine"
        for point in curve.points
        if point.cut_short
    )
    if curve.enumeration == ENUMERATION_CUT_SHORT:
        gaps.append(
            f"a time limit cut the candidate enumeration at N = {curve.candidates_at} short: the candidate list "
            "may differ on another run or another machine"
        )
    solutions = [
        *(point.solution for point in curve.points if point.solution is not None),
        *(candidate.solution for candidate in curve.candidates),
    ]
    breaches = {core for solution in solutions for core in solution.advisory_breaches}
    breaches |= {load.core for load in projection.loads if load.advisory_breached}
    gaps.extend(
        f"advisory core '{core}' is over its threshold in a state this report states: reported, not constrained"
        for core in sorted(breaches)
    )
    return gaps


def result_document(
    experiment: AllocationConfig,
    projection: Projection,
    curve: Curve,
    fingerprint: Fingerprint,
    *,
    output: str,
    configuration: dict[str, Any] | None,
) -> dict[str, Any]:
    """``result.json``'s document: every figure an integer, a rational or a budget text, timing in ms.

    The budget figures of a task edit (``original``, ``outgoing``, ``incoming``,
    ``written``, ``frees``) are texts in the model's own spelling (``1500us``).

    The record of the run: ``kind`` (the configuration class's own, never a key of
    the file), and under ``fingerprint`` the *configuration* --
    ``{"file", "insideProject", "sha256"}``, the file named project-relative or,
    outside the project, by its file name alone; ``None`` for a run on the
    defaults -- and the effective parameters, with *output* the report directory
    made project-relative. Those two are the document's only paths, both
    relative: no absolute path, and no path to a model file.
    """
    return {
        "schemaVersion": RESULT_SCHEMA_VERSION,
        "name": experiment.name,
        "kind": experiment.kind,
        "summary": summary_line(experiment, projection, curve),
        "fingerprint": {
            "model": fingerprint.model_digest,
            "files": fingerprint.files,
            "swcraft": fingerprint.swcraft_version,
            "solver": fingerprint.solver_version,
            "configuration": configuration,
            "parameters": _parameters(experiment, output),
        },
        "currentState": {
            "loads": [_load_entry(load) for load in projection.loads],
            "volume": _figure(curve.current),
            "calls": projection.current_calls,
        },
        "freedomTable": [_table_entry(row) for row in projection.table],
        "constant": _figure(curve.constant),
        "lastSolved": curve.last_solved,
        "curve": [
            {
                "n": point.n,
                "status": point.status,
                "cutShort": point.cut_short,
                "solution": None if point.solution is None else _solution_entry(point.solution, projection, curve),
                "milliseconds": _milliseconds(point.seconds),
            }
            for point in curve.points
        ],
        "candidates": {
            "at": curve.candidates_at,
            "enumeration": curve.enumeration,
            "toleranceBound": curve.tolerance_bound,
            "items": [
                {
                    "tolerance": TOLERANCE_CLAIM if candidate.within_tolerance else NO_TOLERANCE_CLAIM,
                    **_solution_entry(candidate.solution, projection, curve),
                }
                for candidate in curve.candidates
            ],
        },
        "dropped": [
            {
                "foundAt": n,
                "moves": [_move_entry(move, projection.partitions) for move in refused.moves],
                "rules": list(refused.rules),
                "reason": refused.reason,
                "messages": list(refused.messages),
            }
            for n, refused in _dropped(curve)
        ],
        "dataGaps": _gaps(projection, curve),
        "milliseconds": _milliseconds(curve.seconds),
    }


@dataclass(frozen=True)
class ReportLinks:
    """Where ``report.md`` links a name to: element name -> its model file, relative to the report directory.

    Three mappings, one per layer a linked name belongs to: the component
    types (``swc``), the tasks (``tasks``) and the partitions (``hardware``).
    Each target is POSIX-spelled with every segment percent-encoded, so a
    Markdown preview opens it from the report directory. A name with no target
    is rendered as plain text. Presentation only: ``result.json`` carries none
    of it.
    """

    components: Mapping[str, str] = field(default_factory=dict)
    tasks: Mapping[str, str] = field(default_factory=dict)
    partitions: Mapping[str, str] = field(default_factory=dict)


#: No links: ``report.md`` exactly as ``result.json``'s document renders it.
_NO_LINKS = ReportLinks()


def _layer_targets(model_root: Path, layer: str, directory: Path) -> dict[str, str]:
    """``name -> relative target`` for every element of *layer* declared by exactly one file.

    A name more than one file declares maps to nothing (``check-model`` refuses such a model first).
    """
    targets: dict[str, str] = {}
    for name, files in stored_element_files(model_root, layer, registry=current_registry()).items():
        if len(files) != 1:
            continue
        relative = os.path.relpath(files[0].path, directory)
        targets[name] = "/".join(urllib.parse.quote(segment) for segment in Path(relative).parts)
    return targets


def read_report_links(model_root: str | Path, directory: Path) -> ReportLinks:
    """The links ``report.md`` written under *directory* carries, for the model under *model_root* as it is now.

    What this does not reach: a report copied elsewhere has dead links, and a link
    names the file that declared the element at this scan (after the solve).
    """
    root = Path(model_root)
    return ReportLinks(
        components=_layer_targets(root, "swc", directory),
        tasks=_layer_targets(root, "tasks", directory),
        partitions=_layer_targets(root, "hardware", directory),
    )


def _linked(name: str, targets: Mapping[str, str]) -> str:
    """*name* as ``[name](target)`` when *targets* has one for it, else *name* unchanged."""
    target = targets.get(name)
    return name if target is None else f"[{name}]({target})"


def _cell(value: object) -> str:
    """A table cell: ``|`` escaped, an absent value as an em dash."""
    if value is None or value == "":
        return ABSENT_CELL
    return str(value).replace("|", "\\|")


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows)
    return [*lines, ""]


def _moves_text(moves: Sequence[Mapping[str, Any]]) -> str:
    return "; ".join(f"{move['component']}: {move['source']} -> {move['target']}" for move in moves) or "none"


#: The parameters ``result.json`` holds as exact figures, by dotted key; every other one is a count or a list.
_FIGURE_PARAMETERS = ("search.tolerance", "search.time_limit_s")

#: The configuration's two sections, in the file's order: ``report.md`` lists their keys dotted.
_PARAMETER_SECTIONS = ("search", "constraints")


def _parameter_text(key: str, value: Any) -> str:
    """One parameter as ``report.md`` shows it, rendered by its dotted key and never by the shape of its value."""
    if key in _FIGURE_PARAMETERS:
        return _shown(value)
    if key == "constraints.allowed_partitions":
        return "; ".join(f"{name}: {', '.join(allowed)}" for name, allowed in value.items()) or "none"
    if key == "constraints.locked":
        return ", ".join(value) or "none"
    return str(value)


def _configuration_text(configuration: Mapping[str, Any] | None) -> str:
    """The Fingerprint's ``Configuration:`` value: the file and its digest, or the defaults."""
    if configuration is None:
        return "defaults, no configuration file"
    where = "" if configuration["insideProject"] else " (outside the project directory)"
    return f"`{configuration['file']}`{where}, {configuration['sha256']}"


def _edit_text(edit: Mapping[str, Any]) -> str:
    if edit["keptByZeroRule"]:
        return (
            f"keep {edit['original']} (the zero rule: no declared runnable budget is left in it, and the moves "
            f"free {edit['frees']})"
        )
    if edit["unchanged"]:
        return f"unchanged at {edit['original']} (the moves in and out cancel)"
    return f"{edit['original']} -> {edit['written']} (-{edit['outgoing']} +{edit['incoming']})"


def _solution_lines(entry: Mapping[str, Any], report_links: ReportLinks) -> list[str]:
    lines: list[str] = []
    for move in entry["moves"]:
        component = _linked(move["component"], report_links.components)
        source, target = (
            _linked(move["source"], report_links.partitions),
            _linked(move["target"], report_links.partitions),
        )
        lines.append(
            f"- **{component}**: partition {source} (core {_cell(move['sourceCore'])}) -> "
            f"partition {target} (core {_cell(move['targetCore'])})"
        )
        lines.extend(
            f"  - runnable {each['runnable']}: task {_linked(each['sourceTask'], report_links.tasks)} -> "
            f"task {_linked(each['targetTask'], report_links.tasks)}"
            for each in move["runnables"]
        )
    lines.append("")
    lines.append(
        f"Inter-partition volume: {_shown(entry['volume']['before'])} -> "
        f"{_shown(entry['volume']['after'])} {VOLUME_UNIT}. Inter-partition calls: "
        f"{entry['calls']['before']} -> {entry['calls']['after']}."
    )
    lines.append("")
    before = {load["core"]: load for load in entry["loads"]["before"]}
    after = {load["core"]: load for load in entry["loads"]["after"]}
    rows = [
        [
            core,
            _shown_share(before[core]["load"]) if core in before else ABSENT_CELL,
            _shown_share(after[core]["load"]) if core in after else ABSENT_CELL,
            _shown_share((after.get(core) or before[core])["threshold"]),
        ]
        for core in sorted(set(before) | set(after))
    ]
    if rows:
        lines.extend(_table(("Touched core", "Load before", "Load after", "Threshold"), rows))
    for heading, links in (
        ("Links that become inter-partition", entry["linksSplit"]),
        ("Links that cease to be inter-partition", entry["linksJoined"]),
    ):
        if links:
            lines.append(f"{heading}:")
            lines.extend(
                f"- {link['sender']}.{link['senderPort']} -> {link['receiver']}.{link['receiverPort']} "
                f"({link['element']}): {_shown(link['cost'])} {VOLUME_UNIT}"
                for link in links
            )
            lines.append("")
    if entry["edits"]:
        lines.append("What applying the move takes, one wcet edit per touched task:")
        lines.extend(
            f"- task {_linked(edit['task'], report_links.tasks)} (partition {edit['partition']}): "
            f"wcet {_edit_text(edit)}"
            for edit in entry["edits"]
        )
        lines.append("")
    if entry["advisoryBreaches"]:
        lines.append(
            "Advisory cores over their threshold after the moves (reported, not constrained): "
            + ", ".join(entry["advisoryBreaches"])
        )
        lines.append("")
    return lines


def _freedom_rows(document: Mapping[str, Any], report_links: ReportLinks) -> list[list[object]]:
    rows: list[list[object]] = []
    for row in document["freedomTable"]:
        component = _linked(row["component"], report_links.components)
        where = f"{_cell(row['partition'])} (core {_cell(row['core'])})"
        if not row["free"]:
            rows.append([component, row["level"], where, ABSENT_CELL, "not free", "; ".join(row["notFree"])])
            continue
        if not row["targets"]:
            rows.append([component, row["level"], where, ABSENT_CELL, "no other partition", ""])
        for target in row["targets"]:
            reasons = "; ".join(
                reason["text"] + (f" ({reason['rule']})" if reason["rule"] else "") + f"; opens: {reason['opens']}"
                for reason in target["reasons"]
            )
            if target["unrated"] and not target["reasons"]:
                reasons = "an unrated partition (an assumption, listed below)"
            target_where = f"{target['partition']} (core {_cell(target['core'])})"
            rows.append([component, row["level"], where, target_where, target["class"], reasons])
    return rows


def _heading(section: str) -> list[str]:
    """A section's heading line and the blank line after it."""
    return [f"## {section}", ""]


def render_markdown(document: Mapping[str, Any], report_links: ReportLinks | None = None) -> str:
    """``report.md``, rendered from ``result.json``'s *document* -- the one content of the two files.

    With *report_links* a component's name in the freedom table's *Component*
    column and in each candidate's move line, a move's source and target
    partition, a runnable's source and target task and the task of each
    ``wcet`` edit line link to their model files; every other cell and line is
    the same text. Without it, nothing links.
    """
    report_links = _NO_LINKS if report_links is None else report_links
    fingerprint = document["fingerprint"]
    current = document["currentState"]
    candidates = document["candidates"]
    lines: list[str] = [f"# Allocation experiment `{document['name']}`", "", document["summary"], ""]

    parameters = fingerprint["parameters"]
    lines += _heading(SECTION_FINGERPRINT)
    lines.append(f"- Kind: {document['kind']}")
    lines.append(f"- Configuration: {_configuration_text(fingerprint['configuration'])}")
    lines.append(f"- Output: `{parameters['output']}`")
    lines.append(f"- Model: `{fingerprint['model']}` over {fingerprint['files']} layer files")
    lines.append(f"- SWCraft: {fingerprint['swcraft']}")
    lines.append(f"- Solver: SciPy {fingerprint['solver']}")
    # `name` and `output` are not repeated as parameter lines: the title carries the one, `Output:` the other.
    lines.extend(
        f"- {section}.{key}: {_parameter_text(f'{section}.{key}', value)}"
        for section in _PARAMETER_SECTIONS
        for key, value in parameters[section].items()
    )
    lines.append("")

    lines += _heading(SECTION_CURRENT_STATE)
    load_rows = [
        [
            load["core"],
            load["enforcement"],
            _shown_share(load["load"]),
            _shown_share(load["threshold"]),
            "over" if load["breached"] else "",
        ]
        for load in current["loads"]
    ]
    lines.extend(_table(("Core", "Enforcement", "Load", "Threshold", "Breach"), load_rows))
    lines.append(f"Inter-partition volume: {_shown(current['volume'])} {VOLUME_UNIT}.")
    lines.append(f"Inter-partition calls: {current['calls']}.")
    lines.append("")

    lines += _heading(SECTION_FREEDOM_TABLE)
    lines.extend(
        _table(("Component", "Level", "Partition", "Target", "Class", "Reasons"), _freedom_rows(document, report_links))
    )

    lines += _heading(SECTION_CURVE)
    lines.append(
        f"N = 0, the current state: {_shown(current['volume'])} {VOLUME_UNIT}. Every objective includes "
        f"the constant C0 = {_shown(document['constant'])} {VOLUME_UNIT} once."
    )
    lines.append("")
    curve_rows = [
        [
            point["n"],
            point["status"],
            _shown(point["solution"]["objective"]) if point["solution"] else ABSENT_CELL,
            _moves_text(point["solution"]["moves"]) if point["solution"] else ABSENT_CELL,
            "cut short by the time limit: may differ on another run" if point["cutShort"] else "",
        ]
        for point in document["curve"]
    ]
    lines.extend(_table(("N", "Status", f"Objective ({VOLUME_UNIT})", "Moves", "Note"), curve_rows))
    last, max_moves = document["lastSolved"], parameters["search"]["max_moves"]
    if last < max_moves:
        above = f"N = {last + 1}" if last + 1 == max_moves else f"N = {last + 1} … {max_moves}"
        lines.append(
            f"{above}: the same as N = {last} (no component left to move: a move set never moves more "
            "components than the search may move, so a larger N allows nothing new)."
        )
        lines.append("")

    lines += _heading(SECTION_CANDIDATES)
    if candidates["items"]:
        ended = f" The enumeration ended: {candidates['enumeration']}." if candidates["enumeration"] else ""
        lines.append(f"At N = {candidates['at']}.{ended}")
        lines.append("")
        for index, item in enumerate(candidates["items"], start=1):
            lines.append(f"### Candidate {index}: {_shown(item['objective'])} {VOLUME_UNIT} ({item['tolerance']})")
            lines.append("")
            lines.extend(_solution_lines(item, report_links))
    else:
        lines.append(f"None at N = {candidates['at']}.")
        lines.append("")

    lines += _heading(SECTION_DROPPED)
    if document["dropped"]:
        lines.extend(
            f"- at N = {dropped['foundAt']}: {_moves_text(dropped['moves'])}; {dropped['reason']} "
            f"({', '.join(dropped['rules']) or '; '.join(dropped['messages'])})"
            for dropped in document["dropped"]
        )
    else:
        lines.append("None: every move set the search proposed passed check-model on its copy.")
    lines.append("")

    lines += _heading(SECTION_DATA_GAPS)
    lines.extend(f"- {gap}" for gap in document["dataGaps"])
    lines.append("")
    return "\n".join(lines)


def result_json(document: Mapping[str, Any]) -> str:
    """``result.json``'s text: two-space indentation, non-ASCII kept, one final newline."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AllocationRun:
    """One completed run: its document and the two files it wrote."""

    document: dict[str, Any]
    report_path: Path
    result_path: Path

    @property
    def summary(self) -> str:
        return str(self.document["summary"])


def run_allocation(project: SwcraftProject, config_path: str | Path | None) -> AllocationRun:
    """Read the model, refuse one ``check-model`` refuses, read the configuration, check it, solve, write.

    In that order, and **nothing is written** before the last step: a project that cannot be read and
    every refusal and failure below raise first. *config_path* is resolved against the current
    directory; ``None`` runs the defaults, the same configuration as an empty file. The report
    directory is ``<dist_root>/experiments/<name>/`` (created if absent), or the directory the
    configuration's ``output`` names. ``result.json``'s only paths are the configuration file and the report directory,
    both project-relative (the configuration by its file name alone when outside the project).

    The last step writes both files before it replaces either, ``result.json`` first, so a failed write
    leaves the previous pair as it was. A rename the operating system refuses between the two (a
    ``report.md`` held open on Windows) leaves the new ``result.json`` beside the previous
    ``report.md`` and exits 1; nothing locks the directory against two concurrent runs.

    Raises:
        ModelRefusedError: ``check_cross_layer_consistency`` has error findings (their count named;
            a warning refuses nothing).
        ConfigFileUnreadableError: the configuration file cannot be read.
        ExperimentError: the configuration, a name it uses or its ``output`` is refused.
        SolverMissingError: ``scipy`` cannot be imported (its subclass: it cannot be loaded).
        NotRepresentableError: the experiment does not fit the solver's integers.
        SolverExecutionError: the solver answered an error status or an inexact solution.
    """
    model_root = project.resolved_model_root
    state = load_model_state(model_root)
    # Hashed right after the read the projection is built from, never after the solve.
    digest, files = model_digest(model_root)
    consistency = check_cross_layer_consistency(str(model_root), project.resolved_arxml_version)
    if not consistency.consistent:
        raise ModelRefusedError(len(consistency.issues))
    configuration: dict[str, Any] | None = None
    if config_path is None:
        experiment = AllocationConfig()
    else:
        try:
            experiment, config_digest = load_config(config_path)
        except OSError as exc:
            raise ConfigFileUnreadableError(
                f"the configuration file cannot be read: {exc.strerror or type(exc).__name__}"
            ) from None
        except UnicodeDecodeError:
            raise ConfigFileUnreadableError("the configuration file cannot be read: it is not UTF-8 text") from None
        relative = project_relative_name(project, config_path)
        configuration = {
            "file": Path(config_path).name if relative is None else relative,
            "insideProject": relative is not None,
            "sha256": config_digest,
        }
    # Checked before the report directory is resolved; `read_projection` checks again.
    check_config_names(experiment, state)
    try:
        if experiment.output is None:
            directory = experiment_default_directory(project, f"{EXPERIMENTS_DIRECTORY}/{experiment.name}")
        else:
            directory = experiment_output_directory(project, experiment.output)
    except ValueError as exc:
        raise ExperimentError(str(exc)) from None
    # Both refuse a directory outside the project, so this raises only on a broken invariant, never records "".
    output = directory.relative_to(project.resolved_project_root).as_posix()
    projection = read_projection(model_root, experiment, state)
    curve = solve_curve(model_root, projection, experiment, project.resolved_arxml_version)
    fingerprint = Fingerprint(digest, files, __version__, curve.solver_version)
    document = result_document(experiment, projection, curve, fingerprint, output=output, configuration=configuration)
    report_path = directory / REPORT_FILENAME
    result_path = directory / RESULT_FILENAME
    linked = read_report_links(model_root, directory)
    _write_pair(directory, ((result_path, result_json(document)), (report_path, render_markdown(document, linked))))
    return AllocationRun(document=document, report_path=report_path, result_path=result_path)


#: The suffix of a report file being written, before it is renamed into place.
_PARTIAL_SUFFIX = ".partial"


def _write_pair(directory: Path, files: Sequence[tuple[Path, str]]) -> None:
    """Write every ``(path, text)`` of *files* under a temporary name, then rename each into place in order.

    Each temporary name is unique to this call (``<name>.<random>.partial``,
    created exclusively), so a stale ``*.partial`` is neither read nor
    overwritten. The files get ``0o666`` less the umask, never
    ``tempfile.mkstemp``'s ``0o600``, which the rename would carry into place. A
    failure while writing removes every temporary file and replaces nothing.

    What this does not reach: there is no lock, so two concurrent runs whose
    renames interleave can leave ``result.json`` from one beside ``report.md``
    from the other. A process killed before the ``finally`` leaves its
    ``*.partial`` behind. A report directory this call created stays behind,
    empty, after a failed write.
    """
    directory.mkdir(parents=True, exist_ok=True)
    partial: list[tuple[Path, Path, str]] = []
    try:
        for path, text in files:
            partial.append((create_unique_sibling(directory, path.name, _PARTIAL_SUFFIX), path, text))
        for temporary, _path, text in partial:
            temporary.write_text(text, encoding="utf-8", newline="\n")
        for temporary, path, _text in partial:
            os.replace(temporary, path)
    finally:
        for temporary, _path, _text in partial:
            temporary.unlink(missing_ok=True)
