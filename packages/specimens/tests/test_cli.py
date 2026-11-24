from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from swcraft_specimens.cli import run
from swcraft_specimens.preset import Preset
from swcraft_specimens.specimen.preset import SpecimenPreset
from tests.oracle.swcraft_gate import SUBPROCESS_TIMEOUT


def invoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "swcraft_specimens", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
    )


def write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


FULL_PRESET = """\
name: test_model
seed: 1
components: 2
domains: 1
cores:
  - {name: CORE_A, asil_capability: D}
core_threshold: 0.7
memories:
  shared: {size: 1024, threshold: 0.8}
  private_per_core: {size: 512, threshold: 0.8}
periods_ms: {1: 85}
runnables_per_component: {min: 1, max: 2}
wcet_us_by_period:
  1: {min: 1, average: 2, max: 3}
signal_size_bytes: {1: 1}
intra_domain_link_density: 0.3
inter_domain_link_density: 0.1
client_server_share: 0.1
asil_weights: {QM: 1}
displaced_components: 0
private_memory_share: 0.1
target_core_load: 0.5
task_overhead_us: 5
emit_runnable_wcet: false
"""


def test_module_help() -> None:
    result = invoke("--help")
    assert result.returncode == 0
    assert "usage:" in result.stdout
    for command in ("generate", "preset-from-passport", "specimen"):
        assert command in result.stdout


@pytest.mark.parametrize("command", ["preset-from-passport", "specimen"])
def test_passport_command_help(command: str) -> None:
    result = invoke(command, "--help")
    assert result.returncode == 0
    assert "--out" in result.stdout
    assert "--name" in result.stdout


@pytest.mark.parametrize("command", ["preset-from-passport", "specimen"])
def test_passport_commands_require_out(command: str) -> None:
    result = invoke(command, "passport.json")
    assert result.returncode == 2
    assert "traceback" not in result.stderr.lower()


def test_generate_help() -> None:
    result = invoke("generate", "--help")
    assert result.returncode == 0
    assert "PRESET" not in result.stdout  # argparse displays the destination name in lowercase.
    assert "--out" in result.stdout


@pytest.mark.parametrize("args", [(), ("generate",), ("generate", "preset.yaml")])
def test_missing_arguments_are_cli_errors(args: tuple[str, ...]) -> None:
    result = invoke(*args)
    assert result.returncode == 2
    assert "traceback" not in result.stderr.lower()


def test_missing_preset(tmp_path: Path) -> None:
    result = invoke("generate", str(tmp_path / "missing.yaml"), "--out", str(tmp_path / "out"))
    assert result.returncode == 2
    assert "preset not found" in result.stderr
    assert not (tmp_path / "out").exists()


def test_invalid_yaml(tmp_path: Path) -> None:
    preset = write(tmp_path / "bad.yaml", "root: [unterminated\n")
    result = invoke("generate", str(preset), "--out", str(tmp_path / "out"))
    assert result.returncode == 2
    assert "invalid YAML" in result.stderr
    assert not (tmp_path / "out").exists()


def test_non_mapping_yaml(tmp_path: Path) -> None:
    preset = write(tmp_path / "list.yaml", "- one\n- two\n")
    result = invoke("generate", str(preset), "--out", str(tmp_path / "out"))
    assert result.returncode == 2
    assert "root must be a mapping" in result.stderr
    assert not (tmp_path / "out").exists()


def test_valid_general_preset_generates(tmp_path: Path) -> None:
    preset = write(tmp_path / "minimal.yaml", FULL_PRESET)
    output = tmp_path / "out"
    result = invoke("generate", str(preset), "--out", str(output))
    assert result.returncode == 0, result.stderr
    assert (output / "swcraft.yaml").exists()


@pytest.mark.parametrize("kind", ["file", "nonempty"])
def test_unsafe_output_is_rejected_without_changes(tmp_path: Path, kind: str) -> None:
    preset = write(tmp_path / "preset.yaml", FULL_PRESET)
    output = tmp_path / "out"
    if kind == "file":
        output.write_text("keep", encoding="utf-8")
    else:
        output.mkdir()
        write(output / "keep.txt", "keep")
    before = sorted((path.relative_to(tmp_path) for path in tmp_path.rglob("*")), key=lambda path: path.as_posix())
    result = invoke("generate", str(preset), "--out", str(output))
    after = sorted((path.relative_to(tmp_path) for path in tmp_path.rglob("*")), key=lambda path: path.as_posix())
    assert result.returncode == 2
    assert "output" in result.stderr
    assert before == after


