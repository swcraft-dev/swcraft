"""The editing write path's registry injection seam, held closed.

A ``registry: reg.Registry = reg.REGISTRY`` default would freeze the shipped contract's projection into its module
at import. :func:`~swcraft_tools.editing.engine.handle_request` instead resolves the registry once per request and
threads it through, so one response is never composed from two registries; a direct caller that omits
``registry`` gets its context's registry through :func:`~swcraft_tools.editing.registry.or_current`, and callees
that take none resolve the kernel registry the request binds with
:func:`~swcraft_tools.editing.registry.kernel_context`.

Six checks, each with a self-test that reverts part of the seam in a copy and asserts the check fails: no
parameter default (over ``editing/`` and :data:`AGENT_PACKAGE`); one module reading ``REGISTRY``; every optional
parameter resolved through ``or_current``; no call inside the package relying on the fallback; every import-time
registry surface on the allow-list; every ambient kernel resolution bound by the frame that was handed a registry.

What they do not reach: all six are AST statements and prove no behaviour (the flow itself is
``test_synthetic_flow_through.py``'s); a registry a function receives and then ignores; a function taking no
``registry`` that resolves the context itself; a *required* ``registry`` parameter. Green checks mean no optional
seam is left unresolved, not that the threading is complete.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest
from conftest import REPO_ROOT

#: The package every scan below reads. Rebound by the self-tests onto a copy.
PACKAGE = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "editing"

#: The second tree **check 1 alone** reads: ``scope_proposal.py`` shapes what the
#: LLM is shown and what the parser enforces, so a frozen default there would elide
#: legal addresses from an approval message. Checks 2-4 stop at ``editing/``: the
#: agent is an outside caller whose omitted argument is meant to resolve its context.
AGENT_PACKAGE = REPO_ROOT / "packages" / "agent" / "src" / "swcraft_agent"

#: The one module allowed to read the name ``REGISTRY``: it **defines** it, as the
#: shipped contract's projection, kept exported for the test tree. One member on
#: purpose: a second would mean the seam had reopened, and the answer is to close
#: it, not to name it.
REGISTRY_READERS = {"registry.py": "defines `REGISTRY` as the projection of the shipped contract"}


def _modules(package: Path | None = None) -> list[Path]:
    found = sorted((package or PACKAGE).glob("*.py"), key=lambda path: path.as_posix())
    assert found, f"{package or PACKAGE} holds no modules; every scan below would read nothing"
    return found


def _tree(module: Path) -> ast.Module:
    return ast.parse(module.read_text(encoding="utf-8"))


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]


def _reads_registry(node: ast.AST) -> bool:
    """*node* is the name ``REGISTRY``, bare or as an attribute."""
    return (isinstance(node, ast.Name) and node.id == "REGISTRY") or (
        isinstance(node, ast.Attribute) and node.attr == "REGISTRY"
    )


def _called_name(call: ast.Call) -> str | None:
    return call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)


def _copy_of_the_package(tmp_path: Path) -> Path:
    """A faithful subject for a mutation: the checks read files, not imports."""
    copied = tmp_path / "editing"
    copied.mkdir()
    for module in _modules():
        (copied / module.name).write_text(module.read_text(encoding="utf-8"), encoding="utf-8")
    return copied


# 1. No parameter default binds the shipped projection at import


def test_no_editing_module_takes_the_registry_as_a_parameter_default() -> None:
    """The import-time registry defaults are gone, under ``editing/`` and :data:`AGENT_PACKAGE`.

    No allow-list: a default of ``None`` resolving through ``or_current`` serves every
    caller. Both spellings are matched, ``reg.REGISTRY`` and the bare ``REGISTRY``.
    """
    offenders = [
        f"{module.name}:{default.lineno} {function.name}"
        for module in (*_modules(), *_modules(AGENT_PACKAGE))
        for function in _functions(_tree(module))
        for default in (*function.args.defaults, *(d for d in function.args.kw_defaults if d is not None))
        if _reads_registry(default)
    ]
    assert offenders == [], (
        "a parameter default under editing/ or packages/agent/ resolves REGISTRY again, which freezes the shipped "
        f"ten-layer projection into that module at import: {offenders}. Default it to None and call "
        "reg.or_current()."
    )


def test_the_default_gate_bites_on_a_restored_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Restoring one default fails check 1."""
    copied = _copy_of_the_package(tmp_path)
    target = copied / "passes.py"
    text = target.read_text(encoding="utf-8")
    assert text.count("registry: reg.Registry | None = None") == 3, "the source this mutation reverts has moved"
    target.write_text(
        text.replace("registry: reg.Registry | None = None", "registry: reg.Registry = reg.REGISTRY", 1),
        encoding="utf-8",
    )

    monkeypatch.setattr(f"{__name__}.PACKAGE", copied)
    with pytest.raises(AssertionError, match="freezes the shipped ten-layer projection"):
        test_no_editing_module_takes_the_registry_as_a_parameter_default()


