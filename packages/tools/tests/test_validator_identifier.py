"""``check_identifier_conformance``: ``check-model`` reports every illegal name.

Both licensed schemas constrain a ``SHORT-NAME``, and every ``*-REF`` path
segment, to ``IDENTIFIER--SIMPLE``: ``[a-zA-Z][a-zA-Z0-9_]*``, at most 128
characters. Unchecked, a name like ``"My SWC"`` passes ``check-model`` and
generates ARXML with XSD errors, which a structural-mode ``validate`` would ship.

This module pins the reporting half (the writer's fail-closed twin is
``test_reserved_name_policies.py``):

* **Every ARXML-bound layer is covered, and only those.** Requirements, tasks,
  sequences and hardware are never emitted as a ``SHORT-NAME``, so an AUTOSAR
  constraint on them would be a rule the standard does not ask for. The scope is
  derived from the contract's ``arxmlBound`` flag, not from a list of layer names.
* **The check covers the emitted form, not the declared one**: every generated
  form, including the event names, whose final shape is known only after the
  retroactive ``_0``/``_1`` counter has run, the forms whose budget belongs to a
  **pair** of declared names, and those an application type generates only for
  the fields it declares.
* **Every offender is reported in one run**, because a legacy model needs its
  whole migration list to plan against.
"""

from __future__ import annotations

import ast
import copy
import re
from pathlib import Path
from typing import Any

import pytest
import swcraft_tools.validator
import validator_package
import yaml
from conftest import model_element_path, model_element_storage_name, write_model_element
from swcraft_tools.contract import load_binding, load_contract
from swcraft_tools.identifier import (
    MAX_IDENTIFIER_LENGTH,
    REASON_LENGTH,
    REASON_PATTERN,
    check_identifier,
)
from swcraft_tools.model_registry import (
    build_model_registry,
    production_registry,
    use_registry,
)
from swcraft_tools.validator import (
    GENERATED_CALL_POINT_PREFIX,
    GENERATED_COMPU_METHOD_SUFFIX,
    GENERATED_INIT_CONSTANT_SUFFIX,
    GENERATED_INIT_EVENT_PREFIX,
    GENERATED_INTERNAL_BEHAVIOR_SUFFIX,
    GENERATED_OPERATION_EVENT_PREFIX,
    GENERATED_READ_ACCESS_PREFIX,
    GENERATED_READ_VARIABLE_PREFIX,
    GENERATED_TIMING_EVENT_PREFIX,
    GENERATED_VARIABLE_ACCESS_PREFIXES,
    GENERATED_WRITE_ACCESS_PREFIX,
    GENERATED_WRITE_VARIABLE_PREFIX,
    RESERVED_COMPU_SUFFIX,
    RESERVED_IMPLEMENTATION_SUFFIX,
    RESERVED_RANGE_SUFFIX,
    RESERVED_UNIT_SUFFIX,
    check_cross_layer_consistency,
    check_identifier_conformance,
    derived_event_names,
    suggest_identifier,
)
from swcraft_tools.validator.events import unique_names
from swcraft_tools.validator.identifiers import _identifier_layer_tokens
from swcraft_tools.writer.arxml_generation import (
    CALL_POINT_PREFIX,
    COMPU_METHOD_SUFFIX,
    COMPU_SUFFIX,
    IMPLEMENTATION_SUFFIX,
    INIT_CONSTANT_SUFFIX,
    INIT_EVENT_PREFIX,
    INTERNAL_BEHAVIOR_SUFFIX,
    OPERATION_EVENT_PREFIX,
    RANGE_SUFFIX,
    READ_ACCESS_PREFIX,
    READ_VARIABLE_PREFIX,
    TIMING_EVENT_PREFIX,
    UNIT_SUFFIX,
    VARIABLE_ACCESS_LIST_PREFIXES,
    WRITE_ACCESS_PREFIX,
    WRITE_VARIABLE_PREFIX,
    _unique_event_names,
    access_point_name,
)

#: A suffix the writer no longer reserves (it invents no ``{name}_impl`` twin);
#: this module still spells names ending in it, which are ordinary names.
IMPL_SUFFIX = "_impl"


#: The eighth generated form's joint budget: ``_`` and ``_Init`` cost six of the
#: 128 characters, so an interface name and one of its data element names get
#: **122 between them**. Derived from the writer's own constant, never restated.
JOINT_BUDGET = MAX_IDENTIFIER_LENGTH - len(INIT_CONSTANT_SUFFIX) - 1

#: The three access-point forms' joint budgets: a port name and the member name
#: it names share the 128 with the prefix and the separating ``_``, so ``R_``/``W_``
#: leave **125** between them and ``Call_`` leaves **122**. Derived from the
#: writer's own constants, never restated.
ACCESS_BUDGET = {
    prefix: MAX_IDENTIFIER_LENGTH - len(prefix) - 1
    for prefix in (READ_ACCESS_PREFIX, WRITE_ACCESS_PREFIX, CALL_POINT_PREFIX)
}

#: Which of a runnable's three lists each access-point form is derived from.
ACCESS_LIST_OF = {
    READ_ACCESS_PREFIX: "reads",
    WRITE_ACCESS_PREFIX: "writes",
    CALL_POINT_PREFIX: "calls",
}

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER_PY = REPO_ROOT / "packages" / "mcp" / "src" / "swcraft_mcp" / "server.py"
TEMPLATE_MOON_YML = (
    REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "templates" / "vehicle-architecture" / "moon.yml"
)

FLOAT32 = {
    "name": "float32",
    "kind": "base_type",
    "size": 32,
    "encoding": "IEEE754",
    "native_declaration": "float",
}

#: A name failing the pattern half in the most ordinary way a human produces one.
BAD_NAME = "My SWC"


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _swc(name: str, ports: list | None = None, runnables: list | None = None) -> dict:
    return {"name": name, "type": "application", "ports": ports or [], "runnables": runnables or []}


def _seed_swc(model_root: Path, swc: dict) -> None:
    _write_yaml(model_root / "swc" / "group" / "swc.yaml", swc)


def _issues(model_root: Path) -> list[str]:
    return check_identifier_conformance(str(model_root)).issues


def _reported(model_root: Path, name: str) -> str:
    """The single issue reporting *name*, or a failure naming what was reported."""
    issues = _issues(model_root)
    matching = [issue for issue in issues if f"'{name}'" in issue]
    assert len(matching) == 1, issues
    return matching[0]


# ---------------------------------------------------------------------------
# The scope of the rule: which layers are ARXML-bound
# ---------------------------------------------------------------------------
#
# The scope is a projection of the contract's `arxmlBound`,
# `_identifier_layer_tokens()`. The ARXML goldens cannot be its oracle: they are
# generated from a valid model, so a scope silently widened or narrowed changes
# no golden. The oracle is the set itself, pinned below as a literal and compared
# with the derived one in BOTH directions.


#: The scope as the hand-written table held it before it was derived from the
#: contract: layer token -> (directory, whether the layer nests). A literal on
#: purpose: recomputing it from the contract would compare the mechanism with
#: itself.
PRE_MIGRATION_IDENTIFIER_LAYER_DIRS: dict[str, tuple[str, bool]] = {
    "swc": ("swc", True),
    "connector": ("connectors", False),
    "interface": ("interfaces", False),
    "data_type": ("data_types", False),
}

#: The ARXML-bound layers declared after the literal above was captured, kept
#: apart so the before-state stays a transcription and not a guess. Each file of
#: them emits one ``SHORT-NAME``: a ``COMPOSITION-SW-COMPONENT-TYPE``,
#: ``DATA-TYPE-MAPPING-SET``, ``SW-BASE-TYPE``, ``APPLICATION-*-DATA-TYPE``,
#: ``IMPLEMENTATION-DATA-TYPE``, ``UNIT``, ``COMPU-METHOD`` or ``DATA-CONSTR``.
IDENTIFIER_LAYER_DIRS_ADDED_SINCE: dict[str, tuple[str, bool]] = {
    "composition": ("compositions", False),
    "mapping_set": ("data_types/mapping_sets", False),
    "base_type": ("data_types/base", False),
    "application_type": ("data_types/application", False),
    "implementation_type": ("data_types/implementation", False),
    "unit": ("data_types/units", False),
    "compu_method": ("data_types/compu_methods", False),
    "data_constraint": ("data_types/constraints", False),
}

#: The before-state's tokens declared no more (``data_types`` was split into the
#: data description layers): removed from today's scope, not from the literal.
IDENTIFIER_LAYER_TOKENS_RETIRED_SINCE: frozenset[str] = frozenset({"data_type"})

#: Today's scope: the before-state less what was retired, plus everything declared since.
IDENTIFIER_LAYER_DIRS = {
    token: entry
    for token, entry in PRE_MIGRATION_IDENTIFIER_LAYER_DIRS.items()
    if token not in IDENTIFIER_LAYER_TOKENS_RETIRED_SINCE
} | IDENTIFIER_LAYER_DIRS_ADDED_SINCE


def _is_a_layer_report(func: ast.expr) -> bool:
    """Whether a call to *func* reports a name under a layer token, its third argument.

    A phase of `check_identifier_conformance` calls the `report.declared` /
    `report.generated` fields of the `_IdentifierReport` it is handed.
    """
    return isinstance(func, ast.Attribute) and func.attr in {"declared", "generated"}


def _contract_with(layer_name: str, **changes: Any) -> dict[str, Any]:
    """The shipped contract with one layer declaration amended."""
    contract = copy.deepcopy(load_contract())
    layer = next(entry for entry in contract["layers"] if entry["name"] == layer_name)
    layer.update(changes)
    return contract


