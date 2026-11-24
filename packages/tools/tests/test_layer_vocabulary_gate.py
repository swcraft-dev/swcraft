"""The test suite's own layer vocabulary: no hand-kept layer list in fixtures or instructions.

Gate A: the three shipped copies of the Copilot instruction document carry the layer list
between named markers, and each must equal a fresh :func:`render_layer_block` rendering of the
shipped contract.

Gate B: an AST scan of the fixture roots (every non-``test_*`` module of the three test trees)
reports any literal collection that enumerates two or more layer directories, except the two
allow-listed independent oracles (:data:`ALLOW_LIST`).

What gate B does not reach:

* A single layer name is never a finding: a fixture that writes one element names a layer and
  duplicates no inventory.
* ``test_*`` modules are out of scope. Several hold the whole layer inventory as a literal on
  purpose: each is a frozen oracle a derivation is compared against, and deriving it would make
  the test ``assert derived == derived``.
* The scope is derived from the tree, not listed here, so a new fixture root is scanned
  without an edit to this file.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

import pytest
from conftest import REPO_ROOT
from model_contract_fixtures import merged_contract
from swcraft_tools.model_registry import ModelRegistry, build_model_registry, production_registry
from synthetic_support import merged_binding, merged_registry

# Gate A: the three shipped instruction copies

#: Indented two spaces because the block continues a Markdown list item.
BLOCK_BEGIN = "  <!-- BEGIN GENERATED: model layers -->"
BLOCK_END = "  <!-- END GENERATED: model layers -->"

#: The repository's copy, the packaged template's, and the one the VS Code extension writes into
#: a project during onboarding.
INSTRUCTION_COPIES = (
    "examples/basic/.github/copilot-instructions.md",
    "packages/tools/src/swcraft_tools/templates/vehicle-architecture/.github/copilot-instructions.md",
    "vscode_extension/fixtures/agents/copilot-instructions.md",
)


def render_layer_block(registry: ModelRegistry | None = None) -> str:
    """Render the generated layer list from the shipped contract.

    One line per declared layer, in contract order: its storage path pattern and whether
    ``swcraft init`` creates the directory. *registry* exists so a probe can hand the renderer
    an optional layer; no shipped layer is optional.
    """
    registry = registry or production_registry()
    lines = [BLOCK_BEGIN]
    for facts in registry.layers:
        record = facts.storage
        path = f"<model_root>/{record.directory}/"
        if record.is_recursive:
            path += f"<{record.nesting}>/"
        path += f"<{record.filename_from}>{record.file_suffix}"
        # The note names no command; template invocations are held to the bare command by
        # `test_template_contents.py::TestIndependenceFromTheSwcraftCheckout`.
        note = (
            "**optional — not scaffolded; create the directory only when adding an element of it**"
            if facts.optional
            else "scaffolded when the project is created"
        )
        lines.append(f"  - `{path}` — {note}")
    lines.append(BLOCK_END)
    return "\n".join(lines)


def registry_with_an_optional_layer() -> ModelRegistry:
    """Return the merged registry (the shipped layers plus the synthetic one) with its synthetic layer flipped optional.

    Flipped on the fixture's deep copy, so nothing on disk and no other consumer changes.
    """
    contract = merged_contract()
    for layer in contract["layers"]:
        if layer["name"] == "synthetic":
            layer["optional"] = True
    return build_model_registry(contract=contract, binding=merged_binding())


def _block_of(text: str) -> str:
    start = text.index(BLOCK_BEGIN)
    end = text.index(BLOCK_END) + len(BLOCK_END)
    return text[start:end]


@pytest.mark.parametrize("relative", INSTRUCTION_COPIES)
def test_every_instruction_copy_carries_the_generated_layer_block(relative: str) -> None:
    """Each copy's generated block equals a fresh rendering of the contract.

    Equality with one common value also pins that the copies agree with each other. The subject
    is the block, not the file: the template copy's surrounding prose differs on purpose.
    """
    text = (REPO_ROOT / relative).read_text(encoding="utf-8")
    assert BLOCK_BEGIN in text and BLOCK_END in text, f"{relative} lost the generated layer block"
    assert _block_of(text) == render_layer_block(), (
        f"{relative} is stale; regenerate the block between the markers from `render_layer_block()`"
    )


def test_no_layer_is_optional_and_the_renderer_would_still_say_so() -> None:
    """No shipped layer is rendered optional, and the renderer still says *optional* when one is.

    The second half guards against a renderer whose ``optional`` branch was deleted.
    """
    registry = production_registry()
    assert not [facts for facts in registry.layers if facts.optional], (
        "a layer is optional again; this assertion and the hand-written sentence "
        "above the block in all three instruction copies both have to move"
    )

    rendered = render_layer_block().splitlines()
    for facts in registry.layers:
        line = next(entry for entry in rendered if f"/{facts.storage.directory}/" in entry)
        assert "**optional" not in line, f"{facts.name} is rendered as optional and the contract says it is not"
        assert line.endswith("— scaffolded when the project is created"), (
            f"{facts.name} is rendered with the wrong scaffolding note"
        )

    probe = registry_with_an_optional_layer()
    optional = [facts for facts in probe.layers if facts.optional]
    assert [facts.storage.directory for facts in optional] == ["synthetic"]
    probe_lines = render_layer_block(probe).splitlines()
    for facts in probe.layers:
        line = next(entry for entry in probe_lines if f"/{facts.storage.directory}/" in entry)
        assert ("**optional" in line) is facts.optional, f"{facts.name} is rendered with the wrong scaffolding note"


def test_the_layer_vocabulary_is_the_fifteen_shipped_directories() -> None:
    """Fifteen directories, ``hardware`` among them and ``resources`` and ``systems`` gone.

    The count is written out, not derived: a derived count would compare the registry with itself.
    """
    directories = layer_directories()
    assert len(directories) == 15
    assert {"data_types/units", "data_types/compu_methods", "data_types/constraints"} <= directories
    assert "data_types/mapping_sets" in directories
    assert "data_types/base" in directories
    assert "data_types/application" in directories
    assert "data_types/implementation" in directories
    assert "data_types" not in directories
    assert "mapping_sets" not in directories
    assert "hardware" in directories
    assert "resources" not in directories
    assert "systems" not in directories


def test_the_generated_block_carries_no_layer_the_contract_does_not_declare() -> None:
    """A positive control: the renderer emits one line per layer and no extra."""
    rendered = render_layer_block().splitlines()
    assert len(rendered) == len(production_registry().layers) + 2  # the two markers


# Gate B: the scanned scope

TEST_TREES = ("packages/tools/tests", "packages/mcp/tests", "packages/agent/tests")


def _fixture_roots() -> tuple[str, ...]:
    """Return every non-``test_*`` module of the three test trees.

    These are the modules whose contents reach other suites, so a layer table in one propagates.
    Does not reach a layer literal inside a ``test_*`` module: hundreds of them name a layer
    legitimately, and a per-suite literal propagates nowhere.
    """
    found = []
    for tree in TEST_TREES:
        for path in sorted((REPO_ROOT / tree).rglob("*.py"), key=lambda path: path.as_posix()):
            if not path.name.startswith("test_"):
                found.append(path.relative_to(REPO_ROOT).as_posix())
    return tuple(found)


def scanned_modules() -> tuple[str, ...]:
    """Return the fixture roots, the scope derived from open files alone.

    A closed half re-runs the gate over this scope widened by the migration inventory's test rows.
    """
    return tuple(sorted(set(_fixture_roots())))


def layer_directories(registry: ModelRegistry | None = None) -> frozenset[str]:
    """Return every declared layer's storage directory, the vocabulary a finding is about.

    Defaults to the shipped registry, because gate B scans the repository; *registry* lets a
    probe add a layer.
    """
    registry = registry or production_registry()
    return frozenset(registry.layer(name).storage.directory for name in registry.layer_names)


@dataclass(frozen=True)
class Finding:
    module: str
    line: int
    symbol: str
    layers: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.module}:{self.line}: {self.symbol} enumerates {', '.join(self.layers)}"


@dataclass(frozen=True)
class Exemption:
    """One allow-listed layer table, keyed by its symbol, never a line number (lines drift)."""

    module: str
    symbol: str
    reason: str


ALLOW_LIST: tuple[Exemption, ...] = (
    Exemption(
        module="packages/tools/tests/support/model_contract_fixtures.py",
        symbol="ARXML_BOUND_LAYER_ORDER",
        reason=(
            "the identifier rule's independent oracle, and the same argument the entry below "
            "makes. `emitted_identifier_violations` and `check_identifier_conformance` both derive "
            "their scope from `layerDecl.arxmlBound`, so an expectation derived from that flag "
            "would be the contract compared with itself and could not tell a layer that silently "
            "stopped being ARXML-bound from one that was never meant to be. It sits in a fixture "
            "root rather than in one suite deliberately: three test modules pinned the same four "
            "names apiece once, and one independent oracle is "
            "the whole of the argument for writing them out at all — three are two copies."
        ),
    ),
    Exemption(
        module="packages/tools/tests/support/editing_vector_fixtures.py",
        symbol="LAYER_DIRECTORIES",
        reason=(
            "the acceptance vectors' independent oracle. This harness builds the model each of the "
            "protocol vectors is written against, and its own docstring states the reason the table "
            "is harness-local: a fixture builder that read the registry could not tell a broken "
            "registry from a broken fixture. Deriving it would make the vectors agree with the "
            "kernel by construction, which is the one thing they exist not to do."
        ),
    ),
)

_COLLECTION_NODES = (ast.Tuple, ast.List, ast.Set, ast.Dict)


def _elements(node: ast.expr) -> list[ast.expr]:
    return list(node.keys) if isinstance(node, ast.Dict) else list(node.elts)  # type: ignore[arg-type]


def _enumerated_layers(node: ast.expr, directories: frozenset[str]) -> tuple[str, ...]:
    """Return the layer directories a collection literal names *directly*.

    Its string elements, and those of a nested collection (a table's layer column); not a subtree
    walk, which would report a list of two element paths as a layer table.
    """
    names: set[str] = set()
    for element in _elements(node):
        if isinstance(element, ast.Constant) and isinstance(element.value, str) and element.value in directories:
            names.add(element.value)
        elif isinstance(element, _COLLECTION_NODES):
            for inner in _elements(element):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str) and inner.value in directories:
                    names.add(inner.value)
    return tuple(sorted(names))


def _symbol_for(tree: ast.Module, line: int) -> str:
    """The module-level name or definition the finding sits under."""
    for node in tree.body:
        end = getattr(node, "end_lineno", node.lineno)
        if not node.lineno <= line <= end:
            continue
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            return node.targets[0].id
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            return node.target.id
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            return node.name
    return "<module>"


def enumeration_findings(module: str, source: str, directories: frozenset[str]) -> list[Finding]:
    """Every outermost collection literal in *source* that enumerates two or more layers."""
    tree = ast.parse(source)
    findings: list[Finding] = []

    def walk(node: ast.AST, inside_collection: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _COLLECTION_NODES) and not inside_collection:
                layers = _enumerated_layers(child, directories)
                if len(layers) >= 2:
                    findings.append(Finding(module, child.lineno, _symbol_for(tree, child.lineno), layers))
                walk(child, True)
            else:
                walk(child, inside_collection)

    walk(tree, False)
    return findings


def run_gate_b(allow_list: tuple[Exemption, ...] = ALLOW_LIST) -> list[Finding]:
    allowed = {(entry.module, entry.symbol) for entry in allow_list}
    findings: list[Finding] = []
    for module in scanned_modules():
        source = (REPO_ROOT / module).read_text(encoding="utf-8")
        for finding in enumeration_findings(module, source, layer_directories()):
            if (finding.module, finding.symbol) not in allowed:
                findings.append(finding)
    return findings


# Gate B: the assertions


def test_no_scanned_module_enumerates_the_layer_directories() -> None:
    """The gate itself."""
    findings = run_gate_b()
    assert findings == [], "a layer table is back in a fixture root:\n" + "\n".join(str(f) for f in findings)


def test_the_allow_list_is_exactly_the_two_named_entries() -> None:
    """Two entries, each with a reason; adding another is a decision, not a migration step.

    Both are independent oracles: tables whose value is that they are not derived from what
    the test compares against.
    """
    assert len(ALLOW_LIST) == 2
    assert {entry.symbol for entry in ALLOW_LIST} == {
        "ARXML_BOUND_LAYER_ORDER",
        "LAYER_DIRECTORIES",
    }
    for entry in ALLOW_LIST:
        assert len(entry.reason) > 80, f"{entry.symbol} is allow-listed without a justification"


@pytest.mark.parametrize("entry", ALLOW_LIST, ids=lambda e: e.symbol)
def test_every_exemption_still_matches_a_real_layer_table(entry: Exemption) -> None:
    """An exemption that stopped matching is a permanently satisfied exemption."""
    assert entry.module in scanned_modules(), f"{entry.module} is not in the scanned scope"
    source = (REPO_ROOT / entry.module).read_text(encoding="utf-8")
    matched = [
        finding
        for finding in enumeration_findings(entry.module, source, layer_directories())
        if finding.symbol == entry.symbol
    ]
    assert len(matched) == 1, f"{entry.module}:{entry.symbol} no longer names a layer table"


@pytest.mark.parametrize("entry", ALLOW_LIST, ids=lambda e: e.symbol)
def test_every_exemption_is_exercised(entry: Exemption) -> None:
    """Dropping the exemption must make the gate fail, or it is guarding nothing."""
    without = tuple(other for other in ALLOW_LIST if other != entry)
    assert any(finding.symbol == entry.symbol for finding in run_gate_b(without))


def test_the_scanned_scope_covers_every_fixture_root() -> None:
    """And every ``conftest.py`` of the three test trees, by construction."""
    scanned = set(scanned_modules())
    for tree in TEST_TREES:
        conftest = f"{tree}/conftest.py"
        assert (REPO_ROOT / conftest).is_file(), f"{conftest} is missing"
        assert conftest in scanned


# Gate B: the perturbations, every check shown to bite


def test_a_layer_tuple_injected_into_a_fixture_root_is_reported() -> None:
    source = 'SUBDIRS = ("swc", "interfaces", "data_types/implementation")\n'
    findings = enumeration_findings("fake.py", source, layer_directories())
    assert [str(f) for f in findings] == ["fake.py:1: SUBDIRS enumerates data_types/implementation, interfaces, swc"]


def test_a_layer_keyed_dict_is_reported() -> None:
    source = 'TABLE = {"swc": 1, "hardware": 2}\n'
    findings = enumeration_findings("fake.py", source, layer_directories())
    assert len(findings) == 1
    assert findings[0].layers == ("hardware", "swc")


def test_a_table_of_rows_whose_first_column_is_the_layer_is_reported() -> None:
    """A table of rows whose first column is the layer is reported."""
    source = 'ROWS = [\n    ("swc", "Bad.yaml"),\n    ("interfaces", "Bad.yaml"),\n]\n'
    findings = enumeration_findings("fake.py", source, layer_directories())
    assert len(findings) == 1
    assert findings[0].layers == ("interfaces", "swc")


def test_one_layer_name_on_its_own_is_not_reported() -> None:
    """A fixture that writes one element of one layer names a layer and no table."""
    source = 'def seed(root):\n    write(root / "swc" / "A.yaml", {})\n    write(root / "swc" / "B.yaml", {})\n'
    assert enumeration_findings("fake.py", source, layer_directories()) == []


def test_two_element_paths_in_one_list_are_not_reported() -> None:
    """Two element paths in one list name two layers in passing and are not a layer table."""
    source = 'EXPECTED = [\n    str(root / "swc" / "A.yaml"),\n    str(root / "interfaces" / "B.yaml"),\n]\n'
    assert enumeration_findings("fake.py", source, layer_directories()) == []


def test_a_finding_is_keyed_by_the_enclosing_definition_when_it_is_not_an_assignment() -> None:
    source = 'def scaffold(root):\n    for name in ("swc", "tasks"):\n        (root / name).mkdir()\n'
    findings = enumeration_findings("fake.py", source, layer_directories())
    assert [f.symbol for f in findings] == ["scaffold"]


def test_the_gate_reads_the_shipped_vocabulary_and_not_a_hardcoded_one() -> None:
    """An added layer's directory is in scope the moment a contract declares it.

    Asserted with a probe: one source names one layer under the shipped vocabulary and two under
    a registry with the synthetic layer. A hard-coded frozenset of today's directories answers
    only the first way.
    """
    source = 'PATHS = ["synthetic", "swc"]\n'
    assert enumeration_findings("fake.py", source, layer_directories()) == []

    merged = merged_registry()
    assert "synthetic" in layer_directories(merged)
    findings = enumeration_findings("fake.py", source, layer_directories(merged))
    assert [finding.layers for finding in findings] == [("swc", "synthetic")]
