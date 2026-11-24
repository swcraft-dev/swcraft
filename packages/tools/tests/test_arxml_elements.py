"""The ARXML element walk and comparison, against hand-written ARXML texts.

Every text below is written by hand, never by the writer, so the comparison is
not checked against the code it reads. The expectations are the sentences of the leaf's docstring: a path
``Package/…/SHORT-NAME``, the tag, canonical bytes with blank text removed, the
new document's order and then the removed elements in the old document's.
"""

from __future__ import annotations

import pytest
from lxml import etree
from swcraft_tools.arxml_elements import (
    ArxmlElementChange,
    arxml_elements,
    compare_arxml_elements,
    declared_schema_location,
    parse_arxml,
)

_HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n'
_ROOT_OPEN = '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">'
_ROOT_WITH_SCHEMA = (
    '<AUTOSAR xmlns="http://autosar.org/schema/r4.0" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">'
)


def _document(packages: str, root: str = _ROOT_OPEN) -> str:
    return f"{_HEAD}{root}<AR-PACKAGES>{packages}</AR-PACKAGES></AUTOSAR>"


def _package(name: str, elements: str = "", subpackages: str = "") -> str:
    body = f"<SHORT-NAME>{name}</SHORT-NAME>"
    if elements:
        body += f"<ELEMENTS>{elements}</ELEMENTS>"
    if subpackages:
        body += f"<AR-PACKAGES>{subpackages}</AR-PACKAGES>"
    return f"<AR-PACKAGE>{body}</AR-PACKAGE>"


def _interface(name: str, *, service: str = "false", extra: str = "") -> str:
    return (
        f"<SENDER-RECEIVER-INTERFACE{extra}><SHORT-NAME>{name}</SHORT-NAME>"
        f"<IS-SERVICE>{service}</IS-SERVICE></SENDER-RECEIVER-INTERFACE>"
    )


def _component(name: str) -> str:
    return f"<APPLICATION-SW-COMPONENT-TYPE><SHORT-NAME>{name}</SHORT-NAME></APPLICATION-SW-COMPONENT-TYPE>"


class TestTheWalk:
    def test_a_nested_package_gives_the_path_package_sub_short_name(self) -> None:
        text = _document(
            _package("Top", _interface("ITop"), _package("Sub", _component("Swc"), _package("Deep", _interface("I"))))
        )

        found = arxml_elements(parse_arxml(text))

        assert list(found) == ["Top/ITop", "Top/Sub/Swc", "Top/Sub/Deep/I"]
        assert [tag for tag, _ in found.values()] == [
            "SENDER-RECEIVER-INTERFACE",
            "APPLICATION-SW-COMPONENT-TYPE",
            "SENDER-RECEIVER-INTERFACE",
        ]

    def test_the_bytes_are_canonical_with_blank_text_removed(self) -> None:
        compact = _document(_package("P", _interface("I")))
        spaced = _document(_package("P", _interface("I").replace("><", ">\n    <")))

        assert arxml_elements(parse_arxml(compact)) == arxml_elements(parse_arxml(spaced))
        assert arxml_elements(parse_arxml(compact))["P/I"][1].startswith(b"<SENDER-RECEIVER-INTERFACE")

    def test_bytes_and_text_give_the_same_walk(self) -> None:
        text = _document(_package("P", _interface("I")))

        assert arxml_elements(parse_arxml(text.encode("utf-8"))) == arxml_elements(parse_arxml(text))

    def test_a_package_without_elements_contributes_no_row(self) -> None:
        assert arxml_elements(parse_arxml(_document(_package("Empty")))) == {}

    def test_a_text_that_is_no_xml_document_raises_the_parsers_error(self) -> None:
        with pytest.raises(etree.XMLSyntaxError):
            parse_arxml("this is no XML document")

    def test_bytes_in_another_encoding_than_declared_raise_the_parsers_error(self) -> None:
        latin = _document(_package("P", _interface("Ünterface"))).encode("latin-1")

        with pytest.raises(etree.XMLSyntaxError):
            parse_arxml(latin)


class TestTheComparison:
    def test_an_added_a_removed_and_a_changed_element_in_their_order(self) -> None:
        before = _document(_package("P", _interface("Kept") + _interface("Gone") + _interface("Moved")))
        after = _document(_package("P", _interface("New") + _interface("Moved", service="true") + _interface("Kept")))

        assert compare_arxml_elements(parse_arxml(before), parse_arxml(after)) == (
            ArxmlElementChange("P/New", "SENDER-RECEIVER-INTERFACE", "added"),
            ArxmlElementChange("P/Moved", "SENDER-RECEIVER-INTERFACE", "changed"),
            ArxmlElementChange("P/Gone", "SENDER-RECEIVER-INTERFACE", "removed"),
        )

    def test_one_attribute_changed_is_a_change(self) -> None:
        before = _document(_package("P", _interface("I")))
        after = _document(_package("P", _interface("I", extra=' UUID="1"')))

        assert compare_arxml_elements(parse_arxml(before), parse_arxml(after)) == (
            ArxmlElementChange("P/I", "SENDER-RECEIVER-INTERFACE", "changed"),
        )

    def test_one_child_text_changed_is_a_change(self) -> None:
        before = _document(_package("P", _interface("I", service="false")))
        after = _document(_package("P", _interface("I", service="true")))

        assert compare_arxml_elements(parse_arxml(before), parse_arxml(after)) == (
            ArxmlElementChange("P/I", "SENDER-RECEIVER-INTERFACE", "changed"),
        )

    def test_a_changed_tag_at_the_same_path_is_a_change(self) -> None:
        before = _document(_package("P", _interface("X")))
        after = _document(_package("P", _component("X")))

        assert compare_arxml_elements(parse_arxml(before), parse_arxml(after)) == (
            ArxmlElementChange("P/X", "APPLICATION-SW-COMPONENT-TYPE", "changed"),
        )

    def test_unchanged_with_different_blank_text_is_no_change(self) -> None:
        before = _document(_package("P", _interface("I")))
        after = _document(_package("P", _interface("I").replace("><", ">\n\t<")))

        assert compare_arxml_elements(parse_arxml(before), parse_arxml(after)) == ()

    def test_removed_elements_follow_in_the_old_documents_order(self) -> None:
        before = _document(_package("P", _interface("B") + _interface("A")) + _package("Q", _component("C")))
        after = _document(_package("Q", _component("Z")))

        assert [
            (change.path, change.change) for change in compare_arxml_elements(parse_arxml(before), parse_arxml(after))
        ] == [
            ("Q/Z", "added"),
            ("P/B", "removed"),
            ("P/A", "removed"),
            ("Q/C", "removed"),
        ]


class TestTheDeclaredSchema:
    def test_a_root_with_a_schema_location_names_it(self) -> None:
        text = _document(_package("P"), root=_ROOT_WITH_SCHEMA)

        assert declared_schema_location(parse_arxml(text)) == "http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd"
        assert (
            declared_schema_location(parse_arxml(text.encode("utf-8")))
            == "http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd"
        )

    def test_a_root_without_a_schema_location_names_none(self) -> None:
        assert declared_schema_location(parse_arxml(_document(_package("P")))) is None

    def test_an_unclosed_text_is_no_xml_document_for_the_parser(self) -> None:
        with pytest.raises(etree.XMLSyntaxError):
            parse_arxml("<AUTOSAR>")
