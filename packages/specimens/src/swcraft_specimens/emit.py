"""Canonical YAML materialisation of the semantic model."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .errors import GenerationError
from .model import (
    COMPOSITION_SITE,
    HARDWARE_SITE,
    INTERFACE_SITE,
    PORT_SITE,
    RUNNABLE_SITE,
    SWC_SITE,
    TASK_SITE,
    ArrayType,
    BaseType,
    Component,
    ConnectorEndpoint,
    DataType,
    ExternalInterface,
    Interface,
    LinearCompuMethod,
    Project,
    RecordType,
    ValueType,
    hardware_names,
    partitions,
    task_partition,
)
from .output import validate_output
from .passport import PASSPORT_LAYERS
from .preset import ASIL
from .toolchain import toolchain_version

# The fifteen storage directories under ``model/``, in the order of the pinned SWCraft release's
# model contract.  ``swc`` holds one sub-directory per group; an empty directory holds a
# ``.gitkeep``.
STORAGE_DIRECTORIES = (
    "swc",
    "interfaces",
    "connectors",
    "data_types/application",
    "data_types/implementation",
    "data_types/base",
    "requirements",
    "compositions",
    "tasks",
    "sequences",
    "hardware",
    "data_types/mapping_sets",
    "data_types/units",
    "data_types/compu_methods",
    "data_types/constraints",
)

# The array's one sub-element name: a fixed word, so it consumes no PRNG draw.
ARRAY_ELEMENT_NAME = "Element"

# Every component's restart behaviour: the standard's `NO-SUPPORT`. The specimen generator models
# no restart behaviour; the value is fixed and consumes no PRNG draw.
HANDLE_TERMINATION_AND_RESTART = "no_support"

# The version every WATERS requirement carries (a specimen's states its own), and the source every
# emitted requirement names.
REQUIREMENT_VERSION = 1
REQUIREMENT_SOURCE = "architecture"

# The type of every runnable budget: the runnable's own execution, without external calls.
RUNNABLE_WCET_TYPE = "net"

# The unit suffix of every emitted time budget: a whole number of microseconds, e.g. ``250us``.
MICROSECOND_SUFFIX = "us"


def _microseconds_text(value: int) -> str:
    return f"{value}{MICROSECOND_SUFFIX}"


def satisfies_entry(requirement_id: str, version: int = REQUIREMENT_VERSION) -> str:
    """Return one ``satisfies`` entry: the requirement's id at ``version``."""
    return f"{requirement_id}@{version}"


#: A ``satisfies`` site: its kind (``model.LINK_KINDS``), its element and its member.
Site = tuple[str, str, str | None]


def satisfied_requirements(project: Project) -> dict[Site, list[str]]:
    """Return each site's sorted ``<id>@<version>`` entries, inverted from the requirements.

    The internal model keeps the links on the requirement; SWCraft states them on the site, each at
    the link's version. A site no requirement lists has no entry.
    """
    entries: dict[Site, set[str]] = {}
    for requirement in project.requirements:
        for link in requirement.links:
            entries.setdefault((link.kind, link.element, link.member), set()).add(
                satisfies_entry(requirement.id, link.version)
            )
    return {site: sorted(values) for site, values in entries.items()}


def _satisfies(entries: list[str]) -> tuple[tuple[str, Any], ...]:
    """The ``satisfies`` pair of a site with entries; none for a site without."""
    return (("satisfies", entries),) if entries else ()


def data_type_directory(item: DataType) -> str:
    """Return the storage directory of one data type: a base type, or the implementation side."""
    return "data_types/base" if isinstance(item, BaseType) else "data_types/implementation"


def _data_type(item: DataType) -> dict[str, Any]:
    if isinstance(item, BaseType):
        return _map(
            ("name", item.name),
            ("kind", "base_type"),
            ("size", item.size_bits),
            ("encoding", item.encoding),
            ("native_declaration", item.native_declaration),
        )
    if isinstance(item, ValueType):
        return _map(("name", item.name), ("kind", "value_type"), ("base", item.base))
    if isinstance(item, ArrayType):
        return _map(
            ("name", item.name),
            ("kind", "implementation_array"),
            ("element_type", item.element_type),
            ("length", item.length),
            ("element_name", ARRAY_ELEMENT_NAME),
        )
    if isinstance(item, RecordType):
        elements = [_map(("name", f.name), ("type", f.type)) for f in item.fields]
        return _map(("name", item.name), ("kind", "implementation_record"), ("elements", elements))
    raise GenerationError(f"unsupported data type: {item}")


