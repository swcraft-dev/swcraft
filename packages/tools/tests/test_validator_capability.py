"""`check_hardware_capability` — the placement rules.

One check, one read of four layers. Each rule compares a level with the level the hardware it is placed on is
declared able to host:

* `safety.partition-on-unqualified-core` — a rated partition on a core whose `asil_capability` is below the
  partition's own level, one finding per partition (a partition no task names fires too); it reads no component;
* `safety.memory-below-component` — a component charging a non-zero budget to a memory whose `asil_capability`
  is below the component's effective level, one finding per `(component, memory)`; a budget of `0` declares no use;
* the per-type rules, over the component types a partition's tasks run: `safety.component-above-partition`,
  `safety.component-below-partition` (`QM` included, closed by the partition's `coexistence`) and
  `safety.partition-memory-below-level`, one finding per `(partition, memory)`;
* the trust rules, against the highest level any partition of the processor declares:
  `safety.trusted-partition-below-processor-level` and `safety.trusted-partition-unrated` (`TestTheTrustRules`).

All are declaration checks: nothing in the model expresses the MPU configuration, the linker script or the
safety manual's assumptions of use, so no rule says the hardware enforces anything. Unrated is not `QM`: a
partition, core or memory declaring no level is compared against nothing, and one declaring `QM` hosts no ASIL.

What this module does **not** reach: placement (no linker script, MPU region or OS-Application configuration is
read), the processor (it declares no capability), and the guard on raising or clearing a capability
(`test_authorization_safety.py`, `test_editing_authorization.py`).
"""

from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import validator_package
import yaml
from conftest import REPO_ROOT, SAFETY_MODEL_ROOT, _safety_reading
from model_contract_fixtures import load_binding
from pydantic import ValidationError
from swcraft_tools.diagnostics import Subject, field_step, key_step
from swcraft_tools.editing.diagnosis import CONSISTENCY_VIOLATION, RULE_CODES
from swcraft_tools.reader import read_compositions, read_hardware, read_requirements, read_swc_model, read_tasks
from swcraft_tools.safety import ASIL_LEVELS
from swcraft_tools.scheduling_identity import identities_from
from swcraft_tools.schema import Core, Memory, Partition, Processor
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    check_cross_layer_consistency,
    check_hardware_capability,
    check_hardware_topology,
    hardware_capability_records,
)
from swcraft_tools.validator import hardware as validator_hardware
from swcraft_tools.writer.elements import write_element
from test_model_contract_conformance import (
    cross_element_layer_failures,
    emission_layers,
    load_contract,
)

PARTITION = "safety.partition-on-unqualified-core"
MEMORY = "safety.memory-below-component"
ABOVE = "safety.component-above-partition"
BELOW = "safety.component-below-partition"
PARTITION_MEMORY = "safety.partition-memory-below-level"
TRUSTED_BELOW = "safety.trusted-partition-below-processor-level"
TRUSTED_UNRATED = "safety.trusted-partition-unrated"
#: The seven rules of the check, in the order it mints them.
RULES = (PARTITION, MEMORY, ABOVE, BELOW, PARTITION_MEMORY, TRUSTED_BELOW, TRUSTED_UNRATED)


# ---------------------------------------------------------------------------
# Model fixtures — the four layers the check reads, and nothing else
# ---------------------------------------------------------------------------


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _core(model_root: Path, name: str, capability: str | None = None, *, filename: str | None = None) -> None:
    _write(
        model_root / "hardware" / f"{filename or name}.yaml",
        {"name": name, "kind": "core", "threshold": 1.0, **({"asil_capability": capability} if capability else {})},
    )


def _partition(
    model_root: Path,
    name: str,
    level: str | None = None,
    *,
    core: str = "CORE0",
    filename: str | None = None,
    **extra: Any,
) -> None:
    _write(
        model_root / "hardware" / f"{filename or name}.yaml",
        {"name": name, "kind": "partition", "core": core, **({"asil": level} if level else {}), **extra},
    )


def _memory(
    model_root: Path,
    name: str,
    capability: str | None = None,
    *,
    filename: str | None = None,
    accessible: tuple[str, ...] = ("CORE0",),
) -> None:
    _write(
        model_root / "hardware" / f"{filename or name}.yaml",
        {
            "name": name,
            "kind": "memory",
            "type": "RAM",
            "size": 4096,
            "accessible": list(accessible),
            "threshold": 1.0,
            **({"asil_capability": capability} if capability else {}),
        },
    )


def _swc(model_root: Path, name: str, *, filename: str | None = None, **extra: Any) -> None:
    linked = _PENDING_LINKS.get((model_root, name), [])
    _write(
        model_root / "swc" / "group" / f"{filename or name}.yaml",
        {"name": name, "type": "application", **({"satisfies": linked} if linked else {}), **extra},
    )


def _runnable(name: str) -> dict[str, Any]:
    return {"name": name, "events": [{"type": "timing", "period": 0.01}]}


def _requirement(model_root: Path, req_id: str, links: list[str], **extra: Any) -> None:
    """Write a requirement, and its link into each component *links* names.

    The link is the component's own ``satisfies`` member ``<req_id>@1``, appended
    to every component file declaring the name, written before or after (``_swc``
    keeps it). A name no file declares allocates to nothing.
    """
    _write(
        model_root / "requirements" / f"{req_id}.yaml",
        {
            "id": req_id,
            "title": f"{req_id} title",
            "description": f"The system shall satisfy {req_id}.",
            "version": 1,
            "source": "architecture",
            **extra,
        },
    )
    for name in links:
        _PENDING_LINKS.setdefault((model_root, name), []).append(f"{req_id}@1")
        for path in sorted((model_root / "swc").rglob("*.yaml"), key=lambda candidate: candidate.as_posix()):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            if document.get("name") == name:
                document["satisfies"] = [*document.get("satisfies", []), f"{req_id}@1"]
                _write(path, document)


#: ``(model root, component name) -> links`` written by :func:`_requirement`, so a
#: component file written after its requirement carries them too.
_PENDING_LINKS: dict[tuple[Path, str], list[str]] = {}


def _task(
    model_root: Path, name: str, runnables: list[dict[str, str]], partition: str = "P0", *, filename: str | None = None
) -> None:
    _write(
        model_root / "tasks" / f"{filename or name}.yaml",
        {"name": name, "period": 0.01, "priority": 1, "partition": partition, "runnables": runnables},
    )


def _ref(swc: str, runnable: str = "Run") -> dict[str, str]:
    return {"swc": swc, "runnable": runnable}


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    """The processor every project declares, and nothing else yet."""
    root = tmp_path / "model"
    _write(root / "hardware" / "CPU.yaml", {"name": "CPU", "kind": "processor"})
    return root


def _records(model_root: Path, rule: str | None = None) -> tuple[Any, ...]:
    records = check_hardware_capability(str(model_root)).records
    return tuple(record for record in records if rule is None or record.rule == rule)


def _one(model_root: Path, rule: str) -> Any:
    records = _records(model_root, rule)
    assert len(records) == 1, f"expected one {rule!r}, got {[r.message for r in records]}"
    return records[0]


# ---------------------------------------------------------------------------
# 1. rule:safety.partition-on-unqualified-core
# ---------------------------------------------------------------------------


