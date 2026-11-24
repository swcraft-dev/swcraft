"""The importer creates a unit, a compu method and a data constraint as an element, and a type names it.

Each claim runs ``swcraft import model`` on a hand-built export and reads back the YAML and the report.
Every name is plainly synthetic (``Probe…``). A ``UNIT``, a ``TEXTTABLE``, a ``LINEAR``
(``[offset, factor] / [1]``) and a ``DATA-CONSTR`` of one rule of physical limits become elements the
type names by reference; any other shape is a report line and the type keeps the inline path. A
``TEXTTABLE`` that cannot be an enumeration is not created, and both types naming it keep a line. An
implementation type names a ``texttable`` by ``compu_method_ref``, with no line; a ``LINEAR`` is not
carried there and keeps its line.
Repeated names merge into one element when the definitions are equal and into none when they differ;
two differently named forms of equal content conflict. SWCraft's own private form is read back
inline unless one of its four conditions fails.

**Not reached:** the real tool exports (the closed suites); ``import component``; the writer's
emission of the elements (``test_data_description_writer.py``).
"""

from __future__ import annotations

import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from lxml import etree
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.report import ImportReport
from swcraft_tools.arxml_reader import SkipCategory
from swcraft_tools.model_registry import current_registry
from swcraft_tools.validator import check_cross_layer_consistency

# A hand-built export

_HEADER = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
    ' xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-3-0.xsd"><AR-PACKAGES>'
)
_FOOTER = "</AR-PACKAGES></AUTOSAR>\n"
_NS = "{http://autosar.org/schema/r4.0}"


def package(name: str, elements: str = "", children: str = "") -> str:
    sub = f"<AR-PACKAGES>{children}</AR-PACKAGES>" if children else ""
    return f"<AR-PACKAGE><SHORT-NAME>{name}</SHORT-NAME><ELEMENTS>{elements}</ELEMENTS>{sub}</AR-PACKAGE>"


def _desc(text: str | None) -> str:
    return f'<DESC><L-2 L="FOR-ALL">{text}</L-2></DESC>' if text else ""


def _conditional(*references: str) -> str:
    return (
        "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
        + "".join(references)
        + "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
    )


def base_type(name: str = "ProbeByte") -> str:
    return (
        f"<SW-BASE-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>FIXED_LENGTH</CATEGORY>"
        "<BASE-TYPE-SIZE>8</BASE-TYPE-SIZE><BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>"
        "<NATIVE-DECLARATION>uint8</NATIVE-DECLARATION></SW-BASE-TYPE>"
    )


def application(
    name: str, *, unit: str | None = None, compu: str | None = None, constr: str | None = None, desc: str = ""
) -> str:
    references = [
        f'<COMPU-METHOD-REF DEST="COMPU-METHOD">{compu}</COMPU-METHOD-REF>' if compu else "",
        f'<DATA-CONSTR-REF DEST="DATA-CONSTR">{constr}</DATA-CONSTR-REF>' if constr else "",
        f'<UNIT-REF DEST="UNIT">{unit}</UNIT-REF>' if unit else "",
    ]
    props = _conditional(*references) if any(references) else ""
    return (
        f"<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME>{_desc(desc)}<CATEGORY>VALUE</CATEGORY>"
        f"{props}</APPLICATION-PRIMITIVE-DATA-TYPE>"
    )


def value_type(name: str, base: str, *, compu: str | None = None) -> str:
    references = [f'<BASE-TYPE-REF DEST="SW-BASE-TYPE">{base}</BASE-TYPE-REF>']
    if compu:
        references.append(f'<COMPU-METHOD-REF DEST="COMPU-METHOD">{compu}</COMPU-METHOD-REF>')
    return (
        f"<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>VALUE</CATEGORY>"
        f"{_conditional(*references)}</IMPLEMENTATION-DATA-TYPE>"
    )


def reference_type(name: str, target: str, *, compu: str | None = None) -> str:
    references = []
    if compu:
        references.append(f'<COMPU-METHOD-REF DEST="COMPU-METHOD">{compu}</COMPU-METHOD-REF>')
    references.append(
        f'<IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">{target}</IMPLEMENTATION-DATA-TYPE-REF>'
    )
    return (
        f"<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>{name}</SHORT-NAME><CATEGORY>TYPE_REFERENCE</CATEGORY>"
        f"{_conditional(*references)}</IMPLEMENTATION-DATA-TYPE>"
    )


def unit(name: str, display: str | None = "km/h", *, extra: str = "", desc: str = "") -> str:
    shown = f"<DISPLAY-NAME>{display}</DISPLAY-NAME>" if display is not None else ""
    return f"<UNIT><SHORT-NAME>{name}</SHORT-NAME>{_desc(desc)}{shown}{extra}</UNIT>"


def texttable(
    name: str,
    scales: tuple[tuple[int, str, str], ...] = ((0, "ProbeOff", ""), (1, "ProbeOn", "switched on")),
    *,
    desc: str = "",
    extra: str = "",
) -> str:
    body = "".join(
        f"<COMPU-SCALE><SHORT-LABEL>{label}</SHORT-LABEL>{_desc(text)}"
        f'<LOWER-LIMIT INTERVAL-TYPE="CLOSED">{value}</LOWER-LIMIT>'
        f'<UPPER-LIMIT INTERVAL-TYPE="CLOSED">{value}</UPPER-LIMIT>'
        f"<COMPU-CONST><VT>{label}</VT></COMPU-CONST></COMPU-SCALE>"
        for value, label, text in scales
    )
    return (
        f"<COMPU-METHOD><SHORT-NAME>{name}</SHORT-NAME>{_desc(desc)}<CATEGORY>TEXTTABLE</CATEGORY>"
        f"<COMPU-INTERNAL-TO-PHYS><COMPU-SCALES>{body}</COMPU-SCALES></COMPU-INTERNAL-TO-PHYS>{extra}</COMPU-METHOD>"
    )


def linear(
    name: str,
    unit_path: str | None,
    *,
    numerator: tuple[str, ...] = ("0", "1"),
    denominator: tuple[str, ...] = ("1",),
    limits: str = "",
    label: str | None = None,
    category: str = "LINEAR",
    extra: str = "",
    desc: str = "",
) -> str:
    unit_ref = f'<UNIT-REF DEST="UNIT">{unit_path}</UNIT-REF>' if unit_path else ""
    shown = f"<SHORT-LABEL>{label}</SHORT-LABEL>" if label else ""
    return (
        f"<COMPU-METHOD><SHORT-NAME>{name}</SHORT-NAME>{_desc(desc)}<CATEGORY>{category}</CATEGORY>{unit_ref}"
        f"<COMPU-INTERNAL-TO-PHYS><COMPU-SCALES><COMPU-SCALE>{shown}{limits}<COMPU-RATIONAL-COEFFS>"
        "<COMPU-NUMERATOR>" + "".join(f"<V>{v}</V>" for v in numerator) + "</COMPU-NUMERATOR>"
        "<COMPU-DENOMINATOR>" + "".join(f"<V>{v}</V>" for v in denominator) + "</COMPU-DENOMINATOR>"
        f"</COMPU-RATIONAL-COEFFS></COMPU-SCALE></COMPU-SCALES></COMPU-INTERNAL-TO-PHYS>{extra}</COMPU-METHOD>"
    )


def constraint(
    name: str,
    lower: str | None = "0",
    upper: str | None = "100",
    *,
    level: str | None = "0",
    rules: int = 1,
    internal: bool = False,
    desc: str = "",
) -> str:
    limits = (f"<LOWER-LIMIT>{lower}</LOWER-LIMIT>" if lower is not None else "") + (
        f"<UPPER-LIMIT>{upper}</UPPER-LIMIT>" if upper is not None else ""
    )
    rule = (
        "<DATA-CONSTR-RULE>"
        + (f"<CONSTR-LEVEL>{level}</CONSTR-LEVEL>" if level is not None else "")
        + f"<PHYS-CONSTRS>{limits}</PHYS-CONSTRS>"
        + (f"<INTERNAL-CONSTRS>{limits}</INTERNAL-CONSTRS>" if internal else "")
        + "</DATA-CONSTR-RULE>"
    )
    return (
        f"<DATA-CONSTR><SHORT-NAME>{name}</SHORT-NAME>{_desc(desc)}<DATA-CONSTR-RULES>{rule * rules}"
        "</DATA-CONSTR-RULES></DATA-CONSTR>"
    )


def _init_project(tmp_path: Path) -> Path:
    from swcraft_tools.cli import main

    target = tmp_path / "proj"
    argv = sys.argv
    try:
        sys.argv = ["swcraft", "init", str(target)]
        main()
    finally:
        sys.argv = argv
    return target


def _import(tmp_path: Path, *packages: str) -> tuple[ImportReport, Path]:
    """Import one hand-built export of *packages*; return the report and the model root."""
    source = tmp_path / "probe.arxml"
    source.write_text(_HEADER + "".join(packages) + _FOOTER, encoding="utf-8", newline="\n")
    project = _init_project(tmp_path)
    return run_import(str(source), str(project / "swcraft.yaml")), project / "model"


def _layer(model_root: Path, layer: str) -> dict[str, dict[str, Any]]:
    """Every written element of *layer*, keyed by its name."""
    directory = model_root / current_registry().storage(layer).directory
    documents = (
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.yaml"), key=lambda path: path.as_posix())
    )
    return {document["name"]: document for document in documents}


def _lines(report: ImportReport, path: str, tag: str | None = None) -> list[str]:
    return [entry.reason for entry in report.skip_entries if entry.path == path and (tag is None or entry.tag == tag)]