# 2. The broader form: which module may read the name at all


def test_the_only_module_reading_REGISTRY_is_the_one_defining_it() -> None:
    """Only the defining module reads ``REGISTRY``, so no import-time binding of any shape survives.

    Closes check 1's blind spot: a module-level alias or an instance attribute. An
    allow-listed module must exist.
    """
    for module_name, reason in REGISTRY_READERS.items():
        assert (PACKAGE / module_name).is_file(), f"{module_name} is allow-listed ({reason}) but is not in the package"

    readers = {
        module.name
        for module in _modules()
        for node in ast.walk(_tree(module))
        if _reads_registry(node) or (isinstance(node, ast.alias) and node.name == "REGISTRY")
    }
    assert readers == set(REGISTRY_READERS), (
        f"the modules reading REGISTRY are {sorted(readers)}, not {sorted(REGISTRY_READERS)}. "
        "A new reader binds the shipped contract where the kernel seam cannot reach it; ask "
        "reg.current() at the entry point, or reg.or_current(registry) in the body."
    )


def test_the_reader_gate_bites_on_a_module_level_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A module-level ``_STEP_LIMIT = REGISTRY.max_address_depth`` fails check 2."""
    copied = _copy_of_the_package(tmp_path)
    target = copied / "review.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n\n_STEP_LIMIT = REGISTRY.max_address_depth\n", encoding="utf-8"
    )

    monkeypatch.setattr(f"{__name__}.PACKAGE", copied)
    with pytest.raises(AssertionError, match="modules reading REGISTRY"):
        test_the_only_module_reading_REGISTRY_is_the_one_defining_it()


# 3. An optional `registry` is resolved, or passed straight on


def _is_or_current_assignment(node: ast.AST) -> bool:
    """``registry = reg.or_current(registry)``, in either spelling of the import."""
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return False
    target = node.targets[0]
    if not (isinstance(target, ast.Name) and target.id == "registry"):
        return False
    return isinstance(node.value, ast.Call) and _called_name(node.value) == "or_current"


def _takes_an_optional_projection(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """*function* declares ``registry: <this package's Registry> | None = None``.

    Kernel-typed ``ModelRegistry | None`` parameters are excluded by annotation:
    they hold a kernel registry, not a projection, and are not this seam.
    """
    args = function.args
    positional = args.posonlyargs + args.args
    pairs = list(zip(positional[len(positional) - len(args.defaults) :], args.defaults, strict=True))
    pairs += [(arg, default) for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True) if default]
    return any(
        arg.arg == "registry"
        and isinstance(default, ast.Constant)
        and default.value is None
        and arg.annotation is not None
        and "ModelRegistry" not in ast.unparse(arg.annotation)
        for arg, default in pairs
    )


def _registry_uses(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Every statement of the body that mentions the name ``registry``, in source order."""
    return [
        node
        for node in function.body
        if any(isinstance(inner, ast.Name) and inner.id == "registry" for inner in ast.walk(node))
    ]


