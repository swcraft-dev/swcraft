"""The inter-runnable variables through both write paths.

``writer/yaml_documents.py``'s hand-written ``swc_to_dict``/``runnable_to_dict`` (both importers, the
component export, every ``add_*`` mutation) and ``writer/elements.py``'s ``write_element`` (the editing
protocol). Guards a defect: the first pair dropped a component's ``variables`` and a runnable's access
lists, and the second wrote them without checking the names they emit. The serializer writes the
three lists only when non-empty, equal to ``element_to_dict`` over a component populating every field
of the ``swc`` layer; the mutations and the export's closure keep them; ``write_element`` refuses an
illegal or overlong name before anything is written; the editing protocol creates, updates and
removes them, with an **empty** safety verdict beside the ordinary scope review.

**Not reached:** the ARXML, the importer, the component import's regimes and the differ; every rated
model's safety verdict (the safety rules read no variable, and one rated shape shows it); the
protocol's ``check-model`` findings beyond the one dangling access
(``test_inter_runnable_variable_validator.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import editing_vector_fixtures as fixtures
import pytest
import yaml
from external_stubs_support import change, put
from inter_runnable_variable_support import (
    COMPONENT,
    COMPONENT_FILE,
    FILTERED,
    LOGGER,
    MONITOR,
    PEAK,
    SPARE,
    WRITER,
    variable_model,
)
from swcraft_tools.component_exchange.closure import compute_closure
from swcraft_tools.component_exchange.export import _materialize_closure
from swcraft_tools.editing.authorization import ApprovalDecision
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.model_registry import production_registry
from swcraft_tools.reader import read_swc_model
from swcraft_tools.schema import (
    CallReturnedEvent,
    DataEvent,
    InitEvent,
    OperationEvent,
    Port,
    PortComSpec,
    Runnable,
    SWComponent,
    TimingEvent,
)
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


def _variable_lists(document: dict[str, Any]) -> dict[str, Any]:
    """The three lists of a component document: its variables, and each runnable's two access lists."""
    return {
        "variables": document.get("variables", []),
        "accesses": {
            runnable["name"]: (runnable.get("reads_variables", []), runnable.get("writes_variables", []))
            for runnable in document["runnables"]
        },
    }


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


@pytest.fixture
def model(tmp_path: Path) -> Path:
    return variable_model(tmp_path / "model")


# The hand-written serializer


def _populated() -> SWComponent:
    """One component populating every field of every kind of the ``swc`` layer.

    Nothing here is meant to pass ``check-model``: the serializers do not judge, they write.
    """
    return SWComponent(
        name="Populated",
        type="service",
        description="Every field.",
        external=True,
        external_ref="/Elsewhere/Populated",
        external_dest="APPLICATION-SW-COMPONENT-TYPE",
        ports=[
            Port(
                name="pIn",
                direction="require",
                interface="IIn",
                observed_interface="IObserved",
                description="A port.",
                measures={"Speed": "REQ_M@1"},
                com_specs=[
                    PortComSpec(
                        data_element="Speed",
                        enable_update=True,
                        alive_timeout=0.5,
                        handle_never_received=True,
                        handle_timeout_type="replace",
                        handle_out_of_range="saturate",
                        uses_end_to_end_protection=False,
                        queue_length=4,
                        init=3,
                    )
                ],
                satisfies=["REQ_P@1"],
            )
        ],
        runnables=[
            Runnable(
                name="Run",
                min_start_interval=0.01,
                concurrent=True,
                events=[
                    TimingEvent(
                        type="timing",
                        period=0.01,
                        disabled_modes=[{"port": "pMode", "mode_group_ref": "/M/G", "mode_ref": "/M/G/OFF"}],
                        name="TMT_Stated",
                    ),
                    InitEvent(type="init", name="INIT_Stated"),
                    OperationEvent(type="operation", port="pServe", operation="Op", name="OIT_Stated"),
                    DataEvent(
                        type="data",
                        port="pIn",
                        element="Speed",
                        disabled_modes=[{"port": "pMode", "mode_group_ref": "/M/G", "mode_ref": "/M/G/OFF"}],
                        name="DRT_Stated",
                    ),
                    CallReturnedEvent(
                        type="call_returned", port="pCall", operation="Op", result="Returned", name="ASRT_Stated"
                    ),
                ],
                reads=[{"port": "pIn", "element": "Speed", "by": "value", "name": "R_Stated"}],
                writes=[{"port": "pOut", "element": "Speed", "by": "implicit", "name": "W_Stated"}],
                calls=[
                    {
                        "port": "pCall",
                        "operation": "Op",
                        "asynchronous": True,
                        "poll": True,
                        "result": "Polled",
                        "timeout": 0.5,
                        "name": "Call_Stated",
                    }
                ],
                reads_variables=[{"variable": "Shared", "name": "RV_Stated"}],
                writes_variables=[{"variable": "Other", "name": "WV_Stated"}],
                can_enter=[{"area": "Guard"}],
                runs_inside=[{"area": "Guard"}],
                description="A runnable.",
                symbol="Run_Symbol",
                wcet="400us",
                wcet_type="gross",
                satisfies=["REQ_R@1"],
            )
        ],
        variables=[
            {
                "name": "Shared",
                "access": "implicit",
                "application_type": "VehicleSpeed",
                "init": 0,
                "description": "A variable.",
                "sw_calibration_access": "read_only",
            },
            {"name": "Other", "access": "explicit", "implementation_type": "uint8", "init": [1, 2]},
        ],
        exclusive_areas=[{"name": "Guard", "description": "An area."}],
        memory={"RAM0": 64},
        asil="B",
        handle_termination_and_restart="no_support",
        data_type_mappings=["SpeedMapping"],
        behavior_name="PopulatedBehaviour",
        package="Chassis",
        satisfies=["REQ_C@1"],
    )