#: Family F2's mark (``report_family_support._FAMILY_MARKS``) -- the sentence the import re-words.
_F2_MARK = "COMPU-METHOD-REF of an IMPLEMENTATION-DATA-TYPE"
_F9_MARK = "CONSTR-LEVEL of a DATA-CONSTR-RULE"

_P = "/ProbeTypes"


# Each source shape to its element


class TestEachSourceShapeIsAnElement:
    def test_a_unit_is_a_unit_with_its_display_name(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                unit("ProbeKmh", "km/h", desc="the speed unit") + application("ProbeSpeed", unit=f"{_P}/ProbeKmh"),
            ),
        )
        assert _layer(model_root, "units") == {
            "ProbeKmh": {"name": "ProbeKmh", "display_name": "km/h", "description": "the speed unit"}
        }
        speed = _layer(model_root, "application_types")["ProbeSpeed"]
        assert speed["unit_ref"] == "ProbeKmh"
        assert "unit" not in speed
        assert not _lines(report, f"{_P}/ProbeKmh")

    def test_a_unit_without_a_display_name_has_an_empty_one(self, tmp_path: Path) -> None:
        _, model_root = _import(
            tmp_path, package("ProbeTypes", unit("ProbeBare", None) + application("ProbeSpeed", unit=f"{_P}/ProbeBare"))
        )
        assert _layer(model_root, "units") == {"ProbeBare": {"name": "ProbeBare"}}

    def test_a_texttable_is_a_texttable_and_its_enumeration_names_it(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                texttable("ProbeModes", desc="the modes") + application("ProbeMode", compu=f"{_P}/ProbeModes"),
            ),
        )
        assert _layer(model_root, "compu_methods") == {
            "ProbeModes": {
                "name": "ProbeModes",
                "kind": "texttable",
                "entries": [
                    {"name": "ProbeOff", "value": 0},
                    {"name": "ProbeOn", "value": 1, "description": "switched on"},
                ],
                "description": "the modes",
            }
        }
        mode = _layer(model_root, "application_types")["ProbeMode"]
        assert mode["kind"] == "enum"
        assert mode["compu_method_ref"] == "ProbeModes"
        assert "literals" not in mode
        assert not [entry for entry in report.skip_entries if entry.tag == "DESC"]

    def test_a_linear_is_a_linear_read_as_offset_then_factor(self, tmp_path: Path) -> None:
        """``COMPU-NUMERATOR`` lists the constant term first: ``[-40, 0.5] / [1]`` is factor 0.5, offset -40."""
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                unit("ProbeDeg", "degC")
                + linear("ProbeScale", f"{_P}/ProbeDeg", numerator=("-40", "0.5"))
                + application("ProbeTemp", unit=f"{_P}/ProbeDeg", compu=f"{_P}/ProbeScale"),
            ),
        )
        assert _layer(model_root, "compu_methods") == {
            "ProbeScale": {"name": "ProbeScale", "kind": "linear", "unit": "ProbeDeg", "factor": 0.5, "offset": -40}
        }
        temp = _layer(model_root, "application_types")["ProbeTemp"]
        assert (temp["kind"], temp["compu_method_ref"], temp["unit_ref"]) == (
            "application_type",
            "ProbeScale",
            "ProbeDeg",
        )
        assert "conversion" not in temp and "unit" not in temp
        assert check_cross_layer_consistency(model_root=str(model_root)).consistent

    def test_a_data_constraint_is_a_data_constr_with_its_level(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                constraint("ProbeLimits", "-5", "250", level="0", desc="the limits")
                + application("ProbeLevel", constr=f"{_P}/ProbeLimits"),
            ),
        )
        assert _layer(model_root, "data_constraints") == {
            "ProbeLimits": {"name": "ProbeLimits", "min": -5, "max": 250, "level": 0, "description": "the limits"}
        }
        assert _layer(model_root, "application_types")["ProbeLevel"]["data_constr_ref"] == "ProbeLimits"
        assert "range" not in _layer(model_root, "application_types")["ProbeLevel"]
        assert not [reason for reason in _lines(report, f"{_P}/ProbeLimits/DATA-CONSTR-RULES") if _F9_MARK in reason]

    def test_a_level_no_integer_states_keeps_family_f9(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes", constraint("ProbeOdd", level="x") + application("ProbeLevel", constr=f"{_P}/ProbeOdd")
            ),
        )
        assert "level" not in _layer(model_root, "data_constraints")["ProbeOdd"]
        assert [reason for reason in _lines(report, f"{_P}/ProbeOdd/DATA-CONSTR-RULES") if _F9_MARK in reason]

    def test_an_element_nobody_references_is_created(self, tmp_path: Path) -> None:
        """Legal, and written beside a base type."""
        report, model_root = _import(
            tmp_path,
            package("ProbeTypes", base_type() + unit("ProbeAlone") + texttable("ProbeLoose") + constraint("ProbeFree")),
        )
        assert set(_layer(model_root, "units")) == {"ProbeAlone"}
        assert set(_layer(model_root, "compu_methods")) == {"ProbeLoose"}
        assert set(_layer(model_root, "data_constraints")) == {"ProbeFree"}
        assert not [entry for entry in report.skip_entries if "not used by any imported" in entry.reason]
        assert not [entry for entry in report.skip_entries if "not carried by any imported" in entry.reason]

    def test_the_project_the_import_writes_passes_check_model(self, tmp_path: Path) -> None:
        _, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                base_type()
                + unit("ProbeKmh")
                + linear("ProbeScale", f"{_P}/ProbeKmh")
                + constraint("ProbeLimits")
                + texttable("ProbeModes")
                + application("ProbeSpeed", unit=f"{_P}/ProbeKmh", compu=f"{_P}/ProbeScale", constr=f"{_P}/ProbeLimits")
                + application("ProbeMode", compu=f"{_P}/ProbeModes")
                + value_type("ProbeRaw", f"{_P}/ProbeByte", compu=f"{_P}/ProbeModes"),
            ),
        )
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues


# The shapes outside, each to its line


class TestEachShapeOutsideIsALine:
    @pytest.mark.parametrize("tag", ["FACTOR-SI-TO-UNIT", "OFFSET-SI-TO-UNIT", "PHYSICAL-DIMENSION-REF"])
    def test_a_unit_stating_an_si_relation(self, tmp_path: Path, tag: str) -> None:
        relation = (
            f'<{tag} DEST="PHYSICAL-DIMENSION">/ProbeDims/ProbeLength</{tag}>'
            if tag == "PHYSICAL-DIMENSION-REF"
            else f"<{tag}>1</{tag}>"
        )
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                unit("ProbeMetre", "m", extra=relation) + application("ProbeDistance", unit=f"{_P}/ProbeMetre"),
            ),
        )
        assert _layer(model_root, "units") == {}
        assert _layer(model_root, "application_types")["ProbeDistance"]["unit"] == "m"
        (line,) = _lines(report, f"{_P}/ProbeMetre", "UNIT")
        assert tag in line
        assert "not judged" in line

    def test_a_compu_method_of_another_category(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                unit("ProbeKmh")
                + linear("ProbeScaled", f"{_P}/ProbeKmh", category="SCALE_LINEAR")
                + application("ProbeSpeed", unit=f"{_P}/ProbeKmh", compu=f"{_P}/ProbeScaled"),
            ),
        )
        assert _layer(model_root, "compu_methods") == {}
        assert "conversion" not in _layer(model_root, "application_types")["ProbeSpeed"]
        assert [line for line in _lines(report, f"{_P}/ProbeScaled", "COMPU-METHOD") if "SCALE_LINEAR" in line]

    def test_a_compu_method_stating_the_physical_to_internal_direction(self, tmp_path: Path) -> None:
        inverse = (
            "<COMPU-PHYS-TO-INTERNAL><COMPU-SCALES><COMPU-SCALE><COMPU-RATIONAL-COEFFS>"
            "<COMPU-NUMERATOR><V>0</V><V>1</V></COMPU-NUMERATOR><COMPU-DENOMINATOR><V>1</V></COMPU-DENOMINATOR>"
            "</COMPU-RATIONAL-COEFFS></COMPU-SCALE></COMPU-SCALES></COMPU-PHYS-TO-INTERNAL>"
        )
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                unit("ProbeKmh")
                + linear("ProbeBoth", f"{_P}/ProbeKmh", extra=inverse)
                + application("ProbeSpeed", unit=f"{_P}/ProbeKmh", compu=f"{_P}/ProbeBoth"),
            ),
        )
        assert _layer(model_root, "compu_methods") == {}
        speed = _layer(model_root, "application_types")["ProbeSpeed"]
        assert speed["conversion"] == {"factor": 1, "offset": 0}
        (line,) = _lines(report, f"{_P}/ProbeBoth", "COMPU-METHOD")
        assert "COMPU-PHYS-TO-INTERNAL" in line

    def test_a_linear_whose_denominator_is_not_one(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                unit("ProbeKmh")
                + linear("ProbeRatio", f"{_P}/ProbeKmh", numerator=("0", "1"), denominator=("10",))
                + application("ProbeSpeed", unit=f"{_P}/ProbeKmh", compu=f"{_P}/ProbeRatio"),
            ),
        )
        assert _layer(model_root, "compu_methods") == {}
        assert "conversion" not in _layer(model_root, "application_types")["ProbeSpeed"]
        assert [line for line in report.unread_physics_lines if "COMPU-DENOMINATOR" in line]

    @pytest.mark.parametrize("shape", ["rules", "internal"])
    def test_a_constraint_of_several_rules_or_internal_limits(self, tmp_path: Path, shape: str) -> None:
        constr = constraint("ProbeTwice", rules=2) if shape == "rules" else constraint("ProbeTwice", internal=True)
        report, model_root = _import(
            tmp_path, package("ProbeTypes", constr + application("ProbeLevel", constr=f"{_P}/ProbeTwice"))
        )
        assert _layer(model_root, "data_constraints") == {}
        level = _layer(model_root, "application_types")["ProbeLevel"]
        assert level["range"] == {"min": 0, "max": 100}
        # Which list holds the line, per shape: an `or` of the two would pass on either.
        if shape == "rules":
            assert [line for line in report.unread_physics_lines if "declares 2 DATA-CONSTR-RULEs" in line]
        else:
            assert report.unread_physics_lines == []
            internal = [
                entry.tag
                for entry in report.skip_entries
                if entry.path == f"{_P}/ProbeTwice/DATA-CONSTR-RULES" and entry.reason.startswith("INTERNAL-CONSTRS")
            ]
            assert internal == ["LOWER-LIMIT", "UPPER-LIMIT"]

    def test_a_linear_scale_label_stays_its_section_one_line(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                unit("ProbeKmh")
                + linear("ProbeLabelled", f"{_P}/ProbeKmh", label="ProbeLabel")
                + application("ProbeSpeed", unit=f"{_P}/ProbeKmh", compu=f"{_P}/ProbeLabelled"),
            ),
        )
        assert set(_layer(model_root, "compu_methods")["ProbeLabelled"]) == {"name", "kind", "unit", "factor", "offset"}
        (entry,) = [e for e in report.skip_entries if e.path == f"{_P}/ProbeLabelled/COMPU-INTERNAL-TO-PHYS"]
        assert entry.category is SkipCategory.EXPORT_DEFECT

    def test_a_defective_table_is_not_created(self, tmp_path: Path) -> None:
        """``merge.texttable_defect``: one value twice. Both types naming it keep a line."""
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                base_type()
                + texttable("ProbeBad", ((0, "ProbeOne", ""), (0, "ProbeTwo", "")))
                + application("ProbeBadMode", compu=f"{_P}/ProbeBad")
                + value_type("ProbeBadRaw", f"{_P}/ProbeByte", compu=f"{_P}/ProbeBad"),
            ),
        )
        assert _layer(model_root, "compu_methods") == {}
        mode = _layer(model_root, "application_types")["ProbeBadMode"]
        assert (mode["kind"], mode.get("compu_method_ref"), mode.get("literals")) == ("application_type", None, None)
        raw = _layer(model_root, "implementation_types")["ProbeBadRaw"]
        assert "compu_method_ref" not in raw
        (line,) = _lines(report, f"{_P}/ProbeBadRaw/SW-DATA-DEF-PROPS", "COMPU-METHOD-REF")
        assert _F2_MARK not in line
        assert _lines(report, f"{_P}/ProbeBadMode/SW-DATA-DEF-PROPS", "COMPU-METHOD-REF")
        assert [line for line in _lines(report, f"{_P}/ProbeBad", "COMPU-METHOD") if "not used" in line]


