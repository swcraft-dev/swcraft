"""The SWCraft release a generated project names.

The specimen generator pins no SWCraft release. The `toolchain_version` it writes into
`swcraft.yaml`, and the `swcraft_version` of each sidecar, is found at run time, in this order:

1. an explicit value, `--toolchain-version VERSION` on `generate` and `specimen`, which the CLI sets
   for the run with `explicit_toolchain_version` (a value that is not an exact release is refused);
2. else the version of the `swcraft-tools` distribution installed in the specimen generator's own
   environment (`importlib.metadata`), when it is an exact release;
3. else none: the manifest has no `toolchain_version` key, which pins nothing, and the sidecar's
   `swcraft_version` is `null`.

What this does not reach: case 2 reads the distribution's metadata, not a `swcraft` executable on
`PATH`, so a project generated in one environment and checked by another SWCraft names the first.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from importlib import metadata

from .errors import PresetError

#: The distribution whose version is case 2.
SWCRAFT_DISTRIBUTION = "swcraft-tools"

#: One PEP 440 version in canonical form (the specification's *Appendix B*, without its
#: permissive spellings): an exact release, never a specifier or a word.
_EXACT_RELEASE = re.compile(
    r"(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*"
    r"(?:(?:a|b|rc)[0-9]+)?"
    r"(?:\.post[0-9]+)?"
    r"(?:\.dev[0-9]+)?"
    r"(?:\+[a-z0-9]+(?:\.[a-z0-9]+)*)?"
)

_EXPLICIT: ContextVar[str | None] = ContextVar("swcraft_specimens_toolchain_version", default=None)


def is_exact_release(value: object) -> bool:
    """Whether *value* is one canonical PEP 440 version string."""
    return isinstance(value, str) and _EXACT_RELEASE.fullmatch(value) is not None


def installed_swcraft_version() -> str | None:
    """Case 2: the installed `swcraft-tools` distribution's version, or `None` (case 3)."""
    try:
        version = metadata.version(SWCRAFT_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None
    return version if is_exact_release(version) else None


@contextmanager
def explicit_toolchain_version(value: str | None) -> Iterator[None]:
    """Case 1 for the duration of the block; `None` leaves the lookup to cases 2 and 3."""
    if value is not None and not is_exact_release(value):
        raise PresetError(f"--toolchain-version {value!r} is not an exact release")
    token = _EXPLICIT.set(value)
    try:
        yield
    finally:
        _EXPLICIT.reset(token)


def toolchain_version() -> str | None:
    """The SWCraft release a project generated now names, by the three cases in order."""
    explicit = _EXPLICIT.get()
    if explicit is not None:
        return explicit
    return installed_swcraft_version()
