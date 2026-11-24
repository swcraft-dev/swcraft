"""Measure every recipe form and the reference calls on one or more projects: figures only.

``python scripts/measure_forms.py PROJECT [PROJECT ...] [--output FILE]`` asks each project's MCP
surface, through the in-memory FastMCP client, every request of every recipe (the assistant
profile's and the How-to pages') and a fixed list of reference calls, and writes one Markdown record
of each call's wire bytes, seconds and outcome. A PROJECT is its ``swcraft.yaml``; projects are
labelled ``model A``, ``model B``, ... in argument order. A project that cannot be read is one
sentence naming its label and the exception's class, and exit 1.

**Figures only.** No element name, value or text of a model reaches the record, stdout or stderr:
placeholders are filled locally and never printed, an answer is reduced to the counts its structure
gives (:func:`figures_of`) -- never a number the model holds, such as a memory's ``size`` or a
``resource`` row's figures -- and text is counted by lines only. A refusal is reduced to its closed
error code, its stated size and its structured hints' keys from a closed set (:data:`HINT_KEYS`)
with their integer ``limit`` and closed ``form``; a plain-text refusal is its code alone.

**The targets** are the passport locator's (largest component, heaviest interface). A recipe whose
placeholder the model cannot fill is recorded as ``not asked: the model has no <kind>``. The
reference calls' labels are stable, so records of two releases compare by label.

It needs the dev venv (``fastmcp``, ``swcraft_mcp`` and the tools tree's recipe parser). The server
runs under ``SWCRAFT_REVIEW=stage-only``, and nothing it is asked writes.

**The pairing.** :func:`pair_agrees` is the agreement rule between a real model and its specimen:
the same outcome, the same figures and bytes within the caller's tolerance.
:data:`NAME_DEPENDENT_CALLS` and the recipes that ask by text are recorded and never judged.

Not reached: a number is one call's, never a distribution.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import functools
import importlib.util
import logging
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

from swcraft_tools import __version__
from swcraft_tools.editing import registry as editing_registry
from swcraft_tools.editing.protocol import DISCOVER_FORMS, DiscoverRequest, ReadRequest
from swcraft_tools.editing.read import NARROWING_ORDER, RESPONSE_BUDGET_WIRE_BYTES
from swcraft_tools.env import force_utf8_streams
from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import ModelState, load_model_state
from swcraft_tools.passport import Locator, compute_passport
from swcraft_tools.project import require_project
from swcraft_tools.safety import ASIL_LEVELS
from swcraft_tools.trace import is_element_form, link_sites_of_state, parse_link

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent
TOOLS_TESTS = REPO_ROOT / "packages" / "tools" / "tests"

#: The module name ``measure_mcp_scale.py`` is loaded under, the tests' too, so all share one module object.
SCALE_MODULE = "swcraft_measure_mcp_scale"

#: The answer size the record counts against, report-only; not the response budget.
REPORT_THRESHOLD = 8192

#: How many components the batch and ``find`` reference calls name.
BATCH = 16

#: The closed values a hint's ``form`` may carry, the only string value an outcome cell prints.
CLOSED_FORMS = frozenset((*NARROWING_ORDER, *DISCOVER_FORMS, "count"))

#: The keys a structured hint may carry, the only keys an outcome cell names; any other is
#: :data:`UNRECOGNISED_KEY`. A plain-text refusal's ``Ask for it with`` sentence is never read: a
#: message can quote an element name that itself reads as one.
HINT_KEYS = frozenset(
    {
        "operation",
        *(item.name for item in fields(ReadRequest)),
        *(item.name for item in fields(DiscoverRequest)),
    }
)
UNRECOGNISED_KEY = "unrecognised-key"

#: What a key of ``impact_of``'s ``matched`` counts is printed as when it is no declared element kind.
UNRECOGNISED_KIND = "unrecognised-kind"

#: The shape of a closed error code; anything else is printed as :data:`UNRECOGNISED_CODE`.
_ERROR_CODE = re.compile(r"[a-z][a-z-]*")
UNRECOGNISED_CODE = "unrecognised-code"

#: The layers the reference calls read in all three of their first-page forms, when non-empty.
READ_LAYERS = ("swc", "interfaces", "connectors", "application_types", "implementation_types", "compositions")

#: A placeholder and the kind of element the model lacks when it cannot be filled.
PLACEHOLDER_KINDS: dict[str, str] = {
    "<COMPONENT>": "component",
    "<INTERFACE>": "sender-receiver interface that a port names",
    "<DATA_ELEMENT>": "data element on that interface",
    "<ELEMENT>": "interface that a port names",
    "<LEVEL>": "level",
    "<PARTITION>": "partition",
    "<CORE>": "core",
    "<MEMORY>": "memory",
    "<REQUIREMENT>": "requirement",
    "<TOPIC>": "requirement title",
}

OK = "ok"
REFUSED = "refused"
ERROR = "error"
NOT_ASKED = "not asked"


def _scale() -> ModuleType:
    """``measure_mcp_scale.py``, imported by path once per interpreter.

    Registered under :data:`SCALE_MODULE`, the name the tests load it by, so both share one module object.
    """
    cached = sys.modules.get(SCALE_MODULE)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(SCALE_MODULE, SCRIPTS / "measure_mcp_scale.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[SCALE_MODULE] = module
    spec.loader.exec_module(module)
    return module


def _recipes_module() -> ModuleType:
    """The tools tree's recipe parser, with ``packages/tools/tests`` and its ``support/`` on the path.

    The module imports ``conftest`` and ``agent_recipe_support`` by top-level name, which pytest finds
    through its ``pythonpath``; a standalone interpreter needs both entries inserted.
    """
    for entry in (TOOLS_TESTS / "support", TOOLS_TESTS):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))
    import test_agent_recipes  # noqa: PLC0415 - on the path only once the lines above have run

    return test_agent_recipes


def all_recipes() -> list[Any]:
    """Every recipe, by the tools tree's one loader: the profile's in its order, then the How-to pages'."""
    return _recipes_module().all_recipes()


