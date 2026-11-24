"""Licensed-AUTOSAR-XSD resolution for the test suite, and its skip/fail policy.

Not part of ``conftest.py``: :func:`require_licensed_schema` calls ``pytest.skip``, and
``conftest.py`` is scanned by ``no_skip_offenders()``, which the mandatory acceptances
(``test_template_acceptance.py``, ``test_reproducibility.py``) use to assert their preconditions
never skip. ``conftest.py`` owns the ``--require-autosar-xsd`` option (pytest reads
``pytest_addoption`` from an initial conftest only) and hands its value here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from swcraft_tools.arxml_validation import DEFAULT_SCHEMA_PATHS, SCHEMA_PATH_ENV
from swcraft_tools.arxml_version import SCHEMA_FILENAMES, ArxmlVersion

#: Per-version environment override for a licensed XSD, checked before the
#: single-path ``SWCRAFT_AUTOSAR_XSD_PATH`` the product reads: a conformance run needs
#: both schemas at once, and ``schemas/`` is gitignored, so a worktree has none unless pointed at.
SCHEMA_PATH_ENV_BY_VERSION: dict[ArxmlVersion, str] = {
    ArxmlVersion.V4_2_2: "SWCRAFT_AUTOSAR_XSD_PATH_4_2_2",
    ArxmlVersion.V4_3_0: "SWCRAFT_AUTOSAR_XSD_PATH_4_3_0",
}

#: Set from ``--require-autosar-xsd`` by ``conftest.pytest_configure``. When
#: true, an absent licensed schema fails instead of skipping.
REQUIRE_LICENSED_XSD = False


def set_require_licensed_xsd(required: bool) -> None:
    """Set :data:`REQUIRE_LICENSED_XSD`; called once, from ``pytest_configure``."""
    global REQUIRE_LICENSED_XSD  # noqa: PLW0603 — one process-wide flag, set once from the CLI
    REQUIRE_LICENSED_XSD = required


def licensed_schema(version: ArxmlVersion) -> str | None:
    """Return the licensed XSD for *version*, or ``None``.

    Not ``arxml_validation._resolve_schema_path``: a test precondition must not move with the
    helper the tests exercise. Looked at in order: this version's entry in
    :data:`SCHEMA_PATH_ENV_BY_VERSION`; ``SWCRAFT_AUTOSAR_XSD_PATH`` only when its filename is
    this version's (so a variable naming ``AUTOSAR_4-3-0.xsd`` never validates the 4.2.2
    goldens); the per-version default under ``schemas/``.
    """
    explicit = os.environ.get(SCHEMA_PATH_ENV_BY_VERSION[version])
    if explicit and os.path.isfile(explicit):
        return explicit
    configured = os.environ.get(SCHEMA_PATH_ENV)
    if configured and os.path.basename(configured) == version.schema_filename and os.path.isfile(configured):
        return configured
    default_path = DEFAULT_SCHEMA_PATHS.get(version)
    return default_path if default_path is not None and os.path.isfile(default_path) else None


def no_licensed_schema_reason(version: ArxmlVersion) -> str:
    """Return the skip reason naming every place :func:`licensed_schema` looked."""
    default_path = DEFAULT_SCHEMA_PATHS.get(version)
    default = "the default schema (none outside a source checkout)" if default_path is None else default_path
    return (
        f"Licensed AUTOSAR {version} XSD not available: none of "
        f"{SCHEMA_PATH_ENV_BY_VERSION[version]}, {SCHEMA_PATH_ENV} (set to a file named "
        f"{version.schema_filename}) or {default} resolves to a file. "
        "See scripts/download_autosar_schema.py."
    )


def require_licensed_schema(version: ArxmlVersion) -> str:
    """Return the licensed XSD for *version*, skipping — or **failing** — when absent.

    Called from a test body, not a ``skipif`` marker: a marker is evaluated at collection
    time and always skips, so ``--require-autosar-xsd`` could not turn it into a failure.
    """
    path = licensed_schema(version)
    if path is not None:
        return path
    reason = no_licensed_schema_reason(version)
    if REQUIRE_LICENSED_XSD:
        pytest.fail(f"--require-autosar-xsd was passed, but this test cannot run: {reason}")
    pytest.skip(reason)
    raise AssertionError("unreachable")  # pragma: no cover — pytest.skip raises


def forced_structural_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make :mod:`swcraft_tools.arxml_validation` resolve **no** schema at all.

    A test whose subject is the structural fallback must assert the same thing on every
    machine, not become an XSD test wherever a licensed schema is visible.
    """
    monkeypatch.delenv(SCHEMA_PATH_ENV, raising=False)
    for name in SCHEMA_PATH_ENV_BY_VERSION.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "swcraft_tools.arxml_validation.DEFAULT_SCHEMA_PATHS",
        {version: os.path.join(os.sep, "nonexistent", filename) for version, filename in SCHEMA_FILENAMES.items()},
    )


#: Prepended to every script :func:`run_without_a_checkout` runs: ``env.checkout_root``
#: answers ``None`` before anything imports ``arxml_validation``.
_NO_CHECKOUT_PRELUDE = "import swcraft_tools.env as _env\n_env.checkout_root = lambda module_file=None: None\n"


def run_without_a_checkout(script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run *script* in a fresh interpreter whose ``env.checkout_root()`` answers ``None``.

    That is an installed package's answer, given before ``arxml_validation`` is imported so
    its import-time wiring (``SCHEMA_DIR``, ``DEFAULT_SCHEMA_PATHS``) and every ``.env`` search
    run on it. No schema variable is inherited from this process.
    """
    environment = dict(os.environ)
    for name in (SCHEMA_PATH_ENV, "SWCRAFT_MANIFEST", *SCHEMA_PATH_ENV_BY_VERSION.values()):
        environment.pop(name, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-c", _NO_CHECKOUT_PRELUDE + script],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
