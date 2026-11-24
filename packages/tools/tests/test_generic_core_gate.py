"""The generic-core gate: no total consumer of the model spells a layer out.

A source scan over the generic-core modules and over a named manifest of the migrated sites of the migration
inventory's closed rows inside the generic core. Three checks:

(a) Over the generic-core files, no call in the traversal vocabulary walks a model layer outside
    ``model_state.py``. Calls are classified by the *binding* the name resolves to, not its spelling.
(b) Over the manifest only, no migrated site spells a layer out: **b1** a layer-directory literal as an operand
    of path composition; **b2** a layer token in a state/diff adapter access; **b3** a layer token anywhere
    inside a module-level manifest site.
(c) No migrated function reads a derived view: none is retained, so :func:`derived_view_findings` is shown to
    bite on a probe list, and the deletion is guarded by
    :func:`test_no_deleted_derived_view_is_declared_anywhere_in_the_package`.

What the gate does not reach: the retained partial consumers (``validator/``, ``writer/elements.py``,
``component_exchange/``) are outside the manifest; checks (b)/(c) are named-site scans, and a row's hand-written
``functions``/``module_level`` tuples are not checked to name every migrated function; a layer name assembled at
run time; an unannotated or ``Any`` receiver in b2; anything outside ``packages/tools/src/swcraft_tools``.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT
from swcraft_tools.model_registry import production_registry

PACKAGE_ROOT = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools"

#: The converted readers of the deleted derived views are test modules, scanned here.
TESTS_ROOT = REPO_ROOT / "packages" / "tools" / "tests"

#: The one module the layer walk is allowed to live in.
THE_ONLY_TRAVERSAL_MODULE = "model_state.py"

#: Methods that walk a directory when called on a ``Path``-like receiver.
#: ``iglob`` sits beside ``glob`` because no ``Path`` has such a method: an
#: attribute call spelling it is always the ``glob`` module.
_PATH_TRAVERSAL_METHODS = frozenset({"glob", "rglob", "iterdir", "iglob"})

#: Functions that walk a directory when called on the ``os`` module — or, for
#: ``walk``, on a ``Path`` (3.12+). The receiver matters: a bare ``walk(...)``
#: is ``cli/init_command.py``'s local recursive helper over an ``importlib.resources`` tree,
#: and ``ast.walk`` is a tree visit, so a receiver that is an imported module
#: other than ``os`` is not a traversal while any other receiver is.
_OS_TRAVERSAL_FUNCTIONS = frozenset({"scandir", "listdir", "walk"})

#: The vocabulary check (a) resolves: derived from the two frozensets, so it cannot go stale.
TRAVERSAL_VOCABULARY = tuple(sorted(_PATH_TRAVERSAL_METHODS | _OS_TRAVERSAL_FUNCTIONS))

#: The modules whose bare-imported names are traversals (``from os import listdir``).
_TRAVERSAL_IMPORTS: dict[str, frozenset[str]] = {
    "os": _OS_TRAVERSAL_FUNCTIONS,
    "glob": frozenset({"glob", "iglob"}),
}

#: Callables that compose a path out of their arguments, so a layer-directory
#: literal among those arguments is path composition (b1) exactly as
#: ``root / "swc"`` is. The ``pathlib`` constructors are matched by bare name —
#: every one of them is imported that way — and ``join`` by the module it is
#: called on, so an ordinary ``",".join(...)`` is left alone.
_PATH_CONSTRUCTORS = frozenset({"Path", "PurePath", "PosixPath", "WindowsPath", "PurePosixPath", "PureWindowsPath"})
_JOIN_MODULES = frozenset({"os.path", "posixpath", "ntpath"})

#: The two attributes that expose the kernel value behind an adapter. A literal
#: subscript of either — or of a local alias of either — is a layer name written
#: back into a consumer.
_ADAPTER_MAPPINGS = frozenset({"state", "by_layer"})

#: The adapter types whose *attributes* are the layer set: a name annotated as one
#: of these and read by a layer token (``diff.swcs``) is a hard-coded layer, a shape
#: that is neither a ``getattr`` nor a subscript.
_ADAPTER_TYPES = frozenset({"ModelSnapshot", "ModelDiff", "ModelState", "ModelStateDiff"})

#: The generic-core files named one by one; ``editing/``, ``cli/`` and ``query/``
#: are enumerated from disk (:func:`_package_modules`), so a new module of them
#: cannot arrive unscanned. ``listing.py``'s whole job is to enumerate a layer's
#: files, the shape check (a) keeps out of every module but ``model_state.py``.
#: ``guide_reference/`` modules are named because the top-level partition globs
#: ``*.py`` without descending; its other modules are excused in
#: :data:`_GUIDE_REFERENCE_NOT_GENERIC_CORE`.
_NAMED_GENERIC_CORE_FILES = (
    "contract.py",
    "relations.py",
    "model_registry.py",
    "model_state.py",
    "snapshot.py",
    "differ.py",
    "session.py",
    "impact.py",
    "authorization.py",
    "reader.py",
    "writer/mutations.py",
    "writer/yaml_documents.py",
    "editor_schema.py",
    "listing.py",
    "guide_reference/model.py",
    "guide_reference/rules.py",
    "undeclared_keys.py",
)


def _package_modules(package: str) -> tuple[str, ...]:
    """Every module of the *package* directory, as package-relative paths."""
    return tuple(sorted(path.relative_to(PACKAGE_ROOT).as_posix() for path in (PACKAGE_ROOT / package).rglob("*.py")))


def generic_core_files() -> tuple[str, ...]:
    return (
        *_NAMED_GENERIC_CORE_FILES,
        *_package_modules("editing"),
        *_package_modules("cli"),
        *_package_modules("query"),
    )


# The manifest: the migrated sites of the closed inventory rows inside the generic core.


@dataclass(frozen=True)
class Site:
    """One inventory row's migrated sites in one module.

    *functions* are scanned by all three checks; *module_level* (each a
    projection of the registry) by b3, since an assignment is not a function body.
    """

    row: str
    module: str
    functions: tuple[str, ...] = ()
    module_level: tuple[str, ...] = ()


MANIFEST: tuple[Site, ...] = (
    Site(
        row="reader.layer-directories",
        module="reader.py",
        functions=("read_layer", "find_element_file", "_read_layer_with_placement"),
    ),
    Site(
        row="reader.placement-field-name",
        module="reader.py",
        functions=("swc_placement", "read_swc_model_with_placement"),
        module_level=("_NESTING_LAYER",),
    ),
    Site(
        row="writer-mutations.filename-from-identity",
        module="writer/mutations.py",
        functions=("_target_path",),
    ),
    Site(
        row="writer-mutations.layer-directories",
        module="writer/mutations.py",
        functions=("_target_path",),
    ),
    Site(
        row="snapshot.layer-attributes",
        module="snapshot.py",
        functions=(
            "_from_state",
            "_state_of",
            "load_snapshot",
        ),
    ),
    Site(
        row="snapshot.placement-bridge",
        module="snapshot.py",
        functions=("_nesting_layer", "_from_state"),
    ),
    Site(
        row="differ.layer-display",
        module="differ.py",
        functions=(
            "_layer_renders",
            "_from_state_diff",
            "_by_layer_of",
            "_diff_is_empty",
            "format_layer",
            "_json_layer",
            "format_diff",
            "format_diff_json",
            "diff_model_states",
            "diff_snapshots",
        ),
    ),
    Site(
        row="differ.collection-identity",
        module="differ.py",
        functions=(
            "_identity_fields",
            "_collection_key",
            "_collection_identity",
            "collection_identity",
            "collection_rule",
            "_collection_rule_for",
        ),
    ),
    Site(
        row="authorization.layer-models",
        module="authorization.py",
        functions=(
            "_layer_key",
            "_ordered_layers",
            "_model_classes",
            "_layer_vocabulary",
            "_layer_models",
            "_layer_identity_fields",
            "_layer_names",
            "_layer_fields",
        ),
        module_level=("DELETE_ELEMENT_LAYERS",),
    ),
    Site(
        row="session.layer-directories",
        module="session.py",
        functions=(
            "_layer_of_directory",
            "_is_explained",
            "_require_explained",
            "_touched_key",
            "_touched_identities",
        ),
    ),
    Site(
        row="session.model-file-suffix",
        module="session.py",
        functions=("_iter_model_files", "_index_model_files", "_declared_identity", "_is_explained"),
    ),
    # A `Site` names one module, so each `impact.*` row, whose sites span
    # `query/edges.py` and `impact.py`, is two Sites.
    Site(
        row="impact.layer-key-fns",
        module="query/edges.py",
        functions=(
            "layer_token",
            "_identity_key",
            "field_attribute",
            "_all_key_maps",
            "dependents",
            "dependencies",
        ),
    ),
    Site(
        row="impact.layer-key-fns",
        module="impact.py",
        functions=(
            "_display_tokens",
            "impact_of",
            "format_impact",
            "format_impact_summary",
        ),
    ),
    Site(
        row="impact.reference-topology",
        module="query/edges.py",
        functions=(
            "_from_reference",
            "_from_relation",
            "_declared_edges",
            "_emission_order",
            "_emission_plan",
            "_members",
            "_keys",
            "_targets",
            "build_graphs",
        ),
    ),
    Site(
        row="impact.reference-topology",
        module="impact.py",
        functions=("build_reference_graph",),
    ),
    Site(
        row="impact.undeclared-reference-edges",
        module="query/edges.py",
        functions=("_add_edge", "_emit_both", "build_graphs"),
    ),
    Site(
        row="cli.model-subdirs",
        module="cli/init_command.py",
        functions=("_scaffold_model_directories",),
    ),
    # `_MEMBERS_AND_GENERATED_FORMS` is deliberately not a `module_level` site
    # (see *What the gate does not reach*); the three handlers are named because
    # a layer may still be spelled there.
    Site(
        row="editing.diagnosis-identifier-layers",
        module="editing/diagnosis.py",
        functions=(
            "_identifier_layers",
            "emitted_identifier_violations",
            "_swc_identifiers",
            "_interface_identifiers",
            "_data_type_identifiers",
        ),
    ),
    # The row's symbol is a parameter default, not a layer literal: its sites are
    # the top-level functions that held `registry: reg.Registry = reg.REGISTRY`,
    # plus `audit.block_is_complete`, `review._step_limit` and
    # `engine.handle_request`, where the projection is resolved. The two methods
    # (`StagedModel.from_snapshot`, `StagedChangeSet.materialize`) cannot be named
    # here; `test_editing_registry_injection.py` covers them.
    Site(row="editing.write-path-registry-injection", module="editing/address.py", functions=("resolve",)),
    Site(
        row="editing.write-path-registry-injection",
        module="editing/apply.py",
        functions=("apply_operations", "element_kind_of"),
    ),
    Site(
        row="editing.write-path-registry-injection",
        module="editing/audit.py",
        functions=(
            "_recorded_element_address",
            "element_address_of_target",
            "block_is_complete",
            "committed_element_addresses",
            "written_element_addresses",
        ),
    ),
    Site(row="editing.write-path-registry-injection", module="editing/delta.py", functions=("project_delta",)),
    Site(row="editing.write-path-registry-injection", module="editing/derive.py", functions=("derive_change_set",)),
    Site(
        row="editing.write-path-registry-injection",
        module="editing/passes.py",
        functions=("pass_b", "ambiguous_baseline_violations", "ambiguous_member_violations"),
    ),
    Site(row="editing.write-path-registry-injection", module="editing/read.py", functions=("read_value",)),
    Site(
        row="editing.write-path-registry-injection",
        module="editing/staging.py",
        functions=("_composable_placement", "_locate", "_instantiate"),
    ),
    Site(row="editing.write-path-registry-injection", module="editing/review.py", functions=("_step_limit",)),
    Site(
        row="editing.write-path-registry-injection",
        module="editing/engine.py",
        functions=("handle_request", "_answer_discover"),
    ),
)


#: The thirteen deleted derived views, by the module that declared them: what must
#: not come back. Scanned over the whole package, so a view moved to a neighbour fails.
DELETED_DERIVED_VIEWS: dict[str, tuple[str, ...]] = {
    "session.py": ("_LAYER_DIRECTORIES", "_RECURSIVE_LAYER_DIRECTORIES"),
    "differ.py": ("_LAYER_DISPLAY", "COLLECTION_IDENTITY"),
    "cli/init_command.py": ("_MODEL_SUBDIRS",),
    "impact.py": ("_LAYER_KEY_FNS",),
    "reader.py": ("LAYER_GLOBS", "SWC_PLACEMENT_FIELD", "UNGROUPED_SWC_PLACEMENT"),
    "authorization.py": ("_LAYER_MODELS", "LAYER_NAMES", "LAYER_FIELDS", "LAYER_IDENTITY_FIELDS"),
}

#: The six table names deleted from ``editing/registry.py``. Kept apart from
#: :data:`DELETED_DERIVED_VIEWS`: a derived view *restated* a layer fact, while these
#: were the ``editing/`` package's one projection of the contract. The facts did not
#: move (``_project_tables`` computes them); only the names went.
DELETED_PROJECTED_TABLES: tuple[str, ...] = (
    "LAYER_SPECS",
    "KIND_NAMES",
    "KIND_DESCRIPTIONS",
    "FIELD_ANNOTATIONS",
    "COLLECTION_SPECS",
    "PLACEMENT_FIELDS",
)

#: The five names deleted with the ``resources/`` layer: its schema classes and its
#: reader and writer entry points. ``Resource`` itself is absent: the bare word
#: survives in ``Resource Utilization``, which a word-boundary scan would report.
DELETED_RESOURCES_LAYER_NAMES: tuple[str, ...] = (
    "CpuCoreResource",
    "MemoryResource",
    "read_resources",
    "find_resource_file",
    "create_resource",
)

#: What replaced the six tables and must stay reachable: the private projection,
#: and ``build_registry``'s keyword arguments, the seam every registry self-test
#: perturbs through.
SURVIVING_REGISTRY_NAMES: tuple[str, ...] = (
    "_project_tables",
    "_project_field_annotations",
    "_PRODUCTION_TABLES",
    "_PRODUCTION_FIELD_ANNOTATIONS",
    "build_registry",
    "REGISTRY",
    "current",
    "or_current",
)


# The allow-list: exactly six, each with its reason.


@dataclass(frozen=True)
class Exemption:
    """One allow-listed site.

    *kind* ``traversal`` is a call of *symbol* inside the top-level definition
    *function*; ``declared-vocabulary`` a module-level assignment to *symbol* that
    carries layer tokens. Keyed by the enclosing definition, not a line number,
    which goes stale on the next edit above it. *calls* is how many calls of
    *symbol* the entry covers, so a second call in the same function is reported.
    """

    module: str
    kind: str
    symbol: str
    function: str | None
    reason: str
    calls: int = 1


ALLOW_LIST: tuple[Exemption, ...] = (
    Exemption(
        module="session.py",
        kind="traversal",
        symbol="rglob",
        function="_iter_model_files",
        reason=(
            "the whole-tree fingerprint. It walks every file under the model root, including files "
            "no layer reads, so a foreign file cannot hide from the baseline a concurrent change is "
            "refused against. Deliberately not a layer walk."
        ),
    ),
    Exemption(
        module="cli/init_command.py",
        kind="traversal",
        symbol="iterdir",
        function="_template_file_plan",
        reason=(
            "the packaged template resources, not the model. `swcraft init` copies an "
            "`importlib.resources` tree out of the wheel; the directory being walked is package "
            "data and holds no model element."
        ),
    ),
    Exemption(
        module="cli/build_commands.py",
        kind="traversal",
        symbol="iterdir",
        function="_clean_target",
        reason=(
            "the project's generated output, not the model. `swcraft clean-outputs` lists "
            "`dist_root`/`views_root` only to preserve the audit log and the `_old` ARXML inside "
            "it, and both roots are "
            "refused when they overlap `model_root` — by the manifest loader at load time and "
            "again by `_clean_root_problems` before this call — so no model element can live under "
            "the directory being walked. No layer name is involved."
        ),
    ),
    Exemption(
        module="editing/engine.py",
        kind="traversal",
        symbol="scandir",
        function="_require_model_root",
        reason=(
            "a readability preflight of the model *root*, not of a layer. The question is whether "
            "the root can be listed at all, which is one condition for every caller downstream."
        ),
    ),
    Exemption(
        module="editor_schema.py",
        kind="traversal",
        symbol="glob",
        function="main",
        reason=(
            "the generator's own output directory (`vscode_extension/schemas/`), not the model: "
            "`main` lists the `*.schema.json` files already there to remove the one a renamed or "
            "removed layer left behind. No model element lives there and no "
            "layer name is involved."
        ),
    ),
    Exemption(
        module="authorization.py",
        kind="declared-vocabulary",
        symbol="DELETE_ELEMENT_LAYERS",
        function=None,
        reason=(
            "the safety classifier's own `element_type` vocabulary, deliberately retained by Phase "
            "19.3 and read by nothing else. It is four layer names mapped to four adapter "
            "attributes, and it is the one module-level manifest site that is a declaration rather "
            "than a projection."
        ),
    ),
)


# The shipped vocabularies, read off the production registry.


def layer_directories() -> frozenset[str]:
    """Every shipped layer's storage directory."""
    registry = production_registry()
    return frozenset(registry.layer(name).storage.directory for name in registry.layer_names)


