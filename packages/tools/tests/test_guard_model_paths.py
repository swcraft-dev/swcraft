"""Tests of ``guard_model_paths``, the model guard's two rules.

A judged path holding the model folder's path, or ending like an existing model entry, is refused. The pure
functions on strings, and the model entry index on ``tmp_path`` trees. The decisions the guard builds on them are
the ``test_copilot_guard_*.py`` modules'.

What this does not reach: how a client resolves a path -- the rule needs no knowledge of it, which is its point.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from filesystem_support import accepts_path_segment
from swcraft_tools import guard_model_paths
from symlink_support import symlink_or_skip

_ONE = (("model",),)
_TWO = (("arch", "model"),)

_SUBJECT = "the model directory (`model`)"


def _tree(tmp_path: Path) -> Path:
    """``model/swc/app/Engine.yaml`` under *tmp_path*, the model root returned."""
    root = tmp_path / "model"
    (root / "swc" / "app").mkdir(parents=True)
    (root / "swc" / "app" / "Engine.yaml").write_text("name: Engine\n", encoding="utf-8", newline="\n")
    return root


def _tail(
    values: list[object], root: Path, expansions: list[str] | None = None, deadline: float | None = None
) -> str | None:
    return guard_model_paths.model_tail_reason(values, expansions or [], str(root), _SUBJECT, deadline)


def _listing(monkeypatch: pytest.MonkeyPatch, after: Callable[[int], None]) -> None:
    """``os.scandir`` for :mod:`guard_model_paths` with *after* called past each entry it yields.

    *after* receives the count of entries yielded and, with ``-1``, is called once the listing is closed."""
    scandir = os.scandir

    class Listing:
        def __init__(self, path: str) -> None:
            self.entries = scandir(path)

        def __enter__(self) -> Listing:
            return self

        def __exit__(self, *_exc: object) -> None:
            self.entries.close()
            after(-1)

        def __iter__(self) -> object:
            for count, entry in enumerate(self.entries, 1):
                yield entry
                after(count)

    monkeypatch.setattr(guard_model_paths.os, "scandir", Listing)


def _stop_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard_model_paths.time, "monotonic", lambda: float("inf"))


class TestPathSegments:
    def test_both_separators_split_and_empty_and_dot_segments_drop(self) -> None:
        assert guard_model_paths.path_segments("a\\b//./c/") == ["a", "b", "c"]

    def test_a_climb_is_kept(self) -> None:
        assert guard_model_paths.path_segments("model/../notes") == ["model", "..", "notes"]

    def test_segments_are_casefolded(self) -> None:
        assert guard_model_paths.path_segments("Arch/MODEL") == ["arch", "model"]

    def test_nothing_is_no_segment(self) -> None:
        assert guard_model_paths.path_segments("") == []
        assert guard_model_paths.path_segments("./") == []

    @pytest.mark.parametrize(
        ("text", "segments"),
        [
            ("swc./Foo.yaml.", ["swc", "foo.yaml"]),
            ("swc/Foo.yaml ", ["swc", "foo.yaml"]),
            ("swc . /Foo.yaml. .", ["swc", "foo.yaml"]),
            ("swc/Foo.yaml::$DATA", ["swc", "foo.yaml"]),
            ("swc:x/Foo.yaml:stream", ["swc", "foo.yaml"]),
            ("model::$INDEX_ALLOCATION/swc/New.yaml", ["model", "swc", "new.yaml"]),
            ("C:/x", ["c:", "x"]),
            ("C:model::$DATA\\x", ["c:", "model", "x"]),
            ("a/../b", ["a", "..", "b"]),
        ],
    )
    def test_a_name_is_read_as_windows_reads_it(self, text: str, segments: list[str]) -> None:
        """Trailing dots and spaces are dropped, and a ``:`` stream is cut from every segment, the first included.

        A leading drive is a segment of its own."""
        assert guard_model_paths.path_segments(text) == segments


class TestModelRuns:
    def test_the_written_spelling_is_normalised_lexically(self) -> None:
        assert guard_model_paths.model_runs("/p", "arch/./x/../model", "arch/model") == _TWO

    def test_a_link_gives_two_spellings(self) -> None:
        assert set(guard_model_paths.model_runs("/p", "link/model", "real/model")) == {
            ("link", "model"),
            ("real", "model"),
        }

    def test_a_model_that_is_the_project_directory_has_no_run(self) -> None:
        assert guard_model_paths.model_runs("/p", ".", None) == ()
        assert guard_model_paths.model_runs("/p", "link", None) == ()

    def test_a_project_relative_name_reads_no_drive(self) -> None:
        """``a:design`` is a folder of the project, read as Windows reads a name like every segment of a judged value.

        So the folder written inside a longer path is held."""
        runs = guard_model_paths.model_runs(os.path.abspath("/p"), "a:design", "a:design")
        assert ("a",) in runs
        assert ("c:",) not in runs
        assert guard_model_paths.holds_model_folder("zz/yy/a:design/New.yaml", runs, ())

    def test_a_folder_windows_reads_as_no_name_keeps_its_literal_run(self) -> None:
        """``...`` is no name to Windows, but a folder of that name on POSIX: its literal segments are a run."""
        runs = guard_model_paths.model_runs(os.path.abspath("/p"), "...", "...")
        assert runs == (("...",),)
        assert guard_model_paths.holds_model_folder("zz/.../New.yaml", runs, ())
        assert not guard_model_paths.holds_model_folder("zz/../New.yaml", runs, ())

    def test_a_written_root_on_another_drive_adds_no_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``os.path.relpath`` fails across Windows drives; the resolved name's run stays."""
        relpaths: list[str] = []

        def another_drive(path: str, start: str) -> str:
            relpaths.append(path)
            raise ValueError("path is on mount 'D:', start on mount 'C:'")

        monkeypatch.setattr(guard_model_paths.os.path, "relpath", another_drive)
        assert guard_model_paths.model_runs("/p", "D:/elsewhere/model", "arch/model") == _TWO
        assert relpaths


