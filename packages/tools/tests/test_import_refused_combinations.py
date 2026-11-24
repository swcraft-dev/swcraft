"""An import that builds a model ``check-model`` refuses says so, one line per case.

Three combinations the importer carries exactly as the file states them -- each
element conforms on its own, so nothing is dropped -- and that the writer's guards
and ``check-model`` then refuse:

* a component's require port on a **queued** data element with no queue length
  (``rule:swc.port.queued-receiver-without-queue-length``): section 1 citing
  ``[TPS_SWCT_01219]`` when the file defines no ``QUEUED-RECEIVER-COM-SPEC`` for
  the element, and SWCraft's own line when it defines one without a
  ``QUEUE-LENGTH``;
* a **composition's port on a service interface**
  (``rule:composition.port-service-interface``): section 1, ``[constr_1174]``;
* a **by-value read of a queued element**
  (``rule:swc.access.queued-read-by-value``): section 1, ``[constr_1277]``.

Every case asserts both halves: the line, and that ``check-model`` really reports
the rule the line names on the model the import wrote. The sources are SWCraft's
own export of the ``component_exchange`` fixture, patched with ``lxml``.

What it does **not** reach: the real tool exports (the closed suites, ``dev.py test-internal``),
a composition's port on a queued element (it carries no queue length, and has its own
whole-com-spec line), and a port on an external
interface stub, which declares no element and no ``IS-SERVICE``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from lxml import etree
from project_support import init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_reader import SkipCategory, StandardRule, cite
from swcraft_tools.arxml_version import NAMESPACE, ArxmlVersion
from swcraft_tools.component_exchange import LossyComponentImportError, export_component, import_component
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer.arxml_generation import generate_arxml

NS = f"{{{NAMESPACE}}}"
EXCHANGE_MODEL_ROOT = Path(__file__).resolve().parent / "fixtures" / "component_exchange" / "model"
ACTUATOR = "/ComponentTypes/ActuatorSwc"

QUEUE_RULE = "swc.port.queued-receiver-without-queue-length"
SERVICE_RULE = "composition.port-service-interface"
READ_RULE = "swc.access.queued-read-by-value"
THE_THREE = {QUEUE_RULE, SERVICE_RULE, READ_RULE}


def _model(root: Path) -> Path:
    """The ``component_exchange`` tree, ``ActuatorSwc`` reading ``rSpeed.speed`` by value."""
    shutil.copytree(EXCHANGE_MODEL_ROOT, root)
    path = root / "swc" / "actuation" / "ActuatorSwc.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["runnables"][0]["reads"] = [{"port": "rSpeed", "element": "speed", "by": "value"}]
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return root


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("refused")
    out = base / "model.arxml"
    generate_arxml(str(_model(base / "model")), str(out))
    return out


def _named(tree: etree._ElementTree, tag: str, name: str) -> etree._Element:
    (found,) = [el for el in tree.iter(f"{NS}{tag}") if el.findtext(f"{NS}SHORT-NAME") == name]
    return found


def _sub(parent: etree._Element, tag: str, text: str | None = None, **attributes: str) -> etree._Element:
    element = etree.SubElement(parent, f"{NS}{tag}", **attributes)
    if text is not None:
        element.text = text
    return element


def _queue(tree: etree._ElementTree, receiver: str | None) -> None:
    """Make ``speed`` queued; *receiver* is ``None`` (no com-spec), ``"bare"`` (no ``QUEUE-LENGTH``) or a length."""
    prototype = _named(tree, "VARIABLE-DATA-PROTOTYPE", "speed")
    props = etree.Element(f"{NS}SW-DATA-DEF-PROPS")
    _sub(_sub(_sub(props, "SW-DATA-DEF-PROPS-VARIANTS"), "SW-DATA-DEF-PROPS-CONDITIONAL"), "SW-IMPL-POLICY", "QUEUED")
    prototype.find(f"{NS}SHORT-NAME").addnext(props)
    for port, tag in (("pSpeed", "P-PORT-PROTOTYPE"), ("rSpeed", "R-PORT-PROTOTYPE")):
        (block,) = [child for child in _named(tree, tag, port) if child.tag.endswith("-COM-SPECS")]
        (spec,) = list(block)
        reference = spec.find(f"{NS}DATA-ELEMENT-REF")
        block.remove(spec)
        if port == "rSpeed" and receiver is None:
            block.getparent().remove(block)
            continue
        queued = _sub(block, "QUEUED-SENDER-COM-SPEC" if port == "pSpeed" else "QUEUED-RECEIVER-COM-SPEC")
        queued.append(reference)
        if port == "rSpeed" and receiver != "bare":
            _sub(queued, "QUEUE-LENGTH", receiver)


def _by_reference(tree: etree._ElementTree) -> None:
    """Turn the one by-value receive point into a by-argument one, which a queued element may have."""
    (container,) = list(tree.iter(f"{NS}DATA-RECEIVE-POINT-BY-VALUES"))
    container.tag = f"{NS}DATA-RECEIVE-POINT-BY-ARGUMENTS"


def _service_outer_port(tree: etree._ElementTree) -> None:
    """Give the composition a require port on ``IVehicleSpeed`` and make that interface a service one."""
    interface = _named(tree, "SENDER-RECEIVER-INTERFACE", "IVehicleSpeed")
    interface.find(f"{NS}IS-SERVICE").text = "true"
    reference = _named(tree, "R-PORT-PROTOTYPE", "rSpeed").find(f"{NS}REQUIRED-INTERFACE-TREF")
    (composition,) = tree.iter(f"{NS}COMPOSITION-SW-COMPONENT-TYPE")
    ports = composition.find(f"{NS}PORTS")
    if ports is None:
        ports = etree.Element(f"{NS}PORTS")
        composition.find(f"{NS}SHORT-NAME").addnext(ports)
    port = _sub(ports, "R-PORT-PROTOTYPE")
    _sub(port, "SHORT-NAME", "rOuter")
    _sub(port, "REQUIRED-INTERFACE-TREF", reference.text, DEST=reference.get("DEST"))


def _imported(source: Path, tmp_path: Path, *patches: Any) -> tuple[Path, list[Any]]:
    tree = etree.parse(str(source))
    for patch in patches:
        patch(tree)
    patched = tmp_path / "patched.arxml"
    patched.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    project = init_project(tmp_path / "proj")
    report = run_import(str(patched), str(project / "swcraft.yaml"), force=True)
    return project / "model", list(report.skip_entries)


def _naming(entries: list[Any], rule: str) -> list[Any]:
    return [entry for entry in entries if f"(rule:{rule})" in entry.reason]


def _reported(model_root: Path) -> set[str]:
    """Which of the three rules ``check-model`` reports on the imported model."""
    return {record.rule for record in check_cross_layer_consistency(str(model_root)).records} & THE_THREE


class TestAConformingSourceHasNoneOfTheThreeLines:
    def test_the_writers_own_file(self, source: Path, tmp_path: Path) -> None:
        model_root, entries = _imported(source, tmp_path)

        assert [entry for entry in entries if "check-model refuses" in entry.reason] == []
        assert _reported(model_root) == set()

    def test_a_queued_element_with_its_queue_length_read_by_argument(self, source: Path, tmp_path: Path) -> None:
        model_root, entries = _imported(source, tmp_path, lambda tree: _queue(tree, "4"), _by_reference)

        assert [entry for entry in entries if "check-model refuses" in entry.reason] == []
        assert _reported(model_root) == set()


class TestAQueuedElementWithNoQueueLength:
    def test_no_com_spec_at_all_is_the_files_defect(self, source: Path, tmp_path: Path) -> None:
        model_root, entries = _imported(source, tmp_path, lambda tree: _queue(tree, None), _by_reference)

        (line,) = _naming(entries, QUEUE_RULE)
        assert (line.path, line.tag) == (f"{ACTUATOR}/rSpeed/REQUIRED-COM-SPECS", "QUEUED-RECEIVER-COM-SPEC")
        assert (line.category, line.rule, line.effect) == (
            SkipCategory.EXPORT_DEFECT,
            StandardRule.QUEUED_COM_SPEC_DEFINED,
            "abort",
        )
        assert cite(line.rule, ArxmlVersion.V4_2_2) == "R4.2.2 [TPS_SWCT_01219]"
        assert line.reason == (
            "no QUEUED-RECEIVER-COM-SPEC is defined for queued data element 'speed'; the port is imported with no "
            "queue_length for it, and the model is one check-model refuses "
            "(rule:swc.port.queued-receiver-without-queue-length)"
        )
        assert _reported(model_root) == {QUEUE_RULE}

    def test_a_signed_queue_length_is_no_queue_length(self, source: Path, tmp_path: Path) -> None:
        """A signed ``QUEUE-LENGTH`` is no length: ``+4`` is read as no length, never as four.

        ``QUEUED-RECEIVER-COM-SPEC/QUEUE-LENGTH`` is a ``POSITIVE-INTEGER`` (XSD R4.3.0:86393; R4.2.2:75075),
        which admits no sign.
        """
        model_root, entries = _imported(source, tmp_path, lambda tree: _queue(tree, "+4"), _by_reference)

        (line,) = _naming(entries, QUEUE_RULE)
        assert (line.category, line.rule, line.effect) == (SkipCategory.NOT_SUPPORTED, None, "abort")
        assert "states no QUEUE-LENGTH SWCraft carries" in line.reason
        assert _reported(model_root) == {QUEUE_RULE}

    def test_a_com_spec_without_a_queue_length_is_swcrafts_line(self, source: Path, tmp_path: Path) -> None:
        """The com-spec is defined, so ``[TPS_SWCT_01219]`` is kept: no quoted rule is broken."""
        model_root, entries = _imported(source, tmp_path, lambda tree: _queue(tree, "bare"), _by_reference)

        (line,) = _naming(entries, QUEUE_RULE)
        assert (line.path, line.tag) == (f"{ACTUATOR}/rSpeed/REQUIRED-COM-SPECS", "QUEUED-RECEIVER-COM-SPEC")
        assert (line.category, line.rule, line.effect) == (SkipCategory.NOT_SUPPORTED, None, "abort")
        assert "states no QUEUE-LENGTH SWCraft carries" in line.reason
        assert _reported(model_root) == {QUEUE_RULE}

    def test_a_non_queued_com_spec_of_a_queued_element_is_a_missing_queued_one(
        self, source: Path, tmp_path: Path
    ) -> None:
        """The wrong kind is its own whole-com-spec line; the queued one is still not defined."""

        def patch(tree: etree._ElementTree) -> None:
            prototype = _named(tree, "VARIABLE-DATA-PROTOTYPE", "speed")
            props = etree.Element(f"{NS}SW-DATA-DEF-PROPS")
            conditional = _sub(_sub(props, "SW-DATA-DEF-PROPS-VARIANTS"), "SW-DATA-DEF-PROPS-CONDITIONAL")
            _sub(conditional, "SW-IMPL-POLICY", "QUEUED")
            prototype.find(f"{NS}SHORT-NAME").addnext(props)

        model_root, entries = _imported(source, tmp_path, patch, _by_reference)

        (line,) = _naming(entries, QUEUE_RULE)
        assert (line.category, line.rule) == (SkipCategory.EXPORT_DEFECT, StandardRule.QUEUED_COM_SPEC_DEFINED)
        assert QUEUE_RULE in _reported(model_root)

    def test_import_component_refuses_it(self, tmp_path: Path) -> None:
        model_root = _model(tmp_path / "source" / "model")
        export = tmp_path / "source" / "ActuatorSwc.arxml"
        export_component(str(model_root), "ActuatorSwc", str(export))
        tree = etree.parse(str(export))
        _queue_component(tree)
        _by_reference(tree)
        export.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
        project = init_project(tmp_path / "target")

        with pytest.raises(LossyComponentImportError) as refused:
            import_component(str(export), str(project / "model"))

        assert "no QUEUED-RECEIVER-COM-SPEC is defined for queued data element 'speed'" in str(refused.value)


def _queue_component(tree: etree._ElementTree) -> None:
    """A component export's ``speed`` made queued, its one receiver com-spec removed."""
    prototype = _named(tree, "VARIABLE-DATA-PROTOTYPE", "speed")
    props = etree.Element(f"{NS}SW-DATA-DEF-PROPS")
    _sub(_sub(_sub(props, "SW-DATA-DEF-PROPS-VARIANTS"), "SW-DATA-DEF-PROPS-CONDITIONAL"), "SW-IMPL-POLICY", "QUEUED")
    prototype.find(f"{NS}SHORT-NAME").addnext(props)
    (block,) = [child for child in _named(tree, "R-PORT-PROTOTYPE", "rSpeed") if child.tag.endswith("-COM-SPECS")]
    block.getparent().remove(block)


