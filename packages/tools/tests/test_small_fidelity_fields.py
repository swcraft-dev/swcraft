"""Two small fidelity fields: declared, loaded, written back unchanged, and written to ARXML.

A runnable's ``symbol`` (AUTOSAR's ``RunnableEntity.symbol``) and a port's own init value (a ``com_specs`` entry's
``init``, the com-spec's ``initValue``). This module holds the contract half and
the model-file half:

* the contract declares both fields, optional and last in their kinds, and the
  contract's version and counts;
* each field loads and defaults to ``null``;
* each is written back **last in its kind** and **left out while unset**, by the
  generic writer (``writer.elements.write_element``) and by the hand-written
  dumps that write its owner, and the two
  agree;
* every mutation that rewrites a component file keeps both values;
* ``generate arxml`` and the component export write a model stating either.

What it does not reach: the rules over the two fields (a symbol that is not a C
identifier, a duplicate written symbol, a port ``init`` its element's type
cannot hold), the exact ARXML the writer emits for them
(``test_arxml_symbol_and_port_init_writer.py``), and the importers.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from model_contract_fixtures import load_contract
from swcraft_tools.component_exchange import export_component
from swcraft_tools.reader import read_swc_model
from swcraft_tools.schema import PortComSpec, Runnable, SWComponent
from swcraft_tools.writer import (
    add_init_event,
    add_port,
    add_runnable,
    add_timing_event,
    set_swc_memory_budget,
    unset_swc_memory_budget,
    write_element,
)
from swcraft_tools.writer.arxml_generation import generate_arxml
from swcraft_tools.writer.yaml_documents import _port_com_spec_to_dict, runnable_to_dict, swc_to_dict

TESTS = Path(__file__).resolve().parent
EXCHANGE_MODEL_ROOT = TESTS / "fixtures" / "component_exchange" / "model"

#: ``field id -> (owning kind, contract type)``. Both are optional with a ``null`` default.
ADDED_FIELDS: dict[str, tuple[str, str]] = {
    "field:swc.runnable.symbol": ("kind:swc.runnable", "string"),
    "field:swc.port_com_spec.init": ("kind:swc.port_com_spec", "value"),
}

#: Fields appended to the same kinds after these two, set aside so their position stays pinned.
_APPENDED_SINCE = frozenset(
    {
        "field:swc.runnable.wcet",
        "field:swc.runnable.wcet_type",
        "field:swc.runnable.satisfies",
        "field:swc.port.satisfies",
    }
)


def _kinds() -> dict[str, dict[str, Any]]:
    return {kind["id"]: kind for layer in load_contract()["layers"] for kind in layer["kinds"]}


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


class TestTheContractDeclaresTheFields:
    def test_the_version_and_the_counts(self) -> None:
        contract = load_contract()
        kinds = [kind for layer in contract["layers"] for kind in layer["kinds"]]
        fields = [field for kind in kinds for field in kind["fields"]]
        # The shipped contract's version: every contract change moves it.
        assert contract["contractVersion"] == "12.7.1"
        # (layers, kinds, fields) of the shipped contract: a layer, kind or field added or removed moves it.
        assert (len(contract["layers"]), len(kinds), len(fields)) == (15, 67, 359)

    @pytest.mark.parametrize("field_id", sorted(ADDED_FIELDS))
    def test_each_is_optional_and_last_in_its_kind(self, field_id: str) -> None:
        """Last among its kind's fields, :data:`_APPENDED_SINCE` set aside."""
        kind_id, field_type = ADDED_FIELDS[field_id]
        declared = [field for field in _kinds()[kind_id]["fields"] if field["id"] not in _APPENDED_SINCE][-1]
        assert declared["id"] == field_id
        assert declared["type"] == field_type
        assert (declared["required"], declared["default"], declared["immutable"]) == (False, None, False)
        assert "omitAtDefault" not in declared

    def test_the_port_s_init_takes_the_element_s_type(self) -> None:
        element_init = next(
            field for field in _kinds()["kind:interfaces.data_element"]["fields"] if field["name"] == "init"
        )
        port_init = _kinds()["kind:swc.port_com_spec"]["fields"][-1]
        assert port_init["type"] == element_init["type"] == "value"


# ---------------------------------------------------------------------------
# Loading and defaults
# ---------------------------------------------------------------------------


class TestEachFieldLoadsAndDefaults:
    def test_the_defaults(self) -> None:
        assert Runnable(name="Run").symbol is None
        assert PortComSpec(data_element="e").init is None

    @pytest.mark.parametrize("value", [0, 1.5, True, "ON", [1, 2, 3], {"a": 1, "b": [0, 0]}])
    def test_a_stated_init_loads_in_every_shape(self, value: Any) -> None:
        assert PortComSpec.model_validate({"data_element": "e", "init": value}).init == value

    def test_a_stated_symbol_loads_as_written(self) -> None:
        # Whether it is a C identifier is `check-model`'s, as the element's own `init`'s shape is.
        assert Runnable.model_validate({"name": "Run", "symbol": "_Run_Entry"}).symbol == "_Run_Entry"


