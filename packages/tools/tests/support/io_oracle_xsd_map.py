"""The input/output oracle's XSD map: which ``(parent tag, tag)`` holds a number, and which is a wrapper.

The generator of :mod:`io_oracle_xsd_map_data`, the committed module the input/output oracle reads. It walks the
two licensed schemas (R4.2.2 and R4.3.0), resolves every element declaration **in its parent's type** and records,
per ``(parent tag, tag)`` pair:

- the pair's **number class** (``NUMERIC_CONTEXTS``), decided by the child's type and never by a tag: the simple
  type a ``simpleContent`` extension rests on (:data:`SIMPLE_TYPE_CLASSES`), or a mixed type's
  ``mmt.qualifiedName`` (:data:`FORMULA_QUALIFIED_NAMES`). Every other type is no number class, and the oracle
  compares its text exactly;
- whether the child is a **wrapper** (``WRAPPER_CONTEXTS``): an anonymous element-only complex type, the
  serialization's role and type wrappers (``[TPS_GST_00064]``), every ``*-VARIANTS`` element among them.

A pair whose declarations disagree on the class or the wrapper verdict is excluded from both sets and listed in the
data module's header. One pair per distinct ``(declaring element's name, declared child element's name)``, the
children taken through ``xsd:sequence``, ``xsd:choice`` and ``xsd:group``, never into a child's own type; the two
releases' pairs are united.

**What it does not reach.** Attributes; a mixed type's text beyond its class; a schema other than the two walked.
An ``xsd:complexContent`` derivation is not descended into, an ``xsd:element ref=`` child is skipped, and an
anonymous ``simpleContent`` type is classed no number (on both schemas: none of the first two, and every one of the
third extends ``REF``). It imports ``lxml`` and the standard library only, like the oracle.

Run ``python packages/tools/tests/support/io_oracle_xsd_map.py`` with ``SWCRAFT_AUTOSAR_XSD_PATH_4_2_2`` and
``SWCRAFT_AUTOSAR_XSD_PATH_4_3_0`` set to rewrite the data module; never hand-edit it.
``test_io_oracle_xsd_map.py`` re-derives it in the ``test-xsd`` suite.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from lxml import etree

#: The committed data module this generator writes.
DATA_MODULE = Path(__file__).with_name("io_oracle_xsd_map_data.py")

#: The release each schema variable names, in the order the header lists them.
SCHEMA_VARIABLES: Mapping[str, str] = {
    "4.2.2": "SWCRAFT_AUTOSAR_XSD_PATH_4_2_2",
    "4.3.0": "SWCRAFT_AUTOSAR_XSD_PATH_4_3_0",
}

_XS = "{http://www.w3.org/2001/XMLSchema}"

#: The repository's line length (``ruff.toml``), which the data module is written within.
_LINE_LENGTH = 120

#: The number class of a ``simpleContent`` type, by its simple type's exact name.
#: ``numerical``: the ``NUMERICAL-VALUE`` pattern (decimal, float, octal ``0[0-7]+``, hexadecimal,
#: binary, ``INF``/``-INF``/``NaN``); ``integer``: the signed integer patterns (no fraction, no
#: exponent, a sign on a nonzero decimal only); ``positive-integer``: the unsigned integer patterns of
#: ``POSITIVE-INTEGER`` and ``POSITIVE-UNLIMITED-INTEGER`` (XSD R4.3.0:86393, :86410; R4.2.2:75075,
#: :75092) and ``ALIGNMENT-TYPE`` (R4.3.0:79341; R4.2.2:68895), which admit no sign at all;
#: ``double``: ``xsd:double``'s lexical space, where ``010`` is ten.
SIMPLE_TYPE_CLASSES: Mapping[str, NumberClass] = {
    "NUMERICAL-VALUE--SIMPLE": "numerical",
    "LIMIT-VALUE--SIMPLE": "numerical",
    "INTEGER--SIMPLE": "integer",
    "POSITIVE-INTEGER--SIMPLE": "positive-integer",
    "UNLIMITED-INTEGER--SIMPLE": "integer",
    "POSITIVE-UNLIMITED-INTEGER--SIMPLE": "positive-integer",
    "ALIGNMENT-TYPE--SIMPLE": "positive-integer",
    "FLOAT--SIMPLE": "double",
    "TIME-VALUE--SIMPLE": "double",
}

#: The mixed complex types whose text is the formula language's number (class ``formula``), by
#: ``mmt.qualifiedName``: ``LIMIT`` (*"in fact a NumericalValuationPoint"*, XSD R4.3.0:47494) and the
#: five numeric ``*-VALUE-VARIATION-POINT`` types. ``BooleanValueVariationPoint`` is not among them:
#: the six subclasses of ``AttributeValueVariationPoint`` *"provide information on the expected return
#: type of the formula"* and correspond to the primitive types *"Numerical, Float, Integer, Boolean,
#: PositiveInteger and UnlimitedInteger"* (GST R4.3.0:9304-9311; R4.2.2:8191-8199) -- a boolean's
#: return type is no number, so its text is compared exactly.
FORMULA_QUALIFIED_NAMES: frozenset[str] = frozenset(
    {
        "LimitValueVariationPoint",
        "NumericalValueVariationPoint",
        "IntegerValueVariationPoint",
        "PositiveIntegerValueVariationPoint",
        "UnlimitedIntegerValueVariationPoint",
        "FloatValueVariationPoint",
    }
)

#: A number class the closed oracle parses (its ``as_number``).
NumberClass = Literal["numerical", "integer", "positive-integer", "double", "formula"]

#: The number classes, in the order the map's header lists them.
NUMBER_CLASSES: tuple[NumberClass, ...] = ("numerical", "integer", "positive-integer", "double", "formula")


def _strip(name: str) -> str:
    """*name* without its namespace prefix."""
    return name.split(":", 1)[-1]


def _qualified_name(node: etree._Element) -> str | None:
    """The ``mmt.qualifiedName`` a declaration's ``tags`` appinfo states, or ``None``."""
    for appinfo in node.iterfind(f"{_XS}annotation/{_XS}appinfo"):
        if appinfo.get("source") != "tags":
            continue
        for item in (appinfo.text or "").split(";"):
            key, _, value = item.partition("=")
            if key.strip() == "mmt.qualifiedName":
                return value.strip().strip('"')
    return None


