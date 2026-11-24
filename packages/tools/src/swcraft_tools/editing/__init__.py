"""The deterministic transactional model editor.

The provider-independent core of the editing protocol: one derived description
of the model (:mod:`swcraft_tools.editing.registry`) and, on top of it, the
change-set lifecycle. Nothing here is an MCP tool.

The package holds **no** layer knowledge outside the registry, which is the
single source of truth for it.

Nothing in this package is imported by ``writer/``: the dependency arrow points
one way (``editing`` -> ``writer``), or the two packages would form a cycle.
"""

from __future__ import annotations
