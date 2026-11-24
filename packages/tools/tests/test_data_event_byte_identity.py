"""The byte-identity gate: a model without a data-received event generates what it always did.

For every committed model tree the record names that declares no ``kind:swc.data_event`` (a
``DATA-RECEIVED-EVENT``), the generated ARXML (4.2.2 and 4.3.0) and every file ``swcraft generate diagrams``
writes are byte for byte what the record holds.

It **reuses the record** of ``test_external_stubs_byte_identity`` (``WITHOUT_A_STUB``); :data:`RECORD_DIGEST`
is pinned here as a literal, so a re-measurement of any row fails here too and has to say whether this claim
moved. The premise (no recorded tree declares a data-received event) is asserted, with field names and the
discriminator read off the contract. The trees in :data:`DECLARES_A_VARIABLE` have their ARXML cells
excluded; their diagrams are compared. The ARXML bytes are compared after
``termination_strip_support.strip_recorded``.

**This is a record, not a golden to refresh:** a change that moves a digest here is an output change for a
model without a data-received event and carries its own ``Qualification Impact`` section.

What it does not reach: models beyond the recorded trees (regression evidence, not a proof); a tree the
model loader would read differently from the premise's file scan; which file moved; the CLI,
``generate arxml --component``, ``validate``, ``resource-report``, ``metrics``, the experiments and the
importer; the *with* half, which is the writer's and the XSD suites' to witness.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from swcraft_tools.contract import load_contract
from swcraft_tools.yaml_load import load_yaml_document
from termination_strip_support import strip_recorded
from test_external_stubs_byte_identity import WITHOUT_A_STUB, _diagram_digest, _generate
from test_inter_runnable_variable_byte_identity import DECLARES_A_VARIABLE

#: The commit the record was first measured at.
BRANCH_POINT = "0ab38e7c1"

#: One SHA-256 over ``WITHOUT_A_STUB`` -- canonical JSON, keys sorted, rows as lists
#: (:func:`_record_digest`). Any re-measured cell of the record moves it.
RECORD_DIGEST = "08149e87f6e8607f251a2187446a1dbb785d502c39a995662f419b3de061bbb5"

_ARXML = ("arxml-4.2.2", "arxml-4.3.0")
_ARTIFACTS = _ARXML

_CASES = [
    (tree, artifact, WITHOUT_A_STUB[tree][index])
    for tree in WITHOUT_A_STUB
    for index, artifact in enumerate(_ARTIFACTS)
    if not (artifact in _ARXML and tree in DECLARES_A_VARIABLE)
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


def _data_event_shapes(model_root: Path) -> list[str]:
    """Every data-received event in *model_root*'s component files, as ``<storage>:<runnable>:...``."""
    runnables = _field("field:swc.swc.runnables")["name"]
    events = _field("field:swc.runnable.events")["name"]
    event_type = _field("field:swc.data_event.type")
    (data,) = event_type["enum"]
    found: list[str] = []
    for path in sorted((model_root / _swc_directory()).rglob("*.yaml"), key=lambda p: p.as_posix()):
        element = load_yaml_document(path.read_text(encoding="utf-8")) or {}
        storage = path.relative_to(model_root).as_posix()
        for runnable in element.get(runnables) or []:
            found.extend(
                f"{storage}:{runnable.get('name')}:a data-received event"
                for event in runnable.get(events) or []
                if event.get(event_type["name"]) == data
            )
    return found


def test_the_record_is_the_branch_points() -> None:
    """The sibling module's record is the one :data:`RECORD_DIGEST` pins."""
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


def test_no_recorded_tree_declares_a_data_received_event() -> None:
    """No recorded tree declares a data-received event; one that gains one leaves the claim visibly."""
    declaring = {tree: shapes for tree in WITHOUT_A_STUB if (shapes := _data_event_shapes(REPO_ROOT / tree))}
    assert declaring == {}


def test_the_premise_check_sees_the_shape(tmp_path: Path) -> None:
    """The premise check finds a data-received event and not a timing event beside it."""
    group = tmp_path / _swc_directory() / "g"
    group.mkdir(parents=True)
    (group / "Receiver.yaml").write_text(
        "name: Receiver\nrunnables:\n  - name: Run\n    events:\n"
        "      - type: timing\n        period: 10ms\n"
        "      - type: data\n        port: In\n        element: Speed\n",
        encoding="utf-8",
        newline="\n",
    )
    assert _data_event_shapes(tmp_path) == ["swc/g/Receiver.yaml:Run:a data-received event"]