def test_every_optional_registry_parameter_resolves_through_or_current() -> None:
    """An omitted ``registry`` is resolved through ``or_current`` before its first use.

    Unresolved, ``None`` reaches ``registry.layer(...)`` as an ``AttributeError``
    deep in a walk. The resolution must be the first statement mentioning the
    argument: a later one did not happen for the earlier use. A function that only
    hands ``registry`` on is an offender: a binder must hold the resolved projection.
    """
    resolvers: list[str] = []
    offenders: list[str] = []
    for module in _modules():
        for function in _functions(_tree(module)):
            if not _takes_an_optional_projection(function):
                continue
            where = f"{module.name}:{function.lineno} {function.name}"
            uses = _registry_uses(function)
            if uses and _is_or_current_assignment(uses[0]):
                resolvers.append(where)
            else:
                offenders.append(where)
    assert offenders == [], (
        "these functions take `registry: ... | None = None` and do not resolve it through "
        f"reg.or_current() before first use: {offenders}"
    )
    assert resolvers, "no function resolves an optional registry at all; this check is reading nothing"


def test_the_resolution_gate_bites_on_an_unresolved_optional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Removing ``read_answer``'s resolution fails check 3."""
    copied = _copy_of_the_package(tmp_path)
    target = copied / "read.py"
    text = target.read_text(encoding="utf-8")
    # `read_answer`'s, named by its first use: `read.py` holds three resolutions.
    resolution = "    registry = reg.or_current(registry)\n"
    first_use = "    layer = _layer_or_none(registry, address.layer)\n"
    assert text.count(resolution + first_use) == 1, "the source this mutation reverts has moved"
    target.write_text(text.replace(resolution + first_use, first_use, 1), encoding="utf-8")

    monkeypatch.setattr(f"{__name__}.PACKAGE", copied)
    with pytest.raises(AssertionError, match="do not resolve it through"):
        test_every_optional_registry_parameter_resolves_through_or_current()


def test_the_resolution_gate_bites_on_a_late_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolution moved after the first use fails check 3."""
    copied = _copy_of_the_package(tmp_path)
    target = copied / "read.py"
    text = target.read_text(encoding="utf-8")
    resolution = "    registry = reg.or_current(registry)\n"
    first_use = "    layer = _layer_or_none(registry, address.layer)\n"
    assert text.count(resolution + first_use) == 1, "the source this mutation reorders has moved"
    target.write_text(text.replace(resolution + first_use, first_use + resolution, 1), encoding="utf-8")

    monkeypatch.setattr(f"{__name__}.PACKAGE", copied)
    with pytest.raises(AssertionError, match="do not resolve it through"):
        test_every_optional_registry_parameter_resolves_through_or_current()


# 4. Nothing inside the package relies on the fallback


def _projection_taking_functions() -> set[str]:
    """The names of the functions that accept this package's projection."""
    return {
        function.name
        for module in _modules()
        for function in _functions(_tree(module))
        if _takes_an_optional_projection(function)
    }


def test_no_call_inside_the_package_relies_on_the_fallback() -> None:
    """Every call inside ``editing/`` to a projection-taking function passes the projection.

    Resolving once per request buys nothing if a callee omits the argument and
    resolves again: the answers agree until a context moves mid-request.
    """
    names = _projection_taking_functions()
    assert names, "no function accepts the projection; this check is reading nothing"
    omitting = [
        f"{module.name}:{node.lineno} {ast.unparse(node)[:90]}"
        for module in _modules()
        for node in ast.walk(_tree(module))
        if isinstance(node, ast.Call)
        and _called_name(node) in names
        and not any(keyword.arg == "registry" for keyword in node.keywords)
    ]
    assert omitting == [], (
        "these calls inside editing/ leave the registry to the fallback, so one request can be served "
        f"by two resolutions: {omitting}"
    )