class TestTheHandWrittenSerializer:
    def test_it_writes_the_fixture_s_three_lists_in_field_order(self, model: Path) -> None:
        written = swc_to_dict(_component(model))

        assert _variable_lists(written) == _variable_lists(_load(model))
        assert list(written).index("variables") == list(written).index("runnables") + 1
        (monitor,) = (runnable for runnable in written["runnables"] if runnable["name"] == MONITOR)
        assert list(monitor) == ["name", "events", "reads_variables", "writes_variables"]

    def test_a_component_without_them_writes_no_key(self) -> None:
        written = swc_to_dict(SWComponent(name="Plain", runnables=[Runnable(name="Run")]))

        assert "variables" not in written
        assert list(written["runnables"][0]) == ["name", "events"]

    def test_an_access_writes_its_name_only_when_stated(self, model: Path) -> None:
        (logger,) = (runnable for runnable in _component(model).runnables if runnable.name == LOGGER)
        (monitor,) = (runnable for runnable in _component(model).runnables if runnable.name == MONITOR)

        assert runnable_to_dict(logger)["reads_variables"] == [{"variable": FILTERED, "name": "RV_Logged"}]
        assert runnable_to_dict(monitor)["writes_variables"] == [{"variable": PEAK}]

    def test_it_agrees_with_the_generic_serializer_over_the_fixture(self, model: Path) -> None:
        swc = _component(model)
        hand_written, generic = swc_to_dict(swc), element_to_dict(swc)

        assert hand_written == generic
        assert list(hand_written) == list(generic)

    def test_it_agrees_with_the_generic_serializer_over_a_populated_component(self) -> None:
        """``swc_to_dict(c) == element_to_dict(c)``, key order included, member by member."""
        swc = _populated()
        hand_written, generic = swc_to_dict(swc), element_to_dict(swc)

        assert hand_written == generic
        assert list(hand_written) == list(generic)
        for ours, theirs in zip(hand_written["runnables"], generic["runnables"], strict=True):
            assert list(ours) == list(theirs)
        for ours, theirs in zip(hand_written["variables"], generic["variables"], strict=True):
            assert list(ours) == list(theirs)
        for ours, theirs in zip(hand_written["exclusive_areas"], generic["exclusive_areas"], strict=True):
            assert list(ours) == list(theirs)

    def test_the_two_serializers_produce_the_same_bytes(self, tmp_path: Path) -> None:
        swc = _populated()
        written = Path(write_element(str(tmp_path), "swc/chassis/Populated.yaml", swc, create=True))

        assert written.read_text(encoding="utf-8") == yaml.safe_dump(
            swc_to_dict(swc), default_flow_style=False, sort_keys=False
        )

    def test_the_populated_component_really_populates_every_declared_field(self) -> None:
        """The populated component writes every declared field of the ``swc`` layer's kinds.

        ``element_to_dict`` omits a field at an omittable default, so the equality alone cannot state it;
        derived from the shipped registry, so a new field written by neither serializer fails by name.
        **Not reached:** that the value is a meaningful one.
        """
        document = element_to_dict(_populated())
        runnables = document["runnables"]
        events = [event for runnable in runnables for event in runnable["events"]]
        com_specs = [entry for port in document["ports"] for entry in port["com_specs"]]

        def keys(entries: list[dict[str, Any]]) -> set[str]:
            return {key for entry in entries for key in entry}

        written: dict[str, set[str]] = {
            "swc": set(document),
            "port": keys(document["ports"]),
            "port_com_spec": keys(com_specs),
            "runnable": keys(runnables),
            "timing_event": keys([event for event in events if event["type"] == "timing"]),
            "disabled_mode": keys([mode for event in events for mode in event.get("disabled_modes", [])]),
            "init_event": keys([event for event in events if event["type"] == "init"]),
            "operation_event": keys([event for event in events if event["type"] == "operation"]),
            "data_event": keys([event for event in events if event["type"] == "data"]),
            "call_returned_event": keys([event for event in events if event["type"] == "call_returned"]),
            "data_read": keys([read for runnable in runnables for read in runnable["reads"]]),
            "data_write": keys([write for runnable in runnables for write in runnable["writes"]]),
            "operation_call": keys([call for runnable in runnables for call in runnable["calls"]]),
            "variable": keys(document["variables"]),
            "variable_read": keys([read for runnable in runnables for read in runnable["reads_variables"]]),
            "variable_write": keys([write for runnable in runnables for write in runnable["writes_variables"]]),
            "exclusive_area": keys(document["exclusive_areas"]),
            "can_enter_area": keys([entry for runnable in runnables for entry in runnable["can_enter"]]),
            "runs_inside_area": keys([entry for runnable in runnables for entry in runnable["runs_inside"]]),
        }
        layer = next(facts for facts in production_registry().layers if facts.name == "swc")

        assert {kind.kind for kind in layer.kinds} == set(written)
        for kind in layer.kinds:
            declared = {field.name for field in kind.fields if not field.placement}
            missing = declared - written[kind.kind]
            assert not missing, (
                f"`_populated()` leaves {sorted(missing)} of `{kind.id}` at a default, so the serializer "
                "equality above compares two documents that are both missing it. Populate it here."
            )


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
#: file with no runnable and no variable, so it has nothing to keep): ``(prepare, call)``.
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