def layer_tokens() -> frozenset[str]:
    """Every spelling of a shipped layer: the ``snapshotAttributes`` and the layer names."""
    registry = production_registry()
    names = {name for name in registry.layer_names}
    attributes = {
        registry.layer(name).snapshot_attribute
        for name in registry.layer_names
        if registry.layer(name).snapshot_attribute is not None
    }
    return frozenset(names | attributes)


# The scanners.


@dataclass(frozen=True)
class Finding:
    """One reported site, with its file and line.

    *symbol* and *enclosing* (check (a) only) with *module* are the key an
    exemption is written against; the other checks leave them empty.
    """

    module: str
    line: int
    detail: str
    symbol: str = ""
    enclosing: str = ""

    def __str__(self) -> str:
        return f"{self.module}:{self.line}: {self.detail}"


def _in_source_order(findings: list[Finding]) -> list[Finding]:
    """Findings in source order; `ast.walk` is breadth-first."""
    return sorted(findings, key=lambda finding: (finding.line, finding.detail))


def _parse(module: str) -> ast.Module:
    return ast.parse((PACKAGE_ROOT / module).read_text(encoding="utf-8"), filename=module)


def _dotted(node: ast.expr) -> str:
    """``os.path`` for an attribute chain, ``""`` for anything else."""
    parts: list[str] = []
    current: Any = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return ""
    parts.append(current.id)
    return ".".join(reversed(parts))


