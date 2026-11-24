"""The external oracle's own semantics, on inline exports.

``arxml_export_oracle`` is the independent counter the importer is compared against; an oracle nobody
checks is a second implementation of the same guess.

What these tests do **not** reach: the real tool exports, the module's cost on a large file, and whether
the importer agrees with the oracle.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arxml_export_oracle import (
    CAUSE_CUT_PART_WAY,
    CAUSE_LEAF_ONLY,
    CAUSE_NO_TREE,
    CONFLICTING,
    MERGED,
    child_text,
    complete_package_selection,
    component_packages,
    dangling_references,
    duplicate_groups,
    has_descendant,
    package_selected,
    read_export,
    selection_closure,
)

_NS = 'xmlns="http://autosar.org/schema/r4.0"'


def _export(tmp_path: Path, body: str, name: str = "inline.arxml") -> Path:
    """One export file whose packages are *body*, in the AUTOSAR namespace a tool export declares."""
    path = tmp_path / name
    path.write_text(
        f"<AUTOSAR {_NS}><AR-PACKAGES>{body}</AR-PACKAGES></AUTOSAR>",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _value_type(name: str, base: str, description: str = "") -> str:
    desc = f"<DESC><L-2 L='EN'>{description}</L-2></DESC>" if description else ""
    return (
        f"<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME>{desc}"
        f"<CATEGORY>VALUE</CATEGORY>"
        f"<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
        f"<BASE-TYPE-REF DEST='SW-BASE-TYPE'>{base}</BASE-TYPE-REF>"
        f"</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
        f"</IMPLEMENTATION-DATA-TYPE>"
    )


def _base_type(name: str, size: str = "8") -> str:
    return (
        f"<SW-BASE-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>FIXED_LENGTH</CATEGORY>"
        f"<BASE-TYPE-SIZE>{size}</BASE-TYPE-SIZE>"
        f"<BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>"
        f"<NATIVE-DECLARATION>uint{size}</NATIVE-DECLARATION></SW-BASE-TYPE>"
    )


def _package(name: str, elements: str) -> str:
    return f"<AR-PACKAGE><SHORT-NAME>{name}</SHORT-NAME><ELEMENTS>{elements}</ELEMENTS></AR-PACKAGE>"


class TestWhatCountsAsADeclaration:
    def test_a_record_element_is_not_a_declaration(self, tmp_path: Path) -> None:
        """``<ELEMENTS>`` is not a package-only container: a record type's fields sit under one too.

        Testing the tag alone made every repeated field name a phantom duplicate declaration.
        """
        record = (
            "<APPLICATION-RECORD-DATA-TYPE><SHORT-NAME>{outer}</SHORT-NAME><ELEMENTS>"
            "<APPLICATION-RECORD-ELEMENT><SHORT-NAME>Value</SHORT-NAME></APPLICATION-RECORD-ELEMENT>"
            "</ELEMENTS></APPLICATION-RECORD-DATA-TYPE>"
        )
        source = _export(
            tmp_path,
            _package("A", record.format(outer="R1")) + _package("B", record.format(outer="R2")),
        )

        export = read_export(source)

        assert [element.path for element in export.declarations("APPLICATION-RECORD-DATA-TYPE")] == [
            "/A/R1",
            "/B/R2",
        ]
        assert export.resolve("/A/R1/Value") is not None, "a nested name is still indexed"
        assert export.resolve("/A/R1/Value").declaration is False
        assert duplicate_groups(export) == []


class TestTheEqualityRelation:
    def test_a_description_does_not_separate_two_declarations(self, tmp_path: Path) -> None:
        """``DESC`` is excluded: an absent ``DESC`` compares equal to a present one."""
        source = _export(
            tmp_path,
            _package("A", _base_type("T") + _value_type("V", "/A/T"))
            + _package("B", _base_type("T") + _value_type("V", "/B/T", description="documented")),
        )

        groups = {group.key: group for group in duplicate_groups(read_export(source))}

        assert groups["data_types/IMPLEMENTATION-DATA-TYPE/V"].verdict == MERGED

    def test_a_nested_short_name_is_part_of_the_definition(self, tmp_path: Path) -> None:
        """Only the declaration's own name is excluded: differently named elements make different interfaces."""
        interface = (
            "<SENDER-RECEIVER-INTERFACE><SHORT-NAME>I</SHORT-NAME><DATA-ELEMENTS>"
            "<VARIABLE-DATA-PROTOTYPE><SHORT-NAME>{element}</SHORT-NAME></VARIABLE-DATA-PROTOTYPE>"
            "</DATA-ELEMENTS></SENDER-RECEIVER-INTERFACE>"
        )
        source = _export(
            tmp_path,
            _package("A", interface.format(element="speed")) + _package("B", interface.format(element="torque")),
        )

        groups = {group.key: group for group in duplicate_groups(read_export(source))}

        assert groups["interfaces/SENDER-RECEIVER-INTERFACE/I"].verdict == CONFLICTING

    def test_a_reference_into_the_declarations_own_tree_is_compared_resolved(self, tmp_path: Path) -> None:
        """Two copies of a type pointing at two copies of one base type are one declaration, not two."""
        source = _export(
            tmp_path,
            _package("A", _base_type("T") + _value_type("V", "/A/T"))
            + _package("B", _base_type("T") + _value_type("V", "/B/T")),
        )

        groups = {group.key: group for group in duplicate_groups(read_export(source))}

        assert groups["data_types/IMPLEMENTATION-DATA-TYPE/V"].own_bytes_identical is False
        assert groups["data_types/IMPLEMENTATION-DATA-TYPE/V"].locally_equal is True
        assert groups["data_types/IMPLEMENTATION-DATA-TYPE/V"].verdict == MERGED

    def test_a_conflict_is_inherited_through_the_reference(self, tmp_path: Path) -> None:
        """A declaration byte-identical to its twin still conflicts when what it points at conflicts."""
        source = _export(
            tmp_path,
            _package("A", _base_type("T", size="8") + _value_type("V", "/A/T"))
            + _package("B", _base_type("T", size="16") + _value_type("V", "/B/T")),
        )

        groups = {group.key: group for group in duplicate_groups(read_export(source))}

        assert groups["data_types/SW-BASE-TYPE/T"].verdict == CONFLICTING
        value = groups["data_types/IMPLEMENTATION-DATA-TYPE/V"]
        assert value.locally_equal is True, "its own bytes say nothing is wrong"
        assert value.verdict == CONFLICTING
        assert value.inherited_from == "data_types/SW-BASE-TYPE/T"

    def test_a_group_in_a_reference_cycle_is_conflicting_and_named_cyclic(self, tmp_path: Path) -> None:
        """Two groups provable equal only by assuming each other are refused, not merged."""
        cycle = (
            "<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>TYPE_REFERENCE</CATEGORY>"
            "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
            "<IMPLEMENTATION-DATA-TYPE-REF DEST='IMPLEMENTATION-DATA-TYPE'>{target}</IMPLEMENTATION-DATA-TYPE-REF>"
            "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
            "</IMPLEMENTATION-DATA-TYPE>"
        )
        source = _export(
            tmp_path,
            _package("A", cycle.format(name="X", target="/A/Y") + cycle.format(name="Y", target="/A/X"))
            + _package("B", cycle.format(name="X", target="/B/Y") + cycle.format(name="Y", target="/B/X")),
        )

        groups = {group.key: group for group in duplicate_groups(read_export(source))}

        for name in ("X", "Y"):
            group = groups[f"data_types/IMPLEMENTATION-DATA-TYPE/{name}"]
            assert group.locally_equal is True
            assert group.verdict == CONFLICTING
            assert group.cyclic is True


