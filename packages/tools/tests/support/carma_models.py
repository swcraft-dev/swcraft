"""Synthetic models for the allocation experiment (CARMA): one builder, no hand-kept files.

Every fixture is a tiny model **root** written by Python into a test's ``tmp_path``,
every write naming ``encoding=`` and ``newline=``, as ``test_platform_io.py`` requires of
every text write in a test helper. Every model is synthetic -- no element, file or column
of a real project.

:class:`CarmaModel` collects element documents and writes them; :func:`three_cores`
is the base the projection suite's fixtures edit before they write.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import yaml
from conftest import model_element_storage_name
from swcraft_tools.allocation import AllocationConstraints, AllocationSearch

#: A period in seconds -> the priority the builder gives a task of it: shorter
#: period, higher priority (the resource analysis' rate-monotonic check).
_PRIORITY_BY_PERIOD = {0.005: 40, 0.01: 30, 0.02: 20, 0.05: 10, 0.1: 5, 0.5: 3, 1.0: 1}


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")


def runnable(
    name: str,
    *,
    period: float | None = 0.01,
    wcet: str | None = "100us",
    wcet_type: str = "gross",
    writes: Iterable[tuple[str, str]] = (),
    calls: Iterable[tuple[str, str]] = (),
    events: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One runnable document: a timing event of *period* (none when ``None``), a budget, its writes and calls."""
    document: dict[str, Any] = {"name": name}
    if events is not None:
        document["events"] = list(events)
    elif period is not None:
        document["events"] = [{"type": "timing", "period": period}]
    if writes:
        document["writes"] = [{"port": port, "element": element} for port, element in writes]
    if calls:
        document["calls"] = [{"port": port, "operation": operation} for port, operation in calls]
    if wcet is not None:
        document["wcet"] = wcet
        document["wcet_type"] = wcet_type
    return document


def init_runnable(name: str, **keys: Any) -> dict[str, Any]:
    """An initialization runnable: one ``init`` event."""
    return runnable(name, events=[{"type": "init"}], **keys)


#: The client-server interface, its operation and the provide port a :func:`server_runnable` serves.
SERVER_INTERFACE = "IOp"
SERVER_OPERATION = "Do"
SERVER_PORT = "sOp"


def server_runnable(name: str, *, init: bool = False, **keys: Any) -> dict[str, Any]:
    """A server runnable of ``SERVER_PORT.SERVER_OPERATION``; with *init*, an ``init`` event beside the operation event.

    Its component needs the port: :meth:`CarmaModel.provide_server_port`.
    """
    events: list[dict[str, Any]] = [{"type": "init"}] if init else []
    events.append({"type": "operation", "port": SERVER_PORT, "operation": SERVER_OPERATION})
    return runnable(name, events=events, **keys)


#: The sender-receiver interface, its element and the require port a :func:`data_runnable`'s event names.
DATA_INTERFACE = "IData"
DATA_ELEMENT = "value"
DATA_PORT = "rData"


def data_runnable(name: str, *, also: Sequence[dict[str, Any]] = (), **keys: Any) -> dict[str, Any]:
    """A runnable started by a data-received event on ``DATA_PORT.DATA_ELEMENT``, after the events in *also*.

    Its component needs the port: :meth:`CarmaModel.require_data_port`.
    """
    events = [*also, {"type": "data", "port": DATA_PORT, "element": DATA_ELEMENT}]
    return runnable(name, events=events, **keys)


#: The require port a :func:`call_returned_runnable`'s event and an :func:`asynchronous_call` name: the client
#: side of ``SERVER_INTERFACE.SERVER_OPERATION``.
CALL_PORT = "rOp"


def call_returned_runnable(name: str, *, also: Sequence[dict[str, Any]] = (), **keys: Any) -> dict[str, Any]:
    """A runnable started by a ``call_returned`` event on ``CALL_PORT.SERVER_OPERATION``, after the events in *also*.

    Its component needs the port (:meth:`CarmaModel.require_call_port`) and a runnable that calls the pair
    asynchronously (an :func:`asynchronous_call` in some runnable's ``calls``).
    """
    events = [*also, {"type": "call_returned", "port": CALL_PORT, "operation": SERVER_OPERATION}]
    return runnable(name, events=events, **keys)


def asynchronous_call() -> dict[str, Any]:
    """The asynchronous call of ``CALL_PORT.SERVER_OPERATION`` a ``call_returned`` event answers."""
    return {"port": CALL_PORT, "operation": SERVER_OPERATION, "asynchronous": True}


#: The link every component of a synthetic model carries to its one requirement.
CARMA_LINK = "REQ_CARMA_001@1"


