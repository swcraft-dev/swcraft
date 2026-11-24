"""The mixed-event fixture, read through a throwaway copy.

``packages/tools/tests/fixtures/operation_events/model`` is one component whose one
runnable carries a timing event and an operation event. It is committed without
a ``compositions/`` layer, so that the byte-identity record (which recognises a
tree by that layer and whose premise refuses an operation event) never takes it
in — the fixture's README says why. :func:`mixed_event_model` copies it and adds
the one root composition, so ``check-model`` over the copy reports nothing.

:func:`every_event_shape_model` grows the same copy into the writer's positive
fixture: every event kind, several operations on one runnable, ``concurrent``,
an ``OIT_`` name at the 128-character limit and two pairs deriving one base.

What this does **not** reach: it builds two shapes. Every negative case a test
needs is a change to a copy, written in that test.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml
from external_stubs_support import change, put

#: The committed tree. Never written to: every consumer works on a copy.
MIXED_EVENT_MODEL_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "operation_events" / "model"

#: The component, its runnable, its port, its interface and its operation, by name.
SERVER_FILE = "swc/server/ServerSwc.yaml"
SERVER = "ServerSwc"
RUNNABLE = "ServeAndTick"
PORT = "pRecords"
INTERFACE_FILE = "interfaces/IRecords.yaml"
INTERFACE = "IRecords"
OPERATION = "ReadRecord"

#: The two events the runnable carries, as the file spells them.
TIMING_EVENT: dict[str, Any] = {"type": "timing", "period": 0.01}
OPERATION_EVENT: dict[str, Any] = {"type": "operation", "port": PORT, "operation": OPERATION}

#: The root composition the copy gains: the component, once.
ROOT_COMPOSITION: dict[str, Any] = {
    "name": "Root",
    "title": "Root composition",
    "root": True,
    "members": [{"name": SERVER, "type": SERVER}],
}


def load(model_root: Path, relative: str) -> dict[str, Any]:
    """Read one element file under *model_root*."""
    return yaml.safe_load((model_root / relative).read_text(encoding="utf-8"))


def mixed_event_model(destination: Path) -> Path:
    """Copy the mixed-event fixture to *destination*, add its root composition and return *destination*."""
    shutil.copytree(MIXED_EVENT_MODEL_ROOT, destination)
    put(destination, "compositions/Root.yaml", ROOT_COMPOSITION)
    return destination


#: The boundary pair of :func:`every_event_shape_model`: a port name and an
#: operation name whose ``OIT_{port}_{operation}`` is exactly 128 characters —
#: ``OIT_`` and the separating ``_`` leave the two 123 between them.
BOUNDARY_PORT = "pBoundary" + "P" * (61 - len("pBoundary"))
BOUNDARY_OPERATION = "OpBoundary" + "O" * (62 - len("OpBoundary"))

#: The two distinct pairs of :func:`every_event_shape_model` that derive one
#: ``OIT_`` base, ``OIT_p_A_B``: ``(p_A, B)`` and ``(p, A_B)``.
COLLIDING_PAIRS: tuple[tuple[str, str], tuple[str, str]] = (("p_A", "B"), ("p", "A_B"))


def _no_argument_interface(model_root: Path, name: str, operation: str) -> None:
    put(
        model_root,
        f"interfaces/{name}.yaml",
        {"name": name, "kind": "client_server", "operations": [{"name": operation, "arguments": []}]},
    )


def every_event_shape_model(destination: Path) -> Path:
    """The mixed-event copy, grown to carry every event shape the writer emits.

    Built rather than committed for the reason the module docstring gives; over the
    copy ``check-model`` reports nothing. Beside the mixed runnable
    ``ServeAndTick`` (a timing and an operation event) it declares:

    * ``Serve`` — ``concurrent: true``, started by **two** operations of
      ``IRecords`` (``ReadOther``, ``ReadThird``) whose signatures equal
      ``ReadRecord``'s;
    * ``Initialize`` — an init event;
    * ``CollideA`` / ``CollideB`` — the two :data:`COLLIDING_PAIRS`, one each,
      whose one base the writer's retroactive counter suffixes ``_0`` / ``_1``;
    * ``Boundary`` — :data:`BOUNDARY_PORT` / :data:`BOUNDARY_OPERATION`, whose
      ``OIT_`` name is exactly 128 characters.

    What it does **not** reach: it is one model, not every model; its XSD pass
    is schema evidence only, and the mixed runnable in it is never labelled
    RTE-conformant.

    Returns:
        *destination*, the model root of the copy.
    """
    mixed_event_model(destination)
    signature = load(destination, INTERFACE_FILE)["operations"][0]["arguments"]

    def grow_interface(document: dict[str, Any]) -> None:
        for name in ("ReadOther", "ReadThird"):
            document["operations"].append({"name": name, "arguments": signature})

    change(destination, INTERFACE_FILE, grow_interface)
    _no_argument_interface(destination, "IPairA", COLLIDING_PAIRS[0][1])
    _no_argument_interface(destination, "IPairB", COLLIDING_PAIRS[1][1])
    _no_argument_interface(destination, "IBoundary", BOUNDARY_OPERATION)

    def grow_component(document: dict[str, Any]) -> None:
        document["ports"].extend(
            [
                {"name": COLLIDING_PAIRS[0][0], "direction": "provide", "interface": "IPairA"},
                {"name": COLLIDING_PAIRS[1][0], "direction": "provide", "interface": "IPairB"},
                {"name": BOUNDARY_PORT, "direction": "provide", "interface": "IBoundary"},
            ]
        )
        document["runnables"].extend(
            [
                {
                    "name": "Serve",
                    "concurrent": True,
                    "events": [
                        {"type": "operation", "port": PORT, "operation": "ReadOther"},
                        {"type": "operation", "port": PORT, "operation": "ReadThird"},
                    ],
                },
                {"name": "Initialize", "events": [{"type": "init"}]},
                {"name": "CollideA", "events": [{"type": "operation", "port": "p_A", "operation": "B"}]},
                {"name": "CollideB", "events": [{"type": "operation", "port": "p", "operation": "A_B"}]},
                {
                    "name": "Boundary",
                    "events": [{"type": "operation", "port": BOUNDARY_PORT, "operation": BOUNDARY_OPERATION}],
                },
            ]
        )

    change(destination, SERVER_FILE, grow_component)
    return destination
