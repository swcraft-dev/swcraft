"""The behavioural values on the import side, and the composition's ``LONG-NAME``.

* **carried** -- ``IS-SERVICE``, a data element's ``SW-IMPL-POLICY`` and
  ``SW-CALIBRATION-ACCESS``, every com-spec attribute of a ``port_com_spec``, a queued receiver's
  ``QUEUE-LENGTH`` and a timing event's disabled mode on a port typed by a mode-switch stub;
* **by equivalence** -- a value equal to the writer's silent one
  (``writer.arxml_generation.COM_SPEC_SILENT_VALUES``) is recorded nowhere, and a ``FILTER`` whose
  only child is ``DATA-FILTER-TYPE`` ``ALWAYS`` is no filter;
* **exactly** -- a ``DESC`` and a composition's ``LONG-NAME`` keep their whitespace;
* **chained** -- a disabled mode whose context port was skipped is one line at the event;
* **compared** -- ``import component`` and the merge treat a differing carried value as a difference.

The sources are SWCraft's own ARXML of the ``component_exchange`` fixture, patched with ``lxml``.
Does not reach the report lines of values the model does not carry (``test_uncarried_losses.py``),
the real tool exports (the closed suites), or the writer's half (``test_behavioural_emission.py``).
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
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.merge import CONFLICTING, MERGED, com_spec_values
from swcraft_tools.arxml_import.merge_verdict import decide_merges
from swcraft_tools.arxml_reader import (
    CSArgumentInfo,
    CSInterfaceInfo,
    CSOperationInfo,
    DataElementComSpecInfo,
    DisabledModeInfo,
    ImportModel,
    SRDataElementInfo,
    SRInterfaceInfo,
    SwcEventInfo,
    SwcInfo,
    SwcPortInfo,
    SwcRunnableInfo,
    _extract_description,
    _extract_long_name,
    read_arxml,
)
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.component_exchange import (
    ComponentImportConflictError,
    LossyComponentImportError,
    export_component,
    import_component,
)
from swcraft_tools.writer.arxml_generation import COM_SPEC_SILENT_VALUES, generate_arxml

NS = f"{{{NAMESPACE}}}"
EXCHANGE_MODEL_ROOT = Path(__file__).resolve().parent / "fixtures" / "component_exchange" / "model"

#: The stub the disabled-mode cases type their mode port by: a path the file does not declare.
MODE_INTERFACE = "/Ext/ModeInterfaces/EcuModeIf"
MODE_GROUP = f"{MODE_INTERFACE}/ecuMode"
MODE = "/Ext/ModeGroups/EcuMode/STARTUP"


# Sources


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _file_of(model_root: Path, layer: str, name: str) -> dict[str, Any]:
    (path,) = [path for path in (model_root / layer).rglob("*.yaml") if _load(path).get("name") == name]
    return _load(path)


def _named(tree: etree._ElementTree, tag: str, name: str) -> etree._Element:
    found = [el for el in tree.iter(f"{NS}{tag}") if el.findtext(f"{NS}SHORT-NAME") == name]
    assert len(found) == 1, (tag, name, len(found))
    return found[0]


def _save(tree: etree._ElementTree, path: Path) -> Path:
    path.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return path


def _sub(parent: etree._Element, tag: str, text: str | None = None, **attributes: str) -> etree._Element:
    element = etree.SubElement(parent, f"{NS}{tag}", **attributes)
    if text is not None:
        element.text = text
    return element


def _port_com_spec(tree: etree._ElementTree, port: str) -> etree._Element:
    """The one sender/receiver com-spec of the port named *port*."""
    (spec,) = [
        child
        for block in _named(tree, "P-PORT-PROTOTYPE" if port.startswith("p") else "R-PORT-PROTOTYPE", port)
        for child in block.iter()
        if isinstance(child.tag, str) and child.tag.endswith("-COM-SPEC") and "CLIENT" not in child.tag
    ]
    return spec


def _set_children(spec: etree._Element, children: dict[str, str]) -> None:
    """Replace each named leaf child of *spec* with *text*, adding it after ``DATA-ELEMENT-REF`` when absent."""
    for tag, text in children.items():
        existing = spec.find(f"{NS}{tag}")
        if existing is None:
            existing = etree.Element(f"{NS}{tag}")
            spec.find(f"{NS}DATA-ELEMENT-REF").addnext(existing)
        existing.text = text


def _retag(spec: etree._Element, tag: str, keep: tuple[str, ...]) -> None:
    """Make *spec* a *tag* com-spec holding only the children named in *keep* (plus its ``DATA-ELEMENT-REF``)."""
    spec.tag = f"{NS}{tag}"
    for child in list(spec):
        if etree.QName(child).localname not in ("DATA-ELEMENT-REF", *keep):
            spec.remove(child)


def _element_properties(tree: etree._ElementTree, element: str, **values: str) -> None:
    """Give the ``VARIABLE-DATA-PROTOTYPE`` *element* a conditional stating *values* (tag spelled with ``_``)."""
    prototype = _named(tree, "VARIABLE-DATA-PROTOTYPE", element)
    props = etree.Element(f"{NS}SW-DATA-DEF-PROPS")
    conditional = _sub(_sub(props, "SW-DATA-DEF-PROPS-VARIANTS"), "SW-DATA-DEF-PROPS-CONDITIONAL")
    for tag in ("SW_CALIBRATION_ACCESS", "SW_IMPL_POLICY"):
        if tag in values:
            _sub(conditional, tag.replace("_", "-"), values[tag])
    prototype.find(f"{NS}SHORT-NAME").addnext(props)


def _is_service(tree: etree._ElementTree, tag: str, name: str, text: str) -> None:
    _named(tree, tag, name).find(f"{NS}IS-SERVICE").text = text


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The ``component_exchange`` model's whole-model ARXML, written by SWCraft."""
    base = tmp_path_factory.mktemp("behavioural")
    model_root = base / "model"
    shutil.copytree(EXCHANGE_MODEL_ROOT, model_root)
    out = base / "model.arxml"
    generate_arxml(str(model_root), str(out))
    return out


