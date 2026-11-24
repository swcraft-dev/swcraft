"""Tests for snapshot.py."""

from __future__ import annotations

import dataclasses
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml
from conftest import commit_into_a_damaged_repository, commit_link_out_of_the_tree
from scratch_git import git as _git
from swcraft_tools.differ import diff_snapshots
from swcraft_tools.impact import impact_of
from swcraft_tools.model_state import ElementParseError
from swcraft_tools.snapshot import ModelSnapshot, SnapshotReadError, load_snapshot

# ---------------------------------------------------------------------------
# Helpers
#
# `_git` is `scratch_git.git`, and the `git_repo` fixture lives in conftest.py (shared
# with test_cli.py); only the snapshot-specific YAML-writing helpers stay local.
# ---------------------------------------------------------------------------


def _write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")


def _write_swc(model_root, name):
    _write_yaml(
        model_root / "swc" / f"{name}.yaml",
        {"name": name, "type": "application", "description": "test swc"},
    )


def _write_core(model_root, name, threshold):
    _write_yaml(
        model_root / "hardware" / f"{name}.yaml",
        {"name": name, "kind": "core", "threshold": threshold},
    )


def _write_memory(model_root, name, size, threshold, accessible=("CORE0",)):
    _write_yaml(
        model_root / "hardware" / f"{name}.yaml",
        {
            "name": name,
            "kind": "memory",
            "type": "RAM",
            "size": size,
            "accessible": list(accessible),
            "threshold": threshold,
        },
    )


# ---------------------------------------------------------------------------
# Working tree (rev=None)
# ---------------------------------------------------------------------------


def test_load_snapshot_working_tree_no_git(tmp_path):
    """rev=None reads model_root directly, no git involved at all."""
    model_root = tmp_path / "model"
    _write_swc(model_root, "SwcOne")

    snapshot = load_snapshot(str(model_root), rev=None)

    assert isinstance(snapshot, ModelSnapshot)
    assert [s.name for s in snapshot.swcs] == ["SwcOne"]
    assert snapshot.compositions == []
    assert snapshot.interfaces == []
    assert snapshot.implementation_types == []
    assert snapshot.connectors == []
    assert snapshot.requirements == []
    assert snapshot.tasks == []
    assert snapshot.sequences == []
    assert snapshot.hardware == []


def test_load_snapshot_working_tree_reads_hardware_layer(tmp_path):
    """The hardware layer is read from the working tree like every other one.

    Its three kinds are discriminated on `kind` and ordered by filename."""
    model_root = tmp_path / "model"
    _write_swc(model_root, "SwcOne")
    _write_yaml(model_root / "hardware" / "CPU.yaml", {"name": "CPU", "kind": "processor"})
    _write_core(model_root, "CORE0", threshold=0.7)
    _write_memory(model_root, "RAM0", size=262144, threshold=0.8)

    snapshot = load_snapshot(str(model_root), rev=None)

    assert [element.name for element in snapshot.hardware] == ["CORE0", "CPU", "RAM0"]
    assert [element.kind for element in snapshot.hardware] == ["core", "processor", "memory"]
    assert snapshot.hardware[0].threshold == 0.7
    assert snapshot.hardware[2].size == 262144
    assert snapshot.hardware[2].accessible == ["CORE0"]


def test_load_snapshot_working_tree_outside_git_repo(tmp_path):
    """rev=None must work even when model_root is not inside a git repo."""
    model_root = tmp_path / "not_a_repo" / "model"
    _write_swc(model_root, "Solo")

    snapshot = load_snapshot(str(model_root), rev=None)

    assert [s.name for s in snapshot.swcs] == ["Solo"]


# ---------------------------------------------------------------------------
# Historical revision (rev=<str>)
# ---------------------------------------------------------------------------


def test_load_snapshot_at_head_matches_committed_content(git_repo):
    repo_root, model_root = git_repo

    snapshot = load_snapshot(str(model_root), rev="HEAD")

    assert [s.name for s in snapshot.swcs] == ["SwcOne"]


