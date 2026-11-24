"""The model passport: the strict reader and the interpreter of the vendored schema.

The schema is `passport_schema/model_passport.schema.json`, a verbatim copy of SWCraft's
`model_passport.schema.json` from tag `v0.21.0` (passport `1.0.0`, model contract major 12). The
specimen generator never imports SWCraft code: it reads that file and interprets it itself.

The interpreter handles exactly the assertion keywords the schema uses (`type`, `properties`,
`required`, `additionalProperties: false`, `$ref` into `$defs`, `items`, `maxItems`, `minimum`,
`pattern`) and ignores its annotation keywords (`$schema`, `$id`, `title`, `description`, `$defs`
as a container, `x-passport-version`, `x-contract-major`). A keyword in neither list is refused
when the schema is loaded, on every path of it, so a re-vendored schema that uses more fails
loudly instead of being half-read.

The number rules: a JSON boolean is never an integer or a number; an integral float such as `1.0`
is an integer, as Draft 2020-12 defines `integer`, and is normalised to `int`; a non-finite number
is refused whatever the document was read from (a YAML preset's `.nan` and `.inf` included).

The strict reading of a JSON text: a repeated key, `NaN`, `Infinity` and `-Infinity` are refused,
and so is a number too large to be finite or an integer of more digits than Python converts (4,300
by default), each as `reading` at its path.

What this does not reach: the rules JSON Schema cannot state (a total equal to the sum of its
parts, a descending list, the supported values of each leaf) are the feasibility check's. A
`pattern` is matched as `re.search` matches it, the reading `jsonschema` takes, so an anchored
`$` also accepts one trailing newline; the version leaves are parsed again by that check.

Every refusal names a dotted path (`components.unconnected.total`; a list item is its index,
`components.hubs.2`; the whole document is `(document)`) and never echoes a string value, so
nothing printed can carry a name.
"""

from __future__ import annotations

import copy
import json
import math
import re
import statistics
import sys
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from functools import lru_cache
from importlib.resources import files
from typing import Any, NamedTuple

from .errors import GenerationError, SpecimenGeneratorError

PASSPORT_VERSION = "1.0.0"
#: The model contract the pinned SWCraft release reads, which a passport states.
MODEL_CONTRACT_VERSION = "12.0.0"
#: Its major, the one a passport's `contract_version` must have.
CONTRACT_MAJOR = int(MODEL_CONTRACT_VERSION.split(".", 1)[0])
#: The SHA-256 of the vendored schema's bytes, as taken from SWCraft at tag `v0.21.0`.
SCHEMA_SHA256 = "9ed2fd65ecf67c01c1ebc72d3f33913b7c0829c7c2dccd2178f420b54e4bde8f"
SCHEMA_RESOURCE = "passport_schema/model_passport.schema.json"

DOCUMENT = "(document)"

ASSERTION_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "$ref",
        "items",
        "maxItems",
        "minimum",
        "pattern",
    }
)
ANNOTATION_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "title",
        "description",
        "$defs",
        "x-passport-version",
        "x-contract-major",
    }
)
SUPPORTED_TYPES = frozenset({"object", "array", "integer", "number", "string"})
REF_PREFIX = "#/$defs/"


class Refusal(NamedTuple):
    """One refused leaf: its dotted path, its closed class and one sentence."""

    leaf: str
    refusal_class: str
    sentence: str


#: The class of a stated leaf the generated model misses, found by the self-check after
#: generation: its line reads `refused in generation: <leaf> (missed): configured X,
#: generated Y`.
MISSED = "missed"
#: The class of a passport that is not one readable JSON document: a repeated key, a number
#: that is not finite or has more digits than a reader converts, or malformed JSON.
READING = "reading"
#: The class of a passport the vendored schema refuses.
SCHEMA = "schema"


def _refusal_line(refusal: Refusal) -> str:
    prefix = "refused in generation" if refusal.refusal_class == MISSED else "refused"
    return f"{prefix}: {refusal.leaf} ({refusal.refusal_class}): {refusal.sentence}"


class PassportRefused(SpecimenGeneratorError):
    """A passport, or a leaf of it, is refused; a configuration error (exit 2).

    A `missed` leaf, refused after generation by the self-check, is the same type with its own
    line prefix.
    """

    def __init__(self, refusals: Sequence[Refusal]) -> None:
        self.refusals = tuple(refusals)
        super().__init__("\n".join(_refusal_line(r) for r in self.refusals))


