"""``check_name_uniqueness`` — ``check-model`` and the ARXML writer agree about "valid".

``check-model`` is the model-consistency gate and writes nothing, while
``swcraft generate arxml`` reads the model directly with no gate in front of
it. So a name shape the writer refuses — a duplicate SWC, interface or data
type name, a reserved name, a duplicate *nested* name (ports, runnables, data
elements, operations, arguments) — is reported by the gate too.

Each test below states one model shape and asserts both halves of the agreement:
``check_name_uniqueness`` reports it, and ``build_workspace()`` refuses it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import model_element_path, write_model_element
from swcraft_tools.diagnostics import Subject, field_step, member_step
from swcraft_tools.schema import (
    ApplicationDataType,
    BaseDataType,
    EnumDataType,
    RecordDataType,
    ReferenceDataType,
    ValueDataType,
)
from swcraft_tools.validator import (
    GENERATED_BUDGET_SUFFIX,
    RESERVED_IMPLEMENTATION_SUFFIX,
    RESERVED_TIMING_SUFFIX,
    check_cross_layer_consistency,
    check_name_uniqueness,
    check_port_interface_references,
)
from swcraft_tools.writer import build_workspace
from swcraft_tools.writer.arxml_generation import (
    ACCESS_LIST_PREFIXES,
    BUDGET_SUFFIX,
    COMPU_METHOD_SUFFIX,
    IMPLEMENTATION_SUFFIX,
    RESERVED_DATA_TYPE_RULES,
    TIMING_SUFFIX,
    access_point_name,
    init_constant_name,
)

#: Two names the writer no longer invents: the application *view* of a value or
#: reference type and the single ``DATA-TYPE-MAPPING-SET`` that tied the views
#: back. ``arxml_generation`` defines neither constant, so they are spelled here
#: as literals, the ordinary declared names they are.
APP_VIEW_SUFFIX = "_AppView"
MAPPING_SET_NAME = "DataTypeMappingSet"

FLOAT32 = {
    "name": "float32",
    "kind": "base_type",
    "size": 32,
    "encoding": "IEEE754",
    "native_declaration": "float",
}


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _swc(name: str, ports: list | None = None, runnables: list | None = None) -> dict:
    """One application component declaring ``no_support`` (a component without it is refused)."""
    return {
        "name": name,
        "type": "application",
        "ports": ports or [],
        "runnables": runnables or [],
        "handle_termination_and_restart": "no_support",
    }


def _assert_reported_and_refused(model_root: Path, fragment: str, writer_fragment: str) -> None:
    """Both gates see the same defect: the validator reports it, the writer refuses it."""
    result = check_name_uniqueness(str(model_root))
    assert not result.consistent
    assert any(fragment in issue for issue in result.issues), result.issues

    # The aggregation point, which is what `swcraft check-model` really runs.
    assert any(fragment in issue for issue in check_cross_layer_consistency(str(model_root)).issues)

    with pytest.raises(ValueError, match=writer_fragment):
        build_workspace(str(model_root))


class TestTheTwoGatesShareTheirReservedNames:
    """The reserved literals ``validator`` restates match the writer's.

    ``validator`` restates them rather than importing them (it stays importable
    without ``writer``, and ``writer`` imports it); this guard stops the two
    copies drifting. The composition name, the application-view suffix and the
    mapping-set name are reserved no more: the writer invents none of those
    elements, so a guard refusing them would be stricter than the writer. The
    application-type forms ``{x}_Unit``/``{x}_Compu``/``{x}_Range`` are pinned in
    ``test_validator_identifier.py`` with the whole restated-affix table; the
    importers' reserved names in ``test_reserved_name_policies.py``.
    """

    def test_the_implementation_suffix_matches(self) -> None:
        assert RESERVED_IMPLEMENTATION_SUFFIX == IMPLEMENTATION_SUFFIX

    def test_the_two_execution_time_budget_suffixes_match(self) -> None:
        """``{swc}_Timing`` (reserved) and ``{runnable}_Budget`` (charged only)."""
        assert RESERVED_TIMING_SUFFIX == TIMING_SUFFIX
        assert GENERATED_BUDGET_SUFFIX == BUDGET_SUFFIX


class TestTheTwoRetiredDataTypeNamesAreAcceptedByBothGates:
    """``{x}_AppView`` and ``DataTypeMappingSet`` are accepted by both gates.

    The writer invents neither an ``APPLICATION-PRIMITIVE-DATA-TYPE`` view nor a
    ``DATA-TYPE-MAPPING-SET``, so a guard refusing those names would be stricter
    than the writer. The writer half alone is
    ``test_arxml_application_types_writer.py``'s.
    """

    @staticmethod
    def _seed(model_root: Path, types: list[dict]) -> None:
        for data in types:
            _write_yaml(model_element_path(model_root, data["name"], kind=data["kind"]), data)

    def test_a_declared_app_view_name_is_reported_by_neither(self, tmp_path: Path) -> None:
        # `{x}_AppView` beside a value type `x`, both over a base type of their
        # own.
        self._seed(
            tmp_path,
            [
                {**FLOAT32, "name": "float32_base"},
                {"name": "float32", "kind": "value_type", "base": "float32_base"},
                {"name": f"float32{APP_VIEW_SUFFIX}", "kind": "value_type", "base": "float32_base"},
            ],
        )

        issues = check_cross_layer_consistency(str(tmp_path)).issues
        assert not any(APP_VIEW_SUFFIX in issue for issue in issues), issues
        build_workspace(str(tmp_path))

    def test_the_retired_mapping_set_name_is_reported_by_neither(self, tmp_path: Path) -> None:
        self._seed(tmp_path, [FLOAT32, {"name": MAPPING_SET_NAME, "kind": "value_type", "base": "float32"}])

        issues = check_cross_layer_consistency(str(tmp_path)).issues
        assert not any(MAPPING_SET_NAME in issue for issue in issues), issues
        build_workspace(str(tmp_path))


class TestTheSharedReservationTableMatchesGeneration:
    """``RESERVED_DATA_TYPE_RULES`` is what the two write-path guards read.

    ``writer/mutations.py``'s ``check_reserved_data_type_name`` and
    ``component_exchange/name_policies.py``'s ``reserved_name_violations`` are
    *derived* from this table rather than restating it, so it has to be a
    truthful statement of what ``_check_data_type_names`` enforces — including
    the kind scope, the half an un-scoped restatement gets wrong.
    """

    #: A minimal declaration per kind, for building the base of each rule. The
    #: record's field names an application type, because a composite member may
    #: not name an implementation type. The application type declares a
    #: ``unit``, so it really mints a generated form — ``Base_T_Unit`` — and the
    #: "unlisted kind reserves nothing" case below therefore also measures that
    #: ``_Compu`` and ``_CompuMethod`` are two reservations and not one.
    _BY_KIND = {
        BaseDataType: {"kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8_t"},
        ValueDataType: {"kind": "value_type", "base": "float32"},
        ReferenceDataType: {"kind": "reference_type", "base": "Speed_T"},
        EnumDataType: {"kind": "enum", "literals": [{"name": "A", "value": 0}]},
        RecordDataType: {"kind": "record", "fields": [{"name": "f", "type": "Phys_T"}]},
        ApplicationDataType: {"kind": "application_type", "unit": "mps"},
    }

    @classmethod
    def _seed_pair(cls, model_root: Path, base_kind: type, suffix: str) -> None:
        _write_yaml(model_element_path(model_root, "float32", kind="base_type"), FLOAT32)
        write_model_element(model_root, {"name": "Speed_T", "kind": "value_type", "base": "float32"})
        write_model_element(model_root, {"name": "Phys_T", "kind": "application_type", "unit": "mps"})
        _write_yaml(
            model_element_path(model_root, "Base_T", kind=cls._BY_KIND[base_kind]["kind"]),
            {"name": "Base_T", **cls._BY_KIND[base_kind]},
        )
        # An application-layer occupant: a form collides inside its owner's
        # package only, and every kind-scoped form is an enum's.
        _write_yaml(
            model_element_path(model_root, f"Base_T{suffix}", kind="application_type"),
            {"name": f"Base_T{suffix}", "kind": "application_type"},
        )

    @pytest.mark.parametrize(("suffix", "element", "base_kinds"), RESERVED_DATA_TYPE_RULES)
    def test_a_base_of_a_listed_kind_really_reserves_the_suffixed_name(
        self, tmp_path: Path, suffix: str, element: str, base_kinds: tuple[type, ...]
    ) -> None:
        for base_kind in base_kinds:
            root = tmp_path / base_kind.__name__
            self._seed_pair(root, base_kind, suffix)

            with pytest.raises(ValueError, match=f"Base_T{suffix}"):
                build_workspace(str(root))

    @pytest.mark.parametrize(("suffix", "element", "base_kinds"), RESERVED_DATA_TYPE_RULES)
    def test_a_base_of_an_unlisted_kind_reserves_nothing(
        self, tmp_path: Path, suffix: str, element: str, base_kinds: tuple[type, ...]
    ) -> None:
        """A guard stricter than the writer refuses names generation accepts."""
        for base_kind in self._BY_KIND:
            if base_kind in base_kinds:
                continue
            root = tmp_path / base_kind.__name__
            self._seed_pair(root, base_kind, suffix)

            build_workspace(str(root))  # no raise: the writer generates nothing for this base


class TestAPortsInterfaceMustBeDeclared:
    """An undeclared port interface: ``check-model`` reports what ``generate_arxml`` refuses."""

    @staticmethod
    def _seed(model_root: Path, interface: str) -> None:
        _write_yaml(
            model_root / "swc" / "group" / "A.yaml",
            _swc("A", ports=[{"name": "p", "direction": "provide", "interface": interface}]),
        )
        # The data element names a value type over a base type.
        write_model_element(model_root, {**FLOAT32, "name": "float32_base"})
        write_model_element(model_root, {"name": "float32", "kind": "value_type", "base": "float32_base"})
        _write_yaml(
            model_root / "interfaces" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "v", "implementation_type": "float32"}],
            },
        )

    def test_an_undeclared_interface_is_reported_and_refused(self, tmp_path: Path) -> None:
        self._seed(tmp_path, "INope")

        issues = check_port_interface_references(str(tmp_path)).issues
        assert issues == ["SWC 'A' port 'p' references interface 'INope' which is not declared in model/interfaces/."]
        assert any("INope" in issue for issue in check_cross_layer_consistency(str(tmp_path)).issues)
        with pytest.raises(ValueError, match="Interface 'INope'"):
            build_workspace(str(tmp_path))

    def test_a_declared_interface_passes(self, tmp_path: Path) -> None:
        """An unconnected port is still checked — no connector reaches it."""
        self._seed(tmp_path, "ISpeed")

        assert check_port_interface_references(str(tmp_path)).consistent
        build_workspace(str(tmp_path))


class TestPackageLevelNames:
    """Package-level names: duplicate SWCs and interfaces, and the generated-name twins."""

    def test_duplicate_swc_names_are_reported(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "swc" / "a" / "one.yaml", _swc("Foo"))
        _write_yaml(tmp_path / "swc" / "b" / "two.yaml", _swc("Foo"))

        _assert_reported_and_refused(tmp_path, "Duplicate SWC name 'Foo'", "Duplicate SWC names")

    def test_duplicate_interface_names_are_reported(self, tmp_path: Path) -> None:
        _write_yaml(model_element_path(tmp_path, "float32", kind="base_type"), FLOAT32)
        for stem in ("first", "second"):
            _write_yaml(
                tmp_path / "interfaces" / f"{stem}.yaml",
                {
                    "name": "ISame",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": stem, "implementation_type": "float32"}],
                },
            )

        _assert_reported_and_refused(tmp_path, "Duplicate interface name 'ISame'", "Duplicate interface names")

    def test_an_implementation_twin_name_is_reported(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "swc" / "a" / "A.yaml", _swc("A"))
        _write_yaml(tmp_path / "swc" / "a" / "AI.yaml", _swc(f"A{RESERVED_IMPLEMENTATION_SUFFIX}"))

        _assert_reported_and_refused(tmp_path, "auto-generated SWC-IMPLEMENTATION name", "collide with auto-generated")

    def test_an_internal_behavior_twin_name_is_not_reported(self, tmp_path: Path) -> None:
        """``B_InternalBehavior`` is nested in its own component, so both paths resolve."""
        _write_yaml(tmp_path / "swc" / "a" / "B.yaml", _swc("B"))
        _write_yaml(tmp_path / "swc" / "a" / "BI.yaml", _swc("B_InternalBehavior"))

        assert check_name_uniqueness(str(tmp_path)).consistent


class TestNestedNames:
    """Duplicate nested ``SHORT-NAME``s: ports, runnables, data elements, operations and arguments."""

    def test_duplicate_port_names_are_reported(self, tmp_path: Path) -> None:
        _write_yaml(model_element_path(tmp_path, "float32", kind="base_type"), FLOAT32)
        _write_yaml(
            tmp_path / "interfaces" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "speed", "implementation_type": "float32"}],
            },
        )
        _write_yaml(
            tmp_path / "swc" / "a" / "A.yaml",
            _swc(
                "A",
                ports=[
                    {"name": "p", "direction": "provide", "interface": "ISpeed"},
                    {"name": "p", "direction": "require", "interface": "ISpeed"},
                ],
            ),
        )

        _assert_reported_and_refused(tmp_path, "duplicate port name 'p'", "duplicate port name")

    def test_duplicate_runnable_names_are_reported(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "swc" / "a" / "A.yaml",
            _swc("A", runnables=[{"name": "R", "events": []}, {"name": "R", "events": []}]),
        )

        _assert_reported_and_refused(tmp_path, "duplicate runnable name 'R'", "duplicate runnable name")

    def test_duplicate_data_element_names_are_reported(self, tmp_path: Path) -> None:
        _write_yaml(model_element_path(tmp_path, "float32", kind="base_type"), FLOAT32)
        _write_yaml(
            tmp_path / "interfaces" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [
                    {"name": "d", "implementation_type": "float32"},
                    {"name": "d", "implementation_type": "float32"},
                ],
            },
        )

        _assert_reported_and_refused(tmp_path, "duplicate data element name 'd'", "duplicate data element name")

    def test_duplicate_operation_names_are_reported(self, tmp_path: Path) -> None:
        _write_yaml(model_element_path(tmp_path, "float32", kind="base_type"), FLOAT32)
        _write_yaml(
            tmp_path / "interfaces" / "IDiag.yaml",
            {
                "name": "IDiag",
                "kind": "client_server",
                "operations": [{"name": "op", "arguments": []}, {"name": "op", "arguments": []}],
            },
        )

        _assert_reported_and_refused(tmp_path, "duplicate operation name 'op'", "duplicate operation name")

    def test_duplicate_argument_names_are_reported(self, tmp_path: Path) -> None:
        _write_yaml(model_element_path(tmp_path, "float32", kind="base_type"), FLOAT32)
        _write_yaml(
            tmp_path / "interfaces" / "IDiag.yaml",
            {
                "name": "IDiag",
                "kind": "client_server",
                "operations": [
                    {
                        "name": "op",
                        "arguments": [
                            {"name": "a", "implementation_type": "float32", "direction": "in"},
                            {"name": "a", "implementation_type": "float32", "direction": "in"},
                        ],
                    }
                ],
            },
        )

        _assert_reported_and_refused(tmp_path, "duplicate argument name 'a'", "duplicate argument name")

    def test_the_same_name_in_two_components_is_not_reported(self, tmp_path: Path) -> None:
        """Sibling-scoped, not global — the check must not refuse a legal model."""
        _write_yaml(model_element_path(tmp_path, "float32", kind="base_type"), FLOAT32)
        _write_yaml(
            tmp_path / "interfaces" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "speed", "implementation_type": "float32"}],
            },
        )
        for name in ("A", "B"):
            _write_yaml(
                tmp_path / "swc" / "a" / f"{name}.yaml",
                _swc(
                    name,
                    ports=[{"name": "p", "direction": "provide", "interface": "ISpeed"}],
                    runnables=[{"name": "R", "events": []}],
                ),
            )

        assert check_name_uniqueness(str(tmp_path)).consistent


class TestTheInitConstantNameCollision:
    """The eighth generated form's report — ``interface.init-constant-name-collision``.

    The writer emits one ``CONSTANT-SPECIFICATION`` per data element that
    carries an ``init``, names it ``{Interface}_{Element}_Init`` and puts it in
    ``PortInterfaces`` as a **sibling of the interfaces**. Two claims on one such
    ``SHORT-NAME`` are what this rule reports, in two shapes:

    * **constant ↔ interface** — a declared interface literally named like some
      pair's constant;
    * **constant ↔ constant** — the derivation is *not injective*, because an
      interface name and a data element name may both contain ``_``: interface
      ``A_B`` with element ``C`` and interface ``A`` with element ``B_C`` both
      give ``A_B_C_Init``. ``ShortNameScope`` refuses the two constants at write
      time with a message about neither pair, which is why the report exists.

    The reservation is **unconditional** — every declared sender/receiver pair,
    whether or not its element carries an ``init`` today — as ``{enum}_CompuMethod``
    is for every declared enum: a conditional one would mean that adding the
    first initial value to one interface starts failing generation on a file
    nobody touched. (The application-type forms are reserved per *declared
    field* instead, because that is when the writer emits them.)

    Neither half is renamed and neither gets a counter: both names are declared,
    and a declared name is the model's to change, never the writer's.
    ``swc.access.name-collision`` reports the same underscore ambiguity at the
    runnable scope (``TestTheAccessPointNameCollision``).
    """

    RULE = "interface.init-constant-name-collision"

    @staticmethod
    def _sr(name: str, elements: list[dict]) -> dict:
        return {"name": name, "kind": "sender_receiver", "data_elements": elements}

    @staticmethod
    def _model(root: Path, interfaces: list[tuple[str, dict]]) -> None:
        """Write *interfaces* as ``{stem}.yaml``, so reader order is the caller's to choose."""
        _write_yaml(model_element_path(root, "float32", kind="base_type"), FLOAT32)
        for stem, interface in interfaces:
            _write_yaml(root / "interfaces" / f"{stem}.yaml", interface)

    def _records(self, model_root: Path) -> list:
        return [record for record in check_name_uniqueness(str(model_root)).records if record.rule == self.RULE]

    def _only(self, model_root: Path):
        records = self._records(model_root)
        assert len(records) == 1, [record.message for record in records]
        return records[0]

    # -- constant <-> interface, in both declaration orders -----------------

    def test_an_interface_named_like_a_later_pairs_constant_is_reported(self, tmp_path: Path) -> None:
        """The colliding interface is read **first**: the pair that derives its name comes after it."""
        shared = init_constant_name("ISpeed", "speed")
        self._model(
            tmp_path,
            [
                ("a", self._sr(shared, [{"name": "value", "implementation_type": "float32"}])),
                ("b", self._sr("ISpeed", [{"name": "speed", "implementation_type": "float32"}])),
            ],
        )

        record = self._only(tmp_path)
        assert shared in record.message
        assert "interface 'ISpeed' data element 'speed'" in record.message
        assert f"declared as interface '{shared}'" in record.message
        assert record.subjects == (Subject("interfaces", "ISpeed"), Subject("interfaces", shared))

    def test_a_pair_whose_constant_is_an_earlier_interfaces_name_is_reported(self, tmp_path: Path) -> None:
        """The same collision with the two files read the other way round.

        The answer must not depend on which file the reader reaches first — what a
        walk keyed on a dict gets wrong in one of the two directions.
        """
        shared = init_constant_name("ISpeed", "speed")
        self._model(
            tmp_path,
            [
                ("a", self._sr("ISpeed", [{"name": "speed", "implementation_type": "float32"}])),
                ("b", self._sr(shared, [{"name": "value", "implementation_type": "float32"}])),
            ],
        )

        record = self._only(tmp_path)
        assert shared in record.message
        assert "interface 'ISpeed' data element 'speed'" in record.message
        assert record.subjects == (Subject("interfaces", "ISpeed"), Subject("interfaces", shared))

    # -- constant <-> constant, the underscore ambiguity ---------------------

    def test_two_pairs_deriving_one_constant_are_one_finding_naming_both(self, tmp_path: Path) -> None:
        shared = init_constant_name("A_B", "C")
        assert shared == init_constant_name("A", "B_C")
        self._model(
            tmp_path,
            [
                ("a", self._sr("A", [{"name": "B_C", "implementation_type": "float32", "init": 1.0}])),
                ("b", self._sr("A_B", [{"name": "C", "implementation_type": "float32", "init": 2.0}])),
            ],
        )

        record = self._only(tmp_path)
        assert shared in record.message
        assert "interface 'A' data element 'B_C'" in record.message
        assert "interface 'A_B' data element 'C'" in record.message
        assert "not injective" in record.message
        assert record.subjects == (Subject("interfaces", "A"), Subject("interfaces", "A_B"))

    def test_the_same_pair_set_with_no_init_anywhere_is_still_a_finding(self, tmp_path: Path) -> None:
        """The reservation is unconditional.

        Reserving only the pairs that carry a value today would mean that adding
        the first ``init`` to ``A`` starts failing generation on ``A_B``, a file
        nobody touched.
        """
        shared = init_constant_name("A_B", "C")
        self._model(
            tmp_path,
            [
                ("a", self._sr("A", [{"name": "B_C", "implementation_type": "float32"}])),
                ("b", self._sr("A_B", [{"name": "C", "implementation_type": "float32"}])),
            ],
        )

        record = self._only(tmp_path)
        assert shared in record.message
        assert record.subjects == (Subject("interfaces", "A"), Subject("interfaces", "A_B"))

    def test_three_pairs_deriving_one_constant_are_one_finding_naming_all_three(self, tmp_path: Path) -> None:
        """One finding per shared *name*, not one per claimant pair."""
        shared = init_constant_name("A_B_C", "D")
        assert shared == init_constant_name("A_B", "C_D") == init_constant_name("A", "B_C_D")
        self._model(
            tmp_path,
            [
                ("a", self._sr("A", [{"name": "B_C_D", "implementation_type": "float32"}])),
                ("b", self._sr("A_B", [{"name": "C_D", "implementation_type": "float32"}])),
                ("c", self._sr("A_B_C", [{"name": "D", "implementation_type": "float32"}])),
            ],
        )

        record = self._only(tmp_path)
        assert shared in record.message
        for interface_name, element_name in (("A", "B_C_D"), ("A_B", "C_D"), ("A_B_C", "D")):
            assert f"interface '{interface_name}' data element '{element_name}'" in record.message
        assert record.subjects == (
            Subject("interfaces", "A"),
            Subject("interfaces", "A_B"),
            Subject("interfaces", "A_B_C"),
        )

    # -- the two gates agree, and a clean model is left alone ----------------

    def test_the_gate_reports_the_name_the_writer_refuses(self, tmp_path: Path) -> None:
        """The gates agree on the derived name, not on a sentence.

        The writer batches its offenders into one ``ValueError`` while the validator
        emits one record per shared name.
        """
        shared = init_constant_name("A_B", "C")
        self._model(
            tmp_path,
            [
                ("a", self._sr("A", [{"name": "B_C", "implementation_type": "float32"}])),
                ("b", self._sr("A_B", [{"name": "C", "implementation_type": "float32"}])),
            ],
        )

        _assert_reported_and_refused(tmp_path, shared, "CONSTANT-SPECIFICATION")

    def test_a_duplicate_interface_name_is_not_reported_a_second_time_here(self, tmp_path: Path) -> None:
        """One root cause, one finding: two files of one interface are ``name.duplicate-interface``."""
        self._model(
            tmp_path,
            [
                ("a", self._sr("ISame", [{"name": "speed", "implementation_type": "float32"}])),
                ("b", self._sr("ISame", [{"name": "speed", "implementation_type": "float32"}])),
            ],
        )

        assert self._records(tmp_path) == []
        assert any("Duplicate interface name 'ISame'" in issue for issue in check_name_uniqueness(str(tmp_path)).issues)

    def test_underscores_that_derive_distinct_names_are_left_alone(self, tmp_path: Path) -> None:
        """The negative control: ``_`` in both names is legal and common; only a *shared* derived name is a defect."""
        self._model(
            tmp_path,
            [
                ("a", self._sr("A_B", [{"name": "C_D", "implementation_type": "float32", "init": 1.0}])),
                ("b", self._sr("A_B_C", [{"name": "E", "implementation_type": "float32"}])),
                ("c", self._sr("A", [{"name": "B_D_E", "implementation_type": "float32"}])),
            ],
        )

        assert self._records(tmp_path) == []
        assert check_name_uniqueness(str(tmp_path)).consistent


