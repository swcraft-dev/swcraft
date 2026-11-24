"""XSD conformance against the licensed AUTOSAR 4.2.2 and 4.3.0 schemas.

Every test whose subject is XSD conformance resolves its schema through
``xsd_support.require_licensed_schema`` and names the version; ``python scripts/dev.py test-xsd``
passes ``--require-autosar-xsd``, which turns a missing schema from a skip into a failure, so a
green gate means these tests ran. A test of the structural
fallback forces it (``xsd_support.forced_structural_validation``) and lives elsewhere.

``schemas/`` is gitignored, so a worktree points ``SWCRAFT_AUTOSAR_XSD_PATH_4_2_2`` /
``SWCRAFT_AUTOSAR_XSD_PATH_4_3_0`` at the two files (or symlinks ``schemas/``); the single-path
``SWCRAFT_AUTOSAR_XSD_PATH`` is accepted only when its filename matches the version under test.

The module is also the oracle for :mod:`swcraft_tools.identifier`: it reads the name rule out of the
schemas' ``IDENTIFIER--SIMPLE`` facets and proves it on hand-substituted documents, because the
writer refuses a non-conforming name and so no generated document can carry one.
``TestTheHeadlineAcceptance`` is the generated counterpart: a model whose every binding generated
form is exactly 128 characters, which checks that each writer guard measures the budget the emitter
spends.
"""

from __future__ import annotations

import functools
import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import xsd_support
import yaml
from conftest import (
    ACCESS_POINTS_MODEL_ROOT,
    COMPATIBLE_INTERFACES_MODEL_ROOT,
    COMPOSITIONS_MODEL_ROOT,
    GOLDEN_DIR,
    GOLDEN_MODEL_ROOTS,
    HANDLE_TERMINATION_AND_RESTART_MODEL_ROOT,
    INIT_VALUES_MODEL_ROOT,
    REPO_ROOT,
    _root_composition,
    declare_no_support,
    handle_termination_and_restart_declared_copy,
    layer_directories,
    make_empty_model_root,
    model_element_path,
    write_model_element,
)
from data_event_support import COMPONENT as DATA_EVENT_COMPONENT
from data_event_support import COMPONENT_FILE as DATA_EVENT_COMPONENT_FILE
from data_event_support import WHEEL_INTERFACE_FILE, data_event_model, stub_free_data_event_model
from exclusive_area_support import area_model, every_area_shape_model
from external_stubs_support import BOUNDARY_PORTS as STUB_BOUNDARY_PORTS
from external_stubs_support import CS_INTERFACE as STUB_CS_INTERFACE
from external_stubs_support import INTERFACE_STUBS as STUB_INTERFACES
from external_stubs_support import (
    SECOND_COMPONENT_STUB,
    STUB_PORTS,
    change,
    component_stub_ports_model_root,
    external_stubs_model_root,
    listed_stub_model_root,
)
from external_stubs_support import TYPE_STUB as STUB_TYPE
from impl_composites_support import impl_composites_model_root
from inter_runnable_variable_support import COMPONENT, every_variable_shape_model, variable_model
from lxml import etree
from operation_event_support import (
    BOUNDARY_OPERATION,
    BOUNDARY_PORT,
    every_event_shape_model,
    mixed_event_model,
)
from swcraft_tools.arxml_validation import MODE_XSD, ValidationOutcome, check_arxml
from swcraft_tools.arxml_version import SUPPORTED_VERSIONS, ArxmlVersion
from swcraft_tools.capability_markers import (
    ASYNCHRONOUS_CALLS,
    DATA_EVENTS,
    EXCLUSIVE_AREAS,
    IMPLICIT_ACCESS,
    INTER_RUNNABLE_VARIABLES,
    OPERATION_EVENTS,
)
from swcraft_tools.component_exchange import export_component
from swcraft_tools.identifier import (
    C_IDENTIFIER_PATTERN,
    IDENTIFIER_PATTERN,
    MAX_IDENTIFIER_LENGTH,
    check_identifier,
)
from swcraft_tools.reader import read_mapping_sets
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml
from swcraft_tools.writer.arxml_generation import (
    CALL_POINT_PREFIX,
    COMPU_METHOD_SUFFIX,
    COMPU_SUFFIX,
    IMPLEMENTATION_SUFFIX,
    INIT_CONSTANT_SUFFIX,
    INIT_EVENT_PREFIX,
    INTERNAL_BEHAVIOR_SUFFIX,
    RANGE_SUFFIX,
    READ_ACCESS_PREFIX,
    TIMING_EVENT_PREFIX,
    UNIT_SUFFIX,
    WRITE_ACCESS_PREFIX,
    access_point_name,
    init_constant_name,
)
from swcraft_tools.writer.arxml_xml import ar_qname, short_name_of
from swcraft_tools.writer.mutations import (
    add_init_event,
    add_port,
    add_runnable,
    add_timing_event,
    add_trace_link,
    create_connector,
    create_cs_interface,
    create_data_type,
    create_hardware_element,
    create_interface,
    create_swc,
)
from test_arxml_fixtures import FIXTURE_SCHEMA_FILENAMES, FIXTURES_DIR
from test_arxml_symbol_and_port_init_writer import symbol_and_port_init_model_root
from test_behavioural_emission import _mixed_composition_ports, behavioural_model
from test_description_emission import _describe_model
from test_package_placement_writer import placed_model_root
from test_value_type_range import _exchange_model
from test_writer_defaults import EXCHANGE_MODEL_ROOT as COMPONENT_EXCHANGE_MODEL_ROOT
from test_writer_defaults import _members_model
from test_writer_fidelity_refreeze import BRANCH_POINT
from xsd_support import require_licensed_schema

#: The ``{name}_impl`` suffix, a literal: the writer generates no such twin for a primitive, and names
#: here end in it as ordinary names.
IMPL_SUFFIX = "_impl"

GOLDEN_NAMES = ["basic-4.2.2", "composite-4.2.2", "empty-4.2.2"]

#: The versions the parametrized tests cover, pinned to ``SUPPORTED_VERSIONS`` by
#: ``TestBothVersionsAreCovered``.
_CONFORMANCE_VERSIONS = tuple(ArxmlVersion)


def _assert_conforms(arxml_path: str, version: ArxmlVersion) -> None:
    """XSD-validate *arxml_path* against *version*'s real schema, reporting why not."""
    schema = require_licensed_schema(version)
    outcome = check_arxml(arxml_path, schema_path=schema)
    assert outcome.mode == MODE_XSD
    assert outcome.schema_error is False, (
        f"{schema} could not be compiled — a toolchain fault, not a model one: {outcome.messages}"
    )
    assert outcome.valid, "\n".join((f"{arxml_path} does not conform to {schema}:", *outcome.messages))


def _model_roots(tmp_path: Path) -> dict[str, str]:
    """Every golden's model root, including the empty tree ``empty`` is built from."""
    roots = {name: str(root) for name, root in GOLDEN_MODEL_ROOTS.items()}
    roots["empty-4.2.2"] = str(make_empty_model_root(tmp_path))
    return roots


