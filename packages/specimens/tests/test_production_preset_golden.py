from __future__ import annotations

import functools
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from swcraft_specimens.architecture import Architecture, build_architecture, validate_architecture
from swcraft_specimens.deterministic import SplitMix64
from swcraft_specimens.emit import build_file_map
from swcraft_specimens.generate import generate_project
from swcraft_specimens.model import Project
from swcraft_specimens.packaged import preset_path
from swcraft_specimens.passport import compute_passport
from swcraft_specimens.placement import (
    PlacementResult,
    architecture_to_project,
    build_placement,
    placement_fingerprint,
)
from swcraft_specimens.preset import Preset, load_preset
from swcraft_specimens.sidecar import build_generation_document, generation_json_bytes, project_tree_sha256
from swcraft_specimens.validate import validate_file_map, validate_generation
from tests.oracle.generator_start import BIN_VARIABLE, generator_command
from tests.oracle.sidecar_golden import sidecar_golden_sha256
from tests.oracle.small_placement import exhaustive
from tests.oracle.swcraft_gate import (
    assert_live_passport_equals_sidecar,
    assert_swcraft_clean,
    run_checked,
)

ROOT = Path(__file__).parents[1]
Built = tuple[Preset, Architecture, PlacementResult, Project, dict[str, bytes], bytes]
PYTHON = sys.executable
SWCRAFT = os.environ.get("SWCRAFT_BIN")
PRESETS = tuple(preset_path(name) for name in ("small", "medium", "large"))
SPECIMEN_PRESETS = tuple(
    preset_path(name)
    for name in (
        "minimal",
        "topology",
        "large-scale-topology",
        "shapes",
        "chain-empty",
        "large-scale-shapes",
    )
)
# SWCraft's `swcraft passport`: the pinned release ships it, so `SWCRAFT_BIN` answers the passport too.
SWCRAFT_PASSPORT = os.environ.get("SWCRAFT_BIN")
# The generator the integration tests run: `SWCRAFT_SPECIMENS_BIN` when set (SWCraft's archive test
# names the installed `swcraft-specimens`), else this interpreter's `python -m swcraft_specimens`.
SWCRAFT_SPECIMENS_BIN = os.environ.get(BIN_VARIABLE)

EXPECTED_PROJECT_TREE_SHA256 = {
    # Update these goldens only after an intentional generator or preset-contract change and a
    # reviewed regeneration. Taken with no SWCraft found (the autouse fixture), so no
    # `toolchain_version` is written.
    "small": "39a8e1e820867f53483b5610d075b54aab90feb15dff4e81905eee13debaffe0",
    "medium": "dbca14196237ad9893a1af1c6f78f81a46ee4d5b22cee5b45723c0eabf65fae3",
    "large": "847a59df481df5fd02813e92e59e4d9fbfaf7b2834cb9d6d12c87a20e446f9b4",
}
EXPECTED_SIDECAR_SHA256 = {
    # Taken with `generator.version` read back to GOLDEN_GENERATOR_VERSION (tests/oracle/sidecar_golden.py),
    # so a SWCraft version bump moves none of them.
    "small": "73f7523759e0f6e61417bdcac81b294838a6869c0ab30232a190822cb6f2b772",
    "medium": "a26c22bf7f0b60468cfa3d783344b89164a47fd47cb34854bcb87ba0e2604967",
    "large": "9b68949ba6437c787c00f396a65f58818edc7afc7030e360ab08db75ececd3de",
}
EXPECTED_PLACEMENT_FINGERPRINT = {
    "small": "934372a498ae5e87e9159cdc9a62f3982bd439e7ec873336664f0c2dda60211c",
    "medium": "c32ff5812c7399b2e6cc94df01b4cbe9416c08f8390d0a7ae5450c0428a00ec1",
    "large": "bcddf64235d010548b31845af34efb25579ddb191237de58864419886b9e7849",
}
EXPECTED_COUNTS = {
    "small": (3, 15, 57, 21, 2, 23, 23, 5, 36, 11, 8, 2, 3, 3600, 41140),
    "medium": (6, 60, 196, 215, 24, 239, 239, 8, 51, 11, 8, 9, 8, 113555, 314601),
    "large": (8, 160, 532, 1175, 131, 1306, 1306, 11, 78, 11, 8, 24, 20, 1208684, 3665055),
}


