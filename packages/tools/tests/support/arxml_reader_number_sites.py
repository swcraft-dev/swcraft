"""Every production call of the reader's five number helpers, with its field and the grammar it reads by.

``parse_int_literal``, ``exact_decimal``, ``parse_exact_number``, ``_read_constant_scalar`` and ``stripped_spelling``
(``swcraft_tools.arxml_reader``) take a required ``grammar`` (``arxml_reader.NumberGrammar``), because the grammar
is the field's: ``"pattern"`` for ``NUMERICAL-VALUE``, ``"integer"`` for a signed integer, ``"positive-integer"``
for ``POSITIVE-INTEGER``, ``"formula"`` for a ``LIMIT`` or a ``*-VALUE-VARIATION-POINT``. Each row names the
module, the enclosing function, the helper, the ``(parent tag, tag)`` fields and the grammar;
``test_arxml_reader_number_sites.py`` holds the table against the AST of every module and against
``io_oracle_xsd_map_data.NUMERIC_CONTEXTS``. The helpers' own chaining rows read no field and spell their grammar
:data:`PASSED_ON`.

**What this does not reach:** whether *fields* is the right list for a site (the AST sees the call, not the
element; ``TestEveryNumberFieldIsReadByItsOwnGrammar`` witnesses the sites it reaches). Not members:
``parse_float_literal`` (``xsd:double``'s lexical space, ``arxml_reader._XSD_DOUBLE``), ``_integer_spelling`` and
``_real_spelling`` (called only inside the helpers), and ``_read_cse_code`` (a ``CSE-CODE`` is an
``xsd:unsignedInt``, read by that type's own lexical space) with the one ``_INTEGER_SPELLING`` read beside it.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

#: The five helpers whose every call names a grammar.
HELPERS = frozenset(
    {"parse_int_literal", "exact_decimal", "parse_exact_number", "_read_constant_scalar", "stripped_spelling"}
)

#: A site's grammar: ``arxml_reader.NumberGrammar``'s four, or :data:`PASSED_ON`.
SiteGrammar = Literal["pattern", "integer", "positive-integer", "formula", "grammar"]

#: The grammar of a chaining call: the enclosing helper's own ``grammar`` parameter, passed on.
PASSED_ON: SiteGrammar = "grammar"

#: The XSD map's number class -> the grammar a field of that class is read by.
GRAMMAR_OF_CLASS: dict[str, SiteGrammar] = {
    "numerical": "pattern",
    "integer": "integer",
    "positive-integer": "positive-integer",
    "formula": "formula",
}


class NumberSite(NamedTuple):
    """One call, or several identical calls in one function (*count*)."""

    module: str
    function: str
    helper: str
    fields: tuple[tuple[str, str], ...]
    grammar: SiteGrammar
    count: int = 1


_QUEUE_LENGTHS = (("QUEUED-RECEIVER-COM-SPEC", "QUEUE-LENGTH"), ("SERVER-COM-SPEC", "QUEUE-LENGTH"))
_PHYS_LIMITS = (("PHYS-CONSTRS", "LOWER-LIMIT"), ("PHYS-CONSTRS", "UPPER-LIMIT"))
_INTERNAL_LIMITS = (("INTERNAL-CONSTRS", "LOWER-LIMIT"), ("INTERNAL-CONSTRS", "UPPER-LIMIT"))
_SCALE_LIMITS = (("COMPU-SCALE", "LOWER-LIMIT"), ("COMPU-SCALE", "UPPER-LIMIT"))

NUMBER_SITES: tuple[NumberSite, ...] = (
    # The helpers' own chaining.
    NumberSite("arxml_reader.py", "exact_decimal", "parse_int_literal", (), PASSED_ON),
    NumberSite("arxml_reader.py", "parse_exact_number", "parse_int_literal", (), PASSED_ON),
    NumberSite("arxml_reader.py", "parse_int_literal", "stripped_spelling", (), PASSED_ON),
    NumberSite("arxml_reader.py", "_real_spelling", "stripped_spelling", (), PASSED_ON),
    NumberSite("arxml_reader.py", "_read_constant_scalar", "stripped_spelling", (), PASSED_ON),
    # The reader.
    NumberSite(
        "arxml_reader.py",
        "_read_sw_base_type",
        "parse_int_literal",
        (("SW-BASE-TYPE", "BASE-TYPE-SIZE"),),
        "positive-integer",
    ),
    NumberSite(
        "arxml_reader.py",
        "_read_impl_sub_elements",
        "parse_int_literal",
        (("IMPLEMENTATION-DATA-TYPE-ELEMENT", "ARRAY-SIZE"),),
        "formula",
    ),
    NumberSite("arxml_reader.py", "_read_data_constr", "parse_exact_number", _PHYS_LIMITS, "formula"),
    NumberSite(
        "arxml_reader.py", "_read_data_constr", "parse_int_literal", (("DATA-CONSTR-RULE", "CONSTR-LEVEL"),), "integer"
    ),
    NumberSite(
        "arxml_reader.py",
        "_read_app_array_data_type",
        "parse_int_literal",
        (("ELEMENT", "MAX-NUMBER-OF-ELEMENTS"),),
        "formula",
    ),
    NumberSite(
        "arxml_reader.py", "_read_compu_method", "parse_int_literal", (("COMPU-SCALE", "LOWER-LIMIT"),), "formula"
    ),
    NumberSite(
        "arxml_reader.py", "_read_application_value", "_read_constant_scalar", (("SW-VALUES-PHYS", "V"),), "pattern"
    ),
    NumberSite(
        "arxml_reader.py",
        "_read_value_specification",
        "_read_constant_scalar",
        (("NUMERICAL-VALUE-SPECIFICATION", "VALUE"),),
        "formula",
    ),
    NumberSite("arxml_reader.py", "_value_type_bound", "parse_exact_number", _INTERNAL_LIMITS, "formula"),
    # A budget's factor is an `AR:INTEGER`.
    NumberSite(
        "arxml_reader.py",
        "_read_multidimensional_time",
        "parse_int_literal",
        (("MAXIMUM", "CSE-CODE-FACTOR"), ("MINIMUM", "CSE-CODE-FACTOR")),
        "integer",
    ),
    # The importer.
    NumberSite("arxml_import/merge.py", "_coefficient", "stripped_spelling", (("COMPU-NUMERATOR", "V"),), "formula"),
    NumberSite("arxml_import/merge.py", "_coefficient", "parse_exact_number", (("COMPU-NUMERATOR", "V"),), "formula"),
    NumberSite(
        "arxml_import/merge.py", "_denominator_is_one", "exact_decimal", (("COMPU-DENOMINATOR", "V"),), "formula"
    ),
    NumberSite(
        "arxml_import/merge.py",
        "texttable_limit_losses",
        "parse_int_literal",
        (("COMPU-SCALE", "UPPER-LIMIT"),),
        "formula",
    ),
    NumberSite("arxml_import/merge.py", "_com_spec_value", "parse_int_literal", _QUEUE_LENGTHS, "positive-integer"),
    NumberSite(
        "arxml_import/merge_verdict.py",
        "_server_queue_length_signature",
        "parse_int_literal",
        (("SERVER-COM-SPEC", "QUEUE-LENGTH"),),
        "positive-integer",
    ),
    NumberSite(
        "arxml_import/merge_verdict.py",
        "_limit_signature",
        "exact_decimal",
        _PHYS_LIMITS + _INTERNAL_LIMITS + _SCALE_LIMITS,
        "formula",
    ),
    NumberSite(
        "arxml_import/merge_verdict.py",
        "_limit_signature",
        "parse_exact_number",
        _PHYS_LIMITS + _INTERNAL_LIMITS + _SCALE_LIMITS,
        "formula",
    ),
    NumberSite(
        "arxml_import/swc_mapping.py",
        "server_queue_length_losses",
        "parse_int_literal",
        _QUEUE_LENGTHS,
        "positive-integer",
    ),
    NumberSite(
        "arxml_import/swc_mapping.py",
        "_composition_queue_length_losses",
        "parse_int_literal",
        (("SERVER-COM-SPEC", "QUEUE-LENGTH"),),
        "positive-integer",
    ),
    # A LINEAR scale's limits are carried, read by the formula grammar of their LIMIT type.
    NumberSite("arxml_import/merge.py", "linear_scale_limits", "parse_exact_number", _SCALE_LIMITS, "formula"),
    # A number given under a TEXTTABLE is read through the scale whose interval contains it,
    # the UPPER-LIMIT read as `texttable_limit_losses` reads it.
    NumberSite(
        "arxml_import/type_mapping.py",
        "_scale_reading",
        "parse_int_literal",
        (("COMPU-SCALE", "UPPER-LIMIT"),),
        "formula",
    ),
)