@dataclass(frozen=True)
class Verdict:
    """One child declaration's verdict: its number class (``None`` for an exact text) and its wrapper-ness."""

    klass: NumberClass | None
    wrapper: bool


@dataclass
class Release:
    """What one schema's walk found."""

    release: str
    sha256: str
    file_name: str
    #: ``(parent, tag)`` -> every verdict a declaration of that pair received.
    pairs: dict[tuple[str, str], set[Verdict]] = field(default_factory=lambda: defaultdict(set))
    #: The type names each class was decided by (simple type names and qualified names).
    decided_by: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))


class _Schema:
    def __init__(self, path: str) -> None:
        self.root = etree.parse(path).getroot()
        self.complex_types = {e.get("name"): e for e in self.root.iter(f"{_XS}complexType") if e.get("name")}
        self.simple_types = {e.get("name"): e for e in self.root.iter(f"{_XS}simpleType") if e.get("name")}
        self.groups = {e.get("name"): e for e in self.root.iter(f"{_XS}group") if e.get("name")}
        #: ``id(content)`` -> ``(content, children)``: the element is held so its ``id`` is never reused.
        self._children: dict[int, tuple[etree._Element, list[etree._Element]]] = {}

    def children(self, content: etree._Element) -> list[etree._Element]:
        """The element declarations of a complex type's content model, through groups, never into a child's type."""
        key = id(content)
        if key not in self._children:
            found: list[etree._Element] = []
            self._collect(content, found, set())
            self._children[key] = (content, found)
        return self._children[key][1]

    def _collect(self, node: etree._Element, found: list[etree._Element], seen: set[str]) -> None:
        for child in node:
            if not isinstance(child.tag, str) or child.tag in (f"{_XS}annotation", f"{_XS}attribute"):
                continue
            if child.tag == f"{_XS}element":
                found.append(child)
            elif child.tag == f"{_XS}group" and child.get("ref"):
                name = _strip(child.get("ref"))
                if name not in seen:
                    self._collect(self.groups[name], found, seen | {name})
            elif child.tag in (f"{_XS}sequence", f"{_XS}choice", f"{_XS}all"):
                self._collect(child, found, seen)

    def content_of(self, declaration: etree._Element) -> etree._Element | None:
        """The complex type an element declaration's children are declared in, or ``None`` for a simple type."""
        anonymous = declaration.find(f"{_XS}complexType")
        if anonymous is not None:
            return anonymous
        type_name = declaration.get("type")
        return self.complex_types.get(_strip(type_name)) if type_name else None

    def verdict(self, declaration: etree._Element, decided_by: dict[str, set[str]]) -> Verdict:
        """*declaration*'s :class:`Verdict`, each class's deciding simple type added to *decided_by*."""
        anonymous = declaration.find(f"{_XS}complexType")
        if anonymous is not None:
            element_only = (
                anonymous.get("mixed") != "true"
                and anonymous.find(f"{_XS}simpleContent") is None
                and anonymous.find(f"{_XS}complexContent") is None
            )
            return Verdict(None, element_only)
        type_name = _strip(declaration.get("type") or "")
        if type_name in self.simple_types:
            klass = SIMPLE_TYPE_CLASSES.get(type_name)
            if klass:
                decided_by[klass].add(type_name)
            return Verdict(klass, False)
        complex_type = self.complex_types.get(type_name)
        if complex_type is None:
            return Verdict(None, False)
        if complex_type.get("mixed") == "true":
            qualified = _qualified_name(complex_type)
            if qualified in FORMULA_QUALIFIED_NAMES:
                decided_by["formula"].add(f"{type_name} ({qualified})")
                return Verdict("formula", False)
            return Verdict(None, False)
        simple = complex_type.find(f"{_XS}simpleContent")
        if simple is not None:
            derivation = next((c for c in simple if isinstance(c.tag, str) and c.get("base")), None)
            base = _strip(derivation.get("base")) if derivation is not None else ""
            klass = SIMPLE_TYPE_CLASSES.get(base)
            if klass:
                decided_by[klass].add(base)
            return Verdict(klass, False)
        return Verdict(None, False)


