"""An independent oracle for the references of a generated ARXML document.

Package placement moves an element's path, and every reference to it must move with it. This module reads the
writer's **output** and shares no code with it: it parses the file with ``lxml``, builds the document's own path
index from its ``SHORT-NAME`` s, and resolves every reference against it.

**What it asserts.** Every element carrying a ``DEST`` attribute holds an absolute path naming an element **inside
the document**, and the ``DEST`` is that element's own tag. The exceptions are the paths the caller names: an
external stub's ``external_ref`` (and a stub port's path below it), and **exact** paths the model holds verbatim,
exempt only as that string; :func:`verbatim_references` returns the references it exempted.

**How the index is built**, by GST ``[TPS_GST_00018]`` (a ``SHORT-NAME`` is unique within its nearest
``Identifiable`` ancestor): an element with a ``SHORT-NAME`` child extends its nearest named ancestor's path; one
without is transparent. No list of named tags is kept.

**What it does not reach.** That a reference names the element the model *meant*; a reference into another file
that the caller does not call external (a failure); and anything but ``DEST`` references (an ``*-IREF`` is covered
through the references it wraps).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lxml import etree

#: The attribute every AUTOSAR reference carries: the tag of the element it names.
_DEST = "DEST"

_SHORT_NAME = "SHORT-NAME"


@dataclass(frozen=True)
class Reference:
    """One reference as the file spells it: its tag, its path text and its ``DEST``."""

    tag: str
    path: str
    dest: str


def _local(element: etree._Element) -> str:
    return etree.QName(element).localname


def _short_name(element: etree._Element) -> str | None:
    for child in element:
        if isinstance(child.tag, str) and _local(child) == _SHORT_NAME:
            return child.text or ""
    return None


def _parse(document: str | bytes | Path) -> etree._Element:
    if isinstance(document, Path):
        return etree.parse(str(document)).getroot()
    data = document.encode("utf-8") if isinstance(document, str) else document
    return etree.fromstring(data)


def path_index(document: str | bytes | Path) -> dict[str, str]:
    """Every absolute path the document declares, mapped to the tag of the element at it.

    Raises:
        AssertionError: when two elements of the document share one path — a
            ``SHORT-NAME`` repeated in one name space, which makes a reference to it
            ambiguous.
    """
    index: dict[str, str] = {}

    def walk(element: etree._Element, prefix: str) -> None:
        for child in element:
            if not isinstance(child.tag, str):
                continue
            name = _short_name(child)
            if name is None:
                walk(child, prefix)
                continue
            path = f"{prefix}/{name}"
            assert path not in index, f"two elements share the path {path}: {index[path]} and {_local(child)}"
            index[path] = _local(child)
            walk(child, path)

    walk(_parse(document), "")
    return index


def references(document: str | bytes | Path) -> list[Reference]:
    """Every element of the document carrying a ``DEST``, in document order."""
    return [
        Reference(_local(element), (element.text or "").strip(), element.get(_DEST))
        for element in _parse(document).iter()
        if isinstance(element.tag, str) and element.get(_DEST) is not None
    ]


def _is_external(path: str, external: frozenset[str]) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in external)


def verbatim_references(document: str | bytes | Path, verbatim: frozenset[str]) -> list[Reference]:
    """The references of *document* whose path is exactly one of *verbatim*, in document order."""
    return [reference for reference in references(document) if reference.path in verbatim]


def reference_problems(
    document: str | bytes | Path,
    *,
    external: frozenset[str] = frozenset(),
    verbatim: frozenset[str] = frozenset(),
) -> list[str]:
    """Every reference of *document* that does not resolve inside it, or whose ``DEST`` disagrees.

    Args:
        document: the ARXML file, or its text.
        external: the ``external_ref`` of every stub the model declares. A reference
            equal to one, or below one, is the stub's and is not resolved.
        verbatim: exact paths the model holds verbatim and the writer writes as they
            are (a disabled mode's two paths). A reference equal to one is not
            resolved; one below it still is.

    Returns:
        One sentence per failing reference, empty when every reference resolves.
    """
    index = path_index(document)
    problems: list[str] = []
    for reference in references(document):
        if reference.path in verbatim or _is_external(reference.path, external):
            continue
        target = index.get(reference.path)
        if target is None:
            problems.append(f"{reference.tag} {reference.path} names no element of the document")
        elif target != reference.dest:
            problems.append(f"{reference.tag} {reference.path} has DEST {reference.dest}, the element is {target}")
    return problems
