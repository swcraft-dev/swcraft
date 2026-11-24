"""The emission scan: one module's source, read for where each consistency rule is reported.

Test support, never part of the product. The model contract's conformance gate
(``test_model_contract_conformance.py::emission_layers``) builds one scan per emitting
module and asks it for the layers each emission site's subjects name.
"""

from __future__ import annotations

import ast
import symtable
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_FUNCTIONS = ast.FunctionDef | ast.AsyncFunctionDef
_COMPREHENSIONS = ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
_SCOPES = ast.Module | _FUNCTIONS | ast.ClassDef | ast.Lambda | _COMPREHENSIONS

#: The name `symtable` gives the scope of each anonymous scope node.
_TABLE_NAMES: dict[type[ast.AST], str] = {
    ast.Lambda: "lambda",
    ast.ListComp: "listcomp",
    ast.SetComp: "setcomp",
    ast.DictComp: "dictcomp",
    ast.GeneratorExp: "genexpr",
}

#: The decorators that keep the body's return value: what their name imports, and the builtin.
_KEEPING_DECORATORS = frozenset({"functools.cache", "functools.lru_cache"})
_KEEPING_BUILTIN_DECORATORS = frozenset({"staticmethod"})

#: What the names the scan reads specially must import: the subject class and the registry accessor.
SUBJECT_CLASS = "swcraft_tools.diagnostics.Subject"
CURRENT_REGISTRY = "swcraft_tools.model_registry.current_registry"

#: The fields of a statement that hold a block of statements.
_BLOCK_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")
_LOOPS = ast.For | ast.AsyncFor | ast.While

#: How deep one reading may nest before it is unreadable: the bound that ends a cyclic chain of calls or bindings.
_MAX_DEPTH = 12

#: The calls whose result holds the elements of their one argument.
_COPYING_CALLS = frozenset({"tuple", "list", "set", "sorted"})

#: The builtins, and the list method, that run a generator expression passed to them before they return.
_CONSUMING_CALLS = _COPYING_CALLS | frozenset({"frozenset", "dict", "any", "all", "sum", "min", "max"})
_CONSUMING_METHODS = frozenset({"extend"})

#: The builtins that read an argument and never change it.
_READING_CALLS = _COPYING_CALLS | frozenset(
    "len bool isinstance any all enumerate zip reversed iter next frozenset dict str repr sum min max id hash"
    " type".split()
)

#: The methods whose argument the scan folds into the collection they are called on.
_FILLING_METHODS = frozenset({"append", "extend"})

#: The other methods that change a list, set or dict in place.
_MUTATORS = frozenset(
    "insert remove pop clear sort reverse add update discard setdefault popitem difference_update intersection_update"
    " symmetric_difference_update __setitem__ __delitem__ __iadd__ __ior__ __imul__ __iand__ __isub__ __ixor__".split()
)

#: The methods that store their argument, or its items, as items of the collection they are called on.
_STORING_METHODS = frozenset({"append", "extend", "add", "insert"})

#: The methods that read a dict, list or set and never change it.
_READING_METHODS = frozenset({"get", "keys", "values", "items", "copy", "index", "count"})

#: The members a value the scan reads may be used through: the reading methods, and the members of the frozen
#: `Subject` (none changes it; no list, tuple, set or dict has one of these names). Any other is unreadable.
_UNCHANGING_MEMBERS = _READING_METHODS | frozenset({"at", "layer", "element", "path", "to_json"})

#: The expressions that hand an operand's value on unchanged, and those that hold it in a new container.
_PASSING = ast.Starred | ast.Await | ast.BoolOp
_HOLDING = ast.Tuple | ast.List | ast.Set | ast.Dict | _COMPREHENSIONS

#: The consumers that read an operand and keep no reference that could change it later.
_READING_CONSUMERS = (
    ast.Return
    | ast.Yield
    | ast.YieldFrom
    | ast.Expr
    | ast.Compare
    | ast.UnaryOp
    | ast.BinOp
    | ast.FormattedValue
    | ast.If
    | ast.While
    | ast.Assert
    | ast.IfExp
    | ast.AugAssign
    | ast.keyword
    | ast.Raise
)


def _outer_parts(scope: ast.AST) -> list[ast.AST]:
    """Return the parts of *scope* that Python evaluates in the scope enclosing it, in `symtable`'s order."""
    if isinstance(scope, _FUNCTIONS | ast.Lambda):
        arguments = scope.args
        parts: list[ast.AST] = [*arguments.defaults, *(value for value in arguments.kw_defaults if value is not None)]
        if isinstance(scope, _FUNCTIONS):
            every = (*arguments.posonlyargs, *arguments.args, arguments.vararg, arguments.kwarg, *arguments.kwonlyargs)
            parts += [argument.annotation for argument in every if argument is not None and argument.annotation]
            parts += [scope.returns] if scope.returns is not None else []
            parts += scope.decorator_list
        return parts
    if isinstance(scope, ast.ClassDef):
        return [*scope.bases, *(keyword.value for keyword in scope.keywords), *scope.decorator_list]
    if isinstance(scope, _COMPREHENSIONS):
        return [scope.generators[0].iter]
    return []


def constructs(tree: ast.Module) -> dict[ast.AST, str]:
    """Return the qualified name of the `def` or `class` each node of *tree* is evaluated in, in source order.

    ``""`` stands for module level. A decorator, default, annotation, base or class keyword belongs to the
    enclosing construct; a lambda or a comprehension is no construct of its own.
    """
    owner: dict[ast.AST, str] = {}

    def place(node: ast.AST, construct: str, skip: frozenset[int]) -> None:
        if id(node) in skip:
            return
        owner[node] = construct
        if not isinstance(node, _FUNCTIONS | ast.ClassDef):
            for child in ast.iter_child_nodes(node):
                place(child, construct, skip)
            return
        outer = _outer_parts(node)
        for part in outer:
            place(part, construct, frozenset())
        inner = f"{construct}.{node.name}" if construct else node.name
        for child in ast.iter_child_nodes(node):
            place(child, inner, frozenset(id(part) for part in outer))

    for child in ast.iter_child_nodes(tree):
        place(child, "", frozenset())
    return owner


def _children(node: ast.AST) -> list[ast.AST]:
    """Return *node*'s children in the order `symtable` visits them: a comprehension's element last."""
    if isinstance(node, ast.DictComp):
        return [*node.generators, node.value, node.key]
    if isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp):
        return [*node.generators, node.elt]
    return list(ast.iter_child_nodes(node))


def _bound_name(node: ast.AST) -> str | None:
    """Return the name *node* binds in its scope, or `None` when it binds none."""
    if isinstance(node, _FUNCTIONS | ast.ClassDef):
        return node.name
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
        return node.id
    if isinstance(node, ast.arg):
        return node.arg
    if isinstance(node, ast.alias):
        return node.asname or node.name.split(".")[0]
    if isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
        return node.name
    if isinstance(node, ast.MatchMapping):
        return node.rest
    return None


def _imported_name(statement: ast.Import | ast.ImportFrom, alias: ast.alias) -> str | None:
    """Return the dotted name *alias* binds: `from m import a` binds `m.a`, `import a.b` binds `a`; relative, `None`."""
    if isinstance(statement, ast.ImportFrom):
        return None if statement.level or statement.module is None else f"{statement.module}.{alias.name}"
    return alias.name if alias.asname else alias.name.split(".")[0]


def _position(node: ast.AST) -> tuple[int, int]:
    return getattr(node, "lineno", 0), getattr(node, "col_offset", 0)


def _end(node: ast.AST) -> tuple[int, int]:
    return getattr(node, "end_lineno", 0) or 0, getattr(node, "end_col_offset", 0) or 0


