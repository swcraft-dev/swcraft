"""The test tables' fact rows, ids and hashes, and the test side's annotations.

Every expected row here is written **by hand** from the YAML the test writes —
never by calling the module under test or the resolver it uses — so the rows
are checked against an oracle independent of the projection. The hash literals
were computed outside the module, from the preimages spelled out beside them.
"""

from __future__ import annotations

import ast
import copy
import csv
import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import element_path, make_empty_model_root, model_element_path
from swcraft_tools import tables_for_testing
from swcraft_tools.tables_for_testing import (
    COMPONENTS,
    NO_PIN,
    ORPHANS,
    PLACEMENT,
    SCHEDULING,
    TEST_COLUMNS,
    Annotations,
    AnnotationsError,
    Table,
    TestTables,
    annotations_location_refusal,
    build_test_tables,
    facts_digest,
    facts_hash,
    orphans_csv,
    read_annotations,
    table_csv,
)
from symlink_support import symlink_or_skip

# ---------------------------------------------------------------------------
# The model: two occurrences of one type, a nested occurrence, an unplaced type
# mapped by a `swc:` entry, an unplaced unmapped type, an external stub with no
# runnables, an unmapped runnable, a runnable with timing + data events and
# disabled modes, a declared level and a derived one.
# ---------------------------------------------------------------------------

DISABLED = {"port": "rMode", "mode_group_ref": "/Modes/Ecu", "mode_ref": "/Modes/Ecu/Off"}


def _model() -> dict[str, Any]:
    """Every element of the model, by a key the tests mutate before writing."""
    return {
        "swc": {
            "Sensor": {
                "name": "Sensor",
                "type": "application",
                "description": "Reads the wheel.",
                "memory": {"RAM0": 128},
                "satisfies": ["REQ_A@1"],
                "runnables": [
                    {
                        "name": "Read",
                        "symbol": "Sensor_Read",
                        "min_start_interval": 0.005,
                        "wcet": "400us",
                        "wcet_type": "gross",
                        "description": "One sample.",
                        "events": [
                            {"type": "timing", "period": 0.01, "disabled_modes": [DISABLED]},
                            {"type": "data", "port": "rIn", "element": "value", "name": "OnValue"},
                        ],
                    },
                    {"name": "Init", "events": [{"type": "init"}]},
                ],
            },
            "Actuator": {
                "name": "Actuator",
                "type": "ecu_abstraction",
                "asil": "B",
                "runnables": [{"name": "Step", "concurrent": True, "events": [{"type": "timing", "period": 0.02}]}],
            },
            "Logger": {"name": "Logger", "type": "service", "runnables": [{"name": "Log"}]},
            "Spare": {"name": "Spare", "runnables": [{"name": "Idle"}]},
            "Stub": {"name": "Stub", "external": True},
        },
        "compositions": {
            "Root": {
                "name": "Root",
                "title": "Root",
                "root": True,
                "members": [
                    {"name": "s1", "type": "Sensor"},
                    {"name": "s2", "type": "Sensor"},
                    {"name": "sub", "composition": "Sub"},
                ],
            },
            "Sub": {"name": "Sub", "title": "Sub", "members": [{"name": "act", "type": "Actuator"}]},
        },
        "tasks": {
            "T10": {
                "name": "T10",
                "period": 0.01,
                "priority": 5,
                "partition": "P0",
                "runnables": [
                    {"instance": "s1", "runnable": "Read"},
                    {"instance": "s1", "runnable": "Init"},
                    {"swc": "Logger", "runnable": "Log"},
                ],
            },
            "T20": {
                "name": "T20",
                "period": 0.02,
                "priority": 3,
                "partition": "P1",
                "runnables": [
                    {"instance": "sub/act", "runnable": "Step"},
                    {"instance": "s2", "runnable": "Read"},
                ],
            },
        },
        "hardware": {
            "CORE0": {"name": "CORE0", "kind": "core", "threshold": 0.7},
            "CORE1": {"name": "CORE1", "kind": "core", "threshold": 0.7},
            "P0": {"name": "P0", "kind": "partition", "core": "CORE0"},
            "P1": {"name": "P1", "kind": "partition", "core": "CORE1"},
        },
        "requirements": {
            "REQ_A": {
                "id": "REQ_A",
                "title": "A",
                "description": "A.",
                "version": 1,
                "source": "architecture",
                "asil": "C",
            },
        },
    }


def _dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8", newline="\n")


def _write(base: Path, model: dict[str, Any], *, groups: dict[str, str] | None = None) -> str:
    root = make_empty_model_root(base)
    for name, payload in model["swc"].items():
        _dump(element_path(root, "swc", name, group=(groups or {}).get(name, "g")), payload)
    for layer in ("compositions", "tasks", "hardware", "requirements"):
        for name, payload in model[layer].items():
            _dump(model_element_path(root, name, layer=layer), payload)
    return str(root)


def _build(
    base: Path, model: dict[str, Any] | None = None, *, annotations: Annotations | None = None, **kwargs: Any
) -> TestTables:
    return build_test_tables(_write(base, model or _model(), **kwargs), "p", annotations)


def _facts(table: Table) -> dict[str, dict[str, str]]:
    """Each row by id, without the hash and the five columns the build leaves to the review."""
    left_out = {"facts_hash", *TEST_COLUMNS, "review_state"}
    return {row["row_id"]: {k: v for k, v in row.items() if k not in left_out} for row in table.rows}


def _hashes(tables: TestTables, name: str) -> dict[str, str]:
    return {row["row_id"]: row["facts_hash"] for row in tables.table(name).rows}


SWC = "swc/g/{}.yaml".format

# ---------------------------------------------------------------------------
# The rows, by hand
# ---------------------------------------------------------------------------


def test_the_components_rows(tmp_path: Path) -> None:
    table = _build(tmp_path).table(COMPONENTS)
    assert [row["row_id"] for row in table.rows] == [
        "component:Actuator",
        "component:Logger",
        "component:Sensor",
        "component:Spare",
        "component:Stub",
    ]
    common = {"external": "false", "asil_declared": "", "memory": "{}", "satisfies": "[]", "description": ""}
    assert _facts(table) == {
        "component:Actuator": {
            **common,
            "row_id": "component:Actuator",
            "component": "Actuator",
            "kind": "ecu_abstraction",
            "asil_declared": "B",
            "asil_effective": "B",
            "occurrences": "1",
            "runnables": "1",
            "source_ref": SWC("Actuator"),
        },
        "component:Logger": {
            **common,
            "row_id": "component:Logger",
            "component": "Logger",
            "kind": "service",
            "asil_effective": "QM",
            "occurrences": "0",
            "runnables": "1",
            "source_ref": SWC("Logger"),
        },
        "component:Sensor": {
            **common,
            "row_id": "component:Sensor",
            "component": "Sensor",
            "kind": "application",
            "asil_effective": "C",
            "memory": '{"RAM0":128}',
            "satisfies": '["REQ_A@1"]',
            "occurrences": "2",
            "runnables": "2",
            "description": "Reads the wheel.",
            "source_ref": SWC("Sensor"),
        },
        "component:Spare": {
            **common,
            "row_id": "component:Spare",
            "component": "Spare",
            "kind": "application",
            "asil_effective": "QM",
            "occurrences": "0",
            "runnables": "1",
            "source_ref": SWC("Spare"),
        },
        "component:Stub": {
            **common,
            "row_id": "component:Stub",
            "component": "Stub",
            "kind": "application",
            "external": "true",
            "asil_effective": "QM",
            "occurrences": "0",
            "runnables": "0",
            "source_ref": SWC("Stub"),
        },
    }


