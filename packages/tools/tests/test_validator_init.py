"""`check_init_conformance`: the value an `init` may hold, against its declared type.

`schema.py`'s `InitValue` admits any nested scalar, list or map and does not relate the value to the declared
type; this pass does, by eight rules:

* `interface.init-shape-mismatch` — a composite where the type takes a scalar, a scalar where it takes a
  composite, or a `bool` under a value type that declares a numeric range;
* `interface.init-unknown-enum-literal` — an enum-typed value that is not the **name** of one of its literals;
* `interface.init-record-field-mismatch` — a map whose keys are not exactly the record's declared fields;
* `interface.init-array-length-mismatch` — a list whose length is not the array's declared length;
* `interface.init-out-of-range` — a number outside a value type's `value_min` / `value_max`;
* `interface.init-string-scalar` — a `str` under a value or reference type, whatever its base and bounds (a text
  names an enum literal, which is the enum rule's);
* `interface.init-unwritable-integer` — an `int`, never a `bool`, at any depth, that this interpreter cannot
  convert to its decimal string;
* `interface.init-on-external-type` — an `init` on an element typed by an external data type stub.

**What this pass does not reach:**

* **a base type's numeric domain:** `init: 999` under a value type over an 8-bit base type is admitted; only a
  value type's bounds are checked;
* **an unresolvable type:** a `type` naming no declared data type, a value type over anything but a base type, an
  undeclared record field or array element type and a type-graph cycle are other checks' and not reported again
  (`TestAnUnresolvableTypeIsReportedOnce`);
* **the client/server side:** `CSArgument` carries no `init`.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import validator_package
import yaml
from conftest import model_element_path
from swcraft_tools import validator
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.editing.diagnosis import CONSISTENCY_VIOLATION, RULE_CODES
from swcraft_tools.validator import CONSISTENCY_RULES, check_cross_layer_consistency, check_init_conformance

#: The eight rules this pass mints, written out once and checked against
#: `CONSISTENCY_RULES`, so a rule renamed in the validator fails here.
#: `interface.init-constant-name-collision` shares the prefix but is minted by
#: `check_name_uniqueness`.
INIT_RULES: tuple[str, ...] = (
    "interface.init-shape-mismatch",
    "interface.init-unknown-enum-literal",
    "interface.init-record-field-mismatch",
    "interface.init-array-length-mismatch",
    "interface.init-out-of-range",
    "interface.init-string-scalar",
    "interface.init-unwritable-integer",
    "interface.init-on-external-type",
)


# ---------------------------------------------------------------------------
# Model fixtures
# ---------------------------------------------------------------------------

#: Data types wide enough for every shape the rules name: two base types, value
#: types with bounds and without, two application types, an enum whose literals include ones spelled
#: like YAML 1.1 booleans, records of one and two fields, arrays of length one
#: and three, an array of records and a record of a record.
DATA_TYPES: dict[str, dict[str, Any]] = {
    # A data prototype names a value type over a `{name}_base` base type; a base
    # type types no prototype.
    "uint8_base": {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE"},
    "float32_base": {"name": "float32_base", "kind": "base_type", "size": 32, "encoding": "IEEE754"},
    "uint8": {"name": "uint8", "kind": "value_type", "base": "uint8_base"},
    "float32": {"name": "float32", "kind": "value_type", "base": "float32_base"},
    "Percent": {
        "name": "Percent",
        "kind": "value_type",
        "base": "float32_base",
        "unit": "percent",
        "value_min": 0.0,
        "value_max": 100.0,
    },
    "Counter": {"name": "Counter", "kind": "value_type", "base": "uint8_base"},
    # A record field and an array element name an application type, so the
    # composites below are built on `Octet` (unbounded, like `uint8`) and
    # `Fraction` (bounded 0-100, like `Percent`).
    "Octet": {"name": "Octet", "kind": "application_type"},
    "Fraction": {"name": "Fraction", "kind": "application_type", "range": {"min": 0.0, "max": 100.0}},
    "SwitchState": {
        "name": "SwitchState",
        "kind": "enum",
        # `ON` and `NO` both resolve to a YAML 1.1 non-string, which is why one
        # negative below writes `init: ON` unquoted.
        "literals": [{"name": "OFF", "value": 0}, {"name": "ON", "value": 1}, {"name": "NO", "value": 2}],
    },
    "Point": {
        "name": "Point",
        "kind": "record",
        "fields": [{"name": "x", "type": "Octet"}, {"name": "y", "type": "Octet"}],
    },
    "OneField": {"name": "OneField", "kind": "record", "fields": [{"name": "only", "type": "Octet"}]},
    "Triple": {"name": "Triple", "kind": "array", "element_type": "Octet", "length": 3},
    "Single": {"name": "Single", "kind": "array", "element_type": "Octet", "length": 1},
    "PointPair": {"name": "PointPair", "kind": "array", "element_type": "Point", "length": 2},
    "Nested": {
        "name": "Nested",
        "kind": "record",
        "fields": [{"name": "where", "type": "Point"}, {"name": "load", "type": "Fraction"}],
    },
}


class _Absent:
    """The sentinel for "no `init` key at all", which `None` cannot spell here."""


_ABSENT = _Absent()


def _write(model_root: Path, layer: str, stem: str, document: Any) -> None:
    # A data type is stored in the layer its kind belongs to.
    if layer == "data_types" and isinstance(document, dict) and "kind" in document:
        path = model_element_path(model_root, stem, kind=document["kind"])
    else:
        path = model_root / layer / f"{stem}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(document, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    """`data_types/` populated and `interfaces/` empty; each test writes its own.

    Only the two layers this pass reads; the aggregate's answers are
    `test_validator.py`'s.
    """
    root = tmp_path / "model"
    for name, document in DATA_TYPES.items():
        _write(root, "data_types", name, document)
    (root / "interfaces").mkdir(parents=True, exist_ok=True)
    return root


def _type_field(model_root: Path, element_type: str) -> str:
    """`application_type` when *element_type* is a declared application type, else `implementation_type`."""
    application = model_element_path(model_root, element_type, kind="enum")
    return "application_type" if application.is_file() else "implementation_type"


def _interface(model_root: Path, element_type: str, init: Any) -> None:
    """One SR interface, `SR_Probe`, with one data element `Value` carrying *init*."""
    element: dict[str, Any] = {"name": "Value", _type_field(model_root, element_type): element_type}
    if not isinstance(init, _Absent):
        element["init"] = init
    _write(
        model_root,
        "interfaces",
        "SR_Probe",
        {"name": "SR_Probe", "kind": "sender_receiver", "data_elements": [element]},
    )


def _records(model_root: Path) -> list[Any]:
    return list(check_init_conformance(str(model_root)).records)


def _rules(model_root: Path) -> list[str]:
    return [record.rule for record in _records(model_root)]


def _one(model_root: Path, rule: str) -> Any:
    result = check_init_conformance(str(model_root))
    matching = [record for record in result.records if record.rule == rule]
    assert matching, f"expected {rule!r}, got {[record.rule for record in result.records]}"
    assert len(matching) == 1, f"expected one {rule!r}, got {[record.message for record in matching]}"
    return matching[0]


def _init_subject() -> Subject:
    return Subject(
        "interfaces",
        "SR_Probe",
        (field_step("data_elements"), member_step("Value"), field_step("init")),
    )


# ---------------------------------------------------------------------------
# 1. The positive vectors, one per shape
# ---------------------------------------------------------------------------


class TestAConformingInitReportsNothing:
    """Every conforming shape is accepted; the refusals are below."""

    @pytest.mark.parametrize(
        ("label", "element_type", "init"),
        [
            ("no init at all", "uint8", _ABSENT),
            ("primitive, integer", "uint8", 7),
            ("primitive, float", "float32", 1.5),
            # No string row: `interface.init-string-scalar` refuses one on a
            # value type, bounded or not (`TestAStringScalarIsOneRule`).
            ("primitive, boolean", "uint8", True),
            ("alias with no bounds", "Counter", 200),
            ("alias inside its bounds", "Percent", 50.0),
            ("alias at value_min", "Percent", 0.0),
            ("alias at value_max", "Percent", 100.0),
            ("alias bound met by an integer", "Percent", 100),
            ("enum literal", "SwitchState", "OFF"),
            ("enum literal spelled like a YAML boolean, quoted", "SwitchState", "ON"),
            ("record, exact fields", "Point", {"x": 1, "y": 2}),
            ("record of one field", "OneField", {"only": 3}),
            ("array, exact length", "Triple", [1, 2, 3]),
            ("array of length 1", "Single", [9]),
            ("array of records", "PointPair", [{"x": 1, "y": 2}, {"x": 3, "y": 4}]),
            ("record of a record and a bounded alias", "Nested", {"where": {"x": 1, "y": 2}, "load": 12.5}),
        ],
        ids=lambda value: value if isinstance(value, str) else "",
    )
    def test_it_is_accepted(self, model_root: Path, label: str, element_type: str, init: Any) -> None:
        _interface(model_root, element_type, init)

        result = check_init_conformance(str(model_root))

        assert result.consistent, result.issues
        assert result.records == ()

    def test_a_client_server_interface_is_untouched(self, model_root: Path) -> None:
        """`CSArgument` carries no `init`; the pass must not invent one for it."""
        _write(
            model_root,
            "interfaces",
            "CS_Probe",
            {
                "name": "CS_Probe",
                "kind": "client_server",
                "operations": [
                    {"name": "Get", "arguments": [{"name": "out", "direction": "out", "implementation_type": "uint8"}]}
                ],
            },
        )

        assert check_init_conformance(str(model_root)).consistent

    def test_the_quoted_yaml_boolean_really_is_a_string_on_the_way_in(self, model_root: Path) -> None:
        """The loader keeps the quoted `'ON'` a string, so the positive above is not vacuous."""
        _interface(model_root, "SwitchState", "ON")

        document = yaml.safe_load((model_root / "interfaces" / "SR_Probe.yaml").read_text(encoding="utf-8"))
        assert document["data_elements"][0]["init"] == "ON"
        assert isinstance(document["data_elements"][0]["init"], str)


# ---------------------------------------------------------------------------
# 2. The negative vectors — one per rule id
# ---------------------------------------------------------------------------


class TestEachRuleFiresForItsOwnFailure:
    """One model per rule, each breaking exactly it.

    The isolation is asserted as **equality** over the reported rules and not as
    membership: a case that also tripped a second rule would satisfy a
    membership assertion and hide which of the two the model is really about.
    """

    def test_a_composite_where_a_scalar_is_declared(self, model_root: Path) -> None:
        _interface(model_root, "uint8", [1, 2])

        assert _rules(model_root) == ["interface.init-shape-mismatch"]
        assert "uint8" in _one(model_root, "interface.init-shape-mismatch").message

    def test_a_scalar_where_a_record_is_declared(self, model_root: Path) -> None:
        _interface(model_root, "Point", 3)

        assert _rules(model_root) == ["interface.init-shape-mismatch"]

    def test_a_scalar_where_an_array_is_declared(self, model_root: Path) -> None:
        _interface(model_root, "Triple", 3)

        assert _rules(model_root) == ["interface.init-shape-mismatch"]

    def test_a_composite_where_an_enum_literal_name_is_declared(self, model_root: Path) -> None:
        _interface(model_root, "SwitchState", ["OFF"])

        assert _rules(model_root) == ["interface.init-shape-mismatch"]

    @pytest.mark.parametrize(("label", "value"), [("a boolean", True)])
    def test_a_non_number_under_an_alias_that_declares_a_range(self, model_root: Path, label: str, value: Any) -> None:
        """A boolean under a bounded value type is a shape refusal, not an out-of-range one.

        `init: ON` loads as `True`, and a bound cannot be applied to a boolean. A
        `str` is refused one branch earlier, as `interface.init-string-scalar`; the
        `bool` asymmetry is `TestTheBoolAsymmetryIsKeptByDecision`'s.
        """
        _interface(model_root, "Percent", value)

        assert _rules(model_root) == ["interface.init-shape-mismatch"], label

    def test_a_value_below_value_min(self, model_root: Path) -> None:
        _interface(model_root, "Percent", -0.5)

        assert _rules(model_root) == ["interface.init-out-of-range"]
        assert "Percent" in _one(model_root, "interface.init-out-of-range").message

    def test_a_value_above_value_max(self, model_root: Path) -> None:
        _interface(model_root, "Percent", 100.5)

        assert _rules(model_root) == ["interface.init-out-of-range"]

    def test_a_name_no_literal_of_the_enum_carries(self, model_root: Path) -> None:
        _interface(model_root, "SwitchState", "MAYBE")

        record = _one(model_root, "interface.init-unknown-enum-literal")
        assert _rules(model_root) == ["interface.init-unknown-enum-literal"]
        assert "MAYBE" in record.message
        assert "OFF" in record.message, "the declared literals are named, so the repair is in the message"

    def test_a_number_where_the_enum_takes_a_literal_name(self, model_root: Path) -> None:
        """The literal's name, and only the name: a numeric spelling is refused."""
        _interface(model_root, "SwitchState", 1)

        assert _rules(model_root) == ["interface.init-unknown-enum-literal"]

    def test_an_unquoted_yaml_boolean_where_a_literal_name_is_meant(self, model_root: Path) -> None:
        """An unquoted `ON` loads as a boolean, and the refusal tells the author to quote it.

        The enum declares a literal named `ON`, so the element would conform if
        YAML 1.1 resolved the token as a string.
        """
        (model_root / "interfaces" / "SR_Probe.yaml").write_text(
            "name: SR_Probe\nkind: sender_receiver\ndata_elements:\n"
            "  - name: Value\n    application_type: SwitchState\n    init: ON\n",
            encoding="utf-8",
            newline="\n",
        )

        record = _one(model_root, "interface.init-unknown-enum-literal")
        assert _rules(model_root) == ["interface.init-unknown-enum-literal"]
        assert "quot" in record.message.lower(), record.message

    def test_a_record_map_missing_a_declared_field(self, model_root: Path) -> None:
        _interface(model_root, "Point", {"x": 1})

        assert _rules(model_root) == ["interface.init-record-field-mismatch"]
        assert "y" in _one(model_root, "interface.init-record-field-mismatch").message

    def test_a_record_map_carrying_an_undeclared_field(self, model_root: Path) -> None:
        _interface(model_root, "Point", {"x": 1, "y": 2, "z": 3})

        assert _rules(model_root) == ["interface.init-record-field-mismatch"]
        assert "z" in _one(model_root, "interface.init-record-field-mismatch").message

    def test_a_list_of_the_wrong_length(self, model_root: Path) -> None:
        _interface(model_root, "Triple", [1, 2])

        assert _rules(model_root) == ["interface.init-array-length-mismatch"]
        assert "3" in _one(model_root, "interface.init-array-length-mismatch").message


