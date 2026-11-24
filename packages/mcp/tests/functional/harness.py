"""The functional scenario harness: the loader, the steps and the expectation evaluator.

A **scenario** is one JSON file under ``scenarios/``, validated by ``scenario.schema.json``, that
names its **specimen** (a key of the pin file). The harness:

1. **loads** it strictly (a repeated key, NaN or Infinity refused) and checks what a schema cannot:
   every threshold, target, step and variant named exists, the specimen is pinned and its ``because``
   paths are passport leaves, step ids are unique, a ``large-bench`` scenario is not ``fresh``, and a
   scenario on the shared project has no step that writes into it (:func:`load_scenario`). That check
   reads a classification (:data:`READ_ONLY_TOOLS`, :data:`READ_ONLY_COMMANDS`), not behaviour;
2. **runs** its steps against a copy of the specimen (:func:`run_steps`): an MCP call through the
   in-memory FastMCP client, a recipe by title through the tool it names, a ``swcraft`` command as
   ``sys.executable -m swcraft_tools.cli``, or an export fault (``export_faults.py``);
3. **evaluates** every expectation and reports **every** failure at once, each naming the scenario,
   the variant, the expectation and the threshold's ``because`` (:func:`evaluate`).

Targets are the locator's answers, read off the generated model, so a scenario never spells a
generated name. A **hook step** is a CLI step with a ``stdin`` object whose stdout must be one JSON
object (:func:`run_cli`). A **text answer** becomes a body through the step's ``text`` reading
(``impact-summary``, ``refusal``); a text of another form is a failed step. A **project file**
expectation reads the file after the variant's last step. A **continued page** passes the earlier
page's cursor as ``after``, refused unless that page is truncated with a non-empty ``next``.
**Wire bytes** are ``sum(escaped_bytes(t))`` over the client's text blocks, the server's budget unit.
"""

from __future__ import annotations

import functools
import json
import operator
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from conftest import REPO_ROOT, specimen_generator_module, specimen_passport
from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator
from passport_support import ABSENT, first_schema_error, leaf
from protocol_client import call
from swcraft_tools.editing.read import escaped_bytes

from tests.functional.export_faults import FaultError, repeated, write_faulted_export

HERE = Path(__file__).resolve().parent
SCENARIO_SCHEMA_PATH = HERE / "scenario.schema.json"
SCENARIO_DIR = HERE / "scenarios"

#: The variant name of a scenario that declares none.
DEFAULT_VARIANT = "default"

#: The tier of a small specimen, any but the large bench.
GENERATED_SMALL = "generated-small"

#: The tier whose model is the ``large-bench`` specimen -- the agent task benchmark's large set's --
#: shared and read only; the specimen of the same name is the one it may name.
LARGE_BENCH = "large-bench"

#: The ``swcraft`` commands a scenario on the shared project may run, each with the options that
#: write no file. Any other command or option (``--report``, say) needs ``"project": "fresh"``.
#: ``test_harness.py`` holds this to the CLI's parser, so a new command or option is a decision here.
READ_ONLY_COMMAND_FLAGS: Mapping[str, frozenset[str]] = {
    "check-model": frozenset({"--project", "--warnings-as-errors", "--format"}),
    "diff": frozenset({"--project", "--format"}),
    "impact": frozenset({"--project", "--depth", "--format"}),
    "list": frozenset({"--project", "--format"}),
    "metrics": frozenset({"--project", "--format", "--since", "--since-head"}),
    "query": frozenset(
        {
            "--project",
            "--format",
            "--dependents",
            "--deps",
            "--where",
            "--exists",
            "--members",
            "--form",
            "--limit",
            "--after",
            "--request",
            "--depth",
        }
    ),
    "resource-report": frozenset({"--project", "--format", "--fit-bytes", "--fit-utilization"}),
}
READ_ONLY_COMMANDS = frozenset(READ_ONLY_COMMAND_FLAGS)

#: Each read-only command's positional arguments by parser ``dest``; none names a file the command
#: writes, so no positional value is judged. Held equal to the CLI's parser.
READ_ONLY_COMMAND_POSITIONALS: Mapping[str, tuple[str, ...]] = {
    "check-model": (),
    "diff": ("rev_a", "rev_b"),
    "impact": ("name",),
    "list": ("layer",),
    "metrics": (),
    "query": ("layer",),
    "resource-report": (),
}

#: The MCP tools a scenario on the shared project may call (directly or through a recipe).
#: ``import_component`` only plans a draft change set.
READ_ONLY_TOOLS = frozenset(
    {
        "discover",
        "read",
        "find",
        "model_diff",
        "impact_of",
        "resource_report",
        "architecture_metrics",
        "import_component",
        "guide",
    }
)

#: The MCP tools that write into the project (``build`` and ``validate`` move the previous ARXML
#: aside), so need ``"project": "fresh"``. The two sets' union is held equal to the server's tools.
WRITING_TOOLS = frozenset({"change_set", "build", "validate", "export_component"})

#: How long one ``swcraft`` step may run before it fails, well under the tree's whole budget.
CLI_TIMEOUT_SECONDS = 60


class ScenarioError(ValueError):
    """A scenario file refused by the loader: the file, and why."""


