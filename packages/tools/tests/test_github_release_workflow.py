"""``.github/workflows/release.yml``, pinned: the open copy's release on GitHub.

Held: the triggers (a ``v*`` tag push, a push to ``release-candidate``, a dispatch with one
boolean ``draft`` input, no pull-request trigger); every job's ``if:`` a conjunction led by
``github.server_url == 'https://github.com'``, so another host that reads ``.github/workflows/``
runs nothing; the runners; Linux and Windows build and smoke-test only; every ``VERIFY_STAGES``
stage but ``install`` in exactly one macOS job (``install`` in every one, each a fresh machine),
each under GitHub's six-hour job limit; ``pwsh``'s native exit codes checked; the candidate branch
publishes nothing; least-privilege permissions, SHA-pinned GitHub-owned actions, no secret and no
cache; the host steps reproduced from the work repository's workflows; and the publish job's two
shell steps, run with ``bash`` over a stub ``gh`` (skipped where the host cannot).

What it does not reach: whether GitHub's runners behave as the YAML says; the real ``gh``'s
wording (``release not found``, ``HTTP 404``), which the stub only repeats; and whether a pinned
SHA is the release its comment names.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT, dev_module

_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
_GUARD = "github.server_url == 'https://github.com'"
_CANDIDATE_SKIP = "github.ref != 'refs/heads/release-candidate'"
_BUILD_JOBS = {"linux": "ubuntu-24.04", "windows": "windows-2022"}
_MACOS_JOBS = ("macos-checks", "macos-slow", "macos-archive", "macos-integration")
_PUBLISH = "publish"
#: What a Linux or Windows job may run of ``dev.py``: ``build`` and the stages that make the archive.
_BUILD_STAGES = ("install", "build", "artifacts", "runtime", "wheelhouse", "workstation-archive")
#: ``dev.py`` commands a macOS job may run that are not ``verify`` stages.
_MACOS_EXTRA_COMMANDS = {"docs"}
_ALLOWED_ACTIONS = {
    "actions/checkout",
    "actions/setup-python",
    "actions/upload-artifact",
    "actions/download-artifact",
}
_SIX_HOURS = 360
#: A ``dev.py`` command, whatever interpreter token runs it.
_DEV_COMMAND = re.compile(r"scripts/dev\.py\s+([a-z][a-z-]*)")
_MACOS_TOOLS_STEP = "Python and Node.js by proto, at the root .prototools' versions"
_BUILD_NODE_STEP = "Node.js by proto, at the root .prototools' version"
_MOON_STEP = "moon by proto, at the template's .prototools version"


def _text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def _workflow() -> dict:
    return yaml.safe_load(_text())


def _jobs() -> dict:
    return _workflow()["jobs"]


def _steps(job: str) -> list[dict]:
    return _jobs()[job]["steps"]


def _step(job: str, name: str) -> dict:
    (step,) = [step for step in _steps(job) if step.get("name") == name]
    return step


def _runs(job: str) -> str:
    return "\n".join(step.get("run", "") for step in _steps(job))


def _dev_commands(job: str) -> list[str]:
    return [command for step in _steps(job) for command in _DEV_COMMAND.findall(step.get("run", ""))]


def _conjuncts(job: str) -> list[str]:
    condition = str(_jobs()[job].get("if", ""))
    assert "||" not in condition, f"{job}'s condition holds an ||, which can defeat the guard: {condition!r}"
    return [term.strip() for term in condition.split("&&")]


def _triggers() -> dict:
    workflow = _workflow()
    # YAML 1.1 reads the bare key `on` as the boolean True; PyYAML follows it.
    return workflow["on"] if "on" in workflow else workflow[True]


def test_the_triggers_are_a_tag_push_a_candidate_push_and_a_dispatch_alone() -> None:
    triggers = _triggers()
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"] == {"tags": ["v*"], "branches": ["release-candidate"]}
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"draft"}
    assert inputs["draft"]["type"] == "boolean"
    assert inputs["draft"]["default"] is True


def test_no_pull_request_workflow_exists() -> None:
    workflows = sorted(path.name for path in (REPO_ROOT / ".github" / "workflows").iterdir())
    assert workflows == ["release.yml"]
    assert "pull_request" not in _text()


def test_every_job_carries_the_github_guard() -> None:
    for name in _jobs():
        assert _conjuncts(name)[0] == _GUARD, f"{name} lacks the guard as its first term"


def test_the_jobs_and_their_runners() -> None:
    jobs = _jobs()
    assert set(jobs) == {*_BUILD_JOBS, *_MACOS_JOBS, _PUBLISH}
    for name, runner in _BUILD_JOBS.items():
        assert jobs[name]["runs-on"] == runner
    for name in _MACOS_JOBS:
        assert jobs[name]["runs-on"] == "macos-15", name
    assert jobs[_PUBLISH]["runs-on"] == "ubuntu-24.04"


def test_every_job_ends_under_the_six_hour_limit_and_every_stage_has_its_own_limit() -> None:
    for name, job in _jobs().items():
        assert 0 < job["timeout-minutes"] < _SIX_HOURS, name
        for step in job["steps"]:
            if _DEV_COMMAND.search(step.get("run", "")):
                assert 0 < step["timeout-minutes"] < job["timeout-minutes"], f"{name}: {step['name']}"


def test_linux_and_windows_build_and_smoke_test_only() -> None:
    for name in _BUILD_JOBS:
        assert _dev_commands(name) == list(_BUILD_STAGES), name
        assert set(_BUILD_STAGES) <= set(dev_module().VERIFY_STAGES)
        smoke = _step(name, "smoke-test the archive")["run"]
        for words in ("--home", "--no-path", "--no-vsix", "--version", " init scaffold", "generate small"):
            assert words in smoke, f"{name}'s smoke step lacks {words!r}"
        assert smoke.count(" validate --project ") == 2, f"{name}: the scaffold and the specimen are validated"
    assert "./swcraft_install.sh" in _step("linux", "smoke-test the archive")["run"]
    assert ".\\swcraft_install.cmd" in _step("windows", "smoke-test the archive")["run"]


def test_every_verify_stage_runs_in_exactly_one_macos_job() -> None:
    stages = list(dev_module().VERIFY_STAGES)
    owners: dict[str, list[str]] = {stage: [] for stage in stages}
    for job in _MACOS_JOBS:
        commands = _dev_commands(job)
        assert commands[0] == "install", f"{job} must install first: {commands}"
        unknown = set(commands) - set(stages) - _MACOS_EXTRA_COMMANDS
        assert not unknown, f"{job} runs dev.py commands that are no verify stage: {unknown}"
        ordered = [command for command in commands if command in stages]
        assert ordered == sorted(ordered, key=stages.index), f"{job} breaks VERIFY_STAGES' order: {ordered}"
        for command in ordered:
            owners[command].append(job)
    assert owners["install"] == list(_MACOS_JOBS), "install runs first in every macOS job"
    for stage in stages:
        if stage != "install":
            assert len(owners[stage]) == 1, f"{stage} runs in {owners[stage] or 'no macOS job'}"


def test_test_slow_runs_without_an_export_flag() -> None:
    assert "--require-" not in _text()


def test_the_macos_archive_is_built_once_and_passed_on() -> None:
    uploads = [
        (job, step)
        for job in _jobs()
        for step in _steps(job)
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    names = {step["with"]["name"]: job for job, step in uploads}
    assert names == {
        "workstation-linux-x64": "linux",
        "workstation-windows-x64": "windows",
        "workstation-macos-arm64": "macos-archive",
    }
    for _, step in uploads:
        # A week: a failed job is re-run against the others' archives without rebuilding them.
        assert step["with"]["retention-days"] == 7
        assert step["with"]["if-no-files-found"] == "error"
    archive = _dev_commands("macos-archive")
    assert archive.index("workstation-archive") < archive.index("test-artifacts")
    integration = _jobs()["macos-integration"]
    assert integration["needs"] == "macos-archive"
    (download,) = [s for s in integration["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact@")]
    assert download["with"] == {"name": "workstation-macos-arm64", "path": "dist/workstation"}
    names_in_order = [s.get("name") or s.get("uses") for s in integration["steps"]]
    assert names_in_order.index(download["uses"]) < names_in_order.index("test-model-integration")


def test_the_candidate_branch_runs_the_macos_jobs_alone_and_publishes_nothing() -> None:
    jobs = _jobs()
    assert set(jobs) == {*_BUILD_JOBS, *_MACOS_JOBS, _PUBLISH}
    for name in (*_BUILD_JOBS, _PUBLISH):
        assert _conjuncts(name) == [_GUARD, _CANDIDATE_SKIP], f"{name} must be skipped on release-candidate"
    for name in _MACOS_JOBS:
        assert _conjuncts(name) == [_GUARD], f"{name} is the check and runs on release-candidate"
    for name in jobs:
        if name != _PUBLISH:
            assert "gh release" not in _runs(name), f"{name} must not touch a release"
            assert "permissions" not in jobs[name], f"{name} must not widen its token"


def test_every_proto_unpack_keeps_the_shim() -> None:
    """proto 0.61 creates its shims from ``proto-shim``; ``proto`` alone fails ``proto install``."""
    unpacks = [
        line
        for job in _jobs()
        for step in _steps(job)
        for line in step.get("run", "").splitlines()
        if "proto.tar.xz" in line and line.strip().startswith("tar ")
    ]
    assert len(unpacks) == 1 + len(_MACOS_JOBS)
    for line in unpacks:
        assert line.rstrip().endswith('"proto_cli-$PROTO_TARGET/proto" "proto_cli-$PROTO_TARGET/proto-shim"'), line
    windows = [
        line for step in _steps("windows") for line in step.get("run", "").splitlines() if "Expand-Archive $zip" in line
    ]
    assert windows == ["Expand-Archive $zip -DestinationPath $bin -Force"], (
        "Windows expands the whole zip, shim included"
    )


def test_publish_needs_every_job_and_runs_only_on_success() -> None:
    publish = _jobs()[_PUBLISH]
    assert set(publish["needs"]) == {*_BUILD_JOBS, *_MACOS_JOBS}
    # No status function: an `if:` without one implies success() of every need.
    assert _conjuncts(_PUBLISH) == [_GUARD, _CANDIDATE_SKIP]


def test_publishing_is_serialised_per_release_version_and_never_cancelled() -> None:
    """A tag run and a dispatch of one release never publish at once, whichever refs started them."""
    assert "concurrency" not in _workflow(), "no group by ref: two refs of one release would publish at once"
    for name, job in _jobs().items():
        if name != _PUBLISH:
            assert "concurrency" not in job, f"{name} publishes nothing and needs no group"
    concurrency = _jobs()[_PUBLISH]["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    group = concurrency["group"]
    assert "${{ needs.linux.outputs.version }}" in group and "github.ref" not in group
    assert _jobs()["linux"]["outputs"] == {"version": "${{ steps.smoke.outputs.version }}"}
    smoke = _step("linux", "smoke-test the archive")
    assert smoke["id"] == "smoke"
    assert 'echo "version=$VERSION" >> "$GITHUB_OUTPUT"' in smoke["run"]


def test_the_version_is_read_by_one_spelling_everywhere() -> None:
    """The smoke steps and the publish job each read ``_version.py``; a change of its spelling must reach all."""
    readers = re.findall(r"import re; print\(re\.search\(.*?\)\.group\(1\)\)", _text())
    assert len(readers) == 3, readers
    assert len(set(readers)) == 1, readers
    opened = re.findall(r'open\("([^"]+)"', readers[0])
    assert len(opened) == 1 and (REPO_ROOT / opened[0]).is_file(), opened


def test_the_draft_mode_is_set_once_for_the_publish_job() -> None:
    assert _jobs()[_PUBLISH]["env"]["DRAFT"] == "${{ github.event_name == 'workflow_dispatch' && inputs.draft }}"
    assert _text().count("inputs.draft }}") == 1
    assert not [step for step in _steps(_PUBLISH) if "DRAFT" in step.get("env", {})]


def test_publish_reads_the_release_note_and_the_tree_s_version() -> None:
    version_step = _step(_PUBLISH, "the version and the tag")["run"]
    assert "packages/tools/src/swcraft_tools/_version.py" in version_step
    assert 'note="docs/release-notes/$version.md"' in version_step
    assert "No release note at $note" in version_step
    assert '[ "$tag" != "v$version" ]' in version_step
    assert "can only make a draft" in version_step
    release = _step(_PUBLISH, "create or update the GitHub Release")
    assert release["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "gh release create" in release["run"]
    assert "--verify-tag" in release["run"]
    assert "already published" in release["run"]
    files = _step(_PUBLISH, "the release files")["run"]
    assert "for platform in linux-x64 windows-x64 macos-arm64" in files
    assert 'SHA256SUMS-$platform"' in files


def test_permissions_are_read_at_the_top_and_write_on_publish_alone() -> None:
    assert _workflow()["permissions"] == {"contents": "read"}
    assert _jobs()[_PUBLISH]["permissions"] == {"contents": "write"}


def test_every_action_is_github_s_own_pinned_by_sha_with_its_version() -> None:
    uses_lines = [line.strip() for line in _text().splitlines() if re.match(r"\s*(- )?uses:", line)]
    assert uses_lines
    for line in uses_lines:
        match = re.fullmatch(r"(?:- )?uses: ([\w.-]+/[\w.-]+)@([0-9a-f]{40}) # v\d+\.\d+\.\d+", line)
        assert match, f"not pinned by a full SHA with its version: {line}"
        assert match.group(1) in _ALLOWED_ACTIONS, line
    assert {re.search(r"uses: ([^@]+)@", line).group(1) for line in uses_lines} == _ALLOWED_ACTIONS


def test_no_secret_and_no_cache() -> None:
    text = _text()
    assert "secrets." not in text
    assert "actions/cache" not in text
    for job in _jobs().values():
        for step in job["steps"]:
            assert "cache" not in step.get("with", {}), step


def test_checkouts_keep_no_credentials() -> None:
    for name in _jobs():
        for step in _steps(name):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False, name


class TestHostSteps:
    """The work repository's host gate steps this file reproduces, by name."""

    def test_python_is_3_11_from_setup_python_on_linux_and_windows(self) -> None:
        for name in _BUILD_JOBS:
            (setup,) = [s for s in _steps(name) if str(s.get("uses", "")).startswith("actions/setup-python@")]
            assert setup["with"] == {"python-version": "3.11"}, name

    @pytest.mark.parametrize("job", _MACOS_JOBS)
    def test_python_on_macos_is_the_prototools_release_by_proto_asserted_arm64(self, job: str) -> None:
        """setup-python's macOS build is python.org's universal2 one, which the workstation archive refuses."""
        assert not [s for s in _steps(job) if str(s.get("uses", "")).startswith("actions/setup-python@")], job
        run = _step(job, _MACOS_TOOLS_STEP)["run"]
        assert "s/^python *= *" in run and ".prototools" in run, job
        assert 'proto" install python "$PYTHON_VERSION"' in run, job
        assert 'proto" bin python "$PYTHON_VERSION"' in run, job
        assert 'tag.endswith("-arm64")' in run and "sysconfig.get_platform()" in run, job
        assert run.index("install python") < run.index('["node"]'), job
        # A symlink to the interpreter in another folder makes ``python -m venv`` record that
        # folder as the venv's home, and the venv finds no standard library: ``install`` fails.
        # proto's own bin folder goes on PATH, and a venv made from it is probed here.
        assert 'PYTHON_BIN=$(dirname "$PYTHON_EXE")' in run, job
        assert not re.search(r"\bln\b[^\n]*\$PYTHON_EXE", run), f"{job} links the interpreter elsewhere"
        assert 'python -m venv "$RUNNER_TEMP/venv-probe"' in run, job
        assert "venv-probe/bin/python\" -c 'import encodings'" in run, job
        assert run.index("venv-probe") > run.index('tag.endswith("-arm64")'), job
        names = [s.get("name") for s in _steps(job)]
        assert names.index(_MACOS_TOOLS_STEP) < names.index("install"), job

    @pytest.mark.parametrize("job", [*_BUILD_JOBS, *_MACOS_JOBS])
    def test_node_comes_from_proto_at_the_root_prototools_version(self, job: str) -> None:
        step = _step(job, _MACOS_TOOLS_STEP if job in _MACOS_JOBS else _BUILD_NODE_STEP)
        assert re.fullmatch(r"[0-9a-f]{64}", step["env"]["PROTO_CLI_SHA256"]), job
        assert 'open(".prototools", "rb"))["node"]' in step["run"], job
        assert "install node" in step["run"], job
        assert "sha256" in step["run"].lower(), job

    def test_the_four_macos_setups_are_one_copy_and_every_proto_target_has_one_checksum(self) -> None:
        def setup(job: str) -> list[dict]:
            steps = []
            for step in _steps(job):
                if step.get("name") == "install":
                    break
                if step.get("name") == _MOON_STEP:
                    continue
                kept = dict(step)
                if "run" in kept:
                    kept["run"] = "\n".join(line for line in kept["run"].splitlines() if "GITHUB_ENV" not in line)
                steps.append(kept)
            return steps

        first = setup(_MACOS_JOBS[0])
        for job in _MACOS_JOBS[1:]:
            assert setup(job) == first, f"{job}'s setup differs from {_MACOS_JOBS[0]}'s"
        sums: dict[str, set[str]] = {}
        for job in _jobs():
            for step in _steps(job):
                env = step.get("env", {})
                if "PROTO_TARGET" in env:
                    sums.setdefault(env["PROTO_TARGET"], set()).add(env["PROTO_CLI_SHA256"])
        assert set(sums) == {"x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc", "aarch64-apple-darwin"}
        assert all(len(values) == 1 for values in sums.values()), sums

    def test_every_proto_download_retries(self) -> None:
        """A transient network error on one platform's download would fail the whole release."""
        downloads = [
            line
            for job in _jobs()
            for step in _steps(job)
            for line in step.get("run", "").splitlines()
            if "proto/releases/download" in line
        ]
        assert len(downloads) >= 4
        for line in downloads:
            assert "--retry 3" in line or "-MaximumRetryCount 3" in line, line

    def test_no_tool_version_is_written_twice(self) -> None:
        root = (REPO_ROOT / ".prototools").read_text(encoding="utf-8")
        template = (
            REPO_ROOT
            / "packages"
            / "tools"
            / "src"
            / "swcraft_tools"
            / "templates"
            / "vehicle-architecture"
            / ".prototools"
        ).read_text(encoding="utf-8")
        node = re.search(r'^node = "([^"]+)"', root, re.MULTILINE).group(1)
        moon = re.search(r'^moon = "([^"]+)"', template, re.MULTILINE).group(1)
        assert node not in _text()
        assert moon not in _text()

    def test_moon_is_installed_at_the_template_version_where_the_integration_stage_runs(self) -> None:
        step = _step("macos-integration", _MOON_STEP)
        assert "packages/tools/src/swcraft_tools/templates/vehicle-architecture/.prototools" in step["run"]
        assert 'install moon "$MOON_VERSION"' in step["run"]
        names = [s.get("name") for s in _steps("macos-integration")]
        assert names.index(step["name"]) < names.index("test-model-integration")

    def test_the_integration_stage_clears_the_ci_variables_moon_reads(self) -> None:
        env = _step("macos-integration", "test-model-integration")["env"]
        assert env == {"CI": "", "GITHUB_ACTIONS": "", "GITEA_ACTIONS": ""}

    def test_windows_sets_git_s_own_long_paths(self) -> None:
        host = _step("windows", "The host this build runs on")["run"]
        assert "git config --global core.longpaths true" in host
        assert "ACP" in host and "LongPathsEnabled" in host

    @pytest.mark.parametrize("job", _MACOS_JOBS)
    def test_every_macos_job_asserts_utf8_and_ditto_sets_a_git_identity_and_requires_links(self, job: str) -> None:
        host = _step(job, "The host is the platform the gate names")["run"]
        assert "getpreferredencoding" in host and '"utf8"' in host
        assert "command -v ditto" in host
        identity = _step(job, "the git identity for tests that commit")["run"]
        assert "git config --global user.name" in identity
        assert "git config --global user.email" in identity
        assert _jobs()[job]["env"]["SWCRAFT_REQUIRE_SYMLINKS"] == "1"
        names = [s.get("name") for s in _steps(job)]
        assert names.index("the git identity for tests that commit") < names.index("install")


