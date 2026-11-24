"""The storage portability and unknown-key rules.

Checks ``storage.filename-case-collision``, ``storage.reserved-device-name``
(:func:`check_storage_portability`) and ``model.unknown-key`` (:func:`check_unknown_keys`).
Each check is a thin reader over the model view plus a pure function that builds the findings.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError

from swcraft_tools.diagnostics import Subject, SubjectStep, field_step, key_step, member_step
from swcraft_tools.model_registry import (
    KindFacts,
    ModelRegistry,
    current_registry,
    kind_of_loaded,
    loaded_member_identity,
    published_value,
    reserved_device_name,
)
from swcraft_tools.undeclared_keys import held_keys, undeclared_keys, unknown_key_clauses
from swcraft_tools.validator.model_view import ModelInput, as_view
from swcraft_tools.validator.results import ConsistencyIssue, ConsistencyResult, consistency_result
from swcraft_tools.yaml_load import load_yaml_document

# ---------------------------------------------------------------------------
# check_storage_portability
# ---------------------------------------------------------------------------


def check_storage_portability(model_root: ModelInput) -> ConsistencyResult:
    """Report every layer directory holding file names that differ only in case.

    ``storage.filename-case-collision``: ``float32.yaml`` and ``Float32.yaml`` are two files on
    ext4 and one on APFS and NTFS, so such a tree loses an element on a Mac or on Windows. Also
    reports ``storage.reserved-device-name``. The check changes nothing; renaming is the user's.

    Not a finding: a file name differing from the identity it declares; two identities differing
    only in case under file names that do not (``Float32.yaml`` beside ``float32~1.yaml``); one
    name in two layer directories. The message names the layer and the elements, never a file.

    What it can see is platform-bound:

    * On a case-insensitive filesystem the pair cannot exist on disk, so the rule fires only on
      a case-sensitive one.
    * A name the layer's suffix pattern does not match (``A.YAML`` beside ``a.yaml`` on ext4)
      is no model file and is not grouped.
    * It compares ``str.casefold()`` only: names differing in Unicode normalisation form are
      not reported.
    * Two directories differing only in case (``swc/a/`` beside ``swc/A/``) are not grouped.

    A file's identity is read only once it is in a colliding group, so a conforming model pays
    the listing and no parse.
    """
    view = as_view(model_root)
    registry = current_registry()
    stored = {layer: view.stored_paths(layer) for layer in registry.layer_names}
    return consistency_result(storage_portability_records(stored, view.declared_identity, registry=registry))


def storage_portability_records(
    stored: Mapping[str, Sequence[str]],
    identity_of: Callable[[str, str], str | None],
    *,
    registry: ModelRegistry | None = None,
) -> list[ConsistencyIssue]:
    """:func:`check_storage_portability`'s rule, over a listing already taken.

    Args:
        stored: ``{layer: model-root-relative POSIX paths}`` in listing order. A key the
            registry does not declare is not read.
        identity_of: ``(layer, path) -> the identity that file declares``, or ``None``. Asked
            only about a file in a colliding group or with a reserved name.
        registry: the model registry to read, or None for the one in effect.

    Returns:
        One ``storage.filename-case-collision`` finding per group of two or more element files
        of one directory whose names are equal under ``str.casefold()`` (layers in registry
        order, groups by first file listed), its subjects the identities declared, each once; a
        file declaring no identity is left out. Then, per layer, one
        ``storage.reserved-device-name`` finding per element file whose name before the first
        ``.`` is a device name Windows reserves
        (:func:`~swcraft_tools.model_registry.reserved_device_name`).
    """
    registry = registry or current_registry()
    records: list[ConsistencyIssue] = []
    for layer in registry.layer_names:
        groups: dict[tuple[str, str], list[str]] = {}
        reserved: list[tuple[str, str]] = []
        for path in stored.get(layer, ()):
            directory, _, name = path.rpartition("/")
            groups.setdefault((directory, name.casefold()), []).append(path)
            device = reserved_device_name(name)
            if device is not None:
                reserved.append((path, device))
        for paths in groups.values():
            if len(paths) < 2:
                continue
            declared = [identity for path in paths if (identity := identity_of(layer, path)) is not None]
            if len(declared) < 2:
                continue
            records.append(
                ConsistencyIssue(
                    rule="storage.filename-case-collision",
                    message=_case_collision_message(layer, declared),
                    # The elements, never the files: the protocol publishes the message.
                    subjects=tuple(Subject(layer, identity) for identity in dict.fromkeys(declared)),
                )
            )
        for path, device in reserved:
            identity = identity_of(layer, path)
            if identity is None:
                continue
            records.append(
                ConsistencyIssue(
                    rule="storage.reserved-device-name",
                    message=_reserved_device_name_message(layer, identity, device),
                    # The element the file declares, never the file: the protocol publishes the message.
                    subjects=(Subject(layer, identity),),
                )
            )
    return records


def _reserved_device_name_message(layer: str, identity: str, device: str) -> str:
    """``Element 'CON' of layer `x` is stored under …`` — the layer, the element and the device, never a file."""
    return (
        f"Element '{identity}' of layer `{layer}` is stored under a file name Windows reserves for the device "
        f"{device}, which no Windows filesystem can hold; rename the element and its file."
    )


def _case_collision_message(layer: str, declared: Sequence[str]) -> str:
    """``Elements 'a' and 'A' of layer `x` are stored …`` — the layer and the elements, never a file."""
    quoted = [f"'{identity}'" for identity in dict.fromkeys(declared)]
    if len(quoted) == 1:
        elements = f"Element {quoted[0]} of layer `{layer}` is"
    else:
        elements = f"Elements {', '.join(quoted[:-1])} and {quoted[-1]} of layer `{layer}` are"
    files = len(declared)
    repair = "rename one of the two files" if files == 2 else f"rename all but one of the {files} files"
    return (
        f"{elements} stored under file names that differ only in case, which a case-insensitive filesystem "
        f"holds as one file; {repair}."
    )


# ---------------------------------------------------------------------------
# check_unknown_keys
# ---------------------------------------------------------------------------


def check_unknown_keys(model_root: ModelInput) -> ConsistencyResult:
    """Report every key of an element file that no field of its kind declares.

    ``model.unknown-key``: a kind that does not forbid extra keys loads such a key and drops it,
    so ``derived___from:`` in place of ``derived_from:`` loses a requirement's parent link while
    the model still reads. The check changes nothing; no protocol address names an undeclared
    key, so the repair is the user's.

    Only a file holding such a key is validated again, through its layer's adapter, so a finding
    is spelled from the element that file declares: two files may declare one name.

    What it does not reach: a file the loader refuses (a kind that forbids extra keys, the
    closed data type kinds and the two stubs, is such a file); the readers refuse it first. The
    second validation is outside ``model_state.single_read``'s memo, whose "a later load reads
    nothing" covers ``parse_element`` alone.
    """
    view = as_view(model_root)
    registry = current_registry()
    files: dict[str, list[tuple[Any, str]]] = {}
    for layer in registry.layer_names:
        adapter: TypeAdapter[Any] | None = None
        for path in view.stored_paths(layer):
            text = view.stored_text(layer, path)
            try:
                document = load_yaml_document(text)
            except yaml.YAMLError:
                continue
            if not undeclared_keys(text, document, layer, registry=registry):
                continue
            if adapter is None:
                adapter = TypeAdapter(registry.layer(layer).element_annotation)
            try:
                element = adapter.validate_python(document)
            except ValidationError:
                continue
            files.setdefault(layer, []).append((element, text))
    return consistency_result(unknown_key_records(files, registry=registry))


def unknown_key_records(
    files: Mapping[str, Sequence[tuple[Any, str]]],
    *,
    registry: ModelRegistry | None = None,
) -> list[ConsistencyIssue]:
    """:func:`check_unknown_keys`' rule, over files already read and loaded.

    Args:
        files: ``{layer: [(element, text)]}``, each file's loaded element and its text, in
            listing order. A key the registry does not declare is not read.
        registry: the model registry to read, or None for the one in effect.

    Returns:
        One ``model.unknown-key`` finding per undeclared key (layers in registry order, files in
        the given order, keys in document order). The subject is spelled from the loaded element,
        each list item as a member step by its collection's identity; it ends with
        ``{key: <the key>}``, or at the holding mapping for an empty key.
    """
    registry = registry or current_registry()
    records: list[ConsistencyIssue] = []
    for layer in registry.layer_names:
        facts = registry.layer(layer)
        for element, text in files.get(layer, ()):
            document = load_yaml_document(text)
            identity = str(published_value(element, facts.identity_field))
            for found in undeclared_keys(text, document, layer, registry=registry):
                steps, places = _unknown_key_path(element, facts.top_level_kinds, found.locator, registry)
                held = held_keys(document, found.locator)
                declared = [field.name for field in registry.kind(found.kind).file_fields]
                # An empty key has no text to name: its subject ends at the mapping.
                last = (key_step(found.key),) if found.key else ()
                records.append(
                    ConsistencyIssue(
                        rule="model.unknown-key",
                        message=_unknown_key_message(layer, identity, places, found.key, found.kind, declared, held),
                        # Never the file: the protocol publishes the message and the subject.
                        subjects=(Subject(layer, identity).at(*steps, *last),),
                    )
                )
    return records


def _identity_text(identity: Any) -> str:
    """A composite or whole-value identity as the message shows it: ``{type: timing, period: 0.01}``."""
    if isinstance(identity, dict):
        return "{" + ", ".join(f"{key}: {_identity_text(value)}" for key, value in identity.items()) + "}"
    if isinstance(identity, list):
        return "[" + ", ".join(_identity_text(value) for value in identity) + "]"
    if identity is None:
        return "null"
    return str(identity)


def _unknown_key_path(
    element: Any,
    top_level: Sequence[KindFacts],
    locator: Sequence[tuple[str, str | int]],
    registry: ModelRegistry,
) -> tuple[list[SubjectStep], list[str]]:
    """The subject steps and the message's place phrases, outermost first, down to *locator*'s mapping."""
    steps: list[SubjectStep] = []
    places: list[str] = []
    value = element
    kind = kind_of_loaded(element, top_level)
    position = 0
    while position < len(locator) and kind is not None:
        name = str(locator[position][1])
        field_facts = kind.get_field(name)
        if field_facts is None:
            break
        value = published_value(value, name)
        steps.append(field_step(name))
        position += 1
        if position < len(locator) and locator[position][0] == "item":
            index = int(locator[position][1])
            position += 1
            collection = field_facts.collection
            if collection is None or not isinstance(value, list) or index >= len(value):
                break
            value = value[index]
            kind = kind_of_loaded(value, [registry.kind(member) for member in collection.member_kinds])
            if kind is None:
                break
            identity = loaded_member_identity(value, collection, registry)
            spelled = f"'{identity}'" if isinstance(identity, str) else _identity_text(identity)
            places.append(f"{kind.kind.replace('_', ' ')} {spelled}")
            steps.append(member_step(identity))
        else:
            places.append(f"field `{name}`")
            kind = registry.kind(field_facts.element_kind) if field_facts.element_kind else None
    return steps, places


def _unknown_key_message(
    layer: str,
    identity: str,
    places: Sequence[str],
    key: str,
    kind: str,
    declared: Sequence[str],
    held: Iterable[str],
) -> str:
    """``Element 'X' of layer `x`, at port 'p', carries the key 'k', …`` -- the layer and the element, never a file.

    The place is the member chain, innermost first; the hint guesses among the *declared* keys
    the mapping does not already hold (*held*).
    """
    at = f", at {' of '.join(reversed(places))}," if places else ""
    carried, hint = unknown_key_clauses(key, declared, held)
    return (
        f"Element '{identity}' of layer `{layer}`{at} carries {carried}, which kind `{kind}` does not declare; "
        f"it is dropped when the model is read. Rename the key to the field it is meant to be, or delete it{hint}."
    )
