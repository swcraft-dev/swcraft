"""First-party ARXML serialization primitives and the ``ArxmlDocument`` container.

This module is the whole of the writer's contact with XML: namespaced element creation, ``SHORT-NAME``
placement, reference elements with their mandatory ``DEST``, numeric/boolean formatting, the AR-PACKAGE
scaffolding and deterministic serialization.

The output is byte-compatible with the goldens under ``packages/tools/tests/fixtures/arxml_golden/``, which
record three habits no XML library's defaults reproduce:

* **Nothing is ever self-closed, and an empty wrapper spans two lines** (``<PORTS>`` then ``</PORTS>``), see
  :func:`_expand_empty_elements`. An element whose text is the empty **string** (a model value of ``""``) keeps
  the one-line ``<TAG></TAG>``, which round-trips back to ``""``.
* **The XML declaration uses double quotes and lowercase ``utf-8``**: ``lxml``'s uses single quotes, so the tree
  is serialized without one and ``XML_DECLARATION`` is prepended verbatim.
* **An empty AR-PACKAGE emits no ``ELEMENTS`` wrapper**, so ``ArxmlPackage`` creates it on its first child.

Floats are written in fixed-point notation (:func:`format_float`): a ``PERIOD`` or ``MINIMUM-START-INTERVAL`` is
written exactly, with no millisecond grid, and scientific notation in a numeric ARXML field would be a defect.
"""

from __future__ import annotations

import copy
import enum
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from os import PathLike
from pathlib import Path

from lxml import etree

from swcraft_tools.arxml_version import (
    DATA_DESCRIPTION_PACKAGE_NAMES,
    DEFAULT_ARXML_VERSION,
    NAMESPACE,
    ROOT_PACKAGE_NAMES,
    XSI_NAMESPACE,
    ArxmlVersion,
)
from swcraft_tools.identifier import short_name_key
from swcraft_tools.xml_text import unrepresentable_text_sentence, xml_unrepresentable_character

#: The AUTOSAR XML namespace, re-exported from ``arxml_version`` (the dependency
#: direction is one-way: the version module never imports the writer).
AR_NAMESPACE = NAMESPACE

#: Written verbatim ahead of the serialized tree — double quotes and lowercase
#: ``utf-8``, matching the goldens.
XML_DECLARATION = '<?xml version="1.0" encoding="utf-8"?>'

#: One indentation level: ``lxml``'s ``pretty_print`` uses two spaces, as the goldens do.
INDENT = "  "

#: Tag of the throwaway root ``serialize_fragment()`` wraps a fragment in, purely
#: so the namespace is declared once and stripped with the wrapper.
_FRAGMENT_HOLDER_TAG = "FRAGMENT"

_SHORT_NAME_TAG = "SHORT-NAME"

#: What may be handed to an element as its text.
Value = str | int | float | bool


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------


def format_integer(value: int) -> str:
    """Format an integer for an ARXML text node.

    Raises:
        TypeError: for a ``bool`` — it is an ``int`` subclass, but AUTOSAR wants
            ``true``/``false``, so silently writing ``1`` would be wrong.
    """
    if isinstance(value, bool):
        raise TypeError("Booleans must be formatted with format_boolean(), not format_integer()")
    if not isinstance(value, int):
        raise TypeError(f"Expected an int, got {type(value).__name__}: {value!r}")
    return str(value)


def format_float(value: float) -> str:
    """Format a float for an ARXML text node, in fixed-point notation.

    An integral value prints with no fractional part (``0.0`` -> ``"0"``), anything else normalized (no trailing
    zeros), via ``Decimal(str(value))``, so binary representation noise never reaches the file.

    Raises:
        ValueError: for ``inf``/``-inf``/``nan``. No SWCraft field can hold one, so a non-finite value here
            is a bug worth surfacing rather than encoding.
    """
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Cannot format non-finite value {number!r} as an ARXML number")
    decimal = Decimal(str(number))
    if decimal == 0:
        # Covers -0.0, which would otherwise print as "-0".
        return "0"
    if decimal == decimal.to_integral_value():
        # to_integral_value(), not quantize(): quantize raises InvalidOperation
        # once the exponent exceeds the decimal context's precision.
        return f"{decimal.to_integral_value():f}"
    return f"{decimal.normalize():f}"