def _import_bindings(tree: ast.AST) -> tuple[dict[str, str], dict[str, str]]:
    """Local name -> module, and local name -> ``module.function``, over the whole tree."""
    modules: dict[str, str] = {}
    functions: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            for alias in node.names:
                functions[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return modules, functions


def _enclosing_definition(tree: ast.Module, line: int) -> str:
    """The top-level definition or assignment *line* falls inside, ``""`` if none."""
    for name, node in _top_level(tree).items():
        end = getattr(node, "end_lineno", node.lineno)
        if node.lineno <= line <= (end or node.lineno):
            return name
    return ""


def traversal_findings(module: str, tree: ast.AST) -> list[Finding]:
    """Check (a): every directory-walking call in one module, classified by binding.

    An imported module other than ``os`` as receiver is not a traversal, which
    keeps ``ast.walk(tree)`` out.
    """
    modules, imported = _import_bindings(tree)
    where = (lambda line: _enclosing_definition(tree, line)) if isinstance(tree, ast.Module) else (lambda line: "")
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            bound = imported.get(node.func.id)
            origin, _, name = (bound or "").rpartition(".")
            if bound is not None and name in _TRAVERSAL_IMPORTS.get(origin, frozenset()):
                findings.append(
                    Finding(
                        module,
                        node.lineno,
                        f"traverses a directory with {bound}()",
                        symbol=name,
                        enclosing=where(node.lineno),
                    )
                )
            continue
        if not isinstance(node.func, ast.Attribute):
            continue
        attribute = node.func.attr
        if attribute in _PATH_TRAVERSAL_METHODS:
            findings.append(
                Finding(
                    module,
                    node.lineno,
                    f"traverses a directory with .{attribute}()",
                    symbol=attribute,
                    enclosing=where(node.lineno),
                )
            )
            continue
        if attribute not in _OS_TRAVERSAL_FUNCTIONS:
            continue
        root = _dotted(node.func).split(".")[0]
        receiver_module = modules.get(root) if root else None
        if receiver_module == "os":
            findings.append(
                Finding(
                    module,
                    node.lineno,
                    f"traverses a directory with os.{attribute}()",
                    symbol=attribute,
                    enclosing=where(node.lineno),
                )
            )
        elif receiver_module is None:
            findings.append(
                Finding(
                    module,
                    node.lineno,
                    f"traverses a directory with .{attribute}()",
                    symbol=attribute,
                    enclosing=where(node.lineno),
                )
            )
    return _in_source_order(findings)


def _string_constants(node: ast.AST) -> list[ast.Constant]:
    return [child for child in ast.walk(node) if isinstance(child, ast.Constant) and isinstance(child.value, str)]


def path_composition_findings(module: str, node: ast.AST, directories: frozenset[str]) -> list[Finding]:
    """Check (b1): a layer directory literal used to build a path, with or without ``/``."""
    findings: list[Finding] = []
    for child in ast.walk(node):
        operands: list[ast.expr] = []
        if isinstance(child, ast.BinOp) and isinstance(child.op, ast.Div):
            operands = [child.left, child.right]
        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            if child.func.attr in {"joinpath", *_PATH_TRAVERSAL_METHODS}:
                operands = list(child.args)
            elif child.func.attr == "join" and _dotted(child.func).rpartition(".")[0] in _JOIN_MODULES:
                operands = list(child.args)
        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id in _PATH_CONSTRUCTORS:
            operands = list(child.args)
        for operand in operands:
            if isinstance(operand, ast.Constant) and operand.value in directories:
                findings.append(
                    Finding(module, operand.lineno, f"composes a path from the layer directory {operand.value!r}")
                )
    return _in_source_order(findings)


def _adapter_annotation(annotation: ast.expr | None) -> bool:
    """Whether *annotation* is one of the adapter types, however it is spelled."""
    for child in ast.walk(annotation) if annotation is not None else ():
        if isinstance(child, ast.Name) and child.id in _ADAPTER_TYPES:
            return True
        if isinstance(child, ast.Attribute) and child.attr in _ADAPTER_TYPES:
            return True
        if isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value in _ADAPTER_TYPES:
            return True
    return False


def adapter_access_findings(module: str, node: ast.AST, tokens: frozenset[str]) -> list[Finding]:
    """Check (b2): a layer literal indexing ``state``/``by_layer`` or naming an adapter field.

    The plain attribute read (``diff.swcs``) is scoped by the receiver's
    annotation, so an ordinary ``system.tasks`` is not a finding.
    """
    aliases = {
        target.id
        for child in ast.walk(node)
        if isinstance(child, ast.Assign)
        for target in child.targets
        if isinstance(target, ast.Name)
        and isinstance(child.value, ast.Attribute)
        and child.value.attr in _ADAPTER_MAPPINGS
    }
    adapters = {
        argument.arg
        for child in ast.walk(node)
        if isinstance(child, ast.arguments)
        for argument in (*child.posonlyargs, *child.args, *child.kwonlyargs)
        if _adapter_annotation(argument.annotation)
    } | {
        child.target.id
        for child in ast.walk(node)
        if isinstance(child, ast.AnnAssign)
        and isinstance(child.target, ast.Name)
        and _adapter_annotation(child.annotation)
    }
    findings: list[Finding] = []
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and isinstance(child.ctx, ast.Load)
            and child.attr in tokens
            and isinstance(child.value, ast.Name)
            and child.value.id in adapters
        ):
            findings.append(Finding(module, child.lineno, f"reads the adapter field {child.attr!r} by name"))
            continue
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "getattr"
            and len(child.args) >= 2
            and isinstance(child.args[1], ast.Constant)
            and child.args[1].value in tokens
        ):
            findings.append(Finding(module, child.lineno, f"reads the adapter field {child.args[1].value!r} by name"))
            continue
        if not isinstance(child, ast.Subscript) or not isinstance(child.slice, ast.Constant):
            continue
        if child.slice.value not in tokens:
            continue
        holder = child.value
        indexed = (isinstance(holder, ast.Attribute) and holder.attr in _ADAPTER_MAPPINGS) or (
            isinstance(holder, ast.Name) and holder.id in aliases
        )
        if indexed:
            findings.append(
                Finding(module, child.lineno, f"indexes the state/diff adapter by the literal {child.slice.value!r}")
            )
    return _in_source_order(findings)


def declared_vocabulary_findings(module: str, name: str, node: ast.AST, tokens: frozenset[str]) -> list[Finding]:
    """Check (b3): a layer token spelled out inside a module-level manifest site."""
    return _in_source_order(
        [
            Finding(module, constant.lineno, f"{name} states the layer token {constant.value!r} as a literal")
            for constant in _string_constants(node)
            if constant.value in tokens
        ]
    )


def derived_view_findings(module: str, node: ast.AST, views: tuple[str, ...]) -> list[Finding]:
    """Check (c): a function body reading one of the retained derived views."""
    findings: list[Finding] = []
    for child in ast.walk(node):
        read = None
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id in views:
            read = child.id
        elif isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load) and child.attr in views:
            read = child.attr
        if read is not None:
            findings.append(Finding(module, child.lineno, f"reads the derived view {read}"))
    return _in_source_order(findings)