def walk(release: str, path: str) -> Release:
    """One schema's pairs and verdicts."""
    schema = _Schema(path)
    data = Path(path).read_bytes()
    found = Release(release, hashlib.sha256(data).hexdigest(), Path(path).name)
    for declaration in schema.root.iter(f"{_XS}element"):
        parent = declaration.get("name")
        if not parent:
            continue
        content = schema.content_of(declaration)
        if content is None:
            continue
        for child in schema.children(content):
            name = child.get("name")
            if name:
                found.pairs[(parent, name)].add(schema.verdict(child, found.decided_by))
    return found


@dataclass(frozen=True)
class Derivation:
    """Both releases' walks, united."""

    releases: tuple[Release, ...]
    numeric: Mapping[tuple[str, str], NumberClass]
    wrappers: frozenset[tuple[str, str]]
    #: ``(parent, tag)`` -> why it is excluded (two classes, or a wrapper in one declaration only).
    excluded: Mapping[tuple[str, str], str]


def derive(schemas: Mapping[str, str]) -> Derivation:
    """The map from the schemas named by *schemas* (release -> path), in :data:`SCHEMA_VARIABLES`' order."""
    releases = tuple(walk(release, schemas[release]) for release in SCHEMA_VARIABLES)
    verdicts: dict[tuple[str, str], set[Verdict]] = defaultdict(set)
    for found in releases:
        for pair, seen in found.pairs.items():
            verdicts[pair] |= seen
    numeric: dict[tuple[str, str], NumberClass] = {}
    wrappers: set[tuple[str, str]] = set()
    excluded: dict[tuple[str, str], str] = {}
    for pair, seen in verdicts.items():
        classes = {v.klass for v in seen}
        wrapped = {v.wrapper for v in seen}
        if len(classes) > 1:
            excluded[pair] = "classes " + ", ".join(sorted(str(c) for c in classes))
            continue
        if len(wrapped) > 1:
            excluded[pair] = "a wrapper in some declarations only"
            continue
        (klass,) = classes
        if klass is not None:
            numeric[pair] = klass
        if True in wrapped:
            wrappers.add(pair)
    return Derivation(releases, numeric, frozenset(wrappers), excluded)


