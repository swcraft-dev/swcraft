"""The test tables: the component and scheduling facts a test side verifies against.

``swcraft generate test-tables`` hands a test side three tables beside the ARXML, one row per object to verify, each
row carrying a stable ``row_id``, the facts the model states and a ``facts_hash`` over them.
:func:`build_test_tables` builds the rows in memory; :func:`generate_test_tables` refuses a project ``check-model``
refuses, reads the annotations and writes the five files of :func:`bundle_files` into ``<dist_root>/test-tables/``.

**Rows.** A row id holds identity only, never a task, period, partition, core, file or comment: ``components.csv``
``component:<Type>``; ``component-placement.csv`` ``occurrence:<path>``, or ``unplaced:<Type>`` for a type with no
occurrence; ``runnable-scheduling.csv`` ``occurrence:<path>:<runnable>`` or ``unplaced:<Type>:<runnable>``. Rows are
ordered by :func:`~swcraft_tools.scheduling_identity.identity_sort_key`, then runnable name (``components.csv`` by
type name), never by a YAML list's order.

**Cells** are text: a list or structure is canonical JSON, a float ``json.dumps`` of the held value (seconds where
the column says ``_s``), a boolean ``true`` / ``false``, an empty collection ``[]`` or ``{}``. An **empty cell means
"not declared in the model"** and nothing else. :func:`facts_hash` is the first 16 hex digits of sha256 over the
canonical JSON of ``{"table", "revision", "row_id", "facts"}`` (``description`` hashed, ``source_ref`` not);
:func:`facts_digest` is the full sha256 over every table's ``(row_id, facts_hash)`` pairs.

**Annotations** (``verification_method``, ``comment``, ``accepted``, ``accepted_facts_hash``) are the test side's,
read by :func:`read_annotations` and joined by ``row_id``. An acceptance is pinned to ``accepted_facts_hash``, else
the file's ``facts_hash``, else :data:`NO_PIN`. ``review_state`` is ``accepted`` / ``rejected`` while the pinned
facts are the row's, ``stale`` when not (SWCraft never clears), else ``not-reviewed``; an annotation whose row is
gone is carried in ``orphaned-annotations.csv``, never dropped.

Not reached: it assumes a model ``check-model`` accepts, and on any other does not judge: an undefined walk gives no
occurrence, an occurrence of an undeclared type is skipped, a task entry the resolver refuses maps nothing, and a
runnable two tasks name keeps the first task listed. It states no execution order (``task_position`` is the list
order), dispatch, preemption, measured time (``wcet`` is a budget), data flow, interfaces or memory access.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from swcraft_tools._version import __version__
from swcraft_tools.experiment_frame import write_report_files
from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import stored_element_files
from swcraft_tools.project import (
    SwcraftProject,
    experiment_default_directory,
    path_contains,
    paths_overlap,
    resolved_path,
)
from swcraft_tools.reader import read_layer
from swcraft_tools.scheduling_identity import Identity, SchedulingIdentities, identities_for, identity_sort_key
from swcraft_tools.schema import (
    CallReturnedEvent,
    DataEvent,
    DisabledMode,
    Hardware,
    InitEvent,
    OperationEvent,
    Runnable,
    SWComponent,
    TimingEvent,
    core_of,
    hardware_by_name,
)
from swcraft_tools.validator import (
    ConsistencyIssue,
    check_cross_layer_consistency,
    effective_asil_levels,
    with_records,
)

__all__ = [
    "ACCEPTED",
    "COMPONENTS",
    "EMPTY_CELL",
    "FIELD_SIZE_LIMIT",
    "MANIFEST_FILE",
    "NOT_REVIEWED",
    "NO_PIN",
    "ORPHANS",
    "ORPHAN_COLUMNS",
    "PLACEMENT",
    "REJECTED",
    "REPORT_DIRECTORY",
    "REVIEW_STATE",
    "REVISIONS",
    "SCHEDULING",
    "SCHEMA_VERSION",
    "STALE",
    "TABLE_ORDER",
    "TEST_COLUMNS",
    "Annotation",
    "Annotations",
    "AnnotationsError",
    "GeneratedTestTables",
    "Table",
    "TablesPartlyReplacedError",
    "TablesRefusedError",
    "TestTables",
    "annotations_location_refusal",
    "build_test_tables",
    "bundle_files",
    "canonical_json",
    "facts_digest",
    "facts_hash",
    "generate_test_tables",
    "manifest_json",
    "orphans_csv",
    "read_annotations",
    "review_counts",
    "table_csv",
]

#: The bundle's directory below ``dist_root``, one of ``project.REPORT_DIRECTORIES``.
REPORT_DIRECTORY = "test-tables"
#: The bundle's manifest file.
MANIFEST_FILE = "manifest.json"
#: The bundle's schema version: a new table or column is a minor, a changed meaning a major.
SCHEMA_VERSION = "1.0.0"

#: The three tables' file stems, in the manifest's order.
COMPONENTS = "components"
PLACEMENT = "component-placement"
SCHEDULING = "runnable-scheduling"
TABLE_ORDER = (COMPONENTS, PLACEMENT, SCHEDULING)

#: Each table's semantic revision: raised when a column keeps its name but
#: changes its meaning, so every acceptance of that table turns stale.
REVISIONS: Mapping[str, int] = {COMPONENTS: 1, PLACEMENT: 1, SCHEDULING: 1}

#: The test side's columns, after SWCraft's own on every table.
TEST_COLUMNS = ("verification_method", "comment", "accepted", "accepted_facts_hash")
#: SWCraft's computed review state, the last column of every table.
REVIEW_STATE = "review_state"
#: The review states, as ``review_state`` writes them.
NOT_REVIEWED = "not-reviewed"
ACCEPTED = "accepted"
REJECTED = "rejected"
STALE = "stale"
#: The pin written when an acceptance names no facts: it matches no hash, so the
#: row stays ``stale`` until the test side clears it and so re-accepts the
#: current facts.
NO_PIN = "none"
#: The ``accepted`` values, after stripping and lower-casing; empty accepts nothing.
_ACCEPTED_VALUES: Mapping[str, str] = {"yes": ACCEPTED, "no": REJECTED}

#: The orphan file's stem and its columns.
ORPHANS = "orphaned-annotations"
ORPHAN_COLUMNS = ("table", "row_id", *TEST_COLUMNS)

#: The two kinds of a :mod:`~swcraft_tools.scheduling_identity` identity a row
#: stands for: an occurrence (``('instance', path)``) and a declared type with
#: none (``('unplaced-type', Type)``); its third, ``syntactic``, has no row.
_INSTANCE = "instance"
_UNPLACED = "unplaced-type"

#: The identity of a row: one of those two kinds and its path or type name, both strings.
_RowIdentity = tuple[str, str]

#: The SWCraft-owned columns that are not facts: the id is the hash's own key,
#: the declaring file is not a fact the test side reviews, the hash is the hash.
_NOT_FACTS = frozenset({"row_id", "source_ref", "facts_hash"})

#: Each table's SWCraft-owned columns, in file order.
_SWCRAFT_COLUMNS: Mapping[str, tuple[str, ...]] = {
    COMPONENTS: (
        "row_id",
        "component",
        "kind",
        "external",
        "asil_declared",
        "asil_effective",
        "memory",
        "satisfies",
        "occurrences",
        "runnables",
        "description",
        "source_ref",
        "facts_hash",
    ),
    PLACEMENT: (
        "row_id",
        "occurrence",
        "component",
        "placement",
        "tasks",
        "partitions",
        "cores",
        "mapping",
        "source_ref",
        "facts_hash",
    ),
    SCHEDULING: (
        "row_id",
        "occurrence",
        "component",
        "runnable",
        "symbol",
        "mapping",
        "task",
        "task_position",
        "task_period_s",
        "task_priority",
        "partition",
        "core",
        "events",
        "min_start_interval_s",
        "concurrent",
        "wcet",
        "wcet_type",
        "description",
        "source_ref",
        "facts_hash",
    ),
}


def canonical_json(value: object) -> str:
    """*value* as canonical JSON: sorted keys, no spaces, non-ASCII kept as written."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _number(value: float | int) -> str:
    """A held number as ``json.dumps`` writes it: locale-independent, ``0.01`` and ``5``."""
    return json.dumps(value)


