"""The completeness guard for the per-item collection diff.

A new collection field must fail here instead of silently falling back to
printing its whole old value beside its whole new value. The universe is derived
from ``schema.py``, split into a covered table and an out-of-scope table with a
reason per entry, and every entry is proved against behaviour:

* **From ``schema.py``** — every field reachable from the layers whose dumped
  value is a container is either in the production ``COLLECTION_IDENTITY`` or
  in :data:`_OUT_OF_SCOPE`. The walk reuses ``differ._model_members``, so it
  cannot disagree with the one the differ recurses through.
* **From ``model_dump``** — :data:`_SAMPLE` holds a real instance of every
  reachable class, and the static classification is checked against what
  ``model_dump(by_alias=True)`` really produces.
* **From the differ** — perturbing one field of a real dump and running
  ``_diff_element`` shows a covered field yields a ``CollectionChange`` and an
  out-of-scope one yields none while still being reported changed.

``_diff_element``, the differ's single pass, is observed at every depth.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

import pytest
from conftest import _layer_element_classes, _list_or_mapping_backed
from pydantic import BaseModel, ValidationError
from swcraft_tools.differ import _diff_element, _model_members, collection_identity, collection_rule
from swcraft_tools.model_registry import production_registry
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    CallReturnedEvent,
    CanEnterArea,
    Composition,
    CompositionMember,
    CompositionPort,
    Connector,
    ConnectorEndpoint,
    Conversion,
    ConversionLimitBound,
    ConversionLimits,
    Core,
    CSArgument,
    CSInterface,
    CSOperation,
    DataConstr,
    DataElement,
    DataEvent,
    DataReadRef,
    DataTypeMap,
    DataWriteRef,
    Delegation,
    DisabledMode,
    EnumDataType,
    EnumLiteral,
    ExclusiveArea,
    ExternalDataType,
    ExternalInterface,
    ImplementationArrayDataType,
    ImplementationElement,
    ImplementationRecordDataType,
    InitEvent,
    Interface,
    Linear,
    LinearLimitBound,
    LinearLimits,
    MappingSet,
    Memory,
    OperationCallRef,
    OperationEvent,
    Partition,
    PointerDataType,
    Port,
    PortComSpec,
    Processor,
    RecordDataType,
    RecordField,
    ReferenceDataType,
    Requirement,
    Runnable,
    RunsInsideArea,
    Sequence,
    SequenceStep,
    SRInterface,
    SWComponent,
    TableEntry,
    Task,
    TaskRunnableRef,
    TextTable,
    TimingEvent,
    Unit,
    ValueDataType,
    ValueRange,
    Variable,
    VariableReadRef,
    VariableWriteRef,
)
from swcraft_tools.snapshot import ModelSnapshot

#: ``(owner class, field name) -> CollectionRule`` over the production contract, read
#: from the kernel; the projection :func:`~swcraft_tools.differ.collection_rule` resolves per call.
COLLECTION_IDENTITY = collection_identity(production_registry())


#: What one field's dumped value is, as far as the per-item diff is concerned.
ContainerKind = str  # "list" | "mapping" | "" for a scalar


# Derivation 1: every container field reachable from the layers.


def _reachable_models() -> tuple[type[BaseModel], ...]:
    """Every model class reachable from the layers' element classes, through ``differ._model_members``.

    Reusing the production unwrapping is the point: an independent walk could
    disagree with the one the differ recurses through.
    """
    pending = list(_layer_element_classes())
    seen: list[type[BaseModel]] = []
    while pending:
        model_class = pending.pop()
        if model_class in seen:
            continue
        seen.append(model_class)
        for field_info in model_class.model_fields.values():
            pending.extend(member for member in _model_members(field_info.annotation) if member not in seen)
    return tuple(seen)


def _container_kind(annotation: Any) -> ContainerKind:
    """Whether one field's *dumped* value is a list, a mapping, or neither.

    The dump is what ``_diff_element`` compares, so a nested model counts as a
    mapping. ``Annotated`` and an optional union are unwrapped first, so
    ``list[X] | None`` is a list, not a scalar.
    """
    if hasattr(annotation, "__metadata__"):
        annotation = annotation.__origin__
    origin = get_origin(annotation)
    if origin in (list, tuple, set, frozenset):
        return "list"
    if origin is dict:
        return "mapping"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "mapping"
    if origin in (Union, UnionType):
        kinds = {_container_kind(member) for member in get_args(annotation) if member is not type(None)}
        return kinds.pop() if len(kinds) == 1 else ""
    return ""


def _container_fields() -> dict[tuple[type[BaseModel], str], ContainerKind]:
    """``(owner class, dumped field name) -> container kind``, from ``schema.py``.

    A field is named by its alias where it has one, the name ``_diff_element`` walks.
    """
    derived: dict[tuple[type[BaseModel], str], ContainerKind] = {}
    for model_class in _reachable_models():
        for field_name, field_info in model_class.model_fields.items():
            kind = _container_kind(field_info.annotation)
            if kind:
                derived[(model_class, field_info.alias or field_name)] = kind
    return derived


def _nested_record_fields() -> set[tuple[type[BaseModel], str]]:
    """The container fields that are a nested **model**, not list- or mapping-backed.

    Only these may be parked in :data:`_OUT_OF_SCOPE`: a fixed record has no
    items to identify. ``conftest._list_or_mapping_backed`` is shared with
    ``test_differ.py``'s nesting walk, so the two cannot disagree.
    """
    return {
        (model_class, field_info.alias or field_name)
        for model_class in _reachable_models()
        for field_name, field_info in model_class.model_fields.items()
        if _container_kind(field_info.annotation) and not _list_or_mapping_backed(field_info.annotation)
    }


#: The container fields the per-item diff deliberately does not detail, with the reason
#: each is out; :class:`TestEachTableIsMeasuredAgainstTheDiffer` measures the claim.
_OUT_OF_SCOPE: dict[tuple[type[BaseModel], str], str] = {
    (Connector, "from"): (
        "a fixed record, not a collection: it has no items to identify, so the identity question "
        "does not arise, and its whole value is a port name beside the one type name that owns it "
        "— exactly one of 'swc' and 'composition' is set — so printing it in full "
        "is the readable form, not a restated value"
    ),
    (Connector, "to"): "the same fixed endpoint record as 'from'",
    (ApplicationDataType, "conversion"): (
        "a fixed record, not a collection: its two fields are one linear conversion, so there are no "
        "items to identify and the whole value is the readable form"
    ),
    (ApplicationDataType, "range"): (
        "a fixed record, not a collection: its two fields are one interval, so there are no items to "
        "identify and the whole value is the readable form"
    ),
    (Conversion, "limits"): (
        "a fixed record, not a collection: its two fields are one scale's two limits, so there are no "
        "items to identify and the whole value is the readable form"
    ),
    (Linear, "limits"): "the same fixed record of two limits as a conversion's",
    (ConversionLimits, "lower"): (
        "a fixed record, not a collection: a value beside its interval, so there are no items to identify"
    ),
    (ConversionLimits, "upper"): "the same fixed bound record as 'lower'",
    (LinearLimits, "lower"): "the same fixed bound record as a conversion's",
    (LinearLimits, "upper"): "the same fixed bound record as a conversion's",
}


class TestEveryContainerFieldIsClassified:
    def test_the_classification_is_exactly_the_derived_set(self) -> None:
        derived = set(_container_fields())
        classified = set(COLLECTION_IDENTITY) | set(_OUT_OF_SCOPE)

        assert derived == classified, (
            "schema.py and the per-item diff's coverage disagree; "
            f"unclassified: {sorted((cls.__name__, name) for cls, name in derived - classified)}, "
            f"stale: {sorted((cls.__name__, name) for cls, name in classified - derived)}"
        )

    def test_the_two_tables_are_disjoint(self) -> None:
        assert not (set(COLLECTION_IDENTITY) & set(_OUT_OF_SCOPE))

    def test_only_a_nested_record_may_be_parked_out_of_scope(self) -> None:
        """Only a nested record may be parked out of scope, never a list- or mapping-backed field."""
        parked_collections = set(_OUT_OF_SCOPE) - _nested_record_fields()

        assert not parked_collections, (
            "a list- or mapping-backed collection is parked out of scope: "
            f"{sorted((cls.__name__, name) for cls, name in parked_collections)}"
        )

    def test_every_covered_collections_kind_is_the_one_the_schema_declares(self) -> None:
        """``CollectionRule.kind`` is the annotation's own dumped shape, never an editorial choice."""
        derived = _container_fields()

        for entry, rule in COLLECTION_IDENTITY.items():
            assert rule.kind == derived[entry], f"{entry[0].__name__}.{entry[1]}"

    def test_no_rule_survives_the_field_it_identifies(self) -> None:
        """A removed field leaves no dead rule; the failure names the missing field."""
        for owner, field_name in COLLECTION_IDENTITY:
            declared = {info.alias or name for name, info in owner.model_fields.items()}
            assert field_name in declared, f"{owner.__name__} declares no field '{field_name}'"

    @pytest.mark.parametrize(
        "entry", [pytest.param(entry, id=f"{entry[0].__name__}.{entry[1]}") for entry in sorted(_OUT_OF_SCOPE, key=str)]
    )
    def test_each_out_of_scope_field_states_a_reason(self, entry: tuple[type[BaseModel], str]) -> None:
        assert _OUT_OF_SCOPE[entry].strip()

    @pytest.mark.parametrize(
        ("annotation", "kind", "backed"),
        [
            pytest.param(list[Port], "list", True, id="list-of-models"),
            pytest.param(list[str], "list", True, id="list-of-scalars"),
            pytest.param(list[TimingEvent | InitEvent], "list", True, id="list-of-a-union"),
            pytest.param(dict[str, int], "mapping", True, id="mapping"),
            pytest.param(ConnectorEndpoint, "mapping", False, id="nested-record"),
            pytest.param(Interface, "mapping", False, id="annotated-discriminated-union"),
            pytest.param(list[str] | None, "list", True, id="optional-list"),
            pytest.param(dict[str, int] | None, "mapping", True, id="optional-mapping"),
            pytest.param(int | None, "", False, id="optional-scalar"),
            pytest.param(str, "", False, id="scalar"),
            pytest.param(Literal["application", "service"], "", False, id="literal"),
        ],
    )
    def test_both_readings_of_an_annotation_agree_row_by_row(self, annotation: Any, kind: str, backed: bool) -> None:
        """:func:`_container_kind` and ``_list_or_mapping_backed`` agree on one table of annotation forms.

        Forms ``schema.py`` does not use yet are pinned too, so the first one is
        not classified as a scalar.
        """
        assert _container_kind(annotation) == kind
        assert _list_or_mapping_backed(annotation) is backed
        assert not backed or bool(kind), "a list- or mapping-backed field is always a container"

    def test_the_walk_reaches_the_classes_it_is_supposed_to(self) -> None:
        """Non-vacuity: the walk reaches the expected classes."""
        reachable = set(_reachable_models())

        assert {SWComponent, Port, Runnable, Connector, ConnectorEndpoint, Requirement, Task} <= reachable
        assert len(reachable) > 20

    def test_the_walk_follows_a_discriminated_union_into_every_member(self) -> None:
        """A layer union's members and a nested union's members are all reached."""
        reachable = set(_reachable_models())

        assert {SRInterface, CSInterface} <= reachable, "the interfaces layer union"
        assert {
            BaseDataType,
            ValueDataType,
            ReferenceDataType,
            EnumDataType,
            RecordDataType,
            ArrayDataType,
        } <= reachable
        assert {Processor, Core, Memory, Partition} <= reachable, "the hardware layer union"
        assert {TimingEvent, InitEvent, OperationEvent, DataEvent, CallReturnedEvent} <= reachable, (
            "Runnable.events' nested union"
        )

    def test_every_collection_the_vocabulary_covers_is_reached_through_a_union_member(self) -> None:
        """Collections reachable only through a union member are in the universe."""
        derived = set(_container_fields())

        assert {(SRInterface, "data_elements"), (CSInterface, "operations"), (Runnable, "events")} <= derived


