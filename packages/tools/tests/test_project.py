"""``swcraft_tools.project``: manifest discovery, its fields, and the rules on the paths it names."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

import pytest
import yaml
from conftest import _write_manifest
from pydantic import ValidationError
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.project import (
    AUDIT_LOG_FILE,
    BUILD_REPORT_FILE,
    DIAGRAM_FILE_SUFFIXES,
    DIAGRAM_REPORT_FILE,
    NAMED_OUTPUT_DIRECTORIES,
    REPORT_DIRECTORIES,
    ManifestOutputCollisionError,
    OutputPaths,
    SwcraftProject,
    experiment_default_directory,
    experiment_output_directory,
    load_project,
    project_relative_name,
    require_project,
    toolchain_versions_agree,
)
from symlink_support import symlink_or_skip

MINIMAL_MANIFEST = {
    "name": "test-project",
    "schema_version": "1.0",
    "model_root": "model",
    "dist_root": "dist",
    "views_root": "views",
}

FULL_MANIFEST = {
    **MINIMAL_MANIFEST,
    "outputs": {
        "arxml": "dist/model.arxml",
        "needs": "dist/needs.json",
    },
}


# ---------------------------------------------------------------------------
# load_project — manifest discovery
# ---------------------------------------------------------------------------


def test_load_finds_yaml_in_cwd(tmp_path: Path) -> None:
    """Place swcraft.yaml in tmp_path; load_project(str(tmp_path)) returns project with correct name."""
    _write_manifest(tmp_path, MINIMAL_MANIFEST)

    project = load_project(str(tmp_path))

    assert project is not None
    assert isinstance(project, SwcraftProject)
    assert project.name == "test-project"


def test_load_walks_up_from_child_dir(tmp_path: Path) -> None:
    """yaml in tmp_path, start from tmp_path/subdir — walk-up finds it."""
    _write_manifest(tmp_path, MINIMAL_MANIFEST)
    subdir = tmp_path / "subdir"
    subdir.mkdir()

    project = load_project(str(subdir))

    assert project is not None
    assert project.name == "test-project"


def test_load_returns_none_when_nothing(tmp_path: Path, monkeypatch) -> None:
    """No yaml anywhere, no env var → returns None."""
    monkeypatch.delenv("SWCRAFT_MANIFEST", raising=False)
    # Use a directory that definitely has no swcraft.yaml in its ancestry
    isolated = tmp_path / "no_manifest_here"
    isolated.mkdir()

    project = load_project(str(isolated))

    assert project is None


def test_load_falls_back_to_env_var(tmp_path: Path, monkeypatch) -> None:
    """No yaml in start_dir; SWCRAFT_MANIFEST env var set → returns project from that path."""
    manifest = _write_manifest(tmp_path / "elsewhere", MINIMAL_MANIFEST)
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(manifest))

    # Start from a directory with no yaml
    isolated = tmp_path / "no_yaml_here"
    isolated.mkdir()

    project = load_project(str(isolated))

    assert project is not None
    assert project.name == "test-project"


# ---------------------------------------------------------------------------
# load_project — resolved paths
# ---------------------------------------------------------------------------


def test_resolved_model_root_absolute(tmp_path: Path) -> None:
    """Relative model_root: model → resolved to <project_dir>/model (absolute)."""
    _write_manifest(tmp_path, MINIMAL_MANIFEST)

    project = load_project(str(tmp_path))

    assert project is not None
    resolved = project.resolved_model_root
    assert Path(resolved).is_absolute()
    assert resolved == str(tmp_path / "model")


def test_resolved_arxml_path(tmp_path: Path) -> None:
    """outputs.arxml: dist/model.arxml → <project_dir>/dist/model.arxml."""
    _write_manifest(tmp_path, FULL_MANIFEST)

    project = load_project(str(tmp_path))

    assert project is not None
    resolved = project.resolved_arxml_path
    assert Path(resolved).is_absolute()
    assert resolved == str(tmp_path / "dist" / "model.arxml")


def test_the_manifest_declares_no_c4_output(tmp_path: Path) -> None:
    """``outputs`` has no ``c4``: the diagrams are written into ``views_root``.

    A stale ``c4:`` line is dropped and the project loads; ``check-model`` and ``validate`` report
    it as ``manifest.unknown-key`` (``test_cli.py``'s ``TestManifestUnknownKey``).
    """
    assert set(OutputPaths.model_fields) == {"arxml", "needs"}
    _write_manifest(tmp_path, {**FULL_MANIFEST, "outputs": {**FULL_MANIFEST["outputs"], "c4": "views/model.c4"}})

    project = require_project(str(tmp_path / "swcraft.yaml"))

    assert not hasattr(project, "resolved_c4_path")
    assert project.outputs.model_dump() == {"arxml": "dist/model.arxml", "needs": "dist/needs.json"}


def test_outputs_default_values(tmp_path: Path) -> None:
    """No outputs: block in yaml → defaults apply (dist/model.arxml, etc.)."""
    # MINIMAL_MANIFEST has no outputs block
    _write_manifest(tmp_path, MINIMAL_MANIFEST)

    project = load_project(str(tmp_path))

    assert project is not None
    assert project.resolved_arxml_path == str(tmp_path / "dist" / "model.arxml")
    assert project.resolved_needs_path == str(tmp_path / "dist" / "needs.json")


# ---------------------------------------------------------------------------
# outputs.needs names one file: the Sphinx-Needs needs.json
# ---------------------------------------------------------------------------


def test_outputs_needs_names_the_file_generate_needs_writes(tmp_path: Path) -> None:
    """``outputs.needs`` is a file path, resolved like ``outputs.arxml``."""
    _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "outputs": {"needs": "dist/published/needs.json"}})

    project = require_project(str(tmp_path / "swcraft.yaml"))

    assert project.resolved_needs_path == str(tmp_path / "dist" / "published" / "needs.json")


def test_an_absolute_needs_file_inside_the_project_loads(tmp_path: Path) -> None:
    """An absolute ``outputs`` value inside the project is allowed, as for ``outputs.arxml``."""
    absolute = (tmp_path / "dist" / "needs.json").as_posix()
    _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "outputs": {"needs": absolute}})

    assert require_project(str(tmp_path / "swcraft.yaml")).resolved_needs_path == str(tmp_path / "dist" / "needs.json")


@pytest.mark.parametrize("written", ["dist/requirements.rst", "dist/needs", "dist/Export.RST"])
def test_no_spelling_of_outputs_needs_is_refused_for_its_suffix(tmp_path: Path, written: str) -> None:
    """No suffix is refused: the file is written where the value names."""
    _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "outputs": {"needs": written}})

    assert require_project(str(tmp_path / "swcraft.yaml")).resolved_needs_path == str(tmp_path / written)


def test_a_manifest_still_holding_outputs_rst_loads_and_drops_the_key(tmp_path: Path) -> None:
    """A manifest holding the renamed key still loads, with the default ``needs.json`` path.

    The loader does not refuse the key: ``check-model`` reports it as ``manifest.unknown-key`` (``test_cli.py``)."""
    _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "outputs": {"arxml": "dist/model.arxml", "rst": "dist/needs"}})

    project = require_project(str(tmp_path / "swcraft.yaml"))

    assert project.outputs.model_dump() == {"arxml": "dist/model.arxml", "needs": "dist/needs.json"}


@pytest.mark.parametrize(
    "written",
    [pytest.param("../outside.json", id="outside"), pytest.param("model/needs.json", id="inside-the-model")],
)
def test_an_outputs_needs_outside_the_project_or_over_the_model_is_refused(tmp_path: Path, written: str) -> None:
    """``outputs.needs`` keeps the outside and overlap rules every output has; the sentence names the field."""
    _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "outputs": {"needs": written}})

    with pytest.raises(ValueError, match=r"outputs\.needs"):
        require_project(str(tmp_path / "swcraft.yaml"))


@pytest.mark.parametrize(
    ("outputs", "what"),
    [
        pytest.param({"needs": "dist/model.arxml"}, "the file 'outputs.arxml' names", id="the-arxml"),
        pytest.param(
            {"arxml": "out/m.arxml", "needs": "OUT/m.ARXML"}, "the file 'outputs.arxml' names", id="case-folded"
        ),
        pytest.param({"needs": "dist/model_old.arxml"}, "the previous ARXML kept beside 'outputs.arxml'", id="old"),
        pytest.param(
            {"arxml": "dist/model.", "needs": "dist/model._old"},
            "the previous ARXML kept beside 'outputs.arxml'",
            id="old-of-a-trailing-dot",
        ),
        pytest.param({"needs": "dist/audit.jsonl"}, "the audit log in 'dist_root'", id="the-audit-log"),
        pytest.param({"needs": "swcraft.yaml"}, "the manifest", id="the-manifest"),
        pytest.param({"needs": "dist/build-report.md"}, "the build report in 'dist_root'", id="the-build-report"),
        pytest.param({"needs": "Dist/Build-Report.MD"}, "the build report in 'dist_root'", id="report-case-folded"),
        *(
            pytest.param({"needs": needs}, "a file `swcraft generate diagrams` writes in 'views_root'", id=needs)
            for needs in ("views/index.html", "views/report.txt", "views/ibd-Chassis.svg", "Views/needs.HTML")
        ),
        *(
            pytest.param({"needs": needs}, f"a file in the report directory of {command} in 'dist_root'", id=needs)
            for needs, command in (
                ("dist/metrics/metrics.md", "`swcraft metrics --report`"),
                ("dist/resource-report/resource-report.md", "`swcraft resource-report --report`"),
                ("dist/change-report/change-report.md", "`swcraft change-report`"),
                ("dist/change-report/change-report-pr.md", "`swcraft change-report`"),
                ("Dist/Change-Report/change-report.JSON", "`swcraft change-report`"),
                ("dist/change-report/overview-x-before.svg", "`swcraft change-report`"),
                ("dist/metrics/sub/needs.json", "`swcraft metrics --report`"),
                ("dist/test-tables/needs.json", "`swcraft generate test-tables`"),
                ("Dist/Test-Tables/manifest.JSON", "`swcraft generate test-tables`"),
            )
        ),
        *(
            pytest.param({"needs": needs}, f"the report directory of {command} in 'dist_root'", id=needs)
            for needs, command in (
                ("dist/metrics", "`swcraft metrics --report`"),
                ("dist/resource-report", "`swcraft resource-report --report`"),
                ("Dist/Change-Report", "`swcraft change-report`"),
                ("dist/test-tables", "`swcraft generate test-tables`"),
                ("Dist/TEST-TABLES", "`swcraft generate test-tables`"),
            )
        ),
        pytest.param({"needs": "dist/components"}, "the directory of the single-component exports in 'dist_root'"),
        pytest.param({"needs": "dist/experiments"}, "the directory of the experiment reports in 'dist_root'"),
    ],
)
def test_an_outputs_needs_naming_a_file_the_project_keeps_is_refused(
    tmp_path: Path, outputs: dict[str, str], what: str
) -> None:
    """Rule 4: ``generate needs`` replaces its file whole: never another output's, the audit log or the manifest.

    The shared sentence names the field and what it names, never a location; the terminal's names
    both resolved locations.
    """
    _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "outputs": outputs})

    with pytest.raises(ManifestOutputCollisionError) as refused:
        require_project(str(tmp_path / "swcraft.yaml"))

    assert str(refused.value) == (
        f"Manifest field 'outputs.needs' names {what}. `swcraft generate needs` would overwrite that file: give the "
        "needs.json a file of its own."
    )
    assert str(tmp_path) not in str(refused.value)
    assert refused.value.render(full_path=True).startswith(
        f"Manifest field 'outputs.needs' resolves to '{(tmp_path / outputs['needs']).resolve()}', which is {what} ('"
    )


@pytest.mark.parametrize(
    "needs",
    [
        "dist/model.arxml.json",
        "dist/audit.json",
        "dist/sub/audit.jsonl",
        "swcraft.json",
        "dist/build-report.json",
        "views/needs.json",
        "views/sub/index.html",
        "dist/index.html",
        "dist/metrics.md",
        "dist/metrics-needs.json",
        "dist/sub/metrics/metrics.md",
        "dist/change-report.json",
        "dist/components/needs.json",
        "dist/experiments/needs.json",
        "metrics/metrics.md",
        "dist/test-tables2",
        "dist/test-tables2/needs.json",
        "dist/test-tables.json",
    ],
)
def test_an_outputs_needs_beside_those_files_loads(tmp_path: Path, needs: str) -> None:
    """Rule 4 compares whole names: a neighbour of a kept file is a file of its own."""
    _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "outputs": {"needs": needs}})

    assert require_project(str(tmp_path / "swcraft.yaml")).resolved_needs_path == str(tmp_path / needs)


def test_an_outputs_needs_at_nodes_split_of_a_trailing_dot_loads(tmp_path: Path) -> None:
    """``dist/model.``'s previous file is ``dist/model._old``; ``dist/model_old.`` is a file of its own."""
    _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "outputs": {"arxml": "dist/model.", "needs": "dist/model_old."}})

    assert require_project(str(tmp_path / "swcraft.yaml")).resolved_needs_path == str(tmp_path / "dist/model_old.")