# Site resolution.


def _top_level(tree: ast.Module) -> dict[str, ast.stmt]:
    """Every top-level definition or assignment of a module, by name."""
    found: dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found[node.name] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found[node.target.id] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node
    return found


def _manifest_modules() -> tuple[str, ...]:
    return tuple(dict.fromkeys(site.module for site in MANIFEST))


def _manifest_sites(attribute: str) -> list[tuple[str, str, ast.stmt]]:
    """``(module, name, node)`` for one manifest name list (``"functions"`` or ``"module_level"``), deduplicated."""
    resolved: list[tuple[str, str, ast.stmt]] = []
    seen: set[tuple[str, str]] = set()
    for module in _manifest_modules():
        table = _top_level(_parse(module))
        for site in MANIFEST:
            if site.module != module:
                continue
            for name in getattr(site, attribute):
                if (module, name) in seen:
                    continue
                seen.add((module, name))
                resolved.append((module, name, table[name]))
    return resolved


def _manifest_functions() -> list[tuple[str, str, ast.stmt]]:
    """``(module, name, node)`` for every function the manifest names, deduplicated."""
    return _manifest_sites("functions")


def _manifest_module_level() -> list[tuple[str, str, ast.stmt]]:
    """``(module, name, node)`` for every module-level site the manifest names."""
    return _manifest_sites("module_level")


def _traversal_allowance(allow_list: tuple[Exemption, ...]) -> dict[tuple[str, str, str], int]:
    """How many calls each traversal exemption covers, keyed by module, function and symbol."""
    allowance: dict[tuple[str, str, str], int] = {}
    for entry in allow_list:
        if entry.kind != "traversal" or entry.function is None:
            continue
        key = (entry.module, entry.function, entry.symbol)
        allowance[key] = allowance.get(key, 0) + entry.calls
    return allowance


def _beyond_the_allowance(findings: list[Finding], allow_list: tuple[Exemption, ...]) -> list[Finding]:
    """*findings* with each exemption's covered calls (the first ones under its key) removed."""
    remaining = _traversal_allowance(allow_list)
    reported: list[Finding] = []
    for finding in findings:
        key = (finding.module, finding.enclosing, finding.symbol)
        if remaining.get(key, 0) > 0:
            remaining[key] -= 1
            continue
        reported.append(finding)
    return reported


def _exempt_vocabularies(allow_list: tuple[Exemption, ...]) -> set[tuple[str, str]]:
    return {(entry.module, entry.symbol) for entry in allow_list if entry.kind == "declared-vocabulary"}


def run_check_a(allow_list: tuple[Exemption, ...] = ALLOW_LIST) -> list[Finding]:
    findings: list[Finding] = []
    for module in generic_core_files():
        if module == THE_ONLY_TRAVERSAL_MODULE:
            continue
        findings.extend(traversal_findings(module, _parse(module)))
    return _beyond_the_allowance(findings, allow_list)


def run_check_b3(allow_list: tuple[Exemption, ...] = ALLOW_LIST) -> list[Finding]:
    exempt = _exempt_vocabularies(allow_list)
    tokens = layer_tokens() | layer_directories()
    return [
        finding
        for module, name, node in _manifest_module_level()
        if (module, name) not in exempt
        for finding in declared_vocabulary_findings(module, name, node, tokens)
    ]


# The allow-list is six entries, each real, each exercised.


def test_the_allow_list_is_exactly_the_six_named_entries() -> None:
    """Six entries with their reasons, keyed by `(module, function, symbol)`."""
    assert len(ALLOW_LIST) == 6
    assert [(entry.module, entry.function, entry.symbol) for entry in ALLOW_LIST] == [
        ("session.py", "_iter_model_files", "rglob"),
        ("cli/init_command.py", "_template_file_plan", "iterdir"),
        ("cli/build_commands.py", "_clean_target", "iterdir"),
        ("editing/engine.py", "_require_model_root", "scandir"),
        ("editor_schema.py", "main", "glob"),
        ("authorization.py", None, "DELETE_ELEMENT_LAYERS"),
    ]
    assert all(len(entry.reason) > 40 for entry in ALLOW_LIST), "an exemption without a reason is a hole"


@pytest.mark.parametrize(
    "entry", [entry for entry in ALLOW_LIST if entry.kind == "traversal"], ids=lambda entry: entry.module
)
def test_every_traversal_exemption_matches_a_real_call_in_its_function(entry: Exemption) -> None:
    """The named function is real, carries that call, and carries it exactly `calls` times."""
    calls = [(finding.enclosing, finding.symbol) for finding in traversal_findings(entry.module, _parse(entry.module))]
    matching = calls.count((entry.function, entry.symbol))
    assert matching == entry.calls, (
        f"{entry.module}:{entry.function} carries {matching} {entry.symbol}() call(s), "
        f"but the exemption covers {entry.calls}; found {sorted(set(calls))}"
    )


def test_the_declared_vocabulary_exemption_matches_a_real_assignment() -> None:
    """`DELETE_ELEMENT_LAYERS` exists, is module-level, and really does carry layer tokens."""
    (entry,) = [item for item in ALLOW_LIST if item.kind == "declared-vocabulary"]
    table = _top_level(_parse(entry.module))
    assert entry.symbol in table, f"{entry.symbol} is not a module-level name of {entry.module}"
    spelled = declared_vocabulary_findings(
        entry.module, entry.symbol, table[entry.symbol], layer_tokens() | layer_directories()
    )
    assert spelled, f"{entry.symbol} no longer spells a layer token; the exemption is dead"


@pytest.mark.parametrize(
    "entry", [entry for entry in ALLOW_LIST if entry.kind == "traversal"], ids=lambda entry: entry.module
)
def test_every_traversal_exemption_is_exercised(entry: Exemption) -> None:
    """Drop it and the gate reports it — so the entry is load-bearing, not decorative."""
    reduced = tuple(item for item in ALLOW_LIST if item is not entry)
    reported = {(finding.module, finding.enclosing, finding.symbol) for finding in run_check_a(reduced)}
    assert reported == {(entry.module, entry.function, entry.symbol)}, (
        f"dropping the {entry.module} exemption should report exactly that call; got {sorted(reported)}"
    )


def test_the_declared_vocabulary_exemption_is_exercised() -> None:
    """The same proof for the fourth entry, which is not a call at all."""
    (entry,) = [item for item in ALLOW_LIST if item.kind == "declared-vocabulary"]
    reduced = tuple(item for item in ALLOW_LIST if item is not entry)
    reported = run_check_b3(reduced)
    assert reported, "dropping the DELETE_ELEMENT_LAYERS exemption should report it"
    assert {finding.module for finding in reported} == {entry.module}
    assert all(entry.symbol in finding.detail for finding in reported), [str(f) for f in reported]


# Check (a): one layer walk, in one module.


def test_the_traversal_vocabulary_is_the_names_the_check_resolves() -> None:
    """The traversal vocabulary, as a literal against the derived constant.

    The literal is on this side so a name added to or dropped from either
    frozenset fails here.
    """
    assert TRAVERSAL_VOCABULARY == ("glob", "iglob", "iterdir", "listdir", "rglob", "scandir", "walk")


def test_the_generic_core_file_list_covers_every_editing_module() -> None:
    """`editing/`, `cli/` and `query/` are enumerated from disk, so a new module cannot arrive unscanned.

    Pinned by name as well, since a lower bound would pass with modules deleted.
    """
    files = generic_core_files()
    assert set(_NAMED_GENERIC_CORE_FILES) <= set(files)
    editing = {name for name in files if name.startswith("editing/")}
    assert editing == {path.relative_to(PACKAGE_ROOT).as_posix() for path in (PACKAGE_ROOT / "editing").rglob("*.py")}
    named = {
        "editing/address.py",
        "editing/apply.py",
        "editing/delta.py",
        "editing/derive.py",
        "editing/discovery.py",
        "editing/engine.py",
        "editing/passes.py",
        "editing/read.py",
        "editing/registry.py",
        "editing/staging.py",
    }
    assert named <= editing, sorted(named - editing)
    command_line = {name for name in files if name.startswith("cli/")}
    assert command_line == {path.relative_to(PACKAGE_ROOT).as_posix() for path in (PACKAGE_ROOT / "cli").rglob("*.py")}
    assert {"cli/__init__.py", "cli/init_command.py", "cli/build_commands.py", "cli/dispatch.py"} <= command_line, (
        sorted(command_line)
    )
    query = {name for name in files if name.startswith("query/")}
    assert query == {path.relative_to(PACKAGE_ROOT).as_posix() for path in (PACKAGE_ROOT / "query").rglob("*.py")}
    assert {"query/__init__.py", "query/edges.py"} <= query, sorted(query)
    assert all((PACKAGE_ROOT / name).is_file() for name in files)