#: A line of a ``pwsh`` step that runs a native program: ``python``, ``git``, or a ``&`` call.
_NATIVE = re.compile(r"(?:^|[=(]\s*)(?:python|git|&)\s")


def test_every_native_command_in_a_windows_step_is_checked_unless_it_is_the_last() -> None:
    """``pwsh`` fails a step on its last native exit code alone, and ``$ErrorActionPreference`` covers none."""
    for step in _steps("windows"):
        lines = [line.strip() for line in step.get("run", "").splitlines() if line.strip()]
        for number, line in enumerate(lines[:-1]):
            if _NATIVE.search(line) and "$LASTEXITCODE" not in line:
                assert "$LASTEXITCODE" in lines[number + 1], f"{step.get('name')}: unchecked native call {line!r}"


# The publish job's shell steps, run over a stub gh

_STUB_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_LOG"
if [ "$1" = probe ]; then echo probe-ok; exit 0; fi
if [ "$1 $2" = "release view" ]; then
  case "$GH_VIEW" in
    missing) echo "release not found" >&2; exit 1 ;;
    draft) echo true ;;
    published) echo false ;;
    *) echo "HTTP 500: the server failed" >&2; exit 1 ;;
  esac
  exit 0
fi
if [ "$1" = api ]; then
  case "$GH_TAG" in
    absent) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
    error) echo "gh: Server Error (HTTP 500)" >&2; exit 1 ;;
    *) echo "commit $GH_TAG" ;;
  esac
  exit 0
