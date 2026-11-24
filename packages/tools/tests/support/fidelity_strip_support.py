"""Undo the writer-fidelity changes in generated ARXML, one reversal per change class.

The fidelity writer writes a ``DESC`` and a ``LONG-NAME``, a composite member's ``CATEGORY``, a ``LINEAR`` compu
method's ``UNIT-REF``, a bounded value type's ``DATA-CONSTR``, ``HANDLE-OUT-OF-RANGE``/``HANDLE-TIMEOUT-TYPE``, the
four behavioural values (``SW-IMPL-POLICY`` with its queued com-specs, ``SW-CALIBRATION-ACCESS``,
``DISABLED-MODE-IREFS``, a stated ``IS-SERVICE``) and init values in their type's form; it stops writing ``FILTER
ALWAYS`` and ``PORT-API-OPTIONS``, omits ``SERVER-ARGUMENT-IMPL-POLICY`` on an array-typed argument, and gives each
data description layer a package under ``/DataTypes``. :func:`flatten_data_description_packages` is reversed first
and :func:`restore_numerical_values` last.

:func:`strip_fidelity` parses with ``remove_blank_text=True``, applies :data:`REVERSALS` in order, and
re-serializes through :func:`~swcraft_tools.writer.arxml_xml.serialize_document`, the authority on a restored
element's indentation. A byte-identity record compares ``sha256(strip(new output))`` with its recorded digest, so
its claim -- *this change moved no other byte* -- survives without a re-recording.

Each reversal is scoped to what the old writer never wrote, and its docstring names its scope. A shape it cannot
map back raises :class:`FidelityStripError` rather than guessing, so an unforeseen writer change fails the record.

**What it does not reach.** An element written with an empty text re-serializes in the two-line form, so a tree
holding one fails the identity test (no committed tree does). The harness says which bytes moved; it never says
the new bytes are right, and a golden stays a change detector, never an oracle.
"""

from __future__ import annotations

from collections.abc import Callable

from lxml import etree
from swcraft_tools.writer.arxml_xml import INDENT, ar_qname, serialize_document

#: The parser the harness reads with: blank text between elements is dropped, so the writer's
#: serializer re-indents every element -- restored ones included -- exactly as it indents its own.
PARSER = etree.XMLParser(remove_blank_text=True)


class FidelityStripError(AssertionError):
    """A reversal met a shape it cannot map back to the old writer's output."""


def _q(tag: str) -> str:
    return ar_qname(tag)


def _local(element: etree._Element) -> str:
    return etree.QName(element).localname


def parse(data: bytes) -> etree._Element:
    """Parse a generated document the way the harness reads it."""
    return etree.fromstring(data, PARSER)


def serialize(root: etree._Element) -> bytes:
    """Serialize *root* through the writer's own serializer, as the writer's file holds it."""
    return serialize_document(root).encode("utf-8")


def _remove(element: etree._Element) -> None:
    parent = element.getparent()
    assert parent is not None
    parent.remove(element)


def _remove_emptied(element: etree._Element | None, *stop_tags: str) -> None:
    """Remove *element* and each ancestor left without a child, up to (not past) a ``stop_tags`` element."""
    stop = {_q(tag) for tag in stop_tags}
    while element is not None and element.tag not in stop and len(element) == 0:
        parent = element.getparent()
        if parent is None:
            return
        parent.remove(element)
        element = parent


def _short_name(element: etree._Element) -> str | None:
    node = element.find(_q("SHORT-NAME"))
    return None if node is None else node.text


def _resolve(root: etree._Element, path: str) -> etree._Element | None:
    """Return the element *path* (``/Pkg/Sub/Name``) names, walking ``SHORT-NAME``s from the root packages."""
    segments = [segment for segment in path.split("/") if segment]
    candidates: list[etree._Element] = list(root.iterfind(f"{_q('AR-PACKAGES')}/{_q('AR-PACKAGE')}"))
    found: etree._Element | None = None
    for segment in segments:
        found = next((node for node in candidates if _short_name(node) == segment), None)
        if found is None:
            return None
        candidates = [
            child
            for holder in (found.find(_q("AR-PACKAGES")), found.find(_q("ELEMENTS")))
            if holder is not None
            for child in holder
        ]
    return found


