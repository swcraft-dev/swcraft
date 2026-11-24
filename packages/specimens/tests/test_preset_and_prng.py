from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from swcraft_specimens.arithmetic import (
    client_server_rate,
    cpu_load,
    milliseconds_to_microseconds,
    require_exact_byte_rate,
    sender_receiver_rate,
    sum_cpu_loads,
)
from swcraft_specimens.deterministic import (
    SplitMix64,
    sample_without_replacement,
    shuffle,
    uneven_partition,
    weighted_choice,
)
from swcraft_specimens.names import component_name, validate_identifier, validate_unique_identifiers
from swcraft_specimens.packaged import preset_path
from swcraft_specimens.preset import ASIL, Preset, load_preset

PRESET = """\
name: test_model
seed: 20260920
components: 10
domains: 2
cores:
  - {name: CORE_A, asil_capability: D}
  - {name: CORE_B, asil_capability: null}
core_threshold: 0.7
memories:
  shared: {size: 4194304, threshold: 0.8}
  private_per_core: {size: 524288, threshold: 0.8}
periods_ms: {1: 3, 2: 2, 5: 2, 10: 25, 20: 25, 50: 3, 100: 20, 200: 1, 1000: 4}
runnables_per_component: {min: 1, max: 6}
wcet_us_by_period:
  1: {min: 1, average: 5, max: 30}
  2: {min: 1, average: 5, max: 30}
  5: {min: 1, average: 5, max: 30}
  10: {min: 1, average: 5, max: 30}
  20: {min: 1, average: 5, max: 30}
  50: {min: 1, average: 5, max: 30}
  100: {min: 1, average: 5, max: 30}
  200: {min: 1, average: 5, max: 30}
  1000: {min: 1, average: 5, max: 30}
signal_size_bytes: {1: 35, 2: 49, 4: 13}
intra_domain_link_density: 0.35
inter_domain_link_density: 0.02
client_server_share: 0.10
asil_weights: {QM: 55, A: 5, B: 20, C: 5, D: 15}
displaced_components: 2
private_memory_share: 0.15
target_core_load: 0.55
task_overhead_us: 5
emit_runnable_wcet: false
"""


def preset_file(tmp_path: Path, text: str = PRESET) -> Path:
    path = tmp_path / "preset.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_full_preset_is_typed_and_preserves_asil_semantics(tmp_path: Path) -> None:
    preset = load_preset(preset_file(tmp_path))
    assert isinstance(preset, Preset)
    assert preset.periods_us[0] == (1000, 3)
    assert preset.cores[1].capability is ASIL.ABSENT
    assert dict(preset.asil_weights)[ASIL.QM] == 55


@pytest.mark.parametrize(
    ("needle", "change"),
    [
        ("preset.unknown", "unknown: 1\n"),
        (
            "memories.shared.extra",
            "memories:\n  shared: {size: 1, threshold: 1, extra: 2}\n  private_per_core: {size: 1, threshold: 1}\n",
        ),
        ("preset.name", "name: null\n"),
        ("components", "components: true\n"),
        ("domains", "domains: 11\n"),
        (
            "cores",
            "cores:\n  - {name: CORE_A, asil_capability: D}\n  - {name: core_a, asil_capability: D}\n",
        ),
        ("asil_weights.QM", "asil_weights: {QM: 0}\n"),
        ("emit_runnable_wcet", "emit_runnable_wcet: 1\n"),
        ("runnables_per_component", "runnables_per_component: {min: 3, max: 1}\n"),
        ("displaced_components", "displaced_components: 10\n"),
        ("task_overhead_us", "task_overhead_us: 0\n"),
    ],
)
def test_preset_rejects_invalid_values_with_paths(tmp_path: Path, needle: str, change: str) -> None:
    before = PRESET
    if change.startswith("memories:") or change.startswith("cores:"):
        text = (
            re.sub(r"(?ms)^memories:.*?^periods_ms:", change + "periods_ms:", before)
            if change.startswith("memories:")
            else re.sub(r"(?ms)^cores:.*?^core_threshold:", change + "core_threshold:", before)
        )
    else:
        key = change.split(":", 1)[0]
        text = re.sub(rf"(?m)^{re.escape(key)}:.*$", change.rstrip(), before) if key in before else before + change
    expected = needle if needle == "preset.unknown" else needle.removeprefix("preset.")
    if expected == "asil_weights.QM":
        expected = "asil_weights"
    with pytest.raises(Exception, match=expected.replace(".", r"\.")):
        load_preset(preset_file(tmp_path, text))


