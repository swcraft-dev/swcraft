"""``check_hardware_topology``: the ``hardware/`` layer read as a whole, and every reference into it.

**Why a validator pass owns the kind.** A contract ``referenceDecl`` names a target layer and identity field and has no
room for a kind; ``hardware/`` keeps its kinds in one identity namespace, so ``partition: RAM0`` resolves for the kernel
and only this pass refuses it. Its rules assert the rule id **and** the message text, which is what a user of ``swcraft
check-model`` reads. The ``hardware.`` prefix also covers three budget rules ``resource_analysis.py`` mints, so every
selection here goes through ``_topology_rules``.

What this module does **not** reach:

* **budget arithmetic**: ``check_resource_budgets``', which runs after this pass because it assumes every reference
  resolves (``packages/tools/tests/test_resource_*.py``);
* **placement**: ``hardware.unreachable-memory`` is a **declaration** check of whether the memory is declared reachable
  from the core; a linker script that disagrees with ``accessible`` passes every assertion here;
* **the aggregation path**: only the order of one model's findings (topology before budget) is asserted here;
  ``packages/tools/tests/test_validator.py`` asserts the aggregate's own answers.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import GOLDEN_MODEL_ROOTS
from swcraft_tools import validator
from swcraft_tools.diagnostics import Subject, field_step, key_step, member_step
from swcraft_tools.validator import CONSISTENCY_RULES, check_hardware_topology
from swcraft_tools.validator.hardware import _RESOURCE_FINDING_RULES

# Model fixtures

#: A TC397 layer, transcribed: one processor, six cores and five memories whose ``accessible`` lists name those cores.
TC397_HARDWARE: dict[str, str] = {
    "CPU": """
name: CPU
kind: processor
description: Infineon AURIX TC397, 6 TriCore 1.6.2P at 300 MHz
""",
    **{
        f"CORE{index}": f"""
name: CORE{index}
kind: core
threshold: 0.7
enforcement: strict
hw_id: {index}
description: CPU{index}
"""
        for index in range(6)
    },
    # One unrated partition per core, so a task placed on any of the six reaches its core through one.
    **{f"PARTITION{index}": f"name: PARTITION{index}\nkind: partition\ncore: CORE{index}\n" for index in range(6)},
    "RAM0": """
name: RAM0
kind: memory
type: RAM
size: 245760
accessible: [CORE0]
threshold: 0.85
enforcement: strict
""",
    "DLMU0": """
name: DLMU0
kind: memory
type: RAM
size: 65536
accessible: [CORE0, CORE1, CORE2, CORE3, CORE4, CORE5]
threshold: 0.8
enforcement: strict
""",
    "SHARED": """
name: SHARED
kind: memory
type: RAM
size: 786432
accessible: [CORE0, CORE1, CORE2, CORE3, CORE4, CORE5]
threshold: 0.8
enforcement: advisory
""",
    "PFLASH0": """
name: PFLASH0
kind: memory
type: Flash
size: 3145728
accessible: [CORE0, CORE1, CORE2, CORE3, CORE4, CORE5]
threshold: 0.9
enforcement: strict
""",
    "DFLASH": """
