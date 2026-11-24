"""Where a data-received runnable's data comes from (``swcraft_tools.data_arrival``).

Each case builds a model under ``tmp_path`` and writes the expected
:class:`~swcraft_tools.data_arrival.Arrival` from that model.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from conftest import _write, element_path
from swcraft_tools import data_arrival
from swcraft_tools.data_arrival import (
    BOUNDED,
    DELEGATED_OUT_OF_ROOT,
    EXTERNAL_SENDER,
    NO_CONNECTOR,
    NO_SINGLE_ROOT,
    NO_WRITER,
    NOT_DERIVED,
    NOT_DERIVED_CASES,
    PAST_THE_CAP,
    UNKNOWN,
    UNPLACED,
    UNRESOLVED,
    WRITER_IN_NO_TASK,
    Arrival,
    arrivals,
)
from swcraft_tools.scheduling_identity import PATH_CAP

ELEMENT = "Speed"
INTERFACE = "ISpeed"
SENDER_PORT = "pSpeed"
RECEIVER_PORT = "rSpeed"
RECEIVER = "WheelMonitor"
RECEIVING_RUNNABLE = "OnSpeed"
SENDER = "WheelSpeed"
WRITER = "Sample"


class _Model:
    """A hand-built model: each method writes one element file under *root*."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.interface(INTERFACE)

    def interface(self, name: str, elements: tuple[str, ...] = (ELEMENT,)) -> None:
        _write(
            element_path(self.root, "interfaces", name),
            {
                "name": name,
                "kind": "sender_receiver",
                "data_elements": [{"name": element, "implementation_type": "uint8"} for element in elements],
            },
        )

    def put(self, layer: str, document: dict[str, Any], **placement: str) -> None:
        _write(element_path(self.root, layer, document["name"], **placement), document)

    def component(self, name: str, *, ports: list[dict], runnables: list[dict], **extra: Any) -> None:
        self.put(
            "swc", {"name": name, "type": "application", "ports": ports, "runnables": runnables, **extra}, group="g"
        )

    def sender(
        self, name: str = SENDER, *, port: str = SENDER_PORT, interface: str = INTERFACE, **runnables: Any
    ) -> None:
        """A component whose runnables (name -> extra keys) each write ``(port, Speed)``; ``Sample`` by default."""
        named = runnables or {WRITER: {}}
        self.component(
            name,
            ports=[{"name": port, "direction": "provide", "interface": interface}],
            runnables=[
                {"name": runnable, "writes": [{"port": port, "element": ELEMENT}], **keys}
                for runnable, keys in named.items()
            ],
        )

    def receiver(
        self,
        name: str = RECEIVER,
        *,
        direction: str = "require",
        interface: str = INTERFACE,
        event_port: str = RECEIVER_PORT,
        element: str = ELEMENT,
    ) -> None:
        self.component(
            name,
            ports=[{"name": RECEIVER_PORT, "direction": direction, "interface": interface}],
            runnables=[
                {
                    "name": RECEIVING_RUNNABLE,
                    "events": [{"type": "data", "port": event_port, "element": element}],
                    "reads": [{"port": RECEIVER_PORT, "element": ELEMENT}],
                }
            ],
        )

    def composition(self, name: str, members: dict[str, str], *, root: bool = False, delegations: tuple = ()) -> None:
        """*members* maps a member name to a component type, or ``"@Name"`` to a composition."""
        self.put(
            "compositions",
            {
                "name": name,
                "title": name,
                "root": root,
                "members": [
                    {"name": member, "composition": kind[1:]}
                    if kind.startswith("@")
                    else {"name": member, "type": kind}
                    for member, kind in members.items()
                ],
                "delegations": [
                    {"name": f"D_{port}_{member}", "port": port, "member": member, "member_port": member_port}
                    for port, member, member_port in delegations
                ],
            },
        )

    def connector(
        self, name: str, source: tuple[str, str], target: tuple[str, str], *, composition: str | None = None
    ) -> None:
        """An endpoint is ``(type, port)``, or ``("@Composition", port)``."""

        def end(endpoint: tuple[str, str]) -> dict[str, str]:
            kind, port = endpoint
            return {"composition": kind[1:], "port": port} if kind.startswith("@") else {"swc": kind, "port": port}

        document: dict[str, Any] = {"name": name, "from": end(source), "to": end(target)}
        if composition is not None:
            document["composition"] = composition
        self.put("connectors", document)

    def task(self, name: str, period: float, *entries: dict[str, str]) -> None:
        self.put("tasks", {"name": name, "period": period, "priority": 1, "partition": "P", "runnables": list(entries)})

    def arrivals(self) -> tuple[Arrival, ...]:
        return arrivals(str(self.root))


