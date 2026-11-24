"""The two read bounds a host and the profiles are told are the protocol's constants.

``read``'s published entry in ``tools/list`` carries ``READ_BATCH_MAX`` as digits in the description of
the ``addresses`` argument, the argument it bounds, and ``MODEL_MAP_LIST_MAX`` as digits in the tool's
description, where the model map (the no-address answer, which no argument carries) is described. The
agent profiles, the Copilot instructions and the standalone agent's prompt state the map bound in one
phrase each.

What this does not reach: the sentence a figure stands in (an unrelated ``50`` in the tool description
would pass the map check), a figure restated anywhere else, such as a guide page, and for the profiles
any wording of the bound other than the one phrase searched for.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from conftest import REPO_ROOT
from protocol_client import list_tools
from swcraft_tools.editing.protocol import MODEL_MAP_LIST_MAX, READ_BATCH_MAX

#: The number words the profiles spell a bound in.
_WORDS = {50: "fifty"}

#: Every site that tells a model the map's bound, by repository path.
MAP_BOUND_SITES = (
    "examples/basic/.github/copilot-instructions.md",
    "examples/basic/.github/agents/swcraft-architect.agent.md",
    "packages/tools/src/swcraft_tools/templates/vehicle-architecture/.github/copilot-instructions.md",
    "packages/tools/src/swcraft_tools/templates/vehicle-architecture/.github/agents/swcraft-architect.agent.md",
    "vscode_extension/fixtures/agents/copilot-instructions.md",
    "vscode_extension/fixtures/agents/swcraft-architect.agent.md",
    "packages/agent/src/swcraft_agent/agent-system-prompt.md",
)


def _flat(text: str) -> str:
    """*text* with every run of whitespace one space, so a phrase wrapped across lines is found."""
    return re.sub(r"\s+", " ", text)


def _holds(text: str, figure: int) -> bool:
    """Whether *text* carries *figure* as a whole number (not part of a longer one; a full stop may follow)."""
    return re.search(rf"(?<![\d.]){figure}(?!\d|\.\d)", text) is not None


def _published_read() -> tuple[str, dict[str, Any]]:
    """``read``'s description and argument properties, as ``tools/list`` publishes them."""
    (tool,) = [tool for tool in list_tools() if tool.name == "read"]
    return tool.description or "", tool.inputSchema["properties"]


def batch_bound_stated(properties: dict[str, Any]) -> bool:
    """Whether the ``addresses`` argument's description carries the batch bound."""
    return _holds(properties.get("addresses", {}).get("description", ""), READ_BATCH_MAX)


def map_bound_stated(description: str) -> bool:
    """Whether the tool's description carries the model map's bound."""
    return _holds(description, MODEL_MAP_LIST_MAX)


def test_the_two_bounds_are_distinct_figures() -> None:
    """A swap of the two figures could pass only if they were equal."""
    assert READ_BATCH_MAX != MODEL_MAP_LIST_MAX


def test_the_read_tool_states_the_batch_bound_on_its_argument() -> None:
    _, properties = _published_read()
    assert batch_bound_stated(properties)


def test_the_read_tool_states_the_map_bound_in_its_description() -> None:
    description, _ = _published_read()
    assert map_bound_stated(description)


def _addresses(description: str) -> dict[str, Any]:
    return {"addresses": {"description": description}}


class TestTheChecksFire:
    """Each check refuses a planted publication that misplaces its figure."""

    def test_a_missing_figure_is_refused(self) -> None:
        assert not batch_bound_stated(_addresses("several addresses"))
        assert not map_bound_stated("the model map")

    def test_swapped_figures_are_refused(self) -> None:
        assert not batch_bound_stated(_addresses(f"up to {MODEL_MAP_LIST_MAX} addresses"))
        assert not map_bound_stated(f"layers holding at most {READ_BATCH_MAX}")

    def test_the_batch_figure_only_in_the_description_is_refused(self) -> None:
        description = f"at most {MODEL_MAP_LIST_MAX} elements; up to {READ_BATCH_MAX} addresses"
        assert not batch_bound_stated(_addresses("several addresses"))
        assert map_bound_stated(description)

    def test_a_longer_number_holding_the_figure_is_not_the_figure(self) -> None:
        assert not _holds(f"{READ_BATCH_MAX}0 addresses", READ_BATCH_MAX)
        assert not _holds(f"{READ_BATCH_MAX}.5 addresses", READ_BATCH_MAX)

    def test_a_figure_ending_a_sentence_is_the_figure(self) -> None:
        assert _holds(f"at most {READ_BATCH_MAX}.", READ_BATCH_MAX)


@pytest.mark.parametrize("site", MAP_BOUND_SITES)
def test_every_profile_and_instruction_site_states_the_map_bound(site: str) -> None:
    text = _flat((REPO_ROOT / site).read_text(encoding="utf-8"))
    assert f"holding at most {_WORDS[MODEL_MAP_LIST_MAX]}" in text, site