class TestOneSegmentModelFolder:
    @pytest.mark.parametrize(
        "value",
        ["model/swc/x.yaml", "zz/yy/Model/swc/x.yaml", "zz\\MODEL\\x", "model/../notes", "docs/model/notes.md"],
    )
    def test_a_value_holding_the_folder_is_held(self, value: str) -> None:
        assert guard_model_paths.holds_model_folder(value, _ONE, ())

    @pytest.mark.parametrize("value", ["notes/x.md", "model_notes/x", "models/x", "x/remodel"])
    def test_a_value_without_the_folder_is_not(self, value: str) -> None:
        assert not guard_model_paths.holds_model_folder(value, _ONE, ())


class TestTwoSegmentModelFolder:
    @pytest.mark.parametrize("value", ["zz/Arch/MODEL/x", "arch/model", "/elsewhere/arch/model/x"])
    def test_the_consecutive_run_is_held(self, value: str) -> None:
        assert guard_model_paths.holds_model_folder(value, _TWO, ())

    @pytest.mark.parametrize("value", ["notes/arch/x", "arch/x/model", "notes/model/x", "/abs/model/x", "arch/x"])
    def test_a_broken_or_partial_run_is_not(self, value: str) -> None:
        assert not guard_model_paths.holds_model_folder(value, _TWO, ())

    @pytest.mark.parametrize("value", ["D:\\model\\x", "C:model\\x"])
    def test_a_value_with_a_drive_is_not_relative(self, value: str) -> None:
        """A drive-letter path and a drive-relative one do not open with the run's last folders.

        The guard denies a drive-relative one unjudged elsewhere."""
        assert not guard_model_paths.holds_model_folder(value, _TWO, ())

    @pytest.mark.parametrize(
        "value",
        [
            "zz/arch./model /x",
            "zz/arch/model:s/x",
            "model./x",
            "arch::$INDEX_ALLOCATION/model/x",
            "model::$INDEX_ALLOCATION/swc/New.yaml",
        ],
    )
    def test_a_windows_alias_of_the_run_is_held(self, value: str) -> None:
        assert guard_model_paths.holds_model_folder(value, _TWO, ())

    @pytest.mark.parametrize("value", ["model/x.yaml", "./model", "Model\\swc\\x.yaml"])
    def test_a_relative_value_opening_with_the_last_folders_is_held(self, value: str) -> None:
        """A second workspace root nested at the model folder makes such a path a model file."""
        assert guard_model_paths.holds_model_folder(value, _TWO, ())

    def test_three_segments_open_with_any_trailing_run(self) -> None:
        runs = (("a", "b", "model"),)
        assert guard_model_paths.holds_model_folder("b/model/x", runs, ())
        assert guard_model_paths.holds_model_folder("model/x", runs, ())
        assert not guard_model_paths.holds_model_folder("a/b/x", runs, ())


