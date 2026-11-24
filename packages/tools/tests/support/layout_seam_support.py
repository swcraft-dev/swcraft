"""The stand-in a diagram test patches over every step of the layout bridge that may start a process.

Used by ``test_diagram_layout.py`` (the bridge alone) and ``test_diagram_generation.py`` (the
generator around it).

What it does not reach: a process started through another module than
``swcraft_tools.writer.diagrams.layout``; the patch is the caller's, on the bridge's seams.
"""

from __future__ import annotations


def refuse_to_spawn(*_args: object, **_kwargs: object) -> None:
    """Fail the test: a step that would start a process (or look for what it starts) was reached."""
    raise AssertionError("no process may be spawned")
