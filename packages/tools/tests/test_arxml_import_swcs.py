"""Tests for `arxml_import.swc_mapper.map_swcs`: the SWC mapping and its skip+report, chained-reason policy.

The mapping covers ports, runnables, events and access points. Fixture cases run the type -> interface -> SWC pipeline
over `minimal_swc.arxml`, `client_server.arxml` (an OPERATION-INVOKED-EVENT on a require port, skipped) and
`unsupported_content.arxml` (a PR-PORT-PROTOTYPE); error cases build small inline `ImportModel`s.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import ACCESS_POINTS_MODEL_ROOT
from swcraft_tools.arxml_import import ImportedSwc, map_data_types, map_interfaces, map_swcs, run_import
from swcraft_tools.arxml_import.merge import _com_spec_value
from swcraft_tools.arxml_reader import (
    ImplDataTypeInfo,
    ImportModel,
    SkipCategory,
    SRDataElementInfo,
    SRInterfaceInfo,
    SwBaseTypeInfo,
    SwcAccessInfo,
    SwcEventInfo,
    SwcInfo,
    SwcPortInfo,
    SwcRunnableInfo,
    read_arxml,
)
from swcraft_tools.cli import main
from swcraft_tools.schema import DataElement, InitEvent, SRInterface, TimingEvent
from swcraft_tools.writer import generate_arxml
from swcraft_tools.writer.yaml_documents import swc_to_dict

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "arxml"
MINIMAL_SWC = FIXTURES_DIR / "minimal_swc.arxml"
CLIENT_SERVER = FIXTURES_DIR / "client_server.arxml"
UNSUPPORTED_CONTENT = FIXTURES_DIR / "unsupported_content.arxml"


def _by_swc_name(items: list[ImportedSwc], name: str) -> ImportedSwc:
    matches = [item for item in items if item.swc.name == name]
    assert len(matches) == 1, f"expected exactly one SWC named {name!r}, found {len(matches)}"
    return matches[0]


def _by_port_name(swc, name: str):
    matches = [p for p in swc.ports if p.name == name]
    assert len(matches) == 1, f"expected exactly one port named {name!r}, found {len(matches)}"
    return matches[0]


def _skip_reason(skip_log, path) -> str | None:
    for entry in skip_log:
        if entry.path == path:
            return entry.reason
    return None


def _map_full(model: ImportModel):
    """Map the types, the interfaces and the SWCs of one ImportModel."""
    forms: list = []
    data_types, registry, type_skip_log = map_data_types(model, elements=forms)
    interfaces, iface_skip_log = map_interfaces(model, registry, data_types, elements=forms)
    swcs, swc_skip_log = map_swcs(model, interfaces, iface_skip_log=iface_skip_log, port_inits={}, variable_types={})
    return data_types, interfaces, swcs, swc_skip_log


# Fixture-driven success case: minimal_swc.arxml


@pytest.fixture(scope="module")
def minimal_swc_mapped():
    model = read_arxml(str(MINIMAL_SWC))
    return _map_full(model)


def test_swc_group_derived_from_parent_package(minimal_swc_mapped):
    _data_types, _interfaces, swcs, swc_skip_log = minimal_swc_mapped
    # The fixture's one port API option requests nothing, so it is a descriptive line, and the only one.
    assert [(entry.tag, entry.effect, entry.reason.split(":")[0]) for entry in swc_skip_log] == [
        ("PORT-API-OPTION", "report", "PORT-API-OPTION states both of its features off")
    ]

    imported = _by_swc_name(swcs, "WheelSpeedSensorSwc")
    # /Acme/Chassis/Components/WheelSpeedSensorSwc -> immediate parent is Components.
    assert imported.group == "Components"


def test_swc_description_is_preserved(minimal_swc_mapped):
    _data_types, _interfaces, swcs, _swc_skip_log = minimal_swc_mapped
    swc = _by_swc_name(swcs, "WheelSpeedSensorSwc").swc
    assert swc.description == "Wheel speed sensor application component (anonymized fixture)."


def test_ports_mapped_to_provide_require_with_resolved_interface_name(minimal_swc_mapped):
    _data_types, interfaces, swcs, _swc_skip_log = minimal_swc_mapped
    assert any(iface.name == "IWheelSpeed" for iface in interfaces)

    swc = _by_swc_name(swcs, "WheelSpeedSensorSwc").swc
    assert len(swc.ports) == 2

    p_port = _by_port_name(swc, "pWheelSpeed")
    assert p_port.direction == "provide"
    assert p_port.interface == "IWheelSpeed"
    assert p_port.description == "Provides the current wheel speed reading."

    r_port = _by_port_name(swc, "rWheelSpeedEcho")
    assert r_port.direction == "require"
    assert r_port.interface == "IWheelSpeed"
    assert r_port.description == "Requires a loop-back echo of the wheel speed value."


def test_runnable_with_timing_event_period_copied_as_seconds_no_conversion(minimal_swc_mapped):
    """PERIOD and MINIMUM-START-INTERVAL are float seconds in ARXML, as the writer emits them: no conversion."""
    _data_types, _interfaces, swcs, _swc_skip_log = minimal_swc_mapped
    swc = _by_swc_name(swcs, "WheelSpeedSensorSwc").swc
    assert len(swc.runnables) == 1

    runnable = swc.runnables[0]
    assert runnable.name == "WheelSpeedSensorRun"
    assert runnable.min_start_interval == pytest.approx(0.01)
    assert len(runnable.events) == 1
    event = runnable.events[0]
    assert isinstance(event, TimingEvent)
    assert event.period == pytest.approx(0.01)


# Fixture-driven skip case: OPERATION-INVOKED-EVENT (client_server.arxml)


@pytest.fixture(scope="module")
def client_server_mapped():
    model = read_arxml(str(CLIENT_SERVER))
    return _map_full(model)


def test_operation_invoked_event_is_skipped_with_reason_swc_kept(client_server_mapped):
    _data_types, interfaces, swcs, swc_skip_log = client_server_mapped
    assert any(iface.name == "IBrakeControlService" for iface in interfaces)

    imported = _by_swc_name(swcs, "BrakeControllerSwc")
    swc = imported.swc
    # SWC and its port/runnable are kept -- only the event is lost.
    assert len(swc.ports) == 1
    assert swc.ports[0].interface == "IBrakeControlService"
    assert len(swc.runnables) == 1
    runnable = swc.runnables[0]
    assert runnable.name == "HandleSetBrakeMode"
    assert runnable.min_start_interval == pytest.approx(0.0)
    assert runnable.events == []

    reason = _skip_reason(
        swc_skip_log,
        "/Acme/Chassis/Components/BrakeControllerSwc/BrakeControllerSwc_InternalBehavior/OIE_SetBrakeMode",
    )
    # The fixture's IREF spells a CONTEXT-R-PORT-REF, which the reader does not read, so the event is skipped
    # for its reference.
    assert reason == "operation event has no CONTEXT-P-PORT-REF in an OPERATION-IREF"


# Fixture-driven skip case: PR-PORT-PROTOTYPE (unsupported_content.arxml)


@pytest.fixture(scope="module")
def unsupported_content_mapped():
    model = read_arxml(str(UNSUPPORTED_CONTENT))
    return _map_full(model)


def test_pr_port_is_skipped_with_reason_swc_kept(unsupported_content_mapped):
    _data_types, _interfaces, swcs, swc_skip_log = unsupported_content_mapped

    imported = _by_swc_name(swcs, "DiagnosticBridgeSwc")
    swc = imported.swc
    assert swc.ports == []
    assert imported.group == "Components"

    reason = _skip_reason(swc_skip_log, "/Acme/Chassis/Components/DiagnosticBridgeSwc/prDiagnosticChannel")
    assert reason is not None
    assert "pr" in reason.lower()


# Error scenario: duplicate SWC name


def _minimal_swc_info(path: str, name: str) -> SwcInfo:
    return SwcInfo(path=path, name=name, description="", ports=[], runnables=[], events=[])


def test_duplicate_swc_name_second_skipped_first_kept():
    model = ImportModel(source_path="inline-test")
    model.swcs.append(_minimal_swc_info("/Pkg/Components/FirstDecl/DoorCtrlSwc", "DoorCtrlSwc"))
    model.swcs.append(_minimal_swc_info("/Pkg/Components/SecondDecl/DoorCtrlSwc", "DoorCtrlSwc"))

    swcs, swc_skip_log = map_swcs(model, interfaces=[], port_inits={}, variable_types={})

    assert len(swcs) == 1
    assert swcs[0].swc.name == "DoorCtrlSwc"
    # First declaration wins.
    assert swcs[0].group == "FirstDecl"

    reason = _skip_reason(swc_skip_log, "/Pkg/Components/SecondDecl/DoorCtrlSwc")
    assert reason is not None
    assert "duplicate" in reason.lower()
    assert "DoorCtrlSwc" in reason


# `group` comes from the raw parent AR-PACKAGE SHORT-NAME, and `run_import()` writes to
# `root / "swc" / imported.group` without `check_path_segment`, so `map_swcs` must skip a ".." group.


def test_swc_with_path_traversal_group_is_skipped_not_written_outside_root():
    model = ImportModel(source_path="inline-test")
    # Immediate parent package SHORT-NAME is literally ".." -> group == "..".
    model.swcs.append(_minimal_swc_info("/Pkg/../EvilSwc", "EvilSwc"))
    model.swcs.append(_minimal_swc_info("/Pkg/Components/GoodSwc", "GoodSwc"))

    swcs, swc_skip_log = map_swcs(model, interfaces=[], port_inits={}, variable_types={})

    assert [s.swc.name for s in swcs] == ["GoodSwc"]
    reason = _skip_reason(swc_skip_log, "/Pkg/../EvilSwc")
    assert reason is not None
    assert "escape" in reason.lower()
    assert ".." in reason


@pytest.mark.parametrize(
    "raw_path",
    [
        "/Pkg/../EvilSwc",  # parent package short-name is exactly ".."
        "/Pkg/a..b/EvilSwc",  # parent contains ".." as a substring
    ],
)
def test_swc_with_various_traversal_shaped_groups_all_skipped(raw_path):
    model = ImportModel(source_path="inline-test")
    model.swcs.append(_minimal_swc_info(raw_path, "EvilSwc"))

    swcs, swc_skip_log = map_swcs(model, interfaces=[], port_inits={}, variable_types={})

    assert swcs == []
    assert len(swc_skip_log) == 1


# Error scenario: a port with a blank SHORT-NAME (`Port.name` has no non-blank validator, so it would crash
# `map_compositions()` later) is skip+reported.


def test_blank_port_name_is_skipped_with_reason_swc_kept():
    interfaces = [
        SRInterface(
            name="ISpeed",
            kind="sender_receiver",
            data_elements=[DataElement(name="x", implementation_type="uint8_impl")],
        )
    ]
    swc_info = SwcInfo(
        path="/Pkg/Components/SpeedSwc",
        name="SpeedSwc",
        description="",
        ports=[
            SwcPortInfo(
                path="/Pkg/Components/SpeedSwc/",
                name="",
                kind="P-PORT-PROTOTYPE",
                interface_ref="/Pkg/Interfaces/ISpeed",
                description="blank SHORT-NAME on this port",
            ),
            SwcPortInfo(
                path="/Pkg/Components/SpeedSwc/pSpeedOk",
                name="pSpeedOk",
                kind="P-PORT-PROTOTYPE",
                interface_ref="/Pkg/Interfaces/ISpeed",
                description="",
            ),
        ],
        runnables=[],
        events=[],
    )
    model = ImportModel(source_path="inline-test")
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/Pkg/Interfaces/ISpeed",
            name="ISpeed",
            description="",
            data_elements=[SRDataElementInfo(name="x", type_ref="/Pkg/Types/uint8_impl")],
        )
    )
    model.swcs.append(swc_info)

    swcs, swc_skip_log = map_swcs(model, interfaces, port_inits={}, variable_types={})

    swc = swcs[0].swc
    # The blank-named port is dropped; the well-formed sibling port survives.
    assert len(swc.ports) == 1
    assert swc.ports[0].name == "pSpeedOk"

    reason = _skip_reason(swc_skip_log, "/Pkg/Components/SpeedSwc/")
    assert reason is not None
    assert "blank" in reason.lower()


# Error scenario: duplicate port name within one SWC


def test_duplicate_port_name_within_one_swc_second_skipped_first_kept():
    interfaces = [
        SRInterface(
            name="ISpeed",
            kind="sender_receiver",
            data_elements=[DataElement(name="x", implementation_type="uint8_impl")],
        )
    ]
    swc_info = SwcInfo(
        path="/Pkg/Components/SpeedSwc",
        name="SpeedSwc",
        description="",
        ports=[
            SwcPortInfo(
                path="/Pkg/Components/SpeedSwc/pSpeed",
                name="pSpeed",
                kind="P-PORT-PROTOTYPE",
                interface_ref="/Pkg/Interfaces/ISpeed",
                description="first declaration",
            ),
            SwcPortInfo(
                path="/Pkg/Components/SpeedSwc/pSpeed",
                name="pSpeed",
                kind="P-PORT-PROTOTYPE",
                interface_ref="/Pkg/Interfaces/ISpeed",
                description="second declaration (malformed duplicate)",
            ),
        ],
        runnables=[],
        events=[],
    )
    model = ImportModel(source_path="inline-test")
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/Pkg/Interfaces/ISpeed",
            name="ISpeed",
            description="",
            data_elements=[SRDataElementInfo(name="x", type_ref="/Pkg/Types/uint8_impl")],
        )
    )
    model.swcs.append(swc_info)

    swcs, swc_skip_log = map_swcs(model, interfaces, port_inits={}, variable_types={})

    swc = swcs[0].swc
    assert len(swc.ports) == 1
    assert swc.ports[0].description == "first declaration"

    reasons = [e.reason for e in swc_skip_log if e.path == "/Pkg/Components/SpeedSwc/pSpeed"]
    assert len(reasons) == 1
    assert "duplicate" in reasons[0].lower()


@pytest.mark.parametrize(
    ("first_kind", "first_interface_ref", "first_reason_fragment"),
    [
        ("PR-PORT-PROTOTYPE", "/Pkg/Interfaces/ISpeed", "PR (combined provide+require"),
        ("P-PORT-PROTOTYPE", "/Pkg/Interfaces/IGone", "unresolved reference /Pkg/Interfaces/IGone"),
    ],
    ids=["a PR port first", "an unresolvable interface first"],
)
def test_a_skipped_port_claims_its_name_so_a_later_same_named_port_is_the_duplicate(
    first_kind: str, first_interface_ref: str, first_reason_fragment: str
) -> None:
    """A skipped port claims its name, so a later same-named port is the duplicate.

    The path index keeps the first of two same-named ports, so an access on that PORT-REF chain-skips
    against the port that claims the path."""
    interfaces = [
        SRInterface(
            name="ISpeed",
            kind="sender_receiver",
            data_elements=[DataElement(name="x", implementation_type="uint8_impl")],
        )
    ]
    port_path = "/Pkg/Components/SpeedSwc/pSpeed"
    swc_info = SwcInfo(
        path="/Pkg/Components/SpeedSwc",
        name="SpeedSwc",
        description="",
        ports=[
            SwcPortInfo(
                path=port_path, name="pSpeed", kind=first_kind, interface_ref=first_interface_ref, description=""
            ),
            SwcPortInfo(
                path=port_path,
                name="pSpeed",
                kind="P-PORT-PROTOTYPE",
                interface_ref="/Pkg/Interfaces/ISpeed",
                description="second declaration (malformed duplicate)",
            ),
        ],
        runnables=[
            SwcRunnableInfo(
                path="/Pkg/Components/SpeedSwc/Behavior/Run",
                name="Run",
                min_start_interval=0.0,
                description="",
                accesses=[
                    SwcAccessInfo(
                        path="/Pkg/Components/SpeedSwc/Behavior/Run/W_pSpeed_x",
                        name="W_pSpeed_x",
                        kind="DATA-SEND-POINTS",
                        tag="VARIABLE-ACCESS",
                        port_ref=port_path,
                        target_ref="/Pkg/Interfaces/ISpeed/x",
                    )
                ],
            )
        ],
        events=[],
    )
    model = ImportModel(source_path="inline-test")
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/Pkg/Interfaces/ISpeed",
            name="ISpeed",
            description="",
            data_elements=[SRDataElementInfo(name="x", type_ref="/Pkg/Types/uint8_impl")],
        )
    )
    model.swcs.append(swc_info)

    swcs, swc_skip_log = map_swcs(model, interfaces, port_inits={}, variable_types={})

    swc = swcs[0].swc
    assert swc.ports == [], "the later same-named port is the duplicate: nothing lands under the name"
    assert swc.runnables[0].writes == []
    port_reasons = [e.reason for e in swc_skip_log if e.path == port_path]
    assert len(port_reasons) == 2
    assert first_reason_fragment in port_reasons[0]
    assert "duplicate port name 'pSpeed'" in port_reasons[1]
    [access_reason] = [e.reason for e in swc_skip_log if e.path.endswith("/W_pSpeed_x")]
    assert access_reason == f"chain-skipped: port 'pSpeed' was skipped ({port_reasons[0]})"


# Error scenario: event references a runnable missing from the SWC
# (dangling/malformed START-ON-EVENT-REF) -- skip that event, not a crash.


def test_event_referencing_missing_runnable_is_skipped_not_crashed():
    swc_info = SwcInfo(
        path="/Pkg/Components/PedalSwc",
        name="PedalSwc",
        description="",
        ports=[],
        runnables=[
            SwcRunnableInfo(
                path="/Pkg/Components/PedalSwc/Behavior/PedalRun",
                name="PedalRun",
                min_start_interval=0.02,
                description="",
            )
        ],
        events=[
            SwcEventInfo(
                path="/Pkg/Components/PedalSwc/Behavior/TMT_Dangling",
                name="TMT_Dangling",
                kind="TIMING-EVENT",
                runnable_ref="/Pkg/Components/PedalSwc/Behavior/NoSuchRunnable",
                period=0.02,
            ),
            SwcEventInfo(
                path="/Pkg/Components/PedalSwc/Behavior/TMT_PedalRun",
                name="TMT_PedalRun",
                kind="TIMING-EVENT",
                runnable_ref="/Pkg/Components/PedalSwc/Behavior/PedalRun",
                period=0.02,
            ),
        ],
    )
    model = ImportModel(source_path="inline-test")
    model.swcs.append(swc_info)

    swcs, swc_skip_log = map_swcs(model, interfaces=[], port_inits={}, variable_types={})

    swc = swcs[0].swc
    assert len(swc.runnables) == 1
    runnable = swc.runnables[0]
    # The dangling event is dropped; the well-formed one for the same
    # runnable still attaches correctly.
    assert len(runnable.events) == 1
    assert isinstance(runnable.events[0], TimingEvent)
    assert runnable.events[0].period == pytest.approx(0.02)

    reason = _skip_reason(swc_skip_log, "/Pkg/Components/PedalSwc/Behavior/TMT_Dangling")
    assert reason is not None
    assert "unresolved reference" in reason.lower() or "not found" in reason.lower()


def test_init_event_maps_to_init_event():
    swc_info = SwcInfo(
        path="/Pkg/Components/InitSwc",
        name="InitSwc",
        description="",
        ports=[],
        runnables=[
            SwcRunnableInfo(
                path="/Pkg/Components/InitSwc/Behavior/InitRun",
                name="InitRun",
                min_start_interval=0.0,
                description="",
            )
        ],
        events=[
            SwcEventInfo(
                path="/Pkg/Components/InitSwc/Behavior/IE_InitRun",
                name="IE_InitRun",
                kind="INIT-EVENT",
                runnable_ref="/Pkg/Components/InitSwc/Behavior/InitRun",
                period=None,
            )
        ],
    )
    model = ImportModel(source_path="inline-test")
    model.swcs.append(swc_info)

    swcs, swc_skip_log = map_swcs(model, interfaces=[], port_inits={}, variable_types={})

    assert len(swc_skip_log) == 0
    runnable = swcs[0].swc.runnables[0]
    assert len(runnable.events) == 1
    assert isinstance(runnable.events[0], InitEvent)


# Error scenario: port references an interface map_interfaces skipped --
# chained reason, port skipped, SWC kept.


def test_port_referencing_a_skipped_interface_chains_reason_swc_kept():
    """A port on a skipped interface is dropped with the reason chained, and the SWC is kept."""
    model = ImportModel(source_path="inline-test")
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/Pkg/Interfaces/IStatus",
            name="IStatus",
            description="",
            data_elements=[SRDataElementInfo(name="status", type_ref="/Pkg/AppTypes/DoesNotExist")],
        )
    )
    model.swcs.append(
        SwcInfo(
            path="/Pkg/Components/StatusSwc",
            name="StatusSwc",
            description="",
            ports=[
                SwcPortInfo(
                    path="/Pkg/Components/StatusSwc/pStatus",
                    name="pStatus",
                    kind="P-PORT-PROTOTYPE",
                    interface_ref="/Pkg/Interfaces/IStatus",
                    description="",
                )
            ],
            runnables=[],
            events=[],
        )
    )

    forms: list = []
    data_types, registry, _type_skip_log = map_data_types(model, elements=forms)
    interfaces, iface_skip_log = map_interfaces(model, registry, data_types, elements=forms)
    assert interfaces == []  # IStatus was skipped wholesale (unresolved reference)
    assert _skip_reason(iface_skip_log, "/Pkg/Interfaces/IStatus") is not None

    swcs, swc_skip_log = map_swcs(model, interfaces, iface_skip_log=iface_skip_log, port_inits={}, variable_types={})

    swc = swcs[0].swc
    assert swc.ports == []  # SWC kept, but the port is gone
    reason = _skip_reason(swc_skip_log, "/Pkg/Components/StatusSwc/pStatus")
    assert reason is not None
    assert "unresolved reference" in reason.lower()
    assert "IStatus" in reason


# `build_interface_path_index` finds the surviving declaration by its absence from map_interfaces' skip log:
# an earlier declaration skipped for its content does not claim the name.


def test_port_resolves_to_the_actually_surviving_interface_declaration_not_first_by_order():
    """A port naming the surviving one of two same-named interfaces resolves; one naming the skipped does not."""
    model = ImportModel(source_path="inline-test")
    model.sw_base_types.append(
        SwBaseTypeInfo(
            path="/Pkg/Types/uint8",
            name="uint8",
            size=8,
            encoding="NONE",
            native_declaration="uint8",
            description="",
        )
    )
    model.impl_data_types.append(
        ImplDataTypeInfo(
            path="/Pkg/Types/Flag_impl",
            name="Flag_impl",
            category="VALUE",
            base_type_ref="/Pkg/Types/uint8",
            impl_type_ref=None,
            description="",
        )
    )
    # FIRST in list order -- will be skipped (unresolved reference).
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/Pkg/InterfacesA/IStatus",
            name="IStatus",
            description="",
            data_elements=[SRDataElementInfo(name="status", type_ref="/Pkg/AppTypes/DoesNotExist")],
        )
    )
    # SECOND in list order, same bare name -- will succeed (primitive element).
    model.sr_interfaces.append(
        SRInterfaceInfo(
            path="/Pkg/InterfacesB/IStatus",
            name="IStatus",
            description="",
            data_elements=[SRDataElementInfo(name="flag", type_ref="/Pkg/Types/Flag_impl")],
        )
    )
    model.swcs.append(
        SwcInfo(
            path="/Pkg/Components/StatusSwc",
            name="StatusSwc",
            description="",
            ports=[
                SwcPortInfo(
                    path="/Pkg/Components/StatusSwc/pGood",
                    name="pGood",
                    kind="P-PORT-PROTOTYPE",
                    interface_ref="/Pkg/InterfacesB/IStatus",  # the SURVIVING path
                    description="",
                ),
                SwcPortInfo(
                    path="/Pkg/Components/StatusSwc/pBad",
                    name="pBad",
                    kind="P-PORT-PROTOTYPE",
                    interface_ref="/Pkg/InterfacesA/IStatus",  # the SKIPPED path
                    description="",
                ),
            ],
            runnables=[],
            events=[],
        )
    )

    forms: list = []
    data_types, registry, _type_skip_log = map_data_types(model, elements=forms)
    interfaces, iface_skip_log = map_interfaces(model, registry, data_types, elements=forms)
    assert len(interfaces) == 1
    assert interfaces[0].name == "IStatus"
    assert _skip_reason(iface_skip_log, "/Pkg/InterfacesA/IStatus") is not None
    assert _skip_reason(iface_skip_log, "/Pkg/InterfacesB/IStatus") is None

    swcs, swc_skip_log = map_swcs(model, interfaces, iface_skip_log=iface_skip_log, port_inits={}, variable_types={})
    swc = swcs[0].swc

    kept_ports = {p.name for p in swc.ports}
    assert "pGood" in kept_ports
    assert swc_skip_log is not None
    for entry in swc_skip_log:
        assert entry.path != "/Pkg/Components/StatusSwc/pGood"

    # Resolving against the surviving declaration would be a silent wrong wiring.
    assert "pBad" not in kept_ports
    bad_reason = _skip_reason(swc_skip_log, "/Pkg/Components/StatusSwc/pBad")
    assert bad_reason is not None
    assert "unresolved reference" in bad_reason.lower()


# A non-positive TIMING-EVENT PERIOD is skip+reported: `TimingEvent`'s ValidationError would abort the import.


@pytest.mark.parametrize("bad_period", [0.0, -0.01])
def test_timing_event_non_positive_period_is_skipped_not_crashed(bad_period):
    swc_info = SwcInfo(
        path="/Pkg/Components/BadPeriodSwc",
        name="BadPeriodSwc",
        description="",
        ports=[],
        runnables=[
            SwcRunnableInfo(
                path="/Pkg/Components/BadPeriodSwc/Behavior/Run1",
                name="Run1",
                min_start_interval=0.0,
                description="",
            )
        ],
        events=[
            SwcEventInfo(
                path="/Pkg/Components/BadPeriodSwc/Behavior/TMT_Run1",
                name="TMT_Run1",
                kind="TIMING-EVENT",
                runnable_ref="/Pkg/Components/BadPeriodSwc/Behavior/Run1",
                period=bad_period,
            )
        ],
    )
    model = ImportModel(source_path="inline-test")
    model.swcs.append(swc_info)

    # Must not raise pydantic.ValidationError.
    swcs, swc_skip_log = map_swcs(model, interfaces=[], port_inits={}, variable_types={})

    runnable = swcs[0].swc.runnables[0]
    assert runnable.events == []  # the bad timing event was dropped, not crashed on
    reason = _skip_reason(swc_skip_log, "/Pkg/Components/BadPeriodSwc/Behavior/TMT_Run1")
    assert reason is not None
    assert "period" in reason.lower()


# A blank SHORT-NAME on a raw SWC, which `SWComponent.name` rejects, is skip+reported.


def test_swc_with_blank_name_is_skipped_not_crashed():
    model = ImportModel(source_path="inline-test")
    model.swcs.append(
        SwcInfo(
            path="/Pkg/Components/",
            name="   ",
            description="",
            ports=[],
            runnables=[],
            events=[],
        )
    )
    model.swcs.append(
        SwcInfo(
            path="/Pkg/Components/GoodSwc",
            name="GoodSwc",
            description="",
            ports=[],
            runnables=[],
            events=[],
        )
    )

    swcs, swc_skip_log = map_swcs(model, interfaces=[], port_inits={}, variable_types={})

    assert [s.swc.name for s in swcs] == ["GoodSwc"]
    reason = _skip_reason(swc_skip_log, "/Pkg/Components/")
    assert reason is not None
    assert "blank" in reason.lower() or "missing" in reason.lower()


# A duplicate runnable name within one SWC is skip+reported and the first kept.


def test_duplicate_runnable_name_second_skipped_first_kept():
    swc_info = SwcInfo(
        path="/Pkg/Components/DupRunSwc",
        name="DupRunSwc",
        description="",
        ports=[],
        runnables=[
            SwcRunnableInfo(
                path="/Pkg/Components/DupRunSwc/Behavior/Run1",
                name="Run1",
                min_start_interval=0.01,
                description="first",
            ),
            SwcRunnableInfo(
                path="/Pkg/Components/DupRunSwc/Behavior2/Run1",
                name="Run1",
                min_start_interval=0.02,
                description="second, duplicate name",
            ),
        ],
        events=[],
    )
    model = ImportModel(source_path="inline-test")
    model.swcs.append(swc_info)

    swcs, swc_skip_log = map_swcs(model, interfaces=[], port_inits={}, variable_types={})

    swc = swcs[0].swc
    assert len(swc.runnables) == 1
    assert swc.runnables[0].min_start_interval == pytest.approx(0.01)  # first one kept

    reason = _skip_reason(swc_skip_log, "/Pkg/Components/DupRunSwc/Behavior2/Run1")
    assert reason is not None
    assert "duplicate runnable name" in reason.lower()


# A negative MIN-START-INTERVAL is skip+reported: `r.min_start_interval or 0.0` substitutes only falsy values.


def test_negative_min_start_interval_is_skipped_not_crashed():
    swc_info = SwcInfo(
        path="/Pkg/Components/BadIntervalSwc",
        name="BadIntervalSwc",
        description="",
        ports=[],
        runnables=[
            SwcRunnableInfo(
                path="/Pkg/Components/BadIntervalSwc/Behavior/Run1",
                name="Run1",
                min_start_interval=-5.0,
                description="",
            )
        ],
        events=[
            SwcEventInfo(
                path="/Pkg/Components/BadIntervalSwc/Behavior/TMT_Run1",
                name="TMT_Run1",
                kind="TIMING-EVENT",
                runnable_ref="/Pkg/Components/BadIntervalSwc/Behavior/Run1",
                period=0.01,
            )
        ],
    )
    model = ImportModel(source_path="inline-test")
    model.swcs.append(swc_info)

    # Must not raise pydantic.ValidationError.
    swcs, swc_skip_log = map_swcs(model, interfaces=[], port_inits={}, variable_types={})

    swc = swcs[0].swc
    assert swc.runnables == []  # the bad runnable was dropped, not crashed on

    reason = _skip_reason(swc_skip_log, "/Pkg/Components/BadIntervalSwc/Behavior/Run1")
    assert reason is not None
    assert "min-start-interval" in reason.lower()


# A NaN PERIOD/MIN-START-INTERVAL passes `<= 0`/`< 0` guards: it would crash the import for PERIOD or write
# `nan` for MIN-START-INTERVAL. Built via the raw dataclasses, past `parse_float_literal`'s NaN filter, to
# pin the guard in `map_swcs` itself.


def test_nan_period_is_skipped_not_crashed():
    swc_info = SwcInfo(
        path="/Pkg/Components/NanPeriodSwc",
        name="NanPeriodSwc",
        description="",
        ports=[],
        runnables=[
            SwcRunnableInfo(
                path="/Pkg/Components/NanPeriodSwc/Behavior/Run1",
                name="Run1",
                min_start_interval=0.0,
                description="",
            )
        ],
        events=[
            SwcEventInfo(
                path="/Pkg/Components/NanPeriodSwc/Behavior/TMT_Run1",
                name="TMT_Run1",
                kind="TIMING-EVENT",
                runnable_ref="/Pkg/Components/NanPeriodSwc/Behavior/Run1",
                period=float("nan"),
            )
        ],
    )
    model = ImportModel(source_path="inline-test")
    model.swcs.append(swc_info)

    # Must not raise pydantic.ValidationError.
    swcs, swc_skip_log = map_swcs(model, interfaces=[], port_inits={}, variable_types={})

    runnable = swcs[0].swc.runnables[0]
    assert runnable.events == []  # the NaN timing event was dropped, not crashed on
    reason = _skip_reason(swc_skip_log, "/Pkg/Components/NanPeriodSwc/Behavior/TMT_Run1")
    assert reason is not None
    assert "period" in reason.lower()


def test_nan_min_start_interval_is_skipped_not_silently_written():
    swc_info = SwcInfo(
        path="/Pkg/Components/NanIntervalSwc",
        name="NanIntervalSwc",
        description="",
        ports=[],
        runnables=[
            SwcRunnableInfo(
                path="/Pkg/Components/NanIntervalSwc/Behavior/Run1",
                name="Run1",
                min_start_interval=float("nan"),
                description="",
            )
        ],
        events=[],
    )
    model = ImportModel(source_path="inline-test")
    model.swcs.append(swc_info)

    # Must not raise pydantic.ValidationError, and must not silently write
    # `nan` into the emitted Runnable either.
    swcs, swc_skip_log = map_swcs(model, interfaces=[], port_inits={}, variable_types={})

    swc = swcs[0].swc
    assert swc.runnables == []  # the NaN runnable was dropped, not silently written

    reason = _skip_reason(swc_skip_log, "/Pkg/Components/NanIntervalSwc/Behavior/Run1")
    assert reason is not None
    assert "min-start-interval" in reason.lower()


# The runnable's access points (DATA-RECEIVE-POINT-BY-ARGUMENTS / -BY-VALUES / DATA-SEND-POINTS /
# SERVER-CALL-POINTS) land as `Runnable.reads` / `writes` / `calls` in the written bytes.
#
# Fragments are built inline: the shapes here are ones a clean fixture must not carry. Access SHORT-NAMEs
# follow a tool's `R_<port>_<element>` / `W_...` / `Call_...` convention; access names are not model content,
# so the bytes assertions check none reaches the file.

_ACCESS_PKG = "/Acme/Chassis"
_ACCESS_SWC_PATH = f"{_ACCESS_PKG}/Components/Control"
_ACCESS_BEHAVIOR_PATH = f"{_ACCESS_SWC_PATH}/Control_InternalBehavior"
_ACCESS_RUNNABLE_PATH = f"{_ACCESS_BEHAVIOR_PATH}/ControlRun"


def _variable_access(name: str, port: str, target: str, dest: str = "R-PORT-PROTOTYPE") -> str:
    return (
        "<VARIABLE-ACCESS>"
        f"<SHORT-NAME>{name}</SHORT-NAME>"
        "<ACCESSED-VARIABLE><AUTOSAR-VARIABLE-IREF>"
        f'<PORT-PROTOTYPE-REF DEST="{dest}">{_ACCESS_SWC_PATH}/{port}</PORT-PROTOTYPE-REF>'
        f'<TARGET-DATA-PROTOTYPE-REF DEST="VARIABLE-DATA-PROTOTYPE">{_ACCESS_PKG}/Interfaces/{target}'
        "</TARGET-DATA-PROTOTYPE-REF>"
        "</AUTOSAR-VARIABLE-IREF></ACCESSED-VARIABLE>"
        "</VARIABLE-ACCESS>"
    )


def _call_point(name: str, port: str, target: str, tag: str = "SYNCHRONOUS-SERVER-CALL-POINT") -> str:
    return (
        f"<{tag}>"
        f"<SHORT-NAME>{name}</SHORT-NAME>"
        "<OPERATION-IREF>"
        f'<CONTEXT-R-PORT-REF DEST="R-PORT-PROTOTYPE">{_ACCESS_SWC_PATH}/{port}</CONTEXT-R-PORT-REF>'
        f'<TARGET-REQUIRED-OPERATION-REF DEST="CLIENT-SERVER-OPERATION">{_ACCESS_PKG}/Interfaces/{target}'
        "</TARGET-REQUIRED-OPERATION-REF>"
        "</OPERATION-IREF>"
        "<TIMEOUT>0</TIMEOUT>"
        f"</{tag}>"
    )


def _variable_access_raw(name: str | None, port_ref: str | None, target_ref: str | None) -> str:
    """A VARIABLE-ACCESS of exactly the parts given, `None` omitting an element.

    It spells the malformed shapes `_variable_access` cannot.
    """
    short = f"<SHORT-NAME>{name}</SHORT-NAME>" if name is not None else ""
    port = (
        f'<PORT-PROTOTYPE-REF DEST="R-PORT-PROTOTYPE">{port_ref}</PORT-PROTOTYPE-REF>' if port_ref is not None else ""
    )
    target = (
        f'<TARGET-DATA-PROTOTYPE-REF DEST="VARIABLE-DATA-PROTOTYPE">{target_ref}</TARGET-DATA-PROTOTYPE-REF>'
        if target_ref is not None
        else ""
    )
    iref = f"<ACCESSED-VARIABLE><AUTOSAR-VARIABLE-IREF>{port}{target}</AUTOSAR-VARIABLE-IREF></ACCESSED-VARIABLE>"
    return f"<VARIABLE-ACCESS>{short}{iref if port or target else ''}</VARIABLE-ACCESS>"


def _call_point_raw(name: str, port_ref: str | None, target_ref: str | None) -> str:
    """`_variable_access_raw`'s counterpart for a SYNCHRONOUS-SERVER-CALL-POINT."""
    port = (
        f'<CONTEXT-R-PORT-REF DEST="R-PORT-PROTOTYPE">{port_ref}</CONTEXT-R-PORT-REF>' if port_ref is not None else ""
    )
    target = (
        f'<TARGET-REQUIRED-OPERATION-REF DEST="CLIENT-SERVER-OPERATION">{target_ref}</TARGET-REQUIRED-OPERATION-REF>'
        if target_ref is not None
        else ""
    )
    return (
        f"<SYNCHRONOUS-SERVER-CALL-POINT><SHORT-NAME>{name}</SHORT-NAME>"
        f"<OPERATION-IREF>{port}{target}</OPERATION-IREF><TIMEOUT>0</TIMEOUT></SYNCHRONOUS-SERVER-CALL-POINT>"
    )