def test_load_snapshot_two_revisions_show_old_and_new_values(git_repo):
    repo_root, model_root = git_repo

    old_snapshot = load_snapshot(str(model_root), rev="HEAD")
    assert old_snapshot.swcs[0].description == "test swc"

    # Modify and commit again.
    _write_yaml(
        model_root / "swc" / "SwcOne.yaml",
        {"name": "SwcOne", "type": "application", "description": "changed swc"},
    )
    _git("add", "-A", cwd=repo_root)
    _git("commit", "-m", "modify SwcOne", cwd=repo_root)

    new_snapshot = load_snapshot(str(model_root), rev="HEAD")
    assert new_snapshot.swcs[0].description == "changed swc"

    # The old revision (HEAD~1) is still exactly as committed.
    old_again = load_snapshot(str(model_root), rev="HEAD~1")
    assert old_again.swcs[0].description == "test swc"


def test_load_snapshot_reads_hardware_from_a_git_revision(git_repo):
    """The hardware layer is also read on the `git archive` path.

    A core committed in HEAD~1 and changed since must come back with its committed values, not the working tree's."""
    repo_root, model_root = git_repo
    _write_core(model_root, "CORE0", threshold=0.7)
    _git("add", "-A", cwd=repo_root)
    _git("commit", "-m", "add CORE0 budget", cwd=repo_root)

    committed = load_snapshot(str(model_root), rev="HEAD")
    assert [element.name for element in committed.hardware] == ["CORE0"]
    assert committed.hardware[0].threshold == 0.7

    # Raise the threshold in the working tree only — HEAD keeps the old value.
    _write_core(model_root, "CORE0", threshold=0.9)

    assert load_snapshot(str(model_root), rev="HEAD").hardware[0].threshold == 0.7
    assert load_snapshot(str(model_root), rev=None).hardware[0].threshold == 0.9


def test_load_snapshot_revision_without_hardware_layer_returns_empty_list(git_repo):
    """A revision predating the layer has no hardware/ directory at all.

    The snapshot is a **reader**, and a reader reports what is there: an empty
    list, not an error. That the layer is mandatory is `check-model`'s finding
    (`hardware.layer-absent`), and it has to stay possible to load a revision
    that predates the layer or `swcraft diff` could not reach across the release
    that introduced it.
    """
    repo_root, model_root = git_repo

    snapshot = load_snapshot(str(model_root), rev="HEAD")

    assert snapshot.hardware == []


def test_load_snapshot_unknown_revision_raises_value_error(git_repo):
    repo_root, model_root = git_repo

    with pytest.raises(ValueError, match="git archive failed"):
        load_snapshot(str(model_root), rev="not-a-real-revision")


def test_load_snapshot_refuses_an_option_like_revision_without_touching_the_file(git_repo, tmp_path):
    """`rev` is caller-supplied (CLI arg, MCP tool arg, LLM-authored JSON).

    `git archive --output=<path>` TRUNCATES <path> to zero bytes before exiting
    non-zero, so an unvalidated revision starting with `-` would turn a read-only
    diff into "destroy any file the user can write".
    """
    _, model_root = git_repo
    victim = tmp_path / "victim.txt"
    victim.write_text("IMPORTANT DATA\n", encoding="utf-8")

    with pytest.raises(ValueError, match="may not start with '-'"):
        load_snapshot(str(model_root), rev=f"--output={victim}")

    assert victim.read_text(encoding="utf-8") == "IMPORTANT DATA\n"


@pytest.mark.parametrize("rev", ["--output=/tmp/x", "-o/tmp/x", "--exec=whoami", "-"])
def test_load_snapshot_refuses_every_option_shaped_revision(git_repo, rev):
    _, model_root = git_repo

    with pytest.raises(ValueError, match="may not start with '-'"):
        load_snapshot(str(model_root), rev=rev)


