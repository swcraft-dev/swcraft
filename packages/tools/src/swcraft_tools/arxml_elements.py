"""The elements of a serialized ARXML document, and two documents' elements compared.

:func:`arxml_elements` keys every element under an ``ELEMENTS`` by its path ``Package/…/SHORT-NAME``, with its
tag and its canonical (C14N) bytes, blank text removed; :func:`compare_arxml_elements` compares two documents'
walks. A reader takes the root :func:`parse_arxml` gave, so a document read twice is parsed once. A text that
is no XML document raises ``etree.XMLSyntaxError``; nothing here catches it.

**A leaf module**: it imports ``lxml`` alone, nothing of ``swcraft_tools``, so ``validate`` and the change
report can share the walk without ``validate`` loading a report module.

**What it does not reach.** AUTOSAR semantics: a reordered reference list or any other reordered child is a
change, since canonical bytes keep the order. Elements outside an ``ELEMENTS``: the ``AR-PACKAGES`` tree itself
(a package renamed with nothing in it, an ``ADMIN-DATA``) moves no row. Two elements at the same path in one
document: the later one is the one compared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lxml import etree

__all__ = [
    "ArxmlElementChange",
    "arxml_elements",
    "compare_arxml_elements",
    "declared_schema_location",
    "parse_arxml",
]

#: Spelled here so this leaf imports no validation module.
_XSI_NAMESPACE = "http://www.w3.org/2001/XMLSchema-instance"
_SCHEMA_LOCATION_ATTRIBUTE = f"{{{_XSI_NAMESPACE}}}schemaLocation"


@dataclass(frozen=True)
class ArxmlElementChange:
    """One ARXML element the change moves: ``Package/SHORT-NAME``, its tag and ``added``/``removed``/``changed``."""

    path: str
    type: str
    change: str


def parse_arxml(text: str | bytes) -> Any:
    """*text*'s root, blank text removed (the parse every reader here compares canonical bytes over)."""
    data = text.encode("utf-8") if isinstance(text, str) else text
    return etree.fromstring(data, etree.XMLParser(remove_blank_text=True))


def _localname(element: Any) -> str:
    return etree.QName(element).localname


def _short_name(element: Any) -> str:
    for child in element:
        if isinstance(child.tag, str) and _localname(child) == "SHORT-NAME":
            return child.text or ""
    return ""


def arxml_elements(root: Any) -> dict[str, tuple[str, bytes]]:
    """``Package/…/SHORT-NAME → (tag, canonical bytes)`` for every element under an ``ELEMENTS``, in document order."""
    found: dict[str, tuple[str, bytes]] = {}

    def walk(package: Any, prefix: str) -> None:
        path = f"{prefix}{_short_name(package)}"
        for child in package:
            if not isinstance(child.tag, str):
                continue
            if _localname(child) == "ELEMENTS":
                for element in child:
                    if isinstance(element.tag, str):
                        found[f"{path}/{_short_name(element)}"] = (
                            _localname(element),
                            etree.tostring(element, method="c14n"),
                        )
            elif _localname(child) == "AR-PACKAGES":
                for sub in child:
                    if isinstance(sub.tag, str):
                        walk(sub, f"{path}/")

    for top in root:
        if isinstance(top.tag, str) and _localname(top) == "AR-PACKAGES":
            for package in top:
                if isinstance(package.tag, str):
                    walk(package, "")
    return found


def compare_arxml_elements(before: Any, after: Any) -> tuple[ArxmlElementChange, ...]:
    """The elements that differ: added and changed in *after*'s order, then removed in *before*'s."""
    before_elements, after_elements = arxml_elements(before), arxml_elements(after)
    changes: list[ArxmlElementChange] = []
    for path, (tag, canonical) in after_elements.items():
        previous = before_elements.get(path)
        if previous is None:
            changes.append(ArxmlElementChange(path, tag, "added"))
        elif previous != (tag, canonical):
            changes.append(ArxmlElementChange(path, tag, "changed"))
    changes.extend(
        ArxmlElementChange(path, tag, "removed")
        for path, (tag, _) in before_elements.items()
        if path not in after_elements
    )
    return tuple(changes)


def declared_schema_location(root: Any) -> str | None:
    """The root's ``xsi:schemaLocation`` as written, or ``None`` when the root declares none."""
    return root.get(_SCHEMA_LOCATION_ATTRIBUTE)
