"""The sidecar goldens' reading of `generator.version`.

The generator ships at SWCraft's version, so a golden SHA-256 over the sidecar as written would move
on every bump. The goldens are taken with `generator.version` read back to `GOLDEN_GENERATOR_VERSION`:
a bump moves no golden, and every other byte of the sidecar is still held.

What this does not reach: the version the sidecar states, which `test_specimen_self_check.py` and
`test_sidecar_and_corruption.py` hold to `swcraft_specimens.__version__` separately.
"""

from __future__ import annotations

import hashlib

from swcraft_specimens import __version__

#: The generator version the sidecar goldens were taken at.
GOLDEN_GENERATOR_VERSION = "0.23.0"


def _generator_entry(version: str) -> str:
    """The `generator` entry as the sidecar writer serialises it (two-space indent, `": "`)."""
    return f'"generator": {{\n    "name": "swcraft-specimens",\n    "version": "{version}"\n  }}'


def sidecar_golden_sha256(sidecar: bytes) -> str:
    """The SHA-256 of *sidecar* with its `generator.version` read back to the golden's version."""
    text = sidecar.decode("utf-8")
    live = _generator_entry(__version__)
    assert text.count(live) == 1, "the sidecar's `generator` entry is not the one the goldens read back"
    return hashlib.sha256(text.replace(live, _generator_entry(GOLDEN_GENERATOR_VERSION)).encode("utf-8")).hexdigest()
