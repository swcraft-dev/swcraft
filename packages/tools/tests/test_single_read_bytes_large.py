"""The shipped sequences in one ``single_read`` block on the large bench (marked ``slow``).

The twin of ``test_single_read_bytes.py``, whose steps, sequences and comparison it reuses, on
``conftest.py``'s session ``large_bench``: the composer, the ARXML, the diagrams and the `needs.json` in one
block against the same steps with no block, byte for byte, on a model the committed trees do not
resemble. Marked ``slow`` because the bench and its layout are.

What this does not reach: the bench's model alone, and what the twin's docstring states. Each
generator alone in a block is not repeated here, since a sequence holds it behind the composer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_single_read_bytes import (
    SEQUENCES,
    Outcome,
    assert_same_outcomes,
    baseline_of,
    node_for,
    run_in_a_block,
)

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def no_block() -> dict[str, Outcome]:
    """The module's no-block outcomes, by step."""
    return {}


@pytest.fixture
def node(sequence: str) -> str | None:
    return node_for(SEQUENCES[sequence])


@pytest.fixture
def baseline(
    sequence: str,
    large_bench: Path,
    node: str | None,
    no_block: dict[str, Outcome],
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Outcome]:
    """The sequence's steps with no block, each built once; no step of the bench may be a refusal."""
    return baseline_of(no_block, SEQUENCES[sequence], large_bench / "model", tmp_path_factory.mktemp("no-block"), node)


@pytest.mark.parametrize("sequence", sorted(SEQUENCES))
def test_a_sequence_in_one_block_changes_no_outcome(
    sequence: str, large_bench: Path, node: str | None, baseline: dict[str, Outcome], tmp_path: Path
) -> None:
    assert_same_outcomes(run_in_a_block(SEQUENCES[sequence], large_bench / "model", tmp_path, node), baseline)
