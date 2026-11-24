"""The symbol citation's grammar and resolver, each refusal shown firing and each pass silent.

``symbol_citations`` is what the coverage oracle cites with and what the
evidence gates resolve against, so its refusals are proved here on planted
modules rather than on a shipped artifact that would have to carry a broken
citation.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import symbol_citations
from symbol_citations import SYMBOL_CITATION, citation_failure, construct_failures, enclosing_symbol, resolve

PLANTED = '''\
"""A planted module."""

from os import path as imported_path

LIMIT = 3
TABLE: dict[str, int] = {}
TWICE = 1
TWICE = 2


def helper() -> int:
    """Mentions `documented` in its docstring only."""
    return LIMIT


def caller() -> int:
    return helper()


def importer() -> str:
    from os import sep as only_imported

    return "x"


@staticmethod
def decorated() -> None:
    pass


class Owner:
    """A class with an attribute, a property and a nested function."""

    size: int = 0

    @property
    def value(self) -> int:
        return self.size

    @value.setter
    def value(self, new: int) -> None:
        self.size = new

    def outer(self) -> int:
        def inner() -> int:
            return helper()

        return inner()


def duplicated() -> None:
    pass


def duplicated() -> None:  # noqa: F811
    pass


print(imported_path.sep)
'''


@pytest.fixture
def planted(tmp_path: Path) -> Path:
    """A package root holding the planted module and two modules sharing a basename."""
    (tmp_path / "planted.py").write_text(PLANTED, encoding="utf-8")
    for directory in ("first", "second"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "shared.py").write_text("def f() -> None:\n    pass\n", encoding="utf-8")
    return tmp_path


def _line_of(text: str, needle: str) -> int:
    return next(number for number, line in enumerate(text.splitlines(), start=1) if needle in line)


# ---------------------------------------------------------------------------
# The grammar
# ---------------------------------------------------------------------------


def test_the_grammar_matches_both_spellings_and_no_neighbours() -> None:
    assert SYMBOL_CITATION.findall("see `validator" + ".py::check_x`") == [("validator.py", "check_x")]
    assert SYMBOL_CITATION.findall("editing/registry.py::Registry.layer") == [("editing/registry.py", "Registry.layer")]
    assert SYMBOL_CITATION.findall("validator.py" + ":3457") == []
    assert SYMBOL_CITATION.findall("model.pyx::f") == []
    assert SYMBOL_CITATION.findall("swcraft.yaml::f") == []


# ---------------------------------------------------------------------------
# The refusals, each shown firing
# ---------------------------------------------------------------------------


def test_an_absent_module_is_refused(planted: Path) -> None:
    assert "names no module" in citation_failure("missing.py::f", root=planted)


def test_an_ambiguous_basename_is_refused_with_the_spellings_to_choose(planted: Path) -> None:
    problem = citation_failure("shared.py::f", root=planted)
    assert "ambiguous" in problem and "first/shared.py" in problem and "second/shared.py" in problem
    assert citation_failure("first/shared.py::f", root=planted) == ""


def test_an_absent_symbol_is_refused(planted: Path) -> None:
    assert "defines no `nowhere`" in citation_failure("planted.py::nowhere", root=planted)
    assert "defines no `Owner.nowhere`" in citation_failure("planted.py::Owner.nowhere", root=planted)


@pytest.mark.parametrize("qualname", ["TWICE", "duplicated"])
def test_a_name_bound_twice_in_one_scope_is_refused(planted: Path, qualname: str) -> None:
    assert "is bound 2 times" in citation_failure(f"planted.py::{qualname}", root=planted)


@pytest.mark.parametrize("qualname", ["imported_path", "importer.only_imported"])
def test_an_imported_name_is_no_construct(planted: Path, qualname: str) -> None:
    assert "defines no" in citation_failure(f"planted.py::{qualname}", root=planted)


def test_an_anchor_the_construct_does_not_reference_is_refused(planted: Path) -> None:
    assert construct_failures("planted.py::helper", "caller", root=planted) == [
        "planted.py::helper: `caller` is neither the cited construct nor referenced in it"
    ]


def test_an_anchor_referenced_only_by_an_import_or_the_docstring_is_refused(planted: Path) -> None:
    (problem,) = construct_failures("planted.py::importer", "only_imported", root=planted)
    assert problem.endswith("(only in an import)"), problem
    (problem,) = construct_failures("planted.py::helper", "documented", root=planted)
    assert problem.endswith("(only in a docstring)"), problem


def test_a_bare_module_reached_only_through_an_import_is_refused(planted: Path) -> None:
    # `print(imported_path.sep)` references the import's binding; the import itself does not count.
    assert construct_failures("planted.py", "imported_path", root=planted) == []
    assert construct_failures("planted.py", "path", root=planted) == [
        "planted.py: `path` is neither the cited construct nor referenced in it"
    ]
    # A name used only inside a definition is not module-level code.
    assert construct_failures("planted.py", "helper", root=planted) != []


def test_an_unresolvable_citation_is_its_own_construct_failure(planted: Path) -> None:
    (problem,) = construct_failures("planted.py::nowhere", "helper", root=planted)
    assert "defines no `nowhere`" in problem


# ---------------------------------------------------------------------------
# The passes, each shown silent
# ---------------------------------------------------------------------------


def test_an_exact_package_path_beats_a_shared_basename() -> None:
    """``passport.py`` is the package's own, although ``guide_reference/passport.py`` shares the name."""
    assert symbol_citations.resolve_module("passport.py") == symbol_citations.PACKAGE_ROOT / "passport.py"
    assert citation_failure("passport.py::compute_passport") == ""
    assert citation_failure("guide_reference/passport.py") == ""


