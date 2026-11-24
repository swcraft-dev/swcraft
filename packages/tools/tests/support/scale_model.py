"""The wide model, built once and read by two test projects.

``packages/tools/tests/conftest.py`` wraps it as the ``wide_model`` fixture, which
``test_validator_scale.py``, ``test_editing_budget.py`` and ``test_editing_read.py``
take. ``packages/mcp/tests/test_response_budget.py`` needs the **same** model to
prove that the budget the engine enforces internally is the budget a host actually
sees, and a pytest fixture cannot cross a project boundary — so the builder lives
in a plain module that both sides import (``packages/mcp/pyproject.toml`` declares
the ``pythonpath``); ``test_measure_mcp_scale.py`` and ``test_diff_impact_tools.py``
there import it too.

The model is deliberately **wide** rather than deep: the two MCP responses that
can grow unbounded are a layer read and a hub component's element read, and both
are functions of width.
"""

from __future__ import annotations

from pathlib import Path

import yaml

#: ``WIDE_SWCS`` components of ``WIDE_PORTS_PER_SWC`` ports each, and
#: ``WIDE_CONNECTORS`` valid connectors between them; ``test_validator_scale.py``'s
#: assertions read these figures.
WIDE_SWCS = 20
WIDE_PORTS_PER_SWC = 50
#: The provide/require pairs one SWC carries — the halving, spelled ONCE and read
#: by both loops below and by ``WIDE_CONNECTORS``, so an odd `WIDE_PORTS_PER_SWC`
#: cannot make the count and the model on disk disagree.
_PORT_PAIRS = WIDE_PORTS_PER_SWC // 2
#: every provide port of SWC i feeds the matching require port of SWC i+1
WIDE_CONNECTORS = WIDE_SWCS * _PORT_PAIRS


def _write(path: Path, document: dict) -> None:
    # `newline="\n"` with `encoding=`: `test_platform_io.py`'s AST scan covers
    # the product trees only, so nothing else catches a text write here.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8", newline="\n")


def build_wide_model(model_root: Path) -> Path:
    """Write the wide model into *model_root* and return it.

    A model **root**, not a project: a caller that needs a manifest writes one
    beside it. Every element file is one ``load_snapshot`` accepts — the whole
    root is loaded by the budget suites, not only the two layers the connector
    check parses.
    """
    _write(model_root / "hardware" / "CPU.yaml", {"name": "CPU", "kind": "processor"})
    _write(
        model_root / "interfaces" / "ISig.yaml",
        {
            "name": "ISig",
            "kind": "sender_receiver",
            "data_elements": [{"name": "value", "implementation_type": "uint8"}],
        },
    )
    from conftest import model_element_path

    _write(
        model_element_path(model_root, "uint8", kind="base_type"),
        {"name": "uint8", "kind": "base_type", "size": 8, "encoding": "NONE"},
    )
    for i in range(WIDE_SWCS):
        ports = []
        for j in range(_PORT_PAIRS):
            ports.append({"name": f"p{j}", "direction": "provide", "interface": "ISig"})
            ports.append({"name": f"r{j}", "direction": "require", "interface": "ISig"})
        _write(model_root / "swc" / "wide" / f"S{i}.yaml", {"name": f"S{i}", "ports": ports})
    for i in range(WIDE_SWCS):
        for j in range(_PORT_PAIRS):
            src, dst = f"S{i}", f"S{(i + 1) % WIDE_SWCS}"
            _write(
                model_root / "connectors" / f"{src}_p{j}__{dst}_r{j}.yaml",
                {
                    "name": f"{src}_p{j}__{dst}_r{j}",
                    "from": {"swc": src, "port": f"p{j}"},
                    "to": {"swc": dst, "port": f"r{j}"},
                },
            )
    return model_root
