"""Every ``DESC`` is carried or reported on import.

A ``DESC`` with a model field to hold it (a runnable's, a data element's, an operation's, an
argument's, an enum literal's, a record field's, an implementation element's, a composition
member's, a connector's, a delegation's) is carried: the first ``L-2``, ``""`` for an empty one. An
event's, an access point's or an array element's non-empty ``DESC`` is one ``NOT_SUPPORTED`` skip line
tagged ``DESC``, which aborts ``import component``. A consumed physics element with a ``DESC`` of its
own is created as an element that carries the text, with no line; a text equal to its type's or empty
gives no line either. ``import component`` sets the fields on create; ``--update`` keeps a target
runnable's text the file does not state and names it; a differing non-empty runnable text is a
difference, never ``unchanged``; a reused shared definition names each text it does not take, without
aborting. The merge keeps the primary's nested text and reports each one it drops.

The sources are written by SWCraft's own writer from copies of the ``component_exchange`` and
``compositions`` fixtures, then given the ``DESC``s under test.

**Not reached:** the real tool exports; a second ``L-2`` or inline markup (the reader keeps the first
``L-2``'s text, a stated limit); a behaviour's or a runnable's documentation children other than the
runnable's ``DESC``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import COMPOSITIONS_MODEL_ROOT
from lxml import etree
from project_support import files_of, init_project
from swcraft_tools.arxml_import import map_data_types, run_import
from swcraft_tools.arxml_import.merge import MERGED
from swcraft_tools.arxml_import.merge_verdict import decide_merges, record_dropped_nested_descriptions
from swcraft_tools.arxml_reader import (
    AppPrimitiveDataTypeInfo,
    AppRecordDataTypeInfo,
    ComponentPrototypeInfo,
    CompositionInfo,
    CompuMethodInfo,
    CompuScaleInfo,
    ConnectorPortRef,
    CSArgumentInfo,
    CSInterfaceInfo,
    CSOperationInfo,
    DelegationConnectorInfo,
    ImplDataTypeInfo,
    ImplSubElementInfo,
    ImportModel,
    RecordElementInfo,
    SkipCategory,
    SkipLog,
    SRDataElementInfo,
    SRInterfaceInfo,
    SwcInfo,
    SwcPortInfo,
    SwcRunnableInfo,
    read_arxml,
)
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.component_exchange import (
    ComponentImportConflictError,
    LossyComponentImportError,
    export_component,
    import_component,
)
from swcraft_tools.component_exchange.report import DESCRIPTIONS_NOT_TAKEN_HEADING
from swcraft_tools.data_description import read_data_description
from swcraft_tools.writer.arxml_generation import generate_arxml

NS = f"{{{NAMESPACE}}}"
EXCHANGE_MODEL_ROOT = Path(__file__).resolve().parent / "fixtures" / "component_exchange" / "model"

EVENT_REASON = "DESC not carried: the event's name is derived and the model holds no text for it"
ACCESS_REASON = "DESC not carried: the access point's name is derived and the model holds no text for it"
ARRAY_REASON = "DESC not carried: the array's element is not a model element"
PHYSICS_REASON = "DESC not carried: it differs from the type's description, which is carried"
PHYSICS_UNDESCRIBED_REASON = "DESC not carried: the type that consumes it carries no description"


# Sources


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _exchange_model(target: Path) -> Path:
    """A copy of the ``component_exchange`` model at *target*, widened to every member kind this module describes."""
    shutil.copytree(EXCHANGE_MODEL_ROOT, target)
    types = target / "data_types" / "implementation"
    _dump(
        types / "Frame_T.yaml",
        {
            "name": "Frame_T",
            "kind": "implementation_record",
            "elements": [{"name": "head", "type": "uint8"}, {"name": "tail", "type": "uint8"}],
        },
    )
    _dump(
        types / "Buf_T.yaml",
        {"name": "Buf_T", "kind": "implementation_array", "element_type": "uint8", "length": 4, "element_name": "Slot"},
    )
    pose = _load(target / "interfaces" / "IPose.yaml")
    pose["data_elements"] += [
        {"name": "frame", "implementation_type": "Frame_T"},
        {"name": "buf", "implementation_type": "Buf_T"},
    ]
    _dump(target / "interfaces" / "IPose.yaml", pose)
    sensor_file = target / "swc" / "sensing" / "SensorSwc.yaml"
    sensor = _load(sensor_file)
    sensor["runnables"][0]["calls"] = [{"port": "rCalib", "operation": "SetOffset"}]
    _dump(sensor_file, sensor)
    return target


def _written(model_root: Path, name: str) -> tuple[Path, str]:
    """A whole-model ARXML of *model_root*, written by SWCraft's own writer, and its path."""
    out = model_root.parent / name
    generate_arxml(str(model_root), str(out))
    return out, out.read_text(encoding="utf-8")


def _desc(text: str) -> etree._Element:
    desc = etree.Element(f"{NS}DESC")
    l2 = etree.SubElement(desc, f"{NS}L-2", L="FOR-ALL")
    if text:
        l2.text = text
    return desc


def _describe(element: etree._Element, text: str) -> None:
    """Give *element* a ``DESC`` holding *text* (an empty ``L-2`` for ``""``) after its name, replacing any."""
    for existing in element.findall(f"{NS}DESC"):
        element.remove(existing)
    for anchor in ("SHORT-NAME", "SHORT-LABEL"):
        found = element.find(f"{NS}{anchor}")
        if found is not None:
            found.addnext(_desc(text))
            return
    element.insert(0, _desc(text))