name: DFLASH
kind: memory
type: EEPROM
size: 1048576
accessible: [CORE0, CORE1, CORE2, CORE3, CORE4, CORE5]
threshold: 0.75
enforcement: advisory
""",
}


def _write_hardware(model_root: Path, files: dict[str, str]) -> None:
    directory = model_root / "hardware"
    directory.mkdir(parents=True, exist_ok=True)
    for stem, source in files.items():
        (directory / f"{stem}.yaml").write_text(source.lstrip(), encoding="utf-8")


def _edit(path: Path, **changes: Any) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.update(changes)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.fixture
def demo_model(tmp_path: Path) -> Path:
    """A writable copy of ``examples/basic/model``, which carries ``hardware/``."""
    model_root = tmp_path / "model"
    shutil.copytree(GOLDEN_MODEL_ROOTS["basic-4.2.2"], model_root)
    return model_root


@pytest.fixture
def tc397_model(demo_model: Path) -> Path:
    """The demo model with the TC397 layer in place of its own."""
    shutil.rmtree(demo_model / "hardware")
    _write_hardware(demo_model, TC397_HARDWARE)
    return demo_model


def _rules(model_root: Path) -> list[str]:
    return [record.rule for record in check_hardware_topology(str(model_root)).records]


def _topology_rules() -> set[str]:
    """The ``hardware.*`` rules **this pass** mints: the prefix minus the budget table.

    Read off ``validator.hardware._RESOURCE_FINDING_RULES`` rather than written out, so the two sets cannot drift apart.
    """
    return {rule for rule in CONSISTENCY_RULES if rule.startswith("hardware.")} - set(_RESOURCE_FINDING_RULES.values())


def _record(model_root: Path, rule: str) -> Any:
    result = check_hardware_topology(str(model_root))
    matching = [record for record in result.records if record.rule == rule]
    assert matching, f"expected {rule!r}, got {sorted({record.rule for record in result.records})}"
    assert len(matching) == 1, f"expected one {rule!r}, got {len(matching)}"
    return matching[0]


#: The perturbation literals the negative vectors below and `_ONE_BREAK_PER_RULE` share.
_SECOND_PROCESSOR = "name: CPU2\nkind: processor\n"
_CORE0_AGAIN_AS_A_PROCESSOR = "name: CORE0\nkind: processor\n"
_MEMORY_ACCESSIBLE_FROM_A_MEMORY = (
    "name: SHARED\nkind: memory\ntype: RAM\nsize: 4096\naccessible: [RAM0]\nthreshold: 0.5\n"
)


def _break_task_partition(partition: str) -> Callable[[Path], None]:
    return lambda model_root: _edit(model_root / "tasks" / "Task_10ms.yaml", partition=partition)


def _break_partition_core(core: str) -> Callable[[Path], None]:
    return lambda model_root: _edit(model_root / "hardware" / "PARTITION0.yaml", core=core)


def _break_swc_memory(memory: dict[str, int]) -> Callable[[Path], None]:
    return lambda model_root: _edit(model_root / "swc" / "example" / "ExampleSWC.yaml", memory=memory)


def _move_the_task_to_a_second_core(model_root: Path) -> None:
    _write_hardware(model_root, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
    _edit(model_root / "tasks" / "Task_10ms.yaml", partition="PARTITION1")


#: One perturbation of the demo model per rule this pass mints, as the negative
#: vectors below make them; keyed by the rule each is written to reach.
_ONE_BREAK_PER_RULE: tuple[tuple[str, Callable[[Path], None]], ...] = (
    ("hardware.layer-absent", lambda model_root: shutil.rmtree(model_root / "hardware")),
    ("hardware.no-processor", lambda model_root: (model_root / "hardware" / "CPU.yaml").unlink()),
    (
        "hardware.multiple-processors",
        lambda model_root: _write_hardware(model_root, {"CPU2": _SECOND_PROCESSOR}),
    ),
    (
        "hardware.duplicate-name",
        lambda model_root: _write_hardware(model_root, {"CORE0_again": _CORE0_AGAIN_AS_A_PROCESSOR}),
    ),
    ("hardware.undeclared-partition", _break_task_partition("Partition0")),
    ("hardware.partition-not-a-partition", _break_task_partition("CORE0")),
    ("hardware.partition-undeclared-core", _break_partition_core("Core0")),
    ("hardware.partition-core-not-a-core", _break_partition_core("RAM0")),
    ("hardware.undeclared-memory", _break_swc_memory({"RAM": 2048})),
    ("hardware.memory-not-a-memory", _break_swc_memory({"CORE0": 2048})),
    (
        "hardware.accessible-undeclared-core",
        lambda model_root: _edit(model_root / "hardware" / "RAM0.yaml", accessible=["CORE0", "CORE9"]),
    ),
    (
        "hardware.accessible-not-a-core",
        lambda model_root: _write_hardware(model_root, {"SHARED": _MEMORY_ACCESSIBLE_FROM_A_MEMORY}),
    ),
    ("hardware.unreachable-memory", _move_the_task_to_a_second_core),
)


# The positive vectors


class TestAConformingLayerReportsNothing:
    def test_the_migrated_demo_model_passes(self, demo_model: Path) -> None:
        result = check_hardware_topology(str(demo_model))

        assert result.consistent, result.issues
        assert result.records == ()

    def test_the_committed_demo_model_is_the_migrated_shape(self) -> None:
        """The committed hardware files, read from the checkout, not a copy.

        Every other test perturbs a copy, and ``swcraft validate``, the guide's executed commands and the editing
        vectors are seeded from this model.
        """
        model_root = GOLDEN_MODEL_ROOTS["basic-4.2.2"]
        assert not (model_root / "resources").exists()
        assert sorted(path.name for path in (model_root / "hardware").glob("*.yaml")) == [
            "CORE0.yaml",
            "CPU.yaml",
            "PARTITION0.yaml",
            "RAM0.yaml",
        ]
        task = yaml.safe_load((model_root / "tasks" / "Task_10ms.yaml").read_text(encoding="utf-8"))
        assert task["partition"] == "PARTITION0"
        partition = yaml.safe_load((model_root / "hardware" / "PARTITION0.yaml").read_text(encoding="utf-8"))
        assert (partition["kind"], partition["core"]) == ("partition", "CORE0")
        for swc_file in sorted((model_root / "swc").rglob("*.yaml"), key=lambda path: path.as_posix()):
            assert set(yaml.safe_load(swc_file.read_text(encoding="utf-8")).get("memory", {})) <= {"RAM0"}
        assert check_hardware_topology(str(model_root)).consistent

    def test_the_surveys_tc397_example_passes(self, tc397_model: Path) -> None:
        """Six cores, five memories and one processor: the multi-core case of every rule at once."""
        result = check_hardware_topology(str(tc397_model))

        assert result.consistent, result.issues

    def test_a_model_with_no_memory_at_all_passes(self, demo_model: Path) -> None:
        """``swcraft init`` scaffolds no memory (a ``size`` cannot be invented), so a layer declaring none passes."""
        (demo_model / "hardware" / "RAM0.yaml").unlink()
        for swc_file in sorted((demo_model / "swc").rglob("*.yaml"), key=lambda path: path.as_posix()):
            _edit(swc_file, memory={})

        assert check_hardware_topology(str(demo_model)).consistent


# One negative per rule: the id, and the message a user reads


class TestTheLayerItself:
    def test_an_absent_layer_is_reported_once(self, demo_model: Path) -> None:
        shutil.rmtree(demo_model / "hardware")

        record = _record(demo_model, "hardware.layer-absent")

        assert record.message == (
            "Project has no hardware/ layer; it is mandatory (a processor, its cores and its memories)."
        )
        assert record.subjects == (Subject("hardware"),)

    def test_an_absent_layer_reports_nothing_else(self, demo_model: Path) -> None:
        """An absent layer is the one finding: the pass returns rather than report every dangling reference."""
        shutil.rmtree(demo_model / "hardware")

        assert _rules(demo_model) == ["hardware.layer-absent"]

    def test_an_empty_layer_directory_is_present_and_has_no_processor(self, demo_model: Path) -> None:
        """Present-but-empty is a *different* finding from absent: the user is told what is missing from it."""
        for path in sorted((demo_model / "hardware").glob("*.yaml"), key=lambda path: path.as_posix()):
            path.unlink()

        assert "hardware.layer-absent" not in _rules(demo_model)
        assert "hardware.no-processor" in _rules(demo_model)

    def test_no_processor(self, demo_model: Path) -> None:
        (demo_model / "hardware" / "CPU.yaml").unlink()

        record = _record(demo_model, "hardware.no-processor")

        assert record.message == "hardware/ declares no 'kind: processor' element; exactly one is required."
        assert record.subjects == (Subject("hardware"),)

    def test_two_processors(self, demo_model: Path) -> None:
        _write_hardware(demo_model, {"CPU2": _SECOND_PROCESSOR})

        record = _record(demo_model, "hardware.multiple-processors")

        assert record.message == (
            "hardware/ declares 2 'kind: processor' elements (CPU, CPU2); exactly one is allowed."
        )
        # Every processor: the client chooses which one survives the repair.
        assert record.subjects == (Subject("hardware", "CPU"), Subject("hardware", "CPU2"))

    def test_a_duplicate_name_across_two_files(self, demo_model: Path) -> None:
        """Two files, one identity, deliberately of two different kinds: the kinds share one namespace."""
        _write_hardware(demo_model, {"CORE0_again": _CORE0_AGAIN_AS_A_PROCESSOR})

        record = _record(demo_model, "hardware.duplicate-name")

        assert record.message == "Duplicate hardware element name 'CORE0' declared in multiple files."
        assert record.subjects == (Subject("hardware", "CORE0"),)

    def test_a_duplicated_identity_resolves_to_the_first_file_in_reader_order(self, demo_model: Path) -> None:
        """A name two files claim resolves to the first file in reader (filename) order.

        Deliberate: ``hardware.duplicate-name`` already reports the duplicate, and the first claimant keeps the *other*
        findings stable instead of depending on which file was written last. Here the real core survives, so no
        ``hardware.partition-core-not-a-core`` is raised.

        What this does **not** pin: the order as a promise to a user; it is the reader's.
        """
        _write_hardware(
            demo_model,
            {"CORE0_again": "name: CORE0\nkind: memory\ntype: RAM\nsize: 8\naccessible: [CORE0]\nthreshold: 0.5\n"},
        )

        result = check_hardware_topology(str(demo_model))

        assert "hardware.duplicate-name" in {record.rule for record in result.records}
        assert "hardware.partition-core-not-a-core" not in {record.rule for record in result.records}


class TestTheTaskPartitionReference:
    """A task names a partition, the partition a core: the task's side of the two reference rules."""

    def test_a_task_in_an_undeclared_partition(self, demo_model: Path) -> None:
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="Partition0")

        record = _record(demo_model, "hardware.undeclared-partition")

        assert record.message == (
            "Task 'Task_10ms' runs in partition 'Partition0', which no hardware element declares "
            "(declared partitions: PARTITION0)."
        )
        assert record.subjects == (
            Subject("tasks", "Task_10ms", (field_step("partition"),)),
            Subject("hardware"),
        )
        assert record.target == "Partition0"

    def test_a_task_in_a_core(self, demo_model: Path) -> None:
        """The reference resolves; the kind is wrong -- the reason the pass owns the kind."""
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="CORE0")

        record = _record(demo_model, "hardware.partition-not-a-partition")

        assert record.message == "Task 'Task_10ms' runs in 'CORE0', which is a core, not a partition."
        assert record.subjects == (Subject("tasks", "Task_10ms", (field_step("partition"),)),)
        assert record.target == "CORE0"

    @pytest.mark.parametrize(("name", "kind"), [("CPU", "processor"), ("RAM0", "memory")])
    def test_a_task_in_another_kind(self, demo_model: Path, name: str, kind: str) -> None:
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition=name)

        assert _record(demo_model, "hardware.partition-not-a-partition").message == (
            f"Task 'Task_10ms' runs in '{name}', which is a {kind}, not a partition."
        )

    def test_the_declared_list_says_none_when_the_layer_declares_none(self, demo_model: Path) -> None:
        (demo_model / "hardware" / "PARTITION0.yaml").unlink()

        assert "(declared partitions: none)" in _record(demo_model, "hardware.undeclared-partition").message


