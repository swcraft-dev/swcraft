"""Fixture scaffolding for the editing-protocol acceptance vectors.

``packages/tools/tests/test_editing_vectors.py`` executes the shipped vectors against the editing engine. A vector
states its baseline in ``context`` and the request/response pair in ``input``/``expected``, but ships no model. This
module builds that model, checks that it satisfies what the vector claims, and defines how to compare the values an
implementation may produce differently (the baseline token and the change-set id).

* **One base model plus a per-vector seed.** The base is ``examples/basic``; :data:`SEEDS` is the one table of
  overlays, keyed by the vector's ``name``, because the vectors state mutually incompatible starting states.
* **The contract JSON is never modified.** Every seed edit satisfies the vector's ``context`` or makes an accepted
  change set's outcome valid under :func:`swcraft_tools.validator.check_cross_layer_consistency`; each
  :class:`SeedEdit` records its rule.
* **The baseline need not be valid**: :attr:`Seed.baseline_valid` records the two seeds that are not, and a harness
  test holds every other seed to a valid baseline.

The address resolver is its own YAML-level walk rather than a call into ``editing/``, so the precondition check is
not vacuous when the engine's resolution is wrong.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from conftest import REPO_ROOT
from jsonschema import Draft202012Validator
from referencing import Registry as SchemaRegistry
from referencing import Resource as SchemaResource
from test_editing_protocol_contracts import (
    ALL_VECTOR_FILES,
    PROTOCOL_PATH,
    _addresses_a_multiset_member,
    _hashable,
    _load,
)

# ---------------------------------------------------------------------------
# The base model and the layer vocabulary the harness needs to walk it.
# ---------------------------------------------------------------------------

#: The base every fixture starts from: the ``examples/basic`` model, copied.
BASE_MODEL_ROOT = REPO_ROOT / "examples" / "basic" / "model"

#: :func:`model_state_key` of that base, **pinned**. ``examples/basic`` is a
#: demo project an unrelated change may legitimately edit, and several vectors
#: state its exact contents. The digest turns "the acceptance suite quietly
#: started asserting something else" into one failing test. It moves with any
#: file of the base's model tree; re-pinning it is deliberate work: re-read and
#: re-record from the live engine the vectors whose ``context`` or ``expected``
#: depends on the base before changing this value.
BASE_MODEL_DIGEST = "855500df4676915fa37e9780c86fef3f997b6e622d9f4b9eefe14a2a0e2afde3"

#: The base manifest, copied beside it. A vector that states a different project
#: name in its expected passport overrides only that key (`Seed.project_name`).
BASE_MANIFEST = REPO_ROOT / "examples" / "basic" / "swcraft.yaml"

#: The project name ``examples/basic`` declares, and therefore the passport name
#: every vector gets unless its seed says otherwise.
BASE_PROJECT_NAME = "swcraft-basic-example"

#: Layer -> its storage directory under ``model/``, and layer -> its identity
#: field. Harness-local on purpose: the registry's own copy is what a parity gate
#: compares against the production tables, and a fixture builder that read the
#: registry could not tell a broken registry from a broken fixture.
LAYER_DIRECTORIES: dict[str, str] = {
    "swc": "swc",
    "interfaces": "interfaces",
    "connectors": "connectors",
    # The data description layers are stored one directory below `data_types/`,
    # which holds no element file of its own; the walk globs a flat layer's own
    # directory only, so no layer reads another's files.
    "application_types": "data_types/application",
    "implementation_types": "data_types/implementation",
    "base_types": "data_types/base",
    "requirements": "requirements",
    "compositions": "compositions",
    "tasks": "tasks",
    "sequences": "sequences",
    "hardware": "hardware",
    "mapping_sets": "data_types/mapping_sets",
    "units": "data_types/units",
    "compu_methods": "data_types/compu_methods",
    "data_constraints": "data_types/constraints",
}

#: ``requirements`` is identified by ``id``; every other layer by ``name``.
LAYER_IDENTITY_FIELDS: dict[str, str] = dict.fromkeys(LAYER_DIRECTORIES, "name") | {"requirements": "id"}

#: Collection field -> the identity field of its members, for the ``field``
#: member-identity strategy alone, and listing only the collections whose
#: answer is not ``name``. Harness-local for ``LAYER_IDENTITY_FIELDS``' reason.
#: ``maps``' members carry no ``name``: a pair is identified by the application
#: type it maps. What it does not reach: two collections of one name in two
#: layers whose members are identified differently; the table is keyed by the
#: field name alone, which the shipped contract makes unambiguous and nothing
#: here checks.
MEMBER_IDENTITY_FIELDS: dict[str, str] = {"maps": "application"}

#: The one layer stored one directory deeper than its layer root, on the
#: ``group`` placement field.
NESTED_LAYERS = frozenset({"swc"})

#: ``$defs/revisionToken``'s pattern, and the change-set id form. Both are *structural*
#: comparisons: no implementation can reproduce a vector's stand-in token, so the
#: harness checks the shape and the relations instead of the bytes.
REVISION_TOKEN_PATTERN = re.compile(r"^rev[0-9]+:[A-Za-z0-9_-]{8,512}$")
AUDIT_REF_PATTERN = re.compile(r"^cs-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")


# ---------------------------------------------------------------------------
# The seed table.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedEdit:
    """One overlay on the base model, with the rule that motivates it.

    Args:
        path: model-root-relative path (``data_types/implementation/VehicleSpeed_kph.yaml``, or
            a layer directory when the whole layer is being removed).
        reason: what in the vector — or in ``check_cross_layer_consistency`` —
            requires this edit. Never "because the test needs it".
        document: the YAML document to write, or ``None`` to delete *path*.
    """

    path: str
    reason: str
    document: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Seed:
    """The whole overlay for one vector, keyed by the vector's stable ``name``."""

    reason: str
    edits: tuple[SeedEdit, ...] = ()
    absent_layers: tuple[str, ...] = ()
    project_name: str = BASE_PROJECT_NAME
    baseline_valid: bool = True
    #: Only set where the baseline is deliberately inconsistent; explains why
    #: that is the accepted case rather than a fixture defect.
    baseline_note: str = ""
    #: Set where the seed keeps an element file in a retired directory. Such a
    #: model is not read at all: every request, ``discover`` included, is refused
    #: before it reaches an element, so the fixture has no baseline token
    #: (:func:`baseline_token`) and the consistency check refuses it.
    retired_layout: bool = False


# --- documents reused by more than one seed -------------------------------

VEHICLE_SPEED_KPH = {
    "name": "VehicleSpeed_kph",
    "kind": "value_type",
    "base": "float32_base",
    "unit": "kph",
    "value_min": 0.0,
    "value_max": 300.0,
    "description": "Vehicle speed in kilometres per hour",
}

#: A second core for the vectors that need one; the budget is the base's own,
#: so nothing about the arithmetic changes.
CORE1_SEEDED = {
    "name": "CORE1",
    "kind": "core",
    "threshold": 0.7,
    "enforcement": "strict",
    "description": "Application core 1",
}

#: The base's own ``CORE0`` and ``RAM0``, field for field, for the vectors that
#: rate one of them or widen what the memory admits. Spelled out rather than
#: read from ``examples/basic`` at import, like every document here.
CORE0_BASE = {
    "name": "CORE0",
    "kind": "core",
    "threshold": 0.7,
    "enforcement": "strict",
    "description": "Application core 0 — 70% CPU load budget from the concept document",
}
RAM0_BASE = {
    "name": "RAM0",
    "kind": "memory",
    "type": "RAM",
    "size": 262144,
    "accessible": ["CORE0"],
    "threshold": 0.8,
    "enforcement": "strict",
    "description": "256 KiB application RAM pool — 80% consumption budget",
}
RAM0_ADMITTING_CORE1 = RAM0_BASE | {"accessible": ["CORE0", "CORE1"]}

#: The base's own ``PARTITION0``, field for field, for the vectors that rate it;
#: and an unrated partition on the seeded ``CORE1``, for the vectors that move a
#: task to the second core -- a task takes its core from its partition.
PARTITION0_BASE = {
    "name": "PARTITION0",
    "kind": "partition",
    "core": "CORE0",
    "description": "The one partition on CORE0, without a level; every task of a single-core project runs in one.",
}
#: A partition's memory-mixing argument, for a seed that rates `PARTITION0` above
#: a component it runs and is about another rule.
PARTITION0_ARGUMENT = "The components share the partition's memory region; the argument is recorded in the safety case."
#: The task's own execution-time argument and a second partition's memory-mixing
#: argument, for the seeds that run `Task_10ms` over two levels:
#: `safety.task-mixed-asil` must be answered for the baseline to be valid, and the
#: partition's argument is a separate statement.
TASK_10MS_ARGUMENT = "The two components are separated in time by the schedule; the argument is in the safety case."
PARTITION1_ARGUMENT = "The partition's components sit in their own memory regions under memory protection."
PARTITION1_SEEDED = {
    "name": "PARTITION1",
    "kind": "partition",
    "core": "CORE1",
    "description": "The partition on the second core, without a level.",
}

#: A minimal ``IVehicleSpeed`` for the vectors that state the interface as
#: already present; the vector that creates it carries its own payload.
IVEHICLE_SPEED_SEEDED = {
    "name": "IVehicleSpeed",
    "kind": "sender_receiver",
    "data_elements": [{"name": "speed_kph", "implementation_type": "float32"}],
    "description": "Vehicle speed",
}

#: The physical view of the vehicle speed signal, an application type. Named
#: `VehicleSpeed_Kph` with a capital `K` so that nothing collides with
#: `VEHICLE_SPEED_KPH` above, the implementation type of the same signal that
#: `create-data-type-alias` authors.
VEHICLE_SPEED_KPH_PHYSICAL = {
    "name": "VehicleSpeed_Kph",
    "kind": "application_type",
    "unit": "kph",
    "conversion": {"factor": 0.1, "offset": 0.0},
    "range": {"min": 0.0, "max": 300.0},
    "description": "Vehicle speed, physical view",
}

#: A second application type, for the pair a change set adds. It declares no
#: `conversion`, which is legal: the three physics fields are independent.
ENGINE_SPEED_RPM = {
    "name": "EngineSpeed_Rpm",
    "kind": "application_type",
    "unit": "rpm",
    "range": {"min": 0.0, "max": 8000.0},
    "description": "Engine speed, physical view",
}

#: The set that pairs the physical view with the implementation type storing it.
PHYSICAL_MAPPING = {
    "name": "PhysicalMapping",
    "maps": [{"application": "VehicleSpeed_Kph", "implementation": "float32"}],
    "description": "The implementation types that store the physical ones",
}

#: A set with no pairs. Legal — a set is a container — and the element the
#: `remove` verb is shown on, because it has no referrer to clear.
LEGACY_MAPPING = {
    "name": "LegacyMapping",
    "maps": [],
    "description": "Declared before the pairs were known",
}

#: One name declared in two layers. Identity is `(layer, name)`, so an
#: application type and an implementation type may share a name. Each states
#: bounds inline, so each generates a `SyntheticTorque_Range` of its own, into the
#: package of its own layer.
SYNTHETIC_TORQUE_APPLICATION = {
    "name": "SyntheticTorque",
    "kind": "application_type",
    "unit": "Nm",
    "range": {"min": -500.0, "max": 500.0},
    "description": "Synthetic torque, physical view",
}
SYNTHETIC_TORQUE_IMPLEMENTATION = {
    "name": "SyntheticTorque",
    "kind": "value_type",
    "base": "float32_base",
    "value_min": -500.0,
    "value_max": 500.0,
    "description": "Synthetic torque, stored",
}
#: The map between the two: both sides spell one name, and each side is
#: resolved in the layer its field targets.
SYNTHETIC_TORQUE_MAPPING = {
    "name": "SyntheticTorqueMapping",
    "maps": [{"application": "SyntheticTorque", "implementation": "SyntheticTorque"}],
    "description": "Pairs the two views of the synthetic torque, which share one name",
}

#: The pair and its map, seeded by the four layer reads of one model.
_SAME_NAME_PAIR_EDITS = (
    SeedEdit(
        path="data_types/application/SyntheticTorque.yaml",
        document=SYNTHETIC_TORQUE_APPLICATION,
        reason="`context.existing_elements` states application_types/SyntheticTorque, the pair's physical view.",
    ),
    SeedEdit(
        path="data_types/implementation/SyntheticTorque.yaml",
        document=SYNTHETIC_TORQUE_IMPLEMENTATION,
        reason=(
            "`context.existing_elements` states implementation_types/SyntheticTorque, the pair's stored view under "
            "the same name."
        ),
    ),
    SeedEdit(
        path="data_types/mapping_sets/SyntheticTorqueMapping.yaml",
        document=SYNTHETIC_TORQUE_MAPPING,
        reason="`context.existing_elements` states mapping_sets/SyntheticTorqueMapping, the map between the two.",
    ),
)

#: The data description elements: a unit, the two compu method kinds and a data
#: constraint, each in its own layer under `data_types/`, and the three types that
#: name them by reference — an application type naming the unit, the linear method
#: and the constraint; an enumeration and a value type sharing one texttable.
PROBE_UNIT = {"name": "ProbeNewtonMetre", "display_name": "Nm", "description": "Probe torque unit"}
PROBE_LINEAR = {
    "name": "ProbeTorqueScale",
    "kind": "linear",
    "unit": "ProbeNewtonMetre",
    "factor": 0.5,
    "offset": -100.0,
    "description": "Probe torque scale: physical = internal x 0.5 - 100",
}
PROBE_TABLE = {
    "name": "ProbeGearTable",
    "kind": "texttable",
    "entries": [
        {"name": "ProbePark", "value": 0, "description": "Probe gear parked"},
        {"name": "ProbeDrive", "value": 1, "description": "Probe gear driving"},
    ],
    "description": "Probe gear values",
}
PROBE_LIMITS = {
    "name": "ProbeTorqueLimits",
    "min": -500.0,
    "max": 500.0,
    "level": 0,
    "description": "Probe torque limits",
}
PROBE_TORQUE = {
    "name": "ProbeTorque",
    "kind": "application_type",
    "unit_ref": "ProbeNewtonMetre",
    "compu_method_ref": "ProbeTorqueScale",
    "data_constr_ref": "ProbeTorqueLimits",
    "description": "Probe torque, every property by reference",
}
PROBE_GEAR = {
    "name": "ProbeGear",
    "kind": "enum",
    "compu_method_ref": "ProbeGearTable",
    "description": "Probe gear, its values in a shared table",
}
PROBE_GEAR_RAW = {
    "name": "ProbeGearRaw",
    "kind": "value_type",
    "base": "uint8_base",
    "compu_method_ref": "ProbeGearTable",
    "description": "Probe gear, stored, naming the same table",
}

#: The seven elements, seeded by the data description element reads and the removal.
_DATA_DESCRIPTION_ELEMENT_EDITS = (
    SeedEdit(
        path="data_types/units/ProbeNewtonMetre.yaml",
        document=PROBE_UNIT,
        reason="`context.existing_elements` states units/ProbeNewtonMetre, the unit two elements name.",
    ),
    SeedEdit(
        path="data_types/compu_methods/ProbeTorqueScale.yaml",
        document=PROBE_LINEAR,
        reason="`context.existing_elements` states compu_methods/ProbeTorqueScale, a linear method naming the unit.",
    ),
    SeedEdit(
        path="data_types/compu_methods/ProbeGearTable.yaml",
        document=PROBE_TABLE,
        reason="`context.existing_elements` states compu_methods/ProbeGearTable, the table two types share.",
    ),
    SeedEdit(
        path="data_types/constraints/ProbeTorqueLimits.yaml",
        document=PROBE_LIMITS,
        reason="`context.existing_elements` states data_constraints/ProbeTorqueLimits, the limits the type names.",
    ),
    SeedEdit(
        path="data_types/application/ProbeTorque.yaml",
        document=PROBE_TORQUE,
        reason="`context.existing_elements` states application_types/ProbeTorque, naming the unit, method and limits.",
    ),
    SeedEdit(
        path="data_types/application/ProbeGear.yaml",
        document=PROBE_GEAR,
        reason="`context.existing_elements` states application_types/ProbeGear, an enumeration naming the table.",
    ),
    SeedEdit(
        path="data_types/implementation/ProbeGearRaw.yaml",
        document=PROBE_GEAR_RAW,
        reason="`context.existing_elements` states implementation_types/ProbeGearRaw, a value type naming the table.",
    ),
)

#: The link a seeded component carries to the base's one requirement: the link
#: lives in the component's own document, so a seed that adds or replaces an SWC
#: writes it there, as the base's own files do.
EXAMPLE_LINK = "REQ_EXAMPLE_001@1"


#: An interface whose one data element is typed by the application type, so that
#: a port on it makes its component speak a physical type — the condition
#: `data-type.application-type-unmapped` reads.
IPHYSICAL_SPEED = {
    "name": "IPhysicalSpeed",
    "kind": "sender_receiver",
    "data_elements": [{"name": "speed", "application_type": "VehicleSpeed_Kph"}],
    "description": "Vehicle speed, physical view",
}

#: An implementation data type another file of the delivery declares, carried
#: as a stub — its source path and its `DEST`, nothing of its content.
FOREIGN_SPEED_TYPE_STUB = {
    "name": "ForeignSpeed_T",
    "kind": "external_type",
    "external_ref": "/Foreign/ImplementationDataTypes/ForeignSpeed_T",
    "external_dest": "IMPLEMENTATION-DATA-TYPE",
}