def test_rule_4_holds_the_writers_own_file_names() -> None:
    """The loader imports no writer, so it repeats their names; these hold the two equal."""
    from swcraft_tools.build_report import BUILD_REPORT_FILE as written_report
    from swcraft_tools.editing.audit import AUDIT_FILENAME
    from swcraft_tools.writer.diagrams import GENERATED_FILE_RE
    from swcraft_tools.writer.diagrams.page import INDEX_PAGE
    from swcraft_tools.writer.diagrams.report import REPORT_FILE

    assert AUDIT_LOG_FILE == AUDIT_FILENAME
    assert BUILD_REPORT_FILE == written_report
    assert DIAGRAM_REPORT_FILE == REPORT_FILE
    assert INDEX_PAGE.endswith(DIAGRAM_FILE_SUFFIXES)
    suffixes = re.search(r"\\.\(\?:([a-z|]+)\)$", GENERATED_FILE_RE.pattern)
    assert suffixes is not None
    assert set(DIAGRAM_FILE_SUFFIXES) == {f".{suffix}" for suffix in suffixes.group(1).split("|")}


def test_rule_4_holds_the_report_writers_own_directories() -> None:
    """The report directories and the named-output directories are the writers' own names, held equal here."""
    from swcraft_tools import (
        change_report_render,
        experiment_frame,
        metrics_render,
        resource_report,
        tables_for_testing,
    )
    from swcraft_tools.component_exchange.export import COMPONENTS_DIR

    assert list(REPORT_DIRECTORIES) == [
        metrics_render.REPORT_DIRECTORY,
        resource_report.REPORT_DIRECTORY,
        change_report_render.REPORT_DIRECTORY,
        tables_for_testing.REPORT_DIRECTORY,
    ]
    assert list(NAMED_OUTPUT_DIRECTORIES) == [COMPONENTS_DIR, experiment_frame.EXPERIMENTS_DIRECTORY]