class TestTheWalkReachesEveryDepth:
    """A nested value is refused where it sits, and the message says where."""

    def test_a_member_of_an_array_of_records(self, model_root: Path) -> None:
        _interface(model_root, "PointPair", [{"x": 1, "y": 2}, {"x": 3}])

        assert "init[1]" in _one(model_root, "interface.init-record-field-mismatch").message

    def test_a_field_of_a_record_of_a_record(self, model_root: Path) -> None:
        _interface(model_root, "Nested", {"where": {"x": 1}, "load": 12.5})

        assert "init.where" in _one(model_root, "interface.init-record-field-mismatch").message

    def test_a_bounded_alias_nested_inside_a_record(self, model_root: Path) -> None:
        _interface(model_root, "Nested", {"where": {"x": 1, "y": 2}, "load": 400.0})

        assert "init.load" in _one(model_root, "interface.init-out-of-range").message

    def test_two_failures_in_one_value_are_two_records(self, model_root: Path) -> None:
        """The walk does not stop at the first refusal, in the pass and in the aggregate.

        The pass returns its records undeduplicated; the aggregate
        (`check_cross_layer_consistency`) deduplicates by **message**, so the second
        assertion shows two findings of one rule survive because the path is part
        of the message.
        """
        _interface(model_root, "PointPair", [{"x": 1}, {"y": 2}])

        records = _records(model_root)
        assert [record.rule for record in records] == ["interface.init-record-field-mismatch"] * 2
        assert len({record.message for record in records}) == 2

        aggregate = [
            record
            for record in check_cross_layer_consistency(str(model_root)).records
            if record.rule.startswith("interface.init-")
        ]
        assert len(aggregate) == 2, [record.message for record in aggregate]