# Sharing, and repeated names on the three tags


class TestSharingAndRepeatedNames:
    def test_a_shared_form_is_one_element_and_two_references(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                unit("ProbeKmh")
                + application("ProbeSpeedA", unit=f"{_P}/ProbeKmh")
                + application("ProbeSpeedB", unit=f"{_P}/ProbeKmh"),
            ),
        )
        assert list(_layer(model_root, "units")) == ["ProbeKmh"]
        types = _layer(model_root, "application_types")
        assert (types["ProbeSpeedA"]["unit_ref"], types["ProbeSpeedB"]["unit_ref"]) == ("ProbeKmh", "ProbeKmh")
        assert not [entry for entry in report.skip_entries if "not carried as one" in entry.reason]
        assert not [entry for entry in report.skip_entries if "SWCraft names it itself" in entry.reason]

    @staticmethod
    def _repeated(tmp_path: Path, second: str, *, type_names: tuple[str, str]) -> tuple[ImportReport, Path]:
        return _import(
            tmp_path,
            package(
                "ProbeTypes",
                children=package(
                    "ProbeA", texttable("ProbeSame") + application(type_names[0], compu="/ProbeTypes/ProbeA/ProbeSame")
                )
                + package("ProbeB", second + application(type_names[1], compu="/ProbeTypes/ProbeB/ProbeSame")),
            ),
        )

    def test_a_repeated_name_of_equal_definitions_is_one_element(self, tmp_path: Path) -> None:
        report, model_root = self._repeated(tmp_path, texttable("ProbeSame"), type_names=("ProbeModeA", "ProbeModeB"))
        assert list(_layer(model_root, "compu_methods")) == ["ProbeSame"]
        assert [str(group.key) for group in report.merged_groups] == ["compu_methods/COMPU-METHOD/ProbeSame"]
        types = _layer(model_root, "application_types")
        assert {types[name]["compu_method_ref"] for name in ("ProbeModeA", "ProbeModeB")} == {"ProbeSame"}

    def test_a_repeated_name_of_differing_definitions_imports_neither(self, tmp_path: Path) -> None:
        differing = texttable("ProbeSame", ((0, "ProbeOff", ""), (1, "ProbeOn", ""), (2, "ProbeIdle", "")))
        report, model_root = self._repeated(tmp_path, differing, type_names=("ProbeModeA", "ProbeModeB"))
        assert _layer(model_root, "compu_methods") == {}
        assert [str(group.key) for group in report.conflicting_groups] == ["compu_methods/COMPU-METHOD/ProbeSame"]
        types = _layer(model_root, "application_types")
        # Each referrer keeps the export's own path (cause `conflict`).
        assert types["ProbeModeA"]["compu_method_ref"] == "/ProbeTypes/ProbeA/ProbeSame"
        assert types["ProbeModeB"]["compu_method_ref"] == "/ProbeTypes/ProbeB/ProbeSame"
        assert Counter(record.cause for record in report.unresolved_references) == Counter({"conflict": 2})
        assert {referrer.field for record in report.unresolved_references for referrer in record.referrers} == {
            "application_types.enum.compu_method_ref"
        }

    def test_a_type_pair_naming_a_conflicting_pair_inherits_the_conflict(self, tmp_path: Path) -> None:
        differing = texttable("ProbeSame", ((0, "ProbeOff", ""), (1, "ProbeOn", ""), (2, "ProbeIdle", "")))
        report, model_root = self._repeated(tmp_path, differing, type_names=("ProbeMode", "ProbeMode"))
        groups = {str(group.key): group for group in report.conflicting_groups}
        assert set(groups) == {
            "compu_methods/COMPU-METHOD/ProbeSame",
            "application_types/APPLICATION-PRIMITIVE-DATA-TYPE/ProbeMode",
        }
        assert groups["application_types/APPLICATION-PRIMITIVE-DATA-TYPE/ProbeMode"].inherited_from is not None
        assert _layer(model_root, "application_types") == {}
        assert _layer(model_root, "compu_methods") == {}

    def test_two_differently_named_forms_of_equal_content_conflict(self, tmp_path: Path) -> None:
        """The merged element can name only one form, so the two declarations are two definitions."""
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                base_type(),
                children=package(
                    "ProbeA", unit("ProbeUnitA") + application("ProbeT", unit="/ProbeTypes/ProbeA/ProbeUnitA")
                )
                + package("ProbeB", unit("ProbeUnitB") + application("ProbeT", unit="/ProbeTypes/ProbeB/ProbeUnitB")),
            ),
        )
        (group,) = report.conflicting_groups
        assert (str(group.key), group.difference) == (
            "application_types/APPLICATION-PRIMITIVE-DATA-TYPE/ProbeT",
            "UNIT",
        )
        assert set(_layer(model_root, "units")) == {"ProbeUnitA", "ProbeUnitB"}


# SWCraft's own private form reads back inline


def _own(elements: str, children: str = "") -> str:
    """Elements in SWCraft's own application layer package, ``/DataTypes/Application``."""
    return package("DataTypes", children=package("Application", elements, children))


_APP = "/DataTypes/Application"