def test_the_threading_gate_bites_on_a_dropped_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dropping one threaded argument fails check 4, although behaviour would not change today."""
    copied = _copy_of_the_package(tmp_path)
    target = copied / "engine.py"
    text = target.read_text(encoding="utf-8")
    threaded = "outcome = pass_b(request.operations, staging.snapshot, registry=registry)"
    assert text.count(threaded) == 1
    target.write_text(
        text.replace(threaded, "outcome = pass_b(request.operations, staging.snapshot)", 1), encoding="utf-8"
    )

    monkeypatch.setattr(f"{__name__}.PACKAGE", copied)
    with pytest.raises(AssertionError, match="leave the registry to the fallback"):
        test_no_call_inside_the_package_relies_on_the_fallback()


# 5. Module-level constants that resolve a registry at import

#: The two trees check 5 walks, recursively and in full: an import-time surface
#: can sit in any module (``reader``'s ``_GENERATION_REGISTRY``) and be reached
#: through a module-local helper (``diagnosis``'s ``_suffixes_by_kind()``).
SCAN_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools",
    REPO_ROOT / "packages" / "agent" / "src" / "swcraft_agent",
)

#: The two registry resolvers. ``current_registry`` at import time means "whichever
#: registry was in scope at first import" — the hazard.
_PRODUCTION_RESOLVER = "production_registry"
_CONTEXT_RESOLVER = "current_registry"

#: Every module-level binding whose value reaches ``production_registry()``, with
#: the reason it may. Not the same population as the calls naming it: one call sits
#: in a helper called at import, another is a per-call fallback.
#: ``model_registry.production_registry``'s docstring states the same four. A new
#: entry means a new import-time surface: close it, do not name it.
IMPORT_TIME_REGISTRY_CONSTANTS = {
    "editing/registry.py:_PRODUCTION_MODEL": (
        "the production projection itself, which is what an import-time constant is FOR. "
        "`_PRODUCTION_TABLES`, `_PRODUCTION_FIELD_ANNOTATIONS` and `REGISTRY` are derived from it "
        "and resolve nothing of their own, so they are as right as this one and no more"
    ),
    "editing/diagnosis.py:_GENERATED_DATA_TYPE_SUFFIXES": (
        "the reserved-suffix table keyed by published kind name, built over the shipped contract "
        "because the writer's table it re-keys is keyed by shipped classes"
    ),
    "reader.py:_GENERATION_REGISTRY": "the reader's layer set and the `_NESTING_LAYER` derived from it",
    "scope_proposal.py:SCOPE_JSON_SCHEMA": "the prompt schema, explicitly the shipped contract's projection",
}


def _callee(node: ast.Call) -> str | None:
    """The bare name of *node*'s callee, however it is spelled."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _resolvers_reached(expression: ast.expr, functions: dict[str, ast.AST]) -> set[str]:
    """Which resolvers *expression* reaches, following **call callees** only.

    Following callees, not every mentioned name, separates an import-time
    resolution from a deferred one: a function merely mentioned resolves per call.
    Cross-module callees are not followed: they reach this module's context only
    through an argument, which is in the walked expression.
    """
    reached: set[str] = set()

    def walk(node: ast.AST, seen: frozenset[str]) -> None:
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            name = _callee(child)
            if name in (_PRODUCTION_RESOLVER, _CONTEXT_RESOLVER):
                reached.add(name)
            if name is not None and name in functions and name not in seen:
                walk(functions[name], seen | {name})

    walk(expression, frozenset())
    return reached


def _import_time_registry_surfaces() -> dict[str, set[str]]:
    """``"<path>:<name>" -> the resolvers it reaches``, over both trees in full."""
    found: dict[str, set[str]] = {}
    for root in SCAN_ROOTS:
        modules = sorted(root.rglob("*.py"), key=lambda path: path.as_posix())
        assert modules, f"{root} holds no modules; the scan would read nothing"
        for module in modules:
            tree = _tree(module)
            functions = {
                node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            }
            for node in tree.body:
                if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
                    continue
                reached = _resolvers_reached(node.value, functions)
                if not reached:
                    continue
                relative = module.relative_to(root.parent).as_posix().partition("/")[2]
                for target in [node.target] if isinstance(node, ast.AnnAssign) else node.targets:
                    if isinstance(target, ast.Name):
                        found[f"{relative}:{target.id}"] = reached
    return found