def test_load_snapshot_passes_end_of_options_so_git_cannot_reparse_the_revision(git_repo, monkeypatch):
    """Second, independent guard: the revision is an operand in the argv itself."""
    _, model_root = git_repo
    seen: list[list[str]] = []
    real_run = subprocess.run

    def _record(argv, *args, **kwargs):
        seen.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _record)
    load_snapshot(str(model_root), rev="HEAD")

    archive_argv = [argv for argv in seen if argv[:2] == ["git", "archive"]]
    assert archive_argv == [["git", "archive", "--end-of-options", "HEAD"]]


def test_load_snapshot_non_git_directory_with_rev_raises_value_error(tmp_path):
    model_root = tmp_path / "not_a_repo" / "model"
    _write_swc(model_root, "Solo")

    with pytest.raises(ValueError, match="not a git repository"):
        load_snapshot(str(model_root), rev="HEAD")


def test_load_snapshot_missing_model_root_on_disk_raises_value_error(tmp_path):
    """A model_root missing on disk surfaces as the same ValueError family.

    When model_root doesn't currently exist on disk at all (e.g. deleted from the working tree),
    subprocess.run(cwd=model_root, ...) raises FileNotFoundError directly (not a non-zero git exit code); load_snapshot
    must surface this as a ValueError, not an unhandled traceback."""
    model_root = tmp_path / "does_not_exist" / "model"

    with pytest.raises(ValueError, match="model root not found"):
        load_snapshot(str(model_root), rev="HEAD")


def test_load_snapshot_corrupt_archive_raises_value_error(git_repo, monkeypatch):
    """An archive stdlib `tarfile` cannot parse surfaces as a ValueError.

    A `git archive` invocation that returns a byte stream `tarfile` cannot parse (mode "r|") must surface as a
    ValueError, not an unhandled `tarfile.TarError` — exercises snapshot.py's TarError except clause directly, without
    needing a genuinely corrupt real git archive."""
    repo_root, model_root = git_repo
    real_run = subprocess.run

    def _fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "archive"]:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=b"not a valid tar stream", stderr=b"")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(ValueError, match="failed to extract archive"):
        load_snapshot(str(model_root), rev="HEAD")


def test_load_snapshot_extracts_with_data_filter(git_repo, monkeypatch):
    """The archive is extracted with `filter="data"` (PEP 706).

    So path traversal, absolute paths and escaping symlinks cannot write outside the temp directory."""
    repo_root, model_root = git_repo
    real_extractall = tarfile.TarFile.extractall
    captured_kwargs: dict = {}

    def _fake_extractall(self, path=".", *args, **kwargs):
        captured_kwargs.update(kwargs)
        return real_extractall(self, path, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "extractall", _fake_extractall)

    load_snapshot(str(model_root), rev="HEAD")

    assert captured_kwargs.get("filter") == "data"


# ---------------------------------------------------------------------------
# A tool's text stays on the terminal
#
# Each refusal of `materialize_revision` that passes on what git or tarfile says
# is reached here by a real failure, no mock and no mount point: each of the
# three texts names a location on any host. `model_diff` replies `str()`, the CLI
# prints `render(full_path=True)`.
# ---------------------------------------------------------------------------


def _assert_the_two_forms(error, opening, reason, tool_fragment):
    """`str()` is exactly *opening* and *reason*; the full form is *opening* and the whole tool text."""
    assert str(error) == f"{opening}: {reason}"
    assert error.render(full_path=True) == f"{opening}: {error.tool_text}"
    assert tool_fragment in error.tool_text


def test_a_failed_rev_parse_says_its_own_sentence_and_keeps_gits_for_the_terminal(tmp_path, monkeypatch):
    model_root = tmp_path / "not_a_repo" / "model"
    _write_swc(model_root, "Solo")
    missing = tmp_path / "no_such_git_directory"
    monkeypatch.setenv("GIT_DIR", str(missing))
    monkeypatch.setenv("LC_ALL", "C")

    with pytest.raises(ValueError, match="not a git repository") as caught:
        load_snapshot(str(model_root), rev="HEAD")

    _assert_the_two_forms(
        caught.value,
        "not a git repository",
        "the model folder is in no git repository this process can read, so it has no revisions to compare",
        "fatal: not a git repository",
    )
    assert missing.name in caught.value.render(full_path=True)