class TestTheDanglingReferences:
    @pytest.mark.parametrize(
        ("target", "cause"),
        [
            ("/Absent/Types/T", CAUSE_NO_TREE),
            ("/A/Missing", CAUSE_LEAF_ONLY),
            ("/A/Deeper/Still/T", CAUSE_CUT_PART_WAY),
        ],
    )
    def test_the_cause_is_the_longest_declared_prefix(self, tmp_path: Path, target: str, cause: str) -> None:
        source = _export(tmp_path, _package("A", _base_type("T") + _value_type("V", target)))

        summary = dangling_references(read_export(source))

        assert [reference.target for reference in summary.unresolved] == [target]
        assert summary.by_cause == {cause: 1}

    def test_a_resolved_reference_whose_dest_disagrees_is_counted_apart(self, tmp_path: Path) -> None:
        """A ``DEST`` naming another tag over a present target is its own finding, not a dangling reference."""
        source = _export(tmp_path, _package("A", _base_type("T") + _value_type("V", "/A/T")))
        text = source.read_text(encoding="utf-8").replace("DEST='SW-BASE-TYPE'", "DEST='IMPLEMENTATION-DATA-TYPE'")
        source.write_text(text, encoding="utf-8", newline="\n")

        summary = dangling_references(read_export(source))

        assert summary.unresolved == []
        assert [reference.target for reference in summary.dest_disagreements] == ["/A/T"]

    def test_a_shadowed_target_is_reported_only_for_a_layer_swcraft_maps(self, tmp_path: Path) -> None:
        """A target is shadowed only by a same-named element of a layer the importer maps.

        A kind the importer does not map cannot shadow, since nothing would bind to it.
        """
        interface = (
            "<SENDER-RECEIVER-INTERFACE><SHORT-NAME>Shared</SHORT-NAME></SENDER-RECEIVER-INTERFACE>"
            "<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>Elsewhere</SHORT-NAME></APPLICATION-PRIMITIVE-DATA-TYPE>"
        )
        port = (
            "<APPLICATION-SW-COMPONENT-TYPE><SHORT-NAME>C</SHORT-NAME><PORTS>"
            "<R-PORT-PROTOTYPE><SHORT-NAME>p</SHORT-NAME>"
            "<REQUIRED-INTERFACE-TREF DEST='SENDER-RECEIVER-INTERFACE'>/Gone/Shared</REQUIRED-INTERFACE-TREF>"
            "</R-PORT-PROTOTYPE>"
            "<R-PORT-PROTOTYPE><SHORT-NAME>q</SHORT-NAME>"
            "<REQUIRED-INTERFACE-TREF DEST='SENDER-RECEIVER-INTERFACE'>/Gone/Elsewhere</REQUIRED-INTERFACE-TREF>"
            "</R-PORT-PROTOTYPE>"
            "</PORTS></APPLICATION-SW-COMPONENT-TYPE>"
        )
        source = _export(tmp_path, _package("A", interface + port))

        summary = dangling_references(read_export(source))

        assert summary.shadowed == {"interfaces": ["/Gone/Shared"]}


