"""Tests for the ``hardware/`` schema classes.

``Hardware`` is a discriminated union on ``kind``, like ``DataType`` / ``Interface`` in ``schema.py``; a retired
``resources/``-shaped file does not parse as hardware. The positive vectors are a TC397 example, transcribed rather
than invented.

What this file does **not** reach: every rule here is element-local. Whether ``accessible`` names a core that exists,
whether exactly one processor is declared, and whether a task's placement resolves are ``check-model``'s
(``packages/tools/tests/test_hardware_topology.py``).
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError
from swcraft_tools.safety import ASIL_VALUES
from swcraft_tools.schema import (
    Core,
    Hardware,
    Memory,
    Partition,
    Processor,
    Task,
    core_of,
    hardware_by_name,
    partition_of,
)

# The TC397 vectors, transcribed

TC397_PROCESSOR = """
name: CPU
kind: processor
description: Infineon AURIX TC397, 6 TriCore 1.6.2P at 300 MHz
"""

TC397_CORE = """
name: CORE0
kind: core
threshold: 0.7
enforcement: strict
description: CPU0, lockstep-checked; runs the safety partition
"""

TC397_MEMORIES = {
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


def _parse(source: str) -> Processor | Core | Memory:
    return TypeAdapter(Hardware).validate_python(yaml.safe_load(source))


class TestTheSurveysExampleParses:
    """The whole TC397 file set, through the union, as YAML."""

    def test_processor(self):
        element = _parse(TC397_PROCESSOR)
        assert isinstance(element, Processor)
        assert element.name == "CPU"
        assert element.description.startswith("Infineon AURIX TC397")

    def test_core(self):
        element = _parse(TC397_CORE)
        assert isinstance(element, Core)
        assert element.name == "CORE0"
        assert element.threshold == pytest.approx(0.7)
        assert element.enforcement == "strict"
        assert element.hw_id is None

    @pytest.mark.parametrize("name", sorted(TC397_MEMORIES))
    def test_memory(self, name):
        element = _parse(TC397_MEMORIES[name])
        assert isinstance(element, Memory)
        assert element.name == name
        assert element.accessible[0] == "CORE0"

    def test_the_five_memories_carry_three_of_the_four_declared_types(self):
        """The vectors exercise three ``type`` literals, so they are not vacuous; ``TestMemoryType`` covers all four."""
        observed = {_parse(source).type for source in TC397_MEMORIES.values()}
        assert observed == {"RAM", "Flash", "EEPROM"}


# Processor


class TestProcessor:
    def test_minimal_construction(self):
        cpu = Processor(kind="processor", name="CPU")
        assert cpu.kind == "processor"
        assert cpu.name == "CPU"
        assert cpu.description == ""

    def test_wrong_kind_raises(self):
        with pytest.raises(ValidationError):
            Processor(kind="core", name="CPU")

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_blank_name_raises(self, blank):
        with pytest.raises(ValidationError):
            Processor(kind="processor", name=blank)

    @pytest.mark.parametrize("bad_name", ["../../etc/passwd", "..\\evil", "..", "a/b"])
    def test_path_traversal_name_raises(self, bad_name):
        with pytest.raises(ValidationError):
            Processor(kind="processor", name=bad_name)

    def test_declares_no_budget_fields(self):
        """Three fields and the trace link, nothing else: no ``vendor``, ``arch`` or ``part``.

        Nothing reads a further fact off a processor, and a field nothing reads is a second truth waiting to disagree
        with the part number in the description.
        """
        assert set(Processor.model_fields) == {"name", "kind", "description", "satisfies"}


# Core


class TestCore:
    def test_minimal_construction(self):
        core = Core(kind="core", name="CORE1", threshold=0.7)
        assert core.kind == "core"
        assert core.name == "CORE1"
        assert core.threshold == pytest.approx(0.7)
        assert core.enforcement == "strict"
        assert core.hw_id is None
        assert core.description == ""

    def test_full_construction(self):
        core = Core(
            kind="core",
            name="CORE0",
            threshold=1.0,
            enforcement="advisory",
            hw_id=0,
            description="CPU0",
        )
        assert core.enforcement == "advisory"
        assert core.hw_id == 0

    def test_wrong_kind_raises(self):
        with pytest.raises(ValidationError):
            Core(kind="memory", name="CORE0", threshold=0.7)

    def test_missing_threshold_raises(self):
        with pytest.raises(ValidationError):
            Core(kind="core", name="CORE0")

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_blank_name_raises(self, blank):
        with pytest.raises(ValidationError):
            Core(kind="core", name=blank, threshold=0.7)

    @pytest.mark.parametrize("bad_name", ["../../etc/passwd", "..\\evil", "..", "a/b"])
    def test_path_traversal_name_raises(self, bad_name):
        with pytest.raises(ValidationError):
            Core(kind="core", name=bad_name, threshold=0.7)

    @pytest.mark.parametrize("hw_id", [0, 5, 255, -1, 1_000_000])
    def test_hw_id_accepts_any_int(self, hw_id):
        """The vendor's physical id: informational and unvalidated, so any int, duplicates included, is accepted."""
        assert Core(kind="core", name="CORE0", threshold=0.7, hw_id=hw_id).hw_id == hw_id

    def test_hw_id_is_absent_by_default(self):
        assert "hw_id" not in Core(kind="core", name="CORE0", threshold=0.7).model_dump(exclude_none=True)


