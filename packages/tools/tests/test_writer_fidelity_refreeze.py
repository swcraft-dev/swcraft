"""The writer-fidelity re-freeze is gated, not trusted: every moved byte is one the harness reverses.

The writer-fidelity changes (the ``DESC`` and ``LONG-NAME``, the composite member's ``CATEGORY``,
the compu method's ``UNIT-REF``, the value type's range, the ``FILTER``, the two added com-spec
attributes, the argument policy, the four behavioural values, the port API options no longer
written, every data type, generated form and mapping set in its layer's package under
``/DataTypes``, an init value in the form its type asks for) move the goldens and every committed
tree's output. The digests below were measured before any of them. ``test_arxml_golden.py`` holds
each golden equal to the writer's output, and this module proves that
``fidelity_strip_support.strip_fidelity`` maps each golden, and each committed tree's output at
both versions, back to its digest here: a golden may move only by what a reversal undoes.

The goldens' digests include the termination handling lines, so this module applies the fidelity
strip alone; the byte-identity records, whose digests predate both, chain the two through
``termination_strip_support.strip_recorded``.

What it does not reach: it is evidence over the committed trees, not a proof over every model. It
says which bytes moved and that the harness reverses them, never that the new bytes are right
(that is each writer change's own standard citation), and no golden is promoted to an oracle.
``test_fidelity_strip_support.py`` holds the strip's identity on the unchanged bytes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import GOLDEN_MODEL_ROOTS, REPO_ROOT
from fidelity_strip_support import strip_fidelity
from swcraft_tools.writer import generate_arxml
from termination_strip_support import TERMINATION_ADDED
from test_diagram_switch_byte_identity import RECORDED

GOLDEN_DIR = REPO_ROOT / "packages/tools/tests/fixtures/arxml_golden"

#: The SHA-256 each golden had before any writer-fidelity change (measured at the harness's head).
PRE_CHANGE: dict[str, str] = {
    "basic-4.2.2": "6f88354d6a7c1b6261f721bb36e7d05598733056b052deb1696932dbe41d407b",
    "composite-4.2.2": "8dbcbbc8cfbac5e12f683fca94b9a76ad91cb86ca0450774a07734751e52a43c",
    "empty-4.2.2": "5ffcd165c2de3d0632d55e0cf4e307e785dda73a84e2ff8b6221b7f4565560ba",
}

#: The SHA-256 of each committed tree's ARXML, 4.2.2 then 4.3.0, measured at the same head.
BRANCH_POINT: dict[str, tuple[str, str]] = {
    "examples/basic/model": (
        "6f88354d6a7c1b6261f721bb36e7d05598733056b052deb1696932dbe41d407b",
        "303f8313e9d6363d9fa9e6463939c1e4dc5030ddd84062bb972427832b256ffa",
    ),
    "packages/tools/src/swcraft_tools/templates/vehicle-architecture/model": (
        "fd4e91236a437658b937f47a678c090d97f7758bae2e69e3b4aaecafe75b7a97",
        "f6546472bfe78f33dd2d329102a0c79f3b74b41b324c596c29f86cac54167d23",
    ),
    "packages/tools/tests/fixtures/access_points/model": (
        "4df606abf1b8e8a76fce49668c75c6693a281ee6d11524da172e0960d56d04b9",
        "f9909d3f6dfdc883f0a4b16fce030a8973a3d4d5786fe54366b6a67aaaa7e82f",
    ),
    "packages/tools/tests/fixtures/compatible_interfaces/model": (
        "7d62b7f8398eda168ebf86d8a5d7bff3a889ec941ca39010fec14316a68a1df3",
        "72486ee6bd90506d02ae7e040cf49bb47019b96fc86be091efc425e12949acc1",
    ),
    "packages/tools/tests/fixtures/component_exchange/model": (
        "8dbcbbc8cfbac5e12f683fca94b9a76ad91cb86ca0450774a07734751e52a43c",
        "d98f3a5a6aa67c1e5f9f3d36b3c7608fc94aa386cb7981187e3ed067344ea379",
    ),
    # Measured with the stub's `external_ref` two package names shorter than at the others' head;
    # only that path's bytes differ.
    "packages/tools/tests/fixtures/compositions/model": (
        "892a14bd4939d7faf56f4317ef5d068da14fa04511d3918865dcc7d6d2d077c8",
        "676d71487996a0319adf5821b76d25d7d4a55bcd976175fcec1e9dd8b48fa196",
    ),
    "packages/tools/tests/fixtures/diagrams/model": (
        "3b2b28097e401ac70de2c3c152329e320ecca15edb58145bf0f9ba5104ae0e13",
        "6b780bda633071be2044c6e3be5376d4fe451ff91478db7b7c9700a8b71721e4",
    ),
    "packages/tools/tests/fixtures/hardware/model": (
        "79895279084e749cb3dd30e783d8af2ab5c819278af4944f79a071cfa30a74fc",
        "ed8d70f414650208cebdb43104ca24814867daa41f60111f51d5fb166363a3b7",
    ),
    "packages/tools/tests/fixtures/init_values/model": (
        "1b9f9ef1a84ea151b867c8f3112d044f0a1a03df627de89d24898b726109e4e0",
        "c0a7d7e4420d34d4ae8c75b71d9dee717aa06386f0ea8582fe24e0f5dd5feffe",
    ),
    "packages/tools/tests/fixtures/interface_compatibility/model": (
        "468f2c8279f1712294f0df2d036f10312c370a4d8954e6fc30d490c6143f34c2",
        "656a69537224df4387674c87887427c63aa86a080abfec9a112681335ea6b156",
    ),
    "packages/tools/tests/fixtures/operation_events/model": (
        "f37a6ed630b016d9d8ef13a4437e6ebe99be4ea95ac75bb4d797de9903fdf35e",
        "d0149acd119fdd46727dc578a527f449f22154e8c718ba334014e507737c32a3",
    ),
    "packages/tools/tests/fixtures/safety/model": (
        "e4d0d2309b22781bd9e6fe75695a572105fdb4c05bd3d950745afcb5cb728305",
        "7bb35a9b3b44325ea68a3c627ede965a755f1c8adabcb534fb75fa2cd1eb0412",
    ),
    "packages/tools/tests/fixtures/showcase/model": (
        "d43003e625615da7271ac9aec98fd7221d9499be27dab089373a3a402eb446c6",
        "c22b7e285f9ad0aa0fcfb52d0e5e607e78397a7e57f2fcf89c010b9565609a8c",
    ),
}

#: The one tree the two records name whose ARXML is a recorded refusal.
_REFUSED = "packages/tools/tests/fixtures/recorded_oracles/impact_model"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("golden", sorted(PRE_CHANGE))
def test_the_golden_strips_to_its_pre_change_digest(golden: str) -> None:
    assert _sha(strip_fidelity((GOLDEN_DIR / f"{golden}.arxml").read_bytes())) == PRE_CHANGE[golden]


def test_every_golden_is_recorded() -> None:
    assert {path.stem for path in GOLDEN_DIR.glob("*.arxml")} == set(PRE_CHANGE)


@pytest.mark.parametrize("golden", sorted(GOLDEN_MODEL_ROOTS))
def test_a_populated_goldens_record_is_its_trees(golden: str) -> None:
    """A populated golden is its tree's 4.2.2 output, so the two records must agree."""
    tree = Path(GOLDEN_MODEL_ROOTS[golden]).resolve().relative_to(REPO_ROOT).as_posix()
    assert BRANCH_POINT[tree][0] == PRE_CHANGE[golden]