class TestTheNamespaceSafeReaders:
    def test_a_property_is_read_through_the_namespace(self, tmp_path: Path) -> None:
        """A namespaced property is read where ``ET``'s own ``findtext`` answers ``None``."""
        source = _export(tmp_path, _package("A", _base_type("T")))
        element = read_export(source).declarations("SW-BASE-TYPE")[0]

        assert element.node.findtext("CATEGORY") is None
        assert child_text(element, "CATEGORY") == "FIXED_LENGTH"
        assert child_text(element, "BYTE-ORDER") is None
        assert has_descendant(element, "NATIVE-DECLARATION") is True
        assert has_descendant(element, "MEM-ALIGNMENT") is False


def _component(name: str, ports: str = "") -> str:
    return (
        f"<APPLICATION-SW-COMPONENT-TYPE><SHORT-NAME>{name}</SHORT-NAME>"
        f"<PORTS>{ports}</PORTS></APPLICATION-SW-COMPONENT-TYPE>"
    )


def _r_port(name: str, interface: str) -> str:
    return (
        f"<R-PORT-PROTOTYPE><SHORT-NAME>{name}</SHORT-NAME>"
        f"<REQUIRED-INTERFACE-TREF DEST='SENDER-RECEIVER-INTERFACE'>{interface}</REQUIRED-INTERFACE-TREF>"
        f"</R-PORT-PROTOTYPE>"
    )


def _interface(name: str, element_type: str) -> str:
    return (
        f"<SENDER-RECEIVER-INTERFACE><SHORT-NAME>{name}</SHORT-NAME><DATA-ELEMENTS>"
        f"<VARIABLE-DATA-PROTOTYPE><SHORT-NAME>e</SHORT-NAME>"
        f"<TYPE-TREF DEST='IMPLEMENTATION-DATA-TYPE'>{element_type}</TYPE-TREF>"
        f"</VARIABLE-DATA-PROTOTYPE></DATA-ELEMENTS></SENDER-RECEIVER-INTERFACE>"
    )