#: Every top-level module that is deliberately not generic core, with its reason; the
#: partition below keeps a new module from arriving beside the named list unscanned.
_NOT_GENERIC_CORE: dict[str, str] = {
    "__init__.py": "the package marker; no code",
    "_version.py": "the one version string",
    "agent_clients.py": "the chat clients' table reader (templates/clients.json); a stdlib leaf, and names no layer",
    "agents_md.py": "the project AGENTS.md rendered from the agent profile's text; a stdlib leaf, and names no layer",
    "allocation.py": "a partial consumer, the allocation experiment's row",
    "arxml_elements.py": "the ARXML element walk the two reports share; reads ARXML, imports lxml alone",
    "arxml_reader.py": "reads ARXML, not the model",
    "arxml_validation.py": "validates ARXML against a schema",
    "arxml_version.py": "the AUTOSAR version vocabulary",
    "auditor.py": "the audit log; names no layer",
    "bm25.py": "BM25 and BM25F over bags of opaque terms, shared by the trace link search and the guide; a stdlib leaf",
    "build_report.py": "renders a build's outcome; reads the model through the resource report only, names no layer",
    "builder.py": "runs the generators; names no layer",
    "capability_markers.py": "the capability markers the writer mints and the reader admits; a leaf, names no layer",
    "change_report.py": "a partial consumer, the change report's row; reads the model through the engines it calls",
    "change_report_render.py": "renders a change report's documents; handed the report, never a model element",
    "copilot_guard.py": "the Copilot hook's decision over file paths; reads model_root, names no layer",
    "data_arrival.py": "a partial consumer, the arrivals' row; walks the components, connectors and compositions",
    "data_description.py": "the resolved view of the data description; names the layers it resolves",
    "diagnostics.py": "the diagnostic record type",
    "entry.py": "the console script's dispatch; reads no model",
    "env.py": "environment lookup",
    "exclusive_create.py": "the exclusive create that follows no link; a stdlib leaf, and names no layer",
    "execution_budget.py": "the execution-time budget grammar and its CSE codes; a leaf, and names no layer",
    "experiment_frame.py": "the experiment frame's kind-independent pieces; reads the layer files through the kernel",
    "guarantee.py": "the explicit presence and class checks (required, required_instance); a stdlib leaf",
    "guard_model_paths.py": "the guard's two rules on a written path; a leaf, and names no layer",
    "guide_corpus.py": "builds the guide's corpus from its Markdown; reads no model at all",
    "guide_search.py": "composes the guide tool's replies from the guide's corpus; reads no model at all",
    "guide_site.py": "locates the installed guide's directory; reads no model at all",
    "identifier.py": "the AUTOSAR Identifier rule; a leaf",
    "implicit_access.py": "the resource report's implicit-access count, on its row; reads components and tasks",
    "kiro_paths.py": "the readings Kiro and Node give a written path; a stdlib leaf, and names no layer",
    "markdown_cells.py": "the safe-Markdown table helpers the two reports share; a leaf, and names no layer",
    "metrics.py": "a partial consumer, the architecture metrics' row; reads the model through the engines it calls",
    "metrics_conclusions.py": "the architecture metrics' sentences; handed the report, never a model element",
    "metrics_render.py": "renders the architecture metrics' documents; handed the report, never a model element",
    "passport.py": "a partial consumer, the model passport's row; counts every layer's elements",
    "project.py": "the manifest; names no layer",
    "rendering.py": "a value or an exception into message text; a leaf, and names no layer",
    "requirements_import.py": "a partial consumer, the requirements import's row; reads the layers through the kernel",
    "requirements_interchange.py": "reads the requirements interchange JSON, not the model; names no layer",
    "requirements_mapping.py": "reads the needs mapping YAML of import requirements, not the model; names no layer",
    "resource_analysis.py": "a partial consumer; names its layers through the registry",
    "resource_report.py": "renders a resource analysis",
    "safety.py": "the ASIL comparison order; a leaf, and names no layer",
    "scheduling_identity.py": "the scheduling identity a task entry resolves to; reads the composition layer by name",
    "schema.py": "the Pydantic model itself — what the contract binds to",
    "swcraft_home.py": "the user's SWCraft home and the review hand-off directory; a stdlib leaf, and names no layer",
    "tables_for_testing.py": "a partial consumer, the test tables' row; projects components, compositions and tasks",
    "tilde_paths.py": "the guard's readings of a leading ~; a leaf, and names no layer",
    "toolchain_pin.py": "the toolchain pin's sentences, from the manifest; a leaf, and names no layer",
    "trace.py": "the trace link grammar and the five linkable layers' tokens; a leaf",
    "trace_address.py": "the trace search's target address and its orders; a stdlib leaf, and names no layer",
    "trace_apply.py": "a partial consumer, the trace experiment's row",
    "trace_records.py": "the trace records, the proposals and decisions files",
    "trace_search.py": "a partial consumer, the trace experiment's row",
    "trace_satisfies_text.py": "the trace Apply's text edit of satisfies; names no layer",
    "trace_terms.py": "the trace terms, a leaf the search and the import share",
    "wire_unit.py": "the wire unit, the bytes of one MCP tool result; a stdlib leaf, and names no layer",
    "xml_text.py": "which characters XML 1.0 cannot carry; a leaf, and names no layer",
    "yaml_load.py": "the one YAML reader, libyaml; a leaf, and names no layer",
    "yaml_text_edit.py": "the primitives of editing a YAML file's text; a leaf, and names no layer",
}


#: The modules of ``guide_reference/`` that are not generic core, with the reason each
#: one is not; needed because the top-level partition's glob does not descend.
_GUIDE_REFERENCE_NOT_GENERIC_CORE: dict[str, str] = {
    "__init__.py": "re-exports the catalogue's names; no code of its own",
    "catalogue.py": "assembles the page sets and threads a registry it never reads",
    "__main__.py": "writes the rendered pages and rewrites the nav; reads no model at all",
    "render.py": "the shared Markdown emitters; imports nothing of swcraft_tools",
    "cli.py": "walks argparse's own tree; reads the parser, never a layer",
    "interchange.py": "copies the package-data interchange schema into one page; reads no model",
    "passport.py": "copies the package-data passport schema into one page; reads no model",
}


#: The modules of ``validator/``, a retained partial consumer, none of them generic core.
_VALIDATOR_NOT_GENERIC_CORE: dict[str, str] = {
    "__init__.py": "the package's facade; re-exports its modules' names, no code of its own",
    "access.py": "the runnable access rules; a module of the validator, a retained partial consumer",
    "asil.py": "the ASIL allocation and coexistence rules; a module of the validator",
    "behaviour.py": "the behavioural value rules; a module of the validator",
    "compatibility.py": "type resolution and interface compatibility; a module of the validator",
    "composer.py": "the cross-layer composer; a module of the validator",
    "compositions.py": "the composition rules; a module of the validator",
    "connectors.py": "the connector rules; a module of the validator",
    "data_types.py": "the data type rules; a module of the validator",
    "derived.py": "the derived values a read asks for; a module of the validator",
    "description_elements.py": "the data description elements' rules; a module of the validator",
    "events.py": "the event and identifier rules; a module of the validator",
    "generated_names.py": "the names the writer generates; a module of the validator",
    "hardware.py": "the hardware topology, budget and capability rules; a module of the validator",
    "identifiers.py": "the layers whose names are identifiers; a module of the validator",
    "init_values.py": "the init value and com spec rules; a module of the validator",
    "model_view.py": "the view every check reads the model through; calls the named layer readers",
    "names.py": "the name uniqueness and name space rules; a module of the validator",
    "representable_text.py": "the XML-representable text rule; a module of the validator",
    "results.py": "the result types and the severity of a rule; names no layer",
    "rules.py": "the closed rule inventory; names no layer",
    "scheduling.py": "the task and sequence rules; a module of the validator",
    "storage.py": "the storage portability and unknown-key rules; a module of the validator",
    "traceability.py": "the requirement traceability rules; a module of the validator",
}


def test_every_validator_module_is_named_as_not_generic_core() -> None:
    """The partition of ``validator/``: every module on disk is named, and every name is a module."""
    on_disk = {path.name for path in (PACKAGE_ROOT / "validator").glob("*.py")}
    scanned = {name for name in _NAMED_GENERIC_CORE_FILES if name.startswith("validator/")}

    assert scanned == set()
    assert set(_VALIDATOR_NOT_GENERIC_CORE) == on_disk, sorted(on_disk ^ set(_VALIDATOR_NOT_GENERIC_CORE))
    assert all(len(reason) > 10 for reason in _VALIDATOR_NOT_GENERIC_CORE.values())


