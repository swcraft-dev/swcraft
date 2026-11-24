"""The connector check reads the ``swc`` layer once, however many connectors there are.

Guards against a layer read per connector (every connector re-reading every SWC file, a cost of
SWCs times connectors), which no example with a handful of connectors would show.

What the guard pins is a **count**, not a duration: ``check_connector_consistency`` causes exactly
one ``read_swc_model`` for the whole run, on a model large enough that a per-connector read would
be a thousand. A duration bound would pass on a fast machine and fail on a loaded one; a count is
the same everywhere. The second test keeps the other half of the contract: a caller that hands no
map — ``create_connector`` in ``writer/mutations.py`` — still gets the layer read for it, so the
single-connector API keeps its shape.

The interface rule judges compatibility rather than name equality, so ``interfaces/`` and
``data_types/`` are read once for the whole check too, and at the single-connector caller not at
all where the two ports name one interface.

What the guard does not reach: the other checks. Each reads the model through its own
``ModelView`` when it is called alone, so each reads a layer once; ``check_cross_layer_consistency``
hands every check one view, so a composed run reads each layer once (``test_model_view.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import WIDE_CONNECTORS as CONNECTORS
from swcraft_tools.reader import read_swc_model
from swcraft_tools.schema import Connector
from swcraft_tools.validator import check_connector_compatibility, check_connector_consistency, model_view

# The model is the shared ``wide_model`` fixture in ``conftest.py``: the response-budget
# suite asks a different question of the same expensive model.


def _counting_layer_reader(monkeypatch: pytest.MonkeyPatch, name: str) -> list[str]:
    """Route one of the view's layer readers through a counter; the real reader still runs."""
    calls: list[str] = []
    real = getattr(model_view, name)

    def counted(model_root: str):
        calls.append(model_root)
        return real(model_root)

    monkeypatch.setattr(model_view, name, counted)
    return calls


def _counting_reader(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Route the view's ``read_swc_model`` through a counter; the real reader still runs."""
    return _counting_layer_reader(monkeypatch, "read_swc_model")


def test_the_consistency_check_reads_the_swc_layer_exactly_once(
    wide_model: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _counting_reader(monkeypatch)

    result = check_connector_consistency(str(wide_model))

    assert result.consistent, result.issues
    assert len(list((wide_model / "connectors").glob("*.yaml"))) == CONNECTORS
    assert len(calls) == 1, f"{len(calls)} layer reads for {CONNECTORS} connectors — the per-connector re-read is back"


def test_a_single_connector_check_without_a_map_still_reads_the_layer_itself(
    wide_model: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _counting_reader(monkeypatch)
    connector = Connector.model_validate(
        {"name": "one", "from": {"swc": "S0", "port": "p0"}, "to": {"swc": "S1", "port": "r0"}}
    )

    assert check_connector_compatibility(str(wide_model), connector).compatible
    assert len(calls) == 1

    swc_map = {swc.name: swc for swc in read_swc_model(str(wide_model))}
    calls.clear()
    assert check_connector_compatibility(str(wide_model), connector, swc_map).compatible
    assert calls == []


def test_the_consistency_check_reads_the_interface_and_data_type_layers_exactly_once(
    wide_model: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interface and data type layers, held to the same count as the ``swc`` layer.

    ``check_connector_compatibility`` reads them for itself when handed no map, so the
    composed pass hands them down.
    """
    interfaces = _counting_layer_reader(monkeypatch, "read_interfaces")
    data_types = _counting_layer_reader(monkeypatch, "read_data_description")

    result = check_connector_consistency(str(wide_model))

    assert result.consistent, result.issues
    assert (len(interfaces), len(data_types)) == (1, 1), (interfaces, data_types)


def test_a_single_connector_whose_two_ports_name_one_interface_reads_neither_new_layer(
    wide_model: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same-name short-circuit, measured at the caller rather than in the predicate.

    Every connector of this model names ``ISig`` on both sides. The two maps are read
    inside the differing-names branch alone, so such a model reaches neither reader.
    """
    interfaces = _counting_layer_reader(monkeypatch, "read_interfaces")
    data_types = _counting_layer_reader(monkeypatch, "read_data_description")
    connector = Connector.model_validate(
        {"name": "one", "from": {"swc": "S0", "port": "p0"}, "to": {"swc": "S1", "port": "r0"}}
    )

    assert check_connector_compatibility(str(wide_model), connector).compatible
    assert (interfaces, data_types) == ([], [])
