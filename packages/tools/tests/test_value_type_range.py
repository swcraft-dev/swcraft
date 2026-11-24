"""A value type's ``value_min`` / ``value_max`` are written as its own ``DATA-CONSTR``.

On the writer's side, the reader's, every reserved-name site's and ``import component``'s:

* **the shape**: a value type declaring a bound emits ``{name}_Range``, a ``DATA-CONSTR`` of one
  ``DATA-CONSTR-RULE`` holding ``INTERNAL-CONSTRS`` (``[TPS_SWCT_01288]``: internal constraints are interpreted with
  respect to implementation data types), with a ``LOWER-LIMIT`` for ``value_min`` and an ``UPPER-LIMIT`` for
  ``value_max``, each only when declared, written exactly with ``INTERVAL-TYPE="CLOSED"``; it is emitted just before
  the type, in the type's package, and the type's ``SW-DATA-DEF-PROPS-CONDITIONAL`` references it by a
  ``DATA-CONSTR-REF`` after ``SW-CALIBRATION-ACCESS``. No bound emits neither, and a ``unit`` emits nothing (Table
  5.18 gives it no place);
* **the read-back**: a ``VALUE`` implementation type's constraint's first rule's ``INTERNAL-CONSTRS`` limits are read
  as closed bounds, an ``INFINITE`` limit as an absent one; everything else is one F17 line keyed
  ``({type}/SW-DATA-DEF-PROPS, DATA-CONSTR-REF)``;
* **the reservation**: ``{name}_Range`` is reserved for a bounded value type at every site that reserves an
  application type's forms (the writer's guards, ``check-model``, the ``create_*`` mutations, pass D, ``import
  model`` with a case-only spelling, ``import component``);
* **import component** compares the bounds, and still ignores the ``unit``.

What it does **not** reach: the constraint's XSD validity (``TestTheValueTypeRangesConform``) and real tool exports.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from behaviour_losses_support import snippet
from conftest import model_element_path
from lxml import etree
from project_support import files_of, init_project
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.merge import CONFLICTING, MERGED
from swcraft_tools.arxml_import.merge_verdict import decide_merges
from swcraft_tools.arxml_import.reconcile import reserved_data_type_names
from swcraft_tools.arxml_reader import SkipCategory, read_arxml
from swcraft_tools.arxml_version import NAMESPACE
from swcraft_tools.component_exchange import ComponentImportConflictError, export_component, import_component
from swcraft_tools.schema import SWComponent, ValueDataType
from swcraft_tools.validator import check_cross_layer_consistency, data_description_package_path
from swcraft_tools.writer.arxml_generation import generate_arxml, generated_data_type_forms
from swcraft_tools.writer.mutations import ReservedNameError, create_data_type

NS = f"{{{NAMESPACE}}}"
TESTS = Path(__file__).resolve().parent
EXCHANGE_MODEL_ROOT = TESTS / "fixtures" / "component_exchange" / "model"
INIT_VALUES_MODEL_ROOT = TESTS / "fixtures" / "init_values" / "model"
VERSIONS = ("4.2.2", "4.3.0")
SPEED = "/DataTypes/Implementation/Speed_T"


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _bound(model_root: Path, name: str, **fields: Any) -> None:
    """Give the value type *name* (implementation or application layer) the fields (``None`` removes one)."""
    (path,) = [
        path
        for path in (
            model_root / "data_types" / "implementation" / f"{name}.yaml",
            model_root / "data_types" / "application" / f"{name}.yaml",
        )
        if path.exists()
    ]
    document = _load(path)
    for key, value in fields.items():
        if value is None:
            document.pop(key, None)
        else:
            document[key] = value
    _dump(path, document)


def _exchange_model(root: Path, **bounds: dict[str, Any]) -> Path:
    """The ``component_exchange`` tree, each named value type given its fields."""
    shutil.copytree(EXCHANGE_MODEL_ROOT, root)
    for name, fields in bounds.items():
        _bound(root, name, **fields)
    return root


def _generate(model_root: Path, out: Path, version: str = "4.2.2") -> etree._Element:
    generate_arxml(str(model_root), str(out), version=version)
    return etree.parse(str(out)).getroot()


def _named(root: etree._Element, tag: str, name: str) -> etree._Element:
    matches = [node for node in root.iter(f"{NS}{tag}") if node.findtext(f"{NS}SHORT-NAME") == name]
    assert len(matches) == 1, (tag, name, len(matches))
    return matches[0]


def _conditional(element: etree._Element) -> etree._Element:
    return element.find(f"{NS}SW-DATA-DEF-PROPS/{NS}SW-DATA-DEF-PROPS-VARIANTS/{NS}SW-DATA-DEF-PROPS-CONDITIONAL")


def _local(node: etree._Element) -> str:
    return etree.QName(node).localname


def _limits(constraint: etree._Element) -> tuple[str, list[tuple[str, str, str | None]]]:
    """The one rule's one group tag, and its limits as ``(tag, text, INTERVAL-TYPE)``."""
    rules = constraint.findall(f"{NS}DATA-CONSTR-RULES/{NS}DATA-CONSTR-RULE")
    assert len(rules) == 1
    (group,) = list(rules[0])
    return _local(group), [(_local(limit), limit.text, limit.get("INTERVAL-TYPE")) for limit in group]


