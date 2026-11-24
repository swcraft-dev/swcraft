"""Tests for schema.py — Pydantic models."""

from __future__ import annotations

import math
from typing import get_args

import pytest
import yaml
from pydantic import BaseModel, TypeAdapter, ValidationError
from swcraft_tools import schema
from swcraft_tools.model_state import ElementParseError
from swcraft_tools.reader import read_tasks
from swcraft_tools.safety import ASIL_VALUES
from swcraft_tools.schema import (
    ArrayDataType,
    BaseDataType,
    Composition,
    Connector,
    Core,
    CSArgument,
    CSInterface,
    CSOperation,
    DataElement,
    DataReadRef,
    DataType,
    DataWriteRef,
    EnumDataType,
    EnumLiteral,
    InitEvent,
    Interface,
    Memory,
    ModelElement,
    OperationCallRef,
    Partition,
    Port,
    RecordDataType,
    RecordField,
    Requirement,
    Runnable,
    SRInterface,
    SWComponent,
    Task,
    TimingEvent,
    ValueDataType,
)
from typing_extensions import TypeAliasType

#: Spellings no rated `asil` field takes: wrong case, an invented prefix, a sixth
#: plain level, `A(A)` (which `[TPS_SAFEX_00201]` does not list), an `X` above
#: its `Y`, `QM` in parentheses, a space before the parenthesis and the empty
#: string.
UNLISTED_LEVELS = ["qm", "d", "ASIL_D", "E", "A(A)", "D(C)", "QM(QM)", "b(d)", "B (D)", ""]

# ---------------------------------------------------------------------------
# TimingEvent
# ---------------------------------------------------------------------------


def test_timing_event_valid():
    ev = TimingEvent(type="timing", period=0.01)
    assert ev.type == "timing"
    assert ev.period == pytest.approx(0.01)


def test_timing_event_period_zero_raises():
    with pytest.raises(ValidationError):
        TimingEvent(type="timing", period=0.0)


def test_timing_event_period_negative_raises():
    with pytest.raises(ValidationError):
        TimingEvent(type="timing", period=-0.01)


# ---------------------------------------------------------------------------
# InitEvent
# ---------------------------------------------------------------------------


def test_init_event_valid():
    ev = InitEvent(type="init")
    assert ev.type == "init"


# ---------------------------------------------------------------------------
# Runnable
# ---------------------------------------------------------------------------


def test_runnable_valid():
    r = Runnable(name="Run1")
    assert r.name == "Run1"
    assert r.min_start_interval == 0.0
    assert r.events == []


def test_runnable_negative_min_start_interval_raises():
    with pytest.raises(ValidationError):
        Runnable(name="Run1", min_start_interval=-1.0)


def test_runnable_zero_min_start_interval_ok():
    r = Runnable(name="Run1", min_start_interval=0.0)
    assert r.min_start_interval == 0.0


def test_runnable_with_events():
    ev = TimingEvent(type="timing", period=0.01)
    r = Runnable(name="Run1", min_start_interval=0.01, events=[ev])
    assert len(r.events) == 1


# ---------------------------------------------------------------------------
# Port
# ---------------------------------------------------------------------------


def test_port_valid():
    p = Port(name="p1", direction="require", interface="ISpeed")
    assert p.name == "p1"
    assert p.direction == "require"
    assert p.interface == "ISpeed"
    assert p.description == ""


def test_port_provide_direction():
    p = Port(name="pOut", direction="provide", interface="ISpeed")
    assert p.direction == "provide"


def test_port_invalid_direction_raises():
    with pytest.raises(ValidationError):
        Port(name="p1", direction="invalid", interface="ISpeed")


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


def test_connector_valid():
    connector = Connector.model_validate(
        {
            "name": "SpeedSensor_to_SpeedCtrl",
            "from": {"swc": "SpeedSensorSWC", "port": "pSpeed"},
            "to": {"swc": "SpeedCtrlSWC", "port": "rSpeed"},
        }
    )
    assert connector.name == "SpeedSensor_to_SpeedCtrl"
    assert connector.from_.swc == "SpeedSensorSWC"
    assert connector.to.port == "rSpeed"


def test_connector_empty_name_raises():
    with pytest.raises(ValidationError):
        Connector.model_validate(
            {
                "name": "",
                "from": {"swc": "SpeedSensorSWC", "port": "pSpeed"},
                "to": {"swc": "SpeedCtrlSWC", "port": "rSpeed"},
            }
        )


def test_connector_empty_endpoint_field_raises():
    with pytest.raises(ValidationError):
        Connector.model_validate(
            {
                "name": "BadConnector",
                "from": {"swc": "", "port": "pSpeed"},
                "to": {"swc": "SpeedCtrlSWC", "port": "rSpeed"},
            }
        )


@pytest.mark.parametrize("bad_name", ["../escape", "a/b", "a\\b"])
def test_connector_path_traversal_name_raises(bad_name):
    with pytest.raises(ValidationError):
        Connector.model_validate(
            {
                "name": bad_name,
                "from": {"swc": "SpeedSensorSWC", "port": "pSpeed"},
                "to": {"swc": "SpeedCtrlSWC", "port": "rSpeed"},
            }
        )


# ---------------------------------------------------------------------------
# SWComponent
# ---------------------------------------------------------------------------


def test_swcomponent_valid():
    swc = SWComponent(name="Foo", type="application")
    assert swc.name == "Foo"
    assert swc.type == "application"
    assert swc.ports == []
    assert swc.runnables == []


def test_swcomponent_empty_name_raises():
    with pytest.raises(ValidationError):
        SWComponent(name="", type="application")


def test_swcomponent_invalid_type_raises():
    with pytest.raises(ValidationError):
        SWComponent(name="Foo", type="invalid")


def test_swcomponent_default_type_is_application():
    swc = SWComponent(name="Foo")
    assert swc.type == "application"


def test_swcomponent_service_type():
    swc = SWComponent(name="Foo", type="service")
    assert swc.type == "service"


def test_swcomponent_ecu_abstraction_type():
    swc = SWComponent(name="Foo", type="ecu_abstraction")
    assert swc.type == "ecu_abstraction"


def test_swcomponent_with_port():
    port = Port(name="p1", direction="require", interface="ISpeed")
    swc = SWComponent(name="Foo", ports=[port])
    assert len(swc.ports) == 1


@pytest.mark.parametrize("bad_name", ["../escape", "a/b", "a\\b"])
def test_swcomponent_path_traversal_name_raises(bad_name):
    with pytest.raises(ValidationError):
        SWComponent(name=bad_name, type="application")


# ---------------------------------------------------------------------------
# SWComponent.memory — pool name -> bytes consumed
# ---------------------------------------------------------------------------


class TestSWComponentMemoryBudget:
    def test_memory_defaults_to_empty_map(self):
        swc = SWComponent(name="Foo")
        assert swc.memory == {}

    def test_memory_accepts_pool_to_bytes(self):
        swc = SWComponent(name="Foo", memory={"RAM": 2048, "ROM": 8192})
        assert swc.memory == {"RAM": 2048, "ROM": 8192}

    def test_memory_zero_bytes_allowed(self):
        swc = SWComponent(name="Foo", memory={"RAM": 0})
        assert swc.memory["RAM"] == 0

    def test_memory_negative_bytes_raises(self):
        with pytest.raises(ValidationError):
            SWComponent(name="Foo", memory={"RAM": -1})

    def test_memory_empty_pool_name_raises(self):
        with pytest.raises(ValidationError):
            SWComponent(name="Foo", memory={"": 2048})

    def test_memory_whitespace_pool_name_raises(self):
        with pytest.raises(ValidationError):
            SWComponent(name="Foo", memory={"   ": 2048})

    def test_memory_fractional_bytes_raises(self):
        with pytest.raises(ValidationError):
            SWComponent(name="Foo", memory={"RAM": 2048.5})

    def test_memory_from_yaml(self):
        raw = yaml.safe_load("name: Foo\nmemory:\n  RAM: 2048\n  ROM: 8192\n")
        swc = SWComponent.model_validate(raw)
        assert swc.memory == {"RAM": 2048, "ROM": 8192}

    def test_swc_yaml_without_memory_still_parses(self):
        data = yaml.safe_load(VALID_SWC_YAML)
        swc = SWComponent.model_validate(data)
        assert swc.memory == {}
        assert swc.name == "SpeedCtrlSWC"
        assert len(swc.ports) == 1


# ---------------------------------------------------------------------------
# Task.wcet — the task's budget, with its unit
# ---------------------------------------------------------------------------


_RETIRED_TASK_SENTENCE = (
    "'wcet_us' was renamed 'wcet' by contract 10.0.0 and now carries its unit: write 'wcet: 1200us' for 'wcet_us: 1200'"
)


class TestTaskWcetBudget:
    def test_wcet_defaults_to_none(self):
        task = Task(name="Task_10ms", period=0.01, priority=10, partition="PARTITION0")
        assert task.wcet is None

    @pytest.mark.parametrize("value", ["400us", "850ns", "2ms", "1s"])
    def test_wcet_is_carried_as_written(self, value):
        task = Task(name="Task_1ms", period=0.001, priority=30, partition="PARTITION2", wcet=value)
        assert task.wcet == value

    def test_wcet_zero_raises_the_positive_rule(self):
        with pytest.raises(ValidationError, match="wcet must be > 0"):
            Task(name="Task_1ms", period=0.001, priority=30, partition="PARTITION0", wcet="0us")

    @pytest.mark.parametrize(
        ("value", "sentence"),
        [
            (400, "wcet 400 names no unit: write it with one of ns, us, ms, s, e.g. '400us'"),
            (-5, "wcet -5 is not a budget: write a whole number with one of ns, us, ms, s, e.g. '400us'"),
            (400.5, "wcet 400.5 is not a budget: write a whole number with one of ns, us, ms, s, e.g. '1500us'"),
            (
                "1.5ms",
                "wcet '1.5ms' is not a whole number of ms (an ARXML cseCodeFactor is an Integer): write '1500us'",
            ),
            ("400", "wcet '400' names no unit: write it with one of ns, us, ms, s, e.g. '400us'"),
            ("850xs", "wcet '850xs' has an unknown unit 'xs': write one of ns, us, ms, s"),
            (
                "1234567890123456789ns",
                "wcet '1234567890123456789ns' has 19 digits: a budget is at most 18 digits of its unit "
                "(999999999999999999); write it in a coarser unit",
            ),
        ],
    )
    def test_the_format_rule_refuses_with_the_spelling_to_write(self, value, sentence):
        with pytest.raises(ValidationError) as caught:
            Task(name="Task_1ms", period=0.001, priority=30, partition="PARTITION0", wcet=value)
        (error,) = caught.value.errors()
        assert error["loc"] == ("wcet",)
        assert error["msg"] == f"Value error, {sentence}"

    def test_wcet_explicit_none_allowed(self):
        task = Task(name="Task_1ms", period=0.001, priority=30, partition="PARTITION0", wcet=None)
        assert task.wcet is None

    def test_wcet_from_yaml(self):
        raw = yaml.safe_load("name: Task_1ms\nperiod: 0.001\npriority: 30\npartition: PARTITION2\nwcet: 400us\n")
        assert Task.model_validate(raw).wcet == "400us"

    @pytest.mark.parametrize(
        "text",
        [
            "name: T\nperiod: 0.01\npriority: 1\npartition: P\nwcet_us: 1200\n",
            "name: T\nperiod: 0.01\npriority: 1\npartition: P\nwcet_us: 1200\nwcet: 1200us\n",
        ],
        ids=["retired", "both-keys"],
    )
    def test_the_retired_spelling_is_refused_with_the_file_s_number(self, text):
        with pytest.raises(ValidationError) as caught:
            Task.model_validate(yaml.safe_load(text))
        (error,) = caught.value.errors()
        assert error["msg"] == f"Value error, {_RETIRED_TASK_SENTENCE}"
        assert "wcet_type" not in error["msg"]

    @pytest.mark.parametrize("value", ["0", "-5", "12.5", "true", "'1200'"])
    def test_a_retired_value_that_is_no_positive_whole_number_is_never_quoted(self, value):
        """``wcet: 0us`` or ``wcet: -5us`` would be refused on the next load: the sentence says ``<value>``."""
        with pytest.raises(ValidationError) as caught:
            Task.model_validate(yaml.safe_load(f"name: T\nperiod: 0.01\npriority: 1\npartition: P\nwcet_us: {value}\n"))
        (error,) = caught.value.errors()
        assert "write 'wcet: <value>us' for 'wcet_us: <value>'" in error["msg"], error["msg"]

    def test_a_model_instance_passes_the_before_validator_untouched(self):
        task = Task(name="T", period=0.01, priority=1, partition="P", wcet="1200us")
        assert Task.model_validate(task) is task

    def test_task_yaml_without_wcet_still_parses(self):
        raw = yaml.safe_load(
            "name: Task_10ms\nperiod: 0.01\npriority: 10\npartition: PARTITION0\n"
            "runnables:\n  - swc: SpeedCtrlSWC\n    runnable: SpeedCtrl_Run\n"
        )
        task = Task.model_validate(raw)
        assert task.wcet is None
        assert task.partition == "PARTITION0"
        assert len(task.runnables) == 1


