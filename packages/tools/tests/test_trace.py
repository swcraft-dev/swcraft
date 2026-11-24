"""The trace leaf (contract ``11.0.0``): the link grammar, the element form and the allocation.

``swcraft_tools.trace`` is the one reader of a ``satisfies`` and a
``derived_from`` member. Three spellings of the split exist -- ``trace.parse_link``,
``relations.resolve_versioned_list_values`` and the extension's
``resolveRelation`` -- and they are held to **one table of values** here:
:data:`SPLIT_TABLE`. The two Python spellings are checked on every row; the
TypeScript test in ``vscode_extension/src/navigationCore.test.ts`` repeats the
``id`` column by hand, element rows included, and
``test_the_typescript_copy_of_the_id_column_is_this_tables`` holds that copy equal.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import validator_package
from conftest import AGENT_TASK_BENCH, DIGIT_LIMIT_FLOOR, REPO_ROOT, int_max_str_digits
from swcraft_tools import trace as leaf
from swcraft_tools.diagnostics import field_step, member_step
from swcraft_tools.relations import RelationResolutionError, resolve_versioned_list_values
from swcraft_tools.schema import Port, Runnable, SWComponent

LEAF_SOURCE = Path(leaf.__file__)

#: ``member -> (id the resolver yields or None, version, suffix)``. The ``id``
#: column is what both Python spellings and the extension's strip agree on: the
#: text before the first ``@``, and nothing for a member holding ``:`` (the
#: element form, which is not a member of the relation). The two other columns
#: are ``parse_link``'s alone. ``navigationCore.test.ts`` repeats the id column.
SPLIT_TABLE: tuple[tuple[str, str | None, int | None, str | None], ...] = (
    ("REQ-12@3", "REQ-12", 3, "3"),
    ("REQ-12", "REQ-12", None, None),
    ("REQ-12@0", "REQ-12", None, "0"),
    ("REQ-12@01", "REQ-12", None, "01"),
    ("REQ-12@x", "REQ-12", None, "x"),
    ("REQ-12@3@4", "REQ-12", None, "3@4"),
    ("REQ-12@", "REQ-12", None, ""),
    ("REQ_EXAMPLE_001@12", "REQ_EXAMPLE_001", 12, "12"),
    ("hardware:Core0", None, None, None),
    ("swc:SpeedCtrlSWC", None, None, None),
    ("connectors:X", None, None, None),
)


@pytest.mark.parametrize(
    ("member", "expected_id", "version", "suffix"), SPLIT_TABLE, ids=[row[0] for row in SPLIT_TABLE]
)
def test_the_two_python_spellings_agree_on_every_row(
    member: str, expected_id: str | None, version: int | None, suffix: str | None
) -> None:
    resolved = tuple(resolve_versioned_list_values(SimpleNamespace(satisfies=[member]), "satisfies"))
    assert resolved == (() if expected_id is None else (expected_id,))
    if expected_id is not None:
        parsed = leaf.parse_link(member)
        assert parsed == leaf.ParsedLink(expected_id, version, suffix)


def test_a_version_wider_than_the_int_digit_limit_is_converted_exactly() -> None:
    """``int()`` refuses a decimal string above ``sys.get_int_max_str_digits()``; the split never does.

    The limit is PINNED at :data:`DIGIT_LIMIT_FLOOR` for the case rather than read
    live: under ``PYTHONINTMAXSTRDIGITS=0`` (no limit) the ``int()`` precondition
    would not raise, and the case would fail although the split is right.
    """
    digits = "1" + "0" * DIGIT_LIMIT_FLOOR  # one digit above the pinned limit
    member = f"REQ-12@{digits}"
    with int_max_str_digits(DIGIT_LIMIT_FLOOR):
        with pytest.raises(ValueError, match="limit"):
            int(digits)

        parsed = leaf.parse_link(member)
        resolved = tuple(resolve_versioned_list_values(SimpleNamespace(satisfies=[member]), "satisfies"))
    assert parsed == leaf.ParsedLink("REQ-12", 10**DIGIT_LIMIT_FLOOR, digits)
    assert resolved == ("REQ-12",)


_TS_TEST = Path(__file__).resolve().parents[3] / "vscode_extension" / "src" / "navigationCore.test.ts"


def test_the_typescript_copy_of_the_id_column_is_this_tables() -> None:
    """``navigationCore.test.ts``'s ``SPLIT_TABLE_IDS`` repeats the id column by hand: held equal here."""
    source = _TS_TEST.read_text(encoding="utf-8")
    block = re.search(r"const SPLIT_TABLE_IDS[^=]*=\s*\[(.*?)\n\];", source, re.DOTALL)
    assert block is not None, "SPLIT_TABLE_IDS not found in navigationCore.test.ts"
    rows = re.findall(r'\[\s*"([^"]*)",\s*(?:"([^"]*)"|(undefined))\s*\]', block.group(1))
    typescript = [(member, None if undefined else expected) for member, expected, undefined in rows]
    assert typescript == [(member, expected_id) for member, expected_id, *_ in SPLIT_TABLE]


