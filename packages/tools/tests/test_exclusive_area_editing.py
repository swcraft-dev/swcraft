"""The exclusive areas through both write paths.

``writer/yaml_documents.py``'s hand-written ``swc_to_dict`` / ``runnable_to_dict`` are
what both importers, the component export and every ``add_*`` mutation write through;
``writer/elements.py``'s ``write_element`` is what the editing protocol writes through.
This module holds:

* **the hand-written serializer**: the three lists in field order, each only when
  non-empty, equal to the generic ``element_to_dict`` over the shared fixture (the
  populated twin is ``test_inter_runnable_variable_editing.py``, the byte-level one
  ``test_writer_elements.py::TestTheTwoWritersAgreeOnTheAccessLists``);
* **the ``add_*`` mutations**: every mutation rewriting a component keeps the areas and
  both lists;
* **the component export's closure**: the mini model it materializes carries them;
* **the write-path name guard**: ``write_element`` refuses an illegal area name before
  anything is written, answered ``invalid-identifier``;
* **the editing protocol**: each area and entry verb, a rename or retarget taken as a
  remove and a create, and removing an area an entry still names refused at pass D;
* **the authorization boundary**: each act yields an empty safety verdict, while the
  ordinary scope review still applies.

What this module does **not** reach:

* the ARXML, the importer, the component import's regimes and the differ;
* every rated model's safety verdict: the safety rules read no area, and one rated
  shape shows it, not derived here;
* the protocol's ``check-model`` findings beyond the one dangling entry
  (``test_exclusive_area_validator.py``'s).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import yaml
from exclusive_area_support import (
    CALIBRATE,
    COMPONENT,
    COMPONENT_FILE,
    FILTER,
    MONITOR,
    OFFSET_TABLE,
    SPARE_AREA,
    SPEED_STATE,
    area_model,
)
from external_stubs_support import change, put
from swcraft_tools.component_exchange.closure import compute_closure
from swcraft_tools.component_exchange.export import _materialize_closure
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.reader import read_swc_model
from swcraft_tools.schema import Runnable, SWComponent
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import mutations
from swcraft_tools.writer.elements import element_to_dict, write_element
from swcraft_tools.writer.mutations import ReservedNameError
from swcraft_tools.writer.yaml_documents import runnable_to_dict, swc_to_dict


def _load(model_root: Path, relative: str = COMPONENT_FILE) -> dict[str, Any]:
    return yaml.safe_load((model_root / relative).read_text(encoding="utf-8"))


def _component(model_root: Path) -> SWComponent:
    (swc,) = (swc for swc in read_swc_model(str(model_root)) if swc.name == COMPONENT)
    return swc


def _area_lists(document: dict[str, Any]) -> dict[str, Any]:
    """The three lists of a component document: its areas, and each runnable's two area lists."""
    return {
        "exclusive_areas": document.get("exclusive_areas", []),
        "entries": {
            runnable["name"]: (runnable.get("can_enter", []), runnable.get("runs_inside", []))
            for runnable in document["runnables"]
        },
    }


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def _runnable_of(document: dict[str, Any], name: str) -> dict[str, Any]:
    (runnable,) = (runnable for runnable in document["runnables"] if runnable["name"] == name)
    return runnable


@pytest.fixture
def model(tmp_path: Path) -> Path:
    return area_model(tmp_path / "model")


# The hand-written serializer


