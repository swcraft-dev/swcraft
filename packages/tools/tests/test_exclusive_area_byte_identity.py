"""The byte-identity gate: a model without an exclusive area generates what it always did.

For every recorded tree, none declaring an exclusive area (``kind:swc.exclusive_area``) or a reference
to one (``kind:swc.can_enter_area``, ``kind:swc.runs_inside_area``), the ARXML and the diagrams are byte
for byte the record.

**One record, not a copy.** The record is ``test_external_stubs_byte_identity``'s ``WITHOUT_A_STUB``;
:data:`RECORD_DIGEST` pins it, so a re-measurement of any of its rows fails here and must say whether
this claim moved. The premise test reads every recorded tree's component files for the three keys, their
names read off the contract. The tree in :data:`DECLARES_A_VARIABLE` has its ARXML cells left out; its
diagrams are compared.

**A record, not a golden to refresh.** A change that moves a compared digest changes output for a model
without an area and carries its own ``Qualification Impact`` section. The termination-handling lines are
removed before hashing (``termination_strip_support.strip_recorded``).

What it does not reach: models beyond the recorded trees (regression evidence, not a proof); a tree the
loader would read differently from the premise's key scan; which file moved (the aggregate diagram digest
names the tree); the CLI, ``generate arxml --component``, ``validate`` and the importer; the *with* half,
which is the writer's and the XSD suites' to witness.
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

#: The commit the record was measured at, before exclusive areas existed.
BRANCH_POINT = "7e52b89ac"

#: One SHA-256 over ``WITHOUT_A_STUB`` (:func:`_record_digest`: canonical JSON, keys
#: sorted, rows as lists), so the record stays in one place. Any cell of that record
#: moving, or a key renamed, moves it; it is re-pinned in that change, with the reason.
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


def _field_name(field_id: str) -> str:
    """A field's YAML name, read off the contract rather than spelled a second time."""
    for layer in load_contract()["layers"]:
        for kind in layer["kinds"]:
            for field in kind["fields"]:
                if field["id"] == field_id:
                    return field["name"]
    raise AssertionError(f"the contract declares no {field_id}")


def _swc_directory() -> str:
    (layer,) = [layer for layer in load_contract()["layers"] if layer["id"] == "layer:swc"]
    return layer["storage"]["directory"]


def _area_shapes(model_root: Path) -> list[str]:
    """Every area or area-reference key in *model_root*'s component files, as ``<storage name>[:<runnable>]:<key>``."""
    areas = _field_name("field:swc.swc.exclusive_areas")
    runnables = _field_name("field:swc.swc.runnables")
    references = (_field_name("field:swc.runnable.can_enter"), _field_name("field:swc.runnable.runs_inside"))
    found: list[str] = []
    for path in sorted((model_root / _swc_directory()).rglob("*.yaml"), key=lambda p: p.as_posix()):
        element = load_yaml_document(path.read_text(encoding="utf-8")) or {}
        storage = path.relative_to(model_root).as_posix()
        if areas in element:
            found.append(f"{storage}:{areas}")
        for runnable in element.get(runnables) or []:
            found.extend(f"{storage}:{runnable.get('name')}:{key}" for key in references if key in runnable)
    return found


def test_the_record_is_the_branch_points() -> None:
    """The sibling module's record is still the one this claim was measured against."""
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


def test_no_recorded_tree_declares_an_area_or_a_reference() -> None:
    """No recorded tree declares an area or a reference; a tree that gains one fails here rather than staying in."""
    declaring = {tree: shapes for tree in WITHOUT_A_STUB if (shapes := _area_shapes(REPO_ROOT / tree))}
    assert declaring == {}


def test_the_premise_check_sees_each_shape(tmp_path: Path) -> None:
    """The premise check is not vacuous: each of the three shapes in a scratch tree is found."""
    group = tmp_path / _swc_directory() / "g"
    group.mkdir(parents=True)
    (group / "Owner.yaml").write_text(
        "name: Owner\nexclusive_areas:\n  - name: A\n"
        "runnables:\n  - name: Run\n    can_enter:\n      - area: A\n"
        "    runs_inside:\n      - area: A\n",
        encoding="utf-8",
        newline="\n",
    )
    assert _area_shapes(tmp_path) == [
        "swc/g/Owner.yaml:exclusive_areas",
        "swc/g/Owner.yaml:Run:can_enter",
        "swc/g/Owner.yaml:Run:runs_inside",
    ]