def test_the_extension_mirrors_the_loaders_default() -> None:
    """``vscode_extension/src/project.ts`` repeats the default and rule 4's names; the two stay equal."""
    source = (Path(__file__).resolve().parents[3] / "vscode_extension" / "src" / "project.ts").read_text(
        encoding="utf-8"
    )
    default = re.search(r'export const DEFAULT_NEEDS_OUTPUT = "([^"]*)";', source)
    assert default is not None
    assert default.group(1) == OutputPaths().needs == "dist/needs.json"
    for name, value in (
        ("AUDIT_LOG_FILE", AUDIT_LOG_FILE),
        ("BUILD_REPORT_FILE", BUILD_REPORT_FILE),
        ("DIAGRAM_REPORT_FILE", DIAGRAM_REPORT_FILE),
    ):
        assert f'export const {name} = "{value}";' in source, name
    suffixes = ", ".join(f'"{suffix}"' for suffix in DIAGRAM_FILE_SUFFIXES)
    assert f"export const DIAGRAM_FILE_SUFFIXES: readonly string[] = [{suffixes}];" in source
    for name, values in (
        ("REPORT_DIRECTORIES", REPORT_DIRECTORIES),
        ("NAMED_OUTPUT_DIRECTORIES", NAMED_OUTPUT_DIRECTORIES),
    ):
        listed = re.search(rf"export const {name}: readonly string\[\] = \[([^\]]*)\];", source)
        assert listed is not None, name
        assert re.findall(r'"([^"]*)"', listed.group(1)) == list(values), name
    assert "RST_FILE_REFUSAL" not in source


def test_the_extension_splits_the_previous_arxml_name_as_python_does() -> None:
    """``project.test.ts``'s table of ``previousArxmlPath`` cases is what ``previous_arxml_path`` gives here.

    Node's ``path.parse`` splits ``model.`` into ``model`` and ``.``; Python 3.11's pathlib gives it
    no suffix, so rule 4's previous file is ``model._old``.
    """
    from swcraft_tools.writer.arxml_xml import previous_arxml_path

    source = (Path(__file__).resolve().parents[3] / "vscode_extension" / "src" / "project.test.ts").read_text(
        encoding="utf-8"
    )
    table = re.search(r"const PREVIOUS_ARXML_NAMES[^=]*= \[\n(.*?)\n\];", source, flags=re.DOTALL)
    assert table is not None
    pairs = re.findall(r'\["([^"]*)", "([^"]*)"\]', table.group(1))
    assert ("model.", "model._old") in pairs
    for name, previous in pairs:
        assert previous_arxml_path(f"dist/{name}").as_posix() == f"dist/{previous}", name


# ---------------------------------------------------------------------------
# require_project
# ---------------------------------------------------------------------------


def test_require_project_raises_on_missing() -> None:
    """require_project('/nonexistent/swcraft.yaml') → FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        require_project("/nonexistent/swcraft.yaml")


def test_require_project_no_discovery(tmp_path: Path) -> None:
    """Empty path string → FileNotFoundError (not a discovery call)."""
    with pytest.raises(FileNotFoundError):
        require_project("")


def test_require_project_loads_valid_manifest(tmp_path: Path) -> None:
    """require_project with valid path → SwcraftProject loaded correctly."""
    manifest = _write_manifest(tmp_path, FULL_MANIFEST)

    project = require_project(str(manifest))

    assert isinstance(project, SwcraftProject)
    assert project.name == "test-project"
    assert project.source == "manifest"


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_load_project_nonexistent_manifest_env(tmp_path: Path, monkeypatch) -> None:
    """SWCRAFT_MANIFEST points to a non-existent file → load_project returns None."""
    nonexistent = str(tmp_path / "nonexistent" / "swcraft.yaml")
    monkeypatch.setenv("SWCRAFT_MANIFEST", nonexistent)

    isolated = tmp_path / "no_yaml_here"
    isolated.mkdir()

    result = load_project(str(isolated))
    assert result is None


# ---------------------------------------------------------------------------
# Path traversal guard
# ---------------------------------------------------------------------------


def test_path_traversal_model_root_rejected(tmp_path: Path) -> None:
    """model_root with .. that escapes project root → ValueError on load."""
    bad = {
        "name": "evil",
        "schema_version": "1.0",
        "model_root": "../../etc",
        "dist_root": "dist",
        "views_root": "views",
    }
    manifest = tmp_path / "swcraft.yaml"
    manifest.write_text(yaml.safe_dump(bad), encoding="utf-8")

    with pytest.raises(ValueError, match="model_root"):
        require_project(str(manifest))


def test_path_traversal_outputs_rejected(tmp_path: Path) -> None:
    """outputs.arxml that escapes project root → ValueError on load."""
    bad = {
        "name": "evil",
        "schema_version": "1.0",
        "model_root": "model",
        "dist_root": "dist",
        "views_root": "views",
        "outputs": {"arxml": "../../tmp/pwned.arxml", "needs": "dist/needs.json"},
    }
    manifest = tmp_path / "swcraft.yaml"
    manifest.write_text(yaml.safe_dump(bad), encoding="utf-8")

    with pytest.raises(ValueError, match="outputs.arxml"):
        require_project(str(manifest))


@pytest.mark.parametrize(
    ("field", "manifest_patch"),
    [
        ("dist_root", {"dist_root": "model/dist"}),
        ("views_root", {"views_root": "model/views"}),
        ("outputs.arxml", {"outputs": {"arxml": "model/model.arxml", "needs": "dist/needs.json"}}),
        ("outputs.needs", {"outputs": {"arxml": "dist/m.arxml", "needs": "model/needs.json"}}),
    ],
)
def test_a_generated_path_inside_the_model_root_is_rejected(tmp_path: Path, field: str, manifest_patch: dict) -> None:
    """Generated output may not land inside the source of truth.

    Inside ``model/`` it is indistinguishable from authored content: copied into a scoped
    session's shadow, counted in ``model_fingerprint()``, and written before anything is approved.
    """
    manifest = tmp_path / "swcraft.yaml"
    manifest.write_text(yaml.safe_dump({**FULL_MANIFEST, **manifest_patch}), encoding="utf-8")

    with pytest.raises(ValueError, match=f"{re.escape(field)}.*overlaps 'model_root'"):
        require_project(str(manifest))


@pytest.mark.parametrize("field", ["dist_root", "views_root"])
def test_a_model_root_inside_a_generated_root_is_rejected(tmp_path: Path, field: str) -> None:
    """The other direction: a build's clean step removes those roots, so a rebuild would delete the model."""
    manifest = tmp_path / "swcraft.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                **MINIMAL_MANIFEST,
                "model_root": f"{'dist' if field == 'dist_root' else 'views'}/model",
                "outputs": {"arxml": "dist/m.arxml", "needs": "dist/needs.json"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="overlaps 'model_root'"):
        require_project(str(manifest))