class TestTheHandWrittenSerializer:
    def test_it_writes_the_fixture_s_three_lists_in_field_order(self, model: Path) -> None:
        written = swc_to_dict(_component(model))

        assert _area_lists(written) == _area_lists(_load(model))
        assert list(written).index("exclusive_areas") == list(written).index("runnables") + 1
        assert list(_runnable_of(written, CALIBRATE)) == ["name", "events", "can_enter"]
        assert list(_runnable_of(written, FILTER)) == ["name", "events", "runs_inside"]

    def test_an_area_writes_its_description_only_when_stated(self, model: Path) -> None:
        areas = swc_to_dict(_component(model))["exclusive_areas"]

        assert [list(area) for area in areas] == [["name", "description"], ["name"], ["name"]]
        assert [area["name"] for area in areas] == [SPEED_STATE, OFFSET_TABLE, SPARE_AREA], "declared order"

    def test_both_lists_on_one_runnable_are_written_can_enter_first(self) -> None:
        runnable = Runnable(
            name="Run", runs_inside=[{"area": "B"}, {"area": "A"}], can_enter=[{"area": "A"}], description="D"
        )

        written = runnable_to_dict(runnable)

        assert list(written) == ["name", "events", "can_enter", "runs_inside", "description"]
        assert written["runs_inside"] == [{"area": "B"}, {"area": "A"}], "declared order, never sorted"

    def test_a_component_without_them_writes_no_key(self) -> None:
        written = swc_to_dict(SWComponent(name="Plain", runnables=[Runnable(name="Run")]))

        assert "exclusive_areas" not in written
        assert list(written["runnables"][0]) == ["name", "events"]

    def test_it_agrees_with_the_generic_serializer_over_the_fixture(self, model: Path) -> None:
        swc = _component(model)
        hand_written, generic = swc_to_dict(swc), element_to_dict(swc)

        assert hand_written == generic
        assert list(hand_written) == list(generic)
        for ours, theirs in zip(hand_written["runnables"], generic["runnables"], strict=True):
            assert list(ours) == list(theirs)


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