@dataclass(frozen=True)
class Planned:
    """One call to make, or the reason it is not asked (``tool`` is then ``None``)."""

    label: str
    tool: str | None
    arguments: Mapping[str, Any]
    reason: str = ""


@dataclass(frozen=True)
class Call:
    """One measured call: its label, status, wire bytes, seconds and outcome cell.

    ``code`` is a refusal's closed error code (``""`` otherwise) and ``figures`` an answer's counts
    (:func:`figures_of`, ``""`` otherwise): the two parts of the cell :func:`pair_agrees` compares.
    """

    label: str
    status: str
    wire_bytes: int
    seconds: float
    outcome: str
    code: str = ""
    figures: str = ""


@dataclass(frozen=True)
class FormsMeasurement:
    """One project's figures: its passport, the recipe calls and the reference calls."""

    passport: Mapping[str, Any]
    recipe_calls: tuple[Call, ...]
    reference_calls: tuple[Call, ...]

    @property
    def calls(self) -> tuple[Call, ...]:
        return self.recipe_calls + self.reference_calls

    @property
    def errors(self) -> tuple[str, ...]:
        """The labels of the calls that answered an error which is not a refusal: a defect."""
        return tuple(call.label for call in self.calls if call.status == ERROR)


def _smallest_name(state: ModelState, kind: str) -> str | None:
    names = sorted(element.name for element in state["hardware"] if element.kind == kind)
    return names[0] if names else None


def _level(passport: Mapping[str, Any]) -> str:
    """The most frequent declared level; ties in ``ASIL_LEVELS`` order, ``QM`` when none is declared."""
    counts = passport["components"]["by_declared_level"]
    best = max(ASIL_LEVELS, key=lambda level: (counts.get(level, 0), -ASIL_LEVELS.index(level)))
    return best if counts.get(best, 0) else ASIL_LEVELS[0]


def _most_linked_requirement(state: ModelState) -> Any | None:
    """The requirement with the most links, ties broken by id; ``None`` when the model has none."""
    registry = current_registry()
    links: Counter[str] = Counter()
    for state_site in link_sites_of_state({name: list(state[name]) for name in registry.layer_names}):
        if not is_element_form(state_site.site.value):
            links[parse_link(state_site.site.value).id] += 1
    requirements = sorted(state["requirements"], key=lambda requirement: (-links[requirement.id], requirement.id))
    return requirements[0] if requirements else None


def placeholder_fills(state: ModelState, passport: Mapping[str, Any], locator: Locator) -> dict[str, str | None]:
    """Each recipe placeholder's value in this model, ``None`` where the model has nothing to fill it with."""
    fills: dict[str, str | None] = dict.fromkeys(PLACEHOLDER_KINDS)
    try:
        fills["<COMPONENT>"] = locator.largest_component()
    except LookupError:
        pass
    try:
        fills["<ELEMENT>"] = locator.heaviest_interface()
    except LookupError:
        pass
    sender_receiver = locator.heaviest_sender_receiver_interfaces()
    if sender_receiver:
        fills["<INTERFACE>"] = sender_receiver[0]
        interface = next(element for element in state["interfaces"] if element.name == sender_receiver[0])
        if interface.data_elements:
            fills["<DATA_ELEMENT>"] = interface.data_elements[0].name
    fills["<LEVEL>"] = _level(passport)
    fills["<PARTITION>"] = _smallest_name(state, "partition")
    fills["<CORE>"] = _smallest_name(state, "core")
    fills["<MEMORY>"] = _smallest_name(state, "memory")
    requirement = _most_linked_requirement(state)
    if requirement is not None:
        fills["<REQUIREMENT>"] = requirement.id
        words = requirement.title.split()[:2]
        fills["<TOPIC>"] = " ".join(words) if words else None
    return fills


def _filled(value: Any, fills: Mapping[str, str | None], missing: list[str]) -> Any:
    if isinstance(value, dict):
        return {key: _filled(item, fills, missing) for key, item in value.items()}
    if isinstance(value, list):
        return [_filled(item, fills, missing) for item in value]
    if isinstance(value, str) and _recipes_module().PLACEHOLDER.fullmatch(value):
        if value not in PLACEHOLDER_KINDS:
            raise ValueError(f"the recipe placeholder {value} has no row in PLACEHOLDER_KINDS")
        if fills[value] is None:
            missing.append(value)
        return fills[value]
    return value


def recipe_plan(recipes: Sequence[Any], fills: Mapping[str, str | None]) -> list[Planned]:
    """Every request of every recipe, in order, labelled ``<title>, <i> of <n>``."""
    planned = []
    for recipe in recipes:
        for index, request in enumerate(recipe.requests, start=1):
            label = f"{recipe.title}, {index} of {len(recipe.requests)}"
            missing: list[str] = []
            arguments = _filled(request, fills, missing)
            if missing:
                planned.append(Planned(label, None, {}, f"the model has no {PLACEHOLDER_KINDS[missing[0]]}"))
            else:
                planned.append(Planned(label, recipe.tool, arguments))
    return planned


