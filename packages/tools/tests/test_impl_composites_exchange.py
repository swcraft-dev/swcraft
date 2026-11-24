"""Component exchange over the implementation record, the implementation array and the pointer.

* **the closure follows every new reference** — an implementation record's element
  types, an implementation array's ``element_type``, a pointer's ``target``, and a
  reference type aliasing any of the three — so a component whose ports name only the top
  of such a chain carries every link below it. A dangling name behind a new edge is a
  :class:`MissingReferenceError`, a name declared twice an
  :class:`AmbiguousReferenceError`, and a type stub behind one is the export's stub
  refusal — each as it already is behind a record field;
* **the import checks the names the three kinds emit** — an implementation record's
  element names and an implementation array's ``element_name`` are sub-element
  ``SHORT-NAME``\\ s;
* **a component reaching the new kinds exports** each implementation type exactly as the
  whole-model ``generate arxml`` emits it, every reference resolving inside the file;
* **and re-imports unchanged**, a second export and import agreeing with the first.

What it does **not** reach: every assertion is over the shapes written here —
example-based evidence, not a proof over every model; and the component is compared
through ``projected_swc``, since the file carries no memory budget, ``asil``, ``satisfies`` or package.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import model_element_path, write_model_element
from impl_composites_support import impl_composites_model_root
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.cli import main
from swcraft_tools.component_exchange import (
    AmbiguousReferenceError,
    ComponentExchangeError,
    MissingReferenceError,
    compute_closure,
    export_component,
    import_component,
)
from swcraft_tools.component_exchange.divergence import projected_swc
from swcraft_tools.component_exchange.name_policies import illegal_identifiers
from swcraft_tools.reader import read_data_types, read_interfaces, read_layer, read_swc_model
from swcraft_tools.schema import (
    ImplementationArrayDataType,
    ImplementationElement,
    ImplementationRecordDataType,
    PointerDataType,
    SWComponent,
)
from swcraft_tools.writer import generate_arxml

AR_NS = "{http://autosar.org/schema/r4.0}"


def _dump(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")


# The closure

#: A chain through every new edge. ``Outer`` holds an alias of a record and a
#: pointer to an array of that record; ``Node`` is a record holding a pointer to
#: its own type (the recursive pointer graph ``check-model`` accepts).
_CHAIN_TYPES: tuple[dict[str, Any], ...] = (
    {"name": "u8_base", "kind": "base_type", "size": 8, "encoding": "NONE"},
    {"name": "U8", "kind": "value_type", "base": "u8_base"},
    {"name": "Rec", "kind": "implementation_record", "elements": [{"name": "a", "type": "U8"}]},
    {"name": "Arr", "kind": "implementation_array", "element_type": "Rec", "length": 2, "element_name": "Item"},
    {"name": "Ptr", "kind": "pointer", "target": "Arr"},
    {"name": "RecRef", "kind": "reference_type", "base": "Rec"},
    {
        "name": "Outer",
        "kind": "implementation_record",
        "elements": [{"name": "r", "type": "RecRef"}, {"name": "p", "type": "Ptr"}],
    },
    {
        "name": "Node",
        "kind": "implementation_record",
        "elements": [{"name": "next", "type": "NodePtr"}, {"name": "v", "type": "U8"}],
    },
    {"name": "NodePtr", "kind": "pointer", "target": "Node"},
    {"name": "Unused_base", "kind": "base_type", "size": 8, "encoding": "NONE"},
)


def _chain_model(tmp_path: Path, top: str) -> Path:
    """A one-component project whose one data element is typed by *top* alone."""
    model_root = tmp_path / "model"
    for document in _CHAIN_TYPES:
        _dump(model_element_path(model_root, document["name"], kind=document["kind"]), document)
    _dump(
        model_root / "interfaces" / "IChain.yaml",
        {"name": "IChain", "kind": "sender_receiver", "data_elements": [{"name": "v", "implementation_type": top}]},
    )
    _dump(
        model_root / "swc" / "body" / "ChainSwc.yaml",
        {"name": "ChainSwc", "ports": [{"name": "pChain", "direction": "provide", "interface": "IChain"}]},
    )
    return model_root


class TestTheClosureFollowsEveryNewReference:
    """Each case types the data element by the top of one chain, so only the walk can collect what is below it."""

    @pytest.mark.parametrize(
        ("top", "expected"),
        [
            pytest.param("Rec", ["Rec", "U8", "u8_base"], id="implementation_record_element"),
            pytest.param("Arr", ["Arr", "Rec", "U8", "u8_base"], id="implementation_array_element_type"),
            pytest.param("Ptr", ["Arr", "Ptr", "Rec", "U8", "u8_base"], id="pointer_target"),
            pytest.param("RecRef", ["Rec", "RecRef", "U8", "u8_base"], id="reference_type_aliasing_a_composite"),
            pytest.param(
                "Outer", ["Arr", "Outer", "Ptr", "Rec", "RecRef", "U8", "u8_base"], id="all_four_edges_at_once"
            ),
            pytest.param("NodePtr", ["Node", "NodePtr", "U8", "u8_base"], id="a_pointer_graph_closing_on_itself"),
        ],
    )
    def test_every_link_below_the_named_type_is_collected(self, tmp_path: Path, top: str, expected: list[str]) -> None:
        closure = compute_closure(str(_chain_model(tmp_path, top)), "ChainSwc")

        assert [d.name for d in closure.data_types] == expected

    @pytest.mark.parametrize(
        ("removed", "top"),
        [
            pytest.param("U8", "Rec", id="behind_an_implementation_record_element"),
            pytest.param("Rec", "Arr", id="behind_an_implementation_array_element_type"),
            pytest.param("Arr", "Ptr", id="behind_a_pointer_target"),
            pytest.param("Rec", "RecRef", id="behind_a_reference_type_aliasing_a_composite"),
        ],
    )
    def test_an_undeclared_type_behind_a_new_edge_is_a_missing_reference(
        self, tmp_path: Path, removed: str, top: str
    ) -> None:
        model_root = _chain_model(tmp_path, top)
        model_element_path(model_root, removed, layer="implementation_types").unlink()

        with pytest.raises(MissingReferenceError, match=removed) as exc_info:
            compute_closure(str(model_root), "ChainSwc")

        assert exc_info.value.missing == frozenset({removed})

    def test_a_type_declared_twice_behind_a_pointer_is_ambiguous(self, tmp_path: Path) -> None:
        model_root = _chain_model(tmp_path, "Ptr")
        _dump(
            model_element_path(model_root, "Arr~2", kind="implementation_array"),
            {"name": "Arr", "kind": "implementation_array", "element_type": "U8", "length": 3, "element_name": "X"},
        )

        with pytest.raises(AmbiguousReferenceError, match="Arr"):
            compute_closure(str(model_root), "ChainSwc")

    def test_a_kept_map_pulls_in_its_implementation_composite_and_everything_below_it(self, tmp_path: Path) -> None:
        """A map's implementation side joins the closure, so a record reached only through a map is walked."""
        model_root = _chain_model(tmp_path, "U8")
        write_model_element(model_root, {"name": "Kph", "kind": "application_type"})
        write_model_element(model_root, {"name": "Pair", "kind": "record", "fields": [{"name": "left", "type": "Kph"}]})
        write_model_element(
            model_root,
            {
                "name": "Maps",
                "maps": [
                    {"application": "Kph", "implementation": "U8"},
                    {"application": "Pair", "implementation": "Outer"},
                ],
            },
            layer="mapping_sets",
        )
        _dump(
            model_root / "interfaces" / "IChain.yaml",
            {"name": "IChain", "kind": "sender_receiver", "data_elements": [{"name": "v", "application_type": "Pair"}]},
        )
        _dump(
            model_root / "swc" / "body" / "ChainSwc.yaml",
            {
                "name": "ChainSwc",
                "data_type_mappings": ["Maps"],
                "ports": [{"name": "pChain", "direction": "provide", "interface": "IChain"}],
            },
        )

        closure = compute_closure(str(model_root), "ChainSwc")

        assert [d.name for d in closure.data_types] == [
            "Arr",
            "Kph",
            "Outer",
            "Pair",
            "Ptr",
            "Rec",
            "RecRef",
            "U8",
            "u8_base",
        ]
        assert [(m.application, m.implementation) for m in closure.mapping_sets[0].maps] == [
            ("Kph", "U8"),
            ("Pair", "Outer"),
        ]