def _imported(source: Path, tmp_path: Path, patch: Any) -> tuple[Path, Any]:
    tree = etree.parse(str(source))
    patch(tree)
    patched = _save(tree, tmp_path / "patched.arxml")
    project = init_project(tmp_path / "proj")
    report = run_import(str(patched), str(project / "swcraft.yaml"), force=True)
    return project / "model", report


def _lines(report: Any) -> list[Any]:
    """The report's skip entries but the two ``SWC-IMPLEMENTATION``s every ``import model`` of a SWCraft file has."""
    return [entry for entry in report.skip_entries if entry.tag != "SWC-IMPLEMENTATION"]


def _port(model_root: Path, swc: str, port: str) -> dict[str, Any]:
    (found,) = [p for p in _file_of(model_root, "swc", swc)["ports"] if p["name"] == port]
    return found


# Carried: the interfaces and the data elements


class TestIsServiceIsCarried:
    @pytest.mark.parametrize(
        ("tag", "name"),
        [("SENDER-RECEIVER-INTERFACE", "IVehicleSpeed"), ("CLIENT-SERVER-INTERFACE", "ICalibration")],
        ids=["sender-receiver", "client-server"],
    )
    @pytest.mark.parametrize("text", ["true", "1", " \t\r\ntrue\n\t "], ids=["true", "1", "xml-whitespace-padded"])
    def test_a_true_value_is_carried(self, source: Path, tmp_path: Path, tag: str, name: str, text: str) -> None:
        model_root, _report = _imported(source, tmp_path, lambda tree: _is_service(tree, tag, name, text))
        assert _file_of(model_root, "interfaces", name)["is_service"] is True

    @pytest.mark.parametrize("text", ["false", "0"])
    def test_a_false_value_writes_nothing(self, source: Path, tmp_path: Path, text: str) -> None:
        model_root, _report = _imported(
            source, tmp_path, lambda tree: _is_service(tree, "SENDER-RECEIVER-INTERFACE", "IVehicleSpeed", text)
        )
        assert "is_service" not in _file_of(model_root, "interfaces", "IVehicleSpeed")


class TestTheDataElementPropertiesAreCarried:
    @pytest.mark.parametrize(("literal", "value"), [("STANDARD", "standard"), ("QUEUED", "queued")])
    def test_each_policy(self, source: Path, tmp_path: Path, literal: str, value: str) -> None:
        model_root, _report = _imported(
            source, tmp_path, lambda tree: _element_properties(tree, "status", SW_IMPL_POLICY=literal)
        )
        (element,) = _file_of(model_root, "interfaces", "IActuatorStatus")["data_elements"]
        assert element["sw_impl_policy"] == value

    @pytest.mark.parametrize(
        ("literal", "value"),
        [("NOT-ACCESSIBLE", "not_accessible"), ("READ-ONLY", "read_only"), ("READ-WRITE", "read_write")],
    )
    def test_each_calibration_access(self, source: Path, tmp_path: Path, literal: str, value: str) -> None:
        model_root, _report = _imported(
            source, tmp_path, lambda tree: _element_properties(tree, "speed", SW_CALIBRATION_ACCESS=literal)
        )
        (element,) = _file_of(model_root, "interfaces", "IVehicleSpeed")["data_elements"]
        assert element["sw_calibration_access"] == value

    def test_a_policy_the_model_does_not_carry_is_absent(self, source: Path, tmp_path: Path) -> None:
        model_root, _report = _imported(
            source, tmp_path, lambda tree: _element_properties(tree, "speed", SW_IMPL_POLICY="MEASUREMENT-POINT")
        )
        (element,) = _file_of(model_root, "interfaces", "IVehicleSpeed")["data_elements"]
        assert "sw_impl_policy" not in element

    def test_a_calibration_access_a_queued_element_does_not_support_is_not_carried(
        self, source: Path, tmp_path: Path
    ) -> None:
        """``[constr_1017]``: the policy is carried and the access is not, so the file still loads."""
        model_root, _report = _imported(
            source,
            tmp_path,
            lambda tree: _element_properties(
                tree, "status", SW_IMPL_POLICY="QUEUED", SW_CALIBRATION_ACCESS="READ-ONLY"
            ),
        )
        (element,) = _file_of(model_root, "interfaces", "IActuatorStatus")["data_elements"]
        assert element["sw_impl_policy"] == "queued"
        assert "sw_calibration_access" not in element


# Carried: the com-spec attributes, each side and each policy

#: ``(tag, the non-silent text, the field, the value)`` of every attribute a non-queued receiver carries.
RECEIVER_ATTRIBUTES = [
    ("ENABLE-UPDATE", "true", "enable_update", True),
    ("ALIVE-TIMEOUT", "0.25", "alive_timeout", 0.25),
    ("HANDLE-NEVER-RECEIVED", "1", "handle_never_received", True),
    ("HANDLE-TIMEOUT-TYPE", "REPLACE", "handle_timeout_type", "replace"),
    ("HANDLE-OUT-OF-RANGE", "SATURATE", "handle_out_of_range", "saturate"),
    ("USES-END-TO-END-PROTECTION", "true", "uses_end_to_end_protection", True),
]
#: ... and a sender (both policies), which has a place for these two only.
SENDER_ATTRIBUTES = [
    ("HANDLE-OUT-OF-RANGE", "DEFAULT", "handle_out_of_range", "default"),
    ("HANDLE-OUT-OF-RANGE", "IGNORE", "handle_out_of_range", "ignore"),
    ("USES-END-TO-END-PROTECTION", "true", "uses_end_to_end_protection", True),
]