class TestTaskWcetThroughAFile:
    """The task's load rules as a user meets them: a written file, read by the one parser."""

    @pytest.mark.parametrize(
        ("line", "sentence"),
        [
            ("wcet_us: 1200", _RETIRED_TASK_SENTENCE),
            ("wcet: 1200", "wcet 1200 names no unit: write it with one of ns, us, ms, s, e.g. '1200us'"),
            ("wcet: 0ms", "wcet must be > 0"),
        ],
        ids=["retired-spelling", "format", "positive"],
    )
    def test_each_rule_refuses_the_file(self, tmp_path, line, sentence):
        path = tmp_path / "tasks" / "T.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(f"name: T\nperiod: 0.01\npriority: 1\npartition: P\n{line}\n", encoding="utf-8", newline="\n")
        with pytest.raises(ElementParseError) as caught:
            read_tasks(str(tmp_path))
        assert sentence in str(caught.value)
        assert str(tmp_path) not in str(caught.value)


# ---------------------------------------------------------------------------
# DataElement
# ---------------------------------------------------------------------------


def test_data_element_valid():
    de = DataElement(name="speed", implementation_type="float32")
    assert de.name == "speed"
    assert de.implementation_type == "float32"


class TestDataElementInit:
    """`init`: the optional initial value of a sender-receiver data element.

    The value's YAML shape follows the element's *declared* type, recursively —
    a scalar type takes a YAML scalar, a `record` a map keyed by its declared
    fields, an `array` a list of its declared length, a reference type its
    target's shape. `InitValue` is the union of those shapes and nothing
    narrower: **it does not relate the value to the declared type at all.**
    That conformance is `check-model`'s, and the editor schemas stay
    deliberately permissive on the field.
    """

    def test_absent_stays_none(self):
        assert DataElement(name="speed", implementation_type="float32").init is None

    def test_an_int_scalar_parses(self):
        de = DataElement(name="speed", implementation_type="uint16", init=0)
        assert de.init == 0
        assert type(de.init) is int

    def test_a_float_scalar_parses(self):
        de = DataElement(name="speed", implementation_type="float32", init=1.5)
        assert de.init == pytest.approx(1.5)
        assert type(de.init) is float

    def test_a_string_scalar_parses(self):
        """The enum spelling: the literal's *name*, never its number."""
        de = DataElement(name="gear", application_type="Gear_T", init="GEAR_NEUTRAL")
        assert de.init == "GEAR_NEUTRAL"
        assert type(de.init) is str

    def test_a_bool_stays_a_bool_and_is_not_coerced_to_one(self):
        """A `boolean`-typed element initialised `true` must not come back as `1`.

        What holds it is Pydantic v2's SMART union, which matches the exact
        type before it tries any coercion — **not** `bool` preceding `int` in
        the member list; :meth:`test_the_member_order_is_not_what_preserves_the_type`
        is the case that tells the two apart.
        """
        de = DataElement(name="active", implementation_type="boolean", init=True)
        assert de.init is True
        assert type(de.init) is bool

    def test_the_member_order_is_not_what_preserves_the_type(self):
        """Two recursive aliases of the members in opposite orders keep a boolean alike, on this Pydantic."""
        # The binding names match the alias names because each alias's members
        # are its own forward references, exactly as `schema.py` spells
        # `InitValue`; a mismatched pair would be an undefined name.
        Declared = TypeAliasType("Declared", "bool | int | float | str | list[Declared] | dict[str, Declared]")
        Swapped = TypeAliasType("Swapped", "int | bool | float | str | list[Swapped] | dict[str, Swapped]")

        for alias in (Declared, Swapped):
            adapter = TypeAdapter(alias)
            assert type(adapter.validate_python(True)) is bool, alias
            assert type(adapter.validate_python(1)) is int, alias

    def test_a_list_parses_for_an_array_type(self):
        de = DataElement(name="samples", application_type="Samples_T", init=[1, 2, 3])
        assert de.init == [1, 2, 3]

    def test_a_nested_map_parses_for_a_record_of_records(self):
        de = DataElement(name="pose", application_type="Pose_T", init={"pos": {"x": 1, "y": 2}, "valid": True})
        assert de.init == {"pos": {"x": 1, "y": 2}, "valid": True}

    def test_a_list_of_maps_parses_for_an_array_of_records(self):
        de = DataElement(name="points", implementation_type="Points_T", init=[{"x": 1}, {"x": 2}])
        assert de.init == [{"x": 1}, {"x": 2}]

    def test_a_list_of_lists_parses(self):
        de = DataElement(name="grid", application_type="Grid_T", init=[[1, 2], [3, 4]])
        assert de.init == [[1, 2], [3, 4]]

    def test_the_alias_is_a_named_recursive_type_alias(self):
        """Named, not a bare recursive union.

        The bare spelling raises `RecursionError` during Pydantic schema generation, and the name is what the type-name
        projection matches by identity to publish the field as `value` — an unrecognised annotation falls through to
        `"string"`, silently wrong."""
        assert isinstance(schema.InitValue, TypeAliasType)
        assert schema.InitValue.__name__ == "InitValue"

    def test_the_field_is_reachable_through_the_interface(self):
        iface = SRInterface(
            name="IGear",
            kind="sender_receiver",
            data_elements=[DataElement(name="gear", application_type="Gear_T", init="GEAR_NEUTRAL")],
        )
        assert iface.data_elements[0].init == "GEAR_NEUTRAL"


class TestInitValueRefusesNonFiniteFloatsAtEveryDepth:
    """`ModelElement`'s `allow_inf_nan=False` reaches *inside* the recursive union, at every depth.

    The union reports one error per member at every nesting level, so these tests
    assert *that* the rejection happens and that it is multi-part, never the exact
    error count, which is a Pydantic-internal function of the union's arity.

    **What this does not reach:** one interpreter on one platform. Nothing in
    this repository measures the behaviour on another Python or another
    Pydantic.
    """

    @pytest.mark.parametrize(
        "yaml_text",
        [
            "init: .nan",
            "init: .inf",
            "init: -.inf",
            "init: [1, .nan]",
            "init: [1, -.inf]",
            "init: {a: {b: .inf}}",
            "init: {a: {b: .nan}}",
            "init: [{a: .inf}]",
        ],
        ids=[
            "nan-at-depth-0",
            "inf-at-depth-0",
            "negative-inf-at-depth-0",
            "nan-in-a-list",
            "negative-inf-in-a-list",
            "inf-in-a-nested-map",
            "nan-in-a-nested-map",
            "inf-in-a-map-inside-a-list",
        ],
    )
    def test_it_is_refused(self, yaml_text):
        payload = yaml.safe_load(yaml_text)
        with pytest.raises(ValidationError) as exc_info:
            DataElement(name="speed", implementation_type="float32", **payload)
        assert "finite" in str(exc_info.value)
        assert len(exc_info.value.errors()) > 1, (
            "the union reports one error per member — if this ever drops to one, "
            "the message quality accepted as a known cost has changed"
        )

    def test_yaml_really_produces_a_non_finite_float_inside_a_collection(self):
        """Non-vacuity: PyYAML parses `.nan` inside a flow sequence as a float.

        Otherwise the rejections above would prove nothing."""
        value = yaml.safe_load("init: [1, .nan]")["init"][1]
        assert isinstance(value, float)
        assert not math.isfinite(value)

    def test_finite_values_at_every_depth_are_untouched(self):
        de = DataElement(name="speed", implementation_type="float32", init={"a": [0.0, -40.5], "b": 1})
        assert de.init == {"a": [0.0, -40.5], "b": 1}