def test_every_guide_reference_module_is_generic_core_or_named_as_not() -> None:
    """The partition of `guide_reference/`, both directions: every module on disk is in exactly one list."""
    package = PACKAGE_ROOT / "guide_reference"
    on_disk = {path.name for path in package.glob("*.py")}
    scanned = {name.split("/", 1)[1] for name in _NAMED_GENERIC_CORE_FILES if name.startswith("guide_reference/")}

    assert scanned, "no module of guide_reference/ is scanned by check (a) any more"
    assert scanned | set(_GUIDE_REFERENCE_NOT_GENERIC_CORE) == on_disk, sorted(
        on_disk - scanned - set(_GUIDE_REFERENCE_NOT_GENERIC_CORE)
    )
    assert scanned & set(_GUIDE_REFERENCE_NOT_GENERIC_CORE) == set()
    assert all(len(reason) > 10 for reason in _GUIDE_REFERENCE_NOT_GENERIC_CORE.values())


def test_every_top_level_module_is_generic_core_or_named_as_not() -> None:
    """The top-level partition, both directions: every module is scanned or excused by name."""
    on_disk = {path.name for path in PACKAGE_ROOT.glob("*.py")}
    generic = {name for name in _NAMED_GENERIC_CORE_FILES if "/" not in name}

    assert generic | set(_NOT_GENERIC_CORE) == on_disk, sorted(on_disk - generic - set(_NOT_GENERIC_CORE))
    assert generic & set(_NOT_GENERIC_CORE) == set()
    assert all(len(reason) > 10 for reason in _NOT_GENERIC_CORE.values()), "an excused module without a reason"


def test_no_generic_core_module_traverses_a_layer_directory() -> None:
    """`model_state.py` is the only layer walk in the generic core."""
    findings = run_check_a()
    assert findings == [], [str(finding) for finding in findings]


def test_model_state_really_is_where_the_one_walk_lives() -> None:
    """The other direction: `model_state.py` holds exactly its three walks, by function.

    A gate that only forbids would pass an empty package; a fourth walk, or one
    moving to another function, fails here by name.
    """
    findings = traversal_findings(THE_ONLY_TRAVERSAL_MODULE, _parse(THE_ONLY_TRAVERSAL_MODULE))
    assert sorted((finding.enclosing, finding.symbol) for finding in findings) == [
        ("_held_names", "scandir"),
        ("_retired_element_files", "glob"),
        ("scan_layer", "glob"),
    ], [str(finding) for finding in findings]


# Check (b): no migrated site spells a layer out.


def test_the_shipped_vocabularies_are_the_fifteen_layers() -> None:
    """The literals the checks compare against are read off the contract, not typed here."""
    # The contract's layer count; a layer added or retired moves it.
    assert len(layer_directories()) == 15
    assert "swc" in layer_directories()
    assert {"swc", "swcs"} <= layer_tokens()


def test_no_migrated_site_composes_a_path_from_a_layer_directory_literal() -> None:
    """Check (b1) over every manifest function body; (b3) already covers the module level."""
    directories = layer_directories()
    findings = [
        finding
        for module, _name, node in _manifest_functions()
        for finding in path_composition_findings(module, node, directories)
    ]
    assert findings == [], [str(finding) for finding in findings]


def test_no_migrated_site_indexes_the_adapter_by_a_layer_literal() -> None:
    """Check (b2): `state`, `by_layer` and `getattr` are reached by variable, never by name."""
    tokens = layer_tokens()
    findings = [
        finding
        for module, _name, node in _manifest_functions()
        for finding in adapter_access_findings(module, node, tokens)
    ]
    assert findings == [], [str(finding) for finding in findings]


def test_every_module_level_migrated_site_is_a_projection_and_not_a_table() -> None:
    """Check (b3), with the one declared vocabulary allow-listed."""
    findings = run_check_b3()
    assert findings == [], [str(finding) for finding in findings]


# Check (c): no migrated function reads a derived view.


def test_no_deleted_derived_view_is_declared_anywhere_in_the_package() -> None:
    """The thirteen deleted derived views are declared by no module of the package.

    AST, not grep: prose naming a view is fine, a top-level binding is the view
    coming back. The count is asserted so a shortened table cannot reduce the scan.
    """
    expected = {name for names in DELETED_DERIVED_VIEWS.values() for name in names}
    assert len(expected) == 13, sorted(expected)

    declared: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py"), key=lambda path: path.as_posix()):
        module = path.relative_to(PACKAGE_ROOT).as_posix()
        for name in _top_level(ast.parse(path.read_text(encoding="utf-8"))):
            if name in expected:
                origin = next(mod for mod, names in DELETED_DERIVED_VIEWS.items() if name in names)
                declared.append(f"{module}:{name} (deleted from {origin})")
    assert declared == [], f"a deleted derived view is declared again: {declared}"


#: The substrings that make a paragraph's mention of a deleted name historical,
#: matched case-insensitively per paragraph (a deletion is stated once for a list of names).
PAST_TENSE_MARKERS: tuple[str, ...] = (
    "deleted",
    "delet",
    "was ",
    "were ",
    "used to",
    "no longer",
    "removed",
    "went with",
    "until ",
    "before ",
    "stopped",
    "retired",
    "replaced",
    "existed",
    "gone",
)


def _paragraphs(text: str) -> list[tuple[int, str]]:
    """``(1-based start line, paragraph)`` for every blank-line-separated block."""
    blocks: list[tuple[int, str]] = []
    position = 0
    for block in text.split("\n\n"):
        blocks.append((text[:position].count("\n") + 1, block))
        position += len(block) + 2
    return blocks