def _pair(pair: tuple[str, str]) -> str:
    """*pair* spelled as the data module's tuple literal."""
    return f'("{pair[0]}", "{pair[1]}")'


def render(derivation: Derivation) -> str:
    """The data module's text."""
    first, second = derivation.releases
    only_first = sorted(set(first.pairs) - set(second.pairs))
    only_second = sorted(set(second.pairs) - set(first.pairs))
    class_changes = sorted(
        pair
        for pair in set(first.pairs) & set(second.pairs)
        if {v.klass for v in first.pairs[pair]} != {v.klass for v in second.pairs[pair]}
    )
    lines = [
        '"""GENERATED by ``io_oracle_xsd_map.py`` -- never hand-edit; ``test_io_oracle_xsd_map.py`` re-derives it.',
        "",
        "The input/output oracle's XSD map (the generator's docstring states the convention and the limits).",
        "",
        "Schemas walked:",
        "",
    ]
    for found in derivation.releases:
        lines.append(f"- R{found.release}: ``{found.file_name}``, SHA-256 ``{found.sha256}``,")
        lines.append(f"  {len(found.pairs)} distinct ``(parent tag, tag)`` pairs.")
    lines += [
        "",
        "Pair-counting convention: one pair per distinct (declaring element's name, declared child",
        "element's name), the children taken from the parent's type's content model through sequences,",
        "choices and group references, never into a child's own type; both releases united.",
        "",
        "Classes decided by (the simple type a ``simpleContent`` extension rests on, or a mixed type's",
        "``mmt.qualifiedName``):",
        "",
    ]
    for klass in NUMBER_CLASSES:
        names = sorted(set().union(*(found.decided_by.get(klass, set()) for found in derivation.releases)))
        lines.append(f"- ``{klass}``:" + ("" if names else " (none)"))
        lines.extend(f"  ``{name}``" for name in names)
    counts = Counter(derivation.numeric.values())
    lines += [
        "",
        f"Sizes: {len(derivation.numeric)} numeric contexts ("
        + ", ".join(f"{klass} {counts[klass]}" for klass in NUMBER_CLASSES)
        + "),",
        f"{len(derivation.wrappers)} wrapper contexts.",
        "",
        f"Excluded pairs (two verdicts across their declarations): {len(derivation.excluded)}.",
    ]
    for pair in sorted(derivation.excluded):
        lines.append(f"- ``{pair[0]}/{pair[1]}``: {derivation.excluded[pair]}")
    lines += [
        "",
        f"Per-release differences: {len(only_first)} pairs in R{first.release} only, {len(only_second)} in"
        f" R{second.release} only, {len(class_changes)} with a class that differs between the releases.",
    ]
    for pair in class_changes:
        lines.append(f"- class change: ``{pair[0]}/{pair[1]}``")
    lines += [
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from collections.abc import Mapping",
        "",
        "#: ``(parent tag, tag)`` -> the number class of the child's text.",
        "NUMERIC_CONTEXTS: Mapping[tuple[str, str], str] = {",
    ]
    for pair in sorted(derivation.numeric):
        line = f'    {_pair(pair)}: "{derivation.numeric[pair]}",'
        if len(line) > _LINE_LENGTH:
            # The form ``ruff format`` gives an entry too long for one line.
            line = f'    (\n        "{pair[0]}",\n        "{pair[1]}",\n    ): "{derivation.numeric[pair]}",'
        lines.append(line)
    lines += [
        "}",
        "",
        "#: Every ``(parent tag, tag)`` whose child is a role or type wrapper (an anonymous element-only type).",
        "WRAPPER_CONTEXTS: frozenset[tuple[str, str]] = frozenset(",
        "    {",
    ]
    for pair in sorted(derivation.wrappers):
        lines.append(f"        {_pair(pair)},")
    lines += ["    }", ")", ""]
    return "\n".join(lines)


def main() -> int:
    schemas = {}
    for release, variable in SCHEMA_VARIABLES.items():
        path = os.environ.get(variable)
        if not path or not os.path.isfile(path):
            print(f"{variable} must name the licensed R{release} schema", file=sys.stderr)
            return 2
        schemas[release] = path
    DATA_MODULE.write_text(render(derive(schemas)), encoding="utf-8", newline="\n")
    print(f"wrote {DATA_MODULE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
