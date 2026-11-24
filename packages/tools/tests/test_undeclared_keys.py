"""The leaf predicate: which keys of an element file no field declares.

:func:`swcraft_tools.undeclared_keys.undeclared_keys` walks a file's constructed
document against :attr:`~swcraft_tools.model_registry.KindFacts.file_fields` and
returns every key no field of its kind declares, with a locator down to the
mapping that holds it. These tests drive it on texts read through the one YAML
reader, :func:`~swcraft_tools.yaml_load.load_yaml_document`, as ``check-model``
does. The independent oracle -- the committed editor schemas read by
``jsonschema`` -- is the rule's, not this module's.
"""

from __future__ import annotations

import ast
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT
from swcraft_tools import editor_schema
from swcraft_tools.model_registry import KindFacts, ModelRegistry, current_registry
from swcraft_tools.project import manifest_keys
from swcraft_tools.undeclared_keys import UndeclaredKey, undeclared_keys, undeclared_manifest_keys
from swcraft_tools.yaml_load import load_yaml_document

MODULE = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "undeclared_keys.py"


def _keys(text: str, layer: str) -> tuple[UndeclaredKey, ...]:
    return undeclared_keys(text, load_yaml_document(text), layer)


def _named(found: tuple[UndeclaredKey, ...]) -> list[tuple[str, str, tuple[Any, ...]]]:
    return [(item.kind, item.key, item.locator) for item in found]


# ---------------------------------------------------------------------------
# The declared key set
# ---------------------------------------------------------------------------


def test_file_fields_are_the_fields_without_placement_in_contract_order() -> None:
    registry = current_registry()
    for kind in registry.kinds.values():
        assert kind.file_fields == tuple(field for field in kind.fields if not field.placement)
    assert "group" not in {field.name for field in registry.kind("swc").file_fields}
    assert "group" in {field.name for field in registry.kind("swc").fields}


def test_the_editor_schema_renders_exactly_the_file_fields() -> None:
    """The two readers of one set: each rendered kind's properties are its ``file_fields``, in order."""
    registry = current_registry()
    for name in registry.layer_names:
        layer = registry.layer(name)
        document = editor_schema.layer_schema(layer)
        top_level = layer.top_level_kinds
        objects = [document] if len(top_level) == 1 else document["oneOf"]
        for kind, rendered in zip(top_level, objects, strict=True):
            assert list(rendered["properties"]) == [field.name for field in kind.file_fields]
        for member, rendered in document.get("definitions", {}).items():
            assert list(rendered["properties"]) == [field.name for field in registry.kind(member).file_fields]


def test_the_leaf_imports_the_registry_and_the_yaml_reader_alone() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("swcraft_tools")
    }
    imported |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name.startswith("swcraft_tools")
    }
    assert imported == {"swcraft_tools.model_registry", "swcraft_tools.yaml_load"}


# ---------------------------------------------------------------------------
# Success: nothing declared is reported
# ---------------------------------------------------------------------------


def _complete(kind: KindFacts, registry: ModelRegistry, depth: int = 0) -> dict[str, Any]:
    """A mapping holding every ``file_fields`` key of *kind*, nested kinds filled the same way."""
    assert depth < 8, "the contract's kinds nest no deeper than this"
    document: dict[str, Any] = {}
    for field in kind.file_fields:
        if field.enum and len(field.enum) == 1:
            document[field.name] = field.enum[0]
        elif field.element_kind is not None:
            document[field.name] = _complete(registry.kind(field.element_kind), registry, depth + 1)
        elif field.collection is not None and field.collection.member_kinds:
            document[field.name] = [
                _complete(registry.kind(member), registry, depth + 1) for member in field.collection.member_kinds
            ]
        elif field.collection is not None and field.collection.collection_kind == "map":
            document[field.name] = {"anything": "goes"}
        elif field.type == "value":
            document[field.name] = {"any": {"nested": "value"}}
        else:
            document[field.name] = None
    return document


def _top_level_cases() -> list[tuple[str, str]]:
    registry = current_registry()
    return [(name, kind.kind) for name in registry.layer_names for kind in registry.layer(name).top_level_kinds]


@pytest.mark.parametrize(("layer", "kind"), _top_level_cases())
def test_every_declared_key_of_every_kind_passes(layer: str, kind: str) -> None:
    """Every top-level kind, with every member kind and element kind it reaches filled to every key."""
    registry = current_registry()
    document = _complete(registry.kind(kind), registry)
    text = yaml.safe_dump(document, sort_keys=False)
    assert _keys(text, layer) == ()
    # The walk ran: the same document with one key planted at its top reports exactly that key.
    planted = yaml.safe_dump({**document, "zz_planted": 1}, sort_keys=False)
    found = undeclared_keys(planted, load_yaml_document(planted), layer, registry=registry)
    assert [(item.kind, item.key, item.locator) for item in found] == [(kind, "zz_planted", ())]