def _boolean(value: bool) -> str:
    return "true" if value else "false"


def facts_hash(table: str, revision: int, row_id: str, facts: Mapping[str, str]) -> str:
    """The first 16 hex digits of sha256 over the row's canonical preimage.

    Column **names** are in the preimage, so a renamed column moves every hash, and so does a raised *revision*
    with the values unchanged.
    """
    preimage = canonical_json({"table": table, "revision": revision, "row_id": row_id, "facts": dict(facts)})
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()[:16]


def _columns(name: str) -> tuple[str, ...]:
    """Table *name*'s columns in file order: SWCraft's, the test side's, the review state."""
    return (*_SWCRAFT_COLUMNS[name], *TEST_COLUMNS, REVIEW_STATE)


def _fact_columns(name: str) -> tuple[str, ...]:
    """Table *name*'s SWCraft-owned columns that state the model — the ones :func:`facts_hash` reads."""
    return tuple(column for column in _SWCRAFT_COLUMNS[name] if column not in _NOT_FACTS)


@dataclass(frozen=True)
class Table:
    """One table: its stem, its revision, its columns and its rows (column -> cell text)."""

    name: str
    revision: int
    rows: tuple[Mapping[str, str], ...]

    @property
    def file(self) -> str:
        return f"{self.name}.csv"

    @property
    def columns(self) -> tuple[str, ...]:
        """Every column in file order: SWCraft's, the test side's, the review state."""
        return _columns(self.name)

    @property
    def fact_columns(self) -> tuple[str, ...]:
        """The SWCraft-owned columns that state the model — the ones :func:`facts_hash` reads."""
        return _fact_columns(self.name)


@dataclass(frozen=True)
class TestTables:
    """The three tables, in :data:`TABLE_ORDER`, and the project they describe."""

    __test__ = False  # a pytest collector must not mistake the name for a test class

    tables: tuple[Table, ...]
    #: The manifest's ``name``, which ``manifest.json`` carries as ``project``.
    project: str = ""
    #: The annotations whose row no longer exists, each by :data:`ORPHAN_COLUMNS`,
    #: ordered by table (:data:`TABLE_ORDER`) then ``row_id``.
    orphans: tuple[Mapping[str, str], ...] = ()

    def table(self, name: str) -> Table:
        return next(table for table in self.tables if table.name == name)