#: Every mutation that rewrites an existing component through `swc_to_dict`, as
#: ``(prepare, call)``; `create_swc` writes a new file with nothing to keep.
REWRITING_MUTATIONS: dict[str, tuple[Any, Any]] = {
    "add_port": (lambda _root: None, lambda root: mutations.add_port(root, COMPONENT, "pSpeed", "require", "ISpeed")),
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


class TestEveryRewritingMutationKeepsTheAreas:
    """A preservation test per calling mutation: a hand-written dump never erases a field."""

    @pytest.mark.parametrize("mutation", sorted(REWRITING_MUTATIONS))
    def test_it_keeps_the_areas_and_every_entry(self, model: Path, mutation: str) -> None:
        prepare, call = REWRITING_MUTATIONS[mutation]
        prepare(model)
        before = _area_lists(_load(model))
        assert before["exclusive_areas"] and before["entries"][CALIBRATE][0] and before["entries"][FILTER][1]

        call(str(model))

        after = _area_lists(_load(model))
        assert after["exclusive_areas"] == before["exclusive_areas"]
        for runnable, entries in before["entries"].items():
            assert after["entries"][runnable] == entries

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
    def test_the_materialized_component_keeps_the_areas_and_every_entry(self, model: Path, tmp_path: Path) -> None:
        """The mini model ``_materialize_closure`` writes through ``swc_to_dict`` keeps the areas and every entry.

        Only the component file is asserted; its ARXML and round trip are other suites'.
        """
        root = tmp_path / "closure"
        _materialize_closure(compute_closure(str(model), COMPONENT), root)

        (component_file,) = (root / "swc").rglob(f"{COMPONENT}.yaml")
        assert _area_lists(yaml.safe_load(component_file.read_text(encoding="utf-8"))) == _area_lists(_load(model))


# The write-path name guard


def _with(model_root: Path, mutate: Any) -> SWComponent:
    document = _load(model_root)
    mutate(document)
    return SWComponent.model_validate(document)


class TestTheWritePathGuard:
    @pytest.mark.parametrize("name", ["speed state", "1Speed", "S" * 129])
    def test_an_illegal_area_name_is_refused_before_anything_is_written(self, model: Path, name: str) -> None:
        swc = _with(model, lambda document: document["exclusive_areas"].append({"name": name}))
        before = _tree(model)

        with pytest.raises(ValueError, match="exclusive area name") as raised:
            write_element(str(model), COMPONENT_FILE, swc)

        assert not isinstance(raised.value, ReservedNameError)
        assert f"SWC '{COMPONENT}'" in str(raised.value)
        assert _tree(model) == before

    def test_an_entry_naming_an_illegal_name_is_no_identifier_of_its_own(self, model: Path) -> None:
        """An entry is a reference and emits no ``SHORT-NAME``: the guard is silent; ``unknown-area`` is pass D's."""
        swc = _with(model, lambda document: _runnable_of(document, FILTER)["runs_inside"].append({"area": "no such"}))

        write_element(str(model), COMPONENT_FILE, swc)

        assert {"area": "no such"} in _runnable_of(_load(model), FILTER)["runs_inside"]

    def test_a_legal_area_is_written(self, model: Path) -> None:
        swc = _with(model, lambda document: document["exclusive_areas"].append({"name": "S" * 128}))

        write_element(str(model), COMPONENT_FILE, swc)

        assert _load(model)["exclusive_areas"][-1] == {"name": "S" * 128}


# The editing protocol


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The area fixture as a project: the ``examples/basic`` manifest beside it."""
    root = tmp_path / "area-project"
    root.mkdir()
    manifest = yaml.safe_load(fixtures.BASE_MANIFEST.read_text(encoding="utf-8"))
    (root / "swcraft.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8", newline="\n")
    area_model(root / "model")
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
        {"operation": "change_set", "baseline": token, "intent": "exclusive area edit", "operations": operations},
        project=str(project),
        review=review or _Review(),
        approved_scope=approved_scope,
        scope_id=None if approved_scope is None else "turn-1",
    )


def _area_address(name: str, *tail: dict[str, Any]) -> dict[str, Any]:
    return {"layer": "swc", "element": COMPONENT, "path": [{"field": "exclusive_areas"}, {"member": name}, *tail]}


def _entry_address(runnable: str, list_name: str, area: str, *tail: dict[str, Any]) -> dict[str, Any]:
    return {
        "layer": "swc",
        "element": COMPONENT,
        "path": [{"field": "runnables"}, {"member": runnable}, {"field": list_name}, {"member": area}, *tail],
    }


def _create(address: dict[str, Any], value: Any) -> dict[str, Any]:
    return {"operation": "create", "address": address, "value": value}


def _update(address: dict[str, Any], value: Any) -> dict[str, Any]:
    return {"operation": "update", "address": address, "value": value}


def _remove(address: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "remove", "address": address}


def _areas(project: Path) -> list[dict[str, Any]]:
    return _load(project / "model").get("exclusive_areas", [])


def _entries(project: Path, runnable: str, list_name: str) -> list[dict[str, Any]]:
    return _runnable_of(_load(project / "model"), runnable).get(list_name, [])


def _findings(project: Path) -> list[Any]:
    result = check_cross_layer_consistency(str(project / "model"))
    return [*result.records, *result.warnings]


NEW_AREA: dict[str, Any] = {"name": "ResetGuard", "description": "Guards the reset request."}


class TestTheProtocolOnAnArea:
    def test_a_create_with_no_refusal_and_no_finding(self, project: Path) -> None:
        response = _change_set(project, [_create(_area_address(NEW_AREA["name"]), NEW_AREA)])

        assert response["status"] == "committed", response
        assert _areas(project)[-1] == NEW_AREA
        assert _findings(project) == []

    def test_a_description_is_an_update_and_clearing_it_is_one_too(self, project: Path) -> None:
        address = _area_address(SPARE_AREA, {"field": "description"})

        response = _change_set(project, [_update(address, "Spare.")])
        assert response["status"] == "committed", response
        assert _areas(project)[2] == {"name": SPARE_AREA, "description": "Spare."}

        response = _change_set(project, [_update(address, "")])
        assert response["status"] == "committed", response
        assert _areas(project)[2] == {"name": SPARE_AREA}

    def test_a_rename_by_update_is_refused(self, project: Path) -> None:
        before = _tree(project / "model")

        response = _change_set(project, [_update(_area_address(SPARE_AREA, {"field": "name"}), "Renamed")])

        assert response.get("error") == "immutable-field", response
        assert _tree(project / "model") == before

    def test_a_rename_is_a_remove_and_a_create(self, project: Path) -> None:
        response = _change_set(
            project, [_remove(_area_address(SPARE_AREA)), _create(_area_address("SpareGuard"), {"name": "SpareGuard"})]
        )

        assert response["status"] == "committed", response
        assert [area["name"] for area in _areas(project)] == [SPEED_STATE, OFFSET_TABLE, "SpareGuard"]

    def test_a_remove(self, project: Path) -> None:
        response = _change_set(project, [_remove(_area_address(SPARE_AREA))])

        assert response["status"] == "committed", response
        assert [area["name"] for area in _areas(project)] == [SPEED_STATE, OFFSET_TABLE]

    @pytest.mark.parametrize("area", [SPEED_STATE, OFFSET_TABLE])
    def test_removing_an_area_an_entry_still_names_is_refused_at_pass_d(self, project: Path, area: str) -> None:
        """The dangling entry: every entry naming the removed area is the broken reference."""
        before = _tree(project / "model")

        response = _change_set(project, [_remove(_area_address(area))])

        assert response.get("error") == "broken-reference", response
        assert f"declares no exclusive area '{area}'" in response["message"], response
        assert _tree(project / "model") == before

    def test_removing_an_area_with_every_entry_naming_it_commits(self, project: Path) -> None:
        response = _change_set(
            project,
            [_remove(_entry_address(CALIBRATE, "can_enter", OFFSET_TABLE)), _remove(_area_address(OFFSET_TABLE))],
        )

        assert response["status"] == "committed", response
        assert [area["name"] for area in _areas(project)] == [SPEED_STATE, SPARE_AREA]
        assert _findings(project) == []

    def test_an_illegal_area_name_is_refused_with_its_code(self, project: Path) -> None:
        before = _tree(project / "model")

        response = _change_set(project, [_create(_area_address("speed state"), {"name": "speed state"})])

        assert response.get("error") == "invalid-identifier", response
        assert _tree(project / "model") == before

    def test_a_read_answers_the_full_values(self, project: Path) -> None:
        response = handle_request({"operation": "read", "address": _area_address(OFFSET_TABLE)}, project=str(project))

        assert response["value"] == {"name": OFFSET_TABLE, "description": ""}


class TestTheProtocolOnAnEntry:
    @pytest.mark.parametrize(
        ("runnable", "list_name", "area", "expected"),
        [
            (FILTER, "can_enter", OFFSET_TABLE, [{"area": OFFSET_TABLE}]),
            (CALIBRATE, "runs_inside", SPEED_STATE, [{"area": SPEED_STATE}]),
            (MONITOR, "runs_inside", SPARE_AREA, [{"area": SPEED_STATE}, {"area": SPARE_AREA}]),
        ],
    )
    def test_a_create(self, project: Path, runnable: str, list_name: str, area: str, expected: list[Any]) -> None:
        response = _change_set(project, [_create(_entry_address(runnable, list_name, area), {"area": area})])

        assert response["status"] == "committed", response
        assert _entries(project, runnable, list_name) == expected
        assert _findings(project) == []

    @pytest.mark.parametrize(
        ("runnable", "list_name", "area"),
        [(CALIBRATE, "can_enter", OFFSET_TABLE), (MONITOR, "runs_inside", SPEED_STATE)],
    )
    def test_an_update_of_its_area_is_refused_as_an_identity_change(
        self, project: Path, runnable: str, list_name: str, area: str
    ) -> None:
        before = _tree(project / "model")

        response = _change_set(
            project, [_update(_entry_address(runnable, list_name, area, {"field": "area"}), SPARE_AREA)]
        )

        assert response.get("error") == "immutable-field", response
        assert _tree(project / "model") == before

    def test_a_retarget_is_a_remove_and_a_create(self, project: Path) -> None:
        response = _change_set(
            project,
            [
                _remove(_entry_address(MONITOR, "runs_inside", SPEED_STATE)),
                _create(_entry_address(MONITOR, "runs_inside", SPARE_AREA), {"area": SPARE_AREA}),
            ],
        )

        assert response["status"] == "committed", response
        assert _entries(project, MONITOR, "runs_inside") == [{"area": SPARE_AREA}]

    @pytest.mark.parametrize(
        ("runnable", "list_name", "area"),
        [(CALIBRATE, "can_enter", OFFSET_TABLE), (MONITOR, "runs_inside", SPEED_STATE)],
    )
    def test_a_remove(self, project: Path, runnable: str, list_name: str, area: str) -> None:
        response = _change_set(project, [_remove(_entry_address(runnable, list_name, area))])

        assert response["status"] == "committed", response
        assert list_name not in _runnable_of(_load(project / "model"), runnable), "an emptied list is omitted"

    def test_an_entry_naming_an_area_created_in_the_same_change_set(self, project: Path) -> None:
        response = _change_set(
            project,
            [
                _create(_area_address(NEW_AREA["name"]), NEW_AREA),
                _create(_entry_address(FILTER, "can_enter", NEW_AREA["name"]), {"area": NEW_AREA["name"]}),
                _create(_entry_address(CALIBRATE, "runs_inside", NEW_AREA["name"]), {"area": NEW_AREA["name"]}),
            ],
        )

        assert response["status"] == "committed", response
        assert _entries(project, FILTER, "can_enter") == [{"area": NEW_AREA["name"]}]
        assert _findings(project) == []


# The authorization boundary: an empty safety verdict, the ordinary review


def _rated(project: Path) -> None:
    """Rate the component and link a rated requirement, so the safety guard has levels to compare."""
    put(project / "model", "requirements/REQ_SPEED_001.yaml", {**REQUIREMENT, "asil": "B"})
    change(
        project / "model",
        COMPONENT_FILE,
        lambda document: document.update({"asil": "B", "satisfies": ["REQ_SPEED_001@1"]}),
    )


#: The acts, each as a list of operations against the fixture.
SAFETY_CASES: dict[str, list[dict[str, Any]]] = {
    "create an area": [_create(_area_address(NEW_AREA["name"]), NEW_AREA)],
    "change an area's description": [_update(_area_address(SPARE_AREA, {"field": "description"}), "Spare.")],
    "remove an area": [_remove(_area_address(SPARE_AREA))],
    "create a can_enter entry": [_create(_entry_address(FILTER, "can_enter", OFFSET_TABLE), {"area": OFFSET_TABLE})],
    "remove a runs_inside entry": [_remove(_entry_address(MONITOR, "runs_inside", SPEED_STATE))],
}


class TestNoSafetyQuestionAndTheOrdinaryReview:
    @pytest.mark.parametrize("case", sorted(SAFETY_CASES))
    def test_the_safety_verdict_is_empty_and_a_declined_answer_still_commits(self, project: Path, case: str) -> None:
        operations = SAFETY_CASES[case]
        _rated(project)
        assert _findings(project) == []
        review = _Review()

        response = _change_set(project, operations, review)

        assert response["status"] == "committed", response
        (proposal,) = review.proposals
        assert proposal.safety.changes == ()
        assert len(proposal.delta) == len(operations)

    @pytest.mark.parametrize("case", sorted(SAFETY_CASES))
    def test_an_empty_scope_refuses_it(self, project: Path, case: str) -> None:
        before = _tree(project / "model")

        response = _change_set(project, SAFETY_CASES[case], approved_scope=[])

        assert response.get("error") == "unauthorized-delta", response
        assert _tree(project / "model") == before

    @pytest.mark.parametrize("case", sorted(SAFETY_CASES))
    def test_the_scope_that_covers_it_commits(self, project: Path, case: str) -> None:
        operations = SAFETY_CASES[case]
        scope = [{"operation": operation["operation"], "address": operation["address"]} for operation in operations]

        response = _change_set(project, operations, approved_scope=scope)

        assert response["status"] == "committed", response