class TestTheComSpecAttributesAreCarried:
    @pytest.mark.parametrize(
        ("tag", "text", "field", "value"), RECEIVER_ATTRIBUTES, ids=[r[0] for r in RECEIVER_ATTRIBUTES]
    )
    def test_a_non_queued_receiver(
        self, source: Path, tmp_path: Path, tag: str, text: str, field: str, value: object
    ) -> None:
        model_root, report = _imported(
            source, tmp_path, lambda tree: _set_children(_port_com_spec(tree, "rSpeed"), {tag: text})
        )
        assert _port(model_root, "ActuatorSwc", "rSpeed")["com_specs"] == [{"data_element": "speed", field: value}]
        assert _lines(report) == []

    @pytest.mark.parametrize(("tag", "text", "field", "value"), SENDER_ATTRIBUTES, ids=["default", "ignore", "e2e"])
    def test_a_non_queued_sender(
        self, source: Path, tmp_path: Path, tag: str, text: str, field: str, value: object
    ) -> None:
        model_root, _report = _imported(
            source, tmp_path, lambda tree: _set_children(_port_com_spec(tree, "pSpeed"), {tag: text})
        )
        assert _port(model_root, "SensorSwc", "pSpeed")["com_specs"] == [{"data_element": "speed", field: value}]

    def test_a_receiver_attribute_on_a_sender_is_not_carried(self, source: Path, tmp_path: Path) -> None:
        model_root, _report = _imported(
            source, tmp_path, lambda tree: _set_children(_port_com_spec(tree, "pSpeed"), {"ENABLE-UPDATE": "true"})
        )
        assert "com_specs" not in _port(model_root, "SensorSwc", "pSpeed")

    def test_a_queued_receiver_and_sender(self, source: Path, tmp_path: Path) -> None:
        """A queued element's com-specs: a receiver's ``QUEUE-LENGTH`` is carried, and the two shared attributes."""

        def queued(tree: etree._ElementTree) -> None:
            _element_properties(tree, "speed", SW_IMPL_POLICY="QUEUED")
            receiver = _port_com_spec(tree, "rSpeed")
            _retag(receiver, "QUEUED-RECEIVER-COM-SPEC", ())
            _set_children(receiver, {"QUEUE-LENGTH": "4"})
            receiver.find(f"{NS}QUEUE-LENGTH").addprevious(etree.Element(f"{NS}HANDLE-OUT-OF-RANGE"))
            receiver.find(f"{NS}HANDLE-OUT-OF-RANGE").text = "IGNORE"
            sender = _port_com_spec(tree, "pSpeed")
            _retag(sender, "QUEUED-SENDER-COM-SPEC", ())
            _set_children(sender, {"USES-END-TO-END-PROTECTION": "true"})

        model_root, _report = _imported(source, tmp_path, queued)
        assert _port(model_root, "ActuatorSwc", "rSpeed")["com_specs"] == [
            {"data_element": "speed", "handle_out_of_range": "ignore", "queue_length": 4}
        ]
        assert _port(model_root, "SensorSwc", "pSpeed")["com_specs"] == [
            {"data_element": "speed", "uses_end_to_end_protection": True}
        ]

    def test_a_non_queued_attribute_on_a_queued_receiver_is_not_carried(self, source: Path, tmp_path: Path) -> None:
        def queued(tree: etree._ElementTree) -> None:
            _element_properties(tree, "speed", SW_IMPL_POLICY="QUEUED")
            receiver = _port_com_spec(tree, "rSpeed")
            _retag(receiver, "QUEUED-RECEIVER-COM-SPEC", ())
            _set_children(receiver, {"QUEUE-LENGTH": "2", "ENABLE-UPDATE": "true"})

        model_root, _report = _imported(source, tmp_path, queued)
        assert _port(model_root, "ActuatorSwc", "rSpeed")["com_specs"] == [{"data_element": "speed", "queue_length": 2}]

    def test_a_non_queued_com_spec_of_a_queued_element_is_not_carried(self, source: Path, tmp_path: Path) -> None:
        """``[TPS_SWCT_01219]``: the kind must follow the element's policy; a mismatched one carries nothing."""

        def mismatched(tree: etree._ElementTree) -> None:
            _element_properties(tree, "speed", SW_IMPL_POLICY="QUEUED")
            _set_children(_port_com_spec(tree, "rSpeed"), {"ENABLE-UPDATE": "true"})

        model_root, _report = _imported(source, tmp_path, mismatched)
        assert "com_specs" not in _port(model_root, "ActuatorSwc", "rSpeed")


