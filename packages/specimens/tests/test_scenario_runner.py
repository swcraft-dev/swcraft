from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tests.oracle.generator_start import generator_command
from tests.scenarios import run_scenarios

ROOT = Path(__file__).parents[1]


def scenario_document(scenarios: list[dict[str, Any]]) -> str:
    return "schema_version: '1.0'\nscenarios:\n" + "\n".join(
        "  - " + json.dumps(s).replace("\n", "\n    ") for s in scenarios
    )


def valid_scenario() -> dict[str, Any]:
    return {
        "id": "valid",
        "description": "x",
        "presets": ["small"],
        "command": ["swcraft", "check-model", "--format", "json", "--project", "{project}"],
        "expected_exit_code": 0,
        "format": "json",
        "assertions": [{"id": "clean", "op": "equals", "path": "findings", "expected": []}],
    }


def load_one(tmp_path: Path, value: dict[str, Any]) -> None:
    path = tmp_path / "scenarios.yaml"
    path.write_text(scenario_document([value]), encoding="utf-8")
    run_scenarios.load_scenarios(path)


def test_production_contract_loads() -> None:
    scenarios = run_scenarios.load_scenarios(ROOT / "tests/scenarios/production.yaml")
    assert len(scenarios) == 15
    assert {p for s in scenarios for p in s["presets"]} == {"small", "medium", "large"}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.update(extra=True),
        lambda s: s["assertions"][0].update(op="unknown"),
        lambda s: s.update(id="bad id"),
        lambda s: s.update(presets=["tiny"]),
        lambda s: s.update(command=["sh", "-c", "true"]),
        lambda s: s.update(command=["swcraft", "check-model", "--project", "{unknown}"]),
    ],
)
def test_invalid_contracts_are_rejected(tmp_path: Path, mutation: Callable[[dict[str, Any]], object]) -> None:
    value = valid_scenario()
    mutation(value)
    with pytest.raises(run_scenarios.ScenarioError):
        load_one(tmp_path, value)


def _production(scenario_id: str) -> dict[str, Any]:
    scenarios = run_scenarios.load_scenarios(ROOT / "tests/scenarios/production.yaml")
    return next(s for s in scenarios if s["id"] == scenario_id)


@pytest.mark.parametrize("scenario_id", ["query-domain", "query-members"])
def test_the_query_scenarios_expect_schema_version_3(scenario_id: str) -> None:
    schema = next(a for a in _production(scenario_id)["assertions"] if a["id"] == "schema")
    assert schema == {"id": "schema", "op": "equals", "path": "schemaVersion", "expected": 3}


def test_the_check_model_scenario_asserts_an_empty_findings_list() -> None:
    scenario = _production("check-model")
    assert scenario["command"] == [
        "swcraft",
        "check-model",
        "--format",
        "json",
        "--project",
        "{project}",
    ]
    assert scenario["format"] == "json"
    assert scenario["assertions"] == [{"id": "clean", "op": "equals", "path": "findings", "expected": []}]


def test_an_empty_findings_list_passes_and_a_warning_fails(tmp_path: Path) -> None:
    clean = {"id": "clean", "op": "equals", "path": "findings", "expected": [], "_scenario": "c"}
    args: tuple[Any, ...] = ({}, tmp_path, "", "small", 1, tmp_path / "swcraft.yaml")
    run_scenarios._assertion({"schemaVersion": 1, "findings": []}, clean, *args)
    warning = {"severity": "warning", "rule": "rule:x", "message": "m"}
    with pytest.raises(run_scenarios.ScenarioError, match="assertion clean failed"):
        run_scenarios._assertion({"schemaVersion": 1, "findings": [warning]}, clean, *args)


def test_the_text_no_findings_operator_is_gone(tmp_path: Path) -> None:
    # No scenario reads check-model's text, so the operator that matched it is gone.
    assert "no_findings" not in run_scenarios.ALLOWED_OPS
    value = valid_scenario()
    value["assertions"] = [{"id": "clean", "op": "no_findings"}]
    with pytest.raises(run_scenarios.ScenarioError, match="unknown assertion operator"):
        load_one(tmp_path, value)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    first = valid_scenario()
    second = valid_scenario()
    path = tmp_path / "duplicate.yaml"
    path.write_text(scenario_document([first, second]), encoding="utf-8")
    with pytest.raises(run_scenarios.ScenarioError, match="duplicate"):
        run_scenarios.load_scenarios(path)


