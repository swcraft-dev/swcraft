"""The stub-bearing model the writer's tests and the XSD suite both generate.

Both stub kinds, in every shape the writer emits a reference to one in, in one
valid project, built in code from the ``read-swc-element`` vector's baseline
(``examples/basic``'s model) rather than committed under
``packages/tools/tests/fixtures/``: a committed tree with a ``compositions/`` file
must be recorded at a branch point by the three byte-identity records
(``test_diagram_switch_byte_identity``, ``test_application_types_byte_identity``,
``test_external_stubs_byte_identity``), and a stub-bearing tree has none.

Beside the baseline's two components, one connector and two interfaces, the model holds:

* four interface stubs — ``CLIENT-SERVER-INTERFACE``, ``SENDER-RECEIVER-INTERFACE``,
  ``MODE-SWITCH-INTERFACE`` and the abstract ``PORT-INTERFACE`` — and one type
  stub, ``IMPLEMENTATION-DATA-TYPE``;
* a provide port on each of the first three on ``ExampleSWC``, a require port on
  each on ``ExampleConsumerSWC``, and a require port on the abstract one;
* two assembly connectors across a stub port (the SR and the CS pair);
* two root boundary ports and two delegations: a provide one from the SR stub onto
  the declared ``IExample`` (the kind compared across the stub, the rest abstaining),
  and a require one on the CS stub at both ends;
* a data element and a client-server argument typed by the type stub, and a
  reference type over it typing a second data element.

``check-model`` reports nothing on it (asserted by the writer tests), so the schema
run validates the output of a model the gate accepts. A second model lives here too:
the compositions fixture's **component** stub wired by :func:`add_component_stub_ports`.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import yaml
from conftest import write_model_element
from swcraft_tools.authorization import SafetyChangeKind
from swcraft_tools.validator import check_cross_layer_consistency

#: ``conftest.COMPOSITIONS_MODEL_ROOT``, spelled from this file rather than imported: ``mcp_server``'s
#: suites import this module too, and there ``conftest`` is their own.
COMPOSITIONS_MODEL_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "compositions" / "model"

#: ``(stub name, external_ref, external_dest)`` for every interface stub the model declares.
INTERFACE_STUBS: tuple[tuple[str, str, str], ...] = (
    ("IForeignCS", "/Foreign/PortInterfaces/IForeignCS", "CLIENT-SERVER-INTERFACE"),
    ("IForeignSR", "/Foreign/PortInterfaces/IForeignSR", "SENDER-RECEIVER-INTERFACE"),
    ("IForeignMode", "/Foreign/ModeInterfaces/IForeignMode", "MODE-SWITCH-INTERFACE"),
    ("IForeignAny", "/Foreign/PortInterfaces/IForeignAny", "PORT-INTERFACE"),
)

#: The one type stub.
TYPE_STUB: tuple[str, str, str] = ("ForeignT", "/Foreign/ImplementationDataTypes/ForeignT", "IMPLEMENTATION-DATA-TYPE")

#: ``(component, port, direction, interface)`` for every port the model adds on a stub.
STUB_PORTS: tuple[tuple[str, str, str, str], ...] = (
    ("ExampleSWC", "pCs", "provide", "IForeignCS"),
    ("ExampleSWC", "pSr", "provide", "IForeignSR"),
    ("ExampleSWC", "pMode", "provide", "IForeignMode"),
    ("ExampleConsumerSWC", "rCs", "require", "IForeignCS"),
    ("ExampleConsumerSWC", "rSr", "require", "IForeignSR"),
    ("ExampleConsumerSWC", "rMode", "require", "IForeignMode"),
    ("ExampleConsumerSWC", "rAny", "require", "IForeignAny"),
)

#: ``(port, direction, interface)`` for the root composition's two boundary ports.
BOUNDARY_PORTS: tuple[tuple[str, str, str], ...] = (
    ("pExtSr", "provide", "IForeignSR"),
    ("rExtCs", "require", "IForeignCS"),
)

PROVIDER = "swc/example/ExampleSWC.yaml"
CONSUMER = "swc/example/ExampleConsumerSWC.yaml"
SR_INTERFACE = "interfaces/IExample.yaml"
CS_INTERFACE = "interfaces/IDiagnosticService.yaml"
ROOT = "compositions/Root.yaml"


def put(model_root: Path, relative: str, document: dict[str, Any]) -> None:
    """Write *document* as the YAML file *relative* to *model_root*."""
    path = model_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def change(model_root: Path, relative: str, mutate: Callable[[dict[str, Any]], object]) -> None:
    """Load the YAML file *relative* to *model_root*, apply *mutate* and write it back."""
    document = yaml.safe_load((model_root / relative).read_text(encoding="utf-8"))
    mutate(document)
    put(model_root, relative, document)


def set_port(model_root: Path, relative: str, port: str, **changes: Any) -> None:
    """Apply *changes* to one port of the component file *relative*; a ``None`` value removes the key."""

    def mutate(document: dict[str, Any]) -> None:
        (target,) = [entry for entry in document["ports"] if entry["name"] == port]
        for key, value in changes.items():
            if value is None:
                target.pop(key, None)
            else:
                target[key] = value

    change(model_root, relative, mutate)


def rules(model_root: Path) -> list[str]:
    """Return the rule of every record ``check-model``'s cross-layer check reports over *model_root*, in order."""
    return [record.rule for record in check_cross_layer_consistency(str(model_root)).records]


