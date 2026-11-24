"""The bytes of every generator, with and without a ``single_read`` block.

A block shares validated elements between its readers, so the one place a consumer's in-place
mutation can reach the writer is a block holding the composer **and** a generator. This module
builds the outcome of each step with no block, then builds it again inside a block, alone and in
the sequences the product ships, and compares byte for byte. An outcome is the bytes, or the
refusal's class and message where the tree has none to give: a refusal must stay the same refusal,
and the refusals are a closed set, :data:`REFUSED`, so a step broken on both sides alike fails.

The trees are ``test_diagram_switch_byte_identity.py``'s ``RECORDED``, the committed model trees.
The large bench's sequences are ``test_single_read_bytes_large.py``, which is ``slow``.

**A sequence ignores the composer's verdict**, as the oracle's build does, where ``validate`` and
``build`` stop at an error finding: a tree the shipped commands would refuse still puts its
elements through the writer behind the composer, which is the stronger comparison.

**What this does not reach.** These are change detectors, not correctness proofs: equal outcomes
say the block changed nothing and never that the bytes are right, which is the oracle's question.
A mutation that moves no byte of these trees is not seen here, and the tripwire covers only what
compares unequal. ``check-model`` is its document, ``_check_model_json`` over the composer, not
the printed lines or the exit code; ``validate`` and ``build`` are the sequences of their calls, not
the commands, whose block is ``test_single_read.py``'s. ``validate``'s ARXML check reads the
generated file and no layer, so it is not a step. Diagrams are compared without
``layout_seconds``, which is a clock and is not in any written file. Only a case naming the
diagrams asks for Node, so ``--skip-node`` skips those cases alone.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from node_support import requires_node
from swcraft_tools.cli.build_commands import _check_model_json
from swcraft_tools.model_state import _SINGLE_READ, single_read
from swcraft_tools.reader import read_interfaces
from swcraft_tools.validator import check_cross_layer_consistency
from swcraft_tools.writer import generate_arxml, generate_diagrams, generate_needs
from test_diagram_switch_byte_identity import _ARTIFACTS, RECORDED

Outcome = bytes | tuple[str, str]
Step = Callable[[Path, Path, str | None], bytes]

PROJECT_NAME = "byte-differential"

#: Each ``(tree, step)`` that may end in a refusal: the ARXML ones ``RECORDED`` states as ``refused: ``,
#: read off the record's leading columns, the ones ``_ARTIFACTS`` names. ``needs`` refuses no committed
#: tree, which :func:`baseline_of` holds: a refusal outside this set fails there.
REFUSED = frozenset(
    (tree, artifact)
    for tree, recorded in RECORDED.items()
    for artifact, digest in zip(_ARTIFACTS, recorded[: len(_ARTIFACTS)], strict=True)
    if digest.startswith("refused: ")
)


def _check_model(model_root: Path, _work: Path, _node: str | None) -> bytes:
    return _check_model_json(check_cross_layer_consistency(str(model_root))).encode("utf-8")


def _arxml(version: str, model_root: Path, work: Path, _node: str | None) -> bytes:
    output = work / "model.arxml"
    generate_arxml(str(model_root), str(output), version=version)
    return output.read_bytes()


def _diagrams(model_root: Path, work: Path, node: str | None) -> bytes:
    assert node is not None, "a case naming the diagrams takes the node fixture"
    views = work / "views"
    generate_diagrams(str(model_root), str(views), PROJECT_NAME, node=node)
    files = sorted((path for path in views.rglob("*") if path.is_file()), key=lambda path: path.as_posix())
    return b"".join(
        path.relative_to(views).as_posix().encode("utf-8") + b"\0" + path.read_bytes() + b"\0" for path in files
    )


def _needs(model_root: Path, work: Path, _node: str | None) -> bytes:
    output = work / "needs.json"
    generate_needs(str(model_root), str(output), PROJECT_NAME)
    return output.read_bytes()


STEPS: dict[str, Step] = {
    "check-model": _check_model,
    "arxml-4.2.2": partial(_arxml, "4.2.2"),
    "arxml-4.3.0": partial(_arxml, "4.3.0"),
    "diagrams": _diagrams,
    "needs": _needs,
}

#: The shipped sequences, at both versions: `validate` is the composer then the ARXML, `build` adds
#: the diagrams and the `needs.json`, in `server.build`'s order.
SEQUENCES: dict[str, tuple[str, ...]] = {
    "validate-4.2.2": ("check-model", "arxml-4.2.2"),
    "validate-4.3.0": ("check-model", "arxml-4.3.0"),
    "build-4.2.2": ("check-model", "arxml-4.2.2", "diagrams", "needs"),
    "build-4.3.0": ("check-model", "arxml-4.3.0", "diagrams", "needs"),
}


def node_for(names: tuple[str, ...]) -> str | None:
    """The Node of a case naming the diagrams, ``None`` for the others: call it from a fixture."""
    return requires_node() if "diagrams" in names else None


def run_steps(names: tuple[str, ...], model_root: Path, work: Path, node: str | None = None) -> dict[str, Outcome]:
    """Each named step's outcome, in order, each writing to a directory of its own under *work*."""
    outcomes: dict[str, Outcome] = {}
    for name in names:
        target = work / name
        target.mkdir(parents=True)
        try:
            outcomes[name] = STEPS[name](model_root, target, node)
        except Exception as error:  # noqa: BLE001 - a refusal is an outcome here
            outcomes[name] = (type(error).__qualname__, str(error))
    return outcomes


