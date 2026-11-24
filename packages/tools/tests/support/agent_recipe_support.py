"""The model the agent's recipes are run against, and the answers it is built to give.

``test_agent_recipes.py`` runs every copy-ready recipe of the SWCraft Architect profile and the agent prompt against
this model and compares the result with an answer known before the run; the question set's oracle tests ask the
same model through the CLI.

**The model.** The seed ``read-swc-writes-joined-to-readers`` (``ExampleSWC`` writes ``IExample.value`` and
``IPhysicalSpeed.speed``, ``ExampleConsumerSWC`` reads both through ``ConsumerGroup``'s delegations), plus:

* two partitions: ``PARTITION1`` on ``CORE1`` runs ``ExampleSWC`` and ``LocalConsumerSWC``, ``PARTITION0`` on
  ``CORE0`` the consumer; ``LocalConsumerSWC`` reads ``IExample.value`` through ``Example_to_Local``;
* ``UnplacedSWC``: run by no task, no description, reading and writing ``IOther.value`` (the readers/writers
  recipe's interface filter keeps it out), and ``ExampleSWC``'s unconnected ``rSpare`` on ``IOther`` (the
  one-component recipe's join on ``element`` keeps ``UnplacedSWC``'s ports out);
* ``ExampleSWC`` and ``LocalConsumerSWC`` declare ``D``, ``UnplacedSWC`` ``B(D)`` and the requirement allocates
  ``B``: two levels, because ``X(Y)`` ranks as ``X`` (``safety.py``), so a recipe comparing the spelling would miss
  ``B(D)``;
* ``REQ_EXAMPLE_002`` (version 2), realised by ``LocalConsumerSWC.rLocal`` at ``@1`` (stale) and by ``IOther`` at
  ``@2``, and ``REQ_EXAMPLE_003``, satisfied by nothing; both speak of the *example value* and ``REQ_EXAMPLE_001``
  does not, so a text search matching one word rather than every word would answer it too.

The baseline is valid (``check_cross_layer_consistency`` finds nothing), so no recipe is answered off a model the
tools would refuse to build.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import yaml

#: The seed the recipe model is built on.
BASE_SEED = "read-swc-writes-joined-to-readers"

#: The value each ``<NAME>`` placeholder of a recipe stands for in the recipe model.
PLACEHOLDERS: dict[str, str] = {
    "<INTERFACE>": "IExample",
    "<DATA_ELEMENT>": "value",
    "<COMPONENT>": "ExampleSWC",
    "<ELEMENT>": "IExample",
    # ``B``: one plain ``B`` and one ``B(D)``, so the count holds only if the rating is compared.
    "<LEVEL>": "B",
    # ``PARTITION1`` runs two of the four components and ``CORE1`` one of the two tasks, so a
    # recipe that dropped its clause on the derived value would answer more.
    "<PARTITION>": "PARTITION1",
    "<CORE>": "CORE1",
    # ``RAM0`` budgets are 2 048, 1 536, 512 and 512, so the largest is one component and a
    # recipe that read the report's order would name another.
    "<MEMORY>": "RAM0",
    # ``REQ_EXAMPLE_002`` has two sites of two kinds, one of them at an older version.
    "<REQUIREMENT>": "REQ_EXAMPLE_002",
    # Two of the three requirements hold both words; ``REQ_EXAMPLE_001`` holds ``example`` alone.
    "<TOPIC>": "example value",
}

LOCAL_CONSUMER_SWC: dict[str, Any] = {
    "name": "LocalConsumerSWC",
    "type": "application",
    "description": "Reads IExample next to its provider, in the provider's partition",
    "ports": [
        {
            "name": "rLocal",
            "direction": "require",
            "interface": "IExample",
            "description": "The local reader's input",
            # Confirmed against REQ_EXAMPLE_002's first version; the requirement is at its second.
            "satisfies": ["REQ_EXAMPLE_002@1"],
        }
    ],
    "runnables": [
        {
            "name": "LocalRun",
            "min_start_interval": 0.01,
            "events": [{"type": "timing", "period": 0.01}],
            "reads": [{"port": "rLocal", "element": "value"}],
        }
    ],
    "memory": {"RAM0": 512},
    "handle_termination_and_restart": "no_support",
    # The requirement link lives in the component.
    "satisfies": [fixtures.EXAMPLE_LINK],
}

UNPLACED_SWC: dict[str, Any] = {
    "name": "UnplacedSWC",
    "type": "application",
    "ports": [
        {"name": "rOther", "direction": "require", "interface": "IOther", "description": "Reads the decoy element"},
        {"name": "pOther", "direction": "provide", "interface": "IOther", "description": "Writes the decoy element"},
    ],
    "runnables": [
        {
            "name": "OtherRun",
            "min_start_interval": 0.01,
            "events": [{"type": "timing", "period": 0.01}],
            "reads": [{"port": "rOther", "element": "value"}],
            "writes": [{"port": "pOther", "element": "value"}],
        }
    ],
    "memory": {"RAM0": 512},
    "handle_termination_and_restart": "no_support",
    # The requirement link lives in the component.
    "satisfies": [fixtures.EXAMPLE_LINK],
}

#: The partitioned editing vectors' documents, by storage name.
_PARTITIONED: dict[str, dict[str, Any]] = {edit.path: dict(edit.document) for edit in fixtures._PARTITIONED_EDITS}

#: Every document the recipe model adds to or replaces in its seed, by its
#: model-root-relative storage name.
EDITS: dict[str, dict[str, Any]] = {
    "interfaces/IOther.yaml": {
        "name": "IOther",
        "kind": "sender_receiver",
        "data_elements": [{"name": "value", "implementation_type": "float32"}],
        "description": "A second interface whose data element is also named value",
        "satisfies": ["REQ_EXAMPLE_002@2"],
    },
    # The partitioned vectors' own documents; the task there runs the local reader beside
    # ExampleSWC.
    **{path: _PARTITIONED[path] for path in ("hardware/CORE1.yaml", "hardware/PARTITION1.yaml", "hardware/RAM0.yaml")},
    "tasks/Task_10ms_P1.yaml": _PARTITIONED["tasks/Task_10ms_P1.yaml"]
    | {
        "runnables": [
            {"swc": "ExampleSWC", "runnable": "ExampleRun"},
            {"swc": "LocalConsumerSWC", "runnable": "LocalRun"},
        ]
    },
    "swc/example/LocalConsumerSWC.yaml": LOCAL_CONSUMER_SWC,
    "swc/example/UnplacedSWC.yaml": UNPLACED_SWC,
    "connectors/Example_to_Local.yaml": {
        "name": "Example_to_Local",
        "from": {"swc": "ExampleSWC", "port": "pOut"},
        "to": {"swc": "LocalConsumerSWC", "port": "rLocal"},
        "description": "ExampleSWC provides IExample data to the local reader",
    },
    "compositions/Root.yaml": {
        "name": "Root",
        "title": "Root composition",
        "root": True,
        "members": [
            {"name": "ExampleSWC", "type": "ExampleSWC"},
            {"name": "LocalConsumerSWC", "type": "LocalConsumerSWC"},
            {"name": "ConsumerGroup", "composition": "ConsumerGroup"},
        ],
    },
    "requirements/REQ_EXAMPLE_001.yaml": fixtures._requirement("B"),
    "requirements/REQ_EXAMPLE_002.yaml": {
        "id": "REQ_EXAMPLE_002",
        "title": "Local reading of the example value",
        "description": "The system shall read the example value next to its provider.",
        "req_type": "req",
        "version": 2,
        "source": "architecture",
    },
    "requirements/REQ_EXAMPLE_003.yaml": {
        "id": "REQ_EXAMPLE_003",
        "title": "Decoy reading of the other example value",
        "description": "The system shall read and write the decoy value of the other interface.",
        "req_type": "req",
        "version": 1,
        "source": "architecture",
    },
}

#: ``ExampleSWC``'s unconnected port: on ``IOther``, which ``IExample``'s impact walk does
#: not reach, and with no word the requirements share, so no other recipe's answer moves.
SPARE_PORT: dict[str, Any] = {
    "name": "rSpare",
    "direction": "require",
    "interface": "IOther",
    "description": "A spare input that nothing connects",
}

#: The two components of ``Task_10ms_P1`` declare the higher level (``safety.task-mixed-asil``),
#: and ``UnplacedSWC`` declares the allocated ``B`` in its decomposed spelling.
DECLARED_LEVELS: dict[str, str] = {"ExampleSWC": "D", "LocalConsumerSWC": "D", "UnplacedSWC": "B(D)"}


def build_recipe_model(destination: Path) -> Path:
    """Materialize the recipe model at *destination* and return its manifest's path."""
    root = fixtures.build_fixture(destination, BASE_SEED)
    model_root = root / "model"
    for storage_name, document in EDITS.items():
        fixtures._write_yaml(model_root / storage_name, document)
    for swc, level in DECLARED_LEVELS.items():
        path = model_root / "swc" / "example" / f"{swc}.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if swc == PLACEHOLDERS["<COMPONENT>"]:
            document["ports"] = [*document["ports"], SPARE_PORT]
        fixtures._write_yaml(path, document | {"asil": level})
    return root / "swcraft.yaml"


