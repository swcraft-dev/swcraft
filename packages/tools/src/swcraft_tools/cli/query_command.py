"""``swcraft query``: its parser, the request it builds from the arguments, and its handler."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

from swcraft_tools.cli.messages import error_text
from swcraft_tools.cli.project_args import guard_project_flag, resolve_project
from swcraft_tools.model_registry import current_registry

# Imported as modules: several names are ambiguous bare (`ABSENT`, `Clause`, `related`).
from swcraft_tools.query import answer as query_answer
from swcraft_tools.query import edges as query_edges
from swcraft_tools.query import formatting as query_formatting
from swcraft_tools.query import request as query_request
from swcraft_tools.query import slicing as query_slicing
from swcraft_tools.snapshot import load_snapshot
from swcraft_tools.validator import DERIVED, derive_rows


def _query_value(raw: str) -> Any:
    """One command-line value, read as JSON where it is JSON and as a string otherwise.

    So ``size=32`` reaches an ``int`` field and a composite ``--after`` cursor is an object. Not reached:
    a string that is valid JSON is read as JSON (``name=null`` is null); send ``'"null"'`` for the text.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _query_clauses(args: argparse.Namespace) -> list[query_request.Clause]:
    """The ``--where`` then ``--exists`` arguments as one AND-composed clause list, in a stable order."""
    clauses = []
    for raw in args.where or ():
        name, separator, value = raw.partition("=")
        if not separator or not name:
            raise query_request.QueryError(f"--where {raw!r}: expected FIELD=VALUE")
        clauses.append(query_request.Clause(field=name, equals=_query_value(value)))
    clauses.extend(query_request.Clause(field=name, exists=True) for name in args.exists or ())
    return clauses


def _refuse_unanswerable_argv(args: argparse.Namespace) -> None:
    """The refusal of the argv shapes ``argparse`` admits and ``query`` cannot answer.

    Request refusals, decided before the manifest is resolved so they are reported as themselves.
    Raises rather than exits, so every refusal reaches ``cmd_query``'s one ``Error:`` path.

    Raises:
        query.request.QueryError: ``--request`` is given beside any argument that
            states a slice or a walk itself; the LAYER and the walk are given
            together or neither is; a slice argument is given beside a walk;
            ``--depth`` is given beside a LAYER; ``--after`` is given without a
            ``--limit``.
    """
    if args.request is not None:
        # Refused by name, never merged: the protocol's `read` has no merge rule.
        beside = [
            flag
            for flag, given in (
                # Presence, not truthiness: `--members ""` was given.
                ("LAYER", args.layer is not None),
                ("--where", args.where is not None),
                ("--exists", args.exists is not None),
                ("--members", args.members is not None),
                ("--form", args.form is not None),
                ("--limit", args.limit is not None),
                ("--after", args.after is not None),
                ("--dependents", args.dependents is not None),
                ("--deps", args.deps is not None),
                ("--depth", args.depth is not None),
            )
            if given
        ]
        if beside:
            raise query_request.QueryError(
                f"--request FILE carries the whole request; {', '.join(beside)} cannot be given beside it"
            )
        return
    # Truthiness from here on: `--dependents ""` is no walk.
    named = [flag for flag, value in (("--dependents", args.dependents), ("--deps", args.deps)) if value]
    if bool(args.layer) == bool(named):
        # Neither given: also name the request file.
        raise query_request.QueryError(
            "swcraft query takes either a LAYER to slice or one of --dependents/--deps NAME to walk"
            + ("" if args.layer else " (or the whole request as --request FILE)")
        )
    if named and (
        args.where or args.exists or args.members or args.form or args.limit is not None or args.after is not None
    ):
        raise query_request.QueryError(
            f"{named[0]} walks the reference graph; --where, --exists, --members, --form, --limit and --after "
            "slice a LAYER and do not apply to it"
        )
    if args.layer and args.depth is not None:
        raise query_request.QueryError("--depth bounds a walk; it does not apply to a LAYER slice")
    if args.after is not None and args.limit is None:
        # An unbounded page is the whole-set read paging replaces; the protocol refuses it too.
        raise query_request.QueryError("--after continues a page, so it needs --limit")


def _refuse_json_constant(constant: str) -> NoReturn:
    """``json.loads``' ``parse_constant``: ``NaN`` and the two infinities are not JSON (RFC 8259)."""
    raise query_request.QueryError(f"{constant} is not a JSON value")


