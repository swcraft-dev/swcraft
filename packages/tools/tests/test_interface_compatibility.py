"""``validator.interface_compatibility``, the standard's interface condition, row by row.

The two interface rules (``rule:connector.interface-mismatch`` and
``rule:composition.delegation-interface-mismatch``) and the writer's delegation refusal judge one
condition, written once: this predicate. Each row is one parametrised case, success and refusal,
sender-receiver and client-server; where a row transcribes a constraint, its section comment names it.

``context`` selects one of two computations:

* ``subset``: every member of the *required* interface has a compatible counterpart **of the
  same name** in the *provided* one (``[constr_1069]`` 1(a), ``[constr_1079]`` 1(a), and
  ``[constr_1070]``/``[constr_1080]`` 1(a) for a ``require`` delegation);
* ``shared-member``: **at least one** member of the first interface has one in the second
  (``[constr_1070]``/``[constr_1080]`` 2(a), a ``provide`` delegation: inner provided against
  outer provided).

Two types are compatible **after resolution** (a reference chain followed to its value type and
that to its base type) when their kinds agree and their structure is identical; type names take
no part, member names do. An **application type**'s category, ``unit``, ``conversion`` and ``range``
are judged for identity, ``[constr_1047]`` 1(a) and 1(b) and, through 1(b), ``[constr_1051]`` 1, 2
and 4, read fail-closed; the ``application-`` rows are that judgement.

What it does not reach: which interface a caller treats as *required* (the callers' tests,
:data:`FRAMING_CALLERS`); attributes the model does not carry, or carries and this comparison ignores
(the ``limit-`` rows show only that they are ignored). A unit compared as text and a range compared for
identity are stricter than ``[constr_1052]`` and ``[constr_1126]``, so they can only reject a pair the
standard joins.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import validator_package
from conftest import REPO_ROOT
from swcraft_tools import validator
from swcraft_tools.data_description import DataDescription
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    Conversion,
    CSArgument,
    CSInterface,
    CSOperation,
    DataElement,
    EnumDataType,
    EnumLiteral,
    RecordDataType,
    RecordField,
    ReferenceDataType,
    SRInterface,
    ValueDataType,
    ValueRange,
)
from swcraft_tools.validator import Incompatibility, interface_compatibility, model_view

_PACKAGE_ROOT = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools"

# The data types every row is built over (invented names)


def _base(name: str, size: int, encoding: str, native: str) -> BaseDataType:
    return BaseDataType(name=name, kind="base_type", size=size, encoding=encoding, native_declaration=native)


def _value(name: str, base: str, **documentation: Any) -> ValueDataType:
    return ValueDataType(name=name, kind="value_type", base=base, **documentation)


def _enum(name: str, *literals: tuple[str, int]) -> EnumDataType:
    return EnumDataType(
        name=name, kind="enum", literals=[EnumLiteral(name=lit, value=value) for lit, value in literals]
    )


def _record(name: str, *fields: tuple[str, str]) -> RecordDataType:
    return RecordDataType(
        name=name, kind="record", fields=[RecordField(name=field, type=type_) for field, type_ in fields]
    )


def _array(name: str, element_type: str, length: int) -> ArrayDataType:
    return ArrayDataType(name=name, kind="array", element_type=element_type, length=length)


def _reference(name: str, base: str) -> ReferenceDataType:
    return ReferenceDataType(name=name, kind="reference_type", base=base)


def _application(
    name: str,
    *,
    unit: str = "",
    conversion: tuple[float, float] | None = None,
    bounds: tuple[float | None, float | None] | None = None,
) -> ApplicationDataType:
    """Build an application type declaring the given *unit*, *conversion* and *bounds*.

    ``None``/empty, the default, is *not declared*: the writer then emits no ``UNIT``,
    ``COMPU-METHOD`` or ``DATA-CONSTR`` for it.
    """
    return ApplicationDataType(
        name=name,
        kind="application_type",
        unit=unit,
        conversion=None if conversion is None else Conversion(factor=conversion[0], offset=conversion[1]),
        range=None if bounds is None else ValueRange(min=bounds[0], max=bounds[1]),
    )


DATA_TYPES: dict[str, Any] = {
    dt.name: dt
    for dt in (
        # Base types: `Octet` is `U8` under another name; each other one differs
        # from `U8` in exactly one of the three attributes `[constr_1220]` names
        # and the model carries.
        _base("U8", 8, "NONE", "uint8"),
        _base("Octet", 8, "NONE", "uint8"),
        _base("U16", 16, "NONE", "uint16"),
        _base("S8", 8, "2C", "uint8"),
        _base("Uchar", 8, "NONE", "unsigned char"),
        _base("Undeclared8", 8, "NONE", ""),
        # Value types over them.
        _value("Count", "U8"),
        _value("Tally", "Octet"),
        _value("Wide", "U16"),
        _value("Signed", "S8"),
        _value("Char", "Uchar"),
        _value("Bare", "Undeclared8"),
        _value("Speed", "U8", unit="km/h", value_min=0.0, value_max=200.0, description="vehicle speed"),
        _value("Velocity", "Octet", unit="m/s", value_min=-1.0, value_max=1.0),
        # Reference chains.
        _reference("TallyRef", "Tally"),
        _reference("TallyRefRef", "TallyRef"),
        _reference("WideRef", "Wide"),
        # Enums.
        _enum("Mode", ("OFF", 0), ("ON", 1)),
        _enum("State", ("OFF", 0), ("ON", 1)),
        _enum("ModeRenamed", ("OFF", 0), ("IDLE", 1)),
        _enum("ModeReordered", ("ON", 1), ("OFF", 0)),
        _enum("ModeAdded", ("OFF", 0), ("ON", 1), ("AUTO", 2)),
        _enum("ModeRevalued", ("OFF", 0), ("ON", 2)),
        # Records. A record field resolves in `application_types` alone, so the members are
        # application types: `Plain` and `PlainTwin` the same physics under two names, `Phys`
        # other physics.
        _record("Point", ("x", "Plain"), ("y", "Plain")),
        _record("Position", ("x", "PlainTwin"), ("y", "PlainTwin")),
        _record("PointRenamed", ("x", "Plain"), ("z", "Plain")),
        _record("PointReordered", ("y", "Plain"), ("x", "Plain")),
        _record("PointShort", ("x", "Plain")),
        _record("PointWide", ("x", "Plain"), ("y", "Phys")),
        _record("PointGhost", ("x", "Plain"), ("y", "Ghost")),
        # Arrays, over application types for the same reason.
        _array("Buf4", "Plain", 4),
        _array("Bytes4", "PlainTwin", 4),
        _array("Buf8", "Plain", 8),
        _array("WideBuf4", "Phys", 4),
        # Application types. Each `PhysOther*` / `PhysNo*` differs from `Phys` in exactly ONE
        # physics field, the one its row names; `PhysTwin` is `Phys` under another name and
        # `Plain` declares none of the three.
        _application("Phys", unit="km/h", conversion=(0.1, 0.0), bounds=(0.0, 400.0)),
        _application("PhysTwin", unit="km/h", conversion=(0.1, 0.0), bounds=(0.0, 400.0)),
        _application("PhysOtherUnit", unit="m/s", conversion=(0.1, 0.0), bounds=(0.0, 400.0)),
        _application("PhysNoUnit", conversion=(0.1, 0.0), bounds=(0.0, 400.0)),
        # The rule compares units stripped: a padded spelling is the same unit to it
        # (and another DISPLAY-NAME in the file), and a unit of blanks is no unit.
        _application("PhysPaddedUnit", unit=" km/h ", conversion=(0.1, 0.0), bounds=(0.0, 400.0)),
        _application("PhysBlankUnit", unit="  ", conversion=(0.1, 0.0), bounds=(0.0, 400.0)),
        _application("PhysOtherFactor", unit="km/h", conversion=(0.01, 0.0), bounds=(0.0, 400.0)),
        _application("PhysOtherOffset", unit="km/h", conversion=(0.1, -40.0), bounds=(0.0, 400.0)),
        _application("PhysNoConversion", unit="km/h", bounds=(0.0, 400.0)),
        _application("PhysOtherMin", unit="km/h", conversion=(0.1, 0.0), bounds=(10.0, 400.0)),
        _application("PhysOtherMax", unit="km/h", conversion=(0.1, 0.0), bounds=(0.0, 300.0)),
        _application("PhysOpenMax", unit="km/h", conversion=(0.1, 0.0), bounds=(0.0, None)),
        _application("PhysNoRange", unit="km/h", conversion=(0.1, 0.0)),
        _application("Plain"),
        _application("PlainTwin"),
        # Composites over them: a record field may name an application type, so the physics
        # is reached through the nesting too.
        _record("PhysPoint", ("v", "Phys")),
        _record("PhysPointTwin", ("v", "PhysTwin")),
        _record("PhysPointOther", ("v", "PhysOtherUnit")),
    )
}


_APPLICATION_CLASSES = (ApplicationDataType, EnumDataType, RecordDataType, ArrayDataType)


def _side(type_name: str) -> dict[str, str]:
    """Return the type field a prototype naming *type_name* sets.

    `application_type` for an application type of :data:`DATA_TYPES`, `implementation_type` for
    anything else (an implementation type, a base type or an undeclared name).
    """
    if isinstance(DATA_TYPES.get(type_name), _APPLICATION_CLASSES):
        return {"application_type": type_name}
    return {"implementation_type": type_name}


def _sr(name: str, *elements: tuple[str, str] | DataElement, description: str = "") -> SRInterface:
    return SRInterface(
        name=name,
        kind="sender_receiver",
        description=description,
        data_elements=[
            element if isinstance(element, DataElement) else DataElement(name=element[0], **_side(element[1]))
            for element in elements
        ],
    )


def _op(name: str, *arguments: tuple[str, str, str]) -> CSOperation:
    return CSOperation(
        name=name,
        arguments=[CSArgument(name=arg, direction=direction, **_side(type_)) for arg, direction, type_ in arguments],
    )


def _cs(name: str, *operations: CSOperation) -> CSInterface:
    return CSInterface(name=name, kind="client_server", operations=list(operations))


def _interfaces(*interfaces: SRInterface | CSInterface) -> dict[str, SRInterface | CSInterface]:
    return {interface.name: interface for interface in interfaces}


def _row(
    row_id: str,
    required: SRInterface | CSInterface,
    provided: SRInterface | CSInterface,
    expected: str | None,
    *,
    context: str = "subset",
) -> Any:
    return pytest.param(required.name, provided.name, context, _interfaces(required, provided), expected, id=row_id)


_ARG_IN = ("x", "in", "Count")

ROWS = [
    # The interface kind
    _row(
        "kind-sr-vs-cs",
        _sr("R", ("a", "Count")),
        _cs("P", _op("f")),
        "interface kinds differ: sender_receiver vs client_server",
    ),
    _row(
        "kind-cs-vs-sr",
        _cs("R", _op("f")),
        _sr("P", ("a", "Count")),
        "interface kinds differ: client_server vs sender_receiver",
    ),
    # Sender-receiver: the subset by name, [constr_1069] 1(a)
    _row(
        "sr-subset-required-within-provided",
        _sr("R", ("a", "Count"), ("b", "Count")),
        _sr("P", ("a", "Count"), ("b", "Count"), ("c", "Count")),
        None,
    ),
    _row(
        "sr-subset-required-exceeds-provided",
        _sr("R", ("a", "Count"), ("b", "Count"), ("c", "Count")),
        _sr("P", ("a", "Count"), ("b", "Count")),
        "data element 'c' has no counterpart in 'P'",
    ),
    _row(
        "sr-declaration-order-decides-the-first",
        _sr("R", ("z", "Count"), ("a", "Wide"), ("b", "Count")),
        _sr("P", ("a", "Count")),
        "data element 'z' has no counterpart in 'P'",
    ),
    # Types by resolved structure, [constr_1050] over [constr_1220]
    _row("type-same-structure-other-names", _sr("R", ("a", "Count")), _sr("P", ("a", "Tally")), None),
    _row(
        "type-base-size",
        _sr("R", ("a", "Count")),
        _sr("P", ("a", "Wide")),
        "data element 'a': type 'Count' vs 'Wide' — base type size 8 vs 16",
    ),
    _row(
        "type-base-encoding",
        _sr("R", ("a", "Count")),
        _sr("P", ("a", "Signed")),
        "data element 'a': type 'Count' vs 'Signed' — base type encoding NONE vs 2C",
    ),
    _row(
        "type-base-native-declaration",
        _sr("R", ("a", "Count")),
        _sr("P", ("a", "Char")),
        "data element 'a': type 'Count' vs 'Char' — base type native declaration 'uint8' vs 'unsigned char'",
    ),
    _row("type-base-native-declaration-blank-is-not-judged", _sr("R", ("a", "Count")), _sr("P", ("a", "Bare")), None),
    _row(
        "type-kind-differs",
        _sr("R", ("a", "Count")),
        _sr("P", ("a", "Mode")),
        "data element 'a': type 'Count' vs 'Mode' — kind value_type vs enum",
    ),
    # A reference chain is resolved first
    _row("reference-chain-resolves-to-the-same-base", _sr("R", ("a", "Count")), _sr("P", ("a", "TallyRefRef")), None),
    _row(
        "reference-chain-resolves-to-another-base",
        _sr("R", ("a", "Count")),
        _sr("P", ("a", "WideRef")),
        "data element 'a': type 'Count' vs 'WideRef' — base type size 8 vs 16",
    ),
    # Records, [constr_1187] over [constr_1050] 1(b)
    _row("record-same-structure", _sr("R", ("a", "Point")), _sr("P", ("a", "Position")), None),
    _row(
        "record-field-renamed",
        _sr("R", ("a", "Point")),
        _sr("P", ("a", "PointRenamed")),
        "data element 'a': type 'Point' vs 'PointRenamed' — record field 2 'y' vs 'z'",
    ),
    _row(
        "record-field-reordered",
        _sr("R", ("a", "Point")),
        _sr("P", ("a", "PointReordered")),
        "data element 'a': type 'Point' vs 'PointReordered' — record field 1 'x' vs 'y'",
    ),
    _row(
        "record-field-missing",
        _sr("R", ("a", "Point")),
        _sr("P", ("a", "PointShort")),
        "data element 'a': type 'Point' vs 'PointShort' — record fields 2 vs 1",
    ),
    _row(
        "record-field-incompatible-type",
        _sr("R", ("a", "Point")),
        _sr("P", ("a", "PointWide")),
        # The member is an application type, so what differs is its physics.
        "data element 'a': type 'Point' vs 'PointWide' — record field 'y': type 'Plain' vs 'Phys' — "
        "application type unit none vs 'km/h'",
    ),
    # Arrays, [constr_1187] and [constr_1050] 1(c)
    _row("array-same-structure", _sr("R", ("a", "Buf4")), _sr("P", ("a", "Bytes4")), None),
    _row(
        "array-length",
        _sr("R", ("a", "Buf4")),
        _sr("P", ("a", "Buf8")),
        "data element 'a': type 'Buf4' vs 'Buf8' — array length 4 vs 8",
    ),
    _row(
        "array-element-type",
        _sr("R", ("a", "Buf4")),
        _sr("P", ("a", "WideBuf4")),
        "data element 'a': type 'Buf4' vs 'WideBuf4' — array element type 'Plain' vs 'Phys' — "
        "application type unit none vs 'km/h'",
    ),
    # Enums
    _row("enum-same-literals", _sr("R", ("a", "Mode")), _sr("P", ("a", "State")), None),
    _row(
        "enum-literal-renamed",
        _sr("R", ("a", "Mode")),
        _sr("P", ("a", "ModeRenamed")),
        "data element 'a': type 'Mode' vs 'ModeRenamed' — enum literal 2 'ON' vs 'IDLE'",
    ),
    _row(
        "enum-literal-reordered",
        _sr("R", ("a", "Mode")),
        _sr("P", ("a", "ModeReordered")),
        "data element 'a': type 'Mode' vs 'ModeReordered' — enum literal 1 'OFF' vs 'ON'",
    ),
    _row(
        "enum-literal-added",
        _sr("R", ("a", "Mode")),
        _sr("P", ("a", "ModeAdded")),
        "data element 'a': type 'Mode' vs 'ModeAdded' — enum literals 2 vs 3",
    ),
    _row(
        "enum-literal-revalued",
        _sr("R", ("a", "Mode")),
        _sr("P", ("a", "ModeRevalued")),
        "data element 'a': type 'Mode' vs 'ModeRevalued' — enum literal 2 'ON' value 1 vs 2",
    ),
    # Abstention: an unresolvable type is another rule's finding
    _row("abstain-provided-type-undeclared", _sr("R", ("a", "Count")), _sr("P", ("a", "Ghost")), None),
    _row("abstain-required-type-undeclared", _sr("R", ("a", "Ghost")), _sr("P", ("a", "Count")), None),
    _row("abstain-prototype-typed-by-a-base-type", _sr("R", ("a", "U8")), _sr("P", ("a", "U16")), None),
    _row("abstain-record-field-type-undeclared", _sr("R", ("a", "Point")), _sr("P", ("a", "PointGhost")), None),
    _row(
        "abstain-does-not-hide-a-later-member",
        _sr("R", ("a", "Ghost"), ("b", "Count")),
        _sr("P", ("a", "Count")),
        "data element 'b' has no counterpart in 'P'",
    ),
    # Client-server, [constr_1079] 1(a) over [constr_1078]/[constr_1076]
    _row(
        "cs-subset-required-within-provided", _cs("R", _op("f"), _op("g")), _cs("P", _op("f"), _op("g"), _op("h")), None
    ),
    _row(
        "cs-subset-required-exceeds-provided",
        _cs("R", _op("f"), _op("g"), _op("h")),
        _cs("P", _op("f"), _op("g")),
        "operation 'h' has no counterpart in 'P'",
    ),
    _row(
        "cs-argument-count",
        _cs("R", _op("f", _ARG_IN)),
        _cs("P", _op("f", _ARG_IN, ("y", "in", "Count"))),
        "operation 'f': 1 argument vs 2",
    ),
    _row(
        "cs-argument-count-plural",
        _cs("R", _op("f", _ARG_IN, ("y", "in", "Count"))),
        _cs("P", _op("f", _ARG_IN)),
        "operation 'f': 2 arguments vs 1",
    ),
    _row(
        "cs-argument-name-differs-is-compatible",
        _cs("R", _op("f", ("x", "in", "Count"))),
        _cs("P", _op("f", ("y", "in", "Tally"))),
        None,
    ),
    _row(
        "cs-argument-direction",
        _cs("R", _op("f", ("x", "in", "Count"))),
        _cs("P", _op("f", ("x", "out", "Count"))),
        "operation 'f': argument 1 'x' direction in vs out",
    ),
    _row(
        "cs-argument-type",
        _cs("R", _op("f", ("x", "in", "Count"))),
        _cs("P", _op("f", ("y", "in", "Wide"))),
        "operation 'f': argument 1 'x': type 'Count' vs 'Wide' — base type size 8 vs 16",
    ),
    # Shared-member: [constr_1070]/[constr_1080] 2(a)
    _row(
        "shared-member-one-shared",
        _sr("Inner", ("a", "Count"), ("b", "Count")),
        _sr("Outer", ("b", "Tally"), ("c", "Count")),
        None,
        context="shared-member",
    ),
    _row(
        "shared-member-none-shared",
        _sr("Inner", ("a", "Count"), ("b", "Count")),
        _sr("Outer", ("c", "Count"), ("d", "Count")),
        "no data element of 'Inner' has a counterpart in 'Outer'",
        context="shared-member",
    ),
    _row(
        "shared-member-same-name-but-incompatible",
        _sr("Inner", ("a", "Count"), ("b", "Point")),
        _sr("Outer", ("a", "Wide"), ("b", "PointShort")),
        "no data element of 'Inner' has a compatible counterpart in 'Outer' "
        "(data element 'a': type 'Count' vs 'Wide' — base type size 8 vs 16)",
        context="shared-member",
    ),
    _row(
        "shared-member-client-server",
        _cs("Inner", _op("f"), _op("g")),
        _cs("Outer", _op("g"), _op("h")),
        None,
        context="shared-member",
    ),
    # An ABSTAINING shared member satisfies the existential clause: the unresolvable type is
    # another rule's finding. Two rows: the abstaining member alone, and beside one that is
    # definitely incompatible, where the answer is still silence.
    _row(
        "shared-member-abstains-on-an-unresolvable-shared-member",
        _sr("Inner", ("a", "Ghost"), ("b", "Count")),
        _sr("Outer", ("a", "Ghost2"), ("c", "Count")),
        None,
        context="shared-member",
    ),
    _row(
        "shared-member-abstention-outweighs-an-incompatible-shared-member",
        _sr("Inner", ("a", "Count"), ("b", "Ghost")),
        _sr("Outer", ("a", "Wide"), ("b", "Ghost")),
        None,
        context="shared-member",
    ),
    _row(
        "shared-member-client-server-none-shared",
        _cs("Inner", _op("f")),
        _cs("Outer", _op("h")),
        "no operation of 'Inner' has a counterpart in 'Outer'",
        context="shared-member",
    ),
    _row(
        "shared-member-kind",
        _sr("Inner", ("a", "Count")),
        _cs("Outer", _op("f")),
        "interface kinds differ: sender_receiver vs client_server",
        context="shared-member",
    ),
    _row(
        "subset-for-a-require-delegation",
        _sr("Inner", ("a", "Count"), ("b", "Count"), ("c", "Count")),
        _sr("Outer", ("a", "Count"), ("b", "Count")),
        "data element 'c' has no counterpart in 'Outer'",
    ),
    # Application types, [constr_1051] read fail-closed as identity:
    # `[constr_1047]`: two ApplicationPrimitiveDataTypes are compatible iff they have the same
    # category (1a) and compatible SwDataDefProps (1b), or through a DataPrototypeMapping (2, 3),
    # which a connector does not have. `[constr_1051]` is 1(b): compatible Units or neither has
    # one (1), compatible conversion methods or neither associates one (2), compatible dataConstrs
    # (4). The comparison follows that order: category (the resolved kind), unit, conversion, range.
    _row("application-identical-physics-other-name", _sr("R", ("a", "Phys")), _sr("P", ("a", "PhysTwin")), None),
    _row(
        "application-unit-differs",
        _sr("R", ("a", "Phys")),
        _sr("P", ("a", "PhysOtherUnit")),
        "data element 'a': type 'Phys' vs 'PhysOtherUnit' — application type unit 'km/h' vs 'm/s'",
    ),
    _row(
        "application-unit-absent-on-one-side",
        _sr("R", ("a", "Phys")),
        _sr("P", ("a", "PhysNoUnit")),
        "data element 'a': type 'Phys' vs 'PhysNoUnit' — application type unit 'km/h' vs none",
    ),
    _row("application-unit-padded-is-the-same-unit", _sr("R", ("a", "Phys")), _sr("P", ("a", "PhysPaddedUnit")), None),
    _row(
        "application-unit-of-blanks-is-no-unit", _sr("R", ("a", "PhysNoUnit")), _sr("P", ("a", "PhysBlankUnit")), None
    ),
    _row(
        "application-conversion-factor",
        _sr("R", ("a", "Phys")),
        _sr("P", ("a", "PhysOtherFactor")),
        "data element 'a': type 'Phys' vs 'PhysOtherFactor' — application type conversion factor 0.1 vs 0.01",
    ),
    _row(
        "application-conversion-offset",
        _sr("R", ("a", "Phys")),
        _sr("P", ("a", "PhysOtherOffset")),
        "data element 'a': type 'Phys' vs 'PhysOtherOffset' — application type conversion offset 0.0 vs -40.0",
    ),
    _row(
        "application-conversion-absent-on-one-side",
        _sr("R", ("a", "Phys")),
        _sr("P", ("a", "PhysNoConversion")),
        "data element 'a': type 'Phys' vs 'PhysNoConversion' — application type conversion declared vs none",
    ),
    _row(
        "application-range-min",
        _sr("R", ("a", "Phys")),
        _sr("P", ("a", "PhysOtherMin")),
        "data element 'a': type 'Phys' vs 'PhysOtherMin' — application type range min 0.0 vs 10.0",
    ),
    _row(
        "application-range-max",
        _sr("R", ("a", "Phys")),
        _sr("P", ("a", "PhysOtherMax")),
        "data element 'a': type 'Phys' vs 'PhysOtherMax' — application type range max 400.0 vs 300.0",
    ),
    _row(
        "application-range-bound-absent-on-one-side",
        _sr("R", ("a", "Phys")),
        _sr("P", ("a", "PhysOpenMax")),
        "data element 'a': type 'Phys' vs 'PhysOpenMax' — application type range max 400.0 vs none",
    ),
    _row(
        "application-range-absent-on-one-side",
        _sr("R", ("a", "Phys")),
        _sr("P", ("a", "PhysNoRange")),
        "data element 'a': type 'Phys' vs 'PhysNoRange' — application type range declared vs none",
    ),
    # Nothing declared on either side is `[constr_1051]` 1 and 2's "or neither of
    # them" arm, and the pair is compatible.
    _row(
        "application-declares-no-physics-on-either-side", _sr("R", ("a", "Plain")), _sr("P", ("a", "PlainTwin")), None
    ),
    # An application type and an implementation type are never compatible at a connector: the
    # standard admits that pair only through a mapping context a connector does not have.
    _row(
        "application-against-an-implementation-type",
        _sr("R", ("a", "Phys")),
        _sr("P", ("a", "Count")),
        "data element 'a': type 'Phys' vs 'Count' — kind application_type vs value_type",
    ),
    _row(
        "implementation-against-an-application-type",
        _sr("R", ("a", "Count")),
        _sr("P", ("a", "Phys")),
        "data element 'a': type 'Count' vs 'Phys' — kind value_type vs application_type",
    ),
    _row(
        "application-against-an-enum",
        _sr("R", ("a", "Phys")),
        _sr("P", ("a", "Mode")),
        "data element 'a': type 'Phys' vs 'Mode' — kind application_type vs enum",
    ),
    _row("abstain-application-counterpart-undeclared", _sr("R", ("a", "Phys")), _sr("P", ("a", "Ghost")), None),
    _row(
        "record-over-application-types-same-physics",
        _sr("R", ("a", "PhysPoint")),
        _sr("P", ("a", "PhysPointTwin")),
        None,
    ),
    _row(
        "record-over-application-types-differing-physics",
        _sr("R", ("a", "PhysPoint")),
        _sr("P", ("a", "PhysPointOther")),
        "data element 'a': type 'PhysPoint' vs 'PhysPointOther' — record field 'v': type 'Phys' vs 'PhysOtherUnit' "
        "— application type unit 'km/h' vs 'm/s'",
    ),
    _row(
        "application-through-a-client-server-argument",
        _cs("R", _op("f", ("x", "in", "Phys"))),
        _cs("P", _op("f", ("y", "in", "PhysOtherUnit"))),
        "operation 'f': argument 1 'x': type 'Phys' vs 'PhysOtherUnit' — application type unit 'km/h' vs 'm/s'",
    ),
    # Stated limits: not judged, so compatible
    _row(
        "limit-init-asil-description-differ",
        _sr("R", DataElement(name="a", implementation_type="Count", init=0, asil="B"), description="one"),
        _sr("P", DataElement(name="a", implementation_type="Count", init=5, asil="D"), description="two"),
        None,
    ),
    _row("limit-unit-and-range-differ", _sr("R", ("a", "Speed")), _sr("P", ("a", "Velocity")), None),
]


@pytest.mark.parametrize(("required", "provided", "context", "interfaces", "expected"), ROWS)
def test_the_predicate_answers_each_row_of_the_table(
    required: str,
    provided: str,
    context: str,
    interfaces: dict[str, SRInterface | CSInterface],
    expected: str | None,
) -> None:
    answer = interface_compatibility(
        required,
        provided,
        context=context,
        interfaces=interfaces,
        data_types=DataDescription.last_declared(DATA_TYPES.values()),
    )

    if expected is None:
        assert answer is None, answer
    else:
        assert answer == Incompatibility(subject=expected)


class _Untouchable(Mapping[str, Any]):
    """A map any access to which fails the test: the short-circuit must read neither."""

    def __getitem__(self, key: str) -> Any:
        raise AssertionError(f"the map was read for {key!r}")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("the map was iterated")

    def __len__(self) -> int:
        raise AssertionError("the map was measured")

    def __contains__(self, key: object) -> bool:
        raise AssertionError(f"the map was searched for {key!r}")

    def get(self, key: str, default: Any = None) -> Any:
        raise AssertionError(f"the map was read for {key!r}")


class _UntouchableView(DataDescription):
    """A data description any lookup in which fails the test: the short-circuit must resolve no type."""

    def application(self, name: str | None) -> Any:
        raise AssertionError(f"the view was asked for application type {name!r}")

    def implementation(self, name: str | None) -> Any:
        raise AssertionError(f"the view was asked for implementation type {name!r}")

    def base(self, name: str | None) -> Any:
        raise AssertionError(f"the view was asked for base type {name!r}")

    @property
    def data_types(self) -> Any:
        raise AssertionError("the view's data types were iterated")


@pytest.mark.parametrize("context", ["subset", "shared-member"])
def test_the_same_interface_name_is_compatible_and_reads_neither_map(context: str) -> None:
    """The same interface name is compatible without reading either map, declared or not.

    An undeclared interface is ``port.undeclared-interface``'s; two ports naming the same one were
    never a mismatch.
    """
    assert (
        interface_compatibility(
            "ISame",
            "ISame",
            context=context,
            interfaces=_Untouchable(),
            data_types=_UntouchableView(()),
        )
        is None
    )


@pytest.mark.parametrize(("required", "provided"), [("IGhost", "P"), ("R", "IGhost")])
def test_an_undeclared_interface_is_an_abstention(required: str, provided: str) -> None:
    """``port.undeclared-interface`` owns an interface that is not declared; this rule reports nothing."""
    interfaces = _interfaces(_sr("R", ("a", "Count")), _sr("P", ("b", "Count")))

    assert (
        interface_compatibility(
            required,
            provided,
            context="subset",
            interfaces=interfaces,
            data_types=DataDescription.last_declared(DATA_TYPES.values()),
        )
        is None
    )


def test_a_context_outside_the_two_computations_is_refused() -> None:
    interfaces = _interfaces(_sr("R", ("a", "Count")), _sr("P", ("a", "Count")))

    with pytest.raises(ValueError, match="context"):
        interface_compatibility(
            "R",
            "P",
            context="superset",
            interfaces=interfaces,
            data_types=DataDescription.last_declared(DATA_TYPES.values()),
        )


@pytest.mark.parametrize("context", ["subset", "shared-member"])
def test_a_member_name_declared_twice_is_answered_by_the_first_declaration(context: str) -> None:
    """A member name declared twice is answered by its first declaration on the offered side.

    The duplicate is already refused by ``name.duplicate-data-element``, so this is no abstention.
    """
    required = _sr("R", ("a", "Count"))
    provided = SRInterface(
        name="P",
        kind="sender_receiver",
        data_elements=[
            DataElement(name="a", implementation_type="Wide"),
            DataElement(name="a", implementation_type="Count"),
        ],
    )

    answer = interface_compatibility(
        "R",
        "P",
        context=context,
        interfaces=_interfaces(required, provided),
        data_types=DataDescription.last_declared(DATA_TYPES.values()),
    )

    assert answer is not None
    assert "base type size 8 vs 16" in answer.subject


def test_the_verdicts_are_lazy_so_a_member_after_the_answer_is_never_resolved() -> None:
    """``subset`` stops at the first failure: the undeclared second element is never resolved into the memo."""
    required = _sr("R", ("a", "Count"), ("b", "Ghost"))
    provided = _sr("P", ("a", "Wide"), ("b", "Count"))
    seen: list[str] = []

    class _RecordingTypes(Mapping[str, Any]):
        def __getitem__(self, key: str) -> Any:
            return DATA_TYPES[key]

        def __iter__(self) -> Iterator[str]:
            return iter(DATA_TYPES)

        def __len__(self) -> int:
            return len(DATA_TYPES)

        def get(self, key: str, default: Any = None) -> Any:
            seen.append(key)
            return DATA_TYPES.get(key, default)

    answer = interface_compatibility(
        "R",
        "P",
        context="subset",
        interfaces=_interfaces(required, provided),
        data_types=DataDescription.last_declared(_RecordingTypes().values()),
    )

    assert answer == Incompatibility(subject="data element 'a': type 'Count' vs 'Wide' — base type size 8 vs 16")
    assert "Ghost" not in seen, seen


@pytest.mark.parametrize(("direction", "context"), [("require", "subset"), ("provide", "shared-member")])
def test_the_delegation_clause_is_one_functions_answer(direction: str, context: str) -> None:
    """``delegation_context`` is the one choice of clause both delegation halves share.

    The reporting half (``check_composition_consistency``) and the refusing half
    (``writer/arxml_generation._emit_delegation``) must choose the same clause for one pair of
    ports; :func:`test_no_module_spells_the_delegation_clause_beside_the_helper` is the other
    direction.
    """
    assert validator.delegation_context(direction) == context


#: The two modules that spell the clause: the helper's own, and the predicate's, whose closed set of
#: contexts names it.
_SHARED_MEMBER_SPELLERS = frozenset({"validator/compositions.py", "validator/compatibility.py"})


def test_no_module_spells_the_delegation_clause_beside_the_helper() -> None:
    """The choice is made in exactly one place in the package."""
    offenders = [
        path.relative_to(_PACKAGE_ROOT).as_posix()
        for path in sorted(_PACKAGE_ROOT.rglob("*.py"), key=lambda p: p.as_posix())
        if '"shared-member"' in path.read_text(encoding="utf-8")
        and path.relative_to(_PACKAGE_ROOT).as_posix() not in _SHARED_MEMBER_SPELLERS
    ]

    assert offenders == []


def test_a_type_cycle_is_an_abstention_and_terminates() -> None:
    """A record that contains itself is ``data-type.cycle``'s; resolution must stop and abstain."""
    data_types = dict(DATA_TYPES)
    data_types["Loop"] = _record("Loop", ("next", "Loop"))
    data_types["LoopA"] = _record("LoopA", ("next", "LoopB"))
    data_types["LoopB"] = _record("LoopB", ("next", "LoopA"))
    interfaces = _interfaces(_sr("R", ("a", "Loop")), _sr("P", ("a", "LoopA")))

    assert (
        interface_compatibility(
            "R",
            "P",
            context="subset",
            interfaces=interfaces,
            data_types=DataDescription.last_declared(data_types.values()),
        )
        is None
    )