class CarmaModel:
    """A model root under construction: element documents keyed by their path below the root."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.root_members: dict[str, dict[str, Any]] = {}
        self.base_type("uint8_base", 8)
        self.base_type("uint32_base", 32)
        self.value_type("uint8", "uint8_base")
        self.value_type("uint32", "uint32_base")
        self.put("hardware/CPU.yaml", {"name": "CPU", "kind": "processor"})

    # -- the files ---------------------------------------------------------

    def put(self, relative: str, document: dict[str, Any]) -> None:
        self.files[relative] = document

    def get(self, relative: str) -> dict[str, Any]:
        return self.files[relative]

    def drop(self, relative: str) -> None:
        del self.files[relative]

    # -- hardware ----------------------------------------------------------

    def core(
        self, name: str, *, threshold: float = 0.8, enforcement: str = "strict", asil_capability: str | None = None
    ) -> None:
        document: dict[str, Any] = {"name": name, "kind": "core", "threshold": threshold, "enforcement": enforcement}
        if asil_capability is not None:
            document["asil_capability"] = asil_capability
        self.put(f"hardware/{name}.yaml", document)

    def partition(self, name: str, core: str, *, asil: str | None = None, coexistence: str = "") -> None:
        document: dict[str, Any] = {"name": name, "kind": "partition", "core": core}
        if asil is not None:
            document["asil"] = asil
        if coexistence:
            document["coexistence"] = coexistence
        self.put(f"hardware/{name}.yaml", document)

    def memory(
        self,
        name: str,
        *,
        accessible: Sequence[str],
        size: int = 1_000_000,
        enforcement: str = "advisory",
        asil_capability: str | None = None,
    ) -> None:
        document: dict[str, Any] = {
            "name": name,
            "kind": "memory",
            "type": "RAM",
            "size": size,
            "accessible": list(accessible),
            "threshold": 0.8,
            "enforcement": enforcement,
        }
        if asil_capability is not None:
            document["asil_capability"] = asil_capability
        self.put(f"hardware/{name}.yaml", document)

    # -- components, tasks, compositions -----------------------------------

    def component(
        self,
        name: str,
        *,
        runnables: Sequence[dict[str, Any]],
        asil: str | None = None,
        ports: Sequence[dict[str, Any]] = (),
        memory: dict[str, int] | None = None,
        mappings: Sequence[str] = (),
        placed: int = 1,
    ) -> None:
        """A component of the root composition, placed *placed* times (0: declared but placed nowhere)."""
        document: dict[str, Any] = {"name": name, "type": "application", "handle_termination_and_restart": "no_support"}
        if ports:
            document["ports"] = list(ports)
        if runnables:
            document["runnables"] = list(runnables)
        if memory:
            document["memory"] = dict(memory)
        if asil is not None:
            document["asil"] = asil
        if mappings:
            document["data_type_mappings"] = list(mappings)
        # Every component satisfies the model's one requirement, so it raises no warning.
        document["satisfies"] = [CARMA_LINK]
        self.put(f"swc/carma/{name}.yaml", document)
        for index in range(placed):
            member = name if index == 0 else f"{name}_{index + 1}"
            self.root_members[member] = {"name": member, "type": name}

    def unsatisfied_requirement(self, identifier: str) -> None:
        """A requirement no component satisfies: ``trace.requirement-unallocated``'s warning, and no other finding."""
        self.put(
            f"requirements/{identifier}.yaml",
            {
                "id": identifier,
                "title": "Unsatisfied",
                "description": "A requirement no component of the synthetic model satisfies.",
                "version": 1,
                "source": "architecture",
            },
        )

    def task(
        self,
        name: str,
        partition: str,
        period: float,
        *,
        runnables: Sequence[tuple[str, str]],
        wcet: str | None = "1000us",
        coexistence: str = "",
        priority: int | None = None,
    ) -> None:
        document: dict[str, Any] = {
            "name": name,
            "period": period,
            "priority": _PRIORITY_BY_PERIOD[period] if priority is None else priority,
            "partition": partition,
        }
        if wcet is not None:
            document["wcet"] = wcet
        if coexistence:
            document["coexistence"] = coexistence
        document["runnables"] = [{"swc": swc, "runnable": runnable_name} for swc, runnable_name in runnables]
        self.put(f"tasks/{name}.yaml", document)

    # -- interfaces and connectors -----------------------------------------

    def sr_interface(self, name: str, elements: dict[str, dict[str, str]]) -> None:
        """A sender-receiver interface; *elements* maps each element to its ``{implementation_type: ...}`` typing."""
        self.put(
            f"interfaces/{name}.yaml",
            {
                "name": name,
                "kind": "sender_receiver",
                "data_elements": [{"name": element, **typing} for element, typing in elements.items()],
            },
        )

    def cs_interface(self, name: str, operations: Sequence[str]) -> None:
        self.put(
            f"interfaces/{name}.yaml",
            {"name": name, "kind": "client_server", "operations": [{"name": op} for op in operations]},
        )

    def provide_server_port(self, component: str) -> CarmaModel:
        """Declare the interface and give *component* the provide port a :func:`server_runnable` serves."""
        self.cs_interface(SERVER_INTERFACE, [SERVER_OPERATION])
        self.get(f"swc/carma/{component}.yaml").setdefault("ports", []).append(
            {"name": SERVER_PORT, "direction": "provide", "interface": SERVER_INTERFACE}
        )
        return self

    def require_data_port(self, component: str) -> CarmaModel:
        """Declare the interface and give *component* the require port a :func:`data_runnable`'s event names."""
        self.sr_interface(DATA_INTERFACE, {DATA_ELEMENT: {"implementation_type": "uint32"}})
        self.get(f"swc/carma/{component}.yaml").setdefault("ports", []).append(
            {"name": DATA_PORT, "direction": "require", "interface": DATA_INTERFACE}
        )
        return self

    def require_call_port(self, component: str) -> CarmaModel:
        """Declare the interface and give *component* the require port a :func:`call_returned_runnable` names."""
        self.cs_interface(SERVER_INTERFACE, [SERVER_OPERATION])
        self.get(f"swc/carma/{component}.yaml").setdefault("ports", []).append(
            {"name": CALL_PORT, "direction": "require", "interface": SERVER_INTERFACE}
        )
        return self

    def connector(self, name: str, sender: tuple[str, str], receiver: tuple[str, str]) -> None:
        self.put(
            f"connectors/{name}.yaml",
            {
                "name": name,
                "from": {"swc": sender[0], "port": sender[1]},
                "to": {"swc": receiver[0], "port": receiver[1]},
            },
        )

    # -- data types --------------------------------------------------------

    def base_type(self, name: str, size: int) -> None:
        self.put(
            model_element_storage_name(name, kind="base_type"),
            {"name": name, "kind": "base_type", "size": size, "encoding": "NONE", "native_declaration": f"{name}_t"},
        )

    def value_type(self, name: str, base: str) -> None:
        self.put(
            model_element_storage_name(name, kind="value_type"), {"name": name, "kind": "value_type", "base": base}
        )

    def implementation_type(self, document: dict[str, Any]) -> None:
        self.put(model_element_storage_name(document["name"], kind=document["kind"]), document)

    def application_type(self, document: dict[str, Any]) -> None:
        self.put(model_element_storage_name(document["name"], kind=document["kind"]), document)

    def mapping_set(self, name: str, maps: Sequence[tuple[str, str]]) -> None:
        self.put(
            model_element_storage_name(name, layer="mapping_sets"),
            {"name": name, "maps": [{"application": app, "implementation": impl} for app, impl in maps]},
        )

    # -- writing -----------------------------------------------------------

    def write(self, model_root: Path) -> Path:
        """Write every document, the root composition and the one requirement every component satisfies."""
        for relative, document in self.files.items():
            _write(model_root / relative, document)
        _write(
            model_root / "compositions" / "Root.yaml",
            {"name": "Root", "title": "Root", "root": True, "members": list(self.root_members.values())},
        )
        _write(
            model_root / "requirements" / "REQ_CARMA_001.yaml",
            {
                "id": "REQ_CARMA_001",
                "title": "Allocation",
                "description": "Every component of the synthetic allocation model.",
                "version": 1,
                "source": "architecture",
            },
        )
        return model_root


