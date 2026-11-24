"""A bound is held exactly: no 64-bit limit is rounded through a float, in either direction.

A ``LOWER-LIMIT`` / ``UPPER-LIMIT`` is a ``Limit`` and a ``COMPU-NUMERATOR`` ``V`` a
``Numerical``: strings with a pattern, not doubles. Pinned here:

* **the model**: ``value_min`` / ``value_max``, ``range`` and a ``conversion``'s
  coefficients are ``int | float`` with no coercion;
* **the writer**: an ``int`` is written as its own digits, at both versions;
* **the reader**: an integer spelling reads as the ``int`` wherever the float's written
  spelling is another number (``-2**63`` prints ``-9223372036854776000``), the float
  otherwise, and nothing (reported) for a float spelling no float holds exactly;
* **the comparisons**: the coherence rule, the init range, the merge row of
  ``import model`` and ``import component``'s reuse compare the exact values.

The three reproduced cases are a 64-bit type's own limits, each through YAML ->
generate and through import -> generate.

What it does **not** reach: a time value (``TimeValue`` is an ``xsd:double``) and a YAML
float literal the loader has already rounded. An init value has the same rule in
``test_exact_init.py``; the two canonical equality keys are exact in
``test_editing_address.py``, ``test_query.py`` and ``test_editing_engine.py``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from behaviour_losses_support import snippet
from conftest import model_element_path
from lxml import etree
from project_support import files_of, init_project
from pydantic import ValidationError
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.merge import CONFLICTING, MERGED, linear_scale_limits
from swcraft_tools.arxml_import.merge_verdict import _limit_signature, decide_merges
from swcraft_tools.arxml_reader import CompuMethodInfo, CompuRationalScaleInfo, parse_exact_number, read_arxml
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.component_exchange import (
    ComponentImportConflictError,
    LossyComponentImportError,
    export_component,
    import_component,
)
from swcraft_tools.editing.registry import REGISTRY
from swcraft_tools.schema import Conversion, ValueDataType, ValueRange
from swcraft_tools.validator import check_init_conformance
from swcraft_tools.writer.arxml_generation import generate_arxml
from swcraft_tools.writer.mutations import create_data_type

NS = f"{{{NAMESPACE}}}"
TESTS = Path(__file__).resolve().parent
EXCHANGE_MODEL_ROOT = TESTS / "fixtures" / "component_exchange" / "model"
VERSIONS = ("4.2.2", "4.3.0")

UINT64_MAX = 18446744073709551615
INT64_MAX = 9223372036854775807
INT64_MIN = -9223372036854775808

#: The three reproduced cases: which limit, and the 64-bit value no float is written back as.
SIXTY_FOUR_BIT = [
    pytest.param("UPPER-LIMIT", UINT64_MAX, id="uint64-max"),
    pytest.param("UPPER-LIMIT", INT64_MAX, id="int64-max"),
    pytest.param("LOWER-LIMIT", INT64_MIN, id="int64-min"),
]


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _exchange_model(root: Path, **types: dict[str, Any]) -> Path:
    """The ``component_exchange`` tree, each named data type given its fields."""
    shutil.copytree(EXCHANGE_MODEL_ROOT, root)
    for name, fields in types.items():
        (path,) = [
            path
            for path in (
                root / "data_types" / "implementation" / f"{name}.yaml",
                root / "data_types" / "application" / f"{name}.yaml",
            )
            if path.exists()
        ]
        _dump(path, {**_load(path), **fields})
    return root


def _bounds(side: str, value: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """*value* on *side*, as a value type's field and as an application type's ``range``."""
    if side == "LOWER-LIMIT":
        return {"value_min": value}, {"range": {"min": value}}
    return {"value_max": value}, {"range": {"max": value}}


def _limit_texts(arxml: Path, constraint: str) -> dict[str, str]:
    """``{constraint}``'s limits, by tag, as written."""
    root = etree.parse(str(arxml)).getroot()
    (node,) = [n for n in root.iter(f"{NS}DATA-CONSTR") if n.findtext(f"{NS}SHORT-NAME") == constraint]
    (group,) = list(node.find(f"{NS}DATA-CONSTR-RULES/{NS}DATA-CONSTR-RULE"))
    return {etree.QName(limit).localname: limit.text for limit in group}


def _source(tmp_path: Path, version: str, **texts: str) -> Path:
    """SWCraft's own export, ``Speed_T`` and ``Velocity_T`` bounded on both sides, then each limit's text replaced.

    *texts* maps ``internal_lower`` / ``internal_upper`` / ``phys_lower`` / ``phys_upper`` to the text a
    foreign exporter wrote there.
    """
    root = tmp_path / "source"
    model_root = _exchange_model(
        root / "model", Speed_T={"value_min": 1.0, "value_max": 2.0}, Velocity_T={"range": {"min": 1.0, "max": 2.0}}
    )
    out = root / "model.arxml"
    generate_arxml(str(model_root), str(out), version=version)
    tree = etree.parse(str(out))
    for key, text in texts.items():
        group, side = key.split("_")
        constraint = "Speed_T_Range" if group == "internal" else "Velocity_T_Range"
        (node,) = [n for n in tree.getroot().iter(f"{NS}DATA-CONSTR") if n.findtext(f"{NS}SHORT-NAME") == constraint]
        (limit,) = list(node.iter(f"{NS}{side.upper()}-LIMIT"))
        limit.text = text
    out.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return out


def _import(source: Path, tmp_path: Path) -> tuple[Path, dict[str, dict[str, Any]], list[Any]]:
    """``import model`` into a fresh project: its model root, the written data types and the report's entries."""
    project = init_project(tmp_path / "project")
    report = run_import(str(source), str(project / "swcraft.yaml"))
    model_root = project / "model"
    types = {
        path.stem: _load(path)
        for directory in (model_root / "data_types" / "implementation", model_root / "data_types" / "application")
        for path in directory.glob("*.yaml")
    }
    return model_root, types, list(report.skip_entries)


def _f17(entries: list[Any]) -> list[Any]:
    return [
        e
        for e in entries
        if e.path == "/DataTypes/Implementation/Speed_T/SW-DATA-DEF-PROPS" and e.tag == "DATA-CONSTR-REF"
    ]


# The reader's one judgement