fi
exit 0
"""

_SHA = "a" * 40
_OTHER_SHA = "b" * 40


class _Shell:
    """The publish job's working folder: the tree's version file, its release note, an asset, a stub gh."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "packages" / "tools" / "src" / "swcraft_tools").mkdir(parents=True)
        (root / "packages" / "tools" / "src" / "swcraft_tools" / "_version.py").write_text(
            '__version__ = "1.2.3"\n', encoding="utf-8", newline="\n"
        )
        (root / "docs" / "release-notes").mkdir(parents=True)
        (root / "docs" / "release-notes" / "1.2.3.md").write_text("# 1.2.3\n", encoding="utf-8", newline="\n")
        (root / "assets").mkdir()
        (root / "assets" / "swcraft-1.2.3-linux-x64.zip").write_bytes(b"zip")
        self.bin = root / "stub-bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(_STUB_GH, encoding="utf-8", newline="\n")
        gh.chmod(0o755)
        self.log = root / "gh.log"
        self.output = root / "github-output"

    def run(self, step: str, **env: str) -> subprocess.CompletedProcess:
        self.log.write_text("", encoding="utf-8", newline="\n")
        self.output.write_text("", encoding="utf-8", newline="\n")
        full = dict(os.environ)
        full.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "GH_LOG": str(self.log),
                "GITHUB_OUTPUT": str(self.output),
                "GITHUB_SHA": _SHA,
                "GH_REPO": "owner/name",
                "GH_TOKEN": "a-token",
                **env,
            }
        )
        return subprocess.run(["bash", "-c", step], cwd=self.root, env=full, capture_output=True, check=False)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def shell(tmp_path: Path) -> _Shell:
    """The folder, where ``bash`` runs a stub ``gh`` from ``PATH`` and ``python3``; a skip where it cannot."""
    shell = _Shell(tmp_path)
    try:
        probe = shell.run('gh probe && python3 -c "print(1)" && read -r a b <<<"x y" && echo "$b"')
    except OSError:
        pytest.skip("this host has no bash to run the publish job's steps with")
    if probe.returncode != 0 or probe.stdout.decode().split() != ["probe-ok", "1", "y"]:
        pytest.skip("this host's bash cannot run a stub gh from PATH with python3")
    return shell


