"""The data-received event through both write paths.

``writer/yaml_documents.py``'s hand-written ``swc_to_dict``/``runnable_to_dict`` (both importers, the
component export, every ``add_*`` mutation) and ``writer/elements.py``'s ``write_element`` (the editing
protocol). The hand-written serializer has one arm per kind and raises ``TypeError`` for any other;
it writes the event as ``type``, ``port``, ``element``, ``disabled_modes`` only when non-empty and a
stated ``name`` last, equal to ``element_to_dict``. The mutations and the export's closure keep the
events. The editing protocol creates, removes and replaces them (a replacement is one ``remove`` plus
one ``create``; a member or field ``update`` is refused as ``immutable-field``; an address without the
event's disabled modes names no event), with an **empty** safety verdict beside the ordinary scope
review.

**Not reached:** the ARXML, the importer, the component import's regimes and the differ; pass D's
refusals (``test_data_event_validator.py``); every rated model's safety verdict (the safety rules
read no event, and one rated shape shows it).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import yaml
from data_event_support import (
    COMPONENT,
    COMPONENT_FILE,
    FILTER,
    FRONT_LEFT,
    FRONT_RIGHT,
    MONITOR,
    REAR_LEFT,
    REAR_PORT,
    SLEEP_MODE,
    TRACK_REAR,
    WHEEL_INTERFACE,
    WHEEL_PORT,
    data_event,
    data_event_model,
)
from external_stubs_support import change, put
from swcraft_tools.component_exchange.closure import compute_closure
from swcraft_tools.component_exchange.export import _materialize_closure
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.reader import read_swc_model
from swcraft_tools.schema import DataEvent, InitEvent, OperationEvent, Runnable, SWComponent, TimingEvent
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import mutations
from swcraft_tools.writer.elements import element_to_dict
from swcraft_tools.writer.mutations import _check_event_identifier
from swcraft_tools.writer.yaml_documents import runnable_to_dict, swc_to_dict

#: The fixture's three data-received events, as its file states them.
FILTER_EVENT = data_event(WHEEL_PORT, FRONT_LEFT)
MONITOR_EVENT = data_event(WHEEL_PORT, FRONT_RIGHT, disabled_modes=[SLEEP_MODE])
REAR_EVENT = data_event(REAR_PORT, REAR_LEFT)
#: The monitor's timing event, beside its data-received event.
MONITOR_TIMING: dict[str, Any] = {"type": "timing", "period": 0.1}


def _load(model_root: Path, relative: str = COMPONENT_FILE) -> dict[str, Any]:
    return yaml.safe_load((model_root / relative).read_text(encoding="utf-8"))


def _component(model_root: Path) -> SWComponent:
    (swc,) = (swc for swc in read_swc_model(str(model_root)) if swc.name == COMPONENT)
    return swc


def _events_by_runnable(document: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {runnable["name"]: runnable["events"] for runnable in document["runnables"]}


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


@pytest.fixture
def model(tmp_path: Path) -> Path:
    return data_event_model(tmp_path / "model")


# The hand-written serializer


class TestTheHandWrittenSerializer:
    def test_the_fixture_s_events_are_written_as_themselves(self, model: Path) -> None:
        written = _events_by_runnable(swc_to_dict(_component(model)))

        assert written == {FILTER: [FILTER_EVENT], MONITOR: [MONITOR_TIMING, MONITOR_EVENT], TRACK_REAR: [REAR_EVENT]}

    def test_each_kind_is_written_by_its_own_arm(self) -> None:
        runnable = Runnable(
            name="R",
            events=[
                TimingEvent(type="timing", period=0.02),
                InitEvent(type="init"),
                OperationEvent(type="operation", port="pServe", operation="Op"),
                DataEvent(type="data", port="rIn", element="value"),
            ],
        )

        assert runnable_to_dict(runnable)["events"] == [
            {"type": "timing", "period": 0.02},
            {"type": "init"},
            {"type": "operation", "port": "pServe", "operation": "Op"},
            {"type": "data", "port": "rIn", "element": "value"},
        ]

    def test_a_data_event_writes_its_disabled_modes_and_its_stated_name_in_field_order(self) -> None:
        event = DataEvent(type="data", port="rIn", element="value", disabled_modes=[SLEEP_MODE], name="DRT_Stated")

        (written,) = runnable_to_dict(Runnable(name="R", events=[event]))["events"]

        assert written == {**data_event("rIn", "value", disabled_modes=[SLEEP_MODE]), "name": "DRT_Stated"}
        assert list(written) == ["type", "port", "element", "disabled_modes", "name"]
        assert list(written["disabled_modes"][0]) == ["port", "mode_group_ref", "mode_ref"]

    def test_no_disabled_mode_and_no_stated_name_write_no_key(self) -> None:
        (written,) = runnable_to_dict(Runnable(name="R", events=[DataEvent(**data_event("rIn", "value"))]))["events"]

        assert list(written) == ["type", "port", "element"]

    def test_it_agrees_with_the_generic_serializer_over_the_fixture(self, model: Path) -> None:
        swc = _component(model)
        hand_written, generic = swc_to_dict(swc), element_to_dict(swc)

        assert hand_written == generic
        for ours, theirs in zip(hand_written["runnables"], generic["runnables"], strict=True):
            for our_event, their_event in zip(ours["events"], theirs["events"], strict=True):
                assert list(our_event) == list(their_event)

    def test_an_event_kind_it_has_not_been_taught_is_still_refused(self) -> None:
        """The data arm is explicit: no ``else`` stands for another kind."""

        class Untaught:
            type = "untaught"

        runnable = Runnable.model_construct(name="R", min_start_interval=0.0, concurrent=False, events=[Untaught()])
        with pytest.raises(TypeError, match="Untaught"):
            runnable_to_dict(runnable)

    def test_the_event_identifier_guard_refuses_a_data_event_by_its_own_arm(self) -> None:
        """``add_timing_event``/``add_init_event``'s guard: a data-received event is named for its pair."""
        with pytest.raises(TypeError, match=r"'data'.*port and element"):
            _check_event_identifier("R", "S", "data")

    def test_the_event_identifier_guard_refuses_an_unknown_kind(self) -> None:
        with pytest.raises(TypeError, match="'untaught'"):
            _check_event_identifier("R", "S", "untaught")