def config_document(**keys: Any) -> dict[str, Any]:
    """A configuration document from flat *keys*, each filed under the section that declares it.

    A key :class:`AllocationSearch` declares goes under ``search``, one
    :class:`AllocationConstraints` declares under ``constraints``; every other
    key -- ``name``, ``output``, and a key neither section declares -- stays at
    the top level, so a misspelt ``max_move`` is refused naming that key.
    """
    document: dict[str, Any] = {}
    for key, value in keys.items():
        if key in AllocationSearch.model_fields:
            document.setdefault("search", {})[key] = value
        elif key in AllocationConstraints.model_fields:
            document.setdefault("constraints", {})[key] = value
        else:
            document[key] = value
    return document


#: The deadline :func:`write_project` writes unless the caller names its own. The shipped 10 s is
#: wall-clock time, so a loaded host would cut an enumeration short and fail a test with no fault in
#: the code. Deadline tests name theirs and replace the clock (``allocation._monotonic``). What this
#: does not reach: a test that rewrites the configuration file or runs with no file keeps the shipped 10 s.
UNREACHABLE_TIME_LIMIT_S = 3600


def full_run_document(**keys: Any) -> dict[str, Any]:
    """:func:`config_document` for a run that is not cut short: ``relieve`` and the unreachable deadline.

    Either is the caller's to name. :func:`config_document` itself stays free of defaults, because
    ``test_allocation_projection.py`` states the shipped deadline through it.
    """
    return config_document(**{"name": "relieve", "time_limit_s": UNREACHABLE_TIME_LIMIT_S, **keys})


