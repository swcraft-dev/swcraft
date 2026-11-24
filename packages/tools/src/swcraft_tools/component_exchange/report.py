"""Component-exchange reports.

:class:`ComponentExportReport` and :class:`ComponentImportReport`. Each export report maps 1:1
onto one ``export-summary.json`` entry.

``export-summary.json`` schema (fixed, deterministic): a JSON list with one object per
exportable SWC (never a stub), sorted by SWC name, in this key order:

- ``swc`` -- the SWC's ``name``
- ``output_path`` -- the written ARXML **relative to the output dir** (so the summary is
  byte-identical across locations); ``null`` when the export failed
- ``status`` -- ``"ok"`` or ``"failed"``
- ``interfaces`` -- name-sorted interfaces of the closure (empty on failure)
- ``data_types`` -- name-sorted transitive data-type closure (empty on failure)
- ``error`` -- the failure message, ``null`` on success

Rendered with ``json.dumps(..., indent=2)`` plus a trailing newline; byte-level determinism is
part of the contract. :class:`ComponentImportPlan` is the planner's result.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ..arxml_import.merge import ELEMENT_LAYERS
from ..arxml_import.report import (
    NATIVE_DECLARATION_MISSING_HEADING,
    PLACEMENT_WHAT,
    QUEUE_LENGTH_HEADING,
    QUEUE_LENGTH_HEADING_TAIL,
    REPRESENTED_HEADING,
    UNREAD_PHYSICS_HEADING,
    file_suffix_heading,
    package_placement_heading,
)
from ..markdown_cells import heading_text, paragraph_start
from .capabilities import CAPABILITIES
from .report_lines import NONE, ReportLine, ReportList

if TYPE_CHECKING:
    from .dependencies import MissingReference

EXPORT_SUMMARY_FILENAME = "export-summary.json"

STATUS_OK = "ok"
STATUS_FAILED = "failed"

#: The import report's first line up to its source: the text form's title, and the Markdown
#: form's heading with its colon escaped. The VS Code extension reads it (``linkCore.ts``).
IMPORT_REPORT_TITLE = "Component import report: "


@dataclass
class ComponentExportReport:
    """Per-SWC result of one component export.

    ``interfaces``/``data_types`` carry the closure's **names**, sorted, not model objects.

    What it does not reach: the closure also carries the component's ``mapping_sets``, each
    narrowed to the closure, and which maps were dropped is visible in the exported file only,
    never in this report or an MCP reply. The import side names them
    (``created_mapping_sets`` / ``extended_mapping_sets`` / ``reused_mapping_sets``).
    """

    swc: str
    output_path: str | None
    status: str
    interfaces: list[str] = field(default_factory=list)
    data_types: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def render(self) -> str:
        """The report as text."""
        lines = [f"Component export report: {self.swc}"]
        lines.append(f"STATUS {self.status}")
        if self.output_path is not None:
            lines.append(f"OUTPUT {self.output_path}")
        lines.append(f"EXPORTED {len(self.interfaces)} interface(s)")
        for name in self.interfaces:
            lines.append(f"  - {name}")
        lines.append(f"EXPORTED {len(self.data_types)} data type(s)")
        for name in self.data_types:
            lines.append(f"  - {name}")
        if self.error is not None:
            lines.append(f"ERROR {self.error}")
        return "\n".join(lines)


#: The heading of :attr:`ComponentImportReport.descriptions_not_taken_lines`.
DESCRIPTIONS_NOT_TAKEN_HEADING = "Descriptions not taken from the file"
#: The heading of :attr:`ComponentImportReport.not_carried_lines`.
NOT_CARRIED_HEADING = "Not carried from the file"
NOT_CARRIED_HEADING_TAIL = (
    "descriptive values SWCraft does not carry -- documentation, labels, com-spec values and names SWCraft "
    "generates itself; the import is not refused for them"
)
#: The heading of :attr:`ComponentImportReport.missing_references`, and the sentence naming
#: what a verdict withholds (runnables, and the mapping sets nothing answers).
MISSING_REFERENCES_HEADING = "References the project does not have"
MISSING_REFERENCES_HEADING_TAIL = (
    "the component's runnables and any mapping set listed below are withheld; "
    "its ports, interfaces, description and resolved mapping sets are written"
)
#: The same block's sentence when the ``--update`` overlay ran: the file's runnables are
#: withheld and the project's own runnables and mapping sets are kept.
MISSING_REFERENCES_KEPT_TAIL = (
    "the file's runnables are withheld; the project's own behaviour of the component, its runnables and "
    "mapping sets, is kept and the file's ports, interfaces and description are written over it"
)
#: The same block's sentence when the overlay ran and the component is ``unchanged``: nothing
#: of it is written, so :data:`MISSING_REFERENCES_KEPT_TAIL`'s "written over it" would be false.
MISSING_REFERENCES_UNCHANGED_TAIL = (
    "the file's runnables are withheld; the project's component, its runnables and mapping sets included, "
    "is kept as it stands and nothing of it is written"
)
#: The mark a created or reused interface carries in its list when it is an external stub.
STUB_INTERFACE_MARK = "(external interface stub)"
DESCRIPTIONS_NOT_TAKEN_HEADING_TAIL = (
    "not supported by SWCraft -- a shared definition the target already holds is reused as it stands, "
    "so the file's text is written nowhere"
)
#: The heading of :attr:`ComponentImportReport.async_without_result_lines`, and what the import
#: did about them.
ASYNC_WITHOUT_RESULT_HEADING = "ASYNCHRONOUS CALLS WITHOUT A RESULT"
ASYNC_WITHOUT_RESULT_HEADING_TAIL = (
    "the project generates AUTOSAR 4.2.2, and `check-model` refuses each until it is answered; "
    "the import is not refused for them"
)
#: The heading of :attr:`ComponentImportReport.async_call_finding_lines`.
ASYNC_CALL_FINDINGS_HEADING = "ASYNCHRONOUS CALL FINDINGS"
ASYNC_CALL_FINDINGS_HEADING_TAIL = (
    "`check-model` refuses each, at either AUTOSAR version, until it is answered; the import is not refused for them"
)
#: The heading of :attr:`ComponentImportReport.implicit_access_finding_lines`.
IMPLICIT_READ_FINDINGS_HEADING = "IMPLICIT ACCESS FINDINGS"
IMPLICIT_READ_FINDINGS_HEADING_TAIL = ASYNC_CALL_FINDINGS_HEADING_TAIL


@dataclass
class ComponentImportReport:
    """Result of one conflict-free :func:`~swcraft_tools.component_exchange.import_.import_component` call.

    ``swc_action`` is ``"created"``, ``"unchanged"`` (identical, a no-op) or ``"updated"``
    (replaced via ``update=True``; ``swc_diff_preview`` carries the mandatory diff).
    ``extended_mapping_sets`` names every pair added to a reused mapping set, with the other target
    components naming the set.

    Out-of-scope content is a ``skip_lines`` entry; a descriptive value a :attr:`not_carried_lines`
    line; a behaviour reference nothing answers a :attr:`missing_references` line, the component then
    written without its ``runnables``. ``file_suffix_lines`` is filled by ``import_component`` only.

    ``preserved_*`` record what an ``"updated"`` replacement carried from the target because the
    export cannot state it or is silent (memory, ASIL, ``handle_termination_and_restart``, also on
    ``"unchanged"``, descriptions, ``wcet``, measures, links). Each ``*_regime`` names the signal that
    decided a capability family: ``marked``, ``replace-flag``, ``carried``, ``stated`` or ``none``.
    """

    source: str
    dry_run: bool
    swc: str
    swc_action: str
    #: The ``--update`` overlay ran: the project's own behaviour of the component was kept under
    #: the file's descriptive part (never on a create or over an ``external: true`` stub). It
    #: picks the sentence of :attr:`missing_references`' heading.
    behaviour_kept: bool = False
    swc_diff_preview: str = ""
    preserved_memory: dict[str, int] = field(default_factory=dict)
    preserved_asil: str | None = None
    preserved_handle_termination_and_restart: str | None = None
    preserved_measures: dict[str, dict[str, str]] = field(default_factory=dict)
    dropped_measures: list[str] = field(default_factory=list)
    #: The target's trace links carried across a replacement, per site, and one line per site
    #: the export removed, whose links went with it.
    preserved_links: dict[str, list[str]] = field(default_factory=dict)
    dropped_links: list[str] = field(default_factory=list)
    preserved_descriptions: list[str] = field(default_factory=list)
    preserved_costs: dict[str, str] = field(default_factory=dict)
    preserved_access_lists: dict[str, dict[str, int]] = field(default_factory=dict)
    access_list_regime: str = NONE
    access_marker_value: str | None = None
    created_interfaces: list[str] = field(default_factory=list)
    reused_interfaces: list[str] = field(default_factory=list)
    stub_interfaces: list[str] = field(default_factory=list)
    created_data_types: list[str] = field(default_factory=list)
    reused_data_types: list[str] = field(default_factory=list)
    #: The units, compu methods and data constraints created and reused, by layer, each list in
    #: the export's source-path order. A layer the exchange did not touch has no key.
    created_elements: dict[str, list[str]] = field(default_factory=dict)
    reused_elements: dict[str, list[str]] = field(default_factory=dict)
    created_mapping_sets: list[str] = field(default_factory=list)
    reused_mapping_sets: list[str] = field(default_factory=list)
    extended_mapping_sets: dict[str, list[str]] = field(default_factory=dict)
    extended_mapping_set_components: dict[str, list[str]] = field(default_factory=dict)
    skip_lines: list[str] = field(default_factory=list)
    written_paths: list[str] = field(default_factory=list)
    access_collapse_lines: list[str] = field(default_factory=list)
    file_suffix_lines: list[str] = field(default_factory=list)
    native_declaration_missing_lines: list[str] = field(default_factory=list)
    unread_physics_lines: list[str] = field(default_factory=list)
    #: ``import model``'s ``represented_lines``, narrowed to the types and data constraints this
    #: plan creates.
    represented_lines: list[str] = field(default_factory=list)
    operation_event_regime: str = NONE
    preserved_operation_events: dict[str, dict[str, int | bool]] = field(default_factory=dict)
    queue_length_lines: list[str] = field(default_factory=list)
    descriptions_not_taken_lines: list[str] = field(default_factory=list)
    #: One line per descriptive entry of the import's skip logs (a ``SW-CALIBRATION-ACCESS``
    #: where the model holds none, a ``CONSTR-LEVEL``, a label, a ``UUID``, package content, a
    #: name the writer generates itself). Report-only, never an abort.
    not_carried_lines: list[str] = field(default_factory=list)
    #: One line per created element placed in a sub-package of its root, because the resulting
    #: model would otherwise hold a name differing from it only in case. Not a loss: the name
    #: and every reference are unchanged.
    package_placement_lines: list[str] = field(default_factory=list)
    #: One line per behaviour reference neither the file nor the target answers, by layer and
    #: element, never by a path. Non-empty exactly when the import withheld ``runnables`` and
    #: unresolved ``data_type_mappings`` (or, with :attr:`behaviour_kept`, kept the project's own).
    missing_references: list[str] = field(default_factory=list)
    #: The regime for inter-runnable variables and their accesses (``capabilities.VARIABLES``).
    variable_regime: str = NONE
    #: What the ``carried`` variable regime kept: the target's variables in its order, and per
    #: runnable the variable accesses carried by list name.
    preserved_variables: list[str] = field(default_factory=list)
    preserved_variable_accesses: dict[str, dict[str, int]] = field(default_factory=dict)
    #: The target's ``data_type_mappings`` entries the carried variables need and the
    #: file does not name, carried with them, in the target's order.
    preserved_variable_mappings: list[str] = field(default_factory=list)
    #: The regime for exclusive areas and ``can_enter``/``runs_inside``
    #: (``capabilities.EXCLUSIVE_AREAS``).
    exclusive_area_regime: str = NONE
    #: What the ``carried`` area regime kept: the target's areas in its order, and per runnable
    #: the area references carried by list name.
    preserved_exclusive_areas: list[str] = field(default_factory=list)
    preserved_exclusive_area_references: dict[str, dict[str, int]] = field(default_factory=dict)
    #: ``(areas, references)`` of the written component for a ``created`` or ``updated`` SWC;
    #: ``(0, 0)`` otherwise, and then nothing is rendered.
    written_exclusive_areas: tuple[int, int] = (0, 0)
    #: The regime for data-received events (``capabilities.DATA_EVENTS``).
    data_event_regime: str = NONE
    #: What the ``carried`` data-received event regime kept: events carried per runnable.
    preserved_data_events: dict[str, int] = field(default_factory=dict)
    #: The regime for the implicit form of reads and writes (``capabilities.IMPLICIT_ACCESS``).
    implicit_access_regime: str = NONE
    #: What the ``carried`` implicit access regime kept: per runnable, the reads and writes given
    #: the target's ``by: implicit``, by list name.
    preserved_implicit_accesses: dict[str, dict[str, int]] = field(default_factory=dict)
    #: The regime for asynchronous calls and ``call_returned`` events
    #: (``capabilities.ASYNCHRONOUS_CALLS``).
    asynchronous_call_regime: str = NONE
    #: What the ``carried`` asynchronous call regime kept of the synchrony: per runnable, the
    #: calls given the target's ``asynchronous`` with its ``poll``, ``result`` and ``timeout``.
    preserved_asynchronous_calls: dict[str, int] = field(default_factory=dict)
    #: What the ``carried`` asynchronous call regime kept of the events: ``call_returned``
    #: events carried per runnable, in their places.
    preserved_call_returned_events: dict[str, int] = field(default_factory=dict)
    #: One line per asynchronous call the written component leaves with no result point at the
    #: project's ``arxml_version`` (``rule:swc.call.async-without-result``, 4.2.2 alone) that
    #: the target's component did not hold, for a ``created`` or ``updated`` SWC. Named by
    #: runnable, port and operation; report-only, on ``--dry-run`` and in the MCP plan too.
    async_without_result_lines: list[str] = field(default_factory=list)
    #: One line per finding of the other asynchronous call rules (none version-aware) the
    #: written component holds and the target's did not, as rule id and message; report-only.
    async_call_finding_lines: list[str] = field(default_factory=list)
    #: One line per implicit read finding (``swc.access.queued-implicit-read``,
    #: ``swc.access.implicit-read-enable-update``) the carries introduce and neither the
    #: target's component nor the file's held, as rule id and message; report-only.
    implicit_access_finding_lines: list[str] = field(default_factory=list)

    def render(self) -> str:
        """The report as text: :meth:`blocks`, a list's items as ``  - <item>`` under its heading."""
        lines = [self.title]
        for block in self.blocks():
            lines.extend(block.text_lines())
        return "\n".join(lines)

    def render_markdown(self) -> str:
        """The same report as Markdown (``import component --report PATH.md``): :meth:`blocks` again.

        Nothing is added to or left out of the text form. Newline-terminated. A source that is not a
        bare file name follows the heading as ``Source: <path>``. Does not reach: the editor's report
        links, which read the text form only.
        """
        lines = [f"# {heading_text(IMPORT_REPORT_TITLE + self.source_name)}"]
        if self.source_name != self.source:
            lines += ["", paragraph_start(f"Source: {self.source}")]
        for block in self.blocks():
            lines += ["", *block.markdown_lines()]
        return "\n".join(lines) + "\n"

    @property
    def source_name(self) -> str:
        """The source's file name: its last component split at ``/`` and at ``\\`` alike.

        ``PureWindowsPath`` splits at both separators, so a Windows path names its file on a
        POSIX host too. What it does not reach: a POSIX file name holding a backslash is cut at
        it. A path with no file name (a bare drive or root) keeps the source whole.
        """
        return PureWindowsPath(self.source).name or self.source

    @property
    def title(self) -> str:
        """The report's first line."""
        return IMPORT_REPORT_TITLE + self.source

    def blocks(self) -> list[ReportLine | ReportList]:
        """The report below its title, in order: the one list both spellings are written from."""
        blocks: list[ReportLine | ReportList] = []
        if self.dry_run:
            blocks.append(ReportLine("(DRY RUN -- no files were written)"))
        blocks.append(ReportLine(f"SWC {self.swc}: {self.swc_action}", strong=True))
        if self.missing_references:
            if not self.behaviour_kept:
                tail = MISSING_REFERENCES_HEADING_TAIL
            elif self.swc_action == "updated":
                tail = MISSING_REFERENCES_KEPT_TAIL
            else:
                tail = MISSING_REFERENCES_UNCHANGED_TAIL
            blocks.append(
                ReportList(
                    f"{MISSING_REFERENCES_HEADING} ({len(self.missing_references)}; {tail}):",
                    list(self.missing_references),
                )
            )
        if self.swc_diff_preview:
            blocks.append(ReportList("Update diff preview:", self.swc_diff_preview.splitlines(), verbatim=True))
        if self.preserved_memory:
            budgets = ", ".join(f"{pool}={size}" for pool, size in sorted(self.preserved_memory.items()))
            blocks.append(
                ReportLine(
                    f"MEMORY BUDGET preserved from the target project (not carried by the source ARXML): {budgets}"
                )
            )
        if self.preserved_asil is not None:
            blocks.append(
                ReportLine(
                    f"ASIL preserved from the target project (not carried by the source ARXML): {self.preserved_asil}"
                )
            )
        if self.preserved_handle_termination_and_restart is not None:
            blocks.append(
                ReportLine(
                    "HANDLE-TERMINATION-AND-RESTART preserved from target "
                    f"({self.preserved_handle_termination_and_restart}): source element absent; "
                    "absence does not request clearing this field."
                )
            )
        for part in self.preserved_descriptions:
            blocks.append(ReportLine(f"DESC of {part} preserved from target: source element carries none"))
        if self.preserved_costs:
            costs = ", ".join(f"{runnable}={cost}" for runnable, cost in sorted(self.preserved_costs.items()))
            blocks.append(
                ReportLine(f"WCET preserved from the target project (not carried by the source ARXML): {costs}")
            )
        if self.preserved_measures:
            measures = [
                f"{port}: " + ", ".join(f"{element}={text!r}" for element, text in sorted(entries.items()))
                for port, entries in sorted(self.preserved_measures.items())
            ]
            blocks.append(
                ReportList("MEASURES preserved from the target project (not carried by the source ARXML):", measures)
            )
        if self.dropped_measures:
            # A separate block: a measure the update does NOT carry is what the integrator
            # has to see before the write.
            blocks.append(ReportList("MEASURES NOT carried from the target project:", list(self.dropped_measures)))
        if self.preserved_links:
            blocks.append(
                ReportList(
                    "LINKS preserved from the target project (not carried by the source ARXML):",
                    [f"{site}: {', '.join(links)}" for site, links in self.preserved_links.items()],
                )
            )
        if self.dropped_links:
            blocks.append(ReportList("LINKS NOT carried from the target project:", list(self.dropped_links)))
        # The capability-marker families in ``CAPABILITIES``' order: lines before the regime
        # line, the regime line, then what the family's carry kept.
        for capability in CAPABILITIES:
            blocks.extend(capability.blocks_before(self))
            regime = getattr(self, capability.regime_field)
            if regime != NONE:
                blocks.append(ReportLine(f"{capability.heading} regime: {regime} -- {capability.sentences[regime]}"))
            blocks.extend(capability.blocks_after(self))
        for heading, tail, findings in (
            (ASYNC_WITHOUT_RESULT_HEADING, ASYNC_WITHOUT_RESULT_HEADING_TAIL, self.async_without_result_lines),
            (ASYNC_CALL_FINDINGS_HEADING, ASYNC_CALL_FINDINGS_HEADING_TAIL, self.async_call_finding_lines),
            (
                IMPLICIT_READ_FINDINGS_HEADING,
                IMPLICIT_READ_FINDINGS_HEADING_TAIL,
                self.implicit_access_finding_lines,
            ),
        ):
            if findings:
                blocks.append(ReportList(f"{heading} ({len(findings)}; {tail}):", list(findings)))
        for label, names in (
            ("CREATED", self.created_interfaces),
            ("REUSED", self.reused_interfaces),
        ):
            marked = [f"{name} {STUB_INTERFACE_MARK}" if name in self.stub_interfaces else name for name in names]
            blocks.append(ReportList(f"{label} {len(names)} interface(s)", marked))
        for label, names in (
            ("CREATED", self.created_data_types),
            ("REUSED", self.reused_data_types),
        ):
            blocks.append(ReportList(f"{label} {len(names)} data type(s)", list(names)))
        # Rendered only when the exchange touched the layer (the mapping sets' reason below).
        for label, by_layer in (("CREATED", self.created_elements), ("REUSED", self.reused_elements)):
            for layer in ELEMENT_LAYERS:
                names = by_layer.get(layer, [])
                if names:
                    blocks.append(ReportList(f"{label} {len(names)} {PLACEMENT_WHAT[layer]}(s)", list(names)))
        # Rendered only when touched, unlike the interface and data-type blocks: most
        # components name no mapping set, and a "0 mapping set(s)" line on each would serve
        # no reader.
        for label, names in (
            ("CREATED", self.created_mapping_sets),
            ("REUSED", self.reused_mapping_sets),
        ):
            if names:
                blocks.append(ReportList(f"{label} {len(names)} mapping set(s)", list(names)))
        if self.extended_mapping_sets:
            extended = []
            for name, added in self.extended_mapping_sets.items():
                named_by = self.extended_mapping_set_components.get(name, [])
                also = f" (named by {', '.join(named_by)})" if named_by else ""
                extended.append(f"{name}: adds {', '.join(added)}{also}")
            blocks.append(ReportList(f"EXTENDED {len(self.extended_mapping_sets)} mapping set(s)", extended))
        if self.access_collapse_lines:
            blocks.append(ReportList("Collapsed duplicate access points:", list(self.access_collapse_lines)))
        if self.file_suffix_lines:
            blocks.append(ReportList(file_suffix_heading(len(self.file_suffix_lines)), list(self.file_suffix_lines)))
        if self.package_placement_lines:
            blocks.append(
                ReportList(
                    package_placement_heading(len(self.package_placement_lines)), list(self.package_placement_lines)
                )
            )
        if self.native_declaration_missing_lines:
            blocks.append(
                ReportList(NATIVE_DECLARATION_MISSING_HEADING + ":", list(self.native_declaration_missing_lines))
            )
        if self.unread_physics_lines:
            blocks.append(ReportList(UNREAD_PHYSICS_HEADING + ":", list(self.unread_physics_lines)))
        if self.represented_lines:
            blocks.append(ReportList(REPRESENTED_HEADING + ":", list(self.represented_lines)))
        if self.queue_length_lines:
            blocks.append(
                ReportList(
                    f"{QUEUE_LENGTH_HEADING} ({len(self.queue_length_lines)}; {QUEUE_LENGTH_HEADING_TAIL}):",
                    list(self.queue_length_lines),
                )
            )
        if self.descriptions_not_taken_lines:
            blocks.append(
                ReportList(
                    f"{DESCRIPTIONS_NOT_TAKEN_HEADING} ({len(self.descriptions_not_taken_lines)}; "
                    f"{DESCRIPTIONS_NOT_TAKEN_HEADING_TAIL}):",
                    list(self.descriptions_not_taken_lines),
                )
            )
        if self.not_carried_lines:
            blocks.append(
                ReportList(
                    f"{NOT_CARRIED_HEADING} ({len(self.not_carried_lines)}; {NOT_CARRIED_HEADING_TAIL}):",
                    list(self.not_carried_lines),
                )
            )
        if self.skip_lines:
            blocks.append(ReportList("Skipped out-of-scope elements:", list(self.skip_lines)))
        if self.written_paths:
            blocks.append(ReportList(f"WROTE {len(self.written_paths)} file(s)", list(self.written_paths)))
        return blocks


