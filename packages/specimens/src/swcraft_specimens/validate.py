"""Generation orchestration and rendered-tree validation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

import yaml

from .architecture import Architecture, validate_architecture
from .contract import validate_contract
from .emit import (
    MICROSECOND_SUFFIX,
    STORAGE_DIRECTORIES,
    data_type_directory,
    satisfies_entry,
    type_layer_documents,
)
from .errors import GenerationError
from .metrics import build_metrics
from .model import (
    COMPOSITION_SITE,
    HARDWARE_SITE,
    INTERFACE_SITE,
    SWC_SITE,
    TASK_SITE,
    Project,
    hardware_names,
    partitions,
    task_partition,
    validate_project,
)
from .passport import PASSPORT_LAYERS, RenderedDocuments
from .placement import PlacementResult, validate_placement
from .preset import ASIL, Preset
from .sidecar import validate_sidecar
from .toolchain import is_exact_release

_MICROSECONDS = re.compile(rf"-?[0-9]+{MICROSECOND_SUFFIX}")


def _microseconds(value: Any, what: str, path: str) -> int:
    if not isinstance(value, str) or not _MICROSECONDS.fullmatch(value):
        raise GenerationError(f"{what} {value!r} is not a whole number of microseconds: {path}")
    return int(value[: -len(MICROSECOND_SUFFIX)])


def _name(value: Any, what: str, path: str) -> str:
    """Return a rendered name used as a set or dictionary key; any non-string is refused.

    The contract gate checks keys, not value types: a list or a mapping here would otherwise be
    an uncaught ``TypeError`` (unhashable)."""
    if not isinstance(value, str):
        raise GenerationError(f"rendered {what} {value!r} is not a string: {path}")
    return value


def _optional_name(value: Any, what: str, path: str) -> str | None:
    """Like ``_name``, for a reference that may be absent."""
    return None if value is None else _name(value, what, path)


def validate_file_map(file_map: Mapping[str, bytes], project: Project, *, task_overhead_us: int) -> RenderedDocuments:
    """Check the rendered tree; ``task_overhead_us`` is the preset's, for the task budgets.

    Return the documents this check parsed, per passport layer, each layer's in path order, so
    the passport pass reads them without parsing the tree a second time."""
    paths = list(file_map)
    folded: dict[str, str] = {}
    for path in paths:
        p = PurePosixPath(path)
        if p.is_absolute() or ".." in p.parts or path != p.as_posix():
            raise GenerationError(f"rendered file path is not relative POSIX: {path}")
        if path.casefold() in folded:
            raise GenerationError(f"case-only path collision: {folded[path.casefold()]} and {path}")
        folded[path.casefold()] = path
        if path != "generation.json":
            try:
                text = file_map[path].decode("utf-8")
            except UnicodeDecodeError:
                raise GenerationError(f"rendered file is not UTF-8: {path}") from None
            if "\r" in text:
                raise GenerationError(f"rendered file has CRLF: {path}")
            if "&id" in text or "*id" in text:
                raise GenerationError(f"YAML alias is forbidden: {path}")
    if "swcraft.yaml" not in file_map:
        raise GenerationError("rendered tree lacks swcraft.yaml")
    validate_contract(file_map)
    if "generation.json" in file_map and "model/generation.json" in file_map:
        raise GenerationError("generation.json must be outside model/")
    manifest = _load_yaml(file_map["swcraft.yaml"], "swcraft.yaml")
    # An absent `toolchain_version` pins nothing; a present one must be one exact release.
    if manifest.get("schema_version") != "1.0" or (
        "toolchain_version" in manifest and not is_exact_release(manifest["toolchain_version"])
    ):
        raise GenerationError("swcraft.yaml manifest version mismatch")
    for layer in STORAGE_DIRECTORIES:
        prefix = f"model/{layer}/"
        if not any(path.startswith(prefix) for path in paths):
            raise GenerationError(f"rendered tree lacks layer {layer}")
    expected = _expected_files(project)
    # Every YAML file under model/ belongs to a layer that has expected elements; a file in an
    # empty storage directory or outside every storage directory is something nothing writes.
    for path in paths:
        if path.startswith("model/") and path.endswith(".yaml"):
            if not any(path.startswith(f"model/{layer}/") for layer in expected):
                raise GenerationError(f"rendered file outside every emitted layer: {path}")
    parsed: dict[str, dict[str, Any]] = {}
    for layer, layer_expected in expected.items():
        actual = {path for path in paths if path.startswith(f"model/{layer}/") and path.endswith(".yaml")}
        if actual != set(layer_expected):
            raise GenerationError(f"rendered {layer} file map mismatch")
        for path, identity in layer_expected.items():
            data = _load_yaml(file_map[path], path)
            parsed[path] = data
            key = "id" if layer == "requirements" else "name"
            if data.get(key) != identity:
                raise GenerationError(f"filename/identity mismatch: {path} -> {data.get(key)}")
            if (
                layer == "swc"
                and not project.emit_runnable_wcet
                and any(key in runnable for runnable in data.get("runnables", []) for key in ("wcet", "wcet_type"))
            ):
                raise GenerationError(f"runnable wcet emitted while emit_runnable_wcet=false: {path}")
    for path in paths:
        if path.endswith(".gitkeep"):
            # The rule covers every storage directory: a .gitkeep sits only in an empty one.
            layer = path[len("model/") : -len("/.gitkeep")] if path.startswith("model/") else ""
            if layer not in STORAGE_DIRECTORIES:
                raise GenerationError(f"unexpected .gitkeep outside a storage directory: {path}")
            if any(candidate.startswith(f"model/{layer}/") and candidate.endswith(".yaml") for candidate in paths):
                raise GenerationError(f"unexpected .gitkeep in non-empty layer: {path}")
    # Each component's rendered document by name; ``expected["swc"]`` maps its path to the name.
    swc_paths = {name: path for path, name in expected["swc"].items()}
    component_docs = {name: parsed[path] for name, path in swc_paths.items()}
    _validate_satisfies(expected, parsed)
    _validate_connectors_and_compositions(project, expected, parsed, swc_paths, component_docs)
    _validate_partitions_and_tasks(project, expected, parsed, swc_paths, component_docs)
    if project.emit_runnable_wcet:
        _validate_runnable_budgets(expected, parsed, swc_paths, component_docs, task_overhead_us)
    return {
        layer: [parsed[path] for path in sorted(expected.get(directory, {}))]
        for layer, directory in PASSPORT_LAYERS.items()
    }


def _expected_files(project: Project) -> dict[str, dict[str, str]]:
    """Return each emitted layer's expected ``{path: identity}``; two elements of one file fail."""
    # Each layer's expected (path, identity) pairs as a list, so two elements that would share one
    # file are refused rather than collapsed into one dictionary key.
    expected_pairs: dict[str, list[tuple[str, str]]] = {
        "requirements": [(f"model/requirements/{x.id}.yaml", x.id) for x in project.requirements],
        "compositions": [(f"model/compositions/{x.name}.yaml", x.name) for x in project.compositions],
        "interfaces": [(f"model/interfaces/{x.name}.yaml", x.name) for x in project.interfaces],
        "connectors": [(f"model/connectors/{x.name}.yaml", x.name) for x in project.connectors],
        "tasks": [(f"model/tasks/{x.name}.yaml", x.name) for x in project.tasks],
        "sequences": [(f"model/sequences/{x.name}.yaml", x.name) for x in project.sequences],
        "hardware": [(f"model/hardware/{name}.yaml", name) for name in hardware_names(project)],
        # The data type directories come from the emitter's own dispatch, never a second list.
        **{
            directory: [
                (f"model/{directory}/{x.name}.yaml", x.name)
                for x in project.data_types
                if data_type_directory(x) == directory
            ]
            for directory in sorted({data_type_directory(x) for x in project.data_types})
        },
        "swc": [(f"model/swc/{x.group}/{x.name}.yaml", x.name) for x in project.components],
    }
    # The five type layers, from the emitter's own dispatch too; an empty one expects no file.
    for directory, documents in type_layer_documents(project).items():
        expected_pairs.setdefault(directory, []).extend(
            (f"model/{directory}/{name}.yaml", name) for name, _ in documents
        )
    expected: dict[str, dict[str, str]] = {}
    for layer, pairs in expected_pairs.items():
        folded_paths = [path.casefold() for path, _ in pairs]
        if len(set(folded_paths)) != len(folded_paths):
            raise GenerationError(f"two {layer} elements share one rendered file")
        expected[layer] = dict(pairs)
    return expected


