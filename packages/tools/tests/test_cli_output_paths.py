"""No ``swcraft`` command prints where the project is stored, but on the lines that announce a written file.

Each command of :data:`CASES` runs in-process through ``cli.main`` on a copy of ``examples/basic`` (a git
repository with one commit, since ``diff``, ``change-report`` and ``metrics`` read ``HEAD``), named by an
absolute ``--project``, from a working directory outside the project, in its text form and its ``--format
json`` form where it offers one. Every run must succeed with output, so a run that failed early cannot pass by
printing nothing. The runs read no ``.env``: ``env.checkout_root`` answers ``None``, as in an installed package.

Stdout and stderr hold neither the project's directory nor its model root, in any spelling. A line of
:data:`KEPT_IN_FULL` announcing a file the run wrote, moved or removed keeps its full path, because VS Code
links a path in the extension's output channel only when it is absolute under the workspace folder; that path
must lie inside the project. A path the user gave inside the project is printed relative to it, one outside it
as given.

What this does not reach: a command that takes no project (``docs``, ``guard``, ``mcp-serve``, ``agent``); the
commands that import into the model for real (``import model``, ``import requirements``, ``import component``
without ``--dry-run`` and ``experiment trace --apply`` without ``--dry-run``); an ``Error:`` line, whose
exception may name the user's own file in full; the warnings ``check_arxml`` writes about a configured schema it
rejects; and a failing run of any command.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import swcraft_tools.env as env_module
from conftest import REPO_ROOT, make_git_repository
from node_support import requires_node
from swcraft_tools.cli import main
from swcraft_tools.project import require_project

# The announcement lines that keep a full path, by their fixed prefix.
KEPT_IN_FULL = (
    "ARXML generated: ",
    "Created ",
    "Generated ARXML: ",
    "Generated diagrams: ",
    "Generated needs.json: ",
    "Generated proposals: ",
    "Generated report: ",
    "Generated result: ",
    "Generated test tables: ",
    "OUTPUT ",
    "Previous ARXML kept: ",
    "Removed ",
    "Report: ",
    "Skipped ",
    "Summary: ",
)

# An AUTOSAR 4.2.2 schema that admits every document, so ``validate`` runs in XSD mode without the
# licensed schema; only its file name and its target namespace are the real schema's.
ANY_AUTOSAR_XSD = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="http://autosar.org/schema/r4.0"
           elementFormDefault="qualified">
  <xs:element name="AUTOSAR">
    <xs:complexType>
      <xs:sequence>
        <xs:any namespace="##any" processContents="skip" minOccurs="0" maxOccurs="unbounded"/>
      </xs:sequence>
      <xs:anyAttribute namespace="##any" processContents="skip"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

# Each case: the runs that prepare it, then the run it checks; ``{manifest}`` is the
# absolute manifest, ``{project}`` the project directory, ``{outside}`` a directory
# beside the project, ``{fresh}`` an absolute directory ``init`` scaffolds.
CASES: dict[str, tuple[tuple[str, ...], ...]] = {
    "generate arxml": (("generate", "arxml", "--project", "{manifest}"),),
    "generate arxml again": (
        ("generate", "arxml", "--project", "{manifest}"),
        ("generate", "arxml", "--project", "{manifest}"),
    ),
    "generate arxml --arxml-version": (("generate", "arxml", "--arxml-version", "4.3.0", "--project", "{manifest}"),),
    "generate arxml --component": (("generate", "arxml", "--component", "ExampleSWC", "--project", "{manifest}"),),
    "generate arxml --all-components": (
        (
            "generate",
            "arxml",
            "--all-components",
            "--output-dir",
            "{project}/dist/components",
            "--project",
            "{manifest}",
        ),
    ),
    "generate needs": (("generate", "needs", "--project", "{manifest}"),),
    "generate diagrams": (("generate", "diagrams", "--project", "{manifest}"),),
    "generate test-tables": (("generate", "test-tables", "--project", "{manifest}"),),
    "validate": (("validate", "--project", "{manifest}"),),
    "validate --report": (("validate", "--report", "--project", "{manifest}"),),
    "check-model": (("check-model", "--project", "{manifest}"),),
    "check-model json": (("check-model", "--format", "json", "--project", "{manifest}"),),
    "clean-outputs": (
        ("validate", "--report", "--project", "{manifest}"),
        ("clean-outputs", "--project", "{manifest}"),
    ),
    "diff": (("diff", "--project", "{manifest}"),),
    "diff json": (("diff", "--format", "json", "--project", "{manifest}"),),
    "impact": (("impact", "ExampleSWC", "--project", "{manifest}"),),
    "impact json": (("impact", "ExampleSWC", "--format", "json", "--project", "{manifest}"),),
    "list": (("list", "--project", "{manifest}"),),
    "list json": (("list", "--format", "json", "--project", "{manifest}"),),
    "query": (("query", "swc", "--project", "{manifest}"),),
    "query json": (("query", "swc", "--format", "json", "--project", "{manifest}"),),
    "resource-report": (("resource-report", "--project", "{manifest}"),),
    "resource-report json": (("resource-report", "--format", "json", "--project", "{manifest}"),),
    "resource-report --report": (("resource-report", "--report", "--project", "{manifest}"),),
    "resource-report json --report": (("resource-report", "--format", "json", "--report", "--project", "{manifest}"),),
    "metrics": (("metrics", "--project", "{manifest}"),),
    "metrics json": (("metrics", "--format", "json", "--project", "{manifest}"),),
    "metrics --report": (("metrics", "--report", "--project", "{manifest}"),),
    "change-report": (("change-report", "--project", "{manifest}"),),
    "change-report json": (("change-report", "--format", "json", "--project", "{manifest}"),),
    "experiment allocation": (("experiment", "allocation", "--project", "{manifest}"),),
    "experiment allocation json": (("experiment", "allocation", "--format", "json", "--project", "{manifest}"),),
    "experiment trace": (("experiment", "trace", "--project", "{manifest}"),),
    "experiment trace json": (("experiment", "trace", "--format", "json", "--project", "{manifest}"),),
    "experiment trace --apply --dry-run": (
        ("experiment", "trace", "--project", "{manifest}"),
        (
            "experiment",
            "trace",
            "--apply={project}/dist/experiments/trace/proposals.json",
            "--dry-run",
            "--project",
            "{manifest}",
        ),
    ),
    "passport": (("passport", "--project", "{manifest}"),),
    "import component --dry-run": (
        (
            "generate",
            "arxml",
            "--component",
            "ExampleSWC",
            "--output",
            "{outside}/ExampleSWC.arxml",
            "--project",
            "{manifest}",
        ),
        ("import", "component", "--source", "{outside}/ExampleSWC.arxml", "--dry-run", "--project", "{manifest}"),
    ),
    "import component --dry-run from inside": (
        (
            "generate",
            "arxml",
            "--component",
            "ExampleSWC",
            "--output",
            "{project}/exchange/ExampleSWC.arxml",
            "--project",
            "{manifest}",
        ),
        (
            "import",
            "component",
            "--source",
            "{project}/exchange/ExampleSWC.arxml",
            "--dry-run",
            "--project",
            "{manifest}",
        ),
    ),
    "import model --dry-run from inside": (
        ("init", "{fresh}"),
        (
            "generate",
            "arxml",
            "--component",
            "ExampleSWC",
            "--output",
            "{fresh}/exchange/ExampleSWC.arxml",
            "--project",
            "{manifest}",
        ),
        (
            "import",
            "model",
            "--source",
            "{fresh}/exchange/ExampleSWC.arxml",
            "--dry-run",
            "--project",
            "{fresh}/swcraft.yaml",
        ),
    ),
    "init": (("init", "{fresh}"),),
}


def _run(
    argv: tuple[str, ...], places: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[str, str]:
    """Run ``swcraft *argv*`` (placeholders from *places*) through ``cli.main``; assert exit 0; its stdout, stderr."""
    filled = [part.format(**places) for part in argv]
    monkeypatch.setattr("sys.argv", ["swcraft", *filled])
    try:
        main()
    except SystemExit as stop:
        assert stop.code in (0, None), f"swcraft {' '.join(argv)} exited {stop.code}: {capsys.readouterr().err}"
    captured = capsys.readouterr()
    return captured.out, captured.err


def _copy_example(tmp_path: Path) -> Path:
    """``examples/basic`` copied to ``tmp_path/basic``, without its outputs."""
    project = tmp_path / "basic"
    shutil.copytree(REPO_ROOT / "examples" / "basic", project, ignore=shutil.ignore_patterns("dist", "views"))
    return project


def _without_a_checkout(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch ``env.checkout_root`` to answer ``None``, as in an installed package; the list records each call."""
    asked: list[str] = []
    monkeypatch.setattr(env_module, "checkout_root", lambda module_file=None: asked.append("asked"))
    return asked


