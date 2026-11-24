"""Rewrite the ``CATEGORY`` line of every enum type in an ARXML document's text.

Shared by ``test_enum_category_refreeze.py`` and ``test_component_import.py``. Both edit a
document the writer generated, so the substitution is on the text -- never an XML
re-serialization, so every other byte stays the writer's own.

An *enum type* here is an ``APPLICATION-PRIMITIVE-DATA-TYPE`` whose ``COMPU-METHOD-REF`` names a
``COMPU-METHOD`` of ``CATEGORY`` ``TEXTTABLE``; the category it carries, and why, is
``writer.arxml_generation._emit_enum_type``'s to state. A line is rewritten only when it is the
``CATEGORY`` line directly following the ``SHORT-NAME`` line of the type's opening tag -- the
layout the writer emits; a hand-laid document with another layout substitutes nothing, which the
callers' count assertions make visible.
"""

from __future__ import annotations

import re

from lxml import etree
from swcraft_tools.writer.arxml_xml import AR_NAMESPACE

_NS = {"ar": AR_NAMESPACE}
_TYPE_OPEN = re.compile(rb"^\s*<APPLICATION-PRIMITIVE-DATA-TYPE(?:\s[^>]*)?>\s*$")
_SHORT_NAME = re.compile(rb"^\s*<SHORT-NAME>([^<]+)</SHORT-NAME>\s*$")


def enum_type_names(document: bytes) -> frozenset[str]:
    """The application primitives whose ``COMPU-METHOD-REF`` names a ``TEXTTABLE`` compu method.

    Read with a parser only to *find* the names; the substitution itself is on the text.
    """
    root = etree.fromstring(document)
    texttable = set()
    for package in root.iter(f"{{{AR_NAMESPACE}}}AR-PACKAGE"):
        path = "/" + "/".join(
            p.findtext("ar:SHORT-NAME", namespaces=_NS)
            for p in [*reversed(list(package.iterancestors(f"{{{AR_NAMESPACE}}}AR-PACKAGE"))), package]
        )
        for method in package.findall("ar:ELEMENTS/ar:COMPU-METHOD", _NS):
            if method.findtext("ar:CATEGORY", namespaces=_NS) == "TEXTTABLE":
                texttable.add(f"{path}/{method.findtext('ar:SHORT-NAME', namespaces=_NS)}")
    return frozenset(
        node.findtext("ar:SHORT-NAME", namespaces=_NS)
        for node in root.iter(f"{{{AR_NAMESPACE}}}APPLICATION-PRIMITIVE-DATA-TYPE")
        if (ref := node.find(".//ar:COMPU-METHOD-REF", _NS)) is not None and ref.text in texttable
    )


def rewrite_enum_type_category(document: bytes, old: str, new: str) -> tuple[bytes, int]:
    """Rewrite each enum type's ``<CATEGORY>{old}</CATEGORY>`` line to ``{new}``.

    Returns the rewritten bytes and the number of lines substituted.
    """
    enums = enum_type_names(document)
    old_element = f"<CATEGORY>{old}</CATEGORY>".encode()
    new_element = f"<CATEGORY>{new}</CATEGORY>".encode()
    category_line = re.compile(rb"^\s*" + re.escape(old_element) + rb"\s*$")
    lines = document.splitlines(keepends=True)
    substituted = 0
    for index in range(2, len(lines)):
        name = _SHORT_NAME.match(lines[index - 1])
        if (
            name
            and category_line.match(lines[index])
            and _TYPE_OPEN.match(lines[index - 2])
            and name.group(1).decode("utf-8") in enums
        ):
            lines[index] = lines[index].replace(old_element, new_element)
            substituted += 1
    return b"".join(lines), substituted
