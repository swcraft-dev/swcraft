"""The production loader for the model contract.

The four normative JSON files under ``swcraft_tools/model_contract/`` are read
here and nowhere else; every other consumer reaches the declarations through
this module. They are read through ``importlib.resources``, so the exact
reviewed bytes load from an installed wheel with no build-time copy.

An unknown ``contractVersion`` major is refused before the document is parsed:
a consumer that skipped the keys it did not recognize would silently drop
layers, fields or rules a newer contract added. :func:`load_contract` screens
the version on the **raw text**; :func:`require_supported_contract_version` is
the same rule over a parsed document.

Two checks run on every load:

* the **structural** check: the top-level keys the meta-schema's ``required``
  list names. It always runs and reaches no deeper; a malformed ``layers`` entry
  passes it.
* the **meta-schema** check: full Draft 2020-12 validation, run only when
  ``jsonschema`` (a ``dev`` extra) is importable. An install without it gets the
  top-level and version guarantees and no field-level one.

Neither check validates the contract against ``schema.py``. This module is a
leaf: it imports nothing from ``swcraft_tools``.
"""

from __future__ import annotations

import functools
import importlib
import json
import re
from importlib.resources import files as resource_files
from typing import Any

#: The distribution package the contract ships inside, and its directory there.
_PACKAGE = "swcraft_tools"
_CONTRACT_DIRECTORY = "model_contract"

_CONTRACT_RESOURCE = "model-contract.json"
_CONTRACT_SCHEMA_RESOURCE = "contract.schema.json"
_BINDING_RESOURCE = "binding.json"
_BINDING_SCHEMA_RESOURCE = "binding.schema.json"

#: The ``contractVersion`` majors this repository knows how to read.
SUPPORTED_CONTRACT_MAJORS = frozenset({12})

#: The shape ``contract.schema.json``'s ``semanticVersion`` declares, restated
#: because this guard runs on raw text, before (or without) the meta-schema.
_SEMANTIC_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

#: ``"contractVersion": <value>`` as it appears in the raw text, quoted or not,
#: so a number or ``null`` gets the parsed-document guard's message.
_VERSION_IN_TEXT = re.compile(r'"contractVersion"\s*:\s*(?:"(?P<quoted>[^"]*)"|(?P<bare>[^,}\s]+))')


class UnsupportedContractVersion(RuntimeError):
    """The document declares a ``contractVersion`` this repository cannot read."""


class ContractShapeError(RuntimeError):
    """The document is not a readable contract or binding artifact."""


# ---------------------------------------------------------------------------
# Reading the packaged bytes
# ---------------------------------------------------------------------------


@functools.cache
def _read_resource(name: str) -> str:
    """The text of one packaged contract resource.

    Cached as **text**, never as a parsed document: a cached ``dict`` would be
    shared mutable state, and one consumer's in-place edit would change the
    contract for every later reader.
    """
    return (resource_files(_PACKAGE) / _CONTRACT_DIRECTORY / name).read_text(encoding="utf-8")


@functools.cache
def _required_keys(schema_resource: str) -> tuple[str, ...]:
    """The top-level keys a shipped meta-schema requires, derived from the schema rather than restated."""
    schema = json.loads(_read_resource(schema_resource))
    required = schema.get("required") if isinstance(schema, dict) else None
    if not isinstance(required, list) or not all(isinstance(key, str) for key in required):
        raise ContractShapeError(f"{schema_resource} declares no top-level `required` key list")
    return tuple(required)


def _import_validator_class() -> Any:
    """The Draft 2020-12 validator class, or ``None`` when ``jsonschema`` is absent.

    ``jsonschema`` is a ``dev`` extra, so its absence is supported: the caller falls
    back to the structural check. It is imported by name because an ``import``
    statement would declare a runtime dependency this package must not acquire.
    """
    try:
        module = importlib.import_module("jsonschema")
    except ImportError:
        return None
    return getattr(module, "Draft202012Validator", None)


@functools.cache
def _validator_for(schema_resource: str) -> Any:
    """A validator over one shipped meta-schema, or ``None`` without ``jsonschema``."""
    validator_class = _import_validator_class()
    if validator_class is None:
        return None
    return validator_class(json.loads(_read_resource(schema_resource)))


#: Every cache :func:`clear_cache` empties, held by object so a test that
#: replaces ``_read_resource`` on the module still clears the real one.
_CACHES = (_read_resource, _required_keys, _validator_for)


def clear_cache() -> None:
    """Drop the cached resource text and everything derived from it *in this module*.

    **It does not reach the kernel registry.** ``model_registry.production_registry()``
    caches its own build, and the module-level surfaces built from it are resolved
    once at import, so a rebuilt registry would disagree with them. A test needing
    a different contract uses ``model_registry.use_registry``; this function is for
    tests of the loader itself.
    """
    for cache in _CACHES:
        cache.cache_clear()


# ---------------------------------------------------------------------------
# The version guard
# ---------------------------------------------------------------------------


