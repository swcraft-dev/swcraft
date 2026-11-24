"""The ARXML writer writes a stated name as it is, else the derived one.

Contract ``8.0.0`` lets a component state its internal behaviour's name
(``behavior_name``) and an event, a read, a write or a call state its own
(``name``). On the writer's side:

* **written: stated, else derived** -- a stated ``SHORT-NAME`` is written where
  one is set, the derived form (``{swc}_InternalBehavior``, ``TMT_``/``IT_``/``OIT_``
  with the retroactive counter, ``R_``/``W_``/``Call_``) everywhere else;
* **the counter runs over every event**: a stated event keeps its slot, so the
  derived name of an unstated event is the same whether or not another event
  states one;
* **written as it is, never fitted** -- no prefix, no counter, no case change;
* **the behaviour's path follows its name**: every ``START-ON-EVENT-REF`` and the
  ``SWC-IMPLEMENTATION``'s ``BEHAVIOR-REF`` name the stated behaviour;
* a stated name equal to the derived one states the same thing: the same bytes;
* **the structural backstop**: ``EVENTS`` and a runnable's access points are each
  one ``ShortNameScope``, so two elements written with one name are refused even
  past the model-level guards.

Every stated name here is invented. What this module does **not** reach: the
refusals of an illegal, colliding or duplicate stated name before anything is
written (``test_stated_name_rules.py``); the XSD evidence
(``test_arxml_xsd_conformance.py``'s ``TestTheStatedNamesModelConforms``); the
importers and component exchange (``test_stated_names_import.py``).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT
from lxml import etree
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.schema import SWComponent
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml
from swcraft_tools.writer.arxml_generation import _emit_access_points, _emit_events
from swcraft_tools.writer.arxml_xml import ShortNameScope

NS = f"{{{NAMESPACE}}}"
BASIC = REPO_ROOT / "examples" / "basic"
VERSIONS = ("4.2.2", "4.3.0")
PROVIDER = "swc/example/ExampleSWC.yaml"
CONSUMER = "swc/example/ExampleConsumerSWC.yaml"

#: The invented names the model states; every other element of the same kinds states none.
BEHAVIOUR = "ProviderBehaviour"
TIMING = "Tick10ms"
INIT = "Startup"
OPERATION = "OnReadDtc"
WRITE = "SendValue"
READ = "GetValue"
CALL = "CallRead"

Mutation = Callable[[dict[str, Any]], None]


def _provider(document: dict[str, Any], *, stated: bool = True) -> None:
    """``ExampleSWC`` gains a server port, an init runnable and a server runnable; *stated* names half of each kind."""
    if stated:
        document["behavior_name"] = BEHAVIOUR
    document["ports"].append({"name": "pDiag", "direction": "provide", "interface": "IDiagnosticService"})
    (run,) = document["runnables"]
    run["events"] = [
        {"type": "timing", "period": 0.01, **({"name": TIMING} if stated else {})},
        {"type": "timing", "period": 0.02},
    ]
    run["writes"] = [{"port": "pOut", "element": "value", **({"name": WRITE} if stated else {})}]
    document["runnables"].append(
        {"name": "ProviderInit", "events": [{"type": "init", **({"name": INIT} if stated else {})}]}
    )
    document["runnables"].append(
        {
            "name": "ProviderServe",
            "events": [
                {
                    "type": "operation",
                    "port": "pDiag",
                    "operation": "ReadDtc",
                    **({"name": OPERATION} if stated else {}),
                },
                {"type": "operation", "port": "pDiag", "operation": "ClearDtc"},
            ],
        }
    )


def _consumer(document: dict[str, Any], *, stated: bool = True) -> None:
    """``ExampleConsumerSWC`` gains a client port; it reads by value and calls two operations, stating one of each."""
    document["ports"].append({"name": "rDiag", "direction": "require", "interface": "IDiagnosticService"})
    (run,) = document["runnables"]
    run["reads"] = [{"port": "rIn", "element": "value", "by": "value", **({"name": READ} if stated else {})}]
    run["calls"] = [
        {"port": "rDiag", "operation": "ReadDtc", **({"name": CALL} if stated else {})},
        {"port": "rDiag", "operation": "ClearDtc"},
    ]


def _project(tmp_path: Path, *mutations: tuple[str, Mutation]) -> Path:
    """``examples/basic``'s model, each ``(file, mutation)`` applied to the file's YAML; the tree is copied."""
    project = tmp_path / "project"
    shutil.copytree(BASIC, project, ignore=shutil.ignore_patterns("dist", "views"))
    for relative, mutation in mutations:
        path = project / "model" / relative
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutation(document)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return project / "model"


def stated_names_model_root(tmp_path: Path) -> Path:
    """``examples/basic`` stating one name of every kind the contract lets a model state, the rest derived."""
    return _project(tmp_path, (PROVIDER, _provider), (CONSUMER, _consumer))


def _unstated_model_root(tmp_path: Path) -> Path:
    return _project(
        tmp_path,
        (PROVIDER, lambda d: _provider(d, stated=False)),
        (CONSUMER, lambda d: _consumer(d, stated=False)),
    )


# ---------------------------------------------------------------------------
# Reading the output
# ---------------------------------------------------------------------------


def _local(node: etree._Element) -> str:
    return etree.QName(node).localname


def _short_name(node: etree._Element) -> str | None:
    found = node.find(f"{NS}SHORT-NAME")
    return None if found is None else found.text


def _generate(model_root: Path, out: Path, version: str = "4.2.2") -> etree._Element:
    generate_arxml(str(model_root), str(out), version=version)
    return etree.parse(str(out)).getroot()


def _component(root: etree._Element, name: str) -> etree._Element:
    (node,) = [node for node in root.iter(f"{NS}APPLICATION-SW-COMPONENT-TYPE") if _short_name(node) == name]
    return node


def _behaviour(root: etree._Element, component: str) -> etree._Element:
    (node,) = _component(root, component).iter(f"{NS}SWC-INTERNAL-BEHAVIOR")
    return node


def _event_names(root: etree._Element, component: str) -> list[tuple[str, str | None]]:
    events = _behaviour(root, component).find(f"{NS}EVENTS")
    return [(_local(node), _short_name(node)) for node in events]


def _access_names(root: etree._Element, component: str, runnable: str) -> list[tuple[str, str | None]]:
    (entity,) = [
        node for node in _behaviour(root, component).iter(f"{NS}RUNNABLE-ENTITY") if _short_name(node) == runnable
    ]
    containers = ("DATA-RECEIVE-POINT-BY-VALUES", "DATA-SEND-POINTS", "SERVER-CALL-POINTS")
    return [
        (_local(container), _short_name(point))
        for container in entity
        if _local(container) in containers
        for point in container
    ]


# ---------------------------------------------------------------------------
# The model is one check-model accepts
# ---------------------------------------------------------------------------


def test_check_model_accepts_the_stated_names(tmp_path: Path) -> None:
    """The model every case below generates is valid: the writer's output is not a refusal's by-product."""
    result = check_cross_layer_consistency(str(stated_names_model_root(tmp_path)))
    assert [record for record in result.records if record.severity == "error"] == []


# ---------------------------------------------------------------------------
# Stated, else derived, written as it is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", VERSIONS)
class TestTheStatedNamesAreWritten:
    def test_the_behaviour_is_written_with_its_stated_name(self, tmp_path: Path, version: str) -> None:
        root = _generate(stated_names_model_root(tmp_path), tmp_path / "out.arxml", version)

        assert _short_name(_behaviour(root, "ExampleSWC")) == BEHAVIOUR
        assert _short_name(_behaviour(root, "ExampleConsumerSWC")) == "ExampleConsumerSWC_InternalBehavior"

    def test_every_start_reference_and_the_behavior_reference_follow_the_stated_behaviour(
        self, tmp_path: Path, version: str
    ) -> None:
        root = _generate(stated_names_model_root(tmp_path), tmp_path / "out.arxml", version)
        behaviour_path = f"/ComponentTypes/ExampleSWC/{BEHAVIOUR}"

        starts = [node.text for node in _behaviour(root, "ExampleSWC").iter(f"{NS}START-ON-EVENT-REF")]
        assert starts == [
            f"{behaviour_path}/ExampleRun",
            f"{behaviour_path}/ExampleRun",
            f"{behaviour_path}/ProviderInit",
            f"{behaviour_path}/ProviderServe",
            f"{behaviour_path}/ProviderServe",
        ]
        references = {
            _short_name(node): node.find(f"{NS}BEHAVIOR-REF").text for node in root.iter(f"{NS}SWC-IMPLEMENTATION")
        }
        assert references["ExampleSWC_Implementation"] == behaviour_path
        assert references["ExampleConsumerSWC_Implementation"] == (
            "/ComponentTypes/ExampleConsumerSWC/ExampleConsumerSWC_InternalBehavior"
        )

    def test_each_event_is_written_with_its_stated_name_else_the_derived_one(
        self, tmp_path: Path, version: str
    ) -> None:
        """The second timing event keeps the counter's ``_1``: the stated first one holds its slot."""
        root = _generate(stated_names_model_root(tmp_path), tmp_path / "out.arxml", version)

        assert _event_names(root, "ExampleSWC") == [
            ("TIMING-EVENT", TIMING),
            ("TIMING-EVENT", "TMT_ExampleRun_1"),
            ("INIT-EVENT", INIT),
            ("OPERATION-INVOKED-EVENT", OPERATION),
            ("OPERATION-INVOKED-EVENT", "OIT_pDiag_ClearDtc"),
        ]

    def test_each_access_point_is_written_with_its_stated_name_else_the_derived_one(
        self, tmp_path: Path, version: str
    ) -> None:
        root = _generate(stated_names_model_root(tmp_path), tmp_path / "out.arxml", version)

        assert _access_names(root, "ExampleSWC", "ExampleRun") == [("DATA-SEND-POINTS", WRITE)]
        assert _access_names(root, "ExampleConsumerSWC", "ExampleConsumerRun") == [
            ("DATA-RECEIVE-POINT-BY-VALUES", READ),
            ("SERVER-CALL-POINTS", CALL),
            ("SERVER-CALL-POINTS", "Call_rDiag_ClearDtc"),
        ]