class TestTheIdentifierScopeIsTheContractsArxmlBoundLayers:
    def test_the_derived_set_equals_the_pinned_pre_migration_one(self) -> None:
        """The derived token set equals the pinned one, in both directions.

        `derived <= pinned` passes on a dropped layer and the converse on an added
        one, and no golden from a conforming model shows either. Tokens only: the
        pinned directories are compared with the storage records below.
        """
        derived = _identifier_layer_tokens()
        assert derived == frozenset(IDENTIFIER_LAYER_DIRS)
        assert derived <= frozenset(IDENTIFIER_LAYER_DIRS)
        assert frozenset(IDENTIFIER_LAYER_DIRS) <= derived

    def test_the_pinned_set_is_the_arxml_bound_layers_of_the_contract(self) -> None:
        """The pinned table is the contract's ARXML-bound layers: token <- layer name, directory <- storage."""
        bound = [layer for layer in production_registry().layers if layer.arxml_bound]
        assert {layer.name for layer in bound} == {
            "swc",
            "connectors",
            "interfaces",
            "implementation_types",
            "compositions",
            "mapping_sets",
            "base_types",
            "application_types",
            "units",
            "compu_methods",
            "data_constraints",
        }
        assert {layer.storage.directory: layer.storage.is_recursive for layer in bound} == {
            directory: nests for directory, nests in IDENTIFIER_LAYER_DIRS.values()
        }

    def test_a_layer_that_declares_no_arxml_bound_is_out_of_scope(self) -> None:
        """A layer is in scope only when it declares `arxmlBound`, never by arriving in the contract.

        The absent key's `false` fallback is exercised beside both explicit answers.
        """
        for declaration in ({}, {"arxmlBound": False}):
            contract = copy.deepcopy(load_contract())
            layer = next(entry for entry in contract["layers"] if entry["name"] == "tasks")
            layer.pop("arxmlBound")
            layer.update(declaration)
            registry = build_model_registry(contract=contract, binding=load_binding())
            assert "task" not in _identifier_layer_tokens(registry), declaration

        registry = build_model_registry(contract=_contract_with("tasks", arxmlBound=True), binding=load_binding())
        assert "task" in _identifier_layer_tokens(registry)

    def test_declaring_a_layer_bound_does_not_by_itself_make_it_reported(self, tmp_path: Path) -> None:
        """Declaring `tasks` ARXML-bound widens the projection but reports nothing new.

        The scope is stated twice: `_identifier_layer_tokens()` and the reader
        loops inside `check_identifier_conformance`, which are what is read and
        checked. `test_the_reported_layers_are_exactly_the_derived_ones` holds the
        two equal.
        """
        _write_yaml(tmp_path / "tasks" / "t.yaml", {"name": "Task 10ms", "period": 0.01, "priority": 1})
        assert _issues(tmp_path) == []

        registry = build_model_registry(contract=_contract_with("tasks", arxmlBound=True), binding=load_binding())
        assert "task" in _identifier_layer_tokens(registry)
        with use_registry(registry):
            assert _issues(tmp_path) == []

    def test_the_reported_layers_are_exactly_the_derived_ones(self) -> None:
        """The layer tokens the report calls name, recomputed from the source, equal the derived scope.

        A read layer no longer declared `arxmlBound` would be checked while the
        declared scope says it is not, and a declared layer no call site names is a
        declaration nothing reads; set equality refuses both.
        """
        events = swcraft_tools.validator.events
        defined = validator_package.module_functions(events)
        # The check and every phase it reaches: the module-level functions of the
        # module it calls by name, followed transitively.
        reached = validator_package.reached_functions(events, "check_identifier_conformance")
        reported = {
            node.args[2].value
            for name in reached
            for node in ast.walk(defined[name])
            if isinstance(node, ast.Call)
            and _is_a_layer_report(node.func)
            and len(node.args) > 2
            and isinstance(node.args[2], ast.Constant)
        }
        assert reported, "no _report call site was recognised; the scan has gone stale"
        assert reported == set(_identifier_layer_tokens())

    def test_the_registry_argument_is_resolved_in_the_body(self) -> None:
        """Not as a default expression, which would bind the production registry forever."""
        registry = build_model_registry(contract=_contract_with("tasks", arxmlBound=True), binding=load_binding())
        with use_registry(registry):
            assert "task" in _identifier_layer_tokens()
        assert "task" not in _identifier_layer_tokens()


class TestTheMessageNamesTheDeclaringLayerAndNoFile:
    """One violation in each ARXML-bound layer, and the layer each message names.

    A validator message reaches the editing protocol's response verbatim, so it
    names the element and its layer and never where the element is stored (a path
    would leak the request's private staging directory). The expected layer names
    are literals so the assertion is not the mechanism compared with itself.
    """

    def test_every_layer_names_its_declaring_layer_and_no_file(self, tmp_path: Path) -> None:
        _seed_swc(tmp_path, _swc("My SWC"))
        _write_yaml(
            tmp_path / "connectors" / "c.yaml",
            {"name": "My Connector", "from": {"swc": "A", "port": "p"}, "to": {"swc": "B", "port": "q"}},
        )
        _write_yaml(
            tmp_path / "interfaces" / "i.yaml",
            {
                "name": "I-Sensor",
                "kind": "sender_receiver",
                "data_elements": [{"name": "d", "implementation_type": "float32"}],
            },
        )
        _write_yaml(model_element_path(tmp_path, "t", kind="base_type"), {**FLOAT32, "name": "float 32"})
        # Base types are a layer of their own, so `implementation_types` is
        # seeded by a type it holds.
        _write_yaml(
            model_element_path(tmp_path, "v", kind="value_type"),
            {"name": "speed 1", "kind": "value_type", "base": "float 32"},
        )
        _write_yaml(tmp_path / "compositions" / "r.yaml", {"name": "My Root", "title": "T"})
        _write_yaml(model_element_path(tmp_path, "s", layer="mapping_sets"), {"name": "My Set", "maps": []})
        _write_yaml(
            model_element_path(tmp_path, "a", kind="application_type"),
            {"name": "speed app", "kind": "application_type"},
        )
        _write_yaml(model_element_path(tmp_path, "u", layer="units"), {"name": "km h"})
        _write_yaml(
            model_element_path(tmp_path, "m", layer="compu_methods"),
            {"name": "gear table", "kind": "texttable", "entries": [{"name": "P", "value": 0}]},
        )
        _write_yaml(model_element_path(tmp_path, "l", layer="data_constraints"), {"name": "speed limits"})

        seeded: dict[str, tuple[str, str]] = {
            "My SWC": ("swc", "swc"),
            "My Connector": ("connector", "connectors"),
            "I-Sensor": ("interface", "interfaces"),
            "speed 1": ("implementation_type", "implementation_types"),
            "float 32": ("base_type", "base_types"),
            "My Root": ("composition", "compositions"),
            "My Set": ("mapping_set", "mapping_sets"),
            "speed app": ("application_type", "application_types"),
            "km h": ("unit", "units"),
            "gear table": ("compu_method", "compu_methods"),
            "speed limits": ("data_constraint", "data_constraints"),
        }
        # Derived rather than counted: a new ARXML-bound layer makes this red
        # until it is seeded.
        assert {token for token, _layer in seeded.values()} == set(IDENTIFIER_LAYER_DIRS)

        for name, (_token, layer) in seeded.items():
            issue = _reported(tmp_path, name)
            assert f" in the {layer} layer is not a legal AUTOSAR Identifier" in issue, name
            assert str(tmp_path) not in issue and ".yaml" not in issue, issue

    def test_a_nested_name_names_the_owning_elements_layer(self, tmp_path: Path) -> None:
        """A nested name is declared in its owner's layer, and no file is named for either."""
        _seed_swc(tmp_path, _swc("A", ports=[{"name": "my port", "direction": "provide", "interface": "I"}]))
        issue = _reported(tmp_path, "my port")
        assert " in the swc layer " in issue and str(tmp_path) not in issue and ".yaml" not in issue, issue


# ---------------------------------------------------------------------------
# The two copies that must not drift
# ---------------------------------------------------------------------------


class TestTheGeneratedFormConstantsMatchTheWriter:
    """``validator`` restates the writer's names rather than importing them, because ``writer`` imports it.

    These guards keep the copies equal, beside the ones
    ``test_validator_name_uniqueness.py`` pins.
    """

    @pytest.mark.parametrize(
        ("restated", "original"),
        [
            (GENERATED_COMPU_METHOD_SUFFIX, COMPU_METHOD_SUFFIX),
            (GENERATED_INTERNAL_BEHAVIOR_SUFFIX, INTERNAL_BEHAVIOR_SUFFIX),
            (GENERATED_TIMING_EVENT_PREFIX, TIMING_EVENT_PREFIX),
            (GENERATED_INIT_EVENT_PREFIX, INIT_EVENT_PREFIX),
            # `OIT_{port}_{operation}`.
            (GENERATED_OPERATION_EVENT_PREFIX, OPERATION_EVENT_PREFIX),
            (RESERVED_IMPLEMENTATION_SUFFIX, IMPLEMENTATION_SUFFIX),
            (GENERATED_INIT_CONSTANT_SUFFIX, INIT_CONSTANT_SUFFIX),
            (GENERATED_READ_ACCESS_PREFIX, READ_ACCESS_PREFIX),
            (GENERATED_WRITE_ACCESS_PREFIX, WRITE_ACCESS_PREFIX),
            (GENERATED_CALL_POINT_PREFIX, CALL_POINT_PREFIX),
            # `RV_{variable}` and `WV_{variable}`.
            (GENERATED_READ_VARIABLE_PREFIX, READ_VARIABLE_PREFIX),
            (GENERATED_WRITE_VARIABLE_PREFIX, WRITE_VARIABLE_PREFIX),
            # The `UNIT`, `COMPU-METHOD` and `DATA-CONSTR` an application type's
            # own name derives.
            (RESERVED_UNIT_SUFFIX, UNIT_SUFFIX),
            (RESERVED_COMPU_SUFFIX, COMPU_SUFFIX),
            (RESERVED_RANGE_SUFFIX, RANGE_SUFFIX),
        ],
    )
    def test_the_restated_affix_matches(self, restated: str, original: str) -> None:
        assert restated == original

    def test_the_restated_variable_access_table_matches(self) -> None:
        """The variable access list-to-prefix table matches, not only its two values."""
        assert GENERATED_VARIABLE_ACCESS_PREFIXES == VARIABLE_ACCESS_LIST_PREFIXES


