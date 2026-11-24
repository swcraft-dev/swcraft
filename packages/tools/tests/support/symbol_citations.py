"""Symbol citations: ``<module>::<qualname>``, resolved against the source by AST.

Test support, never part of the product. A citation in the model contract's
evidence, or a finding of the coverage oracle, names the construct that performs
what the sentence claims, never a line number:

* ``<module>::<qualname>`` -- ``editing/registry.py::_type_name``,
  ``arxml_import/report.py::ImportReport.render``, ``outer.inner``, ``CONSISTENCY_RULES``
  (pytest's node-id spelling, which no dotted prose anchor can be mistaken for);
* ``<module>`` alone -- the module's code outside every ``def`` and ``class``,
  imports excluded.

**The module** is a path relative to the package root: an exact package-relative
path wins, otherwise a unique basename, otherwise it is refused as ambiguous with the
spellings to choose from.

**What resolves** is a ``def`` or a ``class`` at any nesting, or a module-level or
class-level ``Assign``/``AnnAssign`` to a plain name; a span starts at the first
decorator. A name bound twice in one scope is refused, except a ``property`` with
its setter/getter/deleter and an ``@overload`` group, which take the union of their
spans. An import binds no construct, so an imported name resolves to nothing.

**What it does not reach.** A citation proves its construct exists, not the position
inside it (a 1,000-line function admits any statement) nor the prose around it.
Local assignments, names bound by ``for``/``with``/walrus, and tuple targets are not
constructs; cite the function.

Stdlib only, the package root from this file: ``coverage_oracle`` imports this
module and must import no ``swcraft_tools``.
"""

from __future__ import annotations

import ast
import functools
import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import NamedTuple

#: The package the evidence cites into, resolved from this file.
PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "swcraft_tools"

#: ``module.py::qualname``; the qualname is dotted identifiers.
SYMBOL_CITATION = re.compile(r"(?<![\w/.])((?:[\w.]+/)*[\w.]+\.py)::([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)")

_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_IMPORTS = (ast.Import, ast.ImportFrom)
_PROPERTY_PARTS = frozenset({"setter", "getter", "deleter"})


class Construct(NamedTuple):
    """One resolvable construct: its qualname, its span and the nodes it is made of.

    ``nodes`` holds one node, or every part of a property or an overload group.
    """

    qualname: str
    start: int
    end: int
    nodes: tuple[ast.AST, ...]


def _start(node: ast.stmt) -> int:
    decorators = getattr(node, "decorator_list", [])
    return min([node.lineno, *(decorator.lineno for decorator in decorators)])


def _end(node: ast.stmt) -> int:
    return node.end_lineno or node.lineno


def _children(statement: ast.stmt) -> Iterator[ast.stmt]:
    """The statements nested in a compound statement that stay in its scope."""
    for name in ("body", "orelse", "finalbody"):
        yield from getattr(statement, name, [])
    for handler in getattr(statement, "handlers", []):
        yield from handler.body
    for case in getattr(statement, "cases", []):
        yield from case.body


def _bindings(body: Iterable[ast.stmt], *, assignments: bool) -> Iterator[tuple[str, ast.stmt]]:
    """``(name, node)`` for every construct *body* binds in its own scope."""
    for statement in body:
        if isinstance(statement, _DEFINITIONS):
            yield statement.name, statement
        elif isinstance(statement, ast.Assign):
            if assignments:
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        yield target.id, statement
        elif isinstance(statement, ast.AnnAssign):
            if assignments and isinstance(statement.target, ast.Name):
                yield statement.target.id, statement
        else:
            yield from _bindings(_children(statement), assignments=assignments)


def _decorator_names(node: ast.AST) -> Iterator[tuple[str, str]]:
    """``(receiver, attribute)`` of each decorator; ``("", name)`` for a bare name."""
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name):
            yield "", target.id
        elif isinstance(target, ast.Attribute):
            receiver = target.value.id if isinstance(target.value, ast.Name) else ""
            yield receiver, target.attr


def _is_one_construct(name: str, nodes: list[ast.stmt]) -> bool:
    """True when the repeated bindings are a property's parts or an overload group."""
    if not all(isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) for node in nodes):
        return False
    overloads = [any(attribute == "overload" for _, attribute in _decorator_names(node)) for node in nodes]
    if all(overloads[:-1]):
        return True
    return all(
        any(receiver == name and attribute in _PROPERTY_PARTS for receiver, attribute in _decorator_names(node))
        for node in nodes[1:]
    )