def run_in_a_block(names: tuple[str, ...], model_root: Path, work: Path, node: str | None = None) -> dict[str, Outcome]:
    """:func:`run_steps` in one block, held to have been one: an empty memo would prove nothing."""
    with single_read():
        outcomes = run_steps(names, model_root, work, node)
        held = _SINGLE_READ.get()
        assert held, f"{names} read no layer through the block"
    return outcomes


def assert_same_outcomes(built: dict[str, Outcome], baseline: dict[str, Outcome]) -> None:
    """Name the step that moved; a byte comparison's own diff of a whole document says nothing."""
    moved = [name for name, outcome in built.items() if outcome != baseline[name]]
    assert not moved, f"a block changed the outcome of {moved}"


def baseline_of(
    held: dict[str, Outcome],
    names: tuple[str, ...],
    model_root: Path,
    work: Path,
    node: str | None,
    refused: frozenset[str] = frozenset(),
) -> dict[str, Outcome]:
    """*names* with no block open, each step built once into *held*, and only *refused* a refusal.

    Equal outcomes prove nothing of a step that breaks the same way on both sides, so a refusal
    outside the closed set fails here, and so does a stated refusal that no longer happens.
    """
    assert _SINGLE_READ.get() is None
    held.update(run_steps(tuple(name for name in names if name not in held), model_root, work, node))
    assert {name for name in names if isinstance(held[name], tuple)} == refused & set(names), {
        name: held[name] for name in names if isinstance(held[name], tuple)
    }
    return {name: held[name] for name in names}


@pytest.fixture(scope="module")
def no_block() -> dict[str, dict[str, Outcome]]:
    """The module's no-block outcomes, by tree and then by step."""
    return {}


@pytest.fixture
def node(names: tuple[str, ...]) -> str | None:
    return node_for(names)


@pytest.fixture
def baseline(
    tree: str,
    names: tuple[str, ...],
    node: str | None,
    no_block: dict[str, dict[str, Outcome]],
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Outcome]:
    """The steps of this case on *tree* with no block open, each built once per process."""
    refused = frozenset(step for where, step in REFUSED if where == tree)
    work = tmp_path_factory.mktemp("no-block")
    return baseline_of(no_block.setdefault(tree, {}), names, REPO_ROOT / tree, work, node, refused)


_CASES = [(f"alone:{name}", (name,)) for name in STEPS] + list(SEQUENCES.items())


@pytest.mark.parametrize("tree", sorted(RECORDED))
@pytest.mark.parametrize(("case", "names"), _CASES, ids=[case for case, _ in _CASES])
def test_a_block_changes_no_outcome(
    tree: str, case: str, names: tuple[str, ...], node: str | None, baseline: dict[str, Outcome], tmp_path: Path
) -> None:
    assert_same_outcomes(run_in_a_block(names, REPO_ROOT / tree, tmp_path, node), baseline)


def test_a_refusal_is_compared_by_its_class_and_message(tmp_path: Path) -> None:
    """The tree the writer refuses: the outcome is the refusal, and the block does not lift it."""
    root = REPO_ROOT / "packages/tools/tests/fixtures/recorded_oracles/impact_model"
    kind, message = run_in_a_block(("check-model", "arxml-4.2.2"), root, tmp_path)["arxml-4.2.2"]
    assert kind == "ValueError"
    # The first refusal is its variable's unmapped type, `check-model`'s finding on the tree
    # (`test_external_stubs_byte_identity.WITHOUT_A_STUB`'s row says why).
    assert message.startswith("Component(s) speak a physical type no mapping set they name pairs")


def test_the_comparison_sees_a_mutation_of_a_shared_element(tmp_path: Path) -> None:
    """The differential's teeth: an element edited between the composer and the writer moves the bytes.

    The edit is put back before the block closes, so this case asserts what the comparison sees
    and leaves the tripwire's half to ``test_single_read.py``.
    """
    root = REPO_ROOT / "examples/basic/model"
    clean = run_steps(("arxml-4.2.2",), root, tmp_path / "clean")
    with single_read():
        interface = next(element for element in read_interfaces(str(root)) if element.name == "IExample")
        element = interface.data_elements[0]
        original = element.name
        element.name = "mutated"
        try:
            dirty = run_steps(("arxml-4.2.2",), root, tmp_path / "dirty")
        finally:
            element.name = original
    assert dirty != clean
    assert b"mutated" in dirty["arxml-4.2.2"]