def interface_stub(model_root: Path, name: str, ref: str, dest: str) -> None:
    """Declare an ``external_interface`` stub."""
    put(
        model_root,
        f"interfaces/{name}.yaml",
        {"name": name, "kind": "external_interface", "external_ref": ref, "external_dest": dest},
    )


def type_stub(model_root: Path, name: str, ref: str, dest: str) -> None:
    """Declare an ``external_type`` stub."""
    write_model_element(model_root, {"name": name, "kind": "external_type", "external_ref": ref, "external_dest": dest})


def baseline_model_root(destination: Path) -> Path:
    """Build the ``read-swc-element`` vector's baseline project at *destination*; return its model root."""
    return fixtures.build_fixture(destination, "read-swc-element") / "model"


def external_stubs_model_root(destination: Path) -> Path:
    """Build the stub-bearing project at *destination* (which must not exist); return its model root."""
    return add_external_stubs(baseline_model_root(destination))


def add_external_stubs(model_root: Path) -> Path:
    """Add every stub, port, connector and delegation above to the baseline model at *model_root*; return it.

    Split out so a suite building its own project from ``examples/basic`` (the MCP
    reply gate) can put the same stubs into that project.
    """
    for name, ref, dest in INTERFACE_STUBS:
        interface_stub(model_root, name, ref, dest)
    type_stub(model_root, *TYPE_STUB)
    write_model_element(model_root, {"name": "RefForeign_T", "kind": "reference_type", "base": TYPE_STUB[0]})

    def more_elements(document: dict[str, Any]) -> None:
        document["data_elements"].append({"name": "foreign", "implementation_type": TYPE_STUB[0]})
        document["data_elements"].append({"name": "foreignRef", "implementation_type": "RefForeign_T"})

    change(model_root, SR_INTERFACE, more_elements)
    change(
        model_root,
        CS_INTERFACE,
        lambda d: d["operations"][0]["arguments"].append(
            {"name": "foreign", "direction": "in", "implementation_type": TYPE_STUB[0]}
        ),
    )

    for component, relative in (("ExampleSWC", PROVIDER), ("ExampleConsumerSWC", CONSUMER)):
        ports = [
            {"name": port, "direction": direction, "interface": interface}
            for owner, port, direction, interface in STUB_PORTS
            if owner == component
        ]
        change(model_root, relative, lambda d, ports=ports: d["ports"].extend(ports))

    for name, source, target in (("Sr_across_stub", "pSr", "rSr"), ("Cs_across_stub", "pCs", "rCs")):
        put(
            model_root,
            f"connectors/{name}.yaml",
            {
                "name": name,
                "from": {"swc": "ExampleSWC", "port": source},
                "to": {"swc": "ExampleConsumerSWC", "port": target},
            },
        )

    def boundary(document: dict[str, Any]) -> None:
        document["ports"] = [
            {"name": port, "direction": direction, "interface": interface}
            for port, direction, interface in BOUNDARY_PORTS
        ]
        document["delegations"] = [
            {"name": "Root_pExtSr", "port": "pExtSr", "member": "ExampleSWC", "member_port": "pOut"},
            {"name": "Root_rExtCs", "port": "rExtCs", "member": "ExampleConsumerSWC", "member_port": "rCs"},
        ]

    change(model_root, ROOT, boundary)
    return model_root


