"""The import shape corpus guards itself.

``fixtures/arxml_import_shapes/`` is the input of the importer's shape tests, and
``arxml_import_shapes_support.SHAPES`` says what each file holds. This module pins the directory as a set,
re-takes every recorded figure with both parsers on every run, and asserts that the corpus covers the
shapes the importer's tests need a subject for.

What this module does **not** reach:

* the importer: nothing runs ``run_import``; this suite only says what the fixtures are;
* whether the oracle is right: the figures are one parser's answers, not the standard's
  (``test_arxml_export_oracle.py`` argues the oracle's semantics);
* schema conformance: no fixture is validated against an AUTOSAR XSD, and
  ``cyclic_composition_graph.arxml`` is malformed on purpose;
* what the corpus leaves out, enumerated in ``arxml_import_shapes_support``'s module docstring -- scale,
  groups larger than two, duplicate absolute paths, case-only pairs and cross-kind names.
"""

from __future__ import annotations

import pytest
from arxml_export_oracle import (
    CAUSE_CUT_PART_WAY,
    CAUSE_LEAF_ONLY,
    CAUSE_NO_TREE,
    CONFLICTING,
    MERGED,
    dangling_references,
    duplicate_groups,
)
from arxml_import_shapes_support import (
    SHAPES,
    SHAPES_DIR,
    Shape,
    read_shape_export,
    read_shape_model,
)

FIXTURE_NAMES = sorted(SHAPES)


class TestTheTableIsTheDirectory:
    """The set is pinned in both directions."""

    def test_every_file_in_the_directory_has_a_row(self) -> None:
        on_disk = {path.name for path in SHAPES_DIR.glob("*.arxml")}

        assert on_disk == set(SHAPES), (
            "every fixture under arxml_import_shapes/ must be named in "
            "arxml_import_shapes_support.SHAPES: a file with no row has no integrity guard, "
            "and a row with no file is a subject nothing exercises"
        )

    def test_the_directory_holds_nothing_but_arxml(self) -> None:
        """The corpus is ARXML only, beside the one provenance ``README.md`` every ``.arxml`` fixture folder carries."""
        strays = sorted(
            path.name for path in SHAPES_DIR.iterdir() if path.suffix != ".arxml" and path.name != "README.md"
        )

        assert strays == [], f"the shape corpus is ARXML only; found {strays}"

    @pytest.mark.parametrize("name", FIXTURE_NAMES)
    def test_each_rows_key_is_its_own_filename(self, name: str) -> None:
        assert SHAPES[name].filename == name

    @pytest.mark.parametrize("name", FIXTURE_NAMES)
    def test_each_row_says_what_its_fixture_is_for(self, name: str) -> None:
        """A row with no subject is a file nobody can decide whether to edit."""
        subject = SHAPES[name].subject

        assert len(subject) > 80, f"{name}: the subject must say what shape the file exists for"

    @pytest.mark.parametrize("name", FIXTURE_NAMES)
    def test_each_fixture_says_its_names_are_invented(self, name: str) -> None:
        """Every file carries the claim that its names are invented; only the claim's presence is checked."""
        # Whitespace is collapsed because the claim is prose in a wrapped XML
        # comment: one of the fixtures qualifies it over a line break.
        text = " ".join(SHAPES[name].path.read_text(encoding="utf-8").split())

        assert "Invented names only" in text
        assert "no content copied from any real export" in text.lower()


class TestEveryFixtureParsesWithTheExternalOracle:
    """The recorded oracle figures, re-taken on every run."""

    @pytest.mark.parametrize("name", FIXTURE_NAMES)
    def test_the_path_index_and_the_reference_count(self, name: str) -> None:
        shape = SHAPES[name]
        export = read_shape_export(name)

        assert export.named_element_count == shape.oracle.named_elements
        assert len(export.references) == shape.oracle.references
        assert export.duplicate_paths == [], "no fixture carries a duplicate absolute path"

    @pytest.mark.parametrize("name", FIXTURE_NAMES)
    def test_the_unresolved_references(self, name: str) -> None:
        shape = SHAPES[name]
        dangling = dangling_references(read_shape_export(name))

        assert len(dangling.unresolved) == shape.oracle.unresolved
        assert tuple(sorted(dangling.distinct_targets)) == shape.oracle.distinct_unresolved_targets
        assert dict(dangling.by_cause) == shape.oracle.unresolved_by_cause
        assert dict(dangling.by_role) == shape.oracle.unresolved_by_role
        assert {layer: tuple(sorted(targets)) for layer, targets in dangling.shadowed.items()} == shape.oracle.shadowed

    @pytest.mark.parametrize("name", FIXTURE_NAMES)
    def test_the_duplicate_groups_and_their_verdicts(self, name: str) -> None:
        shape = SHAPES[name]
        groups = duplicate_groups(read_shape_export(name))

        measured = tuple(
            (
                group.key,
                group.verdict,
                group.own_bytes_identical,
                group.locally_equal,
                group.cyclic,
                group.inherited_from,
                tuple(group.paths),
            )
            for group in groups
        )
        expected = tuple(
            (
                group.key,
                group.verdict,
                group.own_bytes_identical,
                group.locally_equal,
                group.cyclic,
                group.inherited_from,
                group.paths,
            )
            for group in shape.oracle.groups
        )

        assert measured == expected