class Scopes:
    """The scopes of one module, paired with `symtable`'s, and the names each scope binds.

    Every node is placed in the scope Python evaluates it in: a function's or lambda's body in the function; a
    decorator, default and annotation of a `def`, a class's bases and keywords, and a comprehension's first iterable
    in the enclosing scope; a class body in the class.
    """

    def __init__(self, tree: ast.Module, source: str) -> None:
        self.scope_of: dict[ast.AST, ast.AST] = {}
        self.imports: dict[ast.AST, str | None] = {
            alias: _imported_name(statement, alias)
            for statement in ast.walk(tree)
            if isinstance(statement, ast.Import | ast.ImportFrom)
            for alias in statement.names
        }
        self.bound: dict[ast.AST, dict[str, list[ast.AST]]] = {tree: {}}
        self.nested: dict[ast.AST, list[ast.AST]] = {tree: []}
        for child in ast.iter_child_nodes(tree):
            self._place(child, tree, frozenset())
        self.tables: dict[ast.AST, symtable.SymbolTable] = {}
        self._pair(tree, symtable.symtable(source, "<emission scan>", "exec"))
        self._resolved: dict[tuple[str, ast.AST], _FUNCTIONS | None] = {}
        self.consumed_at_once = {
            argument
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and self._consumes(node)
            for argument in node.args
            if isinstance(argument, ast.GeneratorExp)
        }

    def _consumes(self, call: ast.Call) -> bool:
        """Return whether *call* runs a generator expression it is passed before it returns."""
        function = call.func
        if isinstance(function, ast.Attribute):
            return function.attr in _CONSUMING_METHODS
        return isinstance(function, ast.Name) and function.id in _CONSUMING_CALLS and self.is_builtin(function.id, call)

    def _place(self, node: ast.AST, scope: ast.AST, skip: frozenset[int]) -> None:
        if id(node) in skip:
            return
        self.scope_of[node] = scope
        name = _bound_name(node)
        if name is not None:
            self.bound[scope].setdefault(name, []).append(node)
            if isinstance(node, ast.Name) and isinstance(scope, _COMPREHENSIONS):
                self._bind_walrus_target(node, scope)
        if not isinstance(node, _SCOPES):
            for child in _children(node):
                self._place(child, scope, skip)
            return
        outer = _outer_parts(node)
        for part in outer:
            self._place(part, scope, skip)
        self.nested[scope].append(node)
        self.bound[node] = {}
        self.nested[node] = []
        inner = frozenset(id(part) for part in outer)
        for child in _children(node):
            self._place(child, node, inner)

    def _bind_walrus_target(self, node: ast.Name, scope: ast.AST) -> None:
        """Count an assignment expression's target in a comprehension where Python binds it as well."""
        if not any(isinstance(parent, ast.NamedExpr) and parent.target is node for parent in ast.walk(scope)):
            return
        enclosing = self.scope_of.get(scope)
        while isinstance(enclosing, _COMPREHENSIONS):
            enclosing = self.scope_of.get(enclosing)
        if enclosing is not None:
            self.bound[enclosing].setdefault(node.id, []).append(node)

    def _pair(self, scope: ast.AST, table: symtable.SymbolTable) -> None:
        """Pair each scope nested in *scope* with its table, by name and line, in `symtable`'s visiting order."""
        self.tables[scope] = table
        children: dict[tuple[str, int], list[symtable.SymbolTable]] = {}
        for child_table in table.get_children():
            children.setdefault((child_table.get_name(), child_table.get_lineno()), []).append(child_table)
        for node in self.nested[scope]:
            name = node.name if isinstance(node, _FUNCTIONS | ast.ClassDef) else _TABLE_NAMES[type(node)]
            candidates = children.get((name, node.lineno), [])  # type: ignore[attr-defined]
            if candidates:
                self._pair(node, candidates.pop(0))

    def _outward(self, scope: ast.AST) -> ast.AST | None:
        """Return the scope a free name of *scope* is looked up in next: the enclosing one, past class bodies."""
        enclosing = self.scope_of.get(scope)
        while isinstance(enclosing, ast.ClassDef):
            enclosing = self.scope_of.get(enclosing)
        return enclosing

    def resolve(self, name: str, use: ast.AST) -> _FUNCTIONS | None:
        """Return the `def` a bare *name* evaluated at *use* binds to, or `None` when no single `def` does.

        In a class body that binds *name*, Python finds the class's binding only once it has run: a `def` standing
        before the use resolves, a name the class binds only after the use is looked up in the module.
        """
        scope = self.scope_of.get(use)
        if scope is None:
            return None
        if isinstance(scope, ast.ClassDef) and self.binding_scope(name, scope) is scope:
            return self._class_body_def(name, use, scope)
        key = (name, scope)
        if key not in self._resolved:
            bound = self.binding_scope(name, scope)
            rebound = bound is None or self.rebound_inside(name, bound)
            self._resolved[key] = None if bound is None or rebound else self._only_def(bound, name)
        return self._resolved[key]

    def _class_body_def(self, name: str, use: ast.AST, scope: ast.ClassDef) -> _FUNCTIONS | None:
        bindings = self.bound[scope][name]
        if not all(any(binding is statement for statement in scope.body) for binding in bindings):
            return None
        before = [binding for binding in bindings if _end(binding) <= _position(use)]
        if before:
            return self._only_def(scope, name) if len(bindings) == 1 else None
        module = self._module(scope)
        if self.binding_scope(name, module) is not module or self.rebound_inside(name, module):
            return None
        return self._only_def(module, name)

    def import_of(self, name: str, use: ast.AST) -> str | None:
        """Return the dotted name a bare *name* evaluated at *use* imports, when an import is its one binding."""
        bound = self.binding_scope(name, self.scope_of.get(use), declared=True)
        if bound is None or isinstance(bound, ast.ClassDef) or self.rebound_inside(name, bound):
            return None
        bindings = self.bound[bound].get(name, [])
        return self.imports.get(bindings[0]) if len(bindings) == 1 else None

    def module_constant(self, name: str, use: ast.AST) -> bool:
        """Return whether a bare *name* evaluated at *use* is a module name bound once, so the module's value is its."""
        scope = self.scope_of.get(use)
        bound = None if scope is None else self.binding_scope(name, scope)
        return (
            isinstance(bound, ast.Module)
            and len(self.bound[bound].get(name, [])) == 1
            and not self.rebound_inside(name, bound)
        )

    def runs_in_place(self, scope: ast.AST, bound: ast.AST) -> bool:
        """Return whether every scope from *scope* out to *bound* is a comprehension that runs where it stands.

        A generator expression runs when it is consumed; only one passed straight to a builtin that copies or folds it
        (`tuple`, `sorted`, `any`, …) or to `.extend` runs where it stands.
        """
        current: ast.AST | None = scope
        while (
            current is not bound
            and isinstance(current, _COMPREHENSIONS)
            and (not isinstance(current, ast.GeneratorExp) or current in self.consumed_at_once)
        ):
            current = self._outward(current)
        return current is bound

    def binding_scope(self, name: str, scope: ast.AST | None, *, declared: bool = False) -> ast.AST | None:
        """Return the scope a *name* evaluated in *scope* is bound in, or `None` (unbound, or not found).

        A `global` or `nonlocal` name is `None`, or with *declared* the module or the enclosing binding scope.
        """
        while scope is not None:
            table = self.tables.get(scope)
            if table is None:
                return None
            symbol = table.lookup(name) if name in table.get_identifiers() else None
            if symbol is not None and symbol.is_declared_global():
                return self._module(scope) if declared else None
            if symbol is not None and symbol.is_nonlocal() and not declared:
                return None
            if symbol is not None and symbol.is_local():
                return scope
            scope = self._outward(scope)
        return None

    def _module(self, scope: ast.AST) -> ast.AST:
        while not isinstance(scope, ast.Module):
            scope = self.scope_of[scope]
        return scope

    def is_builtin(self, name: str, use: ast.AST) -> bool:
        """Return whether a bare *name* evaluated at *use* is the builtin: no scope it can be looked up in binds it."""
        scope = self.scope_of.get(use)
        if scope is None or self.binding_scope(name, scope, declared=True) is not None:
            return False
        return not any(
            name in table.get_identifiers() and table.lookup(name).is_declared_global()
            for table in self.tables.values()
        )

    def rebound_inside(self, name: str, bound: ast.AST) -> bool:
        """Return whether a scope nested in *bound* assigns *name* by `nonlocal` (or `global`, *bound* a module)."""
        for scope, table in self.tables.items():
            if scope is bound or name not in table.get_identifiers():
                continue
            symbol = table.lookup(name)
            declared = symbol.is_nonlocal() or (isinstance(bound, ast.Module) and symbol.is_declared_global())
            if declared and symbol.is_assigned() and self._inside(scope, bound):
                return True
        return False

    def _inside(self, scope: ast.AST, bound: ast.AST) -> bool:
        current = self.scope_of.get(scope)
        while current is not None and current is not bound:
            current = self.scope_of.get(current)
        return current is bound

    def _only_def(self, scope: ast.AST, name: str) -> _FUNCTIONS | None:
        bindings = self.bound[scope].get(name, [])
        if len(bindings) != 1 or not isinstance(bindings[0], _FUNCTIONS):
            return None
        function = bindings[0]
        return function if all(self._keeps_return(decorator) for decorator in function.decorator_list) else None

    def _keeps_return(self, decorator: ast.expr) -> bool:
        """Return whether *decorator* is one that keeps the body's return value, by what its name is bound to."""
        spelled = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(spelled, ast.Name):
            if spelled.id in _KEEPING_BUILTIN_DECORATORS and self.is_builtin(spelled.id, spelled):
                return True
            return self.import_of(spelled.id, spelled) in _KEEPING_DECORATORS
        if isinstance(spelled, ast.Attribute) and isinstance(spelled.value, ast.Name):
            module = self.import_of(spelled.value.id, spelled.value)
            return f"{module}.{spelled.attr}" in _KEEPING_DECORATORS
        return False

    def unpaired(self) -> list[int]:
        """Return the line of each scope paired with no `symtable` table; a name evaluated there resolves nothing."""
        return sorted(getattr(scope, "lineno", 0) for scope in self.bound if scope not in self.tables)

    def collisions(self) -> list[str]:
        """Return `<scope>: <name>` for each module or function scope that binds one name with two `def`s."""
        found: list[str] = []
        for scope, names in self.bound.items():
            if not isinstance(scope, ast.Module | _FUNCTIONS):
                continue
            label = "<module>" if isinstance(scope, ast.Module) else scope.name
            found += [
                f"{label}: {name}"
                for name, bindings in names.items()
                if sum(isinstance(binding, _FUNCTIONS) for binding in bindings) > 1
            ]
        return sorted(found)


