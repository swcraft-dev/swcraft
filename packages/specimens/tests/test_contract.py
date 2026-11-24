from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from swcraft_specimens.architecture import build_architecture
from swcraft_specimens.contract import validate_contract
from swcraft_specimens.deterministic import SplitMix64
from swcraft_specimens.emit import build_file_map
from swcraft_specimens.errors import GenerationError
from swcraft_specimens.model import build_tiny_project
from swcraft_specimens.packaged import preset_path
from swcraft_specimens.placement import architecture_to_project, build_placement
from swcraft_specimens.preset import load_preset
from tests.oracle.new_kinds import project_with_later_kinds, project_with_new_kinds


def files() -> dict[str, bytes]:
    preset = load_preset(preset_path("small"))
    rng = SplitMix64(preset.seed)
    architecture = build_architecture(preset, rng)
    placement = build_placement(architecture, preset, rng)
    return build_file_map(architecture_to_project(placement, preset))


def add_key(tree: dict[str, bytes], path: str, object_path: list[str], key: str = "unexpected") -> dict[str, bytes]:
    result = dict(tree)
    value = yaml.safe_load(result[path].decode())
    target = value
    for part in object_path:
        target = target[int(part)] if part.isdigit() else target[part]
    target[key] = True
    result[path] = yaml.safe_dump(value, sort_keys=False).encode()
    return result


@pytest.mark.parametrize(
    "layer",
    [
        "requirements",
        "compositions",
        "swc",
        "interfaces",
        "data_types/base",
        "data_types/implementation",
        "connectors",
        "tasks",
        "hardware",
    ],
)
def test_unknown_top_level_property_is_rejected_for_every_emitted_layer(layer: str) -> None:
    tree = files()
    path = next(p for p in tree if p.startswith(f"model/{layer}/") and p.endswith(".yaml"))
    with pytest.raises(GenerationError, match=rf"{path}: unknown key 'unexpected'"):
        validate_contract(add_key(tree, path, []))


@pytest.mark.parametrize(
    "path,object_path",
    [
        ("swc", ["ports", "0"]),
        ("swc", ["runnables", "0"]),
        ("swc", ["runnables", "0", "events", "0"]),
        ("interfaces", ["data_elements", "0"]),
        ("connectors", ["from"]),
        ("tasks", ["runnables", "0"]),
    ],
)
def test_unknown_nested_property_reports_exact_object_path(path: str, object_path: list[str]) -> None:
    tree = files()
    candidates = [p for p in tree if p.startswith(f"model/{path}/") and p.endswith(".yaml")]
    file_path = next(p for p in candidates if _has_path(tree[p], object_path))
    with pytest.raises(GenerationError, match=rf"{file_path}.*unknown key 'unexpected'"):
        validate_contract(add_key(tree, file_path, object_path))


def _has_path(data: bytes, object_path: list[str]) -> bool:
    try:
        value = yaml.safe_load(data.decode())
        for part in object_path:
            value = value[int(part)] if part.isdigit() else value[part]
        return True
    except (KeyError, IndexError, TypeError):
        return False


def test_placement_metadata_is_not_a_physical_swc_key() -> None:
    tree = files()
    path = next(p for p in tree if p.startswith("model/swc/") and p.endswith(".yaml"))
    with pytest.raises(GenerationError, match=rf"{path}: unknown key 'group'"):
        validate_contract(add_key(tree, path, [], "group"))


def test_contract_gate_rejects_task_asil_but_preserves_supported_asil_fields() -> None:
    tree = files()
    task = next(p for p in tree if p.startswith("model/tasks/") and p.endswith(".yaml"))
    with pytest.raises(GenerationError, match=rf"{task}: unknown key 'asil'"):
        validate_contract(add_key(tree, task, [], "asil"))
    swc = next(p for p in tree if p.startswith("model/swc/") and p.endswith(".yaml"))
    requirement = next(p for p in tree if p.startswith("model/requirements/") and p.endswith(".yaml"))
    core = next(p for p in tree if p.startswith("model/hardware/") and yaml.safe_load(tree[p]).get("kind") == "core")
    assert "asil" in yaml.safe_load(tree[swc])
    assert "asil" in yaml.safe_load(tree[requirement])
    assert "asil_capability" in yaml.safe_load(tree[core])