class TestSwcraftsOwnFormReadsBackInline:
    def test_all_four_forms_read_back_into_their_fields(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path,
            _own(
                unit("ProbeSpeed_Unit", "km/h")
                + linear("ProbeSpeed_Compu", f"{_APP}/ProbeSpeed_Unit", numerator=("1", "2"))
                + constraint("ProbeSpeed_Range", "0", "200", level=None)
                + application(
                    "ProbeSpeed",
                    unit=f"{_APP}/ProbeSpeed_Unit",
                    compu=f"{_APP}/ProbeSpeed_Compu",
                    constr=f"{_APP}/ProbeSpeed_Range",
                )
                + texttable("ProbeMode_CompuMethod")
                + application("ProbeMode", compu=f"{_APP}/ProbeMode_CompuMethod")
            ),
        )
        for layer in ("units", "compu_methods", "data_constraints"):
            assert _layer(model_root, layer) == {}, layer
        types = _layer(model_root, "application_types")
        assert types["ProbeSpeed"] == {
            "name": "ProbeSpeed",
            "kind": "application_type",
            "unit": "km/h",
            "conversion": {"factor": 2, "offset": 1},
            "range": {"min": 0, "max": 200},
        }
        assert types["ProbeMode"]["literals"] == [
            {"name": "ProbeOff", "value": 0},
            {"name": "ProbeOn", "value": 1, "description": "switched on"},
        ]
        assert "compu_method_ref" not in types["ProbeMode"]
        assert not [entry for entry in report.skip_entries if "SWCraft names it itself" in entry.reason]

    @pytest.mark.parametrize(
        ("elements", "children", "reference"),
        [
            # Another package, a sub-package of the type's own.
            ("", package("ProbeSub", unit("ProbeSpeed_Unit")), f"{_APP}/ProbeSub/ProbeSpeed_Unit"),
            # Another name.
            (unit("ProbeSpeedUnit"), "", f"{_APP}/ProbeSpeedUnit"),
            # A second referrer.
            (
                unit("ProbeSpeed_Unit") + application("ProbeOther", unit=f"{_APP}/ProbeSpeed_Unit"),
                "",
                f"{_APP}/ProbeSpeed_Unit",
            ),
            # Content the inline field cannot state: a description.
            (unit("ProbeSpeed_Unit", desc="said of the unit"), "", f"{_APP}/ProbeSpeed_Unit"),
        ],
        ids=["package", "name", "referrer", "content"],
    )
    def test_a_form_failing_one_condition_is_an_element(
        self, tmp_path: Path, elements: str, children: str, reference: str
    ) -> None:
        _, model_root = _import(tmp_path, _own(elements + application("ProbeSpeed", unit=reference), children))
        name = reference.rsplit("/", 1)[-1]
        assert list(_layer(model_root, "units")) == [name]
        speed = _layer(model_root, "application_types")["ProbeSpeed"]
        assert (speed.get("unit_ref"), speed.get("unit")) == (name, None)

    def test_an_application_types_own_named_compu_method_is_an_element(self, tmp_path: Path) -> None:
        """A ``LINEAR`` named ``{type}_CompuMethod`` is an element, and with it the unit it names.

        It is no own form: the writer names its own ``{type}_Compu``.
        """
        _, model_root = _import(
            tmp_path,
            _own(
                unit("ProbeSpeed_Unit")
                + linear("ProbeSpeed_CompuMethod", f"{_APP}/ProbeSpeed_Unit")
                + application("ProbeSpeed", unit=f"{_APP}/ProbeSpeed_Unit", compu=f"{_APP}/ProbeSpeed_CompuMethod")
            ),
        )
        assert set(_layer(model_root, "compu_methods")) == {"ProbeSpeed_CompuMethod"}
        assert set(_layer(model_root, "units")) == {"ProbeSpeed_Unit"}
        speed = _layer(model_root, "application_types")["ProbeSpeed"]
        assert (speed.get("compu_method_ref"), speed.get("unit_ref"), speed.get("conversion")) == (
            "ProbeSpeed_CompuMethod",
            "ProbeSpeed_Unit",
            None,
        )

    def test_an_inline_form_beside_an_element_of_its_name(self, tmp_path: Path) -> None:
        """The private form takes no verdict: it is no element, so the element of its name is kept whole."""
        report, model_root = _import(
            tmp_path,
            package(
                "DataTypes",
                children=package(
                    "Application", unit("ProbeSpeed_Unit") + application("ProbeSpeed", unit=f"{_APP}/ProbeSpeed_Unit")
                )
                + package("Units", unit("ProbeSpeed_Unit", "mph")),
            ),
        )
        assert _layer(model_root, "units") == {"ProbeSpeed_Unit": {"name": "ProbeSpeed_Unit", "display_name": "mph"}}
        assert _layer(model_root, "application_types")["ProbeSpeed"]["unit"] == "km/h"
        assert report.merged_groups == [] and report.conflicting_groups == []


# An implementation type's compu method


class TestAnImplementationTypesCompuMethodIsCarried:
    def test_a_value_type_and_a_reference_type_name_the_table(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                base_type()
                + texttable("ProbeModes")
                + value_type("ProbeRaw", f"{_P}/ProbeByte", compu=f"{_P}/ProbeModes")
                + reference_type("ProbeAlias", f"{_P}/ProbeRaw", compu=f"{_P}/ProbeModes"),
            ),
        )
        types = _layer(model_root, "implementation_types")
        assert (types["ProbeRaw"]["compu_method_ref"], types["ProbeAlias"]["compu_method_ref"]) == (
            "ProbeModes",
            "ProbeModes",
        )
        for name in ("ProbeRaw", "ProbeAlias"):
            assert _lines(report, f"{_P}/{name}/SW-DATA-DEF-PROPS", "COMPU-METHOD-REF") == [], name
        assert check_cross_layer_consistency(model_root=str(model_root)).consistent

    def test_a_linear_is_not_carried_and_keeps_a_line(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                base_type()
                + unit("ProbeKmh")
                + linear("ProbeScale", f"{_P}/ProbeKmh")
                + value_type("ProbeRaw", f"{_P}/ProbeByte", compu=f"{_P}/ProbeScale"),
            ),
        )
        assert "compu_method_ref" not in _layer(model_root, "implementation_types")["ProbeRaw"]
        (line,) = _lines(report, f"{_P}/ProbeRaw/SW-DATA-DEF-PROPS", "COMPU-METHOD-REF")
        assert _F2_MARK not in line


# A LINEAR scale's limits, carried on the element whatever the types converting through it


class TestALinearsScaleLimitsAreCarriedOnTheElement:
    """A ``linear`` element holds its scale's limits.

    A scale's limits are the conversion's scope (``[TPS_SWCT_01548]``), which no type's bound states,
    so they are carried on the element whatever the referrers' bounds, and no line is owed.
    """

    @staticmethod
    def _shared(tmp_path: Path, second_bound: str) -> tuple[ImportReport, Path]:
        limits = (
            '<LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT><UPPER-LIMIT INTERVAL-TYPE="OPEN">100</UPPER-LIMIT>'
        )
        return _import(
            tmp_path,
            package(
                "ProbeTypes",
                unit("ProbeKmh")
                + linear("ProbeScale", f"{_P}/ProbeKmh", limits=limits)
                + constraint("ProbeLimitsA", "0", "100")
                + constraint("ProbeLimitsB", "0", second_bound)
                + application("ProbeA", unit=f"{_P}/ProbeKmh", compu=f"{_P}/ProbeScale", constr=f"{_P}/ProbeLimitsA")
                + application("ProbeB", unit=f"{_P}/ProbeKmh", compu=f"{_P}/ProbeScale", constr=f"{_P}/ProbeLimitsB"),
            ),
        )

    @pytest.mark.parametrize("second_bound", ["100", "90"], ids=["equal-bounds", "another-bound"])
    def test_the_limits_are_carried_whatever_the_referrers_bounds(self, tmp_path: Path, second_bound: str) -> None:
        report, model_root = self._shared(tmp_path, second_bound)
        assert not [entry for entry in report.skip_entries if "COMPU-SCALE" in entry.reason]
        method = _layer(model_root, "compu_methods")["ProbeScale"]
        assert method["limits"] == {
            "lower": {"value": 0, "interval": "closed"},
            "upper": {"value": 100, "interval": "open"},
        }


# A --packages selection keeps the elements its types name

#: The fixture tree stating every element kind and every reference field.
_ELEMENTS_TREE = Path(__file__).parent / "fixtures" / "data_description" / "model"


class TestASelectionKeepsTheElementsItsTypesName:
    """A conflict-free selection keeps every element its kept types name, and still generates and validates.

    A form takes no part in the selection's declaration walk; an element no kept type names is
    excluded with the closure's line.
    """

    @staticmethod
    def _selected(tmp_path: Path) -> tuple[ImportReport, Path, str]:
        import shutil

        from lxml import etree
        from swcraft_tools.writer import generate_arxml

        copy = tmp_path / "tree"
        shutil.copytree(_ELEMENTS_TREE, copy)
        source = tmp_path / "build.arxml"
        generate_arxml(str(copy), str(source), version="4.3.0")
        namespace = "{http://autosar.org/schema/r4.0}"
        leaves = {
            package.findtext(f"{namespace}SHORT-NAME")
            for package in etree.parse(str(source)).iter(f"{namespace}AR-PACKAGE")
            if package.find(f"{namespace}ELEMENTS/{namespace}APPLICATION-SW-COMPONENT-TYPE") is not None
        }
        (leaf,) = leaves
        project = _init_project(tmp_path)
        report = run_import(str(source), str(project / "swcraft.yaml"), packages=[leaf])
        return report, project / "model", leaf

    def test_the_selection_generates_and_validates(self, tmp_path: Path) -> None:
        from swcraft_tools.arxml_validation import check_arxml
        from swcraft_tools.writer import generate_arxml

        report, model_root, _leaf = self._selected(tmp_path)

        assert report.conflicting_groups == [] and report.unresolved_references == []
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues
        for version in ("4.2.2", "4.3.0"):
            out = tmp_path / f"selected-{version}.arxml"
            generate_arxml(str(model_root), str(out), version=version)
            outcome = check_arxml(str(out))
            assert outcome.valid, outcome.messages

    def test_the_elements_the_kept_types_name_are_kept_and_no_other(self, tmp_path: Path) -> None:
        report, model_root, _leaf = self._selected(tmp_path)

        kept = {layer: set(_layer(model_root, layer)) for layer in ("units", "compu_methods", "data_constraints")}
        named: set[tuple[str, str]] = set()
        for document in [
            *_layer(model_root, "application_types").values(),
            *_layer(model_root, "implementation_types").values(),
        ]:
            for field_name, layer in (
                ("unit_ref", "units"),
                ("compu_method_ref", "compu_methods"),
                ("data_constr_ref", "data_constraints"),
            ):
                if document.get(field_name):
                    named.add((layer, document[field_name]))
        for document in _layer(model_root, "compu_methods").values():
            if document["kind"] == "linear":
                named.add(("units", document["unit"]))
        assert {(layer, name) for layer, names in kept.items() for name in names} == named
        declared = {
            (layer, path.stem)
            for layer, directory in (
                ("units", "units"),
                ("compu_methods", "compu_methods"),
                ("data_constraints", "constraints"),
            )
            for path in (_ELEMENTS_TREE / "data_types" / directory).glob("*.yaml")
        }
        dropped = declared - {(layer, name) for layer, names in kept.items() for name in names}
        # The fixture tree states elements the component's types do not name, so the selection drops some.
        assert dropped
        (excluded,) = [line for line in report.closure_lines if "unit, compu method or data constraint" in line]
        assert excluded == (
            f"{len(dropped)} unit, compu method or data constraint element(s) excluded (no data type the selection "
            "kept names them): " + ", ".join(sorted(name for _layer_name, name in dropped))
        )


