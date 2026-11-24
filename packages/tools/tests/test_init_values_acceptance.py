"""The acceptance suite for `init`: the edge cases, the same project through the writer, the additive delta.

1. **The edge cases pass the shipped gate.** An element without `init`; a one-field record; an array of
   length 1; a quoted `'ON'` enum literal; a literal named like a number; a value type bound exactly at
   `value_min`. A quoted `'010'` string value is a negative vector (`rule:interface.init-string-scalar`).
   They run through `check_cross_layer_consistency`, the call `swcraft check-model` makes, on a real
   project copied from `examples/basic`.
2. **The same project is generated, and every value comes back**, read through the call sequence
   `import_component()` makes: the `bool` emitted as `1`, the two floats that keep their point and their
   type at depth 0 and inside a record, the element without `init` gaining no constant, and the two *name*
   rules of the eighth generated form (the joint budget, and two pairs deriving one constant name) through
   both surfaces.
3. **The ARXML delta of a value is exactly the nodes that carry it.** The valued document, with every
   `CONSTANT-SPECIFICATION` element and every `INIT-VALUE` subtree stripped and re-serialized by the
   writer's own serializer, is **byte-identical** to the value-less one, at 4.2.2 and at 4.3.0. The
   project declaring no `init` emits neither new tag, and its 4.2.2 document is still the committed golden.

What it does not reach: `generate diagrams` and `generate needs` (the coverage oracle witnesses that no
`data_element` reaches them); schema validity of the added nodes; an unbounded value type's numeric domain
(`init: 999` on `uint8` is admitted); a value the exporter cannot spell is refused (`init: '010'` is
`interface.init-string-scalar`, an integer above `sys.get_int_max_str_digits()` is
`interface.init-unwritable-integer`), but the *unquoted* `init: 010` loads as the integer `8` and passes;
projects other than this one.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT, golden_slice, model_element_path, read_golden
from lxml import etree
from swcraft_tools.cli import main
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH
from swcraft_tools.schema import SRInterface
from swcraft_tools.validator import check_cross_layer_consistency, check_identifier_conformance
from swcraft_tools.writer.arxml_generation import (
    INIT_CONSTANT_ADDED_LENGTH,
    generate_arxml,
    init_constant_name,
)
from swcraft_tools.writer.arxml_xml import AR_NAMESPACE, serialize_document
from test_arxml_golden import TestSemanticRoundTrip as SemanticRoundTripOracle

#: The project every test here starts from: two SWCs, a task, a processor and a
#: core, one SR and one CS interface — a model that already passes
#: `check-model`, so any finding a test sees is the one it wrote.
BASIC_PROJECT = REPO_ROOT / "examples" / "basic"


def _write(model_root: Path, layer: str, stem: str, document: Any) -> None:
    """Write one element file in the canonical writer form.

    A data-type document is routed by its ``kind``: an application type
    lands under ``data_types/application/``, an implementation type under
    ``data_types/implementation/``, a mapping set under ``data_types/mapping_sets/``.
    """
    if layer == "data_types" and isinstance(document, dict) and "kind" in document:
        path = model_element_path(model_root, stem, kind=document["kind"])
    elif layer == "mapping_sets":
        path = model_element_path(model_root, stem, layer="mapping_sets")
    else:
        path = model_root / layer / f"{stem}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(document, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )


def _copy_basic(tmp_path: Path) -> Path:
    """Copy `examples/basic`'s manifest and model into *tmp_path*; return the project."""
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    shutil.copy(BASIC_PROJECT / "swcraft.yaml", project / "swcraft.yaml")
    shutil.copytree(BASIC_PROJECT / "model", project / "model")
    return project


