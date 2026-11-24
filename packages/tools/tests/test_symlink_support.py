"""``symlink_support``'s switch: ``SWCRAFT_REQUIRE_SYMLINKS=1`` turns a skip into a failure.

The refusal is simulated by making ``Path.symlink_to`` (and ``os.symlink``)
raise, so both states are proven on every host, including one that can make
links.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import symlink_support
from symlink_support import REQUIRE_SYMLINKS_VARIABLE, skip_without_symlink_support, symlink_or_skip


def _refuse(*_args: object, **_kwargs: object) -> None:
    raise OSError(1314, "A required privilege is not held by the client")


@pytest.fixture
def refusing_host(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(Path, "symlink_to", _refuse)
    monkeypatch.setattr(os, "symlink", _refuse)
    return monkeypatch


@pytest.mark.parametrize("value", [None, "0", ""])
def test_unset_or_not_one_a_refused_link_skips(refusing_host: pytest.MonkeyPatch, tmp_path: Path, value) -> None:
    if value is None:
        refusing_host.delenv(REQUIRE_SYMLINKS_VARIABLE, raising=False)
    else:
        refusing_host.setenv(REQUIRE_SYMLINKS_VARIABLE, value)
    assert not symlink_support.symlinks_required()
    with pytest.raises(pytest.skip.Exception):
        symlink_or_skip(tmp_path / "link", tmp_path)
    with pytest.raises(pytest.skip.Exception):
        skip_without_symlink_support(tmp_path)


def test_required_a_refused_link_fails(refusing_host: pytest.MonkeyPatch, tmp_path: Path) -> None:
    refusing_host.setenv(REQUIRE_SYMLINKS_VARIABLE, "1")
    assert symlink_support.symlinks_required()
    with pytest.raises(pytest.fail.Exception, match=REQUIRE_SYMLINKS_VARIABLE):
        symlink_or_skip(tmp_path / "link", tmp_path)
    with pytest.raises(pytest.fail.Exception, match=REQUIRE_SYMLINKS_VARIABLE):
        skip_without_symlink_support(tmp_path)