class TestAStringScalarIsOneRule:
    """A `str` under a type that references no table is one `interface.init-string-scalar` finding.

    Such a type carries a number, whatever its base `encoding` and whether or not
    a bound is declared. A value or reference type that references a table takes
    an entry's name (a `TEXT-VALUE-SPECIFICATION`) and is judged by
    `interface.init-unknown-enum-literal` instead.

    **One finding and never two.** On the bounded row the "is not a number"
    refusal still exists for a `bool`; a string reaching it too would report one
    value twice with two repairs, so every assertion is an equality.

    **Does not reach** a string-typed signal: an `encoding`-aware rule would
    accept a string on a `VOID`/`ISO-8859-1` base type that the writer then
    refuses, a disagreement that must never happen.
    """

    @pytest.mark.parametrize(
        ("label", "element_type"),
        [
            ("a bounded alias", "Percent"),
            ("an unbounded alias over the same base", "Counter"),
            ("a bare primitive", "uint8"),
        ],
        ids=lambda value: value if isinstance(value, str) and " " in value else "",
    )
    def test_an_alphabetic_string_is_one_string_scalar_finding(
        self, model_root: Path, label: str, element_type: str
    ) -> None:
        _interface(model_root, element_type, "abc")

        assert _rules(model_root) == ["interface.init-string-scalar"], label

    @pytest.mark.parametrize(
        ("label", "element_type"),
        [
            ("a bounded alias", "Percent"),
            ("an unbounded alias over the same base", "Counter"),
            ("a bare primitive", "uint8"),
        ],
        ids=lambda value: value if isinstance(value, str) and " " in value else "",
    )
    def test_a_numeric_looking_string_is_the_same_one_finding(
        self, model_root: Path, label: str, element_type: str
    ) -> None:
        """A quoted numeric string like `'010'` is the same one finding.

        It looks writable and is not: the model carries a `str` and the emitter
        has no text form to put it in.
        """
        _interface(model_root, element_type, "010")

        assert _rules(model_root) == ["interface.init-string-scalar"], label

    def test_the_message_names_the_string_and_the_declared_type(self, model_root: Path) -> None:
        _interface(model_root, "uint8", "abc")

        record = _one(model_root, "interface.init-string-scalar")

        assert "'abc'" in record.message
        assert "uint8" in record.message

    def test_a_string_inside_a_record_field_is_reached(self, model_root: Path) -> None:
        """The walk is the same walk: depth is not a special case."""
        _interface(model_root, "Point", {"x": 1, "y": "two"})

        assert _rules(model_root) == ["interface.init-string-scalar"]
        assert "init.y" in _one(model_root, "interface.init-string-scalar").message

    def test_a_string_inside_an_array_member_is_reached(self, model_root: Path) -> None:
        _interface(model_root, "Triple", [1, "2", 3])

        assert _rules(model_root) == ["interface.init-string-scalar"]
        assert "init[1]" in _one(model_root, "interface.init-string-scalar").message

    def test_a_string_under_a_bounded_alias_nested_in_a_record_is_one_finding_too(self, model_root: Path) -> None:
        """The composition of the two cases that could each report on their own."""
        _interface(model_root, "Nested", {"where": {"x": 1, "y": 2}, "load": "abc"})

        assert _rules(model_root) == ["interface.init-string-scalar"]

    def test_an_enum_literal_name_is_still_the_literal_branch(self, model_root: Path) -> None:
        """An enum takes the literal's NAME, a `str`, and stays accepted.

        The string refusal sits on the value/reference type branch, not over the
        value's own type, or every enum init would be a finding.
        """
        _interface(model_root, "SwitchState", "ON")

        assert _rules(model_root) == []

    def test_an_unknown_enum_literal_is_still_the_enum_rule_and_not_the_string_one(self, model_root: Path) -> None:
        """Non-vacuity for the row above: the enum branch still refuses its own way."""
        _interface(model_root, "SwitchState", "MAYBE")

        assert _rules(model_root) == ["interface.init-unknown-enum-literal"]