def _swc(component: str, runnable: str = WRITER) -> dict[str, str]:
    return {"swc": component, "runnable": runnable}


def _instance(path: str, runnable: str = WRITER) -> dict[str, str]:
    return {"instance": path, "runnable": runnable}


def _bound(occurrence: str, period: float, writer: str, task: str, more: int = 0) -> Arrival:
    return Arrival(occurrence, RECEIVING_RUNNABLE, RECEIVER_PORT, ELEMENT, BOUNDED, period, writer, task, more=more)


def _unknown(occurrence: str, case: str, more: int = 0) -> Arrival:
    return Arrival(occurrence, RECEIVING_RUNNABLE, RECEIVER_PORT, ELEMENT, UNKNOWN, case=case, more=more)


def _not_derived(case: str, *, port: str = RECEIVER_PORT, element: str = ELEMENT, more: int = 0) -> Arrival:
    return Arrival(RECEIVER, RECEIVING_RUNNABLE, port, element, NOT_DERIVED, case=case, more=more)


def _flat(tmp_path: Path) -> _Model:
    """The plain case: ``Sensors`` (WheelSpeed) wired to ``Monitor`` (WheelMonitor) in the root."""
    model = _Model(tmp_path)
    model.sender()
    model.receiver()
    model.composition("Vehicle", {"Sensors": SENDER, "Monitor": RECEIVER}, root=True)
    model.connector("C1", (SENDER, SENDER_PORT), (RECEIVER, RECEIVER_PORT))
    return model


# Bounds


