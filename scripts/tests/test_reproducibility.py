"""Reproducibility of the three builds.

1. **The architecture project's build** (``moon run :build`` in a scaffolded
   project, ``integration``): two clean runs -- outputs **and** the Moon cache
   removed, else a cache hit restores the first run's files -- produce identical bytes.
2. **SWCraft's reference build** (``dev.py build`` on ``examples/basic``): the
   generators depend on no clock, iteration order or leftover output.
3. **The local artifacts** (``build_artifacts.py``): two runs at one commit give the
   same wheel, sdist and guide-archive digests, through ``SOURCE_DATE_EPOCH``. MkDocs
   stamps a timestamp into ``sitemap.xml.gz``'s gzip header, so the guide needs it too.

Nothing here may be skipped: a missing ``moon``, ``.venv`` or workstation archive is
a failure (:func:`test_this_module_never_skips`).
"""

from __future__ import annotations

import os
import shutil
import zipfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import pytest

# Shared with test_template_acceptance.py through conftest.py, so the setup exists once.
from conftest import (
    CONFTEST_PATH,
    LOADER_MODULES,
    REPO_ROOT,
    build_artifacts_module,
    declared_outputs,
    dev_module,
    moon_build,
    no_skip_offenders,
    output_text,
    run_captured,
    seed_valid_model,
)

_DEV_SCRIPT = REPO_ROOT / "scripts" / "dev.py"


_DEV = dev_module()
_ARTIFACTS = build_artifacts_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def read_outputs(paths: Iterable[Path]) -> dict[str, bytes]:
    """The generated artifacts' bytes, keyed by filename, proven non-empty.

    Otherwise producing nothing twice would count as reproducible.
    """
    contents: dict[str, bytes] = {}
    for path in paths:
        assert path.is_file(), f"{path} was not produced"
        data = path.read_bytes()
        assert data, f"{path} is empty"
        contents[path.name] = data
    return contents


def digests(directory: Path) -> dict[str, str]:
    """SHA-256 of every file in *directory*, keyed by filename."""
    return {
        path.name: _ARTIFACTS.sha256_file(path)
        for path in sorted(directory.iterdir(), key=lambda path: path.as_posix())
        if path.is_file()
    }


def require_venv_python() -> Path:
    """This checkout's own interpreter, or a hard failure naming the fix."""
    interpreter = _DEV.venv_python(REPO_ROOT)
    assert interpreter.exists(), (
        f"{interpreter} is missing -- run `python scripts/dev.py install` first. "
        "The reproducibility gate builds real artifacts and may not be downgraded to a skip."
    )
    return interpreter


def clean_moon_build(project: Path) -> dict[str, bytes]:
    """One **clean** ``moon run :build``: no prior outputs, no Moon cache.

    Without the cache moon cannot restore the previous run's files, so a broken
    generator cannot "reproduce" its earlier output.
    """
    for path in declared_outputs(project).values():
        path.unlink(missing_ok=True)
    shutil.rmtree(project / ".moon" / "cache", ignore_errors=True)

    completed = moon_build(project)
    assert completed.returncode == 0, output_text(completed)
    return read_outputs(declared_outputs(project).values())


# ---------------------------------------------------------------------------
# SOURCE_DATE_EPOCH: the mechanism the artifact reproducibility rests on
# ---------------------------------------------------------------------------