# ---------------------------------------------------------------------------
# The model writers
# ---------------------------------------------------------------------------

_INIT = {"lo": 0, "hi": [1, 2]}
_ENTRIES = [{"data_element": "value", "enable_update": True, "init": _INIT}, {"data_element": "other", "init": 7}]


def _component() -> SWComponent:
    return SWComponent.model_validate(
        {
            "name": "Carrier",
            "handle_termination_and_restart": "no_support",
            "ports": [{"name": "rIn", "direction": "require", "interface": "IExample", "com_specs": _ENTRIES}],
            "runnables": [
                {
                    "name": "Run",
                    "events": [{"type": "timing", "period": 0.01}],
                    "description": "the main cycle",
                    "symbol": "Carrier_Run",
                }
            ],
        }
    )


class TestTheWritersWriteEachFieldLastAndOmitItUnset:
    def test_the_generic_writer(self, tmp_path: Path) -> None:
        write_element(str(tmp_path), "swc/g/Carrier.yaml", _component(), create=True)
        document = yaml.safe_load((tmp_path / "swc/g/Carrier.yaml").read_text(encoding="utf-8"))
        runnable = document["runnables"][0]
        assert list(runnable)[-1] == "symbol" and runnable["symbol"] == "Carrier_Run"
        entries = document["ports"][0]["com_specs"]
        assert [list(entry)[-1] for entry in entries] == ["init", "init"]
        assert entries == _ENTRIES

    def test_the_generic_writer_leaves_both_out_while_unset(self, tmp_path: Path) -> None:
        plain = SWComponent.model_validate(
            {
                "name": "Plain",
                "handle_termination_and_restart": "no_support",
                "ports": [
                    {
                        "name": "rIn",
                        "direction": "require",
                        "interface": "IExample",
                        "com_specs": [{"data_element": "value", "enable_update": True}],
                    }
                ],
                "runnables": [{"name": "Run", "events": [{"type": "timing", "period": 0.01}]}],
            }
        )
        write_element(str(tmp_path), "swc/g/Plain.yaml", plain, create=True)
        text = (tmp_path / "swc/g/Plain.yaml").read_text(encoding="utf-8")
        assert "symbol" not in text and "init" not in text

    def test_the_hand_written_component_dump_equals_the_generic_one(self, tmp_path: Path) -> None:
        component = _component()
        write_element(str(tmp_path), "swc/g/Carrier.yaml", component, create=True)
        generic = (tmp_path / "swc/g/Carrier.yaml").read_text(encoding="utf-8")
        hand = yaml.safe_dump(swc_to_dict(component), default_flow_style=False, sort_keys=False)
        assert hand == generic

    @pytest.mark.parametrize(
        "init",
        [
            3,
            1.5,
            True,
            "ON",
            {"x": 1, "y": 2},
            [1, 2, 3],
            {"where": {"x": 1, "y": 2}, "samples": [1.0, 2.5, 0.0]},
            [{"x": 1, "y": 2}, {"x": 3, "y": 4}],
        ],
        ids=[
            "integer",
            "float",
            "boolean",
            "enum-literal-name",
            "record",
            "array",
            "record-holding-an-array",
            "array-of-records",
        ],
    )
    def test_a_port_init_of_every_shape_round_trips_through_both_writers(self, init: Any, tmp_path: Path) -> None:
        """Loaded, written by the generic writer and by the hand-written component dump, reloaded equal.

        The two dumps agree byte for byte, and each file reads back to the model
        it was written from -- the shape the value was stated in included: a
        literal name spelled like a YAML 1.1 boolean stays a string, a float
        stays a float, and a map nested in a list stays in place.
        """
        component = SWComponent.model_validate(
            {
                "name": "Shape",
                "handle_termination_and_restart": "no_support",
                "ports": [
                    {
                        "name": "rIn",
                        "direction": "require",
                        "interface": "IExample",
                        "com_specs": [{"data_element": "value", "init": init}],
                    }
                ],
            }
        )
        assert component.ports[0].com_specs[0].init == init

        write_element(str(tmp_path), "swc/g/Shape.yaml", component, create=True)
        generic = (tmp_path / "swc/g/Shape.yaml").read_text(encoding="utf-8")
        hand = yaml.safe_dump(swc_to_dict(component), default_flow_style=False, sort_keys=False)

        assert hand == generic
        (reloaded,) = read_swc_model(str(tmp_path))
        assert reloaded == component
        assert SWComponent.model_validate(yaml.safe_load(hand)) == component
        assert type(reloaded.ports[0].com_specs[0].init) is type(init)

    def test_the_two_member_dumps_write_only_what_is_stated(self) -> None:
        assert runnable_to_dict(Runnable(name="Run")) == {"name": "Run", "events": []}
        assert list(runnable_to_dict(Runnable(name="Run", symbol="Run", description="d"))) == [
            "name",
            "events",
            "description",
            "symbol",
        ]
        assert _port_com_spec_to_dict(PortComSpec(data_element="e")) == {"data_element": "e"}
        # A stated `0`, `false` or empty list is a value, written as held; only `None` is left out.
        for value in (0, False, [], {}):
            assert _port_com_spec_to_dict(PortComSpec(data_element="e", init=value)) == {
                "data_element": "e",
                "init": value,
            }