def test_emitted_swc_uses_group_path_and_emitted_task_has_no_asil() -> None:
    tree = files()
    swc_path = next(p for p in tree if p.startswith("model/swc/") and p.endswith(".yaml"))
    task_path = next(p for p in tree if p.startswith("model/tasks/") and p.endswith(".yaml"))
    swc = yaml.safe_load(tree[swc_path])
    task = yaml.safe_load(tree[task_path])
    assert "group" not in swc
    assert swc_path.split("/")[2] in {
        "powertrain",
        "chassis",
        "vehicle_services",
        "energy_management",
        "thermal_management",
        "body_comfort",
        "safety_supervision",
        "platform_services",
    }
    assert "asil" not in task
    assert task["name"].split("_")[-1] in {"QM", "A", "B", "C", "D"}


def _implementation_paths(tree: dict[str, bytes], kind: str | None = None) -> list[str]:
    prefix = "model/data_types/implementation/"
    return [p for p in tree if p.startswith(prefix) and (kind is None or yaml.safe_load(tree[p])["kind"] == kind)]


def test_a_flat_data_type_file_is_refused_naming_its_path() -> None:
    tree = files()
    source = _implementation_paths(tree)[0]
    flat = "model/data_types/x.yaml"
    broken = dict(tree)
    broken[flat] = tree[source]
    message = rf"{flat}: a data type must sit in its layer directory"
    with pytest.raises(GenerationError, match=message):
        validate_contract(broken)


@pytest.mark.parametrize("retired", ["array", "record"])
def test_the_retired_array_and_record_kinds_are_refused(retired: str) -> None:
    tree = files()
    path = _implementation_paths(tree, "implementation_array")[0]
    broken = dict(tree)
    broken[path] = tree[path].replace(b"kind: implementation_array", f"kind: {retired}".encode())
    message = rf"{path}: kind '{retired}' is not allowed in data_types/implementation"
    with pytest.raises(GenerationError, match=message):
        validate_contract(broken)


def test_a_data_type_in_the_wrong_layer_directory_is_refused() -> None:
    tree = files()
    base = next(p for p in tree if p.startswith("model/data_types/base/") and p.endswith(".yaml"))
    moved = base.replace("/base/", "/implementation/")
    broken = {k: v for k, v in tree.items() if k != base}
    broken[moved] = tree[base]
    message = rf"{moved}: kind 'base_type' is not allowed in data_types/implementation"
    with pytest.raises(GenerationError, match=message):
        validate_contract(broken)


def test_an_implementation_array_requires_its_element_name() -> None:
    tree = files()
    path = _implementation_paths(tree, "implementation_array")[0]
    value = yaml.safe_load(tree[path])
    del value["element_name"]
    broken = dict(tree)
    broken[path] = yaml.safe_dump(value, sort_keys=False).encode()
    with pytest.raises(GenerationError, match=rf"{path}: missing key 'element_name'"):
        validate_contract(broken)


def test_the_tiny_record_passes_the_contract_and_its_elements_are_checked() -> None:
    tree = build_file_map(build_tiny_project(load_preset(Path("tests/fixtures/tiny.yaml"))))
    validate_contract(tree)
    path = "model/data_types/implementation/payload_record.yaml"
    with pytest.raises(GenerationError, match=rf"{path}.elements\[0\]: unknown key 'unexpected'"):
        validate_contract(add_key(tree, path, ["elements", "0"]))


def _first(tree: dict[str, bytes], prefix: str, key: str) -> str:
    return next(p for p in tree if p.startswith(prefix) and p.endswith(".yaml") and key in yaml.safe_load(tree[p]))


def _rewrite(tree: dict[str, bytes], path: str, change: Any) -> dict[str, bytes]:
    value = yaml.safe_load(tree[path].decode())
    change(value)
    broken = dict(tree)
    broken[path] = yaml.safe_dump(value, sort_keys=False).encode()
    return broken