#: An active call: the callee whose return expressions are being read, and the call that passes its arguments.
_Frame = tuple[ast.AST, ast.Call]


@dataclass(frozen=True)
class _Parameter:
    """A name bound to a parameter of *function*: positional at *index*, or keyword-only (`None`)."""

    function: ast.AST
    index: int | None
    name: str


@dataclass(frozen=True)
class _Element:
    """A name a `for` or comprehension target binds: the item at *path* of each element *iterable* yields."""

    iterable: ast.expr
    path: tuple[int, ...]
    loop: ast.AST


@dataclass(frozen=True)
class _Item:
    """A name an unpacking assignment binds: the item at *path* of *value*."""

    value: ast.expr
    path: tuple[int, ...]


@dataclass(frozen=True)
class _Augmented:
    """A name `target += value` binds: what it held before, and the elements of *value*."""

    target: ast.Name
    value: ast.expr


@dataclass(frozen=True)
class _Fill:
    """What a use adds to a collection in place: *value* itself (*whole*) or its elements, read through *frames*."""

    value: ast.expr
    whole: bool
    frames: tuple[_Frame, ...]


#: What one binding of a name gives it: an assigned value, a parameter, a loop element, an unpacked item or a `+=`.
_Reading = ast.expr | _Parameter | _Element | _Item | _Augmented