class TestTheProjectPrefix:
    def test_a_project_under_a_folder_named_like_the_model_is_matched_below_it(self) -> None:
        prefixes = ("/work/model/proj",)
        assert not guard_model_paths.holds_model_folder("/work/model/proj/notes.md", _ONE, prefixes)
        assert not guard_model_paths.holds_model_folder("\\WORK\\Model\\proj\\notes.md", _ONE, prefixes)
        assert not guard_model_paths.holds_model_folder("/work/model/proj", _ONE, prefixes)
        assert guard_model_paths.holds_model_folder("/WORK/Model/proj/model/x", _ONE, prefixes)
        assert guard_model_paths.holds_model_folder("/work/model/projects/x", _ONE, prefixes)

    def test_either_prefix_serves(self) -> None:
        """The project directory resolved and as the guard found it."""
        prefixes = ("/private/w/model/proj", "/w/model/proj/")
        assert not guard_model_paths.holds_model_folder("/private/w/model/proj/x", _ONE, prefixes)
        assert not guard_model_paths.holds_model_folder("/w/model/proj/x", _ONE, prefixes)

    def test_a_climb_below_the_prefix_is_still_judged(self) -> None:
        assert guard_model_paths.holds_model_folder("/work/proj/../model/x", _ONE, ("/work/proj",))

    def test_a_value_below_the_prefix_is_not_relative(self) -> None:
        assert not guard_model_paths.holds_model_folder("/work/proj/model/x", _TWO, ("/work/proj",))
        assert guard_model_paths.holds_model_folder("/work/proj/arch/model/x", _TWO, ("/work/proj",))

    def test_a_windows_drive_path_is_matched_casefolded(self) -> None:
        assert not guard_model_paths.holds_model_folder("c:\\Work\\model\\proj\\x", _ONE, ("C:/work/model/proj",))


def _resolver(links: dict[str, str]) -> tuple[list[str], Callable[[str], str | None]]:
    """A resolution that follows *links* at the start of a path, and the paths it was asked for.

    Each link maps a written folder to its target."""
    asked: list[str] = []

    def resolve(path: str) -> str | None:
        asked.append(path)
        for link, target in links.items():
            if path == link or path.startswith(link + os.sep):
                return target + path[len(link) :]
        return path

    return asked, resolve