# What an element does not hold is named

_TYPE_PROPS = f"{_P}/ProbeMode/SW-DATA-DEF-PROPS"


class TestAnEnumerationsUnitAndRangeAreNamed:
    """An enumeration's ``UNIT-REF`` and ``DATA-CONSTR-REF`` are lines, its unit and range elements of their own.

    An enumeration holds no unit and no range: the two references it states are one line each at the type, and the
    ``UNIT`` and the ``DATA-CONSTR`` are still elements of their own.
    """

    def test_an_enumeration_naming_its_table_element(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                texttable("ProbeTable")
                + unit("ProbeKmh")
                + constraint("ProbeLimits", "0", "1")
                + application("ProbeMode", compu=f"{_P}/ProbeTable", unit=f"{_P}/ProbeKmh", constr=f"{_P}/ProbeLimits"),
            ),
        )
        assert _layer(model_root, "application_types")["ProbeMode"] == {
            "name": "ProbeMode",
            "kind": "enum",
            "compu_method_ref": "ProbeTable",
        }
        assert set(_layer(model_root, "units")) == {"ProbeKmh"}
        assert set(_layer(model_root, "data_constraints")) == {"ProbeLimits"}
        lines = {
            (entry.tag, entry.category): entry.reason for entry in report.skip_entries if entry.path == _TYPE_PROPS
        }
        assert set(lines) == {
            ("UNIT-REF", SkipCategory.NOT_SUPPORTED),
            ("DATA-CONSTR-REF", SkipCategory.NOT_SUPPORTED),
        }
        assert "an enum holds no unit" in lines["UNIT-REF", SkipCategory.NOT_SUPPORTED]
        assert "an enum holds no range" in lines["DATA-CONSTR-REF", SkipCategory.NOT_SUPPORTED]

    def test_an_enumeration_reading_its_own_table_inline(self, tmp_path: Path) -> None:
        """SWCraft's own ``{enum}_CompuMethod`` is read as ``literals``; the references are named alike."""
        report, model_root = _import(
            tmp_path,
            _own(
                texttable("ProbeMode_CompuMethod")
                + unit("ProbeKmh")
                + application("ProbeMode", compu=f"{_APP}/ProbeMode_CompuMethod", unit=f"{_APP}/ProbeKmh")
            ),
        )
        mode = _layer(model_root, "application_types")["ProbeMode"]
        assert [literal["name"] for literal in mode["literals"]] == ["ProbeOff", "ProbeOn"]
        assert _layer(model_root, "compu_methods") == {}
        assert [entry.tag for entry in report.skip_entries if entry.path == f"{_APP}/ProbeMode/SW-DATA-DEF-PROPS"] == [
            "UNIT-REF"
        ]

    def test_an_enumeration_stating_neither_has_no_line(self, tmp_path: Path) -> None:
        report, _ = _import(
            tmp_path,
            package("ProbeTypes", texttable("ProbeTable") + application("ProbeMode", compu=f"{_P}/ProbeTable")),
        )
        assert [entry for entry in report.skip_entries if entry.path == _TYPE_PROPS] == []

    @pytest.mark.parametrize("table", ["ProbeMode_CompuMethod", "ProbeTable"], ids=["own-table", "table-element"])
    def test_an_enumerations_own_named_unit_and_range_are_elements(self, tmp_path: Path, table: str) -> None:
        """An enumeration's ``{enum}_Unit`` and ``{enum}_Range`` are elements; only the two references are lines.

        The writer generates no unit or range form for an enumeration, so these are another author's forms.
        """
        own = f"{_APP}/ProbeMode/SW-DATA-DEF-PROPS"
        report, model_root = _import(
            tmp_path,
            _own(
                texttable(table)
                + unit("ProbeMode_Unit")
                + constraint("ProbeMode_Range", "0", "5")
                + application(
                    "ProbeMode",
                    compu=f"{_APP}/{table}",
                    unit=f"{_APP}/ProbeMode_Unit",
                    constr=f"{_APP}/ProbeMode_Range",
                )
            ),
        )
        assert _layer(model_root, "units") == {"ProbeMode_Unit": {"name": "ProbeMode_Unit", "display_name": "km/h"}}
        constr = _layer(model_root, "data_constraints")["ProbeMode_Range"]
        assert (constr["min"], constr["max"]) == (0, 5)
        tables = 0 if table == "ProbeMode_CompuMethod" else 1
        assert (report.imported_units, report.imported_compu_methods, report.imported_data_constraints) == (
            1,
            tables,
            1,
        )
        assert sorted((entry.path, entry.tag) for entry in report.skip_entries if entry.path.startswith(_APP)) == [
            (own, "DATA-CONSTR-REF"),
            (own, "UNIT-REF"),
        ]

    def test_a_type_naming_a_table_it_cannot_hold_names_its_own_named_unit_as_an_element(self, tmp_path: Path) -> None:
        """A type naming a defective table names its ``{type}_Unit`` as the element.

        No type the writer writes names a ``TEXTTABLE`` and states a unit.
        """
        _, model_root = _import(
            tmp_path,
            _own(
                texttable("ProbeMode_CompuMethod", ((0, "ProbeOff", ""), (0, "ProbeOn", "")))
                + unit("ProbeMode_Unit")
                + application("ProbeMode", compu=f"{_APP}/ProbeMode_CompuMethod", unit=f"{_APP}/ProbeMode_Unit")
            ),
        )
        mode = _layer(model_root, "application_types")["ProbeMode"]
        assert (mode["kind"], mode.get("unit_ref"), mode.get("unit")) == ("application_type", "ProbeMode_Unit", None)
        assert set(_layer(model_root, "units")) == {"ProbeMode_Unit"}

    @pytest.mark.parametrize("prefix", ["Gear_T", "ProbeOther"], ids=["own-named", "other-named"])
    def test_import_component_refuses_on_the_two_references_alone(self, tmp_path: Path, prefix: str) -> None:
        """``import component`` aborts on the enumeration's two reference lines alone, whatever the forms are named."""
        from swcraft_tools.component_exchange import LossyComponentImportError, export_component, import_component

        model_root = tmp_path / "source" / "model"
        shutil.copytree(Path(__file__).resolve().parent / "fixtures" / "component_exchange" / "model", model_root)
        arxml = tmp_path / "SensorSwc.arxml"
        export_component(str(model_root), "SensorSwc", str(arxml))
        tree = etree.parse(str(arxml))
        (gear,) = [
            element
            for element in tree.iter(f"{_NS}APPLICATION-PRIMITIVE-DATA-TYPE")
            if element.findtext(f"{_NS}SHORT-NAME") == "Gear_T"
        ]
        compu_ref = next(gear.iter(f"{_NS}COMPU-METHOD-REF"))
        home = compu_ref.text.rpartition("/")[0]
        for tag, dest, name in (
            ("UNIT-REF", "UNIT", f"{prefix}_Unit"),
            ("DATA-CONSTR-REF", "DATA-CONSTR", f"{prefix}_Range"),
        ):
            reference = etree.Element(f"{_NS}{tag}", DEST=dest)
            reference.text = f"{home}/{name}"
            compu_ref.addnext(reference)
        declared = unit(f"{prefix}_Unit") + constraint(f"{prefix}_Range", "0", "5")
        forms = etree.fromstring(f'<ELEMENTS xmlns="{_NS[1:-1]}">{declared}</ELEMENTS>')
        gear.getparent().extend(list(forms))
        tree.write(str(arxml), xml_declaration=True, encoding="UTF-8")
        target = tmp_path / "target" / "model"
        target.mkdir(parents=True)

        with pytest.raises(LossyComponentImportError) as refused:
            import_component(str(arxml), str(target))

        assert len(refused.value.skip_lines) == 2, refused.value.skip_lines
        assert all("imported as an enum not carried" in line for line in refused.value.skip_lines)


class TestATexttablesOwnUnitIsNamed:
    """A ``texttable`` holds entries and no unit: a ``TEXTTABLE``'s own ``UNIT-REF`` is a line at the method."""

    _UNIT_REF = f'<UNIT-REF DEST="UNIT">{_P}/ProbeKmh</UNIT-REF>'

    def test_the_table_is_an_element_and_its_unit_reference_a_line(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                unit("ProbeKmh")
                + texttable("ProbeTable", extra=self._UNIT_REF)
                + application("ProbeMode", compu=f"{_P}/ProbeTable"),
            ),
        )
        assert set(_layer(model_root, "compu_methods")["ProbeTable"]) == {"name", "kind", "entries"}
        (entry,) = [entry for entry in report.skip_entries if entry.path == f"{_P}/ProbeTable"]
        assert (entry.tag, entry.category) == ("UNIT-REF", SkipCategory.NOT_SUPPORTED)
        assert "a texttable element holds no unit" in entry.reason

    def test_swcrafts_own_table_stating_a_unit_is_no_longer_its_own_form(self, tmp_path: Path) -> None:
        """``_texttable_inline`` asks for no ``UNIT-REF``: the generated form states none."""
        unit_ref = f'<UNIT-REF DEST="UNIT">{_APP}/ProbeKmh</UNIT-REF>'
        report, model_root = _import(
            tmp_path,
            _own(
                unit("ProbeKmh")
                + texttable("ProbeMode_CompuMethod", extra=unit_ref)
                + application("ProbeMode", compu=f"{_APP}/ProbeMode_CompuMethod")
            ),
        )
        assert _layer(model_root, "application_types")["ProbeMode"]["compu_method_ref"] == "ProbeMode_CompuMethod"
        assert [entry.tag for entry in report.skip_entries if entry.path == f"{_APP}/ProbeMode_CompuMethod"] == [
            "UNIT-REF"
        ]