def type_layer_documents(project: Project) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Return the five type layers' ``(name, document)`` pairs, keyed by storage directory.

    Each kind has exactly the fields SWCraft's schema gives it, keys in a fixed order; an empty
    optional field is not written.
    """
    units = [
        (
            u.name,
            _map(("name", u.name), *((("display_name", u.display_name),) if u.display_name else ())),
        )
        for u in project.units
    ]
    compu_methods = [
        (
            m.name,
            _map(
                ("name", m.name),
                ("kind", "linear"),
                ("unit", m.unit),
                ("factor", m.factor),
                ("offset", m.offset),
            )
            if isinstance(m, LinearCompuMethod)
            else _map(
                ("name", m.name),
                ("kind", "texttable"),
                ("entries", [_map(("name", n), ("value", v)) for n, v in m.entries]),
            ),
        )
        for m in project.compu_methods
    ]
    constraints = [
        (
            c.name,
            _map(
                ("name", c.name),
                *((key, value) for key, value in (("min", c.min), ("max", c.max)) if value is not None),
            ),
        )
        for c in project.data_constraints
    ]
    applications = [
        (
            a.name,
            _map(
                ("name", a.name),
                ("kind", "application_type"),
                *(
                    (key, value)
                    for key, value in (
                        ("unit_ref", a.unit_ref),
                        ("compu_method_ref", a.compu_method_ref),
                        ("data_constr_ref", a.data_constr_ref),
                    )
                    if value is not None
                ),
            ),
        )
        for a in project.application_types
    ]
    mapping_sets = [
        (
            m.name,
            _map(
                ("name", m.name),
                (
                    "maps",
                    [_map(("application", a), ("implementation", i)) for a, i in m.maps],
                ),
            ),
        )
        for m in project.mapping_sets
    ]
    return {
        PASSPORT_LAYERS["application_types"]: applications,
        PASSPORT_LAYERS["mapping_sets"]: mapping_sets,
        PASSPORT_LAYERS["units"]: units,
        PASSPORT_LAYERS["compu_methods"]: compu_methods,
        PASSPORT_LAYERS["data_constraints"]: constraints,
    }


def _interface(interface: Interface | ExternalInterface, satisfies: list[str]) -> dict[str, Any]:
    if isinstance(interface, ExternalInterface):
        # A stub: the reference pair and no member names.
        return _map(
            ("name", interface.name),
            ("kind", interface.kind),
            ("external_ref", interface.external_ref),
            ("external_dest", interface.external_dest),
            *_satisfies(satisfies),
        )
    if interface.kind == "sender_receiver":
        return _map(
            ("name", interface.name),
            ("kind", interface.kind),
            (
                "data_elements",
                [_map(("name", e.name), ("implementation_type", e.type)) for e in interface.data_elements],
            ),
            *_satisfies(satisfies),
        )
    if interface.kind == "client_server":
        return _map(
            ("name", interface.name),
            ("kind", interface.kind),
            (
                "operations",
                [
                    _map(
                        ("name", op.name),
                        (
                            "arguments",
                            [
                                _map(
                                    ("name", a.name),
                                    ("direction", a.direction),
                                    ("implementation_type", a.type),
                                )
                                for a in op.arguments
                            ],
                        ),
                    )
                    for op in interface.operations
                ],
            ),
            *_satisfies(satisfies),
        )
    raise GenerationError(f"unsupported interface kind: {interface.kind}")


class _Quoted(str):
    pass


class CanonicalDumper(yaml.SafeDumper):
    pass


def _quoted(dumper: yaml.SafeDumper, value: _Quoted) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(value), '"')


def _decimal(dumper: yaml.SafeDumper, value: Decimal) -> yaml.ScalarNode:
    text = format(value, "f")
    # Keep integral decimals visibly numeric: without the decimal point PyYAML emits ``!!float '1'``,
    # which the VS Code YAML extension marks as a string where the schema requires a number.
    if "." not in text:
        text += ".0"
    return dumper.represent_scalar("tag:yaml.org,2002:float", text)


CanonicalDumper.add_representer(_Quoted, _quoted)
CanonicalDumper.add_representer(Decimal, _decimal)


def _map(*pairs: tuple[str, Any]) -> dict[str, Any]:
    return dict(pairs)


def _yaml(value: dict[str, Any]) -> bytes:
    text = yaml.dump(value, Dumper=CanonicalDumper, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return text.replace("\r\n", "\n").encode("utf-8")


def _swc(component: Component, satisfies: Mapping[Site, list[str]], emit_runnable_wcet: bool) -> dict[str, Any]:
    ports = [
        _map(
            ("name", p.name),
            ("direction", p.direction),
            ("interface", p.interface),
            *_satisfies(satisfies.get((PORT_SITE, component.name, p.name), [])),
        )
        for p in component.ports
    ]
    runnables = []
    for runnable in component.runnables:
        events = (
            []
            if runnable.period_us is None
            else [
                _map(
                    ("type", "timing"),
                    ("period", Decimal(runnable.period_us) / Decimal(1_000_000)),
                )
            ]
        )
        pairs: list[tuple[str, Any]] = [
            ("name", runnable.name),
            ("events", events),
            ("reads", [_map(("port", p), ("element", e)) for p, e in runnable.reads]),
            ("writes", [_map(("port", p), ("element", e)) for p, e in runnable.writes]),
            ("calls", [_map(("port", p), ("operation", o)) for p, o in runnable.calls]),
        ]
        if emit_runnable_wcet:
            # The calibrated integer the load arithmetic uses, never the raw draw; ``net`` because
            # it is the runnable's own execution, without the time spent in external calls.
            pairs.append(("wcet", _microseconds_text(runnable.wcet_us)))
            pairs.append(("wcet_type", RUNNABLE_WCET_TYPE))
        pairs.extend(_satisfies(satisfies.get((RUNNABLE_SITE, component.name, runnable.name), [])))
        runnables.append(_map(*pairs))
    # ``group`` is SWCraft placement metadata and is represented by the
    # model/swc/<group>/ directory, not by a physical YAML property.
    return _map(
        ("name", component.name),
        ("type", "application"),
        ("description", component.description),
        ("ports", ports),
        ("runnables", runnables),
        ("memory", dict(component.memory)),
        # A component declaring no level omits ``asil``; no WATERS component is ABSENT.
        *((("asil", component.asil.value),) if component.asil is not ASIL.ABSENT else ()),
        ("handle_termination_and_restart", HANDLE_TERMINATION_AND_RESTART),
        *_satisfies(satisfies.get((SWC_SITE, component.name, None), [])),
    )


class _FileMap(dict[str, bytes]):
    """A file map that refuses to write one path twice, so a name collision never overwrites."""

    def __setitem__(self, path: str, data: bytes) -> None:
        if path in self:
            raise GenerationError(f"two generated files share one path: {path}")
        super().__setitem__(path, data)


def _connector_endpoint(value: ConnectorEndpoint) -> dict[str, Any]:
    """A connector's end as a mapping: its component or composition when it names one, then the port."""
    endpoint_pairs: list[tuple[str, Any]] = []
    if value.swc is not None:
        endpoint_pairs.append(("swc", value.swc))
    if value.composition is not None:
        endpoint_pairs.append(("composition", value.composition))
    endpoint_pairs.append(("port", value.port))
    return _map(*endpoint_pairs)