def _composition(name: str, members: str) -> str:
    return (
        f"<COMPOSITION-SW-COMPONENT-TYPE><SHORT-NAME>{name}</SHORT-NAME><COMPONENTS>{members}"
        f"</COMPONENTS></COMPOSITION-SW-COMPONENT-TYPE>"
    )


def _member(name: str, type_ref: str, dest: str = "APPLICATION-SW-COMPONENT-TYPE") -> str:
    return (
        f"<SW-COMPONENT-PROTOTYPE><SHORT-NAME>{name}</SHORT-NAME>"
        f"<TYPE-TREF DEST='{dest}'>{type_ref}</TYPE-TREF></SW-COMPONENT-PROTOTYPE>"
    )


class TestTheCompleteSelection:
    """The closure ``--packages`` walks, and which selections are complete."""

    def _two_packages(self, tmp_path: Path) -> Path:
        """``Clean`` resolves everything; ``Open`` has a port with no interface."""
        shared = _package("Shared", _base_type("T") + _value_type("V", "/Shared/T") + _interface("I", "/Shared/V"))
        clean = _package("Clean", _component("A", _r_port("p", "/Shared/I")))
        open_package = _package("Open", _component("B", _r_port("p", "/Gone/I")))
        return _export(tmp_path, shared + clean + open_package)

    def test_a_package_whose_closure_resolves_is_complete_and_one_whose_port_dangles_is_not(
        self, tmp_path: Path
    ) -> None:
        export = read_export(self._two_packages(tmp_path))

        assert selection_closure(export, {"/Clean"}).complete is True
        open_closure = selection_closure(export, {"/Open"})
        assert open_closure.complete is False
        assert [reference.target for reference in open_closure.unresolved] == ["/Gone/I"]
        assert complete_package_selection(export) == ["/Clean"]
        assert component_packages(export) == ["/Clean", "/Open"]

    def test_the_closure_reaches_the_interface_and_the_types_under_it(self, tmp_path: Path) -> None:
        """A kept port pulls in its interface, its data elements' types and their bases."""
        export = read_export(self._two_packages(tmp_path))

        closure = selection_closure(export, {"/Clean"})

        assert closure.roots == frozenset({"/Clean/A"})
        assert closure.reached == frozenset({"/Clean/A", "/Shared/I", "/Shared/V", "/Shared/T"})

    def test_a_member_of_an_absent_type_is_a_stub_and_not_an_incompleteness(self, tmp_path: Path) -> None:
        """An unresolved prototype ``TYPE-TREF`` is the importer's ``external`` stub, not an incompleteness."""
        body = _package("Top", _composition("C", _member("m", "/Nowhere/Missing")))
        export = read_export(_export(tmp_path, body))

        closure = selection_closure(export, {"/Top"})

        assert closure.complete is True
        assert complete_package_selection(export) == ["/Top"]

    def test_a_selection_is_incomplete_through_a_member_it_places(self, tmp_path: Path) -> None:
        """A dangling port on a placed component makes the selection incomplete: completeness is the closure's."""
        shared = _package("Shared", _component("B", _r_port("p", "/Gone/I")))
        top = _package("Top", _composition("C", _member("m", "/Shared/B")))
        export = read_export(_export(tmp_path, shared + top))

        assert selection_closure(export, {"/Top"}).complete is False
        assert complete_package_selection(export) == []

    def test_a_bare_package_name_selects_at_any_depth(self, tmp_path: Path) -> None:
        """A bare short-name selects at any depth; the oracle re-implements the predicate rather than importing it."""
        inner = _package("Inner", _component("A"))
        nested = f"<AR-PACKAGE><SHORT-NAME>Outer</SHORT-NAME><AR-PACKAGES>{inner}</AR-PACKAGES></AR-PACKAGE>"
        export = read_export(_export(tmp_path, nested))

        assert package_selected("/Outer/Inner/A", {"Inner"}) is True
        assert package_selected("/Outer/Inner/A", {"/Outer"}) is True
        assert package_selected("/Outer/Inner/A", {"A"}) is False, "an element's own leaf never selects it"
        assert selection_closure(export, {"Inner"}).roots == frozenset({"/Outer/Inner/A"})
