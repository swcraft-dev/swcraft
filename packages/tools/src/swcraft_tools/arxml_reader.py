"""XML -> ImportModel: the ARXML import reader.

``read_arxml(path)`` parses one ARXML export file with plain ``xml.etree.ElementTree``, never a strict
schema-driven loader: a strict loader raises on the first unknown tag, the opposite of the skip-and-report policy
this importer needs. It produces an ``ImportModel``: every AUTOSAR package visited, every recognized element
indexed by its full AUTOSAR short-name path (``/Pkg/SubPkg/Element``), and a ``SkipLog`` of anything the reader
chose not to index, with a reason. The path index is the foundation of the two-pass reference resolution and of
the type-name normalization policy.

**The reader is AUTOSAR-version-agnostic.** :data:`AR_NS` is ``http://autosar.org/schema/r4.0``, stable across
the AUTOSAR 4.x line: only the XSD filename named in ``xsi:schemaLocation`` changes per release. That attribute is
read once, before the walk, as a **label** (``ImportModel.declared_schema``) the import report's citations name
the file's own release by -- never to choose a code path. A 4.3.0 export is traversed by the same code as a 4.2.2
one; add no version branching without a schema difference to point at, because an element the reader cannot make
sense of already becomes a named :class:`SkipLog` finding, never silence. ``test_arxml_reader.py``'s
``TestReadsA4_3_0LabelledFile`` compares a 4.3.0-labelled fixture's whole ``ImportModel``, skip log included, with
its 4.2.2 twin's. That establishes version-*label* tolerance only: the fixtures are hand-crafted to SWCraft's own
shapes, and the reader has not been verified against a real 4.3.0 export.
"""

from __future__ import annotations

import math
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal, NamedTuple

from swcraft_tools.arxml_version import (
    HANDLE_TERMINATION_AND_RESTART_LITERALS,
    XSI_NAMESPACE,
    ArxmlVersion,
    release_of_schema_filename,
    schema_filename_from_location,
)
from swcraft_tools.arxml_version import NAMESPACE as AR_NS
from swcraft_tools.capability_markers import OWN_MARKERS, SDG_GID


def _ar(tag: str) -> str:
    """Build a namespace-qualified AUTOSAR tag for ElementTree find/findall."""
    return f"{{{AR_NS}}}{tag}"


_SHORT_NAME = _ar("SHORT-NAME")
_DESC = _ar("DESC")
_L2 = _ar("L-2")
_ADMIN_DATA = _ar("ADMIN-DATA")
_SDGS = _ar("SDGS")
_SDG = _ar("SDG")
_SD = _ar("SD")

#: Every ``SD`` ``GID`` :func:`_is_own_marker_sdg` admits in the writer's own group, those of
#: ``capability_markers.OWN_MARKERS``. They come from the leaf ``capability_markers``, which the writer mints from
#: too, because the import side may not depend on the writer package. They are the SHAPE half of the exemption: a
#: ``SWCRAFT`` group holding an ``SD`` of any other ``GID`` stays vendor metadata, is reported, and aborts a
#: component import. The ``SD``'s TEXT is no part of the shape: a marker with an unexpected value is read here and
#: distrusted by the component exchange, which keeps a typo out of the lossy-import abort.
_OWN_MARKER_GIDS = frozenset(marker.sd_gid for marker in OWN_MARKERS)
_AR_PACKAGE = _ar("AR-PACKAGE")
_AR_PACKAGES = _ar("AR-PACKAGES")
_ELEMENTS = _ar("ELEMENTS")

# Element kinds the reader recognizes directly inside an ELEMENTS block and indexes for a later pass to interpret
# (imported, reduced, or skipped and reported) -- not a "will be imported" set. Anything else found there (a
# vendor extension, an unmodeled AUTOSAR kind) goes to SkipLog, so an export dialect variant degrades to a report
# line, never a crash.
KNOWN_ELEMENT_TAGS = frozenset(
    {
        "SW-BASE-TYPE",
        "IMPLEMENTATION-DATA-TYPE",
        "APPLICATION-PRIMITIVE-DATA-TYPE",
        "APPLICATION-RECORD-DATA-TYPE",
        "APPLICATION-ARRAY-DATA-TYPE",
        "COMPU-METHOD",
        "UNIT",
        "DATA-CONSTR",
        "DATA-TYPE-MAPPING-SET",
        "SENDER-RECEIVER-INTERFACE",
        "CLIENT-SERVER-INTERFACE",
        "APPLICATION-SW-COMPONENT-TYPE",
        "COMPOSITION-SW-COMPONENT-TYPE",
        "SWC-IMPLEMENTATION",
        # A component's execution-time budgets. The other timing views (VFB-, ECU-, SYSTEM-, SW-COMPOSITION-,
        # BSW-MODULE-TIMING) stay unrecognized tags.
        "SWC-TIMING",
        "CONSTANT-SPECIFICATION",
        "SW-ADDR-METHOD",
        "SYSTEM",
    }
)


def local_tag(qualified_tag: str) -> str:
    """Strip the namespace: ``{http://autosar.org/schema/r4.0}TAG`` -> ``TAG``."""
    if qualified_tag.startswith("{"):
        return qualified_tag.split("}", 1)[1]
    return qualified_tag


@dataclass
class RawElement:
    """One indexed ARXML element: AUTOSAR path, local tag, live ElementTree node and DESC text ("" when none)."""

    path: str
    tag: str
    node: ET.Element
    description: str = ""


class SkipCategory(StrEnum):
    """Whose problem a skipped element is -- the three sections of the import report.

    Set where the entry is recorded, never inferred afterwards from the reason's text.
    """

    #: The file breaks a rule of the standard, quoted as a :class:`StandardRule`.
    #: The fix belongs to whoever made the file.
    EXPORT_DEFECT = "export-defect"
    #: SWCraft's own gap or SWCraft's own stricter rule; also the default of a
    #: site no classification names. The fix is SWCraft's.
    NOT_SUPPORTED = "not-supported"
    #: The file references content it does not declare. The fix is to bring
    #: that content; until then the stubs stand in for it.
    OUTSIDE_FILE = "outside-file"


class StandardRule(StrEnum):
    """The closed set of normative rules an :attr:`SkipCategory.EXPORT_DEFECT` entry cites.

    Each value is the rule's id as the standard prints it, without brackets. Every member has **the same id and
    the same normative text** in R4.2.2 and R4.3.0, which lets :func:`cite` print either release's name before
    it -- with two exceptions: :attr:`TEXT_VALUE_UNDER_A_TABLE_ONLY`, whose list of categories differs and which is
    cited only where the two releases agree, and :attr:`ONE_RESULT_POINT_PER_CALL`, whose count differs (exactly
    one, at most one) and which is cited only where the release it names forbids what the file states. GST is
    ``AUTOSAR_TPS_GenericStructureTemplate.txt``, SWCT ``AUTOSAR_TPS_SoftwareComponentTemplate.txt``, each in its
    release's directory; a line is where the item starts.

    A rule is added only after its text was read in both releases: a reason that cannot be shown to break a quoted
    rule stays :attr:`SkipCategory.NOT_SUPPORTED`.

    **One value is not an id.** :attr:`C_IDENTIFIER_FORM` cites a primitive type's class table, ``GST primitive
    CIdentifier``: the ``CIdentifier`` pattern is the type's own definition, enforced by both schemas, and no
    ``constr_``, ``TPS_GST_`` or ``TPS_SWCT_`` item restates it. Its table number differs between the releases
    (4.31, 4.49), so the value names the table by its title, the same in both.

    **One sentence names a table by its release's number.** :attr:`COMPU_ATTRIBUTES_BY_CATEGORY` is
    ``[constr_1375]``, whose sentence is the same in both releases but for the table's number (5.74, 5.78). The
    member cites one row of that table, the same in both; its comment says which.
    """

    #: An instance reference's first context element is a feature of its base,
    #: and each later one a feature of the type of the element before it -- GST
    #: R4.2.2 :5299, R4.3.0 :6151.
    INSTANCE_REF_CONTEXT = "constr_2530"
    #: The context features in order, then the target, form the path from the
    #: root to the inner instance, the target always the last element of the
    #: instance reference -- GST R4.2.2 :5295,
    #: R4.3.0 :6147.
    INSTANCE_REF_PATH = "TPS_GST_00162"
    #: "The content of shortName needs to be unique (case insensitive) within a
    #: given Identifiable." -- GST R4.2.2 :2635, R4.3.0 :2711.
    SHORT_NAME_UNIQUE = "constr_2508"
    #: "A DelegationSwConnector can only connect PortPrototypes of the same kind,
    #: i.e. PPortPrototype to PPortPrototype and RPortPrototype to
    #: RPortPrototype." -- SWCT R4.2.2 :3504, R4.3.0 :3854.
    DELEGATION_SAME_KIND = "constr_1032"
    #: "Each pair of PortPrototypes can only be connected by one and only one
    #: SwConnector." -- SWCT R4.2.2 :3513, R4.3.0 :3863.
    ONE_CONNECTOR_PER_PAIR = "constr_1086"
    #: "A DelegationSwConnector can only connect a PortPrototype of a
    #: SwComponentPrototype that is owned by the same CompositionSwComponentType
    #: that also owns the connected delegation PortPrototype." -- SWCT R4.2.2
    #: :3530, R4.3.0 :3880.
    DELEGATION_INSIDE_COMPOSITION = "constr_1088"
    #: "Two SwComponentPrototypes are eventually connected by hooking up a
    #: PPortPrototype or PRPortPrototype of one SwComponentPrototype to a
    #: compatible RPortPrototype or PRPortPrototype of the other
    #: SwComponentPrototypes." -- SWCT R4.2.2 :2877, R4.3.0 :3228. Cited for an
    #: assembly of two provide or two require ports, whatever their interfaces.
    CONNECT_PROVIDE_TO_REQUIRE = "TPS_SWCT_01113"
    #: Two ports typed by port interfaces of different kinds cannot be
    #: connected; the subclasses of ``DataInterface`` are the exception and
    #: connect to each other -- SWCT R4.2.2 :3949,
    #: R4.3.0 :4300.
    CONNECT_SAME_INTERFACE_KIND = "constr_1036"
    #: A data prototype typed by an application primitive data type takes its initial value as an application
    #: value specification; when that type is an enumeration, the value names one of the text values (``vt``,
    #: ``shortLabel`` or ``symbol``) its compu scales define -- SWCT R4.2.2 :21113, R4.3.0 :23511. The first
    #: sentence of ``constr_1221`` is cited for a prototype's own value of an application primitive type that is
    #: not an application value (numerical form or a text value); the second for an initial value that matches none
    #: of its enumeration's literals.
    APPLICATION_VALUE_FOR_AN_APPLICATION_PRIMITIVE = "constr_1221"
    #: "If a DataPrototype is typed by an ImplementationDataType its initValue shall not be provided by an
    #: ApplicationValueSpecification." -- SWCT R4.2.2 :21119, R4.3.0 :23522. Cited for a prototype's own value
    #: under an implementation type with no table given in the application form.
    NO_APPLICATION_VALUE_FOR_AN_IMPLEMENTATION_TYPE = "constr_1385"
    #: A data prototype typed by an implementation data type whose compu method is of category ``TEXTTABLE`` or
    #: ``BITFIELD_TEXTTABLE`` takes a text value specification, and the value names one of the text values
    #: (``vt``, ``shortLabel`` or ``symbol``) its compu scales define -- SWCT R4.2.2 :15421, R4.3.0 :16544 (the
    #: same two categories in both). Cited for a prototype's own value under such a type that is not a text value
    #: naming one of its scales.
    TABLE_INIT_IS_A_TEXT_VALUE = "constr_1225"
    #: A text value specification belongs only under a data type whose compu method, in the role
    #: ``ImplementationDataType.swDataDefProps.compuMethod``, is of category ``TEXTTABLE`` or
    #: ``BITFIELD_TEXTTABLE`` -- SWCT R4.3.0 :21951, R4.2.2 :20008. **The one member whose two releases' lists
    #: differ:** R4.2.2 names SCALE_LINEAR_AND_TEXTTABLE and SCALE_RATIONAL_AND_TEXTTABLE beside the two, R4.3.0
    #: does not. Cited only where both forbid the text value: a text value under a type whose compu method, in that
    #: role, is of neither the two categories nor the two mixed ones -- under an application primitive type, whose
    #: compu method is in no implementation type's role, and under an implementation type that references none.
    TEXT_VALUE_UNDER_A_TABLE_ONLY = "constr_1284"
    #: A reference (an association of the meta-model) is written as a
    #: case-sensitive path of short names, absolute or relative, together with
    #: the destination type of the reference -- GST R4.2.2 :5753, R4.3.0
    #: :6728. The destination types, from the class tables:
    #: ``AutosarDataPrototype.type`` is an ``AutosarDataType`` (SWCT R4.2.2
    #: :13888, R4.3.0 :14836), ``ApplicationCompositeElementDataPrototype.type``
    #: an ``ApplicationDataType`` (:13908 in Table 5.30, :14857 in Table 5.31) and
    #: ``SwDataDefProps.implementationDataType`` an ``ImplementationDataType``
    #: (:14768, :15882); a ``SwBaseType`` is none of the three (:13479,
    #: :14239). ``ApplicationArrayElement`` and ``ApplicationRecordElement``
    #: take that ``type`` from their base ``ApplicationCompositeElementDataPrototype``
    #: (Base rows R4.2.2 :11881 in Table 5.13 and :12342 in Table 5.17, R4.3.0
    #: :12476 and :13095, the same table numbers); an ``ImplementationDataType``'s
    #: bases name ``AutosarDataType`` and no ``ApplicationDataType`` (Table 5.19,
    #: R4.2.2 :12534-12536, R4.3.0 :13287-13289). The schemas say the same: the
    #: ``TYPE-TREF`` of ``APPLICATION-COMPOSITE-ELEMENT-DATA-PROTOTYPE``, the group
    #: both element types include (``AUTOSAR_4-2-2.xsd:1611`` and ``:2127``,
    #: ``AUTOSAR_4-3-0.xsd:2067`` and ``:2777``), takes a ``DEST`` of
    #: ``APPLICATION-DATA-TYPE--SUBTYPES-ENUM`` (``AUTOSAR_4-2-2.xsd:1678``,
    #: ``AUTOSAR_4-3-0.xsd:2134``), whose five values are the application kinds
    #: alone (``AUTOSAR_4-2-2.xsd:1767-1775``, ``AUTOSAR_4-3-0.xsd:2318-2326``).
    #: Cited for a reference of one of the three that names a ``SW-BASE-TYPE``,
    #: and for a record or array element whose type reference names an
    #: ``IMPLEMENTATION-DATA-TYPE``.
    REFERENCE_DESTINATION_TYPE = "TPS_GST_00020"
    #: "If true, enforce minimum multiplicity; otherwise, it is "0"." -- GST
    #: R4.2.2 :1331, R4.3.0 :1408; ``Referrable.shortName`` has multiplicity 1
    #: and ``Tags: xml.enforceMinMultiplicity=true`` (R4.2.2 :2538 and :2542,
    #: R4.3.0 :2614 and :2618). Cited for a missing or blank ``SHORT-NAME``.
    SHORT_NAME_REQUIRED = "TPS_GST_00057"
    #: "The table 5.49 defines the supported combinations of swImplPolicy and swCalibrationAccess attribute
    #: setting." -- SWCT R4.2.2 :15613 (Table 5.49), R4.3.0 :16760 (the same sentence naming Table 5.50, whose rows
    #: are identical): ``queued`` supports ``notAccessible`` only, ``measurementPoint`` ``readOnly`` only, ``fixed``
    #: ``notAccessible`` only, ``const`` not ``readWrite``. Cited for a data element's ``SW-CALIBRATION-ACCESS`` its
    #: ``SW-IMPL-POLICY`` does not support.
    CALIBRATION_ACCESS_SUPPORTED = "constr_1017"
    #: "If the value of this attribute is set to queued then QueuedSenderComSpec
    #: and/or QueuedReceiverComSpec shall be defined." -- SWCT R4.2.2 :7734,
    #: R4.3.0 :8221 (the attribute is ``dataElement.swDataDefProps.swImplPolicy``).
    #: Cited for a component's require port that defines no
    #: ``QUEUED-RECEIVER-COM-SPEC`` for a queued data element of its interface.
    QUEUED_COM_SPEC_DEFINED = "TPS_SWCT_01219"
    #: "CompositionSwComponentTypes shall not own PortPrototypes typed by
    #: PortInterfaces where the attribute isService is set to true." -- SWCT
    #: R4.2.2 :4016, R4.3.0 :4369. Cited for a composition's port on such an
    #: interface.
    NO_SERVICE_PORT_ON_A_COMPOSITION = "constr_1174"
    #: "The SwDataDefProps.swImplPolicy of a VariableDataPrototype referenced by
    #: a VariableAccess aggregated in the role dataReceivePointByValue shall not
    #: be set to queued." -- SWCT R4.2.2 :25448, R4.3.0 :28306. Cited for a
    #: ``DATA-RECEIVE-POINT-BY-VALUE`` on a queued data element.
    NO_QUEUED_RECEIVE_BY_VALUE = "constr_1277"
    #: "The swImplPolicy of the VariableDataPrototype referenced by a VariableAccess in role dataReadAccess shall
    #: not be set to queued." -- SWCT R4.2.2 :25331, R4.3.0 :28449. Cited for a ``DATA-READ-ACCESS`` (an implicit
    #: read) of a queued data element.
    NO_QUEUED_IMPLICIT_READ = "constr_2020"
    #: A non-queued receiver com-spec with ``enableUpdate`` true must not reference a data element that a variable
    #: access in the role ``dataReadAccess`` references -- SWCT R4.2.2 :7950, R4.3.0 :8450. Cited for an implicit
    #: read of a data element whose port's com-spec states ``ENABLE-UPDATE`` true.
    NO_ENABLE_UPDATE_FOR_AN_IMPLICIT_READ = "constr_1103"
    #: "A ClientServerOperation of a particular RPortPrototype shall be mutually exclusive referenced by either a
    #: SynchronousServerCallPoints or an AsynchronousServerCallPoints." -- SWCT R4.2.2 :25868, R4.3.0 :28782.
    #: Cited for an operation of one port a component calls both synchronously and asynchronously
    #: (``rule:swc.call.mixed-synchrony``).
    CALL_SYNCHRONY_EXCLUSIVE = "constr_2022"
    #: "The timeout values of all ServerCallPoints referencing the same instance of ClientServerOperation in a
    #: RPortPrototype shall be identical." -- SWCT R4.2.2 :25875, R4.3.0 :28789. Cited for the asynchronous calls
    #: of one operation of one port stating different timeouts, an absent ``TIMEOUT`` beside a stated one
    #: included (``rule:swc.call.timeout-mismatch``).
    CALL_TIMEOUTS_IDENTICAL = "constr_2023"
    #: "A RunnableEntity referenced by an InitEvent in the role startOnEvent may aggregate an
    #: AsynchronousServerCallPoint but it shall not aggregate an AsynchronousServerCallResultPoint." -- SWCT
    #: R4.2.2 :24057, R4.3.0 :26717. Cited for a result point -- a poll's or a returns event's -- owned by a
    #: runnable an init event starts (``rule:swc.call.init-runnable-result``).
    NO_RESULT_POINT_IN_AN_INIT_RUNNABLE = "constr_1259"
    #: "The AsynchronousServerCallPoint has to be referenced by exactly one AsynchronousServerCallResultPoint."
    #: -- SWCT R4.2.2 :26048; "The AsynchronousServerCallPoint may be be referenced by at most one
    #: AsynchronousServerCallResultPoint." -- R4.3.0 :28964. **The second member whose text differs between the
    #: releases**, cited only where the release it names forbids what the file states: two or more result points
    #: of one call point under either release (``rule:swc.call.several-results``), and a call point with none only
    #: under a file that declares R4.2.2 (``rule:swc.call.async-without-result``), which :func:`cite` then prints
    #: as R4.2.2.
    ONE_RESULT_POINT_PER_CALL = "constr_2006"
    #: Within one ``EcuInstance``, the combination of a deployed runnable entity's ``RunnableEntity.symbol`` with
    #: its component's ``AtomicSwComponentType.symbol`` is unique: no two deployed runnable entities share one
    #: such combination -- System Template (``AUTOSAR_TPS_SystemTemplate.txt``) R4.2.2 :26114, R4.3.0 :30512;
    #: the SWCT's ``RunnableEntity.symbol`` row points to it (R4.2.2 :23620, R4.3.0 :26186). The System
    #: Template's ``AtomicSwComponentType.symbol`` is the SWCT's ``SymbolProps.symbol`` aggregated as
    #: ``symbolProps``. Two runnables of **one** component share that component's symbol, so two equal runnable
    #: symbols there are two equal combinations in every EcuInstance that deploys the component -- the inference
    #: this citation rests on. Cited for two or more runnables of one component whose stated ``SYMBOL`` texts are
    #: equal.
    SYMBOL_UNIQUE = "constr_2025"
    #: "A RunnableEntity that defines a VariableAccess in role writtenLocalVariable and readLocalVariable shall
    #: make use of AutosarVariableRef.localVariable." -- SWCT R4.2.2 :24930, R4.3.0 :27691. Cited for a local
    #: variable access whose ``ACCESSED-VARIABLE`` states an ``AUTOSAR-VARIABLE-IREF`` or an
    #: ``AUTOSAR-VARIABLE-IN-IMPL-DATATYPE``.
    LOCAL_VARIABLE_REFERENCE = "TPS_SWCT_01521"
    #: The variable a ``localVariable`` reference names belongs to the same internal behaviour as the runnable,
    #: and it is declared in the role ``implicitInterRunnableVariable`` or ``explicitInterRunnableVariable`` --
    #: SWCT R4.2.2 :24934, R4.3.0 :27695. Cited for a ``LOCAL-VARIABLE-REF`` that names no variable of its own
    #: behaviour.
    LOCAL_VARIABLE_OWNER = "constr_2026"
    #: "This datatype represents a string, that follows the rules of
    #: C-identifiers." with ``xml.xsd.pattern=[a-zA-Z_][a-zA-Z0-9_]*`` -- GST
    #: R4.2.2 Table 4.31 *Primitive CIdentifier* (:3203), R4.3.0 Table 4.49
    #: (:3910); both schemas enforce it (``C-IDENTIFIER--SIMPLE``, XSD 4.2.2
    #: :8295, 4.3.0 :9281). **Not a rule id**: the class docstring says why.
    #: Cited for a ``SYMBOL`` whose text, as written, is not a C identifier.
    C_IDENTIFIER_FORM = "GST primitive CIdentifier"
    #: Which attributes a compu method and its related meta-classes may hold depends on its category, as a table
    #: of the template restricts them (Table 5.74 in R4.2.2) -- SWCT R4.2.2 :17401 (Table 5.74, caption :17445);
    #: R4.3.0 :18891 names Table 5.78 (caption :18928) in the same sentence. Cited for the table's
    #: ``CompuScale.shortLabel`` row, the same in both releases -- ``N/A`` in the ``LINEAR`` column (R4.2.2
    #: :17438, R4.3.0 :18921). The ``compuPhysToInternal`` row is **not** the same in the two (``LINEAR``: ``D``
    #: at R4.2.2 :17431, ``D(1)`` at R4.3.0 :18914) and is not what this member cites. Cited for a
    #: ``SHORT-LABEL`` on the scale of a ``LINEAR`` compu method; descriptive, a label changes no behaviour.
    COMPU_ATTRIBUTES_BY_CATEGORY = "constr_1375"


#: The release a file that declares no release SWCraft writes is judged
#: against: the later of the two. SWCraft does not claim to know a later
#: release's ids (R22-11, for one, retired ``[constr_2530]`` in favour of
#: ``[TPS_GST_00386]`` and ``[constr_2626]``), and the report's heading says
#: which release it cited from.
FALLBACK_JUDGING_RELEASE = ArxmlVersion.V4_3_0


def cite(rule: StandardRule, release: ArxmlVersion | None) -> str:
    """The printed citation of *rule* in *release*: ``R4.2.2 [constr_2530]``.

    *release* is the file's :attr:`ImportModel.declared_release`; ``None`` cites
    :data:`FALLBACK_JUDGING_RELEASE`. The value that is not an id prints the same way,
    ``R4.3.0 [GST primitive CIdentifier]`` (:attr:`StandardRule.C_IDENTIFIER_FORM`).
    """
    judged_by = FALLBACK_JUDGING_RELEASE if release is None else release
    return f"R{judged_by} [{rule}]"


def _check_rule(category: SkipCategory, rule: StandardRule | None) -> None:
    """A defect cannot exist without its rule, and nothing else carries one.

    The type checks are load-bearing: both enums are ``StrEnum``s, so a bare ``"export-defect"`` compares equal
    to the member and would slip past the identity tests below.
    """
    if not isinstance(category, SkipCategory):
        raise TypeError(f"category must be a SkipCategory, got {category!r}")
    if rule is not None and not isinstance(rule, StandardRule):
        raise TypeError(f"rule must be a StandardRule or None, got {rule!r}")
    if category is SkipCategory.EXPORT_DEFECT and rule is None:
        raise ValueError("an export defect must cite the StandardRule the file breaks")
    if category is not SkipCategory.EXPORT_DEFECT and rule is not None:
        raise ValueError(f"only an export defect cites a rule; {category} was given {rule}")


@dataclass(frozen=True)
class SkipReason:
    """A reason-returning helper's answer: the text and its category, decided together.

    Helpers that return why an element cannot be mapped return this instead of a bare string, so the category is
    decided where the cause is known and passed on whole (:meth:`SkipLog.record_reason`). The constructor applies
    :class:`SkipEntry`'s rule, so a defect reason cannot exist without its rule either.
    """

    text: str
    category: SkipCategory
    rule: StandardRule | None = None

    def __post_init__(self) -> None:
        _check_rule(self.category, self.rule)


#: The sentence a duplicate-name reason gains in a variant-rich file. Both
#: releases let a (not-yet-bound) variant-rich model repeat a shortName:
#: ``[TPS_GST_00251]`` (GST R4.2.2 :7772, R4.3.0 :8816) substitutes
#: ``[constr_2508]`` by ``[constr_2512]`` for such models, and SWCraft binds no
#: variant, so it cannot tell variant alternatives from a real duplicate. The
#: line is SWCraft's (section 2) and carries no citation in its sentence.
VARIANT_RICH_DUPLICATE_CLAUSE = (
    "-- the file carries variation points, whose alternatives may share a name; SWCraft binds no variant"
)


def duplicate_name_reason(text: str, *, variant_rich: bool) -> SkipReason:
    """The category of a duplicate name among one parent's children, decided per file.

    In a file that carries no ``VARIATION-POINT`` the two declarations share one AUTOSAR name space, so the file
    breaks ``[constr_2508]`` (:attr:`StandardRule.SHORT_NAME_UNIQUE`) and *text* is returned unchanged. In a
    variant-rich file (:attr:`ImportModel.variant_rich`) the line is :attr:`SkipCategory.NOT_SUPPORTED` and
    *text* gains :data:`VARIANT_RICH_DUPLICATE_CLAUSE`.

    **What this does not reach:** the flag is per file, not per element, so a variant-rich file's *genuine*
    duplicate -- not variant alternatives, or alternatives whose ``shortLabel``s also collide, which
    ``[constr_2512]`` itself forbids -- is section 2 as well, the conservative side.
    """
    if variant_rich:
        return SkipReason(f"{text} {VARIANT_RICH_DUPLICATE_CLAUSE}", SkipCategory.NOT_SUPPORTED)
    return SkipReason(text, SkipCategory.EXPORT_DEFECT, StandardRule.SHORT_NAME_UNIQUE)


def not_supported(text: str) -> SkipReason:
    """*text* as SWCraft's own refusal: :attr:`SkipCategory.NOT_SUPPORTED`, no rule."""
    return SkipReason(text, SkipCategory.NOT_SUPPORTED)


def blank_short_name(text: str = "missing or blank SHORT-NAME") -> SkipReason:
    """*text* as the file's defect of a missing or blank ``SHORT-NAME``.

    :attr:`SkipCategory.EXPORT_DEFECT` citing ``[TPS_GST_00057]``
    (:attr:`StandardRule.SHORT_NAME_REQUIRED`), decided here once for every
    element and member kind the importer skips for it; *text* names which one.
    """
    return SkipReason(text, SkipCategory.EXPORT_DEFECT, StandardRule.SHORT_NAME_REQUIRED)


#: What an entry does to ``import component``: :data:`ABORT` -- the value changes what an RTE or an integrator
#: does, so the lossy-import refusal applies -- or :data:`REPORT` -- the value documents or annotates, so the entry
#: is printed in the pre-write preview and under ``--dry-run`` in the *Not carried from the file* block and the
#: import goes past it. ``import model`` prints both alike, in their sections.
SkipEffect = Literal["abort", "report"]
ABORT: SkipEffect = "abort"
REPORT: SkipEffect = "report"
_SKIP_EFFECTS = frozenset({ABORT, REPORT})


@dataclass
class SkipEntry:
    """One reason an element did not make it into the ImportModel.

    ``category`` and ``effect`` are required with no default, so a site cannot forget to choose them; ``rule`` is
    required for :attr:`SkipCategory.EXPORT_DEFECT` and forbidden otherwise, checked here. ``effect`` is
    :data:`ABORT` or :data:`REPORT`, what the entry does to ``import component``; a chained entry inherits its
    cause's (:meth:`SkipLog.record_chained`). ``reason`` stays mutable: a chained entry is re-worded once its
    target is written as a stub.
    """

    path: str
    tag: str
    reason: str
    category: SkipCategory = field(kw_only=True)
    rule: StandardRule | None = field(default=None, kw_only=True)
    effect: SkipEffect = field(kw_only=True)

    def __post_init__(self) -> None:
        _check_rule(self.category, self.rule)
        if self.effect not in _SKIP_EFFECTS:
            raise ValueError(f"effect must be one of {sorted(_SKIP_EFFECTS)}, got {self.effect!r}")


@dataclass
class SkipLog:
    """Append-only collector of skipped elements: element path + tag + reason.

    Every importer step that chooses not to import something records the reason here instead of raising:
    skip-and-report is the importer's failure mode, not an exceptional path.

    It also indexes the **first** :data:`ABORT` entry recorded at each path (:meth:`first_at`): the one that
    decided the element's fate, and the root a chained entry inherits its category, rule and effect from
    (:meth:`record_chained`). A :data:`REPORT` entry documents an element that is imported, so it decides no fate.
    """

    entries: list[SkipEntry] = field(default_factory=list)
    _first: dict[str, SkipEntry] = field(default_factory=dict, init=False, repr=False, compare=False)
    #: The first entry recorded at each ``(path, tag)``, of either effect (:meth:`first_tagged`).
    _first_tagged: dict[tuple[str, str], SkipEntry] = field(default_factory=dict, init=False, repr=False, compare=False)
    #: The ``(path, tag, reason)`` of every text-form line recorded (:func:`_report_text_form`): an indexed
    #: element's ``DESC`` is judged by the index and by its kind's reader, so the second judgement is a set lookup.
    text_forms_reported: set[tuple[str, str, str]] = field(default_factory=set, init=False, repr=False, compare=False)

    def record(
        self,
        path: str,
        tag: str,
        reason: str,
        *,
        category: SkipCategory,
        rule: StandardRule | None = None,
        effect: SkipEffect,
    ) -> SkipEntry:
        entry = SkipEntry(path=path, tag=tag, reason=reason, category=category, rule=rule, effect=effect)
        self.entries.append(entry)
        self._first_tagged.setdefault((path, tag), entry)
        if effect == ABORT:
            # A descriptive entry is never a root a chained entry inherits from.
            self._first.setdefault(path, entry)
        return entry

    def record_reason(
        self, path: str, tag: str, reason: SkipReason, *, prefix: str = "", effect: SkipEffect
    ) -> SkipEntry:
        """Record *reason* whole: its text (after *prefix*, such as ``"inner end: "``), category and rule.

        The one place a reason-returning helper's answer becomes an entry, so a wrapping site cannot drop the
        category. *effect* has no default, as on :meth:`record`.
        """
        return self.record(
            path, tag, f"{prefix}{reason.text}", category=reason.category, rule=reason.rule, effect=effect
        )

    def record_chained(self, path: str, tag: str, reason: str, *, root: SkipEntry) -> SkipEntry:
        """Record an entry skipped only because *root* was: it inherits *root*'s category, rule and effect.

        The entry's own *reason* names the root, so a chained line in the defects section says which element the
        cited rule is about.
        """
        return self.record(path, tag, reason, category=root.category, rule=root.rule, effect=root.effect)

    def first_at(self, path: str) -> SkipEntry | None:
        """The first entry recorded at *path*, or ``None`` when nothing was."""
        return self._first.get(path)

    def first_tagged(self, path: str, tag: str) -> SkipEntry | None:
        """The first entry recorded at ``(path, tag)``, whatever its effect, or ``None`` when nothing was."""
        return self._first_tagged.get((path, tag))

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)


# ---------------------------------------------------------------------------
# Typed data-type element content
#
# These dataclasses mirror the ARXML shape (paths, categories, raw refs), not SWCraft schema models: the
# translation is arxml_import's job, so this module never imports schema.py.
# ---------------------------------------------------------------------------


@dataclass
class SwBaseTypeInfo:
    """One SW-BASE-TYPE: the AUTOSAR leaf bit-representation type. Maps to a SWCraft `base_type`.

    The last three fields map to nothing and are read for the merge alone: `[constr_1220]` makes two SW-BASE-TYPEs
    of one name the same definition only when BASE-TYPE-SIZE, BASE-TYPE-ENCODING, NATIVE-DECLARATION, BYTE-ORDER
    and MEM-ALIGNMENT -- and the CATEGORY -- all agree, and the merge may never silently assume a property it
    cannot see is equal.

    `encoding` and `native_declaration` are the element's text exactly as written, surrounding whitespace
    included: the writer writes each as the model holds it. Only the merge strips them.

    The last three are kept as the export spelled them: `None` when the element does not declare the property,
    unparsed and stripped of surrounding whitespace otherwise (`_declared_text`). MEM-ALIGNMENT is a
    PositiveInteger, so `8` and `08` are one alignment and two texts; comparing the texts refuses a merge the
    standard would allow, the fail-closed direction, where parsing would map an unreadable value onto the `None`
    an absent one gets. A refused merge is a conflicting group, which imports **neither** member and dangles
    every reference into the group.
    """

    path: str
    name: str
    size: int | None
    encoding: str
    native_declaration: str
    description: str
    byte_order: str | None = None
    mem_alignment: str | None = None
    category: str | None = None


@dataclass(frozen=True)
class ImplSubElementInfo:
    """One ``IMPLEMENTATION-DATA-TYPE-ELEMENT`` of a ``STRUCTURE`` or an ``ARRAY``.

    Every value is read at the sub-element's **own** node -- its own ``SW-DATA-DEF-PROPS`` conditional's direct
    children -- so a nested inline member's references are never read as its parent's. ``ARRAY-SIZE``,
    ``ARRAY-SIZE-SEMANTICS`` and ``ARRAY-SIZE-HANDLING`` sit on the sub-element in AUTOSAR, so they are read here
    and are ``None`` where it declares none; ``array_size`` is ``None`` for an unreadable size as well.

    What this does **not** carry: a nested inline member's own children. ``nested_sub_elements`` counts them so
    the importer can name the shape it does not model.
    """

    name: str
    category: str
    impl_type_ref: str | None
    base_type_ref: str | None
    array_size: int | None = None
    array_size_semantics: str | None = None
    array_size_handling: str | None = None
    nested_sub_elements: int = 0
    #: The element's own ``DESC``. A record's element carries it into ``ImplementationElement.description``; an
    #: array's one element is no model element, so the reader reports it instead (:func:`_read_impl_data_type`).
    description: str = ""


@dataclass
class ImplDataTypeInfo:
    """One IMPLEMENTATION-DATA-TYPE; its CATEGORY decides what it maps to.

    VALUE (BASE-TYPE-REF -> SW-BASE-TYPE) maps to `value_type`; TYPE_REFERENCE (IMPLEMENTATION-DATA-TYPE-REF ->
    another impl type) maps to `reference_type`, one step: its `base` is the type the reference names.

    The three composite categories' content is read without deciding what is imported: a ``STRUCTURE``'s or an
    ``ARRAY``'s ``SUB-ELEMENTS`` in order, and a ``DATA_REFERENCE``'s ``SW-POINTER-TARGET-PROPS`` -- its
    ``TARGET-CATEGORY``, the target's ``IMPLEMENTATION-DATA-TYPE-REF`` (or ``BASE-TYPE-REF``, a ``void``
    target's shape), whether the target props nest another pointer inline, and the tag of every other qualifier,
    so none is dropped in silence. Each defaults to "absent".
    """

    path: str
    name: str
    category: str
    base_type_ref: str | None
    impl_type_ref: str | None
    description: str
    sub_elements: tuple[ImplSubElementInfo, ...] = ()
    pointer_target_category: str | None = None
    pointer_target_ref: str | None = None
    pointer_target_base_type_ref: str | None = None
    pointer_target_nested: bool = False
    pointer_target_qualifiers: tuple[str, ...] = ()
    #: The ``DATA-CONSTR-REF`` of the type's first conditional, and the closed bounds
    #: :func:`_carry_value_type_ranges` reads for a ``VALUE`` type from the first rule's ``INTERNAL-CONSTRS`` --
    #: ``None`` where no bound is carried. A bound is the model value that holds the source's text exactly
    #: (:func:`parse_exact_number`): a ``float`` where one does, the ``int`` for an integer no float holds (a
    #: 64-bit limit above 2**53), and nothing for a float spelling no float holds exactly.
    data_constr_ref: str | None = None
    value_min: int | float | None = None
    value_max: int | float | None = None
    #: The ``COMPU-METHOD-REF`` of the type's first conditional, ``None`` when it states none.
    compu_method_ref: str | None = None


@dataclass
class AppPrimitiveDataTypeInfo:
    """One APPLICATION-PRIMITIVE-DATA-TYPE.

    Maps to ``enum`` when its ``COMPU-METHOD-REF`` resolves to a TEXTTABLE compu-method and to
    ``application_type`` otherwise. The three references below are the whole of the ``SwDataDefProps`` content the
    model holds -- a unit, a conversion and a range; everything else the conditional carries is reported by
    :func:`_read_app_primitive_data_type`.
    """

    path: str
    name: str
    compu_method_ref: str | None
    description: str
    unit_ref: str | None = None
    data_constr_ref: str | None = None
    #: The declared ``CATEGORY``, stripped, or ``""`` where the export declares none. No model field carries it
    #: (SWCraft writes both kinds as ``VALUE``), but the merge's equality row needs it: two declarations of one
    #: ``SHORT-NAME`` differing only in ``CATEGORY`` are two different declarations. The importer, not the
    #: reader, reports its loss on re-export.
    category: str = ""


