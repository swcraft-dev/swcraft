"""The Node.js precondition of the diagram layout tests, and its skip/fail policy.

Not in ``conftest.py``: :func:`requires_node` calls ``pytest.skip``, and
``conftest.py`` is scanned by ``no_skip_offenders()``.

``conftest.py`` owns the ``--skip-node`` pytest option (pytest only reads
``pytest_addoption`` from an initial conftest) and hands its value here;
``python scripts/dev.py --skip-node test`` is what passes it, to the ``tools`` and ``scripts`` steps.

**Why a failure and not a skip.** Every diagram's layout goes through one Node
process (``swcraft_tools.writer.diagrams.layout``); a skip on a checkout that was
supposed to have Node would report nothing, so the states are separated:

* Node **resolves** -> the test runs, whatever the flag says;
* Node does **not** resolve and ``--skip-node`` was passed -> skip, naming the flag;
* Node does **not** resolve and the flag was not passed -> **fail**, naming ``node``.

The probe is the product's own resolution, :func:`layout.resolve_node` -- the
installation's Node beside the interpreter first, ``PATH`` second -- asked **once per
session**, so the tests exercise the Node the generator would pick and a missing
Node is diagnosed once rather than per case.

What it does not reach: whether the Node it found is above the bridge's floor. A Node
below it resolves, the test runs, and the layout's own refusal -- naming both
versions -- is the failure the run reports.
"""

from __future__ import annotations

import functools

import pytest
from swcraft_tools.writer.diagrams import layout

#: Set from ``--skip-node`` by ``conftest.pytest_configure``.
SKIP_NODE = False


def set_skip_node(skip: bool) -> None:
    """Set :data:`SKIP_NODE`; called once, from ``pytest_configure``."""
    global SKIP_NODE  # noqa: PLW0603 — one process-wide flag, set once from the CLI
    SKIP_NODE = skip


@functools.cache
def _probe() -> tuple[str | None, str]:
    """The resolved Node, or ``None`` with the resolution's own refusal text."""
    try:
        return layout.resolve_node(), ""
    except layout.DiagramLayoutError as error:
        return None, str(error)


def requires_node() -> str:
    """The Node the layout would run, skipping -- or **failing** -- when there is none.

    Called from a fixture or a test body rather than from a ``skipif`` marker, for
    ``xsd_support.require_licensed_schema``'s reason: a marker is evaluated at
    collection time and could never turn the skip into a failure.
    """
    node, refusal = _probe()
    if node is not None:
        return node
    if SKIP_NODE:
        pytest.skip(f"--skip-node was passed and this host has no Node.js for the diagram layout: {refusal}")
    pytest.fail(
        f"this test needs Node.js and none was found: {refusal} This checkout did not pass --skip-node, "
        "so a missing node is a failure and not an absence."
    )
    raise AssertionError("unreachable")  # pragma: no cover — pytest.fail raises
