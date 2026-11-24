"""Audit completeness on the change-set lifecycle, proven over the wire.

> Every change set produces a lifecycle record whatever its outcome, and every
> operation of a committed change set produces a writer entry.

* **A refused change set leaves evidence.** The engine allocates the change-set
  id on receipt, so each rejection point before staging (pass A, the fail-fast
  staleness check, gate 1) leaves an identifiable `change_set_rejected` record.
* **The completeness check is derived, not maintained.** `block_is_complete`
  compares the element addresses of a committed delta with those of the writer
  entries between the block's two delimiters; a new field or layer needs no edit.
* **An interrupted commit is detectable**: an intent record with no completion
  record. This is the MCP-surface half of
  `packages/tools/tests/test_editing_recovery.py`'s engine-level check.

`discover` and `read` write **nothing**, not a byte, even when refused: a
rejected change set is evidence, a mistyped read is not. The other retained
tools are unaudited; `import_component` plans and writes nothing, and its
submission through `change_set` has the ordinary lifecycle.

Everything goes through `tests/support/protocol_client.py`: the subject is the
surface's audit behaviour, not the engine's.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import editing_vector_fixtures as fixtures
import pytest
import swcraft_mcp.server as server
import swcraft_tools.session as session_mod
from decision_table import ADDED_QUESTIONS, live_retained, load_projections
from fastmcp.exceptions import ToolError
from guide_support import fixture_corpus, install_corpus
from protocol_client import DECLINE, ScriptedHuman, call, call_reviewed, lifecycle_records
from scratch_git import quiet_maintenance
from swcraft_tools.editing.audit import (
    AUDIT_FILENAME,
    CHANGE_SET_ABANDONED,
    CHANGE_SET_APPROVED,
    CHANGE_SET_COMMIT_INTENT,
    CHANGE_SET_COMMITTED,
    CHANGE_SET_ID_PATTERN,
    CHANGE_SET_REJECTED,
    CHANGE_SET_STAGED,
    ELEMENT_WRITER_OPERATIONS,
    LIFECYCLE_OPERATIONS,
    CommitBlock,
    block_is_complete,
    commit_block,
    committed_element_addresses,
    incomplete_commits,
    written_element_addresses,
)

from tests.test_approved_scope_channel import set_scope
from tests.test_review_mode import PLAIN, RENAME, arguments, set_mode, state, use_project

REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Reading the real project's log
# ---------------------------------------------------------------------------


def log_path(project: Path) -> Path:
    """The **real** project's append-only log — the one all six records go to."""
    return project / "dist" / AUDIT_FILENAME


def log_bytes(project: Path) -> bytes | None:
    """The log's bytes, or ``None`` when there is no log file at all.

    Bytes, because "leaves the log untouched" is a stronger claim than "leaves no
    audit record", and the one made about `discover` and `read`.
    """
    path = log_path(project)
    return path.read_bytes() if path.is_file() else None


def entries(project: Path) -> list[dict[str, Any]]:
    """Every audit entry, in insertion order — what `commit_block` takes."""
    raw = log_bytes(project)
    if raw is None:
        return []
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def sole_change_set_id(records: list[dict[str, Any]]) -> str:
    """The one id every record in *records* carries, asserted to be well-formed (an id a human can grep for)."""
    assert records, "the change set left no lifecycle record at all"
    identifiers = {record["args"]["change_set_id"] for record in records}
    assert len(identifiers) == 1, f"one change set left records under {len(identifiers)} ids: {sorted(identifiers)}"
    (identifier,) = identifiers
    assert CHANGE_SET_ID_PATTERN.match(identifier), identifier
    return identifier


def operations_of(records: list[dict[str, Any]]) -> list[str]:
    return [record["operation"] for record in records]


# ---------------------------------------------------------------------------
# Every change set leaves a record block under one id
# ---------------------------------------------------------------------------


