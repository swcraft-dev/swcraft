"""The ARXML writer's half of contract ``12.7.0``: implicit access, asynchronous calls, ``call_returned``.

Over a copy of ``fixtures/access_forms/model`` at 4.2.2 and 4.3.0 (one element order at both): each form
in its container and position, the call and result points and the returns event, stated and derived
names, the version-aware rule (``[constr_2006]``: an asynchronous call with no result point refused at
4.2.2, written at 4.3.0), and the refusal matrix, which leaves a fresh directory empty. An asynchronous
call point's ``TIMEOUT`` is written only when stated, never an invented ``0``; a synchronous call point keeps
its ``TIMEOUT`` ``0``.

What this module does **not** reach: XSD conformance (``test_arxml_xsd_conformance.py``); the importer
and the component export; a wait point, which SWCraft does not model; and whether an RTE generator
accepts the file -- this is example-based evidence, not a proof over every model.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, _write_manifest
from external_stubs_support import change
from lxml import etree
from swcraft_tools.cli import main
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer.arxml_generation import (
    _ACCESS_CONTAINER_ORDER,
    INTERNAL_BEHAVIOR_SUFFIX,
    RESULT_POINT_PREFIX,
    RETURNS_EVENT_PREFIX,
    generate_arxml,
)

FIXTURE = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "access_forms" / "model"
NS = {"ar": "http://autosar.org/schema/r4.0"}
VERSIONS = ("4.2.2", "4.3.0")
CONTROLLER = "BrakeController"
CONTROLLER_FILE = "swc/chassis/BrakeController.yaml"
COMPUTE = "ComputeBrakeTorque"
ON_WRITTEN = "OnBlockWritten"
DIAGNOSE = "Diagnose"
BEHAVIOUR = f"/ComponentTypes/{CONTROLLER}/{CONTROLLER}{INTERNAL_BEHAVIOR_SUFFIX}"
NEW_TAGS = (
    "DATA-READ-ACCESSS",
    "DATA-WRITE-ACCESSS",
    "ASYNCHRONOUS-SERVER-CALL-POINT",
    "ASYNCHRONOUS-SERVER-CALL-RESULT-POINTS",
    "ASYNCHRONOUS-SERVER-CALL-RESULT-POINT",
    "ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT",
)


@pytest.fixture
def model(tmp_path: Path) -> Path:
    root = tmp_path / "model"
    shutil.copytree(FIXTURE, root)
    return root


def _local(node: etree._Element) -> str:
    return etree.QName(node).localname


def _children(node: etree._Element) -> list[str]:
    return [_local(child) for child in node]


def _generate(model_root: Path, tmp_path: Path, version: str) -> etree._Element:
    output = tmp_path / f"out-{version}" / "model.arxml"
    generate_arxml(str(model_root), str(output), version=version)
    return etree.parse(str(output)).getroot()


def _named(root: etree._Element, tag: str, name: str) -> etree._Element:
    (node,) = root.xpath(f"//ar:{tag}[ar:SHORT-NAME='{name}']", namespaces=NS)
    return node


def _short_names(nodes: list[etree._Element]) -> list[str]:
    return [node.findtext("ar:SHORT-NAME", namespaces=NS) for node in nodes]


def _ref(node: etree._Element, tag: str) -> tuple[str | None, str | None]:
    ref = node.find(f"ar:{tag}", namespaces=NS)
    assert ref is not None, f"{tag} missing under {_local(node)}"
    return ref.get("DEST"), ref.text


def _runnable(document: dict[str, Any], name: str) -> dict[str, Any]:
    (runnable,) = [entry for entry in document["runnables"] if entry["name"] == name]
    return runnable


def _call(document: dict[str, Any], runnable: str, port: str, operation: str) -> dict[str, Any]:
    (call,) = [
        entry
        for entry in _runnable(document, runnable)["calls"]
        if (entry["port"], entry["operation"]) == (port, operation)
    ]
    return call


def _controller(model_root: Path, mutate: Any) -> None:
    change(model_root, CONTROLLER_FILE, mutate)


def _drop_the_event(model_root: Path) -> None:
    """``OnBlockWritten`` started by a timing event: ``WriteBlock``'s asynchronous call has no result point."""
    _controller(model_root, lambda d: _runnable(d, ON_WRITTEN).update(events=[{"type": "timing", "period": 0.1}]))


