"""Contract <-> wheel drift guard.

The four normative contract files under
``packages/tools/src/swcraft_tools/model_contract/`` are package data because the
installed loader reads these exact bytes out of the wheel. Hatchling ships whatever
sits under ``packages = ["src/swcraft_tools"]``, so a dropped file, a later
``exclude`` or a stray file would ship an unreviewed contract while every suite that
reads the *source* tree stays green.

This module compares the packaged tree against the source tree **byte-for-byte and
in both directions**, against a wheel built in this session rather than whatever sits
in ``dist/python/`` (which may predate the contract). The rule is a pure function over
``{relative path: bytes}`` mappings, so the guard is also shown to fire. The
wheel-building tests carry ``@pytest.mark.artifact`` and run in ``dev.py test-artifacts``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from model_contract_fixtures import CONTRACT_PACKAGE_ROOT, NORMATIVE_CONTRACT_FILES

#: Where the contract lands inside the wheel: package data shipped by hatchling's
#: ``packages`` entry, with no ``force-include`` and no build-time copy step.
_WHEEL_CONTRACT_PREFIX = "swcraft_tools/model_contract/"

#: The project whose wheel carries the contract.
_TOOLS_PROJECT = "packages/tools"


# ---------------------------------------------------------------------------
# Reading the two sides
# ---------------------------------------------------------------------------


def read_tree(root: Path) -> dict[str, bytes]:
    """``{relative posix path: bytes}`` for every file under *root*.

    ``__pycache__`` is skipped: a checkout can acquire one and a wheel never
    carries one, so it would make the guard fire on a local artifact.
    """
    contents: dict[str, bytes] = {}
    for path in sorted(root.rglob("*"), key=lambda path: path.as_posix()):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        contents[path.relative_to(root).as_posix()] = path.read_bytes()
    return contents


def read_wheel_contract(wheel: Path) -> dict[str, bytes]:
    """``{relative posix path: bytes}`` for the contract inside a built wheel."""
    contents: dict[str, bytes] = {}
    with zipfile.ZipFile(wheel) as archive:
        for name in sorted(archive.namelist()):
            if not name.startswith(_WHEEL_CONTRACT_PREFIX) or name.endswith("/"):
                continue
            contents[name[len(_WHEEL_CONTRACT_PREFIX) :]] = archive.read(name)
    return contents


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def drift(source: dict[str, bytes], packaged: dict[str, bytes]) -> list[str]:
    """Every difference between the source contract and the packaged one.

    Reported in both directions and per file: a dropped file and an edited file
    are different defects with different fixes.
    """
    findings = []
    for relative in sorted(set(source) - set(packaged)):
        findings.append(f"missing from the wheel: {relative}")
    for relative in sorted(set(packaged) - set(source)):
        findings.append(f"present in the wheel only: {relative}")
    for relative in sorted(set(source) & set(packaged)):
        if source[relative] != packaged[relative]:
            findings.append(f"differs: {relative}")
    return findings


# ---------------------------------------------------------------------------
# The rule fires (fast — no wheel is built)
# ---------------------------------------------------------------------------


def test_identical_trees_report_no_drift() -> None:
    source = read_tree(CONTRACT_PACKAGE_ROOT)
    assert source, "the source contract directory must not be empty"
    assert drift(source, dict(source)) == []


def test_drift_reports_a_file_missing_from_the_wheel() -> None:
    source = read_tree(CONTRACT_PACKAGE_ROOT)
    packaged = dict(source)
    del packaged["binding.json"]

    assert drift(source, packaged) == ["missing from the wheel: binding.json"]


def test_drift_reports_a_file_the_wheel_carries_alone() -> None:
    source = read_tree(CONTRACT_PACKAGE_ROOT)
    packaged = dict(source) | {"scratch.json": b"{}\n"}

    assert drift(source, packaged) == ["present in the wheel only: scratch.json"]


def test_drift_reports_edited_content() -> None:
    source = read_tree(CONTRACT_PACKAGE_ROOT)
    packaged = dict(source)
    packaged["model-contract.json"] = source["model-contract.json"] + b"\n"

    assert drift(source, packaged) == ["differs: model-contract.json"]


def test_the_guard_fires_on_a_mutated_extraction(tmp_path: Path) -> None:
    """Three drifts on a directory standing in for the wheel's extraction are each named."""
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    for relative, payload in read_tree(CONTRACT_PACKAGE_ROOT).items():
        target = extracted / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    (extracted / "model-contract.json").write_bytes(b"{}\n")
    (extracted / "binding.json").unlink()
    (extracted / "notes.txt").write_bytes(b"scratch\n")

    findings = drift(read_tree(CONTRACT_PACKAGE_ROOT), read_tree(extracted))

    assert findings == [
        "missing from the wheel: binding.json",
        "present in the wheel only: notes.txt",
        "differs: model-contract.json",
    ]