def test_an_incompatibility_carries_one_sentence_and_nothing_else() -> None:
    """``Incompatibility`` holds one rendered ``subject`` and is frozen."""
    assert [field.name for field in dataclasses.fields(Incompatibility)] == ["subject"]
    answer = Incompatibility(subject="x")
    with pytest.raises(AttributeError):
        answer.subject = "y"  # type: ignore[misc]


# One vocabulary: a single renderer, and callers that only frame its subject

#: ``(module path relative to the package, function name)`` of every caller that frames the
#: predicate's subject into a message of its own: the connector rule, the delegation phase, the
#: ``check_port_compatibility`` pre-flight the MCP tool answers before a connector is authored,
#: and the writer's ``_emit_delegation``, which raises with the same rendered subject.
FRAMING_CALLERS: tuple[tuple[str, str], ...] = (
    ("validator/connectors.py", "check_connector_compatibility"),
    ("validator/compositions.py", "_delegation_records"),
    ("validator/compatibility.py", "check_port_compatibility"),
    ("writer/arxml_generation.py", "_emit_delegation"),
)

#: Phrases of the subject's vocabulary, allowed as string text only inside the predicate's own
#: functions, so a caller cannot word a subject beside the renderer.
#:
#: Does not reach the subject's other fragments (``type '``, ``argument ``, ``record field ``,
#: ``enum literal ``), which the package legitimately writes elsewhere; a subject worded from
#: those alone is caught only by the framing callers' message oracles.
_VOCABULARY_PHRASES = ("has no counterpart", "compatible counterpart", "interface kinds differ", "base type size")

