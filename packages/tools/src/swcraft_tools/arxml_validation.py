"""Hybrid structural/XSD validation for generated AUTOSAR ARXML.

The AUTOSAR XSDs are license-gated and not committed. With a schema available, validation uses it through
:mod:`xmlschema`; otherwise it checks that the document is well-formed XML with an ``AUTOSAR`` root element.

**The version is read from the file itself**, from the root element's ``xsi:schemaLocation``, so
``python -m swcraft_tools.arxml_validation <path>`` needs no version argument:

===========================  =================  ===========================================
File declares                Schema available   Outcome
===========================  =================  ===========================================
a supported version          yes                XSD validation against that version
a supported version          no                 structural validation
nothing (no schemaLocation)  --                 structural validation, no error
an unrecognized version      --                 ``False``, with a message naming the version
malformed XML                --                 ``False``
===========================  =================  ===========================================

A file with no ``xsi:schemaLocation`` is never XSD-validated; one of an unrecognized version is checked
structurally only through ``schema_path`` or with the declaration stripped.

**Schema resolution**: an explicit ``schema_path`` wins; then ``SWCRAFT_AUTOSAR_XSD_PATH``, only when its file
name is the one the declared version expects; then the version's file in a source checkout's ``schemas/``
(``AUTOSAR_4-2-2.xsd``); an installed package has no default. A configured schema of another version, or a
missing path, degrades to structural validation with a warning. File names match case-insensitively through
symlinks; a vendor-renamed schema is not identified by its content.

**An XSD failure says why**: ``ValidationOutcome.messages`` carries capped ``xmlschema`` diagnostics (element path
and reason); a schema that cannot be compiled is a toolchain fault, in ``ValidationOutcome.schema_error``.
"""

from __future__ import annotations

import os
import re
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

import xmlschema
from lxml import etree
from xmlschema.exceptions import XMLSchemaWarning

from swcraft_tools import env
from swcraft_tools.arxml_version import (
    SCHEMA_FILENAMES,
    SUPPORTED_VERSIONS,
    XSI_NAMESPACE,
    ArxmlVersion,
    release_of_schema_filename,
    schema_filename_from_location,
)

AUTOSAR_ROOT_LOCAL_NAME = "AUTOSAR"
SCHEMA_PATH_ENV = "SWCRAFT_AUTOSAR_XSD_PATH"
DEFAULT_SCHEMA_RECURSION_LIMIT = 100_000
SCHEMA_RECURSION_LIMIT_ENV = "SWCRAFT_XML_SCHEMA_RECURSION_LIMIT"

#: How many ``xmlschema`` diagnostics a failing XSD check reports; the AUTOSAR schema can produce hundreds for
#: one misplaced element, and the report says how many were suppressed.
MAX_SCHEMA_ERRORS = 10

#: The ``schemaLocation`` attribute carrying the declared version. Its namespace comes from ``arxml_version``,
#: not restated here, so the attribute the writer sets and the one read back cannot drift apart.
SCHEMA_LOCATION_ATTRIBUTE = f"{{{XSI_NAMESPACE}}}schemaLocation"

#: The two mode strings ``main()`` reports and :class:`ValidationOutcome` carries.
MODE_XSD = "XSD"
MODE_STRUCTURAL = "structural"


#: The default schema directory: ``<checkout>/schemas`` in a source checkout,
#: ``None`` in an installed package, which has no default schema.
#: ``SWCRAFT_AUTOSAR_XSD_PATH`` is how an installed user configures one.
_CHECKOUT_ROOT = env.checkout_root()
SCHEMA_DIR: Path | None = None if _CHECKOUT_ROOT is None else _CHECKOUT_ROOT / "schemas"

#: Version -> default XSD path; empty in an installed package. Schema resolution reads this mapping.
DEFAULT_SCHEMA_PATHS: dict[ArxmlVersion, str] = (
    {} if SCHEMA_DIR is None else {version: str(SCHEMA_DIR / name) for version, name in SCHEMA_FILENAMES.items()}
)

