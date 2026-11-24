"""``generate``, ``validate``, ``check-model`` and ``clean-outputs``: their parsers and handlers."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from swcraft_tools import project as project_module
from swcraft_tools.arxml_validation import MODE_STRUCTURAL, check_arxml
from swcraft_tools.arxml_version import DEFAULT_ARXML_VERSION, SUPPORTED_VERSIONS, ArxmlVersion
from swcraft_tools.cli.messages import error_text, print_findings
from swcraft_tools.cli.project_args import as_given_or_in_project, guard_project_flag, resolve_project
from swcraft_tools.cli.report_files import build_report_step
from swcraft_tools.component_exchange import (
    EXPORT_SUMMARY_FILENAME,
    default_component_output,
    export_all_components,
    export_component,
)
from swcraft_tools.project import SwcraftProject, is_absolute_anywhere, manifest_keys
from swcraft_tools.undeclared_keys import UndeclaredKey, held_keys, undeclared_manifest_keys, unknown_key_clauses
from swcraft_tools.validator import ConsistencyIssue, ConsistencyResult, check_cross_layer_consistency, with_records
from swcraft_tools.writer import DiagramsResult, generate_arxml, generate_diagrams, generate_needs
from swcraft_tools.writer.arxml_xml import move_previous_arxml, previous_arxml_path
from swcraft_tools.yaml_load import load_yaml_document


def _arxml_version_for(args: argparse.Namespace, project: SwcraftProject) -> ArxmlVersion:
    """The AUTOSAR version this run generates at: ``--arxml-version``, else the project's.

    The flag is never written back: a one-off export must not retarget the project.
    """
    return ArxmlVersion.from_string(args.arxml_version) if args.arxml_version else project.resolved_arxml_version


def cmd_generate(args: argparse.Namespace) -> None:
    """``swcraft generate FORMAT --project PATH``: output files generated from the YAML model.

    ``--component NAME`` exports one SWC's dependency closure (default output derived here from the
    manifest's roots). ``--all-components --output-dir DIR`` exports every SWC, never aborts the batch,
    and exits 1 if any export failed. ``--arxml-version`` overrides the manifest for one run and is
    never written back. Format-specific flags are refused by name with any other format.
    """
    guard_project_flag(args, f"generate {args.format}")
    if (args.component or args.all_components) and args.format != "arxml":
        print(
            "Error: --component/--all-components apply only to 'generate arxml'.",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.arxml_version and args.format != "arxml":
        print(
            "Error: --arxml-version applies only to 'generate arxml'.",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.annotations is not None and args.format != "test-tables":
        print("Error: --annotations applies only to 'generate test-tables'.", file=sys.stderr)
        sys.exit(1)
    if args.all_components and not args.output_dir:
        print(
            "Error: --output-dir is required with --all-components.",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.output and not args.component:
        print("Error: --output requires --component.", file=sys.stderr)
        sys.exit(1)
    if args.output_dir and not args.all_components:
        print("Error: --output-dir requires --all-components.", file=sys.stderr)
        sys.exit(1)
    project = resolve_project(args)
    if args.format == "test-tables":
        _generate_test_tables(args, project)
        return

    try:
        if args.format == "arxml" and args.component:
            output = args.output or _default_component_output(project, args.component)
            version = _arxml_version_for(args, project)
            report = export_component(project.resolved_model_root, args.component, output, version=version)
            print(report.render())
            print(f"Generated at AUTOSAR {version}.")
        elif args.format == "arxml" and args.all_components:
            version = _arxml_version_for(args, project)
            reports = export_all_components(project.resolved_model_root, args.output_dir, version=version)
            for report in reports:
                print(report.render())
            print(f"Summary: {os.path.join(args.output_dir, EXPORT_SUMMARY_FILENAME)}")
            print(f"Generated at AUTOSAR {version}.")
            if any(not report.ok for report in reports):
                sys.exit(1)
        elif args.format == "arxml":
            version = _arxml_version_for(args, project)
            # A one-off --arxml-version leaves the manifest's ARXML at a version the manifest does not
            # declare; the manifest is not rewritten, so the user is told.
            if version != project.resolved_arxml_version:
                print(
                    f"Warning: {project_module.project_relative_name(project, project.resolved_arxml_path)} now "
                    f"declares AUTOSAR {version}, but the manifest declares {project.resolved_arxml_version}. The "
                    "next generate/validate without --arxml-version will overwrite it at "
                    f"{project.resolved_arxml_version}; set arxml_version in swcraft.yaml to make "
                    "this permanent.",
                    file=sys.stderr,
                )
            previous = _previous_arxml_state(project.resolved_arxml_path)
            out = generate_arxml(project.resolved_model_root, project.resolved_arxml_path, version=version)
            print(f"Generated ARXML: {out} (AUTOSAR {version})")
            _announce_previous_arxml(project.resolved_arxml_path, previous)
        elif args.format == "diagrams":
            result = generate_diagrams(project.resolved_model_root, project.resolved_views_root, project.name)
            _print_diagrams_result(result, project.resolved_views_root)
        elif args.format == "needs":
            out = generate_needs(project.resolved_model_root, project.resolved_needs_path, project.name)
            print(f"Generated needs.json: {out}")
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- entry: a failed generation is an Error: line and exit 1
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)


#: The rule of an undeclared manifest key. Outside the contract (which describes the model, not the
#: manifest); emitted only by ``check-model`` and ``validate``.
MANIFEST_UNKNOWN_KEY_RULE = "manifest.unknown-key"


def _manifest_unknown_key_records(
    found: tuple[UndeclaredKey, ...], keys: Mapping[str, frozenset[str]], document: Any
) -> list[ConsistencyIssue]:
    """One :data:`MANIFEST_UNKNOWN_KEY_RULE` error per undeclared manifest key, in document order.

    No subject (the manifest is no layer). ``target`` is the top-level key the finding sits under
    (``outputs`` for a key inside it; ``None`` for an empty key), where VS Code places it. The message
    names no file.
    """
    records: list[ConsistencyIssue] = []
    for item in found:
        nested = item.locator[0][1] if item.locator else None
        declared = keys.get("" if nested is None else str(nested), frozenset())
        carried, hint = unknown_key_clauses(item.key, declared, held_keys(document, item.locator))
        where = "The manifest" if nested is None else f"The manifest's `{nested}`"
        records.append(
            ConsistencyIssue(
                rule=MANIFEST_UNKNOWN_KEY_RULE,
                message=(
                    f"{where} carries {carried}, which it does not declare; it is dropped when the project is "
                    f"read. Rename the key to the setting it is meant to be, or delete it{hint}."
                ),
                target=str(nested) if nested is not None else (item.key or None),
            )
        )
    return records


def _manifest_records(manifest: str) -> list[ConsistencyIssue]:
    """The undeclared-key findings of the manifest at *manifest*.

    Used by ``check-model``, ``validate`` and ``generate test-tables`` only; every other command reads
    the manifest with the key dropped.
    """
    text = Path(manifest).read_text(encoding="utf-8")
    document = load_yaml_document(text)
    keys = manifest_keys()
    return _manifest_unknown_key_records(undeclared_manifest_keys(text, document, keys), keys, document)


def _findings_refuse(result: ConsistencyResult, warnings_as_errors: bool) -> bool:
    """Whether a model check's findings refuse the command.

    An error refuses; warnings refuse only under ``--warnings-as-errors``, which prints one stderr line
    so the verdict is seen to come from the flag. Called after :func:`print_findings`.
    """
    if not result.consistent:
        return True
    if warnings_as_errors and result.warnings:
        count = len(result.warnings)
        print(f"Error: {count} warning(s) treated as errors (--warnings-as-errors).", file=sys.stderr)
        return True
    return False


def cmd_validate(args: argparse.Namespace) -> None:
    """``swcraft validate``: the model checked, ARXML generated on success, then that ARXML checked.

    The generated file is checked by :func:`~swcraft_tools.arxml_validation.check_arxml` (full XSD with
    the licensed schema, structural otherwise, the mode reported). The ARXML is written before it is
    checked, so a failing check exits 1 with the file on disk.

    An error finding refuses before generation; warnings are listed and generation proceeds, unless
    ``--warnings-as-errors`` makes them refuse too. ``--report`` writes ``<dist_root>/build-report.md``
    after the ARXML check, whatever its outcome; a run refused before generation writes none.
    """
    guard_project_flag(args, "validate")
    project = resolve_project(args)
    model_root = project.resolved_model_root

    # The composer runs every other consistency check; calling one beside it would duplicate output.
    try:
        result = with_records(
            # The version-aware rule `[constr_2006]` is judged at the manifest's version.
            check_cross_layer_consistency(model_root, project.resolved_arxml_version),
            _manifest_records(args.project),
        )
    except Exception as exc:  # noqa: BLE001 -- entry: a model the check cannot read is an Error: line and exit 1
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)

    print_findings(result)
    if _findings_refuse(result, args.warnings_as_errors):
        sys.exit(1)

    version = project.resolved_arxml_version
    previous = _previous_arxml_state(project.resolved_arxml_path)
    try:
        out = generate_arxml(project.resolved_model_root, project.resolved_arxml_path, version=version)
    except Exception as exc:  # noqa: BLE001 -- entry: a failed ARXML build is an Error: line and exit 1
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)
    # Named before the check, so a build log always names the file it produced.
    print(f"ARXML generated: {out} (AUTOSAR {version})")
    _announce_previous_arxml(project.resolved_arxml_path, previous)
    # `check_arxml` reads the version from the file's own xsi:schemaLocation, so it checks against the
    # schema this run targeted.
    outcome = check_arxml(out)
    build_report_step(args, project, result, outcome)
    if outcome.schema_error:
        # A schema that does not compile is a toolchain fault, not a model fault; `check_arxml` already
        # printed the compiler's message.
        print(
            f"Error: {out} could not be XSD-validated because the schema {outcome.schema_path} "
            "is unusable; see the error above. This is a toolchain/configuration problem, not a "
            "problem with the model.",
            file=sys.stderr,
        )
        sys.exit(1)
    if outcome.mode == MODE_STRUCTURAL:
        # The licensed schema cannot be redistributed; the note names the file for this project's version.
        print(f"Note: XSD validation is skipped (requires the licensed {version.schema_filename} schema).")
    else:
        print(f"ARXML validated against {as_given_or_in_project(project, str(outcome.schema_path))}.")
    if not outcome.valid:
        # `check_arxml` already printed every diagnostic.
        print(f"Error: the generated ARXML failed {outcome.mode} validation: {out}", file=sys.stderr)
        sys.exit(1)
    print("Validation passed.")


#: The version of the document ``check-model --format json`` prints.
CHECK_MODEL_SCHEMA_VERSION = 1


def _check_model_json(result: ConsistencyResult) -> str:
    """The document ``check-model --format json`` prints: the run's findings, and nothing else.

    ``{"schemaVersion", "findings"}``: errors then warnings, in :func:`print_findings`' order, each with
    ``rule``, ``severity``, ``message``, ``subjects`` (never a storage path) and ``target`` (``null``
    unless the rule is about an unresolved identity). The verdict is the exit code alone. Encoded with
    ``allow_nan=False``; the caller handles ``ValueError``/``TypeError``.
    """
    findings = [
        {
            "rule": record.rule,
            "severity": record.severity,
            "message": record.message,
            "subjects": [subject.to_json() for subject in record.subjects],
            "target": record.target,
        }
        for record in (*result.records, *result.warnings)
    ]
    document = {"schemaVersion": CHECK_MODEL_SCHEMA_VERSION, "findings": findings}
    return json.dumps(document, indent=2, allow_nan=False) + "\n"


def cmd_check_model(args: argparse.Namespace) -> None:
    """``swcraft check-model``: the model checked only; it writes nothing, so a build graph can run it as a gate.

    The findings and verdict are ``validate``'s. ``--format json`` changes the rendering, never the
    verdict: stdout holds only the document, written before a refusing exit. A run refused before the
    check (manifest, pin, loader) prints no document.
    """
    guard_project_flag(args, "check-model")
    project = resolve_project(args)
    model_root = project.resolved_model_root

    # The composer runs every other consistency check; calling one beside it would duplicate output.
    try:
        result = with_records(
            # The version-aware rule `[constr_2006]` is judged at the manifest's version.
            check_cross_layer_consistency(model_root, project.resolved_arxml_version),
            _manifest_records(args.project),
        )
    except Exception as exc:  # noqa: BLE001 -- entry: a model the check cannot read is an Error: line and exit 1
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)

    print_findings(result)
    as_json = args.format == "json"
    if as_json:
        # `TypeError` too: a subject value `json` cannot encode is an `Error:` line, not a traceback.
        try:
            rendered = _check_model_json(result)
        except (TypeError, ValueError) as exc:
            print(f"Error: {error_text(exc)}", file=sys.stderr)
            sys.exit(1)
        sys.stdout.write(rendered)
    if _findings_refuse(result, args.warnings_as_errors):
        sys.exit(1)

    if not as_json:
        print("Validation passed (no artifacts generated).")


#: Names kept directly inside every directory ``clean-outputs`` empties (a nested one goes with its
#: directory). The MCP audit log is history, not build output, and is not committed, so wiping it would
#: lose the record. Everything else goes, so no stale output survives under a fresh build's name.
_CLEAN_KEEP_NAMES = frozenset({"audit.jsonl"})


def _clean_root_problems(project: SwcraftProject) -> list[str]:
    """Why the manifest's two output roots cannot be cleaned, or ``[]``.

    Refuses a root that is the project directory, is absolute, or resolves outside the project, one
    message per field, before anything is removed. The loader refuses the same; this guard repeats it
    because it stands in front of a deletion.
    """
    project_root = Path(project.resolved_project_root)
    declared = {"dist_root": project.dist_root, "views_root": project.views_root}
    resolved = {
        "dist_root": Path(project.resolved_dist_root),
        "views_root": Path(project.resolved_views_root),
    }
    problems = []
    for field, target in resolved.items():
        written = declared[field]
        # Not `Path.is_absolute()`: the platforms disagree, and this guard must answer as the loader does.
        if is_absolute_anywhere(written):
            problems.append(
                f"swcraft.yaml declares {field}: {written}, an absolute path — "
                "it must be written relative to the project directory."
            )
        elif target == project_root:
            problems.append(
                f"swcraft.yaml declares {field}: {written}, which is the project directory "
                "itself — cleaning it would remove the model."
            )
        elif not target.is_relative_to(project_root):
            problems.append(
                f"swcraft.yaml declares {field}: {written}, which resolves to '{target}', "
                f"outside the project directory '{project_root}'."
            )
    return problems


def _clean_output_problems(project: SwcraftProject) -> list[str]:
    """Which of the manifest's two outputs land outside both roots, or ``[]``.

    An output outside them would survive a failing build as a stale artifact, and deleting an arbitrary
    path named by a manifest line is refused instead. Resolved paths are compared, so ``..`` cannot
    escape.
    """
    roots = [Path(project.resolved_dist_root), Path(project.resolved_views_root)]
    declared = {
        "arxml": project.outputs.arxml,
        "needs": project.outputs.needs,
    }
    resolved = {
        "arxml": Path(project.resolved_arxml_path),
        "needs": Path(project.resolved_needs_path),
    }
    named = f"dist_root: {project.dist_root}, views_root: {project.views_root}"
    problems = []
    for key, path in resolved.items():
        if any(path.is_relative_to(root) for root in roots):
            continue
        problems.append(
            f"swcraft.yaml declares outputs.{key}: {declared[key]}, which is outside the "
            f"directories this command removes ({named})."
        )
    return problems


def _clean_target(target: Path, keep: frozenset[str]) -> list[Path]:
    """The removal of *target*, keeping any file named in *keep* directly in it; returns what was removed.

    A directory with nothing to keep is removed whole. One level only: a kept name nested deeper goes.
    """
    if not target.exists():
        return []
    if not target.is_dir():
        target.unlink()
        return [target]

    children = sorted(target.iterdir(), key=Path.as_posix)
    if not any(child.name in keep and child.is_file() for child in children):
        shutil.rmtree(target)
        return [target]

    removed = []
    for child in children:
        if child.name in keep and child.is_file():
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
        removed.append(child)
    return removed


def cmd_clean_outputs(args: argparse.Namespace) -> None:
    """``swcraft clean-outputs``: the manifest's ARXML moved aside, then its two output directories removed.

    A build graph runs this first, so after a failing build no output exists under its own name; the
    previous ARXML is kept as ``<stem>_old<suffix>``. Every refusal exits 1 before anything moves.
    """
    guard_project_flag(args, "clean-outputs")
    project = resolve_project(args)

    problems = _clean_root_problems(project) + _clean_output_problems(project)
    if problems:
        for problem in problems:
            print(f"Error: {problem}", file=sys.stderr)
        sys.exit(1)

    _move_previous_arxml_aside(project)
    dist_keep = _dist_keep_names(project)
    for target, keep in ((project.resolved_dist_root, dist_keep), (project.resolved_views_root, _CLEAN_KEEP_NAMES)):
        for removed in _clean_target(Path(target), keep):
            print(f"Removed {removed}")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


#: The help of ``--warnings-as-errors``, shared by ``validate`` and ``check-model`` (the only two
#: commands that declare it); the reference page renders it.
_WARNINGS_AS_ERRORS_HELP = (
    "treat every warning as an error: the warnings are still listed as warnings, the run then exits 1 "
    "and generates nothing — for a release gate"
)


def add_generate_parser(subparsers: argparse._SubParsersAction) -> None:
    """The ``swcraft generate`` parser: an output format, the project, the component scope and its outputs."""
    gen_parser = subparsers.add_parser("generate", help="Generate output files from the YAML model")
    gen_parser.add_argument("format", choices=["arxml", "diagrams", "needs", "test-tables"], help="Output format")
    gen_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Path to swcraft.yaml manifest (required)",
    )
    gen_scope = gen_parser.add_mutually_exclusive_group()
    gen_scope.add_argument(
        "--component",
        default=None,
        metavar="NAME",
        help="Export one SWC's dependency closure as component-scoped ARXML (arxml format only)",
    )
    gen_scope.add_argument(
        "--all-components",
        action="store_true",
        help="Export every SWC as its own component-scoped ARXML plus export-summary.json (arxml format only)",
    )
    gen_parser.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="Output path for --component (default: <dist_root>/components/<NAME>.arxml)",
    )
    gen_parser.add_argument(
        "--output-dir",
        default=None,
        metavar="PATH",
        help="Output directory for --all-components (required with it)",
    )
    gen_parser.add_argument(
        "--arxml-version",
        default=None,
        choices=list(SUPPORTED_VERSIONS),
        help=(
            "Target AUTOSAR version for this run (arxml format only); "
            "overrides the manifest's arxml_version without rewriting it "
            f"(default: the manifest value, itself defaulting to {DEFAULT_ARXML_VERSION})"
        ),
    )
    gen_parser.add_argument(
        "--annotations",
        default=None,
        metavar="DIR",
        help=(
            "The test side's directory of annotated tables to join by row_id (test-tables format only); "
            "read, never written"
        ),
    )


def add_validate_parser(subparsers: argparse._SubParsersAction) -> None:
    """The ``swcraft validate`` parser: the project, ``--warnings-as-errors`` and the build report."""
    val_parser = subparsers.add_parser(
        "validate",
        help=(
            "Validate the model, generate the ARXML and check it against the AUTOSAR XSD "
            "(structurally, saying so, when no licensed XSD is configured)"
        ),
    )
    val_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Path to swcraft.yaml manifest (required)",
    )
    val_parser.add_argument("--warnings-as-errors", action="store_true", help=_WARNINGS_AS_ERRORS_HELP)
    val_parser.add_argument(
        "--report",
        action="store_true",
        help=(
            "also write the build report, build-report.md in the manifest's dist directory, after the ARXML "
            "check, whether the check passes or fails (the exit code stays the check's)"
        ),
    )


def add_check_model_parser(subparsers: argparse._SubParsersAction) -> None:
    """The ``swcraft check-model`` parser: the project, ``--warnings-as-errors`` and the output format."""
    check_model_parser = subparsers.add_parser(
        "check-model",
        help="Validate the model only (no artifact generation; build-graph-safe)",
    )
    check_model_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Path to swcraft.yaml manifest (required)",
    )
    check_model_parser.add_argument("--warnings-as-errors", action="store_true", help=_WARNINGS_AS_ERRORS_HELP)
    check_model_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help=(
            "Output format: 'text' (default, human-readable: the success line on stdout) or 'json' "
            "(versioned, machine-readable: one document of the run's findings, each with its rule, "
            "severity, message and subjects' addresses, on stdout and nothing else there)"
        ),
    )


def add_clean_outputs_parser(subparsers: argparse._SubParsersAction) -> None:
    """The ``swcraft clean-outputs`` parser: the project whose generated directories are removed."""
    clean_parser = subparsers.add_parser(
        "clean-outputs",
        help="Remove the two directories this project generates into, so a failing build leaves nothing stale behind",
    )
    clean_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Path to swcraft.yaml manifest (required)",
    )


def _previous_arxml_state(arxml_path: str) -> tuple[int, int] | None:
    """The kept previous ARXML's ``(st_ino, st_mtime_ns)``, or ``None`` when it cannot be read.

    Compared before and after ``generate_arxml`` to learn whether it moved the old file; the inode
    tells two moves apart within one mtime tick (``os.replace`` keeps the mtime). Never raises.
    """
    try:
        stat = previous_arxml_path(arxml_path).stat()
    except OSError:
        return None
    return (stat.st_ino, stat.st_mtime_ns)


def _announce_previous_arxml(arxml_path: str, before: tuple[int, int] | None) -> None:
    """The ``Previous ARXML kept: <path>`` line, printed when this run moved the old ARXML aside."""
    after = _previous_arxml_state(arxml_path)
    if after is not None and after != before:
        print(f"Previous ARXML kept: {previous_arxml_path(arxml_path)}")


def _print_diagrams_result(result: DiagramsResult, views_root: str) -> None:
    """What one ``generate diagrams`` run did, one line per view, then the time and the report.

    The layout time is printed and never written, so the report stays byte-identical across runs.
    """
    written = set(result.written)
    for view in result.views:
        state = "written" if {f"{view}.svg", f"{view}.html"} & written else "kept"
        print(f"Diagram {view}: {state}")
    for name in result.removed:
        print(f"Removed: {name}")
    print(f"Layout: {result.layout_seconds:.2f} s")
    print(f"Report: {os.path.join(views_root, result.report_file)}")
    count = len(result.views)
    print(f"Generated diagrams: {views_root} ({count} view{'' if count == 1 else 's'})")


def _default_component_output(project: SwcraftProject, swc_name: str) -> str:
    """``--component``'s output path when ``--output`` is not given (``<NAME>~N`` for a case-only twin)."""
    return default_component_output(project.resolved_model_root, project.resolved_dist_root, swc_name)