def _returned(port: str, operation: str, **fields: Any) -> dict[str, Any]:
    return {"type": "call_returned", "port": port, "operation": operation, **fields}


# Each form in its container and position


class TestEachFormInItsContainer:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_the_caller_s_children_in_the_schema_order(self, model: Path, tmp_path: Path, version: str) -> None:
        compute = _named(_generate(model, tmp_path, version), "RUNNABLE-ENTITY", COMPUTE)

        assert _children(compute) == [
            "SHORT-NAME",
            "MINIMUM-START-INTERVAL",
            "ASYNCHRONOUS-SERVER-CALL-RESULT-POINTS",
            "CAN-BE-INVOKED-CONCURRENTLY",
            "DATA-READ-ACCESSS",
            "DATA-RECEIVE-POINT-BY-ARGUMENTS",
            "DATA-WRITE-ACCESSS",
            "SERVER-CALL-POINTS",
            "SYMBOL",
        ]

    @pytest.mark.parametrize("version", VERSIONS)
    def test_an_implicit_read_beside_a_read_by_value(self, model: Path, tmp_path: Path, version: str) -> None:
        diagnose = _named(_generate(model, tmp_path, version), "RUNNABLE-ENTITY", DIAGNOSE)

        assert _children(diagnose) == [
            "SHORT-NAME",
            "MINIMUM-START-INTERVAL",
            "CAN-BE-INVOKED-CONCURRENTLY",
            "DATA-READ-ACCESSS",
            "DATA-RECEIVE-POINT-BY-VALUES",
            "SERVER-CALL-POINTS",
            "SYMBOL",
        ]

    def test_an_implicit_access_is_the_explicit_forms_variable_access(self, model: Path, tmp_path: Path) -> None:
        compute = _named(_generate(model, tmp_path, "4.2.2"), "RUNNABLE-ENTITY", COMPUTE)

        (read,) = compute.findall("ar:DATA-READ-ACCESSS/ar:VARIABLE-ACCESS", namespaces=NS)
        (write,) = compute.findall("ar:DATA-WRITE-ACCESSS/ar:VARIABLE-ACCESS", namespaces=NS)
        (explicit,) = compute.findall("ar:DATA-RECEIVE-POINT-BY-ARGUMENTS/ar:VARIABLE-ACCESS", namespaces=NS)
        assert _short_names([read, explicit, write]) == [
            "R_rWheelSpeed_speed",
            "R_rPedal_position",
            "W_pBrakeTorque_torque",
        ]
        iref = read.find("ar:ACCESSED-VARIABLE/ar:AUTOSAR-VARIABLE-IREF", namespaces=NS)
        assert _ref(iref, "PORT-PROTOTYPE-REF") == ("R-PORT-PROTOTYPE", f"/ComponentTypes/{CONTROLLER}/rWheelSpeed")
        assert _ref(iref, "TARGET-DATA-PROTOTYPE-REF") == (
            "VARIABLE-DATA-PROTOTYPE",
            "/PortInterfaces/WheelSpeedIf/speed",
        )
        write_iref = write.find("ar:ACCESSED-VARIABLE/ar:AUTOSAR-VARIABLE-IREF", namespaces=NS)
        assert _ref(write_iref, "PORT-PROTOTYPE-REF") == (
            "P-PORT-PROTOTYPE",
            f"/ComponentTypes/{CONTROLLER}/pBrakeTorque",
        )

    def test_the_container_order_is_the_schema_s(self) -> None:
        assert _ACCESS_CONTAINER_ORDER == (
            "DATA-READ-ACCESSS",
            "DATA-RECEIVE-POINT-BY-ARGUMENTS",
            "DATA-RECEIVE-POINT-BY-VALUES",
            "DATA-SEND-POINTS",
            "DATA-WRITE-ACCESSS",
            "SERVER-CALL-POINTS",
        )

    def test_an_implicit_write_of_a_sender(self, model: Path, tmp_path: Path) -> None:
        sample = _named(_generate(model, tmp_path, "4.2.2"), "RUNNABLE-ENTITY", "SampleSpeed")

        assert "DATA-WRITE-ACCESSS" in _children(sample)
        assert "DATA-SEND-POINTS" not in _children(sample)