class PassportSchemaError(GenerationError):
    """The vendored schema uses a keyword the interpreter does not handle: a defect (exit 1)."""


def schema_bytes() -> bytes:
    """The vendored schema's bytes, read from the package data."""
    return files("swcraft_specimens").joinpath(SCHEMA_RESOURCE).read_bytes()


@lru_cache(maxsize=1)
def _vendored_schema() -> dict[str, Any]:
    document = json.loads(schema_bytes().decode("utf-8"))
    if not isinstance(document, dict):
        raise PassportSchemaError("the vendored passport schema is not an object")
    _check_schema(document)
    return document


def schema_document() -> dict[str, Any]:
    """A copy of the vendored schema, parsed, for a caller that may change it."""
    return copy.deepcopy(_vendored_schema())


def vendored_schema() -> Mapping[str, Any]:
    """The vendored schema, parsed once and shared: read it, never change it."""
    return _vendored_schema()


def _definition(definitions: Mapping[str, Any], ref: str) -> Any:
    """The `$defs` entry a `$ref` names."""
    return definitions[ref[len(REF_PREFIX) :]]


def resolve_schema_node(node: Mapping[str, Any], schema: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """A node of `schema` (the vendored one when `None`) with its `$ref` into `$defs` followed."""
    definitions = (_vendored_schema() if schema is None else schema)["$defs"]
    while "$ref" in node:
        node = _definition(definitions, node["$ref"])
    return node


# --- the strict reader ----------------------------------------------------------------------


class _Pairs(list[tuple[str, Any]]):
    """An object's key/value pairs in file order, before repeated keys are checked."""


class _NonFinite:
    """A number the strict reader refuses: `NaN`, `Infinity` or one too large to be finite."""

    def __init__(self, spelling: str) -> None:
        self.spelling = spelling


class _TooLong:
    """An integer of more digits than Python converts (`sys.get_int_max_str_digits`)."""


def _parse_constant(spelling: str) -> _NonFinite:
    return _NonFinite(spelling)


def _parse_int(spelling: str) -> int | _TooLong:
    limit = sys.get_int_max_str_digits()
    if limit and len(spelling.lstrip("-")) > limit:
        return _TooLong()
    return int(spelling)


def _parse_float(spelling: str) -> float | _NonFinite:
    value = float(spelling)
    return value if math.isfinite(value) else _NonFinite(spelling)


def _dotted(path: Sequence[str]) -> str:
    return ".".join(path) if path else DOCUMENT


def _resolve(value: Any, path: list[str], refusals: list[Refusal]) -> Any:
    if isinstance(value, _Pairs):
        result: dict[str, Any] = {}
        for key, item in value:
            if key in result:
                refusals.append(Refusal(_dotted([*path, key]), READING, "the key is repeated"))
                continue
            result[key] = _resolve(item, [*path, key], refusals)
        return result
    if isinstance(value, list):
        return [_resolve(item, [*path, str(i)], refusals) for i, item in enumerate(value)]
    if isinstance(value, _NonFinite):
        refusals.append(Refusal(_dotted(path), READING, "the number is not finite"))
        return None
    if isinstance(value, _TooLong):
        refusals.append(Refusal(_dotted(path), READING, "the integer has more digits than a reader converts"))
        return None
    return value


def parse_passport_json(text: str) -> Any:
    """Parse a JSON text strictly: no repeated key and no non-finite number, all collected."""
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_Pairs,
            parse_constant=_parse_constant,
            parse_float=_parse_float,
            parse_int=_parse_int,
        )
        refusals: list[Refusal] = []
        document = _resolve(raw, [], refusals)
    except json.JSONDecodeError as error:
        sentence = f"the text is not JSON ({error.msg}, line {error.lineno} column {error.colno})"
        raise PassportRefused([Refusal(DOCUMENT, READING, sentence)]) from None
    except RecursionError:
        # Valid JSON nested deeper than the reader's recursion follows: no passport nests so.
        sentence = "the text nests arrays or objects deeper than the reader follows"
        raise PassportRefused([Refusal(DOCUMENT, READING, sentence)]) from None
    if refusals:
        raise PassportRefused(refusals)
    return document


def read_passport(text: str) -> dict[str, Any]:
    """Read a passport's JSON text strictly and validate it against the vendored schema."""
    return validate_passport(parse_passport_json(text))


# --- the interpreter ------------------------------------------------------------------------


