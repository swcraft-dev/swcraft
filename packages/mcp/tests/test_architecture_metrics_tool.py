"""The ``architecture_metrics`` MCP tool, driven through ``tools/call``.

The tool is ``swcraft metrics``' computation with a bounded answer: the answers, each section's
conclusion and limit, the figures, every growing list capped at ten, no per-component row and no table
per composition. The answers and conclusions are held equal to the CLI's ``--format json`` strings, so
the tool states no meaning of its own.

What this module does not reach: the figures themselves (``packages/tools/tests/test_metrics.py``) and
the sentences (``packages/tools/tests/test_metrics_conclusions.py``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from protocol_client import call
from scratch_git import quiet_maintenance
from swcraft_tools.editing import read as read_mod
from swcraft_tools.metrics_conclusions import NO_BASE_NAMED
from swcraft_tools.metrics_render import DEFINITIONS, DEFINITIONS_TITLE

_GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com"]

#: The answer's keys, in order: no version, kind or project name, which belong to the CLI's file.
ANSWER_KEYS = ["response", "columns", "answers", "state", "movement", "no_base", "look_at", "conclusions"]
STATE_KEYS = [
    "label",
    "size",
    "coupling",
    "links_not_placed",
    "links_not_placed_by_reason",
    "cohesion",
    "coverage",
    "integrity",
]
STATE_SECTIONS = ["size", "coupling", "tables", "cohesion", "coverage", "integrity"]


def _git(root: Path, *args: str) -> None:
    subprocess.run([*_GIT, *args], cwd=root, check=True, capture_output=True, encoding="utf-8")


def _commit(root: Path) -> None:
    _git(root, "init", "-q")
    quiet_maintenance(root)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")


def _answer(**arguments: Any) -> Any:
    """The tool's answer over the wire: the decoded mapping, or the text of a refusal."""
    result = call("architecture_metrics", **arguments)
    if result.structured is None:
        return result.texts[0]
    return result.structured