# The call points


class TestTheCallPoints:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_each_call_point_by_its_synchrony(self, model: Path, tmp_path: Path, version: str) -> None:
        root = _generate(model, tmp_path, version)
        compute = _named(root, "RUNNABLE-ENTITY", COMPUTE)
        diagnose = _named(root, "RUNNABLE-ENTITY", DIAGNOSE)

        points = compute.find("ar:SERVER-CALL-POINTS", namespaces=NS)
        assert [(_local(point), name) for point, name in zip(points, _short_names(list(points)), strict=True)] == [
            ("ASYNCHRONOUS-SERVER-CALL-POINT", "Call_rNvStorage_WriteBlock"),
            ("ASYNCHRONOUS-SERVER-CALL-POINT", "Call_rCalibration_ReadBlock"),
        ]
        (synchronous,) = diagnose.find("ar:SERVER-CALL-POINTS", namespaces=NS)
        assert _local(synchronous) == "SYNCHRONOUS-SERVER-CALL-POINT"

    def test_timeout_written_exactly_when_stated(self, model: Path, tmp_path: Path) -> None:
        root = _generate(model, tmp_path, "4.2.2")
        unstated = _named(root, "ASYNCHRONOUS-SERVER-CALL-POINT", "Call_rNvStorage_WriteBlock")
        stated = _named(root, "ASYNCHRONOUS-SERVER-CALL-POINT", "Call_rCalibration_ReadBlock")

        assert _children(unstated) == ["SHORT-NAME", "OPERATION-IREF"]
        assert _children(stated) == ["SHORT-NAME", "OPERATION-IREF", "TIMEOUT"]
        assert stated.findtext("ar:TIMEOUT", namespaces=NS) == "0.5"

    def test_a_stated_timeout_of_zero_is_written(self, model: Path, tmp_path: Path) -> None:
        _controller(model, lambda d: _call(d, COMPUTE, "rCalibration", "ReadBlock").update(timeout=0))

        stated = _named(
            _generate(model, tmp_path, "4.2.2"), "ASYNCHRONOUS-SERVER-CALL-POINT", "Call_rCalibration_ReadBlock"
        )

        assert stated.findtext("ar:TIMEOUT", namespaces=NS) == "0"

    def test_the_synchronous_point_keeps_timeout_zero(self, model: Path, tmp_path: Path) -> None:
        synchronous = _named(
            _generate(model, tmp_path, "4.2.2"), "SYNCHRONOUS-SERVER-CALL-POINT", "Call_rCalibration_GetVersion"
        )

        assert _children(synchronous) == ["SHORT-NAME", "OPERATION-IREF", "TIMEOUT"]
        assert synchronous.findtext("ar:TIMEOUT", namespaces=NS) == "0"

    def test_the_asynchronous_point_s_operation_iref(self, model: Path, tmp_path: Path) -> None:
        point = _named(
            _generate(model, tmp_path, "4.2.2"), "ASYNCHRONOUS-SERVER-CALL-POINT", "Call_rNvStorage_WriteBlock"
        )

        iref = point.find("ar:OPERATION-IREF", namespaces=NS)
        assert _children(iref) == ["CONTEXT-R-PORT-REF", "TARGET-REQUIRED-OPERATION-REF"]
        assert _ref(iref, "CONTEXT-R-PORT-REF") == ("R-PORT-PROTOTYPE", f"/ComponentTypes/{CONTROLLER}/rNvStorage")
        assert _ref(iref, "TARGET-REQUIRED-OPERATION-REF") == (
            "CLIENT-SERVER-OPERATION",
            "/PortInterfaces/NvStorageIf/WriteBlock",
        )


