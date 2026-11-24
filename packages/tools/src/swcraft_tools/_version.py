"""Single source of truth for the SWCraft version.

Every other version site derives from this one: ``packages/tools/pyproject.toml`` reads it through
hatchling's dynamic version; the ``mcp`` and ``agent`` projects and the specimen generator restate it
and pin their inter-package dependencies at ``== __version__``; ``vscode_extension/package.json`` and
``package-lock.json`` carry the same string. The four Python distributions and the extension move in
lockstep.
"""

from __future__ import annotations

__version__ = "0.24.0"

__all__ = ["__version__"]