class TestACarriedValueAndItsReportLineAgree:
    """What ``Port.com_specs`` carries and what the report calls *not carried* are one judgement.

    A value is carried exactly when no whole-com-spec line names its com-spec; each case is a
    malformed source the schema or the standard refuses and a reader still meets.
    """

    def _lines_of(self, report: Any, port: str) -> list[tuple[str, str]]:
        return sorted((e.path.rsplit("/", 2)[-1], e.tag) for e in _lines(report) if f"/{port}/" in e.path)

    @pytest.mark.parametrize("kind", ["NONQUEUED-RECEIVER-COM-SPEC", "QUEUED-RECEIVER-COM-SPEC"])
    def test_a_com_spec_in_the_other_block_does_not_shadow_the_ports_own(
        self, source: Path, tmp_path: Path, kind: str
    ) -> None:
        def patch(tree: etree._ElementTree) -> None:
            own = _port_com_spec(tree, "rSpeed")
            _set_children(own, {"ALIVE-TIMEOUT": "0.5"})
            stray = etree.fromstring(etree.tostring(own))
            stray.tag = f"{NS}{kind}"
            _set_children(stray, {"ALIVE-TIMEOUT": "9"})
            block = etree.Element(f"{NS}PROVIDED-COM-SPECS")
            block.append(stray)
            own.getparent().addprevious(block)

        model_root, report = _imported(source, tmp_path, patch)
        assert _port(model_root, "ActuatorSwc", "rSpeed")["com_specs"] == [
            {"data_element": "speed", "alive_timeout": 0.5}
        ]
        assert self._lines_of(report, "rSpeed") == [("PROVIDED-COM-SPECS", kind)]

    def test_a_second_com_spec_after_one_of_the_wrong_kind_carries_nothing_and_both_are_lines(
        self, source: Path, tmp_path: Path
    ) -> None:
        """First-for-its-element is asked before the kind, by the mapper and the report alike."""

        def patch(tree: etree._ElementTree) -> None:
            own = _port_com_spec(tree, "rSpeed")
            _set_children(own, {"ALIVE-TIMEOUT": "0.5"})
            wrong = etree.fromstring(etree.tostring(own))
            _retag(wrong, "QUEUED-RECEIVER-COM-SPEC", ())
            own.addprevious(wrong)

        model_root, report = _imported(source, tmp_path, patch)
        assert "com_specs" not in _port(model_root, "ActuatorSwc", "rSpeed")
        assert self._lines_of(report, "rSpeed") == [
            ("REQUIRED-COM-SPECS", "NONQUEUED-RECEIVER-COM-SPEC"),
            ("REQUIRED-COM-SPECS", "QUEUED-RECEIVER-COM-SPEC"),
        ]

    def test_an_element_of_another_namespace_is_no_com_spec_and_no_value(self, source: Path, tmp_path: Path) -> None:
        """A foreign com-spec shifts no pairing, and a foreign leaf overrides no value."""
        foreign = "{urn:not-autosar}"

        def patch(tree: etree._ElementTree) -> None:
            own = _port_com_spec(tree, "rSpeed")
            _set_children(own, {"ALIVE-TIMEOUT": "0.5"})
            stray = etree.Element(f"{foreign}NONQUEUED-RECEIVER-COM-SPEC")
            etree.SubElement(stray, f"{foreign}ENABLE-UPDATE").text = "true"
            own.addprevious(stray)
            etree.SubElement(own, f"{foreign}ALIVE-TIMEOUT").text = "9"

        model_root, report = _imported(source, tmp_path, patch)
        assert _port(model_root, "ActuatorSwc", "rSpeed")["com_specs"] == [
            {"data_element": "speed", "alive_timeout": 0.5}
        ]
        assert not [e for e in _lines(report) if "second com-spec" in e.reason or "no data element" in e.reason]

    def test_the_reader_hands_each_shape_its_own_values(self, source: Path) -> None:
        model = read_arxml(str(source))
        ports = [port for swc in model.swcs for port in swc.ports]
        assert any(port.data_element_com_specs for port in ports)
        for port in ports:
            assert port.data_element_com_specs == [s.data for s in port.com_spec_shapes if s.data is not None]
            assert port.server_com_specs == [s.server for s in port.com_spec_shapes if s.server is not None]
            for shape in port.com_spec_shapes:
                if shape.data is not None:
                    assert (shape.data.com_spec_tag, shape.data.block_tag) == (shape.tag, shape.block_tag)

    def test_a_queued_receiver_com_spec_on_a_compositions_port_is_one_whole_line(
        self, source: Path, tmp_path: Path
    ) -> None:
        """The writer writes no com-spec for a queued element on a composition's require port."""

        def patch(tree: etree._ElementTree) -> None:
            _element_properties(tree, "speed", SW_IMPL_POLICY="QUEUED")
            for port, kind in (("rSpeed", "QUEUED-RECEIVER-COM-SPEC"), ("pSpeed", "QUEUED-SENDER-COM-SPEC")):
                _retag(_port_com_spec(tree, port), kind, ())
            _set_children(_port_com_spec(tree, "rSpeed"), {"QUEUE-LENGTH": "4"})
            (composition,) = tree.iter(f"{NS}COMPOSITION-SW-COMPONENT-TYPE")
            interface = _named(tree, "R-PORT-PROTOTYPE", "rSpeed").find(f"{NS}REQUIRED-INTERFACE-TREF")
            port = _sub(composition.find(f"{NS}PORTS"), "R-PORT-PROTOTYPE")
            _sub(port, "SHORT-NAME", "rOuter")
            spec = _sub(_sub(port, "REQUIRED-COM-SPECS"), "QUEUED-RECEIVER-COM-SPEC")
            _sub(spec, "DATA-ELEMENT-REF", f"{interface.text}/speed", DEST="VARIABLE-DATA-PROTOTYPE")
            _sub(spec, "QUEUE-LENGTH", "4")
            _sub(port, "REQUIRED-INTERFACE-TREF", interface.text, DEST=interface.get("DEST"))

        _model_root, report = _imported(source, tmp_path, patch)
        assert self._lines_of(report, "rOuter") == [("REQUIRED-COM-SPECS", "QUEUED-RECEIVER-COM-SPEC")]


class TestASilentValueRecordsNothing:
    def test_a_swcraft_written_file_carries_no_com_specs_and_no_line(self, source: Path, tmp_path: Path) -> None:
        model_root, report = _imported(source, tmp_path, lambda tree: None)
        for swc in ("SensorSwc", "ActuatorSwc"):
            assert all("com_specs" not in port for port in _file_of(model_root, "swc", swc)["ports"])
        assert _lines(report) == []

    @pytest.mark.parametrize(
        ("tag", "attribute"), list(COM_SPEC_SILENT_VALUES.items()), ids=list(COM_SPEC_SILENT_VALUES)
    )
    def test_every_silent_value_stated_explicitly_is_carried_by_equivalence(
        self, source: Path, tmp_path: Path, tag: str, attribute: Any
    ) -> None:
        silent = attribute.silent
        text = attribute.literals[silent] if attribute.literals is not None else ("false" if silent is False else "0")
        model_root, report = _imported(
            source, tmp_path, lambda tree: _set_children(_port_com_spec(tree, "rSpeed"), {tag: text})
        )
        assert "com_specs" not in _port(model_root, "ActuatorSwc", "rSpeed")
        assert _lines(report) == []


