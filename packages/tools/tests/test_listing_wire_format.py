"""The cross-language drift gate on the ``swcraft list --format json`` wire format.

``listing.py``'s :data:`~swcraft_tools.listing.LISTING_SCHEMA_VERSION` and the document
``format_listing_json`` renders are read by ``vscode_extension/src/commandCore.ts``'s
``parseListJson``, a second declaration of the same format with nothing between them. A bumped
version or a renamed ``layers`` / ``elements`` would leave every extension test green while the
QuickPicks silently fell back to a free-text box.

Both directions are checked: every key the document carries is read by the extension or declared
unread (a renamed key fails), and every key declared unread really is unread (a key that becomes
load-bearing must be declared).

**What this module does not reach**: it reads the TypeScript as text, so it witnesses that the
parser *names* each key, not that it uses it correctly, and a key reached through a computed
property or another module is invisible. It says nothing about ``--format text``. It compares two
declarations, not two running programs; ``commandCore.test.ts`` exercises ``parseListJson``.
"""

from __future__ import annotations

import json
import re

from conftest import REPO_ROOT
from swcraft_tools.listing import LISTING_SCHEMA_VERSION, LayerListing, format_listing_json

COMMAND_CORE = REPO_ROOT / "vscode_extension" / "src" / "commandCore.ts"

#: The document keys ``parseListJson`` deliberately does not read, with the reason. A key here is
#: an exemption from the first test and an obligation under the second.
UNREAD_BY_THE_EXTENSION = {"identityField": "the QuickPick shows identities, never the field they came from"}


def _command_core() -> str:
    assert COMMAND_CORE.is_file(), f"{COMMAND_CORE} is missing"
    return COMMAND_CORE.read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """Return *source* with its ``//`` and ``/* … */`` comments removed.

    The doc comments name every key in prose. Crude (a ``//`` inside a string literal takes the
    rest of the line), but it can only remove evidence, so the tests fail closed.
    """
    without_blocks = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", without_blocks)


def _reads_property(source: str, key: str) -> bool:
    """Does *source* read ``.<key>`` as a property? A prose mention in a comment does not count."""
    return re.search(rf"\.{re.escape(key)}\b", _code_only(source)) is not None


def _document_keys() -> set[str]:
    """Every key ``format_listing_json`` emits, at both of its levels, from a real document."""
    document = json.loads(format_listing_json({"swc": LayerListing(identity_field="name", identities=("ExampleSWC",))}))
    keys = set(document)
    for entry in document["layers"].values():
        keys.update(entry)
    return keys


def test_the_extension_declares_the_same_listing_schema_version() -> None:
    """The CLI and the extension declare the same listing schema version, parsed out of the TypeScript."""
    source = _command_core()
    declared = re.findall(r"export const LISTING_SCHEMA_VERSION = (\d+);", source)
    assert declared == [str(LISTING_SCHEMA_VERSION)], (
        f"{COMMAND_CORE.name} declares LISTING_SCHEMA_VERSION {declared}, "
        f"listing.py declares {LISTING_SCHEMA_VERSION} — a document the CLI emits would be refused "
        "by parseListJson and both QuickPicks would silently fall back to a free-text box"
    )


def test_every_key_the_document_carries_is_read_by_the_extension_or_declared_unread() -> None:
    """A renamed key fails here: the extension would stop naming the key it reads."""
    source = _command_core()
    keys = _document_keys()
    assert keys == {"schemaVersion", "layers", "identityField", "elements"}, sorted(keys)
    unread = set(UNREAD_BY_THE_EXTENSION)
    assert unread <= keys, f"declared unread but not emitted at all: {sorted(unread - keys)}"
    missing = sorted(key for key in keys - unread if not _reads_property(source, key))
    assert missing == [], (
        f"{COMMAND_CORE.name} reads none of {missing}, which `swcraft list --format json` emits — "
        "either the extension was not updated with the CLI, or the key belongs in "
        "UNREAD_BY_THE_EXTENSION with a reason"
    )


def test_every_key_declared_unread_really_is_unread() -> None:
    """An exemption may not outlive its reason: a key declared unread is not read."""
    source = _command_core()
    read_after_all = sorted(key for key in UNREAD_BY_THE_EXTENSION if _reads_property(source, key))
    assert read_after_all == [], (
        f"{COMMAND_CORE.name} now reads {read_after_all}, declared here as unread "
        f"({[UNREAD_BY_THE_EXTENSION[key] for key in read_after_all]}) — remove the exemption so the "
        "rename direction is guarded for it too"
    )


def test_the_python_document_is_what_the_extension_shape_expects() -> None:
    """Each layer entry is an object with an element array, the shape ``parseListJson`` requires."""
    document = json.loads(
        format_listing_json(
            {
                "swc": LayerListing(identity_field="name", identities=("A", "B")),
                "requirements": LayerListing(identity_field="id", identities=()),
            }
        )
    )
    assert document["schemaVersion"] == LISTING_SCHEMA_VERSION
    assert list(document["layers"]) == ["swc", "requirements"], "the layer order is the document's own"
    assert document["layers"]["swc"]["elements"] == ["A", "B"]
    assert document["layers"]["requirements"]["elements"] == [], "an empty layer keeps its key"
