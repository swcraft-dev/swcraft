"""Tests for MCP validate and build tools (TDD)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from swcraft_mcp.server import (
    build,
    export_component,
    validate,
)
from swcraft_tools import arxml_validation, validator

# ---------------------------------------------------------------------------
# The hardware the `model_root` fixture deliberately does not write
# ---------------------------------------------------------------------------


def _seed_hardware(model_root: str) -> None:
    """One processor and nothing else: `check-model`'s mandatory layer, with no budget to analyse."""
    hardware = Path(model_root) / "hardware"
    hardware.mkdir(parents=True, exist_ok=True)
    (hardware / "CPU.yaml").write_text(
        yaml.safe_dump({"name": "CPU", "kind": "processor"}, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _set_links(model_root: str, links: list[str], *, extra: str | None = None) -> None:
    """Make exactly the components named in *links* satisfy the fixture's one requirement, ``REQ_001``.

    *extra*, another ``satisfies`` member, is added to the first named component.
    """
    for path in sorted((Path(model_root) / "swc").rglob("*.yaml"), key=lambda candidate: candidate.as_posix()):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data.pop("satisfies", None)
        if data["name"] in links:
            data["satisfies"] = ["REQ_001@1", *([extra] if extra and data["name"] == links[0] else [])]
        path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")


def _make_model_consistent(model_root) -> None:
    """Everything the ``model_root`` fixture leaves for a case that needs a clean model.

    The processor the fixture omits on purpose (without one the model has ``hardware.no-processor``),
    and ``SpeedCtrlSWC``'s link: the Sphinx-Needs export writes a component's need only when it is
    linked, which the need-id collision case needs.
    """
    _set_links(model_root, ["SpeedSensorSWC", "SpeedCtrlSWC"])
    _seed_hardware(model_root)


# ---------------------------------------------------------------------------
# The connector checks are validate's
# ---------------------------------------------------------------------------
#
# A connector's two ends are judged when its change set is staged, and the whole-model connector
# check is one of the checks `validate` runs.


def _break_the_connector(model_root: str) -> None:
    """Point the fixture's one connector at a port its requirer does not declare."""
    path = Path(model_root) / "connectors" / "Speed_to_Ctrl.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["to"]["port"] = "rGhost"
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")


def test_validate_reports_every_finding_of_the_connector_check(model_root):
    _make_model_consistent(model_root)
    _break_the_connector(model_root)
    issues = validator.check_connector_consistency(model_root).issues
    assert issues, "the broken connector raised no connector finding"

    result = validate()

    assert result["success"] is False
    messages = [error["message"] for error in result["errors"]]
    for issue in issues:
        assert issue in messages, (issue, messages)


def test_validate_reports_no_connector_finding_on_a_consistent_model(model_root):
    _make_model_consistent(model_root)
    assert validator.check_connector_consistency(model_root).issues == []

    result = validate()

    assert result["success"] is True, result["errors"]
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# build — standalone (no moon workspace) direct-call path
# ---------------------------------------------------------------------------


def test_build_uses_env_override_command(monkeypatch):
    monkeypatch.setenv("SWCRAFT_MCP_BUILD_COMMAND", "python -m swcraft_tools.cli validate --project x.yaml")
    with patch("swcraft_tools.builder.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        build()
    called_args = mock_run.call_args[0][0]
    assert called_args == ["python", "-m", "swcraft_tools.cli", "validate", "--project", "x.yaml"]


def test_build_env_override_failure_parses_errors(monkeypatch):
    monkeypatch.setenv("SWCRAFT_MCP_BUILD_COMMAND", "moon run :build")
    bad_output = "tools:validate-arxml | INVALID (structural mode): dist/model.arxml\n"
    with patch("swcraft_tools.builder.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=bad_output, stderr="")
        result = build()
    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert result["errors"][0]["message"] == "INVALID (structural mode): dist/model.arxml"
    assert result["errors"][0]["path"] == "dist/model.arxml"


def test_build_standalone_valid_model_generates_all_artifacts(model_root):
    _make_model_consistent(model_root)

    result = build()
    assert result["success"] is True
    assert result["errors"] == []
    project_root = Path(model_root).parent
    assert (project_root / "dist" / "model.arxml").exists()
    assert (project_root / "views" / "index.html").exists()
    assert (project_root / "views" / "ibd-PowertrainCtrl.svg").exists()
    assert not (project_root / "views" / "model.c4").exists()
    assert (project_root / "dist" / "needs.json").is_file()
    assert not (project_root / "dist" / "needs").exists()
    assert result["output"].endswith(
        "\nDiagrams generated: views (3 views, report views/report.txt)\nneeds.json generated: dist/needs.json"
    )


# ---------------------------------------------------------------------------
# The needs.json export's refusals and its storage name, through `build`
# ---------------------------------------------------------------------------


def test_build_answers_a_need_id_collision_with_the_layer_and_name_sentence(model_root, tmp_path):
    """A requirement id equal to an element's need id refuses the export, by layer and name, with no path.

    The tool builds the ``needs.json`` text before its first write, so the refusal writes nothing at all.
    """
    _make_model_consistent(model_root)
    (Path(model_root) / "requirements" / "SWC-SpeedCtrlSWC.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "SWC-SpeedCtrlSWC",
                "title": "Named like a need",
                "description": "",
                "req_type": "req",
                "version": 1,
                "source": "architecture",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )

    result = build()

    assert result["success"] is False
    assert [error["message"] for error in result["errors"]] == [
        "the id SWC-SpeedCtrlSWC would be taken by more than one need: requirement SWC-SpeedCtrlSWC, "
        "element SWC-SpeedCtrlSWC"
    ]
    assert str(tmp_path) not in repr(result)
    assert tmp_path.as_posix() not in repr(result)
    assert list((tmp_path / "dist").iterdir()) == []
    assert not (tmp_path / "views").exists()


def test_build_answers_a_needs_output_naming_a_directory_without_naming_a_location(model_root, tmp_path):
    """An ``outputs.needs`` naming an existing directory fails at the write with the OS's reason, path-free.

    A directory is no previous file, so the build's removal leaves it as it was.
    """
    _make_model_consistent(model_root)
    (tmp_path / "swcraft.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "test-project",
                "schema_version": "1.0",
                "model_root": "model",
                "dist_root": "dist",
                "views_root": "views",
                "outputs": {"arxml": "dist/model.arxml", "needs": "dist/needs"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )
    folder = tmp_path / "dist" / "needs"
    folder.mkdir(parents=True)
    (folder / "index.rst").write_text("kept\n", encoding="utf-8", newline="\n")

    result = build()

    assert result["success"] is False
    (error,) = result["errors"]
    assert "dist/needs" in error["message"], error
    assert str(tmp_path) not in repr(result)
    assert tmp_path.as_posix() not in repr(result)
    assert sorted(path.name for path in folder.iterdir()) == ["index.rst"]


def test_build_names_an_absolute_needs_output_by_its_storage_name(model_root, tmp_path):
    """An absolute `outputs.needs` inside the project is allowed; the reply names it relative to the project."""
    _make_model_consistent(model_root)
    absolute = (tmp_path / "published" / "needs.json").as_posix()
    (tmp_path / "swcraft.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "test-project",
                "schema_version": "1.0",
                "model_root": "model",
                "dist_root": "dist",
                "views_root": "views",
                "outputs": {"arxml": "dist/model.arxml", "needs": absolute},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )

    result = build()

    assert result["success"] is True, result["errors"]
    assert result["output"].endswith("\nneeds.json generated: published/needs.json")
    assert absolute not in repr(result)
    assert str(tmp_path) not in repr(result)
    assert (tmp_path / "published" / "needs.json").is_file()


def test_build_refuses_a_link_to_a_dotted_id_without_naming_a_location(model_root, tmp_path):
    """A `satisfies` naming an id holding `.` refuses the export by its sentence alone.

    A previous `needs.json` is removed, as `swcraft generate needs` removes it.
    """
    _make_model_consistent(model_root)
    previous = tmp_path / "dist" / "needs.json"
    previous.parent.mkdir(parents=True, exist_ok=True)
    previous.write_bytes(b"{}\n")
    (Path(model_root) / "requirements" / "SR.3.1.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "SR.3.1",
                "title": "An incoming requirement",
                "description": "",
                "req_type": "req",
                "version": 1,
                "source": "external",
                "baseline": "2.3",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )
    swc_file = next((Path(model_root) / "swc").rglob("SpeedCtrlSWC.yaml"))
    data = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
    data["satisfies"] = [*data.get("satisfies", []), "SR.3.1@1"]
    swc_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")

    result = build()

    assert result["success"] is False
    assert [error["message"] for error in result["errors"]] == [
        "element SWC-SpeedCtrlSWC cannot be published: its satisfies names SR.3.1, an id holding '.', which "
        "Sphinx-Needs reads as an id and a part under every configuration, so the link would name another need or "
        "none; SWCraft does not support such an id in a published link"
    ]
    assert str(tmp_path) not in repr(result)
    assert tmp_path.as_posix() not in repr(result)
    assert not (tmp_path / "dist" / "needs.json").exists()
    # The composer's warning naming the site stays beside the refusal.
    assert any("holds '.'" in warning["message"] for warning in result["warnings"]), result["warnings"]


def test_build_reports_a_layout_that_cannot_run_without_naming_a_location(model_root, monkeypatch, tmp_path):
    """No Node.js anywhere: the refusal says so, and names neither the interpreter's directory nor a Node."""
    _make_model_consistent(model_root)
    empty = tmp_path / "no-node-here"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    result = build()

    assert result["success"] is False
    (error,) = result["errors"]
    assert error["message"].startswith("Node.js was not found"), error
    assert "/" not in error["message"] and "\\" not in error["message"], error
    assert not (Path(model_root).parent / "views").exists()


def test_build_standalone_broken_model_returns_errors_without_stale_artifacts(model_root):
    """A model error (no processor) fails the build before any generator runs, with the warning beside it."""
    _unallocate_the_requirement(model_root)
    with (
        patch("swcraft_tools.writer.generate_arxml") as mock_arxml,
        patch("swcraft_tools.writer.generate_diagrams") as mock_diagrams,
        patch("swcraft_tools.writer.write_needs_json") as mock_needs,
    ):
        result = build()
    assert result["success"] is False
    assert len(result["errors"]) > 0
    assert all("moon" not in e["message"].lower() for e in result["errors"])
    assert not any(_THE_UNALLOCATED_REQUIREMENT in e["message"] for e in result["errors"])
    assert any(_THE_UNALLOCATED_REQUIREMENT in w["message"] for w in result["warnings"])
    mock_arxml.assert_not_called()
    mock_diagrams.assert_not_called()
    mock_needs.assert_not_called()


def test_build_standalone_prior_success_then_broken_model_reports_failure(model_root):
    """A stale dist/model.arxml from an earlier build is not reported as a fresh success."""
    _make_model_consistent(model_root)

    first = build()
    assert first["success"] is True
    project_root = Path(model_root).parent
    arxml_path = project_root / "dist" / "model.arxml"
    assert arxml_path.exists()
    mtime_after_first_build = arxml_path.stat().st_mtime

    # A link naming an undeclared requirement is an error; removing a link would break nothing.
    _set_links(model_root, ["SpeedSensorSWC", "SpeedCtrlSWC"], extra="REQ_GHOST@1")

    second = build()
    assert second["success"] is False
    assert len(second["errors"]) > 0
    assert any("REQ_GHOST" in e["message"] for e in second["errors"])
    assert second["warnings"] == []
    assert arxml_path.exists()
    assert arxml_path.stat().st_mtime == mtime_after_first_build


def test_build_generator_raises_returns_error_dict_not_raise(model_root):
    _make_model_consistent(model_root)

    with patch("swcraft_tools.writer.generate_diagrams", side_effect=ValueError("boom: unresolvable reference")):
        result = build()
    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert result["errors"][0]["path"] == ""
    assert "boom" in result["errors"][0]["message"]


def test_build_bad_manifest_returns_error_dict_not_raise(monkeypatch, tmp_path):
    """A bad SWCRAFT_MANIFEST returns a clean error dict, as validate() does, and never raises."""
    bad_manifest = tmp_path / "does-not-exist.yaml"
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(bad_manifest))

    result = build()
    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert result["errors"][0]["path"] == ""
    assert result["errors"][0]["message"] != ""


def test_build_env_override_bad_manifest_returns_error_dict_not_raise(monkeypatch, tmp_path):
    """The SWCRAFT_MCP_BUILD_COMMAND branch also returns a clean error dict for a bad SWCRAFT_MANIFEST."""
    monkeypatch.setenv("SWCRAFT_MCP_BUILD_COMMAND", "moon run :build")
    bad_manifest = tmp_path / "does-not-exist.yaml"
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(bad_manifest))

    result = build()
    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert result["errors"][0]["path"] == ""
    assert result["errors"][0]["message"] != ""


# ---------------------------------------------------------------------------
# validate — standalone (no moon workspace) direct-call path
# ---------------------------------------------------------------------------


def test_validate_standalone_broken_model_returns_errors(model_root):
    """Without a moon workspace, a model error (no processor) is a normal failure result with the warning beside it."""
    _unallocate_the_requirement(model_root)
    result = validate()
    assert result["success"] is False
    assert len(result["errors"]) > 0
    assert all("moon" not in e["message"].lower() for e in result["errors"])
    assert not any(_THE_UNALLOCATED_REQUIREMENT in e["message"] for e in result["errors"])
    assert any(_THE_UNALLOCATED_REQUIREMENT in w["message"] for w in result["warnings"])
    assert all(e["path"] == "" for e in result["errors"])


def test_validate_standalone_valid_model_generates_arxml(model_root):
    _make_model_consistent(model_root)

    result = validate()
    assert result["success"] is True
    assert result["errors"] == []
    arxml_path = Path(model_root).parent / "dist" / "model.arxml"
    assert arxml_path.exists()


def test_validate_env_override_uses_run_build(monkeypatch, model_root):
    monkeypatch.setenv(
        "SWCRAFT_MCP_VALIDATE_COMMAND",
        "python -m swcraft_tools.cli check-model --project x.yaml",
    )
    with patch("swcraft_tools.builder.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="VALID", stderr="")
        result = validate()
    called_args = mock_run.call_args[0][0]
    assert called_args == [
        "python",
        "-m",
        "swcraft_tools.cli",
        "check-model",
        "--project",
        "x.yaml",
    ]
    assert result["success"] is True


def test_validate_env_override_failure_parses_errors(monkeypatch, model_root):
    monkeypatch.setenv("SWCRAFT_MCP_VALIDATE_COMMAND", "moon run tools:validate-arxml")
    bad_output = "tools:validate-arxml | INVALID (XSD mode): dist/model.arxml\n"
    with patch("swcraft_tools.builder.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=bad_output, stderr="")
        result = validate()
    assert result["success"] is False
    assert result["errors"][0]["message"] == "INVALID (XSD mode): dist/model.arxml"


def test_validate_bad_manifest_returns_error_dict_not_raise(monkeypatch, tmp_path):
    bad_manifest = tmp_path / "does-not-exist.yaml"
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(bad_manifest))

    result = validate()
    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert result["errors"][0]["path"] == ""
    assert result["errors"][0]["message"] != ""


def test_validate_env_override_bad_manifest_returns_error_dict_not_raise(monkeypatch, tmp_path):
    """The SWCRAFT_MCP_VALIDATE_COMMAND branch also returns a clean error dict for a bad SWCRAFT_MANIFEST."""
    monkeypatch.setenv("SWCRAFT_MCP_VALIDATE_COMMAND", "moon run tools:validate-arxml")
    bad_manifest = tmp_path / "does-not-exist.yaml"
    monkeypatch.setenv("SWCRAFT_MANIFEST", str(bad_manifest))

    result = validate()
    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert result["errors"][0]["path"] == ""
    assert result["errors"][0]["message"] != ""


def test_validate_generator_raises_returns_error_dict_not_raise(model_root):
    """A raising generate_arxml() returns an error dict, as in build()."""
    _make_model_consistent(model_root)

    with patch("swcraft_tools.writer.generate_arxml", side_effect=ValueError("boom: unresolvable reference")):
        result = validate()
    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert result["errors"][0]["path"] == ""
    assert "boom" in result["errors"][0]["message"]


# ---------------------------------------------------------------------------
# AUTOSAR version
#
# build() and validate() generate ARXML in-process, so they must target the same AUTOSAR version
# the CLI does. Asserted on the generated file's xsi:schemaLocation, the whole observable
# difference between the two versions for the element subset SWCraft emits.
# ---------------------------------------------------------------------------


def _set_manifest_arxml_version(model_root, version: str) -> None:
    """Declare ``arxml_version`` in the test project's manifest; before the first tool call only (it is cached)."""
    manifest_path = Path(model_root).parent / "swcraft.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["arxml_version"] = version
    manifest_path.write_text(yaml.safe_dump(manifest, default_flow_style=False, sort_keys=False), encoding="utf-8")


def test_build_honors_manifest_arxml_version(model_root):
    _make_model_consistent(model_root)
    _set_manifest_arxml_version(model_root, "4.3.0")

    result = build()
    assert result["success"] is True, result["errors"]
    arxml = (Path(model_root).parent / "dist" / "model.arxml").read_text(encoding="utf-8")
    assert "AUTOSAR_4-3-0.xsd" in arxml
    assert "AUTOSAR_4-2-2.xsd" not in arxml
    assert "4.3.0" in result["output"]


def test_validate_honors_manifest_arxml_version(model_root):
    _make_model_consistent(model_root)
    _set_manifest_arxml_version(model_root, "4.3.0")

    result = validate()
    assert result["success"] is True, result["errors"]
    arxml = (Path(model_root).parent / "dist" / "model.arxml").read_text(encoding="utf-8")
    assert "AUTOSAR_4-3-0.xsd" in arxml
    assert "AUTOSAR_4-2-2.xsd" not in arxml
    assert "4.3.0" in result["output"]


def test_build_without_manifest_arxml_version_targets_4_2_2(model_root):
    """A manifest with no ``arxml_version`` produces 4.2.2 output."""
    _make_model_consistent(model_root)

    result = build()
    assert result["success"] is True, result["errors"]
    arxml = (Path(model_root).parent / "dist" / "model.arxml").read_text(encoding="utf-8")
    assert "AUTOSAR_4-2-2.xsd" in arxml
    assert "AUTOSAR_4-3-0.xsd" not in arxml
    assert "4.2.2" in result["output"]


def test_validate_without_manifest_arxml_version_targets_4_2_2(model_root):
    _make_model_consistent(model_root)

    result = validate()
    assert result["success"] is True, result["errors"]
    arxml = (Path(model_root).parent / "dist" / "model.arxml").read_text(encoding="utf-8")
    assert "AUTOSAR_4-2-2.xsd" in arxml
    assert "AUTOSAR_4-3-0.xsd" not in arxml
    assert "4.2.2" in result["output"]


def test_build_unsupported_manifest_arxml_version_returns_error_dict(model_root):
    """An unsupported manifest value fails at load time, as build()'s normal error dict."""
    _make_model_consistent(model_root)
    _set_manifest_arxml_version(model_root, "4.9.9")

    result = build()
    assert result["success"] is False
    assert len(result["errors"]) == 1
    # The sentence names the field and the kind of error, never the value.
    message = result["errors"][0]["message"]
    assert "field `arxml_version` is invalid" in message
    assert "4.9.9" not in message
    assert not (Path(model_root).parent / "dist" / "model.arxml").exists()


def test_validate_unsupported_manifest_arxml_version_returns_error_dict(model_root):
    _make_model_consistent(model_root)
    _set_manifest_arxml_version(model_root, "4.9.9")

    result = validate()
    assert result["success"] is False
    assert len(result["errors"]) == 1
    # The sentence names the field and the kind of error, never the value.
    message = result["errors"][0]["message"]
    assert "field `arxml_version` is invalid" in message
    assert "4.9.9" not in message
    assert not (Path(model_root).parent / "dist" / "model.arxml").exists()


# ---------------------------------------------------------------------------
# `[constr_2006]` is judged at the manifest's version
#
# Both tools hand the composer `project.resolved_arxml_version`; one that dropped it would judge at
# 4.2.2 and refuse a 4.3.0 model whose asynchronous call has no result point. The `access_forms`
# fixture replaces the conftest's model, its polled call without its `poll`.
# ---------------------------------------------------------------------------

_ACCESS_FORMS = (
    Path(__file__).resolve().parents[3] / "packages" / "tools" / "tests" / "fixtures" / "access_forms" / "model"
)


def _async_call_without_a_result(model_root) -> None:
    import shutil

    shutil.rmtree(model_root)
    shutil.copytree(_ACCESS_FORMS, model_root)
    controller = Path(model_root) / "swc" / "chassis" / "BrakeController.yaml"
    document = yaml.safe_load(controller.read_text(encoding="utf-8"))
    (compute,) = [runnable for runnable in document["runnables"] if runnable["name"] == "ComputeBrakeTorque"]
    (call,) = [call for call in compute["calls"] if (call["port"], call["operation"]) == ("rCalibration", "ReadBlock")]
    del call["poll"]
    controller.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


@pytest.mark.parametrize("tool", [validate, build], ids=["validate", "build"])
@pytest.mark.parametrize(("version", "accepted"), [("4.3.0", True), (None, False)], ids=["4.3.0", "default"])
def test_an_asynchronous_call_without_a_result_is_judged_at_the_manifest_s_version(
    tmp_path, tool, version, accepted
) -> None:
    model_root = tmp_path / "model"
    _async_call_without_a_result(model_root)
    if version is not None:
        _set_manifest_arxml_version(model_root, version)

    result = tool()

    messages = [error["message"] for error in result["errors"]]
    assert result["success"] is accepted, messages
    assert any("nothing fetches its result" in message for message in messages) is not accepted


# ---------------------------------------------------------------------------
# The ARXML gate
#
# Like `swcraft validate`, validate()/build() run check_arxml() on the file they generated: these
# tools drive the agent's self-correction loop, so an ARXML the CLI rejects must not read as a pass.
# ---------------------------------------------------------------------------


def test_validate_runs_the_arxml_check_on_the_file_it_generated(model_root):
    _make_model_consistent(model_root)

    with patch("swcraft_tools.arxml_validation.check_arxml", wraps=arxml_validation.check_arxml) as spy:
        result = validate()

    assert result["success"] is True, result["errors"]
    assert spy.call_count == 1
    assert spy.call_args.args[0] == str(Path(model_root).parent / "dist" / "model.arxml")


def test_build_runs_the_arxml_check_on_the_file_it_generated(model_root):
    _make_model_consistent(model_root)

    with patch("swcraft_tools.arxml_validation.check_arxml", wraps=arxml_validation.check_arxml) as spy:
        result = build()

    assert result["success"] is True, result["errors"]
    assert spy.call_count == 1
    assert spy.call_args.args[0] == str(Path(model_root).parent / "dist" / "model.arxml")


def _invalid_outcome(mode: str = "structural"):
    return arxml_validation.ValidationOutcome(valid=False, mode=mode)


def test_validate_reports_an_invalid_generated_arxml_as_a_failure(model_root):
    """The CLI exits 1 here; this tool must not answer success."""
    _make_model_consistent(model_root)

    with patch("swcraft_tools.arxml_validation.check_arxml", return_value=_invalid_outcome()):
        result = validate()

    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert "structural validation" in result["errors"][0]["message"]
    assert result["errors"][0]["path"].endswith("model.arxml")


def test_build_reports_an_invalid_generated_arxml_as_a_failure_before_diagrams_and_needs(model_root):
    """A rejected ARXML stops the build, the same way a rejected model does."""
    _make_model_consistent(model_root)

    with (
        patch("swcraft_tools.arxml_validation.check_arxml", return_value=_invalid_outcome("XSD")),
        patch("swcraft_tools.writer.generate_diagrams") as mock_diagrams,
        patch("swcraft_tools.writer.write_needs_json") as mock_needs,
    ):
        result = build()

    assert result["success"] is False
    assert len(result["errors"]) == 1
    assert "XSD validation" in result["errors"][0]["message"]
    mock_diagrams.assert_not_called()
    mock_needs.assert_not_called()


def _previous_needs_json(project_root: Path) -> Path:
    """A previous build's ``dist/needs.json``, which a failing build must not leave beside its new ARXML."""
    previous = project_root / "dist" / "needs.json"
    previous.parent.mkdir(parents=True, exist_ok=True)
    previous.write_bytes(b'{"previous": true}\n')
    return previous


def test_build_whose_arxml_is_refused_leaves_no_previous_needs_json(model_root):
    """A refused ARXML after the needs.json text was built: the new ARXML stays, the previous needs.json does not.

    The build removes the previous file before its first write, as the CLI graph's ``clean-outputs``
    does, so a refused ARXML never sits beside a needs.json of an earlier model.
    """
    _make_model_consistent(model_root)
    project_root = Path(model_root).parent
    previous = _previous_needs_json(project_root)

    with (
        patch("swcraft_tools.arxml_validation.check_arxml", return_value=_invalid_outcome("XSD")),
        patch("swcraft_tools.writer.write_needs_json") as mock_needs,
    ):
        result = build()

    assert result["success"] is False
    assert "XSD validation" in result["errors"][0]["message"]
    mock_needs.assert_not_called()
    assert (project_root / "dist" / "model.arxml").is_file()
    assert not previous.exists()


def test_build_whose_diagrams_fail_leaves_no_previous_needs_json(model_root):
    """A diagram generator raising after the ARXML passed: the reply is the raised sentence, no needs.json is left."""
    _make_model_consistent(model_root)
    project_root = Path(model_root).parent
    previous = _previous_needs_json(project_root)

    with patch("swcraft_tools.writer.generate_diagrams", side_effect=RuntimeError("the layout failed")):
        result = build()

    assert result["success"] is False
    assert [error["message"] for error in result["errors"]] == ["the layout failed"]
    assert (project_root / "dist" / "model.arxml").is_file()
    assert not previous.exists()


def test_build_whose_arxml_generator_raises_leaves_no_previous_needs_json(model_root):
    """The ARXML generator raising before it writes: neither a new ARXML nor the previous needs.json is left."""
    _make_model_consistent(model_root)
    project_root = Path(model_root).parent
    previous = _previous_needs_json(project_root)

    with patch("swcraft_tools.writer.generate_arxml", side_effect=ValueError("refused")):
        result = build()

    assert result["success"] is False
    assert [error["message"] for error in result["errors"]] == ["refused"]
    assert not (project_root / "dist" / "model.arxml").exists()
    assert not previous.exists()


def test_build_whose_needs_text_cannot_be_read_leaves_no_previous_needs_json(model_root):
    """The text builder raising after the check passed leaves no previous needs.json.

    The builder raises on a model file it cannot read; ``swcraft generate needs`` removes the file in that case.
    """
    _make_model_consistent(model_root)
    project_root = Path(model_root).parent
    previous = _previous_needs_json(project_root)

    with patch("swcraft_tools.writer.build_needs_json", side_effect=ValueError("unreadable")):
        result = build()

    assert result["success"] is False
    assert [error["message"] for error in result["errors"]] == ["unreadable"]
    assert not (project_root / "dist" / "model.arxml").exists()
    assert not previous.exists()


def test_build_whose_needs_write_fails_leaves_no_needs_json(model_root):
    """The final write failing: the new ARXML and views stay, and no needs.json at all.

    The previous one went before the first write, so ``a write that fails keeps the previous file`` is
    ``generate needs``' alone.
    """
    _make_model_consistent(model_root)
    project_root = Path(model_root).parent
    previous = _previous_needs_json(project_root)

    with patch("swcraft_tools.writer.write_needs_json", side_effect=OSError(28, "No space left on device")):
        result = build()

    assert result["success"] is False
    assert [error["message"] for error in result["errors"]] == ["[Errno 28] No space left on device"]
    assert (project_root / "dist" / "model.arxml").is_file()
    assert not previous.exists()


def test_build_refused_by_the_model_check_keeps_the_previous_needs_json(model_root):
    """What the removal does not reach: a model ``check-model`` refuses writes nothing and removes nothing.

    The previous outputs stay a matched set; the CLI graph's ``clean-outputs`` removes them, ``build`` does not.
    """
    _unallocate_the_requirement(model_root)
    project_root = Path(model_root).parent
    previous = _previous_needs_json(project_root)

    result = build()

    assert result["success"] is False
    assert previous.read_bytes() == b'{"previous": true}\n'


# ---------------------------------------------------------------------------
# The gate must say *why*
#
# The agent reads the structured error list in a failing result before spending a retry; a lone
# "failed XSD validation: <path>" is not a list, and the retries are spent guessing.
# ---------------------------------------------------------------------------


def test_an_invalid_arxml_forwards_every_diagnostic_as_its_own_error(model_root):
    _make_model_consistent(model_root)
    outcome = arxml_validation.ValidationOutcome(
        valid=False,
        mode="XSD",
        schema_path="/schemas/AUTOSAR_4-2-2.xsd",
        messages=(
            "model.arxml does not conform to /schemas/AUTOSAR_4-2-2.xsd:",
            "  /AUTOSAR/AR-PACKAGES/AR-PACKAGE: Unexpected child with tag 'BOGUS'",
        ),
    )

    with patch("swcraft_tools.arxml_validation.check_arxml", return_value=outcome):
        result = validate()

    assert result["success"] is False
    messages = [error["message"] for error in result["errors"]]
    assert messages[0].startswith("the generated ARXML failed XSD validation")
    assert any("Unexpected child with tag 'BOGUS'" in message for message in messages)
    assert all(error["path"] == "dist/model.arxml" for error in result["errors"])
    # No location in a reply: the ARXML relative to the project, the schema by its file name.
    assert messages[0] == "the generated ARXML failed XSD validation: dist/model.arxml"
    assert messages[1] == "model.arxml does not conform to AUTOSAR_4-2-2.xsd:"


def test_a_diagnostic_naming_the_arxml_or_the_schema_in_any_spelling_names_neither_location(model_root):
    """The scrub replaces every spelling a validator may use, not the one string the tool passed.

    Resolved (`/private/var` on macOS), POSIX-spelled or a `file://` URI, each is named relative to the
    project, and the schema by its file name.
    """
    from swcraft_mcp.server import _get_project

    _make_model_consistent(model_root)
    arxml = Path(_get_project().resolved_arxml_path)
    schema = Path(model_root).parent / "licensed" / "AUTOSAR_4-2-2.xsd"
    outcome = arxml_validation.ValidationOutcome(
        valid=False,
        mode="XSD",
        schema_path=str(schema),
        messages=(
            f"{arxml.resolve().as_uri()}:3: element BOGUS is not allowed by {schema.resolve().as_uri()}",
            f"{arxml.as_uri()} and {arxml.resolve()} and {arxml.as_posix()}",
            f"{schema.resolve()} could not be read",
        ),
    )

    with patch("swcraft_tools.arxml_validation.check_arxml", return_value=outcome):
        result = validate()

    messages = [error["message"] for error in result["errors"]]
    assert messages[1:] == [
        "dist/model.arxml:3: element BOGUS is not allowed by AUTOSAR_4-2-2.xsd",
        "dist/model.arxml and dist/model.arxml and dist/model.arxml",
        "AUTOSAR_4-2-2.xsd could not be read",
    ], messages


def test_an_unusable_schema_is_reported_as_a_toolchain_problem_not_a_model_problem(model_root):
    """A schema that will not compile means the model was never examined.

    Reported as an invalid ARXML, it would send the agent editing a model no check had looked at.
    """
    _make_model_consistent(model_root)
    outcome = arxml_validation.ValidationOutcome(
        valid=False,
        mode="XSD",
        schema_path="/schemas/AUTOSAR_4-2-2.xsd",
        messages=("Error: the XML schema /schemas/AUTOSAR_4-2-2.xsd could not be compiled",),
        schema_error=True,
    )

    with patch("swcraft_tools.arxml_validation.check_arxml", return_value=outcome):
        result = validate()

    assert result["success"] is False
    headline = result["errors"][0]["message"]
    assert "could not be XSD-validated" in headline
    assert "not a problem with the model" in headline
    assert "failed XSD validation" not in headline
    assert headline.startswith("the ARXML at dist/model.arxml could not be XSD-validated because the schema ")
    assert "the schema AUTOSAR_4-2-2.xsd is unusable" in headline
    assert all("/schemas/" not in error["message"] for error in result["errors"])


@pytest.mark.parametrize("tool", [validate, build])
def test_validate_and_build_output_state_the_validation_mode(model_root, monkeypatch, tool):
    """The tool result says which validation ran, in the CLI's own sentence for that mode.

    With no licensed schema reachable, that is the structural-mode note naming the schema file needed
    (`AUTOSAR_4-2-2.xsd` for this fixture's default version).
    """
    monkeypatch.delenv("SWCRAFT_AUTOSAR_XSD_PATH", raising=False)
    # The per-version default exists on a machine holding the licensed schemas; point it nowhere.
    from swcraft_tools import arxml_validation

    monkeypatch.setattr(
        arxml_validation,
        "DEFAULT_SCHEMA_PATHS",
        {v: "/nonexistent/schemas/" + v.schema_filename for v in arxml_validation.DEFAULT_SCHEMA_PATHS},
    )
    _make_model_consistent(model_root)

    result = tool()

    assert result["success"] is True
    lines = result["output"].splitlines()
    assert lines[0].startswith("ARXML generated: ")
    assert lines[1] == "XSD validation is skipped (requires the licensed AUTOSAR_4-2-2.xsd schema).", result["output"]


@pytest.mark.parametrize("tool", [validate, build])
def test_a_licensed_schema_is_named_by_its_file_name_and_version(model_root, tool):
    """The XSD mode line names the schema by file name and version, never its location.

    A tool reply names no location, so the generated ARXML is named relative to the project.
    """
    from swcraft_tools.arxml_version import ArxmlVersion

    _make_model_consistent(model_root)
    outcome = arxml_validation.ValidationOutcome(
        valid=True,
        mode="XSD",
        version=ArxmlVersion.from_string("4.2.2"),
        schema_path="/licensed/schemas/AUTOSAR_4-2-2.xsd",
    )

    with patch("swcraft_tools.arxml_validation.check_arxml", return_value=outcome):
        result = tool()

    assert result["success"] is True, result
    lines = result["output"].splitlines()
    assert lines[0] == "ARXML generated: dist/model.arxml (AUTOSAR 4.2.2)"
    assert lines[1] == "ARXML validated against AUTOSAR_4-2-2.xsd (AUTOSAR 4.2.2)."


@pytest.mark.parametrize("tool", [validate, build])
def test_a_second_run_after_a_model_change_keeps_the_first_arxml_and_says_nothing_of_it(model_root, tool):
    """The previous ARXML is kept as ``model_old.arxml``; the reply says nothing of it (the CLI alone announces it)."""
    _make_model_consistent(model_root)
    project_root = Path(model_root).parent
    arxml = project_root / "dist" / "model.arxml"

    first = tool()
    assert first["success"] is True, first
    first_build = arxml.read_bytes()
    (swc_file,) = Path(model_root).rglob("SpeedSensorSWC.yaml")
    swc = yaml.safe_load(swc_file.read_text(encoding="utf-8"))
    swc["description"] = "a description the first build did not carry"
    swc_file.write_text(yaml.safe_dump(swc, default_flow_style=False, sort_keys=False), encoding="utf-8")

    second = tool()

    assert second["success"] is True, second
    assert arxml.read_bytes() != first_build
    assert (project_root / "dist" / "model_old.arxml").read_bytes() == first_build
    assert second["output"] == first["output"]
    assert "_old" not in second["output"] and "Previous" not in second["output"]


# ---------------------------------------------------------------------------
# The warning class: `warnings` beside `errors` on every reply of the two gates
# ---------------------------------------------------------------------------

#: The requirement `_unallocate_the_requirement` leaves satisfied by nothing
#: (`trace.requirement-unallocated`, a warning); with `_seed_hardware` too it is the only finding.
_THE_UNALLOCATED_REQUIREMENT = "REQ_001"
#: One shape on every path: the direct replies, the override path, the exception dict.
_REPLY_KEYS = ["success", "errors", "warnings", "output"]
_THE_TWO_GATES = pytest.mark.parametrize("tool", [validate, build], ids=["validate", "build"])


def _unallocate_the_requirement(model_root: str) -> None:
    """No component satisfying the fixture's one requirement: one warning."""
    _set_links(model_root, [])


def _unallocate_two_requirements(model_root: str) -> None:
    """Two requirements nothing satisfies, the fixture's own and a second one: two warnings."""
    _unallocate_the_requirement(model_root)
    (Path(model_root) / "requirements" / "REQ_002.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "REQ_002",
                "title": "Speed limiting",
                "description": "The system shall limit vehicle speed.",
                "req_type": "req",
                "version": 1,
                "source": "architecture",
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )


def _the_one_warning(result: dict) -> None:
    (warning,) = result["warnings"]
    assert _THE_UNALLOCATED_REQUIREMENT in warning["message"], warning
    assert warning["path"] == ""


class TestTheRepliesCarryWarnings:
    """`warnings` beside `errors` on every reply of the two gates."""

    def test_validate_over_warnings_alone_succeeds_with_the_warning_and_writes_the_arxml(self, model_root):
        _seed_hardware(model_root)
        _unallocate_the_requirement(model_root)

        result = validate()

        assert list(result) == _REPLY_KEYS
        assert result["success"] is True
        assert result["errors"] == []
        _the_one_warning(result)
        assert (Path(model_root).parent / "dist" / "model.arxml").exists()
        assert result["output"].startswith("ARXML generated: dist/model.arxml (AUTOSAR 4.2.2)\n")

    def test_build_over_warnings_alone_succeeds_with_the_warning_and_the_three_artifacts(self, model_root):
        _seed_hardware(model_root)
        _unallocate_the_requirement(model_root)

        result = build()

        assert list(result) == _REPLY_KEYS
        assert result["success"] is True
        assert result["errors"] == []
        _the_one_warning(result)
        project_root = Path(model_root).parent
        assert (project_root / "dist" / "model.arxml").exists()
        assert (project_root / "views" / "index.html").exists()
        assert (project_root / "dist" / "needs.json").is_file()
        assert result["output"].endswith("\nneeds.json generated: dist/needs.json")

    @_THE_TWO_GATES
    def test_two_warnings_are_carried_in_the_composers_order(self, model_root, tool):
        """`_finding_entries` over more than one record: the count and the emission order."""
        _seed_hardware(model_root)
        _unallocate_two_requirements(model_root)
        composed = validator.check_cross_layer_consistency(model_root)
        assert composed.consistent and len(composed.warnings) == 2
        assert {record.subjects[0].element for record in composed.warnings} == {_THE_UNALLOCATED_REQUIREMENT, "REQ_002"}

        result = tool()

        assert list(result) == _REPLY_KEYS
        assert result["success"] is True
        assert result["errors"] == []
        assert result["warnings"] == [{"message": record.message, "path": ""} for record in composed.warnings]

    @_THE_TWO_GATES
    def test_a_rejected_arxml_carries_the_warnings_beside_its_error(self, model_root, tool):
        """The composer ran before the ARXML check refused, so its warnings are in the failure reply."""
        _seed_hardware(model_root)
        _unallocate_the_requirement(model_root)

        with patch("swcraft_tools.arxml_validation.check_arxml", return_value=_invalid_outcome()):
            result = tool()

        assert list(result) == _REPLY_KEYS
        assert result["success"] is False
        (error,) = result["errors"]
        assert "structural validation" in error["message"]
        _the_one_warning(result)

    @_THE_TWO_GATES
    def test_the_fixed_model_carries_no_warning(self, model_root, tool):
        _make_model_consistent(model_root)

        result = tool()

        assert list(result) == _REPLY_KEYS
        assert result["success"] is True, result["errors"]
        assert result["errors"] == []
        assert result["warnings"] == []

    @_THE_TWO_GATES
    def test_a_broken_model_carries_both_lists_and_runs_no_generator(self, model_root, tool):
        """The error refuses; the warning rides beside it and names what the error does not."""
        _unallocate_the_requirement(model_root)
        with (
            patch("swcraft_tools.writer.generate_arxml") as mock_arxml,
            patch("swcraft_tools.writer.generate_diagrams") as mock_diagrams,
            patch("swcraft_tools.writer.write_needs_json") as mock_needs,
        ):
            result = tool()

        assert list(result) == _REPLY_KEYS
        assert result["success"] is False
        (error,) = result["errors"]
        assert "processor" in error["message"] and _THE_UNALLOCATED_REQUIREMENT not in error["message"], error
        assert error["path"] == ""
        _the_one_warning(result)
        assert result["output"] == ""
        mock_arxml.assert_not_called()
        mock_diagrams.assert_not_called()
        mock_needs.assert_not_called()

    @pytest.mark.parametrize(
        ("tool", "variable"),
        [(validate, "SWCRAFT_MCP_VALIDATE_COMMAND"), (build, "SWCRAFT_MCP_BUILD_COMMAND")],
        ids=["validate", "build"],
    )
    @pytest.mark.parametrize("returncode", [0, 1], ids=["passed", "failed"])
    def test_the_override_path_answers_no_warning(self, monkeypatch, model_root, tool, variable, returncode):
        """A project's own command reports in text: its reply carries the key, empty, whichever way it went."""
        monkeypatch.setenv(variable, "moon run :build")
        output = "tools:validate-arxml | INVALID (structural mode): dist/model.arxml\n" if returncode else "VALID\n"
        with patch("swcraft_tools.builder.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=returncode, stdout=output, stderr="")
            result = tool()

        assert list(result) == _REPLY_KEYS
        assert result["success"] is (returncode == 0)
        assert len(result["errors"]) == returncode
        assert result["warnings"] == []

    @_THE_TWO_GATES
    def test_the_exception_dict_answers_no_warning_before_the_composer_ran(self, monkeypatch, tmp_path, tool):
        monkeypatch.setenv("SWCRAFT_MANIFEST", str(tmp_path / "does-not-exist.yaml"))

        result = tool()

        assert list(result) == _REPLY_KEYS
        assert result["success"] is False
        assert len(result["errors"]) == 1
        assert result["warnings"] == []

    @_THE_TWO_GATES
    def test_the_exception_dict_answers_no_warning_after_the_composer_ran(self, model_root, tool):
        """A generator raising after the composer ran: the exception dict carries the raised sentence alone."""
        _seed_hardware(model_root)

        with patch("swcraft_tools.writer.generate_arxml", side_effect=ValueError("boom: unresolvable reference")):
            result = tool()

        assert list(result) == _REPLY_KEYS
        assert result["success"] is False
        (error,) = result["errors"]
        assert "boom" in error["message"]
        assert result["warnings"] == []

    def test_the_key_is_the_two_gates_alone(self, model_root):
        """A component-exchange reply answers its three keys: `warnings` was added per tool, not to the shared shape."""
        _make_model_consistent(model_root)

        exported = export_component("SpeedSensorSWC")
        assert exported["success"] is True, exported["errors"]
        assert list(exported) == ["success", "errors", "output"]