class TestAStubBehindANewEdgeIsRefused:
    """``_refuse_reached_stubs`` reads the closure, so a stub the walk now reaches is refused, nothing written."""

    @pytest.mark.parametrize(
        ("holder", "document"),
        [
            pytest.param(
                "Rec",
                {"name": "Rec", "kind": "implementation_record", "elements": [{"name": "a", "type": "ForeignT"}]},
                id="an_implementation_record_element",
            ),
            pytest.param(
                "Arr",
                {
                    "name": "Arr",
                    "kind": "implementation_array",
                    "element_type": "ForeignT",
                    "length": 2,
                    "element_name": "Item",
                },
                id="an_implementation_array_element_type",
            ),
            pytest.param("Ptr", {"name": "Ptr", "kind": "pointer", "target": "ForeignT"}, id="a_pointer_target"),
        ],
    )
    def test_the_export_names_the_stub_and_writes_nothing(
        self, tmp_path: Path, holder: str, document: dict[str, Any]
    ) -> None:
        model_root = _chain_model(tmp_path, holder)
        _dump(model_element_path(model_root, holder, kind=document["kind"]), document)
        write_model_element(
            model_root,
            {
                "name": "ForeignT",
                "kind": "external_type",
                "external_ref": "/Foreign/ImplementationDataTypes/ForeignT",
                "external_dest": "IMPLEMENTATION-DATA-TYPE",
            },
        )
        output = tmp_path / "out" / "ChainSwc.arxml"
        output.parent.mkdir()

        with pytest.raises(ComponentExchangeError, match="external type 'ForeignT'"):
            export_component(str(model_root), "ChainSwc", str(output))

        assert list(output.parent.iterdir()) == []


