"""`check_unknown_keys` -- `rule:model.unknown-key`.

A key of an element file that no field of its kind declares is dropped when the
model is read: `derived___from:` in place of `derived_from:` loses a
requirement's parent link and the model still loads. `check-model` and `validate`
report every such key as an **error**, one finding per key, at a canonical subject
spelled from the loaded element and ending in the key's own `{key: ...}` step; the
read-only commands still read the project.

What this module does **not** reach:

* **the leaf predicate's walk** -- which keys are undeclared, the union layers'
  discriminators, YAML 1.1 key spelling over source nodes -- is
  `test_undeclared_keys.py`'s;
* **the editor schemas as an independent oracle** over a planted corpus;
* **pass D through the engine** and the review's dropped keys. The protocol code and
  its place in `RULE_CODES` are asserted here; the message gate over a broken reference
  model (`test_editing_validator_messages.py`'s `TestNoValidatorMessageCarriesAStoragePath`)
  carries a `model.unknown-key` trigger of its own.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT
from pydantic import TypeAdapter
from scratch_git import quiet_maintenance
from swcraft_tools import cli
from swcraft_tools.diagnostics import Subject, field_step, key_step, member_step
from swcraft_tools.editing.address import collection_at, member_identity_of
from swcraft_tools.editing.diagnosis import CONSISTENCY_VIOLATION, RULE_CODES, subject_address
from swcraft_tools.model_registry import current_registry
from swcraft_tools.validator import (
    CONSISTENCY_RULES,
    check_cross_layer_consistency,
    check_unknown_keys,
    unknown_key_records,
)

RULE = "model.unknown-key"

_EXAMPLE_PROJECT = REPO_ROOT / "examples" / "basic"
_REQUIREMENT = "requirements/REQ_EXAMPLE_001.yaml"
_PROVIDER = "swc/example/ExampleSWC.yaml"
_TASK = "tasks/Task_10ms.yaml"


def _project_copy(tmp_path: Path) -> Path:
    """A private copy of ``examples/basic``: the manifest and the model, not the outputs."""
    target = tmp_path / "project"
    shutil.copytree(_EXAMPLE_PROJECT, target, ignore=shutil.ignore_patterns("dist", "views", "__pycache__"))
    return target


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _put(path: Path, document: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _change(project: Path, relative: str, mutate: Any) -> None:
    path = project / "model" / relative
    document = _load(path)
    mutate(document)
    _put(path, document)


def _append(project: Path, relative: str, text: str) -> None:
    path = project / "model" / relative
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8", newline="\n")


def _run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    """One in-process ``swcraft`` run: its exit code, stdout and stderr."""
    monkeypatch.setattr(sys, "argv", ["swcraft", *argv])
    try:
        cli.main()
        code = 0
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else (0 if exit_.code is None else 1)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _findings(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], project: Path) -> tuple[int, list]:
    code, out, _ = _run(
        monkeypatch, capsys, "check-model", "--project", str(project / "swcraft.yaml"), "--format", "json"
    )
    return code, [finding for finding in json.loads(out)["findings"] if finding["rule"] == RULE]


def _records(project: Path) -> list:
    return [record for record in check_unknown_keys(str(project / "model")).records if record.rule == RULE]


# ---------------------------------------------------------------------------
# The rule's place
# ---------------------------------------------------------------------------


def test_the_rule_is_an_error_with_its_own_protocol_group_appended_last() -> None:
    """An error rule needs a code; this one is appended LAST so it never stands first in pass D."""
    assert RULE in CONSISTENCY_RULES
    assert RULE_CODES[RULE].code == CONSISTENCY_VIOLATION
    assert RULE_CODES[RULE].group == RULE
    assert list(RULE_CODES)[-1] == RULE


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def test_a_misspelt_key_in_a_requirement_is_one_error_naming_the_nearest_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``derived___from:`` is reported, the model still loads, and the exit is 1."""
    project = _project_copy(tmp_path)
    _append(project, _REQUIREMENT, "derived___from:\n- REQ_OTHER\n")

    code, findings = _findings(monkeypatch, capsys, project)

    assert code == 1
    assert len(findings) == 1
    (finding,) = findings
    assert finding["severity"] == "error"
    assert finding["message"] == (
        "Element 'REQ_EXAMPLE_001' of layer `requirements` carries the key 'derived___from', which kind "
        "`requirement` does not declare; it is dropped when the model is read. Rename the key to the field it is "
        "meant to be, or delete it; the nearest declared key is 'derived_from'."
    )
    assert finding["subjects"] == [
        {"layer": "requirements", "element": "REQ_EXAMPLE_001", "path": [{"key": "derived___from"}]}
    ]