def write_project(model: CarmaModel, project_dir: Path, **experiment: Any) -> tuple[Path, Path]:
    """*model* as a project under *project_dir* and one configuration file: ``(manifest, configuration file)``.

    The manifest is the minimal one (``model``, ``dist``, ``views``); the
    configuration file is ``experiments/<name>.yaml``, holding ``name``
    (``relieve`` unless given), :data:`UNREACHABLE_TIME_LIMIT_S` as
    ``time_limit_s`` unless given, and *experiment*'s keys filed into their
    sections (:func:`full_run_document`).
    """
    model.write(project_dir / "model")
    manifest = project_dir / "swcraft.yaml"
    _write(
        manifest,
        {
            "name": "carma-project",
            "schema_version": "1.0",
            "model_root": "model",
            "dist_root": "dist",
            "views_root": "views",
            "outputs": {"arxml": "dist/model.arxml", "needs": "dist/needs.json"},
        },
    )
    document = full_run_document(**experiment)
    experiment_file = project_dir / "experiments" / f"{document['name']}.yaml"
    _write(experiment_file, document)
    return manifest, experiment_file


def sr_ports(provides: Iterable[str] = (), requires: Iterable[str] = (), interface: str = "ISig") -> list[dict]:
    """Ports of one sender-receiver *interface*: the named provide ports, then the require ports."""
    return [{"name": name, "direction": "provide", "interface": interface} for name in provides] + [
        {"name": name, "direction": "require", "interface": interface} for name in requires
    ]


# ---------------------------------------------------------------------------
# Three cores
# ---------------------------------------------------------------------------

#: The twelve components of :func:`three_cores` by the partition each starts in,
#: each rated like that partition, so the starting model is legal.
THREE_CORES_LAYOUT: dict[str, tuple[str, ...]] = {
    "P1": ("Q1", "Q2", "Q3", "Q4"),
    "P2": ("B1", "B2", "B3", "B4"),
    "P3": ("B5", "B6", "B7", "B8"),
}
#: The level each partition is rated at.
THREE_CORES_LEVEL = {"P1": "QM", "P2": "B", "P3": "B"}
#: The links of the fixture: (sender, receiver), each one ``uint32`` element written every 10 ms
#: (4 bytes x 100 Hz = 400 bytes x Hz).
THREE_CORES_LINKS: tuple[tuple[str, str], ...] = (
    ("B1", "B5"),
    ("B2", "B6"),
    ("Q1", "B7"),
    ("Q2", "Q3"),
    ("B3", "B4"),
)
#: Every runnable's cost and every task's budget.
THREE_CORES_RUNNABLE_WCET = "100us"
THREE_CORES_TASK_WCET = "1000us"


def three_cores() -> CarmaModel:
    """Three cores, three partitions, twelve components, tasks of two periods everywhere.

    ``P1`` rated ``QM`` on ``C1``, ``P2`` rated ``B`` on ``C2``, ``P3`` rated
    ``B`` on ``C3`` with a partition ``coexistence`` and a task ``coexistence``
    on each of its tasks; each core declared able to host ``B``. Each partition
    runs a 10 ms and a 20 ms task with a budget; the first two components of a
    partition run in its 10 ms task, the other two in its 20 ms task.
    """
    model = CarmaModel()
    model.sr_interface("ISig", {"value": {"implementation_type": "uint32"}})
    model.core("C1", asil_capability="B")
    model.core("C2", asil_capability="B")
    model.core("C3", asil_capability="B")
    model.partition("P1", "C1", asil="QM")
    model.partition("P2", "C2", asil="B")
    model.partition("P3", "C3", asil="B", coexistence="Memory partitioning argument (synthetic).")
    links_out: dict[str, list[str]] = {}
    links_in: dict[str, list[str]] = {}
    for index, (sender, receiver) in enumerate(THREE_CORES_LINKS):
        links_out.setdefault(sender, []).append(f"pL{index}")
        links_in.setdefault(receiver, []).append(f"rL{index}")
    for partition, components in THREE_CORES_LAYOUT.items():
        fast, slow = [], []
        for position, name in enumerate(components):
            period = 0.01 if position < 2 else 0.02
            outgoing = links_out.get(name, [])
            model.component(
                name,
                asil=THREE_CORES_LEVEL[partition],
                ports=sr_ports(outgoing, links_in.get(name, [])),
                runnables=[
                    runnable(
                        f"{name}_Run",
                        period=period,
                        wcet=THREE_CORES_RUNNABLE_WCET,
                        writes=[(port, "value") for port in outgoing],
                    )
                ],
            )
            (fast if position < 2 else slow).append((name, f"{name}_Run"))
        coexistence = "Execution-time argument (synthetic)." if partition == "P3" else ""
        model.task(
            f"T_{partition}_10ms", partition, 0.01, runnables=fast, wcet=THREE_CORES_TASK_WCET, coexistence=coexistence
        )
        model.task(
            f"T_{partition}_20ms", partition, 0.02, runnables=slow, wcet=THREE_CORES_TASK_WCET, coexistence=coexistence
        )
    for index, (sender, receiver) in enumerate(THREE_CORES_LINKS):
        model.connector(f"L{index}", (sender, f"pL{index}"), (receiver, f"rL{index}"))
    return model