# The import's identifier check


class TestTheImportChecksTheNamesTheNewKindsEmit:
    """An implementation record's element names and an array's ``element_name`` are ``SHORT-NAME``\\ s."""

    @staticmethod
    def _swc() -> SWComponent:
        return SWComponent(name="TargetSwc", ports=[], runnables=[])

    def test_legal_element_names_report_nothing(self) -> None:
        data_types: list[Any] = [
            ImplementationRecordDataType(
                name="Rec",
                kind="implementation_record",
                elements=[ImplementationElement(name="front", type="U8")],
            ),
            ImplementationArrayDataType(
                name="Arr", kind="implementation_array", element_type="Rec", length=2, element_name="Item"
            ),
            PointerDataType(name="Ptr", kind="pointer", target="Arr"),
        ]

        assert illegal_identifiers(self._swc(), [], data_types) == []

    def test_an_illegal_implementation_record_element_name_is_reported(self) -> None:
        record = ImplementationRecordDataType(
            name="Rec",
            kind="implementation_record",
            elements=[ImplementationElement(name="front", type="U8"), ImplementationElement(name="re-ar", type="U8")],
        )

        violations = illegal_identifiers(self._swc(), [], [record])

        assert len(violations) == 1
        assert violations[0].startswith("Implementation record 'Rec' element name 're-ar' is not a legal AUTOSAR")

    def test_an_illegal_implementation_array_element_name_is_reported(self) -> None:
        array = ImplementationArrayDataType(
            name="Arr", kind="implementation_array", element_type="U8", length=2, element_name="1Item"
        )

        violations = illegal_identifiers(self._swc(), [], [array])

        assert len(violations) == 1
        assert violations[0].startswith("Implementation array 'Arr' element name '1Item' is not a legal AUTOSAR")

    def test_every_offender_is_reported_at_once(self) -> None:
        data_types: list[Any] = [
            ImplementationRecordDataType(
                name="Rec",
                kind="implementation_record",
                elements=[ImplementationElement(name="a b", type="U8"), ImplementationElement(name="c-d", type="U8")],
            ),
            ImplementationArrayDataType(
                name="Arr", kind="implementation_array", element_type="U8", length=2, element_name="_x"
            ),
            PointerDataType(name="Ptr", kind="pointer", target="Arr"),
        ]

        violations = illegal_identifiers(self._swc(), [], data_types)

        assert [line.split(" is not", 1)[0] for line in violations] == [
            "Implementation array 'Arr' element name '_x'",
            "Implementation record 'Rec' element name 'a b'",
            "Implementation record 'Rec' element name 'c-d'",
        ]


