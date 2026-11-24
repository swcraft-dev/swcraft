"""The data-received event fixture, read through a throwaway copy.

``packages/tools/tests/fixtures/data_events/model`` is one receiving component whose
runnables are started when a value arrives, mapped to two tasks of one
partition. It is committed without a ``compositions/`` layer, so that the
byte-identity record (which recognises a tree by that layer) never takes it in
-- the fixture's README says why. :func:`data_event_model` copies it and adds
the one root composition, so ``check-model`` over the copy reports nothing at
all, no warning included.

What this does **not** reach: it builds one shape. Every negative case a test
needs is a change to a copy, written in that test.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from external_stubs_support import change, put
from swcraft_tools.reader import read_swc_model, read_tasks
from swcraft_tools.schema import DataEvent

#: The committed tree. Never written to: every consumer works on a copy.
DATA_EVENT_MODEL_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "data_events" / "model"

#: The component, by file and by name.
COMPONENT_FILE = "swc/chassis/SpeedEstimator.yaml"
COMPONENT = "SpeedEstimator"

#: The three require ports: the sender/receiver one carrying both front wheel
#: speeds, the one on a sender/receiver stub listing ``RearLeft``, and the one
#: on a mode-switch stub the disabled mode arrives through.
WHEEL_PORT = "R_WheelSpeed"
REAR_PORT = "R_RearSpeed"
MODE_PORT = "R_EcuMode"

#: The interfaces, by file and by name.
WHEEL_INTERFACE_FILE = "interfaces/WheelSpeedIf.yaml"
WHEEL_INTERFACE = "WheelSpeedIf"
REAR_INTERFACE = "RearSpeedIf"
MODE_INTERFACE = "EcuModeIf"

#: The data elements: ``FrontLeft`` (standard), ``FrontRight`` (queued) on the
#: declared interface, ``RearLeft`` listed by the stub.
FRONT_LEFT = "FrontLeft"
FRONT_RIGHT = "FrontRight"
REAR_LEFT = "RearLeft"

#: The three runnables: the filter started by ``FrontLeft`` and reading it; the
#: monitor started by a 100 ms timing event **and** by ``FrontRight`` (the
#: standard's own example), the latter disabled in ``Sleep``; the rear tracker
#: started through the stub port.
FILTER = "FilterSpeed"
MONITOR = "MonitorSpeed"
TRACK_REAR = "TrackRearSpeed"

#: The disabled mode the monitor's data-received event carries.
SLEEP_MODE: dict[str, str] = {
    "port": MODE_PORT,
    "mode_group_ref": "/Foreign/ModeInterfaces/EcuModeIf/EcuMode",
    "mode_ref": "/Foreign/ModeDeclarationGroups/EcuMode/Sleep",
}

#: The root composition the copy gains: the component, once.
ROOT_COMPOSITION: dict[str, Any] = {
    "name": "Root",
    "title": "Root composition",
    "root": True,
    "members": [{"name": COMPONENT, "type": COMPONENT}],
}


def data_event(port: str, element: str, **extra: Any) -> dict[str, Any]:
    """One data-received event as a model file states it."""
    return {"type": "data", "port": port, "element": element, **extra}


def tasks_of_runnables_started_by(model: Path, element: str) -> dict[str, list[str]]:
    """Each runnable of the fixture's component a data-received event on *element* starts, with the tasks mapping it.

    Read through the readers, so a test whose name says *in two tasks* shows it
    rather than leaning on the fixture's ``tasks/`` in silence.
    """
    (component,) = read_swc_model(str(model))
    started = [
        runnable.name
        for runnable in component.runnables
        if any(isinstance(event, DataEvent) and event.element == element for event in runnable.events)
    ]
    tasks = read_tasks(str(model))
    return {name: [task.name for task in tasks if any(e.runnable == name for e in task.runnables)] for name in started}


def data_event_model(destination: Path) -> Path:
    """Copy the data-received event fixture to *destination*, add its root composition and return *destination*."""
    shutil.copytree(DATA_EVENT_MODEL_ROOT, destination)
    put(destination, "compositions/Root.yaml", ROOT_COMPOSITION)
    return destination


def stub_free_data_event_model(destination: Path) -> Path:
    """:func:`data_event_model` without its two stubs and everything that names them.

    A component export refuses a closure that reaches an external stub, so the
    component exchange's subject drops the rear port, the mode port, their two stubs,
    the runnable started through the rear port (and its task entry) and the disabled
    mode. What is left: ``FilterSpeed`` started by ``FrontLeft`` and ``MonitorSpeed`` by
    a timing event and ``FrontRight``, both through the declared ``WheelSpeedIf``.

    Returns:
        *destination*, the model root of the copy.
    """
    data_event_model(destination)
    for name in (REAR_INTERFACE, MODE_INTERFACE):
        (destination / "interfaces" / f"{name}.yaml").unlink()

    def component(document: dict[str, Any]) -> None:
        document["ports"] = [port for port in document["ports"] if port["name"] == WHEEL_PORT]
        document["runnables"] = [runnable for runnable in document["runnables"] if runnable["name"] != TRACK_REAR]
        for runnable in document["runnables"]:
            for event in runnable["events"]:
                event.pop("disabled_modes", None)

    change(destination, COMPONENT_FILE, component)

    def task(document: dict[str, Any]) -> None:
        document["runnables"] = [entry for entry in document["runnables"] if entry["runnable"] != TRACK_REAR]

    change(destination, "tasks/Task_10ms.yaml", task)
    return destination
