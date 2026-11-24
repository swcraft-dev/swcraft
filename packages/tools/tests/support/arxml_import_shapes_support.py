"""The ARXML import shape corpus: one fixture per shape, with what each holds.

``fixtures/arxml_import_shapes/`` is the synthetic corpus the importer's unit tests run on: every merge rule,
unresolved-reference role, report category and hand-over has a file here with **invented names** (real exports are
the closed gates' subject, never a unit test's).

For each file the table records its shape (:attr:`Shape.subject`), what the external oracle
(``arxml_export_oracle``) answers on it, and what ``swcraft_tools.arxml_reader.read_arxml`` finds in it; both are
re-taken by ``test_arxml_import_shapes.py`` on every run. The oracle is an independent parser that knows no SWCraft
kind, so a disagreement is written into the subject rather than reconciled (``value_conflict_enum.arxml``).

What this corpus does **not** reach
-----------------------------------

* scale, groups larger than two, and duplicate absolute paths (the reader drops those at parse time);
* case-only pairs and names shared across kinds (fixtures elsewhere);
* the written model: a fixture is an input;
* AUTOSAR legality beyond what the shape needs (``cyclic_composition_graph`` is malformed on purpose; none is
  schema-validated here).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from arxml_export_oracle import (
    CAUSE_CUT_PART_WAY,
    CAUSE_LEAF_ONLY,
    CAUSE_NO_TREE,
    CONFLICTING,
    MERGED,
    Export,
    read_export,
)
from swcraft_tools.arxml_reader import ImportModel, RawElement, read_arxml

#: The directory the corpus lives in. ``test_arxml_import_shapes.py`` pins its file
#: set against :data:`SHAPES` in both directions.
SHAPES_DIR = Path(__file__).parents[1] / "fixtures" / "arxml_import_shapes"


@dataclass(frozen=True)
class GroupExpectation:
    """One ``(layer, kind, SHORT-NAME)`` the oracle finds declared twice.

    ``key`` is the oracle's spelling, ``layer/kind/name``, with ``None`` for a
    declaration kind the importer maps onto no layer (``COMPU-METHOD``), which still
    takes a verdict because a group reached from it inherits that verdict.
    """

    key: str
    verdict: str
    own_bytes_identical: bool
    """Equal comparing each declaration's own bytes, references compared as
    the export spelled them. This is the **declaration-local** answer, the one
    a single pass can give, and where it differs from ``verdict`` the fixture
    exists to show that it is wrong."""

    locally_equal: bool
    """Equal with every reference replaced by the group key of the duplicated
    declaration it points into. Still not the verdict: a locally equal group
    whose referenced group conflicts conflicts too."""

    cyclic: bool
    inherited_from: str | None
    paths: tuple[str, ...]


@dataclass(frozen=True)
class OracleCounts:
    """What ``arxml_export_oracle`` answers on one fixture."""

    named_elements: int
    references: int
    unresolved: int
    distinct_unresolved_targets: tuple[str, ...]
    unresolved_by_cause: dict[str, int]
    """Distinct unresolved **targets** per cause, the report's own grouping."""

    unresolved_by_role: dict[str, int]
    """Unresolved references per ``<tag> on <owner tag>`` role."""

    shadowed: dict[str, tuple[str, ...]]
    """Unresolved targets whose bare short name is declared elsewhere in the
    file by an element of the same layer, by layer."""

    groups: tuple[GroupExpectation, ...]

    @property
    def merged(self) -> int:
        return sum(1 for group in self.groups if group.verdict == MERGED)

    @property
    def conflicting(self) -> int:
        return sum(1 for group in self.groups if group.verdict == CONFLICTING)

    @property
    def cyclic(self) -> int:
        return sum(1 for group in self.groups if group.cyclic)

    @property
    def own_bytes_identical(self) -> int:
        return sum(1 for group in self.groups if group.own_bytes_identical)


@dataclass(frozen=True)
class ReaderCounts:
    """What ``read_arxml`` finds in one fixture.

    Recorded so "the fixture parses with the reader" has content: a file the reader
    reads nothing out of parses too.
    """

    packages: int
    path_index: int
    sw_base_types: int
    impl_data_types: int
    app_primitive_data_types: int
    app_record_data_types: int
    app_array_data_types: int
    compu_methods: int
    sr_interfaces: int
    cs_interfaces: int
    swcs: int
    compositions: int
    skip_entries: int
    # Read for an application type's inline `unit` and `range`; defaulted because
    # most rows hold neither.
    units: int = 0
    data_constrs: int = 0


@dataclass(frozen=True)
class Shape:
    """One fixture: why it exists, and what both parsers answer on it."""

    filename: str
    subject: str
    oracle: OracleCounts
    reader: ReaderCounts

    @property
    def path(self) -> Path:
        return SHAPES_DIR / self.filename


def _group(
    key: str,
    verdict: str,
    *,
    own: bool,
    local: bool,
    paths: tuple[str, ...],
    cyclic: bool = False,
    inherited_from: str | None = None,
) -> GroupExpectation:
    return GroupExpectation(
        key=key,
        verdict=verdict,
        own_bytes_identical=own,
        locally_equal=local,
        cyclic=cyclic,
        inherited_from=inherited_from,
        paths=paths,
    )