class TestTheReaderAnswersTheValueThatHoldsTheText:
    @pytest.mark.parametrize(
        ("text", "value", "kind"),
        [
            ("18446744073709551615", UINT64_MAX, int),
            ("9223372036854775807", INT64_MAX, int),
            # A float IS this power of two, and the writer prints it -9223372036854776000.
            ("-9223372036854775808", INT64_MIN, int),
            ("0xFFFFFFFFFFFFFFFF", UINT64_MAX, int),
            ("9007199254740993", 9007199254740993, int),
            ("300", 300.0, float),
            ("0x10", 16.0, float),
            ("-40", -40.0, float),
            ("9007199254740992", 9007199254740992.0, float),
            ("100000000000000000000", 1e20, float),
            ("300.5", 300.5, float),
            ("0.1", 0.1, float),
            ("1E3", 1000.0, float),
        ],
    )
    def test_an_exact_value(self, text: str, value: int | float, kind: type) -> None:
        read, inexact = parse_exact_number(text, grammar="formula")
        assert (read, type(read), inexact) == (value, kind, False)

    @pytest.mark.parametrize("text", ["16777217.000000001", "0.10000000000000001", "18446744073709551615.0"])
    def test_a_float_spelling_no_float_holds_is_no_value_and_says_so(self, text: str) -> None:
        assert parse_exact_number(text, grammar="formula") == (None, True)

    @pytest.mark.parametrize("text", [None, "", "  ", "NaN", "INF", "three hundred"])
    def test_what_is_no_finite_number_is_no_value(self, text: str | None) -> None:
        assert parse_exact_number(text, grammar="formula") == (None, False)


# The model


class TestTheModelHoldsAnIntegerAsTheInteger:
    def test_a_value_types_bounds_are_not_coerced(self) -> None:
        held = ValueDataType(name="T", kind="value_type", base="b", value_min=INT64_MIN, value_max=UINT64_MAX)
        assert (held.value_min, type(held.value_min)) == (INT64_MIN, int)
        assert (held.value_max, type(held.value_max)) == (UINT64_MAX, int)

    def test_a_float_stays_a_float(self) -> None:
        held = ValueDataType(name="T", kind="value_type", base="b", value_min=0.0, value_max=300.5)
        assert (type(held.value_min), type(held.value_max)) == (float, float)
        assert type(ValueRange(min=0.0).min) is float

    def test_a_range_and_a_conversion_are_not_coerced(self) -> None:
        held = ValueRange(min=INT64_MIN, max=UINT64_MAX)
        assert (held.min, type(held.min), held.max, type(held.max)) == (INT64_MIN, int, UINT64_MAX, int)
        conversion = Conversion(factor=9007199254740993, offset=INT64_MIN)
        assert (conversion.factor, type(conversion.factor)) == (9007199254740993, int)
        assert (conversion.offset, type(conversion.offset)) == (INT64_MIN, int)
        assert (Conversion().factor, Conversion().offset) == (1.0, 0.0)

    def test_the_coherence_rule_compares_exactly(self) -> None:
        """Guards a defect: rounded through a float both are 2**64, and the pair was accepted."""
        with pytest.raises(ValidationError, match="value_min must be <= value_max"):
            ValueDataType(name="T", kind="value_type", base="b", value_min=UINT64_MAX + 1, value_max=UINT64_MAX)
        with pytest.raises(ValidationError, match="range 'min' must be <= 'max'"):
            ValueRange(min=UINT64_MAX + 1, max=UINT64_MAX)

    @pytest.mark.parametrize(
        ("kind", "field"),
        [("value_type", "value_min"), ("value_type", "value_max"), ("range", "min"), ("range", "max")]
        + [("conversion", "factor"), ("conversion", "offset")],
    )
    def test_the_published_type_is_still_number(self, kind: str, field: str) -> None:
        """A JSON number is what an integer of any width is: discover and the editor schema do not move."""
        assert REGISTRY.element_kind(kind).get_field(field).type == "number"

    def test_the_write_path_keeps_the_digits(self, tmp_path: Path) -> None:
        model_root = _exchange_model(tmp_path / "model")
        create_data_type(str(model_root), "Wide", "value_type", base="uint16_base", value_max=UINT64_MAX)
        create_data_type(str(model_root), "Phys", "application_type", range={"min": INT64_MIN})

        assert "value_max: 18446744073709551615\n" in _text(model_element_path(model_root, "Wide", kind="value_type"))
        assert "min: -9223372036854775808\n" in _text(model_element_path(model_root, "Phys", kind="application_type"))


# The three cases, both versions, both paths


class TestASixtyFourBitLimitIsWrittenDigitForDigit:
    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize(("side", "value"), SIXTY_FOUR_BIT)
    def test_from_hand_authored_yaml(self, tmp_path: Path, version: str, side: str, value: int) -> None:
        internal, physical = _bounds(side, value)
        model_root = _exchange_model(tmp_path / "model", Speed_T=internal, Velocity_T=physical)
        out = tmp_path / "out.arxml"

        generate_arxml(str(model_root), str(out), version=version)

        assert _limit_texts(out, "Speed_T_Range") == {side: str(value)}
        assert _limit_texts(out, "Velocity_T_Range") == {side: str(value)}

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize(("side", "value"), SIXTY_FOUR_BIT)
    def test_from_an_import(self, tmp_path: Path, version: str, side: str, value: int) -> None:
        key = "lower" if side == "LOWER-LIMIT" else "upper"
        source = _source(tmp_path, version, **{f"internal_{key}": str(value), f"phys_{key}": str(value)})
        expected = _limit_texts(source, "Speed_T_Range")

        model_root, types, entries = _import(source, tmp_path)

        field = "value_min" if side == "LOWER-LIMIT" else "value_max"
        assert (types["Speed_T"][field], type(types["Speed_T"][field])) == (value, int)
        bound = types["Velocity_T"]["range"]["min" if side == "LOWER-LIMIT" else "max"]
        assert (bound, type(bound)) == (value, int)
        assert _f17(entries) == []
        assert not [entry for entry in entries if "holds exactly" in entry.reason]

        out = tmp_path / "out.arxml"
        generate_arxml(str(model_root), str(out), version=version)
        assert _limit_texts(out, "Speed_T_Range") == expected
        assert _limit_texts(out, "Velocity_T_Range") == _limit_texts(source, "Velocity_T_Range")

    def test_a_hexadecimal_limit_is_carried_by_its_value(self, tmp_path: Path) -> None:
        source = _source(tmp_path, "4.2.2", internal_upper="0xFFFFFFFFFFFFFFFF", phys_upper="0xFFFFFFFFFFFFFFFF")

        _model_root, types, entries = _import(source, tmp_path)

        assert types["Speed_T"]["value_max"] == UINT64_MAX
        assert types["Velocity_T"]["range"]["max"] == UINT64_MAX
        assert _f17(entries) == []

    def test_an_integer_a_float_holds_reads_back_as_the_float_it_was_written_from(self, tmp_path: Path) -> None:
        """The writer prints ``2.0`` as ``2``, so a float-held integer reads back as the float it was written from."""
        _model_root, types, _entries = _import(_source(tmp_path, "4.2.2"), tmp_path)

        assert (types["Speed_T"]["value_max"], type(types["Speed_T"]["value_max"])) == (2.0, float)
        assert type(types["Velocity_T"]["range"]["max"]) is float


