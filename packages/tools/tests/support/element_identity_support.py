"""Read which element a model file declares — the suites' own reader, independent of the product's.

A file's *name* is not its element's identity: a collision gives the file a
``~N`` suffix, and the product finds an element by what its file **declares**,
so a suite asserting where an element landed reads the content too.

**Deliberately not** :func:`swcraft_tools.model_state.declared_identity`. That is
the product's own reader, and the assertions here are about what the product
wrote; checking a write with the very function the write consulted would make a
whole class of defect invisible (both sides reading ``name`` for a layer
identified by ``id``, say). This module reads the mapping directly and asks for
``id`` before ``name``, which is the layer-independent question a test wants.

**What it does not reach.** It parses YAML and nothing else: a file that does not
parse, is not a mapping, or declares neither key is ``None`` rather than an error,
because every caller is asserting about a *tree* and a missing file is the
assertion's own business. It knows no layer, so it cannot tell an element that
genuinely declares ``id`` from one that declares both.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml


def declared_identity(path: Path) -> Any:
    """The identity the YAML file at *path* declares — its ``id``, else its ``name``, else ``None``."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    if not isinstance(document, dict):
        return None
    return document.get("id", document.get("name"))


def declared_tree(model_root: Path, directories: Iterable[str]) -> dict[str, str]:
    """Every ``*.yaml`` under *directories* -> the identity its **content** declares.

    *directories* is any iterable of model-root-relative paths whose first
    segment names a directory to read — a tree's own keys will do. Keys are
    model-root-relative and POSIX-spelled, so a comparison of two trees is
    platform-independent.
    """
    tree: dict[str, str] = {}
    for directory in sorted({relative.split("/", 1)[0] for relative in directories}):
        for path in sorted((model_root / directory).rglob("*.yaml"), key=lambda entry: entry.as_posix()):
            tree[path.relative_to(model_root).as_posix()] = declared_identity(path)
    return tree