def test_the_placement_rows(tmp_path: Path) -> None:
    table = _build(tmp_path).table(PLACEMENT)
    placed = {"placement": "in-composition"}
    unplaced = {"occurrence": "", "placement": "not-in-composition"}
    nothing = {"tasks": "[]", "partitions": "[]", "cores": "[]"}
    assert [row["row_id"] for row in table.rows] == [
        "occurrence:s1",
        "occurrence:s2",
        "occurrence:sub/act",
        "unplaced:Logger",
        "unplaced:Spare",
        "unplaced:Stub",
    ]
    assert _facts(table) == {
        "occurrence:s1": {
            **placed,
            "row_id": "occurrence:s1",
            "occurrence": "s1",
            "component": "Sensor",
            "tasks": '["T10"]',
            "partitions": '["P0"]',
            "cores": '["CORE0"]',
            "mapping": "all-mapped",
            "source_ref": SWC("Sensor"),
        },
        "occurrence:s2": {
            **placed,
            "row_id": "occurrence:s2",
            "occurrence": "s2",
            "component": "Sensor",
            "tasks": '["T20"]',
            "partitions": '["P1"]',
            "cores": '["CORE1"]',
            "mapping": "partly-mapped",
            "source_ref": SWC("Sensor"),
        },
        "occurrence:sub/act": {
            **placed,
            "row_id": "occurrence:sub/act",
            "occurrence": "sub/act",
            "component": "Actuator",
            "tasks": '["T20"]',
            "partitions": '["P1"]',
            "cores": '["CORE1"]',
            "mapping": "all-mapped",
            "source_ref": SWC("Actuator"),
        },
        "unplaced:Logger": {
            **unplaced,
            "row_id": "unplaced:Logger",
            "component": "Logger",
            "tasks": '["T10"]',
            "partitions": '["P0"]',
            "cores": '["CORE0"]',
            "mapping": "all-mapped",
            "source_ref": SWC("Logger"),
        },
        "unplaced:Spare": {
            **unplaced,
            **nothing,
            "row_id": "unplaced:Spare",
            "component": "Spare",
            "mapping": "not-mapped",
            "source_ref": SWC("Spare"),
        },
        "unplaced:Stub": {
            **unplaced,
            **nothing,
            "row_id": "unplaced:Stub",
            "component": "Stub",
            "mapping": "no-runnables",
            "source_ref": SWC("Stub"),
        },
    }


def test_the_scheduling_rows(tmp_path: Path) -> None:
    table = _build(tmp_path).table(SCHEDULING)
    unmapped = {
        "mapping": "not-mapped",
        "task": "",
        "task_position": "",
        "task_period_s": "",
        "task_priority": "",
        "partition": "",
        "core": "",
    }
    plain = {"symbol": "", "min_start_interval_s": "0.0", "concurrent": "false", "wcet": "", "wcet_type": ""}
    sensor_read = {
        "symbol": "Sensor_Read",
        "events": (
            '[{"disabled_modes":[{"mode_group_ref":"/Modes/Ecu","mode_ref":"/Modes/Ecu/Off","port":"rMode"}],'
            '"period_s":0.01,"type":"timing"},{"disabled_modes":[],"element":"value","name":"OnValue","port":"rIn","type":"data"}]'
        ),
        "min_start_interval_s": "0.005",
        "concurrent": "false",
        "wcet": "400us",
        "wcet_type": "gross",
        "description": "One sample.",
        "source_ref": SWC("Sensor"),
    }
    sensor_init = {**plain, "events": '[{"type":"init"}]', "description": "", "source_ref": SWC("Sensor")}
    assert [row["row_id"] for row in table.rows] == [
        "occurrence:s1:Init",
        "occurrence:s1:Read",
        "occurrence:s2:Init",
        "occurrence:s2:Read",
        "occurrence:sub/act:Step",
        "unplaced:Logger:Log",
        "unplaced:Spare:Idle",
    ]
    assert _facts(table) == {
        "occurrence:s1:Init": {
            **sensor_init,
            "row_id": "occurrence:s1:Init",
            "occurrence": "s1",
            "component": "Sensor",
            "runnable": "Init",
            "mapping": "mapped",
            "task": "T10",
            "task_position": "2",
            "task_period_s": "0.01",
            "task_priority": "5",
            "partition": "P0",
            "core": "CORE0",
        },
        "occurrence:s1:Read": {
            **sensor_read,
            "row_id": "occurrence:s1:Read",
            "occurrence": "s1",
            "component": "Sensor",
            "runnable": "Read",
            "mapping": "mapped",
            "task": "T10",
            "task_position": "1",
            "task_period_s": "0.01",
            "task_priority": "5",
            "partition": "P0",
            "core": "CORE0",
        },
        "occurrence:s2:Init": {
            **sensor_init,
            **unmapped,
            "row_id": "occurrence:s2:Init",
            "occurrence": "s2",
            "component": "Sensor",
            "runnable": "Init",
        },
        "occurrence:s2:Read": {
            **sensor_read,
            "row_id": "occurrence:s2:Read",
            "occurrence": "s2",
            "component": "Sensor",
            "runnable": "Read",
            "mapping": "mapped",
            "task": "T20",
            "task_position": "2",
            "task_period_s": "0.02",
            "task_priority": "3",
            "partition": "P1",
            "core": "CORE1",
        },
        "occurrence:sub/act:Step": {
            **plain,
            "row_id": "occurrence:sub/act:Step",
            "occurrence": "sub/act",
            "component": "Actuator",
            "runnable": "Step",
            "mapping": "mapped",
            "task": "T20",
            "task_position": "1",
            "task_period_s": "0.02",
            "task_priority": "3",
            "partition": "P1",
            "core": "CORE1",
            "events": '[{"disabled_modes":[],"period_s":0.02,"type":"timing"}]',
            "concurrent": "true",
            "description": "",
            "source_ref": SWC("Actuator"),
        },
        "unplaced:Logger:Log": {
            **plain,
            "row_id": "unplaced:Logger:Log",
            "occurrence": "",
            "component": "Logger",
            "runnable": "Log",
            "mapping": "mapped",
            "task": "T10",
            "task_position": "3",
            "task_period_s": "0.01",
            "task_priority": "5",
            "partition": "P0",
            "core": "CORE0",
            "events": "[]",
            "description": "",
            "source_ref": SWC("Logger"),
        },
        "unplaced:Spare:Idle": {
            **plain,
            **unmapped,
            "row_id": "unplaced:Spare:Idle",
            "occurrence": "",
            "component": "Spare",
            "runnable": "Idle",
            "events": "[]",
            "description": "",
            "source_ref": SWC("Spare"),
        },
    }