class TestFilterAlwaysIsNoFilter:
    """A ``FILTER`` whose only child is ``DATA-FILTER-TYPE`` ``ALWAYS`` means no filtering."""

    def _spec(self, filter_children: dict[str, str]) -> etree._Element:
        spec = etree.Element(f"{NS}NONQUEUED-RECEIVER-COM-SPEC")
        filter_el = _sub(spec, "FILTER")
        for tag, text in filter_children.items():
            _sub(filter_el, tag, text)
        return spec

    def test_a_bare_always_filter_is_silent(self, source: Path, tmp_path: Path) -> None:
        def patch(tree: etree._ElementTree) -> None:
            spec = _port_com_spec(tree, "rSpeed")
            assert spec.find(f"{NS}FILTER") is None  # the writer writes none, so the patch is the filter
            filter_el = etree.Element(f"{NS}FILTER")
            _sub(filter_el, "DATA-FILTER-TYPE", "ALWAYS")
            spec.find(f"{NS}HANDLE-NEVER-RECEIVED").addprevious(filter_el)

        model_root, report = _imported(source, tmp_path, patch)

        patched = tmp_path / "patched.arxml"
        assert patched.read_text(encoding="utf-8").count("ALWAYS</") == 1
        specs = [
            spec for swc in read_arxml(str(patched)).swcs for port in swc.ports for spec in port.data_element_com_specs
        ]
        assert specs and all(spec.filter_is_silent for spec in specs)
        assert "com_specs" not in _port(model_root, "ActuatorSwc", "rSpeed")
        assert _lines(report) == []

    def test_no_filter_is_silent(self) -> None:
        from swcraft_tools.arxml_reader import _filter_is_silent

        assert _filter_is_silent(etree.Element(f"{NS}NONQUEUED-RECEIVER-COM-SPEC")) is True
        assert _filter_is_silent(self._spec({"DATA-FILTER-TYPE": "ALWAYS"})) is True

    @pytest.mark.parametrize(
        "children",
        [{"DATA-FILTER-TYPE": "NEVER"}, {"DATA-FILTER-TYPE": "ALWAYS", "MASK": "3"}, {}],
        ids=["never", "always-with-a-mask", "empty"],
    )
    def test_any_other_filter_is_not(self, children: dict[str, str]) -> None:
        from swcraft_tools.arxml_reader import _filter_is_silent

        assert _filter_is_silent(self._spec(children)) is False


# The pre-0.14.0 component file


def _pre_0_14_0(export: Path, target: Path) -> Path:
    """*export* in the shape SWCraft wrote before 0.14.0, whatever the writer writes today.

    Every non-queued receiver carries ``FILTER`` ``ALWAYS`` before ``HANDLE-NEVER-RECEIVED``, every
    argument ``SERVER-ARGUMENT-IMPL-POLICY`` ``USE-ARGUMENT-TYPE`` after its ``DIRECTION``, and no
    com-spec states ``HANDLE-OUT-OF-RANGE`` or ``HANDLE-TIMEOUT-TYPE``.
    """
    tree = etree.parse(str(export))
    for spec in tree.iter(f"{NS}NONQUEUED-RECEIVER-COM-SPEC", f"{NS}NONQUEUED-SENDER-COM-SPEC"):
        for tag in ("HANDLE-OUT-OF-RANGE", "HANDLE-TIMEOUT-TYPE"):
            for child in spec.findall(f"{NS}{tag}"):
                spec.remove(child)
    for spec in tree.iter(f"{NS}NONQUEUED-RECEIVER-COM-SPEC"):
        if spec.find(f"{NS}FILTER") is None:
            filter_el = etree.Element(f"{NS}FILTER")
            _sub(filter_el, "DATA-FILTER-TYPE", "ALWAYS")
            spec.find(f"{NS}HANDLE-NEVER-RECEIVED").addprevious(filter_el)
    for argument in tree.iter(f"{NS}ARGUMENT-DATA-PROTOTYPE"):
        if argument.find(f"{NS}SERVER-ARGUMENT-IMPL-POLICY") is None:
            argument.find(f"{NS}DIRECTION").addnext(etree.Element(f"{NS}SERVER-ARGUMENT-IMPL-POLICY"))
        argument.find(f"{NS}SERVER-ARGUMENT-IMPL-POLICY").text = "USE-ARGUMENT-TYPE"
    return _save(tree, target)


