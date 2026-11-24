"""The performance baseline harness.

Two kinds of tests live in this file:

- The fixed mutation's tests (unmarked, fast, in the default suite): the names
  `mutations.py` spells each hold their role on the `benchmark-default`
  specimen, and the mutation adds, removes and changes on a copy of it and
  leaves no dangling requirement link.
- `@pytest.mark.benchmark`-marked performance scenarios, deselected from the
  fast gate and run by `python scripts/dev.py benchmark`. pytest-benchmark
  measures TIME only; each scenario then runs one extra, deliberately UNTIMED
  invocation under `tracemalloc` to record peak memory into
  `benchmark.extra_info` -- tracemalloc's overhead must never contaminate a
  timed round.

The workload is a copy of the `benchmark-default` specimen, and the element
counts a probe records are the ones its passport gives. Each scenario measures
the function contracts as they are (`load_snapshot`,
`check_cross_layer_consistency`, `generate_arxml` each read the model
themselves; only diff/impact pre-build their inputs in untimed setup).
"""

from __future__ import annotations

import gc
import itertools
import json
import os
import platform
import shutil
import sys
import tracemalloc
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from conftest import copy_specimen
from swcraft_tools.arxml_import import run_import
from swcraft_tools.cli import main as _swcraft_main
from swcraft_tools.differ import diff_snapshots
from swcraft_tools.impact import impact_of
from swcraft_tools.model_state import load_model_state
from swcraft_tools.passport import compute_passport
from swcraft_tools.snapshot import ModelSnapshot, load_snapshot
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml, generate_diagrams, generate_needs

from .mutations import (
    CHANGED_DESCRIPTION,
    HUB_TYPE_NAME,
    MUTATION_ADDED_SWC_NAME,
    MUTATION_CHANGED_DATA_TYPE_NAME,
    MUTATION_REMOVED_INTERFACE_NAME,
    SPECIMEN,
    apply_fixed_mutations,
)

REAL_EXPORT_ENV_VAR = "SWCRAFT_RHAPSODY_EXPORT"
REAL_EXPORT_PATH = os.environ.get(REAL_EXPORT_ENV_VAR)

#: The committed reference architecture model, measured by the full-rebuild
#: scenario. Read only -- every round writes into a fresh temp dir.
REFERENCE_MODEL_ROOT = Path(__file__).resolve().parents[4] / "examples" / "basic" / "model"

#: The requirement-link rules a dangling or unversioned link would raise.
LINK_RULES = frozenset(
    {"trace.link-unknown-requirement", "trace.link-version-not-stated", "trace.link-needs-confirmation"}
)

#: The element counts a memory probe records, by the passport's layer each is read from.
Sizes = dict[str, int]


def _sizes(model_root: Path) -> Sizes:
    """The element counts of the model at *model_root*, as its passport gives them."""
    layers = compute_passport(model_root).passport["layers"]
    return {
        "n_swcs": layers["swc"],
        "n_interfaces": layers["interfaces"],
        "n_data_types": layers["application_types"] + layers["implementation_types"] + layers["base_types"],
        "n_connectors": layers["connectors"],
        "n_tasks": layers["tasks"],
        "n_sequences": layers["sequences"],
    }


# ---------------------------------------------------------------------------
# The fixed mutation's tests (unmarked -- part of the default `tools:test`
# run, not the benchmark suite).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def specimen_model(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, dict[str, Any]]]:
    """The specimen's elements by layer and identity, dumped by alias, read from a copy."""
    root = copy_specimen(SPECIMEN, tmp_path_factory.mktemp("roles") / SPECIMEN) / "model"
    state = load_model_state(root)
    return {
        "swc": {swc.name: swc.model_dump(by_alias=True) for swc in state["swc"]},
        "interfaces": {i.name: i.model_dump(by_alias=True) for i in state["interfaces"]},
        "implementation_types": {t.name: t.model_dump(by_alias=True) for t in state["implementation_types"]},
        "mapping_sets": {m.name: m.model_dump(by_alias=True) for m in state["mapping_sets"]},
        "compositions": {c.name: c.model_dump(by_alias=True) for c in state["compositions"]},
    }