def _sr_interface(name: str, elements: list[str], type_name: str = "Speed_impl") -> str:
    prototypes = "".join(
        "<VARIABLE-DATA-PROTOTYPE>"
        f"<SHORT-NAME>{el}</SHORT-NAME>"
        f'<TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">{_ACCESS_PKG}/DataTypes/{type_name}</TYPE-TREF>'
        "</VARIABLE-DATA-PROTOTYPE>"
        for el in elements
    )
    return (
        "<SENDER-RECEIVER-INTERFACE>"
        f"<SHORT-NAME>{name}</SHORT-NAME>"
        "<IS-SERVICE>false</IS-SERVICE>"
        f"<DATA-ELEMENTS>{prototypes}</DATA-ELEMENTS>"
        "</SENDER-RECEIVER-INTERFACE>"
    )


def _port(name: str, kind: str, interface: str) -> str:
    tref = "PROVIDED-INTERFACE-TREF" if kind == "P-PORT-PROTOTYPE" else "REQUIRED-INTERFACE-TREF"
    dest = "CLIENT-SERVER-INTERFACE" if interface == "IDiag" else "SENDER-RECEIVER-INTERFACE"
    return (
        f"<{kind}><SHORT-NAME>{name}</SHORT-NAME>"
        f'<{tref} DEST="{dest}">{_ACCESS_PKG}/Interfaces/{interface}</{tref}>'
        f"</{kind}>"
    )