def misspell(name: str) -> str:
    """*name* with characters 2 and 3 swapped (an ``x`` appended below four characters)."""
    if len(name) < 4:
        return name + "x"
    return name[:2] + name[3] + name[2] + name[4:]


def reference_plan(state: ModelState, locator: Locator) -> list[Planned]:
    """The reference calls, in a fixed order under stable labels."""
    planned: list[Planned] = []

    def ask(label: str, tool: str, **arguments: Any) -> None:
        planned.append(Planned(label, tool, arguments))

    def skip(label: str, reason: str) -> None:
        planned.append(Planned(label, None, {}, reason))

    ask("discover baseline (default)", "discover")
    ask("discover summary", "discover", form="summary")
    ask("discover full", "discover", form="full")
    for layer in ("swc", "interfaces", "connectors"):
        ask(f"discover {layer} full", "discover", layer=layer, form="full")
    ask("read map (no address)", "read")
    for layer in READ_LAYERS:
        for form, label in (("count", "count"), ("index", "index, first page"), ("summary", "summary, first page")):
            if len(state[layer]):
                ask(f"read {layer} {label}", "read", address={"layer": layer}, form=form)
            else:
                skip(f"read {layer} {label}", "the layer is empty")

    def on_present(value: Any, reason: str, calls: Sequence[tuple[str, str, Mapping[str, Any]]]) -> None:
        """Each of *calls* asked when *value* is there, else not asked for *reason*."""
        for label, tool, arguments in calls:
            if value is None:
                skip(label, reason)
            else:
                ask(label, tool, **arguments)

    try:
        component = locator.largest_component()
    except LookupError:
        component = None
    largest = locator.largest_components(BATCH)
    swc = {"layer": "swc", "element": component}
    no_component = "the model has no component"
    on_present(
        component,
        no_component,
        [
            ("read largest SWC full", "read", {"address": swc, "form": "full"}),
            ("read largest SWC summary", "read", {"address": swc, "form": "summary"}),
            (
                "read 16 largest SWCs summary (batch)",
                "read",
                {"addresses": [{"layer": "swc", "element": name} for name in largest], "form": "summary"},
            ),
            (
                "read 16 largest SWCs full (batch)",
                "read",
                {"addresses": [{"layer": "swc", "element": name} for name in largest], "form": "full"},
            ),
        ],
    )
    # The find calls are built from the component's spelling, so without one there is nothing to build.
    spelt = component or ""
    on_present(
        component,
        no_component,
        [
            (
                "find 4 names (exact, lower, prefix, misspelt)",
                "find",
                {"names": [spelt, spelt.lower(), spelt[:6], misspell(spelt)]},
            ),
            ("find 16 names (the 16 largest SWCs)", "find", {"names": list(largest)}),
            (
                "find text (two words of the largest SWC's name)",
                "find",
                {"text": " ".join(filter(None, [spelt[:4], spelt[4:8]]))},
            ),
        ],
    )
    # ``members`` and ``derived`` are layer-address controls: at an element address they are refused
    # and would measure nothing.
    unconnected = {"members": "ports", "derived": ["unconnected"]}
    whole = {"layer": "swc"}
    ask("derived unconnected, whole swc layer, first page", "read", address=whole, form="index", **unconnected)
    ask("derived unconnected count, whole swc layer", "read", address=whole, form="count", **unconnected)
    ask("derived partitions, swc index", "read", address=whole, form="index", derived=["partitions"])
    ask(
        "derived effective_level count rating B",
        "read",
        address=whole,
        form="count",
        derived=["effective_level"],
        where=[{"field": "derived.effective_level.rating", "equals": "B"}],
    )
    ask("derived effective_level, swc index", "read", address=whole, form="index", derived=["effective_level"])
    connectors = {"layer": "connectors"}
    ask(
        "derived crossing_ends, connectors index first page",
        "read",
        address=connectors,
        form="index",
        derived=["crossing_ends"],
    )
    ask(
        "derived connection_ends, connectors count",
        "read",
        address=connectors,
        form="count",
        derived=["connection_ends"],
    )

    try:
        interface = locator.heaviest_interface()
    except LookupError:
        interface = None
    on_present(
        interface,
        "the model has no interface that a port names",
        [
            ("impact_of heaviest interface, count", "impact_of", {"name": interface, "form": "count"}),
            ("impact_of heaviest interface, summary (default)", "impact_of", {"name": interface}),
            (
                "impact_of heaviest interface, summary depth 1 limit 20",
                "impact_of",
                {"name": interface, "depth": 1, "limit": 20},
            ),
        ],
    )
    on_present(
        component,
        no_component,
        [
            ("impact_of largest SWC, count", "impact_of", {"name": component, "form": "count"}),
            ("impact_of largest SWC, summary", "impact_of", {"name": component}),
            (
                "impact_of largest SWC, dependencies count",
                "impact_of",
                {"name": component, "form": "count", "direction": "dependencies"},
            ),
            ("impact_of largest SWC, dependencies", "impact_of", {"name": component, "direction": "dependencies"}),
        ],
    )

    ask("resource_report overview", "resource_report")
    core = _smallest_name(state, "core")
    on_present(core, "the model has no core", [("resource_report core", "resource_report", {"core": core})])
    memory = _smallest_name(state, "memory")
    on_present(
        memory,
        "the model has no memory",
        [
            ("resource_report memory", "resource_report", {"memory": memory}),
            ("resource_report fits 40 KB in memory", "resource_report", {"fits": {"memory": memory, "bytes": 40960}}),
        ],
    )
    on_present(
        len(state["tasks"]) or None,
        "the model has no task",
        [("derived core, tasks index", "read", {"address": {"layer": "tasks"}, "form": "index", "derived": ["core"]})],
    )
    on_present(
        len(state["requirements"]) or None,
        "the model has no requirement",
        [
            (
                "derived realised_by, requirements index",
                "read",
                {"address": {"layer": "requirements"}, "form": "index", "derived": ["realised_by"]},
            )
        ],
    )
    return planned


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _kind_or_unrecognised(key: object, kinds: frozenset[str]) -> str:
    return key if isinstance(key, str) and key in kinds else UNRECOGNISED_KIND