class TestSourceDateEpochIsPinned:
    def test_an_exported_value_wins(self):
        """Whoever exported it is reproducing an older build; do not override it."""
        environ = {"SOURCE_DATE_EPOCH": "1234567890"}
        assert _ARTIFACTS.source_date_epoch(REPO_ROOT, environ) == "1234567890"

    def test_the_commit_date_is_used_when_nothing_is_exported(self):
        stamp = _ARTIFACTS.source_date_epoch(REPO_ROOT, {})
        assert stamp.isdigit(), stamp
        assert stamp == _ARTIFACTS.read_git_commit_epoch(REPO_ROOT)

    def test_a_tree_with_no_history_falls_back_instead_of_failing(self, tmp_path):
        """A missing commit date must not fail a build -- the artifacts are valid."""
        assert _ARTIFACTS.read_git_commit_epoch(tmp_path) is None
        assert _ARTIFACTS.source_date_epoch(tmp_path, {}) == _ARTIFACTS.FALLBACK_SOURCE_DATE_EPOCH

    def test_the_context_manager_exports_and_restores(self):
        environ: dict[str, str] = {}
        with _ARTIFACTS.reproducible_build_environment(REPO_ROOT, environ) as stamp:
            assert environ["SOURCE_DATE_EPOCH"] == stamp
        assert "SOURCE_DATE_EPOCH" not in environ, "the variable must not leak out of the build"

    def test_the_context_manager_restores_a_previous_value(self):
        environ = {"SOURCE_DATE_EPOCH": "42"}
        with _ARTIFACTS.reproducible_build_environment(REPO_ROOT, environ):
            pass
        assert environ == {"SOURCE_DATE_EPOCH": "42"}

    def test_every_python_build_step_runs_with_it_set(self, tmp_path):
        """Grounded in the real build function, not in the context manager alone."""
        observed: list[str | None] = []

        def runner(argv, cwd):
            observed.append(os.environ.get("SOURCE_DATE_EPOCH"))

        outdir = tmp_path / "dist" / _ARTIFACTS.PYTHON_DIRNAME
        outdir.mkdir(parents=True)
        _ARTIFACTS.build_python_artifacts(REPO_ROOT, tmp_path / "dist", run=runner)

        assert len(observed) == len(_ARTIFACTS.PYTHON_PROJECTS)
        assert all(stamp and stamp.isdigit() for stamp in observed), observed
        assert len(set(observed)) == 1, f"the stamp changed mid-build: {observed}"
        assert "SOURCE_DATE_EPOCH" not in os.environ, "the build must not leak the variable"


# ---------------------------------------------------------------------------
# SWCraft's own reference build
# ---------------------------------------------------------------------------


@pytest.mark.artifact
def test_dev_build_is_byte_reproducible():
    """Two ``dev.py build`` runs over ``examples/basic`` produce the same bytes.

    Run in the live checkout; only the gitignored outputs are touched, deleted before each run.
    """
    interpreter = require_venv_python()
    outputs = _DEV.reference_outputs(REPO_ROOT)
    argv = [interpreter, _DEV_SCRIPT, "--repo-root", REPO_ROOT, "build"]

    generated: list[dict[str, bytes]] = []
    for _ in range(2):
        for path in outputs:
            path.unlink(missing_ok=True)
        completed = run_captured(argv, REPO_ROOT)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        generated.append(read_outputs(outputs))

    first, second = generated
    assert set(first) == set(second)
    for name in sorted(first):
        assert first[name] == second[name], f"{name} differs between two `dev.py build` runs"


# ---------------------------------------------------------------------------
# The local artifacts
# ---------------------------------------------------------------------------


@pytest.mark.artifact
def test_two_artifact_builds_at_the_same_commit_have_identical_digests(tmp_path):
    """The same commit built twice yields the same wheel and sdist digests.

    Built into two separate directories so the second run cannot be graded on the first's files.
    """
    interpreter = require_venv_python()

    outputs = []
    for name in ("first", "second"):
        dist_root = tmp_path / name
        (dist_root / _ARTIFACTS.PYTHON_DIRNAME).mkdir(parents=True)
        _ARTIFACTS.build_python_artifacts(REPO_ROOT, dist_root, python_executable=str(interpreter))
        outputs.append(dist_root / _ARTIFACTS.PYTHON_DIRNAME)

    first, second = (digests(directory) for directory in outputs)
    assert set(first) == set(second)
    assert len(first) == 2 * len(_ARTIFACTS.PYTHON_PROJECTS), f"expected a wheel and an sdist per project: {first}"
    for filename in sorted(first):
        assert first[filename] == second[filename], f"{filename} is not reproducible at this commit"


@pytest.mark.artifact
def test_two_guide_site_builds_at_the_same_commit_produce_the_same_archive(tmp_path):
    """The user guide's release artifact is reproducible, not merely rebuilt.

    Each render goes to a directory of its own, so neither is graded on the other's
    output and ``dist/docs/site`` is left alone. Not reached: reproducibility across
    environments (another MkDocs version renders other bytes).
    """
    interpreter = require_venv_python()
    epoch = _ARTIFACTS.source_date_epoch(REPO_ROOT)

    archives = []
    for name in ("first", "second"):
        site_dir = _ARTIFACTS.build_docs_site(
            REPO_ROOT, python_executable=str(interpreter), site_dir=tmp_path / name / "site"
        )
        archives.append(_ARTIFACTS.archive_docs_site(site_dir, tmp_path / name / "swcraft-guide.zip", epoch))

    first, second = archives
    with zipfile.ZipFile(first) as opened:
        members = opened.namelist()
    assert len(members) > 1, f"the archive carries almost nothing: {members}"
    assert first.read_bytes() == second.read_bytes(), (
        "two renders of the same commit produced different archives -- look at sitemap.xml.gz "
        "first, whose gzip header carries a timestamp"
    )