def test_every_table_carries_its_columns_in_order_with_the_review_left_open(tmp_path: Path) -> None:
    tables = _build(tmp_path)
    assert [table.name for table in tables.tables] == [COMPONENTS, PLACEMENT, SCHEDULING]
    assert [table.file for table in tables.tables] == [
        "components.csv",
        "component-placement.csv",
        "runnable-scheduling.csv",
    ]
    review = ("verification_method", "comment", "accepted", "accepted_facts_hash", "review_state")
    assert tables.table(PLACEMENT).columns == (
        "row_id",
        "occurrence",
        "component",
        "placement",
        "tasks",
        "partitions",
        "cores",
        "mapping",
        "source_ref",
        "facts_hash",
        *review,
    )
    assert tables.table(PLACEMENT).fact_columns == (
        "occurrence",
        "component",
        "placement",
        "tasks",
        "partitions",
        "cores",
        "mapping",
    )
    for table in tables.tables:
        assert table.revision == 1
        for row in table.rows:
            assert tuple(row) == table.columns
            assert [row[column] for column in TEST_COLUMNS] == ["", "", "", ""]
            assert row["review_state"] == "not-reviewed"


def test_the_order_of_a_yaml_list_moves_no_row(tmp_path: Path) -> None:
    base = _build(tmp_path / "a")
    model = _model()
    model["swc"]["Sensor"]["runnables"].reverse()
    reordered = _build(tmp_path / "b", model)
    for name in (COMPONENTS, PLACEMENT, SCHEDULING):
        assert base.table(name).rows == reordered.table(name).rows
    assert facts_digest(base.tables) == facts_digest(reordered.tables)


# ---------------------------------------------------------------------------
# What moves the hash, and what moves the id
# ---------------------------------------------------------------------------


def test_the_hash_ignores_the_declaring_file(tmp_path: Path) -> None:
    base = _build(tmp_path / "a")
    moved = _build(tmp_path / "b", groups={"Sensor": "sensors"})
    assert moved.table(COMPONENTS).rows[2]["source_ref"] == "swc/sensors/Sensor.yaml"
    for name in (COMPONENTS, PLACEMENT, SCHEDULING):
        assert _hashes(base, name) == _hashes(moved, name)


def test_the_hash_moves_with_a_description(tmp_path: Path) -> None:
    base = _build(tmp_path / "a")
    model = _model()
    model["swc"]["Sensor"]["runnables"][1]["description"] = "Starts."
    changed = _build(tmp_path / "b", model)
    before, after = _hashes(base, SCHEDULING), _hashes(changed, SCHEDULING)
    assert before.keys() == after.keys()
    assert {row for row in before if before[row] != after[row]} == {"occurrence:s1:Init", "occurrence:s2:Init"}


def test_a_changed_period_keeps_the_ids_and_moves_the_hashes(tmp_path: Path) -> None:
    base = _build(tmp_path / "a")
    model = _model()
    model["tasks"]["T20"]["period"] = 0.025
    changed = _build(tmp_path / "b", model)
    before, after = _hashes(base, SCHEDULING), _hashes(changed, SCHEDULING)
    assert before.keys() == after.keys()
    assert {row for row in before if before[row] != after[row]} == {"occurrence:s2:Read", "occurrence:sub/act:Step"}
    assert changed.table(SCHEDULING).rows[4]["task_period_s"] == "0.025"
    # The placement states no period: its rows do not move.
    assert _hashes(base, PLACEMENT) == _hashes(changed, PLACEMENT)


def test_a_runnable_moved_to_another_task_keeps_its_id_and_moves_its_hash(tmp_path: Path) -> None:
    base = _build(tmp_path / "a")
    model = _model()
    log = model["tasks"]["T10"]["runnables"].pop()
    model["tasks"]["T20"]["runnables"].append(log)
    changed = _build(tmp_path / "b", model)
    before, after = _hashes(base, SCHEDULING), _hashes(changed, SCHEDULING)
    assert before.keys() == after.keys()
    assert {row for row in before if before[row] != after[row]} == {"unplaced:Logger:Log"}
    row = next(row for row in changed.table(SCHEDULING).rows if row["row_id"] == "unplaced:Logger:Log")
    assert (row["task"], row["task_position"], row["partition"], row["core"]) == ("T20", "3", "P1", "CORE1")
    placement_before, placement_after = _hashes(base, PLACEMENT), _hashes(changed, PLACEMENT)
    assert placement_before.keys() == placement_after.keys()
    assert {row for row in placement_before if placement_before[row] != placement_after[row]} == {"unplaced:Logger"}


def test_placing_an_unplaced_type_changes_its_ids(tmp_path: Path) -> None:
    model = _model()
    model["compositions"]["Root"]["members"].append({"name": "sp", "type": "Spare"})
    tables = _build(tmp_path, model)
    placement = _hashes(tables, PLACEMENT)
    scheduling = _hashes(tables, SCHEDULING)
    assert "unplaced:Spare" not in placement
    assert "occurrence:sp" in placement
    assert "unplaced:Spare:Idle" not in scheduling
    assert "occurrence:sp:Idle" in scheduling


def test_a_type_renamed_under_an_unchanged_member_keeps_its_placed_ids_and_moves_their_hashes(tmp_path: Path) -> None:
    base = _build(tmp_path / "a")
    model = _model()
    motor = model["swc"].pop("Actuator")
    model["swc"]["Motor"] = {**motor, "name": "Motor"}
    model["compositions"]["Sub"]["members"][0]["type"] = "Motor"
    renamed = _build(tmp_path / "b", model)
    for name, row_id in ((PLACEMENT, "occurrence:sub/act"), (SCHEDULING, "occurrence:sub/act:Step")):
        before, after = _hashes(base, name), _hashes(renamed, name)
        assert before.keys() == after.keys()
        assert {row for row in before if before[row] != after[row]} == {row_id}
    components = _hashes(renamed, COMPONENTS)
    assert "component:Actuator" not in components
    assert "component:Motor" in components


def test_an_undefined_walk_places_nothing(tmp_path: Path) -> None:
    model = _model()
    model["compositions"]["Sub"]["root"] = True
    tables = _build(tmp_path, model)
    assert [row["row_id"] for row in tables.table(PLACEMENT).rows] == [
        f"unplaced:{name}" for name in ("Actuator", "Logger", "Sensor", "Spare", "Stub")
    ]


def test_more_occurrences_than_the_walk_keeps_paths_for_are_all_rows(tmp_path: Path) -> None:
    model = _model()
    model["compositions"]["Root"]["members"] += [{"name": f"x{index:02d}", "type": "Stub"} for index in range(70)]
    rows = [row for row in _build(tmp_path, model).table(PLACEMENT).rows if row["component"] == "Stub"]
    assert [row["row_id"] for row in rows] == [f"occurrence:x{index:02d}" for index in range(70)]