# ---------------------------------------------------------------------------
# The reversals, one per change class. Each mutates *root* and returns how many places it changed.
# ---------------------------------------------------------------------------


def remove_descriptions(root: etree._Element) -> int:
    """Remove every ``DESC`` and every ``LONG-NAME``."""
    found = [node for tag in ("DESC", "LONG-NAME") for node in root.iter(_q(tag))]
    for node in found:
        _remove(node)
    return len(found)


def restore_member_categories(root: etree._Element) -> int:
    """Set a composite member's ``CATEGORY`` back to ``VALUE``.

    A composite member is an ``APPLICATION-RECORD-ELEMENT`` or an ``APPLICATION-ARRAY-DATA-TYPE``'s
    ``ELEMENT``; the old writer wrote ``VALUE`` on each.
    """
    changed = 0
    members = [
        *root.iter(_q("APPLICATION-RECORD-ELEMENT")),
        *root.iterfind(f".//{_q('APPLICATION-ARRAY-DATA-TYPE')}/{_q('ELEMENT')}"),
    ]
    for member in members:
        category = member.find(_q("CATEGORY"))
        if category is None:
            raise FidelityStripError(f"composite member {_short_name(member)} carries no CATEGORY")
        if category.text != "VALUE":
            category.text = "VALUE"
            changed += 1
    return changed


def remove_compu_method_units(root: etree._Element) -> int:
    """Remove a ``COMPU-METHOD``'s own ``UNIT-REF``."""
    found = list(root.iterfind(f".//{_q('COMPU-METHOD')}/{_q('UNIT-REF')}"))
    for node in found:
        _remove(node)
    return len(found)


def remove_value_type_ranges(root: etree._Element) -> int:
    """Remove a value type's ``DATA-CONSTR-REF`` and the ``{name}_Range`` it names.

    A value type is an ``IMPLEMENTATION-DATA-TYPE``; the old writer gave none a ``DATA-CONSTR-REF``.
    The referenced ``DATA-CONSTR`` must be that type's own ``{name}_Range``; an ``ELEMENTS`` left
    empty is removed with it (the writer never writes an empty one), its package kept.
    """
    conditional_path = "/".join(
        _q(tag) for tag in ("SW-DATA-DEF-PROPS", "SW-DATA-DEF-PROPS-VARIANTS", "SW-DATA-DEF-PROPS-CONDITIONAL")
    )
    changed = 0
    for impl in list(root.iter(_q("IMPLEMENTATION-DATA-TYPE"))):
        for conditional in impl.iterfind(conditional_path):
            ref = conditional.find(_q("DATA-CONSTR-REF"))
            if ref is None:
                continue
            name = _short_name(impl)
            target = _resolve(root, ref.text or "")
            if target is None:
                raise FidelityStripError(f"{name}'s DATA-CONSTR-REF names nothing: {ref.text}")
            if _local(target) != "DATA-CONSTR" or _short_name(target) != f"{name}_Range":
                raise FidelityStripError(f"{name}'s DATA-CONSTR-REF names {ref.text}, not its own {name}_Range")
            _remove(ref)
            holder = target.getparent()
            _remove(target)
            _remove_emptied(holder, "AR-PACKAGE")
            changed += 1
    return changed


def restore_filters(root: etree._Element) -> int:
    """Put ``FILTER ALWAYS`` back on every non-queued receiver com-spec without one.

    The old writer wrote it directly after ``ENABLE-UPDATE``, the schema's position.
    """
    changed = 0
    for spec in root.iter(_q("NONQUEUED-RECEIVER-COM-SPEC")):
        if spec.find(_q("FILTER")) is not None:
            continue
        anchor = spec.find(_q("ENABLE-UPDATE"))
        if anchor is None:
            raise FidelityStripError("a NONQUEUED-RECEIVER-COM-SPEC without FILTER carries no ENABLE-UPDATE")
        node = etree.Element(_q("FILTER"))
        etree.SubElement(node, _q("DATA-FILTER-TYPE")).text = "ALWAYS"
        anchor.addnext(node)
        changed += 1
    return changed