def with_unmapped_server(**keys: Any) -> CarmaModel:
    """:func:`three_cores` with ``Q1`` (P1) calling ``B1`` (P2) over ``LOp``; ``B1_Srv`` runs in no task.

    *keys* go to :func:`server_runnable` (a ``wcet`` among them).
    """
    model = three_cores().provide_server_port("B1")
    model.get("swc/carma/Q1.yaml")["ports"].append(
        {"name": "cOp", "direction": "require", "interface": SERVER_INTERFACE}
    )
    model.get("swc/carma/Q1.yaml")["runnables"][0]["calls"] = [{"port": "cOp", "operation": SERVER_OPERATION}]
    model.get("swc/carma/B1.yaml")["runnables"].append(server_runnable("B1_Srv", **keys))
    model.connector("LOp", ("B1", SERVER_PORT), ("Q1", "cOp"))
    return model


# ---------------------------------------------------------------------------
# The search's fixtures
# ---------------------------------------------------------------------------


def zero_rule() -> CarmaModel:
    """Two partitions ``PA`` and ``PB`` sharing the ``strict`` core ``C1`` (threshold 0.815), ``PC`` on ``C2``.

    ``X`` (100 us, alone in ``TA``, whose budget is 100) sends a 4-byte element
    every 10 ms to ``Y`` in ``PB`` (400 bytes x Hz); ``Z`` (500 us in ``TZ``,
    budget 1000) sends a 1-byte one to ``W`` in ``PC`` (100 bytes x Hz). ``C1``
    carries 0.01 + 0.70 + 0.10 = 0.81. Moving ``X`` alone into ``PB`` empties
    ``TA``, whose budget the zero rule keeps: the copy's ``C1`` is 0.82, a
    breach, though the solver counted the move as load-neutral. Moving ``Z`` to
    ``PC`` as well frees 0.05 on ``C1`` and makes that superset legal (0.77,
    where the solver's own figure is 0.76). Run it with ``Y`` and ``W`` locked.
    """
    model = CarmaModel()
    model.sr_interface("ISig", {"value": {"implementation_type": "uint32"}})
    model.sr_interface("ISmall", {"value": {"implementation_type": "uint8"}})
    model.core("C1", threshold=0.815)
    model.core("C2")
    model.partition("PA", "C1")
    model.partition("PB", "C1")
    model.partition("PC", "C2")
    model.component("X", ports=sr_ports(["pXY"]), runnables=[runnable("X_Run", writes=[("pXY", "value")])])
    model.component("Y", ports=sr_ports(requires=["rXY"]), runnables=[runnable("Y_Run")])
    model.component(
        "Z",
        ports=sr_ports(["pZW"], interface="ISmall"),
        runnables=[runnable("Z_Run", wcet="500us", writes=[("pZW", "value")])],
    )
    model.component("W", ports=sr_ports(requires=["rZW"], interface="ISmall"), runnables=[runnable("W_Run")])
    model.task("TA", "PA", 0.01, runnables=[("X", "X_Run")], wcet="100us")
    model.task("TZ", "PA", 0.01, runnables=[("Z", "Z_Run")], wcet="1000us")
    model.task("TB", "PB", 0.01, runnables=[("Y", "Y_Run")], wcet="7000us")
    model.task("TC", "PC", 0.01, runnables=[("W", "W_Run")], wcet="1000us")
    model.connector("XY", ("X", "pXY"), ("Y", "rXY"))
    model.connector("ZW", ("Z", "pZW"), ("W", "rZW"))
    return model


def budget_cap() -> CarmaModel:
    """Two partitions ``PA`` and ``PB`` on the ``advisory`` core ``C1``; ``A`` (in ``TA``) sends to ``B`` (in ``TB``).

    Each runnable is budgeted ``1ns``; ``TA``'s budget is ``1ns`` and ``TB``'s
    ``1000000000s``. Moving ``A`` into ``PB`` would make ``TB``
    ``1000000000000000001ns`` -- 19 digits, past the factor cap the model loads
    -- so that candidate is refused and never written; moving ``B`` into ``PA``
    leaves ``TB`` at ``999999999999999999ns``, exactly 18 digits, which is
    written and loads. The core is ``advisory`` so the vast task budget is an
    information line and never a constraint.
    """
    model = CarmaModel()
    model.sr_interface("ISig", {"value": {"implementation_type": "uint32"}})
    model.core("C1", enforcement="advisory")
    model.partition("PA", "C1")
    model.partition("PB", "C1")
    model.component("A", ports=sr_ports(["pAB"]), runnables=[runnable("A_Run", wcet="1ns", writes=[("pAB", "value")])])
    model.component("B", ports=sr_ports(requires=["rAB"]), runnables=[runnable("B_Run", wcet="1ns")])
    model.task("TA", "PA", 0.01, runnables=[("A", "A_Run")], wcet="1ns")
    model.task("TB", "PB", 0.01, runnables=[("B", "B_Run")], wcet="1000000000s")
    model.connector("AB", ("A", "pAB"), ("B", "rAB"))
    return model


