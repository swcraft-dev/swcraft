"""What ``import component`` reads of the target project outside its validated model.

The named-file scan the ambiguity check runs before the Pydantic read, the tolerant scan for the
bare application/implementation pairs the target declares, the lookups of a target definition or
the external stub standing for the same AUTOSAR path, the conflict lines a stub gives, and one
layer of the project the import would leave. Nothing here raises one of :mod:`.import_`'s
refusal classes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TypeVar

import yaml

from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import parse_element, parse_label
from swcraft_tools.schema import (
    DataDescriptionElement,
    DataType,
    ExternalDataType,
    ExternalInterface,
    Interface,
    MappingSet,
    SWComponent,
)
from swcraft_tools.yaml_load import load_yaml_document


def scan_named_files(layer_dir: Path, pattern: str, model_root: Path, layer: str) -> dict[str, list[Path]]:
    """``name`` field -> YAML files declaring it, for one model layer dir.

    Raw documents on purpose: this runs before the Pydantic read, so duplicate detection and
    file-path recording work although ``reader.py`` returns objects with neither. A file that is
    not YAML is refused by the reader's own
    :class:`~swcraft_tools.model_state.ElementParseError`, so the file gets one sentence, not two.
    """
    label = parse_label(current_registry().layer(layer))
    names: dict[str, list[Path]] = {}
    if not layer_dir.exists():
        return names
    for yaml_file in sorted(layer_dir.glob(pattern), key=lambda path: path.as_posix()):
        data = parse_element(yaml_file, label, lambda document: document, model_root)
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            names.setdefault(data["name"], []).append(yaml_file)
    return names


#: The element kinds whose creates are planned in source-path order (``import_._in_source_path_order``) and
#: whose layers :func:`target_first` orders: each carries a ``name``.
Ordered = TypeVar("Ordered", bound=DataType | Interface | MappingSet | DataDescriptionElement)


def external_stub_conflict(what: str, name: str, stub: ExternalInterface | ExternalDataType) -> str:
    """The conflict line for an incoming definition *name* the target holds as the external *stub*.

    A stub refers to a definition outside the target, so there is nothing to compare with:
    reusing it would bind the component to a definition nobody checked, and replacing the stub
    is the architect's decision, not an import's. So it is a conflict with no per-import
    override. A stub of another name standing for the same path is named beside *name*.
    """
    held = "" if stub.name == name else f" as '{stub.name}'"
    return (
        f"shared {what} '{name}' is an external stub in the target project{held} (a reference to "
        f"{stub.external_ref!r}, a definition outside the project), so the incoming definition cannot be "
        "compared with it"
    )


def layer_directory(root: Path, layer: str) -> Path:
    """The directory *layer* stores its files in under *root*, read from the registry in effect.

    A layer's directory is a model-root-relative path of one or two segments, so it is read from
    the registry rather than spelled here.
    """
    return root / current_registry().storage(layer).directory


#: The three stub kinds :func:`_stub_of_ref` looks among: each carries a ``name`` and an ``external_ref``.
_Stub = TypeVar("_Stub", ExternalInterface, ExternalDataType, SWComponent)


def _stub_of_ref(stubs: Iterable[_Stub], ref: str | None) -> _Stub | None:
    """The first by name of *stubs* whose ``external_ref`` **is** *ref*, or ``None``.

    Matched by the **exact** ``external_ref``, never the last segment. Does not reach: a stub whose
    ``external_ref`` is blank or malformed matches no path; a stub found by neither its name nor its
    path is not found.
    """
    if ref is None:
        return None
    return min((stub for stub in stubs if stub.external_ref == ref), key=lambda stub: stub.name, default=None)


def target_type_or_stub(
    target_types: Mapping[tuple[str, str], DataType], key: tuple[str, str], ref: str | None
) -> DataType | None:
    """The target's data type of *key* ``(layer, name)``, else its external type stub for *ref* in that layer."""
    layer, name = key
    found = target_types.get(key)
    if found is not None:
        return found
    stubs = (
        element
        for (element_layer, _), element in target_types.items()
        if element_layer == layer and isinstance(element, ExternalDataType)
    )
    return _stub_of_ref(stubs, ref)