# The result points and the returns event


class TestTheResultPoints:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_a_poll_s_result_point_on_its_own_runnable(self, model: Path, tmp_path: Path, version: str) -> None:
        compute = _named(_generate(model, tmp_path, version), "RUNNABLE-ENTITY", COMPUTE)

        (point,) = compute.find("ar:ASYNCHRONOUS-SERVER-CALL-RESULT-POINTS", namespaces=NS)
        assert _local(point) == "ASYNCHRONOUS-SERVER-CALL-RESULT-POINT"
        assert _children(point) == ["SHORT-NAME", "ASYNCHRONOUS-SERVER-CALL-POINT-REF"]
        assert _short_names([point]) == [f"{RESULT_POINT_PREFIX}rCalibration_ReadBlock"]
        assert _ref(point, "ASYNCHRONOUS-SERVER-CALL-POINT-REF") == (
            "ASYNCHRONOUS-SERVER-CALL-POINT",
            f"{BEHAVIOUR}/{COMPUTE}/Call_rCalibration_ReadBlock",
        )

    @pytest.mark.parametrize("version", VERSIONS)
    def test_an_event_s_result_point_on_the_callback(self, model: Path, tmp_path: Path, version: str) -> None:
        written = _named(_generate(model, tmp_path, version), "RUNNABLE-ENTITY", ON_WRITTEN)

        assert _children(written) == [
            "SHORT-NAME",
            "MINIMUM-START-INTERVAL",
            "ASYNCHRONOUS-SERVER-CALL-RESULT-POINTS",
            "CAN-BE-INVOKED-CONCURRENTLY",
            "SYMBOL",
        ]
        (point,) = written.find("ar:ASYNCHRONOUS-SERVER-CALL-RESULT-POINTS", namespaces=NS)
        assert _short_names([point]) == [f"{RESULT_POINT_PREFIX}rNvStorage_WriteBlock"]
        # The component's one asynchronous call point of the pair, on the runnable that makes the call.
        assert _ref(point, "ASYNCHRONOUS-SERVER-CALL-POINT-REF") == (
            "ASYNCHRONOUS-SERVER-CALL-POINT",
            f"{BEHAVIOUR}/{COMPUTE}/Call_rNvStorage_WriteBlock",
        )

    def test_polls_first_then_events(self, model: Path, tmp_path: Path) -> None:
        def mutate(document: dict[str, Any]) -> None:
            _runnable(document, COMPUTE)["events"].append(_returned("rNvStorage", "WriteBlock"))
            _runnable(document, ON_WRITTEN)["events"] = [{"type": "timing", "period": 0.1}]

        _controller(model, mutate)
        assert check_cross_layer_consistency(str(model)).consistent

        compute = _named(_generate(model, tmp_path, "4.2.2"), "RUNNABLE-ENTITY", COMPUTE)

        points = list(compute.find("ar:ASYNCHRONOUS-SERVER-CALL-RESULT-POINTS", namespaces=NS))
        assert _short_names(points) == ["Result_rCalibration_ReadBlock", "Result_rNvStorage_WriteBlock"]

    @pytest.mark.parametrize("version", VERSIONS)
    def test_the_returns_event(self, model: Path, tmp_path: Path, version: str) -> None:
        event = _named(
            _generate(model, tmp_path, version),
            "ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT",
            f"{RETURNS_EVENT_PREFIX}rNvStorage_WriteBlock",
        )

        assert _children(event) == ["SHORT-NAME", "START-ON-EVENT-REF", "EVENT-SOURCE-REF"]
        assert _ref(event, "START-ON-EVENT-REF") == ("RUNNABLE-ENTITY", f"{BEHAVIOUR}/{ON_WRITTEN}")
        assert _ref(event, "EVENT-SOURCE-REF") == (
            "ASYNCHRONOUS-SERVER-CALL-RESULT-POINT",
            f"{BEHAVIOUR}/{ON_WRITTEN}/Result_rNvStorage_WriteBlock",
        )

    def test_the_returns_event_among_the_events_by_runnable(self, model: Path, tmp_path: Path) -> None:
        root = _generate(model, tmp_path, "4.2.2")
        behaviour = _named(root, "SWC-INTERNAL-BEHAVIOR", f"{CONTROLLER}{INTERNAL_BEHAVIOR_SUFFIX}")

        events = list(behaviour.find("ar:EVENTS", namespaces=NS))
        assert [(_local(event), name) for event, name in zip(events, _short_names(events), strict=True)] == [
            ("TIMING-EVENT", f"TMT_{COMPUTE}"),
            ("ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT", "ASRT_rNvStorage_WriteBlock"),
            ("TIMING-EVENT", f"TMT_{DIAGNOSE}"),
        ]