class TestWhatAStatedNameDoesNotMove:
    def test_the_unstated_model_writes_every_derived_name(self, tmp_path: Path) -> None:
        """The same shapes stating nothing: the derived forms, the counter over both timing events."""
        root = _generate(_unstated_model_root(tmp_path), tmp_path / "out.arxml")

        assert _short_name(_behaviour(root, "ExampleSWC")) == "ExampleSWC_InternalBehavior"
        assert _event_names(root, "ExampleSWC") == [
            ("TIMING-EVENT", "TMT_ExampleRun_0"),
            ("TIMING-EVENT", "TMT_ExampleRun_1"),
            ("INIT-EVENT", "IT_ProviderInit"),
            ("OPERATION-INVOKED-EVENT", "OIT_pDiag_ReadDtc"),
            ("OPERATION-INVOKED-EVENT", "OIT_pDiag_ClearDtc"),
        ]
        assert _access_names(root, "ExampleConsumerSWC", "ExampleConsumerRun") == [
            ("DATA-RECEIVE-POINT-BY-VALUES", "R_rIn_value"),
            ("SERVER-CALL-POINTS", "Call_rDiag_ReadDtc"),
            ("SERVER-CALL-POINTS", "Call_rDiag_ClearDtc"),
        ]

    @pytest.mark.parametrize("version", VERSIONS)
    def test_a_stated_name_equal_to_the_derived_one_writes_the_same_bytes(self, tmp_path: Path, version: str) -> None:
        """The ``symbol`` precedent: stating the derived name states the same thing, byte for byte."""

        def provider(document: dict[str, Any]) -> None:
            _provider(document, stated=False)
            document["behavior_name"] = "ExampleSWC_InternalBehavior"
            (first, second) = document["runnables"][0]["events"]
            first["name"], second["name"] = "TMT_ExampleRun_0", "TMT_ExampleRun_1"
            document["runnables"][0]["writes"][0]["name"] = "W_pOut_value"
            document["runnables"][1]["events"][0]["name"] = "IT_ProviderInit"
            for event in document["runnables"][2]["events"]:
                event["name"] = f"OIT_{event['port']}_{event['operation']}"

        def consumer(document: dict[str, Any]) -> None:
            _consumer(document, stated=False)
            (run,) = document["runnables"]
            run["reads"][0]["name"] = "R_rIn_value"
            for call in run["calls"]:
                call["name"] = f"Call_{call['port']}_{call['operation']}"

        unstated = tmp_path / "unstated.arxml"
        generate_arxml(str(_unstated_model_root(tmp_path / "a")), str(unstated), version=version)
        stated = tmp_path / "stated.arxml"
        generate_arxml(
            str(_project(tmp_path / "b", (PROVIDER, provider), (CONSUMER, consumer))), str(stated), version=version
        )

        assert stated.read_bytes() == unstated.read_bytes()

    def test_a_stated_name_is_written_as_it_is(self, tmp_path: Path) -> None:
        """No prefix, no counter, no case change -- two timing events on one runnable, both stated."""

        def provider(document: dict[str, Any]) -> None:
            (run,) = document["runnables"]
            run["events"] = [
                {"type": "timing", "period": 0.01, "name": "fast"},
                {"type": "timing", "period": 0.1, "name": "SLOW"},
            ]

        root = _generate(_project(tmp_path, (PROVIDER, provider)), tmp_path / "out.arxml")

        assert _event_names(root, "ExampleSWC") == [("TIMING-EVENT", "fast"), ("TIMING-EVENT", "SLOW")]