@pytest.mark.artifact
def test_the_rendered_sitemap_carries_the_commit_date_rather_than_the_clock(tmp_path):
    """Why the site build runs under ``reproducible_build_environment``.

    Unset, MkDocs gzips ``sitemap.xml`` with today's date in the header. The gzip
    MTIME field (bytes 4-7, little-endian) is read back. It is the epoch's date at
    00:00 UTC, not the epoch: MkDocs parses a ``YYYY-MM-DD`` ``update_date`` back to
    midnight. Not reached: any other timestamp a theme or plugin might embed.
    """
    interpreter = require_venv_python()
    site_dir = _ARTIFACTS.build_docs_site(REPO_ROOT, python_executable=str(interpreter), site_dir=tmp_path / "site")

    sitemap = site_dir / "sitemap.xml.gz"
    assert sitemap.is_file(), f"{sitemap} was not rendered"
    stamped = int.from_bytes(sitemap.read_bytes()[4:8], "little")

    epoch = int(_ARTIFACTS.source_date_epoch(REPO_ROOT))
    expected = int(datetime.fromtimestamp(epoch, UTC).replace(hour=0, minute=0, second=0).timestamp())
    assert stamped == expected, (
        "sitemap.xml.gz was stamped with something other than SOURCE_DATE_EPOCH's date -- "
        "the site build is no longer running under reproducible_build_environment"
    )


# ---------------------------------------------------------------------------
# The architecture project's build (the delivered contract)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_two_clean_moon_builds_are_byte_identical(architecture_project: Path):
    """``moon run :build`` twice, cleanly, produces identical artifacts."""
    seed_valid_model(architecture_project)

    first = clean_moon_build(architecture_project)
    second = clean_moon_build(architecture_project)

    assert set(first) == set(second)
    for name in sorted(first):
        assert first[name] == second[name], f"{name} differs between two clean `moon run :build` runs"


@pytest.mark.integration
def test_the_moon_cache_is_really_cleared_between_the_runs(architecture_project: Path):
    """``clean_moon_build`` really removes the Moon cache, which the case above relies on."""
    seed_valid_model(architecture_project)
    clean_moon_build(architecture_project)
    cache = architecture_project / ".moon" / "cache"
    assert cache.is_dir(), "the build did not run at all -- moon writes its cache when it does"

    # The helper itself must remove the sentinel; deleting it inline would test `rmtree`.
    sentinel = cache / "sentinel-from-the-previous-run.txt"
    sentinel.write_text("written between the two clean builds\n", encoding="utf-8")

    clean_moon_build(architecture_project)

    assert not sentinel.exists(), (
        "clean_moon_build no longer clears .moon/cache -- the second build could be a "
        "cache restore of the first, and a reproducible build would be unverified"
    )


# ---------------------------------------------------------------------------
# The performance baseline is recorded, not merely measured
# ---------------------------------------------------------------------------


class TestThePerformanceBaselineIsDocumented:
    """The performance baseline is report-only, so the record is the deliverable."""

    def test_the_measurement_is_not_a_stage_of_verify(self):
        """The benchmark may never fail a build."""
        assert "benchmark" not in _DEV.VERIFY_STAGES


# ---------------------------------------------------------------------------
# Structural: this module may not be skipped
# ---------------------------------------------------------------------------


def test_this_module_never_skips():
    """A reproducibility gate that quietly disappears is not a gate.

    Also scanned: the ``tools`` ``conftest.py``, where the preconditions live, and the
    two modules that load it under this root.
    """
    for module in (Path(__file__), CONFTEST_PATH, *LOADER_MODULES):
        offenders = no_skip_offenders(module)
        assert not offenders, f"the reproducibility gate may never be skipped ({module.name}): {offenders}"