def test_an_interface_carrying_init_values_re_serializes_byte_identically():
    """A Pydantic round trip over the QUOTING rule, on the bytes.

    The subject is `yaml.safe_dump(..., default_flow_style=False,
    sort_keys=False)` — the call every writer in the tree makes — over a
    validated `SRInterface`, so a file already in that dump form survives
    load → validate → dump unchanged and the quoted spellings come back
    quoted.

    **What this is not.** It is deliberately NOT an oracle for any writer's
    own dump shape: the dump below goes through `model_dump(exclude_none=True)`,
    which no product module calls, and the fixture carries `description: ''` --
    on the interface and on every data element -- which no writer emits (`create_interface` guards on truthiness and
    `element_to_dict` drops the `""` default — pinned by
    `test_create_interface_without_description_omits_key`). The writers' own
    shapes have their own oracles: `test_writer_swc.py`'s
    `test_create_interface_writes_the_init_value_it_was_given` and
    `test_arxml_import_cli.py`'s `TestInitValuesReachTheWrittenYaml`, which
    are compared against each other byte for byte.

    Two of the elements are the quoting rule: `ON` and `010` resolve to
    `True` and `8` under YAML 1.1 unless they are quoted, and `safe_dump`
    quotes them back, so a hand-authored file that quotes them is stable.
    """
    text = (
        "name: IGear\n"
        "kind: sender_receiver\n"
        "data_elements:\n"
        "- name: gear\n"
        "  implementation_type: Gear_T\n"
        "  init: GEAR_NEUTRAL\n"
        "  description: ''\n"
        "- name: lamp\n"
        "  implementation_type: OnOff_T\n"
        "  init: 'ON'\n"
        "  description: ''\n"
        "- name: code\n"
        "  implementation_type: Code_T\n"
        "  init: '010'\n"
        "  description: ''\n"
        "- name: speed\n"
        "  implementation_type: float32\n"
        "  init: 0.0\n"
        "  description: ''\n"
        "- name: flags\n"
        "  implementation_type: Flags_T\n"
        "  init:\n"
        "  - 1\n"
        "  - 2\n"
        "  - 3\n"
        "  description: ''\n"
        "- name: point\n"
        "  implementation_type: Point_T\n"
        "  init:\n"
        "    x: 1\n"
        "    y: 2\n"
        "  description: ''\n"
        "- name: plain\n"
        "  implementation_type: uint8\n"
        "  description: ''\n"
        "description: ''\n"
        # `model_dump` writes the flag at its default, which the product's
        # serializer omits (`omitAtDefault`); stated here so the text
        # round-trips through the dump this test compares.
        "is_service: false\n"
        # The trace links at their `[]` default, for the same reason.
        "satisfies: []\n"
    )
    loaded = yaml.safe_load(text)

    # Non-vacuity: the two quoted spellings really are the ones YAML 1.1 would
    # have resolved to a non-string.
    assert loaded["data_elements"][1]["init"] == "ON"
    assert yaml.safe_load("v: ON")["v"] is True
    assert loaded["data_elements"][2]["init"] == "010"
    assert yaml.safe_load("v: 010")["v"] == 8

    iface = SRInterface.model_validate(loaded)
    dumped = yaml.safe_dump(iface.model_dump(exclude_none=True), default_flow_style=False, sort_keys=False)
    assert dumped == text


# ---------------------------------------------------------------------------
# SRInterface
# ---------------------------------------------------------------------------


def test_sr_interface_valid():
    de = DataElement(name="speed", implementation_type="float32")
    iface = SRInterface(name="ISpeed", kind="sender_receiver", data_elements=[de])
    assert iface.name == "ISpeed"
    assert iface.kind == "sender_receiver"
    assert len(iface.data_elements) == 1


def test_sr_interface_empty_data_elements_raises():
    with pytest.raises(ValidationError):
        SRInterface(name="ISpeed", kind="sender_receiver", data_elements=[])


def test_sr_interface_invalid_kind_raises():
    with pytest.raises(ValidationError):
        SRInterface(
            name="ISpeed",
            kind="client_server",
            data_elements=[DataElement(name="speed", implementation_type="float32")],
        )


@pytest.mark.parametrize("bad_name", ["../escape", "a/b", "a\\b"])
def test_sr_interface_path_traversal_name_raises(bad_name):
    with pytest.raises(ValidationError):
        SRInterface(
            name=bad_name,
            kind="sender_receiver",
            data_elements=[DataElement(name="speed", implementation_type="float32")],
        )


def test_sr_interface_blank_name_raises():
    with pytest.raises(ValidationError):
        SRInterface(
            name="   ",
            kind="sender_receiver",
            data_elements=[DataElement(name="speed", implementation_type="float32")],
        )


# ---------------------------------------------------------------------------
# Requirement
# ---------------------------------------------------------------------------


_REQ = {"title": "T", "description": "D.", "version": 1, "source": "architecture"}


def test_requirement_valid():
    req = Requirement(
        id="REQ_001", title="Speed", description="The system shall provide speed.", version=3, source="external"
    )
    assert req.id == "REQ_001"
    assert req.req_type == "req"
    assert req.version == 3
    assert req.source == "external"
    assert req.derived_from == []
    assert req.asil is None


def test_requirement_empty_id_raises():
    with pytest.raises(ValidationError):
        Requirement(id="", **_REQ)


def test_requirement_spec_type():
    req = Requirement(id="SPEC_001", req_type="spec", **_REQ)
    assert req.req_type == "spec"


@pytest.mark.parametrize("bad_id", ["../escape", "a/b", "a\\b"])
def test_requirement_path_traversal_id_raises(bad_id):
    with pytest.raises(ValidationError):
        Requirement(id=bad_id, **_REQ)


