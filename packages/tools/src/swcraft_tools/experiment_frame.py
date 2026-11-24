"""The experiment frame's kind-independent pieces: the configuration file, the report name, the digest, the write.

Every experiment kind shares one frame: a configuration file read as bytes,
digested and parsed by the one YAML reader; a refusal that names its dotted key
and never a path; a report directory named by one directory segment; a digest of
the model's layer files; and a report written whole or not at all.
:mod:`swcraft_tools.allocation` keeps its own copies of these pieces; this module
holds them for every other kind, so a second kind never imports the first.

**What this does not reach:** the configuration's *shape* is each kind's own
pydantic model, and each kind wraps the exceptions raised here in its own error
family; this module names no kind and decides no exit code.
"""

from __future__ import annotations

import hashlib
import os
import urllib.parse
from collections.abc import Callable, Sequence
from pathlib import Path

import yaml
from pydantic import ValidationError

from swcraft_tools.exclusive_create import create_unique_sibling
from swcraft_tools.model_registry import current_registry, reserved_device_name, segment_refusal
from swcraft_tools.model_state import scan_layer, stored_element_files
from swcraft_tools.yaml_load import load_yaml_document

__all__ = [
    "EXPERIMENTS_DIRECTORY",
    "ConfigDocumentError",
    "layer_link_targets",
    "model_digest",
    "read_config_document",
    "refusal_lines",
    "report_name_refusal",
    "write_report_files",
]

#: The directory under the dist root every kind's default report directory sits in.
EXPERIMENTS_DIRECTORY = "experiments"


class ConfigDocumentError(ValueError):
    """The configuration file's text is not YAML: the sentence names the parser's problem, never the path."""


def read_config_document(path: str | Path) -> tuple[object, str]:
    """The configuration file at *path*, parsed: ``(document, "sha256:<hex>")``.

    The digest is of the bytes exactly (a CRLF copy digests differently and
    parses equal); they are decoded as strict UTF-8 and parsed by
    :func:`~swcraft_tools.yaml_load.load_yaml_document`. ``None`` -- an empty
    file, or one of comments only -- is returned as it is.

    Raises:
        OSError: the file cannot be read.
        UnicodeDecodeError: the bytes are not UTF-8.
        ConfigDocumentError: the text is not YAML.
    """
    data = Path(path).read_bytes()
    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    text = data.decode("utf-8")
    try:
        document = load_yaml_document(text)
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None) or "it does not parse"
        raise ConfigDocumentError(f"the configuration file is not valid YAML: {problem}") from None
    return document, digest


_PYDANTIC_VALUE_ERROR = "Value error, "


def refusal_lines(error: ValidationError) -> list[str]:
    """One line per pydantic error, each naming its dotted key and never the input's location on disk."""
    lines: list[str] = []
    for item in error.errors():
        message = str(item["msg"])
        if message.startswith(_PYDANTIC_VALUE_ERROR):
            message = message[len(_PYDANTIC_VALUE_ERROR) :]
        key = ".".join(str(part) for part in item["loc"])
        lines.append(f"'{key}': {message}" if key else message)
    return lines


def report_name_refusal(value: str) -> str | None:
    """Why *value* cannot name a report directory, or ``None`` when it can.

    One directory segment, never a device name Windows reserves (asked on every
    platform), and no trailing space or dot, which Windows strips.
    """
    refusal = segment_refusal(value, "name", directory=True)
    if refusal is not None:
        return refusal
    device = reserved_device_name(value)
    if device is not None:
        return (
            f"name '{value}' names a device Windows reserves ({device}): the report directory "
            "could not exist on Windows"
        )
    if value.endswith((" ", ".")):
        return (
            f"name '{value}' ends in a space or a dot, which Windows strips from a directory name: "
            "the report directory would not carry the name"
        )
    return None


def model_digest(model_root: str | Path) -> tuple[str, int]:
    """A framed sha256 over every layer file under *model_root*, and the number of files.

    The files are the ones the readers list (``scan_layer`` per declared
    layer), each recorded as its model-root-relative POSIX name, its length and
    its content's sha256, length-framed so file names and boundaries take part.
    **What it does not reach:** a stray file beside the model is not read, and
    the digest is a record of the files when it was taken, never a lock on them.
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


def layer_link_targets(model_root: str | Path, layer: str, directory: Path) -> dict[str, str]:
    """``name -> relative target`` for every element of *layer* declared by exactly one file.

    The target is the element's file relative to the report *directory*,
    POSIX-spelled with every segment percent-encoded, so a Markdown preview
    opens it from there. A name more than one file declares maps to nothing.
    What this does not reach: a report copied elsewhere has dead links.
    """
    targets: dict[str, str] = {}
    for name, files in stored_element_files(Path(model_root), layer, registry=current_registry()).items():
        if len(files) != 1:
            continue
        relative = os.path.relpath(files[0].path, directory)
        targets[name] = "/".join(urllib.parse.quote(segment) for segment in Path(relative).parts)
    return targets


#: The suffix of a report file being written, before it is renamed into place.
_PARTIAL_SUFFIX = ".partial"


def write_report_files(
    directory: Path,
    files: Sequence[tuple[Path, str]],
    before_replace: Callable[[], None] | None = None,
    *,
    after_replace: Callable[[Path], None] | None = None,
) -> None:
    """Write every ``(path, text)`` of *files* under a temporary name, then rename each into place in order.

    *before_replace*, when given, is called once every temporary file is written
    and before the first rename; an exception it raises replaces nothing.
    *after_replace*, when given, is called with each target path right after its
    rename, so a caller learns how far a failed run got.

    Each temporary name (``<name>.<random>.partial``) is unique to this call, so
    a stale ``*.partial`` left by a killed run is neither read nor overwritten.
    The files get ``0o666`` less the umask. A failure while writing removes every
    temporary file this call made and replaces nothing.

    What this does not reach: a rename the operating system refuses *between*
    two of the files (one held open on Windows) leaves a mixed set; there is no
    lock, so two concurrent runs whose renames interleave can do the same; a
    process killed before the ``finally`` leaves its ``*.partial`` behind; and
    the report directory this call created stays behind, empty, after a failed
    write.
    """
    directory.mkdir(parents=True, exist_ok=True)
    partial: list[tuple[Path, Path, str]] = []
    try:
        for path, text in files:
            partial.append((create_unique_sibling(directory, path.name, _PARTIAL_SUFFIX), path, text))
        for temporary, _path, text in partial:
            temporary.write_text(text, encoding="utf-8", newline="\n")
        if before_replace is not None:
            before_replace()
        for temporary, path, _text in partial:
            os.replace(temporary, path)
            if after_replace is not None:
                after_replace(path)
    finally:
        for temporary, _path, _text in partial:
            temporary.unlink(missing_ok=True)