# Derivation 2: the container shape, read off real dumps.

#: The one disabled mode the samples carry.
_DISABLED_MODE = DisabledMode(port="rMode", mode_group_ref="/Ifs/IMode/mode", mode_ref="/Modes/Mode/OFF")

#: One real instance of every reachable class, every container field populated: an
#: empty or absent one dumps as nothing for the shape check and the perturbation to read.
_SAMPLE: dict[type[BaseModel], BaseModel] = {
    Composition: Composition(
        name="Powertrain",
        title="Powertrain",
        root=True,
        ports=[CompositionPort(name="pSpeed", direction="provide", interface="ISpeed")],
        members=[CompositionMember(name="SpeedCtrl", type="SpeedCtrlSWC")],
        delegations=[Delegation(name="dSpeed", port="pSpeed", member="SpeedCtrl", member_port="pSpeed")],
    ),
    CompositionPort: CompositionPort(name="pSpeed", direction="provide", interface="ISpeed"),
    CompositionMember: CompositionMember(name="SpeedCtrl", type="SpeedCtrlSWC"),
    Delegation: Delegation(name="dSpeed", port="pSpeed", member="SpeedCtrl", member_port="pSpeed"),
    SWComponent: SWComponent(
        name="SpeedCtrlSWC",
        ports=[
            Port(
                name="pSpeed",
                direction="provide",
                interface="ISpeed",
                measures={"speed": "E2E profile 4"},
                com_specs=[PortComSpec(data_element="speed", uses_end_to_end_protection=True)],
            )
        ],
        runnables=[
            Runnable(
                name="SpeedCtrl_Run",
                events=[TimingEvent(type="timing", period=0.01, disabled_modes=[_DISABLED_MODE])],
                reads=[DataReadRef(port="rWheel", element="wheel_speed")],
                writes=[DataWriteRef(port="pSpeed", element="speed")],
                calls=[OperationCallRef(port="rDiag", operation="ReadDtc")],
                reads_variables=[VariableReadRef(variable="FilteredSpeed")],
                writes_variables=[VariableWriteRef(variable="FilteredSpeed")],
                can_enter=[CanEnterArea(area="SpeedState")],
                runs_inside=[RunsInsideArea(area="SpeedState")],
            ),
        ],
        variables=[Variable(name="FilteredSpeed", access="implicit", implementation_type="uint32", init=0)],
        exclusive_areas=[ExclusiveArea(name="SpeedState", description="Guards the speed.")],
        memory={"bss": 1024},
        data_type_mappings=["Physical"],
    ),
    Port: Port(
        name="pSpeed",
        direction="provide",
        interface="ISpeed",
        measures={"speed": "E2E profile 4"},
        com_specs=[PortComSpec(data_element="speed", uses_end_to_end_protection=True)],
    ),
    PortComSpec: PortComSpec(data_element="speed", uses_end_to_end_protection=True),
    Runnable: Runnable(
        name="SpeedCtrl_Run",
        events=[TimingEvent(type="timing", period=0.01, disabled_modes=[_DISABLED_MODE])],
        reads=[DataReadRef(port="rWheel", element="wheel_speed")],
        writes=[DataWriteRef(port="pSpeed", element="speed")],
        calls=[OperationCallRef(port="rDiag", operation="ReadDtc")],
        reads_variables=[VariableReadRef(variable="FilteredSpeed")],
        writes_variables=[VariableWriteRef(variable="FilteredSpeed")],
        can_enter=[CanEnterArea(area="SpeedState")],
        runs_inside=[RunsInsideArea(area="SpeedState")],
    ),
    TimingEvent: TimingEvent(type="timing", period=0.01, disabled_modes=[_DISABLED_MODE]),
    DisabledMode: _DISABLED_MODE,
    DataReadRef: DataReadRef(port="rWheel", element="wheel_speed"),
    DataWriteRef: DataWriteRef(port="pSpeed", element="speed"),
    OperationCallRef: OperationCallRef(port="rDiag", operation="ReadDtc"),
    Variable: Variable(name="FilteredSpeed", access="implicit", implementation_type="uint32", init=0),
    VariableReadRef: VariableReadRef(variable="FilteredSpeed"),
    VariableWriteRef: VariableWriteRef(variable="FilteredSpeed"),
    ExclusiveArea: ExclusiveArea(name="SpeedState", description="Guards the speed."),
    CanEnterArea: CanEnterArea(area="SpeedState"),
    RunsInsideArea: RunsInsideArea(area="SpeedState"),
    InitEvent: InitEvent(type="init"),
    OperationEvent: OperationEvent(type="operation", port="pServe", operation="Get"),
    DataEvent: DataEvent(type="data", port="rWheel", element="wheel_speed", disabled_modes=[_DISABLED_MODE]),
    CallReturnedEvent: CallReturnedEvent(type="call_returned", port="rStore", operation="Write", result="Written"),
    SRInterface: SRInterface(
        name="ISpeed", kind="sender_receiver", data_elements=[DataElement(name="speed", implementation_type="uint32")]
    ),
    DataElement: DataElement(name="speed", implementation_type="uint32"),
    CSInterface: CSInterface(
        name="IDiag",
        kind="client_server",
        operations=[
            CSOperation(
                name="ReadDtc", arguments=[CSArgument(name="dtc", direction="out", implementation_type="uint32")]
            )
        ],
    ),
    CSOperation: CSOperation(
        name="ReadDtc", arguments=[CSArgument(name="dtc", direction="out", implementation_type="uint32")]
    ),
    CSArgument: CSArgument(name="dtc", direction="out", implementation_type="uint32"),
    BaseDataType: BaseDataType(name="uint32", kind="base_type", size=32, encoding="NONE"),
    ValueDataType: ValueDataType(name="Speed_T", kind="value_type", base="uint32"),
    ReferenceDataType: ReferenceDataType(name="WheelSpeed_T", kind="reference_type", base="Speed_T"),
    EnumDataType: EnumDataType(name="Gear_T", kind="enum", literals=[EnumLiteral(name="Park", value=0)]),
    EnumLiteral: EnumLiteral(name="Park", value=0),
    RecordDataType: RecordDataType(name="Pose_T", kind="record", fields=[RecordField(name="x", type="uint32")]),
    RecordField: RecordField(name="x", type="uint32"),
    ArrayDataType: ArrayDataType(name="Samples_T", kind="array", element_type="uint32", length=4),
    ApplicationDataType: ApplicationDataType(
        name="Kph_T",
        kind="application_type",
        unit="km/h",
        conversion=Conversion(factor=0.1, offset=0.0),
        range=ValueRange(min=0.0, max=400.0),
    ),
    Conversion: Conversion(
        factor=0.1,
        offset=0.0,
        limits=ConversionLimits(
            lower=ConversionLimitBound(value=0), upper=ConversionLimitBound(value=4000, interval="open")
        ),
    ),
    ConversionLimits: ConversionLimits(
        lower=ConversionLimitBound(value=0), upper=ConversionLimitBound(value=4000, interval="open")
    ),
    ConversionLimitBound: ConversionLimitBound(value=0),
    ValueRange: ValueRange(min=0.0, max=400.0),
    # A stub lists `operations` or `data_elements` by its `DEST`, never both; the
    # sample needs an item of each, so it is constructed without validation.
    ExternalInterface: ExternalInterface.model_construct(
        name="Service_If",
        kind="external_interface",
        external_ref="/Platform/Service_If",
        external_dest="CLIENT-SERVER-INTERFACE",
        operations=["Get"],
        data_elements=["value"],
        description="",
    ),
    ExternalDataType: ExternalDataType(
        name="Counter_T",
        kind="external_type",
        external_ref="/Platform/Counter_T",
        external_dest="IMPLEMENTATION-DATA-TYPE",
    ),
    ImplementationRecordDataType: ImplementationRecordDataType(
        name="Pose_impl",
        kind="implementation_record",
        elements=[ImplementationElement(name="x", type="Speed_T")],
    ),
    ImplementationElement: ImplementationElement(name="x", type="Speed_T"),
    ImplementationArrayDataType: ImplementationArrayDataType(
        name="Samples_impl", kind="implementation_array", element_type="Speed_T", length=4, element_name="Element"
    ),
    PointerDataType: PointerDataType(name="Pose_ptr", kind="pointer", target="Pose_impl"),
    MappingSet: MappingSet(name="Physical", maps=[DataTypeMap(application="Kph_T", implementation="Speed_T")]),
    Unit: Unit(name="Kmh", display_name="km/h"),
    TextTable: TextTable(name="Gear_Table", kind="texttable", entries=[TableEntry(name="Park", value=0)]),
    TableEntry: TableEntry(name="Park", value=0),
    Linear: Linear(
        name="ProbeLinearScaling",
        kind="linear",
        unit="Kmh",
        factor=0.1,
        limits=LinearLimits(lower=LinearLimitBound(value=0), upper=LinearLimitBound(value=4000, interval="open")),
    ),
    LinearLimits: LinearLimits(lower=LinearLimitBound(value=0), upper=LinearLimitBound(value=4000, interval="open")),
    LinearLimitBound: LinearLimitBound(value=0),
    DataConstr: DataConstr(name="Kph_Limits", min=0.0, max=400.0, level=0),
    DataTypeMap: DataTypeMap(application="Kph_T", implementation="Speed_T"),
    Connector: Connector.model_validate(
        {
            "name": "cSpeed",
            "from": {"swc": "SpeedCtrlSWC", "port": "pSpeed"},
            "to": {"swc": "DashSWC", "port": "rSpeed"},
        }
    ),
    ConnectorEndpoint: ConnectorEndpoint(swc="SpeedCtrlSWC", port="pSpeed"),
    Requirement: Requirement(
        id="REQ-001",
        title="Speed",
        description="Report the speed.",
        version=1,
        source="architecture",
        derived_from=["REQ-000@1"],
    ),
    Task: Task(
        name="Task10ms",
        period=0.01,
        priority=5,
        partition="PARTITION0",
        runnables=[TaskRunnableRef(swc="SpeedCtrlSWC", runnable="SpeedCtrl_Run")],
    ),
    TaskRunnableRef: TaskRunnableRef(swc="SpeedCtrlSWC", runnable="SpeedCtrl_Run"),
    Sequence: Sequence(
        name="speed_flow", task="Task10ms", steps=[SequenceStep(swc="SpeedCtrlSWC", runnable="SpeedCtrl_Run")]
    ),
    SequenceStep: SequenceStep(swc="SpeedCtrlSWC", runnable="SpeedCtrl_Run"),
    Processor: Processor(name="CPU", kind="processor"),
    Core: Core(name="CORE0", kind="core", threshold=0.7),
    Memory: Memory(name="RAM0", kind="memory", type="RAM", size=8192, accessible=["CORE0"], threshold=0.8),
    Partition: Partition(name="PARTITION0", kind="partition", core="CORE0", asil="B", coexistence="argued"),
}