class TestEveryChangeSetLeavesARecordBlockUnderOneId:
    """Three of the four outcomes, each with its records; rejection has its own class.

    Asserted for **equality and in order**: a subset assertion would miss a record
    missing or written twice.
    """

    def test_a_committed_change_set_records_the_whole_lifecycle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)

        structured, _texts = call_reviewed(ScriptedHuman(True), "change_set", **arguments(PLAIN, project))

        assert structured["status"] == "committed"
        records = lifecycle_records(project)
        assert operations_of(records) == [
            CHANGE_SET_STAGED,
            CHANGE_SET_APPROVED,
            CHANGE_SET_COMMIT_INTENT,
            CHANGE_SET_COMMITTED,
        ]
        # `audit_ref` promises this block is in the log, so it is the id the block is filed under.
        assert structured["audit_ref"] == sole_change_set_id(records)

    def test_a_staged_change_set_records_its_staging(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Staging is unconditional, so the record is written in a run with no review channel at all.
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        before = state(project)

        structured, _texts = call("change_set", **arguments(PLAIN, project))

        assert structured["status"] == "staged"
        assert state(project) == before
        records = lifecycle_records(project)
        assert operations_of(records) == [CHANGE_SET_STAGED]
        sole_change_set_id(records)

    def test_an_abandoned_change_set_records_the_answer_and_the_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        before = state(project)

        structured, _texts = call_reviewed(ScriptedHuman(DECLINE), "change_set", **arguments(PLAIN, project))

        assert structured["status"] == "abandoned"
        assert state(project) == before
        records = lifecycle_records(project)
        assert operations_of(records) == [CHANGE_SET_STAGED, CHANGE_SET_APPROVED, CHANGE_SET_ABANDONED]
        # The declining answer is recorded: `change_set_approved` is the only evidence a human answered.
        approved = records[1]
        assert approved["args"]["decision"] == "declined"
        sole_change_set_id(records)


# ---------------------------------------------------------------------------
# The three rejection points that precede staging
# ---------------------------------------------------------------------------
#
# Each helper returns the response and the model state captured before the call: a rejection must also
# leave the model byte-identical.

Rejection = Callable[[Path, pytest.MonkeyPatch], tuple[dict[str, Any], str]]


def _reject_at_pass_a(project: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], str]:
    """An operation name the protocol does not have: refused before a project is opened."""
    named = arguments(PLAIN, project)
    before = state(project)
    structured, _texts = call(
        "change_set",
        baseline=named["baseline"],
        operations=[{"operation": "rename", "address": {"layer": "swc", "element": "ExampleSWC"}}],
    )
    return structured, before


def _reject_at_the_staleness_check(project: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], str]:
    """The model moved after the token was read: the fail-fast check inside staging."""
    named = arguments(PLAIN, project)
    fixtures.make_stale(project)
    before = state(project)
    structured, _texts = call("change_set", **named)
    return structured, before


#: A `create` the passes would refuse on its own (a data type with no `kind`): gate 1
#: runs first, so the answer must be `unauthorized-delta`, never the passes' code.
INVALID_CREATE = {
    "operation": "create",
    "address": {"layer": "implementation_types", "element": "Unstageable"},
    "value": {"name": "Unstageable"},
}


def _reject_at_gate_one(project: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], str]:
    """A scoped run in which nothing is approved: gate 1, at step 0."""
    named = arguments(PLAIN, project)
    set_scope(monkeypatch, [])
    before = state(project)
    structured, _texts = call("change_set", baseline=named["baseline"], operations=[INVALID_CREATE])
    return structured, before