class TestTheRestatedEventCounterMatchesTheWriter:
    """``derived_event_names`` restates ``_unique_event_names``; a corpus holds them equal.

    The counter is retroactive and skips taken indices, so a plausible
    reimplementation is wrong exactly in the corner cases. The corpus is fed
    literally through ``unique_names``: bases derived from a model group each
    runnable's events and never reach the interleaved rows. The per-event prefix
    is pinned separately below.
    """

    _CORPUS = [
        [],
        ["TMT_R"],
        ["TMT_R", "TMT_R"],
        ["TMT_R", "IT_R"],
        ["TMT_R", "TMT_R", "TMT_R"],
        ["TMT_R", "TMT_R_0", "TMT_R"],
        ["TMT_R_0", "TMT_R", "TMT_R"],
        ["TMT_R", "TMT_R_1", "TMT_R", "TMT_R"],
        ["TMT_A", "TMT_B", "TMT_A", "TMT_B", "TMT_A"],
        ["TMT_R_12x", "TMT_R", "TMT_R"],
        # The retroactive rename has to skip an index a *later*-numbered name
        # already holds, in both directions.
        ["TMT_R", "TMT_R_0", "TMT_R_1", "TMT_R"],
        ["IT_R", "IT_R_0", "IT_R"],
        ["TMT_R", "TMT_R_0", "TMT_R", "TMT_R_2", "TMT_R"],
    ]

    @pytest.mark.parametrize("bases", _CORPUS)
    def test_the_two_implementations_agree(self, bases: list[str]) -> None:
        assert unique_names(bases) == _unique_event_names(bases)

    def test_the_corpus_reaches_the_skip_a_taken_index_branch(self) -> None:
        """The corpus reaches ``_free``'s walk past an index another name already holds.

        A restatement returning ``f"{base}_{start}"`` unconditionally fails only here.
        """
        assert unique_names(["TMT_R", "TMT_R_0", "TMT_R"]) == ["TMT_R_1", "TMT_R_0", "TMT_R_2"]

    def test_the_retroactive_rename_is_reproduced(self) -> None:
        """The **first** event is the one renamed, and both sides skip a taken index.

        The property a "suffix the later one" reimplementation gets wrong."""
        assert _unique_event_names(["TMT_R", "TMT_R"]) == ["TMT_R_0", "TMT_R_1"]
        assert _unique_event_names(["TMT_R", "TMT_R_0", "TMT_R"]) == ["TMT_R_1", "TMT_R_0", "TMT_R_2"]

    def test_the_base_name_of_each_event_carries_its_own_prefix(self, tmp_path: Path) -> None:
        """``derived_event_names`` picks the prefix per **event type**, not per runnable.

        The half the corpus cannot cover: it emits one name per ``(runnable, event)`` pair in that order."""
        from swcraft_tools.reader import read_swc_model

        _seed_swc(
            tmp_path,
            _swc(
                "A",
                runnables=[
                    {"name": "Run", "events": [{"type": "timing", "period": 0.01}, {"type": "init"}]},
                    {"name": "Other", "events": [{"type": "init"}]},
                ],
            ),
        )
        emitted = derived_event_names(read_swc_model(str(tmp_path))[0])

        assert emitted == [
            f"{TIMING_EVENT_PREFIX}Run",
            f"{INIT_EVENT_PREFIX}Run",
            f"{INIT_EVENT_PREFIX}Other",
        ]


# ---------------------------------------------------------------------------
# Every ARXML-bound layer
# ---------------------------------------------------------------------------


class TestEveryArxmlBoundLayerIsReported:
    def test_a_swc_name(self, tmp_path: Path) -> None:
        _seed_swc(tmp_path, _swc(BAD_NAME))
        assert "SWC 'My SWC'" in _reported(tmp_path, BAD_NAME)

    def test_a_port_name(self, tmp_path: Path) -> None:
        _seed_swc(tmp_path, _swc("A", ports=[{"name": "my port", "direction": "provide", "interface": "I"}]))
        assert "port 'my port'" in _reported(tmp_path, "my port")

    def test_a_runnable_name(self, tmp_path: Path) -> None:
        _seed_swc(tmp_path, _swc("A", runnables=[{"name": "9run"}]))
        assert "runnable '9run'" in _reported(tmp_path, "9run")

    def test_a_connector_name(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "connectors" / "c.yaml",
            {
                "name": "My Connector",
                "from": {"swc": "A", "port": "p"},
                "to": {"swc": "B", "port": "q"},
            },
        )
        assert "Connector 'My Connector'" in _reported(tmp_path, "My Connector")

    def test_an_interface_name(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "interfaces" / "i.yaml",
            {
                "name": "I-Sensor",
                "kind": "sender_receiver",
                "data_elements": [{"name": "d", "implementation_type": "float32"}],
            },
        )
        assert "Interface 'I-Sensor'" in _reported(tmp_path, "I-Sensor")

    def test_a_data_element_name(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "interfaces" / "i.yaml",
            {
                "name": "ISensor",
                "kind": "sender_receiver",
                "data_elements": [{"name": "my value", "implementation_type": "float32"}],
            },
        )
        assert "data element 'my value'" in _reported(tmp_path, "my value")

    def test_an_operation_name(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "interfaces" / "i.yaml",
            {"name": "ICtl", "kind": "client_server", "operations": [{"name": "do it", "arguments": []}]},
        )
        assert "operation 'do it'" in _reported(tmp_path, "do it")

    def test_an_argument_name(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "interfaces" / "i.yaml",
            {
                "name": "ICtl",
                "kind": "client_server",
                "operations": [
                    {
                        "name": "Do",
                        "arguments": [{"name": "in.value", "direction": "in", "implementation_type": "float32"}],
                    }
                ],
            },
        )
        issue = _reported(tmp_path, "in.value")
        assert "operation 'Do' argument 'in.value'" in issue

    def test_a_data_type_name(self, tmp_path: Path) -> None:
        _write_yaml(model_element_path(tmp_path, "t", kind="base_type"), {**FLOAT32, "name": "float 32"})
        assert "Data type 'float 32'" in _reported(tmp_path, "float 32")

    def test_an_enum_literal_name(self, tmp_path: Path) -> None:
        _write_yaml(
            model_element_path(tmp_path, "e", kind="enum"),
            {"name": "Gear_T", "kind": "enum", "literals": [{"name": "1st", "value": 0}]},
        )
        assert "Enum 'Gear_T' literal '1st'" in _reported(tmp_path, "1st")

    def test_a_record_field_name(self, tmp_path: Path) -> None:
        _write_yaml(model_element_path(tmp_path, "float32", kind="base_type"), FLOAT32)
        _write_yaml(
            model_element_path(tmp_path, "r", kind="record"),
            {"name": "Pose_T", "kind": "record", "fields": [{"name": "x pos", "type": "float32"}]},
        )
        assert "Record 'Pose_T' field 'x pos'" in _reported(tmp_path, "x pos")


class TestEveryOffenderIsReportedInOneRun:
    """A legacy model needs its whole migration list, not the first line of it."""

    def test_all_layers_at_once(self, tmp_path: Path) -> None:
        _seed_swc(
            tmp_path,
            _swc(
                "My SWC",
                ports=[{"name": "my port", "direction": "provide", "interface": "I-Sensor"}],
                runnables=[{"name": "9run"}],
            ),
        )
        _write_yaml(
            tmp_path / "interfaces" / "i.yaml",
            {
                "name": "I-Sensor",
                "kind": "sender_receiver",
                "data_elements": [{"name": "my value", "implementation_type": "t 1"}],
            },
        )
        _write_yaml(model_element_path(tmp_path, "t", kind="base_type"), {**FLOAT32, "name": "t 1"})
        _write_yaml(
            tmp_path / "connectors" / "c.yaml",
            {"name": "My Connector", "from": {"swc": "A", "port": "p"}, "to": {"swc": "B", "port": "q"}},
        )

        issues = _issues(tmp_path)
        for name in ("My SWC", "my port", "9run", "My Connector", "I-Sensor", "my value", "t 1"):
            assert any(f"'{name}'" in issue for issue in issues), (name, issues)
        assert len(issues) == 7, issues

    def test_two_bad_names_in_one_component(self, tmp_path: Path) -> None:
        _seed_swc(
            tmp_path,
            _swc("A", ports=[{"name": "p 1", "direction": "provide", "interface": "I"}], runnables=[{"name": "r 2"}]),
        )
        assert len(_issues(tmp_path)) == 2


class TestTheOutOfScopeLayersAreNotReported:
    """These layers are never emitted as an AUTOSAR ``SHORT-NAME``, so no AUTOSAR constraint applies.

    ``Sequence.name`` keeps its own, deliberately different rule, which allows a
    leading underscore.
    """

    def test_a_requirement_id(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "requirements" / "r.yaml",
            {"id": "REQ-BRAKE-001", "text": "Something", "links": []},
        )
        assert _issues(tmp_path) == []

    def test_a_system_name(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "systems" / "s.yaml", {"name": "Vehicle System", "description": "d"})
        assert _issues(tmp_path) == []

    def test_a_container_name(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "containers" / "c.yaml",
            {"name": "Brake ECU", "system": "Vehicle System", "description": "d"},
        )
        assert _issues(tmp_path) == []

    def test_a_task_name(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "tasks" / "t.yaml",
            {"name": "Task 10ms", "period": 0.01, "priority": 1, "partition": "PARTITION0"},
        )
        assert _issues(tmp_path) == []

    def test_a_sequence_name(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "sequences" / "s.yaml",
            {"name": "_startup", "task": "Task10ms", "steps": []},
        )
        assert _issues(tmp_path) == []
        assert check_identifier("_startup") == REASON_PATTERN  # illegal here, legal in LikeC4

    def test_a_hardware_name(self, tmp_path: Path) -> None:
        """`layer:hardware` declares `arxmlBound: false`, so the scope skips it.

        Exporting the layer and flipping the flag would refuse `shared ram`, which
        is the mechanism working, not a gap.
        """
        _write_yaml(
            tmp_path / "hardware" / "m.yaml",
            {
                "name": "shared ram",
                "kind": "memory",
                "type": "RAM",
                "size": 4096,
                "accessible": ["CORE0"],
                "threshold": 0.7,
            },
        )
        assert _issues(tmp_path) == []


# ---------------------------------------------------------------------------
# The generated forms
# ---------------------------------------------------------------------------


