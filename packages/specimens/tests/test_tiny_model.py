from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from swcraft_specimens.architecture import build_architecture
from swcraft_specimens.deterministic import SplitMix64
from swcraft_specimens.emit import STORAGE_DIRECTORIES, build_file_map
from swcraft_specimens.errors import GenerationError
from swcraft_specimens.model import Project, build_tiny_project, partition_name, validate_project
from swcraft_specimens.packaged import preset_path
from swcraft_specimens.placement import architecture_to_project, build_placement
from swcraft_specimens.preset import load_preset
from swcraft_specimens.toolchain import explicit_toolchain_version
from swcraft_specimens.validate import validate_file_map
from tests.oracle.generator_start import generator_command
from tests.oracle.swcraft_gate import assert_swcraft_clean

ROOT = Path(__file__).parent
TINY = ROOT / "fixtures" / "tiny.yaml"
PYTHON = sys.executable
SWCRAFT = os.environ.get("SWCRAFT_BIN")
GATE_COMMANDS = [
    ("check-model", "--format", "json"),
    ("validate",),
    ("generate", "arxml"),
    ("generate", "diagrams"),
    ("generate", "needs"),
    ("resource-report",),
]


def tiny_project() -> Project:
    return build_tiny_project(load_preset(TINY))


def small_project() -> Project:
    return generated_project(preset_path("small"))


def generated_project(preset_path: Path) -> Project:
    preset = load_preset(preset_path)
    rng = SplitMix64(preset.seed)
    architecture = build_architecture(preset, rng)
    return architecture_to_project(build_placement(architecture, preset, rng), preset)


def digest_map(path: Path) -> dict[str, str]:
    return {
        file.relative_to(path).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in sorted(path.rglob("*"), key=lambda item: item.as_posix())
        if file.is_file()
    }


def test_tiny_model_has_required_semantic_kinds_and_invariants() -> None:
    project = tiny_project()
    validate_project(project)
    assert len(project.components) == 2
    assert len(project.domains) == 1
    assert len(project.cores) == 2
    assert len(project.compositions) == 1 and project.compositions[0].root
    assert {type(item).__name__ for item in project.data_types} == {
        "BaseType",
        "ValueType",
        "ArrayType",
        "RecordType",
    }
    assert {item.kind for item in project.interfaces} == {"sender_receiver", "client_server"}
    assert {item.kind for item in project.connectors} == {"sender_receiver", "client_server"}
    assert {r.name for c in project.components for r in c.runnables} >= {"write", "read", "call"}
    assert {m.name for m in project.compositions[0].members} == {c.name for c in project.components}
    assert {name for c in project.components for name, _ in c.memory} == {m.name for m in project.memories}


# The fifteen storage directories, in contract order.
CONTRACT_ORDER = (
    "swc",
    "interfaces",
    "connectors",
    "data_types/application",
    "data_types/implementation",
    "data_types/base",
    "requirements",
    "compositions",
    "tasks",
    "sequences",
    "hardware",
    "data_types/mapping_sets",
    "data_types/units",
    "data_types/compu_methods",
    "data_types/constraints",
)
EMPTY_DIRECTORIES = {
    "data_types/application",
    "sequences",
    "data_types/mapping_sets",
    "data_types/units",
    "data_types/compu_methods",
    "data_types/constraints",
}


def _parsed(files: dict[str, bytes], path: str) -> Any:
    import yaml

    return yaml.safe_load(files[path].decode("utf-8"))


def test_storage_directories_are_the_fifteen_in_contract_order() -> None:
    assert STORAGE_DIRECTORIES == CONTRACT_ORDER


@pytest.mark.parametrize("builder", ["tiny", "small"])
def test_every_directory_is_written_with_a_gitkeep_in_exactly_the_empty_ones(builder: str) -> None:
    files = build_file_map(tiny_project() if builder == "tiny" else small_project())
    for directory in STORAGE_DIRECTORIES:
        assert any(path.startswith(f"model/{directory}/") for path in files), directory
    gitkeeps = {p.removeprefix("model/").removesuffix("/.gitkeep") for p in files if p.endswith("/.gitkeep")}
    assert gitkeeps == EMPTY_DIRECTORIES
    assert all(files[f"model/{d}/.gitkeep"] == b"" for d in EMPTY_DIRECTORIES)
    assert all(p.endswith((".yaml", ".gitkeep")) or p == "swcraft.yaml" for p in files)