class TestTheAccessPointNameCollision:
    """The three access-point forms' report — ``swc.access.name-collision``.

    The writer emits one ``VARIABLE-ACCESS`` or ``SYNCHRONOUS-SERVER-CALL-POINT``
    per entry of a runnable's ``reads``, ``writes`` and ``calls``, names it
    ``R_<port>_<element>``, ``W_<port>_<element>`` or ``Call_<port>_<operation>``
    and puts it **under the runnable**. The four access containers are not path
    segments, so every access point of one runnable is a ``SHORT-NAME`` sibling of
    every other: the scope of a clash is the runnable and not the component.

    One shape reaches that scope, the underscore. The derivation is **not
    injective** because a port name and a member name may both contain ``_``:
    port ``a_b`` reading element ``c`` and port ``a`` reading element ``b_c``
    both give ``R_a_b_c``, and ``arxml_xml.ShortNameScope`` refuses the two at
    write time with a message about neither entry, which is why the report
    exists. Nothing else can collide here: the three prefixes keep the lists
    apart, a ``(port, member)`` pair listed twice in one list is refused at load
    (``rule:swc.runnable.<list>.no-duplicate-entry``), and a runnable's
    generated event names live under ``EVENTS``, a different parent.

    Neither name is renamed and neither gets a counter: both are declared, and a
    declared name is the model's to change, never the writer's. A ``_0``/``_1``
    suffix would also make the emitted name a function of list order, which the
    lists do not carry. ``interface.init-constant-name-collision`` reports the
    same ambiguity at the interfaces scope (``TestTheInitConstantNameCollision``).
    """

    RULE = "swc.access.name-collision"

    @staticmethod
    def _sr(name: str, elements: list[str]) -> dict:
        return {
            "name": name,
            "kind": "sender_receiver",
            "data_elements": [{"name": element, "implementation_type": "float32"} for element in elements],
        }

    @staticmethod
    def _cs(name: str, operations: list[str]) -> dict:
        return {
            "name": name,
            "kind": "client_server",
            "operations": [{"name": operation, "arguments": []} for operation in operations],
        }

    def _model(self, root: Path, *, interfaces: list[dict], ports: list[dict], runnables: list[dict]) -> None:
        """One component, ``C``, in one group, over *interfaces*."""
        _write_yaml(model_element_path(root, "float32", kind="base_type"), FLOAT32)
        for interface in interfaces:
            _write_yaml(root / "interfaces" / f"{interface['name']}.yaml", interface)
        _write_yaml(root / "swc" / "grp" / "C.yaml", _swc("C", ports=ports, runnables=runnables))

    def _records(self, model_root: Path) -> list:
        return [record for record in check_name_uniqueness(str(model_root)).records if record.rule == self.RULE]

    def _only(self, model_root: Path):
        records = self._records(model_root)
        assert len(records) == 1, [record.message for record in records]
        return records[0]

    @staticmethod
    def _runnable_subject(runnable: str) -> Subject:
        return Subject("swc", "C").at(field_step("runnables"), member_step(runnable))

    # -- the underscore ambiguity, inside one runnable ------------------------

    def test_two_entries_of_one_runnable_deriving_one_name_are_one_finding_naming_both(self, tmp_path: Path) -> None:
        emitted = access_point_name(ACCESS_LIST_PREFIXES["reads"], "a_b", "c")
        assert emitted == access_point_name(ACCESS_LIST_PREFIXES["reads"], "a", "b_c")
        self._model(
            tmp_path,
            interfaces=[self._sr("IAB", ["c"]), self._sr("IA", ["b_c"])],
            ports=[
                {"name": "a_b", "direction": "require", "interface": "IAB"},
                {"name": "a", "direction": "require", "interface": "IA"},
            ],
            runnables=[
                {
                    "name": "Run",
                    "events": [],
                    "reads": [{"port": "a_b", "element": "c"}, {"port": "a", "element": "b_c"}],
                }
            ],
        )

        record = self._only(tmp_path)
        assert emitted in record.message
        assert "SWC 'C' runnable 'Run'" in record.message
        assert "reads port 'a_b' element 'c'" in record.message
        assert "reads port 'a' element 'b_c'" in record.message
        assert "not injective" in record.message
        assert record.subjects == (self._runnable_subject("Run"),)

    def test_the_same_two_entries_split_over_two_runnables_are_not_a_finding(self, tmp_path: Path) -> None:
        """The scope is the runnable, which is the whole of what this asserts.

        The two access points are siblings only when one runnable holds both:
        under two runnables they sit under two different parents and the paths
        ``/…/{runnable}/R_a_b_c`` differ in the segment before the name.
        """
        self._model(
            tmp_path,
            interfaces=[self._sr("IAB", ["c"]), self._sr("IA", ["b_c"])],
            ports=[
                {"name": "a_b", "direction": "require", "interface": "IAB"},
                {"name": "a", "direction": "require", "interface": "IA"},
            ],
            runnables=[
                {"name": "First", "events": [], "reads": [{"port": "a_b", "element": "c"}]},
                {"name": "Second", "events": [], "reads": [{"port": "a", "element": "b_c"}]},
            ],
        )

        assert self._records(tmp_path) == []
        assert check_name_uniqueness(str(tmp_path)).consistent

    def test_three_entries_deriving_one_name_are_one_finding_naming_all_three(self, tmp_path: Path) -> None:
        """One finding per shared *name*, not one per claiming entry."""
        emitted = access_point_name(ACCESS_LIST_PREFIXES["reads"], "a_b_c", "d")
        assert (
            emitted
            == access_point_name(ACCESS_LIST_PREFIXES["reads"], "a_b", "c_d")
            == access_point_name(ACCESS_LIST_PREFIXES["reads"], "a", "b_c_d")
        )
        self._model(
            tmp_path,
            interfaces=[self._sr("IABC", ["d"]), self._sr("IAB", ["c_d"]), self._sr("IA", ["b_c_d"])],
            ports=[
                {"name": "a_b_c", "direction": "require", "interface": "IABC"},
                {"name": "a_b", "direction": "require", "interface": "IAB"},
                {"name": "a", "direction": "require", "interface": "IA"},
            ],
            runnables=[
                {
                    "name": "Run",
                    "events": [],
                    "reads": [
                        {"port": "a_b_c", "element": "d"},
                        {"port": "a_b", "element": "c_d"},
                        {"port": "a", "element": "b_c_d"},
                    ],
                }
            ],
        )

        record = self._only(tmp_path)
        assert emitted in record.message
        for port_name, element_name in (("a_b_c", "d"), ("a_b", "c_d"), ("a", "b_c_d")):
            assert f"reads port '{port_name}' element '{element_name}'" in record.message
        assert record.subjects == (self._runnable_subject("Run"),)

    def test_a_by_value_read_claims_the_same_name_as_a_by_argument_one(self, tmp_path: Path) -> None:
        """``by`` chooses the container, never the name.

        The two receive containers are not path segments either, so a by-value
        read and a by-argument read of one runnable are siblings like any other
        pair — the case the container split could have hidden.
        """
        emitted = access_point_name(ACCESS_LIST_PREFIXES["reads"], "a_b", "c")
        self._model(
            tmp_path,
            interfaces=[self._sr("IAB", ["c"]), self._sr("IA", ["b_c"])],
            ports=[
                {"name": "a_b", "direction": "require", "interface": "IAB"},
                {"name": "a", "direction": "require", "interface": "IA"},
            ],
            runnables=[
                {
                    "name": "Run",
                    "events": [],
                    "reads": [
                        {"port": "a_b", "element": "c"},
                        {"port": "a", "element": "b_c", "by": "value"},
                    ],
                }
            ],
        )

        record = self._only(tmp_path)
        assert emitted in record.message
        assert record.subjects == (self._runnable_subject("Run"),)

    # -- the controls: a prefix keeps two lists apart -------------------------

    def test_a_read_and_a_write_deriving_one_tail_are_not_a_collision(self, tmp_path: Path) -> None:
        """``R_`` and ``W_`` keep the two lists apart: the *tail* ``a_b_c`` is shared, the emitted names are not."""
        self._model(
            tmp_path,
            interfaces=[self._sr("IAB", ["c"]), self._sr("IA", ["b_c"])],
            ports=[
                {"name": "a_b", "direction": "require", "interface": "IAB"},
                {"name": "a", "direction": "provide", "interface": "IA"},
            ],
            runnables=[
                {
                    "name": "Run",
                    "events": [],
                    "reads": [{"port": "a_b", "element": "c"}],
                    "writes": [{"port": "a", "element": "b_c"}],
                }
            ],
        )

        assert self._records(tmp_path) == []
        assert access_point_name(ACCESS_LIST_PREFIXES["reads"], "a_b", "c") != access_point_name(
            ACCESS_LIST_PREFIXES["writes"], "a", "b_c"
        )

    def test_a_call_never_shares_a_name_with_a_read(self, tmp_path: Path) -> None:
        """The third prefix, ``Call_``, over the same shared tail."""
        self._model(
            tmp_path,
            interfaces=[self._sr("IAB", ["c"]), self._cs("ICS", ["b_c"])],
            ports=[
                {"name": "a_b", "direction": "require", "interface": "IAB"},
                {"name": "a", "direction": "require", "interface": "ICS"},
            ],
            runnables=[
                {
                    "name": "Run",
                    "events": [],
                    "reads": [{"port": "a_b", "element": "c"}],
                    "calls": [{"port": "a", "operation": "b_c"}],
                }
            ],
        )

        assert self._records(tmp_path) == []
        assert check_name_uniqueness(str(tmp_path)).consistent

    def test_underscores_that_derive_distinct_names_are_left_alone(self, tmp_path: Path) -> None:
        """The negative control: ``_`` in both names is legal and common; only a *shared* derived name is a defect."""
        self._model(
            tmp_path,
            interfaces=[self._sr("IAB", ["c_d"]), self._sr("IABC", ["e"]), self._sr("IA", ["b_d_e"])],
            ports=[
                {"name": "a_b", "direction": "require", "interface": "IAB"},
                {"name": "a_b_c", "direction": "require", "interface": "IABC"},
                {"name": "a", "direction": "require", "interface": "IA"},
            ],
            runnables=[
                {
                    "name": "Run",
                    "events": [],
                    "reads": [
                        {"port": "a_b", "element": "c_d"},
                        {"port": "a_b_c", "element": "e"},
                        {"port": "a", "element": "b_d_e"},
                    ],
                }
            ],
        )

        assert self._records(tmp_path) == []
        assert check_name_uniqueness(str(tmp_path)).consistent

    def test_a_runnable_declaring_no_access_is_left_alone(self, tmp_path: Path) -> None:
        self._model(
            tmp_path,
            interfaces=[self._sr("IAB", ["c"])],
            ports=[{"name": "a_b", "direction": "require", "interface": "IAB"}],
            runnables=[{"name": "Run", "events": []}],
        )

        assert self._records(tmp_path) == []
        assert check_name_uniqueness(str(tmp_path)).consistent

    # -- the two gates agree --------------------------------------------------

    def test_the_gate_reports_the_name_the_writer_refuses(self, tmp_path: Path) -> None:
        """The gates agree on the derived name, not on a sentence.

        The writer batches its offenders into one ``ValueError`` while the validator
        emits one record per shared name.
        """
        emitted = access_point_name(ACCESS_LIST_PREFIXES["reads"], "a_b", "c")
        self._model(
            tmp_path,
            interfaces=[self._sr("IAB", ["c"]), self._sr("IA", ["b_c"])],
            ports=[
                {"name": "a_b", "direction": "require", "interface": "IAB"},
                {"name": "a", "direction": "require", "interface": "IA"},
            ],
            runnables=[
                {
                    "name": "Run",
                    "events": [],
                    "reads": [{"port": "a_b", "element": "c"}, {"port": "a", "element": "b_c"}],
                }
            ],
        )

        _assert_reported_and_refused(tmp_path, emitted, "are written with one access point SHORT-NAME")