class TestTheRequirementShapeOfContract11:
    """``version`` and ``source`` required, ``derived_from`` optional, the two retired keys refused.

    The retired keys are refused naming the new shape; ``@`` and ``:`` are illegal in an id."""

    @pytest.mark.parametrize("bad_id", ["REQ@1", "REQ:1", "hardware:Core0"])
    def test_an_id_holding_a_separator_is_refused(self, bad_id: str) -> None:
        with pytest.raises(ValidationError, match="must not contain"):
            Requirement(id=bad_id, **_REQ)

    @pytest.mark.parametrize("version", [0, -1])
    def test_a_version_below_one_is_refused(self, version: int) -> None:
        with pytest.raises(ValidationError, match="version must be >= 1"):
            Requirement(id="REQ_1", **{**_REQ, "version": version})

    @pytest.mark.parametrize("version", [True, "3", 2.0], ids=["bool", "numeric-string", "float"])
    def test_a_version_that_is_not_an_integer_is_refused(self, version: object) -> None:
        """Strict: YAML's ``true``, a quoted ``'3'`` and ``2.0`` are no revision, though lax mode coerces them."""
        with pytest.raises(ValidationError, match="Input should be a valid integer"):
            Requirement(id="REQ_1", **{**_REQ, "version": version})

    @pytest.mark.parametrize("missing", ["version", "source"])
    def test_version_and_source_are_required(self, missing: str) -> None:
        data = {"id": "REQ_1", **_REQ}
        del data[missing]
        with pytest.raises(ValidationError, match=missing):
            Requirement.model_validate(data)

    def test_source_is_one_of_two(self) -> None:
        with pytest.raises(ValidationError):
            Requirement(id="REQ_1", **{**_REQ, "source": "import"})

    def test_derived_from_is_held_as_written(self) -> None:
        req = Requirement(id="REQ_2", derived_from=["REQ_1@2", "hardware:Core0"], **_REQ)
        assert req.derived_from == ["REQ_1@2", "hardware:Core0"]

    def test_links_is_refused_naming_where_the_link_lives_now(self) -> None:
        with pytest.raises(ValidationError) as caught:
            Requirement.model_validate({"id": "REQ_1", **_REQ, "links": ["Ctl"]})
        message = str(caught.value)
        assert "'links' moved to the elements by contract 11.0.0" in message
        assert "'satisfies: [REQ_1@N]'" in message
        assert "component, port, runnable, composition, interface, task or hardware element" in message

    def test_a_file_carrying_both_retired_keys_is_refused_naming_the_move(self) -> None:
        with pytest.raises(ValidationError, match="'links' moved to the elements"):
            Requirement.model_validate({"id": "REQ_1", **_REQ, "status": "open", "links": ["Ctl"]})

    def test_status_is_refused_naming_the_owner_of_the_lifecycle(self) -> None:
        with pytest.raises(ValidationError) as caught:
            Requirement.model_validate({"id": "REQ_1", **_REQ, "status": "open"})
        assert "'status' is retired by contract 11.0.0: an external tool owns the lifecycle; delete the key" in str(
            caught.value
        )

    def test_an_old_shape_file_is_refused_on_read(self, tmp_path) -> None:
        from swcraft_tools.reader import read_requirements

        directory = tmp_path / "requirements"
        directory.mkdir()
        (directory / "REQ_1.yaml").write_text(
            "id: REQ_1\ntitle: T\ndescription: D.\nreq_type: req\nstatus: open\nlinks:\n- Ctl\n",
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(ElementParseError, match="'links' moved to the elements"):
            read_requirements(str(tmp_path))

    @pytest.mark.parametrize(
        "kind",
        [
            schema.SWComponent,
            schema.Port,
            schema.Runnable,
            schema.Composition,
            schema.SRInterface,
            schema.CSInterface,
            schema.ExternalInterface,
            schema.Task,
            schema.Processor,
            schema.Core,
            schema.Memory,
            schema.Partition,
        ],
    )
    def test_the_twelve_linkable_kinds_carry_satisfies_last(self, kind: type[BaseModel]) -> None:
        assert list(kind.model_fields)[-1] == "satisfies"
        assert kind.model_fields["satisfies"].default == []


def test_satisfies_round_trips_through_the_loader_on_every_linkable_layer(tmp_path):
    from swcraft_tools.reader import read_compositions, read_hardware, read_interfaces, read_swc_model

    files = {
        "swc/g/Ctl.yaml": (
            "name: Ctl\nports:\n- name: pIn\n  direction: require\n  interface: I\n  satisfies:\n  - R@1\n"
            "runnables:\n- name: Run\n  satisfies:\n  - R@2\nsatisfies:\n- R@3\n"
        ),
        "compositions/Top.yaml": "name: Top\ntitle: T\nsatisfies:\n- R@1\n",
        "interfaces/I.yaml": (
            "name: I\nkind: sender_receiver\ndata_elements:\n- name: v\n  implementation_type: u8\nsatisfies:\n- R@1\n"
        ),
        "interfaces/C.yaml": "name: C\nkind: client_server\noperations:\n- name: Op\nsatisfies:\n- R@1\n",
        "interfaces/X.yaml": "name: X\nkind: external_interface\nsatisfies:\n- R@1\n",
        "tasks/T.yaml": "name: T\nperiod: 0.01\npriority: 1\npartition: P\nsatisfies:\n- R@1\n",
        "hardware/CPU.yaml": "name: CPU\nkind: processor\nsatisfies:\n- R@1\n",
        "hardware/C0.yaml": "name: C0\nkind: core\nthreshold: 0.5\nsatisfies:\n- R@1\n",
        "hardware/M.yaml": (
            "name: M\nkind: memory\ntype: RAM\nsize: 8\naccessible: [C0]\nthreshold: 0.5\nsatisfies:\n- R@1\n"
        ),
        "hardware/P.yaml": "name: P\nkind: partition\ncore: C0\nsatisfies:\n- R@1\n",
    }
    for relative, text in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    root = str(tmp_path)
    (swc,) = read_swc_model(root)
    assert (swc.satisfies, swc.ports[0].satisfies, swc.runnables[0].satisfies) == (["R@3"], ["R@1"], ["R@2"])
    assert [c.satisfies for c in read_compositions(root)] == [["R@1"]]
    assert sorted(tuple(i.satisfies) for i in read_interfaces(root)) == [("R@1",)] * 3
    assert [t.satisfies for t in read_tasks(root)] == [["R@1"]]
    assert [h.satisfies for h in read_hardware(root)] == [["R@1"]] * 4


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def test_composition_valid():
    comp = Composition(name="Root", title="Root composition")
    assert comp.name == "Root"
    assert comp.title == "Root composition"
    assert comp.description == ""
    assert comp.root is False
    assert comp.ports == []
    assert comp.members == []
    assert comp.delegations == []


# ---------------------------------------------------------------------------
# YAML round-trip
# ---------------------------------------------------------------------------

VALID_SWC_YAML = """
name: SpeedCtrlSWC
type: application
description: Controls vehicle speed using PID regulator

ports:
  - name: pSpeed
    direction: require
    interface: ISpeed
    description: Input current vehicle speed

runnables:
  - name: SpeedCtrl_Run
    min_start_interval: 0.01
    events:
      - type: timing
        period: 0.01
"""

INVALID_SWC_YAML = """
name: ""
type: unknown_type
"""


def test_swcomponent_model_validate_from_yaml():
    data = yaml.safe_load(VALID_SWC_YAML)
    swc = SWComponent.model_validate(data)
    assert swc.name == "SpeedCtrlSWC"
    assert len(swc.ports) == 1
    assert len(swc.runnables) == 1
    assert isinstance(swc.runnables[0].events[0], TimingEvent)


def test_swcomponent_model_validate_invalid_yaml_raises():
    data = yaml.safe_load(INVALID_SWC_YAML)
    with pytest.raises(ValidationError):
        SWComponent.model_validate(data)


# ---------------------------------------------------------------------------
# DataType variants
# ---------------------------------------------------------------------------


class TestPrimitiveDataType:
    def test_valid_primitive(self):
        dt = BaseDataType(name="float32", kind="base_type", size=32, encoding="IEEE754")
        assert dt.name == "float32"
        assert dt.kind == "base_type"
        assert dt.size == 32
        assert dt.encoding == "IEEE754"
        assert dt.native_declaration == ""
        assert dt.description == ""

    def test_optional_fields_default(self):
        dt = BaseDataType(name="uint8", kind="base_type", size=8, encoding="NONE")
        assert dt.native_declaration == ""
        assert dt.description == ""

    def test_with_native_declaration_and_description(self):
        dt = BaseDataType(
            name="float32",
            kind="base_type",
            size=32,
            encoding="IEEE754",
            native_declaration="float",
            description="32-bit float",
        )
        assert dt.native_declaration == "float"
        assert dt.description == "32-bit float"

    def test_missing_size_raises(self):
        with pytest.raises(ValidationError):
            BaseDataType(name="float32", kind="base_type", encoding="IEEE754")

    def test_missing_encoding_raises(self):
        with pytest.raises(ValidationError):
            BaseDataType(name="float32", kind="base_type", size=32)

    def test_invalid_kind_raises(self):
        with pytest.raises(ValidationError):
            BaseDataType(name="float32", kind="value_type", size=32, encoding="IEEE754")

    def test_empty_name_raises(self):
        with pytest.raises(ValidationError):
            BaseDataType(name="", kind="base_type", size=32, encoding="IEEE754")

    def test_whitespace_name_raises(self):
        with pytest.raises(ValidationError):
            BaseDataType(name="   ", kind="base_type", size=32, encoding="IEEE754")

    def test_path_traversal_slash_raises(self):
        with pytest.raises(ValidationError):
            BaseDataType(name="../../etc/passwd", kind="base_type", size=32, encoding="NONE")

    def test_path_traversal_backslash_raises(self):
        with pytest.raises(ValidationError):
            BaseDataType(name="..\\evil", kind="base_type", size=32, encoding="NONE")

    def test_path_traversal_dotdot_raises(self):
        with pytest.raises(ValidationError):
            BaseDataType(name="..", kind="base_type", size=32, encoding="NONE")

    @pytest.mark.parametrize("encoding", ["", "   ", "\t"])
    def test_blank_encoding_raises(self, encoding):
        # AUTOSAR's BASE-TYPE-ENCODING is a required enumerated token, and a
        # blank one is not round-trippable: the ARXML importer's SW-BASE-TYPE
        # row reads an empty <BASE-TYPE-ENCODING> as *missing* and skips the
        # type with a report line.
        with pytest.raises(ValidationError, match="encoding must not be blank"):
            BaseDataType(name="uint8", kind="base_type", size=8, encoding=encoding)

    def test_the_blank_encoding_message_names_a_valid_token(self):
        """The message says what to write instead, not only that the encoding must not be blank."""
        with pytest.raises(ValidationError) as exc_info:
            BaseDataType(name="uint8", kind="base_type", size=8, encoding="")

        message = str(exc_info.value)
        assert "BASE-TYPE-ENCODING" in message
        assert "NONE" in message

    def test_create_data_type_and_the_validator_agree_about_what_is_writable(self, tmp_path):
        """No blank encoding may enter a model through the documented tool surface.

        Guards ``add_data_type(kind="base_type", encoding="")`` writing a file the
        validator refuses to read back.
        """
        from conftest import model_element_path
        from swcraft_tools.auditor import AuditLog
        from swcraft_tools.writer import create_data_type

        auditor = AuditLog(str(tmp_path / "dist" / "audit.jsonl"), project_root=str(tmp_path))
        with pytest.raises(ValueError, match="encoding must not be blank"):
            create_data_type(
                str(tmp_path),
                "u8",
                "base_type",
                size=8,
                encoding="",
                auditor=auditor,
            )
        assert not model_element_path(tmp_path, "u8", kind="base_type").exists()


class TestAliasDataType:
    def test_valid_alias(self):
        dt = ValueDataType(name="VehicleSpeed", kind="value_type", base="float32")
        assert dt.name == "VehicleSpeed"
        assert dt.kind == "value_type"
        assert dt.base == "float32"
        assert dt.unit == ""
        assert dt.value_min is None
        assert dt.value_max is None
        assert dt.description == ""

    def test_with_all_fields(self):
        dt = ValueDataType(
            name="VehicleSpeed",
            kind="value_type",
            base="float32",
            unit="km_h",
            value_min=0.0,
            value_max=300.0,
            description="Vehicle longitudinal speed",
        )
        assert dt.unit == "km_h"
        assert dt.value_min == pytest.approx(0.0)
        assert dt.value_max == pytest.approx(300.0)

    def test_missing_base_raises(self):
        with pytest.raises(ValidationError):
            ValueDataType(name="VehicleSpeed", kind="value_type")

    def test_invalid_kind_raises(self):
        with pytest.raises(ValidationError):
            ValueDataType(name="VehicleSpeed", kind="base_type", base="float32")

    def test_a_base_that_shares_the_value_types_name_is_accepted(self):
        """A value type and the base type it rests on may share a name.

        ``base`` names an element of the ``base_types`` layer."""
        dt = ValueDataType(name="VehicleSpeed", kind="value_type", base="VehicleSpeed")
        assert dt.base == dt.name == "VehicleSpeed"

    def test_blank_base_raises(self):
        with pytest.raises(ValidationError, match="blank"):
            ValueDataType(name="VehicleSpeed", kind="value_type", base="")

    def test_whitespace_base_raises(self):
        with pytest.raises(ValidationError, match="blank"):
            ValueDataType(name="VehicleSpeed", kind="value_type", base="   ")

    def test_empty_name_raises(self):
        with pytest.raises(ValidationError):
            ValueDataType(name="", kind="value_type", base="float32")

    def test_whitespace_name_raises(self):
        with pytest.raises(ValidationError):
            ValueDataType(name="   ", kind="value_type", base="float32")

    def test_value_min_greater_than_value_max_raises(self):
        with pytest.raises(ValidationError):
            ValueDataType(name="Speed", kind="value_type", base="float32", value_min=100.0, value_max=0.0)

    def test_value_min_equals_value_max_ok(self):
        dt = ValueDataType(name="Speed", kind="value_type", base="float32", value_min=50.0, value_max=50.0)
        assert dt.value_min == pytest.approx(50.0)
        assert dt.value_max == pytest.approx(50.0)

    def test_value_min_without_max_ok(self):
        dt = ValueDataType(name="Speed", kind="value_type", base="float32", value_min=0.0)
        assert dt.value_min == pytest.approx(0.0)
        assert dt.value_max is None

    def test_path_traversal_slash_raises(self):
        with pytest.raises(ValidationError):
            ValueDataType(name="../../etc/passwd", kind="value_type", base="float32")

    def test_path_traversal_backslash_raises(self):
        with pytest.raises(ValidationError):
            ValueDataType(name="..\\evil", kind="value_type", base="float32")

    def test_path_traversal_dotdot_raises(self):
        with pytest.raises(ValidationError):
            ValueDataType(name="..", kind="value_type", base="float32")


class TestEnumLiteral:
    def test_valid_enum_literal(self):
        lit = EnumLiteral(name="PARK", value=0)
        assert lit.name == "PARK"
        assert lit.value == 0

    def test_blank_name_raises(self):
        with pytest.raises(ValidationError):
            EnumLiteral(name="", value=0)

    def test_whitespace_name_raises(self):
        with pytest.raises(ValidationError):
            EnumLiteral(name="   ", value=0)


class TestEnumDataType:
    def test_valid_enum(self):
        dt = EnumDataType(
            name="GearPosition",
            kind="enum",
            literals=[
                EnumLiteral(name="PARK", value=0),
                EnumLiteral(name="DRIVE", value=1),
            ],
        )
        assert dt.name == "GearPosition"
        assert dt.kind == "enum"
        assert len(dt.literals) == 2

    def test_empty_literals_raises(self):
        with pytest.raises(ValidationError):
            EnumDataType(name="GearPosition", kind="enum", literals=[])

    def test_missing_literals_raises(self):
        with pytest.raises(ValidationError):
            EnumDataType(name="GearPosition", kind="enum")

    def test_invalid_kind_raises(self):
        with pytest.raises(ValidationError):
            EnumDataType(
                name="GearPosition",
                kind="base_type",
                literals=[EnumLiteral(name="PARK", value=0)],
            )

    def test_description_optional(self):
        dt = EnumDataType(
            name="GearPosition",
            kind="enum",
            literals=[EnumLiteral(name="PARK", value=0)],
            description="Gear position enum",
        )
        assert dt.description == "Gear position enum"

    def test_empty_name_raises(self):
        with pytest.raises(ValidationError):
            EnumDataType(name="", kind="enum", literals=[EnumLiteral(name="PARK", value=0)])

    def test_whitespace_name_raises(self):
        with pytest.raises(ValidationError):
            EnumDataType(name="  ", kind="enum", literals=[EnumLiteral(name="PARK", value=0)])

    def test_path_traversal_slash_raises(self):
        with pytest.raises(ValidationError):
            EnumDataType(name="../../etc/passwd", kind="enum", literals=[EnumLiteral(name="PARK", value=0)])

    def test_path_traversal_backslash_raises(self):
        with pytest.raises(ValidationError):
            EnumDataType(name="..\\evil", kind="enum", literals=[EnumLiteral(name="PARK", value=0)])

    def test_path_traversal_dotdot_raises(self):
        with pytest.raises(ValidationError):
            EnumDataType(name="..", kind="enum", literals=[EnumLiteral(name="PARK", value=0)])


class TestRecordField:
    def test_valid_record_field(self):
        rf = RecordField(name="speed", type="VehicleSpeed")
        assert rf.name == "speed"
        assert rf.type == "VehicleSpeed"

    def test_blank_name_raises(self):
        with pytest.raises(ValidationError):
            RecordField(name="", type="float32")

    def test_whitespace_name_raises(self):
        with pytest.raises(ValidationError):
            RecordField(name="   ", type="float32")

    def test_blank_type_raises(self):
        with pytest.raises(ValidationError):
            RecordField(name="speed", type="")

    def test_whitespace_type_raises(self):
        with pytest.raises(ValidationError):
            RecordField(name="speed", type="   ")


class TestRecordDataType:
    def test_valid_record(self):
        dt = RecordDataType(
            name="VehicleState",
            kind="record",
            fields=[
                RecordField(name="speed", type="VehicleSpeed"),
                RecordField(name="ignition_on", type="bool"),
            ],
        )
        assert dt.name == "VehicleState"
        assert dt.kind == "record"
        assert len(dt.fields) == 2

    def test_empty_fields_raises(self):
        with pytest.raises(ValidationError):
            RecordDataType(name="VehicleState", kind="record", fields=[])

    def test_missing_fields_raises(self):
        with pytest.raises(ValidationError):
            RecordDataType(name="VehicleState", kind="record")

    def test_invalid_kind_raises(self):
        with pytest.raises(ValidationError):
            RecordDataType(
                name="VehicleState",
                kind="enum",
                fields=[RecordField(name="speed", type="float32")],
            )

    def test_empty_name_raises(self):
        with pytest.raises(ValidationError):
            RecordDataType(name="", kind="record", fields=[RecordField(name="speed", type="float32")])

    def test_whitespace_name_raises(self):
        with pytest.raises(ValidationError):
            RecordDataType(name=" ", kind="record", fields=[RecordField(name="speed", type="float32")])

    def test_path_traversal_slash_raises(self):
        with pytest.raises(ValidationError):
            RecordDataType(name="../../etc/passwd", kind="record", fields=[RecordField(name="speed", type="float32")])

    def test_path_traversal_backslash_raises(self):
        with pytest.raises(ValidationError):
            RecordDataType(name="..\\evil", kind="record", fields=[RecordField(name="speed", type="float32")])

    def test_path_traversal_dotdot_raises(self):
        with pytest.raises(ValidationError):
            RecordDataType(name="..", kind="record", fields=[RecordField(name="speed", type="float32")])


class TestArrayDataType:
    def test_valid_array(self):
        dt = ArrayDataType(
            name="WheelSpeedArray",
            kind="array",
            element_type="float32",
            length=4,
        )
        assert dt.name == "WheelSpeedArray"
        assert dt.kind == "array"
        assert dt.element_type == "float32"
        assert dt.length == 4
        assert dt.description == ""

    def test_missing_element_type_raises(self):
        with pytest.raises(ValidationError):
            ArrayDataType(name="WheelSpeedArray", kind="array", length=4)

    def test_missing_length_raises(self):
        with pytest.raises(ValidationError):
            ArrayDataType(name="WheelSpeedArray", kind="array", element_type="float32")

    def test_zero_length_raises(self):
        with pytest.raises(ValidationError):
            ArrayDataType(name="WheelSpeedArray", kind="array", element_type="float32", length=0)

    def test_negative_length_raises(self):
        with pytest.raises(ValidationError):
            ArrayDataType(name="WheelSpeedArray", kind="array", element_type="float32", length=-1)

    def test_invalid_kind_raises(self):
        with pytest.raises(ValidationError):
            ArrayDataType(name="WheelSpeedArray", kind="record", element_type="float32", length=4)

    def test_empty_name_raises(self):
        with pytest.raises(ValidationError):
            ArrayDataType(name="", kind="array", element_type="float32", length=4)

    def test_whitespace_name_raises(self):
        with pytest.raises(ValidationError):
            ArrayDataType(name="   ", kind="array", element_type="float32", length=4)

    def test_blank_element_type_raises(self):
        with pytest.raises(ValidationError, match="blank"):
            ArrayDataType(name="WheelSpeedArray", kind="array", element_type="", length=4)

    def test_whitespace_element_type_raises(self):
        with pytest.raises(ValidationError, match="blank"):
            ArrayDataType(name="WheelSpeedArray", kind="array", element_type="   ", length=4)

    def test_path_traversal_slash_raises(self):
        with pytest.raises(ValidationError):
            ArrayDataType(name="../../etc/passwd", kind="array", element_type="float32", length=4)

    def test_path_traversal_backslash_raises(self):
        with pytest.raises(ValidationError):
            ArrayDataType(name="..\\evil", kind="array", element_type="float32", length=4)

    def test_path_traversal_dotdot_raises(self):
        with pytest.raises(ValidationError):
            ArrayDataType(name="..", kind="array", element_type="float32", length=4)


class TestDataTypeDiscriminatedUnion:
    """Test the DataType discriminated union dispatches correctly."""

    def test_primitive_dispatch(self):
        data = {"name": "float32", "kind": "base_type", "size": 32, "encoding": "IEEE754"}
        adapter = TypeAdapter(DataType)
        dt = adapter.validate_python(data)
        assert isinstance(dt, BaseDataType)
        assert dt.name == "float32"

    def test_alias_dispatch(self):
        adapter = TypeAdapter(DataType)
        dt = adapter.validate_python({"name": "VehicleSpeed", "kind": "value_type", "base": "float32"})
        assert isinstance(dt, ValueDataType)

    def test_enum_dispatch(self):
        adapter = TypeAdapter(DataType)
        dt = adapter.validate_python(
            {
                "name": "GearPosition",
                "kind": "enum",
                "literals": [{"name": "PARK", "value": 0}],
            }
        )
        assert isinstance(dt, EnumDataType)

    def test_record_dispatch(self):
        adapter = TypeAdapter(DataType)
        dt = adapter.validate_python(
            {
                "name": "VehicleState",
                "kind": "record",
                "fields": [{"name": "speed", "type": "float32"}],
            }
        )
        assert isinstance(dt, RecordDataType)

    def test_array_dispatch(self):
        adapter = TypeAdapter(DataType)
        dt = adapter.validate_python(
            {"name": "WheelSpeedArray", "kind": "array", "element_type": "float32", "length": 4}
        )
        assert isinstance(dt, ArrayDataType)

    def test_invalid_kind_raises(self):
        adapter = TypeAdapter(DataType)
        with pytest.raises(ValidationError):
            adapter.validate_python({"name": "Foo", "kind": "unknown"})

    def test_yaml_roundtrip_primitive(self):
        """Verify YAML round-trip for the base type kind."""
        raw = yaml.safe_load("name: float32\nkind: base_type\nsize: 32\nencoding: IEEE754\nnative_declaration: float\n")
        adapter = TypeAdapter(DataType)
        dt = adapter.validate_python(raw)
        assert isinstance(dt, BaseDataType)
        assert dt.size == 32


def _write_data_type_file(model_root, text: str, stem: str = "T"):
    """One data type file under *model_root*, named *stem*, holding *text* verbatim.

    A ``base_type`` file goes to its own layer's directory, ``data_types/base/``.
    """
    from conftest import model_element_path

    kind = "base_type" if "\nkind: base_type\n" in text else "value_type"
    path = model_element_path(model_root, stem, kind=kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _read_both_layers(model_root) -> list:
    """Every element of the two data description layers, base types first."""
    from swcraft_tools.reader import read_data_types, read_layer

    return [*read_layer(str(model_root), "base_types"), *read_data_types(str(model_root))]


def _load_refusal(model_root) -> str:
    """The message the loader refuses *model_root*'s data description layers with."""
    with pytest.raises(ValueError) as exc_info:
        _read_both_layers(model_root)
    return str(exc_info.value)


class TestTheThreeDataTypeKindsAtLoad:
    """``base_type``, ``value_type`` and ``reference_type`` at LOAD.

    The loader holds what a kind can express. Two of these refusals are
    ``extra="forbid"``'s: a ``base`` key on a ``base_type`` and a ``unit`` /
    ``value_min`` / ``value_max`` key on a ``reference_type``. They are **not**
    the discriminator's — a ``Literal`` discriminator only selects a variant —
    and without the ``forbid`` Pydantic's default ``extra="ignore"`` would drop
    the key without a word, which is what every other kind still does
    (:class:`TestTheForbidIsScopedToTheKindsThatDeclareIt`).
    """

    def test_a_base_key_on_a_base_type_is_refused_naming_the_file(self, tmp_path):
        path = _write_data_type_file(
            tmp_path, "name: u8\nkind: base_type\nsize: 8\nencoding: NONE\nbase: other\n", stem="u8"
        )
        message = _load_refusal(tmp_path)
        assert path.relative_to(tmp_path).as_posix() == f"data_types/base/{path.name}"
        assert f"data_types/base/{path.name}" in message
        assert "base" in message
        assert "Extra inputs are not permitted" in message

    @pytest.mark.parametrize("key, value", [("unit", "km/h"), ("value_min", "0.0"), ("value_max", "100.0")])
    def test_a_unit_or_range_key_on_a_reference_type_is_refused_naming_the_file(self, tmp_path, key, value):
        path = _write_data_type_file(tmp_path, f"name: Speed\nkind: reference_type\nbase: Raw\n{key}: {value}\n")
        message = _load_refusal(tmp_path)
        assert f"data_types/implementation/{path.name}" in message
        assert key in message
        assert "Extra inputs are not permitted" in message

    def test_a_value_type_with_no_base_is_refused_naming_the_file(self, tmp_path):
        path = _write_data_type_file(tmp_path, "name: Speed\nkind: value_type\n")
        message = _load_refusal(tmp_path)
        assert f"data_types/implementation/{path.name}" in message
        assert "base" in message
        assert "Field required" in message

    def test_an_unknown_kind_names_the_six_legal_values(self):
        with pytest.raises(ValidationError) as exc_info:
            TypeAdapter(DataType).validate_python({"name": "Foo", "kind": "primitive", "size": 8, "encoding": "NONE"})
        message = str(exc_info.value)
        for kind in ("base_type", "value_type", "reference_type", "enum", "record", "array"):
            assert f"'{kind}'" in message, (kind, message)
        assert "'alias'" not in message

    def test_a_value_type_whose_base_shares_its_name_is_accepted(self):
        """The base is a base type, an element of another layer, so the shared name is no self-reference."""
        loaded = TypeAdapter(DataType).validate_python({"name": "Speed", "kind": "value_type", "base": "Speed"})
        assert isinstance(loaded, ValueDataType)
        assert loaded.base == "Speed"

    def test_a_value_type_whose_minimum_exceeds_its_maximum_is_refused(self):
        with pytest.raises(ValidationError, match="value_min must be <= value_max"):
            TypeAdapter(DataType).validate_python(
                {"name": "Speed", "kind": "value_type", "base": "f32", "value_min": 100.0, "value_max": 0.0}
            )

    def test_a_reference_type_whose_base_is_its_own_name_is_refused(self):
        with pytest.raises(ValidationError, match="self-referential"):
            TypeAdapter(DataType).validate_python({"name": "Speed", "kind": "reference_type", "base": "Speed"})

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param(
                "name: float32_base\nkind: base_type\nsize: 32\nencoding: IEEE754\nnative_declaration: float\n"
                "description: How a float32 is stored.\n",
                id="base_type",
            ),
            pytest.param(
                "name: Speed_T\nkind: value_type\nbase: float32_base\nunit: km/h\nvalue_min: -10.5\n"
                "value_max: 300.0\ndescription: A speed.\n",
                id="value_type",
            ),
            pytest.param(
                "name: WheelSpeed_T\nkind: reference_type\nbase: Speed_T\ndescription: Another name for Speed_T.\n",
                id="reference_type",
            ),
        ],
    )
    def test_a_hand_written_file_of_each_kind_reads_back_byte_identical(self, tmp_path, text):
        """Load through the reader, write through the writer's own serializer, compare bytes."""
        from swcraft_tools.writer.elements import element_to_dict

        _write_data_type_file(tmp_path, text)
        (loaded,) = _read_both_layers(tmp_path)
        dumped = yaml.safe_dump(element_to_dict(loaded), default_flow_style=False, sort_keys=False)
        assert dumped == text