#: ``AUTOSAR_4-1-1.xsd`` -> ``4.1.1``, so an unsupported declaration is reported as a version. Only the dashed
#: form is matched: it is what every 4.x release uses (the numbered ``AUTOSAR_000NN.xsd`` form starts with AP
#: R17-03), and an unmatched filename is reported verbatim.
_DASHED_SCHEMA_FILENAME = re.compile(r"^AUTOSAR_(\d+)-(\d+)-(\d+)\.xsd$", re.IGNORECASE)


@dataclass(frozen=True)
class ValidationOutcome:
    """What :func:`check_arxml` did and what it concluded.

    ``mode`` is the mode the file **actually got**. ``schema_error`` separates *the schema is unusable* from
    *the document is invalid*; both are ``valid=False``, since a gate that cannot check a file must not pass it.
    ``messages`` are the human-readable reasons: the ``xmlschema`` diagnostics (capped at
    :data:`MAX_SCHEMA_ERRORS`), the compilation error, or a resolution warning, already written to stderr.
    """

    valid: bool
    mode: str
    version: ArxmlVersion | None = None
    schema_path: str | None = None
    messages: tuple[str, ...] = ()
    schema_error: bool = False


@dataclass(frozen=True)
class _SchemaCheck:
    """What one XSD check concluded, and why; ``schema_error`` means the schema, not the document, failed."""

    valid: bool
    messages: tuple[str, ...] = ()
    schema_error: bool = False


def _names_schema_file(configured_path: str, schema_filename: str) -> bool:
    """Whether *configured_path* names *schema_filename*, ignoring case and symlinks.

    A different case (``autosar_4-2-2.xsd``) and a symlink whose target carries the canonical name
    (``current.xsd -> AUTOSAR_4-2-2.xsd``) are accepted; a vendor-renamed copy
    is rejected.
    """
    wanted = schema_filename.casefold()
    candidates = {os.path.basename(configured_path), os.path.basename(os.path.realpath(configured_path))}
    return any(candidate.casefold() == wanted for candidate in candidates)


def _resolve_schema_path(version: ArxmlVersion | None) -> tuple[str | None, tuple[str, ...]]:
    """Apply the precedence rule; return the schema to use and any warnings.

    A ``None`` *version* (nothing declared) is structural regardless of which schema is configured. A
    configured ``SWCRAFT_AUTOSAR_XSD_PATH`` that does not exist or does not :func:`name <_names_schema_file>`
    the declared version's schema file yields ``(None, (warning,))`` — structural validation with a warning.
    """
    if version is None:
        return None, ()

    configured_path = os.environ.get(SCHEMA_PATH_ENV)
    if configured_path:
        if not os.path.isfile(configured_path):
            warning = (
                f"Warning: {SCHEMA_PATH_ENV}={configured_path} does not exist. Falling back to "
                f"structural validation; point {SCHEMA_PATH_ENV} at {version.schema_filename}, "
                "or unset it to use the per-version default, to XSD-validate this file."
            )
            return None, (warning,)
        if not _names_schema_file(configured_path, version.schema_filename):
            warning = (
                f"Warning: {SCHEMA_PATH_ENV}={configured_path} is not "
                f"{version.schema_filename}, the schema for the AUTOSAR {version} this file "
                "declares. Falling back to structural validation; point "
                f"{SCHEMA_PATH_ENV} at {version.schema_filename}, or pass an explicit schema "
                "path, to XSD-validate this file."
            )
            return None, (warning,)
        return configured_path, ()

    default_path = DEFAULT_SCHEMA_PATHS.get(version)
    if default_path is not None and os.path.isfile(default_path):
        return default_path, ()
    return None, ()


def _schema_recursion_limit() -> int:
    raw = os.environ.get(SCHEMA_RECURSION_LIMIT_ENV, "").strip()
    if not raw:
        return DEFAULT_SCHEMA_RECURSION_LIMIT
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_SCHEMA_RECURSION_LIMIT


def _parse(arxml_path: str) -> etree._ElementTree | None:
    """Parse the document, or ``None`` when it is not well-formed XML."""
    try:
        return etree.parse(arxml_path)
    except etree.XMLSyntaxError:
        return None


