"""A project holding one of every newer element kind.

The tiny project with an external interface stub, two units, a linear and a texttable compu
method, a data constraint, two application types and a mapping set added: every reference names a
declared element, in the shapes SWCraft's schema admits.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from swcraft_specimens.model import (
    ApplicationType,
    DataConstr,
    ExternalInterface,
    LinearCompuMethod,
    MappingSet,
    Partition,
    Project,
    Requirement,
    RequirementLink,
    TaskSequence,
    TextTableCompuMethod,
    Unit,
    build_tiny_project,
)
from swcraft_specimens.preset import ASIL, load_preset
from swcraft_specimens.specimen.hardware import PARTITION_COEXISTENCE
from swcraft_specimens.specimen.tasks import TASK_COEXISTENCE

ROOT = Path(__file__).parents[2]


def project_with_new_kinds() -> Project:
    """Return the tiny project with one of every new element kind."""
    tiny = build_tiny_project(load_preset(ROOT / "tests/fixtures/tiny.yaml"))
    stub = ExternalInterface("IfStub", "/External/Interfaces/IfStub", "CLIENT-SERVER-INTERFACE")
    return replace(
        tiny,
        interfaces=(*tiny.interfaces, stub),
        units=(Unit("Un1"), Unit("Un2", display_name="km/h")),
        compu_methods=(
            LinearCompuMethod("Cm1", unit="Un1", factor=1, offset=0),
            TextTableCompuMethod("Cm2", entries=(("E0", 0), ("E1", 1))),
        ),
        data_constraints=(DataConstr("Dc1", min=0, max=100),),
        application_types=(
            ApplicationType("At1", unit_ref="Un1", compu_method_ref="Cm1", data_constr_ref="Dc1"),
            ApplicationType("At2"),
        ),
        mapping_sets=(MappingSet("Ms1", maps=(("At1", "u8"), ("At2", "u8"))),),
    )


#: The later kinds' names.
C0, C1 = "Dom01_Swc001", "Dom01_Swc002"
TASK0, TASK1 = "Task_CORE0_B", "Task_CORE1_B"
PARTITION0, PARTITION1 = "Pa1", "Pa2"


def project_with_later_kinds() -> Project:
    """Return `project_with_new_kinds` with one of every later kind.

    Two explicit partitions, the first rated with a `coexistence`; the two tasks naming them, the
    first with a `coexistence`; a sequence over the second task's two runnables; requirement
    `Rq1` at version 2 linked once on each of the seven site kinds, its port link stale (`@1`);
    `Rq2` without `asil`, linked on one port; `Rq3` linked nowhere.
    """
    base = project_with_new_kinds()
    core0, core1 = (core.name for core in base.cores)
    task0, task1 = base.tasks
    return replace(
        base,
        partitions=(
            Partition(PARTITION0, core0, ASIL.B, coexistence=PARTITION_COEXISTENCE),
            Partition(PARTITION1, core1),
        ),
        tasks=(
            replace(task0, partition=PARTITION0, coexistence=TASK_COEXISTENCE),
            replace(task1, partition=PARTITION1),
        ),
        sequences=(TaskSequence("Sq1", TASK1, ((C1, "read"), (C1, "call"))),),
        requirements=(
            Requirement(
                "Rq1",
                "Supply voltage monitoring",
                "The provider shall monitor the supply voltage.",
                ASIL.ABSENT,
                version=2,
                links=(
                    RequirementLink("swc", C0, None, 2),
                    RequirementLink("port", C0, "provide_data", 1),
                    RequirementLink("runnable", C1, "read", 2),
                    RequirementLink("interface", "IfData", None, 2),
                    RequirementLink("composition", "Root", None, 2),
                    RequirementLink("task", TASK0, None, 2),
                    RequirementLink("hardware", "CPU", None, 2),
                ),
            ),
            Requirement(
                "Rq2",
                "Specimen requirement Rq2",
                "A specimen requirement.",
                ASIL.ABSENT,
                links=(RequirementLink("port", C1, "require_data", 1),),
            ),
            Requirement("Rq3", "Specimen requirement Rq3", "A specimen requirement.", ASIL.B, ()),
        ),
    )