class TestEveryGeneratedFormIsChecked:
    """A declared name inside the limit can produce an emitted one outside it; the message names the emitted form.

    The forms combining two declared names are in
    :class:`TestTheInitConstantIsAJointBudget` and
    :class:`TestTheAccessPointFormsAreJointBudgetsToo`; the application-type forms,
    scoped by a declared field rather than a kind, in
    :class:`TestTheApplicationTypeFormsAreCheckedOnlyWhereTheFieldIsDeclared`. A
    value type and a reference type are charged for nothing.
    """

    def test_a_base_type_has_no_generated_form_to_check(self, tmp_path: Path) -> None:
        """A base type generates no form, so a name that would overflow an ``_impl`` twin is reported for nothing.

        The ``IMPL_SUFFIX`` length keeps the case non-vacuous: the name is over the
        limit once that suffix goes on it.
        """
        name = "P" * (MAX_IDENTIFIER_LENGTH - len(IMPL_SUFFIX) + 1)
        _write_yaml(model_element_path(tmp_path, "t", kind="base_type"), {**FLOAT32, "name": name})
        assert check_identifier(name) is None

        issues = _issues(tmp_path)
        assert not any(f"{name}{IMPL_SUFFIX}'" in issue for issue in issues), issues
        assert issues == [], issues

    def test_the_compu_method_form_of_an_enum(self, tmp_path: Path) -> None:
        name = "E" * (MAX_IDENTIFIER_LENGTH - len(COMPU_METHOD_SUFFIX) + 1)
        _write_yaml(
            model_element_path(tmp_path, "t", kind="enum"),
            {"name": name, "kind": "enum", "literals": [{"name": "A", "value": 0}]},
        )
        assert check_identifier(name) is None
        assert f"{name}{COMPU_METHOD_SUFFIX}" in _reported(tmp_path, name)

    def test_a_reference_type_has_no_generated_form_to_check(self, tmp_path: Path) -> None:
        """A reference type generates no form, so the whole 128 characters are the declared name's.

        A composite member names a declared ``application_type``, so the writer
        invents no view for a reference type.
        """
        name = "R" * MAX_IDENTIFIER_LENGTH
        _write_yaml(model_element_path(tmp_path, "float32", kind="base_type"), FLOAT32)
        _write_yaml(
            model_element_path(tmp_path, "speed", kind="value_type"),
            {"name": "Speed_T", "kind": "value_type", "base": "float32"},
        )
        _write_yaml(
            model_element_path(tmp_path, "t", kind="reference_type"),
            {"name": name, "kind": "reference_type", "base": "Speed_T"},
        )
        assert check_identifier(name) is None
        assert _issues(tmp_path) == []

    def test_a_value_type_has_no_generated_form_to_check(self, tmp_path: Path) -> None:
        """A value type generates no form, so a name at the limit is reported for nothing."""
        name = "V" * MAX_IDENTIFIER_LENGTH
        _write_yaml(model_element_path(tmp_path, "float32", kind="base_type"), FLOAT32)
        _write_yaml(
            model_element_path(tmp_path, "t", kind="value_type"),
            {"name": name, "kind": "value_type", "base": "float32"},
        )
        assert check_identifier(name) is None

        assert _issues(tmp_path) == []

    def test_the_enum_has_its_compu_method_form_and_no_other(self, tmp_path: Path) -> None:
        """An enum's own ``COMPU-METHOD`` is the one name derived from it.

        It has no ``_impl`` twin, no application view and none of the
        application-type forms, which belong to a declared ``application_type``; a
        guard stricter than the writer would refuse names generation accepts.
        """
        name = "E" * (MAX_IDENTIFIER_LENGTH - len(COMPU_METHOD_SUFFIX) + 1)
        _write_yaml(
            model_element_path(tmp_path, "t", kind="enum"),
            {"name": name, "kind": "enum", "literals": [{"name": "A", "value": 0}]},
        )
        issues = _issues(tmp_path)
        assert len(issues) == 1, issues
        assert f"{name}{COMPU_METHOD_SUFFIX}'" in issues[0]

    def test_a_record_and_an_array_have_no_generated_form(self, tmp_path: Path) -> None:
        """A record and an array generate no form, so a name at the very limit is fine.

        Neither kind is named by the kind-scoped data-type rule or can declare a
        field the application-type forms are emitted for. The members name an
        ``application_type``, as a composite member must.
        """
        name = "R" * MAX_IDENTIFIER_LENGTH
        write_model_element(tmp_path, {"name": "Phys_T", "kind": "application_type"})
        _write_yaml(
            model_element_path(tmp_path, "r", kind="record"),
            {"name": name, "kind": "record", "fields": [{"name": "x", "type": "Phys_T"}]},
        )
        _write_yaml(
            model_element_path(tmp_path, "a", kind="array"),
            {"name": f"A{name[1:]}", "kind": "array", "element_type": "Phys_T", "length": 4},
        )
        assert _issues(tmp_path) == []

    def test_the_implementation_form_of_a_component(self, tmp_path: Path) -> None:
        name = "S" * (MAX_IDENTIFIER_LENGTH - len(IMPLEMENTATION_SUFFIX) + 1)
        _seed_swc(tmp_path, _swc(name))
        assert any(f"{name}{IMPLEMENTATION_SUFFIX}" in issue for issue in _issues(tmp_path))

    def test_a_112_character_component_fails_only_on_internal_behavior(self, tmp_path: Path) -> None:
        """``_Implementation`` is legal at 112 characters and ``_InternalBehavior``, 17 characters long, is not."""
        name = "S" * 112
        assert check_identifier(f"{name}{IMPLEMENTATION_SUFFIX}") is None
        assert check_identifier(f"{name}{INTERNAL_BEHAVIOR_SUFFIX}") == REASON_LENGTH

        _seed_swc(tmp_path, _swc(name))
        issue = _reported(tmp_path, name)
        assert f"{name}{INTERNAL_BEHAVIOR_SUFFIX}" in issue
        assert f"{name}{IMPLEMENTATION_SUFFIX}'" not in issue

    def test_the_timing_event_form(self, tmp_path: Path) -> None:
        runnable = "R" * (MAX_IDENTIFIER_LENGTH - len(TIMING_EVENT_PREFIX) + 1)
        _seed_swc(
            tmp_path,
            _swc("A", runnables=[{"name": runnable, "events": [{"type": "timing", "period": 0.01}]}]),
        )
        assert f"{TIMING_EVENT_PREFIX}{runnable}" in _reported(tmp_path, runnable)

    def test_the_init_event_form(self, tmp_path: Path) -> None:
        runnable = "R" * (MAX_IDENTIFIER_LENGTH - len(INIT_EVENT_PREFIX) + 1)
        _seed_swc(tmp_path, _swc("A", runnables=[{"name": runnable, "events": [{"type": "init"}]}]))
        assert f"{INIT_EVENT_PREFIX}{runnable}" in _reported(tmp_path, runnable)

    def test_a_generated_form_is_not_reported_when_the_declared_name_already_is(self, tmp_path: Path) -> None:
        """One root cause, one issue, so the migration list stays readable."""
        _write_yaml(model_element_path(tmp_path, "t", kind="base_type"), {**FLOAT32, "name": "my type"})
        assert len(_issues(tmp_path)) == 1

    def test_an_event_form_is_not_reported_when_the_runnable_name_already_is(self, tmp_path: Path) -> None:
        """A bad runnable name is reported once, not again through its event form.

        Every other bad-runnable test gives the runnable no events, so the skip
        never fires there. The name fails the **pattern** half: ``TMT_9run`` is a
        legal Identifier, so a leading digit would prove nothing.
        """
        _seed_swc(
            tmp_path,
            _swc("A", runnables=[{"name": "my run", "events": [{"type": "timing", "period": 0.01}]}]),
        )
        assert check_identifier(f"{TIMING_EVENT_PREFIX}my run") == REASON_PATTERN

        issues = _issues(tmp_path)
        assert len(issues) == 1, issues
        assert "runnable 'my run'" in issues[0]
        assert TIMING_EVENT_PREFIX not in issues[0]


def _sr_interface(model_root: Path, interface_name: str, element_name: str, init: object | None = None) -> None:
    """One sender-receiver interface with one data element, optionally valued."""
    element: dict[str, Any] = {"name": element_name, "implementation_type": "float32"}
    if init is not None:
        element["init"] = init
    _write_yaml(
        model_root / "interfaces" / "I.yaml",
        {"name": interface_name, "kind": "sender_receiver", "data_elements": [element]},
    )