#: Every component of the model, in name order.
COMPONENTS = ("ExampleConsumerSWC", "ExampleSWC", "LocalConsumerSWC", "UnplacedSWC")

#: Every component grouped by its effective level's rating, stated from how the model is built,
#: never derived from a recipe's answer (the recipe's counts cannot rebuild every group).
#: ``ExampleConsumerSWC`` takes the allocated ``B``; ``UnplacedSWC``'s ``B(D)`` is rated ``B``.
EFFECTIVE_LEVELS_BY_RATING: dict[str, list[str]] = {
    "B": ["ExampleConsumerSWC", "UnplacedSWC"],
    "D": ["ExampleSWC", "LocalConsumerSWC"],
}

#: The answer each recipe is built to give, by the recipe's heading. ``UnplacedSWC``'s
#: ``IOther.value`` appears nowhere: no connector carries it.
EXPECTED: dict[str, Any] = {
    "Who reads or writes a data element": {
        "readers": [("ExampleConsumerSWC", "ExampleConsumerRun", "rIn"), ("LocalConsumerSWC", "LocalRun", "rLocal")],
        "writers": [("ExampleSWC", "ExampleRun", "pOut")],
    },
    # (writer, runnable, port, data element, connector, reader, runnable, port).
    "The sender-receiver table": [
        (
            "ExampleSWC",
            "ExampleRun",
            "pOut",
            "value",
            "Example_to_Consumer",
            "ExampleConsumerSWC",
            "ExampleConsumerRun",
            "rIn",
        ),
        ("ExampleSWC", "ExampleRun", "pOut", "value", "Example_to_Local", "LocalConsumerSWC", "LocalRun", "rLocal"),
        (
            "ExampleSWC",
            "ExampleRun",
            "pPhysical",
            "speed",
            "Physical_to_Consumer",
            "ExampleConsumerSWC",
            "ExampleConsumerRun",
            "rPhysical",
        ),
        (
            "ExampleSWC",
            "ExampleSlowRun",
            "pPhysical",
            "speed",
            "Physical_to_Consumer",
            "ExampleConsumerSWC",
            "ExampleConsumerRun",
            "rPhysical",
        ),
    ],
    "Elements without a description": ["UnplacedSWC"],
    # The recipe lists a level only on request, so its gate step is one level's count and page.
    "Components by effective level": {"count": 2, "which": ["ExampleConsumerSWC", "UnplacedSWC"]},
    "How many components are at a level": {"count": 2, "which": ["ExampleConsumerSWC", "UnplacedSWC"]},
    "A component's partitions": [("PARTITION1", "CORE1")],
    "Components a partition runs": ["ExampleSWC", "LocalConsumerSWC"],
    "Connections that cross a partition": [
        ("Example_to_Consumer", "ExampleSWC", "PARTITION1", "ExampleConsumerSWC", "PARTITION0"),
        ("Physical_to_Consumer", "ExampleSWC", "PARTITION1", "ExampleConsumerSWC", "PARTITION0"),
    ],
    "Components that no partition runs": ["UnplacedSWC"],
    # ``ExampleConsumerSWC``'s ports are reached through ``ConsumerGroup``'s delegations.
    "Ports that nothing connects": {
        "count": 3,
        "ports": [("ExampleSWC", "rSpare"), ("UnplacedSWC", "rOther"), ("UnplacedSWC", "pOther")],
    },
    # The join on ``element`` keeps ``ExampleSWC``'s own port and drops ``UnplacedSWC``'s two.
    "A component's unconnected ports": {"count": 1, "ports": [("ExampleSWC", "rSpare")]},
    "Tasks that run on a core": [("Task_10ms_P1", "PARTITION1")],
    # ``rSpare`` and ``UnplacedSWC``'s decoy pair are on ``IOther``, so a recipe that dropped
    # its ``where`` would answer them too.
    "Components with a port on an interface": [
        ("ExampleConsumerSWC", "rIn"),
        ("ExampleSWC", "pOut"),
        ("LocalConsumerSWC", "rLocal"),
    ],
    # (layer, element, member, kind, stated).
    "Who realises a requirement": [
        ("swc", "LocalConsumerSWC", "rLocal", "port", 1),
        ("interfaces", "IOther", None, "interface", 2),
    ],
    "Requirements nothing realises, and stale links": {
        "unallocated": ["REQ_EXAMPLE_003"],
        "stale": [("REQ_EXAMPLE_002", "swc", "LocalConsumerSWC", "rLocal", 1, 2)],
    },
    "Requirements about a topic": ["REQ_EXAMPLE_002", "REQ_EXAMPLE_003"],
    # ``LocalConsumerSWC`` alone shares ``local``, ``provider`` and ``next`` with the
    # requirement, so it is first; the order of the other two is the search's, pinned as it is.
    "Elements that could realise a requirement": {
        "places": ["LocalConsumerSWC", "IDiagnosticService", "UnplacedSWC"],
        "first_terms": ["local", "provider", "next", "value"],
    },
    # Everything that references ``IExample``, transitively, by the dependent's layer. The
    # requirement the components satisfy is not a dependent: the component depends on it.
    "What a change affects": {
        "count": {
            "interface": {
                "total": 11,
                "by_kind": {"composition": 2, "connector": 3, "sequence": 1, "swc": 3, "task": 2},
            }
        },
        "affected": {
            "composition": ["ConsumerGroup", "Root"],
            "connector": ["Example_to_Consumer", "Example_to_Local", "Physical_to_Consumer"],
            "sequence": ["task10ms_consumer_cycle"],
            "swc": ["ExampleConsumerSWC", "ExampleSWC", "LocalConsumerSWC"],
            "task": ["Task_10ms", "Task_10ms_P1"],
        },
    },
    # ``RAM0``: 262 144 bytes at a 0.8 threshold is a 209 715.2-byte budget, 4 608 of it used,
    # so the recipe's 40 960 bytes fit.
    "Whether a memory holds a new component": True,
    # 2 048 (``ExampleSWC``) before 1 536, 512 and 512.
    "The largest consumer of a memory": "ExampleSWC",
}
