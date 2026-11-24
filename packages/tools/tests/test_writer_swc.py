"""Tests for writer/mutations.py: create_swc, add_port, create_interface, create_connector."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import model_element_path
from pydantic import ValidationError
from swcraft_tools.auditor import AuditLog
from swcraft_tools.reader import read_connectors, read_swc_model
from swcraft_tools.writer import add_port, create_connector, create_interface, create_swc

# ---------------------------------------------------------------------------
# create_swc
# ---------------------------------------------------------------------------


def test_create_swc_creates_yaml(tmp_path):
    path = create_swc(str(tmp_path), "FooSWC", group="body")
    expected = tmp_path / "swc" / "body" / "FooSWC.yaml"
    assert expected.exists(), f"Expected {expected} to exist"
    assert path == str(expected)

    swcs = read_swc_model(str(tmp_path))
    assert len(swcs) == 1
    assert swcs[0].name == "FooSWC"
    assert swcs[0].type == "application"


def test_create_swc_with_explicit_type(tmp_path):
    create_swc(str(tmp_path), "SvcSWC", group="services", swc_type="service")
    swcs = read_swc_model(str(tmp_path))
    assert swcs[0].type == "service"


def test_create_swc_duplicate_raises(tmp_path):
    create_swc(str(tmp_path), "FooSWC", group="body")
    with pytest.raises(ValueError, match="FooSWC"):
        create_swc(str(tmp_path), "FooSWC", group="body")


def test_create_swc_empty_name_raises(tmp_path):
    with pytest.raises(ValueError):
        create_swc(str(tmp_path), "", group="body")


def test_create_swc_invalid_type_raises(tmp_path):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        create_swc(str(tmp_path), "FooSWC", group="body", swc_type="bogus")


class TestCreateSwcRefusesReservedNames:
    """The write tool must refuse what the ARXML writer will refuse.

    ``arxml_generation._check_component_names()`` rejects these at generation time;
    a write that accepted them would surface the failure later, in an unrelated
    ``validate``/``build`` naming a file the caller had not touched.
    """

    def test_the_old_composition_name_is_an_ordinary_swc_name(self, tmp_path):
        """``SwCraftComposition`` is an ordinary component name.

        Every composition is a declared element and the writer invents none, so the
        name mints nothing; refusing it would make this guard stricter than the
        writer it agrees with. Accepting it is asserted because it is the behaviour
        that once regressed.
        """
        create_swc(str(tmp_path), "SwCraftComposition", group="body")

        assert [swc.name for swc in read_swc_model(str(tmp_path))] == ["SwCraftComposition"]

    def test_an_implementation_twin_of_an_existing_swc_is_refused(self, tmp_path):
        create_swc(str(tmp_path), "Foo", group="body")
        with pytest.raises(ValueError, match="auto-generated SWC-IMPLEMENTATION name"):
            create_swc(str(tmp_path), "Foo_Implementation", group="body")

    def test_the_reverse_order_is_refused_too(self, tmp_path):
        """Either order produces the same collision, so both are guarded."""
        create_swc(str(tmp_path), "Foo_Implementation", group="body")
        with pytest.raises(ValueError, match="collides with the existing SWC"):
            create_swc(str(tmp_path), "Foo", group="body")

    def test_an_implementation_suffix_with_no_twin_is_allowed(self, tmp_path):
        """``Bar_Implementation`` alone collides with nothing; refusing it would reject a legal name."""
        create_swc(str(tmp_path), "Bar_Implementation", group="body")
        assert [swc.name for swc in read_swc_model(str(tmp_path))] == ["Bar_Implementation"]

    def test_an_internal_behavior_twin_is_allowed(self, tmp_path):
        """That element is nested inside its own component, so both paths resolve."""
        create_swc(str(tmp_path), "B", group="body")
        create_swc(str(tmp_path), "B_InternalBehavior", group="body")
        assert len(read_swc_model(str(tmp_path))) == 2


class TestCreateSwcDeclaresTheTerminationHandling:
    """The field is required; ``create_swc`` takes it as an appended optional keyword."""

    @pytest.mark.parametrize("value", ["no_support", "can_be_terminated", "can_be_terminated_and_restarted"])
    def test_a_value_is_written_last_and_audited(self, tmp_path, value):
        auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
        path = create_swc(str(tmp_path), "FooSWC", "body", "application", auditor, None, value)

        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        assert document["handle_termination_and_restart"] == value
        assert read_swc_model(str(tmp_path))[0].handle_termination_and_restart == value
        assert auditor.get_entries()[0]["args"]["handle_termination_and_restart"] == value

    def test_absent_it_writes_no_key_and_chooses_no_value(self, tmp_path):
        path = create_swc(str(tmp_path), "FooSWC", group="body")
        assert "handle_termination_and_restart" not in yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    def test_a_value_outside_the_three_is_refused_with_no_file(self, tmp_path):
        with pytest.raises(ValidationError):
            create_swc(str(tmp_path), "FooSWC", group="body", handle_termination_and_restart="restart")
        assert not (tmp_path / "swc").exists() or not any((tmp_path / "swc").rglob("*.yaml"))


def test_create_swc_with_auditor(tmp_path):
    auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
    create_swc(str(tmp_path), "FooSWC", group="body", auditor=auditor)
    entries = auditor.get_entries()
    assert len(entries) == 1
    assert entries[0]["operation"] == "create_swc"


# ---------------------------------------------------------------------------
# add_port
# ---------------------------------------------------------------------------


def _make_swc(tmp_path, name="FooSWC", group="body"):
    """Create a SWC and return its path."""
    return create_swc(str(tmp_path), name, group=group)


def test_add_port_updates_yaml(tmp_path):
    _make_swc(tmp_path)
    result = add_port(str(tmp_path), "FooSWC", "pOut", "provide", "IFoo")
    swcs = read_swc_model(str(tmp_path))
    assert len(swcs) == 1
    assert len(swcs[0].ports) == 1
    port = swcs[0].ports[0]
    assert port.name == "pOut"
    assert port.direction == "provide"
    assert port.interface == "IFoo"
    assert result is not None


def test_add_port_duplicate_raises(tmp_path):
    _make_swc(tmp_path)
    add_port(str(tmp_path), "FooSWC", "pOut", "provide", "IFoo")
    with pytest.raises(ValueError, match="pOut"):
        add_port(str(tmp_path), "FooSWC", "pOut", "provide", "IFoo")


def test_add_port_invalid_direction_raises(tmp_path):
    _make_swc(tmp_path)
    with pytest.raises(ValueError):
        add_port(str(tmp_path), "FooSWC", "pX", "invalid", "IFoo")


def test_add_port_nonexistent_swc_raises(tmp_path):
    with pytest.raises(ValueError, match="NonExistent"):
        add_port(str(tmp_path), "NonExistent", "pX", "provide", "IFoo")


def test_add_port_multiple_ports(tmp_path):
    _make_swc(tmp_path)
    add_port(str(tmp_path), "FooSWC", "pOut", "provide", "IFoo")
    add_port(str(tmp_path), "FooSWC", "pIn", "require", "IBar")
    swcs = read_swc_model(str(tmp_path))
    assert len(swcs[0].ports) == 2


def test_add_port_with_auditor(tmp_path):
    auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
    _make_swc(tmp_path)
    add_port(str(tmp_path), "FooSWC", "pOut", "provide", "IFoo", auditor=auditor)
    entries = auditor.get_entries()
    assert len(entries) == 1
    assert entries[0]["operation"] == "add_port"


# ---------------------------------------------------------------------------
# create_interface
# ---------------------------------------------------------------------------


def test_create_interface_creates_yaml(tmp_path):
    path = create_interface(str(tmp_path), "ISpeed", [{"name": "speed", "implementation_type": "float32"}])
    expected = tmp_path / "interfaces" / "ISpeed.yaml"
    assert expected.exists(), f"Expected {expected} to exist"
    assert path == str(expected)

    data = yaml.safe_load(expected.read_text(encoding="utf-8"))
    assert data["name"] == "ISpeed"
    assert data["kind"] == "sender_receiver"
    assert len(data["data_elements"]) == 1
    assert data["data_elements"][0]["name"] == "speed"
    assert data["data_elements"][0]["implementation_type"] == "float32"


def test_create_interface_multiple_elements(tmp_path):
    create_interface(
        str(tmp_path),
        "IMulti",
        [{"name": "speed", "implementation_type": "float32"}, {"name": "torque", "implementation_type": "float32"}],
    )
    data = yaml.safe_load((tmp_path / "interfaces" / "IMulti.yaml").read_text(encoding="utf-8"))
    assert len(data["data_elements"]) == 2


def test_create_interface_duplicate_raises(tmp_path):
    elements = [{"name": "speed", "implementation_type": "float32"}]
    create_interface(str(tmp_path), "ISpeed", elements)
    with pytest.raises(ValueError, match="ISpeed"):
        create_interface(str(tmp_path), "ISpeed", elements)


def test_create_interface_with_auditor(tmp_path):
    auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
    create_interface(
        str(tmp_path),
        "ISpeed",
        [{"name": "speed", "implementation_type": "float32"}],
        auditor=auditor,
    )
    entries = auditor.get_entries()
    assert len(entries) == 1
    assert entries[0]["operation"] == "create_interface"


def test_create_interface_with_description_is_emitted(tmp_path):
    """SRInterface.description round-trips through the YAML."""
    create_interface(
        str(tmp_path),
        "ISpeed",
        [{"name": "speed", "implementation_type": "float32"}],
        description="Wheel speed sender/receiver interface.",
    )
    data = yaml.safe_load((tmp_path / "interfaces" / "ISpeed.yaml").read_text(encoding="utf-8"))
    assert data["description"] == "Wheel speed sender/receiver interface."

    from swcraft_tools.reader import read_interfaces

    ifaces = read_interfaces(str(tmp_path))
    assert ifaces[0].description == "Wheel speed sender/receiver interface."


def test_create_interface_writes_the_init_value_it_was_given(tmp_path):
    """An `init` passed through lands in the FILE.

    Asserted on the file's bytes and read back through `read_interfaces`, not on the
    `SRInterface` the writer built: the dump dict in `create_interface` is
    hand-written, so a value that never reaches a key there would be erased behind a
    valid model object.
    """
    create_interface(
        str(tmp_path),
        "IInits",
        [
            {"name": "counter", "implementation_type": "uint8", "init": 255},
            {"name": "point", "implementation_type": "Point", "init": {"x": 1, "y": 2}},
            {"name": "triple", "implementation_type": "Triple", "init": [0, 1, 16]},
            {"name": "mode", "implementation_type": "Gear", "init": "GEAR_NEUTRAL"},
        ],
    )
    written = (tmp_path / "interfaces" / "IInits.yaml").read_text(encoding="utf-8")
    assert "  init: 255\n" in written
    assert "  init:\n    x: 1\n    y: 2\n" in written
    assert "  init:\n  - 0\n  - 1\n  - 16\n" in written
    assert "  init: GEAR_NEUTRAL\n" in written

    from swcraft_tools.reader import read_interfaces

    elements = read_interfaces(str(tmp_path))[0].data_elements
    assert [element.init for element in elements] == [255, {"x": 1, "y": 2}, [0, 1, 16], "GEAR_NEUTRAL"]


def test_create_interface_without_init_omits_the_key(tmp_path):
    """No `init` -> no key, so an interface without one is rewritten byte-identically, not gaining a null."""
    create_interface(str(tmp_path), "ISpeed", [{"name": "speed", "implementation_type": "float32"}])
    written = (tmp_path / "interfaces" / "ISpeed.yaml").read_text(encoding="utf-8")
    assert "init" not in written
    assert (
        written
        == "name: ISpeed\nkind: sender_receiver\ndata_elements:\n- name: speed\n  implementation_type: float32\n"
    )


def test_create_interface_refuses_an_init_the_schema_rejects(tmp_path):
    """`init` is validated by `schema.DataElement`, so a non-finite float is refused and no file is written."""
    with pytest.raises(ValidationError, match="finite"):
        create_interface(
            str(tmp_path), "INaN", [{"name": "speed", "implementation_type": "float32", "init": float("nan")}]
        )
    assert not (tmp_path / "interfaces" / "INaN.yaml").exists()


def test_create_interface_without_description_omits_key(tmp_path):
    """No description -> no key in the YAML, and interface files without it still load."""
    create_interface(str(tmp_path), "ISpeed", [{"name": "speed", "implementation_type": "float32"}])
    data = yaml.safe_load((tmp_path / "interfaces" / "ISpeed.yaml").read_text(encoding="utf-8"))
    assert "description" not in data

    from swcraft_tools.reader import read_interfaces

    ifaces = read_interfaces(str(tmp_path))
    assert ifaces[0].description == ""


# ---------------------------------------------------------------------------
# create_swc with real AuditLog
# ---------------------------------------------------------------------------


def test_create_swc_with_real_audit_log(tmp_path):
    """create_swc writes exactly 1 entry with operation == 'create_swc' to AuditLog."""
    audit_log = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
    create_swc(str(tmp_path), "AuditedSWC", group="body", auditor=audit_log)
    entries = audit_log.get_entries()
    assert len(entries) == 1
    assert entries[0]["operation"] == "create_swc"


# ---------------------------------------------------------------------------
# create_connector
# ---------------------------------------------------------------------------


def _make_connected_swcs(tmp_path, provider_interface="ISpeed", requirer_interface="ISpeed"):
    create_swc(str(tmp_path), "ProviderSWC", group="body")
    add_port(str(tmp_path), "ProviderSWC", "pOut", "provide", provider_interface)
    create_swc(str(tmp_path), "RequirerSWC", group="body")
    add_port(str(tmp_path), "RequirerSWC", "rIn", "require", requirer_interface)


def _declare_sr_interface(tmp_path, name, *elements, type_name="uint8"):
    """Write one sender-receiver interface file carrying *elements*, all of one type.

    ``create_connector``'s compatibility check resolves the two interfaces instead
    of comparing names, and an undeclared interface is an abstention
    (``port.undeclared-interface``'s finding), so a case about an incompatible pair
    has to declare the pair. Cases about what the interfaces *hold* name a
    *type_name* of their own.
    """
    directory = tmp_path / "interfaces"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "kind": "sender_receiver",
                "data_elements": [{"name": element, "implementation_type": type_name} for element in elements],
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )


def _declare_two_spellings_of_one_type(tmp_path):
    """``Count`` and ``Tally``: one structure under two names, over one base type.

    Compatible **after resolution** and incomparable by name. Declaring the types
    keeps the compatible case from passing vacuously: an unresolvable type is an
    abstention, judged compatible without the resolver deciding anything.
    """
    for document in (
        {"name": "u8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
        {"name": "Count", "kind": "value_type", "base": "u8_base"},
        {"name": "Tally", "kind": "value_type", "base": "u8_base"},
    ):
        # The payload's kind picks the layer directory (a base type: data_types/base/).
        path = model_element_path(tmp_path, document["name"], kind=document["kind"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(document, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
            newline="\n",
        )


class TestCreateConnectorFollowsTheWidenedCondition:
    """``create_connector`` follows the resolved compatibility condition.

    It is the single-connector caller: it passes no maps and
    ``check_connector_compatibility`` reads the two layers itself. The refusal's
    message is pinned by ``test_create_connector_interface_mismatch_raises``; here a
    pair the standard joins is written, and one it does not leaves the model exactly
    as it was.
    """

    @staticmethod
    def _model(tmp_path, *, required_element):
        _make_connected_swcs(tmp_path, provider_interface="IStatusWide", requirer_interface="IStatusNarrow")
        _declare_two_spellings_of_one_type(tmp_path)
        _declare_sr_interface(tmp_path, "IStatusWide", "speed", "level", type_name="Count")
        _declare_sr_interface(tmp_path, "IStatusNarrow", required_element, type_name="Tally")

    def test_a_pair_the_standard_joins_is_written(self, tmp_path):
        self._model(tmp_path, required_element="speed")

        path = create_connector(str(tmp_path), "Status", "ProviderSWC", "pOut", "RequirerSWC", "rIn")

        assert path.endswith("Status.yaml")
        (connector,) = read_connectors(str(tmp_path))
        assert connector.name == "Status"
        # Non-vacuity, both halves: the two ports name two interfaces, so the
        # same-name short-circuit did not answer, and the shared element is
        # typed by two different declared names, so the resolver did.
        ports = {port.name: port.interface for swc in read_swc_model(str(tmp_path)) for port in swc.ports}
        assert ports == {"pOut": "IStatusWide", "rIn": "IStatusNarrow"}
        assert (model_element_path(tmp_path, "Count", kind="value_type")).is_file()
        assert (model_element_path(tmp_path, "Tally", kind="value_type")).is_file()

    def test_an_incompatible_pair_leaves_no_file_and_no_audit_entry(self, tmp_path):
        """The writer pattern's half of the refusal: nothing is written first, so it costs the model nothing."""
        self._model(tmp_path, required_element="torque")
        auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))

        with pytest.raises(ValueError, match="interface mismatch"):
            create_connector(str(tmp_path), "Status", "ProviderSWC", "pOut", "RequirerSWC", "rIn", auditor=auditor)

        assert read_connectors(str(tmp_path)) == []
        assert not (tmp_path / "connectors" / "Status.yaml").exists()
        assert auditor.get_entries() == []


