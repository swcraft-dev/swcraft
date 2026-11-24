"""The byte-identity gate: a model without a stub port generates what it always did.

An external component stub may declare ports (name and direction, no ``interface``), and every port may carry an
``observed_interface`` hint. This is the *without* half: for every committed model tree, none of which holds a stub
port, the ARXML at 4.2.2 and 4.3.0 and every file ``swcraft generate diagrams`` writes are byte for byte the record.

The record is ``test_external_stubs_byte_identity.WITHOUT_A_STUB``, reused rather than copied; :data:`RECORD_DIGEST`
makes any re-measurement of a row fail here. **This is a record, not a golden to refresh:** a change that moves a
digest changes output for a model without a stub port and carries its own ``Qualification Impact``. The ARXML is
compared after ``termination_strip_support.strip_recorded`` removes each component's
``HANDLE-TERMINATION-AND-RESTART`` line; the record's RST column is not read.

**What it does not reach:** models beyond the recorded trees (regression evidence, not a proof); a tree a loader
would read differently from ``yaml.safe_load``; which diagram file moved; uncommitted trees; the CLI's generators,
``generate arxml --component``, ``validate`` and the importer; and the *with* half, which the writer's, the
diagrams' and the XSD suites own.
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

#: The commit where the record was first measured.
BRANCH_POINT = "4ade64d0"

#: One SHA-256 over ``WITHOUT_A_STUB`` -- canonical JSON, keys sorted, rows as lists
#: (:func:`_record_digest`). It moves only when a row of the record is re-measured, and each such
#: move must show that no cell of a tree without a stub port moved for a stub-port reason.
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


def _field_name(field_id: str) -> str:
    """A field's YAML key, read off the contract rather than spelled a second time."""
    for layer in load_contract()["layers"]:
        for kind in layer["kinds"]:
            for field in kind["fields"]:
                if field["id"] == field_id:
                    return field["name"]
    raise AssertionError(f"the contract declares no {field_id}")


def _swc_directory() -> str:
    (layer,) = [layer for layer in load_contract()["layers"] if layer["id"] == "layer:swc"]
    return layer["storage"]["directory"]


def _stub_port_shapes(model_root: Path) -> list[str]:
    """Every stub-port shape in *model_root*'s component files, as ``<storage name>:<port>:<shape>``.

    A port under an external component, a port without an ``interface`` key, and a port carrying an
    ``observed_interface`` key.
    """
    external = _field_name("field:swc.swc.external")
    ports = _field_name("field:swc.swc.ports")
    interface = _field_name("field:swc.port.interface")
    observed = _field_name("field:swc.port.observed_interface")
    found: list[str] = []
    for path in sorted((model_root / _swc_directory()).rglob("*.yaml"), key=lambda p: p.as_posix()):
        element = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        storage = path.relative_to(model_root).as_posix()
        for port in element.get(ports) or []:
            name = port.get("name")
            if element.get(external) is True:
                found.append(f"{storage}:{name}:on an external owner")
            if port.get(interface) is None:
                found.append(f"{storage}:{name}:no interface")
            if observed in port:
                found.append(f"{storage}:{name}:an observed interface")
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
def test_no_recorded_tree_holds_a_stub_port(tree: str) -> None:
    """The premise, asserted: these trees hold no stub-port shape.

    A tree that gains one leaves this record (its bytes are the *with* half's subject).
    """
    shapes = _stub_port_shapes(REPO_ROOT / tree)
    assert shapes == [], f"{tree} holds what this phase added: {shapes}"


def test_the_premise_check_sees_each_shape(tmp_path: Path) -> None:
    """The premise check is not vacuous: each of the three shapes in a scratch tree is found."""
    group = tmp_path / _swc_directory() / "external"
    group.mkdir(parents=True)
    (group / "Stub.yaml").write_text(
        "name: Stub\nexternal: true\nports:\n  - name: P\n    direction: provide\n    observed_interface: If\n",
        encoding="utf-8",
        newline="\n",
    )
    assert _stub_port_shapes(tmp_path) == [
        "swc/external/Stub.yaml:P:on an external owner",
        "swc/external/Stub.yaml:P:no interface",
        "swc/external/Stub.yaml:P:an observed interface",
    ]