_TS_NAVIGATION = Path(__file__).resolve().parents[3] / "vscode_extension" / "src" / "navigationCore.ts"
_REFERENCES = Path(__file__).resolve().parents[3] / "vscode_extension" / "schemas" / "references.json"


def _walk_outside_loops(node: ast.AST) -> list[ast.AST]:
    """*node* and its descendants, stopping at a nested ``for`` loop."""
    if isinstance(node, ast.For):
        return []
    found = [node]
    for child in ast.iter_child_nodes(node):
        found.extend(_walk_outside_loops(child))
    return found


def test_the_typescript_copy_of_the_linkable_layers_is_this_leafs() -> None:
    """``navigationCore.ts``'s ``LINKABLE_LAYERS`` repeats the layer names by hand: held equal here.

    Either side changing alone fails: a sixth linkable layer here, or one dropped
    there, would make Go to Definition on ``<token>:<name>`` resolve nothing.
    """
    source = _TS_NAVIGATION.read_text(encoding="utf-8")
    block = re.search(r"const LINKABLE_LAYERS: ReadonlySet<string> = new Set\(\[(.*?)\]\);", source, re.DOTALL)
    assert block is not None, "LINKABLE_LAYERS not found in navigationCore.ts"
    typescript = re.findall(r'"([^"]*)"', block.group(1))
    assert sorted(typescript) == sorted(leaf.LINKABLE_LAYERS.values())
    assert len(typescript) == len(set(typescript))


def test_the_typescript_element_form_relation_is_the_field_the_validator_reads_the_element_form_in() -> None:
    """``navigationCore.ts``'s ``ELEMENT_FORM_RELATION`` names the one relation whose members take the element form.

    Held from both sides: the identifier must be a ``versioned-list-values``
    relation of the generated ``references.json`` (a renamed contract identifier
    regenerates that file), and the field its one path ends at must be exactly
    the set of fields the validator reads through ``parse_element_reference``
    (every ``for ... in <element>.<field>`` loop whose body calls it).
    """
    source = _TS_NAVIGATION.read_text(encoding="utf-8")
    found = re.search(r'const ELEMENT_FORM_RELATION = "([^"]*)";', source)
    assert found is not None, "ELEMENT_FORM_RELATION not found in navigationCore.ts"
    relations = [
        edge
        for edge in json.loads(_REFERENCES.read_text(encoding="utf-8"))["relations"]
        if edge["field"] == found.group(1)
    ]
    assert relations, f"{found.group(1)} is no relation of references.json"
    assert {edge["resolution"] for edge in relations} == {"versioned-list-values"}
    assert {edge["sourceLayer"] for edge in relations} == {"requirements"}
    published = {entry["path"][-1] for edge in relations for entry in edge["paths"]}

    read_in: set[str] = set()
    loops = (
        node for path in validator_package.paths() for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
    )
    for loop in loops:
        if not (isinstance(loop, ast.For) and isinstance(loop.iter, ast.Attribute)):
            continue
        # The loop's own body, never a nested loop's: the innermost loop names the field.
        own = [node for statement in loop.body for node in _walk_outside_loops(statement)]
        if any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "parse_element_reference"
            for node in own
        ):
            read_in.add(loop.iter.attr)
    assert read_in, "the validator reads no field through parse_element_reference"
    assert published == read_in


