"""``swcraft experiment trace --labelling=<JSON>``: the panel's passes for one run.

The value replaces the configuration's ``search.labelling`` for that run, is
validated by the same field and the same name check (a refusal names
``--labelling``), is recorded in the run's own files and is never written into
the configuration file. Every model is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import DIGIT_LIMIT_FLOOR, int_max_str_digits
from swcraft_tools.project import require_project
from swcraft_tools.trace_search import TraceExperimentError, run_trace
from trace_models import TraceModel, write_project


def _model() -> TraceModel:
    model = TraceModel()
    model.component("BrakeUnit", description="brake temperature supervision", satisfies=["SR-1@1", "DR-1@1"])
    model.component("DoorUnit", description="door lock control")
    for identifier, title, document in (
        ("SR-1", "Brake temperature", "Brake"),
        ("SR-2", "Brake report", "Brake"),
        ("DR-1", "Door lock", "Door"),
        ("DR-2", "Door unlock", "Door"),
    ):
        model.requirement(identifier, title, f"The {title.lower()} shall work", document=document)
    model.requirement("N-1", "Lamp", "The lamp shall light")
    return model


def _run(tmp_path: Path, flag: str | None, **keys: Any):
    manifest, config = write_project(_model(), tmp_path / "project", **keys)
    return run_trace(require_project(str(manifest)), config, labelling=flag), config


def _passes(run) -> dict[str | None, str | None]:
    return {row["document"]: row["pass"] for row in run.document["documents"]}


@pytest.mark.parametrize(
    ("text", "key", "expected"),
    [
        ('"all"', "all", {"Brake": "labelling", "Door": "labelling", None: "labelling"}),
        ("[]", [], {"Brake": "review", "Door": "review", None: "review"}),
        ('["Door"]', ["Door"], {"Brake": "review", "Door": "labelling", None: "review"}),
    ],
    ids=["all", "none", "a-list"],
)
def test_each_value_gives_the_passes_the_key_would(tmp_path: Path, text: str, key: Any, expected) -> None:
    by_flag, _config = _run(tmp_path / "flag", text, labelling=["Brake"])
    by_key, _config = _run(tmp_path / "key", None, labelling=key)
    assert _passes(by_flag) == _passes(by_key) == expected
    assert by_flag.document["requirements"] == by_key.document["requirements"]


def test_it_is_recorded_and_the_configuration_file_is_never_written(tmp_path: Path) -> None:
    manifest, config = write_project(_model(), tmp_path / "project", labelling=["Brake"])
    assert config is not None
    before = config.read_bytes()  # read before the run, so a rewrite by the run would show
    run = run_trace(require_project(str(manifest)), config, labelling='["Door"]')
    assert config.read_bytes() == before
    assert run.document["fingerprint"]["commandLine"] == {"labelling": ["Door"]}
    assert run.document["fingerprint"]["parameters"]["search"]["labelling"] == ["Door"]
    report = run.report_path.read_text(encoding="utf-8")
    assert '- search.labelling: ["Door"] (from the command line)\n' in report
    assert json.loads(run.result_path.read_text(encoding="utf-8"))["fingerprint"]["commandLine"] == {
        "labelling": ["Door"]
    }


def test_without_it_nothing_is_recorded(tmp_path: Path) -> None:
    run, _config = _run(tmp_path, None, labelling=["Brake"])
    assert "commandLine" not in run.document["fingerprint"]
    assert "(from the command line)" not in run.report_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        ('["Nowhere"]', r"^'--labelling' names document 'Nowhere', which no requirement of the model carries$"),
        ("not json", r"^--labelling is not JSON \("),
        ("5", r"^--labelling is refused: "),
        ('["Door", "Door"]', r"^--labelling is refused: .*labelling names a document more than once: Door"),
        ('"some"', r"^--labelling is refused: "),
    ],
    ids=["unknown-document", "not-json", "a-number", "repeated", "another-word"],
)
def test_a_refused_value_names_the_flag_and_nothing_is_written(tmp_path: Path, text: str, refusal: str) -> None:
    manifest, config = write_project(_model(), tmp_path / "project")
    with pytest.raises(TraceExperimentError, match=refusal):
        run_trace(require_project(str(manifest)), config, labelling=text)
    assert not (tmp_path / "project" / "dist").exists()


def test_an_integer_past_the_digit_limit_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    """Python's JSON reader refuses an integer past the digit limit with a plain ``ValueError``."""
    manifest, config = write_project(_model(), tmp_path / "project")
    with (
        int_max_str_digits(DIGIT_LIMIT_FLOOR),
        pytest.raises(TraceExperimentError, match=r"^--labelling is refused: .*too long"),
    ):
        run_trace(require_project(str(manifest)), config, labelling="1" * (DIGIT_LIMIT_FLOOR + 1))
    assert not (tmp_path / "project" / "dist").exists()


def test_a_document_the_constraints_leave_out_is_refused_by_the_flag_s_name(tmp_path: Path) -> None:
    manifest, config = write_project(_model(), tmp_path / "project", documents=["Brake"])
    with pytest.raises(TraceExperimentError, match=r"^'--labelling' names document 'Door', which 'constraints"):
        run_trace(require_project(str(manifest)), config, labelling='["Door"]')