# ---------------------------------------------------------------------------
# An interface stub lists the members its ports use
# ---------------------------------------------------------------------------

#: The operations the client-server stub lists, in list order. Invented names.
LISTED_OPERATIONS: tuple[str, ...] = ("Get", "Set")

#: The data elements the sender-receiver stub lists, in list order. Invented names.
LISTED_DATA_ELEMENTS: tuple[str, ...] = ("speed", "angle")

#: ``(component file, runnable list or "events", entry)`` for every construct
#: :func:`add_listed_stub_members` makes name a listed member, one of each kind.
LISTED_MEMBER_REFERRERS: tuple[tuple[str, str, dict[str, str]], ...] = (
    (PROVIDER, "events", {"type": "operation", "port": "pCs", "operation": "Get"}),
    (PROVIDER, "writes", {"port": "pSr", "element": "speed"}),
    (CONSUMER, "reads", {"port": "rSr", "element": "angle"}),
    (CONSUMER, "calls", {"port": "rCs", "operation": "Set"}),
)


def listed_stub_model_root(destination: Path) -> Path:
    """Build the stub-bearing project at *destination* with both stubs' members listed; return its model root."""
    return add_listed_stub_members(external_stubs_model_root(destination))


def add_listed_stub_members(model_root: Path) -> Path:
    """List the stubs' members on the model :func:`add_external_stubs` built, and name each; return it.

    The client-server stub lists :data:`LISTED_OPERATIONS`, the sender-receiver
    one :data:`LISTED_DATA_ELEMENTS`; the mode-switch and the abstract stub list
    nothing (a mode-switch stub may not). :data:`LISTED_MEMBER_REFERRERS` then
    names one member through each construct: an operation event on the provide
    port ``pCs`` (on a runnable ``Serve`` of its own), a write on ``pSr``, a read
    by value on ``rSr`` and a call on ``rCs``. ``check-model`` reports nothing on
    the result.
    """
    change(model_root, "interfaces/IForeignCS.yaml", lambda d: d.update(operations=list(LISTED_OPERATIONS)))
    change(model_root, "interfaces/IForeignSR.yaml", lambda d: d.update(data_elements=list(LISTED_DATA_ELEMENTS)))
    for relative, where, entry in LISTED_MEMBER_REFERRERS:
        if where == "events":
            change(
                model_root,
                relative,
                lambda d, entry=entry: d["runnables"].append({"name": "Serve", "events": [entry]}),
            )
        else:
            change(model_root, relative, lambda d, where=where, entry=entry: d["runnables"][0].update({where: [entry]}))
    return model_root


# ---------------------------------------------------------------------------
# Ports on an external COMPONENT stub
# ---------------------------------------------------------------------------

#: ``(port, direction, observed_interface or None)`` on the compositions fixture's stub ``ExternalStub``.
COMPONENT_STUB_PORTS: tuple[tuple[str, str, str | None], ...] = (
    ("pExt", "provide", "ITrigger"),
    ("rExt", "require", None),
)

#: The second component stub, so one connector can run stub to stub: ``(name, external_ref, its one port)``.
SECOND_COMPONENT_STUB: tuple[str, str, tuple[str, str, str | None]] = (
    "OtherStub",
    "/Other/Project/SupplierPlatforms/Bsw/OtherStub",
    ("rOther", "require", None),
)


def stub_port(name: str, direction: str, hint: str | None = None) -> dict[str, Any]:
    """Return a stub's port as the importer writes one: no ``interface``, the hint only when there is one."""
    port: dict[str, Any] = {"name": name, "direction": direction}
    if hint is not None:
        port["observed_interface"] = hint
    return port