#: The base's own `ExampleSWC` with a second port on `IPhysicalSpeed` and the one
#: `data_type_mappings` entry that covers what that port speaks. Spelled out
#: rather than derived from `_example_swc`, like every document here.
EXAMPLE_SWC_SPEAKING_A_PHYSICAL_TYPE = {
    "name": "ExampleSWC",
    "type": "application",
    "description": "Example software component for testing and demonstration",
    "ports": [
        {
            "name": "pOut",
            "direction": "provide",
            "interface": "IExample",
            "description": "Example output port providing data",
        },
        {
            "name": "pPhysical",
            "direction": "provide",
            "interface": "IPhysicalSpeed",
            "description": "Publishes the physical vehicle speed",
        },
    ],
    "runnables": [
        {
            "name": "ExampleRun",
            "min_start_interval": 0.01,
            "events": [{"type": "timing", "period": 0.01}],
        }
    ],
    "memory": {"RAM0": 2048},
    "handle_termination_and_restart": "no_support",
    "data_type_mappings": ["PhysicalMapping"],
    "satisfies": [EXAMPLE_LINK],
}


#: The member-path seed: the base's sender-receiver traffic written down as
#: runnable accesses, with one consumer behind a delegation and one data element
#: typed by a mapped application type. The provider writes the physical speed
#: from two runnables, so the owner chain (`through`) is what tells two rows of
#: one `(port, element)` pair apart.
EXAMPLE_SWC_WRITING = {
    "name": "ExampleSWC",
    "type": "application",
    "description": "Example software component for testing and demonstration",
    "ports": [
        {
            "name": "pOut",
            "direction": "provide",
            "interface": "IExample",
            "description": "Example output port providing data",
        },
        {
            "name": "pPhysical",
            "direction": "provide",
            "interface": "IPhysicalSpeed",
            "description": "Publishes the physical vehicle speed",
        },
    ],
    "runnables": [
        {
            "name": "ExampleRun",
            "min_start_interval": 0.01,
            "events": [{"type": "timing", "period": 0.01}],
            "writes": [{"port": "pOut", "element": "value"}, {"port": "pPhysical", "element": "speed"}],
        },
        {
            "name": "ExampleSlowRun",
            "min_start_interval": 0.1,
            "events": [{"type": "timing", "period": 0.1}],
            "writes": [{"port": "pPhysical", "element": "speed"}],
        },
    ],
    "memory": {"RAM0": 2048},
    "handle_termination_and_restart": "no_support",
    "data_type_mappings": ["PhysicalMapping"],
    "satisfies": [EXAMPLE_LINK],
}

#: The base's consumer, reading both signals through its own two ports.
EXAMPLE_CONSUMER_SWC_READING = {
    "name": "ExampleConsumerSWC",
    "type": "application",
    "description": "Example software component consuming data from ExampleSWC",
    "ports": [
        {
            "name": "rIn",
            "direction": "require",
            "interface": "IExample",
            "description": "Example input port requiring data",
        },
        {
            "name": "rPhysical",
            "direction": "require",
            "interface": "IPhysicalSpeed",
            "description": "Receives the physical vehicle speed",
        },
    ],
    "runnables": [
        {
            "name": "ExampleConsumerRun",
            "min_start_interval": 0.01,
            "events": [{"type": "timing", "period": 0.01}],
            "reads": [{"port": "rIn", "element": "value"}, {"port": "rPhysical", "element": "speed"}],
        }
    ],
    "memory": {"RAM0": 1536},
    "handle_termination_and_restart": "no_support",
    "data_type_mappings": ["PhysicalMapping"],
    "satisfies": [EXAMPLE_LINK],
}

#: The composition the consumer stands in, one level below the root, with a
#: boundary port per signal delegated to the consumer's own.
CONSUMER_GROUP = {
    "name": "ConsumerGroup",
    "title": "Consumer group",
    "description": "Holds the consumer behind two delegated boundary ports",
    "ports": [
        {"name": "rIn", "direction": "require", "interface": "IExample"},
        {"name": "rPhysical", "direction": "require", "interface": "IPhysicalSpeed"},
    ],
    "members": [{"name": "ExampleConsumerSWC", "type": "ExampleConsumerSWC"}],
    "delegations": [
        {"name": "rIn_to_consumer", "port": "rIn", "member": "ExampleConsumerSWC", "member_port": "rIn"},
        {
            "name": "rPhysical_to_consumer",
            "port": "rPhysical",
            "member": "ExampleConsumerSWC",
            "member_port": "rPhysical",
        },
    ],
}

#: The root, placing the provider and the group.
ROOT_WITH_CONSUMER_GROUP = {
    "name": "Root",
    "title": "Root composition",
    "root": True,
    "members": [
        {"name": "ExampleSWC", "type": "ExampleSWC"},
        {"name": "ConsumerGroup", "composition": "ConsumerGroup"},
    ],
}

#: The two connectors, each from the provider into the group's boundary.
EXAMPLE_TO_CONSUMER_GROUP = {
    "name": "Example_to_Consumer",
    "from": {"swc": "ExampleSWC", "port": "pOut"},
    "to": {"composition": "ConsumerGroup", "port": "rIn"},
    "description": "ExampleSWC provides IExample data to the consumer group",
}
PHYSICAL_TO_CONSUMER_GROUP = {
    "name": "Physical_to_Consumer",
    "from": {"swc": "ExampleSWC", "port": "pPhysical"},
    "to": {"composition": "ConsumerGroup", "port": "rPhysical"},
    "description": "ExampleSWC provides the physical speed to the consumer group",
}

#: The pilot seed's edits, shared by every vector written against it.
_RUNNABLE_ACCESS_EDITS = (
    SeedEdit(
        path="data_types/application/VehicleSpeed_Kph.yaml",
        document=VEHICLE_SPEED_KPH_PHYSICAL,
        reason="`context.existing_elements` states application_types/VehicleSpeed_Kph, the physical speed's type.",
    ),
    SeedEdit(
        path="data_types/mapping_sets/PhysicalMapping.yaml",
        document=PHYSICAL_MAPPING,
        reason=(
            "`context.existing_elements` states mapping_sets/PhysicalMapping. Both components speak the "
            "application type, so `data-type.application-type-unmapped` requires the pair they name."
        ),
    ),
    SeedEdit(
        path="interfaces/IPhysicalSpeed.yaml",
        document=IPHYSICAL_SPEED,
        reason="`context.existing_elements` states interfaces/IPhysicalSpeed, the interface of the second signal.",
    ),
    SeedEdit(
        path="swc/example/ExampleSWC.yaml",
        document=EXAMPLE_SWC_WRITING,
        reason="`context.existing_elements` states the provider's two runnables and the three writes they declare.",
    ),
    SeedEdit(
        path="swc/example/ExampleConsumerSWC.yaml",
        document=EXAMPLE_CONSUMER_SWC_READING,
        reason="`context.existing_elements` states the consumer's second port and the two reads its runnable declares.",
    ),
    SeedEdit(
        path="compositions/ConsumerGroup.yaml",
        document=CONSUMER_GROUP,
        reason="`context.existing_elements` states compositions/ConsumerGroup, the delegation the consumer is behind.",
    ),
    SeedEdit(
        path="compositions/Root.yaml",
        document=ROOT_WITH_CONSUMER_GROUP,
        reason=(
            "`rule:composition.connector-endpoint-not-a-member`: each connector's endpoints must be members of "
            "the root, which now places the group instead of the consumer."
        ),
    ),
    SeedEdit(
        path="connectors/Example_to_Consumer.yaml",
        document=EXAMPLE_TO_CONSUMER_GROUP,
        reason="The base's connector, re-pointed at the group's boundary port: the root no longer places the consumer.",
    ),
    SeedEdit(
        path="connectors/Physical_to_Consumer.yaml",
        document=PHYSICAL_TO_CONSUMER_GROUP,
        reason="`context.existing_elements` states connectors/Physical_to_Consumer, the second signal's connector.",
    ),
)


def _requirement(asil: str | None = None) -> dict[str, Any]:
    """``REQ_EXAMPLE_001``, allocating *asil* when one is given.

    The requirement names no component (each component names it in
    ``satisfies``), so a seed rewrites this file only to allocate a level.
    """
    document: dict[str, Any] = {
        "id": "REQ_EXAMPLE_001",
        "title": "Example requirement for ExampleSWC",
        "description": "The system shall provide example functionality via ExampleSWC.",
        "req_type": "req",
        "version": 1,
        "source": "architecture",
    }
    if asil is not None:
        document["asil"] = asil
    return document


def _root_composition_document(members: Sequence[str]) -> dict[str, Any]:
    """The base's root composition with *members* replaced, each named after its type.

    ``rule:composition.connector-endpoint-not-a-member`` requires every connector
    endpoint to name a type the composition places, so a seed that adds an SWC a
    connector names has to place it here too.
    """
    return {
        "name": "Root",
        "title": "Root composition",
        "root": True,
        "members": [{"name": name, "type": name} for name in members],
    }


#: How many ports ``WideSWC`` carries. Chosen by measurement, not by taste: the
#: ``swc`` layer's ``full`` publication has to exceed
#: ``read.RESPONSE_BUDGET_WIRE_BYTES`` (80 000 **wire** bytes) while its
#: ``summary`` and its ``index`` stay far under it, so that
#: ``negative/read-layer-full-over-budget.json``'s refusal offers a chain that
#: really answers. Sized with room: 800 ports measure 139 850 wire bytes, a 75 %
#: margin, so narrowing what a port publishes by one field cannot flip a shipped
#: negative vector into an answered one; ``summary`` and ``index`` grow with the
#: width by no more than the count they carry.
WIDE_SWC_PORTS = 800


def _wide_swc() -> dict[str, Any]:
    """One ordinary, valid component that is merely very wide.

    Every port binds the base's own ``IExample``, so the vector isolates the one
    thing it is about: the size of a ``full`` layer read.
    """
    return {
        "name": "WideSWC",
        "type": "application",
        "description": "A component wide enough that a full layer read does not fit in one response",
        "ports": [
            {
                "name": f"p{index:03d}",
                "direction": "provide" if index % 2 == 0 else "require",
                "interface": "IExample",
            }
            for index in range(WIDE_SWC_PORTS)
        ],
        "memory": {"RAM0": 1024},
        "handle_termination_and_restart": "no_support",
        "satisfies": [EXAMPLE_LINK],
    }


#: The edit that makes the base model wide, shared by the two seeds whose subject
#: is size — a ``full`` layer read and a join's product — so the two vectors read
#: one model.
_WIDE_SWC_EDITS = (
    SeedEdit(
        path="swc/example/WideSWC.yaml",
        document=_wide_swc(),
        reason=(
            "`context.existing_elements` states swc/WideSWC, and its width is what puts the "
            "layer's `full` publication, and its ports' join with themselves, over the response budget."
        ),
    ),
)