# The add_* mutations


#: A declared memory, so the two budget mutations have a pool to name.
RAM0: dict[str, Any] = {
    "name": "RAM0",
    "kind": "memory",
    "type": "RAM",
    "size": 4096,
    "accessible": ["CORE0"],
    "threshold": 0.8,
}

#: A requirement the trace-link mutation can link the component to.
REQUIREMENT: dict[str, Any] = {
    "id": "REQ_SPEED_001",
    "title": "Estimate the vehicle speed",
    "description": "The component shall estimate the vehicle speed.",
    "req_type": "req",
    "version": 1,
    "source": "architecture",
}


def _with_other_runnable(model_root: Path) -> None:
    change(model_root, COMPONENT_FILE, lambda document: document["runnables"].append({"name": "Other", "events": []}))


def _with_a_budget(model_root: Path) -> None:
    put(model_root, "hardware/RAM0.yaml", RAM0)
    change(model_root, COMPONENT_FILE, lambda document: document.update({"memory": {"RAM0": 64}}))


def _with_a_requirement(model_root: Path) -> None:
    put(model_root, "requirements/REQ_SPEED_001.yaml", REQUIREMENT)


#: Every mutation that rewrites an existing component through `swc_to_dict` (`create_swc` writes a new
#: file with no runnable, so it has nothing to keep): ``(prepare, call)``.
REWRITING_MUTATIONS: dict[str, tuple[Any, Any]] = {
    "add_port": (
        lambda _root: None,
        lambda root: mutations.add_port(root, COMPONENT, "R_Second", "require", WHEEL_INTERFACE),
    ),
    "add_runnable": (lambda _root: None, lambda root: mutations.add_runnable(root, COMPONENT, "Other")),
    "add_timing_event": (_with_other_runnable, lambda root: mutations.add_timing_event(root, COMPONENT, "Other", 0.02)),
    "add_init_event": (_with_other_runnable, lambda root: mutations.add_init_event(root, COMPONENT, "Other")),
    "set_swc_memory_budget": (
        _with_a_budget,
        lambda root: mutations.set_swc_memory_budget(root, COMPONENT, "RAM0", 128),
    ),
    "unset_swc_memory_budget": (
        _with_a_budget,
        lambda root: mutations.unset_swc_memory_budget(root, COMPONENT, "RAM0"),
    ),
    "add_trace_link": (_with_a_requirement, lambda root: mutations.add_trace_link(root, "REQ_SPEED_001", COMPONENT)),
}