def test_a_dist_root_equal_to_the_model_root_is_rejected(tmp_path: Path) -> None:
    manifest = tmp_path / "swcraft.yaml"
    manifest.write_text(yaml.safe_dump({**MINIMAL_MANIFEST, "dist_root": "model"}), encoding="utf-8")

    with pytest.raises(ValueError, match="overlaps 'model_root'"):
        require_project(str(manifest))


def test_sibling_generated_roots_are_still_accepted(tmp_path: Path) -> None:
    """A ``model`` / ``models`` pair, which a plain prefix test would wrongly call an overlap."""
    manifest = tmp_path / "swcraft.yaml"
    manifest.write_text(yaml.safe_dump({**FULL_MANIFEST, "views_root": "models_views"}), encoding="utf-8")

    project = require_project(str(manifest))
    assert project.resolved_views_root == str(tmp_path / "models_views")


@pytest.mark.parametrize(
    ("field", "manifest_patch"),
    [
        ("dist_root", {"dist_root": "Model/dist"}),
        ("views_root", {"views_root": "MODEL/views"}),
        ("dist_root", {"model_root": "Model", "dist_root": "model/dist"}),
        (
            "outputs.arxml",
            {"outputs": {"arxml": "Model/swc/EngineController.yaml", "needs": "dist/needs.json"}},
        ),
        ("outputs.needs", {"outputs": {"arxml": "dist/m.arxml", "needs": "MODEL/needs.json"}}),
    ],
)
def test_a_case_variant_of_the_model_root_is_rejected_on_every_platform(
    tmp_path: Path, field: str, manifest_patch: dict
) -> None:
    """The overlap rule is case-insensitive, unconditionally.

    On APFS and NTFS ``model`` and ``Model`` are one directory, and a committed manifest cannot
    know which filesystem it will be opened on, so the rejection is the same on Linux.
    """
    manifest = tmp_path / "swcraft.yaml"
    manifest.write_text(yaml.safe_dump({**FULL_MANIFEST, **manifest_patch}), encoding="utf-8")

    with pytest.raises(ValueError, match=f"{re.escape(field)}.*overlaps 'model_root'"):
        require_project(str(manifest))


def test_a_case_variant_of_a_sibling_root_is_still_accepted(tmp_path: Path) -> None:
    """Casefolding does not widen the overlap into a prefix test: ``model`` / ``models_views`` stays legal."""
    manifest = tmp_path / "swcraft.yaml"
    manifest.write_text(yaml.safe_dump({**FULL_MANIFEST, "views_root": "Models_Views"}), encoding="utf-8")

    project = require_project(str(manifest))
    assert project.resolved_views_root == str(tmp_path / "Models_Views")


def test_a_generated_root_symlinked_into_the_model_is_rejected(tmp_path: Path) -> None:
    """The overlap rule follows links, because it compares resolved paths.

    So rule 2 covers the whole generated subtree: every file under a refused ``dist_root`` is
    outside the model too, with no row of its own.
    """
    (tmp_path / "model").mkdir()
    (tmp_path / "model" / "generated").mkdir()
    symlink_or_skip(tmp_path / "dist", tmp_path / "model" / "generated")
    manifest = tmp_path / "swcraft.yaml"
    manifest.write_text(yaml.safe_dump(MINIMAL_MANIFEST), encoding="utf-8")

    with pytest.raises(ValueError, match="dist_root.*overlaps 'model_root'"):
        require_project(str(manifest))


def test_a_link_below_a_generated_root_is_outside_the_validated_surface(tmp_path: Path) -> None:
    """The stated boundary: the subject is the manifest, not the filesystem, so the load succeeds.

    A link planted below a validated root (``dist/audit.jsonl`` pointing at a model file) is not
    detected. The LLM is untrusted and the user's filesystem trusted, and no MCP tool creates a
    link, so only the user can plant one; probing would touch the filesystem on every load.
    """
    model_root = tmp_path / "model"
    (model_root / "swc").mkdir(parents=True)
    authored = model_root / "swc" / "SwcOne.yaml"
    authored.write_text("name: SwcOne\ntype: application\n", encoding="utf-8")
    (tmp_path / "dist").mkdir()
    symlink_or_skip(tmp_path / "dist" / "audit.jsonl", authored)
    manifest = tmp_path / "swcraft.yaml"
    manifest.write_text(yaml.safe_dump(MINIMAL_MANIFEST), encoding="utf-8")

    project = require_project(str(manifest))

    assert project.resolved_dist_root == str(tmp_path / "dist")


def test_valid_absolute_path_inside_project_accepted(tmp_path: Path) -> None:
    """Absolute path that resolves inside the project root is accepted."""
    inside = str(tmp_path / "model")
    good = {
        "name": "ok-project",
        "schema_version": "1.0",
        "model_root": inside,
        "dist_root": "dist",
        "views_root": "views",
    }
    manifest = tmp_path / "swcraft.yaml"
    manifest.write_text(yaml.safe_dump(good), encoding="utf-8")

    project = require_project(str(manifest))
    assert project.resolved_model_root == inside


def test_schema_validation_rejects_missing_name(tmp_path: Path) -> None:
    """yaml without name field → ValidationError raised."""
    bad_manifest = {
        "schema_version": "1.0",
        "model_root": "model",
        "dist_root": "dist",
        "views_root": "views",
    }
    manifest = tmp_path / "swcraft.yaml"
    manifest.write_text(yaml.safe_dump(bad_manifest), encoding="utf-8")

    with pytest.raises(ValidationError):
        require_project(str(manifest))


# ---------------------------------------------------------------------------
# arxml_version — the target AUTOSAR version
# ---------------------------------------------------------------------------