def test_a_declared_from_passes() -> None:
    text = "name: C1\nfrom:\n  swc: A\n  port: pOut\nto:\n  composition: Inner\n  port: pIn\n"
    assert _keys(text, "connectors") == ()


def test_init_and_memory_contents_are_never_reported() -> None:
    swc = (
        "name: Brake\n"
        "memory:\n  not_a_field: 4\n"
        "ports:\n"
        "  - name: rSpeed\n    direction: require\n    interface: Speed\n"
        "    measures:\n      anything_at_all: some text\n"
        "    com_specs:\n      - init: {whatever: 1, deeper: {x: 2}}\n"
        "variables:\n  - name: v\n    init: {free: form}\n"
    )
    assert _keys(swc, "swc") == ()
    interface = "name: Speed\nkind: sender_receiver\ndata_elements:\n  - name: speed\n    init: {not_a_field: 1}\n"
    assert _keys(interface, "interfaces") == ()


def test_a_union_layer_s_branch_is_chosen_by_its_discriminator() -> None:
    """``operations`` is declared on ``client_server`` and not on ``sender_receiver``."""
    client_server = "name: Cmd\nkind: client_server\noperations:\n  - name: Run\n"
    sender_receiver = "name: Cmd\nkind: sender_receiver\noperations:\n  - name: Run\n"
    assert _keys(client_server, "interfaces") == ()
    assert _named(_keys(sender_receiver, "interfaces")) == [("sender_receiver", "operations", ())]


def test_an_event_is_matched_by_its_type() -> None:
    """``element`` is a data event's key and no operation event's; ``operation`` the other way round."""
    swc = (
        "name: Brake\n"
        "runnables:\n"
        "  - name: Step\n"
        "    events:\n"
        "      - {type: data, port: rIn, element: speed}\n"
        "      - {type: operation, port: pSrv, operation: Run}\n"
        "      - {type: operation, port: pSrv, element: speed}\n"
    )
    assert _named(_keys(swc, "swc")) == [
        ("operation_event", "element", (("field", "runnables"), ("item", 0), ("field", "events"), ("item", 2)))
    ]


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def test_a_misspelt_key_at_the_top_of_a_requirement() -> None:
    text = "id: REQ_1\ntitle: One\nderived___from:\n  - REQ_0\n"
    assert _named(_keys(text, "requirements")) == [("requirement", "derived___from", ())]


def test_a_misspelt_key_at_the_top_of_a_component() -> None:
    assert _named(_keys("name: Brake\ndescripton: typo\n", "swc")) == [("swc", "descripton", ())]


def test_a_key_in_a_port() -> None:
    text = "name: Brake\nports:\n  - name: rA\n    direction: require\n    intrface: A\n"
    assert _named(_keys(text, "swc")) == [("port", "intrface", (("field", "ports"), ("item", 0)))]


def test_a_key_in_a_runnable() -> None:
    text = "name: Brake\nrunnables:\n  - name: Step\n  - name: Init\n    wcet_budget: 3\n"
    assert _named(_keys(text, "swc")) == [("runnable", "wcet_budget", (("field", "runnables"), ("item", 1)))]


def test_a_key_in_a_data_element() -> None:
    text = "name: Speed\nkind: sender_receiver\ndata_elements:\n  - name: speed\n    aplication_type: Kph\n"
    assert _named(_keys(text, "interfaces")) == [
        ("data_element", "aplication_type", (("field", "data_elements"), ("item", 0)))
    ]


def test_a_key_in_a_from() -> None:
    text = "name: C1\nfrom:\n  swc: A\n  prt: pOut\nto:\n  swc: B\n  port: pIn\n"
    assert _named(_keys(text, "connectors")) == [("connector_endpoint", "prt", (("field", "from"),))]


def test_a_key_in_an_event() -> None:
    text = "name: Brake\nrunnables:\n  - name: Step\n    events:\n      - type: timing\n        perod: 10\n"
    assert _named(_keys(text, "swc")) == [
        ("timing_event", "perod", (("field", "runnables"), ("item", 0), ("field", "events"), ("item", 0)))
    ]


def test_a_key_in_a_disabled_mode_of_an_event() -> None:
    text = (
        "name: Brake\nrunnables:\n  - name: Step\n    events:\n      - type: timing\n        period: 10\n"
        "        disabled_modes:\n          - {port: rMode, mode_group_ref: G, mode: OFF}\n"
    )
    assert _named(_keys(text, "swc")) == [
        (
            "disabled_mode",
            "mode",
            (
                ("field", "runnables"),
                ("item", 0),
                ("field", "events"),
                ("item", 0),
                ("field", "disabled_modes"),
                ("item", 0),
            ),
        )
    ]