def test_empty_existing_output_is_allowed_by_validation(tmp_path: Path) -> None:
    preset = write(tmp_path / "preset.yaml", FULL_PRESET)
    output = tmp_path / "out"
    output.mkdir()
    result = invoke("generate", str(preset), "--out", str(output))
    assert result.returncode == 0, result.stderr
    assert output.is_dir()
    assert (output / "swcraft.yaml").exists()


def test_absent_output_is_allowed_by_validation(tmp_path: Path) -> None:
    preset = write(tmp_path / "preset.yaml", FULL_PRESET)
    output = tmp_path / "out"
    result = invoke("generate", str(preset), "--out", str(output))
    assert result.returncode == 0, result.stderr
    assert (output / "swcraft.yaml").exists()


def test_success_exit_code_with_test_generator(tmp_path: Path) -> None:
    preset = write(tmp_path / "preset.yaml", FULL_PRESET)
    output = tmp_path / "out"

    def substitute(loaded: Preset, target: Path) -> None:
        assert isinstance(loaded, Preset)
        assert target == output

    assert run(["generate", str(preset), "--out", str(output)], generator=substitute) == 0


def test_generation_error_has_exit_code_one(tmp_path: Path) -> None:
    preset = write(tmp_path / "preset.yaml", FULL_PRESET)

    def failing(_loaded: Preset, _target: Path) -> None:
        from swcraft_specimens.errors import GenerationError

        raise GenerationError("invariant failed: component X")

    assert run(["generate", str(preset), "--out", str(tmp_path / "out")], generator=failing) == 1


def test_generate_dispatches_on_kind(tmp_path: Path) -> None:
    waters = write(tmp_path / "waters.yaml", FULL_PRESET)
    specimen_text = Path(__file__).parents[1] / "tests/fixtures/passport/examples/minimal.json"
    from swcraft_specimens.passport import read_passport
    from swcraft_specimens.specimen.preset import preset_text

    passport = read_passport(specimen_text.read_text(encoding="utf-8"))
    specimen = write(tmp_path / "specimen.yaml", preset_text(passport))
    calls: list[str] = []

    def waters_generator(loaded: Preset, _target: Path) -> None:
        assert isinstance(loaded, Preset)
        calls.append("waters")

    def specimen_generator(loaded: SpecimenPreset, _target: Path) -> None:
        assert loaded.passport == passport
        calls.append("specimen")

    for preset in (waters, specimen):
        argv = ["generate", str(preset), "--out", str(tmp_path / preset.stem)]
        assert run(argv, generator=waters_generator, specimen_generator=specimen_generator) == 0
    assert calls == ["waters", "specimen"]


SHOWCASE_TWIN = Path(__file__).parent / "fixtures/specimen-acceptance/showcase-twin.json"
SHOWCASE_TWIN_REFUSAL = "refused: interfaces.fan_out.top (fan-out): "


def assert_refused(result: subprocess.CompletedProcess[str], output: Path) -> None:
    assert result.returncode == 2
    lines = result.stderr.splitlines()
    assert lines and all(line.startswith("refused: ") for line in lines)
    assert SHOWCASE_TWIN_REFUSAL in result.stderr
    assert "traceback" not in result.stderr.lower()
    assert not output.exists()


def test_a_refused_passport_exits_two_through_the_module(tmp_path: Path) -> None:
    result = invoke("specimen", str(SHOWCASE_TWIN), "--out", str(tmp_path / "out"))
    assert_refused(result, tmp_path / "out")


# --- the console script ----------------------------------------------------------------------


def console_script() -> Path:
    """The installed `swcraft-specimens` executable next to the interpreter."""
    folder = Path(sys.executable).parent
    return folder / ("swcraft-specimens.exe" if sys.platform == "win32" else "swcraft-specimens")


