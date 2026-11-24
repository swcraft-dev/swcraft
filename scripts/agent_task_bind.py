"""Bind the agent task benchmark's any-model set to one model: a target and an expected answer per task.

The **any-model set** is ``packages/tools/tests/fixtures/agent-bench/templates/NN-*.json``: one
*template* per task, its prompt carrying ``{{name}}`` placeholders, and a *binding* -- a
deterministic pass over the model under test that chooses the targets and computes the expected
answer from the loaded model. The template format, the tie-breaking and the orders are that folder's
``README.md`` (*The any-model templates*).

A binding is ``bind(model) -> Rendered | NotApplicable`` over one :class:`BoundModel`, built once
per run; it never goes through the MCP server under test. :func:`render_task` turns a template and
its :class:`Rendered` into a task file in the committed format plus ``"set": "any"`` and
``"oracle"``, which ``scripts/agent_task_check.py --task-file`` reads. A template with no target
answers one code of :data:`NOT_APPLICABLE`, never a guess: a question whose right answer would be
empty is not applicable. A list longer than :data:`LONG_LIST_HEAD` opts into the checker's
long-list rule in row order.

A change template's :class:`Rendered` carries its **right run** -- the edit a correct agent makes
-- and is offered only when that right run passes the checker on a scratch copy (:func:`offer`).
New names take the first free numeric suffix, compared ignoring case.

No ``main``: the runner's ``--render-only`` checks a binding. Nothing here prints, and a model name
leaves this module only inside a rendered task. Not reached: whether a prompt reads as an architect
would type it.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import posixpath
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fractions import Fraction
from functools import cached_property
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml
from pydantic import BaseModel
from swcraft_tools.component_exchange import import_component
from swcraft_tools.model_registry import ModelRegistry, current_registry
from swcraft_tools.model_state import ModelState, free_storage_path, load_model_state, stored_element_files
from swcraft_tools.passport import Locator, compute_passport
from swcraft_tools.project import require_project
from swcraft_tools.query import dependents, ordered_elements
from swcraft_tools.resource_analysis import (
    STATUS_UNKNOWN,
    ResourceUsage,
    budget_status,
    fits,
    format_bytes,
    reachable_memories,
)
from swcraft_tools.resource_report import resource_report
from swcraft_tools.safety import ASIL_LEVELS, asil_rank, parse_asil
from swcraft_tools.scheduling_identity import identities_from
from swcraft_tools.schema import (
    ApplicationDataType,
    CompositionMember,
    Connector,
    DataTypeMap,
    Memory,
    Partition,
    Port,
    Requirement,
    SRInterface,
    SWComponent,
    ValueDataType,
    core_of,
)
from swcraft_tools.trace import (
    is_element_form,
    link_sites_of_state,
    parse_element_reference,
    parse_link,
    requirement_stubs,
    requirement_versions,
    satisfying_elements,
    site_kind,
    site_kind_layer,
    stale_link,
)
from swcraft_tools.trace_records import DECISIONS_FILE
from swcraft_tools.validator import (
    effective_asil_levels,
    requirement_allocated,
)
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.yaml_load import load_yaml_document

REPO_ROOT = Path(__file__).resolve().parent.parent
SET_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "agent-bench"
TEMPLATES_DIR = SET_DIR / "templates"

#: The two oracle classes a template states.
ORACLES = ("structural", "product-rule")

#: The checker's task kinds but ``guide``, whose questions are the same on every model.
KINDS = ("change", "question")

#: A template file's keys: every one required but ``note``.
TEMPLATE_KEYS = ("id", "task", "group", "kind", "binding", "oracle", "prompt", "note")

#: The set a rendered task names, the checker's ``ANY_SET``.
ANY_SET = "any"

#: The long-list rule's head, the checker's ``LONG_LIST_HEAD``: a list longer than this opts in.
LONG_LIST_HEAD = 20

#: How many components a ``mentions_none`` of task 12 names at most.
NONE_COMPONENTS = 4

#: The closed codes a binding answers when a template has no target on a model, plus
#: ``baseline-has-errors``, decided once for every change template, and ``binding-failed``, which
#: the runner records for a binding that raised (its exception is never written: a model value may
#: be in its text).
NOT_APPLICABLE = (
    "baseline-has-errors",
    "binding-failed",
    "consumer-named-like-memory",
    "core-named-like-an-answer-word",
    "export-refused",
    "fewer-than-three-candidates",
    "fewer-than-two-components",
    "indistinct-consumers",
    "long-site-list",
    "nested-requirement-id",
    "no-component",
    "no-core-with-one-ram",
    "no-core-with-tasks",
    "no-free-port",
    "no-interface-named",
    "no-linked-requirement",
    "no-mapping-set",
    "no-memory-consumer",
    "no-ordinary-component",
    "no-partition",
    "no-product-order",
    "no-provide-port-in-a-composition",
    "no-rated-component",
    "no-require-port",
    "no-sender-receiver-interface",
    "no-sender-receiver-read",
    "no-stale-link",
    "no-synchronous-call",
    "no-task-pair",
    "no-topic",
    "no-unallocated-requirement",
    "no-unconnected-port",
    "no-value-implementation-type",
    "numeric-requirement-id",
    "right-run-refused",
    "search-refused",
    "tie",
    "undeclared-consumer",
    "undetermined-fit",
)


def _checker() -> ModuleType:
    """``scripts/agent_task_check.py``, loaded once by path; its ``load_script`` loads the other siblings."""
    name = "swcraft_agent_task_check"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / "agent_task_check.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CHECK = _checker()
_PLACEHOLDERS = CHECK.load_script(CHECK.PLACEHOLDERS_MODULE)
placeholders_in = _PLACEHOLDERS.placeholders_in
fill = _PLACEHOLDERS.fill


class TemplateError(Exception):
    """A template file this script cannot bind as written."""


class ProjectError(Exception):
    """A project this script cannot copy as written (its manifest is not a mapping)."""


# ---------------------------------------------------------------------------
# The frame.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class BoundModel:
    """One model under test, loaded once: its state, its ``Locator``, the registry, its paths.

    ``repr`` is the class name alone: a bound model printed by accident names nothing of the project.
    """

    manifest: Path
    model_root: Path
    registry: ModelRegistry
    state: ModelState
    locator: Locator

    def __repr__(self) -> str:
        return "BoundModel()"

    @classmethod
    def load(cls, manifest: Path) -> BoundModel:
        """The project whose manifest is *manifest*, its model root resolved from that manifest."""
        model_root = Path(require_project(manifest.as_posix()).resolved_model_root)
        registry = current_registry()
        state = load_model_state(model_root, registry=registry)
        # The passport pass loads the model again; the YAML texts are cached per process.
        locator = compute_passport(model_root).locator
        return cls(manifest=manifest, model_root=model_root, registry=registry, state=state, locator=locator)

    @property
    def project(self) -> Path:
        """The project directory, the manifest's own."""
        return self.manifest.parent

    def instance_words(self, types: Iterable[str]) -> list[str]:
        """The segments of every instance path of *types* (component type names), in row order, each once.

        Each member instancing one of them and up the composition tree each holder and its instance
        (``RootComposition/ExcludedSwc``). A right reply may name them beside a type, so a
        ``mentions_none`` leaves them out.
        """
        compositions = self.by_name("compositions")
        order = self.names("compositions")
        wanted = set(types)
        words: dict[str, None] = {}
        holders: list[str] = []
        for name in order:
            for member in compositions[name].members:
                if member.type in wanted:
                    words.setdefault(member.name)
                    holders.append(name)
        seen: set[str] = set()
        while holders:
            holder = holders.pop(0)
            if holder in seen:
                continue
            seen.add(holder)
            words.setdefault(holder)
            for name in order:
                for member in compositions[name].members:
                    if member.composition == holder:
                        words.setdefault(member.name)
                        holders.append(name)
        return list(words)

    def by_name(self, layer: str) -> dict[str, Any]:
        """*layer*'s elements by identity, the first of a repeated identity kept (first wins)."""
        identity = self.registry.layer(layer).identity_field
        elements: dict[str, Any] = {}
        for element in self.state[layer]:
            elements.setdefault(str(getattr(element, identity)), element)
        return elements

    def names(self, layer: str) -> list[str]:
        """*layer*'s identities in row order (``swcraft query LAYER``'s), each once (the first kept)."""
        facts = self.registry.layer(layer)
        ordered = ordered_elements(list(self.state[layer]), facts, self.registry)
        return list(dict.fromkeys(str(getattr(element, facts.identity_field)) for element in ordered))

    def in_row_order(self, layer: str, names: Iterable[str]) -> list[str]:
        """*names* of *layer*, each once, in row order; a name the layer does not declare last, by name."""
        wanted = set(names)
        ordered = [name for name in self.names(layer) if name in wanted]
        return ordered + sorted(wanted - set(ordered), key=str)

    @cached_property
    def baseline_error_count(self) -> int:
        """How many errors ``check-model --format json`` reports on the model, taken once per bound model.

        Read on a scratch copy (:func:`scratch_project`). A model with an error defeats every change
        template: the editing engine waives no violation the baseline already held, so no change set
        commits on it, a correct agent's included.
        """
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
            return check_model_error_count(scratch_project(self, Path(scratch) / "project"))


@dataclass(frozen=True, repr=False)
class Rendered:
    """A template bound to one model: its placeholder values and its expectation.

    ``expect`` is in the committed task format; ``reference`` says in words how it was computed;
    ``right_run`` is a change template's right run, called with the model root of a project copy,
    ``None`` for a question. ``inputs`` are files the task needs in the project before the run, by
    project-relative POSIX path. ``repr`` names nothing.
    """

    values: dict[str, str]
    expect: dict[str, Any]
    reference: dict[str, Any]
    right_run: Callable[[Path], None] | None = None
    inputs: dict[str, bytes] = dataclasses.field(default_factory=dict)

    def __repr__(self) -> str:
        return "Rendered()"


@dataclass(frozen=True)
class NotApplicable:
    """A template with no target on a model, by one closed code of :data:`NOT_APPLICABLE`."""

    code: str

    def __post_init__(self) -> None:
        if self.code not in NOT_APPLICABLE:
            raise ValueError(f"not a not-applicable code: {self.code!r}")


Binding = Callable[[BoundModel], "Rendered | NotApplicable"]


def _question(answer: dict[str, Any]) -> dict[str, Any]:
    return {"model": "unchanged", "answer": answer}


def _mentions(names: list[str], **more: Any) -> dict[str, Any]:
    """An answer naming every one of *names*, opted into the long-list rule when they are many."""
    answer: dict[str, Any] = {"mentions_all": names, **more}
    if len(names) > LONG_LIST_HEAD:
        answer["long_list"] = True
    return answer


def _inside(name: str, other: str) -> bool:
    """Whether *name* appears in *other* as the checker's whole word (``REQ-1`` inside ``REQ-1.1``), or equals it."""
    return CHECK._named(name).search(CHECK._bare(other)) is not None


def _numeric(name: str) -> bool:
    """Whether *name* holds no letter (``3``, ``120``, ``1.2``): a reply's count or figure would name it as a word."""
    return re.search(r"[A-Za-z]", name) is None


def _plain_word(name: str) -> bool:
    """Whether *name* reads as an ordinary word a reply's own prose may carry (``Other``, ``Component``).

    Its bare form is letters only, with no capital after its first letter; a digit, an inner capital
    or an underscore makes an identifier no sentence holds by accident. Not reached: an acronym or a
    digit a reply still writes as prose (``RAM``, ``2nd``).
    """
    bare = CHECK._bare(name)
    return re.fullmatch(r"[a-z]+", bare.casefold()) is not None and bare[1:] == bare[1:].lower()


#: An acronym a reply's prose may carry: one token of capitals and digits (``RAM``, ``CAN2``).
_CAPITALS_TOKEN = re.compile(r"[A-Z0-9]*[A-Z][A-Z0-9]*")


def _prose_word(name: str) -> bool:
    """Whether a right reply's own prose may carry *name*: an ordinary word, a checker layer word or an acronym.

    Not reached: a mixed-case prose word (``iPhone``), and a capitals token that is a real
    identifier (``BRAKECTL``), which is left out too -- a weaker check, never a false failure.
    """
    bare = CHECK._bare(name)
    layer_words = {word.casefold() for words in CHECK.LAYER_WORDS.values() for word in words}
    return _plain_word(name) or bare.casefold() in layer_words or _CAPITALS_TOKEN.fullmatch(bare) is not None


