"""Tests for ``scripts/download_autosar_schema.py``; its stdlib copy of the filename table is held to the enum."""

from __future__ import annotations

from pathlib import Path

from conftest import download_autosar_schema_module
from swcraft_tools.arxml_version import SCHEMA_FILENAMES, ArxmlVersion


class TestSchemaProvisioningScript:
    def test_its_filename_table_matches_the_enum(self):
        """The script cannot import ``swcraft_tools``; this test keeps its copy of the table in step."""
        script = download_autosar_schema_module()
        assert script.SCHEMA_FILENAMES == {version.value: filename for version, filename in SCHEMA_FILENAMES.items()}

    def test_it_defaults_to_4_2_2(self):
        script = download_autosar_schema_module()
        assert script.DEFAULT_VERSION == ArxmlVersion.V4_2_2.value
        # A single hardcoded 4.2.2 path is what `schema_path(version)` replaced.
        assert not hasattr(script, "SCHEMA_PATH")
        assert not hasattr(script, "SCHEMA_FILENAME")
        assert script.schema_path(script.DEFAULT_VERSION).name == SCHEMA_FILENAMES[ArxmlVersion.V4_2_2]

    def test_no_arguments_reports_the_4_2_2_path(self, monkeypatch, capsys):
        script = download_autosar_schema_module()
        monkeypatch.delenv(script.URL_ENV_VAR, raising=False)
        monkeypatch.delenv(script.url_env_var("4.2.2"), raising=False)
        monkeypatch.setattr(script, "SCHEMA_DIR", Path("/nonexistent/schemas"))

        assert script.main([]) == 0
        assert SCHEMA_FILENAMES[ArxmlVersion.V4_2_2] in capsys.readouterr().out

    def test_a_named_version_reports_that_versions_path(self, monkeypatch, capsys):
        script = download_autosar_schema_module()
        monkeypatch.delenv(script.URL_ENV_VAR, raising=False)
        monkeypatch.delenv(script.url_env_var("4.3.0"), raising=False)
        monkeypatch.setattr(script, "SCHEMA_DIR", Path("/nonexistent/schemas"))

        assert script.main(["4.3.0"]) == 0
        out = capsys.readouterr().out
        assert SCHEMA_FILENAMES[ArxmlVersion.V4_3_0] in out
        assert script.url_env_var("4.3.0") in out

    def test_an_unsupported_version_is_a_usage_error(self, capsys):
        script = download_autosar_schema_module()
        assert script.main(["4.1.1"]) == 2
        assert "4.1.1" in capsys.readouterr().out

    def test_the_unversioned_url_is_not_reused_for_another_version(self, monkeypatch):
        """Reusing it would save a 4.2.2 schema under the 4.3.0 filename."""
        script = download_autosar_schema_module()
        monkeypatch.setenv(script.URL_ENV_VAR, "https://example.invalid/AUTOSAR_4-2-2.xsd")
        monkeypatch.delenv(script.url_env_var("4.3.0"), raising=False)

        assert script._configured_url("4.3.0") is None
        assert script._configured_url("4.2.2") == (
            "https://example.invalid/AUTOSAR_4-2-2.xsd",
            script.URL_ENV_VAR,
        )

    def test_a_version_specific_url_outranks_the_unversioned_one(self, monkeypatch):
        script = download_autosar_schema_module()
        monkeypatch.setenv(script.URL_ENV_VAR, "https://example.invalid/generic.xsd")
        monkeypatch.setenv(script.url_env_var("4.2.2"), "https://example.invalid/specific.xsd")

        assert script._configured_url("4.2.2") == (
            "https://example.invalid/specific.xsd",
            script.url_env_var("4.2.2"),
        )