def test_type_on_a_data_element_is_refused() -> None:
    tree = files()
    path = _first(tree, "model/interfaces/", "data_elements")
    broken = _rewrite(tree, path, lambda v: v["data_elements"][0].__setitem__("type", "u8"))
    with pytest.raises(GenerationError, match=rf"{path}.data_elements\[0\]: unknown key 'type'"):
        validate_contract(broken)


def test_type_on_an_argument_is_refused() -> None:
    tree = files()
    path = _first(tree, "model/interfaces/", "operations")
    broken = _rewrite(tree, path, lambda v: v["operations"][0]["arguments"][0].__setitem__("type", "u8"))
    message = rf"{path}.operations\[0\].arguments\[0\]: unknown key 'type'"
    with pytest.raises(GenerationError, match=message):
        validate_contract(broken)


@pytest.mark.parametrize("key", ["links", "status"])
def test_the_retired_requirement_keys_are_refused(key: str) -> None:
    tree = files()
    path = _first(tree, "model/requirements/", "id")
    with pytest.raises(GenerationError, match=rf"{path}: unknown key '{key}'"):
        validate_contract(add_key(tree, path, [], key))


@pytest.mark.parametrize(
    "prefix,key",
    [
        ("model/requirements/", "version"),
        ("model/requirements/", "source"),
        ("model/swc/", "handle_termination_and_restart"),
    ],
)
def test_the_new_required_keys_are_required(prefix: str, key: str) -> None:
    tree = files()
    path = _first(tree, prefix, key)
    broken = _rewrite(tree, path, lambda v: v.pop(key))
    with pytest.raises(GenerationError, match=rf"{path}: missing key '{key}'"):
        validate_contract(broken)


def test_satisfies_is_an_allowed_component_key() -> None:
    tree = files()
    path = _first(tree, "model/swc/", "satisfies")
    validate_contract(tree)
    assert yaml.safe_load(tree[path])["satisfies"]


@pytest.mark.parametrize("key", ["core", "wcet_us"])
def test_the_retired_task_keys_are_refused(key: str) -> None:
    tree = files()
    path = _first(tree, "model/tasks/", "partition")
    with pytest.raises(GenerationError, match=rf"{path}: unknown key '{key}'"):
        validate_contract(add_key(tree, path, [], key))


def _partition(tree: dict[str, bytes]) -> str:
    return next(
        p for p in tree if p.startswith("model/hardware/") and yaml.safe_load(tree[p]).get("kind") == "partition"
    )


def test_asil_on_a_partition_passes_the_contract() -> None:
    # A specimen's partition may be rated; a WATERS partition stays unrated, which the rendered-tree
    # oracle holds, not the key allowlist.
    tree = files()
    path = _partition(tree)
    validate_contract(_rewrite(tree, path, lambda v: v.__setitem__("asil", "QM")))


def test_another_key_on_a_partition_is_refused() -> None:
    tree = files()
    path = _partition(tree)
    with pytest.raises(GenerationError, match=rf"{path}: unknown key 'trust'"):
        validate_contract(add_key(tree, path, [], "trust"))


@pytest.mark.parametrize("key", ["name", "core"])
def test_a_partition_requires_its_name_and_its_core(key: str) -> None:
    tree = files()
    path = _partition(tree)
    broken = _rewrite(tree, path, lambda v: v.pop(key))
    with pytest.raises(GenerationError, match=rf"{path}: missing key '{key}'"):
        validate_contract(broken)


@pytest.mark.parametrize("key", ["partition", "wcet"])
def test_a_task_requires_its_partition_and_its_budget(key: str) -> None:
    tree = files()
    path = _first(tree, "model/tasks/", key)
    broken = _rewrite(tree, path, lambda v: v.pop(key))
    with pytest.raises(GenerationError, match=rf"{path}: missing key '{key}'"):
        validate_contract(broken)