class TestEveryRewritingMutationKeepsTheEvents:
    """A preservation test per calling mutation: a hand-written dump never erases a field."""

    @pytest.mark.parametrize("mutation", sorted(REWRITING_MUTATIONS))
    def test_it_keeps_every_data_received_event(self, model: Path, mutation: str) -> None:
        prepare, call = REWRITING_MUTATIONS[mutation]
        prepare(model)
        before = _events_by_runnable(_load(model))
        assert before[FILTER] == [FILTER_EVENT] and before[MONITOR] == [MONITOR_TIMING, MONITOR_EVENT]

        call(str(model))

        # The fixture's three runnables; the mutation may add one of its own (``Other``) or an event to it.
        after = _events_by_runnable(_load(model))
        for runnable in (FILTER, MONITOR, TRACK_REAR):
            assert after[runnable] == before[runnable]

    def test_the_mutation_set_is_every_rewriting_caller(self) -> None:
        """The table above against the module: every function calling ``swc_to_dict`` but ``create_swc``."""
        tree = ast.parse(inspect.getsource(mutations))
        callers = {
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and any(
                isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "swc_to_dict"
                for call in ast.walk(node)
            )
        }

        assert callers - {"create_swc"} == set(REWRITING_MUTATIONS)


# The component export's closure


class TestTheComponentExportClosure:
    def test_the_materialized_component_keeps_every_data_received_event(self, model: Path, tmp_path: Path) -> None:
        """The mini model ``_materialize_closure`` writes through ``swc_to_dict`` keeps every event."""
        root = tmp_path / "closure"
        _materialize_closure(compute_closure(str(model), COMPONENT), root)

        (component_file,) = (root / "swc").rglob(f"{COMPONENT}.yaml")
        closure = yaml.safe_load(component_file.read_text(encoding="utf-8"))
        assert _events_by_runnable(closure) == _events_by_runnable(_load(model))


