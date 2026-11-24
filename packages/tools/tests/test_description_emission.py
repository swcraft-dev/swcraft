"""The writer writes every declared ``description`` as a ``DESC``, and a title as a ``LONG-NAME``.

On the writer's side and on ``import component``'s:

* **every kind that emits an element** writes its ``description`` as one ``DESC`` holding one
  ``L-2 L="FOR-ALL"``, directly after the ``SHORT-NAME`` (after the ``LONG-NAME`` where one is
  written) -- the ``Identifiable`` order, the same at 4.2.2 and 4.3.0 -- and an enumeration literal's
  on its ``COMPU-SCALE``, between ``SHORT-LABEL`` and ``LOWER-LIMIT``;
* **the text is written exactly as held**; only the empty default writes nothing;
* **an enumeration's own text is on its type alone**, never on the generated ``{enum}_CompuMethod``;
* **a composition's ``title`` is its ``LONG-NAME``** exactly when it differs from its ``name``;
* **an external stub emits no element**, so its text is nowhere in the document;
* **``import component``** compares the component's, each port's and each runnable's text: an absent
  incoming one keeps the target's and says so, and a differing non-empty one is a conflict without
  ``--update`` and wins with it;
* **the three goldens** match their recorded pre-fidelity digests after every reversal of the fidelity
  harness; the comparison does not isolate the ``DESC``s and ``LONG-NAME``s from the other fidelity
  changes.

**Not reached:** the XSD validity of the written documents (the ``test-xsd`` suite), and an import's
reading of the texts beyond the round trip asserted here (``test_description_import.py``,
``test_behavioural_import.py``).
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import COMPOSITIONS_MODEL_ROOT, make_empty_model_root, model_element_path, write_model_element
from fidelity_strip_support import strip_fidelity
from lxml import etree
from project_support import files_of, init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.component_exchange import ComponentImportConflictError, export_component, import_component
from swcraft_tools.writer.arxml_generation import generate_arxml
from swcraft_tools.writer.arxml_xml import make_element, set_description, set_long_name, sub_element
from test_writer_fidelity_refreeze import PRE_CHANGE

NS = f"{{{NAMESPACE}}}"
GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "arxml_golden"
EXCHANGE_MODEL_ROOT = Path(__file__).resolve().parent / "fixtures" / "component_exchange" / "model"
VERSIONS = ("4.2.2", "4.3.0")


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n")


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _text(tag: str, name: str) -> str:
    """The description the model gives the element *name* emitted as *tag*: unique, so a find is unambiguous."""
    return f"About {name}, the {tag.lower()}."


#: ``(ARXML tag, SHORT-NAME)`` of every element the described model gives a text, one row per kind
#: that emits an element (the component port and the composition port are both a ``*-PORT-PROTOTYPE``).
DESCRIBED = [
    ("APPLICATION-SW-COMPONENT-TYPE", "Gamma"),
    ("R-PORT-PROTOTYPE", "rInner"),
    ("RUNNABLE-ENTITY", "GammaMain"),
    ("COMPOSITION-SW-COMPONENT-TYPE", "Sub"),
    ("P-PORT-PROTOTYPE", "pSignalOut"),
    ("SW-COMPONENT-PROTOTYPE", "Alpha"),
    ("ASSEMBLY-SW-CONNECTOR", "sub_link"),
    ("DELEGATION-SW-CONNECTOR", "Sub_pSignalOut_Alpha"),
    ("SENDER-RECEIVER-INTERFACE", "ISignal"),
    ("VARIABLE-DATA-PROTOTYPE", "value"),
    ("CLIENT-SERVER-INTERFACE", "ICalc"),
    ("CLIENT-SERVER-OPERATION", "Op"),
    ("ARGUMENT-DATA-PROTOTYPE", "x"),
    ("SW-BASE-TYPE", "uint8_base"),
    ("IMPLEMENTATION-DATA-TYPE", "uint8"),
    ("IMPLEMENTATION-DATA-TYPE", "U8Ref"),
    ("IMPLEMENTATION-DATA-TYPE", "Frame_T"),
    ("IMPLEMENTATION-DATA-TYPE-ELEMENT", "head"),
    ("IMPLEMENTATION-DATA-TYPE", "Buf_T"),
    ("IMPLEMENTATION-DATA-TYPE", "Buf_P"),
    ("APPLICATION-PRIMITIVE-DATA-TYPE", "Mode_T"),
    ("APPLICATION-PRIMITIVE-DATA-TYPE", "Level_T"),
    ("APPLICATION-RECORD-DATA-TYPE", "Pair_T"),
    ("APPLICATION-RECORD-ELEMENT", "level"),
    ("APPLICATION-ARRAY-DATA-TYPE", "Levels_T"),
    ("DATA-TYPE-MAPPING-SET", "Maps"),
]

#: The literal's text, on its ``COMPU-SCALE``.
LITERAL_TEXT = "The mode is off,\nand stays off."
STUB_TEXT = "A stub's text, which has no place."


def _undescribed(node: Any) -> Any:
    """*node* with every ``description`` key removed, at any depth."""
    if isinstance(node, dict):
        return {key: _undescribed(value) for key, value in node.items() if key != "description"}
    if isinstance(node, list):
        return [_undescribed(item) for item in node]
    return node


def _describe_model(root: Path) -> Path:
    """The ``compositions`` fixture, widened to every kind that emits an element and described throughout."""
    shutil.copytree(COMPOSITIONS_MODEL_ROOT, root)
    types = root / "data_types" / "implementation"
    for path in root.rglob("*.yaml"):
        if path.parent.name != "requirements":
            _dump(path, _undescribed(_load(path)))

    def described(path: Path, update: Any) -> None:
        document = _load(path)
        update(document)
        _dump(path, document)

    def top(tag: str) -> Any:
        return lambda document: document.update(description=_text(tag, document["name"]))

    described(root / "swc" / "core" / "Gamma.yaml", top("APPLICATION-SW-COMPONENT-TYPE"))

    def gamma(document: dict[str, Any]) -> None:
        document["ports"][0]["description"] = _text("R-PORT-PROTOTYPE", "rInner")
        document["runnables"] = [
            {
                "name": "GammaMain",
                "events": [{"type": "timing", "period": 0.01}],
                "description": _text("RUNNABLE-ENTITY", "GammaMain"),
            }
        ]

    described(root / "swc" / "core" / "Gamma.yaml", gamma)

    def sub(document: dict[str, Any]) -> None:
        document["description"] = _text("COMPOSITION-SW-COMPONENT-TYPE", "Sub")
        (port,) = [port for port in document["ports"] if port["name"] == "pSignalOut"]
        port["description"] = _text("P-PORT-PROTOTYPE", "pSignalOut")
        for member in document["members"]:
            member.pop("description", None)
            if member["name"] == "Alpha":
                member["description"] = _text("SW-COMPONENT-PROTOTYPE", "Alpha")
        for delegation in document["delegations"]:
            delegation.pop("description", None)
            if delegation["name"] == "Sub_pSignalOut_Alpha":
                delegation["description"] = _text("DELEGATION-SW-CONNECTOR", "Sub_pSignalOut_Alpha")

    described(root / "compositions" / "Sub.yaml", sub)
    described(root / "connectors" / "sub_link.yaml", top("ASSEMBLY-SW-CONNECTOR"))

    def signal(document: dict[str, Any]) -> None:
        document["description"] = _text("SENDER-RECEIVER-INTERFACE", "ISignal")
        document["data_elements"][0]["description"] = _text("VARIABLE-DATA-PROTOTYPE", "value")

    described(root / "interfaces" / "ISignal.yaml", signal)
    _dump(
        root / "interfaces" / "ICalc.yaml",
        {
            "name": "ICalc",
            "kind": "client_server",
            "description": _text("CLIENT-SERVER-INTERFACE", "ICalc"),
            "operations": [
                {
                    "name": "Op",
                    "description": _text("CLIENT-SERVER-OPERATION", "Op"),
                    "arguments": [
                        {
                            "name": "x",
                            "direction": "in",
                            "implementation_type": "uint8",
                            "description": _text("ARGUMENT-DATA-PROTOTYPE", "x"),
                        }
                    ],
                }
            ],
        },
    )
    _dump(
        root / "interfaces" / "IExt.yaml",
        {
            "name": "IExt",
            "kind": "external_interface",
            "external_ref": "/Platform/PortInterfaces/IExt",
            "external_dest": "CLIENT-SERVER-INTERFACE",
            "description": STUB_TEXT,
        },
    )
    described(model_element_path(root, "uint8_base", kind="base_type"), top("SW-BASE-TYPE"))
    described(types / "uint8.yaml", top("IMPLEMENTATION-DATA-TYPE"))
    for document in (
        {"name": "U8Ref", "kind": "reference_type", "base": "uint8"},
        {
            "name": "Frame_T",
            "kind": "implementation_record",
            "elements": [
                {"name": "head", "type": "uint8", "description": _text("IMPLEMENTATION-DATA-TYPE-ELEMENT", "head")},
                {"name": "tail", "type": "uint8"},
            ],
        },
        {"name": "Buf_T", "kind": "implementation_array", "element_type": "uint8", "length": 4, "element_name": "Slot"},
        {"name": "Buf_P", "kind": "pointer", "target": "uint8"},
    ):
        document.setdefault("description", _text("IMPLEMENTATION-DATA-TYPE", document["name"]))
        _dump(types / f"{document['name']}.yaml", document)
    _dump(
        model_element_path(root, "Mode_T", kind="enum"),
        {
            "name": "Mode_T",
            "kind": "enum",
            "description": _text("APPLICATION-PRIMITIVE-DATA-TYPE", "Mode_T"),
            "literals": [{"name": "OFF", "value": 0, "description": LITERAL_TEXT}, {"name": "ON", "value": 1}],
        },
    )
    _dump(
        model_element_path(root, "Level_T", kind="application_type"),
        {
            "name": "Level_T",
            "kind": "application_type",
            "description": _text("APPLICATION-PRIMITIVE-DATA-TYPE", "Level_T"),
        },
    )
    _dump(
        model_element_path(root, "Pair_T", kind="record"),
        {
            "name": "Pair_T",
            "kind": "record",
            "description": _text("APPLICATION-RECORD-DATA-TYPE", "Pair_T"),
            "fields": [
                {"name": "level", "type": "Level_T", "description": _text("APPLICATION-RECORD-ELEMENT", "level")},
                {"name": "mode", "type": "Mode_T"},
            ],
        },
    )
    _dump(
        model_element_path(root, "Levels_T", kind="array"),
        {
            "name": "Levels_T",
            "kind": "array",
            "element_type": "Level_T",
            "length": 2,
            "description": _text("APPLICATION-ARRAY-DATA-TYPE", "Levels_T"),
        },
    )
    _dump(
        types / "Counter_T.yaml",
        {
            "name": "Counter_T",
            "kind": "external_type",
            "external_ref": "/Platform/ImplementationDataTypes/Counter_T",
            "external_dest": "IMPLEMENTATION-DATA-TYPE",
            "description": STUB_TEXT,
        },
    )
    write_model_element(
        root, {"name": "Maps", "description": _text("DATA-TYPE-MAPPING-SET", "Maps"), "maps": []}, layer="mapping_sets"
    )
    return root


@pytest.fixture(scope="module")
def described_model(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _describe_model(tmp_path_factory.mktemp("described") / "model")


@pytest.fixture(scope="module", params=VERSIONS)
def written(described_model: Path, tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest) -> str:
    out = tmp_path_factory.mktemp(f"written-{request.param}") / "model.arxml"
    generate_arxml(str(described_model), str(out), version=request.param)
    return out.read_text(encoding="utf-8")


def _named(root: etree._Element, tag: str, name: str) -> etree._Element:
    found = [element for element in root.iter(f"{NS}{tag}") if element.findtext(f"{NS}SHORT-NAME") == name]
    assert len(found) == 1, (tag, name, len(found))
    return found[0]


def _local(element: etree._Element) -> str:
    return etree.QName(element).localname


# The writer


class TestEveryKindWritesItsDesc:
    @pytest.mark.parametrize(("tag", "name"), DESCRIBED, ids=[f"{tag}-{name}" for tag, name in DESCRIBED])
    def test_at_its_identifiable_position_exactly(self, written: str, tag: str, name: str) -> None:
        element = _named(etree.fromstring(written.encode("utf-8")), tag, name)
        children = [_local(child) for child in element]
        documentation = [tag for tag in children[1:3] if tag in {"LONG-NAME", "DESC"}]
        assert children[0] == "SHORT-NAME"
        assert children[1 : 1 + len(documentation)] == documentation
        assert documentation[-1] == "DESC", children
        desc = element.find(f"{NS}DESC")
        assert [(_local(child), dict(child.attrib), child.text) for child in desc] == [
            ("L-2", {"L": "FOR-ALL"}, _text(tag, name))
        ]

    def test_each_text_is_written_once(self, written: str) -> None:
        for tag, name in DESCRIBED:
            assert written.count(f">{_text(tag, name)}<") == 1, (tag, name)

    def test_nothing_else_carries_a_desc(self, written: str) -> None:
        # The 26 described elements plus the literal's scale; every other element's text is empty.
        assert written.count("<DESC>") == len(DESCRIBED) + 1

    def test_the_literals_text_is_on_its_scale(self, written: str) -> None:
        root = etree.fromstring(written.encode("utf-8"))
        (scale,) = [
            scale for scale in root.iter(f"{NS}COMPU-SCALE") if scale.findtext(f"{NS}COMPU-CONST/{NS}VT") == "OFF"
        ]
        assert [_local(child) for child in scale] == [
            "SHORT-LABEL",
            "DESC",
            "LOWER-LIMIT",
            "UPPER-LIMIT",
            "COMPU-CONST",
        ]
        assert scale.findtext(f"{NS}DESC/{NS}L-2") == LITERAL_TEXT
        (bare,) = [
            scale for scale in root.iter(f"{NS}COMPU-SCALE") if scale.findtext(f"{NS}COMPU-CONST/{NS}VT") == "ON"
        ]
        assert bare.find(f"{NS}DESC") is None

    def test_the_enums_text_is_on_its_type_alone(self, written: str) -> None:
        root = etree.fromstring(written.encode("utf-8"))
        method = _named(root, "COMPU-METHOD", "Mode_T_CompuMethod")
        assert method.find(f"{NS}DESC") is None
        assert written.count(_text("APPLICATION-PRIMITIVE-DATA-TYPE", "Mode_T")) == 1

    def test_a_stub_writes_no_element_and_so_no_text(self, written: str) -> None:
        assert STUB_TEXT not in written
        assert "IExt" not in written
        assert "Counter_T" not in written

    def test_a_multi_line_text_is_one_paragraph_with_its_newlines(self, written: str) -> None:
        assert f'<L-2 L="FOR-ALL">{LITERAL_TEXT}</L-2>' in written

    def test_the_two_versions_write_the_same_documentation(self, described_model: Path, tmp_path: Path) -> None:
        texts = []
        for version in VERSIONS:
            out = tmp_path / f"{version}.arxml"
            generate_arxml(str(described_model), str(out), version=version)
            texts.append(out.read_text(encoding="utf-8").split("<AR-PACKAGES>", 1)[1])
        assert texts[0] == texts[1]


class TestTheHelperWritesTheTextAsHeld:
    @staticmethod
    def _identifiable(*children: str) -> etree._Element:
        element = make_element("RUNNABLE-ENTITY")
        sub_element(element, "SHORT-NAME", "R")
        for child in children:
            sub_element(element, child, "x")
        return element

    @pytest.mark.parametrize(
        "text",
        ["plain", "   ", " leading and trailing ", "line one\n  line two\n", "tab\there", "carriage\r\nreturn"],
        ids=["plain", "whitespace-only", "padded", "lines", "tab", "crlf"],
    )
    def test_the_text_round_trips_through_the_file(self, text: str, tmp_path: Path) -> None:
        element = self._identifiable("MINIMUM-START-INTERVAL")
        set_description(element, text)
        path = tmp_path / "one.xml"
        path.write_bytes(etree.tostring(element, encoding="UTF-8"))
        read = etree.parse(str(path)).getroot()
        assert read.findtext(f"{NS}DESC/{NS}L-2") == text

    def test_the_empty_default_writes_none(self) -> None:
        element = self._identifiable("CATEGORY")
        assert set_description(element, "") is None
        assert element.find(f"{NS}DESC") is None

    def test_the_position_is_found_not_assumed(self) -> None:
        element = self._identifiable("CATEGORY", "ADMIN-DATA")
        set_long_name(element, "Long")
        set_description(element, "Text")
        assert [_local(child) for child in element] == ["SHORT-NAME", "LONG-NAME", "DESC", "CATEGORY", "ADMIN-DATA"]
        assert element.find(f"{NS}LONG-NAME/{NS}L-4").attrib == {"L": "FOR-ALL"}

    def test_a_second_desc_is_refused(self) -> None:
        element = self._identifiable()
        set_description(element, "one")
        with pytest.raises(ValueError, match="already has a DESC"):
            set_description(element, "two")

    def test_a_character_xml_cannot_carry_is_refused_naming_the_element(self) -> None:
        element = self._identifiable()
        with pytest.raises(
            ValueError, match=r"DESC text of <RUNNABLE-ENTITY> 'R' holds U\+[0-9A-F]{4}, a character XML 1\.0"
        ):
            set_description(element, "bell\x07")
        assert element.find(f"{NS}DESC") is None


class TestTheCompositionsLongName:
    @staticmethod
    def _root(model_root: Path, tmp_path: Path, title: str) -> etree._Element:
        path = model_root / "compositions" / "Sub.yaml"
        document = _load(path)
        document["title"] = title
        _dump(path, document)
        out = tmp_path / "titled.arxml"
        generate_arxml(str(model_root), str(out))
        return _named(etree.parse(str(out)).getroot(), "COMPOSITION-SW-COMPONENT-TYPE", "Sub")

    @pytest.fixture
    def model_root(self, described_model: Path, tmp_path: Path) -> Path:
        root = tmp_path / "model"
        shutil.copytree(described_model, root)
        return root

    def test_a_title_other_than_the_name_is_written(self, model_root: Path, tmp_path: Path) -> None:
        composition = self._root(model_root, tmp_path, " The producing half ")
        assert [_local(child) for child in composition][:3] == ["SHORT-NAME", "LONG-NAME", "DESC"]
        assert composition.findtext(f"{NS}LONG-NAME/{NS}L-4") == " The producing half "

    def test_a_title_equal_to_the_name_writes_none(self, model_root: Path, tmp_path: Path) -> None:
        composition = self._root(model_root, tmp_path, "Sub")
        assert composition.find(f"{NS}LONG-NAME") is None

    def test_the_import_reads_the_title_and_every_text_back(self, described_model: Path, tmp_path: Path) -> None:
        out = tmp_path / "model.arxml"
        generate_arxml(str(described_model), str(out))
        project = init_project(tmp_path / "proj")
        run_import(str(out), str(project / "swcraft.yaml"), force=True)
        imported = project / "model"
        titles = {
            document["name"]: document["title"]
            for document in (_load(path) for path in (imported / "compositions").rglob("*.yaml"))
        }
        declared = {
            document["name"]: document["title"]
            for document in (_load(path) for path in (described_model / "compositions").rglob("*.yaml"))
        }
        assert titles == declared
        texts = set()
        for path in imported.rglob("*.yaml"):
            texts.update(_strings(_load(path), "description"))
        assert {_text(tag, name) for tag, name in DESCRIBED} | {LITERAL_TEXT} <= texts


def _strings(node: Any, key: str) -> set[str]:
    """Every string under *key* anywhere in a loaded YAML document."""
    found: set[str] = set()
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key and isinstance(value, str):
                found.add(value)
            else:
                found |= _strings(value, key)
    elif isinstance(node, list):
        for item in node:
            found |= _strings(item, key)
    return found


class TestTheGoldensStripToTheirPreChangeDigests:
    @pytest.mark.parametrize("name", sorted(PRE_CHANGE))
    def test_only_the_documentation_moved(self, name: str) -> None:
        stripped = strip_fidelity((GOLDEN_DIR / f"{name}.arxml").read_bytes())
        assert hashlib.sha256(stripped).hexdigest() == PRE_CHANGE[name]

    def test_the_empty_model_writes_no_documentation(self, tmp_path: Path) -> None:
        out = tmp_path / "empty.arxml"
        generate_arxml(str(make_empty_model_root(tmp_path)), str(out))
        assert "<DESC>" not in out.read_text(encoding="utf-8")


# import component


def _exchange_target(tmp_path: Path, *, component: str = "", port: str = "", interval: float = 0.005) -> Path:
    """A project holding the ``component_exchange`` model, ``SensorSwc`` and its ``pSpeed`` described as given."""
    project = init_project(tmp_path / "target")
    shutil.rmtree(project / "model")
    shutil.copytree(EXCHANGE_MODEL_ROOT, project / "model")
    _set_sensor(project / "model", component=component, port=port, interval=interval)
    return project / "model"


def _set_sensor(model_root: Path, *, component: str, port: str, interval: float = 0.005) -> None:
    path = model_root / "swc" / "sensing" / "SensorSwc.yaml"
    document = _load(path)
    document.pop("description", None)
    if component:
        document["description"] = component
    for entry in document["ports"]:
        entry.pop("description", None)
        if entry["name"] == "pSpeed" and port:
            entry["description"] = port
    document["runnables"][0]["min_start_interval"] = interval
    _dump(path, document)


def _export(tmp_path: Path, *, component: str = "", port: str = "") -> Path:
    """``SensorSwc``'s component export from a copy described as given."""
    model_root = tmp_path / "source" / "model"
    shutil.copytree(EXCHANGE_MODEL_ROOT, model_root)
    _set_sensor(model_root, component=component, port=port)
    out = tmp_path / "source" / "SensorSwc.arxml"
    export_component(str(model_root), "SensorSwc", str(out))
    return out


