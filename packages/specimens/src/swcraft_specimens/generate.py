"""Production generation pipeline."""

from pathlib import Path

from .architecture import build_architecture, validate_architecture
from .deterministic import SplitMix64
from .emit import build_file_map, materialize
from .metrics import build_metrics
from .model import validate_project
from .passport import compute_passport
from .placement import architecture_to_project, build_placement
from .preset import Preset
from .sidecar import build_generation_document, generation_json_bytes, project_tree_sha256
from .validate import validate_file_map, validate_generation


def generate_project(preset: Preset, output: Path) -> None:
    """Build, validate, render, and publish a complete general project."""
    rng = SplitMix64(preset.seed)
    architecture = build_architecture(preset, rng)
    validate_architecture(architecture, preset)
    placement = build_placement(architecture, preset, rng)
    project = architecture_to_project(placement, preset)
    validate_project(project)
    file_map = build_file_map(project)
    metrics = build_metrics(architecture, placement, project, preset)
    digest = project_tree_sha256(file_map)
    # validate_generation parses the full tree again, independently, and recomputes the passport.
    documents = validate_file_map(file_map, project, task_overhead_us=preset.task_overhead_us)
    sidecar = generation_json_bytes(
        build_generation_document(
            architecture,
            placement,
            project,
            preset,
            digest,
            metrics,
            passport=compute_passport(documents),
        )
    )
    full_map = dict(file_map)
    full_map["generation.json"] = sidecar
    validate_generation(architecture, placement, project, preset, full_map, sidecar)
    materialize(full_map, output)