def _spellings(*directories: Path) -> set[str]:
    """Each directory as given and resolved, in its native and its POSIX spelling, and each as JSON spells it."""
    plain = {
        spelling
        for directory in directories
        for each in (directory, directory.resolve())
        for spelling in (str(each), each.as_posix())
    }
    return plain | {json.dumps(spelling)[1:-1] for spelling in plain}


def _kept_path_outside(line: str, prefix: str, roots: set[str]) -> tuple[str, bool]:
    """The kept *line* without the root spelling its path starts with, and whether it starts with none.

    A text after *prefix* that is not absolute is not a kept path, and the line is returned whole.
    The root is matched as a spelling up to a separator, never by splitting the line into words, so a
    path holding a space is matched whole.
    """
    named = line[len(prefix) :]
    if not Path(named).is_absolute():
        return line, False
    for root in sorted(roots, key=len, reverse=True):
        rest = named[len(root) :]
        if named.startswith(root) and rest[:1] in ("", "/", "\\"):
            return prefix + rest, False
    return line, True


def _assert_names_no_location(case: str, out: str, err: str, root: Path, tmp_path: Path) -> None:
    """No line of *out* and *err* names *root* or its model root, but a kept line's path inside it."""
    forbidden = _spellings(root, root / "model")
    allowed = _spellings(root, tmp_path / "outside")
    for line in (out + err).splitlines():
        prefix = next((each for each in KEPT_IN_FULL if line.startswith(each)), None)
        rest, outside = (line, False) if prefix is None else _kept_path_outside(line, prefix, allowed)
        assert not outside, f"{case}: {line!r} announces a path outside the project"
        leaked = sorted(spelling for spelling in forbidden if spelling in rest)
        assert not leaked, f"{case}: the line {line!r} names the project's location"