def _access_fragment(
    *,
    by_arguments: str = "",
    by_values: str = "",
    send_points: str = "",
    call_points: str = "",
    with_broken_port: bool = False,
    extra_runnables: str = "",
) -> str:
    """One ARXML document whose SWC's runnable `ControlRun` carries the four container payloads given.

    It holds type `Speed_impl`, interfaces `ISpeed`, `ITorque`, `IDiag`, and the SWC `Control` with ports `rSpeed`,
    `pTorque`, `rDiag`, `pDiag` (plus `rBroken` on a skipped interface on request).
    """
    broken_interface = _sr_interface("IBroken", ["x"], type_name="DoesNotExist") if with_broken_port else ""
    broken_port = _port("rBroken", "R-PORT-PROTOTYPE", "IBroken") if with_broken_port else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">'
        "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Acme</SHORT-NAME><AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Chassis</SHORT-NAME>"
        "<AR-PACKAGES>"
        "<AR-PACKAGE><SHORT-NAME>DataTypes</SHORT-NAME><ELEMENTS>"
        "<SW-BASE-TYPE><SHORT-NAME>uint8</SHORT-NAME><CATEGORY>FIXED_LENGTH</CATEGORY>"
        "<BASE-TYPE-SIZE>8</BASE-TYPE-SIZE><BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>"
        "<NATIVE-DECLARATION>uint8</NATIVE-DECLARATION></SW-BASE-TYPE>"
        "<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>Speed_impl</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
        "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
        f'<BASE-TYPE-REF DEST="SW-BASE-TYPE">{_ACCESS_PKG}/DataTypes/uint8</BASE-TYPE-REF>'
        "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
        "</IMPLEMENTATION-DATA-TYPE>"
        "</ELEMENTS></AR-PACKAGE>"
        "<AR-PACKAGE><SHORT-NAME>Interfaces</SHORT-NAME><ELEMENTS>"
        + _sr_interface("ISpeed", ["value", "valid"])
        + _sr_interface("ITorque", ["value", "valid"])
        + "<CLIENT-SERVER-INTERFACE><SHORT-NAME>IDiag</SHORT-NAME><IS-SERVICE>false</IS-SERVICE><OPERATIONS>"
        "<CLIENT-SERVER-OPERATION><SHORT-NAME>ReadDtc</SHORT-NAME><ARGUMENTS>"
        "<ARGUMENT-DATA-PROTOTYPE><SHORT-NAME>dtc</SHORT-NAME>"
        f'<TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">{_ACCESS_PKG}/DataTypes/Speed_impl</TYPE-TREF>'
        "<DIRECTION>IN</DIRECTION></ARGUMENT-DATA-PROTOTYPE>"
        "</ARGUMENTS></CLIENT-SERVER-OPERATION>"
        "<CLIENT-SERVER-OPERATION><SHORT-NAME>ClearDtc</SHORT-NAME><ARGUMENTS/></CLIENT-SERVER-OPERATION>"
        "</OPERATIONS></CLIENT-SERVER-INTERFACE>" + broken_interface + "</ELEMENTS></AR-PACKAGE>"
        "<AR-PACKAGE><SHORT-NAME>Components</SHORT-NAME><ELEMENTS>"
        "<APPLICATION-SW-COMPONENT-TYPE><SHORT-NAME>Control</SHORT-NAME><PORTS>"
        + _port("rSpeed", "R-PORT-PROTOTYPE", "ISpeed")
        + _port("pTorque", "P-PORT-PROTOTYPE", "ITorque")
        + _port("rDiag", "R-PORT-PROTOTYPE", "IDiag")
        + _port("pDiag", "P-PORT-PROTOTYPE", "IDiag")
        + broken_port
        + "</PORTS><INTERNAL-BEHAVIORS><SWC-INTERNAL-BEHAVIOR><SHORT-NAME>Control_InternalBehavior</SHORT-NAME>"
        "<RUNNABLES><RUNNABLE-ENTITY><SHORT-NAME>ControlRun</SHORT-NAME>"
        "<MINIMUM-START-INTERVAL>0.0</MINIMUM-START-INTERVAL>"
        f"<DATA-RECEIVE-POINT-BY-ARGUMENTS>{by_arguments}</DATA-RECEIVE-POINT-BY-ARGUMENTS>"
        f"<DATA-RECEIVE-POINT-BY-VALUES>{by_values}</DATA-RECEIVE-POINT-BY-VALUES>"
        f"<DATA-SEND-POINTS>{send_points}</DATA-SEND-POINTS>"
        f"<SERVER-CALL-POINTS>{call_points}</SERVER-CALL-POINTS>"
        "<SYMBOL>ControlRun</SYMBOL>"
        "</RUNNABLE-ENTITY>" + extra_runnables + "</RUNNABLES>"
        # Writer-shaped: an absent value is a reported difference.
        "<SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>"
        "</SWC-INTERNAL-BEHAVIOR></INTERNAL-BEHAVIORS></APPLICATION-SW-COMPONENT-TYPE>"
        "</ELEMENTS></AR-PACKAGE>"
        "</AR-PACKAGES></AR-PACKAGE></AR-PACKAGES></AR-PACKAGE></AR-PACKAGES></AUTOSAR>"
    )