def _declared_schema_filename(tree: etree._ElementTree) -> str | None:
    """The XSD filename the root element's ``xsi:schemaLocation`` names, if any."""
    return schema_filename_from_location(tree.getroot().get(SCHEMA_LOCATION_ATTRIBUTE))


def _declared_version_label(schema_filename: str) -> str:
    match = _DASHED_SCHEMA_FILENAME.match(schema_filename)
    if match is None:
        return schema_filename
    return ".".join(match.groups())


def _is_well_formed_autosar(tree: etree._ElementTree) -> bool:
    return etree.QName(tree.getroot().tag).localname == AUTOSAR_ROOT_LOCAL_NAME


#: ``realpath`` -> ``((mtime_ns, size), compiled schema or the reason it is unusable)``. One entry per file,
#: replaced on change, so a long-lived server keeps no superseded compile of a large schema.
_SCHEMA_CACHE: dict[str, tuple[tuple[int, int], tuple[xmlschema.XMLSchema | None, str | None]]] = {}


def _schema_cache_key(schema_path: str) -> tuple[str, tuple[int, int]] | None:
    """Identity of the schema file on disk, or ``None`` when it cannot be stat'ed.

    ``(realpath, (mtime_ns, size))``: the realpath is the cache slot, so a symlink and its target share one,
    and the stamp invalidates an edited or replaced schema.
    """
    try:
        stat = os.stat(schema_path)
    except OSError:
        return None
    return (os.path.realpath(schema_path), (stat.st_mtime_ns, stat.st_size))


