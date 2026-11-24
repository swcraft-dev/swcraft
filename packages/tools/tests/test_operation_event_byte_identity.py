"""The byte-identity gate: a model without an operation event generates the recorded bytes.

The operation event (``OPERATION-INVOKED-EVENT``) and the runnable's ``concurrent`` flag
(``CAN-BE-INVOKED-CONCURRENTLY``) must not move the output of a model that declares neither: for every committed
model tree, the ARXML at 4.2.2 and 4.3.0 and every file ``swcraft generate diagrams`` writes are byte for byte the
recorded ones. This is the *without* half of the claim.

The record is ``test_external_stubs_byte_identity.WITHOUT_A_STUB``, reused through that module's helpers;
:data:`RECORD_DIGEST` makes a re-measurement of any row fail here. It is a record, not a golden to refresh. The
ARXML is compared after ``termination_strip_support.strip_recorded`` removes the one
``HANDLE-TERMINATION-AND-RESTART`` line per component (gated by ``test_termination_refreeze.py``); the record's
RST column is not read.

**What it does not reach:**

* a model shape none of the recorded trees holds: this is regression evidence, not a proof;
* a tree the loader would read differently: the premise (no operation event, no ``concurrent`` key at all) is read
  off the files with ``yaml.safe_load``;
* the mixed-event fixture, which declares no ``compositions/`` layer and so is not a recorded tree;
* which diagram file moved: the aggregate digest names the tree;
* an uncommitted tree; the CLI's generators, ``generate arxml --component``, ``validate`` and the importer;
* the *with* half, which the writer's and the XSD suites witness; neither half discharges the other.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT
from swcraft_tools.contract import load_contract
from termination_strip_support import strip_recorded
from test_external_stubs_byte_identity import WITHOUT_A_STUB, _diagram_digest, _generate

#: The commit whose generators the record was first measured against.
BRANCH_POINT = "5da69318"

#: One SHA-256 over ``WITHOUT_A_STUB`` -- canonical JSON, keys sorted, rows as lists
#: (:func:`_record_digest`); the record itself stays in one place. Any re-measured cell of the
#: record moves it, and the re-measurement has to say why this module's claim did not move.
RECORD_DIGEST = "08149e87f6e8607f251a2187446a1dbb785d502c39a995662f419b3de061bbb5"

_ARTIFACTS = ("arxml-4.2.2", "arxml-4.3.0")

_CASES = [
    (tree, artifact, WITHOUT_A_STUB[tree][index])
    for tree in WITHOUT_A_STUB
    for index, artifact in enumerate(_ARTIFACTS)
]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record_digest() -> str:
    canonical = json.dumps({tree: list(row) for tree, row in WITHOUT_A_STUB.items()}, sort_keys=True)
    return _sha(canonical.encode("utf-8"))


def _field(field_id: str) -> dict:
    """A field's contract entry, read off the contract rather than spelled a second time."""
    for layer in load_contract()["layers"]:
        for kind in layer["kinds"]:
            for field in kind["fields"]:
                if field["id"] == field_id:
                    return field
    raise AssertionError(f"the contract declares no {field_id}")


def _swc_directory() -> str:
    (layer,) = [layer for layer in load_contract()["layers"] if layer["id"] == "layer:swc"]
    return layer["storage"]["directory"]


def _operation_event_shapes(model_root: Path) -> list[str]:
    """Each operation event and ``concurrent`` key in *model_root*'s component files, read off the files.

    Each is named ``<storage name>:<runnable>:<shape>``.
    """
    runnables = _field("field:swc.swc.runnables")["name"]
    events = _field("field:swc.runnable.events")["name"]
    concurrent = _field("field:swc.runnable.concurrent")["name"]
    event_type = _field("field:swc.operation_event.type")
    (operation,) = event_type["enum"]
    found: list[str] = []
    for path in sorted((model_root / _swc_directory()).rglob("*.yaml"), key=lambda p: p.as_posix()):
        element = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        storage = path.relative_to(model_root).as_posix()
        for runnable in element.get(runnables) or []:
            name = runnable.get("name")
            if concurrent in runnable:
                found.append(f"{storage}:{name}:a concurrent key")
            for event in runnable.get(events) or []:
                if event.get(event_type["name"]) == operation:
                    found.append(f"{storage}:{name}:an operation event")
    return found


def test_the_record_is_the_branch_points() -> None:
    """The sibling module's record is still the one :data:`RECORD_DIGEST` was taken over."""
    assert _record_digest() == RECORD_DIGEST


@pytest.mark.parametrize(("tree", "artifact", "recorded"), _CASES, ids=[f"{t}:{a}" for t, a, _ in _CASES])
def test_the_arxml_is_the_branch_points(tree: str, artifact: str, recorded: str, tmp_path: Path) -> None:
    model_root = REPO_ROOT / tree
    assert model_root.is_dir(), f"{tree} is recorded but not committed"
    output = tmp_path / artifact
    if recorded.startswith("refused: "):
        with pytest.raises(ValueError, match=recorded.removeprefix("refused: ")):
            _generate(model_root, artifact, output)
        return
    _generate(model_root, artifact, output)
    assert _sha(strip_recorded(tree, artifact, output.read_bytes())) == recorded


@pytest.mark.parametrize("tree", sorted(WITHOUT_A_STUB))
def test_every_diagram_file_is_the_branch_points(tree: str, tmp_path: Path) -> None:
    count, digest = _diagram_digest(REPO_ROOT / tree, tmp_path / "views")
    assert (count, digest) == WITHOUT_A_STUB[tree][3:]


@pytest.mark.parametrize("tree", sorted(WITHOUT_A_STUB))
def test_no_recorded_tree_declares_an_operation_event(tree: str) -> None:
    """The premise: no recorded tree declares an operation event or a ``concurrent`` key.

    A tree that gains one leaves this record rather than quietly staying in it.
    """
    shapes = _operation_event_shapes(REPO_ROOT / tree)
    assert shapes == [], f"{tree} holds what this phase added: {shapes}"


def test_the_premise_check_sees_each_shape(tmp_path: Path) -> None:
    """The premise check is not vacuous: each of the two shapes in a scratch tree is found."""
    group = tmp_path / _swc_directory() / "g"
    group.mkdir(parents=True)
    (group / "Server.yaml").write_text(
        "name: Server\nrunnables:\n  - name: Serve\n    concurrent: true\n    events:\n"
        "      - type: timing\n        period: 10ms\n"
        "      - type: operation\n        port: P\n        operation: Op\n",
        encoding="utf-8",
        newline="\n",
    )
    assert _operation_event_shapes(tmp_path) == [
        "swc/g/Server.yaml:Serve:a concurrent key",
        "swc/g/Server.yaml:Serve:an operation event",
    ]