# Memory


def _memory(**overrides):
    fields = {
        "kind": "memory",
        "name": "RAM0",
        "type": "RAM",
        "size": 262144,
        "accessible": ["CORE0"],
        "threshold": 0.8,
    }
    fields.update(overrides)
    return Memory(**fields)


class TestMemory:
    def test_minimal_construction(self):
        mem = _memory()
        assert mem.kind == "memory"
        assert mem.type == "RAM"
        assert mem.size == 262144
        assert mem.accessible == ["CORE0"]
        assert mem.enforcement == "strict"
        assert mem.description == ""

    def test_wrong_kind_raises(self):
        with pytest.raises(ValidationError):
            _memory(kind="core")

    @pytest.mark.parametrize("missing", ["type", "size", "accessible", "threshold"])
    def test_every_required_field_is_required(self, missing):
        fields = {
            "kind": "memory",
            "name": "RAM0",
            "type": "RAM",
            "size": 262144,
            "accessible": ["CORE0"],
            "threshold": 0.8,
        }
        del fields[missing]
        with pytest.raises(ValidationError):
            Memory(**fields)

    @pytest.mark.parametrize("size", [0, -1, -262144])
    def test_non_positive_size_raises(self, size):
        with pytest.raises(ValidationError):
            _memory(size=size)

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_blank_name_raises(self, blank):
        with pytest.raises(ValidationError):
            _memory(name=blank)

    @pytest.mark.parametrize("bad_name", ["../../etc/passwd", "..\\evil", "..", "a/b"])
    def test_path_traversal_name_raises(self, bad_name):
        with pytest.raises(ValidationError):
            _memory(name=bad_name)


class TestMemoryType:
    """The four AUTOSAR `memoryType` literals, in their standard spelling."""

    @pytest.mark.parametrize("value", ["RAM", "ROM", "EEPROM", "Flash"])
    def test_accepted_values(self, value):
        assert _memory(type=value).type == value

    @pytest.mark.parametrize("value", ["ram", "flash", "SRAM", "DRAM", "", "NVM"])
    def test_rejected_values(self, value):
        """Lower-case spellings are refused: the literals quote the ECU Resource Template.

        Accepting ``ram`` beside ``RAM`` would make a lossless export ambiguous about which spelling it emitted.
        """
        with pytest.raises(ValidationError):
            _memory(type=value)


class TestMemoryAccessible:
    """At least one core, no repeats, no blank entry: the element-local half."""

    def test_single_core_is_the_private_case(self):
        assert _memory(accessible=["CORE0"]).accessible == ["CORE0"]

    def test_order_is_preserved(self):
        assert _memory(accessible=["CORE1", "CORE0"]).accessible == ["CORE1", "CORE0"]

    def test_empty_list_raises(self):
        with pytest.raises(ValidationError, match="at least one core"):
            _memory(accessible=[])

    def test_duplicate_core_raises(self):
        with pytest.raises(ValidationError, match="duplicate core in accessible: CORE0"):
            _memory(accessible=["CORE0", "CORE1", "CORE0"])

    @pytest.mark.parametrize("blank", ["", " ", "\t"])
    def test_a_blank_entry_raises(self, blank):
        """A blank entry names no core and never can, so the element refuses it.

        Left to parse, it would be reported by the topology check quoting an empty name, which reads as a checker
        defect.
        """
        with pytest.raises(ValidationError, match="must not be blank"):
            _memory(accessible=["CORE0", blank])

    def test_an_undeclared_core_is_accepted_here(self):
        """Existence is a cross-element question: ``hardware.accessible-undeclared-core`` refuses it."""
        assert _memory(accessible=["CORE9"]).accessible == ["CORE9"]


