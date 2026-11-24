"""The model writers and generators: YAML mutations, ARXML, ``needs.json`` and diagrams.

This package re-exports the public mutation and generator API, so callers resolve names through
``swcraft_tools.writer``. It re-exports no underscore-prefixed name.

Internal dependency graph (each module depends only on the ones to its left; no cycles)::

    composition_structure -> arxml_generation -> mutations -> elements
    yaml_documents (no internal deps)
    needs_export (no internal deps)
    diagrams (no internal deps)

- ``mutations`` -- the atomic YAML write operations; ``yaml_documents`` -- the ``*_to_dict`` functions.
- ``composition_structure`` -- the member-graph cycle and external-stub refusals.
- ``arxml_generation`` -- ``build_workspace()`` and ``generate_arxml()``, emitting into ``arxml_xml``.
- ``elements`` -- ``write_element()``/``remove_element()``; it depends on nothing in ``swcraft_tools.editing``.
- ``needs_export`` -- ``generate_needs()``, split into ``build_needs_json()`` and ``write_needs_json()``.
- ``diagrams`` -- ``generate_diagrams()``, the UML/SysML views laid out by ELK.
"""

from .arxml_generation import (
    build_workspace,
    generate_arxml,
    generate_arxml_cli,
)
from .diagrams import DiagramGenerationError, DiagramLayoutError, DiagramsResult, generate_diagrams
from .elements import remove_element, write_element
from .mutations import (
    add_init_event,
    add_port,
    add_runnable,
    add_timing_event,
    add_trace_link,
    create_composition,
    create_connector,
    create_cs_interface,
    create_data_type,
    create_hardware_element,
    create_interface,
    create_mapping_set,
    create_requirement,
    create_sequence,
    create_swc,
    create_task,
    find_runnable_ref,
    map_runnable_to_task,
    set_swc_memory_budget,
    set_task_partition,
    set_task_priority,
    set_task_wcet,
    unset_swc_memory_budget,
    update_resource,
)
from .needs_export import (
    NeedsExportRefused,
    build_needs_json,
    generate_needs,
    remove_previous_needs_json,
    write_needs_json,
)

__all__ = [
    "DiagramGenerationError",
    "DiagramLayoutError",
    "DiagramsResult",
    "NeedsExportRefused",
    "add_init_event",
    "add_port",
    "add_runnable",
    "add_timing_event",
    "add_trace_link",
    "build_needs_json",
    "build_workspace",
    "create_composition",
    "create_connector",
    "create_cs_interface",
    "create_data_type",
    "create_hardware_element",
    "create_interface",
    "create_mapping_set",
    "create_requirement",
    "create_sequence",
    "create_swc",
    "create_task",
    "find_runnable_ref",
    "generate_arxml",
    "generate_arxml_cli",
    "generate_diagrams",
    "generate_needs",
    "map_runnable_to_task",
    "remove_element",
    "remove_previous_needs_json",
    "set_swc_memory_budget",
    "set_task_partition",
    "set_task_priority",
    "set_task_wcet",
    "unset_swc_memory_budget",
    "update_resource",
    "write_element",
    "write_needs_json",
]