# The editing protocol


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The data-received event fixture as a project: the ``examples/basic`` manifest beside it."""
    root = tmp_path / "data-event-project"
    root.mkdir()
    manifest = yaml.safe_load(fixtures.BASE_MANIFEST.read_text(encoding="utf-8"))
    (root / "swcraft.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8", newline="\n")
    data_event_model(root / "model")
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
        {"operation": "change_set", "baseline": token, "intent": "data event edit", "operations": operations},
        project=str(project),
        review=review or _Review(),
        approved_scope=approved_scope,
        scope_id=None if approved_scope is None else "turn-1",
    )


def _events_address(runnable: str, member: dict[str, Any] | None = None) -> dict[str, Any]:
    path: list[dict[str, Any]] = [{"field": "runnables"}, {"member": runnable}, {"field": "events"}]
    if member is not None:
        path.append({"member": member})
    return {"layer": "swc", "element": COMPONENT, "path": path}


def _create_event(runnable: str, event: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "create", "address": _events_address(runnable, event), "value": event}


def _remove_event(runnable: str, event: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "remove", "address": _events_address(runnable, event)}


def _events(project: Path, runnable: str) -> list[dict[str, Any]]:
    return _events_by_runnable(_load(project / "model"))[runnable]


def _findings(project: Path) -> list[Any]:
    result = check_cross_layer_consistency(str(project / "model"))
    return [*result.records, *result.warnings]


def _without(runnable: str, event: dict[str, Any]) -> Any:
    """A ``prepare``: the fixture with *event* taken off *runnable*, so a create has a place to land."""

    def prepare(project: Path) -> None:
        def mutate(document: dict[str, Any]) -> None:
            (entry,) = (entry for entry in document["runnables"] if entry["name"] == runnable)
            entry["events"].remove(event)

        change(project / "model", COMPONENT_FILE, mutate)

    return prepare


#: The filter's new event: the second front wheel, no disabled mode.
FRONT_RIGHT_EVENT = data_event(WHEEL_PORT, FRONT_RIGHT)
#: The filter's new event with a disabled mode.
FRONT_RIGHT_ASLEEP = data_event(WHEEL_PORT, FRONT_RIGHT, disabled_modes=[SLEEP_MODE])


class TestTheProtocolCreates:
    @pytest.mark.parametrize("event", [FRONT_RIGHT_EVENT, FRONT_RIGHT_ASLEEP], ids=["no-modes", "disabled-modes"])
    def test_a_create_with_no_refusal_and_no_finding(self, project: Path, event: dict[str, Any]) -> None:
        response = _change_set(project, [_create_event(FILTER, event)])

        assert response["status"] == "committed", response
        assert _events(project, FILTER) == [FILTER_EVENT, event]
        assert _findings(project) == []

    def test_beside_an_existing_timing_event_with_no_refusal_and_no_finding(self, project: Path) -> None:
        _without(MONITOR, MONITOR_EVENT)(project)
        assert _events(project, MONITOR) == [MONITOR_TIMING]

        response = _change_set(project, [_create_event(MONITOR, MONITOR_EVENT)])

        assert response["status"] == "committed", response
        assert _events(project, MONITOR) == [MONITOR_TIMING, MONITOR_EVENT]
        assert _findings(project) == []


class TestTheProtocolRemoves:
    def test_an_event_without_disabled_modes(self, project: Path) -> None:
        response = _change_set(project, [_remove_event(TRACK_REAR, REAR_EVENT)])

        assert response["status"] == "committed", response
        assert _events(project, TRACK_REAR) == []

    def test_an_event_with_disabled_modes_by_its_whole_value(self, project: Path) -> None:
        response = _change_set(project, [_remove_event(MONITOR, MONITOR_EVENT)])

        assert response["status"] == "committed", response
        assert _events(project, MONITOR) == [MONITOR_TIMING]

    def test_an_address_without_the_disabled_modes_names_no_event(self, project: Path) -> None:
        """The disabled modes are part of the identity: the bare pair is another value, which is not there."""
        before = _tree(project / "model")

        response = _change_set(project, [_remove_event(MONITOR, data_event(WHEEL_PORT, FRONT_RIGHT))])

        assert "error" in response, response
        assert _tree(project / "model") == before


class TestTheProtocolReplaces:
    def test_a_member_update_is_immutable(self, project: Path) -> None:
        before = _tree(project / "model")

        response = _change_set(
            project,
            [{"operation": "update", "address": _events_address(FILTER, FILTER_EVENT), "value": FRONT_RIGHT_EVENT}],
        )

        assert response.get("error") == "immutable-field", response
        assert _tree(project / "model") == before

    def test_a_field_update_is_immutable(self, project: Path) -> None:
        before = _tree(project / "model")
        address = _events_address(FILTER, FILTER_EVENT)
        address["path"].append({"field": "element"})

        response = _change_set(project, [{"operation": "update", "address": address, "value": FRONT_RIGHT}])

        assert response.get("error") == "immutable-field", response
        assert _tree(project / "model") == before

    def test_a_replacement_is_one_remove_and_one_create(self, project: Path) -> None:
        review = _Review()

        response = _change_set(
            project, [_remove_event(FILTER, FILTER_EVENT), _create_event(FILTER, FRONT_RIGHT_EVENT)], review
        )

        assert response["status"] == "committed", response
        assert _events(project, FILTER) == [FRONT_RIGHT_EVENT]
        event_entries = [
            (entry.operation, entry.before if entry.operation == "remove" else entry.after)
            for entry in review.proposals[0].delta
            if entry.address.layer == "swc"
        ]
        # A delta's halves are full values: `disabled_modes` at `[]` and `name` at `None`.
        assert event_entries == [
            ("remove", {**FILTER_EVENT, "disabled_modes": [], "name": None}),
            ("create", {**FRONT_RIGHT_EVENT, "disabled_modes": [], "name": None}),
        ]


class TestTheProtocolReads:
    def test_the_full_values(self, project: Path) -> None:
        response = handle_request({"operation": "read", "address": _events_address(MONITOR)}, project=str(project))

        assert response["value"] == [
            {**MONITOR_TIMING, "disabled_modes": [], "name": None},
            {**MONITOR_EVENT, "name": None},
        ]


# The authorization boundary: an empty safety verdict, the ordinary review


def _rated(project: Path) -> None:
    """Rate the component and link a rated requirement, so the safety guard has levels to compare."""
    put(project / "model", "requirements/REQ_SPEED_001.yaml", {**REQUIREMENT, "asil": "B"})
    change(
        project / "model",
        COMPONENT_FILE,
        lambda document: document.update({"asil": "B", "satisfies": ["REQ_SPEED_001@1"]}),
    )


def _nothing(_project: Path) -> None:
    return None


#: The acts, each as ``(prepare, operations)``: ``prepare`` shapes the baseline first.
SAFETY_CASES: dict[str, tuple[Any, list[dict[str, Any]]]] = {
    "create an event": (_nothing, [_create_event(FILTER, FRONT_RIGHT_EVENT)]),
    "create an event with disabled modes": (_without(MONITOR, MONITOR_EVENT), [_create_event(MONITOR, MONITOR_EVENT)]),
    "remove an event": (_nothing, [_remove_event(TRACK_REAR, REAR_EVENT)]),
    "remove an event with disabled modes": (_nothing, [_remove_event(MONITOR, MONITOR_EVENT)]),
    "replace an event": (
        _nothing,
        [_remove_event(FILTER, FILTER_EVENT), _create_event(FILTER, FRONT_RIGHT_EVENT)],
    ),
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
