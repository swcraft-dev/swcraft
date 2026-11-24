"""Reader functions for model/ YAML files.

All public functions accept model_root as a str path and use pathlib internally.

**Every read_* function names the offending file.** A malformed or schema-invalid YAML file is
reported as ``ValueError("Invalid <layer> YAML in <storage name>: <details>")``, never as a bare
``pydantic.ValidationError``. *details* is :attr:`ElementParseError.details`, one
``<field>: <msg>`` per error joined by ``; `` (or ``line L, column C: <problem>`` for a YAML syntax
error), with no input value, error type or URL. The refusal is :class:`ElementParseError`, a
``ValueError`` subclass naming the file by its model-root-relative storage name; see its docstring.

The eleven ``read_*`` and seven ``find_*_file`` functions are one-line wrappers over
:func:`read_layer` and :func:`find_element_file`, which take the directory, the recursion and the
file suffix from the kernel registry's storage record, so a layer the contract adds is readable
with no edit here. Each wrapper names one layer; they are kept as this package's public API.

``ElementParseError`` and ``_load_element`` are re-exports of :mod:`swcraft_tools.model_state`'s
names (``reader.ElementParseError is model_state.ElementParseError``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from swcraft_tools.model_registry import LayerFacts, ModelRegistry, current_registry, production_registry
from swcraft_tools.model_state import ElementParseError as ElementParseError
from swcraft_tools.model_state import ModelRootNotFoundError as ModelRootNotFoundError
from swcraft_tools.model_state import (
    load_layer,
    load_layer_with_placement,
    parse_element,
    placement_from_location,
    refuse_retired_storage,
    scan_layer,
)
from swcraft_tools.schema import (
    Composition,
    Connector,
    DataType,
    Hardware,
    Interface,
    MappingSet,
    Requirement,
    Sequence,
    SWComponent,
    Task,
)
from swcraft_tools.yaml_load import load_yaml_document

_load_element = parse_element

#: Production, so this module-level fact does not depend on the context of the first import; every
#: function resolves ``current_registry()`` per call instead.
_GENERATION_REGISTRY = production_registry()

#: The one layer whose storage nests (``swc``), or ``None``; found by asking which layer nests.
_NESTING_LAYER: LayerFacts | None = next(
    (facts for facts in _GENERATION_REGISTRY.layers if facts.storage.is_recursive), None
)


def swc_placement(yaml_file: Path, swc_dir: Path) -> dict[str, str]:
    """Derive one SWC's placement metadata from where its file sits.

    A wrapper over :func:`~swcraft_tools.model_state.placement_from_location`, kept under this name and
    signature because ``component_exchange/import_.py`` imports it. The value is the POSIX-relative
    directory of the file under ``swc/``: ``swc/sensors/Foo.yaml`` is ``sensors``, a flat
    ``swc/Foo.yaml`` is the empty string, and a deeper tree (``swc/a/b/Foo.yaml``) is reported
    verbatim as ``a/b``. A reader reports what is on disk; refusing a multi-segment value is the
    validation and protocol layers' job.
    """
    if _NESTING_LAYER is None:
        return {}
    return dict(placement_from_location(yaml_file, swc_dir, _NESTING_LAYER.storage))


# ---------------------------------------------------------------------------
# The two generic functions the wrappers delegate to
# ---------------------------------------------------------------------------


def read_layer(model_root: str, layer: str, *, registry: ModelRegistry | None = None) -> list[Any]:
    """Every element of *layer* under *model_root*, parsed, by composed storage path (never file name).

    Args:
        model_root: the model directory.
        layer: the contract's name for the layer — ``"swc"``, ``"implementation_types"``.
        registry: the kernel registry to resolve the layer against; ``None`` resolves the current
            one at call time.

    A missing layer directory yields ``[]``.

    Raises:
        ElementParseError: a file that does not parse or does not validate,
            naming it. A ``ValueError``.
    """
    return load_layer(model_root, layer, registry=registry)


def find_element_file(
    model_root: str, layer: str, identity: str, *, registry: ModelRegistry | None = None
) -> str | None:
    """The path of *layer*'s element whose identity field equals *identity*.

    A content scan, not a filename match: the file whose declared identity field (``id`` for
    ``requirements``, ``name`` elsewhere) equals *identity* is returned even when its filename
    disagrees. The YAML is read raw, so an element that does not validate can still be located.

    Returns:
        Absolute path string if found, None otherwise — including when the
        layer's directory does not exist.
    """
    registry = registry or current_registry()
    identity_field = registry.layer(layer).identity_field
    scan = scan_layer(model_root, layer, registry=registry)
    if not scan.present:
        return None
    for entry in scan.files:
        data = load_yaml_document(entry.path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get(identity_field) == identity:
            return str(entry.path)
    return None


def _read_layer_with_placement(
    model_root: str, layer: str, *, registry: ModelRegistry | None = None
) -> list[tuple[Any, dict[str, str]]]:
    """Every element of *layer*, each paired with what its location encodes.

    The placement mapping is empty for a layer that does not nest.
    """
    registry = registry or current_registry()
    refuse_retired_storage(model_root, registry=registry)
    _present, parsed = load_layer_with_placement(model_root, layer, registry)
    return [(element, dict(values)) for element, values in parsed]


# ---------------------------------------------------------------------------
# The wrappers: one per layer, names, signatures and refusals unchanged
# ---------------------------------------------------------------------------


def read_swc_model_with_placement(model_root: str) -> list[tuple[SWComponent, dict[str, str]]]:
    """Read all SWC YAML files, each paired with its placement metadata.

    The parsed element is exactly what :func:`read_swc_model` returns; the placement values sit
    beside it, because ``SWComponent`` has no ``group`` field (it would be a second representation
    beside the directory that can disagree with it).

    Raises:
        FileNotFoundError: if model_root does not exist.
        ValueError: a YAML file fails to parse or validate; the message names the file.
    """
    root = Path(model_root)
    if not root.exists():
        raise ModelRootNotFoundError(root)
    return _read_layer_with_placement(model_root, "swc")


def read_swc_model(model_root: str) -> list[SWComponent]:
    """Read all SWC YAML files from model_root/swc/**/*.yaml.

    Raises:
        FileNotFoundError: if model_root does not exist.
        ValueError: a YAML file fails to parse or validate; the message names the file.
    """
    return [element for element, _ in read_swc_model_with_placement(model_root)]


def read_interfaces(model_root: str) -> list[Interface]:
    """Read all interface YAML files from model_root/interfaces/*.yaml.

    Returns a list of Interface objects (either SRInterface or CSInterface),
    discriminated by the ``kind`` field in each YAML file.

    Raises:
        ValueError: a YAML file fails to parse or validate; the message names the file.
    """
    return read_layer(model_root, "interfaces")


def read_requirements(model_root: str) -> list[Requirement]:
    """Read all requirement YAML files from model_root/requirements/*.yaml.

    Raises:
        ValueError: a YAML file fails to parse or validate; the message names the file.
    """
    return read_layer(model_root, "requirements")


def read_compositions(model_root: str) -> list[Composition]:
    """Read all composition YAML files from model_root/compositions/*.yaml.

    Raises:
        ValueError: a YAML file fails to parse or validate; the message names the file.
    """
    return read_layer(model_root, "compositions")


def read_connectors(model_root: str) -> list[Connector]:
    """Read all connector YAML files from model_root/connectors/*.yaml.

    Raises:
        ValueError: a YAML file fails to parse or validate; the message names the file.
    """
    return read_layer(model_root, "connectors")


def read_data_types(model_root: str) -> list[DataType]:
    """Read every implementation type file, model_root/data_types/implementation/*.yaml.

    Raises:
        ValueError: a YAML file fails to parse or validate; the message names the file.
    """
    return read_layer(model_root, "implementation_types")


def read_mapping_sets(model_root: str) -> list[MappingSet]:
    """Read all mapping set YAML files from model_root/data_types/mapping_sets/*.yaml.

    A missing ``data_types/mapping_sets/`` directory returns an empty list. The layer is mandatory
    and may be empty (a project typed by implementation types alone declares no set), so an empty
    answer is not a model error.

    Raises:
        ValueError: a YAML file fails to parse or validate; the message names the file.
    """
    return read_layer(model_root, "mapping_sets")


def read_tasks(model_root: str) -> list[Task]:
    """Read all task YAML files from model_root/tasks/*.yaml.

    Raises:
        ValueError: a YAML file fails to parse or validate; the message names the file.
    """
    return read_layer(model_root, "tasks")


def read_hardware(model_root: str) -> list[Hardware]:
    """Read all hardware YAML files from model_root/hardware/*.yaml.

    Returns a list of Hardware objects (a ``Processor``, a ``Core``, a ``Memory`` or a
    ``Partition``), discriminated by the ``kind`` field in each YAML file, ordered by declared name.

    A missing ``hardware/`` directory returns an empty list: the layer is mandatory, but the absence
    is reported by :func:`~swcraft_tools.validator.check_hardware_topology` as
    ``hardware.layer-absent``, not refused here.

    Raises:
        ValueError: a YAML file fails to parse or validate; the message names the file.
    """
    return read_layer(model_root, "hardware")


def read_sequences(model_root: str) -> list[Sequence]:
    """Read all sequence YAML files from model_root/sequences/*.yaml.

    Raises:
        ValueError: a YAML file fails to parse or validate; the message names the file.
    """
    return read_layer(model_root, "sequences")


def find_swc_file(model_root: str, swc_name: str) -> str | None:
    """Find YAML file path for SWC by name field.

    Returns:
        Absolute path string if found, None otherwise.
    """
    return find_element_file(model_root, "swc", swc_name)


def find_requirement_file(model_root: str, req_id: str) -> str | None:
    """Find YAML file path for a requirement by id field.

    Returns:
        Absolute path string if found, None otherwise.
    """
    return find_element_file(model_root, "requirements", req_id)


def find_connector_file(model_root: str, name: str) -> str | None:
    """Find YAML file path for a connector by name field.

    Returns:
        Absolute path string if found, None otherwise.
    """
    return find_element_file(model_root, "connectors", name)


def find_data_type_file(model_root: str, name: str) -> str | None:
    """Find YAML file path for an implementation type by name field.

    Returns:
        Absolute path string if found, None otherwise.
    """
    return find_element_file(model_root, "implementation_types", name)


def find_task_file(model_root: str, name: str) -> str | None:
    """Find YAML file path for a task by name field.

    Returns:
        Absolute path string if found, None otherwise.
    """
    return find_element_file(model_root, "tasks", name)


def find_hardware_file(model_root: str, name: str) -> str | None:
    """Find YAML file path for a hardware element by name field.

    Returns:
        Absolute path string if found, None otherwise.
    """
    return find_element_file(model_root, "hardware", name)


def find_sequence_file(model_root: str, name: str) -> str | None:
    """Find YAML file path for a sequence by name field.

    Returns:
        Absolute path string if found, None otherwise.
    """
    return find_element_file(model_root, "sequences", name)