class TestEveryRewritingMutationKeepsTheVariables:
    """A preservation test per calling mutation: a hand-written dump never erases a field."""

    @pytest.mark.parametrize("mutation", sorted(REWRITING_MUTATIONS))
    def test_it_keeps_the_variables_and_every_access(self, model: Path, mutation: str) -> None:
        prepare, call = REWRITING_MUTATIONS[mutation]
        prepare(model)
        before = _variable_lists(_load(model))
        assert before["variables"] and before["accesses"][MONITOR][0]

        call(str(model))

        after = _variable_lists(_load(model))
        assert after["variables"] == before["variables"]
        for runnable, accesses in before["accesses"].items():
            assert after["accesses"][runnable] == accesses

    def test_the_mutation_set_is_every_rewriting_caller(self) -> None:
        """The table above against the module: every function calling ``swc_to_dict`` but ``create_swc``."""
        import ast
        import inspect

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
    def test_the_materialized_component_keeps_the_variables_and_every_access(self, model: Path, tmp_path: Path) -> None:
        """The mini model ``_materialize_closure`` writes through ``swc_to_dict`` keeps the variables and accesses."""
        root = tmp_path / "closure"
        _materialize_closure(compute_closure(str(model), COMPONENT), root)

        (component_file,) = (root / "swc").rglob(f"{COMPONENT}.yaml")
        assert _variable_lists(yaml.safe_load(component_file.read_text(encoding="utf-8"))) == _variable_lists(
            _load(model)
        )


# The write-path name guards


def _with(model_root: Path, mutate: Any) -> SWComponent:
    document = _load(model_root)
    mutate(document)
    return SWComponent.model_validate(document)