class EmissionScan:
    """One module's source, read for where each consistency rule is reported.

    An **emission site** is any call passing ``subjects=``; the scan answers the layers its subjects name for **the
    shapes it reads, each resolved through scopes** (:class:`Scopes`); **every other shape answers ``None``**.

    The shapes: a call of the name importing `swcraft_tools.diagnostics.Subject`, its layer a literal, a module name
    bound once or an item of a module table only ever read; `.at(…)` to its receiver; tuples, lists, sets, their
    comprehensions and copies by an unshadowed builtin; a conditional (for a layer, `or`/`and` too) as all its arms;
    a call of the one `def` its name binds, decorated by nothing but the builtin `staticmethod` or an imported
    `functools.cache`/`lru_cache`, as its `return`s, or a generator's `yield`s, the parameters bound to the
    arguments; in a class body, a `def` standing before the use, else the module's; a parameter as the argument of
    the call being read, else of every call site of a module-private `def` whose every call the scan sees (no
    method, no `def` read other than as a callee or a reporter field); a `param.field(…)` call on a parameter
    annotated with a reporter class (a `typing.NamedTuple` of fields alone) with every construction and instance
    seen. A value name is the union of its reaching bindings: an assigned value, an unpacked item, a parameter, a
    loop or comprehension target's position in what its iterable yields, a `+=`; an assignment expression ends no
    earlier one; a generator expression not consumed at once reads free names as a nested `def`. A collection is
    read whole with what each use adds in place (:meth:`_fills`).

    ``None`` for anything else: a binding it does not read (never skipped for an earlier one), a use that could
    change a value unseen (a member off a closed list of reading ones, a display unpacked into names), a coroutine.
    Not reached: a call from another module of a module-private `def`, or through a reporter instance; a change
    through another object (`setattr`, `globals()`, a monkeypatch, an item of a container holding the value, a
    receiver filling a list a `return` or `yield` handed out); a constructor changing the `subjects=` it takes;
    `registry` passed in to `registry or current_registry()`; a `break` or `return` cutting a path from a binding or
    fill to its use (the union names more).

    `Subject(layer, …)` in the body of `for layer in registry.layer_names:`, `registry` bound to an imported
    `current_registry()` or `registry or current_registry()`, answers *declared_layers*; without them, `None`.
    """

    def __init__(
        self,
        source: str,
        namespace: Mapping[str, Any],
        *,
        declared_layers: frozenset[str] | None = None,
    ) -> None:
        self.namespace = namespace
        self.declared_layers = declared_layers
        self.tree = ast.parse(source)
        self.parents: dict[ast.AST, ast.AST] = {}
        self._index(self.tree)
        self.scopes = Scopes(self.tree, source)
        self._audited: dict[tuple[str, ast.AST], tuple[_Fill, ...] | None] = {}
        self.reporters = self._reporter_fields()

    def _reporter_fields(self) -> dict[tuple[ast.ClassDef, str], set[_FUNCTIONS]]:
        """Return `(class, field) -> the functions of this module a construction of the class binds to the field`.

        A reporter class is a module class bound once whose constructor stores each field as passed
        (:meth:`_stores_as_passed`). It counts only when every reference is a construction or an annotation, every
        construction passes each field by keyword as a `def` of this module, and every instance stays where the scan
        sees its calls (:meth:`_instance_stays`); any other class gives no call a target.
        """
        fields: dict[tuple[ast.ClassDef, str], set[_FUNCTIONS]] = {}
        self._reporter_values: set[ast.AST] = set()
        for name, bindings in self.scopes.bound[self.tree].items():
            node = bindings[0]
            declared = [item for item in getattr(node, "body", []) if isinstance(item, ast.AnnAssign)]
            if len(bindings) != 1 or not isinstance(node, ast.ClassDef) or not self._stores_as_passed(node):
                continue
            names = {item.target.id for item in declared if isinstance(item.target, ast.Name) and item.value is None}
            bound = self._constructions(node, name, names) if len(names) == len(declared) else None
            if bound is None or not self._instance_stays(node, names):
                continue
            for keyword, function in bound:
                fields.setdefault((node, keyword.arg or ""), set()).add(function)
                self._reporter_values.add(keyword.value)
        return fields

    def _stores_as_passed(self, node: ast.ClassDef) -> bool:
        """Return whether *node* is a `typing.NamedTuple` of fields alone, whose constructor stores what it is passed.

        No decorator, no keyword, one base resolving to the imported `typing.NamedTuple`, and a body of a docstring
        and at least one field (`field: Type`, no default); any other class has a constructor the scan does not read.
        """
        base = node.bases[0] if len(node.bases) == 1 else None
        body = node.body[1:] if ast.get_docstring(node, clean=False) is not None else node.body
        return (
            isinstance(base, ast.Name)
            and self.scopes.import_of(base.id, base) == "typing.NamedTuple"
            and not node.decorator_list
            and not node.keywords
            and bool(body)
            and all(isinstance(item, ast.AnnAssign) and item.value is None for item in body)
        )

    def _references(self, name: str, scope: ast.AST) -> list[ast.Name]:
        """Return every read of the bare *name* that resolves to its binding in *scope*."""
        return [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, ast.Load)
            and self.scopes.binding_scope(name, self.scopes.scope_of.get(node), declared=True) is scope
        ]

    def _constructions(
        self, node: ast.ClassDef, name: str, names: set[str]
    ) -> list[tuple[ast.keyword, _FUNCTIONS]] | None:
        """Return each field keyword of every construction of *node* with its `def`, or `None` if one is not that."""
        bound: list[tuple[ast.keyword, _FUNCTIONS]] = []
        for reference in self._references(name, self.tree):
            call = self.parents.get(reference)
            if self._in_annotation(reference):
                continue
            if not (isinstance(call, ast.Call) and call.func is reference) or call.args:
                return None
            if sorted(keyword.arg or "" for keyword in call.keywords) != sorted(names):
                return None
            for keyword in call.keywords:
                value = keyword.value
                function = self.scopes.resolve(value.id, value) if isinstance(value, ast.Name) else None
                if function is None:
                    return None
                bound.append((keyword, function))
        return bound

    def _in_annotation(self, node: ast.AST) -> bool:
        child, parent = node, self.parents.get(node)
        while parent is not None and not isinstance(parent, ast.stmt):
            if isinstance(parent, ast.arg) and child is parent.annotation:
                return True
            child, parent = parent, self.parents.get(parent)
        if isinstance(parent, _FUNCTIONS):
            return child is parent.returns
        return isinstance(parent, ast.AnnAssign) and child is parent.annotation

    def _annotated_class(self, annotation: ast.expr | None) -> ast.ClassDef | None:
        """Return the module class a bare-name *annotation* resolves to, or `None`."""
        if not isinstance(annotation, ast.Name):
            return None
        scope = self.scopes.binding_scope(annotation.id, self.scopes.scope_of.get(annotation), declared=True)
        bindings = self.scopes.bound[scope].get(annotation.id, []) if scope is self.tree else []
        return bindings[0] if len(bindings) == 1 and isinstance(bindings[0], ast.ClassDef) else None

    def _instance_stays(self, node: ast.ClassDef, names: set[str]) -> bool:
        """Return whether every instance of *node* is used only where the scan sees what calls its fields.

        An instance is a construction, a call of a `def` annotated to return the class, or a parameter annotated with
        it of a module-private function. Each one may be returned by a function annotated to return the class, bound
        once to a local name, passed to a parameter annotated with the class, or, as a parameter, have a field called.
        """
        functions = [function for function in ast.walk(self.tree) if isinstance(function, _FUNCTIONS)]
        held: list[ast.expr] = [
            call
            for call in ast.walk(self.tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and (
                (call.func.id == node.name and self._annotated_class(call.func) is node)
                or self._returns_class(self.scopes.resolve(call.func.id, call), node)
            )
        ]
        for function in functions:
            signature = function.args
            for argument in (*signature.posonlyargs, *signature.args, *signature.kwonlyargs):
                if self._annotated_class(argument.annotation) is not node:
                    continue
                if self.scopes.scope_of.get(function) is not self.tree or not function.name.startswith("_"):
                    return False
                if len(self.scopes.bound[function].get(argument.arg, [])) != 1:
                    return False
                held += self._references(argument.arg, function)
        return all(self._instance_use_stays(value, node, names) for value in held)

    def _returns_class(self, function: _FUNCTIONS | None, node: ast.ClassDef) -> bool:
        return function is not None and self._annotated_class(function.returns) is node

    def _instance_use_stays(self, value: ast.expr, node: ast.ClassDef, names: set[str]) -> bool:
        parent = self.parents.get(value)
        if isinstance(parent, ast.Return):
            function = self.scopes.scope_of.get(parent)
            return isinstance(function, _FUNCTIONS) and self._returns_class(function, node)
        if isinstance(parent, ast.Assign) and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name):
            target = parent.targets[0]
            scope = self.scopes.scope_of.get(target)
            if scope is None or len(self.scopes.bound[scope].get(target.id, [])) != 1:
                return False
            return all(self._instance_use_stays(use, node, names) for use in self._references(target.id, scope))
        if isinstance(parent, ast.Attribute) and isinstance(value, ast.Name) and parent.attr in names:
            grand = self.parents.get(parent)
            return isinstance(grand, ast.Call) and grand.func is parent and self._parameter_annotation(value) is node
        call = self.parents.get(parent) if isinstance(parent, ast.keyword) else parent
        argument = parent if isinstance(parent, ast.keyword) else value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func is not value):
            return False
        callee = self.scopes.resolve(call.func.id, call)
        name = None if callee is None else self._parameter_passed(callee, call, argument)
        signature = None if callee is None else callee.args
        every = [] if signature is None else (*signature.posonlyargs, *signature.args, *signature.kwonlyargs)
        return any(each.arg == name and self._annotated_class(each.annotation) is node for each in every)

    def _parameter_annotation(self, name: ast.Name) -> ast.ClassDef | None:
        """Return the module class *name*'s parameter is annotated with, when *name* is bound to a parameter."""
        parameter = self._parameter_of(name)
        if parameter is None:
            return None
        signature = parameter.function.args  # type: ignore[attr-defined]
        arguments = signature.posonlyargs + signature.args + signature.kwonlyargs
        annotation = next((argument.annotation for argument in arguments if argument.arg == parameter.name), None)
        return self._annotated_class(annotation)

    def _escapes(self, function: _FUNCTIONS) -> bool:
        """Return whether *function* can be called where the scan does not see the call.

        A method, a public module function, and a `def` whose name is read other than as a call's callee or as a
        reporter field's construction keyword can.
        """
        scope = self.scopes.scope_of.get(function)
        if scope is None or isinstance(scope, ast.ClassDef):
            return True
        if scope is self.tree and not function.name.startswith("_"):
            return True
        for reference in self._references(function.name, scope):
            parent = self.parents.get(reference)
            called = isinstance(parent, ast.Call) and parent.func is reference
            if not called and reference not in self._reporter_values:
                return True
        return False

    def _parameter_of(self, name: ast.Name) -> _Parameter | None:
        """Return the parameter *name* is bound to, when that is its one reaching binding."""
        readings = self._binding_of(name.id, name)
        return (
            readings[0] if readings is not None and len(readings) == 1 and isinstance(readings[0], _Parameter) else None
        )

    def _callers(self, target: ast.AST) -> list[ast.Call]:
        """Return every call of the `def` *target*: by a bare name bound to it, or a reporter parameter's field."""
        return [
            node for node in ast.walk(self.tree) if isinstance(node, ast.Call) and target in (self._called(node) or ())
        ]

    def _index(self, root: ast.AST) -> None:
        for node in ast.walk(root):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node

    def sites(self) -> list[ast.Call]:
        """Return every call that passes a ``subjects=`` keyword, in source order."""
        return [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call) and any(keyword.arg == "subjects" for keyword in node.keywords)
        ]

    @staticmethod
    def keyword(call: ast.Call, name: str) -> ast.expr | None:
        return next((keyword.value for keyword in call.keywords if keyword.arg == name), None)

    def keyword_strings(self, call: ast.Call) -> set[str] | None:
        """Return the strings *call*'s own keywords carry, `subjects` aside, whatever the keyword's name.

        A keyword that reads no string adds nothing; one whose conditional reads some arms and not others makes the
        answer `None`, since the arm it cannot read may name another rule.
        """
        found: set[str] = set()
        for keyword in call.keywords:
            if keyword.arg == "subjects":
                continue
            read = self.strings(keyword.value)
            if read is None and any(self.strings(arm) for arm in self._arms(keyword.value)):
                return None
            found |= read or set()
        return found

    def table_strings(self, call: ast.Call) -> set[str]:
        """Return the strings of every module-level dict, only ever read, that *call* subscripts.

        The fallback for a call whose rule identifier is decided one call away
        (the connector rules arrive as `result.rule`, but the call subscripts a
        table keyed by them). Keys and values are both offered and the caller
        keeps only what the contract declares. Consulted only when the keywords
        identified nothing, so a table cannot broaden a site that named its rule.
        """
        strings: set[str] = set()
        for node in ast.walk(call):
            if not isinstance(node, ast.Subscript) or not isinstance(node.value, ast.Name):
                continue
            table = self._module_value(node.value)
            if isinstance(table, dict):
                strings |= {entry for entry in (*table, *table.values()) if isinstance(entry, str)}
        return strings

    def strings(self, expr: ast.expr | None) -> set[str] | None:
        """Return the string values *expr* can take, or `None` when one of them is not a literal reading.

        A literal, a module name bound once to a string, and a subscript of a module-level table
        (`_UNRESOLVED_ENDPOINT_RULES[...]`) that is only ever read, which answers every string value the table
        holds: the key is a runtime value, and over-naming a site makes the containment check run on more rules,
        never on fewer. A conditional or a boolean operation answers the union of its arms, `None` when any arm
        is `None`.
        """
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return {expr.value}
        if isinstance(expr, ast.IfExp | ast.BoolOp):
            arms = [self.strings(arm) for arm in self._arms(expr)]
            return None if any(arm is None for arm in arms) else set().union(*arms)  # type: ignore[arg-type]
        if isinstance(expr, ast.Name) and isinstance(self._module_value(expr), str):
            return {str(self._module_value(expr))}
        if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name):
            table = self._module_value(expr.value)
            if isinstance(table, dict):
                return {value for value in table.values() if isinstance(value, str)}
        return None

    @staticmethod
    def _arms(expr: ast.expr) -> list[ast.expr]:
        """Return the expressions a conditional or a boolean operation can evaluate to; else none."""
        if isinstance(expr, ast.IfExp):
            return [expr.body, expr.orelse]
        return list(expr.values) if isinstance(expr, ast.BoolOp) else []

    def _module_value(self, name: ast.Name) -> object:
        """Return the module's value of *name* when it resolves to a module name bound once, else `None`.

        A dict, list or set is answered only when every use of the name in the module reads it (:meth:`_only_read`).
        """
        if not self.scopes.module_constant(name.id, name):
            return None
        value = self.namespace.get(name.id)
        return None if isinstance(value, dict | list | set) and not self._only_read(name.id) else value

    def _only_read(self, name: str) -> bool:
        """Return whether every use of the module name *name*, past its one binding, reads it and changes nothing.

        A read is a subscript load, an iteration, an argument of an unshadowed builtin that only reads, a method
        that only reads, or the right operand of `in`; anything else could change the value unseen.
        """
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Name) or node.id != name or isinstance(node.ctx, ast.Store):
                continue
            scope = self.scopes.scope_of.get(node)
            if scope is None or self.scopes.binding_scope(name, scope, declared=True) is not self.tree:
                continue
            if not self._reads_only(node):
                return False
        return True

    def _reads_only(self, node: ast.Name) -> bool:
        parent = self.parents.get(node)
        grand = self.parents.get(parent) if parent is not None else None
        if isinstance(node.ctx, ast.Del):
            return False
        if isinstance(parent, ast.Subscript):
            return parent.value is node and isinstance(parent.ctx, ast.Load)
        if isinstance(parent, ast.For | ast.AsyncFor | ast.comprehension):
            return parent.iter is node
        if isinstance(parent, ast.Call):
            function = parent.func
            return (
                node in parent.args
                and isinstance(function, ast.Name)
                and function.id in _READING_CALLS
                and self.scopes.is_builtin(function.id, parent)
            )
        if isinstance(parent, ast.Attribute):
            return parent.attr in _READING_METHODS and isinstance(grand, ast.Call) and grand.func is parent
        if isinstance(parent, ast.Compare):
            return any(
                comparator is node and isinstance(op, ast.In | ast.NotIn)
                for op, comparator in zip(parent.ops, parent.comparators, strict=True)
            )
        return False

    def _binding_of(self, name: str, use: ast.AST, skip: ast.AST | None = None) -> list[_Reading] | None:
        """Return what each binding of *name* that can reach *use* gives it, or `None` when one of them cannot be read.

        The scope is the one Python binds *name* in (:class:`Scopes`). In the use's own scope, or one reached only
        through comprehensions, the reaching bindings are the nearest one above the use, every earlier one until
        one stands in no block the use is outside of, and every one below the use inside a loop that holds both.
        From a nested `def` or lambda the scope's binding must be its only one. *skip* is a binding left out.
        """
        scope = self.scopes.scope_of.get(use)
        bound = None if scope is None else self.scopes.binding_scope(name, scope)
        if scope is None or bound is None or self.scopes.rebound_inside(name, bound):
            return None
        bindings = [binding for binding in self.scopes.bound[bound].get(name, []) if binding is not skip]
        if isinstance(bound, _COMPREHENSIONS) or not self.scopes.runs_in_place(scope, bound):
            reaching = bindings if len(bindings) == 1 else []
        else:
            reaching = self._reaching(bindings, use, bound)
        readings = [self._reading(binding) for binding in reaching]
        return None if not readings or None in readings else readings  # type: ignore[return-value]

    def _blocks(self, node: ast.AST, scope: ast.AST) -> set[tuple[ast.AST, str]]:
        """Return the `(statement, block field)` pairs holding *node* inside *scope*; a loop's target is in its body."""
        blocks: set[tuple[ast.AST, str]] = set()
        child, parent = node, self.parents.get(node)
        while parent is not None and parent is not scope:
            if isinstance(parent, ast.For | ast.AsyncFor) and child is parent.target:
                blocks.add((parent, "body"))
            for field in _BLOCK_FIELDS:
                block = getattr(parent, field, None)
                if isinstance(block, list) and any(statement is child for statement in block):
                    blocks.add((parent, field))
            child, parent = parent, self.parents.get(parent)
        return blocks

    def _bound_at(self, binding: ast.AST) -> tuple[int, int]:
        """Return where *binding* takes effect: after its assignment's value, or its loop's iterable, is evaluated."""
        child, parent = binding, self.parents.get(binding)
        while isinstance(parent, ast.Tuple | ast.List | ast.Starred):
            child, parent = parent, self.parents.get(parent)
        if isinstance(parent, ast.Assign | ast.AugAssign | ast.AnnAssign | ast.NamedExpr):
            return parent.end_lineno or 0, parent.end_col_offset or 0
        if isinstance(parent, ast.For | ast.AsyncFor) and parent.target is child:
            return parent.iter.end_lineno or 0, parent.iter.end_col_offset or 0
        return _position(binding)

    def _reaching(self, bindings: list[ast.AST], use: ast.AST, scope: ast.AST) -> list[ast.AST]:
        """Return the bindings that can reach *use*; an assignment expression may stand unevaluated, so kills none."""
        holding = self._blocks(use, scope)
        reaching: list[ast.AST] = []
        above = [binding for binding in bindings if self._bound_at(binding) <= _position(use)]
        for binding in sorted(above, key=self._bound_at, reverse=True):
            reaching.append(binding)
            if self._blocks(binding, scope) <= holding and not isinstance(self.parents.get(binding), ast.NamedExpr):
                break
        loops = {statement for statement, field in holding if isinstance(statement, _LOOPS) and field == "body"}
        for binding in above:
            blocks = self._blocks(binding, scope)
            if blocks <= holding and not isinstance(self.parents.get(binding), ast.AugAssign | ast.NamedExpr):
                loops -= {statement for statement, _ in blocks}
        return reaching + [
            binding
            for binding in bindings
            if self._bound_at(binding) > _position(use)
            and any(statement in loops for statement, _ in self._blocks(binding, scope))
        ]

    def _reading(self, binding: ast.AST) -> _Reading | None:
        """Return what *binding* gives its name, or `None` for a binding the scan does not read."""
        if isinstance(binding, ast.arg):
            function = self.scopes.scope_of.get(binding)
            signature = getattr(function, "args", None)
            if function is None or signature is None:
                return None
            positional = signature.posonlyargs + signature.args
            if any(argument is binding for argument in positional):
                return _Parameter(function, next(i for i, a in enumerate(positional) if a is binding), binding.arg)
            if any(argument is binding for argument in signature.kwonlyargs):
                return _Parameter(function, None, binding.arg)
            return None
        if not isinstance(binding, ast.Name):
            return None
        path: tuple[int, ...] = ()
        child: ast.AST = binding
        parent = self.parents.get(binding)
        while isinstance(parent, ast.Tuple | ast.List) and isinstance(child, ast.Name | ast.Tuple | ast.List):
            if any(isinstance(element, ast.Starred) for element in parent.elts):
                return None
            path = (next(i for i, element in enumerate(parent.elts) if element is child), *path)
            child, parent = parent, self.parents.get(parent)
        if isinstance(parent, ast.For | ast.AsyncFor | ast.comprehension) and parent.target is child:
            return _Element(parent.iter, path, parent)
        if isinstance(parent, ast.Assign) and any(target is child for target in parent.targets):
            return _Item(parent.value, path) if path else parent.value
        if isinstance(parent, ast.AnnAssign | ast.NamedExpr) and parent.target is child and parent.value and not path:
            return parent.value
        if isinstance(parent, ast.AugAssign) and isinstance(parent.op, ast.Add) and parent.target is binding:
            return _Augmented(binding, parent.value)
        return None

    def _returns(
        self, function: _FUNCTIONS, call: ast.Call, depth: int, frames: tuple[_Frame, ...]
    ) -> frozenset[str] | None:
        frames = (*frames, (function, call))
        layers: set[str] = set()
        for node in ast.walk(function):
            if not isinstance(node, ast.Return) or node.value is None or self.scopes.scope_of.get(node) is not function:
                continue
            resolved = self.layers(node.value, depth + 1, frames)
            if resolved is None:
                return None
            layers |= resolved
        return frozenset(layers)

    @staticmethod
    def _argument(call: ast.Call, index: int | None, name: str) -> ast.expr | None:
        """Return the expression *call* passes for the parameter *name* at positional *index*.

        `None` when it cannot be told: no argument names it, or a starred argument stands at or before *index*.
        """
        named = [keyword.value for keyword in call.keywords if keyword.arg == name]
        if named:
            return named[0]
        starred = [at for at, value in enumerate(call.args) if isinstance(value, ast.Starred)]
        if index is None or index >= len(call.args) or (starred and starred[0] <= index):
            return None
        return call.args[index]

    def _passed(self, expr: ast.expr | None, frames: tuple[_Frame, ...]) -> tuple[ast.expr | None, tuple[_Frame, ...]]:
        """Return *expr*, or the argument an active call passes for the parameter it names, with the caller's frames."""
        while isinstance(expr, ast.Name):
            parameter = self._parameter_of(expr)
            position = None if parameter is None else self._active(parameter, frames)
            if parameter is None or position is None:
                break
            if self._name_fills(expr) != ():
                return None, frames
            expr, frames = self._argument(frames[position][1], parameter.index, parameter.name), frames[:position]
        return expr, frames

    @staticmethod
    def _active(parameter: _Parameter, frames: tuple[_Frame, ...]) -> int | None:
        """Return the index of the innermost active call of *parameter*'s function, or `None`."""
        return next((at for at in range(len(frames) - 1, -1, -1) if frames[at][0] is parameter.function), None)

    def layers(self, expr: ast.expr | None, depth: int = 0, frames: tuple[_Frame, ...] = ()) -> frozenset[str] | None:
        """Return the layer names *expr*'s subjects name, or `None` when it cannot be read.

        *frames* are the calls being read through, innermost last; a parameter of one of them is its argument.
        """
        expr, frames = self._passed(expr, frames)
        if expr is None or depth > _MAX_DEPTH:
            return None
        if isinstance(expr, ast.Tuple | ast.List | ast.Set):
            layers: set[str] = set()
            for element in expr.elts:
                resolved = self.layers(
                    element.value if isinstance(element, ast.Starred) else element, depth + 1, frames
                )
                if resolved is None:
                    return None
                layers |= resolved
            return frozenset(layers)
        if isinstance(expr, ast.GeneratorExp | ast.ListComp | ast.SetComp):
            return self.layers(expr.elt, depth + 1, frames)
        if isinstance(expr, ast.IfExp):
            body = self.layers(expr.body, depth + 1, frames)
            orelse = self.layers(expr.orelse, depth + 1, frames)
            return None if body is None or orelse is None else body | orelse
        if isinstance(expr, ast.Name):
            return self._readings_layers(expr, (), depth, frames, element=False)
        if isinstance(expr, ast.Call):
            return self._call_layers(expr, depth, frames)
        return None

    def _reading_layers(
        self, reading: _Reading, path: tuple[int, ...], depth: int, frames: tuple[_Frame, ...]
    ) -> frozenset[str] | None:
        """Return the layers of the item at *path* of what *reading* gives its name."""
        if isinstance(reading, _Element):
            return self._element_layers(reading.iterable, (*reading.path, *path), depth + 1, frames)
        if isinstance(reading, _Item):
            return self._item_layers(reading.value, (*reading.path, *path), depth + 1, frames)
        if isinstance(reading, _Parameter):
            return self._parameter_layers(reading, path, depth, frames, element=False)
        if isinstance(reading, _Augmented):
            return None
        return self._item_layers(reading, path, depth + 1, frames)

    def _parameter_layers(
        self, reading: _Parameter, path: tuple[int, ...], depth: int, frames: tuple[_Frame, ...], *, element: bool
    ) -> frozenset[str] | None:
        """Return the layers at *path* of the argument the active call, else every call site, passes for *reading*."""
        position = self._active(reading, frames)
        read = self._element_layers if element else self._item_layers
        if position is not None:
            argument = self._argument(frames[position][1], reading.index, reading.name)
            return read(argument, path, depth + 1, frames[:position])
        callers = self._callers(reading.function)
        if not callers or not isinstance(reading.function, _FUNCTIONS) or self._escapes(reading.function):
            return None
        return self._union(
            [read(self._argument(caller, reading.index, reading.name), path, depth + 1, ()) for caller in callers]
        )

    def _item_layers(
        self, item: ast.expr | None, path: tuple[int, ...], depth: int, frames: tuple[_Frame, ...]
    ) -> frozenset[str] | None:
        """Return the layers of the part at *path* of *item*; the whole of it when *path* is empty."""
        if not path:
            return self.layers(item, depth, frames)
        item, frames = self._passed(item, frames)
        if item is None or depth > _MAX_DEPTH:
            return None
        if isinstance(item, ast.Tuple | ast.List):
            if any(isinstance(element, ast.Starred) for element in item.elts) or path[0] >= len(item.elts):
                return None
            return self._item_layers(item.elts[path[0]], path[1:], depth + 1, frames)
        if isinstance(item, ast.IfExp):
            body = self._item_layers(item.body, path, depth + 1, frames)
            orelse = self._item_layers(item.orelse, path, depth + 1, frames)
            return None if body is None or orelse is None else body | orelse
        if isinstance(item, ast.Name):
            return self._readings_layers(item, path, depth, frames, element=False)
        return None

    def _element_layers(
        self, iterable: ast.expr | None, path: tuple[int, ...], depth: int, frames: tuple[_Frame, ...]
    ) -> frozenset[str] | None:
        """Return the layers of the part at *path* of every element *iterable* yields."""
        iterable, frames = self._passed(iterable, frames)
        if iterable is None or depth > _MAX_DEPTH:
            return None
        if isinstance(iterable, ast.Tuple | ast.List | ast.Set):
            return self._union(
                [
                    self._element_layers(element.value, path, depth + 1, frames)
                    if isinstance(element, ast.Starred)
                    else self._item_layers(element, path, depth + 1, frames)
                    for element in iterable.elts
                ]
            )
        if isinstance(iterable, ast.GeneratorExp | ast.ListComp | ast.SetComp):
            return self._item_layers(iterable.elt, path, depth + 1, frames)
        if isinstance(iterable, ast.Name):
            return self._readings_layers(iterable, path, depth, frames, element=True)
        if not (isinstance(iterable, ast.Call) and isinstance(iterable.func, ast.Name)):
            return None
        if iterable.func.id in _COPYING_CALLS and self.scopes.is_builtin(iterable.func.id, iterable):
            return self._element_layers(iterable.args[0], path, depth + 1, frames) if iterable.args else None
        target = self.scopes.resolve(iterable.func.id, iterable)
        if target is None or isinstance(target, ast.AsyncFunctionDef):
            return None
        inner = (*frames, (target, iterable))
        generator = self._is_generator(target)
        produced: list[frozenset[str] | None] = []
        for node in ast.walk(target):
            if self.scopes.scope_of.get(node) is not target:
                continue
            if isinstance(node, ast.Return) and node.value is not None and not generator:
                produced.append(self._element_layers(node.value, path, depth + 1, inner))
            elif isinstance(node, ast.Yield):
                produced.append(self._item_layers(node.value, path, depth + 1, inner))
            elif isinstance(node, ast.YieldFrom):
                produced.append(self._element_layers(node.value, path, depth + 1, inner))
        return self._union(produced)

    def _readings_layers(
        self,
        name: ast.Name,
        path: tuple[int, ...],
        depth: int,
        frames: tuple[_Frame, ...],
        *,
        element: bool,
        skip: ast.AST | None = None,
    ) -> frozenset[str] | None:
        """Return the layers at *path* of *name*'s value (*element*: of each element), over every reaching binding.

        What the name's uses add to its value in place (:meth:`_fills`) is folded in, so a collection is read whole;
        a value filled that way, or a `+=`, is never indexed by position.
        """
        readings = self._binding_of(name.id, name, skip)
        fills = self._name_fills(name)
        if readings is None or fills is None or depth > _MAX_DEPTH:
            return None
        collection = element or not path
        if not collection and (fills or any(isinstance(reading, _Augmented) for reading in readings)):
            return None
        parts: list[frozenset[str] | None] = []
        for reading in readings:
            if isinstance(reading, _Augmented):
                parts += [
                    self._element_layers(reading.value, path, depth + 1, frames),
                    self._readings_layers(reading.target, path, depth + 1, frames, element=True, skip=reading.target),
                ]
            elif not element:
                parts.append(self._reading_layers(reading, path, depth, frames))
            elif isinstance(reading, _Parameter):
                parts.append(self._parameter_layers(reading, path, depth, frames, element=True))
            else:
                parts.append(
                    self._element_layers(reading, path, depth + 1, frames) if isinstance(reading, ast.expr) else None
                )
        for fill in fills:
            read = self._item_layers if fill.whole else self._element_layers
            parts.append(read(fill.value, path, depth + 1, (*frames, *fill.frames)))
        return self._union(parts)

    def _name_fills(self, name: ast.Name) -> tuple[_Fill, ...] | None:
        """Return what the uses of *name*'s variable add to its value in place, or `None` if a use could add unseen."""
        scope = self.scopes.scope_of.get(name)
        bound = None if scope is None else self.scopes.binding_scope(name.id, scope)
        return None if bound is None else self._fills(name.id, bound, frozenset())

    def _fills(self, name: str, bound: ast.AST, active: frozenset[tuple[str, ast.AST]]) -> tuple[_Fill, ...] | None:
        """Audit every use of the variable *name* bound in *bound*, in that scope and the scopes nested there.

        `append(x)`, `extend(xs)` and `+= xs` add to it; a use that reads it (an iteration, a copy, a `return`,
        `subjects=`, an element of a new container, a comparison, an attribute) adds nothing; passing it to a
        function of the module adds what that function's parameter gets. Any other use is `None`: another method
        that changes a collection, an item assignment or `del`, an alias, a call the scan cannot follow.
        """
        key = (name, bound)
        if key in self._audited:
            return self._audited[key]
        if key in active:
            return None
        fills: list[_Fill] | None = []
        for node in ast.walk(bound):
            if fills is None or not (isinstance(node, ast.Name) and node.id == name):
                continue
            use_scope = self.scopes.scope_of.get(node)
            if use_scope is not None and self.scopes.binding_scope(name, use_scope, declared=True) is bound:
                found = self._use_fills(node, active | {key})
                fills = None if found is None else [*fills, *found]
        result = None if fills is None else tuple(fills)
        if not active:
            self._audited[key] = result
        return result

    def _use_fills(self, node: ast.Name, active: frozenset[tuple[str, ast.AST]]) -> list[_Fill] | None:
        """Return what one use of a variable adds to its value in place, or `None` when the scan cannot tell."""
        parent = self.parents.get(node)
        if isinstance(node.ctx, ast.Del):
            return None
        if isinstance(node.ctx, ast.Store):
            if isinstance(parent, ast.AugAssign):
                return [_Fill(parent.value, False, ())] if isinstance(parent.op, ast.Add) else None
            return []
        grand = self.parents.get(parent) if parent is not None else None
        if isinstance(parent, ast.Attribute) and isinstance(grand, ast.Call) and grand.func is parent:
            if parent.attr in _FILLING_METHODS:
                (value,) = grand.args if len(grand.args) == 1 and not grand.keywords else (None,)
                if value is None or isinstance(value, ast.Starred):
                    return None
                return [_Fill(value, parent.attr == "append", ())]
            return [] if parent.attr in _UNCHANGING_MEMBERS else None
        return self._held_fills(node, active)

    def _held_fills(self, node: ast.Name, active: frozenset[tuple[str, ast.AST]]) -> list[_Fill] | None:
        """Return what a use as an operand adds: climbed through the expressions that pass the value on to its consumer.

        A value held in a new container adds nothing; an item taken from it (`xs[i]`) is read like the value.
        """
        child: ast.AST = node
        parent = self.parents.get(node)
        element = False
        while True:
            if isinstance(parent, _HOLDING):
                return None if self._unpacked(parent, child) else []
            if isinstance(parent, ast.Subscript):
                if child is not parent.value:
                    return []
                if not isinstance(parent.ctx, ast.Load):
                    return None
                element = True
            elif not (isinstance(parent, _PASSING) or (isinstance(parent, ast.IfExp) and child is not parent.test)):
                break
            child, parent = parent, self.parents.get(parent)
        if isinstance(parent, ast.keyword):
            call = self.parents.get(parent)
            if parent.arg == "subjects" and isinstance(call, ast.Call) and self._called(call) is None:
                return []
            child, parent = parent, call
        if isinstance(parent, ast.Call):
            return [] if child is parent.func else self._passed_fills(parent, child, element, active)
        if isinstance(parent, ast.Attribute) and not element:
            return [] if parent.attr in _UNCHANGING_MEMBERS else None
        if isinstance(parent, ast.Attribute):
            return None if parent.attr in _MUTATORS | _FILLING_METHODS else []
        if isinstance(parent, ast.Assign | ast.AnnAssign | ast.NamedExpr):
            targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            return [] if all(isinstance(target, ast.Tuple | ast.List) for target in targets) else None
        if isinstance(parent, ast.For | ast.AsyncFor | ast.comprehension):
            return [] if child is parent.iter else None
        return [] if isinstance(parent, _READING_CONSUMERS) else None

    def _unpacked(self, display: ast.AST, child: ast.AST) -> bool:
        """Return whether *display*, holding *child* as an element, can hand that element on to a new name.

        A starred element is copied; otherwise the outermost display around it, a copy by an unshadowed builtin
        counted as one, is unpacked when it is the value of an assignment to a tuple or list target, the iterable of
        a loop or comprehension, or a call's argument other than the `subjects=` of a call the scan does not follow
        and the item a method stores in its receiver (an item of that container is the limit :class:`EmissionScan`
        names).
        """
        if isinstance(child, ast.Starred):
            return False
        outer, parent = display, self.parents.get(display)
        while parent is not None and (isinstance(parent, _HOLDING | ast.Starred) or self._copies(parent, outer)):
            outer, parent = parent, self.parents.get(parent)
        if isinstance(parent, ast.Assign):
            return parent.value is outer and not all(isinstance(target, ast.Name) for target in parent.targets)
        if isinstance(parent, ast.For | ast.AsyncFor | ast.comprehension):
            return parent.iter is outer
        if isinstance(parent, ast.keyword):
            call = self.parents.get(parent)
            if parent.arg == "subjects" and isinstance(call, ast.Call) and self._called(call) is None:
                return False
            parent = call
        if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Attribute):
            return parent.func.attr not in _STORING_METHODS
        return isinstance(parent, ast.Call) and parent.func is not outer

    def _copies(self, node: ast.AST, argument: ast.AST) -> bool:
        """Return whether *node* is a call of an unshadowed copying builtin whose one argument is *argument*."""
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _COPYING_CALLS
            and node.args == [argument]
            and self.scopes.is_builtin(node.func.id, node)
        )

    def _called(self, call: ast.Call) -> list[_FUNCTIONS] | None:
        """Return the functions of this module *call* runs: one by a bare name, or a reporter parameter's field."""
        function = call.func
        if isinstance(function, ast.Name):
            target = self.scopes.resolve(function.id, call)
            return None if target is None else [target]
        if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            annotated = self._parameter_annotation(function.value)
            functions = set() if annotated is None else self.reporters.get((annotated, function.attr), set())
            return sorted(functions, key=_position) or None
        return None

    def _passed_fills(
        self, call: ast.Call, argument: ast.AST, element: bool, active: frozenset[tuple[str, ast.AST]]
    ) -> list[_Fill] | None:
        """Return what *call* adds to the variable passed as *argument* (*element*: an item of it), or `None`.

        A builtin that only reads adds nothing; a function of the module adds what its parameter gets.
        """
        function = call.func
        if (
            isinstance(function, ast.Name)
            and function.id in _READING_CALLS
            and self.scopes.is_builtin(function.id, call)
        ):
            return []
        targets = self._called(call)
        if targets is None or isinstance(argument, ast.Starred):
            return None
        fills: list[_Fill] = []
        for target in targets:
            parameter = self._parameter_passed(target, call, argument)
            found = None if parameter is None else self._fills(parameter, target, active)
            if found is None or (element and found):
                return None
            fills += [_Fill(fill.value, fill.whole, ((target, call), *fill.frames)) for fill in found]
        return fills

    @staticmethod
    def _parameter_passed(target: _FUNCTIONS, call: ast.Call, argument: ast.AST) -> str | None:
        """Return the parameter of *target* that *call* binds *argument* to, or `None` when it cannot be told."""
        signature = target.args
        if isinstance(argument, ast.keyword):
            names = [each.arg for each in signature.posonlyargs + signature.args + signature.kwonlyargs]
            return argument.arg if argument.arg in names else None
        index = next(at for at, value in enumerate(call.args) if value is argument)
        positional = signature.posonlyargs + signature.args
        if index >= len(positional) or any(isinstance(value, ast.Starred) for value in call.args[:index]):
            return None
        return positional[index].arg

    @staticmethod
    def _union(parts: list[frozenset[str] | None]) -> frozenset[str] | None:
        """Return the union of *parts*, or `None` when any of them is unreadable."""
        if any(part is None for part in parts):
            return None
        return frozenset().union(*parts)  # type: ignore[arg-type]

    def _call_layers(self, expr: ast.Call, depth: int, frames: tuple[_Frame, ...]) -> frozenset[str] | None:
        function = expr.func
        if isinstance(function, ast.Attribute) and function.attr == "at":
            return self.layers(function.value, depth + 1, frames)
        if not isinstance(function, ast.Name):
            return None
        if self.scopes.import_of(function.id, function) == SUBJECT_CLASS:
            first, _ = self._passed(expr.args[0] if expr.args else self.keyword(expr, "layer"), frames)
            readings = self._binding_of(first.id, first) if isinstance(first, ast.Name) else None
            if readings is not None and any(isinstance(reading, _Element) for reading in readings):
                return self._registry_loop_layers(first, readings)  # type: ignore[arg-type]
            return frozenset(self.strings(first) or ()) or None
        if function.id in _COPYING_CALLS and self.scopes.is_builtin(function.id, expr):
            return self.layers(expr.args[0], depth + 1, frames) if expr.args else None
        target = self.scopes.resolve(function.id, expr)
        if target is None or isinstance(target, ast.AsyncFunctionDef):
            return None
        if self._is_generator(target):
            return self._element_layers(expr, (), depth + 1, frames)
        return self._returns(target, expr, depth, frames)

    def _is_generator(self, function: _FUNCTIONS) -> bool:
        """Return whether *function* is a generator function: a call returns an iterator over its `yield`s."""
        return any(
            isinstance(node, ast.Yield | ast.YieldFrom) and self.scopes.scope_of.get(node) is function
            for node in ast.walk(function)
        )

    def _in_body(self, node: ast.AST, loop: ast.For) -> bool:
        """Return whether *node* stands in *loop*'s body."""
        child, parent = node, self.parents.get(node)
        while parent is not None and parent is not loop:
            child, parent = parent, self.parents.get(parent)
        return parent is loop and any(statement is child for statement in loop.body)

    def _registry_loop_layers(self, name: ast.Name, readings: list[_Reading]) -> frozenset[str] | None:
        """Return *declared_layers* when *name* is the target of a `for` over the resolved registry's `.layer_names`.

        The use must stand in that loop's body, and the loop's target must be *name* alone; anything else is `None`.
        """
        if self.declared_layers is None or len(readings) != 1:
            return None
        (reading,) = readings
        loop = reading.loop if isinstance(reading, _Element) and not reading.path else None
        iterable = loop.iter if isinstance(loop, ast.For) and self._in_body(name, loop) else None
        if not (
            isinstance(iterable, ast.Attribute)
            and iterable.attr == "layer_names"
            and isinstance(iterable.value, ast.Name)
        ):
            return None
        bound = self._binding_of(iterable.value.id, iterable.value)
        if bound is None or len(bound) != 1 or not isinstance(bound[0], ast.expr):
            return None
        value = bound[0]
        resolved = value.values[-1] if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or) else value
        is_registry = (
            isinstance(resolved, ast.Call)
            and isinstance(resolved.func, ast.Name)
            and self.scopes.import_of(resolved.func.id, resolved.func) == CURRENT_REGISTRY
            and not resolved.args
        )
        return frozenset(self.declared_layers) if is_registry and self.declared_layers else None