def test_a_swc_entry_naming_a_type_with_one_occurrence_maps_that_occurrence(tmp_path: Path) -> None:
    model = _model()
    model["tasks"]["T20"]["runnables"][0] = {"swc": "Actuator", "runnable": "Step"}
    tables = _build(tmp_path, model)
    step = _row(tables, SCHEDULING, STEP)
    assert (step["mapping"], step["task"], step["task_position"], step["partition"], step["core"]) == (
        "mapped",
        "T20",
        "1",
        "P1",
        "CORE1",
    )
    assert _row(tables, PLACEMENT, "occurrence:sub/act")["mapping"] == "all-mapped"
    ids = [row["row_id"] for table in tables.tables for row in table.rows]
    assert not [row_id for row_id in ids if row_id.startswith("unplaced:Actuator")]


def test_an_operation_event_and_a_task_on_a_partition_with_no_core(tmp_path: Path) -> None:
    model = _model()
    model["swc"]["Spare"]["runnables"][0]["events"] = [{"type": "operation", "port": "pSrv", "operation": "Get"}]
    model["tasks"]["T30"] = {
        "name": "T30",
        "period": 0.1,
        "priority": 1,
        "partition": "P9",
        "runnables": [{"swc": "Spare", "runnable": "Idle"}],
    }
    tables = _build(tmp_path, model)
    idle = _row(tables, SCHEDULING, IDLE)
    assert (idle["events"], idle["task"], idle["partition"], idle["core"], idle["task_period_s"]) == (
        '[{"operation":"Get","port":"pSrv","type":"operation"}]',
        "T30",
        "P9",
        "",
        "0.1",
    )
    spare = _row(tables, PLACEMENT, "unplaced:Spare")
    assert (spare["tasks"], spare["partitions"], spare["cores"], spare["mapping"]) == (
        '["T30"]',
        '["P9"]',
        "[]",
        "all-mapped",
    )


def test_a_call_returned_event_holds_its_pair_and_a_stated_result(tmp_path: Path) -> None:
    """A `call_returned` event holds its port, operation and result's stated name; an unstated one is left out."""
    model = _model()
    model["swc"]["Spare"]["runnables"][0]["events"] = [
        {"type": "call_returned", "port": "rSrv", "operation": "Get"},
        {"type": "call_returned", "port": "rSrv", "operation": "Put", "result": "R_Put", "name": "OnPut"},
    ]
    idle = _row(_build(tmp_path, model), SCHEDULING, IDLE)
    assert idle["events"] == (
        '[{"operation":"Get","port":"rSrv","type":"call_returned"},'
        '{"name":"OnPut","operation":"Put","port":"rSrv","result":"R_Put","type":"call_returned"}]'
    )


def test_a_task_entry_the_resolver_refuses_maps_nothing(tmp_path: Path) -> None:
    model = _model()
    model["tasks"]["T20"]["runnables"].append({"instance": "nowhere", "runnable": "Init"})
    model["tasks"]["T20"]["runnables"].append({"swc": "Sensor", "runnable": "Init"})  # two occurrences: ambiguous
    tables = _build(tmp_path, model)
    assert _row(tables, SCHEDULING, "occurrence:s2:Init")["mapping"] == "not-mapped"
    assert _row(tables, SCHEDULING, "occurrence:s1:Init")["task"] == "T10"
    assert [row["row_id"] for row in tables.table(SCHEDULING).rows] == [
        row["row_id"] for row in _build(tmp_path / "base").table(SCHEDULING).rows
    ]


def test_a_runnable_named_by_two_tasks_keeps_the_first_in_task_listing_order(tmp_path: Path) -> None:
    model = _model()
    model["tasks"]["T05"] = {
        "name": "T05",
        "period": 0.005,
        "priority": 9,
        "partition": "P1",
        "runnables": [{"instance": "s1", "runnable": "Init"}, {"instance": "s1", "runnable": "Read"}],
    }
    tables = _build(tmp_path, model)
    read = _row(tables, SCHEDULING, "occurrence:s1:Read")
    assert (read["task"], read["task_position"], read["core"]) == ("T05", "2", "CORE1")
    assert _row(tables, PLACEMENT, "occurrence:s1")["tasks"] == '["T05"]'


# ---------------------------------------------------------------------------
# The hash and the digest against hand-computed literals
# ---------------------------------------------------------------------------

#: sha256 of '{"facts":{"component":"Stub","tasks":"[]"},"revision":1,"row_id":"unplaced:Stub",
#: "table":"component-placement"}', first 16 digits
LITERAL_FACTS = {"component": "Stub", "tasks": "[]"}
LITERAL_HASH = "40588b8d61239c69"


def test_the_facts_hash_against_a_literal() -> None:
    assert facts_hash(PLACEMENT, 1, "unplaced:Stub", LITERAL_FACTS) == LITERAL_HASH


def test_a_renamed_column_or_a_raised_revision_moves_the_hash() -> None:
    renamed = {"component": "Stub", "task_list": "[]"}
    assert facts_hash(PLACEMENT, 1, "unplaced:Stub", renamed) != LITERAL_HASH
    assert facts_hash(PLACEMENT, 2, "unplaced:Stub", LITERAL_FACTS) != LITERAL_HASH


def test_a_real_rows_hash_against_a_literal(tmp_path: Path) -> None:
    # sha256 of '{"facts":{"component":"Stub","cores":"[]","mapping":"no-runnables","occurrence":"",
    # "partitions":"[]","placement":"not-in-composition","tasks":"[]"},"revision":1,
    # "row_id":"unplaced:Stub","table":"component-placement"}', first 16 digits
    stub = _build(tmp_path).table(PLACEMENT).rows[-1]
    assert stub["row_id"] == "unplaced:Stub"
    assert stub["facts_hash"] == "91d5ecabafa75590"


def test_the_facts_digest_against_a_literal() -> None:
    def row(row_id: str, digest: str) -> dict[str, str]:
        return {"row_id": row_id, "facts_hash": digest}

    tables = [
        Table(name=COMPONENTS, revision=1, rows=(row("component:A", "0123456789abcdef"),)),
        Table(name=PLACEMENT, revision=1, rows=()),
        Table(
            name=SCHEDULING,
            revision=2,
            rows=(row("unplaced:A:r", "fedcba9876543210"), row("unplaced:A:s", "00000000ffffffff")),
        ),
    ]
    # sha256 of '[["components",1,[["component:A","0123456789abcdef"]]],["component-placement",1,[]],
    # ["runnable-scheduling",2,[["unplaced:A:r","fedcba9876543210"],["unplaced:A:s","00000000ffffffff"]]]]'
    assert facts_digest(tables) == "648c1f7839ad7b630d4cf86f22ff0ab4fb996cdb43c3a09988a2d30ad353b67c"


def test_two_builds_of_one_model_agree(tmp_path: Path) -> None:
    model = _model()
    first = _build(tmp_path / "a", copy.deepcopy(model))
    second = _build(tmp_path / "b", model)
    assert first == second
    assert facts_digest(first.tables) == facts_digest(second.tables)