@pytest.mark.parametrize(
    "qualname",
    ["LIMIT", "TABLE", "helper", "decorated", "Owner", "Owner.size", "Owner.outer", "Owner.outer.inner"],
)
def test_a_construct_at_any_nesting_resolves(planted: Path, qualname: str) -> None:
    assert citation_failure(f"planted.py::{qualname}", root=planted) == ""


def test_a_property_and_its_setter_are_one_construct(planted: Path) -> None:
    construct = resolve("planted.py::Owner.value", root=planted)
    assert not isinstance(construct, str), construct
    assert len(construct.nodes) == 2
    assert construct.start == _line_of(PLANTED, "@property")
    assert construct.end == _line_of(PLANTED, "self.size = new")


def test_an_overload_group_is_one_construct(tmp_path: Path) -> None:
    (tmp_path / "typed.py").write_text(
        textwrap.dedent(
            """\
            from typing import overload


            @overload
            def parse(value: int) -> int: ...
            @overload
            def parse(value: str) -> str: ...
            def parse(value):
                return value
            """
        ),
        encoding="utf-8",
    )
    construct = resolve("typed.py::parse", root=tmp_path)
    assert not isinstance(construct, str), construct
    assert len(construct.nodes) == 3


@pytest.mark.parametrize(
    "source",
    [
        # a second def decorated as another name's setter
        "class Owner:\n"
        "    @property\n"
        "    def value(self):\n"
        "        return 1\n"
        "    @other.setter\n"
        "    def value(self, new):\n"
        "        pass\n",
        # an overload group whose non-final member lacks @overload
        "from typing import overload\n\n"
        "@overload\n"
        "def parse(value: int) -> int: ...\n"
        "def parse(value: str) -> str: ...\n"
        "def parse(value):\n"
        "    return value\n",
    ],
    ids=["setter-of-another-name", "overload-member-without-the-decorator"],
)
def test_a_repeated_binding_that_is_no_property_or_overload_group_is_refused(tmp_path: Path, source: str) -> None:
    (tmp_path / "twice.py").write_text(source, encoding="utf-8")
    qualname = "Owner.value" if "class Owner" in source else "parse"
    refusal = resolve(f"twice.py::{qualname}", root=tmp_path)
    assert isinstance(refusal, str) and "is bound" in refusal, refusal


def test_a_changed_file_is_parsed_again(tmp_path: Path) -> None:
    module = tmp_path / "changing.py"
    module.write_text("def first():\n    return 1\n", encoding="utf-8")
    assert not isinstance(resolve("changing.py::first", root=tmp_path), str)
    module.write_text("def second():\n    return 22\n", encoding="utf-8")
    assert isinstance(resolve("changing.py::first", root=tmp_path), str)
    assert not isinstance(resolve("changing.py::second", root=tmp_path), str)


