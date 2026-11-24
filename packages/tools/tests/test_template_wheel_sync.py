"""Template <-> wheel drift guard.

``swcraft init --integration moon`` reads the vehicle-architecture template out
of the installed ``swcraft-tools`` package, so the template inside a freshly
built wheel must be byte-for-byte the source template, with the same file set in
both directions. The wheel is built for the session rather than read from
``dist/python/``, because a stale ``dist/`` is one of the drifts worth catching.

Dotfiles are the interesting case: the template's build contract lives almost
entirely under dot-names, and packaging tools are inconsistent about shipping
them, so the comparison is over the full recursive file set and every file's bytes.

The rule checks are pure functions over ``{relative path: bytes}`` mappings, so
the guard is also shown to fire against a mutated extraction in ``tmp_path``.
"""

from __future__ import annotations

import tomllib
import zipfile
from pathlib import Path

import pytest
from conftest import REPO_ROOT, TEMPLATE_ROOT

#: Where the template must land inside the wheel. It is package data under the
#: package directory, so hatchling's ``packages = ["src/swcraft_tools"]`` ships
#: it with no ``force-include`` and no build-time copy step.
_WHEEL_TEMPLATE_PREFIX = "swcraft_tools/templates/vehicle-architecture/"

#: The project whose wheel carries the template.
_TOOLS_PROJECT = "packages/tools"


# ---------------------------------------------------------------------------
# Reading the two sides
# ---------------------------------------------------------------------------


def read_tree(root: Path) -> dict[str, bytes]:
    """``{relative posix path: bytes}`` for every file under *root*.

    ``__pycache__`` is skipped: a checkout can hold one and a wheel never
    carries one, so it would make the guard fire on a local artifact.
    """
    contents: dict[str, bytes] = {}
    for path in sorted(root.rglob("*"), key=lambda path: path.as_posix()):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        contents[path.relative_to(root).as_posix()] = path.read_bytes()
    return contents


def read_wheel_template(wheel: Path) -> dict[str, bytes]:
    """``{relative posix path: bytes}`` for the template inside a built wheel."""
    contents: dict[str, bytes] = {}
    with zipfile.ZipFile(wheel) as archive:
        for name in sorted(archive.namelist()):
            if not name.startswith(_WHEEL_TEMPLATE_PREFIX) or name.endswith("/"):
                continue
            contents[name[len(_WHEEL_TEMPLATE_PREFIX) :]] = archive.read(name)
    return contents


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def drift(source: dict[str, bytes], packaged: dict[str, bytes]) -> list[str]:
    """Every difference between the source template and the packaged one, per file, both directions."""
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
    source = read_tree(TEMPLATE_ROOT)
    assert source, "the source template must not be empty"
    assert drift(source, dict(source)) == []


def test_drift_reports_a_file_missing_from_the_wheel() -> None:
    source = read_tree(TEMPLATE_ROOT)
    packaged = dict(source)
    del packaged[".moon/workspace.yml"]

    assert drift(source, packaged) == ["missing from the wheel: .moon/workspace.yml"]


def test_drift_reports_a_file_the_wheel_carries_alone() -> None:
    source = read_tree(TEMPLATE_ROOT)
    packaged = dict(source) | {"azure-pipelines.yml": b"trigger: none\n"}

    assert drift(source, packaged) == ["present in the wheel only: azure-pipelines.yml"]


def test_drift_reports_edited_content() -> None:
    source = read_tree(TEMPLATE_ROOT)
    packaged = dict(source)
    packaged["moon.yml"] = source["moon.yml"] + b"\n# edited in the wheel\n"

    assert drift(source, packaged) == ["differs: moon.yml"]


def test_the_guard_fires_on_a_mutated_extraction(tmp_path: Path) -> None:
    """Three drifts introduced on disk in a stand-in extraction are each named individually."""
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    for relative, payload in read_tree(TEMPLATE_ROOT).items():
        target = extracted / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    (extracted / "moon.yml").write_bytes(b"tasks: {}\n")
    (extracted / ".prototools").unlink()
    (extracted / ".github" / "workflows").mkdir(parents=True)
    (extracted / ".github" / "workflows" / "build.yml").write_bytes(b"on: push\n")

    findings = drift(read_tree(TEMPLATE_ROOT), read_tree(extracted))

    assert findings == [
        "missing from the wheel: .prototools",
        "present in the wheel only: .github/workflows/build.yml",
        "differs: moon.yml",
    ]