@pytest.mark.parametrize("name", [COMPONENTS, PLACEMENT, SCHEDULING])
def test_every_row_id_is_unique(tmp_path: Path, name: str) -> None:
    ids = [row["row_id"] for row in _build(tmp_path).table(name).rows]
    assert len(ids) == len(set(ids))


#: The calls that read a file, by the name or attribute called: the builtin, ``io``'s and ``os``'s
#: openers, ``Path``'s readers and a file object's ``read``.
_FILE_READS = frozenset({"open", "fdopen", "read", "readlines", "read_text", "read_bytes"})


def test_the_module_reads_no_file_by_itself() -> None:
    """The model is read through the readers and the kernel only; the one file read is the annotations reader's.

    An AST scan of the module: every call whose callee's name or attribute is a
    file read (:data:`_FILE_READS`), and every import of ``io``, ``os`` or
    ``builtins`` that names one of them under any alias. What it does not reach:
    a read through ``getattr``, a name bound by assignment, or a helper of
    another module that reads on the module's behalf (the readers, the kernel and
    ``write_report_files`` are such helpers, by design).
    """
    tree = ast.parse(Path(tables_for_testing.__file__).read_text(encoding="utf-8"))
    called = [
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
    ]
    assert [name for name in called if name in _FILE_READS] == ["read_bytes"]
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module in ("io", "os", "builtins")
        for alias in node.names
    ]
    assert imported == []


# ---------------------------------------------------------------------------
# The test side's directory: helpers
# ---------------------------------------------------------------------------

Edits = dict[tuple[str, str], dict[str, str]]


def _put(directory: Path, name: str, text: str) -> None:
    """*text* written as UTF-8 bytes, no newline translation — the bytes a test means."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(text.encode("utf-8"))


def _hand_back(directory: Path, tables: TestTables, edits: Edits | None = None) -> None:
    """The generated tables and orphan file, as the test side hands them back, with *edits* applied."""
    edits = edits or {}
    for table in tables.tables:
        rows = [{**row, **edits.get((table.name, row["row_id"]), {})} for row in table.rows]
        _put(directory, table.file, table_csv(Table(name=table.name, revision=table.revision, rows=tuple(rows))))
    _put(directory, f"{ORPHANS}.csv", orphans_csv(tables.orphans))


def _bytes_of(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir(), key=lambda path: path.as_posix())}


def _row(tables: TestTables, name: str, row_id: str) -> Mapping[str, str]:
    return next(row for row in tables.table(name).rows if row["row_id"] == row_id)


def _review(tables: TestTables, name: str, row_id: str) -> tuple[str, ...]:
    """A row's four test-owned cells and its review state."""
    row = _row(tables, name, row_id)
    return (*(row[column] for column in TEST_COLUMNS), row["review_state"])


def _refused(directory: Path) -> str:
    with pytest.raises(AnnotationsError) as caught:
        read_annotations(directory)
    return str(caught.value)


IDLE = "unplaced:Spare:Idle"
STEP = "occurrence:sub/act:Step"

# ---------------------------------------------------------------------------
# The reader: refusals
# ---------------------------------------------------------------------------


def test_a_missing_directory_is_refused(tmp_path: Path) -> None:
    assert "does not exist" in _refused(tmp_path / "absent")


def test_a_table_file_without_a_row_id_column_is_refused(tmp_path: Path) -> None:
    _put(tmp_path, "components.csv", "id,comment\ncomponent:A,x\n")
    message = _refused(tmp_path)
    assert message == f"{tmp_path / 'components.csv'}: line 1: the header has no 'row_id' column"


def test_the_orphan_file_needs_a_table_column(tmp_path: Path) -> None:
    _put(tmp_path, "orphaned-annotations.csv", "row_id,comment\nx,y\n")
    assert "the header has no 'table' column" in _refused(tmp_path)


def test_a_header_naming_a_column_twice_is_refused(tmp_path: Path) -> None:
    _put(tmp_path, "components.csv", "row_id,comment,comment\ncomponent:A,x,y\n")
    message = _refused(tmp_path)
    assert message == (
        f"{tmp_path / 'components.csv'}: line 1, column 3: the header names the column 'comment' twice"
        " (first in column 2)"
    )


def test_a_row_without_a_row_id_and_another_cell_filled_is_refused(tmp_path: Path) -> None:
    _put(tmp_path, "components.csv", "comment,row_id\nx,component:A\nlost,\n")
    assert _refused(tmp_path) == f"{tmp_path / 'components.csv'}: line 3, column 2: the row has no row_id"


def test_a_repeated_row_id_is_refused(tmp_path: Path) -> None:
    _put(tmp_path, "components.csv", 'row_id,comment\ncomponent:A,x\n"component:B","multi\nline"\ncomponent:A,z\n')
    # The third record starts on line 5: the second spans two lines.
    assert _refused(tmp_path) == (
        f"{tmp_path / 'components.csv'}: line 5, column 1: the row_id 'component:A' is repeated (first on line 2)"
    )


@pytest.mark.parametrize("value", ["TRUE", "x", "accepted", "y"])
def test_an_accepted_value_outside_yes_no_empty_is_refused(tmp_path: Path, value: str) -> None:
    _put(tmp_path, "components.csv", f"row_id,accepted\ncomponent:A,{value}\n")
    assert _refused(tmp_path) == (
        f"{tmp_path / 'components.csv'}: line 2, column 2: accepted is '{value}'; write yes, no or leave it empty"
    )


def test_a_file_that_is_not_utf8_is_refused_at_its_line_and_column(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "runnable-scheduling.csv").write_bytes(b"row_id,comment\nunplaced:A:r,caf\xe9\n")
    assert (
        _refused(tmp_path) == f"{tmp_path / 'runnable-scheduling.csv'}: line 2, column 17: the file is not UTF-8 text"
    )


def test_a_byte_order_mark_does_not_shift_the_line_and_column_of_a_non_utf8_byte(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "runnable-scheduling.csv").write_bytes(b"\xef\xbb\xbfrow_id,comment\nunplaced:A:r,caf\xe9\n")
    assert (
        _refused(tmp_path) == f"{tmp_path / 'runnable-scheduling.csv'}: line 2, column 17: the file is not UTF-8 text"
    )


def test_an_orphan_naming_no_table_is_refused(tmp_path: Path) -> None:
    _put(tmp_path, "orphaned-annotations.csv", "table,row_id,comment\nsr-elements,x,y\n")
    assert "line 2, column 1: 'sr-elements' names no table" in _refused(tmp_path)


def test_one_row_in_a_table_file_and_in_the_orphan_file_is_refused_naming_both(tmp_path: Path) -> None:
    _put(tmp_path, "components.csv", "row_id,comment\ncomponent:A,x\n")
    _put(tmp_path, "orphaned-annotations.csv", "table,row_id,comment\ncomponents,component:A,y\n")
    message = _refused(tmp_path)
    assert str(tmp_path / "components.csv") in message
    assert str(tmp_path / "orphaned-annotations.csv") in message
    assert "is annotated twice" in message


# ---------------------------------------------------------------------------
# The reader: leniencies
# ---------------------------------------------------------------------------