# ---------------------------------------------------------------------------
# The shape
# ---------------------------------------------------------------------------


class TestTheConstraintIsWritten:
    @pytest.mark.parametrize("version", VERSIONS)
    def test_each_bound_alone_and_both_as_internal_closed_limits(self, tmp_path: Path, version: str) -> None:
        model_root = _exchange_model(
            tmp_path / "model",
            Speed_T={"value_min": 0.0, "value_max": 300.5},
            float32={"value_min": -40.0},
            uint16={"value_max": 65535},
        )
        root = _generate(model_root, tmp_path / "out.arxml", version)

        assert _limits(_named(root, "DATA-CONSTR", "Speed_T_Range")) == (
            "INTERNAL-CONSTRS",
            [("LOWER-LIMIT", "0", "CLOSED"), ("UPPER-LIMIT", "300.5", "CLOSED")],
        )
        assert _limits(_named(root, "DATA-CONSTR", "float32_Range")) == (
            "INTERNAL-CONSTRS",
            [("LOWER-LIMIT", "-40", "CLOSED")],
        )
        assert _limits(_named(root, "DATA-CONSTR", "uint16_Range")) == (
            "INTERNAL-CONSTRS",
            [("UPPER-LIMIT", "65535", "CLOSED")],
        )

    def test_the_bounds_are_written_exactly(self, tmp_path: Path) -> None:
        model_root = _exchange_model(tmp_path / "model", Speed_T={"value_min": -273.15, "value_max": 0.1})
        _bound(model_root, "float32", value_min=1e-07, value_max=12345678.9)
        root = _generate(model_root, tmp_path / "out.arxml")

        assert _limits(_named(root, "DATA-CONSTR", "Speed_T_Range"))[1] == [
            ("LOWER-LIMIT", "-273.15", "CLOSED"),
            ("UPPER-LIMIT", "0.1", "CLOSED"),
        ]
        assert _limits(_named(root, "DATA-CONSTR", "float32_Range"))[1] == [
            ("LOWER-LIMIT", "0.0000001", "CLOSED"),
            ("UPPER-LIMIT", "12345678.9", "CLOSED"),
        ]

    def test_the_type_references_it_after_its_calibration_access_and_after_it(self, tmp_path: Path) -> None:
        model_root = _exchange_model(tmp_path / "model", Speed_T={"value_min": 0.0})
        root = _generate(model_root, tmp_path / "out.arxml")

        speed = _named(root, "IMPLEMENTATION-DATA-TYPE", "Speed_T")
        conditional = _conditional(speed)
        assert [_local(child) for child in conditional] == [
            "BASE-TYPE-REF",
            "SW-CALIBRATION-ACCESS",
            "DATA-CONSTR-REF",
        ]
        reference = conditional.find(f"{NS}DATA-CONSTR-REF")
        assert (reference.text, reference.get("DEST")) == ("/DataTypes/Implementation/Speed_T_Range", "DATA-CONSTR")
        constraint = _named(root, "DATA-CONSTR", "Speed_T_Range")
        assert constraint.getparent() is speed.getparent()
        siblings = list(speed.getparent())
        assert siblings.index(constraint) == siblings.index(speed) - 1

    def test_no_bound_writes_neither_and_a_unit_writes_nothing(self, tmp_path: Path) -> None:
        model_root = _exchange_model(tmp_path / "model", Speed_T={"unit": "furlong/fortnight"})
        root = _generate(model_root, tmp_path / "out.arxml")

        assert [node.findtext(f"{NS}SHORT-NAME") for node in root.iter(f"{NS}DATA-CONSTR")] == ["Velocity_T_Range"]
        assert _conditional(_named(root, "IMPLEMENTATION-DATA-TYPE", "Speed_T")).find(f"{NS}DATA-CONSTR-REF") is None
        assert not [node for node in root.iter(f"{NS}UNIT") if node.findtext(f"{NS}SHORT-NAME") == "Speed_T_Unit"]
        assert "furlong/fortnight" not in (tmp_path / "out.arxml").read_text(encoding="utf-8")

    def test_a_placed_value_types_constraint_is_placed_beside_it(self, tmp_path: Path) -> None:
        model_root = _exchange_model(tmp_path / "model", Speed_T={"value_max": 1.0, "package": "Signals"})
        root = _generate(model_root, tmp_path / "out.arxml")

        constraint = _named(root, "DATA-CONSTR", "Speed_T_Range")
        package = constraint.getparent().getparent()
        assert package.findtext(f"{NS}SHORT-NAME") == "Signals"
        reference = _conditional(_named(root, "IMPLEMENTATION-DATA-TYPE", "Speed_T")).find(f"{NS}DATA-CONSTR-REF")
        assert reference.text == "/DataTypes/Implementation/Signals/Speed_T_Range"

    @pytest.mark.parametrize("version", VERSIONS)
    def test_init_values_ratio_emits_its_constraint(self, tmp_path: Path, version: str) -> None:
        """The one committed value type with bounds: ``Ratio_T`` in ``init_values``."""
        root = _generate(INIT_VALUES_MODEL_ROOT, tmp_path / "out.arxml", version)

        assert _limits(_named(root, "DATA-CONSTR", "Ratio_T_Range")) == (
            "INTERNAL-CONSTRS",
            [("LOWER-LIMIT", "0", "CLOSED"), ("UPPER-LIMIT", "100", "CLOSED")],
        )
        reference = _conditional(_named(root, "IMPLEMENTATION-DATA-TYPE", "Ratio_T")).find(f"{NS}DATA-CONSTR-REF")
        assert reference.text == "/DataTypes/Implementation/Ratio_T_Range"

    def test_the_fidelity_harness_strips_the_constraint_back(self, tmp_path: Path) -> None:
        """The fidelity strip reverses the value type's ``DATA-CONSTR-REF`` and its ``{name}_Range``."""
        from fidelity_strip_support import strip_counts, strip_fidelity

        out = tmp_path / "out.arxml"
        _generate(INIT_VALUES_MODEL_ROOT, out)
        stripped = strip_fidelity(out.read_bytes())

        assert b"Ratio_T_Range" in out.read_bytes()
        assert b"Ratio_T_Range" not in stripped
        assert strip_counts(stripped)[1] == strip_counts(strip_fidelity(stripped))[1]