@dataclass
class RecordElementInfo:
    """One APPLICATION-RECORD-ELEMENT: a named, typed field of an APPLICATION-RECORD-DATA-TYPE."""

    name: str
    type_ref: str
    #: Carried into ``RecordField.description``.
    description: str = ""


@dataclass
class AppRecordDataTypeInfo:
    """One APPLICATION-RECORD-DATA-TYPE. Maps to `record`, fields from its APPLICATION-RECORD-ELEMENT children."""

    path: str
    name: str
    elements: list[RecordElementInfo]
    description: str


@dataclass
class AppArrayDataTypeInfo:
    """One APPLICATION-ARRAY-DATA-TYPE as `array`: element type and MAX-NUMBER-OF-ELEMENTS from its ELEMENT."""

    path: str
    name: str
    element_type_ref: str
    max_number_of_elements: int | None
    description: str


@dataclass
class CompuScaleInfo:
    """One COMPU-SCALE inside a TEXTTABLE COMPU-METHOD: an integer value paired with its literal text (VT)."""

    value: int
    literal: str
    #: The ``COMPU-SCALE``'s own ``DESC``, carried into ``EnumLiteral.description``.
    description: str = ""
    #: The scale's ``UPPER-LIMIT`` text minus surrounding whitespace (``None`` when absent), and both limits'
    #: ``INTERVAL-TYPE``s. The writer writes a literal as a single-point interval, ``UPPER-LIMIT`` equal to
    #: ``LOWER-LIMIT`` and both ``CLOSED``, so the mapper reports a scale whose upper limit or interval says
    #: anything else.
    upper_limit: str | None = None
    lower_interval: str | None = None
    upper_interval: str | None = None
    #: Whether the scale states a ``SYMBOL`` (a flag; SWCraft holds no symbol). R4.3's ``[TPS_SWCT_01696]``
    #: ranks a scale's ``symbol`` above its ``vt``, so a text value under a table one of whose scales states one
    #: cannot be shown to name the entry SWCraft holds.
    states_symbol: bool = False
    #: The scale's ``SHORT-LABEL`` as written, ``None`` when absent. ``[constr_1221]`` and ``[constr_1225]``
    #: admit a text value matching a scale's ``shortLabel``; SWCraft holds one name per entry, its ``VT``, so a
    #: text equal to a label alone is not carried and the import judge names that cause.
    short_label: str | None = None


@dataclass
class CompuRationalScaleInfo:
    """One COMPU-SCALE's ``COMPU-RATIONAL-COEFFS``, as the export spelled it.

    The coefficients are kept as their **source literals**, not floats, because "the denominator is 1" is judged
    through ``fractions.Fraction`` on the literal: ``0.1`` is not exactly a float. Both tuples are empty for a
    scale that carries no ``COMPU-RATIONAL-COEFFS``.
    """

    numerator: tuple[str, ...]
    denominator: tuple[str, ...]
    #: The scale's ``LOWER-LIMIT`` / ``UPPER-LIMIT`` texts minus surrounding whitespace and their
    #: ``INTERVAL-TYPE``s, ``None`` when absent. ``[TPS_SWCT_01548]`` reads a ``LINEAR`` scale's limits as the
    #: conversion's scope, the values before it is applied, which the type's ``DATA-CONSTR`` does not state, so
    #: the mapper reads them into the method's ``limits``.
    lower_limit: str | None = None
    lower_interval: str | None = None
    upper_limit: str | None = None
    upper_interval: str | None = None


@dataclass
class CompuMethodInfo:
    """One COMPU-METHOD.

    ``scales`` is only populated for CATEGORY=TEXTTABLE (reduced to an ``enum``). ``rational_scales`` is
    populated for every other category, one entry per ``COMPU-SCALE``, so a LINEAR method's single scale can be
    read as a ``conversion``. ``scale_count`` counts the ``COMPU-SCALE`` children whatever the category, because a
    scale whose coefficients are missing is still a scale.
    """

    path: str
    name: str
    category: str
    scales: list[CompuScaleInfo]
    description: str
    rational_scales: list[CompuRationalScaleInfo] = field(default_factory=list)
    scale_count: int = 0
    #: The method's own ``UNIT-REF`` -- what a ``linear`` element names as its ``unit`` -- ``None`` when it
    #: states none; and whether it states the ``COMPU-PHYS-TO-INTERNAL`` direction, which no element holds, so a
    #: method stating it is not created.
    unit_ref: str | None = None
    phys_to_internal: bool = False


@dataclass
class UnitInfo:
    """One UNIT, imported as a ``unit`` element or read inline onto the type whose ``UNIT-REF`` names it.

    ``display_name`` is the ``DISPLAY-NAME``'s text **as written**, surrounding whitespace included, because the
    writer writes a ``unit`` exactly; ``None`` means no ``DISPLAY-NAME``, so the ``SHORT-NAME`` becomes the type's
    ``unit``. ``arxml_import.merge_verdict.carried_unit_text`` alone decides a blank one; both consumers ask it.
    """

    path: str
    name: str
    display_name: str | None
    description: str
    #: Each of ``FACTOR-SI-TO-UNIT``, ``OFFSET-SI-TO-UNIT`` and ``PHYSICAL-DIMENSION-REF`` the unit states, in the
    #: schema's order, as its tag and its text minus surrounding whitespace -- a relation to the SI system no
    #: ``unit`` element holds, so a unit stating one is not created. The merge compares the texts, so two
    #: declarations of one unit stating different values conflict.
    si_relation: tuple[tuple[str, str], ...] = ()


@dataclass
class DataConstrInfo:
    """One DATA-CONSTR: an application type's ``range``, or a value type's bounds.

    Both limit pairs of the **first** ``DATA-CONSTR-RULE`` are read, and they are two quantities. The
    **physical** limits (``PHYS-CONSTRS``) are the interval an application type is declared within, carried as
    its ``range``. The **internal** limits (``INTERNAL-CONSTRS``) are in an implementation type's own units:
    never an application type's range, and carried as a ``VALUE`` implementation type's ``value_min`` /
    ``value_max`` where it names the constraint (:func:`_carry_value_type_ranges`). ``rule_count`` lets the
    mapper say so when the export declares more than one rule.
    """

    path: str
    name: str
    #: The model value that holds the limit's text exactly (:func:`parse_exact_number`).
    phys_lower: int | float | None
    phys_upper: int | float | None
    rule_count: int
    description: str
    #: Which of ``LOWER-LIMIT`` / ``UPPER-LIMIT`` the first rule declares and this reader could not parse as a
    #: number. ``phys_lower``/``phys_upper`` collapse absent and unreadable onto one ``None``, and an unreadable
    #: lower limit on a closed range would make the re-export silently assert less than the source did.
    unreadable_limits: frozenset[str] = frozenset()
    #: Which of the two the first rule declares as a finite number **no model value holds exactly** -- a float
    #: spelling such as ``16777217.000000001``, which the nearest binary float reads as ``16777217.0``. Such a
    #: bound is not carried: the writer would state a number the source never did.
    inexact_limits: frozenset[str] = frozenset()
    #: The first rule's ``PHYS-CONSTRS`` limit texts as spelled minus surrounding whitespace, ``None`` when
    #: absent, and their ``INTERVAL-TYPE``s: what the merge's limit signature compares and an own-side infinity
    #: (``INFINITE`` or the spelling) is read from.
    phys_lower_text: str | None = None
    phys_upper_text: str | None = None
    phys_lower_interval: str | None = None
    phys_upper_interval: str | None = None
    #: Whether the first rule's ``PHYS-CONSTRS`` holds any element, and its ``INTERNAL-CONSTRS`` -- present or
    #: not, the two limit texts as spelled minus surrounding whitespace with their ``INTERVAL-TYPE``s, and the
    #: tags of every other element it holds (:func:`_carry_value_type_ranges`).
    phys_present: bool = False
    internal_present: bool = False
    internal_lower_text: str | None = None
    internal_upper_text: str | None = None
    internal_lower_interval: str | None = None
    internal_upper_interval: str | None = None
    internal_other_tags: tuple[str, ...] = ()
    #: Every rule's ``INTERNAL-CONSTRS`` with the rule it sits in, reported as unread content unless a ``VALUE``
    #: implementation type reads the constraint (:func:`_carry_value_type_ranges`).
    internal_constrs: tuple[tuple[ET.Element, ET.Element], ...] = field(default=(), compare=False, repr=False)
    #: The first rule's ``CONSTR-LEVEL`` as an integer (:func:`parse_int_literal`), and its text as spelled minus
    #: surrounding whitespace, so an absent level (both ``None``) is told from one no integer states.
    level: int | None = None
    level_text: str | None = None


@dataclass
class DataTypeMapInfo:
    """One DATA-TYPE-MAP entry: an application-type path paired with the implementation-type path it reduces to."""

    application_ref: str
    implementation_ref: str


@dataclass
class DataTypeMappingSetInfo:
    """One DATA-TYPE-MAPPING-SET: the application<->implementation reduction table the type-name normalization reads."""

    path: str
    name: str
    maps: list[DataTypeMapInfo]
    #: The set's ``DESC``. Defaulted so a hand-built info without it still constructs.
    description: str = ""


# ---------------------------------------------------------------------------
# Interface element content (SENDER-RECEIVER-INTERFACE, CLIENT-SERVER-INTERFACE)
#
# Raw TYPE-TREF paths and DIRECTION strings: resolving and reducing them is arxml_import's map_interfaces.
# ---------------------------------------------------------------------------


@dataclass
class SRDataElementInfo:
    """One VARIABLE-DATA-PROTOTYPE inside a SENDER-RECEIVER-INTERFACE's DATA-ELEMENTS block.

    ``init_ref`` is the AUTOSAR path this element's own ``INIT-VALUE/CONSTANT-REFERENCE/CONSTANT-REF`` names, or
    None when the element declares no init value; left raw, like every other ref.

    ``init_unreadable`` separates the two ways ``init_ref`` is None: no INIT-VALUE at all (False), or one this
    reader could not follow (True, already skip-logged). The merge keeps the two apart, so an element that
    declares nothing and one whose declaration was not read are never one element.
    """

    name: str
    type_ref: str
    init_ref: str | None = None
    init_unreadable: bool = False
    #: Carried into ``DataElement.description``.
    description: str = ""
    #: The ``SW-IMPL-POLICY`` and ``SW-CALIBRATION-ACCESS`` of the element's first
    #: ``SW-DATA-DEF-PROPS-CONDITIONAL``, each the literal as spelled minus surrounding whitespace, ``None`` when
    #: absent.
    sw_impl_policy: str | None = None
    sw_calibration_access: str | None = None


@dataclass
class SRInterfaceInfo:
    """One SENDER-RECEIVER-INTERFACE. Maps to SWCraft `sender_receiver`."""

    path: str
    name: str
    description: str
    data_elements: list[SRDataElementInfo]
    #: The ``IS-SERVICE`` text as spelled minus surrounding XML whitespace (:func:`_declared_text`), ``None`` when
    #: absent; the mapper parses it.
    is_service: str | None = None


@dataclass
class CSArgumentInfo:
    """One ARGUMENT-DATA-PROTOTYPE of a CLIENT-SERVER-OPERATION; `direction` is the raw IN/OUT/INOUT."""

    name: str
    type_ref: str
    direction: str
    #: Carried into ``CSArgument.description``.
    description: str = ""
    #: The ``SERVER-ARGUMENT-IMPL-POLICY`` literal as spelled minus surrounding whitespace, ``None`` when absent.
    #: No model field holds it: the mapper compares it with what the writer's output means for the argument.
    server_argument_impl_policy: str | None = None


@dataclass
class CSOperationInfo:
    """One CLIENT-SERVER-OPERATION: a named operation with a list of typed, directional arguments."""

    name: str
    arguments: list[CSArgumentInfo]
    #: Carried into ``CSOperation.description``.
    description: str = ""


@dataclass
class CSInterfaceInfo:
    """One CLIENT-SERVER-INTERFACE. Maps to SWCraft `client_server`."""

    path: str
    name: str
    description: str
    operations: list[CSOperationInfo]
    #: The ``IS-SERVICE`` text, as on :class:`SRInterfaceInfo`.
    is_service: str | None = None


# ---------------------------------------------------------------------------
# SWC element content (ports, runnables, events)
#
# Raw *-INTERFACE-TREF and START-ON-EVENT-REF paths, the raw ARXML tag as `kind`: what maps, what chain-skips and
# what has no SWCraft equivalent is arxml_import's map_swcs. Every kind is captured whether or not it maps.
# ---------------------------------------------------------------------------


def com_spec_block(block_tag: str | None, com_spec_tag: str) -> str:
    """The block a port com-spec sits in: *block_tag*, or the one its kind sits in when a hand-built record states none.

    A sender's com-spec is provided, a receiver's required. The one spelling of that default, which the port-init
    key, the report key of a port-level ``INIT-VALUE`` line and the merge's ordinal must agree on. It lives here,
    beside the records it reads, because ``registry`` and ``merge`` share the import package's first stage and
    neither imports the other.
    """
    return block_tag or ("PROVIDED-COM-SPECS" if "SENDER" in com_spec_tag else "REQUIRED-COM-SPECS")


@dataclass
class PortComSpecInitInfo:
    """One port COM-SPEC's INIT-VALUE, as declared -- not as resolved.

    ``constant_ref`` is the path the COM-SPEC's ``INIT-VALUE/CONSTANT-REFERENCE/CONSTANT-REF`` names. ``value``
    is the value specification written out in place, read as an element's constant is
    (``_read_value_specification``): SWCraft's own writer writes a port's init value that way.
    ``unreadable_reason`` is why neither was read, naming the inner tags found under INIT-VALUE. **Exactly one of
    the three is set**: the entry exists only because an INIT-VALUE is there, and one this importer cannot read
    is a skip-with-reason, never a silent pass.
    """

    port_path: str
    com_spec_tag: str
    data_element_ref: str | None
    constant_ref: str | None
    unreadable_reason: str | None = None
    #: The block the com-spec sits in, ``PROVIDED-COM-SPECS`` or ``REQUIRED-COM-SPECS`` -- the second half of the
    #: report key ``({port}/{block}, INIT-VALUE)``. ``""`` on a hand-built info, which the key derives from the
    #: kind.
    block_tag: str = ""
    #: The value written in place, when the INIT-VALUE holds a value specification other than a
    #: CONSTANT-REFERENCE and it was read.
    value: ConstantValue | None = None
    #: The com-spec's index among every child of its block, the one :attr:`DataElementComSpecInfo.position`
    #: records, so an outcome decided from this INIT-VALUE is bound to this com-spec alone. ``0`` on a hand-built
    #: info.
    position: int = 0
    #: The port's own index among every child of its owner's ``PORTS`` (:attr:`SwcPortInfo.position`): two ports
    #: of one owner with one ``SHORT-NAME`` share a path, and this keeps an outcome decided from one of them off
    #: the other. ``0`` on a hand-built info.
    port_position: int = 0


@dataclass
class DataElementComSpecInfo:
    """One sender or receiver com-spec of a port, as declared.

    Read for every ``NONQUEUED-``/``QUEUED-SENDER-``/``RECEIVER-COM-SPEC`` in the port's ``PROVIDED-COM-SPECS`` or
    ``REQUIRED-COM-SPECS``. ``texts`` holds every direct child in the AUTOSAR namespace that is a leaf **and holds
    text**, keyed by its tag, its text minus the XML whitespace around it (:data:`_NUMBER_PADDING`, never
    ``str.strip()``'s Unicode set, which would read a no-break-space-padded value as the number inside); of a tag
    repeated against the schema the first is kept. An empty leaf (``<QUEUE-LENGTH/>``) states no value and has no
    entry; its tag is still one of ``ComSpecInfo.child_tags``. Nothing is parsed here.

    ``filter_is_silent`` is ``True`` when the com-spec has no ``FILTER``, or one whose only child is
    ``DATA-FILTER-TYPE`` ``ALWAYS`` -- *no filtering is performed*, which is what no filter means -- and
    ``False`` for any other filter. The equivalence holds on a ``NONQUEUED-RECEIVER-COM-SPEC`` alone, the one kind
    both licensed schemas give a ``FILTER``; on another kind any ``FILTER`` is an unread child.
    """

    com_spec_tag: str
    data_element_ref: str | None
    texts: dict[str, str] = field(default_factory=dict)
    filter_is_silent: bool = True
    #: The block the com-spec sits in, ``PROVIDED-COM-SPECS`` or ``REQUIRED-COM-SPECS``: a com-spec in the other
    #: side's block is not the port's, and the mapper carries no value from it. ``""`` on a hand-built info, read
    #: as the port's own block.
    block_tag: str = ""
    #: The com-spec's index among every child of its block, counted as :attr:`PortComSpecInitInfo.position`
    #: counts it. ``0`` on a hand-built info.
    position: int = 0


@dataclass
class ServerComSpecInfo:
    """One ``SERVER-COM-SPEC`` of a port's ``PROVIDED-COM-SPECS``.

    Read for one comparison and written into nothing: the writer emits every server com-spec with a queue length
    of 1, so the importer reports any other source queue length as a loss. ``operation_ref`` is the
    ``OPERATION-REF`` path read raw and ``queue_length`` the ``QUEUE-LENGTH`` text minus surrounding whitespace --
    ``None`` when either is absent. Nothing is parsed here.
    """

    operation_ref: str | None
    queue_length: str | None


@dataclass
class ClientComSpecInfo:
    """One ``CLIENT-COM-SPEC`` of a port's ``REQUIRED-COM-SPECS``.

    Read for one judgement and written into nothing: the writer emits one client com-spec per operation of the
    port's interface (or per operation an external interface stub lists), so the importer reports one naming no
    such operation, and a second for one operation, as not carried. ``operation_ref`` is the ``OPERATION-REF``
    path read raw; ``None`` when absent.
    """

    operation_ref: str | None


@dataclass
class ComSpecInfo:
    """One com-spec of a port, whatever its kind, as its shape.

    ``block_tag`` is ``PROVIDED-COM-SPECS`` or ``REQUIRED-COM-SPECS``, ``tag`` the com-spec's own kind,
    ``child_tags`` the tag of every direct child in document order and ``attributes`` the local name of every
    attribute it carries. The values are **this** com-spec's: ``data`` is set on each of the four sender/receiver
    kinds, ``server`` on a ``SERVER-COM-SPEC`` of the provided block and ``client`` on a ``CLIENT-COM-SPEC`` of the
    required block, read in the one walk that reads the shape, so a shape and its values are never paired by
    position.
    """

    block_tag: str
    tag: str
    child_tags: tuple[str, ...] = ()
    attributes: tuple[str, ...] = ()
    data: DataElementComSpecInfo | None = None
    server: ServerComSpecInfo | None = None
    client: ClientComSpecInfo | None = None
    #: The local name of every direct child **outside** the AUTOSAR namespace, in document order (each also in
    #: ``child_tags``): none is read as a value.
    foreign_child_tags: tuple[str, ...] = ()


@dataclass
class SwcPortInfo:
    """One P-PORT-PROTOTYPE / R-PORT-PROTOTYPE / PR-PORT-PROTOTYPE inside a SWC's PORTS block.

    `kind` is the raw ARXML tag; `interface_ref` is whichever *-INTERFACE-TREF that kind carries
    (PR -> PROVIDED-INTERFACE-TREF, arbitrarily: PR ports have no SWCraft equivalent and are always skipped)."""

    path: str
    name: str
    kind: str
    interface_ref: str | None
    description: str
    # One entry per NONQUEUED-SENDER-/RECEIVER-COM-SPEC on this port that carries an INIT-VALUE.
    com_spec_inits: list[PortComSpecInitInfo] = field(default_factory=list)
    # Every SERVER-COM-SPEC of the port's PROVIDED-COM-SPECS, in source order.
    server_com_specs: list[ServerComSpecInfo] = field(default_factory=list)
    # Every sender/receiver com-spec of the port, in source order.
    data_element_com_specs: list[DataElementComSpecInfo] = field(default_factory=list)
    # Every com-spec of either block, of every kind, as its shape, in document order.
    com_spec_shapes: list[ComSpecInfo] = field(default_factory=list)
    # The port's index among every child of its owner's PORTS, the identity a sibling of the same SHORT-NAME (and
    # so of the same `path`) does not share. 0 on a hand-built info.
    position: int = 0


@dataclass
class SwcAccessInfo:
    """One access point of a runnable.

    A VARIABLE-ACCESS inside DATA-RECEIVE-POINT-BY-ARGUMENTS / DATA-RECEIVE-POINT-BY-VALUES / DATA-SEND-POINTS /
    DATA-READ-ACCESSS / DATA-WRITE-ACCESSS, or a SYNCHRONOUS- or ASYNCHRONOUS-SERVER-CALL-POINT inside
    SERVER-CALL-POINTS. `kind` is the raw *container* tag, which decides `reads`/`writes`/`calls` and the `by`
    form; `tag` is the element's own tag, one of `_ACCESS_CONTAINERS[kind]`, which tells a synchronous call point
    from an asynchronous one inside the same SERVER-CALL-POINTS container.
    `port_ref` / `target_ref` are the raw PORT-PROTOTYPE-REF (CONTEXT-R-PORT-REF for a call) and
    TARGET-DATA-PROTOTYPE-REF (TARGET-REQUIRED-OPERATION-REF) paths; the mapper resolves them. `name` is the
    access SHORT-NAME."""

    path: str
    name: str
    kind: str
    tag: str
    port_ref: str | None
    target_ref: str | None
    #: A call point's ``TIMEOUT`` text minus surrounding XML white space, ``None`` when the point states none.
    timeout_text: str | None = None


@dataclass
class SwcResultPointInfo:
    """One ``ASYNCHRONOUS-SERVER-CALL-RESULT-POINT`` of a runnable.

    ``path`` is ``{runnable}/{SHORT-NAME}``, the path a returns event's ``EVENT-SOURCE-REF`` names; ``tag`` the
    element's own tag. ``call_point_ref`` is the ``ASYNCHRONOUS-SERVER-CALL-POINT-REF`` text as written and
    ``call_point_dest`` its ``DEST``, each ``None`` when absent. Which call point it names, and whether it becomes a
    poll or a ``call_returned`` event's result point, is the mapper's to decide.
    """

    path: str
    name: str
    tag: str
    call_point_ref: str | None = None
    call_point_dest: str | None = None


@dataclass
class SwcLocalAccessInfo:
    """One ``VARIABLE-ACCESS`` of a runnable's two local-variable containers.

    Kept apart from :class:`SwcAccessInfo`, whose every reader resolves a **port**: a local access names a
    variable of its own behaviour instead. ``container`` is the raw container tag (it decides ``reads_variables``
    or ``writes_variables``), ``tag`` the element's own tag. ``reference_forms`` is the local tag of every AUTOSAR
    child of the access's ``ACCESSED-VARIABLE``, in document order: ``AUTOSAR-VARIABLE-REF`` is an
    ``xsd:sequence`` of three optional children at both versions, not a choice, so the mapper judges every form
    the export states. ``variable_ref`` is the ``LOCAL-VARIABLE-REF`` text as written, ``None`` when there is
    none; ``scope`` the ``SCOPE`` literal minus surrounding XML white space, ``None`` when absent -- the model
    holds no scope, and the reader has already reported a stated one. ``name`` is the access ``SHORT-NAME``.
    """

    path: str
    name: str
    container: str
    tag: str
    reference_forms: tuple[str, ...] = ()
    variable_ref: str | None = None
    scope: str | None = None


@dataclass
class SwcRunnableInfo:
    """One RUNNABLE-ENTITY inside a SWC's internal behavior RUNNABLES block.

    `path` is one segment deeper than the owning SWC's path (prefixed by the SWC-INTERNAL-BEHAVIOR's own
    SHORT-NAME, see `_behavior_path`), the real AUTOSAR path `_index_named_element` would assign it. `accesses`
    holds the access containers' entries in document order.

    `can_be_invoked_concurrently` is the runnable's ``CAN-BE-INVOKED-CONCURRENTLY``: ``True`` or ``False`` for
    the xsd:boolean spellings ``true``/``1`` and ``false``/``0``, ``None`` when absent or any other text -- the
    mapper decides what an absent value means. `unreadable_concurrency` tells the two ``None``s apart: the text
    minus surrounding whitespace when present and not an xsd:boolean, ``None`` otherwise.

    `unreadable_min_start_interval` does the same for `min_start_interval`: the ``MINIMUM-START-INTERVAL`` text
    as written when present and :func:`parse_float_literal` reads no finite number from it, so the mapper never
    writes an unreadable text as an absent element's 0.

    `symbol` is the runnable's ``SYMBOL`` **as written**, surrounding whitespace included, ``""`` for a present
    but empty element and ``None`` for an absent one; the reader judges nothing about it."""

    path: str
    name: str
    min_start_interval: float | None
    description: str
    accesses: list[SwcAccessInfo] = field(default_factory=list)
    can_be_invoked_concurrently: bool | None = None
    unreadable_concurrency: str | None = None
    symbol: str | None = None
    unreadable_min_start_interval: str | None = None
    #: The ``READ-LOCAL-VARIABLES`` and ``WRITTEN-LOCAL-VARIABLES`` entries, in document order across the two
    #: containers (:func:`_read_runnable_local_accesses`) -- never in :attr:`accesses`, whose mapper resolves
    #: ports.
    local_accesses: list[SwcLocalAccessInfo] = field(default_factory=list)
    #: Every reference of the ``CAN-ENTER-EXCLUSIVE-AREA-REFS`` and ``RUNS-INSIDE-EXCLUSIVE-AREA-REFS`` wrappers,
    #: in document order across the two (:func:`_read_runnable_area_refs`).
    area_refs: list[SwcAreaRefInfo] = field(default_factory=list)
    #: Every ``ASYNCHRONOUS-SERVER-CALL-RESULT-POINT`` of the runnable, in document order
    #: (:func:`_read_runnable_result_points`).
    result_points: list[SwcResultPointInfo] = field(default_factory=list)


@dataclass
class SwcAreaRefInfo:
    """One reference of a runnable's two exclusive area wrappers.

    ``role`` is the raw wrapper tag, ``CAN-ENTER-EXCLUSIVE-AREA-REFS`` or ``RUNS-INSIDE-EXCLUSIVE-AREA-REFS``
    (``ExecutableEntity``'s ``canEnterExclusiveArea`` and ``runsInsideExclusiveArea``), and ``tag`` the
    reference's own tag. ``ref`` is the reference text as written (``""`` for an empty element) and ``dest`` its
    ``DEST``, ``None`` when absent. A reference has no ``SHORT-NAME``: ``path`` is its runnable's, the key every
    line about it is recorded at, so :meth:`SkipLog.first_at` on a runnable's path can answer a reference's line
    while the runnable is kept; no caller asks it about a runnable's path.
    """

    path: str
    role: str
    tag: str
    ref: str
    dest: str | None


@dataclass
class SwcExclusiveAreaInfo:
    """One ``EXCLUSIVE-AREA`` of a behaviour's ``EXCLUSIVE-AREAS``.

    ``path`` is ``{behaviour}/{SHORT-NAME}``: the wrapper carries no ``SHORT-NAME``, so an area is a direct child
    of the behaviour's name space, the path a ``CAN-ENTER-`` / ``RUNS-INSIDE-EXCLUSIVE-AREA-REF`` names.
    ``description`` is the first ``DESC`` paragraph. Nothing else an area holds is carried: the documentation
    group, a ``DESC`` not carried exactly and ``ADMIN-DATA`` are the index's lines, and a ``VARIATION-POINT`` or
    any other child is :func:`_read_swc_exclusive_areas`' line.
    """

    path: str
    name: str
    description: str = ""


@dataclass
class SwcVariableInfo:
    """One ``VARIABLE-DATA-PROTOTYPE`` of a behaviour's inter-runnable variable wrappers.

    ``role`` is the raw wrapper tag, ``EXPLICIT-INTER-RUNNABLE-VARIABLES`` or ``IMPLICIT-INTER-RUNNABLE-VARIABLES``
    (``[TPS_SWCT_01052]``'s two roles), which decides the variable's ``access``. ``path`` is
    ``{behaviour}/{SHORT-NAME}``, the path a ``LOCAL-VARIABLE-REF`` names. ``type_ref`` / ``type_dest`` are the
    ``TYPE-TREF`` text and its ``DEST``, raw. The initial value is read as a port com-spec's is
    (:func:`_read_port_init_value`): exactly one of ``init_constant_ref``, ``init_value`` and ``init_unreadable``
    is set when the variable declares an ``INIT-VALUE``, none when it declares none. ``sw_impl_policy`` and
    ``sw_calibration_access`` are the first ``SW-DATA-DEF-PROPS-CONDITIONAL``'s literals minus surrounding white
    space, ``None`` when absent; the model carries the calibration access and no policy, and the reader has
    already reported a stated one.
    """

    path: str
    name: str
    role: str
    type_ref: str
    type_dest: str | None
    description: str = ""
    init_constant_ref: str | None = None
    init_value: ConstantValue | None = None
    init_unreadable: str | None = None
    sw_impl_policy: str | None = None
    sw_calibration_access: str | None = None


@dataclass
class DisabledModeInfo:
    """One ``DISABLED-MODE-IREF`` of an event, its three references raw.

    ``context_port_ref`` is bound by the mapper to one of the component's own ports by exact path; the two mode
    references are carried verbatim, because the model declares no mode-switch interface to resolve them against.
    Each is ``None`` when absent.
    """

    context_port_ref: str | None
    mode_group_ref: str | None
    mode_ref: str | None


@dataclass
class SwcEventInfo:
    """One TIMING-EVENT / INIT-EVENT / OPERATION-INVOKED-EVENT / DATA-RECEIVED-EVENT of a SWC's internal behavior.

    `kind` is the raw ARXML tag; `runnable_ref` is the raw START-ON-EVENT-REF path, matched by exact path because
    runnable names are only unique per SWC; `period` is only populated for TIMING-EVENT.

    `context_port_ref` / `target_operation_ref` are the ``CONTEXT-P-PORT-REF`` and
    ``TARGET-PROVIDED-OPERATION-REF`` paths, read at the event's **own** ``OPERATION-IREF`` child and only for an
    OPERATION-INVOKED-EVENT -- `None` for every other kind and for an absent reference."""

    path: str
    name: str
    kind: str
    runnable_ref: str | None
    period: float | None
    context_port_ref: str | None = None
    target_operation_ref: str | None = None
    #: Every ``DISABLED-MODE-IREFS/DISABLED-MODE-IREF`` of the event, in source order, read for every kind.
    disabled_modes: list[DisabledModeInfo] = field(default_factory=list)
    #: The ``PERIOD`` text as written when the element is present and :func:`parse_float_literal` reads no finite
    #: number from it, ``None`` otherwise -- what tells an unreadable period from an absent one.
    unreadable_period: str | None = None
    #: A DATA-RECEIVED-EVENT's ``DATA-IREF``, read at the event's **own** ``DATA-IREF`` child
    #: (:func:`_read_data_iref`): the raw ``CONTEXT-R-PORT-REF`` path and its ``DEST``, and the raw
    #: ``TARGET-DATA-ELEMENT-REF`` path; ``None`` for every other kind and for an absent reference.
    context_r_port_ref: str | None = None
    context_r_port_dest: str | None = None
    target_data_element_ref: str | None = None
    #: An ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT's ``EVENT-SOURCE-REF`` text as written and its ``DEST`` -- the
    #: result point whose return starts the runnable; ``None`` for every other kind and for an absent reference.
    event_source_ref: str | None = None
    event_source_dest: str | None = None


@dataclass(frozen=True)
class PortApiOptionInfo:
    """One ``PORT-API-OPTION`` of a component's first ``SWC-INTERNAL-BEHAVIOR``, as raw facts.

    SWCraft carries no port API option and the writer writes none; the mapper decides which line a source option
    gets once the component's ports are decided. The reader captures what that decision reads:

    - ``container_path`` -- ``{behaviour}/PORT-API-OPTIONS``, the key of an option without a ``PORT-REF``;
    - ``port_ref`` / ``port_ref_dest`` -- the first ``PORT-REF``'s text **as written** (``""`` for an empty
      element) and its ``DEST``; ``None`` when the option has none;
    - ``referenced_kinds`` -- the tag of every port of the component whose path is ``port_ref`` exactly;
    - ``child_tags`` -- every child element's tag, in document order; one outside the AUTOSAR namespace keeps its
      namespace (``{namespace}LOCAL``), so it is never read as a ``PORT-REF`` or a flag;
    - ``stated_attributes`` -- every attribute but a ``PORT-REF``'s ``DEST``, as ``(element tag, attribute)``;
    - ``enable_take_address`` / ``indirect_api`` -- each flag element's text as written, one per occurrence.
    """

    container_path: str
    port_ref: str | None
    port_ref_dest: str | None
    referenced_kinds: tuple[str, ...]
    child_tags: tuple[str, ...]
    stated_attributes: tuple[tuple[str, str], ...]
    enable_take_address: tuple[str, ...]
    indirect_api: tuple[str, ...]


@dataclass
class SwcInfo:
    """One APPLICATION-SW-COMPONENT-TYPE, mapped to SWCraft `swc/{group}/`.

    The importer derives `group` (the immediate parent AR-PACKAGE short-name) from `path`.

    `swcraft_markers` holds the `SD` data of the writer's own `ADMIN-DATA/SDGS/SDG GID="SWCRAFT"` marker group on
    this component's SWC-INTERNAL-BEHAVIOR, keyed by each `SD`'s own `GID` (`{"ACCESS-POINTS": "explicit"}` for an
    access-aware SWCraft export). It is a *capability* signal and no part of the model: it tells an export that
    says "this runnable accesses nothing" from one that cannot say anything at all. Empty for every export that
    carries no such group.
    """

    path: str
    name: str
    description: str
    ports: list[SwcPortInfo]
    runnables: list[SwcRunnableInfo]
    events: list[SwcEventInfo]
    swcraft_markers: dict[str, str] = field(default_factory=dict)
    #: Every ``DATA-TYPE-MAPPING-REF`` this component's SWC-INTERNAL-BEHAVIOR declares, in source order, as the
    #: export spelled it: ``swc.data_type_mappings`` is an ordered list and the export's order is the only one.
    data_type_mapping_refs: list[str] = field(default_factory=list)
    #: The behaviour's ``HANDLE-TERMINATION-AND-RESTART`` as the export spelled it -- one of the schema's three
    #: literals (``arxml_version.HANDLE_TERMINATION_AND_RESTART_LITERALS``' values) -- or ``None`` when the element
    #: is absent or carries a literal outside the three, which :func:`_read_handle_termination_and_restart` has
    #: then recorded as a skip entry.
    handle_termination_and_restart: str | None = None
    #: The ``SHORT-NAME`` of the first ``SWC-INTERNAL-BEHAVIOR`` as spelled, ``None`` when the component has none.
    behavior_name: str | None = None
    #: Every ``PORT-API-OPTION`` of the first behaviour, in document order.
    port_api_options: list[PortApiOptionInfo] = field(default_factory=list)
    #: Every inter-runnable variable of the first behaviour, the explicit wrapper's then the implicit one's, each
    #: in document order (:func:`_read_swc_variables`).
    variables: list[SwcVariableInfo] = field(default_factory=list)
    #: Every exclusive area of the first behaviour, in document order (:func:`_read_swc_exclusive_areas`).
    exclusive_areas: list[SwcExclusiveAreaInfo] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Composition, prototype, and connector element content
#
# Raw CONTEXT-COMPONENT-REF / TARGET-*-PORT-REF / OUTER-PORT-REF paths: resolving them against the hierarchy is
# arxml_import's map_compositions. `ConnectorPortRef` does not distinguish "provider" from "requester" beyond
# which raw field it was read from: the importer takes orientation from each resolved port's actual P/R kind,
# never from the IREF tag.
# ---------------------------------------------------------------------------


@dataclass
class ComponentPrototypeInfo:
    """One SW-COMPONENT-PROTOTYPE inside a composition's COMPONENTS block: a named instance of a component type.

    The type is a leaf SWC or a nested composition; `type_ref` is resolved by the importer, not here.
    `dest` is the ``TYPE-TREF``'s ``DEST`` attribute, read verbatim. It is not derivable from `type_ref`: a
    reference into a package the export does not carry resolves against nothing, and the importer writes both
    strings onto an external stub so the exporter reproduces the reference exactly; a wrong ``DEST`` would be
    XSD-valid and dangling in the target toolchain."""

    path: str
    name: str
    type_ref: str
    dest: str
    #: The prototype's ``DESC``, carried into ``CompositionMember.description``.
    description: str = ""


@dataclass
class ConnectorPortRef:
    """One (component-instance ref, type-level port ref) pair read from one connector endpoint.

    `component_ref` is the path of the SW-COMPONENT-PROTOTYPE the port lives on (an instance inside the SAME
    composition as the owning connector); `port_ref` is the type-level port path on whatever type that prototype
    instantiates -- not an instance-qualified path, matching how PROVIDER-IREF/REQUESTER-IREF/
    INNER-PORT-IREF/OUTER-PORT-REF reference ports in AUTOSAR."""

    component_ref: str | None
    port_ref: str | None
    #: The target port reference's ``DEST`` attribute, verbatim; ``None`` when the reference or the attribute is
    #: absent. An end on an external component stub mints the stub's port from it, so it is read, never inferred:
    #: the writer's own ``DEST`` is fixed by the instance-ref wrapper and would silently rewrite the export.
    dest: str | None = None
    #: The direction the end's own element states -- ``provide`` for ``PROVIDER-IREF`` and a
    #: ``P-PORT-IN-COMPOSITION-INSTANCE-REF`` wrapper, ``require`` for ``REQUESTER-IREF`` and an ``R-`` wrapper,
    #: ``None`` where no end was read. The importer checks ``dest`` against it before minting a stub's port.
    stated_direction: str | None = None


@dataclass
class AssemblyConnectorInfo:
    """One ASSEMBLY-SW-CONNECTOR: connects two component-instance ports within one composition.

    `provider`/`requester` are read from PROVIDER-IREF/REQUESTER-IREF; the naming is kept for traceability only and
    is never trusted for orientation."""

    path: str
    name: str
    provider: ConnectorPortRef
    requester: ConnectorPortRef
    #: Carried into ``Connector.description``.
    description: str = ""


@dataclass
class DelegationConnectorInfo:
    """One DELEGATION-SW-CONNECTOR: a boundary port of the composition itself (`outer_port_ref`) to `inner`."""

    path: str
    name: str
    inner: ConnectorPortRef
    outer_port_ref: str | None
    #: Carried into ``Delegation.description``.
    description: str = ""