def facts_digest(tables: Sequence[Table]) -> str:
    """The full sha256 hex over ``[[table, revision, [[row_id, facts_hash], …]], …]``, in the given order."""
    preimage = canonical_json(
        [[table.name, table.revision, [[row["row_id"], row["facts_hash"]] for row in table.rows]] for table in tables]
    )
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def _finish(name: str, rows: Sequence[dict[str, str]], annotations: Mapping[tuple[str, str], Annotation]) -> Table:
    """Hash every row's facts and join the test side's columns to it (:func:`_review`)."""
    revision, columns, fact_columns = REVISIONS[name], _columns(name), _fact_columns(name)
    finished: list[dict[str, str]] = []
    for row in rows:
        facts = {column: row[column] for column in fact_columns}
        row["facts_hash"] = facts_hash(name, revision, row["row_id"], facts)
        row.update(_review(annotations.get((name, row["row_id"])), row["facts_hash"]))
        finished.append({column: row[column] for column in columns})
    return Table(name=name, revision=revision, rows=tuple(finished))


def _test_cells(annotation: Annotation | None) -> dict[str, str]:
    """The four test-owned cells an annotation is written back as, the pin resolved (:data:`NO_PIN` when none).

    ``accepted`` is written lower-case; a pin on a row whose ``accepted`` is
    empty is ignored and written empty.
    """
    if annotation is None:
        return dict.fromkeys(TEST_COLUMNS, "")
    return {
        "verification_method": annotation.verification_method,
        "comment": annotation.comment,
        "accepted": annotation.accepted,
        "accepted_facts_hash": (annotation.pin or NO_PIN) if annotation.accepted else "",
    }


def _review(annotation: Annotation | None, current: str) -> dict[str, str]:
    """The test-owned cells and the review state of a row whose facts hash is *current*."""
    if annotation is None or not annotation.accepted:
        state = NOT_REVIEWED  # a comment or a method alone accepts nothing
    elif annotation.pin == current:
        state = _ACCEPTED_VALUES[annotation.accepted]
    else:
        state = STALE  # the pinned facts are not the row's, or no facts were pinned
    return {**_test_cells(annotation), REVIEW_STATE: state}


def _disabled_modes(modes: Sequence[DisabledMode]) -> list[dict[str, str]]:
    return [{"port": mode.port, "mode_group_ref": mode.mode_group_ref, "mode_ref": mode.mode_ref} for mode in modes]


def _event_cell(event: TimingEvent | InitEvent | OperationEvent | DataEvent | CallReturnedEvent) -> dict[str, object]:
    """One event as an object of its held fields: an unstated ``name``/``result`` left out, ``period`` as ``period_s``.

    Fields are read by name, never by a dump of the element, so a field the contract adds reaches this table
    only by a change here.
    """
    held: dict[str, object] = {"type": event.type}
    if event.name is not None:
        held["name"] = event.name
    if isinstance(event, TimingEvent):
        held["period_s"] = event.period
        held["disabled_modes"] = _disabled_modes(event.disabled_modes)
    elif isinstance(event, OperationEvent):
        held["port"] = event.port
        held["operation"] = event.operation
    elif isinstance(event, DataEvent):
        held["port"] = event.port
        held["element"] = event.element
        held["disabled_modes"] = _disabled_modes(event.disabled_modes)
    elif isinstance(event, CallReturnedEvent):
        held["port"] = event.port
        held["operation"] = event.operation
        if event.result is not None:
            held["result"] = event.result
    return held


@dataclass(frozen=True)
class _Slot:
    """Where a resolved runnable is mapped: the task, its 1-based entry position, and its placement."""

    task: str
    position: int
    period_s: str
    priority: str
    partition: str
    core: str


def _row_id_of(identity: Identity) -> str:
    kind, value = identity
    return f"occurrence:{value}" if kind == _INSTANCE else f"unplaced:{value}"


def _source_refs(model_root: str, components: Iterable[str]) -> dict[str, str]:
    """Each component's ``source_ref``: the first file declaring it, model-root-relative in ``/`` form, else empty."""
    root = Path(model_root)
    stored = stored_element_files(root, "swc", registry=current_registry())
    refs: dict[str, str] = {}
    for component in components:
        files = stored.get(component, ())
        refs[component] = files[0].path.relative_to(root).as_posix() if files else ""
    return refs


def _placed(
    identities: SchedulingIdentities, swcs: Mapping[str, SWComponent]
) -> tuple[list[tuple[_RowIdentity, str]], dict[str, int]]:
    """The occurrences (uncapped) and the declared types with none, in identity order, and each type's count."""
    occurrences = [(path, component) for path, component in identities.occurrences() if component in swcs]
    counts: dict[str, int] = {}
    for _, component in occurrences:
        counts[component] = counts.get(component, 0) + 1
    placed: list[tuple[_RowIdentity, str]] = [((_INSTANCE, path), component) for path, component in occurrences]
    placed += [((_UNPLACED, name), name) for name in swcs if name not in counts]
    placed.sort(key=lambda entry: identity_sort_key(entry[0]))
    return placed, counts


def _slots(
    model_root: str, identities: SchedulingIdentities, hardware: Mapping[str, Hardware]
) -> dict[tuple[Identity, str], _Slot]:
    """Every task entry, resolved as every consumer resolves it; the first task wins a runnable named twice."""
    slots: dict[tuple[Identity, str], _Slot] = {}
    for task in read_layer(model_root, "tasks"):
        core = core_of(task, hardware)
        for position, entry in enumerate(task.runnables, start=1):
            resolution = identities.resolve(entry.swc, entry.instance)
            if resolution.refusal is not None or resolution.identity[0] not in (_INSTANCE, _UNPLACED):
                continue
            slots.setdefault(
                (resolution.identity, entry.runnable),
                _Slot(
                    task=task.name,
                    position=position,
                    period_s=_number(task.period),
                    priority=_number(task.priority),
                    partition=task.partition,
                    core=core.name if core is not None else "",
                ),
            )
    return slots


