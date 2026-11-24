"""An independent counter of duplicate declarations and dangling references in an AUTOSAR export.

It parses the file with ``xml.etree.ElementTree`` alone and knows nothing of ``arxml_reader.py``, ``arxml_import/``
or the model contract, so the importer's own counts have an oracle that cannot agree with them by construction.

It answers every named element by its full path (:func:`read_export`); every reference leaf (``*-REF``,
``*-TREF``) with its owner's tag; the references that resolve against no path of the same file, by cause
(:func:`dangling_references`); the ``(layer, kind, SHORT-NAME)`` triples declared more than once and whether the
declarations are equal, by bytes and with references compared resolved (:func:`duplicate_groups`); the groups in
a reference cycle (:attr:`Group.cyclic`); and which ``--packages`` selections have a complete closure over
:data:`CLOSURE_ROLES` (:func:`selection_closure`, :func:`complete_package_selection`).

What it does **not** reach
--------------------------

* Equality compares serialised subtrees, so a difference in insignificant element order reads as a conflict.
* ``ADMIN-DATA``, ``DESC``, ``UUID`` and the element's own ``SHORT-NAME`` are excluded from equality; nothing
  else is.
* A reference resolves against the **same file** only; a delivery's other files are not read.
* A kind outside :data:`LAYER_BY_TAG` is counted under ``None``.
* XML comments, processing instructions, attribute order and namespaces are ignored.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

#: The AUTOSAR element tags the model importer maps onto a SWCraft layer, by
#: the layer it maps them onto. The triple ``(layer, kind, SHORT-NAME)`` is the
#: grouping key of the merge verdict, so a tag absent here is grouped under the
#: layer ``None`` and counted separately -- it can never take the verdict.
LAYER_BY_TAG: dict[str, str] = {
    "SW-BASE-TYPE": "data_types",
    "IMPLEMENTATION-DATA-TYPE": "data_types",
    "APPLICATION-PRIMITIVE-DATA-TYPE": "data_types",
    "APPLICATION-RECORD-DATA-TYPE": "data_types",
    "APPLICATION-ARRAY-DATA-TYPE": "data_types",
    "SENDER-RECEIVER-INTERFACE": "interfaces",
    "CLIENT-SERVER-INTERFACE": "interfaces",
    "APPLICATION-SW-COMPONENT-TYPE": "swc",
    "COMPOSITION-SW-COMPONENT-TYPE": "compositions",
    # The importer writes one `mapping_sets/` element per DATA-TYPE-MAPPING-SET,
    # so two declarations of one set name are judged like the nine above.
    "DATA-TYPE-MAPPING-SET": "mapping_sets",
}

#: Tags whose text is a reference to another element's full AUTOSAR path. The
#: set is derived from the suffix rather than enumerated, because a tool may
#: write roles no release of the standard this repository was read against
#: defines; ``_is_reference`` is the rule.
_REFERENCE_SUFFIXES = ("-REF", "-TREF")

#: Excluded from equality wherever they stand: documentation and identity
#: metadata, not definition.
_EXCLUDED_FROM_EQUALITY = frozenset({"DESC", "ADMIN-DATA", "UUID"})

#: Excluded once, at the top of a declaration: the element's own ``SHORT-NAME``
#: is the grouping key and equal by construction. A **nested** ``SHORT-NAME`` --
#: a record element's, a data element's, an operation's -- is part of the
#: definition and is compared, which is why this is not a member of
#: :data:`_EXCLUDED_FROM_EQUALITY`.
_OWN_IDENTITY = "SHORT-NAME"

#: The cause of a dangling reference, in the wording the report renders.
CAUSE_NO_TREE = "the whole package tree is absent from this export"
CAUSE_LEAF_ONLY = "the parent package is present and the element alone is absent"
CAUSE_CUT_PART_WAY = "the package tree is cut part-way"


def _strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _is_reference(tag: str) -> bool:
    return any(tag.endswith(suffix) for suffix in _REFERENCE_SUFFIXES)


@dataclass(frozen=True)
class Reference:
    """One reference leaf: where it stands, what role it plays, where it points."""

    owner_path: str
    """The full path of the nearest named ancestor -- the element that makes the
    reference. Empty when the reference stands above every ``SHORT-NAME``."""

    owner_tag: str
    """The tag of the element the reference leaf is a direct child of. Together
    with :attr:`tag` this is the *role*: ``TYPE-TREF`` under
    ``VARIABLE-DATA-PROTOTYPE`` is a data element's type, under
    ``SW-COMPONENT-PROTOTYPE`` it is a composition member's type."""

    tag: str
    """The reference leaf's own tag, e.g. ``TYPE-TREF`` or ``BASE-TYPE-REF``."""

    dest: str | None
    """The ``DEST`` attribute, the tag the reference claims its target carries."""

    target: str
    """The text of the reference, verbatim."""

    @property
    def role(self) -> str:
        """``<tag> on <owner tag>``: the role's spelling."""
        return f"{self.tag} on {self.owner_tag}"