#: Every kind carrying a `satisfies` list holds one link, for the same reason.
_SAMPLE.update(
    {
        owner: sample.model_copy(update={"satisfies": ["REQ-001@1"]})
        for owner, sample in _SAMPLE.items()
        if "satisfies" in type(sample).model_fields
    }
)


def _dump_sample(model_class: type[BaseModel]) -> dict[str, Any]:
    """One reachable class's sample in its dumped form, aliases and all."""
    return _SAMPLE[model_class].model_dump(by_alias=True)


class TestTheContainerShapeIsReadOffRealDumps:
    """:func:`_container_kind` reads annotations, ``_diff_element`` dumps; the two must agree."""

    def test_every_reachable_class_has_a_sample(self) -> None:
        missing = [model_class.__name__ for model_class in _reachable_models() if model_class not in _SAMPLE]
        assert not missing, f"schema.py grew a class this guard cannot dump: {sorted(missing)}"

    @pytest.mark.parametrize(
        "model_class",
        [pytest.param(cls, id=cls.__name__) for cls in sorted(_reachable_models(), key=lambda cls: cls.__name__)],
    )
    def test_the_dump_agrees_with_the_static_classification(self, model_class: type[BaseModel]) -> None:
        derived = _container_fields()
        for field_name, value in _dump_sample(model_class).items():
            if value is None:
                continue  # an unset optional says nothing about its field's shape
            observed = "list" if isinstance(value, list) else "mapping" if isinstance(value, Mapping) else ""
            assert derived.get((model_class, field_name), "") == observed, f"{model_class.__name__}.{field_name}"

    def test_a_nested_record_really_dumps_as_a_mapping(self) -> None:
        """A nested record dumps as a mapping, like ``SWComponent.memory``, so it is in the universe."""
        assert isinstance(_dump_sample(Connector)["from"], Mapping)
        assert isinstance(_dump_sample(SWComponent)["memory"], Mapping)


