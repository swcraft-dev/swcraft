"""The specimen preset: `kind: specimen`, an optional name and the stated passport.

A specimen preset is a YAML mapping:

    kind: specimen
    name: SpecimenArchitecture   # optional; an identifier
    passport: {...}              # the stated leaves, any subset the schema accepts

`preset_text` writes one from a passport with every key in the schema's order, so the bytes depend
on the passport's values only; `parse_specimen_text` reads one back. A passport read from YAML
follows the interpreter's rules exactly as one read from JSON: YAML's `.nan` and `.inf` are
refused, a boolean is never a count, an integral float is an integer. A repeated YAML key is
refused like a repeated JSON key.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from ..errors import PresetError
from ..names import validate_identifier
from ..passport import resolve_schema_node, validate_passport, vendored_schema

SPECIMEN_KIND = "specimen"
DEFAULT_NAME = "SpecimenArchitecture"
ROOT_KEYS = ("kind", "name", "passport")


@dataclass(frozen=True)
class SpecimenPreset:
    """A specimen preset: the project name and the stated passport, validated and normalised."""

    name: str
    passport: dict[str, Any]


class _Loader(yaml.SafeLoader):
    """YAML's safe reading, with a repeated key refused."""


def _line(node: yaml.Node) -> int:
    return int(node.start_mark.line) + 1


def yaml_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    """A YAML mapping with a repeated key, or a key that is not a scalar, refused."""
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            repeated = key in result
        except TypeError:
            raise PresetError(f"a YAML key is not a scalar (line {_line(key_node)})") from None
        if repeated:
            raise PresetError(f"duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


def yaml_int(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> int:
    """A YAML integer, refused when it has more digits than Python converts."""
    try:
        value: int = yaml.SafeLoader.construct_yaml_int(loader, node)
    except ValueError:
        raise PresetError(f"a YAML integer has more digits than a reader converts (line {_line(node)})") from None
    return value


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, yaml_mapping)
_Loader.add_constructor("tag:yaml.org,2002:int", yaml_int)


def load_yaml_text(text: str) -> Any:
    """Parse a specimen preset's YAML text (floats stay floats, a repeated key is refused)."""
    return yaml.load(text, Loader=_Loader)


def _validate_name(name: object) -> str:
    if not isinstance(name, str):
        raise PresetError("name: must be an identifier")
    try:
        return validate_identifier(name, "specimen name")
    except ValueError:
        raise PresetError("name: must be an identifier, [a-zA-Z][a-zA-Z0-9_]*, at most 128 characters") from None


def specimen_from_value(value: Mapping[Any, Any]) -> SpecimenPreset:
    """A specimen preset from a parsed YAML mapping whose `kind` is `specimen`."""
    for key in value:
        if key not in ROOT_KEYS:
            raise PresetError(f"preset.{key}: unknown key of a specimen preset")
    if value.get("kind") != SPECIMEN_KIND:
        raise PresetError("kind: a specimen preset states kind: specimen")
    if "passport" not in value:
        raise PresetError("preset.passport: a specimen preset states a passport")
    name = _validate_name(value["name"]) if "name" in value else DEFAULT_NAME
    passport = validate_passport(copy.deepcopy(value["passport"]))
    return SpecimenPreset(name, ordered_passport(passport))


def parse_specimen_text(text: str, source: str) -> SpecimenPreset:
    """Read a specimen preset's YAML text; `source` names it in a reading error."""
    try:
        value = load_yaml_text(text)
    except RecursionError:
        raise PresetError(f"invalid YAML in preset {source}: it nests deeper than the YAML reader follows") from None
    except yaml.YAMLError as exc:
        detail = getattr(exc, "problem", None) or "invalid YAML"
        raise PresetError(f"invalid YAML in preset {source}: {detail}") from None
    if not isinstance(value, dict):
        raise PresetError(f"preset root must be a mapping: {source}")
    return specimen_from_value(value)


def ordered_passport(passport: Mapping[str, Any]) -> dict[str, Any]:
    """A schema-valid passport with every object's keys in the schema's property order."""

    def order(node: Mapping[str, Any], value: Any) -> Any:
        node = resolve_schema_node(node)
        if node.get("type") == "object" and isinstance(value, Mapping):
            properties: Mapping[str, Any] = node.get("properties", {})
            return {key: order(child, value[key]) for key, child in properties.items() if key in value}
        if isinstance(value, list):
            return list(value)
        return value

    result: dict[str, Any] = order(vendored_schema(), passport)
    return result


class _Dumper(yaml.SafeDumper):
    """Block mappings, flow lists: one leaf per line, a list leaf on its own line."""


def _list(dumper: yaml.SafeDumper, value: list[Any]) -> yaml.SequenceNode:
    return dumper.represent_sequence("tag:yaml.org,2002:seq", value, flow_style=True)


_Dumper.add_representer(list, _list)


def preset_text(passport: Mapping[str, Any], name: str | None = None) -> str:
    """The specimen preset of a schema-valid passport, keys in schema order (deterministic)."""
    document: dict[str, Any] = {"kind": SPECIMEN_KIND}
    if name is not None:
        document["name"] = _validate_name(name)
    document["passport"] = ordered_passport(validate_passport(copy.deepcopy(dict(passport))))
    return yaml.dump(
        document,
        Dumper=_Dumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=False,
        width=100,
    )