def test_a_key_in_a_conversion_is_found_although_the_closed_kind_refuses_it_at_load() -> None:
    """A predicate-only case: ``check-model`` never asks, because the loader refuses the file first."""
    text = "name: Kph\nkind: application_type\nconversion:\n  factor: 2\n  ofset: 1\n"
    assert _named(_keys(text, "application_types")) == [("conversion", "ofset", (("field", "conversion"),))]


def test_two_keys_in_one_file_are_two_results_in_document_order() -> None:
    text = (
        "name: Brake\n"
        "zz_first: 1\n"
        "ports:\n  - name: rA\n    direction: require\n    interface: A\n    aa_second: 2\n"
        "bb_third: 3\n"
    )
    assert _named(_keys(text, "swc")) == [
        ("swc", "zz_first", ()),
        ("port", "aa_second", (("field", "ports"), ("item", 0))),
        ("swc", "bb_third", ()),
    ]


def test_group_in_a_component_file_is_reported() -> None:
    """``group`` is a placement field: the directory says it, and the file has no key for it."""
    assert _named(_keys("name: Brake\ngroup: chassis\n", "swc")) == [("swc", "group", ())]


# ---------------------------------------------------------------------------
# Spelling
# ---------------------------------------------------------------------------


def test_a_yes_key_is_named_yes() -> None:
    assert _named(_keys("name: Brake\nyes: 1\n", "swc")) == [("swc", "yes", ())]


def test_a_numeric_key_is_named_by_its_text() -> None:
    assert _named(_keys("name: Brake\n1: x\n", "swc")) == [("swc", "1", ())]


def test_colliding_boolean_keys_are_three_results() -> None:
    """``yes``, ``on`` and ``true`` construct into one ``True`` key; the source holds three."""
    text = "name: Brake\nyes: 1\non: 2\ntrue: 3\n"
    assert set(load_yaml_document(text)) == {"name", True}
    assert _named(_keys(text, "swc")) == [("swc", "yes", ()), ("swc", "on", ()), ("swc", "true", ())]


def test_a_non_string_key_deep_in_the_file_re_walks_the_whole_file_with_its_locators() -> None:
    text = "name: Brake\nstray: 0\nports:\n  - name: rA\n    direction: require\n    off: 1\n"
    assert _named(_keys(text, "swc")) == [
        ("swc", "stray", ()),
        ("port", "off", (("field", "ports"), ("item", 0))),
    ]


def test_an_empty_key_gives_an_empty_name_at_its_mapping() -> None:
    text = 'name: Brake\nports:\n  - name: rA\n    "": 1\n'
    assert _named(_keys(text, "swc")) == [("port", "", (("field", "ports"), ("item", 0)))]


def test_a_merged_discriminator_chooses_the_kind_in_the_source_node_walk() -> None:
    """``<<`` is resolved as the loader resolves it: a merged ``kind`` names the branch, and no key is skipped.

    Guards the defect where ``yes:`` sent the walk to the source nodes and a ``kind`` reached only
    through ``<<`` left the top kind unchosen and the whole file -- ``data_elemnts`` with it -- unreported.
    """
    text = "<<: {kind: sender_receiver}\nname: Speed\nyes: 1\ndata_elemnts: []\n"
    assert load_yaml_document(text)["kind"] == "sender_receiver"
    assert _named(_keys(text, "interfaces")) == [
        ("sender_receiver", "yes", ()),
        ("sender_receiver", "data_elemnts", ()),
    ]


def test_a_merged_key_is_walked_in_the_order_and_with_the_value_the_loader_constructs() -> None:
    """A merged stray is one result at the mapping it is merged into, and the ``<<`` key is never one.

    The loader's order: of a list of sources the earlier wins, and the mapping's own key
    wins over a merged one; the key keeps the place it was first merged at. So ``zz``
    is one result before ``gone``, and the own ``direction`` overrides the merged one.
    """
    text = (
        "name: Brake\nyes: 1\n"
        "ports:\n"
        "  - <<: [{zz: 1, direction: provide}, {zz: 2, gone: 3}]\n"
        "    name: rA\n"
        "    direction: require\n"
    )
    port = load_yaml_document(text)["ports"][0]
    assert list(port) == ["zz", "gone", "direction", "name"]
    assert (port["zz"], port["direction"]) == (1, "require")
    assert _named(_keys(text, "swc")) == [
        ("swc", "yes", ()),
        ("port", "zz", (("field", "ports"), ("item", 0))),
        ("port", "gone", (("field", "ports"), ("item", 0))),
    ]


