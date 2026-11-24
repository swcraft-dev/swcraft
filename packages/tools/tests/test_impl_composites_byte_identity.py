"""The byte-identity gate: a model without an implementation composite generates what it always did.

For every committed model tree the record names, the ARXML at 4.2.2 and at 4.3.0 and every file
``swcraft generate diagrams`` writes are byte for byte what the record holds. The record is
``test_external_stubs_byte_identity``'s ``WITHOUT_A_STUB``, reused rather than copied; :data:`RECORD_DIGEST`
pins it, so a change to any of its rows fails here and has to say whether this module's claim moved.

**One recorded tree declares the new kinds.** ``recorded_oracles/impact_model`` holds a three-type chain (an
``implementation_record``, an ``implementation_array`` of it and a ``pointer`` to that) and stays in the record
because its ARXML is a recorded refusal (an unmapped application type, raised by the writer's
``_check_application_type_mappings`` before a data type is written; the message is matched), and its diagrams
read no data type. :func:`test_only_the_impact_model_declares_the_new_kinds` pins it as the only one.

**This is a record, not a golden to refresh.** A change that moves a digest is a change to ARXML or diagram
output for a recorded model, and it carries its own ``Qualification Impact`` section. The ARXML bytes are
compared after ``termination_strip_support.strip_recorded``.

What it does not reach: a model outside the recorded trees (regression evidence, not a proof); a tree whose
YAML the loader reads differently from the premise's ``data_types`` scan; what the writer would emit for
``impact_model``'s chain; which diagram file moved; the CLI, ``generate arxml --component``, ``validate`` and the
importer; the *with* half, which is the writer's and the XSD suites' to witness.
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

#: The commit the record was first measured at.
BRANCH_POINT = "f267962c"

#: One SHA-256 over ``WITHOUT_A_STUB`` -- canonical JSON, keys sorted, rows as lists
#: (:func:`_record_digest`). It moves whenever any cell of the record moves.
RECORD_DIGEST = "08149e87f6e8607f251a2187446a1dbb785d502c39a995662f419b3de061bbb5"

#: The new kinds a data-type file can declare. The member kind ``implementation_element`` has no
#: file of its own: it lives inside an ``implementation_record``.
NEW_KINDS = (
    "kind:implementation_types.implementation_record",
    "kind:implementation_types.implementation_array",
    "kind:implementation_types.pointer",
)

#: The one recorded tree that declares them, and exactly what it declares.
IMPACT_MODEL = "packages/tools/tests/fixtures/recorded_oracles/impact_model"
IMPACT_MODEL_SHAPES = [
    "data_types/implementation/SpeedFrame.yaml:implementation_record",
    "data_types/implementation/SpeedFrames.yaml:implementation_array",
    "data_types/implementation/SpeedFramesPtr.yaml:pointer",
]

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


def _data_types_layer() -> dict:
    (layer,) = [layer for layer in load_contract()["layers"] if layer["id"] == "layer:implementation_types"]
    return layer


def _new_kind_discriminators() -> tuple[str, frozenset[str]]:
    """The ``kind`` key's name and the new kinds' discriminator values, read off the contract."""
    key_names: set[str] = set()
    values: set[str] = set()
    for kind in _data_types_layer()["kinds"]:
        if kind["id"] not in NEW_KINDS:
            continue
        field_id = f"field:{kind['id'].removeprefix('kind:')}.kind"
        (field,) = [field for field in kind["fields"] if field["id"] == field_id]
        (value,) = field["enum"]
        key_names.add(field["name"])
        values.add(value)
    assert len(values) == len(NEW_KINDS), "the contract no longer declares every new kind"
    (key_name,) = key_names
    return key_name, frozenset(values)


def _new_kind_shapes(model_root: Path) -> list[str]:
    """Every element of a new kind in *model_root*'s data-type files, read off the files.

    Each is named ``<storage name>:<kind>``.
    """
    key_name, discriminators = _new_kind_discriminators()
    directory = model_root / _data_types_layer()["storage"]["directory"]
    found: list[str] = []
    for path in sorted(directory.rglob("*.yaml"), key=lambda p: p.as_posix()):
        element = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        kind = element.get(key_name)
        if kind in discriminators:
            found.append(f"{path.relative_to(model_root).as_posix()}:{kind}")
    return found


def test_the_record_is_the_branch_points() -> None:
    """The sibling module's record is still the one :data:`RECORD_DIGEST` pins."""
    assert _record_digest() == RECORD_DIGEST