# ---------------------------------------------------------------------------
# The packaging decision this guard is shaped around
# ---------------------------------------------------------------------------


def test_the_template_has_exactly_one_editable_copy() -> None:
    """The template is package data shipped by ``packages``: no root ``templates/``, no ``force-include``.

    So no build-time copy can go stale between an edit and the next install.
    """
    assert not (REPO_ROOT / "templates").exists(), (
        "a repo-root templates/ would be a second editable copy; the template is package data"
    )
    assert TEMPLATE_ROOT.parent.parent.name == "swcraft_tools"

    pyproject = tomllib.loads((REPO_ROOT / _TOOLS_PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel_target = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel_target["packages"] == ["src/swcraft_tools"]
    assert "force-include" not in wheel_target, (
        "force-include materializes into site-packages at install time, so an editable install "
        "would keep serving the copy made by the last `pip install -e` — the template is shipped "
        "as package data precisely to avoid that"
    )


#: The template's client files under a dot-name, which Codex, Claude Code and Kiro read. Named
#: because packaging tools have dropped dot-entries before; the artifact cases must find them.
_CLIENT_DOT_FILES = frozenset(
    {
        ".claude/settings.json",
        ".codex/config.toml",
        ".codex/hooks.json",
        ".mcp.json",
        ".kiro/settings/mcp.json",
        ".kiro/steering/swcraft.md",
        ".kiro/hooks/swcraft-guard.json",
    }
)


def test_nothing_in_the_build_configuration_drops_a_template_dot_file() -> None:
    """The fast half of the dot-file guarantee: what hatchling would read to drop one.

    Hatchling ships every file under ``packages`` unless the build's ``exclude`` names it or
    the project root's ignore file does (``ignore-vcs = false``, read at ``packages/tools/``). So: the
    client dot-files exist in the source template, no ``exclude`` pattern reaches the template,
    ``packages/tools/`` carries no ignore file, and the hatch settings that could exclude more are absent.

    Does not reach hatchling's own behaviour, which only the artifact-marked cases below show.
    """
    assert _CLIENT_DOT_FILES <= set(read_tree(TEMPLATE_ROOT))
    pyproject = tomllib.loads((REPO_ROOT / _TOOLS_PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    build = pyproject["tool"]["hatch"]["build"]
    template = "/src/" + _WHEEL_TEMPLATE_PREFIX
    assert [pattern for pattern in build.get("exclude", []) if template.startswith(pattern.rstrip("/") + "/")] == []
    assert [pattern for pattern in build.get("exclude", []) if pattern.startswith(template.rstrip("/"))] == []
    for key in ("ignore-vcs", "only-include", "include", "artifacts"):
        assert key not in build and key not in build["targets"]["wheel"], key
    assert not (REPO_ROOT / _TOOLS_PROJECT / ".gitignore").exists()
    assert not (REPO_ROOT / _TOOLS_PROJECT / ".hgignore").exists()


# ---------------------------------------------------------------------------
# Against a real wheel (deselected from the fast run; `dev.py test-artifacts`)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def packaged_template(swcraft_tools_wheel: Path) -> dict[str, bytes]:
    """The template as it ships, read out of the session's one built wheel.

    Shared with ``test_model_contract_packaging.py``, so both guards read the same artifact.
    """
    return read_wheel_template(swcraft_tools_wheel)


@pytest.mark.artifact
def test_every_template_file_ships_byte_identical_in_the_wheel(packaged_template: dict[str, bytes]) -> None:
    findings = drift(read_tree(TEMPLATE_ROOT), packaged_template)

    assert findings == [], (
        "the template inside the built wheel has drifted from the source template — "
        "`swcraft init --integration moon` would scaffold something this repository never reviewed:\n"
        + "\n".join(findings)
    )


@pytest.mark.artifact
def test_the_wheel_and_the_source_carry_the_same_template_file_set(packaged_template: dict[str, bytes]) -> None:
    """The wheel's and the source's template file sets are equal, dot-files included."""
    source_names = set(read_tree(TEMPLATE_ROOT))
    packaged_names = set(packaged_template)

    assert packaged_names == source_names
    assert {name for name in packaged_names if name.startswith(".")} >= {
        ".gitignore",
        ".prototools",
    }
    assert {".moon/workspace.yml", ".moon/toolchain.yml", ".vscode/settings.json"} <= packaged_names
    assert _CLIENT_DOT_FILES <= packaged_names