class TestAUnitsSiRelationTakesPartInItsVerdict:
    """Two declarations of one unit stating different SI values conflict.

    The merge compares a unit's SI texts, not only which tags it states.
    """

    @staticmethod
    def _pair(tmp_path: Path, first: str, second: str) -> ImportReport:
        report, _ = _import(
            tmp_path,
            package("ProbeA", unit("ProbeKmh", extra=first)),
            package("ProbeB", unit("ProbeKmh", extra=second) + application("ProbeSpeed", unit="/ProbeB/ProbeKmh")),
        )
        return report

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("<FACTOR-SI-TO-UNIT>1</FACTOR-SI-TO-UNIT>", "<FACTOR-SI-TO-UNIT>3.6</FACTOR-SI-TO-UNIT>"),
            ("<OFFSET-SI-TO-UNIT>0</OFFSET-SI-TO-UNIT>", "<OFFSET-SI-TO-UNIT>273.15</OFFSET-SI-TO-UNIT>"),
            (
                '<PHYSICAL-DIMENSION-REF DEST="PHYSICAL-DIMENSION">/ProbeDims/ProbeSpeed</PHYSICAL-DIMENSION-REF>',
                '<PHYSICAL-DIMENSION-REF DEST="PHYSICAL-DIMENSION">/ProbeDims/ProbeLength</PHYSICAL-DIMENSION-REF>',
            ),
        ],
        ids=["factor", "offset", "dimension"],
    )
    def test_differing_values_conflict(self, tmp_path: Path, first: str, second: str) -> None:
        report = self._pair(tmp_path, first, second)
        assert [str(group.key) for group in report.conflicting_groups] == ["units/UNIT/ProbeKmh"]
        assert report.merged_groups == []

    def test_equal_values_merge(self, tmp_path: Path) -> None:
        relation = "<FACTOR-SI-TO-UNIT>3.6</FACTOR-SI-TO-UNIT>"
        report = self._pair(tmp_path, relation, relation)
        assert [str(group.key) for group in report.merged_groups] == ["units/UNIT/ProbeKmh"]
        assert report.conflicting_groups == []


class TestTheElementsAreCountedAsImported:
    def test_a_file_of_forms_alone_imports_them(self, tmp_path: Path) -> None:
        """A file of forms alone imports them: the three element counts join ``total_imported`` and the summary."""
        report, model_root = _import(
            tmp_path, package("ProbeTypes", unit("ProbeKmh") + texttable("ProbeTable") + constraint("ProbeLimits"))
        )
        assert (report.imported_units, report.imported_compu_methods, report.imported_data_constraints) == (1, 1, 1)
        assert report.total_imported == 3
        rendered = report.render().splitlines()
        assert {"IMPORTED 1 units", "IMPORTED 1 compu methods", "IMPORTED 1 data constraints"} <= set(rendered)
        assert [set(_layer(model_root, layer)) for layer in ("units", "compu_methods", "data_constraints")] == [
            {"ProbeKmh"},
            {"ProbeTable"},
            {"ProbeLimits"},
        ]

    def test_a_file_of_no_form_renders_no_element_line(self, tmp_path: Path) -> None:
        report, _ = _import(tmp_path, package("ProbeTypes", base_type()))
        assert (report.imported_units, report.imported_compu_methods, report.imported_data_constraints) == (0, 0, 0)
        assert not [line for line in report.render().splitlines() if line.startswith("IMPORTED") and "units" in line]


class TestAConstraintLevelLine:
    def test_a_merged_members_level_is_carried_by_the_primary(self, tmp_path: Path) -> None:
        """The merge compares the level: the alias's is the primary's, carried by the element; no ``_F9_MARK`` line."""
        report, model_root = _import(
            tmp_path,
            package("ProbeA", constraint("ProbeLimits", level="0")),
            package(
                "ProbeB", constraint("ProbeLimits", level="0") + application("ProbeLevel", constr="/ProbeB/ProbeLimits")
            ),
        )
        assert [str(group.key) for group in report.merged_groups] == ["data_constraints/DATA-CONSTR/ProbeLimits"]
        assert _layer(model_root, "data_constraints")["ProbeLimits"]["level"] == 0
        assert [entry for entry in report.skip_entries if _F9_MARK in entry.reason] == []

    def test_a_level_not_carried_says_what_holds_one(self, tmp_path: Path) -> None:
        report, _ = _import(
            tmp_path,
            package(
                "ProbeTypes", constraint("ProbeTwice", rules=2) + application("ProbeLevel", constr=f"{_P}/ProbeTwice")
            ),
        )
        reasons = {entry.reason for entry in report.skip_entries if _F9_MARK in entry.reason}
        assert reasons == {
            "CONSTR-LEVEL of a DATA-CONSTR-RULE not carried: a level is held by a data_constr element alone, as an "
            "integer, and this rule's is not imported as one"
        }


class TestAFormReadInlineStillComparesItsText:
    """The ``DESC`` comparison stays for a form a type reads inline; an element carries its own ``DESC``.

    Such a form is a unit stating an SI relation, which no element holds.
    """

    _RELATION = "<FACTOR-SI-TO-UNIT>1</FACTOR-SI-TO-UNIT>"

    @pytest.mark.parametrize(
        ("type_text", "expected"),
        [
            ("The type's words.", "DESC not carried: it differs from the type's description, which is carried"),
            ("", "DESC not carried: the type that consumes it carries no description"),
        ],
        ids=["differs", "undescribed-type"],
    )
    def test_a_differing_text_is_one_line(self, tmp_path: Path, type_text: str, expected: str) -> None:
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                unit("ProbeMetre", "m", extra=self._RELATION, desc="The unit's own words.")
                + application("ProbeDistance", unit=f"{_P}/ProbeMetre", desc=type_text),
            ),
        )
        assert _layer(model_root, "units") == {}
        assert [
            entry.reason for entry in report.skip_entries if entry.path == f"{_P}/ProbeMetre" and entry.tag == "DESC"
        ] == [expected]


class TestAScaleLabelDifferingFromItsText:
    def test_the_entry_is_named_by_the_text_and_the_label_keeps_its_line(self, tmp_path: Path) -> None:
        scale = (
            "<COMPU-SCALE><SHORT-LABEL>ProbeLabel</SHORT-LABEL>"
            '<LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT><UPPER-LIMIT INTERVAL-TYPE="CLOSED">0</UPPER-LIMIT>'
            "<COMPU-CONST><VT>ProbeText</VT></COMPU-CONST></COMPU-SCALE>"
        )
        table = (
            "<COMPU-METHOD><SHORT-NAME>ProbeTable</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY>"
            f"<COMPU-INTERNAL-TO-PHYS><COMPU-SCALES>{scale}</COMPU-SCALES></COMPU-INTERNAL-TO-PHYS></COMPU-METHOD>"
        )
        report, model_root = _import(
            tmp_path, package("ProbeTypes", table + application("ProbeMode", compu=f"{_P}/ProbeTable"))
        )
        assert [entry["name"] for entry in _layer(model_root, "compu_methods")["ProbeTable"]["entries"]] == [
            "ProbeText"
        ]
        (entry,) = [entry for entry in report.skip_entries if entry.path == f"{_P}/ProbeTable/COMPU-INTERNAL-TO-PHYS"]
        assert (entry.tag, entry.category) == ("SHORT-LABEL", SkipCategory.NOT_SUPPORTED)
        assert (
            entry.reason
            == "SHORT-LABEL of a TEXTTABLE COMPU-SCALE not carried: SWCraft writes the literal as the label"
        )