def mixed_task() -> CarmaModel:
    """``Q`` (QM, in ``P1``) sends to ``B`` (ASIL B, in ``P2``); each may move into the unrated ``P4``.

    The freedom table admits each move on its own -- ``T_P4_10ms`` runs nothing
    yet -- but the two together make that task mix levels without a
    ``coexistence`` argument: ``rule:safety.task-mixed-asil``, which the
    per-candidate check finds and the table, judging one move at a time, cannot.
    """
    model = CarmaModel()
    model.sr_interface("ISig", {"value": {"implementation_type": "uint32"}})
    for core in ("C1", "C2", "C4"):
        model.core(core, asil_capability="B")
    model.partition("P1", "C1", asil="QM")
    model.partition("P2", "C2", asil="B")
    model.partition("P4", "C4")
    model.component("Q", asil="QM", ports=sr_ports(["pQB"]), runnables=[runnable("Q_Run", writes=[("pQB", "value")])])
    model.component("B", asil="B", ports=sr_ports(requires=["rQB"]), runnables=[runnable("B_Run")])
    model.task("T_P1_10ms", "P1", 0.01, runnables=[("Q", "Q_Run")])
    model.task("T_P2_10ms", "P2", 0.01, runnables=[("B", "B_Run")])
    model.task("T_P4_10ms", "P4", 0.01, runnables=[], wcet="500us")
    model.connector("QB", ("Q", "pQB"), ("B", "rQB"))
    return model


#: :func:`formulation`'s links, name -> (sender, receiver), each commented with its shape.
FORMULATION_LINKS: dict[str, tuple[str, str]] = {
    "AE": ("A", "E"),  # free / free, overlapping domains: VARIABLE
    "AG": ("A", "G"),  # free / fixed, G's partition inside A's domain: VARIABLE
    "AH": ("A", "H"),  # free / fixed, H's partition outside A's domain: CONSTANT, always crosses
    "ED": ("E", "D"),  # free / free, disjoint domains: CONSTANT, always crosses
    "GH": ("G", "H"),  # fixed / fixed, crossing: CONSTANT
}
#: The experiment keys that shape :func:`formulation`'s domains: A {P1, P2}, E {P2, P1}, D {P3, P4}.
FORMULATION_EXPERIMENT: dict[str, Any] = {
    "locked": ["G", "H"],
    "allowed_partitions": {"A": ["P2"], "E": ["P1"], "D": ["P4"]},
}


def formulation() -> CarmaModel:
    """Four partitions on four cores; every link one 4-byte element every 10 ms (400 bytes x Hz)."""
    model = CarmaModel()
    model.sr_interface("ISig", {"value": {"implementation_type": "uint32"}})
    home = {"A": "P1", "E": "P2", "D": "P3", "G": "P2", "H": "P3"}
    for index in range(1, 5):
        model.core(f"C{index}")
        model.partition(f"P{index}", f"C{index}")
    out: dict[str, list[str]] = {}
    into: dict[str, list[str]] = {}
    for name, (sender, receiver) in FORMULATION_LINKS.items():
        out.setdefault(sender, []).append(f"p{name}")
        into.setdefault(receiver, []).append(f"r{name}")
    for name in home:
        model.component(
            name,
            ports=sr_ports(out.get(name, []), into.get(name, [])),
            runnables=[runnable(f"{name}_Run", writes=[(port, "value") for port in out.get(name, [])])],
        )
    for index in range(1, 5):
        partition = f"P{index}"
        members = [(name, f"{name}_Run") for name, where in home.items() if where == partition]
        model.task(f"T_{partition}_10ms", partition, 0.01, runnables=members)
    for name, (sender, receiver) in FORMULATION_LINKS.items():
        model.connector(name, (sender, f"p{name}"), (receiver, f"r{name}"))
    return model