def component_stub_ports_model_root(destination: Path) -> Path:
    """Copy ``fixtures/compositions/model`` to *destination*, wire it by :func:`add_component_stub_ports`; return it."""
    shutil.copytree(COMPOSITIONS_MODEL_ROOT, destination)
    return add_component_stub_ports(destination)


def add_component_stub_ports(model_root: Path) -> Path:
    """Wire the compositions fixture's stub: ports, two connectors and a delegation; return *model_root*.

    *model_root* is a writable copy of ``fixtures/compositions/model``. It gains every
    shape a connection to a stub's port takes:

    * ``ExternalStub`` declares :data:`COMPONENT_STUB_PORTS` — a provide port with
      a hint, a require port without one;
    * a second stub, ``OtherStub``, placed in ``Sub`` as member ``Ext2``, with one
      require port;
    * ``Beta`` gains a require port ``rFromExt`` on ``ITrigger``;
    * two assembly connectors drawn inside ``Sub``: ``ext_link`` from the stub's
      ``pExt`` to ``Beta.rFromExt`` (declared on one end), and ``stub_link`` from
      ``pExt`` to ``OtherStub.rOther`` (a stub on both ends);
    * a boundary port ``rExtIn`` on ``Sub`` delegated onto the stub's ``rExt``.

    ``check-model`` reports nothing on the result.
    """
    change(
        model_root,
        "swc/external/ExternalStub.yaml",
        lambda d: d.update(ports=[stub_port(*port) for port in COMPONENT_STUB_PORTS]),
    )
    name, ref, port = SECOND_COMPONENT_STUB
    put(
        model_root,
        f"swc/external/{name}.yaml",
        {
            "name": name,
            "type": "application",
            "external": True,
            "external_ref": ref,
            "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
            "ports": [stub_port(*port)],
            # The stub satisfies the tree's requirement itself.
            "satisfies": ["REQ_COMPOSITIONS_001@1"],
        },
    )
    change(
        model_root,
        "swc/core/Beta.yaml",
        lambda d: d["ports"].append({"name": "rFromExt", "direction": "require", "interface": "ITrigger"}),
    )

    def sub(document: dict[str, Any]) -> None:
        document["members"].append({"name": "Ext2", "type": name})
        document["ports"].append({"name": "rExtIn", "direction": "require", "interface": "ITrigger"})
        document["delegations"].append(
            {"name": "Sub_rExtIn_Ext", "port": "rExtIn", "member": "Ext", "member_port": "rExt"}
        )

    change(model_root, "compositions/Sub.yaml", sub)
    for connector, target in (
        ("ext_link", {"swc": "Beta", "port": "rFromExt"}),
        ("stub_link", {"swc": name, "port": port[0]}),
    ):
        put(
            model_root,
            f"connectors/{connector}.yaml",
            {
                "name": connector,
                "composition": "Sub",
                "from": {"swc": "ExternalStub", "port": "pExt"},
                "to": target,
            },
        )
    return model_root


#: The component stub :func:`add_basic_component_stub` places in ``examples/basic``'s model:
#: ``(name, external_ref)``.
BASIC_COMPONENT_STUB: tuple[str, str] = ("BasicStub", "/Other/Project/SupplierPlatforms/Bsw/BasicStub")

#: ``(port, direction, observed_interface or None)`` on :data:`BASIC_COMPONENT_STUB`.
BASIC_COMPONENT_STUB_PORTS: tuple[tuple[str, str, str | None], ...] = (
    ("pStub", "provide", "IExample"),
    ("rStub", "require", None),
)

#: The require port :func:`add_basic_component_stub` adds on ``ExampleConsumerSWC`` for the stub to feed.
BASIC_STUB_NEIGHBOUR_PORT = "rFromStub"


