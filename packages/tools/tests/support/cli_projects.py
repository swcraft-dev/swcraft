"""Write the small `swcraft` projects the CLI suites share."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from conftest import _write_manifest, write_model_element


def dump_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` as block-style YAML, creating the parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")


def write_query_project(project_dir: Path) -> Path:
    """Write a manifest and a model rich enough for every `swcraft query` surface.

    It carries an ``implementation_types`` layer of **two kinds** (so the read order and
    the readers' order differ), an ``swc`` layer of two components in two ``group``
    directories, each with a port, and a memory map (so a map collection's rows carry a
    ``key``).
    """
    manifest = _write_manifest(project_dir)
    model_root = project_dir / "model"
    write_model_element(model_root, {"name": "float32", "kind": "base_type", "size": 32, "encoding": "IEEE754"})
    write_model_element(model_root, {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE"})
    write_model_element(model_root, {"name": "Speed", "kind": "value_type", "base": "float32", "unit": "km/h"})
    write_model_element(model_root, {"name": "Alias", "kind": "reference_type", "base": "Speed"})
    # A data element's `type` resolves in `implementation_types` alone, so the interface
    # names the value type rather than its base type.
    dump_yaml(
        model_root / "interfaces" / "ISpeed.yaml",
        {
            "name": "ISpeed",
            "kind": "sender_receiver",
            "data_elements": [{"name": "speed", "implementation_type": "Speed"}],
        },
    )
    dump_yaml(
        model_root / "swc" / "body" / "SpeedSwc.yaml",
        {
            "name": "SpeedSwc",
            "type": "application",
            "ports": [{"name": "pSpeed", "direction": "provide", "interface": "ISpeed"}],
            "memory": {"RAM0": 128},
            "handle_termination_and_restart": "no_support",
        },
    )
    dump_yaml(
        model_root / "swc" / "control" / "CtrlSwc.yaml",
        {
            "name": "CtrlSwc",
            "type": "application",
            "ports": [{"name": "rSpeed", "direction": "require", "interface": "ISpeed"}],
            "handle_termination_and_restart": "no_support",
        },
    )
    dump_yaml(
        model_root / "compositions" / "Root.yaml",
        {
            "name": "Root",
            "title": "Root composition",
            "root": True,
            "members": [
                {"name": "speed", "type": "SpeedSwc"},
                {"name": "ctrl", "type": "CtrlSwc"},
            ],
        },
    )
    dump_yaml(model_root / "hardware" / "CPU.yaml", {"name": "CPU", "kind": "processor"})
    dump_yaml(model_root / "hardware" / "CORE0.yaml", {"name": "CORE0", "kind": "core", "threshold": 0.8})
    dump_yaml(
        model_root / "hardware" / "RAM0.yaml",
        {"name": "RAM0", "kind": "memory", "type": "RAM", "size": 4096, "accessible": ["CORE0"], "threshold": 0.8},
    )
    return manifest