def remove_range_and_timeout_handling(root: etree._Element) -> int:
    """Remove a com-spec's ``HANDLE-OUT-OF-RANGE`` and ``HANDLE-TIMEOUT-TYPE``."""
    found = [
        node
        for tag in ("HANDLE-OUT-OF-RANGE", "HANDLE-TIMEOUT-TYPE")
        for node in root.iter(_q(tag))
        if _local(node.getparent()).endswith("-COM-SPEC")
    ]
    for node in found:
        _remove(node)
    return len(found)


def restore_argument_policies(root: etree._Element) -> int:
    """Put ``SERVER-ARGUMENT-IMPL-POLICY USE-ARGUMENT-TYPE`` back on every argument without one.

    The old writer wrote it on every argument, directly after ``DIRECTION``.
    """
    changed = 0
    for argument in root.iter(_q("ARGUMENT-DATA-PROTOTYPE")):
        if argument.find(_q("SERVER-ARGUMENT-IMPL-POLICY")) is not None:
            continue
        anchor = argument.find(_q("DIRECTION"))
        if anchor is None:
            raise FidelityStripError(f"argument {_short_name(argument)} carries no DIRECTION")
        node = etree.Element(_q("SERVER-ARGUMENT-IMPL-POLICY"))
        node.text = "USE-ARGUMENT-TYPE"
        anchor.addnext(node)
        changed += 1
    return changed


def remove_impl_policies(root: etree._Element) -> int:
    """Remove every ``SW-IMPL-POLICY``, and a props chain it leaves empty."""
    found = list(root.iter(_q("SW-IMPL-POLICY")))
    for node in found:
        parent = node.getparent()
        _remove(node)
        _remove_emptied(parent)
    return len(found)


def remove_data_element_calibration_access(root: etree._Element) -> int:
    """Remove a **data element's** ``SW-CALIBRATION-ACCESS``, and a chain left empty.

    A data element is a ``VARIABLE-DATA-PROTOTYPE`` of a ``SENDER-RECEIVER-INTERFACE``'s
    ``DATA-ELEMENTS``. A value type's and an argument's ``NOT-ACCESSIBLE`` are the old writer's own
    and stay.
    """
    path = "/".join(
        _q(tag)
        for tag in (
            "DATA-ELEMENTS",
            "VARIABLE-DATA-PROTOTYPE",
            "SW-DATA-DEF-PROPS",
            "SW-DATA-DEF-PROPS-VARIANTS",
            "SW-DATA-DEF-PROPS-CONDITIONAL",
            "SW-CALIBRATION-ACCESS",
        )
    )
    found = list(root.iterfind(f".//{_q('SENDER-RECEIVER-INTERFACE')}/{path}"))
    for node in found:
        parent = node.getparent()
        _remove(node)
        _remove_emptied(parent)
    return len(found)


def remove_disabled_modes(root: etree._Element) -> int:
    """Remove every ``DISABLED-MODE-IREFS``."""
    found = list(root.iter(_q("DISABLED-MODE-IREFS")))
    for node in found:
        _remove(node)
    return len(found)


def remove_queued_com_specs(root: etree._Element) -> int:
    """Remove every queued sender and receiver com-spec, and a wrapper left empty."""
    found = [node for tag in ("QUEUED-SENDER-COM-SPEC", "QUEUED-RECEIVER-COM-SPEC") for node in root.iter(_q(tag))]
    for node in found:
        parent = node.getparent()
        _remove(node)
        _remove_emptied(parent, "P-PORT-PROTOTYPE", "R-PORT-PROTOTYPE")
    return len(found)


def restore_is_service(root: etree._Element) -> int:
    """Set every interface's ``IS-SERVICE`` back to ``false``."""
    changed = 0
    for interface in ("SENDER-RECEIVER-INTERFACE", "CLIENT-SERVER-INTERFACE"):
        for node in root.iterfind(f".//{_q(interface)}/{_q('IS-SERVICE')}"):
            if node.text != "false":
                node.text = "false"
                changed += 1
    return changed


#: The port kinds the old writer wrote, each of which it gave one ``PORT-API-OPTION``.
_OPTIONED_PORTS = ("P-PORT-PROTOTYPE", "R-PORT-PROTOTYPE")