#: The middle of the sentence the two validator rules frame a subject in, spelled once by
#: ``validator.interface_mismatch_reason``. Only the middle: each caller opens with its own words.
_FRAME_PHRASE = "is not satisfied by"


def _module_functions(path: Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _predicate_family() -> dict[str, ast.FunctionDef]:
    """Return ``interface_compatibility`` and every module-level validator function it reaches."""
    functions = {name: node for path in validator_package.paths() for name, node in _module_functions(path).items()}
    family: dict[str, ast.FunctionDef] = {}
    pending = ["interface_compatibility"]
    while pending:
        name = pending.pop()
        if name in family or name not in functions:
            continue
        family[name] = functions[name]
        pending.extend(
            node.func.id
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        )
    return family


def _text_constants(function: ast.FunctionDef) -> Iterator[str]:
    """Return every string constant in *function*, its docstring excluded."""
    body = function.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                yield node.value


def test_there_is_exactly_one_subject_renderer_in_the_package() -> None:
    definitions = [
        path.relative_to(_PACKAGE_ROOT).as_posix()
        for path in sorted(_PACKAGE_ROOT.rglob("*.py"), key=lambda p: p.as_posix())
        if "_render_subject" in _module_functions(path)
    ]

    assert definitions == ["validator/compatibility.py"]


def test_there_is_exactly_one_interface_mismatch_sentence_in_the_package() -> None:
    """The frame around the subject is spelled once in the package.

    ``writer/arxml_generation._emit_delegation`` is out of scope: a delegation's refusal is a
    different sentence, so the pinned phrase is the frame's middle, not ``mismatch``.

    Does not reach a caller that rewords the frame instead of reusing it; the three callers'
    byte-for-byte message oracles catch that.
    """
    definitions = [
        path.relative_to(_PACKAGE_ROOT).as_posix()
        for path in sorted(_PACKAGE_ROOT.rglob("*.py"), key=lambda p: p.as_posix())
        if "interface_mismatch_reason" in _module_functions(path)
    ]

    assert definitions == ["validator/compatibility.py"]

    offenders: list[str] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py"), key=lambda p: p.as_posix()):
        relative = path.relative_to(_PACKAGE_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name == "interface_mismatch_reason":
                continue
            offenders.extend(f"{relative}:{node.name}" for text in _text_constants(node) if _FRAME_PHRASE in text)

    assert offenders == []


def test_every_incompatibility_is_built_from_the_renderer() -> None:
    """Every ``Incompatibility(...)`` in the package takes its subject from ``_render_subject``."""
    offenders: list[str] = []
    sites = 0
    for path in sorted(_PACKAGE_ROOT.rglob("*.py"), key=lambda p: p.as_posix()):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Incompatibility"
            ):
                continue
            sites += 1
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            rendered = (
                len(arguments) == 1
                and isinstance(arguments[0], ast.Call)
                and isinstance(arguments[0].func, ast.Name)
                and arguments[0].func.id == "_render_subject"
            )
            if not rendered:
                offenders.append(f"{path.relative_to(_PACKAGE_ROOT).as_posix()}:{node.lineno}")

    assert sites > 0, "no Incompatibility is built anywhere: the scan is looking at the wrong thing"
    assert offenders == []


def test_no_function_outside_the_predicate_words_the_subject_vocabulary() -> None:
    """A caller frames the subject; it never spells a subject's words beside the renderer."""
    family = _predicate_family()
    assert {"interface_compatibility", "_render_subject", "resolve_type", "_types_compatible"} <= set(family)

    offenders: list[str] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py"), key=lambda p: p.as_posix()):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(_PACKAGE_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if relative.startswith("validator/") and node.name in family:
                continue
            for text in _text_constants(node):
                offenders.extend(
                    f"{relative}:{node.name} spells {phrase!r}" for phrase in _VOCABULARY_PHRASES if phrase in text
                )

    assert offenders == []


def test_every_framing_caller_reads_the_predicates_subject() -> None:
    """Each caller registered in :data:`FRAMING_CALLERS` asks the predicate and uses its ``subject``."""
    for module, name in FRAMING_CALLERS:
        functions = _module_functions(_PACKAGE_ROOT / module)
        assert name in functions, f"{module} defines no {name}"
        nodes = list(ast.walk(functions[name]))
        assert any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "interface_compatibility"
            for node in nodes
        ), f"{module}:{name} does not ask interface_compatibility"
        assert any(isinstance(node, ast.Attribute) and node.attr == "subject" for node in nodes), (
            f"{module}:{name} does not frame the predicate's subject"
        )