# ---------------------------------------------------------------------------
# The read-back and family F17
# ---------------------------------------------------------------------------


def _import_types(source: Path, tmp_path: Path) -> tuple[dict[str, dict[str, Any]], list[Any]]:
    """``import model`` of *source* into a fresh project: the written data types, and the report's entries."""
    project = init_project(tmp_path / "project")
    report = run_import(str(source), str(project / "swcraft.yaml"))
    types = {
        path.stem: _load(path)
        for directory in (
            project / "model" / "data_types" / "implementation",
            project / "model" / "data_types" / "application",
        )
        for path in directory.glob("*.yaml")
    }
    return types, list(report.skip_entries)


def _mutated(source: Path, out: Path, mutate) -> Path:
    tree = etree.parse(str(source))
    mutate(tree.getroot())
    out.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
    return out


def _f17(entries: list[Any], path: str = SPEED) -> list[Any]:
    return [entry for entry in entries if entry.path == f"{path}/SW-DATA-DEF-PROPS" and entry.tag == "DATA-CONSTR-REF"]


@pytest.fixture(scope="module")
def ranged_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """SWCraft's own export of the ``component_exchange`` tree, ``Speed_T`` bounded ``[0, 300.5]``."""
    root = tmp_path_factory.mktemp("ranged")
    model_root = _exchange_model(root / "model", Speed_T={"value_min": 0.0, "value_max": 300.5})
    out = root / "model.arxml"
    generate_arxml(str(model_root), str(out))
    return out


def _speed_group(root: etree._Element) -> etree._Element:
    return _named(root, "DATA-CONSTR", "Speed_T_Range").find(f"{NS}DATA-CONSTR-RULES/{NS}DATA-CONSTR-RULE/{NS}*")


