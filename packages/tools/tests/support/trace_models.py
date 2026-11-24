"""Synthetic models for the trace link search: one builder, no hand-kept files.

Every fixture is a tiny model root written by Python into a test's ``tmp_path``,
every write naming ``encoding=`` and ``newline=``, as ``test_platform_io.py`` requires of
every text write in a test helper. Every model is synthetic: no element, file, document
or requirement of a real project, generic names only.

:class:`TraceModel` collects element documents and writes them; :func:`write_project`
wraps a project around it. Apply's tests share :class:`ApplyProject`, :func:`tree`
and :func:`failing_replace`, so no test module imports another.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import model_element_storage_name
from pydantic import TypeAdapter
from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import load_model_state, scan_layer
from swcraft_tools.project import SwcraftProject, require_project
from swcraft_tools.trace import LINKABLE_LABELS
from swcraft_tools.trace_apply import ApplyPlan, plan_apply
from swcraft_tools.trace_search import (
    PROPOSALS_FORMAT_VERSION,
    TargetAddress,
    TraceConstraints,
    TraceSearch,
    document_of,
    existing_links,
    proposals_json,
    requirement_fingerprint,
    target_fingerprint,
)
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.yaml_load import load_yaml_document

#: The implementation type every data element of a fixture carries.
DATA_TYPE = "uint8"


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")


def port(
    name: str,
    *,
    direction: str = "provide",
    interface: str | None = None,
    description: str = "",
    satisfies: Iterable[str] = (),
) -> dict[str, Any]:
    """A port document; ``interface=None`` writes a port with no interface."""
    document: dict[str, Any] = {"name": name, "direction": direction}
    if interface is not None:
        document["interface"] = interface
    if description:
        document["description"] = description
    if satisfies:
        document["satisfies"] = list(satisfies)
    return document


def runnable(
    name: str,
    *,
    description: str = "",
    reads: Iterable[tuple[str, str]] = (),
    writes: Iterable[tuple[str, str]] = (),
    calls: Iterable[tuple[str, str]] = (),
    satisfies: Iterable[str] = (),
) -> dict[str, Any]:
    """A runnable document; *reads* and *writes* are ``(port, element)``, *calls* ``(port, operation)``."""
    document: dict[str, Any] = {"name": name}
    if description:
        document["description"] = description
    if reads:
        document["reads"] = [{"port": p, "element": e} for p, e in reads]
    if writes:
        document["writes"] = [{"port": p, "element": e} for p, e in writes]
    if calls:
        document["calls"] = [{"port": p, "operation": o} for p, o in calls]
    if satisfies:
        document["satisfies"] = list(satisfies)
    return document


class TraceModel:
    """A model root under construction: element documents keyed by their path below the root."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        # The one data type every data element is typed with, so an interface loads and draws no finding.
        self.put(
            model_element_storage_name("uint8_base", kind="base_type"),
            {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
        )
        self.put(
            model_element_storage_name(DATA_TYPE, kind="value_type"),
            {"name": DATA_TYPE, "kind": "value_type", "base": "uint8_base"},
        )

    # -- the files ---------------------------------------------------------

    def put(self, relative: str, document: dict[str, Any]) -> None:
        self.files[relative] = document

    def get(self, relative: str) -> dict[str, Any]:
        return self.files[relative]

    # -- requirements ------------------------------------------------------

    def requirement(
        self,
        identifier: str,
        title: str,
        description: str = "",
        *,
        version: int = 1,
        document: str | None = None,
        stub: bool = False,
    ) -> None:
        payload: dict[str, Any] = {
            "id": identifier,
            "title": title,
            "description": description,
            "version": version,
            "source": "external",
        }
        if document is not None:
            payload["document"] = document
        if stub:
            payload["stub"] = True
        self.put(model_element_storage_name(identifier, layer="requirements"), payload)

    # -- components --------------------------------------------------------

    def component(
        self,
        name: str,
        *,
        description: str = "",
        ports: Sequence[dict[str, Any]] = (),
        runnables: Sequence[dict[str, Any]] = (),
        satisfies: Iterable[str] = (),
        external: bool = False,
        group: str = "app",
        asil: str | None = None,
    ) -> None:
        document: dict[str, Any] = {"name": name}
        if description:
            document["description"] = description
        if asil is not None:
            document["asil"] = asil
        if external:
            document["external"] = True
            document["external_ref"] = f"/Outside/{name}"
            document["external_dest"] = "APPLICATION-SW-COMPONENT-TYPE"
        if ports:
            document["ports"] = list(ports)
        if runnables:
            document["runnables"] = list(runnables)
        if satisfies:
            document["satisfies"] = list(satisfies)
        self.put(f"swc/{group}/{name}.yaml", document)

    def link(self, relative: str, *links: str) -> None:
        """Append *links* to the ``satisfies`` of the element stored at *relative*."""
        self.get(relative).setdefault("satisfies", []).extend(links)

    # -- interfaces --------------------------------------------------------

    def sr_interface(
        self,
        name: str,
        elements: Sequence[tuple[str, str]],
        *,
        description: str = "",
        satisfies: Iterable[str] = (),
    ) -> None:
        """A sender-receiver interface; *elements* are ``(name, description)``."""
        document: dict[str, Any] = {
            "name": name,
            "kind": "sender_receiver",
            "data_elements": [
                {"name": element, "implementation_type": DATA_TYPE, **({"description": text} if text else {})}
                for element, text in elements
            ],
        }
        if description:
            document["description"] = description
        if satisfies:
            document["satisfies"] = list(satisfies)
        self.put(f"interfaces/{name}.yaml", document)

    def cs_interface(
        self,
        name: str,
        operations: Sequence[tuple[str, str]],
        *,
        description: str = "",
        satisfies: Iterable[str] = (),
    ) -> None:
        """A client-server interface; *operations* are ``(name, description)``."""
        document: dict[str, Any] = {
            "name": name,
            "kind": "client_server",
            "operations": [{"name": op, **({"description": text} if text else {})} for op, text in operations],
        }
        if description:
            document["description"] = description
        if satisfies:
            document["satisfies"] = list(satisfies)
        self.put(f"interfaces/{name}.yaml", document)

    def external_interface(
        self, name: str, *, data_elements: Sequence[str] = (), operations: Sequence[str] = ()
    ) -> None:
        document: dict[str, Any] = {"name": name, "kind": "external_interface", "external_ref": f"/Outside/{name}"}
        if data_elements:
            document["external_dest"] = "SENDER-RECEIVER-INTERFACE"
            document["data_elements"] = list(data_elements)
        if operations:
            document["external_dest"] = "CLIENT-SERVER-INTERFACE"
            document["operations"] = list(operations)
        self.put(f"interfaces/{name}.yaml", document)

    # -- compositions, tasks, hardware -------------------------------------

    def composition(self, name: str, *, description: str = "", satisfies: Iterable[str] = ()) -> None:
        document: dict[str, Any] = {"name": name, "title": name}
        if description:
            document["description"] = description
        if satisfies:
            document["satisfies"] = list(satisfies)
        self.put(f"compositions/{name}.yaml", document)

    def task(self, name: str, *, partition: str, description: str = "", satisfies: Iterable[str] = ()) -> None:
        document: dict[str, Any] = {"name": name, "period": 0.01, "priority": 10, "partition": partition}
        if description:
            document["description"] = description
        if satisfies:
            document["satisfies"] = list(satisfies)
        self.put(f"tasks/{name}.yaml", document)

    def hardware(
        self, name: str, kind: str, *, description: str = "", satisfies: Iterable[str] = (), **keys: Any
    ) -> None:
        """A hardware element of *kind*; the keys its kind requires default to a minimal valid value."""
        required: dict[str, dict[str, Any]] = {
            "processor": {},
            "core": {"threshold": 0.8},
            "partition": {"core": "Core0"},
            "memory": {"type": "RAM", "size": 1024, "accessible": ["Core0"], "threshold": 0.8},
        }
        document: dict[str, Any] = {"name": name, "kind": kind, **required[kind], **keys}
        if description:
            document["description"] = description
        if satisfies:
            document["satisfies"] = list(satisfies)
        self.put(f"hardware/{name}.yaml", document)

    # -- writing -----------------------------------------------------------

    def write(self, model_root: Path) -> Path:
        for relative, document in self.files.items():
            _write(model_root / relative, document)
        model_root.mkdir(parents=True, exist_ok=True)
        return model_root


def config_document(**keys: Any) -> dict[str, Any]:
    """A configuration document from flat *keys*, each filed under the section that declares it."""
    document: dict[str, Any] = {}
    for key, value in keys.items():
        if key in TraceSearch.model_fields:
            document.setdefault("search", {})[key] = value
        elif key in TraceConstraints.model_fields:
            document.setdefault("constraints", {})[key] = value
        else:
            document[key] = value
    return document


def write_project(model: TraceModel, project_dir: Path, **experiment: Any) -> tuple[Path, Path | None]:
    """*model* as a project under *project_dir*: ``(manifest, configuration file or None)``.

    The configuration file ``experiments/trace.yaml`` is written only when
    *experiment* names a key, each filed into its section by :func:`config_document`.
    """
    model.write(project_dir / "model")
    manifest = project_dir / "swcraft.yaml"
    _write(
        manifest,
        {
            "name": "trace-project",
            "schema_version": "1.0",
            "model_root": "model",
            "dist_root": "dist",
            "views_root": "views",
            "outputs": {"arxml": "dist/model.arxml", "needs": "dist/needs.json"},
        },
    )
    if not experiment:
        return manifest, None
    document = config_document(**experiment)
    experiment_file = project_dir / "experiments" / f"{document.get('name', 'trace')}.yaml"
    _write(experiment_file, document)
    return manifest, experiment_file


def showcase_shaped() -> TraceModel:
    """A synthetic model in the showcase's shape, generic names only.

    Three documents of different sizes and a group with no document; links on
    components, ports, runnables, an interface, a task and a partition; one link
    awaiting confirmation (``BRK-6@1`` against version 2); one dangling member
    (``GONE-1@1``); and one external component stub, which is never a target and
    holds the only link of ``SYS-5``.
    """
    model = TraceModel()
    model.sr_interface("IWheelSpeed", [("speed", "the wheel speed in km/h")], description="wheel speed signal")
    model.sr_interface("IBrakeCmd", [("pressure", "the brake pressure command")], description="brake command")
    model.sr_interface("ILampCmd", [("state", "the lamp state")], description="lamp command")
    model.cs_interface("IDiag", [("readFault", "read a stored fault"), ("clearFault", "clear a stored fault")])
    model.external_interface("IExtBus", data_elements=["frame"])
    model.component(
        "BrakeController",
        description="computes the brake pressure from the wheel speed",
        ports=[
            port("pWheelSpeed", direction="require", interface="IWheelSpeed"),
            port("pBrakeCmd", interface="IBrakeCmd", satisfies=["BRK-1@1"]),
        ],
        runnables=[
            runnable(
                "ComputeBrake",
                description="brake pressure computation",
                reads=[("pWheelSpeed", "speed")],
                writes=[("pBrakeCmd", "pressure")],
                satisfies=["BRK-2@1"],
            ),
            runnable("MonitorPads", description="brake pad wear monitoring"),
        ],
        satisfies=["BRK-6@1"],
    )
    model.component(
        "WheelSensor",
        description="samples the wheel speed sensors",
        ports=[port("pSpeedOut", interface="IWheelSpeed")],
        runnables=[runnable("SampleSpeed", description="wheel speed sampling")],
        satisfies=["BRK-3@1"],
    )
    model.component(
        "LampController",
        description="drives the exterior lamps",
        ports=[port("pLampCmd", interface="ILampCmd")],
        runnables=[runnable("DriveLamps", description="lamp driver")],
        satisfies=["BODY-1@1"],
    )
    model.component(
        "DoorController",
        description="locks and unlocks the doors",
        runnables=[runnable("LockDoors", description="door lock actuation")],
        satisfies=["GONE-1@1"],
    )
    model.component(
        "DiagManager",
        description="stores and reports diagnostic faults",
        ports=[port("pDiag", interface="IDiag")],
        runnables=[runnable("HandleFault", description="fault handling")],
        satisfies=["SYS-4@1"],
    )
    model.component(
        "GatewayStub",
        external=True,
        ports=[port("pBus", interface="IExtBus")],
        satisfies=["SYS-5@1"],
    )
    model.link("interfaces/IWheelSpeed.yaml", "BRK-4@1")
    model.composition("VehicleTop", description="the vehicle software composition", satisfies=["SYS-1@1"])
    model.hardware("Mcu", "processor", description="the main microcontroller")
    model.hardware("Core0", "core", description="the application core")
    model.hardware("AppPartition", "partition", description="the application partition")
    model.hardware("SafePartition", "partition", description="the safety partition", satisfies=["SYS-3@1"])
    model.hardware("Ram", "memory", description="the main memory")
    model.task("Task10ms", partition="AppPartition", description="the fast cyclic task", satisfies=["SYS-2@1"])
    model.task("Task100ms", partition="AppPartition", description="the slow cyclic task")

    system = "System Spec"
    for identifier, title, text in (
        ("SYS-1", "Vehicle composition", "The vehicle software composition shall group the components."),
        ("SYS-2", "Fast task", "The fast cyclic task shall run every ten milliseconds."),
        ("SYS-3", "Safety partition", "Safety software shall run in the safety partition."),
        ("SYS-4", "Diagnostics", "Diagnostic faults shall be stored and reported."),
        ("SYS-5", "Gateway", "The gateway shall forward bus frames."),
        ("SYS-6", "Core load", "Core0 load shall stay below its threshold."),
        ("SYS-7", "Memory budget", "Ram usage shall be bounded."),
        ("SYS-8", "Microcontroller", "The Mcu shall host the application partition."),
    ):
        model.requirement(identifier, title, text, document=system)
    for identifier, title, text in (
        ("BRK-1", "Brake command", "The brake pressure command shall be sent."),
        ("BRK-2", "Brake computation", "The brake pressure shall be computed from the wheel speed."),
        ("BRK-3", "Wheel speed sampling", "The wheel speed sensors shall be sampled."),
        ("BRK-4", "Wheel speed signal", "The wheel speed signal shall carry km/h."),
        ("BRK-5", "Pad wear", "Brake pad wear shall be monitored."),
    ):
        model.requirement(identifier, title, text, document="Brake Spec")
    model.requirement("BRK-6", "Brake release", "The brake shall release on request.", version=2, document="Brake Spec")
    for identifier, title, text in (
        ("BODY-1", "Exterior lamps", "The exterior lamps shall be driven."),
        ("BODY-2", "Door lock", "The doors shall lock on request."),
        ("BODY-3", "Lamp state", "The lamp state shall be reported."),
    ):
        model.requirement(identifier, title, text, document="  Body Spec ")
    model.requirement("N-1", "Fault clearing", "A stored fault shall be cleared on request.")
    model.requirement("N-2", "Unrelated", "Nothing in the model answers this.")
    return model


#: A small vocabulary the scale model's texts are drawn from, deterministically.
_SCALE_WORDS = (
    "brake wheel speed pressure door lock lamp light fault diagnostic sensor actuator torque motor "
    "battery charge voltage current temperature cooling fan window seat mirror horn wiper steering "
    "angle yaw rate gateway frame signal command state request release monitor limit threshold"
).split()


def scale_model(*, requirements: int = 6000, components: int = 150, documents: int = 20) -> TraceModel:
    """The benchmark's synthetic model, deterministic.

    *components* components of 10 ports and 3 runnables each, and *requirements* requirements over *documents*
    documents, a third of them linked."""
    model = TraceModel()
    words = _SCALE_WORDS
    for index in range(components):
        a, b, c = words[index % len(words)], words[(index * 7 + 3) % len(words)], words[(index * 13 + 5) % len(words)]
        interface = f"IScale{index}"
        model.sr_interface(interface, [(f"{a}Value", f"the {a} {b} value")])
        model.component(
            f"Unit{index}",
            description=f"{a} {b} {c} unit",
            ports=[
                port(f"p{a.capitalize()}{b.capitalize()}{slot}", interface=interface, description=f"{b} {c} port")
                for slot in range(10)
            ],
            runnables=[runnable(f"Run{c.capitalize()}{slot}", description=f"{c} {a} step") for slot in range(3)],
        )
    for index in range(requirements):
        a, b, c = words[index % len(words)], words[(index * 5 + 1) % len(words)], words[(index * 11 + 2) % len(words)]
        identifier = f"REQ-{index:05d}"
        model.requirement(
            identifier,
            f"{a.capitalize()} {b}",
            f"The {a} {b} shall control the {c}.",
            document=f"Spec {index % documents}",
        )
        if index % 3 == 0:
            model.link(f"swc/app/Unit{index % components}.yaml", f"{identifier}@1")
    return model


# ---------------------------------------------------------------------------
# Proposals files
# ---------------------------------------------------------------------------

#: The model digest a built proposals file records: any well-formed one, since Apply only reports a difference.
PROPOSALS_MODEL_DIGEST = "sha256:" + "0" * 64

#: A row's score and evidence: well-formed, and read by nothing Apply decides.
_ROW_SCORE = {"n": 500000, "d": 1000000}


def proposals_for(
    model_root: Path,
    rows: Mapping[str, Sequence[tuple[TargetAddress, str | None]]],
    *,
    chosen: Mapping[str, Sequence[TargetAddress]] | None = None,
    name: str = "trace",
) -> dict[str, Any]:
    """A valid ``proposals.json`` document for the model at *model_root*, built rather than hand-copied.

    *rows* maps each requirement id, in the entries' order, to its rows as
    ``(target, decision)``; *chosen* to the targets of its ``chosen`` entries.
    Every fingerprint is the search's own over the model as it stands, and the
    ``linked`` entries are the search's (``decision: null`` on each one awaiting
    confirmation), so a test edits the one slot it is about.
    """
    state = load_model_state(model_root)
    requirements = {requirement.id: requirement for requirement in state["requirements"]}
    links = existing_links(state)
    entries = []
    for identifier, entry_rows in rows.items():
        requirement = requirements[identifier]
        candidates = []
        for place, (address, decision) in enumerate(entry_rows, start=1):
            fingerprint = target_fingerprint(state, address)
            assert fingerprint is not None, f"{address} names no target of the model"
            group = address.group
            candidates.append(
                {
                    "place": place,
                    "group": {**group.to_json(), "score": dict(_ROW_SCORE), "evidence": []},
                    "target": address.to_json(),
                    "fingerprint": fingerprint,
                    "band": "medium",
                    "score": dict(_ROW_SCORE),
                    "evidence": [],
                    "decision": decision,
                }
            )
        entries.append(
            {
                "id": identifier,
                "title": requirement.title,
                "description": requirement.description,
                "version": requirement.version,
                "fingerprint": requirement_fingerprint(requirement),
                "document": document_of(requirement),
                "pass": "review",
                "chosenBecause": None,
                "lowAccuracy": False,
                "linked": [
                    link.to_json() if link.confirmed else {**link.to_json(), "decision": None}
                    for link in links.get(identifier, ())
                ],
                "candidates": candidates,
                "chosen": [{"target": address.to_json()} for address in (chosen or {}).get(identifier, ())],
            }
        )
    return {
        "formatVersion": PROPOSALS_FORMAT_VERSION,
        "kind": "trace",
        "name": name,
        "model": {"digest": PROPOSALS_MODEL_DIGEST, "files": 0, "swcraftVersion": "0.0.0"},
        "configuration": None,
        "requirements": entries,
    }


def write_proposals(path: Path, document: Mapping[str, Any]) -> Path:
    """*document* written at *path* in the search's own form; returns *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(proposals_json(document), encoding="utf-8", newline="\n")
    return path


# ---------------------------------------------------------------------------
# The working cycle
# ---------------------------------------------------------------------------

#: The cycle's two documents.
CYCLE_DOCUMENTS = ("Plant Spec", "Platform Spec")

#: The cycle's requirements as the interchange file brings them: ``(id, title, content, document)``.
CYCLE_REQUIREMENTS: tuple[tuple[str, str, str, str], ...] = (
    ("PL-1", "Pump flow", "The Pump shall deliver the coolant flow.", "Plant Spec"),
    ("PL-2", "Pump reset", "The pump service shall reset a stored fault on request.", "Plant Spec"),
    ("PL-3", "Valve limit", "The Valve shall limit the coolant flow to 600 l/h.", "Plant Spec"),
    ("PL-4", "Plant grouping", "The Plant composition shall group the coolant units.", "Plant Spec"),
    ("PL-5", "Flow signal", "The flow signal shall carry the coolant flow in l/h.", "Plant Spec"),
    ("PF-1", "Valve input", "The Valve shall read the coolant flow input.", "Platform Spec"),
    ("PF-2", "Cyclic task", "The cyclic task shall run every 10 ms.", "Platform Spec"),
    ("PF-3", "Memory budget", "The Ram usage shall stay bounded.", "Platform Spec"),
    ("PF-4", "Core load", "The Core0 load shall stay below its threshold.", "Platform Spec"),
    ("PF-5", "Controller", "The Mcu shall host the application partition.", "Platform Spec"),
)


def cycle_need(identifier: str, **fields: Any) -> dict[str, Any]:
    """The interchange need of the cycle's requirement *identifier*, *fields* replacing its keys."""
    (title, content, document) = next(row[1:] for row in CYCLE_REQUIREMENTS if row[0] == identifier)
    return {"id": identifier, "title": title, "content": content, "type": "req", "document": document, **fields}


def cycle_needs(*needs: dict[str, Any], baseline: str = "Baseline-1") -> dict[str, Any]:
    """An interchange document whose one version, *baseline*, holds *needs* and is current."""
    return {"current_version": baseline, "versions": {baseline: {"needs": {need["id"]: need for need in needs}}}}


def cycle_model() -> TraceModel:
    """The working cycle's model: every kind of target, and a link on each document to start from.

    No requirement is in it: the cycle starts with the requirements import of
    :data:`CYCLE_REQUIREMENTS`, which writes them at version 1. ``PL-1`` (on
    ``Pump``), ``PF-1`` (on port ``pFlowIn`` of ``Valve``) and ``PF-2`` (on
    ``Task10ms``) are linked, so each document has a neighbour. Generic names only.
    """
    model = TraceModel()
    model.sr_interface("IFlow", [("flow", "the coolant flow in l/h")], description="the coolant flow signal")
    model.cs_interface("IService", [("reset", "reset a stored fault")], description="the pump service")
    model.component(
        "Pump",
        description="delivers the coolant flow",
        ports=[
            port("pFlow", interface="IFlow", description="the delivered coolant flow"),
            port("pService", interface="IService", description="the pump service"),
        ],
        runnables=[runnable("Run_Pump", description="pump drive step", writes=[("pFlow", "flow")])],
        satisfies=["PL-1@1"],
    )
    model.component(
        "Valve",
        description="limits the coolant flow",
        ports=[
            port(
                "pFlowIn",
                direction="require",
                interface="IFlow",
                description="the coolant flow input",
                satisfies=["PF-1@1"],
            )
        ],
        runnables=[runnable("Run_Valve", description="valve limit step", reads=[("pFlowIn", "flow")])],
    )
    for name in ("Pump", "Valve"):  # what the ARXML writer requires of a component it writes
        model.get(f"swc/app/{name}.yaml")["handle_termination_and_restart"] = "no_support"
    model.composition("Plant", description="the plant composition of the coolant units")
    model.hardware("Mcu", "processor", description="the controller")
    model.hardware("Core0", "core", description="the application core")
    model.hardware("AppPart", "partition", description="the application partition")
    model.hardware("Ram", "memory", description="the main memory")
    model.task("Task10ms", partition="AppPart", description="the cyclic task", satisfies=["PF-2@1"])
    return model


# ---------------------------------------------------------------------------
# Apply's projects: one model, its proposals file and the plan
# ---------------------------------------------------------------------------

CTL = TargetAddress("swc", None, "Ctl")
OUT = TargetAddress("port", "Ctl", "pOut")
STEP = TargetAddress("runnable", "Ctl", "Step")
TOP = TargetAddress("composition", None, "Top")


def apply_model() -> TraceModel:
    """SR-1 at version 2, linked to ``Ctl`` at version 1 (awaiting confirmation); SR-2 and SR-3 unlinked."""
    model = TraceModel()
    model.requirement("SR-1", "Brake temperature", "Supervise the brake temperature", version=2)
    model.requirement("SR-2", "Door lock", "Doors shall lock")
    model.requirement("SR-3", "Lamp state", "The lamp state shall be reported")
    model.component(
        "Ctl",
        description="brake control",
        satisfies=["SR-1@1"],
        ports=[port("pOut")],
        runnables=[runnable("Step")],
    )
    model.composition("Top", description="the top composition")
    return model


def _serialize(element: Any) -> str:
    """The text :func:`~swcraft_tools.writer.elements.write_element` writes for *element*."""
    return yaml.safe_dump(element_to_dict(element), default_flow_style=False, sort_keys=False)


def writer_form(model_root: Path) -> None:
    """Rewrite every linkable file in the writer's own form, so a test sees no *writer's form* entry by accident."""
    registry = current_registry()
    for layer in LINKABLE_LABELS:  # the linkable layers, named by the trace module, never listed here
        adapter = TypeAdapter(registry.layer(layer).element_annotation)
        for entry in scan_layer(model_root, layer, registry=registry).files:
            element = adapter.validate_python(load_yaml_document(entry.path.read_text(encoding="utf-8")))
            entry.path.write_text(_serialize(element), encoding="utf-8", newline="\n")


class ApplyProject:
    """A synthetic project, its proposals file and the plan over them."""

    def __init__(self, tmp_path: Path, model: TraceModel, *, in_writer_form: bool = True) -> None:
        self.dir = tmp_path / "project"
        self.manifest, _ = write_project(model, self.dir)
        self.root = self.dir / "model"
        if in_writer_form:
            writer_form(self.root)
        self.proposals = self.dir / "dist" / "experiments" / "trace" / "proposals.json"

    @property
    def project(self) -> SwcraftProject:
        return require_project(str(self.manifest))

    def propose(self, rows: dict[str, list[tuple[TargetAddress, str | None]]], **keys: Any) -> dict[str, Any]:
        return proposals_for(self.root, rows, **keys)

    def write(self, document: dict[str, Any]) -> Path:
        return write_proposals(self.proposals, document)

    def plan(self, document: dict[str, Any] | None = None) -> ApplyPlan:
        if document is not None:
            self.write(document)
        return plan_apply(self.project, self.proposals)

    def edit(self, relative: str, change: Any) -> None:
        """Change the element file at *relative* below the model root through *change(document)*."""
        path = self.root / relative
        document = load_yaml_document(path.read_text(encoding="utf-8"))
        change(document)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def confirming(project: ApplyProject, rows: dict[str, list[tuple[Any, str | None]]]) -> dict[str, Any]:
    """*rows* for SR-2 and SR-3, with SR-1's awaiting link (``Ctl``, ``SR-1@1``) decided ``confirm``."""
    document = project.propose({"SR-1": [], **rows})
    document["requirements"][0]["linked"][0]["decision"] = "confirm"
    return document


def tree(directory: Path) -> dict[str, bytes]:
    """Every file below *directory*, by its relative POSIX name: the bytes a refusal must leave alone."""
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def failing_replace(monkeypatch: pytest.MonkeyPatch, failing: int) -> list[Any]:
    """Make the *failing*-th ``os.replace`` raise; return the targets it was called with."""
    real_replace = os.replace
    calls: list[Any] = []

    def replace(src: Any, dst: Any) -> None:
        calls.append(dst)
        if len(calls) == failing:
            raise OSError("injected rename failure")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    return calls