@pytest.mark.parametrize("builder", ["tiny", "small"])
def test_data_types_sit_in_their_layer_directories(builder: str) -> None:
    files = build_file_map(tiny_project() if builder == "tiny" else small_project())
    data_type_paths = [p for p in files if p.startswith("model/data_types/") and p.endswith("yaml")]
    assert data_type_paths
    assert not any(len(p.split("/")) == 3 for p in files if p.startswith("model/data_types/"))
    for path in data_type_paths:
        kind = _parsed(files, path)["kind"]
        layer = path.split("/")[2]
        assert layer == ("base" if kind == "base_type" else "implementation"), (path, kind)
        assert kind in {"base_type", "value_type", "implementation_array", "implementation_record"}
    kinds = {_parsed(files, path)["kind"] for path in data_type_paths}
    assert {"base_type", "value_type", "implementation_array"} <= kinds


@pytest.mark.parametrize("builder", ["tiny", "small"])
def test_every_array_is_an_implementation_array_with_its_element_name_last(builder: str) -> None:
    files = build_file_map(tiny_project() if builder == "tiny" else small_project())
    implementation = [p for p in files if p.startswith("model/data_types/implementation/")]
    arrays = [_parsed(files, p) for p in implementation]
    arrays = [a for a in arrays if a["kind"] == "implementation_array"]
    assert arrays
    for array in arrays:
        assert list(array) == ["name", "kind", "element_type", "length", "element_name"]
        assert array["element_name"] == "Element"


def test_the_tiny_record_is_an_implementation_record_with_elements() -> None:
    files = build_file_map(tiny_project())
    record = _parsed(files, "model/data_types/implementation/payload_record.yaml")
    assert record == {
        "name": "payload_record",
        "kind": "implementation_record",
        "elements": [{"name": "value", "type": "u8"}, {"name": "payload", "type": "payload_array"}],
    }
    assert list(record) == ["name", "kind", "elements"]