def test_a_bom_crlf_records_spaces_and_trailing_empty_rows_are_read(tmp_path: Path) -> None:
    text = "﻿row_id , accepted,comment\r\n component:A ,  Yes ,ok\r\ncomponent:B,NO,\r\n,,\r\n , ,\r\n"
    _put(tmp_path, "components.csv", text)
    annotations = read_annotations(tmp_path)
    assert sorted(annotations.entries) == [(COMPONENTS, "component:A"), (COMPONENTS, "component:B")]
    first = annotations.entries[(COMPONENTS, "component:A")]
    assert (first.accepted, first.comment, first.pin, first.line) == ("yes", "ok", None, 2)
    assert annotations.entries[(COMPONENTS, "component:B")].accepted == "no"


def test_the_row_id_is_compared_exactly_otherwise(tmp_path: Path) -> None:
    _put(tmp_path, "runnable-scheduling.csv", f"row_id,comment\n{IDLE.upper()},x\n")
    tables = _build(tmp_path / "m", annotations=read_annotations(tmp_path))
    assert [orphan["row_id"] for orphan in tables.orphans] == [IDLE.upper()]


def test_unknown_files_are_listed_and_not_read(tmp_path: Path) -> None:
    _put(tmp_path, "notes.txt", "\xff not even csv")
    _put(tmp_path, "readme.json", "{}")
    _put(tmp_path, "components.csv", "row_id\n")
    annotations = read_annotations(tmp_path)
    assert annotations.unknown_files == ("notes.txt", "readme.json")
    assert annotations.entries == {}


def test_a_handed_back_manifest_is_expected_and_not_read(tmp_path: Path) -> None:
    _put(tmp_path, "manifest.json", "\xff not even json")
    _put(tmp_path, "components.csv", "row_id\n")
    annotations = read_annotations(tmp_path)
    assert annotations.unknown_files == ()
    assert annotations.entries == {}


def test_a_row_whose_four_test_cells_are_empty_is_no_annotation(tmp_path: Path) -> None:
    _put(
        tmp_path,
        "components.csv",
        "row_id,component,verification_method,comment,accepted,accepted_facts_hash,facts_hash\n"
        "component:A,A,, , ,,0123456789abcdef\n"
        "component:B,B,,seen,,,0123456789abcdef\n",
    )
    _put(tmp_path, "orphaned-annotations.csv", "table,row_id,comment\ncomponents,component:C,\n")
    assert sorted(read_annotations(tmp_path).entries) == [(COMPONENTS, "component:B")]


def test_an_empty_row_repeating_a_row_id_conflicts_with_nothing(tmp_path: Path) -> None:
    _put(tmp_path, "components.csv", "row_id,comment\ncomponent:A,x\ncomponent:A,\n")
    _put(tmp_path, "orphaned-annotations.csv", "table,row_id,comment\ncomponents,component:A,\n")
    annotations = read_annotations(tmp_path)
    assert annotations.entries[(COMPONENTS, "component:A")].comment == "x"


def test_a_field_past_the_default_csv_limit_reads(tmp_path: Path) -> None:
    long = "w" * 200_000
    model = _model()
    model["swc"]["Spare"]["runnables"][0]["description"] = long
    tables = _build(tmp_path / "m", model)
    _hand_back(tmp_path / "ann", tables, {(SCHEDULING, IDLE): {"comment": long}})
    before = csv.field_size_limit()
    annotations = read_annotations(tmp_path / "ann")
    assert annotations.entries[(SCHEDULING, IDLE)].comment == long
    assert csv.field_size_limit() == before, "the process-wide limit is put back"


def test_the_csv_limit_is_put_back_after_a_refused_file(tmp_path: Path) -> None:
    _put(tmp_path, "components.csv", 'row_id,comment\ncomponent:A,"open\n')
    before = csv.field_size_limit()
    assert "not a readable CSV record" in _refused(tmp_path)
    assert csv.field_size_limit() == before


def test_line_ends_inside_a_description_and_a_comment_round_trip(tmp_path: Path) -> None:
    endings = {"Init": "a\rb", "Read": "a\nb", "Step": "a\r\nb"}
    model = _model()
    for runnable in model["swc"]["Sensor"]["runnables"]:
        runnable["description"] = endings[runnable["name"]]
    model["swc"]["Actuator"]["runnables"][0]["description"] = endings["Step"]
    tables = _build(tmp_path / "m", model)
    comments = {
        "occurrence:s1:Init": "c\rd",
        "occurrence:s1:Read": "c\nd",
        STEP: "c\r\nd",
    }
    _hand_back(tmp_path / "ann", tables, {(SCHEDULING, row_id): {"comment": text} for row_id, text in comments.items()})
    data = (tmp_path / "ann" / "runnable-scheduling.csv").read_bytes()
    # Every record ends in "\n"; a line end inside a field sits inside its quotes.
    records = list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))
    assert len(records) == 1 + len(tables.table(SCHEDULING).rows)
    read_back = {record[0]: dict(zip(records[0], record, strict=True)) for record in records[1:]}
    for row_id, runnable in (("occurrence:s1:Init", "Init"), ("occurrence:s1:Read", "Read"), (STEP, "Step")):
        assert read_back[row_id]["description"] == endings[runnable]
    annotations = read_annotations(tmp_path / "ann")
    for row_id, text in comments.items():
        assert annotations.entries[(SCHEDULING, row_id)].comment == text


def test_a_slim_file_of_the_test_sides_own_columns_reads(tmp_path: Path) -> None:
    _put(tmp_path / "ann", "runnable-scheduling.csv", f"row_id,verification_method,their_own\n{IDLE},HIL,kept out\n")
    tables = _build(tmp_path / "m", annotations=read_annotations(tmp_path / "ann"))
    assert _review(tables, SCHEDULING, IDLE) == ("HIL", "", "", "", "not-reviewed")


# ---------------------------------------------------------------------------
# The join, the pin and the review state
# ---------------------------------------------------------------------------


def test_a_comment_alone_stays_not_reviewed(tmp_path: Path) -> None:
    tables = _build(tmp_path / "m")
    _hand_back(tmp_path / "ann", tables, {(SCHEDULING, IDLE): {"comment": "looked at it"}})
    joined = _build(tmp_path / "m2", annotations=read_annotations(tmp_path / "ann"))
    assert _review(joined, SCHEDULING, IDLE) == ("", "looked at it", "", "", "not-reviewed")


def test_an_acceptance_of_the_facts_in_view_is_accepted_and_pinned(tmp_path: Path) -> None:
    tables = _build(tmp_path / "m")
    current = _row(tables, SCHEDULING, IDLE)["facts_hash"]
    _hand_back(
        tmp_path / "ann", tables, {(SCHEDULING, IDLE): {"accepted": "Yes"}, (SCHEDULING, STEP): {"accepted": "no"}}
    )
    joined = _build(tmp_path / "m2", annotations=read_annotations(tmp_path / "ann"))
    assert _review(joined, SCHEDULING, IDLE) == ("", "", "yes", current, "accepted")
    step = _row(tables, SCHEDULING, STEP)["facts_hash"]
    assert _review(joined, SCHEDULING, STEP) == ("", "", "no", step, "rejected")