@dataclass
class CompositionInfo:
    """One COMPOSITION-SW-COMPONENT-TYPE: boundary ports, instantiated prototypes and its own connectors.

    Composition nesting in AUTOSAR is instantiation-based (a prototype's TYPE-TREF pointing at another
    composition), not XML-nesting-based: every COMPOSITION-SW-COMPONENT-TYPE is a sibling top-level ELEMENTS
    entry, so `ImportModel.compositions` is a flat list."""

    path: str
    name: str
    description: str
    ports: list[SwcPortInfo]
    prototypes: list[ComponentPrototypeInfo]
    assembly_connectors: list[AssemblyConnectorInfo]
    delegation_connectors: list[DelegationConnectorInfo]
    #: The first ``LONG-NAME/L-4`` text, exactly as held, blank included; ``None`` when the composition has no
    #: ``LONG-NAME`` (:func:`_extract_long_name`). A present one becomes the composition's ``title``.
    long_name: str | None = None


# ---------------------------------------------------------------------------
# SWC-TIMING content (the execution-time budgets)
#
# The records keep the ARXML shape -- raw reference paths, the EXECUTION-TIME-TYPE literal as written, a MAXIMUM
# as its two integers -- and swc_mapping decides which shape becomes a runnable's `wcet` and `wcet_type` and which
# is a skip. The reader judges nothing about a budget.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MultidimensionalTimeInfo:
    """One ``MAXIMUM`` or ``MINIMUM`` of a timing constraint: its ``CSE-CODE`` and ``CSE-CODE-FACTOR``.

    ``cse_code`` is the ``CSE-CODE`` read by its own lexical space, ``xsd:unsignedInt`` (:func:`_read_cse_code`),
    ``None`` when absent or no ``unsignedInt``; ``code_text`` is the text as written. ``cse_factor`` is the
    ``CSE-CODE-FACTOR`` read by the ``AR:INTEGER`` grammar (``parse_int_literal(grammar="integer")``), ``None``
    when absent or no integer spelling; ``factor_text`` the text as written. ``factor_overflow`` is
    ``"positive"`` or ``"negative"`` when the text **is** an integer spelling CPython could not convert or write
    back (beyond the interpreter's digit limit), never read as unreadable: the mapper skips a positive one as too
    large and a negative one as not positive.
    ``factor_radix_source`` is the factor's hexadecimal, octal or binary spelling as written (stripped), ``None``
    for a decimal one, so the import report can say the radix was dropped.
    """

    cse_code: int | None
    code_text: str | None
    cse_factor: int | None
    factor_text: str | None
    factor_overflow: Literal["positive", "negative"] | None = None
    factor_radix_source: str | None = None


@dataclass(frozen=True)
class TimingConstraintInfo:
    """One child of an ``SWC-TIMING``'s ``TIMING-REQUIREMENTS`` or ``TIMING-GUARANTEES``.

    ``kind`` is the constraint's own tag and ``role`` the container it sits in (``requirement`` or
    ``guarantee``, TPS GST R4.3.0 Table E.69, R4.2.2 Table E.64). The remaining fields are read for an
    ``EXECUTION-TIME-CONSTRAINT`` only: ``executable_ref`` and ``executable_dest`` the ``EXECUTABLE-REF`` text and
    its ``DEST``, ``execution_time_type`` the stripped ``EXECUTION-TIME-TYPE`` token, ``maximum`` and ``minimum``,
    ``scoping`` the members present among ``TIMING-CONDITION-REF`` (R4.3.0 only), ``COMPONENT-IREF`` and
    ``VARIATION-POINT`` in that order -- each changes what the maximum means -- and ``uncarried`` the
    ``(tag, reason)`` of every identifiable or traceable child the model holds nowhere (``DESC`` with text,
    ``CATEGORY``, ``TRACE-REFS``).
    """

    path: str
    name: str
    kind: str
    role: Literal["requirement", "guarantee"]
    executable_ref: str | None = None
    executable_dest: str | None = None
    execution_time_type: str | None = None
    maximum: MultidimensionalTimeInfo | None = None
    minimum: MultidimensionalTimeInfo | None = None
    scoping: tuple[str, ...] = ()
    uncarried: tuple[tuple[str, str], ...] = ()


@dataclass
class SwcTimingInfo:
    """One ``SWC-TIMING``: its ``BEHAVIOR-REF`` and ``COMPONENT-REF`` as written and its constraints.

    ``variation_point`` is whether the ``SWC-TIMING`` itself carries a ``VARIATION-POINT`` (every budget under it
    is then variant-conditional). ``constraints`` holds the children of ``TIMING-GUARANTEES`` and
    ``TIMING-REQUIREMENTS`` in document order. Everything else the element holds is reported by
    :func:`_read_swc_timing`.
    """

    path: str
    name: str
    behavior_ref: str | None
    component_ref: str | None
    variation_point: bool
    constraints: list[TimingConstraintInfo]


# ---------------------------------------------------------------------------
# CONSTANT-SPECIFICATION content (the init values)
#
# A value tree with the record members' SHORT-LABELs kept but not interpreted; map_interfaces shapes one against
# a declared SWCraft type.
# ---------------------------------------------------------------------------


class ValueForm(StrEnum):
    """The value specification a leaf was read from.

    Recorded so the importer can judge a leaf's form against the type the file declares for its prototype
    (``[constr_1221]``, ``[constr_1225]``, ``[constr_1385]``, ``[constr_1284]``); never part of the value itself.
    """

    #: ``APPLICATION-VALUE-SPECIFICATION``: its one ``SW-VALUES-PHYS/V`` or ``VT``.
    APPLICATION = "application"
    #: ``NUMERICAL-VALUE-SPECIFICATION``: its ``VALUE``.
    NUMERICAL = "numerical"
    #: ``TEXT-VALUE-SPECIFICATION``: its ``VALUE``.
    TEXT = "text"


@dataclass
class ConstantScalar:
    """A leaf value of an application, numerical or text value specification.

    It is APPLICATION-VALUE-SPECIFICATION's single ``SW-VALUE-CONT/SW-VALUES-PHYS/V`` or ``VT``,
    NUMERICAL-VALUE-SPECIFICATION's ``VALUE``, or TEXT-VALUE-SPECIFICATION's ``VALUE``.

    ``value`` is a **number** for a ``V`` and a numerical ``VALUE``, and the **text as written** (a ``str``) for a
    ``VT`` and a text ``VALUE`` -- both are verbatim strings in the schemas, so a text is never trimmed and never
    read as a number. A number is already lexically normalized: ``0xFF`` and ``0377`` (octal) are read as the int
    255, and the source radix is not preserved.

    ``radix_source`` is the raw hexadecimal, octal or binary spelling the value was read from, or None when the
    export wrote it in decimal, so the loss is *reported* rather than hidden: the import report renders one line
    per element whose init lands in a committed interface at a leaf written as a number. An enum-typed leaf is
    written as the literal's name and gets no line. Nothing reads it to re-spell a value; the normalization is
    one-way.
    """

    value: int | float | str
    radix_source: str | None = None
    #: The value specification's own ``SHORT-LABEL`` as spelled, ``None`` when it has none. Not compared: two
    #: values are equal whatever they are labelled.
    label: str | None = field(default=None, compare=False)
    #: The value specification the leaf was read from. Not compared: one number written in the numerical and in
    #: the application form is one value, and the importer judges the form against the leaf's type.
    form: ValueForm = field(default=ValueForm.NUMERICAL, compare=False)
    #: The path an application value's ``SW-VALUE-CONT/UNIT-REF`` names, as written; ``None`` when it states none,
    #: and always ``None`` for the other two forms. Not compared: a value's unit is its type's, and the importer
    #: names a value unit that is not.
    unit_ref: str | None = field(default=None, compare=False)


@dataclass
class ConstantArray:
    """ARRAY-VALUE-SPECIFICATION: its ``ELEMENTS`` children, in document order."""

    members: list[ConstantValue]
    #: The specification's own ``SHORT-LABEL`` (:attr:`ConstantScalar.label`).
    label: str | None = field(default=None, compare=False)


@dataclass
class ConstantRecordField:
    """One RECORD-VALUE-SPECIFICATION member.

    ``label`` is its SHORT-LABEL where it has one. AUTOSAR makes it optional and a written one need not repeat the
    declared field name, so it is recorded and **not** used for matching: position is the only total rule."""

    label: str | None
    value: ConstantValue


@dataclass
class ConstantRecord:
    """RECORD-VALUE-SPECIFICATION: its ``FIELDS`` children, in document order."""

    fields: list[ConstantRecordField]
    #: The specification's own ``SHORT-LABEL`` (:attr:`ConstantScalar.label`).
    label: str | None = field(default=None, compare=False)


#: One value specification: a scalar, an array or a record, which may nest. :func:`_read_value_specification`
#: descends with no depth bound of its own, so a tree past CPython's recursion limit raises instead of being read
#: (see :func:`_read_constant_scalar`).
ConstantValue = ConstantScalar | ConstantArray | ConstantRecord


@dataclass
class ConstantSpecInfo:
    """One CONSTANT-SPECIFICATION.

    Exactly one of ``value`` / ``unreadable_reason`` is set. A shape this reader does not read is neither an
    exception nor a silent None: the reason travels with the constant so whoever dereferences it can report a
    skip naming the real cause, and the data element then imports without its init rather than with a guessed
    one."""

    path: str
    name: str
    value: ConstantValue | None
    unreadable_reason: str | None


@dataclass
class ImportModel:
    """The XML slice this reader extracts from one ARXML file."""

    source_path: str
    # Every AR-PACKAGE path encountered during the walk, in depth-first document order, independent of the
    # `packages` selection (see read_arxml).
    packages: list[str] = field(default_factory=list)
    # AUTOSAR short-name path -> RawElement, for every recognized, selected element (and every named descendant of
    # one) in the file.
    path_index: dict[str, RawElement] = field(default_factory=dict)
    skip_log: SkipLog = field(default_factory=SkipLog)
    sw_base_types: list[SwBaseTypeInfo] = field(default_factory=list)
    impl_data_types: list[ImplDataTypeInfo] = field(default_factory=list)
    app_primitive_data_types: list[AppPrimitiveDataTypeInfo] = field(default_factory=list)
    app_record_data_types: list[AppRecordDataTypeInfo] = field(default_factory=list)
    app_array_data_types: list[AppArrayDataTypeInfo] = field(default_factory=list)
    compu_methods: list[CompuMethodInfo] = field(default_factory=list)
    # Read for the application type's `unit` and `range`, resolved by path.
    units: list[UnitInfo] = field(default_factory=list)
    data_constrs: list[DataConstrInfo] = field(default_factory=list)
    data_type_mapping_sets: list[DataTypeMappingSetInfo] = field(default_factory=list)
    sr_interfaces: list[SRInterfaceInfo] = field(default_factory=list)
    cs_interfaces: list[CSInterfaceInfo] = field(default_factory=list)
    swcs: list[SwcInfo] = field(default_factory=list)
    compositions: list[CompositionInfo] = field(default_factory=list)
    # A constant is not an SWCraft element; it is read so an INIT-VALUE reference can be dereferenced.
    constant_specs: list[ConstantSpecInfo] = field(default_factory=list)
    # The runnables' execution-time budgets.
    swc_timings: list[SwcTimingInfo] = field(default_factory=list)
    # The basename of the XSD the root's `xsi:schemaLocation` names, exactly as the file writes it
    # (`AUTOSAR_4-2-2.xsd`, `AUTOSAR_00051.xsd`, ...), or None when the attribute is absent or selects no token. A
    # LABEL carried to the import report's citations, never a version branch.
    declared_schema: str | None = None
    # Whether the file carries at least one VARIATION-POINT in the AUTOSAR
    # namespace. A variant-rich file may repeat a shortName ([TPS_GST_00251]:
    # [constr_2508] is substituted by [constr_2512]), and SWCraft binds no
    # variant, so a duplicate name in such a file is not cited as a defect.
    variant_rich: bool = False

    @property
    def declared_release(self) -> ArxmlVersion | None:
        """The release SWCraft writes whose schema file :attr:`declared_schema` names, else ``None``.

        ``None`` means *no release SWCraft writes*: :attr:`declared_schema` says whether the file named another
        schema or none at all.
        """
        return release_of_schema_filename(self.declared_schema)


def _first_paragraph_text(el: ET.Element, wrapper: str, paragraph: str) -> str:
    """The text of *wrapper*'s first *paragraph* child on `el`, exactly as the file holds it.

    ``""`` when `el` has no *wrapper*, the wrapper no *paragraph*, or the paragraph no text node. The text is
    **never stripped**: the writer writes a description as held, whitespace included. What the paragraph holds
    besides its first text node (an inline child, a second language) is not read here.
    """
    wrapper_el = el.find(wrapper)
    if wrapper_el is None:
        return ""
    paragraph_el = wrapper_el.find(paragraph)
    if paragraph_el is None or paragraph_el.text is None:
        return ""
    return paragraph_el.text


def _extract_description(el: ET.Element, path: str | None = None, skip_log: SkipLog | None = None) -> str:
    """First DESC/L-2 text on `el`, exactly as held, or "" if it has no DESC.

    The first language variant wins. An empty ``<L-2 L="FOR-ALL"/>`` is ``""``, no description; a text is read
    unstripped (:func:`_first_paragraph_text`).

    Given *path* -- the key the entry is recorded at: the element's own path, or
    ``{compu method}/COMPU-INTERNAL-TO-PHYS`` for an enumeration literal's ``COMPU-SCALE`` -- and *skip_log*, what
    the first ``L-2``'s text does not carry is one line (:func:`_report_text_form`). A caller without a log is an
    indexed element's second read, which :func:`_index_named_element` has already judged.
    """
    if path is not None and skip_log is not None:
        _report_text_form(el, _DESC, _L2, path, skip_log)
    return _first_paragraph_text(el, _DESC, _L2)


def _extract_long_name(el: ET.Element, path: str | None = None, skip_log: SkipLog | None = None) -> str | None:
    """First LONG-NAME/L-4 text on `el`, exactly as held; ``None`` when `el` has no LONG-NAME.

    Read for a composition only, whose ``title`` it becomes whenever the composition **has** a ``LONG-NAME`` -- a
    blank text included, because the writer writes a title that differs from the name exactly, blank or not. A
    ``LONG-NAME`` whose first ``L-4`` holds no text is ``""``. ``None`` is the one case the title falls back on
    the ``SHORT-NAME``. Read and judged the way a ``DESC`` is when *path* and *skip_log* are given.
    """
    if el.find(_LONG_NAME) is None:
        return None
    if path is not None and skip_log is not None:
        _report_text_form(el, _LONG_NAME, _L4, path, skip_log)
    return _first_paragraph_text(el, _LONG_NAME, _L4)


#: The one ``L`` the writer states (``arxml_xml.set_description``): the schema's
#: language-neutral value, spelled as its enumeration spells it.
_FOR_ALL = "FOR-ALL"


def _text_form_loss(wrappers: list[ET.Element], paragraph: str) -> str | None:
    """What a ``DESC`` / ``LONG-NAME`` holds beyond its first paragraph's plain text, or ``None``.

    The reader carries only the text node of the first ``L-2`` (``L-4``). Everything else the schema admits is
    named here, in a closed order, one phrase per shape: a second wrapper, an attribute on it (``S``, ``T``), a
    child other than the paragraph, a second paragraph, an element inside a paragraph (``BR``, ``FT``, ``TT``,
    ``XREF``, ``TRACE-REF``, ``E``, ``SUB``, ``SUP``, ``IE``, ``XREF-TARGET``), an attribute on a paragraph other
    than ``L``, and -- last -- an ``L`` other than ``FOR-ALL``.

    The writer states ``FOR-ALL``, so a paragraph given in one language would come back as applicable to all of
    them. That is a line whenever the paragraph is written back: a ``DESC``'s with text (an empty one is no
    ``DESC`` at all), and a ``LONG-NAME``'s always, because a composition's blank title is written too.
    """
    wrapper = wrappers[0]
    children = [child for child in list(wrapper) if isinstance(child.tag, str)]
    paragraphs = [child for child in children if child.tag == paragraph]
    if len(wrappers) > 1:
        return f"a second {local_tag(wrapper.tag)}"
    if wrapper.attrib:
        return f"an attribute on {local_tag(wrapper.tag)}"
    if len(paragraphs) != len(children):
        return f"a child other than {local_tag(paragraph)}"
    if len(paragraphs) > 1:
        return f"a second {local_tag(paragraph)}"
    for item in paragraphs:
        if len(item):
            return f"inline markup inside {local_tag(paragraph)}"
        if set(item.attrib) - {"L"}:
            return f"an attribute other than L on {local_tag(paragraph)}"
    for item in paragraphs:
        if item.get("L", _FOR_ALL) != _FOR_ALL and (item.text or paragraph == _L4):
            return f"a language other than {_FOR_ALL} on {local_tag(paragraph)}"
    return None


def _report_text_form(
    el: ET.Element, wrapper: str, paragraph: str, path: str, skip_log: SkipLog, *, uncarried: str | None = None
) -> None:
    """One family F14 line when *el*'s ``DESC`` / ``LONG-NAME`` is not carried exactly.

    Descriptive: printed by ``import component``, never an abort. Recorded at most once per ``(path, tag, reason)``
    (:attr:`SkipLog.text_forms_reported`), because an indexed element's text is read twice.

    *uncarried* is the reason for a text the reader reads nowhere at all -- a ``LINEAR`` compu scale's, which no
    model field holds -- recorded whenever the first paragraph has text, in place of the form's reason.
    """
    wrappers = el.findall(wrapper)
    if not wrappers:
        return
    loss = _text_form_loss(wrappers, paragraph)
    tag = local_tag(wrapper)
    if uncarried is not None and _first_paragraph_text(el, wrapper, paragraph):
        reason = uncarried
    elif loss is not None:
        reason = (
            f"{tag} not carried exactly: it holds {loss} -- SWCraft carries the first "
            f"{local_tag(paragraph)}'s text alone"
        )
    else:
        return
    key = (path, tag, reason)
    if key in skip_log.text_forms_reported:
        return
    skip_log.text_forms_reported.add(key)
    skip_log.record(path, tag, reason, category=SkipCategory.NOT_SUPPORTED, effect=REPORT)


#: The reasons of the ``DESC``s the reader reads and the model has no field for. Each is recorded only when
#: :func:`_extract_description` is not ``""``: an empty ``DESC`` loses nothing.
_EVENT_DESC_REASON = "DESC not carried: the event's name is derived and the model holds no text for it"
_ACCESS_POINT_DESC_REASON = "DESC not carried: the access point's name is derived and the model holds no text for it"
_ARRAY_ELEMENT_DESC_REASON = "DESC not carried: the array's element is not a model element"


def _report_uncarried_description(description: str, path: str, reason: str, skip_log: SkipLog) -> None:
    """One ``NOT_SUPPORTED`` entry, tagged ``DESC``, when *description* (an :func:`_extract_description`) is not empty.

    The one record site for a ``DESC`` the reader reads and no model field holds: an event's, an access point's, an
    application array's ``ELEMENT``'s and an implementation array's element's. The entry is at the owning
    element's own path and aborts ``import component``.
    """
    if description:
        skip_log.record(path=path, tag="DESC", reason=reason, category=SkipCategory.NOT_SUPPORTED, effect="abort")


# ---------------------------------------------------------------------------
# The reader's report families and the closed child tables
# ---------------------------------------------------------------------------
#
# Every value the importer does not carry is one entry of one family -- unless it equals what SWCraft's output
# states for the element, in which case it is carried by equivalence and gets no line. An entry's key is one
# ``(path, tag)`` pair: at a referrable, its own path and the child's (or attribute's) tag; for content nested in
# non-referrable structure, the nearest referrable's path, ``/`` and the first child tag under it --
# ``{type}/SW-DATA-DEF-PROPS``, ``{compu method}/COMPU-INTERNAL-TO-PHYS``, ``{constraint}/DATA-CONSTR-RULES``,
# ``{event}/DISABLED-MODE-IREFS``. Each family is one record site, and each states its effect: ``ABORT`` for a
# behavioural value (it changes what an RTE or an integrator does, so ``import component`` refuses it) and
# ``REPORT`` for a descriptive one (printed and imported past).

#: Every ``Identifiable``'s documentation group, as the closed tables classify it: the name and the ``DESC`` are
#: read (a ``DESC`` not carried exactly is F14), ``ADMIN-DATA`` is the index's (:func:`_index_named_element`),
#: and the rest -- with the ``UUID`` attribute -- is family F13. ``S`` and ``T`` (``AR-OBJECT``'s checksum and
#: timestamp) are read by no one.
_IDENTIFIABLE_TABLE: dict[str, str] = {
    "SHORT-NAME": "read",
    "SHORT-NAME-FRAGMENTS": "F13",
    "LONG-NAME": "F13",
    "DESC": "read|F14",
    "INTRODUCTION": "F13",
    "ANNOTATIONS": "F13",
    "ADMIN-DATA": "index",
    "VARIATION-POINT": "unread",
    "@UUID": "F13",
    "@S": "unread",
    "@T": "unread",
}

#: The closed child tables: for each covered owner, every child element and attribute (``@``) the 4.2.2 and 4.3.0
#: schemas admit (``xsd_child_sets.json``, held by ``test_closed_child_tables.py``), and what the reader does with
#: it. ``read`` -- read into a record; ``equivalent`` -- carried by equivalence (the value is what the writer writes
#: back); ``index`` -- reported by :func:`_index_named_element`; ``F1``..``F22`` -- reported under that family;
#: ``unread`` -- reported by :func:`_report_closed_table` through :func:`_report_unread_child` (one entry per item)
#: and :func:`_report_unread_attribute`. ``a|b`` is one or the other by the value or the owner's context, which the
#: reading site decides. A tag or attribute the table does not name is ``unread`` too.
#:
#: What the tables do not reach: an owner not listed. Its unread children stay what the other closed tables (a
#: behaviour's, a runnable's) and the index report.
CLOSED_CHILD_TABLES: dict[str, dict[str, str]] = {
    "AR-PACKAGE": {
        "SHORT-NAME": "read",
        "ELEMENTS": "read",
        "AR-PACKAGES": "read",
        **{
            tag: "F15"
            for tag in (
                "SHORT-NAME-FRAGMENTS",
                "LONG-NAME",
                "DESC",
                "CATEGORY",
                "ADMIN-DATA",
                "INTRODUCTION",
                "ANNOTATIONS",
                "VARIATION-POINT",
                "REFERENCE-BASES",
                "BLUEPRINT-POLICYS",
                "SHORT-NAME-PATTERN",
                "@UUID",
                "@S",
                "@T",
            )
        },
    },
    "VARIABLE-DATA-PROTOTYPE": {
        **_IDENTIFIABLE_TABLE,
        "CATEGORY": "unread",
        "SW-DATA-DEF-PROPS": "read",
        "TYPE-TREF": "read",
        "INIT-VALUE": "read",
    },
    "ARGUMENT-DATA-PROTOTYPE": {
        **_IDENTIFIABLE_TABLE,
        "CATEGORY": "unread",
        "SW-DATA-DEF-PROPS": "read",
        "TYPE-TREF": "read",
        "TYPE-BLUEPRINTS": "unread",
        "DIRECTION": "read",
        "SERVER-ARGUMENT-IMPL-POLICY": "equivalent|F6",
    },
    "IMPLEMENTATION-DATA-TYPE": {
        **_IDENTIFIABLE_TABLE,
        "CATEGORY": "read",
        "BLUEPRINT-POLICYS": "unread",
        "SHORT-NAME-PATTERN": "unread",
        "DYNAMIC-ARRAY-SIZE-PROFILE": "unread",
        "SUB-ELEMENTS": "read",
        "SW-DATA-DEF-PROPS": "read",
        "SYMBOL-PROPS": "unread",
        "TYPE-EMITTER": "unread",
    },
    "IMPLEMENTATION-DATA-TYPE-ELEMENT": {
        **_IDENTIFIABLE_TABLE,
        "CATEGORY": "read",
        "ARRAY-SIZE": "read",
        "ARRAY-SIZE-HANDLING": "read",
        "ARRAY-SIZE-SEMANTICS": "read",
        "SUB-ELEMENTS": "read",
        "SW-DATA-DEF-PROPS": "read",
    },
    "SENDER-RECEIVER-INTERFACE": {
        **_IDENTIFIABLE_TABLE,
        "CATEGORY": "unread",
        "BLUEPRINT-POLICYS": "unread",
        "SHORT-NAME-PATTERN": "unread",
        "IS-SERVICE": "read",
        "SERVICE-KIND": "unread",
        "DATA-ELEMENTS": "read",
        "INVALIDATION-POLICYS": "unread",
    },
    "CLIENT-SERVER-INTERFACE": {
        **_IDENTIFIABLE_TABLE,
        "CATEGORY": "unread",
        "BLUEPRINT-POLICYS": "unread",
        "SHORT-NAME-PATTERN": "unread",
        "IS-SERVICE": "read",
        "SERVICE-KIND": "unread",
        "OPERATIONS": "read",
        "POSSIBLE-ERRORS": "F7",
    },
    "TIMING-EVENT": {
        **_IDENTIFIABLE_TABLE,
        # An event's own DESC is read and reported whole: the model holds no text for a derived name.
        "CATEGORY": "unread",
        "ACTIVATION-REASON-REPRESENTATION-REF": "unread",
        "DISABLED-MODE-IREFS": "read|F16",
        "START-ON-EVENT-REF": "read",
        "PERIOD": "read",
    },
    "R-MODE-IN-ATOMIC-SWC-INSTANCE-REF": {
        "CONTEXT-PORT-REF": "read",
        "CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF": "read",
        "TARGET-MODE-DECLARATION-REF": "read",
        "@S": "unread",
        "@T": "unread",
    },
    # The timing event's shape. The event's own VARIATION-POINT is read -- one loss line at the event
    # (`_report_access_variation_point`), never one per item.
    "DATA-RECEIVED-EVENT": {
        **_IDENTIFIABLE_TABLE,
        "VARIATION-POINT": "read",
        "CATEGORY": "unread",
        "ACTIVATION-REASON-REPRESENTATION-REF": "unread",
        "DISABLED-MODE-IREFS": "read|F16",
        "START-ON-EVENT-REF": "read",
        "DATA-IREF": "read",
    },
    # The event's DATA-IREF, the disabled-mode IREF's shape.
    "R-VARIABLE-IN-ATOMIC-SWC-INSTANCE-REF": {
        "CONTEXT-R-PORT-REF": "read",
        "TARGET-DATA-ELEMENT-REF": "read",
        "@S": "unread",
        "@T": "unread",
    },
    "DATA-CONSTR-RULE": {
        "CONSTR-LEVEL": "F9",
        "PHYS-CONSTRS": "read",
        # An application type's range is its PHYS-CONSTRS and a value type's its INTERNAL-CONSTRS
        # ([TPS_SWCT_01288]). What no VALUE implementation type reads is reported by `_carry_value_type_ranges`.
        "INTERNAL-CONSTRS": "read",
        "@S": "unread",
        "@T": "unread",
    },
    "COMPU-SCALE": {
        # TEXTTABLE: the literal is VT and LOWER-LIMIT; the writer writes the SHORT-LABEL as the literal and
        # UPPER-LIMIT equal to LOWER-LIMIT, so a label or an upper limit that says the same is carried, and one that
        # does not is F10 / F11. LINEAR: the writer writes the coefficients and the limits the method carries, so a
        # limit is read or F23, and a label is F10's line in section 1 -- `[constr_1375]` admits none there.
        "SHORT-LABEL": "equivalent|F10",
        "SYMBOL": "unread",
        "DESC": "read|F14",
        "MASK": "unread",
        "LOWER-LIMIT": "read|F11|F23",
        "UPPER-LIMIT": "equivalent|read|F11|F23",
        "COMPU-INVERSE-VALUE": "unread",
        "COMPU-CONST": "read|unread",
        "COMPU-RATIONAL-COEFFS": "read|unread",
        "VARIATION-POINT": "unread",
        "@S": "unread",
        "@T": "unread",
    },
    "DESC": {"L-2": "read|F14", "@S": "F14", "@T": "F14"},
    "L-2": {
        **{
            tag: "F14"
            for tag in ("BR", "E", "FT", "IE", "SUB", "SUP", "TRACE-REF", "TT", "XREF", "XREF-TARGET", "@S", "@T")
        },
        "@L": "read",
    },
    "LONG-NAME": {"L-4": "read|F14", "@S": "F14", "@T": "F14"},
    "L-4": {**{tag: "F14" for tag in ("E", "IE", "SUB", "SUP", "TT", "@S", "@T")}, "@L": "read"},
    # Every com-spec child is read, carried by equivalence (the writer's silent value), or family F4, decided by
    # the mapper; every com-spec of a port on an external interface stub is F5.
    **{
        tag: {
            **{child: "F4" for child in children},
            **dict.fromkeys(read, "read|F4"),
            "@S": "F4",
            "@T": "F4",
        }
        for tag, children, read in (
            (
                "NONQUEUED-RECEIVER-COM-SPEC",
                (
                    "COMPOSITE-NETWORK-REPRESENTATIONS",
                    "EXTERNAL-REPLACEMENT-REF",
                    "HANDLE-DATA-STATUS",
                    "HANDLE-OUT-OF-RANGE-STATUS",
                    "MAX-DELTA-COUNTER-INIT",
                    "MAX-NO-NEW-OR-REPEATED-DATA",
                    "NETWORK-REPRESENTATION",
                    "REPLACE-WITH",
                    "SYNC-COUNTER-INIT",
                    "TIMEOUT-SUBSTITUTION-VALUE",
                    "TRANSFORMATION-COM-SPEC-PROPSS",
                ),
                (
                    "DATA-ELEMENT-REF",
                    "INIT-VALUE",
                    "ALIVE-TIMEOUT",
                    "ENABLE-UPDATE",
                    "FILTER",
                    "HANDLE-NEVER-RECEIVED",
                    "HANDLE-OUT-OF-RANGE",
                    "HANDLE-TIMEOUT-TYPE",
                    "USES-END-TO-END-PROTECTION",
                ),
            ),
            (
                "NONQUEUED-SENDER-COM-SPEC",
                ("COMPOSITE-NETWORK-REPRESENTATIONS", "NETWORK-REPRESENTATION", "TRANSMISSION-ACKNOWLEDGE"),
                ("DATA-ELEMENT-REF", "INIT-VALUE", "HANDLE-OUT-OF-RANGE", "USES-END-TO-END-PROTECTION"),
            ),
            (
                "QUEUED-RECEIVER-COM-SPEC",
                (
                    "COMPOSITE-NETWORK-REPRESENTATIONS",
                    "EXTERNAL-REPLACEMENT-REF",
                    "HANDLE-OUT-OF-RANGE-STATUS",
                    "MAX-DELTA-COUNTER-INIT",
                    "MAX-NO-NEW-OR-REPEATED-DATA",
                    "NETWORK-REPRESENTATION",
                    "REPLACE-WITH",
                    "SYNC-COUNTER-INIT",
                    "TRANSFORMATION-COM-SPEC-PROPSS",
                ),
                ("DATA-ELEMENT-REF", "HANDLE-OUT-OF-RANGE", "QUEUE-LENGTH", "USES-END-TO-END-PROTECTION"),
            ),
            (
                "QUEUED-SENDER-COM-SPEC",
                ("COMPOSITE-NETWORK-REPRESENTATIONS", "NETWORK-REPRESENTATION", "TRANSMISSION-ACKNOWLEDGE"),
                ("DATA-ELEMENT-REF", "HANDLE-OUT-OF-RANGE", "USES-END-TO-END-PROTECTION"),
            ),
            ("SERVER-COM-SPEC", ("TRANSFORMATION-COM-SPEC-PROPSS",), ("OPERATION-REF", "QUEUE-LENGTH")),
            ("CLIENT-COM-SPEC", ("TRANSFORMATION-COM-SPEC-PROPSS",), ("OPERATION-REF",)),
        )
    },
    # A FILTER is carried by equivalence exactly when it holds DATA-FILTER-TYPE ALWAYS and nothing else on a
    # NONQUEUED-RECEIVER-COM-SPEC, the one kind with a place for it; every other filter is one F4 line.
    "DATA-FILTER": {
        "DATA-FILTER-TYPE": "equivalent|F4",
        **{tag: "F4" for tag in ("MASK", "MAX", "MIN", "OFFSET", "PERIOD", "X", "@S", "@T")},
    },
    # Every child of an SWC-TIMING is read: its references, constraint containers and own VARIATION-POINT for the
    # mapper, its descriptions, conditions, resource, DESC and CATEGORY for the reader's own descriptive lines.
    # The documentation group is the index's.
    "SWC-TIMING": {
        **_IDENTIFIABLE_TABLE,
        "VARIATION-POINT": "read",
        "CATEGORY": "read",
        "TIMING-CONDITIONS": "read",
        "TIMING-DESCRIPTIONS": "read",
        "TIMING-GUARANTEES": "read",
        "TIMING-REQUIREMENTS": "read",
        "TIMING-RESOURCE": "read",
        "BEHAVIOR-REF": "read",
        "COMPONENT-REF": "read",
    },
    # Every member the mapper decides on and the traceable children it reports for a carried constraint.
    "EXECUTION-TIME-CONSTRAINT": {
        **_IDENTIFIABLE_TABLE,
        "VARIATION-POINT": "read",
        "CATEGORY": "read",
        "TRACE-REFS": "read",
        "TIMING-CONDITION-REF": "read",
        "COMPONENT-IREF": "read",
        "EXECUTABLE-REF": "read",
        "EXECUTION-TIME-TYPE": "read",
        "MAXIMUM": "read",
        "MINIMUM": "read",
    },
}

#: The owners whose own ``VARIATION-POINT`` is read rather than ``unread``: an ``SWC-TIMING``'s and an
#: execution-time constraint's, each a skip with its own reason in the mapper, and a ``DATA-RECEIVED-EVENT``'s, one
#: loss line at the event (:func:`_report_access_variation_point`).
VARIATION_POINT_READ_OWNERS = frozenset({"SWC-TIMING", "EXECUTION-TIME-CONSTRAINT", "DATA-RECEIVED-EVENT"})

#: The owners whose ``ADMIN-DATA`` the index reports as descriptive: a budget carries no administrative data, and
#: losing it changes no budget.
_DESCRIPTIVE_ADMIN_DATA_OWNERS = frozenset({"SWC-TIMING", "EXECUTION-TIME-CONSTRAINT"})

#: The ``SW-DATA-DEF-PROPS-CONDITIONAL`` tables, one per owner it is read on. A child an owner's table does not
#: name is family F3; the application primitive's other children are reported by
#: :func:`_read_app_primitive_data_type`.
CONDITIONAL_TABLES: dict[str, dict[str, str]] = {
    "VARIABLE-DATA-PROTOTYPE": {
        "SW-CALIBRATION-ACCESS": "read|F22|F3",
        "SW-IMPL-POLICY": "read|F21|F3",
    },
    "ARGUMENT-DATA-PROTOTYPE": {"SW-CALIBRATION-ACCESS": "equivalent|F1"},
    "IMPLEMENTATION-DATA-TYPE": {
        "BASE-TYPE-REF": "read",
        "IMPLEMENTATION-DATA-TYPE-REF": "read",
        "SW-POINTER-TARGET-PROPS": "read",
        "COMPU-METHOD-REF": "F2",
        # Read on a VALUE type, whose range it is; F17 on any other category, which has no place for one.
        "DATA-CONSTR-REF": "read|F17",
        "SW-CALIBRATION-ACCESS": "equivalent|F1",
    },
    "IMPLEMENTATION-DATA-TYPE-ELEMENT": {
        "BASE-TYPE-REF": "read",
        "IMPLEMENTATION-DATA-TYPE-REF": "read",
        "SW-CALIBRATION-ACCESS": "equivalent|F1",
    },
    "APPLICATION-PRIMITIVE-DATA-TYPE": {
        "COMPU-METHOD-REF": "read",
        "UNIT-REF": "read",
        "DATA-CONSTR-REF": "read",
    },
}

#: The literals a data element's ``SW-IMPL-POLICY`` and ``SW-CALIBRATION-ACCESS`` are carried as, restated from
#: ``arxml_import.type_mapping``'s tables because the reader must not import the mapper;
#: ``packages/tools/tests/test_uncarried_losses.py`` pins the two equal.
CARRIED_SW_IMPL_POLICIES = frozenset({"STANDARD", "QUEUED"})
CARRIED_SW_CALIBRATION_ACCESSES = frozenset({"NOT-ACCESSIBLE", "READ-ONLY", "READ-WRITE"})
#: ``[constr_1017]`` over the two policies the model carries: ``QUEUED`` supports ``NOT-ACCESSIBLE`` alone
#: (Table 5.49 at R4.2.2, 5.50 at R4.3.0).
_QUEUED_SUPPORTED_ACCESS = "NOT-ACCESSIBLE"
#: The standard's default of ``swCalibrationAccess`` (``[TPS_SWCT_01559]``), which the writer states on an
#: argument and an implementation type: equal to it is carried by equivalence.
_DEFAULT_CALIBRATION_ACCESS = "NOT-ACCESSIBLE"


def _holds_text(el: ET.Element) -> bool:
    """Whether *el* holds text: a multi-language paragraph's text node, or any other non-blank character data.

    A paragraph (``L-1`` .. ``L-10``) is read **exactly** (:func:`_first_paragraph_text`), so its text node is a
    value whatever it holds, white space alone included. Everywhere else in *el* white space is the file's layout.
    """
    for node in el.iter():
        if local_tag(node.tag) in _PARAGRAPH_TAGS and node.text:
            return True
    return bool("".join(el.itertext()).strip())


def _carries_nothing(el: ET.Element) -> bool:
    """Whether *el* holds no text and no element but empty multi-language paragraphs.

    An ``<L-2 L="FOR-ALL"/>`` or an empty container holds nothing to lose, so a family reporting documentation
    records nothing for it. A paragraph of white space is text (:func:`_holds_text`).
    """
    if _holds_text(el):
        return False
    for node in el.iter():
        if node is el:
            if set(node.attrib):
                return False
            continue
        if local_tag(node.tag) not in _PARAGRAPH_TAGS or set(node.attrib) - {"L"}:
            return False
    return True


_PARAGRAPH_TAGS = frozenset({"L-1", "L-2", "L-3", "L-4", "L-5", "L-6", "L-7", "L-8", "L-9", "L-10"})