@pytest.fixture(scope="module")
def exchange_exports(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("exports")
    exports: dict[str, Path] = {}
    for swc in ("ActuatorSwc", "SensorSwc"):
        out = base / f"{swc}.arxml"
        export_component(str(EXCHANGE_MODEL_ROOT), swc, str(out))
        exports[swc] = _pre_0_14_0(out, base / f"{swc}-pre-0.14.0.arxml")
    return exports


class TestAPre0140ComponentFile:
    def test_without_an_array_argument_it_imports_with_no_line(
        self, exchange_exports: dict[str, Path], tmp_path: Path
    ) -> None:
        text = exchange_exports["ActuatorSwc"].read_text(encoding="utf-8")
        assert "<DATA-FILTER-TYPE>ALWAYS</DATA-FILTER-TYPE>" in text
        project = init_project(tmp_path / "proj")

        report = import_component(str(exchange_exports["ActuatorSwc"]), str(project / "model"))

        assert report.swc_action == "created"
        assert "com_specs" not in _port(project / "model", "ActuatorSwc", "rSpeed")

    def test_with_an_array_argument_it_aborts_under_f6(self, exchange_exports: dict[str, Path], tmp_path: Path) -> None:
        project = init_project(tmp_path / "proj")
        before = files_of(project)

        with pytest.raises(LossyComponentImportError) as caught:
            import_component(str(exchange_exports["SensorSwc"]), str(project / "model"))

        assert "SERVER-ARGUMENT-IMPL-POLICY" in str(caught.value)
        assert files_of(project) == before


# Disabled modes


def _mode_port(tree: etree._ElementTree, interface_ref: str, dest: str) -> str:
    """Add ``rMode`` to ``ActuatorSwc``, typed by *interface_ref*; returns the port's path."""
    swc = _named(tree, "APPLICATION-SW-COMPONENT-TYPE", "ActuatorSwc")
    port = _sub(swc.find(f"{NS}PORTS"), "R-PORT-PROTOTYPE")
    _sub(port, "SHORT-NAME", "rMode")
    _sub(port, "REQUIRED-INTERFACE-TREF", interface_ref, DEST=dest)
    package_path = []
    element = swc
    while element is not None:
        name = element.findtext(f"{NS}SHORT-NAME")
        if name is not None:
            package_path.append(name)
        element = element.getparent()
    return "/" + "/".join(reversed(package_path)) + "/rMode"


def _disable(tree: etree._ElementTree, port_path: str) -> None:
    """Give ``ActuatorSwc``'s timing event one disabled mode through *port_path*."""
    (event,) = [
        el for el in tree.iter(f"{NS}TIMING-EVENT") if "ActuatorMain" in (el.findtext(f"{NS}START-ON-EVENT-REF") or "")
    ]
    irefs = etree.Element(f"{NS}DISABLED-MODE-IREFS")
    iref = _sub(irefs, "DISABLED-MODE-IREF")
    _sub(iref, "CONTEXT-PORT-REF", port_path, DEST="R-PORT-PROTOTYPE")
    _sub(iref, "CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF", MODE_GROUP, DEST="MODE-DECLARATION-GROUP-PROTOTYPE")
    _sub(iref, "TARGET-MODE-DECLARATION-REF", MODE, DEST="MODE-DECLARATION")
    event.find(f"{NS}START-ON-EVENT-REF").addprevious(irefs)


def _timing_events(model_root: Path) -> list[dict[str, Any]]:
    (runnable,) = _file_of(model_root, "swc", "ActuatorSwc")["runnables"]
    return [event for event in runnable["events"] if event["type"] == "timing"]


class TestDisabledModes:
    def test_a_disabled_mode_on_a_stub_port_is_carried(self, source: Path, tmp_path: Path) -> None:
        def patch(tree: etree._ElementTree) -> None:
            _disable(tree, _mode_port(tree, MODE_INTERFACE, "MODE-SWITCH-INTERFACE"))

        model_root, report = _imported(source, tmp_path, patch)

        assert _timing_events(model_root) == [
            {
                "type": "timing",
                "period": 0.02,
                "disabled_modes": [{"port": "rMode", "mode_group_ref": MODE_GROUP, "mode_ref": MODE}],
            }
        ]
        assert [entry for entry in report.skip_entries if entry.tag == "DISABLED-MODE-IREFS"] == []

    def test_a_disabled_mode_whose_port_was_skipped_is_a_chained_line(self, source: Path, tmp_path: Path) -> None:
        """The port is typed by a ``MODE-SWITCH-INTERFACE`` the file declares, which SWCraft skips."""

        def patch(tree: etree._ElementTree) -> None:
            interfaces = _named(tree, "SENDER-RECEIVER-INTERFACE", "IVehicleSpeed").getparent()
            declared = _sub(interfaces, "MODE-SWITCH-INTERFACE")
            _sub(declared, "SHORT-NAME", "EcuModeIf")
            package = [interfaces.getparent().findtext(f"{NS}SHORT-NAME")]
            _disable(tree, _mode_port(tree, f"/{package[0]}/EcuModeIf", "MODE-SWITCH-INTERFACE"))

        model_root, report = _imported(source, tmp_path, patch)

        assert _timing_events(model_root) == [{"type": "timing", "period": 0.02}]
        (line,) = [entry for entry in report.skip_entries if entry.tag == "DISABLED-MODE-IREFS"]
        (port_line,) = [entry for entry in report.skip_entries if entry.path.endswith("/rMode")]
        assert line.path.endswith("/TMT_ActuatorMain")
        assert "its context port" in line.reason and "was skipped" in line.reason
        assert (line.category, line.rule) == (port_line.category, port_line.rule)

    @pytest.mark.parametrize("kind", ["TIMING-EVENT", "INIT-EVENT", "OPERATION-INVOKED-EVENT"])
    def test_the_reader_reads_every_event_kind_s_disabled_modes(self, source: Path, tmp_path: Path, kind: str) -> None:
        tree = etree.parse(str(source))
        port_path = _mode_port(tree, MODE_INTERFACE, "MODE-SWITCH-INTERFACE")
        _disable(tree, port_path)
        (event,) = [el for el in tree.iter(f"{NS}TIMING-EVENT") if el.find(f"{NS}DISABLED-MODE-IREFS") is not None]
        name = event.findtext(f"{NS}SHORT-NAME")
        event.tag = f"{NS}{kind}"

        model = read_arxml(str(_save(tree, tmp_path / "patched.arxml")))

        (read,) = [found for swc in model.swcs for found in swc.events if found.name == name]
        assert read.kind == kind
        assert [(mode.context_port_ref, mode.mode_group_ref, mode.mode_ref) for mode in read.disabled_modes] == [
            (port_path, MODE_GROUP, MODE)
        ]

    def test_an_event_stating_none_reads_none(self, source: Path) -> None:
        events = [event for swc in read_arxml(str(source)).swcs for event in swc.events]
        assert events and all(event.disabled_modes == [] for event in events)


# Descriptions and the composition's LONG-NAME, exactly


def _l_paragraph(owner: etree._Element, wrapper: str, paragraph: str, text: str | None) -> None:
    for existing in owner.findall(f"{NS}{wrapper}"):
        owner.remove(existing)
    element = etree.Element(f"{NS}{wrapper}")
    inner = _sub(element, paragraph, text, L="FOR-ALL")
    del inner
    owner.find(f"{NS}SHORT-NAME").addnext(element)


class TestTextsAreReadExactly:
    @pytest.mark.parametrize(
        "text",
        ["  leading and trailing  ", "line one\n  line two\n", " ", "plain"],
        ids=["spaces", "lines", "blank", "plain"],
    )
    def test_a_desc_keeps_its_whitespace(self, text: str) -> None:
        owner = etree.Element(f"{NS}RUNNABLE-ENTITY")
        _sub(owner, "SHORT-NAME", "R")
        _l_paragraph(owner, "DESC", "L-2", text)
        assert _extract_description(owner) == text

    def test_an_empty_desc_is_the_empty_string(self) -> None:
        owner = etree.Element(f"{NS}RUNNABLE-ENTITY")
        _sub(owner, "SHORT-NAME", "R")
        _l_paragraph(owner, "DESC", "L-2", None)
        assert _extract_description(owner) == ""

    def test_a_long_name_keeps_its_whitespace(self) -> None:
        owner = etree.Element(f"{NS}COMPOSITION-SW-COMPONENT-TYPE")
        _sub(owner, "SHORT-NAME", "C")
        _l_paragraph(owner, "LONG-NAME", "L-4", "  The Title \n")
        assert _extract_long_name(owner) == "  The Title \n"

    def test_an_imported_runnable_description_keeps_its_whitespace(self, source: Path, tmp_path: Path) -> None:
        text = "  Runs the actuator.\n  Twice.  "
        model_root, _report = _imported(
            source,
            tmp_path,
            lambda tree: _l_paragraph(_named(tree, "RUNNABLE-ENTITY", "ActuatorMain"), "DESC", "L-2", text),
        )
        (runnable,) = _file_of(model_root, "swc", "ActuatorSwc")["runnables"]
        assert runnable["description"] == text


class TestTheCompositionTitle:
    @pytest.fixture(scope="class")
    @staticmethod
    def compositions(tmp_path_factory: pytest.TempPathFactory) -> Path:
        base = tmp_path_factory.mktemp("compositions")
        model_root = base / "model"
        shutil.copytree(COMPOSITIONS_MODEL_ROOT, model_root)
        out = base / "compositions.arxml"
        generate_arxml(str(model_root), str(out))
        # The writer writes a title other than the name as the composition's LONG-NAME;
        # each test states its own, so none is kept.
        tree = etree.parse(str(out))
        for long_name in list(tree.iter(f"{NS}LONG-NAME")):
            long_name.getparent().remove(long_name)
        return _save(tree, out)

    def _titles(self, tmp_path: Path, arxml: Path) -> dict[str, str]:
        project = init_project(tmp_path / "proj")
        run_import(str(arxml), str(project / "swcraft.yaml"), force=True)
        return {
            document["name"]: document["title"]
            for document in (_load(path) for path in (project / "model" / "compositions").rglob("*.yaml"))
        }

    def test_a_long_name_becomes_the_title_exactly(self, compositions: Path, tmp_path: Path) -> None:
        tree = etree.parse(str(compositions))
        composition = next(tree.iter(f"{NS}COMPOSITION-SW-COMPONENT-TYPE"))
        name = composition.findtext(f"{NS}SHORT-NAME")
        _l_paragraph(composition, "LONG-NAME", "L-4", " The Whole Vehicle ")
        titles = self._titles(tmp_path, _save(tree, tmp_path / "titled.arxml"))
        assert titles[name] == " The Whole Vehicle "

    def test_without_one_the_title_is_the_name(self, compositions: Path, tmp_path: Path) -> None:
        titles = self._titles(tmp_path, compositions)
        assert titles and all(title == name for name, title in titles.items())

    @pytest.mark.parametrize("text", ["   ", "", None], ids=["whitespace", "empty", "no-text"])
    def test_a_blank_one_is_the_title_exactly(self, compositions: Path, tmp_path: Path, text: str | None) -> None:
        """A present LONG-NAME is carried verbatim, blank included."""
        tree = etree.parse(str(compositions))
        composition = next(tree.iter(f"{NS}COMPOSITION-SW-COMPONENT-TYPE"))
        name = composition.findtext(f"{NS}SHORT-NAME")
        _l_paragraph(composition, "LONG-NAME", "L-4", text)
        titles = self._titles(tmp_path, _save(tree, tmp_path / "blank.arxml"))
        assert titles[name] == (text or "")


# import component compares the carried values


def _target(tmp_path: Path) -> Path:
    project = init_project(tmp_path / "target")
    shutil.rmtree(project / "model")
    shutil.copytree(EXCHANGE_MODEL_ROOT, project / "model")
    return project / "model"


class TestImportComponentComparesTheValues:
    @pytest.fixture(scope="class")
    @staticmethod
    def actuator_export(tmp_path_factory: pytest.TempPathFactory) -> Path:
        out = tmp_path_factory.mktemp("actuator") / "ActuatorSwc.arxml"
        export_component(str(EXCHANGE_MODEL_ROOT), "ActuatorSwc", str(out))
        return out

    def _patched(self, export: Path, target: Path, patch: Any) -> Path:
        tree = etree.parse(str(export))
        patch(tree)
        return _save(tree, target)

    def test_the_unchanged_export_is_unchanged(self, actuator_export: Path, tmp_path: Path) -> None:
        report = import_component(str(actuator_export), str(_target(tmp_path)))
        assert report.swc_action == "unchanged"

    def test_a_shared_interface_differing_only_in_is_service_is_a_conflict(
        self, actuator_export: Path, tmp_path: Path
    ) -> None:
        model_root = _target(tmp_path)
        source = self._patched(
            actuator_export,
            tmp_path / "service.arxml",
            lambda tree: _is_service(tree, "SENDER-RECEIVER-INTERFACE", "IVehicleSpeed", "true"),
        )
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError) as caught:
            import_component(str(source), str(model_root))

        assert "IVehicleSpeed" in str(caught.value)
        assert files_of(model_root) == before

    @pytest.mark.parametrize(
        "properties",
        [{"SW_IMPL_POLICY": "QUEUED"}, {"SW_CALIBRATION_ACCESS": "READ-ONLY"}],
        ids=["policy", "calibration-access"],
    )
    def test_a_shared_interface_differing_only_in_a_data_element_property_is_a_conflict(
        self, actuator_export: Path, tmp_path: Path, properties: dict[str, str]
    ) -> None:
        model_root = _target(tmp_path)

        def patch(tree: etree._ElementTree) -> None:
            _element_properties(tree, "speed", **properties)
            if properties.get("SW_IMPL_POLICY") == "QUEUED":
                # A queued element's receiver takes the queued kind ([TPS_SWCT_01219]);
                # the non-queued one the export carried would be a not-carried line,
                # an abort before the comparison this is about.
                spec = _port_com_spec(tree, "rSpeed")
                _retag(spec, "QUEUED-RECEIVER-COM-SPEC", keep=("USES-END-TO-END-PROTECTION",))
                _set_children(spec, {"QUEUE-LENGTH": "1"})

        source = self._patched(actuator_export, tmp_path / "element.arxml", patch)
        with pytest.raises(ComponentImportConflictError):
            import_component(str(source), str(model_root))

    def test_a_component_differing_only_in_a_com_spec_value_is_a_difference(
        self, actuator_export: Path, tmp_path: Path
    ) -> None:
        model_root = _target(tmp_path)
        source = self._patched(
            actuator_export,
            tmp_path / "com-spec.arxml",
            lambda tree: _set_children(_port_com_spec(tree, "rSpeed"), {"ENABLE-UPDATE": "true"}),
        )
        with pytest.raises(ComponentImportConflictError):
            import_component(str(source), str(model_root))

        report = import_component(str(source), str(model_root), update=True)

        assert report.swc_action == "updated"
        assert _port(model_root, "ActuatorSwc", "rSpeed")["com_specs"] == [
            {"data_element": "speed", "enable_update": True}
        ]