def test_a_failed_git_archive_says_its_own_sentence_and_keeps_gits_for_the_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("LC_ALL", "C")
    project = tmp_path / "project"
    model_root = project / "model"
    _write_swc(model_root, "Solo")
    tree = commit_into_a_damaged_repository(project, tmp_path / "separate.git")

    with pytest.raises(ValueError, match="git archive failed") as caught:
        load_snapshot(str(model_root), rev="HEAD")

    _assert_the_two_forms(
        caught.value,
        "git archive failed for revision 'HEAD'",
        "git could not read this revision, which may be an unknown name or a damaged repository",
        "is empty",
    )
    assert tree[2:] in caught.value.render(full_path=True)


def test_a_revision_longer_than_the_command_line_is_a_revision_not_read_never_a_missing_model_root(git_repo):
    # Two million characters: past the 32,767 a Windows command line holds (error 206, which Python
    # raises as a FileNotFoundError), past Linux's 128 KiB for one argument and the Mac's 1 MiB for
    # all of them (E2BIG), so the system refuses to start git on every runner, before git reads it.
    _, model_root = git_repo
    revision = "r" * 2_000_000

    with pytest.raises(ValueError, match="could not be read") as caught:
        load_snapshot(str(model_root), rev=revision)

    _assert_the_two_forms(
        caught.value,
        f"revision '{'r' * 64}[... 1999936 more characters]' could not be read",
        "git could not be started with it, which a revision longer than the system's command line causes",
        "[Errno ",
    )
    assert "model root" not in caught.value.render(full_path=True)


def test_a_failed_extraction_says_its_own_sentence_and_keeps_tarfiles_for_the_terminal(git_repo, tmp_path):
    repo_root, model_root = git_repo
    commit_link_out_of_the_tree(repo_root, tmp_path)

    with pytest.raises(ValueError, match="failed to extract archive") as caught:
        load_snapshot(str(model_root), rev="HEAD")

    _assert_the_two_forms(
        caught.value,
        "failed to extract archive for revision 'HEAD'",
        "the revision's files could not be unpacked safely",
        "outside the destination",
    )
    # `tarfile` quotes the path by repr(), which doubles a Windows backslash: the tail, not the path.
    assert "would link to" in caught.value.tool_text


# ---------------------------------------------------------------------------
# Placement metadata
#
# The reader captures the SWC directory beside the parsed element so that
# `read` and the create/remove delta can answer `group`, strictly additively.
# ---------------------------------------------------------------------------


THE_TEN_LAYERS = (
    "compositions",
    "swcs",
    "interfaces",
    "connectors",
    "requirements",
    "tasks",
    "sequences",
    "hardware",
    "mapping_sets",
    # The three type layers' own attributes, appended in the binding's key order.
    "application_types",
    "implementation_types",
    "base_types",
    # The three layers of the forms a type names by reference, appended after
    # the type layers in the binding's key order.
    "units",
    "compu_methods",
    "data_constraints",
)


