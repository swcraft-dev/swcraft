"""The `toolchain_version` rule.

A generated project names the SWCraft release found at run time: `--toolchain-version`, else the
installed `swcraft-tools` distribution, else none. Every consumer follows: the manifest writer, the
sidecar writers and readers, the rendered-tree gate, the contract gate, and the scenario runner,
which pins no release and reads the generator's version from the installed distribution.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest
import yaml
from swcraft_specimens import __version__, toolchain
from swcraft_specimens.cli import run
from swcraft_specimens.contract import CONTRACT_PREFIX, validate_contract
from swcraft_specimens.errors import GenerationError
from swcraft_specimens.passport import CONTRACT_MAJOR, read_passport
from swcraft_specimens.sidecar import validate_specimen_sidecar
from swcraft_specimens.specimen.build import build_specimen_files, render_specimen
from swcraft_specimens.specimen.preset import DEFAULT_NAME, SpecimenPreset
from swcraft_specimens.validate import validate_file_map
from tests.oracle.swcraft_gate import SUBPROCESS_TIMEOUT

ROOT = Path(__file__).parents[1]
TINY = ROOT / "tests/fixtures/tiny.yaml"
PASSPORT = ROOT / "tests/fixtures/passport/examples/minimal.json"
INSTALLED = "0.22.0"
EXPLICIT = "0.30.1"


@pytest.fixture
def installed_swcraft(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case 2: an installed `swcraft-tools` distribution, faked through `importlib.metadata`."""
    real = metadata.version

    def version(name: str) -> str:
        return INSTALLED if name == toolchain.SWCRAFT_DISTRIBUTION else real(name)

    monkeypatch.setattr(metadata, "version", version)


def _specimen() -> SpecimenPreset:
    return SpecimenPreset(DEFAULT_NAME, read_passport(PASSPORT.read_text(encoding="utf-8")))


def _arguments(command: str, out: Path) -> list[str]:
    source = TINY if command == "generate" else PASSPORT
    return [command, str(source), "--out", str(out)]


def _named(out: Path) -> tuple[Any, Any]:
    """The manifest's `toolchain_version` (`...` if absent) and the sidecar's `swcraft_version`."""
    manifest = yaml.safe_load((out / "swcraft.yaml").read_text(encoding="utf-8"))
    sidecar = json.loads((out / "generation.json").read_text(encoding="utf-8"))
    return manifest.get("toolchain_version", ...), sidecar["toolchain"]["swcraft_version"]


# --- the three cases, for `generate` and for `specimen` -----------------------------------------