def _implementation_type_references(model: dict[str, dict[str, dict[str, Any]]]) -> Counter[str]:
    """How many references name each implementation type: data elements, arguments, maps, other types."""
    named: Counter[str] = Counter()
    for interface in model["interfaces"].values():
        for element in interface.get("data_elements") or []:
            if isinstance(element, dict) and element.get("implementation_type"):
                named[element["implementation_type"]] += 1
        for operation in interface.get("operations") or []:
            for argument in (operation.get("arguments") or []) if isinstance(operation, dict) else []:
                if argument.get("implementation_type"):
                    named[argument["implementation_type"]] += 1
    for mapping_set in model["mapping_sets"].values():
        for entry in mapping_set.get("maps") or []:
            named[entry["implementation"]] += 1
    for data_type in model["implementation_types"].values():
        for key in ("base", "element_type", "target"):
            if data_type.get(key) in model["implementation_types"]:
                named[data_type[key]] += 1
        for element in data_type.get("elements") or []:
            named[element["type"]] += 1
    return named


def _hub_type(model: dict[str, dict[str, dict[str, Any]]]) -> str:
    """The implementation type the most references name; ties by name, code point."""
    named = _implementation_type_references(model)
    return min(model["implementation_types"], key=lambda name: (-named[name], name))


def _removed_interface(model: dict[str, dict[str, dict[str, Any]]]) -> str:
    """A client-server interface no port names; the first by name."""
    ports = [
        port for holder in ("swc", "compositions") for e in model[holder].values() for port in e.get("ports") or []
    ]
    spoken = {port.get("interface") for port in ports}
    return min(
        name
        for name, interface in model["interfaces"].items()
        if interface["kind"] == "client_server" and name not in spoken
    )


def _changed_data_type(model: dict[str, dict[str, dict[str, Any]]]) -> str:
    """A value type other than the hub type; the first by name."""
    hub = _hub_type(model)
    return min(name for name, t in model["implementation_types"].items() if t["kind"] == "value_type" and name != hub)


@pytest.mark.parametrize(
    ("name", "role"),
    [
        (HUB_TYPE_NAME, _hub_type),
        (MUTATION_REMOVED_INTERFACE_NAME, _removed_interface),
        (MUTATION_CHANGED_DATA_TYPE_NAME, _changed_data_type),
    ],
    ids=["hub-type", "removed-interface", "changed-data-type"],
)
def test_each_name_the_mutation_spells_holds_its_role(
    name: str, role: Any, specimen_model: dict[str, dict[str, dict[str, Any]]]
) -> None:
    assert role(specimen_model) == name


def test_the_added_component_is_absent_from_the_specimen(
    specimen_model: dict[str, dict[str, dict[str, Any]]],
) -> None:
    assert MUTATION_ADDED_SWC_NAME not in specimen_model["swc"]


def test_apply_fixed_mutations_add_remove_change(tmp_path: Path) -> None:
    base_root = copy_specimen(SPECIMEN, tmp_path / "base") / "model"
    mutated_root = copy_specimen(SPECIMEN, tmp_path / "mutated") / "model"
    apply_fixed_mutations(mutated_root)

    old_snapshot = load_snapshot(str(base_root))
    new_snapshot = load_snapshot(str(mutated_root))

    old_swc_names = {swc.name for swc in old_snapshot.swcs}
    new_swc_names = {swc.name for swc in new_snapshot.swcs}
    assert new_swc_names - old_swc_names == {MUTATION_ADDED_SWC_NAME}
    assert old_swc_names <= new_swc_names

    old_iface_names = {iface.name for iface in old_snapshot.interfaces}
    new_iface_names = {iface.name for iface in new_snapshot.interfaces}
    assert old_iface_names - new_iface_names == {MUTATION_REMOVED_INTERFACE_NAME}
    assert new_iface_names <= old_iface_names

    [changed] = [t for t in new_snapshot.implementation_types if t.name == MUTATION_CHANGED_DATA_TYPE_NAME]
    assert changed.description == CHANGED_DESCRIPTION

    state = load_model_state(mutated_root)
    [group] = {placement["group"] for placement in state.placement["swc"].values()}
    assert state.placement["swc"][MUTATION_ADDED_SWC_NAME] == {"group": group}, "written into the one group"
    first = min(state["requirements"], key=lambda requirement: requirement.id)
    [added] = [swc for swc in state["swc"] if swc.name == MUTATION_ADDED_SWC_NAME]
    assert added.satisfies == [f"{first.id}@{first.version}"]

    result = check_cross_layer_consistency(str(mutated_root))
    assert result.consistent, result.issues
    assert not [record for record in (*result.records, *result.warnings) if record.rule in LINK_RULES]