#: The layers whose elements carry a ``satisfies`` list of their own, by the site kind
#: (``model.LINK_KINDS``) each element is; a component's ports and runnables carry theirs in the
#: component's document.
_SATISFIES_LAYERS = {
    SWC_SITE: "swc",
    INTERFACE_SITE: "interfaces",
    COMPOSITION_SITE: "compositions",
    TASK_SITE: "tasks",
    HARDWARE_SITE: "hardware",
}


def _validate_satisfies(expected: Mapping[str, Mapping[str, str]], parsed: Mapping[str, dict[str, Any]]) -> None:
    """Every ``satisfies`` entry of every site names a rendered requirement at one of its versions.

    The versions are 1 to the version the requirement states.
    """
    requirement_entries = set()
    for path in expected["requirements"]:
        version = parsed[path].get("version")
        if isinstance(version, int) and not isinstance(version, bool):
            requirement_entries |= {satisfies_entry(parsed[path]["id"], v) for v in range(1, version + 1)}
    for kind, layer in _SATISFIES_LAYERS.items():
        for path in expected[layer]:
            doc = parsed[path]
            sites = [doc]
            if kind == SWC_SITE:
                sites += [*doc.get("ports", []), *doc.get("runnables", [])]
            for site in sites:
                for entry in site.get("satisfies", []):
                    if _name(entry, "satisfies entry", path) not in requirement_entries:
                        raise GenerationError(f"unresolved satisfies entry {entry!r}: {path}")