def _path_of(element: etree._Element) -> str:
    """Return the absolute path of *element*: every ``SHORT-NAME`` from the root package down to it."""
    names = [name for node in (element, *element.iterancestors()) if (name := _short_name(node)) is not None]
    return "/" + "/".join(reversed(names))


def restore_port_api_options(root: etree._Element) -> int:
    """Put back the ``PORT-API-OPTIONS`` the old writer wrote for every component port.

    The model states no port API option, so the writer writes none. The old writer
    (``_emit_port_api_options``) wrote the container in every ``SWC-INTERNAL-BEHAVIOR`` whose
    component has a port: one ``PORT-API-OPTION`` per port, sorted by port name case-insensitively
    and stably over the order ``PORTS`` holds them in, each ``ENABLE-TAKE-ADDRESS false``,
    ``INDIRECT-API false`` and a ``PORT-REF`` to the port with its kind as ``DEST`` -- after
    ``HANDLE-TERMINATION-AND-RESTART`` and before ``RUNNABLES``, or before
    ``SUPPORTS-MULTIPLE-INSTANTIATION`` when there are no runnables.

    A behaviour that holds a container is left alone, whatever it holds, so on a document the old
    writer produced this reversal changes nothing; so is one no component holds, which has no port.
    A port of a kind the old writer never wrote, and a behaviour with neither anchor, are refused.
    """
    changed = 0
    for behavior in root.iter(_q("SWC-INTERNAL-BEHAVIOR")):
        if behavior.find(_q("PORT-API-OPTIONS")) is not None:
            continue
        holder = behavior.getparent()
        component = None if holder is None else holder.getparent()
        if holder is None or _local(holder) != "INTERNAL-BEHAVIORS" or component is None:
            continue  # no component, so no port to restore an option for
        ports_node = component.find(_q("PORTS"))
        ports = [] if ports_node is None else list(ports_node)
        for port in ports:
            if _local(port) not in _OPTIONED_PORTS:
                raise FidelityStripError(
                    f"{_short_name(component)} holds a {_local(port)}, which the old writer never wrote"
                )
        if not ports:
            continue
        anchor = behavior.find(_q("RUNNABLES"))
        if anchor is None:
            anchor = behavior.find(_q("SUPPORTS-MULTIPLE-INSTANTIATION"))
        if anchor is None:
            raise FidelityStripError(
                f"behaviour {_short_name(behavior)} holds neither RUNNABLES nor SUPPORTS-MULTIPLE-INSTANTIATION"
            )
        component_path = _path_of(component)
        options = etree.Element(_q("PORT-API-OPTIONS"))
        for port in sorted(ports, key=lambda node: (_short_name(node) or "").lower()):
            option = etree.SubElement(options, _q("PORT-API-OPTION"))
            etree.SubElement(option, _q("ENABLE-TAKE-ADDRESS")).text = "false"
            etree.SubElement(option, _q("INDIRECT-API")).text = "false"
            reference = etree.SubElement(option, _q("PORT-REF"), DEST=_local(port))
            reference.text = f"{component_path}/{_short_name(port)}"
        anchor.addprevious(options)
        changed += 1
    return changed


#: The root package the data description's layer packages sit in.
_DATA_TYPES = "DataTypes"

#: The layer packages under ``/DataTypes`` this reversal maps back, **in the old writer's pass
#: order**: base types, then the implementation pass (each value type preceded by its own
#: ``{name}_Range``), then each application type with its private forms and the composites, then
#: the mapping sets.
_FLATTENED_LAYER_PACKAGES = ("Base", "Implementation", "Application", "MappingSets")

#: The layer packages that hold **declared** units, compu methods and data constraints -- elements
#: the old writer's output has no place for. Refused.
_REFUSED_LAYER_PACKAGES = ("Units", "CompuMethods", "Constraints")


def _top_level_package(root: etree._Element, name: str) -> etree._Element | None:
    return next(
        (node for node in root.iterfind(f"{_q('AR-PACKAGES')}/{_q('AR-PACKAGE')}") if _short_name(node) == name),
        None,
    )