def _runnable_of(document: dict[str, Any], name: str) -> dict[str, Any]:
    (runnable,) = (runnable for runnable in document["runnables"] if runnable["name"] == name)
    return runnable


class TestTheWritePathGuards:
    def test_an_illegal_variable_name_is_refused_before_anything_is_written(self, model: Path) -> None:
        swc = _with(model, lambda document: document["variables"].append({**document["variables"][2], "name": "a b"}))
        before = _tree(model)

        with pytest.raises(ValueError, match="variable name") as raised:
            write_element(str(model), COMPONENT_FILE, swc)

        assert not isinstance(raised.value, ReservedNameError)
        assert "'a b'" in str(raised.value)
        assert _tree(model) == before

    def test_an_illegal_stated_access_name_is_refused(self, model: Path) -> None:
        swc = _with(model, lambda document: _runnable_of(document, LOGGER)["reads_variables"][0].update(name="RV a"))
        before = _tree(model)

        with pytest.raises(ValueError, match="stated name"):
            write_element(str(model), COMPONENT_FILE, swc)

        assert _tree(model) == before

    @pytest.mark.parametrize(("list_name", "prefix"), [("reads_variables", "RV_"), ("writes_variables", "WV_")])
    def test_an_overlong_derived_access_name_is_refused(self, model: Path, list_name: str, prefix: str) -> None:
        long_name = "V" * 126

        def mutate(document: dict[str, Any]) -> None:
            document["variables"].append({"name": long_name, "access": "explicit", "implementation_type": "uint8"})
            _runnable_of(document, WRITER).setdefault(list_name, []).append({"variable": long_name})

        swc = _with(model, mutate)
        before = _tree(model)

        with pytest.raises(ValueError, match=f"{prefix}{long_name}"):
            write_element(str(model), COMPONENT_FILE, swc)

        assert _tree(model) == before

    def test_a_derived_access_name_on_its_budget_is_written(self, model: Path) -> None:
        name = "V" * 125

        def mutate(document: dict[str, Any]) -> None:
            document["variables"].append({"name": name, "access": "explicit", "implementation_type": "uint8"})
            _runnable_of(document, WRITER)["writes_variables"].append({"variable": name})

        write_element(str(model), COMPONENT_FILE, _with(model, mutate))

        assert {"variable": name} in _runnable_of(_load(model), WRITER)["writes_variables"]

    def test_a_stated_name_replaces_the_derived_form_and_is_not_charged_its_budget(self, model: Path) -> None:
        long_name = "V" * 126

        def mutate(document: dict[str, Any]) -> None:
            document["variables"].append({"name": long_name, "access": "explicit", "implementation_type": "uint8"})
            _runnable_of(document, WRITER)["writes_variables"].append({"variable": long_name, "name": "WV_Long"})

        write_element(str(model), COMPONENT_FILE, _with(model, mutate))

        assert {"variable": long_name, "name": "WV_Long"} in _runnable_of(_load(model), WRITER)["writes_variables"]

    def test_a_stated_access_name_equal_to_a_derived_one_is_refused_as_a_collision(self, model: Path) -> None:
        swc = _with(
            model, lambda document: _runnable_of(document, MONITOR)["writes_variables"][0].update(name=f"RV_{PEAK}")
        )

        with pytest.raises(ReservedNameError, match=f"RV_{PEAK}"):
            write_element(str(model), COMPONENT_FILE, swc)

    def test_a_variable_init_the_dump_cannot_write_is_refused_as_the_rule(self, model: Path) -> None:
        swc = _with(model, lambda document: document["variables"][1].update(init=10**5000))
        before = _tree(model)

        with pytest.raises(ReservedNameError, match="interface.init-unwritable-integer") as raised:
            write_element(str(model), COMPONENT_FILE, swc)

        assert f"variable '{PEAK}'" in str(raised.value)
        assert _tree(model) == before