# The merge compares the carried values


def _sr(
    package: str, *, is_service: str | None = None, policy: str | None = None, access: str | None = None
) -> SRInterfaceInfo:
    return SRInterfaceInfo(
        path=f"{package}/ISig",
        name="ISig",
        description="",
        data_elements=[
            SRDataElementInfo(name="d", type_ref="/Shared/T", sw_impl_policy=policy, sw_calibration_access=access)
        ],
        is_service=is_service,
    )


def _cs(package: str, is_service: str | None) -> CSInterfaceInfo:
    argument = CSArgumentInfo(name="a", type_ref="/Shared/T", direction="IN")
    return CSInterfaceInfo(
        path=f"{package}/ICall",
        name="ICall",
        description="",
        operations=[CSOperationInfo(name="Op", arguments=[argument])],
        is_service=is_service,
    )


def _swc(package: str, *, texts: dict[str, str] | None = None, modes: tuple[str, ...] = ()) -> SwcInfo:
    spec = DataElementComSpecInfo(
        com_spec_tag="NONQUEUED-RECEIVER-COM-SPEC", data_element_ref="/Shared/I/d", texts=dict(texts or {})
    )
    event = SwcEventInfo(
        path=f"{package}/Comp/Beh/TMT_Run",
        name="TMT_Run",
        kind="TIMING-EVENT",
        runnable_ref=f"{package}/Comp/Beh/Run",
        period=0.01,
        disabled_modes=[DisabledModeInfo(f"{package}/Comp/p", mode, f"{mode}/M") for mode in modes],
    )
    return SwcInfo(
        path=f"{package}/Comp",
        name="Comp",
        description="",
        ports=[
            SwcPortInfo(
                path=f"{package}/Comp/p",
                name="p",
                kind="R-PORT-PROTOTYPE",
                interface_ref="/Shared/I",
                description="",
                data_element_com_specs=[spec],
            )
        ],
        runnables=[
            SwcRunnableInfo(path=f"{package}/Comp/Beh/Run", name="Run", min_start_interval=None, description="")
        ],
        events=[event],
    )


