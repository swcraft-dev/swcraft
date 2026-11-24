"""The writer-fidelity harness is trusted only as far as these tests reach.

``fidelity_strip_support.strip_fidelity`` parses a generated document and re-serializes it through
the writer's own serializer, so its **first** test is identity: every committed tree's output, at
both versions, and every committed golden survive parse-and-serialize byte for byte. If one did not,
a digest match after the strip would prove nothing.

Then one test per reversal class on a hand-built fragment, each with the neighbour it must leave
alone, and the refusals a reversal raises rather than guessing. Last, on a document in the old
writer's form the chained strip (``termination_strip_support.strip_recorded``: the fidelity strip,
then the termination strip) changes nothing but the termination lines.

**Not reached:** the fragments are hand-built to the shapes the writer was designed to emit; a writer
change of another shape is caught by ``test_writer_fidelity_refreeze.py``'s digests, not here. The
identity claim is over the committed trees and the goldens, not every model: an element written with an
empty text would not survive it (``fidelity_strip_support``'s docstring).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import INIT_VALUES_MODEL_ROOT, REPO_ROOT, make_empty_model_root
from fidelity_strip_support import (
    REVERSALS,
    FidelityStripError,
    flatten_data_description_packages,
    parse,
    restore_numerical_values,
    serialize,
    strip_counts,
    strip_fidelity,
)
from lxml import etree
from swcraft_tools.writer import generate_arxml
from swcraft_tools.writer.arxml_xml import AR_NAMESPACE, INDENT, format_init_number
from termination_strip_support import TERMINATION_ADDED, strip_recorded, strip_tree
from test_writer_fidelity_refreeze import CASES

GOLDEN_DIR = REPO_ROOT / "packages/tools/tests/fixtures/arxml_golden"

_CASES = CASES
#: The one recorded tree that states an init.
INIT_VALUES_TREE = INIT_VALUES_MODEL_ROOT.relative_to(REPO_ROOT).as_posix()
_IDS = [f"{tree}:{version}" for tree, version in _CASES]


def _generated(tree: str, version: str, tmp_path: Path) -> bytes:
    output = tmp_path / "model.arxml"
    generate_arxml(str(REPO_ROOT / tree), str(output), version=version)
    return output.read_bytes()


# Identity: the harness's own premise


@pytest.mark.parametrize(("tree", "version"), _CASES, ids=_IDS)
def test_a_committed_trees_output_survives_parse_and_serialize(tree: str, version: str, tmp_path: Path) -> None:
    data = _generated(tree, version, tmp_path)
    assert serialize(parse(data)) == data


@pytest.mark.parametrize("golden", sorted(path.name for path in GOLDEN_DIR.glob("*.arxml")))
def test_a_committed_golden_survives_parse_and_serialize(golden: str) -> None:
    data = (GOLDEN_DIR / golden).read_bytes()
    assert serialize(parse(data)) == data


@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
def test_the_empty_models_output_survives_parse_and_serialize(version: str, tmp_path: Path) -> None:
    output = tmp_path / "empty.arxml"
    generate_arxml(str(make_empty_model_root(tmp_path)), str(output), version=version)
    data = output.read_bytes()
    assert serialize(parse(data)) == data


_AR = f"{{{AR_NAMESPACE}}}"


def _layer_package_elements(root: etree._Element) -> int:
    """How many elements ``/DataTypes``' layer packages hold, counted with ``lxml`` alone."""
    return sum(
        len(elements)
        for package in root.iterfind(f"{_AR}AR-PACKAGES/{_AR}AR-PACKAGE")
        if package.findtext(f"{_AR}SHORT-NAME") == "DataTypes"
        for elements in package.iterfind(f"{_AR}AR-PACKAGES/{_AR}AR-PACKAGE/{_AR}ELEMENTS")
    )


def _own_data_types_elements(root: etree._Element) -> int:
    """How many elements ``/DataTypes`` holds in its own ``ELEMENTS``, outside every layer package."""
    return sum(
        len(elements)
        for package in root.iterfind(f"{_AR}AR-PACKAGES/{_AR}AR-PACKAGE")
        if package.findtext(f"{_AR}SHORT-NAME") == "DataTypes"
        for elements in package.iterfind(f"{_AR}ELEMENTS")
    )


@pytest.mark.parametrize(("tree", "version"), _CASES, ids=_IDS)
def test_the_package_flattening_moves_every_layer_packages_element_of_a_committed_trees_output(
    tree: str, version: str, tmp_path: Path
) -> None:
    """The reversal moves exactly the layer packages' elements, leaves ``/DataTypes`` flat, and is idempotent."""
    root = parse(_generated(tree, version, tmp_path))
    held = _layer_package_elements(root)
    # Not vacuous: a tree that declares a data type emits it into a layer package, never into
    # `/DataTypes` itself, so a writer that went back to the old layout fails here.
    declares = any((REPO_ROOT / tree / "data_types").rglob("*.yaml"))
    assert (held > 0) == declares
    assert _own_data_types_elements(root) == 0
    assert flatten_data_description_packages(root) == held
    assert _layer_package_elements(root) == 0
    assert flatten_data_description_packages(parse(serialize(root))) == 0


@pytest.mark.parametrize("golden", sorted(path.name for path in GOLDEN_DIR.glob("*.arxml")))
def test_the_package_flattening_moves_every_layer_packages_element_of_a_committed_golden(golden: str) -> None:
    root = parse((GOLDEN_DIR / golden).read_bytes())
    held = _layer_package_elements(root)
    # Not vacuous: `/DataTypes` holds no element of its own, so every data type the golden carries is
    # one the flattening moves; only the empty model's golden carries none.
    assert _own_data_types_elements(root) == 0
    assert (held > 0) == (not golden.startswith("empty-"))
    assert flatten_data_description_packages(root) == held
    assert _layer_package_elements(root) == 0


# On a document in the old writer's form the chained strip changes nothing


@pytest.mark.parametrize(("tree", "version"), _CASES, ids=_IDS)
def test_on_an_old_form_document_the_chained_strip_changes_nothing(tree: str, version: str, tmp_path: Path) -> None:
    """The stripped output is the old writer's form; stripping it again moves nothing.

    The chained strip then removes the termination lines alone.
    """
    old_form = strip_fidelity(_generated(tree, version, tmp_path))
    again, counts = strip_counts(old_form)
    assert again == old_form
    assert counts == dict.fromkeys(counts, 0)
    termination_only = strip_tree(tree, old_form) if tree in TERMINATION_ADDED else old_form
    assert strip_recorded(tree, f"arxml-{version}", old_form) == termination_only


def test_the_chained_strip_leaves_every_other_artifact_alone() -> None:
    data = b"not ARXML at all\n"
    assert strip_recorded("examples/basic/model", "needs", data) == data


# One test per reversal class, on hand-built fragments


def _document(elements: str, *, extra_packages: str = "") -> bytes:
    """A whole document in the writer's serialization: one ``Pkg`` package holding *elements*."""
    text = (
        f'<?xml version="1.0" encoding="utf-8"?>\n<AUTOSAR xmlns="{AR_NAMESPACE}"><AR-PACKAGES>'
        f"<AR-PACKAGE><SHORT-NAME>Pkg</SHORT-NAME><ELEMENTS>{elements}</ELEMENTS></AR-PACKAGE>"
        f"{extra_packages}</AR-PACKAGES></AUTOSAR>"
    )
    return serialize(parse(text.encode("utf-8")))


def _strip(document: bytes) -> tuple[bytes, dict[str, int]]:
    stripped, counts = strip_counts(document)
    return stripped, {name: count for name, count in counts.items() if count}


def _props(*children: str) -> str:
    return (
        "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
        + "".join(children)
        + "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
    )


_ACCESS = "<SW-CALIBRATION-ACCESS>NOT-ACCESSIBLE</SW-CALIBRATION-ACCESS>"


def test_every_reversal_has_a_test_below() -> None:
    assert [name for name, _ in REVERSALS] == [
        "data-description-packages",
        "descriptions",
        "member-categories",
        "compu-method-units",
        "value-type-ranges",
        "filters",
        "range-and-timeout-handling",
        "argument-policies",
        "impl-policies",
        "data-element-calibration-access",
        "disabled-modes",
        "queued-com-specs",
        "is-service",
        "port-api-options",
        "value-forms",
    ]


