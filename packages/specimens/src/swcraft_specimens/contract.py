"""Independent physical YAML contract gate for SWCraft's model contract major 12.

A small, hand-written allowlist of the subset the specimen generator emits, taken from SWCraft's
public editor schemas (``vscode_extension/schemas/*.schema.json``) for model contract 12.0.0. It is
never generated from, and never imports, the installed SWCraft package, and it names no SWCraft
release. Placement metadata is represented by the path (``swc/<group>/``), never by a YAML key.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

import yaml

from .errors import GenerationError
from .passport import CONTRACT_MAJOR

# The error prefix names the contract major alone: the specimen generator pins no SWCraft
# release, and `passport.py`'s `CONTRACT_MAJOR` is the one definition of the major it writes.
CONTRACT_PREFIX = f"SWCraft model contract major {CONTRACT_MAJOR} contract"


def _schema(keys: tuple[str, ...], required: tuple[str, ...] = ()) -> tuple[frozenset[str], frozenset[str]]:
    return frozenset(keys), frozenset(required)


SCHEMAS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "manifest": _schema(
        (
            "name",
            "schema_version",
            "toolchain_version",
            "model_root",
            "dist_root",
            "views_root",
            "outputs",
        ),
        # `toolchain_version` is optional: none found, no key.
        (
            "name",
            "schema_version",
            "model_root",
            "dist_root",
            "views_root",
            "outputs",
        ),
    ),
    "manifest.outputs": _schema(("arxml", "needs")),
    "requirements.requirement": _schema(
        ("id", "title", "description", "asil", "version", "source"),
        ("id", "title", "description", "version", "source"),
    ),
    "compositions.composition": _schema(
        ("name", "title", "root", "description", "ports", "members", "delegations", "satisfies"),
        ("name", "members"),
    ),
    "compositions.port": _schema(("name", "direction", "interface", "description"), ("name", "direction", "interface")),
    "compositions.member": _schema(("name", "type", "composition", "description"), ("name",)),
    "compositions.delegation": _schema(
        ("name", "port", "member", "member_port", "description"),
        ("name", "port", "member", "member_port"),
    ),
    "swc.swc": _schema(
        (
            "name",
            "type",
            "description",
            "ports",
            "runnables",
            "memory",
            "asil",
            "handle_termination_and_restart",
            "satisfies",
        ),
        ("name", "handle_termination_and_restart"),
    ),
    "swc.port": _schema(("name", "direction", "interface", "satisfies"), ("name", "direction", "interface")),
    "swc.runnable": _schema(
        ("name", "events", "reads", "writes", "calls", "wcet", "wcet_type", "satisfies"),
        ("name", "events", "reads", "writes", "calls"),
    ),
    "swc.timing_event": _schema(("type", "period"), ("type", "period")),
    "swc.data_read": _schema(("port", "element"), ("port", "element")),
    "swc.data_write": _schema(("port", "element"), ("port", "element")),
    "swc.operation_call": _schema(("port", "operation"), ("port", "operation")),
    "interfaces.sender_receiver": _schema(
        ("name", "kind", "data_elements", "satisfies"), ("name", "kind", "data_elements")
    ),
    "interfaces.client_server": _schema(("name", "kind", "operations", "satisfies"), ("name", "kind", "operations")),
    # A stub: its reference pair and no member names.
    "interfaces.external_interface": _schema(
        ("name", "kind", "external_ref", "external_dest", "satisfies"),
        ("name", "kind", "external_ref", "external_dest"),
    ),
    "interfaces.data_element": _schema(("name", "implementation_type"), ("name", "implementation_type")),
    "interfaces.operation": _schema(("name", "arguments"), ("name", "arguments")),
    "interfaces.argument": _schema(
        ("name", "direction", "implementation_type"), ("name", "direction", "implementation_type")
    ),
    "data_types.base_type": _schema(
        ("name", "kind", "size", "encoding", "native_declaration"),
        ("name", "kind", "size", "encoding", "native_declaration"),
    ),
    "data_types.value_type": _schema(("name", "kind", "base"), ("name", "kind", "base")),
    "data_types.implementation_array": _schema(
        ("name", "kind", "element_type", "length", "element_name"),
        ("name", "kind", "element_type", "length", "element_name"),
    ),
    "data_types.implementation_record": _schema(("name", "kind", "elements"), ("name", "kind", "elements")),
    "data_types.implementation_element": _schema(("name", "type"), ("name", "type")),
    # The five type layers: references only, nothing inline.
    "data_types.application_type": _schema(
        ("name", "kind", "unit_ref", "compu_method_ref", "data_constr_ref"), ("name", "kind")
    ),
    "data_types.unit": _schema(("name", "display_name"), ("name",)),
    "data_types.linear": _schema(
        ("name", "kind", "unit", "factor", "offset"), ("name", "kind", "unit", "factor", "offset")
    ),
    "data_types.texttable": _schema(("name", "kind", "entries"), ("name", "kind", "entries")),
    "data_types.table_entry": _schema(("name", "value"), ("name", "value")),
    "data_types.data_constr": _schema(("name", "min", "max", "level"), ("name",)),
    "data_types.mapping_set": _schema(("name", "maps"), ("name", "maps")),
    "data_types.map": _schema(("application", "implementation"), ("application", "implementation")),
    "connectors.connector": _schema(("name", "from", "to", "composition", "description"), ("name", "from", "to")),
    "connectors.endpoint": _schema(("swc", "composition", "port"), ("port",)),
    "tasks.task": _schema(
        (
            "name",
            "period",
            "priority",
            "partition",
            "wcet",
            "runnables",
            "coexistence",
            "satisfies",
        ),
        ("name", "period", "priority", "partition", "wcet", "runnables"),
    ),
    "tasks.runnable_reference": _schema(("swc", "runnable"), ("swc", "runnable")),
    # A specimen's sequence: its title and notes written empty.
    "sequences.sequence": _schema(("name", "task", "title", "steps"), ("name", "task", "steps")),
    "sequences.step": _schema(("swc", "runnable", "note"), ("swc", "runnable")),
    "hardware.processor": _schema(("name", "kind", "satisfies"), ("name", "kind")),
    "hardware.core": _schema(
        ("name", "kind", "threshold", "enforcement", "asil_capability", "satisfies"),
        ("name", "kind", "threshold", "enforcement"),
    ),
    "hardware.memory": _schema(
        ("name", "kind", "type", "size", "accessible", "threshold", "enforcement", "satisfies"),
        ("name", "kind", "type", "size", "accessible", "threshold", "enforcement"),
    ),
    # The WATERS presets write one unrated partition per core; a specimen's partition may be rated
    # and argue its levels' coexistence.
    "hardware.partition": _schema(
        ("name", "kind", "core", "asil", "coexistence", "satisfies"), ("name", "kind", "core")
    ),
}


# The data type kinds each kind-dispatched ``data_types/<layer>`` directory admits; a kind is
# dispatched by its directory.
DATA_TYPE_KINDS: dict[str, frozenset[str]] = {
    "data_types/application": frozenset(("application_type",)),
    "data_types/base": frozenset(("base_type",)),
    "data_types/compu_methods": frozenset(("linear", "texttable")),
    "data_types/implementation": frozenset(("value_type", "implementation_array", "implementation_record")),
}

# The one schema of each ``data_types/<layer>`` directory whose elements carry no ``kind``: a
# ``kind`` key there is an unknown key, so a kind-dispatched element in one is refused.
DATA_TYPE_SCHEMAS: dict[str, str] = {
    "data_types/constraints": "data_types.data_constr",
    "data_types/mapping_sets": "data_types.mapping_set",
    "data_types/units": "data_types.unit",
}

# The kinds each kind-dispatched layer emits; an absent or other kind is refused, never looked up.
INTERFACE_KINDS = frozenset(("sender_receiver", "client_server", "external_interface"))
HARDWARE_KINDS = frozenset(("processor", "core", "memory", "partition"))


def _fail(path: str, key: str) -> None:
    raise GenerationError(f"{CONTRACT_PREFIX}: {path}: unknown key '{key}'")


def _check(value: Any, kind: str, path: str) -> None:
    if not isinstance(value, dict):
        raise GenerationError(f"{CONTRACT_PREFIX}: {path}: expected mapping")
    allowed, required = SCHEMAS[kind]
    for key in value:
        if key not in allowed:
            _fail(path, key)
    missing = required - value.keys()
    if missing:
        raise GenerationError(f"{CONTRACT_PREFIX}: {path}: missing key '{sorted(missing)[0]}'")


def _check_kind(data: Any, layer: str, kinds: frozenset[str], path: str, schema_prefix: str | None = None) -> str:
    """Check a kind-dispatched element: a mapping whose ``kind`` the layer allows, then its keys.

    The schema is ``<schema_prefix>.<kind>``, the prefix defaulting to the layer; returns the kind.
    """
    if not isinstance(data, dict):
        raise GenerationError(f"{CONTRACT_PREFIX}: {path}: expected mapping")
    kind = data.get("kind")
    # A list or a mapping is unhashable: test the type first, so it is refused, not a TypeError.
    if not isinstance(kind, str) or kind not in kinds:
        raise GenerationError(f"{CONTRACT_PREFIX}: {path}: kind '{kind}' is not allowed in {layer}")
    _check(data, f"{schema_prefix or layer}.{kind}", path)
    return kind


def _list(data: dict[str, Any], key: str, path: str) -> list[Any]:
    """Return the list under ``key``, or ``[]`` when it is absent; any other value is refused."""
    value = data.get(key, [])
    if not isinstance(value, list):
        raise GenerationError(f"{CONTRACT_PREFIX}: {path}.{key}: expected list")
    return value


def _yaml(data: bytes, path: str) -> Any:
    try:
        return yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise GenerationError(f"{CONTRACT_PREFIX}: {path}: invalid YAML: {exc}") from None


def _validate_layer(data: Any, layer: str, path: str) -> None:
    if layer == "requirements":
        _check(data, "requirements.requirement", path)
    elif layer == "compositions":
        _check(data, "compositions.composition", path)
        _list(data, "satisfies", path)
        for i, x in enumerate(_list(data, "ports", path)):
            _check(x, "compositions.port", f"{path}.ports[{i}]")
        for i, x in enumerate(_list(data, "members", path)):
            _check(x, "compositions.member", f"{path}.members[{i}]")
        for i, x in enumerate(_list(data, "delegations", path)):
            _check(x, "compositions.delegation", f"{path}.delegations[{i}]")
    elif layer == "swc":
        _check(data, "swc.swc", path)
        _list(data, "satisfies", path)
        for i, x in enumerate(_list(data, "ports", path)):
            _check(x, "swc.port", f"{path}.ports[{i}]")
            _list(x, "satisfies", f"{path}.ports[{i}]")
        for i, x in enumerate(_list(data, "runnables", path)):
            rp = f"{path}.runnables[{i}]"
            _check(x, "swc.runnable", rp)
            _list(x, "satisfies", rp)
            # A runnable's budget and its type are written together or not at all.
            if ("wcet" in x) != ("wcet_type" in x):
                absent = "wcet_type" if "wcet" in x else "wcet"
                raise GenerationError(f"{CONTRACT_PREFIX}: {rp}: missing key '{absent}'")
            # The specimen generator emits timing events only; any other event fails the timing allowlist.
            for j, e in enumerate(_list(x, "events", rp)):
                _check(e, "swc.timing_event", f"{rp}.events[{j}]")
            for field, kind in (
                ("reads", "swc.data_read"),
                ("writes", "swc.data_write"),
                ("calls", "swc.operation_call"),
            ):
                for j, e in enumerate(_list(x, field, rp)):
                    _check(e, kind, f"{rp}.{field}[{j}]")
    elif layer == "interfaces":
        kind = _check_kind(data, layer, INTERFACE_KINDS, path)
        _list(data, "satisfies", path)
        if kind == "sender_receiver":
            for i, x in enumerate(_list(data, "data_elements", path)):
                _check(x, "interfaces.data_element", f"{path}.data_elements[{i}]")
        elif kind == "client_server":
            for i, x in enumerate(_list(data, "operations", path)):
                op = f"{path}.operations[{i}]"
                _check(x, "interfaces.operation", op)
                for j, a in enumerate(_list(x, "arguments", op)):
                    _check(a, "interfaces.argument", f"{op}.arguments[{j}]")
    elif layer in DATA_TYPE_KINDS:
        type_kind = _check_kind(data, layer, DATA_TYPE_KINDS[layer], path, "data_types")
        if type_kind == "implementation_record":
            for i, x in enumerate(_list(data, "elements", path)):
                _check(x, "data_types.implementation_element", f"{path}.elements[{i}]")
        elif type_kind == "texttable":
            for i, x in enumerate(_list(data, "entries", path)):
                _check(x, "data_types.table_entry", f"{path}.entries[{i}]")
    elif layer in DATA_TYPE_SCHEMAS:
        _check(data, DATA_TYPE_SCHEMAS[layer], path)
        if layer == "data_types/mapping_sets":
            for i, x in enumerate(_list(data, "maps", path)):
                _check(x, "data_types.map", f"{path}.maps[{i}]")
    elif layer.startswith("data_types"):
        directories = ", ".join(sorted({*DATA_TYPE_KINDS, *DATA_TYPE_SCHEMAS}))
        raise GenerationError(f"{CONTRACT_PREFIX}: {path}: a data type must sit in its layer directory ({directories})")
    elif layer == "connectors":
        _check(data, "connectors.connector", path)
        _check(data["from"], "connectors.endpoint", f"{path}.from")
        _check(data["to"], "connectors.endpoint", f"{path}.to")
    elif layer == "tasks":
        _check(data, "tasks.task", path)
        _list(data, "satisfies", path)
        for i, x in enumerate(_list(data, "runnables", path)):
            _check(x, "tasks.runnable_reference", f"{path}.runnables[{i}]")
    elif layer == "sequences":
        _check(data, "sequences.sequence", path)
        for i, x in enumerate(_list(data, "steps", path)):
            _check(x, "sequences.step", f"{path}.steps[{i}]")
    elif layer == "hardware":
        _check_kind(data, layer, HARDWARE_KINDS, path)
        _list(data, "satisfies", path)
    else:
        raise GenerationError(f"{CONTRACT_PREFIX}: {path}: no file is emitted in layer '{layer}'")


def validate_contract(file_map: Mapping[str, bytes]) -> None:
    """Reject unknown physical keys in every emitted SWCraft YAML file."""
    manifest = _yaml(file_map["swcraft.yaml"], "swcraft.yaml")
    _check(manifest, "manifest", "swcraft.yaml")
    _check(manifest["outputs"], "manifest.outputs", "swcraft.yaml.outputs")
    for path, data in file_map.items():
        if not path.startswith("model/") or not path.endswith(".yaml"):
            continue
        parts = PurePosixPath(path).parts
        if len(parts) < 3:
            raise GenerationError(f"{CONTRACT_PREFIX}: {path}: a model file must sit in a layer")
        # A data type's layer is its directory under ``data_types/``; a flat file has none.
        nested = parts[1] == "data_types" and len(parts) > 3
        layer = "/".join(parts[1:3]) if nested else parts[1]
        _validate_layer(_yaml(data, path), layer, path)