def _report_unread_attribute(path: str, attribute: str, owner_tag: str, skip_log: SkipLog) -> None:
    """One entry for an attribute a closed table names ``unread``: ``S``, ``T`` or a later one.

    Keyed as the attribute's element, with the attribute's name as the tag. Behavioural, as every unread child of
    a closed table is: the reader cannot tell what it means.
    """
    skip_log.record(
        path,
        attribute,
        f"attribute {attribute} of a {owner_tag} is not read by SWCraft -- not imported or preserved",
        category=SkipCategory.NOT_SUPPORTED,
        effect=ABORT,
    )


def _report_closed_table(el: ET.Element, path: str, owner_tag: str, skip_log: SkipLog) -> None:
    """Report every child and attribute of *el* its closed table names ``unread``.

    *path* is the key the entries are recorded at: the owner's own path when it is referrable, else the nearest
    referrable's path and the first child tag under it. The children the table sends to a family or the index are
    that site's; this reports the rest through :func:`_report_unread_child` (one entry per item of a container,
    none for an empty child). A tag the table does not name is ``unread`` too.
    """
    table = CLOSED_CHILD_TABLES[owner_tag]
    kept = _first_claimants(el)
    for child in list(el):
        if not _in_autosar_namespace(child):
            continue
        if table.get(local_tag(child.tag), "unread") == "unread":
            _report_unread_child(child, path, owner_tag, kept, skip_log)
    for attribute in el.attrib:
        name = local_tag(attribute)
        if table.get(f"@{name}", "unread") == "unread":
            _report_unread_attribute(path, name, owner_tag, skip_log)


def _report_uncarried_calibration_access(path: str, owner_tag: str, literal: str, skip_log: SkipLog) -> None:
    """Family F1: ``SW-CALIBRATION-ACCESS`` other than ``NOT-ACCESSIBLE`` where the model holds none.

    On an argument, an implementation type or an implementation element the writer states ``NOT-ACCESSIBLE``
    (``[TPS_SWCT_01559]``'s default), so that value is carried by equivalence; any other is a
    measurement-and-calibration visibility the model does not carry. Descriptive (``[TPS_SWCT_01274]``).
    """
    skip_log.record(
        f"{path}/SW-DATA-DEF-PROPS",
        "SW-CALIBRATION-ACCESS",
        f"SW-CALIBRATION-ACCESS {literal} on a {owner_tag} not carried: SWCraft writes NOT-ACCESSIBLE there",
        category=SkipCategory.NOT_SUPPORTED,
        effect=REPORT,
    )


def _report_implementation_compu_method(path: str, skip_log: SkipLog) -> None:
    """Family F2: an implementation type's own ``COMPU-METHOD-REF``. Behavioural.

    Recorded for every such reference; the importers carry the reference to a ``texttable`` element and leave this
    line out of their reports, so the sentence is what a reader-level consumer of the skip log sees.
    """
    skip_log.record(
        f"{path}/SW-DATA-DEF-PROPS",
        "COMPU-METHOD-REF",
        "COMPU-METHOD-REF of an IMPLEMENTATION-DATA-TYPE not carried: SWCraft writes a compu method on the "
        "application type only",
        category=SkipCategory.NOT_SUPPORTED,
        effect=ABORT,
    )


def _report_unread_property(path: str, tag: str, reason: str, skip_log: SkipLog) -> None:
    """Family F3: a ``SW-DATA-DEF-PROPS`` child, attribute or further conditional the closed table does not read.

    Behavioural: a property of the data definition (an address method, a
    record layout, an invalid value, ...) the model has no field for.
    """
    skip_log.record(f"{path}/SW-DATA-DEF-PROPS", tag, reason, category=SkipCategory.NOT_SUPPORTED, effect=ABORT)


def _report_value_type_constraint(
    path: str, what: str, skip_log: SkipLog, *, category: SkipCategory = SkipCategory.NOT_SUPPORTED
) -> None:
    """Family F17: an implementation type's constraint the reader does not carry. Behavioural.

    SWCraft reads a ``VALUE`` implementation type's range from its constraint's first rule's ``INTERNAL-CONSTRS``,
    as closed bounds; *what* names what is not carried. A constraint the file does not declare is
    :attr:`SkipCategory.OUTSIDE_FILE`.
    """
    skip_log.record(
        f"{path}/SW-DATA-DEF-PROPS",
        "DATA-CONSTR-REF",
        f"DATA-CONSTR-REF of an IMPLEMENTATION-DATA-TYPE not carried: {what}; SWCraft reads an implementation "
        "type's range from INTERNAL-CONSTRS, as closed bounds",
        category=category,
        effect=ABORT,
    )


def _report_measurement_point(path: str, skip_log: SkipLog) -> None:
    """Family F21: ``SW-IMPL-POLICY`` ``MEASUREMENT-POINT`` on a data element. Behavioural."""
    skip_log.record(
        f"{path}/SW-DATA-DEF-PROPS",
        "SW-IMPL-POLICY",
        "SW-IMPL-POLICY MEASUREMENT-POINT on a data element not carried: SWCraft carries STANDARD and QUEUED only",
        category=SkipCategory.NOT_SUPPORTED,
        effect=ABORT,
    )


def _report_unsupported_calibration_access(path: str, literal: str, skip_log: SkipLog) -> None:
    """Family F22: a calibration access ``[constr_1017]`` does not support beside ``QUEUED``.

    Section 1: the file breaks the rule. The policy is carried and the calibration access is not, so the import
    never writes a model the schema refuses at load. Descriptive.
    """
    skip_log.record(
        f"{path}/SW-DATA-DEF-PROPS",
        "SW-CALIBRATION-ACCESS",
        f"SW-CALIBRATION-ACCESS {literal} beside SW-IMPL-POLICY QUEUED is not a supported combination -- the "
        "policy is carried and the calibration access is not",
        category=SkipCategory.EXPORT_DEFECT,
        rule=StandardRule.CALIBRATION_ACCESS_SUPPORTED,
        effect=REPORT,
    )


def _report_conditional(
    node: ET.Element, path: str, owner_tag: str, skip_log: SkipLog, *, variable: bool = False
) -> None:
    """Report what *node*'s ``SW-DATA-DEF-PROPS`` holds that the model does not carry.

    The reader takes the first ``SW-DATA-DEF-PROPS-CONDITIONAL`` (:func:`_find_sw_data_def_props_conditional`);
    every further one is one F3 entry. In the first, each child is judged by the owner's table in
    :data:`CONDITIONAL_TABLES`, and a child no row names is F3, an attribute too. An empty child carries nothing.
    The application primitive's other children are reported by :func:`_read_app_primitive_data_type`.

    *variable*: the prototype is an inter-runnable variable, not a data element. The model holds no policy on a
    variable, so a stated ``SW-IMPL-POLICY`` is :func:`_read_swc_variables`' own line whatever its literal, and the
    data element's ``[constr_1017]`` line is never recorded, because the mapping keeps the access and drops the
    policy.
    """
    props = node.find(_SW_DATA_DEF_PROPS)
    variants = props.find(_SW_DATA_DEF_PROPS_VARIANTS) if props is not None else None
    conditionals = variants.findall(_SW_DATA_DEF_PROPS_CONDITIONAL) if variants is not None else []
    if not conditionals:
        return
    for _further in conditionals[1:]:
        _report_unread_property(
            path,
            "SW-DATA-DEF-PROPS-CONDITIONAL",
            f"a further SW-DATA-DEF-PROPS-CONDITIONAL of a {owner_tag} is not read by SWCraft -- only the first "
            "is read; not imported or preserved",
            skip_log,
        )
    conditional = conditionals[0]
    table = CONDITIONAL_TABLES[owner_tag]
    application = owner_tag == "APPLICATION-PRIMITIVE-DATA-TYPE"
    policy = _declared_text(conditional, _SW_IMPL_POLICY)
    for child in list(conditional):
        if not _in_autosar_namespace(child):
            continue
        tag = local_tag(child.tag)
        row = table.get(tag)
        if row is None:
            if not application and not _carries_nothing(child):
                _report_unread_property(
                    path,
                    tag,
                    f"{tag} in the SW-DATA-DEF-PROPS of a {owner_tag} is not read by SWCraft -- not imported or "
                    "preserved",
                    skip_log,
                )
            continue
        literal = (child.text or "").strip(_NUMBER_PADDING)
        if row == "F2":
            _report_implementation_compu_method(path, skip_log)
        elif row == "read|F17":
            if (_declared_text(node, _CATEGORY) or "") != "VALUE":
                _report_value_type_constraint(path, "only a VALUE type has a place for a range", skip_log)
        elif row == "equivalent|F1":
            if literal != _DEFAULT_CALIBRATION_ACCESS:
                _report_uncarried_calibration_access(path, owner_tag, literal, skip_log)
        elif row == "read|F21|F3":
            if variable:
                continue
            if literal == "MEASUREMENT-POINT":
                _report_measurement_point(path, skip_log)
            elif literal not in CARRIED_SW_IMPL_POLICIES:
                _report_unread_property(
                    path,
                    tag,
                    f"SW-IMPL-POLICY {literal} on a data element is not a policy SWCraft carries -- not imported or "
                    "preserved",
                    skip_log,
                )
        elif row == "read|F22|F3":
            if literal not in CARRIED_SW_CALIBRATION_ACCESSES:
                owner = "an inter-runnable variable" if variable else "a data element"
                _report_unread_property(
                    path,
                    tag,
                    f"SW-CALIBRATION-ACCESS {literal} on {owner} is not a value SWCraft carries -- not "
                    "imported or preserved",
                    skip_log,
                )
            elif not variable and (policy or "") == "QUEUED" and literal != _QUEUED_SUPPORTED_ACCESS:
                _report_unsupported_calibration_access(path, literal, skip_log)
    for attribute in conditional.attrib:
        name = local_tag(attribute)
        if application:
            _report_unread_attribute(f"{path}/SW-DATA-DEF-PROPS", name, owner_tag, skip_log)
        else:
            _report_unread_property(
                path,
                name,
                f"attribute {name} of a {owner_tag}'s SW-DATA-DEF-PROPS-CONDITIONAL is not read by SWCraft -- not "
                "imported or preserved",
                skip_log,
            )


def _report_application_errors(interface: ET.Element, path: str, skip_log: SkipLog) -> None:
    """Family F7: every ``APPLICATION-ERROR`` of a client-server interface, one entry each. Behavioural.

    SWCraft models no application error, so the error, its code and every
    ``POSSIBLE-ERROR-REFS`` naming it (F8) are not carried.
    """
    errors = interface.find(_POSSIBLE_ERRORS)
    for error in list(errors) if errors is not None else []:
        if not _in_autosar_namespace(error):
            continue
        name = error.findtext(_SHORT_NAME)
        skip_log.record(
            path if not name else f"{path}/{name}",
            local_tag(error.tag),
            "APPLICATION-ERROR of a CLIENT-SERVER-INTERFACE not carried: SWCraft models no application error",
            category=SkipCategory.NOT_SUPPORTED,
            effect=ABORT,
        )


def _report_possible_error_refs(path: str, skip_log: SkipLog) -> None:
    """Family F8: an operation's ``POSSIBLE-ERROR-REFS``, one entry per operation. Behavioural."""
    skip_log.record(
        path,
        "POSSIBLE-ERROR-REFS",
        "POSSIBLE-ERROR-REFS of a CLIENT-SERVER-OPERATION not carried: SWCraft models no application error",
        category=SkipCategory.NOT_SUPPORTED,
        effect=ABORT,
    )


def _report_constr_level(path: str, skip_log: SkipLog) -> None:
    """Family F9: a ``DATA-CONSTR-RULE``'s ``CONSTR-LEVEL``. Descriptive.

    A ``data_constr`` element holds a level, an integer, and the importers leave this line out where the level is
    carried. It stays for a constraint not imported as an element (several rules, internal limits, an unreadable or
    inexact limit, a merge conflict) and a level no integer states.

    ``[constr_2561]``: level ``0`` means hard limits.
    """
    skip_log.record(
        f"{path}/DATA-CONSTR-RULES",
        "CONSTR-LEVEL",
        "CONSTR-LEVEL of a DATA-CONSTR-RULE not carried: a level is held by a data_constr element alone, as an "
        "integer, and this rule's is not imported as one",
        category=SkipCategory.NOT_SUPPORTED,
        effect=REPORT,
    )


def _report_scale_short_label(path: str, category: str, skip_log: SkipLog) -> None:
    """Family F10: a compu scale's ``SHORT-LABEL`` the writer does not write back. Descriptive.

    A scale's of a category other than ``TEXTTABLE`` and ``LINEAR`` (the writer writes the coefficients alone) and
    a ``TEXTTABLE`` scale's that differs from its literal (the writer writes the literal as the label). A
    ``LINEAR`` scale's is section 1's, :func:`_report_linear_scale_short_label`.
    """
    skip_log.record(
        f"{path}/COMPU-INTERNAL-TO-PHYS",
        "SHORT-LABEL",
        f"SHORT-LABEL of a {category} COMPU-SCALE not carried: SWCraft writes "
        + ("the literal as the label" if category == "TEXTTABLE" else "no label on the scale"),
        category=SkipCategory.NOT_SUPPORTED,
        effect=REPORT,
    )


def _report_linear_scale_short_label(path: str, skip_log: SkipLog) -> None:
    """A ``LINEAR`` scale's ``SHORT-LABEL``: section 1, ``[constr_1375]``. Descriptive.

    The standard admits no label on a ``LINEAR`` method's scale (:attr:`StandardRule.COMPU_ATTRIBUTES_BY_CATEGORY`),
    so the file breaks the rule and the label is not carried. Family F10's key; a label changes no behaviour.
    """
    skip_log.record(
        f"{path}/COMPU-INTERNAL-TO-PHYS",
        "SHORT-LABEL",
        "SHORT-LABEL of a LINEAR COMPU-SCALE not carried: the standard admits no label on the scale of a LINEAR "
        "compu method",
        category=SkipCategory.EXPORT_DEFECT,
        rule=StandardRule.COMPU_ATTRIBUTES_BY_CATEGORY,
        effect=REPORT,
    )


def _report_documentation_group(el: ET.Element, path: str, tag: str, skip_log: SkipLog) -> None:
    """Family F13: an indexed element's documentation SWCraft does not carry. Descriptive.

    ``SHORT-NAME-FRAGMENTS``, ``LONG-NAME`` (but a composition's, its ``title``), ``INTRODUCTION`` and
    ``ANNOTATIONS`` holding anything, one entry each, and the ``UUID`` attribute. A behaviour's and a runnable's
    children are their own closed tables' (``_REPORTED_DOCUMENTATION_TAGS``); their ``UUID`` is here.
    """
    reported = tag not in ("SWC-INTERNAL-BEHAVIOR", "RUNNABLE-ENTITY")
    items = [
        local_tag(child.tag)
        for child in list(el)
        if reported
        and _in_autosar_namespace(child)
        and local_tag(child.tag) in _DOCUMENTATION_GROUP_TAGS
        and not (local_tag(child.tag) == "LONG-NAME" and tag == "COMPOSITION-SW-COMPONENT-TYPE")
        and not _carries_nothing(child)
    ]
    if el.get("UUID"):
        items.append("UUID")
    for item in items:
        skip_log.record(
            path,
            item,
            f"{item} not carried: SWCraft carries no {item} of an element -- not imported or preserved",
            category=SkipCategory.NOT_SUPPORTED,
            effect=REPORT,
        )


_DOCUMENTATION_GROUP_TAGS = frozenset({"SHORT-NAME-FRAGMENTS", "LONG-NAME", "INTRODUCTION", "ANNOTATIONS"})


def _report_package_content(package: ET.Element, path: str, skip_log: SkipLog) -> None:
    """Family F15: an ``AR-PACKAGE``'s own content. Descriptive.

    Every child but ``SHORT-NAME``, ``ELEMENTS`` and ``AR-PACKAGES`` that holds anything, and every attribute
    (``UUID``, ``S``, ``T``), one entry each at the package's path. SWCraft writes its own package tree and no
    package content.
    """
    items = [
        local_tag(child.tag)
        for child in list(package)
        if _in_autosar_namespace(child)
        and local_tag(child.tag) not in ("SHORT-NAME", "ELEMENTS", "AR-PACKAGES")
        and not _carries_nothing(child)
    ]
    for tag in [*items, *(local_tag(attribute) for attribute in package.attrib)]:
        skip_log.record(
            path,
            tag,
            f"{tag} of an AR-PACKAGE is package content SWCraft does not carry -- not imported or preserved",
            category=SkipCategory.NOT_SUPPORTED,
            effect=REPORT,
        )


# ---------------------------------------------------------------------------
# Typed data-type element content extraction
# ---------------------------------------------------------------------------

_CATEGORY = _ar("CATEGORY")
_BASE_TYPE_SIZE = _ar("BASE-TYPE-SIZE")
_BASE_TYPE_ENCODING = _ar("BASE-TYPE-ENCODING")
_NATIVE_DECLARATION = _ar("NATIVE-DECLARATION")
_BYTE_ORDER = _ar("BYTE-ORDER")
_MEM_ALIGNMENT = _ar("MEM-ALIGNMENT")
_SW_DATA_DEF_PROPS = _ar("SW-DATA-DEF-PROPS")
_SW_DATA_DEF_PROPS_VARIANTS = _ar("SW-DATA-DEF-PROPS-VARIANTS")
_SW_DATA_DEF_PROPS_CONDITIONAL = _ar("SW-DATA-DEF-PROPS-CONDITIONAL")
_BASE_TYPE_REF = _ar("BASE-TYPE-REF")
_IMPLEMENTATION_DATA_TYPE_REF = _ar("IMPLEMENTATION-DATA-TYPE-REF")
_COMPU_METHOD_REF = _ar("COMPU-METHOD-REF")
_SUB_ELEMENTS = _ar("SUB-ELEMENTS")
_IMPLEMENTATION_DATA_TYPE_ELEMENT = _ar("IMPLEMENTATION-DATA-TYPE-ELEMENT")
_ARRAY_SIZE = _ar("ARRAY-SIZE")
_ARRAY_SIZE_SEMANTICS = _ar("ARRAY-SIZE-SEMANTICS")
_ARRAY_SIZE_HANDLING = _ar("ARRAY-SIZE-HANDLING")
_SW_POINTER_TARGET_PROPS = _ar("SW-POINTER-TARGET-PROPS")
_TARGET_CATEGORY = _ar("TARGET-CATEGORY")
_APPLICATION_RECORD_ELEMENT = _ar("APPLICATION-RECORD-ELEMENT")
_TYPE_TREF = _ar("TYPE-TREF")
_ELEMENT = _ar("ELEMENT")
_MAX_NUMBER_OF_ELEMENTS = _ar("MAX-NUMBER-OF-ELEMENTS")
_COMPU_INTERNAL_TO_PHYS = _ar("COMPU-INTERNAL-TO-PHYS")
_COMPU_SCALES = _ar("COMPU-SCALES")
_COMPU_SCALE = _ar("COMPU-SCALE")
_LOWER_LIMIT = _ar("LOWER-LIMIT")
_COMPU_CONST = _ar("COMPU-CONST")
_VT = _ar("VT")
# --- The application primitive's own three properties -----------------------
_UNIT_REF = _ar("UNIT-REF")
_DATA_CONSTR_REF = _ar("DATA-CONSTR-REF")
_DISPLAY_NAME = _ar("DISPLAY-NAME")
_DATA_CONSTR_RULES = _ar("DATA-CONSTR-RULES")
_DATA_CONSTR_RULE = _ar("DATA-CONSTR-RULE")
_PHYS_CONSTRS = _ar("PHYS-CONSTRS")
_INTERNAL_CONSTRS = _ar("INTERNAL-CONSTRS")
_UPPER_LIMIT = _ar("UPPER-LIMIT")
_COMPU_RATIONAL_COEFFS = _ar("COMPU-RATIONAL-COEFFS")
_COMPU_NUMERATOR = _ar("COMPU-NUMERATOR")
_COMPU_DENOMINATOR = _ar("COMPU-DENOMINATOR")
# --- What decides whether a form is created as an element -------------------
_COMPU_PHYS_TO_INTERNAL = _ar("COMPU-PHYS-TO-INTERNAL")
#: A ``UNIT``'s relation to the SI system and its dimension (``group UNIT``), none of which a ``unit`` element
#: holds: a unit stating one is not created.
_UNIT_SI_RELATION = tuple(_ar(tag) for tag in ("FACTOR-SI-TO-UNIT", "OFFSET-SI-TO-UNIT", "PHYSICAL-DIMENSION-REF"))
_V = _ar("V")

#: The ``SW-DATA-DEF-PROPS-CONDITIONAL`` children of an ``APPLICATION-PRIMITIVE-DATA-TYPE`` the model **does**
#: hold -- the unit, the conversion and the range. Every other child (``INVALID-VALUE``, ``SW-CALIBRATION-ACCESS``,
#: ``DISPLAY-FORMAT``, ...) is reported by :func:`_read_app_primitive_data_type` under its own tag; the set names
#: what IS modelled so a property a later release defines is reported too.
_MODELLED_APPLICATION_PROPERTIES = frozenset({"COMPU-METHOD-REF", "UNIT-REF", "DATA-CONSTR-REF"})

#: The two ``CATEGORY`` values that name a SWCraft kind on an ``APPLICATION-PRIMITIVE-DATA-TYPE``: ``VALUE``,
#: which the writer emits for both an ``application_type`` and an ``enum``; and ``TEXTTABLE``, the enum spelling
#: of older SWCraft exports, **kept for the note's wording only**, so such an enum is reported as *an enum*
#: re-exported ``VALUE``. It is not a list of legal categories -- ``[constr_1007]``'s table of application data
#: type categories does not list ``TEXTTABLE`` -- and it does not decide whether a note is recorded.
CARRIED_APPLICATION_CATEGORIES = frozenset({"VALUE", "TEXTTABLE"})
_DATA_TYPE_MAPS = _ar("DATA-TYPE-MAPS")
_POSSIBLE_ERRORS = _ar("POSSIBLE-ERRORS")
_POSSIBLE_ERROR_REFS = _ar("POSSIBLE-ERROR-REFS")
_CONSTR_LEVEL = _ar("CONSTR-LEVEL")
_DATA_TYPE_MAPPING_REFS = _ar("DATA-TYPE-MAPPING-REFS")
_DATA_TYPE_MAPPING_REF = _ar("DATA-TYPE-MAPPING-REF")
_DATA_TYPE_MAP = _ar("DATA-TYPE-MAP")
_APPLICATION_DATA_TYPE_REF = _ar("APPLICATION-DATA-TYPE-REF")
_HANDLE_TERMINATION_AND_RESTART = _ar("HANDLE-TERMINATION-AND-RESTART")

# --- Interface element content ----------------------------------------------
_DATA_ELEMENTS = _ar("DATA-ELEMENTS")
_VARIABLE_DATA_PROTOTYPE = _ar("VARIABLE-DATA-PROTOTYPE")
_INIT_VALUE = _ar("INIT-VALUE")
_CONSTANT_REFERENCE = _ar("CONSTANT-REFERENCE")
_CONSTANT_REF = _ar("CONSTANT-REF")
_OPERATIONS = _ar("OPERATIONS")
_CLIENT_SERVER_OPERATION = _ar("CLIENT-SERVER-OPERATION")
_ARGUMENTS = _ar("ARGUMENTS")
_ARGUMENT_DATA_PROTOTYPE = _ar("ARGUMENT-DATA-PROTOTYPE")
_DIRECTION = _ar("DIRECTION")

# --- SWC element content (ports, runnables, events) -------------------------
_PORTS = _ar("PORTS")
_PROVIDED_COM_SPECS = _ar("PROVIDED-COM-SPECS")
_REQUIRED_COM_SPECS = _ar("REQUIRED-COM-SPECS")
_DATA_ELEMENT_REF = _ar("DATA-ELEMENT-REF")
_PROVIDED_INTERFACE_TREF = _ar("PROVIDED-INTERFACE-TREF")
_REQUIRED_INTERFACE_TREF = _ar("REQUIRED-INTERFACE-TREF")
_INTERNAL_BEHAVIORS = _ar("INTERNAL-BEHAVIORS")
_SWC_INTERNAL_BEHAVIOR = _ar("SWC-INTERNAL-BEHAVIOR")
_EVENTS = _ar("EVENTS")
_START_ON_EVENT_REF = _ar("START-ON-EVENT-REF")
_PERIOD = _ar("PERIOD")
_RUNNABLES = _ar("RUNNABLES")
_RUNNABLE_ENTITY = _ar("RUNNABLE-ENTITY")
_MINIMUM_START_INTERVAL = _ar("MINIMUM-START-INTERVAL")
_SUPPORTS_MULTIPLE_INSTANTIATION = _ar("SUPPORTS-MULTIPLE-INSTANTIATION")
_SYMBOL = _ar("SYMBOL")
_PORT_API_OPTIONS = _ar("PORT-API-OPTIONS")

# Raw ARXML tags recognized inside a SWC's PORTS / EVENTS blocks, read whether map_swcs maps or skips them.
# `MAPPED_PORT_KINDS` are the two kinds map_swcs keeps, and so the two a port API option may name a kept port
# of; the PR kind is read and then skipped there.
MAPPED_PORT_KINDS = frozenset({"P-PORT-PROTOTYPE", "R-PORT-PROTOTYPE"})
_PORT_KINDS = MAPPED_PORT_KINDS | {"PR-PORT-PROTOTYPE"}
_EVENT_KINDS = frozenset(
    {
        "TIMING-EVENT",
        "INIT-EVENT",
        "OPERATION-INVOKED-EVENT",
        "DATA-RECEIVED-EVENT",
        # The returns event of an asynchronous call.
        "ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT",
    }
)

# The COM-SPEC kinds whose INIT-VALUE initializes a sender-receiver DATA ELEMENT, the one subject the model
# carries an init for. Of the twelve *-COM-SPEC element declarations 4.2.2 and 4.3.0 each define, three others
# carry an INIT-VALUE, each for a different subject: PARAMETER-PROVIDE/REQUIRE-COM-SPEC a PARAMETER-REF and
# NV-REQUIRE-COM-SPEC a VARIABLE-REF, none of them a DATA-ELEMENT-REF. NV-PROVIDE-COM-SPEC spells its init
# RAM-BLOCK-INIT-VALUE / ROM-BLOCK-INIT-VALUE, so the kind filter is what drops it. (Resolved through both
# licensed schemas' xs:group chains; a later revision or a vendor extension is outside that resolution.) Their
# ports are already reported by map_swcs against their own unresolvable interface reference.
DATA_ELEMENT_COM_SPEC_KINDS = frozenset({"NONQUEUED-SENDER-COM-SPEC", "NONQUEUED-RECEIVER-COM-SPEC"})
# --- A runnable's access points ---------------------------------------------
# The RUNNABLE-ENTITY containers the model represents, each mapped to the element tags it may hold: a
# VARIABLE-ACCESS for the data containers (the implicit DATA-READ-ACCESSS and DATA-WRITE-ACCESSS included), a
# SYNCHRONOUS- or ASYNCHRONOUS-SERVER-CALL-POINT for the call container. Any other child of one of these
# containers is skipped and reported by `_read_runnable_accesses`. The local variables (`_LOCAL_ACCESS_CONTAINERS`),
# the exclusive area references (`_AREA_REF_ROLES`) and the result points (`_RESULT_POINTS`) have their own
# readers; the other containers a RUNNABLE-ENTITY carries (mode and parameter access, wait points, ...) are not
# read, and each item they hold is a skip entry (`_RUNNABLE_CHILDREN` below).
_ACCESS_CONTAINERS: dict[str, tuple[str, ...]] = {
    "DATA-READ-ACCESSS": ("VARIABLE-ACCESS",),
    "DATA-RECEIVE-POINT-BY-ARGUMENTS": ("VARIABLE-ACCESS",),
    "DATA-RECEIVE-POINT-BY-VALUES": ("VARIABLE-ACCESS",),
    "DATA-SEND-POINTS": ("VARIABLE-ACCESS",),
    "DATA-WRITE-ACCESSS": ("VARIABLE-ACCESS",),
    "SERVER-CALL-POINTS": ("SYNCHRONOUS-SERVER-CALL-POINT", "ASYNCHRONOUS-SERVER-CALL-POINT"),
}
# The RUNNABLE-ENTITY wrapper of the asynchronous calls' result points and the one element tag it may hold; any
# other child is `_read_runnable_result_points`' unrecognized-kind entry.
_RESULT_POINTS = "ASYNCHRONOUS-SERVER-CALL-RESULT-POINTS"
_RESULT_POINT = "ASYNCHRONOUS-SERVER-CALL-RESULT-POINT"
# --- The closed tables of a behaviour's and a runnable's children ------------
# Every direct child of a SWC-INTERNAL-BEHAVIOR and of a RUNNABLE-ENTITY is in one of four groups --
# * READ into the ImportModel;
# * COMPARED against what the writer regenerates (reported wherever the source differs from it, absence
#   included);
# * OWNED ELSEWHERE: ADMIN-DATA, which `_index_named_element` reports;
# * REPORTED: the Identifiable documentation SWCraft does not carry (`_REPORTED_DOCUMENTATION_TAGS`, and a
#   behaviour's own DESC -- a runnable's DESC is read into its description). Each child is ONE skip entry at the
#   parent's path, tagged with the child's tag, and only when it holds text (`_report_documentation_child`).
# Any other child that holds an element, text or an attribute is one skip entry per item (`_report_unread_child`),
# so a tag the next export brings is reported by construction. Reading a new container means editing THIS table.
# `UUID` is an attribute, never a child, so it has no row. The group is the comment beside each line.
_REPORTED_DOCUMENTATION_TAGS = ("LONG-NAME", "CATEGORY", "INTRODUCTION", "ANNOTATIONS")
# --- Inter-runnable variables and the runnables' local accesses ---------------
# The two SWC-INTERNAL-BEHAVIOR wrappers of `[TPS_SWCT_01052]`'s two roles, each holding
# VARIABLE-DATA-PROTOTYPEs, and the two RUNNABLE-ENTITY containers of `[TPS_SWCT_01053]`'s two roles, each holding
# VARIABLE-ACCESSes (both schemas: an unbounded choice of the one element). Any other child of a wrapper is the
# selecting container's line (`_SELECTED_CHILD`), and any other child of a container is
# `_read_runnable_local_accesses`' unrecognized-kind entry.
_VARIABLE_ROLES = ("EXPLICIT-INTER-RUNNABLE-VARIABLES", "IMPLICIT-INTER-RUNNABLE-VARIABLES")
_LOCAL_ACCESS_CONTAINERS = ("READ-LOCAL-VARIABLES", "WRITTEN-LOCAL-VARIABLES")
# --- Exclusive areas and the runnables' references to them -------------------
# The SWC-INTERNAL-BEHAVIOR wrapper `EXCLUSIVE-AREAS` (`InternalBehavior.exclusiveArea`) holding EXCLUSIVE-AREAs,
# and the two RUNNABLE-ENTITY wrappers of `ExecutableEntity`'s two references, each holding its one reference tag
# (both schemas: an unbounded choice of the one element). Any other child of the area wrapper is the selecting
# container's line (`_SELECTED_CHILD`), and any other child of a reference wrapper is `_read_runnable_area_refs`'
# own line. The nesting orders (`EXCLUSIVE-AREA-NESTING-ORDERS`, a runnable's
# `EXCLUSIVE-AREA-NESTING-ORDER-REFS`) and 4.3.0's `EXCLUSIVE-AREA-POLICYS` stay out of both tables: SWCraft
# models neither, so each item they hold keeps the closed table's unread line.
_AREA_REF_ROLES: dict[str, str] = {
    "CAN-ENTER-EXCLUSIVE-AREA-REFS": "CAN-ENTER-EXCLUSIVE-AREA-REF",
    "RUNS-INSIDE-EXCLUSIVE-AREA-REFS": "RUNS-INSIDE-EXCLUSIVE-AREA-REF",
}
_BEHAVIOUR_CHILDREN = frozenset(
    (
        # read
        "SHORT-NAME",
        "DATA-TYPE-MAPPING-REFS",
        "EVENTS",
        "RUNNABLES",
        "HANDLE-TERMINATION-AND-RESTART",
        # read: each PORT-API-OPTION's raw facts, judged by the mapper
        "PORT-API-OPTIONS",
        # read: the inter-runnable variables
        *_VARIABLE_ROLES,
        # read: the exclusive areas
        "EXCLUSIVE-AREAS",
        # compared
        "SUPPORTS-MULTIPLE-INSTANTIATION",
        # owned elsewhere
        "ADMIN-DATA",
        # reported
        *_REPORTED_DOCUMENTATION_TAGS,
        "DESC",
    )
)
_RUNNABLE_CHILDREN = frozenset(
    (
        # read
        "SHORT-NAME",
        "MINIMUM-START-INTERVAL",
        "CAN-BE-INVOKED-CONCURRENTLY",
        "DESC",
        *_ACCESS_CONTAINERS,
        # read: the local variable accesses
        *_LOCAL_ACCESS_CONTAINERS,
        # read: the exclusive area references
        *_AREA_REF_ROLES,
        # read: the asynchronous calls' result points
        _RESULT_POINTS,
        "SYMBOL",
        # owned elsewhere
        "ADMIN-DATA",
        # reported
        *_REPORTED_DOCUMENTATION_TAGS,
    )
)
# The containers the reader reads (or compares) by selecting ONE child tag: any
# other child inside one is reported. EVENTS and the four access containers keep
# their own unrecognized-kind entries and are not listed, and so do the two local
# variable containers and the two exclusive area reference wrappers.
_SELECTED_CHILD: dict[str, str] = {
    "RUNNABLES": "RUNNABLE-ENTITY",
    "DATA-TYPE-MAPPING-REFS": "DATA-TYPE-MAPPING-REF",
    "PORT-API-OPTIONS": "PORT-API-OPTION",
    # Each wrapper holds variables only.
    **{role: "VARIABLE-DATA-PROTOTYPE" for role in _VARIABLE_ROLES},
    # The wrapper holds areas only.
    "EXCLUSIVE-AREAS": "EXCLUSIVE-AREA",
}
_LOCAL_VARIABLE_REF = _ar("LOCAL-VARIABLE-REF")
_EXCLUSIVE_AREAS = _ar("EXCLUSIVE-AREAS")
_EXCLUSIVE_AREA = _ar("EXCLUSIVE-AREA")
_SCOPE = _ar("SCOPE")
_ACCESSED_VARIABLE = _ar("ACCESSED-VARIABLE")
_AUTOSAR_VARIABLE_IREF = _ar("AUTOSAR-VARIABLE-IREF")
_PORT_PROTOTYPE_REF = _ar("PORT-PROTOTYPE-REF")
_TARGET_DATA_PROTOTYPE_REF = _ar("TARGET-DATA-PROTOTYPE-REF")
_OPERATION_IREF = _ar("OPERATION-IREF")
_CONTEXT_R_PORT_REF = _ar("CONTEXT-R-PORT-REF")
_TARGET_REQUIRED_OPERATION_REF = _ar("TARGET-REQUIRED-OPERATION-REF")
# --- The operation event's IREF, concurrency, the queue ---------------------
_CONTEXT_P_PORT_REF = _ar("CONTEXT-P-PORT-REF")
_TARGET_PROVIDED_OPERATION_REF = _ar("TARGET-PROVIDED-OPERATION-REF")
_CAN_BE_INVOKED_CONCURRENTLY = _ar("CAN-BE-INVOKED-CONCURRENTLY")
_OPERATION_REF = _ar("OPERATION-REF")
_QUEUE_LENGTH = _ar("QUEUE-LENGTH")
# --- The four behavioural values ---------------------------------------------
_IS_SERVICE = _ar("IS-SERVICE")
_SW_IMPL_POLICY = _ar("SW-IMPL-POLICY")
_SW_CALIBRATION_ACCESS = _ar("SW-CALIBRATION-ACCESS")
_SERVER_ARGUMENT_IMPL_POLICY = _ar("SERVER-ARGUMENT-IMPL-POLICY")
_FILTER = _ar("FILTER")
_DATA_FILTER_TYPE = _ar("DATA-FILTER-TYPE")
_DISABLED_MODE_IREFS = _ar("DISABLED-MODE-IREFS")
_DISABLED_MODE_IREF = _ar("DISABLED-MODE-IREF")
_CONTEXT_PORT_REF = _ar("CONTEXT-PORT-REF")
_CONTEXT_MODE_DECLARATION_GROUP_PROTOTYPE_REF = _ar("CONTEXT-MODE-DECLARATION-GROUP-PROTOTYPE-REF")
_TARGET_MODE_DECLARATION_REF = _ar("TARGET-MODE-DECLARATION-REF")
_LONG_NAME = _ar("LONG-NAME")
_L4 = _ar("L-4")
#: The four sender/receiver com-spec kinds whose attributes a ``port_com_spec`` entry carries: both directions,
#: non-queued and queued. The two ``QUEUED-*`` kinds hold no ``INIT-VALUE``, so ``DATA_ELEMENT_COM_SPEC_KINDS``
#: leaves them out.
_SENDER_RECEIVER_COM_SPEC_KINDS = frozenset(
    {
        "NONQUEUED-SENDER-COM-SPEC",
        "NONQUEUED-RECEIVER-COM-SPEC",
        "QUEUED-SENDER-COM-SPEC",
        "QUEUED-RECEIVER-COM-SPEC",
    }
)

# --- Composition/prototype/connector element content ------------------------
_COMPONENTS = _ar("COMPONENTS")
_SW_COMPONENT_PROTOTYPE = _ar("SW-COMPONENT-PROTOTYPE")
_CONNECTORS = _ar("CONNECTORS")
_PROVIDER_IREF = _ar("PROVIDER-IREF")
_REQUESTER_IREF = _ar("REQUESTER-IREF")
_CONTEXT_COMPONENT_REF = _ar("CONTEXT-COMPONENT-REF")
_TARGET_P_PORT_REF = _ar("TARGET-P-PORT-REF")
_TARGET_R_PORT_REF = _ar("TARGET-R-PORT-REF")
_INNER_PORT_IREF = _ar("INNER-PORT-IREF")
_OUTER_PORT_REF = _ar("OUTER-PORT-REF")
_P_PORT_IN_COMPOSITION_INSTANCE_REF = _ar("P-PORT-IN-COMPOSITION-INSTANCE-REF")
_R_PORT_IN_COMPOSITION_INSTANCE_REF = _ar("R-PORT-IN-COMPOSITION-INSTANCE-REF")

# Raw ARXML tags recognized inside a composition's CONNECTORS block. Any other connector kind (e.g.
# PASS-THROUGH-SW-CONNECTOR) is not collected, and `_read_composition` records a SkipLog entry for it: nested
# content is not subject to KNOWN_ELEMENT_TAGS' gate, and must be reported too.
_CONNECTOR_KINDS = frozenset({"ASSEMBLY-SW-CONNECTOR", "DELEGATION-SW-CONNECTOR"})


