"""``swcraft import``: its parser and the ARXML, component and requirements import handlers."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from swcraft_tools.arxml_import import run_import
from swcraft_tools.cli.messages import error_text
from swcraft_tools.cli.project_args import (
    as_given_or_in_project,
    check_toolchain_pin_of,
    guard_project_flag,
    resolve_project,
)
from swcraft_tools.cli.report_files import refuse_missing_report_directory, wanted_markdown_report, write_report_file
from swcraft_tools.component_exchange import ComponentImportConflictError, import_component
from swcraft_tools.project import require_project
from swcraft_tools.requirements_import import (
    NO_DELIVERY_VERSION,
    apply_requirements_import,
    delivery_version,
    mapping_as_named,
    plan_requirements_import,
    render_report,
    render_report_markdown,
)
from swcraft_tools.requirements_interchange import InterchangeError, no_lone_surrogate, read_needs_file
from swcraft_tools.requirements_mapping import DEFAULT_MAPPING, MappingError, read_mapping


def cmd_import(args: argparse.Namespace) -> None:
    """Import an external architecture export into a SWCraft project.

    ``model``: bulk import into a fresh project. Exits non-zero when nothing was imported (skips alone
    are expected and reported, not a failure) or on any import error. ``component`` and
    ``requirements`` are dispatched to their own handlers.
    """
    if args.format == "component":
        cmd_import_component(args)
        return
    if args.format == "requirements":
        cmd_import_requirements(args)
        return

    guard_project_flag(args, "import model")
    _refuse_requirements_flags(args)
    if args.update:
        print(
            "Error: --update applies only to 'import component'.",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.replace_access_lists:
        print(
            "Error: --replace-access-lists applies only to 'import component'.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not args.source:
        print(
            "Error: --source is required.  Run: swcraft import model --source FILE --project PATH",
            file=sys.stderr,
        )
        sys.exit(1)
    # After the usage refusals, so those are decided whatever the project.
    check_toolchain_pin_of(args.project, args.command)

    packages: list[str] | None = None
    if args.packages:
        packages = [p.strip() for p in args.packages.split(",") if p.strip()]

    try:
        report = run_import(
            source=args.source,
            project_manifest=args.project,
            packages=packages,
            dry_run=args.dry_run,
            force=args.force,
        )
    except Exception as exc:  # noqa: BLE001 -- entry: a failed import is an Error: line and exit 1
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)

    report.source = as_given_or_in_project(require_project(args.project), args.source)
    text = report.render(verbose=args.verbose)
    print(text)
    if args.report:
        # A transcript of what was printed, nothing reads it back: the platform's line ending.
        Path(args.report).write_text(text, encoding="utf-8", newline=None)

    if report.total_imported == 0:
        sys.exit(1)


#: The exit status of the one refusal ``--update`` resolves: the component's own divergence is the only
#: conflict. Every other refusal exits 1. The extension's copy is held equal by a test.
UPDATE_RESOLVES_EXIT = 3


def _import_done(args: argparse.Namespace) -> str:
    """Return what an import run that got as far as its report did: the opening of its report's ``Error:`` lines."""
    return "the dry run completed" if args.dry_run else "the import was applied"