def element_kinds() -> frozenset[str]:
    """The running contract's element-kind tokens, the keys ``impact_of``'s ``count`` form emits."""
    registry = current_registry()
    return frozenset(registry.layer(layer).token for layer in registry.layer_names)


def _matched_figures(matched: Mapping[Any, Any]) -> str:
    """``impact_of``'s ``count`` form: per matched layer its total and its count per dependent kind.

    The keys at both levels are element-kind tokens (:func:`element_kinds`), printed only when they
    are declared ones (else :data:`UNRECOGNISED_KIND`); a total or a count that is not an integer is
    left out. Sorted by the printed key, so two answers with the same counts render the same line.
    """
    kinds_declared = element_kinds()
    cells = []
    for layer, counts in matched.items():
        if not isinstance(counts, Mapping):
            continue
        total = _count(counts.get("total"))
        by_kind = counts.get("by_kind")
        kinds = sorted(
            f"{_kind_or_unrecognised(kind, kinds_declared)} {count}"
            for kind, count in (by_kind.items() if isinstance(by_kind, Mapping) else ())
            if _count(count) is not None
        )
        cell = _kind_or_unrecognised(layer, kinds_declared)
        if total is not None:
            cell += f" total {total}"
        if kinds:
            cell += " (" + ", ".join(kinds) + ")"
        cells.append(cell)
    return f"matched {len(matched)}" + (": " + ", ".join(sorted(cells)) if cells else "")


#: The top-level keys of an answer whose list is printed as its length. Every one is this surface's
#: own field name; a key outside the set is not read at all.
LIST_KEYS = (
    "names",
    "matches",
    "candidates",
    "elements",
    "dependents",
    "dependencies",
    "scope",
    "cores",
    "memories",
    "tasks",
    "consumers",
    "findings",
    "topology",
    "undeclared_first",
    "existing_links",
)

#: The top-level keys of an answer whose integer is printed as it stands: each a count of elements
#: the reply's structure gives, never a number the model holds.
COUNT_KEYS = ("neighbours", "undeclared", "declared_zero")

#: ``resource_report``'s ``fits`` verdict as a closed token: ``true``, ``false`` or ``null``.
FITS_TOKENS: dict[object, str] = {True: "yes", False: "no", None: "unknown"}

#: The key :func:`figures_of` prints for a ``read`` map's ``counts`` key that is no declared layer.
UNRECOGNISED_LAYER = "unrecognised-layer"


def _length(value: Any) -> int | None:
    """A list's or a mapping's length; ``None`` for anything else, an integer included."""
    return len(value) if isinstance(value, (list, Mapping)) else None


def _collection_fields_of_layer(layer: str) -> frozenset[str]:
    """The field names every element kind of *layer* that declares one declares a collection."""
    kinds = [kind for kind in editing_registry.current().kinds.values() if kind.layer == layer and not kind.member_only]
    names = {field.name for kind in kinds for field in kind.fields}
    return frozenset(
        name
        for name in names
        if all(field.collection is not None for kind in kinds for field in kind.fields if field.name == name)
    )


def summary_collection_fields(answer: Mapping[Any, Any]) -> frozenset[str]:
    """The keys of a ``read`` answer's ``value`` whose integer is a collection's count.

    Only a ``summary`` answer at an element address (no ``path``) of a declared layer has them: the
    summary answers each declared collection by its count. A field counts only when every element
    kind of the layer declaring it declares it a collection, since the kind is not read off the value.
    """
    if answer.get("form") != "summary":
        return frozenset()
    address = answer.get("address")
    if not isinstance(address, Mapping) or address.get("path") or not isinstance(address.get("element"), str):
        return frozenset()
    layer = address.get("layer")
    if not isinstance(layer, str) or layer not in current_registry().layer_names:
        return frozenset()
    return _collection_fields_of_layer(layer)


def _value_figures(value: Any, collections: frozenset[str] = frozenset()) -> str:
    """One ``read`` answer's ``value``: a page of rows, an ``index`` form, or one element.

    A list is its rows; an integer ``count`` beside an ``identities`` list is the ``index`` form. Any
    other mapping is one element: per field in key order, without the key, a list's or mapping's
    length, the integer of a field in *collections*, and ``-`` for every other field (an element's
    other integers are model values).
    """
    if isinstance(value, list):
        return f"rows {len(value)}"
    if not isinstance(value, Mapping):
        return ""
    identities = value.get("identities")
    if (count := _count(value.get("count"))) is not None and isinstance(identities, list):
        return f"rows {count} identities {len(identities)}"
    sizes = [
        _length(value[key]) if key not in collections else _count(value[key])
        for key in sorted(key for key in value if isinstance(key, str))
    ]
    return "element " + ",".join("-" if size is None else str(size) for size in sizes)


def _answer_figures(answer: Any) -> str:
    """One answer of a batch: its refusal's code, or its value's figures."""
    if not isinstance(answer, Mapping):
        return "?"
    if answer.get("response") == "error":
        code = answer.get("error")
        return f"refused {code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else UNRECOGNISED_CODE}"
    return _value_figures(answer.get("value"), summary_collection_fields(answer)) or "?"