def test_partial_placeholder_is_rejected(tmp_path: Path) -> None:
    value = valid_scenario()
    value["command"][-1] = "prefix-{project}"
    path = tmp_path / "partial.yaml"
    path.write_text(scenario_document([value]), encoding="utf-8")
    with pytest.raises(run_scenarios.ScenarioError, match="whole scalar"):
        run_scenarios.load_scenarios(path)


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in swcraft is a shebang script, not started on Windows")
def test_fake_executable_is_invoked_without_shell(tmp_path: Path) -> None:
    fake = tmp_path / "fake swcraft"
    fake.write_text(
        f"#!{sys.executable}\nimport sys\nprint('Validation passed')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    result = run_scenarios._run([str(fake)], tmp_path)
    assert result.returncode == 0
    assert "Validation passed" in result.stdout


def test_semantic_assertion_failure_is_reported(tmp_path: Path) -> None:
    with pytest.raises(run_scenarios.ScenarioError, match="assertion bad failed"):
        run_scenarios._assertion(
            {"value": 1},
            {"id": "bad", "op": "equals", "path": "value", "expected": 2, "_scenario": "semantic"},
            {},
            tmp_path,
            "",
            "small",
            1,
            tmp_path / "swcraft.yaml",
        )


def test_damaged_json_response_is_rejected() -> None:
    with pytest.raises(run_scenarios.ScenarioError, match="damaged JSON"):
        run_scenarios.parse_json_response("not-json")


@pytest.mark.parametrize(
    ("option", "environ", "expected"),
    [
        ("/opt/a/swcraft-specimens", {}, "/opt/a/swcraft-specimens"),
        (None, {"SWCRAFT_SPECIMENS_BIN": "/opt/b/swcraft-specimens"}, "/opt/b/swcraft-specimens"),
        ("/opt/a/swcraft-specimens", {"SWCRAFT_SPECIMENS_BIN": "/opt/b/swcraft-specimens"}, "/opt/a/swcraft-specimens"),
    ],
    ids=["option", "variable", "the-option-wins"],
)
def test_the_generator_is_the_option_else_the_variable(
    option: str | None, environ: dict[str, str], expected: str
) -> None:
    assert run_scenarios.specimens_bin(option, environ) == expected


def test_without_either_the_run_is_refused_and_path_is_never_searched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `swcraft-specimens` first on `PATH` is not taken: the results would name a generator that did not run."""
    on_path = tmp_path / "bin"
    on_path.mkdir()
    for name in ("swcraft", "swcraft-specimens"):
        stub = on_path / name
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(on_path))
    monkeypatch.delenv("SWCRAFT_SPECIMENS_BIN", raising=False)
    started: list[object] = []
    monkeypatch.setattr(run_scenarios, "run_suite", lambda *arguments: started.append(arguments))
    assert run_scenarios.main(["--preset", "small", "--swcraft-bin", str(on_path / "swcraft")]) == 1
    assert started == []
    assert "use --specimens-bin or SWCRAFT_SPECIMENS_BIN" in capsys.readouterr().err


def test_main_hands_the_named_generator_to_the_suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWCRAFT_SPECIMENS_BIN", "/opt/b/swcraft-specimens")
    started: list[tuple[object, ...]] = []
    monkeypatch.setattr(run_scenarios, "run_suite", lambda *arguments: started.append(arguments))
    argv = ["--preset", "small", "--swcraft-bin", "/opt/swcraft", "--specimens-bin", "/opt/a/swcraft-specimens"]
    assert run_scenarios.main(argv) == 0
    assert [arguments[-1] for arguments in started] == [["/opt/a/swcraft-specimens"]]


def test_a_generation_by_the_recorded_generator_passes() -> None:
    generation = {"generator": {"name": "swcraft-specimens", "version": "1.2.3"}}
    run_scenarios.generator_provenance(generation, "small", "1.2.3")


@pytest.mark.parametrize(
    "named",
    [
        {"name": "swcraft-specimens", "version": "1.2.2"},
        {"name": "another-generator", "version": "1.2.3"},
        None,
    ],
    ids=["another-version", "another-name", "none"],
)
def test_a_generation_by_another_generator_is_refused(named: dict[str, str] | None) -> None:
    """A sidecar naming another generator refuses the run: the results record this runner's version and digest."""
    with pytest.raises(run_scenarios.ScenarioError, match=r"^small: the generator that ran is "):
        run_scenarios.generator_provenance({"generator": named}, "small", "1.2.3")


def test_the_generator_is_handed_the_preset_file_whose_digest_is_recorded(tmp_path: Path) -> None:
    """The runner hands over the preset file it hashes: a name could resolve to a differing packaged copy."""
    from swcraft_specimens.packaged import preset_path

    argv_log = tmp_path / "argv.json"
    fake = tmp_path / "fake_generator.py"
    fake.write_text(
        "import json, sys\n"
        f"with open({str(argv_log)!r}, 'w', encoding='utf-8', newline='\\n') as handle:\n"
        "    json.dump(sys.argv[1:], handle)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
        newline="\n",
    )
    build = tmp_path / "build"
    with pytest.raises(run_scenarios.ScenarioError, match=r"^small: generation failed"):
        run_scenarios.run_suite(
            ["small"], sys.executable, build / "scenarios", build / "results.json", [sys.executable, str(fake)]
        )
    argv = json.loads(argv_log.read_text(encoding="utf-8"))
    assert argv[:2] == ["generate", str(preset_path("small"))]


def test_the_results_document_is_schema_2_since_its_keys_were_renamed() -> None:
    assert run_scenarios.RESULTS_SCHEMA_VERSION == "2.0"


@pytest.mark.integration
def test_the_runner_with_real_swcraft(tmp_path: Path) -> None:
    swcraft = os.environ.get("SWCRAFT_BIN")
    if not swcraft:
        pytest.skip("SWCRAFT_BIN is not set")
    # The one start helper, so `SWCRAFT_SPECIMENS_BIN` reaches the generator and the start is logged.
    results = run_scenarios.run_suite(
        ["small"], swcraft, tmp_path / "build", tmp_path / "build/results.json", generator_command()
    )
    assert len(results["presets"][0]["scenarios"]) == 15
    assert all(s["status"] == "passed" for s in results["presets"][0]["scenarios"])
    assert str(tmp_path) not in json.dumps(results)