class TestTheBoundsAreReadBack:
    def test_the_writers_own_constraint_reads_back_with_no_line(self, ranged_source: Path, tmp_path: Path) -> None:
        types, entries = _import_types(ranged_source, tmp_path)

        assert (types["Speed_T"].get("value_min"), types["Speed_T"].get("value_max")) == (0.0, 300.5)
        assert _f17(entries) == []
        assert not [entry for entry in entries if "Speed_T_Range" in entry.path]

    def test_the_reader_carries_them_on_the_implementation_record(self, ranged_source: Path) -> None:
        model = read_arxml(str(ranged_source))
        speed = next(impl for impl in model.impl_data_types if impl.name == "Speed_T")
        assert (speed.data_constr_ref, speed.value_min, speed.value_max) == (
            "/DataTypes/Implementation/Speed_T_Range",
            0.0,
            300.5,
        )

    def test_an_absent_interval_type_is_closed_and_an_infinite_one_is_no_bound(
        self, ranged_source: Path, tmp_path: Path
    ) -> None:
        def mutate(root: etree._Element) -> None:
            lower, upper = list(_speed_group(root))
            del lower.attrib["INTERVAL-TYPE"]
            upper.set("INTERVAL-TYPE", "INFINITE")

        types, entries = _import_types(_mutated(ranged_source, tmp_path / "in.arxml", mutate), tmp_path)

        assert (types["Speed_T"].get("value_min"), types["Speed_T"].get("value_max")) == (0.0, None)
        assert _f17(entries) == []

    @pytest.mark.parametrize(
        ("mutation", "what", "carried"),
        [
            ("phys-only", "a DATA-CONSTR-RULE with no INTERNAL-CONSTRS", (None, None)),
            ("open-upper", "an INTERVAL-TYPE OPEN UPPER-LIMIT", (0.0, None)),
            ("misplaced-infinity", "a LOWER-LIMIT spelling the other side's infinity", (None, 300.5)),
            ("unparsable", "a UPPER-LIMIT that is not a finite number", (0.0, None)),
            ("no-break-space", "a UPPER-LIMIT that is not a finite number", (0.0, None)),
            ("no-break-space-interval", "an INTERVAL-TYPE CLOSED\u00a0 UPPER-LIMIT", (0.0, None)),
            ("inverted", "a LOWER-LIMIT above the UPPER-LIMIT, so neither bound", (None, None)),
            ("extra-child", "MAX-DIFF in INTERNAL-CONSTRS", (0.0, 300.5)),
            ("second-rule", "1 further DATA-CONSTR-RULE(s)", (0.0, 300.5)),
            ("phys-beside", "PHYS-CONSTRS, an application data type's range", (0.0, 300.5)),
        ],
    )
    def test_what_is_not_carried_is_one_f17_line(
        self, ranged_source: Path, tmp_path: Path, mutation: str, what: str, carried: tuple
    ) -> None:
        def mutate(root: etree._Element) -> None:
            group = _speed_group(root)
            lower, upper = list(group)
            if mutation == "phys-only":
                group.tag = f"{NS}PHYS-CONSTRS"
            elif mutation == "open-upper":
                upper.set("INTERVAL-TYPE", "OPEN")
            elif mutation == "misplaced-infinity":
                lower.text = "INF"
            elif mutation == "unparsable":
                upper.text = "three hundred"
            elif mutation == "no-break-space":
                # No grammar of a limit admits a no-break space, and XML does not call it white
                # space: it is never stripped as if it were.
                upper.text = f"\u00a0{upper.text}\u00a0"
            elif mutation == "no-break-space-interval":
                upper.set("INTERVAL-TYPE", "CLOSED\u00a0")
            elif mutation == "inverted":
                lower.text = "400"
            elif mutation == "extra-child":
                group.append(snippet("<MAX-DIFF>1</MAX-DIFF>")[0])
            elif mutation == "second-rule":
                rule = group.getparent()
                rule.addnext(etree.fromstring(etree.tostring(rule)))
            elif mutation == "phys-beside":
                group.addprevious(
                    snippet('<PHYS-CONSTRS><LOWER-LIMIT INTERVAL-TYPE="CLOSED">1</LOWER-LIMIT></PHYS-CONSTRS>')[0]
                )

        types, entries = _import_types(_mutated(ranged_source, tmp_path / "in.arxml", mutate), tmp_path)

        (line,) = _f17(entries)
        assert what in line.reason
        assert "DATA-CONSTR-REF of an IMPLEMENTATION-DATA-TYPE not carried" in line.reason
        assert (line.effect, line.category) == ("abort", SkipCategory.NOT_SUPPORTED)
        assert (types["Speed_T"].get("value_min"), types["Speed_T"].get("value_max")) == carried
        # The constraint itself is consumed by the type that names it: no second line calls it uncarried.
        assert not [
            entry
            for entry in entries
            if entry.path == "/DataTypes/Implementation/Speed_T_Range" and entry.tag == "DATA-CONSTR"
        ]

    def test_xml_whitespace_around_a_limit_and_its_interval_is_still_read(
        self, ranged_source: Path, tmp_path: Path
    ) -> None:
        """XML's own white space (space, tab, CR, LF) around a limit's text and its ``INTERVAL-TYPE`` is stripped."""

        def mutate(root: etree._Element) -> None:
            lower, upper = list(_speed_group(root))
            lower.text = f" \t\r\n{lower.text}\n"
            upper.text = f"\n{upper.text} \t"
            upper.set("INTERVAL-TYPE", " CLOSED\t")

        types, entries = _import_types(_mutated(ranged_source, tmp_path / "in.arxml", mutate), tmp_path)

        assert (types["Speed_T"].get("value_min"), types["Speed_T"].get("value_max")) == (0.0, 300.5)
        assert _f17(entries) == []

    def test_an_undeclared_constraint_is_outside_the_file(self, ranged_source: Path, tmp_path: Path) -> None:
        def mutate(root: etree._Element) -> None:
            reference = _conditional(_named(root, "IMPLEMENTATION-DATA-TYPE", "Speed_T")).find(f"{NS}DATA-CONSTR-REF")
            reference.text = "/Elsewhere/Speed_T_Range"

        types, entries = _import_types(_mutated(ranged_source, tmp_path / "in.arxml", mutate), tmp_path)

        (line,) = _f17(entries)
        assert "/Elsewhere/Speed_T_Range is not declared in this file" in line.reason
        assert line.category is SkipCategory.OUTSIDE_FILE
        assert "value_min" not in types["Speed_T"]

    def test_a_constraint_on_a_type_that_is_not_a_value_is_f17(self, ranged_source: Path, tmp_path: Path) -> None:
        def mutate(root: etree._Element) -> None:
            speed = _named(root, "IMPLEMENTATION-DATA-TYPE", "Speed_T")
            speed.addnext(
                snippet(
                    "<IMPLEMENTATION-DATA-TYPE><SHORT-NAME>SpeedRef_T</SHORT-NAME><CATEGORY>TYPE_REFERENCE</CATEGORY>"
                    "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
                    '<DATA-CONSTR-REF DEST="DATA-CONSTR">/DataTypes/Implementation/Speed_T_Range</DATA-CONSTR-REF>'
                    '<IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/DataTypes/Implementation/Speed_T'
                    "</IMPLEMENTATION-DATA-TYPE-REF>"
                    "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
                    "</IMPLEMENTATION-DATA-TYPE>"
                )[0]
            )

        types, entries = _import_types(_mutated(ranged_source, tmp_path / "in.arxml", mutate), tmp_path)

        (line,) = _f17(entries, "/DataTypes/Implementation/SpeedRef_T")
        assert "only a VALUE type has a place for a range" in line.reason
        assert _f17(entries) == []
        assert types["SpeedRef_T"]["kind"] == "reference_type"

    def test_an_application_types_internal_constraints_stay_unread(self, ranged_source: Path, tmp_path: Path) -> None:
        """No value type reads ``Velocity_T_Range``: its ``INTERNAL-CONSTRS`` is the closed table's unread content."""

        def mutate(root: etree._Element) -> None:
            rule = _named(root, "DATA-CONSTR", "Velocity_T_Range").find(f"{NS}DATA-CONSTR-RULES/{NS}DATA-CONSTR-RULE")
            rule.append(
                snippet('<INTERNAL-CONSTRS><LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT></INTERNAL-CONSTRS>')[0]
            )

        _types, entries = _import_types(_mutated(ranged_source, tmp_path / "in.arxml", mutate), tmp_path)

        unread = [
            entry for entry in entries if entry.path == "/DataTypes/Application/Velocity_T_Range/DATA-CONSTR-RULES"
        ]
        assert [(entry.tag, entry.effect) for entry in unread] == [("LOWER-LIMIT", "abort")]
        assert "INTERNAL-CONSTRS of a DATA-CONSTR-RULE is not read by SWCraft" in unread[0].reason

    def test_a_constraint_an_application_type_and_a_value_type_share_carries_both(
        self, ranged_source: Path, tmp_path: Path
    ) -> None:
        """``[TPS_SWCT_01288]``: one constraint, both halves carried.

        Its physical half is the application type's range, its internal half the value type's bounds.
        """

        def mutate(root: etree._Element) -> None:
            reference = _conditional(_named(root, "IMPLEMENTATION-DATA-TYPE", "Speed_T")).find(f"{NS}DATA-CONSTR-REF")
            reference.text = "/DataTypes/Application/Velocity_T_Range"
            rule = _named(root, "DATA-CONSTR", "Velocity_T_Range").find(f"{NS}DATA-CONSTR-RULES/{NS}DATA-CONSTR-RULE")
            rule.append(
                snippet(
                    '<INTERNAL-CONSTRS><LOWER-LIMIT INTERVAL-TYPE="CLOSED">5</LOWER-LIMIT>'
                    '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">6</UPPER-LIMIT></INTERNAL-CONSTRS>'
                )[0]
            )

        types, entries = _import_types(_mutated(ranged_source, tmp_path / "in.arxml", mutate), tmp_path)

        assert (types["Speed_T"].get("value_min"), types["Speed_T"].get("value_max")) == (5.0, 6.0)
        assert types["Velocity_T"].get("range")
        assert _f17(entries) == []
        assert not [
            entry for entry in entries if entry.path == "/DataTypes/Application/Velocity_T_Range/DATA-CONSTR-RULES"
        ]
        shared = [
            entry
            for entry in entries
            if entry.path == "/DataTypes/Application/Velocity_T_Range" and entry.tag == "DATA-CONSTR"
        ]
        assert [entry.reason for entry in shared] == [
            "DATA-CONSTR shared by 2 data types not carried as one: SWCraft writes one per type"
        ]