def build(preset_path: Path) -> Built:
    preset = load_preset(preset_path)
    rng = SplitMix64(preset.seed)
    architecture = build_architecture(preset, rng)
    validate_architecture(architecture, preset)
    placement = build_placement(architecture, preset, rng)
    project = architecture_to_project(placement, preset)
    file_map = build_file_map(project)
    digest = project_tree_sha256(file_map)
    documents = validate_file_map(file_map, project, task_overhead_us=preset.task_overhead_us)
    sidecar = generation_json_bytes(
        build_generation_document(
            architecture,
            placement,
            project,
            preset,
            digest,
            passport=compute_passport(documents),
        )
    )
    return preset, architecture, placement, project, file_map, sidecar


def _tree(path: Path) -> dict[str, bytes]:
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in sorted(path.rglob("*"), key=lambda item: item.as_posix())
        if item.is_file()
    }


@pytest.mark.parametrize("preset_path", PRESETS, ids=lambda p: p.stem)
def test_production_preset_is_byte_deterministic_and_golden(preset_path: Path, tmp_path: Path) -> None:
    name = preset_path.stem
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_project(load_preset(preset_path), first)
    generate_project(load_preset(preset_path), second)
    assert _tree(first) == _tree(second)
    files = _tree(first)
    assert (
        project_tree_sha256({k: v for k, v in files.items() if k != "generation.json"})
        == EXPECTED_PROJECT_TREE_SHA256[name]
    )
    assert sidecar_golden_sha256(files["generation.json"]) == EXPECTED_SIDECAR_SHA256[name]
    document = json.loads(files["generation.json"])
    assert document["project"]["project_tree_sha256"] == EXPECTED_PROJECT_TREE_SHA256[name]
    preset, architecture, placement, project, file_map, sidecar = build(preset_path)
    validate_generation(
        architecture,
        placement,
        project,
        preset,
        dict(file_map, **{"generation.json": sidecar}),
        sidecar,
    )
    assert placement_fingerprint(placement) == EXPECTED_PLACEMENT_FINGERPRINT[name]
    assert set(files) == set(_tree(second))


@pytest.mark.parametrize("preset_path", PRESETS, ids=lambda p: p.stem)
def test_production_exact_counts_and_distributions(preset_path: Path) -> None:
    preset, architecture, placement, project, file_map, sidecar = build(preset_path)
    sr = sum(link.kind == "sender_receiver" for link in architecture.connectors)
    cs = sum(link.kind == "client_server" for link in architecture.connectors)
    counts = (
        len(architecture.domains),
        len(architecture.components),
        sum(len(c.runnables) for c in architecture.components),
        sr,
        cs,
        len(architecture.connectors),
        len(architecture.interfaces),
        len(architecture.requirements),
        len(placement.emitted_tasks.tasks),
        len(architecture.data_types),
        1 + len(placement.cores) + len(placement.memories),
        len(placement.pinned),
        len(placement.displaced),
        int(placement.planted_cost.total),
        int(placement.emitted_cost.total),
    )
    assert counts == EXPECTED_COUNTS[preset.name]
    assert all(
        preset.runnables_per_component.minimum <= len(c.runnables) <= preset.runnables_per_component.maximum
        for c in architecture.components
    )
    expected_periods = {period: 0 for period, _ in preset.periods_us}
    for component in architecture.components:
        for runnable in component.runnables:
            expected_periods[runnable.period_us] += 1
            params = dict(preset.wcet_us_by_period)[runnable.period_us]
            assert params.minimum_us <= runnable.raw_execution_us <= params.maximum_us
    total = sum(expected_periods.values())
    weights = dict(preset.periods_us)
    for period, count in expected_periods.items():
        assert (
            abs(count / total - weights[period] / sum(weights.values()))
            <= (0.25, 0.12, 0.08)[("small", "medium", "large").index(preset.name)]
        )
    sizes: list[int] = []
    for interface in architecture.interfaces:
        sizes.extend(e.size_bytes for e in interface.data_elements)
        sizes.extend(a.size_bytes for a in interface.arguments)
    assert sizes and set(sizes) <= {1, 2, 4, 5, 9, 17, 33, 65}
    assert placement.emitted_cost.total > placement.planted_cost.total