def flatten_data_description_packages(root: etree._Element) -> int:
    """Move the elements of ``/DataTypes``' layer packages back into ``/DataTypes`` itself.

    The old writer's pass order is the layer packages concatenated in
    :data:`_FLATTENED_LAYER_PACKAGES`' order, so nothing is re-sorted. The moved elements form
    ``/DataTypes``' ``ELEMENTS`` after its ``SHORT-NAME``; each emptied layer package goes, and so
    does an ``AR-PACKAGES`` it leaves empty. Every ``DEST``-carrying element's
    ``/DataTypes/{LayerPackage}/…`` text is rewritten to ``/DataTypes/…``. Without layer packages it
    changes nothing. Returns the number of elements moved (rewritten references not counted).

    Refused (:class:`FidelityStripError`), as shapes it cannot map back: a ``Units``,
    ``CompuMethods`` or ``Constraints`` package (declared elements, which the old writer had no place
    for); a sub-package inside a layer package (an element with a ``package``); a ``/DataTypes``
    sub-package it does not know; two ``/DataTypes`` sub-packages of one name; ``/DataTypes``
    holding elements of its own beside a layer package (no pass order places the two against each
    other); two moved elements of one ``SHORT-NAME`` (a same-name pair of two layers, which the old
    writer's one ``/DataTypes`` could not hold and whose references the rewrite would make
    indistinguishable; the comparison is exact, so a case-only pair is moved); and a
    ``/DataTypes/{LayerPackage}/`` text in an element with no ``DEST``, or naming one of the three
    refused packages.
    """
    data_types = _top_level_package(root, _DATA_TYPES)
    moved: list[etree._Element] = []
    if data_types is not None:
        holder = data_types.find(_q("AR-PACKAGES"))
        names = [] if holder is None else [_short_name(node) for node in holder]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise FidelityStripError(f"/DataTypes holds two packages named {repeated[0]}")
        packages = {} if holder is None else {_short_name(node): node for node in holder}
        for name, package in packages.items():
            if name in _REFUSED_LAYER_PACKAGES:
                raise FidelityStripError(f"/DataTypes/{name} holds declared elements the old writer had no place for")
            if name not in _FLATTENED_LAYER_PACKAGES:
                raise FidelityStripError(f"/DataTypes/{name} is not a layer package")
            if package.find(_q("AR-PACKAGES")) is not None:
                raise FidelityStripError(f"/DataTypes/{name} holds a sub-package")
        if packages and data_types.find(_q("ELEMENTS")) is not None:
            raise FidelityStripError("/DataTypes holds elements of its own beside its layer packages")
        for name in _FLATTENED_LAYER_PACKAGES:
            package = packages.get(name)
            if package is None:
                continue
            elements = package.find(_q("ELEMENTS"))
            if elements is not None:
                for element in elements:
                    _forget_empty_element_indentation(element)
                moved.extend(elements)
            _remove(package)
        if holder is not None and len(holder) == 0:
            _remove(holder)
        moved_names = [_short_name(element) for element in moved]
        colliding = sorted({name for name in moved_names if moved_names.count(name) > 1})
        if colliding:
            raise FidelityStripError(
                f"two moved elements are both named {colliding[0]}: the old /DataTypes could not hold both"
            )
        if moved:
            target = etree.Element(_q("ELEMENTS"))
            target.extend(moved)
            data_types.find(_q("SHORT-NAME")).addnext(target)
    _rewrite_layer_package_paths(root)
    return len(moved)


def _depth(element: etree._Element) -> int:
    depth = 0
    parent = element.getparent()
    while parent is not None:
        depth += 1
        parent = parent.getparent()
    return depth


def _forget_empty_element_indentation(element: etree._Element) -> None:
    """Drop the closing-tag indentation the writer's serializer gave each empty element under *element*.

    The serializer writes a childless, textless element in two lines, its text one newline plus
    the indentation **of its depth** (``arxml_xml._expand_empty_elements``); the parser keeps that
    text, so a moved element would carry the old depth. Called on moved elements before the move,
    it drops only text that is **exactly** that expansion at the present depth; any other text is
    kept, so the byte comparison still fails on it.

    **What it does not reach:** it cannot tell a writer that changed the indentation of an empty
    moved element from one that did not.
    """
    for node in element.iter(etree.Element):
        if len(node) == 0 and node.text == "\n" + INDENT * _depth(node):
            node.text = None


