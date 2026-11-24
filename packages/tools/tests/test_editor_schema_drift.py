"""The drift gate for the committed editor artifacts under ``vscode_extension/schemas/``.

The directory holds the rendered output of
:func:`swcraft_tools.editor_schema.editor_artifacts`, committed so the ``.vsix`` ships it
without the Node build resolving a ``.venv``. Both directions are checked: every
generated artifact is on disk byte-identically, and every file on disk is one the
generator produces (no orphan of a renamed or removed layer). There is no ``--check``
mode on the generator: this module is the comparison.

What this module does not reach: whether the bytes are *right* (``test_editor_schema.py``'s
rendering rules and corpus oracle), and ``package.json``'s association block
(``test_editor_schema_manifest.py``).
"""

from __future__ import annotations

from pathlib import Path

from conftest import REPO_ROOT
from swcraft_tools import __version__
from swcraft_tools.editor_schema import editor_artifacts

SCHEMAS_DIR = REPO_ROOT / "vscode_extension" / "schemas"

REGENERATE = "regenerate with `python scripts/dev.py schemas` and commit the result"


def _committed_files() -> dict[str, Path]:
    """Every regular file under the schemas directory, keyed by its path relative to the directory."""
    assert SCHEMAS_DIR.is_dir(), f"{SCHEMAS_DIR} is missing — {REGENERATE}"
    return {
        path.relative_to(SCHEMAS_DIR).as_posix(): path
        for path in sorted(SCHEMAS_DIR.rglob("*"), key=lambda path: path.as_posix())
        if path.is_file()
    }


def test_every_generated_artifact_is_committed_byte_identically() -> None:
    """Generator ahead of the tree: a missing or differing file is drift, and the message names the fix."""
    committed = _committed_files()
    artifacts = editor_artifacts()
    missing = [relative for relative in artifacts if relative not in committed]
    assert not missing, f"not committed under {SCHEMAS_DIR}: {missing} — {REGENERATE}"
    stale = [
        relative for relative, text in artifacts.items() if committed[relative].read_bytes() != text.encode("utf-8")
    ]
    assert not stale, f"stale under {SCHEMAS_DIR} (the generator renders different bytes): {stale} — {REGENERATE}"


def test_every_committed_file_is_one_the_generator_produces() -> None:
    """Tree ahead of the generator: an orphan left behind by a renamed or removed layer is drift too."""
    orphans = sorted(set(_committed_files()) - set(editor_artifacts()))
    assert not orphans, (
        f"files under {SCHEMAS_DIR} that no artifact produces: {orphans} — delete them, then {REGENERATE}"
    )


def test_no_committed_artifact_carries_the_product_version() -> None:
    """The editor artifacts are not a lockstep site: the version string appears in none of them."""
    carriers = [
        relative for relative, path in _committed_files().items() if __version__ in path.read_text(encoding="utf-8")
    ]
    assert not carriers, f"artifacts embedding the product version {__version__!r}: {carriers}"