class TestThePartitionRule:
    def test_a_partition_above_its_core_is_reported_naming_every_party(self, model_root: Path) -> None:
        _core(model_root, "CORE0", "B")
        _partition(model_root, "P0", "D")
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi")])

        record = _one(model_root, PARTITION)

        assert record.message == (
            "Partition 'P0' (ASIL D) is on core 'CORE0' (ASIL capability B): a partition placed on a core not "
            "declared able to host its level."
        )
        # The legal repairs, as subjects: lower the partition, raise the core,
        # move the partition to another core.
        assert record.subjects == (
            Subject("hardware", "P0", (field_step("asil"),)),
            Subject("hardware", "CORE0", (field_step("asil_capability"),)),
            Subject("hardware", "P0", (field_step("core"),)),
        )

    def test_an_empty_partition_is_reported_too(self, model_root: Path) -> None:
        """The level is the partition's own declaration, not a reading of what runs in it."""
        _core(model_root, "CORE0", "A")
        _partition(model_root, "P_Empty", "C")

        assert _one(model_root, PARTITION).subjects[0] == Subject("hardware", "P_Empty", (field_step("asil"),))
        assert not list(Path(model_root).glob("tasks/*.yaml"))

    def test_a_partition_equal_to_its_core_is_silent(self, model_root: Path) -> None:
        _core(model_root, "CORE0", "B")
        _partition(model_root, "P0", "B")

        assert _records(model_root, PARTITION) == ()

    def test_a_partition_below_its_core_is_silent(self, model_root: Path) -> None:
        _core(model_root, "CORE0", "D")
        _partition(model_root, "P0", "A")

        assert _records(model_root, PARTITION) == ()

    def test_an_unrated_partition_is_compared_against_nothing(self, model_root: Path) -> None:
        _core(model_root, "CORE0", "QM")
        _partition(model_root, "P0")
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi")])

        assert _records(model_root, PARTITION) == ()

    def test_a_core_declaring_no_capability_is_unrated_and_compared_against_nothing(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "D")

        assert _records(model_root, PARTITION) == ()

    def test_a_declared_qm_capability_hosts_no_asil_so_a_rated_partition_is_reported(self, model_root: Path) -> None:
        _core(model_root, "CORE0", "QM")
        _partition(model_root, "P0", "A")

        assert "ASIL capability QM" in _one(model_root, PARTITION).message

    def test_a_qm_partition_on_a_qm_core_is_silent(self, model_root: Path) -> None:
        """`QM` is never above anything, whatever the core declares."""
        _core(model_root, "CORE0", "QM")
        _partition(model_root, "P0", "QM")

        assert _records(model_root, PARTITION) == ()

    def test_the_rule_reads_no_component(self, model_root: Path) -> None:
        """A `D` component in an `A` partition on an `A` core is not this rule's finding.

        The rule compares the partition's DECLARED level with the core; a component
        above its partition is `safety.component-above-partition`'s.
        """
        _core(model_root, "CORE0", "A")
        _partition(model_root, "P0", "A")
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi")])

        assert _records(model_root, PARTITION) == ()

    def test_two_partitions_on_one_core_both_above_are_two_findings_in_reader_order(self, model_root: Path) -> None:
        _core(model_root, "CORE0", "A")
        _partition(model_root, "P_b", "C")
        _partition(model_root, "P_a", "D")

        assert [record.subjects[0].element for record in _records(model_root, PARTITION)] == ["P_a", "P_b"]

    def test_a_partition_on_an_undeclared_core_is_the_topology_passs_and_not_this_rules(self, model_root: Path) -> None:
        _core(model_root, "CORE0", "QM")
        _partition(model_root, "P0", "D", core="CORE9")

        assert _records(model_root, PARTITION) == ()
        assert "hardware.partition-undeclared-core" in {
            r.rule for r in check_hardware_topology(str(model_root)).records
        }

    def test_a_partition_on_a_memory_is_the_topology_passs_and_not_this_rules(self, model_root: Path) -> None:
        """A rated memory is still not a core, whatever capability it declares."""
        _core(model_root, "CORE0")
        _memory(model_root, "RAM0", "QM")
        _partition(model_root, "P0", "D", core="RAM0")

        assert _records(model_root, PARTITION) == ()
        assert "hardware.partition-core-not-a-core" in {
            r.rule for r in check_hardware_topology(str(model_root)).records
        }

    def test_a_task_in_an_undeclared_partition_changes_nothing_here(self, model_root: Path) -> None:
        _core(model_root, "CORE0", "QM")
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi")], partition="P9")

        assert _records(model_root, PARTITION) == ()
        assert "hardware.undeclared-partition" in {r.rule for r in check_hardware_topology(str(model_root)).records}