def _compile_schema(schema_path: str) -> tuple[xmlschema.XMLSchema | None, str | None]:
    """Compile *schema_path*, memoized; return ``(schema, None)`` or ``(None, error)``.

    Compiling the AUTOSAR XSD is the expensive half of validation, and the MCP server runs it in-process;
    failures are cached too. The AUTOSAR schema's inheritance depth overflows CPython's default recursion limit
    during compilation, so the raised limit is scoped to this call and restored at once.
    """
    key = _schema_cache_key(schema_path)
    if key is not None:
        realpath, stamp = key
        cached = _SCHEMA_CACHE.get(realpath)
        if cached is not None and cached[0] == stamp:
            return cached[1]

    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, _schema_recursion_limit()))
    try:
        # xmlschema warns "can't verify the content model ... due to exceeding of maximum recursion depth" on
        # the AUTOSAR schema: a statement about the schema's UPA check, not the document's verdict, which a
        # reader would take for a failure. Scoped because ``catch_warnings`` swaps the process-wide filter list.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"can't verify the content model of .* due to exceeding of maximum recursion depth",
                category=XMLSchemaWarning,
            )
            result: tuple[xmlschema.XMLSchema | None, str | None] = (xmlschema.XMLSchema(schema_path), None)
    except Exception as exc:  # noqa: BLE001 -- fallback: a bad file raises far more than XMLSchemaException
        result = (
            None,
            f"Error: the XML schema {schema_path} could not be compiled, so no XSD validation was "
            f"performed. This is a toolchain/configuration problem, not a problem with the model: "
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        sys.setrecursionlimit(old_limit)

    if key is not None:
        # Assignment drops the previous compile of this file: the whole eviction policy.
        _SCHEMA_CACHE[key[0]] = (key[1], result)
    return result


def _format_schema_errors(schema: xmlschema.XMLSchema, arxml_path: str) -> tuple[str, ...]:
    """Render up to :data:`MAX_SCHEMA_ERRORS` diagnostics for an invalid document.

    Each line is an element path and a reason. Iteration stops one past the cap, so a large file's error set
    is not walked whole.
    """
    rendered: list[str] = []
    extra = 0
    for error in schema.iter_errors(arxml_path):
        if len(rendered) >= MAX_SCHEMA_ERRORS:
            extra += 1
            break
        location = getattr(error, "path", None) or "(document root)"
        reason = getattr(error, "reason", None) or str(error)
        rendered.append(f"  {location}: {reason}")
    if extra:
        rendered.append(f"  ... and further errors (only the first {MAX_SCHEMA_ERRORS} are shown)")
    return tuple(rendered)


def _validate_against_schema(arxml_path: str, schema_path: str) -> _SchemaCheck:
    """XSD-validate *arxml_path*, reporting *why* it failed."""
    schema, compile_error = _compile_schema(schema_path)
    if schema is None:
        return _SchemaCheck(False, (compile_error,) if compile_error else (), schema_error=True)
    try:
        if schema.is_valid(arxml_path):
            return _SchemaCheck(True)
        messages = _format_schema_errors(schema, arxml_path)
    except xmlschema.XMLSchemaException as exc:
        return _SchemaCheck(False, (f"{type(exc).__name__}: {exc}",))
    header = f"{arxml_path} does not conform to {schema_path}:"
    return _SchemaCheck(False, (header, *messages))


def _report(outcome: ValidationOutcome) -> ValidationOutcome:
    """Emit an outcome's messages on stderr, once, and return it unchanged.

    Here rather than in ``main()``, so the bool-only :func:`validate_arxml` does not drop a warning.
    """
    for message in outcome.messages:
        print(message, file=sys.stderr)
    return outcome


def check_arxml(arxml_path: str, schema_path: str | None = None) -> ValidationOutcome:
    """Validate an ARXML file and report how it was validated.

    Args:
        arxml_path: the file to validate.
        schema_path: an explicit XSD to validate against, bypassing both version
            inference and the ``SWCRAFT_AUTOSAR_XSD_PATH``/default lookup; the file's own declaration is not
            consulted.

    Returns:
        The :class:`ValidationOutcome`; its ``messages`` have already been written to stderr.

    Raises:
        FileNotFoundError: If ``arxml_path`` does not exist.
    """
    if not os.path.isfile(arxml_path):
        raise FileNotFoundError(arxml_path)

    if schema_path is not None:
        check = _validate_against_schema(arxml_path, schema_path)
        return _report(
            ValidationOutcome(
                check.valid,
                MODE_XSD,
                schema_path=schema_path,
                messages=check.messages,
                schema_error=check.schema_error,
            )
        )

    tree = _parse(arxml_path)
    if tree is None:
        return _report(ValidationOutcome(False, MODE_STRUCTURAL))

    version: ArxmlVersion | None = None
    declared_filename = _declared_schema_filename(tree)
    if declared_filename is not None:
        version = release_of_schema_filename(declared_filename)
        if version is None:
            message = (
                f"Unsupported AUTOSAR version {_declared_version_label(declared_filename)} "
                f"declared by {arxml_path} (xsi:schemaLocation names {declared_filename}); "
                f"supported versions are: {', '.join(SUPPORTED_VERSIONS)}"
            )
            return _report(ValidationOutcome(False, MODE_STRUCTURAL, messages=(message,)))

    resolved_schema, messages = _resolve_schema_path(version)
    if resolved_schema is not None:
        check = _validate_against_schema(arxml_path, resolved_schema)
        return _report(
            ValidationOutcome(
                check.valid,
                MODE_XSD,
                version,
                resolved_schema,
                (*messages, *check.messages),
                check.schema_error,
            )
        )
    return _report(ValidationOutcome(_is_well_formed_autosar(tree), MODE_STRUCTURAL, version, None, messages))


def validate_arxml(arxml_path: str, schema_path: str | None = None) -> bool:
    """Return whether an ARXML file passes XSD or structural validation.

    Raises:
        FileNotFoundError: If ``arxml_path`` does not exist.
    """
    return check_arxml(arxml_path, schema_path).valid


def main(argv: list[str] | None = None) -> int:
    """Validate an ARXML file and return a process exit code."""
    env.load_env_file()

    args = sys.argv[1:] if argv is None else argv
    arxml_path = args[0] if args else os.path.join("dist", "model.arxml")

    outcome = check_arxml(arxml_path)
    verdict = "VALID" if outcome.valid else "INVALID"
    # ``builder.parse_moon_errors`` takes everything after the colon of the verdict line as the path, so the
    # schema-error note gets a line of its own, printed first so the verdict stays the last word.
    if outcome.schema_error:
        print(f"NOTE: the schema could not be compiled; see the error above ({arxml_path})")
    print(f"{verdict} ({outcome.mode} mode): {arxml_path}")
    return 0 if outcome.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