def test_wcet_us_on_a_runnable_is_refused() -> None:
    tree = files()
    path = next(p for p in tree if p.startswith("model/swc/") and p.endswith(".yaml"))
    with pytest.raises(GenerationError, match=rf"{path}.runnables\[0\]: unknown key 'wcet_us'"):
        validate_contract(add_key(tree, path, ["runnables", "0"], "wcet_us"))


def test_a_runnable_budget_and_its_type_are_allowed_together() -> None:
    tree = files()
    path = next(p for p in tree if p.startswith("model/swc/") and p.endswith(".yaml"))
    runnable = yaml.safe_load(tree[path])["runnables"][0]
    assert runnable["wcet"] and runnable["wcet_type"] == "net"
    validate_contract(tree)


@pytest.mark.parametrize(("present", "absent"), [("wcet", "wcet_type"), ("wcet_type", "wcet")])
def test_a_runnable_budget_and_its_type_are_refused_alone(present: str, absent: str) -> None:
    tree = files()
    path = next(p for p in tree if p.startswith("model/swc/") and p.endswith(".yaml"))
    broken = _rewrite(tree, path, lambda v: v["runnables"][0].pop(absent))
    assert present in yaml.safe_load(broken[path])["runnables"][0]
    with pytest.raises(GenerationError, match=rf"{path}.runnables\[0\]: missing key '{absent}'"):
        validate_contract(broken)


def _set_kind(tree: dict[str, bytes], path: str, kind: str | None) -> dict[str, bytes]:
    result = dict(tree)
    value = yaml.safe_load(result[path].decode())
    if kind is None:
        del value["kind"]
    else:
        value["kind"] = kind
    result[path] = yaml.safe_dump(value, sort_keys=False).encode()
    return result


@pytest.mark.parametrize("layer", ["hardware", "interfaces"])
@pytest.mark.parametrize("kind", [None, "foo"])
def test_an_absent_or_unknown_kind_is_refused_naming_the_path(layer: str, kind: str | None) -> None:
    tree = files()
    path = next(p for p in tree if p.startswith(f"model/{layer}/") and p.endswith(".yaml"))
    with pytest.raises(GenerationError, match=rf"{path}: kind '{kind}' is not allowed in {layer}"):
        validate_contract(_set_kind(tree, path, kind))


@pytest.mark.parametrize("layer", ["hardware", "interfaces"])
def test_a_kind_dispatched_file_that_is_not_a_mapping_is_refused(layer: str) -> None:
    tree = dict(files())
    path = next(p for p in tree if p.startswith(f"model/{layer}/") and p.endswith(".yaml"))
    tree[path] = b"- not a mapping\n"
    with pytest.raises(GenerationError, match=rf"{path}: expected mapping"):
        validate_contract(tree)


def test_an_event_that_is_not_a_mapping_is_refused() -> None:
    tree = dict(files())
    path = next(p for p in tree if p.startswith("model/swc/") and p.endswith(".yaml"))
    value = yaml.safe_load(tree[path].decode())
    value["runnables"][0]["events"] = ["x"]
    tree[path] = yaml.safe_dump(value, sort_keys=False).encode()
    with pytest.raises(GenerationError, match=r"runnables\[0\]\.events\[0\]: expected mapping"):
        validate_contract(tree)


@pytest.mark.parametrize(
    ("path", "match"),
    [
        ("model/sequences/S.yaml", "unknown key 'anything'"),
        ("model/bogus/x.yaml", "no file is emitted in layer 'bogus'"),
        ("model/x.yaml", "a model file must sit in a layer"),
    ],
)
def test_a_yaml_file_in_no_emitted_layer_is_refused(path: str, match: str) -> None:
    tree = dict(files())
    tree[path] = b"anything: 1\n"
    with pytest.raises(GenerationError, match=rf"{path}: {match}"):
        validate_contract(tree)