def _rewrite_layer_package_paths(root: etree._Element) -> None:
    """Rewrite ``/DataTypes/{LayerPackage}/…`` to ``/DataTypes/…`` in every ``DEST``-carrying element's text."""
    flattened = tuple(f"/{_DATA_TYPES}/{name}/" for name in _FLATTENED_LAYER_PACKAGES)
    refused = tuple(f"/{_DATA_TYPES}/{name}/" for name in _REFUSED_LAYER_PACKAGES)
    for node in root.iter(etree.Element):
        text = node.text
        if not text or f"/{_DATA_TYPES}/" not in text:
            continue
        if any(prefix in text for prefix in refused):
            raise FidelityStripError(f"{_local(node)} names a package the old writer had no place for: {text}")
        prefix = next((prefix for prefix in flattened if prefix in text), None)
        if prefix is None:
            continue
        if node.get("DEST") is None or not text.startswith(prefix):
            raise FidelityStripError(f"{_local(node)} spells a layer package path outside a reference: {text}")
        node.text = f"/{_DATA_TYPES}/" + text[len(prefix) :]


#: The value specification tags an application value is replaced by (the old writer's leaf) and refused as.
_APPLICATION_VALUE = "APPLICATION-VALUE-SPECIFICATION"
_TEXT_VALUE = "TEXT-VALUE-SPECIFICATION"


def _scale_limits(root: etree._Element) -> dict[str, set[str]]:
    """Map every ``TEXTTABLE`` scale's ``COMPU-CONST/VT`` text to the ``LOWER-LIMIT`` texts of the scales holding it."""
    limits: dict[str, set[str]] = {}
    for method in root.iter(_q("COMPU-METHOD")):
        if method.findtext(_q("CATEGORY")) != "TEXTTABLE":
            continue
        for scale in method.iterfind(f".//{_q('COMPU-SCALE')}"):
            text = scale.findtext(f"{_q('COMPU-CONST')}/{_q('VT')}")
            if text is not None:
                limits.setdefault(text, set()).add(scale.findtext(_q("LOWER-LIMIT")) or "")
    return limits


def _application_leaf(value: etree._Element) -> tuple[etree._Element | None, etree._Element]:
    """Return the ``SHORT-LABEL`` and the one ``V`` or ``VT`` of an application value of the writer's shape.

    The shape is ``SHORT-LABEL``? ``CATEGORY`` (``VALUE``) ``SW-VALUE-CONT``, the content
    ``UNIT-REF``? ``SW-VALUES-PHYS``, and the physical values exactly one ``V`` or ``VT`` with a
    text; anything else is refused.
    """
    children = list(value)
    label = children.pop(0) if children and _local(children[0]) == "SHORT-LABEL" else None
    if [_local(child) for child in children] != ["CATEGORY", "SW-VALUE-CONT"]:
        found = ", ".join(_local(child) for child in value)
        raise FidelityStripError(f"an {_APPLICATION_VALUE} of a shape the writer does not write: {found}")
    category = children[0].text
    if category != "VALUE":
        raise FidelityStripError(f"an {_APPLICATION_VALUE} of CATEGORY {category}, not VALUE")
    content = [_local(child) for child in children[1]]
    if content not in (["SW-VALUES-PHYS"], ["UNIT-REF", "SW-VALUES-PHYS"]):
        raise FidelityStripError(f"an {_APPLICATION_VALUE}'s SW-VALUE-CONT holds {', '.join(content)}")
    physical = children[1][-1]
    if len(physical) != 1 or _local(physical[0]) not in ("V", "VT"):
        found = ", ".join(_local(child) for child in physical) or "nothing"
        raise FidelityStripError(f"an {_APPLICATION_VALUE}'s SW-VALUES-PHYS holds {found}, not one V or VT")
    leaf = physical[0]
    if not (leaf.text or "").strip():
        raise FidelityStripError(f"an {_APPLICATION_VALUE}'s {_local(leaf)} is empty")
    return label, leaf