#: Every fixture of the corpus, by filename, pinned against the directory listing.
SHAPES: dict[str, Shape] = {
    "identical_duplicates.arxml": Shape(
        filename="identical_duplicates.arxml",
        subject=(
            "Two declarations of one name that nothing SWCraft can read distinguishes: Flag with "
            "no description at all, and ILevel with two DIFFERENT descriptions. Both merge, "
            "because DESC is excluded from the equality relation; the merge line owes the second "
            "description's path and the fact that it was not carried."
        ),
        oracle=OracleCounts(
            named_elements=17,
            references=3,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(
                _group(
                    "data_types/SW-BASE-TYPE/Flag",
                    MERGED,
                    own=True,
                    local=True,
                    paths=("/Acme/Left/Types/Flag", "/Acme/Right/Types/Flag"),
                ),
                _group(
                    "interfaces/SENDER-RECEIVER-INTERFACE/ILevel",
                    MERGED,
                    own=True,
                    local=True,
                    paths=("/Acme/Left/Interfaces/ILevel", "/Acme/Right/Interfaces/ILevel"),
                ),
            ),
        ),
        reader=ReaderCounts(
            packages=9,
            path_index=8,
            sw_base_types=3,
            impl_data_types=1,
            app_primitive_data_types=0,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=2,
            cs_interfaces=0,
            swcs=0,
            compositions=0,
            skip_entries=0,
        ),
    ),
    "value_conflict_enum.arxml": Shape(
        filename="value_conflict_enum.arxml",
        subject=(
            "A value conflict: one enum name declared twice, five COMPU-SCALEs against four. For "
            "the IMPORTER the backing scales are part of the enum's own comparison, so this is a "
            "direct conflict on DriveModeEnum; for the ORACLE, which knows no SWCraft kind, the "
            "COMPU-METHOD is a declaration of its own and the enum inherits its conflict. The two "
            "readings name the same defect at the same place and are recorded rather than "
            "reconciled."
        ),
        oracle=OracleCounts(
            named_elements=11,
            references=2,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(
                _group(
                    "None/COMPU-METHOD/DriveModeEnum_CompuMethod",
                    CONFLICTING,
                    own=False,
                    local=False,
                    paths=(
                        "/Acme/Left/Methods/DriveModeEnum_CompuMethod",
                        "/Acme/Right/Methods/DriveModeEnum_CompuMethod",
                    ),
                ),
                _group(
                    "data_types/APPLICATION-PRIMITIVE-DATA-TYPE/DriveModeEnum",
                    CONFLICTING,
                    own=False,
                    local=True,
                    inherited_from="None/COMPU-METHOD/DriveModeEnum_CompuMethod",
                    paths=("/Acme/Left/AppTypes/DriveModeEnum", "/Acme/Right/AppTypes/DriveModeEnum"),
                ),
            ),
        ),
        reader=ReaderCounts(
            packages=7,
            path_index=4,
            sw_base_types=0,
            impl_data_types=0,
            app_primitive_data_types=2,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=2,
            sr_interfaces=0,
            cs_interfaces=0,
            swcs=0,
            compositions=0,
            skip_entries=0,
        ),
    ),
    "transitive_conflict.arxml": Shape(
        filename="transitive_conflict.arxml",
        subject=(
            "A conflict inherited through a reference: Word16 differs in its own properties "
            "(16 bits against 32) and Distance differs in nothing but the path it points at, so "
            "Distance conflicts only because Word16 does. The conflict line owes the group it "
            "inherits from."
        ),
        oracle=OracleCounts(
            named_elements=9,
            references=2,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(
                _group(
                    "data_types/IMPLEMENTATION-DATA-TYPE/Distance",
                    CONFLICTING,
                    own=False,
                    local=True,
                    inherited_from="data_types/SW-BASE-TYPE/Word16",
                    paths=("/Acme/Left/Types/Distance", "/Acme/Right/Types/Distance"),
                ),
                _group(
                    "data_types/SW-BASE-TYPE/Word16",
                    CONFLICTING,
                    own=False,
                    local=False,
                    paths=("/Acme/Left/Types/Word16", "/Acme/Right/Types/Word16"),
                ),
            ),
        ),
        reader=ReaderCounts(
            packages=5,
            path_index=4,
            sw_base_types=2,
            impl_data_types=2,
            app_primitive_data_types=0,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=0,
            cs_interfaces=0,
            swcs=0,
            compositions=0,
            skip_entries=0,
        ),
    ),
    "three_level_fixpoint.arxml": Shape(
        filename="three_level_fixpoint.arxml",
        subject=(
            "Three duplicate groups stacked -- base type, value type, interface -- of which only "
            "the bottom one is byte-identical. The fixpoint merges all THREE; a declaration-local "
            "comparison answers ONE merge and TWO conflicts, and that answer is wrong. The one "
            "own-bytes-identical group in the table beside three merged verdicts IS the two "
            "answers side by side."
        ),
        oracle=OracleCounts(
            named_elements=15,
            references=4,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(
                _group(
                    "data_types/IMPLEMENTATION-DATA-TYPE/Level",
                    MERGED,
                    own=False,
                    local=True,
                    paths=("/Acme/Left/Types/Level", "/Acme/Right/Types/Level"),
                ),
                _group(
                    "data_types/SW-BASE-TYPE/Byte8",
                    MERGED,
                    own=True,
                    local=True,
                    paths=("/Acme/Left/Types/Byte8", "/Acme/Right/Types/Byte8"),
                ),
                _group(
                    "interfaces/SENDER-RECEIVER-INTERFACE/ILevel",
                    MERGED,
                    own=False,
                    local=True,
                    paths=("/Acme/Left/Interfaces/ILevel", "/Acme/Right/Interfaces/ILevel"),
                ),
            ),
        ),
        reader=ReaderCounts(
            packages=7,
            path_index=8,
            sw_base_types=2,
            impl_data_types=2,
            app_primitive_data_types=0,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=2,
            cs_interfaces=0,
            swcs=0,
            compositions=0,
            skip_entries=0,
        ),
    ),
    "clean_path_only_duplicate.arxml": Shape(
        filename="clean_path_only_duplicate.arxml",
        subject=(
            "A duplicate whose two declarations differ in NOTHING but the path they reference, "
            "and which is therefore clean. It is value_conflict_enum.arxml's twin: the same "
            "two-level enum shape with the two compu-methods identical instead of five scales "
            "against four, so one file merges both groups where the other conflicts on both."
        ),
        oracle=OracleCounts(
            named_elements=11,
            references=2,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(
                _group(
                    "None/COMPU-METHOD/GearStateEnum_CompuMethod",
                    MERGED,
                    own=True,
                    local=True,
                    paths=(
                        "/Acme/Left/Methods/GearStateEnum_CompuMethod",
                        "/Acme/Right/Methods/GearStateEnum_CompuMethod",
                    ),
                ),
                _group(
                    "data_types/APPLICATION-PRIMITIVE-DATA-TYPE/GearStateEnum",
                    MERGED,
                    own=False,
                    local=True,
                    paths=("/Acme/Left/AppTypes/GearStateEnum", "/Acme/Right/AppTypes/GearStateEnum"),
                ),
            ),
        ),
        reader=ReaderCounts(
            packages=7,
            path_index=4,
            sw_base_types=0,
            impl_data_types=0,
            app_primitive_data_types=2,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=2,
            sr_interfaces=0,
            cs_interfaces=0,
            swcs=0,
            compositions=0,
            skip_entries=0,
        ),
    ),
    "cyclic_composition_graph.arxml": Shape(
        filename="cyclic_composition_graph.arxml",
        subject=(
            "Two duplicate groups whose verdicts depend on each other through a reference cycle: "
            "each Upper places its own tree's Lower and each Lower places its own tree's Upper. "
            "Neither can be refused on its own properties and neither can be PROVED merged, so "
            "both are conflicting and the line says cyclic -- fail-closed, because the standard "
            "defines no equality of definition at all."
        ),
        oracle=OracleCounts(
            named_elements=13,
            references=4,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(
                _group(
                    "compositions/COMPOSITION-SW-COMPONENT-TYPE/Lower",
                    CONFLICTING,
                    own=False,
                    local=True,
                    cyclic=True,
                    paths=("/Acme/Left/Compositions/Lower", "/Acme/Right/Compositions/Lower"),
                ),
                _group(
                    "compositions/COMPOSITION-SW-COMPONENT-TYPE/Upper",
                    CONFLICTING,
                    own=False,
                    local=True,
                    cyclic=True,
                    paths=("/Acme/Left/Compositions/Upper", "/Acme/Right/Compositions/Upper"),
                ),
            ),
        ),
        reader=ReaderCounts(
            packages=5,
            path_index=8,
            sw_base_types=0,
            impl_data_types=0,
            app_primitive_data_types=0,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=0,
            cs_interfaces=0,
            swcs=0,
            compositions=4,
            skip_entries=0,
        ),
    ),
    "packages_selection_conflict.arxml": Shape(
        filename="packages_selection_conflict.arxml",
        subject=(
            "A conflicting pair of which a package selection keeps exactly one member: TreeA and "
            "TreeB are two parallel copies of one cluster, and App is typed out of TreeA alone. "
            "Over the whole file both groups conflict; under a selection of App whose closure is "
            "taken after full mapping, exactly one member of each survives and the conflict is "
            "not a conflict."
        ),
        # RatioSwc's behaviour is counted: it declares HANDLE-TERMINATION-AND-RESTART, without
        # which a component is refused.
        oracle=OracleCounts(
            named_elements=17,
            references=4,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(
                _group(
                    "data_types/IMPLEMENTATION-DATA-TYPE/Ratio",
                    CONFLICTING,
                    own=False,
                    local=True,
                    inherited_from="data_types/SW-BASE-TYPE/Word",
                    paths=("/Acme/TreeA/Types/Ratio", "/Acme/TreeB/Types/Ratio"),
                ),
                _group(
                    "data_types/SW-BASE-TYPE/Word",
                    CONFLICTING,
                    own=False,
                    local=False,
                    paths=("/Acme/TreeA/Types/Word", "/Acme/TreeB/Types/Word"),
                ),
            ),
        ),
        reader=ReaderCounts(
            packages=8,
            path_index=9,
            sw_base_types=2,
            impl_data_types=2,
            app_primitive_data_types=0,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=0,
            skip_entries=0,
        ),
    ),
    "shadowed_unresolved_target.arxml": Shape(
        filename="shadowed_unresolved_target.arxml",
        subject=(
            "An unresolved target whose bare short name IS declared elsewhere in the file, by an "
            "element of the layer the reference names. Binding by name would bind it to an "
            "unrelated element, which is why the reference is recorded as the PATH the export "
            "spelled. The cause is the tree cut part-way: /Acme is declared, /Acme/Absent is not."
        ),
        oracle=OracleCounts(
            named_elements=8,
            references=2,
            unresolved=1,
            distinct_unresolved_targets=("/Acme/Absent/Types/SpeedValue",),
            unresolved_by_cause={CAUSE_CUT_PART_WAY: 1},
            unresolved_by_role={"TYPE-TREF on VARIABLE-DATA-PROTOTYPE": 1},
            shadowed={"data_types": ("/Acme/Absent/Types/SpeedValue",)},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=4,
            path_index=4,
            sw_base_types=1,
            impl_data_types=1,
            app_primitive_data_types=0,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=0,
            compositions=0,
            skip_entries=0,
        ),
    ),
    "unresolved_argument_type.arxml": Shape(
        filename="unresolved_argument_type.arxml",
        subject=(
            "A client-server operation argument whose type is not in this export, beside one that "
            "is. The cause is the leaf alone absent -- the parent package is declared and holds "
            "other elements -- which is the one cause of the three a user reads as a deleted "
            "element rather than a missing file."
        ),
        oracle=OracleCounts(
            named_elements=10,
            references=3,
            unresolved=1,
            distinct_unresolved_targets=("/Acme/Chassis/Types/AbsentResultType",),
            unresolved_by_cause={CAUSE_LEAF_ONLY: 1},
            unresolved_by_role={"TYPE-TREF on ARGUMENT-DATA-PROTOTYPE": 1},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=4,
            path_index=6,
            sw_base_types=1,
            impl_data_types=1,
            app_primitive_data_types=0,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=0,
            cs_interfaces=1,
            swcs=0,
            compositions=0,
            skip_entries=0,
        ),
    ),
    "unresolved_implementation_type_reference.arxml": Shape(
        filename="unresolved_implementation_type_reference.arxml",
        subject=(
            "A reference type whose base is not in this export -- the whole /Vendor tree absent, "
            "the delivery's other file. Until this slice the whole type was skipped for it. A "
            "base type and a value type that DO resolve sit beside it, so a test can tell "
            "'the type was kept with the path recorded' from 'the export mapped nothing'."
        ),
        oracle=OracleCounts(
            named_elements=6,
            references=2,
            unresolved=1,
            distinct_unresolved_targets=("/Vendor/Types/BaseSample",),
            unresolved_by_cause={CAUSE_NO_TREE: 1},
            unresolved_by_role={"IMPLEMENTATION-DATA-TYPE-REF on SW-DATA-DEF-PROPS-CONDITIONAL": 1},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=3,
            path_index=3,
            sw_base_types=1,
            impl_data_types=2,
            app_primitive_data_types=0,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=0,
            cs_interfaces=0,
            swcs=0,
            compositions=0,
            skip_entries=0,
        ),
    ),
    "platform_package_reference.arxml": Shape(
        filename="platform_package_reference.arxml",
        subject=(
            "A reference into the delivered AUTOSAR platform package, which this export does not "
            "declare. It carries no special case: the cause is the same absent tree any other "
            "missing delivery file gets. The fixture exists so that 'SWCraft treats the platform "
            "package as any other missing file' is something a test says."
        ),
        oracle=OracleCounts(
            named_elements=7,
            references=2,
            unresolved=1,
            distinct_unresolved_targets=("/AUTOSAR_Platform/ImplementationDataTypes/uint8",),
            unresolved_by_cause={CAUSE_NO_TREE: 1},
            unresolved_by_role={"IMPLEMENTATION-DATA-TYPE-REF on SW-DATA-DEF-PROPS-CONDITIONAL": 1},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=4,
            path_index=3,
            sw_base_types=0,
            impl_data_types=1,
            app_primitive_data_types=0,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=0,
            compositions=0,
            skip_entries=0,
        ),
    ),
    "unresolved_reference_roles.arxml": Shape(
        filename="unresolved_reference_roles.arxml",
        subject=(
            "One unresolved reference per role of the design's table that no other fixture here "
            "carries: a value type's base, a data element's type, a record field's type, an array "
            "element's type, a component port's interface and a composition BOUNDARY port's "
            "interface. The last two share one role string -- a port is a port whoever owns it -- "
            "so a test tells them apart by the element the reference stands in."
        ),
        # ReporterSwc's behaviour is counted: it declares HANDLE-TERMINATION-AND-RESTART, without
        # which a component is refused.
        oracle=OracleCounts(
            named_elements=20,
            references=7,
            unresolved=6,
            distinct_unresolved_targets=(
                "/Acme/Chassis/AppTypes/AbsentFieldType",
                "/Acme/Chassis/AppTypes/AbsentItemType",
                "/Acme/Chassis/Interfaces/AbsentOuterInterface",
                "/Acme/Chassis/Interfaces/AbsentProvidedInterface",
                "/Acme/Chassis/Types/AbsentElementType",
                "/Vendor/BaseTypes/Word",
            ),
            unresolved_by_cause={CAUSE_LEAF_ONLY: 5, CAUSE_NO_TREE: 1},
            unresolved_by_role={
                "BASE-TYPE-REF on SW-DATA-DEF-PROPS-CONDITIONAL": 1,
                "PROVIDED-INTERFACE-TREF on P-PORT-PROTOTYPE": 2,
                "TYPE-TREF on APPLICATION-RECORD-ELEMENT": 1,
                "TYPE-TREF on ELEMENT": 1,
                "TYPE-TREF on VARIABLE-DATA-PROTOTYPE": 1,
            },
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=7,
            path_index=13,
            sw_base_types=0,
            impl_data_types=1,
            app_primitive_data_types=0,
            app_record_data_types=1,
            app_array_data_types=1,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=1,
            skip_entries=0,
        ),
    ),
    "same_name_base_and_implementation.arxml": Shape(
        filename="same_name_base_and_implementation.arxml",
        subject=(
            "A SW-BASE-TYPE and an IMPLEMENTATION-DATA-TYPE declaring exactly one SHORT-NAME: they "
            "are elements of two layers and both are written under the one name. It is deliberately "
            "NOT a duplicate group: "
            "the verdict is keyed on (layer, KIND, name) and never judges across kinds, so the "
            "oracle reports no group here at all."
        ),
        oracle=OracleCounts(
            named_elements=6,
            references=1,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=4,
            path_index=2,
            sw_base_types=1,
            impl_data_types=1,
            app_primitive_data_types=0,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=0,
            cs_interfaces=0,
            swcs=0,
            compositions=0,
            skip_entries=0,
        ),
    ),
    "prototype_of_an_absent_type.arxml": Shape(
        filename="prototype_of_an_absent_type.arxml",
        subject=(
            "A component prototype whose type is not in this export mints an external stub, "
            "because a prototype's type is needed as a REFERENCE only and the stub carries the path and the "
            "DEST verbatim. A resolvable member sits beside it."
        ),
        oracle=OracleCounts(
            named_elements=15,
            references=5,
            unresolved=1,
            distinct_unresolved_targets=("/Vendor/Components/RemoteSwc",),
            unresolved_by_cause={CAUSE_NO_TREE: 1},
            unresolved_by_role={"TYPE-TREF on SW-COMPONENT-PROTOTYPE": 1},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=6,
            path_index=9,
            sw_base_types=1,
            impl_data_types=1,
            app_primitive_data_types=0,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=1,
            skip_entries=0,
        ),
    ),
    "connector_to_a_stub_port.arxml": Shape(
        filename="connector_to_a_stub_port.arxml",
        subject=(
            "A connector endpoint on a port of an external stub, named under the stub's own "
            "TYPE-TREF path: the stub mints the port by name and DEST and the connector is kept. A "
            "delegation onto the resolvable member sits beside it."
        ),
        oracle=OracleCounts(
            named_elements=18,
            references=13,
            unresolved=2,
            distinct_unresolved_targets=(
                "/Vendor/Components/RemoteSwc",
                "/Vendor/Components/RemoteSwc/pLevel",
            ),
            unresolved_by_cause={CAUSE_NO_TREE: 2},
            unresolved_by_role={
                "TARGET-P-PORT-REF on PROVIDER-IREF": 1,
                "TYPE-TREF on SW-COMPONENT-PROTOTYPE": 1,
            },
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=6,
            path_index=12,
            sw_base_types=1,
            impl_data_types=1,
            app_primitive_data_types=0,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=1,
            skip_entries=0,
        ),
    ),
    "component_same_name_pair.arxml": Shape(
        filename="component_same_name_pair.arxml",
        subject=(
            "A component-scoped export -- exactly one component, no composition, no connector -- "
            "carrying a same-name pair that differs. The verdict is `import model`'s alone, so this "
            "fixture pins that `import component`'s answer on it does not depend on the verdict."
        ),
        oracle=OracleCounts(
            named_elements=15,
            references=3,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(
                _group(
                    "data_types/SW-BASE-TYPE/Octet",
                    CONFLICTING,
                    own=False,
                    local=False,
                    paths=("/Acme/Left/Types/Octet", "/Acme/Right/Types/Octet"),
                ),
            ),
        ),
        reader=ReaderCounts(
            packages=8,
            path_index=7,
            sw_base_types=2,
            impl_data_types=1,
            app_primitive_data_types=0,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=0,
            skip_entries=0,
        ),
    ),
    "component_dangling_interface.arxml": Shape(
        filename="component_dangling_interface.arxml",
        subject=(
            "A component-scoped export whose one component has a port typed by an interface the "
            "file does not declare, beside a port whose interface it does. The keep-with-path is "
            "`import model`'s alone, so this pins that `import component`'s answer does not depend "
            "on it."
        ),
        oracle=OracleCounts(
            named_elements=12,
            references=4,
            unresolved=1,
            distinct_unresolved_targets=("/Vendor/Interfaces/IStatus",),
            unresolved_by_cause={CAUSE_NO_TREE: 1},
            unresolved_by_role={"REQUIRED-INTERFACE-TREF on R-PORT-PROTOTYPE": 1},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=5,
            path_index=7,
            sw_base_types=1,
            impl_data_types=1,
            app_primitive_data_types=0,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=0,
            skip_entries=0,
        ),
    ),
    # --- Application primitives, their physics and the narrowed twin. None
    # declares a name twice, so the external oracle reports no duplicate group and
    # no unresolved reference: they exercise ordering and content only.
    "bare_application_twin.arxml": Shape(
        filename="bare_application_twin.arxml",
        subject=(
            "A same-name application/implementation pair whose application side is BARE: it "
            "declares no UNIT-REF, no COMPU-METHOD-REF and no DATA-CONSTR-REF, so the two "
            "declarations say one thing and decision 6 keeps the collapse for them. The two sit "
            "in two packages, which is what makes them two declarations at all."
        ),
        oracle=OracleCounts(
            named_elements=15,
            references=5,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=7,
            path_index=8,
            sw_base_types=1,
            impl_data_types=1,
            app_primitive_data_types=1,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=0,
            skip_entries=0,
        ),
    ),
    "application_twin_with_physics.arxml": Shape(
        filename="application_twin_with_physics.arxml",
        subject=(
            "A same-name pair whose application side carries physics: a UNIT, a one-scale LINEAR "
            "COMPU-METHOD with the numerator [0, 0.1] over the denominator [1], and a DATA-CONSTR "
            "with both physical limits. The importer keeps both elements here, the application one "
            "under the declared name and the implementation one under '{name}_impl'."
        ),
        oracle=OracleCounts(
            named_elements=18,
            references=8,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=7,
            path_index=11,
            sw_base_types=1,
            impl_data_types=1,
            app_primitive_data_types=1,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=1,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=0,
            skip_entries=0,
            units=1,
            data_constrs=1,
        ),
    ),
    "map_naming_a_merged_twin_alias.arxml": Shape(
        filename="map_naming_a_merged_twin_alias.arxml",
        subject=(
            "The same-name pair of `application_twin_with_physics`, but the IMPLEMENTATION-DATA-TYPE "
            "is declared TWICE, identically, and the DATA-TYPE-MAP names the SECOND declaration -- "
            "the non-primary member of a merged group of identical declarations. The importer still keeps "
            "both members of the pair and still gives the declared name to the application type; keyed on "
            "the path the map spelled rather than on the group's primary, the twin would not be minted and "
            "the two elements would trade names, silently and with no check-model finding."
        ),
        oracle=OracleCounts(
            named_elements=18,
            references=7,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(
                _group(
                    "data_types/IMPLEMENTATION-DATA-TYPE/Speed",
                    MERGED,
                    own=True,
                    local=True,
                    paths=("/Acme/ImplTypes/Speed", "/Acme/ImplTypesCopy/Speed"),
                ),
            ),
        ),
        reader=ReaderCounts(
            packages=8,
            path_index=10,
            sw_base_types=1,
            impl_data_types=2,
            app_primitive_data_types=1,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=0,
            skip_entries=0,
            units=1,
            data_constrs=0,
        ),
    ),
    "twinned_texttable_enum.arxml": Shape(
        filename="twinned_texttable_enum.arxml",
        subject=(
            "A TEXTTABLE application primitive of three literals twinned with an implementation "
            "type of exactly its own name. Until contract 7.0.0 the twin collapse ran before the "
            "TEXTTABLE check and every literal was lost; the ordering is the defect this fixture "
            "exists for, and the enum is the element that proves it."
        ),
        oracle=OracleCounts(
            named_elements=16,
            references=6,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=7,
            path_index=9,
            sw_base_types=1,
            impl_data_types=1,
            app_primitive_data_types=1,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=1,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=0,
            skip_entries=0,
        ),
    ),
    "linear_conversion_with_offset.arxml": Shape(
        filename="linear_conversion_with_offset.arxml",
        subject=(
            "An untwinned application primitive over a LINEAR compu method whose numerator is "
            "[-40, 0.1]: offset -40 and factor 0.1, so an internal 650 is a physical 25. The "
            "offset is deliberately not zero, because a fixture whose offset were zero could not "
            "tell the two coefficients apart and that swap is malfunction class (a)."
        ),
        oracle=OracleCounts(
            named_elements=18,
            references=8,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=7,
            path_index=11,
            sw_base_types=1,
            impl_data_types=1,
            app_primitive_data_types=1,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=1,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=0,
            skip_entries=0,
            units=1,
            data_constrs=1,
        ),
    ),
    "linear_conversion_not_read.arxml": Shape(
        filename="linear_conversion_not_read.arxml",
        subject=(
            "The two LINEAR shapes decision 16 refuses to read, in one file: a scale whose "
            "COMPU-DENOMINATOR is [4], and a method of two COMPU-SCALEs. This fixture is the only "
            "evidence for them in this repository."
        ),
        oracle=OracleCounts(
            named_elements=19,
            references=9,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=5,
            path_index=14,
            sw_base_types=1,
            impl_data_types=0,
            app_primitive_data_types=2,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=2,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=0,
            skip_entries=0,
            units=2,
            data_constrs=2,
        ),
    ),
    "non_linear_compu_method.arxml": Shape(
        filename="non_linear_compu_method.arxml",
        subject=(
            "An application primitive over a RAT_FUNC compu method: neither TEXTTABLE nor LINEAR, "
            "so the conversion cannot be read at all. The type keeps its unit and its range, and "
            "the report has to name the category rather than saying only that something was "
            "dropped."
        ),
        oracle=OracleCounts(
            named_elements=14,
            references=5,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=5,
            path_index=9,
            sw_base_types=1,
            impl_data_types=0,
            app_primitive_data_types=1,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=1,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=0,
            skip_entries=0,
            units=1,
            data_constrs=1,
        ),
    ),
    "implementation_typed_data_prototype.arxml": Shape(
        filename="implementation_typed_data_prototype.arxml",
        subject=(
            "One sender-receiver interface whose two data elements are typed the two ways the "
            "standard allows: one TYPE-TREF with DEST IMPLEMENTATION-DATA-TYPE and one with DEST "
            "APPLICATION-PRIMITIVE-DATA-TYPE. The importer writes both as the export declared them, "
            "and a tool export may carry both shapes in one file."
        ),
        oracle=OracleCounts(
            named_elements=20,
            references=10,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=7,
            path_index=13,
            sw_base_types=1,
            impl_data_types=2,
            app_primitive_data_types=1,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=1,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=0,
            skip_entries=0,
            units=1,
            data_constrs=1,
        ),
    ),
    "mapping_set_named_by_every_behaviour.arxml": Shape(
        filename="mapping_set_named_by_every_behaviour.arxml",
        subject=(
            "One DATA-TYPE-MAPPING-SET and two components whose behaviours both name it -- a "
            "common shape of a tool export, a set the whole delivery shares. It is what "
            "`swc.data_type_mappings` is written from, and it is what makes "
            "`rule:data-type.application-type-unmapped` quiet on the imported project: the port "
            "speaks the physical view and a set the component names pairs it with its store."
        ),
        oracle=OracleCounts(
            named_elements=21,
            references=11,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=5,
            path_index=16,
            sw_base_types=1,
            impl_data_types=1,
            app_primitive_data_types=1,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=2,
            compositions=1,
            skip_entries=0,
            units=1,
        ),
    ),
    "mapping_set_named_like_a_data_type.arxml": Shape(
        filename="mapping_set_named_like_a_data_type.arxml",
        subject=(
            "mapping_set_named_by_every_behaviour's shape with the set's SHORT-NAME changed to the "
            "application type's, Cadence, in another package -- legal AUTOSAR, and one SHORT-NAME in "
            "SWCraft's single DataTypes package, so phase B renames the SET and has to follow the "
            "rename into every component's `data_type_mappings`. Every behaviour naming the set is "
            "the realistic shape, and it is the one whose rename crashed the import: the follow "
            "step handed the `ImportedSwc` dataclass to a Pydantic re-validation."
        ),
        oracle=OracleCounts(
            named_elements=21,
            references=11,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=5,
            path_index=16,
            sw_base_types=1,
            impl_data_types=1,
            app_primitive_data_types=1,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=2,
            compositions=1,
            skip_entries=0,
            units=1,
        ),
    ),
    "application_primitive_of_an_uncarried_category.arxml": Shape(
        filename="application_primitive_of_an_uncarried_category.arxml",
        subject=(
            "mapping_set_named_by_every_behaviour's shape with the application primitive declaring "
            "CATEGORY BOOLEAN, the standard spelling of a boolean application type, and the set "
            "renamed FlagMaps. SWCraft has no kind for the category: the type is written as an "
            "application_type and would be re-exported as VALUE, which is reported as a physics note "
            "on the written element and never as a skip entry -- the reader's skip entry it was is a "
            "non-deferred tag, and `import component` refuses every one as a lossy import."
        ),
        oracle=OracleCounts(
            named_elements=21,
            references=11,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=5,
            path_index=16,
            sw_base_types=1,
            impl_data_types=1,
            app_primitive_data_types=1,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=2,
            compositions=1,
            skip_entries=0,
            units=1,
        ),
    ),
    "application_categories_against_the_written_kind.arxml": Shape(
        filename="application_categories_against_the_written_kind.arxml",
        subject=(
            "Five application primitives on one set and one interface, each CATEGORY compared with the "
            "one the kind it is written as re-exports, VALUE for both since contract 7.5.1: a "
            "TEXTTABLE type with a TEXTTABLE method becomes an enum and a TEXTTABLE type with a LINEAR "
            "method becomes an application_type, both would be re-exported as VALUE and both are "
            "noted, while a VALUE enum, a VALUE application type and a type declaring no CATEGORY "
            "change nothing and are not noted. Up to 7.5.0 an enum re-exported as TEXTTABLE, so the "
            "VALUE enum was the noted one and the TEXTTABLE enum was not. The note was once decided "
            "by membership in a fixed set, which noted neither change. The LINEAR type carries a unit, as its "
            "compu method must, since contract 7.11.0."
        ),
        oracle=OracleCounts(
            named_elements=32,
            references=26,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=5,
            path_index=27,
            sw_base_types=1,
            impl_data_types=1,
            app_primitive_data_types=5,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=3,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=2,
            compositions=1,
            skip_entries=0,
            units=1,
        ),
    ),
    "two_behaviours_on_two_sets.arxml": Shape(
        filename="two_behaviours_on_two_sets.arxml",
        subject=(
            "Two DATA-TYPE-MAPPING-SETs, one component naming BOTH and one naming one of them. "
            "`data_type_mappings` is an ordered LIST, so a behaviour may name several sets and the "
            "export's order is the only order there is. The second component sits in a package of "
            "its own, which is what makes the per-component narrowing visible: a --packages "
            "selection over it drops the set only the other component names, AND drops the one row "
            "of the KEPT set whose application side only the other component's port speaks."
        ),
        oracle=OracleCounts(
            named_elements=30,
            references=20,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=7,
            path_index=23,
            sw_base_types=1,
            impl_data_types=2,
            app_primitive_data_types=2,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=2,
            cs_interfaces=0,
            swcs=2,
            compositions=1,
            skip_entries=0,
            units=2,
        ),
    ),
    "map_onto_a_structure_implementation.arxml": Shape(
        filename="map_onto_a_structure_implementation.arxml",
        subject=(
            "A DATA-TYPE-MAP whose implementation side is an IMPLEMENTATION-DATA-TYPE of CATEGORY "
            "STRUCTURE declaring no SUB-ELEMENTS, beside one that can be written. A structure of that "
            "shape is not imported (contract 7.8.0 imports the supported shape only), so the map is "
            "REPORTED and not written, and the set is still imported with the entry it can carry."
        ),
        oracle=OracleCounts(
            named_elements=20,
            references=11,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=5,
            path_index=15,
            sw_base_types=1,
            impl_data_types=2,
            app_primitive_data_types=1,
            app_record_data_types=1,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=1,
            skip_entries=0,
        ),
    ),
    "map_of_a_collapsed_application_type.arxml": Shape(
        filename="map_of_a_collapsed_application_type.arxml",
        subject=(
            "A DATA-TYPE-MAP whose application side decision 6 COLLAPSED onto its implementation "
            "twin, beside one whose application side carries physics and is kept whole. The "
            "collapsed pair is one element, so its map pairs a type with itself and says nothing: "
            "it is dropped beside the reduction line that explains which two declarations became "
            "one, and the other map is written."
        ),
        oracle=OracleCounts(
            named_elements=22,
            references=12,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=7,
            path_index=15,
            sw_base_types=1,
            impl_data_types=2,
            app_primitive_data_types=2,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=1,
            skip_entries=0,
            units=1,
        ),
    ),
    "identical_mapping_sets.arxml": Shape(
        filename="identical_mapping_sets.arxml",
        subject=(
            "One DATA-TYPE-MAPPING-SET name declared in two packages whose maps resolve to one "
            "pair: the merge verdict on mapping sets, in the MERGED direction. The two "
            "sets name two declarations of one application type and one implementation type, each "
            "of which merges, so comparing the maps RESOLVED agrees where comparing the paths the "
            "export spelled would refuse a pair the verdict has just called one element."
        ),
        oracle=OracleCounts(
            named_elements=19,
            references=10,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(
                _group(
                    "data_types/APPLICATION-PRIMITIVE-DATA-TYPE/Flow",
                    MERGED,
                    own=True,
                    local=True,
                    paths=("/Acme/Left/Flow", "/Acme/Right/Flow"),
                ),
                _group(
                    "data_types/IMPLEMENTATION-DATA-TYPE/RawFlow",
                    MERGED,
                    own=False,
                    local=True,
                    paths=("/Acme/Left/RawFlow", "/Acme/Right/RawFlow"),
                ),
                _group(
                    "data_types/SW-BASE-TYPE/Word",
                    MERGED,
                    own=True,
                    local=True,
                    paths=("/Acme/Left/Word", "/Acme/Right/Word"),
                ),
                _group(
                    "mapping_sets/DATA-TYPE-MAPPING-SET/Maps",
                    MERGED,
                    own=False,
                    local=True,
                    paths=("/Acme/Left/Maps", "/Acme/Right/Maps"),
                ),
            ),
        ),
        reader=ReaderCounts(
            packages=4,
            path_index=15,
            sw_base_types=2,
            impl_data_types=2,
            app_primitive_data_types=2,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=1,
            skip_entries=0,
        ),
    ),
    "differing_mapping_sets.arxml": Shape(
        filename="differing_mapping_sets.arxml",
        subject=(
            "One DATA-TYPE-MAPPING-SET name declared in two packages whose maps name two different "
            "implementation types: the merge verdict on mapping sets, in the CONFLICTING direction. The "
            "application type is a singleton, so nothing about it is in question -- what the two "
            "declarations disagree about is which implementation type stores it, which is the "
            "set's own content, and a conflicting group imports neither member."
        ),
        oracle=OracleCounts(
            named_elements=18,
            references=10,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(
                _group(
                    "mapping_sets/DATA-TYPE-MAPPING-SET/Maps",
                    CONFLICTING,
                    own=False,
                    local=False,
                    paths=("/Acme/Left/Maps", "/Acme/Right/Maps"),
                ),
            ),
        ),
        reader=ReaderCounts(
            packages=5,
            path_index=13,
            sw_base_types=1,
            impl_data_types=2,
            app_primitive_data_types=1,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=1,
            skip_entries=0,
        ),
    ),
    "unresolved_mapping_references.arxml": Shape(
        filename="unresolved_mapping_references.arxml",
        subject=(
            "The three mapping roles of a kept unresolved reference, one reference each: a DATA-TYPE-MAP's "
            "two sides and a "
            "behaviour's DATA-TYPE-MAPPING-REF, all three naming a package tree this export does "
            "not carry. Each is KEPT with the path the export spelled, so the element survives and "
            "check-model names what is missing; the set's other map resolves, which is what says "
            "an unresolvable row costs the set nothing."
        ),
        oracle=OracleCounts(
            named_elements=16,
            references=10,
            unresolved=3,
            distinct_unresolved_targets=(
                "/Elsewhere/Mappings/PlatformMaps",
                "/Elsewhere/Types/Humidity",
                "/Elsewhere/Types/RawHumidity",
            ),
            unresolved_by_cause={CAUSE_NO_TREE: 3},
            unresolved_by_role={
                "APPLICATION-DATA-TYPE-REF on DATA-TYPE-MAP": 1,
                "IMPLEMENTATION-DATA-TYPE-REF on DATA-TYPE-MAP": 1,
                "DATA-TYPE-MAPPING-REF on DATA-TYPE-MAPPING-REFS": 1,
            },
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=5,
            path_index=11,
            sw_base_types=1,
            impl_data_types=1,
            app_primitive_data_types=1,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=1,
            skip_entries=0,
        ),
    ),
    "twin_name_already_declared.arxml": Shape(
        filename="twin_name_already_declared.arxml",
        subject=(
            "A same-name pair with physics whose implementation twin would take a name the export "
            "itself declares: 'Angle' twice, once as an application primitive and once as an "
            "implementation type, beside a third declaration called 'Angle_impl'. A minted name "
            "may never evict a declared one, so the twin falls through rule 2's candidates."
        ),
        oracle=OracleCounts(
            named_elements=20,
            references=10,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=7,
            path_index=13,
            sw_base_types=1,
            impl_data_types=2,
            app_primitive_data_types=1,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=1,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=0,
            skip_entries=0,
            units=1,
            data_constrs=1,
        ),
    ),
    "map_rows_naming_a_merged_application_type.arxml": Shape(
        filename="map_rows_naming_a_merged_application_type.arxml",
        subject=(
            "One set, two DATA-TYPE-MAP rows for ONE application type after the merge: the application "
            "primitive is declared twice identically (a merged group), the first row names the "
            "non-primary member with an implementation type this export declares nowhere, the second "
            "names the primary with one it declares. The set carries one row per application type and "
            "must keep the one whose both sides resolve; keyed on the path each row spelled, the "
            "row naming the undeclared implementation type would be written and the resolvable one dropped as "
            "the duplicate."
        ),
        oracle=OracleCounts(
            named_elements=18,
            references=9,
            unresolved=1,
            distinct_unresolved_targets=("/Elsewhere/Types/RawTemp",),
            unresolved_by_cause={CAUSE_NO_TREE: 1},
            unresolved_by_role={"IMPLEMENTATION-DATA-TYPE-REF on DATA-TYPE-MAP": 1},
            shadowed={},
            groups=(
                _group(
                    "data_types/APPLICATION-PRIMITIVE-DATA-TYPE/Temperature",
                    MERGED,
                    own=True,
                    local=True,
                    paths=("/Acme/Types/Temperature", "/Acme/TypesCopy/Temperature"),
                ),
            ),
        ),
        reader=ReaderCounts(
            packages=6,
            path_index=12,
            sw_base_types=1,
            impl_data_types=1,
            app_primitive_data_types=2,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=1,
            skip_entries=0,
        ),
    ),
    "half_data_type_maps.arxml": Shape(
        filename="half_data_type_maps.arxml",
        subject=(
            "A set carrying one whole DATA-TYPE-MAP and two HALF maps, one with no "
            "IMPLEMENTATION-DATA-TYPE-REF and one with no APPLICATION-DATA-TYPE-REF. A half map pairs "
            "nothing and leaves no unresolved reference to keep with its path: it is a reader skip entry "
            "under DATA-TYPE-MAP (the two recorded below), so no declared map is lost in silence, and "
            "the set keeps its whole map."
        ),
        oracle=OracleCounts(
            named_elements=18,
            references=10,
            unresolved=0,
            distinct_unresolved_targets=(),
            unresolved_by_cause={},
            unresolved_by_role={},
            shadowed={},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=5,
            path_index=13,
            sw_base_types=1,
            impl_data_types=2,
            app_primitive_data_types=2,
            app_record_data_types=0,
            app_array_data_types=0,
            compu_methods=0,
            sr_interfaces=1,
            cs_interfaces=0,
            swcs=1,
            compositions=1,
            skip_entries=2,
        ),
    ),
    # --- What a `--packages` selection leaves out of the reduction and rename
    # sections. No name is declared twice within one kind; the one unresolved
    # reference is the external stub's type, the rename's subject.
    "packages_selection_report_lines.arxml": Shape(
        filename="packages_selection_report_lines.arxml",
        subject=(
            "Two package trees, one kept by a selection and one not. The kept tree carries one "
            "app/impl twin collapse and one rule-2 rename (an application array named like an application "
            "primitive, one layer); the other carries a second collapse, a second rule-2 rename of the same "
            "shape, and an external stub renamed because its bare name is the kept component's. An "
            "application and an implementation type of one name sit beside each array and keep the name both "
            "(two layers since contract 8.0.0). A narrowed import's report must carry the kept collapse and "
            "rename and nothing of the other tree."
        ),
        oracle=OracleCounts(
            named_elements=46,
            references=20,
            unresolved=1,
            distinct_unresolved_targets=("/Remote/Components/LevelSwc",),
            unresolved_by_cause={CAUSE_NO_TREE: 1},
            unresolved_by_role={"TYPE-TREF on SW-COMPONENT-PROTOTYPE": 1},
            shadowed={"swc": ("/Remote/Components/LevelSwc",)},
            groups=(),
        ),
        reader=ReaderCounts(
            packages=16,
            path_index=30,
            sw_base_types=1,
            impl_data_types=4,
            app_primitive_data_types=4,
            app_record_data_types=0,
            app_array_data_types=2,
            compu_methods=0,
            sr_interfaces=2,
            cs_interfaces=0,
            swcs=2,
            compositions=1,
            skip_entries=0,
        ),
    ),
}


def shape(filename: str) -> Shape:
    """The table row for *filename*, by its bare name."""
    return SHAPES[filename]


def shape_path(filename: str) -> Path:
    """The fixture's path on disk, by its bare name."""
    return SHAPES[filename].path


def read_shape_export(filename: str) -> Export:
    """Parse the fixture with the **external oracle**.

    The oracle holds the whole tree, which is why every caller on a real export sits
    behind a machine-local gate; on these files it costs nothing.
    """
    return read_export(shape_path(filename))


def read_shape_model(filename: str) -> ImportModel:
    """Parse the fixture with **SWCraft's own reader**."""
    return read_arxml(str(shape_path(filename)))


def raw_element(path: str) -> RawElement:
    """A minimal ``RawElement`` for an **inline** model's path index.

    A suite that assembles an :class:`~swcraft_tools.arxml_reader.ImportModel` by
    hand still has to fill ``path_index``, because the import policies ask it what
    the export declares. The node is empty, so a ``DEST`` read off it gets the
    unstated answer, which every caller here wants.
    """
    return RawElement(path=path, tag=path.rsplit("/", 1)[-1], node=ET.Element("X"), description="")