class StepFailed(AssertionError):
    """A step that could not produce an answer (a recipe in neither the profile nor a How-to page, a tool error)."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _refuse_repeated_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    twice = repeated(key for key, _ in pairs)
    if twice:
        raise ScenarioError(f"a repeated key: {', '.join(twice)}")
    return dict(pairs)


def _refuse_constant(constant: str) -> Any:
    raise ScenarioError(f"{constant} is not a JSON number")


def strict_json(text: str) -> Any:
    """*text* parsed with a repeated key, NaN and Infinity refused."""
    return json.loads(text, object_pairs_hook=_refuse_repeated_keys, parse_constant=_refuse_constant)


@functools.cache
def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCENARIO_SCHEMA_PATH.read_text(encoding="utf-8")))


@functools.cache
def pinned_specimens() -> Mapping[str, Mapping[str, Any]]:
    """The pin file's ``specimens`` map: each specimen's digests and its optional ``because``."""
    return specimen_generator_module().load_pins(REPO_ROOT)["specimens"]


def specimen_because(name: str) -> Mapping[str, str]:
    """Why a leaf of the specimen *name*'s passport is what it is (the pin's ``because``), or nothing."""
    return pinned_specimens()[name].get("because", {})


@dataclass(frozen=True)
class Threshold:
    value: int
    because: str


@dataclass(frozen=True)
class Scenario:
    """A loaded scenario. ``document`` is the file as read; the rest are its parts."""

    id: str
    document: Mapping[str, Any]
    thresholds: Mapping[str, Threshold]
    targets: Mapping[str, Any]

    @property
    def tier(self) -> str:
        return self.document["tier"]

    @property
    def specimen(self) -> str:
        """The specimen the scenario runs on, a key of the pin file."""
        return self.document["specimen"]

    @property
    def because(self) -> Mapping[str, str]:
        """Why a leaf of the specimen's passport is what it is: the pin file's ``because``."""
        return specimen_because(self.specimen)

    @property
    def fresh(self) -> bool:
        return self.document.get("project", "shared") == "fresh"

    @property
    def guide(self) -> str:
        """The user guide installed for the ``guide`` tool: ``installed`` or ``absent`` (the default)."""
        return self.document.get("guide", "absent")

    def variant_names(self) -> tuple[str, ...]:
        return tuple(self.document.get("variants", {})) or (DEFAULT_VARIANT,)

    def steps(self, variant: str) -> list[dict[str, Any]]:
        return self._part(variant, "steps")

    def expectations(self, variant: str) -> list[dict[str, Any]]:
        return self._part(variant, "expectations")

    def _part(self, variant: str, key: str) -> list[dict[str, Any]]:
        override = self.document.get("variants", {}).get(variant, {})
        return override.get(key, self.document[key])


def _named_targets(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        if set(value) == {"$target"}:
            yield value["$target"]
        else:
            for item in value.values():
                yield from _named_targets(item)
    elif isinstance(value, list):
        for item in value:
            yield from _named_targets(item)


def _passport_leaf_exists(passport: Any, path: str) -> bool:
    value = leaf(passport, path)
    return value is not ABSENT and not isinstance(value, dict)


def _writing_option(argv: list[str]) -> str | None:
    """The first token of *argv* that makes it write into the project, or ``None``.

    A command outside :data:`READ_ONLY_COMMANDS`, or an option outside that command's reviewed
    flags (``--name=value`` judged by ``--name``). An argument that is not an option -- a positional
    (:data:`READ_ONLY_COMMAND_POSITIONALS`), an option's value -- is not judged.
    """
    command = argv[0]
    if command not in READ_ONLY_COMMAND_FLAGS:
        return command
    allowed = READ_ONLY_COMMAND_FLAGS[command]
    for token in argv[1:]:
        if token.startswith("-") and token.split("=", 1)[0] not in allowed:
            return token
    return None


def _recipe_tool(title: str, index: int) -> str | None:
    """The tool the recipe *title* calls, or ``None`` when there is no such recipe or request."""
    try:
        return _recipe(title, index).tool
    except StepFailed:
        return None


def _writes_into_the_project(step: Mapping[str, Any]) -> str | None:
    """What makes *step* write into the project -- a fault, a tool, a command or an option -- or ``None``."""
    if "export_faults" in step:
        return "an export fault"
    if "mcp" in step:
        return None if step["mcp"] in READ_ONLY_TOOLS else f"tool {step['mcp']}"
    if "recipe" in step:
        tool = _recipe_tool(step["recipe"], step["request"])
        return None if tool is None or tool in READ_ONLY_TOOLS else f"tool {tool} (its recipe's request)"
    if "cli" in step:
        token = _writing_option(step["cli"])
        return None if token is None else f"swcraft {token}"
    return None


#: The expectation entries that name a threshold, as ``(term, key)``: the term's value itself when
#: *key* is ``None``, else its value's *key*.
_THRESHOLD_REFERENCES: tuple[tuple[str, str | None], ...] = (
    ("rows_at_most", None),
    ("bytes_at_most", None),
    ("count_above", None),
    ("count_equals", "threshold"),
)

#: The expectation entries that name a target, as ``(term, key)``; ``findings_exactly`` names one per
#: pair and is checked beside them.
_TARGET_REFERENCES: tuple[tuple[str, str], ...] = (
    ("count_equals", "of"),
    ("report_merged_exactly", "interfaces"),
    ("values_equal", "target"),
    ("report_referrers_exactly", "ports_naming"),
    ("rows_among", "of"),
    ("report_names_exactly", "$target"),
)


def _require(kind: str, where: str, name: str, declared: Iterable[str]) -> None:
    """Refuse *name*, a *kind* (``target`` or ``threshold``) named at *where*, unless *declared*."""
    if name not in declared:
        under = "target" if kind == "target" else "thresholds"
        raise ScenarioError(f"{where}: {kind} {name!r} is not declared under {under!r}")


def _check_step(step: Mapping[str, Any], targets: set[str], *, shared: bool) -> None:
    named = [
        *_named_targets(step.get("arguments", {})),
        *_named_targets(step.get("export_faults", {})),
        *step.get("fill", {}).values(),
    ]
    for target in named:
        _require("target", f"step {step['id']}", target, targets)
    writer = _writes_into_the_project(step) if shared else None
    if writer is not None:
        raise ScenarioError(
            f"step {step['id']}: it writes into the project ({writer}), and the scenario's project is the "
            'shared one -- declare "project": "fresh"'
        )


def _check_expectation(
    expectation: Mapping[str, Any],
    variant: str,
    step_ids: Mapping[str, list[str]],
    document: Mapping[str, Any],
) -> None:
    ids = step_ids[variant]
    thresholds = set(document["thresholds"])
    targets = set(document.get("target", {}))
    if expectation["step"] not in ids:
        raise ScenarioError(f"variant {variant}: an expectation names step {expectation['step']!r}, not a step")
    for term, key in _TARGET_REFERENCES:
        if key in expectation.get(term, {}):
            _require("target", term, expectation[term][key], targets)
    for term, key in _THRESHOLD_REFERENCES:
        if term in expectation and (key is None or key in expectation[term]):
            _require("threshold", term, expectation[term] if key is None else expectation[term][key], thresholds)
    if "disjoint_from" in expectation and expectation["disjoint_from"] not in ids:
        raise ScenarioError(f"variant {variant}: disjoint_from names step {expectation['disjoint_from']!r}, not a step")
    source = expectation.get("count_equals", {})
    if "step" in source and source["step"] not in ids:
        raise ScenarioError(f"variant {variant}: count_equals names step {source['step']!r}, not a step")
    exactly = expectation.get("findings_exactly", {})
    for pair in exactly.get("pairs", []):
        _require("target", "findings_exactly", pair["ports_naming"], targets)
    if "minus_variant" in exactly:
        other = exactly["minus_variant"]
        if other not in document.get("variants", {}):
            raise ScenarioError(f"findings_exactly: minus_variant {other!r} is not a variant")
        if expectation["step"] not in step_ids[other]:
            raise ScenarioError(f"findings_exactly: minus_variant {other!r} has no step {expectation['step']!r}")


def _check_references(document: Mapping[str, Any]) -> None:
    if document["tier"] == LARGE_BENCH and document.get("project", "shared") == "fresh":
        raise ScenarioError(
            f'a {LARGE_BENCH} scenario reads the one shared large bench, so "project": "fresh" is refused'
        )
    name = document["specimen"]
    if name not in pinned_specimens():
        raise ScenarioError(f"specimen {name!r} is not a key of the pin file")
    passport = specimen_passport(name)
    for path in specimen_because(name):
        if not _passport_leaf_exists(passport, path):
            raise ScenarioError(f"specimen {name!r}: because {path!r} is not a leaf of its passport")
    targets = set(document.get("target", {}))
    shared = document.get("project", "shared") == "shared"
    parts = [(DEFAULT_VARIANT, document["steps"], document["expectations"])]
    for name, override in document.get("variants", {}).items():
        parts.append(
            (name, override.get("steps", document["steps"]), override.get("expectations", document["expectations"]))
        )
    step_ids = {variant: [step["id"] for step in steps] for variant, steps, _ in parts}
    for variant, steps, expectations in parts:
        twice = repeated(step_ids[variant])
        if twice:
            raise ScenarioError(f"variant {variant}: a step id twice: {', '.join(twice)}")
        for index, step in enumerate(steps):
            _check_step(step, targets, shared=shared)
            if "continues" in step and step["continues"] not in step_ids[variant][:index]:
                raise ScenarioError(
                    f"variant {variant}: step {step['id']} continues {step['continues']!r}, not an earlier step"
                )
        for expectation in expectations:
            _check_expectation(expectation, variant, step_ids, document)


def parse_scenario(text: str, *, source: str) -> Scenario:
    """The scenario in *text*, or :class:`ScenarioError` naming *source* and the reason."""
    try:
        document = strict_json(text)
    except ScenarioError as error:
        raise ScenarioError(f"{source}: {error}") from None
    except ValueError as error:
        raise ScenarioError(f"{source}: not JSON: {error}") from None
    error = first_schema_error(_validator(), document)
    if error is not None:
        where = "/".join(map(str, error.absolute_path)) or "(root)"
        raise ScenarioError(f"{source}: refused by the scenario schema at {where}: {error.message}")
    try:
        _check_references(document)
    except ScenarioError as error:
        raise ScenarioError(f"{source}: {error}") from None
    thresholds = {name: Threshold(entry["value"], entry["because"]) for name, entry in document["thresholds"].items()}
    return Scenario(id=document["id"], document=document, thresholds=thresholds, targets=document.get("target", {}))


def load_scenario(path: Path) -> Scenario:
    """The scenario file at *path*, strictly read and validated; its ``id`` must be its stem."""
    scenario = parse_scenario(path.read_text(encoding="utf-8"), source=path.name)
    if scenario.id != path.stem:
        raise ScenarioError(f"{path.name}: id {scenario.id!r} is not the file's stem")
    return scenario


def scenario_paths() -> list[Path]:
    """Every scenario file, in POSIX order."""
    return sorted(SCENARIO_DIR.glob("*.json"), key=lambda p: p.as_posix())


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


#: A target's answer: one name, several in order, or a whole number (an amount the fit questions
#: read off the model, passed to a step's argument as it is).
Answer = str | tuple[str, ...] | int


class TargetLocator(Protocol):
    """What the harness asks a locator: the target questions, by the scenario's name, and the figures."""

    def largest_component(self) -> str: ...

    def largest_components(self) -> tuple[str, ...]: ...

    def heaviest_interface(self) -> str: ...

    def heaviest_sender_receiver_interfaces(self) -> tuple[str, ...]: ...

    def most_unconnected_component(self) -> str: ...

    def port_count(self, component: str) -> int: ...

    def connector_count(self, component: str) -> int: ...

    def unconnected_ports(self, component: str) -> tuple[str, ...]: ...

    def unconnected_pairs(self) -> tuple[tuple[str, str], ...]: ...

    def ports_naming(self, interface: str) -> tuple[tuple[str, str], ...]: ...

    def most_read_sender_receiver_interface(self) -> str: ...

    def most_read_data_element(self) -> str: ...

    def commonest_level(self) -> str: ...

    def readers_count(self, interface: str) -> int: ...

    def declared_level_count(self, level: str) -> int: ...

    def sender_receiver_rows(self) -> int: ...

    def threshold_gap_memory(self) -> str: ...

    def threshold_gap_bytes(self) -> int: ...

    def over_size_bytes(self) -> int: ...

    def declared_resources(self) -> tuple[str, ...]: ...

    def model_components(self) -> int: ...

    def model_compositions(self) -> int: ...

    def model_component_ports(self) -> int: ...

    def model_connectors(self) -> int: ...

    def model_runnables(self) -> int: ...

    def model_dependencies(self) -> int: ...