@dataclass
class Element:
    """One named element of the export."""

    path: str
    tag: str
    name: str
    node: ET.Element = field(repr=False)

    declaration: bool = False
    """A direct child of an ``<AR-PACKAGE>``'s ``<ELEMENTS>`` -- a packageable
    element, and the only thing the merge verdict groups. A nested
    ``SHORT-NAME`` (a port, a data element, a component prototype) is
    *owner-scoped*: two of them in one export are not two declarations of one
    name. The grandparent is part of the test because ``<ELEMENTS>`` is **not**
    a package-only container: an ``APPLICATION-RECORD-DATA-TYPE``'s record
    elements sit under an ``<ELEMENTS>`` of their own, and counting them would
    turn every repeated field name into a duplicate group."""

    @property
    def layer(self) -> str | None:
        return LAYER_BY_TAG.get(self.tag)


@dataclass
class Export:
    """One parsed export: its path index, its references, and what was dropped."""

    source: Path
    elements: dict[str, Element]
    """Every named element by its full path. A path declared twice keeps the
    **first** declaration here and the second is recorded in
    :attr:`duplicate_paths` -- the reader's own rule, reproduced so the two
    counts are comparable."""

    duplicate_paths: list[str]
    """Full paths declared more than once (the standard's own malformed case)."""

    references: list[Reference]

    @property
    def named_element_count(self) -> int:
        return len(self.elements)

    def resolve(self, target: str) -> Element | None:
        return self.elements.get(target)

    def declarations(self, tag: str) -> list[Element]:
        """Every packageable declaration carrying *tag*, in path order."""
        return sorted(
            (element for element in self.elements.values() if element.declaration and element.tag == tag),
            key=lambda element: element.path,
        )


def child_text(element: Element, tag: str) -> str | None:
    """The text of *element*'s direct child *tag*, or ``None`` when absent.

    Namespaces are stripped, which is why this exists: ``ET``'s own
    ``findtext("CATEGORY")`` answers ``None`` for every element of an export
    that declares the AUTOSAR namespace, and reading that as "no element
    declares a category" is a measurement error, not a measurement.
    """
    for child in element.node:
        if _strip_namespace(child.tag) == tag:
            return (child.text or "").strip()
    return None


def has_descendant(element: Element, tag: str) -> bool:
    """True when *tag* occurs anywhere below *element*, namespace stripped."""

    def walk(node: ET.Element) -> bool:
        return any(_strip_namespace(child.tag) == tag or walk(child) for child in node)

    return walk(element.node)