class TestArxmlVersionField:
    """``arxml_version`` is additive, defaulted to 4.2.2 and unrelated to ``schema_version``.

    ``schema_version`` is the manifest schema's version; the two are asserted apart.
    """

    def test_defaults_to_4_2_2_when_the_manifest_omits_it(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, MINIMAL_MANIFEST)

        project = require_project(str(tmp_path / "swcraft.yaml"))

        assert project.arxml_version == "4.2.2"

    def test_a_manifest_without_the_field_loads_unchanged(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, FULL_MANIFEST)

        project = require_project(str(tmp_path / "swcraft.yaml"))

        assert project.name == "test-project"
        assert project.schema_version == "1.0"
        assert project.resolved_model_root == str(tmp_path / "model")
        assert project.resolved_arxml_path == str(tmp_path / "dist" / "model.arxml")

    def test_schema_version_is_untouched_by_the_new_field(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "arxml_version": "4.3.0"})

        project = require_project(str(tmp_path / "swcraft.yaml"))

        assert project.schema_version == "1.0"
        assert project.arxml_version == "4.3.0"

    def test_a_declared_supported_version_is_read_back(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "arxml_version": "4.3.0"})

        project = require_project(str(tmp_path / "swcraft.yaml"))

        assert project.arxml_version == "4.3.0"
        assert project.resolved_arxml_version is ArxmlVersion.V4_3_0

    def test_resolved_property_is_the_enum_member(self, tmp_path: Path) -> None:
        # Callers pass the resolved_* property to the writer, so it is an ArxmlVersion, not a string.
        _write_manifest(tmp_path, MINIMAL_MANIFEST)

        project = require_project(str(tmp_path / "swcraft.yaml"))

        assert project.resolved_arxml_version is ArxmlVersion.V4_2_2
        assert project.resolved_arxml_version.schema_filename == "AUTOSAR_4-2-2.xsd"

    @pytest.mark.parametrize("value", ["4.5.0", "4.2", "R4.2.2", "latest", ""])
    def test_an_unsupported_version_is_rejected_at_load_time(self, tmp_path: Path, value: str) -> None:
        # At load time, where the field is read and can be named.
        _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "arxml_version": value})

        with pytest.raises(ValueError) as exc:
            require_project(str(tmp_path / "swcraft.yaml"))

        message = str(exc.value)
        assert "arxml_version" in message
        assert value in message or repr(value) in message
        assert "4.2.2" in message and "4.3.0" in message

    def test_a_non_string_version_is_rejected(self, tmp_path: Path) -> None:
        # `arxml_version: 4.2` in YAML is a float: Pydantic's own type error is the message.
        _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "arxml_version": 4.2})

        with pytest.raises(ValidationError):
            require_project(str(tmp_path / "swcraft.yaml"))

    def test_load_project_honors_the_field_too(self, tmp_path: Path) -> None:
        # The MCP server reaches the manifest through load_project().
        _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "arxml_version": "4.3.0"})

        project = load_project(str(tmp_path))

        assert project is not None
        assert project.resolved_arxml_version is ArxmlVersion.V4_3_0


# ---------------------------------------------------------------------------
# toolchain_version — the workstation pin
# ---------------------------------------------------------------------------


class TestToolchainVersionField:
    """``toolchain_version`` is optional, exact, and a *version* rather than a range.

    Only the grammar is asserted here; what a mismatch costs a command is ``test_cli.py``'s. A
    manifest without the field pins nothing.
    """

    def test_the_field_is_absent_by_default(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, MINIMAL_MANIFEST)

        project = require_project(str(tmp_path / "swcraft.yaml"))

        assert project.toolchain_version is None
        assert project.schema_version == "1.0"

    @pytest.mark.parametrize("value", ["0.7.0", "1.0", "0.7.0rc1", "1.2.3.post1", "2!1.0", "1.0+local.1"])
    def test_an_exact_version_is_read_back_as_written(self, tmp_path: Path, value: str) -> None:
        # Stored as written: the comparison parses it, the manifest keeps the author's spelling.
        _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "toolchain_version": value})

        project = require_project(str(tmp_path / "swcraft.yaml"))

        assert project.toolchain_version == value

    @pytest.mark.parametrize("value", [">=0.7", "==0.7.0", "0.7.*", "~=0.7.0", "latest", "0.7.0 or newer", ""])
    def test_a_range_or_a_word_is_refused_at_load_time(self, tmp_path: Path, value: str) -> None:
        # A specifier would pin nothing; refused where the field can be named.
        _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "toolchain_version": value})

        with pytest.raises(ValueError) as exc:
            require_project(str(tmp_path / "swcraft.yaml"))

        message = str(exc.value)
        assert "toolchain_version" in message
        # Read off SWCraft's own sentence, before pydantic's `[type=...]` diagnostics, which append
        # `input_value=<repr>` of their own. What it does not reach: `>=0.7` is quoted in the
        # refusal's own example clause, so that one parameter would pass a message that dropped
        # the value.
        sentence = message.split("[type=")[0]
        assert repr(value) in sentence, message

    def test_a_non_string_pin_is_rejected(self, tmp_path: Path) -> None:
        # `toolchain_version: 0.7` in YAML is a float: Pydantic's own type error is the message.
        _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "toolchain_version": 0.7})

        with pytest.raises(ValidationError):
            require_project(str(tmp_path / "swcraft.yaml"))

    def test_load_project_honors_the_field_too(self, tmp_path: Path) -> None:
        # The MCP server reaches the manifest through `load_project()`.
        _write_manifest(tmp_path, {**MINIMAL_MANIFEST, "toolchain_version": "0.7.0"})

        project = load_project(str(tmp_path))

        assert project is not None
        assert project.toolchain_version == "0.7.0"


class TestToolchainVersionsAgree:
    """The comparison the pin check makes: equality under PEP 440, never an ordering."""

    def test_the_same_string_agrees_with_itself(self) -> None:
        assert toolchain_versions_agree("0.7.0", "0.7.0")

    @pytest.mark.parametrize(
        ("pinned", "running"),
        [
            ("0.7", "0.7.0"),  # a trailing zero segment is padding, not a difference
            ("0.7.0.0", "0.7.0"),
            ("v0.7.0", "0.7.0"),  # PEP 440's own optional `v` prefix
            (" 0.7.0 ", "0.7.0"),  # PEP 440 permits surrounding whitespace
            ("0.7.0RC1", "0.7.0rc1"),
            ("0.7.0-rc1", "0.7.0rc1"),
            ("0.7.0c1", "0.7.0rc1"),  # `c` is `rc` after normalization
        ],
    )
    def test_two_spellings_of_one_release_agree(self, pinned: str, running: str) -> None:
        assert toolchain_versions_agree(pinned, running)

    @pytest.mark.parametrize(
        ("pinned", "running"),
        [
            ("0.7.0", "0.6.0"),
            ("0.7.0", "0.7.1"),
            ("0.7.0rc1", "0.7.0"),  # a pre-release is not its own final release
            ("0.7.0", "0.7.0.post1"),
            ("0.7.0", "0.7.0.dev1"),
            ("1!0.7.0", "0.7.0"),
            ("0.7.0+local", "0.7.0"),
        ],
    )
    def test_two_different_releases_do_not_agree(self, pinned: str, running: str) -> None:
        assert not toolchain_versions_agree(pinned, running)

    def test_something_that_is_not_a_version_never_agrees(self) -> None:
        # False rather than a raise: a caller holding two strings has no field to name.
        assert not toolchain_versions_agree(">=0.7", "0.7.0")
        assert not toolchain_versions_agree("0.7.0", "not a version")


