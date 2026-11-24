"""Tests for structural and optional XSD ARXML validation.

The target version is read from the file's own ``xsi:schemaLocation``, never
from a flag, so ``scripts/dev.py`` and the shipped template keep their static,
bare-path invocations.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest
import swcraft_tools.arxml_validation as _validation
import swcraft_tools.env as swcraft_env
import yaml
from conftest import (
    REPO_ROOT,
    TEMPLATE_ROOT,
    dev_context,
    dev_module,
)
from swcraft_tools.arxml_validation import (
    MODE_STRUCTURAL,
    MODE_XSD,
    SCHEMA_PATH_ENV,
    _resolve_schema_path,
    _schema_recursion_limit,
    check_arxml,
    validate_arxml,
)
from swcraft_tools.arxml_version import NAMESPACE, SCHEMA_FILENAMES, ArxmlVersion
from swcraft_tools.builder import parse_moon_errors
from symlink_support import symlink_or_skip
from xsd_support import require_licensed_schema, run_without_a_checkout

VALID_ARXML = """\
<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR>
  <AR-PACKAGES/>
</AUTOSAR>
"""

MALFORMED_ARXML = "<AUTOSAR><AR-PACKAGES></AUTOSAR>"
NON_AUTOSAR_XML = '<?xml version="1.0"?>\n<NotAutosar/>'

XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"

TINY_XSD = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="value" type="xs:string"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "arxml_golden"

#: The module's own default mapping, read before the autouse fixture below patches it.
_CHECKOUT_DEFAULT_SCHEMA_PATHS = dict(_validation.DEFAULT_SCHEMA_PATHS)


@pytest.fixture(autouse=True)
def _isolate_from_installed_autosar_schema(monkeypatch):
    """Keep structural-mode tests independent of a developer's local XSD.

    Patches ``DEFAULT_SCHEMA_PATHS`` and clears ``SCHEMA_PATH_ENV``, which outranks it.
    """
    monkeypatch.delenv(SCHEMA_PATH_ENV, raising=False)
    monkeypatch.setattr(
        _validation,
        "DEFAULT_SCHEMA_PATHS",
        {version: f"/nonexistent/{filename}" for version, filename in SCHEMA_FILENAMES.items()},
    )
    # The compiled-schema cache is process-global and keyed by (realpath, mtime,
    # size). Two tests writing different schemas to the same `tmp_path` name
    # inside one filesystem timestamp tick would otherwise share a compile.
    _validation._SCHEMA_CACHE.clear()


def _write_valid_arxml(path):
    path.write_text(VALID_ARXML, encoding="utf-8")
    return path


def _declared_arxml(schema_filename: str) -> str:
    """A minimal well-formed AUTOSAR document declaring *schema_filename*."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<AUTOSAR xmlns="{NAMESPACE}" xmlns:xsi="{XSI_NAMESPACE}" '
        f'xsi:schemaLocation="{NAMESPACE} {schema_filename}">\n'
        "  <AR-PACKAGES/>\n"
        "</AUTOSAR>\n"
    )


def _write_declared(tmp_path, schema_filename: str, name: str = "model.arxml"):
    path = tmp_path / name
    path.write_text(_declared_arxml(schema_filename), encoding="utf-8")
    return path


@pytest.fixture
def recorded_schemas(monkeypatch):
    """Replace XSD validation with a recorder of the schema path it was handed.

    The licensed schemas are absent from this checkout, so the resolution is asserted directly.
    """
    seen: list[str] = []

    def _record(arxml_path: str, schema_path: str) -> _validation._SchemaCheck:
        seen.append(schema_path)
        return _validation._SchemaCheck(True)

    monkeypatch.setattr(_validation, "_validate_against_schema", _record)
    return seen


@pytest.fixture
def installed_schemas(tmp_path, monkeypatch):
    """Make every version's default schema path resolve to an existing file."""
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    paths = {}
    for version, filename in SCHEMA_FILENAMES.items():
        path = schema_dir / filename
        path.write_text(TINY_XSD, encoding="utf-8")
        paths[version] = str(path)
    monkeypatch.setattr(_validation, "DEFAULT_SCHEMA_PATHS", paths)
    return paths


# Basic behaviour


def test_validate_valid_arxml_returns_true(tmp_path):
    arxml = _write_valid_arxml(tmp_path / "model.arxml")
    assert validate_arxml(str(arxml)) is True


def test_validate_malformed_arxml_returns_false(tmp_path):
    bad = tmp_path / "bad.arxml"
    bad.write_text(MALFORMED_ARXML, encoding="utf-8")
    assert validate_arxml(str(bad)) is False