# The reading discipline: the predicate receives maps and reads no layer


def _layer_readers() -> frozenset[str]:
    """Return the layer reads the validator modules import.

    Every name imported from ``reader`` or ``model_state``, and every ``read_*`` from ``data_description``.

    The view's own class is not a read: a caller hands it to the predicate in place of a map.
    """
    return frozenset(
        alias.asname or alias.name
        for path in validator_package.paths()
        for node in ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if node.module in {"swcraft_tools.reader", "swcraft_tools.model_state"}
        or (node.module == "swcraft_tools.data_description" and alias.name.startswith("read_"))
    )


def test_the_predicate_takes_maps_and_no_model_root() -> None:
    parameters = inspect.signature(interface_compatibility).parameters

    assert list(parameters) == ["required", "provided", "context", "interfaces", "data_types"]
    assert [name for name, parameter in parameters.items() if parameter.kind is parameter.KEYWORD_ONLY] == [
        "context",
        "interfaces",
        "data_types",
    ]


def test_no_function_the_predicate_reaches_reads_a_layer() -> None:
    """Statically: the predicate's call closure names no reader.

    A per-connector caller must not make the predicate read a layer per call; the callers read
    both layers once and hand them over as maps.
    """
    readers = _layer_readers()
    assert {"read_interfaces", "read_data_description", "scan_layer"} <= readers

    calls = {
        f"{name} calls {node.func.id}"
        for name, function in _predicate_family().items()
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in readers
    }

    assert calls == set()