class TestAFloatSpellingNoFloatHoldsIsReportedAndNotCarried:
    """``16777217.000000001`` reads as the float ``16777217.0``, which the writer would state as ``16777217``."""

    def test_a_value_types_bound_is_one_f17_line(self, tmp_path: Path) -> None:
        source = _source(tmp_path, "4.2.2", internal_lower="16777217.000000001", internal_upper="16777218")

        _model_root, types, entries = _import(source, tmp_path)

        (line,) = _f17(entries)
        assert "a LOWER-LIMIT no model value holds exactly" in line.reason
        assert ("value_min" in types["Speed_T"], types["Speed_T"]["value_max"]) == (False, 16777218.0)

    def test_an_application_types_bound_is_named_and_the_other_survives(self, tmp_path: Path) -> None:
        source = _source(tmp_path, "4.2.2", phys_lower="16777217.000000001", phys_upper="16777218")

        project = init_project(tmp_path / "project")
        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert _load(model_element_path(project / "model", "Velocity_T", kind="application_type"))["range"] == {
            "max": 16777218.0
        }
        assert [line for line in report.unread_physics_lines if "holds exactly" in line] == [
            "/DataTypes/Application/Velocity_T: DATA-CONSTR "
            "/DataTypes/Application/Velocity_T_Range declares a PHYS-CONSTRS LOWER-LIMIT "
            "no model value holds exactly; that bound is not imported"
        ]

    def test_a_conversion_coefficient_drops_the_conversion_and_says_so(self, tmp_path: Path) -> None:
        source = _source(tmp_path, "4.2.2")
        tree = etree.parse(str(source))
        (numerator,) = list(tree.getroot().iter(f"{NS}COMPU-NUMERATOR"))
        list(numerator)[1].text = "0.10000000000000001"
        source.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))

        project = init_project(tmp_path / "project")
        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert "conversion" not in _load(model_element_path(project / "model", "Velocity_T", kind="application_type"))
        (line,) = [line for line in report.unread_physics_lines if "holds exactly" in line]
        assert "has a COMPU-NUMERATOR coefficient no model value holds exactly" in line
        assert "imported without a conversion and nothing is rounded" in line


# The comparisons


class TestTwoDeclarationsAreComparedByTheirExactLimits:
    @staticmethod
    def _twin(source: Path, out: Path, upper: str, interval: str | None = "CLOSED") -> Path:
        """*source* with a second ``Speed_T`` in a package of its own, its upper limit *upper*."""
        tree = etree.parse(str(source))
        root = tree.getroot()

        def named(tag: str, name: str) -> etree._Element:
            (node,) = [n for n in root.iter(f"{NS}{tag}") if n.findtext(f"{NS}SHORT-NAME") == name]
            return node

        speed = etree.fromstring(etree.tostring(named("IMPLEMENTATION-DATA-TYPE", "Speed_T")))
        constraint = etree.fromstring(etree.tostring(named("DATA-CONSTR", "Speed_T_Range")))
        next(speed.iter(f"{NS}DATA-CONSTR-REF")).text = "/Other/Speed_T_Range"
        limit = next(constraint.iter(f"{NS}UPPER-LIMIT"))
        limit.text = upper
        if interval is None:
            del limit.attrib["INTERVAL-TYPE"]
        else:
            limit.set("INTERVAL-TYPE", interval)
        package = snippet("<AR-PACKAGE><SHORT-NAME>Other</SHORT-NAME><ELEMENTS/></AR-PACKAGE>")[0]
        package.find(f"{NS}ELEMENTS").extend([speed, constraint])
        root.find(f"{NS}AR-PACKAGES").append(package)
        out.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
        return out

    @pytest.mark.parametrize(
        ("first", "second", "interval", "verdict"),
        [
            ("18446744073709551615", "18446744073709551614", "CLOSED", CONFLICTING),
            ("18446744073709551615", "0xFFFFFFFFFFFFFFFF", "CLOSED", MERGED),
            # Neither is carried, and they are still two declarations.
            ("16777217.000000001", "16777217.000000002", "CLOSED", CONFLICTING),
            ("16777217.000000001", "16777217", "CLOSED", CONFLICTING),
            # The model holds the integer spelling exactly and not the `.0` one, so
            # one is carried and the other not: two declarations.
            ("9007199254740993", "9007199254740993.0", "CLOSED", CONFLICTING),
            # A float holds 2**53 exactly, so both spellings are carried, as one value.
            ("9007199254740992", "9007199254740992.0", "CLOSED", MERGED),
            ("16", "16.0", None, MERGED),
            ("16", "16", "OPEN", CONFLICTING),
            ("16", "17", "INFINITE", CONFLICTING),
        ],
    )
    def test_the_merge_row(self, tmp_path: Path, first: str, second: str, interval: str | None, verdict: str) -> None:
        source = self._twin(_source(tmp_path, "4.2.2", internal_upper=first), tmp_path / "in.arxml", second, interval)

        (group,) = [g for g in decide_merges(read_arxml(str(source))).groups if g.key.name == "Speed_T"]

        assert group.verdict == verdict
        if verdict == CONFLICTING:
            assert group.difference == "DATA-CONSTR"

    def test_an_infinite_limit_is_the_absent_one(self, tmp_path: Path) -> None:
        """One declaration states no upper limit and the other an ``INFINITE`` one: neither bounds anything."""
        source = _source(tmp_path, "4.2.2")
        tree = etree.parse(str(source))
        (constraint,) = [
            n for n in tree.getroot().iter(f"{NS}DATA-CONSTR") if n.findtext(f"{NS}SHORT-NAME") == "Speed_T_Range"
        ]
        upper = next(constraint.iter(f"{NS}UPPER-LIMIT"))
        upper.getparent().remove(upper)
        source.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
        twin = tmp_path / "in.arxml"
        twin_tree = etree.parse(str(source))
        root = twin_tree.getroot()
        package = snippet(
            "<AR-PACKAGE><SHORT-NAME>Other</SHORT-NAME><ELEMENTS>"
            "<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>Speed_T</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
            "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
            '<BASE-TYPE-REF DEST="SW-BASE-TYPE">/DataTypes/Base/float32_base</BASE-TYPE-REF>'
            "<SW-CALIBRATION-ACCESS>NOT-ACCESSIBLE</SW-CALIBRATION-ACCESS>"
            '<DATA-CONSTR-REF DEST="DATA-CONSTR">/Other/Speed_T_Range</DATA-CONSTR-REF>'
            "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
            "</IMPLEMENTATION-DATA-TYPE>"
            "<DATA-CONSTR><SHORT-NAME>Speed_T_Range</SHORT-NAME><DATA-CONSTR-RULES><DATA-CONSTR-RULE>"
            '<INTERNAL-CONSTRS><LOWER-LIMIT INTERVAL-TYPE="CLOSED">1</LOWER-LIMIT>'
            '<UPPER-LIMIT INTERVAL-TYPE="INFINITE">17</UPPER-LIMIT></INTERNAL-CONSTRS>'
            "</DATA-CONSTR-RULE></DATA-CONSTR-RULES></DATA-CONSTR>"
            "</ELEMENTS></AR-PACKAGE>"
        )[0]
        root.find(f"{NS}AR-PACKAGES").append(package)
        twin.write_bytes(etree.tostring(twin_tree, xml_declaration=True, encoding="UTF-8"))

        groups = [g for g in decide_merges(read_arxml(str(twin))).groups if g.key.name == "Speed_T"]

        assert [(g.verdict, g.difference) for g in groups] == [(MERGED, None)]


