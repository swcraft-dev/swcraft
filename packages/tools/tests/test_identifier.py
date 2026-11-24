"""The AUTOSAR ``Identifier`` rule.

Every name SWCraft emits into ARXML, as a ``SHORT-NAME`` or a ``*-REF`` path segment, must be an
AUTOSAR ``Identifier``: ``[a-zA-Z][a-zA-Z0-9_]*`` with at most 128 characters. This module pins
the rule itself; ``test_arxml_xsd_conformance.py`` pins it against the licensed schemas, its only
real oracle.

Three properties get their own coverage:

* **The match is a ``fullmatch``, not ``re.match`` against ``^…$``**: ``$`` matches before a
  trailing newline, so ``"A\\n"`` would slip through. The constant is stored unanchored, as the
  schema's ``pattern`` facet is, and the anchoring lives in the call.
* **The reason distinguishes pattern from length**: they need different advice, and a
  *generated* suffix (``_InternalBehavior``, 17 characters) can overflow a declared name well
  inside the limit.
* **This is not ``Sequence.name``'s rule**: ``schema.py``'s ``_SEQUENCE_NAME_RE`` allows a
  leading underscore and AUTOSAR does not; the two must stay visibly distinct.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from swcraft_tools import identifier as identifier_module
from swcraft_tools.identifier import (
    C_IDENTIFIER_PATTERN,
    C_IDENTIFIER_RE,
    IDENTIFIER_PATTERN,
    IDENTIFIER_RE,
    MAX_IDENTIFIER_LENGTH,
    REASON_C_IDENTIFIER,
    REASON_LENGTH,
    REASON_PATTERN,
    check_c_identifier,
    check_identifier,
    short_name_key,
)
from swcraft_tools.schema import _SEQUENCE_NAME_RE

MODULE_PATH = Path(identifier_module.__file__)


class TestConformingNames:
    """The legal side of the rule, including its length boundary."""

    @pytest.mark.parametrize(
        "name",
        [
            "A",
            "a",
            "Speed",
            "ISpeed",
            "uint16",
            "uint16_impl",
            "TMT_Run_0",
            "A1",
            "A_",
            "A__B",
            "aB9_c",
        ],
    )
    def test_accepts_a_conforming_name(self, name):
        assert check_identifier(name) is None

    def test_accepts_a_name_at_the_length_limit(self):
        # The boundary on the legal side: 128 is allowed, 129 is not.
        assert check_identifier("A" * MAX_IDENTIFIER_LENGTH) is None

    def test_max_length_is_128(self):
        assert MAX_IDENTIFIER_LENGTH == 128


class TestNonConformingNames:
    """The reproduction set, each shape a real model or a real export can produce."""

    @pytest.mark.parametrize(
        ("name", "why"),
        [
            ("1Speed", "leading digit"),
            ("_Speed", "leading underscore"),
            ("My SWC", "space"),
            ("My-SWC", "hyphen"),
            ("My.SWC", "dot"),
            ("", "empty"),
            ("Speed/Vehicle", "path separator"),
            ("Speed:1", "colon"),
            ("Speed$", "dollar"),
            (" Speed", "leading space"),
            ("Speed ", "trailing space"),
        ],
    )
    def test_rejects_a_non_conforming_name(self, name, why):
        assert check_identifier(name) == REASON_PATTERN, why

    def test_rejects_a_name_one_over_the_length_limit(self):
        assert check_identifier("A" * (MAX_IDENTIFIER_LENGTH + 1)) == REASON_LENGTH


class TestAnchoring:
    """``fullmatch``, not ``re.match`` against ``^…$`` — the newline hole."""

    def test_pattern_constant_is_stored_unanchored(self):
        # Compared literally with the schemas' ``pattern`` facet, which carries no anchors:
        # XSD patterns are implicitly whole-value.
        assert IDENTIFIER_PATTERN == "[a-zA-Z][a-zA-Z0-9_]*"
        assert IDENTIFIER_RE.pattern == IDENTIFIER_PATTERN
        assert not IDENTIFIER_PATTERN.startswith("^")
        assert not IDENTIFIER_PATTERN.endswith("$")

    def test_a_prefix_match_is_not_enough(self):
        # The unanchored regex happily matches the legal prefix of an illegal
        # name; only the ``fullmatch`` in ``check_identifier`` rejects it.
        assert IDENTIFIER_RE.match("A B") is not None
        assert check_identifier("A B") == REASON_PATTERN

    @pytest.mark.parametrize(
        ("name", "why"),
        [
            ("A\n", "trailing newline — the one ``$`` would let through"),
            ("A\nB", "embedded newline"),
            ("A\r", "trailing carriage return"),
            ("A\tB", "tab"),
            ("A\x00B", "NUL control character"),
            ("A\x07", "BEL control character"),
        ],
    )
    def test_rejects_whitespace_and_control_characters(self, name, why):
        assert check_identifier(name) == REASON_PATTERN, why

    @pytest.mark.parametrize("name", ["Ärger", "naïve", "Скорость", "速度", "Aµ", "A²"])
    def test_rejects_non_ascii_letters(self, name):
        # ``[a-zA-Z]`` is ASCII by construction; a Unicode-aware reading of
        # "letter" would accept names the schema rejects.
        assert check_identifier(name) == REASON_PATTERN


class TestTheReasonDistinguishesPatternFromLength:
    """Two failures, two pieces of advice — and the length one can be generated."""

    def test_the_two_reasons_differ(self):
        assert REASON_PATTERN != REASON_LENGTH

    def test_the_pattern_reason_names_the_pattern(self):
        assert IDENTIFIER_PATTERN in REASON_PATTERN

    def test_the_length_reason_names_the_limit(self):
        assert str(MAX_IDENTIFIER_LENGTH) in REASON_LENGTH

    def test_a_long_but_well_formed_name_fails_on_length_only(self):
        assert check_identifier("A" * 200) == REASON_LENGTH

    def test_a_short_but_ill_formed_name_fails_on_pattern_only(self):
        assert check_identifier("My SWC") == REASON_PATTERN

    def test_a_name_failing_both_reports_the_pattern(self):
        # Deliberate ordering: the pattern failure is the one the human acts on
        # first — a name shortened but still holding a space is still refused.
        assert check_identifier("My SWC " + "A" * 200) == REASON_PATTERN

    def test_a_generated_form_can_fail_where_the_declared_name_passes(self):
        # A 112-character SWC name yields a legal ``_Implementation`` (127) and an illegal
        # ``_InternalBehavior`` (129).
        declared = "A" * 112
        assert check_identifier(declared) is None
        assert check_identifier(declared + "_Implementation") is None
        assert check_identifier(declared + "_InternalBehavior") == REASON_LENGTH


class TestThisIsNotTheSequenceNameRule:
    """``Sequence.name`` keeps its own, deliberately different, rule."""

    def test_a_leading_underscore_is_legal_for_a_sequence_name_and_illegal_here(self):
        assert _SEQUENCE_NAME_RE.match("_view") is not None
        assert check_identifier("_view") == REASON_PATTERN

    def test_the_two_patterns_are_not_the_same_expression(self):
        assert _SEQUENCE_NAME_RE.pattern != IDENTIFIER_PATTERN

    def test_the_sequence_name_rule_has_no_length_limit(self):
        # A sequence name is not an AUTOSAR identifier and is not capped;
        # merging the two rules would impose a limit the diagram layer never had.
        assert _SEQUENCE_NAME_RE.match("A" * 200) is not None
        assert check_identifier("A" * 200) == REASON_LENGTH


class TestShortNameKey:
    """``short_name_key``: the one comparison key of ``[constr_2508]``'s case-insensitive uniqueness.

    Over the Identifier grammar it is R22-11's ASCII definition of *case insensitive* (GST R22-11,
    lines 2989-2997 of its text): the 52 letters fold pairwise, and digits and ``_`` are untouched.
    """

    def test_a_case_only_pair_has_one_key(self):
        assert short_name_key("Float32") == short_name_key("float32") == short_name_key("FLOAT32")

    def test_names_differing_otherwise_have_two(self):
        assert short_name_key("Float32") != short_name_key("Float64")
        assert short_name_key("a_b") != short_name_key("ab")

    def test_every_letter_folds_to_its_pair_and_nothing_else_moves(self):
        lower = "abcdefghijklmnopqrstuvwxyz"
        assert short_name_key(lower.upper()) == lower
        assert short_name_key("x0123456789_") == "x0123456789_"

    def test_over_the_grammar_it_equals_ascii_lowering(self):
        names = ["A", "aB_9", "Z" * MAX_IDENTIFIER_LENGTH, "Mixed_Case_Name_42"]
        assert all(IDENTIFIER_RE.fullmatch(name) for name in names)
        assert [short_name_key(name) for name in names] == [name.lower() for name in names]


class TestTheCIdentifierRule:
    """``check_c_identifier``: a runnable ``symbol``, AUTOSAR's ``CIdentifier``.

    GST R4.2.2 Table 4.31 and R4.3.0 Table 4.49 give the primitive the pattern
    ``[a-zA-Z_][a-zA-Z0-9_]*`` and no length; both XSDs' ``C-IDENTIFIER--SIMPLE`` carry it, which
    ``test_arxml_xsd_conformance.py`` compares literally. A leading underscore is legal and nothing
    caps the length.
    """

    @pytest.mark.parametrize("text", ["_leading", "a", "Run_1", "_", "A" * (MAX_IDENTIFIER_LENGTH + 50)])
    def test_accepts_a_c_identifier(self, text):
        assert check_c_identifier(text) is None

    @pytest.mark.parametrize(
        "text",
        ["1run", "run-1", "", "run\n", " run", "run ", "r un", "run\t", "Läufer"],
        ids=[
            "leading-digit",
            "hyphen",
            "empty",
            "trailing-newline",
            "leading-space",
            "trailing-space",
            "inner-space",
            "tab",
            "non-ascii",
        ],
    )
    def test_refuses_what_is_not_one(self, text):
        assert check_c_identifier(text) == REASON_C_IDENTIFIER

    def test_the_pattern_is_stored_unanchored_and_compiled_once(self):
        assert C_IDENTIFIER_PATTERN == "[a-zA-Z_][a-zA-Z0-9_]*"
        assert C_IDENTIFIER_RE.pattern == C_IDENTIFIER_PATTERN

    def test_every_identifier_is_a_c_identifier(self):
        for name in ("A", "uint16_impl", "A" * MAX_IDENTIFIER_LENGTH):
            assert check_identifier(name) is None
            assert check_c_identifier(name) is None

    def test_the_reason_names_the_pattern(self):
        assert C_IDENTIFIER_PATTERN in REASON_C_IDENTIFIER
        assert REASON_C_IDENTIFIER != REASON_PATTERN


class TestDependencyDirection:
    """The module is a leaf: ``validator/`` and ``writer/`` both read it, and ``writer`` imports ``validator``."""

    def _imported_modules(self) -> set[str]:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules.add(node.module)
        return modules

    def test_imports_nothing_from_the_writer_package(self):
        offenders = sorted(m for m in self._imported_modules() if m.startswith("swcraft_tools.writer"))
        assert offenders == [], f"identifier.py must not import the writer package; found {offenders}"

    def test_imports_nothing_from_swcraft_tools_at_all(self):
        offenders = sorted(m for m in self._imported_modules() if m.split(".")[0] == "swcraft_tools")
        assert offenders == [], f"identifier.py must stay a leaf module; found {offenders}"

    def test_uses_no_relative_imports_either(self):
        # A relative import has no module prefix to catch above, so it is checked
        # separately rather than being invisible to the leaf guard.
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        relative = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level > 0]
        assert relative == [], "identifier.py must stay a leaf module; found a relative import"