def _none_of(candidates: list[str], answer_names: list[str], limit: int) -> list[str]:
    """Up to *limit* of *candidates* that no right answer can name by accident.

    A candidate is left out when it equals a name of *answer_names* ignoring case or either holds
    the other as a whole word (naming the right one would fail the right reply), and when it holds
    no letter (a right reply's count would name it).
    """
    return [
        name
        for name in candidates
        if not _numeric(name) and not any(_inside(name, taken) or _inside(taken, name) for taken in answer_names)
    ][:limit]


def _nested_ids(names: Iterable[str], ids: Iterable[str]) -> bool:
    """Whether a name of *names* appears as the checker's whole word inside another id of *ids*.

    Then a reply naming only the longer id (``REQ-1.1``) would meet ``mentions_all`` for the shorter
    (``REQ-1``), a pass the agent did not earn.
    """
    others = list(ids)
    return any(name.casefold() != other.casefold() and _inside(name, other) for name in names for other in others)


# ---------------------------------------------------------------------------
# The bindings the Locator answers (08, 11, 12, 13).
# ---------------------------------------------------------------------------


def users_of_the_heaviest_interface(model: BoundModel) -> Rendered | NotApplicable:
    """08: the direct users of the interface the most component ports name.

    The first level of ``impact NAME --depth 1``, in that walk's order. A name the walk does not
    report follows in row order; a walk the product refuses (two elements sharing an identity) leaves
    row order, and a long list then has no product order.
    """
    try:
        interface = model.locator.heaviest_interface()
    except LookupError:
        return NotApplicable("no-interface-named")
    components = model.in_row_order("swc", (component for component, _ in model.locator.ports_naming(interface)))
    compositions = [
        name
        for name in model.names("compositions")
        if any(port.interface == interface for port in model.by_name("compositions")[name].ports)
    ]
    users = components + compositions
    try:
        walked = [
            dependent.ref.name
            for dependents_ in dependents(model.state, interface, depth=1).values()
            for dependent in dependents_
        ]
    except ValueError:
        if len(users) > LONG_LIST_HEAD:
            return NotApplicable("no-product-order")
    else:
        first = [name for name in dict.fromkeys(walked) if name in set(users)]
        users = first + [name for name in users if name not in set(first)]
    return Rendered(
        values={"interface": interface},
        expect=_question(_mentions(users)),
        reference={
            "target": "the interface the most component ports name (Locator.heaviest_interface)",
            "computed_by": "swcraft impact INTERFACE --depth 1 --format json, the entries matched as the interface",
        },
    )


def ports_of_the_largest_component(model: BoundModel) -> Rendered | NotApplicable:
    """11: how many ports the component with the most ports has, and which, in port order."""
    try:
        component = model.locator.largest_component()
    except LookupError:
        return NotApplicable("no-component")
    ports = [port.name for port in model.by_name("swc")[component].ports]
    if not ports:
        return NotApplicable("no-component")
    return Rendered(
        values={"component": component},
        expect=_question(_mentions(ports, number=len(ports))),
        reference={
            "target": "the component with the most ports (Locator.largest_component)",
            "computed_by": "swcraft query swc --members ports, the rows of the component",
        },
    )


def components_on_the_heaviest_interface(model: BoundModel) -> Rendered | NotApplicable:
    """12: the components with a port of the interface the most component ports name, row order."""
    try:
        interface = model.locator.heaviest_interface()
    except LookupError:
        return NotApplicable("no-interface-named")
    pairs = model.locator.ports_naming(interface)
    components = model.in_row_order("swc", (component for component, _ in pairs))
    others = [name for name in model.names("swc") if name not in set(components)]
    words = components + [interface] + [port for _, port in pairs]
    answer = _mentions(components)
    none = _none_of(others, [*words, *model.instance_words(components)], NONE_COMPONENTS)
    if none:
        answer["mentions_none"] = none
    return Rendered(
        values={"interface": interface},
        expect=_question(answer),
        reference={
            "target": "the interface the most component ports name (Locator.heaviest_interface)",
            "computed_by": "swcraft query --request {address: {layer: swc}, members: ports, "
            "where: [{field: interface, equals: INTERFACE}]}",
            "mentions_none": f"the first {NONE_COMPONENTS} components, in row order, with no port of it",
        },
    )


def unconnected_component_ports(model: BoundModel) -> Rendered | NotApplicable:
    """13: every unconnected component port with its component, components in row order and ports in port order.

    The answer is the checker's ``with_owner``, so a port name two components share is asked once
    per component. Composition ports are not asked: the prompt says *component ports*.
    """
    pairs = [
        {"owner": component, "name": port}
        for component in model.names("swc")
        for port in model.locator.unconnected_ports(component)
    ]
    if not pairs:
        return NotApplicable("no-unconnected-port")
    answer: dict[str, Any] = {"with_owner": pairs}
    if len(pairs) > LONG_LIST_HEAD:
        answer["long_list"] = True
    return Rendered(
        values={},
        expect=_question(answer),
        reference={
            "target": "the model",
            "computed_by": "swcraft query --request {address: {layer: swc}, members: ports, form: index, "
            "derived: [unconnected], where: [{field: derived.unconnected.direction, exists: true}]}",
        },
    )


# ---------------------------------------------------------------------------
# The structural questions the Locator lacks (15, 16, 18, 21).
# ---------------------------------------------------------------------------

#: How many elements a ``mentions_none`` of tasks 15, 16 and 18 names at most.
NONE_OTHERS = 3


def _components_by_partition(model: BoundModel) -> dict[str, list[str]]:
    """Each partition's components, in row order: the components a task of it maps a runnable of.

    A task maps a component when one of its runnable entries resolves, by type or by instance, to a
    declared component that declares the runnable; an entry resolving to nothing places nothing.
    """
    hardware = model.by_name("hardware")
    components = model.by_name("swc")
    identities = identities_from(list(model.state["compositions"]))
    placed: dict[str, set[str]] = {name: set() for name, element in hardware.items() if isinstance(element, Partition)}
    for task in model.state["tasks"]:
        if task.partition not in placed:
            continue
        for entry in task.runnables:
            component = identities.component_of(entry.swc, entry.instance, components)
            if component is not None and any(runnable.name == entry.runnable for runnable in component.runnables):
                placed[task.partition].add(component.name)
    return {name: model.in_row_order("swc", members) for name, members in placed.items()}


def components_in_the_busiest_partition(model: BoundModel) -> Rendered | NotApplicable:
    """15: the components inside the partition that runs the most of them, row order.

    The ``mentions_none`` names components another partition runs first: the clearer wrong answer.
    """
    placed = {name: members for name, members in _components_by_partition(model).items() if members}
    if not placed:
        return NotApplicable("no-partition")
    partition = min(placed, key=lambda name: (-len(placed[name]), name))
    members = placed[partition]
    elsewhere = {component for name, held in placed.items() if name != partition for component in held}
    outside = [name for name in model.names("swc") if name not in set(members)]
    others = [name for name in outside if name in elsewhere] + [name for name in outside if name not in elsewhere]
    answer = _mentions(members)
    none = _none_of(others, [*members, partition, *model.instance_words(members)], NONE_OTHERS)
    if none:
        answer["mentions_none"] = none
    return Rendered(
        values={"partition": partition},
        expect=_question(answer),
        reference={
            "target": "the partition whose tasks map the runnables of the most components",
            "computed_by": "swcraft query --request {address: {layer: swc}, form: index, derived: [partitions]}",
            "mentions_none": f"up to {NONE_OTHERS} components no task of it maps, those another partition "
            "runs first, each group in row order",
        },
    )


def tasks_on_the_busiest_core(model: BoundModel) -> Rendered | NotApplicable:
    """16: the tasks on the core whose partitions hold the most tasks, row order.

    A task is on the core of its partition (``schema.core_of``); one that resolves to nothing is on no core.
    """
    hardware = model.by_name("hardware")
    on_core: dict[str, list[str]] = {}
    tasks_by_name = model.by_name("tasks")
    for name in model.names("tasks"):
        core = core_of(tasks_by_name[name], hardware)
        if core is not None:
            on_core.setdefault(core.name, []).append(name)
    if not on_core:
        return NotApplicable("no-core-with-tasks")
    core_name = min(on_core, key=lambda name: (-len(on_core[name]), name))
    tasks = on_core[core_name]
    elsewhere = model.in_row_order(
        "tasks", (task for name, held in on_core.items() if name != core_name for task in held)
    )
    answer = _mentions(tasks)
    none = _none_of(elsewhere, [*tasks, core_name], NONE_OTHERS)
    if none:
        answer["mentions_none"] = none
    return Rendered(
        values={"core": core_name},
        expect=_question(answer),
        reference={
            "target": "the core whose partitions hold the most tasks",
            "computed_by": "swcraft query hardware --where core=CORE, then swcraft query tasks --where "
            "partition=PARTITION for each",
            "mentions_none": f"the first {NONE_OTHERS} tasks, in row order, on another core",
        },
    )


def _requirement_sites(model: BoundModel) -> dict[str, list[dict[str, str]]]:
    """Each requirement id's link sites, in the contract's layer order, each site once.

    A site is a ``satisfies`` member naming the id (any version), stated as the ``with_layer`` check
    reads it: a port's or a runnable's with its owning component, a hardware element's under ``hardware``.
    """
    sites: dict[str, list[dict[str, str]]] = {}
    for layer, element, site in link_sites_of_state(model.state.layers):
        if is_element_form(site.value):
            continue
        kind = site_kind(layer, element, site)
        entry = {"name": site.owner[1] if site.owner else element.name}
        entry["layer"] = "hardware" if site_kind_layer(kind) == "hardware" else kind
        if site.owner is not None:
            entry["owner"] = element.name
        held = sites.setdefault(parse_link(site.value).id, [])
        if entry not in held:
            held.append(entry)
    return sites


def _declared_requirements(model: BoundModel) -> list[str]:
    """The declared requirements that are not stubs, in row order."""
    requirements = model.by_name("requirements")
    return [rid for rid in model.names("requirements") if not requirements[rid].stub]


def _most_linked_requirement(model: BoundModel) -> tuple[str, list[dict[str, str]]] | None:
    """The declared, non-stub requirement with the most link sites, and its sites; ``None`` when none has one."""
    sites = _requirement_sites(model)
    linked = [rid for rid in _declared_requirements(model) if sites.get(rid)]
    if not linked:
        return None
    requirement = min(linked, key=lambda rid: (-len(sites[rid]), rid))
    return requirement, sites[requirement]


def elements_satisfying_the_most_linked_requirement(model: BoundModel) -> Rendered | NotApplicable:
    """18: the elements satisfying the requirement with the most link sites, each with its layer.

    More than :data:`LONG_LIST_HEAD` sites is ``long-site-list``: the profile's long-list rule answers
    the count and the first twenty, and ``with_layer`` has no short form.
    """
    found = _most_linked_requirement(model)
    if found is None:
        return NotApplicable("no-linked-requirement")
    requirement, sites = found
    if len(sites) > LONG_LIST_HEAD:
        return NotApplicable("long-site-list")
    holders = {site.get("owner", site["name"]) for site in sites if site["layer"] in ("swc", "port", "runnable")}
    others = [name for name in model.names("swc") if name not in holders]
    words = [requirement, *(site["name"] for site in sites), *(site.get("owner", "") for site in sites)]
    answer: dict[str, Any] = {"with_layer": sites}
    none = _none_of(others, [*(word for word in words if word), *model.instance_words(holders)], NONE_OTHERS)
    if none:
        answer["mentions_none"] = none
    return Rendered(
        values={"requirement": requirement},
        expect=_question(answer),
        reference={
            "target": "the declared requirement, not a stub, with the most link sites (element-form members excluded)",
            "computed_by": "swcraft query --dependents REQUIREMENT --depth 1 --format json",
            "mentions_none": f"the first {NONE_OTHERS} components, in row order, with no link on it",
        },
    )


#: A title word the topic rule reads: letters only.
_LETTERS = re.compile(r"[A-Za-z]+")
#: How many letters a topic word has at least, after a plural ``s`` is dropped.
TOPIC_WORD_LETTERS = 4
#: The endings whose ``s`` is no plural (``status``, ``analysis``, ``class``), kept.
_NOT_A_PLURAL = ("ss", "us", "is")


def _topic_word(word: str) -> str | None:
    """*word* as a topic word -- lower case, a plural ``s`` dropped -- or ``None`` when it is none."""
    if _LETTERS.fullmatch(word) is None:
        return None
    word = word.lower()
    if word.endswith("s") and not word.endswith(_NOT_A_PLURAL):
        word = word[:-1]
    return word if len(word) >= TOPIC_WORD_LETTERS else None