def _finite_json_number(text: str) -> float:
    """``json.loads``' ``parse_float``: refuse a number that would parse to an infinity.

    So every refusal of the file's text is decided before the manifest is read.
    """
    value = float(text)
    if abs(value) == float("inf"):  # a JSON number literal is never NaN
        raise query_request.QueryError(f"{text} is beyond the range of a finite number")
    return value


def _read_query_request(spelled: str) -> query_request.QueryRequest:
    """``--request FILE``, read and parsed by the kernel, or the refusal naming the file as given.

    An argument refusal (unreadable, not UTF-8 JSON, not a read request) is decided before the manifest
    is resolved and exits 1 whatever the project.
    """
    try:
        # A leading byte order mark is skipped (RFC 8259 allows it; Windows PowerShell 5.1 writes one).
        text = Path(spelled).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise query_request.QueryError(
            f"--request {spelled}: not UTF-8 text ({exc.reason} at byte {exc.start})"
        ) from exc
    except OSError as exc:
        raise query_request.QueryError(f"--request {spelled}: cannot read the file ({exc.strerror or exc})") from exc
    try:
        data = json.loads(text, parse_constant=_refuse_json_constant, parse_float=_finite_json_number)
    except json.JSONDecodeError as exc:
        raise query_request.QueryError(
            f"--request {spelled}: not a JSON document ({exc.msg} at line {exc.lineno}, column {exc.colno})"
        ) from exc
    except (ValueError, RecursionError) as exc:  # ours, an over-long integer literal, or nesting too deep
        raise query_request.QueryError(f"--request {spelled}: not a JSON document ({exc})") from exc
    try:
        return query_request.parse_request(data)
    except query_request.QueryError as exc:
        raise query_request.QueryError(f"--request {spelled}: {exc}") from exc


def cmd_query(args: argparse.Namespace) -> None:
    """``swcraft query``: filtered, sliced and paged slices of one layer, or one bounded walk.

    Slices come in the read order (kind rank, then reader order), unlike ``list``'s reader order.

    Read-only: a successful run exits 0, an empty answer included. Non-zero: a project it cannot read
    (one unparseable element file included), and a request it cannot answer, for two causes. The
    model's declarations cannot answer it: an undeclared ``--where``/``--exists`` field, ``--members``
    naming no collection, an ``--after`` cursor the slice does not carry, a ``--request`` naming no
    declared layer, a ``derived`` name outside the vocabulary or computed at another set, a ``join``
    path naming no value of its row, a ``where`` over a computed value not asked for, naming no value
    of it, or asking a set-valued one for no value (``exists: false``, ``equals: null``). Or the
    arguments refuse it: a ``--limit`` or ``--depth`` below 1, an unreadable or malformed ``--request``
    file, the three combinations mixing the slice with the walk, and ``--request`` beside any slice or
    walk argument.

    ``--request FILE`` is the protocol ``read`` request minus ``operation``; one with ``derived`` or
    ``join`` answers identity rows. ``--form full`` widens a LAYER slice to every declared field.
    ``--limit``/``--after`` page a LAYER slice only.
    """
    guard_project_flag(args, "query")
    try:
        _refuse_unanswerable_argv(args)
        request = None if args.request is None else _read_query_request(args.request)
    except Exception as exc:  # noqa: BLE001 -- entry: a request that cannot be read is an Error: line and exit 1
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)
    # After the argument refusals, so those are decided whatever the project.
    project = resolve_project(args)

    try:
        snapshot = load_snapshot(project.resolved_model_root, rev=None)
        if request is not None:
            try:
                result = query_answer.answer_request(snapshot.state, request, vocabulary=DERIVED, derive=derive_rows)
            except query_request.QueryError as exc:
                # Name the file, as every refusal of its shape does.
                raise query_request.QueryError(f"--request {args.request}: {exc}") from exc
            rendered = (
                query_formatting.format_query_json(result)
                if args.format == "json"
                else query_formatting.format_query(result)
            )
        elif args.layer:
            result = query_slicing.slice_layer(
                snapshot.state,
                args.layer,
                where=_query_clauses(args),
                members=args.members,
                limit=args.limit,
                after=query_request.ABSENT if args.after is None else _query_value(args.after),
                form=args.form or query_request.DEFAULT_QUERY_FORM,
            )
            rendered = (
                query_formatting.format_query_json(result)
                if args.format == "json"
                else query_formatting.format_query(result)
            )
        else:
            direction = "dependents" if args.dependents else "dependencies"
            name = args.dependents or args.deps
            walk = query_edges.related(snapshot.state, name, direction, args.depth)
            renderer = (
                query_formatting.format_related_json if args.format == "json" else query_formatting.format_related
            )
            rendered = renderer(name, walk, direction=direction, depth=args.depth)
    except (OSError, ValueError) as exc:
        # `QueryError` and parse refusals are `ValueError`s; `OSError` is an absent model root.
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)

    sys.stdout.write(rendered)