def _component_rows(
    swcs: Mapping[str, SWComponent],
    effective: Mapping[str, str],
    counts: Mapping[str, int],
    refs: Mapping[str, str],
) -> list[dict[str, str]]:
    """``components.csv``'s rows, by type name, before :func:`_finish`."""
    return [
        {
            "row_id": f"component:{name}",
            "component": name,
            "kind": swc.type,
            "external": _boolean(swc.external),
            "asil_declared": swc.asil or "",
            "asil_effective": effective[name],
            "memory": canonical_json(swc.memory),
            "satisfies": canonical_json(swc.satisfies),
            "occurrences": _number(counts.get(name, 0)),
            "runnables": _number(len(swc.runnables)),
            "description": swc.description,
            "source_ref": refs[name],
        }
        for name, swc in sorted(swcs.items())
    ]


def _runnable_slots(
    identity: Identity, swc: SWComponent, slots: Mapping[tuple[Identity, str], _Slot]
) -> list[tuple[Runnable, _Slot | None]]:
    """The type's runnables by name, each with the slot it is mapped to at *identity*, or ``None``."""
    return [
        (runnable, slots.get((identity, runnable.name)))
        for runnable in sorted(swc.runnables, key=lambda runnable: runnable.name)
    ]


def _placement_rows(
    placed: Sequence[tuple[_RowIdentity, str]],
    swcs: Mapping[str, SWComponent],
    slots: Mapping[tuple[Identity, str], _Slot],
    refs: Mapping[str, str],
) -> list[dict[str, str]]:
    """``component-placement.csv``'s rows, in identity order, before :func:`_finish`."""
    rows: list[dict[str, str]] = []
    for identity, component in placed:
        kind, value = identity
        runnables = _runnable_slots(identity, swcs[component], slots)
        mapped = [slot for _, slot in runnables if slot is not None]
        if not runnables:
            mapping = "no-runnables"
        elif len(mapped) == len(runnables):
            mapping = "all-mapped"
        elif mapped:
            mapping = "partly-mapped"
        else:
            mapping = "not-mapped"
        rows.append(
            {
                "row_id": _row_id_of(identity),
                "occurrence": value if kind == _INSTANCE else "",
                "component": component,
                "placement": "in-composition" if kind == _INSTANCE else "not-in-composition",
                "tasks": canonical_json(sorted({slot.task for slot in mapped})),
                "partitions": canonical_json(sorted({slot.partition for slot in mapped})),
                "cores": canonical_json(sorted({slot.core for slot in mapped if slot.core})),
                "mapping": mapping,
                "source_ref": refs[component],
            }
        )
    return rows


def _scheduling_rows(
    placed: Sequence[tuple[_RowIdentity, str]],
    swcs: Mapping[str, SWComponent],
    slots: Mapping[tuple[Identity, str], _Slot],
    refs: Mapping[str, str],
) -> list[dict[str, str]]:
    """``runnable-scheduling.csv``'s rows, in identity order then by runnable name, before :func:`_finish`."""
    rows: list[dict[str, str]] = []
    for identity, component in placed:
        kind, value = identity
        for runnable, slot in _runnable_slots(identity, swcs[component], slots):
            rows.append(
                {
                    "row_id": f"{_row_id_of(identity)}:{runnable.name}",
                    "occurrence": value if kind == _INSTANCE else "",
                    "component": component,
                    "runnable": runnable.name,
                    "symbol": runnable.symbol or "",
                    "mapping": "mapped" if slot is not None else "not-mapped",
                    "task": slot.task if slot is not None else "",
                    "task_position": _number(slot.position) if slot is not None else "",
                    "task_period_s": slot.period_s if slot is not None else "",
                    "task_priority": slot.priority if slot is not None else "",
                    "partition": slot.partition if slot is not None else "",
                    "core": slot.core if slot is not None else "",
                    "events": canonical_json([_event_cell(event) for event in runnable.events]),
                    "min_start_interval_s": _number(runnable.min_start_interval),
                    "concurrent": _boolean(runnable.concurrent),
                    "wcet": runnable.wcet or "",
                    "wcet_type": runnable.wcet_type or "",
                    "description": runnable.description,
                    "source_ref": refs[component],
                }
            )
    return rows


def _orphans(tables: Sequence[Table], joined: Mapping[tuple[str, str], Annotation]) -> tuple[Mapping[str, str], ...]:
    """The annotations whose row no longer exists, by table (:data:`TABLE_ORDER`) then ``row_id``."""
    current = {(table.name, row["row_id"]) for table in tables for row in table.rows}
    return tuple(
        {"table": key[0], "row_id": key[1], **_test_cells(joined[key])}
        for key in sorted(joined, key=lambda key: (TABLE_ORDER.index(key[0]), key[1]))
        if key not in current
    )


