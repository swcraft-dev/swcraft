"""The importers carry a port's own com-spec init value.

A com-spec's ``initValue`` supersedes its data prototype's on that port (``[TPS_SWCT_01182]``, SWCT R4.2.2
``:21053-21077``, R4.3.0 ``:23450-23475``). The data element's ``init`` is decided by the element alone; a
non-queued com-spec whose ``INIT-VALUE`` is not the reference to the element's own constant is the port's own value:

* it is read as a ``CONSTANT-REFERENCE`` or, as SWCraft's own writer writes it, a value specification in place
  (``PortComSpecInitInfo.value``);
* ``map_interfaces`` shapes it against the element's type, and the component mapper carries it on the port's
  ``com_specs`` entry when it differs from the element's own; a value equal to the element's is carried by
  equivalence, with no entry and no line;
* what is not carried is one F4 line at ``({port}/{block}, INIT-VALUE)``: a value the reader could not read or the
  mapper cannot shape, and every value on a composition's port;
* the ``SHORT-LABEL``s such a value loses are **one** descriptive line per com-spec at ``({port}/{block},
  SHORT-LABEL)``; a hexadecimal spelling is a radix line; a port the import does not keep gets no line of its own.

The sources are the writer's own output of the committed ``init_values`` tree, edited with ``lxml`` where a case
needs what SWCraft does not write. What this module does **not** reach: real tool exports, and the input-vs-output
oracle's cases over this module's source, which live in the closed half.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import INIT_VALUES_MODEL_ROOT
from lxml import etree
from project_support import init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.merge import CONFLICTING, MERGED
from swcraft_tools.arxml_reader import (
    ClientComSpecInfo,
    CompositionInfo,
    ComSpecInfo,
    ConstantScalar,
    DataElementComSpecInfo,
    ImportModel,
    PortApiOptionInfo,
    PortComSpecInitInfo,
    ServerComSpecInfo,
    SkipCategory,
    SkipEntry,
    SwcRunnableInfo,
    read_arxml,
)
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.component_exchange import (
    ComponentImportConflictError,
    export_component,
    import_component,
)
from swcraft_tools.reader import read_interfaces, read_swc_model
from swcraft_tools.schema import SRInterface
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml
from test_arxml_import_merges import _group, _map_all, _port, _swc

NS = f"{{{NAMESPACE}}}"
COMPONENT = "SignalsSwc"
PROVIDE = f"/ComponentTypes/{COMPONENT}/pSignals"
#: A value specification the reader does not read: its refusal names the tag.
_UNREAD_VALUE = (
    "<REFERENCE-VALUE-SPECIFICATION>"
    '<REFERENCE-VALUE-REF DEST="VARIABLE-DATA-PROTOTYPE">/PortInterfaces/ISignals/speed</REFERENCE-VALUE-REF>'
    "</REFERENCE-VALUE-SPECIFICATION>"
)
REQUIRE = f"/ComponentTypes/{COMPONENT}/rSignals"
PROVIDED = f"{PROVIDE}/PROVIDED-COM-SPECS"
REQUIRED = f"{REQUIRE}/REQUIRED-COM-SPECS"

#: The entries ``test_arxml_symbol_and_port_init_writer.py`` writes, one of each shape.
PROVIDE_ENTRIES: list[dict[str, Any]] = [
    {"data_element": "speed", "init": 9},
    {"data_element": "plain", "init": 3},
    {"data_element": "limits", "init": {"low": -5.5, "high": 5.5, "tiny": 0.25, "whole": 100.0}},
]
REQUIRE_ENTRIES: list[dict[str, Any]] = [
    {"data_element": "mode", "init": "OFF"},
    {"data_element": "bytes", "init": [3, 4]},
    {"data_element": "deep", "init": [{"row": {"index": 4, "mode": "ON"}}, {"row": {"index": 5, "mode": "OFF"}}]},
    {"data_element": "speed", "init": 7},
]

#: A record value for ``Limits_T`` labelled at the record and at each member with a text the
#: writer does not write back (it labels a member with its field name): five labels lost.
LABELLED_LIMITS = (
    "<RECORD-VALUE-SPECIFICATION><SHORT-LABEL>Bounds</SHORT-LABEL><FIELDS>"
    + "".join(
        f"<NUMERICAL-VALUE-SPECIFICATION><SHORT-LABEL>m{index}</SHORT-LABEL><VALUE>{value}</VALUE>"
        "</NUMERICAL-VALUE-SPECIFICATION>"
        for index, value in enumerate(("-2", "2", "0.5", "50"))
    )
    + "</FIELDS></RECORD-VALUE-SPECIFICATION>"
)
LABELLED_LIMITS_VALUE = {"low": -2, "high": 2, "tiny": 0.5, "whole": 50}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _model_root(tmp_path: Path, provide=None, require=None) -> Path:
    """``fixtures/init_values``' model, copied, with the two ports' ``com_specs`` set as given."""
    root = tmp_path / "source_model"
    shutil.copytree(INIT_VALUES_MODEL_ROOT, root)
    path = root / "swc" / "signals" / f"{COMPONENT}.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    ports = {port["name"]: port for port in document["ports"]}
    if provide is not None:
        ports["pSignals"]["com_specs"] = provide
    if require is not None:
        ports["rSignals"]["com_specs"] = require
    path.write_text(yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")
    return root


def _generated(tmp_path: Path, *, version: str = "4.3.0", provide=None, require=None) -> Path:
    output = tmp_path / f"signals-{version}.arxml"
    generate_arxml(str(_model_root(tmp_path, provide, require)), str(output), version=version)
    return output


def _snippet(xml: str) -> etree._Element:
    return etree.fromstring(f'<W xmlns="{NAMESPACE}">{xml}</W>')[0]


class _Source:
    """A generated file, edited in memory and written back by :meth:`save`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.tree = etree.parse(str(path))
        self.root = self.tree.getroot()

    def save(self) -> Path:
        self.path.write_bytes(etree.tostring(self.tree, xml_declaration=True, encoding="UTF-8"))
        return self.path

    def package(self, name: str) -> etree._Element:
        return next(
            package for package in self.root.iter(f"{NS}AR-PACKAGE") if package.findtext(f"{NS}SHORT-NAME") == name
        )

    def node(self, path: str) -> etree._Element:
        """The element at an AUTOSAR *path*, walked by ``SHORT-NAME``."""
        names = path.strip("/").split("/")
        found = [
            node
            for node in self.root.iter()
            if isinstance(node.tag, str) and node.findtext(f"{NS}SHORT-NAME") == names[-1]
        ]
        for node in found:
            ancestry, current = [], node
            while current is not None:
                name = current.findtext(f"{NS}SHORT-NAME") if isinstance(current.tag, str) else None
                if name is not None:
                    ancestry.append(name)
                current = current.getparent()
            if list(reversed(ancestry)) == names:
                return node
        raise AssertionError(f"no element at {path}")

    def com_spec(self, port_path: str, element: str) -> etree._Element:
        port = self.node(port_path)
        return next(
            spec
            for spec in port.iter(f"{NS}NONQUEUED-SENDER-COM-SPEC", f"{NS}NONQUEUED-RECEIVER-COM-SPEC")
            if spec.findtext(f"{NS}DATA-ELEMENT-REF").endswith(f"/{element}")
        )

    def add_constant(self, name: str, value_xml: str) -> str:
        """A ``CONSTANT-SPECIFICATION`` *name* in ``PortInterfaces``, holding *value_xml*; its path."""
        elements = self.package("PortInterfaces").find(f"{NS}ELEMENTS")
        elements.insert(
            0,
            _snippet(
                f"<CONSTANT-SPECIFICATION><SHORT-NAME>{name}</SHORT-NAME><VALUE-SPEC>{value_xml}</VALUE-SPEC>"
                "</CONSTANT-SPECIFICATION>"
            ),
        )
        return f"/PortInterfaces/{name}"

    def init_value(self, port_path: str, element: str, value_xml: str) -> None:
        """The com-spec's ``INIT-VALUE`` replaced by one holding *value_xml*, at its own position."""
        spec = self.com_spec(port_path, element)
        old = spec.find(f"{NS}INIT-VALUE")
        new = _snippet(f"<INIT-VALUE>{value_xml}</INIT-VALUE>")
        if old is None:
            spec.append(new)
        else:
            old.addprevious(new)
            spec.remove(old)

    def reference(self, port_path: str, element: str, constant_path: str) -> None:
        self.init_value(
            port_path,
            element,
            f'<CONSTANT-REFERENCE><CONSTANT-REF DEST="CONSTANT-SPECIFICATION">{constant_path}</CONSTANT-REF>'
            "</CONSTANT-REFERENCE>",
        )


def _import(tmp_path: Path, source: Path):
    project = init_project(tmp_path / "proj")
    report = run_import(str(source), str(project / "swcraft.yaml"))
    return project / "model", report


def _entries(model_root: Path, port: str) -> dict[str, object]:
    """``{data element: init}`` of the component port *port*'s ``com_specs`` entries stating one."""
    (swc,) = [swc for swc in read_swc_model(str(model_root)) if swc.name == COMPONENT]
    (found,) = [candidate for candidate in swc.ports if candidate.name == port]
    return {entry.data_element: entry.init for entry in found.com_specs if entry.init is not None}


def _element_inits(model_root: Path) -> dict[str, object]:
    interface = next(iface for iface in read_interfaces(str(model_root)) if iface.name == "ISignals")
    assert isinstance(interface, SRInterface)
    return {element.name: element.init for element in interface.data_elements}


def _at(report, path: str, tag: str | None = None) -> list[SkipEntry]:
    return [entry for entry in report.skip_entries if entry.path == path and (tag is None or entry.tag == tag)]


# ---------------------------------------------------------------------------
# The reader reads a value written in place
# ---------------------------------------------------------------------------


