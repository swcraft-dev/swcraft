"""The writer places every declaration and every reference from the model.

A root-level element that declares ``package`` is emitted in that sub-package of its root
(``/DataTypes/{package}/{name}``), its generated forms beside it, and every reference to it carries that path. The
writer reads placement and never computes it: ``arxml_generation._build_document`` builds one
``arxml_xml.PackagePlacement`` table before the first element is created; declarations ask it through
``ArxmlPackage.create_element`` and references through ``arxml_generation._placed_path``. This module holds:

* **the placed model** (one element of each root-level kind in a sub-package, two base types sharing one, the first
  created element a placed one) at both versions: each element and form at its placed path, no reference by the root
  path, ``ELEMENTS`` before ``AR-PACKAGES``, both containment checks passing (its XSD run is
  ``TestThePlacedModelConforms``);
* **the component export places identically** to the whole-model export;
* **a model with no ``package``** emits no ``AR-PACKAGES`` inside a root but the layer packages (its bytes are held
  by the goldens and the byte-identity records); **a document built with no table** puts everything in its roots;
* **the two containment checks**, each failing the way it should;
* **the reference oracle** (``reference_oracle.py``) over every fixture tree at both versions, and its non-vacuity.

What it does **not** reach: the case-folding claim in ``ArxmlPackage``, the name-space rule, and the importers
recording placement.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT, make_empty_model_root, model_element_storage_name, write_model_element
from external_stubs_support import put
from lxml import etree
from reference_oracle import path_index, reference_problems, references
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.component_exchange import export_component
from swcraft_tools.data_description import read_data_description
from swcraft_tools.reader import read_interfaces, read_swc_model
from swcraft_tools.writer import generate_arxml
from swcraft_tools.writer.arxml_generation import (
    _HOME,
    _build_document,
    _check_package_containment,
    _package_placement,
    _placed_path,
)
from swcraft_tools.writer.arxml_xml import ArxmlDocument, PackagePlacement, ar_qname

VERSIONS = [str(version) for version in ArxmlVersion]

FIXTURES = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures"

#: Every committed model tree ``generate arxml`` accepts. Refused on purpose:
#: ``editor_schema/accepted_but_invalid``, ``editor_schema/valid``,
#: ``recorded_oracles/impact_model``, and ``handle_termination_and_restart``, whose
#: ``SilentSwc`` is the absence fixture of
#: ``rule:swc.undeclared-termination-and-restart`` (its other components are covered
#: by ``test_handle_termination_and_restart.py`` and ``test_arxml_xsd_conformance.py``
#: through a copy without it).
FIXTURE_TREES: tuple[Path, ...] = (
    REPO_ROOT / "examples" / "basic" / "model",
    FIXTURES / "access_points" / "model",
    FIXTURES / "compatible_interfaces" / "model",
    FIXTURES / "component_exchange" / "model",
    FIXTURES / "compositions" / "model",
    FIXTURES / "diagrams" / "model",
    FIXTURES / "hardware" / "model",
    FIXTURES / "init_values" / "model",
    FIXTURES / "interface_compatibility" / "model",
    FIXTURES / "operation_events" / "model",
    FIXTURES / "safety" / "model",
    FIXTURES / "showcase" / "model",
)

_TREE_IDS = [tree.relative_to(REPO_ROOT).parent.as_posix() for tree in FIXTURE_TREES]


# ---------------------------------------------------------------------------
# The placed model
# ---------------------------------------------------------------------------

#: The data description layers' packages, each a home an element's ``package``
#: is a sub-package of.
BASE = "DataTypes/Base"
IMPLEMENTATION = "DataTypes/Implementation"
APPLICATION = "DataTypes/Application"
MAPPING_SETS = "DataTypes/MappingSets"

#: A data type's home by its ``kind``, as the placed model writes them.
HOME_OF_KIND = {
    "base_type": BASE,
    **dict.fromkeys(
        ("value_type", "reference_type", "implementation_record", "implementation_array", "pointer"), IMPLEMENTATION
    ),
    **dict.fromkeys(("application_type", "enum", "record", "array"), APPLICATION),
}

#: Every element the placed model puts in a sub-package, keyed by its home: one of
#: each root-level kind, the two base types sharing ``PlatformTypes``.
PLACED: dict[tuple[str, str], str] = {
    (BASE, "uint8_base"): "PlatformTypes",
    (BASE, "sint16_base"): "PlatformTypes",
    (IMPLEMENTATION, "sint16"): "Values",
    (IMPLEMENTATION, "u8_ref"): "Impl",
    (IMPLEMENTATION, "Frame"): "Impl",
    (IMPLEMENTATION, "Buf"): "Impl",
    (IMPLEMENTATION, "Ptr"): "Impl",
    (APPLICATION, "Mode"): "App",
    (APPLICATION, "Speed"): "App",
    (APPLICATION, "Pair"): "App",
    (APPLICATION, "Row"): "App",
    (MAPPING_SETS, "Maps"): "Mappings",
    ("PortInterfaces", "ISpeed"): "Sr",
    ("PortInterfaces", "IDiag"): "Cs",
    ("ComponentTypes", "Provider"): "Swcs",
    ("ComponentTypes", "Inner"): "Comps",
}

#: The generated forms of the placed elements, each beside its owner.
GENERATED: dict[tuple[str, str], str] = {
    (APPLICATION, "Mode_CompuMethod"): "App",
    (APPLICATION, "Speed_Unit"): "App",
    (APPLICATION, "Speed_Compu"): "App",
    (APPLICATION, "Speed_Range"): "App",
    ("PortInterfaces", "ISpeed_raw_Init"): "Sr",
    ("ComponentTypes", "Provider_Implementation"): "Swcs",
}

#: The root-level elements the placed model leaves in their home.
UNPLACED: tuple[tuple[str, str], ...] = (
    (IMPLEMENTATION, "uint8"),
    ("ComponentTypes", "Consumer"),
    ("ComponentTypes", "Consumer_Implementation"),
    ("ComponentTypes", "Root"),
)

#: Every reference kind the placed model emits at least once into a sub-package.
#: ``PORT-REF`` is absent: the writer writes no port API option.
REFERENCE_TAGS_INTO_A_SUB_PACKAGE = frozenset(
    {
        "TYPE-TREF",
        "BASE-TYPE-REF",
        "IMPLEMENTATION-DATA-TYPE-REF",
        "APPLICATION-DATA-TYPE-REF",
        "COMPU-METHOD-REF",
        "UNIT-REF",
        "DATA-CONSTR-REF",
        "PROVIDED-INTERFACE-TREF",
        "REQUIRED-INTERFACE-TREF",
        "DATA-ELEMENT-REF",
        "CONSTANT-REF",
        "OPERATION-REF",
        "TARGET-PROVIDED-OPERATION-REF",
        "TARGET-REQUIRED-OPERATION-REF",
        "TARGET-DATA-PROTOTYPE-REF",
        "CONTEXT-P-PORT-REF",
        "PORT-PROTOTYPE-REF",
        "BEHAVIOR-REF",
        "START-ON-EVENT-REF",
        "DATA-TYPE-MAPPING-REF",
        "CONTEXT-COMPONENT-REF",
        "TARGET-P-PORT-REF",
        "OUTER-PORT-REF",
    }
)


def _placed(document: dict[str, Any], key: tuple[str, str]) -> dict[str, Any]:
    package = PLACED.get(key)
    return document if package is None else {**document, "package": package}


def placed_model_root(base: Path) -> Path:
    """Write the placed model under *base* and return its model root.

    One element of each root-level kind in a sub-package (:data:`PLACED`); the base
    types, the first data types the writer creates, are placed, so the first element
    created in ``/DataTypes`` goes into a sub-package; ``uint8``, ``Consumer`` and
    ``Root`` stay in their roots, so a root's own elements and its sub-packages meet.
    """
    model_root = make_empty_model_root(base)
    data_types = [
        {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
        {"name": "sint16_base", "kind": "base_type", "size": 16, "encoding": "2C", "native_declaration": "int16_t"},
        {"name": "uint8", "kind": "value_type", "base": "uint8_base"},
        {"name": "sint16", "kind": "value_type", "base": "sint16_base"},
        {"name": "u8_ref", "kind": "reference_type", "base": "sint16"},
        {"name": "Frame", "kind": "implementation_record", "elements": [{"name": "x", "type": "sint16"}]},
        {"name": "Buf", "kind": "implementation_array", "element_type": "sint16", "length": 4, "element_name": "Item"},
        {"name": "Ptr", "kind": "pointer", "target": "sint16"},
        {"name": "Mode", "kind": "enum", "literals": [{"name": "OFF", "value": 0}, {"name": "ON", "value": 1}]},
        {
            "name": "Speed",
            "kind": "application_type",
            "unit": "km/h",
            "conversion": {"factor": 0.1, "offset": 0.0},
            "range": {"min": 0.0, "max": 250.0},
        },
        {
            "name": "Pair",
            "kind": "record",
            "fields": [{"name": "speed", "type": "Speed"}, {"name": "mode", "type": "Mode"}],
        },
        {"name": "Row", "kind": "array", "element_type": "Speed", "length": 4},
    ]
    for document in data_types:
        put(
            model_root,
            # The payload's kind picks the layer directory (a base type: data_types/base/).
            model_element_storage_name(document["name"], kind=document["kind"]),
            _placed(document, (HOME_OF_KIND[document["kind"]], document["name"])),
        )
    maps = {
        "name": "Maps",
        "maps": [
            {"application": "Speed", "implementation": "sint16"},
            {"application": "Mode", "implementation": "sint16"},
        ],
    }
    put(model_root, model_element_storage_name("Maps", layer="mapping_sets"), _placed(maps, (MAPPING_SETS, "Maps")))
    speed = {
        "name": "ISpeed",
        "kind": "sender_receiver",
        "data_elements": [
            {"name": "raw", "implementation_type": "uint8", "init": 0},
            {"name": "speed", "application_type": "Speed"},
            {"name": "frame", "implementation_type": "Frame"},
            {"name": "ref", "implementation_type": "u8_ref"},
        ],
    }
    put(model_root, "interfaces/ISpeed.yaml", _placed(speed, ("PortInterfaces", "ISpeed")))
    diag = {
        "name": "IDiag",
        "kind": "client_server",
        "operations": [
            {"name": "Read", "arguments": [{"name": "value", "direction": "out", "implementation_type": "sint16"}]}
        ],
    }
    put(model_root, "interfaces/IDiag.yaml", _placed(diag, ("PortInterfaces", "IDiag")))
    provider = {
        "name": "Provider",
        "ports": [
            {"name": "pOut", "direction": "provide", "interface": "ISpeed"},
            {"name": "pDiag", "direction": "provide", "interface": "IDiag"},
        ],
        "runnables": [
            {
                "name": "Tick",
                "events": [{"type": "timing", "period": 0.01}],
                "writes": [{"port": "pOut", "element": "raw"}],
            },
            {"name": "Serve", "events": [{"type": "operation", "port": "pDiag", "operation": "Read"}]},
        ],
        "data_type_mappings": ["Maps"],
        "handle_termination_and_restart": "no_support",
    }
    put(model_root, "swc/core/Provider.yaml", _placed(provider, ("ComponentTypes", "Provider")))
    consumer = {
        "name": "Consumer",
        "ports": [
            {"name": "rIn", "direction": "require", "interface": "ISpeed"},
            {"name": "rDiag", "direction": "require", "interface": "IDiag"},
        ],
        "runnables": [
            {
                "name": "Run",
                "events": [{"type": "timing", "period": 0.01}],
                "reads": [{"port": "rIn", "element": "raw"}],
                "calls": [{"port": "rDiag", "operation": "Read"}],
            }
        ],
        "data_type_mappings": ["Maps"],
        "handle_termination_and_restart": "no_support",
    }
    put(model_root, "swc/core/Consumer.yaml", consumer)
    inner = {
        "name": "Inner",
        "title": "The placed composition",
        "ports": [{"name": "pOut", "direction": "provide", "interface": "ISpeed"}],
        "members": [{"name": "prov", "type": "Provider"}],
        "delegations": [{"name": "Inner_pOut_prov", "port": "pOut", "member": "prov", "member_port": "pOut"}],
    }
    put(model_root, "compositions/Inner.yaml", _placed(inner, ("ComponentTypes", "Inner")))
    root = {
        "name": "Root",
        "title": "The root, left in its root package",
        "root": True,
        "members": [{"name": "inner", "composition": "Inner"}, {"name": "cons", "type": "Consumer"}],
    }
    put(model_root, "compositions/Root.yaml", root)
    link = {
        "name": "link",
        "from": {"composition": "Inner", "port": "pOut"},
        "to": {"swc": "Consumer", "port": "rIn"},
    }
    put(model_root, "connectors/link.yaml", link)
    return model_root


def _generate(model_root: Path, tmp_path: Path, version: str, name: str = "model") -> Path:
    output = tmp_path / f"{name}-{version}.arxml"
    generate_arxml(str(model_root), str(output), version=version)
    return output


def _external_refs(model_root: Path) -> frozenset[str]:
    """The ``external_ref`` of every stub *model_root* declares, read by the reader."""
    declared = [
        *read_swc_model(str(model_root)),
        *read_interfaces(str(model_root)),
        *read_data_description(str(model_root)).data_types,
    ]
    return frozenset(ref for ref in (getattr(element, "external_ref", None) for element in declared) if ref)


@pytest.fixture(scope="module")
def placed_outputs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("placed")
    model_root = placed_model_root(base)
    return {version: _generate(model_root, base, version) for version in VERSIONS}


@pytest.mark.parametrize("version", VERSIONS)
class TestThePlacedModel:
    def test_each_placed_element_is_in_its_sub_package(self, placed_outputs: dict[str, Path], version: str) -> None:
        index = path_index(placed_outputs[version])
        for (root, name), package in PLACED.items():
            assert f"/{root}/{package}/{name}" in index, (root, package, name)
            assert f"/{root}/{name}" not in index, (root, name)

    def test_each_generated_form_sits_beside_its_owner(self, placed_outputs: dict[str, Path], version: str) -> None:
        index = path_index(placed_outputs[version])
        for (root, name), package in GENERATED.items():
            assert f"/{root}/{package}/{name}" in index, (root, package, name)
            assert f"/{root}/{name}" not in index, (root, name)

    def test_the_behaviour_follows_its_component(self, placed_outputs: dict[str, Path], version: str) -> None:
        index = path_index(placed_outputs[version])
        assert index["/ComponentTypes/Swcs/Provider/Provider_InternalBehavior"] == "SWC-INTERNAL-BEHAVIOR"

    def test_the_unplaced_elements_stay_in_their_root(self, placed_outputs: dict[str, Path], version: str) -> None:
        index = path_index(placed_outputs[version])
        for root, name in UNPLACED:
            assert f"/{root}/{name}" in index, (root, name)

    def test_two_elements_share_one_sub_package(self, placed_outputs: dict[str, Path], version: str) -> None:
        tree = etree.parse(str(placed_outputs[version]))
        packages = [
            node
            for node in tree.iter(ar_qname("AR-PACKAGE"))
            if node.findtext(ar_qname("SHORT-NAME")) == "PlatformTypes"
        ]
        assert len(packages) == 1
        names = [node.findtext(ar_qname("SHORT-NAME")) for node in packages[0].find(ar_qname("ELEMENTS"))]
        assert sorted(names) == ["sint16_base", "uint8_base"]

    def test_every_reference_resolves(self, placed_outputs: dict[str, Path], version: str) -> None:
        assert reference_problems(placed_outputs[version]) == []

    def test_no_reference_names_a_placed_element_by_its_root_path(
        self, placed_outputs: dict[str, Path], version: str
    ) -> None:
        stale = {f"/{root}/{name}" for root, name in {**PLACED, **GENERATED}}
        offenders = [
            reference
            for reference in references(placed_outputs[version])
            if any(reference.path == path or reference.path.startswith(f"{path}/") for path in stale)
        ]
        assert offenders == []

    def test_every_reference_kind_reaches_a_sub_package(self, placed_outputs: dict[str, Path], version: str) -> None:
        placed = {f"/{root}/{package}/" for (root, _name), package in {**PLACED, **GENERATED}.items()}
        reached = {
            reference.tag
            for reference in references(placed_outputs[version])
            if any(reference.path.startswith(prefix) for prefix in placed)
        }
        assert REFERENCE_TAGS_INTO_A_SUB_PACKAGE - reached == set()

    def test_the_pointer_target_is_placed(self, placed_outputs: dict[str, Path], version: str) -> None:
        tree = etree.parse(str(placed_outputs[version]))
        (props,) = list(tree.iter(ar_qname("SW-POINTER-TARGET-PROPS")))
        (target,) = list(props.iter(ar_qname("IMPLEMENTATION-DATA-TYPE-REF")))
        assert target.text == "/DataTypes/Implementation/Values/sint16"

    def test_elements_precede_ar_packages_in_every_home(self, placed_outputs: dict[str, Path], version: str) -> None:
        """``ELEMENTS`` precedes ``AR-PACKAGES`` in every home, as the schema's sequence requires.

        The first element created in ``/DataTypes/Implementation`` is a placed
        value type (``sint16``, read before ``uint8``), and the layer package's own
        ``ELEMENTS`` still comes first. A home whose every element is placed
        (``PortInterfaces``, ``Base``) holds no ``ELEMENTS``, and neither does
        ``/DataTypes``."""
        tree = etree.parse(str(placed_outputs[version]))
        roots = tree.getroot().find(ar_qname("AR-PACKAGES"))

        def layouts(parent: etree._Element) -> dict[str, list[str]]:
            return {
                package.findtext(ar_qname("SHORT-NAME")): [etree.QName(child).localname for child in package]
                for package in parent.findall(ar_qname("AR-PACKAGE"))
            }

        assert layouts(roots) == {
            "DataTypes": ["SHORT-NAME", "AR-PACKAGES"],
            "PortInterfaces": ["SHORT-NAME", "AR-PACKAGES"],
            "ComponentTypes": ["SHORT-NAME", "ELEMENTS", "AR-PACKAGES"],
        }
        data_types = roots.find(ar_qname("AR-PACKAGE"))
        assert layouts(data_types.find(ar_qname("AR-PACKAGES"))) == {
            "Base": ["SHORT-NAME", "AR-PACKAGES"],
            "Implementation": ["SHORT-NAME", "ELEMENTS", "AR-PACKAGES"],
            "Application": ["SHORT-NAME", "AR-PACKAGES"],
            "MappingSets": ["SHORT-NAME", "AR-PACKAGES"],
        }
        implementation = data_types.find(ar_qname("AR-PACKAGES")).findall(ar_qname("AR-PACKAGE"))[1]
        first = implementation.find(ar_qname("AR-PACKAGES")).find(ar_qname("AR-PACKAGE"))
        assert first.findtext(ar_qname("SHORT-NAME")) == "Values"

    def test_both_containment_checks_pass(self, tmp_path: Path, version: str) -> None:
        document = _build_document(str(placed_model_root(tmp_path)), version)
        _check_package_containment(document)
        claims = {
            (package.key, name): placed
            for package in document.placed_packages()
            for placed, name in package.element_claims
        }
        assert {key: claims[key] for key in {**PLACED, **GENERATED}} == {**PLACED, **GENERATED}
        assert {package.key: package.sub_package_claims for package in document.placed_packages()} == {
            "DataTypes": (),
            BASE: ("PlatformTypes",),
            IMPLEMENTATION: ("Values", "Impl"),
            APPLICATION: ("App",),
            MAPPING_SETS: ("Mappings",),
            "PortInterfaces": ("Cs", "Sr"),
            "ComponentTypes": ("Swcs", "Comps"),
        }
        assert document.data_types.layer_package_claims == ("Base", "Implementation", "Application", "MappingSets")


def layered_model_root(base: Path) -> Path:
    """The placed model with every ``package`` removed: each element in its home itself."""
    model_root = placed_model_root(base)
    for path in sorted(model_root.rglob("*.yaml"), key=lambda path: path.as_posix()):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(document, dict) and document.pop("package", None) is not None:
            put(model_root, path.relative_to(model_root).as_posix(), document)
    return model_root


#: Each data description kind's path when nothing is placed: its layer's package, a
#: private form beside the type it is generated for.
LAYERED_PATHS: dict[str, str] = {
    "/DataTypes/Base/uint8_base": "SW-BASE-TYPE",
    "/DataTypes/Base/sint16_base": "SW-BASE-TYPE",
    "/DataTypes/Implementation/uint8": "IMPLEMENTATION-DATA-TYPE",
    "/DataTypes/Implementation/sint16": "IMPLEMENTATION-DATA-TYPE",
    "/DataTypes/Implementation/u8_ref": "IMPLEMENTATION-DATA-TYPE",
    "/DataTypes/Implementation/Frame": "IMPLEMENTATION-DATA-TYPE",
    "/DataTypes/Implementation/Buf": "IMPLEMENTATION-DATA-TYPE",
    "/DataTypes/Implementation/Ptr": "IMPLEMENTATION-DATA-TYPE",
    "/DataTypes/Application/Mode": "APPLICATION-PRIMITIVE-DATA-TYPE",
    "/DataTypes/Application/Mode_CompuMethod": "COMPU-METHOD",
    "/DataTypes/Application/Speed": "APPLICATION-PRIMITIVE-DATA-TYPE",
    "/DataTypes/Application/Speed_Unit": "UNIT",
    "/DataTypes/Application/Speed_Compu": "COMPU-METHOD",
    "/DataTypes/Application/Speed_Range": "DATA-CONSTR",
    "/DataTypes/Application/Pair": "APPLICATION-RECORD-DATA-TYPE",
    "/DataTypes/Application/Row": "APPLICATION-ARRAY-DATA-TYPE",
    "/DataTypes/MappingSets/Maps": "DATA-TYPE-MAPPING-SET",
}


@pytest.mark.parametrize("version", VERSIONS)
class TestTheLayerPackages:
    """Each data description layer's elements are in its own package."""

    def test_each_kind_is_in_its_layers_package(self, version: str, tmp_path: Path) -> None:
        index = path_index(_generate(layered_model_root(tmp_path), tmp_path, version))
        assert {path: index.get(path) for path in LAYERED_PATHS} == LAYERED_PATHS
        assert not [
            path
            for path in index
            if path.count("/") == 2 and path.startswith("/DataTypes/") and index[path] != "AR-PACKAGE"
        ]

    def test_a_bounded_value_types_range_is_beside_it(self, version: str, tmp_path: Path) -> None:
        model_root = layered_model_root(tmp_path)
        write_model_element(
            model_root, {"name": "sint16", "kind": "value_type", "base": "sint16_base", "value_min": -5, "value_max": 5}
        )
        output = _generate(model_root, tmp_path, version)
        index = path_index(output)
        assert index["/DataTypes/Implementation/sint16_Range"] == "DATA-CONSTR"
        refs = [r.path for r in references(output) if r.tag == "DATA-CONSTR-REF"]
        assert "/DataTypes/Implementation/sint16_Range" in refs
        assert reference_problems(output) == []

    def test_every_reference_names_the_layer_package(self, version: str, tmp_path: Path) -> None:
        output = _generate(layered_model_root(tmp_path), tmp_path, version)
        into_data_types = [r.path for r in references(output) if r.path.startswith("/DataTypes/")]
        assert into_data_types
        assert all(path.split("/")[2] in LAYER_PACKAGES for path in into_data_types)
        assert reference_problems(output) == []

    def test_the_packages_appear_in_the_passes_order_and_hold_no_own_elements(
        self, version: str, tmp_path: Path
    ) -> None:
        tree = etree.parse(str(_generate(layered_model_root(tmp_path), tmp_path, version)))
        data_types = tree.getroot().find(ar_qname("AR-PACKAGES")).find(ar_qname("AR-PACKAGE"))
        assert [etree.QName(child).localname for child in data_types] == ["SHORT-NAME", "AR-PACKAGES"]
        names = [node.findtext(ar_qname("SHORT-NAME")) for node in data_types.find(ar_qname("AR-PACKAGES"))]
        assert names == list(LAYER_PACKAGES)

    def test_a_layer_with_no_element_emits_no_package(self, version: str, tmp_path: Path) -> None:
        model_root = make_empty_model_root(tmp_path)
        write_model_element(
            model_root,
            {"name": "u8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
        )
        write_model_element(model_root, {"name": "u8", "kind": "value_type", "base": "u8_base"})
        nested = _nested_packages(_generate(model_root, tmp_path, version))
        assert nested == ["DataTypes/Base", "DataTypes/Implementation"]


#: The raw SHA-256 of the two committed trees that declare no data type, 4.2.2 then
#: 4.3.0: the bytes such a model emitted before the layer packages existed, which
#: they must not change.
NO_DATA_TYPE_BRANCH_POINT: dict[str, tuple[str, str]] = {
    "packages/tools/src/swcraft_tools/templates/vehicle-architecture/model": (
        "6a1f6039722ae770e555200f3a039c93aa7b907114d8ae160c1f09b9209b48c3",
        "61882ec245289922d51f404acfd2c14bd65445bc5a50a242e1b08b042834f3e7",
    ),
    "packages/tools/tests/fixtures/hardware/model": (
        "e5651677508946662f6cb5cce4307eb3b1f4315a069d3e4fa44eeb1faa1e5b3f",
        "f72b393720b12d12c26e8e764025234798a1e6661662934511756bf555030d7d",
    ),
}


class TestAModelWithNoDataTypeKeepsItsBytes:
    """No data type, no layer package: such a tree's output keeps its pinned bytes, unstripped."""

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize("tree", sorted(NO_DATA_TYPE_BRANCH_POINT))
    def test_its_raw_output_is_the_branch_points(self, tree: str, version: str, tmp_path: Path) -> None:
        import hashlib

        # Recursive: a base type's file lies in `data_types/base/`.
        assert not list((REPO_ROOT / tree / "data_types").rglob("*.yaml"))
        output = _generate(REPO_ROOT / tree, tmp_path, version)
        expected = NO_DATA_TYPE_BRANCH_POINT[tree][VERSIONS.index(version)]
        assert hashlib.sha256(output.read_bytes()).hexdigest() == expected


class TestTheComponentExportPlacesIdentically:
    """Placement is model data and the closure carries the field, so the component export places alike."""

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize("component", ["Provider", "Consumer"])
    def test_every_path_it_declares_is_the_whole_models(self, component: str, version: str, tmp_path: Path) -> None:
        model_root = placed_model_root(tmp_path)
        whole = path_index(_generate(model_root, tmp_path, version))
        exported = tmp_path / f"{component}.arxml"
        export_component(str(model_root), component, str(exported), version=version)
        declared = path_index(exported)
        assert {path: whole.get(path) for path in declared} == declared
        assert reference_problems(exported) == []

    def test_the_placed_component_is_exported_at_its_placed_path(self, tmp_path: Path) -> None:
        model_root = placed_model_root(tmp_path)
        exported = tmp_path / "Provider.arxml"
        export_component(str(model_root), "Provider", str(exported))
        index = path_index(exported)
        assert index["/ComponentTypes/Swcs/Provider"] == "APPLICATION-SW-COMPONENT-TYPE"
        assert index["/PortInterfaces/Sr/ISpeed"] == "SENDER-RECEIVER-INTERFACE"
        assert index["/DataTypes/MappingSets/Mappings/Maps"] == "DATA-TYPE-MAPPING-SET"


# ---------------------------------------------------------------------------
# A model with no package, and a document with no table
# ---------------------------------------------------------------------------


#: The layer packages ``/DataTypes`` holds, in the order the writer's passes open them.
LAYER_PACKAGES = ("Base", "Implementation", "Application", "MappingSets")


def _nested_packages(output: Path) -> list[str]:
    """Every ``AR-PACKAGE`` inside another ``AR-PACKAGE``, by its path below the document root."""
    tree = etree.parse(str(output))
    found: list[str] = []
    for node in tree.iter(ar_qname("AR-PACKAGE")):
        names = [node.findtext(ar_qname("SHORT-NAME"))]
        parent = node.getparent().getparent()
        while parent.tag == ar_qname("AR-PACKAGE"):
            names.insert(0, parent.findtext(ar_qname("SHORT-NAME")))
            parent = parent.getparent().getparent()
        if len(names) > 1:
            found.append("/".join(names))
    return found


class TestAModelWithoutPackage:
    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize("tree", FIXTURE_TREES, ids=_TREE_IDS)
    def test_it_emits_no_sub_package_but_the_layer_packages(self, tree: Path, version: str, tmp_path: Path) -> None:
        """Only ``/DataTypes``' layer packages, each once and in the passes' order."""
        nested = _nested_packages(_generate(tree, tmp_path, version))
        layers = [path.removeprefix("DataTypes/") for path in nested]
        assert nested == [f"DataTypes/{layer}" for layer in layers]
        assert layers == [layer for layer in LAYER_PACKAGES if layer in layers]

    @pytest.mark.parametrize("tree", FIXTURE_TREES, ids=_TREE_IDS)
    def test_its_table_places_nothing(self, tree: Path) -> None:
        document = _build_document(str(tree))
        assert set(document.placement.entries.values()) == {None}
        assert all(not packages for packages in document.placement.sub_packages.values())

    def test_the_empty_tree_too(self, tmp_path: Path) -> None:
        """No data type, no layer package: ``/DataTypes`` is emitted as it always was."""
        output = _generate(make_empty_model_root(tmp_path), tmp_path, "4.2.2")
        assert _nested_packages(output) == []
        assert _build_document(str(make_empty_model_root(tmp_path))).placement.entries == {}


class TestADocumentWithoutATable:
    def test_everything_goes_to_the_roots(self) -> None:
        document = ArxmlDocument()
        assert document.placement.entries == {}
        document.data_types.create_element("SW-BASE-TYPE", "float32")
        assert document.find("/DataTypes/float32") is not None
        assert document.data_types.element_claims == ((None, "float32"),)
        assert "<AR-PACKAGES>" not in document.serialize().split("<SHORT-NAME>DataTypes</SHORT-NAME>", 1)[1]

    def test_an_emitter_called_alone_names_the_home(self) -> None:
        """Outside ``_build_document`` the placement in force is the empty table."""
        assert _placed_path(_HOME.IMPLEMENTATION_TYPES, "uint8") == "/DataTypes/Implementation/uint8"
        assert _placed_path(_HOME.COMPONENT_TYPES, "Swc") == "/ComponentTypes/Swc"

    def test_placed_path_takes_a_home_not_a_string(self) -> None:
        with pytest.raises(TypeError, match="Home"):
            _placed_path("DataTypes", "uint8")  # type: ignore[arg-type]


class TestThePlacementDoesNotOutliveTheBuild:
    def test_a_refused_build_leaves_the_empty_table_in_force(self, tmp_path: Path) -> None:
        model_root = placed_model_root(tmp_path)
        put(
            model_root,
            "connectors/link.yaml",
            {
                "name": "link",
                "from": {"composition": "Inner", "port": "pNone"},
                "to": {"swc": "Consumer", "port": "rIn"},
            },
        )
        with pytest.raises(ValueError):
            _build_document(str(model_root))
        assert _placed_path(_HOME.BASE_TYPES, "uint8_base") == "/DataTypes/Base/uint8_base"


# ---------------------------------------------------------------------------
# The containment checks
# ---------------------------------------------------------------------------


def _table(
    entries: dict[tuple[str, str], str | None], declared: set[tuple[str, str]] | None = None
) -> PackagePlacement:
    return PackagePlacement(entries=entries, declared=frozenset(declared or ()))


class TestTheContainmentChecks:
    def test_an_element_the_table_does_not_know_raises(self) -> None:
        document = ArxmlDocument(placement=_table({("DataTypes", "Known"): None}))
        document.data_types.create_element("SW-BASE-TYPE", "Known")
        document.data_types.create_element("SW-BASE-TYPE", "Stray")
        with pytest.raises(ValueError, match="DataTypes element 'Stray' is not in the placement table"):
            _check_package_containment(document)

    def test_a_sub_package_outside_the_tables_set_raises(self) -> None:
        document = ArxmlDocument(placement=_table({("DataTypes", "X"): "Lib"}))
        document.data_types.create_element("SW-BASE-TYPE", "X")
        with pytest.raises(ValueError, match="sub-package DataTypes/Lib is not in the placement table"):
            _check_package_containment(document, _table({("DataTypes", "X"): None}))

    def test_an_element_created_elsewhere_than_the_table_says_raises(self) -> None:
        document = ArxmlDocument(placement=_table({("DataTypes", "X"): "Lib"}))
        document.data_types.create_element("SW-BASE-TYPE", "X")
        with pytest.raises(ValueError, match="'X' was created in 'Lib', the table places it in 'Other'"):
            _check_package_containment(document, _table({("DataTypes", "X"): "Other"}))

    def test_a_declared_element_never_created_raises(self) -> None:
        document = ArxmlDocument(placement=_table({("PortInterfaces", "I"): "Sr"}, {("PortInterfaces", "I")}))
        with pytest.raises(ValueError, match="declared PortInterfaces element 'I' was never created"):
            _check_package_containment(document)

    def test_a_table_placing_outside_the_roots_is_refused(self) -> None:
        with pytest.raises(ValueError, match="names no root package"):
            _table({("Calibration", "X"): None})

    def test_a_table_declaring_what_it_does_not_place_is_refused(self) -> None:
        with pytest.raises(ValueError, match="declares elements it does not place"):
            _table({}, {("DataTypes", "X")})

    def test_a_declared_element_wins_over_a_generated_form_that_is_never_emitted(self) -> None:
        """``A_Unit`` beside an application type ``A`` that declares no unit is legal, placed or not."""
        from swcraft_tools.schema import ApplicationDataType, MappingSet, ValueDataType

        app = ApplicationDataType.model_validate({"name": "A", "kind": "application_type"})
        for declared_first in (True, False):
            unit = ValueDataType(name="A_Unit", kind="value_type", base="b", package="P")
            types = [unit, app] if declared_first else [app, unit]
            table = _package_placement([], [], types, [], [MappingSet(name="A_Range", package="Q")], ())
            assert table.package_of(IMPLEMENTATION, "A_Unit") == "P"
            assert table.package_of(MAPPING_SETS, "A_Range") == "Q"
            assert table.package_of(APPLICATION, "A_Compu") is None
            # The form's own entry is beside its owner, in another home.
            assert table.package_of(APPLICATION, "A_Unit") is None

    def test_two_declared_elements_of_one_name_in_two_packages_raise(self) -> None:
        """Two declarations of one home sharing one path raise; the up-front guards refuse it first."""
        from swcraft_tools.schema import ReferenceDataType, ValueDataType

        with pytest.raises(ValueError, match="'X' is placed twice in DataTypes/Implementation, in 'P' and in 'Q'"):
            _package_placement(
                [],
                [],
                [
                    ValueDataType(name="X", kind="value_type", base="b", package="P"),
                    ReferenceDataType(name="X", kind="reference_type", base="b", package="Q"),
                ],
                [],
                [],
                (),
            )

    def test_one_name_in_two_homes_is_two_entries(self) -> None:
        """The table is keyed by home and name: a value type and a mapping set of one name are two."""
        from swcraft_tools.schema import MappingSet, ValueDataType

        table = _package_placement(
            [], [], [ValueDataType(name="X", kind="value_type", base="b", package="P")], [], [MappingSet(name="X")], ()
        )
        assert table.package_of(IMPLEMENTATION, "X") == "P"
        assert table.knows(MAPPING_SETS, "X")
        assert table.package_of(MAPPING_SETS, "X") is None

    @pytest.mark.parametrize("with_init", [False, True], ids=["no-init", "init"])
    def test_two_placed_interfaces_deriving_one_init_constant_are_refused_by_the_rule(
        self, with_init: bool, tmp_path: Path
    ) -> None:
        """``A_B``/``C`` in ``P`` and ``A``/``B_C`` in ``Q`` both derive ``A_B_C_Init`` and are refused.

        The reservation of ``{I}_{E}_Init`` is unconditional (``_init_constant_pairs``
        and ``interface.init-constant-name-collision``), so ``check-model`` refuses
        the pair and the writer's ``_check_init_constant_names`` refuses it before
        the table is built -- whether or not ``init`` is set, wherever the two are
        placed; the table's internal "placed twice" is never reached.
        """
        model_root = placed_model_root(tmp_path)
        extra = {"init": 0} if with_init else {}
        put(
            model_root,
            "interfaces/A_B.yaml",
            {
                "name": "A_B",
                "kind": "sender_receiver",
                "package": "P",
                "data_elements": [{"name": "C", "implementation_type": "uint8", **extra}],
            },
        )
        put(
            model_root,
            "interfaces/A.yaml",
            {
                "name": "A",
                "kind": "sender_receiver",
                "package": "Q",
                "data_elements": [{"name": "B_C", "implementation_type": "uint8", **extra}],
            },
        )
        from swcraft_tools.validator import check_cross_layer_consistency

        rule = "interface.init-constant-name-collision"
        (record,) = [record for record in check_cross_layer_consistency(str(model_root)).records if record.rule == rule]
        assert "'A_B_C_Init' has more than one claimant" in record.message
        with pytest.raises(ValueError, match="derive one CONSTANT-SPECIFICATION SHORT-NAME") as refused:
            generate_arxml(str(model_root), str(tmp_path / "out" / "model.arxml"))
        assert "placed twice" not in str(refused.value)
        assert not (tmp_path / "out").exists()

    def test_the_whole_model_generates_with_the_declared_element_placed(self, tmp_path: Path) -> None:
        model_root = placed_model_root(tmp_path)
        write_model_element(model_root, {"name": "Plain", "kind": "application_type"})
        write_model_element(
            model_root, {"name": "Plain_Unit", "kind": "value_type", "base": "uint8_base", "package": "Units"}
        )
        output = tmp_path / "out" / "model.arxml"
        generate_arxml(str(model_root), str(output))
        index = path_index(output)
        assert "/DataTypes/Implementation/Units/Plain_Unit" in index
        assert "/DataTypes/Implementation/Plain_Unit" not in index

    def test_the_table_reads_the_model_and_enters_generated_forms_with_their_owner(self, tmp_path: Path) -> None:
        from swcraft_tools.reader import read_compositions, read_mapping_sets

        model_root = str(placed_model_root(tmp_path))
        table = _package_placement(
            read_swc_model(model_root),
            read_interfaces(model_root),
            # The writer's own list: both data description layers (`_build_document`).
            list(read_data_description(model_root).data_types),
            read_compositions(model_root),
            read_mapping_sets(model_root),
            (),
        )
        assert {key: table.package_of(*key) for key in {**PLACED, **GENERATED}} == {**PLACED, **GENERATED}
        assert table.sub_packages["DataTypes"] == frozenset()
        assert {home: table.sub_packages[home] for home in (BASE, IMPLEMENTATION, APPLICATION, MAPPING_SETS)} == {
            BASE: {"PlatformTypes"},
            IMPLEMENTATION: {"Values", "Impl"},
            APPLICATION: {"App"},
            MAPPING_SETS: {"Mappings"},
        }
        assert ("ComponentTypes", "Consumer_Implementation") not in table.declared
        assert ("ComponentTypes", "Consumer") in table.declared


class TestTheRootPackageLayout:
    def test_a_placed_first_element_keeps_elements_before_ar_packages(self) -> None:
        document = ArxmlDocument(placement=_table({("DataTypes", "a"): "Lib", ("DataTypes", "b"): None}))
        document.data_types.create_element("SW-BASE-TYPE", "a")
        document.data_types.create_element("SW-BASE-TYPE", "b")
        tags = [etree.QName(child).localname for child in document.data_types.node]
        assert tags == ["SHORT-NAME", "ELEMENTS", "AR-PACKAGES"]
        assert document.find("/DataTypes/Lib/a") is not None
        assert document.find("/DataTypes/b") is not None

    def test_a_sub_package_is_named_in_its_roots_name_space(self) -> None:
        document = ArxmlDocument(placement=_table({("DataTypes", "Lib"): None, ("DataTypes", "x"): "Lib"}))
        document.data_types.create_element("SW-BASE-TYPE", "Lib")
        # A sub-package's name against an element is the case-folding claim's
        # refusal under `rule:name.name-space-collision`, exact or not -- no
        # model-identity rule sees the pair.
        with pytest.raises(
            ValueError, match=r"rule:name\.name-space-collision: package '/DataTypes' holds 'Lib' and 'Lib' \(a sub"
        ):
            document.data_types.create_element("SW-BASE-TYPE", "x")

    def test_add_element_is_placed_too(self) -> None:
        document = ArxmlDocument(placement=_table({("DataTypes", "a"): "Lib"}))
        element = etree.Element(ar_qname("SW-BASE-TYPE"))
        etree.SubElement(element, ar_qname("SHORT-NAME")).text = "a"
        document.data_types.add_element(element)
        assert document.find("/DataTypes/Lib/a") is element


# ---------------------------------------------------------------------------
# The reference oracle
# ---------------------------------------------------------------------------


class TestTheReferenceOracle:
    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize("tree", FIXTURE_TREES, ids=_TREE_IDS)
    def test_every_fixture_trees_references_resolve(self, tree: Path, version: str, tmp_path: Path) -> None:
        output = _generate(tree, tmp_path, version)
        assert references(output), "a tree with no reference proves nothing here"
        assert reference_problems(output, external=_external_refs(tree)) == []

    def test_the_external_stub_tree_needs_its_stubs_named(self, tmp_path: Path) -> None:
        """Non-vacuity of the exception: without the stub's path, its reference fails."""
        tree = FIXTURES / "compositions" / "model"
        output = _generate(tree, tmp_path, "4.2.2")
        assert _external_refs(tree)
        assert reference_problems(output) != []

    def test_a_dangling_reference_is_reported(self, tmp_path: Path) -> None:
        model_root = tmp_path / "copy"
        shutil.copytree(FIXTURES / "component_exchange" / "model", model_root)
        text = _generate(model_root, tmp_path, "4.2.2").read_text(encoding="utf-8")
        broken = text.replace(
            'DEST="SENDER-RECEIVER-INTERFACE">/PortInterfaces/', 'DEST="SENDER-RECEIVER-INTERFACE">/Gone/', 1
        )
        assert broken != text
        assert any("names no element of the document" in problem for problem in reference_problems(broken))

    def test_a_wrong_dest_is_reported(self, tmp_path: Path) -> None:
        text = _generate(FIXTURES / "component_exchange" / "model", tmp_path, "4.2.2").read_text(encoding="utf-8")
        broken = text.replace('DEST="SENDER-RECEIVER-INTERFACE"', 'DEST="CLIENT-SERVER-INTERFACE"', 1)
        assert broken != text
        assert any("has DEST CLIENT-SERVER-INTERFACE" in problem for problem in reference_problems(broken))