def deleted_name_prose_findings(text: str) -> list[tuple[int, tuple[str, ...], str]]:
    """Every paragraph of *text* naming a deleted symbol with no past-tense marker."""
    names = (
        {name for group in DELETED_DERIVED_VIEWS.values() for name in group}
        | set(DELETED_PROJECTED_TABLES)
        | set(DELETED_RESOURCES_LAYER_NAMES)
    )
    findings: list[tuple[int, tuple[str, ...], str]] = []
    for line, block in _paragraphs(text):
        hits = tuple(
            sorted(name for name in names if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", block))
        )
        if hits and not any(marker in block.lower() for marker in PAST_TENSE_MARKERS):
            findings.append((line, hits, " ".join(block.split())[:160]))
    return findings


@pytest.mark.parametrize(
    ("prose", "reported"),
    [
        pytest.param(
            "The registry does not invent the answer. `differ.py`'s `COLLECTION_IDENTITY` already\n"
            "decides it for all twelve nested collections.",
            True,
            id="present-tense-is-reported",
        ),
        pytest.param(
            "`differ.py`'s `COLLECTION_IDENTITY` decided it for all twelve nested collections until\n"
            "the view was deleted.",
            False,
            id="past-tense-passes",
        ),
        pytest.param(
            "`build_registry` still accepts `LAYER_SPECS` and `PLACEMENT_FIELDS` as keywords.",
            True,
            id="a-projected-table-name-counts-too",
        ),
        pytest.param(
            "The synthetic layer needs no `LAYER_SPECS_EXTRA` entry.",
            False,
            id="a-longer-name-is-not-a-match",
        ),
    ],
)
def test_the_deleted_name_prose_scan_bites(prose: str, reported: bool) -> None:
    """The deleted-name prose classifier bites, and respects the word boundary."""
    assert bool(deleted_name_prose_findings(prose)) is reported


def test_no_projected_table_name_is_declared_anywhere_in_the_package() -> None:
    """The six deleted projected table names are declared by no module of the package.

    The two deletion records must not overlap, or a name would be misattributed.
    """
    views = {name for names in DELETED_DERIVED_VIEWS.values() for name in names}
    expected = set(DELETED_PROJECTED_TABLES)
    assert len(expected) == len(DELETED_PROJECTED_TABLES) == 6, sorted(expected)
    assert not (expected & views), sorted(expected & views)

    declared: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py"), key=lambda path: path.as_posix()):
        module = path.relative_to(PACKAGE_ROOT).as_posix()
        for name in _top_level(ast.parse(path.read_text(encoding="utf-8"))):
            if name in expected:
                declared.append(f"{module}:{name}")
    assert declared == [], f"a projected table deleted as a name is declared again: {declared}"


def test_the_registry_names_that_replaced_them_are_reachable() -> None:
    """Deleting the six names kept the facts and the perturbation seam reachable.

    `model_registry`, the seventh keyword, is the kernel registry the tables were
    projected from; a perturbed projection claims none, so its default is `None`
    too. Resolved on the imported module, because reachability is the claim.
    """
    import inspect

    from swcraft_tools.editing import registry as reg

    missing = [name for name in SURVIVING_REGISTRY_NAMES if not hasattr(reg, name)]
    assert missing == [], f"the registry names that replaced the six deleted tables are gone: {missing}"

    parameters = inspect.signature(reg.build_registry).parameters
    assert set(parameters) == {
        "layer_specs",
        "kind_names",
        "kind_descriptions",
        "field_annotations",
        "collection_specs",
        "placement_fields",
        "model_registry",
    }
    assert all(parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in parameters.values())
    assert all(parameter.default is None for parameter in parameters.values()), (
        "a table is bound in a default-argument expression again; the fallback must resolve in the body"
    )

    # A `None` default alone does not prove the fallback is the shipped projection.
    assert [layer.name for layer in reg.build_registry().layers] == [
        spec.name for spec in reg._PRODUCTION_TABLES.layer_specs
    ]


#: Every test module that read one of the deleted views, and the views it read;
#: asserted below to be exhaustive against :data:`DELETED_DERIVED_VIEWS`.
CONVERTED_VIEW_READERS: dict[str, tuple[str, ...]] = {
    "test_session.py": ("_LAYER_DIRECTORIES", "_MODEL_SUBDIRS"),
    "test_session_generic.py": ("_LAYER_DIRECTORIES", "_RECURSIVE_LAYER_DIRECTORIES"),
    "test_cli.py": ("_MODEL_SUBDIRS",),
    "test_cli_scaffold_generic.py": ("_MODEL_SUBDIRS",),
    "test_reader.py": ("UNGROUPED_SWC_PLACEMENT",),
    "test_reader_generic.py": ("LAYER_GLOBS", "SWC_PLACEMENT_FIELD", "UNGROUPED_SWC_PLACEMENT"),
    "test_model_relations.py": ("_LAYER_KEY_FNS",),
    "test_differ.py": ("COLLECTION_IDENTITY",),
    "test_differ_identity.py": ("COLLECTION_IDENTITY",),
    "test_differ_coverage.py": ("COLLECTION_IDENTITY",),
    "test_model_state_diff.py": ("COLLECTION_IDENTITY", "_LAYER_DISPLAY"),
    "test_model_contract.py": ("_LAYER_DISPLAY",),
    "test_authorization_generic.py": ("_LAYER_MODELS", "LAYER_NAMES", "LAYER_FIELDS", "LAYER_IDENTITY_FIELDS"),
    "test_authorization_scope.py": ("LAYER_NAMES", "LAYER_FIELDS", "LAYER_IDENTITY_FIELDS"),
}

#: The kernel entry points a converted reader must reach for.
KERNEL_ENTRY_POINTS = frozenset({"production_registry", "current_registry", "use_registry"})

#: The import path fragments a deleted view could be reached through: the module that declared it and, for a
#: module of a package, the package, whose facade a reader could name instead.
_VIEW_OWNERS = {
    name: tuple(module.removesuffix(".py").split("/"))
    for module, names in DELETED_DERIVED_VIEWS.items()
    for name in names
}


def _imports_and_attribute_reads(tree: ast.AST) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """``({(module, imported name)}, {(receiver, attribute)})`` over a whole module."""
    imported: set[tuple[str, str]] = set()
    attributes: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.update((node.module.rsplit(".", 1)[-1], alias.name) for alias in node.names)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            attributes.add((node.value.id, node.attr))
    return imported, attributes


def test_every_converted_view_reader_reaches_the_kernel_instead() -> None:
    """Each listed reader reaches a kernel entry point and no deleted view.

    Does not reach: that the local projection is computed *from* that entry
    point, only that both facts hold in the same module.
    """
    offenders: dict[str, list[str]] = {}
    for module, views in sorted(CONVERTED_VIEW_READERS.items()):
        tree = ast.parse((TESTS_ROOT / module).read_text(encoding="utf-8"))
        imported, attributes = _imports_and_attribute_reads(tree)
        problems = [
            f"still imports {name} from {owner}"
            for name in views
            for owner in _VIEW_OWNERS[name]
            if (owner, name) in imported
        ]
        problems += [
            f"still reads {owner}.{name}"
            for name in views
            for owner in _VIEW_OWNERS[name]
            if (owner, name) in attributes
        ]
        if not {name for _module, name in imported} & KERNEL_ENTRY_POINTS:
            problems.append(f"reaches no kernel entry point ({sorted(KERNEL_ENTRY_POINTS)})")
        if problems:
            offenders[module] = problems
    assert offenders == {}, offenders


def test_the_converted_reader_table_covers_every_deleted_view() -> None:
    """Every deleted view appears against at least one reader, and every reader exists."""
    covered = {name for names in CONVERTED_VIEW_READERS.values() for name in names}
    expected = {name for names in DELETED_DERIVED_VIEWS.values() for name in names}
    assert covered == expected, sorted(expected ^ covered)
    for module in CONVERTED_VIEW_READERS:
        assert (TESTS_ROOT / module).is_file(), module


def test_every_module_that_declared_a_deleted_view_still_exists() -> None:
    """A positive control: each declaring module still exists and is a manifest module."""
    for module in DELETED_DERIVED_VIEWS:
        assert (PACKAGE_ROOT / module).is_file(), module
    assert set(DELETED_DERIVED_VIEWS) <= set(_manifest_modules()), sorted(
        set(DELETED_DERIVED_VIEWS) - set(_manifest_modules())
    )


# The manifest itself.


def test_every_manifest_site_names_a_real_top_level_node() -> None:
    """A renamed function must fail here rather than quietly shrink the scan."""
    for module in _manifest_modules():
        table = _top_level(_parse(module))
        for site in MANIFEST:
            if site.module != module:
                continue
            missing = [name for name in (*site.functions, *site.module_level) if name not in table]
            assert missing == [], f"{site.row}: {module} has no {missing}"


def test_the_manifest_functions_are_functions_and_the_module_level_sites_are_not() -> None:
    """The two scans are over different node kinds; mixing them would silence check (c)."""
    for module, name, node in _manifest_functions():
        assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef), f"{module}.{name} is not a function"
    for module, name, node in _manifest_module_level():
        assert isinstance(node, ast.Assign | ast.AnnAssign), f"{module}.{name} is not an assignment"


def test_the_manifest_does_not_reach_the_retained_partial_consumers() -> None:
    """The retained partial consumers are outside the manifest; `editing/diagnosis.py` is inside it.

    The partial consumers are not generic-core files. `editing/diagnosis.py`
    belongs to the total `editing` consumer, so it is asserted to be scanned.
    """
    scanned = set(_manifest_modules())
    assert scanned.isdisjoint({f"validator/{name}" for name in _VALIDATOR_NOT_GENERIC_CORE})
    assert scanned.isdisjoint(
        {
            "writer/elements.py",
            "component_exchange/import_.py",
            "component_exchange/export.py",
        }
    )
    assert "editing/diagnosis.py" in scanned


def test_the_placement_table_reads_the_state_by_layer_name() -> None:
    """`editing/read.py`'s `_placement_table` asks the state, by layer name.

    Guards a composed `f"{layer.name}_placement"` attribute, which missed under
    any nesting layer not named `swc` and published no placement. A name built at
    run time is invisible to check (b), so only a named assertion holds it.
    """
    table = _top_level(_parse("editing/read.py"))
    node = table["_placement_table"]
    composed = [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.JoinedStr)
        and any(isinstance(part, ast.Constant) and part.value == "_placement" for part in child.values)
    ]
    assert composed == [], "_placement_table composes an attribute name again"
    calls = [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "placement_of"
    ]
    assert len(calls) == 1, "_placement_table no longer reads the state's placement by layer name"
    literals = [
        constant.value for constant in _string_constants(node) if constant.value in layer_tokens() | layer_directories()
    ]
    assert literals == [], f"_placement_table now spells a layer out: {literals}"


# Perturbations: the gate bites, and it does not bite payloads.


def _perturbed(tmp_path: Path, name: str, source: str) -> tuple[str, ast.Module]:
    """Write a perturbed module to a temporary file and parse it from there."""
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return str(path), ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_a_literal_injected_into_a_guarded_path_expression_is_reported(tmp_path: Path) -> None:
    """b1 bites: the join `_target_path` replaced, put back by hand."""
    source = (
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def _target_path(model_root, identity):\n"
        '    return Path(model_root) / "swc" / f"{identity}.yaml"\n'
    )
    module, tree = _perturbed(tmp_path, "mutations_perturbed.py", source)
    node = _top_level(tree)["_target_path"]

    findings = path_composition_findings(module, node, layer_directories())

    assert [str(finding) for finding in findings] == [f"{module}:5: composes a path from the layer directory 'swc'"]