class TestPlacementIsStrictlyAdditive:
    def test_the_ten_layer_attributes_come_first_and_are_unchanged(self):
        """A consumer that knows no placement reads only these, positionally or by name."""
        names = [f.name for f in dataclasses.fields(ModelSnapshot)]

        assert tuple(names[: len(THE_TEN_LAYERS)]) == THE_TEN_LAYERS
        assert names == [*THE_TEN_LAYERS, "swc_placement"]

    def test_swc_placement_defaults_to_empty_so_every_construction_site_keeps_working(self):
        snapshot = ModelSnapshot(**{layer: [] for layer in THE_TEN_LAYERS})

        assert snapshot.swc_placement == {}

    def test_a_working_tree_snapshot_reports_the_group_of_every_component(self, tmp_path):
        model_root = tmp_path / "model"
        _write_yaml(
            model_root / "swc" / "sensors" / "SpeedSensorSWC.yaml",
            {"name": "SpeedSensorSWC", "type": "application", "description": "sensor"},
        )
        _write_swc(model_root, "FlatSWC")

        snapshot = load_snapshot(str(model_root), rev=None)

        assert snapshot.swc_placement == {
            "SpeedSensorSWC": {"group": "sensors"},
            "FlatSWC": {"group": ""},
        }
        # ... and the elements themselves stay group-free.
        assert all("group" not in swc.model_dump() for swc in snapshot.swcs)

    def test_a_revision_snapshot_reports_placement_too(self, git_repo):
        """The git path reads through the same `read_*` functions, so it must."""
        _repo_root, model_root = git_repo

        snapshot = load_snapshot(str(model_root), rev="HEAD")

        assert set(snapshot.swc_placement) == {swc.name for swc in snapshot.swcs}

    def test_placement_is_readable_but_not_diffed(self, tmp_path):
        """`group` is immutable, so a move has no delta — and the differ must not invent one.

        The two snapshots genuinely differ (that is the first assertion), and
        `ModelDiff` still reports nothing, because comparing placement is not
        something an immutable field asks of the differ.
        """
        before = tmp_path / "before"
        after = tmp_path / "after"
        payload = {"name": "MovedSWC", "type": "application", "description": "moved"}
        _write_yaml(before / "swc" / "example" / "MovedSWC.yaml", payload)
        _write_yaml(after / "swc" / "sensors" / "MovedSWC.yaml", payload)

        old = load_snapshot(str(before), rev=None)
        new = load_snapshot(str(after), rev=None)

        assert old.swc_placement != new.swc_placement
        assert old.swcs == new.swcs

        diff = diff_snapshots(old, new)

        assert diff.is_empty
        assert diff.swcs.added == [] and diff.swcs.removed == [] and diff.swcs.changed == []

    def test_impact_sees_exactly_what_it_saw_before(self, tmp_path):
        """`impact` walks every declared layer; placement is invisible to it."""
        model_root = tmp_path / "model"
        _write_yaml(
            model_root / "swc" / "sensors" / "ProducerSWC.yaml",
            {
                "name": "ProducerSWC",
                "type": "application",
                "description": "producer",
                "ports": [{"name": "pOut", "direction": "provide", "interface": "ISpeed", "description": "out"}],
            },
        )
        _write_yaml(
            model_root / "interfaces" / "ISpeed.yaml",
            {
                "name": "ISpeed",
                "kind": "sender_receiver",
                "data_elements": [{"name": "speed", "implementation_type": "uint16"}],
            },
        )

        snapshot = load_snapshot(str(model_root), rev=None)

        result = impact_of(snapshot, "ISpeed")

        assert [dep.ref.name for dep in result["interface"]] == ["ProducerSWC"]


def test_a_revision_refusal_wrapping_a_two_field_load_error_stays_one_line(tmp_path):
    """*revision … does not contain* keeps one line, the details joined by ``; ``.

    Built directly, because the loader raises no such pairing of its own today: the wrapper's
    shape is what is held, for any cause that renders itself.
    """
    cause = ElementParseError(
        "tasks/Task_bad.yaml",
        label="task",
        path=Path(tmp_path) / "model" / "tasks" / "Task_bad.yaml",
        details=("period: Input should be a finite number", "priority: Input should be a valid integer"),
    )
    error = SnapshotReadError(cause, model_root=str(tmp_path / "model"), revision="HEAD")
    joined = "period: Input should be a finite number; priority: Input should be a valid integer"
    opening = "revision 'HEAD' does not contain the model root: Invalid task YAML in tasks/Task_bad.yaml"
    assert str(error) == f"{opening}: {joined}"
    full = error.render(full_path=True)
    assert "\n" not in full and full.endswith(f"Task_bad.yaml: {joined}")