def test_every_import_time_registry_surface_is_on_the_allow_list() -> None:
    """A module-level binding that resolves a registry freezes it at import; each is allow-listed.

    Every surface must reach ``production_registry`` and none ``current_registry``,
    so the constant means the same whatever context it is first imported inside.

    Does not reach: a value that is no call (``X = SOME_REGISTRY``); a resolver
    reached through another module's argumentless callee; a target that is not a
    bare ``Name``; an assignment inside a module-level ``if`` or ``try``.
    """
    found = _import_time_registry_surfaces()
    assert set(found) == set(IMPORT_TIME_REGISTRY_CONSTANTS), (
        "a module-level binding that resolves a registry appeared or moved, and it binds at import: "
        f"{sorted(set(found) ^ set(IMPORT_TIME_REGISTRY_CONSTANTS))}"
    )
    context_resolving = sorted(name for name, reached in found.items() if _CONTEXT_RESOLVER in reached)
    assert context_resolving == [], (
        "these import-time surfaces resolve the context variable, so their value depends on which "
        f"registry is in scope at first import: {context_resolving}"
    )
    missing = sorted(name for name, reached in found.items() if _PRODUCTION_RESOLVER not in reached)
    assert missing == [], f"these import-time surfaces no longer name the production registry: {missing}"


@pytest.mark.parametrize(
    ("relative", "before", "after", "expected"),
    [
        pytest.param(
            "packages/agent/src/swcraft_agent/scope_proposal.py",
            "build_scope_json_schema(current(production_registry()))",
            "build_scope_json_schema()",
            "appeared or moved",
            id="the-prompt-schema-stops-naming-production",
        ),
        pytest.param(
            "packages/tools/src/swcraft_tools/editing/diagnosis.py",
            "    model_registry = production_registry()",
            "    model_registry = current_registry()",
            "resolve the context variable",
            id="the-indirect-suffix-table-resolves-the-context",
        ),
    ],
)
def test_the_surface_gate_bites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
    before: str,
    after: str,
    expected: str,
) -> None:
    """Each mutation fails check 5; the second is an indirect resolver in a helper called at import."""
    roots = []
    for root in SCAN_ROOTS:
        copied = tmp_path / root.name
        shutil.copytree(root, copied)
        roots.append(copied)
        _, marker, tail = relative.partition(f"{root.name}/")
        if not marker or not tail:
            continue
        candidate = copied / tail
        text = candidate.read_text(encoding="utf-8")
        assert text.count(before) == 1, f"the source this mutation reverts has moved in {relative}"
        candidate.write_text(text.replace(before, after, 1), encoding="utf-8")

    monkeypatch.setattr(f"{__name__}.SCAN_ROOTS", tuple(roots))
    with pytest.raises(AssertionError, match=expected):
        test_every_import_time_registry_surface_is_on_the_allow_list()


# 6. A threaded projection, and the ambient resolutions underneath it

#: The whole package, because check 6 follows calls out of ``editing/``: ``read``
#: -> ``load_snapshot`` -> ``model_state.load_model_state`` -> ``current_registry()``
#: spans three modules. Rebound by the self-test onto a copy.
TOOLS_PACKAGE = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools"

#: Scanned only to prove no covered frame has a caller outside the tools package.
FOREIGN_PACKAGES = (
    REPO_ROOT / "packages" / "mcp" / "src" / "swcraft_mcp",
    REPO_ROOT / "packages" / "agent" / "src" / "swcraft_agent",
)

#: The name a frame binds its projection's kernel registry with.
_BINDER = "kernel_context"

#: Parameter names that mean *this frame was handed a registry*, of either kind.
_REGISTRY_PARAMETERS = frozenset({"registry", "model_registry"})

#: Population members left deliberately open, with the reason per member.
#:
#: ``or_current``'s resolution runs only when the caller supplied nothing, so there
#: is no threaded registry to disagree with; the check cannot read the ``IfExp``
#: guard, so the member is named. Any other entry should be a ``kernel_context``
#: binding instead.
AMBIENT_RESOLUTIONS_LEFT_OPEN = {
    "editing.registry:or_current": (
        "the fallback itself: the resolution is on the `registry is None` branch of an IfExp, so "
        "there is no threaded registry for it to disagree with and the value resolved is what the "
        "caller then threads"
    ),
}


class _Frame:
    """One function of the scanned tree, with what its own body does."""

    __slots__ = ("_class", "calls", "module", "node", "qualname", "resolves")

    def __init__(self, module: str, qualname: str, node: ast.AST, owner: str | None) -> None:
        self.module = module
        self.qualname = qualname
        self.node = node
        self._class = owner
        #: True when the body names ``current_registry`` or ``production_registry``.
        self.resolves = False
        #: ``(call, callee keys, is the call inside a `kernel_context` block)``.
        self.calls: list[tuple[ast.Call, set[str], bool]] = []

    @property
    def key(self) -> str:
        return f"{self.module}:{self.qualname}"