class TestTheElementOrInlineDecisionsBoundaries:
    """``_constr_holds_whole``'s conditions beyond the rule count and the internal limits.

    And a table scale's limits on the element path.
    """

    @staticmethod
    def _constrained(tmp_path: Path, limits: str) -> tuple[ImportReport, Path]:
        constr = (
            "<DATA-CONSTR><SHORT-NAME>ProbeLimits</SHORT-NAME><DATA-CONSTR-RULES><DATA-CONSTR-RULE>"
            f"<PHYS-CONSTRS>{limits}</PHYS-CONSTRS></DATA-CONSTR-RULE></DATA-CONSTR-RULES></DATA-CONSTR>"
        )
        return _import(tmp_path, package("ProbeTypes", constr + application("ProbeLevel", constr=f"{_P}/ProbeLimits")))

    @pytest.mark.parametrize(
        ("limits", "range_", "note"),
        [
            (
                "<LOWER-LIMIT>0xZZ</LOWER-LIMIT><UPPER-LIMIT>100</UPPER-LIMIT>",
                {"max": 100},
                "LOWER-LIMIT this release cannot read as a number",
            ),
            (
                "<LOWER-LIMIT>0</LOWER-LIMIT><UPPER-LIMIT>16777217.000000001</UPPER-LIMIT>",
                {"min": 0},
                "UPPER-LIMIT no model value holds exactly",
            ),
            ("<LOWER-LIMIT>5</LOWER-LIMIT><UPPER-LIMIT>1</UPPER-LIMIT>", None, "lower limit above its upper one"),
        ],
        ids=["unreadable", "inexact", "lower-above-upper"],
    )
    def test_a_constraint_no_element_holds_whole_is_read_inline_with_its_note(
        self, tmp_path: Path, limits: str, range_: dict[str, int] | None, note: str
    ) -> None:
        report, model_root = self._constrained(tmp_path, limits)
        assert _layer(model_root, "data_constraints") == {}
        level = _layer(model_root, "application_types")["ProbeLevel"]
        assert level.get("range") == range_
        assert "data_constr_ref" not in level
        assert [line for line in report.unread_physics_lines if note in line]

    def test_an_infinite_limit_is_no_bound_on_the_element(self, tmp_path: Path) -> None:
        report, model_root = self._constrained(
            tmp_path, "<LOWER-LIMIT>-INF</LOWER-LIMIT><UPPER-LIMIT>100</UPPER-LIMIT>"
        )
        assert _layer(model_root, "data_constraints") == {"ProbeLimits": {"name": "ProbeLimits", "max": 100}}
        assert _layer(model_root, "application_types")["ProbeLevel"]["data_constr_ref"] == "ProbeLimits"
        assert not [entry for entry in report.skip_entries if entry.path.startswith(f"{_P}/ProbeLimits")]

    def test_an_open_limit_is_held_closed_as_on_the_inline_path(self, tmp_path: Path) -> None:
        """A limit's ``INTERVAL-TYPE`` is not carried, inline or on the element; no line says so per constraint."""
        report, model_root = self._constrained(
            tmp_path, '<LOWER-LIMIT INTERVAL-TYPE="OPEN">0</LOWER-LIMIT><UPPER-LIMIT>100</UPPER-LIMIT>'
        )
        assert _layer(model_root, "data_constraints") == {"ProbeLimits": {"name": "ProbeLimits", "min": 0, "max": 100}}
        assert not [entry for entry in report.skip_entries if entry.path.startswith(f"{_P}/ProbeLimits")]

    def test_a_table_scale_of_a_range_is_a_limit_line_on_the_element_path(self, tmp_path: Path) -> None:
        scale = (
            "<COMPU-SCALE><SHORT-LABEL>ProbeLow</SHORT-LABEL>"
            '<LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT><UPPER-LIMIT INTERVAL-TYPE="CLOSED">9</UPPER-LIMIT>'
            "<COMPU-CONST><VT>ProbeLow</VT></COMPU-CONST></COMPU-SCALE>"
        )
        table = (
            "<COMPU-METHOD><SHORT-NAME>ProbeTable</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY>"
            f"<COMPU-INTERNAL-TO-PHYS><COMPU-SCALES>{scale}</COMPU-SCALES></COMPU-INTERNAL-TO-PHYS></COMPU-METHOD>"
        )
        report, model_root = _import(
            tmp_path, package("ProbeTypes", table + application("ProbeMode", compu=f"{_P}/ProbeTable"))
        )
        assert _layer(model_root, "compu_methods")["ProbeTable"]["entries"] == [{"name": "ProbeLow", "value": 0}]
        (entry,) = [entry for entry in report.skip_entries if entry.path == f"{_P}/ProbeTable/COMPU-INTERNAL-TO-PHYS"]
        assert (entry.tag, entry.category) == ("UPPER-LIMIT", SkipCategory.NOT_SUPPORTED)


def _own_form_case(form: str, condition: str) -> tuple[str, str, str, str]:
    """Build the case of SWCraft's own ``form`` failing *condition*.

    Returns:
        ``(elements, children, the type's declaration, the form's emitted name)``.
    """
    name = {"compu": "ProbeSpeed_Compu", "range": "ProbeSpeed_Range", "table": "ProbeMode_CompuMethod"}[form]
    if condition == "name":
        name = name.replace("_", "")
    content = condition == "content"
    owner, attribute, base = {
        "compu": ("ProbeSpeed", "compu", unit("ProbeKmh")),
        "range": ("ProbeSpeed", "constr", ""),
        "table": ("ProbeMode", "compu", ""),
    }[form]
    if form == "compu":
        built = linear(name, f"{_APP}/ProbeKmh", desc="said of it" if content else "")
    elif form == "range":
        built = constraint(name, level="0" if content else None)
    else:
        built = texttable(name, desc="said of it" if content else "")

    def referrer(type_name: str, reference: str) -> str:
        units = {"unit": f"{_APP}/ProbeKmh"} if form == "compu" else {}
        return application(type_name, **{attribute: reference}, **units)

    if condition == "package":
        return base, package("ProbeSub", built), referrer(owner, f"{_APP}/ProbeSub/{name}"), name
    second = referrer("ProbeOther", f"{_APP}/{name}") if condition == "referrer" else ""
    return base + built + second, "", referrer(owner, f"{_APP}/{name}"), name


class TestEveryOwnFormFailingOneConditionIsAnElement:
    @pytest.mark.parametrize("condition", ["package", "name", "referrer", "content"])
    @pytest.mark.parametrize(
        ("form", "layer"), [("compu", "compu_methods"), ("range", "data_constraints"), ("table", "compu_methods")]
    )
    def test_the_form_is_an_element_and_its_type_names_it(
        self, tmp_path: Path, form: str, layer: str, condition: str
    ) -> None:
        elements, children, declaration, name = _own_form_case(form, condition)
        _, model_root = _import(tmp_path, _own(elements + declaration, children))
        assert name in _layer(model_root, layer)
        owner = _layer(model_root, "application_types")["ProbeMode" if form == "table" else "ProbeSpeed"]
        field_name = "data_constr_ref" if form == "range" else "compu_method_ref"
        inline_field = {"compu": "conversion", "range": "range", "table": "literals"}[form]
        assert (owner.get(field_name), owner.get(inline_field)) == (name, None)


class TestTwoDifferentlyNamedFormsOfEqualContentConflictForEveryTag:
    @pytest.mark.parametrize(
        ("tag", "first", "second", "reference"),
        [
            ("COMPU-METHOD", texttable("ProbeTableA"), texttable("ProbeTableB"), "compu"),
            ("DATA-CONSTR", constraint("ProbeLimitsA"), constraint("ProbeLimitsB"), "constr"),
        ],
        ids=["COMPU-METHOD", "DATA-CONSTR"],
    )
    def test_the_type_pair_conflicts_on_the_reference(
        self, tmp_path: Path, tag: str, first: str, second: str, reference: str
    ) -> None:
        """The ``UNIT`` case is ``TestSharingAndRepeatedNames``'; the merged element can name only one form."""
        first_name = "ProbeTableA" if tag == "COMPU-METHOD" else "ProbeLimitsA"
        second_name = first_name[:-1] + "B"
        report, _ = _import(
            tmp_path,
            package(
                "ProbeTypes",
                children=package(
                    "ProbeA", first + application("ProbeT", **{reference: f"/ProbeTypes/ProbeA/{first_name}"})
                )
                + package("ProbeB", second + application("ProbeT", **{reference: f"/ProbeTypes/ProbeB/{second_name}"})),
            ),
        )
        (group,) = report.conflicting_groups
        assert (str(group.key), group.difference) == ("application_types/APPLICATION-PRIMITIVE-DATA-TYPE/ProbeT", tag)


class TestARangeStatingNoLimitRoundTrips:
    def test_the_writers_own_range_reads_back_as_the_empty_range(self, tmp_path: Path) -> None:
        """The writer's ``{type}_Range`` with an empty ``PHYS-CONSTRS`` reads back as ``range: {}``, not no range."""
        from swcraft_tools.writer import generate_arxml

        source_root = _init_project(tmp_path / "source") / "model"
        (source_root / "data_types" / "application" / "ProbeRanged.yaml").write_text(
            "name: ProbeRanged\nkind: application_type\nrange: {}\n", encoding="utf-8", newline="\n"
        )
        built = tmp_path / "ranged.arxml"
        generate_arxml(str(source_root), str(built), version="4.3.0")
        project = _init_project(tmp_path / "target")

        report = run_import(str(built), str(project / "swcraft.yaml"))

        ranged = _layer(project / "model", "application_types")["ProbeRanged"]
        assert ranged == {"name": "ProbeRanged", "kind": "application_type", "range": {}}
        assert _layer(project / "model", "data_constraints") == {}
        assert [line for line in report.unread_physics_lines if "ProbeRanged" in line] == []


class TestTheFixtureTreesBuildReadsBackWhole:
    """``import model`` of SWCraft's own build of the elements fixture tree reads every element back equal.

    Every unit, compu method and data constraint comes back equal to its source, ``package`` included (the selection
    tests compare names).
    """

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_every_element_equals_its_source(self, tmp_path: Path, version: str) -> None:
        import shutil

        from swcraft_tools.data_description import read_data_description
        from swcraft_tools.validator import data_type_layer
        from swcraft_tools.writer import generate_arxml

        copy = tmp_path / "tree"
        shutil.copytree(_ELEMENTS_TREE, copy)
        source = tmp_path / "build.arxml"
        generate_arxml(str(copy), str(source), version=version)
        project = _init_project(tmp_path)

        report = run_import(str(source), str(project / "swcraft.yaml"))

        def elements(model_root: Path) -> dict[tuple[str, str], Any]:
            return {
                (data_type_layer(element), element.name): element
                for element in read_data_description(str(model_root)).elements
            }

        expected, imported = elements(copy), elements(project / "model")
        assert set(imported) == set(expected)
        for key, element in expected.items():
            assert imported[key] == element, key
        assert {key: element.package for key, element in imported.items() if element.package} == {
            key: element.package for key, element in expected.items() if element.package
        }
        assert (report.imported_units, report.imported_compu_methods, report.imported_data_constraints) == (
            4,
            2,
            3,
        )


