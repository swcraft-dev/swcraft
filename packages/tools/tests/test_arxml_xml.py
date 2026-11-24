"""``writer/arxml_xml.py`` -- the first-party ARXML serialization primitives.

Pinned against the goldens, because the primitives are where a byte difference is cheapest to catch:

* the empty-element form: never self-closed, never collapsed onto one line (``lxml`` self-closes by
  default);
* the XML prolog with *double* quotes and lowercase ``utf-8`` (``lxml`` uses single quotes), then
  ``xmlns``/``xmlns:xsi``/``xsi:schemaLocation`` in that order;
* the lazy ``ELEMENTS`` wrapper: an empty AR-PACKAGE emits none (``empty-4.2.2.arxml``);
* exact timing: ``period`` and ``min_start_interval`` go through ``format_float`` with no millisecond
  grid, so a sub-millisecond value is written as itself.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest
from conftest import golden_slice, read_golden
from lxml import etree
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.writer.arxml_xml import (
    AR_NAMESPACE,
    INDENT,
    XML_DECLARATION,
    XSI_NAMESPACE,
    ArxmlDocument,
    ar_qname,
    find_element,
    format_boolean,
    format_float,
    format_init_number,
    format_integer,
    format_value,
    make_element,
    move_previous_arxml,
    named_element,
    reference_element,
    serialize_document,
    serialize_fragment,
    set_short_name,
    short_name_of,
    sub_element,
)


def _swc_with_empty_ports(document: ArxmlDocument) -> None:
    """Add a port-less SWC — an *empty* ``PORTS`` element."""
    swc = document.component_types.create_element("APPLICATION-SW-COMPONENT-TYPE", "PortlessSwc")
    sub_element(swc, "PORTS")


class TestQualifiedNames:
    """Tags are namespace-qualified in the tree but prefix-free in the output."""

    def test_ar_qname_qualifies_with_the_autosar_namespace(self):
        assert ar_qname("SHORT-NAME") == f"{{{AR_NAMESPACE}}}SHORT-NAME"

    def test_created_elements_carry_the_qualified_tag(self):
        assert make_element("SHORT-NAME").tag == ar_qname("SHORT-NAME")

    def test_namespaces_are_declared_once_on_the_root(self):
        text = ArxmlDocument().serialize()
        assert text.count(f'xmlns="{AR_NAMESPACE}"') == 1
        assert text.count(f'xmlns:xsi="{XSI_NAMESPACE}"') == 1

    def test_child_elements_are_written_without_a_prefix(self):
        document = ArxmlDocument()
        document.data_types.create_element("SW-BASE-TYPE", "float32")
        text = document.serialize()
        assert "<SW-BASE-TYPE>" in text
        assert "xmlns" not in text.split("\n", 2)[2]


class TestElementCreation:
    """``make_element``/``sub_element``/``named_element`` and SHORT-NAME placement."""

    def test_sub_element_appends_in_call_order(self):
        parent = make_element("SW-BASE-TYPE")
        sub_element(parent, "CATEGORY", "FIXED_LENGTH")
        sub_element(parent, "BASE-TYPE-SIZE", 32)
        assert serialize_fragment(parent) == (
            "<SW-BASE-TYPE>\n"
            f"{INDENT}<CATEGORY>FIXED_LENGTH</CATEGORY>\n"
            f"{INDENT}<BASE-TYPE-SIZE>32</BASE-TYPE-SIZE>\n"
            "</SW-BASE-TYPE>"
        )

    def test_named_element_writes_short_name_first(self):
        parent = make_element("ELEMENTS")
        named_element(parent, "SW-BASE-TYPE", "float32")
        assert serialize_fragment(parent).splitlines()[2].strip() == "<SHORT-NAME>float32</SHORT-NAME>"

    def test_set_short_name_inserts_before_existing_children(self):
        # golden_slice() and every reader that identifies an element by name rely on this.
        node = make_element("SW-BASE-TYPE")
        sub_element(node, "CATEGORY", "FIXED_LENGTH")
        set_short_name(node, "float32")
        assert serialize_fragment(node).splitlines()[1].strip() == "<SHORT-NAME>float32</SHORT-NAME>"

    def test_set_short_name_rejects_a_second_short_name(self):
        node = named_element(make_element("ELEMENTS"), "SW-BASE-TYPE", "float32")
        with pytest.raises(ValueError, match="SHORT-NAME"):
            set_short_name(node, "float64")

    def test_text_is_escaped(self):
        node = make_element("NATIVE-DECLARATION", text='a & b < c > d "e"')
        assert serialize_fragment(node) == '<NATIVE-DECLARATION>a &amp; b &lt; c &gt; d "e"</NATIVE-DECLARATION>'

    def test_attribute_values_are_escaped(self):
        node = reference_element(make_element("HOLDER"), "TYPE-TREF", "/DataTypes/x", dest='A"B')
        assert 'DEST="A&quot;B"' in serialize_fragment(node.getparent())


class TestReferenceElements:
    """Every reference carries a ``DEST``; a missing one is refused, never emitted."""

    def test_reference_emits_dest_and_path(self):
        parent = make_element("SW-DATA-DEF-PROPS-CONDITIONAL")
        reference_element(parent, "BASE-TYPE-REF", "/DataTypes/float32", dest="SW-BASE-TYPE")
        assert serialize_fragment(parent) == (
            "<SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            f'{INDENT}<BASE-TYPE-REF DEST="SW-BASE-TYPE">/DataTypes/float32</BASE-TYPE-REF>\n'
            "</SW-DATA-DEF-PROPS-CONDITIONAL>"
        )

    @pytest.mark.parametrize("dest", [None, ""])
    def test_an_absent_dest_raises_rather_than_emitting_an_unresolvable_reference(self, dest):
        # arxml_reader.py resolves references by their DEST, so a DEST-less one could not be read back.
        parent = make_element("SW-DATA-DEF-PROPS-CONDITIONAL")
        with pytest.raises(ValueError, match="DEST"):
            reference_element(parent, "BASE-TYPE-REF", "/DataTypes/float32", dest=dest)
        assert len(parent) == 0

    def test_dest_is_keyword_only_so_omitting_it_is_a_type_error(self):
        parent = make_element("SW-DATA-DEF-PROPS-CONDITIONAL")
        with pytest.raises(TypeError):
            reference_element(parent, "BASE-TYPE-REF", "/DataTypes/float32")  # type: ignore[call-arg]

    @pytest.mark.parametrize("path", [None, ""])
    def test_an_empty_reference_path_raises(self, path):
        parent = make_element("SW-DATA-DEF-PROPS-CONDITIONAL")
        with pytest.raises(ValueError, match="path"):
            reference_element(parent, "BASE-TYPE-REF", path, dest="SW-BASE-TYPE")
        assert len(parent) == 0


class TestValueFormatting:
    """Numbers, booleans and exact timing."""

    @pytest.mark.parametrize(("value", "expected"), [(0, "0"), (1, "1"), (32, "32"), (-7, "-7")])
    def test_integers(self, value, expected):
        assert format_integer(value) == expected

    def test_format_integer_refuses_a_boolean(self):
        # bool is a subclass of int; AUTOSAR wants true/false, not 1/0.
        with pytest.raises(TypeError):
            format_integer(True)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.0, "0"),  # golden: <MINIMUM-START-INTERVAL>0</MINIMUM-START-INTERVAL>
            (-0.0, "0"),
            (0.005, "0.005"),
            (0.01, "0.01"),
            (0.02, "0.02"),
            (1.0, "1"),  # integral floats lose their fractional part, as cogu's do
            (1.10, "1.1"),  # normalized: no trailing zero
            (2.5, "2.5"),
            (-0.25, "-0.25"),
        ],
    )
    def test_floats(self, value, expected):
        assert format_float(value) == expected

    @pytest.mark.parametrize("value", [5e-7, 1e21, 0.0000005])
    def test_floats_never_use_scientific_notation(self, value):
        formatted = format_float(value)
        assert "e" not in formatted.lower()

    def test_floats_carry_no_binary_representation_noise(self):
        # 0.007 * 1000 == 6.999999999999999 in binary floating point; Decimal(str(value)) prints the
        # shortest round-tripping form instead.
        assert format_float(0.007) == "0.007"
        assert format_float(7 / 1000) == "0.007"

    @pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
    def test_non_finite_floats_raise(self, value):
        with pytest.raises(ValueError):
            format_float(value)

    def test_booleans(self):
        assert format_boolean(True) == "true"
        assert format_boolean(False) == "false"

    def test_format_value_dispatches_bool_before_int(self):
        assert format_value(True) == "true"
        assert format_value(1) == "1"
        assert format_value(0.5) == "0.5"
        assert format_value("NOT-ACCESSIBLE") == "NOT-ACCESSIBLE"

    def test_format_value_refuses_an_unsupported_type(self):
        with pytest.raises(TypeError):
            format_value([1])  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0.0, "0"),  # composite golden's MINIMUM-START-INTERVAL
            (0.005, "0.005"),  # composite golden's MINIMUM-START-INTERVAL
            (0.01, "0.01"),  # basic golden's PERIOD and MINIMUM-START-INTERVAL
            (0.02, "0.02"),  # composite golden's PERIOD
        ],
    )
    def test_a_whole_millisecond_timing_value_formats_as_the_goldens_record_it(self, seconds, expected):
        # Every committed timing value is whole milliseconds, where the exact form and the retired grid's
        # agree, so no golden moves.
        assert format_float(seconds) == expected

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0.00125, "0.00125"),  # the grid wrote 0.001
            (0.0025, "0.0025"),  # the grid wrote 0.002 (round() ties to even)
            (0.0004, "0.0004"),  # the grid wrote 0: a PERIOD of zero from a valid period
            (0.0125, "0.0125"),  # the grid wrote 0.012
        ],
    )
    def test_a_sub_millisecond_timing_value_is_written_as_itself(self, seconds, expected):
        formatted = format_float(seconds)
        assert formatted == expected
        assert float(formatted) == seconds

    def test_the_millisecond_grid_helpers_are_gone(self):
        # A helper that quantized would change a model value on write.
        from swcraft_tools.writer import arxml_xml

        assert not hasattr(arxml_xml, "to_milliseconds")
        assert not hasattr(arxml_xml, "format_timing")
        assert not hasattr(arxml_xml, "_MILLISECONDS_PER_SECOND")


class TestInitNumberFormatting:
    """``format_init_number`` -- the init-value leaf grammar.

    Deliberately different from :func:`format_float`, which prints an integral float without a point: an
    init value must come back from ``arxml_reader.py`` as the same Python type, and that reader tries
    ``_DECIMAL_LITERAL`` before ``_FLOAT_LITERAL``. So every float row must match the float grammar and not
    the decimal one, asserted as an invariant. The expected texts are hand-derived from the grammar.
    """

    #: The two reader patterns, restated rather than imported so they cannot drift with ``arxml_reader``'s;
    #: ``packages/tools/tests/test_arxml_init_writer.py`` reads the values back through the real reader.
    _DECIMAL_LITERAL = re.compile(r"[+-]?[0-9]+")
    _FLOAT_LITERAL = re.compile(r"[+-]?([0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)([eE][+-]?[0-9]+)?")

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, "0"), (1, "1"), (255, "255"), (-7, "-7"), (2**70, str(2**70))],
    )
    def test_an_integer_is_its_decimal_spelling(self, value, expected):
        assert format_init_number(value) == expected

    def test_an_integer_is_never_re_spelled_in_hexadecimal(self):
        # The radix is dropped on the way in and nothing re-spells it: `0xFF`
        # was read as 255 and leaves as 255.
        assert format_init_number(0xFF) == "255"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.0, "0.0"),
            (-0.0, "0.0"),  # the sign never reaches the file
            (100.0, "100.0"),  # NOT normalize(): that prints `100`
            (2.50, "2.5"),
            (-0.25, "-0.25"),
            (1e-7, "0.0000001"),  # fixed point, never scientific
            (1e20, "100000000000000000000.0"),  # the appended `.0`: 1e+20 has no point
            (-1e20, "-100000000000000000000.0"),
            (0.1 + 0.2, "0.30000000000000004"),
        ],
    )
    def test_a_float_is_its_fixed_point_spelling_with_a_point(self, value, expected):
        assert format_init_number(value) == expected

    @pytest.mark.parametrize(
        "value",
        [0.0, -0.0, 100.0, 2.50, -0.25, 1e-7, 1e20, -1e20, 0.1 + 0.2, 1e-300, 3.4028235e38],
    )
    def test_every_float_reads_back_as_a_float_and_not_as_an_integer(self, value):
        """Type preservation, not just value."""
        text = format_init_number(value)

        assert self._FLOAT_LITERAL.fullmatch(text), text
        assert not self._DECIMAL_LITERAL.fullmatch(text), text
        assert float(text) == value

    @pytest.mark.parametrize("value", [1e-7, 1e20, -1e20, 1e-300, 3.4028235e38])
    def test_a_float_never_uses_scientific_notation(self, value):
        assert "e" not in format_init_number(value).lower()

    def test_the_width_of_a_fixed_point_float_is_unbounded(self):
        """`1e-300` prints 302 characters: the width is unbounded."""
        text = format_init_number(1e-300)

        assert len(text) == 302
        assert text == "0." + "0" * 299 + "1"

    def test_a_boolean_is_refused_so_the_caller_maps_it_first(self):
        # `bool` is an `int` subclass; it is written `1`/`0`, and the mapping
        # belongs to the emitter that knows the declared type — the same
        # division `format_integer` makes.
        with pytest.raises(TypeError):
            format_init_number(True)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            format_init_number(False)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
    def test_a_non_finite_float_is_refused(self, value):
        """A defence, not a path: `allow_inf_nan=False` refuses these at load."""
        with pytest.raises(ValueError):
            format_init_number(value)

    @pytest.mark.parametrize("value", ["3", None, [1], {"a": 1}])
    def test_anything_that_is_not_a_number_is_refused(self, value):
        with pytest.raises(TypeError):
            format_init_number(value)  # type: ignore[arg-type]

    def test_format_float_still_prints_an_integral_float_without_a_point(self):
        """The two formatters differ: the goldens' timing values depend on `format_float`'s behaviour."""
        assert format_float(0.0) == "0"
        assert format_float(100.0) == "100"
        assert format_init_number(0.0) == "0.0"
        assert format_init_number(100.0) == "100.0"


class TestEmptyElementForm:
    """Nothing is ever self-closed, and an empty element spans two lines."""

    def test_an_empty_element_is_written_open_then_close(self):
        node = make_element("PORTS")
        assert serialize_fragment(node) == "<PORTS>\n</PORTS>"

    def test_a_nested_empty_element_closes_at_its_own_indentation(self):
        parent = make_element("APPLICATION-SW-COMPONENT-TYPE")
        set_short_name(parent, "PortlessSwc")
        sub_element(parent, "PORTS")
        assert serialize_fragment(parent) == (
            "<APPLICATION-SW-COMPONENT-TYPE>\n"
            f"{INDENT}<SHORT-NAME>PortlessSwc</SHORT-NAME>\n"
            f"{INDENT}<PORTS>\n"
            f"{INDENT}</PORTS>\n"
            "</APPLICATION-SW-COMPONENT-TYPE>"
        )

    def test_no_self_closing_tag_anywhere_in_a_document(self):
        document = ArxmlDocument()
        _swc_with_empty_ports(document)
        assert "/>" not in document.serialize()

    @pytest.mark.parametrize("golden", ["basic-4.2.2", "composite-4.2.2", "empty-4.2.2"])
    def test_the_goldens_confirm_the_rule(self, golden):
        assert "/>" not in read_golden(golden)


class TestAnEmptyStringIsAValueNotAnEmptyElement:
    """``text == ""`` is a model-supplied value; only ``text is None`` is a wrapper.

    Guards an expansion that tested falsiness and turned every ``""`` into a whitespace text node, which
    ``arxml_reader.py`` (``findtext(...) or ""``, no strip) read back as whitespace.
    """

    def test_an_empty_text_value_is_not_padded_with_whitespace(self):
        node = make_element("BASE-TYPE-ENCODING", "")
        assert serialize_fragment(node) == "<BASE-TYPE-ENCODING></BASE-TYPE-ENCODING>"

    def test_an_empty_text_value_round_trips_as_the_empty_string(self):
        node = make_element("BASE-TYPE-ENCODING", "")
        assert etree.fromstring(serialize_fragment(node).encode("utf-8")).text in (None, "")

    def test_an_empty_short_name_stays_resolvable(self):
        # A whitespace SHORT-NAME breaks find_element() and every path
        # reference to the element, so this is not only a cosmetic difference.
        document = ArxmlDocument()
        document.data_types.create_element("SW-BASE-TYPE", "")
        assert short_name_of(next(iter(document.data_types.elements))) == ""
        reparsed = etree.fromstring(document.serialize().encode("utf-8"))
        assert find_element(reparsed, "/DataTypes") is not None

    def test_a_textless_wrapper_still_gets_the_two_line_form(self):
        assert serialize_fragment(make_element("PORTS")) == "<PORTS>\n</PORTS>"


class TestArxmlDocument:
    """The container: three packages, in the load-bearing order, with lazy ELEMENTS."""

    def test_defaults_to_4_2_2(self):
        assert ArxmlDocument().version is ArxmlVersion.V4_2_2

    def test_constructs_at_a_given_version(self):
        assert ArxmlDocument(ArxmlVersion.V4_3_0).version is ArxmlVersion.V4_3_0

    def test_the_three_packages_are_created_in_order(self):
        # DataTypes -> PortInterfaces -> ComponentTypes is load-bearing: references
        # point backwards, so a package must exist before it is referenced.
        text = ArxmlDocument().serialize()
        positions = [text.index(f"<SHORT-NAME>{name}</SHORT-NAME>") for name in ArxmlDocument.PACKAGE_NAMES]
        assert positions == sorted(positions)
        assert list(ArxmlDocument.PACKAGE_NAMES) == ["DataTypes", "PortInterfaces", "ComponentTypes"]

    def test_named_package_accessors_return_the_same_objects_as_lookup(self):
        document = ArxmlDocument()
        assert document.data_types is document.package("DataTypes")
        assert document.port_interfaces is document.package("PortInterfaces")
        assert document.component_types is document.package("ComponentTypes")

    def test_unknown_package_name_raises(self):
        with pytest.raises(ValueError, match="Calibration"):
            ArxmlDocument().package("Calibration")

    def test_elements_wrapper_is_created_lazily(self):
        document = ArxmlDocument()
        assert document.data_types.has_elements is False
        assert "<ELEMENTS>" not in document.serialize()

        document.data_types.create_element("SW-BASE-TYPE", "float32")
        assert document.data_types.has_elements is True
        assert document.serialize().count("<ELEMENTS>") == 1

    def test_create_element_returns_a_named_element_inside_elements(self):
        document = ArxmlDocument()
        node = document.data_types.create_element("SW-BASE-TYPE", "float32")
        sub_element(node, "CATEGORY", "FIXED_LENGTH")
        text = document.serialize()
        assert "      <ELEMENTS>\n        <SW-BASE-TYPE>\n          <SHORT-NAME>float32</SHORT-NAME>\n" in text

    def test_add_element_appends_a_detached_element(self):
        document = ArxmlDocument()
        node = make_element("SW-BASE-TYPE")
        set_short_name(node, "float32")
        document.data_types.add_element(node)
        assert "<SHORT-NAME>float32</SHORT-NAME>" in document.serialize()

    def test_a_repeated_short_name_in_one_package_raises(self):
        """A ``SHORT-NAME`` is unique among a package's siblings.

        Otherwise every ``*-REF`` naming it is ambiguous; this is the backstop behind ``arxml_generation``'s
        guards for an emitter added later.
        """
        document = ArxmlDocument()
        document.component_types.create_element("APPLICATION-SW-COMPONENT-TYPE", "Foo")
        with pytest.raises(ValueError, match="Duplicate SHORT-NAME 'Foo'"):
            document.component_types.create_element("SWC-IMPLEMENTATION", "Foo")

    def test_add_element_claims_the_name_too(self):
        """Both entry points share one claim, so neither is a way around it."""
        document = ArxmlDocument()
        document.data_types.create_element("SW-BASE-TYPE", "float32")
        detached = make_element("IMPLEMENTATION-DATA-TYPE")
        set_short_name(detached, "float32")
        with pytest.raises(ValueError, match="Duplicate SHORT-NAME"):
            document.data_types.add_element(detached)

    def test_the_same_name_in_two_packages_is_fine(self):
        """The rule is per package: siblings, not the whole document."""
        document = ArxmlDocument()
        document.data_types.create_element("SW-BASE-TYPE", "Speed")
        document.port_interfaces.create_element("SENDER-RECEIVER-INTERFACE", "Speed")
        assert document.find("/DataTypes/Speed") is not None
        assert document.find("/PortInterfaces/Speed") is not None

    def test_an_unnamed_element_is_appended_without_a_claim(self):
        """The invariant is about named elements; an unnamed one carries no identity."""
        document = ArxmlDocument()
        document.data_types.add_element(make_element("SW-BASE-TYPE"))
        document.data_types.add_element(make_element("SW-BASE-TYPE"))
        assert len(document.data_types.elements) == 2

    def test_elements_keep_insertion_order(self):
        document = ArxmlDocument()
        for name in ("float32", "uint8", "Speed_T"):
            document.data_types.create_element("SW-BASE-TYPE", name)
        text = document.serialize()
        assert [line.strip() for line in text.splitlines() if "SHORT-NAME" in line][:4] == [
            "<SHORT-NAME>DataTypes</SHORT-NAME>",
            "<SHORT-NAME>float32</SHORT-NAME>",
            "<SHORT-NAME>uint8</SHORT-NAME>",
            "<SHORT-NAME>Speed_T</SHORT-NAME>",
        ]

    def test_save_writes_the_serialized_bytes_and_creates_parent_directories(self, tmp_path):
        document = ArxmlDocument()
        target = tmp_path / "dist" / "nested" / "model.arxml"
        returned = document.save(target)
        assert returned == str(target)
        # Bytes, not text: an accidental newline translation would break byte
        # equality with the goldens on Windows and nowhere else.
        assert target.read_bytes() == document.serialize().encode("utf-8")

    def test_a_failing_serialization_leaves_the_previous_file_intact(self, tmp_path, monkeypatch):
        """A failed generation leaves no partial file.

        ``open(..., "w")`` truncates first, so serializing inside the ``with`` block would leave a zero-byte
        ``model.arxml`` on an error.
        """
        document = ArxmlDocument()
        target = tmp_path / "dist" / "model.arxml"
        target.parent.mkdir(parents=True)
        target.write_text("previous run", encoding="utf-8")

        def _boom() -> str:
            raise RuntimeError("serialization failed")

        monkeypatch.setattr(document, "serialize", _boom)
        with pytest.raises(RuntimeError, match="serialization failed"):
            document.save(target)

        assert target.read_text(encoding="utf-8") == "previous run"


class TestEmptySkeleton:
    """The three-package skeleton, byte-compared against the empty-model golden."""

    def test_matches_the_empty_golden_exactly(self):
        assert ArxmlDocument().serialize() == read_golden("empty-4.2.2")

    def test_the_4_3_0_skeleton_differs_only_in_the_schema_filename(self):
        at_4_2_2 = ArxmlDocument(ArxmlVersion.V4_2_2).serialize()
        at_4_3_0 = ArxmlDocument(ArxmlVersion.V4_3_0).serialize()
        assert at_4_3_0 == at_4_2_2.replace("AUTOSAR_4-2-2.xsd", "AUTOSAR_4-3-0.xsd")
        assert at_4_3_0 != at_4_2_2


class TestPrologAndRootAttributes:
    """The first two lines are byte-pinned by all three goldens."""

    def test_declaration_uses_double_quotes_and_lowercase_utf_8(self):
        assert XML_DECLARATION == '<?xml version="1.0" encoding="utf-8"?>'

    @pytest.mark.parametrize("golden", ["basic-4.2.2", "composite-4.2.2", "empty-4.2.2"])
    def test_first_two_lines_match_every_golden(self, golden):
        produced = ArxmlDocument().serialize().splitlines()
        assert produced[:2] == read_golden(golden).splitlines()[:2]

    def test_root_attribute_order_is_xmlns_then_xsi_then_schema_location(self):
        root_line = ArxmlDocument().serialize().splitlines()[1]
        assert root_line.index("xmlns=") < root_line.index("xmlns:xsi=") < root_line.index("xsi:schemaLocation=")

    def test_output_ends_with_exactly_one_trailing_newline(self):
        text = ArxmlDocument().serialize()
        assert text.endswith("</AUTOSAR>\n")


class TestDeterminism:
    """Two generations of the same model must be byte-identical."""

    def test_serializing_the_same_document_twice_is_byte_identical(self):
        document = ArxmlDocument()
        _swc_with_empty_ports(document)
        assert document.serialize() == document.serialize()

    def test_serialization_does_not_mutate_the_document(self):
        # The empty-element expansion runs on a copy; if it mutated the live tree,
        # a second serialization (or a later emitter) would see altered text nodes.
        document = ArxmlDocument()
        _swc_with_empty_ports(document)
        before = len(list(document.root.iter()))
        document.serialize()
        assert len(list(document.root.iter())) == before

    def test_two_identically_built_documents_are_byte_identical(self):
        def build() -> str:
            document = ArxmlDocument()
            node = document.data_types.create_element("IMPLEMENTATION-DATA-TYPE", "float32_impl")
            sub_element(node, "CATEGORY", "VALUE")
            reference_element(node, "BASE-TYPE-REF", "/DataTypes/float32", dest="SW-BASE-TYPE")
            return document.serialize()

        assert build() == build()


class TestFragmentSerialization:
    """The emitters' comparison mechanism: a sub-document against a named golden slice."""

    def test_a_hand_built_fragment_matches_a_golden_slice(self):
        node = make_element("SW-BASE-TYPE")
        set_short_name(node, "float32_base")
        sub_element(node, "CATEGORY", "FIXED_LENGTH")
        sub_element(node, "BASE-TYPE-SIZE", 32)
        sub_element(node, "BASE-TYPE-ENCODING", "IEEE754")
        sub_element(node, "NATIVE-DECLARATION", "float32")
        assert serialize_fragment(node) == golden_slice(
            read_golden("composite-4.2.2"), "float32_base", tag="SW-BASE-TYPE"
        )

    def test_a_fragment_with_a_reference_matches_a_golden_slice(self):
        node = make_element("IMPLEMENTATION-DATA-TYPE")
        set_short_name(node, "float32")
        sub_element(node, "CATEGORY", "VALUE")
        props = sub_element(node, "SW-DATA-DEF-PROPS")
        variants = sub_element(props, "SW-DATA-DEF-PROPS-VARIANTS")
        conditional = sub_element(variants, "SW-DATA-DEF-PROPS-CONDITIONAL")
        reference_element(conditional, "BASE-TYPE-REF", "/DataTypes/Base/float32_base", dest="SW-BASE-TYPE")
        sub_element(conditional, "SW-CALIBRATION-ACCESS", "NOT-ACCESSIBLE")
        assert serialize_fragment(node) == golden_slice(
            read_golden("composite-4.2.2"), "float32", tag="IMPLEMENTATION-DATA-TYPE"
        )

    def test_a_fragment_declares_no_namespace_of_its_own(self):
        # A slice of a golden carries no xmlns (the root declares it once), so a
        # fragment that re-declared it could never compare equal.
        assert "xmlns" not in serialize_fragment(make_element("PORTS"))

    def test_a_fragment_taken_from_a_live_document_serializes_the_same(self):
        document = ArxmlDocument()
        node = document.data_types.create_element("SW-BASE-TYPE", "float32")
        sub_element(node, "CATEGORY", "FIXED_LENGTH")
        assert serialize_fragment(node) == (
            "<SW-BASE-TYPE>\n"
            f"{INDENT}<SHORT-NAME>float32</SHORT-NAME>\n"
            f"{INDENT}<CATEGORY>FIXED_LENGTH</CATEGORY>\n"
            "</SW-BASE-TYPE>"
        )

    def test_serialize_document_and_the_document_method_agree(self):
        document = ArxmlDocument()
        assert serialize_document(document.root) == document.serialize()