def add_query_parser(subparsers: argparse._SubParsersAction) -> None:
    """The ``swcraft query`` parser: a layer slice or a walk, its filters, paging and request file, project, format."""
    # LAYER's choices come from the registry per run, as `list`'s do.
    query_parser = subparsers.add_parser(
        "query",
        help=(
            "Filtered, member-sliced and paged slices of one layer, or one bounded walk "
            "(read-only; a project it cannot read and a request it cannot answer both exit 1)"
        ),
    )
    query_parser.add_argument(
        "layer",
        nargs="?",
        default=None,
        metavar="LAYER",
        choices=current_registry().display_order,
        help="The layer to slice (omit it and give --dependents or --deps NAME to walk the reference graph)",
    )
    # Refused beside a LAYER in `cmd_query`: argparse cannot express that against a positional.

    walk_group = query_parser.add_mutually_exclusive_group()
    walk_group.add_argument(
        "--dependents",
        default=None,
        metavar="NAME",
        help=(
            "Walk to the elements that depend on NAME (grouped by the matched element's layer, named by "
            "its singular token — `interface`, not the `interfaces` a LAYER slice takes)"
        ),
    )
    walk_group.add_argument(
        "--deps",
        default=None,
        metavar="NAME",
        help="Walk to the elements NAME depends on (the same walk, the same layer tokens, the other direction)",
    )
    query_parser.add_argument(
        "--where",
        action="append",
        default=None,
        metavar="FIELD=VALUE",
        help="Keep the elements whose declared FIELD equals VALUE (repeatable; the clauses are AND-composed)",
    )
    query_parser.add_argument(
        "--exists",
        action="append",
        default=None,
        metavar="FIELD",
        help=(
            "Keep the elements that carry a value for the declared FIELD; null and the empty string "
            "are no value (repeatable; AND-composed)"
        ),
    )
    query_parser.add_argument(
        "--members",
        default=None,
        metavar="FIELD",
        help=(
            "Answer every member of the declared collection FIELD across the layer, beside its owner; "
            "a dotted path of collections (runnables.writes) also names the members it passes through"
        ),
    )
    query_parser.add_argument(
        "--form",
        choices=query_request.QUERY_FORMS,
        default=None,
        metavar="FORM",
        help=(
            "How wide each row is: `index` (the identity; the default) or `full` (every declared field, "
            "as the MCP `read` answers at `full`)"
        ),
    )
    query_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Answer at most N rows, with the total and a cursor to continue past (LAYER slices only)",
    )
    query_parser.add_argument(
        "--after",
        default=None,
        metavar="ID",
        help="Continue past the identity ID, which a previous page answered as its cursor (needs --limit)",
    )
    query_parser.add_argument(
        "--request",
        default=None,
        metavar="FILE",
        help=(
            "Read the whole request from FILE, a JSON object with the keys of the MCP read request minus "
            "`operation` (its address names a layer); given alone, beside no LAYER, slice or walk argument. "
            "A `where` clause may name `derived.NAME.PATH`, a value of a computed value the request asks for in "
            "`derived`; asking a set-valued one for no value (`exists: false`, `equals: null`) is refused"
        ),
    )
    query_parser.add_argument(
        "--depth",
        type=int,
        default=None,
        metavar="N",
        help="Bound a walk to N levels (default: the whole transitive walk; walks only)",
    )
    query_parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Path to swcraft.yaml manifest (required)",
    )
    query_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help=(
            "Output format: 'text' (default, one 'layer: identity' line, at --form full followed by one indented"
            " 'field: value' line per field) or 'json' (versioned, machine-readable)"
        ),
    )