def test_create_connector_creates_yaml(tmp_path):
    _make_connected_swcs(tmp_path)

    path = create_connector(
        str(tmp_path),
        "Provider_to_Requirer",
        "ProviderSWC",
        "pOut",
        "RequirerSWC",
        "rIn",
    )

    connectors = read_connectors(str(tmp_path))
    assert len(connectors) == 1
    assert connectors[0].name == "Provider_to_Requirer"
    assert connectors[0].from_.swc == "ProviderSWC"
    assert connectors[0].to.port == "rIn"
    assert path.endswith("Provider_to_Requirer.yaml")


def test_create_connector_duplicate_raises(tmp_path):
    _make_connected_swcs(tmp_path)
    create_connector(str(tmp_path), "Provider_to_Requirer", "ProviderSWC", "pOut", "RequirerSWC", "rIn")

    with pytest.raises(ValueError, match="Provider_to_Requirer"):
        create_connector(str(tmp_path), "Provider_to_Requirer", "ProviderSWC", "pOut", "RequirerSWC", "rIn")


def test_create_connector_nonexistent_swc_raises(tmp_path):
    _make_connected_swcs(tmp_path)

    with pytest.raises(ValueError, match="MissingSWC"):
        create_connector(str(tmp_path), "Bad", "MissingSWC", "pOut", "RequirerSWC", "rIn")