@pytest.mark.parametrize("layer", ["hardware", "interfaces", "data_types/base", "data_types/implementation"])
@pytest.mark.parametrize("kind", [["core"], {"core": 1}])
def test_an_unhashable_kind_is_refused_naming_the_path(layer: str, kind: Any) -> None:
    tree = dict(files())
    path = next(p for p in tree if p.startswith(f"model/{layer}/") and p.endswith(".yaml"))
    value = yaml.safe_load(tree[path].decode())
    value["kind"] = kind
    tree[path] = yaml.safe_dump(value, sort_keys=False).encode()
    with pytest.raises(GenerationError, match=rf"{path}: kind '.*' is not allowed in {layer}"):
        validate_contract(tree)


@pytest.mark.parametrize(
    ("prefix", "has", "object_path", "key"),
    [
        ("model/swc/", "runnables", [], "runnables"),
        ("model/swc/", "ports", [], "ports"),
        ("model/swc/", "satisfies", [], "satisfies"),
        ("model/swc/", "runnables", ["runnables", "0"], "events"),
        ("model/swc/", "runnables", ["runnables", "0"], "reads"),
        ("model/compositions/", "members", [], "members"),
        ("model/compositions/", "delegations", [], "delegations"),
        ("model/interfaces/", "data_elements", [], "data_elements"),
        ("model/interfaces/", "operations", [], "operations"),
        ("model/interfaces/", "operations", ["operations", "0"], "arguments"),
        ("model/tasks/", "runnables", [], "runnables"),
    ],
)
@pytest.mark.parametrize("value", [None, "x", {"a": 1}])
def test_a_list_field_that_is_not_a_list_is_refused_naming_the_path(
    prefix: str, has: str, object_path: list[str], key: str, value: Any
) -> None:
    tree = dict(files())
    path = _first(tree, prefix, has)
    document = yaml.safe_load(tree[path].decode())
    target = document
    for part in object_path:
        target = target[int(part)] if part.isdigit() else target[part]
    target[key] = value
    tree[path] = yaml.safe_dump(document, sort_keys=False).encode()
    with pytest.raises(GenerationError, match=rf"{path}.*\.{key}: expected list"):
        validate_contract(tree)


def test_an_implementation_record_elements_field_that_is_not_a_list_is_refused() -> None:
    tree = build_file_map(build_tiny_project(load_preset(Path("tests/fixtures/tiny.yaml"))))
    path = "model/data_types/implementation/payload_record.yaml"
    document = yaml.safe_load(tree[path].decode())
    document["elements"] = None
    tree = dict(tree, **{path: yaml.safe_dump(document, sort_keys=False).encode()})
    with pytest.raises(GenerationError, match=rf"{path}\.elements: expected list"):
        validate_contract(tree)


# The new element kinds: an external interface stub and the five type layers, each admitted in its
# own layer only.


def _new_kinds_tree() -> dict[str, bytes]:
    return build_file_map(project_with_new_kinds())


NEW_KIND_FILES = (
    "model/interfaces/IfStub.yaml",
    "model/data_types/units/Un1.yaml",
    "model/data_types/compu_methods/Cm1.yaml",
    "model/data_types/compu_methods/Cm2.yaml",
    "model/data_types/constraints/Dc1.yaml",
    "model/data_types/application/At1.yaml",
    "model/data_types/mapping_sets/Ms1.yaml",
)


def test_the_contract_gate_accepts_every_new_kind_in_its_layer() -> None:
    tree = _new_kinds_tree()
    assert set(NEW_KIND_FILES) <= set(tree)
    validate_contract(tree)


@pytest.mark.parametrize("path", NEW_KIND_FILES)
def test_an_unknown_key_on_a_new_kind_is_refused(path: str) -> None:
    with pytest.raises(GenerationError, match=rf"{path}: unknown key 'unexpected'"):
        validate_contract(add_key(_new_kinds_tree(), path, []))


@pytest.mark.parametrize(
    ("path", "object_path"),
    [
        ("model/data_types/compu_methods/Cm2.yaml", ["entries", "0"]),
        ("model/data_types/mapping_sets/Ms1.yaml", ["maps", "0"]),
    ],
)
def test_an_unknown_key_on_a_new_nested_member_is_refused(path: str, object_path: list[str]) -> None:
    with pytest.raises(GenerationError, match=rf"{path}.*unknown key 'unexpected'"):
        validate_contract(add_key(_new_kinds_tree(), path, object_path))