def _named(tree: etree._ElementTree, tag: str, name: str) -> etree._Element:
    found = [el for el in tree.iter(f"{NS}{tag}") if el.findtext(f"{NS}SHORT-NAME") == name]
    assert len(found) == 1, (tag, name, len(found))
    return found[0]


def _first(tree: etree._ElementTree, tag: str) -> etree._Element:
    return next(tree.iter(f"{NS}{tag}"))


def _scale(tree: etree._ElementTree, literal: str) -> etree._Element:
    found = [el for el in tree.iter(f"{NS}COMPU-SCALE") if el.findtext(f"{NS}COMPU-CONST/{NS}VT") == literal]
    assert len(found) == 1, literal
    return found[0]


def _save(tree: etree._ElementTree, path: Path) -> Path:
    path.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return path


def _patched(source: Path, target: Path, *edits: tuple[str, str, str]) -> Path:
    """*source* with one ``DESC`` per ``(tag, SHORT-NAME or literal, text)``, saved at *target*.

    The tag ``COMPU-SCALE`` names the scale by its literal; ``*`` as the name
    takes the first element of the tag.
    """
    tree = etree.parse(str(source))
    for tag, name, text in edits:
        if tag == "COMPU-SCALE":
            element = _scale(tree, name)
        elif name == "*":
            element = _first(tree, tag)
        else:
            element = _named(tree, tag, name)
        _describe(element, text)
    return _save(tree, target)


def _file_of(model_root: Path, layer: str, name: str) -> dict[str, Any]:
    (path,) = [path for path in (model_root / layer).rglob("*.yaml") if _load(path).get("name") == name]
    return _load(path)