def _add_component_files(files: _FileMap, project: Project, satisfies: Mapping[Site, list[str]]) -> None:
    """The SWC layer: one document per component, under its group directory."""
    for component in project.components:
        files[f"model/swc/{component.group}/{component.name}.yaml"] = _yaml(
            _swc(component, satisfies, project.emit_runnable_wcet)
        )


def _add_requirement_files(files: _FileMap, project: Project) -> None:
    """The requirements layer: one document per requirement."""
    for requirement in project.requirements:
        files[f"model/requirements/{requirement.id}.yaml"] = _yaml(
            _map(
                ("id", requirement.id),
                ("title", requirement.title),
                ("description", requirement.description),
                # A requirement allocating no level omits ``asil`` (a specimen's).
                *((("asil", requirement.asil.value),) if requirement.asil is not ASIL.ABSENT else ()),
                ("version", requirement.version),
                ("source", REQUIREMENT_SOURCE),
            )
        )


def _add_composition_files(files: _FileMap, project: Project, satisfies: Mapping[Site, list[str]]) -> None:
    """The compositions layer: one document per composition, with its members, ports and delegations."""
    for composition in project.compositions:
        members = []
        for m in composition.members:
            pairs: list[tuple[str, Any]] = [("name", m.name)]
            if m.type is not None:
                pairs.append(("type", m.type))
            if m.composition is not None:
                pairs.append(("composition", m.composition))
            if m.description:
                pairs.append(("description", m.description))
            members.append(_map(*pairs))
        ports = [
            _map(
                ("name", p.name),
                ("direction", p.direction),
                ("interface", p.interface),
                *((("description", p.description),) if p.description else ()),
            )
            for p in composition.ports
        ]
        delegations = [
            _map(
                ("name", d.name),
                ("port", d.port),
                ("member", d.member),
                ("member_port", d.member_port),
                *((("description", d.description),) if d.description else ()),
            )
            for d in composition.delegations
        ]
        pairs = [("name", composition.name), ("title", composition.title)]
        if composition.root:
            pairs.append(("root", True))
        if composition.description:
            pairs.append(("description", composition.description))
        pairs += [("ports", ports), ("members", members), ("delegations", delegations)]
        site = (COMPOSITION_SITE, composition.name, None)
        pairs.extend(_satisfies(satisfies.get(site, [])))
        files[f"model/compositions/{composition.name}.yaml"] = _yaml(_map(*pairs))