def read_export(source: str | Path) -> Export:
    """Parse *source* into its path index and its reference list.

    A streaming ``ET.iterparse`` pass would not do: a named element's path is
    built from its ancestors' ``SHORT-NAME`` children, so the walk is recursive
    and holds the tree. On a large tool export that costs a great deal of memory
    and time, which is why every call on a tool export is behind a machine-local
    export gate; the ordinary suites call it on small synthetic files
    (``test_arxml_export_oracle.py``, ``arxml_import_shapes_support.py``).
    """
    source = Path(source)
    root = ET.parse(str(source)).getroot()  # noqa: S314 -- a trusted machine-local export, never network input
    elements: dict[str, Element] = {}
    duplicate_paths: list[str] = []
    references: list[Reference] = []

    def short_name(node: ET.Element) -> str | None:
        for child in node:
            if _strip_namespace(child.tag) == "SHORT-NAME":
                return (child.text or "").strip() or None
        return None

    def walk(node: ET.Element, parent_path: str, owner_path: str, container: str, grandparent: str) -> None:
        tag = _strip_namespace(node.tag)
        name = short_name(node)
        path = owner = owner_path
        if name is not None:
            path = f"{parent_path}/{name}"
            owner = path
            if path in elements:
                duplicate_paths.append(path)
            else:
                elements[path] = Element(
                    path=path,
                    tag=tag,
                    name=name,
                    node=node,
                    declaration=container == "ELEMENTS" and grandparent == "AR-PACKAGE",
                )
        for child in node:
            child_tag = _strip_namespace(child.tag)
            if _is_reference(child_tag):
                text = (child.text or "").strip()
                if text:
                    references.append(
                        Reference(
                            owner_path=owner,
                            owner_tag=tag,
                            tag=child_tag,
                            dest=child.get("DEST"),
                            target=text,
                        )
                    )
            walk(child, path if name is not None else parent_path, owner, tag, container)

    walk(root, "", "", "", "")
    return Export(source=source, elements=elements, duplicate_paths=duplicate_paths, references=references)


# ---------------------------------------------------------------------------
# Dangling references
# ---------------------------------------------------------------------------


def classify_cause(export: Export, target: str) -> str:
    """Why *target* resolves against nothing: the three causes of the report.

    The longest declared prefix of the path decides. Nothing declared, not even
    the root package, is :data:`CAUSE_NO_TREE`; everything but the leaf declared
    is :data:`CAUSE_LEAF_ONLY`; anything between is :data:`CAUSE_CUT_PART_WAY`.
    """
    segments = [segment for segment in target.split("/") if segment]
    longest = 0
    for count in range(1, len(segments)):
        prefix = "/" + "/".join(segments[:count])
        if prefix in export.elements:
            longest = count
    if longest == 0:
        return CAUSE_NO_TREE
    if longest == len(segments) - 1:
        return CAUSE_LEAF_ONLY
    return CAUSE_CUT_PART_WAY


@dataclass
class DanglingSummary:
    """Every unresolved reference of one export, counted every way the report renders."""

    total_references: int
    unresolved: list[Reference]
    by_role: Counter[str]
    """Unresolved count per ``<tag> on <owner tag>`` role."""

    total_by_role: Counter[str]
    """Every reference per role, so a role can be reported *unresolved / total*."""

    by_cause: Counter[str]
    """Distinct unresolved **targets** per cause (a target referenced ten times
    is one entry, which is how the report groups it)."""

    distinct_targets: set[str]
    root_packages: Counter[str]
    """The first path segment of each distinct unresolved target."""

    shadowed: dict[str, list[str]]
    """Unresolved targets whose bare ``SHORT-NAME`` *is* declared elsewhere in
    the file by an element of the same SWCraft layer, by layer. Binding one of
    these by name would bind it to an unrelated element."""

    dest_disagreements: list[Reference]
    """**Resolved** references whose ``DEST`` is not the target's own tag."""