def build_test_tables(model_root: str, project_name: str, annotations: Annotations | None = None) -> TestTables:
    """The three tables over the model under *model_root*, built in memory.

    *project_name* is the manifest's ``name``, carried for ``manifest.json``. *annotations*, when given, are
    joined to the rows by ``(table, row_id)``; those that match no row become :attr:`TestTables.orphans`.
    """
    swcs: dict[str, SWComponent] = {}
    for swc in read_layer(model_root, "swc"):
        swcs.setdefault(swc.name, swc)
    effective = effective_asil_levels(list(swcs.values()), read_layer(model_root, "requirements"))
    hardware = hardware_by_name(read_layer(model_root, "hardware"))
    identities = identities_for(model_root)
    refs = _source_refs(model_root, swcs)
    placed, counts = _placed(identities, swcs)
    slots = _slots(model_root, identities, hardware)

    joined = annotations.entries if annotations is not None else {}
    tables = (
        _finish(COMPONENTS, _component_rows(swcs, effective, counts, refs), joined),
        _finish(PLACEMENT, _placement_rows(placed, swcs, slots, refs), joined),
        _finish(SCHEDULING, _scheduling_rows(placed, swcs, slots, refs), joined),
    )
    return TestTables(tables=tables, project=project_name, orphans=_orphans(tables, joined))


# ---------------------------------------------------------------------------
# The CSV text: every field quoted, every record ending in "\n"
# ---------------------------------------------------------------------------


def _csv_text(columns: Sequence[str], rows: Iterable[Mapping[str, str]]) -> str:
    """A header and *rows* as CSV text: ``QUOTE_ALL``, records ended by ``\\n``, a field's own line ends kept.

    ``QUOTE_ALL`` because ``QUOTE_MINIMAL`` with a ``\\n`` terminator leaves a lone ``\\r`` unquoted, which a
    reader takes as a record end. Write the text without newline translation (``newline="\\n"``).
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row[column] for column in columns])
    return buffer.getvalue()


def table_csv(table: Table) -> str:
    """*table* as its CSV file's text."""
    return _csv_text(table.columns, table.rows)


def orphans_csv(orphans: Iterable[Mapping[str, str]]) -> str:
    """The orphaned annotations as ``orphaned-annotations.csv``'s text (the header alone when there are none)."""
    return _csv_text(ORPHAN_COLUMNS, orphans)


# ---------------------------------------------------------------------------
# The test side's directory: read, never written
# ---------------------------------------------------------------------------

#: The largest field the reader accepts: a C ``long``'s maximum on Windows too
#: (``sys.maxsize`` overflows there). The default 131 072 characters would refuse
#: SWCraft's own export handed back, since a description has no length limit.
FIELD_SIZE_LIMIT = 2**31 - 1

#: The file names the directory may hold, each to its table (the orphan file to ``None``).
_ANNOTATION_FILES: Mapping[str, str | None] = {
    **{f"{name}.csv": name for name in TABLE_ORDER},
    f"{ORPHANS}.csv": None,
}


class AnnotationsError(Exception):
    """An annotations directory or file refused: the message names the file as given, its line and column."""


@dataclass(frozen=True)
class Annotation:
    """The test side's columns of one row, as read: ``accepted`` normalised, the pin resolved.

    *pin* is the facts hash the acceptance refers to (``accepted_facts_hash`` when filled, else the same file's
    ``facts_hash`` when ``accepted`` is set, spaces stripped), or ``None`` when ``accepted`` is empty or no hash
    was found.
    """

    table: str
    row_id: str
    verification_method: str
    comment: str
    accepted: str
    pin: str | None
    file: str
    line: int


@dataclass(frozen=True)
class Annotations:
    """Every annotation read, by ``(table, row_id)``, and the directory's files that name no table."""

    entries: Mapping[tuple[str, str], Annotation] = field(default_factory=dict)
    unknown_files: tuple[str, ...] = ()


def _where(file: str, line: int, column: int | None = None) -> str:
    return f"{file}: line {line}" + (f", column {column}" if column is not None else "")


def _decode(file: str, data: bytes) -> str:
    """*data* as UTF-8 text, a leading byte order mark skipped; a non-UTF-8 byte refused at its line and column."""
    # The mark is stripped before decoding, so the error's offset indexes the same bytes as the count below.
    data = data.removeprefix(b"\xef\xbb\xbf")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        before = data[: error.start]
        line = before.count(b"\n") + 1
        column = error.start - (before.rfind(b"\n") + 1) + 1
        raise AnnotationsError(f"{_where(file, line, column)}: the file is not UTF-8 text") from None


def _records(file: str, text: str) -> Iterator[tuple[int, list[str]]]:
    """Each CSV record of *text* with the line it starts on (``\\r\\n`` and ``\\n`` records alike).

    The whole text is parsed under :data:`FIELD_SIZE_LIMIT` before the first record is returned, and Python's
    process-wide ``csv.field_size_limit`` is put back, whether the parse ends or raises.
    """
    found: list[tuple[int, list[str]]] = []
    previous = csv.field_size_limit(FIELD_SIZE_LIMIT)
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        start = 1
        try:
            for record in reader:
                found.append((start, record))
                start = reader.line_num + 1
        except csv.Error as error:
            raise AnnotationsError(f"{_where(file, start)}: not a readable CSV record ({error})") from None
    finally:
        csv.field_size_limit(previous)
    return iter(found)