def _check_schema(schema: dict[str, Any]) -> None:
    """Refuse a schema using a keyword (or a keyword form) the interpreter does not handle."""
    definitions = schema.get("$defs", {})

    def walk(node: Any, where: str) -> None:
        if not isinstance(node, dict):
            raise PassportSchemaError(f"the passport schema node {where} is not an object")
        for keyword, value in node.items():
            at = f"{where}/{keyword}"
            if keyword not in ASSERTION_KEYWORDS and keyword not in ANNOTATION_KEYWORDS:
                raise PassportSchemaError(
                    f"the passport schema uses {keyword!r} at {where or '/'}, a keyword the interpreter does not handle"
                )
            if keyword in ("properties", "$defs"):
                if not isinstance(value, dict):
                    raise PassportSchemaError(f"the passport schema's {at} is not an object")
                for name, child in value.items():
                    walk(child, f"{at}/{name}")
            elif keyword == "items":
                walk(value, at)
            elif keyword == "additionalProperties" and value is not False:
                raise PassportSchemaError(f"the passport schema's additionalProperties at {where or '/'} is not false")
            elif keyword == "$ref" and not (
                isinstance(value, str) and value.startswith(REF_PREFIX) and value[len(REF_PREFIX) :] in definitions
            ):
                raise PassportSchemaError(
                    f"the passport schema's $ref at {where or '/'} does not name one of its $defs"
                )
            elif keyword == "type" and value not in SUPPORTED_TYPES:
                raise PassportSchemaError(
                    f"the passport schema's type at {where or '/'} is not one the interpreter handles"
                )
            elif keyword == "required" and not (isinstance(value, list) and all(isinstance(key, str) for key in value)):
                raise PassportSchemaError(f"the passport schema's required at {where or '/'} is not a list of keys")
            elif keyword in ("maxItems", "minimum") and (isinstance(value, bool) or not isinstance(value, int | float)):
                raise PassportSchemaError(f"the passport schema's {keyword} at {where or '/'} is not a number")
            elif keyword == "pattern" and not isinstance(value, str):
                raise PassportSchemaError(f"the passport schema's pattern at {where or '/'} is not a string")

    walk(schema, "")


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, int | float):
        return "a number"
    if value is None:
        return "null"
    return "a value of another kind"


_ARTICLE = {
    "object": "an object",
    "array": "a list",
    "integer": "an integer",
    "number": "a number",
    "string": "a string",
}


class _Interpreter:
    def __init__(self, schema: dict[str, Any]) -> None:
        self.definitions: dict[str, Any] = schema.get("$defs", {})
        self.refusals: list[Refusal] = []

    def refuse(self, path: list[str], sentence: str) -> None:
        self.refusals.append(Refusal(_dotted(path), SCHEMA, sentence))

    def validate(self, node: dict[str, Any], value: Any, path: list[str]) -> Any:
        if "$ref" in node:
            value = self.validate(_definition(self.definitions, node["$ref"]), value, path)
        if "type" in node:
            typed, ok = self.check_type(node["type"], value, path)
            if not ok:
                return value
            value = typed
        if "minimum" in node and isinstance(value, int | float) and not isinstance(value, bool):
            if value < node["minimum"]:
                self.refuse(path, f"is {value}, below the minimum {node['minimum']}")
        if "pattern" in node and isinstance(value, str):
            if re.search(node["pattern"], value) is None:
                self.refuse(path, "does not match the pattern the schema states")
        if isinstance(value, list):
            if "maxItems" in node and len(value) > node["maxItems"]:
                self.refuse(path, f"has {len(value)} items, more than {node['maxItems']}")
            if "items" in node:
                value = [self.validate(node["items"], item, [*path, str(index)]) for index, item in enumerate(value)]
        if isinstance(value, dict):
            value = self.validate_object(node, value, path)
        return value

    def check_type(self, expected: str, value: Any, path: list[str]) -> tuple[Any, bool]:
        if expected in ("integer", "number"):
            if isinstance(value, float) and not math.isfinite(value):
                self.refuse(path, f"is not a finite number, not {_ARTICLE[expected]}")
                return value, False
            if expected == "integer" and isinstance(value, float) and value.is_integer():
                return int(value), True
            if isinstance(value, int) and not isinstance(value, bool):
                return value, True
            if expected == "number" and isinstance(value, float):
                return value, True
            if expected == "integer" and isinstance(value, float):
                self.refuse(path, f"is {value}, not an integer")
                return value, False
        elif expected == "object" and isinstance(value, dict):
            return value, True
        elif expected == "array" and isinstance(value, list):
            return value, True
        elif expected == "string" and isinstance(value, str):
            return value, True
        self.refuse(path, f"is {_kind(value)}, not {_ARTICLE[expected]}")
        return value, False

    def validate_object(self, node: dict[str, Any], value: dict[Any, Any], path: list[str]) -> dict[str, Any]:
        properties: dict[str, Any] = node.get("properties", {})
        for key in node.get("required", []):
            if key not in value:
                self.refuse([*path, key], "is required and absent")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                self.refuse([*path, str(key)], "is a key that is not a string")
            elif key in properties:
                result[key] = self.validate(properties[key], item, [*path, key])
            elif node.get("additionalProperties") is False:
                self.refuse([*path, key], "is not a key the schema declares")
            else:
                result[key] = item
        return result


