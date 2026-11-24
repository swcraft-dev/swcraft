"""The server's manifest sentences as pure functions: one case per branch.

These pin every branch of :func:`server.broken_manifest_sentence`, :func:`server._refusal_kind` and
:func:`server._manifest_as_named` on every operating system, the unreadable manifest included.
What they do not reach: that the loader raises these exceptions for a real file (the process case in
``test_client_stanzas.py`` and the typed refusals' case below do).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from swcraft_mcp import server
from swcraft_tools.project import ManifestRootWrittenAbsoluteError, ManifestShapeError, require_project

#: How every sentence opens and ends for a manifest given as ``swcraft.yaml``.
OPENS = "The SWCraft project cannot be opened: swcraft.yaml "
CORRECT = "; correct the manifest, then call the tool again."

#: A value no sentence may repeat.
SECRET = "/Users/someone/secret-model"


def _sentence(exc: BaseException, given: str = "swcraft.yaml") -> str:
    sentence = server.broken_manifest_sentence(given, exc)
    assert SECRET not in sentence
    return sentence


class TestBrokenManifestSentence:
    def test_text_that_is_not_utf8(self) -> None:
        exc = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        assert _sentence(exc) == f"{OPENS}is not UTF-8 text{CORRECT}"

    def test_a_document_that_is_no_mapping(self) -> None:
        assert _sentence(ManifestShapeError("list")) == f"{OPENS}is not a mapping of manifest fields{CORRECT}"

    def test_an_empty_document(self) -> None:
        assert _sentence(ManifestShapeError("NoneType")) == f"{OPENS}is empty{CORRECT}"

    @pytest.mark.parametrize("field", ["dist_root", "views_root"])
    def test_a_root_written_absolute_names_the_field_alone(self, field: str) -> None:
        assert _sentence(ManifestRootWrittenAbsoluteError(field, SECRET)) == (
            f"{OPENS}writes field `{field}` as an absolute path, which must be relative to the project folder{CORRECT}"
        )

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError(SECRET),
            # A plain ValueError is not read by its text: only the type counts.
            ValueError(f"Manifest field 'dist_root' is written as the absolute path '{SECRET}'."),
            ValueError("Invalid swcraft.yaml: expected a mapping, got list"),
        ],
        ids=["runtime", "value-error-with-the-root-text", "value-error-with-the-shape-text"],
    )
    def test_any_other_exception_is_named_by_its_class(self, exc: BaseException) -> None:
        assert _sentence(exc) == (
            f"{OPENS}cannot be used ({type(exc).__name__}; the server's log has the detail){CORRECT}"
        )

    def test_a_validation_error_with_an_empty_location(self) -> None:
        exc = ValidationError.from_exception_data(
            "SwcraftProject",
            [{"type": "value_error", "loc": (), "input": SECRET, "ctx": {"error": ValueError(SECRET)}}],
        )
        assert _sentence(exc) == (
            f"The SWCraft project cannot be opened: in swcraft.yaml, the document is invalid (value_error){CORRECT}"
        )

    def test_an_unreadable_manifest_on_every_system(self) -> None:
        exc = PermissionError(13, "Permission denied", f"{SECRET}/swcraft.yaml")
        assert _sentence(exc) == (
            f"{OPENS}cannot be read (Permission denied); make it readable to the server, then call the tool again."
        )

    def test_an_os_error_without_a_reason_is_named_by_its_class(self) -> None:
        assert _sentence(PermissionError()) == (
            f"{OPENS}cannot be read (PermissionError); make it readable to the server, then call the tool again."
        )


class TestManifestAsNamed:
    @pytest.mark.parametrize(
        ("given", "named"),
        [
            ("swcraft.yaml", "swcraft.yaml"),
            ("./swcraft.yaml", "./swcraft.yaml"),
            ("sub/swcraft.yaml", "sub/swcraft.yaml"),
            ("~/swcraft.yaml", "swcraft.yaml"),
            ("~/projects/p/swcraft.yaml", "swcraft.yaml"),
            ("C:swcraft.yaml", "swcraft.yaml"),
            ("C:x\\swcraft.yaml", "swcraft.yaml"),
            ("C:\\Users\\someone\\p\\swcraft.yaml", "swcraft.yaml"),
            ("\\\\host\\share\\swcraft.yaml", "swcraft.yaml"),
            ("/Users/someone/p/swcraft.yaml", "swcraft.yaml"),
        ],
        ids=[
            "bare",
            "dot-relative",
            "relative",
            "tilde",
            "tilde-deeper",
            "drive-relative",
            "drive-relative-folder",
            "windows-absolute",
            "unc",
            "posix-absolute",
        ],
    )
    def test_a_located_manifest_is_named_by_its_file_name(self, given: str, named: str) -> None:
        """Read by both platforms' rules wherever the server runs: a Windows path is located on POSIX too."""
        assert server._manifest_as_named(given) == named

    def test_a_located_manifest_missing_names_the_file_alone(self) -> None:
        assert server.missing_manifest_sentence("C:\\Users\\someone\\swcraft.yaml") == (
            "No SWCraft project: the manifest given to the server (swcraft.yaml) was not found; "
            "correct its path in the client's server configuration."
        )


class TestTheLoaderRaisesTheTypedRefusals:
    """The typed refusals are the loader's, ``ValueError`` still, with the loader's sentence unchanged."""

    def test_a_root_written_absolute(self, tmp_path: Path) -> None:
        written = (tmp_path.parent / "elsewhere").as_posix()
        manifest = tmp_path / "swcraft.yaml"
        manifest.write_text(f"name: p\ndist_root: {written}\n", encoding="utf-8", newline="\n")
        with pytest.raises(ManifestRootWrittenAbsoluteError) as raised:
            require_project(str(manifest))
        assert isinstance(raised.value, ValueError)
        assert raised.value.field == "dist_root"
        assert str(raised.value) == (
            f"Manifest field 'dist_root' is written as the absolute path '{written}'. The two roots "
            "`swcraft clean-outputs` removes must be written relative to the project directory, so "
            "that the manifest means the same thing on every machine that reads it."
        )

    @pytest.mark.parametrize(("text", "got"), [("", "NoneType"), ("- a\n", "list")], ids=["empty", "list"])
    def test_a_document_that_is_no_mapping(self, tmp_path: Path, text: str, got: str) -> None:
        manifest = tmp_path / "swcraft.yaml"
        manifest.write_text(text, encoding="utf-8", newline="\n")
        with pytest.raises(ManifestShapeError) as raised:
            require_project(str(manifest))
        assert isinstance(raised.value, ValueError)
        assert raised.value.got == got
        assert str(raised.value) == f"Invalid swcraft.yaml: expected a mapping, got {got}"