def add_basic_component_stub(model_root: Path) -> Path:
    """Place a component stub with ports in ``examples/basic``'s model at *model_root*; return it.

    :func:`add_component_stub_ports`' shape, reduced for a suite built on
    ``examples/basic`` (the MCP reply gate): :data:`BASIC_COMPONENT_STUB` declares
    :data:`BASIC_COMPONENT_STUB_PORTS` (a provide port hinting ``IExample``, a require
    port with none) and stands in ``Root``; ``ExampleConsumerSWC`` gains
    :data:`BASIC_STUB_NEIGHBOUR_PORT` on ``IExample``, and ``Stub_to_Consumer`` runs from
    the stub's ``pStub`` to it. ``check-model`` reports nothing on the result.
    """
    name, ref = BASIC_COMPONENT_STUB
    put(
        model_root,
        f"swc/external/{name}.yaml",
        {
            "name": name,
            "type": "application",
            "external": True,
            "external_ref": ref,
            "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
            "ports": [stub_port(*port) for port in BASIC_COMPONENT_STUB_PORTS],
            # The stub satisfies the base's requirement itself.
            "satisfies": ["REQ_EXAMPLE_001@1"],
        },
    )
    neighbour = {"name": BASIC_STUB_NEIGHBOUR_PORT, "direction": "require", "interface": "IExample"}
    change(model_root, CONSUMER, lambda d: d["ports"].append(neighbour))
    change(model_root, ROOT, lambda d: d["members"].append({"name": name, "type": name}))
    put(
        model_root,
        "connectors/Stub_to_Consumer.yaml",
        {
            "name": "Stub_to_Consumer",
            "from": {"swc": name, "port": BASIC_COMPONENT_STUB_PORTS[0][0]},
            "to": {"swc": "ExampleConsumerSWC", "port": BASIC_STUB_NEIGHBOUR_PORT},
        },
    )
    return model_root


# ---------------------------------------------------------------------------
# A rated consumer a change set can convert into a component stub
# ---------------------------------------------------------------------------

#: ``RatedConsumer``'s file under the model root.
RATED_CONSUMER_FILE = "swc/example/RatedConsumer.yaml"

#: The safety question clearing ``RatedConsumer.rIn``'s interface asks, as the suites project one:
#: ``(kind, resource, field, old, new)``.
RATED_CONSUMER_CLEARED = (SafetyChangeKind.LOWER_ALLOCATION.value, "RatedConsumer.rIn", "interface", "IExample", None)


def add_rated_consumer(model_root: Path) -> None:
    """Declare ``RatedConsumer`` in ``examples/basic``'s model at *model_root*, linked from ``REQ_EXAMPLE_001``.

    A declared component at ``B`` with one ``require`` port ``rIn`` on ``IExample``
    and nothing a stub may not carry but that port's interface, one memory budget and
    ``handle_termination_and_restart: no_support`` (refused missing on an emitted
    behaviour), so :func:`convert_rated_consumer` can make it a stub by fields --
    which ``ExampleConsumerSWC``, with runnables, a task and a sequence, cannot be.
    It is in no composition and wired to nothing; a suite needing either adds it.
    """
    put(
        model_root,
        RATED_CONSUMER_FILE,
        {
            "name": "RatedConsumer",
            "type": "application",
            "asil": "B",
            "ports": [{"name": "rIn", "direction": "require", "interface": "IExample"}],
            "memory": {"RAM0": 64},
            "handle_termination_and_restart": "no_support",
            "satisfies": ["REQ_EXAMPLE_001@1"],
        },
    )


def convert_rated_consumer() -> list[dict[str, Any]]:
    """Return the operations that make ``RatedConsumer`` a component stub: the triple set, budget and value dropped.

    The value is ``handle_termination_and_restart``, which a stub declaring is
    ``rule:swc.external-with-ports``' finding. The port is left alone.
    """

    def field(name: str, value: Any) -> dict[str, Any]:
        return {
            "operation": "update",
            "address": {"layer": "swc", "element": "RatedConsumer", "path": [{"field": name}]},
            "value": value,
        }

    return [
        field("external", True),
        field("external_ref", "/Foreign/RatedConsumer"),
        field("external_dest", "APPLICATION-SW-COMPONENT-TYPE"),
        {
            "operation": "remove",
            "address": {"layer": "swc", "element": "RatedConsumer", "path": [{"field": "memory"}, {"key": "RAM0"}]},
        },
        field("handle_termination_and_restart", None),
    ]