class TestTwoDeclarationsOfOneNameAreComparedByTheirBounds:
    """A duplicate ``VALUE`` type merges only when the bounds the import would write agree."""

    @staticmethod
    def _twin(root: etree._Element, lower: str, upper: str) -> None:
        """A second ``Speed_T`` with its own constraint, in a package of its own."""
        speed = etree.fromstring(etree.tostring(_named(root, "IMPLEMENTATION-DATA-TYPE", "Speed_T")))
        constraint = etree.fromstring(etree.tostring(_named(root, "DATA-CONSTR", "Speed_T_Range")))
        _conditional(speed).find(f"{NS}DATA-CONSTR-REF").text = "/Other/Speed_T_Range"
        limits = constraint.find(f"{NS}DATA-CONSTR-RULES/{NS}DATA-CONSTR-RULE/{NS}INTERNAL-CONSTRS")
        limits.find(f"{NS}LOWER-LIMIT").text = lower
        limits.find(f"{NS}UPPER-LIMIT").text = upper
        package = snippet("<AR-PACKAGE><SHORT-NAME>Other</SHORT-NAME><ELEMENTS/></AR-PACKAGE>")[0]
        package.find(f"{NS}ELEMENTS").extend([speed, constraint])
        root.find(f"{NS}AR-PACKAGES").append(package)

    def test_differing_bounds_are_a_conflict_and_neither_is_imported(self, ranged_source: Path, tmp_path: Path) -> None:
        source = _mutated(ranged_source, tmp_path / "in.arxml", lambda root: self._twin(root, "5", "50"))
        model = read_arxml(str(source))
        (group,) = [g for g in decide_merges(model).groups if g.key.name == "Speed_T"]
        assert (group.verdict, group.difference) == (CONFLICTING, "DATA-CONSTR")

        types, _entries = _import_types(source, tmp_path)
        assert "Speed_T" not in types

    def test_equal_bounds_merge(self, ranged_source: Path, tmp_path: Path) -> None:
        source = _mutated(ranged_source, tmp_path / "in.arxml", lambda root: self._twin(root, "0", "300.5"))
        model = read_arxml(str(source))
        (group,) = [g for g in decide_merges(model).groups if g.key.name == "Speed_T"]
        assert group.verdict == MERGED