def test_one_writer_in_one_task_bounds_the_arrival_by_its_period(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.task("Task_10ms", 0.01, _swc(SENDER))
    assert model.arrivals() == (_bound("Monitor", 0.01, "Sensors.Sample", "Task_10ms"),)


def test_two_writers_in_two_tasks_the_shortest_period_wins_and_the_other_is_counted(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.sender(Sample={}, Fast={})
    model.task("Task_20ms", 0.02, _swc(SENDER, "Sample"))
    model.task("Task_5ms", 0.005, _swc(SENDER, "Fast"))
    assert model.arrivals() == (_bound("Monitor", 0.005, "Sensors.Fast", "Task_5ms", more=1),)


def test_a_writer_mapped_to_two_tasks_is_bounded_by_the_shorter_period(tmp_path: Path) -> None:
    """``check-model`` flags the model (``task.runnable-mapped-to-multiple-tasks``); the bound is not optimistic."""
    model = _flat(tmp_path)
    model.task("Task_20ms", 0.02, _swc(SENDER))
    model.task("Task_5ms", 0.005, _swc(SENDER))
    assert model.arrivals() == (_bound("Monitor", 0.005, "Sensors.Sample", "Task_5ms"),)


def test_a_writer_mapped_to_two_tasks_of_one_period_takes_the_first_in_reader_order(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.task("Task_B", 0.01, _swc(SENDER))
    model.task("Task_A", 0.01, _swc(SENDER))
    assert model.arrivals() == (_bound("Monitor", 0.01, "Sensors.Sample", "Task_A"),)


def test_two_writers_of_one_period_the_first_in_walk_order_is_named(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.sender(Sample={}, Fast={})
    model.task("Task_A", 0.01, _swc(SENDER, "Fast"))
    model.task("Task_B", 0.01, _swc(SENDER, "Sample"))
    assert model.arrivals() == (_bound("Monitor", 0.01, "Sensors.Sample", "Task_B", more=1),)


def test_a_connector_reaching_the_port_takes_precedence_over_a_delegation_of_it(tmp_path: Path) -> None:
    """The walk goes up through a delegation (step 4) only when no connector reaches the port."""
    model = _Model(tmp_path)
    model.sender()
    model.receiver()
    model.composition(
        "Inner",
        {"Sensors": SENDER, "Monitor": RECEIVER},
        delegations=((RECEIVER_PORT, "Monitor", RECEIVER_PORT),),
    )
    model.composition("Vehicle", {"Box": "@Inner"}, root=True)
    model.connector("C1", (SENDER, SENDER_PORT), (RECEIVER, RECEIVER_PORT), composition="Inner")
    model.task("Task_10ms", 0.01, _swc(SENDER))
    assert model.arrivals() == (_bound("Box/Monitor", 0.01, "Box/Sensors.Sample", "Task_10ms"),)


def test_a_fan_in_of_two_periodic_connectors_takes_the_shortest(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.sender("RearSpeed")
    model.composition("Vehicle", {"Sensors": SENDER, "Rear": "RearSpeed", "Monitor": RECEIVER}, root=True)
    model.connector("C2", ("RearSpeed", SENDER_PORT), (RECEIVER, RECEIVER_PORT))
    model.task("Task_20ms", 0.02, _swc(SENDER))
    model.task("Task_10ms", 0.01, _swc("RearSpeed"))
    assert model.arrivals() == (_bound("Monitor", 0.01, "Rear.Sample", "Task_10ms", more=1),)


def test_a_source_two_levels_down_is_followed_through_nested_delegations(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.sender()
    model.receiver()
    model.composition("Corner", {"Sensor": SENDER}, delegations=((SENDER_PORT, "Sensor", SENDER_PORT),))
    model.composition("FrontAxle", {"Left": "@Corner"}, delegations=((SENDER_PORT, "Left", SENDER_PORT),))
    model.composition("Vehicle", {"Front": "@FrontAxle", "Monitor": RECEIVER}, root=True)
    model.connector("C1", ("@FrontAxle", SENDER_PORT), (RECEIVER, RECEIVER_PORT))
    model.task("Task_10ms", 0.01, _instance("Front/Left/Sensor"))
    assert model.arrivals() == (_bound("Monitor", 0.01, "Front/Left/Sensor.Sample", "Task_10ms"),)


def test_a_receiver_two_levels_down_is_delegated_up_and_then_connected(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.sender()
    model.receiver()
    model.composition("BrakeCore", {"Monitor": RECEIVER}, delegations=((RECEIVER_PORT, "Monitor", RECEIVER_PORT),))
    model.composition("Brakes", {"Inner": "@BrakeCore"}, delegations=((RECEIVER_PORT, "Inner", RECEIVER_PORT),))
    model.composition("Vehicle", {"Sensors": SENDER, "Brake": "@Brakes"}, root=True)
    model.connector("C1", (SENDER, SENDER_PORT), ("@Brakes", RECEIVER_PORT))
    model.task("Task_10ms", 0.01, _swc(SENDER))
    assert model.arrivals() == (_bound("Brake/Inner/Monitor", 0.01, "Sensors.Sample", "Task_10ms"),)


def test_a_type_placed_twice_gives_two_lines_with_two_bounds(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.sender()
    model.receiver()
    model.composition("Side", {"Sensor": SENDER, "Monitor": RECEIVER})
    model.composition("Vehicle", {"L": "@Side", "R": "@Side"}, root=True)
    model.connector("C1", (SENDER, SENDER_PORT), (RECEIVER, RECEIVER_PORT), composition="Side")
    model.task("Task_10ms", 0.01, _instance("L/Sensor"))
    model.task("Task_20ms", 0.02, _instance("R/Sensor"))
    assert model.arrivals() == (
        _bound("L/Monitor", 0.01, "L/Sensor.Sample", "Task_10ms"),
        _bound("R/Monitor", 0.02, "R/Sensor.Sample", "Task_20ms"),
    )


def test_a_writer_that_is_itself_data_received_is_bounded_by_its_own_task(tmp_path: Path) -> None:
    """A writer's own trigger is not read: it is bounded by its own task."""
    model = _flat(tmp_path)
    model.component(
        SENDER,
        ports=[
            {"name": SENDER_PORT, "direction": "provide", "interface": INTERFACE},
            {"name": "rRaw", "direction": "require", "interface": INTERFACE},
        ],
        runnables=[
            {
                "name": WRITER,
                "events": [{"type": "data", "port": "rRaw", "element": ELEMENT}],
                "writes": [{"port": SENDER_PORT, "element": ELEMENT}],
            }
        ],
    )
    model.task("Task_5ms", 0.005, _swc(SENDER))
    answer = model.arrivals()
    assert _bound("Monitor", 0.005, "Sensors.Sample", "Task_5ms") in answer
    # The writer's own event is a second receiving runnable, and nothing feeds its port.
    assert Arrival("Sensors", WRITER, "rRaw", ELEMENT, UNKNOWN, case=NO_CONNECTOR) in answer


def test_a_port_on_an_external_interface_stub_listing_the_element_is_followed(tmp_path: Path) -> None:
    """A stub hides the interface's content, not who sends: the port is followed."""
    model = _Model(tmp_path)
    model.put(
        "interfaces",
        {
            "name": "IStub",
            "kind": "external_interface",
            "external_ref": "/Other/IStub",
            "external_dest": "SENDER-RECEIVER-INTERFACE",
            "data_elements": [ELEMENT],
        },
    )
    model.sender(interface="IStub")
    model.receiver(interface="IStub")
    model.composition("Vehicle", {"Sensors": SENDER, "Monitor": RECEIVER}, root=True)
    model.connector("C1", (SENDER, SENDER_PORT), (RECEIVER, RECEIVER_PORT))
    model.task("Task_10ms", 0.01, _swc(SENDER))
    assert model.arrivals() == (_bound("Monitor", 0.01, "Sensors.Sample", "Task_10ms"),)


# Unknown: the closed list, one test per case


def test_no_connector_reaches_the_port(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    (tmp_path / "connectors" / "C1.yaml").unlink()
    model.task("Task_10ms", 0.01, _swc(SENDER))
    assert model.arrivals() == (_unknown("Monitor", NO_CONNECTOR),)


def test_a_delegation_out_of_the_root_ends_outside_the_model(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.receiver()
    model.composition(
        "Vehicle", {"Monitor": RECEIVER}, root=True, delegations=((RECEIVER_PORT, "Monitor", RECEIVER_PORT),)
    )
    assert model.arrivals() == (_unknown("Monitor", DELEGATED_OUT_OF_ROOT),)


def test_two_delegations_out_of_the_root_are_two_outside_sources(tmp_path: Path) -> None:
    """Each root boundary port is fed from outside on its own, as below the root each is followed."""
    model = _Model(tmp_path)
    model.receiver()
    model.composition(
        "Vehicle",
        {"Monitor": RECEIVER},
        root=True,
        delegations=(("rSpeedA", "Monitor", RECEIVER_PORT), ("rSpeedB", "Monitor", RECEIVER_PORT)),
    )
    assert model.arrivals() == (_unknown("Monitor", DELEGATED_OUT_OF_ROOT, more=1),)


def test_an_external_component_sender_is_outside_the_model(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.put(
        "swc",
        {
            "name": SENDER,
            "external": True,
            "external_ref": "/Other/WheelSpeed",
            "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
        },
        group="g",
    )
    assert model.arrivals() == (_unknown("Monitor", EXTERNAL_SENDER),)


def test_a_sender_with_no_runnable_writing_the_element_is_outside_the_model(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.component(
        SENDER,
        ports=[{"name": SENDER_PORT, "direction": "provide", "interface": INTERFACE}],
        runnables=[{"name": WRITER}],
    )
    model.task("Task_10ms", 0.01, _swc(SENDER))
    assert model.arrivals() == (_unknown("Monitor", NO_WRITER),)


def test_a_writer_in_no_task_is_outside_the_model(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    assert model.arrivals() == (_unknown("Monitor", WRITER_IN_NO_TASK),)


def test_a_task_entry_naming_a_type_placed_twice_maps_no_writer(tmp_path: Path) -> None:
    """``swc: WheelSpeed`` resolves to no one occurrence (``check-model``'s ``instance-ambiguous``): a stated limit."""
    model = _flat(tmp_path)
    model.composition("Vehicle", {"Sensors": SENDER, "Spare": SENDER, "Monitor": RECEIVER}, root=True)
    model.task("Task_10ms", 0.01, _swc(SENDER))
    assert model.arrivals() == (_unknown("Monitor", WRITER_IN_NO_TASK),)


def test_a_connector_from_a_type_the_composition_does_not_place_draws_nothing(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.composition("Vehicle", {"Monitor": RECEIVER}, root=True)
    model.task("Task_10ms", 0.01, _swc(SENDER))
    assert model.arrivals() == (_unknown("Monitor", NO_CONNECTOR),)


def test_a_source_composition_port_no_inner_delegation_reaches_has_no_writer(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.sender()
    model.receiver()
    model.composition("Corner", {"Sensor": SENDER})
    model.composition("Vehicle", {"Front": "@Corner", "Monitor": RECEIVER}, root=True)
    model.connector("C1", ("@Corner", SENDER_PORT), (RECEIVER, RECEIVER_PORT))
    model.task("Task_10ms", 0.01, _instance("Front/Sensor"))
    assert model.arrivals() == (_unknown("Monitor", NO_WRITER),)


def test_a_sender_type_no_component_file_declares_has_no_writer(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.receiver()
    model.composition("Vehicle", {"Sensors": SENDER, "Monitor": RECEIVER}, root=True)
    model.connector("C1", (SENDER, SENDER_PORT), (RECEIVER, RECEIVER_PORT))
    assert model.arrivals() == (_unknown("Monitor", NO_WRITER),)


def test_a_fan_in_of_a_periodic_source_and_an_outside_one_reads_unknown_and_names_the_outside_case(
    tmp_path: Path,
) -> None:
    model = _flat(tmp_path)
    model.put(
        "swc",
        {
            "name": "Gateway",
            "external": True,
            "external_ref": "/Other/Gateway",
            "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
        },
        group="g",
    )
    model.composition("Vehicle", {"Sensors": SENDER, "Gw": "Gateway", "Monitor": RECEIVER}, root=True)
    model.connector("C2", ("Gateway", SENDER_PORT), (RECEIVER, RECEIVER_PORT))
    model.task("Task_10ms", 0.01, _swc(SENDER))
    assert model.arrivals() == (_unknown("Monitor", EXTERNAL_SENDER),)


def test_further_outside_sources_are_counted_after_the_first_in_walk_order(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.sender("RearSpeed")
    model.composition("Vehicle", {"Sensors": SENDER, "Rear": "RearSpeed", "Monitor": RECEIVER}, root=True)
    model.connector("C2", ("RearSpeed", SENDER_PORT), (RECEIVER, RECEIVER_PORT))
    assert model.arrivals() == (_unknown("Monitor", WRITER_IN_NO_TASK, more=1),)


# Not derived


def test_no_root_composition_is_not_derived(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.composition("Vehicle", {"Sensors": SENDER, "Monitor": RECEIVER})
    assert model.arrivals() == (_not_derived(NO_SINGLE_ROOT),)


def test_two_roots_are_not_derived(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.composition("Other", {"Monitor": RECEIVER}, root=True)
    assert model.arrivals() == (_not_derived(NO_SINGLE_ROOT),)


def test_a_cycle_the_root_reaches_is_not_derived(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.composition("A", {"Next": "@B"})
    model.composition("B", {"Back": "@A"})
    model.composition("Vehicle", {"Sensors": SENDER, "Monitor": RECEIVER, "Loop": "@A"}, root=True)
    assert model.arrivals() == (_not_derived(NO_SINGLE_ROOT),)


def test_a_type_placed_in_no_composition_is_not_derived(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.composition("Vehicle", {"Sensors": SENDER}, root=True)
    assert model.arrivals() == (_not_derived(UNPLACED),)


def test_an_unknown_port_does_not_resolve(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.receiver(event_port="rNowhere")
    assert model.arrivals() == (_not_derived(UNRESOLVED, port="rNowhere"),)


def test_a_provide_port_does_not_resolve(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.receiver(direction="provide")
    assert model.arrivals() == (_not_derived(UNRESOLVED),)


def test_a_client_server_port_does_not_resolve(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.put("interfaces", {"name": "IOp", "kind": "client_server", "operations": [{"name": "Do"}]})
    model.receiver(interface="IOp")
    assert model.arrivals() == (_not_derived(UNRESOLVED),)


def test_an_unknown_element_does_not_resolve(tmp_path: Path) -> None:
    model = _flat(tmp_path)
    model.receiver(element="Torque")
    assert model.arrivals() == (_not_derived(UNRESOLVED, element="Torque"),)


# The empty answer, the cap and the import set


def test_a_model_with_no_data_received_event_answers_nothing_and_reads_no_other_layer(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.sender()
    (tmp_path / "connectors").mkdir()
    (tmp_path / "connectors" / "Broken.yaml").write_text("name: [unclosed\n", encoding="utf-8", newline="\n")
    assert model.arrivals() == ()


def test_a_missing_model_root_answers_nothing(tmp_path: Path) -> None:
    """The report over a missing root names the absent hardware layer; the walk has nothing to add."""
    assert arrivals(str(tmp_path / "absent")) == ()


def test_occurrences_past_the_cap_are_counted_and_not_walked(tmp_path: Path) -> None:
    model = _Model(tmp_path)
    model.receiver()
    model.composition("Wrap", {"Monitor": RECEIVER})
    placed = PATH_CAP + 3
    model.composition("Vehicle", {f"W{index:03d}": "@Wrap" for index in range(placed)}, root=True)
    answer = model.arrivals()
    walked = [f"W{index:03d}/Monitor" for index in range(PATH_CAP)]
    assert [arrival.occurrence for arrival in answer[:-1]] == walked
    assert all(arrival.case == NO_CONNECTOR for arrival in answer[:-1])
    assert answer[-1] == _not_derived(PAST_THE_CAP, more=3)


def _diamond(model: _Model, depth: int) -> None:
    """Build a diamond of *depth* levels: ``2**depth`` senders reach the one receiver through one connector."""
    model.receiver()
    for side in "AB":
        model.sender(f"Sensor{side}")
        model.composition(f"L0{side}", {"s": f"Sensor{side}"}, delegations=((SENDER_PORT, "s", SENDER_PORT),))
    for level in range(1, depth + 1):
        for side in "AB":
            model.composition(
                f"L{level}{side}",
                {"a": f"@L{level - 1}A", "b": f"@L{level - 1}B"},
                delegations=((SENDER_PORT, "a", SENDER_PORT), (SENDER_PORT, "b", SENDER_PORT)),
            )
    model.composition("Vehicle", {"Src": f"@L{depth}A", "Monitor": RECEIVER}, root=True)
    model.connector("C1", (f"@L{depth}A", SENDER_PORT), (RECEIVER, RECEIVER_PORT))


def _count_walks(monkeypatch: Any) -> list[int]:
    """How many times a sender composition's answer is computed rather than read from the memo."""
    calls = [0]
    computed = data_arrival._sender_below

    def counted(*args: Any) -> Any:
        calls[0] += 1
        return computed(*args)

    monkeypatch.setattr(data_arrival, "_sender_below", counted)
    return calls


def test_a_deep_diamond_is_walked_once_per_composition_and_counts_every_sender(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Memoised, each of the 50 compositions is walked once (unmemoised, depth 24 is 16 777 216 senders)."""
    depth = 24
    model = _Model(tmp_path)
    _diamond(model, depth)
    calls = _count_walks(monkeypatch)
    assert model.arrivals() == (_unknown("Monitor", WRITER_IN_NO_TASK, more=2**depth - 1),)
    assert calls[0] <= 2 * (depth + 1)


def test_a_deep_diamond_with_a_mapped_writer_walks_its_path_apart_and_keeps_every_count_exact(
    tmp_path: Path, monkeypatch: Any
) -> None:
    depth = 22
    model = _Model(tmp_path)
    _diamond(model, depth)
    model.task("Task_10ms", 0.01, _instance("/".join(["Src", *["b"] * depth, "s"])))
    calls = _count_walks(monkeypatch)
    assert model.arrivals() == (_unknown("Monitor", WRITER_IN_NO_TASK, more=2**depth - 2),)
    assert calls[0] <= 3 * (depth + 1)


def test_a_diamond_with_every_writer_mapped_names_the_first_shortest_in_walk_order_by_its_full_path(
    tmp_path: Path,
) -> None:
    model = _Model(tmp_path)
    _diamond(model, 2)
    periods = {"a/a": 0.02, "a/b": 0.005, "b/a": 0.005, "b/b": 0.01}
    for index, (below, period) in enumerate(periods.items()):
        model.task(f"Task_{index}", period, _instance(f"Src/{below}/s"))
    assert model.arrivals() == (_bound("Monitor", 0.005, "Src/a/b/s.Sample", "Task_1", more=3),)


def test_the_cap_case_is_a_member_of_the_closed_list_and_names_no_count_of_the_model() -> None:
    """The count travels in ``more``; the case is one fixed string, as every other case is."""
    assert PAST_THE_CAP in NOT_DERIVED_CASES
    assert PAST_THE_CAP == f"the occurrences past the first {PATH_CAP} are not walked"


def test_the_leaf_imports_the_schema_the_reader_and_the_scheduling_identity_and_nothing_else() -> None:
    """Neither the validator nor the writer is reached."""
    tree = ast.parse(Path(data_arrival.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("swcraft_tools"):
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names if alias.name.startswith("swcraft_tools")}
    assert imported == {"swcraft_tools.schema", "swcraft_tools.reader", "swcraft_tools.scheduling_identity"}