# ---------------------------------------------------------------------------
# The packaging decision this guard is shaped around
# ---------------------------------------------------------------------------


def test_the_contract_has_exactly_one_editable_copy() -> None:
    """The contract ships from the package directory with no ``force-include``, so no copy can go stale."""
    assert CONTRACT_PACKAGE_ROOT.parent.name == "swcraft_tools"

    pyproject = tomllib.loads((REPO_ROOT / _TOOLS_PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel_target = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel_target["packages"] == ["src/swcraft_tools"]
    assert "force-include" not in wheel_target, (
        "force-include materializes into site-packages at install time, so an editable install "
        "would keep serving the copy made by the last `pip install -e` — the contract is shipped "
        "as package data precisely so the kernel loads the reviewed bytes"
    )


def test_the_normative_files_are_all_there_and_carry_no_python() -> None:
    """Four JSON files and no module beside them: the loader does not live with the data."""
    source = read_tree(CONTRACT_PACKAGE_ROOT)

    assert set(source) == set(NORMATIVE_CONTRACT_FILES)
    assert [name for name in source if name.endswith(".py")] == []


# ---------------------------------------------------------------------------
# Against a real wheel (deselected from the fast run; `dev.py test-artifacts`)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def packaged_contract(swcraft_tools_wheel: Path) -> dict[str, bytes]:
    """The contract as it ships, read out of the session's one built wheel.

    The wheel is shared with ``test_template_wheel_sync.py`` so the two guards
    cannot pass against two different builds.
    """
    return read_wheel_contract(swcraft_tools_wheel)


@pytest.mark.artifact
def test_every_contract_file_ships_byte_identical_in_the_wheel(packaged_contract: dict[str, bytes]) -> None:
    findings = drift(read_tree(CONTRACT_PACKAGE_ROOT), packaged_contract)

    assert findings == [], (
        "the model contract inside the built wheel has drifted from the source contract — "
        "the kernel would load a contract this repository never reviewed:\n" + "\n".join(findings)
    )


@pytest.mark.artifact
def test_the_wheel_and_the_source_carry_the_same_contract_file_set(packaged_contract: dict[str, bytes]) -> None:
    """The file sets agree, and the four normative files are named so a file deleted from both trees fails too."""
    source_names = set(read_tree(CONTRACT_PACKAGE_ROOT))
    packaged_names = set(packaged_contract)

    assert packaged_names == source_names
    assert packaged_names == set(NORMATIVE_CONTRACT_FILES)


@pytest.mark.artifact
def test_the_installed_loader_reads_the_packaged_contract(swcraft_tools_wheel: Path, tmp_path: Path) -> None:
    """The loader resolves the contract out of ``site-packages`` in a venv holding only the wheel.

    An editable install would resolve to the source tree and could not tell a
    missing package-data entry from a present one. ``--no-deps --no-index``: the
    loader needs only the standard library, so this also proves it works without
    ``jsonschema`` (structural check and version guard only).
    """
    environment = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", "--no-index", "--quiet", str(swcraft_tools_wheel)],
        check=True,
    )

    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, sys\n"
        "from swcraft_tools import contract\n"
        "assert contract._import_validator_class() is None, 'the venv is not free of jsonschema'\n"
        "sys.stdout.write(\n"
        "    json.dumps(\n"
        "        {\n"
        '            "contract": contract.load_contract(),\n'
        '            "binding": contract.load_binding(),\n'
        '            "version": contract.contract_version(),\n'
        "        },\n"
        "        sort_keys=True,\n"
        "    )\n"
        ")\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [str(python), str(probe)], capture_output=True, text=True, cwd=str(tmp_path), encoding="utf-8"
    )
    assert completed.returncode == 0, completed.stderr

    loaded = json.loads(completed.stdout)
    source = read_tree(CONTRACT_PACKAGE_ROOT)
    assert loaded["contract"] == json.loads(source["model-contract.json"])
    assert loaded["binding"] == json.loads(source["binding.json"])
    assert loaded["version"] == loaded["contract"]["contractVersion"]


@pytest.mark.artifact
def test_the_wheel_carries_no_design_time_evidence(swcraft_tools_wheel: Path) -> None:
    """The wheel carries none of the review artifacts (coverage, inventories, synthetic fixtures).

    Read from the wheel's own name list: a ``force-include`` or ``artifacts``
    entry ships files from outside ``src/`` that the source layout cannot see.
    """
    with zipfile.ZipFile(swcraft_tools_wheel) as archive:
        names = archive.namelist()

    strays = sorted(
        name
        for name in names
        if "model-contract" in name or "/coverage/" in name or name.endswith("consumer-inventory.json")
    )
    assert strays == [f"{_WHEEL_CONTRACT_PREFIX}model-contract.json"], (
        f"the wheel carries design-time evidence from docs/: {strays}"
    )
    assert not any(name.startswith("docs/") for name in names), "the wheel carries files from docs/"