def constructs(tree: ast.Module) -> dict[str, list[Construct]]:
    """Every construct of *tree* by qualname; two entries mean a name bound twice.

    A property or an overload group is one entry, spanning all its parts.
    """
    table: dict[str, list[Construct]] = {}

    def walk(body: Iterable[ast.stmt], prefix: str, *, assignments: bool) -> None:
        grouped: dict[str, list[ast.stmt]] = {}
        for name, node in _bindings(body, assignments=assignments):
            grouped.setdefault(name, []).append(node)
        for name, nodes in grouped.items():
            qualname = f"{prefix}{name}"
            if len(nodes) > 1 and _is_one_construct(name, nodes):
                start = min(_start(node) for node in nodes)
                end = max(_end(node) for node in nodes)
                table.setdefault(qualname, []).append(Construct(qualname, start, end, tuple(nodes)))
            else:
                for node in nodes:
                    table.setdefault(qualname, []).append(Construct(qualname, _start(node), _end(node), (node,)))
            for node in nodes:
                if isinstance(node, _DEFINITIONS):
                    walk(node.body, f"{qualname}.", assignments=isinstance(node, ast.ClassDef))

    walk(tree.body, "", assignments=True)
    return table


class Module(NamedTuple):
    """A parsed module: its tree, its constructs and its innermost-construct line map."""

    tree: ast.Module
    table: Mapping[str, list[Construct]]
    #: ``lines[n]`` is the qualname of the innermost resolvable construct holding
    #: line *n* (1-based), ``""`` at module level.
    lines: tuple[str, ...]


def parse(text: str) -> Module:
    """Parse *text* and index its constructs."""
    tree = ast.parse(text)
    table = constructs(tree)
    painted = [""] * (len(text.splitlines()) + 2)
    resolvable = sorted(
        (entries[0] for entries in table.values() if len(entries) == 1),
        key=lambda construct: (construct.start, -construct.end, construct.qualname.count(".")),
    )
    for construct in resolvable:
        for line in range(construct.start, min(construct.end, len(painted) - 1) + 1):
            painted[line] = construct.qualname
    return Module(tree, table, tuple(painted))


@functools.cache
def _parse_file(path: Path, stamp: tuple[int, int]) -> Module:
    del stamp  # the cache key: a rewritten file is parsed again
    return parse(path.read_text(encoding="utf-8"))


def parse_file(path: Path) -> Module:
    """:func:`parse` over *path*, cached until the file changes."""
    status = path.stat()
    return _parse_file(path.resolve(), (status.st_mtime_ns, status.st_size))


@functools.cache
def _package_modules(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.rglob("*.py"), key=lambda path: path.as_posix()))


def _modules_under(root: Path) -> tuple[Path, ...]:
    """Every module under *root*; cached for the package, listed afresh for any other root."""
    if root == PACKAGE_ROOT:
        return _package_modules(root)
    return tuple(sorted(root.rglob("*.py"), key=lambda path: path.as_posix()))


def resolve_module(module: str, *, root: Path = PACKAGE_ROOT) -> Path | str:
    """The file *module* names under *root*, or the refusal as a sentence.

    An exact package-relative path wins over a basename shared with a deeper
    module (``cli.py`` beats ``guide_reference/cli.py``); otherwise the basename
    must be unique.
    """
    resolved = root.resolve()
    modules = _modules_under(resolved)
    candidates = [path for path in modules if path.as_posix().endswith("/" + module)]
    exact = [path for path in candidates if path.relative_to(resolved).as_posix() == module]
    if len(exact) == 1:
        return exact[0]
    if not candidates:
        return f"'{module}' names no module under {root.name}/"
    if len(candidates) > 1:
        qualified = [path.relative_to(resolved).as_posix() for path in candidates]
        return f"'{module}' is ambiguous: cite it as one of {qualified}"
    return candidates[0]


def _bound_names(init: Path) -> frozenset[str]:
    """Every name a package's ``__init__.py`` binds at its top level: imports, assignments, definitions."""
    names: set[str] = set()
    for node in parse_file(init).tree.body:
        if isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, _DEFINITIONS):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return frozenset(names)