class TestThePartitionCoreReference:
    def test_a_partition_on_an_undeclared_core(self, demo_model: Path) -> None:
        _edit(demo_model / "hardware" / "PARTITION0.yaml", core="Core0")

        record = _record(demo_model, "hardware.partition-undeclared-core")

        assert record.message == (
            "Partition 'PARTITION0' is on core 'Core0', which no hardware element declares (declared cores: CORE0)."
        )
        assert record.subjects == (
            Subject("hardware", "PARTITION0", (field_step("core"),)),
            Subject("hardware"),
        )
        assert record.target == "Core0"

    @pytest.mark.parametrize(("name", "kind"), [("RAM0", "memory"), ("CPU", "processor")])
    def test_a_partition_on_another_kind(self, demo_model: Path, name: str, kind: str) -> None:
        _edit(demo_model / "hardware" / "PARTITION0.yaml", core=name)

        record = _record(demo_model, "hardware.partition-core-not-a-core")

        assert record.message == f"Partition 'PARTITION0' is on '{name}', which is a {kind}, not a core."
        assert record.subjects == (Subject("hardware", "PARTITION0", (field_step("core"),)),)
        assert record.target == name

    def test_a_partition_on_a_partition(self, demo_model: Path) -> None:
        """The fourth kind reaches the rule too: a partition is not a core."""
        _write_hardware(demo_model, {"P_Nested": "name: P_Nested\nkind: partition\ncore: PARTITION0\n"})

        assert _record(demo_model, "hardware.partition-core-not-a-core").message == (
            "Partition 'P_Nested' is on 'PARTITION0', which is a partition, not a core."
        )

    def test_an_empty_partition_is_legal(self, demo_model: Path) -> None:
        """No rule requires a task in a partition."""
        _write_hardware(demo_model, {"P_Empty": "name: P_Empty\nkind: partition\ncore: CORE0\n"})

        assert check_hardware_topology(str(demo_model)).consistent

    def test_a_task_in_a_partition_on_an_undeclared_core_is_reported_once(self, demo_model: Path) -> None:
        """The task's reference resolves; the partition's does not -- one finding, the partition's."""
        _edit(demo_model / "hardware" / "PARTITION0.yaml", core="CORE9")

        assert _rules(demo_model) == ["hardware.partition-undeclared-core"]