def _seed(project_model: Path) -> None:
    target = project_model / "swc" / "example" / "Carrier.yaml"
    target.write_text(
        yaml.safe_dump(swc_to_dict(_component()), default_flow_style=False, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )


@pytest.fixture
def model_root(tmp_path: Path) -> Path:
    import editing_vector_fixtures as fixtures

    project = fixtures.build_fixture(tmp_path / "project", "read-swc-element")
    root = project / "model"
    _seed(root)
    return root


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda root: add_port(str(root), "Carrier", "pExtra", "provide", "IExample"), id="add_port"),
        pytest.param(lambda root: add_runnable(str(root), "Carrier", "Extra"), id="add_runnable"),
        pytest.param(
            lambda root: (
                add_runnable(str(root), "Carrier", "Extra"),
                add_timing_event(str(root), "Carrier", "Extra", 0.02),
            ),
            id="add_timing_event",
        ),
        pytest.param(lambda root: add_init_event(str(root), "Carrier", "Run"), id="add_init_event"),
        pytest.param(lambda root: set_swc_memory_budget(str(root), "Carrier", "RAM0", 64), id="set_memory"),
        pytest.param(
            lambda root: (
                set_swc_memory_budget(str(root), "Carrier", "RAM0", 64),
                unset_swc_memory_budget(str(root), "Carrier", "RAM0"),
            ),
            id="unset_memory",
        ),
    ],
)
def test_every_mutation_rewriting_a_component_keeps_both_values(model_root: Path, mutation: Any) -> None:
    mutation(model_root)
    carrier = next(swc for swc in read_swc_model(str(model_root)) if swc.name == "Carrier")
    run = next(runnable for runnable in carrier.runnables if runnable.name == "Run")
    assert run.symbol == "Carrier_Run"
    port = next(port for port in carrier.ports if port.name == "rIn")
    assert [entry.model_dump(exclude_none=True) for entry in port.com_specs] == _ENTRIES


# ---------------------------------------------------------------------------
# The writer writes them: generate arxml and the component export
# ---------------------------------------------------------------------------


def _with_symbol(model_root: Path) -> None:
    path = model_root / "swc" / "actuation" / "ActuatorSwc.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["runnables"][0]["symbol"] = "Actuator_Main"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


def _with_port_init(model_root: Path) -> None:
    path = model_root / "swc" / "actuation" / "ActuatorSwc.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    (port,) = [port for port in document["ports"] if port["name"] == "rSpeed"]
    port["com_specs"] = [{"data_element": "speed", "init": 3}]
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")


#: Each stated field and the line the written ARXML carries for it. The receiver com-spec's
#: ``INIT-VALUE`` holds the port's own value in place (``test_arxml_symbol_and_port_init_writer.py``
#: holds every shape at both versions); here it is one line of the generated text.
_STATED = [
    pytest.param(_with_symbol, "<SYMBOL>Actuator_Main</SYMBOL>", id="symbol"),
    pytest.param(_with_port_init, "<VALUE>3</VALUE>", id="port-init"),
]


@pytest.mark.parametrize(("patch", "written"), _STATED)
@pytest.mark.parametrize("version", ["4.2.2", "4.3.0"])
def test_generate_arxml_writes_a_stated_field(tmp_path: Path, patch: Any, written: str, version: str) -> None:
    model_root = tmp_path / "model"
    shutil.copytree(EXCHANGE_MODEL_ROOT, model_root)
    patch(model_root)
    out = tmp_path / "model.arxml"

    generate_arxml(str(model_root), str(out), version=version)

    assert written in out.read_text(encoding="utf-8")


@pytest.mark.parametrize(("patch", "written"), _STATED)
def test_the_component_export_writes_a_stated_field(tmp_path: Path, patch: Any, written: str) -> None:
    model_root = tmp_path / "model"
    shutil.copytree(EXCHANGE_MODEL_ROOT, model_root)
    patch(model_root)
    out = tmp_path / "ActuatorSwc.arxml"

    export_component(str(model_root), "ActuatorSwc", str(out))

    assert written in out.read_text(encoding="utf-8")
    assert not Path(f"{out}.tmp").exists()


def test_a_model_stating_neither_still_generates(tmp_path: Path) -> None:
    out = tmp_path / "model.arxml"
    generate_arxml(str(EXCHANGE_MODEL_ROOT), str(out))
    assert out.exists()
