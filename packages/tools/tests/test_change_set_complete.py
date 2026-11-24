"""``change_set`` with ``complete: true`` — the server adds a unique repair, marked.

A component created speaking an application type that no set it names maps is refused with
``consistency-violation``; with ``complete: true`` the server names the one declared, conflict-free set that maps
the type in the component's ``data_type_mappings``, stages the set again and marks the entry it added to in the
delta, which the human reviews like any other row. The completion reaches only the components the client's own
operations create or update, fires only when every finding is one it repairs, answers a refusal of the completed
set with the first run's refusal unchanged, and the audit records the server's operations apart from the
client's.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import swcraft_tools.editing.registry as reg
from conftest import REPO_ROOT
from swcraft_tools.auditor import AuditLog
from swcraft_tools.editing.address import Address, FieldStep, MemberStep
from swcraft_tools.editing.audit import AUDIT_FILENAME, CHANGE_SET_REJECTED, CHANGE_SET_STAGED
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import ChangeSetProposal, _mark_server_entries, handle_request
from swcraft_tools.editing.protocol import CreateOperation, DeltaEntry, UpdateOperation
from swcraft_tools.editing.review import render_delta_review


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "basic"
    shutil.copytree(REPO_ROOT / "examples" / "basic", root, ignore=shutil.ignore_patterns("dist", "views"))
    model = root / "model"
    _write(model / "data_types/application/Temp_C.yaml", "name: Temp_C\nkind: application_type\n")
    _write(
        model / "interfaces/ITemp.yaml",
        "name: ITemp\nkind: sender_receiver\ndata_elements:\n  - name: t\n    application_type: Temp_C\n",
    )
    return root


def _carrier(project: Path, *names: str) -> None:
    for name in names:
        _write(
            project / f"model/data_types/mapping_sets/{name}.yaml",
            f"name: {name}\nmaps:\n  - application: Temp_C\n    implementation: float32\n",
        )


def _request(project: Path, *, complete: bool) -> dict[str, Any]:
    baseline = handle_request({"operation": "discover", "form": "baseline"}, project=project)["baseline"]
    logger = {
        "name": "Logger",
        "group": "example",
        "type": "application",
        "ports": [{"name": "rTemp", "direction": "require", "interface": "ITemp"}],
        "memory": {"RAM0": 0},
        "handle_termination_and_restart": "no_support",
    }
    request: dict[str, Any] = {
        "operation": "change_set",
        "baseline": baseline,
        "operations": [{"operation": "create", "address": {"layer": "swc", "element": "Logger"}, "value": logger}],
    }
    if complete:
        request["complete"] = True
    return request


def _approve(seen: list[ChangeSetProposal]):
    def review(proposal: ChangeSetProposal) -> ApprovalDecision:
        seen.append(proposal)
        return ApprovalDecision(change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True)

    return review


def test_without_the_flag_the_unique_repair_is_named_and_refused(project: Path) -> None:
    _carrier(project, "Thermal")
    response = handle_request(_request(project, complete=False), project=project)
    assert response["error"] == "consistency-violation"
    assert "Set 'Thermal' is the one declared set" in response["message"]
    assert "name it in the component's data_type_mappings" in response["message"]


def test_with_the_flag_the_server_adds_the_set_and_marks_the_entry(project: Path) -> None:
    _carrier(project, "Thermal")
    seen: list[ChangeSetProposal] = []
    response = handle_request(_request(project, complete=True), project=project, review=_approve(seen))
    assert response["status"] == "committed"
    (entry,) = response["delta"]
    assert entry["origin"] == "client_and_server"
    assert entry["after"]["data_type_mappings"] == ["Thermal"]
    assert "[the server added to this]" in render_delta_review(seen[0])


def test_a_repair_that_is_not_unique_is_refused_with_the_candidates(project: Path) -> None:
    _carrier(project, "Thermal", "Thermal2")
    response = handle_request(_request(project, complete=True), project=project)
    assert response["error"] == "consistency-violation"
    assert "'Thermal', 'Thermal2'" in response["message"]


def test_a_member_added_to_an_existing_element_is_the_servers_entry(project: Path) -> None:
    _carrier(project, "Thermal")
    request = _request(project, complete=False)
    handle_request(
        {
            **request,
            "operations": [{**request["operations"][0], "value": {**request["operations"][0]["value"], "ports": []}}],
        },
        project=project,
        review=_approve([]),
    )
    baseline = handle_request({"operation": "discover", "form": "baseline"}, project=project)["baseline"]
    port = {"name": "rTemp", "direction": "require", "interface": "ITemp"}
    response = handle_request(
        {
            "operation": "change_set",
            "baseline": baseline,
            "complete": True,
            "operations": [
                {
                    "operation": "create",
                    "address": {"layer": "swc", "element": "Logger", "path": [{"field": "ports"}, {"member": "rTemp"}]},
                    "value": port,
                }
            ],
        },
        project=project,
        review=_approve([]),
    )
    assert response["status"] == "committed"
    origins = {entry["address"]["path"][0]["field"]: entry.get("origin") for entry in response["delta"]}
    assert origins == {"ports": None, "data_type_mappings": "server"}


def _records(project: Path, operation: str) -> list[dict[str, Any]]:
    return [
        entry
        for entry in AuditLog(str(project / "dist" / AUDIT_FILENAME)).get_entries()
        if entry["operation"] == operation
    ]


def _baseline(project: Path) -> str:
    return handle_request({"operation": "discover", "form": "baseline"}, project=project)["baseline"]


def test_the_audit_records_the_servers_operations_apart_with_the_flag(project: Path) -> None:
    _carrier(project, "Thermal")
    response = handle_request(_request(project, complete=True), project=project, review=_approve([]))
    assert response["status"] == "committed"
    (staged,) = _records(project, CHANGE_SET_STAGED)
    assert staged["args"]["operations"] == [{"operation": "create", "address": {"layer": "swc", "element": "Logger"}}]
    assert staged["args"]["server_operations"] == [
        {
            "operation": "create",
            "address": {
                "layer": "swc",
                "element": "Logger",
                "path": [{"field": "data_type_mappings"}, {"member": "Thermal"}],
            },
        }
    ]
    assert staged["args"]["complete"] is True
    assert "1 added by the server" in staged["result"]


def test_a_change_set_with_nothing_to_complete_records_the_keys_empty(project: Path) -> None:
    _carrier(project, "Thermal")
    request = _request(project, complete=False)
    request["operations"][0]["value"]["data_type_mappings"] = ["Thermal"]
    assert handle_request(request, project=project)["status"] == "staged"
    (staged,) = _records(project, CHANGE_SET_STAGED)
    assert staged["args"]["server_operations"] == []
    assert staged["args"]["complete"] is False


def _watcher(project: Path) -> None:
    """An existing component speaking ITemp through Fahrenheit, which maps Temp_F; Thermal maps Temp_C."""
    _write(project / "model/data_types/application/Temp_F.yaml", "name: Temp_F\nkind: application_type\n")
    _write(
        project / "model/interfaces/ITemp.yaml",
        "name: ITemp\nkind: sender_receiver\ndata_elements:\n  - name: t\n    application_type: Temp_F\n",
    )
    _write(
        project / "model/data_types/mapping_sets/Fahrenheit.yaml",
        "name: Fahrenheit\nmaps:\n  - application: Temp_F\n    implementation: float32\n",
    )
    _carrier(project, "Thermal")
    _write(
        project / "model/swc/example/Watcher.yaml",
        "name: Watcher\ntype: application\nports:\n  - name: rTemp\n    direction: require\n    interface: ITemp\n"
        "memory:\n  RAM0: 0\nhandle_termination_and_restart: no_support\ndata_type_mappings:\n  - Fahrenheit\n",
    )


def test_a_component_the_change_set_never_touched_is_refused_as_usual(project: Path) -> None:
    _watcher(project)
    request = {
        "operation": "change_set",
        "baseline": _baseline(project),
        "complete": True,
        "operations": [
            {
                "operation": "update",
                "address": {
                    "layer": "interfaces",
                    "element": "ITemp",
                    "path": [{"field": "data_elements"}, {"member": "t"}, {"field": "application_type"}],
                },
                "value": "Temp_C",
            }
        ],
    }
    response = handle_request(request, project=project)
    assert response["error"] == "consistency-violation"
    assert response["addresses"] == [{"layer": "swc", "element": "Watcher", "path": [{"field": "data_type_mappings"}]}]
    assert "SWC 'Watcher'" in response["message"]
    assert "Set 'Thermal' is the one declared set" in response["message"]
    assert _records(project, CHANGE_SET_STAGED) == []
    assert "Thermal" not in (project / "model/swc/example/Watcher.yaml").read_text(encoding="utf-8")


def test_a_carrier_that_pairs_a_mapped_type_twice_is_no_repair(project: Path) -> None:
    _write(project / "model/data_types/application/Press_P.yaml", "name: Press_P\nkind: application_type\n")
    _write(
        project / "model/interfaces/ITemp.yaml",
        "name: ITemp\nkind: sender_receiver\ndata_elements:\n  - name: t\n    application_type: Temp_C\n"
        "  - name: p\n    application_type: Press_P\n",
    )
    _carrier(project, "Thermal")
    _write(
        project / "model/data_types/mapping_sets/Both.yaml",
        "name: Both\nmaps:\n  - application: Press_P\n    implementation: uint16\n"
        "  - application: Temp_C\n    implementation: uint16\n",
    )
    request = _request(project, complete=True)
    request["operations"][0]["value"]["data_type_mappings"] = ["Thermal"]
    response = handle_request(request, project=project)
    assert response["error"] == "consistency-violation"
    assert "speaks type 'Press_P'" in response["message"]
    assert "Declared sets that carry it: 'Both'." in response["message"]
    assert (
        "Naming 'Both' would pair a type the component maps with a second implementation type." in response["message"]
    )
    assert "double" not in response["message"]
    assert _records(project, CHANGE_SET_STAGED) == []


@pytest.mark.parametrize("scoped", [False, True], ids=["unscoped", "scoped"])
def test_an_unrelated_finding_answers_the_first_refusal_unchanged(project: Path, scoped: bool) -> None:
    _carrier(project, "Thermal")
    without = _request(project, complete=False)
    without["operations"][0]["value"]["ports"].append({"name": "rNone", "direction": "require", "interface": "INone"})
    scope = [{"operation": "create", "address": {"layer": "swc", "element": "Logger"}}] if scoped else None
    refused = handle_request(without, project=project, approved_scope=scope)
    # Pass D's findings: the undeclared interface, and the unmapped type the completion would repair.
    assert refused["error"] == "broken-reference"
    assert [violation["error"] for violation in refused["additional"]] == ["consistency-violation"]
    completed = handle_request({**without, "complete": True}, project=project, approved_scope=scope)
    assert completed == refused
    assert _records(project, CHANGE_SET_STAGED) == []
    assert len(_records(project, CHANGE_SET_REJECTED)) == 2


def _port_request(project: Path) -> dict[str, Any]:
    """Logger committed without ports, then a change set adding the port that speaks Temp_C."""
    request = _request(project, complete=False)
    request["operations"][0]["value"]["ports"] = []
    assert handle_request(request, project=project, review=_approve([]))["status"] == "committed"
    return {
        "operation": "change_set",
        "baseline": _baseline(project),
        "operations": [
            {
                "operation": "create",
                "address": {"layer": "swc", "element": "Logger", "path": [{"field": "ports"}, {"member": "rTemp"}]},
                "value": {"name": "rTemp", "direction": "require", "interface": "ITemp"},
            }
        ],
    }


def test_a_scope_that_does_not_cover_the_servers_row_answers_the_first_refusal(project: Path) -> None:
    _carrier(project, "Thermal")
    request = _port_request(project)
    scope = [{"operation": "create", "address": request["operations"][0]["address"]}]
    refused = handle_request(request, project=project, approved_scope=scope)
    assert refused["error"] == "consistency-violation"
    completed = handle_request({**request, "complete": True}, project=project, approved_scope=scope)
    # Never the `unauthorized-delta` the server's own create would have drawn.
    assert completed == refused
    assert _records(project, CHANGE_SET_STAGED)[1:] == []


def test_a_scope_over_the_created_element_covers_the_servers_row(project: Path) -> None:
    _carrier(project, "Thermal")
    scope = [{"operation": "create", "address": {"layer": "swc", "element": "Logger"}}]
    response = handle_request(_request(project, complete=True), project=project, approved_scope=scope)
    assert response["status"] == "staged"


def test_the_servers_own_entry_is_headlined_as_added_by_the_server(project: Path) -> None:
    _carrier(project, "Thermal")
    request = _port_request(project)
    seen: list[ChangeSetProposal] = []
    response = handle_request({**request, "complete": True}, project=project, review=_approve(seen))
    assert response["status"] == "committed"
    assert "[added by the server]" in render_delta_review(seen[0])


def test_a_field_entry_holding_a_server_member_is_both_parties_and_a_sibling_is_neither() -> None:
    def entry(*path: Any) -> DeltaEntry:
        return DeltaEntry(operation="update", address=Address(layer="swc", element="Logger", path=path), after=[])

    server = (
        CreateOperation(
            address=Address(
                layer="swc", element="Logger", path=(FieldStep("data_type_mappings"), MemberStep("Thermal"))
            ),
            value="Thermal",
        ),
    )
    # The client's own operation touches Logger: a containing entry is both parties' only then
    # (`test_answer_forms.py::TestOrigin` holds the other outcomes).
    client = (UpdateOperation(address=Address(layer="swc", element="Logger", path=(FieldStep("ports"),)), value=[]),)
    marked = _mark_server_entries(
        [entry(FieldStep("data_type_mappings")), entry(FieldStep("ports"))], server, reg.current(), client
    )
    assert [item.origin for item in marked] == ["client_and_server", None]


def test_an_undeclared_origin_is_refused_on_construction_and_off_the_wire() -> None:
    with pytest.raises(ValueError, match="origin"):
        DeltaEntry(operation="create", address=Address(layer="swc", element="Logger"), origin="agent")
    with pytest.raises(ValueError, match="origin"):
        DeltaEntry.from_json(
            {"operation": "create", "address": {"layer": "swc", "element": "Logger"}, "origin": "agent"}
        )


@pytest.mark.parametrize("value", ["true", 1, None], ids=["string", "integer", "null"])
def test_a_complete_that_is_not_a_boolean_is_a_schema_violation(project: Path, value: Any) -> None:
    request = {**_request(project, complete=False), "complete": value}
    response = handle_request(request, project=project)
    assert response["error"] == "schema-violation"
    assert "complete must be a boolean" in response["message"]


def test_a_repair_that_would_reverse_the_clients_own_remove_answers_the_first_refusal(project: Path) -> None:
    """A client that removes the one carrier is refused, never reversed into an empty committed change set."""
    _carrier(project, "Thermal")
    request = _request(project, complete=False)
    request["operations"][0]["value"]["data_type_mappings"] = ["Thermal"]
    assert handle_request(request, project=project, review=_approve([]))["status"] == "committed"
    path = [{"field": "data_type_mappings"}, {"member": "Thermal"}]
    without = {
        "operation": "change_set",
        "baseline": _baseline(project),
        "operations": [{"operation": "remove", "address": {"layer": "swc", "element": "Logger", "path": path}}],
    }
    refused = handle_request(without, project=project)
    assert refused["error"] == "consistency-violation"
    completed = handle_request({**without, "complete": True}, project=project, review=_approve([]))
    assert completed == refused
    assert "Thermal" in (project / "model/swc/example/Logger.yaml").read_text(encoding="utf-8")


def test_a_component_renamed_in_the_change_set_is_completed_under_its_new_name(project: Path) -> None:
    """A rename is a remove and a create: the create of the new name is a touch, the remove of the old is not."""
    _carrier(project, "Thermal")
    request = _request(project, complete=False)
    request["operations"][0]["value"]["data_type_mappings"] = ["Thermal"]
    assert handle_request(request, project=project, review=_approve([]))["status"] == "committed"
    renamed = {**request["operations"][0]["value"], "name": "Recorder"}
    del renamed["data_type_mappings"]
    response = handle_request(
        {
            "operation": "change_set",
            "baseline": _baseline(project),
            "complete": True,
            "operations": [
                {"operation": "remove", "address": {"layer": "swc", "element": "Logger"}},
                {"operation": "create", "address": {"layer": "swc", "element": "Recorder"}, "value": renamed},
            ],
        },
        project=project,
        review=_approve([]),
    )
    assert response["status"] == "committed"
    created = next(entry for entry in response["delta"] if entry["address"]["element"] == "Recorder")
    assert created["origin"] == "client_and_server"
    assert created["after"]["data_type_mappings"] == ["Thermal"]


def test_an_update_of_any_field_touches_the_component_and_completes_its_earlier_unmapped_type(project: Path) -> None:
    """'Create or update' is read off the operations, not off what caused the finding."""
    _watcher(project)
    _write(
        project / "model/interfaces/ITemp.yaml",
        "name: ITemp\nkind: sender_receiver\ndata_elements:\n  - name: t\n    application_type: Temp_C\n",
    )
    _write(project / "model/data_types/mapping_sets/Fahrenheit.yaml", "name: Fahrenheit\nmaps: []\n")
    response = handle_request(
        {
            "operation": "change_set",
            "baseline": _baseline(project),
            "complete": True,
            "operations": [
                {
                    "operation": "update",
                    "address": {"layer": "swc", "element": "Watcher", "path": [{"field": "memory"}, {"key": "RAM0"}]},
                    "value": 1,
                }
            ],
        },
        project=project,
        review=_approve([]),
    )
    assert response["status"] == "committed"
    origins = {entry["address"]["path"][0]["field"]: entry.get("origin") for entry in response["delta"]}
    assert origins == {"memory": None, "data_type_mappings": "server"}