class TestTheMemoryBudgetKeys:
    def test_an_swc_budgeting_an_undeclared_memory(self, demo_model: Path) -> None:
        _edit(demo_model / "swc" / "example" / "ExampleSWC.yaml", memory={"RAM": 2048})

        record = _record(demo_model, "hardware.undeclared-memory")

        assert record.message == (
            "SWC 'ExampleSWC' budgets memory 'RAM', which no hardware element declares (declared memories: RAM0)."
        )
        assert record.subjects == (
            Subject("swc", "ExampleSWC", (field_step("memory"), key_step("RAM"))),
            Subject("hardware"),
        )
        assert record.target == "RAM"

    def test_an_swc_budgeting_a_core(self, demo_model: Path) -> None:
        _edit(demo_model / "swc" / "example" / "ExampleSWC.yaml", memory={"CORE0": 2048})

        record = _record(demo_model, "hardware.memory-not-a-memory")

        assert record.message == "SWC 'ExampleSWC' budgets 'CORE0', which is a core, not a memory."
        assert record.subjects == (Subject("swc", "ExampleSWC", (field_step("memory"), key_step("CORE0"))),)
        assert record.target == "CORE0"

    def test_one_finding_per_swc_and_key(self, demo_model: Path) -> None:
        """The map is the unit of the budget; each entry is its own reference."""
        _edit(demo_model / "swc" / "example" / "ExampleSWC.yaml", memory={"RAM": 2048, "ROM": 512})

        assert _rules(demo_model).count("hardware.undeclared-memory") == 2


