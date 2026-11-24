"""The enum category change is gated, not trusted: every moved byte is one category line.

The writer emits an enumeration as an ``APPLICATION-PRIMITIVE-DATA-TYPE`` of ``CATEGORY``
``VALUE``, keeping ``TEXTTABLE`` on its ``COMPU-METHOD`` (the rationale is
``writer.arxml_generation._emit_enum_type``'s); a ``TEXTTABLE`` category on the type is the form it does not write.

**The check.** Each moved document is mapped back: the ``<CATEGORY>`` line of every
``APPLICATION-PRIMITIVE-DATA-TYPE`` whose ``COMPU-METHOD-REF`` names a ``TEXTTABLE`` method
is rewritten from ``VALUE`` to ``TEXTTABLE`` (``enum_category_support``), a line
substitution and never an XML re-serialization, and the result must hash to the digest the
document had before the change. The number of substituted lines must equal the enums the
model's YAML declares. Together: one line per enum moved, and nothing else.

The termination-handling lines are first stripped by
``termination_strip_support.strip_recorded`` (``test_termination_refreeze.py`` gates it),
so both changes are undone in turn.

**What it does not reach.** The five recorded documents (the golden and the two
enum-declaring trees at both versions) and no other model. It says which bytes moved,
never that ``VALUE`` is right, which is the standard's text. And the pre-change digests
are the old writer's bytes, themselves wrong in this one value.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from enum_category_support import rewrite_enum_type_category
from swcraft_tools.reader import read_layer
from swcraft_tools.schema import EnumDataType
from swcraft_tools.writer import generate_arxml
from swcraft_tools.writer.arxml_xml import AR_NAMESPACE
from termination_strip_support import strip_recorded

_COMPONENT_EXCHANGE = "packages/tools/tests/fixtures/component_exchange/model"
_INIT_VALUES = "packages/tools/tests/fixtures/init_values/model"

#: The SHA-256 each generated document had before the change, keyed by ``(tree, version)``.
PRE_CHANGE: dict[tuple[str, str], str] = {
    (_COMPONENT_EXCHANGE, "4.2.2"): "02db8ba587ccf35980551d39b696fa1156bf0b77a3ebef08d39cfc7e04cc3805",
    (_COMPONENT_EXCHANGE, "4.3.0"): "0383a093302528d4da1b6235ff8aa1dcf6b960611ef3abc4c36a838c4cba9b8b",
    (_INIT_VALUES, "4.2.2"): "ea2d8d517a167315a99b776a4b294a53220bc48a309c62545367c65b8cbed69d",
    (_INIT_VALUES, "4.3.0"): "48faf5dd2b05e70c3b4931c716d0eec1583d86b05e595e143fc26595f10d9846",
}

#: The committed golden is the ``component_exchange`` model's 4.2.2 document
#: (``conftest.GOLDEN_MODEL_ROOTS``' ``composite-4.2.2``), so its pre-change digest is that entry's.
GOLDEN = REPO_ROOT / "packages/tools/tests/fixtures/arxml_golden/composite-4.2.2.arxml"
GOLDEN_MODEL = REPO_ROOT / _COMPONENT_EXCHANGE
GOLDEN_PRE_CHANGE = PRE_CHANGE[(_COMPONENT_EXCHANGE, "4.2.2")]


def reverse_map(document: bytes) -> tuple[bytes, int]:
    """Rewrite each enum type's ``VALUE`` category line back to ``TEXTTABLE``."""
    return rewrite_enum_type_category(document, "VALUE", "TEXTTABLE")


def undo_both(tree: str, version: str, document: bytes) -> tuple[bytes, int]:
    """Undo the termination handling re-freeze, then the enum category change, in that order."""
    return reverse_map(strip_recorded(tree, f"arxml-{version}", document))


def _enum_count(model_root: Path) -> int:
    """The enums the model declares, counted from its ``application_types`` YAML."""
    return sum(isinstance(dt, EnumDataType) for dt in read_layer(str(model_root), "application_types"))


def _generated(tree: str, version: str, tmp_path: Path) -> bytes:
    output = tmp_path / f"model-{version}.arxml"
    generate_arxml(str(REPO_ROOT / tree), str(output), version=version)
    return output.read_bytes()


def test_the_golden_reverse_maps_to_its_pre_change_digest() -> None:
    mapped, substituted = undo_both(_COMPONENT_EXCHANGE, "4.2.2", GOLDEN.read_bytes())
    assert substituted == _enum_count(GOLDEN_MODEL) == 1
    assert hashlib.sha256(mapped).hexdigest() == GOLDEN_PRE_CHANGE


@pytest.mark.parametrize(("tree", "version"), list(PRE_CHANGE))
def test_a_generated_document_reverse_maps_to_its_pre_change_digest(tree: str, version: str, tmp_path: Path) -> None:
    mapped, substituted = undo_both(tree, version, _generated(tree, version, tmp_path))
    assert substituted == _enum_count(REPO_ROOT / tree) == 1
    assert hashlib.sha256(mapped).hexdigest() == PRE_CHANGE[(tree, version)]


def test_the_reverse_map_touches_no_physical_type_and_no_compu_method() -> None:
    """Planted: only an enum type's line moves; a ``VALUE`` physical type and a method stay.

    ``Speed_T`` references a ``LINEAR`` compu method, so a regression that took any primitive
    with a ``COMPU-METHOD-REF`` for an enum would substitute two lines, not one.
    """
    ns = AR_NAMESPACE
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<AUTOSAR xmlns="{ns}">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>DataTypes</SHORT-NAME>
      <ELEMENTS>
        <COMPU-METHOD>
          <SHORT-NAME>Gear_T_CompuMethod</SHORT-NAME>
          <CATEGORY>TEXTTABLE</CATEGORY>
        </COMPU-METHOD>
        <APPLICATION-PRIMITIVE-DATA-TYPE>
          <SHORT-NAME>Gear_T</SHORT-NAME>
          <CATEGORY>VALUE</CATEGORY>
          <SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>
            <COMPU-METHOD-REF DEST="COMPU-METHOD">/DataTypes/Gear_T_CompuMethod</COMPU-METHOD-REF>
          </SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>
        </APPLICATION-PRIMITIVE-DATA-TYPE>
        <COMPU-METHOD>
          <SHORT-NAME>Speed_T_CompuMethod</SHORT-NAME>
          <CATEGORY>LINEAR</CATEGORY>
        </COMPU-METHOD>
        <APPLICATION-PRIMITIVE-DATA-TYPE>
          <SHORT-NAME>Speed_T</SHORT-NAME>
          <CATEGORY>VALUE</CATEGORY>
          <SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>
            <COMPU-METHOD-REF DEST="COMPU-METHOD">/DataTypes/Speed_T_CompuMethod</COMPU-METHOD-REF>
          </SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>
        </APPLICATION-PRIMITIVE-DATA-TYPE>
      </ELEMENTS>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
""".encode()
    mapped, substituted = reverse_map(document)
    assert substituted == 1
    assert mapped.count(b"<CATEGORY>TEXTTABLE</CATEGORY>") == 2
    assert mapped.count(b"<CATEGORY>VALUE</CATEGORY>") == 1
    assert mapped.count(b"<CATEGORY>LINEAR</CATEGORY>") == 1
    assert len(mapped) == len(document) + len(b"TEXTTABLE") - len(b"VALUE")
