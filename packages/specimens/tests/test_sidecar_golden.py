"""The sidecar goldens read `generator.version` back, so a SWCraft version bump moves none of them."""

from __future__ import annotations

import json
from typing import Any

import pytest
from swcraft_specimens import __version__
from tests.oracle import sidecar_golden
from tests.oracle.sidecar_golden import GOLDEN_GENERATOR_VERSION, sidecar_golden_sha256


def _sidecar(version: str, **rest: Any) -> bytes:
    """A sidecar serialised the way `sidecar.generation_json_bytes` writes one."""
    document = {"kind": "specimen", "generator": {"name": "swcraft-specimens", "version": version}, **rest}
    text = json.dumps(document, ensure_ascii=False, indent=2, separators=(",", ": "), allow_nan=False) + "\n"
    return text.encode("utf-8")


def test_the_golden_version_is_a_release() -> None:
    assert GOLDEN_GENERATOR_VERSION.count(".") == 2


def test_another_version_reads_back_to_the_same_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    golden = sidecar_golden_sha256(_sidecar(__version__, project={"a": 1}))
    monkeypatch.setattr(sidecar_golden, "__version__", "9.9.9")
    assert sidecar_golden_sha256(_sidecar("9.9.9", project={"a": 1})) == golden


def test_every_other_byte_is_still_held() -> None:
    assert sidecar_golden_sha256(_sidecar(__version__, project={"a": 1})) != sidecar_golden_sha256(
        _sidecar(__version__, project={"a": 2})
    )


def test_a_sidecar_not_stating_the_running_version_is_refused() -> None:
    with pytest.raises(AssertionError, match="generator"):
        sidecar_golden_sha256(_sidecar("0.0.1"))