def test_the_resolver_skips_the_element_form_and_keeps_the_order() -> None:
    element = SimpleNamespace(derived_from=["REQ-1@2", "hardware:Core0", "REQ-2"])
    assert tuple(resolve_versioned_list_values(element, "derived_from")) == ("REQ-1", "REQ-2")


def test_the_resolver_refuses_a_string_and_answers_nothing_for_none() -> None:
    with pytest.raises(RelationResolutionError, match="versioned-list-values"):
        resolve_versioned_list_values(SimpleNamespace(satisfies="REQ-1@1"), "satisfies")
    assert tuple(resolve_versioned_list_values(SimpleNamespace(satisfies=None), "satisfies")) == ()


@pytest.mark.parametrize("suffix", ["1", "9", "10", "123"])
def test_a_version_is_a_positive_integer_without_a_leading_zero(suffix: str) -> None:
    assert leaf.is_version(suffix)


@pytest.mark.parametrize("suffix", [None, "", "0", "01", "x", "-1", "1.0", " 1", "1 "])
def test_anything_else_is_not_a_version(suffix: str | None) -> None:
    assert not leaf.is_version(suffix)


@pytest.mark.parametrize(
    ("token", "layer"),
    [
        ("swc", "swc"),
        ("interface", "interfaces"),
        ("composition", "compositions"),
        ("task", "tasks"),
        ("hardware", "hardware"),
    ],
)
def test_each_linkable_token_names_its_layer(token: str, layer: str) -> None:
    assert leaf.parse_element_reference(f"{token}:Element") == (layer, "Element")


@pytest.mark.parametrize("value", ["connectors:X", "requirement:REQ-1", "REQ-1@2", "REQ-1", "sequences:S", ":X"])
def test_anything_else_is_not_an_element_reference(value: str) -> None:
    assert leaf.parse_element_reference(value) is None


def test_the_five_tokens_are_the_contract_tokens_of_the_five_linkable_layers() -> None:
    from swcraft_tools.model_registry import production_registry

    registry = production_registry()
    for token, layer in leaf.LINKABLE_LAYERS.items():
        assert registry.layer(layer).token == token
        satisfies = [kind for kind in registry.layer(layer).kinds if kind.get_field("satisfies") is not None]
        assert satisfies, f"{layer} declares no kind carrying satisfies"


def test_a_requirement_id_never_contains_the_two_separators() -> None:
    from swcraft_tools.schema import Requirement

    for separator in ("@", ":"):
        with pytest.raises(ValueError, match=f"must not contain '{separator}'"):
            Requirement(id=f"REQ{separator}1", title="t", description="d", version=1, source="architecture")


def _component() -> SWComponent:
    return SWComponent(
        name="Ctl",
        satisfies=["REQ-B@1", "REQ-A@2"],
        ports=[Port(name="pIn", direction="require", interface="I", satisfies=["REQ-C@1", "REQ-A@3"])],
        runnables=[Runnable(name="Run", satisfies=["REQ-D", "REQ-B@1", "hardware:Core0"])],
    )


def test_the_allocation_is_the_component_then_its_ports_then_its_runnables_each_id_once() -> None:
    assert leaf.component_requirement_ids(_component()) == ("REQ-B", "REQ-A", "REQ-C", "REQ-D")