class TestTheReferenceModelStaysClean:
    def test_the_example_model_has_no_name_issues(self) -> None:
        """A regression tripwire: the built-in model is what ``dev.py build`` gates on."""
        model_root = Path(__file__).resolve().parents[3] / "examples" / "basic" / "model"
        assert check_name_uniqueness(str(model_root)).consistent

    def test_an_empty_model_is_consistent(self, tmp_path: Path) -> None:
        assert check_name_uniqueness(str(tmp_path)).consistent


class TestTheCompuMethodScopeIsOneScopeInBothGates:
    """``_CompuMethod`` is reserved for exactly the kind the writer emits a method for.

    It is the one kind-scoped reservation ``RESERVED_DATA_TYPE_RULES`` holds. The
    writer's ``_check_data_type_names`` and ``check_data_type_references`` /
    ``check_identifier_conformance`` must move together, so the agreement is
    asserted per kind, on **both** the collision half and the overflow half. The
    application-type forms are scoped by a *declared field*, a different property
    with cases in ``test_validator_identifier.py``; the ``application_type`` row
    is here because, like every kind without a method, it must generate no
    ``_CompuMethod``.
    """

    #: One declaration per top-level data type kind, over the two seeded bases.
    _DECLARATIONS: dict[str, dict] = {
        "base_type": {"kind": "base_type", "size": 8, "encoding": "NONE"},
        "value_type": {"kind": "value_type", "base": "float32"},
        "reference_type": {"kind": "reference_type", "base": "Speed_T"},
        "enum": {"kind": "enum", "literals": [{"name": "A", "value": 0}]},
        # Both composites name the seeded application type: a member may not name
        # an implementation type, and a fixture the writer refuses for an
        # unrelated reason would make the "not refused for this collision" half
        # of each case true for the wrong reason.
        "record": {"kind": "record", "fields": [{"name": "f", "type": "Phys_T"}]},
        "array": {"kind": "array", "element_type": "Phys_T", "length": 2},
        "application_type": {"kind": "application_type", "unit": "mps"},
    }

    #: The kinds the writer generates ``{x}_CompuMethod`` for, stated from the
    #: contract's `appliesTo` rather than read from either implementation.
    _METHODED = frozenset({"enum"})

    @staticmethod
    def _seed(model_root: Path, name: str, kind: str, extra: list[dict] = ()) -> None:
        for data in (
            FLOAT32,
            {"name": "Speed_T", "kind": "value_type", "base": "float32"},
            {"name": "Phys_T", "kind": "application_type"},
            {"name": name, **TestTheCompuMethodScopeIsOneScopeInBothGates._DECLARATIONS[kind]},
            *extra,
        ):
            _write_yaml(model_element_path(model_root, data["name"], kind=data["kind"]), data)

    @staticmethod
    def _writer_refusal(model_root: Path) -> str:
        try:
            build_workspace(str(model_root))
        except ValueError as refusal:
            return str(refusal)
        return ""

    @pytest.mark.parametrize("kind", sorted(_DECLARATIONS))
    def test_the_collision_is_reported_and_refused_for_the_same_kinds(self, kind: str, tmp_path: Path) -> None:
        colliding = f"X{COMPU_METHOD_SUFFIX}"
        # An occupant of the enum's package: a form collides inside its owner's
        # package only.
        self._seed(tmp_path, "X", kind, [{"name": colliding, "kind": "application_type"}])

        reported = any(
            colliding in issue and "CompuMethod name for enum" in issue
            for issue in check_cross_layer_consistency(str(tmp_path)).issues
        )
        refused = colliding in self._writer_refusal(tmp_path)

        assert reported == refused, (kind, reported, refused)
        assert refused == (kind in self._METHODED), kind

    @pytest.mark.parametrize("kind", sorted(_DECLARATIONS))
    def test_the_overflowing_form_is_reported_and_refused_for_the_same_kinds(self, kind: str, tmp_path: Path) -> None:
        from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH
        from swcraft_tools.validator import check_identifier_conformance

        name = "X" * (MAX_IDENTIFIER_LENGTH - len(COMPU_METHOD_SUFFIX) + 1)
        self._seed(tmp_path, name, kind)

        emitted = f"{name}{COMPU_METHOD_SUFFIX}"
        reported = any(f"'{emitted}'" in issue for issue in check_identifier_conformance(str(tmp_path)).issues)
        refused = emitted in self._writer_refusal(tmp_path)

        assert reported == refused, (kind, reported, refused)
        assert refused == (kind in self._METHODED), kind