class TestTheInitConstantIsAJointBudget:
    """The eighth form is the first one derived from **two** declared names.

    ``{Interface}_{Element}_Init`` spends ``_`` and ``_Init`` — six characters —
    so the two declared names get 122 between them, and neither of them alone has
    a budget at all: a 120-character interface name is legal beside a
    two-character element name and illegal beside a three-character one. That is
    why the message names both, states the joint budget and suggests shortening
    the longer of the two rather than "the" declared name.
    """

    def test_a_pair_exactly_on_the_joint_budget_is_accepted(self, tmp_path: Path) -> None:
        interface_name = "I" * 61
        element_name = "E" * 61
        assert len(interface_name) + len(element_name) == JOINT_BUDGET
        _sr_interface(tmp_path, interface_name, element_name)

        assert _issues(tmp_path) == []

    def test_one_character_more_is_reported(self, tmp_path: Path) -> None:
        interface_name = "I" * 62
        element_name = "E" * 61
        emitted = f"{interface_name}_{element_name}{INIT_CONSTANT_SUFFIX}"
        assert check_identifier(interface_name) is None and check_identifier(element_name) is None
        assert check_identifier(emitted) == REASON_LENGTH
        _sr_interface(tmp_path, interface_name, element_name)

        issues = _issues(tmp_path)

        assert len(issues) == 1, issues
        assert emitted in issues[0]
        assert f"({len(emitted)} characters)" in issues[0]
        assert f"'{interface_name}'" in issues[0] and f"'{element_name}'" in issues[0]
        assert f"{JOINT_BUDGET} characters between them" in issues[0]
        assert REASON_LENGTH in issues[0]

    def test_the_longer_of_the_two_names_is_the_one_suggested(self, tmp_path: Path) -> None:
        """The element is shorter here, so the interface is what must give."""
        interface_name = "I" * 100
        element_name = "E" * 30
        _sr_interface(tmp_path, interface_name, element_name)

        issue = _issues(tmp_path)[0]

        assert f"rename '{interface_name}' to at most 92 characters" in issue
        assert f"rename '{element_name}'" not in issue

    def test_a_tie_suggests_the_data_element(self, tmp_path: Path) -> None:
        """A tie suggests the name a designer changes with less blast radius.

        An interface name is referenced by every port that names it, a data element name by the com-specs of those ports
        alone."""
        interface_name = "I" * 62
        element_name = "E" * 62
        _sr_interface(tmp_path, interface_name, element_name)

        issue = _issues(tmp_path)[0]

        assert f"rename '{element_name}' to at most 60 characters" in issue

    def test_the_suggestion_arithmetic_is_clamped(self, tmp_path: Path) -> None:
        """The suggestion arithmetic is clamped.

        Two conforming 125-character names overflow by 128 — more than either name is long — so an unclamped ``len(name)
        - overflow`` is **negative** and ``suggest_identifier``'s ``cleaned[:max_length]`` answers a suffix-trimmed name
        still far over the limit instead of raising."""
        interface_name = "I" * 125
        element_name = "E" * 125
        assert check_identifier(interface_name) is None and check_identifier(element_name) is None
        _sr_interface(tmp_path, interface_name, element_name)

        issue = _issues(tmp_path)[0]

        suggested = re.search(r"for example to '([^']*)'", issue)
        assert suggested is not None, issue
        assert check_identifier(suggested.group(1)) is None, issue
        assert "at most -" not in issue

    def test_the_pair_is_checked_whether_or_not_the_element_carries_a_value(self, tmp_path: Path) -> None:
        """The pair is checked whether or not the element carries a value, as ``{enum}_CompuMethod`` is.

        A conditional rule would let adding the first ``init`` to one interface fail
        generation on a file nobody touched. The application-type forms are
        conditional only because the writer emits nothing for an undeclared field.
        """
        interface_name = "I" * 62
        element_name = "E" * 61
        _sr_interface(tmp_path, interface_name, element_name, init=1.5)
        with_value = _issues(tmp_path)

        _sr_interface(tmp_path, interface_name, element_name)
        without_value = _issues(tmp_path)

        assert len(with_value) == 1 and with_value == without_value

    def test_a_client_server_interface_has_no_pair_at_all(self, tmp_path: Path) -> None:
        """Kind scope: no constant is derived from an operation or an argument.

        So a client-server name at the very limit is fine."""
        name = "C" * MAX_IDENTIFIER_LENGTH
        _write_yaml(
            tmp_path / "interfaces" / "I.yaml",
            {"name": name, "kind": "client_server", "operations": [{"name": "Do", "arguments": []}]},
        )

        assert _issues(tmp_path) == []

    def test_the_pair_form_is_not_checked_when_a_declared_name_is_already_reported(self, tmp_path: Path) -> None:
        """One root cause, one issue — ``check_identifier_conformance``'s own rule.

        Both halves: a bad interface name and a bad element name."""
        _sr_interface(tmp_path, "I" * 62, "my value")
        assert len(_issues(tmp_path)) == 1

        _sr_interface(tmp_path, "my interface", "E" * 61)
        assert len(_issues(tmp_path)) == 1

    def test_the_finding_is_the_generated_rule_on_the_interfaces_layer(self, tmp_path: Path) -> None:
        """The finding is `rule:identifier.generated`, whose `appliesTo` names `layer:interfaces` for it."""
        _sr_interface(tmp_path, "I" * 62, "E" * 61)

        records = check_identifier_conformance(str(tmp_path)).records

        assert [record.rule for record in records] == ["identifier.generated"]
        assert [(subject.layer, subject.element) for subject in records[0].subjects] == [("interfaces", "I" * 62)]


def _access_swc(
    model_root: Path,
    port_name: str,
    member_name: str,
    *,
    list_name: str = "reads",
    declare_port: bool = True,
) -> None:
    """One component whose single runnable declares one access entry.

    The port and the interface are declared too, so ``check_runnable_access``
    accepts the model, though the identifier report depends on neither. A list
    entry itself makes the generated name exist, which *declare_port* witnesses.
    """
    kind = "client_server" if list_name == "calls" else "sender_receiver"
    members: dict[str, Any] = (
        {"operations": [{"name": member_name, "arguments": []}]}
        if list_name == "calls"
        else {"data_elements": [{"name": member_name, "implementation_type": "float32"}]}
    )
    _write_yaml(model_element_path(model_root, "float32", kind="base_type"), FLOAT32)
    _write_yaml(model_root / "interfaces" / "IAcc.yaml", {"name": "IAcc", "kind": kind, **members})
    member_field = "operation" if list_name == "calls" else "element"
    ports = (
        [
            {
                "name": port_name,
                "direction": "provide" if list_name == "writes" else "require",
                "interface": "IAcc",
            }
        ]
        if declare_port
        else []
    )
    _seed_swc(
        model_root,
        _swc(
            "A",
            ports=ports,
            runnables=[{"name": "Run", list_name: [{"port": port_name, member_field: member_name}]}],
        ),
    )


def _access_pair(prefix: str, port_seed: str = "P", member_seed: str = "M", *, over: int = 0) -> tuple[str, str]:
    """A port name and a member name summing to the form's budget plus *over*."""
    budget = ACCESS_BUDGET[prefix] + over
    port_length = budget // 2
    return port_seed * port_length, member_seed * (budget - port_length)


class TestTheAccessPointFormsAreJointBudgetsToo:
    """Three more forms derived from **two** declared names.

    ``R_{port}_{element}`` and ``W_{port}_{element}`` leave a port name and each
    data element it reads or writes **125** between them;
    ``Call_{port}_{operation}`` leaves **122**. Unlike the eighth form the two names
    are declared in different layers (the port on the component, the member on
    the interface); the finding is the one ``identifier.generated`` rule on
    ``layer:swc``, the deriving entry's layer.

    An entry is the declaration that makes the name exist, so every declared entry
    is checked whether or not it resolves to anything.
    """

    @pytest.mark.parametrize("prefix", sorted(ACCESS_BUDGET), ids=repr)
    def test_a_pair_exactly_on_the_budget_is_accepted(self, prefix: str, tmp_path: Path) -> None:
        port_name, member_name = _access_pair(prefix)
        assert len(port_name) + len(member_name) == ACCESS_BUDGET[prefix]
        assert len(access_point_name(prefix, port_name, member_name)) == MAX_IDENTIFIER_LENGTH
        _access_swc(tmp_path, port_name, member_name, list_name=ACCESS_LIST_OF[prefix])

        assert _issues(tmp_path) == []

    @pytest.mark.parametrize("prefix", sorted(ACCESS_BUDGET), ids=repr)
    def test_one_character_more_is_reported(self, prefix: str, tmp_path: Path) -> None:
        port_name, member_name = _access_pair(prefix, over=1)
        emitted = access_point_name(prefix, port_name, member_name)
        assert check_identifier(port_name) is None and check_identifier(member_name) is None
        assert check_identifier(emitted) == REASON_LENGTH
        _access_swc(tmp_path, port_name, member_name, list_name=ACCESS_LIST_OF[prefix])

        issues = _issues(tmp_path)

        assert len(issues) == 1, issues
        assert emitted in issues[0]
        assert f"({len(emitted)} characters)" in issues[0]
        assert f"'{port_name}'" in issues[0] and f"'{member_name}'" in issues[0]
        assert f"{ACCESS_BUDGET[prefix]} characters between them" in issues[0]
        assert REASON_LENGTH in issues[0]

    def test_the_longer_of_the_two_names_is_the_one_suggested(self, tmp_path: Path) -> None:
        """The member is shorter here, so the port is what must give."""
        port_name, member_name = "P" * 100, "M" * 30
        _access_swc(tmp_path, port_name, member_name)

        issue = _issues(tmp_path)[0]

        assert f"rename '{port_name}' to at most 95 characters" in issue
        assert f"rename '{member_name}'" not in issue

    def test_a_tie_suggests_the_member(self, tmp_path: Path) -> None:
        """A tie suggests the name a designer changes with less blast radius.

        A port name is referenced by every connector that names it, a data element name by the com-specs of the ports on
        its interface."""
        port_name, member_name = "P" * 63, "M" * 63
        _access_swc(tmp_path, port_name, member_name)

        issue = _issues(tmp_path)[0]

        assert f"rename '{member_name}' to at most 62 characters" in issue

    def test_the_suggestion_arithmetic_is_clamped(self, tmp_path: Path) -> None:
        """The suggestion arithmetic is clamped.

        Two conforming 125-character names overflow by 125 — more than either name is long — so an unclamped ``len(name)
        - overflow`` is **negative** and ``suggest_identifier``'s ``cleaned[:max_length]`` answers a suffix-trimmed name
        still far over the limit instead of raising."""
        port_name, member_name = "P" * 125, "M" * 125
        assert check_identifier(port_name) is None and check_identifier(member_name) is None
        _access_swc(tmp_path, port_name, member_name)

        # A 125-character data element overflows its interface's own init
        # constant too, so this model reports two pairs; the access one is
        # selected by name rather than by position.
        emitted = access_point_name(READ_ACCESS_PREFIX, port_name, member_name)
        issue = next(issue for issue in _issues(tmp_path) if emitted in issue)

        suggested = re.search(r"for example to '([^']*)'", issue)
        assert suggested is not None, issue
        assert check_identifier(suggested.group(1)) is None, issue
        assert "at most -" not in issue

    def test_an_entry_naming_no_declared_port_is_checked_all_the_same(self, tmp_path: Path) -> None:
        """An entry naming no declared port is checked all the same.

        A list entry is the declaration that makes the name exist, so the budget is a fact about the entry and not about
        what it resolves to. ``swc.access.unknown-port`` is the *other* finding, reported by ``check_runnable_access``
        and not by this check."""
        port_name, member_name = _access_pair(READ_ACCESS_PREFIX, over=1)
        _access_swc(tmp_path, port_name, member_name, declare_port=False)

        issues = _issues(tmp_path)

        assert len(issues) == 1, issues
        assert access_point_name(READ_ACCESS_PREFIX, port_name, member_name) in issues[0]

    def test_the_pair_form_is_not_checked_when_a_declared_name_is_already_reported(self, tmp_path: Path) -> None:
        """One root cause, one issue — ``check_identifier_conformance``'s own rule.

        Both halves: an illegal port name and an illegal member name."""
        _access_swc(tmp_path, "my port", "M" * 100)
        assert len(_issues(tmp_path)) == 1

        _access_swc(tmp_path, "P" * 120, "my value")
        assert len(_issues(tmp_path)) == 1

    def test_a_runnable_declaring_no_access_reports_nothing(self, tmp_path: Path) -> None:
        """The control: the forms exist only where an entry declares them."""
        _seed_swc(tmp_path, _swc("A", runnables=[{"name": "Run"}]))

        assert _issues(tmp_path) == []

    def test_the_finding_is_the_generated_rule_on_the_swc_layer(self, tmp_path: Path) -> None:
        """``rule:identifier.generated`` already names ``layer:swc``.

        The entry that derives the name is a member of a component, so no ``appliesTo`` moves for these three forms."""
        port_name, member_name = _access_pair(READ_ACCESS_PREFIX, over=1)
        _access_swc(tmp_path, port_name, member_name)

        records = check_identifier_conformance(str(tmp_path)).records

        assert [record.rule for record in records] == ["identifier.generated"]
        assert [(subject.layer, subject.element) for subject in records[0].subjects] == [("swc", "A")]