def _read_file(file: str, data: bytes, table: str | None) -> list[Annotation]:
    """The annotations of one file: a table's (*table*) or the orphan file's (``None``)."""
    records = _records(file, _decode(file, data))
    header_line, header = next(records, (1, []))
    columns: dict[str, int] = {}
    for index, name in enumerate(column.strip() for column in header):
        if name and name in columns:
            raise AnnotationsError(
                f"{_where(file, header_line, index + 1)}: the header names the column '{name}' twice"
                f" (first in column {columns[name] + 1})"
            )
        if name:
            columns[name] = index
    for name in ("row_id",) if table is not None else ("table", "row_id"):
        if name not in columns:
            raise AnnotationsError(f"{_where(file, header_line)}: the header has no '{name}' column")

    def cell(record: list[str], name: str) -> str:
        index = columns.get(name)
        return record[index] if index is not None and index < len(record) else ""

    found: list[Annotation] = []
    seen: dict[tuple[str, str], int] = {}
    for line, record in records:
        if not any(value.strip() for value in record):
            continue  # a spreadsheet's trailing empty row
        row_id = cell(record, "row_id").strip()
        if not row_id:
            raise AnnotationsError(f"{_where(file, line, columns['row_id'] + 1)}: the row has no row_id")
        row_table = table
        if row_table is None:
            named = cell(record, "table").strip()
            if named not in TABLE_ORDER:
                raise AnnotationsError(
                    f"{_where(file, line, columns['table'] + 1)}: '{named}' names no table"
                    f" (one of {', '.join(TABLE_ORDER)})"
                )
            row_table = named
        if not any(cell(record, name).strip() for name in TEST_COLUMNS):
            continue  # four empty test-owned cells: a generated row nobody annotated, not an annotation
        key = (row_table, row_id)
        if key in seen:
            raise AnnotationsError(
                f"{_where(file, line, columns['row_id'] + 1)}: the row_id '{row_id}' is repeated"
                f" (first on line {seen[key]})"
            )
        seen[key] = line
        accepted = cell(record, "accepted").strip().lower()
        if accepted and accepted not in _ACCEPTED_VALUES:
            raise AnnotationsError(
                f"{_where(file, line, columns['accepted'] + 1)}: accepted is"
                f" '{cell(record, 'accepted')}'; write yes, no or leave it empty"
            )
        pinned = cell(record, "accepted_facts_hash").strip() or cell(record, "facts_hash").strip()
        pin = (pinned or None) if accepted else None
        found.append(
            Annotation(
                table=row_table,
                row_id=row_id,
                verification_method=cell(record, "verification_method"),
                comment=cell(record, "comment"),
                accepted=accepted,
                pin=pin,
                file=file,
                line=line,
            )
        )
    return found


def read_annotations(directory: str | os.PathLike[str]) -> Annotations:
    """The test side's annotations under *directory*: read, never written.

    Each of the three table files and ``orphaned-annotations.csv`` the directory holds is read as UTF-8 (a byte
    order mark skipped), its columns by header name: ``row_id`` required (and ``table`` in the orphan file);
    ``verification_method``, ``comment``, ``accepted``, ``accepted_facts_hash`` and ``facts_hash`` read; every
    other column ignored, so a handed-back generated table reads as well as a slim file of the test side's own.
    Lenient where a spreadsheet differs harmlessly: ``\\r\\n`` records, spaces around ``row_id``, ``accepted``,
    ``accepted_facts_hash`` and ``facts_hash``, ``Yes`` for ``yes``, rows whose every cell is empty. A row whose
    four test-owned cells are all empty (spaces counting as empty) is no annotation: it is neither joined nor
    carried as an orphan, and is skipped before the repeated-``row_id`` check.

    Refused with :class:`AnnotationsError`, naming the file as *directory* gives it plus line and column: a
    directory that does not exist, a table file without ``row_id``, a header naming a column twice, a row with an
    empty ``row_id`` and another cell filled, a repeated ``row_id`` (within a file, or the same ``(table, row_id)``
    in a table file and the orphan file), an ``accepted`` other than ``yes``/``no``/empty, a file that is not
    UTF-8. ``manifest.json`` is expected and not read; every other entry of the directory is listed in
    :attr:`Annotations.unknown_files` and not read.

    What it does not reach: where the directory lies (:func:`annotations_location_refusal`'s); a ``row_id`` a
    spreadsheet rewrote (it matches no row and becomes an orphan); a value in ``accepted_facts_hash`` that is no
    hash (it matches no row's facts, so the row is ``stale``).
    """
    root = Path(directory)
    if not root.is_dir():
        raise AnnotationsError(f"{os.fspath(directory)}: the annotations directory does not exist")
    entries: dict[tuple[str, str], Annotation] = {}
    unknown: list[str] = []
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if entry.name not in _ANNOTATION_FILES:
            if entry.name != MANIFEST_FILE:  # a handed-back bundle's own manifest: expected, not read
                unknown.append(entry.name)
            continue
        file = os.path.join(os.fspath(directory), entry.name)
        try:
            data = entry.read_bytes()
        except OSError as error:
            raise AnnotationsError(f"{file}: the file cannot be read ({error.strerror or error})") from None
        for annotation in _read_file(file, data, _ANNOTATION_FILES[entry.name]):
            key = (annotation.table, annotation.row_id)
            if key in entries:
                first = entries[key]
                raise AnnotationsError(
                    f"{_where(annotation.file, annotation.line)}: the row_id '{annotation.row_id}' of"
                    f" {annotation.table} is annotated twice (also {_where(first.file, first.line)})"
                )
            entries[key] = annotation
    return Annotations(entries=entries, unknown_files=tuple(unknown))