def _arxml_in_dist_root(project: SwcraftProject) -> bool:
    """Whether the manifest's ARXML is a **direct child** of ``dist_root``: the one place ``clean-outputs`` keeps it."""
    return Path(project.resolved_arxml_path).parent == Path(project.resolved_dist_root)


def _dist_keep_names(project: SwcraftProject) -> frozenset[str]:
    """``dist_root``'s keep set for ``clean-outputs``: ``audit.jsonl``, and the previous ARXML's ``_old`` name.

    The ``_old`` name is kept whenever the ARXML is a direct child of ``dist_root``, moved this run or
    not, so the last good ARXML survives a build that failed before generating.
    """
    if not _arxml_in_dist_root(project):
        return _CLEAN_KEEP_NAMES
    return _CLEAN_KEEP_NAMES | {previous_arxml_path(project.resolved_arxml_path).name}


def _move_previous_arxml_aside(project: SwcraftProject) -> None:
    """The previous ARXML moved aside for ``clean-outputs`` and announced, or a refusal with exit 1.

    Only an ARXML directly in ``dist_root`` is moved, to ``<stem>_old<suffix>``. Not reached: an ARXML
    in a subdirectory of ``dist_root`` (not moved; its ``_old`` goes with the folder), a ``dist_root``
    inside ``views_root`` (the second pass removes the kept file), an ARXML under ``views_root``. A move
    the OS refuses exits 1 before the first removal.
    """
    if not _arxml_in_dist_root(project):
        return
    try:
        previous = move_previous_arxml(project.resolved_arxml_path)
    except OSError as exc:
        print(f"Error: cannot keep the previous ARXML: {exc}", file=sys.stderr)
        sys.exit(1)
    if previous is not None:
        print(f"Previous ARXML kept: {previous}")


