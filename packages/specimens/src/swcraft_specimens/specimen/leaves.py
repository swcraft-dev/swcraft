"""The leaf table: every leaf of the vendored passport schema, one class each.

A leaf is a schema node that is not an object: a count, a version string, a mean, or a list
(`components.hubs`, `components.ports.top`, `interfaces.fan_out.top`), which is one leaf. Paths are
dotted, as the interpreter's refusals, the CLI and the sidecar spell them.

The two classes:

- `version`: `passport_version` (any `1.x.y`) and `contract_version` (major 12).
- `met`: every other leaf: generated exactly, refused by the feasibility classes when infeasible,
  and verified after generation. `layers.interfaces` is `met` at any value of at least
  `sender_receiver + client_server` (the interfaces beyond are external interface stubs; below,
  `layer-total`). `layers.compositions` is `met` at any value of at least 1; 0 is refused
  `layer-total`, since SWCraft needs one root. `connectors.delegation`'s bounds are the class
  `delegations`. The five type layers' counts and identifier leaves are `met` at any count. The
  task and sequence leaves are refused by `sum`, `tasks` and `ceiling` (`tasks.py`), the
  requirement leaves by `sum`, `requirements`, `derived` and `ceiling` (`requirements.py`).

`later-shape` is a refusal class for a stated `layers.hardware` of 0 only; no leaf carries a
neutral value.

What this does not reach: the table classifies the leaves of the vendored schema `1.0.0` and no
other; a re-vendored schema with another leaf fails `tests/test_specimen_preset.py` until the leaf
is classified here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..passport import (
    PASSPORT_LAYERS,
    PassportSchemaError,
    resolve_schema_node,
    vendored_schema,
)
from .types import TYPE_LAYERS
from .vocabulary import CONTRACT_VERSION_LEAF, MET, PASSPORT_VERSION_LEAF, VERSION


@dataclass(frozen=True)
class Leaf:
    """One schema leaf: its dotted path and its class."""

    path: str
    leaf_class: str


#: The type layers whose count the builder meets at any count.
MET_TYPE_LAYERS = frozenset(TYPE_LAYERS)
#: The layers whose identifier lengths the builder meets: those it names at stated lengths, the
#: type layers among them.
MET_IDENTIFIER_LAYERS = (
    frozenset(
        {
            "swc",
            "interfaces",
            "connectors",
            "implementation_types",
            "base_types",
            "hardware",
            "compositions",
            "tasks",
            "sequences",
            "requirements",
        }
    )
    | MET_TYPE_LAYERS
)
#: The layers whose element count the builder meets, within their refusals: every layer.
MET_LAYERS = MET_IDENTIFIER_LAYERS


def _walk(node: Mapping[str, Any], schema: Mapping[str, Any], path: list[str]) -> list[str]:
    node = resolve_schema_node(node, schema)
    if node.get("type") != "object":
        return [".".join(path)]
    paths: list[str] = []
    for key, child in node.get("properties", {}).items():
        paths.extend(_walk(child, schema, [*path, key]))
    return paths


def schema_leaf_paths(schema: Mapping[str, Any]) -> list[str]:
    """Every leaf path of a passport schema, dotted, in the schema's property order."""
    return _walk(schema, schema, [])


def _classify(path: str) -> Leaf:
    if path in (PASSPORT_VERSION_LEAF, CONTRACT_VERSION_LEAF):
        return Leaf(path, VERSION)
    parts = path.split(".")
    if parts[0] == "layers" and parts[1] not in MET_LAYERS:
        raise PassportSchemaError(f"the vendored schema's layer {parts[1]} is not classified")
    if parts[0] == "identifiers" and len(parts) == 3 and parts[1] not in MET_IDENTIFIER_LAYERS:
        raise PassportSchemaError(f"the vendored schema's layer {parts[1]} is not classified")
    return Leaf(path, MET)


def _table() -> dict[str, Leaf]:
    paths = schema_leaf_paths(vendored_schema())
    layer_paths = [path for path in paths if path.startswith("layers.")]
    if [path.split(".", 1)[1] for path in layer_paths] != list(PASSPORT_LAYERS):
        raise PassportSchemaError("the vendored schema's layers differ from the fifteen the pass reads")
    return {path: _classify(path) for path in paths}


#: Every leaf of the vendored schema, in schema order, with its class.
LEAVES: dict[str, Leaf] = _table()


def stated_leaves(passport: Mapping[str, Any]) -> dict[str, Any]:
    """The leaves a schema-valid passport states, dotted, in schema order, with their values."""
    stated: dict[str, Any] = {}

    def walk(node: Mapping[str, Any], value: Any, path: list[str]) -> None:
        node = resolve_schema_node(node)
        if node.get("type") != "object":
            stated[".".join(path)] = value
            return
        for key, child in node.get("properties", {}).items():
            if isinstance(value, Mapping) and key in value:
                walk(child, value[key], [*path, key])

    walk(vendored_schema(), passport, [])
    return stated