def test_validate_non_autosar_xml_returns_false(tmp_path):
    bad = tmp_path / "other.xml"
    bad.write_text(NON_AUTOSAR_XML, encoding="utf-8")
    assert validate_arxml(str(bad)) is False


def test_validate_nonexistent_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        validate_arxml(str(tmp_path / "does_not_exist.arxml"))


def test_validate_against_xsd_schema(tmp_path):
    schema = tmp_path / "tiny.xsd"
    schema.write_text(TINY_XSD, encoding="utf-8")

    good = tmp_path / "good.xml"
    good.write_text("<root><value>ok</value></root>", encoding="utf-8")
    assert validate_arxml(str(good), schema_path=str(schema)) is True

    bad = tmp_path / "wrong.xml"
    bad.write_text("<root><unexpected/></root>", encoding="utf-8")
    assert validate_arxml(str(bad), schema_path=str(schema)) is False


def test_schema_recursion_limit_default_is_large_enough_for_autosar(monkeypatch):
    monkeypatch.delenv("SWCRAFT_XML_SCHEMA_RECURSION_LIMIT", raising=False)
    assert _schema_recursion_limit() == 100_000


def test_schema_recursion_limit_can_be_overridden(monkeypatch):
    monkeypatch.setenv("SWCRAFT_XML_SCHEMA_RECURSION_LIMIT", "123456")
    assert _schema_recursion_limit() == 123456


def test_schema_recursion_limit_invalid_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SWCRAFT_XML_SCHEMA_RECURSION_LIMIT", "not-an-int")
    assert _schema_recursion_limit() == 100_000


# An XSD failure must say why


class TestXsdFailuresAreDiagnosable:
    """``valid=False`` carries a readable error list, not a bare verdict.

    ``swcraft validate`` and the MCP ``validate()``/``build()`` tools gate on it,
    and ``packages/agent/src/swcraft_agent/agent-system-prompt.md`` tells the agent
    to read the structured error list before spending a retry.
    """

    def test_an_invalid_document_reports_the_element_path_and_the_reason(self, tmp_path):
        schema = tmp_path / "tiny.xsd"
        schema.write_text(TINY_XSD, encoding="utf-8")
        bad = tmp_path / "wrong.xml"
        bad.write_text("<root><unexpected/></root>", encoding="utf-8")

        outcome = check_arxml(str(bad), schema_path=str(schema))

        assert outcome.valid is False
        assert outcome.schema_error is False
        joined = "\n".join(outcome.messages)
        assert "does not conform to" in joined
        assert "unexpected" in joined

    def test_the_diagnostics_reach_stderr(self, tmp_path, capsys):
        """``_report`` is the single emission point, so the bool-only ``validate_arxml`` cannot drop them."""
        schema = tmp_path / "tiny.xsd"
        schema.write_text(TINY_XSD, encoding="utf-8")
        bad = tmp_path / "wrong.xml"
        bad.write_text("<root><unexpected/></root>", encoding="utf-8")

        outcome = check_arxml(str(bad), schema_path=str(schema))

        captured = capsys.readouterr().err
        for message in outcome.messages:
            assert message in captured

    def test_the_error_list_is_capped(self, tmp_path):
        """One misplaced element in a real AUTOSAR document produces hundreds."""
        schema = tmp_path / "many.xsd"
        schema.write_text(
            '<?xml version="1.0"?>\n'
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
            '  <xs:element name="root">\n'
            "    <xs:complexType><xs:sequence>\n"
            '      <xs:element name="value" type="xs:int" maxOccurs="unbounded"/>\n'
            "    </xs:sequence></xs:complexType>\n"
            "  </xs:element>\n"
            "</xs:schema>\n",
            encoding="utf-8",
        )
        bad = tmp_path / "many.xml"
        offenders = "".join("<value>nope</value>" for _ in range(_validation.MAX_SCHEMA_ERRORS + 5))
        bad.write_text(f"<root>{offenders}</root>", encoding="utf-8")

        outcome = check_arxml(str(bad), schema_path=str(schema))

        assert outcome.valid is False
        # One header line, the capped diagnostics, and the "there were more" line.
        assert len(outcome.messages) == _validation.MAX_SCHEMA_ERRORS + 2
        assert "and further errors" in outcome.messages[-1]

    def test_an_uncompilable_schema_is_a_toolchain_error_not_a_model_error(self, tmp_path):
        """An uncompilable schema is reported as a toolchain error, not a model error.

        ``scripts/download_autosar_schema.py`` fetches through an unchecked
        ``urlretrieve``, so a corporate SSO redirect can write an HTML login page
        to ``schemas/AUTOSAR_4-2-2.xsd``; that must not be blamed on the model.
        """
        not_a_schema = tmp_path / "AUTOSAR_4-2-2.xsd"
        not_a_schema.write_text("<html><body>SSO login required</body></html>", encoding="utf-8")
        arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_2_2])

        outcome = check_arxml(str(arxml), schema_path=str(not_a_schema))

        assert outcome.valid is False
        assert outcome.schema_error is True
        joined = "\n".join(outcome.messages)
        assert "could not be compiled" in joined
        assert "not a problem with the model" in joined

    def test_a_valid_document_carries_no_diagnostics(self, tmp_path):
        schema = tmp_path / "tiny.xsd"
        schema.write_text(TINY_XSD, encoding="utf-8")
        good = tmp_path / "good.xml"
        good.write_text("<root><value>ok</value></root>", encoding="utf-8")

        outcome = check_arxml(str(good), schema_path=str(schema))

        assert outcome.valid is True
        assert outcome.messages == ()
        assert outcome.schema_error is False

    def test_main_marks_an_unusable_schema_without_breaking_the_moon_parser(self, tmp_path, monkeypatch, capsys):
        """The note gets a line of its own, and the verdict line stays clean.

        ``builder.parse_moon_errors`` takes everything after ``INVALID (<mode> mode): `` as the
        path the agent's fix is aimed at, so a note on that line would corrupt it; asserted
        through the real parser rather than by string shape.
        """
        not_a_schema = tmp_path / SCHEMA_FILENAMES[ArxmlVersion.V4_2_2]
        not_a_schema.write_text("<html/>", encoding="utf-8")
        monkeypatch.setenv(SCHEMA_PATH_ENV, str(not_a_schema))
        arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_2_2])

        assert _validation.main([str(arxml)]) == 1

        out = capsys.readouterr().out
        assert f"INVALID ({MODE_XSD} mode): {arxml}\n" in out
        assert "the schema could not be compiled" in out

        prefixed = "\n".join(f"validate-arxml | {line}" for line in out.splitlines())
        errors = parse_moon_errors(prefixed)
        assert [error.path for error in errors] == [str(arxml)]


