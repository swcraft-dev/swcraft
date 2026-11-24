"""The ``{{name}}`` placeholder form of the agent benchmark's templates: find the names, fill them.

A placeholder is lower-case letters and underscores between double braces. Callers load this
module by path (``scripts/`` is not a package). Stdlib only.

:func:`fill` raises ``KeyError`` on a name *values* lacks, so a caller checks
:func:`placeholders_in` against its values first.
"""

from __future__ import annotations

import re
from typing import Any

PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")


def placeholders_in(value: Any) -> set[str]:
    """Every placeholder name in *value*, at any depth of its dicts and lists."""
    if isinstance(value, dict):
        return set().union(*(placeholders_in(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(placeholders_in(item) for item in value))
    if isinstance(value, str):
        return set(PLACEHOLDER.findall(value))
    return set()


def fill(value: Any, values: dict[str, str]) -> Any:
    """*value* with every ``{{NAME}}`` replaced by ``values[NAME]``, at any depth; keys are kept."""
    if isinstance(value, dict):
        return {key: fill(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [fill(item, values) for item in value]
    if isinstance(value, str):
        return PLACEHOLDER.sub(lambda match: values[match.group(1)], value)
    return value