@pytest.mark.parametrize("command", ["generate", "specimen"])
def test_case_3_no_swcraft_found_writes_no_key_and_a_null_sidecar(command: str, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert run(_arguments(command, out)) == 0
    assert _named(out) == (..., None)
    assert b"toolchain_version" not in (out / "swcraft.yaml").read_bytes()


@pytest.mark.parametrize("command", ["generate", "specimen"])
def test_case_2_the_installed_swcraft_tools_is_named(command: str, tmp_path: Path, installed_swcraft: None) -> None:
    out = tmp_path / "out"
    assert run(_arguments(command, out)) == 0
    assert _named(out) == (INSTALLED, INSTALLED)
    assert f'toolchain_version: "{INSTALLED}"\n'.encode() in (out / "swcraft.yaml").read_bytes()


@pytest.mark.parametrize("command", ["generate", "specimen"])
@pytest.mark.parametrize("installed", [False, True])
def test_case_1_the_option_wins_over_the_lookup(
    command: str, installed: bool, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    if installed:
        request.getfixturevalue("installed_swcraft")
    out = tmp_path / "out"
    assert run([*_arguments(command, out), "--toolchain-version", EXPLICIT]) == 0
    assert _named(out) == (EXPLICIT, EXPLICIT)


@pytest.mark.parametrize("command", ["generate", "specimen"])
@pytest.mark.parametrize("value", [">=0.7", "latest", "0.7.*", " 0.7.0"])
def test_an_option_that_is_no_exact_release_is_refused_before_writing(
    command: str, value: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    assert run([*_arguments(command, out), "--toolchain-version", value]) == 2
    assert "is not an exact release" in capsys.readouterr().err
    assert not out.exists()


def test_the_explicit_value_lasts_for_the_run_alone() -> None:
    with toolchain.explicit_toolchain_version(EXPLICIT):
        assert toolchain.toolchain_version() == EXPLICIT
    assert toolchain.toolchain_version() is None


def test_an_installed_version_that_is_no_exact_release_names_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata, "version", lambda name: "0.22.0-weird spelling")
    assert toolchain.installed_swcraft_version() is None


# --- the rendered-tree gate, the contract gate and the sidecar readers ---------------------------


def _with_manifest_value(file_map: dict[str, bytes], value: str) -> dict[str, bytes]:
    manifest = file_map["swcraft.yaml"]
    anchor = b'schema_version: "1.0"\n'
    assert anchor in manifest and b"toolchain_version" not in manifest
    changed = dict(file_map)
    changed["swcraft.yaml"] = manifest.replace(anchor, anchor + f'toolchain_version: "{value}"\n'.encode())
    return changed


@pytest.mark.parametrize("value", [None, INSTALLED, EXPLICIT, "1!2.0rc1.post3.dev4+local.7"])
def test_the_gates_accept_an_absent_key_and_any_exact_release(value: str | None) -> None:
    rendered = render_specimen(_specimen())
    file_map = rendered.file_map
    if value is not None:
        file_map = _with_manifest_value(file_map, value)
    validate_contract(file_map)
    validate_file_map(file_map, rendered.project, task_overhead_us=0)


@pytest.mark.parametrize("value", [">=0.7", "latest", "0.7.*"])
def test_the_tree_gate_refuses_a_present_value_that_is_no_exact_release(value: str) -> None:
    rendered = render_specimen(_specimen())
    with pytest.raises(GenerationError, match="manifest version mismatch"):
        validate_file_map(_with_manifest_value(rendered.file_map, value), rendered.project, task_overhead_us=0)


def test_the_contract_requires_no_toolchain_version_and_names_the_major_alone() -> None:
    validate_contract(render_specimen(_specimen()).file_map)
    assert CONTRACT_PREFIX == f"SWCraft model contract major {CONTRACT_MAJOR} contract"
    assert CONTRACT_MAJOR == 12


def _sidecar_check(files: dict[str, bytes], sidecar_version: object) -> None:
    preset = _specimen()
    rendered = render_specimen(preset)
    document = json.loads(files["generation.json"])
    document["toolchain"]["swcraft_version"] = sidecar_version
    tree = {path: data for path, data in files.items() if path != "generation.json"}
    validate_specimen_sidecar(
        json.dumps(document).encode(),
        preset,
        rendered.project,
        tree,
        documents=rendered.documents,
    )


def test_the_sidecar_reader_holds_the_sidecar_to_the_manifest(installed_swcraft: None) -> None:
    with_key = build_specimen_files(_specimen())
    assert json.loads(with_key["generation.json"])["toolchain"] == {"swcraft_version": INSTALLED}
    _sidecar_check(with_key, INSTALLED)
    for wrong in (None, EXPLICIT):
        with pytest.raises(GenerationError, match="swcraft_version mismatch"):
            _sidecar_check(with_key, wrong)


def test_the_sidecar_reader_holds_a_null_to_a_manifest_without_the_key() -> None:
    without = build_specimen_files(_specimen())
    assert json.loads(without["generation.json"])["toolchain"] == {"swcraft_version": None}
    _sidecar_check(without, None)
    with pytest.raises(GenerationError, match="swcraft_version mismatch"):
        _sidecar_check(without, INSTALLED)


# --- `--version` ---------------------------------------------------------------------------------


def test_the_cli_prints_its_own_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "swcraft_specimens", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"swcraft-specimens {__version__}\n"


# --- the scenario runner -------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in swcraft is a shebang script, not started on Windows")
def test_the_scenario_runner_pins_no_swcraft_and_reads_the_installed_generator(
    tmp_path: Path,
) -> None:
    """Run from a copy with no `src/` directory, as from the sdist.

    It imports, accepts any SWCraft, and takes its version from the installed distribution.
    """
    copy = tmp_path / "copy"
    shutil.copytree(ROOT / "tests/scenarios", copy / "tests/scenarios")
    assert not (copy / "src").exists()
    fake = tmp_path / "swcraft"
    fake.write_text(f"#!{sys.executable}\nprint('swcraft 9.9.9')\n", encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    child = (
        "import json, runpy, sys\n"
        "from pathlib import Path\n"
        "module = runpy.run_path(sys.argv[1])\n"
        "assert module['ROOT'] == Path(sys.argv[2]), module['ROOT']\n"
        "results = module['run_suite']([], sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5]), [])\n"
        "print(json.dumps(results))\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            child,
            str(copy / "tests/scenarios/run_scenarios.py"),
            str(copy / "tests/scenarios"),
            str(fake),
            str(tmp_path / "build/scenarios"),
            str(tmp_path / "build/results.json"),
        ],
        cwd=copy,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, result.stderr
    results = json.loads(result.stdout)
    assert results["swcraft_version"] == "swcraft 9.9.9"
    assert results["swcraft_specimens_version"] == metadata.version("swcraft-specimens")