class TestStatedNamesAndPlacement:
    """Every name stated, the component in a stated ``package`` with a stated ``behavior_name``."""

    @pytest.fixture
    def stated(self, model: Path) -> Path:
        def mutate(document: dict[str, Any]) -> None:
            document["package"] = "Chassis"
            document["behavior_name"] = "BrakeBehaviour"
            _call(document, COMPUTE, "rNvStorage", "WriteBlock")["name"] = "StoreCall"
            _call(document, COMPUTE, "rCalibration", "ReadBlock")["result"] = "CalibrationFetched"
            _runnable(document, ON_WRITTEN)["events"] = [
                _returned("rNvStorage", "WriteBlock", result="StoreDone", name="OnStoreDone")
            ]

        _controller(model, mutate)
        assert check_cross_layer_consistency(str(model)).consistent
        return model

    @pytest.mark.parametrize("version", VERSIONS)
    def test_every_stated_name_and_path(self, stated: Path, tmp_path: Path, version: str) -> None:
        root = _generate(stated, tmp_path, version)
        behaviour = f"/ComponentTypes/Chassis/{CONTROLLER}/BrakeBehaviour"

        polled = _named(root, "ASYNCHRONOUS-SERVER-CALL-RESULT-POINT", "CalibrationFetched")
        assert _ref(polled, "ASYNCHRONOUS-SERVER-CALL-POINT-REF") == (
            "ASYNCHRONOUS-SERVER-CALL-POINT",
            f"{behaviour}/{COMPUTE}/Call_rCalibration_ReadBlock",
        )
        returned = _named(root, "ASYNCHRONOUS-SERVER-CALL-RESULT-POINT", "StoreDone")
        assert _ref(returned, "ASYNCHRONOUS-SERVER-CALL-POINT-REF") == (
            "ASYNCHRONOUS-SERVER-CALL-POINT",
            f"{behaviour}/{COMPUTE}/StoreCall",
        )
        event = _named(root, "ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT", "OnStoreDone")
        assert _ref(event, "START-ON-EVENT-REF") == ("RUNNABLE-ENTITY", f"{behaviour}/{ON_WRITTEN}")
        assert _ref(event, "EVENT-SOURCE-REF") == (
            "ASYNCHRONOUS-SERVER-CALL-RESULT-POINT",
            f"{behaviour}/{ON_WRITTEN}/StoreDone",
        )
        point = _named(root, "ASYNCHRONOUS-SERVER-CALL-POINT", "StoreCall")
        assert _ref(point.find("ar:OPERATION-IREF", namespaces=NS), "CONTEXT-R-PORT-REF") == (
            "R-PORT-PROTOTYPE",
            f"/ComponentTypes/Chassis/{CONTROLLER}/rNvStorage",
        )

    def test_the_derived_event_name_is_counted_with_the_others(self, model: Path, tmp_path: Path) -> None:
        """Two events of one derived base take the shared retroactive counter, as ``OIT_`` and ``DRT_`` do."""
        _controller(model, lambda d: _runnable(d, DIAGNOSE)["events"].append({"type": "timing", "period": 2.0}))

        root = _generate(model, tmp_path, "4.2.2")

        assert _short_names(root.xpath("//ar:ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT", namespaces=NS)) == [
            "ASRT_rNvStorage_WriteBlock"
        ]
        assert _short_names(root.xpath(f"//ar:TIMING-EVENT[contains(ar:SHORT-NAME, '{DIAGNOSE}')]", namespaces=NS)) == [
            f"TMT_{DIAGNOSE}_0",
            f"TMT_{DIAGNOSE}_1",
        ]