# Derivation 3: both tables, measured against the differ.


def _perturbed_scalar(value: Any) -> Any:
    """Some other value of the same shape, whatever the scalar is."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return value + 1
    if isinstance(value, str):
        return f"{value}X"
    if isinstance(value, Mapping):
        # A record nested in a record (a scale's limits hold two bounds): the same rule one level down.
        first = next(iter(value))
        return {**value, first: _perturbed_scalar(value[first])}
    raise AssertionError(f"no perturbation for {value!r}")


def _perturbed(value: Any) -> Any:
    """One container value, changed in the smallest way its shape allows.

    A mapping gets a different value under its first key; a list gains a copy of
    its first item, which every identity rule reads as one addition. Generic on
    purpose, so the proof cannot be tuned per field.
    """
    if isinstance(value, Mapping):
        first = next(iter(value))
        return {**value, first: _perturbed_scalar(value[first])}
    return [*value, value[0]]


def _first_item(value: Any) -> Any:
    """One item of a container, whichever shape it is."""
    return next(iter(value.values())) if isinstance(value, Mapping) else value[0]


def _modifiable_field(owner: type[BaseModel], field_name: str) -> str | None:
    """The first field of this collection's items that is **not** part of its key.

    ``None`` when the item is a bare scalar or every scalar field is read by its
    identity rule; such rows are counted in :data:`_UNMODIFIABLE_ROWS`.
    """
    rule = collection_rule(owner, field_name)
    if rule is None:
        return None
    item = _first_item(_dump_sample(owner)[field_name])
    if not isinstance(item, Mapping):
        return None
    key = rule.key(item)
    for name, value in item.items():
        if value is None or isinstance(value, (list, Mapping)):
            continue
        if rule.key({**item, name: _perturbed_scalar(value)}) == key:
            return name
    return None


def _modified(owner: type[BaseModel], field_name: str) -> Any:
    """The container with its first item **changed in place**, keeping its key."""
    value = _dump_sample(owner)[field_name]
    name = _modifiable_field(owner, field_name)
    assert name is not None, f"{owner.__name__}.{field_name} carries no modifiable field"
    item = _first_item(value)
    changed = {**item, name: _perturbed_scalar(item[name])}
    if isinstance(value, Mapping):
        first = next(iter(value))
        return {**value, first: changed}
    return [changed, *value[1:]]


def _diff_one_field(owner: type[BaseModel], field_name: str) -> tuple[list[str], list[str]]:
    """Perturb one field of *owner*'s sample; return the fields reported whole and those detailed."""
    old_dump = _dump_sample(owner)
    new_dump = {**old_dump, field_name: _perturbed(old_dump[field_name])}
    changed_fields, collection_changes = _diff_element(old_dump, new_dump, (owner,))
    return [name for name, _, _ in changed_fields], [change.field for change in collection_changes]