# The export of a component reaching the new kinds

#: ``ExampleSWC``'s closure over ``impl_composites_support``'s model, derived by
#: hand from its declarations, one reference at a time, and never read off an
#: export: ``IExample`` names ``float32`` (the baseline's element),
#: ``WheelSpeeds_T`` (-> ``uint16`` -> ``uint16_base``),
#: ``WheelSpeedHistory_T`` (-> ``WheelSpeeds_T``) and ``Frame_Ref`` (->
#: ``Frame_T`` -> ``WheelSpeeds_Ref`` -> ``WheelSpeeds_T``; ``uint8`` ->
#: ``uint8_base``); ``IDiagnosticService`` names ``WheelSpeeds_Ptr`` (->
#: ``WheelSpeeds_T``) and ``WheelSpeeds_PtrPtr`` (-> ``WheelSpeeds_Ptr``).
#: ``Handle_T`` and ``Spare_T`` are named by nothing the component speaks.
EXAMPLE_SWC_TYPES = [
    "Frame_Ref",
    "Frame_T",
    "WheelSpeedHistory_T",
    "WheelSpeeds_Ptr",
    "WheelSpeeds_PtrPtr",
    "WheelSpeeds_Ref",
    "WheelSpeeds_T",
    "float32",
    "float32_base",
    "uint16",
    "uint16_base",
    "uint8",
    "uint8_base",
]

#: The implementation types of the new kinds, and the reference types aliasing them, the export must carry.
NEW_KIND_TYPES = [
    "Frame_Ref",
    "Frame_T",
    "WheelSpeedHistory_T",
    "WheelSpeeds_Ptr",
    "WheelSpeeds_PtrPtr",
    "WheelSpeeds_Ref",
    "WheelSpeeds_T",
]


@pytest.fixture(scope="module")
def impl_model_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``impl_composites_support``'s model, built once and only read."""
    return impl_composites_model_root(tmp_path_factory.mktemp("impl-composites") / "project")


def _implementation_types(arxml: Path) -> dict[str, bytes]:
    """Every ``IMPLEMENTATION-DATA-TYPE`` in *arxml*, by ``SHORT-NAME``, canonicalized."""
    root = ET.parse(arxml).getroot()
    found: dict[str, bytes] = {}
    for element in root.iter(f"{AR_NS}IMPLEMENTATION-DATA-TYPE"):
        name = element.findtext(f"{AR_NS}SHORT-NAME")
        assert name is not None
        found[name] = ET.canonicalize(ET.tostring(element, encoding="unicode"), strip_text=True).encode("utf-8")
    return found


def _defined_paths(root: ET.Element) -> set[str]:
    """Every AUTOSAR path a ``SHORT-NAME`` defines in the document."""
    paths: set[str] = set()

    def walk(element: ET.Element, prefix: str) -> None:
        name = element.findtext(f"{AR_NS}SHORT-NAME")
        here = f"{prefix}/{name}" if name is not None else prefix
        if name is not None:
            paths.add(here)
        for child in element:
            walk(child, here)

    walk(root, "")
    return paths


