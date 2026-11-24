"""The ``deployment`` view: processor, cores, partitions, tasks and components as boxes in boxes.

The processor is outermost; inside it every core with its declared ASIL capability; inside each core
its partitions with their level (and ``trusted`` when trusted); inside each partition the tasks that
name it; inside each task, as leaves, the components it runs with their effective ASIL. An unrated
element or a ``QM`` level carries no suffix; a partition no task names is an empty box. The tree is
the extractor's, a ``graph.NestingNode`` whose ``title`` holds the level text.

It is drawn by ``nesting.render_nesting`` with this view's id, a fill per kind and the leaf's title
as its label suffix, so no layout engine and no Node.js is needed.

What it does not reach: memories, a partition's ``coexistence`` argument, whether a placement is
safe (``check-model``'s findings, never a colour here), and a component's place in a composition.
"""

from __future__ import annotations

from collections.abc import Mapping

from swcraft_tools.writer.diagrams.nesting import NestingNodeLike, render_nesting
from swcraft_tools.writer.diagrams.svg import COMPONENT_FILL, COMPOSITION_FILL, ModelEntry

#: The view id and file stem; equal to ``graph.DEPLOYMENT_VIEW`` but not imported, since a renderer
#: imports nothing that reads the model.
VIEW = "deployment"

#: A task's fill: pale green, deliberately not ``sequence.TASK_FILL``.
DEPLOYMENT_TASK_FILL = "#e8f5e9"

#: A partition's fill: pale purple, so the level between a core and its tasks reads as its own box.
DEPLOYMENT_PARTITION_FILL = "#f3e5f5"

#: The fill of every kind below the processor, which takes the frame's.
FILLS: Mapping[str, str] = {
    "core": COMPOSITION_FILL,
    "partition": DEPLOYMENT_PARTITION_FILL,
    "task": DEPLOYMENT_TASK_FILL,
    "component": COMPONENT_FILL,
}

#: The one kind drawn as a leaf.
LEAVES: frozenset[str] = frozenset({"component"})


def render_deployment(root: NestingNodeLike, *, sources: list[ModelEntry] | None = None) -> str:
    """The SVG of the ``deployment`` view under *root*, the processor; *sources* as ``render_nesting``'s."""
    return render_nesting(root, view=VIEW, fills=FILLS, leaves=LEAVES, leaf_titles=True, sources=sources)