class TestTheHardwareResolvesFirstWinsOverEveryElement:
    """`check_hardware_topology`'s own resolution, kind-agnostic.

    One identity namespace holds all four kinds, and the topology pass resolves a
    reference against the FIRST file of a name, whatever its kind. A
    capability-filtered comprehension would be last-wins and would let a later
    rated duplicate displace an earlier unrated claimant — or resolve a partition
    to a different core than the topology pass resolved it to. The reader yields
    `hardware/*.yaml` in filename order, so `A_CORE0.yaml` is read before
    `B_CORE0.yaml`.
    """

    def test_two_cores_of_one_name_the_first_decides(self, model_root: Path) -> None:
        _core(model_root, "CORE0", "B", filename="A_CORE0")
        _core(model_root, "CORE0", "D", filename="B_CORE0")
        _partition(model_root, "P0", "C")

        assert "ASIL capability B" in _one(model_root, PARTITION).message
        assert "hardware.duplicate-name" in {r.rule for r in check_hardware_topology(str(model_root)).records}

    def test_an_unrated_first_beside_a_rated_second_is_silent(self, model_root: Path) -> None:
        _core(model_root, "CORE0", filename="A_CORE0")
        _core(model_root, "CORE0", "QM", filename="B_CORE0")
        _partition(model_root, "P0", "D")

        assert _records(model_root, PARTITION) == ()

    def test_a_core_and_a_memory_of_one_name_the_first_decides_whatever_its_kind(self, model_root: Path) -> None:
        _memory(model_root, "CORE0", "QM", filename="A_CORE0")
        _core(model_root, "CORE0", "QM", filename="B_CORE0")
        _partition(model_root, "P0", "D")

        assert _records(model_root, PARTITION) == ()
        assert "hardware.partition-core-not-a-core" in {
            r.rule for r in check_hardware_topology(str(model_root)).records
        }

    def test_two_partition_files_of_one_name_are_each_judged_by_their_own_core(self, model_root: Path) -> None:
        """Per partition FILE, as the memory rule judges every component file.

        `hardware.duplicate-name` reports the duplicate itself; this rule judges
        each object, so the second file's placement is not hidden behind the first.
        """
        _core(model_root, "CORE0", "B")
        _core(model_root, "CORE1", "A")
        _partition(model_root, "P0", "D", filename="A_P0")
        _partition(model_root, "P0", "C", core="CORE1", filename="B_P0")

        assert [record.subjects[1].element for record in _records(model_root, PARTITION)] == ["CORE0", "CORE1"]
        assert "hardware.duplicate-name" in {r.rule for r in check_hardware_topology(str(model_root)).records}

    def test_a_memory_resolves_first_wins_too(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _memory(model_root, "RAM0", "B", filename="A_RAM0")
        _memory(model_root, "RAM0", filename="B_RAM0")
        _swc(model_root, "Hi", asil="D", memory={"RAM0": 64})

        assert "ASIL capability B" in _one(model_root, MEMORY).message

    def test_an_unrated_first_memory_beside_a_rated_second_is_silent(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _memory(model_root, "RAM0", filename="A_RAM0")
        _memory(model_root, "RAM0", "QM", filename="B_RAM0")
        _swc(model_root, "Hi", asil="D", memory={"RAM0": 64})

        assert _records(model_root, MEMORY) == ()


# ---------------------------------------------------------------------------
# 2. rule:safety.memory-below-component
# ---------------------------------------------------------------------------


class TestTheMemoryRule:
    def test_a_component_above_a_memory_it_budgets_is_reported_naming_every_party(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _memory(model_root, "RAM0", "B")
        _swc(model_root, "Hi", asil="D", memory={"RAM0": 512})

        record = _one(model_root, MEMORY)

        for token in ("'Hi'", "(ASIL D)", "'RAM0'", "ASIL capability B", "512"):
            assert token in record.message, (token, record.message)
        assert record.subjects == (
            Subject("swc", "Hi", (field_step("asil"),)),
            Subject("swc", "Hi", (field_step("memory"), key_step("RAM0"))),
            Subject("hardware", "RAM0", (field_step("asil_capability"),)),
        )

    def test_a_zero_budget_declares_no_use_and_is_outside_the_rule(self, model_root: Path) -> None:
        """The decision `hardware.unreachable-memory` took, for the level.

        Under the default `enforcement: strict` a MISSING entry is
        `hardware.missing-budget`, so `0` is the only way a rated component names
        a lower-capability memory legally. Reading it as a use would make the two
        rules unsatisfiable together.
        """
        _core(model_root, "CORE0")
        _memory(model_root, "RAM0", "QM")
        _swc(model_root, "Hi", asil="D", memory={"RAM0": 0})

        assert _records(model_root, MEMORY) == ()

    def test_a_component_equal_to_the_memory_is_silent(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _memory(model_root, "RAM0", "B")
        _swc(model_root, "Eq", asil="B", memory={"RAM0": 128})

        assert _records(model_root, MEMORY) == ()

    def test_a_memory_declaring_no_capability_is_unrated(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _memory(model_root, "RAM0")
        _swc(model_root, "Hi", asil="D", memory={"RAM0": 128})

        assert _records(model_root, MEMORY) == ()

    def test_a_declared_qm_capability_under_a_rated_budget_is_reported(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _memory(model_root, "RAM0", "QM")
        _swc(model_root, "Low", asil="A", memory={"RAM0": 1})

        assert "ASIL capability QM" in _one(model_root, MEMORY).message

    def test_a_component_no_task_runs_is_reached_because_the_rule_reads_its_own_map(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _memory(model_root, "RAM0", "B")
        _swc(model_root, "Idle", asil="D", memory={"RAM0": 512}, runnables=[_runnable("Run")])

        assert _one(model_root, MEMORY).subjects[0] == Subject("swc", "Idle", (field_step("asil"),))
        assert not list(Path(model_root).glob("tasks/*.yaml"))

    def test_a_key_naming_no_declared_memory_is_the_topology_passs(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _swc(model_root, "Hi", asil="D", memory={"RAM9": 512})

        assert _records(model_root, MEMORY) == ()
        assert "hardware.undeclared-memory" in {r.rule for r in check_hardware_topology(str(model_root)).records}

    def test_a_key_naming_a_rated_core_is_the_topology_passs(self, model_root: Path) -> None:
        _core(model_root, "CORE0", "QM")
        _swc(model_root, "Hi", asil="D", memory={"CORE0": 512})

        assert _records(model_root, MEMORY) == ()
        assert "hardware.memory-not-a-memory" in {r.rule for r in check_hardware_topology(str(model_root)).records}

    def test_a_derived_level_names_only_the_requirements_above_the_memory_as_repairs(self, model_root: Path) -> None:
        """The core rule's case, for the memory: one requirement above, one equal, one `QM`."""
        _core(model_root, "CORE0")
        _memory(model_root, "RAM0", "B")
        _swc(model_root, "Hi", memory={"RAM0": 512})
        _requirement(model_root, "REQ_D", ["Hi"], asil="D")
        _requirement(model_root, "REQ_B", ["Hi"], asil="B")
        _requirement(model_root, "REQ_QM", ["Hi"], asil="QM")

        record = _one(model_root, MEMORY)

        assert "SWC 'Hi' (ASIL D)" in record.message, record.message
        assert ", derived from 'REQ_D' (ASIL D): " in record.message, record.message
        assert "REQ_B" not in record.message and "REQ_QM" not in record.message, record.message
        assert record.subjects == (
            Subject("swc", "Hi", (field_step("asil"),)),
            Subject("swc", "Hi", (field_step("memory"), key_step("RAM0"))),
            Subject("hardware", "RAM0", (field_step("asil_capability"),)),
            Subject("requirements", "REQ_D", (field_step("asil"),)),
        )

    def test_an_external_stub_with_a_budget_is_judged_like_any_other_component(self, model_root: Path) -> None:
        """`asil` and `memory` are values the project wrote into the stub's own file."""
        _core(model_root, "CORE0")
        _memory(model_root, "RAM0", "A")
        _swc(
            model_root,
            "Stub",
            external=True,
            external_ref="/Pkg/Stub",
            external_dest="APPLICATION-SW-COMPONENT-TYPE",
            asil="B",
            memory={"RAM0": 16},
        )

        assert "'Stub'" in _one(model_root, MEMORY).message

    def test_two_budgets_on_two_lower_memories_are_two_findings(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _memory(model_root, "RAM0", "A")
        _memory(model_root, "RAM1", "B")
        _swc(model_root, "Hi", asil="D", memory={"RAM0": 1, "RAM1": 2})

        assert [record.subjects[2].element for record in _records(model_root, MEMORY)] == ["RAM0", "RAM1"]


class TestReaderOrderDecidesNothing:
    """Two component files of one name is `name.duplicate-swc`'s finding.

    The reader yields `Src_a` before `Src_b`; what is parametrized is which of
    the two DECLARES the level, so a rule that let the last file decide passes
    one parametrization and fails the other.
    """

    @staticmethod
    def _duplicate(model_root: Path, declaring: str, **extra: Any) -> None:
        """`Src_a` and `Src_b`: *declaring* declares `D`, the other derives `C` from `REQ_1`."""
        for suffix in ("a", "b"):
            _swc(
                model_root,
                "Src",
                filename=f"Src_{suffix}",
                **({"asil": "D"} if suffix == declaring else {}),
                **extra,
            )
        _requirement(model_root, "REQ_1", ["Src"], asil="C")

    @pytest.mark.parametrize("declaring", ["a", "b"])
    def test_the_memory_rule_judges_each_file_by_its_own_level(self, model_root: Path, declaring: str) -> None:
        """Per OBJECT, as the provider rule is: each file's own map and its own level.

        A `C` memory is below the `D` file and equal to the `C` one, so exactly
        one finding, whichever file declares `D`.
        """
        _core(model_root, "CORE0")
        _memory(model_root, "RAM0", "C")
        self._duplicate(model_root, declaring, memory={"RAM0": 8})

        record = _one(model_root, MEMORY)

        assert "(ASIL D)" in record.message
        assert not [subject for subject in record.subjects if subject.layer == "requirements"]


# ---------------------------------------------------------------------------
# 2b. The three per-type rules
# ---------------------------------------------------------------------------


class TestTheComponentAbovePartitionRule:
    """`safety.component-above-partition` — nothing closes it, and the rule does not choose the repair."""

    def test_a_component_above_its_partition_is_reported_naming_every_party(self, model_root: Path) -> None:
        """The standard-table row *a component above its partition*: an OS-Application hosts its level and no more."""
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "B")
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi")])

        record = _one(model_root, ABOVE)

        assert record.message == (
            "SWC 'Hi' (ASIL D) is run by partition 'P0' (ASIL B): a component rated above the partition that runs it."
        )
        assert record.subjects == (
            Subject("hardware", "P0", (field_step("asil"),)),
            Subject("swc", "Hi", (field_step("asil"),)),
        )

    def test_an_argued_partition_is_reported_too_because_no_argument_closes_it(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "B", coexistence="Separated by memory protection.")
        _swc(model_root, "Hi", asil="C", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi")])

        assert _one(model_root, ABOVE).subjects[1] == Subject("swc", "Hi", (field_step("asil"),))

    @pytest.mark.parametrize("level", ["B", "A", "QM"])
    def test_a_component_at_or_below_its_partition_is_silent(self, model_root: Path, level: str) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "B", coexistence="Argued, so the lower levels are silent too.")
        _swc(model_root, "C", asil=level, runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("C")])

        assert _records(model_root, ABOVE) == ()

    def test_an_empty_partition_runs_nothing_and_mints_nothing(self, model_root: Path) -> None:
        """A `D` component no task of the partition runs is not the partition's."""
        _core(model_root, "CORE0")
        _partition(model_root, "P_Empty", "QM")
        _partition(model_root, "P_Other", "D")
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi")], partition="P_Other")

        assert _records(model_root, ABOVE) == ()

    def test_an_unrated_partition_is_compared_against_nothing(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P0")
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi")])

        assert _records(model_root, ABOVE) == ()

    def test_a_derived_level_names_only_the_requirements_above_the_partition_as_repairs(self, model_root: Path) -> None:
        """`_derived_level_repairs`' reading, against the partition's level: one above, one equal, one `QM`."""
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "B")
        _swc(model_root, "Hi", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi")])
        _requirement(model_root, "REQ_D", ["Hi"], asil="D")
        _requirement(model_root, "REQ_B", ["Hi"], asil="B")
        _requirement(model_root, "REQ_QM", ["Hi"], asil="QM")

        record = _one(model_root, ABOVE)

        assert record.message == (
            "SWC 'Hi' (ASIL D) is run by partition 'P0' (ASIL B), derived from 'REQ_D' (ASIL D): a component "
            "rated above the partition that runs it."
        )
        assert record.subjects == (
            Subject("hardware", "P0", (field_step("asil"),)),
            Subject("swc", "Hi", (field_step("asil"),)),
            Subject("requirements", "REQ_D", (field_step("asil"),)),
        )

    def test_a_component_run_by_two_tasks_of_the_partition_is_one_finding(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "A")
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run"), _runnable("Other")])
        _task(model_root, "T1", [_ref("Hi", "Run")])
        _task(model_root, "T2", [_ref("Hi", "Other")])

        assert _one(model_root, ABOVE).subjects[1].element == "Hi"

    def test_one_finding_per_partition_and_component_in_name_order(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P_a", "A")
        _partition(model_root, "P_b", "QM")
        _swc(model_root, "Zed", asil="D", runnables=[_runnable("Run")])
        _swc(model_root, "Alpha", asil="B", runnables=[_runnable("Run"), _runnable("Other")])
        _task(model_root, "T1", [_ref("Zed"), _ref("Alpha")], partition="P_a")
        _task(model_root, "T2", [_ref("Alpha", "Other")], partition="P_b")

        assert [(r.subjects[0].element, r.subjects[1].element) for r in _records(model_root, ABOVE)] == [
            ("P_a", "Alpha"),
            ("P_a", "Zed"),
            ("P_b", "Alpha"),
        ]


class TestTheComponentBelowPartitionRule:
    """`safety.component-below-partition` — mixing inside one partition, closed by the partition's argument."""

    def test_mixing_without_an_argument_is_reported_naming_every_party(self, model_root: Path) -> None:
        """The standard-table row *mixing inside one partition*, without an argument."""
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "D")
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run")])
        _swc(model_root, "Lo", asil="B", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi"), _ref("Lo")])

        record = _one(model_root, BELOW)

        assert record.message == (
            "SWC 'Lo' (ASIL B) is run by partition 'P0' (ASIL D), which declares no coexistence argument: a "
            "component rated below the partition that runs it, sharing its memory protection."
        )
        assert record.subjects == (
            Subject("hardware", "P0", (field_step("coexistence"),)),
            Subject("hardware", "P0", (field_step("asil"),)),
            Subject("swc", "Lo", (field_step("asil"),)),
        )

    def test_mixing_with_an_argument_is_silent(self, model_root: Path) -> None:
        """The same row, with the partition's `coexistence` declared."""
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "D", coexistence="The two components are separated by the MPU.")
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run")])
        _swc(model_root, "Lo", asil="B", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi"), _ref("Lo")])

        assert _records(model_root, BELOW) == ()

    def test_a_qm_component_beside_a_rated_partition_is_below_it(self, model_root: Path) -> None:
        """`QM` is a level and is below every ASIL; the partition rated `A` runs it alone and is still mixed."""
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "A")
        _swc(model_root, "Plain", asil="QM", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Plain")])

        assert "SWC 'Plain' (ASIL QM)" in _one(model_root, BELOW).message

    def test_a_derived_qm_is_below_it_too(self, model_root: Path) -> None:
        """A component nothing allocates a level to derives `QM`, as every effective-level rule reads it."""
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "B")
        _swc(model_root, "Plain", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Plain")])

        assert "SWC 'Plain' (ASIL QM)" in _one(model_root, BELOW).message

    def test_a_qm_partition_has_nothing_below_it(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "QM")
        _swc(model_root, "Plain", asil="QM", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Plain")])

        assert _records(model_root, BELOW) == ()

    def test_an_unrated_partition_is_compared_against_nothing(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P0")
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run")])
        _swc(model_root, "Lo", asil="QM", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi"), _ref("Lo")])

        assert _records(model_root, BELOW) == ()

    def test_an_empty_partition_runs_nothing_and_mints_nothing(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P_Empty", "D")
        _swc(model_root, "Lo", asil="QM", runnables=[_runnable("Run")])

        assert _records(model_root, BELOW) == ()

    def test_the_task_argument_does_not_answer_the_partition_rule(self, model_root: Path) -> None:
        """A task's argument does not close the partition rule: execution-time and memory mixing are two questions."""
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "D")
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run")])
        _swc(model_root, "Lo", asil="B", runnables=[_runnable("Run")])
        _write(
            model_root / "tasks" / "T.yaml",
            {
                "name": "T",
                "period": 0.01,
                "priority": 1,
                "partition": "P0",
                "coexistence": "Timing protection separates the two runnables.",
                "runnables": [_ref("Hi"), _ref("Lo")],
            },
        )

        assert _one(model_root, BELOW).subjects[2] == Subject("swc", "Lo", (field_step("asil"),))
        assert "safety.task-mixed-asil" not in {
            record.rule for record in check_cross_layer_consistency(str(model_root)).records
        }


class TestThePartitionMemoryRule:
    """`safety.partition-memory-below-level` — what a partition's components budget, against its level."""

    def test_a_memory_below_the_partitions_level_is_reported_naming_every_party(self, model_root: Path) -> None:
        """The standard-table row *a memory below the partition's level*."""
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "C", coexistence="Argued.")
        _memory(model_root, "RAM0", "A")
        _swc(model_root, "Lo", asil="A", runnables=[_runnable("Run")], memory={"RAM0": 32})
        _task(model_root, "T", [_ref("Lo")])

        record = _one(model_root, PARTITION_MEMORY)

        assert record.message == (
            "Partition 'P0' (ASIL C) runs 'Lo', budgeting memory 'RAM0' (ASIL capability A): a partition whose "
            "components charge a memory not declared able to host its level."
        )
        assert record.subjects == (
            Subject("hardware", "RAM0", (field_step("asil_capability"),)),
            Subject("hardware", "P0", (field_step("asil"),)),
            Subject("swc", "Lo", (field_step("memory"), key_step("RAM0"))),
        )
        # The component itself is at the memory's level: the per-component rule is silent.
        assert _records(model_root, MEMORY) == ()

    def test_two_components_on_one_memory_are_one_finding_naming_both(self, model_root: Path) -> None:
        """Deduplicated per `(partition, memory)`, the contributing components in name order."""
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "D", coexistence="Argued.")
        _memory(model_root, "RAM0", "B")
        _swc(model_root, "Zed", asil="B", runnables=[_runnable("Run")], memory={"RAM0": 8})
        _swc(model_root, "Alpha", asil="A", runnables=[_runnable("Run")], memory={"RAM0": 16})
        _task(model_root, "T", [_ref("Zed"), _ref("Alpha")])

        record = _one(model_root, PARTITION_MEMORY)

        assert "runs 'Alpha', 'Zed', budgeting memory 'RAM0'" in record.message, record.message
        assert record.subjects[2:] == (
            Subject("swc", "Alpha", (field_step("memory"), key_step("RAM0"))),
            Subject("swc", "Zed", (field_step("memory"), key_step("RAM0"))),
        )

    def test_two_memories_are_two_findings_in_name_order(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "D", coexistence="Argued.")
        _memory(model_root, "RAM1", "A")
        _memory(model_root, "RAM0", "QM")
        _swc(model_root, "C", asil="D", runnables=[_runnable("Run")], memory={"RAM1": 1, "RAM0": 1})
        _task(model_root, "T", [_ref("C")])

        assert [record.subjects[0].element for record in _records(model_root, PARTITION_MEMORY)] == ["RAM0", "RAM1"]

    def test_two_partitions_charging_one_memory_are_two_findings(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P_a", "C")
        _partition(model_root, "P_b", "D")
        _memory(model_root, "RAM0", "B")
        _swc(model_root, "X", asil="C", runnables=[_runnable("Run")], memory={"RAM0": 1})
        _swc(model_root, "Y", asil="D", runnables=[_runnable("Run")], memory={"RAM0": 1})
        _task(model_root, "T1", [_ref("X")], partition="P_a")
        _task(model_root, "T2", [_ref("Y")], partition="P_b")

        assert [record.subjects[1].element for record in _records(model_root, PARTITION_MEMORY)] == ["P_a", "P_b"]

    def test_a_zero_budget_declares_no_use(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "D", coexistence="Argued.")
        _memory(model_root, "RAM0", "QM")
        _swc(model_root, "C", asil="QM", runnables=[_runnable("Run")], memory={"RAM0": 0})
        _task(model_root, "T", [_ref("C")])

        assert _records(model_root, PARTITION_MEMORY) == ()

    def test_a_memory_at_or_above_the_partitions_level_is_silent(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "B", coexistence="Argued.")
        _memory(model_root, "RAM0", "B")
        _memory(model_root, "RAM1", "D")
        _swc(model_root, "C", asil="QM", runnables=[_runnable("Run")], memory={"RAM0": 4, "RAM1": 4})
        _task(model_root, "T", [_ref("C")])

        assert _records(model_root, PARTITION_MEMORY) == ()

    def test_an_unrated_memory_is_compared_against_nothing(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "D")
        _memory(model_root, "RAM0")
        _swc(model_root, "C", asil="D", runnables=[_runnable("Run")], memory={"RAM0": 4})
        _task(model_root, "T", [_ref("C")])

        assert _records(model_root, PARTITION_MEMORY) == ()

    def test_an_unrated_partition_is_compared_against_nothing(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P0")
        _memory(model_root, "RAM0", "QM")
        _swc(model_root, "C", asil="QM", runnables=[_runnable("Run")], memory={"RAM0": 4})
        _task(model_root, "T", [_ref("C")])

        assert _records(model_root, PARTITION_MEMORY) == ()

    def test_an_empty_partition_charges_nothing(self, model_root: Path) -> None:
        """Rule 2 still reaches the component no task runs; the partition rule does not."""
        _core(model_root, "CORE0")
        _partition(model_root, "P_Empty", "D")
        _memory(model_root, "RAM0", "QM")
        _swc(model_root, "Idle", asil="A", runnables=[_runnable("Run")], memory={"RAM0": 4})

        assert _records(model_root, PARTITION_MEMORY) == ()
        assert _one(model_root, MEMORY).subjects[0] == Subject("swc", "Idle", (field_step("asil"),))

    def test_a_key_naming_no_memory_is_the_topology_passs(self, model_root: Path) -> None:
        _core(model_root, "CORE0", "QM")
        _partition(model_root, "P0", "D", coexistence="Argued.")
        _swc(model_root, "C", asil="QM", runnables=[_runnable("Run")], memory={"CORE0": 4, "RAM9": 4})
        _task(model_root, "T", [_ref("C")])

        assert _records(model_root, PARTITION_MEMORY) == ()


class TestWhereThePerTypeRulesAbstain:
    """A broken reference is the topology pass's, and a duplicate is reported once."""

    def test_a_task_naming_no_declared_partition_is_counted_by_none(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "QM")
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi")], partition="P9")

        assert {record.rule for record in _records(model_root)} == set()
        assert "hardware.undeclared-partition" in {r.rule for r in check_hardware_topology(str(model_root)).records}

    def test_a_task_naming_a_core_as_its_partition_is_counted_by_none(self, model_root: Path) -> None:
        _core(model_root, "CORE0", "QM")
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi")], partition="CORE0")

        assert _records(model_root) == ()
        assert "hardware.partition-not-a-partition" in {
            r.rule for r in check_hardware_topology(str(model_root)).records
        }

    @pytest.mark.parametrize("core", ["CORE9", "RAM0"])
    def test_a_partition_on_no_core_is_the_topology_passs(self, model_root: Path, core: str) -> None:
        _core(model_root, "CORE0")
        _memory(model_root, "RAM0", "QM")
        _partition(model_root, "P0", "B", core=core)
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run")], memory={"RAM0": 4})
        _swc(model_root, "Lo", asil="QM", runnables=[_runnable("Run")], memory={"RAM0": 0})
        _task(model_root, "T", [_ref("Hi"), _ref("Lo")])

        assert {record.rule for record in _records(model_root)} == {MEMORY}

    def test_a_runnable_naming_no_declared_component_contributes_nothing(self, model_root: Path) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "D")
        _task(model_root, "T", [_ref("Ghost")])

        assert _records(model_root) == ()

    def test_two_partition_files_of_one_name_judge_what_runs_once(self, model_root: Path) -> None:
        """The first file decides; the second runs nothing here (rule 1 still judges each file)."""
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "B", filename="A_P0")
        _partition(model_root, "P0", "A", filename="B_P0")
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi")])

        record = _one(model_root, ABOVE)
        assert "partition 'P0' (ASIL B)" in record.message

    def test_a_component_file_of_a_duplicate_name_is_read_first_wins(self, model_root: Path) -> None:
        """`check_safety_coexistence`'s `by_swc_name`: the first file of a name is the component a runnable names."""
        _core(model_root, "CORE0")
        _partition(model_root, "P0", "B")
        _swc(model_root, "Src", filename="Src_a", asil="D", runnables=[_runnable("Run")])
        _swc(model_root, "Src", filename="Src_b", asil="B", runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Src")])

        assert "(ASIL D)" in _one(model_root, ABOVE).message


class TestThePlacementRulesCompareByX:
    """Every placement rule compares a decomposed level by ``X``.

    These are the rules where a rated value (one of eighteen) meets a capability
    (one of five) or another rated value. A tie by ``X`` fires nothing; a lower
    ``X`` fires and the message names each value as written.
    """

    @pytest.mark.parametrize(("level", "fires"), [("C(D)", False), ("D(D)", True)])
    def test_the_partition_rule(self, model_root: Path, level: str, fires: bool) -> None:
        _core(model_root, "CORE0", "C")
        _partition(model_root, "P0", level)

        records = _records(model_root, PARTITION)

        assert len(records) == fires
        if fires:
            assert f"Partition 'P0' (ASIL {level}) is on core 'CORE0' (ASIL capability C)" in records[0].message

    @pytest.mark.parametrize(("level", "fires"), [("B(D)", False), ("C(D)", True)])
    def test_the_memory_rule(self, model_root: Path, level: str, fires: bool) -> None:
        _core(model_root, "CORE0")
        _memory(model_root, "RAM0", "B")
        _swc(model_root, "Hi", asil=level, memory={"RAM0": 64})

        records = _records(model_root, MEMORY)

        assert len(records) == fires
        if fires:
            assert f"(ASIL {level})" in records[0].message

    @pytest.mark.parametrize(("partition", "component", "fires"), [("B(D)", "B", False), ("B", "C(D)", True)])
    def test_the_component_above_partition_rule(
        self, model_root: Path, partition: str, component: str, fires: bool
    ) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P0", partition)
        _swc(model_root, "Hi", asil=component, runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi")])

        records = _records(model_root, ABOVE)

        assert len(records) == fires
        if fires:
            assert records[0].message.startswith(
                f"SWC 'Hi' (ASIL {component}) is run by partition 'P0' (ASIL {partition})"
            )

    @pytest.mark.parametrize(
        ("partition", "levels", "fires"),
        [("B(D)", ("B", "B(C)"), False), ("D(D)", ("D", "B(D)"), True)],
    )
    def test_the_component_below_partition_rule_and_the_guards_reading(
        self, model_root: Path, partition: str, levels: tuple[str, str], fires: bool
    ) -> None:
        """Rule ``component-below-partition`` and ``safety_reading.mixed_partitions`` agree by ``X``."""
        _core(model_root, "CORE0")
        _partition(model_root, "P0", partition)
        _swc(model_root, "Hi", asil=levels[0], runnables=[_runnable("Run")])
        _swc(model_root, "Lo", asil=levels[1], runnables=[_runnable("Run")])
        _task(model_root, "T", [_ref("Hi"), _ref("Lo")])

        records = _records(model_root, BELOW)
        reading = _safety_reading(model_root)

        assert len(records) == fires
        assert (reading.mixed_partitions == frozenset({"P0"})) is fires
        if fires:
            assert records[0].message.startswith(
                f"SWC 'Lo' (ASIL {levels[1]}) is run by partition 'P0' (ASIL {partition})"
            )

    @pytest.mark.parametrize(("partition", "fires"), [("C(D)", False), ("D(D)", True)])
    def test_the_partition_memory_rule(self, model_root: Path, partition: str, fires: bool) -> None:
        _core(model_root, "CORE0")
        _partition(model_root, "P0", partition, coexistence="Argued.")
        _memory(model_root, "RAM0", "C")
        _swc(model_root, "Lo", asil="C", runnables=[_runnable("Run")], memory={"RAM0": 32})
        _task(model_root, "T", [_ref("Lo")])

        records = _records(model_root, PARTITION_MEMORY)

        assert len(records) == fires
        if fires:
            assert records[0].message.startswith(f"Partition 'P0' (ASIL {partition}) runs 'Lo'")


class TestTheTrustRules:
    """Rules 6 and 7 — the fire/no-fire table, one test per row.

    Every model holds one core ``CORE0`` declaring no capability, so rule 1 is
    silent throughout, and no task, so rules 3–5 are too: the trust rules read
    the partitions' declarations alone.
    """

    @pytest.fixture
    def root(self, model_root: Path) -> Path:
        _core(model_root, "CORE0")
        return model_root

    def test_a_trusted_b_beside_d_fires_rule_6_naming_the_d_partition(self, root: Path) -> None:
        _partition(root, "P_Trusted", "B", trust="trusted")
        _partition(root, "P_High", "D")
        record = _one(root, TRUSTED_BELOW)
        assert record.message == (
            "Partition 'P_Trusted' (trusted, ASIL B) shares the processor with partition 'P_High' (ASIL D): "
            "a trusted partition, unrestricted for memory, rated below a partition whose memory it may write."
        )
        assert record.subjects == (
            Subject("hardware", "P_Trusted", (field_step("trust"),)),
            Subject("hardware", "P_Trusted", (field_step("asil"),)),
            Subject("hardware", "P_High", (field_step("asil"),)),
        )
        assert _records(root) == (record,)

    def test_a_trusted_d_beside_b_is_silent(self, root: Path) -> None:
        _partition(root, "P_Trusted", "D", trust="trusted")
        _partition(root, "P_Low", "B")
        assert _records(root) == ()

    def test_a_trusted_d_tied_with_a_d_is_silent(self, root: Path) -> None:
        _partition(root, "P_High", "D")
        _partition(root, "P_Trusted", "D", trust="trusted")
        assert _records(root) == ()

    def test_a_trusted_qm_beside_a_fires_rule_6_because_qm_is_rated(self, root: Path) -> None:
        _partition(root, "P_Trusted", "QM", trust="trusted")
        _partition(root, "P_A", "A")
        record = _one(root, TRUSTED_BELOW)
        assert "'P_Trusted' (trusted, ASIL QM)" in record.message and "'P_A' (ASIL A)" in record.message
        assert _records(root, TRUSTED_UNRATED) == ()

    def test_a_trusted_qm_beside_qm_is_silent(self, root: Path) -> None:
        _partition(root, "P_Trusted", "QM", trust="trusted")
        _partition(root, "P_Other", "QM")
        assert _records(root) == ()

    def test_a_trusted_unrated_beside_d_fires_rule_7(self, root: Path) -> None:
        _partition(root, "P_Trusted", trust="trusted")
        _partition(root, "P_High", "D")
        record = _one(root, TRUSTED_UNRATED)
        assert record.message == (
            "Partition 'P_Trusted' (trusted, unrated) shares the processor with partition 'P_High' (ASIL D): "
            "a trusted partition of unknown assurance, unrestricted for memory, beside a partition rated above QM."
        )
        assert record.subjects == (
            Subject("hardware", "P_Trusted", (field_step("trust"),)),
            Subject("hardware", "P_Trusted", (field_step("asil"),)),
        )
        assert _records(root) == (record,)

    @pytest.mark.parametrize("level", ["A", "B", "C", "D"])
    def test_a_trusted_unrated_beside_any_level_above_qm_fires_rule_7(self, root: Path, level: str) -> None:
        # `A` is the lower boundary: the rule fires on a highest level above QM,
        # not above A.
        _partition(root, "P_Trusted", trust="trusted")
        _partition(root, "P_Rated", level)
        record = _one(root, TRUSTED_UNRATED)
        assert f"partition 'P_Rated' (ASIL {level})" in record.message
        assert _records(root) == (record,)

    def test_a_trusted_unrated_beside_qm_only_is_silent(self, root: Path) -> None:
        _partition(root, "P_Trusted", trust="trusted")
        _partition(root, "P_Qm", "QM")
        _partition(root, "P_Qm2", "QM")
        assert _records(root) == ()

    @pytest.mark.parametrize("neighbours", [0, 1, 2])
    def test_a_trusted_unrated_alone_or_beside_unrated_only_is_silent(self, root: Path, neighbours: int) -> None:
        _partition(root, "P_Trusted", trust="trusted")
        for index in range(neighbours):
            _partition(root, f"P_Unrated{index}")
        assert _records(root) == ()

    def test_trusted_with_protection_fires_neither(self, root: Path) -> None:
        _partition(root, "P_Below", "B", trust="trusted_with_protection")
        _partition(root, "P_Unrated", trust="trusted_with_protection")
        _partition(root, "P_High", "D")
        assert _records(root) == ()

    @pytest.mark.parametrize("level", [None, "QM", "A", "B", "C"])
    def test_non_trusted_fires_neither_declared_or_by_default(self, root: Path, level: str | None) -> None:
        _partition(root, "P_Declared", level, trust="non_trusted")
        _partition(root, "P_Default", level)
        _partition(root, "P_High", "D")
        assert _records(root) == ()

    @pytest.mark.parametrize("level", ["B", None], ids=["rule-6", "rule-7"])
    def test_a_trusted_duplicate_second_file_abstains(self, root: Path, level: str | None) -> None:
        # The first file of the name is `non_trusted`; the `trusted` second one
        # is `hardware.duplicate-name`'s, and no task's name can resolve to it.
        _partition(root, "P_Dup", level, filename="a_first")
        _partition(root, "P_Dup", level, filename="b_second", trust="trusted")
        _partition(root, "P_High", "D")
        assert _records(root) == ()

    @pytest.mark.parametrize("level", ["B", None], ids=["rule-6", "rule-7"])
    def test_a_trusted_partition_on_an_unresolved_core_abstains(self, root: Path, level: str | None) -> None:
        _partition(root, "P_Trusted", level, core="NO_SUCH_CORE", trust="trusted")
        _partition(root, "P_High", "D")
        assert _records(root) == ()

    def test_a_d_partition_on_an_unresolved_core_is_not_compared_against(self, root: Path) -> None:
        _partition(root, "P_Trusted", "B", trust="trusted")
        _partition(root, "P_High", "D", core="NO_SUCH_CORE")
        assert _records(root) == ()

    def test_the_highest_tie_names_the_first_in_reader_order(self, root: Path) -> None:
        # `read_hardware` orders by declared name, so `P_First` is read first.
        _partition(root, "P_Trusted", "B", trust="trusted")
        _partition(root, "P_Second", "D")
        _partition(root, "P_First", "D")
        assert "partition 'P_First' (ASIL D)" in _one(root, TRUSTED_BELOW).message

    @pytest.mark.parametrize(("trusted", "other"), [("B(D)", "B"), ("B", "B(D)"), ("B(C)", "B(D)")])
    def test_a_trusted_partition_tied_by_x_fires_neither_rule(self, root: Path, trusted: str, other: str) -> None:
        """The highest level and the comparison are by ``X``; ``(Y)`` decides nothing.

        The highest level is ``max_asil``'s and the comparison is by
        :func:`asil_rank`, so a ``B`` and a ``B(D)`` partition tie, and a trusted
        one tied with the highest is not below it.
        """
        _partition(root, "P_Trusted", trusted, trust="trusted")
        _partition(root, "P_Other", other)
        assert _records(root) == ()

    @pytest.mark.parametrize(
        ("first", "second", "named"),
        [
            ("C(D)", "C", "partition 'P_B' (ASIL C)"),
            ("C", "C(D)", "partition 'P_A' (ASIL C)"),
            ("C(C)", "C(D)", "partition 'P_B' (ASIL C(D))"),
            ("C(D)", "C(C)", "partition 'P_A' (ASIL C(D))"),
        ],
    )
    @pytest.mark.parametrize(("level", "rule"), [("A", TRUSTED_BELOW), (None, TRUSTED_UNRATED)])
    def test_the_highest_tied_by_x_is_decided_by_the_values_not_reader_order(
        self, root: Path, first: str, second: str, named: str, level: str | None, rule: str
    ) -> None:
        """The named highest is ``max_asil``'s tie rule, whichever file is read first.

        A plain spelling wins over a decomposed one, and the higher ``Y`` among
        decomposed ones; the partition named is the first carrying exactly that
        value. Which rule fires is unchanged: it reads the highest's ``X`` alone.
        """
        _partition(root, "P_Trusted", level, trust="trusted")
        _partition(root, "P_A", first)
        _partition(root, "P_B", second)
        assert named in _one(root, rule).message
        assert len(_records(root)) == 1

    def test_a_trusted_decomposed_partition_below_by_x_fires_rule_6_naming_both_as_written(self, root: Path) -> None:
        _partition(root, "P_Trusted", "B(D)", trust="trusted")
        _partition(root, "P_High", "C(D)")
        record = _one(root, TRUSTED_BELOW)
        assert "'P_Trusted' (trusted, ASIL B(D))" in record.message
        assert "partition 'P_High' (ASIL C(D))" in record.message

    def test_a_trusted_unrated_beside_a_decomposed_qm_only_is_silent(self, root: Path) -> None:
        """Rule 7 asks for a highest level above ``QM`` by ``X``: ``QM(D)`` is not one."""
        _partition(root, "P_Trusted", trust="trusted")
        _partition(root, "P_Qm", "QM(D)")
        assert _records(root) == ()

    def test_two_trusted_partitions_are_two_findings_in_reader_order(self, root: Path) -> None:
        _partition(root, "P0_High", "D")
        _partition(root, "P1_B", "B", trust="trusted")
        _partition(root, "P2_Unrated", trust="trusted")
        _partition(root, "P3_A", "A", trust="trusted")
        assert [(record.rule, record.subjects[0].element) for record in _records(root)] == [
            (TRUSTED_BELOW, "P1_B"),
            (TRUSTED_UNRATED, "P2_Unrated"),
            (TRUSTED_BELOW, "P3_A"),
        ]


class TestThePartitionRunsHelper:
    """`partition_runs` — the one spelling of what a partition runs, `_task_component_levels` merged."""

    def test_it_merges_every_task_of_the_partition_and_no_other(self) -> None:
        from swcraft_tools.schema import SWComponent, Task
        from swcraft_tools.validator.asil import partition_runs

        def task(name: str, partition: str, *swcs: str) -> Task:
            return Task.model_validate(
                {
                    "name": name,
                    "period": 0.01,
                    "priority": 1,
                    "partition": partition,
                    "runnables": [{"swc": swc, "runnable": f"R_{name}"} for swc in swcs],
                }
            )

        runnables = [{"name": f"R_{task_name}"} for task_name in ("T1", "T2", "T3")]
        by_name = {
            name: SWComponent.model_validate(
                {"name": name, "type": "application", "asil": level, "runnables": runnables}
            )
            for name, level in (("A", "D"), ("B", "QM"), ("C", "B"))
        }
        tasks = [task("T1", "P0", "A", "B"), task("T2", "P1", "C"), task("T3", "P0", "A", "Ghost")]

        # No composition layer: the walk is undefined and every `swc` entry reads
        # as the type it writes (the syntactic fallback).
        identities = identities_from([])
        runs = partition_runs("P0", tasks, by_name, lambda swc: swc.asil or "QM", identities)

        assert runs == {"A": "D", "B": "QM"}
        assert partition_runs("P_Empty", tasks, by_name, lambda swc: swc.asil or "QM", identities) == {}

    def test_an_entry_naming_an_undeclared_runnable_is_not_counted(self) -> None:
        """A mistyped runnable is `task.mapping-unknown-runnable`'s finding and runs nothing here.

        The split rule skips such an entry too; a partition rule counting it would
        report one defect twice, against a rating nobody changed.
        """
        from swcraft_tools.schema import SWComponent, Task
        from swcraft_tools.validator.asil import partition_runs

        by_name = {
            "A": SWComponent.model_validate(
                {"name": "A", "type": "application", "asil": "D", "runnables": [{"name": "Run"}]}
            )
        }
        typo = Task.model_validate(
            {
                "name": "T",
                "period": 0.01,
                "priority": 1,
                "partition": "P0",
                "runnables": [{"swc": "A", "runnable": "Rnu"}],
            }
        )

        assert partition_runs("P0", [typo], by_name, lambda swc: swc.asil or "QM", identities_from([])) == {}


# ---------------------------------------------------------------------------
# 3. The split, the registration and the composition
# ---------------------------------------------------------------------------


def _basic_with_both_rules_minting(root: Path) -> Path:
    """``examples/basic`` with the requirement at `D`, its partition rated `D` and its core and memory rated `B`.

    Both components derive `D` and budget ``RAM0`` a non-zero amount, and
    ``PARTITION0`` declares `D` on the `B` core, so both rules mint over a tree
    the acceptance fixture does not share.
    """
    shutil.copytree(REPO_ROOT / "examples" / "basic" / "model", root)
    for relative, key, value in (
        ("requirements/REQ_EXAMPLE_001.yaml", "asil", "D"),
        ("hardware/CORE0.yaml", "asil_capability", "B"),
        ("hardware/PARTITION0.yaml", "asil", "D"),
        ("hardware/RAM0.yaml", "asil_capability", "B"),
    ):
        document = yaml.safe_load((root / relative).read_text(encoding="utf-8"))
        document[key] = value
        _write(root / relative, document)
    return root


class TestTheReadingAndTheRuleAreTwoFunctions:
    @pytest.mark.parametrize("tree", ["acceptance-fixture", "basic-injected"])
    def test_the_records_function_over_layers_already_read_is_the_check(self, tree: str, tmp_path: Path) -> None:
        """The records function over four layers already read is the check itself.

        It is what ``binding.json`` names and what the guard-versus-rule sweep in
        ``test_editing_authorization.py`` reads over snapshots. Both trees mint
        findings, so the equality is never ``() == ()``.
        """
        root = SAFETY_MODEL_ROOT if tree == "acceptance-fixture" else _basic_with_both_rules_minting(tmp_path / "model")
        model_root = str(root)
        in_memory = hardware_capability_records(
            read_hardware(model_root),
            read_tasks(model_root),
            read_swc_model(model_root),
            read_requirements(model_root),
            read_compositions(model_root),
        )

        # Every rule but `component-above-partition` mints on the fixture (nothing
        # is above `D`); on the injected tree the components derive `D` like
        # their partition, so neither component rule does.
        expected = (
            {PARTITION, MEMORY, BELOW, PARTITION_MEMORY}
            if tree == "acceptance-fixture"
            else {PARTITION, MEMORY, PARTITION_MEMORY}
        )
        assert {record.rule for record in in_memory} == expected, in_memory
        assert tuple(in_memory) == check_hardware_capability(model_root).records


#: The seven `appliesTo` lists, written out rather than read from the contract,
#: so a narrowed list fails here: the partition rule's the partition's `asil`
#: and `core` and the core's `asil_capability`; the three per-type rules' the
#: partition's identity, level and core, the task fields that say what it runs,
#: the component's level and the requirements it may derive from, and the
#: argument or the memory each reads.
DESIGN_APPLIES_TO: dict[str, list[str]] = {
    PARTITION: [
        "field:hardware.partition.name",
        "field:hardware.partition.asil",
        "field:hardware.partition.core",
        "field:hardware.core.name",
        "field:hardware.core.asil_capability",
    ],
    MEMORY: [
        "field:swc.swc.name",
        "field:swc.swc.asil",
        "field:swc.swc.memory",
        "field:hardware.memory.name",
        "field:hardware.memory.asil_capability",
        "field:requirements.requirement.asil",
        "field:swc.swc.satisfies",
        "field:swc.port.satisfies",
        "field:swc.runnable.satisfies",
    ],
    ABOVE: [
        "field:hardware.partition.name",
        "field:hardware.partition.asil",
        "field:hardware.partition.core",
        "field:hardware.core.name",
        "field:tasks.task.partition",
        "field:tasks.task.runnables",
        "field:tasks.task_runnable_ref.swc",
        "field:tasks.task_runnable_ref.instance",
        "field:tasks.task_runnable_ref.runnable",
        "field:swc.swc.name",
        "field:swc.swc.runnables",
        "field:swc.runnable.name",
        "field:swc.swc.asil",
        "field:requirements.requirement.id",
        "field:requirements.requirement.asil",
        "field:swc.swc.satisfies",
        "field:swc.port.satisfies",
        "field:swc.runnable.satisfies",
    ],
    BELOW: [
        "field:hardware.partition.name",
        "field:hardware.partition.asil",
        "field:hardware.partition.core",
        "field:hardware.core.name",
        "field:hardware.partition.coexistence",
        "field:tasks.task.partition",
        "field:tasks.task.runnables",
        "field:tasks.task_runnable_ref.swc",
        "field:tasks.task_runnable_ref.instance",
        "field:tasks.task_runnable_ref.runnable",
        "field:swc.swc.name",
        "field:swc.swc.runnables",
        "field:swc.runnable.name",
        "field:swc.swc.asil",
        "field:requirements.requirement.asil",
        "field:swc.swc.satisfies",
        "field:swc.port.satisfies",
        "field:swc.runnable.satisfies",
    ],
    PARTITION_MEMORY: [
        "field:hardware.partition.name",
        "field:hardware.partition.asil",
        "field:hardware.partition.core",
        "field:hardware.core.name",
        "field:tasks.task.partition",
        "field:tasks.task.runnables",
        "field:tasks.task_runnable_ref.swc",
        "field:tasks.task_runnable_ref.instance",
        "field:tasks.task_runnable_ref.runnable",
        "field:swc.swc.name",
        "field:swc.swc.runnables",
        "field:swc.runnable.name",
        "field:swc.swc.asil",
        "field:swc.swc.memory",
        "field:hardware.memory.name",
        "field:hardware.memory.asil_capability",
        "field:requirements.requirement.asil",
        "field:swc.swc.satisfies",
        "field:swc.port.satisfies",
        "field:swc.runnable.satisfies",
    ],
    # The trust rules: the partition's identity, level, trust and core, and
    # the core's identity the resolution condition reads.
    TRUSTED_BELOW: [
        "field:hardware.partition.name",
        "field:hardware.partition.asil",
        "field:hardware.partition.trust",
        "field:hardware.partition.core",
        "field:hardware.core.name",
    ],
    TRUSTED_UNRATED: [
        "field:hardware.partition.name",
        "field:hardware.partition.asil",
        "field:hardware.partition.trust",
        "field:hardware.partition.core",
        "field:hardware.core.name",
    ],
}


class TestTheRulesAreRegisteredEverywhereOnce:
    @pytest.mark.parametrize("rule", RULES)
    def test_the_rule_is_in_the_closed_inventory_with_the_contracts_summary(self, rule: str) -> None:
        declared = {entry["id"]: entry for entry in load_contract()["rules"]}
        assert CONSISTENCY_RULES[rule] == declared[f"rule:{rule}"]["summary"]

    @pytest.mark.parametrize("rule", RULES)
    def test_the_protocol_maps_it_to_consistency_violation_in_a_group_of_its_own(self, rule: str) -> None:
        assert RULE_CODES[rule].code == CONSISTENCY_VIOLATION
        assert RULE_CODES[rule].group == rule

    @pytest.mark.parametrize("rule", RULES)
    def test_the_contract_declares_it_cross_element_with_the_designs_applies_to(self, rule: str) -> None:
        declared = {entry["id"]: entry for entry in load_contract()["rules"]}
        entry = declared[f"rule:{rule}"]
        assert entry["family"] == "cross-element"
        assert entry["appliesTo"] == DESIGN_APPLIES_TO[rule]

    #: The phase of `hardware_capability_records` that mints each rule, the
    #: callable `binding.json` names for it.
    BOUND_PHASE = {
        PARTITION: "_partition_core_capability_records",
        MEMORY: "_memory_below_component_records",
        ABOVE: "_component_above_partition_records",
        BELOW: "_component_below_partition_records",
        PARTITION_MEMORY: "_partition_memory_records",
        TRUSTED_BELOW: "_trusted_partition_records",
        TRUSTED_UNRATED: "_trusted_partition_records",
    }

    @pytest.mark.parametrize("rule", RULES)
    def test_the_binding_names_the_records_function(self, rule: str) -> None:
        """The binding names the phase of `hardware_capability_records` that mints the rule.

        The phase is one the records function calls, read off its syntax tree, so
        a binding onto a function the records function never reaches fails.
        """
        assert load_binding()["rules"][f"rule:{rule}"] == {
            "module": "swcraft_tools.validator.hardware",
            "callable": self.BOUND_PHASE[rule],
        }
        assert self.BOUND_PHASE[rule] in validator_package.reached_functions(
            validator_hardware, "hardware_capability_records"
        )

    def test_every_emission_site_yields_the_layers_the_contract_declares(self) -> None:
        observed = emission_layers()
        expected = {
            PARTITION: frozenset({"hardware"}),
            MEMORY: frozenset({"swc", "hardware", "requirements"}),
            ABOVE: frozenset({"hardware", "swc", "requirements"}),
            BELOW: frozenset({"hardware", "swc"}),
            PARTITION_MEMORY: frozenset({"hardware", "swc"}),
            TRUSTED_BELOW: frozenset({"hardware"}),
            TRUSTED_UNRATED: frozenset({"hardware"}),
        }
        for rule, layers in expected.items():
            sites = observed[f"rule:{rule}"]
            assert sites and all(site == layers for site in sites), (rule, sites)
        assert cross_element_layer_failures(load_contract(), observed) == []


class TestTheFieldsAreDeclaredOnCoreAndMemoryAndNowhereElse:
    @pytest.mark.parametrize("kind", ["core", "memory"])
    def test_the_contract_declares_the_field_as_an_optional_asil_string(self, kind: str) -> None:
        """`string`, the five capitals, no default, no `omitAtDefault`."""
        hardware = next(layer for layer in load_contract()["layers"] if layer["id"] == "layer:hardware")
        fields = next(k for k in hardware["kinds"] if k["id"] == f"kind:hardware.{kind}")["fields"]
        declared = next(field for field in fields if field["name"] == "asil_capability")

        assert declared["id"] == f"field:hardware.{kind}.asil_capability"
        assert declared["type"] == "string"
        assert declared["enum"] == list(ASIL_LEVELS)
        assert declared["required"] is False
        assert declared["default"] is None
        assert "omitAtDefault" not in declared
        # Placed after `enforcement`, the reader order the guide and the editor follow.
        names = [field["name"] for field in fields]
        assert names.index("asil_capability") == names.index("enforcement") + 1

    def test_no_processor_carries_a_capability(self) -> None:
        """Nothing places anything on a processor, so a value there is checkable by nothing."""
        hardware = next(layer for layer in load_contract()["layers"] if layer["id"] == "layer:hardware")
        processor = next(k for k in hardware["kinds"] if k["id"] == "kind:hardware.processor")

        assert "asil_capability" not in {field["name"] for field in processor["fields"]}
        assert "asil_capability" not in Processor.model_fields

    @pytest.mark.parametrize("cls", [Core, Memory])
    def test_the_schema_defaults_to_unrated_and_refuses_a_sixth_value(self, cls: type[Any]) -> None:
        base: dict[str, Any] = {"name": "X", "threshold": 1.0}
        if cls is Core:
            base["kind"] = "core"
        else:
            base.update(kind="memory", type="RAM", size=1, accessible=["CORE0"])

        assert cls.model_validate(base).asil_capability is None
        assert cls.model_validate({**base, "asil_capability": "QM"}).asil_capability == "QM"
        with pytest.raises(ValidationError):
            cls.model_validate({**base, "asil_capability": "E"})


class TestTheComposition:
    def test_the_rules_follow_the_coexistence_rules_in_the_aggregate(self, model_root: Path) -> None:
        """Appended LAST, after `check_safety_coexistence`, and in the check's own order.

        Asserted on a root where a coexistence rule and four of these fire, so a
        reordered tuple turns this red where a fixture-order assertion alone
        might not: `Lo` (`QM`) below the `D` partition with no argument, and
        `Hi`'s `RAM0` budget below the partition's level.
        """
        _core(model_root, "CORE0", "B")
        _partition(model_root, "P0", "D")
        _memory(model_root, "RAM0", "B")
        _swc(model_root, "Hi", asil="D", runnables=[_runnable("Run")], memory={"RAM0": 64})
        _swc(model_root, "Lo", asil="QM", runnables=[_runnable("Run")], memory={"RAM0": 0})
        _task(model_root, "T", [_ref("Hi"), _ref("Lo")])

        safety = [
            record.rule
            for record in check_cross_layer_consistency(str(model_root)).records
            if record.rule.startswith("safety.")
        ]

        assert safety == ["safety.task-mixed-asil", PARTITION, MEMORY, BELOW, PARTITION_MEMORY]


# ---------------------------------------------------------------------------
# 4. The acceptance fixture
# ---------------------------------------------------------------------------


class TestTheAcceptanceFixtureCarriesEveryFieldOfThePhase:
    def test_the_check_mints_the_nine_placement_findings_the_readme_lists(self) -> None:
        """One partition per task, each `D` on the `B` core: three partition findings.

        The memory rule adds `TorqueLimiter`'s, and the per-type rules five:
        `P_Mixed`'s two lower components (it declares no argument; `P_Argued` does,
        so its derived-`B` `SpeedSensor` is silent), and `RAM0` (`B`) once per `D`
        partition whose components budget it non-zero.
        """
        records = check_hardware_capability(str(SAFETY_MODEL_ROOT)).records

        assert [(record.rule, record.subjects[0].element, record.subjects[1].element) for record in records] == [
            (PARTITION, "P_Argued", "CORE0"),
            (PARTITION, "P_Homogeneous", "CORE0"),
            (PARTITION, "P_Mixed", "CORE0"),
            (MEMORY, "TorqueLimiter", "TorqueLimiter"),
            (BELOW, "P_Mixed", "P_Mixed"),
            (BELOW, "P_Mixed", "P_Mixed"),
            (PARTITION_MEMORY, "RAM0", "P_Argued"),
            (PARTITION_MEMORY, "RAM0", "P_Homogeneous"),
            (PARTITION_MEMORY, "RAM0", "P_Mixed"),
        ]
        assert [record.subjects[2].element for record in records if record.rule == BELOW] == [
            "DiagnosticsAgent",
            "PlainWorker",
        ]

    def test_every_partition_is_rated_d_and_each_task_names_its_own(self) -> None:
        partitions = {
            element.name: element for element in read_hardware(str(SAFETY_MODEL_ROOT)) if isinstance(element, Partition)
        }
        assert {name: (p.asil, p.core) for name, p in partitions.items()} == {
            "P_Argued": ("D", "CORE0"),
            "P_Homogeneous": ("D", "CORE0"),
            "P_Mixed": ("D", "CORE0"),
        }
        assert partitions["P_Argued"].coexistence
        assert {task.name: task.partition for task in read_tasks(str(SAFETY_MODEL_ROOT))} == {
            "Task_Argued": "P_Argued",
            "Task_Homogeneous": "P_Homogeneous",
            "Task_Mixed": "P_Mixed",
        }

    def test_the_zero_budget_the_equal_level_and_the_lower_level_are_all_silent(self) -> None:
        """A zero budget, an equal level and a lower level are each silent.

        `SpeedSensor` derives `B` and budgets `0`; `PlainWorker` is `B` on `B`;
        `DiagnosticsAgent` derives `QM`.
        """
        swcs = {swc.name: swc for swc in read_swc_model(str(SAFETY_MODEL_ROOT))}

        assert swcs["SpeedSensor"].memory == {"RAM0": 0}
        assert swcs["PlainWorker"].asil == "B"
        assert swcs["PlainWorker"].memory == {"RAM0": 256}
        assert swcs["DiagnosticsAgent"].memory == {"RAM0": 128}
        reported = {record.subjects[0].element for record in _records(SAFETY_MODEL_ROOT, MEMORY)}
        assert reported == {"TorqueLimiter"}

    def test_the_budgets_keep_every_hardware_rule_silent(self) -> None:
        """A budget on every component, `RAM0` reachable from `CORE0`, 896 under a limit of 2048."""
        hardware = {element.name: element for element in read_hardware(str(SAFETY_MODEL_ROOT))}
        ram = hardware["RAM0"]
        assert isinstance(ram, Memory)
        assert ram.accessible == ["CORE0"]
        assert ram.size * ram.threshold == 2048
        assert sum(swc.memory["RAM0"] for swc in read_swc_model(str(SAFETY_MODEL_ROOT))) == 896
        assert isinstance(hardware["CORE0"], Core) and hardware["CORE0"].asil_capability == "B"
        assert not isinstance(hardware["CPU"], Core | Memory)

        rules = {record.rule for record in check_cross_layer_consistency(str(SAFETY_MODEL_ROOT)).records}
        assert not {rule for rule in rules if rule.startswith("hardware.")}

    def test_every_hardware_and_component_file_round_trips_through_the_writer(self, tmp_path: Path) -> None:
        """What `write_element` does with the fixture, measured rather than assumed.

        The hand-written files are **not** byte-identical to the writer's output: a
        long `description` is wrapped, a non-ASCII character is escaped, the default
        `enforcement: strict` is written out and `asil` takes its declaration
        position. What holds, and is asserted: every hardware and component file
        reads back equal after one write, a second write is byte-identical to the
        first, and `asil_capability` is written for exactly the two rated elements.
        """
        root = tmp_path / "model"
        shutil.copytree(SAFETY_MODEL_ROOT, root)
        hardware = read_hardware(str(root))
        swcs = read_swc_model(str(root))
        targets = {element.name: f"hardware/{element.name}.yaml" for element in hardware}
        targets |= {swc.name: f"swc/safety/{swc.name}.yaml" for swc in swcs}
        assert all((root / target).is_file() for target in targets.values())

        for element in (*hardware, *swcs):
            write_element(str(root), targets[element.name], element)
        first = {target: (root / target).read_bytes() for target in targets.values()}

        assert read_hardware(str(root)) == hardware
        assert read_swc_model(str(root)) == swcs
        for element in (*read_hardware(str(root)), *read_swc_model(str(root))):
            write_element(str(root), targets[element.name], element)
        assert {target: (root / target).read_bytes() for target in targets.values()} == first
        declaring = sorted(target for target, data in first.items() if b"asil_capability" in data)
        assert declaring == ["hardware/CORE0.yaml", "hardware/RAM0.yaml"]


# ---------------------------------------------------------------------------
# An `instance` entry is read as its resolved type
# ---------------------------------------------------------------------------


def _as_instances(model_root: Path, path_of: dict[str, str]) -> None:
    """Rewrite every task entry ``swc: X`` of *model_root* to ``instance: path_of[X]``."""
    for task_file in sorted((model_root / "tasks").glob("*.yaml"), key=lambda path: path.as_posix()):
        document = yaml.safe_load(task_file.read_text(encoding="utf-8"))
        document["runnables"] = [
            {"instance": path_of[entry["swc"]], "runnable": entry["runnable"]} for entry in document["runnables"]
        ]
        task_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _places(model_root: Path) -> Counter:
    """Every finding by rule and by the elements it names, member identities left out.

    A member step names the entry by its own triple, which is the one thing that
    differs between the two spellings, so the comparison is over what a finding
    is ABOUT: its rule and the elements and fields it addresses.
    """
    records = check_cross_layer_consistency(str(model_root)).records
    return Counter(
        (
            record.rule,
            tuple(
                (subject.layer, subject.element, tuple(step.value for step in subject.path if step.kind == "field"))
                for subject in record.subjects
            ),
        )
        for record in records
    )


class TestAnInstanceEntryRaisesWhatTheSwcEntryRaises:
    """The safety readers and the topology pass read the TYPE an entry resolves to."""

    def test_the_safety_fixture_written_with_instances_raises_every_finding_it_raised(self, tmp_path: Path) -> None:
        import shutil

        swc_root, instance_root = tmp_path / "swc" / "model", tmp_path / "instance" / "model"
        shutil.copytree(SAFETY_MODEL_ROOT, swc_root)
        shutil.copytree(SAFETY_MODEL_ROOT, instance_root)
        # Every component of the fixture is a root member named after its type.
        members = {member.type: member.name for member in read_compositions(str(instance_root))[0].members}
        _as_instances(instance_root, members)

        expected = _places(swc_root)
        assert {rule for rule, _places_ in expected} >= {
            "safety.task-mixed-asil",
            "safety.component-below-partition",
            "safety.partition-memory-below-level",
            "task.instance-in-multiple-partitions",
        }
        assert _places(instance_root) == expected

    def test_the_editing_reading_is_the_same_over_either_spelling(self, tmp_path: Path) -> None:
        instance_root = tmp_path / "model"
        shutil.copytree(SAFETY_MODEL_ROOT, instance_root)
        members = {member.type: member.name for member in read_compositions(str(instance_root))[0].members}
        _as_instances(instance_root, members)

        before, after = _safety_reading(SAFETY_MODEL_ROOT), _safety_reading(instance_root)
        assert after.mixed_tasks == before.mixed_tasks != frozenset()
        assert after.mixed_partitions == before.mixed_partitions != frozenset()

    @pytest.mark.parametrize("selector", ["swc", "instance"])
    def test_a_component_above_its_partition_and_an_unreachable_memory_are_raised_through_either(
        self, tmp_path: Path, selector: str
    ) -> None:
        from conftest import _core, _memory, _partition, _root_composition, _swc, _task

        _core(tmp_path, "CORE0", partition=False)
        _core(tmp_path, "CORE1", partition=False)
        _partition(tmp_path, "P_B", core="CORE0", asil="B")
        _memory(tmp_path, "RAM1", accessible=["CORE1"])
        _swc(tmp_path, "Hot", asil="D", runnables=[{"name": "Run"}], memory={"RAM1": 16})
        _root_composition(tmp_path, [{"name": "hot", "type": "Hot"}])
        entry = {"swc": "Hot"} if selector == "swc" else {"instance": "hot"}
        _task(tmp_path, "T", partition="P_B", runnables=[{**entry, "runnable": "Run"}])

        rules = {rule for rule, _places_ in _places(tmp_path)}
        assert {"safety.component-above-partition", "hardware.unreachable-memory"} <= rules
