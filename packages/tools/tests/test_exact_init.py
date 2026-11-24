"""A data element's ``init`` is held exactly, or not carried and named, as a bound is.

``Numerical`` is a string with a pattern, so a source states its number exactly.
SWCraft holds an ``int`` of any width or an IEEE double:

* an integer spelling is the ``int``, above 2**53 too, written back digit for digit;
* a float spelling is the double only where ``Decimal(str(value))`` equals the text;
* a float spelling no double holds exactly (underflow to zero included) is not carried:
  the element lands without its ``init`` and the report names the value, *not supported*
  and behavioural.

A float spelling whose exponent ``decimal`` cannot construct
(``1e-99999999999999999999``) is the same outcome with a sentence of its own, never a
``decimal.InvalidOperation`` aborting the import; the same texts as a bound, a
coefficient, a denominator, a scale limit and a merge row's limit are each one refused
value (the last class of this file).

What this does **not** reach: a YAML float literal the loader has already rounded; an
array or record init is refused whole when one leaf is inexact; and ``check-model``'s
range rule sees no init on the refused element.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import model_element_path
from lxml import etree
from project_support import init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.merge import _coefficient, _denominator_is_one, linear_scale_limits
from swcraft_tools.arxml_import.merge_verdict import _limit_signature
from swcraft_tools.arxml_reader import (
    CompuMethodInfo,
    CompuRationalScaleInfo,
    SkipCategory,
    _read_constant_scalar,
    _stated_decimal,
    exact_decimal,
    parse_exact_number,
)
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.cli import main
from swcraft_tools.writer.arxml_generation import generate_arxml

NS = f"{{{NAMESPACE}}}"
TESTS = Path(__file__).resolve().parent
EXCHANGE_MODEL_ROOT = TESTS / "fixtures" / "component_exchange" / "model"
VERSIONS = ("4.2.2", "4.3.0")
ELEMENT = "/PortInterfaces/IVehicleSpeed/speed"

#: Float spellings ``float()`` reads (as zero) and ``decimal`` refuses to
#: construct: the exponent has more digits than its limits hold.
BEYOND_DECIMAL = [
    pytest.param("1e-99999999999999999999", id="one-times-ten-to-minus-huge"),
    pytest.param("0.0e99999999999999999999", id="zero-times-ten-to-huge"),
    pytest.param("0e-9999999999999999999999", id="zero-times-ten-to-minus-huge"),
]
BEYOND_DECIMAL_TEXTS = tuple(param.values[0] for param in BEYOND_DECIMAL)
#: The two the formula grammar spells (a zero mantissa before an exponent is none of its
#: literals); every field re-spelled below is a formula field.
FORMULA_BEYOND_DECIMAL = frozenset(BEYOND_DECIMAL_TEXTS[:2])


def _linear_method(
    *,
    lower: str | None = None,
    upper: str | None = None,
    lower_interval: str | None = None,
    upper_interval: str | None = None,
) -> CompuMethodInfo:
    """A ``LINEAR`` method of one scale ``[0, 1] / [1]`` with the given limits."""
    return CompuMethodInfo(
        path="/P/M",
        name="M",
        category="LINEAR",
        scales=[],
        description="",
        rational_scales=[
            CompuRationalScaleInfo(
                numerator=("0", "1"),
                denominator=("1",),
                lower_limit=lower,
                lower_interval=lower_interval,
                upper_limit=upper,
                upper_interval=upper_interval,
            )
        ],
        scale_count=1,
    )


FORMULA_BEYOND_DECIMAL_PARAMS = [param for param in BEYOND_DECIMAL if param.values[0] in FORMULA_BEYOND_DECIMAL]

UINT64_MAX = 18446744073709551615
INT64_MIN = -9223372036854775808


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _model(root: Path, init: int | float, *, element_type: str = "Speed_T") -> Path:
    """The ``component_exchange`` tree, ``IVehicleSpeed.speed`` typed *element_type* and initialized to *init*."""
    shutil.copytree(EXCHANGE_MODEL_ROOT, root)
    path = root / "interfaces" / "IVehicleSpeed.yaml"
    declared = _load(path)
    declared["data_elements"][0].update({"type": element_type, "init": init})
    path.write_text(yaml.safe_dump(declared, sort_keys=False), encoding="utf-8", newline="\n")
    return root


def _init_node(tree: etree._ElementTree) -> etree._Element:
    (constant,) = [
        node
        for node in tree.getroot().iter(f"{NS}CONSTANT-SPECIFICATION")
        if node.findtext(f"{NS}SHORT-NAME") == "IVehicleSpeed_speed_Init"
    ]
    (value,) = [node for node in constant.iter() if node.tag in (f"{NS}VALUE", f"{NS}V")]
    return value


def _init_text(arxml: Path) -> str:
    return _init_node(etree.parse(str(arxml))).text


def _source(tmp_path: Path, version: str, text: str, *, element_type: str = "Speed_T") -> Path:
    """SWCraft's own export with the element's init constant re-spelled *text*, as a foreign exporter wrote it."""
    out = tmp_path / "source.arxml"
    generate_arxml(str(_model(tmp_path / "source", 1.5, element_type=element_type)), str(out), version=version)
    tree = etree.parse(str(out))
    _init_node(tree).text = text
    out.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return out


def _import(source: Path, tmp_path: Path) -> tuple[Path, dict[str, Any], list[Any]]:
    project = init_project(tmp_path / "project")
    report = run_import(str(source), str(project / "swcraft.yaml"))
    model_root = project / "model"
    (element,) = _load(model_root / "interfaces" / "IVehicleSpeed.yaml")["data_elements"]
    return model_root, element, [entry for entry in report.skip_entries if entry.path == ELEMENT]


@pytest.mark.parametrize("version", VERSIONS)
class TestAnIntegerInitAboveTwoToTheFiftyThirdIsExact:
    @pytest.mark.parametrize("value", [UINT64_MAX, INT64_MIN, 2**53 + 1], ids=["uint64-max", "int64-min", "2^53+1"])
    def test_from_hand_authored_yaml_through_generate_and_import(
        self, tmp_path: Path, version: str, value: int
    ) -> None:
        source = tmp_path / "model.arxml"
        generate_arxml(str(_model(tmp_path / "source", value, element_type="float32")), str(source), version=version)
        assert _init_text(source) == str(value)

        model_root, element, lines = _import(source, tmp_path)

        assert (element["init"], type(element["init"])) == (value, int)
        assert lines == []
        again = tmp_path / "again.arxml"
        generate_arxml(str(model_root), str(again), version=version)
        assert _init_text(again) == str(value)

    @pytest.mark.parametrize("text", ["18446744073709551615", "0xFFFFFFFFFFFFFFFF"], ids=["decimal", "hexadecimal"])
    def test_from_a_foreign_spelling(self, tmp_path: Path, version: str, text: str) -> None:
        _root, element, _lines = _import(_source(tmp_path, version, text, element_type="float32"), tmp_path)
        assert (element["init"], type(element["init"])) == (UINT64_MAX, int)


@pytest.mark.parametrize("version", VERSIONS)
class TestAFloatSpellingNoDoubleHoldsIsNotCarriedAndIsNamed:
    @pytest.mark.parametrize(
        ("text", "nearest"),
        [
            ("16777217.000000001", "16777217.0"),
            ("0.10000000000000001", "0.1"),
            ("1e-400", "0.0"),
        ],
        ids=["above-the-mantissa", "one-digit-past", "underflow-to-zero"],
    )
    def test_the_element_lands_without_its_init_and_one_line_says_why(
        self, tmp_path: Path, version: str, text: str, nearest: str
    ) -> None:
        model_root, element, lines = _import(_source(tmp_path, version, text), tmp_path)

        assert "init" not in element
        (line,) = lines
        assert (line.tag, line.category, line.rule, line.effect) == (
            "INIT-VALUE",
            SkipCategory.NOT_SUPPORTED,
            None,
            "abort",
        )
        assert f"value '{text}' is a number SWCraft cannot hold exactly" in line.reason
        assert f"the nearest one is {nearest}" in line.reason
        # Nothing rounded reaches the output: the regenerated file states no init.
        again = tmp_path / "again.arxml"
        generate_arxml(str(model_root), str(again), version=version)
        assert not [
            node
            for node in etree.parse(str(again)).getroot().iter(f"{NS}CONSTANT-SPECIFICATION")
            if node.findtext(f"{NS}SHORT-NAME") == "IVehicleSpeed_speed_Init"
        ]

    def test_an_overflow_is_refused_as_before(self, tmp_path: Path, version: str) -> None:
        _root, element, lines = _import(_source(tmp_path, version, "1e400"), tmp_path)
        assert "init" not in element
        (line,) = lines
        assert (line.category, line.effect) == (SkipCategory.NOT_SUPPORTED, "abort")
        assert "value '1e400' is not a finite number" in line.reason

    @pytest.mark.parametrize(("text", "value"), [("0.1", 0.1), ("2.5e2", 250.0), ("0.30000000000000004", 0.1 + 0.2)])
    def test_a_float_spelling_a_double_holds_is_carried(
        self, tmp_path: Path, version: str, text: str, value: float
    ) -> None:
        _root, element, lines = _import(_source(tmp_path, version, text), tmp_path)
        assert (element["init"], type(element["init"])) == (value, float)
        assert lines == []


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("text", FORMULA_BEYOND_DECIMAL_PARAMS)
class TestAnExponentDecimalDoesNotConstructIsOneNamedLineNeverAnAbortedImport:
    def test_the_element_lands_without_its_init_and_one_line_says_why(
        self, tmp_path: Path, version: str, text: str
    ) -> None:
        model_root, element, lines = _import(_source(tmp_path, version, text), tmp_path)

        assert "init" not in element
        (line,) = lines
        assert (line.tag, line.category, line.rule, line.effect) == (
            "INIT-VALUE",
            SkipCategory.NOT_SUPPORTED,
            None,
            "abort",
        )
        assert f"value '{text}' has an exponent beyond the range SWCraft compares exactly" in line.reason
        again = tmp_path / "again.arxml"
        generate_arxml(str(model_root), str(again), version=version)
        assert not [
            node
            for node in etree.parse(str(again)).getroot().iter(f"{NS}CONSTANT-SPECIFICATION")
            if node.findtext(f"{NS}SHORT-NAME") == "IVehicleSpeed_speed_Init"
        ]

    def test_import_model_succeeds_through_the_cli(
        self,
        tmp_path: Path,
        version: str,
        text: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Guards a defect: this argv ended in ``Error: [<class 'decimal.InvalidOperation'>]`` and exit 1."""
        source = _source(tmp_path, version, text)
        project = init_project(tmp_path / "project")
        capsys.readouterr()
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "import", "model", "--source", str(source), "--project", str(project / "swcraft.yaml")],
        )

        main()

        captured = capsys.readouterr()
        assert "InvalidOperation" not in captured.out + captured.err
        assert captured.out.count(f"value '{text}' has an exponent beyond the range") == 1
        (element,) = _load(project / "model" / "interfaces" / "IVehicleSpeed.yaml")["data_elements"]
        assert "init" not in element

    def test_a_constant_no_element_references_does_not_abort_either(
        self, tmp_path: Path, version: str, text: str
    ) -> None:
        """A constant no element references does not abort the import either."""
        source = tmp_path / "model.arxml"
        generate_arxml(str(_model(tmp_path / "source", 1.5)), str(source), version=version)
        tree = etree.parse(str(source))
        (constant,) = [
            node
            for node in tree.getroot().iter(f"{NS}CONSTANT-SPECIFICATION")
            if node.findtext(f"{NS}SHORT-NAME") == "IVehicleSpeed_speed_Init"
        ]
        orphan = etree.fromstring(etree.tostring(constant))
        orphan.find(f"{NS}SHORT-NAME").text = "Orphan_Init"
        (value,) = [node for node in orphan.iter() if node.tag in (f"{NS}VALUE", f"{NS}V")]
        value.text = text
        constant.addnext(orphan)
        source.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))

        _root, element, lines = _import(source, tmp_path)

        assert (element["init"], lines) == (1.5, [])

    def test_as_a_bound_the_range_is_not_carried_and_is_named(self, tmp_path: Path, version: str, text: str) -> None:
        source = tmp_path / "model.arxml"
        generate_arxml(str(_model(tmp_path / "source", 1.5)), str(source), version=version)
        tree = etree.parse(str(source))
        (constr,) = [
            node
            for node in tree.getroot().iter(f"{NS}DATA-CONSTR")
            if node.findtext(f"{NS}SHORT-NAME") == "Velocity_T_Range"
        ]
        next(constr.iter(f"{NS}LOWER-LIMIT")).text = text
        source.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
        project = init_project(tmp_path / "project")

        report = run_import(str(source), str(project / "swcraft.yaml"))

        # The bound is not imported, the other one is, and the type's note names it.
        assert _load(model_element_path(project / "model", "Velocity_T", kind="application_type"))["range"] == {
            "max": 400.0
        }
        rendered = report.render(verbose=True)
        assert rendered.count("LOWER-LIMIT no model value holds exactly; that bound is not imported") == 1

    def test_as_a_coefficient_the_conversion_is_not_carried_and_is_named(
        self, tmp_path: Path, version: str, text: str
    ) -> None:
        source = tmp_path / "model.arxml"
        generate_arxml(str(_model(tmp_path / "source", 1.5)), str(source), version=version)
        tree = etree.parse(str(source))
        (compu,) = [
            node
            for node in tree.getroot().iter(f"{NS}COMPU-METHOD")
            if node.findtext(f"{NS}SHORT-NAME") == "Velocity_T_Compu"
        ]
        numerator = next(compu.iter(f"{NS}COMPU-NUMERATOR"))
        numerator.findall(f"{NS}V")[1].text = text
        source.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
        project = init_project(tmp_path / "project")

        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert "conversion" not in _load(model_element_path(project / "model", "Velocity_T", kind="application_type"))
        rendered = report.render(verbose=True)
        assert (
            rendered.count(
                "has a COMPU-NUMERATOR coefficient no model value holds exactly; "
                "the type is imported without a conversion and nothing is rounded"
            )
            == 1
        )