class TestCompiledSchemasAreCached:
    """Compiled schemas are cached: the AUTOSAR schema is expensive to compile.

    Compilation happens in-process in the long-lived MCP server on every ``validate()``/``build()``.
    """

    def test_one_schema_is_compiled_once(self, tmp_path, monkeypatch):
        schema = tmp_path / "tiny.xsd"
        schema.write_text(TINY_XSD, encoding="utf-8")
        good = tmp_path / "good.xml"
        good.write_text("<root><value>ok</value></root>", encoding="utf-8")

        compiles: list[str] = []
        real = _validation.xmlschema.XMLSchema

        def _counting(path, *args, **kwargs):
            compiles.append(str(path))
            return real(path, *args, **kwargs)

        monkeypatch.setattr(_validation.xmlschema, "XMLSchema", _counting)

        for _ in range(3):
            assert check_arxml(str(good), schema_path=str(schema)).valid is True

        assert compiles == [str(schema)]

    def test_an_edited_schema_replaces_its_entry_rather_than_adding_one(self, tmp_path):
        """One entry per schema file, not one per ``(path, mtime, size)``.

        A compiled AUTOSAR schema is large and the cache lives in the long-lived
        MCP server, so a stale entry per schema replacement would leak.
        """
        schema = tmp_path / "tiny.xsd"
        document = tmp_path / "doc.xml"
        document.write_text("<root><value>ok</value></root>", encoding="utf-8")

        for tick in range(3):
            schema.write_text(TINY_XSD + f"<!-- {tick} -->", encoding="utf-8")
            os.utime(schema, (tick, tick))
            check_arxml(str(document), schema_path=str(schema))

        assert list(_validation._SCHEMA_CACHE) == [os.path.realpath(schema)]

    def test_two_spellings_of_one_file_share_the_single_entry(self, tmp_path):
        schema = tmp_path / "tiny.xsd"
        schema.write_text(TINY_XSD, encoding="utf-8")
        alias = tmp_path / "current.xsd"
        symlink_or_skip(alias, schema)
        document = tmp_path / "doc.xml"
        document.write_text("<root><value>ok</value></root>", encoding="utf-8")

        check_arxml(str(document), schema_path=str(schema))
        check_arxml(str(document), schema_path=str(alias))

        assert list(_validation._SCHEMA_CACHE) == [os.path.realpath(schema)]

    def test_an_edited_schema_is_recompiled(self, tmp_path, monkeypatch):
        """The entry carries mtime and size, so a replaced schema is never served stale."""
        schema = tmp_path / "tiny.xsd"
        schema.write_text(TINY_XSD, encoding="utf-8")
        document = tmp_path / "doc.xml"
        document.write_text("<root><value>ok</value></root>", encoding="utf-8")
        assert check_arxml(str(document), schema_path=str(schema)).valid is True

        schema.write_text(TINY_XSD.replace('name="value"', 'name="other"'), encoding="utf-8")
        # A same-tick rewrite of the same size would collide; make the key differ
        # the way a real schema swap does.
        os.utime(schema, (0, 0))

        assert check_arxml(str(document), schema_path=str(schema)).valid is False

    def test_an_uncompilable_schema_is_not_recompiled_per_call(self, tmp_path, monkeypatch):
        """A broken schema stays broken for the session and is not recompiled per call."""
        broken = tmp_path / "broken.xsd"
        broken.write_text("<html/>", encoding="utf-8")
        document = tmp_path / "doc.xml"
        document.write_text("<root/>", encoding="utf-8")

        compiles: list[str] = []
        real = _validation.xmlschema.XMLSchema

        def _counting(path, *args, **kwargs):
            compiles.append(str(path))
            return real(path, *args, **kwargs)

        monkeypatch.setattr(_validation.xmlschema, "XMLSchema", _counting)

        for _ in range(3):
            assert check_arxml(str(document), schema_path=str(broken)).schema_error is True

        assert compiles == [str(broken)]

    def test_the_recursion_limit_is_restored_after_compilation(self, tmp_path):
        """A limit left raised is a limit the rest of the process runs under."""
        schema = tmp_path / "tiny.xsd"
        schema.write_text(TINY_XSD, encoding="utf-8")
        good = tmp_path / "good.xml"
        good.write_text("<root><value>ok</value></root>", encoding="utf-8")

        before = sys.getrecursionlimit()
        check_arxml(str(good), schema_path=str(schema))
        assert sys.getrecursionlimit() == before