class TestTheBoolAsymmetryIsKeptByDecision:
    """A `bool` is refused by a bounded value type only, by decision rather than omission.

    `_is_init_number` excludes `bool` on purpose (`init: ON` loads as `True`), so
    a bound cannot be applied to one: a bounded value type refuses it as
    `interface.init-shape-mismatch`, the unbounded value types `uint8` and
    `Counter` accept it. The emitter writes a `bool` as `1`/`0`, so the refusal
    is about the bound. If this class goes red, the string rule reached further
    than it should.
    """

    def test_a_bounded_alias_still_refuses_it_as_a_shape_mismatch(self, model_root: Path) -> None:
        _interface(model_root, "Percent", True)

        assert _rules(model_root) == ["interface.init-shape-mismatch"]

    def test_an_unbounded_alias_still_accepts_it(self, model_root: Path) -> None:
        _interface(model_root, "Counter", True)

        assert _rules(model_root) == []

    def test_a_bare_primitive_still_accepts_it(self, model_root: Path) -> None:
        _interface(model_root, "uint8", True)

        assert _rules(model_root) == []

    def test_a_bool_is_never_the_string_rule(self, model_root: Path) -> None:
        """`bool` is not a `str`, and the string rule never treats it as one."""
        for element_type in ("Percent", "Counter", "uint8"):
            _interface(model_root, element_type, False)

            assert "interface.init-string-scalar" not in _rules(model_root), element_type