def _map_fragment(tmp_path: Path, document: str):
    """Write `document`, read it back and run the full pipeline; returns (model, swcs, swc_skip_log)."""
    source = tmp_path / "fragment.arxml"
    source.write_text(document, encoding="utf-8", newline="\n")
    model = read_arxml(str(source))
    _data_types, _interfaces, swcs, swc_skip_log = _map_full(model)
    return model, swcs, swc_skip_log


def _control_runnable(swcs: list[ImportedSwc]):
    swc = _by_swc_name(swcs, "Control").swc
    assert [r.name for r in swc.runnables] == ["ControlRun"]
    return swc.runnables[0]


def _control_collapses(swcs: list[ImportedSwc]) -> list[str]:
    """The lossy-transformation lines `map_swcs` recorded for the SWC."""
    return _by_swc_name(swcs, "Control").access_collapses


def _written_bytes(swcs: list[ImportedSwc]) -> str:
    """Exactly what `run_import` writes for the SWC: `swc_to_dict` dumped as `report.write_yaml` dumps it."""
    return yaml.safe_dump(swc_to_dict(_by_swc_name(swcs, "Control").swc), default_flow_style=False, sort_keys=False)


_ACCESS_SKIP_TAGS = frozenset({"VARIABLE-ACCESS", "SYNCHRONOUS-SERVER-CALL-POINT"})