def _layered_document(data_types: str, *, interfaces: str = "") -> bytes:
    """A whole document in the writer's serialization.

    A ``DataTypes`` root package holding *data_types* (its ``ELEMENTS`` and/or ``AR-PACKAGES``), then a
    ``PortInterfaces`` one holding *interfaces*.
    """
    port_interfaces = f"<ELEMENTS>{interfaces}</ELEMENTS>" if interfaces else ""
    text = (
        f'<?xml version="1.0" encoding="utf-8"?>\n<AUTOSAR xmlns="{AR_NAMESPACE}"><AR-PACKAGES>'
        f"<AR-PACKAGE><SHORT-NAME>DataTypes</SHORT-NAME>{data_types}</AR-PACKAGE>"
        f"<AR-PACKAGE><SHORT-NAME>PortInterfaces</SHORT-NAME>{port_interfaces}</AR-PACKAGE>"
        "</AR-PACKAGES></AUTOSAR>"
    )
    return serialize(parse(text.encode("utf-8")))


def _layer_package(name: str, *elements: str) -> str:
    return f"<AR-PACKAGE><SHORT-NAME>{name}</SHORT-NAME><ELEMENTS>{''.join(elements)}</ELEMENTS></AR-PACKAGE>"


def _flatten(document: bytes) -> tuple[bytes, int]:
    root = parse(document)
    count = flatten_data_description_packages(root)
    return serialize(root), count