def cmd_import_component(args: argparse.Namespace) -> None:
    """Import one component-scoped ARXML export into an existing project (``swcraft import component``).

    Exit 1 on any import error: a malformed input, a lossy skip touching the component, an ambiguous
    target, or a conflict (a divergent shared definition always aborts; a divergent SWC unless
    ``--update``). Exit :data:`UPDATE_RESOLVES_EXIT` when the divergent SWC is the only conflict, so a
    caller can offer the update without parsing stderr. A no-op re-import exits 0; argparse's usage
    error is 2.

    The ``--report`` directory is checked first. A rendering or report write that fails after the
    import exits 1 with an ``Error:`` line saying the import was applied, never a traceback.
    """
    guard_project_flag(args, "import component")
    _refuse_requirements_flags(args)
    if not args.source:
        print(
            "Error: --source is required.  Run: swcraft import component --source FILE --project PATH",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.packages or args.force:
        print(
            "Error: --packages/--force apply only to 'import model'.",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.verbose:
        print(
            "Error: --verbose applies only to 'import model'.",
            file=sys.stderr,
        )
        sys.exit(1)
    # Without `--update` it would be silently ignored; refused before the project is read.
    if args.replace_access_lists and not args.update:
        print(
            "Error: --replace-access-lists applies only with --update.",
            file=sys.stderr,
        )
        sys.exit(1)

    refuse_missing_report_directory(args)

    project = resolve_project(args)
    try:
        report = import_component(
            source=args.source,
            model_root=project.resolved_model_root,
            update=args.update,
            dry_run=args.dry_run,
            replace_access_lists=args.replace_access_lists,
            arxml_version=project.resolved_arxml_version,
        )
    except Exception as exc:  # noqa: BLE001 -- entry: a failed import is an Error: line and its exit status
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        resolves = isinstance(exc, ComponentImportConflictError) and exc.update_resolves
        sys.exit(UPDATE_RESOLVES_EXIT if resolves else 1)

    # The import has committed: nothing below may turn the exit into 1 without saying so.
    report.source = as_given_or_in_project(project, args.source)
    try:
        text = report.render()
        markdown = wanted_markdown_report(args, report.render_markdown)
    except Exception as exc:  # noqa: BLE001 -- entry: a report that cannot be rendered after the import is an Error: line and exit 1
        print(f"Error: {_import_done(args)}, but its report could not be rendered ({exc}).", file=sys.stderr)
        sys.exit(1)
    print(text)
    write_report_file(args, text, markdown, _import_done(args))


#: The `import` flags only `requirements` reads, refused by name by `model` and `component`.
_ONLY_FOR_IMPORT_REQUIREMENTS = (("mapping", "--mapping"), ("baseline", "--baseline"))


def _refuse_requirements_flags(args: argparse.Namespace) -> None:
    """Refuse ``--mapping`` and ``--baseline`` on ``import model`` and ``import component``, above the pin.

    Tested against ``None``: a blank value is still the wrong form's flag.
    """
    for attribute, flag in _ONLY_FOR_IMPORT_REQUIREMENTS:
        if getattr(args, attribute) is not None:
            print(f"Error: {flag} applies only to 'import requirements'.", file=sys.stderr)
            sys.exit(1)


#: The `import` flags `requirements` refuses by name, each with the form it belongs to.
_NOT_FOR_IMPORT_REQUIREMENTS = (
    ("packages", "--packages", "import model"),
    ("force", "--force", "import model"),
    ("verbose", "--verbose", "import model"),
    ("update", "--update", "import component"),
    ("replace_access_lists", "--replace-access-lists", "import component"),
)


def cmd_import_requirements(args: argparse.Namespace) -> None:
    """Import one requirements interchange file into an existing project (``swcraft import requirements``).

    Usage refusals come before the pin (missing ``--source``, a sibling's flag, a blank or
    lone-surrogate ``--baseline``, a missing ``--report`` directory). A pin mismatch refuses, ``--dry-run``
    included. Then, in order: a ``--mapping`` inside the model root is refused, the mapping and the
    source are read, the delivery version is decided (``--baseline``, else the file's
    ``current_version``, else refused), the plan and the report are computed, the plan is applied unless
    ``--dry-run``, and only then is the report printed: never ahead of a rolled-back apply, never
    rendered after a committed one.

    Exit 0 on an applied or dry run, 1 on any refusal. Runs no ``check-model`` and writes no audit
    record.
    """
    guard_project_flag(args, "import requirements")
    if not args.source:
        print(
            "Error: --source is required.  Run: swcraft import requirements --source FILE --project PATH",
            file=sys.stderr,
        )
        sys.exit(1)
    for attribute, flag, form in _NOT_FOR_IMPORT_REQUIREMENTS:
        if getattr(args, attribute):
            print(f"Error: {flag} applies only to '{form}'.", file=sys.stderr)
            sys.exit(1)
    if args.baseline is not None:
        if not args.baseline.strip():
            print("Error: --baseline must not be blank.", file=sys.stderr)
            sys.exit(1)
        # POSIX argv decodes an undecodable byte as a lone surrogate, which no output can carry.
        try:
            no_lone_surrogate(args.baseline)
        except ValueError as exc:
            print(f"Error: --baseline {exc}.", file=sys.stderr)
            sys.exit(1)
    refuse_missing_report_directory(args)

    project = resolve_project(args)
    mapping = DEFAULT_MAPPING
    if args.mapping is not None:
        # The loader would read it as an element. Compared resolved (relative spellings and symlinks
        # reached); a case-only difference on a case-insensitive file system is not.
        if Path(args.mapping).resolve().is_relative_to(Path(project.resolved_model_root).resolve()):
            print(
                f"Error: {args.mapping}: the mapping file lies inside the model root, "
                "where the model's loader would read it as an element.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            mapping = read_mapping(Path(args.mapping))
        except MappingError as exc:
            print(f"Error: {args.mapping}: {exc}", file=sys.stderr)
            sys.exit(1)
    try:
        selected = read_needs_file(Path(args.source), mapping)
    except InterchangeError as exc:
        print(f"Error: {args.source}: {exc}", file=sys.stderr)
        sys.exit(1)
    baseline = delivery_version(selected, args.baseline)
    if baseline is None:
        print(f"Error: {args.source}: {NO_DELIVERY_VERSION}.", file=sys.stderr)
        sys.exit(1)
    try:
        plan = plan_requirements_import(project.resolved_model_root, selected, baseline=baseline)
    except Exception as exc:  # noqa: BLE001 -- entry: an import that cannot be planned is an Error: line and exit 1
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)
    # The report names a mapping inside the project relative to it; the refusals above keep the spelling.
    named = None if args.mapping is None else mapping_as_named(args.mapping, Path(project.resolved_project_root))
    # Rendered before the apply, printed after: a render failure then refuses with nothing written.
    try:
        text = render_report(plan, dry_run=args.dry_run, mapping=named)
        markdown = wanted_markdown_report(
            args, lambda: render_report_markdown(plan, dry_run=args.dry_run, mapping=named)
        )
    except Exception as exc:  # noqa: BLE001 -- entry: a report that cannot be rendered refuses before anything is written
        print(f"Error: the import's report cannot be rendered, so nothing was written ({exc}).", file=sys.stderr)
        sys.exit(1)
    if not args.dry_run:
        try:
            apply_requirements_import(plan)
        except Exception as exc:  # noqa: BLE001 -- entry: a failed apply is an Error: line, its rollback notes and exit 1
            print(f"Error: {error_text(exc)}", file=sys.stderr)
            # A path the rollback could not restore is only in a note; the user must be told.
            for note in getattr(exc, "__notes__", ()):
                print(note, file=sys.stderr)
            sys.exit(1)

    # The apply has committed: nothing below computes, the streams are UTF-8, and the write's
    # `OSError` is answered.

    print(text, end="")
    write_report_file(args, text, markdown, _import_done(args))


def add_import_parser(subparsers: argparse._SubParsersAction) -> None:
    """Declare ``swcraft import``: the source format, the source file, the project and each format's options."""
    import_parser = subparsers.add_parser(
        "import",
        help="Import an external architecture export into a SWCraft project "
        "(model: bulk into a fresh project; component: safe merge into an existing one; "
        "requirements: one requirements interchange file into an existing one)",
    )
    import_parser.add_argument(
        "format",
        choices=["model", "component", "requirements"],
        metavar="FORMAT",
        help="Source format: 'model' (whole-export bulk import, fresh project only), "
        "'component' (one component-scoped ARXML export, existing project) "
        "or 'requirements' (one requirements interchange file, existing project)",
    )
    import_parser.add_argument(
        "--source",
        default=None,
        metavar="FILE",
        help="Path to the ARXML export file, or for 'requirements' the requirements interchange file (required)",
    )
    import_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Path to swcraft.yaml manifest of the target project (required; "
        "must be fresh for 'model', may be non-empty for 'component' and 'requirements')",
    )
    import_parser.add_argument(
        "--packages",
        default=None,
        metavar="P1,P2,...",
        help="model only: comma-separated package selection (dependency closure "
        "applied automatically); default imports the whole export",
    )
    import_parser.add_argument(
        "--report",
        default=None,
        metavar="PATH",
        help="Also write the human-readable import report to this file (model, component and requirements alike); "
        "for component and requirements a PATH ending in .md gets the report as Markdown, any other PATH the text "
        "printed; model always writes the text",
    )
    import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print the report without writing any files (model, component and requirements alike)",
    )
    import_parser.add_argument(
        "--force",
        action="store_true",
        help="model only: allow importing into a non-empty project model (no merge protection)",
    )
    import_parser.add_argument(
        "--verbose",
        action="store_true",
        help="model only: list every skipped element under its reason; by default the report "
        "prints one line per reason with its count",
    )
    import_parser.add_argument(
        "--update",
        action="store_true",
        help="component only: explicitly replace the target's own divergent definition of "
        "the imported SWC (only that one file; a semantic diff preview is included in the "
        "report). Never affects a divergent shared interface/data type — that always aborts. "
        "Without it, an import whose only conflict is that divergent component exits with status 3 "
        "instead of 1, so a script can tell the refusal this option resolves from every other.",
    )
    import_parser.add_argument(
        "--replace-access-lists",
        action="store_true",
        help="component only, with --update: also take the source export's runnable read/write/call "
        "lists, even though the export does not say whether it can express them. Without this the "
        "component's existing lists are kept and the report says so.",
    )
    import_parser.add_argument(
        "--mapping",
        default=None,
        metavar="FILE",
        help="requirements only: a YAML file mapping the requirements team's need types and field names "
        "to SWCraft's; it must lie outside the model directory. Without it, SWCraft's own names apply",
    )
    import_parser.add_argument(
        "--baseline",
        default=None,
        metavar="TEXT",
        help="requirements only: the version of this requirements delivery (the team's baseline or "
        "commit), recorded on every requirement it holds; it wins over the file's own version. "
        "Without it, the file's version is used, and a file with none is refused",
    )