@pytest.mark.parametrize("version", VERSIONS)
class TestAnOctalInitIsReadInItsRadix:
    """An octal init is read in radix eight, never as ten.

    The init's ``VALUE`` is a formula field: ``010`` is the ``OctIntegerLiteral`` eight (GST R4.3.0:4782-4783;
    R4.2.2:3936-3937), and ``-010`` the unary minus on it (R4.3.0:4667-4668; R4.2.2:3834-3835).
    """

    @pytest.mark.parametrize(("text", "value"), [("010", 8), ("-010", -8), ("0", 0)])
    def test_the_value_is_the_octal_one(self, tmp_path: Path, version: str, text: str, value: int) -> None:
        _root, element, lines = _import(_source(tmp_path, version, text), tmp_path)
        assert (element["init"], type(element["init"]), lines) == (value, int, [])

    def test_the_radix_it_was_read_from_is_named(self, tmp_path: Path, version: str) -> None:
        project = init_project(tmp_path / "project")
        report = run_import(str(_source(tmp_path, version, "010")), str(project / "swcraft.yaml"))
        rendered = report.render(verbose=True)
        assert rendered.count("init read from 010, written decimal") == 1
        # The section's heading names the octal radix too, not hexadecimal alone.
        assert "Init values re-spelled from hexadecimal, octal or binary to decimal (1;" in rendered