class TestTwoPrivateLinearFormsAreComparedByTheirCarriedLimits:
    """The merge row signs a private ``LINEAR`` form's scale limits by what the model carries.

    A different limit or interval is a different type; a different spelling of the same
    carried limit (a radix, a fraction, an absent ``INTERVAL-TYPE`` for ``CLOSED``) is not.
    """

    _NAMES = ("Velocity_T", "Velocity_T_Compu", "Velocity_T_Unit", "Velocity_T_Range")

    @classmethod
    def _twin(cls, tmp_path: Path, tag: str, text: str, interval: str | None | bool) -> Path:
        model_root = _exchange_model(
            tmp_path / "source" / "model",
            Velocity_T={
                "conversion": {
                    "factor": 2,
                    "offset": 0,
                    "limits": {"lower": {"value": 0}, "upper": {"value": 100, "interval": "open"}},
                }
            },
        )
        source = tmp_path / "source" / "model.arxml"
        generate_arxml(str(model_root), str(source), version="4.2.2")
        tree = etree.parse(str(source))
        (home,) = [p for p in tree.getroot().iter(f"{NS}AR-PACKAGE") if p.findtext(f"{NS}SHORT-NAME") == "Application"]
        copies = [
            etree.fromstring(etree.tostring(node))
            for node in home.find(f"{NS}ELEMENTS")
            if node.findtext(f"{NS}SHORT-NAME") in cls._NAMES
        ]
        for node in (n for copy in copies for n in copy.iter() if str(n.tag).endswith("-REF")):
            node.text = node.text.replace("/DataTypes/Application/", "/DataTypes/Application/Other/")
        (compu,) = [c for c in copies if c.findtext(f"{NS}SHORT-NAME") == "Velocity_T_Compu"]
        limit = next(compu.iter(f"{NS}{tag}"))
        limit.text = text
        if interval is None:
            del limit.attrib["INTERVAL-TYPE"]
        elif interval is not True:
            limit.set("INTERVAL-TYPE", interval)
        package = snippet(
            "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Other</SHORT-NAME><ELEMENTS/></AR-PACKAGE></AR-PACKAGES>"
        )[0]
        package.find(f"{NS}AR-PACKAGE/{NS}ELEMENTS").extend(copies)
        assert home.find(f"{NS}AR-PACKAGES") is None
        home.append(package)
        out = tmp_path / "in.arxml"
        out.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
        return out

    @pytest.mark.parametrize(
        ("tag", "text", "interval", "verdict"),
        [
            pytest.param("UPPER-LIMIT", "100", True, MERGED, id="the-same"),
            pytest.param("UPPER-LIMIT", "101", True, CONFLICTING, id="another-value"),
            pytest.param("UPPER-LIMIT", "100", "CLOSED", CONFLICTING, id="another-interval"),
            pytest.param("LOWER-LIMIT", "-INF", "INFINITE", CONFLICTING, id="no-lower-bound"),
            pytest.param("UPPER-LIMIT", "0x64", True, MERGED, id="hexadecimal"),
            pytest.param("UPPER-LIMIT", "100.0", True, MERGED, id="a-fraction"),
            pytest.param("LOWER-LIMIT", "0", None, MERGED, id="absent-interval-is-closed"),
        ],
    )
    def test_the_merge_row(
        self, tmp_path: Path, tag: str, text: str, interval: str | None | bool, verdict: str
    ) -> None:
        source = self._twin(tmp_path, tag, text, interval)

        groups = [g for g in decide_merges(read_arxml(str(source))).groups if g.key.name == "Velocity_T"]

        assert [(g.verdict, g.difference) for g in groups] == [
            (verdict, "COMPU-METHOD" if verdict == CONFLICTING else None)
        ]