def _layer_counts(counts: Mapping[Any, Any]) -> str:
    """A ``read`` map's ``counts``: per declared layer its integer, sorted by the printed key."""
    declared = frozenset(current_registry().layer_names)
    cells = sorted(
        f"{layer if isinstance(layer, str) and layer in declared else UNRECOGNISED_LAYER} {count}"
        for layer, count in counts.items()
        if _count(count) is not None
    )
    return "counts " + ", ".join(cells)


def _layers_figures(layers: Any) -> str:
    """``discover``'s ``layers`` list (present ones, element kinds, fields) or a map's per-layer names."""
    if isinstance(layers, Mapping):
        return f"layers {len(layers)} names {sum(len(names) for names in layers.values() if isinstance(names, list))}"
    if not isinstance(layers, list):
        return ""
    entries = [layer for layer in layers if isinstance(layer, Mapping)]
    kinds = [kind for layer in entries for kind in layer.get("element_kinds") or () if isinstance(kind, Mapping)]
    present = sum(1 for layer in entries if layer.get("present") is True)
    field_count = sum(len(kind["fields"]) for kind in kinds if isinstance(kind.get("fields"), list))
    return f"layers {len(layers)} present {present} kinds {len(kinds)} fields {field_count}"


def figures_of(result: object) -> str:
    """Numbers only from an answer, for every reply shape the recipe forms and the reference calls get.

    Every number is a count the reply's structure gives and every key from a closed set (the module
    docstring's audit); a plain text is its line count alone. Not read: a string anywhere, a number
    the model holds, and any key below the top level other than the ones named.
    """
    scale = _scale()
    body = scale.body_of(result)
    if body is None:
        # A plain text can quote a model's names, so it is counted by lines, never parsed.
        lines = scale.text_of(result).splitlines()
        return f"lines {len(lines)}" if lines else ""
    parts = []
    if (count := _count(body.get("count"))) is not None:
        parts.append(f"count {count}")
    page = body.get("page")
    if isinstance(page, dict):
        total, returned, truncated = _count(page.get("total")), _count(page.get("returned")), page.get("truncated")
        if total is not None and returned is not None and isinstance(truncated, bool):
            parts.append(f"page total {total} returned {returned} truncated {truncated}")
    matched = body.get("matched")
    if isinstance(matched, Mapping):
        parts.append(_matched_figures(matched))
    if value := _value_figures(body.get("value"), summary_collection_fields(body)):
        parts.append(value)
    answers = body.get("answers")
    if isinstance(answers, list):
        parts.append(f"answers {len(answers)}: " + " | ".join(_answer_figures(answer) for answer in answers))
    results = body.get("results")
    if isinstance(results, list):
        candidates = [
            str(len(item["candidates"]))
            if isinstance(item, Mapping) and isinstance(item.get("candidates"), list)
            else "?"
            for item in results
        ]
        parts.append(f"results {len(results)}" + (f" (candidates {', '.join(candidates)})" if candidates else ""))
    counts = body.get("counts")
    if isinstance(counts, Mapping):
        parts.append(_layer_counts(counts))
    if layers := _layers_figures(body.get("layers")):
        parts.append(layers)
    for key in LIST_KEYS:
        if isinstance(body.get(key), list):
            parts.append(f"{key} {len(body[key])}")
        elif isinstance(body.get(key), dict):
            parts.append(f"{key} keys {len(body[key])}")
    for key in COUNT_KEYS:
        if (value := _count(body.get(key))) is not None:
            parts.append(f"{key} {value}")
    if "fits" in body and (body["fits"] is None or isinstance(body["fits"], bool)):
        parts.append(f"fits {FITS_TOKENS[body['fits']]}")
    return "; ".join(parts)


def hint_cell(hint: Mapping[str, Any]) -> str:
    """One structured hint as its keys in sorted order: ``limit`` with an integer, ``form`` with its closed value.

    ``after`` and every other value are left out: a cursor and an address carry names. Keys outside
    :data:`HINT_KEYS` become one trailing :data:`UNRECOGNISED_KEY`, so not even their place is printed.
    """
    parts = []
    for key in sorted(key for key in hint if key in HINT_KEYS):
        value = hint[key]
        if key == "after":
            continue
        if key == "limit" and _count(value) is not None:
            parts.append(f"{key}={value}")
        elif key == "form" and isinstance(value, str) and value in CLOSED_FORMS:
            parts.append(f"form={value}")
        else:
            parts.append(key)
    if any(key not in HINT_KEYS for key in hint):
        parts.append(UNRECOGNISED_KEY)
    return "+".join(parts)


def refusal_code(result: object) -> str:
    """A refusal's closed error code (:data:`UNRECOGNISED_CODE` for any other string); ``""`` for an answer."""
    refusal = _scale().refusal_of(result)
    if refusal is None:
        return ""
    code = refusal.get("error")
    return code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else UNRECOGNISED_CODE


class Outcome(NamedTuple):
    """One result read once: its status, its cell, a refusal's code and an answer's figures."""

    status: str
    cell: str
    code: str = ""
    figures: str = ""