class TestAValueTooWideToRenderIsReportedRatherThanAborting:
    """An oversized integer is a FINDING, not a traceback.

    `int.__repr__` is subject to `sys.get_int_max_str_digits()`, while YAML
    resolves `0x…` through `int(v, 16)`, which CPython exempts from that limit.
    So a hand-authored `init: 0x<4000 f>` **loads**, and a message spelled with a
    bare `!r` would raise while formatting the finding. Every row renders the
    value through `rendering.render_model_scalar`. Which rule fires on which type
    is `TestAnUnwritableIntegerIsAFinding`'s; this class adds the hand-authored
    spelling, which passes no reader guard, and the aggregate gate.

    **Does not reach** writing the value: the substitution is confined to
    messages, and a `yaml.safe_dump` or `json.dumps` of the same model still
    raises. Such a model is reported, refused by the writer, and still not
    serializable.
    """

    #: 4 000 hexadecimal digits, 4 817 decimal ones: over the 4 300-digit default
    #: and any limit an embedder is likely to set. Written as TEXT because
    #: `yaml.safe_dump` would itself raise; a hand-authored file is the path that
    #: reaches `schema.py` past both reader guards.
    WIDE = "0x" + "f" * 4000

    def _hand_written_interface(self, model_root: Path, element_type: str) -> None:
        (model_root / "interfaces").mkdir(parents=True, exist_ok=True)
        (model_root / "interfaces" / "SR_Probe.yaml").write_text(
            "name: SR_Probe\n"
            "kind: sender_receiver\n"
            "data_elements:\n"
            "  - name: Value\n"
            f"    {_type_field(model_root, element_type)}: {element_type}\n"
            f"    init: {self.WIDE}\n",
            encoding="utf-8",
            newline="\n",
        )

    @pytest.mark.parametrize(
        ("label", "element_type"),
        [
            ("a bounded alias", "Percent"),
            ("an enum", "SwitchState"),
            ("a bare primitive", "uint8"),
        ],
        ids=lambda value: value if isinstance(value, str) and " " in value else "",
    )
    def test_the_value_is_described_rather_than_reprd(self, model_root: Path, label: str, element_type: str) -> None:
        """One rule on all three declared types, and the description in every message.

        The unbounded `uint8` row shows the rule is a refusal, not only a
        rendering policy.
        """
        self._hand_written_interface(model_root, element_type)

        record = _one(model_root, "interface.init-unwritable-integer")

        assert _rules(model_root) == ["interface.init-unwritable-integer"], label
        assert "too wide to render" in record.message
        assert "16000 bits" in record.message

    def test_the_aggregate_gate_answers_instead_of_raising(self, model_root: Path) -> None:
        """`check_cross_layer_consistency`, which `check-model` calls, answers instead of raising."""
        self._hand_written_interface(model_root, "Percent")

        result = check_cross_layer_consistency(str(model_root))

        assert not result.consistent
        assert any(record.rule == "interface.init-unwritable-integer" for record in result.records)