class TestTwoLinearElementsAreComparedByTheirExactLimits:
    """A ``LINEAR`` ``COMPU-METHOD`` element's row signs its scale limits as the private form's does.

    A different spelling of one limit is one element; a different value or interval is two.
    """

    @staticmethod
    def _method(package: str, upper: str) -> str:
        return (
            f"<AR-PACKAGE><SHORT-NAME>{package}</SHORT-NAME><ELEMENTS>"
            "<COMPU-METHOD><SHORT-NAME>ProbeScale</SHORT-NAME><CATEGORY>LINEAR</CATEGORY>"
            "<COMPU-INTERNAL-TO-PHYS><COMPU-SCALES><COMPU-SCALE>"
            f'<LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>{upper}'
            "<COMPU-RATIONAL-COEFFS><COMPU-NUMERATOR><V>0</V><V>2</V></COMPU-NUMERATOR>"
            "<COMPU-DENOMINATOR><V>1</V></COMPU-DENOMINATOR></COMPU-RATIONAL-COEFFS>"
            "</COMPU-SCALE></COMPU-SCALES></COMPU-INTERNAL-TO-PHYS></COMPU-METHOD>"
            "</ELEMENTS></AR-PACKAGE>"
        )

    @pytest.mark.parametrize(
        ("upper", "verdict"),
        [
            pytest.param('<UPPER-LIMIT INTERVAL-TYPE="CLOSED">100</UPPER-LIMIT>', MERGED, id="the-same"),
            pytest.param('<UPPER-LIMIT INTERVAL-TYPE="CLOSED">0x64</UPPER-LIMIT>', MERGED, id="hexadecimal"),
            pytest.param('<UPPER-LIMIT INTERVAL-TYPE="CLOSED">100.0</UPPER-LIMIT>', MERGED, id="a-fraction"),
            pytest.param("<UPPER-LIMIT>100</UPPER-LIMIT>", MERGED, id="absent-interval-is-closed"),
            pytest.param('<UPPER-LIMIT INTERVAL-TYPE="CLOSED">101</UPPER-LIMIT>', CONFLICTING, id="another-value"),
            pytest.param('<UPPER-LIMIT INTERVAL-TYPE="OPEN">100</UPPER-LIMIT>', CONFLICTING, id="another-interval"),
        ],
    )
    def test_the_merge_row(self, tmp_path: Path, upper: str, verdict: str) -> None:
        source = tmp_path / "in.arxml"
        source.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            f'<AUTOSAR xmlns="{NAMESPACE}"><AR-PACKAGES>'
            + self._method("A", '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">100</UPPER-LIMIT>')
            + self._method("B", upper)
            + "</AR-PACKAGES></AUTOSAR>\n",
            encoding="utf-8",
            newline="\n",
        )

        groups = [g for g in decide_merges(read_arxml(str(source))).groups if g.key.name == "ProbeScale"]

        assert [(g.verdict, g.difference) for g in groups] == [
            (verdict, "COMPU-RATIONAL-COEFFS" if verdict == CONFLICTING else None)
        ]


#: An infinite spelling on its own side of a limit pair, in every case and padding the reader takes.
OWN_SIDE_INFINITE = [
    pytest.param("lower", "-INF", id="lower-minus-inf"),
    pytest.param("lower", " -inf ", id="lower-padded-lower-case"),
    pytest.param("upper", "INF", id="upper-inf"),
    pytest.param("upper", "+INF", id="upper-plus-inf"),
    pytest.param("upper", "+Inf", id="upper-mixed-case"),
]
#: The same spellings on the side they bound nothing on: no value is above ``INF`` or below ``-INF``.
OTHER_SIDE_INFINITE = [
    pytest.param("lower", "INF", id="lower-inf"),
    pytest.param("lower", "+INF", id="lower-plus-inf"),
    pytest.param("upper", "-INF", id="upper-minus-inf"),
]


def _limit_tag(side: str) -> str:
    return f"{side.upper()}-LIMIT"


def _set_interval(source: Path, constraint: str, tag: str, interval: str | None) -> Path:
    """*source* with ``{constraint}``'s *tag* limit given *interval* (``None`` removes the attribute)."""
    tree = etree.parse(str(source))
    (node,) = [n for n in tree.getroot().iter(f"{NS}DATA-CONSTR") if n.findtext(f"{NS}SHORT-NAME") == constraint]
    (limit,) = list(node.iter(f"{NS}{tag}"))
    if interval is None:
        limit.attrib.pop("INTERVAL-TYPE", None)
    else:
        limit.set("INTERVAL-TYPE", interval)
    source.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return source


def _phys_limits(source: Path, lower: str | None, upper: str | None) -> Path:
    """*source* with ``Velocity_T_Range``'s two ``PHYS-CONSTRS`` limits spelled as given (``None`` removes one)."""
    tree = etree.parse(str(source))
    (node,) = [
        n for n in tree.getroot().iter(f"{NS}DATA-CONSTR") if n.findtext(f"{NS}SHORT-NAME") == "Velocity_T_Range"
    ]
    for tag, text in (("LOWER-LIMIT", lower), ("UPPER-LIMIT", upper)):
        (limit,) = list(node.iter(f"{NS}{tag}"))
        if text is None:
            limit.getparent().remove(limit)
        else:
            limit.text = text
    source.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return source