def outcome_cell(result: object) -> Outcome:
    """The :class:`Outcome` of one result: ``ok`` with its figures, a refusal by its code, or a tool error."""
    scale = _scale()
    refusal = scale.refusal_of(result)
    if refusal is None:
        if getattr(result, "is_error", False):
            return Outcome(ERROR, "tool error")
        figures = figures_of(result)
        return Outcome(OK, f"ok ({figures})" if figures else "ok", figures=figures)
    code = refusal_code(result)
    cell = f"refused `{code}`"
    if code == scale.TEXT_REFUSAL:
        # A plain-text refusal may quote an element name: nothing of it reaches the cell.
        return Outcome(REFUSED, cell, code=code)
    measured = refusal.get("measured")
    if isinstance(measured, dict) and (size := _count(measured.get("wire_bytes"))) is not None:
        cell += f", would be {size:,} B"
    hints = scale.hints_of(result)
    if hints is not None:
        cell += f", hints [{', '.join(hint_cell(hint) for hint in hints)}]"
    return Outcome(REFUSED, cell, code=code)


#: The logger namespaces the in-memory server logs under: FastMCP logs an unexpected tool
#: exception's text and pydantic's errors with their input values, and ``mcp``'s records reach the
#: last-resort stderr handler.
SILENCED_LOGGERS = ("fastmcp", "mcp")


@contextlib.contextmanager
def _server_logs_silenced() -> Any:
    """Nothing logged under :data:`SILENCED_LOGGERS` while inside; every setting restored after.

    Each existing logger is ``disabled``, and each namespace root takes a level above ``CRITICAL``
    for children created during the calls. Not reached: a child that sets its own level later, and
    output written without ``logging``.
    """
    prefixes = tuple(f"{name}." for name in SILENCED_LOGGERS)
    existing = [
        logger
        for name, logger in logging.Logger.manager.loggerDict.items()
        if isinstance(logger, logging.Logger) and (name in SILENCED_LOGGERS or name.startswith(prefixes))
    ]
    roots = [logging.getLogger(name) for name in SILENCED_LOGGERS]
    saved_disabled = {logger: logger.disabled for logger in {*existing, *roots}}
    saved_levels = {logger: logger.level for logger in roots}
    for logger in saved_disabled:
        logger.disabled = True
    for logger in roots:
        logger.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        for logger, level in saved_levels.items():
            logger.setLevel(level)
        for logger, disabled in saved_disabled.items():
            logger.disabled = disabled


async def _run(client: Any, plan: Sequence[Planned]) -> tuple[Call, ...]:
    """Every call of *plan* through *client*, in order, each reduced to its :class:`Call`."""
    calls = []
    for planned in plan:
        if planned.tool is None:
            calls.append(Call(planned.label, NOT_ASKED, 0, 0.0, f"not asked: {planned.reason}"))
            continue
        started = time.perf_counter()
        result = await client.call_tool(planned.tool, dict(planned.arguments), raise_on_error=False)
        elapsed = time.perf_counter() - started
        outcome = outcome_cell(result)
        calls.append(
            Call(
                planned.label,
                outcome.status,
                _scale().wire_bytes(result),
                elapsed,
                outcome.cell,
                code=outcome.code,
                figures=outcome.figures,
            )
        )
    return tuple(calls)


async def _ask(recipes: Sequence[Planned], references: Sequence[Planned]) -> tuple[tuple[Call, ...], tuple[Call, ...]]:
    from fastmcp import Client  # noqa: PLC0415 - after the environment the server reads is set
    from swcraft_mcp import server  # noqa: PLC0415

    async with Client(server.mcp) as client:
        return await _run(client, recipes), await _run(client, references)


def measure(manifest: Path) -> FormsMeasurement:
    """Every recipe form and every reference call on the project of *manifest*, measured.

    Asks the server in this process under ``SWCRAFT_REVIEW=stage-only``, its logging silenced; the
    environment and the server's cached project are restored afterwards, so several projects can be
    measured in one process. Raises what the project's load raises.
    """
    project = require_project(str(manifest))
    model_root = project.resolved_model_root
    state = load_model_state(model_root, registry=current_registry())
    result = compute_passport(model_root)
    recipes = recipe_plan(all_recipes(), placeholder_fills(state, result.passport, result.locator))
    references = reference_plan(state, result.locator)

    from swcraft_mcp import server  # noqa: PLC0415 - the server reads its environment at import

    saved = {key: os.environ.get(key) for key in ("SWCRAFT_MANIFEST", "SWCRAFT_REVIEW")}
    os.environ["SWCRAFT_MANIFEST"] = str(manifest)
    # Set rather than assumed: an exported `elicit` would turn the measurement into a question nobody answers.
    os.environ["SWCRAFT_REVIEW"] = "stage-only"
    server._configure_review_mode()
    # The server caches its project; dropped so that this project, and not a previous one, answers.
    server._PROJECT = None
    try:
        with _server_logs_silenced():
            recipe_calls, reference_calls = asyncio.run(_ask(recipes, references))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        server._PROJECT = None
        server._configure_review_mode()
    return FormsMeasurement(result.passport, recipe_calls, reference_calls)


def _passport_rows(passport: Mapping[str, Any]) -> list[tuple[str, str]]:
    layers = passport["layers"]

    def joined(*names: str, sep: str = " / ") -> str:
        return sep.join(f"{layers[name]:,}" for name in names)

    top = passport["components"]["ports"]["top"]
    return [
        ("Components / interfaces / connectors", joined("swc", "interfaces", "connectors")),
        (
            "Data types (application / implementation / base)",
            joined("application_types", "implementation_types", "base_types"),
        ),
        ("Compositions, hardware", joined("compositions", "hardware", sep=", ")),
        ("Tasks, requirements", joined("tasks", "requirements", sep=", ")),
        ("Largest component, by ports", f"{top[0] if top else 0:,}"),
        ("Heaviest interface, by ports naming it", f"{passport['interfaces']['fan_out']['max']:,}"),
        ("Ports no connector reaches", f"{passport['components']['unconnected']['total']:,}"),
    ]