def anchor_parts(module: str, anchor: str, *, root: Path = PACKAGE_ROOT) -> list[str]:
    """*anchor*'s dotted parts with the module qualifier it opens with removed.

    A qualifier is removed only where it is a real access path to the name: the
    module's stem (``hardware.X``), its full dotted path as the citation writes it
    (``validator.hardware.X``), or one package the citation names whose
    ``__init__.py`` binds the name that follows (``validator.X`` beside
    ``validator/hardware.py`` when the facade re-exports ``X``). Any other anchor
    keeps all its parts.
    """
    parts = anchor.split(".")
    path = Path(module)
    packages = path.parent.parts
    dotted_module = [*packages, path.stem]
    if len(parts) > len(dotted_module) and parts[: len(dotted_module)] == dotted_module and packages:
        return parts[len(dotted_module) :]
    if len(parts) > 1 and parts[0] == path.stem:
        return parts[1:]
    if len(parts) > 1 and parts[0] in packages:
        resolved = resolve_module(module, root=root)
        if isinstance(resolved, Path):
            depth = len(packages) - 1 - packages.index(parts[0])
            init = resolved.parents[depth] / "__init__.py"
            if init.is_file() and parts[1] in _bound_names(init):
                return parts[1:]
    return parts


def split(citation: str) -> tuple[str, str]:
    """``(module, qualname)``; the qualname is ``""`` for a bare module."""
    module, _, qualname = citation.partition("::")
    return module, qualname


def resolve(citation: str, *, root: Path = PACKAGE_ROOT) -> Construct | str:
    """The construct *citation* names, or why it names none.

    A bare ``module.py`` resolves to the module's unnamed code (``qualname`` is
    ``""``): every top-level statement that is neither a definition nor an import,
    the module docstring included. That is the one place a docstring counts as
    code, so a bare-module citation may rest on it alone (the evidence gates list
    each such citation with its reason); every named construct's docstring is prose,
    never evidence.
    """
    module, qualname = split(citation)
    if not module.endswith(".py"):
        return f"{citation!r} names no Python module"
    path = resolve_module(module, root=root)
    if isinstance(path, str):
        return f"{citation!r}: {path}"
    parsed = parse_file(path)
    if not qualname:
        nodes = tuple(node for node in parsed.tree.body if not isinstance(node, _DEFINITIONS + _IMPORTS))
        return Construct("", 1, len(parsed.lines) - 2, nodes)
    entries = parsed.table.get(qualname, [])
    if not entries:
        return f"{citation!r}: {module} defines no `{qualname}` (an imported name is no construct)"
    if len(entries) > 1:
        lines = ", ".join(str(entry.start) for entry in entries)
        return (
            f"{citation!r}: `{qualname}` is bound {len(entries)} times in {module} (lines {lines}); "
            "cite the enclosing construct"
        )
    return entries[0]


def citation_failure(citation: str, *, root: Path = PACKAGE_ROOT) -> str:
    """``""`` when *citation* resolves, the refusal otherwise."""
    resolved = resolve(citation, root=root)
    return resolved if isinstance(resolved, str) else ""


