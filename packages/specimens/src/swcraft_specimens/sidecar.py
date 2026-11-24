"""Deterministic generation.json construction and independent validation.

Two sidecar kinds share the file name. A WATERS sidecar (schema 2.1) carries no `kind` key; a
specimen sidecar states `kind: specimen` and has a schema of its own, 1.0. `sidecar_kind` reads the kind first, and each
validator refuses a sidecar of the other kind before it reads anything else.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any

import yaml

from . import __version__
from .architecture import Architecture
from .errors import GenerationError
from .metrics import AsilCount, GenerationMetrics, PlacementMetric, build_metrics, layer_counts
from .model import Project
from .passport import PassportRefused, RenderedDocuments, compute_passport, validate_passport
from .placement import PlacementResult
from .preset import Preset
from .specimen.preset import SPECIMEN_KIND, SpecimenPreset, preset_text
from .toolchain import is_exact_release, toolchain_version

# 2.1 adds the field ``passport``, the passport of the generated model.
GENERATION_SCHEMA_VERSION = "2.1"

#: The kind of a sidecar without a `kind` key.
WATERS_KIND = "waters"
#: The specimen sidecar's schema; its kind is the specimen preset's, `SPECIMEN_KIND`.
SPECIMEN_SCHEMA_VERSION = "1.0"
#: The specimen sidecar's keys, in their written order.
SPECIMEN_KEYS = (
    "kind",
    "schema_version",
    "generator",
    "toolchain",
    "preset",
    "project",
    "passport",
)
SPECIMEN_PROJECT_KEYS = ("layer_counts", "project_tree_sha256")


def _generator() -> dict[str, str]:
    """The `generator` entry of a sidecar this version writes."""
    return {"name": "swcraft-specimens", "version": __version__}


def _toolchain() -> dict[str, str | None]:
    """The `toolchain` entry of a sidecar written now.

    The SWCraft release the manifest names, or `null` when none is found.
    """
    return {"swcraft_version": toolchain_version()}


def _check_toolchain(document: Mapping[str, Any], file_map: Mapping[str, bytes]) -> None:
    """The sidecar's `swcraft_version` is the manifest's `toolchain_version`, or `null`.

    It is `null` when the manifest has none; a present value is one exact release.
    """
    toolchain = document.get("toolchain")
    if not isinstance(toolchain, dict) or tuple(toolchain) != ("swcraft_version",):
        raise GenerationError("generation.json.toolchain.swcraft_version mismatch")
    try:
        manifest = yaml.safe_load(file_map["swcraft.yaml"].decode("utf-8"))
    except (KeyError, UnicodeError, yaml.YAMLError):
        raise GenerationError("generation.json.toolchain: no readable swcraft.yaml") from None
    named = manifest.get("toolchain_version") if isinstance(manifest, dict) else None
    version = toolchain["swcraft_version"]
    if version != named or (version is not None and not is_exact_release(version)):
        raise GenerationError("generation.json.toolchain.swcraft_version mismatch")


def _load_sidecar(sidecar_bytes: bytes) -> dict[str, Any]:
    """A sidecar's bytes parsed as a JSON object, or a `GenerationError`."""
    try:
        document = json.loads(sidecar_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GenerationError(f"generation.json is invalid: {exc}") from None
    except RecursionError:
        raise GenerationError("generation.json is invalid: it nests deeper than the JSON reader follows") from None
    if not isinstance(document, dict):
        raise GenerationError("generation.json is not an object")
    return document


def _assert_no_float_but_passport(document: Mapping[str, Any]) -> None:
    """The float ban, which holds everywhere but `passport`: a passport's `mean_length` is a float.

    The passport is held instead by the interpreter of the vendored schema and by its
    recomputation from the rendered documents.
    """
    _assert_no_float({key: value for key, value in document.items() if key != "passport"})


def sidecar_kind(document: Mapping[str, Any]) -> str:
    """The kind of a parsed sidecar: `waters` without a `kind` key, else its stated kind."""
    if "kind" not in document:
        return WATERS_KIND
    kind = document["kind"]
    if kind != SPECIMEN_KIND:
        raise GenerationError("generation.json.kind: neither a WATERS nor a specimen sidecar")
    return SPECIMEN_KIND


def project_tree_sha256(file_map: Mapping[str, bytes]) -> str:
    digest = sha256()
    for path in sorted(file_map):
        if PurePosixPath(path).name == "generation.json" or path.startswith("dist/") or path.startswith("views/"):
            continue
        normalized = PurePosixPath(path).as_posix()
        if normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
            raise GenerationError(f"invalid project tree path: {path}")
        data = file_map[path]
        encoded = normalized.encode("utf-8")
        digest.update(encoded)
        digest.update(b"\0")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _fraction(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _decimal(value: Decimal) -> str:
    return format(value, "f")


def _preset(preset: Preset) -> dict[str, Any]:
    return {
        "name": preset.name,
        "seed": preset.seed,
        "parameters": {
            "components": preset.components,
            "domains": preset.domains,
            "cores": [c.name for c in preset.cores],
            "core_threshold": _decimal(preset.core_threshold),
            "displaced_components": preset.displaced_components,
            "private_memory_share": _decimal(preset.private_memory_share),
            "target_core_load": _decimal(preset.target_core_load),
            "task_overhead_us": preset.task_overhead_us,
            "emit_runnable_wcet": preset.emit_runnable_wcet,
        },
        "units": {
            "core_threshold": "load fraction",
            "target_core_load": "load fraction",
            "private_memory_share": "fraction",
        },
    }


def _asil_counts(values: tuple[AsilCount, ...]) -> list[dict[str, Any]]:
    return [{"asil": x.asil.value, "count": x.count} for x in values]


def _placement(value: PlacementMetric) -> dict[str, Any]:
    return {
        "component_to_core": [{"component": c, "core": k} for c, k in value.component_to_core],
        "core_metrics": [
            {
                "core": x.core,
                "task_count": x.task_count,
                "non_empty_task_count": x.non_empty_task_count,
                "load": _fraction(x.load),
                "threshold": _fraction(x.threshold),
                "remaining_capacity": _fraction(x.remaining_capacity),
            }
            for x in value.core_metrics
        ],
        "sender_receiver_exchange_cost": _fraction(value.sender_receiver_cost),
        "client_server_exchange_cost": _fraction(value.client_server_cost),
        "total_exchange_cost": _fraction(value.total_cost),
        "displaced_components": list(value.displaced),
        "pinned_components": list(value.pinned),
        "configured_displacement_count": value.configured_displacement_count,
        "actual_displacement_count": value.actual_displacement_count,
    }


def build_generation_document(
    architecture: Architecture,
    result: PlacementResult,
    project: Project,
    preset: Preset,
    tree_digest: str,
    metrics: GenerationMetrics | None = None,
    *,
    passport: Mapping[str, Any],
) -> dict[str, Any]:
    """The sidecar document.

    ``passport`` is ``passport.compute_passport`` of the rendered documents, which the caller has
    from the file-map gate.
    """
    metrics = metrics or build_metrics(architecture, result, project, preset)
    component_by_name = {c.name: c for c in architecture.components}
    connector_records = []
    planted = dict(result.planted.mapping)
    emitted = dict(result.emitted.mapping)
    for link in architecture.connectors:
        provider = component_by_name[link.provider]
        consumer = component_by_name[link.consumer]
        same_domain = provider.domain == consumer.domain
        rate = (
            Fraction(link.payload_bytes * 1_000_000, link.period_us)
            if link.kind == "sender_receiver"
            else Fraction((link.request_bytes + link.response_bytes) * 1_000_000, link.period_us)
        )
        connector_records.append(
            {
                "identity": link.name,
                "kind": link.kind,
                "provider": {"component": link.provider, "runnable": link.provider_runnable},
                "consumer": {"component": link.consumer, "runnable": link.consumer_runnable},
                "physical_owner": link.owner or "SyntheticVehicleArchitecture",
                "physical_endpoints": {
                    "from": {"composition": provider.domain, "port": link.provider_port}
                    if not same_domain
                    else {"swc": link.provider, "port": link.provider_port},
                    "to": {"composition": consumer.domain, "port": link.consumer_port}
                    if not same_domain
                    else {"swc": link.consumer, "port": link.consumer_port},
                },
                "period_us": link.period_us,
                "payload_bytes": link.payload_bytes if link.kind == "sender_receiver" else None,
                "request_bytes": link.request_bytes if link.kind == "client_server" else None,
                "response_bytes": link.response_bytes if link.kind == "client_server" else None,
                "byte_rate": _fraction(rate),
                "intra_domain": same_domain,
                "inter_domain": not same_domain,
                "planted_same_core": planted[link.provider] == planted[link.consumer],
                "emitted_same_core": emitted[link.provider] == emitted[link.consumer],
                "motif": link.motif,
            }
        )
    timing_records = []
    for component in architecture.components:
        for runnable in component.runnables:
            timing_records.append(
                {
                    "component": component.name,
                    "runnable": runnable.name,
                    "period_us": runnable.period_us,
                    "raw_execution_us": _decimal(runnable.raw_execution_us),
                    "wcet_us": runnable.wcet_us,
                    "calibration_factor": _fraction(runnable.calibration_factor),
                }
            )
    metadata = dict(architecture.metadata)
    domains = [
        {
            "identifier": d.token,
            "title": d.title,
            "token": d.token,
            "description": d.description,
            "composition": d.name,
            "components": list(d.components),
        }
        for d in architecture.domains
    ]
    component_records = [
        {
            "component": c.name,
            "domain": c.domain,
            "semantic_role": c.architectural_role,
            "description": c.description,
            "asil": c.asil.value,
            "runnables": len(c.runnables),
        }
        for c in architecture.components
    ]
    hierarchy = [
        {
            "name": c.name,
            "root": c.root,
            "description": c.description,
            "ports": [asdict(p) for p in c.ports],
            "members": [asdict(m) for m in c.members],
            "delegations": [asdict(d) for d in c.delegations],
        }
        for c in project.compositions
    ]
    return {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "generator": _generator(),
        "toolchain": _toolchain(),
        "preset": _preset(preset),
        "project": {
            "name": project.name,
            "project_tree_sha256": tree_digest,
            "layer_counts": asdict(metrics.layer_counts),
        },
        "architecture": {
            "domains": domains,
            "composition_hierarchy": hierarchy,
            "domain_counts": [asdict(x) for x in metrics.domain_counts],
            "components": component_records,
            "period_distribution": [asdict(x) for x in metrics.period_distribution],
            "domain_asil_distribution": _asil_counts(metrics.domain_asil),
            "component_asil_distribution": _asil_counts(metrics.component_asil),
            "runnable_wcet_us": [{"component": c, "runnable": r, "wcet_us": w} for c, r, w in metrics.runnable_wcet_us],
            "runnable_timing": timing_records,
            "target_runnable_load": _fraction(metadata["target_load"]),
            "achieved_runnable_load": _fraction(metadata["achieved_load"]),
            "calibration_residual": _fraction(metadata["residual"]),
            "calibration_tolerance": _fraction(metadata["calibration_tolerance"]),
            "global_calibration_factor": _fraction(metadata["calibration_factor"]),
        },
        "timing": {
            "runnables": timing_records,
            "units": {
                "period_us": "microseconds",
                "raw_execution_us": "microseconds",
                "wcet_us": "microseconds",
            },
        },
        "communication": {
            "connectors": connector_records,
            "summary": asdict(metrics.communication),
            "units": {"byte_rate": "bytes_per_second"},
        },
        "resources": {
            "memory": [
                {
                    "memory": x.memory,
                    "size_bytes": x.size_bytes,
                    "threshold_capacity_bytes": x.threshold_capacity,
                    "consumption_bytes": x.consumption,
                    "remaining_capacity_bytes": x.remaining_capacity,
                    "accessible_cores": list(x.accessible_cores),
                }
                for x in metrics.memory
            ],
            "budgets": [
                {"component": b.component, "memory": b.memory, "bytes": b.bytes} for b in result.memory_budgets
            ],
        },
        "placement": {
            "planted": _placement(metrics.planted),
            "emitted": _placement(metrics.emitted),
            "proof": {
                "emitted_cost_greater_than_planted": result.emitted_cost.total > result.planted_cost.total,
                "both_placements_feasible": True,
                "every_component_single_core": True,
            },
        },
        "landmarks": asdict(metrics.landmarks),
        "passport": dict(passport),
    }


def generation_json_bytes(document: dict[str, Any]) -> bytes:
    text = json.dumps(document, ensure_ascii=False, indent=2, separators=(",", ": "), allow_nan=False) + "\n"
    return text.encode("utf-8")


def _assert_no_float(value: object, path: str = "generation.json") -> None:
    if isinstance(value, float):
        raise GenerationError(f"{path}: floating point value is forbidden")
    if isinstance(value, dict):
        for key, child in value.items():
            _assert_no_float(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_float(child, f"{path}[{index}]")


def _validate_passport_field(value: object, documents: RenderedDocuments) -> None:
    if value is None:
        raise GenerationError("generation.json.passport is missing")
    try:
        validate_passport(value)
    except PassportRefused as exc:
        raise GenerationError(f"generation.json.passport is not a valid passport:\n{exc}") from None
    # Compared as canonical text, so key order and an int against a float both count.
    expected = compute_passport(documents)
    if json.dumps(value, ensure_ascii=False) != json.dumps(expected, ensure_ascii=False):
        raise GenerationError("generation.json.passport does not match the rendered tree")


def validate_sidecar(
    sidecar_bytes: bytes,
    architecture: Architecture,
    result: PlacementResult,
    project: Project,
    preset: Preset,
    file_map: Mapping[str, bytes],
    *,
    documents: RenderedDocuments,
    expected_digest: str | None = None,
) -> None:
    """Check the sidecar against the model.

    ``documents`` are the file-map gate's parsed documents, from which the ``passport`` is
    recomputed.
    """
    document = _load_sidecar(sidecar_bytes)
    if sidecar_kind(document) != WATERS_KIND:
        raise GenerationError("generation.json.kind: a specimen sidecar, not a WATERS one")
    _assert_no_float_but_passport(document)
    _validate_passport_field(document.get("passport"), documents)
    if document.get("schema_version") != GENERATION_SCHEMA_VERSION:
        raise GenerationError(f"generation.json.schema_version: expected {GENERATION_SCHEMA_VERSION}")
    if (
        document.get("generator", {}).get("name") != "swcraft-specimens"
        or document.get("generator", {}).get("version") != __version__
    ):
        raise GenerationError("generation.json.generator version mismatch")
    _check_toolchain(document, file_map)
    digest = project_tree_sha256(file_map)
    if expected_digest is not None and document.get("project", {}).get("project_tree_sha256") != expected_digest:
        raise GenerationError("generation.json.project.project_tree_sha256 mismatch")
    if document.get("project", {}).get("project_tree_sha256") != digest:
        raise GenerationError("generation.json.project.project_tree_sha256 does not match rendered tree")
    expected = build_metrics(architecture, result, project, preset)
    actual_counts = document.get("project", {}).get("layer_counts")
    if actual_counts != asdict(expected.layer_counts):
        raise GenerationError("generation.json.project.layer_counts mismatch")
    if document.get("project", {}).get("name") != project.name:
        raise GenerationError("generation.json.project.name mismatch")
    timing = document.get("timing", {}).get("runnables", [])
    expected_timing = []
    for component in architecture.components:
        for runnable in component.runnables:
            expected_timing.append(
                {
                    "component": component.name,
                    "runnable": runnable.name,
                    "period_us": runnable.period_us,
                    "raw_execution_us": _decimal(runnable.raw_execution_us),
                    "wcet_us": runnable.wcet_us,
                    "calibration_factor": _fraction(runnable.calibration_factor),
                }
            )
    if timing != expected_timing:
        raise GenerationError("generation.json.timing.runnables mismatch")
    expected_budgets = [{"component": b.component, "memory": b.memory, "bytes": b.bytes} for b in result.memory_budgets]
    if document.get("resources", {}).get("budgets") != expected_budgets:
        raise GenerationError("generation.json.resources.budgets mismatch")
    for label, placement, metric in (
        ("planted", result.planted, expected.planted),
        ("emitted", result.emitted, expected.emitted),
    ):
        value = document.get("placement", {}).get(label, {})
        if value.get("core_metrics") != [
            {
                "core": x.core,
                "task_count": x.task_count,
                "non_empty_task_count": x.non_empty_task_count,
                "load": _fraction(x.load),
                "threshold": _fraction(x.threshold),
                "remaining_capacity": _fraction(x.remaining_capacity),
            }
            for x in metric.core_metrics
        ]:
            raise GenerationError(f"generation.json.placement.{label}.core_metrics mismatch")
        if (
            value.get("sender_receiver_exchange_cost") != _fraction(metric.sender_receiver_cost)
            or value.get("client_server_exchange_cost") != _fraction(metric.client_server_cost)
            or value.get("total_exchange_cost") != _fraction(metric.total_cost)
        ):
            raise GenerationError(f"generation.json.placement.{label}.exchange_cost mismatch")
        if value.get("displaced_components") != list(metric.displaced) or value.get("pinned_components") != list(
            metric.pinned
        ):
            raise GenerationError(f"generation.json.placement.{label}.component sets mismatch")
    connectors = document.get("communication", {}).get("connectors", [])
    if [x.get("identity") for x in connectors] != [x.name for x in architecture.connectors]:
        raise GenerationError("generation.json.communication connector order mismatch")
    if len(connectors) != len(architecture.connectors):
        raise GenerationError("generation.json.communication connector count mismatch")
    for actual, link in zip(connectors, architecture.connectors):
        expected_rate = (
            Fraction(link.payload_bytes * 1_000_000, link.period_us)
            if link.kind == "sender_receiver"
            else Fraction((link.request_bytes + link.response_bytes) * 1_000_000, link.period_us)
        )
        if (
            actual.get("kind") != link.kind
            or actual.get("provider") != {"component": link.provider, "runnable": link.provider_runnable}
            or actual.get("consumer") != {"component": link.consumer, "runnable": link.consumer_runnable}
            or actual.get("period_us") != link.period_us
            or actual.get("byte_rate") != _fraction(expected_rate)
            or actual.get("motif") != link.motif
        ):
            raise GenerationError(f"generation.json.communication connector mismatch: {link.name}")
    expected_landmarks = asdict(expected.landmarks)
    if document.get("landmarks") != expected_landmarks:
        raise GenerationError("generation.json.landmarks mismatch")
    identities = {c.name for c in architecture.components}
    component_records = document.get("architecture", {}).get("components", [])
    if len(component_records) != len(architecture.components):
        raise GenerationError("generation.json architecture component count mismatch")
    for item in component_records:
        if item.get("component") not in identities:
            raise GenerationError(f"generation.json architecture references absent component {item.get('component')}")
    if document.get("placement", {}).get("planted", {}).get("component_to_core") != [
        {"component": c, "core": k} for c, k in result.planted.mapping
    ]:
        raise GenerationError("generation.json placement.planted mapping mismatch")
    if document.get("placement", {}).get("emitted", {}).get("component_to_core") != [
        {"component": c, "core": k} for c, k in result.emitted.mapping
    ]:
        raise GenerationError("generation.json placement.emitted mapping mismatch")
    for landmark, identity in document.get("landmarks", {}).items():
        if identity is not None and not (
            identity in identities
            or identity in {i.name for i in architecture.interfaces}
            or identity in {r.id for r in architecture.requirements}
        ):
            raise GenerationError(f"generation.json.landmarks.{landmark} references absent identity {identity}")


# --- the specimen sidecar -----------------------------------------------------------------------


def specimen_preset_sha256(preset: SpecimenPreset) -> str:
    """The SHA-256 of the specimen preset's canonical text.

    The text is `preset_text` of its passport with its name always written, so a preset file's
    formatting and an explicit default name do not move it, and the `specimen` command and
    `preset-from-passport` + `generate` agree.
    """
    return sha256(preset_text(preset.passport, preset.name).encode("utf-8")).hexdigest()


def build_specimen_document(
    preset: SpecimenPreset, project: Project, tree_digest: str, passport: Mapping[str, Any]
) -> dict[str, Any]:
    """The specimen sidecar.

    The generator, the toolchain, the preset's digest, the layer counts and tree digest, and the
    passport the pass computed over the rendered documents.
    """
    return {
        "kind": SPECIMEN_KIND,
        "schema_version": SPECIMEN_SCHEMA_VERSION,
        "generator": _generator(),
        "toolchain": _toolchain(),
        "preset": {"sha256": specimen_preset_sha256(preset)},
        "project": {
            "layer_counts": asdict(layer_counts(project)),
            "project_tree_sha256": tree_digest,
        },
        "passport": dict(passport),
    }


def validate_specimen_sidecar(
    sidecar_bytes: bytes,
    preset: SpecimenPreset,
    project: Project,
    file_map: Mapping[str, bytes],
    *,
    documents: RenderedDocuments,
) -> None:
    """Check a specimen sidecar against the preset, the project and the rendered tree.

    ``documents`` are the file-map gate's parsed documents, from which the ``passport`` is
    recomputed.
    """
    document = _load_sidecar(sidecar_bytes)
    if sidecar_kind(document) != SPECIMEN_KIND:
        raise GenerationError("generation.json.kind: a WATERS sidecar, not a specimen one")
    if tuple(document) != SPECIMEN_KEYS:
        raise GenerationError("generation.json keys: not those of a specimen sidecar")
    _assert_no_float_but_passport(document)
    if document["schema_version"] != SPECIMEN_SCHEMA_VERSION:
        raise GenerationError(f"generation.json.schema_version: expected {SPECIMEN_SCHEMA_VERSION}")
    if document["generator"] != _generator():
        raise GenerationError("generation.json.generator version mismatch")
    _check_toolchain(document, file_map)
    if document["preset"] != {"sha256": specimen_preset_sha256(preset)}:
        raise GenerationError("generation.json.preset.sha256 mismatch")
    section = document["project"]
    if not isinstance(section, dict) or tuple(section) != SPECIMEN_PROJECT_KEYS:
        raise GenerationError("generation.json.project keys: not those of a specimen sidecar")
    expected_counts = asdict(layer_counts(project))
    if section["layer_counts"] != expected_counts or list(section["layer_counts"]) != list(expected_counts):
        raise GenerationError("generation.json.project.layer_counts mismatch")
    if section["project_tree_sha256"] != project_tree_sha256(file_map):
        raise GenerationError("generation.json.project.project_tree_sha256 does not match rendered tree")
    _validate_passport_field(document["passport"], documents)
    if {layer: document["passport"]["layers"][layer] for layer in expected_counts} != (expected_counts):
        raise GenerationError("generation.json.passport.layers differ from project.layer_counts")
