"""The implementation-composite model the writer's tests and the XSD suite both generate.

Every shape the writer emits for ``implementation_record``, ``implementation_array`` and ``pointer``, in one
project ``check-model`` accepts, built in code from ``examples/basic``'s model (a committed tree would be one the
byte-identity records enumerate). It adds to the baseline:

* **the three kinds**: ``WheelSpeeds_T`` (a record), ``WheelSpeedHistory_T`` (an array of it), ``WheelSpeeds_Ptr``
  and ``WheelSpeeds_PtrPtr`` (a pointer to that pointer through the named type, ``[constr_1254]``'s option 1);
* **the dependency the writer's one ordered pass exists for**: ``WheelSpeeds_Ref`` references the record,
  ``Frame_T``'s first element is typed by it and ``Frame_Ref`` references ``Frame_T``; the reader orders a layer by
  name, so ``Frame_Ref`` is read before everything it depends on;
* **a pointer sub-element** (``Handle_T``) and **an unreferenced composite** (``Spare_T``, still emitted);
* **the application side**: ``WheelSpeed_Kph``, the record ``WheelSpeeds``, the array ``WheelSpeedHistory`` and the
  mapping set ``WheelSpeedMaps``; the record's field names deliberately differ from the implementation record's;
* **the data prototypes**: three data elements on ``IExample`` with an ``init`` each, and an operation on
  ``IDiagnosticService`` with pointer-typed arguments behind an unconnected provide port (a pointer across a
  connector between two application components is ``[constr_1295]``'s finding).

``check-model`` reports nothing on it, so the schema run validates the output of a model the gate accepts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import model_element_storage_name
from external_stubs_support import CS_INTERFACE, PROVIDER, SR_INTERFACE, baseline_model_root, change, put

#: Every data type the model adds, in the order the helper writes them.
DATA_TYPES: tuple[dict[str, Any], ...] = (
    {
        "name": "WheelSpeeds_T",
        "kind": "implementation_record",
        "elements": [{"name": "front", "type": "uint16"}, {"name": "rear", "type": "uint16"}],
    },
    {
        "name": "WheelSpeedHistory_T",
        "kind": "implementation_array",
        "element_type": "WheelSpeeds_T",
        "length": 4,
        "element_name": "Sample",
    },
    {"name": "WheelSpeeds_Ptr", "kind": "pointer", "target": "WheelSpeeds_T"},
    {"name": "WheelSpeeds_PtrPtr", "kind": "pointer", "target": "WheelSpeeds_Ptr"},
    {"name": "WheelSpeeds_Ref", "kind": "reference_type", "base": "WheelSpeeds_T"},
    {
        "name": "Frame_T",
        "kind": "implementation_record",
        "elements": [{"name": "speeds", "type": "WheelSpeeds_Ref"}, {"name": "count", "type": "uint8"}],
    },
    {"name": "Frame_Ref", "kind": "reference_type", "base": "Frame_T"},
    {"name": "Handle_T", "kind": "implementation_record", "elements": [{"name": "target", "type": "WheelSpeeds_Ptr"}]},
    {"name": "Spare_T", "kind": "implementation_array", "element_type": "uint8", "length": 2, "element_name": "Byte"},
    {"name": "WheelSpeed_Kph", "kind": "application_type"},
    {
        "name": "WheelSpeeds",
        "kind": "record",
        "fields": [{"name": "left", "type": "WheelSpeed_Kph"}, {"name": "right", "type": "WheelSpeed_Kph"}],
    },
    {"name": "WheelSpeedHistory", "kind": "array", "element_type": "WheelSpeeds", "length": 4},
)

#: The one mapping set, pairing the scalar and the two composites.
MAPPING_SET: dict[str, Any] = {
    "name": "WheelSpeedMaps",
    "maps": [
        {"application": "WheelSpeed_Kph", "implementation": "uint16"},
        {"application": "WheelSpeeds", "implementation": "WheelSpeeds_T"},
        {"application": "WheelSpeedHistory", "implementation": "WheelSpeedHistory_T"},
    ],
}

#: The three data elements added to ``IExample``, each with its ``init``.
DATA_ELEMENTS: tuple[dict[str, Any], ...] = (
    {"name": "speeds", "implementation_type": "WheelSpeeds_T", "init": {"front": 1, "rear": 2}},
    {
        "name": "history",
        "implementation_type": "WheelSpeedHistory_T",
        "init": [{"front": index, "rear": index + 10} for index in range(4)],
    },
    {"name": "frame", "implementation_type": "Frame_Ref", "init": {"speeds": {"front": 5, "rear": 6}, "count": 3}},
)

#: The client-server operation added to ``IDiagnosticService``: both arguments typed by a pointer.
POINTER_OPERATION: dict[str, Any] = {
    "name": "ReadSpeeds",
    "arguments": [
        {"name": "buffer", "direction": "in", "implementation_type": "WheelSpeeds_Ptr"},
        {"name": "handle", "direction": "out", "implementation_type": "WheelSpeeds_PtrPtr"},
    ],
}

#: The unconnected provide port on ``ExampleSWC`` that carries ``IDiagnosticService``.
POINTER_PORT: dict[str, Any] = {"name": "pDiag", "direction": "provide", "interface": "IDiagnosticService"}


def data_type(model_root: Path, document: dict[str, Any]) -> None:
    """Write one data type to its layer's directory (the document's ``kind`` picks it)."""
    put(model_root, model_element_storage_name(document["name"], kind=document["kind"]), document)


def impl_composites_model_root(destination: Path) -> Path:
    """Build the implementation-composite project at *destination* (which must not exist); return its model root."""
    return add_impl_composites(baseline_model_root(destination))


def add_impl_composites(model_root: Path) -> Path:
    """Add every data type, map, element, operation and port above to the baseline at *model_root*; return it."""
    for document in DATA_TYPES:
        data_type(model_root, document)
    put(model_root, model_element_storage_name(MAPPING_SET["name"], layer="mapping_sets"), MAPPING_SET)
    change(model_root, SR_INTERFACE, lambda d: d["data_elements"].extend(dict(e) for e in DATA_ELEMENTS))
    change(model_root, CS_INTERFACE, lambda d: d["operations"].append(dict(POINTER_OPERATION)))
    change(model_root, PROVIDER, lambda d: d["ports"].append(dict(POINTER_PORT)))
    return model_root


#: Two pointer graphs that close on themselves, for the read-only commands' walks
#: (``check-model`` refuses each as a ``data-type.cycle`` finding, so neither is
#: part of the model above): ``Node_T`` holds a pointer back to itself — the
#: linked-list shape — and ``Ring_Rec`` → ``Ring_T`` → ``Ring_Ptr`` → ``Ring_Rec``
#: closes through all three new reference fields in one ring.
POINTER_CYCLES: tuple[dict[str, Any], ...] = (
    {
        "name": "Node_T",
        "kind": "implementation_record",
        "elements": [{"name": "next", "type": "Node_Ptr"}, {"name": "value", "type": "uint8"}],
    },
    {"name": "Node_Ptr", "kind": "pointer", "target": "Node_T"},
    {"name": "Ring_Rec", "kind": "implementation_record", "elements": [{"name": "slots", "type": "Ring_T"}]},
    {"name": "Ring_T", "kind": "implementation_array", "element_type": "Ring_Ptr", "length": 2, "element_name": "Slot"},
    {"name": "Ring_Ptr", "kind": "pointer", "target": "Ring_Rec"},
)


def add_pointer_cycles(model_root: Path) -> Path:
    """Add :data:`POINTER_CYCLES` to the model at *model_root*; return it."""
    for document in POINTER_CYCLES:
        data_type(model_root, document)
    return model_root