def annotations_location_refusal(directory: Path, *, output_directory: Path, roots: Mapping[str, Path]) -> str | None:
    """Why the annotations *directory* may not be used where it lies, or ``None``.

    Every path is resolved first, so a link is judged by where it points. The directory may not equal, contain or
    lie inside *output_directory* (the bundle would be written over the test side's files), nor equal or lie
    inside any of *roots*, given by the name the refusal states: ``model_root`` (it would sit among the model's
    layers), ``dist_root`` and ``views_root`` (``clean-outputs`` removes them). **Compared case-insensitively**
    on every platform, failing closed: a pair distinct on a case-sensitive filesystem is refused too. What it
    does not reach: a filesystem that folds case otherwise than ``str.casefold`` does, and two spellings of one
    place only a filesystem probe would equate (a hard link or a bind mount).
    """
    resolved = resolved_path(directory)
    if paths_overlap(resolved, resolved_path(output_directory)):
        return "the annotations directory overlaps the test tables' output directory"
    for name, root in roots.items():
        if path_contains(resolved_path(root).casefold(), resolved.casefold()):
            return f"the annotations directory lies inside {name}"
    return None


# ---------------------------------------------------------------------------
# The bundle and the command's run
# ---------------------------------------------------------------------------

#: What an empty cell and an empty collection mean, stated once in ``manifest.json``.
EMPTY_CELL = (
    "An empty cell means 'not declared in the model' and nothing else; a collection is always written, [] or {} "
    "when it declares no member; what is unmapped is a status column's explicit value, never an empty cell."
)

#: Each table's row-id rule, in words, for ``manifest.json``.
_ROW_ID_RULES: Mapping[str, str] = {
    COMPONENTS: "component:<Type>, one row per component type",
    PLACEMENT: (
        "occurrence:<path> for each occurrence reachable from the root composition (member names joined by /), "
        "unplaced:<Type> for a type with no occurrence"
    ),
    SCHEDULING: (
        "occurrence:<path>:<runnable> for each runnable of an occurrence, unplaced:<Type>:<runnable> for each "
        "runnable of a type with no occurrence"
    ),
}

#: What each column means, units included, for ``manifest.json``; the owner is decided by the column's place.
_MEANINGS: Mapping[str, str] = {
    "row_id": "The row's stable identity: names only, never a task, period, partition, core, file or comment.",
    "component": "The component type's name.",
    "kind": "The component type's kind: application, service or ecu_abstraction.",
    "external": "Whether the component is declared external: true or false.",
    "asil_declared": "The safety level the component declares; empty when it declares none.",
    "asil_effective": (
        "The effective safety level: the declared one, else the highest level of the requirements allocated to "
        "the component; QM when nothing is allocated."
    ),
    "memory": "The component's declared memory map, memory name to bytes, as a JSON object.",
    "satisfies": "The requirement links the component declares, as written, as a JSON list.",
    "occurrences": "How many occurrences of the type the root composition reaches.",
    "runnables": "How many runnables the type declares.",
    "description": "The description as written in the model.",
    "source_ref": "The file declaring the component type, relative to model_root (not a fact; not hashed).",
    "facts_hash": "The first 16 hex digits of sha256 over the row's table, revision, row_id and fact columns.",
    "occurrence": "The occurrence's path, member names joined by /; empty when the type has no occurrence.",
    "placement": "in-composition when the root composition reaches the occurrence, not-in-composition otherwise.",
    "tasks": "The distinct tasks the occurrence's runnables are mapped to, sorted, as a JSON list.",
    "partitions": "The distinct partitions of those tasks, sorted, as a JSON list.",
    "cores": "The distinct cores of those partitions, sorted, as a JSON list.",
    "mapping": ("Placement: all-mapped, partly-mapped, not-mapped or no-runnables; scheduling: mapped or not-mapped."),
    "runnable": "The runnable's name.",
    "symbol": "The runnable's declared entry-point symbol; empty when none is declared (the symbol is then the name).",
    "task": "The task the runnable is mapped to; empty when not mapped.",
    "task_position": (
        "The runnable's 1-based place among the task's entries, the declared order: never an execution order."
    ),
    "task_period_s": "The task's period in seconds.",
    "task_priority": "The task's priority.",
    "partition": "The task's partition.",
    "core": "The core the task's partition is placed on; empty when none is declared.",
    "events": "The runnable's events, each an object of its declared fields (periods as period_s, in seconds).",
    "min_start_interval_s": "The runnable's minimum start interval in seconds.",
    "concurrent": "Whether the runnable may run concurrently with itself: true or false.",
    "wcet": "The runnable's execution-time budget as written (for example 400us): a budget, not a measurement.",
    "wcet_type": "Whether the budget is gross or net, as declared; empty when no budget is declared.",
    "verification_method": "The test side's: how the row is verified. SWCraft carries it, never writes it.",
    "comment": "The test side's comment. SWCraft carries it, never writes it.",
    "accepted": "The test side's verdict: yes, no or empty. A comment or a method alone accepts nothing.",
    "accepted_facts_hash": (
        "The facts_hash the verdict refers to; none when no facts could be found. Clear it to accept the current facts."
    ),
    "review_state": (
        "Computed by SWCraft: not-reviewed, accepted, rejected, or stale when the verdict refers to other facts."
    ),
    "table": "The table the orphaned annotation was written for.",
}

#: The review states in the order the printed counts and the manifest give them.
_STATE_ORDER = (ACCEPTED, STALE, REJECTED, NOT_REVIEWED)


def _column_entries(columns: Sequence[str]) -> list[dict[str, str]]:
    return [
        {"name": column, "owner": "test" if column in TEST_COLUMNS else "swcraft", "meaning": _MEANINGS[column]}
        for column in columns
    ]


def review_counts(table: Table) -> dict[str, int]:
    """How many rows of *table* are in each review state, in the printed order."""
    counts = dict.fromkeys(_STATE_ORDER, 0)
    for row in table.rows:
        counts[row[REVIEW_STATE]] += 1
    return counts