def test_a_preset_that_emits_runnable_budgets_loads(tmp_path: Path) -> None:
    text = PRESET.replace("emit_runnable_wcet: false", "emit_runnable_wcet: true")
    assert load_preset(preset_file(tmp_path, text)).emit_runnable_wcet is True


@pytest.mark.parametrize("name", ["small", "medium", "large"])
def test_the_shipped_presets_emit_runnable_budgets(name: str) -> None:
    assert load_preset(preset_path(name)).emit_runnable_wcet is True


def test_duplicate_yaml_keys_and_mismatched_execution_table(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="duplicate YAML key"):
        load_preset(preset_file(tmp_path, PRESET.replace("name: test_model", "name: test_model\nname: other")))
    text = PRESET.replace("  1000: {min: 1, average: 5, max: 30}\n", "")
    with pytest.raises(Exception, match="wcet_us_by_period"):
        load_preset(preset_file(tmp_path, text))


def test_period_exactness_and_rate_exactness(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="periods_ms.0.0001"):
        load_preset(preset_file(tmp_path, PRESET.replace("periods_ms: {1:", "periods_ms: {0.0001:")))
    with pytest.raises(Exception, match="exact integer byte rate"):
        load_preset(preset_file(tmp_path, PRESET.replace("periods_ms: {1:", "periods_ms: {3:")))


def test_prng_golden_vectors_and_operations() -> None:
    rng = SplitMix64(0)
    assert [rng.next_u64() for _ in range(5)] == [
        16294208416658607535,
        7960286522194355700,
        487617019471545679,
        17909611376780542444,
        1961750202426094747,
    ]
    assert [SplitMix64(123).bounded(10) for _ in range(1)] == [5]
    assert weighted_choice(SplitMix64(1), ["a", "b", "c"], [0, 1, 0]) == "b"
    assert shuffle(SplitMix64(7), [0, 1, 2, 3, 4]) == [4, 1, 3, 0, 2]
    assert sample_without_replacement(SplitMix64(7), list(range(6)), 3) == [1, 5, 0]
    result = uneven_partition(12, 4, SplitMix64(7))
    assert len(result) == 4 and sum(result) == 12 and all(part > 0 for part in result)


def test_prng_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        SplitMix64(1).bounded(0)
    with pytest.raises(ValueError):
        weighted_choice(SplitMix64(1), [1], [0])
    with pytest.raises(ValueError):
        sample_without_replacement(SplitMix64(1), [1], 2)
    with pytest.raises(ValueError):
        uneven_partition(2, 3, SplitMix64(1))


def test_exact_arithmetic() -> None:
    assert milliseconds_to_microseconds("0.03") == 30
    with pytest.raises(ValueError):
        milliseconds_to_microseconds("0.0001")
    from fractions import Fraction

    assert cpu_load(3, 10) == Fraction(3, 10)
    assert sum_cpu_loads([cpu_load(1, 3), cpu_load(1, 6)]) == Fraction(1, 2)
    assert sender_receiver_rate(3, 10_000) == 300
    assert client_server_rate(2, 3, 10_000) == 500
    assert require_exact_byte_rate(sender_receiver_rate(3, 10_000)) == 300
    with pytest.raises(ValueError):
        require_exact_byte_rate(sender_receiver_rate(1, 3_000))


def test_identifier_rules_and_stable_names() -> None:
    assert validate_identifier("A_1") == "A_1"
    assert component_name(3, 47) == "Dom03_Swc047"
    with pytest.raises(ValueError, match="bad-name"):
        validate_identifier("bad-name", "component name")
    with pytest.raises(ValueError, match="max 60"):
        validate_identifier("A" * 61, "generated name", generated=True)
    with pytest.raises(ValueError, match="ignoring case"):
        validate_unique_identifiers(["Port_A", "port_a"], "ports")


def test_hash_seed_independent_subprocess() -> None:
    code = (
        "from swcraft_specimens.deterministic import *; r=SplitMix64(7); "
        "print(shuffle(r, ['a','b','c','d']), uneven_partition(10,3,r))"
    )
    outputs = []
    for hash_seed in ("1", "999"):
        env = dict(os.environ, PYTHONHASHSEED=hash_seed)
        outputs.append(subprocess.check_output([sys.executable, "-c", code], env=env, text=True))
    assert outputs == ["['b', 'c', 'a', 'd'] [4, 3, 3]\n"] * 2