# ---------------------------------------------------------------------------
# The reservation, at every site
# ---------------------------------------------------------------------------

#: A 123-character name: legal alone, and 129 characters with ``_Range``.
LONG_NAME = "R" + "x" * 122


def _types_model(tmp_path: Path, *documents: dict[str, Any]) -> Path:
    """The ``component_exchange`` tree with *documents* added to their layers' directories."""
    model_root = _exchange_model(tmp_path / "model")
    for document in documents:
        _dump(model_element_path(model_root, document["name"], kind=document["kind"]), document)
    return model_root


def _ratio(**bounds: Any) -> dict[str, Any]:
    return {"name": "Ratio", "kind": "value_type", "base": "float32_base", **bounds}


def _occupant(name: str) -> dict[str, Any]:
    return {"name": name, "kind": "value_type", "base": "uint8_base"}


def _rules(model_root: Path) -> list[tuple[str, str]]:
    return [(record.rule, record.message) for record in check_cross_layer_consistency(str(model_root)).records]


class TestTheRangeIsReserved:
    def test_check_model_reports_the_collision_naming_the_value_type(self, tmp_path: Path) -> None:
        model_root = _types_model(tmp_path, _ratio(value_max=1.0), _occupant("Ratio_Range"))

        assert (
            "data-type.range-name-collision",
            "Data type name 'Ratio_Range' conflicts with the auto-generated DATA-CONSTR name for value type 'Ratio'.",
        ) in _rules(model_root)
        with pytest.raises(ValueError, match=r"\['Ratio_Range'\] collide with auto-generated '\{base\}_Range'"):
            generate_arxml(str(model_root), str(tmp_path / "out.arxml"))
        assert not (tmp_path / "out.arxml").exists()

    def test_an_unbounded_value_type_reserves_nothing(self, tmp_path: Path) -> None:
        model_root = _types_model(tmp_path, _ratio(unit="m"), _occupant("Ratio_Range"))

        assert not [rule for rule, _message in _rules(model_root) if rule.endswith("-name-collision")]
        generate_arxml(str(model_root), str(tmp_path / "out.arxml"))
        assert generated_data_type_forms(ValueDataType(**_ratio(unit="m"))) == frozenset()

    def test_a_case_only_spelling_is_a_name_space_collision(self, tmp_path: Path) -> None:
        model_root = _types_model(tmp_path, _ratio(value_min=0.0), _occupant("RATIO_range"))

        assert "name.name-space-collision" in [rule for rule, _message in _rules(model_root)]
        with pytest.raises(ValueError):
            generate_arxml(str(model_root), str(tmp_path / "out.arxml"))

    def test_the_identifier_budget_is_charged_for_the_generated_name(self, tmp_path: Path) -> None:
        bounded = _types_model(tmp_path / "bounded", {**_ratio(value_min=0.0), "name": LONG_NAME})
        unbounded = _types_model(tmp_path / "unbounded", {**_ratio(), "name": LONG_NAME})

        assert any(
            rule == "identifier.generated" and f"{LONG_NAME}_Range" in message for rule, message in _rules(bounded)
        ), _rules(bounded)
        assert not [rule for rule, _message in _rules(unbounded) if rule.startswith("identifier.")]
        with pytest.raises(ValueError, match="generated DATA-CONSTR name"):
            generate_arxml(str(bounded), str(tmp_path / "out.arxml"))

    def test_the_range_lies_in_the_implementation_package_so_an_application_type_may_take_it(
        self, tmp_path: Path
    ) -> None:
        """A generated form lies in its owner's package and collides only with a type of that package.

        An application type ``Ratio_Range`` in ``/DataTypes/Application`` is legal beside the bounded ``Ratio``."""
        bounded = ValueDataType(**_ratio(value_max=1.0))
        assert data_description_package_path(bounded) == "DataTypes/Implementation"
        assert generated_data_type_forms(bounded) == frozenset({"_Range"})

        occupant = {"name": "Ratio_Range", "kind": "application_type", "unit": "m"}
        model_root = _types_model(tmp_path, _ratio(value_max=1.0), occupant)

        assert [rule for rule, _message in _rules(model_root) if rule.startswith(("data-type.", "name."))] == []
        root = _generate(model_root, tmp_path / "out.arxml")
        constraint = _named(root, "DATA-CONSTR", "Ratio_Range")
        application = _named(root, "APPLICATION-PRIMITIVE-DATA-TYPE", "Ratio_Range")
        assert constraint.getparent().getparent().findtext(f"{NS}SHORT-NAME") == "Implementation"
        assert application.getparent().getparent().findtext(f"{NS}SHORT-NAME") == "Application"

    def test_the_write_path_refuses_both_directions(self, tmp_path: Path) -> None:
        model_root = _types_model(tmp_path, _ratio(value_max=1.0))
        with pytest.raises(ReservedNameError, match="DATA-CONSTR auto-generated for 'Ratio'") as claimed:
            create_data_type(str(model_root), "Ratio_Range", "value_type", base="uint8_base")
        # The refusal names the element it is about in its own layer.
        assert (claimed.value.layer, claimed.value.element) == ("implementation_types", "Ratio_Range")

        other = _types_model(tmp_path / "other", _occupant("Level_Range"))
        with pytest.raises(ReservedNameError, match="auto-generated DATA-CONSTR") as owner:
            create_data_type(str(other), "Level", "value_type", base="float32_base", value_min=0.0)
        assert (owner.value.layer, owner.value.element) == ("implementation_types", "Level")
        create_data_type(str(other), "Level", "value_type", base="float32_base")

    def test_import_model_renames_the_exact_and_the_case_only_spelling(self) -> None:
        bounded = ValueDataType(**_ratio(value_min=0.0))
        for claim in ("Ratio_Range", "rATIO_rANGE"):
            reserved = reserved_data_type_names({"Ratio": bounded, claim: ValueDataType(**_occupant(claim))})
            assert reserved == {claim: "collides with the DATA-CONSTR auto-generated for 'Ratio'"}
        unbounded = ValueDataType(**_ratio())
        assert (
            reserved_data_type_names({"Ratio": unbounded, "Ratio_Range": ValueDataType(**_occupant("Ratio_Range"))})
            == {}
        )

    def test_import_component_refuses_the_exact_and_the_case_only_spelling(self) -> None:
        from swcraft_tools.component_exchange.name_policies import reserved_name_violations

        for claim in ("Ratio_Range", "RATIO_range"):
            violations = reserved_name_violations(
                SWComponent(name="Unrelated"),
                [ValueDataType(**_occupant(claim))],
                {},
                set(),
                {("implementation_types", "Ratio"): ValueDataType(**_ratio(value_max=2.0))},
                [],
                {},
            )
            assert len(violations) == 1 and f"'{claim}'" in violations[0], violations
        assert (
            reserved_name_violations(
                SWComponent(name="Unrelated"),
                [ValueDataType(**_occupant("Ratio_Range"))],
                {},
                set(),
                {("implementation_types", "Ratio"): ValueDataType(**_ratio())},
                [],
                {},
            )
            == []
        )