def test_file_map_is_canonical_and_utf8(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    for output in (first, second):
        output.mkdir()
        from swcraft_specimens.emit import materialize

        materialize(build_file_map(tiny_project()), output)
    assert digest_map(first) == digest_map(second)
    for relative, digest in digest_map(first).items():
        data = (first / relative).read_bytes()
        data.decode("utf-8")
        assert b"\r" not in data
        assert b"&id" not in data and b"*id" not in data
        assert str(tmp_path) not in data.decode("utf-8", errors="ignore")
    with explicit_toolchain_version("0.22.0"):
        manifest = build_file_map(tiny_project())["swcraft.yaml"].decode("utf-8")
    assert (
        manifest.index("schema_version")
        < manifest.index("toolchain_version")
        < manifest.index("model_root")
        < manifest.index("outputs")
    )


def test_manifest_names_the_found_swcraft_release_and_the_needs_folder() -> None:
    import yaml

    # The release found at run time is none here (the autouse fixture), so no key;
    # `tests/test_toolchain_version.py` covers the installed cases.
    manifest = yaml.safe_load(build_file_map(tiny_project())["swcraft.yaml"].decode("utf-8"))
    assert "toolchain_version" not in manifest
    with explicit_toolchain_version("0.21.0"):
        named = yaml.safe_load(build_file_map(tiny_project())["swcraft.yaml"].decode("utf-8"))
    assert named["toolchain_version"] == "0.21.0"
    assert manifest["outputs"] == {"arxml": "dist/model.arxml", "needs": "dist/needs.json"}


@pytest.mark.parametrize("value", [">=0.19", "latest"])
def test_validate_file_map_refuses_a_manifest_that_names_no_exact_release(value: str) -> None:
    project = tiny_project()
    with explicit_toolchain_version("0.21.0"):
        files = build_file_map(project)
    files["swcraft.yaml"] = files["swcraft.yaml"].replace(
        b'toolchain_version: "0.21.0"', f'toolchain_version: "{value}"'.encode()
    )
    assert f'"{value}"'.encode() in files["swcraft.yaml"]
    with pytest.raises(GenerationError, match="manifest version mismatch"):
        validate_file_map(files, project, task_overhead_us=5)


def test_generation_is_hash_seed_independent(tmp_path: Path) -> None:
    maps = []
    for seed in ("1", "999"):
        output = tmp_path / seed
        environment = dict(os.environ, PYTHONHASHSEED=seed)
        result = subprocess.run(
            [PYTHON, "-m", "swcraft_specimens", "generate", str(TINY), "--out", str(output)],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
        maps.append(digest_map(output))
    assert maps[0] == maps[1]


def test_cli_generation_missing_and_empty_output(tmp_path: Path) -> None:
    for output in (tmp_path / "missing", tmp_path / "empty"):
        output.mkdir(exist_ok=True)
        result = subprocess.run(
            [PYTHON, "-m", "swcraft_specimens", "generate", str(TINY), "--out", str(output)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
        assert (output / "swcraft.yaml").exists()


def test_general_builder_accepts_non_tiny_shape(tmp_path: Path) -> None:
    preset = tmp_path / "bad.yaml"
    preset.write_text(TINY.read_text(encoding="utf-8").replace("components: 2", "components: 3"), encoding="utf-8")
    output = tmp_path / "out"
    result = subprocess.run(
        [PYTHON, "-m", "swcraft_specimens", "generate", str(preset), "--out", str(output)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert (output / "swcraft.yaml").exists()


@pytest.mark.integration
def test_external_swcraft_gate(tmp_path: Path) -> None:
    if not SWCRAFT:
        pytest.skip("SWCRAFT_BIN is not set")
    output = tmp_path / "out"
    result = subprocess.run(
        [*generator_command(), "generate", str(TINY), "--out", str(output)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    version = subprocess.run([SWCRAFT, "--version"], capture_output=True, text=True, encoding="utf-8", check=True)
    assert version.stdout.startswith("swcraft ")
    assert_swcraft_clean(SWCRAFT, output / "swcraft.yaml", GATE_COMMANDS)
    assert (output / "dist/needs.json").is_file()
    assert (output / "dist/model.arxml").exists()
    assert (output / "views/report.txt").exists()


def _yaml_docs(files: dict[str, bytes], prefix: str) -> dict[str, Any]:
    return {p: _parsed(files, p) for p in files if p.startswith(prefix) and p.endswith(".yaml")}


@pytest.mark.parametrize("builder", ["tiny", "small"])
def test_interfaces_name_the_implementation_type_in_the_retired_slot(builder: str) -> None:
    files = build_file_map(tiny_project() if builder == "tiny" else small_project())
    elements = arguments = 0
    for interface in _yaml_docs(files, "model/interfaces/").values():
        for element in interface.get("data_elements", []):
            assert list(element) == ["name", "implementation_type"]
            elements += 1
        for operation in interface.get("operations", []):
            for argument in operation["arguments"]:
                assert list(argument) == ["name", "direction", "implementation_type"]
                arguments += 1
    assert elements and arguments


@pytest.mark.parametrize("builder", ["tiny", "small"])
def test_components_satisfy_exactly_the_requirements_that_list_them(builder: str) -> None:
    project = tiny_project() if builder == "tiny" else small_project()
    files = build_file_map(project)
    components = _yaml_docs(files, "model/swc/")
    emitted = {(doc["name"], entry) for doc in components.values() for entry in doc.get("satisfies", [])}
    expected = {(link.element, f"{r.id}@1") for r in project.requirements for link in r.links}
    assert emitted == expected
    for doc in components.values():
        if "satisfies" in doc:
            assert doc["satisfies"] and doc["satisfies"] == sorted(doc["satisfies"])
        assert doc["handle_termination_and_restart"] == "no_support"
        keys = list(doc)
        tail = ["asil", "handle_termination_and_restart"]
        tail += ["satisfies"] if "satisfies" in doc else []
        assert keys[-len(tail) :] == tail, keys
    satisfied = {entry.split("@")[0] for _, entry in emitted}
    assert satisfied == {r.id for r in project.requirements}


@pytest.mark.parametrize("builder", ["tiny", "small"])
def test_requirements_carry_version_and_source_and_no_links(builder: str) -> None:
    files = build_file_map(tiny_project() if builder == "tiny" else small_project())
    requirements = _yaml_docs(files, "model/requirements/")
    assert requirements
    for doc in requirements.values():
        assert list(doc) == ["id", "title", "description", "asil", "version", "source"]
        assert doc["version"] == 1 and doc["source"] == "architecture"
        assert "links" not in doc and "status" not in doc


@pytest.mark.parametrize("builder", ["tiny", "small"])
def test_one_unrated_partition_per_core_is_written_after_the_memories(builder: str) -> None:
    project = tiny_project() if builder == "tiny" else small_project()
    files = build_file_map(project)
    hardware = [p for p in files if p.startswith("model/hardware/") and p.endswith(".yaml")]
    kinds = [_parsed(files, p)["kind"] for p in hardware]
    expected = ["processor"] + ["core"] * len(project.cores)
    expected += ["memory"] * len(project.memories) + ["partition"] * len(project.cores)
    assert kinds == expected
    partitions = {p: _parsed(files, p) for p in hardware if _parsed(files, p)["kind"] == "partition"}
    assert partitions == {
        f"model/hardware/Partition_{core.name}.yaml": {
            "name": f"Partition_{core.name}",
            "kind": "partition",
            "core": core.name,
        }
        for core in project.cores
    }
    for doc in partitions.values():
        assert list(doc) == ["name", "kind", "core"]


@pytest.mark.parametrize("builder", ["tiny", "small"])
def test_tasks_name_their_partition_and_a_budget_in_its_unit_in_the_retired_slots(
    builder: str,
) -> None:
    project = tiny_project() if builder == "tiny" else small_project()
    files = build_file_map(project)
    for task in project.tasks:
        doc = _parsed(files, f"model/tasks/{task.name}.yaml")
        assert list(doc) == ["name", "period", "priority", "partition", "wcet", "runnables"]
        assert doc["partition"] == partition_name(task.core)
        assert doc["wcet"] == f"{task.wcet_us}us"
        assert "core" not in doc and "wcet_us" not in doc


def test_runnables_end_with_the_calibrated_budget_and_its_type_when_switched_on() -> None:
    project = small_project()
    assert project.emit_runnable_wcet
    preset = load_preset(preset_path("small"))
    raw = {
        (c.name, r.name): r.raw_execution_us
        for c in build_architecture(preset, SplitMix64(preset.seed)).components
        for r in c.runnables
    }
    files = build_file_map(project)
    differs = 0
    for component in project.components:
        doc = _parsed(files, f"model/swc/{component.group}/{component.name}.yaml")
        assert [r["name"] for r in doc["runnables"]] == [r.name for r in component.runnables]
        for runnable, rendered in zip(component.runnables, doc["runnables"], strict=True):
            assert list(rendered)[-2:] == ["wcet", "wcet_type"]
            assert rendered["wcet"] == f"{runnable.wcet_us}us"
            assert rendered["wcet_type"] == "net"
            assert "wcet_us" not in rendered
            differs += raw[(component.name, runnable.name)] != runnable.wcet_us
    # The calibrated integer is written, never the raw decimal draw; the two differ on this preset.
    assert differs


@pytest.mark.parametrize("builder", ["tiny", "small_scale"])
def test_no_runnable_carries_a_budget_when_switched_off(builder: str) -> None:
    if builder == "tiny":
        project = tiny_project()
    else:
        project = generated_project(ROOT / "fixtures" / "small_scale.yaml")
    assert not project.emit_runnable_wcet
    files = build_file_map(project)
    runnables = [r for doc in _yaml_docs(files, "model/swc/").values() for r in doc["runnables"]]
    assert runnables
    assert all("wcet" not in r and "wcet_type" not in r and "wcet_us" not in r for r in runnables)


def test_the_tiny_builder_keeps_runnable_budgets_off_because_its_runnables_cost_zero() -> None:
    from dataclasses import replace

    # The tiny builder gives every runnable wcet_us 0, and a zero budget is not taken, so this stays false.
    assert load_preset(TINY).emit_runnable_wcet is False
    project = tiny_project()
    assert {r.wcet_us for c in project.components for r in c.runnables} == {0}
    switched = replace(project, emit_runnable_wcet=True)
    with pytest.raises(GenerationError, match="runnable wcet '0us' is not positive"):
        validate_file_map(build_file_map(switched), switched, task_overhead_us=5)


@pytest.mark.parametrize("side", ["source", "target"])
def test_a_connector_endpoint_selecting_neither_swc_nor_composition_is_refused(side: str) -> None:
    from dataclasses import replace

    project = small_project()
    connector = project.connectors[0]
    endpoint = replace(getattr(connector, side), swc=None, composition=None)
    broken = replace(
        project,
        connectors=(replace(connector, **{side: endpoint}), *project.connectors[1:]),
    )
    with pytest.raises(
        GenerationError,
        match=f"connector endpoint selects neither swc nor composition: {connector.name}",
    ):
        validate_project(broken)


@pytest.mark.parametrize("stray", [False, True], ids=["one-domain-left", "a-stray-component"])
def test_a_malformed_domain_shape_keeps_the_domain_shape_message(stray: bool) -> None:
    # A root with one domain composition and a component beside it holds one composition member,
    # as the chain does; the domain names keep it in the domain shape, so the message is that shape's.
    from dataclasses import replace

    from swcraft_specimens.model import CompositionMember

    project = small_project()
    extra = (CompositionMember("Stray", project.components[0].name),) if stray else ()
    broken = replace(
        project,
        compositions=tuple(replace(c, members=(c.members[0], *extra)) if c.root else c for c in project.compositions),
    )
    with pytest.raises(GenerationError) as raised:
        validate_project(broken)
    assert str(raised.value) == "root composition must contain only domain compositions"
