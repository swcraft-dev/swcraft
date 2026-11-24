"""The operation event and ``concurrent`` through both write paths.

``writer/yaml_documents.py``'s hand-written ``swc_to_dict`` is what both importers, the component export and every
``add_*`` mutation write through; ``writer/elements.py``'s ``write_element`` is the editing protocol's. A serializer
that wrote every non-timing event as ``{"type": "init"}`` would make an export emit an ``INIT-EVENT`` in an
operation event's place. This module holds:

* **the hand-written serializer**: each event written as its kind (one arm per kind, a refusal for anything else),
  ``concurrent`` only when ``true`` and in the contract's field order, equal, key order included, to
  ``element_to_dict``;
* **the ``add_*`` mutations** keep the operation event and ``concurrent: true``;
* **the component export** keeps both and emits no ``INIT-EVENT`` in the event's place;
* **the editing protocol**: create, replacement (a ``remove`` plus a ``create``; a member ``update`` is
  ``immutable-field``), reordering, removal (of the ancestor runnable too), ``concurrent`` set and cleared,
  ``read``, and an over-budget ``OIT_`` pair refused as ``invalid-identifier``;
* **the authorization boundary**: each act yields an **empty** safety verdict while scope and delta review apply.

All of it runs over the mixed-event fixture, whose mixing SWCraft does not judge.

What this module does **not** reach: the ARXML beyond the export's read-back (``test_operation_event_arxml.py``,
``test_operation_event_exchange.py``); the importers' own reading; and every rated model's safety verdict (one
shape shows it is empty).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import yaml
from operation_event_support import (
    INTERFACE,
    INTERFACE_FILE,
    MIXED_EVENT_MODEL_ROOT,
    OPERATION,
    OPERATION_EVENT,
    PORT,
    RUNNABLE,
    SERVER,
    SERVER_FILE,
    TIMING_EVENT,
    change,
    load,
    mixed_event_model,
    put,
)
from swcraft_tools.arxml_import import map_data_types, map_interfaces, map_swcs
from swcraft_tools.arxml_reader import read_arxml
from swcraft_tools.component_exchange.export import export_component
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH
from swcraft_tools.reader import read_swc_model
from swcraft_tools.schema import InitEvent, OperationEvent, Runnable, SWComponent, TimingEvent
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import mutations
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.writer.mutations import _check_event_identifier
from swcraft_tools.writer.yaml_documents import runnable_to_dict, swc_to_dict

#: The runnable as the fixture declares it, with ``concurrent`` set.
CONCURRENT_RUNNABLE: dict[str, Any] = {
    "name": RUNNABLE,
    "concurrent": True,
    "events": [TIMING_EVENT, OPERATION_EVENT],
}


def _set_concurrent(model_root: Path) -> None:
    def mutate(document: dict[str, Any]) -> None:
        runnable = document["runnables"][0]
        document["runnables"][0] = {"name": runnable["name"], "concurrent": True, "events": runnable["events"]}

    change(model_root, SERVER_FILE, mutate)


def _server(model_root: Path) -> SWComponent:
    (server,) = (swc for swc in read_swc_model(str(model_root)) if swc.name == SERVER)
    return server


def _event(port: str = PORT, operation: str = OPERATION) -> dict[str, Any]:
    return {"type": "operation", "port": port, "operation": operation}


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


@pytest.fixture
def model(tmp_path: Path) -> Path:
    return mixed_event_model(tmp_path / "model")


# ---------------------------------------------------------------------------
# The mixed-event fixture loads as written
# ---------------------------------------------------------------------------


class TestTheFixtureLoads:
    def test_the_committed_tree_loads_both_events(self) -> None:
        (runnable,) = _server(MIXED_EVENT_MODEL_ROOT).runnables
        assert [type(event) for event in runnable.events] == [TimingEvent, OperationEvent]
        assert runnable.concurrent is False


# ---------------------------------------------------------------------------
# The hand-written serializer
# ---------------------------------------------------------------------------


class TestTheHandWrittenSerializer:
    def test_an_operation_event_is_written_as_itself(self, model: Path) -> None:
        written = swc_to_dict(_server(model))
        assert written["runnables"][0]["events"] == [TIMING_EVENT, OPERATION_EVENT]

    def test_each_kind_is_written_by_its_own_arm(self) -> None:
        runnable = Runnable(
            name="R",
            events=[TimingEvent(type="timing", period=0.02), InitEvent(type="init"), OperationEvent(**_event())],
        )
        assert runnable_to_dict(runnable)["events"] == [{"type": "timing", "period": 0.02}, {"type": "init"}, _event()]

    def test_concurrent_is_written_only_when_true_and_in_field_order(self) -> None:
        concurrent = runnable_to_dict(Runnable(name="R", min_start_interval=0.01, concurrent=True))
        assert list(concurrent) == ["name", "min_start_interval", "concurrent", "events"]
        assert concurrent["concurrent"] is True
        assert "concurrent" not in runnable_to_dict(Runnable(name="R"))

    def test_it_agrees_with_the_generic_serializer(self, model: Path) -> None:
        _set_concurrent(model)
        server = _server(model)
        hand_written, generic = swc_to_dict(server), element_to_dict(server)
        assert hand_written == generic
        assert list(hand_written["runnables"][0]) == list(generic["runnables"][0])

    def test_an_event_kind_it_has_not_been_taught_is_refused(self) -> None:
        """No ``else`` means ``init``: a fourth kind is an error, never an ``{"type": "init"}``."""

        class Untaught:
            type = "untaught"

        runnable = Runnable.model_construct(name="R", min_start_interval=0.0, concurrent=False, events=[Untaught()])
        with pytest.raises(TypeError, match="untaught|Untaught"):
            runnable_to_dict(runnable)

    def test_the_event_identifier_guard_takes_no_else_either(self) -> None:
        """``add_timing_event``/``add_init_event``'s guard names an operation event for its pair.

        A ``TypeError``: in ``writer/`` a ``ValueError`` is the model-refusal channel, and this is a
        programming error."""
        with pytest.raises(TypeError, match="operation"):
            _check_event_identifier("R", "S", "operation")


# ---------------------------------------------------------------------------
# The add_* mutations
# ---------------------------------------------------------------------------


#: A declared memory, so the two budget mutations have a pool to name.
RAM0: dict[str, Any] = {
    "name": "RAM0",
    "kind": "memory",
    "type": "RAM",
    "size": 4096,
    "accessible": ["CORE0"],
    "threshold": 0.8,
}


def _with_other_runnable(model_root: Path) -> None:
    change(model_root, SERVER_FILE, lambda document: document["runnables"].append({"name": "Other", "events": []}))


def _with_a_budget(model_root: Path) -> None:
    put(model_root, "hardware/RAM0.yaml", RAM0)
    change(model_root, SERVER_FILE, lambda document: document.update({"memory": {"RAM0": 64}}))


#: Every mutation that rewrites an existing component through `swc_to_dict` (`create_swc` writes a new
#: file with no runnable, so it has nothing to keep): ``(prepare, call)``.
REWRITING_MUTATIONS: dict[str, tuple[Any, Any]] = {
    "add_port": (lambda _root: None, lambda root: mutations.add_port(root, SERVER, "pSecond", "provide", INTERFACE)),
    "add_runnable": (lambda _root: None, lambda root: mutations.add_runnable(root, SERVER, "Other")),
    "add_timing_event": (_with_other_runnable, lambda root: mutations.add_timing_event(root, SERVER, "Other", 0.02)),
    "add_init_event": (_with_other_runnable, lambda root: mutations.add_init_event(root, SERVER, "Other")),
    "set_swc_memory_budget": (_with_a_budget, lambda root: mutations.set_swc_memory_budget(root, SERVER, "RAM0", 128)),
    "unset_swc_memory_budget": (_with_a_budget, lambda root: mutations.unset_swc_memory_budget(root, SERVER, "RAM0")),
}


class TestEveryRewritingMutationKeepsTheEventAndConcurrent:
    """A preservation test per mutation that rewrites a component."""

    @pytest.mark.parametrize("mutation", sorted(REWRITING_MUTATIONS))
    def test_it_keeps_both(self, model: Path, mutation: str) -> None:
        prepare, call = REWRITING_MUTATIONS[mutation]
        prepare(model)
        _set_concurrent(model)

        call(str(model))

        assert load(model, SERVER_FILE)["runnables"][0] == CONCURRENT_RUNNABLE


# ---------------------------------------------------------------------------
# The component export
# ---------------------------------------------------------------------------


class TestTheComponentExport:
    def test_the_exported_file_reads_back_as_the_runnable(self, model: Path, tmp_path: Path) -> None:
        """The exported file, read back through the importer's own mapping, holds the runnable the model declares."""
        _set_concurrent(model)
        output = tmp_path / "ServerSwc.arxml"
        export_component(str(model), SERVER, str(output))

        arxml = read_arxml(str(output))
        forms: list = []
        data_types, registry, _ = map_data_types(arxml, elements=forms)
        interfaces, iface_skips = map_interfaces(arxml, registry, data_types, elements=forms)
        (imported,), _ = map_swcs(arxml, interfaces, iface_skip_log=iface_skips, port_inits={}, variable_types={})

        assert imported.swc.runnables == [Runnable.model_validate(CONCURRENT_RUNNABLE)]

    def test_no_init_event_is_emitted_in_its_place(self, model: Path, tmp_path: Path) -> None:
        """The export emits the ``OPERATION-INVOKED-EVENT`` and ``CAN-BE-INVOKED-CONCURRENTLY``, no ``INIT-EVENT``."""
        _set_concurrent(model)
        output = tmp_path / "ServerSwc.arxml"

        export_component(str(model), SERVER, str(output))

        text = output.read_text(encoding="utf-8")
        assert "<INIT-EVENT>" not in text
        assert f"<SHORT-NAME>OIT_{PORT}_{OPERATION}</SHORT-NAME>" in text
        assert text.count("<OPERATION-INVOKED-EVENT>") == 1
        assert text.count("<TIMING-EVENT>") == 1
        assert f'<CONTEXT-P-PORT-REF DEST="P-PORT-PROTOTYPE">/ComponentTypes/{SERVER}/{PORT}</CONTEXT-P-PORT-REF>' in (
            text
        )
        assert "<CAN-BE-INVOKED-CONCURRENTLY>true</CAN-BE-INVOKED-CONCURRENTLY>" in text
        assert not Path(f"{output}.tmp").exists()


