"""The inter-runnable variable fixture, read through a throwaway copy.

``packages/tools/tests/fixtures/inter_runnable_variables/model`` is one component whose
runnables share three variables. It is committed without a ``compositions/``
layer, so that the byte-identity record (which recognises a tree by that layer)
never takes it in -- the fixture's README says why. :func:`variable_model`
copies it and adds the one root composition, so ``check-model`` over the copy
reports nothing at all, no warning included.

What this does **not** reach: it builds one shape. Every negative case a test
needs is a change to a copy, written in that test.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from external_stubs_support import change, put

#: The committed tree. Never written to: every consumer works on a copy.
VARIABLE_MODEL_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "inter_runnable_variables" / "model"

#: The component, by file and by name.
COMPONENT_FILE = "swc/chassis/SpeedEstimator.yaml"
COMPONENT = "SpeedEstimator"

#: The three variables: implicit and application-typed with an init, explicit and
#: implementation-typed with an init, and explicit, implementation-typed and untouched.
FILTERED = "FilteredSpeed"
PEAK = "PeakSpeed"
SPARE = "SpareCounter"

#: The three runnables: the 10 ms writer, the 50 ms reader whose access states its
#: name, and the 100 ms reader that also reads and writes ``PeakSpeed``.
WRITER = "FilterSpeed"
LOGGER = "LogSpeed"
MONITOR = "MonitorSpeed"

#: The root composition the copy gains: the component, once.
ROOT_COMPOSITION: dict[str, Any] = {
    "name": "Root",
    "title": "Root composition",
    "root": True,
    "members": [{"name": COMPONENT, "type": COMPONENT}],
}


def variable_model(destination: Path) -> Path:
    """A copy of the variable fixture at *destination*, with its root composition added.

    Returns:
        *destination*, the model root of the copy.
    """
    shutil.copytree(VARIABLE_MODEL_ROOT, destination)
    put(destination, "compositions/Root.yaml", ROOT_COMPOSITION)
    return destination


#: The every-shape copy's stated placement and behaviour name (the writer's path test and the XSD case).
PACKAGE = "Chassis"
BEHAVIOUR = "SpeedBehaviour"

#: The every-shape copy's three added variables, one per composite value form.
FRAME = "SpeedFrameCache"
HISTORY = "SpeedHistory"
GEAR = "CurrentGear"


def every_variable_shape_model(destination: Path) -> Path:
    """:func:`variable_model` plus every value form and access neighbour the writer places a variable among.

    The copy gains:

    * three variables, one per composite or literal ``init`` form: an explicit
      implementation record with a record value, an implicit implementation array
      with an array value, and an implicit application enumeration (mapped onto
      ``uint8`` by ``SpeedMapping``) with a literal's name;
    * a sender-receiver require port read by ``MonitorSpeed`` (a
      ``DATA-RECEIVE-POINT-BY-ARGUMENTS``) and a client-server require port it
      calls (a ``SERVER-CALL-POINTS``), so its ``READ-LOCAL-VARIABLES`` has a
      neighbour on each side;
    * a stated ``package`` and a stated ``behavior_name``, which every
      ``LOCAL-VARIABLE-REF`` path follows.

    ``check-model`` over it reports nothing. Returns *destination*.
    """
    root = variable_model(destination)
    put(
        root,
        "data_types/implementation/FrameRecord.yaml",
        {
            "name": "FrameRecord",
            "kind": "implementation_record",
            "elements": [{"name": "count", "type": "uint8"}, {"name": "peak", "type": "uint16"}],
        },
    )
    put(
        root,
        "data_types/implementation/HistoryArray.yaml",
        {
            "name": "HistoryArray",
            "kind": "implementation_array",
            "element_type": "uint8",
            "element_name": "sample",
            "length": 2,
        },
    )
    put(
        root,
        "data_types/application/Gear.yaml",
        {
            "name": "Gear",
            "kind": "enum",
            "literals": [{"name": "P", "value": 0}, {"name": "D", "value": 1}],
        },
    )
    change(
        root,
        "data_types/mapping_sets/SpeedMapping.yaml",
        lambda document: document["maps"].append({"application": "Gear", "implementation": "uint8"}),
    )
    put(
        root,
        "interfaces/ISpeed.yaml",
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [{"name": "value", "implementation_type": "uint16"}],
        },
    )
    put(
        root,
        "interfaces/IReset.yaml",
        {
            "name": "IReset",
            "kind": "client_server",
            "operations": [{"name": "Reset", "arguments": []}],
        },
    )

    def mutate(document: dict[str, Any]) -> None:
        document["package"] = PACKAGE
        document["behavior_name"] = BEHAVIOUR
        document["ports"] = [
            {"name": "rSpeed", "direction": "require", "interface": "ISpeed"},
            {"name": "rReset", "direction": "require", "interface": "IReset"},
        ]
        document["variables"] += [
            {
                "name": FRAME,
                "access": "explicit",
                "implementation_type": "FrameRecord",
                "init": {"count": 1, "peak": 2},
            },
            {"name": HISTORY, "access": "implicit", "implementation_type": "HistoryArray", "init": [3, 4]},
            {"name": GEAR, "access": "implicit", "application_type": "Gear", "init": "D"},
        ]
        (monitor,) = [runnable for runnable in document["runnables"] if runnable["name"] == MONITOR]
        monitor["reads"] = [{"port": "rSpeed", "element": "value"}]
        monitor["calls"] = [{"port": "rReset", "operation": "Reset"}]
        monitor["reads_variables"] += [{"variable": FRAME}, {"variable": HISTORY}, {"variable": GEAR}]

    change(root, COMPONENT_FILE, mutate)
    return root
