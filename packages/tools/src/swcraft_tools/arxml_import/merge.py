"""Duplicate declarations: two declarations of one name are one element, or none.

A group of declarations sharing one ``(layer, kind, SHORT-NAME)`` is

* **merged** -- one element, registered under the declared name, with every
  member path mapped onto it -- when nothing SWCraft can read distinguishes
  the members; or
* **conflicting** -- *nothing* imported, the name still claimed -- when
  something does, when a group it references conflicts, or when its verdict
  would depend on itself through a reference cycle; or
* **selected** -- one member imported under the declared name and every other
  excluded -- when a ``--packages`` selection keeps exactly one member of a
  conflicting group (:func:`narrow_to_selection` alone).

The relation is SWCraft's own policy, not the standard's, and fails closed. The
verdict itself is :mod:`~swcraft_tools.arxml_import.merge_verdict`'s; this module
holds the groups, the chains and the readers they use, and depends on no other
``arxml_import`` submodule.

The comparison sees exactly what :mod:`swcraft_tools.arxml_reader` extracts: two
declarations differing only in a property the reader does not read merge. A
description, a composition's ``LONG-NAME`` and the whitespace around a base
type's encoding or native declaration are not compared (the stripped values
are, and differing ones conflict): the primary's is kept and another member's
is recorded on the :class:`MergeGroup` and reported. Lists are compared in
declaration order, and the verdict is taken over the export alone, before
mapping and whatever ``--packages`` keeps.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Final, Literal, NamedTuple, TypedDict

from swcraft_tools.arxml_reader import (
    DATA_ELEMENT_COM_SPEC_KINDS,
    OTHER_LIMIT,
    AppPrimitiveDataTypeInfo,
    CompuMethodInfo,
    ComSpecInfo,
    DataConstrInfo,
    DataElementComSpecInfo,
    ImportModel,
    SkipCategory,
    SkipEntry,
    SkipLog,
    SkipReason,
    UnitInfo,
    exact_decimal,
    is_infinite_limit,
    not_supported,
    parse_exact_number,
    parse_float_literal,
    parse_int_literal,
    parse_xsd_boolean,
    phys_limits,
    stripped_spelling,
)
from swcraft_tools.arxml_version import DATA_DESCRIPTION_PACKAGE_NAMES, ROOT_PACKAGE_NAMES
from swcraft_tools.writer.arxml_generation import (
    COM_SPEC_SILENT_VALUES,
    COMPU_METHOD_SUFFIX,
    COMPU_SUFFIX,
    RANGE_SUFFIX,
    UNIT_SUFFIX,
)

#: The verdict of a group whose members are one element.
MERGED = "merged"
#: The verdict of a group nothing of which is imported.
CONFLICTING = "conflicting"
#: The verdict of a conflicting group a ``--packages`` selection reduced to one
#: member: that member is imported under the declared name and the group's
#: conflict line is suppressed. Minted by :func:`narrow_to_selection` alone.
SELECTED = "selected"

#: The SWCraft layers a group is keyed by. Each has a flat identity namespace,
#: which makes *two declarations want one name* one question in all of them.
LAYER_APPLICATION_TYPES = "application_types"
LAYER_IMPLEMENTATION_TYPES = "implementation_types"
LAYER_BASE_TYPES = "base_types"
LAYER_INTERFACES = "interfaces"
LAYER_SWC = "swc"
LAYER_COMPOSITIONS = "compositions"
LAYER_CONNECTORS = "connectors"
LAYER_MAPPING_SETS = "mapping_sets"
#: The three layers of the forms a type names by reference: ``UNIT``, ``COMPU-METHOD``, ``DATA-CONSTR``.
LAYER_UNITS = "units"
LAYER_COMPU_METHODS = "compu_methods"
LAYER_DATA_CONSTRAINTS = "data_constraints"

#: Every declaration kind that takes a verdict, onto its SWCraft layer.
#:
#: The **kind is the ARXML declaration tag**, not the SWCraft kind:
#: ``value_type`` and ``reference_type`` are both ``IMPLEMENTATION-DATA-TYPE``,
#: so two implementation types of one name are one group whose ``CATEGORY`` is
#: compared (likewise a ``texttable`` and a ``linear`` ``COMPU-METHOD``). Groups
#: never span kinds.
#:
#: ``ASSEMBLY-SW-CONNECTOR`` is keyed more widely than the standard scopes the
#: name: a connector name is unique per composition, but SWCraft's
#: ``connectors/`` layer is flat, so the key has no composition and the row's
#: first property is the owning composition. Two compositions declaring one
#: connector name therefore conflict, and both connectors are dropped, with
#: ``composition`` named as the differing property -- importing either would be
#: a guess about which connector the project means.
LAYER_BY_TAG: dict[str, str] = {
    "SW-BASE-TYPE": LAYER_BASE_TYPES,
    "IMPLEMENTATION-DATA-TYPE": LAYER_IMPLEMENTATION_TYPES,
    "APPLICATION-PRIMITIVE-DATA-TYPE": LAYER_APPLICATION_TYPES,
    "APPLICATION-RECORD-DATA-TYPE": LAYER_APPLICATION_TYPES,
    "APPLICATION-ARRAY-DATA-TYPE": LAYER_APPLICATION_TYPES,
    "SENDER-RECEIVER-INTERFACE": LAYER_INTERFACES,
    "CLIENT-SERVER-INTERFACE": LAYER_INTERFACES,
    "APPLICATION-SW-COMPONENT-TYPE": LAYER_SWC,
    "COMPOSITION-SW-COMPONENT-TYPE": LAYER_COMPOSITIONS,
    "ASSEMBLY-SW-CONNECTOR": LAYER_CONNECTORS,
    "DATA-TYPE-MAPPING-SET": LAYER_MAPPING_SETS,
    "UNIT": LAYER_UNITS,
    "COMPU-METHOD": LAYER_COMPU_METHODS,
    "DATA-CONSTR": LAYER_DATA_CONSTRAINTS,
}

#: The three layers of the forms a type names by reference, in the contract's order.
ELEMENT_LAYERS: tuple[str, ...] = (LAYER_UNITS, LAYER_COMPU_METHODS, LAYER_DATA_CONSTRAINTS)

#: For each of :data:`ELEMENT_LAYERS`, the tag its element is written under, and the noun a
#: report line or a refusal names one by.
ELEMENT_TAG_BY_LAYER: dict[str, str] = {
    LAYER_UNITS: "UNIT",
    LAYER_COMPU_METHODS: "COMPU-METHOD",
    LAYER_DATA_CONSTRAINTS: "DATA-CONSTR",
}
ELEMENT_NOUN_BY_LAYER: dict[str, str] = {
    LAYER_UNITS: "unit",
    LAYER_COMPU_METHODS: "compu method",
    LAYER_DATA_CONSTRAINTS: "data constraint",
}


@dataclass(frozen=True, order=True)
class GroupKey:
    """One ``(layer, kind, SHORT-NAME)`` the export declares more than once."""

    layer: str
    kind: str
    name: str

    def __str__(self) -> str:
        return f"{self.layer}/{self.kind}/{self.name}"


@dataclass(frozen=True)
class MergeGroup:
    """The verdict on one group, with everything a report line needs.

    ``paths`` is in **source-path order**, which is the order the importer
    writes each layer in, and ``primary`` is its first element: the member whose
    content -- its ``description`` above all -- the merged element keeps.
    """

    key: GroupKey
    paths: tuple[str, ...]
    verdict: str
    #: The first property on which a member differs from the primary -- ``None``
    #: unless :attr:`verdict` is :data:`CONFLICTING` *and* the difference is the
    #: group's own.
    difference: str | None = None
    #: The group whose conflict this one inherits: its own properties agree, and
    #: a reference of its reaches a conflicting group.
    inherited_from: GroupKey | None = None
    #: True when the group's verdict would depend on itself through a reference
    #: cycle. Fail-closed: a cycle is not an invitation to assume equality.
    cyclic: bool = False
    #: ``(path, description)`` of every member after the primary carrying an
    #: **element-level** description of its own that the merged element does not
    #: keep. A description below the element is :attr:`dropped_nested_descriptions`'.
    dropped_descriptions: tuple[tuple[str, str], ...] = ()
    #: ``(member source path, member path below the element)`` of every
    #: non-empty **nested** description of a member after the primary that the
    #: merged element does not keep -- its text differs from the primary's
    #: member of that path, or the primary's member has none.
    dropped_nested_descriptions: tuple[tuple[str, str], ...] = ()
    #: ``(path, LONG-NAME)`` of every composition after the primary whose
    #: ``LONG-NAME`` is present and is not the primary's: the merged
    #: composition's ``title`` is the primary's, so this text is dropped.
    dropped_long_names: tuple[tuple[str, str], ...] = ()
    #: ``(path, BASE-TYPE-ENCODING)`` of every base type after the primary whose
    #: encoding is the primary's token **spelled otherwise** (surrounding
    #: whitespace alone): the merged base type keeps the primary's text, so this
    #: spelling is not carried.
    dropped_encodings: tuple[tuple[str, str], ...] = ()
    #: ``(path, NATIVE-DECLARATION)`` of every base type after the primary whose
    #: native declaration is the primary's **spelled otherwise**, as for the encoding.
    dropped_native_declarations: tuple[tuple[str, str], ...] = ()
    #: The one member a ``--packages`` selection kept -- set only for
    #: :data:`SELECTED`, where it is the member that is mapped and need not be
    #: the first in source-path order. Every other member is excluded: skipped
    #: without a claim, and a reference to its path binds to nothing.
    selected_path: str | None = None

    @property
    def primary(self) -> str:
        """The member that is mapped: the one a selection kept, or the first in source-path order."""
        return self.selected_path or self.paths[0]


#: What the mapping layer does with one declaration once the verdict is in.
MAP: Final = "map"
"""Map it: it is in no group, or it is the primary of a merged one."""
ALIAS: Final = "alias"
"""Skip it silently: another member of its merged group is the element."""
CONFLICT: Final = "conflict"
"""Skip it, claim its name, import nothing of the group."""
EXCLUDED: Final = "excluded"
"""Skip it silently: a ``--packages`` selection kept another member of its group.