def test_the_link_sites_spell_the_subject_path_of_each_member() -> None:
    sites = list(leaf.link_sites(_component()))
    assert sites[0] == leaf.LinkSite((field_step("satisfies"), member_step("REQ-B@1")), "REQ-B@1")
    assert sites[2] == leaf.LinkSite(
        (field_step("ports"), member_step("pIn"), field_step("satisfies"), member_step("REQ-C@1")),
        "REQ-C@1",
        ("port", "pIn"),
    )
    assert sites[4].steps[:2] == (field_step("runnables"), member_step("Run"))
    assert [site.owner for site in sites] == [None, None, *[("port", "pIn")] * 2, *[("runnable", "Run")] * 3]
    assert [site.value for site in sites] == [
        "REQ-B@1",
        "REQ-A@2",
        "REQ-C@1",
        "REQ-A@3",
        "REQ-D",
        "REQ-B@1",
        "hardware:Core0",
    ]


def test_the_element_form_is_any_member_holding_a_colon() -> None:
    assert leaf.is_element_form("hardware:Core0")
    assert leaf.is_element_form("nosuch:Thing")
    assert not leaf.is_element_form("REQ-A@2")


def test_the_allocation_by_name_unions_every_file_of_one_name() -> None:
    first = _component()
    second = SWComponent(name=first.name, satisfies=["REQ-E"])
    other = SWComponent(name="Other")
    allocated = leaf.allocated_requirement_ids([first, second, other])
    assert allocated == {first.name: {"REQ-A", "REQ-B", "REQ-C", "REQ-D", "REQ-E"}, "Other": set()}


def _linked_model(tmp_path: Path) -> Path:
    """A model whose five linkable layers each carry a ``satisfies`` member; returns its root."""
    root = tmp_path / "model"
    (root / "swc" / "g").mkdir(parents=True)
    (root / "tasks").mkdir()
    (root / "swc" / "g" / "Ctl.yaml").write_text(
        "name: Ctl\nports:\n- name: pIn\n  direction: require\n  interface: I\n  satisfies:\n  - REQ-1@1\n"
        "satisfies:\n- REQ-2@1\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "tasks" / "Ctl.yaml").write_text(
        "name: Ctl\nperiod: 0.01\npriority: 1\npartition: P\nsatisfies:\n- REQ-1@4\n- hardware:Core0\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "tasks" / "Alpha.yaml").write_text(
        "name: Alpha\nperiod: 0.01\npriority: 1\npartition: P\nsatisfies:\n- REQ-1\n",
        encoding="utf-8",
        newline="\n",
    )
    # The three other linkable layers: an interface, a composition and a hardware element.
    for relative, text in (
        (
            "interfaces/IBus.yaml",
            "name: IBus\nkind: sender_receiver\ndata_elements:\n- name: v\n  implementation_type: u8\n"
            "satisfies:\n- REQ-3@1\n",
        ),
        ("compositions/Top.yaml", "name: Top\ntitle: Top\nsatisfies:\n- REQ-3\n"),
        ("hardware/Core1.yaml", "name: Core1\nkind: core\nthreshold: 0.7\nsatisfies:\n- REQ-3@2\n- REQ-2@1\n"),
    ):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text, encoding="utf-8", newline="\n")
    return root


def test_satisfying_elements_is_the_inverse_by_top_level_name(tmp_path: Path) -> None:
    root = _linked_model(tmp_path)
    # A component and a task of one name are one entry, by bare name.
    assert leaf.satisfying_elements(str(root)) == {
        "REQ-1": ("Alpha", "Ctl"),
        "REQ-2": ("Core1", "Ctl"),
        "REQ-3": ("Core1", "IBus", "Top"),
    }


def _layers(root: str) -> tuple[list, list, list, list, list]:
    """The five linkable layers of *root*, in ``satisfying_elements_of``'s parameter order."""
    return (
        leaf.read_swc_model(root),
        leaf.read_interfaces(root),
        leaf.read_compositions(root),
        leaf.read_tasks(root),
        leaf.read_hardware(root),
    )


def test_satisfying_elements_of_equals_satisfying_elements_on_the_linked_model(tmp_path: Path) -> None:
    root = str(_linked_model(tmp_path))
    assert leaf.satisfying_elements_of(*_layers(root)) == leaf.satisfying_elements(root)