def _writable_as_decimal(value: int) -> bool:
    """Whether *value* can leave this reader as a decimal string.

    ``int(s, 16)``, ``int(s, 8)`` and ``int(s, 2)`` are exempt from ``sys.get_int_max_str_digits()`` (CPython
    applies that limit to non-power-of-2 bases only), so a prefixed literal reads without complaint and would raise
    much later, in the ``yaml.safe_dump`` that writes it back as a decimal scalar, mid-import. Both prefixed read
    paths ask this where the answer can still become a skip with a reason. It *does* the conversion rather than
    estimating the width, so the answer is exact and a limit of ``0`` means *no limit*, as in CPython.

    :func:`_read_constant_scalar`'s decimal branch cannot use this: ``int()`` of an over-limit decimal string
    raises on the way in, so its literal is measured before ``int()`` is called.
    """
    try:
        str(value)
    except ValueError:
        return False
    return True


def _declared_text(node: ET.Element, tag: str) -> str | None:
    """``tag``'s text content without surrounding XML whitespace, or ``None`` when ``node`` has no such child.

    Every property this is used for is a token, a number or a boolean (``CATEGORY``, ``BYTE-ORDER``,
    ``MEM-ALIGNMENT``, ``QUEUE-LENGTH``, a scale's ``LOWER-LIMIT``, ``CAN-BE-INVOKED-CONCURRENTLY``), so an
    exporter that pretty-prints the value onto its own line reads as one that keeps it inline; otherwise the merge
    would refuse two identical declarations over indentation alone, and a conflicting group imports **neither**
    member. Every such type restricts ``xsd:string`` with a pattern or an enumeration in both licensed schemas, so
    no padding is the standard's: stripping XML's own white space (:data:`_NUMBER_PADDING`) is the stated
    tolerance, and nothing wider -- a no-break space reaches the caller's grammar as written and gets that field's
    own line. An absent child is ``None``; a declared empty one is ``""``.

    **What this does not reach:** lexical differences that are not whitespace. ``8`` and ``08`` are one AUTOSAR
    alignment and stay two texts here (see :class:`SwBaseTypeInfo`), and so do ``0x10`` and ``16`` wherever a
    caller compares literals. Nor is it applied to a type that **preserves** whitespace: ``NATIVE-DECLARATION`` and
    ``BASE-TYPE-ENCODING`` are strings in both licensed schemas, reach a written model field and are read raw. Nor
    to the reader at large: ``SHORT-NAME``s and reference paths are read raw, because an identity is compared
    against the model's own strings.
    """
    text = node.findtext(tag)
    return None if text is None else text.strip(_NUMBER_PADDING)


#: The grammars an ARXML number field is written in, chosen by **the field's
#: XSD type at each call site** and never by the helper that reads the text:
#:
#: * ``"pattern"`` -- ``NUMERICAL-VALUE`` (XSD R4.3.0:86078; R4.2.2:74788), a
#:   simple type with a lexical pattern. A hexadecimal, an octal (``0[0-7]+``)
#:   and a binary spelling are **unsigned**: *"Negative numbers can only be
#:   expressed in decimal or float notation"* (R4.3.0:86066; R4.2.2:74776), so
#:   ``-010`` matches no alternative. A decimal has no leading zero, so ``08``
#:   and ``010.5`` match none either; its one-digit arm ``[+\-]?[0-9]`` signs
#:   zero, so ``-0`` and ``+0`` are zero.
#: * ``"integer"`` -- the signed integer types' patterns (``INTEGER``, R4.3.0:84188;
#:   R4.2.2:72915; ``UNLIMITED-INTEGER``, R4.3.0:88867; R4.2.2:77205): the
#:   pattern's radixes and unsigned prefixed literals, no fraction and no
#:   exponent, and a sign on a **nonzero** decimal only (``[+\-]?[1-9][0-9]*``)
#:   -- zero is the octal ``0[0-7]*``, so ``-0`` and ``+0`` are no integer spelling.
#: * ``"positive-integer"`` -- ``POSITIVE-INTEGER`` and ``POSITIVE-UNLIMITED-INTEGER``
#:   (R4.3.0:86393, :86410; R4.2.2:75075, :75092): the integer pattern with **no
#:   sign at all** (``[1-9][0-9]*|0[xX]...|0[bB]...|0[0-7]*``), so ``+8`` and
#:   ``-8`` are no spelling of it -- a ``BASE-TYPE-SIZE`` or a ``QUEUE-LENGTH``
#:   written so is unread and reported, never carried as eight.
#: * ``"formula"`` -- the mixed ``*-VALUE-VARIATION-POINT`` types and ``LIMIT``
#:   (R4.3.0:47492; R4.2.2:41124 -- *"in fact a NumericalValuationPoint"*), whose
#:   text is the formula language's: one literal (GST R4.3.0:4776-4800; R4.2.2:
#:   3932-3952), its integer literals in their radix, and **at most one sign**,
#:   which is the unary ``SumOperator`` (GST R4.3.0:4667-4668, 4710-4711;
#:   R4.2.2:3834-3835, 3878-3879) and not part of the literal -- so ``-010`` is
#:   minus octal ten, -8, and ``-0x10`` is -16. The language's ``WS`` token is
#:   hidden between any two tokens (GST R4.3.0:4809 -- space, tab, CR, LF;
#:   R4.2.2:3957-3958 -- space, tab, CR; the superset read at both releases), so
#:   whitespace between the sign and its literal is no part of the number and
#:   ``- 8`` is -8 (:func:`stripped_spelling`); whitespace inside a literal is never
#:   skipped, because a literal is one token. Nothing is evaluated: a text that
#:   is more than one signed literal is no number to this reader.
#:
#: A ``TIME-VALUE`` field is an ``xsd:double`` and neither: it is read by
#: :func:`parse_float_literal`, by ``xsd:double``'s lexical space
#: (:data:`_XSD_DOUBLE`), where ``010`` is ten. The reader accepts the
#: R4.3.0 pattern's spellings (``0X``, ``0B``, ``e``) at both releases, a superset
#: of R4.2.2's lowercase-prefix, uppercase-``E`` one.
NumberGrammar = Literal["pattern", "integer", "positive-integer", "formula"]

#: An integer spelling of each grammar; exactly one named group is set.
_INTEGER_SPELLING: dict[NumberGrammar, re.Pattern[str]] = {
    "pattern": re.compile(
        r"0[xX](?P<hex>[0-9a-fA-F]+)|0[bB](?P<bin>[01]+)|0(?P<oct>[0-7]+)|(?P<dec>[+-]?(?:0|[1-9][0-9]*))"
    ),
    "integer": re.compile(
        r"0[xX](?P<hex>[0-9a-fA-F]+)|0[bB](?P<bin>[01]+)|0(?P<oct>[0-7]+)|(?P<dec>0|[+-]?[1-9][0-9]*)"
    ),
    "positive-integer": re.compile(
        r"0[xX](?P<hex>[0-9a-fA-F]+)|0[bB](?P<bin>[01]+)|0(?P<oct>[0-7]+)|(?P<dec>0|[1-9][0-9]*)"
    ),
    "formula": re.compile(
        r"(?P<sign>[+-]?)(?:0[xX](?P<hex>[0-9a-fA-F]+)|0[bB](?P<bin>[01]+)|0(?P<oct>[0-7]+)|(?P<dec>0|[1-9][0-9]*))"
    ),
}

#: Every other finite spelling of each grammar: the pattern's decimal-with-
#: fraction-or-exponent alternative and its ``.0``; none of the integer's; the formula's
#: ``DecimalLiteral`` and finite ``DoubleLiteral`` behind one optional sign.
#: ``INF``, ``-INF`` and ``NaN`` are in both grammars and are no finite number.
_REAL_SPELLING: dict[NumberGrammar, re.Pattern[str]] = {
    "pattern": re.compile(r"[+-]?(?:[1-9][0-9]+|[0-9])(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|\.0"),
    "integer": re.compile(r"(?!)"),
    "positive-integer": re.compile(r"(?!)"),
    "formula": re.compile(
        r"[+-]?(?:0?\.[0-9]+|[1-9][0-9]*\.[0-9]*|(?:0?\.[0-9]+|[1-9][0-9]*(?:\.[0-9]*)?)[eE][+-]?[0-9]+)"
    ),
}

#: The radix of each integer spelling's named group, with the word a reason names it by.
_RADIX = (("hex", 16, "hexadecimal"), ("oct", 8, "octal"), ("bin", 2, "binary"), ("dec", 10, "decimal"))

#: A formula's leading sign and the ``WS`` the language hides after it (GST R4.3.0:4809;
#: R4.2.2:3957-3958): the whitespace between the unary operator and its literal only.
_SIGN_AND_WHITESPACE = re.compile(r"\A([+-])[ \t\r\n]+")

#: The whitespace a number text is stripped of at both ends: the formula's ``WS`` (GST
#: R4.3.0:4809), which is also XML's own whitespace -- never Python's wider Unicode set
#: (``str.strip()`` would also remove a no-break space, which no ARXML grammar skips).
_NUMBER_PADDING = " \t\r\n"


def stripped_spelling(text: str, *, grammar: NumberGrammar) -> str:
    """*text* stripped, and in the formula grammar with the whitespace between its sign and its literal removed.

    The formula language hides its ``WS`` token between tokens, and the sign is a token of its own, so ``- 8``
    spells -8 there; the pattern grammar's sign is part of its decimal literal, so a pattern text is only stripped.
    Whitespace inside a literal stays and makes the text no number of either grammar.

    **A stated tolerance, beyond the standard:** a pattern-class text (``NUMERICAL-VALUE``, the integer types)
    restricts ``xsd:string`` and admits no padding, so ``" 8 "`` there is no number by the standard. This reader
    strips :data:`_NUMBER_PADDING` from both ends in both grammars: the value is the one the digits state and
    nothing is lost. The input-vs-output oracle reads the standard and reports such a text as a difference.
    """
    stripped = text.strip(_NUMBER_PADDING)
    return _SIGN_AND_WHITESPACE.sub(r"\1", stripped) if grammar == "formula" else stripped


def _integer_digits(match: re.Match[str]) -> tuple[int, str, int, str]:
    """An :data:`_INTEGER_SPELLING` match's sign (1 or -1), digit run, radix and the radix's word.

    The pattern and integer grammars' decimal carries its sign inside the ``dec`` group, which
    ``int()`` reads (the positive integer's has none); only the formula grammar has a ``sign``
    group of its own.
    """
    sign = -1 if match.groupdict().get("sign") == "-" else 1
    group, base, word = next(radix for radix in _RADIX if match[radix[0]] is not None)
    return sign, match[group], base, word


def _integer_spelling(text: str, grammar: NumberGrammar) -> int | None:
    """*text* (stripped) as the integer its grammar spells, or ``None``.

    The digit run is converted in its radix; a decimal run longer than
    ``sys.get_int_max_str_digits()`` makes ``int()`` raise, which this does not
    catch -- every caller decides first, or catches, what that means for it.
    """
    match = _INTEGER_SPELLING[grammar].fullmatch(text)
    if match is None:
        return None
    sign, digits, base, _word = _integer_digits(match)
    return sign * int(digits, base)


def _real_spelling(text: str, grammar: NumberGrammar) -> str | None:
    """*text* as its grammar's finite non-integer spelling (:func:`stripped_spelling`), or ``None`` where it is none."""
    spelled = stripped_spelling(text, grammar=grammar)
    return spelled if _REAL_SPELLING[grammar].fullmatch(spelled) else None


def parse_int_literal(text: str | None, *, grammar: NumberGrammar) -> int | None:
    """Parse an ARXML integer spelling of *grammar* (:data:`NumberGrammar`) into a Python ``int``.

    The caller names the grammar of its field's XSD type: ``"pattern"`` for a ``NUMERICAL-VALUE`` field,
    ``"integer"`` for a signed integer field, ``"positive-integer"`` for a ``POSITIVE-INTEGER`` field, ``"formula"``
    for a ``LIMIT`` or a ``*-VALUE-VARIATION-POINT``. All four read decimal, hexadecimal (``0x``/``0X``), binary
    (``0b``/``0B``) and **octal** (``0`` followed by octal digits: ``010`` is eight) -- never ``int(text, 0)``,
    whose own grammar is none of them -- and they differ in the sign: the pattern signs a decimal only, the integer
    a nonzero decimal only (``-0`` is zero in the pattern and nothing in the integer), the positive integer nothing
    at all (``+8`` is eight in the integer and nothing in the positive integer), the formula puts one sign before
    any literal (``-0x0A`` is -10 there and nothing in the other three). ``08``, ``010.5``, an exponent and
    Python's own extras (``1_000``) are no integer spelling of any.

    Returns ``None`` (never raises) for missing text, text that is no integer spelling of the grammar and a value
    CPython could not write back as a decimal string, so every caller's "None -> skip and report" handling covers
    all three.
    """
    if text is None:
        return None
    try:
        value = _integer_spelling(stripped_spelling(text, grammar=grammar), grammar)
    except ValueError:
        # A decimal run above `sys.get_int_max_str_digits()`.
        return None
    if value is None:
        return None
    # The power-of-two radixes read an oversized literal without complaint (`_writable_as_decimal`), so the
    # question is asked here, where the answer joins the "None -> skip and report" path instead of aborting
    # mid-import.
    if not _writable_as_decimal(value):
        return None
    return value


#: ``xsd:double``'s lexical space (XML Schema Part 2, 3.2.5.1, its mantissa by
#: ``decimal``'s rule, 3.2.3.1, and its exponent by ``integer``'s): ASCII digits
#: only, and ``INF``, ``-INF`` and ``NaN``, which are no finite number. Every
#: AUTOSAR type :func:`parse_float_literal` serves restricts it with no facet of
#: its own (``TIME-VALUE--SIMPLE``, XSD R4.3.0:88606-88608; R4.2.2:76972-76974).
#: The type's ``whiteSpace`` facet is ``collapse``, so the XML whitespace around
#: the token is no part of it -- and nothing else is (:data:`_NUMBER_PADDING`).
_XSD_DOUBLE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|-?INF|NaN")


def parse_float_literal(text: str | None) -> float | None:
    """Parse an AUTOSAR ``TIME-VALUE`` string -- an ``xsd:double`` -- into a Python ``float``.

    Used for TIMING-EVENT ``PERIOD``, RUNNABLE-ENTITY ``MINIMUM-START-INTERVAL`` and a com-spec's
    ``ALIVE-TIMEOUT``; :func:`parse_exact_number` also hands it a text ``_real_spelling`` has already matched. The
    text is read by ``xsd:double``'s lexical space (:data:`_XSD_DOUBLE`), never by ``float()``'s, which also
    reads ``1_000``, Unicode digits and whitespace, and ``infinity`` or ``nan`` in any case. A non-finite value
    must never reach a mapper's numeric guard.

    Returns ``None`` (never raises) for missing, unparsable or non-finite text, so every caller's "None -> skip
    and report" handling covers a malformed literal.
    """
    if text is None:
        return None
    stripped = text.strip(_NUMBER_PADDING)
    if _XSD_DOUBLE.fullmatch(stripped) is None:
        return None
    value = float(stripped)
    if not math.isfinite(value):
        return None
    return value