class TestAnUnwritableIntegerIsAFinding:
    """An `int`, never a `bool`, that `str()` cannot convert on this interpreter is a finding.

    The bound is `sys.get_int_max_str_digits()`, read at run time, and `0` means
    *no limit*: the bound is on the one operation the emitter performs,
    `str(value)`. Detection is that operation, never a digit count (counting the
    digits of such a value is the conversion that raises), which is also why the
    message names the **bit length**, through `rendering.render_model_scalar`.

    **Does not reach** `enum_literal.value` and `core.hw_id`, the other two
    integer fields with no validator, which stay unbounded. A `bool` is outside
    the rule by type, although CPython makes it an `int`.
    """

    @pytest.fixture(autouse=True)
    def _requires_a_limit(self) -> None:
        if not sys.get_int_max_str_digits():
            pytest.skip("this interpreter has no integer-to-string limit, so nothing is unwritable")

    def _hex(self) -> str:
        """A hexadecimal spelling above this interpreter's limit, derived from it.

        One hexadecimal digit is about 1.2 decimal ones, so `limit` hexadecimal
        digits clear a `limit`-digit bound with margin; the figure is read from
        `sys.get_int_max_str_digits()`, never a literal.
        """
        return "0x" + "f" * sys.get_int_max_str_digits()

    def _value(self) -> int:
        return int(self._hex(), 16)

    def _hand_written(self, model_root: Path, element_type: str, init: str) -> None:
        """Write the element file as TEXT, because `yaml.safe_dump` cannot write this value.

        A hand-authored `0x…` spelling is the only way such a value reaches
        `schema.py`: YAML resolves it through `int(v, 16)`, which CPython exempts
        from the limit.
        """
        (model_root / "interfaces").mkdir(parents=True, exist_ok=True)
        (model_root / "interfaces" / "SR_Probe.yaml").write_text(
            f"name: SR_Probe\nkind: sender_receiver\ndata_elements:\n  - name: Value\n"
            f"    {_type_field(model_root, element_type)}: {element_type}\n{init}",
            encoding="utf-8",
            newline="\n",
        )

    @pytest.mark.parametrize(
        ("label", "element_type"),
        [
            ("a bare primitive", "uint8"),
            ("an unbounded alias", "Counter"),
            ("a bounded alias", "Percent"),
            ("an enum, on the value handed to the literal branch", "SwitchState"),
        ],
        ids=lambda value: value if isinstance(value, str) and " " in value else "",
    )
    def test_it_is_one_finding_whatever_the_declared_type(
        self, model_root: Path, label: str, element_type: str
    ) -> None:
        self._hand_written(model_root, element_type, f"    init: {self._hex()}\n")

        assert _rules(model_root) == ["interface.init-unwritable-integer"], label

    def test_at_depth_zero_the_message_names_the_bit_length_and_the_limit(self, model_root: Path) -> None:
        self._hand_written(model_root, "uint8", f"    init: {self._hex()}\n")

        record = _one(model_root, "interface.init-unwritable-integer")

        assert f"{self._value().bit_length()} bits" in record.message
        assert str(sys.get_int_max_str_digits()) in record.message

    def test_the_message_never_carries_a_digit_count(self, model_root: Path) -> None:
        """The message carries no digit count, whose computation is the conversion that raises.

        Asserted through what the message does say, because the decimal digit
        count is not computable here either.
        """
        self._hand_written(model_root, "uint8", f"    init: {self._hex()}\n")

        record = _one(model_root, "interface.init-unwritable-integer")

        assert "digit" in record.message, "the interpreter's limit is a digit limit and is named as one"
        assert "too wide to render" in record.message
        assert self._hex()[:32] not in record.message, "the value itself is never spelled back"

    def test_inside_a_list_it_is_reached(self, model_root: Path) -> None:
        self._hand_written(model_root, "Triple", f"    init: [1, {self._hex()}, 3]\n")

        assert _rules(model_root) == ["interface.init-unwritable-integer"]
        assert "init[1]" in _one(model_root, "interface.init-unwritable-integer").message

    def test_inside_a_nested_map_it_is_reached(self, model_root: Path) -> None:
        self._hand_written(
            model_root,
            "Nested",
            f"    init:\n      where: {{x: 1, y: {self._hex()}}}\n      load: 12.5\n",
        )

        assert _rules(model_root) == ["interface.init-unwritable-integer"]
        assert "init.where.y" in _one(model_root, "interface.init-unwritable-integer").message

    def test_a_writable_integer_reports_nothing(self, model_root: Path) -> None:
        """Non-vacuity: the rule is about the limit, not about magnitude."""
        _interface(model_root, "Counter", 10**100)

        assert _rules(model_root) == []

    def test_a_bool_is_never_measured(self, model_root: Path) -> None:
        """`bool` is an `int` subclass, and the rule never measures it."""
        _interface(model_root, "uint8", True)

        assert _rules(model_root) == []

    def test_with_the_limit_disabled_nothing_is_reported(self, model_root: Path) -> None:
        """`sys.get_int_max_str_digits() == 0` is *no limit*, so `str()` never raises.

        A real reconfiguration through `sys.set_int_max_str_digits`, restored in a
        `finally`; the assertion before the switch keeps it from passing vacuously.
        """
        self._hand_written(model_root, "uint8", f"    init: {self._hex()}\n")
        assert _rules(model_root) == ["interface.init-unwritable-integer"], "non-vacuity, before the switch"

        previous = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(0)
        try:
            assert _rules(model_root) == []
        finally:
            sys.set_int_max_str_digits(previous)


