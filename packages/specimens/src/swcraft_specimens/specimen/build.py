"""The specimen builder: the parts assembled into one project, rendered, self-checked and published.

`build_project` assembles the parts in dependency order: the shape (`shape.py`), the hardware
counts, the identifier plan (`identifiers.py`), then the interfaces, the types, the type layers,
the ports, the connectors, the compositions (a chain: the root and `layers.compositions` - 1 nested
ones), the delegations and the hardware under those names. The ports, connectors, compositions and
delegations execute the shape's layout (`layout.Layout`) and search nothing. Every component sits
in the one domain `specimen`; its `R1` reads and writes the data elements of its sender-receiver
ports (`accesses.py`). The requirements are planned over the built sites and their sentences
become the components' descriptions.

`render_specimen` runs the full pipeline: the model check under the specimen's identifier limits,
the emitter, the contract, the rendered-tree oracle and the passport pass.

`build_specimen_files` refuses before generation what the feasibility check refuses, renders, then
self-checks: every stated leaf but the version leaves is compared with the passport of the rendered
documents. A miss is refused at the leaf (class `missed`, exit 2) and nothing is written; nothing is
approximated. `layout.check_layout` then re-reads the layout from the built project; a difference
is a `GenerationError`. Only then is the sidecar built, and the whole tree goes through the oracle
and the sidecar's validator once more before `generate_specimen` publishes it.

What this does not reach: the self-check judges the stated leaves by the generator's own pass;
that the pass agrees with SWCraft's `swcraft passport` is for the recorded fixtures and the
external gate to show.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contract import validate_contract
from ..emit import build_file_map, materialize
from ..errors import GenerationError
from ..model import (
    AnyInterface,
    Component,
    Domain,
    ExternalInterface,
    Port,
    Project,
    Runnable,
    ValueType,
    validate_project,
)
from ..passport import MISSED, PassportRefused, Refusal, RenderedDocuments, compute_passport
from ..sidecar import (
    build_specimen_document,
    generation_json_bytes,
    project_tree_sha256,
    validate_specimen_sidecar,
)
from ..validate import validate_file_map
from .accesses import data_accesses
from .common import hub_count, stated_count
from .components import build_components
from .compositions import build_compositions
from .connectors import build_connectors, construction_spec
from .delegations import build_delegations
from .feasibility import check_feasibility
from .hardware import build_hardware, memory_budgets, rate_partitions
from .identifiers import (
    SPECIMEN_LIMITS,
    hardware_names_by_kind,
    identifier_counts,
    identifier_names,
)
from .interfaces import build_interfaces, connectable_fan_outs
from .layout import NOTICE, check_layout
from .leaves import LEAVES, stated_leaves
from .preset import SpecimenPreset
from .requirements import (
    SiteCounts,
    build_requirements,
    descriptions,
    plan_requirements,
    requirement_counts,
)
from .shape import Shape, specimen_shape
from .tasks import TaskComponent, build_tasks, busy_tasks
from .types import build_types
from .vocabulary import TASKS_IDLE, VERSION

#: The one domain every specimen component sits in.
DOMAIN = "specimen"


def _value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True)


def self_check(stated: Mapping[str, Any], generated: Mapping[str, Any]) -> list[Refusal]:
    """The `missed` refusals of a generated passport against the stated one, in leaf-table order.

    Every stated leaf but the version leaves (any `1.x.y` and any `12.x.y` is accepted, while the
    pass states its own) must equal the generated value; numbers compare by value, so a neutral
    mean stated as `0.0` meets the pass's int `0`.
    """
    wanted = stated_leaves(stated)
    actual = stated_leaves(generated)
    refusals: list[Refusal] = []
    for path in LEAVES:
        if path not in wanted or LEAVES[path].leaf_class == VERSION:
            continue
        if path not in actual or actual[path] != wanted[path]:
            generated_value = _value(actual[path]) if path in actual else "nothing"
            refusals.append(
                Refusal(
                    path,
                    MISSED,
                    f"configured {_value(wanted[path])}, generated {generated_value}",
                )
            )
    return refusals


@dataclass(frozen=True)
class RenderedSpecimen:
    """A specimen through the full pipeline.

    The project, its checked file map, the documents the oracle parsed and the passport the pass
    computes over them.
    """

    project: Project
    file_map: dict[str, bytes]
    documents: RenderedDocuments
    passport: dict[str, Any]


def _feasible_shape(stated: Mapping[str, Any]) -> Shape:
    """The shape of stated leaves the feasibility check accepted (cached on the passport).

    A refusal here is a defect and a `GenerationError`.
    """
    shape = specimen_shape(stated)
    if not isinstance(shape, Shape):
        raise GenerationError("the shape was refused after feasibility")
    return shape


def _runnables(
    component: str,
    count: int,
    periods: Mapping[tuple[str, str], int],
    ports: Sequence[Port],
    interfaces: Mapping[str, AnyInterface],
) -> tuple[Runnable, ...]:
    """A component's runnables `R1 ... R<count>`, each at its task's period.

    `R1` carries the component's data accesses.
    """
    accesses = data_accesses(ports, interfaces)
    return tuple(
        Runnable(
            f"R{index}",
            periods.get((component, f"R{index}")),
            writes=accesses.writes if index == 1 else (),
            reads=accesses.reads if index == 1 else (),
        )
        for index in range(1, count + 1)
    )


def build_project(preset: SpecimenPreset) -> Project:
    """The project of a feasible specimen preset (refused passports are the caller's to stop)."""
    stated = stated_leaves(preset.passport)
    shape = _feasible_shape(stated)
    shapes = build_components(stated, shape.counts)
    counts = identifier_counts(stated, shape.connectors)
    names = identifier_names(stated, counts)
    plan = build_interfaces(stated, shape.fan_outs, names)
    types = build_types(
        stated,
        [data_type for data_type in plan.data_types if isinstance(data_type, ValueType)],
        names,
    )
    component_names = names["swc"]
    # The construction executes the layout the shape decided, with no search. With no delegation
    # the layout is the one the connector search decided, every component a root member.
    layout = shape.layout
    connectable = len(connectable_fan_outs(stated, plan.fan_outs))
    solution = layout.solution(shape.counts, plan.fan_outs[:connectable])
    wiring = build_connectors(
        construction_spec(stated),
        shape.counts,
        plan.fan_outs,
        plan.interfaces[: len(plan.fan_outs)],
        component_names,
        names["connectors"],
        shape.search,
        connectable=connectable,
        unconnected=layout.unconnected,
        solution=solution,
    )
    hardware = build_hardware(stated, hardware_names_by_kind(names, counts))
    tasks = build_tasks(
        [
            TaskComponent(name, component_shape.runnables, component_shape.level)
            for name, component_shape in zip(component_names, shapes, strict=True)
        ],
        busy_tasks(stated),
        stated_count(stated, TASKS_IDLE),
        hardware.partitions,
        names["tasks"],
        names["sequences"],
    )
    sites = SiteCounts(
        tuple(len(ports) for ports in wiring.ports),
        tuple(component_shape.runnables for component_shape in shapes),
        len(plan.interfaces),
        len(tasks.tasks),
    )
    requirement_plan = plan_requirements(requirement_counts(stated), sites)
    texts = descriptions(requirement_plan)
    core_of = {component: task.core for task in tasks.tasks for component, _ in task.runnables}
    budgets, memories = memory_budgets(
        hardware,
        [core_of.get(name) for name in component_names],
        hub_count([len(ports) for ports in wiring.ports]),
    )
    interfaces_by_name = {interface.name: interface for interface in plan.interfaces}
    compositions = build_compositions(
        names["compositions"],
        [component_names[a] for a in layout.root],
        [component_names[a] for a in layout.deep],
    )
    if layout.delegations:
        compositions = build_delegations(
            compositions,
            component_names,
            wiring.ports,
            wiring.open_ports,
            layout,
            wiring.centres,
            frozenset(i.name for i in plan.interfaces if isinstance(i, ExternalInterface)),
        )
    components = tuple(
        Component(
            name,
            DOMAIN,
            component_shape.level,
            ports,
            _runnables(name, component_shape.runnables, tasks.periods, ports, interfaces_by_name),
            budget,
            text,
        )
        for name, component_shape, ports, budget, text in zip(
            component_names, shapes, wiring.ports, budgets, texts, strict=True
        )
    )
    requirements = build_requirements(
        requirement_plan,
        names["requirements"],
        component_names,
        [[port.name for port in ports] for ports in wiring.ports],
        [component_shape.runnables for component_shape in shapes],
        [interface.name for interface in plan.interfaces],
        compositions[0].name,
        [task.name for task in tasks.tasks],
        hardware.processor.name,
    )
    return Project(
        preset.name,
        hardware.processor,
        hardware.cores,
        memories,
        (Domain(DOMAIN, tuple(component_names)),),
        components,
        plan.data_types,
        plan.interfaces,
        wiring.connectors,
        requirements,
        tasks.tasks,
        compositions,
        partitions=rate_partitions(hardware.partitions, tasks.partition_levels),
        units=types.units,
        compu_methods=types.compu_methods,
        data_constraints=types.data_constraints,
        application_types=types.application_types,
        mapping_sets=types.mapping_sets,
        sequences=tasks.sequences,
    )


def render_specimen(preset: SpecimenPreset) -> RenderedSpecimen:
    """The full pipeline over the project of a feasible specimen preset."""
    project = build_project(preset)
    validate_project(project, SPECIMEN_LIMITS)
    file_map = build_file_map(project)
    validate_contract(file_map)
    documents = validate_file_map(file_map, project, task_overhead_us=0)
    return RenderedSpecimen(project, file_map, documents, compute_passport(documents))


def build_specimen_files(preset: SpecimenPreset, notify: Callable[[str], None] | None = None) -> dict[str, bytes]:
    """The complete file map of a specimen, `generation.json` included.

    Refused before generation by the feasibility check, refused in generation by the self-check,
    and checked again whole, sidecar and all, before it is returned. `notify` is given one notice
    line, at most once, when the entry planner starts its exact program.
    """
    if notify is None:
        return _build_specimen_files(preset)
    told = False

    def notice(text: str) -> None:
        nonlocal told
        if not told:
            told = True
            notify(f"swcraft-specimens: specimen {preset.name}: {text}")

    token = NOTICE.set(notice)
    try:
        return _build_specimen_files(preset)
    finally:
        NOTICE.reset(token)


def _build_specimen_files(preset: SpecimenPreset) -> dict[str, bytes]:
    refusals = check_feasibility(preset.passport)
    if refusals:
        raise PassportRefused(refusals)
    rendered = render_specimen(preset)
    missed = self_check(preset.passport, rendered.passport)
    if missed:
        raise PassportRefused(missed)
    # What the pass cannot see (which component holds which delegation-only port, the membership,
    # the sources) is re-read from the project.
    check_layout(rendered.project, _feasible_shape(stated_leaves(preset.passport)).layout)
    sidecar = generation_json_bytes(
        build_specimen_document(preset, rendered.project, project_tree_sha256(rendered.file_map), rendered.passport)
    )
    full_map = dict(rendered.file_map)
    full_map["generation.json"] = sidecar
    # The full tree is parsed again, independently of the parse the pass read, and the sidecar's
    # passport is recomputed against it.
    documents = validate_file_map(full_map, rendered.project, task_overhead_us=0)
    validate_specimen_sidecar(sidecar, preset, rendered.project, full_map, documents=documents)
    return full_map


def generate_specimen(preset: SpecimenPreset, output: Path, notify: Callable[[str], None] | None = None) -> None:
    """Generate a specimen and publish it, tree and sidecar, transactionally.

    A refusal writes nothing. `notify` as in `build_specimen_files`.
    """
    materialize(build_specimen_files(preset, notify), output)