_UNIVERSE = [
    pytest.param(owner, field_name, id=f"{owner.__name__}.{field_name}")
    for owner, field_name in sorted(_container_fields(), key=str)
]
_COVERED_ROWS = [param for param in _UNIVERSE if (param.values[0], param.values[1]) in COLLECTION_IDENTITY]
_OUT_OF_SCOPE_ROWS = [param for param in _UNIVERSE if (param.values[0], param.values[1]) in _OUT_OF_SCOPE]

#: The covered rows split by whether an item can be *changed* in place: :func:`_perturbed`
#: only appends, so the modification is measured on the first half. Together the two
#: lists are the covered universe.
_MODIFIABLE_ROWS = [param for param in _COVERED_ROWS if _modifiable_field(*param.values)]
_UNMODIFIABLE_ROWS = [param for param in _COVERED_ROWS if not _modifiable_field(*param.values)]


def _union_member_classes() -> set[type[BaseModel]]:
    """Every model class that is one member of a union of models.

    Only such an element can keep its key while changing ``kind``, so only it can
    hold a field present on one side and absent on the other.
    """
    hints = get_type_hints(ModelSnapshot)
    annotations = [hints[layer.name] for layer in dataclasses.fields(ModelSnapshot)]
    annotations += [
        info.annotation for model_class in _reachable_models() for info in model_class.model_fields.values()
    ]
    members = [_model_members(annotation) for annotation in annotations]
    return {member for group in members if len(group) > 1 for member in group}