class TestTheRuleChainRefusesAnUnmappedViolation:
    """`check_init_conformance`'s rule chain ends in a raise, not a catch-all.

    `_InitViolation.rule` is typed `str`, so an unmapped rule (or a typo in a
    helper) would otherwise be emitted under the last branch's identifier
    unnoticed: `CONSISTENCY_RULES` set equality and `binding.json`'s honesty scan
    both still pass.
    """

    def test_a_violation_carrying_an_unknown_rule_raises_rather_than_mislabelling(self, model_root: Path) -> None:
        _interface(model_root, "Percent", -0.5)
        real = validator.init_values._init_violations

        def _one_unknown_violation(*args: Any, **kwargs: Any) -> list[Any]:
            violations = real(*args, **kwargs)
            return [dataclasses.replace(v, rule="interface.init-invented") for v in violations]

        with mock.patch.object(validator.init_values, "_init_violations", _one_unknown_violation):
            with pytest.raises(AssertionError, match="unmapped init violation rule"):
                check_init_conformance(str(model_root))

    def test_the_same_model_is_reported_normally_without_the_patch(self, model_root: Path) -> None:
        """Non-vacuity: the raise is the patch's doing and not the model's."""
        _interface(model_root, "Percent", -0.5)

        assert _rules(model_root) == ["interface.init-out-of-range"]


# ---------------------------------------------------------------------------
# 3. No double report
# ---------------------------------------------------------------------------


class TestAnUnresolvableTypeIsReportedOnce:
    """An init finding on top of a broken reference would send two repairs.

    `check_data_type_references` already reports the reference; this pass
    abstains everywhere the resolution it would need is the thing that is
    missing.
    """

    def test_an_undeclared_element_type_yields_no_init_finding(self, model_root: Path) -> None:
        _interface(model_root, "NoSuchType", {"x": 1})

        assert check_init_conformance(str(model_root)).records == ()

    def test_the_broken_reference_is_still_reported_by_its_own_check(self, model_root: Path) -> None:
        """The other half: abstaining here must not make the model pass."""
        _interface(model_root, "NoSuchType", {"x": 1})

        rules = [record.rule for record in validator.check_data_type_references(str(model_root)).records]
        assert "interface.data-element-undeclared-type" in rules

    def test_an_alias_over_a_non_primitive_yields_no_init_finding(self, model_root: Path) -> None:
        """A value type over a value type yields no init finding.

        A `base` resolves in `base_types` alone, so `Percent` is undeclared there
        and `data-type.value-type-undeclared-base` owns it; the base's shape is
        unknowable, and `data-type.value-type-base-not-base-type` is reported by
        nobody.
        """
        _write(model_root, "data_types", "OverAlias", {"name": "OverAlias", "kind": "value_type", "base": "Percent"})
        _interface(model_root, "OverAlias", [1, 2, 3])

        assert check_init_conformance(str(model_root)).records == ()
        rules = [record.rule for record in validator.check_data_type_references(str(model_root)).records]
        assert "data-type.value-type-undeclared-base" in rules
        assert "data-type.value-type-base-not-base-type" not in rules

    def test_an_alias_over_an_undeclared_base_yields_no_init_finding(self, model_root: Path) -> None:
        _write(model_root, "data_types", "Orphan", {"name": "Orphan", "kind": "value_type", "base": "NoSuchType"})
        _interface(model_root, "Orphan", [1, 2, 3])

        assert check_init_conformance(str(model_root)).records == ()

    def test_an_undeclared_record_field_type_yields_no_finding_for_that_field(self, model_root: Path) -> None:
        _write(
            model_root,
            "data_types",
            "HalfKnown",
            {"name": "HalfKnown", "kind": "record", "fields": [{"name": "a", "type": "NoSuchType"}]},
        )
        _interface(model_root, "HalfKnown", {"a": [1, 2]})

        assert check_init_conformance(str(model_root)).records == ()

    def test_an_undeclared_array_element_type_yields_no_finding_for_the_members(self, model_root: Path) -> None:
        """The array's own length rule still applies — only the members are unjudged."""
        _write(
            model_root,
            "data_types",
            "OrphanArray",
            {"name": "OrphanArray", "kind": "array", "element_type": "NoSuchType", "length": 2},
        )
        _interface(model_root, "OrphanArray", [{"a": 1}, {"b": 2}])

        assert check_init_conformance(str(model_root)).records == ()

    def test_the_length_rule_still_applies_to_an_array_of_an_undeclared_type(self, model_root: Path) -> None:
        _write(
            model_root,
            "data_types",
            "OrphanArray",
            {"name": "OrphanArray", "kind": "array", "element_type": "NoSuchType", "length": 2},
        )
        _interface(model_root, "OrphanArray", [{"a": 1}])

        assert _rules(model_root) == ["interface.init-array-length-mismatch"]

    def test_a_type_cycle_terminates_instead_of_recursing(self, model_root: Path) -> None:
        """`data-type.cycle` owns it; the walk must not hang or overflow.

        The record is its own field's type, so a walk with no guard would
        descend for as long as the value nests. The assertion is that the pass
        returns at all, with nothing reported for the level it refuses to
        re-enter.
        """
        _write(
            model_root,
            "data_types",
            "SelfRecord",
            {"name": "SelfRecord", "kind": "record", "fields": [{"name": "inner", "type": "SelfRecord"}]},
        )
        _interface(model_root, "SelfRecord", {"inner": {"inner": 1}})

        assert check_init_conformance(str(model_root)).records == ()


# ---------------------------------------------------------------------------
# 4. The address the protocol is answered with
# ---------------------------------------------------------------------------