def _example_swc(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """``ExampleSWC`` with its runnable's ``events`` list replaced."""
    return {
        "name": "ExampleSWC",
        "type": "application",
        "description": "Example software component for testing and demonstration",
        "ports": [
            {
                "name": "pOut",
                "direction": "provide",
                "interface": "IExample",
                "description": "Example output port providing data",
            }
        ],
        "runnables": [
            {
                "name": "ExampleRun",
                "min_start_interval": 0.01,
                "events": [dict(event) for event in events],
            }
        ],
        "memory": {"RAM0": 2048},
        "handle_termination_and_restart": "no_support",
        "satisfies": [EXAMPLE_LINK],
    }


#: ``Task_10ms`` running a runnable of both components, which the base does not,
#: for the vectors about ``safety.task-mixed-asil``: a task mixes the levels of the
#: components it runs.
_TASK_10MS_BOTH_RUNNABLES: dict[str, Any] = {
    "name": "Task_10ms",
    "period": 0.01,
    "priority": 5,
    "partition": "PARTITION0",
    "wcet": "1200us",
    "description": "10 ms application task",
    "runnables": [
        {"swc": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"},
        {"swc": "ExampleSWC", "runnable": "ExampleRun"},
    ],
}


#: The eleven value types the base ships that ``read-data-types-layer`` does not
#: name. Every one but ``float32`` goes, because ``float32`` is the one
#: ``IExample``'s data element is typed by and ``IExample`` stays.
_UNNAMED_VALUE_TYPES = (
    "bool",
    "boolean",
    "float64",
    "int8",
    "int16",
    "int32",
    "int64",
    "uint8",
    "uint16",
    "uint32",
    "uint64",
)

#: The data types the base ships that ``read-data-types-layer`` does not name:
#: the eleven value types above. Its expected response is the layer's whole
#: content, so everything it does not list has to go; the base types are a layer
#: of their own, which the read does not address, so they stay.
_UNNAMED_DATA_TYPES = _UNNAMED_VALUE_TYPES

#: The base on two partitions, for ``partitions`` and ``crossing_ends``: the
#: base runs ``ExampleConsumerSWC`` in ``PARTITION0`` (on ``CORE0``) and
#: ``ExampleSWC`` in no task, so no connector crosses a partition. These edits
#: add ``CORE1`` with ``PARTITION1`` on it and a task there running
#: ``ExampleSWC``, which makes ``Example_to_Consumer`` cross from
#: ``PARTITION1`` to ``PARTITION0``, and a third component no task runs. One
#: partition per component (``task.instance-in-multiple-partitions``).
_PARTITIONED_EDITS = (
    SeedEdit(
        path="hardware/CORE1.yaml",
        document={"name": "CORE1", "kind": "core", "threshold": 0.7, "description": "A second application core"},
        reason="`context.existing_elements` states hardware/CORE1, the core PARTITION1 is on.",
    ),
    SeedEdit(
        path="hardware/PARTITION1.yaml",
        document={
            "name": "PARTITION1",
            "kind": "partition",
            "core": "CORE1",
            "description": "The partition on CORE1",
        },
        reason="`context.existing_elements` states hardware/PARTITION1, the provider's partition.",
    ),
    SeedEdit(
        path="hardware/RAM0.yaml",
        document={
            "name": "RAM0",
            "kind": "memory",
            "type": "RAM",
            "size": 262144,
            "accessible": ["CORE0", "CORE1"],
            "threshold": 0.8,
            "enforcement": "strict",
            "description": "256 KiB application RAM pool, shared by both cores",
        },
        reason=(
            "`hardware.unreachable-memory`: ExampleSWC budgets RAM0 and now runs on CORE1, so CORE1 must reach it."
        ),
    ),
    SeedEdit(
        path="tasks/Task_10ms_P1.yaml",
        document={
            "name": "Task_10ms_P1",
            "period": 0.01,
            "priority": 5,
            "partition": "PARTITION1",
            "wcet": "1000us",
            "description": "10 ms task in PARTITION1",
            "runnables": [{"swc": "ExampleSWC", "runnable": "ExampleRun"}],
        },
        reason="`context.summary` states the task running ExampleSWC in PARTITION1.",
    ),
    SeedEdit(
        path="swc/example/UnplacedSWC.yaml",
        document={
            "name": "UnplacedSWC",
            "type": "application",
            "description": "A component no task runs",
            "memory": {"RAM0": 512},
            "handle_termination_and_restart": "no_support",
            "satisfies": [EXAMPLE_LINK],
        },
        reason="`context.existing_elements` states swc/UnplacedSWC, the component no partition runs.",
    ),
)

SEEDS: dict[str, Seed] = {
    # --- examples ---------------------------------------------------------
    "discover-project-all-layers-present": Seed(
        reason="`layers_present` lists all fifteen; the base already has them.",
    ),
    "discover-project-baseline": Seed(
        reason=(
            "The same base as `discover-project-all-layers-present` and deliberately the same model and the same "
            "request: the two differ by the CHANNEL. This one is replayed under the MCP server's "
            '`discover_default="baseline"` (`HOST_CHANNEL_VECTORS`), so it records what the agent host receives '
            "for a project-wide `discover` naming no form: the passport, the `reads` block and the token."
        ),
    ),
    "discover-project-full-over-budget": Seed(
        reason=(
            "The same base as the vector above and deliberately the same model: the two differ by the REQUEST "
            '(`form: "full"`) and not by the project. `summary` is the default and strips every descriptor '
            "`description`, `full` is on request, and the contract's size (its composite collections and member "
            "kinds) puts the project-wide `full` over the budget: it is a REFUSAL on every project, because the "
            "size is the contract's and not the model's."
        ),
    ),
    **{
        f"discover-layer-{layer.replace('_', '-')}-full": Seed(
            reason=(
                "`layers_present` lists all fifteen; the base already has them. One of the fifteen per-layer `full` "
                "discoveries that carry the shipped contract prose, because the project-wide `full` is a refusal — "
                "exactly the requests that refusal hints after its `summary` step."
            ),
        )
        for layer in (
            "swc",
            "interfaces",
            "connectors",
            "implementation_types",
            "mapping_sets",
            "requirements",
            "compositions",
            "tasks",
            "sequences",
            "hardware",
            "base_types",
            "application_types",
            "units",
            "compu_methods",
            "data_constraints",
        )
    },
    "discover-layer-implementation-types": Seed(
        reason="`layers_present` lists all fifteen; the base already has them.",
    ),
    "discover-layer-hardware": Seed(
        reason="`layers_present` lists all fifteen; the base already has them, `hardware/` included.",
    ),
    "discover-layer-hardware-absent": Seed(
        reason=(
            "The one seed that removes a layer. `hardware/` is MANDATORY, so its absence is a project "
            "defect rather than an opted-out regime — the state `check-model` reports as "
            "`hardware.layer-absent` — and discovery is what tells a client so while publishing the "
            "inventory it needs to author the first element. A passport naming a project migrated from "
            "an earlier release rather than the basic example."
        ),
        absent_layers=("hardware",),
        project_name="swcraft-migrated-project",
        baseline_valid=False,
        baseline_note=(
            "A project with no `hardware/` directory fails `check-model` by construction: the layer is "
            "mandatory and `hardware.layer-absent` is exactly that finding. The vector is a `discover`, "
            "which reads and never stages, so no pass validates the baseline — the invalidity IS the "
            "state the exchange is written to describe."
        ),
    ),
    "read-data-types-layer": Seed(
        reason=(
            "The expected response is the layer's whole content — two elements — so the base's other "
            "eleven value types are removed and the value type it names is added. What stays is the two value "
            "types that rest on `float32_base`: the base's own `float32`, which `IExample` is typed by, and "
            "`VehicleSpeed_kph`. The base types stay too, in their own layer, which the read does not address. "
            "The vector's input pins `form: full`; the seed is what makes that full answer two elements rather "
            "than thirteen."
        ),
        edits=(
            *(
                SeedEdit(
                    path=f"data_types/implementation/{name}.yaml",
                    reason="`expected.response.value` names only float32_base, float32 and VehicleSpeed_kph.",
                )
                for name in _UNNAMED_DATA_TYPES
            ),
            SeedEdit(
                path="interfaces/IDiagnosticService.yaml",
                reason=(
                    "It references uint16 and uint8 argument types; removing both above would leave a "
                    "dangling data-type reference, so the interface goes with it and the baseline stays valid."
                ),
            ),
            SeedEdit(
                path="data_types/implementation/VehicleSpeed_kph.yaml",
                document=VEHICLE_SPEED_KPH,
                reason="`context.existing_elements` states implementation_types/VehicleSpeed_kph.",
            ),
        ),
    ),
    "read-data-types-layer-filtered": Seed(
        reason=(
            "The base's twelve base types ARE the vector's subject: the slice is two of them, and the figure the "
            "description quotes is the ratio between the filtered response and the whole `base_types` layer's. Its "
            "sibling `read-data-types-layer` removes value types because it states the `implementation_types` layer's "
            "whole content; this one must not remove a base type, or the filter would be selecting from a set the base "
            "does not have."
        ),
    ),
    "read-data-types-layer-paged": Seed(
        reason=(
            "The base's twelve base types ARE the vector's subject, exactly as they are its filtered "
            "sibling's: `page.total` is 12, the window continues past `float64_base`, and the ordering claim "
            "the description makes (`int8_base` after `int64_base`, the readers' ordinal identity sort) is a "
            "claim about that set of names. Removing any of them would move the total, the window and the "
            "order at once."
        ),
    ),
    "read-swc-element": Seed(
        reason="ExampleSWC, IExample and RAM0 are all in the base.",
    ),
    "read-several-addresses": Seed(
        reason=(
            "ExampleSWC and IExample are in the base, and no component named Logger is: the batch's middle address "
            "is the miss answered beside the two reads that succeed."
        ),
    ),
    "read-model-map": Seed(
        reason="The bare base: every layer holds at most twelve elements, so the map lists every one of them.",
    ),
    "find-names": Seed(
        reason=(
            "The bare base: the three names are resolved against its two components, the members of Root, "
            "the connector, the sequence and IExample, so the candidates and their order are the base's."
        ),
    ),
    "find-names-many": Seed(
        reason=(
            "The bare base, as `find-names` reads it: the nine names are resolved against its components, the "
            "members of Root, the interfaces, the hardware, the task and the sequence, so each name's one "
            "candidate and its `more` are the base's."
        ),
    ),
    "find-text": Seed(
        reason=(
            "The base holds one requirement. The seed adds the two a text search must tell apart -- one "
            "whose text holds both words of the phrase, its source document named, and one holding only "
            "`example` -- so the answer shows two requirements in id order, the words each matched, and no "
            "`document`."
        ),
        edits=(
            SeedEdit(
                path="requirements/REQ_EXAMPLE_002.yaml",
                document=_requirement()
                | {
                    "id": "REQ_EXAMPLE_002",
                    "title": "Example functionality monitoring",
                    "description": "The system shall monitor the example functionality.",
                    "document": "specs/example/system-requirements.reqif",
                },
                reason="`context.existing_elements` states requirements/REQ_EXAMPLE_002, the second match.",
            ),
            SeedEdit(
                path="requirements/REQ_EXAMPLE_003.yaml",
                document=_requirement()
                | {
                    "id": "REQ_EXAMPLE_003",
                    "title": "Diagnostic data retention",
                    "description": "The system shall keep the example diagnostic data.",
                },
                reason="`context.existing_elements` states requirements/REQ_EXAMPLE_003, holding `example` alone.",
            ),
        ),
    ),
    "find-for-requirement": Seed(
        reason=(
            "The base's one requirement is linked from both components, so its ranking has no place left. "
            "The seed adds a requirement whose text names the consumer's input port and data value, linked "
            "already from the interface at its current version -- so the answer shows the trace search's two "
            "places in its own order, a component's port, its runnable and the component itself as rows with "
            "their bands and shared terms, and the interface among the existing links, out of the rows."
        ),
        edits=(
            SeedEdit(
                path="requirements/REQ_EXAMPLE_002.yaml",
                document=_requirement()
                | {
                    "id": "REQ_EXAMPLE_002",
                    "title": "Consumer data reception",
                    "description": "The consumer shall receive the example data value through its input port.",
                },
                reason="`context.existing_elements` states requirements/REQ_EXAMPLE_002, the requirement ranked.",
            ),
            SeedEdit(
                path="interfaces/IExample.yaml",
                document={
                    "name": "IExample",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": "value", "implementation_type": "float32"}],
                    "satisfies": ["REQ_EXAMPLE_002@1"],
                },
                reason="`context.summary` states the interface's link at the current version: an existing link.",
            ),
        ),
    ),
    "find-for-requirement-existing-links-bounded": Seed(
        reason=(
            "The `find-for-requirement` seed's requirement, linked from twenty interfaces at its current version: "
            "past the sixteen links an answer lists, so it carries the first sixteen in the address order, "
            "`existing_links_more` counting the four left out and `existing_links_read`, the read that pages "
            "every link. Each added interface is the base's `IExample` shape under another name, used by no port."
        ),
        edits=(
            SeedEdit(
                path="requirements/REQ_EXAMPLE_002.yaml",
                document=_requirement()
                | {
                    "id": "REQ_EXAMPLE_002",
                    "title": "Consumer data reception",
                    "description": "The consumer shall receive the example data value through its input port.",
                },
                reason="`context.existing_elements` states requirements/REQ_EXAMPLE_002, the requirement asked about.",
            ),
            *(
                SeedEdit(
                    path=f"interfaces/{name}.yaml",
                    document={
                        "name": name,
                        "kind": "sender_receiver",
                        "data_elements": [{"name": "value", "implementation_type": "float32"}],
                        "satisfies": ["REQ_EXAMPLE_002@1"],
                    },
                    reason=f"`context.existing_elements` states interfaces/{name}, one of the twenty links.",
                )
                for name in ("IExample", *(f"ILink{index:02d}" for index in range(1, 20)))
            ),
        ),
    ),
    "read-swc-ports-count-where": Seed(
        reason="The bare base: ExampleSWC's pOut is its one provide port, so the count is 1.",
    ),
    "remove-swc-with-dependents": Seed(
        reason=(
            "The bare base: ExampleConsumerSWC's four referrers -- the connector, Root's member, Task_10ms's "
            "entry and the one step of task10ms_consumer_cycle -- are the base's own, so the server's rows are."
        ),
    ),
    "update-requirement-version-confirming-links": Seed(
        reason=(
            "The base links `REQ_EXAMPLE_001@1` from both components' own `satisfies`. The seed adds the cases "
            "the confirmation must tell apart: a port's bare link (`rIn`, rewritten like a stated one), an "
            "interface whose list already holds the new version beside the old (only the `remove`), and a "
            "second requirement deriving from the first (a `derived_from` member, left alone)."
        ),
        baseline_valid=False,
        baseline_note=(
            "Since contract `12.5.0` the interface's `@1` beside its `@2` is a `trace.duplicate-link`, so the "
            "baseline fails `check-model`; the confirmation's one `remove` is exactly the repair, and pass D "
            "judges the staged result, which holds the requirement once. The rest are warnings."
        ),
        edits=(
            SeedEdit(
                path="swc/example/ExampleConsumerSWC.yaml",
                document={
                    "name": "ExampleConsumerSWC",
                    "type": "application",
                    "description": "Example software component consuming data from ExampleSWC",
                    "ports": [
                        {
                            "name": "rIn",
                            "direction": "require",
                            "interface": "IExample",
                            "description": "Example input port requiring data",
                            "satisfies": ["REQ_EXAMPLE_001"],
                        }
                    ],
                    "runnables": [
                        {
                            "name": "ExampleConsumerRun",
                            "min_start_interval": 0.01,
                            "events": [{"type": "timing", "period": 0.01}],
                        }
                    ],
                    "memory": {"RAM0": 1536},
                    "handle_termination_and_restart": "no_support",
                    "satisfies": [EXAMPLE_LINK],
                },
                reason="`context.summary` states the port's bare link, a site the confirmation rewrites.",
            ),
            SeedEdit(
                path="interfaces/IExample.yaml",
                document={
                    "name": "IExample",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": "value", "implementation_type": "float32"}],
                    "satisfies": [EXAMPLE_LINK, "REQ_EXAMPLE_001@2"],
                },
                reason="`context.summary` states the interface holding both versions: the remove alone.",
            ),
            SeedEdit(
                path="requirements/REQ_EXAMPLE_002.yaml",
                document=_requirement()
                | {
                    "id": "REQ_EXAMPLE_002",
                    "title": "Example requirement derived from the first",
                    "description": "The system shall refine the example functionality.",
                    "derived_from": [EXAMPLE_LINK],
                },
                reason="`context.existing_elements` states requirements/REQ_EXAMPLE_002, the `derived_from` member.",
            ),
        ),
    ),
    "remove-and-create-satisfies-member": Seed(
        reason=(
            "The base's requirement at version 1 leaves no link to replace. The seed raises it to version 2, so "
            "both components' `REQ_EXAMPLE_001@1` need confirming (a warning, so the baseline stays valid) and "
            "the client replaces ExampleSWC's: one member removed and one created in the same `satisfies` list."
        ),
        edits=(
            SeedEdit(
                path="requirements/REQ_EXAMPLE_001.yaml",
                document=_requirement() | {"version": 2},
                reason="`context.summary` states the requirement at version 2, the link at the old one.",
            ),
        ),
    ),
    "schema-violation-with-dependents-on-an-interface": Seed(
        reason="The bare base declares IDiagnosticService; the refusal reads nothing of it.",
    ),
    "create-swc-completed-with-mapping-set": Seed(
        reason=(
            "A physical type, an interface speaking it and the one mapping set that pairs it, none of which the base "
            "declares: the created component speaks Temp_C through ITemp and names no set, so pass D refuses it as "
            "`data-type.application-type-unmapped` and `complete: true` completes it with Thermal."
        ),
        edits=(
            SeedEdit(
                path="data_types/application/Temp_C.yaml",
                document={"name": "Temp_C", "kind": "application_type"},
                reason="`context.existing_elements` states application_types/Temp_C, the type the port speaks.",
            ),
            SeedEdit(
                path="interfaces/ITemp.yaml",
                document={
                    "name": "ITemp",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": "t", "application_type": "Temp_C"}],
                },
                reason="`context.existing_elements` states interfaces/ITemp, the interface the created port names.",
            ),
            SeedEdit(
                path="data_types/mapping_sets/Thermal.yaml",
                document={"name": "Thermal", "maps": [{"application": "Temp_C", "implementation": "float32"}]},
                reason=(
                    "`context.existing_elements` states mapping_sets/Thermal: the one declared set that pairs Temp_C, "
                    "which makes the repair unique."
                ),
            ),
        ),
    ),
    "read-swc-port-interface-field": Seed(
        reason="ExampleSWC's pOut port is in the base.",
    ),
    "read-swc-layer-summary": Seed(
        reason=(
            "The base's `swc/` layer is exactly the two components the expected summary lists, and the "
            "vector's subject is the WIDTH of the answer rather than its content — so a seed that added "
            "or removed an element would change the figure the description quotes for no reason."
        ),
    ),
    "read-swc-ports-collection-summary": Seed(
        reason="ExampleSWC's pOut port is in the base, and it is the whole of the collection the vector reads.",
    ),
    "read-swc-ports-members-summary-omits-unset-interface": Seed(
        reason=(
            "The base's two declared ports, each naming `IExample`, beside one external component stub whose "
            "port names no interface and carries the importer's hint — the two sides of the summary's null "
            "convention in one answer: a set reference published, an unset one absent, the hint never."
        ),
        edits=(
            SeedEdit(
                path="swc/external/SupplierStub.yaml",
                document={
                    "name": "SupplierStub",
                    "type": "application",
                    "external": True,
                    "external_ref": "/Supplier/SupplierPlatforms/SupplierStub",
                    "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
                    "ports": [{"name": "rSupplied", "direction": "require", "observed_interface": "IExample"}],
                    "satisfies": [EXAMPLE_LINK],
                },
                reason=(
                    "`context.existing_elements` states swc/SupplierStub: a stub's port is the one port the "
                    "contract lets name no interface, which is the unset reference the vector is about."
                ),
            ),
        ),
    ),
    "read-swc-where-description-absent": Seed(
        reason=(
            "The base's two components each carry a description, so the vector's two undescribed components are "
            'seeded beside them: one whose file omits the key (the schema default `""`) and one that states it '
            "empty — the two spellings of no description the clause must both answer."
        ),
        edits=(
            SeedEdit(
                path="swc/example/UndescribedSWC.yaml",
                document={
                    "name": "UndescribedSWC",
                    "type": "application",
                    "memory": {"RAM0": 512},
                    "handle_termination_and_restart": "no_support",
                    "satisfies": [EXAMPLE_LINK],
                },
                reason="`context.existing_elements` states swc/UndescribedSWC, whose file has no `description` key.",
            ),
            SeedEdit(
                path="swc/example/BlankDescriptionSWC.yaml",
                document={
                    "name": "BlankDescriptionSWC",
                    "type": "application",
                    "description": "",
                    "memory": {"RAM0": 512},
                    "handle_termination_and_restart": "no_support",
                    "satisfies": [EXAMPLE_LINK],
                },
                reason="`context.existing_elements` states swc/BlankDescriptionSWC, with an empty description.",
            ),
        ),
    ),
    "read-swc-ports-members-filtered": Seed(
        reason=(
            "The base's two components — each with one port, both on IExample — ARE the vector's "
            "subject: a member slice answers members from more than one element, and a seed that "
            "removed either component would leave it demonstrating nothing a collection address "
            "does not already answer."
        ),
    ),
    "read-swc-members-path-runnable-writes": Seed(
        reason=(
            "The base carries no runnable access, so a path below `runnables` would answer nothing. The seed "
            "writes the base's sender-receiver traffic down as accesses: three writes over two runnables of the "
            "provider, two reads of the consumer, one consumer behind a delegation and one data element typed by "
            "a mapped application type."
        ),
        edits=_RUNNABLE_ACCESS_EDITS,
    ),
    "unknown-field-members-path-step": Seed(
        reason="The same seed: the path's first step resolves, and the refusal is the second step's alone.",
        edits=_RUNNABLE_ACCESS_EDITS,
    ),
    "read-connectors-derived-connection-ends": Seed(
        reason=(
            "The same seed: its consumer stands in a composition one level below the root, behind two "
            "delegated boundary ports, so each connector's atomic requester end is reached through a "
            "delegation — the fact `connection_ends` computes and the base model, whose connectors join two "
            "atomic components directly, cannot show."
        ),
        edits=_RUNNABLE_ACCESS_EDITS,
    ),
    "schema-violation-derived-at-wrong-set": Seed(
        reason=(
            "The same seed: the refusal depends on none of it, because a computed value's `at` is checked "
            "against the request before a row is computed — and on this seed the `swc` layer has elements, "
            "so the refusal is visibly not an empty answer."
        ),
        edits=_RUNNABLE_ACCESS_EDITS,
    ),
    "read-swc-derived-effective-level": Seed(
        reason=(
            "The base's two components both derive `QM`: nothing allocates a level. The seed gives the vector "
            "the three ways a component's effective level is answered — `ExampleSWC` DECLARES D, "
            "`ExampleConsumerSWC` declares nothing and DERIVES B from the requirement allocated to it, and a "
            "third component, `UnratedSWC`, is allocated no level and is `QM`. The baseline stays valid: the "
            "declared D is above the allocated B, never below it, and `Task_10ms` runs the consumer alone."
        ),
        edits=(
            SeedEdit(
                path="swc/example/ExampleSWC.yaml",
                document={
                    "name": "ExampleSWC",
                    "type": "application",
                    "description": "Example software component for testing and demonstration",
                    "asil": "D",
                    "ports": [
                        {
                            "name": "pOut",
                            "direction": "provide",
                            "interface": "IExample",
                            "description": "Example output port providing data",
                        }
                    ],
                    "runnables": [
                        {
                            "name": "ExampleRun",
                            "min_start_interval": 0.01,
                            "events": [{"type": "timing", "period": 0.01}],
                        }
                    ],
                    "memory": {"RAM0": 2048},
                    "handle_termination_and_restart": "no_support",
                    "satisfies": [EXAMPLE_LINK],
                },
                reason="`context.summary` states ExampleSWC declaring D: the declared half of the answer.",
            ),
            SeedEdit(
                path="swc/example/UnratedSWC.yaml",
                document={
                    "name": "UnratedSWC",
                    "type": "application",
                    "description": "A component no levelled requirement is allocated to",
                    "memory": {"RAM0": 512},
                    "handle_termination_and_restart": "no_support",
                    "satisfies": ["REQ_EXAMPLE_002@1"],
                },
                reason="`context.existing_elements` states swc/UnratedSWC, the `QM` row.",
            ),
            SeedEdit(
                path="requirements/REQ_EXAMPLE_001.yaml",
                document=_requirement(asil="B"),
                reason=(
                    "`context.summary` states the requirement allocating B. Without it the consumer derives "
                    "`QM` and the vector shows no derived level."
                ),
            ),
            SeedEdit(
                path="requirements/REQ_EXAMPLE_002.yaml",
                document=_requirement()
                | {
                    "id": "REQ_EXAMPLE_002",
                    "title": "Example requirement for UnratedSWC",
                    "description": "The system shall provide unrated functionality via UnratedSWC.",
                },
                reason="UnratedSWC satisfies it, which keeps the component clear of the warning; no level here.",
            ),
        ),
    ),
    "read-requirements-derived-realised-by": Seed(
        reason=(
            "The base links `REQ_EXAMPLE_001` from both components' own `satisfies`, which shows one kind of "
            "site. The seed adds the two others a reader must tell apart -- a link on a port (`rIn`, the "
            "member) stated as a bare id (`stated: null`) and one on an interface -- and a second "
            "requirement nothing names, which has no row. Warnings only, so the baseline stays valid."
        ),
        edits=(
            SeedEdit(
                path="swc/example/ExampleConsumerSWC.yaml",
                document={
                    "name": "ExampleConsumerSWC",
                    "type": "application",
                    "description": "Example software component consuming data from ExampleSWC",
                    "ports": [
                        {
                            "name": "rIn",
                            "direction": "require",
                            "interface": "IExample",
                            "description": "Example input port requiring data",
                            "satisfies": ["REQ_EXAMPLE_001"],
                        }
                    ],
                    "runnables": [
                        {
                            "name": "ExampleConsumerRun",
                            "min_start_interval": 0.01,
                            "events": [{"type": "timing", "period": 0.01}],
                        }
                    ],
                    "memory": {"RAM0": 1536},
                    "handle_termination_and_restart": "no_support",
                    "satisfies": [EXAMPLE_LINK],
                },
                reason="`context.summary` states the port's bare link: the member and the unstated version.",
            ),
            SeedEdit(
                path="interfaces/IExample.yaml",
                document={
                    "name": "IExample",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": "value", "implementation_type": "float32"}],
                    "satisfies": [EXAMPLE_LINK],
                },
                reason="`context.summary` states the interface's link: a site of the `interfaces` layer.",
            ),
            SeedEdit(
                path="requirements/REQ_EXAMPLE_002.yaml",
                document=_requirement()
                | {
                    "id": "REQ_EXAMPLE_002",
                    "title": "Example requirement nothing realises",
                    "description": "The system shall provide functionality no element realises yet.",
                },
                reason="`context.existing_elements` states requirements/REQ_EXAMPLE_002, the requirement with no row.",
            ),
        ),
    ),
    "read-swc-derived-partitions": Seed(
        reason=(
            "The base runs one component in its one partition, so `partitions` would show a single row. The "
            "seed adds a second core with a partition and a task running `ExampleSWC` there, and a component "
            "no task runs, so the answer shows one partition per placed component, each on its core, and no "
            "row for the unplaced one."
        ),
        edits=_PARTITIONED_EDITS,
    ),
    "read-connectors-derived-crossing-ends": Seed(
        reason=(
            "The same seed as `read-swc-derived-partitions`: with `ExampleSWC` in `PARTITION1` and its "
            "consumer in `PARTITION0`, the base's one connector crosses a partition, which the base, running "
            "the provider in no task, cannot show."
        ),
        edits=_PARTITIONED_EDITS,
    ),
    "read-swc-writes-joined-to-readers": Seed(
        reason=(
            "The same seed: its three writes each reach one connector, and each connector's requester end — "
            "reached through the group's delegation — reads the same data element, so the join's two entries "
            "both match and the answer is the sender-receiver table, one row per write."
        ),
        edits=_RUNNABLE_ACCESS_EDITS,
    ),
    "read-swc-ports-unconnected-of-one-element": Seed(
        reason=(
            "The base connects every port it declares, so `unconnected` would answer no row. The seed gives two "
            "components a port that nothing connects -- `ExampleSWC` its `rSpare`, and a third component, "
            "`UnplacedSWC`, its `rIdle` -- so the join on `element` to `ExampleSWC`'s one row is what keeps "
            "`UnplacedSWC`'s port out of the answer. Both are require ports on `IExample`, the base's own "
            "interface, and the third component satisfies the base's requirement, so the baseline stays valid."
        ),
        edits=(
            SeedEdit(
                path="swc/example/ExampleSWC.yaml",
                document=_example_swc([{"type": "timing", "period": 0.01}])
                | {
                    "ports": [
                        *_example_swc(())["ports"],
                        {
                            "name": "rSpare",
                            "direction": "require",
                            "interface": "IExample",
                            "description": "A spare input that nothing connects",
                        },
                    ]
                },
                reason="`context.summary` states ExampleSWC's port rSpare, the one row of the answer.",
            ),
            SeedEdit(
                path="swc/example/UnplacedSWC.yaml",
                document={
                    "name": "UnplacedSWC",
                    "type": "application",
                    "description": "A component whose one port nothing connects",
                    "ports": [
                        {
                            "name": "rIdle",
                            "direction": "require",
                            "interface": "IExample",
                            "description": "An input that nothing connects",
                        }
                    ],
                    "memory": {"RAM0": 512},
                    "handle_termination_and_restart": "no_support",
                    "satisfies": [EXAMPLE_LINK],
                },
                reason=(
                    "`context.existing_elements` states swc/UnplacedSWC, the second component with an unconnected "
                    "port, which the join drops."
                ),
            ),
        ),
    ),
    "schema-violation-join-path-outside-row": Seed(
        reason=(
            "The base, whose components carry no runnable access: the outer member path answers no row, so the "
            "refusal is visibly decided from the declared row shape and never from the rows."
        ),
    ),
    "schema-violation-join-without-index-form": Seed(
        reason="The base: the envelope refuses the request before the model is consulted.",
    ),
    "schema-violation-join-over-max-entries": Seed(
        reason=(
            "The base: the envelope refuses a ninth join entry before the model is consulted, so any seed "
            "carries it; its eight-entry twin is answered on the same model."
        ),
    ),
    "read-layer-full-over-budget": Seed(
        reason=(
            "The one seed whose subject is SIZE. `examples/basic` is two small components, so a `full` "
            "read of its `swc` layer is 1 978 WIRE bytes — the unit the budget is stated in, as "
            "`WIDE_SWC_PORTS` above quotes its own figures in — and fits under any budget; the vector needs a layer "
            "that does not fit, and one wide component states that in one file rather than in fifty. "
            "The requirement is rewritten with a third link because the consistency check demands an "
            "incoming link for every SWC and this baseline must be VALID: the refusal the vector pins "
            "is about the response, not about the model."
        ),
        edits=_WIDE_SWC_EDITS,
    ),
    "read-swc-ports-join-over-budget": Seed(
        reason=(
            "The same wide seed, read for another reason: WideSWC's 800 ports joined with themselves on "
            "their component are 640 000 combined rows, a product no page could bring under the budget, and "
            "the seed states it in one file. The refusal is decided from the join's buckets before a combined "
            "row is built, so the vector costs what a read of the two sides costs."
        ),
        edits=_WIDE_SWC_EDITS,
    ),
    "create-data-type-alias": Seed(
        reason="float32_base is in the base and VehicleSpeed_kph is what the change set creates.",
    ),
    "create-server-runnable-with-operation-events": Seed(
        reason=(
            "ExampleSWC and IDiagnosticService are in the base, and IDiagnosticService's ReadDtc and ClearDtc "
            "share one signature, so one runnable serving both leaves check-model with nothing to report."
        ),
    ),
    "create-variable-and-its-accesses": Seed(
        reason=(
            "ExampleSWC, its runnable ExampleRun and the uint8 implementation type are in the base, and the base "
            "declares no variable, so the change set creates the variable both accesses name."
        ),
    ),
    "create-exclusive-area-and-its-entries": Seed(
        reason=(
            "ExampleSWC and its runnable ExampleRun are in the base, and the base declares no exclusive area, so "
            "the change set creates the area both entries name."
        ),
    ),
    "create-data-received-event": Seed(
        reason=(
            "ExampleConsumerSWC, its require port rIn on IExample and its runnable ExampleConsumerRun are in the "
            "base, and IExample declares the element value, so the change set needs nothing else."
        ),
    ),
    "create-asynchronous-call-and-its-call-returned-event": Seed(
        reason=(
            "ExampleConsumerSWC, its runnable ExampleConsumerRun and IDiagnosticService, which declares ReadDtc, are "
            "in the base, and the manifest states no arxml_version (4.2.2); the change set creates the port, the "
            "asynchronous call and the call_returned event that is its one result point, so it needs nothing else."
        ),
    ),
    "create-swc-with-ports-and-runnables": Seed(
        reason=(
            "SpeedSensorSWC must be ABSENT (the change set creates it) while the interface it binds and "
            "the RAM0 memory it budgets against must be present. Since contract `11.0.0` the link that keeps "
            "the created SWC free of check rule 1's warning is the `satisfies` entry of its own `create` "
            "payload, so the baseline no longer has to name a component it does not hold, and it is valid."
        ),
        edits=(
            SeedEdit(
                path="interfaces/IVehicleSpeed.yaml",
                document=IVEHICLE_SPEED_SEEDED,
                reason="`context.existing_elements` states interfaces/IVehicleSpeed.",
            ),
        ),
    ),
    "create-connector-between-compatible-interfaces": Seed(
        reason=(
            "The bare base, and deliberately so. ExampleSWC, ExampleConsumerSWC, float32 and uint8 are the "
            "base's own, and the two interfaces the connector joins are created BY the change set — which is "
            "what lets a connector between two DIFFERENT interfaces be stated without seeding a model the base "
            "does not have. Neither IStatusWide nor IStatusNarrow may exist beforehand, and neither does."
        ),
    ),
    "create-interface-ports-and-connector": Seed(
        reason=(
            "SpeedSensorSWC, ExampleConsumerSWC, VehicleSpeed_kph and bool must be present while "
            "IVehicleSpeed — the element the change set creates — must not be."
        ),
        edits=(
            SeedEdit(
                path="data_types/implementation/VehicleSpeed_kph.yaml",
                document=VEHICLE_SPEED_KPH,
                reason="`context.existing_elements` states implementation_types/VehicleSpeed_kph.",
            ),
            SeedEdit(
                path="swc/sensors/SpeedSensorSWC.yaml",
                document={
                    "name": "SpeedSensorSWC",
                    "type": "application",
                    "description": "Reads the wheel speed sensors and publishes the vehicle speed",
                    "memory": {"RAM0": 1024},
                    "handle_termination_and_restart": "no_support",
                    "satisfies": [EXAMPLE_LINK],
                },
                reason=(
                    "`context.existing_elements` states swc/SpeedSensorSWC. Its `memory` entry is "
                    "check_resource_budgets': every SWC must declare a budget for every declared memory."
                ),
            ),
            SeedEdit(
                path="compositions/Root.yaml",
                document=_root_composition_document(["ExampleSWC", "ExampleConsumerSWC", "SpeedSensorSWC"]),
                reason=(
                    "`rule:composition.connector-endpoint-not-a-member`: the connector the "
                    "change set creates names SpeedSensorSWC, and a connector's endpoint must name a type "
                    "its composition places. The base root places the two components the base declares; the "
                    "seeded third one has to join them, exactly as it joins the requirement above."
                ),
            ),
        ),
    ),
    "create-task-and-sequence": Seed(
        reason="ExampleSWC's ExampleRun, Task_10ms and CORE0 are all in the base.",
    ),
    "update-runnable-event-and-memory-entry": Seed(
        reason="ExampleRun and RAM0 are in the base, with the 10 ms event the change set retires.",
    ),
    "update-memory-accessible-and-threshold": Seed(
        reason=(
            "The change set widens `RAM0.accessible` to a second core and raises its threshold, so the "
            "core it names has to be declared: `CORE1` is seeded and the base's `CORE0` is already there."
        ),
        edits=(
            SeedEdit(
                path="hardware/CORE1.yaml",
                document=CORE1_SEEDED,
                reason=(
                    "`context.existing_elements` states hardware/CORE1, and `hardware.accessible-undeclared-core` "
                    "would refuse the staged result if the name resolved to nothing."
                ),
            ),
        ),
    ),
    "update-requirement-and-swc-asil": Seed(
        reason=(
            "The base is exactly the state the vector states and no overlay is needed: `REQ_EXAMPLE_001` "
            "links both components, and NOTHING in the base declares an `asil` — the field has no default, "
            "so a project that has never allocated a level carries no key at all and both "
            "`before` values in the expected delta are the real prior state rather than a seeded one. The "
            "accepted outcome depends on the second component too: `ExampleConsumerSWC` declares nothing "
            "and DERIVES the requirement's D, which is why the change set that levels only `ExampleSWC` "
            "leaves a valid model."
        ),
    ),
    # --- the level on the information, and the two arguments ---
    "update-data-element-asil": Seed(
        reason=(
            "`REQ_EXAMPLE_001` must already carry `asil: D`, and that is the whole overlay. Rating the "
            "element on the BARE base would put it above both components, which derive `QM` there, and "
            "`safety.provider-below-element` would refuse the very change set this vector exists to "
            "accept. With the requirement allocated, both components derive D, the element is rated D, "
            "and neither port rule has anything to compare that is not equal."
        ),
        edits=(
            SeedEdit(
                path="requirements/REQ_EXAMPLE_001.yaml",
                document=_requirement(asil="D"),
                reason=(
                    "`context.summary` states both components as deriving D. Without it both derive `QM`, "
                    "the D-rated element is above its own producer, and the accepted vector becomes a "
                    "refusal."
                ),
            ),
        ),
    ),
    "create-port-measure": Seed(
        reason=(
            "The finding this change set ANSWERS has to already stand, so the baseline is deliberately "
            "one that `check-model` reports: the requirement allocates D to both components and the "
            "element is rated A, which puts the consumer above what it takes with no measure declared. "
            "That is not a fixture defect — it is the state an architect is in when they write the "
            "argument, and the point of the exemption is that the model gets there before the argument "
            "does. Pass D judges the STAGED result, which is clean."
        ),
        edits=(
            SeedEdit(
                path="requirements/REQ_EXAMPLE_001.yaml",
                document=_requirement(asil="D"),
                reason=(
                    "Both components must derive D, or the consumer is not above anything and "
                    "there is no finding to answer."
                ),
            ),
            SeedEdit(
                path="interfaces/IExample.yaml",
                document={
                    "name": "IExample",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": "value", "implementation_type": "float32", "asil": "A"}],
                },
                reason=(
                    "The element must be rated BELOW the consumer. `A` and not `QM`, because a rated "
                    "element and an unrated one are different states: an unrated element is compared "
                    "against nothing at all and would produce no finding either."
                ),
            ),
        ),
        baseline_valid=False,
        baseline_note=(
            "The baseline carries exactly one `safety.consumer-above-element` finding, by construction: "
            "the consumer is at D and takes an A-rated element through `rIn` with no measure. A vector "
            "about an exemption cannot have a clean baseline — there would be nothing to exempt — and "
            "pass D validates the staged result, which the change set makes clean."
        ),
    ),
    "update-task-coexistence": Seed(
        reason=(
            "Two things the base does not have: a task that runs BOTH components, and two distinct "
            "effective levels among them. `Task_10ms` runs only `ExampleConsumerRun`, so a mixed task "
            "needs the second runnable mapped; and `ExampleSWC` declares D while `ExampleConsumerSWC` "
            "declares nothing under a requirement that allocates nothing, so it derives `QM` — the pair "
            "`{D, QM}` the rule reports. The requirement is left unallocated deliberately: allocating "
            "one would either level both components or put `ExampleSWC` below it, and the vector would "
            "then be about `safety.component-below-requirements` instead."
        ),
        edits=(
            SeedEdit(
                path="swc/example/ExampleSWC.yaml",
                document=_example_swc(({"type": "timing", "period": 0.01},)) | {"asil": "D"},
                reason=(
                    "One of the two levels the task mixes. Declared rather than derived, because "
                    "nothing allocates one here."
                ),
            ),
            SeedEdit(
                path="tasks/Task_10ms.yaml",
                document=_TASK_10MS_BOTH_RUNNABLES,
                reason=(
                    "The base task runs one runnable, so its components are one component and its levels "
                    "one level. A task cannot mix what it does not run."
                ),
            ),
        ),
        baseline_valid=False,
        baseline_note=(
            "The baseline carries exactly one `safety.task-mixed-asil` finding: `Task_10ms` runs "
            "components at D and at `QM` and declares no argument. Like `create-port-measure` above, a "
            "vector about an exemption needs the finding to already stand."
        ),
    ),
    "consistency-violation-provider-below-element": Seed(
        reason=(
            "None: the bare base IS the state this vector states. Nothing in it declares an `asil`, so "
            "`ExampleSWC` derives `QM`, and rating the element it provides at D is enough to refuse. "
            "That the seed is empty is the point worth recording — a DERIVED `QM` producing a rated "
            "element is a finding, and no overlay is needed to reach it."
        ),
    ),
    "consistency-violation-consumer-above-element": Seed(
        reason=(
            "`REQ_EXAMPLE_001` at `asil: D`, exactly as `update-data-element-asil` needs it and for the "
            "mirror reason: both components must derive D so that rating the element at A puts the "
            "CONSUMER above it. On the bare base both derive `QM`, an A-rated element is above them, "
            "and the finding would be the provider's instead."
        ),
        edits=(
            SeedEdit(
                path="requirements/REQ_EXAMPLE_001.yaml",
                document=_requirement(asil="D"),
                reason="Both components derive D; without it the vector refuses under the other rule.",
            ),
        ),
    ),
    "consistency-violation-task-mixed-asil": Seed(
        reason=(
            "Only the task. Both components derive `QM` on the bare base, so a task running both is "
            "homogeneous and the BASELINE is valid — which is what makes this a vector about a result "
            "the change set creates rather than one it inherits. The change set then declares one "
            "component at D and the task carries two levels."
        ),
        edits=(
            SeedEdit(
                path="tasks/Task_10ms.yaml",
                document=_TASK_10MS_BOTH_RUNNABLES,
                reason="A task cannot mix levels among components it does not run.",
            ),
        ),
    ),
    "consistency-violation-measure-unknown-element": Seed(
        reason=(
            "None: the bare base IS the state. `IExample` declares one element, `value`, and the change "
            "set names `valu`. Nothing about the LEVELS matters here — the rule is about a key the "
            "interface does not declare, and it fires whether or not anything is rated."
        ),
    ),
    "consistency-violation-component-below-requirements": Seed(
        reason=(
            "`REQ_EXAMPLE_001` must already carry `asil: D` — that is the whole difference from the "
            "example vector above, whose fixture is the bare base. With the requirement allocated and "
            "neither component declaring a level, both DERIVE D and the baseline is valid; the change set "
            "declares `ExampleSWC` at B and `safety.component-below-requirements` refuses the result. "
            "Seeding the component instead would make the BASELINE the violation, which is a different "
            "vector: pass D judges the assembled result, and this one is about a result the change set "
            "creates."
        ),
        edits=(
            SeedEdit(
                path="requirements/REQ_EXAMPLE_001.yaml",
                document=_requirement(asil="D"),
                reason=(
                    "`context.summary` states the requirement as already allocated at ASIL D. Without it "
                    "no requirement is above the component's declared B, `check_component_asil` finds "
                    "nothing, and the change set this vector exists to refuse is accepted."
                ),
            ),
        ),
    ),
    # --- the level the hardware is declared able to host ---
    "update-core-asil-capability": Seed(
        reason=(
            "A model that has ADOPTED a capability and reports nothing — the one kind of baseline on which a "
            "capability change set can be observed committing at all. `REQ_EXAMPLE_001` allocates B, so both "
            "components derive B; `PARTITION0` is rated B and `CORE0` declares `asil_capability: B`, so the "
            "partition `Task_10ms` runs in sits on a core declared able to host it: equal, and silent. The change "
            "set raises the capability to D. No finding stands for it to close, and it is asked about anyway: a "
            "raise is an ACT, which asks by its nature, exactly as raising a threshold does whether or not anything "
            "is over budget today."
        ),
        edits=(
            SeedEdit(
                path="requirements/REQ_EXAMPLE_001.yaml",
                document=_requirement(asil="B"),
                reason=(
                    "`context.summary` states both components as deriving B. Without an allocation both derive "
                    "`QM`, and the vector would be about a core no rated component runs on."
                ),
            ),
            SeedEdit(
                path="hardware/CORE0.yaml",
                document=CORE0_BASE | {"asil_capability": "B"},
                reason=(
                    "`context.existing_elements` states hardware/CORE0 carrying a capability — the `before` of the "
                    "expected delta. Equal to the components' level, so the baseline is clean."
                ),
            ),
            SeedEdit(
                path="hardware/PARTITION0.yaml",
                document=PARTITION0_BASE | {"asil": "B"},
                reason=(
                    "`context.summary` states the partition rated B (contract `9.0.0`: the partition carries the "
                    "level the core is compared with). Unrated, it is compared against nothing, and lowering the "
                    "capability to A — the move pass D refuses — would commit unasked."
                ),
            ),
        ),
    ),
    "consistency-violation-partition-on-unqualified-core": Seed(
        reason=(
            "A core the partition can move to that is declared but rated `QM`, and a partition rated above it. "
            "`PARTITION0` is seeded at `asil: B` on the unrated `CORE0`, so the baseline is valid; `CORE1` is "
            "seeded at `QM`, and `RAM0` admits it, so that moving `PARTITION0` there creates the placement finding "
            "and nothing else. The successor (contract `9.0.0`) of the retired "
            "`consistency-violation-task-on-unqualified-core`, which moved the task."
        ),
        edits=(
            SeedEdit(
                path="hardware/PARTITION0.yaml",
                document=PARTITION0_BASE | {"asil": "B", "coexistence": PARTITION0_ARGUMENT},
                reason=(
                    "`context.summary` states the partition rated B. Unrated, it is compared against nothing, and "
                    "the change set this vector refuses is accepted. Its `coexistence` argument answers "
                    "`safety.component-below-partition` for `ExampleConsumerSWC`, whose derived `QM` is below the "
                    "partition's `B`: without it the baseline is not valid and the move would "
                    "not be the vector's one finding."
                ),
            ),
            SeedEdit(
                path="hardware/CORE1.yaml",
                document=CORE1_SEEDED | {"asil_capability": "QM"},
                reason=(
                    "`context.existing_elements` states hardware/CORE1. Declared — so the reference resolves and "
                    "`hardware.partition-undeclared-core` is not what refuses — and rated `QM`, which hosts no ASIL."
                ),
            ),
            SeedEdit(
                path="hardware/RAM0.yaml",
                document=RAM0_ADMITTING_CORE1,
                reason=(
                    "`ExampleConsumerSWC` budgets `RAM0`, so without `CORE1` in `accessible` the move would ALSO be "
                    "`hardware.unreachable-memory` — `consistency-violation-unreachable-memory.json`'s vector — and "
                    "this one would name two findings where it is about one."
                ),
            ),
        ),
    ),
    # --- the partition's level and argument, decided by comparison ---
    "update-partition-asil-lowered": Seed(
        reason=(
            "A rated partition with an argument over the components it runs, so that lowering its level leaves a "
            "clean model and the vector is about the act alone. Nothing in the base declares a level, so both "
            "components derive `QM`; `PARTITION0` is rated B, which puts `ExampleConsumerSWC` below it, and its "
            "`coexistence` answers that. `CORE0` and `RAM0` stay unrated, so nothing compares the partition's level "
            "with them."
        ),
        edits=(
            SeedEdit(
                path="hardware/PARTITION0.yaml",
                document=PARTITION0_BASE | {"asil": "B", "coexistence": PARTITION0_ARGUMENT},
                reason=(
                    "`context.summary` states the partition rated B and argued. Without the argument the derived-`QM` "
                    "consumer below it is `safety.component-below-partition` and the baseline is not valid."
                ),
            ),
        ),
    ),
    "update-partition-asil-raised": Seed(
        reason=(
            "The same shape one level lower: `PARTITION0` rated A and argued over the two `QM` components. The change "
            "set raises it to B, which refuses more and is not asked about — the non-weakening direction, with the "
            "named limit that a raise can close `safety.component-above-partition` where the core and the memories "
            "are unrated."
        ),
        edits=(
            SeedEdit(
                path="hardware/PARTITION0.yaml",
                document=PARTITION0_BASE | {"asil": "A", "coexistence": PARTITION0_ARGUMENT},
                reason=(
                    "`context.summary` states the partition rated A and argued; the argument keeps the derived-`QM` "
                    "consumer below it from being a finding on either side of the raise."
                ),
            ),
        ),
    ),
    "update-partition-coexistence": Seed(
        reason=(
            "A rated partition running a component below its level and declaring no argument: `PARTITION0` at B over "
            "`ExampleConsumerSWC`, which derives `QM` because nothing allocates a level. The baseline therefore "
            "already carries one `safety.component-below-partition` finding, and the change set writes the argument "
            "that answers it."
        ),
        edits=(
            SeedEdit(
                path="hardware/PARTITION0.yaml",
                document=PARTITION0_BASE | {"asil": "B"},
                reason="`context.summary` states the partition rated B with no argument.",
            ),
        ),
        baseline_valid=False,
        baseline_note=(
            "The baseline carries exactly one `safety.component-below-partition` finding: `PARTITION0` is rated B "
            "and runs the derived-`QM` `ExampleConsumerSWC` with no argument. Like `update-task-coexistence`, a "
            "vector about an exemption needs the finding to already stand."
        ),
    ),
    "create-partition-carrying-coexistence": Seed(
        reason=(
            "A task already mixed and argued in an unrated partition: `ExampleSWC` declares D, `ExampleConsumerSWC` "
            "derives `QM`, `Task_10ms` runs both and carries its own execution-time argument, and `PARTITION0` is "
            "unrated, so the baseline reports nothing. The change set creates a rated partition ALREADY carrying a "
            "memory-mixing argument and moves the task into it — the shape no address arm reads, which the "
            "comparison decides."
        ),
        edits=(
            SeedEdit(
                path="swc/example/ExampleSWC.yaml",
                document=_example_swc(({"type": "timing", "period": 0.01},)) | {"asil": "D"},
                reason="One of the two levels the task runs; declared, because nothing allocates one.",
            ),
            SeedEdit(
                path="tasks/Task_10ms.yaml",
                document=_TASK_10MS_BOTH_RUNNABLES | {"coexistence": TASK_10MS_ARGUMENT},
                reason=(
                    "A task cannot mix what it does not run, and the base task runs one runnable; its argument "
                    "answers `safety.task-mixed-asil`, which is the TASK's rule and is not what this vector is about."
                ),
            ),
        ),
    ),
    "update-task-into-argued-partition": Seed(
        reason=(
            "The going-live shape: an argued, rated partition that runs nothing, and a mixed task beside it. "
            "`PARTITION1` is rated D on `CORE0` with an argument and is empty, so the argument answers nothing; "
            "`Task_10ms` runs `ExampleSWC` (D) and `ExampleConsumerSWC` (derived `QM`) in the unrated `PARTITION0`, "
            "with its own argument. The change set moves the task into `PARTITION1`, which makes the standing "
            "argument answer a finding without a single edit to the argument."
        ),
        edits=(
            SeedEdit(
                path="swc/example/ExampleSWC.yaml",
                document=_example_swc(({"type": "timing", "period": 0.01},)) | {"asil": "D"},
                reason="One of the two levels the task runs; declared, because nothing allocates one.",
            ),
            SeedEdit(
                path="tasks/Task_10ms.yaml",
                document=_TASK_10MS_BOTH_RUNNABLES | {"coexistence": TASK_10MS_ARGUMENT},
                reason=(
                    "A task cannot mix what it does not run; its argument answers `safety.task-mixed-asil`, the task's "
                    "rule, so the baseline is valid."
                ),
            ),
            SeedEdit(
                path="hardware/PARTITION1.yaml",
                document={
                    "name": "PARTITION1",
                    "kind": "partition",
                    "core": "CORE0",
                    "asil": "D",
                    "coexistence": PARTITION1_ARGUMENT,
                    "description": "A second partition on CORE0, rated D and argued, running nothing yet.",
                },
                reason=(
                    "`context.existing_elements` states hardware/PARTITION1. Rated D, so the task's D component is not "
                    "above it, and argued, so the derived-`QM` one is not a finding once it arrives."
                ),
            ),
        ),
    ),
    # --- the partition's trust class, decided by comparison ---
    "update-partition-trust-granted": Seed(
        reason=(
            "The base as it stands: `PARTITION0` is the one partition, unrated and `non_trusted` by default, on the "
            "unrated `CORE0`. No partition declares a level, so neither trust rule can fire on either side of the "
            "grant, and the vector is about the act alone."
        ),
    ),
    "create-partition-trusted": Seed(
        reason=(
            "The base as it stands: the partition created is unrated beside the unrated `PARTITION0`, so no level "
            "stands for either trust rule to compare it against, and the outcome is clean."
        ),
    ),
    "update-partition-trust-protected": Seed(
        reason=(
            "A trusted partition with nothing rated beside it, so the baseline reports nothing and the vector is "
            "about the declaration that leaves `trusted`."
        ),
        edits=(
            SeedEdit(
                path="hardware/PARTITION0.yaml",
                document=PARTITION0_BASE | {"trust": "trusted"},
                reason=(
                    "`context.summary` states the partition `trusted` — the `before` of the expected delta. It is "
                    "unrated and alone, so `safety.trusted-partition-unrated` has no level above `QM` beside it."
                ),
            ),
        ),
    ),
    "update-partition-trust-confined": Seed(
        reason=(
            "The base as it stands: `PARTITION0` is `non_trusted` by default, and the change set moves it to the "
            "other confined class, which the guard does not ask about."
        ),
    ),
    "consistency-violation-memory-below-component": Seed(
        reason=(
            "A component rated D that declares a budget of `0` for a memory rated B — silent, because `0` declares "
            "no use — and the change set gives it a real budget. `ExampleSWC` declares its level itself and the "
            "requirement allocates nothing, so `ExampleConsumerSWC` derives `QM` and its own non-zero budget on "
            "`RAM0` is below the capability and silent too; the baseline is valid."
        ),
        edits=(
            SeedEdit(
                path="swc/example/ExampleSWC.yaml",
                document=_example_swc(({"type": "timing", "period": 0.01},)) | {"asil": "D", "memory": {"RAM0": 0}},
                reason=(
                    "`context.summary` states `ExampleSWC` at D with a zero budget on `RAM0`. Declared rather than "
                    "derived, so the requirement stays unallocated and the consumer stays below the memory."
                ),
            ),
            SeedEdit(
                path="hardware/RAM0.yaml",
                document=RAM0_BASE | {"asil_capability": "B"},
                reason="`context.existing_elements` states hardware/RAM0 carrying a capability below the component.",
            ),
        ),
    ),
    "create-and-remove-duplicate-runnable-events": Seed(
        reason=(
            "The change set removes the 10 ms occurrence TWICE and adds a second 20 ms one, so the "
            "baseline has to hold two 10 ms events and one 20 ms event — the multiset case."
        ),
        edits=(
            SeedEdit(
                path="swc/example/ExampleSWC.yaml",
                document=_example_swc(
                    [
                        {"type": "timing", "period": 0.01},
                        {"type": "timing", "period": 0.01},
                        {"type": "timing", "period": 0.02},
                    ]
                ),
                reason=(
                    "`context.existing_elements` states both a 0.01 and a 0.02 occurrence, and the two "
                    "removes take two 0.01 occurrences."
                ),
            ),
        ),
    ),
    "rename-swc-via-remove-create-and-reference-updates": Seed(
        reason="ExampleSWC, its connector and its requirement are all in the base.",
    ),
    "remove-memory-resource-with-referrers-cleared": Seed(
        reason="RAM0 and the two SWC memory entries are all in the base.",
    ),
    # --- negatives --------------------------------------------------------
    "envelope-change-set-without-baseline": Seed(
        reason="Refused by the envelope; the baseline model is irrelevant to the outcome.",
    ),
    "envelope-storage-path-as-address": Seed(
        reason="Refused by the envelope; the baseline model is irrelevant to the outcome.",
    ),
    "envelope-unknown-rename-operation": Seed(
        reason="ExampleSWC is in the base; the request is refused by the envelope regardless.",
    ),
    "traversal-identity-read-address": Seed(
        reason="`layers_present` lists all fifteen; the traversal is refused by the envelope regardless.",
    ),
    "traversal-identity-create-address": Seed(
        reason="float32_base is in the base; the traversal is refused by the envelope regardless.",
    ),
    "traversal-placement-value-create-payload": Seed(
        reason=(
            "`layers_present` lists all fifteen; ExampleSWC is in the base and SpeedSensorSWC is absent, "
            "so the only thing wrong with the change set is its placement value."
        ),
    ),
    "broken-reference-read-absent-element": Seed(
        reason=(
            "`layers_present` lists all fifteen; the base's swc layer holds ExampleSWC and "
            "ExampleConsumerSWC and no SpeedSensorSWC, which is the miss the read states."
        ),
    ),
    "unknown-field-swc-safety-level": Seed(
        reason="ExampleSWC is in the base.",
    ),
    "unknown-field-in-update-payload": Seed(
        reason="ExampleSWC and its pOut port are both in the base, so every address step resolves.",
    ),
    "immutable-field-runnable-renamed-by-collection-update": Seed(
        reason="ExampleSWC and its ExampleRun runnable are both in the base.",
    ),
    "immutable-field-memory-kind-discriminator": Seed(
        reason="RAM0 and the memory budget that names it are both in the base.",
    ),
    "identity-conflict-duplicate-data-type": Seed(
        reason="uint16_base is in the base — which is exactly the identity the create collides with.",
    ),
    "create-at-occupied-storage-target": Seed(
        reason=(
            "The vector needs a file whose basename is NOT the name it declares, which is the one form of "
            "identity/file-name divergence that is filesystem-independent: `data_types/implementation/Speed_kph.yaml` "
            "holds `VehicleSpeed_kph`, so the identity `Speed_kph` is free while its plain file name is held and the "
            "create takes the nearest free collision suffix. The other form — two identities differing only in case — "
            "reproduces the same condition only on a case-insensitive filesystem, and a seed that put two case-variant "
            "file names in one directory would be refused by `storage.filename-case-collision` on a case-sensitive "
            "one, so it cannot be an acceptance fixture."
        ),
        edits=(
            SeedEdit(
                path="data_types/implementation/Speed_kph.yaml",
                document=VEHICLE_SPEED_KPH,
                reason=(
                    "`context.occupied_targets` states implementation_types/Speed_kph, and `context.existing_elements` "
                    "states the element the file actually declares."
                ),
            ),
        ),
    ),
    "update-at-relocated-storage-target": Seed(
        reason=(
            "The same divergence the `create` vector needs, seen from the verb that finds a file rather than "
            "allocating one: `data_types/implementation/Speed_kph.yaml` declares `VehicleSpeed_kph`, so the element is "
            "addressable while its file is named otherwise, and the update is written into that file."
        ),
        edits=(
            SeedEdit(
                path="data_types/implementation/Speed_kph.yaml",
                document=VEHICLE_SPEED_KPH,
                reason=(
                    "`context.occupied_targets` states implementation_types/Speed_kph, and `context.existing_elements` "
                    "states the element the file actually declares."
                ),
            ),
        ),
    ),
    "identity-conflict-same-identity-group-move": Seed(
        reason="ExampleSWC, IExample and RAM0 are all in the base.",
    ),
    "schema-violation-collection-omitted-by-member-update": Seed(
        reason="ExampleSWC, its ExampleRun runnable and that runnable's timing event are all in the base.",
    ),
    "schema-violation-negative-wcet": Seed(
        reason="Task_10ms is in the base.",
    ),
    "additional-violations-unknown-field-and-immutable-field": Seed(
        reason="ExampleSWC, CORE0 and uint16_base are all in the base.",
    ),
    "partial-change-set-update-before-create": Seed(
        reason=(
            "SpeedSensorSWC must be ABSENT — the update names it before the create supplies it — while "
            "IVehicleSpeed and RAM must be present."
        ),
        edits=(
            SeedEdit(
                path="interfaces/IVehicleSpeed.yaml",
                document=IVEHICLE_SPEED_SEEDED,
                reason="`context.existing_elements` states interfaces/IVehicleSpeed.",
            ),
        ),
    ),
    "broken-reference-remove-referenced-interface": Seed(
        reason="IExample and the two ports that name it are all in the base.",
    ),
    "consistency-violation-connector-interface-mismatch": Seed(
        reason=(
            "The same bare base as the accepted half, `create-connector-between-compatible-interfaces`, and "
            "for the same reason: the two interfaces are the change set's own, so the fixture differs from "
            "that vector's in nothing at all and the ONE difference between the two exchanges is the data "
            "element the required interface asks for."
        ),
    ),
    "invalid-identifier-emitted-compu-method-overflow": Seed(
        reason="The enum it creates references nothing; `context` states no existing element at all.",
    ),
    "broken-reference-variable-access-unknown-variable": Seed(
        reason=(
            "The bare base: ExampleSWC and ExampleRun are in it and the component declares no variable, so the "
            "one defect is the access naming a variable nobody declares."
        ),
    ),
    "broken-reference-exclusive-area-unknown-area": Seed(
        reason=(
            "The bare base: ExampleSWC and ExampleRun are in it and the component declares no exclusive area, so "
            "the one defect is the entry naming an area nobody declares."
        ),
    ),
    "broken-reference-data-event-unknown-element": Seed(
        reason=(
            "The bare base: ExampleConsumerSWC's rIn requires IExample, which declares only value, so the one "
            "defect is the event naming an element its port's interface does not declare."
        ),
    ),
    "broken-reference-call-returned-event-unknown-call": Seed(
        reason=(
            "The bare base: no runnable of ExampleConsumerSWC calls anything, so the one defect is the "
            "call_returned event naming a pair no runnable of its component calls asynchronously."
        ),
    ),
    "consistency-violation-asynchronous-call-without-result": Seed(
        reason=(
            "The bare base, whose manifest states no arxml_version, so the project is judged at 4.2.2: "
            "IDiagnosticService declares ReadDtc, and the change set creates the port and the asynchronous call, "
            "so the one defect is the call no result point references (swc.call.async-without-result)."
        ),
    ),
    "invalid-identifier-operation-event-pair-overflow": Seed(
        reason=(
            "ExampleSWC and IDiagnosticService are in the base; the change set declares the operation, the port "
            "and the runnable the over-budget event names, so the one defect is the generated OIT_ name."
        ),
    ),
    "consistency-violation-second-root": Seed(
        reason=(
            "The bare base: `Root` is the one composition declaring `root: true`, and `Root2` is what the change "
            "set creates with the same flag. The exactly-one rule (`composition.multiple-roots`) is about the "
            "assembled result, so the baseline is valid and the outcome is not. An SWC no requirement links "
            "to is no refusal (pass D never sees it), so the tenth code's vector needs a rule that still "
            "refuses."
        ),
    ),
    "stale-baseline-change-set": Seed(
        reason="Task_10ms is in the base; the staleness is produced by `make_stale` after the token is read.",
    ),
    "unauthorized-delta-empty-approved-scope": Seed(
        reason="ExampleConsumerSWC and its connector are in the base.",
    ),
    "broken-reference-task-on-undeclared-partition": Seed(
        reason=(
            "`PARTITION1` must be ABSENT — the task is repointed at a partition the model does not declare — while "
            "`Task_10ms` and `PARTITION0` are both in the base."
        ),
    ),
    "consistency-violation-second-processor": Seed(
        reason=(
            "`CPU` is the base's one processor and `CPU2` is what the change set creates; the exactly-one "
            "rule is about the assembled result, so the baseline is valid and the outcome is not."
        ),
    ),
    "consistency-violation-unreachable-memory": Seed(
        reason=(
            "`CORE1` and the partition on it must be PRESENT — that is the whole difference from "
            "`broken-reference-task-on-undeclared-partition`, whose fixture leaves the partition absent so the "
            "reference dangles one pass earlier. Everything else the vector needs is in the base: `Task_10ms` in "
            "`PARTITION0` on `CORE0`, `ExampleConsumerSWC` budgeting `RAM0`, and `RAM0.accessible` naming `CORE0` "
            "alone."
        ),
        edits=(
            SeedEdit(
                path="hardware/CORE1.yaml",
                document=CORE1_SEEDED,
                reason=(
                    "`context.existing_elements` states hardware/CORE1. Without it the partition on it is refused "
                    "by `hardware.partition-undeclared-core` as a `broken-reference` and "
                    "`hardware.unreachable-memory` — the rule this vector exists to pin — is never reached, because "
                    "its own guard skips a task whose core does not resolve."
                ),
            ),
            SeedEdit(
                path="hardware/PARTITION1.yaml",
                document=PARTITION1_SEEDED,
                reason=(
                    "`context.existing_elements` states hardware/PARTITION1, the partition the change set moves the "
                    "task into (contract `9.0.0`: a task reaches a core through a partition). Without it the change "
                    "set is refused by `hardware.undeclared-partition` one pass earlier."
                ),
            ),
        ),
    ),
    # --- the two codes the reference-role family divides between ---
    "reference-role-value-type-base-in-another-layer": Seed(
        reason=(
            "A reference type the change set's value type can name. The base declares twelve base types, the twelve "
            "value types over them and no reference type, so without one the only `base` on offer that resolves in "
            "`implementation_types` would be a value type. `Float32Ref` over `float32` is itself legal, so the "
            "baseline is valid and the change set creates the finding: since contract `8.0.0` a value type's `base` is "
            "looked up in `base_types` alone, where `Float32Ref` is not."
        ),
        edits=(
            SeedEdit(
                path="data_types/implementation/Float32Ref.yaml",
                document={"name": "Float32Ref", "kind": "reference_type", "base": "float32"},
                reason=(
                    "`context.existing_elements` states implementation_types/Float32Ref, the name the value "
                    "type's `base` names, declared in a layer other than the one that `base` is resolved in."
                ),
            ),
        ),
    ),
    "reference-role-value-type-undeclared-base": Seed(
        reason=(
            "None: the bare base IS the state. It declares `float32_base` and no `float16_base`, and the change set "
            "names the second — a name that resolves to nothing in any layer."
        ),
    ),
    # --- the mapping set layer, its three verbs and its pass-D refusal ---
    "create-application-type-and-mapping-set": Seed(
        reason=(
            "The bare base, and deliberately so. `float32` is the base's own and both elements the change set "
            "creates must be absent: `data_types/mapping_sets/` is present and empty in `examples/basic`, "
            "which is the state a project with no physical type is in and the one the vector's summary describes."
        ),
    ),
    "update-and-remove-mapping-sets": Seed(
        reason=(
            "Two application types and two mapping sets, none of which the base declares. `PhysicalMapping` holds "
            "the pair the change set re-points, `EngineSpeed_Rpm` is the type the added pair maps, and "
            "`LegacyMapping` is the element the change set removes -- an empty set, which is legal and is the "
            "cheapest element to remove without a referrer to clear."
        ),
        edits=(
            SeedEdit(
                path="data_types/application/VehicleSpeed_Kph.yaml",
                document=VEHICLE_SPEED_KPH_PHYSICAL,
                reason=(
                    "`context.existing_elements` states application_types/VehicleSpeed_Kph, the pair's application "
                    "side."
                ),
            ),
            SeedEdit(
                path="data_types/application/EngineSpeed_Rpm.yaml",
                document=ENGINE_SPEED_RPM,
                reason=(
                    "`context.existing_elements` states application_types/EngineSpeed_Rpm. Without it the created "
                    "pair's "
                    "`application` resolves to nothing and `mapping-set.unknown-application` refuses the change "
                    "set one pass earlier than the verbs this vector is about."
                ),
            ),
            SeedEdit(
                path="data_types/mapping_sets/PhysicalMapping.yaml",
                document=PHYSICAL_MAPPING,
                reason="`context.existing_elements` states the set and the pair inside it that the update re-points.",
            ),
            SeedEdit(
                path="data_types/mapping_sets/LegacyMapping.yaml",
                document=LEGACY_MAPPING,
                reason="`context.existing_elements` states mapping_sets/LegacyMapping, the element removed.",
            ),
        ),
    ),
    # The three implementation kinds and a composite map through the generic
    # engine, and `[constr_1066]`'s refusal of a map onto a pointer.
    "create-implementation-composites-and-composite-map": Seed(
        reason=(
            "The bare base, and deliberately so. `uint16` is the base's own and all six elements the change set "
            "creates must be absent; no port speaks the new application types, so nothing must be mapped and the "
            "record's pair is one a map may name."
        ),
    ),
    "consistency-violation-mapping-set-pointer-mapped": Seed(
        reason=(
            "The bare base: the change set creates the pointer, the application type and the pair, and `uint16`, "
            "the pointer's target, is the base's own — so the one defect is the map onto the pointer."
        ),
    ),
    "consistency-violation-application-type-unmapped": Seed(
        reason=(
            "The whole pairing has to stand in the baseline for the change set to break it: the application type, "
            "the set that pairs it, an interface typed by it, and a component with a port on that interface naming "
            "the set. The baseline is valid -- every port is covered -- and removing the one entry is what creates "
            "the finding."
        ),
        edits=(
            SeedEdit(
                path="data_types/application/VehicleSpeed_Kph.yaml",
                document=VEHICLE_SPEED_KPH_PHYSICAL,
                reason="`context.existing_elements` states application_types/VehicleSpeed_Kph, the unmapped type.",
            ),
            SeedEdit(
                path="data_types/mapping_sets/PhysicalMapping.yaml",
                document=PHYSICAL_MAPPING,
                reason=(
                    "`context.existing_elements` states mapping_sets/PhysicalMapping. It stays declared through the "
                    "change set: what is removed is the component's REFERENCE to it, so nothing dangles and the "
                    "refusal is the coverage rule rather than `swc.unknown-mapping-set`."
                ),
            ),
            SeedEdit(
                path="interfaces/IPhysicalSpeed.yaml",
                document=IPHYSICAL_SPEED,
                reason=(
                    "`context.existing_elements` states interfaces/IPhysicalSpeed. The rule reads the types a "
                    "port's interface puts on the bus, so a port alone would reach nothing."
                ),
            ),
            SeedEdit(
                path="swc/example/ExampleSWC.yaml",
                document=EXAMPLE_SWC_SPEAKING_A_PHYSICAL_TYPE,
                reason=(
                    "`context.existing_elements` states the component's `data_type_mappings` entry. The base's "
                    "`ExampleSWC` carries neither the port nor the entry."
                ),
            ),
        ),
    ),
    # The two external stub kinds, written through the generic engine, and their
    # three cross-element rules.
    "create-external-interface-and-type-stubs": Seed(
        reason=(
            "The bare base, and deliberately so. `ExampleConsumerSWC` and its port `rIn` are the base's own, and "
            "both stubs the change set creates must be absent — a project that has never met a reference into "
            "another file is the state the vector's summary describes."
        ),
    ),
    "consistency-violation-interface-external-incomplete-reference": Seed(
        reason=(
            "The bare base: the change set creates the one element the rule judges, and a stub nothing names is "
            "judged all the same, so no referrer has to be seeded."
        ),
    ),
    "consistency-violation-data-type-external-incomplete-reference": Seed(
        reason=(
            "The bare base, for the interface twin's reason: the stub the change set creates is the whole of what "
            "the rule reads."
        ),
    ),
    "consistency-violation-interface-init-on-external-type": Seed(
        reason=(
            "One implementation-type stub the base does not declare. The change set creates the interface whose "
            "data element is typed by it and carries an `init`, so the stub has to stand in the baseline for the "
            "type reference to resolve and for the refusal to be the `init` rule rather than an undeclared type."
        ),
        edits=(
            SeedEdit(
                path="data_types/implementation/ForeignSpeed_T.yaml",
                document=FOREIGN_SPEED_TYPE_STUB,
                reason=(
                    "`context.existing_elements` states implementation_types/ForeignSpeed_T, the stub the element "
                    "names."
                ),
            ),
        ),
    ),
    # The data description layers end to end: four reads of one model, one per
    # type layer and the mapping sets, so one name answered by two layers is
    # visible as two answers.
    **{
        f"read-{layer.replace('_', '-')}-layer": Seed(
            reason=(
                "The base plus one name declared in two layers: an application type and an implementation type "
                "both called `SyntheticTorque`, and the mapping set that pairs them. The four layer reads share this "
                "seed, so they are four answers about one model."
            ),
            edits=_SAME_NAME_PAIR_EDITS,
        )
        for layer in ("application_types", "implementation_types", "base_types", "mapping_sets")
    },
    "create-same-name-application-and-implementation-types": Seed(
        reason=(
            "The bare base, and deliberately so: `float32_base` is the base's own, and every element the change set "
            "creates must be absent, the shared name in both layers included."
        ),
    ),
    "schema-violation-data-element-both-type-fields": Seed(
        reason=(
            "One application type the base does not declare, so that both type fields of the data element name a "
            "declared type and the refusal is the exactly-one rule rather than an undeclared type."
        ),
        edits=(_SAME_NAME_PAIR_EDITS[0],),
    ),
    "schema-violation-data-element-no-type-field": Seed(
        reason="The bare base: the data element names no type at all, so nothing has to be declared for it.",
    ),
    "reference-role-data-element-type-in-another-layer": Seed(
        reason=(
            "One application type the base does not declare, and no implementation type of its name: the data "
            "element's `implementation_type` names it, so the name is declared, but in the other layer."
        ),
        edits=(_SAME_NAME_PAIR_EDITS[0],),
    ),
    "consistency-violation-retired-data-types-directory": Seed(
        reason=(
            "The base plus one element file in each directory contract `8.0.0` retired: an implementation type "
            "directly in `data_types/` and a mapping set in a `mapping_sets/` directory at the model root — the "
            "layout every release before it wrote."
        ),
        edits=(
            SeedEdit(
                path="data_types/SyntheticTorque.yaml",
                document=SYNTHETIC_TORQUE_IMPLEMENTATION,
                reason="The retired `data_types` entry of the contract's `retiredStorage` table holds an element file.",
            ),
            SeedEdit(
                path="mapping_sets/SyntheticTorqueMapping.yaml",
                document={**SYNTHETIC_TORQUE_MAPPING, "maps": []},
                reason=(
                    "The retired `mapping_sets` entry holds an element file too, so the refusal names both retired "
                    "directories."
                ),
            ),
        ),
        baseline_valid=False,
        baseline_note=(
            "A model on a retired layout is not judged at all: every read of it is refused, the consistency check's "
            "included, because an element file the layers do not read would otherwise be silently left out. The "
            "refusal IS the state the exchange is written to describe."
        ),
        retired_layout=True,
    ),
    # The data description elements end to end: the three element layers read,
    # the elements created with the types naming them, the form rules, the
    # undeclared targets and a removal that leaves one.
    **{
        name: Seed(
            reason=(
                "The base plus the seven data description elements and types: a unit, a linear method naming it, a "
                "texttable, a data constraint, an application type naming the unit, the method and the constraint, "
                "and an enumeration and a value type sharing the table. The element reads and the removal share "
                "this seed, so they are answers about one model."
            ),
            edits=_DATA_DESCRIPTION_ELEMENT_EDITS,
        )
        for name in (
            "read-units-layer",
            "read-compu-methods-layer",
            "read-data-constraints-layer",
            "read-compu-methods-texttable-element",
            "consistency-violation-remove-unit-elements-name",
        )
    },
    "create-unit-linear-and-application-type": Seed(
        reason=(
            "The bare base, and deliberately so: the base declares no element of the three element layers, and "
            "every element the change set creates must be absent."
        ),
    ),
    "create-texttable-enum-and-value-type": Seed(
        reason=(
            "The bare base: `uint8_base` is the base's own, and the table, the enumeration and the value type the "
            "change set creates must be absent."
        ),
    ),
    "schema-violation-application-type-unit-and-unit-ref": Seed(
        reason=(
            "One unit the base does not declare, so that the type's `unit_ref` names a declared unit and the "
            "refusal is the form rule rather than an undeclared target."
        ),
        edits=(_DATA_DESCRIPTION_ELEMENT_EDITS[0],),
    ),
    "schema-violation-enum-literals-and-compu-method-ref": Seed(
        reason=(
            "One texttable the base does not declare, so that the enumeration's `compu_method_ref` names a "
            "declared table and the refusal is the form rule rather than an undeclared target."
        ),
        edits=(_DATA_DESCRIPTION_ELEMENT_EDITS[2],),
    ),
    "consistency-violation-undeclared-data-description-elements": Seed(
        reason=(
            "The bare base: it declares no element of the three element layers, so every reference the change set "
            "states names nothing."
        ),
    ),
    # Stated names and stub members: a stated behaviour and event
    # name, a stub listing the operation a call names, and the two refusals the
    # names and the lists add.
    "update-swc-stated-behaviour-and-event-names": Seed(
        reason=(
            "The bare base: `ExampleSWC` and its runnable `ExampleRun` are the base's own, and neither states a "
            "name, so the change set states the first behaviour and event names."
        ),
    ),
    "create-interface-stub-listing-operations-with-a-call": Seed(
        reason=(
            "The bare base: `ExampleConsumerSWC` and its runnable `ExampleConsumerRun` are the base's own, and the "
            "stub, the port and the call the change set creates must be absent."
        ),
    ),
    "consistency-violation-duplicate-event-name": Seed(
        reason=(
            "The bare base: the name the writer derives for `ExampleRun`'s one timing event, `TMT_ExampleRun`, is "
            "what the created event states, so nothing has to be seeded."
        ),
    ),
    "schema-violation-external-interface-member-of-wrong-kind": Seed(
        reason="The bare base: the stub the change set creates is the whole of what the rule reads.",
    ),
    # An `init` under an implementation type that references a table is the name
    # of one of the table's entries, and a number there is refused. One baseline
    # for both exchanges, so they are two answers about one model.
    **{
        name: Seed(
            reason=(
                "The base plus the texttable and the implementation value type naming it: the change set creates "
                "an interface whose data element is typed by the value type and states an `init`, so the type and "
                "its table have to stand in the baseline for the value to be judged against the table's entries."
            ),
            edits=(_DATA_DESCRIPTION_ELEMENT_EDITS[2], _DATA_DESCRIPTION_ELEMENT_EDITS[6]),
        )
        for name in ("create-interface-init-by-table-entry", "consistency-violation-interface-init-number-under-table")
    },
    # --- the instance identity ---
    "update-sequence-step-note-at-instance-address": Seed(
        reason=(
            "The base's one sequence step written with the other selector: `instance: ExampleConsumerSWC`, the root "
            "member that places `ExampleConsumerSWC` once. `Task_10ms` keeps its `swc` entry, so the baseline is valid "
            "only because the two selectors resolve to one scheduling identity."
        ),
        edits=(
            SeedEdit(
                path="sequences/task10ms_consumer_cycle.yaml",
                document={
                    "name": "task10ms_consumer_cycle",
                    "task": "Task_10ms",
                    "title": "Consumer task execution cycle",
                    "steps": [{"instance": "ExampleConsumerSWC", "runnable": "ExampleConsumerRun"}],
                },
                reason=(
                    "`context.summary` states the step written `instance: ExampleConsumerSWC`; the base writes it "
                    "with `swc`."
                ),
            ),
        ),
    ),
    "create-task-runnable-at-two-key-address": Seed(
        reason="`Task_10ms`, `ExampleSWC.ExampleRun` and its 10 ms timing event are all in the base.",
    ),
    "schema-violation-task-runnable-both-selectors": Seed(
        reason="`Task_10ms`, `ExampleSWC.ExampleRun` and the root member `ExampleSWC` are all in the base.",
    ),
    "schema-violation-sequence-step-without-runnable": Seed(
        reason="`Task_10ms` is in the base, and no scenario of the name the change set creates.",
    ),
}


# ---------------------------------------------------------------------------
# Vector loading.
# ---------------------------------------------------------------------------


def load_vectors() -> dict[str, dict[str, Any]]:
    """Every shipped vector, keyed by its stable ``name``.

    ``name`` and not the file name: the name is a vector's stable handle, and
    ``test_editing_protocol_contracts`` pins that names are unique across both
    directories.
    """
    return {_load(path)["name"]: _load(path) for path in ALL_VECTOR_FILES}


VECTORS: dict[str, dict[str, Any]] = load_vectors()


def with_the_summary_defaults_restored(layers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*layers* of a ``summary`` answer with ``false`` written back where it omits one.

    The ``summary`` omits a layer's ``optional`` and a field's ``required`` and
    ``immutable`` where they are ``false`` (absent means ``false``). The three
    positions are WRITTEN OUT BY HAND and never read from
    ``discovery.SUMMARY_OMITTED_DEFAULTS``: the oracles that call this restore
    what the table dropped, so a helper built from the table would pass a
    widened table by construction. Widening it has to fail there first.
    """
    restored = deepcopy(layers)
    for layer in restored:
        layer.setdefault("optional", False)
        for element_kind in layer["element_kinds"]:
            for field in element_kind["fields"]:
                field.setdefault("required", False)
                field.setdefault("immutable", False)
    return restored


# ---------------------------------------------------------------------------
# Fixture construction.
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(dict(document), default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n"
    )


def build_fixture(destination: Path, vector_name: str) -> Path:
    """Materialize *vector_name*'s baseline project at *destination*.

    Args:
        destination: a directory that does not exist yet; it becomes the project
            root (the manifest sits directly inside it, the model under
            ``model/``).
        vector_name: the vector's stable ``name``.

    Returns:
        The project root.

    Raises:
        KeyError: if the vector has no seed entry. Every vector needs one, so a
            newly shipped vector cannot silently run against the bare base.
    """
    seed = SEEDS[vector_name]
    destination.mkdir(parents=True)
    model_root = destination / "model"
    shutil.copytree(BASE_MODEL_ROOT, model_root)

    manifest = yaml.safe_load(BASE_MANIFEST.read_text(encoding="utf-8"))
    manifest["name"] = seed.project_name
    _write_yaml(destination / "swcraft.yaml", manifest)

    for layer in seed.absent_layers:
        shutil.rmtree(model_root / LAYER_DIRECTORIES[layer])

    for edit in seed.edits:
        target = model_root / edit.path
        if edit.document is None:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        else:
            _write_yaml(target, edit.document)

    return destination


def make_stale(project: Path) -> None:
    """Move the model after its token has been read (``context.baseline_state: "stale"``).

    The edit is valid and unrelated to the vector's own operations, so the change
    set fails for staleness rather than incidentally for something else.
    """
    path = project / "model" / "swc" / "example" / "ExampleConsumerSWC.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["description"] = f"{document['description']} (moved after the baseline token was read)"
    _write_yaml(path, document)


def model_state_key(model_root: Path) -> str:
    """A digest of the whole model tree — two fixtures agree iff this agrees.

    The relational half of the token contract is stated over *model states*, so
    the harness needs its own notion of one that does not come from the engine.
    """
    digest = hashlib.sha256()
    for path in sorted((p for p in model_root.rglob("*") if p.is_file()), key=lambda path: path.as_posix()):
        digest.update(path.relative_to(model_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Address resolution against a built fixture.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving one address against a fixture.

    ``occurrences`` is a count, not a boolean: a ``whole_value`` list member can
    be held more than once.
    """

    found: bool
    value: Any = None
    occurrences: int = 0
    missing_step: str = ""


def _member_matches(item: Any, identity: Any, identity_field: str = "name") -> bool:
    """Whether *item* is the collection member *identity* names.

    Three strategies, exactly the three ``collectionDescriptor.member_identity``
    publishes: a string identity against a member carrying *identity_field* is
    the ``field`` strategy; anything else is compared by canonical value
    equality, which covers both ``composite`` keys (``{swc, runnable}``) and
    ``whole_value`` members (a ``links`` string, an ``events`` object).
    *identity_field* is ``name`` for every collection but those
    :data:`MEMBER_IDENTITY_FIELDS` lists.
    """
    if isinstance(identity, str) and isinstance(item, Mapping) and identity_field in item:
        return item[identity_field] == identity
    return _hashable(item) == _hashable(identity)


def _element_documents(model_root: Path, layer: str) -> list[tuple[Path, dict[str, Any]]]:
    directory = model_root / LAYER_DIRECTORIES[layer]
    if not directory.is_dir():
        return []
    paths = sorted(directory.rglob("*.yaml") if layer in NESTED_LAYERS else directory.glob("*.yaml"))
    return [(path, yaml.safe_load(path.read_text(encoding="utf-8"))) for path in paths]


def _composed_target(model_root: Path, address: Mapping[str, Any]) -> Path:
    """The file the layer's composition rule puts one element address at.

    A glob for the one nested layer: whether the place is taken has the same
    answer whichever ``swc/{group}/`` holds it. A path that exists nowhere is
    returned un-nested, so the caller's ``.exists()`` is false.
    """
    directory = model_root / LAYER_DIRECTORIES[address["layer"]]
    filename = f"{address['element']}.yaml"
    if address["layer"] in NESTED_LAYERS:
        matches = sorted(directory.glob(f"*/{filename}"), key=lambda path: path.as_posix())
        if matches:
            return matches[0]
    return directory / filename


def resolve(model_root: Path, address: Mapping[str, Any]) -> Resolution:
    """Resolve *address* against the YAML tree under *model_root*, independently of ``editing/``."""
    layer = address["layer"]
    if layer not in LAYER_DIRECTORIES:
        return Resolution(found=False, missing_step=f"layer {layer!r}")
    if not (model_root / LAYER_DIRECTORIES[layer]).is_dir():
        return Resolution(found=False, missing_step=f"layer {layer!r} is absent")

    element = address.get("element")
    if element is None:
        return Resolution(found=True, value=[document for _path, document in _element_documents(model_root, layer)])

    identity_field = LAYER_IDENTITY_FIELDS[layer]
    matches = [
        document for _path, document in _element_documents(model_root, layer) if document.get(identity_field) == element
    ]
    if not matches:
        return Resolution(found=False, missing_step=f"element {element!r}")

    value: Any = matches[0]
    occurrences = 1
    owning_field = ""
    for step in address.get("path", []):
        if "field" in step or "key" in step:
            key = step.get("field", step.get("key"))
            if not isinstance(value, Mapping) or key not in value:
                return Resolution(found=False, missing_step=str(step))
            value = value[key]
            owning_field = key if "field" in step else ""
            occurrences = 1
            continue
        identity = step["member"]
        if not isinstance(value, list):
            return Resolution(found=False, missing_step=str(step))
        identity_field = MEMBER_IDENTITY_FIELDS.get(owning_field, "name")
        found = [item for item in value if _member_matches(item, identity, identity_field)]
        if not found:
            return Resolution(found=False, missing_step=str(step))
        value, occurrences = found[0], len(found)
    return Resolution(found=True, value=value, occurrences=occurrences)


# ---------------------------------------------------------------------------
# Preconditions: `context` is checked, never generated from.
# ---------------------------------------------------------------------------


def _is_well_formed_request(vector: Mapping[str, Any]) -> bool:
    """Whether the vector's ``input`` is a request the address model can parse.

    The ``protocol-schema`` negatives deliberately carry inputs it cannot, so the
    create-address check would fail on the malformedness they exist to state.
    """
    if vector["kind"] == "example":
        return True
    return vector["expected"].get("violation_layer") == "semantic"


def check_preconditions(project: Path, vector: Mapping[str, Any]) -> None:
    """Assert the built fixture really is the baseline the vector states.

    Four properties, all failures of the fixture rather than of the engine:

    * every layer in ``context.layers_present`` exists and no other does (when
      the vector states the key);
    * every address in ``context.existing_elements`` resolves;
    * every address in ``context.occupied_targets`` resolves to no element while
      a file holds its plain composed file name (such a ``create`` is accepted:
      the new file takes a collision suffix);
    * every ``create`` address is tri-state: absent is the accepted case, present
      must be an ``identity-conflict`` vector, and present in a ``whole_value``
      list is legal either way.

    Raises:
        AssertionError: naming the vector and the property that does not hold.
    """
    name = vector["name"]
    model_root = project / "model"
    context = vector["context"]

    if "layers_present" in context:
        present = set(context["layers_present"])
        for layer, directory in LAYER_DIRECTORIES.items():
            exists = (model_root / directory).is_dir()
            if layer in present:
                assert exists, f"{name}: `layers_present` states {layer!r}, but the fixture has no such layer"
            else:
                assert not exists, f"{name}: `layers_present` omits {layer!r}, but the fixture has it"

    for element in context.get("existing_elements", []):
        assert resolve(model_root, element).found, (
            f"{name}: `context.existing_elements` states {element}, which the seeded fixture does not hold"
        )

    for element in context.get("occupied_targets", []):
        assert not resolve(model_root, element).found, (
            f"{name}: `context.occupied_targets` states {element}, but an element of that layer DECLARES that "
            "identity — which makes it an `existing_elements` entry and a different precondition"
        )
        assert _composed_target(model_root, element).exists(), (
            f"{name}: `context.occupied_targets` states {element}, but nothing occupies the place the layer's "
            "composition rule puts it"
        )

    if not _is_well_formed_request(vector) or vector["input"].get("operation") != "change_set":
        return

    expected = vector["expected"]
    reported = {expected.get("expected_error")} | {entry["expected_error"] for entry in expected.get("additional", [])}
    occupied = {_hashable(dict(element)) for element in context.get("occupied_targets", [])}
    for operation in vector["input"]["operations"]:
        if operation["operation"] != "create":
            continue
        address = operation["address"]
        if _addresses_a_multiset_member(address):
            # A `whole_value` list is a multiset: creating a value it already
            # holds adds a second occurrence rather than colliding, so the
            # address may legally be in either state.
            continue
        if resolve(model_root, address).found:
            assert "identity-conflict" in reported, (
                f"{name}: the fixture already holds the `create` target {address}, so this exchange can only be "
                f"an identity-conflict — but the vector expects {sorted(code for code in reported if code)}"
            )
        elif _hashable(dict(address)) in occupied:
            assert "identity-conflict" not in reported, (
                f"{name}: the `create` target {address} is in `context.occupied_targets` — its plain file name is "
                "held by another element's file while its identity is free — and the store gives "
                "the new element's file a collision suffix, so this exchange cannot be an identity-conflict"
            )


# ---------------------------------------------------------------------------
# The seam between the harness and the engine.
# ---------------------------------------------------------------------------


def dispatch(
    request: Any,
    *,
    project: Path,
    approved_scope: list[dict[str, Any]] | None,
    review: Any = None,
    discover_default: str | None = None,
) -> dict[str, Any]:
    """Send *request* to the editing engine and return its response.

    The one seam between this harness and ``editing/``. The import is inside the
    function so that an engine that fails to import is caught by the per-vector
    ``xfail`` rather than failing collection for the whole runner.

    Args:
        request: the protocol request, exactly as it goes on the wire.
        project: the project root (the directory holding ``swcraft.yaml``).
        approved_scope: ``None`` for an unscoped run, a possibly-empty list of
            ``{operation, address}`` entries for a scoped one. Empty means
            *nothing is approved*, which is not the same as unscoped.
        review: the host's review boundary, or ``None`` for an exchange in which
            no host answered (:func:`host_review`).
        discover_default: ``handle_request``'s channel keyword — the form a
            project-wide ``discover`` naming none is served in; ``None`` keeps
            the protocol's ``summary`` (:func:`discover_default_for`).
    """
    from swcraft_tools.editing.engine import handle_request

    return handle_request(
        request,
        project=project,
        approved_scope=approved_scope,
        review=review,
        discover_default=discover_default,  # type: ignore[arg-type]
    )


def as_sent(response: dict[str, Any]) -> dict[str, Any]:
    """*response* as the MCP server sends it: a whole element's removal with its ``before`` summarised.

    The projection ``swcraft_mcp.server._handle`` applies
    (``editing.delta.summarize_response``), and the form the example vectors
    record; the identity for a response without a whole-element ``remove``.
    Imported lazily for :func:`dispatch`'s reason.
    """
    from swcraft_tools.editing.delta import summarize_response

    return summarize_response(response)


#: The vectors recorded on the MCP server's channel
#: (``swcraft_mcp.server.HOST_DISCOVER_DEFAULT``) rather than the protocol's
#: default one: a vector's context has no key that could state a channel.
HOST_CHANNEL_VECTORS: Mapping[str, str] = MappingProxyType({"discover-project-baseline": "baseline"})


def discover_default_for(vector_name: str) -> str | None:
    """The ``discover_default`` keyword *vector_name* was recorded under."""
    return HOST_CHANNEL_VECTORS.get(vector_name)


def host_review(vector: Mapping[str, Any]) -> Any:
    """The host act a vector's ``expected.response.status`` states, as a callback.

    Commit and abandon are host-side acts with no request of their own, so the
    harness plays the host: ``staged`` offers no review boundary, ``committed``
    approves, ``abandoned`` declines. The decision is bound to the proposal's own
    change-set id and baseline, so it answers this change set only.
    """
    expected = vector.get("expected", {}).get("response")
    if not isinstance(expected, Mapping) or expected.get("response") != "change_set":
        return None
    status = expected.get("status")
    if status == "staged":
        return None

    def review(proposal: Any) -> Any:
        from swcraft_tools.editing.authorization import ApprovalDecision

        return ApprovalDecision(
            change_set_id=proposal.change_set_id,
            baseline=proposal.baseline,
            approved=status == "committed",
            # The safety guard is the host's second question: a committed vector
            # that weakens (removing a memory pool) says the human said yes to
            # both. The engine still refuses a weakening whenever this is False,
            # which `test_editing_engine.py`'s safety-guard class pins.
            safety_approved=status == "committed",
        )

    return review


def current_baseline_token(project: Path) -> str:
    """The fixture's real baseline token, taken through ``discover`` (the token is opaque)."""
    response = dispatch({"operation": "discover"}, project=project, approved_scope=None)
    return response["baseline"]


def baseline_token(project: Path, vector_name: str) -> str | None:
    """*vector_name*'s baseline token, or ``None`` where its seed has none.

    A seed on a retired layout (:attr:`Seed.retired_layout`) gets ``None``; its
    ``discover`` refusal is asserted, so a retired-layout model that became
    readable fails instead of quietly answering ``None``.
    """
    if not SEEDS[vector_name].retired_layout:
        return current_baseline_token(project)
    response = dispatch({"operation": "discover"}, project=project, approved_scope=None)
    assert response.get("error") == "consistency-violation", response
    assert "addresses" not in response, response
    return None


def approved_scope(vector: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """The vector's approved scope: ``None`` when unscoped, a list when scoped.

    An empty list means nothing is approved, which is not the same as unscoped.
    """
    authorization = vector["context"].get("authorization")
    if authorization is None:
        return None
    return deepcopy(authorization["approved_scope"])


def prepare_request(vector: Mapping[str, Any], token: str) -> Any:
    """The vector's ``input`` with its stand-in baseline replaced by *token*.

    A vector's ``input.baseline`` is a stand-in no real fixture produces;
    verbatim, it would fail almost every change set as ``stale-baseline``.
    """
    request = deepcopy(vector["input"])
    if isinstance(request, dict) and "baseline" in request:
        request["baseline"] = token
    return request


# ---------------------------------------------------------------------------
# The comparison contract.
# ---------------------------------------------------------------------------


def assert_response_matches(actual: Mapping[str, Any], expected: Mapping[str, Any], *, request: Any) -> None:
    """Compare an example's actual response against its expected one.

    Three fields are not compared by value, because no implementation can
    reproduce a vector's stand-in:

    * ``baseline`` — against ``$defs/revisionToken``'s pattern, and relationally:
      ``staged``/``abandoned`` return the request's token, ``committed`` another;
    * ``audit_ref`` — present exactly where the vector says, of the
      ``cs-<UTC>-<6 hex>`` form;
    * ``delta`` — by :func:`assert_delta_matches`.

    Every other field is compared by value.

    Raises:
        AssertionError: on the first field that disagrees.
    """
    actual_rest = dict(actual)
    expected_rest = dict(expected)

    actual_baseline = actual_rest.pop("baseline", None)
    expected_baseline = expected_rest.pop("baseline", None)
    assert (actual_baseline is None) == (expected_baseline is None), (
        f"the response carries baseline={actual_baseline!r} where the vector expects {expected_baseline!r}"
    )
    if actual_baseline is not None:
        assert REVISION_TOKEN_PATTERN.fullmatch(actual_baseline), (
            f"{actual_baseline!r} is not a revisionToken ({REVISION_TOKEN_PATTERN.pattern})"
        )

    actual_audit = actual_rest.pop("audit_ref", None)
    expected_audit = expected_rest.pop("audit_ref", None)
    assert (actual_audit is None) == (expected_audit is None), (
        "audit_ref is a security decision, not a convenience: it is omitted on `staged` and present on "
        f"`committed`. Got {actual_audit!r} where the vector expects {expected_audit!r}"
    )
    if actual_audit is not None:
        assert AUDIT_REF_PATTERN.fullmatch(actual_audit), (
            f"{actual_audit!r} is not a change-set id ({AUDIT_REF_PATTERN.pattern})"
        )

    actual_delta = actual_rest.pop("delta", None)
    expected_delta = expected_rest.pop("delta", None)
    assert (actual_delta is None) == (expected_delta is None), (
        f"the response carries delta={actual_delta!r} where the vector expects {expected_delta!r}"
    )
    if expected_delta is not None:
        assert_delta_matches(actual_delta, expected_delta, request=request)

    if expected.get("response") == "change_set":
        carried = request.get("baseline") if isinstance(request, Mapping) else None
        if expected["status"] == "committed":
            assert actual_baseline != carried, (
                "a committed response returns the token the commit produced, not the one the request carried"
            )
        else:
            assert actual_baseline == carried, (
                f"a {expected['status']} response leaves the model byte-identical, so it returns the request's "
                "own token"
            )

    assert actual_rest == expected_rest


def assert_delta_matches(
    actual: Sequence[Mapping[str, Any]],
    expected: Sequence[Mapping[str, Any]],
    *,
    request: Any = None,
) -> None:
    """Compare a ``staged``/``committed`` response's delta against the vector's.

    ``{operation, address}`` is the contract and is compared exactly. The other keys are compared only as the contract
    allows:

    * **``before`` / ``after`` are compared where the vector states them**; a key it omits is not asserted absent. The
      contract's rules hold for every entry (:func:`_assert_the_delta_is_well_formed`): ``create`` carries ``after`` and
      no ``before``, ``remove`` the reverse, ``update`` both; and at the address of a requested ``create`` or
      ``update``, ``after`` agrees with the payload (containment for an object, equality for a scalar).
    * **``summary`` is asserted to exist and never matched**: it is prose with no derivation rule.
    * **The delta is compared as an unordered multiset**: ``delta`` has no ordering statement, and one operation may
      produce two entries or none. A missing, extra or repeated entry still fails.

    Raises:
        AssertionError: naming the entry that has no counterpart.
    """
    _assert_the_delta_is_well_formed(actual, request)
    remaining = list(actual)
    # Most-constrained first, so an entry stating `before`/`after` is not left
    # holding the one candidate a laxer expectation already consumed.
    for want in sorted(expected, key=lambda entry: -len(entry)):
        for index, got in enumerate(remaining):
            if _delta_entry_matches(got, want):
                del remaining[index]
                break
        else:
            raise AssertionError(f"no delta entry matches {want!r}; unmatched entries: {remaining!r}")
    assert not remaining, f"the delta carries {len(remaining)} entries the vector does not state: {remaining!r}"


#: Which of ``before``/``after`` each delta operation carries: the one statement
#: about a delta's values that holds for every entry, whatever a vector spells out.
DELTA_VALUE_KEYS: dict[str, tuple[str, ...]] = {
    "create": ("after",),
    "remove": ("before",),
    "update": ("before", "after"),
}


def _assert_the_delta_is_well_formed(actual: Sequence[Mapping[str, Any]], request: Any) -> None:
    """The two value rules every delta entry obeys — see :func:`assert_delta_matches`.

    Raises:
        AssertionError: naming the entry and the rule it breaks.
    """
    for entry in actual:
        operation = entry.get("operation")
        assert operation in DELTA_VALUE_KEYS, f"a delta entry carries operation {operation!r}: {entry!r}"
        for key in ("before", "after"):
            if key in DELTA_VALUE_KEYS[operation]:
                assert key in entry, f"a {operation} delta entry carries no {key!r}: {entry!r}"
            else:
                assert key not in entry, f"a {operation} delta entry carries a {key!r}: {entry!r}"

    if not isinstance(request, Mapping) or request.get("operation") != "change_set":
        return
    for operation in request.get("operations", []):
        if operation.get("operation") not in ("create", "update"):
            continue
        wanted = _hashable(dict(operation["address"]))
        for entry in actual:
            if entry.get("operation") != operation["operation"]:
                continue
            if _hashable(dict(entry.get("address", {}))) != wanted:
                continue
            _assert_after_carries_the_requested_payload(entry, operation["value"])


def _payload_disagreement(after: Any, requested: Any, where: str) -> str | None:
    """Where *after* disagrees with the payload *requested*, or ``None``.

    Recursive containment, not equality: a materialized element carries every
    schema default its ``create`` did not spell out, at every nesting level.
    ``group`` is skipped at the top level: it is a placement field that no
    persisted element holds.
    """
    if isinstance(requested, Mapping):
        if not isinstance(after, Mapping):
            return f"{where} is {after!r} where the request sent an object"
        for key, wanted in requested.items():
            if key == "group" and not where.endswith("]"):
                continue
            if key not in after:
                return f"{where} drops the requested {key!r}"
            if (found := _payload_disagreement(after[key], wanted, f"{where}.{key}")) is not None:
                return found
        return None
    if isinstance(requested, list):
        if not isinstance(after, list) or len(after) != len(requested):
            return f"{where} is {after!r} where the request sent {len(requested)} item(s)"
        for index, wanted in enumerate(requested):
            if (found := _payload_disagreement(after[index], wanted, f"{where}[{index}]")) is not None:
                return found
        return None
    if _hashable(after) != _hashable(requested):
        return f"{where} is {after!r} where the request sent {requested!r}"
    return None


def _assert_after_carries_the_requested_payload(entry: Mapping[str, Any], value: Any) -> None:
    """``after`` at a requested address agrees with the payload the request sent."""
    disagreement = _payload_disagreement(entry.get("after"), value, "the delta's `after`")
    assert disagreement is None, f"{disagreement} (at {entry.get('address')!r})"


def _delta_entry_matches(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    """One delta entry against one expectation — see :func:`assert_delta_matches`."""
    if actual.get("operation") != expected["operation"]:
        return False
    if _hashable(dict(actual.get("address", {}))) != _hashable(dict(expected["address"])):
        return False
    for key in ("before", "after"):
        if key in expected and (key not in actual or _hashable(actual[key]) != _hashable(expected[key])):
            return False
    if "summary" in expected and not (isinstance(actual.get("summary"), str) and actual["summary"]):
        return False
    return True


def _protocol_def_validator(definition: str) -> Draft202012Validator:
    """A validator for ``protocol.schema.json#/$defs/<definition>``.

    Not reused from ``test_editing_protocol_contracts``, whose registry is a
    pytest fixture.
    """
    schema = _load(PROTOCOL_PATH)
    registry = SchemaRegistry().with_resources([(schema["$id"], SchemaResource.from_contents(schema))])
    return Draft202012Validator({"$ref": f"{schema['$id']}#/$defs/{definition}"}, registry=registry)


def _error_result_validator() -> Draft202012Validator:
    """A validator for ``protocol.schema.json#/$defs/errorResult``."""
    return _protocol_def_validator("errorResult")


def response_validator() -> Draft202012Validator:
    """A validator for ``protocol.schema.json#/$defs/response``, every success response's union."""
    return _protocol_def_validator("response")


_ERROR_RESULT_VALIDATOR = _error_result_validator()


def assert_error_matches(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    """Compare a negative's actual rejection against its expected one.

    Codes are contract and messages are not, so the message is asserted to exist
    and never matched. ``additional`` is compared in order, on code, class and
    addresses, and is asserted absent when the vector states none. ``hints`` and
    ``measured`` get the same absence assertion: ``$defs/errorDetail`` admits both
    on any code, so the schema check cannot tell an ``over-budget``-only key from
    one that has spread.

    **It does not compare the chain itself**: ``vector.schema.json`` has no key
    for an expected ``hints`` array; ``test_editing_vectors.py``'s
    ``TestTheOverBudgetChainTheVectorDescribes`` is that chain's oracle.

    ``addresses`` within one violation is compared as an unordered multiset,
    deliberately: ``additional``'s order is stated by ``errorResult``'s
    description (pass order, then operation order), while ``addresses`` is a plain
    array naming a set of equally responsible places. The multiset still bites
    on a missing, an extra or a different address.
    """
    assert actual.get("response") == "error", f"expected a rejection, got {actual.get('response')!r}"
    assert actual.get("error") == expected["expected_error"]
    assert actual.get("failure_class") == expected["failure_class"]
    assert isinstance(actual.get("message"), str) and actual["message"], (
        "a rejection carries a human-readable message; its text is deliberately not contract"
    )

    if "addresses" in expected:
        assert _address_keys(actual.get("addresses", [])) == _address_keys(expected["addresses"])
    else:
        # Some negatives state no addresses (an envelope refusal, a stale
        # baseline); without this a spurious address on one of them would pass.
        assert "addresses" not in actual, (
            f"the vector states no addresses for this rejection; the response names {actual.get('addresses')!r}"
        )

    if "additional" in expected:
        actual_additional = actual.get("additional", [])
        assert len(actual_additional) == len(expected["additional"]), (
            f"expected {len(expected['additional'])} secondary violations, got {len(actual_additional)}"
        )
        for got, want in zip(actual_additional, expected["additional"], strict=True):
            assert got["error"] == want["expected_error"]
            assert got["failure_class"] == want["failure_class"]
            if "addresses" in want:
                assert _address_keys(got.get("addresses", [])) == _address_keys(want["addresses"])
    else:
        assert "additional" not in actual, (
            "`additional` is omitted rather than empty when the primary is the only violation"
        )

    # `hints` and `measured` are `over-budget`'s alone, but `$defs/errorDetail`
    # admits both on any code, so the schema check below would not catch a chain
    # grown onto another code.
    if expected["expected_error"] != "over-budget":
        for key in ("hints", "measured"):
            assert key not in actual, (
                f"`{key}` is carried by `over-budget` alone; this rejection is "
                f"`{expected['expected_error']}` and names {actual.get(key)!r}"
            )
    else:
        # `measured` alone: an `over-budget` refusal at the chain's terminal step
        # carries no `hints` key (`Violation.to_json` omits an empty tuple).
        assert "measured" in actual, "an `over-budget` rejection states `measured`"

    # The checks above say nothing about an extra key; `$defs/errorResult` is
    # `additionalProperties: false`, so the schema holds the closed shape.
    _ERROR_RESULT_VALIDATOR.validate(dict(actual))


def _address_keys(addresses: Sequence[Mapping[str, Any]]) -> Counter[Any]:
    """Addresses as one comparable multiset — see :func:`assert_error_matches`.

    A :class:`~collections.Counter`: a ``set`` would equate a repeated address
    with a single one, and ``sorted`` cannot order the mixed-type tuples
    :func:`_hashable` produces.
    """
    return Counter(_hashable(dict(address)) for address in addresses)


#: The relational half of the token contract is stated across vectors, so it is
#: checked over a registry of ``vector name -> (model state key, token)`` that
#: one test assembles for the whole vector set (``test_editing_vectors.py``'s
#: token class), so the property does not depend on which cases ran (``-k``,
#: ``xdist``). Both directions below are pure functions over that registry.


def assert_tokens_distinct(registry: Mapping[str, tuple[str, str]]) -> None:
    """Two vectors describing two different model states must not share a token.

    Raises:
        AssertionError: naming the token and the states that collided.
    """
    states_by_token: dict[str, set[str]] = {}
    for _name, (state, token) in registry.items():
        states_by_token.setdefault(token, set()).add(state)
    offenders = {token: sorted(states) for token, states in states_by_token.items() if len(states) > 1}
    assert not offenders, (
        f"one baseline token is produced by {len(offenders)} distinct model states: {sorted(offenders)}"
    )


def assert_equal_states_share_a_token(registry: Mapping[str, tuple[str, str]]) -> None:
    """The converse: one model state always produces one token.

    Without it a random token satisfies the distinctness rule, and a ``staged``
    response's "the model has not moved" claim would be unverifiable.

    Raises:
        AssertionError: naming the state and the tokens it produced.
    """
    tokens_by_state: dict[str, set[str]] = {}
    for _name, (state, token) in registry.items():
        tokens_by_state.setdefault(state, set()).add(token)
    offenders = {state: sorted(tokens) for state, tokens in tokens_by_state.items() if len(tokens) > 1}
    assert not offenders, f"one model state produced several baseline tokens: {sorted(offenders)}"