@pytest.mark.parametrize(("tree", "artifact", "recorded"), _CASES, ids=[f"{t}:{a}" for t, a, _ in _CASES])
def test_the_arxml_is_the_branch_points(tree: str, artifact: str, recorded: str, tmp_path: Path) -> None:
    model_root = REPO_ROOT / tree
    assert model_root.is_dir(), f"{tree} is recorded but not committed"
    output = tmp_path / artifact
    if recorded.startswith("refused: "):
        with pytest.raises(ValueError, match=recorded.removeprefix("refused: ")):
            _generate(model_root, artifact, output)
        assert not output.exists(), f"{tree}: a refused {artifact} left a file behind"
        return
    _generate(model_root, artifact, output)
    assert _sha(strip_recorded(tree, artifact, output.read_bytes())) == recorded


@pytest.mark.parametrize("tree", sorted(WITHOUT_A_STUB))
def test_every_diagram_file_is_the_branch_points(tree: str, tmp_path: Path) -> None:
    count, digest = _diagram_digest(REPO_ROOT / tree, tmp_path / "views")
    assert (count, digest) == WITHOUT_A_STUB[tree][3:]


@pytest.mark.parametrize("tree", sorted(WITHOUT_A_STUB))
def test_only_the_impact_model_declares_the_new_kinds(tree: str) -> None:
    """Exactly one recorded tree, ``impact_model``, holds a new kind: its three-type chain.

    A tree that gains a new kind leaves this record -- its bytes are the *with* half's subject.
    """
    shapes = _new_kind_shapes(REPO_ROOT / tree)
    expected = IMPACT_MODEL_SHAPES if tree == IMPACT_MODEL else []
    assert shapes == expected, f"{tree} holds {shapes}, the record expects {expected}"


def test_the_impact_model_arxml_stays_a_refusal() -> None:
    """The exception's reason is itself recorded: ``impact_model``'s ARXML never generates."""
    assert IMPACT_MODEL in WITHOUT_A_STUB
    for recorded in WITHOUT_A_STUB[IMPACT_MODEL][:2]:
        assert recorded.startswith("refused: "), recorded


def test_the_premise_check_sees_each_new_kind(tmp_path: Path) -> None:
    """The premise check is not vacuous: each new kind in a scratch tree is found, and no old one."""
    directory = tmp_path / _data_types_layer()["storage"]["directory"]
    directory.mkdir(parents=True)
    files = {
        "Frame.yaml": "name: Frame\nkind: implementation_record\nelements:\n  - name: a\n    type: uint8\n",
        "Frames.yaml": "name: Frames\nkind: implementation_array\nelement_type: Frame\nlength: 2\n",
        "FramesPtr.yaml": "name: FramesPtr\nkind: pointer\ntarget: Frames\n",
        "Rec.yaml": "name: Rec\nkind: record\nfields:\n  - name: a\n    type: uint8\n",
        "uint8.yaml": "name: uint8\nkind: value_type\nbase: uint8_base\n",
    }
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8", newline="\n")
    assert _new_kind_shapes(tmp_path) == [
        "data_types/implementation/Frame.yaml:implementation_record",
        "data_types/implementation/Frames.yaml:implementation_array",
        "data_types/implementation/FramesPtr.yaml:pointer",
    ]