@pytest.mark.parametrize(
    ("path", "key"),
    [
        ("model/interfaces/IfStub.yaml", "external_ref"),
        ("model/interfaces/IfStub.yaml", "external_dest"),
        ("model/data_types/compu_methods/Cm1.yaml", "unit"),
        ("model/data_types/compu_methods/Cm2.yaml", "entries"),
        ("model/data_types/mapping_sets/Ms1.yaml", "maps"),
    ],
)
def test_a_new_kind_requires_the_keys_the_generator_always_writes(path: str, key: str) -> None:
    broken = _rewrite(_new_kinds_tree(), path, lambda v: v.pop(key))
    with pytest.raises(GenerationError, match=rf"{path}: missing key '{key}'"):
        validate_contract(broken)


@pytest.mark.parametrize(
    ("source", "directory", "match"),
    [
        # A kindless element in a kind-dispatched layer has no kind there.
        ("model/data_types/units/Un1.yaml", "compu_methods", "kind 'None' is not allowed"),
        ("model/data_types/constraints/Dc1.yaml", "application", "kind 'None' is not allowed"),
        # A kind-dispatched element in a kindless layer carries a key that layer does not have.
        ("model/data_types/compu_methods/Cm1.yaml", "units", "unknown key 'kind'"),
        ("model/data_types/application/At1.yaml", "mapping_sets", "unknown key 'kind'"),
        # A kind in another kind-dispatched layer is not that layer's kind.
        ("model/data_types/application/At1.yaml", "implementation", "kind 'application_type'"),
        ("model/data_types/compu_methods/Cm1.yaml", "application", "kind 'linear'"),
        ("model/data_types/base/u8_base.yaml", "compu_methods", "kind 'base_type'"),
        ("model/interfaces/IfStub.yaml", "data_types/application", "kind 'external_interface'"),
    ],
)
def test_a_new_kind_in_the_wrong_directory_is_refused(source: str, directory: str, match: str) -> None:
    tree = _new_kinds_tree()
    name = source.rsplit("/", 1)[1]
    target = (
        f"model/{directory}/{name}" if directory.startswith("data_types/") else f"model/data_types/{directory}/{name}"
    )
    broken = {k: v for k, v in tree.items() if k != source}
    broken[target] = tree[source]
    with pytest.raises(GenerationError, match=rf"{target}: {match}"):
        validate_contract(broken)


def test_an_external_interface_is_refused_in_the_hardware_layer() -> None:
    tree = _new_kinds_tree()
    broken = dict(tree)
    broken["model/hardware/IfStub.yaml"] = tree["model/interfaces/IfStub.yaml"]
    match = r"model/hardware/IfStub.yaml: kind 'external_interface' is not allowed in hardware"
    with pytest.raises(GenerationError, match=match):
        validate_contract(broken)


@pytest.mark.parametrize(
    ("path", "key"),
    [
        ("model/data_types/compu_methods/Cm2.yaml", "entries"),
        ("model/data_types/mapping_sets/Ms1.yaml", "maps"),
    ],
)
@pytest.mark.parametrize("value", [None, "x", {"a": 1}])
def test_a_new_list_field_that_is_not_a_list_is_refused(path: str, key: str, value: Any) -> None:
    broken = _rewrite(_new_kinds_tree(), path, lambda v: v.__setitem__(key, value))
    with pytest.raises(GenerationError, match=rf"{path}\.{key}: expected list"):
        validate_contract(broken)


# --- the later kinds ----------------------------------------------------------------------------


def _later_kinds_tree() -> dict[str, bytes]:
    return build_file_map(project_with_later_kinds())