class TestTheGateCannotPassOnSkips:
    """``--require-autosar-xsd`` is the difference between "passed" and "ran"."""

    @staticmethod
    def _hide_every_schema(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(xsd_support.SCHEMA_PATH_ENV, raising=False)
        for name in xsd_support.SCHEMA_PATH_ENV_BY_VERSION.values():
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(
            xsd_support, "DEFAULT_SCHEMA_PATHS", dict.fromkeys(ArxmlVersion, "/nonexistent/AUTOSAR.xsd")
        )

    def test_an_absent_schema_fails_when_the_flag_is_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._hide_every_schema(monkeypatch)
        monkeypatch.setattr(xsd_support, "REQUIRE_LICENSED_XSD", True)
        # `pytest.fail.Exception`, not `Exception`: pytest's outcomes derive from
        # BaseException, so a plain `pytest.raises(Exception)` here would let the
        # failure through and this test would report the very skip it forbids.
        with pytest.raises(pytest.fail.Exception, match="--require-autosar-xsd"):
            xsd_support.require_licensed_schema(ArxmlVersion.V4_2_2)

    def test_an_absent_schema_skips_when_the_flag_is_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._hide_every_schema(monkeypatch)
        monkeypatch.setattr(xsd_support, "REQUIRE_LICENSED_XSD", False)
        with pytest.raises(pytest.skip.Exception):
            xsd_support.require_licensed_schema(ArxmlVersion.V4_2_2)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_per_version_env_var_is_honoured(self, version: ArxmlVersion, monkeypatch, tmp_path) -> None:
        """A worktree has no ``schemas/`` of its own — this is how it names both."""
        self._hide_every_schema(monkeypatch)
        elsewhere = tmp_path / f"somewhere-else-{version}.xsd"
        elsewhere.write_text("<xs:schema/>", encoding="utf-8")
        monkeypatch.setenv(xsd_support.SCHEMA_PATH_ENV_BY_VERSION[version], str(elsewhere))
        assert xsd_support.licensed_schema(version) == str(elsewhere)


class TestBothVersionsAreCovered:
    """A version added to the product must be added to this gate, or it is unchecked."""

    def test_every_supported_version_has_a_conformance_test(self) -> None:
        assert {str(version) for version in _CONFORMANCE_VERSIONS} == set(SUPPORTED_VERSIONS)


class TestCommittedGoldensConform:
    """The committed 4.2.2 goldens, byte for byte, against the real 4.2.2 schema."""

    @pytest.mark.parametrize("name", GOLDEN_NAMES)
    def test_golden_conforms_to_the_real_4_2_2_schema(self, name: str) -> None:
        _assert_conforms(str(GOLDEN_DIR / f"{name}.arxml"), ArxmlVersion.V4_2_2)


class TestGeneratedOutputConforms:
    """Every golden's model, generated at each version, against that schema; no golden pins 4.3.0."""

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    @pytest.mark.parametrize("name", GOLDEN_NAMES)
    def test_generated_output_conforms(self, name: str, version: ArxmlVersion, tmp_path) -> None:
        output = tmp_path / f"{name}-{version}.arxml"
        generate_arxml(_model_roots(tmp_path)[name], str(output), version=version)
        _assert_conforms(str(output), version)


class TestEveryRecordedTreeConforms:
    """Every tree ``test_writer_fidelity_refreeze.BRANCH_POINT`` lists, at each version, against that schema.

    Most of these trees no golden pins. Validity only; the meaning of a value is its standard citation's.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    @pytest.mark.parametrize("tree", sorted(BRANCH_POINT))
    def test_the_trees_output_conforms(self, tree: str, version: ArxmlVersion, tmp_path: Path) -> None:
        output = tmp_path / f"model-{version}.arxml"
        generate_arxml(str(REPO_ROOT / tree), str(output), version=version)
        _assert_conforms(str(output), version)


class TestTheDataDescriptionModelConforms:
    """The data description fixture tree and ``ProbeSender``'s export, against both schemas.

    The tree states every element kind of the three data description layers and every reference
    field. Schema evidence only: it resolves no reference, knows no RTE and does not check
    ``[constr_1375]``'s category table.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_tree_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        output = tmp_path / f"data-description-{version}.arxml"
        generate_arxml(
            str(REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "data_description" / "model"),
            str(output),
            version=version,
        )
        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_component_export_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        output = tmp_path / f"ProbeSender-{version}.arxml"
        export_component(
            str(REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "data_description" / "model"),
            "ProbeSender",
            str(output),
            version=str(version),
        )
        _assert_conforms(str(output), version)


class TestTheScaleLimitsConform:
    """A compu method's scale limits, against both schemas.

    A ``LOWER-LIMIT`` / ``UPPER-LIMIT`` with its ``INTERVAL-TYPE`` stands on a ``LINEAR`` scale before
    its ``COMPU-RATIONAL-COEFFS`` (``group COMPU-SCALE``, XSD 4.2.2 ``:12990``, ``:12996``; 4.3.0
    ``:14456``, ``:14462``). Schema evidence only: whether the limits are right is
    ``test_scale_limits.py``'s.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_tree_with_scale_limits_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        from test_scale_limits import limits_tree

        output = tmp_path / f"scale-limits-{version}.arxml"
        generate_arxml(str(limits_tree(tmp_path)), str(output), version=version)
        _assert_conforms(str(output), version)


class TestTheTextValueFormConforms:
    """An init of a table-referencing type as ``TEXT-VALUE-SPECIFICATION``, against both schemas.

    Schema evidence only: it does not know that the text names one of the table's entries."""

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_model_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        from test_init_value_judgement import table_typed_model_root

        output = tmp_path / f"text-values-{version}.arxml"
        generate_arxml(str(table_typed_model_root(tmp_path)), str(output), version=version)
        assert b"<TEXT-VALUE-SPECIFICATION>" in output.read_bytes()
        _assert_conforms(str(output), version)


class TestTheApplicationValueFormConforms:
    """An application primitive's value as ``APPLICATION-VALUE-SPECIFICATION``, against both schemas.

    Schema evidence only: it does not know whether the form fits its type."""

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_model_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        from test_application_value_form import application_value_model_root

        output = tmp_path / f"application-values-{version}.arxml"
        generate_arxml(str(application_value_model_root(tmp_path)), str(output), version=version)
        assert b"<APPLICATION-VALUE-SPECIFICATION>" in output.read_bytes()
        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_formatters_text_matches_the_schemas_pattern_for_v(self, version: ArxmlVersion) -> None:
        """The formatter's text matches each schema's ``NUMERICAL-VALUE--SIMPLE`` pattern for ``V``.

        The pattern differs by version (4.3.0 also admits ``0X``, ``0B``, ``A-F`` and ``e``); an XSD
        pattern is anchored at both ends, hence ``fullmatch``."""
        from swcraft_tools.writer.arxml_xml import format_init_number

        schema = etree.parse(xsd_support.require_licensed_schema(version))
        namespace = {"xsd": "http://www.w3.org/2001/XMLSchema"}
        (pattern,) = schema.xpath(
            "//xsd:simpleType[@name='NUMERICAL-VALUE--SIMPLE']/xsd:restriction/xsd:pattern/@value",
            namespaces=namespace,
        )
        for number in (42, -7, 0, 0.0, -0.0, 0.125, -2.5, 1e20, -1e20, 10**20, 1e-7, 1e-300, 100.0):
            text = format_init_number(number)
            assert re.fullmatch(pattern, text), (number, text)


class TestTheSymbolAndPortInitModelConforms:
    """A stated runnable ``symbol`` and an in-place port ``init`` of every shape, against both schemas.

    Schema evidence only: the meaning of a value is its standard citation's.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_model_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        output = tmp_path / f"symbol-and-port-init-{version}.arxml"
        generate_arxml(str(symbol_and_port_init_model_root(tmp_path)), str(output), version=version)
        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_component_export_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        output = tmp_path / f"SignalsSwc-{version}.arxml"
        export_component(
            str(symbol_and_port_init_model_root(tmp_path)), "SignalsSwc", str(output), version=str(version)
        )
        _assert_conforms(str(output), version)


class TestTheStatedNamesModelConforms:
    """Stated behaviour, event and access-point names beside derived ones, against both schemas.

    Schema evidence only: the schema cannot tell a stated name from a derived one
    (``test_stated_names_writer.py`` reads the names themselves).
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_model_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        from test_stated_names_writer import stated_names_model_root

        output = tmp_path / f"stated-names-{version}.arxml"
        generate_arxml(str(stated_names_model_root(tmp_path)), str(output), version=version)
        _assert_conforms(str(output), version)


class TestThePlacedModelConforms:
    """One element of each root-level kind placed in a sub-package, against both schemas.

    Covers the ``ELEMENTS``-before-``AR-PACKAGES`` sequence and a root holding sub-packages only."""

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_placed_model_conforms(self, version: ArxmlVersion, tmp_path) -> None:
        output = tmp_path / f"placed-{version}.arxml"
        generate_arxml(str(placed_model_root(tmp_path)), str(output), version=version)
        _assert_conforms(str(output), version)


class TestTheExecutionTimeBudgetsConform:
    """The ``SWC-TIMING`` of the whole model and of a component export, against both schemas.

    Schema evidence only: the XSD cannot catch a wrong ``CSE-CODE``, a ``GROSS``/``NET`` swap, nor an
    omitted ``CSE-CODE`` or ``CSE-CODE-FACTOR`` (both ``minOccurs="0"``); ``test_swc_timing_writer.py``
    checks those against its literal code table.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_whole_model_export_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        from test_swc_timing_writer import timed_model_root

        output = tmp_path / f"timed-{version}.arxml"
        generate_arxml(str(timed_model_root(tmp_path)), str(output), version=version)
        assert "<SWC-TIMING>" in output.read_text(encoding="utf-8"), "non-vacuity: the element is emitted"
        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_component_export_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        from test_swc_timing_writer import timed_model_root

        output = tmp_path / f"timed-component-{version}.arxml"
        export_component(str(timed_model_root(tmp_path)), "ActuatorSwc", str(output), version=version)
        assert "<SWC-TIMING>" in output.read_text(encoding="utf-8"), "non-vacuity: the element is emitted"
        _assert_conforms(str(output), version)


# The three data-type shapes


def _three_data_type_shapes_model_root(tmp_path: Path) -> Path:
    """A model with a base type, a value type over it and a two-step reference chain.

    Every place a data prototype can name a type names one, so every reference path is
    validated; composite members name mapped application types, so the application
    primitive, its ``UNIT``, ``COMPU-METHOD``, ``DATA-CONSTR`` and the mapping set are too.
    """
    model_root = make_empty_model_root(tmp_path)
    types = [
        {"name": "uint8_base", "kind": "base_type", "size": 8, "encoding": "NONE", "native_declaration": "uint8"},
        {"name": "uint8", "kind": "value_type", "base": "uint8_base", "value_min": 0, "value_max": 200},
        {"name": "Count_T", "kind": "reference_type", "base": "uint8"},
        {"name": "CountRef_T", "kind": "reference_type", "base": "Count_T"},
        # A bare application primitive and one with all three optional fields.
        {"name": "Raw_P", "kind": "application_type"},
        {
            "name": "Count_P",
            "kind": "application_type",
            "unit": "km/h",
            "conversion": {"factor": 0.1, "offset": -40.0},
            "range": {"min": 0.0, "max": 400.0},
        },
        {
            "name": "Rec_T",
            "kind": "record",
            "fields": [{"name": "raw", "type": "Raw_P"}, {"name": "count", "type": "Count_P"}],
        },
        {"name": "Arr_T", "kind": "array", "element_type": "Count_P", "length": 2},
    ]
    for data in types:
        (model_element_path(model_root, data["name"], kind=data["kind"])).write_text(
            yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
        )
    write_model_element(
        model_root,
        {
            "name": "Stored",
            "maps": [
                {"application": "Raw_P", "implementation": "uint8"},
                {"application": "Count_P", "implementation": "CountRef_T"},
            ],
        },
        layer="mapping_sets",
    )
    interfaces = [
        {
            "name": "ICount",
            "kind": "sender_receiver",
            "data_elements": [
                {"name": "raw", "implementation_type": "uint8"},
                {"name": "count", "implementation_type": "CountRef_T", "init": 3},
                {"name": "pair", "application_type": "Arr_T"},
            ],
        },
        {
            "name": "ICalc",
            "kind": "client_server",
            "operations": [
                {
                    "name": "Add",
                    "arguments": [
                        {"name": "a", "direction": "in", "implementation_type": "Count_T"},
                        {"name": "r", "direction": "out", "application_type": "Rec_T"},
                    ],
                }
            ],
        },
    ]
    for data in interfaces:
        (model_root / "interfaces" / f"{data['name']}.yaml").write_text(
            yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
        )
    return model_root


class TestTheThreeDataTypeShapesConform:
    """The three data-type shapes the writer emits, against both schemas.

    The second test is the non-vacuity half: the validated document carries each shape.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_three_shapes_conform(self, version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = _three_data_type_shapes_model_root(tmp_path)
        # A well-formed model, so the schema run validates what a correct model emits.
        from swcraft_tools.validator import check_data_type_references

        assert check_data_type_references(str(model_root)).consistent
        output = tmp_path / f"three-shapes-{version}.arxml"
        generate_arxml(str(model_root), str(output), version=version)
        _assert_conforms(str(output), version)

    def test_the_validated_document_really_carries_the_three_shapes(self, tmp_path: Path) -> None:
        output = tmp_path / "three-shapes.arxml"
        generate_arxml(str(_three_data_type_shapes_model_root(tmp_path)), str(output))
        root = etree.parse(str(output)).getroot()

        assert [short_name_of(e) for e in root.iter(ar_qname("SW-BASE-TYPE"))] == ["uint8_base"]
        by_category: dict[str, list[etree._Element]] = {}
        for element in root.iter(ar_qname("IMPLEMENTATION-DATA-TYPE")):
            by_category.setdefault(element.findtext(ar_qname("CATEGORY")), []).append(element)
        assert sorted(by_category) == ["TYPE_REFERENCE", "VALUE"]

        (value,) = by_category["VALUE"]
        assert short_name_of(value) == "uint8"
        assert [ref.text for ref in value.iter(ar_qname("BASE-TYPE-REF"))] == ["/DataTypes/Base/uint8_base"]
        assert not list(value.iter(ar_qname("IMPLEMENTATION-DATA-TYPE-REF")))

        references = {short_name_of(e): e for e in by_category["TYPE_REFERENCE"]}
        assert sorted(references) == ["CountRef_T", "Count_T"]
        for name, base in (
            ("Count_T", "/DataTypes/Implementation/uint8"),
            ("CountRef_T", "/DataTypes/Implementation/Count_T"),
        ):
            refs = list(references[name].iter(ar_qname("IMPLEMENTATION-DATA-TYPE-REF")))
            assert [(ref.get("DEST"), ref.text) for ref in refs] == [("IMPLEMENTATION-DATA-TYPE", base)]
            assert not list(references[name].iter(ar_qname("BASE-TYPE-REF")))

        assert all(ref.get("DEST") != "SW-BASE-TYPE" for ref in root.iter(ar_qname("TYPE-TREF")))
        assert not [name for name in (short_name_of(e) for e in root.iter()) if name and name.endswith("_impl")]
        assert list(root.iter(ar_qname("CONSTANT-SPECIFICATION"))), "the init under the chain is emitted"


# The reader fixtures

#: The version each schema filename a fixture may declare stands for.
_VERSION_BY_SCHEMA_FILENAME = {version.schema_filename: version for version in ArxmlVersion}

#: The reader fixtures that conform to the schema their ``xsi:schemaLocation`` declares
#: (``test_arxml_fixtures.py`` checks only the declared filename).
READER_FIXTURES_CONFORMING = frozenset({"composition_nested.arxml", "init_values.arxml"})

#: The reader fixtures that do not conform, each with its measured diagnostic count (a record, not
#: asserted). Debt: each was hand-crafted for a reader behaviour, and a fix moves the reader
#: expectations built on its bytes.
READER_FIXTURES_NONCONFORMING: dict[str, int] = {
    "client_server.arxml": 2,  # OPERATION-IREF child order
    "data_types.arxml": 5,  # IMPLEMENTATION-DATA-TYPE content
    "data_types_4_3_0.arxml": 5,  # the same document at 4.3.0
    "minimal_swc.arxml": 1,  # DESC after SW-DATA-DEF-PROPS in an IMPLEMENTATION-DATA-TYPE
    "minimal_swc_4_3_0.arxml": 1,  # the same document at 4.3.0
    "unsupported_content.arxml": 3,  # PR-PORT-PROTOTYPE child order
}


class TestCommittedReaderFixturesConform:
    """Every reader fixture is on exactly one list, validated only at the version it declares."""

    def test_the_two_lists_partition_the_fixture_directory(self) -> None:
        assert READER_FIXTURES_CONFORMING.isdisjoint(READER_FIXTURES_NONCONFORMING)
        assert READER_FIXTURES_CONFORMING | set(READER_FIXTURES_NONCONFORMING) == set(FIXTURE_SCHEMA_FILENAMES), (
            "a reader fixture is on neither list, or on a list without being a fixture"
        )

    @pytest.mark.parametrize("name", sorted(READER_FIXTURES_CONFORMING))
    def test_a_conforming_fixture_conforms_to_its_declared_schema(self, name: str) -> None:
        version = _VERSION_BY_SCHEMA_FILENAME[FIXTURE_SCHEMA_FILENAMES[name]]
        _assert_conforms(str(FIXTURES_DIR / name), version)

    @pytest.mark.parametrize("name", sorted(READER_FIXTURES_NONCONFORMING))
    def test_a_recorded_nonconforming_fixture_still_does_not_conform(self, name: str) -> None:
        """A fixture made valid must move to the conforming set, or it goes unguarded."""
        version = _VERSION_BY_SCHEMA_FILENAME[FIXTURE_SCHEMA_FILENAMES[name]]
        schema = xsd_support.require_licensed_schema(version)
        outcome = check_arxml(str(FIXTURES_DIR / name), schema_path=schema)
        assert outcome.mode == MODE_XSD
        assert outcome.schema_error is False, outcome.messages
        assert not outcome.valid, f"{name} now conforms: move it to READER_FIXTURES_CONFORMING"


@pytest.fixture(scope="module")
def composite_output(tmp_path_factory) -> etree._Element:
    """The composite golden's model, generated once, parsed."""
    output = tmp_path_factory.mktemp("composite") / "model.arxml"
    generate_arxml(str(GOLDEN_MODEL_ROOTS["composite-4.2.2"]), str(output))
    return etree.parse(str(output)).getroot()


class TestTheCompositeShapeIsWhatTheSchemasRejected:
    """A composite member's ``TYPE-TREF`` names an emitted ``APPLICATION-*`` type, as both schemas require.

    A right ``DEST`` over a reference text naming nothing passes the schema, so the target is checked too.
    """

    #: The two parents whose ``TYPE-TREF`` the schemas restrict to ``APPLICATION-*``.
    COMPOSITE_MEMBER_TAGS = ("APPLICATION-RECORD-ELEMENT", "ELEMENT")

    def _member_refs(self, root: etree._Element) -> list[tuple[str, str]]:
        """``(DEST, path)`` of every composite member's ``TYPE-TREF``."""
        return [
            (ref.get("DEST"), ref.text)
            for tag in self.COMPOSITE_MEMBER_TAGS
            for member in root.iter(ar_qname(tag))
            for ref in member.findall(ar_qname("TYPE-TREF"))
        ]

    def test_no_composite_member_references_an_implementation_data_type(self, composite_output) -> None:
        refs = self._member_refs(composite_output)
        assert refs, "the composite model must contain at least one record field or array element"
        assert [ref for ref in refs if not ref[0].startswith("APPLICATION-")] == []

    def test_every_referenced_member_type_exists_and_is_mapped(self, composite_output) -> None:
        """A corrected ``DEST`` over a path that names nothing would still be false."""
        emitted = {short_name_of(element) for element in composite_output.iter() if short_name_of(element)}
        mapped = {
            entry.findtext(ar_qname("APPLICATION-DATA-TYPE-REF")): entry.findtext(
                ar_qname("IMPLEMENTATION-DATA-TYPE-REF")
            )
            for entry in composite_output.iter(ar_qname("DATA-TYPE-MAP"))
        }
        # Only the primitives: no composite is mapped, so a map for every member
        # would demand a pair the model cannot declare.
        primitives = [
            path for dest, path in self._member_refs(composite_output) if dest == "APPLICATION-PRIMITIVE-DATA-TYPE"
        ]
        assert primitives, "the composite model must exercise at least one application primitive member"
        for path in primitives:
            assert path.rsplit("/", 1)[-1] in emitted, f"{path} is referenced but never emitted"

        declared = [path for path in primitives if path in mapped]
        assert declared, "the composite model must declare at least one map for a member's type"
        for path in declared:
            assert mapped[path].rsplit("/", 1)[-1] in emitted

    def test_every_mapping_set_is_one_the_model_declares(self, composite_output) -> None:
        """Every emitted mapping set is one the model declares; the writer invents none."""
        declared = {mapping_set.name for mapping_set in read_mapping_sets(str(GOLDEN_MODEL_ROOTS["composite-4.2.2"]))}
        names = {short_name_of(element) for element in composite_output.iter(ar_qname("DATA-TYPE-MAPPING-SET"))}
        assert names and names <= declared


# The name rule, read back out of the schemas that define it.

#: The XML Schema namespace, for reading the licensed XSDs' facets as documents.
XSD_NS = "http://www.w3.org/2001/XMLSchema"

#: The simple type every ``SHORT-NAME`` is declared as: the XSDs split each primitive into a
#: complex type and a ``--SIMPLE`` restriction, and only the second carries the facets.
IDENTIFIER_SIMPLE_TYPE = "IDENTIFIER--SIMPLE"

#: The simple type every ``*-REF`` path is declared as.
REF_SIMPLE_TYPE = "REF--SIMPLE"

#: The simple type a ``RUNNABLE-ENTITY``'s ``SYMBOL`` is declared as.
C_IDENTIFIER_SIMPLE_TYPE = "C-IDENTIFIER--SIMPLE"


@functools.cache
def _named_simple_types(schema_path: str) -> dict[str, dict[str, str]]:
    """``{simpleType name: {facet local name: facet value}}`` for one licensed schema (~5 MB, cached)."""
    root = etree.parse(schema_path).getroot()
    types: dict[str, dict[str, str]] = {}
    for simple_type in root.iter(f"{{{XSD_NS}}}simpleType"):
        name = simple_type.get("name")
        restriction = simple_type.find(f"{{{XSD_NS}}}restriction")
        if name is None or restriction is None:
            continue
        types[name] = {
            etree.QName(facet).localname: facet.get("value")
            for facet in restriction
            if isinstance(facet.tag, str) and facet.get("value") is not None
        }
    return types


def _facets(version: ArxmlVersion, type_name: str) -> dict[str, str]:
    """The declared facets of *type_name* in *version*'s licensed schema."""
    types = _named_simple_types(require_licensed_schema(version))
    assert type_name in types, f"{type_name} is not declared by the AUTOSAR {version} schema"
    return types[type_name]


class TestTheIdentifierRuleIsWhatTheSchemasDeclare:
    """:mod:`swcraft_tools.identifier` is a literal copy of the schemas' facets.

    The pattern is stored unanchored so it compares literally: stripping anchors would hide a difference.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_pattern_facet_is_our_constant(self, version: ArxmlVersion) -> None:
        assert _facets(version, IDENTIFIER_SIMPLE_TYPE)["pattern"] == IDENTIFIER_PATTERN

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_max_length_facet_is_our_constant(self, version: ArxmlVersion) -> None:
        assert int(_facets(version, IDENTIFIER_SIMPLE_TYPE)["maxLength"]) == MAX_IDENTIFIER_LENGTH

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_c_identifier_pattern_facet_is_our_constant(self, version: ArxmlVersion) -> None:
        """A runnable's stated ``symbol`` is judged by this pattern."""
        assert _facets(version, C_IDENTIFIER_SIMPLE_TYPE)["pattern"] == C_IDENTIFIER_PATTERN

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_rule_has_exactly_those_two_halves(self, version: ArxmlVersion) -> None:
        """A third facet would be a constraint ``check_identifier`` silently ignores."""
        assert set(_facets(version, IDENTIFIER_SIMPLE_TYPE)) == {"maxLength", "pattern"}

    def test_both_schemas_declare_the_same_rule(self) -> None:
        """One rule covers both versions only for as long as this holds."""
        declared = [_facets(version, IDENTIFIER_SIMPLE_TYPE) for version in _CONFORMANCE_VERSIONS]
        assert declared.count(declared[0]) == len(declared)


class TestTheReferenceRuleIsPerSegment:
    """Checking each ``SHORT-NAME`` suffices to make every ``*-REF`` legal.

    ``REF--SIMPLE`` constrains each path segment by the identifier rule and caps no total length.
    """

    #: The identifier pattern as one bounded path segment: ``maxLength`` pushed into the
    #: pattern as ``{0,127}``.
    SEGMENT = f"{IDENTIFIER_PATTERN.removesuffix('*')}{{0,{MAX_IDENTIFIER_LENGTH - 1}}}"

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_ref_pattern_is_the_identifier_rule_repeated_per_segment(self, version: ArxmlVersion) -> None:
        assert _facets(version, REF_SIMPLE_TYPE)["pattern"] == f"/?{self.SEGMENT}(/{self.SEGMENT})*"

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_a_reference_path_carries_no_length_cap_of_its_own(self, version: ArxmlVersion) -> None:
        """The 128 applies per segment; a deep path is legal however long it gets."""
        assert set(_facets(version, REF_SIMPLE_TYPE)) == {"pattern"}

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_a_path_of_conforming_names_conforms(self, version: ArxmlVersion) -> None:
        pattern = re.compile(_facets(version, REF_SIMPLE_TYPE)["pattern"])
        names = ["DataTypes", "uint8_impl", "A" * MAX_IDENTIFIER_LENGTH]
        assert all(check_identifier(name) is None for name in names)
        assert pattern.fullmatch("/" + "/".join(names))

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_one_non_conforming_segment_invalidates_the_whole_path(self, version: ArxmlVersion) -> None:
        pattern = re.compile(_facets(version, REF_SIMPLE_TYPE)["pattern"])
        assert pattern.fullmatch("/ComponentTypes/My SWC") is None
        assert pattern.fullmatch("/ComponentTypes/" + "A" * (MAX_IDENTIFIER_LENGTH + 1)) is None


class TestSymbolIsStrictlyWeakerAndNeedsNoCheckOfItsOwn:
    """``RUNNABLE-ENTITY/SYMBOL`` is a ``C-IDENTIFIER``, which accepts every identifier.

    So the runnable name written as a symbol needs no second check; a stated ``symbol`` is
    judged by ``identifier.check_c_identifier``.
    """

    @staticmethod
    def _symbol_types(version: ArxmlVersion, group_name: str) -> list[str]:
        root = etree.parse(require_licensed_schema(version)).getroot()
        return [
            element.get("type")
            for group in root.iter(f"{{{XSD_NS}}}group")
            if group.get("name") == group_name
            for element in group.iter(f"{{{XSD_NS}}}element")
            if element.get("name") == "SYMBOL"
        ]

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_symbol_the_writer_emits_is_a_c_identifier(self, version: ArxmlVersion) -> None:
        assert self._symbol_types(version, "RUNNABLE-ENTITY") == ["AR:C-IDENTIFIER"]

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_c_identifier_caps_no_length(self, version: ArxmlVersion) -> None:
        assert set(_facets(version, C_IDENTIFIER_SIMPLE_TYPE)) == {"pattern"}

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_every_identifier_is_also_a_c_identifier(self, version: ArxmlVersion) -> None:
        pattern = re.compile(_facets(version, C_IDENTIFIER_SIMPLE_TYPE)["pattern"])
        for name in ("A", "Runnable_1", "A" * MAX_IDENTIFIER_LENGTH):
            assert check_identifier(name) is None
            assert pattern.fullmatch(name), f"{name} is an Identifier but not a C-Identifier"

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_c_identifier_is_the_weaker_of_the_two(self, version: ArxmlVersion) -> None:
        """Both halves of the rule are relaxed there, which is why they cannot be merged."""
        pattern = re.compile(_facets(version, C_IDENTIFIER_SIMPLE_TYPE)["pattern"])
        too_long = "A" * (MAX_IDENTIFIER_LENGTH + 1)
        for name in ("_leading", too_long):
            assert pattern.fullmatch(name) is not None
            assert check_identifier(name) is not None


def _minimal_document(version: ArxmlVersion) -> str:
    """The empty golden's text retargeted at *version*'s schema; its ``SHORT-NAME``s are unreferenced."""
    text = (GOLDEN_DIR / "empty-4.2.2.arxml").read_text(encoding="utf-8")
    return text.replace(ArxmlVersion.V4_2_2.schema_filename, version.schema_filename)


#: The element whose text every substitution below replaces.
_SUBSTITUTED_SHORT_NAME = "<SHORT-NAME>DataTypes</SHORT-NAME>"


def _document_named(version: ArxmlVersion, short_name: str) -> str:
    return _minimal_document(version).replace(_SUBSTITUTED_SHORT_NAME, f"<SHORT-NAME>{short_name}</SHORT-NAME>", 1)


def _validate_text(tmp_path: Path, version: ArxmlVersion, text: str) -> ValidationOutcome:
    """XSD-validate a document held as a string against *version*'s real schema."""
    schema = require_licensed_schema(version)
    document = tmp_path / f"substituted-{version}.arxml"
    document.write_text(text, encoding="utf-8")
    outcome = check_arxml(str(document), schema_path=schema)
    assert outcome.mode == MODE_XSD
    assert outcome.schema_error is False, f"{schema} could not be compiled: {outcome.messages}"
    return outcome


#: Names the rule rejects on its **pattern** half, one per reason a human hits.
#: ``"My SWC"`` is ``create_swc(model_root, "My SWC")`` reduced to the document it produced.
NON_CONFORMING_SHORT_NAMES = ["My SWC", "1Speed", "_Speed", "Speed-1", "Speed.1", "", "Ünicode", "Speed\tT"]


class TestTheSchemasRejectANonConformingShortName:
    """The licensed schemas reject what the rule rejects, on substituted documents the writer would refuse."""

    def test_the_substitution_target_is_unambiguous(self) -> None:
        """Guards every test below: a silent no-op substitution would pass them all."""
        assert _minimal_document(ArxmlVersion.V4_2_2).count(_SUBSTITUTED_SHORT_NAME) == 1

    def test_the_untouched_document_is_valid_at_both_versions(self, tmp_path: Path) -> None:
        """The control: the substitution is what makes the document invalid, not the retarget."""
        for version in _CONFORMANCE_VERSIONS:
            assert _validate_text(tmp_path, version, _minimal_document(version)).valid

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    @pytest.mark.parametrize("short_name", NON_CONFORMING_SHORT_NAMES, ids=repr)
    def test_the_schema_rejects_it_and_names_the_element(
        self, short_name: str, version: ArxmlVersion, tmp_path: Path
    ) -> None:
        outcome = _validate_text(tmp_path, version, _document_named(version, short_name))
        assert not outcome.valid, f"the AUTOSAR {version} schema accepted SHORT-NAME {short_name!r}"
        assert "SHORT-NAME" in "\n".join(outcome.messages)

    @pytest.mark.parametrize("short_name", NON_CONFORMING_SHORT_NAMES, ids=repr)
    def test_our_rule_agrees_with_the_schema(self, short_name: str) -> None:
        """The oracle's other half — the schema's verdict is the rule's verdict."""
        assert check_identifier(short_name) is not None

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_length_facet_is_pinned_by_the_schema_and_not_only_by_our_constant(
        self, version: ArxmlVersion, tmp_path: Path
    ) -> None:
        too_long = "A" * (MAX_IDENTIFIER_LENGTH + 1)
        outcome = _validate_text(tmp_path, version, _document_named(version, too_long))
        assert not outcome.valid, f"the AUTOSAR {version} schema accepted a {len(too_long)}-character SHORT-NAME"
        assert "SHORT-NAME" in "\n".join(outcome.messages)
        assert check_identifier(too_long) is not None


#: Names at the edges the rule allows, including generated forms the writer's suffixes drive.
CONFORMING_SHORT_NAMES = [
    "A",
    "a",
    "A" * MAX_IDENTIFIER_LENGTH,
    "Speed_1",
    "uint8_impl",
    "Mode_CompuMethod",
    "A" * (MAX_IDENTIFIER_LENGTH - len("_InternalBehavior")) + "_InternalBehavior",
]


class TestTheSchemasAcceptEveryNameTheRuleAllows:
    """The positive control, without which the rejection tests prove only strictness."""

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    @pytest.mark.parametrize("short_name", CONFORMING_SHORT_NAMES, ids=repr)
    def test_a_conforming_short_name_validates(self, short_name: str, version: ArxmlVersion, tmp_path: Path) -> None:
        outcome = _validate_text(tmp_path, version, _document_named(version, short_name))
        assert outcome.valid, "\n".join((f"the AUTOSAR {version} schema rejected {short_name!r}:", *outcome.messages))

    @pytest.mark.parametrize("short_name", CONFORMING_SHORT_NAMES, ids=repr)
    def test_our_rule_agrees_with_the_schema(self, short_name: str) -> None:
        assert check_identifier(short_name) is None

    def test_the_boundary_is_on_the_legal_side_at_exactly_the_limit(self) -> None:
        """128 in, 129 out."""
        assert check_identifier("A" * MAX_IDENTIFIER_LENGTH) is None
        assert check_identifier("A" * (MAX_IDENTIFIER_LENGTH + 1)) is not None


def _at_the_limit(prefix: str, affix: str = "") -> str:
    """A name whose emitted ``{name}{affix}`` form is exactly ``MAX_IDENTIFIER_LENGTH``."""
    return prefix + "A" * (MAX_IDENTIFIER_LENGTH - len(affix) - len(prefix))


#: The ``{Interface}_{Element}_Init`` form's joint budget: an interface name and each of its
#: data element names share what ``_`` and ``_Init`` leave of the 128.
JOINT_BUDGET = MAX_IDENTIFIER_LENGTH - len(INIT_CONSTANT_SUFFIX) - 1


#: The access-point forms' joint budgets: a port name and its member share what the prefix
#: and ``_`` leave of the 128.
ACCESS_BUDGETS = {
    prefix: MAX_IDENTIFIER_LENGTH - len(prefix) - 1
    for prefix in (READ_ACCESS_PREFIX, WRITE_ACCESS_PREFIX, CALL_POINT_PREFIX)
}


def _pair_summing_to(budget: int, first: str, second: str) -> tuple[str, str]:
    """Two names whose lengths sum to exactly *budget*, seeded so they read."""
    first_length = budget // 2
    return (
        first + "A" * (first_length - len(first)),
        second + "A" * (budget - first_length - len(second)),
    )


#: The seeds :func:`_boundary_model_root` builds each access pair from.
_ACCESS_SEEDS = {
    READ_ACCESS_PREFIX: ("PortRd", "AccElem"),
    WRITE_ACCESS_PREFIX: ("PortWr", "AccElem"),
    CALL_POINT_PREFIX: ("PortCall", "CallOp"),
}


def _pair_at_the_access_limit(prefix: str, port: str, member: str) -> tuple[str, str]:
    """A port name and a member name whose access point is exactly 128 long."""
    return _pair_summing_to(ACCESS_BUDGETS[prefix], port, member)


def _pair_at_the_joint_limit(first: str, second: str) -> tuple[str, str]:
    """Two names summing to exactly :data:`JOINT_BUDGET`, so their init constant is 128."""
    return _pair_summing_to(JOINT_BUDGET, first, second)


def _boundary_model_root(tmp_path: Path) -> Path:
    """A model whose binding declared and generated names are exactly at the limit, built by the mutations.

    A name of a form with a joint budget is not at the limit; the derived name is. The interface
    and its data element are 61 each (``{Interface}_{Element}_Init``), and the access-point ports
    and members share their prefix's budget; ``IAcc`` and ``ICall`` are short so their own init
    constants stay inside the limit, and ``PrimBase``, ``Rec`` and the requirement are short helpers.
    The one processor is there for ``check-model``: no hardware name is emitted.
    """
    model_root = tmp_path / "boundary-model"
    for layer in layer_directories(required_only=True):
        (model_root / layer).mkdir(parents=True, exist_ok=True)
    root = str(model_root)

    interface_name, element_name = _pair_at_the_joint_limit("Iface", "Elem")
    read_port, access_member = _pair_at_the_access_limit(READ_ACCESS_PREFIX, *_ACCESS_SEEDS[READ_ACCESS_PREFIX])
    write_port, _ = _pair_at_the_access_limit(WRITE_ACCESS_PREFIX, *_ACCESS_SEEDS[WRITE_ACCESS_PREFIX])
    call_port, call_operation = _pair_at_the_access_limit(CALL_POINT_PREFIX, *_ACCESS_SEEDS[CALL_POINT_PREFIX])
    # `_Compu` and `_Range` are one character longer than `_Unit`, so they bind.
    physical = _at_the_limit("Phys", COMPU_SUFFIX)
    # A value type generates no form of its own.
    primitive = _at_the_limit("Prim")
    enum = _at_the_limit("Enum", COMPU_METHOD_SUFFIX)
    producer = _at_the_limit("Producer", INTERNAL_BEHAVIOR_SUFFIX)
    consumer = _at_the_limit("Consumer", INTERNAL_BEHAVIOR_SUFFIX)
    timed = _at_the_limit("Timed", TIMING_EVENT_PREFIX)
    inited = _at_the_limit("Inited", INIT_EVENT_PREFIX)

    create_hardware_element(root, "CPU", "processor")
    # A value type over a short base type: a base type may type no prototype.
    create_data_type(root, "PrimBase", "base_type", size=32, encoding="IEEE754", native_declaration="float")
    create_data_type(root, primitive, "value_type", base="PrimBase")
    create_data_type(root, enum, "enum", literals=[{"name": _at_the_limit("Lit"), "value": 0}])
    # All three optional fields: each generated form is emitted only for its field.
    write_model_element(
        Path(root),
        {
            "name": physical,
            "kind": "application_type",
            "unit": "m",
            "conversion": {"factor": 0.1, "offset": 0.0},
            "range": {"min": 0.0, "max": 400.0},
        },
    )
    (model_element_path(Path(root), _at_the_limit("Set"), layer="mapping_sets")).write_text(
        yaml.safe_dump(
            {
                "name": _at_the_limit("Set"),
                "maps": [{"application": physical, "implementation": primitive}],
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
        newline="\n",
    )
    # a record field is what makes the application type really get referenced
    create_data_type(root, "Rec", "record", fields=[{"name": _at_the_limit("Field"), "type": physical}])
    # The `init` makes the CONSTANT-SPECIFICATION get emitted.
    create_interface(root, interface_name, [{"name": element_name, "implementation_type": primitive, "init": 1.0}])
    create_interface(root, "IAcc", [{"name": access_member, "implementation_type": primitive}])
    create_cs_interface(root, "ICall", [{"name": call_operation, "arguments": []}])
    # Both declare `no_support`: a component without it is refused.
    declare_no_support(create_swc(root, producer, "grp"))
    declare_no_support(create_swc(root, consumer, "grp"))
    add_port(root, producer, _at_the_limit("PortP"), "provide", interface_name)
    add_port(root, consumer, _at_the_limit("PortR"), "require", interface_name)
    add_port(root, consumer, read_port, "require", "IAcc")
    add_port(root, consumer, write_port, "provide", "IAcc")
    add_port(root, consumer, call_port, "require", "ICall")
    add_runnable(root, producer, timed)
    add_timing_event(root, producer, timed, 0.01)
    add_runnable(
        root,
        consumer,
        inited,
        reads=[{"port": read_port, "element": access_member}],
        writes=[{"port": write_port, "element": access_member}],
        calls=[{"port": call_port, "operation": call_operation}],
    )
    add_init_event(root, consumer, inited)
    create_connector(
        root,
        _at_the_limit("Conn"),
        producer,
        _at_the_limit("PortP"),
        consumer,
        _at_the_limit("PortR"),
    )
    # Every composition SHORT-NAME at 128; member names differ from their types so
    # the prototype and its TYPE-TREF carry different 128-character names.
    _root_composition(
        model_root,
        [
            {"name": _at_the_limit("MemP"), "type": producer},
            {"name": _at_the_limit("MemC"), "type": consumer},
        ],
        name=_at_the_limit("Root"),
        ports=[
            {
                "name": _at_the_limit("PortB"),
                "direction": "require",
                "interface": interface_name,
            }
        ],
        delegations=[
            {
                "name": _at_the_limit("Deleg"),
                "port": _at_the_limit("PortB"),
                "member": _at_the_limit("MemC"),
                "member_port": _at_the_limit("PortR"),
            }
        ],
    )
    (model_root / "requirements" / "REQ_BOUNDARY_001.yaml").write_text(
        "id: REQ_BOUNDARY_001\n"
        "title: Boundary requirement\n"
        "description: The boundary-named components satisfy it.\n"
        "req_type: req\n"
        "version: 1\n"
        "source: architecture\n",
        encoding="utf-8",
        newline="\n",
    )
    for component in (producer, consumer):
        add_trace_link(root, "REQ_BOUNDARY_001", component)
    return model_root


class TestTheHeadlineAcceptance:
    """A boundary model ``check-model`` accepts generates ARXML both schemas accept.

    Every binding generated form is exactly 128 characters, so a guard measuring a budget the
    emitter does not spend is caught by the schema rather than by our own arithmetic.
    """

    def test_the_boundary_model_is_accepted_by_check_model(self, tmp_path: Path) -> None:
        result = check_cross_layer_consistency(str(_boundary_model_root(tmp_path)))

        assert result.consistent, result.issues

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_boundary_model_generates_conforming_arxml(self, version: ArxmlVersion, tmp_path: Path) -> None:
        output = tmp_path / f"boundary-{version}.arxml"

        generate_arxml(str(_boundary_model_root(tmp_path)), str(output), version=version)

        _assert_conforms(str(output), version)

    @staticmethod
    def _emitted_short_names(tmp_path: Path) -> set[str]:
        output = tmp_path / "boundary.arxml"
        generate_arxml(str(_boundary_model_root(tmp_path)), str(output))
        return set(re.findall(r"<SHORT-NAME>([^<]+)</SHORT-NAME>", output.read_text(encoding="utf-8")))

    @pytest.mark.parametrize(
        ("affix", "is_prefix"),
        [
            (COMPU_METHOD_SUFFIX, False),
            (IMPLEMENTATION_SUFFIX, False),
            (INTERNAL_BEHAVIOR_SUFFIX, False),
            (TIMING_EVENT_PREFIX, True),
            (INIT_EVENT_PREFIX, True),
            (INIT_CONSTANT_SUFFIX, False),
            (READ_ACCESS_PREFIX, True),
            (WRITE_ACCESS_PREFIX, True),
            (CALL_POINT_PREFIX, True),
            (UNIT_SUFFIX, False),
            (COMPU_SUFFIX, False),
            (RANGE_SUFFIX, False),
        ],
        ids=repr,
    )
    def test_every_generated_form_is_really_emitted(self, affix: str, is_prefix: bool, tmp_path: Path) -> None:
        """Non-vacuity: every form the writer generates reaches the validated document."""
        carriers = [
            name
            for name in self._emitted_short_names(tmp_path)
            if (name.startswith(affix) if is_prefix else name.endswith(affix))
        ]

        assert carriers, f"the boundary model emits no SHORT-NAME carrying {affix!r}"

    #: The binding affix of each element kind: the longest form it generates, which alone can
    #: land on ``MAX_IDENTIFIER_LENGTH`` (a shorter form of the same name cannot). ``_Compu``
    #: and ``_Range`` are equally long, so both bind; the last four bind a pair of names.
    _BINDING_AFFIXES = (
        (COMPU_METHOD_SUFFIX, False),
        (COMPU_SUFFIX, False),
        (RANGE_SUFFIX, False),
        (INTERNAL_BEHAVIOR_SUFFIX, False),
        (TIMING_EVENT_PREFIX, True),
        (INIT_EVENT_PREFIX, True),
        (INIT_CONSTANT_SUFFIX, False),
        (READ_ACCESS_PREFIX, True),
        (WRITE_ACCESS_PREFIX, True),
        (CALL_POINT_PREFIX, True),
    )

    @pytest.mark.parametrize(("affix", "is_prefix"), _BINDING_AFFIXES, ids=repr)
    def test_the_binding_generated_forms_land_exactly_on_the_limit(
        self, affix: str, is_prefix: bool, tmp_path: Path
    ) -> None:
        """Non-vacuity: each binding form is emitted at exactly the limit, not merely below it."""
        at_the_limit = [
            name
            for name in self._emitted_short_names(tmp_path)
            if len(name) == MAX_IDENTIFIER_LENGTH and (name.startswith(affix) if is_prefix else name.endswith(affix))
        ]

        assert at_the_limit, (
            f"no emitted SHORT-NAME of exactly {MAX_IDENTIFIER_LENGTH} characters carries {affix!r} — "
            "the boundary model has stopped exercising that generated form"
        )


class TestTheJointBudgetIsTheOneTheEmitterSpends:
    """``{Interface}_{Element}_Init``'s joint budget: on it the schemas accept, one over the writer refuses."""

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_constant_of_a_pair_on_the_budget_is_exactly_at_the_limit(
        self, version: ArxmlVersion, tmp_path: Path
    ) -> None:
        interface_name, element_name = _pair_at_the_joint_limit("Iface", "Elem")
        assert len(interface_name) + len(element_name) == JOINT_BUDGET
        emitted = init_constant_name(interface_name, element_name)
        assert len(emitted) == MAX_IDENTIFIER_LENGTH
        output = tmp_path / f"joint-{version}.arxml"

        generate_arxml(str(_boundary_model_root(tmp_path)), str(output), version=version)

        assert f"<SHORT-NAME>{emitted}</SHORT-NAME>" in output.read_text(encoding="utf-8")
        _assert_conforms(str(output), version)

    def test_one_character_more_is_refused_and_leaves_no_document(self, tmp_path: Path) -> None:
        """Not parametrized: the refusal comes before any byte is built, so the version plays no part."""
        model_root = _boundary_model_root(tmp_path)
        interface_name, element_name = _pair_at_the_joint_limit("Iface", "Elem")
        # The element name is lengthened because no port references it.
        over_budget = f"{element_name}A"
        assert check_identifier(over_budget) is None
        interface_file = model_root / "interfaces" / f"{interface_name}.yaml"
        interface_file.write_text(
            interface_file.read_text(encoding="utf-8").replace(element_name, over_budget),
            encoding="utf-8",
            newline="\n",
        )
        output = tmp_path / "over.arxml"

        with pytest.raises(ValueError) as refusal:
            generate_arxml(str(model_root), str(output))

        assert init_constant_name(interface_name, over_budget) in str(refusal.value)
        assert not output.exists(), "a refused model must leave no output file"


class TestTheAccessJointBudgetsAreTheOnesTheEmitterSpends:
    """The access-point forms' joint budgets: on them the schemas accept, one over the writer refuses."""

    @pytest.mark.parametrize("prefix", sorted(ACCESS_BUDGETS), ids=repr)
    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_access_point_of_a_pair_on_the_budget_is_exactly_at_the_limit(
        self, prefix: str, version: ArxmlVersion, tmp_path: Path
    ) -> None:
        port_name, member_name = _pair_at_the_access_limit(prefix, *_ACCESS_SEEDS[prefix])
        assert len(port_name) + len(member_name) == ACCESS_BUDGETS[prefix]
        emitted = access_point_name(prefix, port_name, member_name)
        assert len(emitted) == MAX_IDENTIFIER_LENGTH
        output = tmp_path / f"access-{version}.arxml"

        generate_arxml(str(_boundary_model_root(tmp_path)), str(output), version=version)

        assert f"<SHORT-NAME>{emitted}</SHORT-NAME>" in output.read_text(encoding="utf-8")
        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("prefix", sorted(ACCESS_BUDGETS), ids=repr)
    def test_one_character_more_is_refused_and_leaves_no_document(self, prefix: str, tmp_path: Path) -> None:
        """Not parametrized: the refusal comes before any byte is built.

        The port name is lengthened because it lives in the component's one file; the member is
        resolved by every com-spec of its interface."""
        model_root = _boundary_model_root(tmp_path)
        port_name, member_name = _pair_at_the_access_limit(prefix, *_ACCESS_SEEDS[prefix])
        over_budget = f"{port_name}A"
        assert check_identifier(over_budget) is None
        swc_file = model_root / "swc" / "grp" / f"{_at_the_limit('Consumer', INTERNAL_BEHAVIOR_SUFFIX)}.yaml"
        swc_file.write_text(
            swc_file.read_text(encoding="utf-8").replace(port_name, over_budget),
            encoding="utf-8",
            newline="\n",
        )
        output = tmp_path / "over.arxml"

        with pytest.raises(ValueError) as refusal:
            generate_arxml(str(model_root), str(output))

        assert access_point_name(prefix, over_budget, member_name) in str(refusal.value)
        assert not output.exists(), "a refused model must leave no output file"


# The init-value root, and the position the schemas really police


class TestTheInitValueRootConforms:
    """``fixtures/init_values/model``, the tree carrying every init-value shape, against both schemas.

    It holds both COM-SPEC ``INIT-VALUE`` positions; it is used in place and is not a golden.
    """

    def test_the_root_is_accepted_by_check_model(self) -> None:
        """The oracles below are only worth something over a model a user could author."""
        result = check_cross_layer_consistency(str(INIT_VALUES_MODEL_ROOT))

        assert result.consistent, result.issues

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_root_generates_conforming_arxml(self, version: ArxmlVersion, tmp_path: Path) -> None:
        output = tmp_path / f"init-values-{version}.arxml"

        generate_arxml(str(INIT_VALUES_MODEL_ROOT), str(output), version=version)

        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_conforming_document_really_carries_all_three_owners(
        self, version: ArxmlVersion, tmp_path: Path
    ) -> None:
        """Non-vacuity: the schemas make all three ``INIT-VALUE`` owners optional, so each is asserted."""
        output = tmp_path / f"init-values-{version}.arxml"
        generate_arxml(str(INIT_VALUES_MODEL_ROOT), str(output), version=version)
        root = etree.parse(str(output)).getroot()

        owners = {
            parent.tag.rpartition("}")[2]
            for init_value in root.iter(ar_qname("INIT-VALUE"))
            for parent in (init_value.getparent(),)
        }

        assert owners == {
            "VARIABLE-DATA-PROTOTYPE",
            "NONQUEUED-SENDER-COM-SPEC",
            "NONQUEUED-RECEIVER-COM-SPEC",
        }
        assert [short_name_of(element) for element in root.iter(ar_qname("CONSTANT-SPECIFICATION"))]


def _record_over_array_model_root(tmp_path: Path) -> Path:
    """A root whose one value is a record over an array over a record, built by the mutations.

    ``fixtures/init_values/model`` declares no record with an array-typed field, so its round-trip
    oracle does not see this shape. This
    is the only place a licensed schema sees the nested value specification.
    """
    model_root = tmp_path / "record-over-array-model"
    for layer in layer_directories(required_only=True):
        (model_root / layer).mkdir(parents=True, exist_ok=True)
    root = str(model_root)

    create_hardware_element(root, "CPU", "processor")
    # `check-model` requires a root composition.
    _root_composition(model_root)
    # A record field names an application type; `create_data_type` builds none, so its file is written.
    create_data_type(root, "Index_T_base", "base_type", size=8, encoding="NONE", native_declaration="uint8")
    create_data_type(root, "Index_T", "value_type", base="Index_T_base")
    (model_element_path(model_root, "Index_P", kind="application_type")).write_text(
        "name: Index_P\nkind: application_type\n", encoding="utf-8", newline="\n"
    )
    write_model_element(
        model_root,
        {"name": "Stored", "maps": [{"application": "Index_P", "implementation": "Index_T"}]},
        layer="mapping_sets",
    )
    create_data_type(root, "Row_T", "record", fields=[{"name": "index", "type": "Index_P"}])
    create_data_type(root, "Rows_T", "array", element_type="Row_T", length=2)
    create_data_type(root, "Deep_T", "record", fields=[{"name": "rows", "type": "Rows_T"}])
    create_interface(
        root,
        "IDeep",
        [{"name": "deep", "application_type": "Deep_T", "init": {"rows": [{"index": 1}, {"index": 2}]}}],
    )
    return model_root


class TestARecordWithAnArrayFieldConforms:
    """A record over an array over a record init value, against both schemas (no importer involved)."""

    def test_the_root_is_accepted_by_check_model(self, tmp_path: Path) -> None:
        result = check_cross_layer_consistency(str(_record_over_array_model_root(tmp_path)))

        assert result.consistent, result.issues

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_nested_value_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        output = tmp_path / f"record-over-array-{version}.arxml"

        generate_arxml(str(_record_over_array_model_root(tmp_path)), str(output), version=version)

        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_validated_document_really_carries_the_three_levels(
        self, version: ArxmlVersion, tmp_path: Path
    ) -> None:
        """Non-vacuity: ``VALUE-SPEC``'s content is a choice, so a flattened record would still conform."""
        output = tmp_path / f"record-over-array-{version}.arxml"
        generate_arxml(str(_record_over_array_model_root(tmp_path)), str(output), version=version)
        root = etree.parse(str(output)).getroot()

        constant = next(iter(root.iter(ar_qname("CONSTANT-SPECIFICATION"))))
        outer = constant.find(ar_qname("VALUE-SPEC")).find(ar_qname("RECORD-VALUE-SPECIFICATION"))
        array = outer.find(ar_qname("FIELDS")).find(ar_qname("ARRAY-VALUE-SPECIFICATION"))
        inner = array.find(ar_qname("ELEMENTS")).findall(ar_qname("RECORD-VALUE-SPECIFICATION"))

        assert short_name_of(constant) == "IDeep_deep_Init"
        assert len(inner) == 2
        assert [
            leaf.findtext(ar_qname("VALUE"))
            for record in inner
            for leaf in record.find(ar_qname("FIELDS")).iter(ar_qname("NUMERICAL-VALUE-SPECIFICATION"))
        ] == ["1", "2"]


#: The receiver COM-SPEC's ``HANDLE-TIMEOUT-TYPE`` and the ``INIT-VALUE`` after it, as the
#: writer lays them out; anchored on a shared indentation so only a well-formed pair matches.
_RECEIVER_INIT_VALUE_IN_PLACE = re.compile(
    r"(?P<indent> *)<HANDLE-TIMEOUT-TYPE>(?P<flag>[^<]*)</HANDLE-TIMEOUT-TYPE>\n"
    r"(?P<init>(?P=indent)<INIT-VALUE>\n.*?\n(?P=indent)</INIT-VALUE>\n)",
    re.DOTALL,
)


def _with_the_receiver_init_value_one_position_early(text: str) -> str:
    """Move the first receiver COM-SPEC's ``INIT-VALUE`` one position early, ahead of ``HANDLE-TIMEOUT-TYPE``.

    Nothing else is touched, so a refusal is about the position alone.
    """
    substituted, count = _RECEIVER_INIT_VALUE_IN_PLACE.subn(
        r"\g<init>\g<indent><HANDLE-TIMEOUT-TYPE>\g<flag></HANDLE-TIMEOUT-TYPE>\n",
        text,
        count=1,
    )
    assert count == 1, "the writer's receiver COM-SPEC no longer has the shape this control substitutes into"
    assert substituted != text
    return substituted


class TestTheMisplacedInitValueIsRefused:
    """Both schemas refuse a receiver ``INIT-VALUE`` out of position, so the conformance test is evidence."""

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_an_init_value_before_handle_timeout_type_is_refused(self, version: ArxmlVersion, tmp_path: Path) -> None:
        schema = require_licensed_schema(version)
        generated = tmp_path / f"in-place-{version}.arxml"
        generate_arxml(str(INIT_VALUES_MODEL_ROOT), str(generated), version=version)
        misplaced = tmp_path / f"misplaced-{version}.arxml"
        misplaced.write_text(
            _with_the_receiver_init_value_one_position_early(generated.read_text(encoding="utf-8")),
            encoding="utf-8",
            newline="\n",
        )

        outcome = check_arxml(str(misplaced), schema_path=schema)

        assert outcome.mode == MODE_XSD
        assert outcome.schema_error is False, outcome.messages
        assert not outcome.valid, f"{schema} accepts an INIT-VALUE before HANDLE-TIMEOUT-TYPE"
        assert any("HANDLE-TIMEOUT-TYPE" in message for message in outcome.messages), outcome.messages


# The access-point root, and the two positions the schemas really police


#: The four access containers in the schema's order; written out, not imported from the writer,
#: so the oracle stays independent.
_ACCESS_CONTAINER_TAGS = (
    "DATA-RECEIVE-POINT-BY-ARGUMENTS",
    "DATA-RECEIVE-POINT-BY-VALUES",
    "DATA-SEND-POINTS",
    "SERVER-CALL-POINTS",
)


class TestTheAccessPointRootConforms:
    """``fixtures/access_points/model``, the tree carrying every access shape, against both schemas.

    It holds the capability marker's presence and absence; it is used in place and is not a golden.
    """

    def test_the_root_is_accepted_by_check_model(self) -> None:
        """The oracles below are only worth something over a model a user could author."""
        result = check_cross_layer_consistency(str(ACCESS_POINTS_MODEL_ROOT))

        assert result.consistent, result.issues

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_root_generates_conforming_arxml(self, version: ArxmlVersion, tmp_path: Path) -> None:
        output = tmp_path / f"access-points-{version}.arxml"

        generate_arxml(str(ACCESS_POINTS_MODEL_ROOT), str(output), version=version)

        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_conforming_document_really_carries_all_four_containers(
        self, version: ArxmlVersion, tmp_path: Path
    ) -> None:
        """Non-vacuity: all four containers are optional in both schemas, so each is asserted."""
        output = tmp_path / f"access-points-{version}.arxml"
        generate_arxml(str(ACCESS_POINTS_MODEL_ROOT), str(output), version=version)
        root = etree.parse(str(output)).getroot()

        found = {
            tag: [child.tag.rpartition("}")[2] for child in container]
            for tag in _ACCESS_CONTAINER_TAGS
            for container in root.iter(ar_qname(tag))
        }

        assert set(found) == set(_ACCESS_CONTAINER_TAGS)
        assert found["DATA-RECEIVE-POINT-BY-ARGUMENTS"] == ["VARIABLE-ACCESS"]
        assert found["DATA-RECEIVE-POINT-BY-VALUES"] == ["VARIABLE-ACCESS"]
        assert found["DATA-SEND-POINTS"] == ["VARIABLE-ACCESS"]
        assert found["SERVER-CALL-POINTS"] == ["SYNCHRONOUS-SERVER-CALL-POINT"]
        assert {
            short_name_of(point)
            for tag in _ACCESS_CONTAINER_TAGS
            for container in root.iter(ar_qname(tag))
            for point in container
        } == {
            access_point_name(READ_ACCESS_PREFIX, "rSignals", "speed"),
            access_point_name(READ_ACCESS_PREFIX, "rSignals", "level"),
            access_point_name(WRITE_ACCESS_PREFIX, "pSignals", "speed"),
            access_point_name(CALL_POINT_PREFIX, "rControl", "Apply"),
        }

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_conforming_document_really_carries_the_marker(self, version: ArxmlVersion, tmp_path: Path) -> None:
        """The capability marker is on the behaviour of the component that carries a point, and no other."""
        output = tmp_path / f"access-points-{version}.arxml"
        generate_arxml(str(ACCESS_POINTS_MODEL_ROOT), str(output), version=version)
        root = etree.parse(str(output)).getroot()

        carriers = {
            short_name_of(admin.getparent()): [
                (group.get("GID"), [(datum.get("GID"), datum.text) for datum in group])
                for group in admin.iter(ar_qname("SDG"))
            ]
            for admin in root.iter(ar_qname("ADMIN-DATA"))
        }

        assert carriers == {"AccessSwc_InternalBehavior": [("SWCRAFT", [("ACCESS-POINTS", "explicit")])]}


#: The four containers between ``CAN-BE-INVOKED-CONCURRENTLY`` and ``SYMBOL``, anchored on a
#: shared indentation, so only a well-formed runnable that declares an access matches.
_ACCESS_CONTAINERS_IN_PLACE = re.compile(
    r"(?P<indent> *)<CAN-BE-INVOKED-CONCURRENTLY>(?P<flag>[^<]*)</CAN-BE-INVOKED-CONCURRENTLY>\n"
    r"(?P<containers>(?P=indent)<DATA-RECEIVE-POINT-BY-ARGUMENTS>\n.*?)"
    r"(?P<symbol>(?P=indent)<SYMBOL>[^<]*</SYMBOL>\n)",
    re.DOTALL,
)

#: The two receive containers followed by ``DATA-SEND-POINTS``, anchored the same way.
_SEND_AFTER_RECEIVE_IN_PLACE = re.compile(
    r"(?P<indent> *)<DATA-RECEIVE-POINT-BY-ARGUMENTS>\n"
    r"(?P<receive>.*?(?P=indent)</DATA-RECEIVE-POINT-BY-VALUES>\n)"
    r"(?P<send>(?P=indent)<DATA-SEND-POINTS>\n.*?(?P=indent)</DATA-SEND-POINTS>\n)",
    re.DOTALL,
)


def _with_the_containers_after_symbol(text: str) -> str:
    """Move the four access containers behind the runnable's ``SYMBOL``, touching nothing else."""
    substituted, count = _ACCESS_CONTAINERS_IN_PLACE.subn(
        r"\g<indent><CAN-BE-INVOKED-CONCURRENTLY>\g<flag></CAN-BE-INVOKED-CONCURRENTLY>\n\g<symbol>\g<containers>",
        text,
        count=1,
    )
    assert count == 1, "the writer's RUNNABLE-ENTITY no longer has the shape this control substitutes into"
    assert substituted != text
    return substituted


def _with_the_send_points_before_the_receive_points(text: str) -> str:
    """Move ``DATA-SEND-POINTS`` ahead of both receive containers, so only their order changes."""
    substituted, count = _SEND_AFTER_RECEIVE_IN_PLACE.subn(
        r"\g<send>\g<indent><DATA-RECEIVE-POINT-BY-ARGUMENTS>\n\g<receive>",
        text,
        count=1,
    )
    assert count == 1, "the writer's access containers no longer have the shape this control substitutes into"
    assert substituted != text
    return substituted


class TestTheMisplacedAccessContainersAreRefused:
    """Both schemas refuse access containers out of position or order, so the conformance test is evidence."""

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    @pytest.mark.parametrize(
        ("substitute", "label"),
        [
            pytest.param(_with_the_containers_after_symbol, "after SYMBOL", id="after-symbol"),
            pytest.param(
                _with_the_send_points_before_the_receive_points,
                "before DATA-RECEIVE-POINT-BY-ARGUMENTS",
                id="send-before-receive",
            ),
        ],
    )
    def test_a_misplaced_container_is_refused(
        self, substitute, label: str, version: ArxmlVersion, tmp_path: Path
    ) -> None:
        schema = require_licensed_schema(version)
        generated = tmp_path / f"in-place-{version}.arxml"
        generate_arxml(str(ACCESS_POINTS_MODEL_ROOT), str(generated), version=version)
        misplaced = tmp_path / f"misplaced-{version}.arxml"
        misplaced.write_text(
            substitute(generated.read_text(encoding="utf-8")),
            encoding="utf-8",
            newline="\n",
        )

        outcome = check_arxml(str(misplaced), schema_path=schema)

        assert outcome.mode == MODE_XSD
        assert outcome.schema_error is False, outcome.messages
        assert not outcome.valid, f"{schema} accepts an access container {label}"
        assert any("DATA-RECEIVE-POINT-BY-ARGUMENTS" in message for message in outcome.messages), outcome.messages


class TestTheCompositionHierarchyConforms:
    """``fixtures/compositions/model``, the composition hierarchy, against both schemas.

    Here rather than beside ``test_arxml_compositions_writer.py`` because ``dev.py test-xsd`` selects
    this file by path. Not reached: a ``TYPE-TREF`` pointing outside the document is XSD-valid, so
    wiring is checked in that sibling module.
    """

    def test_the_root_is_accepted_by_check_model(self) -> None:
        """The oracles below are only worth something over a model a user could author."""
        result = check_cross_layer_consistency(str(COMPOSITIONS_MODEL_ROOT))

        assert result.consistent, result.issues

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_root_generates_conforming_arxml(self, version: ArxmlVersion, tmp_path: Path) -> None:
        output = tmp_path / f"compositions-{version}.arxml"

        generate_arxml(str(COMPOSITIONS_MODEL_ROOT), str(output), version=version)

        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_conforming_document_really_carries_the_hierarchy(self, version: ArxmlVersion, tmp_path: Path) -> None:
        """Non-vacuity: every one of these elements is optional in both schemas."""
        output = tmp_path / f"compositions-{version}.arxml"
        generate_arxml(str(COMPOSITIONS_MODEL_ROOT), str(output), version=version)
        root = etree.parse(str(output)).getroot()

        counted = {
            tag: len(list(root.iter(ar_qname(tag))))
            for tag in (
                "COMPOSITION-SW-COMPONENT-TYPE",
                "SW-COMPONENT-PROTOTYPE",
                "DELEGATION-SW-CONNECTOR",
                "ASSEMBLY-SW-CONNECTOR",
                "P-PORT-IN-COMPOSITION-INSTANCE-REF",
                "R-PORT-IN-COMPOSITION-INSTANCE-REF",
            )
        }

        assert counted == {
            "COMPOSITION-SW-COMPONENT-TYPE": 3,
            "SW-COMPONENT-PROTOTYPE": 6,
            "DELEGATION-SW-CONNECTOR": 3,
            "ASSEMBLY-SW-CONNECTOR": 2,
            "P-PORT-IN-COMPOSITION-INSTANCE-REF": 1,
            "R-PORT-IN-COMPOSITION-INSTANCE-REF": 2,
        }
        assert "/Other/Project/SupplierPlatforms/Bsw/ExternalStub" in output.read_text(encoding="utf-8")

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_a_type_tref_outside_the_document_is_accepted(self, version: ArxmlVersion, tmp_path: Path) -> None:
        """The gate's limit, asserted: both schemas accept a reference the document does not contain.

        A wrong outside reference is the round trip's to catch, not this gate's.
        """
        output = tmp_path / f"compositions-{version}.arxml"
        generate_arxml(str(COMPOSITIONS_MODEL_ROOT), str(output), version=version)
        root = etree.parse(str(output)).getroot()

        # Only the prototypes' TYPE-TREFs; a data type's points into /DataTypes.
        dangling = [
            reference.text
            for prototype in root.iter(ar_qname("SW-COMPONENT-PROTOTYPE"))
            for reference in prototype.iter(ar_qname("TYPE-TREF"))
            if not str(reference.text).startswith("/ComponentTypes/")
        ]
        assert dangling == ["/Other/Project/SupplierPlatforms/Bsw/ExternalStub"]

        _assert_conforms(str(output), version)


class TestTheCompatibleInterfaceRootConforms:
    """``fixtures/compatible_interfaces/model`` conforms to both schemas.

    Its connectors and delegations join differently named compatible interfaces.

    Not reached: the XSD states no compatibility requirement between joined ports at all (a client-server port delegated
    onto a sender-receiver one validates), so whether a pair should be joined is the compatibility predicate's.
    """

    def test_the_root_is_accepted_by_check_model(self) -> None:
        """The oracles below are only worth something over a model a user could author."""
        result = check_cross_layer_consistency(str(COMPATIBLE_INTERFACES_MODEL_ROOT))

        assert result.consistent, result.issues

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_root_generates_conforming_arxml(self, version: ArxmlVersion, tmp_path: Path) -> None:
        output = tmp_path / f"compatible-{version}.arxml"

        generate_arxml(str(COMPATIBLE_INTERFACES_MODEL_ROOT), str(output), version=version)

        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_conforming_document_really_carries_the_four_pairs(self, version: ArxmlVersion, tmp_path: Path) -> None:
        """Non-vacuity: every one of these elements is optional in both schemas."""
        output = tmp_path / f"compatible-{version}.arxml"
        generate_arxml(str(COMPATIBLE_INTERFACES_MODEL_ROOT), str(output), version=version)
        root = etree.parse(str(output)).getroot()

        counted = {
            tag: len(list(root.iter(ar_qname(tag))))
            for tag in (
                "COMPOSITION-SW-COMPONENT-TYPE",
                "ASSEMBLY-SW-CONNECTOR",
                "DELEGATION-SW-CONNECTOR",
                "P-PORT-IN-COMPOSITION-INSTANCE-REF",
                "R-PORT-IN-COMPOSITION-INSTANCE-REF",
            )
        }

        assert counted == {
            "COMPOSITION-SW-COMPONENT-TYPE": 2,
            "ASSEMBLY-SW-CONNECTOR": 2,
            "DELEGATION-SW-CONNECTOR": 2,
            "P-PORT-IN-COMPOSITION-INSTANCE-REF": 1,
            "R-PORT-IN-COMPOSITION-INSTANCE-REF": 1,
        }

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_joined_ports_really_carry_two_interfaces_each(self, version: ArxmlVersion, tmp_path: Path) -> None:
        """Each joined pair names two different interfaces in the emitted document.

        One interface per pair would still conform yet exercise no compatibility check.
        """
        output = tmp_path / f"compatible-{version}.arxml"
        generate_arxml(str(COMPATIBLE_INTERFACES_MODEL_ROOT), str(output), version=version)
        root = etree.parse(str(output)).getroot()

        trefs: dict[str, str] = {}
        for component in root.iter(ar_qname("APPLICATION-SW-COMPONENT-TYPE")):
            owner = short_name_of(component)
            for port in list(component.iter(ar_qname("P-PORT-PROTOTYPE"))) + list(
                component.iter(ar_qname("R-PORT-PROTOTYPE"))
            ):
                for tag in ("PROVIDED-INTERFACE-TREF", "REQUIRED-INTERFACE-TREF"):
                    reference = port.find(ar_qname(tag))
                    if reference is not None:
                        trefs[f"{owner}.{short_name_of(port)}"] = str(reference.text)

        assert trefs["Sensor.pStatus"] != trefs["Display.rStatus"]
        assert trefs["Actuator.pCommand"] != trefs["Controller.rCommand"]
        assert trefs["Relay.pStatusOut"] == "/PortInterfaces/IStatusFull"
        assert trefs["Relay.rCommandIn"] == "/PortInterfaces/ICommandBasic"


# The external interface and type stubs


class TestTheExternalStubModelConforms:
    """Every reference the writer emits to an external stub, against both schemas.

    Asked: a port with no ``…-COM-SPECS`` wrapper and a ``DEST`` carried verbatim from the stub.
    What it does not reach: whether the referenced target exists (outside the document by
    definition; the integrator's obligation).
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_stub_model_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = external_stubs_model_root(tmp_path / "project")
        assert check_cross_layer_consistency(str(model_root)).consistent
        output = tmp_path / f"external-stubs-{version}.arxml"
        generate_arxml(str(model_root), str(output), version=version)
        _assert_conforms(str(output), version)

    def test_the_validated_document_really_carries_every_stub_reference(self, tmp_path: Path) -> None:
        """Non-vacuity: every stub reference and every wrapper-less port is in the document."""
        output = tmp_path / "external-stubs.arxml"
        generate_arxml(str(external_stubs_model_root(tmp_path / "project")), str(output))
        root = etree.parse(str(output)).getroot()

        interface_refs = {
            (reference.get("DEST"), reference.text)
            for tag in ("PROVIDED-INTERFACE-TREF", "REQUIRED-INTERFACE-TREF")
            for reference in root.iter(ar_qname(tag))
        }
        assert {(dest, ref) for _name, ref, dest in STUB_INTERFACES} <= interface_refs
        wrapperless = [
            port
            for tag in ("P-PORT-PROTOTYPE", "R-PORT-PROTOTYPE")
            for port in root.iter(ar_qname(tag))
            if not [child for child in port if child.tag.endswith("-COM-SPECS")]
        ]
        assert len(wrapperless) == len(STUB_PORTS) + len(STUB_BOUNDARY_PORTS)
        type_refs = [
            reference
            for tag in ("TYPE-TREF", "IMPLEMENTATION-DATA-TYPE-REF")
            for reference in root.iter(ar_qname(tag))
            if reference.text == STUB_TYPE[1]
        ]
        assert {reference.get("DEST") for reference in type_refs} == {STUB_TYPE[2]}
        assert len(type_refs) == 3, "a data element, an argument and a reference type's base"
        assert len(list(root.iter(ar_qname("DELEGATION-SW-CONNECTOR")))) == 2


class TestTheListedStubModelConforms:
    """A stub listing its members: member references and the ports' derived com-specs, against both schemas.

    What it does not reach: whether a listed member exists in the file that declares the stub's
    target, which lies outside the document by definition.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_listed_stub_model_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = listed_stub_model_root(tmp_path / "project")
        assert check_cross_layer_consistency(str(model_root)).consistent
        output = tmp_path / f"listed-stubs-{version}.arxml"
        generate_arxml(str(model_root), str(output), version=version)
        _assert_conforms(str(output), version)

    def test_the_validated_document_really_carries_the_member_paths(self, tmp_path: Path) -> None:
        """Non-vacuity: every member reference and every derived com-spec kind is in the document."""
        output = tmp_path / "listed-stubs.arxml"
        generate_arxml(str(listed_stub_model_root(tmp_path / "project")), str(output))
        root = etree.parse(str(output)).getroot()
        stub_refs = tuple(f"{ref}/" for _name, ref, _dest in STUB_INTERFACES)
        under_a_stub = {
            etree.QName(element).localname
            for element in root.iter()
            if isinstance(element.tag, str) and (element.text or "").startswith(stub_refs)
        }

        assert under_a_stub == {
            "TARGET-PROVIDED-OPERATION-REF",
            "TARGET-REQUIRED-OPERATION-REF",
            "TARGET-DATA-PROTOTYPE-REF",
            "OPERATION-REF",
            "DATA-ELEMENT-REF",
        }
        for tag in (
            "SERVER-COM-SPEC",
            "CLIENT-COM-SPEC",
            "NONQUEUED-SENDER-COM-SPEC",
            "NONQUEUED-RECEIVER-COM-SPEC",
        ):
            specs = [
                spec for spec in root.iter(ar_qname(tag)) if any((ref.text or "").startswith(stub_refs) for ref in spec)
            ]
            assert specs, tag


class TestTheImportedStubMembersConform:
    """``stub_members.arxml`` imported and rebuilt: what the importer keeps through a stub, against both schemas.

    Schema evidence only: it does not tell a kept record from a derived one.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_imported_model_conforms(
        self, version: ArxmlVersion, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from swcraft_tools.arxml_import import run_import
        from swcraft_tools.cli import main

        project = tmp_path / "project"
        monkeypatch.setattr("sys.argv", ["swcraft", "init", str(project)])
        main()
        run_import(str(_STUB_MEMBERS_SOURCE), str(project / "swcraft.yaml"))
        output = tmp_path / f"stub-members-{version}.arxml"
        generate_arxml(str(project / "model"), str(output), version=version)
        text = output.read_text(encoding="utf-8")
        # Non-vacuity: a kept event's and a kept call's member paths, and the entry's value.
        assert ">/Remote/Ifc/ICalc/Add</TARGET-PROVIDED-OPERATION-REF>" in text
        assert ">/Remote/Ifc/ICalc/Add</TARGET-REQUIRED-OPERATION-REF>" in text
        assert "<ENABLE-UPDATE>true</ENABLE-UPDATE>" in text
        _assert_conforms(str(output), version)


_STUB_MEMBERS_SOURCE = Path(__file__).resolve().parent / "fixtures" / "arxml_external_stubs" / "stub_members.arxml"


class TestAStubTypedArgumentCarriesNoPolicy:
    """An argument typed by an external type stub, directly or through a reference type, carries no policy.

    Whether the stub is an array is unknown here, and ``USE-ARGUMENT-TYPE`` would assert it is not.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_both_paths_omit_it_and_conform(self, version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = external_stubs_model_root(tmp_path / "project")
        change(
            model_root,
            STUB_CS_INTERFACE,
            lambda d: d["operations"][0]["arguments"].append(
                {"name": "foreignRef", "direction": "in", "implementation_type": "RefForeign_T"}
            ),
        )
        assert check_cross_layer_consistency(str(model_root)).consistent
        output = tmp_path / f"stub-arguments-{version}.arxml"
        generate_arxml(str(model_root), str(output), version=version)

        _assert_conforms(str(output), version)
        policies = {
            short_name_of(argument): argument.findtext(ar_qname("SERVER-ARGUMENT-IMPL-POLICY"))
            for argument in etree.parse(str(output)).getroot().iter(ar_qname("ARGUMENT-DATA-PROTOTYPE"))
        }
        assert policies["foreign"] is None
        assert policies["foreignRef"] is None
        others = {name: policy for name, policy in policies.items() if name not in ("foreign", "foreignRef")}
        assert others and set(others.values()) == {"USE-ARGUMENT-TYPE"}


class TestTheComponentStubPortModelConforms:
    """Connectors and delegations ending on an external component stub's port, against both schemas.

    What it does not reach: whether the foreign type owns that port, or what interface it has (the
    stub's type lies outside the document; the integrator's obligation).
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_stub_port_model_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = component_stub_ports_model_root(tmp_path / "model")
        assert check_cross_layer_consistency(str(model_root)).consistent
        output = tmp_path / f"component-stub-ports-{version}.arxml"
        generate_arxml(str(model_root), str(output), version=version)
        _assert_conforms(str(output), version)

    def test_the_validated_document_really_carries_the_stub_port_ends(self, tmp_path: Path) -> None:
        """Non-vacuity: three ends on a stub's port, one of each target tag at least, under the stubs' paths."""
        output = tmp_path / "component-stub-ports.arxml"
        generate_arxml(str(component_stub_ports_model_root(tmp_path / "model")), str(output))
        root = etree.parse(str(output)).getroot()

        stub_paths = ("/Other/Project/SupplierPlatforms/Bsw/ExternalStub/", SECOND_COMPONENT_STUB[1] + "/")
        ends = sorted(
            (reference.get("DEST"), reference.text)
            for tag in ("TARGET-P-PORT-REF", "TARGET-R-PORT-REF")
            for reference in root.iter(ar_qname(tag))
            if reference.text.startswith(stub_paths)
        )
        assert ends == sorted(
            [
                ("P-PORT-PROTOTYPE", stub_paths[0] + "pExt"),
                ("P-PORT-PROTOTYPE", stub_paths[0] + "pExt"),
                ("R-PORT-PROTOTYPE", stub_paths[0] + "rExt"),
                ("R-PORT-PROTOTYPE", stub_paths[1] + "rOther"),
            ]
        ), ends


class TestTheOperationEventShapesConform:
    """Every event shape the writer emits, and a mixed-event runnable, against both schemas.

    Schema evidence only: it does not check that ``OPERATION-IREF`` names the component's own port
    or an operation of its interface, and a mixed runnable passing it is never RTE conformance (the
    RTE specification calls mixing unsupported in explanatory text only; SWCraft does not judge it).
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_every_event_shape_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = every_event_shape_model(tmp_path / "model")
        assert check_cross_layer_consistency(str(model_root)).consistent
        output = tmp_path / f"operation-events-{version}.arxml"

        generate_arxml(str(model_root), str(output), version=version)

        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_mixed_event_fixture_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        output = tmp_path / f"mixed-events-{version}.arxml"

        generate_arxml(str(mixed_event_model(tmp_path / "model")), str(output), version=version)

        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_component_export_with_its_operation_events_marker_conforms(
        self, version: ArxmlVersion, tmp_path: Path
    ) -> None:
        """The component export, with the operation-events ``SD``, conforms.

        Schema evidence for the marker, not for the regime it selects."""
        output = tmp_path / f"ServerSwc-{version}.arxml"

        export_component(str(every_event_shape_model(tmp_path / "model")), "ServerSwc", str(output), version=version)

        assert f'<SD GID="{OPERATION_EVENTS.sd_gid}">' in output.read_text(encoding="utf-8")
        _assert_conforms(str(output), version)

    def test_the_validated_document_really_carries_every_shape(self, tmp_path: Path) -> None:
        """Non-vacuity: ``OPERATION-IREF`` and ``CAN-BE-INVOKED-CONCURRENTLY`` are optional in both schemas."""
        output = tmp_path / "operation-events.arxml"
        generate_arxml(str(every_event_shape_model(tmp_path / "model")), str(output))
        root = etree.parse(str(output)).getroot()

        kinds = [event.tag.rpartition("}")[2] for events in root.iter(ar_qname("EVENTS")) for event in events]
        operation_events = list(root.iter(ar_qname("OPERATION-INVOKED-EVENT")))
        names = {short_name_of(event) for event in operation_events}
        concurrency = [node.text for node in root.iter(ar_qname("CAN-BE-INVOKED-CONCURRENTLY"))]

        assert sorted(set(kinds)) == ["INIT-EVENT", "OPERATION-INVOKED-EVENT", "TIMING-EVENT"]
        assert len(operation_events) == 6
        assert all(event.find(ar_qname("OPERATION-IREF")) is not None for event in operation_events)
        assert {"OIT_p_A_B_0", "OIT_p_A_B_1", f"OIT_{BOUNDARY_PORT}_{BOUNDARY_OPERATION}"} <= names
        assert len(f"OIT_{BOUNDARY_PORT}_{BOUNDARY_OPERATION}") == MAX_IDENTIFIER_LENGTH
        assert "true" in concurrency
        assert "false" in concurrency


# The implementation record, the implementation array and the pointer


class TestTheImplementationCompositesConform:
    """Every shape the writer emits for implementation records, arrays and pointers, against both schemas.

    Asked: ``SUB-ELEMENTS`` (``ARRAY-SIZE`` and ``ARRAY-SIZE-SEMANTICS`` before
    ``SW-DATA-DEF-PROPS``), ``SW-POINTER-TARGET-PROPS`` and a composite constant. What it does not
    reach: whether a map's two sides are compatible (``[TPS_SWCT_01191]``'s and, in part, SWCraft's
    positional check), whether a sub-element's name matches anything, and ``[constr_1066]`` /
    ``[constr_1295]``, which no schema can state.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_model_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = impl_composites_model_root(tmp_path / "project")
        assert check_cross_layer_consistency(str(model_root)).consistent
        output = tmp_path / f"impl-composites-{version}.arxml"

        generate_arxml(str(model_root), str(output), version=version)

        _assert_conforms(str(output), version)

    def test_the_validated_document_really_carries_every_shape(self, tmp_path: Path) -> None:
        """Non-vacuity: ``SUB-ELEMENTS`` and ``SW-POINTER-TARGET-PROPS`` are optional in both schemas."""
        output = tmp_path / "impl-composites.arxml"
        generate_arxml(str(impl_composites_model_root(tmp_path / "project")), str(output))
        root = etree.parse(str(output)).getroot()

        categories = [node.findtext(ar_qname("CATEGORY")) for node in root.iter(ar_qname("IMPLEMENTATION-DATA-TYPE"))]
        assert categories.count("STRUCTURE") == 3
        assert categories.count("ARRAY") == 2
        assert categories.count("DATA_REFERENCE") == 2
        assert len(list(root.iter(ar_qname("SW-POINTER-TARGET-PROPS")))) == 2
        semantics = [
            node.findtext(ar_qname("ARRAY-SIZE-SEMANTICS"))
            for node in root.iter(ar_qname("IMPLEMENTATION-DATA-TYPE-ELEMENT"))
            if node.find(ar_qname("ARRAY-SIZE")) is not None
        ]
        assert semantics == ["FIXED-SIZE", "FIXED-SIZE"]
        maps = {(node.get("DEST"), node.text) for node in root.iter(ar_qname("APPLICATION-DATA-TYPE-REF"))}
        assert ("APPLICATION-RECORD-DATA-TYPE", "/DataTypes/Application/WheelSpeeds") in maps
        assert ("APPLICATION-ARRAY-DATA-TYPE", "/DataTypes/Application/WheelSpeedHistory") in maps
        constants = [short_name_of(node) for node in root.iter(ar_qname("CONSTANT-SPECIFICATION"))]
        assert {"IExample_speeds_Init", "IExample_history_Init", "IExample_frame_Init"} <= set(constants)


# Inter-runnable variables and the runnables' accesses


class TestTheInterRunnableVariablesConform:
    """Inter-runnable variables and their accesses, in the fixture and an every-shape copy, against both schemas.

    What it does not reach: whether a ``LOCAL-VARIABLE-REF`` names a variable of the same behaviour
    (``[constr_2026]``; the writer's tests do), ``[constr_1296]`` and ``[TPS_SWCT_01522]``, which no
    schema can state, and whether an RTE generator accepts the file.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    @pytest.mark.parametrize("build", [variable_model, every_variable_shape_model], ids=["fixture", "every-shape"])
    def test_the_model_conforms(self, build: Callable[[Path], Path], version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = build(tmp_path / "model")
        assert check_cross_layer_consistency(str(model_root)).consistent
        output = tmp_path / f"inter-runnable-variables-{version}.arxml"

        generate_arxml(str(model_root), str(output), version=version)

        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_component_export_with_its_variables_marker_conforms(
        self, version: ArxmlVersion, tmp_path: Path
    ) -> None:
        """The component export, with the inter-runnable-variables ``SD``, conforms.

        Schema evidence for the marker, not for the regime it selects."""
        output = tmp_path / f"{COMPONENT}-{version}.arxml"

        export_component(str(every_variable_shape_model(tmp_path / "model")), COMPONENT, str(output), version=version)

        assert f'<SD GID="{INTER_RUNNABLE_VARIABLES.sd_gid}">' in output.read_text(encoding="utf-8")
        _assert_conforms(str(output), version)

    def test_the_validated_document_really_carries_every_shape(self, tmp_path: Path) -> None:
        """Non-vacuity: every wrapper and container is optional in both schemas."""
        output = tmp_path / "every-shape.arxml"
        generate_arxml(str(every_variable_shape_model(tmp_path / "model")), str(output))
        root = etree.parse(str(output)).getroot()

        for tag in (
            "EXPLICIT-INTER-RUNNABLE-VARIABLES",
            "IMPLICIT-INTER-RUNNABLE-VARIABLES",
            "READ-LOCAL-VARIABLES",
            "WRITTEN-LOCAL-VARIABLES",
            "SERVER-CALL-POINTS",
            "DATA-RECEIVE-POINT-BY-ARGUMENTS",
        ):
            assert next(root.iter(ar_qname(tag)), None) is not None, tag
        values = {etree.QName(value).localname for init in root.iter(ar_qname("INIT-VALUE")) for value in init}
        assert {
            "APPLICATION-VALUE-SPECIFICATION",
            "NUMERICAL-VALUE-SPECIFICATION",
            "RECORD-VALUE-SPECIFICATION",
            "ARRAY-VALUE-SPECIFICATION",
        } <= values
        assert len(list(root.iter(ar_qname("LOCAL-VARIABLE-REF")))) == 8


# Exclusive areas and the runnables' references to them


class TestTheExclusiveAreasConform:
    """Exclusive areas and both reference roles, in the fixture and an every-shape copy, against both schemas.

    What it does not reach: whether a reference names an area of the same behaviour
    (``test_exclusive_area_arxml.py`` does), the regime the export's capability marker selects, and
    whether an RTE generator accepts the file.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_component_export_with_its_areas_marker_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        """The component export, with the exclusive areas ``SD``, conforms.

        Schema evidence for the marker, not for the regime it selects."""
        output = tmp_path / f"{COMPONENT}-{version}.arxml"

        export_component(str(every_area_shape_model(tmp_path / "model")), COMPONENT, str(output), version=version)

        text = output.read_text(encoding="utf-8")
        assert f'<SD GID="{EXCLUSIVE_AREAS.sd_gid}">' in text
        assert "<EXCLUSIVE-AREAS>" in text
        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    @pytest.mark.parametrize("build", [area_model, every_area_shape_model], ids=["fixture", "every-shape"])
    def test_the_model_conforms(self, build: Callable[[Path], Path], version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = build(tmp_path / "model")
        assert check_cross_layer_consistency(str(model_root)).consistent
        output = tmp_path / f"exclusive-areas-{version}.arxml"

        generate_arxml(str(model_root), str(output), version=version)

        _assert_conforms(str(output), version)

    def test_the_validated_document_really_carries_every_shape(self, tmp_path: Path) -> None:
        """Non-vacuity: every wrapper is optional in both schemas."""
        output = tmp_path / "every-shape.arxml"
        generate_arxml(str(every_area_shape_model(tmp_path / "model")), str(output))
        root = etree.parse(str(output)).getroot()

        for tag in (
            "DATA-TYPE-MAPPING-REFS",
            "EXCLUSIVE-AREAS",
            "CAN-ENTER-EXCLUSIVE-AREA-REFS",
            "RUNS-INSIDE-EXCLUSIVE-AREA-REFS",
            "READ-LOCAL-VARIABLES",
            "SERVER-CALL-POINTS",
        ):
            assert next(root.iter(ar_qname(tag)), None) is not None, tag
        assert len(list(root.iter(ar_qname("EXCLUSIVE-AREA")))) == 2
        assert [area.find(ar_qname("DESC")) is not None for area in root.iter(ar_qname("EXCLUSIVE-AREA"))] == [
            True,
            False,
        ]
        assert len(list(root.iter(ar_qname("CAN-ENTER-EXCLUSIVE-AREA-REF")))) == 2
        assert len(list(root.iter(ar_qname("RUNS-INSIDE-EXCLUSIVE-AREA-REF")))) == 2


# Data-received events


def _every_data_event_shape_model(destination: Path) -> Path:
    """The data-received event fixture, placed in packages, with a stated name and a repeated event.

    The repeated event puts the shared counter's ``_0`` / ``_1`` in the document.
    """
    model_root = data_event_model(destination)

    def mutate(document: dict[str, Any]) -> None:
        document["package"] = "Chassis"
        document["runnables"][0]["events"][0]["name"] = "OnFrontLeft"
        rear = document["runnables"][2]["events"]
        rear.append(dict(rear[0]))

    change(model_root, DATA_EVENT_COMPONENT_FILE, mutate)
    change(model_root, WHEEL_INTERFACE_FILE, lambda d: d.update(package="Wheels"))
    return model_root


class TestTheDataEventShapesConform:
    """``DATA-RECEIVED-EVENT``, in the fixture and an every-shape copy, against both schemas.

    What it does not reach: whether the port is the component's own require port or the element its
    interface's (``test_data_event_arxml.py`` does), the regime the export's capability marker
    selects, and whether an RTE generator accepts the file: an event beside a timing event passing
    it is never RTE conformance.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_component_export_with_its_data_events_marker_conforms(
        self, version: ArxmlVersion, tmp_path: Path
    ) -> None:
        """The stub-free fixture's component export, with the data-received events ``SD``, conforms.

        Schema evidence for the marker, not for the regime it selects."""
        output = tmp_path / f"{DATA_EVENT_COMPONENT}-{version}.arxml"

        export_component(
            str(stub_free_data_event_model(tmp_path / "model")), DATA_EVENT_COMPONENT, str(output), version=version
        )

        text = output.read_text(encoding="utf-8")
        assert f'<SD GID="{DATA_EVENTS.sd_gid}">' in text
        assert "<DATA-RECEIVED-EVENT>" in text
        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    @pytest.mark.parametrize("build", [data_event_model, _every_data_event_shape_model], ids=["fixture", "every-shape"])
    def test_the_model_conforms(self, build: Callable[[Path], Path], version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = build(tmp_path / "model")
        assert check_cross_layer_consistency(str(model_root)).consistent
        output = tmp_path / f"data-events-{version}.arxml"

        generate_arxml(str(model_root), str(output), version=version)

        _assert_conforms(str(output), version)

    def test_the_validated_document_really_carries_every_shape(self, tmp_path: Path) -> None:
        """Non-vacuity: ``EVENTS`` is a choice and every child of the event is optional in both schemas."""
        output = tmp_path / "every-shape.arxml"
        generate_arxml(str(_every_data_event_shape_model(tmp_path / "model")), str(output))
        root = etree.parse(str(output)).getroot()

        events = list(root.iter(ar_qname("DATA-RECEIVED-EVENT")))
        assert [short_name_of(event) for event in events] == [
            "OnFrontLeft",
            "DRT_R_WheelSpeed_FrontRight",
            "DRT_R_RearSpeed_RearLeft_0",
            "DRT_R_RearSpeed_RearLeft_1",
        ]
        for tag in ("DISABLED-MODE-IREFS", "START-ON-EVENT-REF", "CONTEXT-R-PORT-REF", "TARGET-DATA-ELEMENT-REF"):
            assert next(root.iter(ar_qname(tag)), None) is not None, tag
        assert len(list(root.iter(ar_qname("DATA-IREF")))) == 4


# Implicit access, asynchronous calls, call_returned events

#: The shared access-forms fixture (``fixtures/access_forms/model``, its README).
ACCESS_FORMS_FIXTURE = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "access_forms" / "model"
ACCESS_FORMS_CONTROLLER_FILE = "swc/chassis/BrakeController.yaml"
#: A port and an operation whose names leave ``Result_{port}_{operation}`` exactly 128 characters.
ACCESS_FORMS_LIMIT_PORT = "p" * 60
ACCESS_FORMS_LIMIT_OPERATION = "o" * 60


def _access_forms_model(destination: Path) -> Path:
    """A copy of the access-forms fixture, valid at 4.2.2 and 4.3.0."""
    shutil.copytree(ACCESS_FORMS_FIXTURE, destination)
    return destination


def _access_forms_runnable(document: dict[str, Any], name: str) -> dict[str, Any]:
    (runnable,) = [entry for entry in document["runnables"] if entry["name"] == name]
    return runnable


def _every_access_form_shape_model(destination: Path) -> Path:
    """The fixture placed and named: a stated ``package`` and ``behavior_name``, every name stated, and the limit.

    One more polled call derives a ``Result_{port}_{operation}`` of exactly 128 characters.
    """
    model_root = _access_forms_model(destination)
    change(
        model_root,
        "interfaces/CalibrationIf.yaml",
        lambda d: d["operations"].append({"name": ACCESS_FORMS_LIMIT_OPERATION}),
    )

    def mutate(document: dict[str, Any]) -> None:
        document["package"] = "Chassis"
        document["behavior_name"] = "BrakeBehaviour"
        document["ports"].append(
            {"name": ACCESS_FORMS_LIMIT_PORT, "direction": "require", "interface": "CalibrationIf"}
        )
        compute = _access_forms_runnable(document, "ComputeBrakeTorque")
        for call in compute["calls"]:
            if call["operation"] == "WriteBlock":
                call["name"] = "StoreCall"
            else:
                call["result"] = "CalibrationFetched"
        _access_forms_runnable(document, "OnBlockWritten")["events"] = [
            {
                "type": "call_returned",
                "port": "rNvStorage",
                "operation": "WriteBlock",
                "result": "StoreDone",
                "name": "OnStoreDone",
            }
        ]
        document["runnables"].append(
            {
                "name": "PollAtTheLimit",
                "events": [{"type": "timing", "period": 0.1}],
                "calls": [
                    {
                        "port": ACCESS_FORMS_LIMIT_PORT,
                        "operation": ACCESS_FORMS_LIMIT_OPERATION,
                        "asynchronous": True,
                        "poll": True,
                    }
                ],
            }
        )

    change(model_root, ACCESS_FORMS_CONTROLLER_FILE, mutate)
    return model_root


def _fire_and_forget_model(destination: Path) -> Path:
    """The fixture with ``OnBlockWritten`` started by a timing event: ``WriteBlock`` has no result point (4.3.0)."""
    model_root = _access_forms_model(destination)

    def mutate(document: dict[str, Any]) -> None:
        _access_forms_runnable(document, "OnBlockWritten")["events"] = [{"type": "timing", "period": 0.1}]

    change(model_root, ACCESS_FORMS_CONTROLLER_FILE, mutate)
    return model_root


class TestTheAccessFormsConform:
    """Implicit access, asynchronous calls and their returns events, against both schemas.

    The fire-and-forget call with no result point is validated at 4.3.0 only: ``[constr_2006]`` of
    R4.2.2 refuses it and the writer does not write it at 4.2.2. What it does not reach: whether a
    reference names the element it should (``test_access_forms_arxml.py`` does), the meta-model's
    multiplicity 1 of ``TIMEOUT``, which the schema does not enforce (``[TPS_GST_00057]``), the regime
    the export's markers select, and whether an RTE generator accepts the file.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_component_export_with_its_two_markers_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        """The controller's export conforms, with the implicit-access and asynchronous-calls markers last."""
        output = tmp_path / f"BrakeController-{version}.arxml"

        export_component(str(_access_forms_model(tmp_path / "model")), "BrakeController", str(output), version=version)

        text = output.read_text(encoding="utf-8")
        assert text.index(f'<SD GID="{DATA_EVENTS.sd_gid}">') < text.index(f'<SD GID="{IMPLICIT_ACCESS.sd_gid}">')
        assert text.index(f'<SD GID="{IMPLICIT_ACCESS.sd_gid}">') < text.index(
            f'<SD GID="{ASYNCHRONOUS_CALLS.sd_gid}">'
        )
        for tag in (
            "<DATA-READ-ACCESSS>",
            "<DATA-WRITE-ACCESSS>",
            "<ASYNCHRONOUS-SERVER-CALL-POINT>",
            "<ASYNCHRONOUS-SERVER-CALL-RESULT-POINT>",
            "<ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT>",
        ):
            assert tag in text, tag
        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    @pytest.mark.parametrize(
        "build", [_access_forms_model, _every_access_form_shape_model], ids=["fixture", "every-shape"]
    )
    def test_the_model_conforms(self, build: Callable[[Path], Path], version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = build(tmp_path / "model")
        assert check_cross_layer_consistency(str(model_root), version).consistent
        output = tmp_path / f"access-forms-{version}.arxml"

        generate_arxml(str(model_root), str(output), version=version)

        _assert_conforms(str(output), version)

    def test_the_fire_and_forget_call_conforms_at_4_3_0(self, tmp_path: Path) -> None:
        model_root = _fire_and_forget_model(tmp_path / "model")
        assert check_cross_layer_consistency(str(model_root), ArxmlVersion.V4_3_0).consistent
        output = tmp_path / "fire-and-forget-4.3.0.arxml"

        generate_arxml(str(model_root), str(output), version=ArxmlVersion.V4_3_0)

        _assert_conforms(str(output), ArxmlVersion.V4_3_0)

    def test_the_validated_document_really_carries_every_shape(self, tmp_path: Path) -> None:
        """Non-vacuity: every container is optional and every new element a choice member in both schemas."""
        output = tmp_path / "every-shape.arxml"
        generate_arxml(str(_every_access_form_shape_model(tmp_path / "model")), str(output))
        root = etree.parse(str(output)).getroot()

        for tag in (
            "DATA-READ-ACCESSS",
            "DATA-WRITE-ACCESSS",
            "ASYNCHRONOUS-SERVER-CALL-RESULT-POINTS",
            "ASYNCHRONOUS-SERVER-CALL-POINT-REF",
            "EVENT-SOURCE-REF",
            "SYNCHRONOUS-SERVER-CALL-POINT",
        ):
            assert next(root.iter(ar_qname(tag)), None) is not None, tag
        assert [short_name_of(node) for node in root.iter(ar_qname("ASYNCHRONOUS-SERVER-CALL-RESULT-POINT"))] == [
            "CalibrationFetched",
            "StoreDone",
            f"Result_{ACCESS_FORMS_LIMIT_PORT}_{ACCESS_FORMS_LIMIT_OPERATION}",
        ]
        assert len(f"Result_{ACCESS_FORMS_LIMIT_PORT}_{ACCESS_FORMS_LIMIT_OPERATION}") == MAX_IDENTIFIER_LENGTH
        assert [short_name_of(node) for node in root.iter(ar_qname("ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT"))] == [
            "OnStoreDone"
        ]
        timeouts = [
            node.findtext(ar_qname("TIMEOUT")) for node in root.iter(ar_qname("ASYNCHRONOUS-SERVER-CALL-POINT"))
        ]
        assert timeouts == [None, "0.5", None]


# A component's termination and restart handling


class TestTheHandleTerminationAndRestartConforms:
    """``HANDLE-TERMINATION-AND-RESTART``, each literal, against both schemas.

    The element sits in ``group SWC-INTERNAL-BEHAVIOR`` before ``PORT-API-OPTIONS``
    (``AUTOSAR_4-2-2.xsd:60954``, ``AUTOSAR_4-3-0.xsd:70249``); its enumeration has three literals
    (``AUTOSAR_4-2-2.xsd:72819-72840``, ``AUTOSAR_4-3-0.xsd:84058-84079``). What it does not reach:
    an omission (``minOccurs="0"``; multiplicity 1 only in the annotation), which the writer refuses
    instead, so the whole model is generated without the fixture's ``SilentSwc``.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_whole_model_export_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = handle_termination_and_restart_declared_copy(tmp_path / "model")
        output = tmp_path / f"handle-termination-{version}.arxml"
        generate_arxml(str(model_root), str(output), version=version)

        _assert_conforms(str(output), version)

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    @pytest.mark.parametrize("component", ["NoSupportSwc", "TerminatedSwc", "RestartedSwc"])
    def test_each_component_export_conforms(self, component: str, version: ArxmlVersion, tmp_path: Path) -> None:
        output = tmp_path / f"{component}-{version}.arxml"
        export_component(str(HANDLE_TERMINATION_AND_RESTART_MODEL_ROOT), component, str(output), version=version)

        _assert_conforms(str(output), version)

    def test_the_validated_document_really_carries_each_literal(self, tmp_path: Path) -> None:
        """Non-vacuity: the element is optional in both schemas."""
        model_root = handle_termination_and_restart_declared_copy(tmp_path / "model")
        output = tmp_path / "handle-termination.arxml"
        generate_arxml(str(model_root), str(output))
        root = etree.parse(str(output)).getroot()

        literals = sorted(node.text for node in root.iter(ar_qname("HANDLE-TERMINATION-AND-RESTART")))
        assert literals == ["CAN-BE-TERMINATED", "CAN-BE-TERMINATED-AND-RESTARTED", "NO-SUPPORT"]


# Every kind's DESC and a composition's LONG-NAME


class TestTheDescriptionsConform:
    """The writer's ``DESC`` on every kind that emits one, and a ``LONG-NAME``, against both schemas.

    What it does not reach: the text itself (any character data in an ``L-2``) and whether
    ``FOR-ALL`` is the right language, a design choice rather than a schema's.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_model_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = _describe_model(tmp_path / "model")
        assert check_cross_layer_consistency(str(model_root)).consistent
        output = tmp_path / f"described-{version}.arxml"

        generate_arxml(str(model_root), str(output), version=version)

        _assert_conforms(str(output), version)
        root = etree.parse(str(output)).getroot()
        assert len(list(root.iter(ar_qname("DESC")))) == 27
        assert len(list(root.iter(ar_qname("LONG-NAME")))) == 3


class TestTheValueTypeRangesConform:
    """A value type's ``DATA-CONSTR`` and its ``DATA-CONSTR-REF``, against both schemas.

    What it does not reach: that ``INTERNAL-CONSTRS`` rather than ``PHYS-CONSTRS`` is the right group
    (the schemas admit both; that is ``[TPS_SWCT_01288]``'s).
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_model_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = _exchange_model(
            tmp_path / "model",
            Speed_T={"value_min": -273.15, "value_max": 300.5},
            float32={"value_min": 0.0},
            uint16={"value_max": 65535, "package": "Counters"},
        )
        output = tmp_path / f"ranged-{version}.arxml"

        generate_arxml(str(model_root), str(output), version=version)

        _assert_conforms(str(output), version)
        root = etree.parse(str(output)).getroot()
        assert len(list(root.iter(ar_qname("INTERNAL-CONSTRS")))) == 3
        assert len(list(root.iter(ar_qname("DATA-CONSTR-REF")))) == 4

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_a_sixty_four_bit_limit_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        """64-bit limits no float holds conform as ``INTERNAL-CONSTRS`` and ``PHYS-CONSTRS``."""
        model_root = _exchange_model(
            tmp_path / "model",
            Speed_T={"value_min": -9223372036854775808, "value_max": 9223372036854775807},
            uint16={"value_max": 18446744073709551615},
            Velocity_T={"range": {"min": -9223372036854775808, "max": 18446744073709551615}},
        )
        output = tmp_path / f"wide-{version}.arxml"

        generate_arxml(str(model_root), str(output), version=version)

        _assert_conforms(str(output), version)
        limits = sorted(
            node.text
            for tag in ("LOWER-LIMIT", "UPPER-LIMIT")
            for node in etree.parse(str(output)).getroot().iter(ar_qname(tag))
            if node.getparent().tag in (ar_qname("INTERNAL-CONSTRS"), ar_qname("PHYS-CONSTRS"))
        )
        assert limits == [
            "-9223372036854775808",
            "-9223372036854775808",
            "18446744073709551615",
            "18446744073709551615",
            "9223372036854775807",
        ]


class TestTheWriterDefaultsConform:
    """The member categories, the compu method's unit and the com-spec defaults, against both schemas.

    What it does not reach: that a member's category is its type's (``[constr_1152]``) or that the
    compu method must refer to a unit (``[constr_1175]``); the schemas admit any category and an
    absent ``UNIT-REF``, so those are ``test_writer_defaults.py``'s.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_members_and_com_specs_conform(self, version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = _members_model(tmp_path / "model")
        output = tmp_path / f"members-{version}.arxml"

        generate_arxml(str(model_root), str(output), version=version)

        _assert_conforms(str(output), version)
        root = etree.parse(str(output)).getroot()
        categories = {
            element.findtext(ar_qname("CATEGORY")) for element in root.iter(ar_qname("APPLICATION-RECORD-ELEMENT"))
        }
        assert {"STRUCTURE", "ARRAY", "VALUE"} <= categories
        assert len(list(root.iter(ar_qname("HANDLE-TIMEOUT-TYPE")))) > 0
        assert len(list(root.iter(ar_qname("HANDLE-OUT-OF-RANGE")))) > len(
            list(root.iter(ar_qname("HANDLE-TIMEOUT-TYPE")))
        )
        assert list(root.iter(ar_qname("FILTER"))) == []

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_the_compu_method_unit_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        output = tmp_path / f"exchange-{version}.arxml"

        generate_arxml(str(COMPONENT_EXCHANGE_MODEL_ROOT), str(output), version=version)

        _assert_conforms(str(output), version)
        root = etree.parse(str(output)).getroot()
        assert [
            ref.text for compu in root.iter(ar_qname("COMPU-METHOD")) for ref in compu.findall(ar_qname("UNIT-REF"))
        ] == ["/DataTypes/Application/Velocity_T_Unit"]


class TestTheBehaviouralValuesConform:
    """The behavioural values and the calibration access, against both schemas.

    What it does not reach: ``[TPS_SWCT_01219]``'s pairing of a queued element with a queued
    com-spec, ``[constr_1017]``'s supported combinations, ``[constr_1174]`` and ``[constr_1277]``;
    the schemas admit every combination, so those are ``test_behavioural_emission.py``'s and
    ``test_behavioural_rules.py``'s.
    """

    @pytest.mark.parametrize("version", _CONFORMANCE_VERSIONS, ids=str)
    def test_every_value_conforms(self, version: ArxmlVersion, tmp_path: Path) -> None:
        model_root = behavioural_model(tmp_path / "model")
        _mixed_composition_ports(model_root)
        output = tmp_path / f"behavioural-{version}.arxml"

        generate_arxml(str(model_root), str(output), version=version)

        _assert_conforms(str(output), version)
        root = etree.parse(str(output)).getroot()
        for tag in (
            "SW-CALIBRATION-ACCESS",
            "SW-IMPL-POLICY",
            "QUEUED-SENDER-COM-SPEC",
            "QUEUED-RECEIVER-COM-SPEC",
            "QUEUE-LENGTH",
            "DISABLED-MODE-IREF",
        ):
            assert list(root.iter(ar_qname(tag))), tag
        assert "true" in {node.text for node in root.iter(ar_qname("IS-SERVICE"))}
        # [TPS_SWCT_01219]: the composition's provide port on a queued element, a queued sender.
        (composition,) = root.iter(ar_qname("COMPOSITION-SW-COMPONENT-TYPE"))
        assert [
            etree.QName(spec).localname
            for port in composition.iter(ar_qname("P-PORT-PROTOTYPE"))
            if port.findtext(ar_qname("SHORT-NAME")) == "pQueued"
            for spec in port.iter(ar_qname("QUEUED-SENDER-COM-SPEC"))
        ] == ["QUEUED-SENDER-COM-SPEC"]


# The closed child tables' expectation, re-derived

_XS = "{http://www.w3.org/2001/XMLSchema}"
#: Each covered owner of the closed child tables and the complex type the schemas declare it by.
_CHILD_SET_OWNERS = {
    "VARIABLE-DATA-PROTOTYPE": "VARIABLE-DATA-PROTOTYPE",
    "ARGUMENT-DATA-PROTOTYPE": "ARGUMENT-DATA-PROTOTYPE",
    "IMPLEMENTATION-DATA-TYPE": "IMPLEMENTATION-DATA-TYPE",
    "IMPLEMENTATION-DATA-TYPE-ELEMENT": "IMPLEMENTATION-DATA-TYPE-ELEMENT",
    "SW-DATA-DEF-PROPS-CONDITIONAL": "SW-DATA-DEF-PROPS-CONDITIONAL",
    "NONQUEUED-SENDER-COM-SPEC": "NONQUEUED-SENDER-COM-SPEC",
    "NONQUEUED-RECEIVER-COM-SPEC": "NONQUEUED-RECEIVER-COM-SPEC",
    "QUEUED-SENDER-COM-SPEC": "QUEUED-SENDER-COM-SPEC",
    "QUEUED-RECEIVER-COM-SPEC": "QUEUED-RECEIVER-COM-SPEC",
    "SERVER-COM-SPEC": "SERVER-COM-SPEC",
    "CLIENT-COM-SPEC": "CLIENT-COM-SPEC",
    "DATA-FILTER": "DATA-FILTER",
    "SENDER-RECEIVER-INTERFACE": "SENDER-RECEIVER-INTERFACE",
    "CLIENT-SERVER-INTERFACE": "CLIENT-SERVER-INTERFACE",
    "TIMING-EVENT": "TIMING-EVENT",
    "R-MODE-IN-ATOMIC-SWC-INSTANCE-REF": "R-MODE-IN-ATOMIC-SWC-INSTANCE-REF",
    "DATA-CONSTR-RULE": "DATA-CONSTR-RULE",
    "COMPU-SCALE": "COMPU-SCALE",
    "AR-PACKAGE": "AR-PACKAGE",
    "DESC": "MULTI-LANGUAGE-OVERVIEW-PARAGRAPH",
    "L-2": "L-OVERVIEW-PARAGRAPH",
    "LONG-NAME": "MULTILANGUAGE-LONG-NAME",
    "L-4": "L-LONG-NAME",
    "SWC-TIMING": "SWC-TIMING",
    "EXECUTION-TIME-CONSTRAINT": "EXECUTION-TIME-CONSTRAINT",
    "DATA-RECEIVED-EVENT": "DATA-RECEIVED-EVENT",
    "R-VARIABLE-IN-ATOMIC-SWC-INSTANCE-REF": "R-VARIABLE-IN-ATOMIC-SWC-INSTANCE-REF",
}
_CHILD_SET_GROUPS = ("REFERRABLE", "MULTILANGUAGE-REFERRABLE", "IDENTIFIABLE")


def _schema_children(
    node: etree._Element,
    groups: dict[str, etree._Element],
    attribute_groups: dict[str, etree._Element],
    elements: set[str],
    attributes: set[str],
    seen: set[str],
) -> None:
    """Every element and attribute *node* admits, expanded through its (attribute) groups."""
    for child in node:
        if not isinstance(child.tag, str) or child.tag == _XS + "annotation":
            continue
        if child.tag == _XS + "element" and child.get("name"):
            elements.add(child.get("name"))
        elif child.tag == _XS + "group" and child.get("ref"):
            name = child.get("ref").split(":")[-1]
            if name not in seen:
                seen.add(name)
                _schema_children(groups[name], groups, attribute_groups, elements, attributes, seen)
        elif child.tag == _XS + "attribute" and child.get("name"):
            attributes.add(child.get("name"))
        elif child.tag == _XS + "attributeGroup" and child.get("ref"):
            name = child.get("ref").split(":")[-1]
            _schema_children(attribute_groups[name], groups, attribute_groups, elements, attributes, seen)
        else:
            _schema_children(child, groups, attribute_groups, elements, attributes, seen)


def derive_child_sets(schemas: dict[str, str]) -> dict[str, dict[str, dict[str, list[str]]]]:
    """``fixtures/xsd_child_sets.json``'s content, derived from the licensed schemas named by *schemas*.

    *schemas* maps a release (``4.2.2``, ``4.3.0``) to its XSD's path.
    """
    out: dict[str, dict[str, dict[str, list[str]]]] = {}
    for release, path in schemas.items():
        root = etree.parse(path).getroot()
        complex_types = {e.get("name"): e for e in root.iter(_XS + "complexType") if e.get("name")}
        groups = {e.get("name"): e for e in root.iter(_XS + "group") if e.get("name")}
        attribute_groups = {e.get("name"): e for e in root.iter(_XS + "attributeGroup") if e.get("name")}
        for owner, type_name in _CHILD_SET_OWNERS.items():
            elements: set[str] = set()
            attributes: set[str] = set()
            _schema_children(complex_types[type_name], groups, attribute_groups, elements, attributes, set())
            sets = out.setdefault(owner, {"elements": {}, "attributes": {}})
            for element in elements:
                sets["elements"].setdefault(element, []).append(release)
            for attribute in attributes:
                sets["attributes"].setdefault(attribute, []).append(release)
        for group in _CHILD_SET_GROUPS:
            elements = set()
            _schema_children(groups[group], groups, attribute_groups, elements, set(), set())
            sets = out.setdefault(f"group {group}", {"elements": {}, "attributes": {}})
            for element in elements:
                sets["elements"].setdefault(element, []).append(release)
    return out


class TestTheChildSetsAreTheSchemas:
    """``fixtures/xsd_child_sets.json`` is what the two licensed schemas admit.

    The schemas are never committed, so ``test_closed_child_tables.py`` holds the tables against
    the JSON and this class re-derives it from both schemas.
    """

    def test_the_committed_json_is_the_derivation(self) -> None:
        schemas = {str(version): require_licensed_schema(version) for version in _CONFORMANCE_VERSIONS}
        committed = json.loads((Path(__file__).parent / "fixtures" / "xsd_child_sets.json").read_text(encoding="utf-8"))
        assert derive_child_sets(schemas) == committed