def test_apply_fixed_mutations_requires_the_benchmark_specimen(tmp_path: Path) -> None:
    """``example-minimal`` lacks the removed interface and the changed data type: refused, nothing written."""
    root = copy_specimen("example-minimal", tmp_path / "minimal") / "model"
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
    with pytest.raises(FileNotFoundError):
        apply_fixed_mutations(root)
    assert sorted(path.relative_to(root).as_posix() for path in root.rglob("*")) == before


def test_apply_fixed_mutations_refuses_a_model_holding_the_added_name(tmp_path: Path) -> None:
    root = copy_specimen(SPECIMEN, tmp_path / "taken") / "model"
    [group] = [entry for entry in (root / "swc").iterdir() if entry.is_dir()]
    (group / f"{MUTATION_ADDED_SWC_NAME}.yaml").write_text(
        f"name: {MUTATION_ADDED_SWC_NAME}\ntype: application\nhandle_termination_and_restart: no_support\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(FileExistsError):
        apply_fixed_mutations(root)
    assert (root / "interfaces" / f"{MUTATION_REMOVED_INTERFACE_NAME}.yaml").is_file(), "nothing removed"


# ---------------------------------------------------------------------------
# Benchmark fixtures + memory-probe helper
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workload(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Sizes]:
    model_root = copy_specimen(SPECIMEN, tmp_path_factory.mktemp("wl_base") / SPECIMEN) / "model"
    return model_root, _sizes(model_root)


@pytest.fixture(scope="module")
def workload_snapshots(
    workload: tuple[Path, Sizes], tmp_path_factory: pytest.TempPathFactory
) -> tuple[ModelSnapshot, ModelSnapshot]:
    """The base workload's snapshot and its mutated copy's, built outside any timed region."""
    model_root, _size = workload
    mutated_root = tmp_path_factory.mktemp("wl_mutated") / "model"
    shutil.copytree(model_root, mutated_root)
    apply_fixed_mutations(mutated_root)

    old_snapshot = load_snapshot(str(model_root))
    new_snapshot = load_snapshot(str(mutated_root))
    return old_snapshot, new_snapshot


def _record_memory_probe(bench: Any, size: Sizes, func: Any, *args: Any, **kwargs: Any) -> None:
    """Run one extra, deliberately UNTIMED invocation under tracemalloc.

    Store the peak (plus machine metadata) in `extra_info`."""
    gc.collect()
    tracemalloc.start()
    try:
        func(*args, **kwargs)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    bench.extra_info.update(
        {
            "peak_memory_mb": round(peak / (1024 * 1024), 3),
            "python_version": sys.version.split()[0],
            "os": platform.system(),
            "os_release": platform.release(),
            "cpu": platform.processor() or platform.machine(),
            **size,
        }
    )


# ---------------------------------------------------------------------------
# Timed scenarios
# ---------------------------------------------------------------------------


@pytest.mark.benchmark(group="load")
def test_benchmark_load_snapshot(benchmark: Any, workload: tuple[Path, Sizes]) -> None:
    model_root, size = workload

    result = benchmark.pedantic(load_snapshot, args=(str(model_root), None), rounds=5, iterations=1)
    assert len(result.swcs) == size["n_swcs"]

    _record_memory_probe(benchmark, size, load_snapshot, str(model_root), None)


@pytest.mark.benchmark(group="validate")
def test_benchmark_check_cross_layer_consistency(benchmark: Any, workload: tuple[Path, Sizes]) -> None:
    model_root, size = workload

    result = benchmark.pedantic(check_cross_layer_consistency, args=(str(model_root),), rounds=5, iterations=1)
    assert result.consistent, result.issues

    _record_memory_probe(benchmark, size, check_cross_layer_consistency, str(model_root))


@pytest.mark.benchmark(group="arxml")
def test_benchmark_generate_arxml(
    benchmark: Any, workload: tuple[Path, Sizes], tmp_path_factory: pytest.TempPathFactory
) -> None:
    model_root, size = workload
    output_path = tmp_path_factory.mktemp("arxml_out") / "model.arxml"

    result = benchmark.pedantic(generate_arxml, args=(str(model_root), str(output_path)), rounds=5, iterations=1)
    assert Path(result).is_file()
    assert Path(result).stat().st_size > 0

    memory_output_path = tmp_path_factory.mktemp("arxml_mem") / "model.arxml"
    _record_memory_probe(benchmark, size, generate_arxml, str(model_root), str(memory_output_path))


@pytest.mark.benchmark(group="diff")
def test_benchmark_diff_snapshots(
    benchmark: Any, workload: tuple[Path, Sizes], workload_snapshots: tuple[ModelSnapshot, ModelSnapshot]
) -> None:
    _model_root, size = workload
    old_snapshot, new_snapshot = workload_snapshots

    result = benchmark(diff_snapshots, old_snapshot, new_snapshot)
    assert result.swcs.added or result.swcs.removed or result.interfaces.removed

    _record_memory_probe(benchmark, size, diff_snapshots, old_snapshot, new_snapshot)


@pytest.mark.benchmark(group="impact")
def test_benchmark_impact_of(
    benchmark: Any, workload: tuple[Path, Sizes], workload_snapshots: tuple[ModelSnapshot, ModelSnapshot]
) -> None:
    _model_root, size = workload
    old_snapshot, _new_snapshot = workload_snapshots

    result = benchmark(impact_of, old_snapshot, HUB_TYPE_NAME)
    assert result, f"expected {HUB_TYPE_NAME!r} to have dependents in the {SPECIMEN} specimen"

    _record_memory_probe(benchmark, size, impact_of, old_snapshot, HUB_TYPE_NAME)


# ---------------------------------------------------------------------------
# A clean full rebuild of the reference architecture model. Report-only, like
# every scenario here: no assertion on the measured time -- a regression is
# read by a human rather than used to fail a build.
# ---------------------------------------------------------------------------


def _full_rebuild(model_root: Path, output_root: Path) -> None:
    """The reference build's whole generation chain, into a clean directory.

    The sequence `dev.py build` runs (check-model, then the three generators),
    in process, so the model chain is measured and not four interpreter startups.
    """
    result = check_cross_layer_consistency(str(model_root))
    assert result.consistent, result.issues
    generate_arxml(str(model_root), str(output_root / "model.arxml"))
    generate_diagrams(str(model_root), str(output_root / "views"), "reference")
    generate_needs(str(model_root), str(output_root / "needs.json"), "reference")


@pytest.mark.benchmark(group="reference_build")
def test_benchmark_reference_model_full_rebuild(benchmark: Any, tmp_path_factory: pytest.TempPathFactory) -> None:
    assert REFERENCE_MODEL_ROOT.is_dir(), f"{REFERENCE_MODEL_ROOT} is missing"

    timed_root = tmp_path_factory.mktemp("reference_build_timed")
    rounds = itertools.count()

    def _rebuild() -> Path:
        # A fresh directory per round: a rebuild that could reuse the previous
        # round's output would be measuring an incremental build, not a clean one.
        output_root = timed_root / f"round{next(rounds)}"
        output_root.mkdir()
        _full_rebuild(REFERENCE_MODEL_ROOT, output_root)
        return output_root

    produced = benchmark.pedantic(_rebuild, rounds=5, iterations=1)
    for name in ("model.arxml", "views/index.html", "needs.json"):
        assert (produced / name).stat().st_size > 0, f"{name} was not generated"

    memory_root = tmp_path_factory.mktemp("reference_build_memory")
    # `_record_memory_probe` records the element counts the reference model's passport gives.
    size = _sizes(REFERENCE_MODEL_ROOT)
    _record_memory_probe(benchmark, size, _full_rebuild, REFERENCE_MODEL_ROOT, memory_root)

    print("\n--- Reference-model clean full rebuild ---")
    print(f"model: {size['n_swcs']} SWCs, {size['n_interfaces']} interfaces, {size['n_data_types']} data types")
    print(f"peak memory (MB) = {benchmark.extra_info['peak_memory_mb']}")


# ---------------------------------------------------------------------------
# Rhapsody import scenario, on a real export: skipped whenever
# SWCRAFT_RHAPSODY_EXPORT is unset.
# ---------------------------------------------------------------------------


def _init_project(tmp_path: Path, name: str) -> Path:
    """Scaffold a fresh SWCraft project via the real `swcraft init` CLI path."""
    target = tmp_path / name
    argv = sys.argv
    try:
        sys.argv = ["swcraft", "init", str(target)]
        _swcraft_main()
    finally:
        sys.argv = argv
    return target


@pytest.mark.benchmark(group="rhapsody_import")
@pytest.mark.slow
@pytest.mark.skipif(
    not REAL_EXPORT_PATH,
    reason=(
        f"{REAL_EXPORT_ENV_VAR} is not set -- this benchmark only runs against the "
        "real tool exports the maintainer holds privately (never committed to the repo). "
        f"Set {REAL_EXPORT_ENV_VAR}=/path/to/export.arxml to run it."
    ),
)
def test_benchmark_rhapsody_import(benchmark: Any, tmp_path_factory: pytest.TempPathFactory) -> None:
    assert REAL_EXPORT_PATH, "guarded by module-level skipif"
    assert Path(REAL_EXPORT_PATH).is_file(), f"{REAL_EXPORT_ENV_VAR}={REAL_EXPORT_PATH!r} does not point to a real file"

    timed_tmp_path = tmp_path_factory.mktemp("rhapsody_timed")

    def _run_timed() -> Any:
        project = _init_project(timed_tmp_path, f"proj_{uuid.uuid4().hex}")
        return run_import(REAL_EXPORT_PATH, str(project / "swcraft.yaml"))

    report = benchmark.pedantic(_run_timed, rounds=1, warmup_rounds=0, iterations=1)
    assert report.total_imported > 0

    # Separate, deliberately UNTIMED invocation for the peak-memory number.
    memory_tmp_path = tmp_path_factory.mktemp("rhapsody_memory")

    def _run_for_memory() -> None:
        project = _init_project(memory_tmp_path, f"proj_{uuid.uuid4().hex}")
        run_import(REAL_EXPORT_PATH, str(project / "swcraft.yaml"))

    gc.collect()
    tracemalloc.start()
    try:
        _run_for_memory()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    benchmark.extra_info.update(
        {
            "peak_memory_mb": round(peak / (1024 * 1024), 3),
            "python_version": sys.version.split()[0],
            "os": platform.system(),
            "os_release": platform.release(),
            "cpu": platform.processor() or platform.machine(),
            "imported_swcs": report.imported_swcs,
            "imported_interfaces": report.imported_interfaces,
            "imported_data_types": report.imported_data_types,
            "imported_connectors": report.imported_connectors,
            "total_imported": report.total_imported,
        }
    )

    print("\n--- Rhapsody import benchmark ---")
    print(f"import time (mean, s) = {benchmark.stats.stats.mean if hasattr(benchmark, 'stats') else 'n/a'}")
    print(f"peak memory (MB)      = {benchmark.extra_info['peak_memory_mb']}")


# ---------------------------------------------------------------------------
# The cost of locating an element by content.
#
# `StagedChangeSet.materialize` alone, over an imported tool export's
# `implementation_types` layer, for a fifty-element `update` and a fifty-element
# `create` (of base types) change set: an update or remove finds its file
# through one identity -> file index per layer per materialization, a create
# asks `model_state.free_storage_path` at its write. Skipped with the same variable.
# ---------------------------------------------------------------------------

#: The size of the two change sets.
_SET_SIZE = 50


def _set_operations(model_root: Path) -> dict[str, list[dict[str, Any]]]:
    names = sorted(data_type.name for data_type in load_snapshot(str(model_root)).implementation_types)[:_SET_SIZE]
    return {
        "update": [
            {
                "operation": "update",
                "address": {"layer": "implementation_types", "element": name, "path": [{"field": "description"}]},
                "value": "D13 benchmark",
            }
            for name in names
        ],
        "create": [
            {
                "operation": "create",
                "address": {"layer": "base_types", "element": f"D13Bench_{index:02d}"},
                "value": {"name": f"D13Bench_{index:02d}", "kind": "base_type", "size": 8, "encoding": "unsigned"},
            }
            for index in range(_SET_SIZE)
        ],
    }


@pytest.mark.benchmark(group="materialize")
@pytest.mark.parametrize("verb", ["update", "create"])
@pytest.mark.skipif(
    not REAL_EXPORT_PATH,
    reason=f"{REAL_EXPORT_ENV_VAR} is not set -- this benchmark materializes over an imported export.",
)
def test_benchmark_materialize_fifty_data_types(
    benchmark: Any, tmp_path_factory: pytest.TempPathFactory, verb: str
) -> None:
    from swcraft_tools.editing.apply import apply_operations
    from swcraft_tools.editing.passes import pass_a
    from swcraft_tools.editing.staging import baseline_token, stage

    assert REAL_EXPORT_PATH, "guarded by the skipif"
    project = _init_project(tmp_path_factory.mktemp("d13"), "proj")
    run_import(REAL_EXPORT_PATH, str(project / "swcraft.yaml"), force=True)
    model_root = project / "model"
    operations = _set_operations(model_root)[verb]

    def _setup() -> tuple[tuple[Any, Any], dict[str, Any]]:
        request = pass_a(
            {
                "operation": "change_set",
                "baseline": baseline_token(model_root),
                "intent": "D13",
                "operations": operations,
            }
        )
        staging = stage(project / "swcraft.yaml")
        outcome = apply_operations(staging.snapshot, request.operations)
        assert not outcome.violations and not outcome.inapplicable and len(outcome.touched) == _SET_SIZE
        return (staging, outcome.touched), {}

    def _materialize(staging: Any, touched: Any) -> None:
        try:
            assert len(staging.materialize(touched)) == _SET_SIZE
        finally:
            staging.abandon()

    benchmark.pedantic(_materialize, setup=_setup, rounds=5, warmup_rounds=1, iterations=1)
    benchmark.extra_info.update({"verb": verb, "elements": _SET_SIZE, "python_version": sys.version.split()[0]})


# ---------------------------------------------------------------------------
# The requirements import's planner at scale.
#
# `plan_requirements_import` over an empty model and a synthetic interchange file
# of `requirements` new needs, every tenth a case variant of the one before it so
# the collision suffix is asked too. Each new requirement's file name is allocated
# by one `model_state.PlannedStorage`, which lists the directory once and reserves
# each answer as it gives it, so the planner stays linear. Two sizes, so the report shows the slope:
# a linear planner takes about four times as long for four times the needs.
# ---------------------------------------------------------------------------


def _synthetic_needs(count: int) -> Any:
    from conftest import need, needs_document
    from swcraft_tools.requirements_interchange import parse_needs_text

    ids = [f"SYN-{index:05d}" if index % 10 else f"syn-{index - 1:05d}" for index in range(1, count + 1)]
    return parse_needs_text(json.dumps(needs_document(*(need(requirement_id) for requirement_id in ids))))


@pytest.mark.benchmark(group="requirements_import")
@pytest.mark.parametrize("requirements", [1000, 4000])
def test_benchmark_plan_requirements_import(
    benchmark: Any, tmp_path_factory: pytest.TempPathFactory, requirements: int
) -> None:
    from conftest import make_empty_model_root
    from swcraft_tools.requirements_import import plan_requirements_import

    model_root = make_empty_model_root(tmp_path_factory.mktemp("requirements_import"))
    selected = _synthetic_needs(requirements)

    plan = benchmark.pedantic(plan_requirements_import, args=(model_root, selected), rounds=3, iterations=1)
    assert len(plan.writes) == requirements
    assert sum("~1.yaml" in planned.target for planned in plan.writes) == requirements // 10

    benchmark.extra_info.update({"requirements": requirements, "python_version": sys.version.split()[0]})