# Shared field validators


class TestThreshold:
    @pytest.mark.parametrize("value", [0.01, 0.5, 0.7, 1.0])
    def test_valid_thresholds(self, value):
        assert Core(kind="core", name="CORE0", threshold=value).threshold == value
        assert _memory(threshold=value).threshold == value

    @pytest.mark.parametrize("value", [0.0, -0.1, 1.0001, 2.0])
    def test_out_of_range_threshold_raises(self, value):
        with pytest.raises(ValidationError):
            Core(kind="core", name="CORE0", threshold=value)
        with pytest.raises(ValidationError):
            _memory(threshold=value)

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_threshold_raises(self, value):
        """``allow_inf_nan=False`` on ``ModelElement`` plus ``_validate_threshold``.

        YAML spells these ``.nan`` / ``.inf``; the base-class setting makes the guard cover every numeric field.
        """
        with pytest.raises(ValidationError):
            Core(kind="core", name="CORE0", threshold=value)
        with pytest.raises(ValidationError):
            _memory(threshold=value)

    @pytest.mark.parametrize("source", [".nan", ".inf", "-.inf"])
    def test_non_finite_threshold_from_yaml_raises(self, source):
        raw = yaml.safe_load(f"kind: core\nname: CORE0\nthreshold: {source}\n")
        with pytest.raises(ValidationError):
            TypeAdapter(Hardware).validate_python(raw)


class TestEnforcement:
    def test_defaults_to_strict(self):
        assert Core(kind="core", name="CORE0", threshold=0.7).enforcement == "strict"
        assert _memory().enforcement == "strict"

    @pytest.mark.parametrize("value", ["strict", "advisory"])
    def test_accepted_values(self, value):
        assert Core(kind="core", name="CORE0", threshold=0.7, enforcement=value).enforcement == value
        assert _memory(enforcement=value).enforcement == value

    @pytest.mark.parametrize("value", ["STRICT", "warn", "", "error"])
    def test_rejected_values(self, value):
        with pytest.raises(ValidationError):
            Core(kind="core", name="CORE0", threshold=0.7, enforcement=value)
        with pytest.raises(ValidationError):
            _memory(enforcement=value)


# Partition


class TestPartition:
    """The layer's fourth kind: the core a task runs on and the level it runs at.

    Two element-local refusals: ``rule:hardware.partition.name.not-blank`` (blank, a path separator or a traversal
    segment) and ``rule:hardware.partition.core.not-blank``.
    """

    def test_minimal_construction_is_unrated_and_unargued(self):
        partition = Partition(kind="partition", name="PARTITION0", core="CORE0")
        assert (partition.core, partition.asil, partition.coexistence, partition.description) == (
            "CORE0",
            None,
            "",
            "",
        )

    def test_full_construction(self):
        partition = Partition.model_validate(
            yaml.safe_load(
                "name: P_Body\nkind: partition\ncore: CORE0\nasil: A\n"
                "coexistence: The QM components are separated by the MPU\ndescription: Body functions\n"
            )
        )
        assert partition.asil == "A"
        assert partition.coexistence == "The QM components are separated by the MPU"

    def test_the_field_order_is_the_contracts(self):
        assert list(Partition.model_fields) == [
            "name",
            "kind",
            "core",
            "asil",
            "trust",
            "coexistence",
            "description",
            "satisfies",
        ]

    def test_wrong_kind_raises(self):
        with pytest.raises(ValidationError):
            Partition(kind="core", name="PARTITION0", core="CORE0")

    def test_missing_core_raises_naming_the_field(self):
        with pytest.raises(ValidationError) as excinfo:
            Partition(kind="partition", name="PARTITION0")
        assert [error["loc"] for error in excinfo.value.errors()] == [("core",)]

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_blank_core_raises(self, blank):
        with pytest.raises(ValidationError) as excinfo:
            Partition(kind="partition", name="PARTITION0", core=blank)
        assert [error["loc"] for error in excinfo.value.errors()] == [("core",)]

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_blank_name_raises(self, blank):
        with pytest.raises(ValidationError):
            Partition(kind="partition", name=blank, core="CORE0")

    @pytest.mark.parametrize("bad_name", ["../../etc/passwd", "..\\evil", "..", "a/b"])
    def test_path_traversal_name_raises(self, bad_name):
        with pytest.raises(ValidationError):
            Partition(kind="partition", name=bad_name, core="CORE0")

    @pytest.mark.parametrize("level", ASIL_VALUES)
    def test_every_declared_level_is_accepted(self, level):
        """The eighteen levels, ``B(D)`` and ``QM(A)`` among them."""
        assert Partition(kind="partition", name="P", core="CORE0", asil=level).asil == level

    @pytest.mark.parametrize("level", ["E", "qm", "ASIL-D", "", 4, "A(A)", "B (D)", "b(d)"])
    def test_an_illegal_level_is_refused(self, level):
        with pytest.raises(ValidationError):
            Partition(kind="partition", name="P", core="CORE0", asil=level)

    def test_an_undeclared_core_is_accepted_here(self):
        """Existence is a cross-element question (`hardware.partition-undeclared-core`)."""
        assert Partition(kind="partition", name="P", core="NoSuchCore").core == "NoSuchCore"


