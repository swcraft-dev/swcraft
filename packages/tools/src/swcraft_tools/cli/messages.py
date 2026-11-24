"""The sentences the CLI prints for a failure: read failures, OS and YAML errors, findings."""

from __future__ import annotations

import sys

import yaml
from pydantic import ValidationError

from swcraft_tools.model_state import ElementParseError, ModelRootNotFoundError
from swcraft_tools.project import (
    MANIFEST_FILENAME,
    ManifestNotFoundError,
    ManifestPathError,
    ManifestRootWrittenAbsoluteError,
    ManifestShapeError,
)
from swcraft_tools.validator import ConsistencyResult


def print_findings(result: ConsistencyResult) -> None:
    """A model check's findings on stderr: every error, then every warning, then one total line.

    A finding is a diagnostic, not the answer, so it goes to stderr. The prefix states the rule's
    class, never the verdict. The total line is printed only when there is a finding.
    """
    for issue in result.issues:
        print(f"  ERROR: {issue}", file=sys.stderr)
    for warning in result.warnings:
        print(f"  WARNING: {warning.message}", file=sys.stderr)
    if result.issues or result.warnings:
        print(f"{len(result.issues)} error(s), {len(result.warnings)} warning(s).", file=sys.stderr)


def error_text(exc: BaseException) -> str:
    """One refusal as this terminal prints it, every location in full.

    An exception with a ``render`` method gives its ``full_path=True`` form, any other its own text. An
    :class:`~swcraft_tools.model_state.ElementParseError` gives one line per field, joined by
    ``\\nError: `` because every caller prints ``Error: `` before the result.
    """

    if isinstance(exc, ElementParseError):
        return "\nError: ".join(exc.render_lines(full_path=True))
    render = getattr(exc, "render", None)
    return render(full_path=True) if callable(render) else str(exc)


# ``swcraft passport``'s read-failure sentences, stricter than :func:`error_text`: they echo no value of
# the project and nothing of the model.


def is_bare_os_error(exc: BaseException) -> bool:
    """Whether *exc* is Python's own ``OSError`` (or a builtin subclass), whose ``filename`` is a location."""
    return isinstance(exc, OSError) and type(exc).__module__ == "builtins"


def passport_project_failure(exc: BaseException) -> str:
    """``swcraft passport``'s refusal of a manifest or project it could not read, echoing nothing of it.

    Every exception is :func:`read_failure_sentence`'s, which names an unknown one by its class alone.
    """
    return f"the project could not be read: {read_failure_sentence(exc)}"


def read_failure_sentence(exc: BaseException) -> str:
    """The sentence for a project or model that could not be read, echoing no value of the project.

    ``swcraft passport``'s stderr travels with a bug report, so it is stricter than the CLI's
    ``render(full_path=True)``: a refusal names the manifest by ``swcraft.yaml``, a field by its
    location and the failure by its type or class, never a value, a user-chosen file name or an
    exception's own text (libyaml and pydantic quote the input; an OS error's ``filename`` is a
    location). Any other exception is its class alone. Not reached: which value it was (the reporter
    finds it in their own manifest).
    """

    if isinstance(exc, yaml.YAMLError):
        return f"{MANIFEST_FILENAME} does not parse: {_yaml_failure(exc)}"
    if isinstance(exc, ManifestNotFoundError):
        return "the manifest --project names does not exist; its name is withheld"
    if is_bare_os_error(exc):
        return os_failure(exc)
    if isinstance(exc, ValidationError):
        errors = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '(the manifest)'}: {error['type']}"
            for error in exc.errors()
        )
        return f"{exc.title} is not valid: {errors}"
    if isinstance(exc, ManifestPathError):
        return exc.render(full_path=False)
    if isinstance(exc, ModelRootNotFoundError):
        return exc.render(full_path=False)
    if isinstance(exc, UnicodeDecodeError):
        return f"{MANIFEST_FILENAME} is not UTF-8 text"
    if isinstance(exc, ManifestRootWrittenAbsoluteError):
        return (
            f"Manifest field '{exc.field}' is written as an absolute path, its value withheld. The two "
            "roots `swcraft clean-outputs` removes must be written relative to the project directory."
        )
    if isinstance(exc, ManifestShapeError):
        return f"{MANIFEST_FILENAME} is not a mapping"
    return f"{type(exc).__name__}, its text withheld"


def _yaml_failure(exc: yaml.YAMLError) -> str:
    """A libyaml refusal by its class and, when it has one, its position: never the text it quotes."""
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return type(exc).__name__
    return f"{type(exc).__name__} at line {mark.line + 1}, column {mark.column + 1}"


def model_read_failure_sentence(exc: BaseException) -> str:
    """The sentence for a model that could not be read, naming nothing of the model.

    Stricter than :func:`read_failure_sentence`: an unparseable element file is named by its layer's
    label and the error types only (its storage name is the element's name), any other refusal by its
    class. Not reached: which file it was; ``swcraft check-model`` finds it.
    """

    if isinstance(exc, ElementParseError):
        cause = exc.__cause__
        if isinstance(cause, ValidationError):
            reason = ", ".join(sorted({error["type"] for error in cause.errors()}))
        elif isinstance(cause, yaml.YAMLError):
            reason = _yaml_failure(cause)
        else:
            reason = "its text withheld"
        return f"a {exc.label} file is not valid ({reason}); its name and its content are withheld"
    if isinstance(exc, ModelRootNotFoundError) or is_bare_os_error(exc):
        return read_failure_sentence(exc)
    return f"{type(exc).__name__}, its text withheld: it may name an element of the model"


def os_failure(exc: BaseException) -> str:
    """An OS error by its class and the operating system's reason: never its ``filename``, a location."""
    return f"{type(exc).__name__}: {getattr(exc, 'strerror', None) or 'the file could not be read'}"


#: The C0 control characters, DEL and the C1 control characters: what a path printed on one stdout line must not hold.
CONTROL_CHARACTERS = frozenset(map(chr, (*range(0x20), *range(0x7F, 0xA0))))


def path_free_reason(exc: Exception) -> str:
    """Why a report step failed, naming no path: an ``OSError``'s ``strerror``, else the type's name.

    ``str(exc)`` and :func:`error_text` can carry a temporary file's absolute name or a model file's path.
    """
    return exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
