"""The sender-receiver table as one request, on a synthetic model, through both surfaces.

The model, built in ``tmp_path``, holds every case the request has to get right at once:

- a **nested composition with delegations**: the connector reaches ``Cluster``, whose boundary port is
  delegated into ``Bay`` and from there to two consumers, so ``connection_ends`` walks two levels;
- an **application type mapped to an implementation type** (``Temperature`` to ``TemperatureRaw``
  through ``ThermalMapping``) and a **value type over a base type** (``Pressure`` over ``f32``), so
  ``base_type`` answers through the mapping set on one row and directly on the other;
- an **external stub component** on the requester side of a second connector: its connection reaches
  the join and is dropped there, because no runnable of the stub reads anything;
- **two writers and two readers** of one port, each reader reading a different data element, so the
  join's ``member.element`` equality keeps a write from pairing with the other element's reader.

The request is answered through ``swcraft query --request`` and through ``engine.handle_request`` (the
call the MCP ``read`` tool makes; the tool itself is driven in
``packages/mcp/tests/test_protocol_discover_read.py``); the rows, the echo and the page block must be
equal, and equal to the table written out here.

**What it does not reach.** A model ``check-model`` accepts: the fixture carries no requirement, no
hardware layer and no base type's ``native_declaration``, so the completeness rules (and only they)
report on it. One placement of each component type (a type placed twice would join rows across its
instances); the consumer-side mapping of a row to a team's columns; and any real project.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from conftest import _write_manifest, write_model_element
from swcraft_tools import query
from swcraft_tools.cli import main
from swcraft_tools.editing import engine

#: The request: each write, the connector that carries it, and each read of the same data element at
#: the requester end.
TABLE_REQUEST: dict[str, Any] = {
    "address": {"layer": "swc"},
    "form": "index",
    "members": "runnables.writes",
    "derived": ["base_type"],
    "join": [
        {
            "with": {"address": {"layer": "connectors"}, "derived": ["connection_ends"]},
            "on": [
                {"left": "element", "right": "derived.connection_ends.provider.swc"},
                {"left": "member.port", "right": "derived.connection_ends.provider.port"},
            ],
            "as": "connection",
        },
        {
            "with": {"address": {"layer": "swc"}, "members": "runnables.reads"},
            "on": [
                {"left": "connection.derived.connection_ends.requester.swc", "right": "element"},
                {"left": "connection.derived.connection_ends.requester.port", "right": "member.port"},
                {"left": "member.element", "right": "member.element"},
            ],
            "as": "reader",
        },
    ],
}


def _dump(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")


def _runnable(name: str, accesses: str, port: str, element: str) -> dict[str, Any]:
    return {
        "name": name,
        "min_start_interval": 0.01,
        "events": [{"type": "timing", "period": 0.01}],
        accesses: [{"port": port, "element": element}],
    }


def _write_pilot_project(project_dir: Path) -> Path:
    """The synthetic model of the module docstring; returns the manifest."""
    manifest = _write_manifest(project_dir)
    model_root = project_dir / "model"
    write_model_element(model_root, {"name": "f32", "kind": "base_type", "size": 32, "encoding": "IEEE754"})
    write_model_element(model_root, {"name": "u16", "kind": "base_type", "size": 16, "encoding": "NONE"})
    write_model_element(model_root, {"name": "Pressure", "kind": "value_type", "base": "f32", "unit": "kPa"})
    write_model_element(model_root, {"name": "TemperatureRaw", "kind": "value_type", "base": "u16"})
    write_model_element(model_root, {"name": "Temperature", "kind": "application_type", "unit": "degC"})
    write_model_element(
        model_root,
        {"name": "ThermalMapping", "maps": [{"application": "Temperature", "implementation": "TemperatureRaw"}]},
        layer="mapping_sets",
    )
    _dump(
        model_root / "interfaces" / "IClimate.yaml",
        {
            "name": "IClimate",
            "kind": "sender_receiver",
            "data_elements": [
                {"name": "temperature", "application_type": "Temperature"},
                {"name": "pressure", "implementation_type": "Pressure"},
            ],
        },
    )
    _dump(
        model_root / "swc" / "source" / "ProducerSwc.yaml",
        {
            "name": "ProducerSwc",
            "type": "application",
            "ports": [{"name": "pClimate", "direction": "provide", "interface": "IClimate"}],
            "runnables": [
                _runnable("SampleFast", "writes", "pClimate", "temperature"),
                _runnable("SampleSlow", "writes", "pClimate", "pressure"),
            ],
            "handle_termination_and_restart": "no_support",
            "data_type_mappings": ["ThermalMapping"],
        },
    )
    for name, runnable, element in (
        ("HeaterSwc", "HeatStep", "temperature"),
        ("VentSwc", "VentStep", "pressure"),
    ):
        _dump(
            model_root / "swc" / "sink" / f"{name}.yaml",
            {
                "name": name,
                "type": "application",
                "ports": [{"name": "rClimate", "direction": "require", "interface": "IClimate"}],
                "runnables": [_runnable(runnable, "reads", "rClimate", element)],
                "handle_termination_and_restart": "no_support",
                "data_type_mappings": ["ThermalMapping"],
            },
        )
    _dump(
        model_root / "swc" / "outside" / "OutsideSwc.yaml",
        {
            "name": "OutsideSwc",
            "external": True,
            "external_ref": "/Outside/OutsideSwc",
            "external_dest": "APPLICATION-SW-COMPONENT-TYPE",
            "ports": [{"name": "sClimate", "direction": "require"}],
        },
    )
    _dump(
        model_root / "compositions" / "Bay.yaml",
        {
            "name": "Bay",
            "title": "Inner bay",
            "ports": [{"name": "bIn", "direction": "require", "interface": "IClimate"}],
            "members": [{"name": "heater", "type": "HeaterSwc"}, {"name": "vent", "type": "VentSwc"}],
            "delegations": [
                {"name": "bIn_to_heater", "port": "bIn", "member": "heater", "member_port": "rClimate"},
                {"name": "bIn_to_vent", "port": "bIn", "member": "vent", "member_port": "rClimate"},
            ],
        },
    )
    _dump(
        model_root / "compositions" / "Cluster.yaml",
        {
            "name": "Cluster",
            "title": "Outer cluster",
            "ports": [{"name": "cIn", "direction": "require", "interface": "IClimate"}],
            "members": [{"name": "bay", "composition": "Bay"}],
            "delegations": [{"name": "cIn_to_bay", "port": "cIn", "member": "bay", "member_port": "bIn"}],
        },
    )
    _dump(
        model_root / "compositions" / "Root.yaml",
        {
            "name": "Root",
            "title": "Root composition",
            "root": True,
            "members": [
                {"name": "producer", "type": "ProducerSwc"},
                {"name": "outside", "type": "OutsideSwc"},
                {"name": "cluster", "composition": "Cluster"},
            ],
        },
    )
    _dump(
        model_root / "connectors" / "Producer_to_Cluster.yaml",
        {
            "name": "Producer_to_Cluster",
            "from": {"swc": "ProducerSwc", "port": "pClimate"},
            "to": {"composition": "Cluster", "port": "cIn"},
        },
    )
    _dump(
        model_root / "connectors" / "Producer_to_Outside.yaml",
        {
            "name": "Producer_to_Outside",
            "from": {"swc": "ProducerSwc", "port": "pClimate"},
            "to": {"swc": "OutsideSwc", "port": "sClimate"},
        },
    )
    return manifest


def _answer_on_both_surfaces(
    body: dict[str, Any], tmp_path: Path, monkeypatch, capsys, *, edit: Callable[[Path], None] | None = None
) -> dict[str, Any]:
    """*body*'s ``rows`` document, asserted to be one answer through the CLI and through the protocol.

    *edit*, when given, changes the written model root before either surface reads it.
    """
    manifest = _write_pilot_project(tmp_path)
    if edit is not None:
        edit(tmp_path / "model")
    request = tmp_path / "request.json"
    request.write_text(json.dumps(body), encoding="utf-8", newline="\n")
    monkeypatch.setattr(
        "sys.argv", ["swcraft", "query", "--request", str(request), "--format", "json", "--project", str(manifest)]
    )
    main()
    document = json.loads(capsys.readouterr().out)
    assert document["schemaVersion"] == query.QUERY_ROWS_SCHEMA_VERSION
    response = engine.handle_request({"operation": "read", **body}, project=str(manifest))
    assert response["response"] == "read", response
    for key in ("derived", "join", "members", "where"):
        assert document.get(key) == body.get(key) == response.get(key), key
    assert document["rows"] == response["value"]["identities"]
    assert document.get("page") == response.get("page")
    return document


def _connection(connector: str, requester: str, port: str) -> dict[str, Any]:
    return {
        "element": connector,
        "derived": {
            "connection_ends": {
                "provider": {"swc": "ProducerSwc", "port": "pClimate"},
                "requester": {"swc": requester, "port": port},
            }
        },
    }


#: The table: one row per write, connector and read of the same data element.
EXPECTED_TABLE: list[dict[str, Any]] = [
    {
        "element": "ProducerSwc",
        "through": ["SampleFast"],
        "member": {"port": "pClimate", "element": "temperature"},
        "derived": {"base_type": {"implementation": "TemperatureRaw", "base": "u16"}},
        "connection": _connection("Producer_to_Cluster", "HeaterSwc", "rClimate"),
        "reader": {
            "element": "HeaterSwc",
            "through": ["HeatStep"],
            "member": {"port": "rClimate", "element": "temperature"},
        },
    },
    {
        "element": "ProducerSwc",
        "through": ["SampleSlow"],
        "member": {"port": "pClimate", "element": "pressure"},
        "derived": {"base_type": {"implementation": "Pressure", "base": "f32"}},
        "connection": _connection("Producer_to_Cluster", "VentSwc", "rClimate"),
        "reader": {
            "element": "VentSwc",
            "through": ["VentStep"],
            "member": {"port": "rClimate", "element": "pressure"},
        },
    },
]


class TestTheSenderReceiverTable:
    def test_the_request_answers_the_table_on_both_surfaces(self, tmp_path: Path, monkeypatch, capsys) -> None:
        assert _answer_on_both_surfaces(TABLE_REQUEST, tmp_path, monkeypatch, capsys)["rows"] == EXPECTED_TABLE

    def test_the_stubs_connection_reaches_the_join_and_is_dropped_there(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The first entry alone keeps the stub's connection; the reader entry drops it.

        Each write meets three requester ends, so the connection join answers six rows, two at
        ``OutsideSwc``; none survives the reader join, nor a write paired with the other element's reader.
        """
        connection_only = {**TABLE_REQUEST, "join": TABLE_REQUEST["join"][:1]}
        rows = _answer_on_both_surfaces(connection_only, tmp_path, monkeypatch, capsys)["rows"]
        requesters = [
            (row["member"]["element"], row["connection"]["derived"]["connection_ends"]["requester"]["swc"])
            for row in rows
        ]
        assert requesters == [
            ("temperature", "HeaterSwc"),
            ("temperature", "VentSwc"),
            ("temperature", "OutsideSwc"),
            ("pressure", "HeaterSwc"),
            ("pressure", "VentSwc"),
            ("pressure", "OutsideSwc"),
        ]
        at_the_stub = [row["connection"] for row in rows if row["connection"]["element"] == "Producer_to_Outside"]
        assert [connection["derived"]["connection_ends"]["requester"] for connection in at_the_stub] == [
            {"swc": "OutsideSwc", "port": "sClimate"}
        ] * 2

    def test_the_table_pages_once_on_both_surfaces(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A walk at ``limit: 1`` replays the whole-row cursor and yields the table once."""
        walked: list[dict[str, Any]] = []
        body: dict[str, Any] = {**TABLE_REQUEST, "limit": 1}
        while True:
            document = _answer_on_both_surfaces(body, tmp_path / f"page{len(walked)}", monkeypatch, capsys)
            walked.extend(document["rows"])
            if "next" not in document["page"]:
                break
            body = {**TABLE_REQUEST, "limit": 1, "after": document["page"]["next"]}
        assert walked == EXPECTED_TABLE

    def test_a_null_join_value_matches_nothing_on_both_surfaces(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A write and a read of an undeclared data element abstain at ``base_type``; a join matches no ``null``."""

        def ghost_accesses(model_root: Path) -> None:
            for name, accesses, runnable, port in (
                ("source/ProducerSwc", "writes", "SampleGhost", "pClimate"),
                ("sink/HeaterSwc", "reads", "HeatGhost", "rClimate"),
            ):
                path = model_root / "swc" / f"{name}.yaml"
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
                document["runnables"].append(_runnable(runnable, accesses, port, "ghost"))
                _dump(path, document)

        body = {
            "address": {"layer": "swc"},
            "form": "index",
            "members": "runnables.writes",
            "derived": ["base_type"],
            "join": [
                {
                    "with": {"address": {"layer": "swc"}, "members": "runnables.reads", "derived": ["base_type"]},
                    "on": [{"left": "derived.base_type.implementation", "right": "derived.base_type.implementation"}],
                    "as": "reader",
                }
            ],
        }
        writes = {key: body[key] for key in ("address", "form", "members", "derived")}
        all_writes = _answer_on_both_surfaces(writes, tmp_path / "writes", monkeypatch, capsys, edit=ghost_accesses)
        assert {
            "element": "ProducerSwc",
            "through": ["SampleGhost"],
            "member": {"port": "pClimate", "element": "ghost"},
            "derived": {"base_type": {"implementation": None, "base": None}},
        } in all_writes["rows"]
        rows = _answer_on_both_surfaces(body, tmp_path / "joined", monkeypatch, capsys, edit=ghost_accesses)["rows"]
        assert [(row["through"], row["reader"]["through"]) for row in rows] == [
            (["SampleFast"], ["HeatStep"]),
            (["SampleSlow"], ["VentStep"]),
        ]
