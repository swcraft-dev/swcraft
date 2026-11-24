from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from swcraft_specimens import __version__
from swcraft_specimens.architecture import Architecture, build_architecture, validate_architecture
from swcraft_specimens.deterministic import SplitMix64
from swcraft_specimens.emit import build_file_map, materialize
from swcraft_specimens.errors import GenerationError
from swcraft_specimens.generate import generate_project
from swcraft_specimens.metrics import LayerCounts, build_metrics
from swcraft_specimens.model import Project, partition_name
from swcraft_specimens.packaged import preset_path
from swcraft_specimens.passport import compute_passport
from swcraft_specimens.placement import (
    PlacementResult,
    architecture_to_project,
    build_placement,
    validate_placement,
)
from swcraft_specimens.preset import Preset, load_preset
from swcraft_specimens.sidecar import (
    GENERATION_SCHEMA_VERSION,
    build_generation_document,
    generation_json_bytes,
    project_tree_sha256,
    validate_sidecar,
)
from swcraft_specimens.toolchain import explicit_toolchain_version
from swcraft_specimens.validate import validate_file_map, validate_generation

ROOT = Path(__file__).parents[1]
Built = tuple[Preset, Architecture, PlacementResult, Project, dict[str, bytes], bytes]
PRESET = ROOT / "tests/fixtures/small_scale.yaml"
SMALL = preset_path("small")

# The sidecar-remainder digest of the shipped `small` preset: the SHA-256 of the canonical JSON
# (sorted keys, separators "," and ":", UTF-8) of its generation.json without the keys of
# SIDECAR_REMAINDER_REMOVED_KEYS. Everything else in the sidecar (placement, costs, loads, landmarks,
# timing) must stay equal to it.
SIDECAR_REMAINDER_REMOVED_KEYS = (
    ("schema_version",),
    ("passport",),
    ("toolchain", "swcraft_version"),
    ("project", "layer_counts"),
    ("project", "project_tree_sha256"),
    ("preset", "parameters", "emit_runnable_wcet"),
)
E386C0B_SMALL_SIDECAR_REMAINDER_SHA256 = "c66860b9bd3e1d2521afe38dd5f1e9c9afc360fdaa93079e9def2f2f916a09ff"
#: The generator version the remainder digest was taken at. The sidecar's `generator.version` is read
#: back to it, so a release of the generator does not move the digest of what the generator computes.
E386C0B_GENERATOR_VERSION = "0.1.0"


def model() -> Built:
    """The fixture preset: runnable budgets switched off."""
    return _build(PRESET)


def shipped(name: str) -> Built:
    """A shipped preset: runnable budgets switched on."""
    return _build(preset_path(name))


def _build(preset_path: Path) -> Built:
    """Run the whole pipeline in memory; the file map carries the sidecar as generation.json."""
    preset = load_preset(preset_path)
    rng = SplitMix64(preset.seed)
    architecture = build_architecture(preset, rng)
    placement = build_placement(architecture, preset, rng)
    project = architecture_to_project(placement, preset)
    files = build_file_map(project)
    digest = project_tree_sha256(files)
    documents = validate_file_map(files, project, task_overhead_us=preset.task_overhead_us)
    sidecar = generation_json_bytes(
        build_generation_document(
            architecture,
            placement,
            project,
            preset,
            digest,
            passport=compute_passport(documents),
        )
    )
    full = dict(files, **{"generation.json": sidecar})
    return preset, architecture, placement, project, full, sidecar


def test_sidecar_is_deterministic_and_contains_no_float_beside_the_passport() -> None:
    first = model()[-1]
    second = model()[-1]
    assert first == second
    assert first.endswith(b"\n")

    def no_float(value: object) -> None:
        assert not isinstance(value, float)
        if isinstance(value, dict):
            for child in value.values():
                no_float(child)
        elif isinstance(value, list):
            for child in value:
                no_float(child)

    # A passport's mean_length is a float; the ban holds for everything else.
    document = json.loads(first)
    del document["passport"]
    no_float(document)


def test_digest_framing_scope_and_order() -> None:
    base = {"swcraft.yaml": b"x", "model/a.yaml": b"a", "model/.gitkeep": b""}
    assert project_tree_sha256(base) == project_tree_sha256(dict(reversed(list(base.items()))))
    assert project_tree_sha256(base) != project_tree_sha256(
        {"swcraft.yaml": b"x", "model/b.yaml": b"a", "model/.gitkeep": b""}
    )
    assert project_tree_sha256(base) != project_tree_sha256(
        {"swcraft.yaml": b"y", "model/a.yaml": b"a", "model/.gitkeep": b""}
    )
    assert project_tree_sha256(
        dict(base, **{"generation.json": b"changed", "dist/x": b"changed", "views/x": b"changed"})
    ) == project_tree_sha256(base)


def bad_schema(d: dict[str, Any]) -> None:
    d["schema_version"] = "9.0"


def bad_counts(d: dict[str, Any]) -> None:
    d["project"]["layer_counts"]["swc"] += 1