def _declare_edge_case_types(model_root: Path) -> None:
    """The data types the edge cases need, beside `examples/basic`'s value types.

    The enum below is spoken on both components' ports, so ``[constr_1004]`` — SWCraft's
    ``rule:data-type.application-type-unmapped`` — requires a set pairing it with the
    implementation type that stores it, and both components name that set.
    """
    _write(
        model_root,
        "data_types",
        "OnOff_T",
        {
            "name": "OnOff_T",
            "kind": "enum",
            # `ON` resolves to `True` under YAML 1.1 unless it is quoted: the whole
            # of the quoting rule for an enum literal name.
            "literals": [{"name": "OFF", "value": 0}, {"name": "ON", "value": 1}],
        },
    )
    _write(
        model_root,
        "data_types",
        "Percent_T",
        {
            "name": "Percent_T",
            "kind": "value_type",
            "base": "float32_base",
            "unit": "percent",
            "value_min": 0.0,
            "value_max": 100.0,
        },
    )
    # A composite member names the physical view. Neither of
    # the two below declares a `range`, so no value in this project is bounded by one.
    _write(model_root, "data_types", "Count_P", {"name": "Count_P", "kind": "application_type"})
    _write(model_root, "data_types", "Measure_P", {"name": "Measure_P", "kind": "application_type"})
    _write(
        model_root,
        "data_types",
        "OneField_T",
        {"name": "OneField_T", "kind": "record", "fields": [{"name": "only", "type": "Count_P"}]},
    )
    _write(
        model_root,
        "data_types",
        "Single_T",
        {"name": "Single_T", "kind": "array", "element_type": "Count_P", "length": 1},
    )
    # The record the two hard floats sit inside, so each of them is measured at depth 0
    # *and* one level down in the same project.
    _write(
        model_root,
        "data_types",
        "FloatPair_T",
        {
            "name": "FloatPair_T",
            "kind": "record",
            "fields": [{"name": "zero", "type": "Measure_P"}, {"name": "huge", "type": "Measure_P"}],
        },
    )
    _write(
        model_root,
        "mapping_sets",
        "PhysicalTypes",
        {
            "name": "PhysicalTypes",
            "maps": [
                {"application": "OnOff_T", "implementation": "uint8"},
                {"application": "Count_P", "implementation": "uint8"},
                {"application": "Measure_P", "implementation": "float32"},
            ],
        },
    )
    for swc in sorted((model_root / "swc").rglob("*.yaml"), key=lambda path: path.as_posix()):
        document = yaml.safe_load(swc.read_text(encoding="utf-8"))
        document["data_type_mappings"] = ["PhysicalTypes"]
        swc.write_text(
            yaml.safe_dump(document, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
            newline="\n",
        )


#: The edge cases, one data element each, written into `examples/basic`'s
#: own `IExample` so every element stays reachable from a real port.
EDGE_CASE_ELEMENTS: list[dict[str, Any]] = [
    # `examples/basic`'s own element, untouched.
    {"name": "value", "implementation_type": "float32"},
    # 1. an element without `init` at all.
    {"name": "plain", "implementation_type": "uint8"},
    # 2. a one-field record.
    {"name": "single_field", "application_type": "OneField_T", "init": {"only": 3}},
    # 3. an array of length 1.
    {"name": "one_element", "application_type": "Single_T", "init": [9]},
    # 4. a quoted `'ON'` enum literal.
    {"name": "lamp", "application_type": "OnOff_T", "init": "ON"},
    # 5. a quoted `'010'` string value is a NEGATIVE vector:
    #    `test_each_edge_case_is_load_bearing`'s string row.
    # 6. a value type bound exactly at `value_min`.
    {"name": "floor", "implementation_type": "Percent_T", "init": 0.0},
    # The four below are values the *writer* has to spell.
    # 7. a `bool` on a value type — accepted here, emitted as `1`, and lossy on
    #    the way back (`True` returns as the int 1).
    {"name": "flag", "implementation_type": "bool", "init": True},
    # 8. and 9. the two floats `format_init_number` appends `.0` for, at depth 0.
    {"name": "zero", "implementation_type": "float32", "init": 0.0},
    {"name": "huge", "implementation_type": "float32", "init": 1e20},
    # 10. the same two, one level down, so the type claim is made in both positions.
    {"name": "pair", "application_type": "FloatPair_T", "init": {"zero": 0.0, "huge": 1e20}},
]


def _write_edge_case_interface(model_root: Path, elements: list[dict[str, Any]]) -> None:
    _write(
        model_root,
        "interfaces",
        "IExample",
        {"name": "IExample", "kind": "sender_receiver", "data_elements": elements},
    )


@pytest.fixture
def edge_case_project(tmp_path: Path) -> Path:
    """`examples/basic` plus the five types and every edge-case element."""
    project = _copy_basic(tmp_path)
    model_root = project / "model"
    _declare_edge_case_types(model_root)
    _write_edge_case_interface(model_root, EDGE_CASE_ELEMENTS)
    return project


# 1. The edge cases through the shipped gate


class TestTheEdgeCasesPassCheckModel:
    """Every edge case, in one project, through `check-model`."""

    def test_the_shipped_command_exits_zero_and_writes_nothing(self, edge_case_project: Path, monkeypatch) -> None:
        """The command itself, not the pass — and `check-model` writes nothing."""
        monkeypatch.setattr(
            "sys.argv", ["swcraft", "check-model", "--project", str(edge_case_project / "swcraft.yaml")]
        )

        try:
            main()
        except SystemExit as exc:  # pragma: no cover - the failure path is the message
            result = check_cross_layer_consistency(str(edge_case_project / "model"))
            pytest.fail(f"check-model exited {exc.code} on the edge-case project: {result.issues}")

        assert not (edge_case_project / "dist").exists()
        assert not (edge_case_project / "views").exists()

    def test_no_init_rule_fires_on_any_of_them(self, edge_case_project: Path) -> None:
        """The aggregate is consistent *and* mints no `interface.init-*` record.

        Exit 0 alone would not say this: a project with no init at all exits 0 too.
        """
        result = check_cross_layer_consistency(str(edge_case_project / "model"))

        assert result.consistent, result.issues
        assert [record.rule for record in result.records if record.rule.startswith("interface.init-")] == []

    def test_the_loader_kept_every_edge_case_as_written(self, edge_case_project: Path) -> None:
        """Non-vacuity: each value arrives as the *type* its case is about.

        `'ON'` arriving as `True` would pass the enum rule just as quietly.
        """
        document = yaml.safe_load(
            (edge_case_project / "model" / "interfaces" / "IExample.yaml").read_text(encoding="utf-8")
        )
        interface = SRInterface.model_validate(document)
        by_name = {element.name: element for element in interface.data_elements}

        assert by_name["plain"].init is None
        assert "init" not in {
            key for element in document["data_elements"] if element["name"] == "plain" for key in element
        }
        assert by_name["single_field"].init == {"only": 3}
        assert by_name["one_element"].init == [9]
        assert by_name["lamp"].init == "ON"
        assert type(by_name["lamp"].init) is str
        assert by_name["floor"].init == 0.0
        # The writer's four. `flag` is the one that would pass the primitive rule
        # just as quietly if YAML had handed over the string `"true"`, and the
        # three float rows would pass it as ints.
        assert by_name["flag"].init is True
        assert type(by_name["flag"].init) is bool
        assert by_name["zero"].init == 0.0
        assert type(by_name["zero"].init) is float
        assert by_name["huge"].init == 1e20
        assert type(by_name["huge"].init) is float
        assert by_name["pair"].init == {"zero": 0.0, "huge": 1e20}
        assert [type(leaf) for leaf in by_name["pair"].init.values()] == [float, float]

    def test_the_alias_bound_really_is_the_value_the_element_carries(self, edge_case_project: Path) -> None:
        """`floor` sits *at* `value_min`, read from the declaration rather than assumed."""
        declaration = yaml.safe_load(
            (model_element_path(edge_case_project / "model", "Percent_T", kind="value_type")).read_text(
                encoding="utf-8"
            )
        )
        document = yaml.safe_load(
            (edge_case_project / "model" / "interfaces" / "IExample.yaml").read_text(encoding="utf-8")
        )
        element = next(candidate for candidate in document["data_elements"] if candidate["name"] == "floor")

        assert element["init"] == declaration["value_min"]

    @pytest.mark.parametrize(
        ("case", "name", "init", "rule"),
        [
            (
                "a one-field record with a second key",
                "single_field",
                {"only": 3, "extra": 1},
                "interface.init-record-field-mismatch",
            ),
            ("a one-field record with none", "single_field", {}, "interface.init-record-field-mismatch"),
            ("an array of length 1 given two", "one_element", [9, 9], "interface.init-array-length-mismatch"),
            ("a value just below value_min", "floor", -0.001, "interface.init-out-of-range"),
            ("an enum literal that is not declared", "lamp", "MAYBE", "interface.init-unknown-enum-literal"),
            ("a map where a primitive is declared", "plain", {"a": 1}, "interface.init-shape-mismatch"),
            ("a quoted numeric string on a primitive", "plain", "010", "interface.init-string-scalar"),
            ("an unquoted alphabetic string on a primitive", "plain", "abc", "interface.init-string-scalar"),
            (
                "a string under an alias that declares a bound",
                "floor",
                "0.0",
                "interface.init-string-scalar",
            ),
            # The writer's four, each one step from its acceptance.
            ("a bool spelled as a string", "flag", "true", "interface.init-string-scalar"),
            ("a float given as a map", "zero", {"a": 0.0}, "interface.init-shape-mismatch"),
            ("a float given as a list", "huge", [1e20], "interface.init-shape-mismatch"),
            (
                "a two-field record given one field",
                "pair",
                {"zero": 0.0},
                "interface.init-record-field-mismatch",
            ),
        ],
        ids=lambda value: value if isinstance(value, str) and " " in value else "",
    )
    def test_each_edge_case_is_load_bearing(
        self, edge_case_project: Path, case: str, name: str, init: Any, rule: str
    ) -> None:
        """Perturb one edge case at a time; the aggregate reports exactly its rule.

        This is what makes the green above a measurement: each accepted value sits one step
        from a refusal.
        """
        elements = [dict(element) for element in EDGE_CASE_ELEMENTS]
        for element in elements:
            if element["name"] == name:
                element["init"] = init
        _write_edge_case_interface(edge_case_project / "model", elements)

        result = check_cross_layer_consistency(str(edge_case_project / "model"))

        assert not result.consistent
        assert [record.rule for record in result.records if record.rule.startswith("interface.init-")] == [rule]

    def test_the_unquoted_spellings_are_the_ones_the_quoting_rule_is_about(self) -> None:
        """The YAML quoting rule, measured on the loader."""
        assert yaml.safe_load("init: ON")["init"] is True
        assert yaml.safe_load("init: '010'")["init"] == "010"
        assert yaml.safe_load("init: 010")["init"] == 8

    def test_the_two_spellings_of_010_now_answer_differently(self, edge_case_project: Path) -> None:
        """The quoted spelling is refused, the unquoted one still passes: the limit that remains.

        The **quoted** `'010'` is `interface.init-string-scalar` — the type references no table,
        so it carries a number — while the **unquoted** `010` loads as `8`, a number the exporter
        writes perfectly well, so the gate cannot see that an author who dropped the quotes meant
        `010`.
        """
        path = edge_case_project / "model" / "interfaces" / "IExample.yaml"
        elements = [
            *[dict(element) for element in EDGE_CASE_ELEMENTS],
            {"name": "code", "implementation_type": "uint8", "init": "010"},
        ]
        _write_edge_case_interface(edge_case_project / "model", elements)

        quoted = check_cross_layer_consistency(str(edge_case_project / "model"))

        assert [record.rule for record in quoted.records if record.rule.startswith("interface.init-")] == [
            "interface.init-string-scalar"
        ]

        path.write_text(
            path.read_text(encoding="utf-8").replace("init: '010'", "init: 010"), encoding="utf-8", newline="\n"
        )
        unquoted = check_cross_layer_consistency(str(edge_case_project / "model"))
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        element = next(candidate for candidate in document["data_elements"] if candidate["name"] == "code")

        assert unquoted.consistent, unquoted.issues
        assert element["init"] == 8

    def test_a_literal_named_like_a_number_is_refused_where_it_is_declared(self, edge_case_project: Path) -> None:
        """The sixth edge case, and it is refused one layer earlier than the init rules.

        A literal name that YAML 1.1 resolves to a *number* has to begin with a digit, and an
        AUTOSAR `Identifier` may not (`identifier.py`'s `[a-zA-Z][a-zA-Z0-9_]*`), so it is an
        `identifier.declared` finding on the **declaration**. The reachable half of the quoting
        rule for an enum literal is therefore the boolean and null family (`ON`, `NO`, `OFF`,
        `YES`), which is why `lamp` above carries the quoting rule and this case shows its limit.
        """
        model_root = edge_case_project / "model"
        _write(
            model_root,
            "data_types",
            "Coded_T",
            {"name": "Coded_T", "kind": "enum", "literals": [{"name": "010", "value": 10}]},
        )
        elements = [
            *[dict(element) for element in EDGE_CASE_ELEMENTS],
            {"name": "coded", "application_type": "Coded_T", "init": "010"},
        ]
        _write_edge_case_interface(model_root, elements)

        identifiers = check_identifier_conformance(str(model_root))
        aggregate = check_cross_layer_consistency(str(model_root))

        assert [record.rule for record in identifiers.records] == ["identifier.declared"]
        assert "010" in identifiers.records[0].message
        assert [record.rule for record in aggregate.records if record.rule.startswith("interface.init-")] == []


# 2. The same project through the writer


#: What the eighth generated form leaves the two declared names *between them* —
#: derived from the writer's own two constants, never restated as 122, so a
#: change to either moves this with it.
JOINT_BUDGET = MAX_IDENTIFIER_LENGTH - INIT_CONSTANT_ADDED_LENGTH

#: `examples/basic`'s own interface, the one every edge case is written into and
#: the one its two SWCs already have ports on — which is what makes the com-spec
#: references reachable without adding a component.
EDGE_CASE_INTERFACE = "IExample"


def _element_name_of_length(interface: str, total: int) -> str:
    """A legal data element name putting `len(interface) + len(element)` at *total*."""
    return "e" * (total - len(interface))


def _rules(model_root: Path, prefix: str = "") -> list[str]:
    """Every rule the shipped gate mints on *model_root*, filtered by *prefix*."""
    result = check_cross_layer_consistency(str(model_root))
    return [record.rule for record in result.records if record.rule.startswith(prefix)]


def _writer_refusal(project: Path) -> str:
    """Generate *project* expecting a refusal; return the message.

    The writer batches every offender of one kind into a single `ValueError`, so what a
    caller can assert is the message, not a per-element exception.
    """
    with pytest.raises(ValueError) as excinfo:
        generate_arxml(str(project / "model"), str(project / "dist" / "model.arxml"))
    return str(excinfo.value)


@pytest.fixture
def edge_case_round_trip(edge_case_project: Path, tmp_path: Path):
    """The edge-case project generated and read straight back in.

    `TestSemanticRoundTrip._round_trip`, imported under a non-`Test` alias so pytest does not
    collect that class a second time, is the call sequence `import_component()` makes, reused
    rather than copied so this project and the golden roots cannot drift apart.
    """
    output = tmp_path / "round-trip.arxml"
    generate_arxml(str(edge_case_project / "model"), str(output))
    return SemanticRoundTripOracle._round_trip(str(output))


class TestTheEdgeCaseProjectIsWrittenAndComesBack:
    """The same project as section 1, through `generate_arxml` and back.

    Section 1 asks the gate whether the values are *legal*; this asks the writer whether it can
    *spell* them, and the reader whether they survive: a value the gate accepts is one the
    writer writes, and the refusals below are refused by both.

    **What it does not claim:** that the emitted bytes are schema-valid
    (`test_arxml_xsd_conformance.py`), or that each fragment is the exact derived shape
    (`test_arxml_init_writer.py`'s frozen fragments).
    """

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_every_valued_element_gets_its_constant_and_the_bare_one_gets_none(
        self, edge_case_project: Path, version: str
    ) -> None:
        text = _generate(edge_case_project, version).decode("utf-8")
        valued = [element for element in EDGE_CASE_ELEMENTS if "init" in element]

        assert valued, "the project would prove nothing if no element carried a value"
        for element in valued:
            assert f"<SHORT-NAME>{init_constant_name(EDGE_CASE_INTERFACE, element['name'])}</SHORT-NAME>" in text
        assert init_constant_name(EDGE_CASE_INTERFACE, "plain") not in text
        assert init_constant_name(EDGE_CASE_INTERFACE, "value") not in text

    def test_the_bool_is_emitted_as_the_number_one(self, edge_case_project: Path) -> None:
        """A `bool` is `1`, never `true` — the schema would take either."""
        fragment = golden_slice(
            _generate(edge_case_project, "4.2.2").decode("utf-8"),
            init_constant_name(EDGE_CASE_INTERFACE, "flag"),
        )

        assert "<VALUE>1</VALUE>" in fragment
        assert "true" not in fragment

    @pytest.mark.parametrize(
        ("element", "value"),
        [("zero", "0.0"), ("huge", "100000000000000000000.0")],
        ids=["0.0", "1e20"],
    )
    def test_a_hard_float_keeps_its_point_at_depth_zero(
        self, edge_case_project: Path, element: str, value: str
    ) -> None:
        """The two rows `format_init_number` appends `.0` for.

        Without it the reader's decimal-first grammar hands both back as `int`s.
        """
        fragment = golden_slice(
            _generate(edge_case_project, "4.2.2").decode("utf-8"),
            init_constant_name(EDGE_CASE_INTERFACE, element),
        )

        assert f"<VALUE>{value}</VALUE>" in fragment

    def test_the_same_two_floats_keep_it_inside_a_record(self, edge_case_project: Path) -> None:
        """Both of them one level down, in declared field order and labelled."""
        fragment = golden_slice(
            _generate(edge_case_project, "4.2.2").decode("utf-8"),
            init_constant_name(EDGE_CASE_INTERFACE, "pair"),
        )

        members = [line.strip() for line in fragment.splitlines() if "<SHORT-LABEL>" in line or "<VALUE>" in line]

        assert members == [
            "<SHORT-LABEL>zero</SHORT-LABEL>",
            "<VALUE>0.0</VALUE>",
            "<SHORT-LABEL>huge</SHORT-LABEL>",
            "<VALUE>100000000000000000000.0</VALUE>",
        ]

    def test_nothing_is_skipped_on_the_way_back_in(self, edge_case_round_trip) -> None:
        """Necessary before any comparison below: a skipped interface would make every `init` assertion vacuous."""
        _, _, _, _, skips, _ = edge_case_round_trip

        assert [f"{entry.tag} at {entry.path}: {entry.reason}" for entry in skips] == []

    def test_every_value_comes_back_equal_and_typed(self, edge_case_round_trip) -> None:
        """The typed round trip over *this* project's edge cases.

        `flag` is the one lossy row, asserted as its outcome by the test below, never exempted here.
        """
        _, interfaces, _, _, _, _ = edge_case_round_trip
        interface = next(candidate for candidate in interfaces if candidate.name == EDGE_CASE_INTERFACE)
        returned = {element.name: element.init for element in interface.data_elements}
        expected = {element["name"]: element.get("init") for element in EDGE_CASE_ELEMENTS}
        expected["flag"] = 1

        assert returned == expected
        assert type(returned["zero"]) is float
        assert type(returned["huge"]) is float
        assert [type(leaf) for leaf in returned["pair"].values()] == [float, float]
        assert type(returned["lamp"]) is str
        assert returned["plain"] is None

    def test_the_bool_comes_back_as_the_int_one(self, edge_case_round_trip) -> None:
        """The `bool`'s lossy row is asserted as its outcome: it comes back as the int one.

        `True == 1` in Python, so a value comparison alone would hide it. The reader does not resolve the base type and
        nothing can restore the `bool`.
        """
        _, interfaces, _, _, _, _ = edge_case_round_trip
        interface = next(candidate for candidate in interfaces if candidate.name == EDGE_CASE_INTERFACE)
        returned = {element.name: element.init for element in interface.data_elements}

        assert returned["flag"] == 1
        assert type(returned["flag"]) is int


class TestTheTwoNameRulesAgreeAcrossBothSurfaces:
    """The eighth generated form's joint budget and its collision, on one project.

    Both are *name* rules, so neither needs a value to fire. What is asserted is the agreement:
    `check-model` reports and `generate_arxml` refuses the same offender, and one character or
    one rename either side of the boundary flips both together. The per-guard messages are the
    unit suites' (`test_validator_identifier.py`, `test_validator_name_uniqueness.py`,
    `test_arxml_init_writer.py`).
    """

    @staticmethod
    def _rewrite(project: Path, elements: list[dict[str, Any]], second: dict[str, Any] | None = None) -> None:
        _write_edge_case_interface(project / "model", elements)
        if second is not None:
            _write(project / "model", "interfaces", second["name"], second)

    def test_a_pair_exactly_on_the_joint_budget_is_accepted_by_both(self, edge_case_project: Path) -> None:
        name = _element_name_of_length(EDGE_CASE_INTERFACE, JOINT_BUDGET)
        self._rewrite(
            edge_case_project, [*EDGE_CASE_ELEMENTS, {"name": name, "implementation_type": "uint8", "init": 1}]
        )

        assert len(EDGE_CASE_INTERFACE) + len(name) == JOINT_BUDGET
        assert len(init_constant_name(EDGE_CASE_INTERFACE, name)) == MAX_IDENTIFIER_LENGTH
        assert _rules(edge_case_project / "model") == []

        text = _generate(edge_case_project, "4.2.2").decode("utf-8")

        assert f"<SHORT-NAME>{init_constant_name(EDGE_CASE_INTERFACE, name)}</SHORT-NAME>" in text

    def test_one_character_more_is_refused_by_both_naming_both_names(self, edge_case_project: Path) -> None:
        """The perturbation that makes the acceptance above a measurement."""
        name = _element_name_of_length(EDGE_CASE_INTERFACE, JOINT_BUDGET + 1)
        self._rewrite(
            edge_case_project, [*EDGE_CASE_ELEMENTS, {"name": name, "implementation_type": "uint8", "init": 1}]
        )

        result = check_cross_layer_consistency(str(edge_case_project / "model"))
        reported = [record for record in result.records if record.rule == "identifier.generated"]

        assert [record.rule for record in result.records] == ["identifier.generated"]
        assert EDGE_CASE_INTERFACE in reported[0].message
        assert name in reported[0].message
        assert str(JOINT_BUDGET) in reported[0].message

        message = _writer_refusal(edge_case_project)

        assert EDGE_CASE_INTERFACE in message
        assert name in message
        assert str(JOINT_BUDGET) in message
        assert not (edge_case_project / "dist" / "model.arxml").exists()

    def test_two_pairs_deriving_one_constant_are_refused_by_both_naming_it(self, edge_case_project: Path) -> None:
        """The underscore ambiguity: `IExample` + `a_b` and `IExample_a` + `b`."""
        shared = init_constant_name(EDGE_CASE_INTERFACE, "a_b")
        self._rewrite(
            edge_case_project,
            [*EDGE_CASE_ELEMENTS, {"name": "a_b", "implementation_type": "uint8", "init": 1}],
            second={
                "name": f"{EDGE_CASE_INTERFACE}_a",
                "kind": "sender_receiver",
                "data_elements": [{"name": "b", "implementation_type": "uint8", "init": 2}],
            },
        )

        assert shared == init_constant_name(f"{EDGE_CASE_INTERFACE}_a", "b")

        result = check_cross_layer_consistency(str(edge_case_project / "model"))
        reported = [record for record in result.records if record.rule == "interface.init-constant-name-collision"]

        assert [record.rule for record in result.records] == ["interface.init-constant-name-collision"]
        assert shared in reported[0].message
        assert "a_b" in reported[0].message

        message = _writer_refusal(edge_case_project)

        assert shared in message
        assert not (edge_case_project / "dist" / "model.arxml").exists()

    def test_renaming_one_of_the_two_elements_makes_both_accept_it_again(self, edge_case_project: Path) -> None:
        """The other direction, so the refusal above is about the collision and not a malformed model."""
        self._rewrite(
            edge_case_project,
            [*EDGE_CASE_ELEMENTS, {"name": "a_c", "implementation_type": "uint8", "init": 1}],
            second={
                "name": f"{EDGE_CASE_INTERFACE}_a",
                "kind": "sender_receiver",
                "data_elements": [{"name": "b", "implementation_type": "uint8", "init": 2}],
            },
        )

        assert _rules(edge_case_project / "model") == []

        text = _generate(edge_case_project, "4.2.2").decode("utf-8")

        assert f"<SHORT-NAME>{init_constant_name(EDGE_CASE_INTERFACE, 'a_c')}</SHORT-NAME>" in text
        assert f"<SHORT-NAME>{init_constant_name(f'{EDGE_CASE_INTERFACE}_a', 'b')}</SHORT-NAME>" in text

    def test_the_quoted_numeric_string_is_refused_by_both_on_this_project(self, edge_case_project: Path) -> None:
        """The quoted `'010'`, in the project section 1 reports it on, is refused by the writer too."""
        self._rewrite(
            edge_case_project, [*EDGE_CASE_ELEMENTS, {"name": "code", "implementation_type": "uint8", "init": "010"}]
        )

        assert _rules(edge_case_project / "model", "interface.init-") == ["interface.init-string-scalar"]

        message = _writer_refusal(edge_case_project)

        assert "code" in message
        assert "interface.init-string-scalar" in message
        assert not (edge_case_project / "dist" / "model.arxml").exists()


# 3. The delta is exactly the added nodes


#: The three elements the delta model gains an `init` on, one per shape
#: `InitValue` admits at the top level: a scalar, a map and a list.
INIT_BY_ELEMENT: dict[str, Any] = {"value": 0.0, "single_field": {"only": 3}, "one_element": [9]}

#: The elements themselves, declared in BOTH variants so the only difference
#: between the two models is the `init` key. An enum-typed element is
#: deliberately absent: `examples/basic` has none, and adding one would put a
#: `TEXTTABLE` compu-method into both documents for no gain here.
DELTA_ELEMENTS: list[dict[str, Any]] = [
    {"name": "value", "implementation_type": "float32"},
    {"name": "single_field", "application_type": "OneField_T"},
    {"name": "one_element", "application_type": "Single_T"},
]

#: The two tags a value adds to the document, and the whole of the delta it may
#: produce. `CONSTANT-SPECIFICATION` is a package-level element;
#: `INIT-VALUE` is a subtree on a `VARIABLE-DATA-PROTOTYPE` and on the two
#: non-queued com-spec kinds. Everything the values put in the document is
#: inside one of the two, which is exactly what the strip below measures.
ADDED_TAGS = ("CONSTANT-SPECIFICATION", "INIT-VALUE")


def _generate(project: Path, version: str) -> bytes:
    """Generate the project's ARXML at *version* and return its bytes."""
    output = project / "dist" / "model.arxml"
    generate_arxml(str(project / "model"), str(output), version=version)
    return output.read_bytes()


def _strip_added_nodes(document: bytes) -> bytes:
    """Remove every node `init` can add and re-serialize with the writer's serializer.

    An lxml pass, never a string edit: the tree is parsed with `remove_blank_text=True` so the
    indentation becomes structure rather than text, and the result goes back through
    `serialize_document()`, the function that produced the bytes in the first place.
    """
    root = etree.fromstring(document, etree.XMLParser(remove_blank_text=True))
    doomed = [node for tag in ADDED_TAGS for node in root.iter(f"{{{AR_NAMESPACE}}}{tag}")]
    for node in doomed:
        parent = node.getparent()
        assert parent is not None, "an added node cannot be the document root"
        parent.remove(node)
    return serialize_document(root).encode("utf-8")


def _write_delta_interface(model_root: Path, valued: bool) -> None:
    elements = [dict(element) for element in DELTA_ELEMENTS]
    if valued:
        for element in elements:
            element["init"] = INIT_BY_ELEMENT[element["name"]]
    _write_edge_case_interface(model_root, elements)


class TestTheDeltaOfAValueIsExactlyTheNodesThatCarryIt:
    """The additive-delta gate, at both AUTOSAR versions.

    The difference between the two documents is *confined*: remove the added nodes and they are
    the same bytes again, so no attribute moved, no order changed and no element was
    re-serialized differently beside them. `examples/basic`'s ports name `IExample`, so the
    valued variant exercises all three insertion points — the prototype and both com-spec kinds.
    """

    @pytest.fixture
    def without_init(self, tmp_path: Path) -> Path:
        project = _copy_basic(tmp_path / "without")
        _declare_edge_case_types(project / "model")
        _write_delta_interface(project / "model", valued=False)
        return project

    @pytest.fixture
    def with_init(self, tmp_path: Path) -> Path:
        project = _copy_basic(tmp_path / "with")
        _declare_edge_case_types(project / "model")
        _write_delta_interface(project / "model", valued=True)
        return project

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_stripping_the_added_nodes_restores_the_value_less_document(
        self, without_init: Path, with_init: Path, version: str
    ) -> None:
        bare = _generate(without_init, version)
        valued = _generate(with_init, version)

        assert _strip_added_nodes(valued) == bare

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_the_two_documents_really_differ(self, without_init: Path, with_init: Path, version: str) -> None:
        """Non-vacuity: without this the strip could be comparing two equal files."""
        bare = _generate(without_init, version)
        valued = _generate(with_init, version)

        assert valued != bare
        assert len(valued) > len(bare)

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_the_strip_pass_itself_moves_no_byte(self, without_init: Path, version: str) -> None:
        """The control: parsing and re-serializing the value-less document is the identity."""
        bare = _generate(without_init, version)

        assert _strip_added_nodes(bare) == bare

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_the_valued_document_carries_all_three_insertion_points(self, with_init: Path, version: str) -> None:
        """Non-vacuity: the strip is removing prototype *and* com-spec references.

        The ports are read out of the model rather than restating a count.
        """
        text = _generate(with_init, version).decode("utf-8")
        ports = sum(
            1
            for path in sorted((with_init / "model" / "swc").rglob("*.yaml"), key=lambda p: p.as_posix())
            for port in yaml.safe_load(path.read_text(encoding="utf-8")).get("ports", [])
            if port["interface"] == "IExample"
        )

        assert ports > 0
        assert text.count("<CONSTANT-SPECIFICATION>") == len(INIT_BY_ELEMENT)
        assert text.count("<INIT-VALUE>") == len(INIT_BY_ELEMENT) * (1 + ports)

    def test_the_valued_variant_really_carries_values(self, with_init: Path) -> None:
        """Non-vacuity: the second model is not the first one twice."""
        document = yaml.safe_load((with_init / "model" / "interfaces" / "IExample.yaml").read_text(encoding="utf-8"))
        interface = SRInterface.model_validate(document)

        assert {element.name: element.init for element in interface.data_elements} == INIT_BY_ELEMENT
        assert check_cross_layer_consistency(str(with_init / "model")).consistent

    def test_the_value_less_variant_carries_neither_added_tag(self, without_init: Path) -> None:
        """The other half of "additive": a model with no value gains nothing."""
        text = _generate(without_init, "4.2.2").decode("utf-8")

        for tag in ADDED_TAGS:
            assert tag not in text


class TestExamplesBasicIsUntouched:
    """The weaker half of the delta claim: a project with no `init` is untouched.

    No digest is frozen here: neither added tag appears at either version, and the 4.2.2
    document is still byte-for-byte the committed golden `basic-4.2.2.arxml`.
    """

    @pytest.fixture
    def basic(self, tmp_path: Path) -> Path:
        return _copy_basic(tmp_path)

    @pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
    def test_neither_added_tag_appears(self, basic: Path, version: str) -> None:
        text = _generate(basic, version).decode("utf-8")

        for tag in ADDED_TAGS:
            assert tag not in text

    def test_the_document_is_still_the_committed_golden(self, basic: Path) -> None:
        assert _generate(basic, "4.2.2").decode("utf-8") == read_golden("basic-4.2.2")

    def test_the_model_really_declares_no_init(self, basic: Path) -> None:
        """Non-vacuity: the claim above is about a project that *could* carry one."""
        interfaces = [
            yaml.safe_load(path.read_text(encoding="utf-8"))
            for path in sorted((basic / "model" / "interfaces").glob("*.yaml"), key=lambda p: p.as_posix())
        ]
        elements = [
            element
            for document in interfaces
            if document.get("kind") == "sender_receiver"
            for element in document["data_elements"]
        ]

        assert elements
        assert all("init" not in element for element in elements)