class TestTheAccessibleList:
    def test_accessible_naming_an_undeclared_core(self, demo_model: Path) -> None:
        _edit(demo_model / "hardware" / "RAM0.yaml", accessible=["CORE0", "CORE9"])

        record = _record(demo_model, "hardware.accessible-undeclared-core")

        assert record.message == (
            "Memory 'RAM0' is accessible from 'CORE9', which no hardware element declares (declared cores: CORE0)."
        )
        # `accessible` is a whole_value list, so the member *is* its identity.
        assert record.subjects == (Subject("hardware", "RAM0", (field_step("accessible"), member_step("CORE9"))),)
        assert record.target == "CORE9"

    def test_accessible_naming_a_memory(self, demo_model: Path) -> None:
        _write_hardware(demo_model, {"SHARED": _MEMORY_ACCESSIBLE_FROM_A_MEMORY})

        record = _record(demo_model, "hardware.accessible-not-a-core")

        assert record.message == "Memory 'SHARED' is accessible from 'RAM0', which is a memory, not a core."
        assert record.subjects == (Subject("hardware", "SHARED", (field_step("accessible"), member_step("RAM0"))),)
        assert record.target == "RAM0"

    def test_the_declared_core_list_names_every_core(self, demo_model: Path) -> None:
        """The message's help is the whole list, not the first name that fits."""
        _write_hardware(demo_model, {"CORE1": "name: CORE1\nkind: core\nthreshold: 0.5\n"})
        _edit(demo_model / "hardware" / "RAM0.yaml", accessible=["CORE9"])

        assert "(declared cores: CORE0, CORE1)" in _record(demo_model, "hardware.accessible-undeclared-core").message

    def test_the_declared_list_says_none_when_the_layer_declares_none(self, demo_model: Path) -> None:
        """An empty parenthesis reads as a truncated message rather than an answer."""
        (demo_model / "hardware" / "CORE0.yaml").unlink()

        assert "(declared cores: none)" in _record(demo_model, "hardware.partition-undeclared-core").message


# Reachability: the rule ``accessible`` exists to make checkable

#: A second core beside the demo model's ``CORE0``: with one core, every valid ``accessible`` list already names it.
_SECOND_CORE = "name: CORE1\nkind: core\nthreshold: 0.5\nenforcement: advisory\n"

#: The unrated partition on ``_SECOND_CORE``: a task moves to ``CORE1`` by naming it.
_SECOND_PARTITION = "name: PARTITION1\nkind: partition\ncore: CORE1\n"


