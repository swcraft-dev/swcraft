"""The build report: one Markdown document of what a ``swcraft validate`` run gave.

:func:`compute_build_report` gathers the facts after the ARXML check returned: the verdict chosen from the
check's own :class:`ValidationOutcome` (the object the exit code is chosen from), the outputs on disk, the run's
warnings, every core and memory, and the ARXML elements that differ from the previous ARXML.
:func:`render_markdown` turns them into the document :func:`write_build_report` writes to
``<dist_root>/build-report.md``. Every sentence the document can carry is a constant of this module.

**Computed, never written by AI, and no qualification evidence**: the exit code of ``swcraft validate`` stays
the one verdict, and nothing here can change it; the caller guards the whole step (computing and writing) and
reports a failure as a warning line.

The resources are the Resource Report's own rows (a memory's budget in whole bytes); the ARXML comparison
reads both files as bytes. It imports no ``writer`` and no Change Report module, so ``validate`` loads
neither; the previous ARXML's name (``<stem>_old<suffix>``) is re-spelled in :func:`previous_arxml_path`. The
document holds no timestamp and no absolute path: every file is named by its project-relative POSIX path (its
file name alone outside the project directory).

**What it does not reach.** AUTOSAR semantics in the comparison (a reordered reference list is a change),
elements outside an ``ELEMENTS``, and which run wrote a file on disk or the ``_old`` file. The validation
messages are not copied. The resource rows are computed from the model on disk after the ARXML check, so a
model edited during the run can give resource figures, findings and ARXML of different states, and in a Build
an edit between ``generate arxml`` and ``validate`` makes the comparison run against that intermediate ARXML.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lxml import etree

from swcraft_tools.arxml_elements import (
    ArxmlElementChange,
    compare_arxml_elements,
    declared_schema_location,
    parse_arxml,
)
from swcraft_tools.arxml_validation import MODE_STRUCTURAL, ValidationOutcome
from swcraft_tools.exclusive_create import open_unique_sibling
from swcraft_tools.markdown_cells import cell_text, code_span, inline_literal, paragraph_start, prose_code_span, table
from swcraft_tools.resource_report import KIND_CORE, budget_rows, byte_figure, resource_report

if TYPE_CHECKING:
    from swcraft_tools.project import SwcraftProject
    from swcraft_tools.validator import ConsistencyResult

__all__ = [
    "BUILD_REPORT_FILE",
    "ArxmlSection",
    "BuildReport",
    "BuiltOutput",
    "ResourceRow",
    "compute_build_report",
    "previous_arxml_path",
    "render_markdown",
    "write_build_report",
]

#: The report's file name, in the manifest's ``dist_root``; one name, overwritten by every run that writes it.
BUILD_REPORT_FILE = "build-report.md"

# ---------------------------------------------------------------------------
# The document's text: every heading, column and sentence it can carry
# ---------------------------------------------------------------------------

TITLE = "Build Report"
SUBTITLE = "{project} · AUTOSAR {version}"

HEADING_VERDICT = "Verdict"
HEADING_BUILT = "What was built"
HEADING_FINDINGS = "Findings"
HEADING_RESOURCES = "Resources"
HEADING_ARXML = "ARXML changes"

#: The verdict, one sentence per outcome of the ARXML check; ``{schema}`` is the schema's file name alone.
VERDICT_XSD_PASSED = "Validation passed: the ARXML was checked against the AUTOSAR schema {schema}."
VERDICT_STRUCTURAL_PASSED = (
    "Validation passed with the structural check only: XSD validation is skipped "
    "(requires the licensed {schema} schema), so the ARXML was not checked against the AUTOSAR schema."
)
VERDICT_FAILED = (
    "Validation failed: the ARXML failed the {mode} check and stays on disk as {arxml}; "
    "the reasons are in the build log."
)
VERDICT_NOT_VALIDATED = (
    "Not validated: the {mode} check could not use the schema {schema}. "
    "This is a toolchain or configuration problem, not a problem with the model."
)
#: What follows the verdict sentence: the run's warning count.
WARNING_COUNT = "{count} warning(s)."
NO_WARNING = "No warning."

BUILT_COLUMNS = ("Output", "File", "State")
OUTPUT_ARXML = "ARXML"
OUTPUT_DIAGRAMS = "Diagrams"
OUTPUT_NEEDS = "Sphinx-Needs"
STATE_WRITTEN = "written by this run, AUTOSAR {version}"
STATE_ON_DISK = "on disk"
STATE_ABSENT = "absent"
#: What "on disk" proves, under the outputs table.
BUILT_NOTE = "After a Build every file listed here is that Build's own: its first step removes them all."

FINDINGS_COLUMNS = ("Rule", "Finding")
NO_FINDINGS = "The model check found no warning."

RESOURCES_COLUMNS = ("Resource", "Kind", "Used", "Limit", "Headroom", "Physical headroom", "Status")
NO_RESOURCES = "The model declares no core and no memory."

#: The ARXML section's first sentence, one per case; ``{old}`` is the previous ARXML's project-relative path
#: (in :data:`ARXML_NOT_XML` the generated ARXML's when that one is the file that is no XML document).
ARXML_NO_PREVIOUS = "No previous ARXML is kept: {old} is absent, so no element is compared."
ARXML_NOT_XML = "Not compared: {old} is not an XML document."
ARXML_NOT_READ = "Not compared: {old} cannot be read."
#: A previous document ``lxml`` parsed but cannot canonicalize (a relative namespace URI, for one).
ARXML_NOT_CANONICAL = "Not compared: {old} is an XML document whose elements cannot be put in canonical form."
ARXML_UNCHANGED = "No ARXML element changed against {old}."
ARXML_CHANGED = "{count} element(s) changed against {old}: {added} added, {removed} removed, {changed} changed."
#: The line after the first sentence when the two roots declare different ``xsi:schemaLocation`` values,
#: each named with its schema locations cut to their file names (:func:`_schema_location_name`).
ARXML_SCHEMAS_DIFFER = "The previous ARXML declares {old_location}; this one declares {new_location}."
#: How a root that declares no ``xsi:schemaLocation`` is named in :data:`ARXML_SCHEMAS_DIFFER`.
NO_SCHEMA_LOCATION = "no schema location"
ARXML_COLUMNS = ("Element", "Type", "Change")

FOOTER = (
    "Generated by `swcraft validate --report`. Every line is computed from the model, the generated ARXML "
    "and the previous one; nothing is written by AI. The verdict is `swcraft validate`'s exit code; "
    "this report is no qualification evidence."
)

#: The page the panel's Diagrams opens, below ``views_root``; ``writer.diagrams.page.INDEX_PAGE``, re-spelled.
DIAGRAMS_PAGE = "index.html"


# ---------------------------------------------------------------------------
# The facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuiltOutput:
    """One row of *What was built*: the output's label, its file's project-relative name and its state."""

    label: str
    path: str
    state: str


@dataclass(frozen=True)
class ResourceRow:
    """One core or memory, its figures as the Resource Report spells them (a memory's with ``bytes``)."""

    name: str
    kind: str
    used: str
    limit: str
    headroom: str
    physical_headroom: str
    status: str


@dataclass(frozen=True)
class ArxmlSection:
    """The ARXML comparison: the first sentence, the schema line and the rows."""

    sentence: str
    schema_sentence: str | None
    changes: tuple[ArxmlElementChange, ...] = ()


@dataclass(frozen=True)
class BuildReport:
    """What one ``validate --report`` run gave.

    ``verdict``, each output's ``state`` and the ARXML section's two sentences are Markdown already; the other
    texts are raw, and :func:`render_markdown` escapes them or sets them in code spans.
    """

    project_name: str
    arxml_version: str
    verdict: str
    warning_count: int
    outputs: tuple[BuiltOutput, ...]
    findings: tuple[tuple[str, str], ...]
    resources: tuple[ResourceRow, ...]
    arxml: ArxmlSection


def previous_arxml_path(arxml_path: str | os.PathLike[str]) -> Path:
    """``<stem>_old<suffix>`` beside *arxml_path*: ``writer.arxml_xml.previous_arxml_path``'s one line, re-spelled."""
    path = Path(arxml_path)
    return path.with_name(f"{path.stem}_old{path.suffix}")


def _project_relative(path: str | os.PathLike[str], project_root: str) -> str:
    """*path* relative to the project directory, POSIX-spelled; its file name alone when it lies outside.

    The paths as given are tried first, then both resolved, so a project reached through a symbolic link still
    gives the project-relative name.
    """
    for candidate, root in ((Path(path), Path(project_root)), (Path(path).resolve(), Path(project_root).resolve())):
        with contextlib.suppress(ValueError):
            return candidate.relative_to(root).as_posix()
    return Path(path).name


def _verdict(project: SwcraftProject, outcome: ValidationOutcome) -> str:
    """The verdict sentence for *outcome*, the object ``validate``'s exit code is chosen from."""
    configured = project.resolved_arxml_version.schema_filename
    schema = prose_code_span(Path(outcome.schema_path).name if outcome.schema_path else configured)
    if outcome.schema_error:
        return VERDICT_NOT_VALIDATED.format(mode=outcome.mode, schema=schema)
    if not outcome.valid:
        arxml = prose_code_span(_project_relative(project.resolved_arxml_path, project.resolved_project_root))
        return VERDICT_FAILED.format(mode=outcome.mode, arxml=arxml)
    if outcome.mode == MODE_STRUCTURAL:
        return VERDICT_STRUCTURAL_PASSED.format(schema=prose_code_span(configured))
    return VERDICT_XSD_PASSED.format(schema=schema)


def _outputs(project: SwcraftProject) -> tuple[BuiltOutput, ...]:
    """The ARXML, the diagrams' page and the ``needs.json``, each named and stated; no directory is listed."""
    root = project.resolved_project_root
    diagrams = Path(project.resolved_views_root) / DIAGRAMS_PAGE
    needs = Path(project.resolved_needs_path)
    return (
        BuiltOutput(
            OUTPUT_ARXML,
            _project_relative(project.resolved_arxml_path, root),
            STATE_WRITTEN.format(version=project.resolved_arxml_version),
        ),
        BuiltOutput(
            OUTPUT_DIAGRAMS, _project_relative(diagrams, root), STATE_ON_DISK if diagrams.is_file() else STATE_ABSENT
        ),
        BuiltOutput(OUTPUT_NEEDS, _project_relative(needs, root), STATE_ON_DISK if needs.is_file() else STATE_ABSENT),
    )


def _resource_row(row: dict[str, Any]) -> ResourceRow:
    """A Resource Report budget row as the table's cells: a core's utilization, a memory's bytes (``byte_figure``)."""
    if row["kind"] == KIND_CORE:
        return ResourceRow(
            row["name"],
            row["kind"],
            row["utilization"],
            row["limit"],
            row["headroom"],
            row["physical_headroom"],
            row["status"],
        )
    used, limit, headroom, physical = (
        byte_figure(row[key]) for key in ("used", "limit", "headroom", "physical_headroom")
    )
    return ResourceRow(row["name"], row["kind"], used, limit, headroom, physical, row["status"])


def _resources(project: SwcraftProject) -> tuple[ResourceRow, ...]:
    """Every core, then every memory, from the Resource Report's own rows (no topology: those are findings)."""
    return tuple(_resource_row(row) for row in budget_rows(resource_report(project.resolved_model_root)))


#: A namespace token kept as written: an ``http:``/``https:`` URI or a ``urn:``, never where a file is stored.
_NAMESPACE_KEPT = re.compile(r"(?:https?://|urn:)", re.IGNORECASE)


def _schema_location_name(location: str | None) -> str | None:
    """*location* with every schema location cut to its file name; ``None`` for a root that declares none.

    A location may be a ``file:`` URI or an absolute path, which would put where a schema is stored into the
    report. A namespace token is kept as written only when it is an ``http:``, ``https:`` or ``urn:`` URI, and
    cut like a location otherwise (a ``file:`` URI or a path in a namespace's place, or the lone token of an
    older ARXML that declares a location without its namespace).
    """
    if location is None:
        return None
    return " ".join(
        token if index % 2 == 0 and _NAMESPACE_KEPT.match(token) else _file_name(token)
        for index, token in enumerate(location.split())
    )


def _file_name(token: str) -> str:
    """*token*'s last ``/`` or ``\\`` step; the token itself when it is nothing but separators."""
    return re.split(r"[/\\]", token.rstrip("/\\"))[-1] or token


def _schema_location_text(location: str | None) -> str:
    return prose_code_span(location) if location is not None else NO_SCHEMA_LOCATION


def _arxml_section(project: SwcraftProject) -> ArxmlSection:
    """The generated ARXML against the previous one, each read as bytes and parsed by ``lxml`` once.

    The previous file can be unreadable, no XML document or one ``lxml`` cannot canonicalize: it is whatever an
    earlier run left. A generated file that is no XML document is named in :data:`ARXML_NOT_XML` (the verdict
    already says the check failed) instead of losing the whole report. A canonicalization fault is the previous
    file's; a fault reading the generated one is the caller's warning.
    """
    current_path = Path(project.resolved_arxml_path)
    previous_path = previous_arxml_path(current_path)
    old = prose_code_span(_project_relative(previous_path, project.resolved_project_root))
    try:
        previous_is_file = stat.S_ISREG(previous_path.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        previous_is_file = False
    except OSError:
        # An inaccessible directory or link on the way: there, but not readable.
        return ArxmlSection(ARXML_NOT_READ.format(old=old), None)
    if not previous_is_file:
        return ArxmlSection(ARXML_NO_PREVIOUS.format(old=old), None)
    try:
        previous_root = parse_arxml(previous_path.read_bytes())
    except OSError:
        return ArxmlSection(ARXML_NOT_READ.format(old=old), None)
    except etree.XMLSyntaxError:
        return ArxmlSection(ARXML_NOT_XML.format(old=old), None)
    try:
        current_root = parse_arxml(current_path.read_bytes())
    except etree.XMLSyntaxError:
        current = prose_code_span(_project_relative(current_path, project.resolved_project_root))
        return ArxmlSection(ARXML_NOT_XML.format(old=current), None)
    previous_location = _schema_location_name(declared_schema_location(previous_root))
    current_location = _schema_location_name(declared_schema_location(current_root))
    try:
        changes = compare_arxml_elements(previous_root, current_root)
    except etree.C14NError:
        return ArxmlSection(ARXML_NOT_CANONICAL.format(old=old), None)
    schema_sentence = (
        None
        if previous_location == current_location
        else ARXML_SCHEMAS_DIFFER.format(
            old_location=_schema_location_text(previous_location),
            new_location=_schema_location_text(current_location),
        )
    )
    if not changes:
        return ArxmlSection(ARXML_UNCHANGED.format(old=old), schema_sentence)
    kinds = [change.change for change in changes]
    sentence = ARXML_CHANGED.format(
        count=len(changes),
        old=old,
        added=kinds.count("added"),
        removed=kinds.count("removed"),
        changed=kinds.count("changed"),
    )
    return ArxmlSection(sentence, schema_sentence, changes)


def compute_build_report(project: SwcraftProject, result: ConsistencyResult, outcome: ValidationOutcome) -> BuildReport:
    """The report of the run that generated *project*'s ARXML and checked it into *outcome*.

    *result* is the model check the run already holds; its warnings are the findings, in the order the run
    printed them. Lists no directory. Raises what reading the model or the files raises (``ValueError``,
    ``OSError``).
    """
    return BuildReport(
        project_name=project.name,
        arxml_version=str(project.resolved_arxml_version),
        verdict=_verdict(project, outcome),
        warning_count=len(result.warnings),
        outputs=_outputs(project),
        findings=tuple((warning.rule, warning.message) for warning in result.warnings),
        resources=_resources(project),
        arxml=_arxml_section(project),
    )


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def _warning_tail(count: int) -> str:
    return WARNING_COUNT.format(count=count) if count else NO_WARNING


def render_markdown(report: BuildReport) -> str:
    """The report as Markdown: the title, five sections in their order, and the footer; ends with a newline."""
    subtitle = SUBTITLE.format(project=paragraph_start(report.project_name), version=report.arxml_version)
    lines = [f"# {TITLE}", "", subtitle, ""]

    lines += [f"## {HEADING_VERDICT}", "", f"{report.verdict} {_warning_tail(report.warning_count)}", ""]

    lines += [f"## {HEADING_BUILT}", ""]
    lines += table(BUILT_COLUMNS, [(output.label, code_span(output.path), output.state) for output in report.outputs])
    lines += ["", BUILT_NOTE, ""]

    lines += [f"## {HEADING_FINDINGS}", ""]
    if report.findings:
        lines += table(
            FINDINGS_COLUMNS, [(code_span(rule), inline_literal(message)) for rule, message in report.findings]
        )
    else:
        lines.append(NO_FINDINGS)
    lines.append("")

    lines += [f"## {HEADING_RESOURCES}", ""]
    if report.resources:
        lines += table(
            RESOURCES_COLUMNS,
            [
                (
                    code_span(row.name),
                    row.kind,
                    cell_text(row.used),
                    cell_text(row.limit),
                    cell_text(row.headroom),
                    cell_text(row.physical_headroom),
                    cell_text(row.status),
                )
                for row in report.resources
            ],
        )
    else:
        lines.append(NO_RESOURCES)
    lines.append("")

    lines += [f"## {HEADING_ARXML}", "", report.arxml.sentence, ""]
    if report.arxml.schema_sentence is not None:
        lines += [report.arxml.schema_sentence, ""]
    if report.arxml.changes:
        lines += table(
            ARXML_COLUMNS,
            [(code_span(change.path), cell_text(change.type), change.change) for change in report.arxml.changes],
        )
        lines.append("")

    lines += ["---", "", FOOTER]
    return "\n".join(lines) + "\n"


def _same_file_name(first: Path, second: Path) -> bool:
    """Whether *first* and *second* name one file.

    Resolved, then compared case-folded where the platform folds case (``os.path.normcase``). What it does
    not reach: a case-insensitive macOS volume, where ``normcase`` folds nothing, and a hard link under
    another name.
    """
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def write_build_report(project: SwcraftProject, report: BuildReport) -> Path:
    """Write *report* to ``<dist_root>/build-report.md`` whole, through a temporary file replaced into place.

    The temporary file ``build-report.md.<random>.tmp`` is created exclusively, following no link at the name,
    so a file or a symbolic link planted there is never written through; the ``FileExistsError`` of the last
    attempt propagates. The temporary file is removed when the write or the replace fails, and the original
    ``OSError`` propagates. A manifest whose ARXML output is the report's own path is refused first, with an
    ``OSError`` (``EEXIST``) and nothing written. Returns the report's path.

    **What it does not reach.** ``os.replace`` replaces a link at ``build-report.md`` itself rather than
    following it, but nothing locks the directory: whoever can write to ``dist_root`` can replace the report
    after the rename, and ``O_EXCL`` is the filesystem's guarantee, weakest on NFS before version 3.
    """
    target = Path(project.resolved_dist_root) / BUILD_REPORT_FILE
    if _same_file_name(target, Path(project.resolved_arxml_path)):
        # The replace below would put the report over the ARXML the run just validated; the strerror names
        # no path.
        raise OSError(errno.EEXIST, "the manifest names the ARXML output at the report's path")
    target.parent.mkdir(parents=True, exist_ok=True)
    text = render_markdown(report)
    temporary, created = open_unique_sibling(target.parent, target.name, ".tmp")
    try:
        with created as handle:
            handle.write(text)
        os.replace(temporary, target)
    except BaseException:
        # A failing cleanup must not mask the error that caused it.
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
    return target