class TestDataDescriptionPackages:
    """The four layer packages are flattened back into ``/DataTypes`` in the old pass order.

    Every ``DEST`` reference is rewritten, and the shapes the reversal cannot map back are refused.
    """

    @staticmethod
    def _ref(tag: str, dest: str, layer: str | None, name: str) -> str:
        path = f"/DataTypes/{name}" if layer is None else f"/DataTypes/{layer}/{name}"
        return f'<{tag} DEST="{dest}">{path}</{tag}>'

    def _elements(self, layered: bool) -> dict[str, list[str]]:
        """Return each layer package's elements in the writer's emission order.

        Each private form sits directly before its type, referencing in either layout.
        """

        def ref(tag: str, dest: str, layer: str, name: str) -> str:
            return self._ref(tag, dest, layer if layered else None, name)

        base = ref("BASE-TYPE-REF", "SW-BASE-TYPE", "Base", "u8_base")
        return {
            "Base": [
                "<SW-BASE-TYPE><SHORT-NAME>u8_base</SHORT-NAME><CATEGORY>FIXED_LENGTH</CATEGORY>"
                "<BASE-TYPE-SIZE>8</BASE-TYPE-SIZE></SW-BASE-TYPE>",
            ],
            "Implementation": [
                "<DATA-CONSTR><SHORT-NAME>Raw_Range</SHORT-NAME></DATA-CONSTR>",
                "<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>Raw</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
                + _props(base, ref("DATA-CONSTR-REF", "DATA-CONSTR", "Implementation", "Raw_Range"))
                + "</IMPLEMENTATION-DATA-TYPE>",
            ],
            "Application": [
                "<UNIT><SHORT-NAME>Speed_Unit</SHORT-NAME></UNIT>",
                "<COMPU-METHOD><SHORT-NAME>Speed_Compu</SHORT-NAME><CATEGORY>IDENTICAL</CATEGORY>"
                + ref("UNIT-REF", "UNIT", "Application", "Speed_Unit")
                + "</COMPU-METHOD>",
                "<DATA-CONSTR><SHORT-NAME>Speed_Range</SHORT-NAME></DATA-CONSTR>",
                "<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>Speed</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
                + _props(
                    ref("COMPU-METHOD-REF", "COMPU-METHOD", "Application", "Speed_Compu"),
                    ref("DATA-CONSTR-REF", "DATA-CONSTR", "Application", "Speed_Range"),
                    ref("UNIT-REF", "UNIT", "Application", "Speed_Unit"),
                )
                + "</APPLICATION-PRIMITIVE-DATA-TYPE>",
                "<COMPU-METHOD><SHORT-NAME>Mode_CompuMethod</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY></COMPU-METHOD>",
                "<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>Mode</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
                + _props(ref("COMPU-METHOD-REF", "COMPU-METHOD", "Application", "Mode_CompuMethod"))
                + "</APPLICATION-PRIMITIVE-DATA-TYPE>",
                "<APPLICATION-RECORD-DATA-TYPE><SHORT-NAME>Pair</SHORT-NAME><CATEGORY>STRUCTURE</CATEGORY>"
                "<ELEMENTS><APPLICATION-RECORD-ELEMENT><SHORT-NAME>speed</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
                + ref("TYPE-TREF", "APPLICATION-PRIMITIVE-DATA-TYPE", "Application", "Speed")
                + "</APPLICATION-RECORD-ELEMENT></ELEMENTS></APPLICATION-RECORD-DATA-TYPE>",
                "<APPLICATION-ARRAY-DATA-TYPE><SHORT-NAME>Modes</SHORT-NAME><CATEGORY>ARRAY</CATEGORY>"
                "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
                "<ELEMENT><SHORT-NAME>Modes_Element</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
                + ref("TYPE-TREF", "APPLICATION-PRIMITIVE-DATA-TYPE", "Application", "Mode")
                + "</ELEMENT></APPLICATION-ARRAY-DATA-TYPE>",
            ],
            "MappingSets": [
                "<DATA-TYPE-MAPPING-SET><SHORT-NAME>Maps</SHORT-NAME><DATA-TYPE-MAPS><DATA-TYPE-MAP>"
                + ref("APPLICATION-DATA-TYPE-REF", "APPLICATION-PRIMITIVE-DATA-TYPE", "Application", "Speed")
                + ref("IMPLEMENTATION-DATA-TYPE-REF", "IMPLEMENTATION-DATA-TYPE", "Implementation", "Raw")
                + "</DATA-TYPE-MAP></DATA-TYPE-MAPS></DATA-TYPE-MAPPING-SET>",
            ],
        }

    def _interface(self, layered: bool) -> str:
        return (
            "<SENDER-RECEIVER-INTERFACE><SHORT-NAME>I</SHORT-NAME><IS-SERVICE>false</IS-SERVICE><DATA-ELEMENTS>"
            "<VARIABLE-DATA-PROTOTYPE><SHORT-NAME>e</SHORT-NAME>"
            + self._ref("TYPE-TREF", "APPLICATION-PRIMITIVE-DATA-TYPE", "Application" if layered else None, "Speed")
            + "</VARIABLE-DATA-PROTOTYPE></DATA-ELEMENTS></SENDER-RECEIVER-INTERFACE>"
        )

    def _new(self) -> bytes:
        packages = "".join(_layer_package(name, *elements) for name, elements in self._elements(True).items())
        return _layered_document(f"<AR-PACKAGES>{packages}</AR-PACKAGES>", interfaces=self._interface(True))

    def _old(self) -> bytes:
        elements = [element for group in self._elements(False).values() for element in group]
        return _layered_document(f"<ELEMENTS>{''.join(elements)}</ELEMENTS>", interfaces=self._interface(False))

    def test_the_four_packages_concatenate_in_the_old_pass_order_with_every_reference_rewritten(self) -> None:
        assert _flatten(self._new()) == (self._old(), 12)

    def test_the_mixed_and_identity_documents_hold_an_empty_element_under_a_moved_parent(self) -> None:
        """Both byte documents carry an empty ``SW-DATA-DEF-PROPS-VARIANTS`` under a moved parent.

        That keeps the closing-tag indentation of such an element under test.
        """

        def homes(document: bytes) -> list[str | None]:
            return [
                next(node.iterancestors(f"{_AR}AR-PACKAGE")).findtext(f"{_AR}SHORT-NAME")
                for node in parse(document).iter(f"{_AR}SW-DATA-DEF-PROPS-VARIANTS")
                if len(node) == 0
            ]

        assert homes(self._new()) == ["Application"]
        assert homes(self._old()) == ["DataTypes"]

    def test_the_packages_document_order_does_not_decide_the_flattened_order(self) -> None:
        """The pass order is the reversal's, so a package written out of it still flattens to the old order."""
        groups = self._elements(True)
        packages = "".join(_layer_package(name, *groups[name]) for name in reversed(list(groups)))
        new = _layered_document(f"<AR-PACKAGES>{packages}</AR-PACKAGES>", interfaces=self._interface(True))
        assert _flatten(new) == (self._old(), 12)

    def test_it_runs_first_so_the_reversals_after_it_see_the_old_layout(self) -> None:
        """The value type's range is resolved by ``value-type-ranges`` through the rewritten path."""
        old_stripped, old_counts = _strip(self._old())
        assert old_counts == {"compu-method-units": 1, "value-type-ranges": 1}
        assert _strip(self._new()) == (old_stripped, {"data-description-packages": 12, **old_counts})

    def test_an_empty_elements_closing_tag_takes_the_old_depth(self) -> None:
        """An empty element's closing tag moved up two levels comes back at the old depth.

        Only the serializer's expansion text is dropped and written again.
        """
        flattened, _count = _flatten(self._new())
        assert flattened == self._old()
        lines = flattened.decode("utf-8").splitlines()
        (empty,) = [
            index
            for index, line in enumerate(lines[:-1])
            if line.strip() == "<SW-DATA-DEF-PROPS-VARIANTS>"
            and lines[index + 1].strip() == "</SW-DATA-DEF-PROPS-VARIANTS>"
        ]
        assert lines[empty + 1] == lines[empty].replace("<", "</", 1)

    @staticmethod
    def _with_empty_element_text(document: bytes, text_at_depth: Callable[[int], str]) -> tuple[bytes, str]:
        """*document* with the moved empty ``SW-DATA-DEF-PROPS-VARIANTS``' text set from its depth."""
        root = parse(document)
        (node,) = [node for node in root.iter(f"{_AR}SW-DATA-DEF-PROPS-VARIANTS") if len(node) == 0]
        text = text_at_depth(sum(1 for _ in node.iterancestors()))
        node.text = text
        return serialize(root), text

    def _flattened_empty_element_text(self, text_at_depth: Callable[[int], str]) -> None:
        new, text = self._with_empty_element_text(self._new(), text_at_depth)
        flattened, _count = _flatten(new)
        assert flattened != self._old()
        (node,) = [node for node in parse(flattened).iter(f"{_AR}SW-DATA-DEF-PROPS-VARIANTS") if len(node) == 0]
        assert node.text == text

    def test_an_empty_moved_elements_text_with_a_non_whitespace_character_is_kept(self) -> None:
        """Only the serializer's own expansion is dropped; a text holding a non-whitespace character is kept."""
        self._flattened_empty_element_text(lambda depth: "\n" + INDENT * depth + "x")

    def test_an_empty_moved_elements_whitespace_of_another_shape_is_kept(self) -> None:
        """Whitespace that is not exactly newline plus the depth's indentation is kept."""
        self._flattened_empty_element_text(lambda depth: "\n" + INDENT * (depth + 1))

    def test_an_empty_element_outside_the_moved_subtrees_is_not_touched(self) -> None:
        """The helper acts only inside the subtrees the reversal moves; an empty element elsewhere is not touched."""
        root = parse(self._new())
        interface = next(root.iter(f"{_AR}SENDER-RECEIVER-INTERFACE"))
        empty = etree.SubElement(interface, f"{_AR}ADMIN-DATA")
        expansion = "\n" + INDENT * sum(1 for _ in empty.iterancestors())
        empty.text = expansion
        flatten_data_description_packages(root)
        assert empty.text == expansion

    def test_a_document_in_the_old_layout_is_left_alone(self) -> None:
        assert _flatten(self._old()) == (self._old(), 0)

    def test_an_empty_data_types_package_is_left_alone(self) -> None:
        document = _layered_document("")
        assert _flatten(document) == (document, 0)

    def test_an_empty_layer_package_goes_and_no_empty_elements_is_written(self) -> None:
        new = _layered_document("<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Base</SHORT-NAME></AR-PACKAGE></AR-PACKAGES>")
        assert _flatten(new) == (_layered_document(""), 0)

    @pytest.mark.parametrize("package", ["Units", "CompuMethods", "Constraints"])
    def test_a_package_of_declared_forms_is_refused(self, package: str) -> None:
        new = _layered_document(
            f"<AR-PACKAGES>{_layer_package(package, '<UNIT><SHORT-NAME>u</SHORT-NAME></UNIT>')}</AR-PACKAGES>"
        )
        with pytest.raises(FidelityStripError, match=f"/DataTypes/{package} holds declared elements"):
            strip_fidelity(new)

    def test_a_sub_package_inside_a_layer_package_is_refused(self) -> None:
        inner = _layer_package("Chassis", "<SW-BASE-TYPE><SHORT-NAME>b</SHORT-NAME></SW-BASE-TYPE>")
        new = _layered_document(
            "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Base</SHORT-NAME>"
            f"<AR-PACKAGES>{inner}</AR-PACKAGES></AR-PACKAGE></AR-PACKAGES>"
        )
        with pytest.raises(FidelityStripError, match="/DataTypes/Base holds a sub-package"):
            strip_fidelity(new)

    def test_a_package_it_does_not_know_is_refused(self) -> None:
        new = _layered_document(
            f"<AR-PACKAGES>{_layer_package('Chassis', '<SW-BASE-TYPE><SHORT-NAME>b</SHORT-NAME></SW-BASE-TYPE>')}"
            "</AR-PACKAGES>"
        )
        with pytest.raises(FidelityStripError, match="/DataTypes/Chassis is not a layer package"):
            strip_fidelity(new)

    def test_own_elements_beside_a_layer_package_are_refused(self) -> None:
        new = _layered_document(
            "<ELEMENTS><SW-BASE-TYPE><SHORT-NAME>a</SHORT-NAME></SW-BASE-TYPE></ELEMENTS>"
            f"<AR-PACKAGES>{_layer_package('Base', '<SW-BASE-TYPE><SHORT-NAME>b</SHORT-NAME></SW-BASE-TYPE>')}"
            "</AR-PACKAGES>"
        )
        with pytest.raises(FidelityStripError, match="elements of its own beside its layer packages"):
            strip_fidelity(new)

    def test_a_layer_package_path_in_an_element_with_no_dest_is_refused(self) -> None:
        new = _layered_document(
            "<ELEMENTS><SW-BASE-TYPE><SHORT-NAME>b</SHORT-NAME><CATEGORY>/DataTypes/Base/b</CATEGORY>"
            "</SW-BASE-TYPE></ELEMENTS>"
        )
        with pytest.raises(FidelityStripError, match="CATEGORY spells a layer package path outside a reference"):
            strip_fidelity(new)

    def test_two_layer_packages_of_one_name_are_refused(self) -> None:
        base = _layer_package("Base", "<SW-BASE-TYPE><SHORT-NAME>b</SHORT-NAME></SW-BASE-TYPE>")
        again = _layer_package("Base", "<SW-BASE-TYPE><SHORT-NAME>c</SHORT-NAME></SW-BASE-TYPE>")
        new = _layered_document(f"<AR-PACKAGES>{base}{again}</AR-PACKAGES>")
        with pytest.raises(FidelityStripError, match="/DataTypes holds two packages named Base"):
            strip_fidelity(new)

    def test_a_same_name_pair_of_two_layer_packages_is_refused(self) -> None:
        """Both would land in one ``ELEMENTS`` and both references would read ``/DataTypes/ProbeShared``."""
        application = _layer_package(
            "Application",
            "<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>ProbeShared</SHORT-NAME></APPLICATION-PRIMITIVE-DATA-TYPE>",
        )
        implementation = _layer_package(
            "Implementation",
            "<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>ProbeShared</SHORT-NAME></IMPLEMENTATION-DATA-TYPE>",
        )
        new = _layered_document(f"<AR-PACKAGES>{implementation}{application}</AR-PACKAGES>")
        with pytest.raises(FidelityStripError, match="two moved elements are both named ProbeShared"):
            strip_fidelity(new)

    def test_a_case_only_pair_of_two_layer_packages_is_moved(self) -> None:
        """The comparison is exact: the old writer's ``/DataTypes`` held a case-only pair (``CaseVariant`` aside)."""
        application = _layer_package(
            "Application",
            "<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>probeshared</SHORT-NAME></APPLICATION-PRIMITIVE-DATA-TYPE>",
        )
        implementation = _layer_package(
            "Implementation",
            "<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>ProbeShared</SHORT-NAME></IMPLEMENTATION-DATA-TYPE>",
        )
        root = parse(_layered_document(f"<AR-PACKAGES>{implementation}{application}</AR-PACKAGES>"))
        assert flatten_data_description_packages(root) == 2

    def test_a_reference_into_a_package_of_declared_forms_is_refused(self) -> None:
        reference = '<UNIT-REF DEST="UNIT">/DataTypes/Units/kmh</UNIT-REF>'
        new = _layered_document(
            "<ELEMENTS><COMPU-METHOD><SHORT-NAME>c</SHORT-NAME><CATEGORY>LINEAR</CATEGORY>"
            f"{reference}</COMPU-METHOD></ELEMENTS>"
        )
        with pytest.raises(FidelityStripError, match="UNIT-REF names a package the old writer had no place for"):
            strip_fidelity(new)