# An infinite physical limit's line


def _limits_constraint(name: str, lower: str, upper: str) -> str:
    """A ``DATA-CONSTR`` of one rule whose two ``PHYS-CONSTRS`` limits are given whole."""
    return (
        f"<DATA-CONSTR><SHORT-NAME>{name}</SHORT-NAME><DATA-CONSTR-RULES><DATA-CONSTR-RULE>"
        f"<PHYS-CONSTRS>{lower}{upper}</PHYS-CONSTRS></DATA-CONSTR-RULE></DATA-CONSTR-RULES></DATA-CONSTR>"
    )


_REPRESENTED = "infinite physical limit represented as an absent bound"
_REPRESENTED_HEADING = "Represented in another form (the value is carried, spelled as the model spells it):"


class TestAnInfinitePhysicalLimitIsReportedAsRepresented:
    """``import model`` carries an own-side infinite ``PHYS-CONSTRS`` limit as the absent bound and says so.

    One line per infinite side, under *Represented in another form* -- never a skip entry or a
    *Physical properties not imported* note -- for an application type's inline range keyed by the
    type, and for a standalone ``data_constr`` element keyed by the constraint.
    """

    @pytest.mark.parametrize(
        ("lower", "upper", "sides", "expected_range"),
        [
            pytest.param(
                "<LOWER-LIMIT>-INF</LOWER-LIMIT>",
                '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">200</UPPER-LIMIT>',
                ["LOWER-LIMIT"],
                {"max": 200},
                id="lower-minus-inf",
            ),
            pytest.param(
                "<LOWER-LIMIT>0</LOWER-LIMIT>",
                "<UPPER-LIMIT>+INF</UPPER-LIMIT>",
                ["UPPER-LIMIT"],
                {"min": 0},
                id="upper-plus-inf",
            ),
            pytest.param(
                '<LOWER-LIMIT INTERVAL-TYPE="INFINITE">-5</LOWER-LIMIT>',
                "<UPPER-LIMIT>INF</UPPER-LIMIT>",
                ["LOWER-LIMIT", "UPPER-LIMIT"],
                {},
                id="both-sides-one-infinite-with-a-number",
            ),
        ],
    )
    def test_an_inline_range(
        self, tmp_path: Path, lower: str, upper: str, sides: list[str], expected_range: dict[str, int]
    ) -> None:
        report, model_root = _import(
            tmp_path,
            _own(
                _limits_constraint("ProbeSpeed_Range", lower, upper)
                + application("ProbeSpeed", constr=f"{_APP}/ProbeSpeed_Range")
            ),
        )

        assert _layer(model_root, "application_types")["ProbeSpeed"]["range"] == expected_range
        assert report.represented_lines == [
            f"{_APP}/ProbeSpeed: DATA-CONSTR {_APP}/ProbeSpeed_Range PHYS-CONSTRS {side}: {_REPRESENTED}"
            for side in sides
        ]
        assert report.unread_physics_lines == []
        assert [entry for entry in report.skip_entries if "ProbeSpeed" in entry.path] == []
        rendered = report.render()
        assert rendered.count(_REPRESENTED_HEADING) == 1
        block = rendered.split(_REPRESENTED_HEADING, 1)[1]
        for line in report.represented_lines:
            assert f"  - {line}" in block

    def test_a_standalone_constraint(self, tmp_path: Path) -> None:
        report, model_root = _import(
            tmp_path,
            package(
                "ProbeTypes",
                _limits_constraint(
                    "ProbeLimits",
                    '<LOWER-LIMIT INTERVAL-TYPE="INFINITE">3</LOWER-LIMIT>',
                    "<UPPER-LIMIT>250</UPPER-LIMIT>",
                )
                + application("ProbeLevel", constr=f"{_P}/ProbeLimits"),
            ),
        )

        assert _layer(model_root, "data_constraints") == {"ProbeLimits": {"name": "ProbeLimits", "max": 250}}
        assert report.represented_lines == [f"{_P}/ProbeLimits: PHYS-CONSTRS LOWER-LIMIT: {_REPRESENTED}"]
        assert report.unread_physics_lines == []
        assert _REPRESENTED_HEADING in report.render()

    def test_no_infinite_limit_renders_no_block(self, tmp_path: Path) -> None:
        report, _ = _import(
            tmp_path,
            package("ProbeTypes", constraint("ProbeLimits") + application("ProbeLevel", constr=f"{_P}/ProbeLimits")),
        )

        assert report.represented_lines == []
        assert "Represented in another form" not in report.render()

    @pytest.mark.parametrize(
        "lower",
        [
            pytest.param("<LOWER-LIMIT>NaN</LOWER-LIMIT>", id="nan"),
            pytest.param('<LOWER-LIMIT INTERVAL-TYPE="INFINITE">NaN</LOWER-LIMIT>', id="nan-under-infinite"),
            pytest.param("<LOWER-LIMIT>INF</LOWER-LIMIT>", id="opposite-side-infinity"),
        ],
    )
    def test_nan_and_an_opposite_side_infinity_keep_their_line(self, tmp_path: Path, lower: str) -> None:
        report, _ = _import(
            tmp_path,
            _own(
                _limits_constraint("ProbeSpeed_Range", lower, "<UPPER-LIMIT>200</UPPER-LIMIT>")
                + application("ProbeSpeed", constr=f"{_APP}/ProbeSpeed_Range")
            ),
        )

        assert report.represented_lines == []
        assert [
            note for note in report.unread_physics_lines if "LOWER-LIMIT this release cannot read as a number" in note
        ] != []

    @staticmethod
    def _selected(tmp_path: Path, speed_type: str) -> ImportReport:
        """Import a model with infinite limits, selecting ``/Swcs``.

        The model holds an inline range and a standalone constraint, each with an infinite limit, and a component
        package ``/Swcs`` whose interface reaches *speed_type* alone.
        """
        interface = (
            "<SENDER-RECEIVER-INTERFACE><SHORT-NAME>IProbe</SHORT-NAME><IS-SERVICE>false</IS-SERVICE><DATA-ELEMENTS>"
            "<VARIABLE-DATA-PROTOTYPE><SHORT-NAME>speed</SHORT-NAME>"
            f'<TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">{speed_type}</TYPE-TREF></VARIABLE-DATA-PROTOTYPE>'
            "</DATA-ELEMENTS></SENDER-RECEIVER-INTERFACE>"
        )
        component = (
            "<APPLICATION-SW-COMPONENT-TYPE><SHORT-NAME>ProbeSwc</SHORT-NAME><PORTS><P-PORT-PROTOTYPE>"
            '<SHORT-NAME>pSpeed</SHORT-NAME><PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Ifs/IProbe'
            "</PROVIDED-INTERFACE-TREF></P-PORT-PROTOTYPE></PORTS><INTERNAL-BEHAVIORS><SWC-INTERNAL-BEHAVIOR>"
            "<SHORT-NAME>ProbeSwc_InternalBehavior</SHORT-NAME>"
            "<HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>"
            "</SWC-INTERNAL-BEHAVIOR></INTERNAL-BEHAVIORS></APPLICATION-SW-COMPONENT-TYPE>"
        )
        source = tmp_path / "probe.arxml"
        source.write_text(
            _HEADER
            + _own(
                _limits_constraint(
                    "ProbeSpeed_Range", "<LOWER-LIMIT>-INF</LOWER-LIMIT>", "<UPPER-LIMIT>9</UPPER-LIMIT>"
                )
                + application("ProbeSpeed", constr=f"{_APP}/ProbeSpeed_Range")
            )
            + package(
                "Dropped",
                _limits_constraint("ProbeLimits", "<LOWER-LIMIT>0</LOWER-LIMIT>", "<UPPER-LIMIT>INF</UPPER-LIMIT>")
                + application("ProbeLevel", constr="/Dropped/ProbeLimits"),
            )
            + package("Ifs", interface)
            + package("Swcs", component)
            + _FOOTER,
            encoding="utf-8",
            newline="\n",
        )
        project = _init_project(tmp_path)
        return run_import(str(source), str(project / "swcraft.yaml"), packages=["/Swcs"])

    @pytest.mark.parametrize(
        ("speed_type", "expected"),
        [
            pytest.param(
                f"{_APP}/ProbeSpeed",
                [f"{_APP}/ProbeSpeed: DATA-CONSTR {_APP}/ProbeSpeed_Range PHYS-CONSTRS LOWER-LIMIT: {_REPRESENTED}"],
                id="the-inline-range-kept-the-constraint-dropped",
            ),
            pytest.param(
                "/Dropped/ProbeLevel",
                [f"/Dropped/ProbeLimits: PHYS-CONSTRS UPPER-LIMIT: {_REPRESENTED}"],
                id="the-constraint-kept-the-inline-range-dropped",
            ),
        ],
    )
    def test_a_selection_keeps_the_lines_of_what_it_writes_and_no_other(
        self, tmp_path: Path, speed_type: str, expected: list[str]
    ) -> None:
        """``--packages`` keeps a line about a written type or a written data constraint, and no other."""
        report = self._selected(tmp_path, speed_type)

        assert report.represented_lines == expected