def _cli_json(project: Path, *since: str) -> dict[str, Any]:
    """``swcraft metrics --format json`` on *project*, run as the CLI runs, in a process of its own."""
    argv = [sys.executable, "-m", "swcraft_tools.cli", "metrics", "--project", str(project / "swcraft.yaml")]
    for revision in since:
        argv += ["--since", revision]
    completed = subprocess.run(
        [*argv, "--format", "json"], cwd=project, capture_output=True, encoding="utf-8", env=os.environ.copy()
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _components(model: Path, count: int) -> None:
    """*count* components with no port and no requirement link: each lands in a list the answer caps."""
    for index in range(count):
        name = f"Extra{index:02d}SWC"
        _write(model / "swc" / "extra" / f"{name}.yaml", {"name": name, "type": "application"})


class TestTheAnswer:
    def test_keys_and_no_per_component_row_or_table(self, model_root: str) -> None:
        answer = _answer()
        assert list(answer) == ANSWER_KEYS
        assert answer["response"] == "metrics"
        assert list(answer["state"]) == STATE_KEYS
        assert "components" not in answer["state"]
        assert "composition_tables" not in answer["state"]
        # The table per composition is the CLI's; its count stays.
        assert answer["state"]["size"]["compositions"] == 1
        assert list(answer["conclusions"]) == [*STATE_SECTIONS, "look_at"]

    def test_without_since_no_movement_and_the_heading_says_no_base(self, model_root: str) -> None:
        answer = _answer()
        assert answer["columns"] == ["working tree"]
        assert answer["movement"] is None
        assert answer["no_base"] is None
        assert answer["answers"]["heading"] == NO_BASE_NAMED

    def test_with_since_the_movement_against_that_revision(self, model_root: str, tmp_path: Path) -> None:
        _commit(tmp_path)
        _components(Path(model_root), 2)
        answer = _answer(since="HEAD")
        assert len(answer["columns"]) == 2
        assert answer["columns"][0].startswith("HEAD (")
        assert answer["columns"][1] == "working tree"
        assert answer["movement"]["added"] == {"first": ["Extra00SWC", "Extra01SWC"], "more": 0}
        assert list(answer["conclusions"]) == [*STATE_SECTIONS, "movement", "look_at"]
        heading = answer["answers"]["heading"]
        added = "2 components added: Extra00SWC, Extra01SWC."
        assert heading.startswith("Dependencies between components held at ")
        assert added in heading
        assert added in answer["conclusions"]["movement"]["conclusion"]

    def test_an_unreadable_revision_is_an_error_string(self, model_root: str, tmp_path: Path) -> None:
        _commit(tmp_path)
        answer = _answer(since="no-such-revision")
        assert isinstance(answer, str)
        assert answer.startswith("ERROR: git archive failed for revision 'no-such-revision'")

    def test_no_repository_is_an_error_string_too(self, model_root: str) -> None:
        # No automatic base: a revision the agent named and git cannot give is always the refusal.
        answer = _answer(since="HEAD")
        assert isinstance(answer, str) and answer.startswith("ERROR: not a git repository")

    def test_over_the_budget_refused_with_the_cli_as_the_whole_answer(
        self, model_root: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _commit(tmp_path)
        monkeypatch.setattr(read_mod, "RESPONSE_BUDGET_WIRE_BYTES", 200)
        refused = _answer(since="HEAD")
        assert isinstance(refused, str)
        assert refused.startswith("ERROR: this answer measures ")
        assert "above the server's 200-byte budget; it is refused rather than truncated." in refused
        assert "`swcraft metrics --since HEAD --project PATH`" in refused
        assert "`swcraft metrics --project PATH`" in _answer()

    def test_an_error_over_the_budget_is_refused_too(self, model_root: str) -> None:
        # Five thousand unnamed runnables: about 40 bytes of load error each carries it past the real
        # budget; it is refused like an answer, never sent whole or cut.
        runnables = [{"description": f"entry {index}"} for index in range(5000)]
        _write(Path(model_root) / "swc" / "extra" / "WideSWC.yaml", {"name": "WideSWC", "runnables": runnables})
        refused = _answer()
        assert isinstance(refused, str)
        budget = read_mod.RESPONSE_BUDGET_WIRE_BYTES
        assert refused.startswith("ERROR: this answer measures ")
        assert f"above the server's {budget}-byte budget; it is refused rather than truncated." in refused
        assert "`swcraft metrics --project PATH`" in refused
        assert read_mod.measure(refused) <= budget

    def test_a_long_revision_is_bounded_in_the_refusal(
        self, model_root: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The refusal names the revision by its first 64 characters and a marker. The budget is lowered:
        # a revision past the real one cannot reach git on Windows (32,767-character command line).
        _commit(tmp_path)
        monkeypatch.setattr(read_mod, "RESPONSE_BUDGET_WIRE_BYTES", 2_000)
        revision = "r" * 5_000
        refused = _answer(since=revision)
        assert isinstance(refused, str)
        budget = read_mod.RESPONSE_BUDGET_WIRE_BYTES
        assert refused.startswith("ERROR: this answer measures ")
        assert f"above the server's {budget}-byte budget; it is refused rather than truncated." in refused
        assert f"`swcraft metrics --since '{'r' * 64}[... 4936 more characters]' --project PATH`" in refused
        assert read_mod.measure(refused) <= budget
        assert len(refused) < 1_000

    def test_a_revision_past_the_command_line_is_a_revision_not_read(self, model_root: str, tmp_path: Path) -> None:
        # Past every runner's command line: the system refuses to start git.
        _commit(tmp_path)
        refused = _answer(since="r" * 2_000_000)
        assert isinstance(refused, str)
        assert refused.startswith(f"ERROR: revision '{'r' * 64}[... 1999936 more characters]' could not be read: ")
        assert "model root" not in refused
        assert len(refused) < 1_000

    def test_a_revision_of_64_characters_is_named_whole(
        self, model_root: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _commit(tmp_path)
        monkeypatch.setattr(read_mod, "RESPONSE_BUDGET_WIRE_BYTES", 100)
        revision = "r" * 64
        refused = _answer(since=revision)
        assert isinstance(refused, str)
        assert f"`swcraft metrics --since {revision} --project PATH`" in refused

    def test_a_list_that_grows_with_the_model_stops_at_ten(self, model_root: str) -> None:
        _components(Path(model_root), 12)
        answer = _answer()
        # Thirteen components with no requirement link: the twelve and SpeedCtrlSWC.
        unlinked = answer["state"]["coverage"]["requirement"]["unlinked"]
        assert len(unlinked["first"]) == 10 and unlinked["more"] == 3
        (no_link,) = [line for line in answer["look_at"] if line["line"] == "no link"]
        assert len(no_link["entries"]) == 10 and no_link["more"] == 2


class TestTheSentencesAreTheCli:
    """The two answers and each section's conclusion and limit, string for string, against the CLI's JSON."""

    @staticmethod
    def _assert_same(answer: dict[str, Any], document: dict[str, Any]) -> None:
        assert answer["answers"] == document["answers"]
        assert answer["conclusions"] == document["conclusions"]
        assert answer["columns"] == document["columns"]
        assert answer["no_base"] == document["no_base"]

    def test_without_a_base(self, model_root: str, tmp_path: Path) -> None:
        self._assert_same(_answer(), _cli_json(tmp_path))

    def test_against_a_revision(self, model_root: str, tmp_path: Path) -> None:
        _commit(tmp_path)
        _components(Path(model_root), 3)
        answer = _answer(since="HEAD")
        document = _cli_json(tmp_path, "HEAD")
        self._assert_same(answer, document)
        assert "movement" in answer["conclusions"]

    def test_no_table_sentence_and_no_definition(self, model_root: str, tmp_path: Path) -> None:
        text = json.dumps(_answer(), ensure_ascii=False)
        document = _cli_json(tmp_path)
        assert document["state"]["composition_tables"]
        for table in document["state"]["composition_tables"]:
            assert table["conclusion"] not in text
        for _, definition in DEFINITIONS:
            assert definition not in text
        assert DEFINITIONS_TITLE not in text
        assert "Terms:" not in text