class TestTheEventCheckUsesTheFinalName:
    """The final event name is not knowable from the runnable name alone.

    ``_unique_event_names`` appends ``_0``/``_1`` **retroactively**.

    The reachable shape is one runnable carrying two events of the same type —
    both want ``TMT_{runnable}``. Two same-named runnables cannot be used: the
    duplicate-name guard rejects that model earlier, so such a test would prove
    nothing about a model that reaches generation.
    """

    def test_a_name_that_only_overflows_after_the_counter_is_reported(self, tmp_path: Path) -> None:
        # 123 + len("TMT_") = 127, legal; the retroactive "_0" makes it 129.
        runnable = "R" * (MAX_IDENTIFIER_LENGTH - len(TIMING_EVENT_PREFIX) - 1)
        assert check_identifier(f"{TIMING_EVENT_PREFIX}{runnable}") is None

        _seed_swc(
            tmp_path,
            _swc(
                "A",
                runnables=[
                    {
                        "name": runnable,
                        "events": [{"type": "timing", "period": 0.01}, {"type": "timing", "period": 0.02}],
                    }
                ],
            ),
        )
        issues = _issues(tmp_path)
        assert len(issues) == 2, issues
        assert any(f"{TIMING_EVENT_PREFIX}{runnable}_0" in issue for issue in issues), issues
        assert any(f"{TIMING_EVENT_PREFIX}{runnable}_1" in issue for issue in issues), issues

    def test_the_uncollided_name_of_the_same_length_is_accepted(self, tmp_path: Path) -> None:
        """The control: one event, no counter, nothing reported."""
        runnable = "R" * (MAX_IDENTIFIER_LENGTH - len(TIMING_EVENT_PREFIX) - 1)
        _seed_swc(
            tmp_path,
            _swc("A", runnables=[{"name": runnable, "events": [{"type": "timing", "period": 0.01}]}]),
        )
        assert _issues(tmp_path) == []


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------


class TestTheMessageIsActionable:
    def test_it_names_the_layer_the_element_and_a_suggestion(self, tmp_path: Path) -> None:
        _seed_swc(tmp_path, _swc(BAD_NAME))
        issue = _reported(tmp_path, BAD_NAME)

        assert " in the swc layer " in issue and str(tmp_path) not in issue and ".yaml" not in issue
        assert "SWC 'My SWC'" in issue
        assert "My_SWC" in issue
        assert "swcraft impact" in issue

    def test_the_reason_distinguishes_pattern_from_length(self, tmp_path: Path) -> None:
        _seed_swc(tmp_path, _swc(BAD_NAME))
        assert REASON_PATTERN in _reported(tmp_path, BAD_NAME)

        long_root = tmp_path / "long"
        _seed_swc(long_root, _swc("S" * (MAX_IDENTIFIER_LENGTH + 1)))
        assert REASON_LENGTH in _issues(long_root)[0]

    def test_a_generated_form_message_states_the_budget_for_the_declared_name(self, tmp_path: Path) -> None:
        name = "S" * 112
        _seed_swc(tmp_path, _swc(name))
        issue = _reported(tmp_path, name)

        budget = MAX_IDENTIFIER_LENGTH - len(INTERNAL_BEHAVIOR_SUFFIX)
        assert f"at most {budget} characters" in issue
        assert f"{len(name)}{INTERNAL_BEHAVIOR_SUFFIX}" not in issue  # the emitted length, not the declared one
        assert f"({MAX_IDENTIFIER_LENGTH + 1} characters)" in issue

    def test_each_layer_names_its_own_layer_and_no_file(self, tmp_path: Path) -> None:
        """Each layer is named by the layer alone, ``swc/``'s extra group level included.

        One layer per reader loop; ``swc/`` is the only layer whose files are not directly under the layer directory."""
        _seed_swc(tmp_path, _swc("A", ports=[{"name": "my port", "direction": "provide", "interface": "I"}]))
        _write_yaml(model_element_path(tmp_path, "t", kind="base_type"), {**FLOAT32, "name": "my type"})
        _write_yaml(
            tmp_path / "interfaces" / "i.yaml",
            {
                "name": "I Sensor",
                "kind": "sender_receiver",
                "data_elements": [{"name": "d", "implementation_type": "my type"}],
            },
        )
        _write_yaml(
            tmp_path / "connectors" / "c.yaml",
            {"name": "My Connector", "from": {"swc": "A", "port": "p"}, "to": {"swc": "B", "port": "q"}},
        )

        for name, layer in (
            ("my port", "swc"),
            ("my type", "base_types"),
            ("I Sensor", "interfaces"),
            ("My Connector", "connectors"),
        ):
            issue = _reported(tmp_path, name)
            assert f" in the {layer} layer " in issue and str(tmp_path) not in issue and ".yaml" not in issue, issue


class TestTheImpactHintNamesACommandThatWorks:
    """The ``swcraft impact`` hint is offered only for top-level layers, with the name quoted.

    ``impact_of()`` resolves top-level layers only (a port and a runnable are
    attributes, not their own ``ElementRef``). The name is quoted because every
    name the hint is offered for was just refused, and ``My SWC`` is two argv words
    unquoted.
    """

    @pytest.mark.parametrize(
        ("seed", "name", "subject"),
        [
            ("swc", BAD_NAME, "SWC"),
            ("connector", "My Connector", "Connector"),
            ("interface", "I Sensor", "Interface"),
            ("data_type", "my type", "Data type"),
        ],
    )
    def test_a_top_level_layer_gets_a_quoted_pointer(self, tmp_path: Path, seed: str, name: str, subject: str) -> None:
        if seed == "swc":
            _seed_swc(tmp_path, _swc(name))
        elif seed == "connector":
            _write_yaml(
                tmp_path / "connectors" / "c.yaml",
                {"name": name, "from": {"swc": "A", "port": "p"}, "to": {"swc": "B", "port": "q"}},
            )
        elif seed == "interface":
            _write_yaml(
                tmp_path / "interfaces" / "i.yaml",
                {
                    "name": name,
                    "kind": "sender_receiver",
                    "data_elements": [{"name": "d", "implementation_type": "float32"}],
                },
            )
        else:
            _write_yaml(model_element_path(tmp_path, "t", kind="base_type"), {**FLOAT32, "name": name})

        issue = _reported(tmp_path, name)
        assert f"'swcraft impact \"{name}\"'" in issue, issue

    @pytest.mark.parametrize(
        ("seed", "name"),
        [
            ("port", "my port"),
            ("runnable", "my run"),
            ("data_element", "my value"),
            ("literal", "1st"),
        ],
    )
    def test_a_nested_name_gets_no_pointer(self, tmp_path: Path, seed: str, name: str) -> None:
        """A nested name gets no pointer: advice that fails is worse than none.

        ``swcraft impact "my port"`` answers ``Element 'my port' not found in any layer.``, and the message already
        names the declaring layer."""
        if seed == "port":
            _seed_swc(tmp_path, _swc("A", ports=[{"name": name, "direction": "provide", "interface": "I"}]))
        elif seed == "runnable":
            _seed_swc(tmp_path, _swc("A", runnables=[{"name": name}]))
        elif seed == "data_element":
            _write_yaml(
                tmp_path / "interfaces" / "i.yaml",
                {
                    "name": "ISensor",
                    "kind": "sender_receiver",
                    "data_elements": [{"name": name, "implementation_type": "float32"}],
                },
            )
        else:
            _write_yaml(
                model_element_path(tmp_path, "e", kind="enum"),
                {"name": "Gear_T", "kind": "enum", "literals": [{"name": name, "value": 0}]},
            )

        assert "swcraft impact" not in _reported(tmp_path, name)

    def test_a_generated_form_of_a_top_level_name_keeps_the_pointer(self, tmp_path: Path) -> None:
        name = "S" * 112
        _seed_swc(tmp_path, _swc(name))
        assert f"'swcraft impact \"{name}\"'" in _reported(tmp_path, name)


class TestSuggestIdentifier:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("My SWC", "My_SWC"),
            ("_leading", "leading"),
            ("9lives", "lives"),
            ("a-b.c", "a_b_c"),
            ("Brake/Controller", "Brake_Controller"),
            ("___", "Element"),
            ("", "Element"),
            ("Größe", "Gr__e"),
        ],
    )
    def test_the_suggestion_is_derived_and_conforming(self, name: str, expected: str) -> None:
        assert suggest_identifier(name) == expected
        assert check_identifier(suggest_identifier(name)) is None

    def test_it_truncates_to_the_limit(self) -> None:
        assert len(suggest_identifier("S" * 400)) == MAX_IDENTIFIER_LENGTH

    def test_it_leaves_room_for_a_generated_suffix(self) -> None:
        budget = MAX_IDENTIFIER_LENGTH - len(INTERNAL_BEHAVIOR_SUFFIX)
        suggestion = suggest_identifier("S" * 400, max_length=budget)
        assert check_identifier(f"{suggestion}{INTERNAL_BEHAVIOR_SUFFIX}") is None


# ---------------------------------------------------------------------------
# Where the check runs
# ---------------------------------------------------------------------------


