"""The specimen builder's vocabulary: every refusal class and every leaf path its modules name.

One definition each, so that a class or a leaf is spelled once. The leaf classes (`VERSION`,
`MET`) are here too; `VERSION` is also a refusal class. The passport reader's own classes
(`passport.READING`, `passport.SCHEMA`, `passport.MISSED`) stay with the reader, which never
imports the builder.
"""

from __future__ import annotations

from ..passport import ASIL_LEVELS

# --- the leaf classes ---------------------------------------------------------------------------

VERSION = "version"
MET = "met"
#: A refusal class only: a stated `layers.hardware` of 0.
LATER_SHAPE = "later-shape"

# --- the refusal classes ------------------------------------------------------------------------

CEILING = "ceiling"
ORDER = "order"
HUB_RULE = "hub-rule"
PORT_TOTAL = "port-total"
SUM = "sum"
FAN_OUT = "fan-out"
LAYER_TOTAL = "layer-total"
UNCONNECTED_CLASS = "unconnected"
CONNECTORS_CLASS = "connectors"
LINKS_CLASS = "links"
HARDWARE_TOPOLOGY = "hardware-topology"
IDENTIFIER_LENGTH = "identifier-length"
IDENTIFIER_MEAN = "identifier-mean"
#: The class of `connectors.delegation` outside the bounds the reached ports, the connectors and
#: the compositions admit.
DELEGATIONS = "delegations"
#: The class of `connectors.delegation` when no configuration tried has a layout (`layout.py`).
NO_LAYOUT = "no-layout"
#: The class of a refusal the derivation of an unstated leaf caused, not a closed-form
#: inequality of the stated leaves (`shape.py`).
DERIVED = "derived"
#: The class of a task or sequence count the runnables and the partitions cannot carry.
TASKS_CLASS = "tasks"
#: The class of a requirement leaf the sites and the other requirement leaves cannot carry.
REQUIREMENTS_CLASS = "requirements"

# --- the leaf paths -----------------------------------------------------------------------------

PASSPORT_VERSION_LEAF = "passport_version"
CONTRACT_VERSION_LEAF = "contract_version"

LAYER_SWC = "layers.swc"
LAYER_INTERFACES = "layers.interfaces"
LAYER_CONNECTORS = "layers.connectors"
LAYER_IMPLEMENTATION = "layers.implementation_types"
LAYER_BASE = "layers.base_types"
LAYER_HARDWARE = "layers.hardware"
LAYER_COMPOSITIONS = "layers.compositions"
LAYER_TASKS = "layers.tasks"
LAYER_SEQUENCES = "layers.sequences"
LAYER_REQUIREMENTS = "layers.requirements"


def layer_leaf(layer: str) -> str:
    """The count leaf of the passport layer `layer` (`layers.<layer>`)."""
    return f"layers.{layer}"


PORTS_TOTAL = "components.ports.total"
PORTS_TOP = "components.ports.top"
HUBS = "components.hubs"
RUNNABLES = "components.runnables"
UNCONNECTED_TOTAL = "components.unconnected.total"
MAX_ON_ONE = "components.unconnected.max_on_one"
LEVEL_LEAVES = tuple(f"components.by_declared_level.{level}" for level in ASIL_LEVELS)

SENDER_RECEIVER = "interfaces.sender_receiver"
CLIENT_SERVER = "interfaces.client_server"
FAN_OUT_MAX = "interfaces.fan_out.max"
FAN_OUT_TOP = "interfaces.fan_out.top"

TASKS_IDLE = "tasks.idle"

ON_ALL_SITE_KINDS = "requirements.on_all_site_kinds"
ON_ONE_PORT = "requirements.on_one_port"
UNALLOCATED = "requirements.unallocated"
STALE_LINKS = "requirements.stale_links"
REQUIREMENT_LINKS = "requirements.links"

ASSEMBLY = "connectors.assembly"
DELEGATION = "connectors.delegation"
LINKS = "connectors.links"

CORES = "hardware.cores"
PARTITIONS = "hardware.partitions"
MEMORIES = "hardware.memories"
RATED_PARTITIONS = "hardware.rated_partitions"

MAX_LENGTH = "identifiers.max_length"
CONNECTOR_MAX_LENGTH = "identifiers.connectors.max_length"
CONNECTOR_MEAN_LENGTH = "identifiers.connectors.mean_length"
#: The identifier leaves a connector count the construction chooses can decide.
COUNT_DECIDED = frozenset({CONNECTOR_MAX_LENGTH, CONNECTOR_MEAN_LENGTH, MAX_LENGTH})
