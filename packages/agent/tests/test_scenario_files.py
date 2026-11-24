"""Tests for the scenario fixtures: their shape and their answer counts.

The scripts hold **natural-language prompts and approval answers only**: no tool names,
no addresses, nothing derived from the model schema. This module pins how many answers
each turn writes down (:data:`_EXPECTED_APPROVALS`). ``ScriptedApprover`` **denies** once
its list runs out, so a turn short one answer replays as "the user said no", which reads
like an enforcement bug. ``test_scenario_replay.py`` asserts the run asks that many questions.

Both turn forms must keep loading: the plain string (a turn that approves nothing) and the
``{user, approvals}`` mapping.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from swcraft_agent.cli import load_script

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCENARIOS_DIR = Path(__file__).resolve().parent / "fixtures" / "scenarios"

#: Every scenario's approval answers, turn by turn.
#:
#: An unscoped turn asks **one question per change set** — the exact staged
#: delta. A scoped turn asks the host's scope question first, then each change
#: set's delta, then — only when that delta weakens a check and only
#: after the delta was approved — the server's independent safety question.
#: A turn that submits no change set asks nothing and writes nothing down.
_EXPECTED_APPROVALS: dict[str, list[tuple[str, ...]]] = {
    # Two change sets, one per turn.
    "01-add-swc.yaml": [("y",), ("y",)],
    # One change set: the connector, whose two ends staging judges before the question.
    "02-connect-ports.yaml": [("y",)],
    # The connecting change set is refused at staging, so no question is asked.
    "03-incompatible-ports.yaml": [()],
    # Turn 1 authors the requirement and its trace link; turn 2 only reads.
    "04-requirement-trace.yaml": [("y",), ()],
    # `build` asks nothing; the repair is one reviewed change set.
    "05-build-error-fix.yaml": [("y",)],
    # Scoped: scope + delta, scope + delta, scope + delta + safety, scope + delta.
    "06-scoped-authorization.yaml": [("y", "y"), ("y", "y"), ("y", "y", "n"), ("y", "n")],
}

#: The scenarios written entirely in the plain string form, so a real file exercises that form of ``load_script``.
_PLAIN_SCENARIOS = ["03-incompatible-ports.yaml"]

#: The scenarios that write at least one approval answer down.
_APPROVAL_SCENARIOS = [name for name, answers in _EXPECTED_APPROVALS.items() if any(answers)]

_EXPECTED_SCENARIOS = list(_EXPECTED_APPROVALS)

#: The files of a scenario the removed-tool reading covers here, as (directory, suffix).
_SCENARIO_FILES = ((_SCENARIOS_DIR, ".yaml"),)


@pytest.mark.parametrize("filename", _EXPECTED_SCENARIOS)
def test_scenario_yaml_exists(filename):
    """Each expected scenario yaml file must exist."""
    assert (_SCENARIOS_DIR / filename).is_file(), f"Scenario file not found: {_SCENARIOS_DIR / filename}"


@pytest.mark.parametrize("filename", _PLAIN_SCENARIOS)
def test_plain_scenario_yaml_is_nonempty_list_of_strings(filename):
    """A scenario that approves nothing must still parse as a plain list of strings."""
    path = _SCENARIOS_DIR / filename

    turns = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert isinstance(turns, list), f"{filename}: expected a list, got {type(turns).__name__}"
    assert len(turns) > 0, f"{filename}: list must not be empty"
    for i, turn in enumerate(turns):
        assert isinstance(turn, str), f"{filename}: turn {i} must be a string, got {type(turn).__name__}: {turn!r}"


@pytest.mark.parametrize("filename", _EXPECTED_SCENARIOS)
def test_scenario_yaml_loads_through_the_real_loader(filename):
    """Every scenario must load through ``load_script`` into non-empty user turns."""
    turns = load_script(str(_SCENARIOS_DIR / filename))

    assert turns, f"{filename}: loaded no turns"
    for i, turn in enumerate(turns):
        assert turn.user.strip(), f"{filename}: turn {i} has an empty user message"


@pytest.mark.parametrize("filename", _EXPECTED_SCENARIOS)
def test_scenario_answers_match_the_questions_the_run_asks(filename):
    """Every turn writes down exactly the answers its review questions consume.

    A change here is a change to the human review boundary, not a fixture detail.
    """
    turns = load_script(str(_SCENARIOS_DIR / filename))

    assert [turn.approvals for turn in turns] == _EXPECTED_APPROVALS[filename]


@pytest.mark.parametrize("filename", _APPROVAL_SCENARIOS)
def test_approval_scenario_answers_are_strings(filename):
    """Approvals are answer strings. A bare YAML ``yes`` would parse as a bool."""
    turns = load_script(str(_SCENARIOS_DIR / filename))

    for i, turn in enumerate(turns):
        for position, answer in enumerate(turn.approvals):
            assert isinstance(answer, str), f"{filename}: turn {i} approval {position} must be a string"


@pytest.mark.parametrize("filename", _EXPECTED_SCENARIOS)
def test_scenario_files_name_no_removed_tool(filename):
    """A scenario script may not name a removed tool."""
    import json

    projections = json.loads(
        (
            _REPO_ROOT
            / "packages"
            / "tools"
            / "tests"
            / "fixtures"
            / "contracts"
            / "editing-protocol"
            / "tool-projections.json"
        ).read_text(encoding="utf-8")
    )
    removed = sorted(projections["projections"]["replaced"])

    for directory, suffix in _SCENARIO_FILES:
        text = (directory / filename.replace(".yaml", suffix)).read_text(encoding="utf-8")
        named = [tool for tool in removed if tool in text]
        assert not named, f"{filename[:-5]}{suffix} still names removed tool(s): {named}"