def bad_mapping(d: dict[str, Any]) -> None:
    d["placement"]["emitted"]["component_to_core"][0]["core"] = "CORE_BAD"


def bad_landmark(d: dict[str, Any]) -> None:
    d["landmarks"]["gateway_component"] = "ABSENT"


def bad_passport_leaf(d: dict[str, Any]) -> None:
    d["passport"]["layers"]["swc"] += 1


def passport_int_as_float(d: dict[str, Any]) -> None:
    # The schema accepts 0.0 as a mean; only the recomputation tells it from the int 0.
    d["passport"]["identifiers"]["sequences"]["mean_length"] = 0.0


def invalid_passport(d: dict[str, Any]) -> None:
    d["passport"]["layers"]["swc"] = -1


def missing_passport(d: dict[str, Any]) -> None:
    del d["passport"]


def float_beside_the_passport(d: dict[str, Any]) -> None:
    d["landmarks"]["float"] = 1.5


@pytest.mark.parametrize(
    "mutate, message",
    [
        (bad_schema, "schema_version"),
        (bad_counts, "layer_counts"),
        (bad_mapping, "placement.emitted"),
        (bad_landmark, "landmarks"),
        (bad_passport_leaf, r"generation\.json\.passport does not match the rendered tree"),
        (passport_int_as_float, r"generation\.json\.passport does not match the rendered tree"),
        (invalid_passport, r"(?s)generation\.json\.passport is not a valid passport.*layers\.swc"),
        (missing_passport, r"generation\.json\.passport is missing"),
        (float_beside_the_passport, r"generation\.json\.landmarks\.float: floating point"),
    ],
)
def test_sidecar_corruption_has_specific_generation_error(
    mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    preset, architecture, placement, project, files, sidecar = model()
    document = json.loads(sidecar)
    mutate(document)
    with pytest.raises(GenerationError, match=message):
        validate_sidecar(
            generation_json_bytes(document),
            architecture,
            placement,
            project,
            preset,
            files,
            documents=validate_file_map(files, project, task_overhead_us=preset.task_overhead_us),
        )


def test_rendered_corruption_has_specific_generation_error() -> None:
    preset, architecture, placement, project, files, sidecar = model()
    broken = dict(files)
    path = next(path for path in broken if path.startswith("model/swc/") and path.endswith(".yaml"))
    broken[path] = broken[path].replace((f"name: {project.components[0].name}".encode()), b"name: Broken")
    with pytest.raises(GenerationError, match="filename/identity mismatch"):
        validate_file_map(broken, project, task_overhead_us=preset.task_overhead_us)


def test_full_internal_validation_and_metrics() -> None:
    preset, architecture, placement, project, files, sidecar = model()
    validate_generation(architecture, placement, project, preset, files, sidecar)
    metrics = build_metrics(architecture, placement, project, preset)
    assert metrics.layer_counts.hardware == 2 * len(project.cores) + len(project.memories) + 1
    assert metrics.planted.total_cost < metrics.emitted.total_cost


def test_transactional_publish_and_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    preset, architecture, placement, project, files, sidecar = model()
    output = tmp_path / "out"
    materialize(files, output)
    assert (output / "generation.json").exists()
    original = Path.write_bytes

    def fail(path: Path, data: bytes) -> int:
        if path.name == "generation.json":
            raise OSError("injected write failure")
        return original(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail)
    with pytest.raises(GenerationError, match="cannot write"):
        materialize(files, tmp_path / "failed")
    assert not (tmp_path / "failed").exists()
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(GenerationError, match="cannot write"):
        materialize(files, existing)
    assert list(existing.iterdir()) == []


def test_successful_generation_publishes_sidecar(tmp_path: Path) -> None:
    output = tmp_path / "out"
    generate_project(load_preset(PRESET), output)
    assert (output / "generation.json").is_file()
    assert (
        json.loads((output / "generation.json").read_text(encoding="utf-8"))["schema_version"]
        == GENERATION_SCHEMA_VERSION
    )


def test_sidecar_names_the_release_the_manifest_names() -> None:
    # No installed release (the autouse fixture) writes `null`.
    document = json.loads(model()[-1].decode("utf-8"))
    assert document["toolchain"] == {"swcraft_version": None}
    with explicit_toolchain_version("0.22.0"):
        named = json.loads(model()[-1].decode("utf-8"))
    assert named["toolchain"] == {"swcraft_version": "0.22.0"}


@pytest.mark.parametrize("value", ["0.22.0", 7, ["0.22.0"]])
def test_the_sidecar_reader_refuses_a_version_the_manifest_does_not_name(value: object) -> None:
    preset, architecture, placement, project, files, sidecar = model()
    document = json.loads(sidecar)
    document["toolchain"]["swcraft_version"] = value
    documents = validate_file_map(files, project, task_overhead_us=preset.task_overhead_us)
    with pytest.raises(GenerationError, match="toolchain.swcraft_version mismatch"):
        validate_sidecar(
            json.dumps(document).encode("utf-8"),
            architecture,
            placement,
            project,
            preset,
            files,
            documents=documents,
        )


@pytest.mark.parametrize(
    "case",
    [
        "broken connector endpoint",
        "broken port/interface reference",
        "unresolved type",
        "duplicate identity",
        "case-only identity collision",
        "missing root member",
        "duplicate root member",
        "missing runnable task assignment",
        "duplicate runnable task assignment",
        "wrong task core",
        "wrong task WCET",
        "missing memory budget",
        "negative memory budget",
        "inaccessible private budget",
        "wrong placement mapping",
        "moved pinned component",
        "wrong displaced count",
        "wrong layer count in sidecar",
        "wrong component count in sidecar",
        "wrong project tree digest",
        "wrong schema version",
        "absent sidecar landmark",
        "mutated rendered YAML identity",
        "case-only path collision",
        "absolute path",
        "path traversal",
        "CRLF",
        "invalid UTF-8",
        "unexpected gitkeep",
        "runnable wcet emitted when false",
    ],
)
def test_the_corruption_cases_are_specific(case: str) -> None:
    preset, architecture, placement, project, files, sidecar = model()
    if case == "broken connector endpoint":
        bad = replace(
            architecture,
            connectors=(replace(architecture.connectors[0], provider="ABSENT"),) + architecture.connectors[1:],
        )
        with pytest.raises(GenerationError, match="connector endpoint|invalid connector endpoints"):
            validate_architecture(bad, preset)
    elif case == "broken port/interface reference":
        bad = replace(
            architecture,
            connectors=(replace(architecture.connectors[0], interface="ABSENT"),) + architecture.connectors[1:],
        )
        with pytest.raises(GenerationError, match="unresolved interface"):
            validate_architecture(bad, preset)
    elif case == "unresolved type":
        with pytest.raises(GenerationError, match="unresolved data type"):
            validate_architecture(replace(architecture, data_types=()), preset)
    elif case in {"duplicate identity", "case-only identity collision"}:
        name = (
            architecture.components[0].name if case == "duplicate identity" else architecture.components[0].name.lower()
        )
        bad = replace(
            architecture,
            components=(architecture.components[0], replace(architecture.components[1], name=name))
            + architecture.components[2:],
        )
        with pytest.raises(GenerationError, match="Root|duplicate|case"):
            validate_architecture(bad, preset)
    elif case in {"missing root member", "duplicate root member"}:
        members = (
            architecture.root.members[:-1]
            if case == "missing root member"
            else architecture.root.members + (architecture.root.members[0],)
        )
        with pytest.raises(GenerationError, match="Root"):
            validate_architecture(replace(architecture, root=replace(architecture.root, members=members)), preset)
    elif case == "missing runnable task assignment":
        with pytest.raises(GenerationError, match="topology"):
            validate_placement(
                replace(
                    placement,
                    emitted_tasks=replace(placement.emitted_tasks, tasks=placement.emitted_tasks.tasks[:-1]),
                ),
                preset,
            )
    elif case == "duplicate runnable task assignment":
        task = placement.emitted_tasks.tasks[0]
        bad_task = replace(task, runnables=task.runnables + task.runnables[:1])
        with pytest.raises(GenerationError, match="topology"):
            validate_placement(
                replace(
                    placement,
                    emitted_tasks=replace(
                        placement.emitted_tasks,
                        tasks=(bad_task,) + placement.emitted_tasks.tasks[1:],
                    ),
                ),
                preset,
            )
    elif case == "wrong task core":
        task = placement.emitted_tasks.tasks[0]
        bad_task = replace(task, core="CORE_BAD")
        with pytest.raises(GenerationError, match="topology"):
            validate_placement(
                replace(
                    placement,
                    emitted_tasks=replace(
                        placement.emitted_tasks,
                        tasks=(bad_task,) + placement.emitted_tasks.tasks[1:],
                    ),
                ),
                preset,
            )
    elif case == "wrong task WCET":
        task = placement.emitted_tasks.tasks[0]
        with pytest.raises(GenerationError, match="WCET mismatch"):
            validate_placement(
                replace(
                    placement,
                    emitted_tasks=replace(
                        placement.emitted_tasks,
                        tasks=(replace(task, wcet_us=task.wcet_us + 1),) + placement.emitted_tasks.tasks[1:],
                    ),
                ),
                preset,
            )
    elif case in {"missing memory budget", "negative memory budget", "inaccessible private budget"}:
        budgets = list(placement.memory_budgets)
        if case == "missing memory budget":
            budgets.pop()
        elif case == "negative memory budget":
            budgets[0] = replace(budgets[0], bytes=-1)
        else:
            positive = next(i for i, b in enumerate(budgets) if b.memory != "RAM_SHARED" and b.bytes > 0)
            target = next(b for b in budgets if b.component == budgets[positive].component and b.memory == "RAM_SHARED")
            budgets[positive] = replace(budgets[positive], component=target.component, memory="RAM_CORE_BAD")
        with pytest.raises(GenerationError, match="memory budget|negative|inaccessible|pinned"):
            validate_placement(replace(placement, memory_budgets=tuple(budgets)), preset)
    elif case == "wrong placement mapping":
        mapping = list(placement.emitted.mapping)
        mapping[0] = (mapping[0][0], "CORE_BAD")
        with pytest.raises(GenerationError, match="unknown core"):
            validate_placement(
                replace(placement, emitted=replace(placement.emitted, mapping=tuple(mapping))),
                preset,
            )
    elif case == "moved pinned component":
        name = placement.pinned[0]
        pinned_mapping = dict(placement.emitted.mapping)
        pinned_mapping[name] = next(c.name for c in placement.cores if c.name != placement.planted.core_for(name))
        with pytest.raises(GenerationError, match="pinned component"):
            validate_placement(
                replace(
                    placement,
                    emitted=replace(placement.emitted, mapping=tuple(pinned_mapping.items())),
                ),
                preset,
            )
    elif case == "wrong displaced count":
        with pytest.raises(GenerationError, match="displaced mapping"):
            validate_placement(replace(placement, displaced=()), preset)
    else:
        document = json.loads(sidecar)
        broken = dict(files)
        if case == "wrong layer count in sidecar":
            document["project"]["layer_counts"]["swc"] += 1
        elif case == "wrong component count in sidecar":
            document["architecture"]["components"] = document["architecture"]["components"][:-1]
        elif case == "wrong project tree digest":
            document["project"]["project_tree_sha256"] = "0" * 64
        elif case == "wrong schema version":
            document["schema_version"] = "9.0"
        elif case == "absent sidecar landmark":
            document["landmarks"]["gateway_component"] = "ABSENT"
        elif case == "mutated rendered YAML identity":
            path = next(p for p in broken if p.startswith("model/swc/") and p.endswith(".yaml"))
            broken[path] = broken[path].replace((f"name: {project.components[0].name}".encode()), b"name: Broken")
        elif case == "case-only path collision":
            broken["model/CASE.yaml"] = b"name: x\n"
            broken["model/case.yaml"] = b"name: y\n"
        elif case == "absolute path":
            broken["/absolute.yaml"] = b"x"
        elif case == "path traversal":
            broken["../escape.yaml"] = b"x"
        elif case == "CRLF":
            broken["swcraft.yaml"] = broken["swcraft.yaml"].replace(b"\n", b"\r\n")
        elif case == "invalid UTF-8":
            broken["swcraft.yaml"] = b"\xff"
        elif case == "unexpected gitkeep":
            broken["model/swc/.gitkeep"] = b""
        elif case == "runnable wcet emitted when false":
            assert not project.emit_runnable_wcet
            path = next(p for p in broken if p.startswith("model/swc/") and p.endswith(".yaml"))
            doc = yaml.safe_load(broken[path].decode("utf-8"))
            doc["runnables"][0].update({"wcet": "1us", "wcet_type": "net"})
            broken[path] = yaml.safe_dump(doc, sort_keys=False).encode("utf-8")
        file_map_messages = {
            "mutated rendered YAML identity": "filename/identity mismatch",
            "case-only path collision": ("case-only path collision: model/CASE.yaml and model/case.yaml"),
            "absolute path": "rendered file path is not relative POSIX: /absolute.yaml",
            "path traversal": r"rendered file path is not relative POSIX: \.\./escape.yaml",
            "CRLF": "rendered file has CRLF: swcraft.yaml",
            "invalid UTF-8": "rendered file is not UTF-8: swcraft.yaml",
            "unexpected gitkeep": "unexpected .gitkeep in non-empty layer: model/swc/.gitkeep",
            "runnable wcet emitted when false": "emitted while emit_runnable_wcet=false",
        }
        sidecar_messages = {
            "wrong layer count in sidecar": r"generation\.json\.project\.layer_counts mismatch",
            "wrong component count in sidecar": "architecture component count mismatch",
            "wrong project tree digest": "project_tree_sha256 does not match rendered tree",
            "wrong schema version": r"generation\.json\.schema_version: expected 2\.1",
            "absent sidecar landmark": r"generation\.json\.landmarks mismatch",
        }
        if case in file_map_messages:
            with pytest.raises(GenerationError, match=file_map_messages[case]):
                validate_file_map(broken, project, task_overhead_us=preset.task_overhead_us)
        else:
            with pytest.raises(GenerationError, match=sidecar_messages[case]):
                validate_sidecar(
                    generation_json_bytes(document),
                    architecture,
                    placement,
                    project,
                    preset,
                    files,
                    documents=validate_file_map(files, project, task_overhead_us=preset.task_overhead_us),
                )


@pytest.mark.parametrize(
    "case",
    [
        "gitkeep beside a base type",
        "missing empty-directory gitkeep",
        "gitkeep directly in data_types",
        "data type file missing",
    ],
)
def test_the_gitkeep_rule_and_the_data_type_paths_cover_all_fifteen_directories(case: str) -> None:
    preset, _, _, project, files, _ = model()
    broken = dict(files)
    validate_file_map(broken, project, task_overhead_us=preset.task_overhead_us)
    if case == "gitkeep beside a base type":
        broken["model/data_types/base/.gitkeep"] = b""
        match = "unexpected .gitkeep in non-empty layer: model/data_types/base/.gitkeep"
    elif case == "missing empty-directory gitkeep":
        del broken["model/data_types/units/.gitkeep"]
        match = "rendered tree lacks layer data_types/units"
    elif case == "gitkeep directly in data_types":
        broken["model/data_types/.gitkeep"] = b""
        match = "unexpected .gitkeep outside a storage directory: model/data_types/.gitkeep"
    else:
        prefix = "model/data_types/implementation/"
        del broken[next(p for p in broken if p.startswith(prefix) and p.endswith(".yaml"))]
        match = "rendered data_types/implementation file map mismatch"
    with pytest.raises(GenerationError, match=match):
        validate_file_map(broken, project, task_overhead_us=preset.task_overhead_us)


@pytest.mark.parametrize("entry", ["REQ_ABSENT@1", "{first}@2", "{first}"])
def test_every_satisfies_entry_resolves_to_an_emitted_requirement_at_version_1(entry: str) -> None:
    preset, _, _, project, files, _ = model()
    validate_file_map(files, project, task_overhead_us=preset.task_overhead_us)
    path = next(p for p in files if p.startswith("model/swc/") and b"satisfies:" in files[p])
    first = project.requirements[0].id
    text = files[path].decode("utf-8")
    head, _, _ = text.partition("satisfies:\n")
    broken = dict(files)
    broken[path] = f"{head}satisfies:\n- {entry.format(first=first)}\n".encode()
    with pytest.raises(GenerationError, match=rf"unresolved satisfies entry .*: {path}"):
        validate_file_map(broken, project, task_overhead_us=preset.task_overhead_us)


@pytest.mark.parametrize("case", ["partition file missing", "task on another core's partition"])
def test_the_partitions_are_expected_and_each_task_names_its_core_partition(case: str) -> None:
    preset, _, _, project, files, _ = model()
    validate_file_map(files, project, task_overhead_us=preset.task_overhead_us)
    broken = dict(files)
    if case == "partition file missing":
        del broken[f"model/hardware/{partition_name(project.cores[0].name)}.yaml"]
        match = "rendered hardware file map mismatch"
    else:
        task = project.tasks[0]
        other = next(core.name for core in project.cores if core.name != task.core)
        path = f"model/tasks/{task.name}.yaml"
        broken[path] = broken[path].replace(
            f"partition: {partition_name(task.core)}\n".encode(),
            f"partition: {partition_name(other)}\n".encode(),
        )
        assert broken[path] != files[path]
        match = (
            rf"task partition '{partition_name(other)}' is not "
            rf"'{partition_name(task.core)}': {path}"
        )
    with pytest.raises(GenerationError, match=match):
        validate_file_map(broken, project, task_overhead_us=preset.task_overhead_us)


def _micro(value: str) -> int:
    assert value.endswith("us")
    return int(value[: -len("us")])


def _rendered_runnable_budgets(files: dict[str, bytes]) -> dict[tuple[str, str], int]:
    budgets: dict[tuple[str, str], int] = {}
    for path, data in files.items():
        if path.startswith("model/swc/") and path.endswith(".yaml"):
            doc = yaml.safe_load(data.decode("utf-8"))
            for runnable in doc["runnables"]:
                budgets[(doc["name"], runnable["name"])] = _micro(runnable["wcet"])
    return budgets


def test_each_rendered_task_budget_is_the_overhead_plus_its_runnables_sum() -> None:
    preset, _, _, project, files, _ = shipped("small")
    assert project.emit_runnable_wcet
    validate_file_map(files, project, task_overhead_us=preset.task_overhead_us)
    budgets = _rendered_runnable_budgets(files)
    loaded = 0
    for task in project.tasks:
        doc = yaml.safe_load(files[f"model/tasks/{task.name}.yaml"].decode("utf-8"))
        total = sum(budgets[(r["swc"], r["runnable"])] for r in doc["runnables"])
        assert _micro(doc["wcet"]) >= total
        assert _micro(doc["wcet"]) == preset.task_overhead_us + total
        loaded += bool(doc["runnables"])
    assert loaded


def _rewrite(files: dict[str, bytes], path: str, change: Callable[[Any], None]) -> dict[str, bytes]:
    doc = yaml.safe_load(files[path].decode("utf-8"))
    change(doc)
    broken = dict(files)
    broken[path] = yaml.safe_dump(doc, sort_keys=False).encode("utf-8")
    return broken


@pytest.mark.parametrize("value", ["0us", "-3us"])
def test_a_runnable_budget_that_is_not_positive_is_refused(value: str) -> None:
    preset, _, _, project, files, _ = shipped("small")
    path = next(p for p in files if p.startswith("model/swc/") and p.endswith(".yaml"))
    broken = _rewrite(files, path, lambda d: d["runnables"][0].update({"wcet": value}))
    with pytest.raises(GenerationError, match=rf"runnable wcet '{value}' is not positive: {path}"):
        validate_file_map(broken, project, task_overhead_us=preset.task_overhead_us)


def test_a_rendered_task_budget_below_its_runnables_sum_is_refused() -> None:
    preset, _, _, project, files, _ = shipped("small")
    budgets = _rendered_runnable_budgets(files)
    task = next(t for t in project.tasks if t.runnables)
    path = f"model/tasks/{task.name}.yaml"
    total = sum(budgets[ref] for ref in task.runnables)
    broken = _rewrite(files, path, lambda d: d.update({"wcet": f"{total - 1}us"}))
    with pytest.raises(GenerationError, match=rf"task wcet {total - 1}us must be >= its runnables' sum {total}us"):
        validate_file_map(broken, project, task_overhead_us=preset.task_overhead_us)


def test_the_task_check_sums_the_rendered_runnable_budgets() -> None:
    preset, _, _, project, files, _ = shipped("small")
    task = next(t for t in project.tasks if t.runnables)
    component, runnable = task.runnables[0]
    path = next(p for p in files if p.startswith("model/swc/") and p.endswith(f"/{component}.yaml"))

    def raise_one(doc: Any) -> None:
        for item in doc["runnables"]:
            if item["name"] == runnable:
                item["wcet"] = f"{_micro(item['wcet']) + 1}us"

    broken = _rewrite(files, path, raise_one)
    with pytest.raises(
        GenerationError,
        match=(
            rf"exactly the overhead {preset.task_overhead_us}us plus that sum: "
            rf"model/tasks/{task.name}.yaml"
        ),
    ):
        validate_file_map(broken, project, task_overhead_us=preset.task_overhead_us)


# `swcraft list`'s layer names, in its order; `LayerCounts` is their one source.
LIST_LAYERS = (
    "requirements",
    "compositions",
    "swc",
    "interfaces",
    "application_types",
    "implementation_types",
    "base_types",
    "units",
    "compu_methods",
    "data_constraints",
    "mapping_sets",
    "connectors",
    "tasks",
    "sequences",
    "hardware",
)

LIST_LAYER_DIRECTORIES = {
    "requirements": "requirements",
    "compositions": "compositions",
    "swc": "swc",
    "interfaces": "interfaces",
    "application_types": "data_types/application",
    "implementation_types": "data_types/implementation",
    "base_types": "data_types/base",
    "units": "data_types/units",
    "compu_methods": "data_types/compu_methods",
    "data_constraints": "data_types/constraints",
    "mapping_sets": "data_types/mapping_sets",
    "connectors": "connectors",
    "tasks": "tasks",
    "sequences": "sequences",
    "hardware": "hardware",
}


@pytest.mark.parametrize("name", ["small", "medium", "large"])
def test_layer_counts_are_the_fifteen_list_layers_and_count_the_emitted_files(name: str) -> None:
    _, _, _, project, files, sidecar = shipped(name)
    document = json.loads(sidecar)
    counts = document["project"]["layer_counts"]
    assert tuple(counts) == LIST_LAYERS
    for layer, directory in LIST_LAYER_DIRECTORIES.items():
        prefix = f"model/{directory}/"
        emitted = [p for p in files if p.startswith(prefix) and p.endswith(".yaml")]
        assert counts[layer] == len(emitted), layer
    assert counts["hardware"] == 1 + 2 * len(project.cores) + len(project.memories)
    assert counts["application_types"] == 0
    assert counts["base_types"] > 0
    assert counts["implementation_types"] > 0


def test_the_sidecar_schema_version_is_2_1() -> None:
    assert GENERATION_SCHEMA_VERSION == "2.1"
    assert json.loads(shipped("small")[-1])["schema_version"] == "2.1"


def test_the_sidecar_remainder_is_unchanged_since_e386c0b() -> None:
    document = json.loads(shipped("small")[-1])
    assert document["generator"] == {"name": "swcraft-specimens", "version": __version__}
    document["generator"]["version"] = E386C0B_GENERATOR_VERSION
    for path in SIDECAR_REMAINDER_REMOVED_KEYS:
        parent = document
        for key in path[:-1]:
            parent = parent[key]
        del parent[path[-1]]
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert digest == E386C0B_SMALL_SIDECAR_REMAINDER_SHA256


def test_the_scenario_layer_paths_are_the_layer_count_fields() -> None:
    raw = yaml.safe_load((ROOT / "tests/scenarios/production.yaml").read_text(encoding="utf-8"))
    listing = next(s for s in raw["scenarios"] if s["id"] == "list")
    paths = [a["path"] for a in listing["assertions"] if a["op"] == "count_equals"]
    assert paths == [f"layers.{field.name}.elements" for field in fields(LayerCounts)]
    expected = [a["expected"] for a in listing["assertions"] if a["op"] == "count_equals"]
    assert expected == [f"{{project.layer_counts.{field.name}}}" for field in fields(LayerCounts)]
    assert tuple(field.name for field in fields(LayerCounts)) == LIST_LAYERS


def _small_with_cores(tmp_path: Path, first: str, second: str) -> Preset:
    text = SMALL.read_text(encoding="utf-8")
    text = text.replace("name: CORE0,", f"name: {first},").replace("name: CORE1,", f"name: {second},")
    path = tmp_path / "preset.yaml"
    path.write_text(text, encoding="utf-8", newline="\n")
    return load_preset(path)


def test_a_partition_name_that_collides_with_a_core_is_refused(tmp_path: Path) -> None:
    preset = _small_with_cores(tmp_path, "A", partition_name("A"))
    output = tmp_path / "out"
    with pytest.raises(GenerationError, match=rf"duplicate hardware names ignoring case: '{partition_name('A')}'"):
        generate_project(preset, output)
    assert not output.exists()


def test_a_partition_name_over_the_generated_limit_is_refused(tmp_path: Path) -> None:
    core = "C" * 51
    preset = _small_with_cores(tmp_path, core, "CORE1")
    match = rf"invalid generated identity '{partition_name(core)}'"
    with pytest.raises(GenerationError, match=match):
        generate_project(preset, tmp_path / "out")


def test_build_file_map_refuses_to_write_one_path_twice() -> None:
    _, _, _, project, _, _ = model()
    clash = replace(project.cores[1], name=partition_name(project.cores[0].name))
    broken = replace(project, cores=(project.cores[0], clash, *project.cores[2:]))
    with pytest.raises(GenerationError, match="two generated files share one path"):
        build_file_map(broken)


def test_validate_file_map_refuses_two_elements_sharing_one_rendered_file() -> None:
    preset, _, _, project, files, _ = model()
    clash = replace(project.cores[1], name=partition_name(project.cores[0].name))
    broken = replace(project, cores=(project.cores[0], clash, *project.cores[2:]))
    with pytest.raises(GenerationError, match="two hardware elements share one rendered file"):
        validate_file_map(files, broken, task_overhead_us=preset.task_overhead_us)


def _as_processor(doc: Any) -> None:
    doc["kind"] = "processor"
    del doc["core"]


@pytest.mark.parametrize("case", ["partition on another core", "partition rated", "partition of another kind"])
def test_a_partition_must_be_the_unrated_partition_on_its_core(case: str) -> None:
    preset, _, _, project, files, _ = model()
    core = project.cores[0].name
    path = f"model/hardware/{partition_name(core)}.yaml"
    if case == "partition on another core":
        broken = _rewrite(files, path, lambda d: d.update({"core": project.cores[1].name}))
    elif case == "partition of another kind":
        # The contract gate admits ``core`` on a partition alone, so a processor document at the
        # partition's path passes it and the file-map check refuses it.
        broken = _rewrite(files, path, _as_processor)
    else:
        broken = _rewrite(files, path, lambda d: d.update({"asil": "QM"}))
    # The contract gate admits ``asil`` on a partition (a specimen's may be rated); the file-map
    # check is the oracle that a WATERS partition stays unrated.
    match = rf"partition is not the unrated partition on {core}: {path}"
    with pytest.raises(GenerationError, match=match):
        validate_file_map(broken, project, task_overhead_us=preset.task_overhead_us)


@pytest.mark.parametrize("path", ["model/sequences/S.yaml", "model/data_types/units/U.yaml"])
def test_a_yaml_file_in_an_empty_storage_directory_is_refused(path: str) -> None:
    preset, _, _, project, files, _ = model()
    broken = dict(files)
    broken[path] = b"anything: 1\n"
    with pytest.raises(GenerationError, match=path):
        validate_file_map(broken, project, task_overhead_us=preset.task_overhead_us)


@pytest.mark.parametrize(
    ("value", "what"),
    [("5ms", "runnable wcet"), ("1.5us", "runnable wcet"), ("2ms", "task wcet")],
)
def test_a_budget_that_is_not_whole_microseconds_is_refused(value: str, what: str) -> None:
    preset, _, _, project, files, _ = shipped("small")
    if what == "runnable wcet":
        path = next(p for p in files if p.startswith("model/swc/") and p.endswith(".yaml"))
        broken = _rewrite(files, path, lambda d: d["runnables"][0].update({"wcet": value}))
    else:
        path = f"model/tasks/{project.tasks[0].name}.yaml"
        broken = _rewrite(files, path, lambda d: d.update({"wcet": value}))
    with pytest.raises(
        GenerationError,
        match=rf"{what} {re.escape(repr(value))} is not a whole number of microseconds: {path}",
    ):
        validate_file_map(broken, project, task_overhead_us=preset.task_overhead_us)


@pytest.mark.parametrize("budgets", [True, False])
@pytest.mark.parametrize("change", ["renamed", "dropped"])
def test_a_task_reference_to_a_runnable_missing_from_the_rendered_component_is_refused(
    budgets: bool, change: str
) -> None:
    preset, _, _, project, files, _ = shipped("small")
    if not budgets:
        project = replace(project, emit_runnable_wcet=False)
        files = build_file_map(project)
    task = next(t for t in project.tasks if t.runnables)
    task_doc = yaml.safe_load(files[f"model/tasks/{task.name}.yaml"].decode("utf-8"))
    swc, runnable = task_doc["runnables"][0]["swc"], task_doc["runnables"][0]["runnable"]
    component = next(c for c in project.components if c.name == swc)
    path = f"model/swc/{component.group}/{component.name}.yaml"

    def rewrite(doc: Any) -> None:
        kept = [r for r in doc["runnables"] if r["name"] != runnable]
        if change == "renamed":
            renamed = next(r for r in doc["runnables"] if r["name"] == runnable)
            kept.append(dict(renamed, name=f"{runnable}Renamed"))
        doc["runnables"] = kept

    broken = _rewrite(files, path, rewrite)
    with pytest.raises(
        GenerationError,
        match=rf"rendered task runnable reference unresolved: model/tasks/{task.name}\.yaml",
    ):
        validate_file_map(broken, project, task_overhead_us=preset.task_overhead_us)


def _component_path(project: Project, name: str) -> str:
    component = next(c for c in project.components if c.name == name)
    return f"model/swc/{component.group}/{component.name}.yaml"


@pytest.mark.parametrize("budgets", [True, False])
def test_a_runnable_rendered_twice_in_one_component_is_refused(budgets: bool) -> None:
    # A copy with a smaller budget placed first: without the refusal the second entry's budget
    # would shadow the first in the task sums.
    preset, _, _, project, files, _ = shipped("small")
    if not budgets:
        project = replace(project, emit_runnable_wcet=False)
        files = build_file_map(project)
    path = _component_path(project, project.components[0].name)

    def duplicate(doc: Any) -> None:
        first = copy.deepcopy(doc["runnables"][0])
        if budgets:
            first["wcet"] = "1us"
        doc["runnables"].insert(0, first)

    broken = _rewrite(files, path, duplicate)
    with pytest.raises(GenerationError, match=rf"duplicate rendered runnable .*: {path}"):
        validate_file_map(broken, project, task_overhead_us=preset.task_overhead_us)


def test_a_rendered_runnable_the_model_does_not_have_is_refused() -> None:
    preset, _, _, project, files, _ = shipped("small")
    path = _component_path(project, project.components[0].name)

    def extra(doc: Any) -> None:
        doc["runnables"].append(dict(copy.deepcopy(doc["runnables"][0]), name="ExtraRunnable"))

    broken = _rewrite(files, path, extra)
    with pytest.raises(GenerationError, match=rf"rendered runnables differ from the model: {path}"):
        validate_file_map(broken, project, task_overhead_us=preset.task_overhead_us)


def _first_path(files: dict[str, bytes], prefix: str, marker: bytes) -> str:
    return next(p for p in sorted(files) if p.startswith(prefix) and marker in files[p])


@pytest.mark.parametrize(
    ("member", "prefix", "marker"),
    [
        ("satisfies entry", "model/swc/", b"satisfies:"),
        ("port name", "model/swc/", b"ports:"),
        ("runnable name", "model/swc/", b"runnables:"),
        ("connector swc", "model/connectors/", b"swc:"),
        # An endpoint's composition, indented under ``from``/``to``, not the connector's own.
        ("connector composition", "model/connectors/", b"\n  composition:"),
        ("member type", "model/compositions/", b"type:"),
        ("member composition", "model/compositions/", b"composition:"),
        ("task runnable", "model/tasks/", b"runnable:"),
    ],
)
def test_a_malformed_rendered_member_is_refused_not_a_type_error(member: str, prefix: str, marker: bytes) -> None:
    # The contract gate checks keys, not value types: an unhashable value at a key the file-map
    # check looks up in a set is refused as a GenerationError, never an uncaught TypeError.
    preset, _, _, project, files, _ = shipped("small")
    path = _first_path(files, prefix, marker)

    def malform(doc: Any) -> None:
        if member == "satisfies entry":
            doc["satisfies"] = [[]]
        elif member == "port name":
            doc["ports"][0]["name"] = {}
        elif member == "runnable name":
            doc["runnables"][0]["name"] = []
        elif member in ("connector swc", "connector composition"):
            key = member.split()[1]
            endpoint = next(e for e in (doc["from"], doc["to"]) if key in e)
            endpoint[key] = []
        elif member in ("member type", "member composition"):
            key = member.split()[1]
            next(m for m in doc["members"] if key in m)[key] = []
        else:
            doc["runnables"][0]["runnable"] = []

    broken = _rewrite(files, path, malform)
    with pytest.raises(GenerationError, match=rf"rendered {member} .* is not a string: {path}"):
        validate_file_map(broken, project, task_overhead_us=preset.task_overhead_us)