# The isolation fixture must patch what resolution actually reads


class TestIsolationFixtureIsNotANoOp:
    """The isolation fixture patches what resolution actually reads, not an unread constant."""

    @pytest.mark.parametrize("version", list(ArxmlVersion))
    def test_no_default_schema_resolves_under_the_fixture(self, version):
        assert _resolve_schema_path(version) == (None, ())


# The version comes from the file


class TestVersionInference:
    @pytest.mark.parametrize("version", list(ArxmlVersion))
    def test_the_declared_version_selects_its_own_schema(self, tmp_path, version, installed_schemas, recorded_schemas):
        arxml = _write_declared(tmp_path, version.schema_filename)

        outcome = check_arxml(str(arxml))

        assert outcome.version is version
        assert outcome.mode == MODE_XSD
        assert recorded_schemas == [installed_schemas[version]]

    def test_a_4_3_0_file_is_never_validated_against_the_4_2_2_schema(
        self, tmp_path, installed_schemas, recorded_schemas
    ):
        arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_3_0])

        check_arxml(str(arxml))

        assert recorded_schemas == [installed_schemas[ArxmlVersion.V4_3_0]]
        assert installed_schemas[ArxmlVersion.V4_2_2] not in recorded_schemas

    @pytest.mark.parametrize("version", list(ArxmlVersion))
    @pytest.mark.parametrize("spelling", [str.lower, str.upper])
    def test_the_declared_filename_is_matched_case_insensitively(
        self, tmp_path, version, spelling, installed_schemas, recorded_schemas
    ):
        """The declared schema filename is matched case-insensitively, like the configured one.

        Guards ``autosar_4-2-2.xsd`` being reported as an unsupported version.
        """
        arxml = _write_declared(tmp_path, spelling(version.schema_filename))

        outcome = check_arxml(str(arxml))

        assert outcome.version is version
        assert outcome.valid is True
        assert recorded_schemas == [installed_schemas[version]]

    def test_an_unrecognized_version_is_an_error_not_a_fallback(
        self, tmp_path, installed_schemas, recorded_schemas, capsys
    ):
        arxml = _write_declared(tmp_path, "AUTOSAR_4-1-1.xsd")

        outcome = check_arxml(str(arxml))

        assert outcome.valid is False
        assert recorded_schemas == []
        assert any("4.1.1" in message for message in outcome.messages)
        assert "4.1.1" in capsys.readouterr().err

    def test_an_unrecognized_version_message_names_the_supported_ones(self, tmp_path):
        arxml = _write_declared(tmp_path, "AUTOSAR_00042.xsd")

        outcome = check_arxml(str(arxml))

        assert outcome.valid is False
        joined = " ".join(outcome.messages)
        assert "AUTOSAR_00042.xsd" in joined
        for version in ArxmlVersion:
            assert version.value in joined

    def test_a_declaration_for_a_foreign_namespace_is_treated_as_no_declaration(self, tmp_path, installed_schemas):
        arxml = tmp_path / "foreign.arxml"
        arxml.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            f'<AUTOSAR xmlns:xsi="{XSI_NAMESPACE}" '
            'xsi:schemaLocation="http://example.invalid/other other.xsd">\n'
            "  <AR-PACKAGES/>\n"
            "</AUTOSAR>\n",
            encoding="utf-8",
        )

        outcome = check_arxml(str(arxml))

        assert outcome.valid is True
        assert outcome.version is None
        assert outcome.mode == MODE_STRUCTURAL


