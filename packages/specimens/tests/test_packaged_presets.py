"""The presets as package data, taken by name.

`generate NAME` resolves a packaged preset; a path still works; an unknown name is refused, listing
the names; a built wheel carries all nine presets; the preset values match the pinned digests.
"""

import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml
from swcraft_specimens.cli import build_parser, run
from swcraft_specimens.packaged import packaged_presets, preset_names, preset_path, resolve_preset
from swcraft_specimens.preset import Preset, _Loader
from swcraft_specimens.specimen.preset import SpecimenPreset, load_yaml_text

ROOT = Path(__file__).parents[1]

#: The SHA-256 of every preset's value as the loader that reads it parses it (JSON in the file's key
#: order, which the generator consumes, with each mapping key's type; a WATERS preset's decimals in
#: their own spelling), so a comment edit moves no digest; a change to a preset's data or its order
#: re-pins it.
PRESET_SHA256 = {
    "large": "e19e2c505d47592eff670913367182bae0f2f3ebe4c56b82fc00c38e503155ed",
    "medium": "6ec28a3cf29422cd00dc82191dd8b9ac0625aee61f047b0482583f8ceed56389",
    "small": "cd983b5af0249ab1d6c74b9066c3ccf10cfb19374775504d0c85e59edcc54f56",
    "chain-empty": "f0867926a5035b76a5c273bdfa9200eae23ae1a3962a277381dd34b43875128a",
    "large-scale-shapes": "1d2345de71584bc42d30bce642613a80a3fe6f8dc8c457d0190d386dc04b7604",
    "large-scale-topology": "1e643cb089c0bfbdf86b361d7bfb25fbe9c43bec2eed9c08e39dfbd2306539bf",
    "minimal": "dacacf14b9ded59f2cc79ddcfb3b2840bb87662977f8a27c5741a78d111caa22",
    "shapes": "695645551b98c54b545a72702a36f662835e99a7b02329fcfafc9ded73678cb6",
    "topology": "74a9301d855ab6128247891a38dcf10bbba7836d3a26989e940b6892e425a0f7",
}
SPECIMENS = ("chain-empty", "large-scale-shapes", "large-scale-topology", "minimal", "shapes")


class Captured:
    def __init__(self) -> None:
        self.presets: list[Preset | SpecimenPreset] = []

    def __call__(self, preset: Preset | SpecimenPreset, output: Path) -> None:
        self.presets.append(preset)


def _run_generate(argument: str, out: Path) -> tuple[int, Captured]:
    captured = Captured()
    code = run(
        ["generate", argument, "--out", str(out)],
        generator=captured,
        specimen_generator=captured,
    )
    return code, captured


def _typed(value: object) -> object:
    """A mapping as its ordered `[key type, key, value]` rows, so a key `1` and a key `"1"` differ."""
    if isinstance(value, dict):
        return {"mapping": [[type(key).__name__, _typed(key), _typed(item)] for key, item in value.items()]}
    if isinstance(value, list):
        return [_typed(item) for item in value]
    return value


def _value_sha256(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    value = load_yaml_text(text)
    if not isinstance(value, dict) or "kind" not in value:
        # Mirrors `preset.py::load_any_preset`'s dispatch; WATERS decimals keep their spelling (0.7 != 0.70).
        value = yaml.load(text, Loader=_Loader)
    return hashlib.sha256(json.dumps(_typed(value), default=repr).encode("utf-8")).hexdigest()


def test_the_package_ships_the_nine_presets_by_name() -> None:
    presets = packaged_presets()
    assert list(presets) == ["large", "medium", "small", *SPECIMENS, "topology"]
    assert preset_names() == tuple(presets)
    for name, path in presets.items():
        assert path.parent.name == ("presets" if name in {"small", "medium", "large"} else "specimens")
    assert not (ROOT / "presets").exists()


def test_the_move_changed_no_preset_byte() -> None:
    digests = {name: _value_sha256(path) for name, path in packaged_presets().items()}
    assert digests == PRESET_SHA256


def test_generate_small_resolves_the_packaged_preset(tmp_path: Path) -> None:
    code, captured = _run_generate("small", tmp_path / "out")
    assert code == 0
    (preset,) = captured.presets
    assert isinstance(preset, Preset) and preset.name == "small"


def test_generate_takes_a_specimen_preset_by_name(tmp_path: Path) -> None:
    code, captured = _run_generate("minimal", tmp_path / "out")
    assert code == 0
    (preset,) = captured.presets
    assert isinstance(preset, SpecimenPreset)


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
def test_a_path_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: bool) -> None:
    copy = tmp_path / "mine.yaml"
    shutil.copyfile(preset_path("small"), copy)
    monkeypatch.chdir(tmp_path)
    code, captured = _run_generate("mine.yaml" if relative else str(copy), tmp_path / "out")
    assert code == 0
    (preset,) = captured.presets
    assert isinstance(preset, Preset) and preset.name == "small"


def test_a_file_named_like_a_preset_is_reached_by_its_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shutil.copyfile(preset_path("minimal"), tmp_path / "small")
    monkeypatch.chdir(tmp_path)
    assert resolve_preset("small") == preset_path("small")
    code, captured = _run_generate("./small", tmp_path / "out")
    assert code == 0
    (preset,) = captured.presets
    assert isinstance(preset, SpecimenPreset)


def test_an_unknown_name_is_refused_with_the_names(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, captured = _run_generate("tiny", tmp_path / "out")
    assert code == 2
    assert captured.presets == []
    error = capsys.readouterr().err
    assert error.startswith("error: unknown preset 'tiny'; the presets are: ")
    assert error.rstrip().endswith(", ".join(PRESET_SHA256))
    assert not (tmp_path / "out").exists()


def test_a_missing_path_keeps_its_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _ = _run_generate(str(tmp_path / "absent.yaml"), tmp_path / "out")
    assert code == 2
    assert "error: preset not found: " in capsys.readouterr().err


def test_help_lists_the_presets() -> None:
    text = build_parser().format_help()
    assert "production: large, medium, small" in text
    assert f"specimens: {', '.join((*SPECIMENS, 'topology'))}" in text


def test_a_built_wheel_contains_every_preset(tmp_path: Path) -> None:
    """A wheel built from a copy of the sources carries all nine presets, the six specimen presets included."""
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copytree(
        ROOT / "src" / "swcraft_specimens",
        source / "src" / "swcraft_specimens",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    out = tmp_path / "wheel"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            "--quiet",
            "--wheel-dir",
            str(out),
            str(source),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    (wheel,) = out.glob("swcraft_specimens-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
    expected = {
        f"swcraft_specimens/presets/{'specimens/' if name in (*SPECIMENS, 'topology') else ''}{name}.yaml"
        for name in PRESET_SHA256
    }
    assert len(expected) == 9
    assert expected <= members
    assert {m for m in members if m.startswith("swcraft_specimens/presets/specimens/")} == {
        f"swcraft_specimens/presets/specimens/{name}.yaml" for name in (*SPECIMENS, "topology")
    }