def _validate_connectors_and_compositions(
    project: Project,
    expected: Mapping[str, Mapping[str, str]],
    parsed: Mapping[str, dict[str, Any]],
    swc_paths: Mapping[str, str],
    component_docs: Mapping[str, dict[str, Any]],
) -> None:
    """Every rendered connector endpoint and composition member resolves."""
    component_names = set(component_docs)
    composition_names = {c.name for c in project.compositions}
    port_names = {
        (name, _name(port["name"], "port name", swc_paths[name]))
        for name, doc in component_docs.items()
        for port in doc.get("ports", [])
    }
    for path in expected["connectors"]:
        data = parsed[path]
        for endpoint in (data.get("from", {}), data.get("to", {})):
            swc = _optional_name(endpoint.get("swc"), "connector swc", path)
            if swc is not None and (
                swc not in component_names
                or (swc, _optional_name(endpoint.get("port"), "connector port", path)) not in port_names
            ):
                raise GenerationError(f"rendered connector reference unresolved: {path}")
            composition = _optional_name(endpoint.get("composition"), "connector composition", path)
            if composition is not None and composition not in composition_names:
                raise GenerationError(f"rendered connector composition reference unresolved: {path}")
    for path in expected["compositions"]:
        for member in parsed[path].get("members", []):
            member_type = _optional_name(member.get("type"), "member type", path)
            if member_type is not None and member_type not in component_names:
                raise GenerationError(f"rendered composition reference unresolved: {path}")
            composition = _optional_name(member.get("composition"), "member composition", path)
            if composition is not None and composition not in composition_names:
                raise GenerationError(f"rendered composition reference unresolved: {path}")