class TestTheCheckReachesEveryGate:
    def test_it_is_wired_into_the_aggregation_point(self, tmp_path: Path) -> None:
        _seed_swc(tmp_path, _swc(BAD_NAME))
        issues = check_cross_layer_consistency(str(tmp_path)).issues
        assert any(BAD_NAME in issue and REASON_PATTERN in issue for issue in issues), issues

    def test_check_model_fails_on_it(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from swcraft_tools.cli import main

        project = tmp_path / "proj"
        _seed_swc(project / "model", _swc(BAD_NAME))
        _write_yaml(
            project / "swcraft.yaml",
            {
                "name": "proj",
                "schema_version": "1.0",
                "model_root": "model",
                "dist_root": "dist",
                "views_root": "views",
                "outputs": {"arxml": "dist/model.arxml", "needs": "dist/needs.json"},
            },
        )

        monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(project / "swcraft.yaml")])
        with pytest.raises(SystemExit) as exit_info:
            main()
        assert exit_info.value.code == 1
        assert BAD_NAME in capsys.readouterr().err

    def test_the_mcp_validate_and_build_tools_route_through_the_aggregation_point(self) -> None:
        """Both MCP tools call ``check_cross_layer_consistency``.

        So wiring the check there is what carries it to the agent surface — asserted on the source rather than by
        starting a server."""
        module = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
        by_name = {
            node.name: node for node in ast.walk(module) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for tool in ("validate", "build"):
            calls = {
                ast.unparse(node.func)
                for node in ast.walk(by_name[tool])
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }
            assert "validator.check_cross_layer_consistency" in calls, tool

    def test_the_template_build_graph_runs_check_model(self) -> None:
        graph = yaml.safe_load(TEMPLATE_MOON_YML.read_text(encoding="utf-8"))
        assert "check-model" in graph["tasks"]["check-model"]["args"]
        for task in ("generate-arxml", "generate-diagrams", "generate-needs"):
            assert "check-model" in graph["tasks"][task]["deps"]


class TestAConformingModelIsUnaffected:
    """Zero offenders across every model this repository can reach, so a later change cannot quietly introduce one."""

    @pytest.mark.parametrize(
        "model_root",
        [
            REPO_ROOT / "examples" / "basic" / "model",
            REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "component_exchange" / "model",
            REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "hardware" / "model",
            REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "templates" / "vehicle-architecture" / "model",
        ],
    )
    def test_no_offender(self, model_root: Path) -> None:
        result = check_identifier_conformance(str(model_root))
        assert result.consistent, result.issues


class TestAnImplementationTypeHasNoBudgetOfItsOwn:
    """A value or a reference type has no budget of its own: both gates accept every length up to 128.

    The writer invents no application view for such a type (a composite member
    names a declared ``application_type``). The two gates must agree at every
    length: a guard still charging eight characters for an ``_AppView`` form would
    refuse a name generation accepts, and disagree here at 121.
    """

    @staticmethod
    def _seed(root: Path, name: str, kind: str) -> None:
        _write_yaml(model_element_path(root, "float32", kind="base_type"), FLOAT32)
        write_model_element(root, {"name": "Speed_T", "kind": "value_type", "base": "float32"})
        body = (
            {"kind": "value_type", "base": "float32"}
            if kind == "value_type"
            else {"kind": "reference_type", "base": "Speed_T"}
        )
        _write_yaml(model_element_path(root, "t", kind=body["kind"]), {"name": name, **body})

    @pytest.mark.parametrize("kind", ["value_type", "reference_type"])
    def test_every_length_up_to_the_limit_is_accepted_by_both_gates(self, kind: str, tmp_path: Path) -> None:
        from swcraft_tools.writer import build_workspace

        accepted: list[int] = []
        for length in range(MAX_IDENTIFIER_LENGTH - 12, MAX_IDENTIFIER_LENGTH + 1):
            root = tmp_path / str(length)
            name = "V" * length
            self._seed(root, name, kind)
            reported = _issues(root)
            try:
                build_workspace(str(root))
                refused = ""
            except ValueError as refusal:
                refused = str(refusal)
            assert bool(reported) == bool(refused), (kind, length, reported, refused)
            if not reported:
                accepted.append(length)

        assert accepted == list(range(MAX_IDENTIFIER_LENGTH - 12, MAX_IDENTIFIER_LENGTH + 1)), accepted
        assert max(accepted) == MAX_IDENTIFIER_LENGTH

    def test_a_base_type_has_no_generated_form_at_all(self, tmp_path: Path) -> None:
        """At the full 128 characters a base type is legal: nothing is generated from it."""
        name = "B" * MAX_IDENTIFIER_LENGTH
        _write_yaml(model_element_path(tmp_path, "t", kind="base_type"), {**FLOAT32, "name": name})
        assert _issues(tmp_path) == []


# ---------------------------------------------------------------------------
# The application-type generated forms
# ---------------------------------------------------------------------------
#
# An application type's name is the stem of up to three more emitted
# ``SHORT-NAME``s: the ``UNIT``, the ``COMPU-METHOD`` and the ``DATA-CONSTR``
# beside it. Each is emitted **only when its field is declared** (unlike the
# kind-scoped ``_CompuMethod``), which the collision half of the rule reads too
# (``validator._generated_application_forms``). Each lengthens **one** declared
# name, so the budget is the single-name one.

#: The three forms, as ``(suffix, the field that makes the writer emit it, a
#: declaration of that field)``. Each budget is derived from
#: :data:`MAX_IDENTIFIER_LENGTH` and the writer's own constant, never restated.
APPLICATION_FORMS: tuple[tuple[str, str, Any], ...] = (
    (UNIT_SUFFIX, "unit", "mps"),
    (COMPU_SUFFIX, "conversion", {"factor": 2.0, "offset": 1.0}),
    (RANGE_SUFFIX, "range", {"min": 0.0, "max": 100.0}),
)


def _application_type(model_root: Path, name: str, **declared: Any) -> None:
    """One application type file, declaring exactly the fields *declared* names."""
    _write_yaml(
        model_element_path(model_root, "app", kind="application_type"),
        {"name": name, "kind": "application_type", **declared},
    )


def _write_application_type(model_root: Path, name: str, **declared: Any) -> str:
    """The same element through the generic write path, which refuses before writing."""
    from swcraft_tools.schema import ApplicationDataType
    from swcraft_tools.writer.elements import write_element

    element = ApplicationDataType.model_validate({"name": name, "kind": "application_type", **declared})
    return write_element(str(model_root), model_element_storage_name(name, kind="application_type"), element)


def _write_base_type(model_root: Path, name: str) -> str:
    from swcraft_tools.schema import BaseDataType
    from swcraft_tools.writer.elements import write_element

    return write_element(
        str(model_root),
        model_element_storage_name(name, kind="base_type"),
        BaseDataType.model_validate({**FLOAT32, "name": name}),
    )


class TestTheApplicationTypeFormsAreCheckedOnlyWhereTheFieldIsDeclared:
    """The presence rule, one case per form and one inversion per form.

    ``_CompuMethod`` is reserved for a *kind*; these three for a **declared
    field**, because that is what the writer emits them for. A stricter rule would
    refuse a name generation accepts, a looser one would let a declared type
    collide with an emitted one.
    """

    @pytest.mark.parametrize(("suffix", "field", "value"), APPLICATION_FORMS)
    def test_a_declared_field_lengthens_the_name(self, suffix: str, field: str, value: Any, tmp_path: Path) -> None:
        name = "A" * (MAX_IDENTIFIER_LENGTH - len(suffix) + 1)
        _application_type(tmp_path, name, **{field: value})
        assert check_identifier(name) is None

        assert f"{name}{suffix}" in _reported(tmp_path, name)

    @pytest.mark.parametrize(("suffix", "field", "value"), APPLICATION_FORMS)
    def test_an_undeclared_field_generates_nothing(self, suffix: str, field: str, value: Any, tmp_path: Path) -> None:
        """The inversion: the same over-long name with the field left out is legal."""
        del field, value
        name = "A" * (MAX_IDENTIFIER_LENGTH - len(suffix) + 1)
        _application_type(tmp_path, name)

        assert _issues(tmp_path) == []

    def test_a_blank_unit_is_no_unit(self, tmp_path: Path) -> None:
        """The field's default is the empty string, and spaces are what clearing it leaves."""
        name = "A" * (MAX_IDENTIFIER_LENGTH - len(UNIT_SUFFIX) + 1)
        _application_type(tmp_path, name, unit="   ")

        assert _issues(tmp_path) == []

    def test_an_empty_conversion_still_generates(self, tmp_path: Path) -> None:
        """``conversion: {}`` is the identity conversion, and the writer emits it.

        Both of its fields carry a default, so an empty map is a *declaration*
        where a blank ``unit`` is an absence — the asymmetry
        ``validator._generated_application_forms`` states and this case pins.
        """
        name = "A" * (MAX_IDENTIFIER_LENGTH - len(COMPU_SUFFIX) + 1)
        _application_type(tmp_path, name, conversion={})

        assert f"{name}{COMPU_SUFFIX}" in _reported(tmp_path, name)

    def test_a_type_declaring_all_three_is_reported_once_per_form(self, tmp_path: Path) -> None:
        """One name, three emitted forms, three findings — the migration list in one run."""
        name = "A" * (MAX_IDENTIFIER_LENGTH - len(UNIT_SUFFIX) + 1)
        _application_type(tmp_path, name, unit="mps", conversion={}, range={"min": 0.0, "max": 1.0})

        issues = _issues(tmp_path)
        assert len(issues) == 3, issues
        for suffix, _field, _value in APPLICATION_FORMS:
            assert any(f"'{name}{suffix}'" in issue for issue in issues), (suffix, issues)

    def test_the_declared_name_is_reported_once_and_no_form_is(self, tmp_path: Path) -> None:
        """One root cause, one issue: a declared name that fails is not charged three more times."""
        _application_type(tmp_path, "My Type", unit="mps", conversion={}, range={"min": 0.0, "max": 1.0})

        issues = _issues(tmp_path)
        assert len(issues) == 1, issues
        assert "'My Type'" in issues[0]


class TestTheApplicationFormBudgetsAreMeasuredNotCopied:
    """The three budgets, measured rather than transcribed.

    ``_Unit`` is **five** characters, so its budget is 123, not the others' 122.
    Every declared length from ``MAX_IDENTIFIER_LENGTH - 12`` to
    ``MAX_IDENTIFIER_LENGTH`` is offered to ``check-model`` **and** to the write
    path (``write_element``, the one the editing protocol drives); the two must
    agree on each, and the longest name both accept is compared with the
    arithmetic.
    """

    @pytest.mark.parametrize(("suffix", "field", "value"), APPLICATION_FORMS)
    def test_the_longest_name_both_gates_accept_is_measured(
        self, suffix: str, field: str, value: Any, tmp_path: Path
    ) -> None:
        accepted: list[int] = []
        for length in range(MAX_IDENTIFIER_LENGTH - 12, MAX_IDENTIFIER_LENGTH + 1):
            root = tmp_path / str(length)
            name = "V" * length
            _application_type(root, name, **{field: value})
            reported = any(f"'{name}{suffix}'" in issue for issue in _issues(root))
            try:
                _write_application_type(root / "written", name, **{field: value})
                refused = False
            except ValueError as refusal:
                refused = f"{name}{suffix}" in str(refusal)
            assert reported == refused, (suffix, length, reported, refused)
            if not reported:
                accepted.append(length)

        assert accepted == list(range(MAX_IDENTIFIER_LENGTH - 12, max(accepted) + 1)), accepted
        assert max(accepted) == MAX_IDENTIFIER_LENGTH - len(suffix)

    def test_the_three_budgets_are_not_one_figure(self) -> None:
        """123 / 122 / 122 — and the point of the case is that the first is not the other two."""
        budgets = {suffix: MAX_IDENTIFIER_LENGTH - len(suffix) for suffix, _field, _value in APPLICATION_FORMS}
        assert budgets == {UNIT_SUFFIX: 123, COMPU_SUFFIX: 122, RANGE_SUFFIX: 122}


class TestTheMappingSetsOwnShortNameIsChecked:
    """``layer:mapping_sets`` is ARXML-bound: one file, one ``DATA-TYPE-MAPPING-SET``.

    A map's two sides are ``*-REF`` paths into names the data-type files
    declare, so they are checked where they are declared — the division
    ``_check_connector_names`` makes for a connector's endpoints — and no
    generated form lengthens the set's own name.
    """

    def test_a_non_conforming_set_name_is_reported(self, tmp_path: Path) -> None:
        _write_yaml(model_element_path(tmp_path, "s", layer="mapping_sets"), {"name": "My Set", "maps": []})

        issue = _reported(tmp_path, "My Set")
        assert "Mapping set 'My Set'" in issue
        assert " in the mapping_sets layer " in issue
        assert str(tmp_path) not in issue and ".yaml" not in issue, issue

    def test_a_set_name_at_the_full_limit_is_legal(self, tmp_path: Path) -> None:
        """No generated form lengthens it, so the whole 128 characters are the set's."""
        name = "S" * MAX_IDENTIFIER_LENGTH
        _write_yaml(model_element_path(tmp_path, "s", layer="mapping_sets"), {"name": name, "maps": []})

        assert _issues(tmp_path) == []

    def test_a_maps_entry_is_not_reported_here(self, tmp_path: Path) -> None:
        """Both sides are references; those names are refused where they are declared."""
        _write_yaml(
            model_element_path(tmp_path, "s", layer="mapping_sets"),
            {"name": "Set", "maps": [{"application": "My App", "implementation": "My Impl"}]},
        )

        assert _issues(tmp_path) == []

    def test_the_write_path_refuses_what_check_model_reports(self, tmp_path: Path) -> None:
        """The fail-closed twin, through the generic element writer."""
        from swcraft_tools.schema import MappingSet
        from swcraft_tools.writer.elements import write_element

        before = sorted(path.as_posix() for path in tmp_path.rglob("*"))
        element = MappingSet.model_validate({"name": "My Set", "maps": []})

        with pytest.raises(ValueError) as excinfo:
            write_element(str(tmp_path), model_element_storage_name("My Set", layer="mapping_sets"), element)

        assert "legal AUTOSAR Identifier" in str(excinfo.value), str(excinfo.value)
        assert sorted(path.as_posix() for path in tmp_path.rglob("*")) == before


class TestTheWritePathRefusesTheApplicationFormsToo:
    """The write path's half of the three forms, and its presence rule.

    ``check-model`` reports and the write path refuses the same names, the
    reservation included: a declared type may not take a name an application type
    beside it generates, in **either** order. A form collides inside its owner's
    package only, so the colliding occupant is an application type and a base type
    under the name is written.
    """

    @pytest.mark.parametrize(("suffix", "field", "value"), APPLICATION_FORMS)
    def test_an_over_budget_declared_field_is_refused(
        self, suffix: str, field: str, value: Any, tmp_path: Path
    ) -> None:
        name = "A" * (MAX_IDENTIFIER_LENGTH - len(suffix) + 1)

        with pytest.raises(ValueError) as excinfo:
            _write_application_type(tmp_path, name, **{field: value})

        assert f"{name}{suffix}" in str(excinfo.value), str(excinfo.value)
        assert list(tmp_path.rglob("*.yaml")) == []

    @pytest.mark.parametrize(("suffix", "field", "value"), APPLICATION_FORMS)
    def test_the_same_name_without_the_field_is_written(
        self, suffix: str, field: str, value: Any, tmp_path: Path
    ) -> None:
        del field, value
        name = "A" * (MAX_IDENTIFIER_LENGTH - len(suffix) + 1)

        _write_application_type(tmp_path, name)

        assert (model_element_path(tmp_path, name, kind="application_type")).exists()

    @pytest.mark.parametrize(("suffix", "field", "value"), APPLICATION_FORMS)
    def test_a_declared_type_colliding_with_an_emitted_name_is_refused(
        self, suffix: str, field: str, value: Any, tmp_path: Path
    ) -> None:
        """The first direction: the application type is already there."""
        _write_application_type(tmp_path, "Speed", **{field: value})
        colliding = f"Speed{suffix}"

        with pytest.raises(ValueError) as excinfo:
            _write_application_type(tmp_path, colliding)

        assert colliding in str(excinfo.value), str(excinfo.value)
        assert not model_element_path(tmp_path, colliding, kind="application_type").exists()

    @pytest.mark.parametrize(("suffix", "field", "value"), APPLICATION_FORMS)
    def test_the_other_direction_is_refused_too(self, suffix: str, field: str, value: Any, tmp_path: Path) -> None:
        """The colliding type is there first, and the application type arrives after."""
        colliding = f"Speed{suffix}"
        _write_application_type(tmp_path, colliding)

        with pytest.raises(ValueError) as excinfo:
            _write_application_type(tmp_path, "Speed", **{field: value})

        assert colliding in str(excinfo.value), str(excinfo.value)
        assert not model_element_path(tmp_path, "Speed", kind="application_type").exists()

    @pytest.mark.parametrize(("suffix", "field", "value"), APPLICATION_FORMS)
    def test_a_stem_that_declares_the_field_elsewhere_is_left_alone(
        self, suffix: str, field: str, value: Any, tmp_path: Path
    ) -> None:
        """A stem that generates nothing reserves nothing, in both directions."""
        del field, value
        _write_application_type(tmp_path, "Speed")
        colliding = f"Speed{suffix}"

        # An occupant of the stem's own package, the only one a form could collide with.
        _write_application_type(tmp_path, colliding)

        assert (model_element_path(tmp_path, colliding, kind="application_type")).exists()
        assert _issues(tmp_path) == []

    @pytest.mark.parametrize(("suffix", "field", "value"), APPLICATION_FORMS)
    def test_a_type_of_another_package_under_the_name_is_written(
        self, suffix: str, field: str, value: Any, tmp_path: Path
    ) -> None:
        """A base type lies in ``/DataTypes/Base`` and the form in ``/DataTypes/Application``."""
        _write_application_type(tmp_path, "Speed", **{field: value})
        colliding = f"Speed{suffix}"

        _write_base_type(tmp_path, colliding)

        assert (model_element_path(tmp_path, colliding, kind="base_type")).exists()
        assert _issues(tmp_path) == []


class TestTheTwoPresenceRulesAgree:
    """The reporting half, the write half and the emitter decide the same forms.

    The **presence** rule is not a constant: ``validator.generated_data_type_forms``
    and ``writer.mutations._generated_data_type_forms`` (which reads the emitter's
    ``arxml_generation.generated_data_type_forms``) implement one sentence twice,
    and only a matrix catches one drifting on the asymmetry (a blank ``unit`` is no
    unit, an empty ``conversion`` is a conversion). The matrix: every subset of the
    application type's three fields in declared and undeclared spellings, and a
    value type's two bounds alone and together beside its ``unit``, which
    generates nothing.
    """

    @pytest.mark.parametrize(
        "declared",
        [
            {"kind": "application_type"},
            {"kind": "application_type", "unit": "mps"},
            {"kind": "application_type", "unit": ""},
            {"kind": "application_type", "unit": "   "},
            {"kind": "application_type", "conversion": {}},
            {"kind": "application_type", "conversion": {"factor": 2.0}},
            {"kind": "application_type", "range": {}},
            {"kind": "application_type", "range": {"min": 0.0, "max": 1.0}},
            {"kind": "application_type", "unit": "mps", "conversion": {}, "range": {"min": 0.0, "max": 1.0}},
            {"kind": "application_type", "unit": " ", "conversion": {"offset": 1.0}},
            {"kind": "value_type", "base": "float32"},
            {"kind": "value_type", "base": "float32", "unit": "mps"},
            {"kind": "value_type", "base": "float32", "value_min": 0.0},
            {"kind": "value_type", "base": "float32", "value_max": 1.0},
            {"kind": "value_type", "base": "float32", "value_min": -1.0, "value_max": 1.0, "unit": "mps"},
        ],
        ids=repr,
    )
    def test_the_same_forms_are_generated_on_both_sides(self, declared: dict) -> None:
        from pydantic import TypeAdapter
        from swcraft_tools.schema import DataType
        from swcraft_tools.validator.generated_names import generated_data_type_forms as reported
        from swcraft_tools.writer.arxml_generation import generated_data_type_forms as emitted
        from swcraft_tools.writer.mutations import _generated_data_type_forms as refused

        element = TypeAdapter(DataType).validate_python({"name": "Speed", **declared})

        assert set(reported(element)) == {suffix for suffix, _what in refused(element)} == set(emitted(element))

    @pytest.mark.parametrize(
        ("declared", "expected"),
        [
            ({"kind": "value_type", "base": "float32"}, frozenset()),
            ({"kind": "value_type", "base": "float32", "unit": "mps"}, frozenset()),
            ({"kind": "value_type", "base": "float32", "value_min": 0.0}, frozenset({"_Range"})),
            ({"kind": "value_type", "base": "float32", "value_max": 0.0}, frozenset({"_Range"})),
        ],
        ids=repr,
    )
    def test_a_value_type_generates_its_range_exactly_when_a_bound_is_declared(self, declared, expected) -> None:
        from pydantic import TypeAdapter
        from swcraft_tools.schema import DataType
        from swcraft_tools.validator.generated_names import generated_data_type_forms as reported

        assert reported(TypeAdapter(DataType).validate_python({"name": "Ratio", **declared})) == expected