# ---------------------------------------------------------------------------
# The structural backstop
# ---------------------------------------------------------------------------


class TestShortNameScopeRefusesWhatTheRulesMissed:
    """Called past ``_check_component_names``: the two scopes alone refuse a written duplicate."""

    def test_two_events_written_with_one_name(self) -> None:
        swc = SWComponent.model_validate(
            {
                "name": "S",
                "type": "application",
                "runnables": [
                    {
                        "name": "R",
                        "events": [
                            {"type": "timing", "period": 0.01, "name": "Tick"},
                            {"type": "init", "name": "Tick"},
                        ],
                    }
                ],
            }
        )
        behaviour = etree.Element(f"{NS}SWC-INTERNAL-BEHAVIOR")

        with pytest.raises(ValueError, match="Duplicate SHORT-NAME 'Tick' in EVENTS of SWC 'S'"):
            _emit_events(behaviour, swc, "/ComponentTypes/S/S_InternalBehavior", {})

    def test_two_access_points_written_with_one_name(self, tmp_path: Path) -> None:
        def consumer(document: dict[str, Any]) -> None:
            _consumer(document, stated=False)
            (run,) = document["runnables"]
            run["reads"][0]["name"] = "Shared"
            run["calls"][0]["name"] = "Shared"

        from swcraft_tools.reader import read_interfaces, read_swc_model

        model_root = _project(tmp_path, (CONSUMER, consumer))
        interfaces = {iface.name: iface for iface in read_interfaces(str(model_root))}
        (swc,) = [swc for swc in read_swc_model(str(model_root)) if swc.name == "ExampleConsumerSWC"]
        port_interfaces = {port.name: (port, interfaces[port.interface]) for port in swc.ports}
        entity = etree.Element(f"{NS}RUNNABLE-ENTITY")

        with pytest.raises(ValueError, match="'Shared'"):
            _emit_access_points(
                entity, swc, swc.runnables[0], port_interfaces, ShortNameScope(entity, "the runnable"), []
            )