def _docstring(node: ast.AST) -> ast.AST | None:
    body = getattr(node, "body", None)
    if (
        isinstance(body, list)
        and body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[0]
    return None


def walk_construct(construct: Construct, *, bare: bool = False) -> Iterator[ast.AST]:
    """Every node of *construct*, imports and docstrings excluded.

    With *bare* (a bare-module citation), nested definitions are excluded too:
    they are constructs of their own.
    """
    pending: list[ast.AST] = list(construct.nodes)
    skipped: set[int] = set()
    while pending:
        node = pending.pop()
        if isinstance(node, _IMPORTS) or id(node) in skipped:
            continue
        if bare and isinstance(node, _DEFINITIONS):
            continue
        docstring = _docstring(node)
        if docstring is not None:
            skipped.add(id(docstring))
        yield node
        pending.extend(ast.iter_child_nodes(node))


def referenced_names(construct: Construct, *, bare: bool = False) -> set[str]:
    """Every ``Name`` id and ``Attribute`` attr in *construct*, outside imports and docstrings."""
    names: set[str] = set()
    for node in walk_construct(construct, bare=bare):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _mentions_outside_code(construct: Construct, leaf: str) -> list[str]:
    """Where *leaf* occurs that the refusal does not count: an import, a docstring."""
    places: list[str] = []
    for root in construct.nodes:
        for node in ast.walk(root):
            if isinstance(node, _IMPORTS) and any(
                (alias.asname or alias.name).split(".")[-1] == leaf or alias.name.split(".")[-1] == leaf
                for alias in node.names
            ):
                places.append("an import")
            docstring = _docstring(node)
            if isinstance(docstring, ast.Expr) and isinstance(docstring.value, ast.Constant):
                if leaf in str(docstring.value.value):
                    places.append("a docstring")
    return sorted(set(places))


def construct_failures(citation: str, anchor: str, *, root: Path = PACKAGE_ROOT) -> list[str]:
    """Why *citation* does not land on *anchor*, the ``def``/``class`` its sentence names.

    It passes when the citation **is** the anchor (its qualname ends in the
    anchor's dotted name), or when the anchor's leaf name occurs as a ``Name`` or
    an ``Attribute`` inside the cited construct -- a call site names its callee.
    An import or a docstring that mentions the anchor does not count: the
    evidence rule cites the call, not the import.
    """
    resolved = resolve(citation, root=root)
    if isinstance(resolved, str):
        return [resolved]
    module, qualname = split(citation)
    parts = anchor_parts(module, anchor, root=root)
    dotted = ".".join(parts)
    if qualname and (qualname == dotted or qualname.endswith("." + dotted)):
        return []
    if parts[-1] in referenced_names(resolved, bare=not qualname):
        return []
    elsewhere = _mentions_outside_code(resolved, parts[-1])
    where = f" (only in {' and '.join(elsewhere)})" if elsewhere else ""
    return [f"{citation}: `{anchor}` is neither the cited construct nor referenced in it{where}"]


#: The two seams a CapWords name is cut at to spell its snake_case twin:
#: ``SafetyReading`` -> ``safety_reading``, ``SWComponent`` -> ``sw_component``.
_SNAKE_SEAMS = (re.compile(r"(.)([A-Z][a-z]+)"), re.compile(r"([a-z0-9])([A-Z])"))


def snake_case(name: str) -> str:
    """*name* as a factory function named after the class would spell it."""
    for seam in _SNAKE_SEAMS:
        name = seam.sub(r"\1_\2", name)
    return name.lower()


def called_names(construct: Construct, *, bare: bool = False) -> set[str]:
    """The leaf name of every callee in *construct*: ``f`` for ``f()``, ``g`` for ``x.g()``."""
    names: set[str] = set()
    for node in walk_construct(construct, bare=bare):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def construction_failures(citation: str, class_name: str, *, root: Path = PACKAGE_ROOT) -> list[str]:
    """Why *citation* does not construct *class_name*, the class its sentence says is built there.

    It passes when the cited construct calls the class or its :func:`snake_case`
    twin -- ``safety_reading()`` builds a ``SafetyReading`` just as the class's
    own call does. A call inside an import or a docstring cannot occur, and a
    mention of the class that is not a call (an annotation, an ``isinstance``)
    does not count.
    """
    resolved = resolve(citation, root=root)
    if isinstance(resolved, str):
        return [resolved]
    callees = called_names(resolved, bare=not split(citation)[1])
    if class_name in callees or snake_case(class_name) in callees:
        return []
    return [
        f"{citation}: `{class_name}` is said to be constructed there, and the construct calls "
        f"neither it nor its factory twin `{snake_case(class_name)}`"
    ]


def enclosing_qualname(module: Module, line: int) -> str:
    """The qualname of the innermost resolvable construct holding *line*; ``""`` at module level."""
    return module.lines[line] if 0 < line < len(module.lines) else ""


def qualname_at(path: Path, line: int) -> str:
    """:func:`enclosing_qualname` of *path*'s line *line*."""
    return enclosing_qualname(parse_file(path), line)


def format_symbol(module: str, qualname: str) -> str:
    """The symbol citation ``module::qualname``, or the bare *module* when *qualname* is empty."""
    return f"{module}::{qualname}" if qualname else module


def enclosing_symbol(path: Path, line: int, *, root: Path = PACKAGE_ROOT) -> str:
    """The symbol citation of *path*'s line *line*: ``module::qualname``, or the bare module.

    A construct whose qualname is bound twice is skipped for the next enclosing
    one, so the answer always resolves.
    """
    module = path.resolve().relative_to(root.resolve()).as_posix()
    return format_symbol(module, qualname_at(path, line))