#: The locator figures of the whole model, which name no target; each is the locator's method.
MODEL_FIGURES = (
    "sender_receiver_rows",
    "model_components",
    "model_compositions",
    "model_component_ports",
    "model_connectors",
    "model_runnables",
    "model_dependencies",
)


@dataclass(frozen=True)
class FunctionalProject:
    """A generated project: its directory, manifest, passport and locator (local, never written)."""

    root: Path
    manifest: Path
    passport: Mapping[str, Any]
    locator: TargetLocator


@dataclass
class StepResult:
    """One step's answer: the MCP body and text blocks, or a CLI run's exit code and output."""

    body: Any = None
    texts: tuple[str, ...] = ()
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    #: Why the step produced no answer (a :class:`StepFailed`); each of its expectations then fails.
    failure: str | None = None

    @property
    def wire_bytes(self) -> int:
        return sum(escaped_bytes(text) for text in self.texts)


@dataclass
class Run:
    """What a scenario variant's steps produced, and what evaluation needs beside it."""

    scenario: Scenario
    variant: str
    project: FunctionalProject
    targets: dict[str, Answer]
    results: dict[str, StepResult] = field(default_factory=dict)
    #: Runs another variant of the same scenario (``minus_variant``); set by the runner.
    other_variant: Callable[[str], Run] | None = None


def resolve_targets(scenario: Scenario, locator: TargetLocator) -> dict[str, Answer]:
    """Each target's answer from *locator*: a name, or a tuple of names.

    A target ``{"question", "first"}`` is the question's first *first* answers; a question answering
    fewer is a :class:`StepFailed`, never a shorter target, and so is a question the model has no
    answer to (the locator's :class:`LookupError`: no unconnected port, say).
    """
    answers: dict[str, Answer] = {}
    for name, question in scenario.targets.items():
        asked = question if isinstance(question, str) else question["question"]
        try:
            answer = getattr(locator, asked)()
        except LookupError as error:
            raise StepFailed(f"target {name}: {asked}: {error}") from None
        if isinstance(question, str):
            answers[name] = answer
            continue
        if len(answer) < question["first"]:
            raise StepFailed(f"target {name}: {question['question']} answers {len(answer)}, not {question['first']}")
        answers[name] = tuple(answer[: question["first"]])
    return answers


def _names(answer: Answer) -> tuple[str, ...]:
    """A target's answer as names: its one name, or its names in order; a number names nothing."""
    if isinstance(answer, int):
        raise StepFailed(f"the target answers the number {answer}, not names")
    return (answer,) if isinstance(answer, str) else tuple(answer)