class TestTheReachabilityRule:
    """Positive and negative, on the shipped model perturbed one field at a time.

    The rule: for every task ``T`` on core ``C``, every SWC ``S`` named by one of ``T``'s runnable refs, and every
    memory ``M`` in ``S.memory`` **that ``S`` charges a non-zero budget**, ``C`` must appear in ``M.accessible``. The
    zero half is ``TestAZeroByteBudgetIsNotAUse``'s. ``ExampleSWC`` budgets ``RAM0`` and no task names it, so the
    messages name ``ExampleConsumerSWC`` unless a test maps ``ExampleSWC`` onto the task.
    """

    def test_the_demo_model_passes(self, demo_model: Path) -> None:
        assert "hardware.unreachable-memory" not in _rules(demo_model)

    def test_a_two_core_model_whose_memory_lists_the_running_core_passes(self, demo_model: Path) -> None:
        """Two cores, one memory admitting both, the task on ``CORE1``: a vector the rule could fail."""
        _write_hardware(demo_model, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
        _edit(demo_model / "hardware" / "RAM0.yaml", accessible=["CORE0", "CORE1"])
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="PARTITION1")

        assert check_hardware_topology(str(demo_model)).consistent

    def test_the_surveys_tc397_layer_passes_with_its_task_on_the_core_that_reaches_ram0(
        self, tc397_model: Path
    ) -> None:
        """Six cores and a memory private to ``CORE0``, with the one task on ``CORE0``, are accepted."""
        assert check_hardware_topology(str(tc397_model)).consistent

    def test_the_surveys_layer_refuses_the_same_task_moved_to_another_of_its_six_cores(self, tc397_model: Path) -> None:
        """The same task moved to ``CORE3`` is refused: the reference resolves, ``RAM0`` does not admit it."""
        _edit(tc397_model / "tasks" / "Task_10ms.yaml", partition="PARTITION3")

        assert _rules(tc397_model) == ["hardware.unreachable-memory"]

    def test_a_task_on_a_core_the_budgeted_memory_does_not_admit(self, demo_model: Path) -> None:
        """The negative vector, on the rule id **and** the message a user reads."""
        _write_hardware(demo_model, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="PARTITION1")

        record = _record(demo_model, "hardware.unreachable-memory")

        assert record.message == (
            "Task 'Task_10ms' runs in partition 'PARTITION1' on core 'CORE1', but SWC 'ExampleConsumerSWC' "
            "budgets memory 'RAM0', which is not accessible from 'CORE1'."
        )
        # The three legal repairs: move the task, move its partition, or admit the core. The last is the *field*: the
        # member the repair adds does not exist yet, so there is no address for it.
        assert record.subjects == (
            Subject("tasks", "Task_10ms", (field_step("partition"),)),
            Subject("hardware", "PARTITION1", (field_step("core"),)),
            Subject("hardware", "RAM0", (field_step("accessible"),)),
        )
        # Not a reference rule: every identity it reads resolves, so there is no ``target``.
        assert record.target is None

    def test_it_is_a_design_rule_and_not_a_gap_in_the_accessible_list(self, demo_model: Path) -> None:
        """``hardware.accessible-undeclared-core`` does not fire beside it: the list itself is valid."""
        _write_hardware(demo_model, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="PARTITION1")

        assert _rules(demo_model) == ["hardware.unreachable-memory"]

    def test_two_runnables_of_one_swc_on_one_task_are_one_finding(self, demo_model: Path) -> None:
        """The emission is keyed on ``(task, SWC, memory)``, not on the runnable ref: the budget is the component's."""
        _write_hardware(demo_model, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="PARTITION1")
        consumer = demo_model / "swc" / "example" / "ExampleConsumerSWC.yaml"
        document = yaml.safe_load(consumer.read_text(encoding="utf-8"))
        document["runnables"].append(
            {"name": "ExampleConsumerRunB", "min_start_interval": 0.01, "events": [{"type": "timing", "period": 0.01}]}
        )
        consumer.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
        _edit(
            demo_model / "tasks" / "Task_10ms.yaml",
            runnables=[
                {"swc": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"},
                {"swc": "ExampleConsumerSWC", "runnable": "ExampleConsumerRunB"},
            ],
        )

        assert _rules(demo_model).count("hardware.unreachable-memory") == 1

    def test_two_swcs_on_one_task_budgeting_one_memory_are_two_findings(self, demo_model: Path) -> None:
        """The SWC is in the key because the message names it: each component's budget is its own repair.

        Keyed on ``(task, memory)`` alone, one of the two was reported and the other met only on the next run.
        """
        _write_hardware(demo_model, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
        _edit(
            demo_model / "tasks" / "Task_10ms.yaml",
            partition="PARTITION1",
            runnables=[
                {"swc": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"},
                {"swc": "ExampleSWC", "runnable": "ExampleRun"},
            ],
        )

        named = [
            record.message
            for record in check_hardware_topology(str(demo_model)).records
            if record.rule == "hardware.unreachable-memory"
        ]

        assert len(named) == 2
        assert [swc for swc in ("ExampleConsumerSWC", "ExampleSWC") if any(swc in message for message in named)] == [
            "ExampleConsumerSWC",
            "ExampleSWC",
        ]

    def test_two_memories_of_one_swc_are_two_findings(self, demo_model: Path) -> None:
        """The other half of the key: each unreachable memory is its own repair."""
        _write_hardware(demo_model, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
        _write_hardware(
            demo_model,
            {"RAM1": "name: RAM1\nkind: memory\ntype: RAM\nsize: 4096\naccessible: [CORE0]\nthreshold: 0.5\n"},
        )
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="PARTITION1")
        _edit(demo_model / "swc" / "example" / "ExampleConsumerSWC.yaml", memory={"RAM0": 1536, "RAM1": 512})

        assert _rules(demo_model).count("hardware.unreachable-memory") == 2

    def test_an_unmapped_component_is_not_reachable_from_anywhere_and_is_not_reported(self, demo_model: Path) -> None:
        """A component no task runs has no core to compare, so its budgeted memory is not reported."""
        _write_hardware(demo_model, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
        _edit(demo_model / "hardware" / "RAM0.yaml", accessible=["CORE1"])
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="PARTITION1")

        assert check_hardware_topology(str(demo_model)).consistent


class TestTheSiblingGuardsReportTheirOwnRuleAndNotThisOne:
    """Each guard of the pass skips a case another rule already reports.

    Without one, the reader is told twice about one repair, or the pass reaches ``.accessible`` on an element that has
    none (an ``AttributeError``). Each guard's removal is caught by a test here, not each by a second rule id; the last
    two assert silence, since those references belong to another layer's rules.
    """

    def test_a_task_on_an_undeclared_core(self, demo_model: Path) -> None:
        """A task in an undeclared partition: the reference rule alone reports it."""
        _edit(demo_model / "hardware" / "RAM0.yaml", accessible=["CORE0"])
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="PARTITION9")

        assert _rules(demo_model) == ["hardware.undeclared-partition"]

    def test_a_task_in_a_partition_on_an_undeclared_core(self, demo_model: Path) -> None:
        """The second hop: the partition resolves and its core does not."""
        _edit(demo_model / "hardware" / "PARTITION0.yaml", core="CORE9")

        assert _rules(demo_model) == ["hardware.partition-undeclared-core"]

    def test_a_task_on_a_hardware_element_that_is_not_a_core(self, demo_model: Path) -> None:
        """A partition on a ``Processor``: ``core_of`` answers no core, so ``.accessible`` is never reached."""
        _edit(demo_model / "hardware" / "PARTITION0.yaml", core="CPU")

        assert _rules(demo_model) == ["hardware.partition-core-not-a-core"]

    def test_a_task_in_an_element_that_is_not_a_partition(self, demo_model: Path) -> None:
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="CPU")

        assert _rules(demo_model) == ["hardware.partition-not-a-partition"]

    def test_an_swc_budgeting_an_undeclared_memory(self, demo_model: Path) -> None:
        _write_hardware(demo_model, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="PARTITION1")
        _edit(demo_model / "swc" / "example" / "ExampleConsumerSWC.yaml", memory={"RAM9": 1536})

        assert _rules(demo_model) == ["hardware.undeclared-memory"]

    def test_an_swc_budgeting_a_core(self, demo_model: Path) -> None:
        """The second kind guard: a `Core` has no `accessible` either."""
        _write_hardware(demo_model, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="PARTITION1")
        _edit(demo_model / "swc" / "example" / "ExampleConsumerSWC.yaml", memory={"CORE0": 1536})

        assert _rules(demo_model) == ["hardware.memory-not-a-memory"]

    def test_a_task_naming_an_swc_no_file_declares(self, demo_model: Path) -> None:
        """An unknown SWC is ``check_task_consistency``'s ``task.mapping-unknown-swc``: this pass stays silent."""
        _write_hardware(demo_model, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="PARTITION1")
        _edit(demo_model / "tasks" / "Task_10ms.yaml", runnables=[{"swc": "GhostSWC", "runnable": "GhostRun"}])

        assert check_hardware_topology(str(demo_model)).consistent

    def test_a_task_naming_a_runnable_the_component_does_not_declare(self, demo_model: Path) -> None:
        """An unknown runnable is ``task.mapping-unknown-runnable``'s: this pass stays silent.

        Without the guard it read the component's budgets off a broken mapping and cascaded a topology repair out of a
        mistyped runnable name; the core and memory both resolve here, so only the runnable stands between the fixture
        and a finding.
        """
        _write_hardware(demo_model, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="PARTITION1")
        _edit(
            demo_model / "tasks" / "Task_10ms.yaml",
            runnables=[{"swc": "ExampleConsumerSWC", "runnable": "NoSuchRun"}],
        )

        assert check_hardware_topology(str(demo_model)).consistent

    def test_the_same_fixture_with_the_declared_runnable_does_report_it(self, demo_model: Path) -> None:
        """The other half of the guard above: only the runnable name differs, so the fixture can produce the finding."""
        _write_hardware(demo_model, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="PARTITION1")
        _edit(
            demo_model / "tasks" / "Task_10ms.yaml",
            runnables=[{"swc": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"}],
        )

        assert _rules(demo_model) == ["hardware.unreachable-memory"]


# What the pass deliberately does not do


class TestWhatThePassLeavesToLaterPhases:
    def test_a_hardware_name_is_not_identifier_checked(self, demo_model: Path) -> None:
        """``layer:hardware`` is not ARXML-bound, so AUTOSAR's ``Identifier`` rule does not apply to ``shared-ram``."""
        _write_hardware(
            demo_model,
            {
                "shared-ram": (
                    "name: shared-ram\nkind: memory\ntype: RAM\nsize: 4096\naccessible: [CORE0]\nthreshold: 0.5\n"
                )
            },
        )

        assert check_hardware_topology(str(demo_model)).consistent
        assert validator.check_identifier_conformance(str(demo_model)).consistent


class TestAZeroByteBudgetIsNotAUse:
    """``0`` declares no consumption, so there is nothing for a core to reach.

    The other reading makes two shipped rules unsatisfiable together, which
    ``test_the_two_rules_are_satisfiable_together_for_a_private_memory`` demonstrates. ``SWComponent.memory`` admits
    ``0``, the writer writes it, and ``resource_analysis._analyze_memory`` treats it as a declared consumer of zero
    bytes, which silences ``hardware.missing-budget``: a zero says "this component uses none of that memory".

    What this does **not** buy: a component that will use the memory but has not measured it yet writes ``0`` and is not
    reachability-checked, the same trade ``hardware.missing-budget`` makes; the check fires on the first non-zero byte.
    """

    def test_a_zero_budget_for_an_unreachable_memory_is_not_reported(self, demo_model: Path) -> None:
        _write_hardware(demo_model, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="PARTITION1")
        _edit(demo_model / "swc" / "example" / "ExampleConsumerSWC.yaml", memory={"RAM0": 0})

        assert check_hardware_topology(str(demo_model)).consistent

    def test_the_same_fixture_with_one_byte_is_reported(self, demo_model: Path) -> None:
        """The other half: only the number differs from the case above."""
        _write_hardware(demo_model, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="PARTITION1")
        _edit(demo_model / "swc" / "example" / "ExampleConsumerSWC.yaml", memory={"RAM0": 1})

        assert _rules(demo_model) == ["hardware.unreachable-memory"]

    def test_a_zero_beside_a_non_zero_reports_only_the_non_zero_one(self, demo_model: Path) -> None:
        """One SWC, two unreachable memories, one of them zeroed."""
        _write_hardware(demo_model, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
        _write_hardware(
            demo_model,
            {"RAM1": "name: RAM1\nkind: memory\ntype: RAM\nsize: 4096\naccessible: [CORE0]\nthreshold: 0.5\n"},
        )
        _edit(demo_model / "tasks" / "Task_10ms.yaml", partition="PARTITION1")
        _edit(demo_model / "swc" / "example" / "ExampleConsumerSWC.yaml", memory={"RAM0": 0, "RAM1": 512})

        record = _record(demo_model, "hardware.unreachable-memory")
        assert "'RAM1'" in record.message
        assert "RAM0" not in record.message

    def test_the_two_rules_are_satisfiable_together_for_a_private_memory(self, demo_model: Path) -> None:
        """Both passes are silent on a model keeping a ``strict`` memory private to another core.

        Under ``strict`` every component without an entry is a ``hardware.missing-budget``, so the only repair that
        keeps ``PRIV1`` private is ``PRIV1: 0`` on the components that run elsewhere.
        """
        _write_hardware(demo_model, {"CORE1": _SECOND_CORE, "PARTITION1": _SECOND_PARTITION})
        _write_hardware(
            demo_model,
            {
                "PRIV1": (
                    "name: PRIV1\nkind: memory\ntype: RAM\nsize: 4096\n"
                    "accessible: [CORE1]\nthreshold: 0.8\nenforcement: strict\n"
                )
            },
        )
        for swc_file in sorted((demo_model / "swc").rglob("*.yaml"), key=lambda path: path.as_posix()):
            budgets = yaml.safe_load(swc_file.read_text(encoding="utf-8")).get("memory", {})
            _edit(swc_file, memory={**budgets, "PRIV1": 0})

        assert check_hardware_topology(str(demo_model)).consistent
        assert validator.check_resource_budgets(str(demo_model)).consistent


# The pass is wired in, and its rules are the contract's


class TestThePassIsWiredIn:
    def test_it_runs_before_the_budget_pass(self, demo_model: Path) -> None:
        """A topology finding is listed before a budget finding in the aggregate's answer.

        The budget pass assumes every reference resolves. What this does not reach: the order on a model where only one
        of the two passes reports.
        """
        _edit(demo_model / "hardware" / "RAM0.yaml", size=16, accessible=["CORE0", "CORE9"])

        rules = [record.rule for record in validator.check_cross_layer_consistency(str(demo_model)).records]

        topology_rules = _topology_rules()
        budget_rules = set(_RESOURCE_FINDING_RULES.values())
        topology = [index for index, rule in enumerate(rules) if rule in topology_rules]
        budgets = [index for index, rule in enumerate(rules) if rule in budget_rules]
        assert topology and budgets, rules
        assert max(topology) < min(budgets), rules

    def test_the_pass_emits_no_rule_the_inventory_does_not_declare(self, demo_model: Path, tmp_path: Path) -> None:
        """Every rule the pass reports over one perturbation per rule is a declared topology rule, and all are.

        What this does not reach: a rule the pass mints on a path none of these perturbations takes.
        """
        reported: set[str] = set()
        for index, (rule, perturb) in enumerate(_ONE_BREAK_PER_RULE):
            model_root = tmp_path / f"break{index}"
            shutil.copytree(demo_model, model_root)
            perturb(model_root)
            rules = _rules(model_root)
            assert rule in rules, (rule, rules)
            reported.update(rules)

        assert reported <= set(CONSISTENCY_RULES), sorted(reported - set(CONSISTENCY_RULES))
        assert reported == _topology_rules()