def _add_interface_files(files: _FileMap, project: Project, satisfies: Mapping[Site, list[str]]) -> None:
    """The interfaces layer: one document per interface."""
    for interface in project.interfaces:
        files[f"model/interfaces/{interface.name}.yaml"] = _yaml(
            _interface(interface, satisfies.get((INTERFACE_SITE, interface.name, None), []))
        )


def _add_data_type_layer_files(
    files: _FileMap, project: Project, layer: str, type_layers: dict[str, list[tuple[str, dict[str, Any]]]]
) -> None:
    """One ``data_types/`` layer: the project's data types stored there, then the type-layer documents of that layer."""
    for data_type in project.data_types:
        if data_type_directory(data_type) == layer:
            files[f"model/{layer}/{data_type.name}.yaml"] = _yaml(_data_type(data_type))
    for name, document in type_layers.get(layer, []):
        files[f"model/{layer}/{name}.yaml"] = _yaml(document)


def _add_connector_files(files: _FileMap, project: Project) -> None:
    """The connectors layer: one document per connector."""
    for connector in project.connectors:
        pairs: list[tuple[str, Any]] = [
            ("name", connector.name),
            ("from", _connector_endpoint(connector.source)),
            ("to", _connector_endpoint(connector.target)),
        ]
        if connector.composition is not None:
            pairs.append(("composition", connector.composition))
        if connector.description:
            pairs.append(("description", connector.description))
        files[f"model/connectors/{connector.name}.yaml"] = _yaml(_map(*pairs))


def _add_task_files(files: _FileMap, project: Project, satisfies: Mapping[Site, list[str]]) -> None:
    """The tasks layer: one document per task."""
    for task in project.tasks:
        # Task YAML has no asil property; the effective ASIL stays in the sidecar.
        files[f"model/tasks/{task.name}.yaml"] = _yaml(
            _map(
                ("name", task.name),
                ("period", Decimal(task.period_us) / Decimal(1_000_000)),
                ("priority", task.priority),
                ("partition", task_partition(task)),
                ("wcet", _microseconds_text(task.wcet_us)),
                (
                    "runnables",
                    [_map(("swc", s), ("runnable", r)) for s, r in task.runnables],
                ),
                *((("coexistence", task.coexistence),) if task.coexistence else ()),
                *_satisfies(satisfies.get((TASK_SITE, task.name, None), [])),
            )
        )


def _add_sequence_files(files: _FileMap, project: Project) -> None:
    """The sequences layer: one document per sequence."""
    for sequence in project.sequences:
        # The title and every note are written empty.
        files[f"model/sequences/{sequence.name}.yaml"] = _yaml(
            _map(
                ("name", sequence.name),
                ("task", sequence.task),
                ("title", ""),
                (
                    "steps",
                    [_map(("swc", s), ("runnable", r), ("note", "")) for s, r in sequence.steps],
                ),
            )
        )


