"""Compositions: the chain and where the components live.

C = `layers.compositions` (1 when unstated). The root `Root` and C - 1 nested compositions `Cmp...`
form a **chain**: the root's members are the **entered** components (the layout's root members:
every component a connector end reaches, and, with R_c > 0, any whose ports are all unconnected)
and, when C > 1, the first nested composition; each nested composition's one composition member is
the next in the chain; the deepest composition's members are the **connector-free** components.
With C = 1 every component is a root member, the entered ones first. A nested composition's member
is named as the composition itself; a component member is `M0001`, ... within its composition.
Either end may hold no component: SWCraft requires no composition to have a member. Connectors
stay in the root between root members.

A chain gives the delegation chains the most depth and is the one deterministic shape that needs
no further choice.

The refusal before generation: `layer-total` at `layers.compositions` for a count of 0, since
SWCraft needs one root.

What this does not reach: a real model's tree depth and members per composition are not passport
leaves, so nothing here meets them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import GenerationError
from ..model import Composition, CompositionMember
from ..passport import Refusal
from .vocabulary import LAYER_COMPOSITIONS, LAYER_TOTAL


def composition_count(stated: Mapping[str, Any]) -> int:
    """C, the compositions a stated passport asks for: one root when the leaf is unstated."""
    return int(stated.get(LAYER_COMPOSITIONS, 1))


def composition_refusals(stated: Mapping[str, Any]) -> list[Refusal]:
    """The refusal of a stated `layers.compositions` of 0: SWCraft needs one root."""
    if composition_count(stated) == 0:
        return [Refusal(LAYER_COMPOSITIONS, LAYER_TOTAL, "states 0; SWCraft needs one root composition")]
    return []


def _component_members(components: Sequence[str]) -> list[CompositionMember]:
    return [CompositionMember(f"M{index:04d}", name) for index, name in enumerate(components, start=1)]


def build_compositions(
    names: Sequence[str], entered: Sequence[str], connector_free: Sequence[str]
) -> tuple[Composition, ...]:
    """The chain of `names` (the identifier plan's: `Root` first, then `Cmp...`), root first.

    `entered` and `connector_free` are component names, each in component order: the root holds
    the entered ones, the deepest composition the connector-free ones (with one composition, the
    root holds both, the entered first)."""
    if not names:
        raise GenerationError("a specimen needs one root composition")
    compositions: list[Composition] = []
    for depth, name in enumerate(names):
        components: list[str] = []
        if depth == 0:
            components.extend(entered)
        if depth == len(names) - 1:
            components.extend(connector_free)
        members = _component_members(components)
        if depth + 1 < len(names):
            child = names[depth + 1]
            members.append(CompositionMember(child, None, child))
        compositions.append(Composition(name, name, depth == 0, tuple(members)))
    return tuple(compositions)