def _generate_test_tables(args: argparse.Namespace, project: SwcraftProject) -> None:
    """``swcraft generate test-tables``: run :func:`swcraft_tools.tables_for_testing.generate_test_tables`.

    A project ``check-model`` refuses (manifest findings included) is refused here. Exit 1 with one
    ``Error:`` line on every refusal (nothing written), a blank ``--annotations`` included (it would name
    the working directory), a failed write, and a bundle only partly replaced, which the line says.
    """

    # `tables_for_testing` writes through `experiment_frame`, which the ARXML path never loads.
    from swcraft_tools import tables_for_testing  # noqa: PLC0415 -- start-up: the report writer stays off the ARXML path

    if args.annotations is not None and not args.annotations.strip():
        print(
            "Error: --annotations names no directory; give the test side's directory or leave it out", file=sys.stderr
        )
        sys.exit(1)
    try:
        generated = tables_for_testing.generate_test_tables(
            project, args.annotations, manifest_records=_manifest_records(args.project)
        )
    except tables_for_testing.TablesPartlyReplacedError as exc:
        print(f"Error: the test tables were only partly replaced ({exc}); run the command again", file=sys.stderr)
        sys.exit(1)
    except (tables_for_testing.TablesRefusedError, tables_for_testing.AnnotationsError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"Error: the test tables could not be written: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 -- entry: anything else is an Error: line and exit 1
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)

    if generated.unknown_files:
        print(
            "Warning: the annotations directory holds files that name no table, ignored: "
            + ", ".join(generated.unknown_files),
            file=sys.stderr,
        )
    print(f"Generated test tables: {generated.directory}")
    for table in generated.tables.tables:
        counts = tables_for_testing.review_counts(table)
        print(
            f"{table.file}: {len(table.rows)} rows ({counts['accepted']} accepted, {counts['stale']} stale, "
            f"{counts['rejected']} rejected, {counts['not-reviewed']} not reviewed)"
        )
    if generated.annotations_read:
        print(f"Orphaned annotations: {len(generated.tables.orphans)}")