def test_create_connector_wrong_direction_raises(tmp_path):
    create_swc(str(tmp_path), "ProviderSWC", group="body")
    add_port(str(tmp_path), "ProviderSWC", "rOut", "require", "ISpeed")
    create_swc(str(tmp_path), "RequirerSWC", group="body")
    add_port(str(tmp_path), "RequirerSWC", "rIn", "require", "ISpeed")

    with pytest.raises(ValueError, match="must be a provide port"):
        create_connector(str(tmp_path), "Bad", "ProviderSWC", "rOut", "RequirerSWC", "rIn")


def test_create_connector_interface_mismatch_raises(tmp_path):
    """The write path refuses a pair the standard does not join.

    The two interfaces are **declared** because the check reads them: ``IThrottle``
    asks for a data element ``ISpeed`` does not offer. With undeclared names the
    check would abstain (``port.undeclared-interface`` reports that).
    """
    _make_connected_swcs(tmp_path, provider_interface="ISpeed", requirer_interface="IThrottle")
    _declare_sr_interface(tmp_path, "ISpeed", "speed")
    _declare_sr_interface(tmp_path, "IThrottle", "throttle")

    with pytest.raises(ValueError) as refusal:
        create_connector(str(tmp_path), "Bad", "ProviderSWC", "pOut", "RequirerSWC", "rIn")

    # The whole message: the subject is the predicate's own rendering, the same
    # sentence `check-model` prints for this pair.
    assert str(refusal.value) == (
        "Connector 'Bad' interface mismatch: required 'IThrottle' (RequirerSWC.rIn) "
        "is not satisfied by provided 'ISpeed' (ProviderSWC.pOut): "
        "data element 'throttle' has no counterpart in 'ISpeed'."
    )