# The version-aware rule


class TestTheVersionDecides:
    def test_4_2_2_refuses_an_asynchronous_call_without_a_result(self, model: Path, tmp_path: Path) -> None:
        _drop_the_event(model)
        output = tmp_path / "out" / "model.arxml"

        with pytest.raises(ValueError, match=r"rule:swc\.call\.async-without-result:"):
            generate_arxml(str(model), str(output), version="4.2.2")

        assert not output.parent.exists() or not any(output.parent.iterdir())

    def test_4_3_0_writes_it_with_no_result_point(self, model: Path, tmp_path: Path) -> None:
        _drop_the_event(model)

        root = _generate(model, tmp_path, "4.3.0")

        point = _named(root, "ASYNCHRONOUS-SERVER-CALL-POINT", "Call_rNvStorage_WriteBlock")
        assert _children(point) == ["SHORT-NAME", "OPERATION-IREF"]
        assert not root.xpath("//ar:ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT", namespaces=NS)
        referenced = [ref.text for ref in root.xpath("//ar:ASYNCHRONOUS-SERVER-CALL-POINT-REF", namespaces=NS)]
        assert referenced == [f"{BEHAVIOUR}/{COMPUTE}/Call_rCalibration_ReadBlock"]
        assert "ASYNCHRONOUS-SERVER-CALL-RESULT-POINTS" not in _children(_named(root, "RUNNABLE-ENTITY", ON_WRITTEN))


def _project(tmp_path: Path, version: str | None) -> Path:
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project / "model")
    _write_manifest(project)
    if version is not None:
        manifest = project / "swcraft.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f'arxml_version: "{version}"\n', encoding="utf-8", newline="\n"
        )
    _drop_the_event(project / "model")
    return project


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["swcraft", *argv])
    try:
        main()
    except SystemExit as exit_:
        return int(exit_.code or 0)
    return 0


class TestTheCliOverrideReachesTheRule:
    """``--arxml-version`` decides for its run, against the manifest's version, both ways."""

    def test_4_3_0_over_a_4_2_2_manifest_writes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = _project(tmp_path, None)
        manifest = project / "swcraft.yaml"

        assert _run(monkeypatch, "generate", "arxml", "--project", str(manifest)) == 1
        assert not (project / "dist" / "model.arxml").exists()
        code = _run(monkeypatch, "generate", "arxml", "--project", str(manifest), "--arxml-version", "4.3.0")

        assert code == 0
        text = (project / "dist" / "model.arxml").read_text(encoding="utf-8")
        assert "<ASYNCHRONOUS-SERVER-CALL-POINT>" in text
        assert "AUTOSAR_4-3-0.xsd" in text
        assert "arxml_version" not in manifest.read_text(encoding="utf-8")

    def test_4_2_2_over_a_4_3_0_manifest_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project = _project(tmp_path, "4.3.0")
        manifest = project / "swcraft.yaml"

        assert _run(monkeypatch, "generate", "arxml", "--project", str(manifest)) == 0
        (project / "dist" / "model.arxml").unlink()
        capsys.readouterr()
        code = _run(monkeypatch, "generate", "arxml", "--project", str(manifest), "--arxml-version", "4.2.2")

        assert code == 1
        assert "swc.call.async-without-result" in capsys.readouterr().err
        assert not (project / "dist" / "model.arxml").exists()