def restore_numerical_values(root: etree._Element) -> int:
    """Map every value in the application form back to the ``NUMERICAL-VALUE-SPECIFICATION`` the old writer wrote.

    The old writer (``_emit_value_specification``) wrote every scalar leaf of an init value as a
    ``NUMERICAL-VALUE-SPECIFICATION``: an optional ``SHORT-LABEL``, then a ``VALUE`` holding
    ``format_init_number``'s text. So an ``APPLICATION-VALUE-SPECIFICATION`` of
    :func:`_application_leaf`'s shape becomes that element in place: ``SHORT-LABEL`` kept,
    ``CATEGORY`` and ``UNIT-REF`` dropped, ``VALUE`` the ``V``'s text, or for a ``VT`` the
    ``LOWER-LIMIT`` of the ``TEXTTABLE`` scale whose ``COMPU-CONST/VT`` is that text (the
    enumeration literal's declared value). Returns the number of values mapped back.

    Refused (:class:`FidelityStripError`), as shapes it cannot map back: a ``TEXT-VALUE-SPECIFICATION``
    (the old writer wrote none, and a table-typed implementation type's init did not exist); an
    application value of any other shape -- a ``CATEGORY`` other than ``VALUE`` or none, a
    ``SW-AXIS-CONTS``, a ``SW-ARRAYSIZE``, a ``UNIT-DISPLAY-NAME``, a label out of place, physical
    values other than exactly one ``V`` or ``VT``, an empty one; and a ``VT`` that no scale holds or
    that scales of different limits hold.

    **What it does not reach.** The lookup is document-wide, because a constant names no type: two
    tables that give one text different limits make it raise, though the leaf's own type would have
    decided. On a document with no application value it changes nothing.
    """
    text_values = list(root.iter(_q(_TEXT_VALUE)))
    if text_values:
        raise FidelityStripError(f"a {_TEXT_VALUE}, which the old writer never wrote")
    values = list(root.iter(_q(_APPLICATION_VALUE)))
    limits = _scale_limits(root) if values else {}
    for value in values:
        label, leaf = _application_leaf(value)
        text = leaf.text or ""
        if _local(leaf) == "VT":
            found = limits.get(text)
            if not found:
                raise FidelityStripError(f"the VT {text} names no scale of a TEXTTABLE")
            if len(found) != 1:
                raise FidelityStripError(f"the VT {text} is held by scales of different limits")
            text = next(iter(found))
        numerical = etree.Element(_q("NUMERICAL-VALUE-SPECIFICATION"))
        if label is not None:
            numerical.append(label)
        etree.SubElement(numerical, _q("VALUE")).text = text
        parent = value.getparent()
        assert parent is not None
        parent.replace(value, numerical)
    return len(values)


#: Every reversal, in the order :func:`strip_fidelity` applies them: ``data-description-packages``
#: **first**, so every other reversal sees the ``/DataTypes`` layout it was written for, and
#: ``value-forms`` last.
REVERSALS: tuple[tuple[str, Callable[[etree._Element], int]], ...] = (
    ("data-description-packages", flatten_data_description_packages),
    ("descriptions", remove_descriptions),
    ("member-categories", restore_member_categories),
    ("compu-method-units", remove_compu_method_units),
    ("value-type-ranges", remove_value_type_ranges),
    ("filters", restore_filters),
    ("range-and-timeout-handling", remove_range_and_timeout_handling),
    ("argument-policies", restore_argument_policies),
    ("impl-policies", remove_impl_policies),
    ("data-element-calibration-access", remove_data_element_calibration_access),
    ("disabled-modes", remove_disabled_modes),
    ("queued-com-specs", remove_queued_com_specs),
    ("is-service", restore_is_service),
    ("port-api-options", restore_port_api_options),
    ("value-forms", restore_numerical_values),
)


def strip_counts(data: bytes) -> tuple[bytes, dict[str, int]]:
    """Return :func:`strip_fidelity`'s bytes, plus how many places each reversal changed."""
    root = parse(data)
    counts = {name: reversal(root) for name, reversal in REVERSALS}
    return serialize(root), counts


def strip_fidelity(data: bytes) -> bytes:
    """Return the old writer's bytes for a document the writer-fidelity writer produced."""
    return strip_counts(data)[0]