def _access_skips(skip_log) -> list[tuple[str, str]]:
    return [(entry.path, entry.reason) for entry in skip_log if entry.tag in _ACCESS_SKIP_TAGS]


_FOUR_CONTAINERS = dict(
    by_arguments=_variable_access("R_rSpeed_value", "rSpeed", "ISpeed/value"),
    by_values=_variable_access("R_rSpeed_valid", "rSpeed", "ISpeed/valid"),
    send_points=(
        _variable_access("W_pTorque_valid", "pTorque", "ITorque/valid", dest="P-PORT-PROTOTYPE")
        + _variable_access("W_pTorque_value", "pTorque", "ITorque/value", dest="P-PORT-PROTOTYPE")
    ),
    call_points=_call_point("Call_rDiag_ReadDtc", "rDiag", "IDiag/ReadDtc"),
)

_FOUR_CONTAINERS_WRITTEN = (
    "name: Control\n"
    "type: application\n"
    "ports:\n"
    "- name: rSpeed\n"
    "  direction: require\n"
    "  interface: ISpeed\n"
    "- name: pTorque\n"
    "  direction: provide\n"
    "  interface: ITorque\n"
    "- name: rDiag\n"
    "  direction: require\n"
    "  interface: IDiag\n"
    "- name: pDiag\n"
    "  direction: provide\n"
    "  interface: IDiag\n"
    "runnables:\n"
    "- name: ControlRun\n"
    "  events: []\n"
    "  reads:\n"
    "  - port: rSpeed\n"
    "    element: value\n"
    "  - port: rSpeed\n"
    "    element: valid\n"
    "    by: value\n"
    "  writes:\n"
    "  - port: pTorque\n"
    "    element: valid\n"
    "  - port: pTorque\n"
    "    element: value\n"
    "  calls:\n"
    "  - port: rDiag\n"
    "    operation: ReadDtc\n"
)


class TestAccessPointsLandInTheWrittenBytes:
    """The four containers, asserted on the bytes `run_import` writes."""

    def test_the_four_containers_are_written_as_the_three_lists(self, tmp_path: Path) -> None:
        """The four containers are written as the three lists, in file order.

        `by: value` is on the by-value read alone, with no default `min_start_interval`.
        """
        _model, swcs, swc_skip_log = _map_fragment(tmp_path, _access_fragment(**_FOUR_CONTAINERS))
        assert _access_skips(swc_skip_log) == []
        assert _written_bytes(swcs) == _FOUR_CONTAINERS_WRITTEN

    def test_the_access_short_names_are_not_model_content(self, tmp_path: Path) -> None:
        """The export's `R_`/`W_`/`Call_` SHORT-NAMEs and registry-minted suffixes reach no byte of the file."""
        _model, swcs, _swc_skip_log = _map_fragment(tmp_path, _access_fragment(**_FOUR_CONTAINERS))
        written = _written_bytes(swcs)
        for short_name in (
            "R_rSpeed_value",
            "R_rSpeed_valid",
            "W_pTorque_valid",
            "W_pTorque_value",
            "Call_rDiag_ReadDtc",
        ):
            assert short_name not in written
        assert "_0x" not in written and "_impl" not in written

    def test_a_runnable_with_empty_containers_is_written_as_before_this_phase(self, tmp_path: Path) -> None:
        """Empty containers on a runnable write no empty `reads`/`writes`/`calls` list."""
        _model, swcs, swc_skip_log = _map_fragment(tmp_path, _access_fragment())
        assert _access_skips(swc_skip_log) == []
        written = _written_bytes(swcs)
        assert written.endswith("runnables:\n- name: ControlRun\n  events: []\n")
        assert "reads" not in written and "writes" not in written and "calls" not in written

    def test_run_import_writes_those_bytes_to_the_component_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`run_import` writes exactly the serializer's bytes to the component file: no second serializer."""
        source = tmp_path / "fragment.arxml"
        source.write_text(_access_fragment(**_FOUR_CONTAINERS), encoding="utf-8", newline="\n")
        project = tmp_path / "proj"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(project)])
        main()

        report = run_import(str(source), str(project / "swcraft.yaml"))
        assert report.imported_swcs == 1
        written = (project / "model" / "swc" / "Components" / "Control.yaml").read_text(encoding="utf-8")
        assert written == _FOUR_CONTAINERS_WRITTEN
        assert "Collapsed duplicate access points" not in report.render()

    def test_an_access_free_runnable_beside_an_accessing_one_stays_free(self, tmp_path: Path) -> None:
        """An access-free runnable beside an accessing one gains no entries and no empty list."""
        extra = (
            "<RUNNABLE-ENTITY><SHORT-NAME>ControlInit</SHORT-NAME>"
            "<DATA-RECEIVE-POINT-BY-ARGUMENTS/><DATA-RECEIVE-POINT-BY-VALUES/>"
            "<DATA-SEND-POINTS/><SERVER-CALL-POINTS/>"
            "</RUNNABLE-ENTITY>"
        )
        _model, swcs, swc_skip_log = _map_fragment(
            tmp_path, _access_fragment(**{**_FOUR_CONTAINERS, "extra_runnables": extra})
        )
        assert _access_skips(swc_skip_log) == []
        swc = _by_swc_name(swcs, "Control").swc
        assert [r.name for r in swc.runnables] == ["ControlRun", "ControlInit"]
        assert (swc.runnables[1].reads, swc.runnables[1].writes, swc.runnables[1].calls) == ([], [], [])
        assert _written_bytes(swcs).endswith("    operation: ReadDtc\n- name: ControlInit\n  events: []\n")


class TestDuplicateAccessCollapses:
    """A repeated `(port, member)` under two SHORT-NAMEs collapses to one entry with a lossy-transformation line.

    It is never a skip, which would abort `import component`.
    """

    def test_a_distinct_name_repeat_collapses_to_one_entry_and_one_report_line(self, tmp_path: Path) -> None:
        _model, swcs, swc_skip_log = _map_fragment(
            tmp_path,
            _access_fragment(
                by_arguments=(
                    _variable_access("R_rSpeed_value", "rSpeed", "ISpeed/value")
                    + _variable_access("R_rSpeed_value_again", "rSpeed", "ISpeed/value")
                )
            ),
        )

        runnable = _control_runnable(swcs)
        assert [(r.port, r.element, r.by) for r in runnable.reads] == [("rSpeed", "value", None)]
        assert _access_skips(swc_skip_log) == []
        collapses = _control_collapses(swcs)
        assert len(collapses) == 1
        assert "Control" in collapses[0] and "ControlRun" in collapses[0]
        assert "rSpeed.value" in collapses[0]
        assert "R_rSpeed_value_again" in collapses[0] and "R_rSpeed_value" in collapses[0]

    def test_a_pair_read_in_both_forms_keeps_the_first_form(self, tmp_path: Path) -> None:
        """A pair read in both forms is one entry, the first in document order: `by` is no part of the identity."""
        _model, swcs, _swc_skip_log = _map_fragment(
            tmp_path,
            _access_fragment(
                by_arguments=_variable_access("R_rSpeed_value", "rSpeed", "ISpeed/value"),
                by_values=_variable_access("R_rSpeed_value_by_value", "rSpeed", "ISpeed/value"),
            ),
        )

        runnable = _control_runnable(swcs)
        assert [(r.port, r.element, r.by) for r in runnable.reads] == [("rSpeed", "value", None)]
        assert len(_control_collapses(swcs)) == 1

    def test_an_unnamed_pair_repeat_is_reported_as_unnamed(self, tmp_path: Path) -> None:
        """An unnamed pair's collapse line says unnamed, not `'' collapsed into ''`."""
        unnamed = _variable_access_raw(None, f"{_ACCESS_SWC_PATH}/rSpeed", f"{_ACCESS_PKG}/Interfaces/ISpeed/value")
        _model, swcs, swc_skip_log = _map_fragment(tmp_path, _access_fragment(by_arguments=unnamed + unnamed))
        assert [(r.port, r.element) for r in _control_runnable(swcs).reads] == [("rSpeed", "value")]
        assert _access_skips(swc_skip_log) == []
        (line,) = _control_collapses(swcs)
        assert line.endswith("'<unnamed>' collapsed into '<unnamed>'"), line

    def test_the_same_pair_in_writes_and_reads_is_not_a_duplicate(self, tmp_path: Path) -> None:
        """Identity is per list: writing and reading one element is two declarations."""
        _model, swcs, swc_skip_log = _map_fragment(
            tmp_path,
            _access_fragment(
                by_arguments=_variable_access("R_rSpeed_value", "rSpeed", "ISpeed/value"),
                send_points=_variable_access("W_pTorque_value", "pTorque", "ITorque/value", dest="P-PORT-PROTOTYPE"),
            ),
        )
        runnable = _control_runnable(swcs)
        assert [(r.port, r.element) for r in runnable.reads] == [("rSpeed", "value")]
        assert [(w.port, w.element) for w in runnable.writes] == [("pTorque", "value")]
        assert _access_skips(swc_skip_log) == []

    def test_the_collapse_reaches_the_import_report(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        source = tmp_path / "fragment.arxml"
        source.write_text(
            _access_fragment(
                by_arguments=(
                    _variable_access("R_rSpeed_value", "rSpeed", "ISpeed/value")
                    + _variable_access("R_rSpeed_value_again", "rSpeed", "ISpeed/value")
                )
            ),
            encoding="utf-8",
            newline="\n",
        )
        project = tmp_path / "proj"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(project)])
        main()

        report = run_import(str(source), str(project / "swcraft.yaml"))
        assert report.access_collapse_lines and "R_rSpeed_value_again" in report.access_collapse_lines[0]
        rendered = report.render()
        assert "Collapsed duplicate access points:" in rendered
        assert "VARIABLE-ACCESS" not in report.skipped_by_tag, "a collapse is a report line, not a skip"

    def test_the_report_gathers_the_lines_of_the_components_it_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`run_import` reports the collapse lines of the components it writes.

        It reports them after selection and renames, naming each by the export's SHORT-NAME.
        """
        tail = "</AR-PACKAGES></AR-PACKAGE></AR-PACKAGES></AR-PACKAGE></AR-PACKAGES></AUTOSAR>"
        other_swc_path = f"{_ACCESS_PKG}/Other/Control_Implementation"
        other_read = _variable_access_raw(
            "R_rSpeed_value", f"{other_swc_path}/rSpeed", f"{_ACCESS_PKG}/Interfaces/ISpeed/value"
        )
        other_read_again = _variable_access_raw(
            "R_rSpeed_value_again", f"{other_swc_path}/rSpeed", f"{_ACCESS_PKG}/Interfaces/ISpeed/value"
        )
        # A second component whose name the writer reserves (`{Control}_Implementation`
        # beside `Control`), in a package of its own, with a collapse of its own.
        other_package = (
            "<AR-PACKAGE><SHORT-NAME>Other</SHORT-NAME><ELEMENTS>"
            "<APPLICATION-SW-COMPONENT-TYPE><SHORT-NAME>Control_Implementation</SHORT-NAME><PORTS>"
            + _port("rSpeed", "R-PORT-PROTOTYPE", "ISpeed")
            + "</PORTS><INTERNAL-BEHAVIORS><SWC-INTERNAL-BEHAVIOR><SHORT-NAME>Other_InternalBehavior</SHORT-NAME>"
            "<RUNNABLES><RUNNABLE-ENTITY><SHORT-NAME>OtherRun</SHORT-NAME>"
            "<MINIMUM-START-INTERVAL>0.0</MINIMUM-START-INTERVAL>"
            f"<DATA-RECEIVE-POINT-BY-ARGUMENTS>{other_read}{other_read_again}</DATA-RECEIVE-POINT-BY-ARGUMENTS>"
            "<SYMBOL>OtherRun</SYMBOL></RUNNABLE-ENTITY></RUNNABLES>"
            "<SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>"
            "</SWC-INTERNAL-BEHAVIOR></INTERNAL-BEHAVIORS></APPLICATION-SW-COMPONENT-TYPE>"
            "</ELEMENTS></AR-PACKAGE>"
        )
        document = _access_fragment(
            by_arguments=(
                _variable_access("R_rSpeed_value", "rSpeed", "ISpeed/value")
                + _variable_access("R_rSpeed_value_again", "rSpeed", "ISpeed/value")
            )
        )
        assert document.count(tail) == 1
        source = tmp_path / "fragment.arxml"
        source.write_text(document.replace(tail, other_package + tail), encoding="utf-8", newline="\n")

        selected = tmp_path / "selected"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(selected)])
        main()
        report = run_import(str(source), str(selected / "swcraft.yaml"), packages=["Components"])
        assert report.packages == ["Components"]
        assert [line.split(":")[0] for line in report.access_collapse_lines] == ["Control/ControlRun"]

        whole = tmp_path / "whole"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(whole)])
        main()
        report = run_import(str(source), str(whole / "swcraft.yaml"))
        assert [line.split(":")[0] for line in report.access_collapse_lines] == [
            "Control/ControlRun",
            "Control_Implementation/OtherRun",
        ]
        assert any("Control_Implementation" in line for line in report.rename_lines), report.rename_lines
        assert not (whole / "model" / "swc" / "Other" / "Control_Implementation.yaml").exists()

    def test_the_same_short_name_twice_is_the_readers_drop_not_a_collapse(self, tmp_path: Path) -> None:
        """The same SHORT-NAME twice is the reader's duplicate-path drop: the mapper sees one entry, no collapse."""
        document = _access_fragment(
            by_arguments=(
                _variable_access("R_rSpeed_value", "rSpeed", "ISpeed/value")
                + _variable_access("R_rSpeed_value", "rSpeed", "ISpeed/value")
            )
        )
        source = tmp_path / "fragment.arxml"
        source.write_text(document, encoding="utf-8", newline="\n")
        model = read_arxml(str(source))
        reader_skips = [(e.path, e.tag) for e in model.skip_log]
        assert reader_skips == [(f"{_ACCESS_RUNNABLE_PATH}/R_rSpeed_value", "VARIABLE-ACCESS")]
        assert len(model.swcs[0].runnables[0].accesses) == 1

        forms: list = []
        data_types, registry, _type_skip_log = map_data_types(model, elements=forms)
        interfaces, iface_skip_log = map_interfaces(model, registry, data_types, elements=forms)
        swcs, swc_skip_log = map_swcs(
            model, interfaces, iface_skip_log=iface_skip_log, port_inits={}, variable_types={}
        )
        assert [(r.port, r.element) for r in _control_runnable(swcs).reads] == [("rSpeed", "value")]
        assert _access_skips(swc_skip_log) == []
        assert _control_collapses(swcs) == []


