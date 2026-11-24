"""The greenfield acceptance scenario, defined once for its two drivers.

* `tests/test_greenfield_acceptance.py` runs it in this repository's suite and asserts against what it observed;
* `scripts/tests/test_clean_install.py` runs it in a fresh venv built from `dist/wheelhouse/`, in a subprocess, and
  asserts against a JSON report.

Only the scenario is shared -- the operations, the scaffold, the server wiring and the run loop; every assertion
stays with its driver. A second copy would drift unseen. The module imports no pytest so the installed-venv
subprocess can import it.

Nothing here writes a model file: `swcraft init` writes the scaffold and every model mutation is a `change_set`
call (held by an AST scan in `test_greenfield_acceptance.py`).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import swcraft_mcp.server as server
from lxml import etree
from protocol_client import ScriptedHuman, call, call_reviewed
from swcraft_tools import cli

# ---------------------------------------------------------------------------
# The scaffold and the server the scenario drives
# ---------------------------------------------------------------------------

#: Pinned rather than derived from the directory: two determinism runs live at two paths, and the name reaches the
#: generated artifacts.
PROJECT_NAME = "greenfield"

#: The generated artifacts `swcraft init`'s manifest declares, relative to the project root: the ARXML, the root
#: composition's internal block diagram, the view index and the Sphinx-Needs JSON. All four are compared byte for
#: byte between two runs.
ARTIFACTS = (
    "dist/model.arxml",
    "views/ibd-Root.svg",
    "views/index.html",
    "dist/needs.json",
)


def scaffold(root: Path) -> Path:
    """`swcraft init` a clean project at *root* and return its path.

    The CLI, not a hand-built manifest, so the scaffold is the one a user gets: the manifest, the model directories,
    `.vscode/mcp.json`, the hardware `CPU`, `CORE0` and `PARTITION0`, and an empty root composition.

    `sys.argv` is swapped by hand because the scenario runs in a module-scoped fixture, where `monkeypatch` is
    unavailable.
    """
    argv = sys.argv
    try:
        sys.argv = ["swcraft", "init", str(root), "--name", PROJECT_NAME]
        cli.main()
    finally:
        sys.argv = argv
    return root


def serve(project: Path, *, review: str | None = "elicit", scope: Any = None, scope_id: str | None = None) -> None:
    """Point the MCP server at *project*, the way a fresh spawn would.

    The review mode and the approved scope are host channels read once at server start, so the environment variable
    is set and re-read, as a restart does. No tool argument can reach these.
    """
    os.environ["SWCRAFT_MANIFEST"] = str(project / "swcraft.yaml")
    if review is None:
        os.environ.pop("SWCRAFT_REVIEW", None)
    else:
        os.environ["SWCRAFT_REVIEW"] = review
    if scope is None:
        os.environ.pop("SWCRAFT_APPROVED_SCOPE", None)
        os.environ.pop("SWCRAFT_SCOPE_ID", None)
    else:
        os.environ["SWCRAFT_APPROVED_SCOPE"] = json.dumps(scope)
        os.environ["SWCRAFT_SCOPE_ID"] = scope_id or "turn-1"
    server._PROJECT = None
    server._configure_review_mode()
    server._configure_scope_channel()


# ---------------------------------------------------------------------------
# The one atomic initial change set
# ---------------------------------------------------------------------------


def create(layer: str, element: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "create", "address": {"layer": layer, "element": element}, "value": value}


def place(name: str) -> dict[str, Any]:
    """Place one component in the scaffolded root composition, by member address.

    `rule:composition.connector-endpoint-not-a-member` requires every connector endpoint to name a type the
    connector's composition places, so the components are placed through a `create` at a member address of the
    scaffolded `Root`. The member is named after its type; both fields are always written.
    """
    return {
        "operation": "create",
        "address": {
            "layer": "compositions",
            "element": "Root",
            "path": [{"field": "members"}, {"member": name}],
        },
        "value": {"name": name, "type": name},
    }


#: Nine layers in one transaction, ordered so every reference is created before the element that names it: the
#: protocol applies operations in order and refuses a set that would need re-ordering (`partial-change-set`).
SEED_OPERATIONS: list[dict[str, Any]] = [
    create(
        "base_types",
        "float32_base",
        {"name": "float32_base", "kind": "base_type", "size": 32, "encoding": "IEEE754", "native_declaration": "float"},
    ),
    create("implementation_types", "float32", {"name": "float32", "kind": "value_type", "base": "float32_base"}),
    create(
        "implementation_types",
        "Percent",
        {
            "name": "Percent",
            "kind": "value_type",
            "base": "float32_base",
            "unit": "%",
            "value_min": 0.0,
            "value_max": 100.0,
        },
    ),
    # An `implementation_types` element the `filtered` read drops; without it a filter that matched everything
    # would answer the same.
    create(
        "implementation_types",
        "SyntheticRef",
        {"name": "SyntheticRef", "kind": "reference_type", "base": "float32"},
    ),
    create(
        "interfaces",
        "IVehicleSpeed",
        {
            "name": "IVehicleSpeed",
            "kind": "sender_receiver",
            "data_elements": [{"name": "speed", "implementation_type": "float32"}],
        },
    ),
    create(
        "interfaces",
        "IBrakeLoad",
        {
            "name": "IBrakeLoad",
            "kind": "sender_receiver",
            "data_elements": [{"name": "load", "implementation_type": "Percent"}],
        },
    ),
    create(
        "swc",
        "SpeedSensorSWC",
        {
            "name": "SpeedSensorSWC",
            "group": "sensors",
            "type": "application",
            "description": "Publishes the measured vehicle speed",
            "handle_termination_and_restart": "no_support",
            "ports": [{"name": "pSpeed", "direction": "provide", "interface": "IVehicleSpeed"}],
            "runnables": [
                {"name": "SpeedSensorRun", "min_start_interval": 0.01, "events": [{"type": "timing", "period": 0.01}]}
            ],
            "satisfies": ["REQ_BRAKE_001@1"],
        },
    ),
    create(
        "swc",
        "BrakeCtrlSWC",
        {
            "name": "BrakeCtrlSWC",
            "group": "control",
            "type": "application",
            "description": "Derives the brake load from the measured vehicle speed",
            "handle_termination_and_restart": "no_support",
            "ports": [
                {"name": "rSpeed", "direction": "require", "interface": "IVehicleSpeed"},
                {"name": "pLoad", "direction": "provide", "interface": "IBrakeLoad"},
            ],
            "runnables": [
                {"name": "BrakeCtrlRun", "min_start_interval": 0.02, "events": [{"type": "timing", "period": 0.02}]}
            ],
            "satisfies": ["REQ_BRAKE_001@1"],
        },
    ),
    place("SpeedSensorSWC"),
    place("BrakeCtrlSWC"),
    create(
        "connectors",
        "Speed_to_BrakeCtrl",
        {
            "name": "Speed_to_BrakeCtrl",
            "from": {"swc": "SpeedSensorSWC", "port": "pSpeed"},
            "to": {"swc": "BrakeCtrlSWC", "port": "rSpeed"},
        },
    ),
    create(
        "requirements",
        "REQ_BRAKE_001",
        {
            "id": "REQ_BRAKE_001",
            "title": "Brake control shall follow the measured vehicle speed",
            "description": "The brake controller shall derive its load from the measured vehicle speed.",
            "req_type": "req",
            "version": 1,
            "source": "architecture",
        },
    ),
    create(
        "tasks",
        "Task_20ms",
        {
            "name": "Task_20ms",
            "period": 0.02,
            "priority": 5,
            # The partition `swcraft init` scaffolded, so the first task needs no hardware edit.
            "partition": "PARTITION0",
            "wcet": "1200us",
            "description": "20 ms brake control task",
            "runnables": [{"swc": "BrakeCtrlSWC", "runnable": "BrakeCtrlRun"}],
        },
    ),
    create(
        "sequences",
        "task20ms_brake_cycle",
        {
            "name": "task20ms_brake_cycle",
            "task": "Task_20ms",
            "title": "Brake control cycle",
            "steps": [{"swc": "BrakeCtrlSWC", "runnable": "BrakeCtrlRun"}],
        },
    ),
]

#: The identity rule, as a step of its own: an application type and an implementation type under one name -- two
#: identities in two layers, never an `identity-conflict` -- the mapping set pairing them, and an interface with a
#: data element on each side. Each type's inline bounds generate a `SyntheticTorque_Range` in its own layer's
#: package, where the two do not collide. No port speaks the interface, so no component needs the set.
SAME_NAME_PAIR = "SyntheticTorque"
SAME_NAME_PAIR_OPERATIONS: list[dict[str, Any]] = [
    create(
        "application_types",
        SAME_NAME_PAIR,
        {
            "name": SAME_NAME_PAIR,
            "kind": "application_type",
            "unit": "Nm",
            "range": {"min": -500.0, "max": 500.0},
            "description": "Synthetic torque, physical view",
        },
    ),
    create(
        "implementation_types",
        SAME_NAME_PAIR,
        {
            "name": SAME_NAME_PAIR,
            "kind": "value_type",
            "base": "float32_base",
            "value_min": -500.0,
            "value_max": 500.0,
            "description": "Synthetic torque, stored",
        },
    ),
    create(
        "mapping_sets",
        "SyntheticTorqueMapping",
        {
            "name": "SyntheticTorqueMapping",
            "maps": [{"application": SAME_NAME_PAIR, "implementation": SAME_NAME_PAIR}],
            "description": "Pairs the two views of the synthetic torque, which share one name",
        },
    ),
    create(
        "interfaces",
        "ISyntheticTorque",
        {
            "name": "ISyntheticTorque",
            "kind": "sender_receiver",
            "data_elements": [
                {"name": "physical", "application_type": SAME_NAME_PAIR},
                {"name": "stored", "implementation_type": SAME_NAME_PAIR},
            ],
            "description": "Both views of the synthetic torque",
        },
    ),
]

#: The data description elements, as a step of its own: a unit, a linear compu method naming it and an application
#: type naming both; a texttable an enumeration and a value type both name. The ARXML carries `UNIT-REF` and
#: `COMPU-METHOD-REF` to the elements' paths and no private form of the types' own. No port speaks the types.
ELEMENTS_UNIT = "SyntheticNewtonMetre"
ELEMENTS_LINEAR = "SyntheticTorqueScale"
ELEMENTS_TYPE = "SyntheticDemand"
ELEMENTS_TABLE = "SyntheticGearTable"
ELEMENTS_ENUM = "SyntheticGear"
ELEMENTS_VALUE_TYPE = "SyntheticGearRaw"
ELEMENTS_OPERATIONS: list[dict[str, Any]] = [
    create(
        "units",
        ELEMENTS_UNIT,
        {"name": ELEMENTS_UNIT, "display_name": "Nm", "description": "The synthetic torque unit"},
    ),
    create(
        "compu_methods",
        ELEMENTS_LINEAR,
        {
            "name": ELEMENTS_LINEAR,
            "kind": "linear",
            "unit": ELEMENTS_UNIT,
            "factor": 0.5,
            "offset": 0.0,
            "description": "Half a newton metre per count",
        },
    ),
    create(
        "application_types",
        ELEMENTS_TYPE,
        {
            "name": ELEMENTS_TYPE,
            "kind": "application_type",
            "unit_ref": ELEMENTS_UNIT,
            "compu_method_ref": ELEMENTS_LINEAR,
            "description": "Synthetic torque demand, its unit and scale by reference",
        },
    ),
    create(
        "compu_methods",
        ELEMENTS_TABLE,
        {
            "name": ELEMENTS_TABLE,
            "kind": "texttable",
            "entries": [{"name": "SyntheticPark", "value": 0}, {"name": "SyntheticDrive", "value": 1}],
            "description": "The synthetic gear's values",
        },
    ),
    create(
        "application_types",
        ELEMENTS_ENUM,
        {
            "name": ELEMENTS_ENUM,
            "kind": "enum",
            "compu_method_ref": ELEMENTS_TABLE,
            "description": "Synthetic gear, its values in the shared table",
        },
    ),
    create(
        "base_types",
        "uint8_base",
        {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
    ),
    create(
        "implementation_types",
        ELEMENTS_VALUE_TYPE,
        {
            "name": ELEMENTS_VALUE_TYPE,
            "kind": "value_type",
            "base": "uint8_base",
            "compu_method_ref": ELEMENTS_TABLE,
            "description": "Synthetic gear, stored, naming the same table",
        },
    ),
]

#: The application value form, as a step of its own: an interface whose two data elements state an `init` under
#: the elements step's two application types. The writer emits one constant per valued element, which
#: :func:`value_forms` reads back.
VALUE_FORM_INTERFACE = "ISyntheticValues"
VALUE_FORM_OPERATIONS: list[dict[str, Any]] = [
    create(
        "interfaces",
        VALUE_FORM_INTERFACE,
        {
            "name": VALUE_FORM_INTERFACE,
            "kind": "sender_receiver",
            "data_elements": [
                {"name": "demand", "application_type": ELEMENTS_TYPE, "init": 12.5},
                {"name": "gear", "application_type": ELEMENTS_ENUM, "init": "SyntheticDrive"},
            ],
            "description": "The synthetic demand and gear, each with its initial value",
        },
    ),
]

#: Stated names and stub members, as a step of its own: an external client-server interface stub listing one
#: operation, a require port on it, a stated internal behaviour name and a named call of the operation. The writer
#: references the operation as the stub's path plus its name, which :func:`stated_names_and_members` reads back.
STATED_STUB = "ISyntheticDiagnosis"
STATED_STUB_REF = "/Synthetic/PortInterfaces/ISyntheticDiagnosis"
STATED_STUB_OPERATION = "ReadFault"
STATED_BEHAVIOUR = "BrakeCtrlBehaviour"
STATED_CALL = "ReadBrakeFault"
_STATED_CALL_ADDRESS: dict[str, Any] = {
    "layer": "swc",
    "element": "BrakeCtrlSWC",
    "path": [
        {"field": "runnables"},
        {"member": "BrakeCtrlRun"},
        {"field": "calls"},
        {"member": {"port": "rDiag", "operation": STATED_STUB_OPERATION}},
    ],
}
STATED_NAMES_OPERATIONS: list[dict[str, Any]] = [
    create(
        "interfaces",
        STATED_STUB,
        {
            "name": STATED_STUB,
            "kind": "external_interface",
            "external_ref": STATED_STUB_REF,
            "external_dest": "CLIENT-SERVER-INTERFACE",
            "operations": [STATED_STUB_OPERATION],
            "description": "The fault reading another file of the delivery serves",
        },
    ),
    {
        "operation": "create",
        "address": {"layer": "swc", "element": "BrakeCtrlSWC", "path": [{"field": "ports"}, {"member": "rDiag"}]},
        "value": {"name": "rDiag", "direction": "require", "interface": STATED_STUB},
    },
    {
        "operation": "update",
        "address": {"layer": "swc", "element": "BrakeCtrlSWC", "path": [{"field": "behavior_name"}]},
        "value": STATED_BEHAVIOUR,
    },
    {
        "operation": "create",
        "address": _STATED_CALL_ADDRESS,
        "value": {"port": "rDiag", "operation": STATED_STUB_OPERATION, "name": STATED_CALL},
    },
]

#: The namespace of both AUTOSAR schema versions the writer targets.
_AUTOSAR_NAMESPACE = {"a": "http://autosar.org/schema/r4.0"}


def value_forms(arxml: bytes) -> dict[str, list[str | None]]:
    """The value form of every constant the value form step's interface generated.

    Flattened to plain strings for the JSON report: the value specification's tag, its `CATEGORY`, its `UNIT-REF`
    (`None` where it states none), and the one `V` or `VT` of `SW-VALUES-PHYS` as its tag and text. A value of another
    form yields its tag and four `None`s.
    """
    root = etree.fromstring(arxml)
    forms: dict[str, list[str | None]] = {}
    for constant in root.iterfind(".//a:CONSTANT-SPECIFICATION", _AUTOSAR_NAMESPACE):
        name = constant.findtext("a:SHORT-NAME", namespaces=_AUTOSAR_NAMESPACE) or ""
        if not name.startswith(f"{VALUE_FORM_INTERFACE}_"):
            continue
        (value,) = constant.find("a:VALUE-SPEC", _AUTOSAR_NAMESPACE)
        leaves = value.findall("a:SW-VALUE-CONT/a:SW-VALUES-PHYS/*", _AUTOSAR_NAMESPACE)
        forms[name] = [
            etree.QName(value).localname,
            value.findtext("a:CATEGORY", namespaces=_AUTOSAR_NAMESPACE),
            value.findtext("a:SW-VALUE-CONT/a:UNIT-REF", namespaces=_AUTOSAR_NAMESPACE),
            *([etree.QName(leaves[0]).localname, leaves[0].text] if len(leaves) == 1 else [None, None]),
        ]
    return forms


def stated_names_and_members(arxml: bytes) -> dict[str, list[list[str | None]]]:
    """The names and the member path the stated names step's elements were written with.

    Flattened to plain strings: every ``SWC-INTERNAL-BEHAVIOR``'s ``SHORT-NAME``, every synchronous server call
    point's ``SHORT-NAME`` with its ``TARGET-REQUIRED-OPERATION-REF``, and every client com-spec's ``OPERATION-REF``.
    """
    root = etree.fromstring(arxml)
    namespaces = _AUTOSAR_NAMESPACE
    return {
        "behaviours": sorted(
            [node.findtext("a:SHORT-NAME", namespaces=namespaces)]
            for node in root.iterfind(".//a:SWC-INTERNAL-BEHAVIOR", namespaces)
        ),
        "call_points": sorted(
            [
                node.findtext("a:SHORT-NAME", namespaces=namespaces),
                node.findtext(".//a:TARGET-REQUIRED-OPERATION-REF", namespaces=namespaces),
            ]
            for node in root.iterfind(".//a:SYNCHRONOUS-SERVER-CALL-POINT", namespaces)
        ),
        "client_com_specs": sorted(
            [node.findtext("a:OPERATION-REF", namespaces=namespaces)]
            for node in root.iterfind(".//a:CLIENT-COM-SPEC", namespaces)
        ),
    }


#: The nested correction: a field inside a member of a collection inside an element (`field -> member -> field`).
NESTED_UPDATE = {
    "operation": "update",
    "address": {
        "layer": "swc",
        "element": "BrakeCtrlSWC",
        "path": [{"field": "ports"}, {"member": "rSpeed"}, {"field": "description"}],
    },
    "value": "Receives the measured vehicle speed",
}

#: The approved removal, with the reference cleanup that keeps the staged result
#: valid: the port that references the interface, the interface, and the value type
#: only that interface used. Split across three layers, in one transaction.
REMOVAL = [
    {
        "operation": "remove",
        "address": {"layer": "swc", "element": "BrakeCtrlSWC", "path": [{"field": "ports"}, {"member": "pLoad"}]},
    },
    {"operation": "remove", "address": {"layer": "interfaces", "element": "IBrakeLoad"}},
    {"operation": "remove", "address": {"layer": "implementation_types", "element": "Percent"}},
]

#: What the removal takes out, by identity. Every observation of the run is
#: checked against this set and against :data:`SURVIVORS`.
REMOVED_IDENTITIES = ("IBrakeLoad", "Percent", "pLoad")

#: What the run leaves behind.
SURVIVORS = (
    "float32",
    "IVehicleSpeed",
    "SpeedSensorSWC",
    "BrakeCtrlSWC",
    "Speed_to_BrakeCtrl",
    "REQ_BRAKE_001",
    "Task_20ms",
    "task20ms_brake_cycle",
    # The scaffolded root composition the run placed both components in.
    "Root",
    SAME_NAME_PAIR,
    "SyntheticTorqueMapping",
    "ISyntheticTorque",
    ELEMENTS_UNIT,
    ELEMENTS_LINEAR,
    ELEMENTS_TYPE,
    ELEMENTS_TABLE,
    ELEMENTS_ENUM,
    ELEMENTS_VALUE_TYPE,
    VALUE_FORM_INTERFACE,
    STATED_STUB,
    STATED_BEHAVIOUR,
    STATED_CALL,
)


# ---------------------------------------------------------------------------
# One run of the whole scenario, and what it observed
# ---------------------------------------------------------------------------


@dataclass
class ScenarioRun:
    """Everything one greenfield run observed, captured as it happened.

    Captured rather than re-derived: the negative half later appends its own `change_set_rejected` records to the
    same project, so re-reading the log would depend on test order.
    """

    project: Path
    first_baseline: str = ""
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    reads: dict[str, Any] = field(default_factory=dict)
    questions: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)
    model: dict[str, bytes] = field(default_factory=dict)
    artifacts: dict[str, bytes] = field(default_factory=dict)
    value_forms: dict[str, list[str | None]] = field(default_factory=dict)
    stated_names: dict[str, list[list[str | None]]] = field(default_factory=dict)


def model_tree(project: Path) -> dict[str, bytes]:
    """Every file under `model/`, keyed by its model-root-relative path.

    Bytes, not text: a decoded comparison would hide a line-ending or encoding change.
    """
    root = project / "model"
    return {
        str(path.relative_to(root).as_posix()): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def audit_records(project: Path) -> list[dict[str, Any]]:
    log = project / "dist" / "audit.jsonl"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_scenario(root: Path) -> ScenarioRun:
    """The whole positive flow, driven only through `tools/call`.

    `discover` -> one atomic creating change set -> three reads -> the bounded reads (`where`, `limit`/`after`,
    `members`) and `discover` at `form: "full"` -> the same-name pair, the data description elements, the value
    forms and the stated names, each committed and read back -> a nested update -> the approved removal ->
    `validate` -> `build`, whose ARXML :func:`value_forms` and :func:`stated_names_and_members` read back. Every
    model mutation is a `change_set` call.

    **What this scenario does not reach**: it exercises each bounded-read key once, so it witnesses that the key
    reached the installed wheel and was answered -- never the width of the answer, the refusal classes
    (`unknown-field`, `page-cursor`, `members` at an element address) or the budget.
    """
    run = ScenarioRun(project=scaffold(root))
    serve(run.project)

    def protocol(name: str, human: ScriptedHuman | None = None, **arguments: Any) -> Any:
        run.tool_calls.append(name)
        if human is None:
            return call(name, **arguments).structured
        structured = call_reviewed(human, name, **arguments).structured
        run.questions.extend(human.questions)
        return structured

    # `summary` by name: the server's own default is `baseline`, and the scenario reads the contract off this answer.
    discovered = protocol("discover", form="summary")
    run.responses["discover"] = discovered
    run.first_baseline = discovered["baseline"]

    seeded = protocol(
        "change_set",
        ScriptedHuman(True),
        baseline=discovered["baseline"],
        operations=SEED_OPERATIONS,
        intent="Create the brake control baseline across eight model layers.",
    )
    run.responses["seed"] = seeded
    assert seeded["status"] == "committed", seeded

    # A whole layer, one element, and one field through a nested address. `form` is named on the layer read: a
    # layer address answers a `summary` by default.
    run.reads["layer"] = protocol("read", address={"layer": "implementation_types"}, form="full")
    run.reads["element"] = protocol("read", address={"layer": "tasks", "element": "Task_20ms"})
    run.reads["field"] = protocol(
        "read",
        address={
            "layer": "swc",
            "element": "BrakeCtrlSWC",
            "path": [{"field": "ports"}, {"member": "rSpeed"}, {"field": "interface"}],
        },
    )

    # The bounded-read keys, here because only these drivers run against the installed distributions: a key in
    # `server.py`'s signature but not the wheel would pass every fast-gate suite.
    #
    # `where` carries both clause shapes, AND-composed, each dropping an element the other keeps: `Percent`
    # satisfies both, `float32`'s `unit` defaults to `""` (counted as none) and fails `exists`, and the reference
    # type `SyntheticRef` fails both.
    run.reads["filtered"] = protocol(
        "read",
        address={"layer": "implementation_types"},
        where=[{"field": "kind", "equals": "value_type"}, {"field": "unit", "exists": True}],
        form="index",
    )
    # The page in two halves, since a cursor never fed back proves only that a number was printed. Kind rank, then
    # ordinal identity order ('P' < 'f'), puts `Percent` and `float32` before `SyntheticRef`.
    run.reads["paged"] = protocol("read", address={"layer": "implementation_types"}, limit=2, form="index")
    run.reads["paged_next"] = protocol(
        "read",
        address={"layer": "implementation_types"},
        limit=2,
        after=run.reads["paged"]["page"]["next"],
        form="index",
    )
    # The layer-wide member slice: three ports over two components, which no element-address read answers at once.
    run.reads["members"] = protocol("read", address={"layer": "swc"}, members="ports")

    # `discover`'s form: `summary` strips every descriptor `description`; asked for by name, the prose comes back.
    # The project-wide `full` is always an addressless `over-budget` refusal, so the prose is asked of one layer.
    run.responses["discover_full"] = protocol("discover", form="full")
    run.responses["discover_layer_full"] = protocol("discover", layer="swc", form="full")

    # Each layer answers its own element under the shared name. After the bounded reads, so they see the seed alone.
    paired = protocol(
        "change_set",
        ScriptedHuman(True),
        baseline=seeded["baseline"],
        operations=SAME_NAME_PAIR_OPERATIONS,
        intent="Declare the synthetic torque's two views under one name, pair them and carry each on the bus.",
    )
    run.responses["pair"] = paired
    assert paired["status"] == "committed", paired
    for layer in ("application_types", "implementation_types"):
        run.reads[f"pair_{layer}"] = protocol("read", address={"layer": layer, "element": SAME_NAME_PAIR})

    elements = protocol(
        "change_set",
        ScriptedHuman(True),
        baseline=paired["baseline"],
        operations=ELEMENTS_OPERATIONS,
        intent="Declare the synthetic demand's unit and scale and the synthetic gear's table as elements.",
    )
    run.responses["elements"] = elements
    assert elements["status"] == "committed", elements
    for layer in ("units", "compu_methods"):
        run.reads[f"elements_{layer}"] = protocol("read", address={"layer": layer})
    for element in (ELEMENTS_TYPE, ELEMENTS_ENUM):
        run.reads[f"elements_{element}"] = protocol("read", address={"layer": "application_types", "element": element})

    values = protocol(
        "change_set",
        ScriptedHuman(True),
        baseline=elements["baseline"],
        operations=VALUE_FORM_OPERATIONS,
        intent="State the synthetic demand's and gear's initial values.",
    )
    run.responses["values"] = values
    assert values["status"] == "committed", values
    # `full` by name: a data element's summary leaves its `init` out.
    run.reads["values"] = protocol(
        "read",
        address={"layer": "interfaces", "element": VALUE_FORM_INTERFACE, "path": [{"field": "data_elements"}]},
        form="full",
    )

    named = protocol(
        "change_set",
        ScriptedHuman(True),
        baseline=values["baseline"],
        operations=STATED_NAMES_OPERATIONS,
        intent="Call the fault reading another file serves, and name the brake controller's behaviour and the call.",
    )
    run.responses["names"] = named
    assert named["status"] == "committed", named
    run.reads["names_stub"] = protocol(
        "read", address={"layer": "interfaces", "element": STATED_STUB, "path": [{"field": "operations"}]}
    )
    run.reads["names_behaviour"] = protocol(
        "read", address={"layer": "swc", "element": "BrakeCtrlSWC", "path": [{"field": "behavior_name"}]}
    )
    run.reads["names_call"] = protocol("read", address=_STATED_CALL_ADDRESS, form="full")

    corrected = protocol(
        "change_set",
        ScriptedHuman(True),
        baseline=named["baseline"],
        operations=[NESTED_UPDATE],
        intent="Describe the speed input port.",
    )
    run.responses["update"] = corrected
    assert corrected["status"] == "committed", corrected

    removed = protocol(
        "change_set",
        ScriptedHuman(True),
        baseline=corrected["baseline"],
        operations=REMOVAL,
        intent="Drop the brake load interface and everything that referenced it.",
    )
    run.responses["remove"] = removed
    assert removed["status"] == "committed", removed

    run.responses["validate"] = protocol("validate")
    run.responses["build"] = protocol("build")

    run.audit = audit_records(run.project)
    run.model = model_tree(run.project)
    run.artifacts = {name: (run.project / name).read_bytes() for name in ARTIFACTS}
    run.value_forms = value_forms(run.artifacts["dist/model.arxml"])
    run.stated_names = stated_names_and_members(run.artifacts["dist/model.arxml"])
    return run


#: The only tools the scenario may call; `validate` and `build` write no model file.
ALLOWED_TOOLS = frozenset({"discover", "read", "change_set", "validate", "build"})


# ---------------------------------------------------------------------------
# The seven invalid-class change sets
# ---------------------------------------------------------------------------


#: The seven invalid-class refusals, each with the operations that produce
#: it against the committed greenfield model.
INVALID_CHANGE_SETS: dict[str, list[dict[str, Any]]] = {
    # A value `schema.py` refuses: `wcet` is a whole number and a unit, and positive.
    "schema-violation": [
        {
            "operation": "update",
            "address": {"layer": "tasks", "element": "Task_20ms", "path": [{"field": "wcet"}]},
            "value": "-1us",
        }
    ],
    # A field the registry does not declare.
    "unknown-field": [
        {
            "operation": "update",
            "address": {"layer": "swc", "element": "BrakeCtrlSWC", "path": [{"field": "safety_level"}]},
            "value": "ASIL-B",
        }
    ],
    # A legal declared name whose *emitted* `{name}_CompuMethod` exceeds the AUTOSAR `Identifier` length: the rule
    # is over what is written, not over what was typed.
    "invalid-identifier": [
        create(
            "application_types",
            "E" + "x" * 116,
            {
                "name": "E" + "x" * 116,
                "kind": "enum",
                "literals": [{"name": "Off", "value": 0}, {"name": "On", "value": 1}],
            },
        )
    ],
    # The outcome would dangle: `Speed_to_BrakeCtrl` names the port.
    "broken-reference": [
        {
            "operation": "remove",
            "address": {
                "layer": "swc",
                "element": "SpeedSensorSWC",
                "path": [{"field": "ports"}, {"member": "pSpeed"}],
            },
        }
    ],
    # An identity the model already declares.
    "identity-conflict": [
        create(
            "implementation_types",
            "float32",
            {"name": "float32", "kind": "value_type", "base": "float32_base"},
        )
    ],
    # A member's identity is immutable: a rename by update would silently be a remove plus a create.
    "immutable-field": [
        {
            "operation": "update",
            "address": {
                "layer": "swc",
                "element": "BrakeCtrlSWC",
                "path": [{"field": "runnables"}, {"member": "BrakeCtrlRun"}, {"field": "name"}],
            },
            "value": "RenamedRun",
        }
    ],
    # A cross-layer rule, not a schema one: a second `root: true` composition (`composition.multiple-roots`).
    "consistency-violation": [
        create(
            "compositions",
            "Root2",
            {"name": "Root2", "title": "A second root", "root": True},
        )
    ],
}


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


def refused(project: Path, operations: list[dict[str, Any]], *, baseline: str | None = None) -> dict[str, Any]:
    """Submit *operations* under an **approving** human and assert byte identity.

    Without an elicitation channel the submission would be abandoned and the model unchanged whether or not the
    refusal worked; with an approving human only the refusal stands between the operations and the disk. A refused
    change set never reaches review, so no question is asked and the "yes" goes unused. The whole model tree is
    compared, so a refusal that touched an unrelated file also fails.
    """
    before = model_tree(project)
    human = ScriptedHuman(True)
    token = baseline if baseline is not None else call("discover").structured["baseline"]
    structured, _texts = call_reviewed(
        human, "change_set", baseline=token, operations=operations, intent="A change set that must not land."
    )
    assert model_tree(project) == before, "a refused change set must leave the real model byte-identical"
    assert human.questions == [], "a refused change set must never reach the review boundary"
    assert human.unanswered == [True], "the approving answer must have gone unused"
    return structured


#: The address the host approved for the `unauthorized-delta` case, on `SWCRAFT_APPROVED_SCOPE`, a channel no tool
#: argument can reach.
UNAUTHORIZED_SCOPE = [
    {
        "operation": "update",
        "address": {"layer": "swc", "element": "SpeedSensorSWC", "path": [{"field": "description"}]},
    }
]

#: The address the change set names instead.
UNAUTHORIZED_ADDRESS = {"layer": "swc", "element": "BrakeCtrlSWC", "path": [{"field": "description"}]}

UNAUTHORIZED_OPERATIONS: list[dict[str, Any]] = [
    {"operation": "update", "address": UNAUTHORIZED_ADDRESS, "value": "Out of the approved scope"}
]

#: Submitted against :attr:`ScenarioRun.first_baseline` — the token the run
#: started from, six commits later.
STALE_OPERATIONS: list[dict[str, Any]] = [
    {
        "operation": "update",
        "address": {"layer": "swc", "element": "BrakeCtrlSWC", "path": [{"field": "description"}]},
        "value": "Stated against a model state that is six commits old",
    }
]

#: Valid as a whole, inapplicable in the order given. Not a `broken-reference`: the outcome would be consistent, so
#: applying it would need a partial or re-ordered apply, either committing a sequence nobody reviewed. The
#: requirement keeps the outcome warning-free, so the order refusal is the only thing between these operations and
#: the disk; :data:`PARTIAL_OPERATIONS_REORDERED` is the positive control.
PARTIAL_OPERATIONS: list[dict[str, Any]] = [
    {
        "operation": "update",
        "address": {"layer": "swc", "element": "LateSWC", "path": [{"field": "description"}]},
        "value": "Described before it exists",
    },
    create(
        "swc",
        "LateSWC",
        {
            "name": "LateSWC",
            "group": "control",
            "type": "application",
            "ports": [],
            "runnables": [],
            "handle_termination_and_restart": "no_support",
            "satisfies": ["REQ_LATE_001@1"],
        },
    ),
    create(
        "requirements",
        "REQ_LATE_001",
        {
            "id": "REQ_LATE_001",
            "title": "The late component shall be traced to a requirement",
            "description": "Traces LateSWC, so the outcome of this change set is a consistent model.",
            "req_type": "req",
            "version": 1,
            "source": "architecture",
        },
    ),
]

#: The same operations in an order that applies: the positive control, which commits, so the refusal above is
#: load-bearing. Applied to a copy of the run's project.
PARTIAL_OPERATIONS_REORDERED: list[dict[str, Any]] = [
    PARTIAL_OPERATIONS[1],
    PARTIAL_OPERATIONS[2],
    PARTIAL_OPERATIONS[0],
]

#: The refusal classes of the closed enum that are not *invalid*: the enum is
#: ``set(INVALID_CHANGE_SETS) | NON_INVALID_REFUSALS``.
NON_INVALID_REFUSALS = ("unauthorized-delta", "stale-baseline", "partial-change-set")


def replay_refusals(run: ScenarioRun) -> dict[str, str]:
    """Submit every refusal case against *run*'s committed model, in one pass.

    Returns:
        The error code of each case, by case name; :func:`refused` has asserted byte identity for each.
    """
    codes: dict[str, str] = {}
    for name, operations in sorted(INVALID_CHANGE_SETS.items()):
        serve(run.project, review="elicit")
        codes[name] = refused(run.project, operations)["error"]

    serve(run.project, scope=UNAUTHORIZED_SCOPE, scope_id="greenfield-turn")
    codes["unauthorized-delta"] = refused(run.project, UNAUTHORIZED_OPERATIONS)["error"]

    serve(run.project)
    codes["stale-baseline"] = refused(run.project, STALE_OPERATIONS, baseline=run.first_baseline)["error"]

    serve(run.project)
    codes["partial-change-set"] = refused(run.project, PARTIAL_OPERATIONS)["error"]
    return codes