No name is claimed and no alias recorded, so a reference to this path binds to
nothing and is an unresolved reference under
:data:`~swcraft_tools.arxml_import.unresolved.CAUSE_SELECTION`, never bound to
the kept member: the two declarations differ."""


@dataclass(frozen=True, eq=False)
class MergeDecisions:
    """Every group of one export, and the three questions the mappers ask.

    A path in no multi-member group is unknown to every method here, so
    ``merges=None`` and an export declaring every name once look alike to the
    mapping layer. ``eq=False``: ``frozen=True`` alone would advertise a
    ``__hash__`` that raises, because ``group_by_path`` is a dict.
    """

    groups: tuple[MergeGroup, ...] = ()
    group_by_path: dict[str, MergeGroup] = field(default_factory=dict, repr=False)

    @property
    def merged(self) -> tuple[MergeGroup, ...]:
        return tuple(group for group in self.groups if group.verdict == MERGED)

    @property
    def conflicting(self) -> tuple[MergeGroup, ...]:
        return tuple(group for group in self.groups if group.verdict == CONFLICTING)

    @property
    def reselected(self) -> tuple[MergeGroup, ...]:
        """Every group a ``--packages`` selection re-admitted (:data:`SELECTED`).

        Empty unless :func:`narrow_to_selection` ran. A group here is in neither
        :attr:`merged` nor :attr:`conflicting`, which suppresses its conflict line.
        """
        return tuple(group for group in self.groups if group.verdict == SELECTED)

    def group_of(self, path: str) -> MergeGroup | None:
        """The group *path* is a member of, or ``None`` for every other path."""
        return self.group_by_path.get(path)

    def outcome(self, path: str) -> Literal["map", "alias", "conflict", "excluded"]:
        """:data:`MAP`, :data:`ALIAS`, :data:`CONFLICT` or :data:`EXCLUDED` for one declaration.

        The return annotation makes the four a closed vocabulary.
        """
        group = self.group_by_path.get(path)
        if group is None:
            return MAP
        if group.verdict == CONFLICTING:
            return CONFLICT
        if group.verdict == SELECTED:
            return MAP if path == group.primary else EXCLUDED
        return MAP if path == group.primary else ALIAS

    def primary_path(self, path: str) -> str:
        """*path*, or the member of its merged group mapped in its place.

        Only a merged group redirects: a conflicting group has no primary, and a
        :data:`SELECTED` group's members differ, so a reference to the excluded
        one is not answered with the kept one.
        """
        group = self.group_by_path.get(path)
        if group is None or group.verdict != MERGED:
            return path
        return group.primary

    def alias_paths(self) -> dict[str, str]:
        """Every non-primary merged member path onto its primary's.

        Handed to :class:`~swcraft_tools.arxml_import.registry.PathRegistry` so
        that a reference spelled at either path resolves to the one emitted name.
        """
        return {
            path: group.primary
            for group in self.groups
            if group.verdict == MERGED
            for path in group.paths
            if path != group.primary
        }


def narrow_to_selection(
    decisions: MergeDecisions,
    *,
    selected: frozenset[str],
    reached: frozenset[str],
) -> MergeDecisions:
    """*decisions* with every conflicting group a selection reduced re-admitted.

    A ``--packages`` selection is computed as a dependency closure after full
    mapping, so the verdict is taken over the whole file and narrowed here: a
    group of which the selection keeps **exactly one** member is re-admitted as
    that member, and one of which it keeps two or none is left alone. A user who
    narrowed to one of two parallel package trees never asked about the other.

    *Keeps* is read **package first**:

    * *selected* -- the declaration paths inside a package the selection names;
    * *reached* -- the declaration paths the selected components and
      compositions reach through the reference graph, asked only when the
      selection names no member of the group at all.

    What this does not reach:

    * A group of which the selection keeps neither member stays conflicting.
    * Nothing is re-decided: this function compares no property.
    * A path in no multi-member group (a kind outside :data:`LAYER_BY_TAG`) is
      unknown here, so a selection changes nothing about it.

    Args:
        decisions: the verdict taken over the whole file.
        selected: declaration paths inside a selected package.
        reached: declaration paths the selection's closure reaches.

    Returns:
        A new :class:`MergeDecisions`; *decisions* is not modified.
    """
    groups: list[MergeGroup] = []
    for group in decisions.groups:
        if group.verdict != CONFLICTING:
            groups.append(group)
            continue
        kept = [path for path in group.paths if path in selected] or [path for path in group.paths if path in reached]
        if len(kept) != 1:
            groups.append(group)
            continue
        groups.append(
            # No `dropped_descriptions`: the other members are no part of the element.
            MergeGroup(key=group.key, paths=group.paths, verdict=SELECTED, selected_path=kept[0])
        )
    return MergeDecisions(
        groups=tuple(groups),
        group_by_path={path: group for group in groups for path in group.paths},
    )


def stripped_text(value: str | None) -> str:
    """Return *value* without surrounding whitespace, or ``""`` when it is absent."""
    return (value or "").strip()


def declared_paths(model: ImportModel) -> frozenset[str]:
    """Every path this export **declares**, whether or not SWCraft maps it.

    The reader's path index plus the paths of the elements the reader refused
    (a ``MODE-SWITCH-INTERFACE``, ``PARAMETER-INTERFACE`` or
    ``TRIGGER-INTERFACE`` is recorded as an unrecognized tag and not indexed).
    It tells a target declared under a kind nobody maps from one declared
    **nowhere**; ``UnresolvedReferences`` (whether a reference is written into
    the model) and :func:`merge_verdict._port_interface_signature` (whether that
    written path is content) share this one definition of that distinction.

    It answers *declared*, never *mapped* or *resolvable*, and membership is
    exact: a path inside a declaration is in it only when the reader indexed or
    refused that path itself.
    """
    return frozenset(model.path_index) | {entry.path for entry in model.skip_log}


def chain_root(cause_path: str, chain_logs: Sequence[SkipLog]) -> SkipEntry | None:
    """The first entry recorded at *cause_path* in the first of *chain_logs* that holds one.

    *chain_logs* is searched **nearest first**: the log the chained entry goes
    into, then the mapper's that decided the cause, and the reader's
    ``model.skip_log`` last -- the reader records a duplicate full path at the
    declaration it kept, so asking it first would hide a mapper's own entry.
    """
    for skip_log in chain_logs:
        root = skip_log.first_at(cause_path)
        if root is not None:
            return root
    return None


def chained_reason(text: str, *, cause_path: str, chain_logs: Sequence[SkipLog]) -> SkipReason:
    """The :class:`SkipReason` of an element skipped because of a *declared* *cause_path*.

    The root's category and rule when one of *chain_logs* holds an entry at
    *cause_path* (:func:`chain_root`), else :attr:`SkipCategory.NOT_SUPPORTED`.
    The caller has already checked that the file declares *cause_path*; a cause
    outside the file is worded by the caller (or :func:`record_chained_to`).
    """
    root = chain_root(cause_path, chain_logs)
    if root is not None:
        return SkipReason(text, root.category, root.rule)
    return not_supported(text)


def record_chained_to(
    skip_log: SkipLog,
    declared: Collection[str],
    path: str,
    tag: str,
    reason: str,
    *,
    cause_path: str,
    upstream: Sequence[SkipLog] = (),
) -> SkipEntry:
    """Record an entry skipped because of *cause_path*, when the site knows only that path.

    The category:

    - :attr:`SkipCategory.NOT_SUPPORTED` when *cause_path* is blank;
    - the root's category, rule and effect when an entry is recorded at
      *cause_path* in *skip_log* or, in order, in *upstream* (:func:`chain_root`);
    - otherwise :attr:`SkipCategory.OUTSIDE_FILE` when *cause_path* is not in
      *declared* (:func:`declared_paths`): the file references content it does
      not declare;
    - otherwise :attr:`SkipCategory.NOT_SUPPORTED`: a declared element dropped
      without an entry of its own, such as a merged group's other member.

    What this does **not** reach: an entry recorded at *cause_path* only after
    this call is not seen, so every root must be recorded before what chains to
    it; nor is an entry in a log outside *upstream*.
    """
    logs = (skip_log, *upstream)
    if not cause_path:
        # Backstop: every caller guards a blank reference first.
        return skip_log.record(path, tag, reason, category=SkipCategory.NOT_SUPPORTED, effect="abort")
    root = chain_root(cause_path, logs)
    if cause_path not in declared and root is None:
        return skip_log.record(path, tag, reason, category=SkipCategory.OUTSIDE_FILE, effect="abort")
    # A chained entry shares its cause, so it inherits the root's effect; with
    # no root it aborts `import component`.
    return skip_log.record_reason(
        path,
        tag,
        chained_reason(reason, cause_path=cause_path, chain_logs=logs),
        effect=root.effect if root is not None else "abort",
    )


# ---------------------------------------------------------------------------
# The twin collapse -- read by both the mapping layer and this module
# ---------------------------------------------------------------------------
#
# A bare application primitive and the same-name implementation type a mapping
# set pairs it with collapse onto one element: the implementation type. The
# predicates below decide whether a pair collapses; ``type_mappers.map_data_types``
# and :func:`_mapping_chain` both read them, so they live in this base module.


def declares_physics(app: AppPrimitiveDataTypeInfo) -> bool:
    """Whether *app* declares any of the three properties the model holds.

    A primitive is *bare*, and may collapse onto its twin, only when it names
    none. Judged on the declarations, not on what could be read: a type naming
    a compu method SWCraft cannot hold still says something its twin does not.
    """
    return bool(app.unit_ref or app.compu_method_ref or app.data_constr_ref)


def texttable_defect(compu: CompuMethodInfo) -> str | None:
    """Why *compu*'s table cannot become an ``EnumDataType``, or ``None``.

    Three shapes, each of which ``schema.py`` or the writer refuses further
    down: no ``COMPU-SCALE`` at all, a scale whose ``VT`` is missing or blank
    (the reader only checks that the element is present), and a duplicate
    literal name or value, which ``writer.arxml_generation``'s enum emission
    rejects with a ``ValueError`` at generation time. The table is what is
    reported; the type itself is still imported, so references to it resolve.
    """
    if not compu.scales:
        return f"TEXTTABLE compu-method {compu.path} has no scales"
    blank = next((scale for scale in compu.scales if not scale.literal or not scale.literal.strip()), None)
    if blank is not None:
        return (
            f"TEXTTABLE compu-method {compu.path} has a COMPU-SCALE "
            f"(value {blank.value}) with missing or blank VT (literal)"
        )
    names = [scale.literal for scale in compu.scales]
    values = [scale.value for scale in compu.scales]
    duplicates = []
    name_dups = sorted({name for name in names if names.count(name) > 1})
    value_dups = sorted({value for value in values if values.count(value) > 1})
    if name_dups:
        duplicates.append(f"duplicate literal name(s) {name_dups}")
    if value_dups:
        duplicates.append(f"duplicate literal value(s) {value_dups}")
    if duplicates:
        return (
            f"TEXTTABLE compu-method {compu.path} has "
            + " and ".join(duplicates)
            + " (literal names/values must be unique within an enum)"
        )
    return None


# ---------------------------------------------------------------------------
# What the model holds of a form, read by the mapper and by the merge verdict alike
# ---------------------------------------------------------------------------


def _coefficient(literal: str) -> tuple[int | float | None, bool]:
    """One ``COMPU-RATIONAL-COEFFS`` ``V`` as the model value that holds it exactly, and whether none does.

    A non-numeric or non-finite coefficient answers ``(None, False)``, and the
    caller reports the conversion as not read. A ``V`` is a ``Numerical``, so
    the value is carried exactly or not at all: an integer no float holds is the
    ``int`` itself, every other number the float that equals it, and a float
    spelling no float holds exactly answers ``(None, True)``. The text is read
    by the formula grammar (``010`` is eight, ``08`` no number), as ``float()``
    reads it once spelled (``arxml_reader.stripped_spelling``: the formula's ``WS``
    between a unary sign and its literal skipped, GST R4.3.0:4809,
    R4.2.2:3957-3958, so ``- 8`` is -8). A hexadecimal or binary coefficient is
    one this release does not read.
    """
    try:
        value = float(stripped_spelling(literal, grammar="formula"))
    except ValueError:
        return None, False
    if not math.isfinite(value):
        return None, False
    return parse_exact_number(literal, grammar="formula")


def _denominator_is_one(denominator: tuple[str, ...]) -> bool:
    """Whether a ``COMPU-DENOMINATOR`` is exactly the polynomial ``1``.

    Judged on the source's literal, never on a float: as its exact decimal by
    the formula grammar (``0x1``, ``+ 1`` and ``1.0`` are one), and through
    :class:`fractions.Fraction` for a ratio spelling (``1/1``) alone. Any other
    denominator is refused rather than divided into the numerator, which would
    be recomputed physics. An **absent** denominator is the polynomial 1:
    ``COMPU-DENOMINATOR`` is ``minOccurs="0"`` in both schemas.
    """
    if not denominator:
        return True
    if len(denominator) != 1:
        return False
    text = denominator[0]
    stated = exact_decimal(text, grammar="formula")
    if stated is not None:
        return stated == 1
    # `Fraction` only for a ratio: it evaluates a decimal exponent as a power of
    # ten, so `1e-99999999999999999999` would not end.
    if "/" not in text:
        return False
    try:
        return Fraction(text) == 1
    except (ValueError, ZeroDivisionError):
        return False


def linear_coefficients(compu: CompuMethodInfo) -> tuple[tuple[int | float, int | float] | None, str | None]:
    """One compu method as a conversion's ``(factor, offset)``, or ``None`` with the sentence why.

    The importer recomputes nothing: a LINEAR method of exactly one
    ``COMPU-SCALE`` whose numerator holds ``[offset, factor]`` over a
    denominator of exactly ``[1]`` is the one shape the model holds; every other
    shape imports **without** the conversion and is named in the report.
    ``COMPU-RATIONAL-COEFFS`` lists coefficients lowest power first, so the
    numerator's first entry is the offset.
    """
    if compu.category != "LINEAR":
        return None, (
            f"compu method {compu.path} has category '{compu.category}', which a linear "
            "'conversion' cannot hold; the type is imported without it"
        )
    if compu.scale_count != 1:
        return None, (
            f"LINEAR compu method {compu.path} has {compu.scale_count} COMPU-SCALE(s), where a "
            "'conversion' is one linear scale; the type is imported without it and nothing is recomputed"
        )
    if not compu.rational_scales:
        return None, (
            f"LINEAR compu method {compu.path} has a COMPU-SCALE with no COMPU-RATIONAL-COEFFS; "
            "the type is imported without a conversion"
        )
    scale = compu.rational_scales[0]
    if len(scale.numerator) != 2:
        return None, (
            f"LINEAR compu method {compu.path} has a COMPU-NUMERATOR of {len(scale.numerator)} "
            "coefficient(s), where a 'conversion' reads exactly the two [offset, factor]; the type "
            "is imported without it"
        )
    if not _denominator_is_one(scale.denominator):
        return None, (
            f"LINEAR compu method {compu.path} has the COMPU-DENOMINATOR {list(scale.denominator)}, "
            "where a 'conversion' reads only the denominator [1]; the type is imported without it "
            "and nothing is recomputed"
        )
    offset, offset_inexact = _coefficient(scale.numerator[0])
    factor, factor_inexact = _coefficient(scale.numerator[1])
    if offset_inexact or factor_inexact:
        return None, (
            f"LINEAR compu method {compu.path} has a COMPU-NUMERATOR coefficient no model value "
            "holds exactly; the type is imported without a conversion and nothing is rounded"
        )
    if offset is None or factor is None:
        return None, (
            f"LINEAR compu method {compu.path} has a COMPU-NUMERATOR coefficient that is not a "
            "finite number; the type is imported without a conversion"
        )
    return (factor, offset), None


class ScaleLimitLine(NamedTuple):
    """One import-report line for a ``LINEAR`` scale's limit and what became of it.

    *lost* is whether the limit is not carried (an unreadable or incoherent
    pair, which ``import component`` refuses as a loss); an own-side infinite
    limit is carried as an absent bound, and its line is descriptive.
    """

    tag: str
    reason: str
    lost: bool


#: A scale limit's ``INTERVAL-TYPE`` as the model spells it; absent is ``CLOSED``,
#: the attribute's own default (XSD 4.2.2 ``:41118``, 4.3.0 ``:47486``).
_CARRIED_INTERVALS: dict[str | None, Literal["closed", "open"]] = {None: "closed", "CLOSED": "closed", "OPEN": "open"}


class CarriedLimit(TypedDict):
    """One carried scale limit: its exact value and its interval as the model spells it."""

    value: int | float
    interval: Literal["closed", "open"]


def carried_interval(interval: str | None) -> str | None:
    """*interval* as the model carries it (:data:`_CARRIED_INTERVALS`), any other one as spelled."""
    return _CARRIED_INTERVALS.get(interval, interval)


def linear_scale_limits(
    compu: CompuMethodInfo,
) -> tuple[dict[str, CarriedLimit] | None, list[ScaleLimitLine]]:
    """A ``LINEAR`` method's one scale's limits as a ``limits`` member, and the report lines owed.

    ``[TPS_SWCT_01548]`` (SWCT R4.2.2 ``:16830-16833``, R4.3.0 ``:18289-18292``) reads a
    scale's limits as the limiting values before the conversion is applied -- the
    conversion's scope (``linear.limits`` and ``conversion.limits``). Per limit, in this
    order:

    * absent: no bound, no line;
    * the **other** side's infinity (a lower limit of ``INF``, an upper one of ``-INF``,
      any interval): it admits no value and is no absent bound -- **unreadable**;
    * ``INTERVAL-TYPE="INFINITE"`` whatever its text (obsolete, XSD 4.2.2
      ``:72938-72940``, 4.3.0 ``:84211-84213``: *use INF / -INF*), or the side's own
      infinity: an **absent bound**, which states no limit on that side, and a
      descriptive line saying so;
    * ``CLOSED``, ``OPEN`` or no ``INTERVAL-TYPE``, and a text the formula grammar
      reads exactly (``arxml_reader.parse_exact_number``: an integer no float holds
      stays an ``int``): **carried**, with its interval; any other interval, a text
      that is no finite number (``NaN`` included) and one no model value holds
      exactly are **unreadable**.

    A pair is carried whole or not at all: an unreadable limit, and a lower limit
    above the upper one (or equal to it with either side open -- an interval that
    holds no value), omit the whole ``limits`` with a line for each limit stated,
    because a half kept would state a wider scope than the source did.

    Returns ``(None, lines)`` when nothing is carried. Asked of a method whose one
    scale :func:`linear_coefficients` has read.
    """
    scale = compu.rational_scales[0]
    carried: dict[str, CarriedLimit] = {}
    lines: list[ScaleLimitLine] = []
    unreadable: dict[str, str] = {}
    stated: list[str] = []
    for side, tag, text, interval in (
        ("lower", "LOWER-LIMIT", scale.lower_limit, scale.lower_interval),
        ("upper", "UPPER-LIMIT", scale.upper_limit, scale.upper_interval),
    ):
        if text is None:
            continue
        if is_infinite_limit(text, OTHER_LIMIT[tag]):
            unreadable[tag] = "spells the other side's infinity, which admits no value"
            continue
        if interval == "INFINITE" or is_infinite_limit(text, tag):
            lines.append(
                ScaleLimitLine(
                    tag,
                    f"{tag} of a LINEAR COMPU-SCALE is infinite on its own side: represented as an absent "
                    "bound, which states no limit on that side",
                    lost=False,
                )
            )
            continue
        if interval not in _CARRIED_INTERVALS:
            unreadable[tag] = f"has the INTERVAL-TYPE '{interval}'"
            continue
        value, no_exact_value = parse_exact_number(text, grammar="formula")
        if no_exact_value:
            unreadable[tag] = "is a number no model value holds exactly"
            continue
        if value is None:
            unreadable[tag] = "is not a finite number"
            continue
        stated.append(tag)
        carried[side] = {"value": value, "interval": _CARRIED_INTERVALS[interval]}
    if unreadable:
        for tag, why in unreadable.items():
            lines.append(
                ScaleLimitLine(
                    tag,
                    f"{tag} of a LINEAR COMPU-SCALE not carried: it {why}; the scale's limits are imported "
                    "together or not at all, so neither is",
                    lost=True,
                )
            )
        for tag in stated:
            other = OTHER_LIMIT[tag]
            lines.append(
                ScaleLimitLine(
                    tag,
                    f"{tag} of a LINEAR COMPU-SCALE not carried: the scale's {other} is not readable, and the "
                    "scale's limits are imported together or not at all",
                    lost=True,
                )
            )
        return None, lines
    lower, upper = carried.get("lower"), carried.get("upper")
    if lower is not None and upper is not None:
        # `schema._limits_incoherence` restated: this module reads no `schema` class.
        low, high = lower["value"], upper["value"]
        both_closed = (lower["interval"], upper["interval"]) == ("closed", "closed")
        empty = low > high or (low == high and not both_closed)
        if empty:
            for tag in ("LOWER-LIMIT", "UPPER-LIMIT"):
                lines.append(
                    ScaleLimitLine(
                        tag,
                        f"{tag} of a LINEAR COMPU-SCALE not carried: the scale's two limits state an empty "
                        "interval (the lower one above the upper one, or equal to it with a side OPEN), so "
                        "neither is imported",
                        lost=True,
                    )
                )
            return None, lines
    return (carried or None), lines


def texttable_limit_losses(compu: CompuMethodInfo) -> list[str]:
    """The limit tags of *compu*'s literals the writer does not write back.

    The writer writes a literal as the single-point interval ``[value, value]``,
    both limits ``CLOSED`` (``_emit_compu_method``): an ``UPPER-LIMIT`` of another
    value, or an ``INTERVAL-TYPE`` other than ``CLOSED`` (absent is ``CLOSED``), is
    a range the enumeration does not carry.
    """
    losses: list[str] = []
    for scale in compu.scales:
        if scale.lower_interval not in (None, "CLOSED"):
            losses.append("LOWER-LIMIT")
        upper = (
            parse_int_literal(scale.upper_limit, grammar="formula") if scale.upper_limit is not None else scale.value
        )
        if upper != scale.value or scale.upper_interval not in (None, "CLOSED"):
            losses.append("UPPER-LIMIT")
    return losses


#: ``/DataTypes/Application`` and ``/DataTypes/Implementation``: the packages of the two type
#: layers a private form is generated beside, each with its sub-packages.
_PRIVATE_FORM_HOMES = tuple(
    f"/{ROOT_PACKAGE_NAMES[0]}/{DATA_DESCRIPTION_PACKAGE_NAMES[layer]}"
    for layer in ("application_types", "implementation_types")
)


def _in_private_form_home(package: str) -> bool:
    return any(package == home or package.startswith(f"{home}/") for home in _PRIVATE_FORM_HOMES)


def _texttable_inline(compu: CompuMethodInfo) -> bool:
    """Whether an enumeration's ``literals`` state *compu* completely: what ``_emit_compu_method`` writes back.

    A table the enumeration can hold (:func:`texttable_defect`), every scale read (none dropped
    as malformed) and each a closed single point, no ``COMPU-PHYS-TO-INTERNAL``, no ``UNIT-REF``
    and no ``DESC`` of the method's own -- the generated form carries none of the three.
    """
    return (
        texttable_defect(compu) is None
        and len(compu.scales) == compu.scale_count
        and not texttable_limit_losses(compu)
        and not compu.phys_to_internal
        and compu.unit_ref is None
        and not compu.description
    )


def _linear_inline(compu: CompuMethodInfo, unit_ref: str | None) -> bool:
    """Whether a ``conversion`` states *compu* completely: what ``_emit_application_compu_method`` writes back.

    One scale read as ``[offset, factor] / [1]`` exactly (:func:`linear_coefficients`), its limits
    carried whole (:func:`linear_scale_limits` owes no lost line), no ``COMPU-PHYS-TO-INTERNAL``, no
    ``DESC`` of its own, and its ``UNIT-REF`` the unit its type names.
    """
    return (
        compu.category == "LINEAR"
        and linear_coefficients(compu)[0] is not None
        and not any(line.lost for line in linear_scale_limits(compu)[1])
        and not compu.phys_to_internal
        and not compu.description
        and compu.unit_ref is not None
        and compu.unit_ref == unit_ref
    )


def _range_inline(constr: DataConstrInfo, *, physical: bool) -> bool:
    """Whether a ``range`` (*physical*) or a value type's bounds state *constr* completely.

    What ``_emit_data_constr`` writes back for a private ``{name}_Range``: one rule, of
    ``PHYS-CONSTRS`` for a range and of ``INTERNAL-CONSTRS`` for a value type's bounds and not
    of the other, every stated limit a number the model holds exactly or one bounding nothing
    (its own side's infinity, or ``INFINITE`` over no ``NaN``: ``PhysLimit.bounds_nothing``), no level, no ``DESC``.
    """
    if constr.rule_count != 1 or constr.level_text is not None or constr.description:
        return False
    if physical:
        stated = [limit for limit in phys_limits(constr) if limit.text is not None]
        return (
            not constr.internal_present
            and not constr.unreadable_limits
            and not constr.inexact_limits
            and all(limit.value is not None or limit.bounds_nothing for limit in stated)
            and not (
                constr.phys_lower is not None
                and constr.phys_upper is not None
                and constr.phys_lower > constr.phys_upper
            )
        )
    return (
        not constr.phys_present
        and constr.internal_present
        and not constr.internal_other_tags
        and all(
            interval in (None, "CLOSED")
            for interval in (constr.internal_lower_interval, constr.internal_upper_interval)
        )
    )


def _unit_inline(unit: UnitInfo) -> bool:
    """Whether a ``unit`` text states *unit* completely: what ``_emit_unit`` writes back.

    A ``DISPLAY-NAME`` that is not blank -- the writer emits ``{name}_Unit`` only for a unit
    that is not blank, its text as ``DISPLAY-NAME`` -- no SI relation and no ``DESC``.
    """
    return (
        unit.display_name is not None
        and bool(unit.display_name.strip())
        and not unit.si_relation
        and not unit.description
    )


def inline_forms(model: ImportModel) -> frozenset[str]:
    """The paths of SWCraft's own private forms, each read back into its type's inline field.

    A form is read inline when **all** of these hold, and is an element otherwise --

    * it sits in its type's own package, sub-package included
      (``/DataTypes/Application[/{package}]`` or ``/DataTypes/Implementation[/{package}]``);
    * it is named ``{type}{suffix}`` for a type declared in that package -- ``_Unit``,
      ``_Compu`` and ``_Range`` for an application primitive's unit, ``LINEAR`` method and
      constraint, ``_CompuMethod`` for an enumeration's ``TEXTTABLE``, ``_Range`` for a value
      type's internal bounds -- the suffixes the writer generates, read from it, and only for the
      type it generates them for: the writer generates no unit and no range form for an
      enumeration (nor for any type naming a ``TEXTTABLE``), so a ``{type}_Unit`` or a
      ``{type}_Range`` beside one is a foreign author's form and an element, and a ``LINEAR``
      named ``{type}_CompuMethod`` is no ``_Compu``;
    * that type is its only referrer -- a unit's own ``_Compu``, itself read inline, is part
      of the type and not a second referrer;
    * its content is one the inline field states completely (:func:`_texttable_inline`,
      :func:`_linear_inline`, :func:`_range_inline`, :func:`_unit_inline`).

    The reading is exact in the file: a form read inline is written back at the same path,
    under the same name, with the same content. Such a form is no element, so it takes no
    merge verdict and its type's equality row compares its content.

    What this does not reach: a foreign file laid out in exactly SWCraft's packages and names
    is read the same way, and its author's form loses its identity as an element in the
    model, though not in the output.
    """
    referrers: dict[str, set[str]] = {}

    def refer(target: str | None, referrer: str) -> None:
        if target:
            referrers.setdefault(target, set()).add(referrer)

    for app in model.app_primitive_data_types:
        refer(app.unit_ref, app.path)
        refer(app.compu_method_ref, app.path)
        refer(app.data_constr_ref, app.path)
    for impl in model.impl_data_types:
        refer(impl.compu_method_ref, impl.path)
        refer(impl.data_constr_ref, impl.path)
    for method in model.compu_methods:
        refer(method.unit_ref, method.path)
    compu_by_path = {method.path: method for method in model.compu_methods}
    unit_by_path = {unit.path: unit for unit in model.units}
    constr_by_path = {constr.path: constr for constr in model.data_constrs}

    def own(type_path: str, form_path: str, suffix: str, also: str | None = None) -> bool:
        package, _, name = type_path.rpartition("/")
        return (
            _in_private_form_home(package)
            and form_path == f"{package}/{name}{suffix}"
            and referrers.get(form_path, set()) - {also} == {type_path}
        )

    inline: set[str] = set()
    # The writer generates no unit and no range form for a type naming a `TEXTTABLE`, so a
    # `_Unit` or a `_Range` beside one is another author's and an element.
    names_a_table: set[str] = set()
    for app in model.app_primitive_data_types:
        compu = compu_by_path.get(app.compu_method_ref or "")
        if compu is not None and compu.category == "TEXTTABLE":
            names_a_table.add(app.path)
            if own(app.path, compu.path, COMPU_METHOD_SUFFIX) and _texttable_inline(compu):
                inline.add(compu.path)
            continue
        if compu is not None and own(app.path, compu.path, COMPU_SUFFIX) and _linear_inline(compu, app.unit_ref):
            inline.add(compu.path)
        constr = constr_by_path.get(app.data_constr_ref or "")
        if constr is not None and own(app.path, constr.path, RANGE_SUFFIX) and _range_inline(constr, physical=True):
            inline.add(constr.path)
    # The units after the compu methods: a type's own `_Compu` names its `_Unit`, and is no
    # second referrer exactly when it is itself read inline.
    for app in model.app_primitive_data_types:
        if app.path in names_a_table:
            continue
        unit = unit_by_path.get(app.unit_ref or "")
        own_compu = app.compu_method_ref if app.compu_method_ref in inline else None
        if unit is not None and own(app.path, unit.path, UNIT_SUFFIX, also=own_compu) and _unit_inline(unit):
            inline.add(unit.path)
    for impl in model.impl_data_types:
        constr = constr_by_path.get(impl.data_constr_ref or "")
        if (
            impl.category == "VALUE"
            and constr is not None
            and own(impl.path, constr.path, RANGE_SUFFIX)
            and _range_inline(constr, physical=False)
            # Every stated internal limit carried as the value type's bound.
            and (constr.internal_lower_text is None) == (impl.value_min is None)
            and (constr.internal_upper_text is None) == (impl.value_max is None)
        ):
            inline.add(constr.path)
    return frozenset(inline)


# ---------------------------------------------------------------------------
# What a com-spec carries, read by the mapper and by the merge verdict alike
# ---------------------------------------------------------------------------

#: The one ``port_com_spec`` attribute with no silent value: a queued receiver's
#: ``QUEUE-LENGTH`` is multiplicity 1 with no default, so a stated one is always carried.
QUEUE_LENGTH_TAG = "QUEUE-LENGTH"

#: The children of a sender/receiver com-spec the importer reads or judges: the
#: reference, the init value, the filter and the six attributes of
#: :data:`COM_SPEC_SILENT_VALUES` with the queue length. Every other child is
#: reported as lost and signed by its tag in the merge verdict.
READ_DATA_ELEMENT_COM_SPEC_CHILDREN = frozenset(
    {"DATA-ELEMENT-REF", "INIT-VALUE", "FILTER", QUEUE_LENGTH_TAG, *COM_SPEC_SILENT_VALUES}
)
#: ... of a server com-spec: the operation and the queue length (the report's
#: *Server queue lengths not carried* block judges a length other than 1).
READ_SERVER_COM_SPEC_CHILDREN = frozenset({"OPERATION-REF", QUEUE_LENGTH_TAG})
#: ... of a client com-spec: the operation, which the writer writes back alone.
READ_CLIENT_COM_SPEC_CHILDREN = frozenset({"OPERATION-REF"})

#: The ``port_com_spec`` attributes a sender com-spec has a place for (Tables 4.60 / 4.67 of SWCT
#: R4.2.2): both directions carry these two.
SENDER_COM_SPEC_FIELDS = frozenset({"handle_out_of_range", "uses_end_to_end_protection"})
#: ... a queued receiver's: the two, plus its ``queueLength`` (Table 4.62).
QUEUED_RECEIVER_COM_SPEC_FIELDS = SENDER_COM_SPEC_FIELDS | {"queue_length"}
#: ... a non-queued receiver's: the two, plus the four of Table 4.61.
NONQUEUED_RECEIVER_COM_SPEC_FIELDS = SENDER_COM_SPEC_FIELDS | {
    "enable_update",
    "alive_timeout",
    "handle_never_received",
    "handle_timeout_type",
}
#: The one com-spec kind with a place for ``FILTER`` (``NONQUEUED-RECEIVER-COM-SPEC``
#: in both licensed schemas, R4.2.2 and R4.3.0), and so the one kind on which
#: a bare ``DATA-FILTER-TYPE ALWAYS`` is no filter.
FILTER_COM_SPEC_KIND = "NONQUEUED-RECEIVER-COM-SPEC"
#: The fields each sender/receiver com-spec kind has a place for.
COM_SPEC_KIND_FIELDS: dict[str, frozenset[str]] = {
    "NONQUEUED-SENDER-COM-SPEC": SENDER_COM_SPEC_FIELDS,
    "QUEUED-SENDER-COM-SPEC": SENDER_COM_SPEC_FIELDS,
    "NONQUEUED-RECEIVER-COM-SPEC": NONQUEUED_RECEIVER_COM_SPEC_FIELDS,
    "QUEUED-RECEIVER-COM-SPEC": QUEUED_RECEIVER_COM_SPEC_FIELDS,
}


def read_com_spec_children(tag: str) -> frozenset[str]:
    """The children of a com-spec of kind *tag* the importer reads by value.

    A sender/receiver kind's :data:`READ_DATA_ELEMENT_COM_SPEC_CHILDREN`, less
    ``INIT-VALUE`` on a ``QUEUED-*`` kind (the reader does not read it, and
    neither licensed schema gives a queued kind one) and less ``FILTER`` on
    every kind but :data:`FILTER_COM_SPEC_KIND` (the only kind the schemas give
    it, and the writer writes none); a server's and a client's sets; none for
    any other kind, which is reported whole.
    """
    if tag in COM_SPEC_KIND_FIELDS:
        read = READ_DATA_ELEMENT_COM_SPEC_CHILDREN
        if tag not in DATA_ELEMENT_COM_SPEC_KINDS:
            read = read - {"INIT-VALUE"}
        if tag != FILTER_COM_SPEC_KIND:
            read = read - {"FILTER"}
        return read
    if tag == "SERVER-COM-SPEC":
        return READ_SERVER_COM_SPEC_CHILDREN
    if tag == "CLIENT-COM-SPEC":
        return READ_CLIENT_COM_SPEC_CHILDREN
    return frozenset()


def unread_com_spec_children(shape: ComSpecInfo, read: Collection[str]) -> list[str]:
    """Every child of *shape* the importer does not read, by its tag.

    Each child whose tag is outside *read*, and of each tag in *read* every
    occurrence but the first in the AUTOSAR namespace, which is the one the
    reader reads: a repeat (``maxOccurs="1"`` in both licensed schemas) and a
    same-named child outside the namespace
    (:attr:`~swcraft_tools.arxml_reader.ComSpecInfo.foreign_child_tags`) are
    unread.
    """
    unread = [tag for tag in shape.child_tags if tag not in read]
    for tag in sorted(set(shape.child_tags) & set(read)):
        occurrences = shape.child_tags.count(tag)
        read_one = occurrences > shape.foreign_child_tags.count(tag)
        unread.extend([tag] * (occurrences - (1 if read_one else 0)))
    return unread


def silent_values_without_place(spec: DataElementComSpecInfo) -> list[str]:
    """Every attribute *spec* states at the writer's silent value that its kind has no place for.

    :func:`com_spec_values` carries a silent value by equivalence, which holds
    only where the writer writes it back. On a kind with no place for the
    attribute (an ``ENABLE-UPDATE`` on a sender) the writer writes nothing, so
    the stated value is lost and reported.
    """
    fields = COM_SPEC_KIND_FIELDS.get(spec.com_spec_tag, frozenset())
    carried, uncarried = com_spec_values(spec)
    return [
        tag
        for tag, attribute in COM_SPEC_SILENT_VALUES.items()
        if tag in spec.texts
        and tag not in uncarried
        and attribute.field not in carried
        and attribute.field not in fields
    ]


def _com_spec_value(tag: str, text: str) -> object | None:
    """The model value of one com-spec attribute's *text*, or ``None`` when the model holds no such value.

    A boolean is an ``xsd:boolean``; ``ALIVE-TIMEOUT`` a finite, non-negative
    number of seconds; an enumeration a literal of the attribute's table in
    :data:`~swcraft_tools.writer.arxml_generation.COM_SPEC_SILENT_VALUES`, whose
    other literals (``EXTERNAL-REPLACEMENT``, ``INVALID``, 4.3.0's
    ``REPLACE-BY-TIMEOUT-SUBSTITUTION-VALUE``) the model does not carry;
    ``QUEUE-LENGTH`` a positive integer.
    """
    if tag == QUEUE_LENGTH_TAG:
        length = parse_int_literal(text, grammar="positive-integer")
        return length if length is not None and length >= 1 else None
    attribute = COM_SPEC_SILENT_VALUES[tag]
    if attribute.literals is not None:
        return next((value for value, literal in attribute.literals.items() if literal == text), None)
    if isinstance(attribute.silent, bool):
        return parse_xsd_boolean(text)
    seconds = parse_float_literal(text)
    return seconds if seconds is not None and seconds >= 0 else None


def com_spec_values(spec: DataElementComSpecInfo) -> tuple[dict[str, object], dict[str, str]]:
    """What one source com-spec carries into a ``port_com_spec`` entry, and what it states that the model cannot hold.

    A value equal to the writer's silent value
    (:data:`~swcraft_tools.writer.arxml_generation.COM_SPEC_SILENT_VALUES`) is
    **carried by equivalence** and recorded nowhere, so a SWCraft-written file
    carries nothing. The first dict is every other value the source states, by
    field, for the seven attributes the model has (``QUEUE-LENGTH`` always,
    having no silent value); the second is every one of those attributes whose
    text is no value the model holds, by tag, as spelled. Which carried value
    has a place on the port is the mapper's to decide.
    """
    carried: dict[str, object] = {}
    uncarried: dict[str, str] = {}
    for tag in (*COM_SPEC_SILENT_VALUES, QUEUE_LENGTH_TAG):
        text = spec.texts.get(tag)
        if text is None:
            continue
        value = _com_spec_value(tag, text)
        if value is None:
            uncarried[tag] = text
            continue
        if tag == QUEUE_LENGTH_TAG:
            carried["queue_length"] = value
            continue
        attribute = COM_SPEC_SILENT_VALUES[tag]
        if value != attribute.silent or type(value) is not type(attribute.silent):
            carried[attribute.field] = value
    return carried, uncarried


def says_more_than_its_twin(app: AppPrimitiveDataTypeInfo, compu_by_path: dict[str, CompuMethodInfo]) -> bool:
    """Whether *app* carries anything its same-name implementation twin does not.

    The first clause of the twin collapse: :func:`declares_physics` refined by
    what can be built -- a TEXTTABLE type says more exactly when its table
    becomes an ``enum`` (or it also names a unit or a range).
    """
    compu = compu_by_path.get(app.compu_method_ref) if app.compu_method_ref else None
    if compu is not None and compu.category == "TEXTTABLE":
        return texttable_defect(compu) is None or bool(app.unit_ref or app.data_constr_ref)
    return declares_physics(app)


def composite_member_targets(model: ImportModel) -> frozenset[str]:
    """Every path an APPLICATION-RECORD-ELEMENT or an array's ELEMENT names.

    The second clause of the twin collapse: a record field or an array element
    may name only an application-side type, so collapsing a pair whose
    application member a composite names would make a model ``check-model``
    refuses. A bare pair is collapsed only where nothing but a data prototype,
    which may name either view, references its application member.
    """
    return frozenset(
        {element.type_ref for record in model.app_record_data_types for element in record.elements}
        | {array.element_type_ref for array in model.app_array_data_types}
    )


def collapses_onto_twin(
    app: AppPrimitiveDataTypeInfo,
    compu_by_path: dict[str, CompuMethodInfo],
    members: frozenset[str],
    kept: frozenset[str] = frozenset(),
) -> bool:
    """Whether a bare application primitive collapses onto its same-name implementation twin.

    Both members are kept when the application member says more than its twin
    (:func:`says_more_than_its_twin`), a composite member of the file names it
    (:func:`composite_member_targets`), or the target project already declares
    both members under its name (*kept*, ``import component`` alone: its export
    carries only the component's closure). ``type_mappers._keeps_both`` is the
    negation.

    Args:
        app: the application primitive the mapping set pairs by bare name.
        compu_by_path: every declared compu method, by path.
        members: :func:`composite_member_targets` of the model.
        kept: the names the target project declares both as an
            ``application_type`` and as an implementation type
            (``component_exchange.target_project.target_bare_pairs``); empty on the
            bulk path.

    Returns:
        Whether the pair is reduced to ONE element. ``False`` means both members
        are kept, each under the declared name in its own layer.
    """
    return not says_more_than_its_twin(app, compu_by_path) and app.path not in members and app.name not in kept


def _mapping_candidates(model: ImportModel) -> tuple[dict[str, str], dict[str, str]]:
    """``(twin target, generic target)`` per application path, the two candidates.

    Built as ``map_data_types`` builds them: the first generic entry wins, the
    last twin entry wins.
    """
    generic: dict[str, str] = {}
    twin: dict[str, str] = {}
    for mapping_set in model.data_type_mapping_sets:
        for entry in mapping_set.maps:
            generic.setdefault(entry.application_ref, entry.implementation_ref)
            if entry.application_ref.rsplit("/", 1)[-1] == entry.implementation_ref.rsplit("/", 1)[-1]:
                twin[entry.application_ref] = entry.implementation_ref
    return twin, generic


def _implementation_types_that_certainly_register(model: ImportModel) -> frozenset[str]:
    """Every ``IMPLEMENTATION-DATA-TYPE`` path of which this model alone says the mapper will register a name.

    **Positive only**: a path is in the set when every registration guard is a
    function of the declarations and every one is cleared, and out of it both
    when a guard refuses it and when the answer rests on anything this module
    cannot read -- which is what lets a fail-closed caller use it.
    :func:`_walked_chain` reads it in one branch alone.

    The guards it models (``map_data_types``' row-1 loop and
    ``resolve_impl_type``): the type is declared with a non-blank ``SHORT-NAME``;
    its ``CATEGORY`` is ``VALUE`` or ``TYPE_REFERENCE`` (a ``STRUCTURE``,
    ``ARRAY`` or ``DATA_REFERENCE`` is answered *not certain*); and its one
    reference resolves -- a ``VALUE``'s ``BASE-TYPE-REF`` onto a declared
    ``SW-BASE-TYPE`` with a non-blank name, a positive ``BASE-TYPE-SIZE`` and a
    non-blank ``BASE-TYPE-ENCODING``, a ``TYPE_REFERENCE``'s onto a declared
    implementation type this predicate answers for. A reference cycle answers *no*.

    **What it does not read**, each therefore outside the set: the type's own
    merge verdict (a conflicting twin is an edge of the reference graph
    instead, so the primitive's group inherits the conflict); the
    unresolved-reference policy, which keeps a type whose reference the export
    does not declare; and a ``--packages`` :data:`EXCLUDED`, decided after the
    verdict. ``test_arxml_import_merges.py`` pins the guard list against
    ``type_mapping.py``'s own skips.
    """
    impl_by_path = {impl.path: impl for impl in model.impl_data_types}
    base_by_path = {base.path: base for base in model.sw_base_types}
    answer: dict[str, bool] = {}

    def a_declared_base_type_registers(path: str) -> bool:
        base = base_by_path.get(path)
        if base is None or not stripped_text(base.name):
            return False
        return base.size is not None and base.size > 0 and bool(stripped_text(base.encoding))

    def registers(path: str, visiting: tuple[str, ...]) -> bool:
        cached = answer.get(path)
        if cached is not None:
            return cached
        if path in visiting:
            return False
        impl = impl_by_path.get(path)
        if impl is None or not stripped_text(impl.name):
            return False
        if impl.category == "VALUE":
            verdict = bool(impl.base_type_ref) and a_declared_base_type_registers(impl.base_type_ref or "")
        elif impl.category == "TYPE_REFERENCE":
            verdict = (
                bool(impl.impl_type_ref)
                and impl.impl_type_ref not in base_by_path
                and registers(impl.impl_type_ref or "", (*visiting, path))
            )
        else:
            # STRUCTURE, ARRAY and DATA_REFERENCE register over members this
            # predicate does not walk -- not certain; every other category never.
            verdict = False
        answer[path] = verdict
        return verdict

    return frozenset(path for path in impl_by_path if registers(path, ()))


def _mapping_chain(model: ImportModel) -> Iterator[tuple[str, str | None, str | None, bool]]:
    """One row per application primitive: ``(path, twin, generic, twin certainly registers)``.

    * *twin* -- the entry whose implementation ``SHORT-NAME`` equals the
      application type's, which the mapper collapses the pair onto. ``None``
      where the collapse cannot be taken: no twin entry, one naming nothing
      this export declares as an ``IMPLEMENTATION-DATA-TYPE``, or a pair the
      mapper keeps whole (:func:`collapses_onto_twin`). Never *the twin does
      not register*: that is a fact about the mapping run, and the fourth
      element is all the declarations say about it.
    * *generic* -- the first entry naming this application path. No mapper
      reads it for the primitive, but the ``mapping_sets/`` element writes it,
      so the ``--packages`` closure must keep the type it names.

    :func:`compared_mapping_targets` reads the twin;
    :func:`reachable_mapping_targets` reads both.
    """
    twin, generic = _mapping_candidates(model)
    implementation_paths = {impl.path for impl in model.impl_data_types}
    certain = _implementation_types_that_certainly_register(model)
    compu_by_path = {compu.path: compu for compu in model.compu_methods}
    members = composite_member_targets(model)
    for primitive in model.app_primitive_data_types:
        collapses = collapses_onto_twin(primitive, compu_by_path, members)
        candidate = twin.get(primitive.path) if collapses else None
        takeable = candidate if candidate is not None and candidate in implementation_paths else None
        yield primitive.path, takeable, generic.get(primitive.path), takeable is not None and takeable in certain


def _mapped(targets: dict[str, tuple[str, ...]], app_path: str, chain: tuple[str | None, ...]) -> None:
    """Record *chain* under *app_path* without absent and repeated entries, or nothing when none is left."""
    acted_on = tuple(dict.fromkeys(target for target in chain if target is not None))
    if acted_on:
        targets[app_path] = acted_on


def _implementation_groups(model: ImportModel) -> dict[str, frozenset[str]]:
    """Each implementation path -> **every** path declaring its ``SHORT-NAME``.

    :attr:`MergeGroup.paths` rebuilt for implementation types without asking the
    verdict. A path with a blank ``SHORT-NAME`` is in no group and absent here.
    """
    paths_by_name: dict[str, set[str]] = {}
    for impl in model.impl_data_types:
        name = stripped_text(impl.name)
        if name:
            paths_by_name.setdefault(name, set()).add(impl.path)
    return {path: frozenset(paths) for paths in paths_by_name.values() for path in paths}


def _one_group(groups: dict[str, frozenset[str]], left: str, right: str) -> bool:
    """Whether two implementation paths are members of one merge group (one path twice answers yes)."""
    return right in groups.get(left, frozenset())


def _walked_chain(
    groups: dict[str, frozenset[str]],
    decisions: MergeDecisions,
    package_selected: frozenset[str],
    twin: str | None,
    fallback: str | None,
    *,
    certain: bool,
    certain_set: frozenset[str],
) -> tuple[str | None, ...]:
    """The chain entries :func:`reachable_mapping_targets` declares; the one reader of the selection here.

    Both entries, except where they are members of one group
    (:func:`_one_group`): there a second edge would leave a conflicting group
    with two reached members and therefore unnarrowed, so the walk declares the
    one member the mapping layer comes to rest on -- by the group's verdict
    first and the selection second, the order the two are decided in:

    * a :data:`MERGED` group: its :attr:`MergeGroup.primary`, whatever the
      packages name, because the layer maps the group there;
    * a :data:`CONFLICTING` group the packages name no member of: the walk fills
      *reached*, so the member declared here is the one the group is narrowed
      to. The twin -- unless the declarations positively say the twin registers
      nothing and the fallback registers (*certain*, *certain_set*), where the
      twin would leave the layer falling through to an excluded entry and the
      primitive mapping nothing;
    * one member named: that member, if it is a candidate, else nothing;
    * two or more named: nothing, because the group stays conflicting and no
      member of it is mapped.

    Resting on nothing is no edge. A path in no group of *decisions* takes the
    selection branch, where the answer is the twin. *decisions* is the
    un-narrowed verdict, because this walk feeds :func:`narrow_to_selection`.
    *package_selected* is that function's *selected*, restricted to
    implementation types; *certain* is :func:`_mapping_chain`'s fourth element
    for this row and *certain_set* the predicate's whole answer.
    """
    if twin is None or fallback is None or not _one_group(groups, twin, fallback):
        # `_mapped` drops an absent or a repeated entry.
        return (twin, fallback)
    group = decisions.group_of(twin)
    if group is not None and group.verdict == MERGED:
        return (group.primary,)
    kept = groups[twin] & package_selected
    if not kept:
        # The member declared here is the one the group is narrowed to; *not
        # certain* covers *unknown*, so the twin stays the default.
        if not certain and fallback in certain_set:
            return (fallback,)
        return (twin,)
    if len(kept) > 1:
        return ()
    member = next(iter(kept))
    return (member,) if member in (twin, fallback) else ()


def compared_mapping_targets(model: ImportModel) -> dict[str, tuple[str, ...]]:
    """The implementation type the mapping layer **acts on** for each application primitive path.

    What :func:`merge_verdict.decide_merges` compares and walks as an edge of
    its reference graph: one entry where the declarations say the twin collapse
    can be taken -- the primitive *becomes* that implementation type -- and the
    path absent otherwise, so two primitives the collapse does not reach are
    compared by the rest of their row. The generic entry takes no part: it
    decides what one ``mapping_sets/`` element holds, which has an equality row
    of its own (:func:`merge_verdict._mapping_set_signature`).

    **What it does not reach:** the twin's merge verdict (a conflicting twin is
    an edge, so the primitive's group inherits the conflict); whether the twin
    registers (where it does not, the type's own properties, compared in the
    same row, decide -- an over-conflict, never a wrong merge); and a
    ``--packages`` exclusion, decided after the verdict.
    """
    targets: dict[str, tuple[str, ...]] = {}
    for app_path, twin, _fallback, _certain in _mapping_chain(model):
        _mapped(targets, app_path, (twin,))
    return targets


def reachable_mapping_targets(
    model: ImportModel, *, decisions: MergeDecisions, package_selected: frozenset[str]
) -> dict[str, tuple[str, ...]]:
    """Every implementation type :func:`_mapping_chain` can **come to rest on**, per application primitive path.

    What must survive ``--packages`` for the primitive to map at all, which
    :func:`~swcraft_tools.arxml_import.report.selection_reach` declares as the
    primitive's outgoing edges. Wider than :func:`compared_mapping_targets`:
    whether the twin registers is decided after the selection is narrowed, and
    where :func:`narrow_to_selection` keeps another member of the twin's group
    the layer falls through to the generic entry, so that entry must be walked
    too, or the primitive and everything typed by it is skipped.

    Both entries are declared, except where they are members of one group;
    :func:`_walked_chain` then picks one by the group's verdict, the selection
    and the declarations, in that order, because :func:`narrow_to_selection`'s
    *keeps* is not monotone in *reached*.

    **What it does not reach:**

    * a row whose two candidates are in **two different groups** is declared
      whole, because which branch the layer takes depends on how the twin's own
      group is narrowed. Where the two candidates' references land in one third
      group, that group is reached twice and stays conflicting, and the element
      the layer rests on loses the reference --
      ``test_two_candidates_in_two_groups_are_both_walked_over_reach_and_all``
      pins that over-reach;
    * the verdict it reads is the **un-narrowed** one, and only for the
      one-group row: the narrowed one consumes this walk's answer.
    """
    groups = _implementation_groups(model)
    certain_set = _implementation_types_that_certainly_register(model)
    targets: dict[str, tuple[str, ...]] = {}
    for app_path, twin, fallback, certain in _mapping_chain(model):
        _mapped(
            targets,
            app_path,
            _walked_chain(
                groups,
                decisions,
                package_selected,
                twin,
                fallback,
                certain=certain,
                certain_set=certain_set,
            ),
        )
    return targets