class TestProjectPrefixes:
    def test_the_call_directory_gives_the_project_as_spelled(self) -> None:
        root = os.path.abspath("/real/proj")
        spelled = os.path.abspath("/alias/proj")
        asked, resolve = _resolver({spelled: root})
        assert guard_model_paths.project_prefixes(root, os.path.join(spelled, "a", "b"), resolve) == [root, spelled]
        assert guard_model_paths.project_prefixes(root, spelled, resolve) == [root, spelled]
        assert asked

    def test_the_project_spelled_as_resolved_adds_nothing(self) -> None:
        root = os.path.abspath("/proj")
        _asked, resolve = _resolver({})
        assert guard_model_paths.project_prefixes(root, os.path.join(root, "a"), resolve) == [root]

    def test_a_directory_outside_the_project_or_unresolved_adds_nothing(self) -> None:
        root = os.path.abspath("/proj")
        _asked, resolve = _resolver({})
        assert guard_model_paths.project_prefixes(root, os.path.abspath("/x/y"), resolve) == [root]
        assert guard_model_paths.project_prefixes(root, os.path.abspath("/proj/a"), lambda _path: None) == [root]

    def test_a_link_deeper_into_the_project_than_its_own_path_adds_nothing(self) -> None:
        """``/l`` is ``<root>/a/b/c/d``: no folder of the call's directory as written is the project."""
        root = os.path.abspath("/w/proj")
        link = os.path.abspath("/l")
        _asked, resolve = _resolver({link: os.path.join(root, "a", "b", "c", "d")})
        assert guard_model_paths.project_prefixes(root, link, resolve) == [root]

    def test_a_link_shallower_than_its_depth_names_no_unrelated_ancestor(self) -> None:
        """``/x/l`` is ``<root>/a/b``: its ancestor ``/`` is not the project, so it is no prefix."""
        root = os.path.abspath("/w/proj")
        link = os.path.abspath("/x/l")
        _asked, resolve = _resolver({link: os.path.join(root, "a", "b")})
        assert guard_model_paths.project_prefixes(root, os.path.join(link, "c"), resolve) == [root]

    def test_the_roots_are_resolved_once_each_and_named(self) -> None:
        root, model = os.path.abspath("/w/proj"), os.path.abspath("/w/proj/arch/model")
        asked: list[str] = []
        roots = guard_model_paths.model_roots(lambda: asked.append("p") or root, lambda: asked.append("m") or model)
        assert roots == (root, model, "arch/model")
        assert asked == ["p", "m"]
        assert guard_model_paths.model_roots(lambda: root, lambda: root) == (root, root, None)
        assert guard_model_paths.model_roots(lambda: root, lambda: os.path.abspath("/w/projx")) == (
            root,
            os.path.abspath("/w/projx"),
            None,
        )

    def test_no_root_is_resolved_past_the_bound(self) -> None:
        asked: list[str] = []
        roots = guard_model_paths.model_roots(lambda: asked.append("p") or "/p", lambda: "/p/m", time.monotonic() - 1)
        assert roots is None
        assert asked == []

    def test_the_model_root_is_not_resolved_once_the_bound_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked: list[str] = []

        def slow() -> str:
            _stop_the_clock(monkeypatch)
            return "/p"

        assert guard_model_paths.model_roots(slow, lambda: asked.append("m") or "/p/m") is None
        assert asked == []

    def test_no_ancestor_is_resolved_past_the_bound(self) -> None:
        root = os.path.abspath("/real/proj")
        spelled = os.path.abspath("/alias/proj")
        asked, resolve = _resolver({spelled: root})
        directory = os.path.join(spelled, "a", "b")
        assert guard_model_paths.project_prefixes(root, directory, resolve, time.monotonic() - 1) == [root]
        assert asked == []

    def test_the_ancestors_stop_once_the_bound_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A resolution that outlasts the run's bound is the last: the ancestors above it are not resolved."""
        root = os.path.abspath("/real/proj")
        spelled = os.path.abspath("/alias/proj")
        asked, resolve = _resolver({spelled: root})

        def slow(path: str) -> str | None:
            _stop_the_clock(monkeypatch)
            return resolve(path)

        directory = os.path.join(spelled, "a", "b")
        assert guard_model_paths.project_prefixes(root, directory, slow) == [root]
        assert asked == [directory]

    def test_a_link_to_the_project_inside_the_model_is_a_prefix_only_where_it_resolves_there(self) -> None:
        root = os.path.abspath("/proj")
        link = os.path.join(root, "model", "l")
        _asked, resolve = _resolver({link: root})
        assert guard_model_paths.project_prefixes(root, link, resolve) == [root, link]
        assert guard_model_paths.project_prefixes(root, os.path.join(root, "model"), resolve) == [root]

    def test_a_link_back_to_the_project_inside_the_model_keeps_the_outer_spelling(self) -> None:
        """A ``<link>/../swc/New.yaml`` value is still matched below the outer spelling, where it holds the run.

        The project is spelled through ``/alias``, and the call's directory is a link in the model back to the
        project."""
        root = os.path.abspath("/real/proj")
        spelled = os.path.abspath("/alias/proj")
        link = os.path.join(spelled, "arch", "model", "l")
        _asked, resolve = _resolver({link: root, spelled: root})
        prefixes = guard_model_paths.project_prefixes(root, link, resolve)
        assert prefixes == [root, link, spelled]
        assert guard_model_paths.holds_model_folder(os.path.join(link, "..", "swc", "New.yaml"), _TWO, prefixes)