def test_create_connector_writes_a_pair_whose_interface_is_not_declared(tmp_path):
    """The abstention on the write path: a pair naming one undeclared interface is written.

    An undeclared interface is ``rule:port.undeclared-interface``'s defect, so the
    compatibility check abstains. Deliberately not guarded here: a guard would make
    this write path stricter than ``check-model`` and pass D, a disagreement that
    must never happen.
    """
    from swcraft_tools.validator import check_port_interface_references

    _make_connected_swcs(tmp_path, provider_interface="ISpeed", requirer_interface="IThrottle")
    _declare_sr_interface(tmp_path, "ISpeed", "speed")

    path = create_connector(str(tmp_path), "Abstained", "ProviderSWC", "pOut", "RequirerSWC", "rIn")

    assert (tmp_path / "connectors" / "Abstained.yaml").is_file()
    assert path == str(tmp_path / "connectors" / "Abstained.yaml")
    # The rule that owns the defect, reporting it: one finding, about the port.
    assert [record.rule for record in check_port_interface_references(str(tmp_path)).records] == [
        "port.undeclared-interface"
    ]


def test_create_connector_with_auditor(tmp_path):
    _make_connected_swcs(tmp_path)
    auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))

    create_connector(
        str(tmp_path),
        "Provider_to_Requirer",
        "ProviderSWC",
        "pOut",
        "RequirerSWC",
        "rIn",
        auditor=auditor,
    )

    entries = auditor.get_entries()
    assert len(entries) == 1
    assert entries[0]["operation"] == "create_connector"