class TestARejectionBeforeStagingIsRecordedToo:
    """A refused change set leaves a record too."""

    @pytest.mark.parametrize(
        ("reject", "code"),
        [
            (_reject_at_pass_a, "schema-violation"),
            (_reject_at_the_staleness_check, "stale-baseline"),
            (_reject_at_gate_one, "unauthorized-delta"),
        ],
        ids=["pass-a", "staleness-check", "gate-1"],
    )
    def test_each_pre_staging_rejection_leaves_one_identifiable_record(
        self, reject: Rejection, code: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)

        structured, before = reject(project, monkeypatch)

        # The code identifies which of the three points refused.
        assert structured["response"] == "error"
        assert structured["error"] == code
        assert state(project) == before
        records = lifecycle_records(project)
        assert operations_of(records) == [CHANGE_SET_REJECTED]
        assert records[0]["args"]["error"] == code
        sole_change_set_id(records)

    def test_the_rejected_record_names_the_code_class_and_addresses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Gate 1's refusal names the uncovered addresses; the response and the record must agree on them.
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)

        structured, _before = _reject_at_gate_one(project, monkeypatch)

        assert structured["error"] == "unauthorized-delta"
        (record,) = lifecycle_records(project)
        assert record["args"]["failure_class"] == structured["failure_class"]
        assert record["args"]["addresses"] == structured["addresses"]
        assert record["args"]["addresses"], "an unauthorized-delta refusal names the uncovered addresses"


# ---------------------------------------------------------------------------
# `discover` and `read` write nothing at all
# ---------------------------------------------------------------------------