class TestDescriptions:
    def test_every_desc_and_long_name_is_removed(self) -> None:
        new = _document(
            "<COMPOSITION-SW-COMPONENT-TYPE><SHORT-NAME>C</SHORT-NAME>"
            '<LONG-NAME><L-4 L="FOR-ALL">Title</L-4></LONG-NAME>'
            '<DESC><L-2 L="FOR-ALL">Text</L-2></DESC></COMPOSITION-SW-COMPONENT-TYPE>'
            "<COMPU-METHOD><SHORT-NAME>E_CompuMethod</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY>"
            "<COMPU-INTERNAL-TO-PHYS><COMPU-SCALES><COMPU-SCALE><SHORT-LABEL>ON</SHORT-LABEL>"
            '<DESC><L-2 L="FOR-ALL">literal</L-2></DESC><LOWER-LIMIT>1</LOWER-LIMIT></COMPU-SCALE>'
            "</COMPU-SCALES></COMPU-INTERNAL-TO-PHYS></COMPU-METHOD>"
        )
        old = _document(
            "<COMPOSITION-SW-COMPONENT-TYPE><SHORT-NAME>C</SHORT-NAME></COMPOSITION-SW-COMPONENT-TYPE>"
            "<COMPU-METHOD><SHORT-NAME>E_CompuMethod</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY>"
            "<COMPU-INTERNAL-TO-PHYS><COMPU-SCALES><COMPU-SCALE><SHORT-LABEL>ON</SHORT-LABEL>"
            "<LOWER-LIMIT>1</LOWER-LIMIT></COMPU-SCALE></COMPU-SCALES></COMPU-INTERNAL-TO-PHYS></COMPU-METHOD>"
        )
        assert _strip(new) == (old, {"descriptions": 3})


class TestMemberCategories:
    def test_a_record_element_and_an_array_element_go_back_to_value(self) -> None:
        def document(record: str, array: str) -> bytes:
            return _document(
                "<APPLICATION-RECORD-DATA-TYPE><SHORT-NAME>R</SHORT-NAME><CATEGORY>STRUCTURE</CATEGORY>"
                "<ELEMENTS><APPLICATION-RECORD-ELEMENT><SHORT-NAME>f</SHORT-NAME>"
                f"<CATEGORY>{record}</CATEGORY></APPLICATION-RECORD-ELEMENT>"
                "<APPLICATION-RECORD-ELEMENT><SHORT-NAME>g</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
                "</APPLICATION-RECORD-ELEMENT></ELEMENTS></APPLICATION-RECORD-DATA-TYPE>"
                "<APPLICATION-ARRAY-DATA-TYPE><SHORT-NAME>A</SHORT-NAME><CATEGORY>ARRAY</CATEGORY>"
                f"<ELEMENT><SHORT-NAME>A_Element</SHORT-NAME><CATEGORY>{array}</CATEGORY></ELEMENT>"
                "</APPLICATION-ARRAY-DATA-TYPE>"
            )

        stripped, counts = _strip(document("STRUCTURE", "ARRAY"))
        assert (stripped, counts) == (document("VALUE", "VALUE"), {"member-categories": 2})
        assert b"<CATEGORY>STRUCTURE</CATEGORY>" in stripped  # the record type's own is kept
        assert b"<CATEGORY>ARRAY</CATEGORY>" in stripped

    def test_a_member_without_a_category_is_refused(self) -> None:
        document = _document(
            "<APPLICATION-RECORD-DATA-TYPE><SHORT-NAME>R</SHORT-NAME><ELEMENTS><APPLICATION-RECORD-ELEMENT>"
            "<SHORT-NAME>f</SHORT-NAME></APPLICATION-RECORD-ELEMENT></ELEMENTS></APPLICATION-RECORD-DATA-TYPE>"
        )
        with pytest.raises(FidelityStripError, match="composite member f carries no CATEGORY"):
            strip_fidelity(document)


class TestCompuMethodUnits:
    def test_the_compu_methods_unit_ref_goes_and_the_types_stays(self) -> None:
        unit = '<UNIT-REF DEST="UNIT">/Pkg/T_Unit</UNIT-REF>'
        new = _document(
            f"<COMPU-METHOD><SHORT-NAME>T_Compu</SHORT-NAME><CATEGORY>LINEAR</CATEGORY>{unit}</COMPU-METHOD>"
            f"<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>T</SHORT-NAME>{_props(unit)}"
            "</APPLICATION-PRIMITIVE-DATA-TYPE>"
        )
        old = _document(
            "<COMPU-METHOD><SHORT-NAME>T_Compu</SHORT-NAME><CATEGORY>LINEAR</CATEGORY></COMPU-METHOD>"
            f"<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>T</SHORT-NAME>{_props(unit)}"
            "</APPLICATION-PRIMITIVE-DATA-TYPE>"
        )
        assert _strip(new) == (old, {"compu-method-units": 1})


class TestValueTypeRanges:
    _APPLICATION = (
        "<DATA-CONSTR><SHORT-NAME>App_Range</SHORT-NAME></DATA-CONSTR>"
        "<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>App</SHORT-NAME>"
        + _props('<DATA-CONSTR-REF DEST="DATA-CONSTR">/Pkg/App_Range</DATA-CONSTR-REF>')
        + "</APPLICATION-PRIMITIVE-DATA-TYPE>"
    )

    def _value_type(self, ref: str | None) -> str:
        children = (
            [_ACCESS] if ref is None else [_ACCESS, f'<DATA-CONSTR-REF DEST="DATA-CONSTR">{ref}</DATA-CONSTR-REF>']
        )
        return (
            "<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>V</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
            f"{_props(*children)}</IMPLEMENTATION-DATA-TYPE>"
        )

    def test_the_value_types_ref_and_its_range_go_and_the_application_types_stay(self) -> None:
        new = _document(
            self._APPLICATION + self._value_type("/Only/V_Range"),
            extra_packages=(
                "<AR-PACKAGE><SHORT-NAME>Constrs</SHORT-NAME><ELEMENTS>"
                "<DATA-CONSTR><SHORT-NAME>Other_Range</SHORT-NAME></DATA-CONSTR></ELEMENTS></AR-PACKAGE>"
                "<AR-PACKAGE><SHORT-NAME>Only</SHORT-NAME><ELEMENTS>"
                "<DATA-CONSTR><SHORT-NAME>V_Range</SHORT-NAME><DATA-CONSTR-RULES/></DATA-CONSTR>"
                "</ELEMENTS></AR-PACKAGE>"
            ),
        )
        old = _document(
            self._APPLICATION + self._value_type(None),
            extra_packages=(
                "<AR-PACKAGE><SHORT-NAME>Constrs</SHORT-NAME><ELEMENTS>"
                "<DATA-CONSTR><SHORT-NAME>Other_Range</SHORT-NAME></DATA-CONSTR></ELEMENTS></AR-PACKAGE>"
                "<AR-PACKAGE><SHORT-NAME>Only</SHORT-NAME></AR-PACKAGE>"
            ),
        )
        assert _strip(new) == (old, {"value-type-ranges": 1})

    def test_a_ref_naming_nothing_is_refused(self) -> None:
        with pytest.raises(FidelityStripError, match="V's DATA-CONSTR-REF names nothing"):
            strip_fidelity(_document(self._value_type("/Pkg/V_Range")))

    def test_a_ref_naming_another_constraint_is_refused(self) -> None:
        with pytest.raises(FidelityStripError, match="not its own V_Range"):
            strip_fidelity(_document(self._APPLICATION + self._value_type("/Pkg/App_Range")))