def dangling_references(export: Export) -> DanglingSummary:
    unresolved: list[Reference] = []
    by_role: Counter[str] = Counter()
    total_by_role: Counter[str] = Counter()
    dest_disagreements: list[Reference] = []
    for reference in export.references:
        total_by_role[reference.role] += 1
        target = export.resolve(reference.target)
        if target is None:
            unresolved.append(reference)
            by_role[reference.role] += 1
        elif reference.dest is not None and reference.dest != target.tag:
            dest_disagreements.append(reference)

    distinct = {reference.target for reference in unresolved}
    by_cause: Counter[str] = Counter()
    root_packages: Counter[str] = Counter()
    for target in distinct:
        by_cause[classify_cause(export, target)] += 1
        segments = [segment for segment in target.split("/") if segment]
        if segments:
            root_packages[segments[0]] += 1

    names_by_layer: dict[str, set[str]] = defaultdict(set)
    for element in export.elements.values():
        if element.layer is not None:
            names_by_layer[element.layer].add(element.name)
    layer_of_role = _layer_by_reference_role()
    shadowed: dict[str, list[str]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for reference in unresolved:
        layer = layer_of_role.get(reference.role)
        if layer is None:
            continue
        bare = reference.target.rsplit("/", 1)[-1]
        if bare in names_by_layer.get(layer, set()) and (layer, reference.target) not in seen:
            seen.add((layer, reference.target))
            shadowed[layer].append(reference.target)

    return DanglingSummary(
        total_references=len(export.references),
        unresolved=unresolved,
        by_role=by_role,
        total_by_role=total_by_role,
        by_cause=by_cause,
        distinct_targets=distinct,
        root_packages=root_packages,
        shadowed=dict(shadowed),
        dest_disagreements=dest_disagreements,
    )


def _layer_by_reference_role() -> dict[str, str]:
    """The SWCraft layer a reference's *target* would live in, per role.

    Only the roles the importer reads are named; a role absent here takes no
    part in the shadowing count, because SWCraft never binds it by name.
    """
    return {
        "TYPE-TREF on VARIABLE-DATA-PROTOTYPE": "data_types",
        "TYPE-TREF on ARGUMENT-DATA-PROTOTYPE": "data_types",
        "TYPE-TREF on APPLICATION-RECORD-ELEMENT": "data_types",
        "TYPE-TREF on ELEMENT": "data_types",
        "BASE-TYPE-REF on SW-DATA-DEF-PROPS-CONDITIONAL": "data_types",
        "IMPLEMENTATION-DATA-TYPE-REF on SW-DATA-DEF-PROPS-CONDITIONAL": "data_types",
        "PROVIDED-INTERFACE-TREF on P-PORT-PROTOTYPE": "interfaces",
        "REQUIRED-INTERFACE-TREF on R-PORT-PROTOTYPE": "interfaces",
        "TYPE-TREF on SW-COMPONENT-PROTOTYPE": "swc",
    }


# ---------------------------------------------------------------------------
# Duplicate declarations and the merge verdict
# ---------------------------------------------------------------------------

MERGED = "merged"
CONFLICTING = "conflicting"


@dataclass
class Group:
    """Two or more declarations of one ``(layer, kind, SHORT-NAME)``."""

    layer: str | None
    kind: str
    name: str
    paths: list[str]

    own_bytes_identical: bool
    """Equal comparing each declaration's own serialised subtree, with every
    reference compared as the **text** the export spelled."""

    locally_equal: bool
    """Equal with every reference replaced by its target's group key when the
    target is itself duplicated, and by the target's path otherwise. This is
    the comparison that can be taken before any verdict is known."""

    verdict: str
    """:data:`MERGED` or :data:`CONFLICTING`."""

    cyclic: bool = False
    """The group's verdict depends on itself through a reference cycle. Such a
    group is :data:`CONFLICTING`, fail-closed."""

    inherited_from: str | None = None
    """The key of the conflicting group this group's conflict is inherited from,
    when the group is locally equal and conflicts only for that reason."""

    @property
    def key(self) -> str:
        return f"{self.layer}/{self.kind}/{self.name}"


def _canonical(node: ET.Element, reference_text, *, drop_own_identity: bool = False) -> str:
    """The element's subtree as a comparable string.

    *reference_text* maps a reference leaf's raw target to the token that stands
    for it, which is how the resolved comparison is expressed: hand it the
    identity and references compare as written; hand it a group-key lookup and
    two declarations pointing at two copies of one merged element compare equal.

    *drop_own_identity* removes this element's own ``SHORT-NAME`` child and is
    set only at the top of a declaration.
    """
    tag = _strip_namespace(node.tag)
    if tag in _EXCLUDED_FROM_EQUALITY:
        return ""
    parts = [tag, repr(sorted(node.attrib.items()))]
    text = (node.text or "").strip()
    if _is_reference(tag):
        parts.append(reference_text(text))
    elif text:
        parts.append(text)
    for child in node:
        if drop_own_identity and _strip_namespace(child.tag) == _OWN_IDENTITY:
            continue
        rendered = _canonical(child, reference_text)
        if rendered:
            # An excluded child renders empty and is dropped rather than kept as
            # a blank slot: a declaration that carries a ``DESC`` and one that
            # carries none must compare equal, which is the exclusion's point.
            parts.append(rendered)
    tail = (node.tail or "").strip()
    if tail:
        parts.append(tail)
    return "(" + "|".join(parts) + ")"


def _canonical_root(element: Element, reference_text) -> str:
    """:func:`_canonical` over a declaration, with its own ``SHORT-NAME`` gone."""
    return _canonical(element.node, reference_text, drop_own_identity=True)


def duplicate_groups(export: Export) -> list[Group]:
    """Every ``(layer, kind, SHORT-NAME)`` declared more than once, with its verdict.

    The verdict is a fixpoint: a group is *merged* when every declaration is
    equal with references compared **resolved**, and every group it reaches is
    merged; it is *conflicting* when a property differs, when it reaches a
    conflicting group, or when it sits in a reference cycle.
    """
    by_key: dict[tuple[str | None, str, str], list[Element]] = defaultdict(list)
    for element in export.elements.values():
        if element.declaration:
            by_key[(element.layer, element.tag, element.name)].append(element)
    candidates = {key: members for key, members in by_key.items() if len(members) > 1}

    group_key_by_path: dict[str, tuple[str | None, str, str]] = {}
    for key, members in candidates.items():
        for member in members:
            group_key_by_path[member.path] = key

    def group_of(target: str) -> tuple[tuple[str | None, str, str], str] | None:
        """The duplicated declaration *target* stands in or under, with the rest of the path.

        A reference into a duplicated declaration's **inside** -- a connector
        naming a port of a component that is itself declared twice -- has to
        compare equal between the two copies, so the token is the group's key
        plus the path below it, never the copy's own absolute path.
        """
        segments = [segment for segment in target.split("/") if segment]
        for count in range(len(segments), 0, -1):
            prefix = "/" + "/".join(segments[:count])
            key = group_key_by_path.get(prefix)
            if key is not None:
                return key, "/".join(segments[count:])
        return None

    def resolved_token(target: str) -> str:
        found = group_of(target)
        if found is None:
            return target
        key, below = found
        return f"<group {key[0]}/{key[1]}/{key[2]}>" + (f"/{below}" if below else "")

    groups: dict[tuple[str | None, str, str], Group] = {}
    reaches: dict[tuple[str | None, str, str], set[tuple[str | None, str, str]]] = {}
    for key, members in candidates.items():
        own = {_canonical_root(member, lambda text: text) for member in members}
        resolved = {_canonical_root(member, resolved_token) for member in members}
        reached: set[tuple[str | None, str, str]] = set()
        for member in members:
            for target in _reference_targets(member.node):
                found = group_of(target)
                if found is not None and found[0] != key:
                    reached.add(found[0])
        reaches[key] = reached
        groups[key] = Group(
            layer=key[0],
            kind=key[1],
            name=key[2],
            paths=sorted(member.path for member in members),
            own_bytes_identical=len(own) == 1,
            locally_equal=len(resolved) == 1,
            verdict=CONFLICTING,
        )

    _settle_verdicts(groups, reaches)
    return sorted(groups.values(), key=lambda group: group.key)


def _reference_targets(node: ET.Element):
    tag = _strip_namespace(node.tag)
    if _is_reference(tag):
        text = (node.text or "").strip()
        if text:
            yield text
    for child in node:
        yield from _reference_targets(child)


def _settle_verdicts(groups, reaches) -> None:
    """Least fixpoint: propagate the conflict, then prove *merged*, then name what never settled.

    A group that is not locally equal is conflicting at once, and a group that
    reaches a conflicting group inherits it -- transitively, which is why the
    propagation runs to a fixpoint of its own rather than looking one edge deep.
    A surviving group becomes merged only when every group it reaches is
    **proved** merged, so nothing is merged on an assumption. What is left over
    is exactly a group whose verdict depends on itself through a reference
    cycle, and it is refused (fail-closed): the standard defines no equality of
    definition at all, so a cycle is not an invitation to invent one.
    """
    conflicting = {key for key, group in groups.items() if not group.locally_equal}
    changed = True
    while changed:
        changed = False
        for key in sorted(groups, key=str):
            if key in conflicting:
                continue
            for other in sorted(reaches[key], key=str):
                if other in conflicting:
                    conflicting.add(key)
                    groups[key].inherited_from = groups[other].key
                    changed = True
                    break

    merged: set = set()
    changed = True
    while changed:
        changed = False
        for key in sorted(groups, key=str):
            if key in conflicting or key in merged:
                continue
            if all(other in merged for other in reaches[key]):
                merged.add(key)
                changed = True

    for key, group in groups.items():
        if key in merged:
            group.verdict = MERGED
        else:
            group.verdict = CONFLICTING
            group.cyclic = key not in conflicting


# ---------------------------------------------------------------------------
# A printable summary
# ---------------------------------------------------------------------------


def summarize(export: Export) -> str:
    """The measurement as text: what a log section quotes."""
    dangling = dangling_references(export)
    groups = duplicate_groups(export)
    lines = [
        f"named elements: {export.named_element_count}",
        f"duplicate absolute paths: {len(export.duplicate_paths)}",
        f"references (leaves): {dangling.total_references}",
        f"unresolved: {len(dangling.unresolved)}",
        f"distinct unresolved targets: {len(dangling.distinct_targets)}",
        f"distinct root packages pointed into (unresolved): {len(dangling.root_packages)}",
        f"DEST disagreements (resolved): {len(dangling.dest_disagreements)}",
        "causes (distinct targets):",
    ]
    lines.extend(f"  {cause}: {count}" for cause, count in sorted(dangling.by_cause.items()))
    lines.append("unresolved by role (unresolved / total):")
    for role in sorted(dangling.total_by_role):
        if dangling.by_role.get(role):
            lines.append(f"  {role}: {dangling.by_role[role]} / {dangling.total_by_role[role]}")
    lines.append("shadowed unresolved targets by layer:")
    for layer, targets in sorted(dangling.shadowed.items()):
        lines.append(f"  {layer}: {len(targets)}")
    lines.append(f"duplicate groups: {len(groups)}")
    lines.append(f"  merged: {sum(1 for group in groups if group.verdict == MERGED)}")
    lines.append(f"  conflicting: {sum(1 for group in groups if group.verdict == CONFLICTING)}")
    lines.append(f"  cyclic: {sum(1 for group in groups if group.cyclic)}")
    lines.append(f"  own-bytes identical: {sum(1 for group in groups if group.own_bytes_identical)}")
    lines.append(f"  locally equal (references resolved): {sum(1 for group in groups if group.locally_equal)}")
    for group in groups:
        lines.append(f"  {group.key}: {group.verdict}{' (cyclic)' if group.cyclic else ''} -- {len(group.paths)} paths")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# A --packages selection whose closure is complete
# ---------------------------------------------------------------------------

#: The roles whose unresolved target the importer has to carry into the model:
#: as an unresolved value, which ``check-model`` reports and ``generate arxml``
#: refuses, or -- for a port's interface, a data element's or an argument's type
#: and the other roles of ``unresolved.STUB_ROLE_LAYERS``, when the stub rule
#: admits the target's ``DEST`` -- as an interface or implementation type stub
#: that generates like any other element. The table does not tell the two
#: apart: a selection whose closure reaches one of these is an *open* selection,
#: even where the importer would complete it with a stub; one that reaches none
#: is a **complete** one.
#:
#: ``TYPE-TREF on SW-COMPONENT-PROTOTYPE`` is deliberately absent: an
#: unresolved one mints an ``external`` stub, which carries the path and its
#: ``DEST`` verbatim and is written, generated and validated like any other
#: component. It is a kept element, never a finding.
VALUE_ROLES = frozenset(
    {
        "TYPE-TREF on VARIABLE-DATA-PROTOTYPE",
        "TYPE-TREF on ARGUMENT-DATA-PROTOTYPE",
        "TYPE-TREF on APPLICATION-RECORD-ELEMENT",
        "TYPE-TREF on ELEMENT",
        "BASE-TYPE-REF on SW-DATA-DEF-PROPS-CONDITIONAL",
        "IMPLEMENTATION-DATA-TYPE-REF on SW-DATA-DEF-PROPS-CONDITIONAL",
        "PROVIDED-INTERFACE-TREF on P-PORT-PROTOTYPE",
        "REQUIRED-INTERFACE-TREF on R-PORT-PROTOTYPE",
    }
)

#: The roles the closure **follows**: :data:`VALUE_ROLES` plus the component
#: prototype's type, which is how a selected composition reaches the types it
#: places. It is the part of the reference graph ``--packages`` walks that this
#: oracle models -- a selected composition pulls in the types of its members
#: (transitively through nested compositions), every kept port pulls in its
#: interface, and every interface pulls in the data types its members
#: reference, each of those pulling in its own base. What it omits: the
#: importer also follows a component's ``DATA-TYPE-MAPPING-REF`` and, from a
#: kept application type, the implementation type its map pairs it with
#: (``report.selection_reach``, ``report.closure_data_type_names``), so an
#: unresolved reference reached only that way is not counted here. A
#: connector's endpoint or a runnable's access point is not a route
#: ``--packages`` takes, so following one here would reject a selection the
#: importer narrows cleanly.
CLOSURE_ROLES = VALUE_ROLES | {"TYPE-TREF on SW-COMPONENT-PROTOTYPE"}

#: The component-type tags a ``--packages`` candidate is proposed from. A
#: package is a candidate when it declares one of these; the tags SWCraft does
#: not import are in the set on purpose, so a package holding one is *proposed*
#: and then rejected by its own closure rather than never considered.
_COMPONENT_TAGS = frozenset(
    {
        "APPLICATION-SW-COMPONENT-TYPE",
        "COMPOSITION-SW-COMPONENT-TYPE",
        "SERVICE-SW-COMPONENT-TYPE",
        "COMPLEX-DEVICE-DRIVER-SW-COMPONENT-TYPE",
        "SENSOR-ACTUATOR-SW-COMPONENT-TYPE",
        "ECU-ABSTRACTION-SW-COMPONENT-TYPE",
        "NV-BLOCK-SW-COMPONENT-TYPE",
    }
)


def package_selected(path: str, selection: Iterable[str]) -> bool:
    """``--packages``' own predicate, re-implemented rather than imported.

    A bare package short-name at any nesting depth, or a full package path,
    sticky downward through every descendant; only ancestor **package** prefixes
    are tested, so an element's own leaf never selects it. It is
    ``arxml_import/report.py``'s ``in_selected_package`` written a second time,
    which is this module's whole method: an oracle that called the importer's
    predicate could not see that predicate change.
    """
    wanted = set(selection)
    segments = path.strip("/").split("/")
    prefix = ""
    for segment in segments[:-1]:
        prefix = f"{prefix}/{segment}"
        if segment in wanted or prefix in wanted:
            return True
    return False


@dataclass(frozen=True)
class Closure:
    """What a ``--packages`` selection reaches, and what it fails to resolve."""

    roots: frozenset[str]
    """The declarations the selection names directly."""

    reached: frozenset[str]
    """Every declaration the closure visits, the roots included."""

    unresolved: tuple[Reference, ...]
    """The references of :data:`VALUE_ROLES` inside the closure that resolve
    against nothing in this file. **Empty is the completeness claim.**"""

    @property
    def complete(self) -> bool:
        return not self.unresolved


def _declaration_of(export: Export, path: str) -> str | None:
    """The nearest packageable ancestor of *path*, or *path* itself."""
    segments = [segment for segment in path.split("/") if segment]
    for count in range(len(segments), 0, -1):
        candidate = "/" + "/".join(segments[:count])
        element = export.elements.get(candidate)
        if element is not None and element.declaration:
            return candidate
    return None


def _references_by_declaration(export: Export) -> dict[str, list[Reference]]:
    """Every reference of the file, filed under the declaration that makes it.

    A reference's own owner is the nearest **named** element -- a port, a data
    element, a component prototype -- and the closure walks declarations, so the
    two are joined here once rather than once per root.
    """
    grouped: dict[str, list[Reference]] = defaultdict(list)
    for reference in export.references:
        owner = _declaration_of(export, reference.owner_path)
        if owner is not None:
            grouped[owner].append(reference)
    return grouped


def selection_closure(
    export: Export,
    selection: Iterable[str],
    *,
    grouped: dict[str, list[Reference]] | None = None,
) -> Closure:
    """Walk ``--packages`` *selection*'s dependency closure over *export*.

    *grouped* is :func:`_references_by_declaration`'s answer, passed in when
    many selections are measured against one export so the join is paid once.

    What this does **not** reach: it is a closure over the **export's**
    declarations and reference roles, not a prediction of the importer's own
    narrowing. The importer also drops a composition member whose type fell
    outside the selection, prunes the delegations that named it and skips a
    connector whose endpoint did not survive -- reported losses no finding
    follows from. The claim is the narrower one the gates need: *no element this
    selection keeps makes a reference the importer would have to write into the
    model as an unresolved path.*
    """
    grouped = _references_by_declaration(export) if grouped is None else grouped
    roots = frozenset(
        path for path, element in export.elements.items() if element.declaration and package_selected(path, selection)
    )
    seen: set[str] = set()
    unresolved: list[Reference] = []
    frontier = list(roots)
    while frontier:
        path = frontier.pop()
        if path in seen:
            continue
        seen.add(path)
        for reference in grouped.get(path, ()):
            if reference.role not in CLOSURE_ROLES:
                continue
            if export.resolve(reference.target) is None:
                if reference.role in VALUE_ROLES:
                    unresolved.append(reference)
                continue
            declaration = _declaration_of(export, reference.target)
            if declaration is not None and declaration not in seen:
                frontier.append(declaration)
    return Closure(roots=roots, reached=frozenset(seen), unresolved=tuple(unresolved))


def complete_package_selection(export: Export) -> list[str]:
    """The widest ``--packages`` selection of *export* whose closure is complete.

    Every package that declares a component type is proposed as a candidate and
    kept when its own closure resolves every :data:`VALUE_ROLES` reference; the
    answer is the kept candidates, sorted. The union of complete candidates is
    itself complete -- the closure of a union is the union of the closures, and
    none of them contributes an unresolved reference -- and a caller that wants
    that said rather than argued runs :func:`selection_closure` over the answer.

    What this does **not** reach: a candidate is proposed per **package**, so a
    package holding one unresolvable component beside twenty clean ones is
    dropped whole; a finer selection may exist and is not searched for, because
    ``--packages`` selects packages and the importer offers no per-element
    selection. And completeness is a property of *this file*: a delivery's other
    files are not read, which is exactly the absence being counted.
    """
    grouped = _references_by_declaration(export)
    return [
        candidate
        for candidate in component_packages(export)
        if selection_closure(export, {candidate}, grouped=grouped).complete
    ]


def component_packages(export: Export) -> list[str]:
    """Every package of *export* that declares a component type, sorted.

    The candidate set :func:`complete_package_selection` filters, exposed so a
    caller can record how many candidates a selection was drawn from without
    re-deriving the tag set and disagreeing with it.
    """
    return sorted(
        {
            element.path.rsplit("/", 1)[0]
            for element in export.elements.values()
            if element.declaration and element.tag in _COMPONENT_TAGS
        }
    )