@dataclass(frozen=True)
class PlannedTarget:
    """One element an import would write, in both vocabularies it is needed in.

    ``path``/``payload`` are the storage form the CLI applies; ``layer``/``placement``/
    ``element`` are the model form the MCP tool derives a change set from. Produced together
    so the two callers cannot disagree. ``layer`` is the editing protocol's layer name, not a
    directory; ``placement`` holds the layer's placement values (``{"group": ...}`` for
    ``swc``). There is deliberately no identity field: the derivation reads it off
    ``element``, and a second copy could fall out of step with the registry.
    """

    layer: str
    element: BaseModel
    placement: Mapping[str, str]
    path: Path
    payload: dict[str, Any]


@dataclass(frozen=True)
class ComponentImportPlan:
    """Everything one import decided, before anything is written.

    ``report`` has ``written_paths`` empty (the apply fills it); ``targets`` is the ordered set
    of elements the import would write. A conflict-free import with no work has empty
    ``targets`` and is a successful no-op, never an error.
    """

    report: ComponentImportReport
    targets: tuple[PlannedTarget, ...] = ()
    #: The lines for planned creates that take a collision-suffixed file name, kept beside
    #: :attr:`report`: ``import_component`` copies them into the CLI's report, and the MCP
    #: tool names no file.
    file_suffix_lines: tuple[str, ...] = ()
    #: The dependency verdicts behind ``report.missing_references``, as records.
    missing_references: tuple[MissingReference, ...] = ()

    @property
    def writes(self) -> list[tuple[Path, dict[str, Any]]]:
        """The planned writes in plan order — ``_atomic_apply``'s argument."""
        return [(target.path, target.payload) for target in self.targets]


def render_export_summary(reports: list[ComponentExportReport], output_dir: str) -> str:
    """The batch ``export-summary.json`` content for ``reports``.

    See the module docstring for the schema. ``reports`` is emitted in the given order (already
    sorted by SWC name); output paths are relative to ``output_dir``.
    """
    entries = [
        {
            "swc": report.swc,
            "output_path": (
                os.path.relpath(report.output_path, output_dir) if report.output_path is not None else None
            ),
            "status": report.status,
            "interfaces": report.interfaces,
            "data_types": report.data_types,
            "error": report.error,
        }
        for report in reports
    ]
    return json.dumps(entries, indent=2) + "\n"