class TestTheForbidIsScopedToTheKindsThatDeclareIt:
    """``extra="forbid"`` is on the classes that declare it and on nothing else.

    ``ModelElement`` is untouched, so every other kind still drops an unknown key
    in silence; this pins that asymmetry.
    """

    def test_model_element_declares_no_extra_policy(self):
        assert "extra" not in ModelElement.model_config

    def test_an_enum_still_drops_an_unknown_key(self):
        dt = TypeAdapter(DataType).validate_python(
            {"name": "Gear", "kind": "enum", "literals": [{"name": "P", "value": 0}], "colour": "red"}
        )
        assert isinstance(dt, EnumDataType)
        assert not hasattr(dt, "colour")

    def test_exactly_the_closed_data_type_classes_forbid_extra_keys(self):
        """The twenty-one closed classes: the data type kinds, their nested records and the two stubs.

        A data type kind or a nested record (a conversion, a range, a unit, a compu
        method, a table entry, a data constraint, a scale's limits and bound) carries
        numbers, where a dropped key is a wrong physical value rather than a missing
        note. The two external stubs declare nothing of their target's content, so a
        ``data_elements``, an ``operations`` or a ``base`` written on one is a
        mistake. The mapping set and its pairs stay open with every other kind: they
        carry names the validator resolves, so a typo there is already a finding.
        """
        forbidding = sorted(
            cls.__name__
            for cls in vars(schema).values()
            if isinstance(cls, type) and issubclass(cls, BaseModel) and cls.model_config.get("extra") == "forbid"
        )
        assert forbidding == [
            "ApplicationDataType",
            "BaseDataType",
            "Conversion",
            "ConversionLimitBound",
            "ConversionLimits",
            "DataConstr",
            "ExternalDataType",
            "ExternalInterface",
            "ImplementationArrayDataType",
            "ImplementationElement",
            "ImplementationRecordDataType",
            "Linear",
            "LinearLimitBound",
            "LinearLimits",
            "PointerDataType",
            "ReferenceDataType",
            "TableEntry",
            "TextTable",
            "Unit",
            "ValueDataType",
            "ValueRange",
        ]