@pytest.mark.parametrize("preset_path", PRESETS, ids=lambda p: p.stem)
def test_integral_periods_are_plain_yaml_numbers(preset_path: Path) -> None:
    """SWCraft and its VS Code schema must agree on integral Decimal scalars."""
    _, _, _, _, file_map, _ = build(preset_path)
    yaml_files = {path: data for path, data in file_map.items() if path.endswith(".yaml")}
    assert all(b"!!float" not in data for data in yaml_files.values())

    one_second_periods: list[float] = []
    for path, data in yaml_files.items():
        document = yaml.safe_load(data)
        if path.startswith("model/tasks/") and document["period"] == 1.0:
            one_second_periods.append(document["period"])
        if path.startswith("model/swc/"):
            for runnable in document.get("runnables", []):
                one_second_periods.extend(
                    event["period"] for event in runnable.get("events", []) if event.get("period") == 1.0
                )
    assert one_second_periods
    assert all(isinstance(period, float) for period in one_second_periods)


def test_small_independent_exhaustive_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    preset, architecture, placement, project, file_map, sidecar = build(preset_path("small"))
    # The oracle's result is computed with the production placement and cost helpers patched out.
    import swcraft_specimens.placement as production_placement

    monkeypatch.setattr(
        production_placement,
        "build_placement",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("production search called")),
    )
    monkeypatch.setattr(
        production_placement,
        "_cost",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("production cost called")),
    )
    results = [exhaustive(architecture, placement, preset, n) for n in (1, 2, 3)]
    assert [item["N"] for item in results] == [1, 2, 3]
    assert [item["optimum_total_cost"]["numerator"] for item in results] == [25600, 11000, 3600]
    assert [item["enumerated_candidates"] for item in results] == [31, 451, 4091]
    assert all(
        {row["component"] for row in item["component_to_core"]} == {c.name for c in architecture.components}
        for item in results
    )
    assert results[2]["optimum_total_cost"]["numerator"] <= placement.planted_cost.total.numerator
    assert results[2]["optimum_total_cost"]["denominator"] == placement.planted_cost.total.denominator


