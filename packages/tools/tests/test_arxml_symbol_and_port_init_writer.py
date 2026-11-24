"""The ARXML writer writes a runnable's ``symbol`` and a port's own ``init``.

**A runnable's ``SYMBOL``** is its ``symbol`` when stated, else its ``name``. ``RunnableEntity.symbol`` is a
``CIdentifier`` of multiplicity 1, *"the symbol describing this RunnableEntity's entry point"* (SWCT
R4.2.2 Table 7.3, R4.3.0 Table 7.4); ``SYMBOL`` is ``AR:C-IDENTIFIER`` in ``group RUNNABLE-ENTITY`` of
both licensed schemas.

**A port's own init value.** A ``com_specs`` entry's ``init`` is the port's value for the element it
names, and a com-spec's ``initValue`` supersedes the data prototype's (``[TPS_SWCT_01182]``, SWCT R4.2.2
``:21053-21077``, R4.3.0 ``:23450-23475``). It is written in place, as the non-queued com-spec's
``INIT-VALUE``, last in the com-spec; an entry stating no ``init`` keeps the reference to the element's
``{Interface}_{Element}_Init`` constant, and no new ``CONSTANT-SPECIFICATION`` is written.

**The expected fragments are hand-derived** from the schema's sequences and the writer's documented value
shapes, as a compact rendering of the element tree.

**Every committed tree's output is unchanged, byte for byte**, against :data:`RAW_BEFORE`, after the
re-freeze harness's reversals flatten the data description packages and put back the old port API options
and value forms.

What this does not reach: whether a symbol is a unique C identifier and whether a port's ``init``
conforms to its type (``test_runnable_symbol_rules.py``, ``test_port_init_rules.py``); the XSD evidence
(``test_arxml_xsd_conformance.py``); and the importers.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import REPO_ROOT
from fidelity_strip_support import (
    flatten_data_description_packages,
    parse,
    restore_numerical_values,
    restore_port_api_options,
    serialize,
)
from lxml import etree
from swcraft_tools.component_exchange import export_component
from swcraft_tools.reader import read_interfaces
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml
from test_writer_fidelity_refreeze import CASES

INIT_VALUES_MODEL_ROOT = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "init_values" / "model"
COMPONENT = "SignalsSwc"
VERSIONS = ("4.2.2", "4.3.0")

#: The symbol the patched model states for its first runnable; the second states none.
STATED_SYMBOL = "Signals_MainEntry"

#: The provide port's entries: a scalar overriding the element's ``7``, a value on an element
#: that states no ``init``, and a record.
PROVIDE_ENTRIES: list[dict[str, Any]] = [
    {"data_element": "speed", "init": 9},
    {"data_element": "plain", "init": 3},
    {"data_element": "limits", "init": {"low": -5.5, "high": 5.5, "tiny": 0.25, "whole": 100.0}},
]

#: The require port's entries: an enum literal, an array, an array of records holding a record,
#: and a value equal to the element's own (still the port's statement, so still written in place).
REQUIRE_ENTRIES: list[dict[str, Any]] = [
    {"data_element": "mode", "init": "OFF"},
    {"data_element": "bytes", "init": [3, 4]},
    {"data_element": "deep", "init": [{"row": {"index": 4, "mode": "ON"}}, {"row": {"index": 5, "mode": "OFF"}}]},
    {"data_element": "speed", "init": 7},
]


def symbol_and_port_init_model_root(tmp_path: Path) -> Path:
    """A copy of ``fixtures/init_values``' model: runnables with and without a symbol, port inits of every shape."""
    model_root = tmp_path / "model"
    shutil.copytree(INIT_VALUES_MODEL_ROOT, model_root)
    path = model_root / "swc" / "signals" / f"{COMPONENT}.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["runnables"][0]["symbol"] = STATED_SYMBOL
    document["runnables"].append({"name": "SignalsAux", "events": [{"type": "timing", "period": 0.02}]})
    ports = {port["name"]: port for port in document["ports"]}
    ports["pSignals"]["com_specs"] = PROVIDE_ENTRIES
    ports["rSignals"]["com_specs"] = REQUIRE_ENTRIES
    path.write_text(yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")
    return model_root


# Reading the output


def _local(node: etree._Element) -> str:
    return etree.QName(node).localname


def _compact(node: etree._Element) -> str:
    """``TAG=text`` for a leaf, ``TAG[child,child]`` otherwise; attributes left out."""
    children = [child for child in node if isinstance(child.tag, str)]
    if not children:
        return f"{_local(node)}={node.text}"
    return f"{_local(node)}[{','.join(_compact(child) for child in children)}]"


def _child(node: etree._Element, tag: str) -> etree._Element | None:
    return next((child for child in node if isinstance(child.tag, str) and _local(child) == tag), None)


def _short_name(node: etree._Element) -> str | None:
    found = _child(node, "SHORT-NAME")
    return None if found is None else found.text


def _parse(path: Path) -> etree._Element:
    return etree.parse(str(path)).getroot()


def _component(root: etree._Element) -> etree._Element:
    return next(
        node
        for node in root.iter()
        if isinstance(node.tag, str)
        and _local(node) == "APPLICATION-SW-COMPONENT-TYPE"
        and _short_name(node) == COMPONENT
    )


def _com_spec(root: etree._Element, port: str, element: str) -> etree._Element:
    """The com-spec of *port* on the component whose ``DATA-ELEMENT-REF`` names *element*."""
    port_node = next(node for node in _child(_component(root), "PORTS") if _short_name(node) == port)
    specs = next(child for child in port_node if _local(child) in ("PROVIDED-COM-SPECS", "REQUIRED-COM-SPECS"))
    (spec,) = [spec for spec in specs if _child(spec, "DATA-ELEMENT-REF").text.rsplit("/", 1)[-1] == element]
    return spec


def _symbols(root: etree._Element) -> dict[str, str]:
    return {
        _short_name(node): _child(node, "SYMBOL").text
        for node in _component(root).iter()
        if isinstance(node.tag, str) and _local(node) == "RUNNABLE-ENTITY"
    }


def _generate(model_root: Path, tmp_path: Path, version: str) -> etree._Element:
    output = tmp_path / f"model-{version}.arxml"
    generate_arxml(str(model_root), str(output), version=version)
    return _parse(output)


# The expected fragments, hand-derived


def _number(value: str, label: str | None = None) -> str:
    labelled = f"SHORT-LABEL={label}," if label is not None else ""
    return f"NUMERICAL-VALUE-SPECIFICATION[{labelled}VALUE={value}]"


def _literal(name: str, label: str | None = None) -> str:
    """An enumeration leaf: its literal's text, one ``VT``."""
    labelled = f"SHORT-LABEL={label}," if label is not None else ""
    return f"APPLICATION-VALUE-SPECIFICATION[{labelled}CATEGORY=VALUE,SW-VALUE-CONT[SW-VALUES-PHYS[VT={name}]]]"


def _row(index: str, mode: str) -> str:
    inner = f"RECORD-VALUE-SPECIFICATION[SHORT-LABEL=row,FIELDS[{_number(index, 'index')},{_literal(mode, 'mode')}]]"
    return f"RECORD-VALUE-SPECIFICATION[FIELDS[{inner}]]"


#: ``(port, element) -> the INIT-VALUE`` each stated value is written as.
IN_PLACE: dict[tuple[str, str], str] = {
    ("pSignals", "speed"): f"INIT-VALUE[{_number('9')}]",
    ("pSignals", "plain"): f"INIT-VALUE[{_number('3')}]",
    ("pSignals", "limits"): (
        "INIT-VALUE[RECORD-VALUE-SPECIFICATION[FIELDS["
        + ",".join((_number("-5.5", "low"), _number("5.5", "high"), _number("0.25", "tiny"), _number("100.0", "whole")))
        + "]]]"
    ),
    # Mode_T's literal 'OFF', as its text.
    ("rSignals", "mode"): f"INIT-VALUE[{_literal('OFF')}]",
    ("rSignals", "bytes"): f"INIT-VALUE[ARRAY-VALUE-SPECIFICATION[ELEMENTS[{_number('3')},{_number('4')}]]]",
    ("rSignals", "deep"): f"INIT-VALUE[ARRAY-VALUE-SPECIFICATION[ELEMENTS[{_row('4', 'ON')},{_row('5', 'OFF')}]]]",
    ("rSignals", "speed"): f"INIT-VALUE[{_number('7')}]",
}

#: The com-spec kind each port's entries sit in.
SPEC_KIND = {"pSignals": "NONQUEUED-SENDER-COM-SPEC", "rSignals": "NONQUEUED-RECEIVER-COM-SPEC"}


def _reference(element: str) -> str:
    return f"INIT-VALUE[CONSTANT-REFERENCE[CONSTANT-REF=/PortInterfaces/ISignals_{element}_Init]]"


# The model is legal


def test_the_patched_model_passes_check_model(tmp_path: Path) -> None:
    result = check_cross_layer_consistency(str(symbol_and_port_init_model_root(tmp_path)))
    assert result.consistent, result.issues


# SYMBOL


@pytest.mark.parametrize("version", VERSIONS)
def test_symbol_is_the_stated_symbol_or_the_name(tmp_path: Path, version: str) -> None:
    root = _generate(symbol_and_port_init_model_root(tmp_path), tmp_path, version)
    assert _symbols(root) == {"SignalsMain": STATED_SYMBOL, "SignalsAux": "SignalsAux"}


def test_a_stated_symbol_equal_to_the_name_writes_the_same_bytes(tmp_path: Path) -> None:
    """``null`` means *the symbol is the name*, so stating the name changes nothing."""
    plain = tmp_path / "plain.arxml"
    generate_arxml(str(INIT_VALUES_MODEL_ROOT), str(plain))
    model_root = tmp_path / "model"
    shutil.copytree(INIT_VALUES_MODEL_ROOT, model_root)
    path = model_root / "swc" / "signals" / f"{COMPONENT}.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["runnables"][0]["symbol"] = document["runnables"][0]["name"]
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")
    stated = tmp_path / "stated.arxml"
    generate_arxml(str(model_root), str(stated))
    assert stated.read_bytes() == plain.read_bytes()


# A port's own init


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize(("port", "element"), sorted(IN_PLACE), ids=[f"{p}-{e}" for p, e in sorted(IN_PLACE)])
def test_a_stated_port_init_is_written_in_place_last_in_its_com_spec(
    tmp_path: Path, version: str, port: str, element: str
) -> None:
    spec = _com_spec(_generate(symbol_and_port_init_model_root(tmp_path), tmp_path, version), port, element)
    assert _local(spec) == SPEC_KIND[port]
    children = [child for child in spec if isinstance(child.tag, str)]
    assert _local(children[-1]) == "INIT-VALUE"
    assert [_local(child) for child in children].count("INIT-VALUE") == 1
    assert _compact(children[-1]) == IN_PLACE[(port, element)]


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("port", sorted(SPEC_KIND))
def test_a_com_spec_whose_entry_states_no_init_keeps_the_element_s_reference(
    tmp_path: Path, version: str, port: str
) -> None:
    root = _generate(symbol_and_port_init_model_root(tmp_path), tmp_path, version)
    stated = {element for p, element in IN_PLACE if p == port}
    (interface,) = read_interfaces(str(INIT_VALUES_MODEL_ROOT))
    for data_element in interface.data_elements:
        if data_element.name in stated:
            continue
        init_value = _child(_com_spec(root, port, data_element.name), "INIT-VALUE")
        if data_element.init is None:
            assert init_value is None, data_element.name
        else:
            assert _compact(init_value) == _reference(data_element.name)
            assert _child(_child(init_value, "CONSTANT-REFERENCE"), "CONSTANT-REF").get("DEST") == (
                "CONSTANT-SPECIFICATION"
            )


@pytest.mark.parametrize("version", VERSIONS)
def test_an_element_with_no_init_and_a_port_with_one(tmp_path: Path, version: str) -> None:
    root = _generate(symbol_and_port_init_model_root(tmp_path), tmp_path, version)
    assert _compact(_child(_com_spec(root, "pSignals", "plain"), "INIT-VALUE")) == IN_PLACE[("pSignals", "plain")]
    assert _child(_com_spec(root, "rSignals", "plain"), "INIT-VALUE") is None
    prototype = next(
        node
        for node in root.iter()
        if isinstance(node.tag, str) and _local(node) == "VARIABLE-DATA-PROTOTYPE" and _short_name(node) == "plain"
    )
    assert _child(prototype, "INIT-VALUE") is None


@pytest.mark.parametrize("version", VERSIONS)
def test_the_element_s_own_init_and_constant_are_untouched(tmp_path: Path, version: str) -> None:
    root = _generate(symbol_and_port_init_model_root(tmp_path), tmp_path, version)
    (interface,) = read_interfaces(str(INIT_VALUES_MODEL_ROOT))
    prototypes = {
        _short_name(node): node
        for node in root.iter()
        if isinstance(node.tag, str) and _local(node) == "VARIABLE-DATA-PROTOTYPE"
    }
    for data_element in interface.data_elements:
        if data_element.init is not None:
            assert _compact(_child(prototypes[data_element.name], "INIT-VALUE")) == _reference(data_element.name)


@pytest.mark.parametrize("version", VERSIONS)
def test_no_constant_specification_beyond_the_elements(tmp_path: Path, version: str) -> None:
    root = _generate(symbol_and_port_init_model_root(tmp_path), tmp_path, version)
    (interface,) = read_interfaces(str(INIT_VALUES_MODEL_ROOT))
    constants = sorted(
        _short_name(node)
        for node in root.iter()
        if isinstance(node.tag, str) and _local(node) == "CONSTANT-SPECIFICATION"
    )
    assert constants == sorted(f"ISignals_{e.name}_Init" for e in interface.data_elements if e.init is not None)


def test_the_component_export_writes_both(tmp_path: Path) -> None:
    """The component export shares the emitters, so it writes the symbol and the value in place too."""
    model_root = symbol_and_port_init_model_root(tmp_path)
    output = tmp_path / f"{COMPONENT}.arxml"
    export_component(str(model_root), COMPONENT, str(output))
    root = _parse(output)
    assert _symbols(root) == {"SignalsMain": STATED_SYMBOL, "SignalsAux": "SignalsAux"}
    for (port, element), expected in IN_PLACE.items():
        assert _compact(_child(_com_spec(root, port, element), "INIT-VALUE")) == expected


# Every committed tree is unchanged

#: Each committed tree's raw ARXML SHA-256, ``4.2.2`` then ``4.3.0``, before the writer wrote either field.
#: Narrowed only by the re-freeze harness's named reversals, never re-recorded.
RAW_BEFORE: dict[str, tuple[str, str]] = {
    "examples/basic/model": (
        "679afb7048652b04ac7bcbd996fc324e5b3c65b9447086095f0f8cfed30cbd58",
        "f7991a782c1ac0370f674017a4ec18084a5d3735a7618788fdb55d6f4cc805fd",
    ),
    "packages/tools/src/swcraft_tools/templates/vehicle-architecture/model": (
        "6a1f6039722ae770e555200f3a039c93aa7b907114d8ae160c1f09b9209b48c3",
        "61882ec245289922d51f404acfd2c14bd65445bc5a50a242e1b08b042834f3e7",
    ),
    "packages/tools/tests/fixtures/access_points/model": (
        "bcb9b8dd8c761acd122f4a6aa894eef5f3a943414be68f6bf455e9602b820c1e",
        "19efbead6f9a678cf545b38a3fc28268d7d92fc4f3062671cab4fa7757287b43",
    ),
    "packages/tools/tests/fixtures/compatible_interfaces/model": (
        "39d85cbebb032482a041c418e8827e44bb210773e9ef734f67b3e0482d1f82e1",
        "d63a6640ed6c9f1f56313f6962d06323eb5c792a99adb42cf64961f87736d1fa",
    ),
    "packages/tools/tests/fixtures/component_exchange/model": (
        "32d24231fde4665888245a96e085c3ced6a95fc91b73f2ef74e74b36a2e13032",
        "045874725e42ae7b29338e5d250e4dbd1823824ff3b807dd4c1cfc799bc1c732",
    ),
    # Measured on a later input of this tree (its stub's `external_ref` and the root composition's
    # description changed); the writer the record was taken with builds that input to these bytes.
    "packages/tools/tests/fixtures/compositions/model": (
        "e14ca80c89e1e9794bbec77ee5968e4bbf2530bfc8dec677ed9cd4796d34c221",
        "78fbfcab03392fbfec889036e9a83cee87b6afd8b1607f729dc439b814184458",
    ),
    "packages/tools/tests/fixtures/diagrams/model": (
        "e1bcfb4fe4e6d541e1fa70b833e1615d67fc713113283553a1e1e45a176b4a6d",
        "7ee96b96056846c83a1a405d7867edc503d7d0df9a4fffd5a19bf6e5872e6869",
    ),
    "packages/tools/tests/fixtures/hardware/model": (
        "e5651677508946662f6cb5cce4307eb3b1f4315a069d3e4fa44eeb1faa1e5b3f",
        "f72b393720b12d12c26e8e764025234798a1e6661662934511756bf555030d7d",
    ),
    "packages/tools/tests/fixtures/init_values/model": (
        "686d5f49985add08f0484700a508124dcc4b8765d2f229f344b578ea01d311fd",
        "0d30dca37a5e49c1f7abac48a483c01d62c7167145b79176bc9dbbd676b2ab7c",
    ),
    "packages/tools/tests/fixtures/interface_compatibility/model": (
        "0b56f3caeb37d2286e7fd5941f81854b1db737bba4d4c6b13b0f0f10dba39cdf",
        "d149f588ed28f4bd7046192bc42204704cbc5ee482407e4c0c619b78152ab25d",
    ),
    "packages/tools/tests/fixtures/operation_events/model": (
        "8cb86c602f91671f5c512bef7d593301c029c17bf6af404e5adb64ce88144b7c",
        "bbc69d8fa894b7819a6e0920afaadce3592d65cef8aef2ce023d238fa5e03fd7",
    ),
    "packages/tools/tests/fixtures/safety/model": (
        "e5bdf23848a8a182d67cbacfebd91bed02473dc92ba2f5d58766a009efd333a4",
        "5bb85c205d1e980403a787cfaf7f6ba263d0de3db748b3e4a346895fcb5dbdc7",
    ),
    "packages/tools/tests/fixtures/showcase/model": (
        "e7a630ec8d50bed0276d6875cc18b8c1715c55e32ba825f8448b957350d2af23",
        "b5959ad9a1f9bb09f709786b335104a8b71d0f54b6bf852b0c1752820a27f0f9",
    ),
}


def test_every_committed_tree_is_recorded() -> None:
    assert {(tree, version) for tree in RAW_BEFORE for version in VERSIONS} == set(CASES)


@pytest.mark.parametrize(("tree", "version"), CASES, ids=[f"{t}:{v}" for t, v in CASES])
def test_a_committed_tree_s_output_is_unchanged_byte_for_byte(tree: str, version: str, tmp_path: Path) -> None:
    """Byte for byte once the harness's three reversals have run, and by nothing else.

    The reversals run in the harness's order: packages flattened, port API options, value forms.
    """
    output = tmp_path / "model.arxml"
    generate_arxml(str(REPO_ROOT / tree), str(output), version=version)
    root = parse(output.read_bytes())
    flatten_data_description_packages(root)
    restore_port_api_options(root)
    restore_numerical_values(root)
    assert hashlib.sha256(serialize(root)).hexdigest() == RAW_BEFORE[tree][VERSIONS.index(version)]
