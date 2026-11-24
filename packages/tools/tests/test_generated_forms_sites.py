"""The application-only presence predicate is called from its two whole-kind twins and nowhere else.

A data type's generated field-scoped names have two halves: an application type's ``_Unit`` /
``_Compu`` / ``_Range`` and a bounded value type's ``_Range``. A site that asked the application
half directly would see no value type's form -- a guard looser than the writer. So over every
module of ``packages/tools/src/swcraft_tools``, by AST, a call of an application half is allowed
only inside the body of a whole-kind predicate, and importing one is allowed nowhere.

What it does not reach: a call through ``getattr`` or a variable bound to the function, and a site
that restates the presence rule instead of calling a predicate (``editing/diagnosis.py``'s,
held by ``test_editing_validation.py``; the validator's twin, by ``test_validator_identifier.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "swcraft_tools"

#: The application-only halves, one per side of the ``validator`` / ``writer`` split.
APPLICATION_HALVES = frozenset({"generated_application_forms", "_generated_application_forms"})
#: The only functions whose bodies may call one of them.
WHOLE_KIND_PREDICATES = frozenset({"generated_data_type_forms"})


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def violations(source: str, label: str) -> list[str]:
    """Every call or import of an application half in *source* outside the allowed bodies."""
    tree = ast.parse(source)
    found: list[str] = []

    def visit(node: ast.AST, enclosing: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            scope = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else enclosing
            if isinstance(child, ast.Call) and _called_name(child) in APPLICATION_HALVES:
                if enclosing not in WHOLE_KIND_PREDICATES:
                    found.append(f"{label}:{child.lineno} calls {_called_name(child)} in {enclosing or 'module scope'}")
            if isinstance(child, ast.ImportFrom):
                for alias in child.names:
                    if alias.name in APPLICATION_HALVES:
                        found.append(f"{label}:{child.lineno} imports {alias.name}")
            visit(child, scope)

    visit(tree, None)
    return found


def test_no_site_asks_the_application_half_directly() -> None:
    found: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py"), key=lambda p: p.as_posix()):
        found.extend(violations(path.read_text(encoding="utf-8"), path.relative_to(PACKAGE_ROOT).as_posix()))
    message = (
        "ask writer.arxml_generation.generated_data_type_forms "
        "(or validator.generated_names.generated_data_type_forms) instead:\n" + "\n".join(found)
    )
    assert found == [], message


def test_the_two_whole_kind_predicates_each_call_their_half_once() -> None:
    """The positive half: the allowed calls exist, so the scan above is not vacuous."""
    for relative, predicate, half in (
        ("writer/arxml_generation.py", "generated_data_type_forms", "generated_application_forms"),
        ("validator/generated_names.py", "generated_data_type_forms", "_generated_application_forms"),
    ):
        tree = ast.parse((PACKAGE_ROOT / relative).read_text(encoding="utf-8"))
        (definition,) = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == predicate]
        calls = [node for node in ast.walk(definition) if isinstance(node, ast.Call) and _called_name(node) == half]
        assert len(calls) == 1, (relative, predicate)
        assert [node.name for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == half] == [half]


def test_the_scan_bites() -> None:
    """A call at module scope, inside another function, through an attribute, and an import: each is found."""
    probe = (
        "from swcraft_tools.writer.arxml_generation import generated_application_forms\n"
        "import swcraft_tools.validator as v\n"
        "x = generated_application_forms(dt)\n"
        "def guard(dt):\n"
        "    return v._generated_application_forms(dt)\n"
        "def generated_data_type_forms(dt):\n"
        "    return generated_application_forms(dt)\n"
    )
    assert violations(probe, "probe") == [
        "probe:1 imports generated_application_forms",
        "probe:3 calls generated_application_forms in module scope",
        "probe:5 calls _generated_application_forms in guard",
    ]