class TestAnInfiniteLimitBoundsNothingOnItsOwnSide:
    """``-INF`` below and ``INF`` / ``+INF`` above state what an absent limit states.

    The other side's spelling (a lower ``INF``, an upper ``-INF``) admits no value, and
    is not carried and named.
    """

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize(("side", "text"), OWN_SIDE_INFINITE)
    def test_a_value_types_own_side_is_no_bound_and_no_line(
        self, tmp_path: Path, version: str, side: str, text: str
    ) -> None:
        source = _source(tmp_path, version, **{f"internal_{side}": text})

        _model_root, types, entries = _import(source, tmp_path)

        gone, kept = ("value_min", "value_max") if side == "lower" else ("value_max", "value_min")
        assert gone not in types["Speed_T"]
        assert types["Speed_T"][kept] == (2.0 if side == "lower" else 1.0)
        assert _f17(entries) == []

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize("interval", ["OPEN", "INFINITE", None])
    def test_a_value_types_own_side_is_no_bound_under_every_interval_type(
        self, tmp_path: Path, version: str, interval: str | None
    ) -> None:
        """An open end at infinity excludes nothing a closed one admits."""
        source = _source(tmp_path, version, internal_lower="-INF")
        _set_interval(source, "Speed_T_Range", "LOWER-LIMIT", interval)

        _model_root, types, entries = _import(source, tmp_path)

        assert ("value_min" in types["Speed_T"], types["Speed_T"]["value_max"]) == (False, 2.0)
        assert _f17(entries) == []

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize("interval", ["CLOSED", "INFINITE"])
    @pytest.mark.parametrize(("side", "text"), OTHER_SIDE_INFINITE)
    def test_a_value_types_other_side_is_not_carried_and_is_named(
        self, tmp_path: Path, version: str, side: str, text: str, interval: str
    ) -> None:
        source = _source(tmp_path, version, **{f"internal_{side}": text})
        _set_interval(source, "Speed_T_Range", _limit_tag(side), interval)

        _model_root, types, entries = _import(source, tmp_path)

        (line,) = _f17(entries)
        assert f"a {_limit_tag(side)} spelling the other side's infinity" in line.reason
        assert line.effect == "abort"
        assert ("value_min" if side == "lower" else "value_max") not in types["Speed_T"]

    @pytest.mark.parametrize("version", VERSIONS)
    def test_a_value_type_bounded_by_its_own_infinities_alone_has_no_bound_and_no_line(
        self, tmp_path: Path, version: str
    ) -> None:
        source = _source(tmp_path, version, internal_lower="-INF", internal_upper="INF")

        _model_root, types, entries = _import(source, tmp_path)

        assert _f17(entries) == []
        assert [entry for entry in entries if "Speed_T_Range" in entry.path] == []
        assert ("value_min" in types["Speed_T"], "value_max" in types["Speed_T"]) == (False, False)

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize(("side", "text"), OWN_SIDE_INFINITE)
    def test_an_application_types_own_side_is_no_bound_and_no_line(
        self, tmp_path: Path, version: str, side: str, text: str
    ) -> None:
        source = _source(tmp_path, version, **{f"phys_{side}": text})

        project = init_project(tmp_path / "project")
        report = run_import(str(source), str(project / "swcraft.yaml"))

        kept = {"max": 2.0} if side == "lower" else {"min": 1.0}
        assert _load(model_element_path(project / "model", "Velocity_T", kind="application_type"))["range"] == kept
        assert [line for line in report.unread_physics_lines if "Velocity_T_Range" in line] == []

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize(
        ("lower", "upper"),
        [
            pytest.param("-INF", "INF", id="both-infinite"),
            pytest.param(" -inf ", "+Inf", id="both-infinite-padded"),
            pytest.param("-INF", None, id="lower-infinite-upper-absent"),
            pytest.param(None, "+INF", id="lower-absent-upper-infinite"),
        ],
    )
    def test_an_application_type_bounded_by_its_own_infinities_alone_has_a_range_with_no_limit_and_no_line(
        self, tmp_path: Path, version: str, lower: str | None, upper: str | None
    ) -> None:
        """Every declared limit is its own side's infinity: ``range: {}``, and no line.

        ``Velocity_T_Range`` is SWCraft's own private form, which reads back as the
        ``range`` it states, so the constraint and the reference are written back.
        """
        source = _phys_limits(_source(tmp_path, version), lower, upper)

        project = init_project(tmp_path / "project")
        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert _load(model_element_path(project / "model", "Velocity_T", kind="application_type"))["range"] == {}
        assert [line for line in report.unread_physics_lines if "Velocity_T_Range" in line] == []
        assert [entry for entry in report.skip_entries if "Velocity_T_Range" in entry.path] == []

    @pytest.mark.parametrize("version", VERSIONS)
    def test_an_application_types_own_constraint_declaring_no_limit_at_all_is_its_empty_range(
        self, tmp_path: Path, version: str
    ) -> None:
        """Neither limit declared: the writer's own ``range: {}`` form reads back as ``range: {}``.

        Guards a defect: the own form was read as no range, so ``range: {}`` did not survive a round trip.
        """
        source = _phys_limits(_source(tmp_path, version), None, None)

        project = init_project(tmp_path / "project")
        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert _load(model_element_path(project / "model", "Velocity_T", kind="application_type"))["range"] == {}
        assert [line for line in report.unread_physics_lines if "Velocity_T_Range" in line] == []

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize(
        ("lower", "upper", "tag"),
        [("-INF", "-INF", "UPPER-LIMIT"), ("INF", "INF", "LOWER-LIMIT")],
        ids=["upper-other-side", "lower-other-side"],
    )
    def test_an_own_side_infinity_beside_an_other_side_one_names_only_the_other(
        self, tmp_path: Path, version: str, lower: str, upper: str, tag: str
    ) -> None:
        source = _phys_limits(_source(tmp_path, version), lower, upper)

        project = init_project(tmp_path / "project")
        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert "range" not in _load(model_element_path(project / "model", "Velocity_T", kind="application_type"))
        assert [line for line in report.unread_physics_lines if "Velocity_T_Range" in line] == [
            "/DataTypes/Application/Velocity_T: DATA-CONSTR "
            "/DataTypes/Application/Velocity_T_Range declares a PHYS-CONSTRS "
            f"{tag} this release cannot read as a number; that bound is not imported"
        ]

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize(("side", "text"), OTHER_SIDE_INFINITE)
    def test_an_application_types_other_side_is_not_carried_and_is_named(
        self, tmp_path: Path, version: str, side: str, text: str
    ) -> None:
        source = _source(tmp_path, version, **{f"phys_{side}": text})

        project = init_project(tmp_path / "project")
        report = run_import(str(source), str(project / "swcraft.yaml"))

        kept = {"max": 2.0} if side == "lower" else {"min": 1.0}
        assert _load(model_element_path(project / "model", "Velocity_T", kind="application_type"))["range"] == kept
        assert [line for line in report.unread_physics_lines if "Velocity_T_Range" in line] == [
            "/DataTypes/Application/Velocity_T: DATA-CONSTR "
            "/DataTypes/Application/Velocity_T_Range declares a PHYS-CONSTRS "
            f"{_limit_tag(side)} this release cannot read as a number; that bound is not imported"
        ]

    @pytest.mark.parametrize(
        ("side", "text", "interval", "signature"),
        [
            ("LOWER-LIMIT", "-INF", None, None),
            ("LOWER-LIMIT", " -inf ", "CLOSED", None),
            ("LOWER-LIMIT", "-INF", "OPEN", None),
            ("UPPER-LIMIT", "INF", None, None),
            ("UPPER-LIMIT", "+INF", "CLOSED", None),
            ("UPPER-LIMIT", "7", "INFINITE", None),
            ("LOWER-LIMIT", "INF", None, (("text", "INF"), None)),
            ("LOWER-LIMIT", "+INF", "INFINITE", (("text", "+INF"), "INFINITE")),
            ("UPPER-LIMIT", "-INF", "CLOSED", (("text", "-INF"), "CLOSED")),
        ],
    )
    def test_the_merge_row_signs_only_the_own_side_as_the_absent_limit(
        self, side: str, text: str, interval: str | None, signature: Any
    ) -> None:
        assert _limit_signature(text, interval, side) == signature

    @pytest.mark.parametrize(
        ("side", "text", "interval", "absent"),
        [
            ("LOWER-LIMIT", "-INF", None, True),
            ("LOWER-LIMIT", "-inf", "OPEN", True),
            ("UPPER-LIMIT", "INF", None, True),
            ("UPPER-LIMIT", "+INF", "CLOSED", True),
            ("UPPER-LIMIT", "7", "INFINITE", True),
            ("LOWER-LIMIT", "INF", None, False),
            ("LOWER-LIMIT", "+INF", "INFINITE", False),
            ("UPPER-LIMIT", "-INF", None, False),
            ("UPPER-LIMIT", "-INF", "INFINITE", False),
        ],
    )
    def test_a_scale_limit_is_absent_only_on_its_own_side(
        self, side: str, text: str, interval: str | None, absent: bool
    ) -> None:
        """A ``LINEAR`` scale's own-side infinity is the absent bound with a descriptive line (family F23).

        The other side's infinity is not carried: a behavioural line, whatever its interval."""
        scale = CompuRationalScaleInfo(numerator=("0", "1"), denominator=("1",))
        prefix = "lower" if side == "LOWER-LIMIT" else "upper"
        setattr(scale, f"{prefix}_limit", text)
        setattr(scale, f"{prefix}_interval", interval)
        method = CompuMethodInfo(
            path="/P/M", name="M", category="LINEAR", scales=[], description="", rational_scales=[scale], scale_count=1
        )
        limits, lines = linear_scale_limits(method)
        assert limits is None
        assert [(line.tag, line.lost) for line in lines] == [(side, not absent)]

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize(("lower", "verdict"), [("-INF", MERGED), ("INF", CONFLICTING)], ids=["own", "other"])
    def test_two_declarations_one_unbounded_below_one_spelling_an_infinity(
        self, tmp_path: Path, version: str, lower: str, verdict: str
    ) -> None:
        """A declaration with no lower limit and a twin spelling one: the same range only on the limit's own side."""
        source = _source(tmp_path, version)
        tree = etree.parse(str(source))
        root = tree.getroot()

        def named(tag: str, name: str) -> etree._Element:
            (node,) = [n for n in root.iter(f"{NS}{tag}") if n.findtext(f"{NS}SHORT-NAME") == name]
            return node

        speed = etree.fromstring(etree.tostring(named("IMPLEMENTATION-DATA-TYPE", "Speed_T")))
        constraint = etree.fromstring(etree.tostring(named("DATA-CONSTR", "Speed_T_Range")))
        next(speed.iter(f"{NS}DATA-CONSTR-REF")).text = "/Other/Speed_T_Range"
        next(constraint.iter(f"{NS}LOWER-LIMIT")).text = lower
        first_lower = next(named("DATA-CONSTR", "Speed_T_Range").iter(f"{NS}LOWER-LIMIT"))
        first_lower.getparent().remove(first_lower)
        package = snippet("<AR-PACKAGE><SHORT-NAME>Other</SHORT-NAME><ELEMENTS/></AR-PACKAGE>")[0]
        package.find(f"{NS}ELEMENTS").extend([speed, constraint])
        root.find(f"{NS}AR-PACKAGES").append(package)
        twin = tmp_path / "in.arxml"
        twin.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))

        (group,) = [g for g in decide_merges(read_arxml(str(twin))).groups if g.key.name == "Speed_T"]

        assert group.verdict == verdict


