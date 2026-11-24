"""No path into a root package is joined under ``writer/`` but by ``_placed_path``.

The writer reads package placement from one table, and every reference into ``/DataTypes``,
``/PortInterfaces`` or ``/ComponentTypes`` is built by ``arxml_generation._placed_path``; a reference
joined anywhere else by hand would point at the root while the element sits in its sub-package.

**The gate refuses the names, not a join shape.** Over every module under
``packages/tools/src/swcraft_tools/writer/`` it refuses any use of ``_DATA_TYPES``,
``_PORT_INTERFACES``, ``_COMPONENT_TYPES``, ``ROOT_PACKAGE_NAMES``, ``DATA_TYPES_PACKAGE_PATH``,
``PORT_INTERFACES_PACKAGE_PATH`` or ``COMPONENT_TYPES_PACKAGE_PATH`` (a bare name, an attribute, an
import) and any string literal containing ``/DataTypes``, ``/PortInterfaces`` or ``/ComponentTypes``.
Refusing the name reaches an f-string, ``"/".join``, ``%`` and ``.format`` without enumerating them.

**The allow-list is three scopes**: ``arxml_generation._placed_path``, ``arxml_xml.PackagePlacement``
(whose ``Root`` enum names the three roots) and ``arxml_xml.ArxmlDocument.__init__``.

**The exemptions, none of them a join:** docstrings (prose builds no path); the two imports of
``ROOT_PACKAGE_NAMES`` in ``arxml_generation.py`` and ``arxml_xml.py``; ``ArxmlDocument.PACKAGE_NAMES``,
the class attribute giving the roots' creation order, joined to nothing; and one error message in
``_check_component_type_names`` that names the package to a reader. Every exemption must still match
something (:func:`test_every_exemption_is_still_needed`).

Every routed site calls ``_placed_path``, counted per function in :data:`PLACED_PATH_CALLS`.

**What it does not reach:** a path rebuilt from ``ArxmlPackage.name`` (an attribute read the gate
cannot tell from any other ``.name``); a path assembled from a variable that already holds a placed
path (the intended use); a root spelled without its slash and joined by hand (``"/" + "DataTypes"``);
and every module outside ``writer/``. The reference oracle (``reference_oracle.py``,
``test_package_placement_writer.py``) covers them on output.
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from conftest import REPO_ROOT

WRITER = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "writer"

#: The names no scope outside the allow-list may use. All but ``ROOT_PACKAGE_NAMES`` are gone from
#: ``writer/``; they stay refused so that one brought back fails here rather than inviting a hand-made join.
REFUSED_NAMES = frozenset(
    {
        "_DATA_TYPES",
        "_PORT_INTERFACES",
        "_COMPONENT_TYPES",
        "ROOT_PACKAGE_NAMES",
        "DATA_TYPES_PACKAGE_PATH",
        "PORT_INTERFACES_PACKAGE_PATH",
        "COMPONENT_TYPES_PACKAGE_PATH",
    }
)

#: The root paths no string literal outside the allow-list may contain.
REFUSED_TEXT = ("/DataTypes", "/PortInterfaces", "/ComponentTypes")

#: ``(module, qualified scope)`` — the scopes the refused names may be used in.
ALLOWED_SCOPES = frozenset(
    {
        ("arxml_generation.py", "_placed_path"),
        ("arxml_xml.py", "PackagePlacement"),
        ("arxml_xml.py", "ArxmlDocument.__init__"),
    }
)

#: The four exemptions besides docstrings, each ``(module, scope, what)``; ``scope``
#: is ``""`` at module level. ``what`` is the statement's kind and the text that
#: identifies it.
EXEMPTIONS = frozenset(
    {
        ("arxml_generation.py", "", "import ROOT_PACKAGE_NAMES"),
        ("arxml_xml.py", "", "import ROOT_PACKAGE_NAMES"),
        ("arxml_xml.py", "ArxmlDocument", "assign PACKAGE_NAMES"),
        ("arxml_generation.py", "_check_component_type_names", "message Colliding /ComponentTypes SHORT-NAME(s)"),
    }
)

#: The ``_placed_path`` calls per function. A new reference into a root package adds its call here; a
#: helper that takes over several references replaces their counts with its own.
PLACED_PATH_CALLS: dict[str, int] = {
    "resolve_type_ref": 1,
    "_resolve_application_type_ref": 1,
    "_emit_implementation_data_type": 3,
    "_emit_implementation_type_reference": 1,
    "_emit_enum_type": 2,
    "_stated_form_path": 2,
    "_emit_mapping_set": 2,
    "_emit_init_value_reference": 1,
    "_member_path": 1,
    "_emit_port": 1,
    "_emit_events": 1,
    "_emit_disabled_modes": 1,
    "_emit_variable_access": 1,
    "_emit_server_call_point": 1,
    "_emit_local_variable_accesses": 1,
    "_emit_data_type_mapping_refs": 1,
    "_emit_internal_behavior": 1,
    "_emit_swc_implementation": 1,
    "_emit_swc_timing": 1,
    "_component_type_path": 2,
    "_emit_assembly_connector": 1,
    "_emit_delegation": 2,
    "_emit_linear_element": 1,
}


@dataclass(frozen=True)
class Use:
    """One use the gate found: where, and what."""

    module: str
    scope: str
    line: int
    what: str


def _docstring_nodes(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                found.add(id(first.value))
    return found


def _statement_label(statement: ast.stmt) -> str | None:
    """How an exemption names *statement*: its kind and the text that identifies it."""
    if isinstance(statement, ast.ImportFrom):
        return "import " + ",".join(alias.name for alias in statement.names if alias.name in REFUSED_NAMES)
    if isinstance(statement, (ast.Assign, ast.AnnAssign)):
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        names: list[str] = []
        for target in targets:
            elements = target.elts if isinstance(target, ast.Tuple) else [target]
            names.extend(element.id for element in elements if isinstance(element, ast.Name))
        return "assign " + ",".join(names)
    return None


def scan(source: str, module: str) -> tuple[list[Use], set[tuple[str, str, str]]]:
    """Every refused use in *source* outside the allow-list, and every exemption that matched.

    Returns:
        ``(offenders, matched)``: the uses no exemption covers, and the
        :data:`EXEMPTIONS` entries some use matched.
    """
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    offenders: list[Use] = []
    matched: set[tuple[str, str, str]] = set()

    def refused(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name) and node.id in REFUSED_NAMES:
            return f"name {node.id}"
        if isinstance(node, ast.Attribute) and node.attr in REFUSED_NAMES:
            return f"attribute {node.attr}"
        if isinstance(node, ast.alias) and node.name in REFUSED_NAMES:
            return f"import {node.name}"
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if any(text in node.value for text in REFUSED_TEXT):
                return f"literal {node.value!r}"
        return None

    def visit(node: ast.AST, scope: str, statement: ast.stmt | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = f"{scope}.{node.name}" if scope else node.name
            if (module, scope) in ALLOWED_SCOPES:
                return
        if isinstance(node, ast.stmt):
            statement = node
        what = refused(node)
        if what is not None:
            label = _statement_label(statement) if statement is not None else None
            exemption = (module, scope, label)
            if label is not None and exemption in EXEMPTIONS:
                matched.add(exemption)
            elif what.startswith("literal") and statement is not None:
                message = next(
                    (
                        entry
                        for entry in EXEMPTIONS
                        if entry[:2] == (module, scope)
                        and entry[2].startswith("message ")
                        and node.value.startswith(entry[2].removeprefix("message "))  # type: ignore[attr-defined]
                    ),
                    None,
                )
                if message is None:
                    offenders.append(Use(module, scope, getattr(node, "lineno", 0), what))
                else:
                    matched.add(message)
            else:
                offenders.append(Use(module, scope, getattr(node, "lineno", 0), what))
        for child in ast.iter_child_nodes(node):
            visit(child, scope, statement)

    visit(tree, "", None)
    return offenders, matched


def _writer_modules() -> list[Path]:
    return sorted(WRITER.rglob("*.py"), key=lambda path: path.as_posix())


def test_no_root_path_is_joined_outside_the_allow_list() -> None:
    offenders: list[Use] = []
    for path in _writer_modules():
        found, _matched = scan(path.read_text(encoding="utf-8"), path.name)
        offenders.extend(found)
    assert offenders == [], "\n".join(
        f"writer/{use.module}:{use.line} ({use.scope or 'module'}): {use.what} -- "
        "build a path into a root package with _placed_path"
        for use in offenders
    )


def test_every_exemption_is_still_needed() -> None:
    matched: set[tuple[str, str, str]] = set()
    for path in _writer_modules():
        matched |= scan(path.read_text(encoding="utf-8"), path.name)[1]
    assert EXEMPTIONS - matched == set()


def test_the_routed_sites_are_the_counted_calls() -> None:
    tree = ast.parse((WRITER / "arxml_generation.py").read_text(encoding="utf-8"))
    calls: Counter[str] = Counter()
    for function in tree.body:
        if isinstance(function, ast.FunctionDef):
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_placed_path":
                    calls[function.name] += 1
    assert sum(PLACED_PATH_CALLS.values()) == 30
    assert dict(calls) == PLACED_PATH_CALLS


class TestTheGateSeesWhatItMustRefuse:
    """Non-vacuity: each shape the gate exists for, in a module it scans."""

    def _offenders(self, source: str, module: str = "emitters.py") -> list[str]:
        return [use.what for use in scan(source, module)[0]]

    def test_an_f_string_join(self) -> None:
        assert self._offenders('def f(n):\n    return f"{DATA_TYPES_PACKAGE_PATH}/{n}"\n') == [
            "name DATA_TYPES_PACKAGE_PATH"
        ]

    def test_a_literal_root(self) -> None:
        assert self._offenders('def f(n):\n    return "/PortInterfaces/" + n\n') == ["literal '/PortInterfaces/'"]

    def test_a_join_call_and_a_format(self) -> None:
        source = (
            'def f(n):\n    a = "/".join(["", _COMPONENT_TYPES, n])\n    return "{}".format(ROOT_PACKAGE_NAMES[0])\n'
        )
        assert self._offenders(source) == ["name _COMPONENT_TYPES", "name ROOT_PACKAGE_NAMES"]

    def test_an_attribute_and_an_import(self) -> None:
        source = "from swcraft_tools.arxml_version import ROOT_PACKAGE_NAMES\nx = module.ROOT_PACKAGE_NAMES\n"
        assert self._offenders(source) == ["import ROOT_PACKAGE_NAMES", "attribute ROOT_PACKAGE_NAMES"]

    def test_the_allow_listed_function_may(self) -> None:
        source = 'def _placed_path(root, n):\n    return f"/{ROOT_PACKAGE_NAMES[0]}/{n}"\n'
        assert self._offenders(source, "arxml_generation.py") == []

    def test_a_docstring_may_name_a_root(self) -> None:
        assert self._offenders('def f():\n    """Emits into ``/DataTypes``."""\n') == []

    def test_an_exemption_is_scoped_to_its_statement(self) -> None:
        source = 'def g():\n    raise ValueError("Colliding /ComponentTypes SHORT-NAME(s) here")\n'
        assert self._offenders(source, "arxml_generation.py") == [
            "literal 'Colliding /ComponentTypes SHORT-NAME(s) here'"
        ]
