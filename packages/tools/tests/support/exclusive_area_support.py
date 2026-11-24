"""The exclusive area fixture, read through a throwaway copy.

``packages/tools/tests/fixtures/exclusive_areas/model`` is one component whose runnables
use three exclusive areas in both roles, mapped to three tasks of one partition.
It is committed without a ``compositions/`` layer, so that the byte-identity
record (which recognises a tree by that layer) never takes it in -- the
fixture's README says why. :func:`area_model` copies it and adds the one root
composition, so ``check-model`` over the copy reports nothing at all, no warning
included.

:func:`every_area_shape_model` is the second shape, the writer's: the
every-variable-shape model with areas and both reference lists added, so an area
and its references sit among every neighbour the writer places them beside.

What this does **not** reach: it builds two shapes. Every negative case a test
needs is a change to a copy, written in that test.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from external_stubs_support import change, put
from inter_runnable_variable_support import MONITOR as VARIABLE_MONITOR
from inter_runnable_variable_support import WRITER as VARIABLE_WRITER
from inter_runnable_variable_support import every_variable_shape_model

#: The committed tree. Never written to: every consumer works on a copy.
AREA_MODEL_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "exclusive_areas" / "model"

#: The component, by file and by name.
COMPONENT_FILE = "swc/chassis/SpeedEstimator.yaml"
COMPONENT = "SpeedEstimator"

#: The three areas: one two runnables run inside, with a description; one a third
#: runnable can enter; and one nobody uses.
SPEED_STATE = "SpeedState"
OFFSET_TABLE = "OffsetTable"
SPARE_AREA = "SpareArea"

#: The three runnables: the 10 ms filter and the 100 ms monitor, both running
#: inside ``SpeedState`` on tasks of different priority, and the 1 s calibration,
#: which can enter ``OffsetTable``.
FILTER = "FilterSpeed"
MONITOR = "MonitorSpeed"
CALIBRATE = "CalibrateOffsets"

#: The root composition the copy gains: the component, once.
ROOT_COMPOSITION: dict[str, Any] = {
    "name": "Root",
    "title": "Root composition",
    "root": True,
    "members": [{"name": COMPONENT, "type": COMPONENT}],
}


def area_model(destination: Path) -> Path:
    """Copy the exclusive area fixture to *destination*, add its root composition and return *destination*."""
    shutil.copytree(AREA_MODEL_ROOT, destination)
    put(destination, "compositions/Root.yaml", ROOT_COMPOSITION)
    return destination


#: The description :func:`every_area_shape_model` gives the runnable that runs
#: inside an area, so the runnable's ``DESC`` precedes its first area reference.
SHAPE_RUNNABLE_DESCRIPTION = "Filters the speed inside the speed state area."


def every_area_shape_model(destination: Path) -> Path:
    """The every-variable-shape model (``every_variable_shape_model``) with exclusive areas, at *destination*.

    The copy's ``SpeedEstimator`` (stated ``package`` and ``behavior_name``, a
    mapping set, port reads, a call point, variables of both roles and their
    accesses) gains:

    * two areas, ``SpeedState`` with a description and ``OffsetTable`` without;
    * on ``FilterSpeed``, a description and ``runs_inside: [SpeedState]``;
    * on ``MonitorSpeed`` (the runnable with the port read, the call point and
      both variable lists), ``can_enter: [OffsetTable, SpeedState]`` and
      ``runs_inside: [SpeedState]`` -- two entries in one list, in declared order
      against the alphabet, and one area in both roles, which is silence.

    ``check-model`` over it reports nothing. Returns *destination*.
    """
    root = every_variable_shape_model(destination)

    def mutate(document: dict[str, Any]) -> None:
        document["exclusive_areas"] = [
            {"name": SPEED_STATE, "description": "Guards the filtered speed."},
            {"name": OFFSET_TABLE},
        ]
        runnables = {runnable["name"]: runnable for runnable in document["runnables"]}
        runnables[VARIABLE_WRITER]["description"] = SHAPE_RUNNABLE_DESCRIPTION
        runnables[VARIABLE_WRITER]["runs_inside"] = [{"area": SPEED_STATE}]
        runnables[VARIABLE_MONITOR]["can_enter"] = [{"area": OFFSET_TABLE}, {"area": SPEED_STATE}]
        runnables[VARIABLE_MONITOR]["runs_inside"] = [{"area": SPEED_STATE}]

    change(root, COMPONENT_FILE, mutate)
    return root
