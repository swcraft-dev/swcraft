"""Helpers the differ and impact test modules share: a snapshot with empty layers and a connector."""

from __future__ import annotations

from typing import Any

from swcraft_tools.schema import Connector
from swcraft_tools.snapshot import ModelSnapshot


def make_snapshot(**layers: Any) -> ModelSnapshot:
    """Build a snapshot whose every layer is empty except the ones passed as keyword arguments."""
    defaults: dict[str, Any] = dict(
        compositions=[],
        swcs=[],
        interfaces=[],
        implementation_types=[],
        connectors=[],
        requirements=[],
        tasks=[],
        sequences=[],
        hardware=[],
        mapping_sets=[],
        base_types=[],
        application_types=[],
        units=[],
        compu_methods=[],
        data_constraints=[],
    )
    defaults.update(layers)
    return ModelSnapshot(**defaults)


def make_connector(name: str, from_swc: str, from_port: str, to_swc: str, to_port: str) -> Connector:
    """Build the connector *name* from ``from_swc.from_port`` to ``to_swc.to_port``."""
    return Connector(
        name=name,
        **{
            "from": {"swc": from_swc, "port": from_port},
            "to": {"swc": to_swc, "port": to_port},
        },
    )