def _receiver(*, filtered: bool, handles: bool = False) -> str:
    return (
        "<NONQUEUED-RECEIVER-COM-SPEC>"
        '<DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/Pkg/I/e</DATA-ELEMENT-REF>'
        + ("<HANDLE-OUT-OF-RANGE>NONE</HANDLE-OUT-OF-RANGE>" if handles else "")
        + "<USES-END-TO-END-PROTECTION>false</USES-END-TO-END-PROTECTION><ALIVE-TIMEOUT>0</ALIVE-TIMEOUT>"
        "<ENABLE-UPDATE>false</ENABLE-UPDATE>"
        + ("<FILTER><DATA-FILTER-TYPE>ALWAYS</DATA-FILTER-TYPE></FILTER>" if filtered else "")
        + "<HANDLE-NEVER-RECEIVED>false</HANDLE-NEVER-RECEIVED>"
        + ("<HANDLE-TIMEOUT-TYPE>NONE</HANDLE-TIMEOUT-TYPE>" if handles else "")
        + "</NONQUEUED-RECEIVER-COM-SPEC>"
    )


def _component(*ports: str) -> str:
    return (
        "<APPLICATION-SW-COMPONENT-TYPE><SHORT-NAME>C</SHORT-NAME><PORTS>"
        + "".join(ports)
        + "</PORTS></APPLICATION-SW-COMPONENT-TYPE>"
    )


def _r_port(name: str, *specs: str) -> str:
    return (
        f"<R-PORT-PROTOTYPE><SHORT-NAME>{name}</SHORT-NAME><REQUIRED-COM-SPECS>"
        + "".join(specs)
        + '</REQUIRED-COM-SPECS><REQUIRED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Pkg/I'
        "</REQUIRED-INTERFACE-TREF></R-PORT-PROTOTYPE>"
    )


class TestFilters:
    def test_filter_always_comes_back_after_enable_update_where_it_is_missing(self) -> None:
        new = _document(_component(_r_port("a", _receiver(filtered=False)), _r_port("b", _receiver(filtered=True))))
        old = _document(_component(_r_port("a", _receiver(filtered=True)), _r_port("b", _receiver(filtered=True))))
        assert _strip(new) == (old, {"filters": 1})

    def test_a_receiver_without_enable_update_is_refused(self) -> None:
        spec = (
            "<NONQUEUED-RECEIVER-COM-SPEC>"
            '<DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/Pkg/I/e</DATA-ELEMENT-REF>'
            "</NONQUEUED-RECEIVER-COM-SPEC>"
        )
        with pytest.raises(FidelityStripError, match="carries no ENABLE-UPDATE"):
            strip_fidelity(_document(_component(_r_port("a", spec))))


class TestRangeAndTimeoutHandling:
    def test_both_attributes_are_removed_from_receiver_and_sender(self) -> None:
        sender = (
            "<NONQUEUED-SENDER-COM-SPEC>"
            '<DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/Pkg/I/e</DATA-ELEMENT-REF>{}'
            "<USES-END-TO-END-PROTECTION>false</USES-END-TO-END-PROTECTION></NONQUEUED-SENDER-COM-SPEC>"
        )
        p_port = (
            "<P-PORT-PROTOTYPE><SHORT-NAME>p</SHORT-NAME><PROVIDED-COM-SPECS>{}</PROVIDED-COM-SPECS>"
            '<PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Pkg/I</PROVIDED-INTERFACE-TREF>'
            "</P-PORT-PROTOTYPE>"
        )
        new = _document(
            _component(
                p_port.format(sender.format("<HANDLE-OUT-OF-RANGE>SATURATE</HANDLE-OUT-OF-RANGE>")),
                _r_port("r", _receiver(filtered=True, handles=True)),
            )
        )
        old = _document(_component(p_port.format(sender.format("")), _r_port("r", _receiver(filtered=True))))
        assert _strip(new) == (old, {"range-and-timeout-handling": 3})


def _argument(name: str, policy: str | None) -> str:
    tail = "" if policy is None else f"<SERVER-ARGUMENT-IMPL-POLICY>{policy}</SERVER-ARGUMENT-IMPL-POLICY>"
    return (
        f"<ARGUMENT-DATA-PROTOTYPE><SHORT-NAME>{name}</SHORT-NAME>{_props(_ACCESS)}"
        f'<TYPE-TREF DEST="APPLICATION-ARRAY-DATA-TYPE">/Pkg/A</TYPE-TREF><DIRECTION>IN</DIRECTION>{tail}'
        "</ARGUMENT-DATA-PROTOTYPE>"
    )


def _cs_interface(*arguments: str, is_service: str = "false") -> str:
    return (
        f"<CLIENT-SERVER-INTERFACE><SHORT-NAME>ICs</SHORT-NAME><IS-SERVICE>{is_service}</IS-SERVICE><OPERATIONS>"
        "<CLIENT-SERVER-OPERATION><SHORT-NAME>op</SHORT-NAME><ARGUMENTS>"
        + "".join(arguments)
        + "</ARGUMENTS></CLIENT-SERVER-OPERATION></OPERATIONS></CLIENT-SERVER-INTERFACE>"
    )


class TestArgumentPolicies:
    def test_the_policy_comes_back_after_direction_where_it_is_missing(self) -> None:
        new = _document(_cs_interface(_argument("a", None), _argument("b", "USE-ARGUMENT-TYPE")))
        old = _document(_cs_interface(_argument("a", "USE-ARGUMENT-TYPE"), _argument("b", "USE-ARGUMENT-TYPE")))
        assert _strip(new) == (old, {"argument-policies": 1})

    def test_an_argument_without_direction_is_refused(self) -> None:
        argument = "<ARGUMENT-DATA-PROTOTYPE><SHORT-NAME>a</SHORT-NAME></ARGUMENT-DATA-PROTOTYPE>"
        with pytest.raises(FidelityStripError, match="argument a carries no DIRECTION"):
            strip_fidelity(_document(_cs_interface(argument)))


def _sr_interface(*elements: str, is_service: str = "false") -> str:
    return (
        f"<SENDER-RECEIVER-INTERFACE><SHORT-NAME>I</SHORT-NAME><IS-SERVICE>{is_service}</IS-SERVICE>"
        "<DATA-ELEMENTS>" + "".join(elements) + "</DATA-ELEMENTS></SENDER-RECEIVER-INTERFACE>"
    )


def _data_element(name: str, *props: str) -> str:
    return (
        f"<VARIABLE-DATA-PROTOTYPE><SHORT-NAME>{name}</SHORT-NAME>"
        + (_props(*props) if props else "")
        + '<TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Pkg/V</TYPE-TREF></VARIABLE-DATA-PROTOTYPE>'
    )


class TestImplPolicies:
    def test_every_impl_policy_goes_with_a_chain_it_leaves_empty(self) -> None:
        new = _document(
            _sr_interface(
                _data_element("q", "<SW-IMPL-POLICY>QUEUED</SW-IMPL-POLICY>"),
                _data_element("s", "<SW-IMPL-POLICY>STANDARD</SW-IMPL-POLICY>"),
            )
        )
        old = _document(_sr_interface(_data_element("q"), _data_element("s")))
        assert _strip(new) == (old, {"impl-policies": 2})


class TestDataElementCalibrationAccess:
    def test_the_data_elements_access_goes_and_the_arguments_stays(self) -> None:
        new = _document(
            _sr_interface(
                _data_element("r", "<SW-CALIBRATION-ACCESS>READ-ONLY</SW-CALIBRATION-ACCESS>"),
                _data_element(
                    "b",
                    "<SW-CALIBRATION-ACCESS>READ-WRITE</SW-CALIBRATION-ACCESS>",
                    "<SW-IMPL-POLICY>STANDARD</SW-IMPL-POLICY>",
                ),
            )
            + _cs_interface(_argument("a", "USE-ARGUMENT-TYPE"))
        )
        old = _document(
            _sr_interface(_data_element("r"), _data_element("b")) + _cs_interface(_argument("a", "USE-ARGUMENT-TYPE"))
        )
        assert _strip(new) == (old, {"impl-policies": 1, "data-element-calibration-access": 2})