#: Every committed model with an ``swc/`` layer: the example and the test tree's fixtures.
_COMMITTED_MODELS = sorted(
    (
        swc.parent
        for swc in (
            REPO_ROOT / "examples" / "basic" / "model" / "swc",
            *(REPO_ROOT / "packages" / "tools" / "tests" / "fixtures").glob("*/model/swc"),
        )
        if swc.is_dir()
    ),
    key=lambda path: path.as_posix(),
)


@pytest.mark.parametrize("model", _COMMITTED_MODELS, ids=lambda path: path.parent.name)
def test_satisfying_elements_of_equals_satisfying_elements_on_every_committed_model(model: Path) -> None:
    root = str(model)
    assert leaf.satisfying_elements_of(*_layers(root)) == leaf.satisfying_elements(root)


def test_satisfying_elements_reads_the_five_layers_once_each_in_the_contract_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read: list[str] = []
    for name in ("read_swc_model", "read_interfaces", "read_compositions", "read_tasks", "read_hardware"):
        monkeypatch.setattr(leaf, name, lambda root, name=name: read.append(f"{name}:{root}") or [])

    assert leaf.satisfying_elements("model") == {}
    assert read == [
        "read_swc_model:model",
        "read_interfaces:model",
        "read_compositions:model",
        "read_tasks:model",
        "read_hardware:model",
    ]