def _call_cell(call: Call) -> str:
    if call.status == NOT_ASKED:
        return call.outcome
    return f"{call.wire_bytes:,} B, {call.seconds:.2f} s, {call.outcome}"


def _table(heading: str, labels: Sequence[str], rows: Sequence[tuple[str, Sequence[str]]]) -> list[str]:
    lines = [f"| {heading} | {' | '.join(labels)} |", "|---|" + "---|" * len(labels)]
    lines += [f"| {label} | {' | '.join(cells)} |" for label, cells in rows]
    return lines


def _calls_table(measurements: Sequence[FormsMeasurement], labels: Sequence[str], which: str) -> list[str]:
    columns = [getattr(measurement, which) for measurement in measurements]
    order = list(dict.fromkeys(call.label for calls in columns for call in calls))
    by_label = [{call.label: call for call in calls} for calls in columns]
    rows = [
        (label, [_call_cell(found[label]) if label in found else "not asked" for found in by_label]) for label in order
    ]
    return _table("call", labels, rows)


def counts_table(
    measurements: Sequence[FormsMeasurement], labels: Sequence[str], facts: Sequence[tuple[str, Sequence[str]]] = ()
) -> list[str]:
    """The record's first table: each model's passport counts, one column per label, then *facts*.

    *facts* are further rows, one cell per label. Figures only; *measurements* is not empty.
    """
    passport_rows = [_passport_rows(measurement.passport) for measurement in measurements]
    first = [(label, [rows[index][1] for rows in passport_rows]) for index, (label, _) in enumerate(passport_rows[0])]
    return _table("", labels, [*first, *facts])


def render_record(measurements: Sequence[FormsMeasurement], labels: Sequence[str], *, date: str) -> str:
    """The Markdown record of *measurements*, one column per label: figures only."""
    counted = "models" if len(measurements) != 1 else "model"
    lines = [
        f"# The question forms on {len(measurements)} {counted} — {date}",
        "",
        f"Built with SWCraft {__version__}; the response budget is {RESPONSE_BUDGET_WIRE_BYTES:,} wire bytes. "
        "Figures only: counts, byte sizes, seconds and closed codes, and no element name, no value and no "
        "text of a model. No LLM was run: every call is a fixed request through the in-memory client. "
        "The labels follow the order the projects were given in, and move when a model is added before "
        "another. Seconds are one "
        "call's, which is not a measurement.",
        "",
    ]
    lines += counts_table(measurements, labels) if measurements else []
    lines += ["", "## Recipe forms", ""]
    lines += _calls_table(measurements, labels, "recipe_calls")
    lines += ["", "## Reference calls", ""]
    lines += _calls_table(measurements, labels, "reference_calls")
    lines.append("")
    for label, measurement in zip(labels, measurements, strict=True):
        statuses = Counter(call.status for call in measurement.calls)
        over = sum(1 for call in measurement.calls if call.status == OK and call.wire_bytes > REPORT_THRESHOLD)
        line = (
            f"{label}: answers over {REPORT_THRESHOLD:,} bytes: {over} of {statuses[OK]}; refused: "
            f"{statuses[REFUSED]}; not asked: {statuses[NOT_ASKED]}"
        )
        lines.append(line + (f"; tool errors: {statuses[ERROR]}." if statuses[ERROR] else "."))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# A real model and its specimen, call by call
# ---------------------------------------------------------------------------

#: The reference calls whose answer follows the spelling of a name, never the passport: a specimen
#: spells its names by the generator's pattern, so they are recorded and never judged. A recipe is
#: judged by its requests (:func:`follows_text`), so a new recipe cannot slip past the rule.
NAME_DEPENDENT_CALLS: frozenset[str] = frozenset(
    {
        "find 4 names (exact, lower, prefix, misspelt)",
        "find 16 names (the 16 largest SWCs)",
        "find text (two words of the largest SWC's name)",
    }
)

#: The request arguments that ask by text.
TEXT_ARGUMENTS = frozenset({"text", "for_requirement"})

#: The fields whose content the passport does not record: a filter over one (``exists`` included)
#: counts what the generator happened to write there, not what the passport states.
TEXT_FIELDS = frozenset({"description", "title"})

#: A recipe call's label: the recipe's title, then ``<i> of <n>``.
_RECIPE_LABEL = re.compile(r"(?P<title>.+), \d+ of \d+")


def follows_text(request: object) -> bool:
    """Whether *request*, at any depth (a join's ``with`` included), asks by text or filters a text field.

    Not reached: a text field a future schema adds, until it is named in :data:`TEXT_FIELDS`.
    """
    if isinstance(request, dict):
        if TEXT_ARGUMENTS & set(request) or request.get("field") in TEXT_FIELDS:
            return True
        return any(follows_text(value) for value in request.values())
    if isinstance(request, list):
        return any(follows_text(value) for value in request)
    return False


@functools.cache
def text_dependent_recipes() -> frozenset[str]:
    """The titles of the recipes one of whose requests :func:`follows_text`; read once."""
    return frozenset(
        recipe.title for recipe in all_recipes() if any(follows_text(request) for request in recipe.requests)
    )


#: The specimen column of a call when no specimen was built.
NO_SPECIMEN = "not run: no specimen"


def name_dependent(label: str) -> bool:
    """Whether the call *label* is never judged, its answer following the names' spelling.

    It is one of :data:`NAME_DEPENDENT_CALLS`, or a call of a recipe in :func:`text_dependent_recipes`.
    """
    match = _RECIPE_LABEL.fullmatch(label)
    return label in NAME_DEPENDENT_CALLS or (match is not None and match["title"] in text_dependent_recipes())