class TestContractTable:
    """The five rows of the version contract table, each as its own assertion."""

    def test_supported_version_with_schema_is_xsd_validated(self, tmp_path, installed_schemas, recorded_schemas):
        arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_2_2])
        outcome = check_arxml(str(arxml))
        assert (outcome.valid, outcome.mode) == (True, MODE_XSD)

    def test_supported_version_without_schema_degrades_to_structural(self, tmp_path):
        arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_3_0])

        outcome = check_arxml(str(arxml))

        assert outcome.valid is True
        assert outcome.mode == MODE_STRUCTURAL
        assert outcome.version is ArxmlVersion.V4_3_0
        assert outcome.schema_path is None

    def test_no_declaration_stays_structural_even_when_schemas_are_installed(self, tmp_path, installed_schemas):
        """A file declaring no version is never validated against one, even with schemas installed."""
        arxml = _write_valid_arxml(tmp_path / "undeclared.arxml")

        outcome = check_arxml(str(arxml))

        assert outcome.valid is True
        assert outcome.mode == MODE_STRUCTURAL
        assert outcome.version is None

    def test_an_unrecognized_version_returns_false_without_raising(self, tmp_path):
        arxml = _write_declared(tmp_path, "AUTOSAR_9-9-9.xsd")
        assert validate_arxml(str(arxml)) is False

    def test_malformed_xml_returns_false_rather_than_raising_out_of_inference(self, tmp_path, installed_schemas):
        """Inference runs ahead of the structural fallback and must not raise."""
        bad = tmp_path / "bad.arxml"
        bad.write_text(MALFORMED_ARXML, encoding="utf-8")

        outcome = check_arxml(str(bad))

        assert outcome.valid is False
        assert outcome.mode == MODE_STRUCTURAL


# Resolution precedence