def _whole_value_member_classes() -> set[type[BaseModel]]:
    """Every model class that is a member of a ``whole_value`` collection.

    Such a member is never compared field by field, so the present-on-one-side
    shape cannot reach a collection it owns (a timing event's ``disabled_modes``).
    """
    return {
        member
        for (owner, field_name), rule in COLLECTION_IDENTITY.items()
        if rule.strategy == "whole_value"
        for member in _model_members(owner.model_fields[field_name].annotation)
    }


#: Every union variant declaring ``satisfies``; a ``kind`` change holds it on both
#: sides, so it never reaches the empty-against-absent shape.
_SATISFIES_ON_UNION_VARIANTS: frozenset[tuple[type[BaseModel], str]] = frozenset(
    (owner, "satisfies") for owner in (SRInterface, CSInterface, ExternalInterface, Processor, Core, Memory, Partition)
)
#: The union-variant collections that may be EMPTY: the external interface stub's two
#: lists, which reach the empty-against-absent shape and are reported whole, and ``satisfies``.
_EMPTY_ADMITTED: frozenset[tuple[type[BaseModel], str]] = (
    frozenset({(ExternalInterface, "operations"), (ExternalInterface, "data_elements")}) | _SATISFIES_ON_UNION_VARIANTS
)

_UNION_VARIANT_COLLECTIONS = [
    pytest.param(owner, field_name, id=f"{owner.__name__}.{field_name}")
    for owner, field_name in sorted(COLLECTION_IDENTITY, key=str)
    if owner in _union_member_classes()
    and owner not in _whole_value_member_classes()
    and (owner, field_name) not in _EMPTY_ADMITTED
]