def test_a_literal_composed_without_the_operator_is_reported(tmp_path: Path) -> None:
    """b1 bites for `Path(...)` and `os.path.join(...)`, and not for `", ".join(...)`."""
    source = (
        "import os.path\n"
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def _target_path(model_root, identity):\n"
        '    a = Path(model_root, "swc", f"{identity}.yaml")\n'
        '    b = os.path.join(model_root, "interfaces")\n'
        '    label = ", ".join(["tasks", "sequences"])\n'
        "    return a, b, label\n"
    )
    module, tree = _perturbed(tmp_path, "mutations_composed_perturbed.py", source)
    node = _top_level(tree)["_target_path"]

    findings = path_composition_findings(module, node, layer_directories())

    assert [str(finding) for finding in findings] == [
        f"{module}:6: composes a path from the layer directory 'swc'",
        f"{module}:7: composes a path from the layer directory 'interfaces'",
    ]


def test_a_literal_injected_into_a_guarded_adapter_access_is_reported(tmp_path: Path) -> None:
    """b2 bites for `getattr`, `state[...]`, `by_layer[...]` and a local alias."""
    source = (
        "def _touched(snapshot, diff):\n"
        '    a = getattr(snapshot, "swcs")\n'
        '    b = snapshot.state["swc"]\n'
        '    c = diff.by_layer["interfaces"]\n'
        "    by_layer = diff.by_layer\n"
        '    d = by_layer["tasks"]\n'
        "    return a, b, c, d\n"
    )
    module, tree = _perturbed(tmp_path, "session_perturbed.py", source)
    node = _top_level(tree)["_touched"]

    findings = adapter_access_findings(module, node, layer_tokens())

    assert [str(finding) for finding in findings] == [
        f"{module}:2: reads the adapter field 'swcs' by name",
        f"{module}:3: indexes the state/diff adapter by the literal 'swc'",
        f"{module}:4: indexes the state/diff adapter by the literal 'interfaces'",
        f"{module}:6: indexes the state/diff adapter by the literal 'tasks'",
    ]


def test_a_plain_adapter_attribute_read_is_reported(tmp_path: Path) -> None:
    """b2 bites for `diff.swcs` on an annotated adapter, not for an unannotated `system.tasks`."""
    source = (
        "from swcraft_tools.differ import ModelDiff\n"
        "from swcraft_tools.snapshot import ModelSnapshot\n"
        "\n"
        "\n"
        "def _touched(diff: ModelDiff, snapshot: ModelSnapshot, system):\n"
        "    a = diff.swcs\n"
        "    b = snapshot.interfaces\n"
        "    c = system.tasks\n"
        "    return a, b, c\n"
    )
    module, tree = _perturbed(tmp_path, "impact_perturbed.py", source)
    node = _top_level(tree)["_touched"]

    findings = adapter_access_findings(module, node, layer_tokens())

    assert [str(finding) for finding in findings] == [
        f"{module}:6: reads the adapter field 'swcs' by name",
        f"{module}:7: reads the adapter field 'interfaces' by name",
    ]


def test_payload_keys_that_merely_spell_a_layer_are_not_reported(tmp_path: Path) -> None:
    """A payload key, a string comparison or a returned constant spelling a layer is not reported."""
    source = (
        "def _payload(element, kind):\n"
        '    document = {"swc": element, "interfaces": [], "layer": "swc"}\n'
        '    if kind == "data_types":\n'
        '        document["tasks"] = element\n'
        '    return document, "requirements"\n'
    )
    module, tree = _perturbed(tmp_path, "payload_perturbed.py", source)
    node = _top_level(tree)["_payload"]

    assert path_composition_findings(module, node, layer_directories()) == []
    assert adapter_access_findings(module, node, layer_tokens()) == []


def test_a_derived_view_read_inside_a_manifest_function_is_reported(tmp_path: Path) -> None:
    """c bites, for a bare name and for an attribute read of the same view."""
    source = (
        "def _is_explained(relative_path, session_mod):\n"
        "    directory = relative_path.split('/')[0]\n"
        "    if directory in _LAYER_DIRECTORIES:\n"
        "        return True\n"
        "    return directory in session_mod._RECURSIVE_LAYER_DIRECTORIES\n"
    )
    module, tree = _perturbed(tmp_path, "session_views_perturbed.py", source)
    node = _top_level(tree)["_is_explained"]

    # An explicit probe list: no view is retained, so this is what shows clause (c) bites.
    findings = derived_view_findings(module, node, ("_LAYER_DIRECTORIES", "_RECURSIVE_LAYER_DIRECTORIES"))

    assert [str(finding) for finding in findings] == [
        f"{module}:3: reads the derived view _LAYER_DIRECTORIES",
        f"{module}:5: reads the derived view _RECURSIVE_LAYER_DIRECTORIES",
    ]


def test_a_traversal_injected_into_a_generic_core_module_is_reported(tmp_path: Path) -> None:
    """(a) bites for glob, rglob, iterdir, scandir, listdir and walk."""
    source = (
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def _read_layer(model_root):\n"
        '    a = list(Path(model_root).glob("*.yaml"))\n'
        '    b = list(Path(model_root).rglob("*.yaml"))\n'
        "    c = list(Path(model_root).iterdir())\n"
        "    d = list(os.scandir(model_root))\n"
        "    e = os.listdir(model_root)\n"
        "    f = list(os.walk(model_root))\n"
        "    return a, b, c, d, e, f\n"
    )
    module, tree = _perturbed(tmp_path, "reader_perturbed.py", source)

    findings = traversal_findings(module, tree)

    assert [finding.line for finding in findings] == [6, 7, 8, 9, 10, 11]


def test_a_second_call_of_an_exempted_symbol_in_the_same_function_is_reported(tmp_path: Path) -> None:
    """A second call under one exemption key is reported unless `Exemption.calls` covers it."""
    source = (
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def _iter_model_files(model_root):\n"
        '    a = sorted(Path(model_root).rglob("*.yaml"))\n'
        '    b = sorted(Path(model_root).rglob("*.yml"))\n'
        "    return a + b\n"
    )
    module, tree = _perturbed(tmp_path, "session_duplicate_perturbed.py", source)
    entry = Exemption(
        module=module,
        kind="traversal",
        symbol="rglob",
        function="_iter_model_files",
        reason="the whole-tree fingerprint, as the real entry states at length",
    )

    findings = traversal_findings(module, tree)

    assert [finding.line for finding in findings] == [5, 6]
    assert [str(finding) for finding in _beyond_the_allowance(findings, (entry,))] == [
        f"{module}:6: traverses a directory with .rglob()"
    ]
    covered = replace(entry, calls=2)
    assert _beyond_the_allowance(findings, (covered,)) == []


def test_a_local_helper_named_walk_is_not_a_traversal(tmp_path: Path) -> None:
    """A local helper named `walk` and `ast.walk` are not traversals."""
    source = (
        "import ast\n"
        "\n"
        "\n"
        "def _plan(node, tree):\n"
        "    def walk(child, prefix):\n"
        "        return prefix\n"
        "\n"
        '    walk(node, "")\n'
        "    return list(ast.walk(tree))\n"
    )
    module, tree = _perturbed(tmp_path, "cli_perturbed.py", source)

    assert traversal_findings(module, tree) == []


def test_a_traversal_reached_by_another_binding_is_reported(tmp_path: Path) -> None:
    """(a) bites for a bare imported `listdir`, an aliased `os`, `Path.walk()` and `iglob`."""
    source = (
        "import ast\n"
        "import os as _os\n"
        "from glob import iglob\n"
        "from os import listdir\n"
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def _read_layer(model_root, tree):\n"
        "    a = listdir(model_root)\n"
        "    b = _os.listdir(model_root)\n"
        "    c = list(Path(model_root).walk())\n"
        '    d = list(iglob("*.yaml"))\n'
        "    e = list(ast.walk(tree))\n"
        "    return a, b, c, d, e\n"
    )
    module, tree = _perturbed(tmp_path, "reader_bindings_perturbed.py", source)

    findings = traversal_findings(module, tree)

    assert [str(finding) for finding in findings] == [
        f"{module}:9: traverses a directory with os.listdir()",
        f"{module}:10: traverses a directory with os.listdir()",
        f"{module}:11: traverses a directory with .walk()",
        f"{module}:12: traverses a directory with glob.iglob()",
    ]