VERSIONS = ("4.2.2", "4.3.0")

#: Every ``(tree, version)`` pair :data:`BRANCH_POINT` records, for parametrisation.
CASES: list[tuple[str, str]] = [(tree, version) for tree in sorted(BRANCH_POINT) for version in VERSIONS]


@pytest.mark.parametrize(("tree", "version"), CASES, ids=[f"{t}:{v}" for t, v in CASES])
def test_a_committed_trees_output_strips_to_its_branch_point(tree: str, version: str, tmp_path: Path) -> None:
    output = tmp_path / "model.arxml"
    generate_arxml(str(REPO_ROOT / tree), str(output), version=version)
    assert _sha(strip_fidelity(output.read_bytes())) == BRANCH_POINT[tree][VERSIONS.index(version)]


def test_every_recorded_tree_is_here() -> None:
    """The trees are those the two neighbouring records name, less the recorded refusal, at both versions.

    ``test_diagram_switch_byte_identity.RECORDED`` holds itself to every committed model tree with
    a composition, and ``TERMINATION_ADDED`` names every tree the termination re-freeze reached
    (``operation_events`` has no composition, so only the second names it).
    """
    trees = {tree for tree, (arxml, _, _) in RECORDED.items() if not arxml.startswith("refused: ")}
    trees |= set(TERMINATION_ADDED) - {_REFUSED}
    assert set(BRANCH_POINT) == trees


# --- The golden README's record of this re-freeze is held to the measurement by a closed test.