def _add_hardware_files(files: _FileMap, project: Project, satisfies: Mapping[Site, list[str]]) -> None:
    """The hardware layer: the processor, cores, memories and partitions, in that order.

    Raises:
        GenerationError: the emitted elements are not :func:`hardware_names`' names
            in its order.
    """
    hardware = [
        (
            project.processor.name,
            _map(("name", project.processor.name), ("kind", "processor")),
        )
    ]
    hardware += [
        (
            item.name,
            _map(
                ("name", item.name),
                ("kind", "core"),
                ("threshold", item.threshold),
                ("enforcement", "strict"),
                *((("asil_capability", item.capability.value),) if item.capability.value != "absent" else ()),
            ),
        )
        for item in project.cores
    ]
    hardware += [
        (
            item.name,
            _map(
                ("name", item.name),
                ("kind", "memory"),
                ("type", "RAM"),
                ("size", item.size_bytes),
                ("accessible", list(item.accessible)),
                ("threshold", item.threshold),
                ("enforcement", "strict"),
            ),
        )
        for item in project.memories
    ]
    hardware += [
        (
            item.name,
            _map(
                ("name", item.name),
                ("kind", "partition"),
                ("core", item.core),
                # A rated partition (a specimen's); a derived one is never rated.
                *((("asil", item.asil.value),) if item.asil is not ASIL.ABSENT else ()),
                *((("coexistence", item.coexistence),) if item.coexistence else ()),
            ),
        )
        for item in partitions(project)
    ]
    if tuple(name for name, _ in hardware) != hardware_names(project):
        raise GenerationError("emitted hardware elements differ from model.hardware_names")
    for name, value in hardware:
        value.update(_satisfies(satisfies.get((HARDWARE_SITE, name, None), [])))
        files[f"model/hardware/{name}.yaml"] = _yaml(value)


def build_file_map(project: Project) -> dict[str, bytes]:
    """Return the complete project tree as an ordered ``path -> bytes`` map, touching no filesystem.

    The map starts with ``swcraft.yaml``, then one section per layer of :data:`STORAGE_DIRECTORIES`, in
    that order; an empty layer contributes a ``.gitkeep``. Two files that would share one path raise
    :class:`GenerationError`.
    """
    files = _FileMap()
    # The SWCraft release the project names; none found, no key.
    pinned = toolchain_version()
    files["swcraft.yaml"] = _yaml(
        _map(
            ("name", project.name),
            ("schema_version", _Quoted("1.0")),
            *((("toolchain_version", _Quoted(pinned)),) if pinned is not None else ()),
            ("model_root", "model"),
            ("dist_root", "dist"),
            ("views_root", "views"),
            ("outputs", _map(("arxml", "dist/model.arxml"), ("needs", "dist/needs.json"))),
        )
    )
    satisfies = satisfied_requirements(project)
    type_layers = type_layer_documents(project)
    for layer in STORAGE_DIRECTORIES:
        before = len(files)
        if layer == "swc":
            _add_component_files(files, project, satisfies)
        elif layer == "requirements":
            _add_requirement_files(files, project)
        elif layer == "compositions":
            _add_composition_files(files, project, satisfies)
        elif layer == "interfaces":
            _add_interface_files(files, project, satisfies)
        elif layer.startswith("data_types/"):
            _add_data_type_layer_files(files, project, layer, type_layers)
        elif layer == "connectors":
            _add_connector_files(files, project)
        elif layer == "tasks":
            _add_task_files(files, project, satisfies)
        elif layer == "sequences":
            _add_sequence_files(files, project)
        elif layer == "hardware":
            _add_hardware_files(files, project, satisfies)
        if len(files) == before:
            files[f"model/{layer}/.gitkeep"] = b""
    return dict(files)


def materialize(file_map: dict[str, bytes], output: Path) -> None:
    """Publish a complete file map transactionally."""
    validate_output(output)
    temporary: Path | None = None
    backup: Path | None = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
        for relative, data in file_map.items():
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if output.exists():
            backup = output.with_name(f".{output.name}.backup")
            if backup.exists():
                raise GenerationError(f"cannot publish output: backup path exists: {backup}")
            output.rename(backup)
        temporary.rename(output)
        temporary = None
        if backup is not None:
            backup.rmdir()
            backup = None
    except OSError as exc:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        if backup is not None and not output.exists():
            try:
                backup.rename(output)
            except OSError:
                pass
        raise GenerationError(f"cannot write generated project: {exc}") from None
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