def manifest_json(tables: TestTables) -> str:
    """``manifest.json``'s text: versions, the project, the facts digest and every table's columns.

    No timestamp, no host path and no annotations path: the same model and the same annotations give the same
    bytes.
    """
    document = {
        "schema_version": SCHEMA_VERSION,
        "swcraft_version": __version__,
        "contract_version": current_registry().contract_version,
        "project": tables.project,
        "facts_digest": facts_digest(tables.tables),
        "empty_cell": EMPTY_CELL,
        "tables": [
            {
                "file": table.file,
                "revision": table.revision,
                "rows": len(table.rows),
                "row_id": _ROW_ID_RULES[table.name],
                "review_states": review_counts(table),
                "fact_columns": list(table.fact_columns),
                "columns": _column_entries(table.columns),
            }
            for table in tables.tables
        ],
        "orphaned_annotations": {
            "file": f"{ORPHANS}.csv",
            "rows": len(tables.orphans),
            "columns": _column_entries(ORPHAN_COLUMNS),
        },
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def bundle_files(tables: TestTables) -> list[tuple[str, str]]:
    """The bundle's five ``(file name, text)`` pairs: the manifest, the three tables, the orphan file."""
    return [
        (MANIFEST_FILE, manifest_json(tables)),
        *((table.file, table_csv(table)) for table in tables.tables),
        (f"{ORPHANS}.csv", orphans_csv(tables.orphans)),
    ]


class TablesRefusedError(Exception):
    """The run is refused before anything is written; the message is the one ``Error:`` sentence."""


class TablesPartlyReplacedError(Exception):
    """A rename failed after an earlier file of the bundle had been replaced: the bundle is mixed."""


@dataclass(frozen=True)
class GeneratedTestTables:
    """What a run wrote: the directory, the tables, and what the annotations directory held besides them."""

    directory: Path
    tables: TestTables
    annotations_read: bool
    unknown_files: tuple[str, ...] = ()


def _write_bundle(directory: Path, files: Sequence[tuple[str, str]]) -> None:
    """Write *files* through ``write_report_files``, telling a failure that replaced nothing from a mixed bundle.

    A failure is a partly replaced bundle exactly when a file was renamed into place before it, counted, never
    inferred from the files on disk. Raises ``OSError`` when nothing was replaced,
    :class:`TablesPartlyReplacedError` otherwise.
    """
    replaced: list[Path] = []
    try:
        write_report_files(directory, [(directory / name, text) for name, text in files], after_replace=replaced.append)
    except OSError as error:
        if replaced:
            raise TablesPartlyReplacedError(error.strerror or str(error)) from error
        raise


def generate_test_tables(
    project: SwcraftProject,
    annotations_dir: str | None = None,
    *,
    manifest_records: Sequence[ConsistencyIssue] = (),
) -> GeneratedTestTables:
    """``swcraft generate test-tables``: refuse, read, build in memory, then write the bundle.

    In order: the output directory ``<dist_root>/test-tables`` must resolve below the resolved ``dist_root`` (a
    link taking it elsewhere is refused); the model is checked as ``check-model`` checks it, with
    *manifest_records* (the manifest's own findings) placed first, and an error finding refuses, a warning
    nothing; the annotations directory, when given, is placed (:func:`annotations_location_refusal`) and only
    then read (:func:`read_annotations`); the tables are built and the five files written. Every refusal is a
    :class:`TablesRefusedError` (or an :class:`AnnotationsError`) raised before anything is written.

    What it does not reach: a failure before the first rename replaces nothing and may leave the directory it
    created, empty; a rename refused after another file was renamed leaves a mixed bundle, raised as
    :class:`TablesPartlyReplacedError`; a link created between the check and the write; two concurrent runs.
    """
    dist = resolved_path(Path(project.resolved_dist_root))
    try:
        directory = experiment_default_directory(project, REPORT_DIRECTORY)
    except ValueError as error:
        raise TablesRefusedError(str(error)) from None
    if not path_contains(dist, str(directory)):
        raise TablesRefusedError(f"the report directory '<dist_root>/{REPORT_DIRECTORY}' resolves outside 'dist_root'")

    # Judged at the manifest's version, so the tables refuse exactly the model check-model refuses.
    result = with_records(
        check_cross_layer_consistency(project.resolved_model_root, project.resolved_arxml_version),
        list(manifest_records),
    )
    if result.issues:
        count = len(result.issues)
        raise TablesRefusedError(
            f"the model is refused: check-model reports {count} error{'' if count == 1 else 's'} "
            "(run swcraft check-model to see them), and the test tables are generated only from a model the gate "
            "passes"
        )

    annotations: Annotations | None = None
    if annotations_dir is not None:
        refusal = annotations_location_refusal(
            Path(annotations_dir),
            output_directory=directory,
            roots={
                "model_root": Path(project.resolved_model_root),
                "dist_root": Path(dist),
                "views_root": Path(project.resolved_views_root),
            },
        )
        if refusal is not None:
            raise TablesRefusedError(f"{annotations_dir}: {refusal}")
        annotations = read_annotations(annotations_dir)

    tables = build_test_tables(project.resolved_model_root, project.name, annotations)
    _write_bundle(directory, bundle_files(tables))
    return GeneratedTestTables(
        directory=directory,
        tables=tables,
        annotations_read=annotations is not None,
        unknown_files=annotations.unknown_files if annotations is not None else (),
    )