# The refusal matrix: what check-model refuses, generation refuses, nothing written


def _queued_implicit_read(model_root: Path) -> None:
    change(model_root, "interfaces/WheelSpeedIf.yaml", lambda d: d["data_elements"][0].update(sw_impl_policy="queued"))

    def queue(document: dict[str, Any]) -> None:
        (port,) = [port for port in document["ports"] if port["name"] == "rWheelSpeed"]
        port["com_specs"] = [{"data_element": "speed", "queue_length": 1}]

    _controller(model_root, queue)


def _enable_update(model_root: Path) -> None:
    def mutate(document: dict[str, Any]) -> None:
        (port,) = [port for port in document["ports"] if port["name"] == "rWheelSpeed"]
        port["com_specs"] = [{"data_element": "speed", "enable_update": True}]

    _controller(model_root, mutate)


def _add_call(runnable: str, call: dict[str, Any]) -> Any:
    return lambda model_root: _controller(
        model_root, lambda d: _runnable(d, runnable).setdefault("calls", []).append(call)
    )


def _add_runnable(name: str, *events: dict[str, Any], calls: list[dict[str, Any]] | None = None) -> Any:
    runnable: dict[str, Any] = {"name": name, "events": list(events), **({"calls": calls} if calls else {})}
    return lambda model_root: _controller(model_root, lambda d: d["runnables"].append(runnable))


def _stated_poll_result(result: str) -> Any:
    return lambda model_root: _controller(
        model_root, lambda d: _call(d, COMPUTE, "rCalibration", "ReadBlock").update(result=result)
    )


LONG_PORT = "p" * 60
LONG_OPERATION = "o" * 61

REFUSALS: dict[str, tuple[Any, str]] = {
    "queued-implicit-read": (_queued_implicit_read, "rule:swc.access.queued-implicit-read:"),
    "implicit-read-enable-update": (_enable_update, "rule:swc.access.implicit-read-enable-update:"),
    "mixed-synchrony": (
        _add_call(DIAGNOSE, {"port": "rNvStorage", "operation": "WriteBlock"}),
        "rule:swc.call.mixed-synchrony:",
    ),
    "timeout-mismatch": (
        _add_call(
            DIAGNOSE,
            {"port": "rCalibration", "operation": "ReadBlock", "asynchronous": True, "poll": True, "timeout": 0.2},
        ),
        "rule:swc.call.timeout-mismatch:",
    ),
    "several-results": (
        _add_runnable("OnCalibrationRead", _returned("rCalibration", "ReadBlock")),
        "rule:swc.call.several-results:",
    ),
    "async-without-result": (_drop_the_event, "rule:swc.call.async-without-result:"),
    "unknown-call": (
        _add_runnable("OnVersion", _returned("rCalibration", "GetVersion")),
        "rule:swc.call-returned-event.unknown-call:",
    ),
    "ambiguous-call": (
        _add_call(DIAGNOSE, {"port": "rNvStorage", "operation": "WriteBlock", "asynchronous": True}),
        "rule:swc.call-returned-event.ambiguous-call:",
    ),
    "init-runnable-result": (
        lambda model_root: _controller(
            model_root, lambda d: _runnable(d, ON_WRITTEN)["events"].append({"type": "init"})
        ),
        "rule:swc.call.init-runnable-result:",
    ),
    "stated-result-not-an-identifier": (_stated_poll_result("1Fetched"), "stated"),
    "stated-result-collides-with-a-call-point": (
        _stated_poll_result("Call_rNvStorage_WriteBlock"),
        "Call_rNvStorage_WriteBlock",
    ),
    "derived-result-over-budget": (
        _add_runnable(
            "PollLong",
            {"type": "timing", "period": 0.1},
            calls=[{"port": LONG_PORT, "operation": LONG_OPERATION, "asynchronous": True, "poll": True}],
        ),
        f"Result_{LONG_PORT}_{LONG_OPERATION}",
    ),
}