# The editing protocol


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The variable fixture as a project: the ``examples/basic`` manifest beside it."""
    root = tmp_path / "variable-project"
    root.mkdir()
    manifest = yaml.safe_load(fixtures.BASE_MANIFEST.read_text(encoding="utf-8"))
    (root / "swcraft.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8", newline="\n")
    variable_model(root / "model")
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
        {"operation": "change_set", "baseline": token, "intent": "variable edit", "operations": operations},
        project=str(project),
        review=review or _Review(),
        approved_scope=approved_scope,
        scope_id=None if approved_scope is None else "turn-1",
    )


def _variable_address(name: str | None = None, *tail: dict[str, Any]) -> dict[str, Any]:
    path: list[dict[str, Any]] = [{"field": "variables"}]
    if name is not None:
        path.append({"member": name})
    return {"layer": "swc", "element": COMPONENT, "path": [*path, *tail]}


def _access_address(runnable: str, list_name: str, variable: str, *tail: dict[str, Any]) -> dict[str, Any]:
    return {
        "layer": "swc",
        "element": COMPONENT,
        "path": [{"field": "runnables"}, {"member": runnable}, {"field": list_name}, {"member": variable}, *tail],
    }


def _create(address: dict[str, Any], value: Any) -> dict[str, Any]:
    return {"operation": "create", "address": address, "value": value}


def _update(address: dict[str, Any], value: Any) -> dict[str, Any]:
    return {"operation": "update", "address": address, "value": value}


def _remove(address: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "remove", "address": address}


def _variables(project: Path) -> list[dict[str, Any]]:
    return _load(project / "model").get("variables", [])


def _accesses(project: Path, runnable: str, list_name: str) -> list[dict[str, Any]]:
    return _runnable_of(_load(project / "model"), runnable).get(list_name, [])


def _findings(project: Path) -> list[Any]:
    return list(check_cross_layer_consistency(str(project / "model")).records)


NEW_VARIABLE: dict[str, Any] = {"name": "ResetRequest", "access": "explicit", "implementation_type": "uint8", "init": 0}


class TestTheProtocolOnAVariable:
    def test_a_create_with_no_refusal_and_no_finding(self, project: Path) -> None:
        response = _change_set(project, [_create(_variable_address(NEW_VARIABLE["name"]), NEW_VARIABLE)])

        assert response["status"] == "committed", response
        assert _variables(project)[-1] == NEW_VARIABLE
        assert _findings(project) == []

    def test_an_access_change_is_an_update(self, project: Path) -> None:
        response = _change_set(project, [_update(_variable_address(SPARE, {"field": "access"}), "implicit")])

        assert response["status"] == "committed", response
        assert _variables(project)[2] == {"name": SPARE, "access": "implicit", "implementation_type": "uint8"}

    def test_an_init_and_a_description_are_updates(self, project: Path) -> None:
        response = _change_set(
            project,
            [
                _update(_variable_address(SPARE, {"field": "init"}), 7),
                _update(_variable_address(SPARE, {"field": "description"}), "Spare."),
            ],
        )

        assert response["status"] == "committed", response
        assert _variables(project)[2]["init"] == 7
        assert _variables(project)[2]["description"] == "Spare."

    def test_a_rename_by_update_is_refused(self, project: Path) -> None:
        before = _tree(project / "model")

        response = _change_set(project, [_update(_variable_address(SPARE, {"field": "name"}), "Renamed")])

        assert response.get("error") == "immutable-field", response
        assert _tree(project / "model") == before

    def test_a_rename_is_a_remove_and_a_create(self, project: Path) -> None:
        renamed = {"name": "SpareCount", "access": "explicit", "implementation_type": "uint8"}

        response = _change_set(
            project, [_remove(_variable_address(SPARE)), _create(_variable_address("SpareCount"), renamed)]
        )

        assert response["status"] == "committed", response
        assert [variable["name"] for variable in _variables(project)] == [FILTERED, PEAK, "SpareCount"]

    def test_a_remove(self, project: Path) -> None:
        response = _change_set(project, [_remove(_variable_address(SPARE))])

        assert response["status"] == "committed", response
        assert [variable["name"] for variable in _variables(project)] == [FILTERED, PEAK]

    def test_removing_a_variable_an_access_names_is_refused_at_pass_d(self, project: Path) -> None:
        """The dangling access: every entry naming the removed variable is the broken reference."""
        before = _tree(project / "model")

        response = _change_set(project, [_remove(_variable_address(PEAK))])

        assert response.get("error") == "broken-reference", response
        assert f"declares no variable '{PEAK}'" in response["message"], response
        assert _tree(project / "model") == before

    def test_a_read_answers_the_full_values(self, project: Path) -> None:
        response = handle_request({"operation": "read", "address": _variable_address(SPARE)}, project=str(project))

        assert response["value"] == {
            "name": SPARE,
            "access": "explicit",
            "application_type": None,
            "implementation_type": "uint8",
            "init": None,
            "description": "",
            "sw_calibration_access": None,
        }


class TestTheProtocolOnAnAccess:
    @pytest.mark.parametrize(("list_name", "runnable"), [("reads_variables", WRITER), ("writes_variables", LOGGER)])
    def test_a_create(self, project: Path, list_name: str, runnable: str) -> None:
        response = _change_set(project, [_create(_access_address(runnable, list_name, PEAK), {"variable": PEAK})])

        assert response["status"] == "committed", response
        assert _accesses(project, runnable, list_name) == [{"variable": PEAK}]
        assert _findings(project) == []

    def test_a_stated_name_is_an_update_and_clearing_it_is_one_too(self, project: Path) -> None:
        address = _access_address(MONITOR, "writes_variables", PEAK, {"field": "name"})

        response = _change_set(project, [_update(address, "WV_Peak")])
        assert response["status"] == "committed", response
        assert _accesses(project, MONITOR, "writes_variables") == [{"variable": PEAK, "name": "WV_Peak"}]

        response = _change_set(project, [_update(address, None)])
        assert response["status"] == "committed", response
        assert _accesses(project, MONITOR, "writes_variables") == [{"variable": PEAK}]

    def test_repointing_an_access_by_update_is_refused(self, project: Path) -> None:
        before = _tree(project / "model")

        response = _change_set(
            project, [_update(_access_address(MONITOR, "reads_variables", PEAK, {"field": "variable"}), SPARE)]
        )

        assert response.get("error") == "immutable-field", response
        assert _tree(project / "model") == before

    @pytest.mark.parametrize(
        ("runnable", "list_name", "variable", "kept"),
        [
            (MONITOR, "reads_variables", PEAK, [{"variable": FILTERED}]),
            (MONITOR, "writes_variables", PEAK, []),
        ],
    )
    def test_a_remove(self, project: Path, runnable: str, list_name: str, variable: str, kept: list[Any]) -> None:
        response = _change_set(project, [_remove(_access_address(runnable, list_name, variable))])

        assert response["status"] == "committed", response
        assert _accesses(project, runnable, list_name) == kept

    def test_an_access_to_a_variable_created_in_the_same_change_set(self, project: Path) -> None:
        response = _change_set(
            project,
            [
                _create(_variable_address(NEW_VARIABLE["name"]), NEW_VARIABLE),
                _create(
                    _access_address(WRITER, "writes_variables", NEW_VARIABLE["name"]), {"variable": "ResetRequest"}
                ),
                _create(_access_address(LOGGER, "reads_variables", NEW_VARIABLE["name"]), {"variable": "ResetRequest"}),
            ],
        )

        assert response["status"] == "committed", response
        assert {"variable": "ResetRequest"} in _accesses(project, WRITER, "writes_variables")
        assert _findings(project) == []

    def test_an_illegal_stated_access_name_is_refused_with_its_code(self, project: Path) -> None:
        before = _tree(project / "model")

        response = _change_set(
            project, [_update(_access_address(LOGGER, "reads_variables", FILTERED, {"field": "name"}), "RV a")]
        )

        assert response.get("error") == "invalid-identifier", response
        assert _tree(project / "model") == before

    def test_a_variable_init_the_dump_cannot_write_is_refused_as_the_rule_s_code(self, project: Path) -> None:
        before = _tree(project / "model")

        response = _change_set(project, [_update(_variable_address(PEAK, {"field": "init"}), 10**5000)])

        assert response.get("error") == "consistency-violation", response
        assert _tree(project / "model") == before


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
    "create a variable": [_create(_variable_address(NEW_VARIABLE["name"]), NEW_VARIABLE)],
    "change a variable's access": [_update(_variable_address(SPARE, {"field": "access"}), "implicit")],
    "remove a variable": [_remove(_variable_address(SPARE))],
    "create a read": [_create(_access_address(WRITER, "reads_variables", PEAK), {"variable": PEAK})],
    "remove a write": [_remove(_access_address(MONITOR, "writes_variables", PEAK))],
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
