"""Tests for resolve.reference_candidates: the nearest names of a dangling reference, by target shape."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import swcraft_tools.editing.registry as reg
from swcraft_tools.editing.diagnosis import RULE_CODES
from swcraft_tools.editing.resolve import ROLE_FIELD_LAYERS, ROLE_LAYERS, Role, reference_candidates


def _member(name: str, **facts: Any) -> SimpleNamespace:
    return SimpleNamespace(name=name, **facts)


#: A snapshot the resolver reads by layer name out of its ``state``: two components with a port
#: each, two compositions with a port each, two hardware elements of different kinds, requirements
#: whose ids hold a ``.``, and one type of each data-type layer spelled alike.
_SNAPSHOT = SimpleNamespace(
    state={
        "swc": [
            SimpleNamespace(name="Sensor", ports=[_member("pTemperature")], runnables=[_member("Sample")]),
            SimpleNamespace(name="Display", ports=[_member("rTemperature")], runnables=[_member("Show")]),
        ],
        "interfaces": [SimpleNamespace(name="ITemp", data_elements=[_member("value")], operations=[])],
        "compositions": [
            SimpleNamespace(name="Cluster", ports=[_member("rTemperatureIn")], members=[], delegations=[]),
            SimpleNamespace(name="Body", ports=[_member("rTemperatures")], members=[], delegations=[]),
        ],
        "hardware": [SimpleNamespace(name="Core0", kind="core"), SimpleNamespace(name="Core1x", kind="partition")],
        "requirements": [SimpleNamespace(id="SYS.REQ_BRAKE"), SimpleNamespace(id="SYS.REQ_SPEED")],
        "application_types": [SimpleNamespace(name="Speed")],
        "implementation_types": [SimpleNamespace(name="uint8")],
        "base_types": [SimpleNamespace(name="uint8_base")],
    }
)


class TestReferenceCandidates:
    """The nearest names of a dangling reference of each target shape (``resolve.reference_candidates``)."""

    @pytest.mark.parametrize(
        ("group", "target", "sentence"),
        [
            pytest.param(
                "connector-port",
                "Sensor.pTemperatur",
                "Sensor.ports holds no 'pTemperatur'; nearest: pTemperature.",
                id="owner-dot-name",
            ),
            pytest.param(
                "connector-port",
                "Sensor.rTemperatur",
                "Sensor.ports holds no 'rTemperatur'; nearest: pTemperature.",
                id="a-port-of-another-owner-is-not-offered",
            ),
            pytest.param(
                "task-mapping-runnable",
                "Display.Shw",
                "Display.runnables holds no 'Shw'; nearest: Show.",
                id="task-mapping-runnable",
            ),
            pytest.param(
                "sequence-step-runnable",
                "T: Display.Shw",
                "Display.runnables holds no 'Shw'; nearest: Show.",
                id="task-prefix",
            ),
            pytest.param(
                "access-element",
                "ITemp.valu",
                "ITemp.data_elements holds no 'valu'; nearest: value.",
                id="access-element",
            ),
            pytest.param(
                "composition-delegation-port",
                "rTemperatur",
                "nearest in compositions.ports: rTemperatures, rTemperatureIn.",
                id="an-outer-port-with-no-role-reads-every-compositions-ports",
            ),
            pytest.param(
                "requirement",
                "SYS.REQ_BRAKF@2",
                "requirements holds no 'SYS.REQ_BRAKF'; nearest: SYS.REQ_BRAKE.",
                id="a-dotted-requirement-id-and-a-version",
            ),
            pytest.param(
                "trace.derived-from-unknown-target",
                "SYS.REQ_SPEEDS",
                "requirements holds no 'SYS.REQ_SPEEDS'; nearest: SYS.REQ_SPEED.",
                id="derived-from-a-requirement",
            ),
            pytest.param(
                "trace.derived-from-unknown-target",
                "swc:Sensr",
                "swc holds no 'Sensr'; nearest: Sensor.",
                id="derived-from-an-element",
            ),
            pytest.param(
                "core",
                "Core1",
                "hardware (core) holds no 'Core1'; nearest: Core0.",
                id="a-core-is-never-near-a-partition",
            ),
            pytest.param(
                "data-type",
                "Zebra",
                "application_types or implementation_types or base_types holds no 'Zebra' and nothing near it.",
                id="data-type",
            ),
            pytest.param("sequence-step-mapping", "T: Display.Show", "", id="exempt"),
        ],
    )
    def test_the_sentence(self, group: str, target: str, sentence: str) -> None:
        assert reference_candidates(_SNAPSHOT, group, target, reg.current_registry()) == sentence

    @pytest.mark.parametrize(
        ("roles", "sentence"),
        [
            pytest.param(
                (("data-type.value-type-undeclared-base", "base", "uint16"),),
                "base_types holds no 'uint8'; nearest: uint8_base.",
                id="a-value-type-base-reads-the-base-types-alone",
            ),
            pytest.param(
                (("interface.data-element-undeclared-type", "application_type", "ITemp"),),
                "application_types holds no 'uint8' and nothing near it.",
                id="a-prototype-reads-the-layer-of-its-field",
            ),
            pytest.param(
                (
                    ("data-type.value-type-undeclared-base", "base", "uint16"),
                    ("data-type.record-field-undeclared-type", "type", "Record"),
                ),
                "base_types or application_types holds no 'uint8'; nearest: uint8_base.",
                id="two-roles-read-both-their-layers",
            ),
            pytest.param(
                (("interface.data-element-undeclared-type", None, "ITemp"),),
                "application_types or implementation_types or base_types holds no 'uint8'; nearest: uint8_base.",
                id="a-role-with-no-layer-of-its-own-keeps-the-group-pool",
            ),
        ],
    )
    def test_a_data_type_reference_is_searched_in_the_layer_its_role_reads(
        self, roles: tuple[Role, ...], sentence: str
    ) -> None:
        """The ``data-type`` group spans three namespaces; a role resolves in one of them."""
        assert reference_candidates(_SNAPSHOT, "data-type", "uint8", reg.current_registry(), roles) == sentence

    @pytest.mark.parametrize(
        ("roles", "sentence"),
        [
            pytest.param(
                (("composition.delegation-unknown-port", "port", "Cluster"),),
                "Cluster.ports holds no 'rTemperatur'; nearest: rTemperatureIn.",
                id="its-own-compositions-ports-alone",
            ),
            pytest.param(
                (("composition.delegation-unknown-port", "port", "Cluster"),)
                + (("composition.delegation-unknown-port", "port", "Body"),),
                "nearest in compositions.ports: rTemperatures, rTemperatureIn.",
                id="two-compositions-read-every-compositions-ports",
            ),
        ],
    )
    def test_a_delegations_outer_port_is_searched_among_its_compositions_ports(
        self, roles: tuple[Role, ...], sentence: str
    ) -> None:
        """Never a component's port: an outer port resolves on its composition alone."""
        assert (
            reference_candidates(_SNAPSHOT, "composition-delegation-port", "rTemperatur", reg.current_registry(), roles)
            == sentence
        )

    @pytest.mark.parametrize(
        ("group", "role", "declared", "sentence"),
        [
            pytest.param(
                "connector-port",
                ("connector.endpoint-unknown-port", "port", "C1"),
                "swc",
                "Twin.ports holds no 'pSpeedOu'; nearest: pSpeed.",
                id="a-connector-end-naming-a-component",
            ),
            pytest.param(
                "connector-port",
                ("connector.endpoint-unknown-port", "port", "C1"),
                "composition",
                "Twin.ports holds no 'pSpeedOu'; nearest: pSpeedOut.",
                id="a-connector-end-naming-a-composition",
            ),
            pytest.param(
                "composition-delegation-port",
                ("composition.delegation-unknown-port", "member_port", "Top"),
                "type",
                "Twin.ports holds no 'pSpeedOu'; nearest: pSpeed.",
                id="a-delegations-member-of-component-type",
            ),
            pytest.param(
                "composition-delegation-port",
                ("composition.delegation-unknown-port", "member_port", "Top"),
                "composition",
                "Twin.ports holds no 'pSpeedOu'; nearest: pSpeedOut.",
                id="a-delegations-member-of-composition-type",
            ),
            pytest.param(
                "connector-port",
                ("connector.endpoint-unknown-port", "port", "Elsewhere"),
                "swc",
                "Twin.ports holds no 'pSpeedOu'; nearest: pSpeedOut, pSpeed.",
                id="no-referrer-in-the-snapshot-keeps-both-layers",
            ),
        ],
    )
    def test_a_typed_port_is_searched_in_the_layer_its_end_declares(
        self, group: str, role: Role, declared: str, sentence: str
    ) -> None:
        """A component and a composition may share a name; the referring end's type field picks one."""
        end = {"composition" if declared == "composition" else "swc": "Twin", "port": "pSpeedOu"}
        member = {"type" if declared in ("swc", "type") else "composition": "Twin"}
        snapshot = SimpleNamespace(
            state={
                "swc": [SimpleNamespace(name="Twin", ports=[_member("pSpeed")], runnables=[])],
                "compositions": [
                    SimpleNamespace(name="Twin", ports=[_member("pSpeedOut")], members=[], delegations=[]),
                    SimpleNamespace(
                        name="Top",
                        ports=[],
                        members=[_member("m", **member)],
                        delegations=[_member("d", port="x", member="m", member_port="pSpeedOu")],
                    ),
                ],
                "connectors": [
                    SimpleNamespace(name="C1", from_=SimpleNamespace(**end), to=SimpleNamespace(swc="X", port="y"))
                ],
            }
        )
        assert reference_candidates(snapshot, group, "Twin.pSpeedOu", reg.current_registry(), (role,)) == sentence

    @pytest.mark.parametrize(
        ("roles", "sentence"),
        [
            pytest.param(
                (("composition.delegation-unknown-member", "member", "Cluster"),),
                "Cluster.members holds no 'Sensr'; nearest: Sensor.",
                id="its-own-compositions-members-alone",
            ),
            pytest.param((), "nearest in compositions.members: Sensor, Sensors.", id="no-role-reads-every-composition"),
        ],
    )
    def test_a_delegations_member_is_searched_among_its_compositions_members(
        self, roles: tuple[Role, ...], sentence: str
    ) -> None:
        snapshot = SimpleNamespace(
            state={
                "compositions": [
                    SimpleNamespace(name="Cluster", ports=[], members=[_member("Sensor")], delegations=[]),
                    SimpleNamespace(name="Body", ports=[], members=[_member("Sensors")], delegations=[]),
                ]
            }
        )
        assert (
            reference_candidates(snapshot, "composition-delegation-member", "Sensr", reg.current_registry(), roles)
            == sentence
        )

    def test_every_data_type_rule_has_a_role(self) -> None:
        rules = {rule for rule, mapping in RULE_CODES.items() if mapping.group == "data-type"}
        assert rules == set(ROLE_LAYERS) | set(ROLE_FIELD_LAYERS)