class TestACompositionPortOnAServiceInterface:
    def test_it_is_the_files_defect_and_the_port_is_imported(self, source: Path, tmp_path: Path) -> None:
        model_root, entries = _imported(source, tmp_path, _service_outer_port)

        (line,) = _naming(entries, SERVICE_RULE)
        assert line.path.endswith("/rOuter")
        assert line.tag == "REQUIRED-INTERFACE-TREF"
        assert (line.category, line.rule, line.effect) == (
            SkipCategory.EXPORT_DEFECT,
            StandardRule.NO_SERVICE_PORT_ON_A_COMPOSITION,
            "abort",
        )
        assert cite(line.rule, ArxmlVersion.V4_3_0) == "R4.3.0 [constr_1174]"
        assert line.reason == (
            "composition port 'rOuter' is typed by interface 'IVehicleSpeed', which states IS-SERVICE true; the "
            "port is imported as the file states it, and the model is one check-model refuses "
            "(rule:composition.port-service-interface)"
        )
        (composition,) = [
            yaml.safe_load(path.read_text(encoding="utf-8")) for path in (model_root / "compositions").rglob("*.yaml")
        ]
        assert [port["name"] for port in composition["ports"]] == ["rOuter"]
        assert _reported(model_root) == {SERVICE_RULE}

    def test_a_component_port_on_a_service_interface_is_no_line(self, source: Path, tmp_path: Path) -> None:
        def patch(tree: etree._ElementTree) -> None:
            _named(tree, "SENDER-RECEIVER-INTERFACE", "IVehicleSpeed").find(f"{NS}IS-SERVICE").text = "true"

        model_root, entries = _imported(source, tmp_path, patch)

        assert _naming(entries, SERVICE_RULE) == []
        assert _reported(model_root) == set()