class TestAComponentReachingTheNewKindsExports:
    def test_the_export_succeeds_with_the_whole_closure(self, impl_model_root: Path, tmp_path: Path) -> None:
        report = export_component(str(impl_model_root), "ExampleSWC", str(tmp_path / "ExampleSWC.arxml"))

        assert report.status == "ok"
        assert report.data_types == EXAMPLE_SWC_TYPES

    @pytest.mark.parametrize("version", list(ArxmlVersion), ids=lambda v: v.value)
    def test_each_implementation_type_is_emitted_as_the_whole_model_emits_it(
        self, impl_model_root: Path, tmp_path: Path, version: ArxmlVersion
    ) -> None:
        component = tmp_path / "ExampleSWC.arxml"
        whole = tmp_path / "model.arxml"
        export_component(str(impl_model_root), "ExampleSWC", str(component), version=version)
        generate_arxml(str(impl_model_root), str(whole), version=version)

        exported = _implementation_types(component)
        generated = _implementation_types(whole)

        assert set(NEW_KIND_TYPES) <= set(exported)
        assert "Handle_T" not in exported and "Spare_T" not in exported
        assert {name: exported[name] for name in exported} == {name: generated[name] for name in exported}

    def test_every_reference_in_the_export_resolves_inside_it(self, impl_model_root: Path, tmp_path: Path) -> None:
        component = tmp_path / "ExampleSWC.arxml"
        export_component(str(impl_model_root), "ExampleSWC", str(component))
        root = ET.parse(component).getroot()

        defined = _defined_paths(root)
        references = {
            (element.text or "").strip()
            for element in root.iter()
            if isinstance(element.tag, str) and element.tag.endswith(("-REF", "-TREF")) and element.get("DEST")
        }

        assert references, "the export carries references"
        assert sorted(references - defined) == []


# The round trip


def _both_type_layers(model_root: str) -> list[Any]:
    """Every element of the two data description layers (``base_types``, then ``implementation_types``)."""
    return [*read_layer(model_root, "base_types"), *read_data_types(model_root)]


def _init_project(tmp_path: Path, name: str) -> Path:
    """Scaffold a fresh project through the real ``swcraft init`` path."""
    target = tmp_path / name
    argv = sys.argv
    try:
        sys.argv = ["swcraft", "init", str(target)]
        main()
    finally:
        sys.argv = argv
    return target


class TestTheRoundTrip:
    def test_export_import_reexport_is_semantically_equivalent(self, impl_model_root: Path, tmp_path: Path) -> None:
        """Export, import, re-export, import again: every layer Pydantic-equal to the source.

        The component is compared through ``projected_swc``, which drops what no export carries (memory
        budget, ``asil``, ``satisfies``, package) and sorts the ports; ``description`` is carried and compared.
        """
        first = tmp_path / "ExampleSWC.arxml"
        export_component(str(impl_model_root), "ExampleSWC", str(first))
        project_a = _init_project(tmp_path, "projA")
        a_root = project_a / "model"
        import_component(str(first), str(a_root))

        source_types = {d.name: d for d in _both_type_layers(str(impl_model_root))}
        a_types = {d.name: d for d in _both_type_layers(str(a_root))}
        assert sorted(a_types) == EXAMPLE_SWC_TYPES
        assert all(a_types[n] == source_types[n] for n in EXAMPLE_SWC_TYPES)
        source_ifaces = {i.name: i for i in read_interfaces(str(impl_model_root))}
        a_ifaces = {i.name: i for i in read_interfaces(str(a_root))}
        source_swc = next(s for s in read_swc_model(str(impl_model_root)) if s.name == "ExampleSWC")
        # Every interface the component's ports speak came back, before each is compared.
        assert set(a_ifaces) == {port.interface for port in source_swc.ports}
        assert all(a_ifaces[n] == source_ifaces[n] for n in a_ifaces)
        (a_swc,) = read_swc_model(str(a_root))
        assert projected_swc(a_swc) == projected_swc(source_swc)

        second = tmp_path / "ExampleSWC.reexport.arxml"
        export_component(str(a_root), "ExampleSWC", str(second))
        project_b = _init_project(tmp_path, "projB")
        b_root = project_b / "model"
        import_component(str(second), str(b_root))
        assert {d.name: d for d in _both_type_layers(str(b_root))} == a_types
        assert {i.name: i for i in read_interfaces(str(b_root))} == a_ifaces