def _whole_word(word: str) -> str:
    """*word* as the committed 21 rule matches it: a whole word, case-insensitive, an optional plural ``s``."""
    return rf"(?<![A-Za-z0-9_]){re.escape(word)}s?(?![A-Za-z0-9_])"


def requirements_about_the_commonest_topic(model: BoundModel) -> Rendered | NotApplicable:
    """21: the requirements about the two-word title phrase the most requirements hold.

    A candidate is two adjacent title words (each :func:`_topic_word`). A requirement holds it when
    its title or description holds the two words adjacent and in order, whole words, case-insensitive,
    each optionally plural. The phrase the most requirements hold wins (ties: the phrase), when at
    least two and at most half of the declared non-stub requirements hold it. A stub is neither
    required nor refused.
    """
    requirements = model.by_name("requirements")
    declared = _declared_requirements(model)
    texts = {rid: f"{requirements[rid].title}\n{requirements[rid].description}" for rid in declared}
    candidates: set[tuple[str, str]] = set()
    for rid in declared:
        words = [_topic_word(word) for word in requirements[rid].title.split()]
        candidates.update((first, second) for first, second in zip(words, words[1:]) if first and second)
    holders: dict[tuple[str, str], list[str]] = {}
    for first, second in candidates:
        phrase = re.compile(_whole_word(first) + r"\s+" + _whole_word(second), re.IGNORECASE)
        held = [rid for rid in declared if phrase.search(texts[rid])]
        if 2 <= len(held) <= len(declared) // 2:
            holders[(first, second)] = held
    if not holders:
        return NotApplicable("no-topic")
    topic = min(holders, key=lambda pair: (-len(holders[pair]), pair))
    matches = holders[topic]
    if _nested_ids(matches, requirements):
        return NotApplicable("nested-requirement-id")
    if any(_numeric(rid) for rid in matches):
        return NotApplicable("numeric-requirement-id")
    words = [re.compile(_whole_word(word), re.IGNORECASE) for word in topic]
    unrelated = [rid for rid in declared if not any(word.search(texts[rid]) for word in words)]
    unrelated = _none_of(unrelated, matches, len(unrelated))
    answer = _mentions(matches)
    if unrelated:
        answer["mentions_none"] = unrelated
    return Rendered(
        values={"phrase": " ".join(topic)},
        expect=_question(answer),
        reference={
            "target": "the two adjacent title words (letters only, four or more, a plural s dropped) the most "
            "requirements hold, at least two and at most half of them",
            "rule": "the committed task 21's: the title or the description holds the phrase's words adjacent and "
            "in order, as whole words, case-insensitive, each optionally followed by a plural s; the reply names "
            "none whose text holds none of the phrase's words; a stub is neither required nor refused",
            "computed_by": "the rule over every declared requirement's title and description, read through the "
            "product's loader",
        },
    )


# ---------------------------------------------------------------------------
# The product-rule questions (09, 14, 17, 19, 20, 22): each answer is a definition the product
# owns, called in-process -- never through the MCP server under test.
# ---------------------------------------------------------------------------

#: The two readings of a kilobyte task 09's size must exceed the headroom by, both.
KILOBYTE_READINGS = (1000, 1024)

#: The memory type task 09 asks about (``schema.Memory.type``).
RAM_TYPE = "RAM"

#: Benchmark task 09's ``first_of`` candidates: the prompt's own answer words, ``no`` expected.
ANSWER_WORDS = ("yes", "no")


def _memories_with_consumers(usages: list[ResourceUsage]) -> list[tuple[ResourceUsage, list[str]]]:
    """Each memory with its consumers -- the components that declared a non-zero amount -- in reader order."""
    found = []
    for usage in usages:
        if isinstance(usage.resource, Memory):
            consumers = [consumer.name for consumer in usage.consumers if consumer.declared and consumer.amount]
            if consumers:
                found.append((usage, consumers))
    return found