class TestAByValueReadOfAQueuedElement:
    def test_it_is_the_files_defect_and_the_read_is_imported(self, source: Path, tmp_path: Path) -> None:
        model_root, entries = _imported(source, tmp_path, lambda tree: _queue(tree, "4"))

        (line,) = _naming(entries, READ_RULE)
        assert line.path.startswith(f"{ACTUATOR}/")
        assert line.tag == "VARIABLE-ACCESS"
        assert (line.category, line.rule, line.effect) == (
            SkipCategory.EXPORT_DEFECT,
            StandardRule.NO_QUEUED_RECEIVE_BY_VALUE,
            "abort",
        )
        assert cite(line.rule, ArxmlVersion.V4_2_2) == "R4.2.2 [constr_1277]"
        assert line.reason == (
            "VARIABLE-ACCESS reads queued data element 'speed' through port 'rSpeed' by value; the read is "
            "imported as the file states it, and the model is one check-model refuses "
            "(rule:swc.access.queued-read-by-value)"
        )
        (swc,) = [
            document
            for document in (
                yaml.safe_load(path.read_text(encoding="utf-8")) for path in (model_root / "swc").rglob("*.yaml")
            )
            if document["name"] == "ActuatorSwc"
        ]
        assert swc["runnables"][0]["reads"] == [{"port": "rSpeed", "element": "speed", "by": "value"}]
        assert _reported(model_root) == {READ_RULE}

    def test_a_by_value_read_of_a_non_queued_element_is_no_line(self, source: Path, tmp_path: Path) -> None:
        _model_root, entries = _imported(source, tmp_path)

        assert _naming(entries, READ_RULE) == []