class TestPathResolution:
    """``find_element()``/``ArxmlDocument.find()`` path resolution.

    It runs over an in-memory document and a parsed generated file alike.
    """

    def _golden_root(self, name: str = "composite-4.2.2") -> etree._Element:
        return etree.fromstring(read_golden(name).encode("utf-8"))

    def test_resolves_a_package(self):
        root = self._golden_root()
        assert short_name_of(find_element(root, "/DataTypes")) == "DataTypes"

    def test_resolves_a_package_level_element(self):
        root = self._golden_root()
        node = find_element(root, "/DataTypes/Base/float32_base")
        assert etree.QName(node).localname == "SW-BASE-TYPE"

    def test_resolves_a_declared_value_type(self):
        """The golden's ``IMPLEMENTATION-DATA-TYPE`` ``float32``: a package-level element of that tag."""
        root = self._golden_root()
        node = find_element(root, "/DataTypes/Implementation/float32")
        assert etree.QName(node).localname == "IMPLEMENTATION-DATA-TYPE"
        assert find_element(root, "/DataTypes/Implementation/float32_impl") is None

    def test_resolves_a_nested_element_through_its_wrappers(self):
        # PORTS is a wrapper, so it is not a path segment -- the same shape the
        # document's own PORT-REF paths use.
        root = self._golden_root()
        node = find_element(root, "/ComponentTypes/SensorSwc/pSpeed")
        assert etree.QName(node).localname == "P-PORT-PROTOTYPE"

    def test_resolves_a_runnable_through_its_internal_behavior(self):
        root = self._golden_root()
        node = find_element(root, "/ComponentTypes/SensorSwc/SensorSwc_InternalBehavior/SensorMain")
        assert etree.QName(node).localname == "RUNNABLE-ENTITY"

    def test_an_absent_element_resolves_to_none(self):
        assert find_element(self._golden_root(), "/DataTypes/NoSuchType") is None

    def test_an_absent_intermediate_segment_resolves_to_none(self):
        assert find_element(self._golden_root(), "/NoSuchPackage/float32") is None

    def test_a_named_element_is_not_searched_through(self):
        """Resolution is a path walk, not a name hunt: a runnable named directly under the component is not found."""
        root = self._golden_root()
        assert find_element(root, "/ComponentTypes/SensorSwc/SensorMain") is None

    def test_a_relative_path_raises(self):
        with pytest.raises(ValueError, match="absolute"):
            find_element(self._golden_root(), "DataTypes/float32")

    def test_an_empty_path_raises(self):
        with pytest.raises(ValueError, match="at least one element"):
            find_element(self._golden_root(), "/")

    def test_the_document_method_resolves_its_own_packages(self):
        document = ArxmlDocument()
        assert document.find("/DataTypes") is document.data_types.node
        assert document.find("/PortInterfaces") is document.port_interfaces.node
        assert document.find("/ComponentTypes") is document.component_types.node

    def test_the_document_method_resolves_an_element_it_was_given(self):
        document = ArxmlDocument()
        node = document.data_types.create_element("SW-BASE-TYPE", "float32")
        assert document.find("/DataTypes/float32") is node

    def test_the_document_method_returns_none_for_an_unknown_element(self):
        assert ArxmlDocument().find("/DataTypes/float32") is None

    def test_short_name_of_distinguishes_a_wrapper_from_a_named_element(self):
        document = ArxmlDocument()
        assert short_name_of(document.data_types.node) == "DataTypes"
        assert short_name_of(sub_element(document.data_types.node, "ELEMENTS")) is None