# ---------------------------------------------------------------------------
# CSArgument
# ---------------------------------------------------------------------------


class TestCSArgument:
    def test_valid_in(self):
        arg = CSArgument(name="request", direction="in", implementation_type="DtcReadRequest")
        assert arg.name == "request"
        assert arg.direction == "in"
        assert arg.implementation_type == "DtcReadRequest"

    def test_valid_out(self):
        arg = CSArgument(name="response", direction="out", implementation_type="DtcReadResponse")
        assert arg.direction == "out"

    def test_valid_inout(self):
        arg = CSArgument(name="data", direction="inout", implementation_type="SomeType")
        assert arg.direction == "inout"

    def test_invalid_direction_raises(self):
        with pytest.raises(ValidationError):
            CSArgument(name="x", direction="invalid", implementation_type="SomeType")

    def test_invalid_direction_uppercase_raises(self):
        with pytest.raises(ValidationError):
            CSArgument(name="x", direction="IN", implementation_type="SomeType")

    def test_blank_name_raises(self):
        with pytest.raises(ValidationError, match="name must not be blank"):
            CSArgument(name="  ", direction="in", implementation_type="SomeType")

    def test_empty_name_raises(self):
        with pytest.raises(ValidationError, match="name must not be blank"):
            CSArgument(name="", direction="in", implementation_type="SomeType")

    def test_blank_type_raises(self):
        with pytest.raises(ValidationError, match="type must not be blank"):
            CSArgument(name="x", direction="in", application_type="  ")

    def test_empty_type_raises(self):
        with pytest.raises(ValidationError, match="type must not be blank"):
            CSArgument(name="x", direction="in", implementation_type="")


class TestAPrototypeStatesTheSideOfItsType:
    """A data element and an argument name their type in exactly one of two fields.

    ``application_type`` names an application type, ``implementation_type`` an
    implementation type: neither and both are refused, each alone is accepted, and a
    blank ``application_type`` is refused like a blank ``implementation_type``.
    """

    def test_a_data_element_with_neither_field_is_refused(self):
        with pytest.raises(
            ValidationError, match="must declare exactly one of 'application_type' and 'implementation_type'"
        ):
            DataElement(name="sample")

    def test_a_data_element_with_both_fields_is_refused(self):
        with pytest.raises(
            ValidationError, match="must declare exactly one of 'application_type' and 'implementation_type'"
        ):
            DataElement(name="sample", application_type="SampleApp_T", implementation_type="SampleImpl_T")

    def test_a_data_element_with_application_type_alone_is_accepted(self):
        de = DataElement(name="sample", application_type="SampleApp_T")
        assert de.application_type == "SampleApp_T"
        assert de.implementation_type is None

    def test_a_data_element_with_type_alone_is_accepted(self):
        de = DataElement(name="sample", implementation_type="SampleImpl_T")
        assert de.implementation_type == "SampleImpl_T"
        assert de.application_type is None

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_data_element_with_a_blank_application_type_is_refused(self, blank):
        with pytest.raises(ValidationError, match="application_type must not be blank"):
            DataElement(name="sample", application_type=blank)

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_data_element_with_a_blank_implementation_type_is_refused(self, blank):
        """``rule:interfaces.data_element.implementation_type.not-blank``: refused when the file loads."""
        with pytest.raises(ValidationError, match="data element implementation_type must not be blank"):
            DataElement(name="sample", implementation_type=blank)

    @pytest.mark.parametrize(
        ("field", "blank"), [(f, b) for f in ("application_type", "implementation_type") for b in ("", "   ")]
    )
    def test_an_argument_with_a_blank_type_field_is_refused(self, field, blank):
        with pytest.raises(ValidationError, match=f"CSArgument {field} must not be blank"):
            CSArgument(name="request", direction="in", **{field: blank})

    def test_an_argument_with_neither_field_is_refused(self):
        with pytest.raises(
            ValidationError, match="must declare exactly one of 'application_type' and 'implementation_type'"
        ):
            CSArgument(name="request", direction="in")

    def test_an_argument_with_both_fields_is_refused(self):
        with pytest.raises(
            ValidationError, match="must declare exactly one of 'application_type' and 'implementation_type'"
        ):
            CSArgument(
                name="request", direction="in", application_type="SampleApp_T", implementation_type="SampleImpl_T"
            )

    def test_an_argument_with_application_type_alone_is_accepted(self):
        arg = CSArgument(name="request", direction="in", application_type="SampleApp_T")
        assert arg.application_type == "SampleApp_T"
        assert arg.implementation_type is None

    def test_an_argument_with_type_alone_is_accepted(self):
        arg = CSArgument(name="request", direction="in", implementation_type="SampleImpl_T")
        assert arg.implementation_type == "SampleImpl_T"
        assert arg.application_type is None

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_an_argument_with_a_blank_application_type_is_refused(self, blank):
        with pytest.raises(ValidationError, match="application_type must not be blank"):
            CSArgument(name="request", direction="in", application_type=blank)


# ---------------------------------------------------------------------------
# CSOperation
# ---------------------------------------------------------------------------


class TestCSOperation:
    def test_valid_with_arguments(self):
        arg = CSArgument(name="request", direction="in", implementation_type="DtcReadRequest")
        op = CSOperation(name="ReadDtc", arguments=[arg])
        assert op.name == "ReadDtc"
        assert len(op.arguments) == 1

    def test_empty_arguments_allowed(self):
        op = CSOperation(name="Ping")
        assert op.arguments == []

    def test_arguments_default_is_empty_list(self):
        op = CSOperation(name="Op1")
        assert op.arguments == []

    def test_blank_name_raises(self):
        with pytest.raises(ValidationError, match="name must not be blank"):
            CSOperation(name="  ")

    def test_empty_name_raises(self):
        with pytest.raises(ValidationError, match="name must not be blank"):
            CSOperation(name="")


# ---------------------------------------------------------------------------
# CSInterface
# ---------------------------------------------------------------------------


VALID_CS_INTERFACE_YAML = """
name: IDiagnosticService
kind: client_server
operations:
  - name: ReadDtc
    arguments:
      - name: request
        direction: in
        implementation_type: DtcReadRequest
      - name: response
        direction: out
        implementation_type: DtcReadResponse
"""


class TestCSInterface:
    def test_valid_cs_interface(self):
        op = CSOperation(
            name="ReadDtc",
            arguments=[
                CSArgument(name="request", direction="in", implementation_type="DtcReadRequest"),
                CSArgument(name="response", direction="out", implementation_type="DtcReadResponse"),
            ],
        )
        iface = CSInterface(name="IDiagnosticService", kind="client_server", operations=[op])
        assert iface.name == "IDiagnosticService"
        assert iface.kind == "client_server"
        assert len(iface.operations) == 1
        assert len(iface.operations[0].arguments) == 2

    def test_empty_operations_raises(self):
        with pytest.raises(ValidationError, match="operations must not be empty"):
            CSInterface(name="IDiagnosticService", kind="client_server", operations=[])

    def test_yaml_roundtrip(self):
        raw = yaml.safe_load(VALID_CS_INTERFACE_YAML)
        iface = CSInterface.model_validate(raw)
        assert iface.name == "IDiagnosticService"
        assert iface.kind == "client_server"
        assert len(iface.operations) == 1
        assert iface.operations[0].name == "ReadDtc"
        assert iface.operations[0].arguments[0].direction == "in"
        assert iface.operations[0].arguments[1].direction == "out"

    def test_invalid_kind_raises(self):
        with pytest.raises(ValidationError):
            CSInterface(
                name="IFoo",
                kind="sender_receiver",
                operations=[CSOperation(name="Op1")],
            )

    def test_argument_with_invalid_direction_raises(self):
        with pytest.raises(ValidationError):
            CSInterface(
                name="IFoo",
                kind="client_server",
                operations=[
                    CSOperation(
                        name="Op1",
                        arguments=[CSArgument(name="x", direction="wrong", implementation_type="T")],
                    )
                ],
            )

    def test_blank_name_raises(self):
        with pytest.raises(ValidationError, match="name must not be blank"):
            CSInterface(
                name="  ",
                kind="client_server",
                operations=[CSOperation(name="Op1")],
            )

    @pytest.mark.parametrize("bad_name", ["../escape", "a/b", "a\\b"])
    def test_path_traversal_name_raises(self, bad_name):
        with pytest.raises(ValidationError):
            CSInterface(
                name=bad_name,
                kind="client_server",
                operations=[CSOperation(name="Op1")],
            )

    def test_empty_name_raises(self):
        with pytest.raises(ValidationError, match="name must not be blank"):
            CSInterface(
                name="",
                kind="client_server",
                operations=[CSOperation(name="Op1")],
            )