def format_init_number(value: int | float) -> str:
    """Format an init value's numeric leaf, so the reader returns the same type.

    A float always carries a ``.`` (fixed point, ``.0`` appended when needed; ``-0.0`` prints ``0.0``), because
    ``arxml_reader.py`` reads text without one as an ``int``; an ``int`` is its plain decimal spelling. The width is
    unbounded (``1e-300`` prints 302 characters).

    Raises:
        TypeError: for a ``bool`` (the emitter maps it to ``1``/``0``), or anything not an ``int`` or a ``float``.
        ValueError: for ``inf``/``-inf``/``nan``, or an ``int`` this interpreter cannot convert to its decimal string.
    """
    if isinstance(value, bool):
        raise TypeError("Booleans must be mapped to 1/0 by the emitter, not formatted with format_init_number()")
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, float):
        raise TypeError(f"Expected an int or a float, got {type(value).__name__}: {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"Cannot format non-finite value {value!r} as an ARXML init value")
    decimal = Decimal(str(value))
    if decimal == 0:
        # Covers -0.0, which would otherwise print as "-0.0".
        return "0.0"
    text = f"{decimal:f}"
    return text if "." in text else f"{text}.0"


def format_boolean(value: bool) -> str:
    """Format a boolean as AUTOSAR's lowercase ``true``/``false``."""
    return "true" if value else "false"


def format_value(value: Value) -> str:
    """Format any supported text-node value.

    ``bool`` is checked before ``int`` on purpose — it is a subclass of it.
    """
    if isinstance(value, bool):
        return format_boolean(value)
    if isinstance(value, int):
        return format_integer(value)
    if isinstance(value, float):
        return format_float(value)
    if isinstance(value, str):
        return value
    raise TypeError(f"Unsupported ARXML text value of type {type(value).__name__}: {value!r}")


# ---------------------------------------------------------------------------
# Element creation
# ---------------------------------------------------------------------------


def ar_qname(tag: str) -> str:
    """Return *tag* qualified with the AUTOSAR namespace, as ``lxml`` wants it."""
    return f"{{{AR_NAMESPACE}}}{tag}"


def make_element(tag: str, text: Value | None = None) -> etree._Element:
    """Create a detached namespaced element, optionally with formatted text."""
    node = etree.Element(ar_qname(tag))
    if text is not None:
        node.text = format_value(text)
    return node


def sub_element(parent: etree._Element, tag: str, text: Value | None = None) -> etree._Element:
    """Append a namespaced child to *parent*, optionally with formatted text."""
    node = etree.SubElement(parent, ar_qname(tag))
    if text is not None:
        node.text = format_value(text)
    return node


def set_short_name(element: etree._Element, short_name: str) -> etree._Element:
    """Insert a ``SHORT-NAME`` as *element*'s **first** child (where the goldens and every reader expect it).

    Raises:
        ValueError: if *element* already has a ``SHORT-NAME`` child.
    """
    if element.find(ar_qname(_SHORT_NAME_TAG)) is not None:
        raise ValueError(f"<{etree.QName(element).localname}> already has a SHORT-NAME")
    node = make_element(_SHORT_NAME_TAG, short_name)
    element.insert(0, node)
    return node


def named_element(parent: etree._Element, tag: str, short_name: str) -> etree._Element:
    """Append a child carrying a ``SHORT-NAME`` and return it."""
    node = sub_element(parent, tag)
    set_short_name(node, short_name)
    return node


#: The ``L`` every written ``L-2`` and ``L-4`` carries: *"``FOR-ALL`` means, that
#: the entity is applicable to all languages. It is language neutral"* (the
#: ``LANGUAGE-SPECIFIC`` attribute's own documentation, XSD 4.2.2 and 4.3.0). The
#: model's text names no language, so the one value that claims none is written.
LANGUAGE_NEUTRAL = "FOR-ALL"

#: The children that stand before a ``DESC`` wherever one is written: the
#: ``Identifiable`` order (``SHORT-NAME``, ``SHORT-NAME-FRAGMENTS``, ``LONG-NAME``,
#: then ``DESC``) and the ``COMPU-SCALE`` group's (``SHORT-LABEL``, ``SYMBOL``, then
#: ``DESC``), the same in both licensed schemas.
_DESC_PRECEDERS = frozenset({_SHORT_NAME_TAG, "SHORT-NAME-FRAGMENTS", "LONG-NAME", "SHORT-LABEL", "SYMBOL"})

#: The children that stand before a ``LONG-NAME`` (``MULTILANGUAGE-REFERRABLE``).
_LONG_NAME_PRECEDERS = frozenset({_SHORT_NAME_TAG, "SHORT-NAME-FRAGMENTS"})


def _insert_after_leading(element: etree._Element, node: etree._Element, preceders: frozenset[str]) -> None:
    """Insert *node* into *element* directly after its leading run of children tagged in *preceders*."""
    index = 0
    for child in element:
        if not isinstance(child.tag, str) or etree.QName(child).localname not in preceders:
            break
        index += 1
    element.insert(index, node)


def _language_paragraph(owner: etree._Element, wrapper_tag: str, paragraph_tag: str, text: str) -> etree._Element:
    """Build a detached ``wrapper_tag`` holding one ``paragraph_tag L="FOR-ALL"`` whose text is *text*, exactly.

    Raises:
        ValueError: ``rule:text.xml-unrepresentable-character: <sentence>`` if *text* holds a character XML 1.0
            cannot carry, naming the owner by its tag and ``SHORT-NAME``. Decided by
            :func:`~swcraft_tools.xml_text.xml_unrepresentable_character`, the predicate the rule judges with, so
            ``check-model`` reports every text refused here.
    """
    character = xml_unrepresentable_character(text)
    if character is not None:
        owner_name = short_name_of(owner)
        subject = f"<{etree.QName(owner).localname}>" + (f" {owner_name!r}" if owner_name is not None else "")
        raise ValueError(
            "rule:text.xml-unrepresentable-character: "
            + unrepresentable_text_sentence(f"The {wrapper_tag} text of {subject}", character)
        )
    wrapper = make_element(wrapper_tag)
    paragraph = sub_element(wrapper, paragraph_tag)
    paragraph.set("L", LANGUAGE_NEUTRAL)
    paragraph.text = text
    return wrapper


def set_description(element: etree._Element, text: str) -> etree._Element | None:
    """Write *text* as *element*'s ``DESC``, one ``L-2 L="FOR-ALL"``, at its schema position; return it.

    The position is found, not assumed, so an emitter may call this at any point. The text is written exactly as held
    (``desc`` is *"only one paragraph"*, so a multi-line text is one ``L-2``).

    Returns:
        The created ``DESC``, or ``None`` for ``""`` (``desc`` is ``0..1``).

    Raises:
        ValueError: if *element* already has a ``DESC``, or *text* holds a character XML 1.0 cannot carry.
    """
    if text == "":
        return None
    if element.find(ar_qname("DESC")) is not None:
        raise ValueError(f"<{etree.QName(element).localname}> already has a DESC")
    node = _language_paragraph(element, "DESC", "L-2", text)
    _insert_after_leading(element, node, _DESC_PRECEDERS)
    return node


def set_long_name(element: etree._Element, text: str) -> etree._Element:
    """Write *text* as *element*'s ``LONG-NAME``, one ``L-4 L="FOR-ALL"``, directly after its ``SHORT-NAME``.

    Raises:
        ValueError: if *element* already has a ``LONG-NAME``, or *text* holds a character XML 1.0 cannot carry.
    """
    if element.find(ar_qname("LONG-NAME")) is not None:
        raise ValueError(f"<{etree.QName(element).localname}> already has a LONG-NAME")
    node = _language_paragraph(element, "LONG-NAME", "L-4", text)
    _insert_after_leading(element, node, _LONG_NAME_PRECEDERS)
    return node


def short_name_of(element: etree._Element) -> str | None:
    """Return *element*'s own ``SHORT-NAME`` text, or None when it carries none.

    An element with an empty ``SHORT-NAME`` returns ``""`` — distinguishable
    from None, which means "this element is an unnamed wrapper".
    """
    node = element.find(ar_qname(_SHORT_NAME_TAG))
    if node is None:
        return None
    return node.text or ""


def _find_named_child(parent: etree._Element, short_name: str) -> etree._Element | None:
    """Return *parent*'s nearest descendant named *short_name*, in document order.

    Unnamed wrappers (``AR-PACKAGES``, ``ELEMENTS``, ``PORTS``, ...) are descended into, while a named element that
    does not match ends the search down that branch, so resolution stays a path walk.
    """
    for child in parent:
        name = short_name_of(child)
        if name is None:
            found = _find_named_child(child, short_name)
            if found is not None:
                return found
        elif name == short_name:
            return child
    return None


def find_element(root: etree._Element, path: str) -> etree._Element | None:
    """Resolve an absolute AUTOSAR path such as ``/ComponentTypes/MySwc`` from *root*.

    Each segment matches the ``SHORT-NAME`` of the nearest named descendants, so the wrappers (``AR-PACKAGES``,
    ``ELEMENTS``, ``PORTS``, ...) are not part of the path, exactly as in the document's ``*-REF`` paths.

    Returns:
        The element the path names, or None when no such element exists.

    Raises:
        ValueError: if *path* is not absolute, or names no segment at all.
    """
    if not path.startswith("/"):
        raise ValueError(f"An AUTOSAR element path must be absolute, got {path!r}")
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        raise ValueError("An AUTOSAR element path must name at least one element")
    node: etree._Element | None = root
    for segment in segments:
        node = _find_named_child(node, segment)
        if node is None:
            return None
    return node


def reference_element(parent: etree._Element, tag: str, path: str | None, *, dest: str | None) -> etree._Element:
    """Append a reference element (``<TAG DEST="...">/path</TAG>``) to *parent*.

    Raises:
        ValueError: if *path* or *dest* is empty or ``None``.
    """
    if not path:
        raise ValueError(f"Reference <{tag}> requires a non-empty path, got {path!r}")
    if not dest:
        raise ValueError(f"Reference <{tag}> to {path!r} requires a DEST attribute, got {dest!r}")
    node = sub_element(parent, tag, path)
    node.set("DEST", dest)
    return node


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _depth(element: etree._Element) -> int:
    """Return *element*'s nesting depth (the document root is 0)."""
    depth = 0
    parent = element.getparent()
    while parent is not None:
        depth += 1
        parent = parent.getparent()
    return depth


def _expand_empty_elements(root: etree._Element) -> None:
    """Give every childless element under *root* whose text is ``None`` the goldens' two-line form; mutates *root*.

    An element whose text is ``""`` carries a model value and is left alone.
    """
    for element in root.iter():
        if len(element) == 0 and element.text is None:
            element.text = "\n" + INDENT * _depth(element)


def serialize_document(root: etree._Element) -> str:
    """Serialize a whole document: the XML declaration, then the pretty-printed tree.

    The result ends with exactly one newline, matching the goldens.
    """
    prepared = copy.deepcopy(root)
    _expand_empty_elements(prepared)
    body = etree.tostring(prepared, pretty_print=True, encoding="unicode")
    return f"{XML_DECLARATION}\n{body}"


def serialize_fragment(element: etree._Element) -> str:
    """Serialize one element with the same settings as a whole document.

    The result is dedented to column 0 and carries no trailing newline and no namespace declaration, so it
    compares directly against ``conftest.golden_slice()``.
    """
    holder = etree.Element(ar_qname(_FRAGMENT_HOLDER_TAG), nsmap={None: AR_NAMESPACE})
    copied = copy.deepcopy(element)
    copied.tail = None
    holder.append(copied)
    _expand_empty_elements(holder)
    lines = etree.tostring(holder, pretty_print=True, encoding="unicode").splitlines()[1:-1]
    return "\n".join(line[len(INDENT) :] if line.startswith(INDENT) else line for line in lines)


# ---------------------------------------------------------------------------
# SHORT-NAME uniqueness
# ---------------------------------------------------------------------------


class ShortNameScope:
    """Sibling ``SHORT-NAME`` uniqueness for one parent element.

    AUTOSAR requires a ``SHORT-NAME`` to be unique **among its siblings**, so that a ``*-REF`` can name it. Two
    ``<SHORT-NAME>p</SHORT-NAME>`` in one ``PORTS`` make ``PORT-REF``, ``TARGET-P-PORT-REF`` and the com-spec refs
    ambiguous, and ``arxml_reader.py``'s path index silently keeps one of the pair. ``arxml_generation``'s
    ``_check_*_names()`` guards catch the reachable model shapes first, naming the model directory; this is the
    structural backstop, also used by :class:`ArxmlPackage`, so the invariant has one implementation.

    **A scope's parent is the element whose path segment the names live under**, not always the element they are
    appended to. :meth:`create` serves direct children; where the members are grandchildren because the wrappers
    between carry no path segment (the four access-point containers of a ``RUNNABLE-ENTITY``), the caller builds the
    scope on the named parent and pairs :meth:`claim` with its own :func:`named_element` call.
    """

    def __init__(self, parent: etree._Element, description: str) -> None:
        """Guard *parent*'s direct children; *description* names the parent in the error message."""
        self._parent = parent
        self._description = description
        self._claimed: set[str] = set()

    def claim(self, short_name: str) -> None:
        """Reserve *short_name* in this scope, or raise if it is already taken.

        Raises:
            ValueError: if this scope already holds an element of that name.
        """
        if short_name in self._claimed:
            raise ValueError(
                f"Duplicate SHORT-NAME {short_name!r} in {self._description}: "
                "an AUTOSAR SHORT-NAME must be unique among its siblings"
            )
        self._claimed.add(short_name)

    def create(self, tag: str, short_name: str) -> etree._Element:
        """Create a named child of the guarded parent and return it.

        Raises:
            ValueError: if this scope already holds an element of that name.
        """
        self.claim(short_name)
        return named_element(self._parent, tag, short_name)


# ---------------------------------------------------------------------------
# The document container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackagePlacement:
    """Where each root-level element is emitted: its home package, or one sub-package of it.

    The model's ``package`` field names the one sub-package of its home an element is emitted in; absent, the
    element lives in the home itself. **The home** (:class:`Home`) is a root package for an interface or a component,
    and the package of its layer under ``/DataTypes`` for a data type, a generated form and a mapping set.
    **The writer reads placement; it never computes it.** A generated form is entered with its owner's home and
    package, so ``{enum}_CompuMethod`` always sits beside its enum. The default table is empty and puts every element
    in the package it is created in.

    Attributes:
        entries: ``(home path, name) -> package | None`` for every declared element and every generated form the
            model can produce, ``None`` meaning the home itself (a key may also name a root).
        declared: the keys of :attr:`entries` that are declared non-external elements, all of which must be emitted.
        sub_packages: per home path (and per root), the sub-package names the entries place something in.
    """

    class Home(enum.Enum):
        """The package an element is emitted in before its own ``package``, as a reference names it.

        A data description layer's package under ``/DataTypes`` or one of the two other roots; a layer with no element
        opens no package. Its value is the package's path below the document root, segments joined by ``/``.
        """

        BASE_TYPES = f"{ROOT_PACKAGE_NAMES[0]}/{DATA_DESCRIPTION_PACKAGE_NAMES['base_types']}"
        IMPLEMENTATION_TYPES = f"{ROOT_PACKAGE_NAMES[0]}/{DATA_DESCRIPTION_PACKAGE_NAMES['implementation_types']}"
        APPLICATION_TYPES = f"{ROOT_PACKAGE_NAMES[0]}/{DATA_DESCRIPTION_PACKAGE_NAMES['application_types']}"
        UNITS = f"{ROOT_PACKAGE_NAMES[0]}/{DATA_DESCRIPTION_PACKAGE_NAMES['units']}"
        COMPU_METHODS = f"{ROOT_PACKAGE_NAMES[0]}/{DATA_DESCRIPTION_PACKAGE_NAMES['compu_methods']}"
        DATA_CONSTRAINTS = f"{ROOT_PACKAGE_NAMES[0]}/{DATA_DESCRIPTION_PACKAGE_NAMES['data_constraints']}"
        MAPPING_SETS = f"{ROOT_PACKAGE_NAMES[0]}/{DATA_DESCRIPTION_PACKAGE_NAMES['mapping_sets']}"
        PORT_INTERFACES = ROOT_PACKAGE_NAMES[1]
        COMPONENT_TYPES = ROOT_PACKAGE_NAMES[2]

        @property
        def root(self) -> str:
            """The root package the home is, or lies in."""
            return self.value.split("/", 1)[0]

        @property
        def layer_package(self) -> str | None:
            """The layer package's own ``SHORT-NAME``, or ``None`` for a home that is a root."""
            _, _, package = self.value.partition("/")
            return package or None

    #: Every key a table may place under: the three roots and every home path.
    KEYS = frozenset({*ROOT_PACKAGE_NAMES, *(home.value for home in Home)})

    entries: Mapping[tuple[str, str], str | None] = field(default_factory=dict)
    declared: frozenset[tuple[str, str]] = frozenset()
    sub_packages: Mapping[str, frozenset[str]] = field(init=False)

    def __post_init__(self) -> None:
        """Copy the table, check its keys, and derive :attr:`sub_packages`.

        Raises:
            ValueError: for an entry outside the three roots and the homes, or a declared
                key the table does not place.
        """
        foreign = sorted({key for key, _ in self.entries} - self.KEYS)
        if foreign:
            raise ValueError(
                f"package placement names no root package or home {foreign}; the roots are {ROOT_PACKAGE_NAMES}"
            )
        unplaced = sorted(self.declared - self.entries.keys())
        if unplaced:
            raise ValueError(f"package placement declares elements it does not place: {unplaced}")
        object.__setattr__(self, "entries", dict(self.entries))
        object.__setattr__(
            self,
            "sub_packages",
            {
                key: frozenset(
                    package for (owner, _), package in self.entries.items() if owner == key and package is not None
                )
                for key in sorted(self.KEYS)
            },
        )

    def knows(self, key: str, name: str) -> bool:
        """Return whether the table has an entry for *name* in the package *key* (a home path or a root)."""
        return (key, name) in self.entries

    def package_of(self, key: str, name: str) -> str | None:
        """Return the sub-package of the package *key* that *name* is emitted in, or ``None`` for *key* itself.

        An unknown name is ``None`` too; ``arxml_generation._build_document``'s containment check refuses an element the
        table did not expect.
        """
        return self.entries.get((key, name))


class ArxmlPackage:
    """One ``AR-PACKAGE``, with its ``ELEMENTS`` wrapper created on first use (an empty package emits none).

    A repeated element ``SHORT-NAME`` raises (a :class:`ShortNameScope`). A root package asks its
    :class:`PackagePlacement` where each element goes; sub-packages, ``AR-PACKAGES`` and ``ELEMENTS`` are created
    lazily, so a model placing nothing emits no ``AR-PACKAGES`` inside a root. The schemas order an ``AR-PACKAGE``'s
    ``ELEMENTS`` before its ``AR-PACKAGES`` (``AUTOSAR_4-2-2.xsd:115`` and ``:346``, ``AUTOSAR_4-3-0.xsd:114`` and
    ``:401``), so ``ELEMENTS`` is inserted before an existing ``AR-PACKAGES``. Every claim is recorded for the
    containment check.

    **The package claim folds case**: a ``SHORT-NAME`` is unique *ignoring case* within its package
    (``[constr_2508]``), so a differently spelled case-equal name, or a sub-package's name equal to an element's,
    raises ``rule:name.name-space-collision``. An exact repeat keeps the :class:`ShortNameScope` message; the nested
    per-container scopes stay exact.
    """

    def __init__(
        self,
        parent: etree._Element,
        name: str,
        placement: PackagePlacement | None = None,
        *,
        path: str | None = None,
        opener: Callable[[ArxmlPackage], None] | None = None,
    ) -> None:
        self._name = name
        # The package's absolute path, for the case-folding claim's message; without
        # its slash it is the key the placement table places under.
        self._path = f"/{name}" if path is None else path
        self._key = self._path.removeprefix("/")
        # short_name_key -> (the spelling claimed, whether it is a sub-package's).
        self._folded_claims: dict[str, tuple[str, bool]] = {}
        # A layer package is built detached and attached by *opener* on first use.
        if parent is None:
            self._node = make_element("AR-PACKAGE")
            set_short_name(self._node, name)
        else:
            self._node = named_element(parent, "AR-PACKAGE", name)
        self._opener = opener
        self._elements: etree._Element | None = None
        self._placement = placement if placement is not None else PackagePlacement()
        # The sub-packages, created on first use, and the AR-PACKAGES that holds them.
        self._sub_package_holder: etree._Element | None = None
        self._sub_packages: dict[str, ArxmlPackage] = {}
        # The layer packages opened in this root, in opening order.
        self._layer_packages: dict[str, ArxmlPackage] = {}
        self._element_claims: list[tuple[str | None, str]] = []
        # Only ``claim()`` is used: ``ELEMENTS`` is created lazily, so the scope
        # cannot own the creation call, but it owns the invariant and its message.
        self._scope = ShortNameScope(self._node, f"AR-PACKAGE {name!r}")

    @property
    def name(self) -> str:
        """The package's ``SHORT-NAME``."""
        return self._name

    @property
    def node(self) -> etree._Element:
        """The ``AR-PACKAGE`` element itself."""
        return self._node

    @property
    def has_elements(self) -> bool:
        """Whether anything has been added yet (i.e. whether ``ELEMENTS`` exists)."""
        return self._elements is not None

    @property
    def key(self) -> str:
        """The package's path without its leading slash: the key the placement table places under."""
        return self._key

    @property
    def elements(self) -> etree._Element:
        """The ``ELEMENTS`` wrapper, created on first access, before any ``AR-PACKAGES``."""
        self._open()
        if self._elements is None:
            self._elements = make_element("ELEMENTS")
            if self._sub_package_holder is None:
                self._node.append(self._elements)
            else:
                self._sub_package_holder.addprevious(self._elements)
        return self._elements

    @property
    def element_claims(self) -> tuple[tuple[str | None, str], ...]:
        """Every named element created or added here, as ``(sub-package | None, name)``, in order."""
        return tuple(self._element_claims)

    @property
    def sub_package_claims(self) -> tuple[str, ...]:
        """The sub-packages created here for an element's ``package``, in creation order."""
        return tuple(self._sub_packages)

    @property
    def layer_package_claims(self) -> tuple[str, ...]:
        """The layer packages opened in this root, in opening order."""
        return tuple(self._layer_packages)

    @property
    def layer_packages(self) -> tuple[ArxmlPackage, ...]:
        """The layer packages opened in this root, in opening order."""
        return tuple(self._layer_packages.values())

    def _open(self) -> None:
        """Attach a detached layer package to its root, once, before its first claim."""
        if self._opener is not None:
            opener, self._opener = self._opener, None
            opener(self)

    def open_layer_package(self, package: ArxmlPackage) -> None:
        """Attach the layer package *package* here, after every package already here, and claim its name.

        The name is claimed as a sub-package's, so it shares the root's name space.
        """
        self._claim_short_name(package.name, sub_package=True)
        if self._sub_package_holder is None:
            self._sub_package_holder = sub_element(self._node, "AR-PACKAGES")
        self._sub_package_holder.append(package.node)
        self._layer_packages[package.name] = package

    def _claim_short_name(self, short_name: str, *, sub_package: bool = False) -> None:
        """Reserve *short_name* in this package, ignoring case, or raise if it is already taken.

        Raises:
            ValueError: if this package already holds a name equal to it ignoring case and spelled differently, or a
                sub-package takes part (``rule:name.name-space-collision``, ``[constr_2508]``); or, for an exact repeat
                of one element name, the :class:`ShortNameScope` message.
        """
        key = short_name_key(short_name)
        held = self._folded_claims.get(key)
        if held is not None and (held[0] != short_name or held[1] or sub_package):
            raise ValueError(_package_name_space_message(self._path, held, (short_name, sub_package)))
        self._scope.claim(short_name)
        self._folded_claims[key] = (short_name, sub_package)

    def _sub_package(self, name: str) -> ArxmlPackage:
        """Return the sub-package *name*, created (and its name claimed here) on first use."""
        existing = self._sub_packages.get(name)
        if existing is not None:
            return existing
        self._claim_short_name(name, sub_package=True)
        if self._sub_package_holder is None:
            self._sub_package_holder = sub_element(self._node, "AR-PACKAGES")
        package = ArxmlPackage(self._sub_package_holder, name, path=f"{self._path}/{name}")
        self._sub_packages[name] = package
        return package

    def _home(self, short_name: str) -> ArxmlPackage:
        """Claim *short_name* where the placement puts it, and return the package that holds it.

        Raises:
            ValueError: if that package already holds an element of that name, or the
                sub-package's own name is already taken in this one.
        """
        self._open()
        placed = self._placement.package_of(self._key, short_name)
        home = self if placed is None else self._sub_package(placed)
        home._claim_short_name(short_name)
        self._element_claims.append((placed, short_name))
        return home

    def add_element(self, element: etree._Element) -> etree._Element:
        """Append an already-built element where the placement puts it, and return it.

        An element without a ``SHORT-NAME`` is appended here without a uniqueness claim.

        Raises:
            ValueError: if that package already holds an element of that name.
        """
        short_name = short_name_of(element)
        home = self if short_name is None else self._home(short_name)
        home.elements.append(element)
        return element

    def create_element(self, tag: str, short_name: str) -> etree._Element:
        """Create a named element where the placement puts it, and return it for filling in.

        Raises:
            ValueError: if that package already holds an element of that name.
        """
        return named_element(self._home(short_name).elements, tag, short_name)


def _package_name_space_message(path: str, held: tuple[str, bool], claimed: tuple[str, bool]) -> str:
    """Return the case-folding package claim's refusal: the rule, the package, both spellings and ``[constr_2508]``.

    The rule id is ``check-model``'s, so both point at one rule.
    """
    spelled = " and ".join(
        f"{name!r}{' (a sub-package)' if is_sub_package else ''}" for name, is_sub_package in (held, claimed)
    )
    return (
        f"rule:name.name-space-collision: package '{path}' holds {spelled}, equal ignoring case -- AUTOSAR "
        "requires a SHORT-NAME to be unique ignoring case within its package ([constr_2508]); set package on "
        "one of them to place it in a sub-package, or rename it."
    )


class ArxmlDocument:
    """An ARXML document at a given :class:`ArxmlVersion`, with SWCraft's three packages.

    The roots are created ``DataTypes``, ``PortInterfaces``, ``ComponentTypes``, and the writer emits in dependency
    order, so most references point backwards. That is a convenience, not a requirement: a reader resolves paths
    through an index. Two kinds point forward: a ``SW-COMPONENT-PROTOTYPE`` naming a composition emitted after it,
    and a reference from a root's element into one of that root's sub-packages.

    *placement* is the :class:`PackagePlacement` every root package consults. **Its homes** (:meth:`home`) are the
    roots ``PortInterfaces`` and ``ComponentTypes`` and one package per data description layer under ``DataTypes``,
    attached by its first element; so ``DataTypes`` itself holds no element, though an emitter a test calls on
    :attr:`data_types` still creates in the root.
    """

    #: In creation order, which is also emission order. An alias of
    #: ``arxml_version.ROOT_PACKAGE_NAMES``, the one definition the validator also reads.
    PACKAGE_NAMES: tuple[str, ...] = ROOT_PACKAGE_NAMES

    def __init__(
        self,
        version: ArxmlVersion | str = DEFAULT_ARXML_VERSION,
        *,
        placement: PackagePlacement | None = None,
    ) -> None:
        self._version = ArxmlVersion.from_string(version)
        self._placement = placement if placement is not None else PackagePlacement()
        self._root = etree.Element(
            ar_qname("AUTOSAR"),
            nsmap={None: AR_NAMESPACE, "xsi": XSI_NAMESPACE},
        )
        self._root.set(f"{{{XSI_NAMESPACE}}}schemaLocation", self._version.schema_location)
        packages = sub_element(self._root, "AR-PACKAGES")
        self._packages = {name: ArxmlPackage(packages, name, self._placement) for name in self.PACKAGE_NAMES}
        self._homes: dict[PackagePlacement.Home, ArxmlPackage] = {}
        for home in PackagePlacement.Home:
            root = self._packages[home.root]
            self._homes[home] = (
                root
                if home.layer_package is None
                else ArxmlPackage(
                    None,
                    home.layer_package,
                    self._placement,
                    path=f"/{home.value}",
                    opener=root.open_layer_package,
                )
            )

    @property
    def placement(self) -> PackagePlacement:
        """The package placement every root package of this document consults."""
        return self._placement

    @property
    def version(self) -> ArxmlVersion:
        """The AUTOSAR version this document declares."""
        return self._version

    @property
    def root(self) -> etree._Element:
        """The ``AUTOSAR`` root element."""
        return self._root

    @property
    def data_types(self) -> ArxmlPackage:
        """The ``DataTypes`` package."""
        return self._packages["DataTypes"]

    @property
    def port_interfaces(self) -> ArxmlPackage:
        """The ``PortInterfaces`` package."""
        return self._packages["PortInterfaces"]

    @property
    def component_types(self) -> ArxmlPackage:
        """The ``ComponentTypes`` package."""
        return self._packages["ComponentTypes"]

    def home(self, home: PackagePlacement.Home) -> ArxmlPackage:
        """Return the package an element of *home* is created in: a root, or a layer package opened on first use."""
        return self._homes[home]

    def placed_packages(self) -> tuple[ArxmlPackage, ...]:
        """Return every package the placement table places under: each root, then the layer packages opened in it.

        This is what ``arxml_generation._check_package_containment`` walks.
        """
        found: list[ArxmlPackage] = []
        for name in self.PACKAGE_NAMES:
            root = self._packages[name]
            found.append(root)
            found.extend(root.layer_packages)
        return tuple(found)

    def package(self, name: str) -> ArxmlPackage:
        """Return a package by ``SHORT-NAME``.

        Raises:
            ValueError: for a name this document has no package for.
        """
        try:
            return self._packages[name]
        except KeyError:
            raise ValueError(
                f"No package named {name!r} in this document; it has: {', '.join(self.PACKAGE_NAMES)}"
            ) from None

    def find(self, path: str) -> etree._Element | None:
        """Resolve an absolute AUTOSAR path within this document (see :func:`find_element`)."""
        return find_element(self._root, path)

    def serialize(self) -> str:
        """Return the whole document as text, byte-comparable against a golden."""
        return serialize_document(self._root)

    def save(self, output_path: str | PathLike[str], *, keep_previous: bool = False) -> str:
        """Write the document to *output_path*, creating parent directories; return the path through ``Path``.

        The text is serialized before the file is opened, so a failed generation leaves no partial file, and written
        with ``newline="\\n"``. With *keep_previous*, an existing file whose bytes differ is moved aside by
        :func:`move_previous_arxml` before the write (an identical file moves nothing); a refused move raises before
        the write, so the old file stays and nothing is written.
        """
        text = self.serialize()
        path = Path(output_path)
        if keep_previous and path.is_file() and path.read_bytes() != text.encode("utf-8"):
            move_previous_arxml(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return str(path)


def previous_arxml_path(output_path: str | PathLike[str]) -> Path:
    """Return where :meth:`ArxmlDocument.save` keeps the previous ARXML: ``<stem>_old<suffix>`` beside it.

    ``scripts/clean_outputs.py`` re-implements this one line (it is stdlib-only) and
    ``scripts/tests/test_dev_build.py`` holds the two equal.
    """
    path = Path(output_path)
    return path.with_name(f"{path.stem}_old{path.suffix}")


def move_previous_arxml(output_path: str | PathLike[str]) -> Path | None:
    """Move the ARXML at *output_path* to :func:`previous_arxml_path`; the new path, or ``None`` when no file is there.

    ``Path.replace``, so an earlier ``_old`` file is replaced; a refused move raises its ``OSError`` with the file
    still under its name. Shared by :meth:`ArxmlDocument.save` and ``swcraft clean-outputs``;
    ``scripts/clean_outputs.py``'s ``move_aside`` re-implements it (stdlib-only).
    """
    path = Path(output_path)
    if not path.is_file():
        return None
    previous = previous_arxml_path(path)
    path.replace(previous)
    return previous
