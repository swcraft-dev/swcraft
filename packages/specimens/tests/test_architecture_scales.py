from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from swcraft_specimens.architecture import (
    architecture_fingerprint,
    build_architecture,
    resolve_type_size,
    validate_architecture,
)
from swcraft_specimens.preset import load_preset

PYTHON = sys.executable


def fixture(tmp_path: Path, components: int, domains: int, seed: int) -> Path:
    wcet = "\n".join(
        f"  {period}: {{min: 1, average: 100, max: 500}}" for period in (1, 2, 5, 10, 20, 50, 100, 200, 1000)
    )
    return_path = tmp_path / f"p{components}.yaml"
    return_path.write_text(
        f"""name: phase4_{components}
seed: {seed}
components: {components}
domains: {domains}
cores:
  - {{name: CORE0, asil_capability: D}}
  - {{name: CORE1, asil_capability: D}}
  - {{name: CORE2, asil_capability: D}}
core_threshold: 0.7
memories:
  shared: {{size: 1, threshold: 1}}
  private_per_core: {{size: 1, threshold: 1}}
periods_ms: {{1: 3, 2: 2, 5: 2, 10: 25, 20: 25, 50: 3, 100: 20, 200: 1, 1000: 4}}
runnables_per_component: {{min: 1, max: 6}}
wcet_us_by_period:
{wcet}
signal_size_bytes: {{1: 35, 2: 49, 4: 13, 5: 1, 9: 1, 17: 1, 33: 1, 65: 1}}
intra_domain_link_density: 0.2
inter_domain_link_density: 0.03
client_server_share: 0.1
asil_weights: {{B: 1}}
displaced_components: 0
private_memory_share: 0.1
target_core_load: 0.1
task_overhead_us: 5
emit_runnable_wcet: false
""",
        encoding="utf-8",
    )
    return return_path


def test_the_scales_are_in_memory_and_exact(tmp_path: Path) -> None:
    for components, domains in ((15, 3), (60, 6), (160, 8)):
        preset = load_preset(fixture(tmp_path, components, domains, 100 + components))
        first = build_architecture(preset)
        second = build_architecture(preset)
        assert first == second
        validate_architecture(first, preset)
        assert len(first.components) == components
        assert len(first.domains) == domains
        assert first.root.members == tuple(domain.name for domain in first.domains)
        assert all(
            resolve_type_size(first.data_types, element.type) == element.size_bytes
            for interface in first.interfaces
            for element in interface.data_elements
        )
        assert any(link.motif == "domain_gateway" for link in first.connectors) if domains > 1 else True
        assert not hasattr(first, "tasks")
        assert not hasattr(first, "placement")


def test_a_seed_changes_partition_and_stochastic_fields(tmp_path: Path) -> None:
    first = build_architecture(load_preset(fixture(tmp_path, 15, 3, 1)))
    second = build_architecture(load_preset(fixture(tmp_path, 15, 3, 2)))
    assert tuple(len(d.components) for d in first.domains) != tuple(len(d.components) for d in second.domains) or tuple(
        d.components for d in first.domains
    ) != tuple(d.components for d in second.domains)
    assert architecture_fingerprint(first) != architecture_fingerprint(second)


def test_type_resolver_reports_unresolved_and_cycle() -> None:
    import pytest
    from swcraft_specimens.errors import GenerationError
    from swcraft_specimens.model import ArrayType, ValueType

    with pytest.raises(GenerationError, match="unresolved"):
        resolve_type_size((ValueType("x", "missing"),), "x")
    with pytest.raises(GenerationError, match="cyclic"):
        resolve_type_size((ValueType("x", "y"), ArrayType("y", "x", 1)), "x")


def test_the_architecture_fingerprint_is_hash_seed_independent(tmp_path: Path) -> None:
    preset = fixture(tmp_path, 15, 3, 77)
    code = (
        "from pathlib import Path; from swcraft_specimens.preset import load_preset; "
        "from swcraft_specimens.architecture import build_architecture, architecture_fingerprint; "
        "print(architecture_fingerprint(build_architecture(load_preset(Path(r'" + str(preset) + "')))))"
    )
    outputs = []
    for hash_seed in ("1", "999"):
        env = dict(os.environ, PYTHONHASHSEED=hash_seed)
        outputs.append(subprocess.check_output([PYTHON, "-c", code], env=env, text=True))
    assert outputs[0] == outputs[1]