def pair_agrees(real: Call, specimen: Call, *, tolerance: float) -> bool:
    """Whether a specimen's call agrees with the real model's: all three conditions at once.

    The outcomes are equal (``ok``, the same refusal code, or both ``not asked``); the figures
    summaries are equal (the row counts); and the specimen's bytes are within *tolerance*, a
    fraction, of the real call's. Seconds are never compared: one call is not a measurement.
    """
    same_outcome = real.status == specimen.status and real.code == specimen.code
    within = abs(specimen.wire_bytes - real.wire_bytes) <= tolerance * real.wire_bytes
    return same_outcome and real.figures == specimen.figures and within


@dataclass(frozen=True)
class Pair:
    """One call on the real model and on its specimen, and what the pair came to.

    ``agrees`` is ``None`` when the pair is not judged: a :func:`name_dependent` call, or no
    specimen (``specimen`` is then ``None``).
    """

    real: Call
    specimen: Call | None
    agrees: bool | None

    @property
    def label(self) -> str:
        return self.real.label

    @property
    def judged(self) -> bool:
        return self.agrees is not None

    def agreement(self) -> str:
        """The pair's agreement cell."""
        if self.specimen is None:
            return "not judged: no specimen"
        if self.agrees is None:
            return "recorded, not judged: follows the names' spelling"
        return "agrees" if self.agrees else "differs"


def paired_calls(
    real: FormsMeasurement, specimen: FormsMeasurement | None, *, tolerance: float
) -> tuple[list[Pair], list[Pair]]:
    """The recipe pairs and the reference pairs of *real* and *specimen*, in the real model's order.

    A label on one side alone is a defect of the chain and raises ``ValueError``.
    """

    def pairs(real_calls: tuple[Call, ...], specimen_calls: tuple[Call, ...] | None) -> list[Pair]:
        if specimen_calls is None:
            return [Pair(call, None, None) for call in real_calls]
        by_label = {call.label: call for call in specimen_calls}
        if [call.label for call in real_calls] != list(by_label):
            raise ValueError("the real model and its specimen were not asked the same calls")
        return [
            Pair(
                call,
                by_label[call.label],
                None if name_dependent(call.label) else pair_agrees(call, by_label[call.label], tolerance=tolerance),
            )
            for call in real_calls
        ]

    if specimen is None:
        return pairs(real.recipe_calls, None), pairs(real.reference_calls, None)
    return pairs(real.recipe_calls, specimen.recipe_calls), pairs(real.reference_calls, specimen.reference_calls)


def disagreeing(pairs: Sequence[Pair]) -> int:
    """How many judged pairs of *pairs* disagree."""
    return sum(1 for pair in pairs if pair.agrees is False)


def render_paired_record(real: FormsMeasurement, specimen: FormsMeasurement | None, *, tolerance: float) -> str:
    """The paired forms tables of one real model: every call's *real | specimen* pair and its agreement.

    Each side's cell is :func:`render_record`'s; the specimen's reads :data:`NO_SPECIMEN` when none was built.
    """
    recipes, references = paired_calls(real, specimen, tolerance=tolerance)
    lines: list[str] = []
    for heading, pairs in (("Recipe forms, paired", recipes), ("Reference calls, paired", references)):
        rows = [
            (
                pair.label,
                [
                    _call_cell(pair.real),
                    NO_SPECIMEN if pair.specimen is None else _call_cell(pair.specimen),
                    pair.agreement(),
                ],
            )
            for pair in pairs
        ]
        lines += [f"## {heading}", "", *_table("call", ["real", "specimen", "agreement"], rows), ""]
    judged = [pair for pair in (*recipes, *references) if pair.judged]
    if specimen is not None:
        lines.append(
            f"Pairs judged: {len(judged)}; disagreeing: {disagreeing(judged)}; recorded, not judged: "
            f"{len(recipes) + len(references) - len(judged)}. "
            f"The bytes agree within {tolerance:.0%} of the real call's."
        )
    return "\n".join(lines).rstrip("\n") + "\n"


def label_letters(index: int) -> str:
    """``0`` -> ``A``, ``25`` -> ``Z``, ``26`` -> ``AA``: a label never runs out."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def model_label(index: int) -> str:
    """``model A``, ``model B``, ... for the project at *index*."""
    return f"model {label_letters(index)}"


def main(argv: list[str]) -> int:
    # A redirected stream on Windows falls back to the locale codec, which cannot carry the em dash.
    force_utf8_streams()
    parser = argparse.ArgumentParser(prog="measure_forms.py", description=__doc__.splitlines()[0])
    parser.add_argument("projects", nargs="+", type=Path, metavar="PROJECT", help="a project's swcraft.yaml")
    parser.add_argument("--output", type=Path, help="write the record to this file instead of stdout")
    args = parser.parse_args(argv[1:])
    labels = [model_label(index) for index in range(len(args.projects))]
    measurements = []
    for label, manifest in zip(labels, args.projects, strict=True):
        try:
            measurements.append(measure(manifest.resolve()))
        except Exception as exc:  # noqa: BLE001 - reported by its class alone, which names nothing
            print(f"measure_forms.py: {label} could not be measured: {type(exc).__name__}", file=sys.stderr)
            return 1
    record = render_record(measurements, labels, date=datetime.date.today().isoformat())
    if args.output is None:
        sys.stdout.write(record)
        return 0
    try:
        args.output.write_text(record, encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"measure_forms.py: the record could not be written: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