def test_the_leaf_imports_the_schema_the_step_constructors_and_five_readers_and_nothing_else() -> None:
    """The leaf's ``swcraft_tools`` imports, pinned as a set (standard-library imports are not collected).

    ``schema.Requirement`` is here so the Sphinx-Needs writer reads the confirmation without importing the
    validator.
    """
    tree = ast.parse(LEAF_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("swcraft_tools"):
            imported |= {f"{node.module}.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names if alias.name.startswith("swcraft_tools")}
    assert imported == {
        "swcraft_tools.diagnostics.SubjectStep",
        "swcraft_tools.diagnostics.field_step",
        "swcraft_tools.diagnostics.member_step",
        "swcraft_tools.reader.read_compositions",
        "swcraft_tools.reader.read_hardware",
        "swcraft_tools.reader.read_interfaces",
        "swcraft_tools.reader.read_swc_model",
        "swcraft_tools.reader.read_tasks",
        "swcraft_tools.schema.Composition",
        "swcraft_tools.schema.Core",
        "swcraft_tools.schema.CSInterface",
        "swcraft_tools.schema.ExternalInterface",
        "swcraft_tools.schema.Memory",
        "swcraft_tools.schema.Partition",
        "swcraft_tools.schema.Processor",
        "swcraft_tools.schema.Requirement",
        "swcraft_tools.schema.SRInterface",
        "swcraft_tools.schema.SWComponent",
        "swcraft_tools.schema.Task",
    }


def test_the_leaf_is_named_as_not_generic_core() -> None:
    from test_generic_core_gate import _NOT_GENERIC_CORE

    assert "leaf" in _NOT_GENERIC_CORE["trace.py"]


# ---------------------------------------------------------------------------
# link_sites_of_state and site_kind: the state-based enumeration
# ---------------------------------------------------------------------------


def test_the_enumeration_reads_the_five_layers_in_the_contracts_order() -> None:
    from swcraft_tools.schema import Composition, Core, Processor, SRInterface, Task

    component = SWComponent.model_validate(
        {
            "name": "C",
            "satisfies": ["R@1"],
            "ports": [{"name": "p", "direction": "provide", "interface": "I", "satisfies": ["R"]}],
            "runnables": [{"name": "run", "satisfies": ["R@x"]}],
        }
    )
    composition = Composition.model_validate(
        {"name": "Top", "title": "Top", "members": [{"name": "c", "type": "C"}], "satisfies": ["R@1"]}
    )
    task = Task.model_validate({"name": "T", "period": 0.01, "priority": 1, "partition": "P", "satisfies": ["R@1"]})
    interface = SRInterface.model_validate(
        {
            "name": "I",
            "kind": "sender_receiver",
            "data_elements": [{"name": "d", "implementation_type": "uint16"}],
            "satisfies": ["R@2"],
        }
    )
    processor = Processor.model_validate({"name": "CPU", "kind": "processor", "satisfies": ["R@1"]})
    core = Core.model_validate({"name": "Core0", "kind": "core", "hw_id": 0, "threshold": 0.7, "satisfies": ["R@1"]})
    layers = {
        "hardware": [processor, core],
        "tasks": [task],
        "compositions": [composition],
        "interfaces": [interface],
        "swc": [component],
        "connectors": [],
    }
    records = list(leaf.link_sites_of_state(layers))
    assert [(record.layer, record.element.name, record.site.value) for record in records] == [
        ("swc", "C", "R@1"),
        ("swc", "C", "R"),
        ("swc", "C", "R@x"),
        ("interfaces", "I", "R@2"),
        ("compositions", "Top", "R@1"),
        ("tasks", "T", "R@1"),
        ("hardware", "CPU", "R@1"),
        ("hardware", "Core0", "R@1"),
    ]
    assert [leaf.site_kind(*record) for record in records] == [
        "swc",
        "port",
        "runnable",
        "interface",
        "composition",
        "task",
        "processor",
        "core",
    ]


def test_the_site_kind_layer_is_the_inverse_of_site_kind() -> None:
    """``site_kind_layer`` maps each of the seven site kinds (the hardware one by its four) back to its layer."""
    expected = {
        "swc": "swc",
        "port": "swc",
        "runnable": "swc",
        "interface": "interfaces",
        "composition": "compositions",
        "task": "tasks",
        "processor": "hardware",
        "core": "hardware",
        "memory": "hardware",
        "partition": "hardware",
    }
    assert {kind: leaf.site_kind_layer(kind) for kind in expected} == expected
    with pytest.raises(KeyError):
        leaf.site_kind_layer("connector")


def test_a_site_target_names_a_members_component_as_its_owner() -> None:
    """``trace_search.site_target``: the one builder of a link site's address, for the search and the confirmation."""
    from swcraft_tools.model_state import load_model_state
    from swcraft_tools.trace_search import TargetAddress, site_target

    state = load_model_state(AGENT_TASK_BENCH / "model")
    for layer, element, site in leaf.link_sites_of_state(state.layers):
        target = site_target(layer, element, site)
        kind = leaf.site_kind(layer, element, site)
        if site.owner is None:
            assert target == TargetAddress(kind, None, element.name)
        else:
            assert target == TargetAddress(kind, element.name, site.owner[1])
        assert leaf.site_kind_layer(target.kind) == layer


def test_the_searchs_site_kinds_are_the_leafs() -> None:
    """``trace_search._all_link_sites`` spells each site's ``kind`` through :func:`trace.site_kind`, one vocabulary."""
    from swcraft_tools.model_state import load_model_state
    from swcraft_tools.trace_search import _all_link_sites

    state = load_model_state(AGENT_TASK_BENCH / "model")
    kinds = [address.kind for address, _value in _all_link_sites(state)]
    assert kinds == [leaf.site_kind(*record) for record in leaf.link_sites_of_state(state.layers)]
    assert set(kinds) >= {"swc", "port", "runnable", "interface", "task", "processor", "core"}


# ---------------------------------------------------------------------------
# The confirmation reading: stale_link and link_confirmed
# ---------------------------------------------------------------------------

#: ``REQ-A`` at version 2, ``REQ-S`` a stub at version 1, ``REQ-D`` declared by two files.
CONFIRMATION_VERSIONS = {"REQ-A": [2], "REQ-S": [1], "REQ-D": [1, 1]}
CONFIRMATION_STUBS = frozenset({"REQ-S"})


def test_the_leafs_three_definitions_are_served_by_the_leaf_alone() -> None:
    """``stale_link``, ``requirement_versions`` and ``StaleLink`` are the leaf's own, and only the leaf serves them.

    The validator's facade re-exports what the validator defines and nothing it imports, so a
    caller takes these three from ``trace``.
    """
    from swcraft_tools import validator

    for name in ("stale_link", "requirement_versions", "StaleLink"):
        assert hasattr(leaf, name), name
        assert not hasattr(validator, name), name


def test_requirement_versions_keeps_every_files_version_in_reader_order() -> None:
    from swcraft_tools.schema import Requirement

    requirements = [
        Requirement.model_validate(
            {"id": "REQ-A", "title": "A", "description": "A", "source": "architecture", "version": 2}
        ),
        Requirement.model_validate(
            {"id": "REQ-D", "title": "D", "description": "D", "source": "architecture", "version": 3}
        ),
        Requirement.model_validate(
            {"id": "REQ-D", "title": "D", "description": "D", "source": "architecture", "version": 1}
        ),
    ]
    assert leaf.requirement_versions(requirements) == {"REQ-A": [2], "REQ-D": [3, 1]}


def test_a_link_at_the_current_version_of_a_non_stub_is_confirmed() -> None:
    assert leaf.link_confirmed("REQ-A@2", CONFIRMATION_VERSIONS, CONFIRMATION_STUBS) is True
    # A requirement that is a stub elsewhere does not matter: the stub set is read by id.
    assert leaf.link_confirmed("REQ-A@2", CONFIRMATION_VERSIONS, frozenset({"REQ-B"})) is True


def test_a_version_in_many_digits_equal_to_the_current_one_is_confirmed() -> None:
    """``parse_link``'s width rule: a version above ``int()``'s digit limit is still compared exactly."""
    digits = "1" + "0" * DIGIT_LIMIT_FLOOR
    with int_max_str_digits(DIGIT_LIMIT_FLOOR):
        versions = {"REQ-W": [10**DIGIT_LIMIT_FLOOR]}
        assert leaf.link_confirmed(f"REQ-W@{digits}", versions, frozenset()) is True
        assert leaf.link_confirmed(f"REQ-W@{digits[:-1]}1", versions, frozenset()) is False


@pytest.mark.parametrize(
    ("value", "stubs"),
    [
        pytest.param("REQ-A", CONFIRMATION_STUBS, id="bare-id"),
        pytest.param("REQ-A@1", CONFIRMATION_STUBS, id="stale-version"),
        pytest.param("REQ-A@3", CONFIRMATION_STUBS, id="newer-than-current"),
        pytest.param("REQ-S@1", CONFIRMATION_STUBS, id="stub-at-its-current-version"),
        pytest.param("REQ-X@1", CONFIRMATION_STUBS, id="declared-by-no-file"),
        pytest.param("REQ-D@1", CONFIRMATION_STUBS, id="declared-by-two-files"),
        pytest.param("REQ-A@x", CONFIRMATION_STUBS, id="suffix-not-a-version"),
        pytest.param("REQ-A@02", CONFIRMATION_STUBS, id="leading-zero"),
        pytest.param("REQ-A@01", frozenset(), id="leading-zero-one"),
        pytest.param("REQ-A@", CONFIRMATION_STUBS, id="empty-suffix"),
        pytest.param("swc:REQ-A", CONFIRMATION_STUBS, id="element-form"),
    ],
)
def test_every_other_link_is_unconfirmed(value: str, stubs: frozenset[str]) -> None:
    assert leaf.link_confirmed(value, CONFIRMATION_VERSIONS, stubs) is False


def test_a_link_check_model_warns_about_is_never_confirmed() -> None:
    """The readings agree: ``stale_link`` classifies a bare id and a stale ``@N``; ``link_confirmed`` refuses them."""
    for value in ("REQ-A", "REQ-A@1"):
        assert leaf.stale_link(value, CONFIRMATION_VERSIONS) is not None
        assert leaf.link_confirmed(value, CONFIRMATION_VERSIONS, frozenset()) is False
    assert leaf.stale_link("REQ-A@2", CONFIRMATION_VERSIONS) is None