def test_a_citation_that_is_the_anchor_passes(planted: Path) -> None:
    assert construct_failures("planted.py::Owner.outer.inner", "inner", root=planted) == []
    assert construct_failures("planted.py::Owner.outer", "Owner.outer", root=planted) == []
    assert construct_failures("planted.py::helper", "planted.helper", root=planted) == []


def test_a_package_qualifier_is_an_access_path_only_where_the_package_binds_the_name(tmp_path: Path) -> None:
    """``pkg.X`` beside ``pkg/inner.py::X`` passes only when ``pkg/__init__.py`` binds ``X``.

    The module's own dotted path (``pkg.inner.X``) and its stem (``inner.X``) are
    access paths whatever the facade binds.
    """
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("from pkg.inner import exported\n", encoding="utf-8")
    (package / "inner.py").write_text(
        "def exported():\n    return 1\n\n\ndef _private():\n    return 2\n", encoding="utf-8"
    )
    assert construct_failures("pkg/inner.py::exported", "pkg.exported", root=tmp_path) == []
    assert construct_failures("pkg/inner.py::_private", "pkg.inner._private", root=tmp_path) == []
    assert construct_failures("pkg/inner.py::_private", "inner._private", root=tmp_path) == []
    (refusal,) = construct_failures("pkg/inner.py::_private", "pkg._private", root=tmp_path)
    assert "neither the cited construct nor referenced in it" in refusal
    assert symbol_citations.anchor_parts("pkg/inner.py", "pkg._private", root=tmp_path) == ["pkg", "_private"]


def test_a_construct_referencing_the_anchor_passes(planted: Path) -> None:
    assert construct_failures("planted.py::caller", "helper", root=planted) == []
    assert construct_failures("planted.py::Owner.value", "size", root=planted) == []


def test_enclosing_symbol_names_the_innermost_construct(planted: Path) -> None:
    module = planted / "planted.py"

    def at(needle: str) -> str:
        return enclosing_symbol(module, _line_of(PLANTED, needle), root=planted)

    assert at("def inner") == "planted.py::Owner.outer.inner"
    assert at("return inner()") == "planted.py::Owner.outer"
    assert at("@staticmethod") == "planted.py::decorated"
    assert at("size: int = 0") == "planted.py::Owner.size"
    assert at("self.size = new") == "planted.py::Owner.value"
    assert at("print(imported_path.sep)") == "planted.py"
    assert at("from os import path") == "planted.py"


def test_enclosing_symbol_skips_a_name_bound_twice_for_its_enclosure(planted: Path) -> None:
    """A construct the resolver would refuse is never handed out as a citation."""
    module = planted / "planted.py"
    assert enclosing_symbol(module, _line_of(PLANTED, "TWICE = 2"), root=planted) == "planted.py"
    for line in range(1, len(PLANTED.splitlines()) + 1):
        assert citation_failure(enclosing_symbol(module, line, root=planted), root=planted) == "", line


# ---------------------------------------------------------------------------
# The constructor refusal's helper
# ---------------------------------------------------------------------------


def test_a_construct_that_calls_neither_the_class_nor_its_twin_is_refused(planted: Path) -> None:
    (problem,) = symbol_citations.construction_failures("planted.py::helper", "Owner", root=planted)
    assert "calls neither it nor its factory twin `owner`" in problem, problem


def test_a_construct_calling_the_class_or_its_factory_twin_passes(planted: Path) -> None:
    # `caller` calls `helper()`, the snake_case twin of a class named `Helper`.
    assert symbol_citations.construction_failures("planted.py::caller", "Helper", root=planted) == []
    assert symbol_citations.construction_failures("planted.py::Owner.outer", "Helper", root=planted) == []
    assert symbol_citations.snake_case("SWComponent") == "sw_component"
    assert symbol_citations.snake_case("SafetyReading") == "safety_reading"


def test_an_unresolvable_construction_citation_is_its_own_failure(planted: Path) -> None:
    (problem,) = symbol_citations.construction_failures("planted.py::nowhere", "Owner", root=planted)
    assert "defines no `nowhere`" in problem