def validate_passport(document: object) -> dict[str, Any]:
    """Validate a parsed passport against the vendored schema, collecting every refusal.

    Return a normalised copy (an integral float that is an integer becomes `int`).
    """
    schema = _vendored_schema()
    interpreter = _Interpreter(schema)
    result = interpreter.validate(schema, document, [])
    if interpreter.refusals:
        raise PassportRefused(interpreter.refusals)
    if not isinstance(result, dict):
        raise PassportRefused([Refusal(DOCUMENT, SCHEMA, "is not an object")])
    return result


# --- the pass -------------------------------------------------------------------------------

#: The fifteen layers, keyed in the registry order a passport's `layers` and `identifiers` use,
#: each with its storage directory under `model/` (`emit.STORAGE_DIRECTORIES`, the same order).
PASSPORT_LAYERS: dict[str, str] = {
    "swc": "swc",
    "interfaces": "interfaces",
    "connectors": "connectors",
    "application_types": "data_types/application",
    "implementation_types": "data_types/implementation",
    "base_types": "data_types/base",
    "requirements": "requirements",
    "compositions": "compositions",
    "tasks": "tasks",
    "sequences": "sequences",
    "hardware": "hardware",
    "mapping_sets": "data_types/mapping_sets",
    "units": "data_types/units",
    "compu_methods": "data_types/compu_methods",
    "data_constraints": "data_types/constraints",
}

#: The parsed rendered documents, per passport layer, as the file-map gate parsed them.
RenderedDocuments = Mapping[str, Sequence[Mapping[str, Any]]]

#: How many entries a `top` list carries.
TOP = 8

#: The declared levels, in the order `by_declared_level` writes them.
ASIL_LEVELS = ("QM", "A", "B", "C", "D")

#: The seven site kinds a requirement on all of them covers; the four hardware kinds are one.
SITE_KIND_COUNT = 7

_HARDWARE_SITE = "hardware element"

#: The site kind of a top-level element's own `satisfies` list, by layer (SWCraft's
#: `trace._SITE_KINDS`); a hardware element's is the one hardware kind here.
_SITE_KINDS = {
    "swc": "swc",
    "interfaces": "interface",
    "compositions": "composition",
    "tasks": "task",
    "hardware": _HARDWARE_SITE,
}

_VERSION = re.compile(r"[1-9][0-9]*")


def _top(counts: Iterable[int]) -> list[int]:
    return sorted(counts, reverse=True)[:TOP]


def _parse_link(value: str) -> tuple[str, str | None]:
    """`trace.parse_link`: `(id, version)`, split at the first `@`.

    The version is the suffix's text only when it is a positive decimal integer with no leading
    zero (`trace.is_version`), kept as text: a canonical spelling compares with `str(version)` at
    any width, where `int()` has a digit limit SWCraft converts around in chunks.
    """
    head, separator, suffix = value.partition("@")
    if not separator:
        return value, None
    return head, (suffix if _VERSION.fullmatch(suffix) else None)


def _link_sites(layers: RenderedDocuments) -> Iterator[tuple[str, str]]:
    """`(site kind, member)` of every `satisfies` member, in `trace.link_sites_of_state`'s order.

    Each component's own list, its ports', its runnables'; then the interfaces, compositions,
    tasks and hardware elements, each its own list.
    """
    for swc in layers["swc"]:
        for value in swc.get("satisfies", []):
            yield "swc", value
        for item in swc.get("ports", []):
            for value in item.get("satisfies", []):
                yield "port", value
        for item in swc.get("runnables", []):
            for value in item.get("satisfies", []):
                yield "runnable", value
    for layer in ("interfaces", "compositions", "tasks", "hardware"):
        for element in layers[layer]:
            for value in element.get("satisfies", []):
                yield _SITE_KINDS[layer], value


