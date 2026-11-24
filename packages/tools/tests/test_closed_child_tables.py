"""The reader's closed child tables against both schemas.

Every covered owner has one closed table in :mod:`swcraft_tools.arxml_reader`: each child element and attribute
the 4.2.2 and 4.3.0 schemas admit is *read*, carried by *equivalence*, reported by the index (*index*), reported
under a named family (:data:`LIVE_FAMILIES`), or reported as *unread* by the closed tables' own walker. The
expectation is ``fixtures/xsd_child_sets.json``, the union of what the two licensed schemas admit for each
owner, expanded through their groups. The schemas are not committed, so ``test_arxml_xsd_conformance.py``'s
``TestTheChildSetsAreTheSchemas`` re-derives the JSON in the ``test-xsd`` suite, and this fast module holds the
tables against the JSON.

What this does not reach: an owner not in the JSON (its unread children are what other closed tables, a
behaviour's and a runnable's, and the index report), and whether a table's classification is *right*
(``test_uncarried_losses.py``, on hand-built files).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from swcraft_tools.arxml_reader import (
    _IDENTIFIABLE_TABLE,
    CLOSED_CHILD_TABLES,
    CONDITIONAL_TABLES,
    VARIATION_POINT_READ_OWNERS,
)

CHILD_SETS_PATH = Path(__file__).parent / "fixtures" / "xsd_child_sets.json"
CHILD_SETS = json.loads(CHILD_SETS_PATH.read_text(encoding="utf-8"))

#: The covered owners, as the JSON keys them.
COVERED_OWNERS = frozenset(
    {
        "VARIABLE-DATA-PROTOTYPE",
        "ARGUMENT-DATA-PROTOTYPE",
        "IMPLEMENTATION-DATA-TYPE",
        "IMPLEMENTATION-DATA-TYPE-ELEMENT",
        "SW-DATA-DEF-PROPS-CONDITIONAL",
        "NONQUEUED-SENDER-COM-SPEC",
        "NONQUEUED-RECEIVER-COM-SPEC",
        "QUEUED-SENDER-COM-SPEC",
        "QUEUED-RECEIVER-COM-SPEC",
        "SERVER-COM-SPEC",
        "CLIENT-COM-SPEC",
        "DATA-FILTER",
        "SENDER-RECEIVER-INTERFACE",
        "CLIENT-SERVER-INTERFACE",
        "TIMING-EVENT",
        "R-MODE-IN-ATOMIC-SWC-INSTANCE-REF",
        "DATA-CONSTR-RULE",
        "COMPU-SCALE",
        "AR-PACKAGE",
        "DESC",
        "L-2",
        "LONG-NAME",
        "L-4",
        # The two owners an execution-time budget is read from.
        "SWC-TIMING",
        "EXECUTION-TIME-CONSTRAINT",
        # The data-received event and its DATA-IREF.
        "DATA-RECEIVED-EVENT",
        "R-VARIABLE-IN-ATOMIC-SWC-INSTANCE-REF",
    }
)
#: Every ``Identifiable``'s documentation group, as the JSON keys the three schema groups.
IDENTIFIABLE_GROUPS = ("group REFERRABLE", "group MULTILANGUAGE-REFERRABLE", "group IDENTIFIABLE")
#: The live families (F18 is retired), F23 being a ``LINEAR`` scale's limits.
LIVE_FAMILIES = frozenset(f"F{n}" for n in (*range(1, 18), *range(19, 24)))
#: The classifications a row may take, one or an ``a|b`` choice the reading site decides.
CLASSIFICATIONS = frozenset({"read", "equivalent", "index", "unread"}) | LIVE_FAMILIES
#: The owners whose table is :data:`CLOSED_CHILD_TABLES`'s; the conditional has one per context.
TABLE_OWNERS = COVERED_OWNERS - {"SW-DATA-DEF-PROPS-CONDITIONAL"}


def _admitted(owner: str) -> set[str]:
    sets = CHILD_SETS[owner]
    return set(sets.get("elements", {})) | {f"@{name}" for name in sets.get("attributes", {})}


def _tokens(row: str) -> set[str]:
    return set(row.split("|"))


class TestTheChildSetsJson:
    def test_it_holds_every_owner_and_the_three_groups_and_nothing_else(self) -> None:
        assert set(CHILD_SETS) == COVERED_OWNERS | set(IDENTIFIABLE_GROUPS)

    def test_every_owner_was_found_in_both_schemas(self) -> None:
        assert [owner for owner, sets in CHILD_SETS.items() if "missing-type" in sets] == []

    def test_the_four_version_specific_children_are_4_3_0_only(self) -> None:
        """Four admitted children are in the 4.3.0 schema alone; every other one is in both."""
        only_one = {
            (owner, child, tuple(versions))
            for owner, sets in CHILD_SETS.items()
            for kind in ("elements", "attributes")
            for child, versions in sets.get(kind, {}).items()
            if versions != ["4.2.2", "4.3.0"]
        }
        assert only_one == {
            ("NONQUEUED-RECEIVER-COM-SPEC", "TIMEOUT-SUBSTITUTION-VALUE", ("4.3.0",)),
            ("SWC-TIMING", "TIMING-CONDITIONS", ("4.3.0",)),
            ("SWC-TIMING", "TIMING-RESOURCE", ("4.3.0",)),
            ("EXECUTION-TIME-CONSTRAINT", "TIMING-CONDITION-REF", ("4.3.0",)),
        }


class TestEveryTableIsClosed:
    @pytest.mark.parametrize("owner", sorted(TABLE_OWNERS))
    def test_every_admitted_child_and_attribute_has_a_row(self, owner: str) -> None:
        assert sorted(_admitted(owner) - set(CLOSED_CHILD_TABLES[owner])) == []

    @pytest.mark.parametrize("owner", sorted(TABLE_OWNERS))
    def test_no_row_names_what_the_schemas_do_not_admit(self, owner: str) -> None:
        assert sorted(set(CLOSED_CHILD_TABLES[owner]) - _admitted(owner)) == []

    @pytest.mark.parametrize("owner", sorted(TABLE_OWNERS))
    def test_every_row_is_read_equivalent_the_index_a_live_family_or_unread(self, owner: str) -> None:
        bad = {child: row for child, row in CLOSED_CHILD_TABLES[owner].items() if not _tokens(row) <= CLASSIFICATIONS}
        assert bad == {}

    def test_the_tables_are_the_owners_and_nothing_else(self) -> None:
        assert set(CLOSED_CHILD_TABLES) == TABLE_OWNERS


class TestTheConditionalTables:
    """``SW-DATA-DEF-PROPS-CONDITIONAL``, one table per owner it is read on.

    A child or attribute a table does not name is family F3 on the four data-definition owners and the
    application primitive's own site's on that one; closed by that default, so only the named rows are pinned.
    """

    def test_the_contexts_are_the_four_owners_and_the_application_primitive(self) -> None:
        assert set(CONDITIONAL_TABLES) == {
            "VARIABLE-DATA-PROTOTYPE",
            "ARGUMENT-DATA-PROTOTYPE",
            "IMPLEMENTATION-DATA-TYPE",
            "IMPLEMENTATION-DATA-TYPE-ELEMENT",
            "APPLICATION-PRIMITIVE-DATA-TYPE",
        }

    @pytest.mark.parametrize("context", sorted(CONDITIONAL_TABLES))
    def test_every_row_is_an_admitted_child(self, context: str) -> None:
        assert sorted(set(CONDITIONAL_TABLES[context]) - _admitted("SW-DATA-DEF-PROPS-CONDITIONAL")) == []

    @pytest.mark.parametrize("context", sorted(CONDITIONAL_TABLES))
    def test_every_row_is_a_known_classification(self, context: str) -> None:
        bad = {child: row for child, row in CONDITIONAL_TABLES[context].items() if not _tokens(row) <= CLASSIFICATIONS}
        assert bad == {}

    def test_the_families_the_plan_names_for_a_data_definition_are_the_rows(self) -> None:
        """F1 (argument, implementation type, element), F2 and F17 (implementation type), F21 and F22 (data element)."""
        rows = {(context, child): row for context, table in CONDITIONAL_TABLES.items() for child, row in table.items()}
        assert rows[("ARGUMENT-DATA-PROTOTYPE", "SW-CALIBRATION-ACCESS")] == "equivalent|F1"
        assert rows[("IMPLEMENTATION-DATA-TYPE", "SW-CALIBRATION-ACCESS")] == "equivalent|F1"
        assert rows[("IMPLEMENTATION-DATA-TYPE-ELEMENT", "SW-CALIBRATION-ACCESS")] == "equivalent|F1"
        assert rows[("IMPLEMENTATION-DATA-TYPE", "COMPU-METHOD-REF")] == "F2"
        # Read on a VALUE type, F17 on any other category.
        assert rows[("IMPLEMENTATION-DATA-TYPE", "DATA-CONSTR-REF")] == "read|F17"
        assert rows[("VARIABLE-DATA-PROTOTYPE", "SW-IMPL-POLICY")] == "read|F21|F3"
        assert rows[("VARIABLE-DATA-PROTOTYPE", "SW-CALIBRATION-ACCESS")] == "read|F22|F3"


class TestTheDocumentationGroup:
    @pytest.mark.parametrize("group", IDENTIFIABLE_GROUPS)
    def test_every_member_of_the_three_groups_has_a_row_or_is_the_owners(self, group: str) -> None:
        """``CATEGORY`` is the one member each owner classifies itself: read on a type, unread elsewhere."""
        assert sorted(set(CHILD_SETS[group]["elements"]) - set(_IDENTIFIABLE_TABLE) - {"CATEGORY"}) == []

    @pytest.mark.parametrize(
        "owner", sorted(owner for owner in TABLE_OWNERS if "UUID" in CHILD_SETS[owner].get("attributes", {}))
    )
    def test_every_identifiable_owner_carries_the_group_as_the_group_table_says(self, owner: str) -> None:
        """An ``AR-PACKAGE``'s documentation is package content (F15), every other owner's the group's row.

        Except, by name, a ``VARIATION-POINT`` read by its own owner: a budget's two owners (each a skip with its
        own reason in the mapper) and a data-received event (one loss line at the event).
        """
        table = CLOSED_CHILD_TABLES[owner]
        for child, row in _IDENTIFIABLE_TABLE.items():
            if owner == "AR-PACKAGE":
                assert table[child] == ("read" if child == "SHORT-NAME" else "F15")
            elif child == "VARIATION-POINT" and owner in VARIATION_POINT_READ_OWNERS:
                assert table[child] == "read"
            else:
                assert table[child] == row

    def test_the_owners_reading_their_variation_point_are_the_budgets_two_and_the_data_event(self) -> None:
        assert VARIATION_POINT_READ_OWNERS == {"SWC-TIMING", "EXECUTION-TIME-CONSTRAINT", "DATA-RECEIVED-EVENT"}
        assert {owner for owner in TABLE_OWNERS if CLOSED_CHILD_TABLES[owner].get("VARIATION-POINT") == "read"} == (
            VARIATION_POINT_READ_OWNERS
        )