class TestTheWholeValueFallbackStaysOutOfReach:
    """A changed collection always yields a ``CollectionChange``, so the renderers' skip hides nothing.

    The one exception, a collection empty on one side and absent on the other,
    needs a union variant changing ``kind``; it stays out of reach because
    ``schema.py`` refuses an empty collection on such variants, except the external
    interface stub's two lists, which reach the shape and are reported whole in
    ``changed_fields`` (pinned below). Nothing else pins that dependency.
    """

    def test_the_rows_are_the_union_variants_that_own_a_collection(self) -> None:
        """Non-vacuity: an empty row set would make the pin below prove nothing."""
        rows = {(param.values[0], param.values[1]) for param in _UNION_VARIANT_COLLECTIONS}

        assert rows == {
            (SRInterface, "data_elements"),
            (CSInterface, "operations"),
            (EnumDataType, "literals"),
            (RecordDataType, "fields"),
            # A `whole_value` collection of bare core names, refused empty too.
            (Memory, "accessible"),
            (ImplementationRecordDataType, "elements"),
            (TextTable, "entries"),
        }
        # `TimingEvent.disabled_modes` may be empty but is out of reach (a whole-value member).
        assert TimingEvent in _union_member_classes()
        assert TimingEvent in _whole_value_member_classes()
        assert (TimingEvent, "disabled_modes") in COLLECTION_IDENTITY

    def test_the_stub_lists_are_the_union_variant_collections_that_admit_empty(self) -> None:
        """The external interface stub's two lists are the exceptions, named and reached."""
        assert ExternalInterface in _union_member_classes()
        assert _EMPTY_ADMITTED <= set(COLLECTION_IDENTITY)
        assert ExternalInterface.model_validate({"name": "IStub", "kind": "external_interface"}).operations == []

    def test_satisfies_is_declared_on_every_variant_of_its_unions(self) -> None:
        """Every variant of its unions declares ``satisfies``, so it never reaches the shape."""
        assert {owner for owner, _field in _SATISFIES_ON_UNION_VARIANTS} <= _union_member_classes()
        for owner, field_name in _SATISFIES_ON_UNION_VARIANTS:
            assert field_name in owner.model_fields, owner
            assert owner.model_fields[field_name].default == []

    def test_an_empty_stub_list_against_an_absent_one_is_reported_whole(self) -> None:
        """The shape is reachable through a stub, and the change is still reported in ``changed_fields``."""
        old_dump = CSInterface(
            name="ISame",
            kind="client_server",
            operations=[CSOperation(name="Get", arguments=[])],
        ).model_dump(by_alias=True)
        new_dump = ExternalInterface(
            name="ISame", kind="external_interface", external_ref="/Ext/ISame", external_dest="CLIENT-SERVER-INTERFACE"
        ).model_dump(by_alias=True)
        changed_fields, collection_changes = _diff_element(old_dump, new_dump, (CSInterface, ExternalInterface))
        assert "data_elements" in [name for name, _, _ in changed_fields]
        assert "data_elements" not in [change.field for change in collection_changes]

    @pytest.mark.parametrize(("owner", "field_name"), _UNION_VARIANT_COLLECTIONS)
    def test_a_union_variant_refuses_an_empty_collection(self, owner: type[BaseModel], field_name: str) -> None:
        empty: Any = [] if _container_fields()[(owner, field_name)] == "list" else {}

        with pytest.raises(ValidationError):
            owner.model_validate({**_dump_sample(owner), field_name: empty})