def _reached_ports(layers: RenderedDocuments) -> set[tuple[str, str]]:
    reached: set[tuple[str, str]] = set()
    for connector in layers["connectors"]:
        for end in (connector.get("from", {}), connector.get("to", {})):
            if end.get("swc") is not None:
                reached.add((end["swc"], end.get("port")))
    for composition in layers["compositions"]:
        member_types = {
            member.get("name"): member["type"]
            for member in composition.get("members", [])
            if member.get("type") is not None
        }
        for delegation in composition.get("delegations", []):
            member_type = member_types.get(delegation.get("member"))
            if member_type is not None:
                reached.add((member_type, delegation.get("member_port")))
    return reached


def _components(layers: RenderedDocuments) -> tuple[dict[str, Any], list[int], set[str]]:
    swcs = layers["swc"]
    reached = _reached_ports(layers)
    unconnected = [
        sum(1 for item in swc.get("ports", []) if (swc["name"], item.get("name")) not in reached) for swc in swcs
    ]
    fan_out: dict[str, int] = {}
    for swc in swcs:
        for item in swc.get("ports", []):
            if item.get("interface") is not None:
                fan_out[item["interface"]] = fan_out.get(item["interface"], 0) + 1
    counts = [len(swc.get("ports", [])) for swc in swcs]
    threshold = 2 * statistics.median(counts) if counts else 0
    hub_names = {swc["name"] for swc in swcs if len(swc.get("ports", [])) > threshold}
    levels = Counter(str(swc["asil"]).partition("(")[0] for swc in swcs if swc.get("asil") is not None)
    section = {
        "ports": {"total": sum(counts), "top": _top(counts)},
        "hubs": sorted((n for n in counts if n > threshold), reverse=True),
        "unconnected": {"total": sum(unconnected), "max_on_one": max(unconnected, default=0)},
        "by_declared_level": {level: levels.get(level, 0) for level in ASIL_LEVELS},
        "runnables": sum(len(swc.get("runnables", [])) for swc in swcs),
    }
    return section, list(fan_out.values()), hub_names


def _interfaces(layers: RenderedDocuments, fan_outs: list[int]) -> dict[str, Any]:
    kinds = Counter(interface.get("kind") for interface in layers["interfaces"])
    top = _top(fan_outs)
    return {
        "sender_receiver": kinds.get("sender_receiver", 0),
        "client_server": kinds.get("client_server", 0),
        "fan_out": {"max": top[0] if top else 0, "top": top},
    }