def _sensor(model_root: Path) -> dict[str, Any]:
    return _load(model_root / "swc" / "sensing" / "SensorSwc.yaml")


class TestTheComponentImportComparesTheTexts:
    def test_an_export_of_a_described_component_is_its_own_no_op(self, tmp_path: Path) -> None:
        model_root = _exchange_target(tmp_path, component="The sensor.", port="Its speed.")
        out = tmp_path / "own.arxml"
        export_component(str(model_root), "SensorSwc", str(out))
        assert '<L-2 L="FOR-ALL">The sensor.</L-2>' in out.read_text(encoding="utf-8")

        report = import_component(str(out), str(model_root))

        assert report.swc_action == "unchanged"

    def test_an_absent_text_is_no_difference(self, tmp_path: Path) -> None:
        model_root = _exchange_target(tmp_path, component="The sensor.", port="Its speed.")
        before = files_of(model_root)

        report = import_component(str(_export(tmp_path)), str(model_root))

        assert report.swc_action == "unchanged"
        assert files_of(model_root) == before

    def test_an_update_keeps_the_targets_texts_and_says_so(self, tmp_path: Path) -> None:
        # The interval differs, so the file is a real update; it states no text.
        model_root = _exchange_target(tmp_path, component="The sensor.", port="Its speed.", interval=0.006)

        report = import_component(str(_export(tmp_path)), str(model_root), update=True)

        assert report.swc_action == "updated"
        sensor = _sensor(model_root)
        assert sensor["description"] == "The sensor."
        assert [entry.get("description") for entry in sensor["ports"] if entry["name"] == "pSpeed"] == ["Its speed."]
        assert report.preserved_descriptions == ["component 'SensorSwc'", "port 'pSpeed'"]
        rendered = report.render()
        assert "DESC of component 'SensorSwc' preserved from target: source element carries none" in rendered
        assert "DESC of port 'pSpeed' preserved from target: source element carries none" in rendered
        assert "description" not in report.swc_diff_preview

    @pytest.mark.parametrize(
        ("component", "port", "named"),
        [("New words.", "", "component 'SensorSwc'"), ("", "New speed words.", "port 'pSpeed'")],
        ids=["component", "port"],
    )
    def test_a_differing_text_is_a_conflict_naming_the_part(
        self, tmp_path: Path, component: str, port: str, named: str
    ) -> None:
        model_root = _exchange_target(tmp_path, component="Old words.", port="Old speed words.")
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError) as caught:
            import_component(str(_export(tmp_path, component=component, port=port)), str(model_root))

        assert f"the description of {named} differs from the target's" in str(caught.value)
        assert files_of(model_root) == before

    def test_with_update_the_files_text_wins(self, tmp_path: Path) -> None:
        model_root = _exchange_target(tmp_path, component="Old words.", port="Old speed words.")

        report = import_component(
            str(_export(tmp_path, component="New words.", port="New speed words.")), str(model_root), update=True
        )

        assert report.swc_action == "updated"
        sensor = _sensor(model_root)
        assert sensor["description"] == "New words."
        assert [entry["description"] for entry in sensor["ports"] if entry["name"] == "pSpeed"] == ["New speed words."]
        assert report.preserved_descriptions == []