@pytest.mark.parametrize("version", VERSIONS)
class TestAZeroMantissaWithAnExponentInAFormulaFieldIsNoNumber:
    """``0e-…`` in a formula field is no number: one named line, no init, never an aborted import.

    The init's ``VALUE`` is a ``NUMERICAL-VALUE-VARIATION-POINT``, a formula field, and ``0e-…`` is none of the
    formula's literals (GST R4.3.0:4795-4800; R4.2.2:3949-3952).
    """

    def test_the_element_lands_without_its_init_and_one_line_says_why(self, tmp_path: Path, version: str) -> None:
        (text,) = (value for value in BEYOND_DECIMAL_TEXTS if value not in FORMULA_BEYOND_DECIMAL)
        _root, element, lines = _import(_source(tmp_path, version, text), tmp_path)

        assert "init" not in element
        (line,) = lines
        assert line.tag == "INIT-VALUE"
        assert f"value '{text}' is not a number this reader can read" in line.reason


class TestEveryExactnessJudgementRefusesSuchAnExponentWithoutRaising:
    """The one conversion (``arxml_reader._stated_decimal``) and each of its callers, asked directly."""

    @pytest.mark.parametrize("grammar", ["pattern", "formula"])
    @pytest.mark.parametrize("text", BEYOND_DECIMAL)
    def test_the_shared_conversion_answers_none(self, text: str, grammar: str) -> None:
        assert _stated_decimal(text) is None
        assert exact_decimal(text, grammar=grammar) is None

    @pytest.mark.parametrize("text", BEYOND_DECIMAL)
    def test_an_init_is_a_reason(self, text: str) -> None:
        # An APPLICATION-VALUE-SPECIFICATION's `V`: the pattern grammar spells all three.
        scalar, reason = _read_constant_scalar(text, grammar="pattern")
        assert scalar is None
        assert reason is not None and "has an exponent beyond the range" in reason

    @pytest.mark.parametrize("text", BEYOND_DECIMAL)
    def test_a_bound_and_a_coefficient_are_no_exact_value(self, text: str) -> None:
        assert parse_exact_number(text, grammar="pattern") == (None, True)
        if text in FORMULA_BEYOND_DECIMAL:
            assert parse_exact_number(text, grammar="formula") == (None, True)
            assert _coefficient(text) == (None, True)

    def test_a_zero_mantissa_with_an_exponent_is_no_formula_literal(self) -> None:
        """``0e…`` is a ``NUMERICAL-VALUE`` and no formula literal.

        It is a ``NUMERICAL-VALUE`` by XSD R4.3.0:86078 (``[0-9]`` before the exponent) and no formula literal
        (``DoubleLiteral``'s mantissa is ``0?.`` and digits, or a non-zero first digit -- GST R4.3.0:4795-4800,
        R4.2.2:3949-3952): in a formula field -- a bound, a coefficient -- it is no number at all.
        """
        (text,) = (value for value in BEYOND_DECIMAL_TEXTS if value not in FORMULA_BEYOND_DECIMAL)
        assert parse_exact_number(text, grammar="formula") == (None, False)
        assert _coefficient(text) == (None, False)
        scalar, reason = _read_constant_scalar(text, grammar="formula")
        assert scalar is None and reason is not None and "is not a number this reader can read" in reason

    @pytest.mark.parametrize("text", BEYOND_DECIMAL)
    def test_a_denominator_is_not_one_and_the_question_ends(self, text: str) -> None:
        """``Fraction`` evaluates the exponent as a power of ten: asked this text, it does not return."""
        assert _denominator_is_one((text,)) is False

    @pytest.mark.parametrize(
        ("denominator", "is_one"),
        [
            ((), True),
            (("1",), True),
            (("1.0",), True),
            (("1e0",), True),
            (("2/2",), True),
            (("3",), False),
            # A COMPU-DENOMINATOR's V is read by its numerator's formula grammar: a radix,
            # an octal and a sign before WS are numbers, `08` and `001.0` are not.
            (("0x1",), True),
            (("0b1",), True),
            (("01",), True),
            (("+ 1",), True),
            (("- 1",), False),
            (("08",), False),
            (("001.0",), False),
        ],
    )
    def test_a_denominator_of_one_is_still_read_in_every_spelling(
        self, denominator: tuple[str, ...], is_one: bool
    ) -> None:
        assert _denominator_is_one(denominator) is is_one

    @pytest.mark.parametrize("text", ["1e999999999999999999", "1e-999999999999999999", *BEYOND_DECIMAL])
    def test_a_scale_limit_no_model_value_holds_is_not_carried(self, text: str) -> None:
        """A ``LINEAR`` scale limit no model value holds is not carried (family F23).

        It and the other limit are a behavioural line, since a pair is carried whole,
        never an exception out of the import.
        """
        limits, lines = linear_scale_limits(_linear_method(upper=text, lower="0"))
        assert limits is None
        assert [(line.tag, line.lost) for line in lines] == [("UPPER-LIMIT", True), ("LOWER-LIMIT", True)]

    @pytest.mark.parametrize(
        ("limits", "carried", "lines"),
        [
            pytest.param(
                {"lower": "-INF", "upper": "10"},
                {"upper": {"value": 10.0, "interval": "closed"}},
                [("LOWER-LIMIT", False, "is infinite on its own side")],
                id="own-infinity-beside-a-finite-limit",
            ),
            pytest.param(
                {"lower": "-INF", "upper": "NaN"},
                None,
                [
                    ("LOWER-LIMIT", False, "is infinite on its own side"),
                    ("UPPER-LIMIT", True, "it is not a finite number"),
                ],
                id="own-infinity-beside-an-unreadable-limit",
            ),
            pytest.param(
                {"lower": "0", "upper": "10", "upper_interval": "HALF"},
                None,
                [
                    ("UPPER-LIMIT", True, "it has the INTERVAL-TYPE 'HALF'"),
                    ("LOWER-LIMIT", True, "the scale's UPPER-LIMIT is not readable"),
                ],
                id="an-interval-type-the-model-has-not",
            ),
            pytest.param(
                {"lower": "5", "upper": "5", "lower_interval": "OPEN", "upper_interval": "OPEN"},
                None,
                [("LOWER-LIMIT", True, "an empty interval"), ("UPPER-LIMIT", True, "an empty interval")],
                id="equal-and-both-open",
            ),
        ],
    )
    def test_an_f23_shape(
        self, limits: dict[str, str], carried: dict[str, Any] | None, lines: list[tuple[str, bool, str]]
    ) -> None:
        """Family F23's further shapes are judged without raising.

        An own-side infinity is an absent bound with a descriptive line, beside a carried or an unreadable limit (not
        counted among the stated ones); an unknown ``INTERVAL-TYPE`` is unreadable; two equal open limits state an empty
        interval.
        """
        got, got_lines = linear_scale_limits(_linear_method(**limits))
        assert got == carried
        if got is not None:
            assert [type(bound["value"]) for bound in got.values()] == [float] * len(got)
        assert len(got_lines) == len(lines)
        for line, (tag, lost, phrase) in zip(got_lines, lines, strict=True):
            assert (line.tag, line.lost) == (tag, lost)
            assert phrase in line.reason

    def test_an_ordinary_limit_is_carried_by_its_value(self) -> None:
        """The decimal and the hexadecimal spelling of one limit are one carried value."""
        for text in ("4000", "0x0FA0"):
            limits, lines = linear_scale_limits(_linear_method(upper=text, upper_interval="CLOSED"))
            assert (limits, lines) == ({"upper": {"value": 4000, "interval": "closed"}}, [])

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("4000", 4000.0),
            ("0x0FA0", 4000.0),
            ("010", 8.0),
            ("0", 0.0),
            ("- 8", -8.0),
            ("4000.0", 4000.0),
            ("2.5", 2.5),
            ("18446744073709551615", 18446744073709551615),
        ],
    )
    def test_a_limit_follows_the_one_exact_number_rule(self, text: str, expected: int | float) -> None:
        """A scale limit, a coefficient and a range bound follow ``parse_exact_number``'s one rule.

        An integer is the ``float`` that holds it exactly, an ``int`` only above that (2**64 - 1 here); a float literal
        is the float that holds it.
        """
        limits, lines = linear_scale_limits(_linear_method(upper=text, upper_interval="CLOSED"))
        assert lines == []
        assert limits is not None
        value = limits["upper"]["value"]
        assert (value, type(value)) == (expected, type(expected))

    @pytest.mark.parametrize("text", BEYOND_DECIMAL)
    def test_a_merge_row_compares_the_limit_as_spelled(self, text: str) -> None:
        assert _limit_signature(text, None, "UPPER-LIMIT") == (("text", text), None)
        assert _limit_signature(text, None, "UPPER-LIMIT") != _limit_signature("0", None, "UPPER-LIMIT")
