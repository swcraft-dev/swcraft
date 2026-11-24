"""``ArxmlVersion`` — the AUTOSAR schema versions SWCraft can emit.

* the **schema filename per member** comes from a table, not a format string: AUTOSAR uses the numbered
  ``AUTOSAR_000NN.xsd`` form from AP R17-03 on, which an ``AUTOSAR_{major}-{minor}-{patch}.xsd`` format
  cannot express, though 4.2.2 and 4.3.0 both use the dashed form;
* the **dependency direction**: ``project.py`` validates the manifest's ``arxml_version`` without
  importing the writer package, so the enum is its own top-level module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from swcraft_tools import arxml_version as arxml_version_module
from swcraft_tools.arxml_version import (
    DEFAULT_ARXML_VERSION,
    NAMESPACE,
    SCHEMA_FILENAMES,
    SUPPORTED_VERSIONS,
    ArxmlVersion,
    release_of_schema_filename,
    schema_filename_from_location,
)

MODULE_PATH = Path(arxml_version_module.__file__)


class TestMembers:
    """Both supported versions, their values and the 4.2.2 default."""

    def test_both_members_exist(self):
        assert [v.name for v in ArxmlVersion] == ["V4_2_2", "V4_3_0"]

    def test_member_values_are_the_dotted_version_strings(self):
        assert ArxmlVersion.V4_2_2.value == "4.2.2"
        assert ArxmlVersion.V4_3_0.value == "4.3.0"

    def test_member_formats_as_its_dotted_version_string(self):
        # StrEnum: a plain ``(str, Enum)`` mixin would give "ArxmlVersion.V4_2_2".
        assert str(ArxmlVersion.V4_2_2) == "4.2.2"
        assert f"{ArxmlVersion.V4_3_0}" == "4.3.0"

    def test_default_is_4_2_2(self):
        # Existing projects, examples/basic, the template and every committed
        # fixture keep producing 4.2.2 output.
        assert DEFAULT_ARXML_VERSION is ArxmlVersion.V4_2_2

    def test_supported_versions_lists_every_member_in_declaration_order(self):
        assert SUPPORTED_VERSIONS == ("4.2.2", "4.3.0")


class TestSchemaFilenames:
    """Version -> XSD filename, by table (never by a format string)."""

    def test_filename_per_member(self):
        assert ArxmlVersion.V4_2_2.schema_filename == "AUTOSAR_4-2-2.xsd"
        assert ArxmlVersion.V4_3_0.schema_filename == "AUTOSAR_4-3-0.xsd"

    def test_table_covers_every_member_exactly(self):
        # A member without an entry fails here rather than as a KeyError at generation time.
        assert set(SCHEMA_FILENAMES) == set(ArxmlVersion)

    def test_filenames_are_distinct(self):
        assert len(set(SCHEMA_FILENAMES.values())) == len(SCHEMA_FILENAMES)

    def test_module_source_contains_no_filename_format_string(self):
        # Behaviour cannot tell a table from a format string while both versions
        # use the dashed form, so the shape is asserted on the source.
        source = MODULE_PATH.read_text(encoding="utf-8")
        for filename in ("AUTOSAR_4-2-2.xsd", "AUTOSAR_4-3-0.xsd"):
            assert f'"{filename}"' in source


class TestSchemaLocation:
    """The ``xsi:schemaLocation`` value — namespace plus filename, space-separated."""

    def test_namespace_is_r4_0_for_every_member(self):
        # The namespace is stable across the 4.x line; only the schema filename differs.
        assert NAMESPACE == "http://autosar.org/schema/r4.0"
        assert {v.namespace for v in ArxmlVersion} == {NAMESPACE}

    def test_namespace_matches_the_reader_s(self):
        # The writer must emit the namespace the first-party reader traverses.
        from swcraft_tools.arxml_reader import AR_NS

        assert NAMESPACE == AR_NS

    def test_schema_location_per_member(self):
        assert ArxmlVersion.V4_2_2.schema_location == "http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd"
        assert ArxmlVersion.V4_3_0.schema_location == "http://autosar.org/schema/r4.0 AUTOSAR_4-3-0.xsd"


class TestTheDeclaredSchemaReading:
    """The one ``xsi:schemaLocation`` reading the XSD gate and the importer share."""

    @pytest.mark.parametrize("version", list(ArxmlVersion))
    def test_a_version_reads_back_from_its_own_schema_location(self, version):
        filename = schema_filename_from_location(version.schema_location)
        assert filename == version.schema_filename
        assert release_of_schema_filename(filename) is version

    @pytest.mark.parametrize("version", list(ArxmlVersion))
    def test_the_release_lookup_ignores_case(self, version):
        assert release_of_schema_filename(version.schema_filename.lower()) is version
        assert release_of_schema_filename(version.schema_filename.upper()) is version

    @pytest.mark.parametrize("value", [None, "", "   ", "http://example.com/other AUTOSAR_4-2-2.xsd"])
    def test_no_autosar_token_is_no_filename(self, value):
        assert schema_filename_from_location(value) is None

    @pytest.mark.parametrize("filename", [None, "AUTOSAR_00051.xsd", "AUTOSAR_4-1-1.xsd"])
    def test_a_schema_swcraft_does_not_write_is_no_release(self, filename):
        assert release_of_schema_filename(filename) is None


class TestParsing:
    """``from_string()`` — the manifest/CLI entry point for a user-supplied version."""

    @pytest.mark.parametrize("value", ["4.2.2", "4.3.0"])
    def test_parses_a_supported_version(self, value):
        assert ArxmlVersion.from_string(value) is ArxmlVersion(value)

    def test_parsing_is_idempotent_for_a_member(self):
        assert ArxmlVersion.from_string(ArxmlVersion.V4_3_0) is ArxmlVersion.V4_3_0

    @pytest.mark.parametrize("value", ["4.5.0", "4.2", "", "4.2.2 ", "R4.2.2", "latest"])
    def test_rejects_an_unsupported_version(self, value):
        with pytest.raises(ValueError) as exc:
            ArxmlVersion.from_string(value)
        message = str(exc.value)
        assert repr(value) in message or value in message
        assert "4.2.2" in message and "4.3.0" in message

    def test_rejects_a_non_string(self):
        with pytest.raises(TypeError):
            ArxmlVersion.from_string(422)  # type: ignore[arg-type]


class TestDependencyDirection:
    """The module is a leaf: ``project.py`` may import it without pulling in the writer."""

    def _imported_modules(self) -> set[str]:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules.add(node.module)
        return modules

    def test_imports_nothing_from_the_writer_package(self):
        offenders = sorted(m for m in self._imported_modules() if m.startswith("swcraft_tools.writer"))
        assert offenders == [], f"arxml_version.py must not import the writer package; found {offenders}"

    def test_imports_nothing_from_swcraft_tools_at_all(self):
        # Stronger than "not the writer": both project.py and the writer depend on
        # the enum, so any SWCraft import here is a potential cycle.
        offenders = sorted(m for m in self._imported_modules() if m.split(".")[0] == "swcraft_tools")
        assert offenders == [], f"arxml_version.py must stay a leaf module; found {offenders}"
