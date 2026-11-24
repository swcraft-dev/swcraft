"""The committed examples/basic/ directory is a valid SWCraft project.

It loads through load_project, carries every declared layer's directory, and its seeded `hardware/`
layer is complete under `strict` enforcement.
"""

from __future__ import annotations

import pathlib

from swcraft_tools.model_registry import production_registry
from swcraft_tools.project import load_project
from swcraft_tools.reader import read_hardware, read_swc_model, read_tasks
from swcraft_tools.resource_analysis import analyze_resource_budgets
from swcraft_tools.validator import check_cross_layer_consistency, check_hardware_topology

_EXAMPLES_BASIC = str(pathlib.Path(__file__).parents[3] / "examples" / "basic")

#: Every declared layer's storage directory, read off the contract rather than listed: `swcraft init`
#: creates every directory in this set, so the example carries them all.
_REQUIRED_MODEL_DIRS = {facts.storage.directory for facts in production_registry().layers}


def test_load_project_from_examples_basic() -> None:
    """load_project("examples/basic") returns a project with all resolved paths inside examples/basic/."""
    project = load_project(_EXAMPLES_BASIC)

    assert project is not None, "load_project returned None for examples/basic/"
    assert project.name == "swcraft-basic-example"
    assert project.schema_version == "1.0"

    examples_basic_path = pathlib.Path(_EXAMPLES_BASIC).resolve()

    assert pathlib.Path(project.resolved_project_root).is_absolute()
    assert pathlib.Path(project.resolved_model_root).is_absolute()
    assert pathlib.Path(project.resolved_dist_root).is_absolute()
    assert pathlib.Path(project.resolved_views_root).is_absolute()
    assert pathlib.Path(project.resolved_arxml_path).is_absolute()
    assert pathlib.Path(project.resolved_needs_path).is_absolute()

    assert project.resolved_project_root == str(examples_basic_path)
    assert project.resolved_model_root == str(examples_basic_path / "model")
    assert project.resolved_dist_root == str(examples_basic_path / "dist")
    assert project.resolved_views_root == str(examples_basic_path / "views")
    assert project.resolved_arxml_path == str(examples_basic_path / "dist" / "model.arxml")
    assert project.resolved_needs_path == str(examples_basic_path / "dist" / "needs.json")


def test_examples_basic_model_dirs_exist() -> None:
    """Every declared layer's model subdirectory exists under resolved_model_root."""
    project = load_project(_EXAMPLES_BASIC)

    assert project is not None, "load_project returned None for examples/basic/"

    model_root = pathlib.Path(project.resolved_model_root)
    assert model_root.is_dir(), f"model_root does not exist: {model_root}"

    missing = {subdir for subdir in _REQUIRED_MODEL_DIRS if not (model_root / subdir).is_dir()}
    assert not missing, f"Missing model subdirectories: {missing}"


class TestExampleHardwareLayer:
    """The seeded `hardware/` layer of examples/basic/ stays complete.

    Every element defaults to `enforcement: strict` (a breach is an error and every consumer declares
    its budget), so a task on an undeclared core, an SWC budgeting an undeclared memory, or no
    processor would make `python scripts/dev.py build` fail on the repository's own example.
    """

    def _model_root(self) -> str:
        project = load_project(_EXAMPLES_BASIC)
        assert project is not None, "load_project returned None for examples/basic/"
        return project.resolved_model_root

    def test_all_four_kinds_are_declared(self) -> None:
        """The example declares a processor, a core, a memory and a partition."""
        elements = read_hardware(self._model_root())

        assert {element.kind for element in elements} == {"processor", "core", "memory", "partition"}
        assert all(element.enforcement == "strict" for element in elements if element.kind in ("core", "memory"))

    def test_the_layer_is_topologically_sound(self) -> None:
        """No structural finding: one processor, and every reference resolves."""
        result = check_hardware_topology(self._model_root())

        assert result.consistent, f"examples/basic/ hardware findings: {result.issues}"
        assert result.records == ()

    def test_every_task_names_a_declared_partition_on_a_declared_core(self) -> None:
        model_root = self._model_root()
        hardware = read_hardware(model_root)
        cores = {element.name for element in hardware if element.kind == "core"}
        partitions = {element.name: element.core for element in hardware if element.kind == "partition"}

        assert cores, "examples/basic/ declares no core"
        assert partitions, "examples/basic/ declares no partition"
        for task in read_tasks(model_root):
            assert task.partition in partitions, f"task {task.name} runs in undeclared partition {task.partition}"
            assert partitions[task.partition] in cores, f"partition {task.partition} is on an undeclared core"

    def test_every_consumer_declares_a_budget_against_a_declared_memory(self) -> None:
        """No SWC without a budget, no budget without a memory."""
        model_root = self._model_root()
        memories = {element.name for element in read_hardware(model_root) if element.kind == "memory"}

        assert memories, "examples/basic/ declares no memory"
        for swc in read_swc_model(model_root):
            assert swc.memory, f"SWC {swc.name} declares no memory budget"
            assert set(swc.memory) <= memories, f"SWC {swc.name} budgets an undeclared memory"

    def test_consumption_stays_within_every_threshold(self) -> None:
        """Headroom is positive everywhere, computed from the model as a second witness of the analysis."""
        model_root = self._model_root()
        swcs = read_swc_model(model_root)

        for memory in (e for e in read_hardware(model_root) if e.kind == "memory"):
            consumption = sum(swc.memory.get(memory.name, 0) for swc in swcs)
            assert consumption < memory.size * memory.threshold, f"memory {memory.name} is over its threshold"

    def test_seeded_budgets_produce_no_findings_at_all(self) -> None:
        """No structural finding, no missing budget, no breach, no RM violation."""
        analysis = analyze_resource_budgets(self._model_root())

        messages = [f"{f.severity}: {f.message}" for f in analysis.all_findings()]
        assert messages == [], f"examples/basic/ resource findings: {messages}"

    def test_cross_layer_consistency_still_passes(self) -> None:
        """`swcraft check-model` (the reference build chain's first step) stays green."""
        result = check_cross_layer_consistency(self._model_root())

        assert result.consistent, f"examples/basic/ consistency issues: {result.issues}"
