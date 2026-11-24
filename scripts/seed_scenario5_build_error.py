"""Seed the live scenario-5 broken-build fixture in examples/basic.

It writes one invalid connector straight to disk, a state the writer refuses, so the live
agent can diagnose the build failure and repair it with one change set adding the missing
provide port.

The broken connector must be the *only* failure: both seeded SWCs carry a ``memory:`` budget
for the ``strict`` memory ``RAM0``, satisfy the one requirement the seed writes (an
unsatisfied requirement is a warning), are members of the root composition
(``rule:composition.connector-endpoint-not-a-member``) and declare
``handle_termination_and_restart`` (``rule:swc.undeclared-termination-and-restart``).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL_ROOT = REPO_ROOT / "examples" / "basic" / "model"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Committed model YAML: a re-seed on Windows must not produce a whole-file diff.
    path.write_text(text, encoding="utf-8", newline="\n")


def place_in_root(root: Path, names: tuple[str, ...]) -> None:
    """Append a member for each of *names* to the root composition, once.

    The append is text, so it holds only while ``members:`` is the file's last top-level
    key; a reshaped ``Root.yaml`` stops the seed instead of producing a file the loader misreads.
    """
    text = root.read_text(encoding="utf-8")
    top_level = [line for line in text.splitlines() if line and not line[0].isspace() and not line.startswith("#")]
    if not text.endswith("\n") or not top_level or top_level[-1] != "members:":
        raise SystemExit(f"{root.name}: 'members:' is not the last top-level key; append the members by hand")
    for name in names:
        if f"  - name: {name}\n" not in text:
            text += f"  - name: {name}\n    type: {name}\n"
    write(root, text)


def main() -> None:
    write(
        MODEL_ROOT / "interfaces" / "ISpeed.yaml",
        """name: ISpeed
kind: sender_receiver
data_elements:
  - name: speed
    implementation_type: float32
    description: Engine speed value
""",
    )
    write(
        MODEL_ROOT / "swc" / "engine" / "EngineController.yaml",
        """name: EngineController
type: application
description: Engine control component seeded for scenario 5
ports: []
runnables: []
memory:
  RAM0: 4096
handle_termination_and_restart: no_support
satisfies:
  - REQ_ENG_SCENARIO5@1
""",
    )
    write(
        MODEL_ROOT / "swc" / "display" / "SpeedDisplay.yaml",
        """name: SpeedDisplay
type: application
description: Speed display component seeded for scenario 5
ports:
  - name: rEngineSpeed
    direction: require
    interface: ISpeed
    description: Required engine speed input
runnables: []
memory:
  RAM0: 2048
handle_termination_and_restart: no_support
satisfies:
  - REQ_ENG_SCENARIO5@1
""",
    )
    write(
        MODEL_ROOT / "connectors" / "EngineCtrl_to_SpeedDisplay.yaml",
        """name: EngineCtrl_to_SpeedDisplay
from:
  swc: EngineController
  port: pEngineSpeed
to:
  swc: SpeedDisplay
  port: rEngineSpeed
description: Broken connector seeded for live build-error scenario
""",
    )
    write(
        MODEL_ROOT / "requirements" / "REQ_ENG_SCENARIO5.yaml",
        """id: REQ_ENG_SCENARIO5
title: Scenario 5 engine speed display requirement
description: Engine speed shall be provided by EngineController and consumed by SpeedDisplay.
req_type: req
version: 1
source: architecture
""",
    )
    place_in_root(MODEL_ROOT / "compositions" / "Root.yaml", ("EngineController", "SpeedDisplay"))
    print("Seeded scenario 5 broken-build fixture in examples/basic.")


if __name__ == "__main__":
    main()