def _check_version_string(declared: Any) -> None:
    """Refuse *declared* unless it is a semantic version with a supported major."""
    match = _SEMANTIC_VERSION.fullmatch(str(declared))
    if match is None:
        raise UnsupportedContractVersion(f"contractVersion is not a semantic version: {declared!r}")
    major = match.group(1)
    if int(major) not in SUPPORTED_CONTRACT_MAJORS:
        raise UnsupportedContractVersion(
            f"contractVersion {declared!r} declares major {major}, "
            f"which this repository cannot read (supported: {sorted(SUPPORTED_CONTRACT_MAJORS)})"
        )


def require_supported_contract_version(document: Any) -> None:
    """Refuse *document* unless its ``contractVersion`` major is supported.

    A document with no ``contractVersion``, or one whose value is not a dotted
    version, is refused rather than defaulted: a default would read an unversioned
    contract as the newest one.
    """
    if not isinstance(document, dict) or "contractVersion" not in document:
        raise UnsupportedContractVersion("the contract document declares no contractVersion")
    _check_version_string(document["contractVersion"])


def _screen_version_text(text: str) -> str:
    """The declared version, refused on the raw text before anything is parsed.

    The **first** ``"contractVersion"`` occurrence is the answer; the shipped
    contract carries exactly one, at the top level (``test_model_contract_loader.py``
    pins the count). Scanning for a *supported* occurrence instead would answer
    differently from :func:`load_contract` for an unsupported document that quotes
    a supported version in prose. The parsed-document guard that runs afterwards
    remains the authority.
    """
    # `is not None` rather than `or`: `"contractVersion": ""` matches `quoted` with an
    # empty string, which `or` would replace with the unmatched `bare` group's None.
    match = _VERSION_IN_TEXT.search(text)
    if match is None:
        raise UnsupportedContractVersion("the contract document declares no contractVersion")
    quoted = match.group("quoted")
    declared = quoted if quoted is not None else match.group("bare")
    _check_version_string(declared)
    return declared


# ---------------------------------------------------------------------------
# The loaders
# ---------------------------------------------------------------------------


def _parse(text: str, *, source: str) -> Any:
    """``json.loads`` with a failure that names the artifact rather than an offset."""
    try:
        return json.loads(text)
    except ValueError as failure:
        raise ContractShapeError(f"{source} is not readable JSON: {failure}") from failure


def _require_shape(document: Any, *, source: str, schema_resource: str) -> dict[str, Any]:
    """The always-on structural check: an object carrying the required top-level keys."""
    if not isinstance(document, dict):
        raise ContractShapeError(f"{source} is not a JSON object: {type(document).__name__}")
    missing = [key for key in _required_keys(schema_resource) if key not in document]
    if missing:
        raise ContractShapeError(f"{source} is missing top-level keys: {', '.join(missing)}")
    return document


def _require_meta_schema(document: dict[str, Any], *, source: str, schema_resource: str) -> None:
    """Full Draft 2020-12 validation, when ``jsonschema`` is importable."""
    validator = _validator_for(schema_resource)
    if validator is None:
        return
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        detail = "; ".join(
            f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
            for error in errors[:5]
        )
        raise ContractShapeError(f"{source} does not satisfy its meta-schema ({len(errors)} error(s)): {detail}")


def load_contract() -> dict[str, Any]:
    """``model-contract.json``, the normative declaration, as shipped.

    The version is screened on the raw text first, so a contract this repository
    cannot read is refused before a single layer is parsed.
    """
    text = _read_resource(_CONTRACT_RESOURCE)
    _screen_version_text(text)

    document = _parse(text, source=_CONTRACT_RESOURCE)
    document = _require_shape(document, source=_CONTRACT_RESOURCE, schema_resource=_CONTRACT_SCHEMA_RESOURCE)
    # The authority: the raw-text screen reads the first occurrence, maybe not the top-level one.
    require_supported_contract_version(document)
    _require_meta_schema(document, source=_CONTRACT_RESOURCE, schema_resource=_CONTRACT_SCHEMA_RESOURCE)
    return document


def load_binding() -> dict[str, Any]:
    """``binding.json``: contract identifiers to runtime Python symbols.

    It has no version of its own; it is versioned with the contract it binds.
    """
    text = _read_resource(_BINDING_RESOURCE)
    document = _parse(text, source=_BINDING_RESOURCE)
    document = _require_shape(document, source=_BINDING_RESOURCE, schema_resource=_BINDING_SCHEMA_RESOURCE)
    _require_meta_schema(document, source=_BINDING_RESOURCE, schema_resource=_BINDING_SCHEMA_RESOURCE)
    return document


def contract_version() -> str:
    """The shipped ``contractVersion``, without parsing the contract.

    Refuses an unsupported major exactly as :func:`load_contract` does. It answers
    the *first* occurrence of the key in the raw text and cannot tell a top-level
    ``contractVersion`` from one nested in a ``description``; for an arbitrary
    document read ``load_contract()["contractVersion"]``.
    """
    return _screen_version_text(_read_resource(_CONTRACT_RESOURCE))
