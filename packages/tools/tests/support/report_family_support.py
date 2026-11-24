"""The report families of the import report, read from a skip entry's reason.

:func:`uncarried_family_counts` counts how many entries of an import report each report family holds,
by the family marks in :data:`_FAMILY_MARKS`. The synthetic import suites and the closed
real-export suites (``dev.py test-internal``) read the same counter, so one table of marks serves both.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from swcraft_tools.arxml_reader import SkipEntry

#: The reasons' marks of the report families, in the order :func:`uncarried_family_counts` asks
#: them -- each family's one record site writes its own sentence, and the first mark in this order
#: that a reason holds names its family.
_FAMILY_MARKS: tuple[tuple[str, str], ...] = (
    ("F1", "not carried: SWCraft writes NOT-ACCESSIBLE there"),
    ("F2", "COMPU-METHOD-REF of an IMPLEMENTATION-DATA-TYPE"),
    ("F5", "port on an external interface stub not carried"),
    ("F4", "SWCraft writes one com-spec per data element"),
    # A port's own value not carried ("INIT-VALUE of a com-spec not carried: ...") is F4's too,
    # whichever report section the line lands in.
    ("F4", "of a com-spec not carried"),
    ("F4", "FILTER other than"),
    ("F4", "COM-SPEC is not read by SWCraft"),
    ("F6", "SERVER-ARGUMENT-IMPL-POLICY"),
    ("F7", "APPLICATION-ERROR of a CLIENT-SERVER-INTERFACE"),
    ("F8", "POSSIBLE-ERROR-REFS of a CLIENT-SERVER-OPERATION"),
    ("F9", "CONSTR-LEVEL of a DATA-CONSTR-RULE"),
    ("F10", "SHORT-LABEL of a"),
    ("F11", "COMPU-SCALE not carried: it is not proven"),
    # F23: a LINEAR scale's limits -- an own-side infinity read as an absent bound, and what is not carried.
    ("F23", "of a LINEAR COMPU-SCALE is infinite on its own side"),
    ("F23", "of a LINEAR COMPU-SCALE not carried"),
    ("F12", "SHORT-LABEL of a value specification"),
    ("F14", "not carried exactly"),
    ("F14", "COMPU-SCALE not carried: SWCraft writes no description"),
    ("F15", "is package content SWCraft does not carry"),
    ("F16 chained", "its context port"),
    ("F16", "disabled mode"),
    ("F17", "DATA-CONSTR-REF of an IMPLEMENTATION-DATA-TYPE"),
    ("F19", "SWCraft names it itself"),
    ("F20", "data types not carried as one"),
    ("F21", "SW-IMPL-POLICY MEASUREMENT-POINT"),
    ("F22", "beside SW-IMPL-POLICY QUEUED"),
    ("F13", "not carried: SWCraft carries no "),
    ("F3", "SW-DATA-DEF-PROPS"),
    ("F3", "is not a policy SWCraft carries"),
    ("F3", "is not a value SWCraft carries"),
    ("unread attribute", "is not read by SWCraft -- not imported or preserved"),
)


def uncarried_family_counts(skip_entries: Iterable[SkipEntry]) -> dict[str, int]:
    """How many entries each report family holds.

    Read from each entry's reason, by :data:`_FAMILY_MARKS`; ``F16 chained`` is F16's half chained
    to a skipped context port. Every other entry is not counted here and is what ``SKIPPED_TOTAL``
    holds beside these. What it does **not** reach: an entry whose reason names no mark, which is
    counted by no family and still moves the total.
    """
    counts: Counter[str] = Counter()
    for entry in skip_entries:
        for family, mark in _FAMILY_MARKS:
            if mark in entry.reason and (family != "F10" or entry.path.endswith("/COMPU-INTERNAL-TO-PHYS")):
                counts[family] += 1
                break
    return dict(counts)