class TestMovePreviousArxml:
    """The one move ``ArxmlDocument.save`` and ``swcraft clean-outputs`` share."""

    def test_an_absent_file_moves_nothing(self, tmp_path: Path) -> None:
        assert move_previous_arxml(tmp_path / "model.arxml") is None
        assert list(tmp_path.iterdir()) == []

    def test_a_file_moves_to_its_old_name_replacing_an_earlier_one(self, tmp_path: Path) -> None:
        (tmp_path / "model.arxml").write_bytes(b"new")
        (tmp_path / "model_old.arxml").write_bytes(b"older")

        assert move_previous_arxml(tmp_path / "model.arxml") == tmp_path / "model_old.arxml"
        assert sorted(path.name for path in tmp_path.iterdir()) == ["model_old.arxml"]
        assert (tmp_path / "model_old.arxml").read_bytes() == b"new"

    def test_a_directory_of_the_arxml_name_is_no_file_and_moves_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "model.arxml").mkdir()

        assert move_previous_arxml(tmp_path / "model.arxml") is None
        assert (tmp_path / "model.arxml").is_dir()

    def test_a_refused_move_raises_and_keeps_the_file_under_its_name(self, tmp_path: Path) -> None:
        (tmp_path / "model.arxml").write_bytes(b"new")
        (tmp_path / "model_old.arxml").mkdir()

        with pytest.raises(OSError):
            move_previous_arxml(tmp_path / "model.arxml")
        assert (tmp_path / "model.arxml").read_bytes() == b"new"