class TestSchemaResolutionPrecedence:
    def test_an_explicit_schema_path_wins_over_the_env_var_and_the_default(
        self, tmp_path, monkeypatch, installed_schemas, recorded_schemas
    ):
        chosen = tmp_path / "chosen.xsd"
        chosen.write_text(TINY_XSD, encoding="utf-8")
        monkeypatch.setenv(SCHEMA_PATH_ENV, installed_schemas[ArxmlVersion.V4_2_2])
        arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_2_2])

        check_arxml(str(arxml), schema_path=str(chosen))

        assert recorded_schemas == [str(chosen)]

    def test_the_env_var_wins_over_the_per_version_default(
        self, tmp_path, monkeypatch, installed_schemas, recorded_schemas
    ):
        configured = tmp_path / SCHEMA_FILENAMES[ArxmlVersion.V4_2_2]
        configured.write_text(TINY_XSD, encoding="utf-8")
        monkeypatch.setenv(SCHEMA_PATH_ENV, str(configured))
        arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_2_2])

        outcome = check_arxml(str(arxml))

        assert recorded_schemas == [str(configured)]
        assert outcome.messages == ()

    def test_a_mismatched_env_var_filename_degrades_to_structural_with_a_warning(
        self, tmp_path, monkeypatch, recorded_schemas, capsys
    ):
        """A 4.2.2 schema configured for a 4.3.0 file degrades to structural with a warning.

        Validating 4.3.0 output against the 4.2.2 schema would fail the gate; refusing to guess is
        the "unrecognized version" row's rule.
        """
        stale = tmp_path / SCHEMA_FILENAMES[ArxmlVersion.V4_2_2]
        stale.write_text(TINY_XSD, encoding="utf-8")
        monkeypatch.setenv(SCHEMA_PATH_ENV, str(stale))
        arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_3_0])

        outcome = check_arxml(str(arxml))

        assert recorded_schemas == []
        assert outcome.mode == MODE_STRUCTURAL
        assert outcome.valid is True
        assert outcome.schema_path is None
        warning = " ".join(outcome.messages)
        assert SCHEMA_PATH_ENV in warning
        assert SCHEMA_FILENAMES[ArxmlVersion.V4_3_0] in warning
        assert warning.strip() in capsys.readouterr().err

    def test_a_mismatched_env_var_does_not_fail_the_validate_gate(self, tmp_path, monkeypatch):
        """A misrouted schema cannot produce the invalid verdict ``cmd_validate`` exits non-zero on."""
        stale = tmp_path / SCHEMA_FILENAMES[ArxmlVersion.V4_2_2]
        stale.write_text(TINY_XSD, encoding="utf-8")
        monkeypatch.setenv(SCHEMA_PATH_ENV, str(stale))
        arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_3_0])

        assert check_arxml(str(arxml)).valid is True

    def test_an_env_var_pointing_at_a_missing_file_degrades_with_a_warning(self, tmp_path, monkeypatch, capsys):
        """A variable naming a missing file degrades with a warning, not in silence.

        A stale variable (a moved schema, a copied ``.env``, a typo) must be
        distinguishable from a machine with no licensed schema.
        """
        missing = tmp_path / "no-such-directory" / "AUTOSAR_4-2-2.xsd"
        monkeypatch.setenv(SCHEMA_PATH_ENV, str(missing))
        arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_2_2])

        outcome = check_arxml(str(arxml))

        assert outcome.valid is True
        assert outcome.mode == MODE_STRUCTURAL
        warning = " ".join(outcome.messages)
        assert str(missing) in warning
        assert "does not exist" in warning
        assert warning.strip() in capsys.readouterr().err

    def test_a_differently_cased_filename_is_still_that_versions_schema(self, tmp_path, monkeypatch, recorded_schemas):
        """The configured filename is compared case-insensitively.

        Filenames are case-insensitive on macOS and Windows; an exact comparison
        silently downgraded a correct schema to a structural pass.
        """
        configured = tmp_path / SCHEMA_FILENAMES[ArxmlVersion.V4_2_2].lower()
        configured.write_text(TINY_XSD, encoding="utf-8")
        monkeypatch.setenv(SCHEMA_PATH_ENV, str(configured))
        arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_2_2])

        outcome = check_arxml(str(arxml))

        assert recorded_schemas == [str(configured)]
        assert outcome.mode == MODE_XSD
        assert outcome.messages == ()

    def test_a_symlink_onto_the_right_schema_is_accepted(self, tmp_path, monkeypatch, recorded_schemas):
        """``current.xsd -> AUTOSAR_4-2-2.xsd`` names the right schema through its target, not the link's basename."""
        real = tmp_path / SCHEMA_FILENAMES[ArxmlVersion.V4_2_2]
        real.write_text(TINY_XSD, encoding="utf-8")
        link = tmp_path / "current.xsd"
        symlink_or_skip(link, real)
        monkeypatch.setenv(SCHEMA_PATH_ENV, str(link))
        arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_2_2])

        outcome = check_arxml(str(arxml))

        assert recorded_schemas == [str(link)]
        assert outcome.mode == MODE_XSD

    def test_a_vendor_renamed_copy_is_still_refused(self, tmp_path, monkeypatch, recorded_schemas):
        """A vendor-renamed copy is refused rather than guessed, with a warning.

        Nothing in an AUTOSAR XSD distinguishes 4.2.2 from 4.3.0 (the namespace is ``r4.0`` across the 4.x line), so a
        name that does not identify the version is never guessed.
        """
        renamed = tmp_path / "acme-autosar-schema.xsd"
        renamed.write_text(TINY_XSD, encoding="utf-8")
        monkeypatch.setenv(SCHEMA_PATH_ENV, str(renamed))
        arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_2_2])

        outcome = check_arxml(str(arxml))

        assert recorded_schemas == []
        assert outcome.mode == MODE_STRUCTURAL
        assert SCHEMA_FILENAMES[ArxmlVersion.V4_2_2] in " ".join(outcome.messages)

    def test_the_per_version_default_is_the_last_resort(self, tmp_path, installed_schemas):
        resolved, messages = _resolve_schema_path(ArxmlVersion.V4_3_0)
        assert resolved == installed_schemas[ArxmlVersion.V4_3_0]
        assert messages == ()

    def test_an_undeclared_file_has_no_default_to_fall_back_to(self, installed_schemas):
        assert _resolve_schema_path(None) == (None, ())

    def test_an_undeclared_file_is_structural_even_with_the_env_var_set(self, tmp_path, monkeypatch):
        """The contract table's "nothing declared" row is unconditional.

        Guards the ``version is None`` check sitting after the env-var branch,
        which gave one file two answers depending on ``SWCRAFT_AUTOSAR_XSD_PATH``.
        """
        schema = tmp_path / "AUTOSAR_4-2-2.xsd"
        schema.write_text(TINY_XSD, encoding="utf-8")
        monkeypatch.setenv(SCHEMA_PATH_ENV, str(schema))
        assert _resolve_schema_path(None) == (None, ())

        arxml = _write_valid_arxml(tmp_path / "undeclared.arxml")
        outcome = check_arxml(str(arxml))
        assert outcome.mode == MODE_STRUCTURAL
        assert outcome.valid is True
        assert outcome.version is None

    def test_an_explicit_schema_path_is_still_the_way_to_check_an_undeclared_file(self, tmp_path):
        """The documented escape hatch, so the row above is a routing rule and not a refusal to validate."""
        schema = tmp_path / "tiny.xsd"
        schema.write_text(TINY_XSD, encoding="utf-8")
        good = tmp_path / "good.xml"
        good.write_text("<root><value>ok</value></root>", encoding="utf-8")

        outcome = check_arxml(str(good), schema_path=str(schema))

        assert outcome.mode == MODE_XSD
        assert outcome.valid is True