def test_a_misspelt_key_in_a_port_is_the_nested_subject(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _change(project, _PROVIDER, lambda d: d["ports"][0].update(intrface="IExample"))

    (record,) = _records(project)

    assert record.subjects == (
        Subject("swc", "ExampleSWC", (field_step("ports"), member_step("pOut"), key_step("intrface"))),
    )
    assert "Element 'ExampleSWC' of layer `swc`, at port 'pOut', carries the key 'intrface'" in record.message
    assert "which kind `port` does not declare" in record.message


def test_two_keys_in_one_file_are_two_findings_in_document_order(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _append(project, _REQUIREMENT, "zeta_key: 1\nalpha_key: 2\n")

    keys = [record.subjects[0].path[-1].value for record in _records(project)]

    assert keys == ["zeta_key", "alpha_key"]


def test_validate_reports_the_same_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project_copy(tmp_path)
    _append(project, _REQUIREMENT, "derived___from:\n- REQ_OTHER\n")

    code, out, err = _run(monkeypatch, capsys, "validate", "--project", str(project / "swcraft.yaml"))

    assert code == 1
    assert "carries the key 'derived___from'" in out + err


# ---------------------------------------------------------------------------
# The canonical subject
# ---------------------------------------------------------------------------


def _loaded(project: Path, layer: str, relative: str) -> Any:
    registry = current_registry()
    return TypeAdapter(registry.layer(layer).element_annotation).validate_python(_load(project / "model" / relative))


def test_a_task_entry_without_an_instance_is_spelled_with_instance_null(tmp_path: Path) -> None:
    """A composite member's absent optional identity field is ``null``, as every protocol address spells it."""
    project = _project_copy(tmp_path)
    _change(project, _TASK, lambda d: d["runnables"][0].update(prioritty=1))

    (record,) = _records(project)

    identity = {"swc": "ExampleConsumerSWC", "instance": None, "runnable": "ExampleConsumerRun"}
    assert record.subjects == (
        Subject("tasks", "Task_10ms", (field_step("runnables"), member_step(identity), key_step("prioritty"))),
    )
    task = _loaded(project, "tasks", _TASK)
    descriptor = collection_at("tasks", [{"field": "runnables"}])
    assert member_identity_of(task.runnables[0], descriptor) == identity
    assert subject_address(record.subjects[0]).to_json() == record.subjects[0].to_json()


def test_a_timing_event_written_with_its_defaults_is_spelled_by_its_reduced_identity(tmp_path: Path) -> None:
    """``name: null`` and ``disabled_modes: []`` are left out of the event's identity, as the loader's member has it."""
    project = _project_copy(tmp_path)
    _change(
        project,
        _PROVIDER,
        lambda d: d["runnables"][0]["events"][0].update(name=None, disabled_modes=[], perod=1),
    )

    (record,) = _records(project)

    identity = {"type": "timing", "period": 0.01}
    assert record.subjects == (
        Subject(
            "swc",
            "ExampleSWC",
            (
                field_step("runnables"),
                member_step("ExampleRun"),
                field_step("events"),
                member_step(identity),
                key_step("perod"),
            ),
        ),
    )
    swc = _loaded(project, "swc", _PROVIDER)
    descriptor = collection_at("swc", [{"field": "runnables"}, {"member": "ExampleRun"}, {"field": "events"}])
    assert member_identity_of(swc.runnables[0].events[0], descriptor) == identity
    assert "at timing event {type: timing, period: 0.01} of runnable 'ExampleRun'" in record.message


def test_two_files_declaring_one_name_each_give_their_own_finding(tmp_path: Path) -> None:
    """The element is the one THAT file declares, never one looked up by identity."""
    project = _project_copy(tmp_path)
    interfaces = project / "model" / "interfaces"
    _put(
        interfaces / "Dup.yaml",
        {
            "name": "Dup",
            "kind": "sender_receiver",
            "data_elements": [
                {"name": "a", "implementation_type": "float32"},
                {"name": "b", "implementation_type": "float32", "typo_one": 1},
            ],
        },
    )
    _put(
        interfaces / "Dup~1.yaml",
        {"name": "Dup", "kind": "client_server", "operations": [{"name": "op", "typo_two": 2}]},
    )

    subjects = [record.subjects[0] for record in _records(project)]

    assert sorted(subjects, key=lambda subject: str(subject.path[-1].value)) == [
        Subject("interfaces", "Dup", (field_step("data_elements"), member_step("b"), key_step("typo_one"))),
        Subject("interfaces", "Dup", (field_step("operations"), member_step("op"), key_step("typo_two"))),
    ]


def test_an_empty_key_ends_its_subject_at_the_mapping(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _change(project, _PROVIDER, lambda d: d["ports"][0].update({"": 1}))

    (record,) = _records(project)

    assert record.subjects == (Subject("swc", "ExampleSWC", (field_step("ports"), member_step("pOut"))),)
    assert "carries an empty key" in record.message


# ---------------------------------------------------------------------------
# Success and the hint
# ---------------------------------------------------------------------------


def test_the_example_unchanged_has_no_finding(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)

    assert _records(project) == []
    assert RULE not in {record.rule for record in check_cross_layer_consistency(str(project / "model")).records}


def test_a_key_with_no_close_match_has_no_nearest_clause(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _append(project, _REQUIREMENT, "zzz_unrelated: 1\n")

    (record,) = _records(project)

    assert "nearest" not in record.message
    assert record.message.endswith("Rename the key to the field it is meant to be, or delete it.")


def test_the_nearest_key_is_never_one_the_mapping_already_holds_at_the_top(tmp_path: Path) -> None:
    # `titel` is 0.8 from `title`, which the requirement holds: the hint must not send the user to it.
    project = _project_copy(tmp_path)
    _append(project, _REQUIREMENT, "titel: A second title\n")

    (record,) = _records(project)

    assert "carries the key 'titel'" in record.message
    assert "the nearest declared key is 'title'" not in record.message


def test_the_nearest_key_is_never_one_the_mapping_already_holds_in_a_member(tmp_path: Path) -> None:
    # The port holds `interface` beside the misspelt `intrface`, which would otherwise be its nearest key.
    project = _project_copy(tmp_path)
    _change(project, _PROVIDER, lambda d: d["ports"][0].update(intrface="IExample"))

    (record,) = _records(project)

    assert "the nearest declared key is 'interface'" not in record.message


def test_a_key_in_an_endpoint_is_placed_at_its_field(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    connector = project / "model" / "connectors" / "Example_to_Consumer.yaml"
    connector.write_text(
        connector.read_text(encoding="utf-8").replace("  port: pOut\n", "  port: pOut\n  prt: pOut\n", 1),
        encoding="utf-8",
        newline="\n",
    )

    (record,) = _records(project)

    assert record.subjects == (Subject("connectors", "Example_to_Consumer", (field_step("from"), key_step("prt"))),)
    assert record.message == (
        "Element 'Example_to_Consumer' of layer `connectors`, at field `from`, carries the key 'prt', which kind "
        "`connector_endpoint` does not declare; it is dropped when the model is read. Rename the key to the field "
        "it is meant to be, or delete it."
    )


#: A component whose operation event and data-received event each carry a stray key.
#: The two identities below are the ones the extension's tests place
#: (``vscode_extension/src/diagnosticsCore.test.ts``, the ``model.unknown-key`` placement):
#: a change to their shape here is a change there.
_EVENT_SWC = """name: EventSWC
type: application
ports:
  - name: pSrv
    direction: provide
    interface: IServer
  - name: rIn
    direction: require
    interface: IExample
runnables:
  - name: Serve
    events:
      - type: operation
        port: pSrv
        operation: op
        zz_op: 1
  - name: OnData
    events:
      - type: data
        port: rIn
        element: value
        zz_data: 1
"""


def test_an_operation_and_a_data_event_are_spelled_by_their_string_identities(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    (project / "model" / "swc" / "example" / "EventSWC.yaml").write_text(_EVENT_SWC, encoding="utf-8", newline="\n")

    subjects = [record.subjects[0].to_json() for record in _records(project)]

    assert subjects == [
        {
            "layer": "swc",
            "element": "EventSWC",
            "path": [
                {"field": "runnables"},
                {"member": "Serve"},
                {"field": "events"},
                {"member": {"type": "operation", "port": "pSrv", "operation": "op"}},
                {"key": "zz_op"},
            ],
        },
        {
            "layer": "swc",
            "element": "EventSWC",
            "path": [
                {"field": "runnables"},
                {"member": "OnData"},
                {"field": "events"},
                {"member": {"type": "data", "port": "rIn", "element": "value"}},
                {"key": "zz_data"},
            ],
        },
    ]


def test_a_file_the_check_cannot_load_is_skipped_without_a_finding(tmp_path: Path) -> None:
    # Both are the loader's to refuse: a closed kind's stray key fails validation, and a
    # file that is no YAML fails the parse. Neither is this rule's finding, and neither raises.
    project = _project_copy(tmp_path)
    base = project / "model" / "data_types" / "base" / "float32_base.yaml"
    base.write_text(base.read_text(encoding="utf-8") + "zz_closed: 1\n", encoding="utf-8", newline="\n")
    broken = project / "model" / "requirements" / "REQ_BROKEN.yaml"
    broken.write_text("id: [unclosed\nzz_broken: 1\n", encoding="utf-8", newline="\n")

    assert check_unknown_keys(str(project / "model")).records == ()


def test_a_task_carrying_core_beside_partition_is_one_finding_on_core(tmp_path: Path) -> None:
    """``schema.py``'s ``Task`` comment: the file loads, and ``core`` is the rule's error."""
    project = _project_copy(tmp_path)
    _change(project, _TASK, lambda d: d.update(core="CORE0"))

    (record,) = _records(project)

    assert record.subjects == (Subject("tasks", "Task_10ms", (key_step("core"),)),)


def test_the_binding_callable_reads_no_disk() -> None:
    """``unknown_key_records`` builds findings from texts and loaded elements alone."""
    registry = current_registry()
    text = "id: REQ_A\ntitle: A\ndescription: A\nreq_type: req\nversion: 1\nsource: architecture\nderived___from: []\n"
    element = TypeAdapter(registry.layer("requirements").element_annotation).validate_python(yaml.safe_load(text))

    (record,) = unknown_key_records({"requirements": [(element, text)]})

    assert record.subjects == (Subject("requirements", "REQ_A", (key_step("derived___from"),)),)


# ---------------------------------------------------------------------------
# The read-only commands still read the project
# ---------------------------------------------------------------------------


def _git(project: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *argv],
        cwd=project,
        check=True,
        capture_output=True,
    )


@pytest.mark.parametrize("command", [("list",), ("query", "requirements"), ("diff",)], ids=lambda c: c[0])
def test_the_read_only_commands_exit_zero_on_the_planted_copy(
    command: tuple[str, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project_copy(tmp_path)
    _git(project, "init", "-q")
    quiet_maintenance(project)
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "base")
    _append(project, _REQUIREMENT, "derived___from:\n- REQ_OTHER\n")

    code, _, err = _run(monkeypatch, capsys, *command, "--project", str(project / "swcraft.yaml"))

    assert code == 0, err


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------


def test_the_message_names_no_storage_location(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    _change(project, _PROVIDER, lambda d: d["ports"][0].update(intrface="IExample"))

    (record,) = _records(project)

    for marker in (str(tmp_path), tmp_path.as_posix(), "ExampleSWC.yaml", "swc/example", ".yaml"):
        assert marker not in record.message


def test_a_yaml_1_1_boolean_key_is_spelled_as_written(tmp_path: Path) -> None:
    """``yes:`` loads as ``True``; the finding names it ``yes``, never ``True``."""
    project = _project_copy(tmp_path)
    _append(project, _REQUIREMENT, "yes: 1\n")

    (record,) = _records(project)

    assert "carries the key 'yes'" in record.message
    assert record.subjects == (Subject("requirements", "REQ_EXAMPLE_001", (key_step("yes"),)),)


def test_a_merged_discriminator_beside_a_yes_key_reports_every_stray_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """End to end: ``kind`` only through ``<<``, a ``yes:`` and a misspelt key.

    ``yes:`` sends the leaf to the source nodes; ``<<`` is resolved there as the loader
    resolves it, so the interface's kind is chosen and neither stray is skipped.
    """
    project = _project_copy(tmp_path)
    (project / "model" / "interfaces" / "IExample.yaml").write_text(
        "<<: {kind: sender_receiver}\n"
        "name: IExample\n"
        "yes: 1\n"
        "data_elemnts: []\n"
        "data_elements:\n"
        "  - name: value\n"
        "    implementation_type: float32\n",
        encoding="utf-8",
        newline="\n",
    )

    code, findings = _findings(monkeypatch, capsys, project)

    assert code == 1
    assert len(findings) == 2
    assert "the key 'yes'" in findings[0]["message"]
    assert "the key 'data_elemnts'" in findings[1]["message"]