class TestResolvedPathsDropWindowsExtendedPrefix:
    """``_resolved``: the ``resolved_*`` strings without ``\\\\?\\``.

    CPython's ``ntpath.realpath`` keeps Windows' extended-length prefix on a path that does not
    exist yet when a parallel process creates its parent between its two probes, and the
    containment check then refused a valid manifest. ``Path.resolve`` is patched to hand back the
    Windows spellings, so the cases hold on every host.
    """

    @pytest.mark.parametrize(
        ("spelled", "expected"),
        [
            ("\\\\?\\C:\\proj\\dist\\requirements.rst", "C:\\proj\\dist\\requirements.rst"),
            ("\\\\?\\UNC\\host\\share\\proj", "\\\\host\\share\\proj"),
            ("C:\\proj\\dist\\model.arxml", "C:\\proj\\dist\\model.arxml"),
            ("/home/user/proj/dist/model.arxml", "/home/user/proj/dist/model.arxml"),
        ],
    )
    def test_the_prefix_and_only_the_prefix_is_dropped(self, monkeypatch, spelled, expected):
        from swcraft_tools import project as project_module

        monkeypatch.setattr(Path, "resolve", lambda self, strict=False: Path(spelled))
        # `str(Path(...))` on both sides: the assertion is about the prefix, not the separator.
        assert project_module._resolved(Path("anything")) == str(Path(expected))

    def test_every_resolved_property_goes_through_the_helper(self, tmp_path, monkeypatch):
        """A prefixed answer from ``resolve()`` reaches none of the six strings."""
        from swcraft_tools import project as project_module

        manifest = _write_manifest(tmp_path)
        project = project_module.require_project(str(manifest))
        monkeypatch.setattr(Path, "resolve", lambda self, strict=False: Path("\\\\?\\" + "C:\\proj\\" + self.name))
        for name in (
            "resolved_project_root",
            "resolved_model_root",
            "resolved_dist_root",
            "resolved_views_root",
            "resolved_arxml_path",
            "resolved_needs_path",
        ):
            assert not getattr(project, name).startswith("\\\\?\\"), name


class TestAPathRefusalNamesTheFieldAndOnlyATerminalTheLocations:
    """The two path refusals reach an MCP client whole, so their shared form names the field alone.

    ``str()`` is what every tool answers with; the CLI prints ``render(full_path=True)``, naming both
    resolved locations.
    """

    @pytest.mark.parametrize(
        ("dist_root", "shared", "full_fragment"),
        [
            pytest.param(
                "../outside",
                "Manifest field 'dist_root' resolves outside the project root.",
                "which is outside the project root",
                id="outside",
            ),
            pytest.param(
                "model/dist",
                "Manifest field 'dist_root' overlaps 'model_root'. Generated paths must be outside the model",
                "which overlaps 'model_root'",
                id="overlaps",
            ),
        ],
    )
    def test_the_shared_form_and_the_full_form(
        self, tmp_path: Path, dist_root: str, shared: str, full_fragment: str
    ) -> None:
        manifest = _write_manifest(tmp_path)
        text = re.sub(r"(?m)^dist_root: .*$", f"dist_root: {dist_root}", manifest.read_text(encoding="utf-8"))
        manifest.write_text(text, encoding="utf-8", newline="\n")

        with pytest.raises(ValueError) as refused:
            require_project(str(manifest))

        assert str(refused.value).startswith(shared), str(refused.value)
        assert str(tmp_path) not in str(refused.value) and str(tmp_path.resolve()) not in str(refused.value)
        full = refused.value.render(full_path=True)
        assert full_fragment in full and str(tmp_path.resolve()) in full, full


