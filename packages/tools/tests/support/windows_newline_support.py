"""Reproduce Windows' CRLF write translation on a POSIX host.

The byte tests this module serves assert that a file SWCraft writes carries no ``\\r\\n``. On
Linux that assertion is vacuous: ``newline=None`` translates ``\\n`` to ``os.linesep``, already
``\\n``, so the test passes against the broken code too. Asserting that ``newline="\\n"`` appears
in the source would be a keyword check, passing an implementation that opens the file a second
time without it.

CPython decides a text stream's write translation in ``_io.TextIOWrapper`` at compile time
(``#ifdef MS_WINDOWS``), so patching ``os.linesep`` does not reach it; the emulation binds
``newline="\\r\\n"`` one level up, onto every text-mode write open whose newline is ``None``.
That is exactly the Windows rule:

- Only writes: a read's ``newline=None`` is universal newline mode on every platform, and
  translating there would corrupt the tests that read their own fixtures.
- Only ``None``: a call naming ``"\\n"``, ``""`` or ``"\\r\\n"`` is honoured verbatim, so a fix
  that names the keyword at one call site and omits it at another still fails the comparison.
- Binary opens are untouched.

What it does not reach: it patches ``io.open`` and ``builtins.open`` (where ``Path.open`` and
``Path.write_text`` end up in CPython 3.11+); a module that captured ``open`` under another name
before the fixture ran, a hand-built ``io.TextIOWrapper``, ``os.write`` on a raw descriptor, or a
third-party extension writing its own bytes pass through unemulated. It is a stand-in for the
platform, not the platform: the Windows runner is still where the suite runs for real.
"""

from __future__ import annotations

import builtins
import io
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

#: The mode characters that make an open a write.
_WRITE_CHARACTERS = frozenset("wxa+")


def _translates(mode: str, newline: str | None) -> bool:
    if "b" in mode:
        return False
    if newline is not None:
        return False
    return any(character in mode for character in _WRITE_CHARACTERS)


@contextmanager
def windows_newline_translation() -> Iterator[None]:
    """Within the block, a text write that declares no newline emits CRLF."""
    real_open = io.open

    def patched_open(
        file: Any,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if _translates(mode, newline):
            newline = "\r\n"
        return real_open(file, mode, buffering, encoding, errors, newline, *args, **kwargs)

    io.open = patched_open  # type: ignore[assignment]
    builtins.open = patched_open  # type: ignore[assignment]
    try:
        yield
    finally:
        io.open = real_open  # type: ignore[assignment]
        builtins.open = real_open  # type: ignore[assignment]


def assert_no_crlf(path: Any, *, what: str) -> bytes:
    """Read *path* as bytes and assert it carries no ``\\r\\n``. Returns them."""
    payload = path.read_bytes()
    count = payload.count(b"\r\n")
    assert count == 0, (
        f"{what} carries CRLF ({count} of them). The write that produced it "
        "inherits the platform's line ending; name newline='\\n' on it. These bytes are "
        "committed, hashed or byte-compared, so they must be the same on every platform."
    )
    return payload