class TestDiscoverAndReadLeaveTheLogUntouched:
    """The read operations take no scope and record nothing, asserted over the log's **bytes**."""

    def test_neither_creates_a_log_in_a_project_that_has_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, None)
        assert log_bytes(project) is None, "the fixture project starts with no log"

        call("discover")
        call("read", address={"layer": "swc", "element": "ExampleSWC"})

        assert log_bytes(project) is None

    def test_neither_appends_a_byte_to_an_existing_log(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Against a log with content, so "unchanged" is a real comparison.
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        call_reviewed(ScriptedHuman(True), "change_set", **arguments(PLAIN, project))
        before = log_bytes(project)
        assert before

        call("discover")
        call("discover", layer="swc")
        call("read", address={"layer": "swc"})
        call("read", address={"layer": "swc", "element": "ExampleSWC", "path": [{"field": "ports"}]})

        assert log_bytes(project) == before

    @pytest.mark.parametrize(
        ("name", "argument", "code"),
        [
            ("discover", {"layer": "not-a-layer"}, "schema-violation"),
            ("read", {"address": {"layer": "swc", "element": "NoSuchSWC"}}, "broken-reference"),
        ],
        ids=["discover-refused", "read-refused"],
    )
    def test_a_refused_read_is_not_evidence_and_leaves_nothing(
        self, name: str, argument: dict[str, Any], code: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A refused change set is recorded, a refused read is not; `claims_change_set` draws the line on the
        # raw request.
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        call_reviewed(ScriptedHuman(True), "change_set", **arguments(PLAIN, project))
        before = log_bytes(project)

        structured, _texts = call(name, **argument)

        assert structured["error"] == code
        assert log_bytes(project) == before


# ---------------------------------------------------------------------------
# The commit block: two delimiters around every element write
# ---------------------------------------------------------------------------


def commit_one_cascade(project: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Commit the published rename cascade and return its change-set id; its block holds several writer entries."""
    set_mode(monkeypatch, server.REVIEW_ELICIT)
    structured, _texts = call_reviewed(ScriptedHuman(True, True), "change_set", **arguments(RENAME, project))
    assert structured["status"] == "committed", structured
    return structured["audit_ref"]


class TestTheCommitBlockBracketsEveryElementWrite:
    def test_the_two_delimiters_enclose_every_element_write_in_the_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(RENAME, tmp_path, monkeypatch)

        identifier = commit_one_cascade(project, monkeypatch)

        recorded = entries(project)
        block = commit_block(recorded, identifier)
        assert block is not None
        start = recorded.index(dict(block.intent))
        end = recorded.index(dict(block.committed))
        written = [index for index, entry in enumerate(recorded) if entry["operation"] in ELEMENT_WRITER_OPERATIONS]
        assert written, "the cascade committed without writing a single element"
        assert all(start < index < end for index in written), (
            f"an element write fell outside the commit block [{start}, {end}]: {written}"
        )

    def test_the_block_holds_element_writes_and_nothing_else(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Membership is positional (writer entries carry no `change_set_id`), so a stray writer entry
        # between the delimiters would silently join the block; a lifecycle record names its own change set.
        project = use_project(RENAME, tmp_path, monkeypatch)

        identifier = commit_one_cascade(project, monkeypatch)

        block = commit_block(entries(project), identifier)
        assert block is not None
        assert {entry["operation"] for entry in block.entries} <= ELEMENT_WRITER_OPERATIONS
        assert len(block.entries) > 1, "the rename cascade is here because it touches several elements"

    def test_the_turn_s_scope_id_is_on_every_lifecycle_record_and_on_no_writer_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Which records a `scope_id` filter finds, and which it does not.

        The six lifecycle records carry the turn's `scope_id`; the writer entries do
        not (the shadow session allocates its own `uuid4`), and the positional
        bracketing ties them to the turn instead. Asserted in both directions:
        threading the turn id through would be a design change.
        """
        from tests.test_approved_scope_channel import TURN, vector_scope

        project = use_project(RENAME, tmp_path, monkeypatch)
        set_scope(monkeypatch, vector_scope(RENAME), scope_id=TURN)

        identifier = commit_one_cascade(project, monkeypatch)

        recorded = entries(project)
        block = commit_block(recorded, identifier)
        assert block is not None
        lifecycle_entries = [entry for entry in recorded if entry["operation"] in LIFECYCLE_OPERATIONS]
        assert lifecycle_entries, "an empty set would make the equality below vacuous"
        assert {entry.get("scope_id") for entry in lifecycle_entries} == {TURN}
        assert block.entries, "the cascade wrote no element"
        writer_ids = {entry.get("scope_id") for entry in block.entries}
        assert TURN not in writer_ids, writer_ids
        assert len(writer_ids) == 1 and None not in writer_ids, writer_ids

    def test_the_committed_delta_and_the_writer_entries_agree_element_for_element(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(RENAME, tmp_path, monkeypatch)

        identifier = commit_one_cascade(project, monkeypatch)

        block = commit_block(entries(project), identifier)
        assert block is not None
        addresses = committed_element_addresses(block)
        assert addresses, "an empty equality would hold for a block that wrote nothing"
        assert addresses == written_element_addresses(block)
        assert block_is_complete(block)

    def test_the_equality_fails_when_a_writer_entry_is_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `block_is_complete` must be able to say no: both sides empty, or a set compared with itself, would pass.
        project = use_project(RENAME, tmp_path, monkeypatch)

        identifier = commit_one_cascade(project, monkeypatch)

        block = commit_block(entries(project), identifier)
        assert block is not None
        truncated = CommitBlock(
            change_set_id=block.change_set_id,
            intent=block.intent,
            committed=block.committed,
            entries=block.entries[:-1],
        )
        assert not block_is_complete(truncated)


class TestTheServersLinkRowsAreRecordedApart:
    """A ``confirm_links`` update's server rows are recorded as ``server_operations``.

    The staged record never credits the client with a row the server wrote, and the committed block still
    brackets every element write the delta names, the server's links included.
    """

    VECTOR = "update-requirement-version-confirming-links"

    def test_the_staged_record_lists_the_servers_links_and_the_block_is_complete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(self.VECTOR, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)

        structured, _texts = call_reviewed(ScriptedHuman(True), "change_set", **arguments(self.VECTOR, project))

        assert structured["status"] == "committed", structured
        (staged,) = [record for record in lifecycle_records(project) if record["operation"] == CHANGE_SET_STAGED]
        assert [row["operation"] for row in staged["args"]["operations"]] == ["update"]
        server_rows = staged["args"]["server_operations"]
        assert [row["operation"] for row in server_rows] == ["remove", "create"] * 3 + ["remove"]
        assert {row["address"]["layer"] for row in server_rows} == {"swc", "interfaces"}
        assert len(server_rows) == sum(1 for entry in structured["delta"] if entry.get("origin") == "server")
        block = commit_block(entries(project), structured["audit_ref"])
        assert block is not None
        assert committed_element_addresses(block) == written_element_addresses(block)
        assert block_is_complete(block)


# ---------------------------------------------------------------------------
# The interrupted apply, at the MCP surface
# ---------------------------------------------------------------------------


def fail_mid_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the first file for real, then fail — a disk that filled up mid-apply.

    Injected at `_apply_model_delta`, so every pre-apply step of the commit really ran.
    """
    original = session_mod._apply_model_delta

    def partial(delta: list[str], shadow_files: Any, real_model_root: Path) -> list[str]:
        original(delta[:1], shadow_files, real_model_root)
        raise OSError("injected: no space left on device")

    monkeypatch.setattr(session_mod, "_apply_model_delta", partial)


class TestAnInterruptedApplyLeavesTheCommitIntentSignature:
    """The engine-level contract, driven over the wire: a transport error, and the evidence in the log."""

    def test_the_call_fails_and_the_signature_stands_in_the_real_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(RENAME, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        named = arguments(RENAME, project)

        with monkeypatch.context() as injected:
            fail_mid_apply(injected)
            with pytest.raises(ToolError):
                call_reviewed(ScriptedHuman(True, True), "change_set", **named)

        recorded = entries(project)
        assert operations_of(lifecycle_records(project)) == [
            CHANGE_SET_STAGED,
            CHANGE_SET_APPROVED,
            CHANGE_SET_COMMIT_INTENT,
        ]
        (identifier,) = incomplete_commits(recorded)
        # Not a partial block: entries after an unterminated intent record are not known to belong to it.
        assert commit_block(recorded, identifier) is None

    def test_the_intent_record_names_the_addresses_to_inspect(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(RENAME, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)

        with monkeypatch.context() as injected:
            fail_mid_apply(injected)
            with pytest.raises(ToolError):
                call_reviewed(ScriptedHuman(True, True), "change_set", **arguments(RENAME, project))

        (intent,) = [entry for entry in entries(project) if entry["operation"] == CHANGE_SET_COMMIT_INTENT]
        assert intent["args"]["addresses"], "the intent record is what points at the elements to inspect"
        assert CHANGE_SET_ID_PATTERN.match(intent["args"]["change_set_id"])

    def test_a_completed_commit_leaves_no_signature(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The signature must not fire for every commit.
        project = use_project(RENAME, tmp_path, monkeypatch)

        commit_one_cascade(project, monkeypatch)

        assert incomplete_commits(entries(project)) == ()


# ---------------------------------------------------------------------------
# The six other retained specialized tools audit nothing
# ---------------------------------------------------------------------------


def retained_tools() -> set[str]:
    """The `retain-specialized` names from the published disposition table, less the withdrawn.

    Read from the table (``decision_table.load_projections``): a hand-kept copy would drift. A withdrawn row
    is no longer registered, so it has no call to make.
    """
    return set(live_retained(load_projections()))


class RetainedCall(NamedTuple):
    """One real invocation of a retained tool, and the *marker* proving it answered (a refusal also logs nothing)."""

    arguments: dict[str, Any]
    marker: str


#: One real call per retained tool, keyed by the tool's name, or `tool/form` for a
#: second call at another argument shape; the arguments name `examples/basic/model`'s elements.
RETAINED_CALLS: dict[str, RetainedCall] = {
    # The committed change set is the one thing HEAD lacks, so the diff naming it proves real history was read.
    "model_diff": RetainedCall({}, "VehicleSpeed_kph"),
    "impact_of": RetainedCall({"name": "ExampleSWC"}, "Impact of 'ExampleSWC'"),
    "resource_report": RetainedCall({}, '"response": "resources"'),
    # Not a retained row: the question added after the table, read-only and unaudited like the three above.
    "architecture_metrics": RetainedCall({"since": "HEAD"}, '"response": "metrics"'),
    # The second question added after the table: read-only, unaudited, and it reads no project at all.
    "guide": RetainedCall({"search": "reading"}, '"response": "guide"'),
    "build": RetainedCall({}, "needs.json generated"),
    "validate": RetainedCall({}, "ARXML generated"),
    "export_component": RetainedCall({"swc_name": "ExampleSWC"}, "Component export report: ExampleSWC"),
    "export_component/every-component": RetainedCall({}, "Component export report: ExampleConsumerSWC"),
}


def tool_of(label: str) -> str:
    """The tool a label calls: the part before the first `/`."""
    return label.partition("/")[0]


def git(*args: str, cwd: Path) -> None:
    """Mirrors `tests/test_diff_impact_tools.py`'s helper: `model_diff` needs history."""
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


class TestTheOtherRetainedToolsAreUnauditedAsBefore:
    """They write no record: none touches model content, and the server holds no auditor of its own."""

    def test_the_call_table_is_the_contracts_own_retained_list(self) -> None:
        # `import_component` has its own class below. Labels compare by the tool they reach; the questions
        # added after the table have no row and are swept too.
        assert {tool_of(label) for label in RETAINED_CALLS} | {"import_component"} == retained_tools() | ADDED_QUESTIONS

    @pytest.fixture()
    def committed_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """A project with git history and a committed change set already in its log."""
        project = use_project(PLAIN, tmp_path, monkeypatch)
        git("init", cwd=project)
        quiet_maintenance(project)
        git("add", "-A", cwd=project)
        git("commit", "-m", "the baseline model", cwd=project)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        structured, _texts = call_reviewed(ScriptedHuman(True), "change_set", **arguments(PLAIN, project))
        assert structured["status"] == "committed"
        # `guide` reads the installed guide, not the project: the fixture guide stands in for it.
        install_corpus(monkeypatch, tmp_path / "installed-guide", fixture_corpus())
        return project

    @pytest.mark.parametrize("name", sorted(RETAINED_CALLS), ids=sorted(RETAINED_CALLS))
    def test_the_tool_answers_and_appends_nothing_to_the_log(self, name: str, committed_project: Path) -> None:
        before = log_bytes(committed_project)
        assert before, "the sweep needs a non-empty log, or 'unchanged' proves nothing"
        invocation = RETAINED_CALLS[name]

        structured, texts = call(tool_of(name), **invocation.arguments)

        # A mapping body is rendered as JSON; a `str` tool's body is its text, which is not one.
        rendered = json.dumps(structured) if isinstance(structured, dict) else "\n".join(texts)
        assert "ERROR:" not in rendered, rendered
        assert invocation.marker in rendered, rendered
        assert log_bytes(committed_project) == before


class TestImportComponentIsTheExplicitException:
    """Planning writes nothing; the submission has the ordinary lifecycle (`tests/test_import_change_set.py`)."""

    def test_planning_leaves_the_model_and_the_log_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = use_project(PLAIN, tmp_path, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_ELICIT)
        call_reviewed(ScriptedHuman(True), "change_set", **arguments(PLAIN, project))
        export = str(tmp_path / "ExampleSWC.arxml")
        assert call("export_component", swc_name="ExampleSWC", output_path=export).structured["success"]
        baseline = call("discover").structured["baseline"]
        before_model, before_log = state(project), log_bytes(project)

        planned, _texts = call("import_component", source=export, baseline=baseline)

        assert planned["success"], planned
        assert state(project) == before_model
        assert log_bytes(project) == before_log