class TestPassDRefusesWhatCheckModelReports:
    """The editing protocol's pass D, through the whole engine, on the fixture every validation vector uses."""

    @staticmethod
    def _project(tmp_path: Path, *documents: dict[str, Any]) -> Path:
        import editing_vector_fixtures as fixtures

        project_root = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
        for document in documents:
            _dump(model_element_path(project_root / "model", document["name"], kind="value_type"), document)
        return project_root

    @staticmethod
    def _create(project_root: Path, document: dict[str, Any]) -> dict[str, Any]:
        from test_editing_validation import _create_data_type, _dispatch

        return _dispatch(project_root, _create_data_type(document))

    @pytest.mark.parametrize("claim", ["Ratio_Range", "RATIO_range"])
    def test_a_claim_on_a_bounded_value_types_range_is_refused(self, tmp_path: Path, claim: str) -> None:
        project_root = self._project(tmp_path, _ratio(value_max=1.0))

        response = self._create(project_root, _occupant(claim) | {"base": "float32_base"})

        assert response.get("error") == "consistency-violation", response

    def test_an_unbounded_value_types_range_is_free(self, tmp_path: Path) -> None:
        project_root = self._project(tmp_path, _ratio())

        response = self._create(project_root, _occupant("Ratio_Range") | {"base": "float32_base"})

        assert response.get("error") is None and response["status"] == "staged", response

    def test_an_overlong_generated_range_is_an_invalid_identifier(self, tmp_path: Path) -> None:
        project_root = self._project(tmp_path)

        bounded = self._create(project_root, {**_ratio(value_min=0.0), "name": LONG_NAME})
        unbounded = self._create(project_root, {**_ratio(), "name": LONG_NAME})

        assert bounded.get("error") == "invalid-identifier", bounded
        assert unbounded.get("error") is None, unbounded