def _smallest_size_that_does_not_fit(usage: ResourceUsage) -> int:
    """The smallest whole number of KB for which ``fits`` answers ``False`` under both readings of a KB.

    Starts past the headroom under the smaller reading and asks the product's own ``fits`` until both
    readings answer ``False``.
    """
    size = max(1, int(usage.headroom // min(KILOBYTE_READINGS)) + 1)
    while any(fits(usage, Fraction(size * reading)) is not False for reading in KILOBYTE_READINGS):
        size += 1
    return size


def ram_left_on_a_core(model: BoundModel) -> Rendered | NotApplicable:
    """09: is there enough RAM left on a core for a size that does not fit -- expected ``no``.

    The core is the first, by name, that exactly one ``RAM`` memory serves, with a threshold below 1,
    a consumer and a determinate budget. The size fits under neither reading of a KB, so the answer
    is ``no`` by construction: a reply that always says no passes, so 09 is read beside 17, never
    alone. A core or memory named like an answer word (``Core-No``) is
    ``core-named-like-an-answer-word``: a right reply naming it would lead with that word.
    """
    report = resource_report(str(model.model_root))
    cores = sorted(
        (usage.name for usage in report.analysis.resources if not isinstance(usage.resource, Memory)), key=str
    )
    served = []
    for core in cores:
        rams = [usage for usage in reachable_memories(report.analysis, core) if usage.resource.type == RAM_TYPE]
        if len(rams) == 1 and rams[0].resource.threshold < 1 and _memories_with_consumers(rams):
            served.append((core, rams[0]))
    if not served:
        return NotApplicable("no-core-with-one-ram")
    determinate = [(core, usage) for core, usage in served if budget_status(usage) != STATUS_UNKNOWN]
    if not determinate:
        return NotApplicable("undetermined-fit")
    core, usage = determinate[0]
    memory = usage.resource
    if any(_inside(word, name) for word in ANSWER_WORDS for name in (core, memory.name)):
        return NotApplicable("core-named-like-an-answer-word")
    size = _smallest_size_that_does_not_fit(usage)
    return Rendered(
        values={"core": core, "size_kb": str(size)},
        expect=_question({"first_of": {"candidates": list(ANSWER_WORDS), "expected": "no"}}),
        reference={
            memory.name: {
                "size": memory.size,
                "threshold": memory.threshold,
                "budget": format_bytes(usage.limit),
                "consumed": format_bytes(usage.consumption),
                "headroom": format_bytes(usage.headroom),
            },
            "requested": f"{size} KB ({size * 1024} or {size * 1000} bytes), both more than the headroom",
            "computed_by": "resource_report.resource_report and resource_analysis.fits, in-process; "
            "swcraft resource-report prints the same figures",
        },
    )


def components_at_the_commonest_level(model: BoundModel) -> Rendered | NotApplicable:
    """14: how many components hold, effectively, the ASIL level from A to D the most components hold.

    The effective level is ``validator.effective_asil_levels``', compared by its base letter (a
    decomposed ``B(D)`` counts as ``B``); a tie goes to the higher level.
    """
    levels = effective_asil_levels(list(model.state["swc"]), list(model.state["requirements"]))
    counts = {level: 0 for level in ASIL_LEVELS if level != "QM"}
    for value in levels.values():
        base, _ = parse_asil(value)
        if base in counts:
            counts[base] += 1
    if not any(counts.values()):
        return NotApplicable("no-rated-component")
    level = max(counts, key=lambda name: (counts[name], asil_rank(name)))
    return Rendered(
        values={"level": level},
        expect=_question({"number": counts[level]}),
        reference={
            "effective": {name: count for name, count in counts.items() if count},
            "computed_by": "validator.effective_asil_levels, in-process; swcraft query --request {address: "
            "{layer: swc}, form: index, derived: [effective_level]} answers the same levels",
        },
    )


def largest_consumer_of_the_busiest_memory(model: BoundModel) -> Rendered | NotApplicable:
    """17: which component takes the most of the memory the most components consume.

    Of the memories whose every consumer declared an amount, the one with the most consumers (ties:
    the name); its largest consumer by the resource report's figure. Not applicable: two consumers
    sharing the largest figure (``tie``), a winner named like the memory
    (``consumer-named-like-memory``), a consumer the checker cannot tell from the winner
    (``indistinct-consumers``). The ``first_of`` candidates are the memory's consumers in name order,
    those named like the memory or a prose word left out (the winner never).
    """
    report = resource_report(str(model.model_root))
    memories = _memories_with_consumers(report.analysis.resources)
    if not memories:
        return NotApplicable("no-memory-consumer")
    # A consumer with no declared amount leaves the largest unknown, and a reply saying so is right.
    memories = [found for found in memories if all(consumer.declared for consumer in found[0].consumers)]
    if not memories:
        return NotApplicable("undeclared-consumer")
    usage, _ = min(memories, key=lambda found: (-len(found[1]), found[0].name))
    amounts = sorted(
        ((consumer.amount, consumer.name) for consumer in usage.consumers if consumer.declared and consumer.amount),
        key=lambda pair: (-pair[0], pair[1]),
    )
    if len(amounts) > 1 and amounts[0][0] == amounts[1][0]:
        return NotApplicable("tie")
    largest = amounts[0][1]
    if _inside(largest, usage.name) or _inside(usage.name, largest):
        return NotApplicable("consumer-named-like-memory")
    consumers = sorted(dict.fromkeys(consumer.name for consumer in usage.consumers), key=str)
    if any(name != largest and (_inside(name, largest) or _inside(largest, name)) for name in consumers):
        # The checker reads both as one name, and the earlier candidate wins that tie.
        return NotApplicable("indistinct-consumers")
    candidates = [
        name
        for name in consumers
        if name == largest
        or (name in _none_of([name], [usage.name, *model.instance_words([largest])], 1) and not _prose_word(name))
    ]
    return Rendered(
        values={"memory": usage.name},
        expect=_question({"first_of": {"candidates": candidates, "expected": largest}}),
        reference={
            "consumers": {name: int(amount) for amount, name in amounts},
            "computed_by": "resource_report.resource_report, in-process; swcraft resource-report prints the same "
            "consumers",
        },
    )


def unallocated_requirements(model: BoundModel) -> Rendered | NotApplicable:
    """19: the requirements no element satisfies, by ``validator.requirement_allocated``, row order.

    A stub is neither required nor refused. The ``mentions_none`` names up to four allocated
    requirements, in row order.
    """
    requirements = list(model.state["requirements"])
    satisfied = satisfying_elements(str(model.model_root)).keys()
    stubs = requirement_stubs(requirements)
    allocation = {
        rid: requirement_allocated(rid, satisfied, stubs)
        for rid in model.in_row_order("requirements", requirement_versions(requirements))
    }
    unallocated = [rid for rid, allocated in allocation.items() if allocated is False]
    if not unallocated:
        return NotApplicable("no-unallocated-requirement")
    if _nested_ids(unallocated, allocation):
        return NotApplicable("nested-requirement-id")
    if any(_numeric(rid) for rid in unallocated):
        return NotApplicable("numeric-requirement-id")
    answer = _mentions(unallocated)
    none = _none_of([rid for rid, allocated in allocation.items() if allocated], unallocated, NONE_COMPONENTS)
    if none:
        answer["mentions_none"] = none
    return Rendered(
        values={},
        expect=_question(answer),
        reference={
            "target": "the model",
            "computed_by": "validator.requirement_allocated over trace.satisfying_elements, in-process; swcraft "
            "check-model --format json, the findings trace.requirement-unallocated",
            "mentions_none": f"the first {NONE_COMPONENTS} allocated requirements, in row order",
        },
    )


def links_needing_confirmation(model: BoundModel) -> Rendered | NotApplicable:
    """20: the links whose requirement has a newer version -- each one's requirement and owning element.

    A link is a ``satisfies`` member or a requirement-form ``derived_from`` member; it needs
    confirmation when ``trace.stale_link`` classifies it with a stated version (a bare id is another
    warning). The requirements come first, in row order, then the owners by name; more than
    :data:`LONG_LIST_HEAD` together is ``no-product-order``.
    """
    requirements = list(model.state["requirements"])
    versions = requirement_versions(requirements)
    links: list[tuple[str, str]] = [
        (element.name, site.value) for _, element, site in link_sites_of_state(model.state.layers)
    ]
    links.extend(
        (requirement.id, value)
        for requirement in requirements
        for value in requirement.derived_from
        if parse_element_reference(value) is None
    )
    stale: list[tuple[str, str]] = []
    for owner, value in links:
        found = stale_link(value, versions)
        if found is not None and found.stated is not None:
            stale.append((found.requirement_id, owner))
    if not stale:
        return NotApplicable("no-stale-link")
    stated = model.in_row_order("requirements", (requirement for requirement, _ in stale))
    owners = sorted({owner for _, owner in stale}, key=str)
    named = [*stated, *(owner for owner in owners if owner not in stated)]
    if _nested_ids(named, [*versions, *named]):
        return NotApplicable("nested-requirement-id")
    if any(_numeric(rid) for rid in stated):
        return NotApplicable("numeric-requirement-id")
    if len(named) > LONG_LIST_HEAD:
        # No one read answers requirements and owners in one order: the long-list head would be a guess.
        return NotApplicable("no-product-order")
    return Rendered(
        values={},
        expect=_question(_mentions(named)),
        reference={
            "target": "the model",
            "computed_by": "validator.stale_link with a stated version, in-process; swcraft check-model --format "
            "json, the findings trace.link-needs-confirmation",
        },
    )


#: How many places of the trace search task 22 asks for.
TOP_PLACES = 3


def _trace_document(model: BoundModel) -> dict[str, Any]:
    """The trace search's result document, its default configuration, run in-process on a scratch copy.

    The search's files land in the copy (:func:`scratch_project`); the project is never written.
    """
    from swcraft_tools import trace_search

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
        copy = scratch_project(model, Path(scratch) / "project")
        return trace_search.run_trace(require_project((copy / MANIFEST).as_posix()), None).document


def _top_places(entry: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """A requirement entry's first :data:`TOP_PLACES` groups: each one's name and the terms it shares, in order."""
    groups: dict[int, dict[str, Any]] = {}
    for candidate in entry["candidates"]:
        groups.setdefault(candidate["place"], candidate["group"])
    top = []
    for place in sorted(groups)[:TOP_PLACES]:
        group = groups[place]
        terms = [term for evidence in group["evidence"] for term in evidence.get("terms", [])]
        top.append((group["name"], list(dict.fromkeys(terms))))
    return top


def candidates_for_a_requirement(model: BoundModel) -> Rendered | NotApplicable:
    """22: the elements that could realise a requirement, with the reason -- the trace search's top three.

    The requirement is the first, by id, for which the trace search answers three distinct groups
    each sharing a term. A search the product refuses (an ambiguous identity) is ``search-refused``.
    """
    from swcraft_tools import trace_search

    try:
        document = _trace_document(model)
    except trace_search.TraceError:
        return NotApplicable("search-refused")
    for entry in sorted(document["requirements"], key=lambda entry: str(entry["id"])):
        top = _top_places(entry)
        names = [name for name, _ in top]
        if len(top) == TOP_PLACES and len(set(names)) == TOP_PLACES and all(terms for _, terms in top):
            return Rendered(
                values={"requirement": entry["id"]},
                expect=_question({"candidates": {"top": names, "terms": dict(top)}}),
                reference={
                    "rule": f"N = {TOP_PLACES}: the reply names the {TOP_PLACES} top groups of the deterministic "
                    "trace search with its default configuration, in any order, and for each at least one term "
                    "the search says it shares",
                    "computed_by": "trace_search.run_trace in-process on a scratch copy; swcraft experiment trace "
                    "--format json answers the same places",
                },
            )
    return NotApplicable("fewer-than-three-candidates")


# ---------------------------------------------------------------------------
# The right run, and the change templates that add (01, 03, 07, 23).
# ---------------------------------------------------------------------------

MANIFEST = CHECK.MANIFEST
#: Where a scratch copy keeps its model root.
SCRATCH_MODEL_ROOT = "model"
#: The manifest field a scratch copy drops: a pin refuses every writing command under another
#: release, while the copy is checked with the toolchain at hand.
TOOLCHAIN_PIN = "toolchain_version"
ERROR_SEVERITY = "error"


def scratch_project(model: BoundModel, target: Path) -> Path:
    """:func:`copy_project` of *model*'s project at *target*, its model root the model's; *target* returned."""
    copy_project(model.manifest, target, model_root=model.model_root)
    return target


def _relative_outputs(document: dict[str, Any], project_dir: Path) -> None:
    """*document*'s absolute ``outputs.arxml`` and ``outputs.needs`` written relative to *project_dir*.

    In a copy an absolute path names the original project, so the copy's loader would refuse it as
    an escape. A path outside *project_dir* is left: the original's loader refuses it too.
    """
    outputs = document.get("outputs")
    if not isinstance(outputs, dict):
        return
    for key in ("arxml", "needs"):
        written = outputs.get(key)
        if not isinstance(written, str) or not Path(written).is_absolute():
            continue
        try:
            outputs[key] = Path(written).resolve().relative_to(project_dir).as_posix()
        except ValueError:
            continue


#: The copy's own output places: the product's defaults, which never meet :data:`SCRATCH_MODEL_ROOT`.
SCRATCH_OUTPUTS = {"dist_root": "dist", "views_root": "views", "arxml": "dist/model.arxml", "needs": "dist/needs.json"}


def _parts(written: str) -> tuple[str, ...]:
    """*written*, a manifest path relative to the project, as its normalised casefolded POSIX parts."""
    normal = posixpath.normpath(written.replace("\\", "/"))
    return tuple(part.casefold() for part in normal.split("/") if part not in ("", "."))


def _meets(first: str, second: str) -> bool:
    """Whether two relative manifest paths name one place or one lies inside the other, case-insensitively."""
    a, b = _parts(first), _parts(second)
    shorter = min(len(a), len(b))
    return a[:shorter] == b[:shorter]


def _clear_outputs(document: dict[str, Any], model_root: str) -> None:
    """*document*'s output roots that meet the copy's *model_root* pointed at :data:`SCRATCH_OUTPUTS`.

    A copy whose model root moved to :data:`SCRATCH_MODEL_ROOT` may sit on an output root
    (``dist_root: model``), which the loader refuses. An absent field takes a default that never
    meets it; a non-string path is left, refused in the original too.
    """
    for key in ("dist_root", "views_root"):
        written = document.get(key)
        if isinstance(written, str) and _meets(written, model_root):
            document[key] = SCRATCH_OUTPUTS[key]
    outputs = document.get("outputs")
    if isinstance(outputs, dict):
        for key in ("arxml", "needs"):
            written = outputs.get(key)
            if isinstance(written, str) and _meets(written, model_root):
                outputs[key] = SCRATCH_OUTPUTS[key]


def copy_project(manifest: Path, target: Path, *, model_root: Path | None = None, keep_relative: bool = False) -> bool:
    """The project of *manifest* copied to *target* (a directory not yet there); whether its pin was dropped.

    Copies the model root (resolved from the manifest unless given) without any ``.git`` entry, the
    trace decisions file when there is one, and the manifest with its toolchain pin dropped, its
    ``model_root`` pointed at the copy's and its output paths made to fit the copy. The model root
    lands under :data:`SCRATCH_MODEL_ROOT`, or with *keep_relative* at its own place relative to the
    manifest (when that lies inside the project directory and is not the directory itself; otherwise
    under :data:`SCRATCH_MODEL_ROOT`). The project is never written.
    """
    if model_root is None:
        model_root = Path(require_project(manifest.as_posix()).resolved_model_root)
    document = load_yaml_document(manifest.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ProjectError("the manifest is not a mapping")
    pinned = document.pop(TOOLCHAIN_PIN, None) is not None
    relative = SCRATCH_MODEL_ROOT
    if keep_relative:
        try:
            relative = model_root.resolve().relative_to(manifest.parent.resolve()).as_posix()
        except ValueError:
            relative = SCRATCH_MODEL_ROOT
        if relative in ("", "."):
            relative = SCRATCH_MODEL_ROOT
    document["model_root"] = relative
    _relative_outputs(document, manifest.parent.resolve())
    _clear_outputs(document, relative)
    target.mkdir(parents=True)
    # A ``.git`` in the copy would make the bench's ``git add`` record a gitlink, not the files.
    shutil.copytree(model_root, target / relative, ignore=shutil.ignore_patterns(".git"))
    decisions = manifest.parent / DECISIONS_FILE
    if decisions.is_file():
        shutil.copyfile(decisions, target / DECISIONS_FILE)
    (target / MANIFEST).write_text(
        yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
    )
    return pinned


def check_model_error_count(project: Path) -> int:
    """The number of findings of severity ``error`` ``check-model --format json`` reports on *project*.

    A run that answers no findings document counts as one error: nothing then proves the model
    free of them.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "check-model", "--format", "json",
         "--project", (project / MANIFEST).as_posix()],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )  # fmt: skip
    try:
        findings = json.loads(completed.stdout)["findings"]
        return sum(1 for finding in findings if finding["severity"] == ERROR_SEVERITY)
    except (ValueError, KeyError, TypeError):
        return 1


def element_text(element: BaseModel) -> str:
    """The file text the product's writers write for *element*."""
    return yaml.safe_dump(element_to_dict(element), default_flow_style=False, sort_keys=False)


def write_element(
    model_root: Path,
    layer: str,
    element: BaseModel,
    registry: ModelRegistry,
    placement: dict[str, str] | None = None,
) -> None:
    """Write *element* into *layer* under *model_root*, as the product writes it.

    At the element's own file when the layer declares its identity (the first such file), else at
    the free storage path of a new one.
    """
    identity = str(getattr(element, registry.layer(layer).identity_field))
    files = stored_element_files(model_root, layer, registry=registry).get(identity)
    if files:
        path = files[0].path
    else:
        path = model_root / free_storage_path(model_root, layer, identity, placement, registry=registry).path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(element_text(element), encoding="utf-8", newline="\n")


def apply_right_run(model: BoundModel, rendered: Rendered, target: Path) -> Path:
    """A scratch copy of *model*'s project at *target* with *rendered*'s right run applied; *target* returned."""
    if rendered.right_run is None:
        raise TemplateError("a question has no right run")
    project = scratch_project(model, target)
    for relative, content in rendered.inputs.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    rendered.right_run(CHECK.model_root(project))
    return project


#: The code a right run that raised is recorded under: it did not leave the state the task asks for.
RIGHT_RUN_RAISED = CHECK.ASSERTION


def right_run_failures(model: BoundModel, rendered: Rendered) -> list[Any]:
    """The checker's failures (``Failure``) of *rendered*'s right run on a scratch copy; empty when it passes.

    A right run that raises (an importer, writer or schema refusing the model's shape) is one
    failure, :data:`RIGHT_RUN_RAISED`, its text never kept since it may name the model.
    """
    task = {"id": "right-run", "kind": "change", "set": ANY_SET, "expect": rendered.expect}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
        try:
            project = apply_right_run(model, rendered, Path(scratch) / "run")
            baseline = model.project
            if model.manifest.resolve().as_posix() != (model.project / MANIFEST).resolve().as_posix():
                baseline = scratch_project(model, Path(scratch) / "baseline")
            return list(CHECK.check_task_coded(project, task, baseline=baseline))
        except Exception as error:  # noqa: BLE001 -- a defeated right run costs a row, never the run
            return [CHECK.Failure(RIGHT_RUN_RAISED, type(error).__name__)]


def offer(model: BoundModel, rendered: Rendered) -> Rendered | NotApplicable:
    """*rendered* when its right run passes the checker, else ``right-run-refused``.

    A model whose shape defeats a template costs a not-applicable row, never a FAIL charged to the agent.
    """
    return rendered if not right_run_failures(model, rendered) else NotApplicable("right-run-refused")


def _free_name(stem: str, taken: Iterable[str], *, width: int = 0) -> str:
    """A new name no name of *taken* equals ignoring case (a file name and the checker's match are both case-blind).

    With *width* 0: *stem* when free, else *stem* with the first free number from 2 appended.
    With a *width*: *stem* with the first free number from 1, zero-padded (``SWR-BENCH-001``).
    """
    held = {name.casefold() for name in taken}
    candidates = (f"{stem}{number:0{width}d}" for number in range(1, len(held) + 2))
    if not width:
        candidates = (stem if number == 1 else f"{stem}{number}" for number in range(1, len(held) + 2))
    return next(name for name in candidates if name.casefold() not in held)


def _with_port(model: BoundModel, component: str, port: Port) -> Any:
    """*component* as loaded, *port* appended to its ports."""
    element = model.by_name("swc")[component]
    return element.model_copy(update={"ports": [*element.ports, port]})


def _with_link(model: BoundModel, component: str, port: str, link: str) -> Any:
    """*component* as loaded, *link* appended to the ``satisfies`` of its port *port*."""
    element = model.by_name("swc")[component]
    ports = [
        held.model_copy(update={"satisfies": [*held.satisfies, link]}) if held.name == port else held
        for held in element.ports
    ]
    return element.model_copy(update={"ports": ports})


def _compositions_holding(model: BoundModel) -> dict[str, set[str]]:
    """Each component type -> the compositions with a member of that type."""
    held: dict[str, set[str]] = {}
    for name, composition in model.by_name("compositions").items():
        for member in composition.members:
            if member.type is not None:
                held.setdefault(member.type, set()).add(name)
    return held


def _port_names(model: BoundModel, component: str) -> list[str]:
    return [port.name for port in model.by_name("swc")[component].ports]


#: How many of the most-named sender-receiver interfaces task 01 tries, in the ``Locator``'s order.
CONNECT_TRIES = 3


def connect_two_components(model: BoundModel) -> Rendered | NotApplicable:
    """01: a sender port on the largest component, a receiver port on a neighbour, and a connector between them.

    The requirer is the largest component sharing a composition with the provider, any other when
    none does; the connector is wired in the first shared composition, else the root. The interface
    is the first of the :data:`CONNECT_TRIES` most-named sender-receiver interfaces whose right run
    the checker accepts: a rated requirer cannot consume an element rated below it without a
    measure, and the prompt asks for none.
    """
    interfaces = model.locator.heaviest_sender_receiver_interfaces()
    if not interfaces:
        return NotApplicable("no-sender-receiver-interface")
    try:
        provider = model.locator.largest_component()
    except LookupError:
        return NotApplicable("fewer-than-two-components")
    components = model.by_name("swc")
    others = [name for name in model.names("swc") if name != provider]
    if not others:
        return NotApplicable("fewer-than-two-components")
    held = _compositions_holding(model)
    sharing = [name for name in others if held.get(name, set()) & held.get(provider, set())]
    requirer = min(sharing or others, key=lambda name: (-len(components[name].ports), name))
    shared = sorted(held.get(requirer, set()) & held.get(provider, set()), key=str)
    composition = shared[0] if shared else None
    provider_port = _free_name("pBenchForwarded", _port_names(model, provider))
    requirer_port = _free_name("rBenchForwarded", _port_names(model, requirer))
    connector = _free_name("BenchForwarded", model.names("connectors"))
    ends = {"from": {"swc": provider, "port": provider_port}, "to": {"swc": requirer, "port": requirer_port}}
    wired = {"composition": composition} if composition is not None else {}

    def rendering(interface: str) -> Rendered:
        def right_run(root: Path) -> None:
            for component, port in ((provider, _port(provider_port, "provide", interface)),
                                    (requirer, _port(requirer_port, "require", interface))):  # fmt: skip
                write_element(root, "swc", _with_port(model, component, port), model.registry)
            write_element(
                root, "connectors", Connector.model_validate({"name": connector, **wired, **ends}), model.registry
            )

        return Rendered(
            values={
                "provider": provider,
                "provider_port": provider_port,
                "requirer": requirer,
                "requirer_port": requirer_port,
                "interface": interface,
            },
            expect=_change(
                [
                    _has_port(provider, {"name": provider_port, "direction": "provide", "interface": interface}),
                    _has_port(requirer, {"name": requirer_port, "direction": "require", "interface": interface}),
                    {"layer": "connectors", "has": ends},
                ]
            ),
            reference={
                "target": "the component with the most ports; the component with the most ports sharing a "
                "composition with it (any other when none does); the first of the "
                f"{CONNECT_TRIES} most-named sender-receiver interfaces whose right run the checker accepts",
                "right_run": "two ports and a connector, wired inside "
                + ("the first shared composition" if composition is not None else "the root"),
            },
            right_run=right_run,
        )

    for interface in interfaces[:CONNECT_TRIES]:
        rendered = rendering(interface)
        if not right_run_failures(model, rendered):
            return rendered
    return NotApplicable("right-run-refused")


def _port(name: str, direction: str, interface: str) -> Port:
    return Port.model_validate({"name": name, "direction": direction, "interface": interface})


def _change(assertions: list[dict[str, Any]]) -> dict[str, Any]:
    """A change task's expectation: its assertions and ``check-model`` with no error."""
    return {"check_model": "no-errors", "assertions": assertions}


def _has_port(component: str, port: dict[str, Any]) -> dict[str, Any]:
    return {"layer": "swc", "name": component, "has": {"ports": [port]}}


#: Benchmark task 03's new data element, inside its new interface (no other name can collide with it).
PAD_WEAR_ELEMENT = "padWear"
#: Benchmark task 03's application type's unit and range, as its prompt states them.
PAD_WEAR_UNIT = "percent"
PAD_WEAR_RANGE = {"min": 0, "max": 100}


def _pad_wear_type(app_type: str) -> list[dict[str, Any]]:
    """Benchmark task 03's application-type assertions, in either form the type may take: inline or by reference.

    The range is ``range`` or a ``data_constr_ref`` to those limits; the unit is not asserted, but a
    ``unit_ref`` or a ``compu_method_ref`` (and its ``unit``) may name new elements. An import states
    shared limits, units and conversions by reference, so asserting the inline form alone would
    charge a correct run in the model's own form to the agent.
    """
    return [
        {
            "any_of": [
                {"layer": "application_types", "name": app_type, "has": {"range": PAD_WEAR_RANGE}},
                {
                    "layer": "application_types",
                    "name": app_type,
                    "refers": {"data_constr_ref": {"layer": "data_constraints", "has": PAD_WEAR_RANGE}},
                },
            ]
        },
        {
            "layer": "application_types",
            "name": app_type,
            "refers": {"unit_ref": {"layer": "units", "optional": True}},
        },
        {
            "layer": "application_types",
            "name": app_type,
            "refers": {
                "compu_method_ref": {
                    "layer": "compu_methods",
                    "optional": True,
                    "refers": {"unit": {"layer": "units", "optional": True}},
                }
            },
        },
    ]


def new_interface_and_type(model: BoundModel) -> Rendered | NotApplicable:
    """03: a new interface of a new application type, a provide port on it, and the type's mapping.

    The component is the one with the most ports; the mapping set the first declared one it lists,
    else the one with the most maps; the implementation type the value type that set maps most
    often. A component listing no declared set gets the chosen one added, and the expectation
    asserts it: a map in a set the component does not list is ``application-type-unmapped``.
    """
    mapping_sets = model.by_name("mapping_sets")
    if not mapping_sets:
        return NotApplicable("no-mapping-set")
    try:
        component = model.locator.largest_component()
    except LookupError:
        return NotApplicable("no-component")
    listed = [name for name in model.by_name("swc")[component].data_type_mappings if name in mapping_sets]
    mapping_set = listed[0] if listed else min(mapping_sets, key=lambda name: (-len(mapping_sets[name].maps), name))
    implementations = model.by_name("implementation_types")
    counts: dict[str, int] = {}
    for entry in mapping_sets[mapping_set].maps:
        if isinstance(implementations.get(entry.implementation), ValueDataType):
            counts[entry.implementation] = counts.get(entry.implementation, 0) + 1
    if not counts:
        return NotApplicable("no-value-implementation-type")
    implementation = min(counts, key=lambda name: (-counts[name], name))
    interface = _free_name("IBenchPadWear", model.names("interfaces"))
    app_type = _free_name("BenchPadWear_Pct", model.names("application_types"))
    port = _free_name("pBenchPadWear", _port_names(model, component))

    def right_run(root: Path) -> None:
        write_element(
            root,
            "application_types",
            ApplicationDataType.model_validate(
                {"name": app_type, "kind": "application_type", "unit": PAD_WEAR_UNIT, "range": PAD_WEAR_RANGE}
            ),
            model.registry,
        )
        write_element(
            root,
            "interfaces",
            SRInterface.model_validate(
                {
                    "name": interface,
                    "kind": "sender_receiver",
                    "data_elements": [{"name": PAD_WEAR_ELEMENT, "application_type": app_type}],
                }
            ),
            model.registry,
        )
        changed = _with_port(model, component, _port(port, "provide", interface))
        if not listed:
            changed = changed.model_copy(update={"data_type_mappings": [*changed.data_type_mappings, mapping_set]})
        write_element(root, "swc", changed, model.registry)
        held = mapping_sets[mapping_set]
        entry = DataTypeMap.model_validate({"application": app_type, "implementation": implementation})
        write_element(root, "mapping_sets", held.model_copy(update={"maps": [*held.maps, entry]}), model.registry)

    return offer(
        model,
        Rendered(
            values={
                "interface": interface,
                "element": PAD_WEAR_ELEMENT,
                "app_type": app_type,
                "component": component,
                "port": port,
                "implementation_type": implementation,
                "mapping_set": mapping_set,
            },
            expect=_change(
                [
                    *_pad_wear_type(app_type),
                    {
                        "layer": "interfaces",
                        "name": interface,
                        "has": {
                            "kind": "sender_receiver",
                            "data_elements": [{"name": PAD_WEAR_ELEMENT, "application_type": app_type}],
                        },
                    },
                    _has_port(component, {"name": port, "direction": "provide", "interface": interface}),
                    *(
                        []
                        if listed
                        else [{"layer": "swc", "name": component, "has": {"data_type_mappings": [mapping_set]}}]
                    ),
                    {
                        "layer": "mapping_sets",
                        "name": mapping_set,
                        "has": {"maps": [{"application": app_type, "implementation": implementation}]},
                    },
                ]
            ),
            reference={
                "target": "the component with the most ports; the first mapping set it lists, else the one with the "
                "most maps, listed in the component's data_type_mappings when it lists none; the value-category "
                "implementation type that set maps most often",
            },
            right_run=right_run,
        ),
    )


#: Benchmark task 07's new requirement's title and description, fixed: the prompt states them.
BENCH_REQUIREMENT_TITLE = "Bench input report"
BENCH_REQUIREMENT_DESCRIPTION = "The component shall report the input it receives."
#: The source a requirement the architecture adds carries.
ARCHITECTURE_SOURCE = "architecture"


def add_requirement(model: BoundModel) -> Rendered | NotApplicable:
    """07: a new requirement at version 1, satisfied by the first require port of the largest component that has one.

    A model whose largest component provides only (a hub) still binds.
    """
    components = model.by_name("swc")
    ranked = model.locator.largest_components(len(components))
    found = next(
        (
            (component, port.name)
            for component in ranked
            for port in components[component].ports
            if port.direction == "require"
        ),
        None,
    )
    if found is None:
        return NotApplicable("no-require-port")
    component, port = found
    requirement = _free_name("SWR-BENCH-", model.names("requirements"), width=3)
    link = f"{requirement}@1"

    def right_run(root: Path) -> None:
        write_element(
            root,
            "requirements",
            Requirement.model_validate(
                {
                    "id": requirement,
                    "title": BENCH_REQUIREMENT_TITLE,
                    "description": BENCH_REQUIREMENT_DESCRIPTION,
                    "req_type": "req",
                    "version": 1,
                    "source": ARCHITECTURE_SOURCE,
                }
            ),
            model.registry,
        )
        write_element(root, "swc", _with_link(model, component, port, link), model.registry)

    return offer(
        model,
        Rendered(
            values={"requirement": requirement, "component": component, "port": port},
            expect=_change(
                [
                    {
                        "layer": "requirements",
                        "name": requirement,
                        "has": {"title": BENCH_REQUIREMENT_TITLE, "version": 1},
                    },
                    _has_port(component, {"name": port, "satisfies": [link]}),
                ]
            ),
            reference={
                "target": "the component with the most ports that has a require port (ties by name), its "
                "first require port"
            },
            right_run=right_run,
        ),
    )


def link_a_port_to_a_requirement(model: BoundModel) -> Rendered | NotApplicable:
    """23: link the largest component's first provide port not yet linked to it to task 18's requirement.

    The requirement is taken at its latest version; the port keeps the links it has.
    """
    found = _most_linked_requirement(model)
    if found is None:
        return NotApplicable("no-linked-requirement")
    requirement, _ = found
    version = max(requirement_versions(model.state["requirements"])[requirement])
    try:
        component = model.locator.largest_component()
    except LookupError:
        return NotApplicable("no-free-port")
    free = [
        port
        for port in model.by_name("swc")[component].ports
        if port.direction == "provide"
        and not any(not is_element_form(value) and parse_link(value).id == requirement for value in port.satisfies)
    ]
    if not free:
        return NotApplicable("no-free-port")
    port = free[0]
    link = f"{requirement}@{version}"

    def right_run(root: Path) -> None:
        write_element(root, "swc", _with_link(model, component, port.name, link), model.registry)

    return offer(
        model,
        Rendered(
            values={"component": component, "port": port.name, "requirement": requirement, "version": str(version)},
            expect=_change([_has_port(component, {"name": port.name, "satisfies": [*port.satisfies, link]})]),
            reference={
                "target": "task 18's requirement at its latest version; the first provide port of the component with "
                "the most ports not already linked to it",
            },
            right_run=right_run,
        ),
    )


# ---------------------------------------------------------------------------
# The change templates that move, remove or rename (04, 05, 06, 24).
# ---------------------------------------------------------------------------


def _entry(entry: Any) -> dict[str, str]:
    """A task's runnable entry, or a sequence step, in its own shape: ``swc`` or ``instance``, and ``runnable``."""
    selector = {"swc": entry.swc} if entry.swc is not None else {"instance": entry.instance}
    return {**selector, "runnable": entry.runnable}


def _resolver(model: BoundModel) -> Callable[[Any], str | None]:
    """The declared component an entry or a step names, by type or instance, across the composition layer."""
    components = model.by_name("swc")
    identities = identities_from(list(model.state["compositions"]))

    def component_of(entry: Any) -> str | None:
        component = identities.component_of(entry.swc, entry.instance, components)
        return None if component is None else component.name

    return component_of


def move_a_runnable(model: BoundModel) -> Rendered | NotApplicable:
    """04: move a runnable entry no sequence step names to another task of the same partition and period.

    The pair is the first, by partition then task names, where the source holds such an entry the
    target lacks and takes with no coexistence argument added (:func:`_takes`); the entry is moved in
    its own shape. A step names an entry when it names the same runnable of the same component, by
    type or instance, or states the same selector.
    """
    component_of = _resolver(model)
    steps = [step for sequence in model.state["sequences"] for step in sequence.steps]

    def sequenced(entry: Any) -> bool:
        component = component_of(entry)
        return any(
            step.runnable == entry.runnable
            and (_entry(step) == _entry(entry) or (component is not None and component_of(step) == component))
            for step in steps
        )

    levels = effective_asil_levels(list(model.state["swc"]), list(model.state["requirements"]))
    partitions: dict[str, list[Any]] = {}
    for name, task in sorted(model.by_name("tasks").items(), key=lambda item: item[0]):
        partitions.setdefault(task.partition, []).append(task)
    for partition in sorted(partitions, key=str):
        for source in partitions[partition]:
            for target in partitions[partition]:
                if target.name == source.name or target.period != source.period:
                    continue
                held = [_entry(entry) for entry in target.runnables]
                movable = [
                    entry
                    for entry in source.runnables
                    if _entry(entry) not in held
                    and not sequenced(entry)
                    and _takes(target, entry, component_of, levels)
                ]
                if movable:
                    return _moving(model, source, target, movable[0])
    return NotApplicable("no-task-pair")


def _takes(target: Any, entry: Any, component_of: Callable[[Any], str | None], levels: dict[str, str]) -> bool:
    """Whether *target* takes *entry* without ``safety.task-mixed-asil`` after the move.

    It declares a ``coexistence`` text, or its components with *entry*'s hold fewer than two
    effective levels by :func:`~swcraft_tools.safety.asil_rank` (``B`` and ``B(D)`` are one). Not
    reached: the other ``check-model`` rules, which :func:`offer` still asks.
    """
    if target.coexistence:
        return True
    components = {component_of(held) for held in [*target.runnables, entry]} - {None}
    return len({asil_rank(levels[component]) for component in components}) < 2


def _moving(model: BoundModel, source: Any, target: Any, entry: Any) -> Rendered | NotApplicable:
    moved = _entry(entry)
    # The prompt names the entry's own selector, as the assertion states it.
    component = moved.get("swc") or moved["instance"]

    def right_run(root: Path) -> None:
        kept = [held for held in source.runnables if _entry(held) != moved]
        write_element(root, "tasks", source.model_copy(update={"runnables": kept}), model.registry)
        write_element(
            root, "tasks", target.model_copy(update={"runnables": [*target.runnables, entry]}), model.registry
        )

    return offer(
        model,
        Rendered(
            values={
                "runnable": entry.runnable,
                "component": component,
                "from_task": source.name,
                "to_task": target.name,
            },
            expect=_change(
                [
                    {"layer": "tasks", "name": target.name, "has": {"runnables": [moved]}},
                    {"layer": "tasks", "name": source.name, "lacks": {"runnables": [moved]}},
                ]
            ),
            reference={
                "target": "the first pair of tasks, partitions then names in order, in one partition with one "
                "period, whose first task holds a runnable entry no sequence step names and the second task takes "
                "with no coexistence argument (it declares one, or the move leaves it one effective ASIL); that "
                "entry in its own shape",
            },
            right_run=right_run,
        ),
    )


def remove_a_component(model: BoundModel) -> Rendered | NotApplicable:
    """05: remove the ordinary component with the most connectors, with everything that refers to it.

    *Ordinary* is the passport's hub rule restated: at most twice the median component's port count.
    It is removed with its connectors, its composition members and their delegations, and every task
    entry and sequence step naming it. A reference the right run does not remove (an element-form
    link) makes the template ``right-run-refused``.
    """
    components = model.by_name("swc")
    if not components:
        return NotApplicable("no-ordinary-component")
    counts = [len(component.ports) for component in components.values()]
    threshold = 2 * statistics.median(counts)
    ordinary = [name for name in model.names("swc") if len(components[name].ports) <= threshold]
    if not ordinary:
        return NotApplicable("no-ordinary-component")
    removed = min(ordinary, key=lambda name: (-model.locator.connector_count(name), name))
    connectors = [
        name
        for name, connector in sorted(model.by_name("connectors").items(), key=lambda item: item[0])
        if removed in (connector.from_.swc, connector.to.swc)
    ]
    instances = sorted(
        {
            member.name
            for composition in model.by_name("compositions").values()
            for member in composition.members
            if member.type == removed
        },
        key=str,
    )
    component_of = _resolver(model)

    def right_run(root: Path) -> None:
        for layer, names in (("swc", [removed]), ("connectors", connectors)):
            stored = stored_element_files(root, layer, registry=model.registry)
            for name in names:
                for layer_file in stored.get(name, []):
                    layer_file.path.unlink()
        for composition in model.by_name("compositions").values():
            gone = {member.name for member in composition.members if member.type == removed}
            if gone:
                write_element(
                    root,
                    "compositions",
                    composition.model_copy(
                        update={
                            "members": [member for member in composition.members if member.name not in gone],
                            "delegations": [held for held in composition.delegations if held.member not in gone],
                        }
                    ),
                    model.registry,
                )
        for layer, field in (("tasks", "runnables"), ("sequences", "steps")):
            for element in model.by_name(layer).values():
                entries = getattr(element, field)
                kept = [entry for entry in entries if component_of(entry) != removed and entry.swc != removed]
                if len(kept) != len(entries):
                    write_element(root, layer, element.model_copy(update={field: kept}), model.registry)

    return offer(
        model,
        Rendered(
            values={"component": removed},
            expect=_change(
                [
                    {"layer": "swc", "name": removed, "absent": True},
                    *({"layer": "connectors", "name": name, "absent": True} for name in connectors),
                    *({"unreferenced": name} for name in dict.fromkeys([removed, *instances])),
                ]
            ),
            reference={
                "target": "of the components with at most twice the median component's ports, the one with the most "
                "connectors",
                "right_run": "the component, its connectors, its composition members and their delegations, and the "
                "task entries and sequence steps naming it, removed",
            },
            right_run=right_run,
        ),
    )


#: The prefix task 06's new data element name takes (``temperature`` -> ``benchTemperature``).
RENAME_PREFIX = "bench"


def rename_a_data_element(model: BoundModel) -> Rendered | NotApplicable:
    """06: rename the first data element of the most-named sender-receiver interface that has one.

    The new name is :data:`RENAME_PREFIX` and the old one capitalised. The rename also moves each
    runnable access, com spec and measure naming the element on a port of the interface; the
    assertions state each access and allow ``ports`` and ``runnables`` of those components to move.
    """
    interfaces = model.by_name("interfaces")
    ranked = [name for name in model.locator.heaviest_sender_receiver_interfaces() if interfaces[name].data_elements]
    if not ranked:
        return NotApplicable("no-sender-receiver-interface")
    interface = interfaces[ranked[0]]
    old = interface.data_elements[0].name
    new = _free_name(RENAME_PREFIX + old[:1].upper() + old[1:], [element.name for element in interface.data_elements])
    components = model.by_name("swc")
    owners = list(dict.fromkeys(component for component, _ in model.locator.ports_naming(interface.name)))
    renamed: dict[str, Any] = {}
    accesses: list[dict[str, Any]] = []
    for owner in owners:
        component = components[owner]
        on_interface = {port.name for port in component.ports if port.interface == interface.name}
        ports = [
            port.model_copy(
                update={
                    "com_specs": [
                        spec.model_copy(update={"data_element": new}) if spec.data_element == old else spec
                        for spec in port.com_specs
                    ],
                    "measures": {(new if key == old else key): text for key, text in port.measures.items()},
                }
            )
            if port.name in on_interface
            else port
            for port in component.ports
        ]
        runnables = []
        for runnable in component.runnables:
            stated: dict[str, list[dict[str, str]]] = {}
            changed: dict[str, list[Any]] = {}
            for field in ("reads", "writes"):
                refs = []
                for ref in getattr(runnable, field):
                    if ref.port in on_interface and ref.element == old:
                        ref = ref.model_copy(update={"element": new})
                        stated.setdefault(field, []).append({"port": ref.port, "element": new})
                    refs.append(ref)
                changed[field] = refs
            runnables.append(runnable.model_copy(update=changed))
            if stated:
                accesses.append(
                    {"layer": "swc", "name": owner, "has": {"runnables": [{"name": runnable.name, **stated}]}}
                )
        renamed[owner] = component.model_copy(update={"ports": ports, "runnables": runnables})
    elements = [
        element.model_copy(update={"name": new}) if element.name == old else element
        for element in interface.data_elements
    ]

    def right_run(root: Path) -> None:
        write_element(root, "interfaces", interface.model_copy(update={"data_elements": elements}), model.registry)
        for component in renamed.values():
            write_element(root, "swc", component, model.registry)

    return offer(
        model,
        Rendered(
            values={"element": old, "interface": interface.name, "new_name": new},
            expect=_change(
                [
                    {"layer": "interfaces", "name": interface.name, "has": {"data_elements": [{"name": new}]}},
                    {"layer": "interfaces", "name": interface.name, "lacks": {"data_elements": [{"name": old}]}},
                    *accesses,
                    *({"layer": "swc", "name": owner, "may_change": ["ports", "runnables"]} for owner in owners),
                ]
            ),
            reference={
                "target": "the most-named sender-receiver interface with a data element (Locator."
                "heaviest_sender_receiver_interfaces), its first element",
                "right_run": "the element renamed in the interface, and every runnable access, com spec and measure "
                "naming it on a port of the interface",
            },
            right_run=right_run,
        ),
    )


# ---------------------------------------------------------------------------
# The change templates of the access forms (35, 36).
# ---------------------------------------------------------------------------

#: The receive forms a read states besides by-argument (its ``by`` absent): ``DataReadRef.by``'s values.
READ_FORMS = ("value", "implicit")
#: Benchmark task 35's form.
IMPLICIT = "implicit"


def _read_entry(read: Any, **update: str) -> dict[str, str]:
    """A read as an assertion states it: port and element, and its form and name where stated."""
    entry = {"port": read.port, "element": read.element}
    for field in ("by", "name"):
        if getattr(read, field) is not None:
            entry[field] = getattr(read, field)
    return {**entry, **update}


def _implicit_read_admitted(component: Any, read: Any, interfaces: dict[str, Any]) -> bool:
    """Whether *read* of *component* takes the implicit form with no ``check-model`` rule refusing it.

    A require port on a sender-receiver interface holding the element; the element not ``queued``
    (``swc.access.queued-implicit-read``) and no ``enable_update: true`` for it
    (``swc.access.implicit-read-enable-update``). :func:`offer` asks the other rules.
    """
    port = next((port for port in component.ports if port.name == read.port), None)
    if port is None or port.direction != "require":
        return False
    interface = interfaces.get(port.interface)
    if not isinstance(interface, SRInterface):
        return False
    element = next((element for element in interface.data_elements if element.name == read.element), None)
    if element is None or element.sw_impl_policy == "queued":
        return False
    return not any(spec.data_element == read.element and spec.enable_update for spec in port.com_specs)


def make_a_read_implicit(model: BoundModel) -> Rendered | NotApplicable:
    """35: make one read implicit, the runnable's other reads left in their forms.

    Of the admitted reads not yet implicit, the one of the data element the most reads name (ties:
    row order). The assertions state every other read of the runnable as it is, lacking every form
    it does not state.
    """
    components = model.by_name("swc")
    interfaces = model.by_name("interfaces")
    counts: dict[tuple[str, str], int] = {}
    candidates: list[tuple[str, Any, Any, tuple[str, str]]] = []
    for name in model.names("swc"):
        component = components[name]
        port_interface = {port.name: port.interface for port in component.ports}
        for runnable in component.runnables:
            for read in runnable.reads:
                key = (port_interface.get(read.port, ""), read.element)
                counts[key] = counts.get(key, 0) + 1
                if read.by != IMPLICIT and _implicit_read_admitted(component, read, interfaces):
                    candidates.append((name, runnable, read, key))
    if not candidates:
        return NotApplicable("no-sender-receiver-read")
    owner, runnable, read, _ = min(enumerate(candidates), key=lambda indexed: (-counts[indexed[1][3]], indexed[0]))[1]
    others = [other for other in runnable.reads if (other.port, other.element) != (read.port, read.element)]
    component = components[owner]
    reads = [other.model_copy(update={"by": IMPLICIT}) if other is read else other for other in runnable.reads]
    changed = component.model_copy(
        update={
            "runnables": [
                held.model_copy(update={"reads": reads}) if held is runnable else held for held in component.runnables
            ]
        }
    )

    def right_run(root: Path) -> None:
        write_element(root, "swc", changed, model.registry)

    def runnable_check(check: str, entries: list[dict[str, str]]) -> dict[str, Any]:
        return {"layer": "swc", "name": owner, check: {"runnables": [{"name": runnable.name, "reads": entries}]}}

    lacking = [
        runnable_check("lacks", [_read_entry(other, by=form)])
        for other in others
        for form in READ_FORMS
        if form != other.by
    ]
    return offer(
        model,
        Rendered(
            values={"component": owner, "runnable": runnable.name, "element": read.element, "port": read.port},
            expect=_change(
                [
                    runnable_check("has", [_read_entry(read, by=IMPLICIT), *(_read_entry(other) for other in others)]),
                    *lacking,
                ]
            ),
            reference={
                "target": "of the reads not yet implicit on a require port of a sender-receiver interface, the element "
                "not queued and no enable_update stated for it on the port, the first in row order of the data element "
                "the most runnable reads name",
                "right_run": "the read's by set to implicit; the runnable's other reads as they are",
            },
            right_run=right_run,
        ),
    )


#: Benchmark task 36's callback runnable: ``On`` + the operation capitalised + this, the first free numeric suffix.
CALLBACK_SUFFIX = "Returned"
#: The event kind task 36's callback runnable carries.
CALL_RETURNED = "call_returned"


def call_asynchronously(model: BoundModel) -> Rendered | NotApplicable:
    """36: make one synchronous call asynchronous, and start a new runnable when it returns.

    The call is the first synchronous one in row order whose ``(port, operation)`` no other call of
    its component names, so neither ``swc.call.mixed-synchrony`` nor
    ``swc.call-returned-event.ambiguous-call`` can follow. The callback ``On{Operation}Returned`` is
    the call's result point, which a 4.2.2 project requires (``swc.call.async-without-result``).
    """
    components = model.by_name("swc")
    for owner in model.names("swc"):
        component = components[owner]
        pairs = [(call.port, call.operation) for runnable in component.runnables for call in runnable.calls]
        for runnable in component.runnables:
            for call in runnable.calls:
                if not call.asynchronous and pairs.count((call.port, call.operation)) == 1:
                    return _calling_asynchronously(model, component, runnable, call)
    return NotApplicable("no-synchronous-call")


def _calling_asynchronously(model: BoundModel, component: Any, runnable: Any, call: Any) -> Rendered | NotApplicable:
    operation = call.operation
    callback = _free_name(
        f"On{operation[:1].upper()}{operation[1:]}{CALLBACK_SUFFIX}", [held.name for held in component.runnables]
    )
    event = {"type": CALL_RETURNED, "port": call.port, "operation": operation}
    calls = [held.model_copy(update={"asynchronous": True}) if held is call else held for held in runnable.calls]
    new_runnable = type(runnable).model_validate({"name": callback, "events": [event]})
    changed = component.model_copy(
        update={
            "runnables": [
                *(
                    held.model_copy(update={"calls": calls}) if held is runnable else held
                    for held in component.runnables
                ),
                new_runnable,
            ]
        }
    )
    stated = {"port": call.port, "operation": operation, "asynchronous": True}
    if call.name is not None:
        stated["name"] = call.name

    def right_run(root: Path) -> None:
        write_element(root, "swc", changed, model.registry)

    def has_runnable(name: str, **fields: Any) -> dict[str, Any]:
        return {"layer": "swc", "name": component.name, "has": {"runnables": [{"name": name, **fields}]}}

    return offer(
        model,
        Rendered(
            values={
                "component": component.name,
                "runnable": runnable.name,
                "operation": operation,
                "port": call.port,
                "callback": callback,
            },
            expect=_change([has_runnable(runnable.name, calls=[stated]), has_runnable(callback, events=[event])]),
            reference={
                "target": "the first synchronous call in row order whose port and operation no other call of its "
                "component names",
                "right_run": "the call made asynchronous, its other fields kept; a new runnable of the component "
                "whose one event is the call_returned of the pair",
            },
            right_run=right_run,
        ),
    )


def _requirement_links(model: BoundModel, requirement: str) -> list[tuple[str, Any, tuple[str, str] | None, str]]:
    """Every link naming *requirement*, as ``(layer, element, owner, value)``.

    The ``satisfies`` members, with their owner (``("port", name)``, ``("runnable", name)`` or
    ``None``), then the requirement-form ``derived_from`` members, owner ``None``.
    """
    links = [
        (layer, element, site.owner, site.value)
        for layer, element, site in link_sites_of_state(model.state.layers)
        if not is_element_form(site.value) and parse_link(site.value).id == requirement
    ]
    links.extend(
        ("requirements", held, None, value)
        for held in model.state["requirements"]
        for value in held.derived_from
        if not is_element_form(value) and parse_link(value).id == requirement
    )
    return links


def _relinked(element: Any, owner: tuple[str, str] | None, field: str, old: str, new: str) -> Any:
    """*element* with *old* replaced by *new* in its own *field*, or in the ``satisfies`` of its member *owner*."""
    if owner is None:
        return element.model_copy(update={field: [new if value == old else value for value in getattr(element, field)]})
    members = "ports" if owner[0] == "port" else "runnables"
    return element.model_copy(
        update={
            members: [
                _relinked(member, None, "satisfies", old, new) if member.name == owner[1] else member
                for member in getattr(element, members)
            ]
        }
    )


def _link_assertion(layer: str, name: str, owner: tuple[str, str] | None, field: str, link: str) -> dict[str, Any]:
    if owner is None:
        return {field: [link]}
    return {"ports" if owner[0] == "port" else "runnables": [{"name": owner[1], "satisfies": [link]}]}


def confirm_links_at_a_new_version(model: BoundModel) -> Rendered | NotApplicable:
    """24: a requirement moves to its next version, and every link to it is confirmed there.

    The requirement is the declared-once, non-stub one with the most link sites, every link to it
    at its latest version (ties: the id).
    """
    sites = _requirement_sites(model)
    declared = {}
    for held in model.state["requirements"]:
        declared[held.id] = declared.get(held.id, 0) + 1
    candidates = []
    for requirement in _declared_requirements(model):
        if not sites.get(requirement) or declared[requirement] != 1:
            continue
        latest = max(requirement_versions(model.state["requirements"])[requirement])
        links = _requirement_links(model, requirement)
        if all(value == f"{requirement}@{latest}" for _, _, _, value in links):
            candidates.append(requirement)
    if not candidates:
        return NotApplicable("no-linked-requirement")
    requirement = min(candidates, key=lambda rid: (-len(sites[rid]), rid))
    held = model.by_name("requirements")[requirement]
    following = held.version + 1
    old, new = f"{requirement}@{held.version}", f"{requirement}@{following}"
    links = _requirement_links(model, requirement)
    assertions: list[dict[str, Any]] = [{"layer": "requirements", "name": requirement, "has": {"version": following}}]
    for layer, element, owner, _ in links:
        field = "derived_from" if layer == "requirements" else "satisfies"
        name = str(getattr(element, model.registry.layer(layer).identity_field))
        assertion = {
            "layer": layer,
            "name": name,
            "has": _link_assertion(layer, name, owner, field, new),
            "lacks": _link_assertion(layer, name, owner, field, old),
        }
        if assertion not in assertions:
            assertions.append(assertion)
    assertions.append({"unreferenced": old})

    def right_run(root: Path) -> None:
        relinked: dict[tuple[str, str], tuple[str, Any]] = {}
        for layer, element, owner, _ in links:
            field = "derived_from" if layer == "requirements" else "satisfies"
            key = (layer, str(getattr(element, model.registry.layer(layer).identity_field)))
            current = relinked.get(key, (layer, element))[1]
            relinked[key] = (layer, _relinked(current, owner, field, old, new))
        key = ("requirements", requirement)
        current = relinked.get(key, ("requirements", held))[1]
        relinked[key] = ("requirements", current.model_copy(update={"version": following}))
        for layer, element in relinked.values():
            write_element(root, layer, element, model.registry)

    return offer(
        model,
        Rendered(
            values={"requirement": requirement, "next": str(following)},
            expect=_change(assertions),
            reference={
                "target": "the declared, non-stub requirement declared once with the most link sites, every link to it "
                "at its latest version",
                "right_run": "the version raised by one, every satisfies and derived_from link rewritten to it",
            },
            right_run=right_run,
        ),
    )


# ---------------------------------------------------------------------------
# The import template (10) and its generated export.
# ---------------------------------------------------------------------------

#: Where task 10's export lands in the project, relative to the manifest.
IMPORTS_DIR = "imports"
#: Benchmark task 10's new component's stems: its name, its require port and its timing runnable.
IMPORT_COMPONENT_STEM = "BenchImportedController"
IMPORT_PORT_STEM = "rBenchImported"
IMPORT_RUNNABLE = "UpdateBenchImported"
#: Benchmark task 10's runnable's period in seconds.
IMPORT_PERIOD = 0.05
IMPORT_CONNECTOR_STEM = "BenchImportedLink"
#: What the new component needs of the first RAM by name, in bytes; every other RAM gets 0.
IMPORT_RAM_BYTES = 512


class ExportRefused(Exception):
    """The product's component export refused (``generate arxml --component`` exited non-zero).

    Its text is withheld: the export's own message names elements of the model.
    """


def write_component_export(project_manifest: Path, component_yaml: str, group_dir: str, name: str) -> bytes:
    """The component export of *name* from a scratch copy of the project with *component_yaml* added.

    *group_dir* is an existing swc group directory, POSIX-spelt relative to the model root
    (``swc/generated``). Returns the bytes of ``swcraft generate arxml --component``; the project is
    never written. Raises :class:`ExportRefused` when the product refuses the export.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
        copy = Path(scratch) / "project"
        copy_project(project_manifest, copy)
        component = copy / SCRATCH_MODEL_ROOT / Path(*group_dir.split("/")) / f"{name}.yaml"
        component.write_text(component_yaml, encoding="utf-8", newline="\n")
        out = Path(scratch) / f"{name}.arxml"
        completed = subprocess.run(
            [sys.executable, "-m", "swcraft_tools.cli", "generate", "arxml",
             "--project", (copy / MANIFEST).as_posix(), "--component", name, "--output", out.as_posix()],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )  # fmt: skip
        if completed.returncode != 0 or not out.is_file():
            raise ExportRefused
        return out.read_bytes()


def _memory_clause(rams: list[str]) -> str:
    """The prompt's sentence of what the new component needs of the RAMs, empty when there is none."""
    if not rams:
        return ""
    clause = f"It needs {IMPORT_RAM_BYTES} bytes of {rams[0]}"
    rest = rams[1:]
    if rest:
        listed = rest[0] if len(rest) == 1 else f"{', '.join(rest[:-1])} or {rest[-1]}"
        clause += f" and none of {listed}"
    return clause + ". "


def import_a_component(model: BoundModel) -> Rendered | NotApplicable:
    """10: import a generated component export, budget its RAM, add it to a composition and connect it.

    The provider is the first component, on the most-named sender-receiver interface that has one,
    with a provide port on it and a composition holding it. The export is a new component with one
    require port on the interface, written into the provider's group directory; it reaches the agent
    as ``imports/<name>.arxml``. A component export carries no memory budget, so the prompt states
    :data:`IMPORT_RAM_BYTES` of the first RAM and none of the others. The right run imports through
    the product's importer, then writes the budget, the member and the connector.
    """
    components = model.by_name("swc")
    held = _compositions_holding(model)
    target: tuple[str, str, str, str] | None = None
    for interface in model.locator.heaviest_sender_receiver_interfaces():
        for component in sorted({owner for owner, _ in model.locator.ports_naming(interface)}, key=str):
            ports = [
                port.name
                for port in components[component].ports
                if port.direction == "provide" and port.interface == interface
            ]
            if ports and held.get(component):
                target = (interface, component, ports[0], sorted(held[component], key=str)[0])
                break
        if target is not None:
            break
    if target is None:
        return NotApplicable("no-provide-port-in-a-composition")
    interface, provider, provider_port, composition = target
    name = _free_name(IMPORT_COMPONENT_STEM, model.names("swc"))
    port = IMPORT_PORT_STEM
    provider_element = components[provider]
    component_yaml = element_text(
        SWComponent.model_validate(
            {
                "name": name,
                "type": "application",
                "ports": [{"name": port, "direction": "require", "interface": interface}],
                "runnables": [{"name": IMPORT_RUNNABLE, "events": [{"type": "timing", "period": IMPORT_PERIOD}]}],
                "data_type_mappings": list(provider_element.data_type_mappings),
                "handle_termination_and_restart": "no_support",
            }
        )
    )
    provider_file = stored_element_files(model.model_root, "swc", registry=model.registry)[provider][0].path
    group_dir = provider_file.parent.relative_to(model.model_root).as_posix()
    try:
        export = write_component_export(model.manifest, component_yaml, group_dir, name)
    except ExportRefused:
        return NotApplicable("export-refused")
    file_name = f"{name}.arxml"
    rams = sorted(
        (memory.name for memory in model.by_name("hardware").values() if isinstance(memory, Memory)
         and memory.type == RAM_TYPE),
        key=str,
    )  # fmt: skip
    budget = {ram: IMPORT_RAM_BYTES if index == 0 else 0 for index, ram in enumerate(rams)}
    members = model.by_name("compositions")[composition].members
    member = _free_name(name, [held_member.name for held_member in members])
    connector = _free_name(IMPORT_CONNECTOR_STEM, model.names("connectors"))
    ends = {"from": {"swc": provider, "port": provider_port}, "to": {"swc": name, "port": port}}

    def right_run(root: Path) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
            source = Path(scratch) / file_name
            source.write_bytes(export)
            import_component(source=source.as_posix(), model_root=root.as_posix())
        imported = {element.name: element for element in load_model_state(root, registry=model.registry)["swc"]}
        write_element(root, "swc", imported[name].model_copy(update={"memory": budget}), model.registry)
        held_composition = model.by_name("compositions")[composition]
        entry = CompositionMember.model_validate({"name": member, "type": name})
        write_element(
            root,
            "compositions",
            held_composition.model_copy(update={"members": [*held_composition.members, entry]}),
            model.registry,
        )
        write_element(
            root,
            "connectors",
            Connector.model_validate({"name": connector, "composition": composition, **ends}),
            model.registry,
        )

    has: dict[str, Any] = {
        "ports": [{"name": port, "direction": "require", "interface": interface}],
        "runnables": [{"name": IMPORT_RUNNABLE}],
    }
    if budget:
        has["memory"] = {rams[0]: IMPORT_RAM_BYTES}
    return offer(
        model,
        Rendered(
            values={
                "file": file_name,
                "memory_clause": _memory_clause(rams),
                "composition": composition,
                "provider": provider,
                "provider_port": provider_port,
                "port": port,
            },
            expect=_change(
                [
                    {"layer": "swc", "name": name, "has": has},
                    {"layer": "compositions", "name": composition, "has": {"members": [{"type": name}]}},
                    {"layer": "connectors", "has": ends},
                ]
            ),
            reference={
                "target": "the first of the most-named sender-receiver interfaces with a provide port on a component "
                "that is a member of a composition; that component, its first provide port on the interface and "
                "the first composition holding it",
                "export": "a new component with one require port on the interface, one timing runnable and the "
                "provider's mapping sets, exported by generate arxml --component from a scratch copy",
                "right_run": "the product's component import, then the RAM budget, the composition member and "
                "the connector",
            },
            right_run=right_run,
            inputs={f"{IMPORTS_DIR}/{file_name}": export},
        ),
    )


#: Every binding a template may name, by name.
BINDINGS: dict[str, Binding] = {
    "users_of_the_heaviest_interface": users_of_the_heaviest_interface,
    "ports_of_the_largest_component": ports_of_the_largest_component,
    "components_on_the_heaviest_interface": components_on_the_heaviest_interface,
    "unconnected_component_ports": unconnected_component_ports,
    "components_in_the_busiest_partition": components_in_the_busiest_partition,
    "tasks_on_the_busiest_core": tasks_on_the_busiest_core,
    "elements_satisfying_the_most_linked_requirement": elements_satisfying_the_most_linked_requirement,
    "requirements_about_the_commonest_topic": requirements_about_the_commonest_topic,
    "ram_left_on_a_core": ram_left_on_a_core,
    "components_at_the_commonest_level": components_at_the_commonest_level,
    "largest_consumer_of_the_busiest_memory": largest_consumer_of_the_busiest_memory,
    "unallocated_requirements": unallocated_requirements,
    "links_needing_confirmation": links_needing_confirmation,
    "candidates_for_a_requirement": candidates_for_a_requirement,
    "connect_two_components": connect_two_components,
    "new_interface_and_type": new_interface_and_type,
    "add_requirement": add_requirement,
    "link_a_port_to_a_requirement": link_a_port_to_a_requirement,
    "move_a_runnable": move_a_runnable,
    "remove_a_component": remove_a_component,
    "rename_a_data_element": rename_a_data_element,
    "confirm_links_at_a_new_version": confirm_links_at_a_new_version,
    "import_a_component": import_a_component,
    "make_a_read_implicit": make_a_read_implicit,
    "call_asynchronously": call_asynchronously,
}


# ---------------------------------------------------------------------------
# Templates and rendering.
# ---------------------------------------------------------------------------


def _check_template(name: str, template: Any) -> dict[str, Any]:
    if not isinstance(template, dict):
        raise TemplateError(f"template {name} is not a JSON object")
    unknown = sorted(set(template) - set(TEMPLATE_KEYS), key=str)
    missing = [key for key in TEMPLATE_KEYS if key != "note" and key not in template]
    if unknown or missing:
        raise TemplateError(f"template {name}: unknown keys {unknown}, missing keys {missing}")
    if template["id"] != name:
        raise TemplateError(f"template {name} carries the id {template['id']!r}")
    if template["kind"] not in KINDS:
        raise TemplateError(f"template {name} has kind {template['kind']!r}, not one of {KINDS}")
    if template["oracle"] not in ORACLES:
        raise TemplateError(f"template {name} has oracle {template['oracle']!r}, not one of {ORACLES}")
    if template["binding"] not in BINDINGS:
        raise TemplateError(f"template {name} names no binding of BINDINGS: {template['binding']!r}")
    return template


def load_templates(directory: Path = TEMPLATES_DIR) -> list[dict[str, Any]]:
    """Every template under *directory*, in file-name order, each refused when it cannot be bound."""
    templates = []
    for path in sorted(directory.glob("*.json"), key=lambda path: path.as_posix()):
        try:
            template = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise TemplateError(f"template {path.stem} cannot be read: {type(exc).__name__}") from None
        templates.append(_check_template(path.stem, template))
    return templates


def bind(model: BoundModel, template: dict[str, Any]) -> Rendered | NotApplicable:
    """*template* bound to *model*.

    A change template on a model whose ``check-model`` reports an error is ``baseline-has-errors``
    before its binding runs: no change set commits there, a correct agent's included.
    """
    if template["kind"] == "change" and model.baseline_error_count:
        return NotApplicable("baseline-has-errors")
    result = BINDINGS[template["binding"]](model)
    if isinstance(result, Rendered):
        result = _prose_safe(template, result)
    return result


def _expected_names(answer: dict[str, Any]) -> list[str]:
    """Every name a right reply to *answer* holds."""
    names = list(answer.get("mentions_all", []))
    for site in [*answer.get("with_layer", []), *answer.get("with_owner", [])]:
        names += [value for value in (site.get("name"), site.get("owner")) if value]
    if "first_of" in answer:
        names.append(answer["first_of"]["expected"])
    return names


#: The bindings whose ``first_of`` candidates are model names, which :func:`_prose_safe` narrows;
#: 09's are the prompt's own answer words and stay.
NAMED_CANDIDATES = frozenset({"largest_consumer_of_the_busiest_memory"})


def _prose_safe(template: dict[str, Any], rendered: Rendered) -> Rendered:
    """*rendered* without the negative names a right reply can hold.

    Left out: every ``mentions_none`` name, and every losing ``first_of`` candidate of a
    :data:`NAMED_CANDIDATES` binding, that a right reply can hold. A right reply repeats the prompt's
    words, states layers and writes ordinary prose, so a negative name that is a word of the rendered
    prompt, a prose word (:func:`_prose_word`) or one the checker reads as an expected name would fail
    it. With nothing left a ``mentions_none`` goes; a
    ``first_of`` always keeps its expected name. Not reached: a prose word :func:`_prose_word` does
    not take (``iPhone``), and the ``first_of`` candidates of a binding outside
    :data:`NAMED_CANDIDATES`, which are answer words by design (09's ``yes`` and ``no``).
    """
    answer = rendered.expect.get("answer")
    if not isinstance(answer, dict):
        return rendered
    prompt = CHECK._bare(fill(template["prompt"], rendered.values))
    expected = {CHECK._bare(name).casefold() for name in _expected_names(answer)}

    def safe(name: str) -> bool:
        return (
            CHECK._bare(name).casefold() not in expected
            and not _prose_word(name)
            and CHECK._named(name).search(prompt) is None
        )

    narrowed = dict(answer)
    if answer.get("mentions_none"):
        kept = [name for name in answer["mentions_none"] if safe(name)]
        del narrowed["mentions_none"]
        if kept:
            narrowed["mentions_none"] = kept
    if "first_of" in answer and template["binding"] in NAMED_CANDIDATES:
        rule = answer["first_of"]
        candidates = [name for name in rule["candidates"] if name == rule["expected"] or safe(name)]
        narrowed["first_of"] = {**rule, "candidates": candidates}
    if narrowed == answer:
        return rendered
    return dataclasses.replace(rendered, expect={**rendered.expect, "answer": narrowed})


def bind_all(model: BoundModel, templates: list[dict[str, Any]] | None = None) -> dict[str, Rendered | NotApplicable]:
    """Every template (the committed directory's by default) bound to *model*, by template id."""
    return {
        template["id"]: bind(model, template) for template in (load_templates() if templates is None else templates)
    }


def render_task(template: dict[str, Any], rendered: Rendered) -> dict[str, Any]:
    """The task file of *template* bound as *rendered*: the committed format plus ``set`` and ``oracle``.

    Raises ``TemplateError`` when the binding's values do not fill the prompt's placeholders exactly.
    """
    wanted = placeholders_in(template["prompt"])
    if wanted != set(rendered.values):
        raise TemplateError(
            f"template {template['id']}: the binding fills {sorted(rendered.values, key=str)}, "
            f"the prompt names {sorted(wanted, key=str)}"
        )
    task: dict[str, Any] = {
        "id": template["id"],
        "task": template["task"],
        "group": template["group"],
        "kind": template["kind"],
        "status": "ready",
        "prompt": fill(template["prompt"], rendered.values),
        "expect": rendered.expect,
        "reference": rendered.reference,
    }
    if "note" in template:
        task["note"] = template["note"]
    task["set"] = ANY_SET
    task["oracle"] = template["oracle"]
    return task
