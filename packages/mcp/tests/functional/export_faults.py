"""Export faults: a second, conflicting declaration of a sender-receiver interface in an export.

For each named interface, :func:`add_conflicts` appends a package ``WlConflict<k>`` holding a copy
under the same SHORT-NAME with one change:

- ``value`` -- the first data element's ``TYPE-TREF`` names another type the export references
  (the first in ``(DEST, path)`` order). The import takes neither declaration and keeps each
  referrer's path for ``check-model`` to report.
- ``description`` -- the copy's ``DESC`` differs (changed, or added right after the SHORT-NAME, where
  the standard orders it). A description differing alone is merged on import.

The rest of the export stays byte for byte; the source file is never written. Interfaces only: an
application type's referrers span more than one layer.

Does not reach an export whose layout is not the writer's (the interface is found by text, not by
an ARXML parser), nor any other kind of export defect.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path

#: The fault kinds, each the one property the second declaration changes.
FAULT_KINDS = ("value", "description")

#: The package each conflicting copy is declared in; ``k`` is its interface's place in the list.
CONFLICT_PACKAGE = "WlConflict{k}"

#: The text a ``description`` fault gives a copy that declares none.
ADDED_DESCRIPTION = "A second declaration."

_TYPE_TREF = re.compile(r'<TYPE-TREF DEST="(?P<dest>[^"]+)">(?P<path>[^<]+)</TYPE-TREF>')
_DESC_TEXT = re.compile(r"(<DESC>\s*<L-2 L=\"[^\"]*\">)(?P<text>[^<]*)(</L-2>)")
_SPACE = re.compile(r"\s*")
#: What may stand between an element's SHORT-NAME and its DESC (the standard's order).
_BEFORE_DESC = re.compile(
    r"(?:\s*<SHORT-NAME-FRAGMENTS>.*?</SHORT-NAME-FRAGMENTS>)?(?:\s*<LONG-NAME>.*?</LONG-NAME>)?", re.S
)


def repeated(items: Iterable[str]) -> list[str]:
    """Every item that *items* names more than once, sorted."""
    return sorted(item for item, count in Counter(items).items() if count > 1)


class FaultError(ValueError):
    """A fault that cannot be made on this export: which, and why."""


def _interface_block(arxml: str, name: str) -> re.Match[str]:
    pattern = re.compile(
        r"^(?P<indent>[ \t]*)<SENDER-RECEIVER-INTERFACE>\s*<SHORT-NAME>"
        + re.escape(name)
        + r"</SHORT-NAME>.*?</SENDER-RECEIVER-INTERFACE>[ \t]*\n",
        re.M | re.S,
    )
    found = list(pattern.finditer(arxml))
    if not found:
        raise FaultError(f"no sender-receiver interface is named {name!r}")
    if len(found) > 1:
        raise FaultError(f"the export already declares the sender-receiver interface {name!r} {len(found)} times")
    return found[0]


def _value_fault(block: str, references: list[tuple[str, str]], name: str) -> str:
    first = _TYPE_TREF.search(block)
    if first is None:
        raise FaultError(f"the sender-receiver interface {name!r} has no data element with a type")
    other = next(((dest, path) for dest, path in references if path != first["path"]), None)
    if other is None:
        raise FaultError(f"the export references no type but {first['path']!r}, so {name!r} cannot name another")
    dest, path = other
    return block[: first.start()] + f'<TYPE-TREF DEST="{dest}">{path}</TYPE-TREF>' + block[first.end() :]


def _description_fault(block: str, indent: str) -> str:
    short_name_end = block.index("</SHORT-NAME>") + len("</SHORT-NAME>")
    after = _BEFORE_DESC.match(block, short_name_end).end()
    existing = _DESC_TEXT.match(block, _SPACE.match(block, after).end())
    if existing is not None:
        return block[: existing.start("text")] + existing["text"] + " (differs)" + block[existing.end("text") :]
    child = indent + "  "
    added = f'\n{child}<DESC>\n{child}  <L-2 L="EN">{ADDED_DESCRIPTION}</L-2>\n{child}</DESC>'
    return block[:after] + added + block[after:]


def add_conflicts(arxml: str, interfaces: Sequence[str], kind: str) -> str:
    """*arxml* with one conflicting copy of each of *interfaces*, in a package of its own, appended.

    The copies come after the last top-level package, in the order *interfaces* names them; nothing
    before them changes. An empty *interfaces* answers *arxml* as it is.
    """
    if kind not in FAULT_KINDS:
        raise FaultError(f"no fault kind {kind!r}; the kinds are {', '.join(FAULT_KINDS)}")
    twice = repeated(interfaces)
    if twice:
        raise FaultError(f"{twice[0]!r} is named twice: one conflicting copy per interface")
    if not interfaces:
        return arxml
    references = sorted({(match["dest"], match["path"]) for match in _TYPE_TREF.finditer(arxml)})
    closing = arxml.rindex("</AR-PACKAGES>")
    line_start = arxml.rindex("\n", 0, closing) + 1
    indent = arxml[line_start:closing] + "  "
    packages = []
    for k, name in enumerate(interfaces):
        match = _interface_block(arxml, name)
        block = match.group(0)
        if kind == "value":
            block = _value_fault(block, references, name)
        else:
            block = _description_fault(block, match["indent"])
        package = CONFLICT_PACKAGE.format(k=k)
        packages.append(
            f"{indent}<AR-PACKAGE>\n{indent}  <SHORT-NAME>{package}</SHORT-NAME>\n{indent}  <ELEMENTS>\n"
            f"{block}{indent}  </ELEMENTS>\n{indent}</AR-PACKAGE>\n"
        )
    return arxml[:line_start] + "".join(packages) + arxml[line_start:]


def write_faulted_export(source: Path, output: Path, interfaces: Sequence[str], kind: str) -> None:
    """The export at *source* with :func:`add_conflicts` applied, written to *output*."""
    arxml = source.read_text(encoding="utf-8")
    output.write_text(add_conflicts(arxml, interfaces, kind), encoding="utf-8", newline="\n")
