"""The acceptance suite for AUTOSAR 4.3.0 output.

For the element subset SWCraft emits, 4.3.0 output differs from 4.2.2 output **only** in the ``xsi:schemaLocation``
filename token; the XML namespace is ``r4.0`` across the whole 4.x line. The claim is held on whole files with the 4.2.2
oracles: byte comparison with the token substituted and forced structural validation over the golden model roots, the
empty model and two throwaway roots; the semantic round trip over the golden model roots only. XSD conformance against
the real 4.3.0 schema is ``test_arxml_xsd_conformance.py``'s.

A genuine 4.3.0 element difference gets an entry in :data:`KNOWN_ELEMENT_DIFFERENCES` naming its schema
reason, its own named test and a relaxation of :class:`TestOnlyTheSchemaLocationDiffers`; an
unregistered one fails that class.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from conftest import ACCESS_POINTS_MODEL_ROOT, GOLDEN_MODEL_ROOTS, INIT_VALUES_MODEL_ROOT, make_empty_model_root
from swcraft_tools.arxml_validation import (
    MODE_STRUCTURAL,
    check_arxml,
)
from swcraft_tools.arxml_version import NAMESPACE, ArxmlVersion
from swcraft_tools.writer import generate_arxml

# ``TestSemanticRoundTrip`` is aliased away from the ``Test*`` name on import:
# pytest collects by name, and the imported class would be collected a second
# time *in this module* — re-running the whole 4.2.2 round trip here under a
# 4.3.0 file name.
from test_arxml_golden import TestSemanticRoundTrip as SemanticRoundTripOracle
from xsd_support import forced_structural_validation

#: The model roots every whole-file assertion here runs over. ``empty`` has no
#: committed tree (an empty directory is not a thing git tracks), so it is
#: materialized per test by ``make_empty_model_root``.
POPULATED_MODEL_ROOTS = sorted(GOLDEN_MODEL_ROOTS)

#: The throwaway init-value root, kept out of :data:`POPULATED_MODEL_ROOTS` because it is a golden of
#: nothing; it carries the constants, the three ``INIT-VALUE`` owners and the value specifications the
#: golden roots lack.
INIT_VALUES_ROOT_ID = "init-values"

#: The throwaway access-point root, for the same reason. 4.3.0 inserts an empty ``ABSTRACT-ACCESS-POINT``
#: group into ``VARIABLE-ACCESS`` and ``SYNCHRONOUS-SERVER-CALL-POINT``; this root keeps "no output
#: difference" a measured fact.
ACCESS_POINTS_ROOT_ID = "access-points"

#: Element differences between 4.2.2 and 4.3.0 output for the subset SWCraft emits. **Empty, and that is
#: the claim**: no tag SWCraft writes was renamed, reordered or re-categorized in R4.3.0. Each entry is
#: ``(description, schema reason)`` and arrives with its own named test below.
KNOWN_ELEMENT_DIFFERENCES: tuple[tuple[str, str], ...] = ()

_SCHEMA_TOKEN_4_2_2 = ArxmlVersion.V4_2_2.schema_filename
_SCHEMA_TOKEN_4_3_0 = ArxmlVersion.V4_3_0.schema_filename


def _generate_both(model_root: str, tmp_path: Path) -> tuple[str, str]:
    """Generate *model_root* at 4.2.2 and at 4.3.0; return the two texts."""
    at_4_2_2 = tmp_path / "at-4-2-2.arxml"
    at_4_3_0 = tmp_path / "at-4-3-0.arxml"
    generate_arxml(model_root, str(at_4_2_2), version=ArxmlVersion.V4_2_2)
    generate_arxml(model_root, str(at_4_3_0), version=ArxmlVersion.V4_3_0)
    return at_4_2_2.read_text(encoding="utf-8"), at_4_3_0.read_text(encoding="utf-8")


def _generate_4_3_0(model_root: str, output_path: Path) -> str:
    generate_arxml(model_root, str(output_path), version=ArxmlVersion.V4_3_0)
    return output_path.read_text(encoding="utf-8")


@pytest.fixture(params=[*POPULATED_MODEL_ROOTS, "empty", INIT_VALUES_ROOT_ID, ACCESS_POINTS_ROOT_ID])
def model_root(request, tmp_path) -> str:
    """Every model shape the goldens cover, the empty one, and the two throwaway roots."""
    if request.param == "empty":
        return str(make_empty_model_root(tmp_path))
    if request.param == INIT_VALUES_ROOT_ID:
        return str(INIT_VALUES_MODEL_ROOT)
    if request.param == ACCESS_POINTS_ROOT_ID:
        return str(ACCESS_POINTS_MODEL_ROOT)
    return str(GOLDEN_MODEL_ROOTS[request.param])


# The declared schema, and the namespace that does *not* change with it


class TestSchemaDeclaration:
    def test_the_root_declares_the_4_3_0_schema_and_the_r4_0_namespace(self, model_root, tmp_path) -> None:
        text = _generate_4_3_0(model_root, tmp_path / "model.arxml")
        assert text.splitlines()[0] == '<?xml version="1.0" encoding="utf-8"?>'
        assert text.splitlines()[1] == (
            '<AUTOSAR xmlns="http://autosar.org/schema/r4.0" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-3-0.xsd">'
        )

    def test_the_parsed_root_is_still_in_the_r4_0_namespace(self, model_root, tmp_path) -> None:
        """Asserted on the parsed tree, not the source line: it is what a reader sees."""
        root = ET.fromstring(_generate_4_3_0(model_root, tmp_path / "model.arxml"))
        assert root.tag == f"{{{NAMESPACE}}}AUTOSAR"

    def test_the_4_2_2_schema_filename_appears_nowhere(self, model_root, tmp_path) -> None:
        assert _SCHEMA_TOKEN_4_2_2 not in _generate_4_3_0(model_root, tmp_path / "model.arxml")

    def test_the_schema_filename_appears_exactly_once(self, model_root, tmp_path) -> None:
        """Guards the substitution: a second occurrence of the token would hide a real difference."""
        assert _generate_4_3_0(model_root, tmp_path / "model.arxml").count(_SCHEMA_TOKEN_4_3_0) == 1


# The claim: only the schema location differs


class TestOnlyTheSchemaLocationDiffers:
    """Byte equality after substituting the one token, in both directions, whole-file."""

    def test_the_registry_of_element_differences_is_empty(self) -> None:
        """The registry of element differences is empty, so none is registered and left untested."""
        assert KNOWN_ELEMENT_DIFFERENCES == ()

    def test_4_3_0_output_is_4_2_2_output_with_the_schema_token_substituted(self, model_root, tmp_path) -> None:
        at_4_2_2, at_4_3_0 = _generate_both(model_root, tmp_path)
        assert at_4_3_0 == at_4_2_2.replace(_SCHEMA_TOKEN_4_2_2, _SCHEMA_TOKEN_4_3_0)

    def test_the_two_versions_do_produce_different_bytes(self, model_root, tmp_path) -> None:
        """Otherwise the substitution test above would pass on a writer that ignores the version."""
        at_4_2_2, at_4_3_0 = _generate_both(model_root, tmp_path)
        assert at_4_3_0 != at_4_2_2

    def test_exactly_one_line_differs_and_it_is_the_root_element(self, model_root, tmp_path) -> None:
        """The one differing line is line 2, the ``AUTOSAR`` root element."""
        at_4_2_2, at_4_3_0 = _generate_both(model_root, tmp_path)
        lines_4_2_2, lines_4_3_0 = at_4_2_2.splitlines(), at_4_3_0.splitlines()
        assert len(lines_4_2_2) == len(lines_4_3_0)
        differing = [index for index, (a, b) in enumerate(zip(lines_4_2_2, lines_4_3_0, strict=True)) if a != b]
        assert differing == [1]
        assert lines_4_3_0[1].startswith("<AUTOSAR ")

    def test_the_element_trees_are_identical_apart_from_the_root_attribute(self, model_root, tmp_path) -> None:
        """The same claim on the parsed tree: every tag, order, text and attribute."""
        at_4_2_2, at_4_3_0 = _generate_both(model_root, tmp_path)

        def shape(text: str) -> list[tuple[str, str | None, tuple[tuple[str, str], ...]]]:
            root = ET.fromstring(text)
            return [
                (
                    element.tag,
                    element.text,
                    tuple(sorted(element.attrib.items())) if element is not root else (),
                )
                for element in root.iter()
            ]

        assert shape(at_4_3_0) == shape(at_4_2_2)


# Validation, determinism and the semantic round trip — the 4.2.2 oracles, at 4.3.0


class TestValidation:
    """The version is inferred from the file, with structural mode forced so every machine asserts the same.

    XSD conformance of the 4.3.0 output is ``test_arxml_xsd_conformance.py``'s.
    """

    def test_generated_4_3_0_output_validates(self, model_root, tmp_path, monkeypatch) -> None:
        output = tmp_path / "model.arxml"
        generate_arxml(model_root, str(output), version=ArxmlVersion.V4_3_0)
        forced_structural_validation(monkeypatch)
        outcome = check_arxml(str(output))
        assert outcome.mode == MODE_STRUCTURAL
        assert outcome.valid is True

    def test_the_version_is_inferred_from_the_generated_file(self, model_root, tmp_path, monkeypatch) -> None:
        forced_structural_validation(monkeypatch)
        output = tmp_path / "model.arxml"
        generate_arxml(model_root, str(output), version=ArxmlVersion.V4_3_0)
        assert check_arxml(str(output)).version is ArxmlVersion.V4_3_0


class TestDeterminism:
    """The version introduces no source of variation."""

    def test_two_generations_are_byte_identical(self, model_root, tmp_path) -> None:
        first = tmp_path / "first.arxml"
        second = tmp_path / "second.arxml"
        generate_arxml(model_root, str(first), version=ArxmlVersion.V4_3_0)
        generate_arxml(model_root, str(second), version=ArxmlVersion.V4_3_0)
        assert first.read_bytes() == second.read_bytes()

    def test_a_4_2_2_generation_between_them_changes_nothing(self, model_root, tmp_path) -> None:
        """The version is a per-call argument, not writer state left behind by a 4.2.2 run."""
        first = tmp_path / "first.arxml"
        between = tmp_path / "between.arxml"
        second = tmp_path / "second.arxml"
        generate_arxml(model_root, str(first), version=ArxmlVersion.V4_3_0)
        generate_arxml(model_root, str(between), version=ArxmlVersion.V4_2_2)
        generate_arxml(model_root, str(second), version=ArxmlVersion.V4_3_0)
        assert first.read_bytes() == second.read_bytes()
        assert _SCHEMA_TOKEN_4_2_2 in between.read_text(encoding="utf-8")


class TestSemanticRoundTripAt4_3_0(SemanticRoundTripOracle):
    """The 4.2.2 semantic round trip against 4.3.0 output; a subclass so the 4.3.0 bar cannot drift.

    The importer gets no version hint and reads the file on the unchanged ``r4.0`` namespace alone.
    """

    @pytest.fixture(params=POPULATED_MODEL_ROOTS)
    def round_tripped(self, request, tmp_path):
        model_root = str(GOLDEN_MODEL_ROOTS[request.param])
        output = tmp_path / "model.arxml"
        generate_arxml(model_root, str(output), version=ArxmlVersion.V4_3_0)
        return model_root, self._round_trip(str(output))