def _own_statements(node: ast.AST) -> list[ast.AST]:
    """Every node of *node*'s body, not descending into a nested def or class (scanned as its own frame)."""
    out: list[ast.AST] = []

    def walk(parent: ast.AST) -> None:
        for child in ast.iter_child_nodes(parent):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                continue
            out.append(child)
            walk(child)

    walk(node)
    return out


def _binding_ranges(node: ast.AST) -> list[tuple[int, int]]:
    """The line spans of this frame's own ``with kernel_context(...)`` blocks."""
    return [
        (statement.lineno, statement.end_lineno or statement.lineno)
        for statement in _own_statements(node)
        if isinstance(statement, ast.With | ast.AsyncWith)
        and any(
            _called_name(item.context_expr) == _BINDER
            for item in statement.items
            if isinstance(item.context_expr, ast.Call)
        )
    ]


def _call_graph(root: Path) -> tuple[dict[str, Path], dict[str, _Frame]]:
    """Every function of *root*, its calls resolved, and where each module lives.

    Narrow where noise would come, wide where silence would. ``name()`` resolves
    through the module's definitions and ``from`` imports, ``alias.name()`` when
    ``alias`` is a module of the tree, ``self.name()`` in the defining class;
    ``variable.name()`` resolves to **every** method of that name (over-reports);
    an attribute of an attribute resolves to nothing, so ``self._path.open(...)``
    is not read as ``ScopedSession.open``.
    """
    modules: dict[str, Path] = {}
    for path in sorted(root.rglob("*.py"), key=lambda path: path.as_posix()):
        parts = [part for part in path.relative_to(root).with_suffix("").parts if part != "__init__"]
        modules[".".join(parts) or "__init__"] = path
    trees = {name: _tree(path) for name, path in modules.items()}

    symbols: dict[str, dict[str, tuple[str, str]]] = {}
    module_aliases: dict[str, dict[str, str]] = {}
    for name, tree in trees.items():
        symbols[name], module_aliases[name] = {}, {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                origin = node.module.removeprefix("swcraft_tools.")
                for alias in node.names:
                    if f"{origin}.{alias.name}" in modules:
                        module_aliases[name][alias.asname or alias.name] = f"{origin}.{alias.name}"
                    elif origin in modules:
                        symbols[name][alias.asname or alias.name] = (origin, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    origin = alias.name.removeprefix("swcraft_tools.")
                    if origin in modules:
                        module_aliases[name][alias.asname or origin.rpartition(".")[2]] = origin

    frames: dict[str, _Frame] = {}
    by_symbol: dict[tuple[str, str], _Frame] = {}
    by_method: dict[str, list[_Frame]] = {}
    for name, tree in trees.items():

        def collect(parent: ast.AST, stack: list[str], owner: str | None, module: str = name) -> None:
            for child in ast.iter_child_nodes(parent):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    frame = _Frame(module, ".".join([*stack, child.name]), child, owner)
                    frames[frame.key] = frame
                    by_symbol.setdefault((module, child.name), frame)
                    if owner is not None:
                        by_method.setdefault(child.name, []).append(frame)
                    collect(child, [*stack, child.name], owner, module)
                elif isinstance(child, ast.ClassDef):
                    collect(child, [*stack, child.name], child.name, module)
                else:
                    collect(child, stack, owner, module)

        collect(tree, [], None)

    def callees(module: str, call: ast.Call, owner: str | None) -> set[str]:
        func = call.func
        if isinstance(func, ast.Name):
            if (module, func.id) in by_symbol:
                return {by_symbol[module, func.id].key}
            if func.id in symbols[module]:
                origin, attribute = symbols[module][func.id]
                if (origin, attribute) in by_symbol:
                    return {by_symbol[origin, attribute].key}
            return set()
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            base = func.value.id
            if base in module_aliases[module]:
                target = module_aliases[module][base]
                return {by_symbol[target, func.attr].key} if (target, func.attr) in by_symbol else set()
            if base == "self" and owner is not None:
                key = f"{module}:{owner}.{func.attr}"
                return {key} if key in frames else set()
            return {frame.key for frame in by_method.get(func.attr, ())}
        return set()

    for frame in frames.values():
        spans = _binding_ranges(frame.node)
        for statement in _own_statements(frame.node):
            if not isinstance(statement, ast.Call):
                continue
            name = _called_name(statement)
            if name in (_PRODUCTION_RESOLVER, _CONTEXT_RESOLVER):
                frame.resolves = True
                continue
            if name == _BINDER:
                continue
            bound = any(start <= statement.lineno <= end for start, end in spans)
            frame.calls.append((statement, callees(frame.module, statement, frame._class), bound))
    return modules, frames


def _reaches_a_resolver(frames: dict[str, _Frame]) -> dict[str, bool]:
    """Transitive closure of *this frame's body, or something it calls, resolves a registry*."""
    reaches = {key: frame.resolves for key, frame in frames.items()}
    changed = True
    while changed:
        changed = False
        for key, frame in frames.items():
            if reaches[key]:
                continue
            if any(reaches.get(callee) for _, callee_keys, _ in frame.calls for callee in callee_keys if callee != key):
                reaches[key] = True
                changed = True
    return reaches


def _ambient_resolution_sites() -> tuple[dict[str, list[str]], dict[str, bool]]:
    """The population and, per member, whether every route into it is bound.

    A member is a frame under ``editing/`` that was handed a registry and holds an
    unbound call, passing no registry, that transitively resolves one. A
    **private** member is covered when it has callers and each call is inside the
    caller's binding block or comes from a covered caller. A **public** member is
    covered only by binding for itself: another tree may enter by its name.
    """
    modules, frames = _call_graph(TOOLS_PACKAGE)
    reaches = _reaches_a_resolver(frames)

    population: dict[str, list[str]] = {}
    for key, frame in frames.items():
        if not frame.module.startswith("editing"):
            continue
        arguments = frame.node.args
        declared = {a.arg for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs}
        if not _REGISTRY_PARAMETERS & declared:
            continue
        for call, callee_keys, bound in frame.calls:
            if bound or not any(reaches.get(callee) for callee in callee_keys):
                continue
            if any(keyword.arg in _REGISTRY_PARAMETERS for keyword in call.keywords) or any(
                isinstance(argument, ast.Name) and argument.id in _REGISTRY_PARAMETERS for argument in call.args
            ):
                continue
            population.setdefault(key, []).append(f"{modules[frame.module].name}:{call.lineno} {_called_name(call)}()")

    callers: dict[str, set[tuple[str, bool]]] = {key: set() for key in frames}
    for key, frame in frames.items():
        for _, callee_keys, bound in frame.calls:
            for callee in callee_keys:
                if callee != key:
                    callers[callee].add((key, bound))

    def covered(key: str, seen: frozenset[str]) -> bool:
        if not key.rpartition(":")[2].rpartition(".")[2].startswith("_"):
            return False  # public: it binds for itself, or it is open
        if key in seen or not callers[key]:
            return False
        return all(bound or covered(caller, seen | {key}) for caller, bound in callers[key])

    return population, {key: covered(key, frozenset()) for key in population}


def test_every_ambient_kernel_resolution_is_bound_by_the_frame_that_was_handed_a_registry() -> None:
    """A frame handed a registry binds the kernel registry its callees resolve, or is named open.

    Covered means bound by :func:`~swcraft_tools.editing.registry.kernel_context`
    around the call, or reached only through callers that bind; otherwise the frame
    is in :data:`AMBIENT_RESOLUTIONS_LEFT_OPEN`. Refuses an unbound frame, under
    which a read with an explicit projection answers ``the snapshot carries no
    'synthetic'``.

    Does not reach:

    * Callers outside ``packages/tools/src/swcraft_tools``: the second assertion
      closes ``packages/mcp/`` and ``packages/agent/`` by name, no third tree.
    * Frames outside ``editing/`` (the two that exist are scan false positives).
    * The run-time binding: ``kernel_context`` binds nothing for a projection with
      no kernel registry, so a covered frame can run unbound
      (``test_editing_registry.py`` tests that behaviour).
    * The test tree: a test calling a covered private frame directly with a
      foreign projection still diverges; hence public members must bind themselves.
    * A receiver that is an attribute of an attribute resolves to nothing.
    * A callback invoked from outside the block: binding is lexical.
    """
    population, covered = _ambient_resolution_sites()
    assert population, (
        "no frame under editing/ was handed a registry and then let a callee resolve one; the scan "
        "is reading nothing and would pass on any tree"
    )
    uncovered = {key: sites for key, sites in population.items() if not covered[key]}
    unexplained = sorted(set(uncovered) - set(AMBIENT_RESOLUTIONS_LEFT_OPEN))
    assert unexplained == [], (
        "these frames hold a threaded registry and let a callee resolve the ambient one instead, so "
        "one request can read the model under a registry it did not answer under. Bind it with "
        "reg.kernel_context(registry), or name the frame in AMBIENT_RESOLUTIONS_LEFT_OPEN with a "
        f"reason: { {key: population[key] for key in unexplained} }"
    )
    stale = sorted(set(AMBIENT_RESOLUTIONS_LEFT_OPEN) - set(uncovered))
    assert stale == [], (
        "these AMBIENT_RESOLUTIONS_LEFT_OPEN entries name nothing the scan reports any more; delete "
        f"them rather than leaving an exemption that outlived its subject: {stale}"
    )
    covered_members = sorted(key for key, is_covered in covered.items() if is_covered)
    assert covered_members, (
        "no population member is covered by a binding at all; either every one is exempted or "
        "kernel_context has been removed, and this check would then pass on an unbound tree"
    )

    # The dominance half, checked: a frame covered by its callers has no caller in
    # the other two packages.
    reached_from_outside = [
        f"{module.relative_to(root.parent)}:{node.lineno} {_called_name(node)}()"
        for key in covered_members
        for root in FOREIGN_PACKAGES
        for module in sorted(root.rglob("*.py"), key=lambda path: path.as_posix())
        for node in ast.walk(_tree(module))
        if isinstance(node, ast.Call) and _called_name(node) == key.rpartition(":")[2].rpartition(".")[2]
    ]
    assert reached_from_outside == [], (
        "a frame covered by its callers inside packages/tools/src is called from another package, where no "
        f"binding was proved: {reached_from_outside}"
    )


@pytest.mark.parametrize(
    ("relative", "before", "after", "expected"),
    [
        pytest.param(
            "editing/engine.py",
            "    with reg.kernel_context(registry):\n",
            "    if True:\n",
            "hold a threaded registry",
            id="the-request-stops-binding-and-the-two-private-frames-open",
        ),
        pytest.param(
            "editing/read.py",
            "    with reg.kernel_context(registry):\n",
            "    if True:\n",
            "hold a threaded registry",
            id="the-read-entry-point-stops-binding",
        ),
        pytest.param(
            "editing/derive.py",
            "    with reg.kernel_context(registry):\n",
            "    if True:\n",
            "hold a threaded registry",
            id="the-derivation-entry-point-stops-binding",
        ),
        pytest.param(
            "editing/staging.py",
            "        with reg.kernel_context(registry):\n",
            "        if True:\n",
            "hold a threaded registry",
            id="materialization-stops-binding",
        ),
    ],
)
def test_the_ambient_binding_gate_bites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
    before: str,
    after: str,
    expected: str,
) -> None:
    """Replacing one binding with a no-op block fails check 6.

    ``if True:`` keeps the body's indentation and line numbers unchanged.
    """
    copied = tmp_path / "swcraft_tools"
    shutil.copytree(TOOLS_PACKAGE, copied)
    target = copied / relative
    text = target.read_text(encoding="utf-8")
    assert text.count(before) == 1, f"the source this mutation reverts has moved in {relative}"
    target.write_text(text.replace(before, after, 1), encoding="utf-8")

    monkeypatch.setattr(f"{__name__}.TOOLS_PACKAGE", copied)
    with pytest.raises(AssertionError, match=expected):
        test_every_ambient_kernel_resolution_is_bound_by_the_frame_that_was_handed_a_registry()