def target_interface_or_stub(
    target_interfaces: Mapping[str, Interface], name: str, ref: str | None
) -> Interface | None:
    """The target's interface *name*, else its external interface stub standing for *ref*."""
    found = target_interfaces.get(name)
    if found is not None:
        return found
    return _stub_of_ref((i for i in target_interfaces.values() if isinstance(i, ExternalInterface)), ref)


def component_stub_conflict(swc: SWComponent, target_swcs: Mapping[str, SWComponent], ref: str | None) -> list[str]:
    """The conflict line for an incoming component the target holds as an external stub of another name.

    :func:`_stub_of_ref`'s rule for component stubs. A stub of the incoming name keeps the
    *diverges* conflict, which ``--update`` resolves by replacing the stub's file. A stub of
    another name for the **same path** is a conflict ``--update`` does not resolve: ``Foo``
    would land in ``Foo_ext0``'s file while every composition prototype still names
    ``Foo_ext0``, and ``import component`` rewrites no composition. A stub for another path is
    another element, and the incoming component is created beside it. What it does not reach: a
    stub whose ``external_ref`` is blank or malformed matches no path.
    """
    if swc.name in target_swcs:
        return []
    stub = _stub_of_ref((target for target in target_swcs.values() if target.external), ref)
    if stub is None:
        return []
    return [
        f"SWC '{swc.name}' is an external stub in the target project as '{stub.name}' (a reference to "
        f"{stub.external_ref!r}), and --update does not replace a stub of another name: the component would "
        f"take the stub's file while every composition prototype still names '{stub.name}' (rename the stub "
        f"and its prototypes to '{swc.name}' to have --update replace it)"
    ]


def target_first(target: Mapping[str, Ordered], incoming: Sequence[Ordered]) -> list[Ordered]:
    """The target's declarations first, then the import's names the target lacks.

    The project the import would leave, for one layer: a name the import also brings is the
    target's declaration (reused, or a divergence reported on its own).
    """
    return [*target.values(), *(item for item in incoming if item.name not in target)]


def target_bare_pairs(root: Path) -> frozenset[str]:
    """The names the target project declares both as an ``application_type`` and as an implementation type.

    A bare same-name pair the project already declares is kept, as the project keeps it, because
    the composite member that makes the project keep it may lie outside the component's closure.

    A **tolerant** scan that never raises, so the order of refusals does not move: it runs
    before ``map_data_types``, and an export's own defect is still refused before any defect of
    the target. The authoritative reads (``scan_named_files`` and ``read_data_description``)
    refuse a target file that does not parse or validate; here it is skipped, as is a directory
    that is missing or cannot be listed. ``Path.is_dir()`` is inside the ``try`` because on
    CPython 3.11 it raises ``PermissionError`` under an untraversable parent. An application
    file counts only with ``kind: application_type`` (an ``enum``, ``record`` or ``array`` is
    not a bare pair); an implementation file counts whatever its kind.

    What it does not reach: a target file that parses but does not validate can put a name into
    the set; such a target is refused before anything is written, so a wrong keep cannot reach
    the model.
    """

    def names(layer: str, kind: str | None) -> set[str]:
        found: set[str] = set()
        directory = layer_directory(root, layer)
        try:
            # Unordered: every name goes into a set, so no order is observed.
            paths = list(directory.glob("*.yaml")) if directory.is_dir() else []
        except OSError:
            return found
        for path in paths:
            try:
                document = load_yaml_document(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, yaml.YAMLError):
                continue
            if isinstance(document, dict) and isinstance(document.get("name"), str):
                if kind is None or document.get("kind") == kind:
                    found.add(document["name"])
        return found

    return frozenset(names("application_types", "application_type") & names("implementation_types", None))