class TestAccessSkips:
    """Every loss is a skip entry with a reason, the SWC and its runnable kept.

    The losses: an undeclared port, a dangling target, a shape mismatch, and the chain-skip.
    """

    def test_an_access_on_a_port_the_swc_never_declares_is_skipped(self, tmp_path: Path) -> None:
        _model, swcs, swc_skip_log = _map_fragment(
            tmp_path, _access_fragment(by_arguments=_variable_access("R_rGhost_value", "rGhost", "ISpeed/value"))
        )
        runnable = _control_runnable(swcs)
        assert runnable.reads == []
        [(path, reason)] = _access_skips(swc_skip_log)
        assert path == f"{_ACCESS_RUNNABLE_PATH}/R_rGhost_value"
        assert "port 'rGhost' is not declared on SWC 'Control'" in reason

    def test_a_dangling_data_prototype_is_skipped(self, tmp_path: Path) -> None:
        _model, swcs, swc_skip_log = _map_fragment(
            tmp_path, _access_fragment(by_arguments=_variable_access("R_rSpeed_ghost", "rSpeed", "ISpeed/ghost"))
        )
        assert _control_runnable(swcs).reads == []
        [(path, reason)] = _access_skips(swc_skip_log)
        assert path == f"{_ACCESS_RUNNABLE_PATH}/R_rSpeed_ghost"
        assert "unresolved reference" in reason and "ISpeed/ghost" in reason and "ISpeed" in reason

    def test_a_target_under_another_interface_is_skipped(self, tmp_path: Path) -> None:
        """A target under another interface than the port's is skipped."""
        _model, swcs, swc_skip_log = _map_fragment(
            tmp_path, _access_fragment(by_arguments=_variable_access("R_rSpeed_value", "rSpeed", "ITorque/value"))
        )
        assert _control_runnable(swcs).reads == []
        [(_path, reason)] = _access_skips(swc_skip_log)
        assert "unresolved reference" in reason and "ITorque/value" in reason

    def test_a_dangling_operation_is_skipped(self, tmp_path: Path) -> None:
        _model, swcs, swc_skip_log = _map_fragment(
            tmp_path, _access_fragment(call_points=_call_point("Call_rDiag_Ghost", "rDiag", "IDiag/Ghost"))
        )
        assert _control_runnable(swcs).calls == []
        [(path, reason)] = _access_skips(swc_skip_log)
        assert path == f"{_ACCESS_RUNNABLE_PATH}/Call_rDiag_Ghost"
        assert "unresolved reference" in reason and "IDiag/Ghost" in reason

    def test_a_data_access_through_a_client_server_port_is_skipped(self, tmp_path: Path) -> None:
        _model, swcs, swc_skip_log = _map_fragment(
            tmp_path, _access_fragment(by_arguments=_variable_access("R_rDiag_ReadDtc", "rDiag", "IDiag/ReadDtc"))
        )
        assert _control_runnable(swcs).reads == []
        [(_path, reason)] = _access_skips(swc_skip_log)
        assert "client/server" in reason and "rDiag" in reason, "the kind is spelled as check-model spells it"

    def test_a_call_through_a_sender_receiver_port_is_skipped(self, tmp_path: Path) -> None:
        _model, swcs, swc_skip_log = _map_fragment(
            tmp_path, _access_fragment(call_points=_call_point("Call_rSpeed_value", "rSpeed", "ISpeed/value"))
        )
        assert _control_runnable(swcs).calls == []
        [(_path, reason)] = _access_skips(swc_skip_log)
        assert "sender/receiver" in reason and "rSpeed" in reason

    def test_an_access_wrong_on_both_counts_is_skipped_for_its_interface_kind(self, tmp_path: Path) -> None:
        """An access wrong on both counts is skipped for its interface kind.

        The kind is decided first, as `check_runnable_access` decides it, so both consumers name the same repair.
        """
        _model, swcs, swc_skip_log = _map_fragment(
            tmp_path,
            _access_fragment(
                by_arguments=_variable_access("R_pDiag_ReadDtc", "pDiag", "IDiag/ReadDtc", dest="P-PORT-PROTOTYPE")
            ),
        )
        assert _control_runnable(swcs).reads == []
        [(_path, reason)] = _access_skips(swc_skip_log)
        assert "client/server" in reason and "pDiag" in reason
        assert "provide" not in reason, "one reason, the one check-model would report"

    def test_a_read_through_a_provide_port_is_skipped(self, tmp_path: Path) -> None:
        """A read through a provide port is skipped: `rule:swc.access.direction-mismatch` would refuse it."""
        _model, swcs, swc_skip_log = _map_fragment(
            tmp_path,
            _access_fragment(
                by_arguments=_variable_access("R_pTorque_value", "pTorque", "ITorque/value", dest="P-PORT-PROTOTYPE")
            ),
        )
        assert _control_runnable(swcs).reads == []
        [(_path, reason)] = _access_skips(swc_skip_log)
        assert "provide" in reason and "pTorque" in reason

    def test_a_write_through_a_require_port_is_skipped(self, tmp_path: Path) -> None:
        _model, swcs, swc_skip_log = _map_fragment(
            tmp_path, _access_fragment(send_points=_variable_access("W_rSpeed_value", "rSpeed", "ISpeed/value"))
        )
        assert _control_runnable(swcs).writes == []
        [(_path, reason)] = _access_skips(swc_skip_log)
        assert "require" in reason and "rSpeed" in reason

    def test_an_access_on_a_skipped_port_chain_skips_with_the_referring_reason(self, tmp_path: Path) -> None:
        """An access on a skipped port chain-skips with the port's reason, not as an undeclared port."""
        _model, swcs, swc_skip_log = _map_fragment(
            tmp_path,
            _access_fragment(
                by_arguments=_variable_access("R_rBroken_x", "rBroken", "IBroken/x"), with_broken_port=True
            ),
        )
        assert _control_runnable(swcs).reads == []
        port_reason = _skip_reason(swc_skip_log, f"{_ACCESS_SWC_PATH}/rBroken")
        assert port_reason is not None and "IBroken" in port_reason
        [(path, reason)] = _access_skips(swc_skip_log)
        assert path == f"{_ACCESS_RUNNABLE_PATH}/R_rBroken_x"
        assert "chain" in reason.lower() and "rBroken" in reason
        assert port_reason in reason, "the referring reason is carried, not paraphrased"
        assert "is not declared on" not in reason

    def test_an_access_on_a_skipped_runnable_chain_skips_too(self, tmp_path: Path) -> None:
        """Every access of a skipped duplicate runnable is chain-skipped with its reason."""
        extra = (
            "<RUNNABLE-ENTITY><SHORT-NAME>ControlRun</SHORT-NAME>"
            f"<DATA-RECEIVE-POINT-BY-ARGUMENTS>{_variable_access('R_rSpeed_valid', 'rSpeed', 'ISpeed/valid')}"
            "</DATA-RECEIVE-POINT-BY-ARGUMENTS>"
            "</RUNNABLE-ENTITY>"
        )
        # The path index drops the second `ControlRun`, but `_read_swc_runnables` walks the RUNNABLES block
        # directly and sees it, which is why `map_swcs` has a duplicate-name skip.
        _model, swcs, swc_skip_log = _map_fragment(
            tmp_path,
            _access_fragment(
                by_arguments=_variable_access("R_rSpeed_value", "rSpeed", "ISpeed/value"), extra_runnables=extra
            ),
        )
        runnable = _control_runnable(swcs)
        assert [(r.port, r.element) for r in runnable.reads] == [("rSpeed", "value")]
        runnable_reason = _skip_reason(swc_skip_log, _ACCESS_RUNNABLE_PATH)
        assert runnable_reason is not None and "duplicate runnable name" in runnable_reason
        [(path, reason)] = _access_skips(swc_skip_log)
        assert path == f"{_ACCESS_RUNNABLE_PATH}/R_rSpeed_valid"
        assert "chain" in reason.lower() and runnable_reason in reason

    def test_an_access_kind_the_reader_does_not_recognize_is_reported(self, tmp_path: Path) -> None:
        """A call point kind the reader does not read is skip+reported, never silently dropped."""
        document = _access_fragment(
            call_points=_call_point("Call_rDiag_ReadDtc", "rDiag", "IDiag/ReadDtc", tag="FUTURE-SERVER-CALL-POINT")
        )
        model, swcs, _swc_skip_log = _map_fragment(tmp_path, document)
        assert _control_runnable(swcs).calls == []
        assert [(e.path, e.tag) for e in model.skip_log] == [
            (f"{_ACCESS_RUNNABLE_PATH}/Call_rDiag_ReadDtc", "FUTURE-SERVER-CALL-POINT")
        ]
        assert "SERVER-CALL-POINTS" in model.skip_log.entries[0].reason

    def test_an_asynchronous_call_point_is_read_as_an_asynchronous_call(self, tmp_path: Path) -> None:
        """The asynchronous call point is read, and the call it becomes is ``asynchronous``."""
        document = _access_fragment(
            call_points=_call_point(
                "Call_rDiag_ReadDtc", "rDiag", "IDiag/ReadDtc", tag="ASYNCHRONOUS-SERVER-CALL-POINT"
            )
        )
        model, swcs, _swc_skip_log = _map_fragment(tmp_path, document)
        assert [(c.port, c.operation, c.asynchronous, c.timeout) for c in _control_runnable(swcs).calls] == [
            ("rDiag", "ReadDtc", True, 0.0)
        ]
        assert list(model.skip_log) == []

    def test_an_access_without_a_port_reference_is_skipped(self, tmp_path: Path) -> None:
        payload = (
            "<VARIABLE-ACCESS><SHORT-NAME>R_nowhere</SHORT-NAME>"
            "<ACCESSED-VARIABLE><AUTOSAR-VARIABLE-IREF>"
            f'<TARGET-DATA-PROTOTYPE-REF DEST="VARIABLE-DATA-PROTOTYPE">{_ACCESS_PKG}/Interfaces/ISpeed/value'
            "</TARGET-DATA-PROTOTYPE-REF>"
            "</AUTOSAR-VARIABLE-IREF></ACCESSED-VARIABLE></VARIABLE-ACCESS>"
        )
        _model, swcs, swc_skip_log = _map_fragment(tmp_path, _access_fragment(by_arguments=payload))
        assert _control_runnable(swcs).reads == []
        [(path, reason)] = _access_skips(swc_skip_log)
        assert path == f"{_ACCESS_RUNNABLE_PATH}/R_nowhere"
        assert "no port reference" in reason

    @pytest.mark.parametrize("target", ["value", " "], ids=["bare-name", "blank"])
    def test_a_data_target_that_is_not_a_path_is_skipped_not_crashed(self, tmp_path: Path, target: str) -> None:
        """A data target with no `/` is a skip line, never a raise out of `map_swcs`.

        Guards a past defect: it aborted `import model` and `import component` with a traceback."""
        payload = _variable_access_raw("R_rSpeed_value", f"{_ACCESS_SWC_PATH}/rSpeed", target)
        _model, swcs, swc_skip_log = _map_fragment(tmp_path, _access_fragment(by_arguments=payload))
        assert _control_runnable(swcs).reads == []
        [(path, reason)] = _access_skips(swc_skip_log)
        assert path == f"{_ACCESS_RUNNABLE_PATH}/R_rSpeed_value"
        assert "unresolved reference" in reason and "not an AUTOSAR path" in reason

    def test_an_operation_target_that_is_not_a_path_is_skipped_not_crashed(self, tmp_path: Path) -> None:
        payload = _call_point_raw("Call_rDiag_ReadDtc", f"{_ACCESS_SWC_PATH}/rDiag", "ReadDtc")
        _model, swcs, swc_skip_log = _map_fragment(tmp_path, _access_fragment(call_points=payload))
        assert _control_runnable(swcs).calls == []
        [(path, reason)] = _access_skips(swc_skip_log)
        assert path == f"{_ACCESS_RUNNABLE_PATH}/Call_rDiag_ReadDtc"
        assert "unresolved reference ReadDtc" in reason and "not an AUTOSAR path" in reason

    def test_a_variable_access_without_a_target_reference_is_skipped(self, tmp_path: Path) -> None:
        payload = _variable_access_raw("R_rSpeed_none", f"{_ACCESS_SWC_PATH}/rSpeed", None)
        _model, swcs, swc_skip_log = _map_fragment(tmp_path, _access_fragment(by_arguments=payload))
        assert _control_runnable(swcs).reads == []
        [(path, reason)] = _access_skips(swc_skip_log)
        assert path == f"{_ACCESS_RUNNABLE_PATH}/R_rSpeed_none"
        assert "no target reference" in reason

    def test_a_variable_access_without_an_accessed_variable_is_skipped(self, tmp_path: Path) -> None:
        """A VARIABLE-ACCESS without an ACCESSED-VARIABLE is skipped naming the missing port."""
        payload = _variable_access_raw("R_bare", None, None)
        _model, swcs, swc_skip_log = _map_fragment(tmp_path, _access_fragment(by_arguments=payload))
        assert _control_runnable(swcs).reads == []
        [(path, reason)] = _access_skips(swc_skip_log)
        assert path == f"{_ACCESS_RUNNABLE_PATH}/R_bare"
        assert "no port reference" in reason

    def test_a_call_point_without_a_context_port_reference_is_skipped(self, tmp_path: Path) -> None:
        payload = _call_point_raw("Call_nowhere", None, f"{_ACCESS_PKG}/Interfaces/IDiag/ReadDtc")
        _model, swcs, swc_skip_log = _map_fragment(tmp_path, _access_fragment(call_points=payload))
        assert _control_runnable(swcs).calls == []
        [(path, reason)] = _access_skips(swc_skip_log)
        assert path == f"{_ACCESS_RUNNABLE_PATH}/Call_nowhere"
        assert "calls access has no port reference" in reason

    def test_a_call_point_without_a_target_operation_reference_is_skipped(self, tmp_path: Path) -> None:
        payload = _call_point_raw("Call_rDiag_none", f"{_ACCESS_SWC_PATH}/rDiag", None)
        _model, swcs, swc_skip_log = _map_fragment(tmp_path, _access_fragment(call_points=payload))
        assert _control_runnable(swcs).calls == []
        [(path, reason)] = _access_skips(swc_skip_log)
        assert path == f"{_ACCESS_RUNNABLE_PATH}/Call_rDiag_none"
        assert "calls access has no target reference" in reason

    def test_a_runnable_skipped_for_its_interval_chain_skips_its_accesses_and_still_claims_its_name(
        self, tmp_path: Path
    ) -> None:
        """A runnable skipped for its interval still claims its name.

        Its accesses chain-skip with the interval reason verbatim. A NaN spelling never reaches this branch
        (`parse_float_literal` returns None for it at the reader); a negative interval reaches it through the reader.
        """
        bad_path = f"{_ACCESS_BEHAVIOR_PATH}/Bad"
        extra = (
            "<RUNNABLE-ENTITY><SHORT-NAME>Bad</SHORT-NAME>"
            "<MINIMUM-START-INTERVAL>-1.0</MINIMUM-START-INTERVAL>"
            f"<DATA-RECEIVE-POINT-BY-ARGUMENTS>{_variable_access('R_rSpeed_valid', 'rSpeed', 'ISpeed/valid')}"
            "</DATA-RECEIVE-POINT-BY-ARGUMENTS>"
            "</RUNNABLE-ENTITY>"
            "<RUNNABLE-ENTITY><SHORT-NAME>Bad</SHORT-NAME>"
            "<MINIMUM-START-INTERVAL>0.0</MINIMUM-START-INTERVAL>"
            "</RUNNABLE-ENTITY>"
        )
        _model, swcs, swc_skip_log = _map_fragment(tmp_path, _access_fragment(extra_runnables=extra))
        assert [r.name for r in _by_swc_name(swcs, "Control").swc.runnables] == ["ControlRun"]
        interval_reason, duplicate_reason = [e.reason for e in swc_skip_log if e.path == bad_path]
        assert "MIN-START-INTERVAL must be >= 0 and finite" in interval_reason
        assert "duplicate runnable name 'Bad'" in duplicate_reason
        [(path, reason)] = _access_skips(swc_skip_log)
        assert path == f"{bad_path}/R_rSpeed_valid"
        assert reason == f"chain-skipped: runnable 'Bad' was skipped ({interval_reason})"

    def test_a_skip_never_costs_the_sibling_entries(self, tmp_path: Path) -> None:
        """One bad entry among good ones: the good ones land, in order."""
        _model, swcs, swc_skip_log = _map_fragment(
            tmp_path,
            _access_fragment(
                by_arguments=(
                    _variable_access("R_rSpeed_value", "rSpeed", "ISpeed/value")
                    + _variable_access("R_rGhost_value", "rGhost", "ISpeed/value")
                    + _variable_access("R_rSpeed_valid", "rSpeed", "ISpeed/valid")
                )
            ),
        )
        assert [(r.port, r.element) for r in _control_runnable(swcs).reads] == [
            ("rSpeed", "value"),
            ("rSpeed", "valid"),
        ]
        assert len(_access_skips(swc_skip_log)) == 1