def test_a_merged_key_in_a_file_of_string_keys_is_resolved_by_construction() -> None:
    text = "<<: {kind: sender_receiver, stray: 1}\nname: Speed\n"
    assert _named(_keys(text, "interfaces")) == [("sender_receiver", "stray", ())]


def test_a_merge_value_the_loader_refuses_is_a_key_named_merge_and_the_file_is_still_walked() -> None:
    """No shape of ``<<`` makes the source-node walk skip a file: an unmergeable value stays a key."""
    text = "name: Brake\nyes: 1\n<<: 3\nstray: 0\n"
    document = {"name": "Brake", True: 1, "stray": 0}
    assert _named(undeclared_keys(text, document, "swc")) == [
        ("swc", "yes", ()),
        ("swc", "<<", ()),
        ("swc", "stray", ()),
    ]


def test_a_duplicated_string_key_is_one_result() -> None:
    """A stated limit: construction collapses it, and the editor's YAML server reports the duplicate."""
    assert _named(_keys("name: Brake\nstray: 1\nstray: 2\n", "swc")) == [("swc", "stray", ())]


# ---------------------------------------------------------------------------
# Edges: a shape the loader refuses gives nothing and raises nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("layer", "text"),
    [
        ("swc", "- not\n- a mapping\n"),
        ("swc", "just a scalar\n"),
        ("swc", "name: Brake\nports: not a list\n"),
        ("swc", "name: Brake\nports:\n  - just a string\n  - [a, list]\n"),
        ("connectors", "name: C1\nfrom: A.pOut\n"),
        ("interfaces", "name: Speed\nkind: no_such_kind\nstray: 1\n"),
        ("interfaces", "name: Speed\nstray: 1\n"),
        ("swc", "name: Brake\nrunnables:\n  - name: Step\n    events:\n      - {type: nowhere, stray: 1}\n"),
        ("swc", "name: Brake\nrunnables:\n  - name: Step\n    events:\n      - {stray: 1}\n"),
    ],
)
def test_a_shape_the_loader_refuses_gives_no_result(layer: str, text: str) -> None:
    assert _keys(text, layer) == ()


def test_an_empty_document_gives_no_result() -> None:
    assert undeclared_keys("", None, "swc") == ()


def test_the_registry_is_resolved_in_the_body() -> None:
    """The kernel's convention: no default-argument expression captures the production registry."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    public = [node for node in tree.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")]
    assert "undeclared_keys" in {function.name for function in public}
    for function in public:
        defaults = [*function.args.defaults, *(default for default in function.args.kw_defaults if default)]
        assert all(isinstance(default, ast.Constant) and default.value is None for default in defaults), function.name


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


def _manifest(text: str) -> list[tuple[str, str, tuple[Any, ...]]]:
    return _named(undeclared_manifest_keys(text, load_yaml_document(text), manifest_keys()))


def test_every_declared_manifest_key_passes() -> None:
    assert _manifest((REPO_ROOT / "examples" / "basic" / "swcraft.yaml").read_text(encoding="utf-8")) == []
    keys = manifest_keys()
    text = "".join(f"{key}: x\n" for key in sorted(keys[""]) if key != "outputs")
    text += "outputs:\n" + "".join(f"  {key}: x\n" for key in sorted(keys["outputs"]))
    assert _manifest(text) == []


def test_a_misspelt_top_level_manifest_key() -> None:
    assert _manifest("name: p\narxml_verison: 4.3.0\n") == [("manifest", "arxml_verison", ())]


def test_a_stale_key_inside_outputs_names_its_mapping() -> None:
    text = "name: p\noutputs:\n  arxml: dist/model.arxml\n  c4: views/model.c4\n"
    assert _manifest(text) == [("manifest.outputs", "c4", (("field", "outputs"),))]


def test_the_loader_s_own_source_key_is_undeclared_in_a_manifest() -> None:
    assert _manifest("name: p\nsource: env\n") == [("manifest", "source", ())]


def test_a_yes_key_in_a_manifest_is_named_yes_and_collisions_are_separate() -> None:
    assert _manifest("name: p\nyes: 1\n") == [("manifest", "yes", ())]
    assert _manifest("name: p\nyes: 1\non: 2\noutputs:\n  1: x\n") == [
        ("manifest", "yes", ()),
        ("manifest", "on", ()),
        ("manifest.outputs", "1", (("field", "outputs"),)),
    ]


def test_an_empty_manifest_key_gives_an_empty_name() -> None:
    assert _manifest('name: p\n"": 1\n') == [("manifest", "", ())]


@pytest.mark.parametrize("text", ["", "- a\n- list\n", "just a scalar\n", "name: p\noutputs: dist\n"])
def test_a_manifest_shape_the_loader_refuses_gives_no_result(text: str) -> None:
    assert _manifest(text) == []