class TestDisabledModes:
    def test_every_disabled_mode_container_is_removed(self) -> None:
        def event(modes: str) -> str:
            return (
                f"<TIMING-EVENT><SHORT-NAME>T</SHORT-NAME>{modes}"
                '<START-ON-EVENT-REF DEST="RUNNABLE-ENTITY">/Pkg/C/B/r</START-ON-EVENT-REF>'
                "<PERIOD>0.01</PERIOD></TIMING-EVENT>"
            )

        modes = (
            "<DISABLED-MODE-IREFS><DISABLED-MODE-IREF>"
            '<CONTEXT-PORT-REF DEST="R-PORT-PROTOTYPE">/Pkg/C/m</CONTEXT-PORT-REF>'
            '<CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF DEST="MODE-DECLARATION-GROUP-PROTOTYPE">/X/M/g'
            "</CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF>"
            '<TARGET-MODE-DECLARATION-REF DEST="MODE-DECLARATION">/X/G/off</TARGET-MODE-DECLARATION-REF>'
            "</DISABLED-MODE-IREF></DISABLED-MODE-IREFS>"
        )
        behaviour = "<SWC-INTERNAL-BEHAVIOR><SHORT-NAME>B</SHORT-NAME><EVENTS>{}</EVENTS></SWC-INTERNAL-BEHAVIOR>"
        new = _document(behaviour.format(event(modes)))
        old = _document(behaviour.format(event("")))
        assert _strip(new) == (old, {"disabled-modes": 1})


class TestQueuedComSpecs:
    def test_queued_com_specs_go_with_a_wrapper_they_leave_empty(self) -> None:
        queued_receiver = (
            "<QUEUED-RECEIVER-COM-SPEC>"
            '<DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/Pkg/I/q</DATA-ELEMENT-REF>'
            "<USES-END-TO-END-PROTECTION>false</USES-END-TO-END-PROTECTION><QUEUE-LENGTH>4</QUEUE-LENGTH>"
            "</QUEUED-RECEIVER-COM-SPEC>"
        )
        queued_sender = (
            "<QUEUED-SENDER-COM-SPEC>"
            '<DATA-ELEMENT-REF DEST="VARIABLE-DATA-PROTOTYPE">/Pkg/I/q</DATA-ELEMENT-REF>'
            "<USES-END-TO-END-PROTECTION>false</USES-END-TO-END-PROTECTION></QUEUED-SENDER-COM-SPEC>"
        )
        p_port = (
            "<P-PORT-PROTOTYPE><SHORT-NAME>p</SHORT-NAME>{}"
            '<PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Pkg/I</PROVIDED-INTERFACE-TREF>'
            "</P-PORT-PROTOTYPE>"
        )
        new = _document(
            _component(
                p_port.format(f"<PROVIDED-COM-SPECS>{queued_sender}</PROVIDED-COM-SPECS>"),
                _r_port("r", _receiver(filtered=True), queued_receiver),
            )
        )
        old = _document(_component(p_port.format(""), _r_port("r", _receiver(filtered=True))))
        assert _strip(new) == (old, {"queued-com-specs": 2})


class TestIsService:
    def test_a_stated_true_goes_back_to_false_on_both_interface_kinds(self) -> None:
        new = _document(
            _sr_interface(_data_element("e"), is_service="true")
            + _cs_interface(_argument("a", "USE-ARGUMENT-TYPE"), is_service="true")
        )
        old = _document(_sr_interface(_data_element("e")) + _cs_interface(_argument("a", "USE-ARGUMENT-TYPE")))
        assert _strip(new) == (old, {"is-service": 2})


def _port(kind: str, name: str) -> str:
    side = "PROVIDED" if kind == "P-PORT-PROTOTYPE" else "REQUIRED"
    return (
        f"<{kind}><SHORT-NAME>{name}</SHORT-NAME>"
        f'<{side}-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Pkg/I</{side}-INTERFACE-TREF></{kind}>'
    )


def _option(kind: str, name: str) -> str:
    return (
        "<PORT-API-OPTION><ENABLE-TAKE-ADDRESS>false</ENABLE-TAKE-ADDRESS><INDIRECT-API>false</INDIRECT-API>"
        f'<PORT-REF DEST="{kind}">/Pkg/C/{name}</PORT-REF></PORT-API-OPTION>'
    )


_TERMINATION = "<HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>"
_RUNNABLES = "<RUNNABLES><RUNNABLE-ENTITY><SHORT-NAME>r</SHORT-NAME><SYMBOL>r</SYMBOL></RUNNABLE-ENTITY></RUNNABLES>"


def _behaving_component(ports: str | None, options: str | None, *, runnables: bool, tail: bool = True) -> str:
    """A component in the writer's layout: ``PORTS``, then its behaviour, the container where the old writer put it."""
    return (
        "<APPLICATION-SW-COMPONENT-TYPE><SHORT-NAME>C</SHORT-NAME>"
        + ("" if ports is None else f"<PORTS>{ports}</PORTS>")
        + "<INTERNAL-BEHAVIORS><SWC-INTERNAL-BEHAVIOR><SHORT-NAME>C_InternalBehavior</SHORT-NAME>"
        + "<EVENTS><INIT-EVENT><SHORT-NAME>E</SHORT-NAME></INIT-EVENT></EVENTS>"
        + _TERMINATION
        + ("" if options is None else f"<PORT-API-OPTIONS>{options}</PORT-API-OPTIONS>")
        + (_RUNNABLES if runnables else "")
        + ("<SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>" if tail else "")
        + "</SWC-INTERNAL-BEHAVIOR></INTERNAL-BEHAVIORS></APPLICATION-SW-COMPONENT-TYPE>"
    )


class TestPortApiOptions:
    """The ``PORT-API-OPTIONS`` container the old writer wrote for every component port.

    One option per P- and R-port, sorted by port name case-insensitively and stably over the document's
    port order, each with both flags ``false`` and a ``PORT-REF`` with its kind as ``DEST``, before
    ``RUNNABLES`` or, with none, before ``SUPPORTS-MULTIPLE-INSTANTIATION``.
    """

    _PORTS = (
        ("P-PORT-PROTOTYPE", "b"),
        ("P-PORT-PROTOTYPE", "x"),
        ("R-PORT-PROTOTYPE", "A"),
        ("R-PORT-PROTOTYPE", "X"),
        ("R-PORT-PROTOTYPE", "c"),
    )
    #: Case-insensitive and stable: ``x`` (provide, first in the document) stays before ``X``.
    _SORTED = (
        ("R-PORT-PROTOTYPE", "A"),
        ("P-PORT-PROTOTYPE", "b"),
        ("R-PORT-PROTOTYPE", "c"),
        ("P-PORT-PROTOTYPE", "x"),
        ("R-PORT-PROTOTYPE", "X"),
    )

    def _ports(self) -> str:
        return "".join(_port(kind, name) for kind, name in self._PORTS)

    def _options(self) -> str:
        return "".join(_option(kind, name) for kind, name in self._SORTED)

    @pytest.mark.parametrize("runnables", [True, False], ids=["before-runnables", "before-the-tail"])
    def test_the_container_comes_back_in_the_old_order_and_position(self, runnables: bool) -> None:
        new = _document(_behaving_component(self._ports(), None, runnables=runnables))
        old = _document(_behaving_component(self._ports(), self._options(), runnables=runnables))
        assert _strip(new) == (old, {"port-api-options": 1})

    def test_a_behaviour_holding_a_container_is_left_alone(self) -> None:
        """Even one that is not what the old writer wrote: the reversal restores only an absent container."""
        partial = _option("R-PORT-PROTOTYPE", "A")
        document = _document(_behaving_component(self._ports(), partial, runnables=True))
        assert _strip(document) == (document, {})

    def test_a_component_without_a_port_gets_none(self) -> None:
        document = _document(_behaving_component(None, None, runnables=True))
        assert _strip(document) == (document, {})

    def test_a_behaviour_with_neither_anchor_is_refused(self) -> None:
        document = _document(_behaving_component(self._ports(), None, runnables=False, tail=False))
        with pytest.raises(FidelityStripError, match="neither RUNNABLES nor SUPPORTS-MULTIPLE-INSTANTIATION"):
            strip_fidelity(document)

    def test_a_port_kind_the_old_writer_never_wrote_is_refused(self) -> None:
        ports = self._ports() + "<PR-PORT-PROTOTYPE><SHORT-NAME>pr</SHORT-NAME></PR-PORT-PROTOTYPE>"
        document = _document(_behaving_component(ports, None, runnables=True))
        with pytest.raises(FidelityStripError, match="PR-PORT-PROTOTYPE"):
            strip_fidelity(document)

    @pytest.mark.parametrize(("tree", "version"), _CASES, ids=_IDS)
    def test_a_committed_trees_containers_removed_come_back_byte_for_byte(
        self, tree: str, version: str, tmp_path: Path
    ) -> None:
        """On the strip's own old form of every committed tree: remove every container, and the strip restores each.

        A **self-consistency** check, not a proof against the old writer: the "old form" is the strip's
        own output. The independent anchor is ``RAW_BEFORE`` in
        ``test_arxml_symbol_and_port_init_writer.py``, the digests of the old writer's own output.
        """
        old_form = strip_fidelity(_generated(tree, version, tmp_path))
        root = parse(old_form)
        containers = list(root.iter(f"{{{AR_NAMESPACE}}}PORT-API-OPTIONS"))
        for container in containers:
            container.getparent().remove(container)
        expected = {"port-api-options": len(containers)} if containers else {}
        assert _strip(serialize(root)) == (old_form, expected)