class TestTheCapabilityIsNotDecomposed:
    """A core's and a memory's ``asil_capability`` keep the five plain levels: a capability is not decomposed."""

    @pytest.mark.parametrize("level", ["QM", "A", "B", "C", "D"])
    def test_the_five_plain_levels_load(self, level):
        assert Core(kind="core", name="CORE0", threshold=0.7, asil_capability=level).asil_capability == level
        assert _memory(asil_capability=level).asil_capability == level

    @pytest.mark.parametrize("level", ["B(D)", "QM(A)", "D(D)"])
    def test_a_decomposed_level_is_refused(self, level):
        with pytest.raises(ValidationError):
            Core(kind="core", name="CORE0", threshold=0.7, asil_capability=level)
        with pytest.raises(ValidationError):
            _memory(asil_capability=level)


class TestPartitionTrust:
    """The OS-Application's class: three values, default ``non_trusted``.

    Any other value is the ``Literal``'s refusal at load, with no rule id.
    """

    def test_an_undeclared_class_is_non_trusted(self):
        assert Partition(kind="partition", name="P", core="CORE0").trust == "non_trusted"

    @pytest.mark.parametrize("trust", ["non_trusted", "trusted_with_protection", "trusted"])
    def test_every_declared_class_loads(self, trust):
        partition = Partition.model_validate(yaml.safe_load(f"name: P\nkind: partition\ncore: CORE0\ntrust: {trust}\n"))
        assert partition.trust == trust

    @pytest.mark.parametrize("trust", ["untrusted", "Trusted", "TRUSTED", "", True, None])
    def test_a_fourth_value_is_refused_by_the_literal(self, trust):
        with pytest.raises(ValidationError) as excinfo:
            Partition(kind="partition", name="P", core="CORE0", trust=trust)
        assert [error["loc"] for error in excinfo.value.errors()] == [("trust",)]
        assert "rule:" not in str(excinfo.value)


# The Hardware discriminated union


class TestHardwareDiscriminatedUnion:
    """The union dispatches on `kind`, like `DataType` / `Interface`."""

    def test_processor_dispatch(self):
        assert isinstance(TypeAdapter(Hardware).validate_python({"kind": "processor", "name": "CPU"}), Processor)

    def test_core_dispatch(self):
        element = TypeAdapter(Hardware).validate_python({"kind": "core", "name": "CORE0", "threshold": 0.7})
        assert isinstance(element, Core)

    def test_memory_dispatch(self):
        element = TypeAdapter(Hardware).validate_python(
            {
                "kind": "memory",
                "name": "RAM0",
                "type": "RAM",
                "size": 1024,
                "accessible": ["CORE0"],
                "threshold": 0.8,
            }
        )
        assert isinstance(element, Memory)

    def test_partition_dispatch(self):
        element = TypeAdapter(Hardware).validate_python({"kind": "partition", "name": "P0", "core": "CORE0"})
        assert isinstance(element, Partition)

    @pytest.mark.parametrize("kind", ["flash_cycles", "cluster", "processor_core", "", "Core", "Partition"])
    def test_a_kind_none_of_the_four_declares_is_refused(self, kind):
        with pytest.raises(ValidationError):
            TypeAdapter(Hardware).validate_python({"kind": kind, "name": "Foo", "threshold": 0.5})

    def test_missing_kind_raises(self):
        with pytest.raises(ValidationError):
            TypeAdapter(Hardware).validate_python({"name": "Foo", "threshold": 0.5})