def test_an_acceptance_with_no_pin_is_stale_and_written_none(tmp_path: Path) -> None:
    _put(tmp_path / "ann", "runnable-scheduling.csv", f"row_id,accepted\n{IDLE},yes\n")
    joined = _build(tmp_path / "m", annotations=read_annotations(tmp_path / "ann"))
    assert _review(joined, SCHEDULING, IDLE) == ("", "", "yes", NO_PIN, "stale")


def test_that_stale_file_handed_back_unedited_stays_stale(tmp_path: Path) -> None:
    """A stale row's own current facts_hash never becomes its pin when the file is handed back."""
    _put(tmp_path / "ann", "runnable-scheduling.csv", f"row_id,accepted\n{IDLE},yes\n")
    first = _build(tmp_path / "m", annotations=read_annotations(tmp_path / "ann"))
    _hand_back(tmp_path / "back", first)
    second = _build(tmp_path / "m2", annotations=read_annotations(tmp_path / "back"))
    assert _review(second, SCHEDULING, IDLE) == ("", "", "yes", NO_PIN, "stale")


def test_clearing_the_pin_re_accepts_at_the_current_facts(tmp_path: Path) -> None:
    _put(tmp_path / "ann", "runnable-scheduling.csv", f"row_id,accepted\n{IDLE},yes\n")
    first = _build(tmp_path / "m", annotations=read_annotations(tmp_path / "ann"))
    _hand_back(tmp_path / "back", first, {(SCHEDULING, IDLE): {"accepted_facts_hash": ""}})
    second = _build(tmp_path / "m2", annotations=read_annotations(tmp_path / "back"))
    current = _row(second, SCHEDULING, IDLE)["facts_hash"]
    assert _review(second, SCHEDULING, IDLE) == ("", "", "yes", current, "accepted")


def test_a_stale_row_keeps_accepted_as_written(tmp_path: Path) -> None:
    tables = _build(tmp_path / "m")
    _hand_back(tmp_path / "ann", tables, {(SCHEDULING, STEP): {"accepted": "no", "accepted_facts_hash": "0" * 16}})
    joined = _build(tmp_path / "m2", annotations=read_annotations(tmp_path / "ann"))
    assert _review(joined, SCHEDULING, STEP) == ("", "", "no", "0" * 16, "stale")


def test_spaces_around_a_pin_are_stripped_and_a_cell_of_spaces_is_empty(tmp_path: Path) -> None:
    tables = _build(tmp_path / "m")
    current = _row(tables, SCHEDULING, IDLE)["facts_hash"]
    step = _row(tables, SCHEDULING, STEP)["facts_hash"]
    _hand_back(
        tmp_path / "ann",
        tables,
        {
            (SCHEDULING, IDLE): {"accepted": "yes", "accepted_facts_hash": f"  {current} "},
            (SCHEDULING, STEP): {"accepted": "yes", "accepted_facts_hash": " ", "facts_hash": f" {step}"},
        },
    )
    joined = _build(tmp_path / "m2", annotations=read_annotations(tmp_path / "ann"))
    assert _review(joined, SCHEDULING, IDLE) == ("", "", "yes", current, "accepted")
    assert _review(joined, SCHEDULING, STEP) == ("", "", "yes", step, "accepted")


def test_a_pin_without_an_acceptance_is_ignored_and_written_empty(tmp_path: Path) -> None:
    tables = _build(tmp_path / "m")
    _hand_back(tmp_path / "ann", tables, {(SCHEDULING, STEP): {"accepted_facts_hash": "0" * 16, "comment": "c"}})
    joined = _build(tmp_path / "m2", annotations=read_annotations(tmp_path / "ann"))
    assert _review(joined, SCHEDULING, STEP) == ("", "c", "", "", "not-reviewed")


def test_an_annotation_changes_no_fact_and_no_hash(tmp_path: Path) -> None:
    tables = _build(tmp_path / "m")
    _hand_back(tmp_path / "ann", tables, {(COMPONENTS, "component:Spare"): {"accepted": "yes", "comment": "c"}})
    joined = _build(tmp_path / "m2", annotations=read_annotations(tmp_path / "ann"))
    assert facts_digest(joined.tables) == facts_digest(tables.tables)
    assert _review(joined, COMPONENTS, "component:Spare")[-1] == "accepted"


# ---------------------------------------------------------------------------
# The orphans, and the regeneration story
# ---------------------------------------------------------------------------


def test_an_orphan_survives_every_hand_back_and_rejoins_its_restored_row(tmp_path: Path) -> None:
    model = _model()
    tables = _build(tmp_path / "m0", copy.deepcopy(model))
    accepted_at = _row(tables, SCHEDULING, IDLE)["facts_hash"]
    _hand_back(tmp_path / "h0", tables, {(SCHEDULING, IDLE): {"accepted": "yes", "comment": "seen"}})

    # Remove the row: its annotation is an orphan, pinned to the facts it accepted.
    removed = copy.deepcopy(model)
    removed["swc"]["Spare"]["runnables"] = []
    first = _build(tmp_path / "m1", copy.deepcopy(removed), annotations=read_annotations(tmp_path / "h0"))
    assert IDLE not in _hashes(first, SCHEDULING)
    orphan = {"table": SCHEDULING, "row_id": IDLE, "verification_method": "", "comment": "seen", "accepted": "yes"}
    assert first.orphans == ({**orphan, "accepted_facts_hash": accepted_at},)

    # Hand back the generated bundle, orphan file included: still carried.
    _hand_back(tmp_path / "h1", first)
    second = _build(tmp_path / "m2", copy.deepcopy(removed), annotations=read_annotations(tmp_path / "h1"))
    assert second.orphans == first.orphans

    # Restore the row: its annotation joins it again, at the facts it accepted.
    _hand_back(tmp_path / "h2", second)
    restored = _build(tmp_path / "m3", copy.deepcopy(model), annotations=read_annotations(tmp_path / "h2"))
    assert restored.orphans == ()
    assert _review(restored, SCHEDULING, IDLE) == ("", "seen", "yes", accepted_at, "accepted")


def test_a_whole_bundle_handed_back_orphans_only_the_annotated_rows(tmp_path: Path) -> None:
    """Every generated row comes back; only a removed row someone wrote on is an orphan."""
    model = _model()
    tables = _build(tmp_path / "m0", copy.deepcopy(model))
    _hand_back(tmp_path / "h0", tables)
    removed = copy.deepcopy(model)
    removed["swc"]["Spare"]["runnables"] = []  # IDLE gone, nobody annotated it
    assert _build(tmp_path / "m1", copy.deepcopy(removed), annotations=read_annotations(tmp_path / "h0")).orphans == ()

    _hand_back(tmp_path / "h1", tables, {(SCHEDULING, "unplaced:Logger:Log"): {"comment": "check"}})
    removed["swc"]["Logger"]["runnables"] = []
    removed["tasks"]["T10"]["runnables"].pop()  # Log gone too, and it was annotated
    orphans = _build(tmp_path / "m2", removed, annotations=read_annotations(tmp_path / "h1")).orphans
    assert [(orphan["row_id"], orphan["comment"]) for orphan in orphans] == [("unplaced:Logger:Log", "check")]