def _stated_decimal(text: str) -> Decimal | None:
    """*text* as the exact finite decimal it states, or ``None`` when ``decimal`` reads no such number from it.

    The **one** place a source's float spelling becomes a ``Decimal``, so every exactness judgement -- a bound and a
    coefficient (:func:`parse_exact_number`), an init (:func:`_read_constant_scalar`), a dropped scale limit and a
    merge row's limit (:func:`exact_decimal`) -- refuses the same texts the same way. ``None`` covers a text that
    is no number, a non-finite one, and a spelling whose **exponent is beyond what ``decimal`` constructs**
    (``1e-99999999999999999999`` raises ``InvalidOperation`` where ``float()`` answers ``0.0``). The caller decides
    what ``None`` means; none may let the exception out.

    What this does not reach: arithmetic on the result. A decimal with an exponent near the limit
    (``1e999999999999999999``) still overflows when multiplied, which is the caller's to guard.
    """
    try:
        number = Decimal(text.strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def exact_decimal(text: str | None, *, grammar: NumberGrammar) -> Decimal | None:
    """*text* as an exact decimal (an integer spelling in any radix as its integer), or ``None`` for no finite number.

    The proof of a dropped scale limit and the merge row's limit comparison are exact decimal arithmetic on the
    source's text, never float, so ``0x10``, ``16`` and ``16.0`` are one number. The text is read by its field's
    *grammar* (:data:`NumberGrammar`): a spelling outside it is ``None``, as is whatever :func:`_stated_decimal`
    refuses.
    """
    if text is None:
        return None
    integer = parse_int_literal(text, grammar=grammar)
    if integer is not None:
        return Decimal(integer)
    spelled = _real_spelling(text, grammar)
    return None if spelled is None else _stated_decimal(spelled)


def parse_exact_number(text: str | None, *, grammar: NumberGrammar) -> tuple[int | float | None, bool]:
    """A ``Limit`` / ``Numerical`` text as the model value that holds it **exactly**, and whether none does.

    Both primitives are strings with a pattern, not doubles (Generic Structure Template, tables *Limit* and
    *Numerical*): an integer spelling of any width is a legal value, and a 64-bit bound above 2**53 has no binary
    float. A ``float`` holds a text exactly when ``Decimal(str(value))`` -- the number the writer prints for that
    float (``format_float``) -- equals the text's own decimal (:func:`_stated_decimal`).

    * An integer spelling of the field's *grammar* (any radix; the value, never the radix) is the ``float`` of that
      integer where the float holds it, and the ``int`` itself where it does not (``18446744073709551615``, and
      ``-9223372036854775808`` too, whose float prints ``-9223372036854776000``). The writer prints an integral
      float without a fraction, so a model written by SWCraft reads back as the bytes it was written from.
    * Any other finite spelling of the grammar is a ``float`` only when the float holds it (``0.1`` passes,
      ``16777217.000000001`` does not); a spelling outside the grammar (``08``, ``010.5``) is no number.

    Returns ``(value, False)``, ``(None, False)`` for a text that is no finite number, and ``(None, True)`` for a
    finite number no model value holds exactly -- the caller reports the two differently.

    What this does not reach: an integer written with an exponent (``1E3``) is a float spelling, held as
    ``1000.0``.
    """
    integer = parse_int_literal(text, grammar=grammar)
    if integer is not None:
        try:
            as_float = float(integer)
        except OverflowError:
            return integer, False
        return (as_float if Decimal(str(as_float)) == Decimal(integer) else integer), False
    if text is None:
        return None, False
    spelled = _real_spelling(text, grammar)
    if spelled is None:
        return None, False
    value = parse_float_literal(spelled)
    if value is None:
        return None, False
    # `stated` is None only for an exponent `decimal` does not construct; no float is shown to hold it.
    stated = _stated_decimal(spelled)
    if stated is None or Decimal(str(value)) != stated:
        return None, True
    return value, False


def _find_sw_data_def_props_conditional(node: ET.Element) -> ET.Element | None:
    """Descend SW-DATA-DEF-PROPS/SW-DATA-DEF-PROPS-VARIANTS to the first SW-DATA-DEF-PROPS-CONDITIONAL."""
    props = node.find(_SW_DATA_DEF_PROPS)
    if props is None:
        return None
    variants = props.find(_SW_DATA_DEF_PROPS_VARIANTS)
    if variants is None:
        return None
    return variants.find(_SW_DATA_DEF_PROPS_CONDITIONAL)


def _read_sw_base_type(entry: RawElement, skip_log: SkipLog) -> SwBaseTypeInfo:
    del skip_log  # uniform dispatch signature (see _TYPED_ELEMENT_READERS); unused here
    node = entry.node
    size_text = node.findtext(_BASE_TYPE_SIZE)
    return SwBaseTypeInfo(
        path=entry.path,
        name=node.findtext(_SHORT_NAME) or "",
        size=parse_int_literal(size_text, grammar="positive-integer"),
        # Not stripped: these two reach a written model field and the writer writes each as the model holds it,
        # so each is carried as the export wrote it. The merge comparison strips both for itself.
        encoding=node.findtext(_BASE_TYPE_ENCODING) or "",
        native_declaration=node.findtext(_NATIVE_DECLARATION) or "",
        description=entry.description,
        # Read for the merge comparison alone (see SwBaseTypeInfo). None for an absent child keeps "the export
        # does not say" distinct from "the export says nothing".
        byte_order=_declared_text(node, _BYTE_ORDER),
        mem_alignment=_declared_text(node, _MEM_ALIGNMENT),
        category=_declared_text(node, _CATEGORY),
    )


#: The children of ``SW-POINTER-TARGET-PROPS`` and of its target's conditional
#: that :class:`ImplDataTypeInfo` reads into a field of its own; every other
#: child is inventoried by tag in ``pointer_target_qualifiers``.
_READ_POINTER_TARGET_PROPS = frozenset({"TARGET-CATEGORY", "SW-DATA-DEF-PROPS"})
_READ_POINTER_TARGET_CONDITIONAL = frozenset(
    {"IMPLEMENTATION-DATA-TYPE-REF", "BASE-TYPE-REF", "SW-POINTER-TARGET-PROPS"}
)


def _read_impl_sub_elements(
    node: ET.Element,
    *,
    array_path: str | None = None,
    skip_log: SkipLog | None = None,
    type_path: str | None = None,
) -> tuple[ImplSubElementInfo, ...]:
    """The direct ``SUB-ELEMENTS`` of *node*, in order, each read at its own node.

    Given *array_path* and *skip_log* (an ``ARRAY``'s), each element's ``DESC`` is reported in the same walk: an
    array's one element is written as the array's ``element_name``, not as a model element, so its text has no
    field to land in.

    Given *type_path* and *skip_log*, each element's closed table and ``SW-DATA-DEF-PROPS`` are judged at
    ``{type_path}/{element}`` (:func:`_report_closed_table`, :func:`_report_conditional`). A nested element is
    not read, and not judged.
    """
    container = node.find(_SUB_ELEMENTS)
    if container is None:
        return ()
    subs: list[ImplSubElementInfo] = []
    for element in container.findall(_IMPLEMENTATION_DATA_TYPE_ELEMENT):
        cond = _find_sw_data_def_props_conditional(element)
        nested = element.find(_SUB_ELEMENTS)
        name = element.findtext(_SHORT_NAME) or ""
        description = _extract_description(element)
        if type_path is not None and skip_log is not None:
            element_path = f"{type_path}/{name}"
            _report_closed_table(element, element_path, "IMPLEMENTATION-DATA-TYPE-ELEMENT", skip_log)
            _report_conditional(element, element_path, "IMPLEMENTATION-DATA-TYPE-ELEMENT", skip_log)
        if array_path is not None and skip_log is not None:
            _report_uncarried_description(description, f"{array_path}/{name}", _ARRAY_ELEMENT_DESC_REASON, skip_log)
        subs.append(
            ImplSubElementInfo(
                name=name,
                category=_declared_text(element, _CATEGORY) or "",
                impl_type_ref=cond.findtext(_IMPLEMENTATION_DATA_TYPE_REF) if cond is not None else None,
                base_type_ref=cond.findtext(_BASE_TYPE_REF) if cond is not None else None,
                array_size=parse_int_literal(element.findtext(_ARRAY_SIZE), grammar="formula"),
                array_size_semantics=_declared_text(element, _ARRAY_SIZE_SEMANTICS),
                array_size_handling=_declared_text(element, _ARRAY_SIZE_HANDLING),
                nested_sub_elements=0 if nested is None else len(nested.findall(_IMPLEMENTATION_DATA_TYPE_ELEMENT)),
                description=description,
            )
        )
    return tuple(subs)


def _read_impl_data_type(entry: RawElement, skip_log: SkipLog) -> ImplDataTypeInfo:
    node = entry.node
    cond = _find_sw_data_def_props_conditional(node)
    base_type_ref = cond.findtext(_BASE_TYPE_REF) if cond is not None else None
    impl_type_ref = cond.findtext(_IMPLEMENTATION_DATA_TYPE_REF) if cond is not None else None
    # A DATA_REFERENCE's target props. `findtext` above reads direct children only, so the target's own
    # reference inside SW-POINTER-TARGET-PROPS never reaches `impl_type_ref`.
    target_props = cond.find(_SW_POINTER_TARGET_PROPS) if cond is not None else None
    target_category: str | None = None
    target_ref: str | None = None
    target_base_ref: str | None = None
    target_nested = False
    qualifiers: list[str] = []
    if target_props is not None:
        target_category = _declared_text(target_props, _TARGET_CATEGORY)
        target_cond = _find_sw_data_def_props_conditional(target_props)
        if target_cond is not None:
            target_ref = target_cond.findtext(_IMPLEMENTATION_DATA_TYPE_REF)
            target_base_ref = target_cond.findtext(_BASE_TYPE_REF)
            target_nested = target_cond.find(_SW_POINTER_TARGET_PROPS) is not None
            qualifiers.extend(
                tag
                for tag in (local_tag(child.tag) for child in target_cond)
                if tag not in _READ_POINTER_TARGET_CONDITIONAL
            )
        qualifiers.extend(
            tag for tag in (local_tag(child.tag) for child in target_props) if tag not in _READ_POINTER_TARGET_PROPS
        )
    category = _declared_text(node, _CATEGORY) or ""
    # The type's closed table and its data definition's; the elements' below.
    _report_closed_table(node, entry.path, "IMPLEMENTATION-DATA-TYPE", skip_log)
    _report_conditional(node, entry.path, "IMPLEMENTATION-DATA-TYPE", skip_log)
    array_path = entry.path if category == "ARRAY" else None
    sub_elements = _read_impl_sub_elements(node, array_path=array_path, skip_log=skip_log, type_path=entry.path)
    return ImplDataTypeInfo(
        path=entry.path,
        name=node.findtext(_SHORT_NAME) or "",
        # Stripped: the merge comparison reads it back and an exporter's indentation is not a category.
        category=category,
        base_type_ref=base_type_ref,
        impl_type_ref=impl_type_ref,
        description=entry.description,
        sub_elements=sub_elements,
        pointer_target_category=target_category,
        pointer_target_ref=target_ref,
        pointer_target_base_type_ref=target_base_ref,
        pointer_target_nested=target_nested,
        pointer_target_qualifiers=tuple(qualifiers),
        data_constr_ref=cond.findtext(_DATA_CONSTR_REF) if cond is not None else None,
        compu_method_ref=cond.findtext(_COMPU_METHOD_REF) if cond is not None else None,
    )


def _read_app_primitive_data_type(entry: RawElement, skip_log: SkipLog) -> AppPrimitiveDataTypeInfo:
    """Read one APPLICATION-PRIMITIVE-DATA-TYPE, and report what it carries that the model does not hold.

    The three references the model holds are read into the record; every other child of the element's own
    ``SW-DATA-DEF-PROPS-CONDITIONAL`` gets a skip entry under its own tag, so ``DISPLAY-FORMAT``,
    ``SW-CALIBRATION-ACCESS`` and ``INVALID-VALUE`` are named rather than dropped in silence. The type itself is
    still imported: what is reported is the property, not the element.

    The ``CATEGORY`` is read and **not** reported here: both kinds are re-exported ``VALUE``, and the importer
    notes a differing category on the element it writes; that note does not reach a bare primitive collapsed onto
    its same-name implementation twin, which is written as no element. It is not a skip entry, because that would
    be ``import component``'s lossy-import refusal and the element is not lost.

    What this does **not** reach: a second ``SW-DATA-DEF-PROPS-CONDITIONAL``.
    :func:`_find_sw_data_def_props_conditional` reads the first only, so a property inside a later variant is
    neither read nor reported here.
    """
    node = entry.node
    # Stripped, as the base type's and the implementation type's are.
    category = _declared_text(node, _CATEGORY) or ""
    cond = _find_sw_data_def_props_conditional(node)
    if cond is None:
        return AppPrimitiveDataTypeInfo(
            path=entry.path,
            name=node.findtext(_SHORT_NAME) or "",
            compu_method_ref=None,
            description=entry.description,
            category=category,
        )
    # A further conditional and an attribute of the first are family F3's and the unread-attribute site's; the
    # children are this site's, keyed at `{type}/SW-DATA-DEF-PROPS`.
    _report_conditional(node, entry.path, "APPLICATION-PRIMITIVE-DATA-TYPE", skip_log)
    for child in list(cond):
        tag = local_tag(child.tag)
        if tag in _MODELLED_APPLICATION_PROPERTIES:
            continue
        skip_log.record(
            path=f"{entry.path}/SW-DATA-DEF-PROPS",
            tag=tag,
            reason=(
                f"{tag} on an APPLICATION-PRIMITIVE-DATA-TYPE has no SWCraft representation -- "
                "the type is imported with its unit, conversion and range, and this property is "
                "not imported or preserved"
            ),
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
    return AppPrimitiveDataTypeInfo(
        path=entry.path,
        name=node.findtext(_SHORT_NAME) or "",
        compu_method_ref=cond.findtext(_COMPU_METHOD_REF),
        description=entry.description,
        unit_ref=cond.findtext(_UNIT_REF),
        data_constr_ref=cond.findtext(_DATA_CONSTR_REF),
        category=category,
    )


def _read_unit(entry: RawElement, skip_log: SkipLog) -> UnitInfo:
    del skip_log  # uniform dispatch signature (see _TYPED_ELEMENT_READERS); unused here
    node = entry.node
    return UnitInfo(
        path=entry.path,
        name=node.findtext(_SHORT_NAME) or "",
        # Read raw, never through `_declared_text`: the writer writes the text back exactly. `None` (no
        # DISPLAY-NAME: the SHORT-NAME is the unit) stays distinct from `""` (a deliberately blank unit).
        display_name=node.findtext(_DISPLAY_NAME),
        description=entry.description,
        si_relation=tuple(
            (local_tag(tag), (node.findtext(tag) or "").strip(_NUMBER_PADDING))
            for tag in _UNIT_SI_RELATION
            if node.find(tag) is not None
        ),
    )


def _read_data_constr(entry: RawElement, skip_log: SkipLog) -> DataConstrInfo:
    node = entry.node
    rules_el = node.find(_DATA_CONSTR_RULES)
    rules = rules_el.findall(_DATA_CONSTR_RULE) if rules_el is not None else []
    # Every rule's CONSTR-LEVEL is family F9 and its closed table is judged, keyed at
    # `{constraint}/DATA-CONSTR-RULES`; a second rule's content is the mapper's note.
    for rule in rules:
        if rule.find(_CONSTR_LEVEL) is not None:
            _report_constr_level(entry.path, skip_log)
        _report_closed_table(rule, f"{entry.path}/DATA-CONSTR-RULES", "DATA-CONSTR-RULE", skip_log)
    internal_constrs = tuple((rule, internal) for rule in rules for internal in rule.findall(_INTERNAL_CONSTRS))
    first_internal = rules[0].find(_INTERNAL_CONSTRS) if rules else None
    first_phys = rules[0].find(_PHYS_CONSTRS) if rules else None
    level_text = rules[0].findtext(_CONSTR_LEVEL) if rules else None
    internal_texts: dict[str, str | None] = {"LOWER-LIMIT": None, "UPPER-LIMIT": None}
    internal_intervals: dict[str, str | None] = {"LOWER-LIMIT": None, "UPPER-LIMIT": None}
    internal_other: list[str] = []
    if first_internal is not None:
        for child in first_internal:
            if not _in_autosar_namespace(child):
                continue
            tag = local_tag(child.tag)
            if tag in internal_texts and internal_texts[tag] is None:
                internal_texts[tag] = (child.text or "").strip(_NUMBER_PADDING)
                interval = child.get("INTERVAL-TYPE")
                internal_intervals[tag] = interval.strip(_NUMBER_PADDING) if interval is not None else None
            else:
                internal_other.append(tag)
    lower: int | float | None = None
    upper: int | float | None = None
    texts: dict[str, str | None] = {"LOWER-LIMIT": None, "UPPER-LIMIT": None}
    intervals: dict[str, str | None] = {"LOWER-LIMIT": None, "UPPER-LIMIT": None}
    unreadable: set[str] = set()
    inexact: set[str] = set()
    if first_phys is not None:
        for tag, label in ((_LOWER_LIMIT, "LOWER-LIMIT"), (_UPPER_LIMIT, "UPPER-LIMIT")):
            text = first_phys.findtext(tag)
            texts[label] = None if text is None else text.strip(_NUMBER_PADDING)
            intervals[label] = _interval_type(first_phys, tag)
            if text is not None and _phys_limit_bounds_nothing(text, intervals[label], label):
                # Its own side's infinity, or `INFINITE`: the absent limit (the other side's is unreadable, below).
                continue
            value, no_exact_value = parse_exact_number(text, grammar="formula")
            if no_exact_value:
                # A finite number no model value holds exactly: see `DataConstrInfo.inexact_limits`.
                inexact.add(label)
            elif value is None and text is not None and text.strip(_NUMBER_PADDING):
                # Declared and unparsable, not the same fact as absent: `DataConstrInfo.unreadable_limits`.
                unreadable.add(label)
            elif tag == _LOWER_LIMIT:
                lower = value
            else:
                upper = value
    return DataConstrInfo(
        path=entry.path,
        name=node.findtext(_SHORT_NAME) or "",
        phys_lower=lower,
        phys_upper=upper,
        rule_count=len(rules),
        description=entry.description,
        unreadable_limits=frozenset(unreadable),
        inexact_limits=frozenset(inexact),
        phys_lower_text=texts["LOWER-LIMIT"],
        phys_upper_text=texts["UPPER-LIMIT"],
        phys_lower_interval=intervals["LOWER-LIMIT"],
        phys_upper_interval=intervals["UPPER-LIMIT"],
        phys_present=first_phys is not None and any(_in_autosar_namespace(child) for child in first_phys),
        internal_present=first_internal is not None,
        internal_lower_text=internal_texts["LOWER-LIMIT"],
        internal_upper_text=internal_texts["UPPER-LIMIT"],
        internal_lower_interval=internal_intervals["LOWER-LIMIT"],
        internal_upper_interval=internal_intervals["UPPER-LIMIT"],
        internal_other_tags=tuple(internal_other),
        internal_constrs=internal_constrs,
        level=parse_int_literal(level_text, grammar="integer"),
        level_text=None if level_text is None else level_text.strip(_NUMBER_PADDING),
    )


def _read_app_record_data_type(entry: RawElement, skip_log: SkipLog) -> AppRecordDataTypeInfo:
    del skip_log  # uniform dispatch signature (see _TYPED_ELEMENT_READERS); unused here
    node = entry.node
    elements: list[RecordElementInfo] = []
    elements_el = node.find(_ELEMENTS)
    if elements_el is not None:
        for el in elements_el.findall(_APPLICATION_RECORD_ELEMENT):
            elements.append(
                RecordElementInfo(
                    name=el.findtext(_SHORT_NAME) or "",
                    type_ref=el.findtext(_TYPE_TREF) or "",
                    description=_extract_description(el),
                )
            )
    return AppRecordDataTypeInfo(
        path=entry.path,
        name=node.findtext(_SHORT_NAME) or "",
        elements=elements,
        description=entry.description,
    )


def _read_app_array_data_type(entry: RawElement, skip_log: SkipLog) -> AppArrayDataTypeInfo:
    node = entry.node
    element_el = node.find(_ELEMENT)
    element_type_ref = ""
    max_number_of_elements = None
    if element_el is not None:
        # The ELEMENT is no model element (the array holds its type and length only), so its `DESC` is reported.
        _report_uncarried_description(
            _extract_description(element_el),
            f"{entry.path}/{element_el.findtext(_SHORT_NAME) or ''}",
            _ARRAY_ELEMENT_DESC_REASON,
            skip_log,
        )
        element_type_ref = element_el.findtext(_TYPE_TREF) or ""
        max_text = element_el.findtext(_MAX_NUMBER_OF_ELEMENTS)
        if max_text is not None:
            max_number_of_elements = parse_int_literal(max_text, grammar="formula")
    return AppArrayDataTypeInfo(
        path=entry.path,
        name=node.findtext(_SHORT_NAME) or "",
        element_type_ref=element_type_ref,
        max_number_of_elements=max_number_of_elements,
        description=entry.description,
    )


def _interval_type(scale: ET.Element, tag: str) -> str | None:
    """The ``INTERVAL-TYPE`` attribute of *scale*'s *tag* limit, stripped, or ``None`` when absent."""
    limit = scale.find(tag)
    value = limit.get("INTERVAL-TYPE") if limit is not None else None
    return None if value is None else value.strip(_NUMBER_PADDING)


def _compu_coefficients(scale: ET.Element, tag: str) -> tuple[str, ...]:
    """One ``COMPU-NUMERATOR`` / ``COMPU-DENOMINATOR``'s ``V`` texts, verbatim."""
    coeffs = scale.find(_COMPU_RATIONAL_COEFFS)
    if coeffs is None:
        return ()
    polynomial = coeffs.find(tag)
    if polynomial is None:
        return ()
    return tuple((v.text or "").strip(_NUMBER_PADDING) for v in polynomial.findall(_V))


def _read_compu_method(entry: RawElement, skip_log: SkipLog) -> CompuMethodInfo:
    node = entry.node
    category = node.findtext(_CATEGORY) or ""
    scales: list[CompuScaleInfo] = []
    rational_scales: list[CompuRationalScaleInfo] = []
    # One walk of `COMPU-INTERNAL-TO-PHYS/COMPU-SCALES/COMPU-SCALE` whatever the category; the branches below
    # differ only in what they read off the scales.
    c2p_el = node.find(_COMPU_INTERNAL_TO_PHYS)
    all_scales_el = c2p_el.find(_COMPU_SCALES) if c2p_el is not None else None
    raw_scales = all_scales_el.findall(_COMPU_SCALE) if all_scales_el is not None else []
    scale_count = len(raw_scales)
    # A scale is no referrable, so what it holds is keyed at `{compu method}/COMPU-INTERNAL-TO-PHYS`.
    scale_key = f"{entry.path}/COMPU-INTERNAL-TO-PHYS"
    for scale in raw_scales:
        _report_closed_table(scale, scale_key, "COMPU-SCALE", skip_log)
        label = scale.findtext(_SHORT_LABEL)
        # The content the other category reads is not read by this one: a
        # LINEAR scale's COMPU-CONST, a TEXTTABLE scale's COMPU-RATIONAL-COEFFS.
        foreign = scale.find(_COMPU_RATIONAL_COEFFS if category == "TEXTTABLE" else _COMPU_CONST)
        if foreign is not None:
            _report_unread_child(foreign, scale_key, "COMPU-SCALE", set(), skip_log)
        if category != "TEXTTABLE":
            # A LINEAR scale's label (F10) and its DESC (F14): the writer writes the coefficients alone, so neither
            # comes back.
            if label is not None and label.strip(_NUMBER_PADDING):
                if category == "LINEAR":
                    _report_linear_scale_short_label(entry.path, skip_log)
                else:
                    _report_scale_short_label(entry.path, category or "LINEAR", skip_log)
            _report_text_form(
                scale,
                _DESC,
                _L2,
                scale_key,
                skip_log,
                uncarried=f"DESC of a {category or 'LINEAR'} COMPU-SCALE not carried: SWCraft writes no description "
                "on the scale",
            )
        else:
            # The writer writes the literal (VT) as the label, so a label that says the same is carried by
            # equivalence and one that does not is F10.
            const_el = scale.find(_COMPU_CONST)
            literal = const_el.findtext(_VT) if const_el is not None else None
            if label is not None and label.strip(_NUMBER_PADDING) != (literal or "").strip(_NUMBER_PADDING):
                _report_scale_short_label(entry.path, "TEXTTABLE", skip_log)
    if category != "TEXTTABLE":
        rational_scales = [
            CompuRationalScaleInfo(
                numerator=_compu_coefficients(scale, _COMPU_NUMERATOR),
                denominator=_compu_coefficients(scale, _COMPU_DENOMINATOR),
                lower_limit=_declared_text(scale, _LOWER_LIMIT),
                lower_interval=_interval_type(scale, _LOWER_LIMIT),
                upper_limit=_declared_text(scale, _UPPER_LIMIT),
                upper_interval=_interval_type(scale, _UPPER_LIMIT),
            )
            for scale in raw_scales
        ]
    else:
        for idx, scale in enumerate(raw_scales):
            lower_text = scale.findtext(_LOWER_LIMIT)
            const_el = scale.find(_COMPU_CONST)
            literal = const_el.findtext(_VT) if const_el is not None else None
            lower_value = parse_int_literal(lower_text, grammar="formula")
            if lower_value is not None and literal is not None:
                scales.append(
                    CompuScaleInfo(
                        value=lower_value,
                        literal=literal,
                        description=_extract_description(scale, scale_key, skip_log),
                        upper_limit=_declared_text(scale, _UPPER_LIMIT),
                        lower_interval=_interval_type(scale, _LOWER_LIMIT),
                        upper_interval=_interval_type(scale, _UPPER_LIMIT),
                        states_symbol=scale.find(_SYMBOL) is not None,
                        short_label=scale.findtext(_SHORT_LABEL),
                    )
                )
            else:
                # Malformed COMPU-SCALE (unparseable/missing LOWER-LIMIT, or missing VT literal text): never
                # silently dropped, or the enum would import with fewer literals than the source defined.
                skip_log.record(
                    path=scale_key,
                    tag="COMPU-SCALE",
                    reason=(
                        f"malformed COMPU-SCALE [{idx}] (LOWER-LIMIT={lower_text!r}, "
                        f"VT={literal!r}) -- entry dropped, enum literal lost"
                    ),
                    category=SkipCategory.NOT_SUPPORTED,
                    effect="abort",
                )
    return CompuMethodInfo(
        path=entry.path,
        name=node.findtext(_SHORT_NAME) or "",
        category=category,
        scales=scales,
        description=entry.description,
        rational_scales=rational_scales,
        scale_count=scale_count,
        unit_ref=node.findtext(_UNIT_REF),
        phys_to_internal=node.find(_COMPU_PHYS_TO_INTERNAL) is not None,
    )


def _read_data_type_mapping_set(entry: RawElement, skip_log: SkipLog) -> DataTypeMappingSetInfo:
    node = entry.node
    maps: list[DataTypeMapInfo] = []
    maps_el = node.find(_DATA_TYPE_MAPS)
    if maps_el is not None:
        for idx, m in enumerate(maps_el.findall(_DATA_TYPE_MAP)):
            app_ref = m.findtext(_APPLICATION_DATA_TYPE_REF)
            impl_ref = m.findtext(_IMPLEMENTATION_DATA_TYPE_REF)
            missing = [
                side
                for side, ref in (("APPLICATION-DATA-TYPE-REF", app_ref), ("IMPLEMENTATION-DATA-TYPE-REF", impl_ref))
                if ref is None or not ref.strip()
            ]
            if missing or app_ref is None or impl_ref is None:
                # A map is a pair: with one side absent (or blank) it pairs nothing. Never dropped in silence: the
                # set is still read and this map is a skip entry, which `import component` refuses, because the
                # component would otherwise arrive without a pairing its exporter declared and leave a project
                # `check-model` and `generate arxml` refuse.
                skip_log.record(
                    path=f"{entry.path}/DATA-TYPE-MAPS",
                    tag="DATA-TYPE-MAP",
                    reason=(
                        f"a DATA-TYPE-MAP [{idx}] with no {' and no '.join(missing)} pairs nothing -- the map is "
                        "dropped"
                    ),
                    category=SkipCategory.NOT_SUPPORTED,
                    effect="abort",
                )
                continue
            maps.append(DataTypeMapInfo(application_ref=app_ref, implementation_ref=impl_ref))
    return DataTypeMappingSetInfo(
        path=entry.path,
        name=node.findtext(_SHORT_NAME) or "",
        maps=maps,
        description=entry.description,
    )


def _read_sr_interface(entry: RawElement, skip_log: SkipLog) -> SRInterfaceInfo:
    node = entry.node
    # The interface's closed table, and each data element's with its data definition's.
    _report_closed_table(node, entry.path, "SENDER-RECEIVER-INTERFACE", skip_log)
    data_elements: list[SRDataElementInfo] = []
    des_el = node.find(_DATA_ELEMENTS)
    if des_el is not None:
        for vdp in des_el.findall(_VARIABLE_DATA_PROTOTYPE):
            element_name = vdp.findtext(_SHORT_NAME) or ""
            element_path = f"{entry.path}/{element_name}"
            _report_closed_table(vdp, element_path, "VARIABLE-DATA-PROTOTYPE", skip_log)
            _report_conditional(vdp, element_path, "VARIABLE-DATA-PROTOTYPE", skip_log)
            init_ref, init_reason = _read_init_value_ref(vdp)
            if init_reason is not None:
                # The element still imports, without its init: the field is optional and a guessed init is not.
                # What must not happen is losing it in silence.
                skip_log.record(
                    path=element_path,
                    tag="INIT-VALUE",
                    reason=init_reason,
                    category=SkipCategory.NOT_SUPPORTED,
                    effect="abort",
                )
            conditional = _find_sw_data_def_props_conditional(vdp)
            data_elements.append(
                SRDataElementInfo(
                    name=element_name,
                    type_ref=vdp.findtext(_TYPE_TREF) or "",
                    init_ref=init_ref,
                    init_unreadable=init_reason is not None,
                    description=_extract_description(vdp, element_path, skip_log),
                    sw_impl_policy=None if conditional is None else _declared_text(conditional, _SW_IMPL_POLICY),
                    sw_calibration_access=(
                        None if conditional is None else _declared_text(conditional, _SW_CALIBRATION_ACCESS)
                    ),
                )
            )
    return SRInterfaceInfo(
        path=entry.path,
        name=node.findtext(_SHORT_NAME) or "",
        description=entry.description,
        data_elements=data_elements,
        is_service=_declared_text(node, _IS_SERVICE),
    )


def _read_cs_interface(entry: RawElement, skip_log: SkipLog) -> CSInterfaceInfo:
    node = entry.node
    # The interface's and each argument's closed table, the application errors (F7) and each operation's
    # POSSIBLE-ERROR-REFS (F8).
    _report_closed_table(node, entry.path, "CLIENT-SERVER-INTERFACE", skip_log)
    _report_application_errors(node, entry.path, skip_log)
    operations: list[CSOperationInfo] = []
    ops_el = node.find(_OPERATIONS)
    if ops_el is not None:
        for op_el in ops_el.findall(_CLIENT_SERVER_OPERATION):
            operation_path = f"{entry.path}/{op_el.findtext(_SHORT_NAME) or ''}"
            refs = op_el.find(_POSSIBLE_ERROR_REFS)
            if refs is not None and len(refs):
                _report_possible_error_refs(operation_path, skip_log)
            arguments: list[CSArgumentInfo] = []
            args_el = op_el.find(_ARGUMENTS)
            if args_el is not None:
                for arg_el in args_el.findall(_ARGUMENT_DATA_PROTOTYPE):
                    argument_path = f"{operation_path}/{arg_el.findtext(_SHORT_NAME) or ''}"
                    _report_closed_table(arg_el, argument_path, "ARGUMENT-DATA-PROTOTYPE", skip_log)
                    _report_conditional(arg_el, argument_path, "ARGUMENT-DATA-PROTOTYPE", skip_log)
                    arguments.append(
                        CSArgumentInfo(
                            name=arg_el.findtext(_SHORT_NAME) or "",
                            type_ref=arg_el.findtext(_TYPE_TREF) or "",
                            direction=arg_el.findtext(_DIRECTION) or "",
                            description=_extract_description(arg_el, argument_path, skip_log),
                            server_argument_impl_policy=_declared_text(arg_el, _SERVER_ARGUMENT_IMPL_POLICY),
                        )
                    )
            operations.append(
                CSOperationInfo(
                    name=op_el.findtext(_SHORT_NAME) or "",
                    arguments=arguments,
                    description=_extract_description(op_el, operation_path, skip_log),
                )
            )
    return CSInterfaceInfo(
        path=entry.path,
        name=node.findtext(_SHORT_NAME) or "",
        description=entry.description,
        operations=operations,
        is_service=_declared_text(node, _IS_SERVICE),
    )


def _read_swc_ports(node: ET.Element, swc_path: str, skip_log: SkipLog) -> list[SwcPortInfo]:
    """Read every P-/R-/PR-PORT-PROTOTYPE directly inside `node`'s PORTS block.

    Each port's path is one segment under `swc_path`. A port kind not in `_PORT_KINDS` is skipped and reported,
    never silently ignored."""
    ports: list[SwcPortInfo] = []
    ports_el = node.find(_PORTS)
    if ports_el is None:
        return ports
    for position, child in enumerate(list(ports_el)):
        tag = local_tag(child.tag)
        if tag not in _PORT_KINDS:
            child_name = child.findtext(_SHORT_NAME) or "<unnamed>"
            skip_log.record(
                path=f"{swc_path}/{child_name}",
                tag=tag,
                reason="unrecognized port kind inside PORTS",
                category=SkipCategory.NOT_SUPPORTED,
                effect="abort",
            )
            continue
        name = child.findtext(_SHORT_NAME) or ""
        if tag == "R-PORT-PROTOTYPE":
            interface_ref = child.findtext(_REQUIRED_INTERFACE_TREF)
        else:
            # PR-PORT-PROTOTYPE carries both refs but has no SWCraft equivalent, so only the provided one is read.
            interface_ref = child.findtext(_PROVIDED_INTERFACE_TREF)
        port_path = f"{swc_path}/{name}"
        com_specs = _read_com_specs(child)
        ports.append(
            SwcPortInfo(
                path=port_path,
                name=name,
                kind=tag,
                interface_ref=interface_ref,
                description=_extract_description(child),
                com_spec_inits=_read_port_com_spec_inits(child, port_path, port_position=position),
                server_com_specs=[shape.server for shape in com_specs if shape.server is not None],
                data_element_com_specs=[shape.data for shape in com_specs if shape.data is not None],
                com_spec_shapes=com_specs,
                position=position,
            )
        )
    return ports


def _read_com_specs(port: ET.Element) -> list[ComSpecInfo]:
    """Every com-spec of *port*'s two blocks, of every kind, in document order (:class:`ComSpecInfo`).

    One walk reads a com-spec's shape **and** its values: a sender/receiver com-spec's
    :class:`DataElementComSpecInfo`, of either block and every kind of :data:`_SENDER_RECEIVER_COM_SPEC_KINDS`
    (which block a kind may sit in is the mapper's to judge); the :class:`ServerComSpecInfo` of a
    ``SERVER-COM-SPEC`` in the provided block (a server com-spec is a provider's, and its queue length is the one
    ``[constr_1128]`` speaks of); and the :class:`ClientComSpecInfo` of a ``CLIENT-COM-SPEC`` in the required
    block. ``SwcPortInfo``'s two value lists are drawn from this one list, so the three cannot fall out of step.

    Anything outside the AUTOSAR namespace -- a com-spec in a block, a leaf child of a com-spec -- is the index's
    own entry and is read as no com-spec and no value here.
    """
    com_specs: list[ComSpecInfo] = []
    for block_tag in (_PROVIDED_COM_SPECS, _REQUIRED_COM_SPECS):
        block = port.find(block_tag)
        if block is None:
            continue
        for position, com_spec in enumerate(list(block)):
            if not _in_autosar_namespace(com_spec):
                continue
            tag = local_tag(com_spec.tag)
            data: DataElementComSpecInfo | None = None
            server: ServerComSpecInfo | None = None
            client: ClientComSpecInfo | None = None
            if tag in _SENDER_RECEIVER_COM_SPEC_KINDS:
                texts: dict[str, str] = {}
                for child in list(com_spec):
                    if _in_autosar_namespace(child) and len(child) == 0 and child.text is not None:
                        texts.setdefault(local_tag(child.tag), child.text.strip(_NUMBER_PADDING))
                data = DataElementComSpecInfo(
                    com_spec_tag=tag,
                    data_element_ref=com_spec.findtext(_DATA_ELEMENT_REF),
                    texts=texts,
                    filter_is_silent=_filter_is_silent(com_spec),
                    block_tag=local_tag(block_tag),
                    position=position,
                )
            elif tag == "SERVER-COM-SPEC" and block_tag == _PROVIDED_COM_SPECS:
                server = ServerComSpecInfo(
                    operation_ref=com_spec.findtext(_OPERATION_REF),
                    queue_length=_declared_text(com_spec, _QUEUE_LENGTH),
                )
            elif tag == "CLIENT-COM-SPEC" and block_tag == _REQUIRED_COM_SPECS:
                client = ClientComSpecInfo(operation_ref=com_spec.findtext(_OPERATION_REF))
            com_specs.append(
                ComSpecInfo(
                    block_tag=local_tag(block_tag),
                    tag=tag,
                    child_tags=tuple(local_tag(child.tag) for child in list(com_spec) if isinstance(child.tag, str)),
                    foreign_child_tags=tuple(
                        local_tag(child.tag)
                        for child in list(com_spec)
                        if isinstance(child.tag, str) and not _in_autosar_namespace(child)
                    ),
                    attributes=tuple(local_tag(attribute) for attribute in com_spec.attrib),
                    data=data,
                    server=server,
                    client=client,
                )
            )
    return com_specs


def _filter_is_silent(com_spec: ET.Element) -> bool:
    """No ``FILTER``, or one holding exactly ``DATA-FILTER-TYPE`` ``ALWAYS`` and nothing else.

    ``ALWAYS`` means *no filtering is performed so that the message always passes* (the ``DataFilterTypeEnum``
    table: SWCT R4.2.2 Table 4.66, R4.3.0 Table 4.68), which is what an absent filter means, so such a filter is
    carried by equivalence -- on a ``NONQUEUED-RECEIVER-COM-SPEC`` alone, the one kind both licensed schemas give a
    ``FILTER``. Any other ``DATA-FILTER`` child (``MASK``, ``MAX``, ``MIN``, ``OFFSET``, ``PERIOD``, ``X``), an
    attribute on either element, or another filter type is not.
    """
    filter_el = com_spec.find(_FILTER)
    if filter_el is None:
        return True
    children = list(filter_el)
    return (
        not filter_el.attrib
        and len(children) == 1
        and children[0].tag == _DATA_FILTER_TYPE
        and not children[0].attrib
        and len(children[0]) == 0
        and (children[0].text or "").strip(_NUMBER_PADDING) == "ALWAYS"
    )


def parse_xsd_boolean(text: str | None) -> bool | None:
    """An ``xsd:boolean`` text as a bool: ``true``/``1`` and ``false``/``0``, or ``None``.

    XML's own white space (:data:`_NUMBER_PADDING`) is stripped first; ``None`` for an absent element or any other
    text, so an unreadable value is never mistaken for ``false``.
    """
    if text is None:
        return None
    value = text.strip(_NUMBER_PADDING)
    if value in ("true", "1"):
        return True
    if value in ("false", "0"):
        return False
    return None


def _read_port_com_spec_inits(port: ET.Element, port_path: str, *, port_position: int) -> list[PortComSpecInitInfo]:
    """Every DATA-ELEMENT COM-SPEC on `port` that carries an INIT-VALUE.

    A port-level init *overrides* the interface element's own: a definition on the com-spec supersedes one on the
    data prototype (``[TPS_SWCT_01182]``). The model carries it on the port's ``com_specs`` entry.

    A COM-SPEC without an INIT-VALUE contributes nothing. A COM-SPEC outside `DATA_ELEMENT_COM_SPEC_KINDS`
    initializes something that is not a data element (a calibration PARAMETER, an NV block), whose only possible
    verdict would be a defect asserted about a construct AUTOSAR defines without a DATA-ELEMENT-REF.

    **What this does not reach.** A COM-SPEC kind that names a data element and is not one of the two kept tags --
    a vendor extension, or a kind a later AUTOSAR revision adds -- is dropped here with no line of its own; its
    port reports it only when the port's interface could not be mapped. The filter is a claim about AUTOSAR's
    schema, not a measurement: a calibration-heavy export is where the dropped kinds live, and nothing here bounds
    that cost.
    """
    inits: list[PortComSpecInitInfo] = []
    for block_tag in (_PROVIDED_COM_SPECS, _REQUIRED_COM_SPECS):
        block = port.find(block_tag)
        if block is None:
            continue
        for position, com_spec in enumerate(list(block)):
            if local_tag(com_spec.tag) not in DATA_ELEMENT_COM_SPEC_KINDS:
                continue
            if com_spec.find(_INIT_VALUE) is None:
                continue
            constant_ref, value, reason = _read_port_init_value(com_spec)
            inits.append(
                PortComSpecInitInfo(
                    port_path=port_path,
                    block_tag=local_tag(block_tag),
                    com_spec_tag=local_tag(com_spec.tag),
                    data_element_ref=com_spec.findtext(_DATA_ELEMENT_REF),
                    constant_ref=constant_ref,
                    # This reason names the inner tags actually found; the importer quotes it, never a guess.
                    unreadable_reason=reason,
                    value=value,
                    position=position,
                    port_position=port_position,
                )
            )
    return inits


def _read_port_init_value(com_spec: ET.Element) -> tuple[str | None, ConstantValue | None, str | None]:
    """A port com-spec's ``INIT-VALUE`` as ``(constant path, value in place, reason)``.

    Exactly one of the three is set. A ``CONSTANT-REFERENCE`` gives its path, or :func:`_read_init_value_ref`'s
    reason when it names none; an ``INIT-VALUE`` holding exactly one other value specification is read by
    :func:`_read_value_specification` and gives that value or that reader's reason. Anything else -- no child, or
    several -- keeps :func:`_read_init_value_ref`'s reason. An element's own ``INIT-VALUE`` is read by
    :func:`_read_init_value_ref` alone; an inter-runnable variable's is read by this, because the writer writes a
    variable's value in place, as a com-spec's own.
    """
    init_el = com_spec.find(_INIT_VALUE)
    constant_ref, reason = _read_init_value_ref(com_spec)
    if constant_ref is not None or init_el is None or init_el.find(_CONSTANT_REFERENCE) is not None:
        return constant_ref, None, reason
    children = list(init_el)
    if len(children) != 1:
        return None, None, reason
    value, value_reason = _read_value_specification(children[0])
    if value is None:
        return None, None, f"INIT-VALUE written in place was not read: {value_reason}"
    return None, value, None


def _find_internal_behavior(node: ET.Element) -> ET.Element | None:
    """Descend INTERNAL-BEHAVIORS/SWC-INTERNAL-BEHAVIOR, the wrapper a SWC's runnables and events live under.

    Only the first behavior is read; the schema admits more, and every further one is a skip entry of its own
    (`_report_further_behaviours`)."""
    behaviors = node.find(_INTERNAL_BEHAVIORS)
    if behaviors is None:
        return None
    return behaviors.find(_SWC_INTERNAL_BEHAVIOR)


def _behavior_path(swc_path: str, behavior: ET.Element | None) -> str:
    """`behavior`'s path one segment under the SWC, as `_index_named_element` assigns it (SkipLog-consistent)."""
    if behavior is None:
        return swc_path
    name = behavior.findtext(_SHORT_NAME)
    return f"{swc_path}/{name}" if name else swc_path


def _read_runnable_accesses(
    r_el: ET.Element, runnable_path: str, claimants: set[int], skip_log: SkipLog
) -> list[SwcAccessInfo]:
    """Read the access containers of one RUNNABLE-ENTITY into `SwcAccessInfo`s, in document order.

    A child of a container that is not one of the element tags the container may hold (`_ACCESS_CONTAINERS`) is
    skipped and reported, never silently ignored. A SHORT-NAME that repeats within one runnable is read **once**,
    the first occurrence: the second is what `_index_named_element` already dropped and reported as a duplicate
    path, and this read walks the raw block, so it re-applies the index's decision instead of reporting the same
    element twice. *claimants* is :func:`_first_claimants` of the runnable, which claims a SHORT-NAME across every
    descendant, as the index does, before the tag is looked at. Not reached: an access with no SHORT-NAME element
    has no path to collide on and is always read; an empty one is the index's path `{runnable}/`, claimed like any
    other name. A stated VARIATION-POINT on a read access is one line (`_report_access_variation_point`)."""
    accesses: list[SwcAccessInfo] = []
    for container in list(r_el):
        container_tag = local_tag(container.tag)
        expected = _ACCESS_CONTAINERS.get(container_tag)
        if expected is None:
            continue
        for child in list(container):
            tag = local_tag(child.tag)
            short_name = child.findtext(_SHORT_NAME)  # None: no element; "": empty, a path to the index
            path = f"{runnable_path}/{short_name or '<unnamed>'}"
            if short_name is not None and id(child) not in claimants:
                continue
            if tag not in expected:
                skip_log.record(
                    path=path,
                    tag=tag,
                    reason=f"unrecognized access kind inside {container_tag} (only {' or '.join(expected)} is read)",
                    category=SkipCategory.NOT_SUPPORTED,
                    effect="abort",
                )
                continue
            if tag == "VARIABLE-ACCESS":
                iref = child.find(f"{_ACCESSED_VARIABLE}/{_AUTOSAR_VARIABLE_IREF}")
                port_ref = iref.findtext(_PORT_PROTOTYPE_REF) if iref is not None else None
                target_ref = iref.findtext(_TARGET_DATA_PROTOTYPE_REF) if iref is not None else None
            else:
                iref = child.find(_OPERATION_IREF)
                port_ref = iref.findtext(_CONTEXT_R_PORT_REF) if iref is not None else None
                target_ref = iref.findtext(_TARGET_REQUIRED_OPERATION_REF) if iref is not None else None
            _report_access_variation_point(child, path, tag, skip_log)
            _report_uncarried_description(_extract_description(child), path, _ACCESS_POINT_DESC_REASON, skip_log)
            accesses.append(
                SwcAccessInfo(
                    path=path,
                    name=short_name or "",
                    kind=container_tag,
                    tag=tag,
                    port_ref=port_ref,
                    target_ref=target_ref,
                    # A call point's TIMEOUT as written, judged by the mapper.
                    timeout_text=None if tag == "VARIABLE-ACCESS" else _declared_text(child, _TIMEOUT),
                )
            )
    return accesses


def _read_runnable_local_accesses(
    r_el: ET.Element, runnable_path: str, claimants: set[int], skip_log: SkipLog
) -> list[SwcLocalAccessInfo]:
    """Read the two local-variable containers of one RUNNABLE-ENTITY, in document order.

    The port containers' rules (:func:`_read_runnable_accesses`) apply: a child that is not a ``VARIABLE-ACCESS`` is
    an unrecognized-kind entry, and a ``SHORT-NAME`` the runnable already claimed (port accesses share its name
    space) is the index's duplicate line. Every form an access's ``ACCESSED-VARIABLE`` states is recorded
    (:attr:`SwcLocalAccessInfo.reference_forms`). A stated ``SCOPE`` is one line, because the model holds no scope
    and the writer writes none. A stated ``VARIATION-POINT`` is one line (:func:`_report_access_variation_point`).
    **Not reached**: inside an access, what the reader does not select (an annotation, say).
    """
    accesses: list[SwcLocalAccessInfo] = []
    for container in list(r_el):
        if not _in_autosar_namespace(container):
            continue
        container_tag = local_tag(container.tag)
        if container_tag not in _LOCAL_ACCESS_CONTAINERS:
            continue
        for child in list(container):
            if not _in_autosar_namespace(child):
                continue
            tag = local_tag(child.tag)
            short_name = child.findtext(_SHORT_NAME)  # None: no element; "": empty, a path to the index
            path = f"{runnable_path}/{short_name or '<unnamed>'}"
            if short_name is not None and id(child) not in claimants:
                continue
            if tag != "VARIABLE-ACCESS":
                skip_log.record(
                    path=path,
                    tag=tag,
                    reason=f"unrecognized access kind inside {container_tag} (only VARIABLE-ACCESS is read)",
                    category=SkipCategory.NOT_SUPPORTED,
                    effect="abort",
                )
                continue
            accessed = child.find(_ACCESSED_VARIABLE)
            forms = (
                tuple(local_tag(form.tag) for form in list(accessed) if _in_autosar_namespace(form))
                if accessed is not None
                else ()
            )
            scope = _declared_text(child, _SCOPE)
            if scope is not None:
                skip_log.record(
                    path=path,
                    tag="SCOPE",
                    reason=(
                        f"SCOPE {scope} of a VARIABLE-ACCESS in {container_tag} not carried: SWCraft models no "
                        "access scope and writes none"
                    ),
                    category=SkipCategory.NOT_SUPPORTED,
                    effect="abort",
                )
            _report_access_variation_point(child, path, tag, skip_log)
            _report_uncarried_description(_extract_description(child), path, _ACCESS_POINT_DESC_REASON, skip_log)
            accesses.append(
                SwcLocalAccessInfo(
                    path=path,
                    name=short_name or "",
                    container=container_tag,
                    tag=tag,
                    reference_forms=forms,
                    variable_ref=accessed.findtext(_LOCAL_VARIABLE_REF) if accessed is not None else None,
                    scope=scope,
                )
            )
    return accesses


def _read_runnable_area_refs(
    r_el: ET.Element, runnable_path: str, claimants: set[int], skip_log: SkipLog
) -> list[SwcAreaRefInfo]:
    """Read the two exclusive area reference wrappers of one RUNNABLE-ENTITY, in document order.

    Each wrapper holds its one reference tag (:data:`_AREA_REF_ROLES`); any other child is one line here, at the
    runnable's path for a reference and at the item's own for a named one (:func:`_record_item`, fed by
    *claimants*, the runnable's :func:`_first_claimants`), because :func:`_report_unread_runnable` skips a wrapper
    the table reads. A reference is read as written, its text and its ``DEST``; whether it names an area of its own
    behaviour is the mapper's to judge. A reference holds no child, so it has no variation point to report.
    """
    refs: list[SwcAreaRefInfo] = []
    for container in list(r_el):
        if not _in_autosar_namespace(container):
            continue
        role = local_tag(container.tag)
        expected = _AREA_REF_ROLES.get(role)
        if expected is None:
            continue
        for item in list(container):
            if not _in_autosar_namespace(item):
                continue
            tag = local_tag(item.tag)
            if tag != expected:
                reason = f"unrecognized child inside {role} (only {expected} is read)"
                _record_item(item, runnable_path, claimants, reason, skip_log)
                continue
            refs.append(
                SwcAreaRefInfo(path=runnable_path, role=role, tag=tag, ref=item.text or "", dest=item.get("DEST"))
            )
    return refs


def _read_swc_variables(
    behavior: ET.Element | None, behavior_path: str, claimants: set[int], skip_log: SkipLog
) -> list[SwcVariableInfo]:
    """Read every inter-runnable variable of *behavior*, the explicit wrapper's first.

    A variable is a direct child of the behaviour's name space, so *claimants* is the behaviour's
    :func:`_first_claimants`: a named variable that is not one repeats a name the index already dropped and
    reported, and is not read. Each variable's own children are judged by the data prototype's closed tables
    (:func:`_report_closed_table`, :func:`_report_conditional` with ``variable=True``). Every stated
    ``SW-IMPL-POLICY`` is one line, whatever its literal, because the model holds no policy on a variable and the
    writer writes none: ``[constr_2038]`` / ``[constr_2039]`` admit only the default ``standard``, so a
    ``STANDARD`` loses no meaning and is descriptive, any other is behavioural.
    """
    variables: list[SwcVariableInfo] = []
    if behavior is None:
        return variables
    for role in _VARIABLE_ROLES:
        for wrapper in behavior.findall(_ar(role)):
            for vdp in wrapper.findall(_VARIABLE_DATA_PROTOTYPE):
                declared_name = vdp.findtext(_SHORT_NAME)
                if declared_name is not None and id(vdp) not in claimants:
                    continue
                name = declared_name or ""
                path = f"{behavior_path}/{name}"
                _report_closed_table(vdp, path, "VARIABLE-DATA-PROTOTYPE", skip_log)
                _report_conditional(vdp, path, "VARIABLE-DATA-PROTOTYPE", skip_log, variable=True)
                conditional = _find_sw_data_def_props_conditional(vdp)
                policy = None if conditional is None else _declared_text(conditional, _SW_IMPL_POLICY)
                if policy is not None:
                    skip_log.record(
                        f"{path}/SW-DATA-DEF-PROPS",
                        "SW-IMPL-POLICY",
                        f"SW-IMPL-POLICY {policy} on an inter-runnable variable not carried: SWCraft writes none, "
                        "and [constr_2038] / [constr_2039] admit STANDARD alone",
                        category=SkipCategory.NOT_SUPPORTED,
                        effect=REPORT if policy == "STANDARD" else ABORT,
                    )
                constant_ref: str | None = None
                value: ConstantValue | None = None
                reason: str | None = None
                if vdp.find(_INIT_VALUE) is not None:
                    constant_ref, value, reason = _read_port_init_value(vdp)
                type_el = vdp.find(_TYPE_TREF)
                variables.append(
                    SwcVariableInfo(
                        path=path,
                        name=name,
                        role=role,
                        type_ref=(type_el.text or "") if type_el is not None else "",
                        type_dest=type_el.get("DEST") if type_el is not None else None,
                        description=_extract_description(vdp, path, skip_log),
                        init_constant_ref=constant_ref,
                        init_value=value,
                        init_unreadable=reason,
                        sw_impl_policy=policy,
                        sw_calibration_access=(
                            None if conditional is None else _declared_text(conditional, _SW_CALIBRATION_ACCESS)
                        ),
                    )
                )
    return variables


#: The children of an ``EXCLUSIVE-AREA`` someone else reports: the name and the ``DESC`` are read; the
#: documentation group, a ``DESC`` not carried exactly and ``ADMIN-DATA`` are the index's lines; ``VARIATION-POINT``
#: is :func:`_report_access_variation_point`'s one line. Anything else is the closed tables' unread line.
_AREA_CHILDREN_REPORTED_ELSEWHERE = frozenset(
    {"SHORT-NAME", "DESC", "ADMIN-DATA", "VARIATION-POINT", *_DOCUMENTATION_GROUP_TAGS},
)


def _read_swc_exclusive_areas(
    behavior: ET.Element | None, behavior_path: str, claimants: set[int], skip_log: SkipLog
) -> list[SwcExclusiveAreaInfo]:
    """Read every exclusive area of *behavior*, in document order.

    An area is a direct child of the behaviour's name space, so *claimants* is the behaviour's
    :func:`_first_claimants`: a named area that is not one repeats a name the index already dropped and reported,
    and is not read, so every reference to that path binds to the area that claimed it first. A stated
    ``VARIATION-POINT`` is one loss line at the area (:func:`_report_access_variation_point`): the area is written
    unconditionally, so its condition is lost.
    """
    areas: list[SwcExclusiveAreaInfo] = []
    if behavior is None:
        return areas
    for wrapper in behavior.findall(_EXCLUSIVE_AREAS):
        for area in wrapper.findall(_EXCLUSIVE_AREA):
            declared_name = area.findtext(_SHORT_NAME)
            if declared_name is not None and id(area) not in claimants:
                continue
            name = declared_name or ""
            path = f"{behavior_path}/{name}"
            kept = _first_claimants(area)
            for child in list(area):
                if not _in_autosar_namespace(child):
                    continue
                tag = local_tag(child.tag)
                if tag in _AREA_CHILDREN_REPORTED_ELSEWHERE:
                    continue
                _report_unread_child(child, path, "EXCLUSIVE-AREA", kept, skip_log)
            _report_access_variation_point(area, path, "EXCLUSIVE-AREA", skip_log)
            areas.append(SwcExclusiveAreaInfo(path=path, name=name, description=_extract_description(area)))
    return areas


def _read_swc_runnables(
    behavior: ET.Element | None, behavior_path: str, claimants: set[int], skip_log: SkipLog
) -> list[SwcRunnableInfo]:
    """Read every RUNNABLE-ENTITY of *behavior* the index keeps, and report what each carries unread.

    *claimants* is :func:`_first_claimants` of the behaviour. A named runnable that is not one is a later
    declaration of a SHORT-NAME the index dropped and reported as a duplicate path, and its children are not
    scanned. After that:

    * the name was taken by an earlier **runnable**: it is still read, because ``map_swcs`` owns that case -- a
      ``duplicate runnable name`` skip that chain-skips every access the declaration carried;
    * the name was taken by an earlier sibling of **another** kind (an event, an exclusive area): it is not read,
      so reader and index agree it was dropped.

    A runnable with no SHORT-NAME element has no path to collide on; it is read, and its children are not scanned
    (the mapper reports its blank name).
    """
    runnables: list[SwcRunnableInfo] = []
    if behavior is None:
        return runnables
    runnables_el = behavior.find(_RUNNABLES)
    if runnables_el is None:
        return runnables
    runnable_names: set[str] = set()
    for r_el in runnables_el.findall(_RUNNABLE_ENTITY):
        declared_name = r_el.findtext(_SHORT_NAME)
        claimant = declared_name is None or id(r_el) in claimants
        if not claimant and declared_name not in runnable_names:
            continue
        if declared_name is not None:
            runnable_names.add(declared_name)
        name = declared_name or ""
        min_text = r_el.findtext(_MINIMUM_START_INTERVAL)
        min_start_interval = parse_float_literal(min_text)
        runnable_path = f"{behavior_path}/{name}"
        runnable_claimants = _first_claimants(r_el)
        if declared_name is not None and claimant:
            _report_unread_runnable(r_el, runnable_path, runnable_claimants, skip_log)
        concurrency_text = _declared_text(r_el, _CAN_BE_INVOKED_CONCURRENTLY)
        concurrency = parse_xsd_boolean(concurrency_text)
        runnables.append(
            SwcRunnableInfo(
                path=runnable_path,
                name=name,
                min_start_interval=min_start_interval,
                description=_extract_description(r_el),
                accesses=_read_runnable_accesses(r_el, runnable_path, runnable_claimants, skip_log),
                local_accesses=_read_runnable_local_accesses(r_el, runnable_path, runnable_claimants, skip_log),
                area_refs=_read_runnable_area_refs(r_el, runnable_path, runnable_claimants, skip_log),
                result_points=_read_runnable_result_points(r_el, runnable_path, runnable_claimants, skip_log),
                can_be_invoked_concurrently=concurrency,
                unreadable_concurrency=concurrency_text if concurrency is None else None,
                # The raw text, judged by the mapper.
                symbol=r_el.findtext(_SYMBOL),
                unreadable_min_start_interval=min_text if min_text is not None and min_start_interval is None else None,
            )
        )
    return runnables


def _read_swc_events(behavior: ET.Element | None, behavior_path: str, skip_log: SkipLog) -> list[SwcEventInfo]:
    """Read every TIMING-, INIT-, OPERATION-INVOKED-, DATA-RECEIVED- and ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT.

    Only those directly inside `behavior`'s EVENTS block are read. Another event kind is skipped and reported, never
    silently ignored. A timing and a data-received event each have a closed table, and so do their disabled modes;
    a data-received event's ``DATA-IREF`` has one of its own (:func:`_read_data_iref`)."""
    events: list[SwcEventInfo] = []
    if behavior is None:
        return events
    events_el = behavior.find(_EVENTS)
    if events_el is None:
        return events
    for child in list(events_el):
        tag = local_tag(child.tag)
        if tag not in _EVENT_KINDS:
            child_name = child.findtext(_SHORT_NAME) or "<unnamed>"
            skip_log.record(
                path=f"{behavior_path}/{child_name}",
                tag=tag,
                reason="unrecognized event kind inside EVENTS",
                category=SkipCategory.NOT_SUPPORTED,
                effect="abort",
            )
            continue
        name = child.findtext(_SHORT_NAME) or ""
        _report_uncarried_description(
            _extract_description(child), f"{behavior_path}/{name}", _EVENT_DESC_REASON, skip_log
        )
        period_text = child.findtext(_PERIOD)
        period = parse_float_literal(period_text)
        context_port_ref = target_operation_ref = None
        if tag == "OPERATION-INVOKED-EVENT":
            # The event's OWN OPERATION-IREF child, never a descendant's.
            iref = child.find(_OPERATION_IREF)
            if iref is not None:
                context_port_ref = iref.findtext(_CONTEXT_P_PORT_REF)
                target_operation_ref = iref.findtext(_TARGET_PROVIDED_OPERATION_REF)
        if tag in _CLOSED_EVENT_KINDS:
            # The event's closed table, and each disabled mode's, keyed at `{event}/DISABLED-MODE-IREFS`.
            _report_closed_table(child, f"{behavior_path}/{name}", tag, skip_log)
        data_iref = _DataIref()
        if tag == "DATA-RECEIVED-EVENT":
            # The event's own VARIATION-POINT is one loss line at the event; then its DATA-IREF.
            _report_access_variation_point(child, f"{behavior_path}/{name}", tag, skip_log)
            data_iref = _read_data_iref(child, f"{behavior_path}/{name}", skip_log)
        # A returns event's own EVENT-SOURCE-REF, the result point it fires for.
        event_source = child.find(_EVENT_SOURCE_REF) if tag == "ASYNCHRONOUS-SERVER-CALL-RETURNS-EVENT" else None
        disabled_modes: list[DisabledModeInfo] = []
        disabled_el = child.find(_DISABLED_MODE_IREFS)
        if disabled_el is not None:
            for iref in disabled_el.findall(_DISABLED_MODE_IREF):
                if tag in _CLOSED_EVENT_KINDS:
                    _report_closed_table(
                        iref,
                        f"{behavior_path}/{name}/DISABLED-MODE-IREFS",
                        "R-MODE-IN-ATOMIC-SWC-INSTANCE-REF",
                        skip_log,
                    )
                disabled_modes.append(
                    DisabledModeInfo(
                        context_port_ref=iref.findtext(_CONTEXT_PORT_REF),
                        mode_group_ref=iref.findtext(_CONTEXT_MODE_DECLARATION_GROUP_PROTOTYPE_REF),
                        mode_ref=iref.findtext(_TARGET_MODE_DECLARATION_REF),
                    )
                )
        events.append(
            SwcEventInfo(
                path=f"{behavior_path}/{name}",
                name=name,
                kind=tag,
                runnable_ref=child.findtext(_START_ON_EVENT_REF),
                period=period,
                context_port_ref=context_port_ref,
                target_operation_ref=target_operation_ref,
                disabled_modes=disabled_modes,
                unreadable_period=period_text if period_text is not None and period is None else None,
                context_r_port_ref=data_iref.context_r_port_ref,
                context_r_port_dest=data_iref.context_r_port_dest,
                target_data_element_ref=data_iref.target_data_element_ref,
                event_source_ref=event_source.text if event_source is not None else None,
                event_source_dest=event_source.get("DEST") if event_source is not None else None,
            )
        )
    return events


def _read_data_type_mapping_refs(behavior: ET.Element | None) -> list[str]:
    """Every ``DATA-TYPE-MAPPING-REF`` of one SWC-INTERNAL-BEHAVIOR, in source order.

    A blank or whitespace-only reference names no set and is dropped here.

    What it does **not** read: ``MODE-REQUEST-TYPE-MAP``s, which the model does not hold, and a mapping reference
    on any element other than the component's own behaviour (no other indexed element carries the container).
    """
    if behavior is None:
        return []
    refs_el = behavior.find(_DATA_TYPE_MAPPING_REFS)
    if refs_el is None:
        return []
    return [text.strip() for ref in refs_el.findall(_DATA_TYPE_MAPPING_REF) if (text := ref.text or "").strip()]


def _read_handle_termination_and_restart(
    behavior: ET.Element | None, behavior_path: str, skip_log: SkipLog
) -> str | None:
    """The behaviour's ``HANDLE-TERMINATION-AND-RESTART`` literal, or ``None``.

    Absent is ``None`` and no entry: the element is ``minOccurs="0"`` in both schemas, and an absent value is the
    source saying nothing, never ``NO-SUPPORT``. A literal outside the schema's three is one skip entry naming the
    tag and the value, and ``None``, so nothing is carried that the writer could not emit back.
    """
    if behavior is None:
        return None
    node = behavior.find(_HANDLE_TERMINATION_AND_RESTART)
    if node is None:
        return None
    literal = (node.text or "").strip(_NUMBER_PADDING)
    if literal in HANDLE_TERMINATION_AND_RESTART_LITERALS.values():
        return literal
    skip_log.record(
        path=behavior_path,
        tag="HANDLE-TERMINATION-AND-RESTART",
        reason=(
            f"unrecognized HANDLE-TERMINATION-AND-RESTART value '{literal}' "
            f"(expected one of {', '.join(HANDLE_TERMINATION_AND_RESTART_LITERALS.values())})"
        ),
        category=SkipCategory.NOT_SUPPORTED,
        effect="abort",
    )
    return None


def _in_autosar_namespace(el: ET.Element) -> bool:
    return isinstance(el.tag, str) and el.tag.startswith(f"{{{AR_NS}}}")


def _holds_content(el: ET.Element) -> bool:
    """Whether *el* holds an element, non-whitespace text or an attribute (an empty container does not)."""
    return bool(len(el) or (el.text or "").strip() or el.attrib)


def _first_claimants(parent: ET.Element) -> set[int]:
    """The ``id``s of the named elements whose nearest named ancestor is *parent* and that the index keeps.

    Re-applies :func:`_index_named_element`'s decision without the index: containers without a ``SHORT-NAME`` are
    transparent, the first element to claim a name keeps it, and a later one -- which the index dropped and
    reported as a duplicate path -- is not a claimant, so it is not reported a second time. An element outside the
    AUTOSAR namespace is the index's own entry and is not walked.
    """
    kept: set[int] = set()
    claimed: set[str] = set()

    def walk(el: ET.Element) -> None:
        for child in list(el):
            if not _in_autosar_namespace(child) or local_tag(child.tag) == "SHORT-NAME":
                continue
            name = child.findtext(_SHORT_NAME)
            if name is None:
                walk(child)
            elif name not in claimed:
                claimed.add(name)
                kept.add(id(child))

    walk(parent)
    return kept


def _unread_reason(tag: str, parent_tag: str) -> str:
    return f"{tag} of a {parent_tag} is not read by SWCraft -- no SWCraft representation, not imported or preserved"


def _record_item(item: ET.Element, parent_path: str, kept: set[int], reason: str, skip_log: SkipLog) -> None:
    """One skip entry for *item*: at its path when named, at *parent_path* when a reference.

    A named item that is not a first claimant is the index's duplicate-path line and gets none here. Every caller
    reports content SWCraft does not read, so the entry is :attr:`SkipCategory.NOT_SUPPORTED`.
    """
    name = item.findtext(_SHORT_NAME)
    if name is not None and id(item) not in kept:
        return
    skip_log.record(
        path=parent_path if name is None else f"{parent_path}/{name}",
        tag=local_tag(item.tag),
        reason=reason,
        category=SkipCategory.NOT_SUPPORTED,
        effect="abort",
    )


def _report_unread_child(
    child: ET.Element, parent_path: str, parent_tag: str, kept: set[int], skip_log: SkipLog
) -> None:
    """One entry per item of a child the closed table does not name.

    A container gives one entry per element it holds, tagged with the item's own tag, at the item's path when
    named and at *parent_path* when a reference; a scalar child with text or an attribute gives one entry of its
    own tag. An empty child gives none.
    """
    tag = local_tag(child.tag)
    items = [item for item in list(child) if _in_autosar_namespace(item)]
    if items:
        for item in items:
            _record_item(item, parent_path, kept, _unread_reason(tag, parent_tag), skip_log)
    elif len(child) == 0 and _holds_content(child):
        skip_log.record(
            path=parent_path,
            tag=tag,
            reason=_unread_reason(tag, parent_tag),
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )


def _report_documentation_child(child: ET.Element, parent_path: str, parent_tag: str, skip_log: SkipLog) -> None:
    """One entry for a documentation child of the closed tables' *reported* group.

    ``LONG-NAME``, ``CATEGORY``, ``INTRODUCTION``, ``ANNOTATIONS`` and a behaviour's own ``DESC``: the model holds
    none of them for a behaviour or a runnable. Unlike :func:`_report_unread_child` the child is one entry
    whatever the number of ``L-2`` / ``L-4`` items it holds, at *parent_path*, and none when it holds no text
    (:func:`_holds_text`). It aborts ``import component``.
    """
    if _holds_text(child):
        tag = local_tag(child.tag)
        skip_log.record(
            path=parent_path,
            tag=tag,
            reason=f"{tag} of a {parent_tag} is documentation SWCraft does not carry -- not imported or preserved",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )


def _report_unselected_children(container: ET.Element, parent_path: str, kept: set[int], skip_log: SkipLog) -> None:
    """Report every child of a selecting container that is not its one tag."""
    container_tag = local_tag(container.tag)
    expected = _SELECTED_CHILD[container_tag]
    for item in list(container):
        if not _in_autosar_namespace(item) or local_tag(item.tag) == expected:
            continue
        reason = f"unrecognized child inside {container_tag} (only {expected} is read)"
        _record_item(item, parent_path, kept, reason, skip_log)


def _read_port_api_options(
    behavior: ET.Element | None, behavior_path: str, swc: ET.Element, swc_path: str
) -> list[PortApiOptionInfo]:
    """Every ``PORT-API-OPTION`` of *behavior*, in document order, as raw facts.

    The reader judges nothing about an option: whether it states what no option states -- a kept port, the first
    option for it, exactly three children, no attribute but the reference's ``DEST``, both flags false -- is the
    mapper's decision, because whether a port is kept is decided there. The ports an option's ``PORT-REF`` names
    are read off the component's own ``PORTS`` (``{component}/{SHORT-NAME}``, every kind included) from the XML
    rather than from ``SwcPortInfo``, which holds only the port kinds of ``_PORT_KINDS``. A foreign child of
    ``PORT-API-OPTIONS`` is the closed table's line (:func:`_report_unselected_children`), never an option --
    except one whose local name is ``PORT-API-OPTION``: it is read as an option of which nothing is read, every
    child keeping its qualified tag, so the mapper names it at the shared key.
    """
    if behavior is None:
        return []
    kinds_by_path: dict[str, list[str]] = {}
    ports_el = swc.find(_PORTS)
    for port in list(ports_el) if ports_el is not None else []:
        if _in_autosar_namespace(port):
            path = f"{swc_path}/{port.findtext(_SHORT_NAME) or ''}"
            kinds_by_path.setdefault(path, []).append(local_tag(port.tag))
    options: list[PortApiOptionInfo] = []
    for container in behavior.findall(_PORT_API_OPTIONS):
        for option in container:
            if not isinstance(option.tag, str) or local_tag(option.tag) != "PORT-API-OPTION":
                continue
            read_children = _in_autosar_namespace(option)
            children = [child for child in list(option) if isinstance(child.tag, str)]
            # A child outside the AUTOSAR namespace, or any child of an option outside it, keeps its namespace
            # (``{namespace}LOCAL``), as the index spells it: it is never a PORT-REF or a flag.
            tags = tuple(
                local_tag(child.tag) if read_children and _in_autosar_namespace(child) else child.tag
                for child in children
            )
            reference: ET.Element | None = None
            attributes = [("PORT-API-OPTION", local_tag(name)) for name in option.attrib]
            flags: dict[str, list[str]] = {"ENABLE-TAKE-ADDRESS": [], "INDIRECT-API": []}
            for child, tag in zip(children, tags, strict=True):
                if tag in flags:
                    flags[tag].append(child.text or "")
                    attributes.extend((tag, local_tag(name)) for name in child.attrib)
                elif tag == "PORT-REF":
                    if reference is None:
                        reference = child
                    attributes.extend((tag, local_tag(name)) for name in child.attrib if name != "DEST")
            port_ref = None if reference is None else (reference.text or "")
            options.append(
                PortApiOptionInfo(
                    container_path=f"{behavior_path}/PORT-API-OPTIONS",
                    port_ref=port_ref,
                    port_ref_dest=None if reference is None else reference.get("DEST"),
                    referenced_kinds=tuple(kinds_by_path.get(port_ref, ())) if port_ref is not None else (),
                    child_tags=tags,
                    stated_attributes=tuple(attributes),
                    enable_take_address=tuple(flags["ENABLE-TAKE-ADDRESS"]),
                    indirect_api=tuple(flags["INDIRECT-API"]),
                )
            )
    return options


def _report_unread_behaviour(
    behavior: ET.Element | None,
    behavior_path: str,
    claimants: set[int],
    skip_log: SkipLog,
) -> None:
    """Report what the closed table says a SWC-INTERNAL-BEHAVIOR's read does not carry.

    *claimants* is :func:`_first_claimants` of the behaviour, shared with :func:`_read_swc_runnables`, which
    reports the runnables' own children. **Not reached**: below a read item -- inside an event or a mapping
    reference -- what the reader does not select is still unreported. The documentation group is one entry per
    child with text (:func:`_report_documentation_child`).
    """
    if behavior is None:
        return
    for child in list(behavior):
        if not _in_autosar_namespace(child):
            continue
        tag = local_tag(child.tag)
        if tag not in _BEHAVIOUR_CHILDREN:
            _report_unread_child(child, behavior_path, "SWC-INTERNAL-BEHAVIOR", claimants, skip_log)
        elif tag in _REPORTED_DOCUMENTATION_TAGS or tag == "DESC":
            _report_documentation_child(child, behavior_path, "SWC-INTERNAL-BEHAVIOR", skip_log)
        elif tag in _SELECTED_CHILD:
            _report_unselected_children(child, behavior_path, claimants, skip_log)
    instantiation = behavior.findtext(_SUPPORTS_MULTIPLE_INSTANTIATION)
    if parse_xsd_boolean(instantiation) is not False:
        state = "is absent" if instantiation is None else "is not false"
        skip_log.record(
            path=behavior_path,
            tag="SUPPORTS-MULTIPLE-INSTANTIATION",
            reason=f"SUPPORTS-MULTIPLE-INSTANTIATION {state} -- the writer emits false",
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )


def _report_further_behaviours(swc: ET.Element, swc_path: str, skip_log: SkipLog) -> None:
    """One entry per SWC-INTERNAL-BEHAVIOR after the first (:func:`_find_internal_behavior` reads one).

    The container's choice is unbounded in both schemas, so a component may carry several; SWCraft holds nothing of
    the others. A named one the index dropped as a duplicate path is the index's line.
    """
    behaviors = swc.find(_INTERNAL_BEHAVIORS)
    if behaviors is None:
        return
    further = behaviors.findall(_SWC_INTERNAL_BEHAVIOR)[1:]
    if not further:
        return
    claimants = _first_claimants(swc)
    for behavior in further:
        _record_item(
            behavior,
            swc_path,
            claimants,
            "a further SWC-INTERNAL-BEHAVIOR of one component is not read by SWCraft -- only the first is read; "
            "not imported or preserved",
            skip_log,
        )


def _report_unread_runnable(r_el: ET.Element, runnable_path: str, claimants: set[int], skip_log: SkipLog) -> None:
    """Report what the closed table says a RUNNABLE-ENTITY's read does not carry.

    The access, local-variable and exclusive area reference containers keep their own unrecognized-kind entries;
    the documentation group is one entry per child with text (:func:`_report_documentation_child`), and the
    runnable's ``DESC`` is read into its description. **Not reached**: inside an access point, what the reader does
    not select. ``SYMBOL`` is read as written; only an absent one is a line here, since the attribute has
    multiplicity 1 and the writer then states the runnable's ``SHORT-NAME``.
    """
    for child in list(r_el):
        if not _in_autosar_namespace(child):
            continue
        tag = local_tag(child.tag)
        if tag not in _RUNNABLE_CHILDREN:
            _report_unread_child(child, runnable_path, "RUNNABLE-ENTITY", claimants, skip_log)
        elif tag in _REPORTED_DOCUMENTATION_TAGS:
            _report_documentation_child(child, runnable_path, "RUNNABLE-ENTITY", skip_log)
    if r_el.find(_SYMBOL) is not None:
        return
    skip_log.record(
        path=runnable_path,
        tag="SYMBOL",
        reason="SYMBOL is absent -- the writer emits the runnable's SHORT-NAME as its SYMBOL",
        category=SkipCategory.NOT_SUPPORTED,
        effect="abort",
    )


def _read_swc(entry: RawElement, skip_log: SkipLog) -> SwcInfo:
    node = entry.node
    behavior = _find_internal_behavior(node)
    behavior_path = _behavior_path(entry.path, behavior)
    ports = _read_swc_ports(node, entry.path, skip_log)
    # One claimant set per behaviour, shared by the closed table and the runnables.
    claimants = _first_claimants(behavior) if behavior is not None else set()
    _report_unread_behaviour(behavior, behavior_path, claimants, skip_log)
    _report_further_behaviours(node, entry.path, skip_log)
    return SwcInfo(
        path=entry.path,
        name=node.findtext(_SHORT_NAME) or "",
        description=entry.description,
        ports=ports,
        runnables=_read_swc_runnables(behavior, behavior_path, claimants, skip_log),
        events=_read_swc_events(behavior, behavior_path, skip_log),
        swcraft_markers=_read_swcraft_markers(behavior),
        data_type_mapping_refs=_read_data_type_mapping_refs(behavior),
        handle_termination_and_restart=_read_handle_termination_and_restart(behavior, behavior_path, skip_log),
        behavior_name=behavior.findtext(_SHORT_NAME) if behavior is not None else None,
        port_api_options=_read_port_api_options(behavior, behavior_path, node, entry.path),
        variables=_read_swc_variables(behavior, behavior_path, claimants, skip_log),
        exclusive_areas=_read_swc_exclusive_areas(behavior, behavior_path, claimants, skip_log),
    )


def _read_component_prototypes(node: ET.Element, comp_path: str) -> list[ComponentPrototypeInfo]:
    """Read every SW-COMPONENT-PROTOTYPE directly inside `node`'s COMPONENTS block, one segment under `comp_path`."""
    prototypes: list[ComponentPrototypeInfo] = []
    components_el = node.find(_COMPONENTS)
    if components_el is None:
        return prototypes
    for child in components_el.findall(_SW_COMPONENT_PROTOTYPE):
        name = child.findtext(_SHORT_NAME) or ""
        type_tref = child.find(_TYPE_TREF)
        prototypes.append(
            ComponentPrototypeInfo(
                path=f"{comp_path}/{name}",
                name=name,
                type_ref=(type_tref.text or "") if type_tref is not None else "",
                # The attribute verbatim, never inferred from the path: an absent type resolves against nothing,
                # and the importer writes this string back out as the stub's `external_dest`.
                dest=(type_tref.get("DEST") or "") if type_tref is not None else "",
                description=_extract_description(child),
            )
        )
    return prototypes


def _read_connector_port_ref(iref_el: ET.Element | None, target_tag: str, stated_direction: str) -> ConnectorPortRef:
    """Read one CONTEXT-COMPONENT-REF + target-port-ref pair directly out of `iref_el`.

    PROVIDER-IREF and REQUESTER-IREF carry the pair unwrapped, unlike INNER-PORT-IREF's
    P-/R-PORT-IN-COMPOSITION-INSTANCE-REF wrapper. The target reference's ``DEST`` is kept verbatim beside the
    direction the element reading it states (`stated_direction`)."""
    if iref_el is None:
        return ConnectorPortRef(component_ref=None, port_ref=None)
    target = iref_el.find(target_tag)
    return ConnectorPortRef(
        component_ref=iref_el.findtext(_CONTEXT_COMPONENT_REF),
        port_ref=iref_el.findtext(target_tag),
        dest=target.get("DEST") if target is not None else None,
        stated_direction=stated_direction,
    )


def _read_assembly_connector(child: ET.Element, comp_path: str) -> AssemblyConnectorInfo:
    name = child.findtext(_SHORT_NAME) or ""
    provider = _read_connector_port_ref(child.find(_PROVIDER_IREF), _TARGET_P_PORT_REF, "provide")
    requester = _read_connector_port_ref(child.find(_REQUESTER_IREF), _TARGET_R_PORT_REF, "require")
    return AssemblyConnectorInfo(
        path=f"{comp_path}/{name}",
        name=name,
        provider=provider,
        requester=requester,
        description=_extract_description(child),
    )


def _read_delegation_connector(child: ET.Element, comp_path: str) -> DelegationConnectorInfo:
    name = child.findtext(_SHORT_NAME) or ""
    inner = ConnectorPortRef(component_ref=None, port_ref=None)
    inner_iref_el = child.find(_INNER_PORT_IREF)
    if inner_iref_el is not None:
        # INNER-PORT-IREF wraps its content in a P- or R- variant; the wrapper tag is the only signal of the
        # delegated port's direction here.
        p_wrapper = inner_iref_el.find(_P_PORT_IN_COMPOSITION_INSTANCE_REF)
        if p_wrapper is not None:
            inner = _read_connector_port_ref(p_wrapper, _TARGET_P_PORT_REF, "provide")
        else:
            r_wrapper = inner_iref_el.find(_R_PORT_IN_COMPOSITION_INSTANCE_REF)
            if r_wrapper is not None:
                inner = _read_connector_port_ref(r_wrapper, _TARGET_R_PORT_REF, "require")
    return DelegationConnectorInfo(
        path=f"{comp_path}/{name}",
        name=name,
        inner=inner,
        outer_port_ref=child.findtext(_OUTER_PORT_REF),
        description=_extract_description(child),
    )


def _read_composition(entry: RawElement, skip_log: SkipLog) -> CompositionInfo:
    node = entry.node
    assembly_connectors: list[AssemblyConnectorInfo] = []
    delegation_connectors: list[DelegationConnectorInfo] = []
    connectors_el = node.find(_CONNECTORS)
    if connectors_el is not None:
        for child in list(connectors_el):
            tag = local_tag(child.tag)
            if tag not in _CONNECTOR_KINDS:
                # Any other connector kind (e.g. PASS-THROUGH-SW-CONNECTOR) has no SWCraft model, and its presence
                # is still skipped and reported, never dropped in silence.
                child_name = child.findtext(_SHORT_NAME) or "<unnamed>"
                skip_log.record(
                    path=f"{entry.path}/{child_name}",
                    tag=tag,
                    reason="unrecognized connector kind inside CONNECTORS",
                    category=SkipCategory.NOT_SUPPORTED,
                    effect="abort",
                )
                continue
            if tag == "ASSEMBLY-SW-CONNECTOR":
                assembly_connectors.append(_read_assembly_connector(child, entry.path))
            else:
                delegation_connectors.append(_read_delegation_connector(child, entry.path))
    return CompositionInfo(
        path=entry.path,
        name=node.findtext(_SHORT_NAME) or "",
        description=entry.description,
        ports=_read_swc_ports(node, entry.path, skip_log),
        prototypes=_read_component_prototypes(node, entry.path),
        assembly_connectors=assembly_connectors,
        delegation_connectors=delegation_connectors,
        long_name=_extract_long_name(node, entry.path, skip_log),
    )


# ---------------------------------------------------------------------------
# Reading one SWC-TIMING (the execution-time budgets)
# ---------------------------------------------------------------------------

_TIMING_GUARANTEES = "TIMING-GUARANTEES"
_TIMING_REQUIREMENTS = "TIMING-REQUIREMENTS"
_TIMING_ROLES: dict[str, Literal["requirement", "guarantee"]] = {
    _TIMING_GUARANTEES: "guarantee",
    _TIMING_REQUIREMENTS: "requirement",
}
EXECUTION_TIME_CONSTRAINT = "EXECUTION-TIME-CONSTRAINT"
_TIMING_RESOURCE = "TIMING-RESOURCE"
_BEHAVIOR_REF = _ar("BEHAVIOR-REF")
_COMPONENT_REF = _ar("COMPONENT-REF")
_EXECUTABLE_REF = _ar("EXECUTABLE-REF")
_EXECUTION_TIME_TYPE = _ar("EXECUTION-TIME-TYPE")
_MAXIMUM = _ar("MAXIMUM")
_MINIMUM = _ar("MINIMUM")
_CSE_CODE = _ar("CSE-CODE")
_CSE_CODE_FACTOR = _ar("CSE-CODE-FACTOR")
_TRACE_REFS = _ar("TRACE-REFS")
_VARIATION_POINT = _ar("VARIATION-POINT")
#: The members of an execution-time constraint that scope its maximum, in the order a skip names the first one
#: present.
_SCOPING_MEMBERS = ("TIMING-CONDITION-REF", "COMPONENT-IREF", "VARIATION-POINT")

#: ``xsd:unsignedInt``'s lexical space (XML Schema Part 2, 3.3.22.1, by
#: ``integer``'s rule, 3.3.13.1): an optional sign and decimal digits, leading zeros
#: allowed. Both licensed schemas type ``CSE-CODE`` so (``CSE-CODE-TYPE-STRING--SIMPLE``,
#: XSD R4.2.2:69881, R4.3.0:80472), with the ``collapse`` white-space facet.
_UNSIGNED_INT_SPELLING = re.compile(r"(?P<sign>[+-]?)(?P<digits>[0-9]+)")
#: ``xsd:unsignedInt``'s ``maxInclusive``.
_UNSIGNED_INT_MAX = 4294967295
#: The digits ``_UNSIGNED_INT_MAX`` has: a longer run (leading zeros stripped) is
#: above it, decided before any conversion.
_UNSIGNED_INT_DIGITS = len(str(_UNSIGNED_INT_MAX))


def _read_cse_code(text: str | None) -> int | None:
    """A ``CSE-CODE`` text as the ``xsd:unsignedInt`` it spells, or ``None``.

    Not one of the grammar-taking number helpers (``NumberGrammar``): the field is no ``AR:INTEGER`` but an XML
    Schema built-in type with its own lexical space -- a sign, **``+`` or ``-``**, then decimal digits with leading
    zeros allowed, XML white space collapsed at both ends. So ``026`` is twenty-six here, where ``AR:INTEGER`` reads
    an octal, and ``-0`` and ``-000`` are zero (code ``0``, microseconds); any other negative value, and a value
    above 4294967295, is ``None``. The digit run is measured before it is converted, so an over-long text never
    reaches ``int()``'s digit limit.
    """
    if text is None:
        return None
    match = _UNSIGNED_INT_SPELLING.fullmatch(text.strip(_NUMBER_PADDING))
    if match is None:
        return None
    digits = match["digits"].lstrip("0") or "0"
    if len(digits) > _UNSIGNED_INT_DIGITS:
        return None
    value = int(digits)
    if (match["sign"] == "-" and value != 0) or value > _UNSIGNED_INT_MAX:
        return None
    return value


def _read_multidimensional_time(node: ET.Element) -> MultidimensionalTimeInfo:
    """A ``MAXIMUM`` / ``MINIMUM``'s two members (:class:`MultidimensionalTimeInfo`).

    The factor is an ``AR:INTEGER`` (XSD R4.3.0:84186), read by that grammar; the
    code by :func:`_read_cse_code`.
    """
    code_text = node.findtext(_CSE_CODE)
    factor_text = node.findtext(_CSE_CODE_FACTOR)
    factor = parse_int_literal(factor_text, grammar="integer")
    stripped = factor_text.strip(_NUMBER_PADDING) if factor_text is not None else ""
    spelling = _INTEGER_SPELLING["integer"].fullmatch(stripped)
    overflow: Literal["positive", "negative"] | None = None
    radix_source: str | None = None
    if spelling is not None and factor is None:
        overflow = "negative" if stripped.startswith("-") else "positive"
    elif spelling is not None and _integer_digits(spelling)[2] != 10:
        radix_source = stripped
    return MultidimensionalTimeInfo(
        cse_code=_read_cse_code(code_text),
        code_text=code_text,
        cse_factor=factor,
        factor_text=factor_text,
        factor_overflow=overflow,
        factor_radix_source=radix_source,
    )


def _report_timing_loss(path: str, tag: str, reason: str, skip_log: SkipLog) -> None:
    """One descriptive entry for timing content SWCraft does not carry.

    A timing description, condition or resource, and an ``SWC-TIMING``'s own ``DESC`` or ``CATEGORY``: each means
    something only through a constraint, or documents one, so losing it changes no budget.
    """
    skip_log.record(path, tag, reason, category=SkipCategory.NOT_SUPPORTED, effect=REPORT)


#: The reasons of the traceable children of a budget.
_BUDGET_DESC_REASON = "DESC not carried: the model holds no text for an execution-time budget"
_BUDGET_TRACE_REASON = "TRACE-REFS not carried: the model holds no trace for an execution-time budget"


def _budget_category_reason(category: str) -> str:
    return f"CATEGORY {category} not carried: the model holds no category for an execution-time budget"


def _read_timing_constraint(
    el: ET.Element, parent_path: str, role: Literal["requirement", "guarantee"], skip_log: SkipLog
) -> TimingConstraintInfo:
    """One constraint of an ``SWC-TIMING`` (:class:`TimingConstraintInfo`).

    A constraint with no ``SHORT-NAME`` is keyed at *parent_path*, the ``SWC-TIMING``'s, as the index keys unnamed
    content. Only an ``EXECUTION-TIME-CONSTRAINT`` under ``TIMING-REQUIREMENTS`` can become a budget, so only its
    closed table's ``unread`` children are reported here; the mapper skips a guarantee and every other kind whole.
    """
    kind = local_tag(el.tag)
    name = el.findtext(_SHORT_NAME) or ""
    path = f"{parent_path}/{name}" if name.strip() else parent_path
    if kind != EXECUTION_TIME_CONSTRAINT:
        return TimingConstraintInfo(path=path, name=name, kind=kind, role=role)
    executable = el.find(_EXECUTABLE_REF)
    maximum = el.find(_MAXIMUM)
    minimum = el.find(_MINIMUM)
    uncarried: list[tuple[str, str]] = []
    if _extract_description(el):
        uncarried.append(("DESC", _BUDGET_DESC_REASON))
    category = _declared_text(el, _CATEGORY)
    if category:
        uncarried.append(("CATEGORY", _budget_category_reason(category)))
    trace = el.find(_TRACE_REFS)
    if trace is not None and _holds_content(trace):
        uncarried.append(("TRACE-REFS", _BUDGET_TRACE_REASON))
    if role == "requirement":
        _report_closed_table(el, path, kind, skip_log)
    return TimingConstraintInfo(
        path=path,
        name=name,
        kind=kind,
        role=role,
        executable_ref=executable.text if executable is not None else None,
        executable_dest=executable.get("DEST") if executable is not None else None,
        execution_time_type=_declared_text(el, _EXECUTION_TIME_TYPE),
        maximum=_read_multidimensional_time(maximum) if maximum is not None else None,
        minimum=_read_multidimensional_time(minimum) if minimum is not None else None,
        scoping=tuple(member for member in _SCOPING_MEMBERS if el.find(_ar(member)) is not None),
        uncarried=tuple(uncarried),
    )


#: The reasons for timing content, by the container (or the element) an entry sits in.
_TIMING_CONTENT_REASONS = {
    "TIMING-DESCRIPTIONS": (
        "timing description not carried: SWCraft carries no timing description -- it means something only "
        "through a constraint SWCraft does not carry"
    ),
    "TIMING-CONDITIONS": (
        "timing condition not carried: SWCraft carries no timing condition, and a constraint naming one is skipped"
    ),
    _TIMING_RESOURCE: "timing resource not carried: SWCraft carries no timing resource",
}


def _read_swc_timing(entry: RawElement, skip_log: SkipLog) -> SwcTimingInfo:
    """One ``SWC-TIMING`` (:class:`SwcTimingInfo`), and the lines of what it holds that no budget is.

    Every timing description and condition and the resource is one descriptive entry, at the entry's own path when
    named; the element's own ``DESC`` and ``CATEGORY`` are one each. Its closed table's ``unread`` children are
    reported as every closed table's are. Nothing here decides a budget: the constraints are read whole for
    ``swc_mapping``.
    """
    node = entry.node
    path = entry.path
    constraints: list[TimingConstraintInfo] = []
    for child in list(node):
        if not _in_autosar_namespace(child):
            continue
        tag = local_tag(child.tag)
        if tag in _TIMING_ROLES:
            constraints.extend(
                _read_timing_constraint(item, path, _TIMING_ROLES[tag], skip_log)
                for item in list(child)
                if _in_autosar_namespace(item)
            )
        elif tag in _TIMING_CONTENT_REASONS:
            items = (
                [child] if tag == _TIMING_RESOURCE else [item for item in list(child) if _in_autosar_namespace(item)]
            )
            for item in items:
                name = item.findtext(_SHORT_NAME) or ""
                _report_timing_loss(
                    f"{path}/{name}" if name.strip() else path,
                    local_tag(item.tag),
                    _TIMING_CONTENT_REASONS[tag],
                    skip_log,
                )
    if entry.description:
        _report_timing_loss(path, "DESC", _BUDGET_DESC_REASON, skip_log)
    category = _declared_text(node, _CATEGORY)
    if category:
        _report_timing_loss(path, "CATEGORY", _budget_category_reason(category), skip_log)
    _report_closed_table(node, path, "SWC-TIMING", skip_log)
    return SwcTimingInfo(
        path=path,
        name=node.findtext(_SHORT_NAME) or "",
        behavior_ref=node.findtext(_BEHAVIOR_REF),
        component_ref=node.findtext(_COMPONENT_REF),
        variation_point=node.find(_VARIATION_POINT) is not None,
        constraints=constraints,
    )


# ---------------------------------------------------------------------------
# Reading one CONSTANT-SPECIFICATION's value
# ---------------------------------------------------------------------------

_VALUE_SPEC = _ar("VALUE-SPEC")
_SW_VALUE_CONT = _ar("SW-VALUE-CONT")
_SW_VALUES_PHYS = _ar("SW-VALUES-PHYS")
_V = _ar("V")
_VALUE = _ar("VALUE")
_SHORT_LABEL = _ar("SHORT-LABEL")
_FIELDS = _ar("FIELDS")


#: Why an empty or whitespace-only leaf is not read -- a number's or a text's.
_NO_VALUE_TEXT = "a value specification carries no value text"


def _read_constant_scalar(text: str | None, *, grammar: NumberGrammar) -> tuple[ConstantScalar | None, str | None]:
    """One leaf value's text -> a number, or a reason it is not one.

    **Exactly, or not at all.** An integer spelling is the ``int`` of any width; a float spelling is the ``float``
    only where that float holds the text exactly, and is otherwise refused with a reason
    (:func:`_inexact_value_reason`), a spelling that underflows to zero included. One that overflows is refused.

    The text is read by its field's *grammar* (:data:`NumberGrammar`): a ``NUMERICAL-VALUE-SPECIFICATION``'s
    ``VALUE`` is a formula, an ``APPLICATION-VALUE-SPECIFICATION``'s ``V`` the pattern. The spellings are matched
    as anchored patterns, never handed to ``int()``/``float()`` directly, which accept spellings ARXML does not use
    (``int("1_000")``, ``float("nan")``) and would turn a reportable skip into an exception mid-import.

    Hexadecimal, octal and binary are read as their value and the radix is dropped: ``0xFF``, ``0377`` and
    ``255`` all arrive as the int 255, so a round trip compares values, never spellings. The raw spelling is kept on
    :attr:`ConstantScalar.radix_source` so the import report can say the normalization happened.

    **What this does not reach:** a value nested deeper than CPython's recursion limit.
    :func:`_read_value_specification` descends ARRAY/RECORD members with no depth bound of its own, so an export
    nesting a value specification some thousand levels deep raises ``RecursionError`` instead of skipping with a
    reason; no bound is set, because none was measured. The digit bounds below exist because CPython's digit limit
    is a stated constant; they reach only a value this function is given: a hand-written ``init:`` integer reaches
    ``schema.py`` without passing here, and nothing bounds it there.
    """
    # `written` is the text as written, kept for the radix line; `spelled` is what the grammar reads
    # (`stripped_spelling`: a formula's whitespace after its sign removed).
    written = (text or "").strip(_NUMBER_PADDING)
    if not written:
        return None, _NO_VALUE_TEXT
    spelled = stripped_spelling(written, grammar=grammar)
    integer = _INTEGER_SPELLING[grammar].fullmatch(spelled)
    if integer is not None:
        sign, digits, base, word = _integer_digits(integer)
        if base != 10:
            value = sign * int(digits, base)
            # The radix is dropped, so this value leaves as a decimal YAML scalar and `yaml.safe_dump` would raise
            # there, long after the importer has written files; the question is asked here, where the reason still
            # has a value specification to name.
            if not _writable_as_decimal(value):
                return None, (
                    f"value has {len(digits)} {word} digits, whose decimal form is above "
                    f"this interpreter's {sys.get_int_max_str_digits()}-digit limit for writing an integer"
                )
            return ConstantScalar(value=value, radix_source=written), None
        # `int()` of a decimal string above `sys.get_int_max_str_digits()` raises, and an exception here would
        # abort the whole import where one constant is to be skipped with a reason, so the literal is measured
        # before the conversion. The limit is asked for, not hard-coded: an embedder may have raised or disabled
        # it, and a limit of 0 means "no limit", not "no digits".
        limit = sys.get_int_max_str_digits()
        width = len(digits.lstrip("+-"))
        if limit and width > limit:
            return None, (
                f"value has {width} digits, above this interpreter's {limit}-digit limit for reading an integer"
            )
        return ConstantScalar(value=sign * int(digits)), None
    if _REAL_SPELLING[grammar].fullmatch(spelled):
        number = float(spelled)
        if not math.isfinite(number):
            return None, f"value '{written}' is not a finite number"
        # The bounds' rule (`parse_exact_number`): a float is the value only where it holds the text --
        # `Decimal(str(number))`, the number the writer prints for it, equals the text's exact decimal. Otherwise
        # (`16777217.000000001`, `0.10000000000000001`, `1e-400`, which underflows to zero) the value is named
        # and not carried, never rounded. `_stated_decimal`, never a bare `Decimal(spelled)`: `decimal` raises on
        # an exponent beyond its limits (`1e-99999999999999999999`).
        stated = _stated_decimal(spelled)
        if stated is None:
            return None, _unread_exponent_reason(written)
        if Decimal(str(number)) != stated:
            return None, _inexact_value_reason(written, number)
        return ConstantScalar(value=number), None
    return None, f"value '{written}' is not a number this reader can read"


def _inexact_value_reason(raw: str, nearest: float) -> str:
    """Why a finite float spelling is not carried: no IEEE double holds it exactly.

    SWCraft's own limit and not a defect of the export (``Numerical`` is a string with a pattern and states the
    number exactly), so the line is *not supported*, and behavioural because it is an init value.
    """
    return (
        f"value '{raw}' is a number SWCraft cannot hold exactly: it holds an IEEE 754 double, and the "
        f"nearest one is {nearest!r}, so the value is not carried rather than rounded"
    )


def _unread_exponent_reason(raw: str) -> str:
    """Why a float spelling with an exponent ``decimal`` does not construct is not carried.

    Not :func:`_inexact_value_reason`'s sentence, which states a fact this path cannot establish:
    ``0.0e99999999999999999999`` *is* zero, which a double holds exactly. What is true of every such text is that
    SWCraft cannot compare it with the double it would hold, so the value is not carried -- the same line, tag and
    category as the inexact one.
    """
    return (
        f"value '{raw}' has an exponent beyond the range SWCraft compares exactly, so it cannot show that "
        "an IEEE 754 double holds the value, and the value is not carried rather than rounded"
    )


def _with_label_and_form(
    read: tuple[ConstantScalar | None, str | None], node: ET.Element, form: ValueForm
) -> tuple[ConstantScalar | None, str | None]:
    """*read*'s scalar with *node*'s own ``SHORT-LABEL`` and the *form* it was read from.

    The one place a leaf's form is set: every leaf of a value specification passes through here."""
    scalar, reason = read
    if scalar is not None:
        scalar.label = node.findtext(_SHORT_LABEL)
        scalar.form = form
    return scalar, reason


def _read_text_scalar(text: str | None) -> tuple[ConstantScalar | None, str | None]:
    """One text leaf -- a ``VT`` or a text ``VALUE`` -- as written, or a reason.

    Both are ``VERBATIM-STRING``s in the schemas, so the text is kept exactly: it names an entry, and a padded text
    names a different one. A text with nothing but whitespace names none and is refused (:data:`_NO_VALUE_TEXT`).
    """
    if text is None or not text.strip():
        return None, _NO_VALUE_TEXT
    return ConstantScalar(value=text), None


def _read_application_value(node: ET.Element) -> tuple[ConstantScalar | None, str | None]:
    """One ``APPLICATION-VALUE-SPECIFICATION`` -> its single value, or a reason.

    ``CATEGORY``, when present, must be ``VALUE``, the category of a single value (``[constr_1006]``); an absent
    one is read, as both schemas admit it. ``SW-VALUES-PHYS`` must hold **exactly one child, a ``V`` or a ``VT``**:
    none, two, a ``V`` beside a ``VT``, a ``VF``, a ``VTF`` or a ``VG`` is refused with what it holds. The
    ``SW-VALUE-CONT``'s ``UNIT-REF`` is recorded as written.

    **What this does not reach:** ``SW-ARRAYSIZE``, ``SW-AXIS-CONTS`` and ``UNIT-DISPLAY-NAME`` are ignored with no
    line: a size of one is the scalar form, and a compound category -- the only one a larger size or an axis
    belongs to -- is refused by its ``CATEGORY``. An application value that states a larger size under ``VALUE`` or
    no category is read as its one value.
    """
    category = node.findtext(_CATEGORY)
    category = None if category is None else category.strip(_NUMBER_PADDING)
    if category is not None and category != "VALUE":
        return None, (
            f"APPLICATION-VALUE-SPECIFICATION of CATEGORY '{category}' is not read; only the single "
            "value of CATEGORY VALUE is"
        )
    cont = node.find(_SW_VALUE_CONT)
    phys = cont.find(_SW_VALUES_PHYS) if cont is not None else None
    entries = list(phys) if phys is not None else []
    if len(entries) != 1 or entries[0].tag not in (_V, _VT):
        held = f" ({', '.join(local_tag(entry.tag) for entry in entries)})" if entries else ""
        return None, (
            f"APPLICATION-VALUE-SPECIFICATION carries {len(entries)} SW-VALUES-PHYS entries{held}; "
            "only a single V or VT is read"
        )
    (entry,) = entries
    scalar, reason = _with_label_and_form(
        _read_text_scalar(entry.text) if entry.tag == _VT else _read_constant_scalar(entry.text, grammar="pattern"),
        node,
        ValueForm.APPLICATION,
    )
    if scalar is not None and cont is not None:
        scalar.unit_ref = cont.findtext(_UNIT_REF)
    return scalar, reason


def _read_value_specification(node: ET.Element) -> tuple[ConstantValue | None, str | None]:
    """One value-specification node -> a `ConstantValue`, or a reason.

    The value-specification kinds below are read, and no other tag is guessed at: an unread shape returns its own
    tag in the reason so the skip line names what was there. A TEXT-VALUE-SPECIFICATION's ``VALUE`` is read as a
    text, and an APPLICATION-VALUE-SPECIFICATION's one ``V`` or ``VT`` (:func:`_read_application_value`). Each
    leaf records the form it had.

    **What this does not reach:** an APPLICATION-VALUE-SPECIFICATION whose ``SW-VALUES-PHYS`` carries anything but
    exactly one ``V`` or ``VT`` (AUTOSAR allows several, for an array written out flat) is refused with what it
    holds: the multi-value form is unmeasured and reading it would be a guess. A rule-based value, a reference
    value and the other value specifications are refused by their tag.
    """
    tag = local_tag(node.tag)
    if tag == "APPLICATION-VALUE-SPECIFICATION":
        return _read_application_value(node)
    if tag == "NUMERICAL-VALUE-SPECIFICATION":
        value_el = node.find(_VALUE)
        if value_el is None:
            return None, "NUMERICAL-VALUE-SPECIFICATION carries no VALUE"
        return _with_label_and_form(_read_constant_scalar(value_el.text, grammar="formula"), node, ValueForm.NUMERICAL)
    if tag == "TEXT-VALUE-SPECIFICATION":
        value_el = node.find(_VALUE)
        if value_el is None:
            return None, "TEXT-VALUE-SPECIFICATION carries no VALUE"
        return _with_label_and_form(_read_text_scalar(value_el.text), node, ValueForm.TEXT)
    if tag == "ARRAY-VALUE-SPECIFICATION":
        members: list[ConstantValue] = []
        elements_el = node.find(_ELEMENTS)
        for child in list(elements_el) if elements_el is not None else []:
            member, reason = _read_value_specification(child)
            if member is None:
                return None, reason
            members.append(member)
        return ConstantArray(members=members, label=node.findtext(_SHORT_LABEL)), None
    if tag == "RECORD-VALUE-SPECIFICATION":
        fields: list[ConstantRecordField] = []
        fields_el = node.find(_FIELDS)
        for child in list(fields_el) if fields_el is not None else []:
            member, reason = _read_value_specification(child)
            if member is None:
                return None, reason
            fields.append(ConstantRecordField(label=child.findtext(_SHORT_LABEL), value=member))
        return ConstantRecord(fields=fields, label=node.findtext(_SHORT_LABEL)), None
    return None, f"{tag} is not a value specification this reader reads"


def _read_constant_specification(entry: RawElement, skip_log: SkipLog) -> ConstantSpecInfo:
    """One CONSTANT-SPECIFICATION -> its value, or the reason it was not read.

    Nothing is skip-logged here: an unread constant only matters where something references it, and that is where
    the reason is reported."""
    del skip_log  # uniform dispatch signature (see _TYPED_ELEMENT_READERS); unused here
    node = entry.node
    name = node.findtext(_SHORT_NAME) or ""
    spec_el = node.find(_VALUE_SPEC)
    children = list(spec_el) if spec_el is not None else []
    if len(children) != 1:
        return ConstantSpecInfo(
            path=entry.path,
            name=name,
            value=None,
            unreadable_reason=(
                f"CONSTANT-SPECIFICATION carries {len(children)} VALUE-SPEC children; expected exactly one"
            ),
        )
    value, reason = _read_value_specification(children[0])
    return ConstantSpecInfo(path=entry.path, name=name, value=value, unreadable_reason=reason)


def _read_init_value_ref(node: ET.Element) -> tuple[str | None, str | None]:
    """`node`'s own INIT-VALUE, as (constant path, reason it was not read).

    Both are None when there is no INIT-VALUE at all, the ordinary case. An INIT-VALUE that is there but is *not* a
    CONSTANT-REFERENCE (a value specification written in place, which this function does not resolve) yields the
    reason instead, so the caller reports it rather than dropping it silently.
    """
    init_el = node.find(_INIT_VALUE)
    if init_el is None:
        return None, None
    reference = init_el.find(_CONSTANT_REFERENCE)
    if reference is None:
        inner = [local_tag(child.tag) for child in list(init_el)]
        return None, (
            "INIT-VALUE is not a CONSTANT-REFERENCE (it carries " + (", ".join(inner) if inner else "nothing") + ")"
        )
    ref = reference.findtext(_CONSTANT_REF)
    if not ref:
        return None, "INIT-VALUE's CONSTANT-REFERENCE carries no CONSTANT-REF"
    return ref, None


_TYPED_ELEMENT_READERS = {
    "SW-BASE-TYPE": ("sw_base_types", _read_sw_base_type),
    "IMPLEMENTATION-DATA-TYPE": ("impl_data_types", _read_impl_data_type),
    "APPLICATION-PRIMITIVE-DATA-TYPE": ("app_primitive_data_types", _read_app_primitive_data_type),
    "APPLICATION-RECORD-DATA-TYPE": ("app_record_data_types", _read_app_record_data_type),
    "APPLICATION-ARRAY-DATA-TYPE": ("app_array_data_types", _read_app_array_data_type),
    "COMPU-METHOD": ("compu_methods", _read_compu_method),
    "UNIT": ("units", _read_unit),
    "DATA-CONSTR": ("data_constrs", _read_data_constr),
    "DATA-TYPE-MAPPING-SET": ("data_type_mapping_sets", _read_data_type_mapping_set),
    "SENDER-RECEIVER-INTERFACE": ("sr_interfaces", _read_sr_interface),
    "CLIENT-SERVER-INTERFACE": ("cs_interfaces", _read_cs_interface),
    "APPLICATION-SW-COMPONENT-TYPE": ("swcs", _read_swc),
    "COMPOSITION-SW-COMPONENT-TYPE": ("compositions", _read_composition),
    "CONSTANT-SPECIFICATION": ("constant_specs", _read_constant_specification),
    "SWC-TIMING": ("swc_timings", _read_swc_timing),
}


def _extract_typed_elements(model: ImportModel) -> None:
    """Post-pass over the path index: turn every recognized top-level element into its typed dataclass.

    Only entries whose *own* tag is a recognized top-level kind are extracted; nested descendants
    (APPLICATION-RECORD-ELEMENT, COMPU-SCALE, ...) are read from the owning element's node by the reader functions
    above, so they are never double-processed.
    """
    for entry in model.path_index.values():
        target = _TYPED_ELEMENT_READERS.get(entry.tag)
        if target is None:
            continue
        list_name, reader = target
        getattr(model, list_name).append(reader(entry, model.skip_log))
    _carry_value_type_ranges(model)


#: The spellings of an infinite limit, by the side each bounds nothing on.
_INFINITE_LIMIT_SPELLINGS: dict[str, frozenset[str]] = {
    "LOWER-LIMIT": frozenset({"-INF"}),
    "UPPER-LIMIT": frozenset({"INF", "+INF"}),
}
#: The other limit of a pair.
OTHER_LIMIT = {"LOWER-LIMIT": "UPPER-LIMIT", "UPPER-LIMIT": "LOWER-LIMIT"}


def is_infinite_limit(text: str, side: str) -> bool:
    """Whether *text* spells the infinity of *side* (``LOWER-LIMIT`` / ``UPPER-LIMIT``): any case, padding aside.

    ``-INF`` on a lower limit and ``INF`` / ``+INF`` on an upper one bound nothing, which is what an absent limit
    states and what the writer writes for an absent bound: such a limit is read as no bound and nothing is lost.
    Asked with the **other** side (:data:`OTHER_LIMIT`) it finds the spelling that is not that -- a lower limit of
    ``INF``, an upper one of ``-INF`` -- which admits no value at all and is not carried and named whatever its
    ``INTERVAL-TYPE``.

    The one judgement for both ranges the reader reads (a value type's ``INTERNAL-CONSTRS`` through
    :func:`_value_type_bound`, a ``PHYS-CONSTRS`` through :func:`_phys_limit_bounds_nothing`, both adding
    ``INFINITE``), the merge's limit signature and a scale's limits, so a spelling that bounds nothing to one of them
    bounds nothing to all.
    """
    return text.strip(_NUMBER_PADDING).upper() in _INFINITE_LIMIT_SPELLINGS[side]


def _value_type_bound(side: str, text: str | None, interval: str | None, lost: list[str]) -> int | float | None:
    """One ``INTERNAL-CONSTRS`` limit of a value type's range, or ``None``; *lost* gains what is not carried.

    ``INFINITE`` and the side's own infinite spelling (:func:`is_infinite_limit`) state no bound and read back as
    an absent one, with nothing lost; ``CLOSED`` and an absent ``INTERVAL-TYPE`` (``Limit.intervalType``'s
    default) read back as the bound. The other side's infinity, ``OPEN`` and any other interval on a finite limit, a
    text that is not a finite number and a finite number no model value holds exactly are not carried. The bound
    is carried **exactly** (:func:`parse_exact_number`).
    """
    if text is None:
        return None
    if is_infinite_limit(text, OTHER_LIMIT[side]):
        lost.append(f"a {side} spelling the other side's infinity")
        return None
    if interval == "INFINITE" or is_infinite_limit(text, side):
        return None
    if interval not in (None, "CLOSED"):
        lost.append(f"an INTERVAL-TYPE {interval} {side}")
        return None
    value, no_exact_value = parse_exact_number(text, grammar="formula")
    if no_exact_value:
        lost.append(f"a {side} no model value holds exactly")
    elif value is None:
        lost.append(f"a {side} that is not a finite number")
    return value


def _carry_value_type_ranges(model: ImportModel) -> None:
    """Read every ``VALUE`` implementation type's range, and report what is not carried.

    Runs once every element is read, because a range is two elements: the type's ``DATA-CONSTR-REF`` and the
    ``DATA-CONSTR`` it names. The bounds are the first rule's ``INTERNAL-CONSTRS`` limits (``[TPS_SWCT_01288]``:
    internal constraints are interpreted with respect to implementation data types). Family F17, one line per type
    keyed ``({type}/SW-DATA-DEF-PROPS, DATA-CONSTR-REF)``, names everything else: a constraint the file does not
    declare (``OUTSIDE_FILE``), a second rule, no ``INTERNAL-CONSTRS`` in the first rule, a ``PHYS-CONSTRS`` no
    application primitive of the file reads, any element beside the two limits, an ``OPEN`` limit, one spelling the
    other side's infinity, one that is not a finite number, one no model value holds exactly, and a lower bound
    above the upper one (then neither bound is carried).

    A constraint no ``VALUE`` implementation type reads keeps its ``INTERNAL-CONSTRS`` reported as unread content:
    an application type's range is its ``PHYS-CONSTRS``.

    What this does not reach: whether the application primitive that reads a shared constraint's
    ``PHYS-CONSTRS`` is itself imported -- the mapper's later skip of that type does not turn the value type's
    line on.
    """
    constr_by_path = {constr.path: constr for constr in model.data_constrs}
    application_refs = {app.data_constr_ref for app in model.app_primitive_data_types if app.data_constr_ref}
    value_refs: set[str] = set()
    for impl in model.impl_data_types:
        if impl.category != "VALUE" or not impl.data_constr_ref:
            continue
        value_refs.add(impl.data_constr_ref)
        constr = constr_by_path.get(impl.data_constr_ref)
        if constr is None:
            _report_value_type_constraint(
                impl.path,
                f"the DATA-CONSTR {impl.data_constr_ref} is not declared in this file",
                model.skip_log,
                category=SkipCategory.OUTSIDE_FILE,
            )
            continue
        lost: list[str] = []
        if constr.rule_count > 1:
            lost.append(f"{constr.rule_count - 1} further DATA-CONSTR-RULE(s)")
        if not constr.internal_present:
            lost.append("a DATA-CONSTR-RULE with no INTERNAL-CONSTRS")
        if constr.phys_present and impl.data_constr_ref not in application_refs:
            lost.append("PHYS-CONSTRS, an application data type's range")
        lost.extend(f"{tag} in INTERNAL-CONSTRS" for tag in constr.internal_other_tags)
        lower = _value_type_bound("LOWER-LIMIT", constr.internal_lower_text, constr.internal_lower_interval, lost)
        upper = _value_type_bound("UPPER-LIMIT", constr.internal_upper_text, constr.internal_upper_interval, lost)
        if lower is not None and upper is not None and lower > upper:
            lost.append("a LOWER-LIMIT above the UPPER-LIMIT, so neither bound")
            lower = upper = None
        impl.value_min, impl.value_max = lower, upper
        if lost:
            _report_value_type_constraint(impl.path, ", ".join(lost), model.skip_log)
    for constr in model.data_constrs:
        if constr.path in value_refs:
            continue
        for rule, internal in constr.internal_constrs:
            _report_unread_child(
                internal, f"{constr.path}/DATA-CONSTR-RULES", "DATA-CONSTR-RULE", _first_claimants(rule), model.skip_log
            )


def _is_own_marker_sdg(el: ET.Element, group: ET.Element) -> bool:
    """Whether *group*, on *el*, is the marker this writer mints.

    Both axes at once, because either alone admits a group SWCraft does not read:

    * the ELEMENT -- `SWC-INTERNAL-BEHAVIOR`, the only element `_read_swc` reads markers off. A `SWCRAFT` group on
      any other identifiable is metadata another tool wrote under SWCraft's name, and none of it is imported;
    * the SHAPE -- what the writer emits and nothing beside it: a group whose only attribute is `GID`, holding at
      least one child, where EVERY child is an `SD` whose only attribute is `GID`, which holds no element, and
      whose `GID` is one of `_OWN_MARKER_GIDS`, each at most once. One child outside that -- an `SD` of another
      `GID`, a nested `SDG`, an `SDX`, any other element, a second `SD` of a known `GID` or an attribute the
      writer never sets -- makes the whole group foreign, so it is reported and a component import aborts on it.

    The `SD`'s TEXT is not part of the shape: it is the marker's value, and the exchange decides what to do with
    one that is not the writer's (a typo must not turn an importable export into vendor metadata). The character
    data AROUND the `SD`s is part of it: ElementTree exposes it as the group's `.text` and each `SD`'s `.tail`,
    CDATA included, and the writer puts XML white space there and nothing else. Everything around the group is
    :func:`_is_the_writers_own_admin_data`'s. What neither reaches is stated in :func:`_index_named_element`.
    """
    if local_tag(el.tag) != "SWC-INTERNAL-BEHAVIOR" or group.get("GID") != SDG_GID:
        return False
    if set(group.attrib) != {"GID"} or len(group) == 0 or not _is_xml_whitespace(group.text):
        return False
    gids = [datum.get("GID") for datum in group]
    return (
        all(
            datum.tag == _SD and set(datum.attrib) == {"GID"} and len(datum) == 0 and _is_xml_whitespace(datum.tail)
            for datum in group
        )
        and all(gid in _OWN_MARKER_GIDS for gid in gids)
        and len(gids) == len(set(gids))
    )


def _is_xml_whitespace(text: str | None) -> bool:
    """Whether *text*, an ElementTree `.text` or `.tail`, is absent or XML white space alone.

    XML's white space is exactly space, tab, carriage return and line feed; `str.isspace`
    would also admit a no-break space, which is character data.
    """
    return text is None or not text.strip(" \t\r\n")


def _own_marker_sdgs(el: ET.Element) -> list[ET.Element]:
    """The one `SWCRAFT` group on *el* that :func:`_is_own_marker_sdg` admits -- or none.

    Looked for under EVERY direct `ADMIN-DATA` of *el* and every `SDGS` in each. None when more than one group is
    admitted: the writer mints one `SWCRAFT` group per behaviour and markers are keyed by `GID`, so two groups
    naming the same `SD` would keep one value and drop the other in silence; every such group is then foreign and
    reported.

    This is what is READ. Whether *el*'s block is also admitted without a report is
    :func:`_is_the_writers_own_admin_data`'s stricter verdict: a group read here can sit in a block that is
    reported, and the report then says the group beside the foreign content was read.
    """
    groups = [
        group
        for admin in el.findall(_ADMIN_DATA)
        for group in admin.findall(f"{_SDGS}/{_SDG}")
        if _is_own_marker_sdg(el, group)
    ]
    return groups if len(groups) <= 1 else []


def _is_the_writers_own_admin_data(el: ET.Element) -> bool:
    """Whether *el*'s `ADMIN-DATA` content is exactly the block the writer mints.

    The one positive rule the skip branch turns on. An element carrying no `ADMIN-DATA` child has nothing to judge;
    one carrying any is admitted without a report ONLY when it holds EXACTLY ONE `ADMIN-DATA`, with no attribute and
    nothing but XML white space as its text and its tail, whose only child is EXACTLY ONE `SDGS`, likewise, whose
    only child is EXACTLY ONE `SDG`, with nothing but XML white space as its tail, which :func:`_is_own_marker_sdg`
    admits -- so *el* is a `SWC-INTERNAL-BEHAVIOR` and the group is the writer's own as a whole. Every other shape
    is reported; :func:`_foreign_admin_data` only names what it found.

    **What the rule does not reach** is stated for the whole reader in :func:`_index_named_element`: among it, XML
    comments and processing instructions, which the parser discards, so a block carrying one is judged as if it
    were not there.
    """
    admins = el.findall(_ADMIN_DATA)
    if len(admins) != 1:
        return False
    (admin,) = admins
    if admin.attrib or not _is_xml_whitespace(admin.text) or not _is_xml_whitespace(admin.tail) or len(admin) != 1:
        return False
    (groups,) = admin
    if (
        groups.tag != _SDGS
        or groups.attrib
        or not _is_xml_whitespace(groups.text)
        or not _is_xml_whitespace(groups.tail)
        or len(groups) != 1
    ):
        return False
    (group,) = groups
    return group.tag == _SDG and _is_xml_whitespace(group.tail) and _is_own_marker_sdg(el, group)


def _foreign_admin_data(el: ET.Element) -> list[str]:
    """What in *el*'s `ADMIN-DATA` content is **not** SWCraft's own marker, as the tokens a report names.

    It names; it never decides (:func:`_is_the_writers_own_admin_data` does). Every direct `ADMIN-DATA` is read,
    and every `SDGS` in each. The structural tokens come first, once each: `<second ADMIN-DATA>` and
    `<second SDGS>` for a repeated wrapper, `<ADMIN-DATA attribute>` and `<SDGS attribute>` for an attribute on
    one, `<content after ADMIN-DATA>` for character data after the block, `<non-SDGS content>` for anything but
    `SDGS` in `ADMIN-DATA` (an element or character data, a tail after an `SDGS` included), `<non-SDG content>` for
    the same inside `SDGS`, and `<no SDG>` when no `ADMIN-DATA` holds any group. Then the `GID` of every group
    that is not the one :func:`_own_marker_sdgs` reads, `<no GID>` for a group without one. The list is never empty
    for a block the rule reports: each way that rule refuses has a token here.
    """
    admins = el.findall(_ADMIN_DATA)
    found: list[str] = []
    if len(admins) > 1:
        found.append("<second ADMIN-DATA>")
    for admin in admins:
        if admin.attrib:
            found.append("<ADMIN-DATA attribute>")
        if not _is_xml_whitespace(admin.tail):
            found.append("<content after ADMIN-DATA>")
        if not _is_xml_whitespace(admin.text) or any(
            child.tag != _SDGS or not _is_xml_whitespace(child.tail) for child in admin
        ):
            found.append("<non-SDGS content>")
        blocks = admin.findall(_SDGS)
        if len(blocks) > 1:
            found.append("<second SDGS>")
        for block in blocks:
            if block.attrib:
                found.append("<SDGS attribute>")
            if not _is_xml_whitespace(block.text) or any(
                child.tag != _SDG or not _is_xml_whitespace(child.tail) for child in block
            ):
                found.append("<non-SDG content>")
    groups = [group for admin in admins for group in admin.findall(f"{_SDGS}/{_SDG}")]
    if admins and not groups:
        found.append("<no SDG>")
    own = _own_marker_sdgs(el)
    return [
        *dict.fromkeys(found),
        *(group.get("GID") or "<no GID>" for group in groups if not any(group is mine for mine in own)),
    ]


def _read_swcraft_markers(el: ET.Element | None) -> dict[str, str]:
    """`{SD GID: text}` of *el*'s own SWCraft marker group.

    Called on a SWC's SWC-INTERNAL-BEHAVIOR, where the writer puts the marker and the only element on which a
    `SWCRAFT` group is read at all. :func:`_own_marker_sdgs` is the lookup the skip branch's suffix uses, so a
    group is read exactly when a report on its block says it is. Reading it in a reported block is deliberate: a
    component import refuses a file without the data-types marker before it aborts on lossy content, and a marker
    the exporter did write must not turn that abort into the earlier-release sentence. Empty for every element
    that carries no such group.
    """
    if el is None:
        return {}
    return {
        gid: datum.text or ""
        for group in _own_marker_sdgs(el)
        for datum in group.findall(_SD)
        if (gid := datum.get("GID"))
    }


def _index_named_element(el: ET.Element, parent_path: str, model: ImportModel) -> None:
    """Recursively index `el` and every named descendant by AUTOSAR path.

    Called only for a KNOWN_ELEMENT_TAGS top-level ELEMENTS child or a descendant of one; everything nested inside
    is indexed. The path is extended only at elements carrying a direct SHORT-NAME child, so grouping containers
    (PORTS, RUNNABLES, ...) are transparent.

    Skipped and reported:

    * a second element computing an already indexed path: the first is kept, the colliding one and its subtree
      are not indexed;
    * an element outside the AUTOSAR namespace (checked first), tagged `{namespace}LOCAL` (`{}LOCAL` for none) so
      that it equals no deferred element tag, with nothing under it walked;
    * a direct `ADMIN-DATA` child, once per element, its content neither imported nor preserved -- except, on a
      `SWC-INTERNAL-BEHAVIOR`, exactly the block the writer mints (:func:`_is_the_writers_own_admin_data`), which
      `_read_swc` reads into `SwcInfo.swcraft_markers`. A `SWCRAFT` group anywhere else, or of any other shape,
      is foreign and still aborts a component import.

    **What the reader does not reach:**

    * (i) an element in the AUTOSAR namespace that no mapper reads is dropped without a skip entry (`LONG-NAME`
      and `INTRODUCTION` included), except where a mapper enumerates a container's children itself (`PORTS`,
      `_BEHAVIOUR_CHILDREN`, `_RUNNABLE_CHILDREN`);
    * (ii) the children of `AR-PACKAGE` and of the `AUTOSAR` root, a foreign `ADMIN-DATA` there included;
    * (iii) attributes in a foreign namespace;
    * (iv) XML comments and processing instructions, which `ET.parse` discards.

    So the rule that content the importer cannot represent aborts a component import does not hold for (i) and
    (ii).
    """
    if not el.tag.startswith(f"{{{AR_NS}}}"):
        namespace, _, local = el.tag[1:].partition("}") if el.tag.startswith("{") else ("", "", el.tag)
        model.skip_log.record(
            path=f"{parent_path}/{local}",
            tag=f"{{{namespace}}}{local}",
            reason=(
                f"element {local} outside the AUTOSAR namespace ({namespace or 'no namespace'}) "
                "has no SWCraft representation -- not imported or preserved"
            ),
            category=SkipCategory.NOT_SUPPORTED,
            effect="abort",
        )
        return
    name = el.findtext(_SHORT_NAME)
    if name is not None:
        path = f"{parent_path}/{name}"
        if path in model.path_index:
            # One file declaring one path twice is not a split (splitting is across files, GST R4.3.0
            # :1065-1071): the two share one parent and so one name space -- [constr_2508], unless the file is
            # variant-rich. There the line drops "malformed export", which the variant clause would contradict.
            malformed = "" if model.variant_rich else "malformed export, "
            model.skip_log.record_reason(
                path,
                local_tag(el.tag),
                duplicate_name_reason(
                    f"duplicate full AUTOSAR path (already indexed as {model.path_index[path].tag!r}) -- "
                    f"{malformed}this declaration is dropped",
                    variant_rich=model.variant_rich,
                ),
                effect="abort",
            )
            return
        tag = local_tag(el.tag)
        # The documentation group (family F13) and a DESC not carried exactly (F14) are judged here, once for
        # every indexed element. A behaviour's DESC is its closed table's, reported whole.
        _report_documentation_group(el, path, tag, model.skip_log)
        judged = tag != "SWC-INTERNAL-BEHAVIOR"
        model.path_index[path] = RawElement(
            path=path,
            tag=tag,
            node=el,
            description=_extract_description(el, path if judged else None, model.skip_log if judged else None),
        )
    else:
        path = parent_path

    if el.findall(_ADMIN_DATA) and not _is_the_writers_own_admin_data(el):
        reason = (
            f"ADMIN-DATA/SDG vendor metadata on {local_tag(el.tag)} "
            f"'{name or path}' has no SWCraft representation -- not "
            "imported or preserved"
        )
        if _own_marker_sdgs(el):
            # The mixed block: say what was not read, and that SWCraft's own group was. Appended only here, so
            # an export's purely foreign lines keep their exact text.
            reason += f" (foreign SDG {', '.join(_foreign_admin_data(el))}; the SWCRAFT group beside it is read)"
        # Keyed at the element itself: the tag names the child. Descriptive on a budget's two elements,
        # behavioural elsewhere.
        model.skip_log.record(
            path=path,
            tag="ADMIN-DATA",
            reason=reason,
            category=SkipCategory.NOT_SUPPORTED,
            effect=REPORT if local_tag(el.tag) in _DESCRIPTIVE_ADMIN_DATA_OWNERS else ABORT,
        )

    for child in list(el):
        if local_tag(child.tag) == "SHORT-NAME":
            continue
        _index_named_element(child, path, model)


def _walk_package(
    package_el: ET.Element,
    parent_path: str,
    model: ImportModel,
) -> None:
    """Recursively walk one AR-PACKAGE, indexing its ELEMENTS and recording package structure.

    No package selection happens here: `--packages` is a dependency-closure selection after mapping, because
    selecting a composition pulls in leaf SWCs declared in other packages.
    """
    name = package_el.findtext(_SHORT_NAME)
    path = f"{parent_path}/{name}"
    model.packages.append(path)
    _report_package_content(package_el, path, model.skip_log)

    elements_el = package_el.find(_ELEMENTS)
    if elements_el is not None:
        for child in list(elements_el):
            tag = local_tag(child.tag)
            if tag not in KNOWN_ELEMENT_TAGS:
                child_name = child.findtext(_SHORT_NAME) or "<unnamed>"
                model.skip_log.record(
                    path=f"{path}/{child_name}",
                    tag=tag,
                    reason="unrecognized element tag inside ELEMENTS",
                    category=SkipCategory.NOT_SUPPORTED,
                    effect="abort",
                )
                continue
            _index_named_element(child, path, model)

    sub_packages_el = package_el.find(_AR_PACKAGES)
    if sub_packages_el is not None:
        for sub_package_el in sub_packages_el.findall(_AR_PACKAGE):
            _walk_package(sub_package_el, path, model)


def read_arxml(path: str) -> ImportModel:
    """Parse one ARXML file into an ImportModel.

    Args:
        path: filesystem path to the ARXML export file.

    Raises:
        FileNotFoundError: `path` does not exist.
        ValueError: the file is not well-formed XML, with a one-line message (file + line/column).
    """
    try:
        tree = ET.parse(path)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"ARXML file not found: {path}") from exc
    except ET.ParseError as exc:
        raise ValueError(f"Malformed ARXML in {path}: {exc}") from exc

    root = tree.getroot()
    model = ImportModel(
        source_path=str(path),
        declared_schema=schema_filename_from_location(root.get(f"{{{XSI_NAMESPACE}}}schemaLocation")),
        variant_rich=next(root.iter(_ar("VARIATION-POINT")), None) is not None,
    )

    ar_packages_el = root.find(_AR_PACKAGES)
    if ar_packages_el is None:
        # No AR-PACKAGES at all is a valid (if useless) AUTOSAR document --
        # an empty ImportModel, not an error.
        return model

    for package_el in ar_packages_el.findall(_AR_PACKAGE):
        _walk_package(package_el, parent_path="", model=model)

    _extract_typed_elements(model)

    return model


def _phys_limit_bounds_nothing(text: str, interval: str | None, side: str) -> bool:
    """Whether a ``PHYS-CONSTRS`` limit is infinite on its own *side*.

    Its own side's infinite spelling (:func:`is_infinite_limit`) under any interval, or ``INTERVAL-TYPE="INFINITE"``
    whatever number it holds: ``INFINITE`` states that the limit is infinite, so the number beside it bounds
    nothing. Two texts are never this, under any interval, and keep the *cannot read as a number* line: the other
    side's infinite spelling, which admits no value, and ``NaN`` (any case, padding aside), which is no number --
    so ``INFINITE`` over ``NaN`` is unreadable here, where a value type's ``INTERNAL-CONSTRS`` reads it as absent.
    The import carries every other such limit as the absent bound and names it in the report's *Represented in
    another form* block.
    """
    if is_infinite_limit(text, OTHER_LIMIT[side]) or text.strip(_NUMBER_PADDING).upper() == "NAN":
        return False
    return interval == "INFINITE" or is_infinite_limit(text, side)


@dataclass(frozen=True)
class PhysLimit:
    """One ``PHYS-CONSTRS`` limit of a :class:`DataConstrInfo`'s first rule, as this reader read it.

    *side* is ``LOWER-LIMIT`` or ``UPPER-LIMIT``; *text* the limit as spelled minus surrounding whitespace
    (``None`` when absent), *value* the model value that holds it exactly (``None`` when none does), and
    *interval_type* its ``INTERVAL-TYPE`` (``None`` when unstated). The names spell the ARXML source.
    """

    side: str
    text: str | None
    value: int | float | None
    interval_type: str | None

    @property
    def bounds_nothing(self) -> bool:
        """Stated and infinite on its own side: :func:`_phys_limit_bounds_nothing`."""
        return self.text is not None and _phys_limit_bounds_nothing(self.text, self.interval_type, self.side)


class PhysLimits(NamedTuple):
    """The two :class:`PhysLimit` of one constraint (:func:`phys_limits`), lower first."""

    lower_limit: PhysLimit
    upper_limit: PhysLimit


def phys_limits(constr: DataConstrInfo) -> PhysLimits:
    """*constr*'s two ``PHYS-CONSTRS`` limits: the one place the side, text, value and interval are paired."""
    return PhysLimits(
        lower_limit=PhysLimit("LOWER-LIMIT", constr.phys_lower_text, constr.phys_lower, constr.phys_lower_interval),
        upper_limit=PhysLimit("UPPER-LIMIT", constr.phys_upper_text, constr.phys_upper, constr.phys_upper_interval),
    )


def _report_access_variation_point(element: ET.Element, path: str, tag: str, skip_log: SkipLog) -> None:
    """One line for a stated ``VARIATION-POINT`` on an element the reader reads.

    The writer writes the element unconditionally, so its condition is lost, and the line keeps that loss from
    being silent. The category,
    effect and sentence are the closed tables' unread child's (:func:`_unread_reason`) -- behavioural. One line at
    the element's path, whatever the point holds, and none for an empty one. Called for every runnable access kind
    the reader reads, each result point, each data-received event and each exclusive area.
    """
    point = element.find(_VARIATION_POINT)
    if point is None or not _holds_content(point):
        return
    skip_log.record(
        path=path,
        tag="VARIATION-POINT",
        reason=_unread_reason("VARIATION-POINT", tag),
        category=SkipCategory.NOT_SUPPORTED,
        effect="abort",
    )


# ---------------------------------------------------------------------------
# The data-received event's DATA-IREF.
# ---------------------------------------------------------------------------

#: The event kinds with a closed table of their own: each table is applied to the
#: event, and the disabled-mode IREF's table to each of its disabled modes.
_CLOSED_EVENT_KINDS = frozenset({"TIMING-EVENT", "DATA-RECEIVED-EVENT"})
_DATA_IREF = _ar("DATA-IREF")
_TARGET_DATA_ELEMENT_REF = _ar("TARGET-DATA-ELEMENT-REF")


class _DataIref(NamedTuple):
    """The three :class:`SwcEventInfo` fields a ``DATA-IREF`` gives, each ``None`` when absent."""

    context_r_port_ref: str | None = None
    context_r_port_dest: str | None = None
    target_data_element_ref: str | None = None


def _read_data_iref(event: ET.Element, event_path: str, skip_log: SkipLog) -> _DataIref:
    """The three :class:`SwcEventInfo` fields of one ``DATA-RECEIVED-EVENT``.

    Read at the event's **own** ``DATA-IREF`` child, never a descendant's: the ``CONTEXT-R-PORT-REF`` path as
    written and its ``DEST``, and the ``TARGET-DATA-ELEMENT-REF`` path as written, each ``None`` when absent. The
    ``DATA-IREF``'s own closed table reports what else it holds, keyed at ``{event}/DATA-IREF``. The event's own
    ``VARIATION-POINT`` is the caller's line.
    """
    iref = event.find(_DATA_IREF)
    if iref is None:
        return _DataIref()
    _report_closed_table(iref, f"{event_path}/DATA-IREF", "R-VARIABLE-IN-ATOMIC-SWC-INSTANCE-REF", skip_log)
    port = iref.find(_CONTEXT_R_PORT_REF)
    return _DataIref(
        context_r_port_ref=port.text if port is not None else None,
        context_r_port_dest=port.get("DEST") if port is not None else None,
        target_data_element_ref=iref.findtext(_TARGET_DATA_ELEMENT_REF),
    )


# ---------------------------------------------------------------------------
# The asynchronous calls' result points.
# ---------------------------------------------------------------------------

_TIMEOUT = _ar("TIMEOUT")
_EVENT_SOURCE_REF = _ar("EVENT-SOURCE-REF")
_ASYNCHRONOUS_SERVER_CALL_POINT_REF = _ar("ASYNCHRONOUS-SERVER-CALL-POINT-REF")
_RESULT_POINT_DESC_REASON = "DESC not carried: the model holds no text for a result point"


def _read_runnable_result_points(
    r_el: ET.Element, runnable_path: str, claimants: set[int], skip_log: SkipLog
) -> list[SwcResultPointInfo]:
    """Read the ``ASYNCHRONOUS-SERVER-CALL-RESULT-POINTS`` of one RUNNABLE-ENTITY, in order.

    The port access containers' rules (:func:`_read_runnable_accesses`): a child that is not an
    ``ASYNCHRONOUS-SERVER-CALL-RESULT-POINT`` is an unrecognized-kind entry, and a ``SHORT-NAME`` the runnable
    already claimed (result points share its name space with the access points) is the index's duplicate line. A
    stated ``VARIATION-POINT`` is one line, a ``DESC`` another. The call point reference is captured as written,
    with its ``DEST``. **Not reached**: inside a result point, what the reader does not select.
    """
    points: list[SwcResultPointInfo] = []
    for container in list(r_el):
        if not _in_autosar_namespace(container) or local_tag(container.tag) != _RESULT_POINTS:
            continue
        for child in list(container):
            if not _in_autosar_namespace(child):
                continue
            tag = local_tag(child.tag)
            short_name = child.findtext(_SHORT_NAME)  # None: no element; "": empty, a path to the index
            path = f"{runnable_path}/{short_name or '<unnamed>'}"
            if short_name is not None and id(child) not in claimants:
                continue
            if tag != _RESULT_POINT:
                skip_log.record(
                    path=path,
                    tag=tag,
                    reason=f"unrecognized result point kind inside {_RESULT_POINTS} (only {_RESULT_POINT} is read)",
                    category=SkipCategory.NOT_SUPPORTED,
                    effect="abort",
                )
                continue
            _report_access_variation_point(child, path, tag, skip_log)
            _report_uncarried_description(_extract_description(child), path, _RESULT_POINT_DESC_REASON, skip_log)
            reference = child.find(_ASYNCHRONOUS_SERVER_CALL_POINT_REF)
            points.append(
                SwcResultPointInfo(
                    path=path,
                    name=short_name or "",
                    tag=tag,
                    call_point_ref=reference.text if reference is not None else None,
                    call_point_dest=reference.get("DEST") if reference is not None else None,
                )
            )
    return points