def tolerance_scale() -> CarmaModel:
    """A VARIABLE link of cost 2 still crossing at the optimum, beside ``C0 = 1``.

    ``M`` (in ``P1``, its runnable every 0.5 s) sends one byte to ``R`` in
    ``P2`` (2 bytes x Hz) and four bytes to ``S`` in ``P3`` (8 bytes x Hz);
    ``G`` in ``P1`` sends one byte every second to ``R`` (1 byte x Hz, fixed /
    fixed, crossing: ``C0 = 1``). ``C2``'s ``strict`` threshold equals its load,
    so ``M`` cannot join ``R``: the optimum moves ``M`` to ``P3`` and keeps the
    cost-2 link crossing, ``V* = 3``. The objective's coefficients are 2 and 8,
    so its scale is ``s = 1/2``. Run it with ``R``, ``S`` and ``G`` locked.
    """
    model = CarmaModel()
    model.sr_interface("ISig", {"value": {"implementation_type": "uint32"}})
    model.sr_interface("ISmall", {"value": {"implementation_type": "uint8"}})
    model.core("C1")
    model.core("C2", threshold=0.102)
    model.core("C3")
    for index in range(1, 4):
        model.partition(f"P{index}", f"C{index}")
    model.component(
        "M",
        ports=[*sr_ports(["pMR"], interface="ISmall"), *sr_ports(["pMS"])],
        runnables=[runnable("M_Run", period=0.5, writes=[("pMR", "value"), ("pMS", "value")])],
    )
    model.component(
        "G",
        ports=sr_ports(["pGR"], interface="ISmall"),
        runnables=[runnable("G_Run", period=1.0, writes=[("pGR", "value")])],
    )
    model.component("R", ports=sr_ports(requires=["rMR", "rGR"], interface="ISmall"), runnables=[runnable("R_Run")])
    model.component("S", ports=sr_ports(requires=["rMS"]), runnables=[runnable("S_Run")])
    model.task("T_P1_500ms", "P1", 0.5, runnables=[("M", "M_Run")])
    model.task("T_P1_1s", "P1", 1.0, runnables=[("G", "G_Run")])
    model.task("T_P2_10ms", "P2", 0.01, runnables=[("R", "R_Run")])
    model.task("T_P2_500ms", "P2", 0.5, runnables=[])
    model.task("T_P3_10ms", "P3", 0.01, runnables=[("S", "S_Run")])
    model.task("T_P3_500ms", "P3", 0.5, runnables=[])
    model.connector("MR", ("M", "pMR"), ("R", "rMR"))
    model.connector("MS", ("M", "pMS"), ("S", "rMS"))
    model.connector("GR", ("G", "pGR"), ("R", "rGR"))
    return model


#: Two periods a float spelling makes awkward: taken exactly, their frequencies' denominators are
#: 5000000000000001 and 15000000000000001, whose least common multiple is about 7.5e31.
AWKWARD_PERIODS = (0.010000000000000002, 0.030000000000000002)


def awkward_periods(*, links: bool = True) -> CarmaModel:
    """A valid model whose exact scaling exceeds the solver's integers: two senders on the awkward periods.

    ``A`` and ``B`` (in ``P1``) each send to a receiver in ``P2`` and may move
    there, so the objective and both cores' constraints carry both
    denominators. Run it with ``RA`` and ``RB`` locked. With *links* false the
    two connectors are left out: the objective carries no volume and the cores'
    constraints alone carry the denominators.
    """
    model = CarmaModel()
    model.sr_interface("ISig", {"value": {"implementation_type": "uint32"}})
    model.core("C1")
    model.core("C2")
    model.partition("P1", "C1")
    model.partition("P2", "C2")
    fast, slow = AWKWARD_PERIODS
    model.component("A", ports=sr_ports(["pA"]), runnables=[runnable("A_Run", period=fast, writes=[("pA", "value")])])
    model.component("B", ports=sr_ports(["pB"]), runnables=[runnable("B_Run", period=slow, writes=[("pB", "value")])])
    model.component("RA", ports=sr_ports(requires=["rA"]), runnables=[runnable("RA_Run")])
    model.component("RB", ports=sr_ports(requires=["rB"]), runnables=[runnable("RB_Run")])
    model.task("T_P1_fast", "P1", fast, runnables=[("A", "A_Run")], priority=30)
    model.task("T_P1_slow", "P1", slow, runnables=[("B", "B_Run")], priority=15)
    model.task("T_P2_10ms", "P2", 0.01, runnables=[("RA", "RA_Run"), ("RB", "RB_Run")], priority=40)
    model.task("T_P2_fast", "P2", fast, runnables=[], priority=30)
    model.task("T_P2_slow", "P2", slow, runnables=[], priority=15)
    if links:
        model.connector("LA", ("A", "pA"), ("RA", "rA"))
        model.connector("LB", ("B", "pB"), ("RB", "rB"))
    return model


#: :func:`unlinked_free`'s components that send nothing and receive nothing, three in each partition.
UNLINKED_FREE_IDLE = ("U0", "U1", "U2", "U3", "U4", "U5")