def _substitute(value: Any, targets: Mapping[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$target"}:
            answer = targets[value["$target"]]
            return answer if isinstance(answer, str | int) else list(answer)
        return {key: _substitute(item, targets) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, targets) for item in value]
    return value


@functools.cache
def _recipes() -> dict[str, Any]:
    """The profile's and the How-to pages' recipes by title, read by the tools tree's one loader (once per session)."""
    import test_agent_recipes as recipes  # noqa: PLC0415 - the tools tree's parser, on the path in the tests

    return {recipe.title: recipe for recipe in recipes.all_recipes()}


def _recipe(title: str, index: int) -> Any:
    """The recipe *title*, which has a request *index*; else a :class:`StepFailed`."""
    found = _recipes()
    if title not in found:
        raise StepFailed(f"recipe in neither the profile nor a How-to page: {title!r}")
    recipe = found[title]
    if index >= len(recipe.requests):
        raise StepFailed(f"recipe {title!r} has {len(recipe.requests)} requests, no request {index}")
    return recipe


def recipe_request(
    title: str, index: int, fill: Mapping[str, str], drop: Iterable[str], targets: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    """``(tool, arguments)`` of request *index* of the recipe *title*, filled from *targets*.

    *index* ``-1`` is the recipe's last request: the page of a recipe that counts first, and its only
    request when it sends one.
    """
    import test_agent_recipes as recipes  # noqa: PLC0415 - the tools tree's parser, on the path in the tests

    recipe = _recipe(title, index)
    index = index % len(recipe.requests)
    placeholders = {placeholder: targets[name] for placeholder, name in fill.items()}

    def filled(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: filled(item) for key, item in value.items()}
        if isinstance(value, list):
            return [filled(item) for item in value]
        if isinstance(value, str) and recipes.PLACEHOLDER.fullmatch(value):
            if value not in placeholders:
                raise StepFailed(f"recipe {title!r}: placeholder {value} is not filled by the step")
            return placeholders[value]
        return value

    arguments = filled(recipe.requests[index])
    for key in drop:
        arguments.pop(key, None)
    return recipe.tool, arguments


_IMPACT_SUMMARY_HEADER = re.compile(r"^Impact of '.*' \(summary\):$")
_IMPACT_SUMMARY_GROUP = re.compile(r"^  (?P<kind>\S+) \((?P<count>\d+)\): (?P<names>.+)$")
_SCOPE_PAGE = re.compile(r"^page: (?P<returned>\d+) of (?P<total>\d+), (?P<state>truncated|complete)$")
_SCOPE_NEXT = re.compile(r'^next: after="(?P<after>.*)"$')


def _scope_page(footer: str) -> dict[str, Any] | None:
    """The page a scope footer (``impact.format_impact_scope``) states, in the ``read`` page's keys."""
    page: dict[str, Any] | None = None
    after = None
    for part in footer[1:-1].split("; "):
        if (match := _SCOPE_PAGE.match(part)) is not None:
            page = {
                "returned": int(match["returned"]),
                "total": int(match["total"]),
                "truncated": match["state"] == "truncated",
            }
        elif (match := _SCOPE_NEXT.match(part)) is not None:
            after = match["after"]
    if page is not None and after is not None:
        page["next"] = after
    return page


def impact_summary_document(texts: tuple[str, ...]) -> dict[str, Any]:
    """The body of an ``impact_of`` summary's text, for the counting terms to walk.

    ``impact.format_impact_summary``'s text with its optional scope footer (``format_impact_scope``):
    ``{"rows": [{"kind", "name"}, ...], "footer": "<the footer line>", "page": {...}}``, one row per
    identity the text lists; ``footer`` is ``""`` when the answer carries none. ``page`` is present
    when the footer states one: ``returned``, ``total`` and ``truncated`` as the ``read`` page names
    them, and ``next``, the cursor, when the footer carries one. A group line whose count is not the
    number of names it lists, a text that is not a summary (a refusal, the "not found" line) and any
    line the form does not have are a :class:`StepFailed`.
    """
    if not texts:
        raise StepFailed("the answer has no text block to read as an impact summary")
    lines = texts[0].split("\n")
    if not _IMPACT_SUMMARY_HEADER.match(lines[0]):
        raise StepFailed(f"the answer is not an impact summary: {texts[0][:200]!r}")
    rows: list[dict[str, str]] = []
    footer = ""
    for line in lines[1:]:
        if not line or line == "  (no dependents)" or (line.endswith(":") and not line.startswith(" ")):
            continue
        if line.startswith("(") and line.endswith(")"):
            footer = line
            continue
        group = _IMPACT_SUMMARY_GROUP.match(line)
        if group is None:
            raise StepFailed(f"an impact summary line of no known shape: {line[:200]!r}")
        names = group["names"].split(", ")
        if len(names) != int(group["count"]):
            raise StepFailed(f"an impact summary group of {group['count']} lists {len(names)} names: {line[:200]!r}")
        rows.extend({"kind": group["kind"], "name": name} for name in names)
    document: dict[str, Any] = {"rows": rows, "footer": footer}
    page = _scope_page(footer) if footer else None
    if page is not None:
        document["page"] = page
    return document


#: The prefix of a text refusal: a tool annotated ``-> dict | str`` answers ``"ERROR: <msg>"``.
_REFUSAL_PREFIX = "ERROR: "


def refusal_document(result: StepResult) -> dict[str, str]:
    """``{"refusal": <msg>}`` of a text refusal, for the body terms to judge.

    The answer must be one text block opening with ``ERROR: `` and no body: an answer with a body
    (the tool answered), several blocks or another text is a :class:`StepFailed`, never a refusal
    read into it. What it reads is the class alone; the message's wording is the tool's tests'.
    """
    if result.body is not None or len(result.texts) != 1 or not result.texts[0].startswith(_REFUSAL_PREFIX):
        shown = result.texts[0][:200] if result.texts else ""
        raise StepFailed(f"the answer is not one text refusal: {shown!r}")
    return {"refusal": result.texts[0][len(_REFUSAL_PREFIX) :]}


def call_tool(tool: str, arguments: Mapping[str, Any]) -> StepResult:
    """``tools/call`` *tool* through the in-memory client; a tool error is a :class:`StepFailed`."""
    try:
        structured, texts = call(tool, **arguments)
    except ToolError as error:
        raise StepFailed(f"{tool} answered a tool error: {error}") from None
    return StepResult(body=structured, texts=texts)


def _spell_stdin(value: Any, project: FunctionalProject) -> Any:
    """*value* with ``{project}`` in each string value replaced by the project's directory; keys as written."""
    if isinstance(value, dict):
        return {key: _spell_stdin(item, project) for key, item in value.items()}
    if isinstance(value, list):
        return [_spell_stdin(item, project) for item in value]
    if isinstance(value, str):
        return value.replace("{project}", project.root.as_posix())
    return value


def run_cli(
    project: FunctionalProject,
    argv: Iterable[str],
    stdin: Mapping[str, Any] | None = None,
    *,
    json_stdout: bool = False,
) -> StepResult:
    """``swcraft`` *argv* from this interpreter, in the project's directory, captured.

    With *stdin* (a hook step) the object, ``{project}`` spelled out, is written as JSON to standard
    input, and stdout must be one JSON object, which becomes ``body``; otherwise a
    :class:`StepFailed`. *json_stdout* asks for the same reading without *stdin*, whatever the exit
    code (``check-model`` prints its document and exits 1 on an error). A run longer than
    :data:`CLI_TIMEOUT_SECONDS` is a :class:`StepFailed`.
    """
    argv = list(argv)
    spelled = [
        arg.replace("{manifest}", project.manifest.as_posix()).replace("{project}", project.root.as_posix())
        for arg in argv
    ]
    given = {} if stdin is None else {"input": json.dumps(_spell_stdin(stdin, project), ensure_ascii=False)}
    try:
        completed = subprocess.run(  # noqa: S603 - this interpreter, a fixed module, the scenario's argv
            [sys.executable, "-m", "swcraft_tools.cli", *spelled],
            cwd=project.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=CLI_TIMEOUT_SECONDS,
            **given,
        )
    except subprocess.TimeoutExpired:
        raise StepFailed(f"swcraft {' '.join(argv)} ran longer than {CLI_TIMEOUT_SECONDS} s") from None
    result = StepResult(exit_code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
    if stdin is not None or json_stdout:
        result.body = _stdout_object(argv, completed.stdout)
    return result


def _stdout_object(argv: list[str], stdout: str) -> dict[str, Any]:
    """A hook step's stdout as the one JSON object it must be; anything else is a :class:`StepFailed`."""
    try:
        document = strict_json(stdout)
    except ValueError:
        document = None
    if not isinstance(document, dict):
        raise StepFailed(f"swcraft {' '.join(argv)} answered no JSON object on stdout: {stdout[:200]!r}")
    return document


def run_steps(run: Run, *, tool_caller: Callable[[str, Mapping[str, Any]], StepResult] = call_tool) -> Run:
    """Every step of *run*'s variant, in order, its answer recorded under its id.

    A step that fails (:class:`StepFailed`: a recipe in neither the profile nor a How-to page, a tool
    error, a text of no known form) is recorded with its failure and the next step still runs, so :func:`evaluate`
    reports every expectation that failed, not the first failed step alone.
    """
    for step in run.scenario.steps(run.variant):
        try:
            result = _run_step(run, step, tool_caller)
        except StepFailed as error:
            result = StepResult(failure=str(error))
        run.results[step["id"]] = result
    return run


def page_cursor(run: Run, step_id: str) -> Any:
    """The cursor of step *step_id*'s page, for a step that ``continues`` it; else a :class:`StepFailed`.

    The step must have answered a page (``page``, as ``read`` and :func:`impact_summary_document`
    spell it) that says it is **truncated** and carries a ``next`` that is a non-empty string or a
    non-empty mapping (``read``'s ``after`` takes either). A page that is complete, or truncated with
    no usable cursor, is refused here, so a continuation can never pass by asking nothing.
    """
    earlier = run.results.get(step_id)
    if earlier is None:
        raise StepFailed(f"step {step_id!r} has not run, so there is no page to continue")
    if earlier.failure is not None:
        raise StepFailed(f"step {step_id!r} failed, so there is no page to continue: {earlier.failure}")
    page = earlier.body.get("page") if isinstance(earlier.body, dict) else None
    if not isinstance(page, dict):
        raise StepFailed(f"step {step_id!r} answered no page to continue")
    if page.get("truncated") is not True:
        raise StepFailed(f"step {step_id!r}'s page does not say it is truncated: {page!r}")
    cursor = page.get("next")
    if not isinstance(cursor, str | dict) or not cursor:
        raise StepFailed(f"step {step_id!r}'s truncated page carries no usable cursor: next = {cursor!r}")
    return cursor


def _continued(run: Run, step: Mapping[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """*arguments* with ``after`` set to the cursor of the page *step* ``continues``, when it names one."""
    if "continues" in step:
        arguments["after"] = page_cursor(run, step["continues"])
    return arguments


def _run_step(
    run: Run, step: Mapping[str, Any], tool_caller: Callable[[str, Mapping[str, Any]], StepResult]
) -> StepResult:
    if "mcp" in step:
        result = tool_caller(step["mcp"], _continued(run, step, _substitute(step["arguments"], run.targets)))
        if step.get("text") == "impact-summary":
            result.body = impact_summary_document(result.texts)
        elif step.get("text") == "refusal":
            result.body = refusal_document(result)
        return result
    if "recipe" in step:
        tool, arguments = recipe_request(
            step["recipe"], step["request"], step.get("fill", {}), step.get("drop", ()), run.targets
        )
        return tool_caller(tool, _continued(run, step, arguments))
    if "export_faults" in step:
        return _export_faults(run, step["export_faults"])
    return run_cli(run.project, step["cli"], step.get("stdin"), json_stdout=step.get("stdout") == "json")


def _export_faults(run: Run, fault: Mapping[str, Any]) -> StepResult:
    """The project's export with :mod:`export_faults` applied to the target's interfaces."""
    names = list(_names(run.targets[fault["interfaces"]["$target"]]))
    try:
        write_faulted_export(
            run.project.root / fault["source"], run.project.root / fault["output"], names, fault["kind"]
        )
    except FaultError as error:
        raise StepFailed(f"export fault: {error}") from None
    except OSError as error:
        raise StepFailed(f"export fault: the export cannot be read or written: {error.strerror}") from None
    return StepResult(body={"interfaces": names, "kind": fault["kind"]})


# ---------------------------------------------------------------------------
# Evaluating
# ---------------------------------------------------------------------------


def select(document: Any, path: str) -> list[Any]:
    """The values at the dotted *path*; ``*`` walks every item of a list or value of a mapping.

    A number takes a list's item at that index (``candidates.0``, a name's first candidate); a node
    with no such item -- a shorter list, or a string such as ``find``'s ``"none"`` -- adds nothing.
    """
    nodes = [document]
    for key in path.split("."):
        found: list[Any] = []
        for node in nodes:
            if key == "*":
                found.extend(node.values() if isinstance(node, dict) else node if isinstance(node, list) else [])
            elif isinstance(node, dict) and key in node:
                found.append(node[key])
            elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
                found.append(node[int(key)])
        if not found:
            raise StepFailed(f"the answer has nothing at {path!r} (stopped at {key!r})")
        nodes = found
    return nodes


def count_at(document: Any, path: str) -> int:
    """The count at *path*: a number as is, a list by its items; several values are summed."""
    total = 0
    for value in select(document, path):
        if isinstance(value, bool) or not isinstance(value, int | list):
            raise StepFailed(f"the value at {path!r} is neither a count nor a list: {type(value).__name__}")
        total += value if isinstance(value, int) else len(value)
    return total


def _locator_figure(run: Run, source: Mapping[str, str]) -> int:
    """The locator figure *source* names; every figure has its branch, an unknown one is refused.

    The :data:`MODEL_FIGURES` are the model's and name no target (one that refuses the model -- a
    connector with an end on a composition -- is a failed step naming why); every other figure is
    of the one element the target ``of`` names.
    """
    locator = run.project.locator
    if source["locator"] in MODEL_FIGURES:
        try:
            return getattr(locator, source["locator"])()
        except ValueError as error:
            raise StepFailed(f"locator {source['locator']} of the model: {error}") from None
    name = run.targets[source["of"]]
    if not isinstance(name, str):
        raise StepFailed(
            f"locator {source['locator']} of {source['of']}: the target names {len(_names(name))} elements, not one"
        )
    if source["locator"] == "port_count":
        try:
            return locator.port_count(name)
        except KeyError:
            raise StepFailed(f"locator port_count of {source['of']}: the model has no component {name!r}") from None
    if source["locator"] == "unconnected_count":
        return len(locator.unconnected_ports(name))
    if source["locator"] == "connector_count":
        return locator.connector_count(name)
    if source["locator"] == "components_naming_count":
        return len({component for component, _ in locator.ports_naming(name)})
    if source["locator"] == "ports_naming_count":
        return len(locator.ports_naming(name))
    if source["locator"] == "readers_count":
        try:
            return locator.readers_count(name)
        except KeyError:
            raise StepFailed(
                f"locator readers_count of {source['of']}: the model has no sender-receiver interface {name!r}"
            ) from None
    if source["locator"] == "declared_level_count":
        try:
            return locator.declared_level_count(name)
        except KeyError:
            raise StepFailed(f"locator declared_level_count of {source['of']}: {name!r} is no level") from None
    raise StepFailed(f"locator figure {source['locator']!r} has no branch in the harness")


def _passport_leaf(passport: Mapping[str, Any], path: str) -> int:
    values = select(passport, path)
    if len(values) != 1:
        raise StepFailed(f"the passport has {len(values)} values at {path!r}, not one")
    if isinstance(values[0], bool) or not isinstance(values[0], int):
        raise StepFailed(f"the passport's value at {path!r} is not a count: {type(values[0]).__name__}")
    return values[0]


def _expected_count(run: Run, source: Mapping[str, str]) -> tuple[int, str]:
    """The figure a ``count_equals`` names, and how a failure spells its source."""
    if "passport" in source:
        return _passport_leaf(run.project.passport, source["passport"]), f"passport {source['passport']}"
    if "threshold" in source:
        return _threshold(run, source["threshold"]).value, source["threshold"]
    if "step" in source:
        other = run.results[source["step"]]
        if other.failure is not None:
            raise StepFailed(f"step {source['step']!r} failed: {other.failure}")
        return count_at(other.body, source["at"]), f"step {source['step']} at {source['at']!r}"
    of = f" of {source['of']}" if "of" in source else " of the model"
    return _locator_figure(run, source), f"locator {source['locator']}{of}"


def _member_spelling(identity: Any) -> str:
    """A subject step's member identity as one string: a name as is, anything else its canonical JSON."""
    if isinstance(identity, str):
        return identity
    return json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def finding_pairs(result: StepResult) -> set[tuple[str, str]]:
    """``(rule, element)`` of every finding of a ``check-model --format json`` run; ``Component/member``.

    A layer-wide subject is named ``layer <layer>`` and a member identity that is not a plain name is
    spelled as its canonical JSON (``Task/{"runnable":"R","swc":"S"}``), so neither can equal an
    expected ``Component/port``: each is reported as an extra pair, never dropped.
    """
    try:
        document = json.loads(result.stdout)
    except ValueError:
        raise StepFailed(f"the step's output is not check-model's JSON document: {result.stdout[:200]!r}") from None
    if not isinstance(document, dict) or not isinstance(document.get("findings"), list):
        raise StepFailed(f"the step's output has no findings list: {result.stdout[:200]!r}")
    pairs = set()
    for finding in document["findings"]:
        for subject in finding.get("subjects", []):
            if subject.get("element") is None:
                pairs.add((finding["rule"], f"layer {subject.get('layer')}"))
                continue
            members = [_member_spelling(part["member"]) for part in subject.get("path", []) if "member" in part]
            element = "/".join([subject["element"], *members[:1]])
            pairs.add((finding["rule"], element))
    return pairs


def _report_section(stdout: str, heading: str, *, markdown: bool = False) -> list[str] | None:
    """A report's lines under its heading; ``None`` when it has no such heading.

    The import report's section is the lines under the first line starting *heading*, up to the
    next blank line. A Markdown document's (*markdown*: the build report) is the lines under the
    first line that **is** *heading* (``## Resources``), blank lines kept, up to the next heading or
    thematic break (a line starting ``#`` or ``---``): its first paragraph follows a blank line, so
    the first-blank-line rule would find it empty.
    """
    lines = stdout.split("\n")
    if markdown:
        start = next((i for i, line in enumerate(lines) if line.rstrip("\r") == heading), None)
    else:
        start = next((i for i, line in enumerate(lines) if line.startswith(heading)), None)
    if start is None:
        return None
    section: list[str] = []
    for line in lines[start + 1 :]:
        if markdown and line.startswith(("#", "---")):
            break
        if not markdown and not line.strip():
            break
        section.append(line.rstrip("\r") if markdown else line)
    return section


_REPORT_UNRESOLVED = "Unresolved references ("
_REPORT_REFERRER = re.compile(r"^ {4,}(?P<path>/\S+) \(")


def report_referrers(result: StepResult) -> set[str]:
    """``Component/port`` of every referring element the import report's unresolved references list.

    A referrer is named by its path's last two segments; a report without the heading answers the
    empty set. A report that counts referrers it does not list (no ``--verbose``) is a
    :class:`StepFailed`, never read as a shorter list.
    """
    referrers = set()
    for line in _report_section(result.stdout, _REPORT_UNRESOLVED) or []:
        if line.strip().startswith("… and "):
            raise StepFailed(f"the import report counts referrers it does not list ({line.strip()}): run it --verbose")
        match = _REPORT_REFERRER.match(line)
        if match is not None:
            referrers.add("/".join(match["path"].split("/")[-2:]))
    return referrers


_REPORT_MERGED = "Merged declarations ("
_REPORT_MERGED_GROUP = re.compile(r"^  - (?P<identity>\S+) declared at ")


def report_merged(result: StepResult) -> set[str]:
    """The name of every group the import report lists under its *Merged declarations* heading.

    Each group line, up to the next blank line, starts ``  - <layer>/<kind>/<name> declared at``; a
    group is named by its identity's last segment. A report without the heading answers the empty set.
    """
    merged = set()
    for line in _report_section(result.stdout, _REPORT_MERGED) or []:
        match = _REPORT_MERGED_GROUP.match(line)
        if match is not None:
            merged.add(match["identity"].rsplit("/", 1)[-1])
    return merged


def _ports_naming(run: Run, target: str) -> set[str]:
    return {
        f"{component}/{port}"
        for interface in _names(run.targets[target])
        for component, port in run.project.locator.ports_naming(interface)
    }


def _expected_pairs(run: Run, pairs: Iterable[Mapping[str, str]]) -> set[tuple[str, str]]:
    return {(pair["rule"], element) for pair in pairs for element in _ports_naming(run, pair["ports_naming"])}


def _threshold(run: Run, name: str) -> Threshold:
    return run.scenario.thresholds[name]


def _count_bound(
    run: Run,
    expectation: Mapping[str, Any],
    result: StepResult,
    term: str,
    relation: str,
    held: Callable[[int, int], bool],
) -> str | None:
    threshold = _threshold(run, expectation[term])
    count = count_at(result.body, expectation["at"])
    if held(count, threshold.value):
        return None
    return (
        f"{count} at {expectation['at']!r}, expected {relation} {expectation[term]} = {threshold.value} "
        f"(because: {threshold.because})"
    )


def _check_rows_at_most(run: Run, expectation: Mapping[str, Any], result: StepResult) -> str | None:
    return _count_bound(run, expectation, result, "rows_at_most", "<=", operator.le)


def _check_count_above(run: Run, expectation: Mapping[str, Any], result: StepResult) -> str | None:
    return _count_bound(run, expectation, result, "count_above", ">", operator.gt)


def _check_bytes_at_most(run: Run, expectation: Mapping[str, Any], result: StepResult) -> str | None:
    threshold = _threshold(run, expectation["bytes_at_most"])
    if result.wire_bytes <= threshold.value:
        return None
    return (
        f"{result.wire_bytes} wire bytes, expected <= {expectation['bytes_at_most']} = {threshold.value} "
        f"(because: {threshold.because})"
    )


def _check_count_equals(run: Run, expectation: Mapping[str, Any], result: StepResult) -> str | None:
    source = expectation["count_equals"]
    expected, named = _expected_count(run, source)
    count = count_at(result.body, expectation["at"])
    if count == expected:
        return None
    because = f" (because: {_threshold(run, source['threshold']).because})" if "threshold" in source else ""
    return f"{count} at {expectation['at']!r}, expected {named} = {expected}{because}"


def _check_values_equal(run: Run, expectation: Mapping[str, Any], result: StepResult) -> str | None:
    name = expectation["values_equal"]["target"]
    expected = list(_names(run.targets[name]))
    actual = select(result.body, expectation["at"])
    if actual == expected:
        return None
    return f"{actual} at {expectation['at']!r}, expected target {name} = {expected}"


def _check_literal_equals(run: Run, expectation: Mapping[str, Any], result: StepResult) -> str | None:
    """Every value at the path is the literal, of its type: ``true`` is not ``1``, ``"1"`` is not ``1``."""
    literal = expectation["literal_equals"]
    actual = select(result.body, expectation["at"])
    if all(type(value) is type(literal) and value == literal for value in actual):
        return None
    return (
        f"{json.dumps(actual)} at {expectation['at']!r}, expected {json.dumps(literal)} "
        f"(because: {expectation['because']})"
    )


def _check_present(run: Run, expectation: Mapping[str, Any], result: StepResult) -> str | None:
    if all(value is None for value in select(result.body, expectation["at"])):
        return f"null at {expectation['at']!r}, expected a value"
    return None


def _check_absent(run: Run, expectation: Mapping[str, Any], result: StepResult) -> str | None:
    """The path selects nothing in the step's body -- the negative of ``present``.

    A step with no body to look in (a CLI step without ``stdin``) fails: over no answer the term
    would hold of any.
    """
    if result.body is None:
        return f"the step answered no body to look in for {expectation['at']!r}"
    try:
        found = select(result.body, expectation["at"])
    except StepFailed:
        return None
    return f"{found[0]!r} at {expectation['at']!r}, expected nothing there"


def _items(document: Any, path: str) -> list[Any]:
    """The values at *path*, a list among them read as its items."""
    items: list[Any] = []
    for value in select(document, path):
        items.extend(value if isinstance(value, list) else [value])
    return items


def _check_disjoint_from(run: Run, expectation: Mapping[str, Any], result: StepResult) -> str | None:
    other_id = expectation["disjoint_from"]
    other = run.results[other_id]
    if other.failure is not None:
        raise StepFailed(f"step {other_id!r} failed: {other.failure}")
    at = expectation["at"]
    rows = _items(result.body, at)
    if not rows:
        return f"no rows at {at!r}, expected rows past step {other_id}'s"
    earlier = {json.dumps(row, sort_keys=True) for row in _items(other.body, at)}
    again = [row for row in rows if json.dumps(row, sort_keys=True) in earlier]
    if not again:
        return None
    return f"{len(again)} of {len(rows)} rows at {at!r} are step {other_id}'s rows again, first {again[0]!r}"


def _row_spelling(row: Any) -> str | None:
    """A member row as ``Component/member``; ``None`` for a row that names no element and member."""
    if isinstance(row, dict) and isinstance(row.get("element"), str) and isinstance(row.get("member"), str):
        return f"{row['element']}/{row['member']}"
    return None


def _check_rows_among(run: Run, expectation: Mapping[str, Any], result: StepResult) -> str | None:
    """The rows at the path are not empty, each is a distinct member row, and the locator's answer holds it.

    The answer is the unconnected ports of the component ``of`` names, or of the whole model. An
    empty page fails: over no rows the term would hold of any answer.
    """
    source = expectation["rows_among"]
    locator = run.project.locator
    if "of" in source:
        name = run.targets[source["of"]]
        if not isinstance(name, str):
            raise StepFailed(f"rows_among of {source['of']}: the target names {len(_names(name))} elements, not one")
        expected = {f"{name}/{port}" for port in locator.unconnected_ports(name)}
        named = f"locator {source['locator']} of {source['of']}"
    else:
        expected = {f"{component}/{port}" for component, port in locator.unconnected_pairs()}
        named = f"locator {source['locator']} of the model"
    at = expectation["at"]
    rows = _items(result.body, at)
    if not rows:
        return f"no rows at {at!r}, expected rows among {named}"
    spelled = [_row_spelling(row) for row in rows]
    if None in spelled:
        first = rows[spelled.index(None)]
        return f"a row at {at!r} names no element and member: {first!r}"
    repeated = [row for row, times in Counter(spelled).items() if times > 1]
    if repeated:
        return f"{len(repeated)} row(s) at {at!r} appear more than once, first {repeated[0]!r}"
    foreign = [row for row in spelled if row not in expected]
    if foreign:
        return f"{len(foreign)} of {len(rows)} rows at {at!r} are not among {named}, first {foreign[0]!r}"
    return None


def _check_exit_code(run: Run, expectation: Mapping[str, Any], result: StepResult) -> str | None:
    if result.exit_code == expectation["exit_code"]:
        return None
    return f"exit code {result.exit_code}, expected {expectation['exit_code']}; stderr: {result.stderr[-500:]!r}"


def _check_findings_exactly(run: Run, expectation: Mapping[str, Any], result: StepResult) -> str | None:
    exactly = expectation["findings_exactly"]
    actual = finding_pairs(result)
    if "minus_variant" in exactly:
        if run.other_variant is None:
            raise StepFailed("findings_exactly names minus_variant, and the runner gave no way to run it")
        other = exactly["minus_variant"]
        other_result = run.other_variant(other).results[expectation["step"]]
        if other_result.failure is not None:
            raise StepFailed(f"variant {other}'s step {expectation['step']!r} failed: {other_result.failure}")
        actual -= finding_pairs(other_result)
    expected = _expected_pairs(run, exactly["pairs"])
    if actual == expected:
        return None
    return (
        f"findings {sorted(actual)}, expected exactly {sorted(expected)} "
        f"(missing {sorted(expected - actual)}, extra {sorted(actual - expected)})"
    )


def _check_report_referrers_exactly(run: Run, expectation: Mapping[str, Any], result: StepResult) -> str | None:
    target = expectation["report_referrers_exactly"]["ports_naming"]
    referrers = report_referrers(result)
    expected = _ports_naming(run, target)
    if referrers == expected:
        return None
    return (
        f"the import report's unresolved references name {sorted(referrers)}, expected exactly the ports "
        f"naming target {target} (missing {sorted(expected - referrers)}, extra {sorted(referrers - expected)})"
    )


def _check_report_merged_exactly(run: Run, expectation: Mapping[str, Any], result: StepResult) -> str | None:
    target = expectation["report_merged_exactly"]["interfaces"]
    merged = report_merged(result)
    expected = set(_names(run.targets[target]))
    if merged == expected:
        return None
    return (
        f"the import report's merged declarations name {sorted(merged)}, expected exactly target {target}'s "
        f"interfaces (missing {sorted(expected - merged)}, extra {sorted(merged - expected)})"
    )


def _markdown_section(run: Run, expectation: Mapping[str, Any]) -> tuple[list[str] | None, str]:
    """The section ``## <heading>`` of the project file the expectation names, and how a failure spells it.

    The file is read (UTF-8) when the expectation is evaluated, after the variant's last step; one
    that is absent or unreadable is a :class:`StepFailed` naming it -- no step wrote it.
    """
    relative = expectation["file"]
    heading = f"## {expectation['heading']}"
    try:
        text = (run.project.root / relative).read_text(encoding="utf-8")
    except OSError as error:
        raise StepFailed(f"{relative} cannot be read: {error.strerror or type(error).__name__}") from None
    return _report_section(text, heading, markdown=True), f"{relative} under {heading!r}"


def _check_report_first_line(run: Run, expectation: Mapping[str, Any], result: StepResult) -> str | None:
    """The first non-empty line under the heading is the string literal."""
    section, where = _markdown_section(run, expectation)
    if section is None:
        return f"{where}: no such heading"
    first = next((line for line in section if line.strip()), None)
    if first == expectation["report_first_line"]:
        return None
    return (
        f"{where}: the first line is {first!r}, expected {expectation['report_first_line']!r} "
        f"(because: {expectation['because']})"
    )


_TABLE_CELL_BREAK = re.compile(r"(?<!\\)\|")


_LEADING_FENCE = re.compile(r"`+")

#: One delimiter cell of a GitHub Flavored Markdown table: dashes, a colon at either end.
_DELIMITER_CELL = re.compile(r":?-+:?")


def _code_span_text(cell: str) -> str | None:
    """The text of the code span *cell* is, else ``None``.

    ``markdown_cells.code_span``'s fences: a single backtick around a name with none, and for a name
    holding a run of backticks a fence one longer than its longest run, padded by one space each side.
    """
    match = _LEADING_FENCE.match(cell)
    if match is None:
        return None
    fence = match.group()
    if len(fence) == 1:
        if len(cell) < 2 or not cell.endswith("`") or cell.endswith("``"):
            return None
        inner = cell[1:-1]
    else:
        opening, closing = f"{fence} ", f" {fence}"
        if len(cell) < 2 * len(opening) or not (cell.startswith(opening) and cell.endswith(closing)):
            return None
        if cell[-len(closing) - 1] == "`":
            return None
        inner = cell[len(opening) : -len(closing)]
    return inner.replace("\\|", "|")


def _is_delimiter_row(row: str) -> bool:
    """Whether *row* is a table's delimiter row: every cell between its outer pipes a delimiter cell."""
    cells = _TABLE_CELL_BREAK.split(row.strip())
    if len(cells) < 3 or cells[0] or cells[-1]:
        return False
    return all(_DELIMITER_CELL.fullmatch(cell.strip()) for cell in cells[1:-1])


def _table_names(section: list[str], where: str) -> list[str] | None:
    """The code-span texts of the first column of the section's first table; ``None`` when it has none.

    A table with no header and delimiter row, or a first cell that is no code span, is a
    :class:`StepFailed` naming *where*.
    """
    start = next((i for i, line in enumerate(section) if line.startswith("|")), None)
    if start is None:
        return None
    table = []
    for line in section[start:]:
        if not line.startswith("|"):
            break
        table.append(line)
    if len(table) < 2 or not _is_delimiter_row(table[1]):
        raise StepFailed(f"{where}: a table with no header and delimiter row")
    names = []
    for row in table[2:]:
        cell = _TABLE_CELL_BREAK.split(row)[1].strip()
        name = _code_span_text(cell)
        if name is None:
            raise StepFailed(f"{where}: a first cell that is no code span: {cell!r}")
        names.append(name)
    return names


def _check_report_names_exactly(run: Run, expectation: Mapping[str, Any], result: StepResult) -> str | None:
    """The first column of the table under the heading names exactly the target's names, each once.

    A target with no name holds over a section with no table (the build report's Resources section
    is one sentence when the model declares no core and no memory); a name listed twice fails.
    """
    section, where = _markdown_section(run, expectation)
    if section is None:
        return f"{where}: no such heading"
    target = expectation["report_names_exactly"]["$target"]
    expected = set(_names(run.targets[target]))
    names = _table_names(section, where)
    if names is None:
        return None if not expected else f"{where}: no table"
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        return f"{where}: the table names {repeated} more than once (because: {expectation['because']})"
    if set(names) == expected:
        return None
    return (
        f"{where}: the table names {sorted(names)}, expected exactly target {target}'s {sorted(expected)} "
        f"(missing {sorted(expected - set(names))}, extra {sorted(set(names) - expected)}; "
        f"because: {expectation['because']})"
    )


#: The closed expectation vocabulary: each term is the key that names it in an expectation, mapped
#: to its check -- ``None`` when the expectation holds on the step's answer, else what failed.
CHECKS: Mapping[str, Callable[[Run, Mapping[str, Any], StepResult], str | None]] = {
    "rows_at_most": _check_rows_at_most,
    "bytes_at_most": _check_bytes_at_most,
    "count_equals": _check_count_equals,
    "count_above": _check_count_above,
    "values_equal": _check_values_equal,
    "literal_equals": _check_literal_equals,
    "present": _check_present,
    "absent": _check_absent,
    "disjoint_from": _check_disjoint_from,
    "rows_among": _check_rows_among,
    "exit_code": _check_exit_code,
    "findings_exactly": _check_findings_exactly,
    "report_referrers_exactly": _check_report_referrers_exactly,
    "report_merged_exactly": _check_report_merged_exactly,
    "report_first_line": _check_report_first_line,
    "report_names_exactly": _check_report_names_exactly,
}
TERMS = tuple(CHECKS)


def _check(run: Run, expectation: Mapping[str, Any]) -> str | None:
    """``None`` when *expectation* holds, else what failed (without the scenario's name)."""
    result = run.results[expectation["step"]]
    if result.failure is not None:
        return f"step {expectation['step']!r} failed: {result.failure}"
    term = next(term for term in TERMS if term in expectation)
    return CHECKS[term](run, expectation, result)


def evaluate(run: Run) -> None:
    """Every expectation of *run*'s variant; one :class:`AssertionError` listing every failure."""
    failures = []
    for expectation in run.scenario.expectations(run.variant):
        try:
            problem = _check(run, expectation)
        except StepFailed as error:
            problem = str(error)
        if problem is not None:
            failures.append(f"  {json.dumps(expectation, sort_keys=True)}: {problem}")
    if failures:
        raise AssertionError(
            f"scenario {run.scenario.id} [{run.variant}]: {len(failures)} expectation(s) failed:\n"
            + "\n".join(failures)
        )