# ---------------------------------------------------------------------------
# Interface discriminated union
# ---------------------------------------------------------------------------


class TestInterfaceDiscriminatedUnion:
    """Test that Interface union dispatches correctly by kind."""

    def test_sr_dispatch(self):
        data = {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [{"name": "speed", "implementation_type": "float32"}],
        }
        adapter = TypeAdapter(Interface)
        iface = adapter.validate_python(data)
        assert isinstance(iface, SRInterface)
        assert iface.name == "ISpeed"

    def test_cs_dispatch(self):
        data = {
            "name": "IDiagnosticService",
            "kind": "client_server",
            "operations": [
                {
                    "name": "ReadDtc",
                    "arguments": [{"name": "request", "direction": "in", "implementation_type": "DtcReadRequest"}],
                }
            ],
        }
        adapter = TypeAdapter(Interface)
        iface = adapter.validate_python(data)
        assert isinstance(iface, CSInterface)
        assert iface.name == "IDiagnosticService"

    def test_unknown_kind_raises(self):
        adapter = TypeAdapter(Interface)
        with pytest.raises(ValidationError):
            adapter.validate_python({"name": "IFoo", "kind": "unknown"})

    def test_sr_yaml_dispatch(self):
        raw = yaml.safe_load(
            "name: ISpeed\nkind: sender_receiver\ndata_elements:\n  - name: speed\n    implementation_type: float32\n"
        )
        adapter = TypeAdapter(Interface)
        iface = adapter.validate_python(raw)
        assert isinstance(iface, SRInterface)

    def test_cs_yaml_dispatch(self):
        raw = yaml.safe_load(VALID_CS_INTERFACE_YAML)
        adapter = TypeAdapter(Interface)
        iface = adapter.validate_python(raw)
        assert isinstance(iface, CSInterface)


# ---------------------------------------------------------------------------
# Non-finite floats
# ---------------------------------------------------------------------------


class TestNoModelFieldAcceptsANonFiniteFloat:
    """`NaN` and `±Infinity` are refused at load, for every float field.

    YAML spells them `.nan` / `.inf` and Pydantic accepts them into a `float`
    by default; without `ModelElement`'s `allow_inf_nan=False` a model carrying
    one passes `check-model` and first fails at ARXML generation with a raw
    `cannot convert float NaN to integer` naming neither the file nor the element.
    """

    @pytest.mark.parametrize("literal", [".nan", ".inf", "-.inf"])
    def test_yaml_really_produces_a_non_finite_float(self, literal):
        """Non-vacuity: PyYAML parses these as floats.

        Otherwise the rejections below would prove nothing."""
        value = yaml.safe_load(f"x: {literal}")["x"]
        assert isinstance(value, float)
        assert not math.isfinite(value)

    @pytest.mark.parametrize("literal", [".nan", ".inf", "-.inf"])
    def test_a_field_with_no_bound_of_its_own_refuses_it(self, literal):
        """`ValueDataType.value_min` has no validator at all.

        The config is the only thing standing between it and a non-finite value."""
        value = yaml.safe_load(f"x: {literal}")["x"]
        with pytest.raises(ValidationError, match="finite"):
            ValueDataType(name="Speed_T", kind="value_type", base="uint16", value_min=value)

    def test_a_hand_rolled_bound_that_a_comparison_cannot_catch_refuses_it(self):
        """A hand-rolled bound a comparison cannot catch: the reason the rule is on the base.

        The rule is on the base rather than on the seven float fields because `min_start_interval`'s guard is `v < 0`,
        and `NaN < 0` is `False`, so the comparison passes it through."""
        nan = yaml.safe_load("x: .nan")["x"]
        assert not (nan < 0), "the guard this test is about would otherwise catch it"
        with pytest.raises(ValidationError, match="finite"):
            Runnable(name="Run", min_start_interval=nan)

    def test_every_model_element_carries_the_rule(self):
        """Derived, not listed.

        A future element that forgets the base fails here rather than silently reopening the hole."""
        elements = [
            value
            for value in vars(schema).values()
            if isinstance(value, type) and issubclass(value, BaseModel) and value.__module__ == schema.__name__
        ]
        assert len(elements) > 20, "the walk found nothing — it stopped working"
        not_covered = [cls.__name__ for cls in elements if cls.model_config.get("allow_inf_nan") is not False]
        assert not_covered == [], f"these do not refuse non-finite floats: {not_covered}"
        assert ModelElement in elements

    def test_finite_values_are_untouched(self):
        assert Runnable(name="Run", min_start_interval=0.5).min_start_interval == pytest.approx(0.5)
        assert TimingEvent(type="timing", period=0.01).period == pytest.approx(0.01)
        assert ValueDataType(
            name="Speed_T", kind="value_type", base="uint16", value_min=-40.0
        ).value_min == pytest.approx(-40.0)


# ---------------------------------------------------------------------------
# Runnable data access
# ---------------------------------------------------------------------------


class TestRunnableDataAccess:
    """`Runnable.reads` / `.writes` / `.calls` — the three optional access lists.

    Each entry names one port of the owning SWC and one member of that port's
    interface: `{port, element}` for a read or a write, `{port, operation}` for
    a call. The identity of an entry is the whole name pair (as in
    `Task.runnables`), which is what the element-local duplicate rule keys on;
    `by: value` on a read is the receive form and no part of the identity, so
    a by-value read and a by-argument read of one pair are the same entry twice.

    What this class does **not** reach: every rule here is element-local — one
    runnable, read alone. Whether `port` names a port of the SWC, whether the
    port's interface carries `element` or `operation`, and whether the port's
    direction and interface kind admit the access are cross-element questions
    `check-model` answers; a class that validates in
    isolation says nothing about them, and a blank name is refused here only
    because it could never resolve. The on-disk half of "a runnable without
    lists is today's runnable" — no `reads: []` in the file — is
    `test_writer_elements.py`'s, and needs the contract's `omitAtDefault`.
    """

    # -- entries parse -----------------------------------------------------

    def test_a_read_entry_parses_and_by_is_absent_by_default(self):
        ref = DataReadRef(port="rSpeed", element="value")
        assert (ref.port, ref.element) == ("rSpeed", "value")
        assert ref.by is None, "absent means by-argument — the AUTOSAR default"

    def test_a_read_entry_takes_by_value(self):
        assert DataReadRef(port="rSpeed", element="value", by="value").by == "value"

    @pytest.mark.parametrize(
        ("cls", "payload"),
        [
            (DataReadRef, {"port": 5, "element": "value"}),
            (DataWriteRef, {"port": "pOut", "element": 5}),
            (OperationCallRef, {"port": "rSrv", "operation": 5}),
        ],
        ids=["read.port", "write.element", "call.operation"],
    )
    def test_a_non_string_name_is_refused_not_coerced(self, cls, payload):
        # An unquoted YAML scalar that parses as an int is refused, not
        # stringified: pydantic's lax mode never coerces int -> str, and a
        # port named `5` would resolve nowhere.
        with pytest.raises(ValidationError, match="string"):
            cls(**payload)

    @pytest.mark.parametrize("word", ["argument", "arguments", "VALUE", ""])
    def test_a_read_entry_refuses_any_other_receive_form(self, word):
        """`by` is the one literal or nothing.

        The by-argument form has no spelling, so nobody can write it out and make the two forms differ in the file where
        they do not differ in meaning."""
        with pytest.raises(ValidationError):
            DataReadRef(port="rSpeed", element="value", by=word)

    def test_a_write_entry_parses(self):
        ref = DataWriteRef(port="pSpeed", element="value")
        assert (ref.port, ref.element) == ("pSpeed", "value")

    def test_a_call_entry_parses(self):
        ref = OperationCallRef(port="rDiag", operation="ReadDtc")
        assert (ref.port, ref.operation) == ("rDiag", "ReadDtc")

    def test_by_is_impossible_on_calls_by_shape(self):
        """The receive form belongs to a read and the send form (`by: implicit`) to a write, and to nothing else.

        It is kept off `calls` by *shape* — the class has no such field — rather than by a validator that would have to
        remember it. A stray `by` key in a file is ignored at load like every other stray key on every other element,
        and the generated editor schema (`additionalProperties: false`) is what flags it in the editor."""
        assert "by" not in OperationCallRef.model_fields
        loaded = OperationCallRef.model_validate({"port": "rDiag", "operation": "ReadDtc", "by": "value"})
        assert not hasattr(loaded, "by")
        assert "by" not in loaded.model_dump()

    def test_a_write_takes_the_implicit_form_and_no_other(self):
        """A write's `by` is `implicit` or absent; the read's `value` is refused."""
        assert DataWriteRef(port="pSpeed", element="value", by="implicit").by == "implicit"
        with pytest.raises(ValidationError):
            DataWriteRef.model_validate({"port": "pSpeed", "element": "value", "by": "value"})

    @pytest.mark.parametrize(
        ("cls", "payload"),
        [
            (DataReadRef, {"port": " ", "element": "value"}),
            (DataReadRef, {"port": "rSpeed", "element": ""}),
            (DataWriteRef, {"port": "", "element": "value"}),
            (DataWriteRef, {"port": "pSpeed", "element": "  "}),
            (OperationCallRef, {"port": "", "operation": "ReadDtc"}),
            (OperationCallRef, {"port": "rDiag", "operation": " "}),
        ],
    )
    def test_a_blank_name_in_an_entry_is_refused(self, cls, payload):
        """A reference whose name is blank can name nothing, so it is refused at load.

        The `TaskRunnableRef` / `SequenceStep` precedent (`rule:tasks.task_runnable_ref.reference-not-blank`): refused
        at load rather than reported as unresolved by `check-model`."""
        with pytest.raises(ValidationError, match="must not be empty"):
            cls.model_validate(payload)

    # -- the lists on the runnable ---------------------------------------

    def test_the_three_lists_default_empty(self):
        r = Runnable(name="Run1")
        assert (r.reads, r.writes, r.calls) == ([], [], [])

    def test_a_runnable_without_lists_is_exactly_the_runnable_before_this_phase(self):
        """A runnable without lists dumps its fields at their defaults beside the base ones."""
        before = {"name": "Run1", "min_start_interval": 0.0, "events": []}
        dumped = Runnable(name="Run1").model_dump()
        assert {key: dumped[key] for key in before} == before
        assert set(dumped) - set(before) == {
            "reads",
            "writes",
            "calls",
            "reads_variables",
            "writes_variables",
            "can_enter",
            "runs_inside",
            "concurrent",
            "description",
            "symbol",
            "wcet",
            "wcet_type",
            "satisfies",
        }
        assert dumped["concurrent"] is False
        assert dumped["description"] == ""
        assert dumped["symbol"] is None
        assert dumped["wcet"] is None and dumped["wcet_type"] is None

    def test_a_runnable_carries_all_three_lists_in_order(self):
        """A YAML runnable with every list, loaded through the SWC the way the reader does it.

        The lists keep their file order; `by` survives on the one entry that carries it and is absent on the rest."""
        data = yaml.safe_load(
            """
name: Control
runnables:
- name: ControlRun
  events:
  - type: timing
    period: 0.01
  reads:
  - port: rSpeed
    element: value
  - port: rSpeed
    element: valid
    by: value
  - port: rMode
    element: mode
  writes:
  - port: pTorque
    element: value
  - port: pTorque
    element: valid
  calls:
  - port: rDiag
    operation: ReadDtc
"""
        )
        runnable = SWComponent.model_validate(data).runnables[0]
        assert [(r.port, r.element, r.by) for r in runnable.reads] == [
            ("rSpeed", "value", None),
            ("rSpeed", "valid", "value"),
            ("rMode", "mode", None),
        ]
        assert [(w.port, w.element) for w in runnable.writes] == [("pTorque", "value"), ("pTorque", "valid")]
        assert [(c.port, c.operation) for c in runnable.calls] == [("rDiag", "ReadDtc")]
        # `model_dump` is order-preserving too — the in-memory half of the round trip.
        dumped = runnable.model_dump()["reads"]
        # Each entry's stated `name`, `None` while derived.
        assert dumped[0] == {"port": "rSpeed", "element": "value", "by": None, "name": None}
        assert dumped[1] == {"port": "rSpeed", "element": "valid", "by": "value", "name": None}

    # -- the element-local duplicate rule ---------------------------------

    @pytest.mark.parametrize(
        ("field", "entry"),
        [
            ("reads", {"port": "rSpeed", "element": "value"}),
            ("writes", {"port": "pSpeed", "element": "value"}),
            ("calls", {"port": "rDiag", "operation": "ReadDtc"}),
        ],
    )
    def test_a_repeated_pair_in_one_list_is_refused_at_load(self, field, entry):
        """`rule:swc.runnable.<list>.no-duplicate-entry`, the `Task.runnables_must_not_have_duplicates` pattern.

        The pattern is `schema.py`'s; the message names the list and the pair."""
        pair = ".".join(entry.values())
        with pytest.raises(ValidationError, match=rf"duplicate {field} entry in runnable: {pair}"):
            Runnable(name="Run1", **{field: [entry, entry]})

    def test_a_by_value_read_and_a_by_argument_read_of_one_pair_are_a_duplicate(self):
        """`by` is a form, not an identity.

        One pair read in both receive forms is no shape the model carries: it refuses the case rather than carrying two
        entries whose derived `R_<port>_<element>` name would collide in the ARXML."""
        with pytest.raises(ValidationError, match="duplicate reads entry in runnable: rSpeed.value"):
            Runnable(
                name="Run1",
                reads=[{"port": "rSpeed", "element": "value"}, {"port": "rSpeed", "element": "value", "by": "value"}],
            )

    def test_the_same_pair_read_and_written_is_legal(self):
        """The rule is per list.

        Reading and writing one element through one port is two different accesses, not a repeat."""
        r = Runnable(
            name="Run1",
            reads=[{"port": "prSignal", "element": "value"}],
            writes=[{"port": "prSignal", "element": "value"}],
        )
        assert (len(r.reads), len(r.writes)) == (1, 1)

    def test_one_port_with_two_elements_is_not_a_duplicate(self):
        r = Runnable(
            name="Run1", reads=[{"port": "rSpeed", "element": "value"}, {"port": "rSpeed", "element": "valid"}]
        )
        assert len(r.reads) == 2

    def test_one_element_name_on_two_ports_is_not_a_duplicate(self):
        r = Runnable(name="Run1", writes=[{"port": "pA", "element": "value"}, {"port": "pB", "element": "value"}])
        assert len(r.writes) == 2