class TestTheRetiredResourcesShapeNoLongerParses:
    """The retired ``resources/`` shape fails on read, so the break shows at ``check-model``, not as an empty layer."""

    def test_a_cpu_core_file_is_not_hardware(self):
        raw = yaml.safe_load("kind: cpu_core\nname: Core0\ncore: 0\nthreshold: 0.7\n")
        with pytest.raises(ValidationError):
            TypeAdapter(Hardware).validate_python(raw)

    def test_a_retired_resources_memory_file_is_not_a_hardware_memory(self):
        """``kind: memory`` survives the discriminator and then fails on the fields.

        It is the one retired file whose ``kind`` still dispatches, so it would half-work if ``type`` and ``accessible``
        were optional.
        """
        raw = yaml.safe_load("kind: memory\nname: RAM\nsize: 262144\nthreshold: 0.8\n")
        with pytest.raises(ValidationError) as excinfo:
            TypeAdapter(Hardware).validate_python(raw)
        missing = {error["loc"][-1] for error in excinfo.value.errors()}
        assert {"type", "accessible"} <= missing

    def test_the_retired_classes_are_named_by_no_hardware_kind(self):
        """`kind` is the only discriminator; `cpu_core` is not one of its values."""
        assert {Processor.model_fields["kind"].annotation.__args__[0]} == {"processor"}
        assert {Core.model_fields["kind"].annotation.__args__[0]} == {"core"}
        assert {Memory.model_fields["kind"].annotation.__args__[0]} == {"memory"}


class TestTheTaskPlacementHelpers:
    """``hardware_by_name``, ``partition_of`` and ``core_of``: the one derivation of a task's core.

    The validator, the resource analysis and the deployment view read placement through these, so it is pinned here:
    first file wins over every hardware element whatever its kind, and a name resolving to another kind answers
    ``None``.
    """

    @staticmethod
    def _task(partition: str) -> Task:
        return Task(name="T", period=0.01, priority=1, partition=partition)

    def test_the_happy_path_answers_the_partition_and_its_core(self):
        core = Core(kind="core", name="CORE0", threshold=0.7)
        partition = Partition(kind="partition", name="P0", core="CORE0")
        by_name = hardware_by_name([core, partition])

        assert partition_of(self._task("P0"), by_name) is partition
        assert core_of(self._task("P0"), by_name) is core

    def test_an_undeclared_partition_answers_none(self):
        by_name = hardware_by_name([Core(kind="core", name="CORE0", threshold=0.7)])

        assert partition_of(self._task("P_Ghost"), by_name) is None
        assert core_of(self._task("P_Ghost"), by_name) is None

    def test_a_name_of_another_kind_answers_none(self):
        by_name = hardware_by_name([Core(kind="core", name="CORE0", threshold=0.7)])

        assert partition_of(self._task("CORE0"), by_name) is None
        assert core_of(self._task("CORE0"), by_name) is None

    def test_a_duplicate_with_the_other_kind_first_answers_none(self):
        """A core and a partition of one name, the core first: the name is the core's.

        A kind-filtered pick would answer a core the topology pass never resolved the name to.
        """
        core = Core(kind="core", name="X", threshold=0.7)
        partition = Partition(kind="partition", name="X", core="X")
        by_name = hardware_by_name([core, partition])

        assert by_name["X"] is core
        assert partition_of(self._task("X"), by_name) is None
        assert core_of(self._task("X"), by_name) is None

    def test_a_duplicate_partition_resolves_to_the_first_file(self):
        first = Partition(kind="partition", name="P0", core="CORE0")
        second = Partition(kind="partition", name="P0", core="CORE1")
        cores = [Core(kind="core", name=name, threshold=0.7) for name in ("CORE0", "CORE1")]
        by_name = hardware_by_name([*cores, first, second])

        assert partition_of(self._task("P0"), by_name) is first
        assert core_of(self._task("P0"), by_name) is cores[0]

    def test_a_partition_on_a_memory_has_no_core(self):
        memory = Memory(kind="memory", name="RAM0", type="RAM", size=1024, accessible=["CORE0"], threshold=0.8)
        partition = Partition(kind="partition", name="P0", core="RAM0")
        by_name = hardware_by_name([memory, partition])

        assert partition_of(self._task("P0"), by_name) is partition
        assert core_of(self._task("P0"), by_name) is None