def _connectors(layers: RenderedDocuments, hub_names: set[str]) -> dict[str, Any]:
    ordinary = {swc["name"] for swc in layers["swc"]} - hub_names
    between = sum(
        1
        for connector in layers["connectors"]
        if connector.get("from", {}).get("swc") in ordinary and connector.get("to", {}).get("swc") in ordinary
    )
    return {
        "assembly": len(layers["connectors"]),
        "delegation": sum(len(c.get("delegations", [])) for c in layers["compositions"]),
        "links": max(0, between - len(ordinary) // 2),
    }


def _hardware(layers: RenderedDocuments) -> dict[str, Any]:
    kinds = Counter(element.get("kind") for element in layers["hardware"])
    partitions = [element for element in layers["hardware"] if element.get("kind") == "partition"]
    return {
        "cores": kinds.get("core", 0),
        "partitions": len(partitions),
        "memories": kinds.get("memory", 0),
        "rated_partitions": sum(1 for p in partitions if p.get("asil") is not None),
    }


def _requirements(layers: RenderedDocuments) -> dict[str, Any]:
    versions: dict[str, list[Any]] = {}
    for requirement in layers["requirements"]:
        versions.setdefault(requirement["id"], []).append(requirement.get("version"))
    kinds: dict[str, set[str]] = {}
    sites: Counter[str] = Counter()
    on_port: Counter[str] = Counter()
    links = 0
    stale = 0
    for kind, value in _link_sites(layers):
        if ":" in value:  # the element form: it names no requirement and is not a link
            continue
        links += 1
        requirement_id, version = _parse_link(value)
        kinds.setdefault(requirement_id, set()).add(kind)
        sites[requirement_id] += 1
        if kind == "port":
            on_port[requirement_id] += 1
        declared = versions.get(requirement_id, [])
        # `validator.stale_link`'s abstentions (an id declared by no file or by two, a suffix
        # that is not a version), its bare id (stated none, not counted here) and its match.
        if len(declared) == 1 and version is not None and version != str(declared[0]):
            stale += 1
    declared_ids = list(dict.fromkeys(requirement["id"] for requirement in layers["requirements"]))
    stubs = {r["id"] for r in layers["requirements"] if r.get("stub") is True}
    return {
        "on_all_site_kinds": sum(1 for rid in declared_ids if len(kinds.get(rid, ())) == SITE_KIND_COUNT),
        "on_one_port": sum(1 for rid in declared_ids if sites[rid] == 1 and on_port[rid] == 1),
        "unallocated": sum(1 for rid in declared_ids if rid not in stubs and rid not in sites),
        "stale_links": stale,
        "links": links,
    }


def _identifiers(layers: RenderedDocuments) -> dict[str, Any]:
    section: dict[str, Any] = {"max_length": 0}
    longest = 0
    for layer in PASSPORT_LAYERS:
        field = "id" if layer == "requirements" else "name"
        lengths = [len(str(element[field])) for element in layers[layer]]
        longest = max([longest, *lengths])
        section[layer] = {
            "max_length": max(lengths, default=0),
            "mean_length": round(statistics.fmean(lengths), 3) if lengths else 0,
        }
    section["max_length"] = longest
    return section


def compute_passport(layers: RenderedDocuments) -> dict[str, Any]:
    """The passport of a rendered model, read from its parsed documents per layer, never the builder's internals.

    The definitions restate SWCraft's `swcraft_tools/passport.py`:

    - `layers.<name>`: each layer's element count, in the order of `PASSPORT_LAYERS`.
    - Component ports are the `swc` elements' ports (a composition's own count nowhere): `ports.total`
      their sum, `top` the eight largest per-component counts; `hubs` the counts above twice the
      median (`statistics.median`); `runnables` summed; `by_declared_level` by declared `asil`, a
      decomposed `X(Y)` counted as `X`.
    - `unconnected`: the component ports no connector end on an `swc` and no delegation from a typed
      member reaches; `total` their number, `max_on_one` the most on one component.
    - `fan_out`: per interface name, the component ports naming it; `max` the largest (0 when none),
      `top` the eight largest.
    - `connectors.assembly`: the connectors; `delegation`: the compositions' delegations; `links`:
      `max(0, C - m // 2)`, `m` the non-hub components, `C` the connectors between two of them.
    - `hardware`: the cores, partitions, memories and rated partitions; `tasks.idle`: the tasks with
      no runnable.
    - `requirements`: every `satisfies` member without a `:` is a link, its site kind one of seven (the four hardware
      kinds as one); `links` counts every link, `on_all_site_kinds`, `on_one_port` and `unallocated` the declared ids (a
      stub never unallocated), `stale_links` the links stating another version than the current one (an id undeclared or
      declared twice, a suffix that is not a version and a bare id not counted).
    - `identifiers`: per layer the longest and `round(statistics.fmean, 3)` of the identity field's
      lengths (an int `0` for an empty layer), and the longest overall.
    - `contract_version` is `MODEL_CONTRACT_VERSION`, `passport_version` `PASSPORT_VERSION`.

    Raises:
        GenerationError: *layers* lacks a layer of `PASSPORT_LAYERS`.

    A document shape SWCraft would refuse to load is not refused here.
    """
    missing = [layer for layer in PASSPORT_LAYERS if layer not in layers]
    if missing:
        raise GenerationError(f"passport pass: rendered documents lack layer {missing[0]}")
    components, fan_outs, hub_names = _components(layers)
    passport: dict[str, Any] = {
        "passport_version": PASSPORT_VERSION,
        "contract_version": MODEL_CONTRACT_VERSION,
        "layers": {layer: len(layers[layer]) for layer in PASSPORT_LAYERS},
        "components": components,
        "interfaces": _interfaces(layers, fan_outs),
        "connectors": _connectors(layers, hub_names),
        "hardware": _hardware(layers),
        "tasks": {"idle": sum(1 for task in layers["tasks"] if not task.get("runnables"))},
        "requirements": _requirements(layers),
        "identifiers": _identifiers(layers),
    }
    return passport