class TestTheReaderReadsTheFourContainers:
    """`arxml_reader._read_swc_runnables` reads the four containers into `SwcRunnableInfo.accesses` raw.

    It reads them in document order; deciding what maps is `map_swcs`'s job.
    """

    def test_every_container_is_read_with_its_kind_and_raw_refs(self, tmp_path: Path) -> None:
        model, _swcs, _swc_skip_log = _map_fragment(tmp_path, _access_fragment(**_FOUR_CONTAINERS))
        accesses = model.swcs[0].runnables[0].accesses
        assert [(a.name, a.kind, a.tag) for a in accesses] == [
            ("R_rSpeed_value", "DATA-RECEIVE-POINT-BY-ARGUMENTS", "VARIABLE-ACCESS"),
            ("R_rSpeed_valid", "DATA-RECEIVE-POINT-BY-VALUES", "VARIABLE-ACCESS"),
            ("W_pTorque_valid", "DATA-SEND-POINTS", "VARIABLE-ACCESS"),
            ("W_pTorque_value", "DATA-SEND-POINTS", "VARIABLE-ACCESS"),
            ("Call_rDiag_ReadDtc", "SERVER-CALL-POINTS", "SYNCHRONOUS-SERVER-CALL-POINT"),
        ]
        assert accesses[0].path == f"{_ACCESS_RUNNABLE_PATH}/R_rSpeed_value"
        assert accesses[0].port_ref == f"{_ACCESS_SWC_PATH}/rSpeed"
        assert accesses[0].target_ref == f"{_ACCESS_PKG}/Interfaces/ISpeed/value"
        assert accesses[4].port_ref == f"{_ACCESS_SWC_PATH}/rDiag"
        assert accesses[4].target_ref == f"{_ACCESS_PKG}/Interfaces/IDiag/ReadDtc"
        assert model.skip_log.entries == []

    def test_two_unnamed_accesses_are_both_read(self, tmp_path: Path) -> None:
        """Two unnamed accesses are both read, each at the `<unnamed>` path."""
        payload = _variable_access_raw(
            None, f"{_ACCESS_SWC_PATH}/rSpeed", f"{_ACCESS_PKG}/Interfaces/ISpeed/value"
        ) + _variable_access_raw(None, f"{_ACCESS_SWC_PATH}/rSpeed", f"{_ACCESS_PKG}/Interfaces/ISpeed/valid")
        model, swcs, swc_skip_log = _map_fragment(tmp_path, _access_fragment(by_arguments=payload))
        accesses = model.swcs[0].runnables[0].accesses
        assert [(a.name, a.path) for a in accesses] == [("", f"{_ACCESS_RUNNABLE_PATH}/<unnamed>")] * 2
        assert model.skip_log.entries == []
        assert [(r.port, r.element) for r in _control_runnable(swcs).reads] == [
            ("rSpeed", "value"),
            ("rSpeed", "valid"),
        ]
        assert _access_skips(swc_skip_log) == []

    def test_an_empty_short_name_is_claimed_as_the_path_index_claims_it(self, tmp_path: Path) -> None:
        """An empty SHORT-NAME is claimed as the path index claims it: one read, one skip, path `<unnamed>`."""
        empty = _variable_access_raw("", f"{_ACCESS_SWC_PATH}/rSpeed", f"{_ACCESS_PKG}/Interfaces/ISpeed/value")
        second = _variable_access_raw("", f"{_ACCESS_SWC_PATH}/rSpeed", f"{_ACCESS_PKG}/Interfaces/ISpeed/valid")
        model, swcs, swc_skip_log = _map_fragment(tmp_path, _access_fragment(by_arguments=empty + second))
        assert [(e.path, e.tag) for e in model.skip_log] == [(f"{_ACCESS_RUNNABLE_PATH}/", "VARIABLE-ACCESS")]
        accesses = model.swcs[0].runnables[0].accesses
        assert [(a.name, a.path) for a in accesses] == [("", f"{_ACCESS_RUNNABLE_PATH}/<unnamed>")]
        assert [(r.port, r.element) for r in _control_runnable(swcs).reads] == [("rSpeed", "value")]
        assert _access_skips(swc_skip_log) == []

    def test_the_same_short_name_in_two_containers_is_read_once(self, tmp_path: Path) -> None:
        """The same SHORT-NAME in two containers is read once, as the path index spans the containers."""
        document = _access_fragment(
            by_arguments=_variable_access("Twin", "rSpeed", "ISpeed/value"),
            send_points=_variable_access("Twin", "pTorque", "ITorque/value", dest="P-PORT-PROTOTYPE"),
        )
        model, swcs, _swc_skip_log = _map_fragment(tmp_path, document)
        accesses = model.swcs[0].runnables[0].accesses
        assert [(a.name, a.kind) for a in accesses] == [("Twin", "DATA-RECEIVE-POINT-BY-ARGUMENTS")]
        assert [(e.path, e.tag) for e in model.skip_log] == [(f"{_ACCESS_RUNNABLE_PATH}/Twin", "VARIABLE-ACCESS")]
        assert "duplicate full AUTOSAR path" in model.skip_log.entries[0].reason
        runnable = _control_runnable(swcs)
        assert [(r.port, r.element) for r in runnable.reads] == [("rSpeed", "value")]
        assert runnable.writes == []

    def test_a_same_name_pair_across_tags_survives_as_the_index_decides(self, tmp_path: Path) -> None:
        """Across tags the first element at a path survives, as the index decides, not by tag."""
        document = _access_fragment(
            call_points=_call_point("Call_x", "rDiag", "IDiag/ReadDtc", tag="FUTURE-SERVER-CALL-POINT")
            + _call_point("Call_x", "rDiag", "IDiag/ReadDtc")
        )
        model, swcs, _swc_skip_log = _map_fragment(tmp_path, document)
        assert model.swcs[0].runnables[0].accesses == []
        assert _control_runnable(swcs).calls == []
        assert sorted((e.path, e.tag) for e in model.skip_log) == [
            (f"{_ACCESS_RUNNABLE_PATH}/Call_x", "FUTURE-SERVER-CALL-POINT"),
            (f"{_ACCESS_RUNNABLE_PATH}/Call_x", "SYNCHRONOUS-SERVER-CALL-POINT"),
        ]

    def test_a_hand_built_runnable_info_has_no_accesses(self) -> None:
        """A hand-built `SwcRunnableInfo(...)` without accesses still constructs."""
        info = SwcRunnableInfo(path="/P/S/B/R", name="R", min_start_interval=None, description="")
        assert info.accesses == []