class TestGlobs:
    def test_only_literal_segments_are_compared(self) -> None:
        assert guard_model_paths.holds_model_folder("zz/**/model/*.yaml", _ONE, (), glob=True)
        assert not guard_model_paths.holds_model_folder("zz/mod?l/x", _ONE, (), glob=True)
        assert not guard_model_paths.holds_model_folder("zz/model*/x", _ONE, (), glob=True)
        assert not guard_model_paths.holds_model_folder("arch/*/model", _TWO, (), glob=True)

    def test_a_relative_glob_opening_with_the_last_folders_is_held(self) -> None:
        assert guard_model_paths.holds_model_folder("model/**", _TWO, (), glob=True)


class TestNoRun:
    def test_no_run_holds_nothing(self) -> None:
        assert not guard_model_paths.holds_model_folder("model/x", (), ("/p",))


def test_the_reason_names_the_model_and_no_path() -> None:
    subject = "the model directory (`arch/model`)"
    reason = guard_model_paths.model_path_reason(subject)
    assert subject in reason
    assert "/" not in reason.replace(subject, "")


class TestTheModelIndex:
    def test_files_and_folders_are_entries_under_their_parent_casefolded(self, tmp_path: Path) -> None:
        root = _tree(tmp_path)
        (root / "Docs").mkdir()
        (root / "Docs" / "ReadMe.MD").write_text("x\n", encoding="utf-8", newline="\n")
        assert guard_model_paths.model_index(str(root)) == {
            ("model", "swc"),
            ("swc", "app"),
            ("app", "engine.yaml"),
            ("model", "docs"),
            ("docs", "readme.md"),
        }

    def test_a_link_is_an_entry_and_is_not_followed(self, tmp_path: Path) -> None:
        root = _tree(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("x\n", encoding="utf-8", newline="\n")
        symlink_or_skip(root / "Link", outside)
        index = guard_model_paths.model_index(str(root))
        assert index is not None
        assert ("model", "link") in index
        assert ("link", "secret.md") not in index

    def test_a_missing_model_root_has_no_entries(self, tmp_path: Path) -> None:
        assert guard_model_paths.model_index(str(tmp_path / "absent")) == frozenset()
        (tmp_path / "file").write_text("x\n", encoding="utf-8", newline="\n")
        assert guard_model_paths.model_index(str(tmp_path / "file" / "model")) == frozenset()

    def test_a_model_root_that_cannot_be_read_makes_the_tree_unreadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a root that does not exist has no entries; one whose status is refused is not known to be empty."""
        root = str(_tree(tmp_path))
        lstat = os.lstat
        refused: list[str] = []

        def refusing(path: str) -> os.stat_result:
            if path == root:
                refused.append(path)
                raise PermissionError(13, "Permission denied")
            return lstat(path)

        monkeypatch.setattr(guard_model_paths.os, "lstat", refusing)
        monkeypatch.setattr(guard_model_paths.os.path, "lexists", lambda _path: False)
        assert guard_model_paths.model_index(root) is None
        assert refused == [root]

    def test_a_name_holding_a_backslash_is_indexed_as_a_judged_value_reads_it(self, tmp_path: Path) -> None:
        """POSIX keeps ``A\\B.yaml`` as one name; a judged value splits it, and so does the index."""
        root = _tree(tmp_path)
        if not accepts_path_segment(tmp_path, "A\\B"):
            pytest.skip("this filesystem reads a backslash as a separator")
        (root / "swc" / "app" / "A\\B.yaml").write_text("x\n", encoding="utf-8", newline="\n")
        assert ("a", "b.yaml") in (guard_model_paths.model_index(str(root)) or ())
        assert _tail(["x/A\\B.yaml"], root) == guard_model_paths.model_entry_reason(_SUBJECT)

    def test_a_name_windows_reads_as_no_name_is_indexed_literally_too(self, tmp_path: Path) -> None:
        """POSIX keeps ``:`` as a folder name that Windows reads as none.

        A value sharing the literal parent and name ends like the entry."""
        root = _tree(tmp_path)
        if not accepts_path_segment(tmp_path, ":"):
            pytest.skip("this filesystem refuses ':' in a name")
        (root / "swc" / ":").mkdir()
        (root / "swc" / ":" / "Engine.yaml").write_text("x\n", encoding="utf-8", newline="\n")
        assert (":", "engine.yaml") in (guard_model_paths.model_index(str(root)) or ())
        assert _tail(["notes/:/Engine.yaml"], root) == guard_model_paths.model_entry_reason(_SUBJECT)

    def test_a_name_is_read_as_windows_reads_it(self, tmp_path: Path) -> None:
        """An entry whose name Windows reads another way is indexed as Windows reads it, like a written path.

        POSIX keeps a trailing dot."""
        root = _tree(tmp_path)
        (root / "swc" / "Odd.yaml.").write_text("x\n", encoding="utf-8", newline="\n")
        assert ("swc", "odd.yaml") in (guard_model_paths.model_index(str(root)) or ())

    def test_past_the_entry_limit_the_tree_is_unreadable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(guard_model_paths, "_MAX_MODEL_ENTRIES", 2)
        assert guard_model_paths.model_index(str(_tree(tmp_path))) is None

    def test_at_the_entry_limit_the_tree_is_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(guard_model_paths, "_MAX_MODEL_ENTRIES", 3)
        assert guard_model_paths.model_index(str(_tree(tmp_path))) is not None

    def test_the_root_is_not_looked_at_past_the_bound(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        looked: list[str] = []
        monkeypatch.setattr(guard_model_paths.os, "lstat", looked.append)
        _stop_the_clock(monkeypatch)
        assert guard_model_paths.model_index(str(tmp_path)) is None
        assert looked == []

    def test_past_the_deadline_the_tree_is_unreadable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(guard_model_paths, "_MODEL_WALK_SECONDS", 0)
        assert guard_model_paths.model_index(str(_tree(tmp_path))) is None

    def test_past_the_callers_deadline_the_tree_is_unreadable(self, tmp_path: Path) -> None:
        """A caller with a bound of its own (a Codex judgement) hands it on, and the walk stops at it."""
        root = _tree(tmp_path)
        reason = guard_model_paths.unreadable_tree_reason(_SUBJECT)
        assert _tail(["notes/a/b.md"], root, deadline=time.monotonic() - 1) == reason
        guard_model_paths.new_run()
        assert _tail(["notes/a/b.md"], root, deadline=time.monotonic() + 60) is None

    def test_the_bound_is_read_within_a_folder(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A flat folder whose listing outlasts the bound stops at the next entry."""
        root = tmp_path / "model"
        root.mkdir()
        for name in ("a.yaml", "b.yaml", "c.yaml"):
            (root / name).write_text("x\n", encoding="utf-8", newline="\n")
        seen: list[int] = []

        def after(count: int) -> None:
            seen.append(count)
            if count == 1:
                _stop_the_clock(monkeypatch)

        _listing(monkeypatch, after)
        assert guard_model_paths.model_index(str(root)) is None
        assert seen == [1, -1]

    def test_an_index_finished_past_the_bound_is_discarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "model"
        root.mkdir()
        (root / "a.yaml").write_text("x\n", encoding="utf-8", newline="\n")
        _listing(monkeypatch, lambda count: _stop_the_clock(monkeypatch) if count == -1 else None)
        assert guard_model_paths.model_index(str(root)) is None

    def test_the_deadline_is_read_between_folders(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A walk whose time runs out once the first folder is listed scans no further folder, an empty one included.

        An empty folder reads no entry, so the bound at its entries never comes."""
        root = tmp_path / "model"
        (root / "a").mkdir(parents=True)
        (root / "b").mkdir()
        _listing(monkeypatch, lambda count: _stop_the_clock(monkeypatch) if count == -1 else None)
        listing = guard_model_paths.os.scandir
        listed: list[str] = []

        def recording(path: str) -> object:
            listed.append(path)
            return listing(path)

        monkeypatch.setattr(guard_model_paths.os, "scandir", recording)
        assert guard_model_paths.model_index(str(root)) is None
        assert listed == [str(root)]

    def test_a_folder_that_cannot_be_read_makes_the_tree_unreadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _tree(tmp_path)
        scandir = os.scandir
        refused: list[str] = []

        def refusing(path: str) -> object:
            if os.path.basename(path) == "app":
                refused.append(path)
                raise PermissionError(13, "Permission denied")
            return scandir(path)

        monkeypatch.setattr(guard_model_paths.os, "scandir", refusing)
        assert guard_model_paths.model_index(str(root)) is None
        assert refused == [str(root / "swc" / "app")]


class TestTheTailRule:
    def test_a_value_ending_like_an_entry_is_refused(self, tmp_path: Path) -> None:
        root = _tree(tmp_path)
        reason = guard_model_paths.model_entry_reason(_SUBJECT)
        assert _tail(["elsewhere/APP/engine.yaml"], root) == reason
        assert _tail(["x\\swc\\app"], root) == reason
        assert _tail(["/abs/model/swc"], root) == reason
        assert _tail([], root, ["notes/**/app/Engine.yaml"]) == reason

    def test_a_tail_no_entry_has_one_segment_or_a_glob_tail_is_not_refused(self, tmp_path: Path) -> None:
        root = _tree(tmp_path)
        assert _tail(["notes/a/b.md", "Engine.yaml", None, 3], root) is None
        assert _tail([], root, ["notes/app/*.yaml", "**/app/Engine.y?ml"]) is None

    def test_an_unreadable_tree_refuses_every_value_of_two_segments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _tree(tmp_path)
        monkeypatch.setattr(guard_model_paths, "_MAX_MODEL_ENTRIES", 0)
        reason = guard_model_paths.unreadable_tree_reason(_SUBJECT)
        assert _tail(["/abs/notes.md"], root) == reason
        assert _tail([], root, ["notes/**/*.md"]) == reason
        assert _tail(["notes.md"], root, ["*.md"]) is None

    def test_the_lockout_ends_once_the_tree_is_readable_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A later run, the tree back under the limit, judges the same path by the entries again."""
        root = _tree(tmp_path)
        with monkeypatch.context() as patched:
            patched.setattr(guard_model_paths, "_MAX_MODEL_ENTRIES", 0)
            assert _tail(["notes/a/b.md"], root) == guard_model_paths.unreadable_tree_reason(_SUBJECT)
        guard_model_paths.new_run()
        assert _tail(["notes/a/b.md"], root) is None

    def test_a_windows_alias_of_an_entrys_tail_is_refused(self, tmp_path: Path) -> None:
        root = _tree(tmp_path)
        reason = guard_model_paths.model_entry_reason(_SUBJECT)
        assert _tail(["x/app./Engine.yaml "], root) == reason
        assert _tail(["x/app/Engine.yaml::$DATA"], root) == reason

    def test_the_tree_is_walked_once_per_run_and_only_for_a_value_of_two_segments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hook process judges one call, so the index :func:`model_index` caches is one run's."""
        root = _tree(tmp_path)
        walked: list[str] = []
        scandir = os.scandir

        def counting(path: str) -> object:
            walked.append(path)
            return scandir(path)

        monkeypatch.setattr(guard_model_paths.os, "scandir", counting)
        assert _tail(["notes.md", "x"], root, ["*.md"]) is None
        assert walked == []
        _tail(["notes/a.md"], root)
        first = len(walked)
        assert first == 3
        _tail(["notes/b.md", "elsewhere/app/Engine.yaml"], root, deadline=time.monotonic() + 60)
        assert len(walked) == first

    def test_a_judgement_past_the_runs_bound_is_locked_out_whatever_the_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bound is the run's: a later judgement after it passed is refused although the index was read whole."""
        root = _tree(tmp_path)
        assert _tail(["notes/a.md"], root) is None
        assert guard_model_paths.model_index(str(root)) is not None
        _stop_the_clock(monkeypatch)
        assert _tail(["notes/a.md"], root) == guard_model_paths.unreadable_tree_reason(_SUBJECT)

    def test_the_reasons_name_the_model_and_no_path(self) -> None:
        for reason in (
            guard_model_paths.model_entry_reason(_SUBJECT),
            guard_model_paths.unreadable_tree_reason(_SUBJECT),
        ):
            assert _SUBJECT in reason
            assert "/" not in reason.replace(_SUBJECT, "")