class TestAbsentSchemaDegrades:
    def test_every_version_degrades_rather_than_raising(self, tmp_path):
        for version in ArxmlVersion:
            arxml = _write_declared(tmp_path, version.schema_filename, name=f"{version.name}.arxml")
            outcome = check_arxml(str(arxml))
            assert outcome.mode == MODE_STRUCTURAL
            assert outcome.valid is True

    def test_the_committed_golden_xsd_validates_when_the_schema_is_present(self, monkeypatch):
        """The resolution path reaches a real schema; conformance itself lives in ``test_arxml_xsd_conformance.py``.

        Resolved inside the body, not by a ``skipif`` marker, so ``--require-autosar-xsd`` (``dev.py test-xsd``) can
        turn the absent-schema skip into a failure.
        """
        schema = require_licensed_schema(ArxmlVersion.V4_2_2)
        monkeypatch.setattr(
            _validation,
            "DEFAULT_SCHEMA_PATHS",
            {**_validation.DEFAULT_SCHEMA_PATHS, ArxmlVersion.V4_2_2: schema},
        )

        outcome = check_arxml(str(GOLDEN_DIR / "basic-4.2.2.arxml"))

        assert outcome.mode == MODE_XSD
        assert outcome.version is ArxmlVersion.V4_2_2
        assert outcome.valid is True


# main()'s reported mode


class TestMainReportsTheModeTheFileGot:
    """``main()`` reports the mode the file was validated in, which only validation itself can know."""

    @pytest.fixture(autouse=True)
    def _no_dotenv(self, monkeypatch):
        """``main()`` loads a ``.env``, which could reintroduce the env var; every test here calls it."""
        calls: list[object] = []
        monkeypatch.setattr(swcraft_env, "load_env_file", lambda *args, **kwargs: calls.append(args))
        yield
        assert calls, "main() did not reach the patched env.load_env_file"

    def test_a_declared_file_with_a_schema_reports_xsd_mode(
        self, tmp_path, capsys, installed_schemas, recorded_schemas
    ):
        arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_2_2])

        assert _validation.main([str(arxml)]) == 0
        assert f"VALID ({MODE_XSD} mode)" in capsys.readouterr().out

    def test_the_same_file_without_a_schema_reports_structural_mode(self, tmp_path, capsys):
        arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_2_2])

        assert _validation.main([str(arxml)]) == 0
        assert f"VALID ({MODE_STRUCTURAL} mode)" in capsys.readouterr().out

    def test_an_undeclared_file_reports_structural_mode_even_with_schemas_installed(
        self, tmp_path, capsys, installed_schemas
    ):
        arxml = _write_valid_arxml(tmp_path / "undeclared.arxml")

        assert _validation.main([str(arxml)]) == 0
        assert f"VALID ({MODE_STRUCTURAL} mode)" in capsys.readouterr().out

    def test_an_unrecognized_version_exits_non_zero_and_names_it(self, tmp_path, capsys):
        arxml = _write_declared(tmp_path, "AUTOSAR_4-1-1.xsd")

        assert _validation.main([str(arxml)]) == 1
        captured = capsys.readouterr()
        assert "INVALID" in captured.out
        assert "4.1.1" in captured.err


# The static call sites carry no version