def _validate_partitions_and_tasks(
    project: Project,
    expected: Mapping[str, Mapping[str, str]],
    parsed: Mapping[str, dict[str, Any]],
    swc_paths: Mapping[str, str],
    component_docs: Mapping[str, dict[str, Any]],
) -> None:
    """Partitions, tasks and sequences name rendered elements.

    Partitions sit on their cores; tasks name their partition and rendered runnables; sequences
    step through their task's rendered runnables.
    """
    for partition in partitions(project):
        path = f"model/hardware/{partition.name}.yaml"
        # The partition must sit on the core the model names, rated exactly as the model rates it:
        # a derived WATERS partition stays unrated; an explicit one may be rated.
        doc = parsed[path]
        rated = partition.asil is not ASIL.ABSENT
        if (
            doc.get("kind") != "partition"
            or doc.get("core") != partition.core
            or ("asil" in doc) != rated
            or (rated and doc.get("asil") != partition.asil.value)
        ):
            raise GenerationError(
                f"partition is not the {'rated' if rated else 'unrated'} partition on {partition.core}: {path}"
            )
    by_name = {task.name: task for task in project.tasks}
    for path, task_name in expected["tasks"].items():
        named = parsed[path].get("partition")
        task = by_name[task_name]
        # A WATERS task names its core's derived partition; a specimen's its own.
        if named != task_partition(task):
            raise GenerationError(f"task partition {named!r} is not {task_partition(task)!r}: {path}")
    # Resolved against the rendered component files, not the in-memory project: the oracle is
    # the tree on disk, and the budget check looks the same keys up in those files.  One name
    # twice in a component would let one budget shadow the other, so it is refused here.
    runnable_ids: set[tuple[str, str]] = set()
    for name, doc in component_docs.items():
        for runnable in doc.get("runnables", []):
            key = (name, _name(runnable.get("name"), "runnable name", swc_paths[name]))
            if key in runnable_ids:
                raise GenerationError(f"duplicate rendered runnable {key[1]!r}: {swc_paths[name]}")
            runnable_ids.add(key)
    for path in expected["tasks"]:
        for runnable in parsed[path].get("runnables", []):
            reference = (
                _name(runnable.get("swc"), "task runnable swc", path),
                _name(runnable.get("runnable"), "task runnable", path),
            )
            if reference not in runnable_ids:
                raise GenerationError(f"rendered task runnable reference unresolved: {path}")
    task_runnables = {
        task_name: {(r.get("swc"), r.get("runnable")) for r in parsed[path].get("runnables", [])}
        for path, task_name in expected["tasks"].items()
    }
    for path in expected["sequences"]:
        doc = parsed[path]
        named_task = _name(doc.get("task"), "sequence task", path)
        # Resolved before the steps, so a sequence with no step cannot carry an undeclared task.
        if named_task not in task_runnables:
            raise GenerationError(f"rendered sequence task unresolved: {path}")
        steps = doc.get("steps", [])
        if not steps:
            raise GenerationError(f"rendered sequence has no step: {path}")
        for step in steps:
            reference = (
                _name(step.get("swc"), "sequence step swc", path),
                _name(step.get("runnable"), "sequence step runnable", path),
            )
            if reference not in task_runnables.get(named_task, set()):
                raise GenerationError(f"rendered sequence step unresolved: {path}")
    # The rendered runnables are the model's, in its order: an extra runnable no task names
    # would pass the reference check above.
    for component in project.components:
        rendered = [r.get("name") for r in component_docs[component.name].get("runnables", [])]
        if rendered != [r.name for r in component.runnables]:
            raise GenerationError(f"rendered runnables differ from the model: {swc_paths[component.name]}")


def _validate_runnable_budgets(
    expected: Mapping[str, Mapping[str, str]],
    parsed: Mapping[str, dict[str, Any]],
    swc_paths: Mapping[str, str],
    component_docs: Mapping[str, dict[str, Any]],
    task_overhead_us: int,
) -> None:
    """The budget oracle, on the rendered values.

    Every runnable budget is positive, and every task budget is at least its runnables' sum and
    exactly the overhead plus that sum.

    The caller has resolved every task reference against the rendered runnables these budgets
    are read from, and refused a runnable name rendered twice in one component."""
    budgets: dict[tuple[str, str], int] = {}
    for name, doc in component_docs.items():
        path = swc_paths[name]
        for runnable in doc.get("runnables", []):
            value = runnable.get("wcet")
            budget = _microseconds(value, "runnable wcet", path)
            if budget <= 0:
                raise GenerationError(f"runnable wcet {value!r} is not positive: {path}")
            budgets[(name, runnable.get("name"))] = budget
    for path in expected["tasks"]:
        doc = parsed[path]
        wcet = _microseconds(doc.get("wcet"), "task wcet", path)
        total = sum(budgets[(r["swc"], r["runnable"])] for r in doc.get("runnables", []))
        # One check carries both halves of the oracle: the overhead is positive (preset.py), so
        # the equality also proves the task budget is at least its runnables' sum.
        if wcet != task_overhead_us + total:
            raise GenerationError(
                f"task wcet {wcet}us must be >= its runnables' sum {total}us and exactly the "
                f"overhead {task_overhead_us}us plus that sum: {path}"
            )


def _load_yaml(data: bytes, path: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise GenerationError(f"invalid rendered YAML {path}: {exc}") from None
    if not isinstance(value, dict):
        raise GenerationError(f"rendered YAML root is not a mapping: {path}")
    return value


def validate_generation(
    architecture: Architecture,
    placement: PlacementResult,
    project: Project,
    preset: Preset,
    file_map: Mapping[str, bytes],
    sidecar_bytes: bytes,
) -> None:
    validate_architecture(architecture, preset)
    validate_placement(placement, preset)
    validate_project(project)
    build_metrics(architecture, placement, project, preset)
    documents = validate_file_map(file_map, project, task_overhead_us=preset.task_overhead_us)
    validate_sidecar(sidecar_bytes, architecture, placement, project, preset, file_map, documents=documents)