def test_the_parser_prog_is_swcraft_specimens() -> None:
    from swcraft_specimens.cli import build_parser

    assert build_parser().prog == "swcraft-specimens"


def test_the_installed_distribution_declares_the_console_script() -> None:
    from importlib.metadata import entry_points

    scripts = entry_points(group="console_scripts")
    assert [entry.value for entry in scripts if entry.name == "swcraft-specimens"] == ["swcraft_specimens.cli:main"]


def test_the_console_script_is_installed_next_to_the_interpreter() -> None:
    script = console_script()
    assert script.is_file(), f"{script} is not installed"
    assert not script.with_suffix(".cmd").exists()
    assert not script.with_suffix(".bat").exists()


def test_the_console_script_refuses_a_passport_with_exit_two(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(console_script()), "specimen", str(SHOWCASE_TWIN), "--out", str(tmp_path / "out")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert_refused(result, tmp_path / "out")


HARNESS_TWO_ROWS = Path(__file__).parent / "fixtures/specimen-acceptance/harness-two-rows.json"


def test_the_console_script_builds_an_acceptance_passport_with_exit_zero(tmp_path: Path) -> None:
    output = tmp_path / "out"
    result = subprocess.run(
        [str(console_script()), "specimen", str(HARNESS_TWO_ROWS), "--out", str(output)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, result.stderr
    assert "refused: " not in result.stderr
    assert (output / "generation.json").is_file()
    assert (output / "swcraft.yaml").is_file()


def test_the_console_script_prints_the_same_usage_as_the_module() -> None:
    script = subprocess.run(
        [str(console_script()), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
    )
    module = invoke("--help")
    assert script.returncode == module.returncode == 0
    assert script.stdout == module.stdout
    assert script.stdout.startswith("usage: swcraft-specimens ")


def test_the_specimen_command_prints_the_exact_program_notice_once_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from swcraft_specimens.passport import compute_passport, validate_passport
    from swcraft_specimens.specimen import layout as layout_module
    from swcraft_specimens.specimen import shape as shape_module
    from tests.oracle.specimen_helpers import shaped_witness

    def not_built(*_: object, **__: object) -> None:
        return None

    # Neither step-2 builder builds seed 501's layout, so the planner reaches step 3.
    monkeypatch.setattr(layout_module, "solve_open", not_built)
    monkeypatch.setattr(layout_module, "first_fit", not_built)
    passport = validate_passport(compute_passport(shaped_witness(501, 3)))
    source = write(tmp_path / "passport.json", json.dumps(passport))
    shape_module._shape_cached.cache_clear()
    try:
        argv = ["specimen", str(source), "--out", str(tmp_path / "out"), "--name", "Sweep"]
        assert run(argv) == 0
    finally:
        shape_module._shape_cached.cache_clear()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "swcraft-specimens: specimen Sweep: the layout needs the specimen generator's exact program, which may take "
        "long at this size; stating components.ports.total and interfaces.fan_out.top shortens "
        "it\n"
    )
    assert (tmp_path / "out" / "generation.json").is_file()


def test_generating_a_specimen_preset_prints_the_exact_program_notice_once_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from swcraft_specimens.passport import compute_passport, validate_passport
    from swcraft_specimens.specimen import layout as layout_module
    from swcraft_specimens.specimen import shape as shape_module
    from swcraft_specimens.specimen.preset import preset_text
    from tests.oracle.specimen_helpers import shaped_witness

    def not_built(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(layout_module, "solve_open", not_built)
    monkeypatch.setattr(layout_module, "first_fit", not_built)
    passport = validate_passport(compute_passport(shaped_witness(501, 3)))
    preset = write(tmp_path / "sweep.yaml", preset_text(passport, "Sweep"))
    shape_module._shape_cached.cache_clear()
    try:
        assert run(["generate", str(preset), "--out", str(tmp_path / "out")]) == 0
    finally:
        shape_module._shape_cached.cache_clear()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "swcraft-specimens: specimen Sweep: the layout needs the specimen generator's exact program, which may take "
        "long at this size; stating components.ports.total and interfaces.fan_out.top shortens "
        "it\n"
    )
    assert layout_module.NOTICE.get() is None