def unlinked_free(*, connector: bool = True) -> CarmaModel:
    """``A`` (in ``P1``) sends to ``X`` (in ``P2``); six unlinked components, each free to move to the other side.

    Every component is unrated, so every move is admissible: a move of a ``U``
    changes no volume and is never part of a point's answer. Run it with ``X``
    locked; the one move that buys anything is ``A -> P2`` (400 bytes x Hz).
    With *connector* false the model carries no link at all: the volume is zero
    at ``N = 0`` and no move can lower it.
    """
    model = CarmaModel()
    model.sr_interface("ISig", {"value": {"implementation_type": "uint32"}})
    model.core("C1")
    model.core("C2")
    model.partition("P1", "C1")
    model.partition("P2", "C2")
    model.component("A", ports=sr_ports(["pAX"]), runnables=[runnable("A_Run", writes=[("pAX", "value")])])
    model.component("X", ports=sr_ports(requires=["rAX"]), runnables=[runnable("X_Run")])
    for name in UNLINKED_FREE_IDLE:
        model.component(name, runnables=[runnable(f"{name}_Run")])
    first, second = UNLINKED_FREE_IDLE[:3], UNLINKED_FREE_IDLE[3:]
    model.task("T1", "P1", 0.01, runnables=[("A", "A_Run"), *((u, f"{u}_Run") for u in first)], wcet="3000us")
    model.task("T2", "P2", 0.01, runnables=[("X", "X_Run"), *((u, f"{u}_Run") for u in second)], wcet="3000us")
    if connector:
        model.connector("AX", ("A", "pAX"), ("X", "rAX"))
    return model


def mixed_task_with_idle() -> CarmaModel:
    """:func:`mixed_task` and ``V`` (QM, in ``P1``, linked to nothing), which may move into ``P4`` too.

    ``{Q -> P4, B -> P4}`` is the only move set that joins ``QB``, and the check
    refuses it (``rule:safety.task-mixed-asil``) at ``N = 2``. At ``N = 3`` the
    search must not propose it again -- a refused set is refused at every N --
    so the one set it proposes and the check refuses there is the superset with
    ``V -> P4``, which mixes the same task.
    """
    model = mixed_task()
    model.component("V", asil="QM", runnables=[runnable("V_Run")])
    model.get("tasks/T_P1_10ms.yaml")["runnables"].append({"swc": "V", "runnable": "V_Run"})
    return model


def tolerance_exact() -> CarmaModel:
    """Two senders in ``P1``, each to a locked receiver in ``P2``: 130 and 100 bytes x Hz, every 100 ms.

    ``A`` sends a 13-byte array to ``X``, ``B`` a 10-byte array to ``Y``. At
    ``N = 1`` the optimum moves ``A`` (100 left); moving ``B`` leaves 130,
    exactly the optimum times ``1 + 0.3``. ``0.3`` as a binary float is below
    ``3/10``, so a bound computed from the float rather than from the shortest
    decimal would floor one below and drop that candidate. Run it with ``X``
    and ``Y`` locked.
    """
    model = CarmaModel()
    for length in (10, 13):
        model.implementation_type(
            {
                "name": f"bytes{length}",
                "kind": "implementation_array",
                "element_type": "uint8",
                "length": length,
                "element_name": "e",
            }
        )
        model.sr_interface(f"I{length}", {"value": {"implementation_type": f"bytes{length}"}})
    model.core("C1")
    model.core("C2")
    model.partition("P1", "C1")
    model.partition("P2", "C2")
    for sender, receiver, interface in (("A", "X", "I13"), ("B", "Y", "I10")):
        model.component(
            sender,
            ports=sr_ports([f"p{sender}"], interface=interface),
            runnables=[runnable(f"{sender}_Run", period=0.1, writes=[(f"p{sender}", "value")])],
        )
        model.component(
            receiver,
            ports=sr_ports(requires=[f"r{sender}"], interface=interface),
            runnables=[runnable(f"{receiver}_Run", period=0.1)],
        )
        model.connector(f"L{sender}", (sender, f"p{sender}"), (receiver, f"r{sender}"))
    model.task("T_P1_100ms", "P1", 0.1, runnables=[("A", "A_Run"), ("B", "B_Run")])
    model.task("T_P2_100ms", "P2", 0.1, runnables=[("X", "X_Run"), ("Y", "Y_Run")])
    return model


class _FailingFinder:
    """A meta path finder that fails the import of ``scipy.optimize`` with *error*, as a DLL that will not load does."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def find_spec(self, name: str, path: object = None, target: object = None) -> None:
        if name == "scipy.optimize":
            raise self.error


def unloadable_solver(monkeypatch, error: BaseException) -> None:
    """Make the next ``import scipy.optimize`` raise *error* (restored by *monkeypatch*).

    ``raising=False``: whether an earlier test in this worker already imported ``scipy.optimize``
    depends on the order and the xdist distribution, so its absence is not an error here; the
    failing finder is consulted only when the module is not cached, which the deletion ensures.
    """
    monkeypatch.delitem(sys.modules, "scipy.optimize", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_FailingFinder(error), *sys.meta_path])