# ---------------------------------------------------------------------------
# The editing protocol
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The mixed-event model as a project: the ``examples/basic`` manifest beside it."""
    root = tmp_path / "operation-event-project"
    root.mkdir()
    manifest = yaml.safe_load(fixtures.BASE_MANIFEST.read_text(encoding="utf-8"))
    (root / "swcraft.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8", newline="\n")
    mixed_event_model(root / "model")
    return root


class _Review:
    """A host that approves the delta, DECLINES any safety question, and records what it was shown."""

    def __init__(self) -> None:
        self.proposals: list[Any] = []

    def __call__(self, proposal: Any) -> ApprovalDecision:
        self.proposals.append(proposal)
        return ApprovalDecision(
            change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=True, safety_approved=False
        )


def _change_set(
    project: Path,
    operations: list[dict[str, Any]],
    review: _Review | None = None,
    approved_scope: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
    return handle_request(
        {"operation": "change_set", "baseline": token, "intent": "operation event edit", "operations": operations},
        project=str(project),
        review=review or _Review(),
        approved_scope=approved_scope,
        scope_id=None if approved_scope is None else "turn-1",
    )


def _runnable_address(runnable: str = RUNNABLE, *tail: dict[str, Any]) -> dict[str, Any]:
    return {"layer": "swc", "element": SERVER, "path": [{"field": "runnables"}, {"member": runnable}, *tail]}


def _events_address(member: dict[str, Any] | None = None) -> dict[str, Any]:
    tail: list[dict[str, Any]] = [{"field": "events"}]
    if member is not None:
        tail.append({"member": member})
    return _runnable_address(RUNNABLE, *tail)


def _create_event(event: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "create", "address": _events_address(event), "value": event}


def _remove_event(event: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "remove", "address": _events_address(event)}


def _set_concurrent_to(value: bool) -> dict[str, Any]:
    return {"operation": "update", "address": _runnable_address(RUNNABLE, {"field": "concurrent"}), "value": value}


def _create_operation(name: str) -> dict[str, Any]:
    return {
        "operation": "create",
        "address": {"layer": "interfaces", "element": INTERFACE, "path": [{"field": "operations"}, {"member": name}]},
        "value": {
            "name": name,
            "arguments": [
                {"name": "id", "direction": "in", "implementation_type": "uint8"},
                {"name": "value", "direction": "out", "implementation_type": "uint8"},
            ],
        },
    }


def _events(project: Path) -> list[dict[str, Any]]:
    return load(project / "model", SERVER_FILE)["runnables"][0]["events"]


def _without_the_operation_event(project: Path) -> None:
    change(project / "model", SERVER_FILE, lambda document: document["runnables"][0]["events"].pop())


def _findings(project: Path) -> list[Any]:
    return list(check_cross_layer_consistency(str(project / "model")).records)


class TestTheProtocolCreates:
    def test_beside_an_existing_timing_event_with_no_refusal_and_no_finding(self, project: Path) -> None:
        _without_the_operation_event(project)
        assert _events(project) == [TIMING_EVENT]

        response = _change_set(project, [_create_event(OPERATION_EVENT)])

        assert response["status"] == "committed", response
        assert _events(project) == [TIMING_EVENT, OPERATION_EVENT]
        assert _findings(project) == []

    def test_an_over_budget_pair_is_refused_as_invalid_identifier(self, project: Path) -> None:
        """``OIT_`` + ``_`` leave 123 characters between port and operation; 124 is one too many."""
        port, operation = "P" * 62, "O" * 62
        assert len("OIT_") + len(port) + 1 + len(operation) == MAX_IDENTIFIER_LENGTH + 1
        event = _event(port, operation)
        before = _tree(project / "model")

        response = _change_set(
            project,
            [
                _create_operation(operation),
                {
                    "operation": "create",
                    "address": {"layer": "swc", "element": SERVER, "path": [{"field": "ports"}, {"member": port}]},
                    "value": {"name": port, "direction": "provide", "interface": INTERFACE},
                },
                _create_event(event),
            ],
        )

        assert response.get("error") == "invalid-identifier", response
        assert response["addresses"][0] == _events_address(event)
        assert f"OIT_{port}_{operation}" in response["message"]
        assert _tree(project / "model") == before


class TestTheProtocolReplacesAndReorders:
    def test_a_member_update_is_immutable(self, project: Path) -> None:
        before = _tree(project / "model")
        response = _change_set(
            project,
            [{"operation": "update", "address": _events_address(OPERATION_EVENT), "value": _event(operation="X")}],
        )
        assert response.get("error") == "immutable-field", response
        assert _tree(project / "model") == before

    def test_a_replacement_is_one_remove_and_one_create(self, project: Path) -> None:
        replacement = _event(operation="ReadOther")
        review = _Review()

        response = _change_set(
            project,
            [_create_operation("ReadOther"), _remove_event(OPERATION_EVENT), _create_event(replacement)],
            review,
        )

        assert response["status"] == "committed", response
        assert _events(project) == [TIMING_EVENT, replacement]
        event_entries = [
            (entry.operation, entry.before if entry.operation == "remove" else entry.after)
            for entry in review.proposals[0].delta
            if entry.address.layer == "swc"
        ]
        # A delta's halves are full values: the event's `name` at `None`.
        assert event_entries == [
            ("remove", {**OPERATION_EVENT, "name": None}),
            ("create", {**replacement, "name": None}),
        ]

    def test_a_reorder_is_a_collection_update(self, project: Path) -> None:
        response = _change_set(
            project, [{"operation": "update", "address": _events_address(), "value": [OPERATION_EVENT, TIMING_EVENT]}]
        )
        assert response["status"] == "committed", response
        assert _events(project) == [OPERATION_EVENT, TIMING_EVENT]


class TestTheProtocolRemoves:
    def test_the_event(self, project: Path) -> None:
        response = _change_set(project, [_remove_event(OPERATION_EVENT)])
        assert response["status"] == "committed", response
        assert _events(project) == [TIMING_EVENT]

    def test_the_ancestor_runnable(self, project: Path) -> None:
        response = _change_set(project, [{"operation": "remove", "address": _runnable_address()}])
        assert response["status"] == "committed", response
        assert load(project / "model", SERVER_FILE).get("runnables", []) == []


class TestTheProtocolSetsAndClearsConcurrent:
    def test_set_then_cleared(self, project: Path) -> None:
        response = _change_set(project, [_set_concurrent_to(True)])
        assert response["status"] == "committed", response
        runnable = load(project / "model", SERVER_FILE)["runnables"][0]
        assert runnable == CONCURRENT_RUNNABLE
        assert list(runnable) == ["name", "concurrent", "events"]

        response = _change_set(project, [_set_concurrent_to(False)])
        assert response["status"] == "committed", response
        assert load(project / "model", SERVER_FILE)["runnables"][0] == {
            "name": RUNNABLE,
            "events": [TIMING_EVENT, OPERATION_EVENT],
        }


class TestTheProtocolReads:
    def test_both_events(self, project: Path) -> None:
        response = handle_request({"operation": "read", "address": _events_address()}, project=str(project))
        # A read answers the full value; a timing event carries its `disabled_modes` even while the file
        # does not, and every event its `name`.
        assert response["value"] == [
            {**TIMING_EVENT, "disabled_modes": [], "name": None},
            {**OPERATION_EVENT, "name": None},
        ]


# ---------------------------------------------------------------------------
# The authorization boundary: an empty safety verdict, the ordinary review
# ---------------------------------------------------------------------------


def _rated(project: Path) -> None:
    """Rate the component and link a rated requirement, so the safety guard has levels to compare."""
    change(project / "model", SERVER_FILE, lambda document: document.update({"asil": "B"}))
    change(project / "model", "requirements/REQ-OPE-001.yaml", lambda document: document.update({"asil": "B"}))


#: The three acts, each as ``(prepare, operations)``: ``prepare`` shapes the baseline first.
SAFETY_CASES: dict[str, tuple[Any, list[dict[str, Any]]]] = {
    "create an operation event": (_without_the_operation_event, [_create_event(OPERATION_EVENT)]),
    "remove an operation event": (lambda _project: None, [_remove_event(OPERATION_EVENT)]),
    "set concurrent": (lambda _project: None, [_set_concurrent_to(True)]),
}


class TestNoSafetyQuestionAndTheOrdinaryReview:
    @pytest.mark.parametrize("case", sorted(SAFETY_CASES))
    def test_the_safety_verdict_is_empty_and_a_declined_answer_still_commits(self, project: Path, case: str) -> None:
        prepare, operations = SAFETY_CASES[case]
        _rated(project)
        prepare(project)
        assert _findings(project) == []
        review = _Review()

        response = _change_set(project, operations, review)

        assert response["status"] == "committed", response
        (proposal,) = review.proposals
        assert proposal.safety.changes == ()
        assert len(proposal.delta) == len(operations)

    @pytest.mark.parametrize("case", sorted(SAFETY_CASES))
    def test_an_empty_scope_refuses_it(self, project: Path, case: str) -> None:
        prepare, operations = SAFETY_CASES[case]
        prepare(project)
        before = _tree(project / "model")

        response = _change_set(project, operations, approved_scope=[])

        assert response.get("error") == "unauthorized-delta", response
        assert _tree(project / "model") == before

    @pytest.mark.parametrize("case", sorted(SAFETY_CASES))
    def test_the_scope_that_covers_it_commits(self, project: Path, case: str) -> None:
        prepare, operations = SAFETY_CASES[case]
        prepare(project)
        scope = [{"operation": operation["operation"], "address": operation["address"]} for operation in operations]

        response = _change_set(project, operations, approved_scope=scope)

        assert response["status"] == "committed", response

    def test_a_declined_delta_abandons(self, project: Path) -> None:
        before = _tree(project / "model")

        def decline(proposal: Any) -> ApprovalDecision:
            return ApprovalDecision(
                change_set_id=proposal.change_set_id, baseline=proposal.baseline, approved=False, safety_approved=True
            )

        token = handle_request({"operation": "discover"}, project=str(project))["baseline"]
        response = handle_request(
            {"operation": "change_set", "baseline": token, "intent": "x", "operations": [_set_concurrent_to(True)]},
            project=str(project),
            review=decline,
        )

        assert response.get("status") != "committed", response
        assert _tree(project / "model") == before


def test_the_fixture_interface_is_the_one_the_event_names() -> None:
    """The fixture's premise, so a test above cannot pass on a renamed interface."""
    document = load(MIXED_EVENT_MODEL_ROOT, INTERFACE_FILE)
    assert OPERATION in {operation["name"] for operation in document["operations"]}