# value-forms: an application value goes back to the old numerical one

#: A ``TEXTTABLE`` the way the writer writes one (``_emit_text_table``): per scale a ``SHORT-LABEL``,
#: two closed limits and a ``COMPU-CONST/VT``, each the literal's name or value.
_MODE_TABLE = (
    "<COMPU-METHOD><SHORT-NAME>Mode_CompuMethod</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY>"
    "<COMPU-INTERNAL-TO-PHYS><COMPU-SCALES>"
    + "".join(
        f"<COMPU-SCALE><SHORT-LABEL>{name}</SHORT-LABEL>"
        f'<LOWER-LIMIT INTERVAL-TYPE="CLOSED">{value}</LOWER-LIMIT>'
        f'<UPPER-LIMIT INTERVAL-TYPE="CLOSED">{value}</UPPER-LIMIT>'
        f"<COMPU-CONST><VT>{name}</VT></COMPU-CONST></COMPU-SCALE>"
        for name, value in (("OFF", 0), ("ON", 1))
    )
    + "</COMPU-SCALES></COMPU-INTERNAL-TO-PHYS></COMPU-METHOD>"
)

_UNIT = '<UNIT-REF DEST="UNIT">/DataTypes/Speed_T_Unit</UNIT-REF>'

#: An element the serializer writes in its two-line empty form, beside the values: the reversal
#: replaces value nodes in place and must leave its indentation as the writer gave it.
_EMPTY_NEIGHBOUR = (
    "<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>Empty_T</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
    "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
    "</APPLICATION-PRIMITIVE-DATA-TYPE>"
)


def _application(leaf: str, *, label: str | None = None, unit: bool = False, category: str = "VALUE") -> str:
    """The application shape: ``SHORT-LABEL`` (a record member only), ``CATEGORY``, then ``SW-VALUE-CONT``."""
    short_label = "" if label is None else f"<SHORT-LABEL>{label}</SHORT-LABEL>"
    return (
        f"<APPLICATION-VALUE-SPECIFICATION>{short_label}<CATEGORY>{category}</CATEGORY>"
        f"<SW-VALUE-CONT>{_UNIT if unit else ''}<SW-VALUES-PHYS>{leaf}</SW-VALUES-PHYS></SW-VALUE-CONT>"
        "</APPLICATION-VALUE-SPECIFICATION>"
    )


def _numerical(value: str, *, label: str | None = None) -> str:
    """The old writer's leaf: ``SHORT-LABEL`` (a record member only), then ``VALUE``."""
    short_label = "" if label is None else f"<SHORT-LABEL>{label}</SHORT-LABEL>"
    return f"<NUMERICAL-VALUE-SPECIFICATION>{short_label}<VALUE>{value}</VALUE></NUMERICAL-VALUE-SPECIFICATION>"


def _constant(name: str, value: str) -> str:
    return (
        f"<CONSTANT-SPECIFICATION><SHORT-NAME>{name}</SHORT-NAME>"
        f"<VALUE-SPEC>{value}</VALUE-SPEC></CONSTANT-SPECIFICATION>"
    )


def _record(*members: str) -> str:
    return f"<RECORD-VALUE-SPECIFICATION><FIELDS>{''.join(members)}</FIELDS></RECORD-VALUE-SPECIFICATION>"


def _array(*members: str) -> str:
    return f"<ARRAY-VALUE-SPECIFICATION><ELEMENTS>{''.join(members)}</ELEMENTS></ARRAY-VALUE-SPECIFICATION>"


def _value_forms(*constants: str, table: str = _MODE_TABLE) -> bytes:
    return _document(table + _EMPTY_NEIGHBOUR + "".join(constants))