class TestEveryFixtureParsesWithTheReader:
    """``read_arxml`` opens every fixture and reads something out of it, since "it parses" alone is vacuous."""

    @pytest.mark.parametrize("name", FIXTURE_NAMES)
    def test_the_reader_finds_what_the_table_records(self, name: str) -> None:
        shape = SHAPES[name]
        model = read_shape_model(name)
        expected = shape.reader

        assert len(model.packages) == expected.packages
        assert len(model.path_index) == expected.path_index
        assert len(model.sw_base_types) == expected.sw_base_types
        assert len(model.impl_data_types) == expected.impl_data_types
        assert len(model.app_primitive_data_types) == expected.app_primitive_data_types
        assert len(model.app_record_data_types) == expected.app_record_data_types
        assert len(model.app_array_data_types) == expected.app_array_data_types
        assert len(model.compu_methods) == expected.compu_methods
        assert len(model.units) == expected.units
        assert len(model.data_constrs) == expected.data_constrs
        assert len(model.sr_interfaces) == expected.sr_interfaces
        assert len(model.cs_interfaces) == expected.cs_interfaces
        assert len(model.swcs) == expected.swcs
        assert len(model.compositions) == expected.compositions
        assert len(model.skip_log.entries) == expected.skip_entries

    @pytest.mark.parametrize("name", FIXTURE_NAMES)
    def test_no_fixture_is_empty_to_the_reader(self, name: str) -> None:
        expected = SHAPES[name].reader
        typed = (
            expected.sw_base_types
            + expected.impl_data_types
            + expected.app_primitive_data_types
            + expected.app_record_data_types
            + expected.app_array_data_types
            + expected.compu_methods
            + expected.sr_interfaces
            + expected.cs_interfaces
            + expected.swcs
            + expected.compositions
        )

        assert typed > 0, f"{name}: the reader extracts no element at all, so no task can use it"


class TestTheCorpusCoversTheShapesTheLaterTasksNeed:
    """Each shape listed below has at least one subject; whether a covered shape is covered well is not checked."""

    def _with_groups(self, predicate) -> list[Shape]:
        return [shape for shape in SHAPES.values() if any(predicate(group) for group in shape.oracle.groups)]

    def test_a_merge_and_a_conflict_both_have_a_subject(self) -> None:
        assert self._with_groups(lambda group: group.verdict == MERGED)
        assert self._with_groups(lambda group: group.verdict == CONFLICTING)

    def test_an_identical_pair_and_a_path_only_pair_are_both_present(self) -> None:
        """The two ways a group can merge: byte-identical, and equal only once its references are resolved."""
        assert self._with_groups(lambda group: group.verdict == MERGED and group.own_bytes_identical)
        assert self._with_groups(lambda group: group.verdict == MERGED and not group.own_bytes_identical)

    def test_a_direct_conflict_and_an_inherited_one_are_both_present(self) -> None:
        assert self._with_groups(lambda group: group.verdict == CONFLICTING and not group.locally_equal)
        assert self._with_groups(lambda group: group.inherited_from is not None)

    def test_a_cyclic_group_is_present(self) -> None:
        assert self._with_groups(lambda group: group.cyclic)

    def test_the_three_level_case_answers_differently_the_two_ways(self) -> None:
        """Three merges, where a declaration-local comparison would give one merge and two conflicts."""
        shape = SHAPES["three_level_fixpoint.arxml"]

        assert len(shape.oracle.groups) == 3
        assert shape.oracle.merged == 3
        assert shape.oracle.own_bytes_identical == 1

    def test_every_cause_of_an_unresolved_reference_has_a_subject(self) -> None:
        causes = {cause for shape in SHAPES.values() for cause in shape.oracle.unresolved_by_cause}

        assert causes == {CAUSE_NO_TREE, CAUSE_LEAF_ONLY, CAUSE_CUT_PART_WAY}

    def test_every_role_the_design_keeps_has_a_subject(self) -> None:
        """One unresolved reference per role the importer keeps.

        A component port's interface and a composition boundary port's share one role string.
        """
        roles = {role for shape in SHAPES.values() for role in shape.oracle.unresolved_by_role}

        assert {
            "BASE-TYPE-REF on SW-DATA-DEF-PROPS-CONDITIONAL",
            "IMPLEMENTATION-DATA-TYPE-REF on SW-DATA-DEF-PROPS-CONDITIONAL",
            "PROVIDED-INTERFACE-TREF on P-PORT-PROTOTYPE",
            "REQUIRED-INTERFACE-TREF on R-PORT-PROTOTYPE",
            "TYPE-TREF on APPLICATION-RECORD-ELEMENT",
            "TYPE-TREF on ARGUMENT-DATA-PROTOTYPE",
            "TYPE-TREF on ELEMENT",
            "TYPE-TREF on SW-COMPONENT-PROTOTYPE",
            "TYPE-TREF on VARIABLE-DATA-PROTOTYPE",
        } <= roles

    def test_a_shadowed_target_has_a_subject(self) -> None:
        assert [shape for shape in SHAPES.values() if shape.oracle.shadowed]

    def test_the_two_import_component_fixtures_carry_exactly_one_component(self) -> None:
        """Each of the two import-component fixtures carries exactly one component.

        ``import component`` refuses an export with none or several, so a fixture with another count would test that
        guard instead of the import.
        """
        for name in ("component_same_name_pair.arxml", "component_dangling_interface.arxml"):
            reader = SHAPES[name].reader

            assert reader.swcs == 1, f"{name}: import component takes exactly one component"
            assert reader.compositions == 0, f"{name}: import component takes no composition content"

    def test_the_same_name_base_and_implementation_pair_is_not_a_group(self) -> None:
        """A CROSS-KIND collision, which the merge verdict never judges."""
        shape = SHAPES["same_name_base_and_implementation.arxml"]

        assert shape.oracle.groups == ()
        assert shape.reader.sw_base_types == 1
        assert shape.reader.impl_data_types == 1
