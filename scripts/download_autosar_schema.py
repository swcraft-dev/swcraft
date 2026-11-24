"""Provision a license-gated AUTOSAR XSD for local validation.

Each schema is stored at ``schemas/<AUTOSAR_x-y-z>.xsd`` in the repository root,
supplied manually or downloaded from a company-internal mirror. Absence is not an
error because validation has a structural fallback.

Usage::

    python scripts/download_autosar_schema.py            # 4.2.2, the default
    python scripts/download_autosar_schema.py 4.3.0

Mirror URLs are read per version from ``SWCRAFT_AUTOSAR_XSD_URL_<VERSION>``
(``SWCRAFT_AUTOSAR_XSD_URL_4_3_0``). The unversioned ``SWCRAFT_AUTOSAR_XSD_URL``
is honoured for the default version only: for 4.3.0 it would silently save a
4.2.2 schema under the 4.3.0 filename.

Stdlib only, so ``SCHEMA_FILENAMES`` is a copy of
``swcraft_tools.arxml_version.SCHEMA_FILENAMES``, pinned against it by
``scripts/tests/test_download_autosar_schema.py``.
"""

from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path

#: Must stay equal to ``swcraft_tools.arxml_version.SCHEMA_FILENAMES``.
SCHEMA_FILENAMES = {
    "4.2.2": "AUTOSAR_4-2-2.xsd",
    "4.3.0": "AUTOSAR_4-3-0.xsd",
}

DEFAULT_VERSION = "4.2.2"

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPOSITORY_ROOT / "schemas"
URL_ENV_VAR = "SWCRAFT_AUTOSAR_XSD_URL"


def schema_path(version: str) -> Path:
    """Where this version's XSD is expected to live."""
    return SCHEMA_DIR / SCHEMA_FILENAMES[version]


def url_env_var(version: str) -> str:
    """The version-specific mirror-URL environment variable name."""
    return f"{URL_ENV_VAR}_{version.replace('.', '_')}"


def _configured_url(version: str) -> tuple[str, str] | None:
    """The mirror URL for *version* and the variable it came from, if set."""
    names = [url_env_var(version)]
    if version == DEFAULT_VERSION:
        names.append(URL_ENV_VAR)
    for name in names:
        url = os.environ.get(name)
        if url:
            return url, name
    return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    version = args[0] if args else DEFAULT_VERSION

    if version not in SCHEMA_FILENAMES:
        supported = ", ".join(SCHEMA_FILENAMES)
        print(f"Unsupported AUTOSAR version {version!r}; supported versions are: {supported}")
        return 2

    target = schema_path(version)
    if target.is_file():
        print(f"Schema already present: {target}")
        return 0

    configured = _configured_url(version)
    if configured is not None:
        url, source_var = configured
        SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Downloading schema from {source_var} -> {target}")
        urllib.request.urlretrieve(url, target)
        print("Done.")
        return 0

    # Name every variable this version honours, the unversioned one included for the default.
    variables = " or ".join(
        [url_env_var(version)] + ([URL_ENV_VAR] if version == DEFAULT_VERSION else []),
    )
    print(
        f"AUTOSAR {version} XSD not found and no download URL configured.\n"
        f"  Expected location: {target}\n"
        f"  The official {target.name} requires an AUTOSAR account and is not\n"
        "  redistributable, so it cannot be fetched automatically.\n"
        "  To enable full XSD validation, either:\n"
        "    - place the schema file at the path above, or\n"
        f"    - set {variables} to a company-internal mirror URL and re-run.\n"
        "  Validation will fall back to a structural/well-formedness check until then."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