def _verdict(first: Any, second: Any, layer: str) -> str:
    model = ImportModel(source_path="inline-test")
    getattr(model, layer).extend([first, second])
    (group,) = decide_merges(model).groups
    return group.verdict


class TestTheMergeComparesTheCarriedValues:
    @pytest.mark.parametrize(
        ("first", "second", "layer", "verdict"),
        [
            (_sr("/A"), _sr("/B"), "sr_interfaces", MERGED),
            (_sr("/A", is_service="false"), _sr("/B"), "sr_interfaces", MERGED),
            (_sr("/A", is_service="true"), _sr("/B"), "sr_interfaces", CONFLICTING),
            (_sr("/A", is_service="yes"), _sr("/B"), "sr_interfaces", CONFLICTING),
            (_sr("/A", is_service="yes"), _sr("/B", is_service="yes"), "sr_interfaces", MERGED),
            (_sr("/A", policy="QUEUED"), _sr("/B"), "sr_interfaces", CONFLICTING),
            (_sr("/A", access="READ-ONLY"), _sr("/B", access="READ-WRITE"), "sr_interfaces", CONFLICTING),
            (_cs("/A", "true"), _cs("/B", None), "cs_interfaces", CONFLICTING),
            (_cs("/A", "0"), _cs("/B", None), "cs_interfaces", MERGED),
            (_swc("/A"), _swc("/B"), "swcs", MERGED),
            (_swc("/A", texts={"ENABLE-UPDATE": "false"}), _swc("/B"), "swcs", MERGED),
            (_swc("/A", texts={"ENABLE-UPDATE": "true"}), _swc("/B"), "swcs", CONFLICTING),
            (_swc("/A", modes=("/X/I/g",)), _swc("/B"), "swcs", CONFLICTING),
        ],
        ids=[
            "sr-equal",
            "sr-false-is-absent",
            "sr-is-service",
            "sr-unreadable-is-service-beside-a-readable-one",
            "sr-one-unreadable-is-service",
            "sr-policy",
            "sr-calibration-access",
            "cs-is-service",
            "cs-false-is-absent",
            "swc-equal",
            "swc-silent-com-spec-is-absent",
            "swc-com-spec-value",
            "swc-disabled-mode",
        ],
    )
    def test_the_verdict(self, first: Any, second: Any, layer: str, verdict: str) -> None:
        assert _verdict(first, second, layer) == verdict


class TestComSpecValues:
    def test_an_unreadable_value_is_uncarried_as_spelled(self) -> None:
        spec = DataElementComSpecInfo(
            com_spec_tag="NONQUEUED-RECEIVER-COM-SPEC",
            data_element_ref="/I/d",
            texts={"HANDLE-OUT-OF-RANGE": "EXTERNAL-REPLACEMENT", "ALIVE-TIMEOUT": "-1", "QUEUE-LENGTH": "0"},
        )
        assert com_spec_values(spec) == (
            {},
            {"HANDLE-OUT-OF-RANGE": "EXTERNAL-REPLACEMENT", "ALIVE-TIMEOUT": "-1", "QUEUE-LENGTH": "0"},
        )

    def test_a_silent_value_is_carried_by_equivalence_and_another_is_carried(self) -> None:
        spec = DataElementComSpecInfo(
            com_spec_tag="NONQUEUED-RECEIVER-COM-SPEC",
            data_element_ref="/I/d",
            texts={
                "ENABLE-UPDATE": "false",
                "ALIVE-TIMEOUT": "0.0",
                "HANDLE-NEVER-RECEIVED": "true",
                "QUEUE-LENGTH": "3",
            },
        )
        assert com_spec_values(spec) == ({"handle_never_received": True, "queue_length": 3}, {})