def test_the_predicate_runs_with_every_layer_reader_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dynamically: with every reader raising, a full comparison still answers.

    The readers are disabled on the view's module, where the package calls them; a name imported
    from ``model_state`` for its type alone is not a read.
    """
    readers = _layer_readers() & set(vars(model_view))
    assert {"read_interfaces", "read_data_description", "scan_layer"} <= readers
    for name in readers:
        monkeypatch.setattr(model_view, name, _refuse(name))
    interfaces = _interfaces(
        _sr("R", ("a", "Point"), ("b", "Buf4"), ("c", "Mode")),
        _sr("P", ("a", "Position"), ("b", "Bytes4"), ("c", "State"), ("d", "WideRef")),
    )

    assert (
        interface_compatibility(
            "R",
            "P",
            context="subset",
            interfaces=interfaces,
            data_types=DataDescription.last_declared(DATA_TYPES.values()),
        )
        is None
    )


def _refuse(name: str) -> Any:
    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(f"the predicate called {name}")

    return refused


# One name on two sides: identity is the side and the name

#: A synthetic name an application type and an implementation type both carry.
_SHARED_NAME = "ProbeShared"

#: The pair, and a base type for the implementation side, handed to :class:`DataDescription` as a
#: list: a name-keyed map such as :data:`DATA_TYPES` cannot hold two declarations of one name.
_SHARED_PAIR_TYPES = [
    _base("ProbeShared_Base", 8, "NONE", "uint8"),
    _application(_SHARED_NAME),
    _value(_SHARED_NAME, "ProbeShared_Base"),
]


#: The kind each side's ``ProbeShared`` resolves to: the subject names the two.
_SIDE_KIND = {"application_type": "application_type", "implementation_type": "value_type"}


def _shared_element(side: str) -> DataElement:
    return DataElement(name="a", **{side: _SHARED_NAME})


def _shared_argument(side: str) -> CSArgument:
    return CSArgument(name="x", direction="in", **{side: _SHARED_NAME})


class TestOneNameOnTwoSides:
    """Two prototypes naming one name on opposite sides name two declarations.

    ``_prototype_type_difference``'s short-circuit and ``resolve_type``'s memo are keyed by
    ``(name, application)``; keyed on the name alone, the pair would read as compatible. The
    same-side rows pin that the side is not over-read into a refusal.
    """

    @pytest.mark.parametrize(
        ("required_side", "provided_side"),
        [("application_type", "implementation_type"), ("implementation_type", "application_type")],
    )
    def test_a_data_element_pair_on_opposite_sides_is_incompatible(
        self, required_side: str, provided_side: str
    ) -> None:
        interfaces = _interfaces(_sr("R", _shared_element(required_side)), _sr("P", _shared_element(provided_side)))

        answer = interface_compatibility(
            "R", "P", context="subset", interfaces=interfaces, data_types=DataDescription(_SHARED_PAIR_TYPES)
        )

        assert answer == Incompatibility(
            subject=f"data element 'a': type '{_SHARED_NAME}' vs '{_SHARED_NAME}' — "
            f"kind {_SIDE_KIND[required_side]} vs {_SIDE_KIND[provided_side]}"
        )

    @pytest.mark.parametrize(
        ("required_side", "provided_side"),
        [("application_type", "implementation_type"), ("implementation_type", "application_type")],
    )
    def test_an_argument_pair_on_opposite_sides_is_incompatible(self, required_side: str, provided_side: str) -> None:
        interfaces = _interfaces(
            _cs("R", CSOperation(name="f", arguments=[_shared_argument(required_side)])),
            _cs("P", CSOperation(name="f", arguments=[_shared_argument(provided_side)])),
        )

        answer = interface_compatibility(
            "R", "P", context="subset", interfaces=interfaces, data_types=DataDescription(_SHARED_PAIR_TYPES)
        )

        assert answer == Incompatibility(
            subject=f"operation 'f': argument 1 'x': type '{_SHARED_NAME}' vs '{_SHARED_NAME}' — "
            f"kind {_SIDE_KIND[required_side]} vs {_SIDE_KIND[provided_side]}"
        )

    @pytest.mark.parametrize("side", ["application_type", "implementation_type"])
    def test_a_pair_on_one_side_stays_compatible(self, side: str) -> None:
        interfaces = _interfaces(_sr("R", _shared_element(side)), _sr("P", _shared_element(side)))

        assert (
            interface_compatibility(
                "R", "P", context="subset", interfaces=interfaces, data_types=DataDescription(_SHARED_PAIR_TYPES)
            )
            is None
        )