class TestTheReader:
    def test_a_value_written_in_place_is_read_and_a_reference_stays_a_path(self, tmp_path: Path) -> None:
        model = read_arxml(str(_generated(tmp_path, provide=PROVIDE_ENTRIES)))
        (swc,) = model.swcs
        inits = {
            (init.port_path, init.data_element_ref.rsplit("/", 1)[1]): init
            for port in swc.ports
            for init in port.com_spec_inits
        }
        speed = inits[(PROVIDE, "speed")]
        assert (speed.constant_ref, speed.unreadable_reason) == (None, None)
        assert speed.value == ConstantScalar(value=9)
        ratio = inits[(PROVIDE, "ratio")]
        assert (ratio.constant_ref, ratio.value) == ("/PortInterfaces/ISignals_ratio_Init", None)
        for init in inits.values():
            assert sum(part is not None for part in (init.constant_ref, init.value, init.unreadable_reason)) == 1

    def test_a_value_in_place_the_reader_cannot_read_is_its_reason(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        source.init_value(PROVIDE, "speed", _UNREAD_VALUE)
        model = read_arxml(str(source.save()))
        (init,) = [
            init
            for port in model.swcs[0].ports
            for init in port.com_spec_inits
            if init.port_path == PROVIDE and init.data_element_ref.endswith("/speed")
        ]
        assert (init.constant_ref, init.value) == (None, None)
        assert init.unreadable_reason == (
            "INIT-VALUE written in place was not read: "
            "REFERENCE-VALUE-SPECIFICATION is not a value specification this reader reads"
        )


# ---------------------------------------------------------------------------
# SWCraft's own output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
def test_swcraft_s_own_output_with_a_port_init_imports_back_to_the_same_values(tmp_path: Path, version: str) -> None:
    """Every port value the writer wrote in place comes back, but the one equal to its element's.

    ``rSignals``' ``speed`` states the element's own ``7``: the importer carries it by
    equivalence and records nowhere.
    """
    source = _generated(tmp_path, version=version, provide=PROVIDE_ENTRIES, require=REQUIRE_ENTRIES)
    model_root, report = _import(tmp_path, source)

    assert _entries(model_root, "pSignals") == {entry["data_element"]: entry["init"] for entry in PROVIDE_ENTRIES}
    assert _entries(model_root, "rSignals") == {
        entry["data_element"]: entry["init"] for entry in REQUIRE_ENTRIES if entry["data_element"] != "speed"
    }
    assert _element_inits(model_root)["speed"] == 7
    assert _at(report, PROVIDED) == [] and _at(report, REQUIRED) == []
    assert check_cross_layer_consistency(str(model_root)).issues == []


# ---------------------------------------------------------------------------
# Overrides through another constant, and their labels
# ---------------------------------------------------------------------------


class TestOverrides:
    def test_an_override_is_carried_and_the_element_keeps_its_own(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        source.reference(REQUIRE, "limits", source.add_constant("PortLimits", LABELLED_LIMITS))
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, "rSignals") == {"limits": LABELLED_LIMITS_VALUE}
        assert _element_inits(model_root)["limits"] == {"low": -1.0e20, "high": 1.0e20, "tiny": 1.0e-7, "whole": 100.0}
        assert _at(report, REQUIRED, "INIT-VALUE") == []
        (line,) = _at(report, REQUIRED, "SHORT-LABEL")
        assert (line.effect, line.category) == ("report", SkipCategory.NOT_SUPPORTED)
        assert line.reason.startswith("5 SHORT-LABEL(s) of the port's own init value for data element 'limits'")
        assert check_cross_layer_consistency(str(model_root)).issues == []

    def test_an_override_on_an_element_with_no_init_is_carried(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        constant = source.add_constant(
            "PortPlain", "<NUMERICAL-VALUE-SPECIFICATION><VALUE>4</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        source.reference(PROVIDE, "plain", constant)
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, "pSignals") == {"plain": 4}
        assert _element_inits(model_root)["plain"] is None
        assert _at(report, PROVIDED) == []

    def test_two_com_specs_in_one_block_losing_labels_are_two_lines(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        source.reference(REQUIRE, "limits", source.add_constant("PortLimits", LABELLED_LIMITS))
        array = (
            "<ARRAY-VALUE-SPECIFICATION><SHORT-LABEL>Pair</SHORT-LABEL><ELEMENTS>"
            "<NUMERICAL-VALUE-SPECIFICATION><VALUE>5</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
            "<NUMERICAL-VALUE-SPECIFICATION><VALUE>6</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
            "</ELEMENTS></ARRAY-VALUE-SPECIFICATION>"
        )
        source.reference(REQUIRE, "bytes", source.add_constant("PortBytes", array))
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, "rSignals") == {"limits": LABELLED_LIMITS_VALUE, "bytes": [5, 6]}
        assert sorted(int(line.reason.split(" ", 1)[0]) for line in _at(report, REQUIRED, "SHORT-LABEL")) == [1, 5]

    def test_a_value_without_lost_labels_has_no_label_line(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        constant = source.add_constant(
            "PortSpeed", "<NUMERICAL-VALUE-SPECIFICATION><VALUE>8</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        source.reference(PROVIDE, "speed", constant)
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, "pSignals") == {"speed": 8}
        assert _at(report, PROVIDED) == []

    def test_a_value_equal_to_the_element_s_through_another_constant_is_no_entry_and_no_line(
        self, tmp_path: Path
    ) -> None:
        """The labels are not compared either: both sides are constant references."""
        source = _Source(_generated(tmp_path))
        constant = source.add_constant(
            "PortSpeed",
            "<NUMERICAL-VALUE-SPECIFICATION><SHORT-LABEL>Seven</SHORT-LABEL><VALUE>7</VALUE>"
            "</NUMERICAL-VALUE-SPECIFICATION>",
        )
        source.reference(PROVIDE, "speed", constant)
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, "pSignals") == {}
        assert _at(report, PROVIDED) == []

    def test_a_value_in_place_equal_to_the_element_s_with_other_labels_is_one_label_line(self, tmp_path: Path) -> None:
        """The writer answers it with the element's constant; the comparison resolves its labels."""
        source = _Source(_generated(tmp_path))
        source.init_value(
            PROVIDE,
            "speed",
            "<NUMERICAL-VALUE-SPECIFICATION><SHORT-LABEL>Seven</SHORT-LABEL><VALUE>7</VALUE>"
            "</NUMERICAL-VALUE-SPECIFICATION>",
        )
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, "pSignals") == {}
        (line,) = _at(report, PROVIDED)
        assert (line.tag, line.effect) == ("SHORT-LABEL", "report")
        assert line.reason.startswith("1 SHORT-LABEL(s)")

    def test_a_hexadecimal_port_value_is_a_radix_line(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        source.init_value(
            PROVIDE, "speed", "<NUMERICAL-VALUE-SPECIFICATION><VALUE>0x0A</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, "pSignals") == {"speed": 10}
        assert f"{PROVIDED}: init read from 0x0A, written decimal" in report.init_radix_lines


# ---------------------------------------------------------------------------
# What is not carried: one line
# ---------------------------------------------------------------------------


class TestWhatIsNotCarried:
    def test_a_value_the_mapper_cannot_shape_is_an_f4_line_and_the_element_keeps_its_own(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        three = (
            "<ARRAY-VALUE-SPECIFICATION><ELEMENTS>"
            + "<NUMERICAL-VALUE-SPECIFICATION><VALUE>1</VALUE></NUMERICAL-VALUE-SPECIFICATION>" * 3
            + "</ELEMENTS></ARRAY-VALUE-SPECIFICATION>"
        )
        source.reference(REQUIRE, "bytes", source.add_constant("PortBytes", three))
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, "rSignals") == {}
        assert _element_inits(model_root)["bytes"] == [1, 2]
        (line,) = _at(report, REQUIRED)
        assert (line.tag, line.effect, line.category) == ("INIT-VALUE", "report", SkipCategory.NOT_SUPPORTED)
        assert line.reason == (
            "INIT-VALUE of a com-spec not carried: data element 'bytes': init: array type 'Bytes_T' declares "
            "length 2 but the constant carries 3 members"
        )

    def test_a_value_the_reader_could_not_read_is_an_f4_line_quoting_it(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        source.init_value(PROVIDE, "speed", _UNREAD_VALUE)
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, "pSignals") == {}
        assert _element_inits(model_root)["speed"] == 7
        (line,) = _at(report, PROVIDED)
        assert line.tag == "INIT-VALUE"
        assert "REFERENCE-VALUE-SPECIFICATION is not a value specification this reader reads" in line.reason

    def test_an_override_on_a_composition_s_port_is_an_f4_line(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        constant = source.add_constant(
            "PortSpeed", "<NUMERICAL-VALUE-SPECIFICATION><VALUE>8</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        composition = next(source.root.iter(f"{NS}COMPOSITION-SW-COMPONENT-TYPE"))
        composition.find(f"{NS}SHORT-NAME").addnext(
            _snippet(
                "<PORTS><P-PORT-PROTOTYPE><SHORT-NAME>pOut</SHORT-NAME><PROVIDED-COM-SPECS>"
                "<NONQUEUED-SENDER-COM-SPEC>"
                '<DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/PortInterfaces/ISignals/speed</DATA-ELEMENT-REF>'
                '<INIT-VALUE><CONSTANT-REFERENCE><CONSTANT-REF DEST="CONSTANT-SPECIFICATION">'
                f"{constant}</CONSTANT-REF></CONSTANT-REFERENCE></INIT-VALUE>"
                "</NONQUEUED-SENDER-COM-SPEC></PROVIDED-COM-SPECS>"
                '<PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/PortInterfaces/ISignals'
                "</PROVIDED-INTERFACE-TREF></P-PORT-PROTOTYPE></PORTS>"
            )
        )
        name = composition.findtext(f"{NS}SHORT-NAME")
        model_root, report = _import(tmp_path, source.save())

        (line,) = _at(report, f"/ComponentTypes/{name}/pOut/PROVIDED-COM-SPECS", "INIT-VALUE")
        assert line.reason == (
            "INIT-VALUE of a com-spec not carried: data element 'speed': a composition's port carries no "
            "com-spec values, and SWCraft writes the element's own"
        )
        assert _element_inits(model_root)["speed"] == 7

    def test_a_port_the_import_does_not_keep_gets_no_line_of_its_own(self, tmp_path: Path) -> None:
        """One cause, one line: the PR port's own entry covers its com-spec."""
        source = _Source(_generated(tmp_path))
        constant = source.add_constant(
            "PortSpeed", "<NUMERICAL-VALUE-SPECIFICATION><VALUE>8</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        source.reference(PROVIDE, "speed", constant)
        source.node(PROVIDE).tag = f"{NS}PR-PORT-PROTOTYPE"
        model_root, report = _import(tmp_path, source.save())

        assert [entry.tag for entry in _at(report, PROVIDE)] == ["PR-PORT-PROTOTYPE"]
        assert _at(report, PROVIDED) == []
        assert _element_inits(model_root)["speed"] == 7

    def test_a_port_on_an_external_interface_stub_carries_none_and_is_named(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        ports = source.node(f"/ComponentTypes/{COMPONENT}").find(f"{NS}PORTS")
        ports.append(
            _snippet(
                "<R-PORT-PROTOTYPE><SHORT-NAME>rRemote</SHORT-NAME><REQUIRED-COM-SPECS>"
                "<NONQUEUED-RECEIVER-COM-SPEC>"
                '<DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/Remote/IRemote/level</DATA-ELEMENT-REF>'
                '<INIT-VALUE><CONSTANT-REFERENCE><CONSTANT-REF DEST="CONSTANT-SPECIFICATION">'
                "/PortInterfaces/ISignals_speed_Init</CONSTANT-REF></CONSTANT-REFERENCE></INIT-VALUE>"
                "</NONQUEUED-RECEIVER-COM-SPEC></REQUIRED-COM-SPECS>"
                '<REQUIRED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Remote/IRemote</REQUIRED-INTERFACE-TREF>'
                "</R-PORT-PROTOTYPE>"
            )
        )
        model_root, report = _import(tmp_path, source.save())

        key = f"/ComponentTypes/{COMPONENT}/rRemote/REQUIRED-COM-SPECS"
        (line,) = _at(report, key, "INIT-VALUE")
        assert "external interface stub" in line.reason
        (swc,) = [swc for swc in read_swc_model(str(model_root)) if swc.name == COMPONENT]
        assert [port.com_specs for port in swc.ports if port.name == "rRemote"] == [[]]


# ---------------------------------------------------------------------------
# The merge verdict: a value written in place signs the port
# ---------------------------------------------------------------------------


def _signed_model(left: int, right: int) -> ImportModel:
    """Two copies of one component whose port's only difference is the value its com-spec writes in place."""
    model = ImportModel(source_path="inline")
    for tree, value in (("TreeA", left), ("TreeB", right)):
        port = _port(f"/Acme/{tree}/Components/Ctrl/pOut", "pOut", "P-PORT-PROTOTYPE", "/Acme/Interfaces/IMissing")
        port.com_spec_inits.append(
            PortComSpecInitInfo(
                port_path=port.path,
                com_spec_tag="NONQUEUED-SENDER-COM-SPEC",
                data_element_ref="/Acme/Interfaces/IMissing/level",
                constant_ref=None,
                block_tag="PROVIDED-COM-SPECS",
                value=ConstantScalar(value=value),
            )
        )
        model.swcs.append(_swc(f"/Acme/{tree}/Components/Ctrl", "Ctrl", [port]))
    return model


def test_two_ports_that_differ_only_by_a_value_in_place_stay_two_ports() -> None:
    differing = _map_all(_signed_model(1, 2))
    group = _group(differing["decisions"], "swc/APPLICATION-SW-COMPONENT-TYPE/Ctrl")
    assert (group.verdict, group.difference) == (CONFLICTING, "PORTS")

    equal = _map_all(_signed_model(1, 1))
    assert _group(equal["decisions"], "swc/APPLICATION-SW-COMPONENT-TYPE/Ctrl").verdict == MERGED


# ---------------------------------------------------------------------------
# import component
# ---------------------------------------------------------------------------


def _exported(tmp_path: Path, provide=None) -> Path:
    arxml = tmp_path / f"{COMPONENT}.arxml"
    export_component(str(_model_root(tmp_path, provide=provide)), COMPONENT, str(arxml))
    return arxml


def _component_entries(project: Path) -> dict[str, object]:
    return _entries(project / "model", "pSignals")


def test_import_component_carries_a_port_s_own_init(tmp_path: Path) -> None:
    arxml = _exported(tmp_path / "file", provide=PROVIDE_ENTRIES)
    project = init_project(tmp_path / "proj")

    report = import_component(str(arxml), str(project / "model"))

    assert report.swc_action == "created"
    assert _component_entries(project) == {entry["data_element"]: entry["init"] for entry in PROVIDE_ENTRIES}


def test_import_component_compares_a_port_s_own_init_as_it_stands(tmp_path: Path) -> None:
    project = init_project(tmp_path / "proj")
    plain = _exported(tmp_path / "plain")
    stated = _exported(tmp_path / "stated", provide=[{"data_element": "speed", "init": 9}])
    import_component(str(plain), str(project / "model"))

    assert import_component(str(plain), str(project / "model")).swc_action == "unchanged"
    with pytest.raises(ComponentImportConflictError):
        import_component(str(stated), str(project / "model"))
    assert _component_entries(project) == {}

    assert import_component(str(stated), str(project / "model"), update=True).swc_action == "updated"
    assert _component_entries(project) == {"speed": 9}
    assert import_component(str(stated), str(project / "model")).swc_action == "unchanged"


def test_import_component_names_a_port_init_it_cannot_carry_and_imports_past_it(tmp_path: Path) -> None:
    """A port's own value not carried is one descriptive line, and the component imports past it."""
    source = _Source(_exported(tmp_path / "file"))
    source.init_value(PROVIDE, "speed", "<TEXT-VALUE-SPECIFICATION><VALUE>fast</VALUE></TEXT-VALUE-SPECIFICATION>")
    arxml = source.save()
    project = init_project(tmp_path / "proj")

    report = import_component(str(arxml), str(project / "model"))

    assert report.swc_action == "created"
    assert sum("INIT-VALUE of a com-spec not carried" in line for line in report.not_carried_lines) == 1
    assert _component_entries(project) == {}


# ---------------------------------------------------------------------------
# The two unread-constant reasons, a second com-spec's value, a composition's label line
# ---------------------------------------------------------------------------


class TestReviewPhaseOne:
    def test_a_reference_to_no_constant_of_the_file_is_an_f4_line(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        source.reference(PROVIDE, "speed", "/PortInterfaces/InventedNoSuchConstant")
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, "pSignals") == {}
        assert _element_inits(model_root)["speed"] == 7
        (line,) = _at(report, PROVIDED, "INIT-VALUE")
        assert (line.effect, line.category) == ("report", SkipCategory.NOT_SUPPORTED)
        assert line.reason == (
            "INIT-VALUE of a com-spec not carried: data element 'speed': it references "
            "'/PortInterfaces/InventedNoSuchConstant', which is not a CONSTANT-SPECIFICATION in this file"
        )

    def test_a_reference_to_a_constant_with_no_readable_value_is_an_f4_line(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        constant = source.add_constant(
            "PortInventedEmpty", "<NUMERICAL-VALUE-SPECIFICATION></NUMERICAL-VALUE-SPECIFICATION>"
        )
        source.reference(PROVIDE, "speed", constant)
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, "pSignals") == {}
        assert _element_inits(model_root)["speed"] == 7
        (line,) = _at(report, PROVIDED, "INIT-VALUE")
        assert line.reason.startswith(
            "INIT-VALUE of a com-spec not carried: data element 'speed': it references "
            "'/PortInterfaces/PortInventedEmpty': "
        )
        assert "NUMERICAL-VALUE-SPECIFICATION carries no VALUE" in line.reason

    def test_a_second_com_spec_s_value_is_never_carried_on_the_first(self, tmp_path: Path) -> None:
        """Of two com-specs for one element, the first stating no ``INIT-VALUE``: nothing is carried.

        The second is reported whole; the first's position has no outcome (``registry.PortInitKey``).
        """
        source = _Source(_generated(tmp_path))
        constant = source.add_constant(
            "PortSpeed", "<NUMERICAL-VALUE-SPECIFICATION><VALUE>8</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        first = source.com_spec(REQUIRE, "speed")
        second = etree.fromstring(etree.tostring(first))
        second.find(f"{NS}INIT-VALUE").find(f".//{NS}CONSTANT-REF").text = constant
        first.remove(first.find(f"{NS}INIT-VALUE"))
        first.addnext(second)
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, "rSignals") == {}
        assert _element_inits(model_root)["speed"] == 7
        whole = [line for line in _at(report, REQUIRED, "NONQUEUED-RECEIVER-COM-SPEC")]
        assert len(whole) == 1 and "a second com-spec for one data element" in whole[0].reason

    def _composition_port(self, source: _Source, element: str, value_xml: str) -> str:
        composition = next(source.root.iter(f"{NS}COMPOSITION-SW-COMPONENT-TYPE"))
        composition.find(f"{NS}SHORT-NAME").addnext(
            _snippet(
                "<PORTS><P-PORT-PROTOTYPE><SHORT-NAME>pOut</SHORT-NAME><PROVIDED-COM-SPECS>"
                "<NONQUEUED-SENDER-COM-SPEC>"
                '<DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">'
                f"/PortInterfaces/ISignals/{element}</DATA-ELEMENT-REF>"
                f"<INIT-VALUE>{value_xml}</INIT-VALUE>"
                "</NONQUEUED-SENDER-COM-SPEC></PROVIDED-COM-SPECS>"
                '<PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/PortInterfaces/ISignals'
                "</PROVIDED-INTERFACE-TREF></P-PORT-PROTOTYPE></PORTS>"
            )
        )
        return f"/ComponentTypes/{composition.findtext(f'{NS}SHORT-NAME')}/pOut/PROVIDED-COM-SPECS"

    @pytest.mark.parametrize(("element", "label_lines"), [("plain", 0), ("speed", 1)])
    def test_a_composition_s_labelled_value_in_place_gets_a_label_line_only_against_an_element_init(
        self, tmp_path: Path, element: str, label_lines: int
    ) -> None:
        """A label line only where the element states an ``init`` the writer answers with.

        ``plain`` states none: the F4 line covers the whole value. ``speed`` states one: the comparison
        resolves the labels and the one label line is spent.
        """
        source = _Source(_generated(tmp_path))
        key = self._composition_port(
            source,
            element,
            "<NUMERICAL-VALUE-SPECIFICATION><SHORT-LABEL>InventedFour</SHORT-LABEL><VALUE>4</VALUE>"
            "</NUMERICAL-VALUE-SPECIFICATION>",
        )
        _model_root, report = _import(tmp_path, source.save())

        assert [line.tag for line in _at(report, key, "INIT-VALUE")] == ["INIT-VALUE"]
        assert len(_at(report, key, "SHORT-LABEL")) == label_lines


def _component_pair(*, symbols: tuple[str | None, str | None] = (None, None), options=((), ())) -> ImportModel:
    """Two copies of one component, identical but for a runnable's raw ``SYMBOL`` and the behaviour's options."""
    model = ImportModel(source_path="inline")
    for tree, symbol, tree_options in zip(("TreeA", "TreeB"), symbols, options, strict=True):
        path = f"/Acme/{tree}/Components/Ctrl"
        port = _port(f"{path}/pOut", "pOut", "P-PORT-PROTOTYPE", "/Acme/Interfaces/IMissing")
        swc = _swc(path, "Ctrl", [port])
        swc.runnables.append(
            SwcRunnableInfo(
                path=f"{path}/Behavior/Run", name="Run", min_start_interval=None, description="", symbol=symbol
            )
        )
        swc.port_api_options.extend(
            PortApiOptionInfo(
                container_path=f"{path}/Behavior/PORT-API-OPTIONS",
                port_ref=f"{path}/pOut",
                port_ref_dest="P-PORT-PROTOTYPE",
                referenced_kinds=("P-PORT-PROTOTYPE",),
                child_tags=child_tags,
                stated_attributes=(),
                enable_take_address=("false",),
                indirect_api=(indirect,),
            )
            for child_tags, indirect in tree_options
        )
        model.swcs.append(swc)
    return model


_PLAIN = (("PORT-REF", "ENABLE-TAKE-ADDRESS", "INDIRECT-API"), "false")


@pytest.mark.parametrize(
    ("model", "difference"),
    [
        (_component_pair(symbols=("Sym_Alpha", "Sym_Beta")), "RUNNABLES"),
        (_component_pair(symbols=("Sym_Alpha", None)), "RUNNABLES"),
        (_component_pair(options=((_PLAIN,), ((_PLAIN[0], "true"),))), "PORT-API-OPTIONS"),
        (
            _component_pair(options=((_PLAIN,), (((*_PLAIN[0], "PORT-ARG-VALUES"), "false"),))),
            "PORT-API-OPTIONS",
        ),
        (_component_pair(options=((_PLAIN,), ())), "PORT-API-OPTIONS"),
    ],
    ids=["two-symbols", "a-symbol-and-none", "indirect-api-true", "port-arg-values", "an-option-and-none"],
)
def test_two_declarations_differing_in_a_symbol_or_a_port_api_option_conflict(model, difference: str) -> None:
    """The merge verdict signs a runnable's ``SYMBOL`` and the port API options raw, so differing declarations conflict.

    The mapper judges a merged group's primary alone, so an unsigned difference would be lost with no line.
    """
    group = _group(_map_all(model)["decisions"], "swc/APPLICATION-SW-COMPONENT-TYPE/Ctrl")
    assert (group.verdict, group.difference) == (CONFLICTING, difference)


def test_two_declarations_equal_in_their_symbol_and_options_still_merge() -> None:
    model = _component_pair(symbols=("Sym_Alpha", "Sym_Alpha"), options=((_PLAIN,), (_PLAIN,)))
    assert _group(_map_all(model)["decisions"], "swc/APPLICATION-SW-COMPONENT-TYPE/Ctrl").verdict == MERGED


# ---------------------------------------------------------------------------
# An outcome is bound to the com-spec it was decided from
# ---------------------------------------------------------------------------


class TestAnOutcomeIsTheComSpecsOwn:
    """Of two com-specs for one element, the first gets its own outcome or none, never the second's.

    ``map_interfaces`` binds an outcome to the com-spec's position (``registry.PortInitKey``'s
    ordinal); the second is reported whole.
    """

    def _second(self, source: _Source, first_value: str | None, second_constant: str) -> None:
        first = source.com_spec(REQUIRE, "speed")
        second = etree.fromstring(etree.tostring(first))
        second.find(f"{NS}INIT-VALUE").find(f".//{NS}CONSTANT-REF").text = second_constant
        if first_value is not None:
            first.find(f"{NS}INIT-VALUE").find(f".//{NS}CONSTANT-REF").text = first_value
        first.addnext(second)

    def _constant(self, source: _Source, name: str, value: int) -> str:
        return source.add_constant(
            name, f"<NUMERICAL-VALUE-SPECIFICATION><VALUE>{value}</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )

    def _second_reported_whole(self, report) -> None:
        whole = _at(report, REQUIRED, "NONQUEUED-RECEIVER-COM-SPEC")
        assert len(whole) == 1 and "a second com-spec for one data element" in whole[0].reason

    def test_a_first_com_spec_naming_the_element_s_own_constant_carries_nothing(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        self._second(source, None, self._constant(source, "PortInventedEight", 8))
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, "rSignals") == {}
        assert _element_inits(model_root)["speed"] == 7
        assert _at(report, REQUIRED, "INIT-VALUE") == []
        self._second_reported_whole(report)

    def test_a_first_com_spec_s_own_value_is_carried_and_the_second_is_reported_whole(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        nine = self._constant(source, "PortInventedNine", 9)
        self._second(source, nine, self._constant(source, "PortInventedEight", 8))
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, "rSignals") == {"speed": 9}
        assert _element_inits(model_root)["speed"] == 7
        self._second_reported_whole(report)

    def test_a_second_com_spec_naming_the_element_s_own_constant_leaves_the_first_s_value(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        own = source.com_spec(REQUIRE, "speed").find(f".//{NS}CONSTANT-REF").text
        self._second(source, self._constant(source, "PortInventedNine", 9), own)
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, "rSignals") == {"speed": 9}
        self._second_reported_whole(report)


def _positioned_model(positions: tuple[int, int]) -> ImportModel:
    """Two copies of one component whose port's one value sits on the com-spec at *positions*' position in each.

    At position ``1`` it sits behind a first com-spec for the same element that states none.
    """
    model = ImportModel(source_path="inline")
    ref = "/Acme/Interfaces/IMissing/level"
    for tree, position in zip(("TreeA", "TreeB"), positions, strict=True):
        port = _port(f"/Acme/{tree}/Components/Ctrl/pOut", "pOut", "P-PORT-PROTOTYPE", "/Acme/Interfaces/IMissing")
        port.data_element_com_specs.extend(
            DataElementComSpecInfo(
                com_spec_tag="NONQUEUED-SENDER-COM-SPEC",
                data_element_ref=ref,
                block_tag="PROVIDED-COM-SPECS",
                position=index,
            )
            for index in range(position + 1)
        )
        port.com_spec_inits.append(
            PortComSpecInitInfo(
                port_path=port.path,
                com_spec_tag="NONQUEUED-SENDER-COM-SPEC",
                data_element_ref=ref,
                constant_ref=None,
                block_tag="PROVIDED-COM-SPECS",
                value=ConstantScalar(value=1),
                position=position,
            )
        )
        model.swcs.append(_swc(f"/Acme/{tree}/Components/Ctrl", "Ctrl", [port]))
    return model


def test_one_value_on_a_first_and_on_a_second_com_spec_signs_two_ports() -> None:
    """The merge verdict signs each init's place among its element's com-specs (``merge_verdict._com_spec_ordinal``).

    The mapper carries the first com-spec's value alone, so the two placements write two models.
    """
    group = _group(_map_all(_positioned_model((0, 1)))["decisions"], "swc/APPLICATION-SW-COMPONENT-TYPE/Ctrl")
    assert (group.verdict, group.difference) == (CONFLICTING, "PORTS")

    same = _group(_map_all(_positioned_model((1, 1)))["decisions"], "swc/APPLICATION-SW-COMPONENT-TYPE/Ctrl")
    assert same.verdict == MERGED


def test_import_component_names_a_port_value_s_lost_labels(tmp_path: Path) -> None:
    """``import component``, which runs no stub pass, records the ``SHORT-LABEL`` line as it stands."""
    source = _Source(_exported(tmp_path / "file"))
    source.init_value(
        PROVIDE,
        "speed",
        "<NUMERICAL-VALUE-SPECIFICATION><SHORT-LABEL>InventedNine</SHORT-LABEL><VALUE>9</VALUE>"
        "</NUMERICAL-VALUE-SPECIFICATION>",
    )
    project = init_project(tmp_path / "proj")

    report = import_component(str(source.save()), str(project / "model"))

    assert _component_entries(project) == {"speed": 9}
    assert (
        sum(
            "SHORT-LABEL(s) of the port's own init value for data element 'speed'" in line
            for line in report.not_carried_lines
        )
        == 1
    )


# ---------------------------------------------------------------------------
# An outcome is bound to the PORT it was decided from
# ---------------------------------------------------------------------------


class TestAnOutcomeIsThePortsOwn:
    """Of two same-named ports, the kept first gets its own com-spec's outcome or none, never the dropped one's.

    The two share a path, so the key carries the port's position among its owner's ``PORTS``
    (``registry.PortInitKey``).
    """

    def _eight(self, source: _Source) -> str:
        return source.add_constant(
            "PortInventedEight", "<NUMERICAL-VALUE-SPECIFICATION><VALUE>8</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )

    def _sibling(self, source: _Source, port_path: str, kind: str) -> etree._Element:
        """A copy of the port at *port_path*, of *kind*, placed right after it: same ``SHORT-NAME``, same blocks."""
        port = source.node(port_path)
        sibling = etree.fromstring(etree.tostring(port))
        sibling.tag = f"{NS}{kind}"
        port.addnext(sibling)
        return sibling

    @staticmethod
    def _speed_ref(port: etree._Element) -> etree._Element:
        spec = next(
            spec
            for spec in port.iter(f"{NS}NONQUEUED-SENDER-COM-SPEC", f"{NS}NONQUEUED-RECEIVER-COM-SPEC")
            if spec.findtext(f"{NS}DATA-ELEMENT-REF").endswith("/speed")
        )
        return spec.find(f".//{NS}CONSTANT-REF")

    @pytest.mark.parametrize(
        ("port_path", "kind"),
        [
            (REQUIRE, "R-PORT-PROTOTYPE"),
            (PROVIDE, "P-PORT-PROTOTYPE"),
            (PROVIDE, "R-PORT-PROTOTYPE"),
            (PROVIDE, "PR-PORT-PROTOTYPE"),
        ],
        ids=["r-beside-r", "p-beside-p", "r-beside-p", "pr-beside-p"],
    )
    def test_a_same_named_sibling_s_value_is_never_carried_on_the_kept_port(
        self, tmp_path: Path, port_path: str, kind: str
    ) -> None:
        """The sibling's value, at the same block position, is never carried on the kept port.

        An R sibling of a P port keeps the P port's ``PROVIDED-COM-SPECS``, which the reader reads on
        every port all the same.
        """
        source = _Source(_generated(tmp_path, provide=[]))
        port = port_path.rsplit("/", 1)[1]
        self._speed_ref(self._sibling(source, port_path, kind)).text = self._eight(source)
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, port) == {}
        assert _element_inits(model_root)["speed"] == 7
        block = REQUIRED if port_path == REQUIRE else PROVIDED
        assert _at(report, block, "INIT-VALUE") == []
        assert any("duplicate port name" in line.reason for line in _at(report, port_path, kind))

    def test_the_kept_port_keeps_its_own_value_beside_a_sibling_s(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        nine = source.add_constant(
            "PortInventedNine", "<NUMERICAL-VALUE-SPECIFICATION><VALUE>9</VALUE></NUMERICAL-VALUE-SPECIFICATION>"
        )
        self._speed_ref(self._sibling(source, REQUIRE, "R-PORT-PROTOTYPE")).text = self._eight(source)
        source.reference(REQUIRE, "speed", nine)
        model_root, _report = _import(tmp_path, source.save())

        assert _entries(model_root, "rSignals") == {"speed": 9}

    def test_a_composition_s_kept_port_never_names_a_sibling_s_value(self, tmp_path: Path) -> None:
        """A composition's F4 line is about the kept port's own com-spec only: the sibling takes no line there."""
        source = _Source(_generated(tmp_path))
        eight = self._eight(source)
        composition = next(source.root.iter(f"{NS}COMPOSITION-SW-COMPONENT-TYPE"))

        def port(constant: str) -> str:
            return (
                "<P-PORT-PROTOTYPE><SHORT-NAME>pOut</SHORT-NAME><PROVIDED-COM-SPECS><NONQUEUED-SENDER-COM-SPEC>"
                '<DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/PortInterfaces/ISignals/speed</DATA-ELEMENT-REF>'
                '<INIT-VALUE><CONSTANT-REFERENCE><CONSTANT-REF DEST="CONSTANT-SPECIFICATION">'
                f"{constant}</CONSTANT-REF></CONSTANT-REFERENCE></INIT-VALUE>"
                "</NONQUEUED-SENDER-COM-SPEC></PROVIDED-COM-SPECS>"
                '<PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/PortInterfaces/ISignals'
                "</PROVIDED-INTERFACE-TREF></P-PORT-PROTOTYPE>"
            )

        composition.find(f"{NS}SHORT-NAME").addnext(
            _snippet(f"<PORTS>{port('/PortInterfaces/ISignals_speed_Init')}{port(eight)}</PORTS>")
        )
        name = composition.findtext(f"{NS}SHORT-NAME")
        _model_root, report = _import(tmp_path, source.save())

        assert _at(report, f"/ComponentTypes/{name}/pOut/PROVIDED-COM-SPECS", "INIT-VALUE") == []
        assert any("duplicate port name" in line.reason for line in _at(report, f"/ComponentTypes/{name}/pOut"))

    def test_a_component_declared_twice_at_one_path_carries_neither_declaration_s_second_value(
        self, tmp_path: Path
    ) -> None:
        """The reader drops a second declaration of one path whole: its ports are never read."""
        source = _Source(_generated(tmp_path))
        component = source.node(f"/ComponentTypes/{COMPONENT}")
        twin = etree.fromstring(etree.tostring(component))
        component.addnext(twin)
        spec = next(
            spec
            for spec in twin.iter(f"{NS}NONQUEUED-RECEIVER-COM-SPEC")
            if spec.findtext(f"{NS}DATA-ELEMENT-REF").endswith("/speed")
        )
        spec.find(f".//{NS}CONSTANT-REF").text = self._eight(source)
        model_root, report = _import(tmp_path, source.save())

        assert _entries(model_root, "rSignals") == {}
        assert _at(report, REQUIRED, "INIT-VALUE") == []


def _sibling_ports_model(order: tuple[str, str]) -> ImportModel:
    """Two declarations of one component, each with two same-named ports, one value on the port *order* names."""
    model = ImportModel(source_path="inline")
    ref = "/Acme/Interfaces/IMissing/level"
    for tree, holder in zip(("TreeA", "TreeB"), order, strict=True):
        path = f"/Acme/{tree}/Components/Ctrl"
        ports = []
        for index, which in enumerate(("first", "second")):
            port = _port(f"{path}/pOut", "pOut", "P-PORT-PROTOTYPE", "/Acme/Interfaces/IMissing")
            port.position = index
            if which == holder:
                port.com_spec_inits.append(
                    PortComSpecInitInfo(
                        port_path=port.path,
                        com_spec_tag="NONQUEUED-SENDER-COM-SPEC",
                        data_element_ref=ref,
                        constant_ref=None,
                        block_tag="PROVIDED-COM-SPECS",
                        value=ConstantScalar(value=1),
                        port_position=index,
                    )
                )
            ports.append(port)
        model.swcs.append(_swc(path, "Ctrl", ports))
    return model


def test_a_value_on_the_first_and_on_the_second_of_two_same_named_ports_signs_two_components() -> None:
    """The merge verdict signs each same-named port in its place in ``PORTS``.

    Different places conflict, the same merges.
    The mapper keeps the first of two same-named ports, so the two placements write two models.
    """
    differing = _group(
        _map_all(_sibling_ports_model(("first", "second")))["decisions"], "swc/APPLICATION-SW-COMPONENT-TYPE/Ctrl"
    )
    assert (differing.verdict, differing.difference) == (CONFLICTING, "PORTS")

    same = _group(
        _map_all(_sibling_ports_model(("second", "second")))["decisions"], "swc/APPLICATION-SW-COMPONENT-TYPE/Ctrl"
    )
    assert same.verdict == MERGED


# ---------------------------------------------------------------------------
# A stated limit: an uncarried port value beside a kept element init
# ---------------------------------------------------------------------------

#: The fixture's root composition with one boundary port, so the writer itself writes that port's com-specs.
_ROOT_WITH_A_PORT = """name: Root
title: Root composition
root: true
ports:
  - name: pOut
    direction: provide
    interface: ISignals
members:
  - name: SignalsSwc
    type: SignalsSwc
delegations:
  - name: dOut
    port: pOut
    member: SignalsSwc
    member_port: pSignals
"""

_COMPOSITION_PROVIDED = "/ComponentTypes/Root/pOut/PROVIDED-COM-SPECS"


# ---------------------------------------------------------------------------
# The merge verdict signs the block a com-spec sits in, and every com-spec's shape
# ---------------------------------------------------------------------------

_SIGNED_KEY = "swc/APPLICATION-SW-COMPONENT-TYPE/Ctrl"
_SIGNED_REF = "/Acme/Interfaces/IMissing/level"


def _two_declarations(*declarations: Any) -> ImportModel:
    """Two copies of one component whose one provide port is shaped by each of *declarations* in turn.

    Each is a callable taking the port and filling its com-spec lists.
    """
    model = ImportModel(source_path="inline")
    for tree, declare in zip(("TreeA", "TreeB"), declarations, strict=True):
        port = _port(f"/Acme/{tree}/Components/Ctrl/pOut", "pOut", "P-PORT-PROTOTYPE", "/Acme/Interfaces/IMissing")
        declare(port)
        model.swcs.append(_swc(f"/Acme/{tree}/Components/Ctrl", "Ctrl", [port]))
    return model


def _sender_with(block: str, texts: dict[str, str] | None = None, *, init: bool = False):
    """A port's one sender com-spec for ``level`` in *block*, stating *texts* and, with *init*, a value of 1."""

    def declare(port) -> None:
        spec = DataElementComSpecInfo(
            com_spec_tag="NONQUEUED-SENDER-COM-SPEC",
            data_element_ref=_SIGNED_REF,
            texts={"DATA-ELEMENT-REF": _SIGNED_REF, **(texts or {})},
            block_tag=block,
        )
        port.data_element_com_specs.append(spec)
        port.com_spec_shapes.append(
            ComSpecInfo(
                block_tag=block,
                tag=spec.com_spec_tag,
                child_tags=tuple(spec.texts) + (("INIT-VALUE",) if init else ()),
                data=spec,
            )
        )
        if init:
            port.com_spec_inits.append(
                PortComSpecInitInfo(
                    port_path=port.path,
                    com_spec_tag=spec.com_spec_tag,
                    data_element_ref=_SIGNED_REF,
                    constant_ref=None,
                    block_tag=block,
                    value=ConstantScalar(value=1),
                )
            )

    return declare


class TestTheMergeVerdictSignsTheBlock:
    """The merge verdict signs the block a com-spec sits in.

    Declarations differing only in which block holds a com-spec are two components.
    A com-spec in the port's own block is carried, one in the other side's reported whole
    (``swc_mapping._gate_data_com_spec``).
    """

    @pytest.mark.parametrize(
        ("texts", "init"),
        [({"HANDLE-OUT-OF-RANGE": "SATURATE"}, False), (None, True)],
        ids=["attributes", "init-value"],
    )
    def test_one_com_spec_in_the_own_block_and_in_the_other_is_a_conflict(
        self, texts: dict[str, str] | None, init: bool
    ) -> None:
        model = _two_declarations(
            _sender_with("PROVIDED-COM-SPECS", texts, init=init), _sender_with("REQUIRED-COM-SPECS", texts, init=init)
        )
        group = _group(_map_all(model)["decisions"], _SIGNED_KEY)
        assert (group.verdict, group.difference) == (CONFLICTING, "PORTS")

    @pytest.mark.parametrize(
        ("texts", "init"),
        [({"HANDLE-OUT-OF-RANGE": "SATURATE"}, False), (None, True)],
        ids=["attributes", "init-value"],
    )
    def test_two_identical_declarations_still_merge(self, texts: dict[str, str] | None, init: bool) -> None:
        model = _two_declarations(
            _sender_with("PROVIDED-COM-SPECS", texts, init=init), _sender_with("PROVIDED-COM-SPECS", texts, init=init)
        )
        assert _group(_map_all(model)["decisions"], _SIGNED_KEY).verdict == MERGED

    def test_a_silent_value_and_no_value_still_merge(self) -> None:
        """A sender's ``USES-END-TO-END-PROTECTION false`` writes what no such child writes, so the two merge."""
        model = _two_declarations(
            _sender_with("PROVIDED-COM-SPECS", {"USES-END-TO-END-PROTECTION": "false"}),
            _sender_with("PROVIDED-COM-SPECS"),
        )
        assert _group(_map_all(model)["decisions"], _SIGNED_KEY).verdict == MERGED

    def test_a_silent_value_its_kind_has_no_place_for_conflicts(self) -> None:
        """A sender has no place for ``ENABLE-UPDATE``, so F4 names a stated one: two imports, a conflict."""
        model = _two_declarations(
            _sender_with("PROVIDED-COM-SPECS", {"ENABLE-UPDATE": "false"}), _sender_with("PROVIDED-COM-SPECS")
        )
        assert _group(_map_all(model)["decisions"], _SIGNED_KEY).verdict == CONFLICTING


class TestTheMergeVerdictSignsEveryComSpecShape:
    """Each fact F4 or the server queue-length block judges, stated by one declaration alone, signs two components."""

    def test_a_com_spec_stating_nothing_is_still_a_com_spec(self) -> None:
        """Its presence decides which com-spec is an element's first, carried or reported whole."""

        def first_empty(port) -> None:
            _sender_with("PROVIDED-COM-SPECS")(port)
            _sender_with("PROVIDED-COM-SPECS", {"HANDLE-OUT-OF-RANGE": "SATURATE"})(port)
            port.data_element_com_specs[1].position = 1

        model = _two_declarations(first_empty, _sender_with("PROVIDED-COM-SPECS", {"HANDLE-OUT-OF-RANGE": "SATURATE"}))
        assert _group(_map_all(model)["decisions"], _SIGNED_KEY).verdict == CONFLICTING

    def test_a_child_the_importer_does_not_read_is_signed_by_its_tag(self) -> None:
        def with_child(port) -> None:
            _sender_with("PROVIDED-COM-SPECS")(port)
            shape = port.com_spec_shapes[0]
            shape.child_tags = (*shape.child_tags, "TRANSMISSION-ACKNOWLEDGE")

        model = _two_declarations(with_child, _sender_with("PROVIDED-COM-SPECS"))
        assert _group(_map_all(model)["decisions"], _SIGNED_KEY).verdict == CONFLICTING

    def test_an_attribute_is_signed_by_its_name(self) -> None:
        def with_attribute(port) -> None:
            _sender_with("PROVIDED-COM-SPECS")(port)
            port.com_spec_shapes[0].attributes = ("UUID",)

        model = _two_declarations(with_attribute, _sender_with("PROVIDED-COM-SPECS"))
        assert _group(_map_all(model)["decisions"], _SIGNED_KEY).verdict == CONFLICTING

    @pytest.mark.parametrize(("other", "verdict"), [(None, CONFLICTING), ("1", MERGED), ("0x1", MERGED), ("2", MERGED)])
    def test_a_server_queue_length_is_signed_unless_it_is_the_writer_s(self, other: str | None, verdict: str) -> None:
        """``2`` against an absent length conflicts; ``1`` and ``0x1`` are the writer's 1 and sign as an absent one."""

        def server(length: str | None):
            def declare(port) -> None:
                info = ServerComSpecInfo(operation_ref="/Acme/Interfaces/IMissing/run", queue_length=length)
                port.server_com_specs.append(info)
                children = ("OPERATION-REF",) + (() if length is None else ("QUEUE-LENGTH",))
                port.com_spec_shapes.append(
                    ComSpecInfo(block_tag="PROVIDED-COM-SPECS", tag="SERVER-COM-SPEC", child_tags=children, server=info)
                )

            return declare

        first = "2" if other in (None, "2") else None
        model = _two_declarations(server(first), server(other))
        assert _group(_map_all(model)["decisions"], _SIGNED_KEY).verdict == verdict


# ---------------------------------------------------------------------------
# The merge verdict signs no order or spelling that nothing reads
# ---------------------------------------------------------------------------


def _sender_for(element: str, position: int, texts: dict[str, str] | None = None, **shape: Any):
    """A port's sender com-spec for *element* at *position* in its provided block, with *shape*'s extras."""

    def declare(port) -> None:
        ref = f"/Acme/Interfaces/IMissing/{element}"
        spec = DataElementComSpecInfo(
            com_spec_tag="NONQUEUED-SENDER-COM-SPEC",
            data_element_ref=ref,
            texts={"DATA-ELEMENT-REF": ref, **(texts or {})},
            block_tag="PROVIDED-COM-SPECS",
            position=position,
        )
        port.data_element_com_specs.append(spec)
        port.com_spec_shapes.append(
            ComSpecInfo(
                block_tag="PROVIDED-COM-SPECS",
                tag=spec.com_spec_tag,
                child_tags=(*spec.texts, *shape.get("children", ())),
                attributes=shape.get("attributes", ()),
                data=spec,
            )
        )

    return declare


def _server_for(operation: str, length: str | None):
    def declare(port) -> None:
        info = ServerComSpecInfo(operation_ref=f"/Acme/Interfaces/IMissing/{operation}", queue_length=length)
        port.server_com_specs.append(info)
        port.com_spec_shapes.append(
            ComSpecInfo(
                block_tag="PROVIDED-COM-SPECS",
                tag="SERVER-COM-SPEC",
                child_tags=("OPERATION-REF",) + (() if length is None else ("QUEUE-LENGTH",)),
                server=info,
            )
        )

    return declare


def _client_for(operation: str | None, interface: str = "IMissing"):
    """A port's ``CLIENT-COM-SPEC`` for *operation* of *interface* (``None``: no ``OPERATION-REF``)."""

    def declare(port) -> None:
        info = ClientComSpecInfo(
            operation_ref=None if operation is None else f"/Acme/Interfaces/{interface}/{operation}"
        )
        port.com_spec_shapes.append(
            ComSpecInfo(
                block_tag="REQUIRED-COM-SPECS",
                tag="CLIENT-COM-SPEC",
                child_tags=() if operation is None else ("OPERATION-REF",),
                client=info,
            )
        )

    return declare


def _in_turn(*declares):
    def declare(port) -> None:
        for each in declares:
            each(port)

    return declare


def _verdict(first, second) -> str:
    return _group(_map_all(_two_declarations(first, second))["decisions"], _SIGNED_KEY).verdict


class TestTheMergeVerdictSignsNoOrderNothingReads:
    """An order or spelling nothing reads is no difference: signing it would make identical declarations conflict."""

    def test_attributes_in_another_serialized_order_merge(self) -> None:
        assert (
            _verdict(
                _sender_for("level", 0, attributes=("UUID", "T")), _sender_for("level", 0, attributes=("T", "UUID"))
            )
            == MERGED
        )

    def test_unread_children_in_another_order_merge(self) -> None:
        assert (
            _verdict(
                _sender_for("level", 0, children=("TRANSMISSION-ACKNOWLEDGE", "VENDOR-X")),
                _sender_for("level", 0, children=("VENDOR-X", "TRANSMISSION-ACKNOWLEDGE")),
            )
            == MERGED
        )

    def test_one_more_unread_child_still_conflicts(self) -> None:
        assert (
            _verdict(
                _sender_for("level", 0, children=("VENDOR-X", "VENDOR-X")),
                _sender_for("level", 0, children=("VENDOR-X",)),
            )
            == CONFLICTING
        )

    @pytest.mark.parametrize(
        ("first", "second", "verdict"),
        [
            ("2", "0x2", MERGED),
            ("2", "02", MERGED),
            ("2", "3", CONFLICTING),
            ("two", "2", CONFLICTING),
            # A POSITIVE-INTEGER admits no sign (XSD R4.3.0:86393): "+2" is no spelling of two.
            ("2", "+2", CONFLICTING),
        ],
    )
    def test_a_server_queue_length_is_signed_by_what_it_reads_as(self, first: str, second: str, verdict: str) -> None:
        assert _verdict(_server_for("run", first), _server_for("run", second)) == verdict

    def test_server_com_specs_of_two_operations_in_another_order_merge(self) -> None:
        assert (
            _verdict(
                _in_turn(_server_for("run", "2"), _server_for("stop", None)),
                _in_turn(_server_for("stop", None), _server_for("run", "2")),
            )
            == MERGED
        )

    def test_a_com_spec_stating_nothing_in_another_place_merges(self) -> None:
        """An empty com-spec for one element before or after a valued one for another: one entry, one order."""
        saturate = {"HANDLE-OUT-OF-RANGE": "SATURATE"}
        assert (
            _verdict(
                _in_turn(_sender_for("level", 0), _sender_for("speed", 1, saturate)),
                _in_turn(_sender_for("speed", 0, saturate), _sender_for("level", 1)),
            )
            == MERGED
        )

    def test_two_valued_com_specs_in_another_order_still_conflict(self) -> None:
        """``com_specs`` keeps its entries in source order, so this order is carried and stays signed."""
        saturate = {"HANDLE-OUT-OF-RANGE": "SATURATE"}
        assert (
            _verdict(
                _in_turn(_sender_for("level", 0, saturate), _sender_for("speed", 1, saturate)),
                _in_turn(_sender_for("speed", 0, saturate), _sender_for("level", 1, saturate)),
            )
            == CONFLICTING
        )

    def test_an_unread_child_on_another_element_s_com_spec_conflicts(self) -> None:
        """Each shape names which com-spec it is, so the multiset cannot move a child from one to another."""
        assert (
            _verdict(
                _in_turn(_sender_for("level", 0, children=("VENDOR-X",)), _sender_for("speed", 1)),
                _in_turn(_sender_for("level", 0), _sender_for("speed", 1, children=("VENDOR-X",))),
            )
            == CONFLICTING
        )


class TestTheMergeVerdictSignsAClientComSpecByItsOperation:
    """The merge verdict signs a ``ClientComSpec`` by the operation it references.

    ``ClientComSpec.operation``, multiplicity 1 -- SWCT R4.3.0:8891-8894, Table 4.75;
    R4.2.2:8353-8356, Table 4.73. Signed as a ``SERVER-COM-SPEC`` is: the operation resolved,
    whether it sits under the port's own interface path, and how many client com-specs naming that
    operation precede it.
    """

    def test_two_client_com_specs_naming_another_operation_conflict(self) -> None:
        assert _verdict(_client_for("run"), _client_for("stop")) == CONFLICTING

    def test_one_client_com_spec_naming_the_same_operation_merges(self) -> None:
        assert _verdict(_client_for("run"), _client_for("run")) == MERGED

    def test_one_with_no_operation_beside_one_naming_an_operation_conflicts(self) -> None:
        assert _verdict(_client_for(None), _client_for("run")) == CONFLICTING

    def test_client_com_specs_of_two_operations_in_another_order_merge(self) -> None:
        """A multiset, as the server com-specs are: F4's lines carry no order."""
        assert (
            _verdict(
                _in_turn(_client_for("run"), _client_for("stop")),
                _in_turn(_client_for("stop"), _client_for("run")),
            )
            == MERGED
        )

    def test_a_client_and_a_server_naming_one_operation_are_counted_apart(self) -> None:
        """The client ordinal counts client com-specs alone, so a server com-spec beside it changes nothing."""
        assert (
            _verdict(
                _in_turn(_server_for("run", None), _client_for("run")),
                _in_turn(_client_for("run"), _server_for("run", None)),
            )
            == MERGED
        )

    def test_an_operation_outside_the_ports_own_interface_is_another_fact(self) -> None:
        assert _verdict(_client_for("run"), _client_for("run", interface="IOther")) == CONFLICTING

    def test_a_second_client_com_spec_for_one_operation_is_its_own_fact(self) -> None:
        """A second com-spec for one operation is reported whole by the mapper, so its ordinal is signed.

        The rule is [constr_1291] (SWCT R4.3.0:8059-8061; R4.2.2:7578-7581): twice ``run`` beside ``run`` and ``stop``
        are two components."""
        assert (
            _verdict(
                _in_turn(_client_for("run"), _client_for("run")),
                _in_turn(_client_for("run"), _client_for("stop")),
            )
            == CONFLICTING
        )


# ---------------------------------------------------------------------------
# A repeated read child is named and signed; order only where it is carried
# ---------------------------------------------------------------------------


def _repeat(spec: etree._Element, tag: str, text: str | None = None) -> None:
    """A second *tag* child of *spec*, right after the first, with *text* when given."""
    first = spec.find(f"{NS}{tag}")
    second = etree.fromstring(etree.tostring(first))
    if text is not None:
        second.text = text
    first.addnext(second)


def _not_read(report, block: str, tag: str) -> list[SkipEntry]:
    return [line for line in _at(report, block, tag) if "is not read by SWCraft" in line.reason]


class TestARepeatedReadChildIsNamed:
    """A repeated read child is an unread child with F4's line by its tag; the first is the one read.

    Neither licensed schema admits a second ``DATA-ELEMENT-REF``, ``INIT-VALUE`` or attribute child in
    a com-spec (each is ``maxOccurs="1"``).
    """

    @pytest.mark.parametrize(
        ("tag", "text"),
        [
            ("ENABLE-UPDATE", "true"),
            ("HANDLE-OUT-OF-RANGE", "SATURATE"),
            ("DATA-ELEMENT-REF", None),
            ("INIT-VALUE", None),
        ],
    )
    def test_import_model_names_the_repeat_and_keeps_the_first(
        self, tmp_path: Path, tag: str, text: str | None
    ) -> None:
        source = _Source(_generated(tmp_path))
        _repeat(source.com_spec(REQUIRE, "speed"), tag, text)
        model_root, report = _import(tmp_path, source.save())

        assert len(_not_read(report, REQUIRED, tag)) == 1
        assert _entries(model_root, "rSignals") == {}
        (swc,) = [swc for swc in read_swc_model(str(model_root)) if swc.name == COMPONENT]
        (port,) = [port for port in swc.ports if port.name == "rSignals"]
        assert port.com_specs == []

    def test_the_first_occurrence_is_the_one_carried(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        spec = source.com_spec(REQUIRE, "speed")
        spec.find(f"{NS}ENABLE-UPDATE").text = "true"
        _repeat(spec, "ENABLE-UPDATE", "false")
        model_root, report = _import(tmp_path, source.save())

        (swc,) = [swc for swc in read_swc_model(str(model_root)) if swc.name == COMPONENT]
        (port,) = [port for port in swc.ports if port.name == "rSignals"]
        assert [(entry.data_element, entry.enable_update) for entry in port.com_specs] == [("speed", True)]
        assert len(_not_read(report, REQUIRED, "ENABLE-UPDATE")) == 1

    def test_import_component_names_the_repeat(self, tmp_path: Path) -> None:
        """A descriptive line: printed, and the component imported past it."""
        source = _Source(_exported(tmp_path / "file"))
        _repeat(next(source.root.iter(f"{NS}NONQUEUED-RECEIVER-COM-SPEC")), "ENABLE-UPDATE", "true")
        project = init_project(tmp_path / "proj")

        report = import_component(str(source.save()), str(project / "model"))

        assert report.swc_action == "created"
        assert any(line.startswith("ENABLE-UPDATE ") for line in report.not_carried_lines)

    def test_a_silent_value_on_a_kind_with_no_place_for_it_is_named(self, tmp_path: Path) -> None:
        source = _Source(_generated(tmp_path))
        spec = source.com_spec(PROVIDE, "speed")
        spec.find(f"{NS}DATA-ELEMENT-REF").addnext(
            etree.fromstring(f'<ENABLE-UPDATE xmlns="{NAMESPACE}">false</ENABLE-UPDATE>')
        )
        _model_root, report = _import(tmp_path, source.save())

        (line,) = _at(report, PROVIDED, "ENABLE-UPDATE")
        assert "has no place for it" in line.reason


class TestTheReadSetCountsOccurrences:
    """``merge.unread_com_spec_children`` and ``merge.read_com_spec_children``.

    F4 and the merge verdict both read them.
    """

    @staticmethod
    def _shape(tag: str, children: tuple[str, ...], foreign: tuple[str, ...] = ()) -> ComSpecInfo:
        return ComSpecInfo(block_tag="PROVIDED-COM-SPECS", tag=tag, child_tags=children, foreign_child_tags=foreign)

    def test_a_repeat_of_a_read_child_is_unread_once_per_extra_occurrence(self) -> None:
        from swcraft_tools.arxml_import.merge import read_com_spec_children, unread_com_spec_children

        tag = "NONQUEUED-RECEIVER-COM-SPEC"
        shape = self._shape(tag, ("DATA-ELEMENT-REF", "ENABLE-UPDATE", "ENABLE-UPDATE", "ENABLE-UPDATE", "VENDOR-X"))
        assert sorted(unread_com_spec_children(shape, read_com_spec_children(tag))) == [
            "ENABLE-UPDATE",
            "ENABLE-UPDATE",
            "VENDOR-X",
        ]

    def test_a_same_named_child_outside_the_namespace_is_unread(self) -> None:
        from swcraft_tools.arxml_import.merge import read_com_spec_children, unread_com_spec_children

        tag = "NONQUEUED-RECEIVER-COM-SPEC"
        alone = self._shape(tag, ("DATA-ELEMENT-REF", "ENABLE-UPDATE"), foreign=("ENABLE-UPDATE",))
        beside = self._shape(tag, ("DATA-ELEMENT-REF", "ENABLE-UPDATE", "ENABLE-UPDATE"), foreign=("ENABLE-UPDATE",))
        read = read_com_spec_children(tag)
        assert unread_com_spec_children(alone, read) == ["ENABLE-UPDATE"]
        assert unread_com_spec_children(beside, read) == ["ENABLE-UPDATE"]

    @pytest.mark.parametrize(
        ("tag", "read"),
        [
            ("NONQUEUED-SENDER-COM-SPEC", True),
            ("NONQUEUED-RECEIVER-COM-SPEC", True),
            ("QUEUED-SENDER-COM-SPEC", False),
            ("QUEUED-RECEIVER-COM-SPEC", False),
        ],
    )
    def test_an_init_value_is_read_on_a_nonqueued_kind_alone(self, tag: str, read: bool) -> None:
        """The reader reads a port init on the two ``NONQUEUED-*`` kinds alone; on a queued kind it is unread."""
        from swcraft_tools.arxml_import.merge import read_com_spec_children, unread_com_spec_children

        shape = self._shape(tag, ("DATA-ELEMENT-REF", "INIT-VALUE"))
        assert unread_com_spec_children(shape, read_com_spec_children(tag)) == ([] if read else ["INIT-VALUE"])


def _composition_declarations(*declarations: Any) -> ImportModel:
    """Two copies of one composition whose one boundary provide port is shaped by each of *declarations*."""
    from test_arxml_import_merges import _root_over

    model = ImportModel(source_path="inline")
    for tree, declare in zip(("TreeA", "TreeB"), declarations, strict=True):
        port = _port(f"/Acme/{tree}/Comps/Cluster/pOut", "pOut", "P-PORT-PROTOTYPE", "/Acme/Interfaces/IMissing")
        declare(port)
        model.compositions.append(
            CompositionInfo(
                path=f"/Acme/{tree}/Comps/Cluster",
                name="Cluster",
                description="",
                ports=[port],
                prototypes=[],
                assembly_connectors=[],
                delegation_connectors=[],
            )
        )
    _root_over(model, "/Acme/TreeA/Comps/Cluster", "/Acme/TreeB/Comps/Cluster")
    return model


class TestTheMergeVerdictSignsARepeatAndOrdersOnlyWhatIsCarried:
    """The merge verdict signs a repeated read child.

    It orders only the com-specs that may write a ``com_specs`` entry.
    """

    def test_a_repeated_read_child_conflicts(self) -> None:
        assert (
            _verdict(
                _sender_for("level", 0, {"HANDLE-OUT-OF-RANGE": "SATURATE"}, children=("HANDLE-OUT-OF-RANGE",)),
                _sender_for("level", 0, {"HANDLE-OUT-OF-RANGE": "SATURATE"}),
            )
            == CONFLICTING
        )

    def test_two_report_only_com_specs_in_another_order_merge(self) -> None:
        """A sender has no place for ``ENABLE-UPDATE true``: neither writes an entry, F4 names both in either order."""
        on = {"ENABLE-UPDATE": "true"}
        assert (
            _verdict(
                _in_turn(_sender_for("level", 0, on), _sender_for("speed", 1, on)),
                _in_turn(_sender_for("speed", 0, on), _sender_for("level", 1, on)),
            )
            == MERGED
        )

    def test_a_carried_and_a_report_only_com_spec_in_another_order_merge(self) -> None:
        """One entry (``level``) either way, and one F4 line for ``speed``'s ``ENABLE-UPDATE`` either way."""
        saturate, on = {"HANDLE-OUT-OF-RANGE": "SATURATE"}, {"ENABLE-UPDATE": "true"}
        assert (
            _verdict(
                _in_turn(_sender_for("level", 0, saturate), _sender_for("speed", 1, on)),
                _in_turn(_sender_for("speed", 0, on), _sender_for("level", 1, saturate)),
            )
            == MERGED
        )

    def test_two_com_specs_carrying_an_init_in_another_order_still_conflict(self) -> None:
        def with_init(element: str, position: int):
            def declare(port) -> None:
                _sender_for(element, position)(port)
                port.com_spec_inits.append(
                    PortComSpecInitInfo(
                        port_path=port.path,
                        com_spec_tag="NONQUEUED-SENDER-COM-SPEC",
                        data_element_ref=f"/Acme/Interfaces/IMissing/{element}",
                        constant_ref=None,
                        block_tag="PROVIDED-COM-SPECS",
                        value=ConstantScalar(value=1),
                        position=position,
                    )
                )

            return declare

        assert (
            _verdict(
                _in_turn(with_init("level", 0), with_init("speed", 1)),
                _in_turn(with_init("speed", 0), with_init("level", 1)),
            )
            == CONFLICTING
        )

    def test_a_composition_port_s_carried_values_in_another_order_merge(self) -> None:
        """A composition's port carries no ``com_specs``: every value is an F4 line, in no order."""
        saturate = {"HANDLE-OUT-OF-RANGE": "SATURATE"}
        model = _composition_declarations(
            _in_turn(_sender_for("level", 0, saturate), _sender_for("speed", 1, saturate)),
            _in_turn(_sender_for("speed", 0, saturate), _sender_for("level", 1, saturate)),
        )
        group = _group(_map_all(model)["decisions"], "compositions/COMPOSITION-SW-COMPONENT-TYPE/Cluster")
        assert group.verdict == MERGED


# ---------------------------------------------------------------------------
# A FILTER on a com-spec kind with no place for it is an unread child
# ---------------------------------------------------------------------------

_ALWAYS = f'<FILTER xmlns="{NAMESPACE}"><DATA-FILTER-TYPE>ALWAYS</DATA-FILTER-TYPE></FILTER>'
_MASKED = (
    f'<FILTER xmlns="{NAMESPACE}"><DATA-FILTER-TYPE>MASKED-NEW-DIFFERS-X</DATA-FILTER-TYPE><MASK>1</MASK></FILTER>'
)


def _with_filter(source: _Source, port_path: str, filter_xml: str) -> None:
    source.com_spec(port_path, "speed").find(f"{NS}DATA-ELEMENT-REF").addnext(etree.fromstring(filter_xml))


class TestAFilterWithNoPlaceIsNamed:
    """A ``FILTER`` is one line, or none for a bare ``ALWAYS`` on a non-queued receiver.

    Both licensed schemas give ``FILTER`` to ``NONQUEUED-RECEIVER-COM-SPEC`` alone, and the writer writes
    none anywhere; on any other kind it is one unread child.
    """

    @pytest.mark.parametrize("filter_xml", [_ALWAYS, _MASKED], ids=["always", "masked"])
    def test_on_a_sender_any_filter_is_one_unread_child(self, tmp_path: Path, filter_xml: str) -> None:
        source = _Source(_generated(tmp_path))
        _with_filter(source, PROVIDE, filter_xml)
        _model_root, report = _import(tmp_path, source.save())

        (line,) = _at(report, PROVIDED, "FILTER")
        assert "is not read by SWCraft" in line.reason

    @pytest.mark.parametrize(("filter_xml", "lines"), [(_ALWAYS, 0), (_MASKED, 1)], ids=["always", "masked"])
    def test_on_a_non_queued_receiver_only_a_filter_other_than_always_is_named(
        self, tmp_path: Path, filter_xml: str, lines: int
    ) -> None:
        source = _Source(_generated(tmp_path))
        _with_filter(source, REQUIRE, filter_xml)
        _model_root, report = _import(tmp_path, source.save())

        found = _at(report, REQUIRED, "FILTER")
        assert len(found) == lines
        assert all("other than DATA-FILTER-TYPE ALWAYS" in line.reason for line in found)

    def test_import_component_names_a_sender_s_always_filter(self, tmp_path: Path) -> None:
        """A descriptive line: printed, and the component imported past it."""
        source = _Source(_exported(tmp_path / "file"))
        spec = next(source.root.iter(f"{NS}NONQUEUED-SENDER-COM-SPEC"))
        spec.find(f"{NS}DATA-ELEMENT-REF").addnext(etree.fromstring(_ALWAYS))
        project = init_project(tmp_path / "proj")

        report = import_component(str(source.save()), str(project / "model"))

        assert report.swc_action == "created"
        assert any(line.startswith("FILTER ") for line in report.not_carried_lines)


def _receiver_for(element: str, position: int, children: tuple[str, ...] = (), *, silent: bool = True):
    """A require port's non-queued receiver com-spec for *element*, with *children* beyond its reference."""

    def declare(port) -> None:
        ref = f"/Acme/Interfaces/IMissing/{element}"
        spec = DataElementComSpecInfo(
            com_spec_tag="NONQUEUED-RECEIVER-COM-SPEC",
            data_element_ref=ref,
            texts={"DATA-ELEMENT-REF": ref},
            filter_is_silent=silent,
            block_tag="REQUIRED-COM-SPECS",
            position=position,
        )
        port.data_element_com_specs.append(spec)
        port.com_spec_shapes.append(
            ComSpecInfo(
                block_tag="REQUIRED-COM-SPECS",
                tag=spec.com_spec_tag,
                child_tags=("DATA-ELEMENT-REF", *children),
                data=spec,
            )
        )

    return declare


def _require_verdict(first, second) -> str:
    model = ImportModel(source_path="inline")
    for tree, declare in zip(("TreeA", "TreeB"), (first, second), strict=True):
        port = _port(f"/Acme/{tree}/Components/Ctrl/rIn", "rIn", "R-PORT-PROTOTYPE", "/Acme/Interfaces/IMissing")
        declare(port)
        model.swcs.append(_swc(f"/Acme/{tree}/Components/Ctrl", "Ctrl", [port]))
    return _group(_map_all(model)["decisions"], _SIGNED_KEY).verdict


class TestTheMergeVerdictSignsAFilterWhereItIsNamed:
    def test_a_sender_s_always_filter_conflicts_with_none(self) -> None:
        assert _verdict(_sender_for("level", 0, children=("FILTER",)), _sender_for("level", 0)) == CONFLICTING

    def test_two_senders_filters_merge_whatever_they_hold(self) -> None:
        """One unread-child line each way, and no filter written: the filter's content is no difference."""

        def masked(port) -> None:
            _sender_for("level", 0, children=("FILTER",))(port)
            port.data_element_com_specs[0].filter_is_silent = False

        assert _verdict(masked, _sender_for("level", 0, children=("FILTER",))) == MERGED

    def test_a_non_queued_receiver_s_always_filter_merges_with_none(self) -> None:
        assert _require_verdict(_receiver_for("level", 0, ("FILTER",)), _receiver_for("level", 0)) == MERGED

    def test_a_non_queued_receiver_s_other_filter_conflicts_with_always(self) -> None:
        assert (
            _require_verdict(
                _receiver_for("level", 0, ("FILTER",), silent=False), _receiver_for("level", 0, ("FILTER",))
            )
            == CONFLICTING
        )


# -- what another module reads from this one --------------------------------------------------

#: The names of this module the closed half (run by ``dev.py test-internal``) imports: its API although most
#: start with ``_``. The test below fails in the open gate on a rename, so it is carried to the closed half.
CLOSED_HALF_READS = (
    "_ALWAYS",
    "_COMPOSITION_PROVIDED",
    "_ROOT_WITH_A_PORT",
    "PROVIDE",
    "PROVIDED",
    "REQUIRE",
    "REQUIRED",
    "_at",
    "_generated",
    "_import",
    "_model_root",
    "_Source",
    "_with_filter",
)


def test_every_name_the_closed_half_reads_is_here() -> None:
    missing = [name for name in CLOSED_HALF_READS if name not in globals()]
    assert missing == [], f"renamed or removed, and read by the closed half under test-internal: {missing}"