class TestImportComponentComparesTheExactBounds:
    @staticmethod
    def _target(tmp_path: Path, **speed: Any) -> Path:
        project = init_project(tmp_path / "target")
        shutil.rmtree(project / "model")
        return _exchange_model(project / "model", Speed_T=speed)

    @staticmethod
    def _export(tmp_path: Path, **speed: Any) -> Path:
        model_root = _exchange_model(tmp_path / "source" / "model", Speed_T=speed)
        out = tmp_path / "source" / "SensorSwc.arxml"
        export_component(str(model_root), "SensorSwc", str(out))
        return out

    def test_equal_sixty_four_bit_bounds_are_reused(self, tmp_path: Path) -> None:
        model_root = self._target(tmp_path, value_min=INT64_MIN, value_max=UINT64_MAX)
        before = files_of(model_root)

        report = import_component(
            str(self._export(tmp_path, value_min=INT64_MIN, value_max=UINT64_MAX)), str(model_root)
        )

        assert "Speed_T" in report.reused_data_types
        assert files_of(model_root) == before

    def test_bounds_a_float_rounds_together_are_a_conflict(self, tmp_path: Path) -> None:
        model_root = self._target(tmp_path, value_max=UINT64_MAX)
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError) as caught:
            import_component(str(self._export(tmp_path, value_max=UINT64_MAX - 1)), str(model_root))

        assert "Speed_T" in str(caught.value)
        assert files_of(model_root) == before

    @staticmethod
    def _respelled_export(tmp_path: Path, version: str, tag: str, text: str) -> Path:
        """``SensorSwc``'s export, ``Speed_T`` bounded ``[0, 300]``, the *tag* limit then respelled *text*."""
        source_root = _exchange_model(tmp_path / "source" / "model", Speed_T={"value_min": 0.0, "value_max": 300.0})
        export = tmp_path / "source" / "SensorSwc.arxml"
        export_component(str(source_root), "SensorSwc", str(export), version=version)
        tree = etree.parse(str(export))
        (node,) = [
            n for n in tree.getroot().iter(f"{NS}DATA-CONSTR") if n.findtext(f"{NS}SHORT-NAME") == "Speed_T_Range"
        ]
        (limit,) = list(node.iter(f"{NS}{tag}"))
        limit.text = text
        export.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
        return export

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize(
        ("tag", "text", "target"),
        [
            ("LOWER-LIMIT", "-INF", {"value_max": 300.0}),
            ("UPPER-LIMIT", "INF", {"value_min": 0.0}),
            ("UPPER-LIMIT", "+INF", {"value_min": 0.0}),
        ],
        ids=["lower-minus-inf", "upper-inf", "upper-plus-inf"],
    )
    def test_an_own_side_infinity_is_the_unbounded_side_and_is_reused(
        self, tmp_path: Path, version: str, tag: str, text: str, target: dict
    ) -> None:
        model_root = self._target(tmp_path, **target)
        before = files_of(model_root)

        report = import_component(str(self._respelled_export(tmp_path, version, tag, text)), str(model_root))

        assert "Speed_T" in report.reused_data_types
        assert files_of(model_root) == before

    @pytest.mark.parametrize("version", VERSIONS)
    @pytest.mark.parametrize(("tag", "text"), [("LOWER-LIMIT", "INF"), ("UPPER-LIMIT", "-INF")])
    def test_an_other_side_infinity_is_refused_by_name(self, tmp_path: Path, version: str, tag: str, text: str) -> None:
        model_root = self._target(tmp_path, value_min=0.0, value_max=300.0)
        before = files_of(model_root)

        with pytest.raises(LossyComponentImportError) as caught:
            import_component(str(self._respelled_export(tmp_path, version, tag, text)), str(model_root))

        assert f"a {tag} spelling the other side's infinity" in str(caught.value)
        assert files_of(model_root) == before