def test_hash_seed_subprocess_determinism(tmp_path: Path) -> None:
    outputs = []
    for hash_seed in ("1", "999"):
        out = tmp_path / hash_seed
        completed = subprocess.run(
            [
                PYTHON,
                "-m",
                "swcraft_specimens",
                "generate",
                str(preset_path("large")),
                "--out",
                str(out),
            ],
            cwd=ROOT,
            env=dict(os.environ, PYTHONHASHSEED=hash_seed),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert completed.returncode == 0, completed.stderr
        outputs.append(
            (
                project_tree_sha256(
                    {
                        p.relative_to(out).as_posix(): p.read_bytes()
                        for p in out.rglob("*")
                        if p.is_file() and p.name != "generation.json"
                    }
                ),
                hashlib.sha256((out / "generation.json").read_bytes()).hexdigest(),
                json.loads((out / "generation.json").read_text(encoding="utf-8"))["project"]["layer_counts"],
            )
        )
    assert outputs[0] == outputs[1]


#: The generator's requirement that generating the `large` preset takes seconds, not minutes.
#: Minutes start at 120 s; the slower Windows runner meets 60 s.
LARGE_PRESET_SPEC_SECONDS = 60.0


@pytest.mark.slow
def test_large_generation_performance(tmp_path: Path) -> None:
    # The small and medium presets first; the large one is timed.
    for name in ("small", "medium"):
        generate_project(load_preset(preset_path(name)), tmp_path / name)
    started = time.perf_counter()
    generate_project(load_preset(preset_path("large")), tmp_path / "large")
    assert time.perf_counter() - started < LARGE_PRESET_SPEC_SECONDS


@pytest.mark.integration
@pytest.mark.parametrize("preset_path", PRESETS, ids=lambda p: p.stem)
def test_production_swcraft_gate(preset_path: Path, tmp_path: Path) -> None:
    """Each production preset, generated by the CLI under test, checks clean, validates and builds.

    It builds every SWCraft output; its passport is the next integration test's.
    """
    if not SWCRAFT:
        pytest.skip("SWCRAFT_BIN is not set")
    out = tmp_path / preset_path.stem
    _generate_with_the_cli(preset_path, out)
    project = out / "swcraft.yaml"
    commands = [
        ("check-model", "--format", "json"),
        ("check-model", "--warnings-as-errors"),
        ("validate",),
        ("resource-report",),
        ("generate", "arxml"),
        ("generate", "diagrams"),
        ("generate", "needs"),
    ]
    assert_swcraft_clean(SWCRAFT, project, commands, cwd=ROOT)
    assert (out / "dist/model.arxml").is_file()
    assert (out / "views/report.txt").is_file()
    assert (out / "dist/needs.json").is_file()


@pytest.mark.parametrize("preset_path", PRESETS, ids=lambda p: p.stem)
def test_the_sidecar_records_runnable_budgets_switched_on(preset_path: Path) -> None:
    *_, sidecar = build(preset_path)
    assert json.loads(sidecar)["preset"]["parameters"]["emit_runnable_wcet"] is True


@functools.cache
def _generator_python() -> str:
    """The interpreter of the environment the generator runs in.

    This one, or the one beside `SWCRAFT_SPECIMENS_BIN` in its virtual environment's `bin/`
    (`Scripts\\` on Windows).
    """
    if not SWCRAFT_SPECIMENS_BIN:
        return PYTHON
    folder = Path(SWCRAFT_SPECIMENS_BIN).parent
    names = ("python.exe",) if os.name == "nt" else ("python", "python3")
    for name in names:
        if (folder / name).is_file():
            return str(folder / name)
    pytest.fail(
        f"SWCRAFT_SPECIMENS_BIN={SWCRAFT_SPECIMENS_BIN}: no {' or '.join(names)} beside it, so the "
        "environment whose swcraft-tools decides toolchain_version cannot be asked"
    )


@functools.cache
def _environment_swcraft_tools() -> str | None:
    """The `swcraft-tools` version installed in the generator's environment, or `None`."""
    probe = (
        "from importlib import metadata\n"
        "try:\n"
        "    print(metadata.version('swcraft-tools'))\n"
        "except metadata.PackageNotFoundError:\n"
        "    pass\n"
    )
    completed = run_checked([_generator_python(), "-I", "-c", probe], None, what="swcraft-tools lookup")
    return completed.stdout.strip() or None


@functools.cache
def _swcraft_version() -> str:
    """The release `swcraft --version` reports (`swcraft X.Y.Z`)."""
    assert SWCRAFT
    completed = run_checked([SWCRAFT, "--version"], None, what="swcraft --version")
    words = completed.stdout.split()
    assert len(words) == 2 and words[0] == "swcraft", completed.stdout
    return words[1]


def _generate_with_the_cli(preset_path: Path, out: Path) -> None:
    """Generate the preset by its packaged name with the CLI under test.

    The manifest's `toolchain_version` equals the release `swcraft --version` reports when the
    generator's environment holds `swcraft-tools`, else it is absent.
    """
    run_checked(
        [*generator_command(), "generate", preset_path.stem, "--out", str(out)],
        ROOT,
        what=f"generate {preset_path.stem}",
    )
    manifest = yaml.safe_load((out / "swcraft.yaml").read_text(encoding="utf-8"))
    if _environment_swcraft_tools() is None:
        assert "toolchain_version" not in manifest, manifest
    else:
        assert manifest.get("toolchain_version") == _swcraft_version(), (
            f"toolchain_version {manifest.get('toolchain_version')!r}, swcraft --version {_swcraft_version()!r}"
        )


@pytest.mark.integration
@pytest.mark.parametrize("preset_path", SPECIMEN_PRESETS, ids=lambda p: p.stem)
def test_specimen_swcraft_gate(preset_path: Path, tmp_path: Path) -> None:
    """A specimen checks clean: no finding at all, none even as a warning, and `validate`."""
    if not SWCRAFT:
        pytest.skip("SWCRAFT_BIN is not set")
    out = tmp_path / preset_path.stem
    _generate_with_the_cli(preset_path, out)
    commands = [
        ("check-model", "--format", "json"),
        ("check-model", "--warnings-as-errors"),
        ("validate",),
    ]
    assert_swcraft_clean(SWCRAFT, out / "swcraft.yaml", commands, cwd=ROOT)


@pytest.mark.integration
@pytest.mark.parametrize("preset_path", PRESETS + SPECIMEN_PRESETS, ids=lambda p: p.stem)
def test_the_live_swcraft_passport_equals_the_sidecar(preset_path: Path, tmp_path: Path) -> None:
    """SWCraft's `swcraft passport` on the generated tree equals the sidecar's `passport`.

    As canonical text (key order and int/float both count), `contract_version` by major.
    """
    if not SWCRAFT_PASSPORT:
        pytest.skip("SWCRAFT_BIN is not set: the live `swcraft passport` of the pinned release is not run")
    out = tmp_path / preset_path.stem
    _generate_with_the_cli(preset_path, out)
    completed = subprocess.run(
        [SWCRAFT_PASSPORT, "passport", "--project", str(out / "swcraft.yaml")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    live = json.loads(completed.stdout)
    sidecar = json.loads((out / "generation.json").read_text(encoding="utf-8"))
    assert_live_passport_equals_sidecar(live, sidecar["passport"])
