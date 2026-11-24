"""A compu method's scale limits are carried: the model, the writer, the importer and the round trip.

``[TPS_SWCT_01548]`` (SWCT R4.2.2 ``:16830-16833``, R4.3.0 ``:18289-18292``) reads a
``CompuScale``'s ``lowerLimit`` / ``upperLimit`` as the limiting values *before* the conversion is
applied -- the conversion's scope, which an MCD tool honours (``[TPS_SWCT_01280]``) and which
decides how an ``invalidValue`` is written (``[constr_1283]``) -- and the type's physical range does
not state it. So ``linear`` and an application type's inline ``conversion`` each carry an optional
``limits``, on the stored side, each bound a ``value`` and an ``interval``.

What this module holds:

* the two coherence rules (``rule:compu_methods.linear.limits-coherent``,
  ``rule:application_types.conversion.limits-coherent``): equal closed limits are a point and
  coherent, equal limits with a side open are an empty interval and refused, a reversed pair is
  refused, and an integer above 2**53 is held exactly;
* the writer's emission: ``LOWER-LIMIT`` / ``UPPER-LIMIT`` with their ``INTERVAL-TYPE`` on the one
  scale, before ``COMPU-RATIONAL-COEFFS``, for a declared ``linear`` and for an application type's
  generated ``{name}_Compu`` alike, and nothing where no limit is declared;
* the round trip YAML -> ARXML -> YAML: closed, open and one-sided limits come back as written, the
  ``linear`` as its element and the inline conversion still inline (its private form read back);
* the interface rules compare a conversion's limits as identity (``[constr_1163]``).

What it does not reach: schema validity (``test_arxml_xsd_conformance.py``'s
``TestTheScaleLimitsConform``, under ``test-xsd``), the importer's report lines for what it cannot
carry (family F23, ``test_uncarried_losses.py``), and real tool exports (``dev.py test-internal``'s
input-vs-output oracle).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT
from lxml import etree
from project_support import init_project
from pydantic import ValidationError
from swcraft_tools.arxml_import import run_import
from swcraft_tools.schema import Conversion, ConversionLimitBound, ConversionLimits, Linear
from swcraft_tools.validator.compatibility import _conversions_compatible
from swcraft_tools.writer.arxml_generation import generate_arxml

FIXTURE = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "data_description" / "model"
AR = "{http://autosar.org/schema/r4.0}"

#: The limits the tree below declares: a ``linear`` with both sides, the upper one open and an
#: integer above 2**53, and an inline conversion with one closed side.
LINEAR_LIMITS: dict[str, Any] = {
    "lower": {"value": -100, "interval": "closed"},
    "upper": {"value": 18446744073709551615, "interval": "open"},
}
INLINE_LIMITS: dict[str, Any] = {"upper": {"value": 2.5, "interval": "closed"}}


def limits_tree(tmp_path: Path) -> Path:
    """The data description fixture tree with limits on its ``linear`` and on its inline conversion."""
    model_root = tmp_path / "model"
    shutil.copytree(FIXTURE, model_root)
    for path, update in (
        (model_root / "data_types/compu_methods/ProbeLinear.yaml", lambda doc: doc.update(limits=LINEAR_LIMITS)),
        (
            model_root / "data_types/application/ProbeMixed.yaml",
            lambda doc: doc["conversion"].update(limits=INLINE_LIMITS),
        ),
    ):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        update(document)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    return model_root


def _method(root: etree._Element, name: str) -> etree._Element:
    (method,) = [m for m in root.iter(f"{AR}COMPU-METHOD") if m.findtext(f"{AR}SHORT-NAME") == name]
    return method


def _scale(method: etree._Element) -> etree._Element:
    (scale,) = method.findall(f"{AR}COMPU-INTERNAL-TO-PHYS/{AR}COMPU-SCALES/{AR}COMPU-SCALE")
    return scale


def _limits(scale: etree._Element) -> list[tuple[str, str | None, str | None]]:
    return [
        (etree.QName(node).localname, node.text, node.get("INTERVAL-TYPE"))
        for node in scale
        if etree.QName(node).localname in ("LOWER-LIMIT", "UPPER-LIMIT")
    ]


class TestTheCoherenceRules:
    @pytest.mark.parametrize("owner", ["linear", "conversion"])
    @pytest.mark.parametrize(
        ("limits", "coherent"),
        [
            ({"lower": {"value": 5}, "upper": {"value": 5}}, True),
            ({"lower": {"value": 5, "interval": "open"}, "upper": {"value": 5}}, False),
            ({"lower": {"value": 5}, "upper": {"value": 5, "interval": "open"}}, False),
            ({"lower": {"value": 6}, "upper": {"value": 5}}, False),
            ({"lower": {"value": 6}}, True),
            ({"upper": {"value": -1, "interval": "open"}}, True),
            ({}, True),
        ],
        ids=["equal-closed", "equal-lower-open", "equal-upper-open", "reversed", "lower-only", "upper-only", "none"],
    )
    def test_a_pair_is_coherent_only_when_it_holds_a_value(
        self, owner: str, limits: dict[str, Any], coherent: bool
    ) -> None:
        def build() -> object:
            if owner == "linear":
                return Linear(name="L", kind="linear", unit="U", limits=limits)
            return Conversion(limits=limits)

        if coherent:
            build()
        else:
            with pytest.raises(ValidationError, match="limits"):
                build()

    def test_an_integer_above_two_to_the_fifty_three_is_held_exactly(self) -> None:
        bound = ConversionLimitBound(value=18446744073709551615)
        assert isinstance(bound.value, int) and bound.value == 18446744073709551615
        assert bound.interval == "closed"

    @pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
    def test_an_infinite_bound_is_no_value(self, value: float) -> None:
        """An infinite limit is an absent bound: the model holds no infinity."""
        with pytest.raises(ValidationError):
            ConversionLimitBound(value=value)

    def test_an_unknown_interval_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ConversionLimitBound(value=1, interval="infinite")


class TestTheWriterWritesTheLimits:
    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_each_declared_limit_is_written_on_the_scale_before_its_coefficients(
        self, tmp_path: Path, version: str
    ) -> None:
        output = tmp_path / "out.arxml"
        generate_arxml(str(limits_tree(tmp_path)), str(output), version=version)
        root = etree.parse(str(output)).getroot()

        linear = _scale(_method(root, "ProbeLinear"))
        assert [etree.QName(node).localname for node in linear] == [
            "LOWER-LIMIT",
            "UPPER-LIMIT",
            "COMPU-RATIONAL-COEFFS",
        ]
        assert _limits(linear) == [
            ("LOWER-LIMIT", "-100", "CLOSED"),
            ("UPPER-LIMIT", "18446744073709551615", "OPEN"),
        ]
        inline = _scale(_method(root, "ProbeMixed_Compu"))
        assert [etree.QName(node).localname for node in inline] == ["UPPER-LIMIT", "COMPU-RATIONAL-COEFFS"]
        assert _limits(inline) == [("UPPER-LIMIT", "2.5", "CLOSED")]

    def test_a_conversion_declaring_no_limit_writes_none(self, tmp_path: Path) -> None:
        output = tmp_path / "out.arxml"
        generate_arxml(str(FIXTURE), str(output), version="4.3.0")
        root = etree.parse(str(output)).getroot()
        for name in ("ProbeLinear", "ProbeMixed_Compu"):
            assert _limits(_scale(_method(root, name))) == [], name


class TestTheRoundTrip:
    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_the_limits_come_back_as_written_and_the_inline_conversion_stays_inline(
        self, tmp_path: Path, version: str
    ) -> None:
        source = tmp_path / "source.arxml"
        generate_arxml(str(limits_tree(tmp_path)), str(source), version=version)
        project = init_project(tmp_path / "project")
        report = run_import(str(source), str(project / "swcraft.yaml"))
        assert not [entry for entry in report.skip_entries if "COMPU-SCALE" in entry.reason]

        model_root = project / "model"
        linear = yaml.safe_load((model_root / "data_types/compu_methods/ProbeLinear.yaml").read_text(encoding="utf-8"))
        assert linear["limits"] == LINEAR_LIMITS
        # The one exact-number rule: an integer limit a float holds comes back that float, an integer
        # above that an `int`; a float limit stays a float.
        assert type(linear["limits"]["lower"]["value"]) is float
        assert type(linear["limits"]["upper"]["value"]) is int
        mixed = yaml.safe_load((model_root / "data_types/application/ProbeMixed.yaml").read_text(encoding="utf-8"))
        assert mixed["conversion"]["limits"] == INLINE_LIMITS
        assert type(mixed["conversion"]["limits"]["upper"]["value"]) is float
        assert "compu_method_ref" not in mixed, "SWCraft's own form with limits is read back inline"

        again = tmp_path / "again.arxml"
        generate_arxml(str(model_root), str(again), version=version)
        first, second = etree.parse(str(source)).getroot(), etree.parse(str(again)).getroot()
        for name in ("ProbeLinear", "ProbeMixed_Compu"):
            assert _limits(_scale(_method(second, name))) == _limits(_scale(_method(first, name))), name


class TestTheInterfaceRulesCompareTheLimits:
    @pytest.mark.parametrize("empty", [{}, {"lower": None, "upper": None}], ids=["empty", "two-nulls"])
    def test_limits_stating_neither_bound_are_no_limits(self, empty: dict[str, Any]) -> None:
        """``limits: {}`` states what an absent ``limits`` states.

        The writer writes no limit for either, so the interface rules see no difference, and print none against a stated
        scope."""
        conversion = Conversion.model_validate({"limits": empty})
        assert _conversions_compatible(conversion, Conversion()) is None
        assert _conversions_compatible(Conversion(), conversion) is None
        bounded = Conversion(limits=ConversionLimits(upper=ConversionLimitBound(value=10)))
        assert _conversions_compatible(bounded, conversion) == ("application type conversion limits (-inf, 10] vs none")

    def test_a_component_file_writes_no_empty_limits(self) -> None:
        """The component exchange's dump leaves an empty ``limits`` out, as the writer writes none for it."""
        from swcraft_tools.schema import ApplicationDataType
        from swcraft_tools.writer.yaml_documents import data_type_to_dict

        application = ApplicationDataType(
            name="T", kind="application_type", conversion=Conversion(factor=2, limits=ConversionLimits())
        )
        assert data_type_to_dict(application)["conversion"] == {"factor": 2, "offset": 0.0}

    def test_two_conversions_differing_only_in_their_limits_are_not_compatible(self) -> None:
        closed = Conversion(limits=ConversionLimits(upper=ConversionLimitBound(value=10)))
        opened = Conversion(limits=ConversionLimits(upper=ConversionLimitBound(value=10, interval="open")))
        assert _conversions_compatible(closed, closed) is None
        assert _conversions_compatible(closed, opened) == (
            "application type conversion limits (-inf, 10] vs (-inf, 10)"
        )
        assert _conversions_compatible(closed, Conversion()) == (
            "application type conversion limits (-inf, 10] vs none"
        )