class TestTheSubjectsNameWhereTheRepairGoes:
    """Every record names the element's own `init`, and the declaration that refused it.

    Two subjects because both are legal repairs: change the value, or change the
    declaration it is judged against. The element's `init` is first — it is the
    one the author almost always means — and an address carries no ordinal, so a
    finding deep inside a nested value still addresses `init` itself and says
    *where* in its message.
    """

    def test_the_first_subject_is_the_elements_init_field(self, model_root: Path) -> None:
        _interface(model_root, "Triple", [1, 2])

        assert _one(model_root, "interface.init-array-length-mismatch").subjects[0] == _init_subject()

    def test_a_nested_failure_still_addresses_the_elements_init(self, model_root: Path) -> None:
        _interface(model_root, "PointPair", [{"x": 1, "y": 2}, {"x": 3}])

        assert _one(model_root, "interface.init-record-field-mismatch").subjects[0] == _init_subject()

    @pytest.mark.parametrize(
        ("element_type", "init", "rule", "declaration", "step"),
        [
            ("uint8", [1], "interface.init-shape-mismatch", "uint8", None),
            ("Percent", 400.0, "interface.init-out-of-range", "Percent", "value_max"),
            ("Percent", -1.0, "interface.init-out-of-range", "Percent", "value_min"),
            ("SwitchState", "MAYBE", "interface.init-unknown-enum-literal", "SwitchState", "literals"),
            ("Point", {"x": 1}, "interface.init-record-field-mismatch", "Point", "fields"),
            ("Triple", [1], "interface.init-array-length-mismatch", "Triple", "length"),
        ],
        ids=["shape", "above-max", "below-min", "literal", "record", "array"],
    )
    def test_the_second_subject_is_the_declaration_that_refused_the_value(
        self,
        model_root: Path,
        element_type: str,
        init: Any,
        rule: str,
        declaration: str,
        step: str | None,
    ) -> None:
        _interface(model_root, element_type, init)

        record = _one(model_root, rule)
        expected = () if step is None else (field_step(step),)
        layer = (
            "application_types"
            if _type_field(model_root, declaration) == "application_type"
            else "implementation_types"
        )
        assert record.subjects[1] == Subject(layer, declaration, expected)

    def test_a_nested_failure_names_the_nested_declaration(self, model_root: Path) -> None:
        """Not the element's own type: the repair is on `Point`, not on `PointPair`."""
        _interface(model_root, "PointPair", [{"x": 1, "y": 2}, {"x": 3}])

        record = _one(model_root, "interface.init-record-field-mismatch")
        assert record.subjects[1] == Subject("application_types", "Point", (field_step("fields"),))

    def test_every_record_states_at_least_one_subject(self, model_root: Path) -> None:
        _interface(model_root, "PointPair", [{"x": 1}, 4])

        records = _records(model_root)
        assert records
        assert all(record.subjects for record in records)

    def test_no_record_carries_a_reference_target(self, model_root: Path) -> None:
        """`target` is the identity that does not resolve, and here every one does.

        Setting it would group these findings with the broken-reference repairs
        the protocol answers separately.
        """
        _interface(model_root, "Triple", [1, 2])

        assert all(record.target is None for record in _records(model_root))


# ---------------------------------------------------------------------------
# 5. The three wiring sites
# ---------------------------------------------------------------------------


class TestTheRuleIsWiredAtAllThreeSites:
    """A rule minted in a function nothing composes is a rule nothing reports.

    The three sites are the composed tuple inside `check_cross_layer_consistency`
    (a check outside it runs nowhere), `validator.CONSISTENCY_RULES` (the closed
    inventory the contract adopts) and `editing/diagnosis.py`'s `RULE_CODES`
    (the protocol code a client is answered with).
    `packages/tools/tests/test_validator_rule_coverage.py` holds the last two against each
    other in both directions across the whole inventory; these eight are pinned
    by name here so a failure says *which* rule is unwired.
    """

    @pytest.mark.parametrize("rule", INIT_RULES)
    def test_it_is_in_the_closed_inventory(self, rule: str) -> None:
        assert rule in CONSISTENCY_RULES
        assert CONSISTENCY_RULES[rule].strip()

    @pytest.mark.parametrize("rule", INIT_RULES)
    def test_it_is_answered_with_a_consistency_violation(self, rule: str) -> None:
        """Every reference the rule reads resolves; what fails is a rule over the result.

        A `broken-reference` here would tell a client to repair an identity that
        is not broken — the type resolving is the precondition this pass abstains
        without.
        """
        assert RULE_CODES[rule].code == CONSISTENCY_VIOLATION

    def test_the_check_is_in_the_composed_tuple(self) -> None:
        """Read from the composer's own body and the phases it calls, never restated as a list."""
        composed = validator_package.composed_checks()
        assert "check_init_conformance" in composed, composed

    def test_the_aggregate_reports_what_the_pass_reports(self, model_root: Path) -> None:
        """The registration, executed: a broken init reaches the composed answer.

        The model here carries two layers only, so the aggregate reports a great
        deal besides; this asserts membership of *this* rule and nothing about
        the rest, which is `test_validator.py`'s.
        """
        _interface(model_root, "Triple", [1, 2])

        rules = {record.rule for record in validator.check_cross_layer_consistency(str(model_root)).records}
        assert "interface.init-array-length-mismatch" in rules
