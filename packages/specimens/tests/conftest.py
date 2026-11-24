"""Shared test configuration.

Every test runs with the `toolchain_version` lookup seeing no installed `swcraft-tools`, so no
manifest names a release and every sidecar's `swcraft_version` is `null`; the tree and sidecar
digest goldens hold in any environment. A test of an installed release fakes the distribution over
this fixture; a subprocess is not reached by it.
"""

from __future__ import annotations

from collections.abc import Iterator
from importlib import metadata

import pytest
from swcraft_specimens import toolchain


@pytest.fixture(autouse=True)
def no_installed_swcraft(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    real = metadata.version

    def version(name: str) -> str:
        if name == toolchain.SWCRAFT_DISTRIBUTION:
            raise metadata.PackageNotFoundError(name)
        return real(name)

    monkeypatch.setattr(metadata, "version", version)
    yield