@pytest.mark.parametrize("case", sorted(CASES))
def test_no_command_prints_the_project_location(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if case == "generate diagrams":
        requires_node()
    project = _copy_example(tmp_path)
    make_git_repository(project)
    (tmp_path / "outside").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    asked = _without_a_checkout(monkeypatch)
    fresh = tmp_path / "fresh"
    places = {
        "manifest": str(project / "swcraft.yaml"),
        "project": str(project),
        "outside": str(tmp_path / "outside"),
        "fresh": str(fresh),
    }
    *preparing, checked = CASES[case]
    for argv in preparing:
        _run(argv, places, monkeypatch, capsys)

    out, err = _run(checked, places, monkeypatch, capsys)

    assert out or err, f"swcraft {' '.join(checked)} printed nothing"
    assert asked, "no run searched for a .env, so the patched checkout root was never reached"
    root = fresh if any("{fresh}" in part for part in checked) else project
    _assert_names_no_location(case, out, err, root, tmp_path)
    if "from inside" in case:
        assert "report: exchange/ExampleSWC.arxml" in out, out


def test_validate_names_a_schema_inside_the_project_relative_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_example(tmp_path)
    (tmp_path / "outside").mkdir()
    schema = project / "schemas" / "AUTOSAR_4-2-2.xsd"
    schema.parent.mkdir()
    schema.write_text(ANY_AUTOSAR_XSD, encoding="utf-8", newline="\n")
    monkeypatch.setenv("SWCRAFT_AUTOSAR_XSD_PATH", str(schema))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    asked = _without_a_checkout(monkeypatch)

    out, err = _run(
        ("validate", "--project", "{manifest}"), {"manifest": str(project / "swcraft.yaml")}, monkeypatch, capsys
    )

    assert asked, "no run searched for a .env, so the patched checkout root was never reached"
    assert "ARXML validated against schemas/AUTOSAR_4-2-2.xsd." in out.splitlines(), out
    _assert_names_no_location("validate with an in-project schema", out, err, project, tmp_path)


def test_resource_report_is_headed_by_the_project_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _copy_example(tmp_path) / "swcraft.yaml"
    monkeypatch.chdir(tmp_path)
    asked = _without_a_checkout(monkeypatch)

    out, _ = _run(("resource-report", "--project", "{manifest}"), {"manifest": str(manifest)}, monkeypatch, capsys)

    assert asked, "the run searched for no .env, so the patched checkout root was never reached"
    assert out.splitlines()[0] == f"Resource utilization report: {require_project(str(manifest)).name}"


def test_a_kept_path_holding_a_space_is_matched_whole(tmp_path: Path) -> None:
    root = tmp_path / "Jane Doe" / "basic"
    line = f"Generated report: {root / 'dist' / 'report.md'}"

    rest, outside = _kept_path_outside(line, "Generated report: ", _spellings(root))

    assert not outside
    assert str(root) not in rest
    assert rest.endswith("report.md")