# ---------------------------------------------------------------------------
# import component
# ---------------------------------------------------------------------------


def _exchange_target(tmp_path: Path, **speed: Any) -> Path:
    project = init_project(tmp_path / "target")
    shutil.rmtree(project / "model")
    _exchange_model(project / "model", Speed_T=speed)
    return project / "model"


def _export(tmp_path: Path, **speed: Any) -> Path:
    model_root = _exchange_model(tmp_path / "source" / "model", Speed_T=speed)
    out = tmp_path / "source" / "SensorSwc.arxml"
    export_component(str(model_root), "SensorSwc", str(out))
    return out


class TestImportComponentComparesTheBounds:
    def test_equal_bounds_are_reused(self, tmp_path: Path) -> None:
        model_root = _exchange_target(tmp_path, value_min=0.0, value_max=300.0, unit="km/h")
        before = files_of(model_root)

        report = import_component(str(_export(tmp_path, value_min=0.0, value_max=300.0)), str(model_root))

        assert "Speed_T" in report.reused_data_types
        assert files_of(model_root) == before

    @pytest.mark.parametrize(
        ("target", "incoming"),
        [
            ({"value_min": 0.0, "value_max": 300.0}, {"value_min": 0.0, "value_max": 250.0}),
            ({"value_min": 0.0}, {}),
            ({}, {"value_max": 1.0}),
        ],
        ids=["differing", "target-only", "incoming-only"],
    )
    def test_a_differing_bound_is_a_conflict(self, tmp_path: Path, target: dict, incoming: dict) -> None:
        model_root = _exchange_target(tmp_path, **target)
        before = files_of(model_root)

        with pytest.raises(ComponentImportConflictError) as caught:
            import_component(str(_export(tmp_path, **incoming)), str(model_root))

        assert "Speed_T" in str(caught.value)
        assert files_of(model_root) == before