# The mapper over the WRITER's own output


@pytest.fixture(scope="module")
def mapped_written_access_points(tmp_path_factory):
    """Return ``fixtures/access_points/model`` generated once, and mapped back.

    The mapper resolves what SWCraft itself writes: exporter and importer agree, asserted rather than assumed.
    """
    output = tmp_path_factory.mktemp("access-points-map") / "model.arxml"
    generate_arxml(str(ACCESS_POINTS_MODEL_ROOT), str(output))
    model = read_arxml(str(output))
    forms: list = []
    data_types, registry, dt_skips = map_data_types(model, elements=forms)
    interfaces, iface_skips = map_interfaces(model, registry, data_types, elements=forms)
    swcs, swc_skips = map_swcs(model, interfaces, iface_skip_log=iface_skips, port_inits={}, variable_types={})
    return swcs, [*model.skip_log, *dt_skips, *iface_skips, *swc_skips]


class TestMapsTheWritersOwnAccessPoints:
    def test_nothing_is_skipped_mapping_a_document_swcraft_wrote(self, mapped_written_access_points) -> None:
        swcs, skips = mapped_written_access_points

        assert [f"{e.tag} at {e.path}: {e.reason}" for e in skips] == []
        assert sorted(imported.swc.name for imported in swcs) == ["AccessSwc", "ServerSwc"]

    def test_the_three_lists_map_back_with_the_by_value_read_as_by_value(self, mapped_written_access_points) -> None:
        """The three lists map back with the by-value read as by-value, signalled by its container alone."""
        swcs, _ = mapped_written_access_points
        runnable = next(r for r in _by_swc_name(swcs, "AccessSwc").swc.runnables if r.name == "AccessMain")

        assert [(r.port, r.element, r.by) for r in runnable.reads] == [
            ("rSignals", "speed", None),
            ("rSignals", "level", "value"),
        ]
        assert [(w.port, w.element) for w in runnable.writes] == [("pSignals", "speed")]
        assert [(c.port, c.operation) for c in runnable.calls] == [("rControl", "Apply")]

    def test_nothing_is_collapsed(self, mapped_written_access_points) -> None:
        """Nothing is collapsed: the writer emits each declared entry once."""
        swcs, _ = mapped_written_access_points

        assert [line for imported in swcs for line in imported.access_collapses] == []

    def test_the_runnable_that_declares_nothing_maps_back_declaring_nothing(self, mapped_written_access_points) -> None:
        """A non-vacuity control: the runnable declaring nothing maps back declaring nothing."""
        swcs, _ = mapped_written_access_points
        quiet = next(r for r in _by_swc_name(swcs, "AccessSwc").swc.runnables if r.name == "QuietRun")

        assert (quiet.reads, quiet.writes, quiet.calls) == ([], [], [])


# A TIMING-EVENT PERIOD is an ``xsd:double`` (``TIME-VALUE``, XSD R4.3.0:88606;
# R4.2.2:76972): a Python-only spelling of a number is no PERIOD, and the event
# is skipped with a line, never imported as the number ``float()`` reads.


@pytest.mark.parametrize("text", ["1_000", "\u00a00.01\u00a0", "\uff11", "infinity"])
def test_a_period_outside_the_xsd_double_lexical_space_is_reported(tmp_path, text):
    source = tmp_path / "period.arxml"
    original = MINIMAL_SWC.read_text(encoding="utf-8")
    assert original.count("<PERIOD>0.01</PERIOD>") == 1
    source.write_text(
        original.replace("<PERIOD>0.01</PERIOD>", f"<PERIOD>{text}</PERIOD>"), encoding="utf-8", newline="\n"
    )
    model = read_arxml(str(source))
    (event,) = model.swcs[0].events
    _, _, swcs, swc_skip_log = _map_full(model)
    assert [e for r in swcs[0].swc.runnables for e in r.events] == []
    assert [(entry.path, entry.tag) for entry in swc_skip_log if entry.tag == "TIMING-EVENT"] == [
        (event.path, "TIMING-EVENT")
    ]
    # The line says the text is no number, never that the PERIOD is absent.
    (entry,) = [entry for entry in swc_skip_log if entry.tag == "TIMING-EVENT"]
    assert entry.reason.startswith(f"PERIOD {text!r} not carried: the text could not be read as a number")


def test_an_absent_period_still_says_so(tmp_path):
    source = tmp_path / "period.arxml"
    source.write_text(
        MINIMAL_SWC.read_text(encoding="utf-8").replace("<PERIOD>0.01</PERIOD>", ""), encoding="utf-8", newline="\n"
    )
    _, _, _, swc_skip_log = _map_full(read_arxml(str(source)))
    assert [entry.reason for entry in swc_skip_log if entry.tag == "TIMING-EVENT"] == ["timing event has no PERIOD"]


# A MINIMUM-START-INTERVAL is a ``TIME-VALUE`` too, and the model always holds an interval (default 0.0): a
# text no finite number reads is never that 0. The runnable is skipped with a line, its accesses and events
# with it.


@pytest.mark.parametrize("text", ["1_000", "\u00a00.01\u00a0", "\uff11", "INF", ""])
def test_an_unreadable_minimum_start_interval_is_never_zero(tmp_path, text):
    source = tmp_path / "interval.arxml"
    original = MINIMAL_SWC.read_text(encoding="utf-8")
    spelled = "<MINIMUM-START-INTERVAL>0.01</MINIMUM-START-INTERVAL>"
    assert original.count(spelled) == 1
    source.write_text(
        original.replace(spelled, f"<MINIMUM-START-INTERVAL>{text}</MINIMUM-START-INTERVAL>"),
        encoding="utf-8",
        newline="\n",
    )
    model = read_arxml(str(source))
    (runnable,) = model.swcs[0].runnables
    assert (runnable.min_start_interval, runnable.unreadable_min_start_interval) == (None, text)
    _, _, swcs, swc_skip_log = _map_full(model)
    assert swcs[0].swc.runnables == []
    (entry,) = [entry for entry in swc_skip_log if entry.path == runnable.path]
    assert (entry.tag, entry.effect, entry.category) == ("RUNNABLE-ENTITY", "abort", SkipCategory.NOT_SUPPORTED)
    assert entry.reason.startswith(
        f"MINIMUM-START-INTERVAL {text!r} not carried: the text could not be read as a number"
    )


def test_an_absent_minimum_start_interval_is_still_the_default(tmp_path):
    source = tmp_path / "interval.arxml"
    source.write_text(
        MINIMAL_SWC.read_text(encoding="utf-8").replace("<MINIMUM-START-INTERVAL>0.01</MINIMUM-START-INTERVAL>", ""),
        encoding="utf-8",
        newline="\n",
    )
    model = read_arxml(str(source))
    assert model.swcs[0].runnables[0].unreadable_min_start_interval is None
    _, _, swcs, _ = _map_full(model)
    assert [r.min_start_interval for r in swcs[0].swc.runnables] == [0.0]


@pytest.mark.parametrize(
    ("text", "expected"), [("1_000", None), ("\u00a00.5", None), ("infinity", None), (" 0.5\n", 0.5)]
)
def test_an_alive_timeout_is_an_xsd_double(text, expected):
    assert _com_spec_value("ALIVE-TIMEOUT", text) == expected