class TestTheExperimentOutputDirectory:
    """``experiment_output_directory``: the configuration's ``output`` resolved inside the project, or refused.

    Seven refusals, each one sentence naming the key and the value as written, never a
    resolved location; the absolute rule reads both platforms' spellings on every host.
    """

    @staticmethod
    def _project(tmp_path: Path, model_root: str = "model") -> SwcraftProject:
        (tmp_path / model_root).mkdir(parents=True)
        manifest = tmp_path / "swcraft.yaml"
        manifest.write_text(
            yaml.safe_dump({**MINIMAL_MANIFEST, "model_root": model_root}), encoding="utf-8", newline="\n"
        )
        return require_project(str(manifest))

    @pytest.mark.parametrize("written", ["dist/experiments/x", "out"])
    def test_a_directory_inside_the_project_is_returned_resolved(self, tmp_path: Path, written: str) -> None:
        project = self._project(tmp_path)

        directory = experiment_output_directory(project, written)

        assert directory == (tmp_path / written).resolve()

    @pytest.mark.parametrize("written", ["/x", "C:/x", "C:\\x"])
    def test_an_absolute_path_on_either_platform_is_refused(self, tmp_path: Path, written: str) -> None:
        project = self._project(tmp_path)

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, written)

        assert str(refused.value) == (
            f"the configuration's 'output' is written as the absolute path '{written}': it must be written "
            "relative to the project directory, so that the configuration means the same on every machine "
            "that reads it"
        )

    @pytest.mark.parametrize("written", ["", "."])
    def test_the_project_directory_itself_is_refused(self, tmp_path: Path, written: str) -> None:
        project = self._project(tmp_path)

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, written)

        assert str(refused.value) == (
            f"the configuration's 'output' ('{written}') is the project directory itself: the report needs "
            "a directory of its own"
        )

    @pytest.mark.parametrize("written", ["..", "../x"])
    def test_a_directory_outside_the_project_is_refused(self, tmp_path: Path, written: str) -> None:
        project = self._project(tmp_path / "project")

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, written)

        assert str(refused.value) == (
            f"the configuration's 'output' ('{written}') resolves outside the project directory"
        )

    @pytest.mark.parametrize(
        ("model_root", "written"),
        [
            pytest.param("model", "model", id="the-model-root"),
            pytest.param("model", "Model/x", id="inside-it-in-another-case"),
            pytest.param("src/model", "src", id="a-parent-of-a-nested-model-root"),
        ],
    )
    def test_a_directory_overlapping_the_model_is_refused(self, tmp_path: Path, model_root: str, written: str) -> None:
        project = self._project(tmp_path, model_root)

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, written)

        assert str(refused.value) == (
            f"the configuration's 'output' ('{written}') overlaps 'model_root': the report would be written "
            "into, or around, the model that is the source of truth"
        )

    def test_an_existing_file_is_refused(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        (tmp_path / "notes.md").write_text("notes\n", encoding="utf-8", newline="\n")

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, "notes.md")

        assert str(refused.value) == "the configuration's 'output' ('notes.md') names a file, not a directory"

    def test_a_directory_below_a_file_is_refused_before_any_write(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, "swcraft.yaml/reports/carma")

        assert str(refused.value) == (
            "the configuration's 'output' ('swcraft.yaml/reports/carma') lies below 'swcraft.yaml', which is a "
            "file, not a directory"
        )
        assert not (tmp_path / "reports").exists()

    def test_a_nul_character_is_refused_in_a_sentence_of_its_own(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, "reports\0carma")

        assert str(refused.value) == (
            "the configuration's 'output' carries a NUL character, which no directory name can hold"
        )

    def test_a_symlink_into_the_model_is_refused_as_an_overlap(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        (tmp_path / "model" / "reports").mkdir()
        symlink_or_skip(tmp_path / "reports", tmp_path / "model" / "reports")

        with pytest.raises(ValueError, match=r"^the configuration's 'output' \('reports'\) overlaps 'model_root'"):
            experiment_output_directory(project, "reports")

    def test_a_symlink_out_of_the_project_is_refused_as_outside(self, tmp_path: Path) -> None:
        project = self._project(tmp_path / "project")
        (tmp_path / "elsewhere").mkdir()
        symlink_or_skip(tmp_path / "project" / "reports", tmp_path / "elsewhere")

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, "reports")

        assert str(refused.value) == "the configuration's 'output' ('reports') resolves outside the project directory"

    def test_no_refusal_names_a_resolved_location(self, tmp_path: Path) -> None:
        project = self._project(tmp_path / "project")

        for written in ("../x", "model", ""):
            with pytest.raises(ValueError) as refused:
                experiment_output_directory(project, written)
            assert str(tmp_path) not in str(refused.value)
            assert str(tmp_path.resolve()) not in str(refused.value)

    # -- refusal 6: '.git' ------------------------------------------------------

    @staticmethod
    def _git_refusal(written: str) -> str:
        return (
            f"the configuration's 'output' ('{written}') names or lies inside '.git', which belongs to git: write "
            "the report to a directory of the project's own"
        )

    @pytest.mark.parametrize("written", [".git", ".git/hooks", "sub/.git/x", ".GIT", "reports/../.git"])
    def test_a_path_naming_or_inside_git_is_refused(self, tmp_path: Path, written: str) -> None:
        """Any component that casefolds to ``.git``, on a case-sensitive file system too."""
        project = self._project(tmp_path)
        (tmp_path / ".git" / "hooks").mkdir(parents=True)

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, written)

        assert str(refused.value) == self._git_refusal(written)
        assert str(tmp_path) not in str(refused.value) and str(tmp_path.resolve()) not in str(refused.value)

    def test_a_worktree_pointer_file_named_git_gets_the_git_sentence_and_not_the_file_one(self, tmp_path: Path) -> None:
        """Refusal 6 runs before refusal 5, so a linked worktree's ``.git`` file is git's, not "a file"."""
        project = self._project(tmp_path)
        (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8", newline="\n")

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, ".git")

        assert str(refused.value) == self._git_refusal(".git")

    def test_a_symlink_resolving_into_git_is_refused(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        (tmp_path / ".git" / "info").mkdir(parents=True)
        symlink_or_skip(tmp_path / "reports", tmp_path / ".git" / "info")

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, "reports")

        assert str(refused.value) == self._git_refusal("reports")

    def test_a_git_directory_outside_the_project_is_refusal_3s(self, tmp_path: Path) -> None:
        """An outside path cannot be made relative to the project: it is the outside sentence, never git's."""
        project = self._project(tmp_path / "project")

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, "../.git")

        assert str(refused.value) == "the configuration's 'output' ('../.git') resolves outside the project directory"

    def test_a_name_merely_containing_git_is_accepted(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)

        for written in (".github/reports", "git", ".gitlab", "x.git"):
            assert experiment_output_directory(project, written) == (tmp_path / written).resolve()

    # -- refusal 7: a report no experiment run wrote ---------------------------

    @staticmethod
    def _foreign_refusal(written: str, *names: str) -> str:
        if len(names) == 1:
            return (
                f"the configuration's 'output' ('{written}') already holds '{names[0]}', which no experiment run "
                "wrote: the run would replace it. Choose another directory, or move that file away."
            )
        return (
            f"the configuration's 'output' ('{written}') already holds "
            + " and ".join(f"'{name}'" for name in names)
            + ", which no experiment run wrote: the run would replace them. Choose another directory, or move "
            "those files away."
        )

    def test_a_hand_written_report_alone_is_refused(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "report.md").write_text("my own notes\n", encoding="utf-8", newline="\n")

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, "docs")

        assert str(refused.value) == self._foreign_refusal("docs", "report.md")
        assert str(tmp_path) not in str(refused.value) and str(tmp_path.resolve()) not in str(refused.value)
        assert (tmp_path / "docs" / "report.md").read_text(encoding="utf-8") == "my own notes\n"

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("not json at all\n", id="not-json"),
            pytest.param("[1, 2]\n", id="a-list"),
            pytest.param(json.dumps({"schemaVersion": 1}), id="no-kind"),
            pytest.param(json.dumps({"schemaVersion": True, "kind": "allocation"}), id="schemaVersion-true"),
            pytest.param(json.dumps({"schemaVersion": "1", "kind": "allocation"}), id="schemaVersion-a-string"),
            pytest.param(json.dumps({"schemaVersion": 1, "kind": ""}), id="an-empty-kind"),
            pytest.param(json.dumps({"schemaVersion": 1, "kind": 3}), id="a-kind-not-a-string"),
        ],
    )
    def test_a_result_json_no_run_wrote_is_refused(self, tmp_path: Path, text: str) -> None:
        project = self._project(tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "result.json").write_text(text, encoding="utf-8", newline="\n")

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, "docs")

        assert str(refused.value) == self._foreign_refusal("docs", "result.json")

    def test_a_result_json_that_is_not_utf8_is_refused(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "result.json").write_bytes(b'{"schemaVersion": 1, "kind": "\xff"}')

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, "docs")

        assert str(refused.value) == self._foreign_refusal("docs", "result.json")

    def test_both_foreign_files_are_named_report_first(self, tmp_path: Path) -> None:
        """A run-shaped ``result.json`` is what makes a ``report.md`` a run's; a foreign one does not."""
        project = self._project(tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "report.md").write_text("mine\n", encoding="utf-8", newline="\n")
        (tmp_path / "docs" / "result.json").write_text("{}\n", encoding="utf-8", newline="\n")

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, "docs")

        assert str(refused.value) == self._foreign_refusal("docs", "report.md", "result.json")

    def test_a_dangling_report_symlink_is_foreign(self, tmp_path: Path) -> None:
        """Presence is ``os.path.lexists``: the write would follow the link, so it counts."""
        project = self._project(tmp_path)
        (tmp_path / "docs").mkdir()
        symlink_or_skip(tmp_path / "docs" / "report.md", tmp_path / "nowhere.md")

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, "docs")

        assert str(refused.value) == self._foreign_refusal("docs", "report.md")

    def test_a_fifo_named_result_json_is_foreign_without_being_opened(self, tmp_path: Path) -> None:
        """A FIFO is judged without being opened, which would block forever."""
        make_fifo = getattr(os, "mkfifo", None)  # absent on Windows, which has no named pipe in a directory
        if make_fifo is None:  # pragma: no cover - platform dependent
            pytest.skip("this platform makes no FIFO in a directory")
        project = self._project(tmp_path)
        (tmp_path / "docs").mkdir()
        try:
            make_fifo(tmp_path / "docs" / "result.json")
        except OSError as exc:  # pragma: no cover - filesystem dependent
            pytest.skip(f"this filesystem makes no FIFO: {exc}")
        outcome: list[BaseException | Path] = []

        def run() -> None:
            try:
                outcome.append(experiment_output_directory(project, "docs"))
            except BaseException as exc:  # noqa: BLE001 - handed back to the test thread
                outcome.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=30)

        assert not worker.is_alive(), "the check blocked reading the FIFO"
        assert isinstance(outcome[0], ValueError), outcome[0]
        assert str(outcome[0]) == self._foreign_refusal("docs", "result.json")

    def test_a_directory_named_result_json_is_foreign(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        (tmp_path / "docs" / "result.json").mkdir(parents=True)

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, "docs")

        assert str(refused.value) == self._foreign_refusal("docs", "result.json")

    def test_a_result_json_linked_to_a_runs_file_is_read_through_the_link(self, tmp_path: Path) -> None:
        """A symlink is followed: what it names is judged, and a regular run's file is a run's."""
        project = self._project(tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "kept.json").write_text(
            json.dumps({"schemaVersion": 1, "kind": "allocation"}), encoding="utf-8", newline="\n"
        )
        symlink_or_skip(tmp_path / "docs" / "result.json", tmp_path / "kept.json")

        assert experiment_output_directory(project, "docs") == (tmp_path / "docs").resolve()

    def test_a_runs_result_json_alone_is_accepted(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "result.json").write_text(
            json.dumps({"schemaVersion": 1, "kind": "allocation"}), encoding="utf-8", newline="\n"
        )

        assert experiment_output_directory(project, "docs") == (tmp_path / "docs").resolve()

    def test_refusal_3_runs_before_refusal_7(self, tmp_path: Path) -> None:
        project = self._project(tmp_path / "project")
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "elsewhere" / "report.md").write_text("mine\n", encoding="utf-8", newline="\n")

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, "../elsewhere")

        assert str(refused.value) == (
            "the configuration's 'output' ('../elsewhere') resolves outside the project directory"
        )

    def test_refusal_4_runs_before_refusal_7(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        (tmp_path / "model" / "reports").mkdir()
        (tmp_path / "model" / "reports" / "report.md").write_text("mine\n", encoding="utf-8", newline="\n")

        with pytest.raises(
            ValueError, match=r"^the configuration's 'output' \('model/reports'\) overlaps 'model_root'"
        ):
            experiment_output_directory(project, "model/reports")

    def test_refusal_6_runs_before_refusal_7(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        (tmp_path / ".git" / "reports").mkdir(parents=True)
        (tmp_path / ".git" / "reports" / "report.md").write_text("mine\n", encoding="utf-8", newline="\n")

        with pytest.raises(ValueError) as refused:
            experiment_output_directory(project, ".git/reports")

        assert str(refused.value) == self._git_refusal(".git/reports")

    def test_a_directory_that_does_not_exist_is_accepted(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)

        assert experiment_output_directory(project, "reports/new") == (tmp_path / "reports" / "new").resolve()

    def test_an_existing_empty_directory_is_accepted(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        (tmp_path / "reports").mkdir()

        assert experiment_output_directory(project, "reports") == (tmp_path / "reports").resolve()

    def test_a_directory_holding_other_files_only_is_accepted(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "notes.md").write_text("mine\n", encoding="utf-8", newline="\n")
        (tmp_path / "docs" / "proposals.json").write_text("[]\n", encoding="utf-8", newline="\n")

        assert experiment_output_directory(project, "docs") == (tmp_path / "docs").resolve()

    @pytest.mark.parametrize("kind", ["allocation", "trace-search", "a kind no module declares"])
    def test_a_runs_result_json_with_its_report_is_accepted_whatever_its_kind(self, tmp_path: Path, kind: str) -> None:
        """Any kind counts: ``project.py`` imports no kind, and reads the kind only to tell a run's file."""
        project = self._project(tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "result.json").write_text(
            json.dumps({"schemaVersion": 1, "kind": kind}), encoding="utf-8", newline="\n"
        )
        (tmp_path / "docs" / "report.md").write_text("# a run's report\n", encoding="utf-8", newline="\n")

        assert experiment_output_directory(project, "docs") == (tmp_path / "docs").resolve()


class TestTheExperimentDefaultDirectory:
    """``experiment_default_directory``: ``<dist_root>/<relative>`` resolved, judged as a configured ``output`` is."""

    @staticmethod
    def _project(tmp_path: Path) -> SwcraftProject:
        return TestTheExperimentOutputDirectory._project(tmp_path)

    def test_the_default_is_returned_resolved_under_the_dist_root(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)

        directory = experiment_default_directory(project, "experiments/allocation")

        assert directory == Path(project.resolved_dist_root) / "experiments" / "allocation"

    def test_a_symlink_below_the_dist_root_out_of_the_project_is_refused(self, tmp_path: Path) -> None:
        project = self._project(tmp_path / "project")
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "project" / "dist").mkdir()
        symlink_or_skip(tmp_path / "project" / "dist" / "experiments", tmp_path / "elsewhere")

        with pytest.raises(ValueError) as refused:
            experiment_default_directory(project, "experiments/allocation")

        assert str(refused.value) == (
            "the report directory '<dist_root>/experiments/allocation' resolves outside the project directory"
        )

    def test_a_symlink_below_the_dist_root_into_the_model_is_refused(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        (tmp_path / "dist").mkdir()
        symlink_or_skip(tmp_path / "dist" / "experiments", tmp_path / "model")

        with pytest.raises(
            ValueError, match=r"^the report directory '<dist_root>/experiments/x' overlaps 'model_root'"
        ):
            experiment_default_directory(project, "experiments/x")

    def test_a_hand_written_report_in_the_default_directory_is_not_checked(self, tmp_path: Path) -> None:
        """Refusals 6 and 7 are the configured ``output``'s alone: the default is unchanged."""
        project = self._project(tmp_path)
        (tmp_path / "dist" / "experiments" / "allocation").mkdir(parents=True)
        (tmp_path / "dist" / "experiments" / "allocation" / "report.md").write_text(
            "mine\n", encoding="utf-8", newline="\n"
        )

        directory = experiment_default_directory(project, "experiments/allocation")

        assert directory == Path(project.resolved_dist_root) / "experiments" / "allocation"

    def test_a_file_on_the_way_is_refused(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)
        (tmp_path / "dist").mkdir()
        (tmp_path / "dist" / "experiments").write_text("mine\n", encoding="utf-8", newline="\n")

        with pytest.raises(ValueError) as refused:
            experiment_default_directory(project, "experiments/allocation")

        assert str(refused.value) == (
            "the report directory '<dist_root>/experiments/allocation' lies below 'dist/experiments', which is a "
            "file, not a directory"
        )


class TestTheProjectRelativeName:
    """``project_relative_name``: what an experiment's record names a file or a directory by, or ``None``."""

    @staticmethod
    def _project(tmp_path: Path) -> SwcraftProject:
        return TestTheExperimentOutputDirectory._project(tmp_path)

    def test_a_path_inside_the_project_is_named_relative_and_posix(self, tmp_path: Path) -> None:
        project = self._project(tmp_path)

        assert project_relative_name(project, tmp_path / "experiments" / "a b.yaml") == "experiments/a b.yaml"

    def test_a_relative_path_resolves_against_the_current_directory(self, tmp_path: Path, monkeypatch) -> None:
        project = self._project(tmp_path)
        (tmp_path / "experiments").mkdir()
        monkeypatch.chdir(tmp_path / "experiments")

        assert project_relative_name(project, "rebalance.yaml") == "experiments/rebalance.yaml"

    def test_a_path_outside_the_project_has_no_name(self, tmp_path: Path) -> None:
        project = self._project(tmp_path / "project")

        assert project_relative_name(project, tmp_path / "outside.yaml") is None
        # A sibling whose name extends the project directory's is outside it too.
        assert project_relative_name(project, tmp_path / "project-other" / "x.yaml") is None

    def test_a_symlink_is_named_by_what_it_resolves_to(self, tmp_path: Path) -> None:
        project = self._project(tmp_path / "project")
        (tmp_path / "elsewhere").mkdir()
        symlink_or_skip(tmp_path / "project" / "linked", tmp_path / "elsewhere")

        assert project_relative_name(project, tmp_path / "project" / "linked" / "x.yaml") is None