def _long_pair(model_root: Path) -> None:
    """A require port and an interface whose names together leave ``Result_`` 129 characters (budget 120)."""
    change(model_root, "interfaces/CalibrationIf.yaml", lambda d: d["operations"].append({"name": LONG_OPERATION}))
    _controller(
        model_root,
        lambda d: d["ports"].append({"name": LONG_PORT, "direction": "require", "interface": "CalibrationIf"}),
    )


#: The rule ``check-model`` reports for each of the three name cases, whose generation refusal names no rule.
NAME_CASE_RULES = {
    "stated-result-not-an-identifier": "identifier.declared",
    "stated-result-collides-with-a-call-point": "swc.access.name-collision",
    "derived-result-over-budget": "identifier.generated",
}


class TestTheRefusalMatrix:
    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize("case", sorted(REFUSALS))
    def test_generation_refuses_and_leaves_a_fresh_directory_empty(
        self, model: Path, tmp_path: Path, case: str, version: str
    ) -> None:
        """Every case at both editions but ``async-without-result``, which 4.3.0 allows (``[constr_2006]``)."""
        if case == "async-without-result" and version == "4.3.0":
            pytest.skip("R4.3.0 allows an asynchronous call point with no result point")
        mutate, said = REFUSALS[case]
        if case == "derived-result-over-budget":
            _long_pair(model)
        mutate(model)
        out = tmp_path / "fresh"
        out.mkdir()

        with pytest.raises(ValueError, match=re.escape(said)):
            generate_arxml(str(model), str(out / "model.arxml"), version=version)

        assert not any(out.iterdir())
        result = check_cross_layer_consistency(str(model), version)
        expected = NAME_CASE_RULES.get(case) or said.split(":")[1]
        assert expected in {record.rule for record in result.records}, (case, [r.rule for r in result.records])

    def test_the_matrix_covers_the_nine_rules(self) -> None:
        rules = {said.split(":")[1] for _mutate, said in REFUSALS.values() if said.startswith("rule:")}
        assert rules == {
            "swc.access.queued-implicit-read",
            "swc.access.implicit-read-enable-update",
            "swc.call.mixed-synchrony",
            "swc.call.timeout-mismatch",
            "swc.call.several-results",
            "swc.call.async-without-result",
            "swc.call-returned-event.unknown-call",
            "swc.call-returned-event.ambiguous-call",
            "swc.call.init-runnable-result",
        }


# A model using none of the forms


class TestAModelWithoutTheForms:
    def test_none_of_the_new_elements_is_written(self, model: Path, tmp_path: Path) -> None:
        def explicit(document: dict[str, Any]) -> None:
            for runnable in document.get("runnables", []):
                for entry in [*runnable.get("reads", []), *runnable.get("writes", [])]:
                    if entry.get("by") == "implicit":
                        entry.pop("by")
                for call in runnable.get("calls", []):
                    for key in ("asynchronous", "poll", "result", "timeout"):
                        call.pop(key, None)
                runnable["events"] = [
                    event for event in runnable.get("events", []) if event["type"] != "call_returned"
                ] or [{"type": "timing", "period": 0.1}]

        for component in ("BrakeController", "WheelSensor", "BrakeActuator", "BrakePedal"):
            change(model, f"swc/chassis/{component}.yaml", explicit)
        assert check_cross_layer_consistency(str(model)).consistent

        for version in VERSIONS:
            text = etree.tostring(_generate(model, tmp_path, version), encoding="unicode")
            assert not [tag for tag in NEW_TAGS if f"<{tag}>" in text or f"<{tag} " in text], version
            assert "<SYNCHRONOUS-SERVER-CALL-POINT>" in text