def test_an_orphan_with_no_pin_is_carried_with_the_sentinel(tmp_path: Path) -> None:
    _put(tmp_path / "ann", "components.csv", "row_id,accepted\ncomponent:Gone,yes\n")
    tables = _build(tmp_path / "m", annotations=read_annotations(tmp_path / "ann"))
    assert tables.orphans == (
        {
            "table": COMPONENTS,
            "row_id": "component:Gone",
            "verification_method": "",
            "comment": "",
            "accepted": "yes",
            "accepted_facts_hash": NO_PIN,
        },
    )
    assert orphans_csv(tables.orphans) == (
        '"table","row_id","verification_method","comment","accepted","accepted_facts_hash"\n'
        '"components","component:Gone","","","yes","none"\n'
    )


def test_the_orphans_are_ordered_by_table_then_row_id(tmp_path: Path) -> None:
    _put(tmp_path / "ann", "runnable-scheduling.csv", "row_id,comment\nunplaced:Z:r,a\nunplaced:A:r,b\n")
    _put(tmp_path / "ann", "components.csv", "row_id,comment\ncomponent:Z,c\n")
    tables = _build(tmp_path / "m", annotations=read_annotations(tmp_path / "ann"))
    assert [(orphan["table"], orphan["row_id"]) for orphan in tables.orphans] == [
        (COMPONENTS, "component:Z"),
        (SCHEDULING, "unplaced:A:r"),
        (SCHEDULING, "unplaced:Z:r"),
    ]


def test_the_regeneration_story(tmp_path: Path) -> None:
    """The test side's review survives a regeneration, and a stale row is flagged."""
    model = _model()
    tables = _build(tmp_path / "m0", copy.deepcopy(model))
    read_s1 = "occurrence:s1:Read"
    annotations_dir = tmp_path / "test-side"
    _hand_back(
        annotations_dir,
        tables,
        {
            (SCHEDULING, "unplaced:Logger:Log"): {"accepted": "yes", "verification_method": "HIL"},  # will move task
            (SCHEDULING, read_s1): {"accepted": "yes"},  # untouched
            (SCHEDULING, "occurrence:s2:Read"): {"accepted": "no", "comment": "budget too tight"},  # period changes
            (PLACEMENT, "occurrence:s1"): {"comment": "check on target"},  # commented only
            (SCHEDULING, IDLE): {"accepted": "yes", "comment": "idle loop"},  # will be removed
        },
    )
    before = _bytes_of(annotations_dir)

    changed = copy.deepcopy(model)
    changed["tasks"]["T20"]["runnables"].append(changed["tasks"]["T10"]["runnables"].pop())  # Log -> T20
    changed["tasks"]["T20"]["period"] = 0.025
    changed["swc"]["Spare"]["runnables"] = []
    regenerated = _build(tmp_path / "m1", changed, annotations=read_annotations(annotations_dir))

    log_pin = _row(tables, SCHEDULING, "unplaced:Logger:Log")["facts_hash"]
    assert _review(regenerated, SCHEDULING, "unplaced:Logger:Log") == ("HIL", "", "yes", log_pin, "stale")
    s2_pin = _row(tables, SCHEDULING, "occurrence:s2:Read")["facts_hash"]
    assert _review(regenerated, SCHEDULING, "occurrence:s2:Read") == ("", "budget too tight", "no", s2_pin, "stale")
    s1_pin = _row(tables, SCHEDULING, read_s1)["facts_hash"]
    assert _row(regenerated, SCHEDULING, read_s1)["facts_hash"] == s1_pin
    assert _review(regenerated, SCHEDULING, read_s1) == ("", "", "yes", s1_pin, "accepted")
    assert _review(regenerated, PLACEMENT, "occurrence:s1") == ("", "check on target", "", "", "not-reviewed")
    idle_pin = _row(tables, SCHEDULING, IDLE)["facts_hash"]
    assert regenerated.orphans == (
        {
            "table": SCHEDULING,
            "row_id": IDLE,
            "verification_method": "",
            "comment": "idle loop",
            "accepted": "yes",
            "accepted_facts_hash": idle_pin,
        },
    )
    assert _bytes_of(annotations_dir) == before


# ---------------------------------------------------------------------------
# Where the annotations directory may lie
# ---------------------------------------------------------------------------


def _layout(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    project = tmp_path / "project"
    roots = {"model_root": project / "model", "dist_root": project / "dist", "views_root": project / "views"}
    for root in roots.values():
        root.mkdir(parents=True)
    return roots["dist_root"] / "test-tables", roots


@pytest.mark.parametrize(
    ("relative", "reason"),
    [
        ("project/dist/test-tables", "overlaps the test tables' output directory"),
        ("project/dist/test-tables/ann", "overlaps the test tables' output directory"),
        ("project/dist", "overlaps the test tables' output directory"),
        ("project", "overlaps the test tables' output directory"),
        ("project/dist/other", "lies inside dist_root"),
        ("project/model", "lies inside model_root"),
        ("project/model/notes", "lies inside model_root"),
        ("project/views/ann", "lies inside views_root"),
    ],
)
def test_an_annotations_directory_in_a_refused_place_is_refused(tmp_path: Path, relative: str, reason: str) -> None:
    output, roots = _layout(tmp_path)
    refusal = annotations_location_refusal(tmp_path / relative, output_directory=output, roots=roots)
    assert refusal == f"the annotations directory {reason}"


def test_an_annotations_directory_beside_the_project_is_accepted(tmp_path: Path) -> None:
    output, roots = _layout(tmp_path)
    for relative in ("project/test-side", "elsewhere", "project/dist-test"):
        assert annotations_location_refusal(tmp_path / relative, output_directory=output, roots=roots) is None


@pytest.mark.parametrize(
    ("relative", "reason"),
    [
        ("project/DIST/Test-Tables", "overlaps the test tables' output directory"),
        ("project/Dist/review", "lies inside dist_root"),
        ("project/MODEL/notes", "lies inside model_root"),
        ("project/Views", "lies inside views_root"),
    ],
)
def test_another_case_of_a_refused_place_is_refused(tmp_path: Path, relative: str, reason: str) -> None:
    """Compared case-insensitively on every platform: on APFS and NTFS ``Dist`` is ``dist``."""
    output, roots = _layout(tmp_path)
    refusal = annotations_location_refusal(tmp_path / relative, output_directory=output, roots=roots)
    assert refusal == f"the annotations directory {reason}"


def test_a_link_is_judged_by_where_it_points(tmp_path: Path) -> None:
    output, roots = _layout(tmp_path)
    link = tmp_path / "test-side"
    symlink_or_skip(link, roots["model_root"])
    assert annotations_location_refusal(link, output_directory=output, roots=roots) == (
        "the annotations directory lies inside model_root"
    )