class TestValueForms:
    """An ``APPLICATION-VALUE-SPECIFICATION`` goes back to the old ``NUMERICAL-VALUE-SPECIFICATION``.

    Its ``V`` becomes the ``VALUE`` and a ``VT`` the ``LOWER-LIMIT`` of the one scale with that text; it keeps its
    ``SHORT-LABEL`` and drops ``CATEGORY`` and ``UNIT-REF``. Every other shape is refused.
    """

    def test_each_new_form_goes_back_to_the_numerical_form(self) -> None:
        new = _value_forms(
            _constant("Top_Init", _application("<V>12.5</V>", unit=True)),
            _constant("Bare_Init", _application("<V>-3</V>")),
            _constant("Mode_Init", _application("<VT>ON</VT>")),
            _constant(
                "Rec_Init",
                _record(
                    _application("<V>7</V>", label="speed", unit=True),
                    _numerical("2", label="count"),
                    _application("<VT>OFF</VT>", label="mode"),
                ),
            ),
            _constant("Arr_Init", _array(_application("<VT>ON</VT>"), _numerical("4"), _application("<V>0.5</V>"))),
        )
        old = _value_forms(
            _constant("Top_Init", _numerical("12.5")),
            _constant("Bare_Init", _numerical("-3")),
            _constant("Mode_Init", _numerical("1")),
            _constant(
                "Rec_Init",
                _record(_numerical("7", label="speed"), _numerical("2", label="count"), _numerical("0", label="mode")),
            ),
            _constant("Arr_Init", _array(_numerical("1"), _numerical("4"), _numerical("0.5"))),
        )
        assert _strip(new) == (old, {"value-forms": 7})

    def test_the_empty_neighbour_keeps_its_two_line_form(self) -> None:
        new = _value_forms(_constant("Mode_Init", _application("<VT>ON</VT>")))
        stripped, _ = _strip(new)
        assert b"<SW-DATA-DEF-PROPS-VARIANTS>\n" in stripped
        assert stripped == _value_forms(_constant("Mode_Init", _numerical("1")))

    def test_the_old_form_is_left_alone(self) -> None:
        old = _value_forms(
            _constant("Top_Init", _numerical("12.5")), _constant("Rec_Init", _record(_numerical("1", label="a")))
        )
        assert _strip(old) == (old, {})

    def test_one_text_on_two_scales_of_one_limit_is_mapped(self) -> None:
        twin = _MODE_TABLE.replace("Mode_CompuMethod", "Other_CompuMethod")
        new = _value_forms(_constant("Mode_Init", _application("<VT>ON</VT>")), table=_MODE_TABLE + twin)
        old = _value_forms(_constant("Mode_Init", _numerical("1")), table=_MODE_TABLE + twin)
        assert _strip(new) == (old, {"value-forms": 1})

    def test_a_text_on_no_scale_is_refused(self) -> None:
        with pytest.raises(FidelityStripError, match="names no scale"):
            strip_fidelity(_value_forms(_constant("Mode_Init", _application("<VT>STANDBY</VT>"))))

    def test_a_text_on_scales_of_different_limits_is_refused(self) -> None:
        other = _MODE_TABLE.replace("Mode_CompuMethod", "Other_CompuMethod").replace(">1<", ">5<")
        document = _value_forms(_constant("Mode_Init", _application("<VT>ON</VT>")), table=_MODE_TABLE + other)
        with pytest.raises(FidelityStripError, match="scales of different limits"):
            strip_fidelity(document)

    def test_a_text_on_a_scale_of_another_category_is_not_looked_up(self) -> None:
        linear = _MODE_TABLE.replace("TEXTTABLE", "SCALE_LINEAR_AND_TEXTTABLE")
        with pytest.raises(FidelityStripError, match="names no scale"):
            strip_fidelity(_value_forms(_constant("Mode_Init", _application("<VT>ON</VT>")), table=linear))

    def test_a_text_value_is_refused(self) -> None:
        text = "<TEXT-VALUE-SPECIFICATION><VALUE>ON</VALUE></TEXT-VALUE-SPECIFICATION>"
        with pytest.raises(FidelityStripError, match="TEXT-VALUE-SPECIFICATION"):
            strip_fidelity(_value_forms(_constant("Mode_Init", text)))

    @pytest.mark.parametrize(
        ("value", "match"),
        [
            (_application("<V>1</V>", category="CURVE"), "CATEGORY CURVE"),
            (
                "<APPLICATION-VALUE-SPECIFICATION><SW-VALUE-CONT><SW-VALUES-PHYS><V>1</V></SW-VALUES-PHYS>"
                "</SW-VALUE-CONT></APPLICATION-VALUE-SPECIFICATION>",
                "shape",
            ),
            (
                "<APPLICATION-VALUE-SPECIFICATION><CATEGORY>VALUE</CATEGORY></APPLICATION-VALUE-SPECIFICATION>",
                "shape",
            ),
            (
                "<APPLICATION-VALUE-SPECIFICATION><CATEGORY>VALUE</CATEGORY><SW-AXIS-CONTS></SW-AXIS-CONTS>"
                "<SW-VALUE-CONT><SW-VALUES-PHYS><V>1</V></SW-VALUES-PHYS></SW-VALUE-CONT>"
                "</APPLICATION-VALUE-SPECIFICATION>",
                "shape",
            ),
            (
                "<APPLICATION-VALUE-SPECIFICATION><CATEGORY>VALUE</CATEGORY><SHORT-LABEL>a</SHORT-LABEL>"
                "<SW-VALUE-CONT><SW-VALUES-PHYS><V>1</V></SW-VALUES-PHYS></SW-VALUE-CONT>"
                "</APPLICATION-VALUE-SPECIFICATION>",
                "shape",
            ),
            (
                "<APPLICATION-VALUE-SPECIFICATION><CATEGORY>VALUE</CATEGORY><SW-VALUE-CONT>"
                "<SW-ARRAYSIZE><V>1</V></SW-ARRAYSIZE><SW-VALUES-PHYS><V>1</V></SW-VALUES-PHYS></SW-VALUE-CONT>"
                "</APPLICATION-VALUE-SPECIFICATION>",
                "SW-VALUE-CONT",
            ),
            (
                "<APPLICATION-VALUE-SPECIFICATION><CATEGORY>VALUE</CATEGORY><SW-VALUE-CONT>"
                "<UNIT-DISPLAY-NAME>km/h</UNIT-DISPLAY-NAME><SW-VALUES-PHYS><V>1</V></SW-VALUES-PHYS></SW-VALUE-CONT>"
                "</APPLICATION-VALUE-SPECIFICATION>",
                "SW-VALUE-CONT",
            ),
            (
                "<APPLICATION-VALUE-SPECIFICATION><CATEGORY>VALUE</CATEGORY><SW-VALUE-CONT>"
                "<SW-VALUES-PHYS><V>1</V></SW-VALUES-PHYS>" + _UNIT + "</SW-VALUE-CONT>"
                "</APPLICATION-VALUE-SPECIFICATION>",
                "SW-VALUE-CONT",
            ),
            (_application("<V>1</V><V>2</V>"), "SW-VALUES-PHYS"),
            (_application("<V>1</V><VT>ON</VT>"), "SW-VALUES-PHYS"),
            (_application(""), "SW-VALUES-PHYS"),
            (_application("<VF>1</VF>"), "SW-VALUES-PHYS"),
            (_application("<V></V>"), "empty"),
            (_application("<VT>   </VT>"), "empty"),
        ],
        ids=[
            "another-category",
            "no-category",
            "no-value-content",
            "axis-contents",
            "label-after-category",
            "array-size",
            "unit-display-name",
            "unit-ref-after-the-values",
            "two-v",
            "v-beside-vt",
            "no-value",
            "vf",
            "empty-v",
            "blank-vt",
        ],
    )
    def test_any_other_application_shape_is_refused(self, value: str, match: str) -> None:
        with pytest.raises(FidelityStripError, match=match):
            strip_fidelity(_value_forms(_constant("X_Init", value)))

    def test_a_vt_of_the_init_values_tree_goes_back_to_the_old_writers_leaf_text(self, tmp_path: Path) -> None:
        """A scale's ``LOWER-LIMIT`` text is the text the old writer gave the leaf (``format_init_number``).

        The init values tree's enum-typed constant is rewritten by hand into the new form, and the
        reversal must give back today's bytes, at both versions.
        """
        for version in ("4.2.2", "4.3.0"):
            output = tmp_path / f"init-{version}.arxml"
            generate_arxml(str(INIT_VALUES_MODEL_ROOT), str(output), version=version)
            today = strip_fidelity(output.read_bytes())
            root = parse(today)
            scales = list(root.iter(f"{_AR}COMPU-SCALE"))
            assert scales, "the init values tree writes a texttable"
            for scale in scales:
                limit = scale.findtext(f"{_AR}LOWER-LIMIT")
                assert limit == format_init_number(int(limit))
            constant = next(
                node
                for node in root.iter(f"{_AR}CONSTANT-SPECIFICATION")
                if node.findtext(f"{_AR}SHORT-NAME") == "ISignals_mode_Init"
            )
            leaf = constant.find(f"{_AR}VALUE-SPEC/{_AR}NUMERICAL-VALUE-SPECIFICATION")
            literal = next(
                scale.findtext(f"{_AR}COMPU-CONST/{_AR}VT")
                for scale in scales
                if scale.findtext(f"{_AR}LOWER-LIMIT") == leaf.findtext(f"{_AR}VALUE")
            )
            new_leaf = etree.fromstring(
                f'<APPLICATION-VALUE-SPECIFICATION xmlns="{AR_NAMESPACE}"><CATEGORY>VALUE</CATEGORY>'
                f"<SW-VALUE-CONT><SW-VALUES-PHYS><VT>{literal}</VT></SW-VALUES-PHYS></SW-VALUE-CONT>"
                "</APPLICATION-VALUE-SPECIFICATION>"
            )
            leaf.getparent().replace(leaf, new_leaf)
            assert strip_counts(serialize(root)) == (
                today,
                {**dict.fromkeys(strip_counts(today)[1], 0), "value-forms": 1},
            )


@pytest.mark.parametrize(("tree", "version"), _CASES, ids=_IDS)
def test_the_value_forms_reversal_is_the_identity_on_a_committed_trees_output(
    tree: str, version: str, tmp_path: Path
) -> None:
    """The value-forms reversal leaves every stripped tree's output as it is.

    The init values tree's raw output states three enumeration leaves as literal text, which the reversal maps back, and
    nothing else.
    """
    raw = _generated(tree, version, tmp_path)
    root = parse(raw)
    assert restore_numerical_values(root) == (3 if tree == INIT_VALUES_TREE else 0)
    if tree != INIT_VALUES_TREE:
        assert serialize(root) == raw
    stripped = strip_fidelity(raw)
    root = parse(stripped)
    assert restore_numerical_values(root) == 0
    assert serialize(root) == stripped


@pytest.mark.parametrize("golden", sorted(path.name for path in GOLDEN_DIR.glob("*.arxml")))
def test_the_value_forms_reversal_is_the_identity_on_a_committed_golden(golden: str) -> None:
    data = (GOLDEN_DIR / golden).read_bytes()
    root = parse(data)
    assert restore_numerical_values(root) == 0
    assert serialize(root) == data