def _version_step() -> str:
    return _step(_PUBLISH, "the version and the tag")["run"]


def _release_step() -> str:
    return _step(_PUBLISH, "create or update the GitHub Release")["run"]


class TestThePublishStepsRun:
    def test_a_tag_naming_the_tree_s_version_is_released(self, shell: _Shell) -> None:
        result = shell.run(_version_step(), REF_TYPE="tag", REF_NAME="v1.2.3", DRAFT="false")
        assert result.returncode == 0, result.stderr
        assert "tag=v1.2.3" in shell.output.read_text(encoding="utf-8")
        assert not shell.calls(), "a tag run asks GitHub nothing about the tag"

    def test_a_tag_naming_another_version_is_refused(self, shell: _Shell) -> None:
        result = shell.run(_version_step(), REF_TYPE="tag", REF_NAME="v1.2.4", DRAFT="false")
        assert result.returncode == 1 and b"does not name this tree's version" in result.stderr

    def test_a_branch_dispatch_with_draft_off_is_refused(self, shell: _Shell) -> None:
        result = shell.run(_version_step(), REF_TYPE="branch", REF_NAME="main", DRAFT="false")
        assert result.returncode == 1 and b"can only make a draft" in result.stderr

    @pytest.mark.parametrize(
        ("tag", "code"),
        [("absent", 0), (_SHA, 0), (_OTHER_SHA, 1), ("error", 1)],
    )
    def test_a_branch_dispatch_refuses_a_tag_that_already_names_another_commit(
        self, shell: _Shell, tag: str, code: int
    ) -> None:
        result = shell.run(_version_step(), REF_TYPE="branch", REF_NAME="main", DRAFT="true", GH_TAG=tag)
        assert result.returncode == code, result.stderr
        assert shell.calls() and shell.calls()[0].startswith("api repos/owner/name/git/ref/tags/v1.2.3")

    def test_a_missing_release_note_is_refused(self, shell: _Shell) -> None:
        (shell.root / "docs" / "release-notes" / "1.2.3.md").unlink()
        result = shell.run(_version_step(), REF_TYPE="tag", REF_NAME="v1.2.3", DRAFT="false")
        assert result.returncode == 1 and b"No release note" in result.stderr

    def _release(self, shell: _Shell, view: str, ref_type: str, draft: str) -> subprocess.CompletedProcess:
        env = {"GH_VIEW": view, "REF_TYPE": ref_type, "DRAFT": draft}
        env.update({"TAG": "v1.2.3", "VERSION": "1.2.3", "NOTE": "docs/release-notes/1.2.3.md"})
        return shell.run(_release_step(), **env)

    def test_a_branch_draft_with_no_release_is_created_as_a_draft_at_the_commit(self, shell: _Shell) -> None:
        assert self._release(shell, "missing", "branch", "true").returncode == 0
        (create,) = [call for call in shell.calls() if call.startswith("release create")]
        assert "--draft" in create and f"--target {_SHA}" in create and "--verify-tag" not in create

    def test_a_tag_with_no_release_is_created_final_on_the_tag(self, shell: _Shell) -> None:
        assert self._release(shell, "missing", "tag", "false").returncode == 0
        (create,) = [call for call in shell.calls() if call.startswith("release create")]
        assert "--verify-tag" in create and "--draft" not in create

    def test_a_draft_dispatch_never_replaces_a_published_release_s_assets(self, shell: _Shell) -> None:
        result = self._release(shell, "published", "tag", "true")
        assert result.returncode == 1 and b"already published" in result.stderr
        assert not [call for call in shell.calls() if call.startswith(("release upload", "release create"))]

    def test_a_draft_is_updated_and_stays_a_draft(self, shell: _Shell) -> None:
        assert self._release(shell, "draft", "branch", "true").returncode == 0
        calls = shell.calls()
        assert any(call.startswith("release upload v1.2.3") and "--clobber" in call for call in calls)
        assert not any("--draft=false" in call for call in calls)

    def test_a_tag_run_publishes_the_draft(self, shell: _Shell) -> None:
        assert self._release(shell, "draft", "tag", "false").returncode == 0
        assert "release edit v1.2.3 --draft=false" in shell.calls()

    def test_another_error_of_release_view_stops_everything(self, shell: _Shell) -> None:
        result = self._release(shell, "error", "tag", "false")
        assert result.returncode == 1 and b"HTTP 500" in result.stderr
        assert not [call for call in shell.calls() if not call.startswith("release view")]