class TestBarePathInvocationsAreUnchanged:
    """No version reaches this module through an argument, at either call site.

    A ``--arxml-version`` flag would leave the template's *static* arg list free
    to disagree with the project's ``arxml_version`` -- 4.3.0 output validated
    against the 4.2.2 schema and reported as a success.
    """

    def test_the_dev_py_build_step_passes_only_the_path(self, tmp_path):
        dev = dev_module()
        step = dev.plan_build(dev_context(tmp_path))[-1]
        assert list(step.argv[1:]) == [
            "-m",
            "swcraft_tools.arxml_validation",
            str(dev.reference_outputs(tmp_path)[0]),
        ]

    def test_the_template_validate_task_passes_the_project_and_no_version(self):
        """The template's gate runs ``swcraft validate`` on the manifest with no version argument."""
        manifest = yaml.safe_load((TEMPLATE_ROOT / "moon.yml").read_text(encoding="utf-8"))
        task = manifest["tasks"]["validate-arxml"]
        assert task["command"] == "swcraft"
        assert task["args"] == ["validate", "--project", "swcraft.yaml", "--report"]

    def test_the_module_parses_no_options_at_all(self):
        """``main()`` reads ``args[0]`` and nothing else -- there is no parser to grow a version flag on.

        Asserted structurally, since the docstring names the rejected flag in prose.
        """
        source = (REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "arxml_validation.py").read_text(
            encoding="utf-8"
        )
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "argparse" not in imported

    def test_running_it_with_a_bare_path_still_works(self, tmp_path, monkeypatch, capsys):
        calls: list[object] = []
        monkeypatch.setattr(swcraft_env, "load_env_file", lambda *args, **kwargs: calls.append(args))
        arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_2_2])
        monkeypatch.setattr("sys.argv", ["arxml_validation", str(arxml)])

        assert _validation.main() == 0
        assert str(arxml) in capsys.readouterr().out
        assert calls, "main() did not reach the patched env.load_env_file"


def test_the_schema_content_model_warning_does_not_escape_compilation(monkeypatch, tmp_path):
    """``_compile_schema`` filters xmlschema's content-model recursion warning, only while compiling.

    The warning concerns the schema's own UPA check and changes no verdict, but
    printed above ``Validation passed`` on every XSD-mode run; an unrelated
    schema warning still reaches the caller.
    """
    import warnings

    from xmlschema.exceptions import XMLSchemaWarning

    class _WarningSchema:
        def __init__(self, path):
            warnings.warn(
                "can't verify the content model of {!r} due to exceeding of maximum recursion depth",
                XMLSchemaWarning,
                stacklevel=1,
            )
            warnings.warn("an unrelated schema warning", XMLSchemaWarning, stacklevel=1)

    monkeypatch.setattr(_validation.xmlschema, "XMLSchema", _WarningSchema)
    schema = tmp_path / "AUTOSAR_4-2-2.xsd"
    schema.write_text("<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'/>", encoding="utf-8")
    _validation._SCHEMA_CACHE.clear()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        compiled, error = _validation._compile_schema(str(schema))

    assert error is None and isinstance(compiled, _WarningSchema)
    assert [str(w.message) for w in caught] == ["an unrelated schema warning"]
    _validation._SCHEMA_CACHE.clear()


def test_the_default_schema_directory_is_the_checkouts_schemas() -> None:
    """In a source checkout the default XSDs sit in ``<repository root>/schemas``.

    A wrong root would degrade XSD mode to structural without a word.
    """
    assert _validation.SCHEMA_DIR == REPO_ROOT / "schemas"
    assert _CHECKOUT_DEFAULT_SCHEMA_PATHS == {
        version: str(REPO_ROOT / "schemas" / filename) for version, filename in SCHEMA_FILENAMES.items()
    }


def test_an_installed_layout_has_no_default_schema(tmp_path: Path) -> None:
    """Without a checkout the module's import-time wiring gives no schema directory and no default."""
    result = run_without_a_checkout(
        "import swcraft_tools.arxml_validation as v\nprint(v.SCHEMA_DIR, v.DEFAULT_SCHEMA_PATHS)\n",
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "None {}\n"


def test_an_installed_layout_validates_a_declared_file_structurally(tmp_path: Path) -> None:
    """``main()`` without a checkout, with no schema configured, reports its own structural mode line."""
    arxml = _write_declared(tmp_path, SCHEMA_FILENAMES[ArxmlVersion.V4_2_2])

    result = run_without_a_checkout(
        f"import sys\nimport swcraft_tools.arxml_validation as v\nsys.exit(v.main([{str(arxml)!r}]))\n", tmp_path
    )

    assert result.returncode == 0, result.stderr
    assert f"VALID ({MODE_STRUCTURAL} mode): {arxml}" in result.stdout