#: Each new key, the file and the object path that carries it in the later-kinds tree.
LATER_KEYS: tuple[tuple[str, list[str], str], ...] = (
    ("model/swc/dom01/Dom01_Swc001.yaml", ["ports", "0"], "satisfies"),
    ("model/swc/dom01/Dom01_Swc002.yaml", ["runnables", "0"], "satisfies"),
    ("model/interfaces/IfData.yaml", [], "satisfies"),
    ("model/compositions/Root.yaml", [], "satisfies"),
    ("model/tasks/Task_CORE0_B.yaml", [], "coexistence"),
    ("model/tasks/Task_CORE0_B.yaml", [], "satisfies"),
    ("model/hardware/CPU.yaml", [], "satisfies"),
    ("model/hardware/Pa1.yaml", [], "coexistence"),
    ("model/sequences/Sq1.yaml", [], "title"),
    ("model/sequences/Sq1.yaml", ["steps", "0"], "note"),
)


def test_the_contract_gate_admits_every_later_key() -> None:
    tree = _later_kinds_tree()
    validate_contract(tree)
    for path, object_path, key in LATER_KEYS:
        value = yaml.safe_load(tree[path])
        for part in object_path:
            value = value[int(part)] if part.isdigit() else value[part]
        assert key in value, (path, object_path, key)


@pytest.mark.parametrize(
    ("path", "object_path"),
    [
        ("model/sequences/Sq1.yaml", []),
        ("model/sequences/Sq1.yaml", ["steps", "0"]),
        ("model/swc/dom01/Dom01_Swc001.yaml", ["ports", "0"]),
        ("model/swc/dom01/Dom01_Swc002.yaml", ["runnables", "0"]),
        ("model/interfaces/IfData.yaml", []),
        ("model/interfaces/IfService.yaml", []),
        ("model/interfaces/IfStub.yaml", []),
        ("model/compositions/Root.yaml", []),
        ("model/tasks/Task_CORE0_B.yaml", []),
        ("model/hardware/CPU.yaml", []),
        ("model/hardware/CORE0.yaml", []),
        ("model/hardware/RAM_SHARED.yaml", []),
        ("model/hardware/Pa1.yaml", []),
        ("model/requirements/Rq1.yaml", []),
    ],
)
def test_an_unknown_key_beside_the_later_keys_is_refused(path: str, object_path: list[str]) -> None:
    with pytest.raises(GenerationError, match=rf"{path}.*unknown key 'unexpected'"):
        validate_contract(add_key(_later_kinds_tree(), path, object_path))


@pytest.mark.parametrize(
    ("path", "key"),
    [
        ("model/sequences/Sq1.yaml", "name"),
        ("model/sequences/Sq1.yaml", "task"),
        ("model/sequences/Sq1.yaml", "steps"),
    ],
)
def test_a_sequence_requires_its_name_its_task_and_its_steps(path: str, key: str) -> None:
    broken = _rewrite(_later_kinds_tree(), path, lambda v: v.pop(key))
    with pytest.raises(GenerationError, match=rf"{path}: missing key '{key}'"):
        validate_contract(broken)


@pytest.mark.parametrize("key", ["swc", "runnable"])
def test_a_sequence_step_requires_its_swc_and_its_runnable(key: str) -> None:
    path = "model/sequences/Sq1.yaml"
    broken = _rewrite(_later_kinds_tree(), path, lambda v: v["steps"][0].pop(key))
    with pytest.raises(GenerationError, match=rf"{path}.steps\[0\]: missing key '{key}'"):
        validate_contract(broken)


@pytest.mark.parametrize(
    ("path", "object_path"),
    [
        ("model/tasks/Task_CORE0_B.yaml", []),
        ("model/hardware/CPU.yaml", []),
        ("model/swc/dom01/Dom01_Swc001.yaml", ["ports", "0"]),
        ("model/swc/dom01/Dom01_Swc002.yaml", ["runnables", "0"]),
    ],
)
def test_a_satisfies_that_is_not_a_list_is_refused(path: str, object_path: list[str]) -> None:
    def broken(value: Any) -> None:
        for part in object_path:
            value = value[int(part)] if part.isdigit() else value[part]
        value["satisfies"] = "Rq1@2"

    with pytest.raises(GenerationError, match=r"satisfies: expected list"):
        validate_contract(_rewrite(_later_kinds_tree(), path, broken))