# ---------------------------------------------------------------------------
# The two `asil` fields
# ---------------------------------------------------------------------------


class TestTheAsilFields:
    """`Requirement.asil` and `SWComponent.asil`: an optional level, with no default.

    The absence of a default is deliberate. A component that declares nothing
    loads as `None` and *derives* its level from the requirements allocated to
    it; a component that writes `asil: QM` has **declared** that no ASIL is
    allocated to it. The two are different states
    at every layer — on disk, here, in `query` and in the rule that reads them —
    and a defaulted field could not tell them apart.

    The enum is a `Literal` and not a validator: a value outside it is a **load
    failure** and never a reported finding.
    """

    def test_both_fields_default_to_none(self):
        assert Requirement(id="R", title="T", description="D.", version=1, source="architecture").asil is None
        assert SWComponent(name="A").asil is None

    @pytest.mark.parametrize("level", ASIL_VALUES)
    def test_every_declared_level_loads(self, level):
        assert (
            Requirement(id="R", title="T", description="D.", asil=level, version=1, source="architecture").asil == level
        )
        assert SWComponent(name="A", asil=level).asil == level

    @pytest.mark.parametrize("bad", UNLISTED_LEVELS)
    def test_a_value_outside_the_enum_is_a_load_failure(self, bad):
        with pytest.raises(ValidationError):
            Requirement(id="R", title="T", description="D.", asil=bad, version=1, source="architecture")
        with pytest.raises(ValidationError):
            SWComponent(name="A", asil=bad)

    def test_a_written_qm_and_an_omitted_key_are_distinguishable(self):
        """`QM` is a declaration that no level is allocated, not an absence."""
        written = yaml.safe_load("name: A\ntype: application\nasil: QM\n")
        omitted = yaml.safe_load("name: A\ntype: application\n")

        assert SWComponent(**written).asil == "QM"
        assert SWComponent(**omitted).asil is None

    def test_the_rated_levels_are_exactly_the_leaf_modules_values(self):
        """The vocabulary is declared once; `safety.py` holds the order over it.

        The four rated fields' `Literal` is the eighteen of
        `safety.ASIL_VALUES`, in its order (the standard's listing order), and
        the two capabilities' stays the five of `ASIL_LEVELS`.
        """
        from swcraft_tools.safety import ASIL_LEVELS

        rated = [(model, "asil", ASIL_VALUES) for model in (Requirement, SWComponent, DataElement, Partition)]
        capability = [(model, "asil_capability", ASIL_LEVELS) for model in (Core, Memory)]
        for model, field, expected in rated + capability:
            annotation = model.model_fields[field].annotation
            levels = tuple(arg for member in get_args(annotation) for arg in get_args(member) if isinstance(arg, str))
            assert levels == expected, model.__name__

    def test_an_unlisted_spelling_is_refused_with_the_literals_own_message(self):
        """The refusal is Pydantic's `Literal` message over the eighteen -- pinned once, here."""
        with pytest.raises(ValidationError) as excinfo:
            Requirement(id="R", title="T", description="D.", asil="A(A)", version=1, source="architecture")
        [error] = excinfo.value.errors()
        assert error["type"] == "literal_error"
        assert error["msg"] == (
            "Input should be 'QM', 'A', 'B', 'C', 'D', 'QM(A)', 'QM(B)', 'QM(C)', 'QM(D)', 'A(B)', 'A(C)', "
            "'A(D)', 'B(B)', 'B(C)', 'B(D)', 'C(C)', 'C(D)' or 'D(D)'"
        )


class TestTheThirdBearerAndTheTwoDeclaredArguments:
    """The level on the information, and the two arguments that answer a finding.

    `DataElement.asil` takes the component's shape exactly — an optional level with
    **no default**, so "declares none" and a written `QM` stay two states — on a
    *member* kind. An element that
    declares nothing is **unrated**, which is not `QM`: neither port rule
    compares against it at all.

    `Port.measures` and `Task.coexistence` are declarations rather than
    verifications, and this module pins what that means where the loader can see
    it: a map on a **`provide`** port LOADS (it is
    `safety.measure-on-provide-port`'s finding, never a load failure, so a
    project carrying one stays readable), a key naming no element LOADS (the
    interface is not this file's to see), and a **blank** text loads on both.
    Nothing here checks content, and nothing anywhere else does either.
    """

    def test_the_three_fields_carry_their_declared_defaults(self):
        assert DataElement(name="e", implementation_type="uint8").asil is None
        assert Port(name="p", direction="provide", interface="I").measures == {}
        assert _task().coexistence == ""

    @pytest.mark.parametrize("level", ASIL_VALUES)
    def test_every_declared_level_loads_on_a_data_element(self, level):
        assert DataElement(name="e", implementation_type="uint8", asil=level).asil == level

    @pytest.mark.parametrize("bad", UNLISTED_LEVELS)
    def test_a_value_outside_the_enum_is_a_load_failure(self, bad):
        with pytest.raises(ValidationError):
            DataElement(name="e", implementation_type="uint8", asil=bad)

    def test_a_written_qm_and_an_omitted_key_are_distinguishable_on_an_element_too(self):
        assert DataElement(**yaml.safe_load("name: e\nimplementation_type: uint8\nasil: QM\n")).asil == "QM"
        assert DataElement(**yaml.safe_load("name: e\nimplementation_type: uint8\n")).asil is None

    def test_a_measures_map_on_a_provide_port_loads(self):
        """A FINDING and never a load failure, so `list` and `query` still work."""
        port = Port(name="p", direction="provide", interface="I", measures={"speed": "E2E profile 4"})

        assert port.measures == {"speed": "E2E profile 4"}

    def test_a_key_naming_nothing_and_a_blank_text_both_load(self):
        """Content is not checked at load: a blank text is `safety.measure-blank`'s finding."""
        port = Port(name="r", direction="require", interface="I", measures={"nosuch": "", "other": "x"})

        assert port.measures == {"nosuch": "", "other": "x"}

    def test_a_coexistence_text_is_free_and_may_be_blank(self):
        assert _task(coexistence="Separate partitions.").coexistence == "Separate partitions."
        assert _task(coexistence="").coexistence == ""

    @pytest.mark.parametrize("bad", [{"speed": 4}, {4: "x"}, ["speed"]])
    def test_a_measures_value_that_is_not_a_string_map_is_a_load_failure(self, bad):
        with pytest.raises(ValidationError):
            Port(name="r", direction="require", interface="I", measures=bad)


def _task(**extra):
    return Task(name="T", period=0.01, priority=1, partition="PARTITION0", runnables=[], **extra)


class TestThePrototypeTypeFieldIsImplementationType:
    """A data element's and an argument's ``type`` is ``implementation_type``.

    A file still stating the old key alone states neither side, so it is refused with the
    exactly-one sentence that names both fields — the upgrade's one message, on both kinds.
    """

    @pytest.mark.parametrize(
        ("model", "extra"), [(DataElement, {}), (CSArgument, {"direction": "in"})], ids=["data_element", "argument"]
    )
    def test_the_new_field_loads_and_the_old_key_alone_is_refused(self, model: type[BaseModel], extra: dict) -> None:
        assert model(name="x", implementation_type="SyntheticT", **extra).implementation_type == "SyntheticT"
        assert "type" not in model.model_fields
        with pytest.raises(
            ValidationError, match="must declare exactly one of 'application_type' and 'implementation_type'"
        ):
            model(name="x", type="SyntheticT", **extra)
