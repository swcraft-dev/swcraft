"""``dependent_removals`` called directly: which removals the flag applies to.

The protocol's envelope admits ``with_dependents`` only on an element address,
so these cases reach the function from a Python caller alone.
"""

from __future__ import annotations

from pathlib import Path

import editing_vector_fixtures as fixtures
import pytest
from swcraft_tools.editing.address import Address
from swcraft_tools.editing.dependents import DependentsNotApplicable, dependent_removals
from swcraft_tools.editing.protocol import RemoveOperation
from swcraft_tools.snapshot import ModelSnapshot, load_snapshot


@pytest.fixture
def base(tmp_path: Path) -> ModelSnapshot:
    """The plain ``examples/basic`` baseline."""
    return load_snapshot(str(fixtures.build_fixture(tmp_path / "base", "read-swc-element") / "model"))


def test_the_flag_on_the_whole_swc_layer_is_refused(base: ModelSnapshot) -> None:
    """A layer address names no component, so there is nothing whose referrers could be removed."""
    layer = Address(layer="swc")
    with pytest.raises(DependentsNotApplicable) as refused:
        dependent_removals(base, [RemoveOperation(layer, with_dependents=True)])
    assert refused.value.address == layer