@pytest.fixture(scope="module")
def exchange(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """The widened model and its SWCraft-written whole-model ARXML (one ``DESC``, the root composition's)."""
    base = tmp_path_factory.mktemp("exchange")
    model_root = _exchange_model(base / "model")
    source, text = _written(model_root, "model.arxml")
    assert text.count("<DESC>") == 1
    return model_root, source


@pytest.fixture(scope="module")
def sensor_export(exchange: tuple[Path, Path], tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``SensorSwc``'s own component export, from the widened model."""
    model_root, _source = exchange
    out = tmp_path_factory.mktemp("sensor") / "SensorSwc.arxml"
    export_component(str(model_root), "SensorSwc", str(out))
    return out


# Carried: import model

#: ``(ARXML tag, SHORT-NAME or literal, model layer, element, member path)`` --
#: one row per carried kind, the member path walking the written YAML.
CARRIED = [
    ("RUNNABLE-ENTITY", "SensorMain", "swc", "SensorSwc", ("runnables", "SensorMain")),
    ("VARIABLE-DATA-PROTOTYPE", "speed", "interfaces", "IVehicleSpeed", ("data_elements", "speed")),
    ("CLIENT-SERVER-OPERATION", "SetOffset", "interfaces", "ICalibration", ("operations", "SetOffset")),
    ("ARGUMENT-DATA-PROTOTYPE", "raw", "interfaces", "ICalibration", ("operations", "SetOffset", "arguments", "raw")),
    ("COMPU-SCALE", "DRIVE", "data_types", "Gear_T", ("literals", "DRIVE")),
    ("APPLICATION-RECORD-ELEMENT", "gear", "data_types", "Pose_T", ("fields", "gear")),
    ("IMPLEMENTATION-DATA-TYPE-ELEMENT", "tail", "data_types", "Frame_T", ("elements", "tail")),
]


def _member(document: dict[str, Any], walk: tuple[str, ...]) -> dict[str, Any]:
    node: Any = document
    for list_key, name in zip(walk[::2], walk[1::2], strict=True):
        (node,) = [item for item in node[list_key] if item["name"] == name]
    return node


class TestTheModelImportCarriesEachKind:
    @pytest.fixture(scope="class")
    @staticmethod
    def imported(exchange: tuple[Path, Path], tmp_path_factory: pytest.TempPathFactory) -> Path:
        _model_root, source = exchange
        base = tmp_path_factory.mktemp("carried")
        described = _patched(
            source,
            base / "described.arxml",
            *((tag, name, f"About {name}, in words.") for tag, name, *_rest in CARRIED),
        )
        project = init_project(base / "proj")
        run_import(str(described), str(project / "swcraft.yaml"), force=True)
        return project / "model"

    @pytest.mark.parametrize(("tag", "name", "layer", "element", "walk"), CARRIED, ids=[row[0] for row in CARRIED])
    def test_the_text_is_carried(
        self, imported: Path, tag: str, name: str, layer: str, element: str, walk: tuple[str, ...]
    ) -> None:
        member = _member(_file_of(imported, layer, element), walk)
        assert member["description"] == f"About {name}, in words.", tag

    @pytest.mark.parametrize(("tag", "name", "layer", "element", "walk"), CARRIED, ids=[row[0] for row in CARRIED])
    def test_an_empty_desc_gives_no_key(
        self,
        exchange: tuple[Path, Path],
        tmp_path: Path,
        tag: str,
        name: str,
        layer: str,
        element: str,
        walk: tuple[str, ...],
    ) -> None:
        _model_root, source = exchange
        empty = _patched(source, tmp_path / "empty.arxml", (tag, name, ""))
        project = init_project(tmp_path / "proj")
        report = run_import(str(empty), str(project / "swcraft.yaml"), force=True)

        assert "description" not in _member(_file_of(project / "model", layer, element), walk)
        assert [entry for entry in report.skip_entries if entry.tag == "DESC"] == []


class TestTheCompositionKindsAreReadAtLast:
    """A member's, a connector's and a delegation's ``DESC``: the fields existed and nothing read them."""

    def test_each_text_lands_in_its_field(self, tmp_path: Path) -> None:
        model_root = tmp_path / "model"
        shutil.copytree(COMPOSITIONS_MODEL_ROOT, model_root)
        source, _text = _written(model_root, "compositions.arxml")
        tree = etree.parse(str(source))
        connector = _first(tree, "ASSEMBLY-SW-CONNECTOR")
        delegation = _first(tree, "DELEGATION-SW-CONNECTOR")
        names = {
            "member": "Gamma",
            "connector": connector.findtext(f"{NS}SHORT-NAME"),
            "delegation": delegation.findtext(f"{NS}SHORT-NAME"),
        }
        _describe(_named(tree, "SW-COMPONENT-PROTOTYPE", "Gamma"), "The member's own words.")
        _describe(connector, "The connector's own words.")
        _describe(delegation, "The delegation's own words.")
        described = _save(tree, tmp_path / "described.arxml")
        project = init_project(tmp_path / "proj")

        run_import(str(described), str(project / "swcraft.yaml"), force=True)

        compositions = [_load(path) for path in (project / "model" / "compositions").rglob("*.yaml")]
        members = {m["name"]: m for c in compositions for m in c.get("members", [])}
        delegations = {d["name"]: d for c in compositions for d in c.get("delegations", [])}
        connector_file = _file_of(project / "model", "connectors", names["connector"])
        assert members["Gamma"]["description"] == "The member's own words."
        assert connector_file["description"] == "The connector's own words."
        assert delegations[names["delegation"]]["description"] == "The delegation's own words."


# Reported: the reader's family and the consumed physics

#: ``(tag, SHORT-NAME, reason, the entry's path suffix)`` -- the reader's family.
REPORTED = [
    ("TIMING-EVENT", "*", EVENT_REASON),
    ("SYNCHRONOUS-SERVER-CALL-POINT", "*", ACCESS_REASON),
    ("ELEMENT", "*", ARRAY_REASON),
    ("IMPLEMENTATION-DATA-TYPE-ELEMENT", "Slot", ARRAY_REASON),
]


def _desc_entries(skip_log: SkipLog) -> list[tuple[str, str, SkipCategory]]:
    return [(entry.path, entry.reason, entry.category) for entry in skip_log if entry.tag == "DESC"]


class TestTheReaderReportsWhatNoFieldHolds:
    @pytest.mark.parametrize(("tag", "name", "reason"), REPORTED, ids=[row[0] for row in REPORTED])
    def test_one_not_supported_line_at_the_owners_path(
        self, exchange: tuple[Path, Path], tmp_path: Path, tag: str, name: str, reason: str
    ) -> None:
        _model_root, source = exchange
        tree = etree.parse(str(source))
        owner = _first(tree, tag) if name == "*" else _named(tree, tag, name)
        owner_name = owner.findtext(f"{NS}SHORT-NAME")

        model = read_arxml(str(_patched(source, tmp_path / "one.arxml", (tag, name, "Words no field holds."))))

        ((path, found_reason, category),) = _desc_entries(model.skip_log)
        assert (found_reason, category) == (reason, SkipCategory.NOT_SUPPORTED)
        assert path.endswith(f"/{owner_name}")

    @pytest.mark.parametrize(("tag", "name", "reason"), REPORTED, ids=[row[0] for row in REPORTED])
    def test_an_empty_desc_is_no_line(
        self, exchange: tuple[Path, Path], tmp_path: Path, tag: str, name: str, reason: str
    ) -> None:
        _model_root, source = exchange
        model = read_arxml(str(_patched(source, tmp_path / "empty.arxml", (tag, name, ""))))
        assert _desc_entries(model.skip_log) == []

    def test_a_record_implementation_element_is_carried_not_reported(
        self, exchange: tuple[Path, Path], tmp_path: Path
    ) -> None:
        # The array's element is reported; a STRUCTURE's element is a model element and carries its text.
        _model_root, source = exchange
        model = read_arxml(
            str(_patched(source, tmp_path / "rec.arxml", ("IMPLEMENTATION-DATA-TYPE-ELEMENT", "tail", "t")))
        )
        assert _desc_entries(model.skip_log) == []


#: ``(tag, SHORT-NAME)`` of each physics element ``Velocity_T`` and ``Gear_T`` consume.
PHYSICS = [("UNIT", "*"), ("DATA-CONSTR", "*"), ("COMPU-METHOD", "Velocity_T_CompuMethod"), ("COMPU-METHOD", "Gear_T")]


def _physics_name(source: Path, tag: str, name: str) -> str:
    """The SHORT-NAME of the physics element a row means (the writer derives them)."""
    tree = etree.parse(str(source))
    if name == "*":
        return _first(tree, tag).findtext(f"{NS}SHORT-NAME")
    candidates = [el.findtext(f"{NS}SHORT-NAME") for el in tree.iter(f"{NS}{tag}")]
    (found,) = [candidate for candidate in candidates if candidate.startswith(name.split("_CompuMethod")[0])]
    return found


class TestAConsumedPhysicsElementIsComparedWithItsType:
    """Such a text is compared with its type's; the rule compares."""

    @staticmethod
    def _owner(tag: str, name: str) -> str:
        return "Gear_T" if name == "Gear_T" else "Velocity_T"

    @pytest.mark.parametrize(("tag", "name"), PHYSICS, ids=["UNIT", "DATA-CONSTR", "LINEAR", "TEXTTABLE"])
    @pytest.mark.parametrize("type_text", ["The type's words.", ""], ids=["described-type", "undescribed-type"])
    def test_a_text_of_its_own_is_carried_by_the_element_it_now_is(
        self, exchange: tuple[Path, Path], tmp_path: Path, tag: str, name: str, type_text: str
    ) -> None:
        """A form with a ``DESC`` of its own is no SWCraft form: it is created as an element carrying the text.

        The generated form carries no ``DESC``, so no line is owed. The comparison stays for a form
        still read inline (``test_an_equal_or_empty_text_is_no_line``).
        """
        _model_root, source = exchange
        element = _physics_name(source, tag, name)
        patched = _patched(
            source,
            tmp_path / "own-words.arxml",
            ("APPLICATION-PRIMITIVE-DATA-TYPE", self._owner(tag, name), type_text),
            (tag, element, "The physics element's own words."),
        )
        forms: list = []
        _types, _registry, skips = map_data_types(read_arxml(str(patched)), elements=forms)

        assert _desc_entries(skips) == []
        (carried,) = [form for form in forms if form.name == element]
        assert carried.description == "The physics element's own words."

    @pytest.mark.parametrize(("tag", "name"), PHYSICS, ids=["UNIT", "DATA-CONSTR", "LINEAR", "TEXTTABLE"])
    @pytest.mark.parametrize("text", ["The type's words.", ""], ids=["equal", "empty"])
    def test_an_equal_or_empty_text_is_no_line(
        self, exchange: tuple[Path, Path], tmp_path: Path, tag: str, name: str, text: str
    ) -> None:
        _model_root, source = exchange
        patched = _patched(
            source,
            tmp_path / "equal.arxml",
            ("APPLICATION-PRIMITIVE-DATA-TYPE", self._owner(tag, name), "The type's words."),
            (tag, _physics_name(source, tag, name), text),
        )
        _types, _registry, skips = map_data_types(read_arxml(str(patched)), elements=[])
        assert _desc_entries(skips) == []


class TestTheModelImportReportsEachLineInSectionTwo:
    def test_the_lines_are_skip_entries_of_the_report(self, exchange: tuple[Path, Path], tmp_path: Path) -> None:
        _model_root, source = exchange
        patched = _patched(
            source, tmp_path / "reported.arxml", *((tag, name, "Lost words.") for tag, name, _r in REPORTED)
        )
        project = init_project(tmp_path / "proj")

        report = run_import(str(patched), str(project / "swcraft.yaml"), dry_run=True, force=True)

        reported = sorted(entry.reason for entry in report.skip_entries if entry.tag == "DESC")
        assert reported == sorted(reason for _tag, _name, reason in REPORTED)
        assert {entry.category for entry in report.skip_entries if entry.tag == "DESC"} == {SkipCategory.NOT_SUPPORTED}


# import component: the reported families abort, and a foreign DESC is taken


class TestEachReportedFamilyAbortsTheComponentImport:
    @pytest.mark.parametrize(
        ("edits", "reason"),
        [
            ((("TIMING-EVENT", "*", "w"),), EVENT_REASON),
            ((("SYNCHRONOUS-SERVER-CALL-POINT", "*", "w"),), ACCESS_REASON),
            ((("ELEMENT", "*", "w"),), ARRAY_REASON),
            ((("IMPLEMENTATION-DATA-TYPE-ELEMENT", "Slot", "w"),), ARRAY_REASON),
        ],
        ids=["event", "access-point", "application-array-element", "implementation-array-element"],
    )
    def test_the_line_is_in_the_refusal_and_nothing_is_written(
        self, sensor_export: Path, tmp_path: Path, edits: tuple[tuple[str, str, str], ...], reason: str
    ) -> None:
        resolved = tuple(
            (tag, _physics_name(sensor_export, tag, name) if tag == "COMPU-METHOD" else name, text)
            for tag, name, text in edits
        )
        foreign = _patched(sensor_export, tmp_path / "foreign.arxml", *resolved)
        project = init_project(tmp_path / "proj")
        before = files_of(project)

        with pytest.raises(LossyComponentImportError) as caught:
            import_component(str(foreign), str(project / "model"))

        assert f": {reason}" in str(caught.value)
        assert files_of(project) == before

    def test_a_described_compu_method_is_an_element_and_its_text_is_carried(
        self, sensor_export: Path, tmp_path: Path
    ) -> None:
        """A described compu method is no SWCraft form: ``import component`` creates it as an element.

        The generated one carries no ``DESC``, so the ``texttable`` element holds the text, the
        enumeration names it, and nothing is refused.
        """
        method = _physics_name(sensor_export, "COMPU-METHOD", "Gear_T")
        foreign = _patched(
            sensor_export,
            tmp_path / "foreign.arxml",
            ("APPLICATION-PRIMITIVE-DATA-TYPE", "Gear_T", "a"),
            ("COMPU-METHOD", method, "b"),
        )
        project = init_project(tmp_path / "proj")

        report = import_component(str(foreign), str(project / "model"))

        assert report.created_elements == {"compu_methods": [method]}
        description = read_data_description(str(project / "model"))
        (table,) = description.compu_methods
        assert (table.name, table.description) == (method, "b")
        (gear,) = [data_type for data_type in description.data_types if data_type.name == "Gear_T"]
        assert (gear.description, gear.compu_method_ref, gear.literals) == ("a", method, None)


class TestAForeignDescSetsTheFieldsOnCreate:
    def test_every_member_kind_takes_its_text(self, sensor_export: Path, tmp_path: Path) -> None:
        foreign = _patched(
            sensor_export,
            tmp_path / "foreign.arxml",
            *((tag, name, f"Foreign {name}.") for tag, name, *_rest in CARRIED),
        )
        project = init_project(tmp_path / "proj")

        report = import_component(str(foreign), str(project / "model"))

        assert report.swc_action == "created"
        for tag, name, layer, element, walk in CARRIED:
            member = _member(_file_of(project / "model", layer, element), walk)
            assert member["description"] == f"Foreign {name}.", tag


# import component: the runnable's description on --update, and as a difference


def _target(exchange: tuple[Path, Path], tmp_path: Path, *, description: str, interval: float = 0.005) -> Path:
    """A project holding the widened model, ``SensorMain`` described and at *interval*."""
    model_root, _source = exchange
    project = init_project(tmp_path / "target")
    shutil.rmtree(project / "model")
    shutil.copytree(model_root, project / "model")
    sensor_file = project / "model" / "swc" / "sensing" / "SensorSwc.yaml"
    sensor = _load(sensor_file)
    sensor["runnables"][0]["min_start_interval"] = interval
    if description:
        sensor["runnables"][0]["description"] = description
    _dump(sensor_file, sensor)
    return project / "model"


def _runnable(model_root: Path) -> dict[str, Any]:
    return _member(_file_of(model_root, "swc", "SensorSwc"), ("runnables", "SensorMain"))


class TestTheUpdateKeepsTheTargetsRunnableText:
    @pytest.mark.parametrize("incoming", [None, ""], ids=["absent", "empty"])
    def test_a_file_stating_none_keeps_it_and_says_so(
        self, exchange: tuple[Path, Path], sensor_export: Path, tmp_path: Path, incoming: str | None
    ) -> None:
        # The target's interval differs, so the file is a real update; a file with no runnable DESC, and an
        # empty one, say as little.
        model_root = _target(exchange, tmp_path, description="The target's words.", interval=0.006)
        source = (
            sensor_export
            if incoming is None
            else _patched(sensor_export, tmp_path / "empty.arxml", ("RUNNABLE-ENTITY", "SensorMain", incoming))
        )

        report = import_component(str(source), str(model_root), update=True)

        assert report.swc_action == "updated"
        assert _runnable(model_root)["description"] == "The target's words."
        assert report.preserved_descriptions == ["runnable 'SensorMain'"]
        assert "DESC of runnable 'SensorMain' preserved from target: source element carries none" in report.render()
        assert "description" not in report.swc_diff_preview

    def test_a_differing_text_replaces_it(
        self, exchange: tuple[Path, Path], sensor_export: Path, tmp_path: Path
    ) -> None:
        model_root = _target(exchange, tmp_path, description="The target's words.", interval=0.006)
        source = _patched(sensor_export, tmp_path / "new.arxml", ("RUNNABLE-ENTITY", "SensorMain", "The file's words."))

        report = import_component(str(source), str(model_root), update=True)

        assert _runnable(model_root)["description"] == "The file's words."
        assert report.preserved_descriptions == []
        assert "preserved from target: source element carries none" not in report.render()


class TestADifferingRunnableTextIsNeverUnchanged:
    """The file's only difference is the runnable's non-empty ``DESC``."""

    def test_without_update_it_is_a_conflict_naming_the_runnable(
        self, exchange: tuple[Path, Path], sensor_export: Path, tmp_path: Path
    ) -> None:
        model_root = _target(exchange, tmp_path, description="Old words.")
        source = _patched(sensor_export, tmp_path / "new.arxml", ("RUNNABLE-ENTITY", "SensorMain", "New words."))
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError) as caught:
            import_component(str(source), str(model_root))

        assert "the description of runnable 'SensorMain' differs from the target's" in str(caught.value)
        assert files_of(model_root) == before

    def test_with_update_it_is_replaced(self, exchange: tuple[Path, Path], sensor_export: Path, tmp_path: Path) -> None:
        model_root = _target(exchange, tmp_path, description="Old words.")
        source = _patched(sensor_export, tmp_path / "new.arxml", ("RUNNABLE-ENTITY", "SensorMain", "New words."))

        report = import_component(str(source), str(model_root), update=True)

        assert report.swc_action == "updated"
        assert _runnable(model_root)["description"] == "New words."

    @pytest.mark.parametrize("incoming", ["Old words.", "", None], ids=["equal", "empty", "absent"])
    def test_an_equal_empty_or_absent_text_is_unchanged(
        self, exchange: tuple[Path, Path], sensor_export: Path, tmp_path: Path, incoming: str | None
    ) -> None:
        model_root = _target(exchange, tmp_path, description="Old words.")
        source = (
            sensor_export
            if incoming is None
            else _patched(sensor_export, tmp_path / "same.arxml", ("RUNNABLE-ENTITY", "SensorMain", incoming))
        )
        before = files_of(model_root)

        report = import_component(str(source), str(model_root))

        assert report.swc_action == "unchanged"
        assert files_of(model_root) == before


# import component: a reused shared definition names every text it does not take

#: ``(tag, SHORT-NAME or literal, the line's element/member)`` -- nested and element-level.
NOT_TAKEN = [
    ("VARIABLE-DATA-PROTOTYPE", "speed", "IVehicleSpeed/speed"),
    ("CLIENT-SERVER-OPERATION", "SetOffset", "ICalibration/SetOffset"),
    ("ARGUMENT-DATA-PROTOTYPE", "raw", "ICalibration/SetOffset/raw"),
    ("COMPU-SCALE", "DRIVE", "Gear_T/DRIVE"),
    ("APPLICATION-RECORD-ELEMENT", "gear", "Pose_T/gear"),
    ("IMPLEMENTATION-DATA-TYPE-ELEMENT", "tail", "Frame_T/tail"),
    ("SENDER-RECEIVER-INTERFACE", "IPose", "IPose"),
    ("APPLICATION-RECORD-DATA-TYPE", "Pose_T", "Pose_T"),
]


class TestAReusedDefinitionNamesTheTextsItDoesNotTake:
    @pytest.mark.parametrize(("tag", "name", "where"), NOT_TAKEN, ids=[row[2] for row in NOT_TAKEN])
    def test_one_line_each_the_target_untouched_and_no_abort(
        self, exchange: tuple[Path, Path], sensor_export: Path, tmp_path: Path, tag: str, name: str, where: str
    ) -> None:
        model_root = _target(exchange, tmp_path, description="")
        source = _patched(sensor_export, tmp_path / "described.arxml", (tag, name, "The file's words."))
        before = files_of(model_root)

        report = import_component(str(source), str(model_root))

        assert report.swc_action == "unchanged"
        assert report.descriptions_not_taken_lines == [f"DESC of {where} not taken: the target definition is kept"]
        assert f"{DESCRIPTIONS_NOT_TAKEN_HEADING} (1; not supported by SWCraft" in report.render()
        assert files_of(model_root) == before

    @pytest.mark.parametrize("text", ["The target's words.", ""], ids=["equal", "empty"])
    def test_an_equal_or_empty_text_gives_no_line(
        self, exchange: tuple[Path, Path], sensor_export: Path, tmp_path: Path, text: str
    ) -> None:
        model_root = _target(exchange, tmp_path, description="")
        speed_file = next((model_root / "interfaces").rglob("IVehicleSpeed.yaml"))
        speed = _load(speed_file)
        speed["data_elements"][0]["description"] = "The target's words."
        _dump(speed_file, speed)
        source = _patched(sensor_export, tmp_path / "same.arxml", ("VARIABLE-DATA-PROTOTYPE", "speed", text))

        report = import_component(str(source), str(model_root))

        assert "IVehicleSpeed" in report.reused_interfaces
        assert report.descriptions_not_taken_lines == []
        assert DESCRIPTIONS_NOT_TAKEN_HEADING not in report.render()


class TestTheFirstL2IsTheTextAndTheRestIsLostWithoutALine:
    """Pins the reader's stated limit, not a design goal.

    ``_extract_description`` keeps the first ``L-2``'s own text: a second language block and whatever
    follows inline markup are lost, and no report line names them. A change to it is a decision.
    """

    @staticmethod
    def _element(desc_body: str) -> Any:
        import xml.etree.ElementTree as ET

        return ET.fromstring(
            f'<RUNNABLE-ENTITY xmlns="{NAMESPACE}"><SHORT-NAME>Run</SHORT-NAME>'
            f"<DESC>{desc_body}</DESC></RUNNABLE-ENTITY>"
        )

    def test_of_two_languages_the_first_is_kept(self) -> None:
        from swcraft_tools.arxml_reader import _extract_description

        element = self._element('<L-2 L="EN">English words.</L-2><L-2 L="DE">Deutsche Worte.</L-2>')
        assert _extract_description(element) == "English words."

    def test_inline_markup_cuts_the_text_at_its_first_child(self) -> None:
        from swcraft_tools.arxml_reader import _extract_description

        element = self._element('<L-2 L="EN">Before <E TYPE="BOLD">bold</E> after.</L-2>')
        # The text node is read exactly, its trailing space included.
        assert _extract_description(element) == "Before "


# The merge: a nested description is reported, never dropped without a word

_REF = ConnectorPortRef(component_ref="/Shared/Root/Member", port_ref="/Shared/Types/Leaf/p")


def _swc(path: str, port_text: str, runnable_text: str) -> SwcInfo:
    return SwcInfo(
        path=path,
        name="Comp",
        description="",
        ports=[
            SwcPortInfo(path=f"{path}/p", name="p", kind="provide", interface_ref="/Shared/I", description=port_text)
        ],
        runnables=[
            SwcRunnableInfo(path=f"{path}/Beh/Run", name="Run", min_start_interval=None, description=runnable_text)
        ],
        events=[],
    )


def _sr(path: str, text: str) -> SRInterfaceInfo:
    return SRInterfaceInfo(
        path=path,
        name="ISig",
        description="",
        data_elements=[SRDataElementInfo(name="d", type_ref="/Shared/T", description=text)],
    )


def _cs(path: str, operation_text: str, argument_text: str) -> CSInterfaceInfo:
    argument = CSArgumentInfo(name="a", type_ref="/Shared/T", direction="IN", description=argument_text)
    return CSInterfaceInfo(
        path=path,
        name="ICall",
        description="",
        operations=[CSOperationInfo(name="Op", arguments=[argument], description=operation_text)],
    )


def _enum(model: ImportModel, package: str, text: str) -> AppPrimitiveDataTypeInfo:
    model.compu_methods.append(
        CompuMethodInfo(
            path=f"{package}/CM",
            name="CM",
            category="TEXTTABLE",
            scales=[CompuScaleInfo(value=0, literal="OFF", description=text)],
            description="",
            scale_count=1,
        )
    )
    return AppPrimitiveDataTypeInfo(
        path=f"{package}/Mode_T", name="Mode_T", compu_method_ref=f"{package}/CM", description=""
    )


def _record(path: str, text: str) -> AppRecordDataTypeInfo:
    return AppRecordDataTypeInfo(
        path=path,
        name="Pose_T",
        elements=[RecordElementInfo(name="x", type_ref="/Shared/T", description=text)],
        description="",
    )


def _implementation_record(path: str, text: str) -> ImplDataTypeInfo:
    element = ImplSubElementInfo(
        name="e", category="TYPE_REFERENCE", impl_type_ref="/Shared/T", base_type_ref=None, description=text
    )
    return ImplDataTypeInfo(
        path=path,
        name="Frame_T",
        category="STRUCTURE",
        base_type_ref=None,
        impl_type_ref=None,
        description="",
        sub_elements=(element,),
    )


def _composition(path: str, member_text: str, delegation_text: str) -> CompositionInfo:
    return CompositionInfo(
        path=path,
        name="Root",
        description="",
        ports=[],
        prototypes=[
            ComponentPrototypeInfo(
                path=f"{path}/m",
                name="m",
                type_ref="/Shared/Leaf",
                dest="APPLICATION-SW-COMPONENT-TYPE",
                description=member_text,
            )
        ],
        assembly_connectors=[],
        delegation_connectors=[
            DelegationConnectorInfo(
                path=f"{path}/d", name="d", inner=_REF, outer_port_ref=None, description=delegation_text
            )
        ],
    )


def _add(model: ImportModel, kind: str, package: str, text: str) -> None:
    """One declaration of *kind* under *package*, whose nested description of that kind holds *text*."""
    if kind in {"port", "runnable"}:
        model.swcs.append(_swc(f"{package}/Comp", text if kind == "port" else "", text if kind == "runnable" else ""))
    elif kind == "data element":
        model.sr_interfaces.append(_sr(f"{package}/ISig", text))
    elif kind in {"operation", "argument"}:
        model.cs_interfaces.append(
            _cs(f"{package}/ICall", text if kind == "operation" else "", text if kind == "argument" else "")
        )
    elif kind == "enum literal":
        model.app_primitive_data_types.append(_enum(model, package, text))
    elif kind == "record field":
        model.app_record_data_types.append(_record(f"{package}/Pose_T", text))
    elif kind == "implementation element":
        model.impl_data_types.append(_implementation_record(f"{package}/Frame_T", text))
    else:
        model.compositions.append(
            _composition(f"{package}/Root", text if kind == "member" else "", text if kind == "delegation" else "")
        )


#: ``(kind, the member's path below the element, its source path under /B, the element, the tag)``.
#: A component's and a composition's members are named with their list (``ports/p``),
#: because a port and a runnable -- or a port and a member -- may share a name.
NESTED = [
    ("port", "ports/p", "/B/Comp/p", "Comp", "APPLICATION-SW-COMPONENT-TYPE"),
    ("runnable", "runnables/Run", "/B/Comp/Beh/Run", "Comp", "APPLICATION-SW-COMPONENT-TYPE"),
    ("data element", "d", "/B/ISig/d", "ISig", "SENDER-RECEIVER-INTERFACE"),
    ("operation", "Op", "/B/ICall/Op", "ICall", "CLIENT-SERVER-INTERFACE"),
    ("argument", "Op/a", "/B/ICall/Op/a", "ICall", "CLIENT-SERVER-INTERFACE"),
    # The table is an element of its own and its group reports its entries' texts; the enum merges too.
    ("enum literal", "OFF", "/B/CM/OFF", "CM", "COMPU-METHOD"),
    ("record field", "x", "/B/Pose_T/x", "Pose_T", "APPLICATION-RECORD-DATA-TYPE"),
    ("implementation element", "e", "/B/Frame_T/e", "Frame_T", "IMPLEMENTATION-DATA-TYPE"),
    ("member", "prototypes/m", "/B/Root/m", "Root", "COMPOSITION-SW-COMPONENT-TYPE"),
    ("delegation", "delegations/d", "/B/Root/d", "Root", "COMPOSITION-SW-COMPONENT-TYPE"),
]


def _merged_pair(kind: str, primary_text: str, other_text: str) -> tuple[ImportModel, SkipLog]:
    model = ImportModel(source_path="inline-test")
    _add(model, kind, "/A", primary_text)
    _add(model, kind, "/B", other_text)
    decisions = decide_merges(model)
    assert [group.verdict for group in decisions.groups] == [MERGED] * (2 if kind == "enum literal" else 1), kind
    skips = SkipLog()
    record_dropped_nested_descriptions(decisions, skips)
    return model, skips


class TestAMergeReportsEachDroppedNestedText:
    @pytest.mark.parametrize(("kind", "member", "source", "element", "tag"), NESTED, ids=[row[0] for row in NESTED])
    def test_a_differing_later_text_is_one_section_two_line(
        self, kind: str, member: str, source: str, element: str, tag: str
    ) -> None:
        _model, skips = _merged_pair(kind, "The primary's words.", "The later words.")

        (entry,) = list(skips)
        assert (entry.path, entry.tag, entry.category) == (source, "DESC", SkipCategory.NOT_SUPPORTED)
        assert (
            entry.reason == f"DESC of {element}/{member} not carried: the merged {tag} keeps /A/{element}'s description"
        )

    @pytest.mark.parametrize(("kind", "member", "source", "element", "tag"), NESTED, ids=[row[0] for row in NESTED])
    @pytest.mark.parametrize(
        ("primary", "other"),
        [("Same words.", "Same words."), ("The primary's words.", ""), ("", "")],
        ids=["equal", "dropped-empty", "both-empty"],
    )
    def test_an_equal_or_empty_dropped_text_is_no_line(
        self, kind: str, member: str, source: str, element: str, tag: str, primary: str, other: str
    ) -> None:
        _model, skips = _merged_pair(kind, primary, other)
        assert list(skips) == []

    def test_a_text_the_primary_lacks_is_dropped_too(self) -> None:
        _model, skips = _merged_pair("data element", "", "Only the later one says this.")
        assert [entry.path for entry in skips] == ["/B/ISig/d"]

    def test_a_port_and_a_runnable_of_one_name_never_shadow_each_other(self) -> None:
        """A port and a runnable ``X`` are legal together: each list's text is compared with its own."""

        def component(package: str, port_text: str, runnable_text: str) -> SwcInfo:
            info = _swc(f"{package}/Comp", port_text, runnable_text)
            info.runnables[0] = SwcRunnableInfo(
                path=f"{package}/Comp/Beh/p", name="p", min_start_interval=None, description=runnable_text
            )
            return info

        model = ImportModel(source_path="inline-test")
        model.swcs.append(component("/A", "Port words.", "Runnable words."))
        model.swcs.append(component("/B", "Port words.", "Other runnable words."))
        skips = SkipLog()
        record_dropped_nested_descriptions(decide_merges(model), skips)

        (entry,) = list(skips)
        assert entry.path == "/B/Comp/Beh/p"
        assert entry.reason.startswith("DESC of Comp/runnables/p not carried")

    def test_three_declarations_give_one_line_per_later_member(self) -> None:
        model = ImportModel(source_path="inline-test")
        for package, text in (("/A", "First."), ("/B", "Second."), ("/C", "Third.")):
            _add(model, "data element", package, text)
        skips = SkipLog()
        record_dropped_nested_descriptions(decide_merges(model), skips)
        assert sorted(entry.path for entry in skips) == ["/B/ISig/d", "/C/ISig/d"]

    def test_an_array_element_gives_no_merge_line(self) -> None:
        """The reader reports an array element's ``DESC`` on every declaration, so the merge adds none."""
        model = ImportModel(source_path="inline-test")
        for package, text in (("/A", "First."), ("/B", "Second.")):
            element = ImplSubElementInfo(
                name="e", category="TYPE_REFERENCE", impl_type_ref="/Shared/T", base_type_ref=None, description=text
            )
            model.impl_data_types.append(
                ImplDataTypeInfo(
                    path=f"{package}/Buf_T",
                    name="Buf_T",
                    category="ARRAY",
                    base_type_ref=None,
                    impl_type_ref=None,
                    description="",
                    sub_elements=(element,),
                )
            )
        decisions = decide_merges(model)
        assert [group.verdict for group in decisions.groups] == [MERGED]
        skips = SkipLog()
        record_dropped_nested_descriptions(decisions, skips)
        assert list(skips) == []

    def test_a_repeated_member_path_keeps_its_first_text(self) -> None:
        """Two literals of one label on one method: the first text is the one compared, as the mapper keeps it."""

        def method(package: str, first: str, second: str) -> CompuMethodInfo:
            return CompuMethodInfo(
                path=f"{package}/CM",
                name="CM",
                category="TEXTTABLE",
                scales=[
                    CompuScaleInfo(value=0, literal="OFF", description=first),
                    CompuScaleInfo(value=1, literal="OFF", description=second),
                ],
                description="",
                scale_count=2,
            )

        model = ImportModel(source_path="inline-test")
        for package, first, second in (("/A", "Kept.", "Primary's second."), ("/B", "Kept.", "Later second.")):
            model.compu_methods.append(method(package, first, second))
            model.app_primitive_data_types.append(
                AppPrimitiveDataTypeInfo(
                    path=f"{package}/Mode_T", name="Mode_T", compu_method_ref=f"{package}/CM", description=""
                )
            )
        skips = SkipLog()
        record_dropped_nested_descriptions(decide_merges(model), skips)
        assert list(skips) == []

    def test_an_enum_whose_method_is_missing_gives_no_line(self) -> None:
        model = ImportModel(source_path="inline-test")
        for package in ("/A", "/B"):
            model.app_primitive_data_types.append(
                AppPrimitiveDataTypeInfo(
                    path=f"{package}/Mode_T", name="Mode_T", compu_method_ref=f"{package}/Gone", description=""
                )
            )
        skips = SkipLog()
        record_dropped_nested_descriptions(decide_merges(model), skips)
        assert list(skips) == []


class TestTheModelImportKeepsThePrimarysNestedText:
    """End to end: a whole ``/PortInterfaces`` package declared twice, the later copy describing one element."""

    def test_the_primary_is_written_and_the_later_text_is_reported(
        self, exchange: tuple[Path, Path], tmp_path: Path
    ) -> None:
        _model_root, source = exchange
        tree = etree.parse(str(source))
        package = next(
            pkg for pkg in tree.iter(f"{NS}AR-PACKAGE") if pkg.findtext(f"{NS}SHORT-NAME") == "PortInterfaces"
        )
        copy = etree.fromstring(etree.tostring(package))
        copy.find(f"{NS}SHORT-NAME").text = "PortInterfacesCopy"
        package.addnext(copy)
        (speed,) = [
            element
            for element in copy.iter(f"{NS}VARIABLE-DATA-PROTOTYPE")
            if element.findtext(f"{NS}SHORT-NAME") == "speed"
        ]
        _describe(speed, "The copy's words.")
        (primary_speed,) = [
            element
            for element in package.iter(f"{NS}VARIABLE-DATA-PROTOTYPE")
            if element.findtext(f"{NS}SHORT-NAME") == "speed"
        ]
        _describe(primary_speed, "The primary's words.")
        duplicated = _save(tree, tmp_path / "duplicated.arxml")
        project = init_project(tmp_path / "proj")

        report = run_import(str(duplicated), str(project / "swcraft.yaml"), force=True)

        (line,) = [entry for entry in report.skip_entries if entry.tag == "DESC"]
        assert line.path == "/PortInterfacesCopy/IVehicleSpeed/speed"
        assert line.reason == (
            "DESC of IVehicleSpeed/speed not carried: the merged SENDER-RECEIVER-INTERFACE keeps "
            "/PortInterfaces/IVehicleSpeed's description"
        )
        assert line.category == SkipCategory.NOT_SUPPORTED
        written = _member(_file_of(project / "model", "interfaces", "IVehicleSpeed"), ("data_elements", "speed"))
        assert written["description"] == "The primary's words."