class TestAComponentFileCarriesAConversionsLimits:
    """``import component`` creates, reuses or refuses an application type by its ``conversion.limits`` too.

    The created file states the bound exactly; a target differing only in its limits is
    a conflict, never a silent reuse.
    """

    LIMITS: ClassVar[dict[str, Any]] = {"upper": {"value": 2**53 + 1, "interval": "open"}}

    @staticmethod
    def _conversion(limits: dict[str, Any]) -> dict[str, Any]:
        return {"conversion": {"factor": 2, "offset": 0, "limits": limits}}

    def _export(self, tmp_path: Path) -> Path:
        model_root = _exchange_model(tmp_path / "source" / "model", Velocity_T=self._conversion(self.LIMITS))
        out = tmp_path / "source" / "SensorSwc.arxml"
        export_component(str(model_root), "SensorSwc", str(out))
        return out

    def test_a_created_type_states_the_limits_exactly(self, tmp_path: Path) -> None:
        project = init_project(tmp_path / "target")

        report = import_component(str(self._export(tmp_path)), str(project / "model"))

        assert "Velocity_T" in report.created_data_types
        written = _load(model_element_path(project / "model", "Velocity_T", kind="application_type"))
        assert written["conversion"] == {"factor": 2.0, "offset": 0.0, "limits": self.LIMITS}
        assert type(written["conversion"]["limits"]["upper"]["value"]) is int

    def test_the_same_limits_are_reused(self, tmp_path: Path) -> None:
        project = init_project(tmp_path / "target")
        shutil.rmtree(project / "model")
        model_root = _exchange_model(project / "model", Velocity_T=self._conversion(self.LIMITS))
        before = files_of(model_root)

        report = import_component(str(self._export(tmp_path)), str(model_root))

        assert "Velocity_T" in report.reused_data_types
        assert files_of(model_root) == before

    def test_limits_differing_only_in_an_interval_are_a_conflict(self, tmp_path: Path) -> None:
        project = init_project(tmp_path / "target")
        shutil.rmtree(project / "model")
        closed = {"upper": {"value": 2**53 + 1, "interval": "closed"}}
        model_root = _exchange_model(project / "model", Velocity_T=self._conversion(closed))
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError) as caught:
            import_component(str(self._export(tmp_path)), str(model_root))

        assert "Velocity_T" in str(caught.value)
        assert files_of(model_root) == before


class TestCheckModelJudgesAnInitAgainstTheExactBound:
    """``rule:interface.init-out-of-range``: 2**64 is above a ``value_max`` of 2**64 - 1, with no float round trip."""

    @staticmethod
    def _model(tmp_path: Path, data_type: dict[str, Any], init: int) -> Path:
        root = tmp_path / "model"
        (root / "data_types").mkdir(parents=True)
        (root / "interfaces").mkdir()
        base = {"name": "uint64_base", "kind": "base_type", "size": 64, "encoding": "NONE"}
        for document in (base, data_type):
            path = model_element_path(root, document["name"], kind=document["kind"])
            path.parent.mkdir(parents=True, exist_ok=True)
            _dump(path, document)
        interface = {
            "name": "SR_Probe",
            "kind": "sender_receiver",
            "data_elements": [
                {
                    "name": "Value",
                    (
                        "application_type" if data_type["kind"] == "application_type" else "implementation_type"
                    ): data_type["name"],
                    "init": init,
                }
            ],
        }
        _dump(root / "interfaces" / "SR_Probe.yaml", interface)
        return root

    @pytest.mark.parametrize(
        "data_type",
        [
            {
                "name": "Wide",
                "kind": "value_type",
                "base": "uint64_base",
                "value_min": INT64_MIN,
                "value_max": UINT64_MAX,
            },
            {"name": "Wide", "kind": "application_type", "range": {"min": INT64_MIN, "max": UINT64_MAX}},
        ],
        ids=["value_type", "application_type"],
    )
    @pytest.mark.parametrize(
        ("init", "word"),
        [(UINT64_MAX, None), (INT64_MIN, None), (UINT64_MAX + 1, "above"), (INT64_MIN - 1, "below")],
    )
    def test_the_bound_itself_conforms_and_one_past_it_does_not(
        self, tmp_path: Path, data_type: dict[str, Any], init: int, word: str | None
    ) -> None:
        records = list(check_init_conformance(str(self._model(tmp_path, data_type, init))).records)

        if word is None:
            assert records == []
            return
        (record,) = records
        assert record.rule == "interface.init-out-of-range"
        assert f"is {init}, which is {word}" in record.message
        assert str(UINT64_MAX if word == "above" else INT64_MIN) in record.message