class TestEachTableIsMeasuredAgainstTheDiffer:
    """Neither table is trusted: both are measured off one perturbation and one ``_diff_element`` call."""

    def test_the_rows_cover_the_whole_universe(self) -> None:
        assert len(_COVERED_ROWS) + len(_OUT_OF_SCOPE_ROWS) == len(_UNIVERSE)
        assert _COVERED_ROWS and _OUT_OF_SCOPE_ROWS, "an empty table would make its measurement vacuous"

    @pytest.mark.parametrize(("owner", "field_name"), _UNIVERSE)
    def test_the_perturbation_really_changes_the_field(self, owner: type[BaseModel], field_name: str) -> None:
        """Non-vacuity: the differ sees each perturbation as a change."""
        reported, _ = _diff_one_field(owner, field_name)

        assert reported == [field_name], reported

    @pytest.mark.parametrize(("owner", "field_name"), _COVERED_ROWS)
    def test_each_covered_field_is_really_detailed_item_by_item(self, owner: type[BaseModel], field_name: str) -> None:
        _, detailed = _diff_one_field(owner, field_name)

        assert detailed == [field_name], (
            f"{owner.__name__}.{field_name} has an identity rule but the differ reported no per-item "
            "detail for it — it would print its whole old value beside its whole new value"
        )

    def test_the_modifiable_split_covers_the_covered_rows(self) -> None:
        """The modifiable split covers the covered rows, with named rows pinned.

        The split reads the ``CollectionRule.key`` the cases grade, so a rule keying
        on the whole item would drop its own rows; the named rows guard that.
        """
        assert len(_MODIFIABLE_ROWS) + len(_UNMODIFIABLE_ROWS) == len(_COVERED_ROWS)
        assert _MODIFIABLE_ROWS and _UNMODIFIABLE_ROWS, "an empty half would make its measurement vacuous"

        modifiable = {param.values for param in _MODIFIABLE_ROWS}
        assert {
            (Composition, "ports"),
            (Composition, "members"),
            (Composition, "delegations"),
            (SWComponent, "ports"),
            (SRInterface, "data_elements"),
        } <= modifiable, sorted(f"{owner.__name__}.{field}" for owner, field in modifiable)

    @pytest.mark.parametrize(("owner", "field_name"), _MODIFIABLE_ROWS)
    def test_a_modified_item_is_one_changed_item_and_not_a_removal_beside_an_addition(
        self, owner: type[BaseModel], field_name: str
    ) -> None:
        """An item that kept its key and changed a field is one changed item, not a removal and an addition."""
        old_dump = _dump_sample(owner)
        new_dump = {**old_dump, field_name: _modified(owner, field_name)}

        _changed_fields, collection_changes = _diff_element(old_dump, new_dump, (owner,))

        assert [change.field for change in collection_changes] == [field_name]
        change = collection_changes[0]
        assert (change.added, change.removed) == ([], []), (
            f"{owner.__name__}.{field_name}: a modified item came out as a removal beside an addition"
        )
        assert len(change.changed) == 1, change.changed

    @pytest.mark.parametrize(("owner", "field_name"), _OUT_OF_SCOPE_ROWS)
    def test_each_out_of_scope_field_is_really_absent_from_the_per_item_diff(
        self, owner: type[BaseModel], field_name: str
    ) -> None:
        reported, detailed = _diff_one_field(owner, field_name)

        assert detailed == [], (
            f"{owner.__name__}.{field_name} is listed as out of scope but the differ details it; "
            "cover it in COLLECTION_IDENTITY instead of parking it here"
        )
        assert reported == [field_name], "an out-of-scope field is still reported whole, never dropped"
