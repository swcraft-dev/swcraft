"""The panel review's hand-off module, alone (`swcraft_mcp.panel_review`).

The home, the two record files, the exclusive writes, the binding checks, the
wait without a deadline (heartbeat, ownership by identity, progress,
cancellation, a closed session) and the sweep. `ask` runs on a worker thread as
in production; the clocks and the sleep are injected, so a wait of hours takes
milliseconds and a callback in the sleep plays the panel. The injected clock
refuses to tick past a cap, so a wait that no longer ends fails at once.
"""

from __future__ import annotations

import concurrent.futures
import errno
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import anyio
import anyio.from_thread
import anyio.to_thread
import pytest
from swcraft_mcp import panel_review
from swcraft_tools import exclusive_create, swcraft_home
from swcraft_tools.editing.engine import ChangeSetProposal
from swcraft_tools.editing.review import delta_review, is_flat
from swcraft_tools.editing.staging import RewrittenFile

CHANGE_SET_ID = "cs-20261025T120000Z-abcdef"
OTHER_ID = "cs-20261025T120000Z-123456"
QUESTION = "Review this change set before it is committed.\n\nCommit exactly this delta?"
WALL = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC).timestamp()
#: A wait token for records the tests build themselves; `ask` draws its own.
WAIT = "0f" * 16
WAIT_PATTERN = re.compile(r"[0-9a-f]{32}")


def _proposal(change_set_id: str = CHANGE_SET_ID, intent: str | None = "Add a speed port") -> ChangeSetProposal:
    return ChangeSetProposal(change_set_id=change_set_id, baseline="sha256:base", intent=intent, delta=())


#: More ticks than any test's wait takes (a day of injected time is 345 600):
#: a wait that no longer ends fails here, at once, instead of at pytest-timeout.
TICK_CAP = 400_000


class _Clock:
    """A monotonic clock that the injected sleep advances, with a panel hook per tick."""

    def __init__(self, panel: Callable[[int], None] | None = None) -> None:
        self.now = 0.0
        self.ticks = 0
        self._panel = panel

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.ticks += 1
        if self.ticks > TICK_CAP:
            raise AssertionError(f"the wait did not end within {TICK_CAP} ticks")
        self.now += seconds
        if self._panel is not None:
            self._panel(self.ticks)


def _run_ask(
    manifest: Path,
    *,
    panel: Callable[[int], None] | None = None,
    kind: str = "delta",
    proposal: ChangeSetProposal | None = None,
    question: str = QUESTION,
    wall: Callable[[], float] = lambda: WALL,
    client: str | None = None,
    timer: _Clock | None = None,
    progress: Callable[[str], None] | None = None,
    session_closed: Callable[[], bool] | None = None,
) -> tuple[bool, list[str], _Clock]:
    """Run `ask` on a worker thread, as the engine does, and return its answer and notes.

    *timer* is passed when the test reads the injected clock itself (a wall
    clock that follows it); otherwise one is built around *panel*.
    """
    notes: list[str] = []
    timer = timer or _Clock(panel)

    async def main() -> bool:
        cancelled = anyio.get_cancelled_exc_class()
        return await anyio.to_thread.run_sync(
            partial(
                panel_review.ask,
                question,
                kind=kind,
                proposal=proposal or _proposal(),
                project_name="test-project",
                manifest=manifest,
                notes=notes.append,
                cancelled_exc=cancelled,
                client=client,
                progress=progress,
                session_closed=session_closed,
                clock=timer.clock,
                wall=wall,
                sleep=timer.sleep,
            )
        )

    return anyio.run(main), notes, timer


def _directory() -> Path:
    return panel_review.reviews_dir()


def _pending(kind: str = "delta", change_set_id: str = CHANGE_SET_ID) -> Path:
    return _directory() / panel_review.pending_name(change_set_id, kind)


def _decision_path(kind: str = "delta", change_set_id: str = CHANGE_SET_ID) -> Path:
    return _directory() / panel_review.decision_name(change_set_id, kind)


def _decision_for(record: dict, decision: str = "approve") -> dict:
    return {
        "format": record["format"],
        "change_set_id": record["change_set_id"],
        "question": record["question"],
        "wait": record["wait"],
        "digest": record["digest"],
        "decision": decision,
    }


def _write_json(path: Path, document: object) -> None:
    path.write_text(json.dumps(document) + "\n", encoding="utf-8", newline="\n")


def _panel_answers(
    decision: str = "approve", kind: str = "delta", overrides: dict | None = None
) -> Callable[[int], None]:
    """A panel that reads the pending record on the first tick and writes its decision."""

    def panel(tick: int) -> None:
        if tick != 1:
            return
        record = json.loads(_pending(kind).read_text(encoding="utf-8"))
        _write_json(_decision_path(kind), {**_decision_for(record, decision), **(overrides or {})})

    return panel


def _panel_links(document: dict) -> bool:
    """Write a decision as the panel's `writeDecision` does: an exclusive link, the first answer winning.

    Returns whether the link was taken; `False` is the tab's "already answered".
    """
    tmp = _directory() / f".{_decision_path().name}.{os.getpid()}.panel.tmp"
    tmp.write_text(json.dumps(document) + "\n", encoding="utf-8", newline="\n")
    try:
        os.link(tmp, _decision_path())
    except FileExistsError:
        return False
    finally:
        tmp.unlink()
    return True


def _observe_then_reject(observe: Callable[[], None]) -> Callable[[int], None]:
    """A panel that observes the record on the first tick and then answers it, so the wait ends."""
    reject = _panel_answers("reject")

    def panel(tick: int) -> None:
        if tick == 1:
            observe()
        reject(tick)

    return panel


def _panel_writes_bytes(payload: bytes) -> Callable[[int], None]:
    def panel(tick: int) -> None:
        if tick == 1:
            _decision_path().write_bytes(payload)

    return panel


@pytest.fixture()
def manifest(tmp_path: Path) -> Path:
    return tmp_path / "swcraft.yaml"


def _mode_bits_are_stored(directory: Path) -> bool:
    """Whether this host stores a file's full mode bits (Windows keeps one read-only flag)."""
    probe = directory / ".mode-probe"
    probe.write_text("", encoding="utf-8", newline="\n")
    try:
        probe.chmod(0o600)
        first = stat.S_IMODE(probe.stat().st_mode) == 0o600
        probe.chmod(0o640)
        second = stat.S_IMODE(probe.stat().st_mode) == 0o640
        return first and second
    finally:
        probe.unlink()


def _require_mode_bits(directory: Path) -> None:
    if not _mode_bits_are_stored(directory):
        pytest.skip("this host's filesystem does not store POSIX mode bits")


# --- the home ------------------------------------------------------------------


class TestTheHome:
    """The home rule is ``swcraft_tools.swcraft_home``'s; its literal cases are tested there."""

    def test_the_home_rule_is_the_leafs(self) -> None:
        """The server holds no copy of the rule: it is ``swcraft_tools.swcraft_home``'s, which the guard reads too."""
        assert panel_review.reviews_location is swcraft_home.reviews_location
        assert not hasattr(panel_review, "swcraft_home"), "the home is imported from the leaf, never re-exported"

    def test_live_pending_records_is_the_panels_rule(self, tmp_path: Path) -> None:
        """``live_pending_records`` lists what the panel lists.

        Listed: a ``*.pending.json`` not opening with ``.``, younger than STALE_SECONDS, a JSON object, of the
        manifest asked for; not listed: a stale one, a dot file, another suffix, undecodable text, a list, another
        project's -- and an unreadable directory lists nothing.
        """
        reviews = tmp_path / "reviews"
        reviews.mkdir()
        own, other = "/p/swcraft.yaml", "/q/swcraft.yaml"

        def record(name: str, manifest: str, age: float = 0.0, text: str | None = None) -> Path:
            path = reviews / name
            body = {"change_set_id": name.split(".")[0], "project": {"name": "p", "manifest": manifest}}
            path.write_text(json.dumps(body) if text is None else text, encoding="utf-8", newline="\n")
            stamp = path.stat().st_mtime - age
            os.utime(path, (stamp, stamp))
            return path

        live = record("cs-live.delta.pending.json", own)
        now = live.stat().st_mtime + 1
        record("cs-stale.delta.pending.json", own, age=panel_review.STALE_SECONDS + 5)
        record(".cs-dot.delta.pending.json", own)
        record("cs-tmp.delta.pending.json.tmp", own)
        record("cs-bad.delta.pending.json", own, text="{")
        record("cs-list.delta.pending.json", own, text="[]")
        record("cs-other.delta.pending.json", other)
        listed = panel_review.live_pending_records(reviews, now, own)
        assert [entry["change_set_id"] for entry in listed] == ["cs-live"]
        everyone = panel_review.live_pending_records(reviews, now)
        assert [entry["change_set_id"] for entry in everyone] == ["cs-live", "cs-other"]
        assert panel_review.live_pending_records(tmp_path / "absent", now) == []

    def test_reviews_dir_is_under_the_redirected_home(self) -> None:
        # The conftest's redirect is what keeps every test out of the real home.
        user_home = Path(os.environ["HOME"])
        expected = (
            Path(os.environ["LOCALAPPDATA"]) / "SWCraft" / "reviews"
            if os.name == "nt"
            else user_home / ".swcraft" / "reviews"
        )
        directory = panel_review.reviews_dir()
        assert directory.as_posix() == expected.as_posix()
        assert directory.is_dir()

    def test_the_conftest_home_is_a_temporary_one(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        base = tmp_path_factory.getbasetemp().resolve()
        assert panel_review.reviews_dir().resolve().is_relative_to(base)

    def test_reviews_dir_is_private(self) -> None:
        directory = panel_review.reviews_dir()
        _require_mode_bits(directory)
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


# --- the records -----------------------------------------------------------------


class TestThePendingRecord:
    def test_its_keys_their_order_and_values(self, manifest: Path) -> None:
        record = panel_review.pending_record(
            QUESTION,
            kind="delta",
            wait=WAIT,
            proposal=_proposal(),
            project_name="test-project",
            manifest=manifest.resolve().as_posix(),
            now=WALL,
        )
        assert list(record) == [
            "format",
            "change_set_id",
            "question",
            "wait",
            "baseline",
            "project",
            "client",
            "intent",
            "text",
            "digest",
            "created",
        ]
        assert record["format"] == 2
        assert record["change_set_id"] == CHANGE_SET_ID
        assert record["question"] == "delta"
        assert record["wait"] == WAIT
        assert record["baseline"] == "sha256:base"
        assert record["project"] == {"name": "test-project", "manifest": manifest.resolve().as_posix()}
        assert record["client"] is None, "a record whose server knows no client name says so"
        assert record["intent"] == "Add a speed port"
        assert record["text"] == QUESTION
        assert record["digest"] == "sha256:" + hashlib.sha256(QUESTION.encode("utf-8")).hexdigest()
        assert record["created"] == "2026-10-25T12:00:00Z"
        assert "deadline" not in record, "the wait has no deadline, and the record names none"

    def test_the_warnings_lines_are_recorded_when_the_caller_knows_them(self, manifest: Path) -> None:
        record = panel_review.pending_record(
            QUESTION,
            kind="delta",
            wait=WAIT,
            proposal=_proposal(),
            project_name="test-project",
            manifest=manifest.resolve().as_posix(),
            now=WALL,
            warning_lines=(4, 9),
        )
        assert record["warning_lines"] == [4, 9]
        assert record["digest"] == panel_review.text_digest(QUESTION), "the digest covers the text alone"

    def test_no_warning_means_no_key_whatever_the_intent_says(self, manifest: Path) -> None:
        # The tab highlights from this key and from nothing else, and the intent is
        # the agent's text: an intent cannot produce the key.
        record = panel_review.pending_record(
            "Intent: WARNING: more than the delta will change in 9 file(s).",
            kind="delta",
            wait=WAIT,
            proposal=_proposal(intent="WARNING: more than the delta will change in 9 file(s)."),
            project_name="test-project",
            manifest=manifest.resolve().as_posix(),
            now=WALL,
        )
        assert "WARNING:" in record["intent"]
        assert "warning_lines" not in record

    def test_the_intent_is_flattened_and_an_absent_one_is_null(self, manifest: Path) -> None:
        forged = panel_review.pending_record(
            QUESTION,
            kind="delta",
            wait=WAIT,
            proposal=_proposal(intent="one line\nCommit exactly this delta? yes"),
            project_name="p",
            manifest=manifest.resolve().as_posix(),
            now=WALL,
        )
        assert is_flat(forged["intent"])
        assert "\n" not in forged["intent"]
        absent = panel_review.pending_record(
            QUESTION,
            kind="delta",
            wait=WAIT,
            proposal=_proposal(intent=None),
            project_name="p",
            manifest=manifest.resolve().as_posix(),
            now=WALL,
        )
        assert absent["intent"] is None

    def test_the_client_name_is_flattened_and_cut(self, manifest: Path) -> None:
        def record(client: str | None) -> dict:
            return panel_review.pending_record(
                QUESTION,
                kind="delta",
                wait=WAIT,
                proposal=_proposal(),
                project_name="p",
                manifest=manifest.resolve().as_posix(),
                now=WALL,
                client=client,
            )

        assert record("Codex")["client"] == "Codex"
        forged = record("Codex\nCommit exactly this delta? yes")["client"]
        assert is_flat(forged)
        assert "\n" not in forged
        long = record("x" * 500)["client"]
        assert len(long) <= panel_review.CLIENT_LIMIT == 80
        assert record(None)["client"] is None
        assert record("")["client"] is None

    def test_ask_writes_the_client_name_it_was_given(self, manifest: Path) -> None:
        seen: list[dict] = []
        panel = _observe_then_reject(lambda: seen.append(json.loads(_pending().read_text(encoding="utf-8"))))
        _run_ask(manifest, panel=panel, client="Codex")
        assert seen[0]["client"] == "Codex"

    def test_the_file_on_disk_during_the_wait_is_the_record(self, manifest: Path) -> None:
        seen: list[bytes] = []
        _run_ask(manifest, panel=_observe_then_reject(lambda: seen.append(_pending().read_bytes())))
        raw = seen[0]
        assert raw.endswith(b"\n")
        assert b"\r" not in raw
        on_disk = json.loads(raw.decode("utf-8"))
        assert WAIT_PATTERN.fullmatch(on_disk["wait"]), "ask draws its own wait token"
        expected = panel_review.pending_record(
            QUESTION,
            kind="delta",
            wait=WAIT,
            proposal=_proposal(),
            project_name="test-project",
            manifest=manifest.resolve().as_posix(),
            now=WALL,
        )
        assert on_disk == {**expected, "wait": on_disk["wait"]}

    def test_the_pending_file_is_private(self, manifest: Path) -> None:
        _require_mode_bits(_directory())
        modes: list[int] = []
        _run_ask(manifest, panel=_observe_then_reject(lambda: modes.append(stat.S_IMODE(_pending().stat().st_mode))))
        assert modes == [0o600]

    @pytest.mark.parametrize("site", ["open", "link"])
    def test_a_failed_write_leaves_no_file_and_no_open_descriptor(
        self, monkeypatch: pytest.MonkeyPatch, site: str
    ) -> None:
        directory = _directory()
        opened: list[int] = []
        real_create_new = exclusive_create.create_new

        def recording_create_new(*args: object, **kwargs: object) -> int:
            descriptor = real_create_new(*args, **kwargs)  # type: ignore[arg-type]
            opened.append(descriptor)
            return descriptor

        def failing(*args: object, **kwargs: object) -> None:
            raise OSError(errno.ENOSPC, "No space left on device", (directory / "x").as_posix())

        monkeypatch.setattr(exclusive_create, "create_new", recording_create_new)
        if site == "open":
            # The builtin `open` over `create_new`'s descriptor, in the shared helper (`open_new`).
            monkeypatch.setattr(exclusive_create, "open", failing, raising=False)
        else:
            monkeypatch.setattr(panel_review.os, site, failing)
        with pytest.raises(OSError):
            panel_review.write_exclusively(directory / panel_review.pending_name(CHANGE_SET_ID, "delta"), {"a": 1})
        assert list(directory.iterdir()) == []
        # The descriptor `create_new` returned is closed: an open one makes the
        # removal a sharing violation on Windows, which leaves the `.tmp` behind.
        assert len(opened) == 1
        with pytest.raises(OSError):
            os.fstat(opened[0])

    def test_the_temporary_file_is_created_by_the_binary_exclusive_create(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On Windows a CRT text-mode descriptor turns `newline="\n"` into CRLF; `create_new`'s is binary.
        modes_seen: list[int] = []
        real_create_new = exclusive_create.create_new

        def recording_create_new(path: Path, mode: int = 0o666) -> int:
            modes_seen.append(mode)
            return real_create_new(path, mode)

        monkeypatch.setattr(exclusive_create, "create_new", recording_create_new)
        directory = _directory()
        target = directory / panel_review.pending_name(CHANGE_SET_ID, "delta")
        assert panel_review.write_exclusively(target, {"a": 1})
        assert modes_seen == [0o600]
        assert target.read_bytes() == panel_review.record_json({"a": 1}).encode("utf-8")

    def test_the_temporary_names_match_neither_glob(self, monkeypatch: pytest.MonkeyPatch) -> None:
        names: list[str] = []
        real_link = os.link

        def recording(source: Path, target: Path) -> None:
            names.append(source.name)
            real_link(source, target)

        monkeypatch.setattr(panel_review.os, "link", recording)
        directory = _directory()
        assert panel_review.write_exclusively(directory / panel_review.pending_name(CHANGE_SET_ID, "delta"), {"a": 1})
        assert len(names) == 1
        assert re.fullmatch(rf"\.{CHANGE_SET_ID}\.delta\.pending\.json\.{os.getpid()}\.[0-9a-f]{{16}}\.tmp", names[0])
        assert [path.name for path in directory.iterdir()] == [panel_review.pending_name(CHANGE_SET_ID, "delta")]
        for name in names:
            assert not fnmatch.fnmatch(name, "*.pending.json")
            assert not fnmatch.fnmatch(name, "*.decision.json")

    def test_every_write_draws_a_temporary_name_of_its_own(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Unique per write, not per process: two asks of one server share a pid.
        names: list[str] = []
        real_link = os.link

        def recording(source: Path, target: Path) -> None:
            names.append(Path(source).name)
            real_link(source, target)

        monkeypatch.setattr(panel_review.os, "link", recording)
        directory = _directory()
        target = directory / panel_review.pending_name(CHANGE_SET_ID, "delta")
        created = panel_review.write_exclusively(target, {"a": 1})
        assert created is not None
        assert os.path.samestat(created, os.stat(target)), "the identity of the file it created"
        assert panel_review.write_exclusively(target, {"a": 2}) is None
        assert len(names) == 2
        assert names[0] != names[1]

    def test_an_existing_temporary_name_is_never_opened_or_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The exclusive create: a file already at the temporary name (another
        # write's, linked to its final record) is neither truncated nor removed.
        monkeypatch.setattr(panel_review.secrets, "token_hex", lambda _n: "ab" * 8)
        directory = _directory()
        target = directory / panel_review.pending_name(CHANGE_SET_ID, "delta")
        held = directory / f".{target.name}.{os.getpid()}.{'ab' * 8}.tmp"
        held.write_bytes(b"another write's record\n")
        with pytest.raises(FileExistsError):
            panel_review.write_exclusively(target, {"a": 1})
        assert held.read_bytes() == b"another write's record\n"
        assert not target.exists()

    def test_an_existing_name_is_never_replaced(self) -> None:
        directory = _directory()
        target = directory / panel_review.pending_name(CHANGE_SET_ID, "delta")
        target.write_bytes(b"the first writer's record\n")
        assert panel_review.write_exclusively(target, {"a": 1}) is None
        assert target.read_bytes() == b"the first writer's record\n"
        assert [path.name for path in directory.iterdir()] == [target.name], "the temporary file is removed"


# --- the outcomes ------------------------------------------------------------------


class TestTheOutcomes:
    def test_approve_commits(self, manifest: Path) -> None:
        answer, notes, _ = _run_ask(manifest, panel=_panel_answers("approve"))
        assert answer is True
        assert notes == []

    def test_reject_declines_without_a_note(self, manifest: Path) -> None:
        answer, notes, _ = _run_ask(manifest, panel=_panel_answers("reject"))
        assert answer is False
        assert notes == []

    def test_the_safety_question_has_its_own_files(self, manifest: Path) -> None:
        answer, notes, _ = _run_ask(manifest, kind="safety", panel=_panel_answers("approve", kind="safety"))
        assert (answer, notes) == (True, [])

    @pytest.mark.parametrize(
        "overrides",
        [
            {"digest": "sha256:" + "0" * 64},
            {"question": "safety"},
            {"change_set_id": OTHER_ID},
            {"wait": "0" * 32},
            {"wait": None},
            {"format": 1},
            {"format": True},
            {"format": "2"},
            {"decision": "maybe"},
            {"decision": 1},
            {"digest": None},
            {"extra": "key"},
        ],
        ids=lambda o: "-".join(f"{k}={v!r}" for k, v in o.items()),
    )
    def test_a_mismatched_decision_abandons(self, manifest: Path, overrides: dict) -> None:
        answer, notes, _ = _run_ask(manifest, panel=_panel_answers("approve", overrides=overrides))
        assert answer is False
        assert notes == [panel_review.MISMATCH_NOTE]

    def test_a_decision_missing_a_key_abandons(self, manifest: Path) -> None:
        def panel(tick: int) -> None:
            if tick == 1:
                record = json.loads(_pending().read_text(encoding="utf-8"))
                decision = _decision_for(record)
                del decision["digest"]
                _write_json(_decision_path(), decision)

        answer, notes, _ = _run_ask(manifest, panel=panel)
        assert (answer, notes) == (False, [panel_review.MISMATCH_NOTE])

    @pytest.mark.parametrize(
        "payload",
        [b"not json at all", b"\xff\xfe\x00garbage", b'["approve"]', b'"approve"', b"null", b"[" * 200_000],
        ids=["not-json", "invalid-utf8", "a-list", "a-string", "null", "nested-past-the-recursion-limit"],
    )
    def test_a_decision_that_is_not_a_record_abandons_and_never_raises(self, manifest: Path, payload: bytes) -> None:
        answer, notes, _ = _run_ask(manifest, panel=_panel_writes_bytes(payload))
        assert (answer, notes) == (False, [panel_review.MISMATCH_NOTE])

    def test_an_answer_after_thirty_minutes_is_taken(self, manifest: Path) -> None:
        # No deadline: a question waits until it is answered.
        thirty_minutes = int(30 * 60 / panel_review.POLL_SECONDS)
        approve = _panel_answers("approve")
        answer, notes, timer = _run_ask(manifest, panel=lambda tick: approve(tick - thirty_minutes + 1))
        assert (answer, notes) == (True, [])
        assert timer.now >= 30 * 60
        assert list(_directory().iterdir()) == []

    def test_a_wall_clock_jump_of_hours_ends_nothing(self, manifest: Path) -> None:
        # A machine sleep: the wall clock jumps six hours while the monotonic
        # one stands still. The wait goes on, and the answer after it is taken.
        approve = _panel_answers("approve")
        timer = _Clock(lambda tick: approve(tick - 19))

        def wall() -> float:
            return WALL + (6 * 3600 if timer.ticks >= 10 else 0)

        answer, notes, _ = _run_ask(manifest, timer=timer, wall=wall)
        assert (answer, notes) == (True, [])
        assert timer.ticks == 20
        assert list(_directory().iterdir()) == []

    def test_an_id_the_engine_did_not_allocate_is_never_a_file_name(self, manifest: Path) -> None:
        answer, notes, _ = _run_ask(manifest, proposal=_proposal(change_set_id="../../escape"))
        assert answer is False
        assert notes == [panel_review.handoff_note("invalid change-set id")]
        assert list(_directory().parent.parent.rglob("*.json")) == []

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            ("approve", (True, [])),
            ("reject", (False, [])),
            ("mismatch", (False, [panel_review.MISMATCH_NOTE])),
            ("session-closed", (False, [panel_review.SESSION_CLOSED_NOTE])),
        ],
    )
    def test_every_exit_removes_both_files(
        self, manifest: Path, outcome: str, expected: tuple[bool, list[str]]
    ) -> None:
        # Only the session-closed case has a probe: an answer path that broke
        # must not end by the closed session and still pass.
        panel = {
            "approve": _panel_answers("approve"),
            "reject": _panel_answers("reject"),
            "mismatch": _panel_answers("approve", overrides={"digest": "sha256:x"}),
            "session-closed": None,
        }[outcome]
        timer = _Clock(panel)
        closed = (lambda: timer.ticks >= 3) if outcome == "session-closed" else None
        answer, notes, _ = _run_ask(manifest, timer=timer, session_closed=closed)
        assert (answer, notes) == expected
        assert list(_directory().iterdir()) == []


class _NeverRaised(BaseException):
    """A cancellation class for an ask that never reaches its wait loop."""


class TestTheWaitToken:
    """Every ask draws a wait token of its own, and a decision must carry it.

    A tab that read an earlier wait's record under the same name, with the same
    text, writes that wait's token; the later wait refuses it.
    """

    def test_every_ask_draws_a_fresh_token(self, manifest: Path) -> None:
        tokens: list[str] = []

        def panel(tick: int) -> None:
            if tick == 1:
                tokens.append(json.loads(_pending().read_text(encoding="utf-8"))["wait"])
                _write_json(
                    _decision_path(), _decision_for(json.loads(_pending().read_text(encoding="utf-8")), "reject")
                )

        _run_ask(manifest, panel=panel)
        _run_ask(manifest, panel=panel)
        assert len(tokens) == 2
        assert all(WAIT_PATTERN.fullmatch(token) for token in tokens)
        assert tokens[0] != tokens[1]

    def test_a_decision_on_another_waits_record_is_refused_with_an_equal_digest(self, manifest: Path) -> None:
        # The earlier wait's record: same id, same question, same text (so the
        # same digest), another project and baseline. Its approval is linked
        # after this ask cleared stale decisions; it still answers nothing.
        earlier = panel_review.pending_record(
            QUESTION,
            kind="delta",
            wait=WAIT,
            proposal=ChangeSetProposal(change_set_id=CHANGE_SET_ID, baseline="sha256:other", intent=None, delta=()),
            project_name="another-project",
            manifest=(manifest.parent / "elsewhere" / "swcraft.yaml").as_posix(),
            now=WALL - 1,
        )

        def panel(tick: int) -> None:
            if tick == 1:
                own = json.loads(_pending().read_text(encoding="utf-8"))
                assert own["digest"] == earlier["digest"]
                assert own["wait"] != earlier["wait"]
                _write_json(_decision_path(), _decision_for(earlier, "approve"))

        answer, notes, _ = _run_ask(manifest, panel=panel)
        assert (answer, notes) == (False, [panel_review.MISMATCH_NOTE])
        assert list(_directory().iterdir()) == []

    def test_two_asks_of_one_process_drawing_one_name_never_touch_each_others_record(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The second ask runs right after the first one's link, while the
        # first's temporary file still exists: with a per-process temporary
        # name its truncating open rewrote the first's record (one inode).
        other_manifest = manifest.parent / "elsewhere" / "swcraft.yaml"
        other_manifest.parent.mkdir()
        real_link = os.link
        second: list[tuple[bool, list[str]]] = []

        def link_then_race(source: Path, target: Path) -> None:
            real_link(source, target)
            if second or not Path(target).name.endswith(panel_review.PENDING_SUFFIX):
                return
            notes: list[str] = []
            second.append((False, notes))
            answer = panel_review.ask(
                QUESTION,
                kind="delta",
                proposal=ChangeSetProposal(change_set_id=CHANGE_SET_ID, baseline="sha256:other", intent=None, delta=()),
                project_name="another-project",
                manifest=other_manifest,
                notes=notes.append,
                # The later ask is refused before its wait loop, the one place
                # that names the class; resolving the backend's here would
                # raise, on a worker thread.
                cancelled_exc=_NeverRaised,
                wall=lambda: WALL,
            )
            second[0] = (answer, notes)

        monkeypatch.setattr(panel_review.os, "link", link_then_race)
        seen: list[dict] = []

        def panel(tick: int) -> None:
            if tick == 1:
                record = json.loads(_pending().read_text(encoding="utf-8"))
                seen.append(record)
                _write_json(_decision_path(), _decision_for(record, "approve"))

        answer, notes, _ = _run_ask(manifest, panel=panel)
        assert second == [(False, [panel_review.NAME_TAKEN_NOTE])]
        assert seen[0]["project"]["manifest"] == manifest.resolve().as_posix()
        assert seen[0]["baseline"] == "sha256:base"
        assert (answer, notes) == (True, [])
        assert list(_directory().iterdir()) == []


class TestANameAnotherWaitHolds:
    """Two servers that drew the same change-set id in the same second.

    The hand-off creates its pending record exclusively, so the later ask
    abandons and the earlier wait's files stay.
    """

    @staticmethod
    def _the_other_wait(manifest: Path, *, text: str = QUESTION, decision: str | None = None) -> dict:
        other = panel_review.pending_record(
            text,
            kind="delta",
            wait=WAIT,
            proposal=_proposal(),
            project_name="another-project",
            manifest=(manifest.parent / "elsewhere" / "swcraft.yaml").as_posix(),
            now=WALL,
        )
        _pending().write_text(panel_review.record_json(other), encoding="utf-8", newline="\n")
        if decision is not None:
            _write_json(_decision_path(), _decision_for(other, decision))
        return other

    def test_a_taken_pending_name_abandons_with_the_note_and_leaves_the_file(self, manifest: Path) -> None:
        self._the_other_wait(manifest)
        before = _pending().read_bytes()
        answer, notes, timer = _run_ask(manifest, panel=_panel_answers("approve"))
        assert (answer, notes) == (False, [panel_review.NAME_TAKEN_NOTE])
        assert timer.ticks == 0
        assert _pending().read_bytes() == before
        assert [path.name for path in _directory().iterdir()] == [_pending().name]

    def test_the_other_waits_decision_is_never_removed(self, manifest: Path) -> None:
        # The same text, so the other wait's approval would carry this one's
        # digest: it is neither taken as this ask's answer nor deleted.
        self._the_other_wait(manifest, decision="approve")
        decision = _decision_path().read_bytes()
        answer, notes, _ = _run_ask(manifest)
        assert (answer, notes) == (False, [panel_review.NAME_TAKEN_NOTE])
        assert _decision_path().read_bytes() == decision
        assert _pending().exists()

    def test_a_name_freed_after_the_cleanup_is_taken_again(self, manifest: Path) -> None:
        answer, notes, _ = _run_ask(manifest, panel=_panel_answers("reject"))
        assert (answer, notes) == (False, [])
        answer, notes, _ = _run_ask(manifest, panel=_panel_answers("approve"))
        assert (answer, notes) == (True, [])

    def test_the_cleanup_removes_the_decision_before_the_pending_record(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Once the pending record is gone another server may take the name; a
        # decision at it is then that server's, so this one goes first.
        removed: list[str] = []
        real_unlink = Path.unlink

        def recording(self: Path, missing_ok: bool = False) -> None:
            if self.name.startswith(CHANGE_SET_ID):
                removed.append(self.name)
            real_unlink(self, missing_ok=missing_ok)

        armed: list[bool] = []
        approve = _panel_answers("approve")

        def panel(tick: int) -> None:
            approve(tick)
            if not armed:
                armed.append(True)
                monkeypatch.setattr(Path, "unlink", recording)

        answer, _, _ = _run_ask(manifest, panel=panel)
        assert answer is True
        assert removed == [_decision_path().name, _pending().name]

    def test_a_link_the_filesystem_refuses_is_a_hand_off_failure(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No fallback: the panel's decision writer links too.
        def failing(source: object, target: object) -> None:
            raise OSError(errno.EPERM, "Operation not permitted", os.fspath(target))

        monkeypatch.setattr(panel_review.os, "link", failing)
        answer, notes, _ = _run_ask(manifest)
        assert (answer, notes) == (False, [panel_review.handoff_note("EPERM")])
        assert list(_directory().iterdir()) == []

    def test_the_note_names_no_location(self) -> None:
        _assert_no_path([panel_review.NAME_TAKEN_NOTE], Path.home())
        assert panel_review.NAME_TAKEN_NOTE.startswith(
            "the change set was abandoned without a human answer: SWCRAFT_REVIEW=panel is set"
        )


class TestAHomeInsideTheProject:
    """The project folder holding the SWCraft home refuses the hand-off.

    An agent confined to the project folder could otherwise read the pending
    record and write a matching approval. A client workspace wider than the
    manifest's directory is not reached here.
    """

    @staticmethod
    def _home_at(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
        home.mkdir(parents=True, exist_ok=True)
        for variable in ("HOME", "LOCALAPPDATA", "USERPROFILE"):
            monkeypatch.setenv(variable, os.fspath(home))

    def test_a_project_folder_holding_the_home_is_refused_before_anything_is_written(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The common case: a project opened at the user's home directory, which
        # holds `.swcraft` (on Windows, `AppData\Local\SWCraft`).
        project = tmp_path / "user"
        self._home_at(monkeypatch, project)
        answer, notes, timer = _run_ask(project / "swcraft.yaml", panel=_panel_answers("approve"))
        assert (answer, notes) == (False, [panel_review.HOME_INSIDE_THE_PROJECT_NOTE])
        assert timer.ticks == 0
        assert not panel_review.reviews_location().exists()
        _assert_no_path(notes, tmp_path, project)

    def test_a_project_folder_that_is_the_home_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        user = tmp_path / "user"
        self._home_at(monkeypatch, user)
        home = Path(swcraft_home.swcraft_home(os.environ, os.name, Path.home()))
        home.mkdir(parents=True)
        answer, notes, _ = _run_ask(home / "swcraft.yaml")
        assert (answer, notes) == (False, [panel_review.HOME_INSIDE_THE_PROJECT_NOTE])

    def test_the_home_reached_through_a_symlink_is_still_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(project, target_is_directory=True)
        except OSError:
            pytest.skip("this host cannot create a directory symlink")
        self._home_at(monkeypatch, link / "user")
        answer, notes, _ = _run_ask(project / "swcraft.yaml")
        assert (answer, notes) == (False, [panel_review.HOME_INSIDE_THE_PROJECT_NOTE])

    def test_a_sibling_whose_name_starts_with_the_projects_is_not_inside_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._home_at(monkeypatch, tmp_path / "project-home")
        (tmp_path / "project").mkdir()
        answer, notes, _ = _run_ask(tmp_path / "project" / "swcraft.yaml", panel=_panel_answers("approve"))
        assert (answer, notes) == (True, [])

    @staticmethod
    def _require_a_case_insensitive_volume(directory: Path) -> None:
        """Skip unless *directory*'s filesystem finds a name under another letter case."""
        probe = directory / "Case-Probe"
        probe.write_text("", encoding="utf-8", newline="\n")
        try:
            probe.with_name("cASE-pROBE").stat()
        except FileNotFoundError:
            pytest.skip("the temporary filesystem is case-sensitive")
        finally:
            probe.unlink()

    def test_a_project_spelled_in_another_letter_case_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # `Path.resolve()` keeps the case it is given, so the two spellings differ while naming one folder.
        project = tmp_path / "Project"
        project.mkdir()
        self._require_a_case_insensitive_volume(tmp_path)
        self._home_at(monkeypatch, project / "user")
        answer, notes, timer = _run_ask(tmp_path / "PROJECT" / "swcraft.yaml", panel=_panel_answers("approve"))
        assert (answer, notes) == (False, [panel_review.HOME_INSIDE_THE_PROJECT_NOTE])
        assert timer.ticks == 0
        assert not panel_review.reviews_location().exists()

    def test_another_letter_case_and_a_home_not_created_yet_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The reviews directory and the home do not exist: their nearest existing
        # ancestor, the project folder, decides.
        project = tmp_path / "Project"
        project.mkdir()
        self._require_a_case_insensitive_volume(tmp_path)
        for variable in ("HOME", "LOCALAPPDATA", "USERPROFILE"):
            monkeypatch.setenv(variable, os.fspath(project / "not-yet" / "user"))
        answer, notes, _ = _run_ask(tmp_path / "pROJECT" / "swcraft.yaml", panel=_panel_answers("approve"))
        assert (answer, notes) == (False, [panel_review.HOME_INSIDE_THE_PROJECT_NOTE])
        assert not (project / "not-yet").exists()

    def test_a_project_reached_through_a_symlink_holding_a_home_not_created_yet_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(project, target_is_directory=True)
        except OSError:
            pytest.skip("this host cannot create a directory symlink")
        for variable in ("HOME", "LOCALAPPDATA", "USERPROFILE"):
            monkeypatch.setenv(variable, os.fspath(project / "user"))
        answer, notes, _ = _run_ask(link / "swcraft.yaml", panel=_panel_answers("approve"))
        assert (answer, notes) == (False, [panel_review.HOME_INSIDE_THE_PROJECT_NOTE])
        assert not (project / "user").exists()

    def test_a_project_folder_that_cannot_be_stat_ed_is_a_hand_off_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Fail closed: an identity that cannot be read is never "not contained".
        self._home_at(monkeypatch, tmp_path / "user")
        missing = tmp_path / "missing"
        answer, notes, timer = _run_ask(missing / "swcraft.yaml", panel=_panel_answers("approve"))
        assert (answer, notes) == (False, [panel_review.handoff_note("ENOENT")])
        assert timer.ticks == 0
        assert not panel_review.reviews_location().exists()
        _assert_no_path(notes, tmp_path, missing)

    def test_a_stat_failure_of_the_project_folder_is_a_hand_off_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        self._home_at(monkeypatch, tmp_path / "user")
        real_stat = Path.stat

        def failing(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            if self == project:
                raise OSError(errno.EACCES, "Permission denied", os.fspath(self))
            return real_stat(self, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(Path, "stat", failing)
        answer, notes, _ = _run_ask(project / "swcraft.yaml", panel=_panel_answers("approve"))
        assert (answer, notes) == (False, [panel_review.handoff_note("EACCES")])
        _assert_no_path(notes, tmp_path, project)

    def test_the_note_names_no_location(self) -> None:
        _assert_no_path([panel_review.HOME_INSIDE_THE_PROJECT_NOTE], Path.home())
        assert panel_review.HOME_INSIDE_THE_PROJECT_NOTE.startswith(
            "the change set was abandoned without a human answer: SWCRAFT_REVIEW=panel is set"
        )


class TestCancellation:
    """A cancelled call withdraws its review, for real: a cancel scope over a worker thread."""

    def test_a_cancelled_wait_withdraws_and_answers_false(self, manifest: Path) -> None:
        notes: list[str] = []
        box: list[tuple[str, object]] = []
        directory = _directory()

        def worker(cancelled: type[BaseException]) -> None:
            try:
                box.append(
                    (
                        "returned",
                        panel_review.ask(
                            QUESTION,
                            kind="delta",
                            proposal=_proposal(),
                            project_name="test-project",
                            manifest=manifest,
                            notes=notes.append,
                            cancelled_exc=cancelled,
                            sleep=lambda _: time.sleep(0.01),
                        ),
                    )
                )
            except BaseException as escaped:
                box.append(("raised", escaped))
                raise

        async def main() -> None:
            # Resolved here, on the loop: in the worker it raises NoEventLoopError.
            cancelled = anyio.get_cancelled_exc_class()
            async with anyio.create_task_group() as group:
                group.start_soon(anyio.to_thread.run_sync, worker, cancelled)
                with anyio.fail_after(10):
                    while not list(directory.glob("*.pending.json")):
                        await anyio.sleep(0.01)
                group.cancel_scope.cancel()

        anyio.run(main)
        assert box == [("returned", False)]
        assert notes == [panel_review.CANCELLED_NOTE]
        assert list(directory.iterdir()) == []


# --- the wait without a deadline -------------------------------------------------------


def _answer_at(when: int, decision: str = "approve") -> Callable[[int], None]:
    """A panel that reads the pending record at tick *when* and answers it."""
    answer = _panel_answers(decision)
    return lambda tick: answer(tick - when + 1)


def _record_beats(monkeypatch: pytest.MonkeyPatch, timer: _Clock, fail: int = 0) -> tuple[list[float], list[float]]:
    """Record every heartbeat on a pending record at the injected clock's time; the first *fail* raise.

    Returns the attempts and the successes.
    """
    attempts: list[float] = []
    beats: list[float] = []
    real_utime = os.utime

    def utime(path: object, *args: object, **kwargs: object) -> None:
        if not os.fspath(path).endswith(panel_review.PENDING_SUFFIX):  # type: ignore[arg-type]
            real_utime(path, *args, **kwargs)  # type: ignore[arg-type]
            return
        attempts.append(timer.now)
        if len(attempts) <= fail:
            raise _denied(os.fspath(path))  # type: ignore[arg-type]
        real_utime(path, *args, **kwargs)  # type: ignore[arg-type]
        beats.append(timer.now)

    monkeypatch.setattr(panel_review.os, "utime", utime)
    return attempts, beats


class TestTheHeartbeat:
    """The pending record's mtime is the server's liveness, refreshed on either clock."""

    def test_it_beats_every_heartbeat_interval_of_the_monotonic_clock(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        timer = _Clock(_answer_at(240))  # 60 s
        _, beats = _record_beats(monkeypatch, timer)
        answer, notes, _ = _run_ask(manifest, timer=timer)
        assert (answer, notes) == (True, [])
        assert panel_review.HEARTBEAT_SECONDS == 15
        assert beats == [15.0, 30.0, 45.0]

    def test_a_forward_wall_jump_beats_at_once(self, manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A wake from sleep: the monotonic clock stood still, the wall clock moved.
        timer = _Clock(_answer_at(20))
        _, beats = _record_beats(monkeypatch, timer)
        answer, _, _ = _run_ask(manifest, timer=timer, wall=lambda: WALL + (3600 if timer.ticks >= 10 else 0))
        assert answer is True
        assert beats == [2.5]

    def test_a_wall_clock_set_back_keeps_the_cadence(self, manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The jump back beats once; the monotonic cadence then goes on from it.
        timer = _Clock(_answer_at(200))  # 50 s
        _, beats = _record_beats(monkeypatch, timer)
        answer, _, _ = _run_ask(manifest, timer=timer, wall=lambda: WALL - (3600 if timer.ticks >= 10 else 0))
        assert answer is True
        assert beats == [2.5, 17.5, 32.5, 47.5]

    def test_a_failing_utime_is_retried_on_the_next_ticks(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fresh: list[bool] = []
        answer_at = _answer_at(124)  # 31 s

        def panel(tick: int) -> None:
            if tick == 123:
                fresh.append(time.time() - _pending().stat().st_mtime < panel_review.STALE_SECONDS)
            answer_at(tick)

        timer = _Clock(panel)
        attempts, beats = _record_beats(monkeypatch, timer, fail=2)
        answer, notes, _ = _run_ask(manifest, timer=timer)
        assert (answer, notes) == (True, [])
        # The marks move only on success, so the retry comes on the very next tick.
        assert attempts == [15.0, 15.25, 15.5, 30.5]
        assert beats == [15.5, 30.5]
        assert fresh == [True], "the record never went stale"


def _keep_aside(path: Path, keep: Path) -> None:
    """Remove *path* from the reviews directory while its inode stays allocated under *keep*.

    A file system that reuses a freed inode number at once would otherwise give
    the next file at the name the removed one's identity.
    """
    os.link(path, keep)
    path.unlink()


def _another_waits_record(manifest: Path) -> dict:
    return panel_review.pending_record(
        QUESTION,
        kind="delta",
        wait=WAIT,
        proposal=_proposal(),
        project_name="another-project",
        manifest=(manifest.parent / "elsewhere" / "swcraft.yaml").as_posix(),
        now=WALL,
    )


class TestTheRecordIsOwnedByIdentity:
    """A swept record is restored; a name another wait took ends this one and touches nothing of it."""

    def test_a_removed_record_is_restored_under_a_fresh_token(self, manifest: Path, tmp_path: Path) -> None:
        seen: dict[str, object] = {}

        def panel(tick: int) -> None:
            if tick == 1:
                seen["before"] = json.loads(_pending().read_text(encoding="utf-8"))
                seen["before_stat"] = os.stat(_pending())
                _keep_aside(_pending(), tmp_path / "kept")
            elif tick == 2:
                after = json.loads(_pending().read_text(encoding="utf-8"))
                seen["after"] = after
                seen["after_stat"] = os.stat(_pending())
                _write_json(_decision_path(), _decision_for(after, "approve"))

        answer, notes, _ = _run_ask(manifest, panel=panel)
        assert (answer, notes) == (True, [])
        before, after = seen["before"], seen["after"]
        assert isinstance(before, dict) and isinstance(after, dict)
        for key in ("text", "digest", "created", "baseline", "change_set_id", "question"):
            assert after[key] == before[key], key
        assert WAIT_PATTERN.fullmatch(after["wait"])
        assert after["wait"] != before["wait"]
        assert not os.path.samestat(seen["before_stat"], seen["after_stat"])  # type: ignore[arg-type]
        assert list(_directory().iterdir()) == []

    def test_an_answer_to_the_swept_question_is_no_answer_to_the_restored_one(
        self, manifest: Path, tmp_path: Path
    ) -> None:
        # The old tab answers the swept record before the restoration: the
        # restoration clears it, as the first link does, and the fresh tab's
        # answer is the one taken.
        def panel(tick: int) -> None:
            if tick == 1:
                old = json.loads(_pending().read_text(encoding="utf-8"))
                _keep_aside(_pending(), tmp_path / "kept")
                _write_json(_decision_path(), _decision_for(old, "approve"))
            elif tick == 2:
                assert not _decision_path().exists()
                _write_json(
                    _decision_path(), _decision_for(json.loads(_pending().read_text(encoding="utf-8")), "reject")
                )

        answer, notes, _ = _run_ask(manifest, panel=panel)
        assert (answer, notes) == (False, [])

    def test_a_click_on_the_restored_question_at_once_meets_the_old_answer_then_is_taken(
        self, manifest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nothing clears a decision before the restoring link, so a click in that instant meets the swept
        # question's answer; the panel finds another wait token and keeps the tab live, `_settle` clears it,
        # and the next click is taken.
        met: list[dict] = []
        clicks: list[bool] = []
        restored: list[dict] = []
        real_write = panel_review.write_exclusively

        def clicked_at_once(path: Path, document: dict) -> object:
            linked = real_write(path, document)
            if linked is not None:
                restored.append(document)
                clicks.append(_panel_links(_decision_for(document, "approve")))
                met.append(json.loads(_decision_path().read_text(encoding="utf-8")))
            return linked

        def panel(tick: int) -> None:
            if tick == 1:
                old = json.loads(_pending().read_text(encoding="utf-8"))
                _keep_aside(_pending(), tmp_path / "kept")
                _write_json(_decision_path(), _decision_for(old, "reject"))
                monkeypatch.setattr(panel_review, "write_exclusively", clicked_at_once)
            elif tick == 2:
                clicks.append(_panel_links(_decision_for(restored[0], "approve")))

        timer = _Clock(panel)
        answer, notes, _ = _run_ask(manifest, timer=timer, session_closed=lambda: timer.ticks >= 10)
        assert clicks == [False, True]
        assert met[0]["decision"] == "reject"
        assert met[0]["wait"] != restored[0]["wait"], "the answer met carries the swept question's token"
        assert (answer, notes) == (True, [])
        assert list(_directory().iterdir()) == []

    def test_a_click_on_a_new_question_at_once_meets_the_old_answer_then_is_taken(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The same instant at the first link: a decision of an earlier wait at
        # the name stays until `_settle` after the link; the next click is taken.
        _write_json(_decision_path(), {"an": "earlier answer"})
        clicks: list[bool] = []
        linked_records: list[dict] = []
        real_write = panel_review.write_exclusively

        def clicked_at_once(path: Path, document: dict) -> object:
            linked = real_write(path, document)
            if linked is not None:
                linked_records.append(document)
                clicks.append(_panel_links(_decision_for(document, "approve")))
            return linked

        def panel(tick: int) -> None:
            if tick == 1:
                clicks.append(_panel_links(_decision_for(linked_records[0], "approve")))

        monkeypatch.setattr(panel_review, "write_exclusively", clicked_at_once)
        timer = _Clock(panel)
        answer, notes, _ = _run_ask(manifest, timer=timer, session_closed=lambda: timer.ticks >= 10)
        assert clicks == [False, True]
        assert (answer, notes) == (True, [])

    def test_a_decision_beside_another_waits_record_is_not_cleared_before_the_link(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nothing is cleared before a link: another live wait's record and
        # answer at the name stay as they are.
        other = _another_waits_record(manifest)
        assert panel_review.write_exclusively(_pending(), other) is not None
        _write_json(_decision_path(), _decision_for(other, "approve"))
        snapshot = (_pending().read_bytes(), _decision_path().read_bytes())
        monkeypatch.setattr(panel_review, "sweep", lambda _directory, _now: None)
        answer, notes, _ = _run_ask(manifest, panel=lambda _tick: None)
        assert (answer, notes) == (False, [panel_review.NAME_TAKEN_NOTE])
        assert (_pending().read_bytes(), _decision_path().read_bytes()) == snapshot

    def test_a_name_another_wait_took_ends_this_one_and_touches_nothing_of_it(
        self, manifest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads: list[int] = []
        real_read = panel_review.read_decision
        frozen: list[int] = []
        snapshot: dict[str, bytes] = {}

        def counting(path: Path, pending: dict) -> bool | None:
            reads.append(1)
            return real_read(path, pending)

        monkeypatch.setattr(panel_review, "read_decision", counting)

        def panel(tick: int) -> None:
            if tick != 1:
                return
            # The sweep, then another server's link, both before this ask's next tick.
            _keep_aside(_pending(), tmp_path / "kept")
            other = _another_waits_record(manifest)
            assert panel_review.write_exclusively(_pending(), other)
            _write_json(_decision_path(), _decision_for(other, "approve"))
            snapshot["pending"] = _pending().read_bytes()
            snapshot["decision"] = _decision_path().read_bytes()
            frozen.append(len(reads))

        answer, notes, _ = _run_ask(manifest, panel=panel)
        assert (answer, notes) == (False, [panel_review.NAME_TAKEN_NOTE])
        assert len(reads) == frozen[0], "the other wait's decision was never read"
        assert _pending().read_bytes() == snapshot["pending"]
        assert _decision_path().read_bytes() == snapshot["decision"]

    def test_a_name_taken_during_the_restoration_is_a_lost_name(
        self, manifest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_write = panel_review.write_exclusively
        snapshot: dict[str, bytes] = {}

        def racing(path: Path, document: dict) -> bool:
            # Another server links its record between the stat and this link.
            if not _pending().exists():
                other = _another_waits_record(manifest)
                assert real_write(path, other)
                _write_json(_decision_path(), _decision_for(other, "approve"))
                snapshot["pending"] = _pending().read_bytes()
                snapshot["decision"] = _decision_path().read_bytes()
            return real_write(path, document)

        def panel(tick: int) -> None:
            if tick == 1:
                _keep_aside(_pending(), tmp_path / "kept")
                monkeypatch.setattr(panel_review, "write_exclusively", racing)

        answer, notes, _ = _run_ask(manifest, panel=panel)
        assert (answer, notes) == (False, [panel_review.NAME_TAKEN_NOTE])
        assert _pending().read_bytes() == snapshot["pending"]
        assert _decision_path().read_bytes() == snapshot["decision"]

    def test_a_name_taken_right_after_the_first_link_is_a_lost_name(
        self, manifest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A suspension right after the link: a sweep and another wait's link before this ask's next call. The
        # identity is the written file's, so that wait's files are neither adopted nor touched (a stat of the
        # name would adopt them and clear the decision).
        real_write = panel_review.write_exclusively
        snapshot: dict[str, bytes] = {}

        def suspended(path: Path, document: dict) -> object:
            linked = real_write(path, document)
            if linked is not None and not snapshot:
                _keep_aside(path, tmp_path / "kept")
                other = _another_waits_record(manifest)
                assert real_write(path, other) is not None
                _write_json(_decision_path(), _decision_for(other, "approve"))
                snapshot["pending"] = _pending().read_bytes()
                snapshot["decision"] = _decision_path().read_bytes()
            return linked

        monkeypatch.setattr(panel_review, "write_exclusively", suspended)
        timer = _Clock()
        answer, notes, _ = _run_ask(manifest, timer=timer, session_closed=lambda: timer.ticks >= 3)
        assert (answer, notes) == (False, [panel_review.NAME_TAKEN_NOTE])
        assert _pending().read_bytes() == snapshot["pending"]
        assert _decision_path().read_bytes() == snapshot["decision"]

    def test_a_name_taken_right_after_the_restoring_link_is_a_lost_name(
        self, manifest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The same suspension after a restoration: the restored record swept,
        # another wait's record and decision at the name before the next call.
        real_write = panel_review.write_exclusively
        snapshot: dict[str, bytes] = {}

        def suspended(path: Path, document: dict) -> object:
            linked = real_write(path, document)
            if linked is not None and not snapshot:
                _keep_aside(path, tmp_path / "kept-restored")
                other = _another_waits_record(manifest)
                assert real_write(path, other) is not None
                _write_json(_decision_path(), _decision_for(other, "approve"))
                snapshot["pending"] = _pending().read_bytes()
                snapshot["decision"] = _decision_path().read_bytes()
            return linked

        def panel(tick: int) -> None:
            if tick == 1:
                _keep_aside(_pending(), tmp_path / "kept")
                monkeypatch.setattr(panel_review, "write_exclusively", suspended)

        timer = _Clock(panel)
        answer, notes, _ = _run_ask(manifest, timer=timer, session_closed=lambda: timer.ticks >= 5)
        assert (answer, notes) == (False, [panel_review.NAME_TAKEN_NOTE])
        assert _pending().read_bytes() == snapshot["pending"]
        assert _decision_path().read_bytes() == snapshot["decision"]

    def test_a_stat_that_fails_neither_restores_nor_abandons(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        failures: list[int] = []
        armed: list[dict] = []
        real_stat = os.stat

        def stat_(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if armed and len(failures) < 3 and os.fspath(path).endswith(panel_review.PENDING_SUFFIX):  # type: ignore[arg-type]
                failures.append(1)
                raise _denied(os.fspath(path))  # type: ignore[arg-type]
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

        def panel(tick: int) -> None:
            if tick == 1:
                armed.append(json.loads(_pending().read_text(encoding="utf-8")))
            elif tick == 4:
                # The record read before the failures: a restoration would
                # have drawn a new token, and this answer would not bind.
                _write_json(_decision_path(), _decision_for(armed[0], "approve"))

        monkeypatch.setattr(panel_review.os, "stat", stat_)
        answer, notes, _ = _run_ask(manifest, panel=panel)
        assert (answer, notes) == (True, [])
        assert len(failures) == 3

    def test_a_link_that_fails_during_the_restoration_is_retried(
        self, manifest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A Windows delete-pending name refuses the link with an access error:
        # that is no lost name, and the next tick links again.
        failures: list[int] = []
        armed: list[bool] = []
        real_link = os.link

        def link(source: object, target: object) -> None:
            if armed and len(failures) < 2 and os.fspath(target).endswith(panel_review.PENDING_SUFFIX):  # type: ignore[arg-type]
                failures.append(1)
                raise _denied(os.fspath(target))  # type: ignore[arg-type]
            real_link(source, target)  # type: ignore[arg-type]

        answered: list[bool] = []

        def panel(tick: int) -> None:
            if tick == 1:
                _keep_aside(_pending(), tmp_path / "kept")
                armed.append(True)
            elif not answered and _pending().exists():
                answered.append(True)
                _write_json(_decision_path(), _decision_for(json.loads(_pending().read_text(encoding="utf-8"))))

        monkeypatch.setattr(panel_review.os, "link", link)
        answer, notes, timer = _run_ask(manifest, panel=panel)
        assert (answer, notes) == (True, [])
        assert len(failures) == 2
        assert timer.ticks == 4, "two failed links, the restoration on the third tick, the answer on the fourth"

    def test_a_stat_that_fails_right_after_the_link_still_knows_its_own_file(
        self, manifest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The stat right after the link fails; the identity is the written file's, so a later foreign
        # replacement still ends the wait as a lost name.
        failures: list[int] = []
        real_stat = os.stat

        def stat_(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if not failures and os.fspath(path).endswith(panel_review.PENDING_SUFFIX):  # type: ignore[arg-type]
                failures.append(1)
                raise _denied(os.fspath(path))  # type: ignore[arg-type]
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

        def panel(tick: int) -> None:
            if tick == 1:
                _keep_aside(_pending(), tmp_path / "kept")
                assert panel_review.write_exclusively(_pending(), _another_waits_record(manifest))

        monkeypatch.setattr(panel_review.os, "stat", stat_)
        answer, notes, _ = _run_ask(manifest, panel=panel)
        assert failures == [1]
        assert (answer, notes) == (False, [panel_review.NAME_TAKEN_NOTE])
        assert _pending().exists(), "the other wait's record is untouched"

    def test_a_foreign_record_after_a_long_failing_stat_is_a_lost_name(
        self, manifest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The stat fails long enough for the record to go stale and another wait to link at the name: the first
        # stat that succeeds sees a lost name, and the other wait's files are neither read nor removed.
        real_stat = os.stat
        failing: list[bool] = [True]
        reads: list[int] = []
        real_read = panel_review.read_decision
        snapshot: dict[str, bytes] = {}

        def stat_(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if failing[0] and os.fspath(path).endswith(panel_review.PENDING_SUFFIX):  # type: ignore[arg-type]
                raise _denied(os.fspath(path))  # type: ignore[arg-type]
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

        def counting(path: Path, pending: dict) -> bool | None:
            reads.append(1)
            return real_read(path, pending)

        frozen: list[int] = []

        def panel(tick: int) -> None:
            if tick == 1:
                failing[0] = False
                _keep_aside(_pending(), tmp_path / "kept")
                other = _another_waits_record(manifest)
                assert panel_review.write_exclusively(_pending(), other)
                _write_json(_decision_path(), _decision_for(other, "approve"))
                snapshot["pending"] = _pending().read_bytes()
                snapshot["decision"] = _decision_path().read_bytes()
                frozen.append(len(reads))
                failing[0] = True
            elif tick == 300:  # 75 s: past the stale bound
                failing[0] = False

        monkeypatch.setattr(panel_review.os, "stat", stat_)
        monkeypatch.setattr(panel_review, "read_decision", counting)
        answer, notes, _ = _run_ask(manifest, panel=panel)
        monkeypatch.setattr(panel_review.os, "stat", real_stat)
        assert (answer, notes) == (False, [panel_review.NAME_TAKEN_NOTE])
        assert len(reads) == frozen[0], "the other wait's decision was never read"
        assert _pending().read_bytes() == snapshot["pending"]
        assert _decision_path().read_bytes() == snapshot["decision"]

    def test_its_own_record_is_still_its_own_after_a_failing_stat(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_stat = os.stat
        failures: list[int] = []

        def stat_(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if len(failures) < 5 and os.fspath(path).endswith(panel_review.PENDING_SUFFIX):  # type: ignore[arg-type]
                failures.append(1)
                raise _denied(os.fspath(path))  # type: ignore[arg-type]
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

        answered: list[int] = []

        def panel(tick: int) -> None:
            if tick == 6:
                answered.append(tick)
                _write_json(_decision_path(), _decision_for(json.loads(_pending().read_text(encoding="utf-8"))))

        monkeypatch.setattr(panel_review.os, "stat", stat_)
        answer, notes, _ = _run_ask(manifest, panel=panel)
        monkeypatch.setattr(panel_review.os, "stat", real_stat)
        assert (answer, notes) == (True, [])
        assert len(failures) == 5
        assert list(_directory().iterdir()) == [], "the written file's identity lets the cleanup remove both files"

    def test_a_wait_that_never_stats_its_record_leaves_the_files_to_the_sweep(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Every stat of the pending name fails: the finally cannot tell this
        # ask's record from another's and touches neither file; the sweep
        # removes them once the heartbeat is stale.
        real_stat = os.stat

        def stat_(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if os.fspath(path).endswith(panel_review.PENDING_SUFFIX):  # type: ignore[arg-type]
                raise _denied(os.fspath(path))  # type: ignore[arg-type]
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

        timer = _Clock()

        def written_on_the_first_tick() -> bool:
            if timer.ticks == 1:
                monkeypatch.setattr(panel_review.os, "stat", real_stat)
                _write_json(_decision_path(), {"any": "thing"})
                monkeypatch.setattr(panel_review.os, "stat", stat_)
            return timer.ticks >= 3

        monkeypatch.setattr(panel_review.os, "stat", stat_)
        answer, notes, _ = _run_ask(manifest, timer=timer, session_closed=written_on_the_first_tick)
        monkeypatch.setattr(panel_review.os, "stat", real_stat)
        assert (answer, notes) == (False, [panel_review.SESSION_CLOSED_NOTE])
        assert _pending().exists()
        assert _decision_path().exists()

    def test_a_stat_that_keeps_failing_skips_the_read_and_the_heartbeat_but_not_progress(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An access error that lasts: no decision read and no utime, but the
        # progress step still runs, keeping a client's idle timer alive.
        real_stat = os.stat
        reads: list[int] = []
        real_read = panel_review.read_decision
        armed: list[bool] = []

        def stat_(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if armed and os.fspath(path).endswith(panel_review.PENDING_SUFFIX):  # type: ignore[arg-type]
                raise _denied(os.fspath(path))  # type: ignore[arg-type]
            return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

        def counting(path: Path, pending: dict) -> bool | None:
            reads.append(1)
            return real_read(path, pending)

        sent: list[float] = []
        timer = _Clock(lambda tick: armed.append(True) if tick == 1 else None)
        attempts, _ = _record_beats(monkeypatch, timer)
        monkeypatch.setattr(panel_review.os, "stat", stat_)
        monkeypatch.setattr(panel_review, "read_decision", counting)
        reads_before_failing: list[int] = []

        def closed() -> bool:
            if timer.ticks == 1:
                reads_before_failing.append(len(reads))
            return timer.ticks >= 250  # 62.5 s, past one progress interval

        answer, notes, _ = _run_ask(
            manifest, timer=timer, progress=lambda _text: sent.append(timer.now), session_closed=closed
        )
        monkeypatch.setattr(panel_review.os, "stat", real_stat)
        assert (answer, notes) == (False, [panel_review.SESSION_CLOSED_NOTE])
        assert attempts == [], "no heartbeat while the stat fails"
        assert len(reads) == reads_before_failing[0], "no decision read while the stat fails"
        assert sent == [60.0]

    def test_a_reviews_directory_removed_during_the_wait_is_created_again(self, manifest: Path) -> None:
        # The SWCraft home removed under a live wait: only the server recreates it (the panel creates nothing).
        pending = panel_review.reviews_location() / panel_review.pending_name(CHANGE_SET_ID, "delta")
        answered: list[int] = []

        def panel(tick: int) -> None:
            if tick == 1:
                shutil.rmtree(panel_review.reviews_location())
            elif not answered and pending.exists():
                answered.append(tick)
                _write_json(_decision_path(), _decision_for(json.loads(pending.read_text(encoding="utf-8"))))

        answer, notes, _ = _run_ask(manifest, panel=panel)
        assert (answer, notes) == (True, [])
        assert answered == [2], "restored on the first tick after the removal"
        assert list(_directory().iterdir()) == []


class TestProgress:
    """Every awake minute the wait reports to the boundary, which sends it to the client."""

    def test_progress_comes_every_progress_interval_with_the_wall_minutes(self, manifest: Path) -> None:
        sent: list[tuple[float, str]] = []
        timer = _Clock(_answer_at(4 * 240 + 1))
        answer, notes, _ = _run_ask(
            manifest, timer=timer, wall=lambda: WALL + timer.now, progress=lambda text: sent.append((timer.now, text))
        )
        assert (answer, notes) == (True, [])
        assert panel_review.PROGRESS_SECONDS == 60
        assert sent == [(60.0 * n, panel_review.progress_text("delta", n)) for n in (1, 2, 3, 4)]
        assert sent[0][1] == "Waiting for your answer in the SWCraft review tab in VS Code (change), 1 min."
        _assert_no_path([text for _, text in sent], manifest.parent, Path.home())

    def test_the_safety_question_names_itself(self) -> None:
        assert panel_review.progress_text("safety", 3) == (
            "Waiting for your answer in the SWCraft review tab in VS Code (safety question), 3 min."
        )

    def test_the_minutes_count_the_wall_clock_across_a_sleep(self, manifest: Path) -> None:
        sent: list[str] = []
        timer = _Clock(_answer_at(241))
        answer, _, _ = _run_ask(
            manifest,
            timer=timer,
            wall=lambda: WALL + timer.now + (45 * 60 if timer.ticks >= 10 else 0),
            progress=sent.append,
        )
        assert answer is True
        assert sent == [panel_review.progress_text("delta", 46)]

    def test_a_failed_send_does_not_end_the_wait(self, manifest: Path) -> None:
        calls: list[str] = []

        def failing(text: str) -> None:
            calls.append(text)
            raise RuntimeError("the stream is closed")

        answer, notes, _ = _run_ask(manifest, panel=_answer_at(500), progress=failing)
        assert (answer, notes) == (True, [])
        assert len(calls) == 2

    @pytest.mark.parametrize(
        "raised",
        [anyio.get_cancelled_exc_class, lambda: concurrent.futures.CancelledError],
        ids=["the-backends-class", "the-thread-bridges-class"],
    )
    def test_a_cancellation_out_of_a_send_withdraws_the_review(
        self, manifest: Path, raised: Callable[[], type[BaseException]]
    ) -> None:
        classes: list[type[BaseException]] = []

        async def resolve() -> None:
            classes.append(raised())

        anyio.run(resolve)

        def cancelled(_text: str) -> None:
            raise classes[0]

        answer, notes, _ = _run_ask(manifest, progress=cancelled)
        assert (answer, notes) == (False, [panel_review.CANCELLED_NOTE])
        assert list(_directory().iterdir()) == []


class TestASessionClosed:
    """The client closing the server's stdin ends the wait: the server's end, not a clock."""

    def test_a_closed_session_withdraws_the_review(self, manifest: Path) -> None:
        timer = _Clock()
        answer, notes, _ = _run_ask(manifest, timer=timer, session_closed=lambda: timer.ticks >= 5)
        assert (answer, notes) == (False, [panel_review.SESSION_CLOSED_NOTE])
        assert timer.ticks == 5
        assert list(_directory().iterdir()) == []
        _assert_no_path(notes, manifest.parent, Path.home())

    def test_a_probe_that_raises_counts_as_open(self, manifest: Path) -> None:
        def broken() -> bool:
            raise AttributeError("_read_stream")

        answer, notes, _ = _run_ask(manifest, panel=_answer_at(3), session_closed=broken)
        assert (answer, notes) == (True, [])


# --- every OSError site ------------------------------------------------------------------


def _assert_no_path(notes: list[str], *roots: Path) -> None:
    for note in notes:
        for root in roots:
            assert root.as_posix() not in note
            assert os.fspath(root) not in note
        assert "/" not in note
        assert "\\" not in note


def _denied(path: object = "/secret/location") -> PermissionError:
    return PermissionError(errno.EACCES, "Permission denied", path)


class TestEveryOSErrorSite:
    def test_mkdir(self, manifest: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        def failing(self: Path, *args: object, **kwargs: object) -> None:
            raise _denied(self.as_posix())

        monkeypatch.setattr(Path, "mkdir", failing)
        answer, notes, _ = _run_ask(manifest)
        assert answer is False
        assert notes == [panel_review.handoff_note("EACCES")]
        _assert_no_path(notes, tmp_path, Path(os.environ["HOME"]))

    def test_chmod(self, manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def failing(self: Path, *args: object, **kwargs: object) -> None:
            raise _denied(self.as_posix())

        monkeypatch.setattr(Path, "chmod", failing)
        answer, notes, _ = _run_ask(manifest)
        assert (answer, notes) == (False, [panel_review.handoff_note("EACCES")])

    def test_the_sweeps_listing(self, manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def failing(path: object) -> None:
            raise OSError(errno.EIO, "Input/output error", path)

        monkeypatch.setattr(panel_review.os, "scandir", failing)
        answer, notes, _ = _run_ask(manifest)
        assert (answer, notes) == (False, [panel_review.handoff_note("EIO")])

    def test_a_sweep_unlink_skips_that_file_and_the_review_goes_on(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The sweep is housekeeping: another question's stuck file never throws
        # away this one. The file stays for a later sweep.
        stuck = _record_file(OTHER_ID)
        _age(stuck, panel_review.STALE_SECONDS + 30)
        real_unlink = Path.unlink

        def failing(self: Path, missing_ok: bool = False) -> None:
            if self.name == stuck.name:
                raise _denied(self.as_posix())
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", failing)
        answer, notes, _ = _run_ask(manifest, panel=_panel_answers("approve"), wall=time.time)
        assert (answer, notes) == (True, [])
        assert stuck.exists()
        assert not _pending().exists()

    def test_a_home_that_cannot_be_determined(self, manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def failing() -> Path:
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(Path, "home", staticmethod(failing))
        answer, notes, _ = _run_ask(manifest)
        assert (answer, notes) == (False, [panel_review.handoff_note("RuntimeError")])

    def test_a_question_the_utf8_codec_refuses(self, manifest: Path) -> None:
        answer, notes, _ = _run_ask(manifest, question="Commit \ud800 exactly this delta?")
        assert (answer, notes) == (False, [panel_review.handoff_note("UnicodeEncodeError")])
        assert list(_directory().iterdir()) == []

    def test_the_pending_write(self, manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def failing(source: object, target: object) -> None:
            raise _denied(os.fspath(target))

        monkeypatch.setattr(panel_review.os, "link", failing)
        answer, notes, _ = _run_ask(manifest)
        assert (answer, notes) == (False, [panel_review.handoff_note("EACCES")])
        assert list(_directory().iterdir()) == []

    def test_an_error_without_an_errno_is_named_by_its_class(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failing(path: object) -> None:
            raise OSError("no errno here, but /a/path")

        monkeypatch.setattr(panel_review.os, "scandir", failing)
        answer, notes, _ = _run_ask(manifest)
        assert (answer, notes) == (False, [panel_review.handoff_note("OSError")])

    def test_the_decision_read_retries_then_succeeds(self, manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        real_read = Path.read_bytes
        failures: list[str] = []

        def flaky(self: Path) -> bytes:
            if self.name.endswith(panel_review.DECISION_SUFFIX) and self.exists() and not failures:
                failures.append(self.name)
                raise _denied(self.as_posix())  # a sharing violation while the panel renames
            return real_read(self)

        monkeypatch.setattr(Path, "read_bytes", flaky)
        answer, notes, timer = _run_ask(manifest, panel=_panel_answers("approve"))
        assert (answer, notes) == (True, [])
        assert failures == [_decision_path().name]
        assert timer.ticks == 2

    @pytest.mark.parametrize("which", ["pending", "decision"])
    def test_a_finally_unlink_never_replaces_the_answer(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch, which: str
    ) -> None:
        real_unlink = Path.unlink
        suffix = panel_review.PENDING_SUFFIX if which == "pending" else panel_review.DECISION_SUFFIX
        armed: list[bool] = []

        def failing(self: Path, missing_ok: bool = False) -> None:
            if armed and self.name.startswith(CHANGE_SET_ID) and self.name.endswith(suffix):
                raise _denied(self.as_posix())
            real_unlink(self, missing_ok=missing_ok)

        approve = _panel_answers("approve")

        def panel(tick: int) -> None:
            approve(tick)
            armed.append(True)  # the guard after the link has run by now; only the finally unlinks from here

        monkeypatch.setattr(Path, "unlink", failing)
        answer, notes, _ = _run_ask(manifest, panel=panel)
        assert (answer, notes) == (True, [])


# --- the sweep ------------------------------------------------------------------


def _record_file(change_set_id: str) -> Path:
    path = _pending(change_set_id=change_set_id)
    _write_json(path, {"format": panel_review.FORMAT, "change_set_id": change_set_id, "question": "delta"})
    return path


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


class TestTheSweep:
    """Liveness is the pending record's mtime, its heartbeat; no record is opened."""

    def test_a_stale_record_goes_and_its_decision_is_not_touched_then(self) -> None:
        # The decision may belong to a question its owner restores right after
        # the record's unlink; the sweep never touches a decision (below).
        stale = _record_file(OTHER_ID)
        _age(stale, panel_review.STALE_SECONDS + 5)
        decision = _decision_path(change_set_id=OTHER_ID)
        _write_json(decision, {"any": "thing"})
        _age(decision, panel_review.SWEEP_GRACE_SECONDS + 30)
        panel_review.sweep(_directory(), time.time())
        assert not stale.exists()
        assert decision.exists()

    def test_a_record_with_a_live_heartbeat_is_kept_with_its_decision(self) -> None:
        live = _record_file(CHANGE_SET_ID)
        _age(live, panel_review.STALE_SECONDS - 5)
        live_decision = _decision_path()
        _write_json(live_decision, {"any": "thing"})
        panel_review.sweep(_directory(), time.time())
        assert live.exists()
        assert live_decision.exists()

    def test_no_record_is_opened(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stale = _record_file(OTHER_ID)
        _age(stale, panel_review.STALE_SECONDS + 5)
        live = _record_file(CHANGE_SET_ID)
        opened: list[str] = []
        real_read_bytes, real_open = Path.read_bytes, Path.open

        def reading(self: Path) -> bytes:
            opened.append(self.name)
            return real_read_bytes(self)

        def opening(self: Path, *args: object, **kwargs: object) -> object:
            opened.append(self.name)
            return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_bytes", reading)
        monkeypatch.setattr(Path, "open", opening)
        panel_review.sweep(_directory(), time.time())
        monkeypatch.undo()
        assert opened == []
        assert not stale.exists()
        assert live.exists()

    def test_a_fresh_decision_beside_a_just_swept_record_survives_a_second_sweep(self) -> None:
        # One sweep removes the stale record, its owner restores it and the user answers; another sweep must
        # never take that answer.
        stale = _record_file(OTHER_ID)
        _age(stale, panel_review.STALE_SECONDS + 5)
        decision = _decision_path(change_set_id=OTHER_ID)
        _write_json(decision, {"old": "answer"})
        _age(decision, panel_review.SWEEP_GRACE_SECONDS + 30)
        panel_review.sweep(_directory(), time.time())
        assert not stale.exists()
        decision.unlink()  # the owner's restoration removes the foreign decision
        _write_json(decision, {"fresh": "answer"})
        panel_review.sweep(_directory(), time.time())
        assert json.loads(decision.read_text(encoding="utf-8")) == {"fresh": "answer"}

    def test_a_record_whose_unlink_fails_keeps_its_decision(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A Windows sharing violation keeps the record under the same wait token; its decision must survive
        # too, or the server waits for an answer already given.
        stale = _record_file(OTHER_ID)
        _age(stale, panel_review.STALE_SECONDS + 5)
        decision = _decision_path(change_set_id=OTHER_ID)
        _write_json(decision, {"given": "answer"})
        _age(decision, panel_review.SWEEP_GRACE_SECONDS + 30)
        given = decision.read_bytes()
        real_unlink = Path.unlink

        def refusing(self: Path, missing_ok: bool = False) -> None:
            if self.name == stale.name:
                raise _denied(os.fspath(self))
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", refusing)
        panel_review.sweep(_directory(), time.time())
        monkeypatch.undo()
        assert stale.exists()
        assert decision.read_bytes() == given
        # `monkeypatch.undo()` also undid the conftest's home, so the directory is the record's.
        assert sorted(entry.name for entry in stale.parent.iterdir()) == sorted([stale.name, decision.name])

    @pytest.mark.parametrize(("age", "kept"), [(0, True), (1, False)], ids=["exactly-stale-seconds", "one-more"])
    def test_the_stale_bound_is_strict(self, age: int, kept: bool) -> None:
        # The extension's `isLive` shows a record while `now - mtime < STALE_MS`;
        # the sweep removes one only when `now - mtime > STALE_SECONDS`, so at
        # the bound itself the tab has stopped showing it and the file is kept.
        record = _record_file(OTHER_ID)
        beat = 1_700_000_000
        os.utime(record, (beat, beat))
        panel_review.sweep(_directory(), beat + panel_review.STALE_SECONDS + age)
        assert record.exists() is kept

    @pytest.mark.parametrize(
        "age",
        [0, panel_review.SWEEP_GRACE_SECONDS + 5, 30 * 24 * 3600],
        ids=["fresh", "older-than-the-grace", "a-month-old"],
    )
    def test_a_decision_without_a_record_is_never_touched(self, age: int, monkeypatch: pytest.MonkeyPatch) -> None:
        # Any judgment of an orphan leaves a window in which a fresh answer to a restored question is at the
        # name; the sweep moves, renames and removes no decision at all, only the name's owner clears.
        orphan = _decision_path(change_set_id=OTHER_ID)
        _write_json(orphan, {"any": "thing"})
        _age(orphan, age)
        given = orphan.read_bytes()
        touched: list[str] = []
        real_rename, real_replace, real_unlink = os.rename, os.replace, Path.unlink

        def renaming(src: object, dst: object, *args: object, **kwargs: object) -> None:
            touched.append(os.fspath(src))  # type: ignore[arg-type]
            real_rename(src, dst, *args, **kwargs)  # type: ignore[arg-type]

        def replacing(src: object, dst: object, *args: object, **kwargs: object) -> None:
            touched.append(os.fspath(src))  # type: ignore[arg-type]
            real_replace(src, dst, *args, **kwargs)  # type: ignore[arg-type]

        def unlinking(self: Path, missing_ok: bool = False) -> None:
            touched.append(self.name)
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(panel_review.os, "rename", renaming)
        monkeypatch.setattr(panel_review.os, "replace", replacing)
        monkeypatch.setattr(Path, "unlink", unlinking)
        panel_review.sweep(_directory(), time.time())
        monkeypatch.undo()
        assert touched == []
        assert orphan.read_bytes() == given
        assert [entry.name for entry in orphan.parent.iterdir()] == [orphan.name]

    def test_a_record_restored_after_the_listing_keeps_its_decision(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A record restored after the listing: its decision is the owner's, and the sweep leaves it alone.
        orphan = _decision_path(change_set_id=OTHER_ID)
        _write_json(orphan, {"restored": "answer"})
        _age(orphan, panel_review.SWEEP_GRACE_SECONDS + 30)
        real_scandir = os.scandir
        restored: list[Path] = []

        def listing_then_restoring(directory: object) -> object:
            entries = list(real_scandir(directory))  # type: ignore[call-overload]
            restored.append(_record_file(OTHER_ID))
            return iter(entries)

        monkeypatch.setattr(panel_review.os, "scandir", listing_then_restoring)
        panel_review.sweep(_directory(), time.time())
        monkeypatch.undo()
        assert restored and restored[0].exists()
        assert json.loads(orphan.read_text(encoding="utf-8")) == {"restored": "answer"}

    def test_a_young_temporary_survives_and_an_old_one_goes(self) -> None:
        directory = _directory()
        young = directory / f".{panel_review.pending_name(OTHER_ID, 'delta')}.4242.tmp"
        old = directory / f".{panel_review.pending_name(CHANGE_SET_ID, 'delta')}.4243.tmp"
        for path in (young, old):
            path.write_text("{", encoding="utf-8", newline="\n")
        _age(old, panel_review.SWEEP_GRACE_SECONDS + 30)
        panel_review.sweep(directory, time.time())
        assert young.exists()
        assert not old.exists()

    @pytest.mark.parametrize(
        "payload",
        [b"\xff\xfe broken", b"not json", b"[]", b"[" * 200_000],
        ids=["invalid-utf8", "not-json", "a-list", "nested-past-the-recursion-limit"],
    )
    def test_a_malformed_record_is_judged_by_its_mtime_alone(self, payload: bytes) -> None:
        directory = _directory()
        young = _pending(change_set_id=OTHER_ID)
        old = _pending(change_set_id="cs-20261025T110000Z-fedcba")
        for path in (young, old):
            path.write_bytes(payload)
        _age(old, panel_review.STALE_SECONDS + 30)
        panel_review.sweep(directory, time.time())
        assert young.exists()
        assert not old.exists()

    @pytest.mark.parametrize("payload", [b"\xff not a record", b"[" * 200_000], ids=["invalid-utf8", "nested"])
    def test_a_malformed_record_never_makes_ask_raise(self, manifest: Path, payload: bytes) -> None:
        stale = _pending(change_set_id=OTHER_ID)
        stale.write_bytes(payload)
        _age(stale, panel_review.STALE_SECONDS + 30)
        answer, notes, _ = _run_ask(manifest, panel=_panel_answers("approve"), wall=time.time)
        assert (answer, notes) == (True, [])
        assert not stale.exists()

    def test_other_files_are_left_alone(self) -> None:
        directory = _directory()
        unrelated = directory / "README.txt"
        unrelated.write_text("mine", encoding="utf-8", newline="\n")
        _age(unrelated, 10_000)
        panel_review.sweep(directory, time.time())
        assert unrelated.exists()

    def test_ask_sweeps_before_it_writes(self, manifest: Path) -> None:
        stale = _record_file(OTHER_ID)
        _age(stale, panel_review.STALE_SECONDS + 30)
        orphan = _decision_path(change_set_id=OTHER_ID)
        _write_json(orphan, {"any": "thing"})
        _age(orphan, panel_review.SWEEP_GRACE_SECONDS + 30)
        _run_ask(manifest, panel=_panel_answers("reject"), wall=time.time)
        assert not stale.exists()
        assert orphan.exists(), "the sweep never removes a decision"

    def test_a_decision_written_before_the_question_answers_nothing(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A valid, digest-correct approval of exactly this question, planted at
        # its own decision name with no pending record, and the sweep skipped
        # (the window between the sweep and the link): the clearing after the
        # link removes it, and only an answer written afterwards can count.
        record = panel_review.pending_record(
            QUESTION,
            kind="delta",
            wait=WAIT,
            proposal=_proposal(),
            project_name="test-project",
            manifest=manifest.resolve().as_posix(),
            now=WALL,
        )
        _write_json(_decision_path(), _decision_for(record, "approve"))
        monkeypatch.setattr(panel_review, "sweep", lambda _directory, _now: None)
        seen: list[bool] = []

        def panel(tick: int) -> None:
            if tick == 1:
                seen.append(_decision_path().exists())

        def cancelled(_text: str) -> None:
            # The client cancels the call once a progress send is in flight.
            raise concurrent.futures.CancelledError

        answer, notes, _ = _run_ask(manifest, panel=panel, progress=cancelled)
        assert seen == [False]
        assert answer is False
        assert notes == [panel_review.CANCELLED_NOTE], "the early approval answered nothing"

    def test_an_answer_written_right_after_the_link_is_kept(
        self, manifest: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An answer written before the guard after the link runs binds to the fresh token: removing it would
        # leave the wait, which has no deadline, running for ever.
        real_write = panel_review.write_exclusively

        def answered_at_once(path: Path, document: dict) -> bool:
            linked = real_write(path, document)
            if linked and path.name.endswith(panel_review.PENDING_SUFFIX):
                _write_json(_decision_path(), _decision_for(document, "approve"))
            return linked

        monkeypatch.setattr(panel_review, "write_exclusively", answered_at_once)
        answer, notes, _ = _run_ask(manifest, panel=lambda _tick: None)
        assert (answer, notes) == (True, [])
        assert list(_directory().iterdir()) == []

    def test_a_file_that_vanishes_under_the_sweep_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        directory = _directory()
        now = time.time()
        gone = _record_file(OTHER_ID)
        temporary = directory / f".{panel_review.pending_name(CHANGE_SET_ID, 'delta')}.4243.tmp"
        temporary.write_text("{", encoding="utf-8", newline="\n")
        expired = _record_file("cs-20261025T110000Z-fedcba")
        for path in (gone, temporary, expired):
            _age(path, panel_review.STALE_SECONDS + panel_review.SWEEP_GRACE_SECONDS + 5)
        real_stat = Path.stat

        def vanishing(self: Path, *args: object, **kwargs: object) -> os.stat_result:
            if self.name in (gone.name, temporary.name):
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", self.as_posix())
            return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "stat", vanishing)
        panel_review.sweep(directory, now)
        monkeypatch.undo()  # `exists()` stats too
        assert gone.exists()
        assert temporary.exists()
        assert not expired.exists(), "the sweep went on past the files it skipped"

    def test_a_stat_that_fails_never_stops_ask(self, manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        stuck = _directory() / f".{panel_review.pending_name(OTHER_ID, 'delta')}.4242.tmp"
        stuck.write_text("{", encoding="utf-8", newline="\n")
        real_stat = Path.stat

        def denied(self: Path, *args: object, **kwargs: object) -> os.stat_result:
            if self.name == stuck.name:
                raise _denied(self.as_posix())
            return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "stat", denied)
        answer, notes, _ = _run_ask(manifest, panel=_panel_answers("approve"))
        assert (answer, notes) == (True, [])


# --- the two halves read each other's records -------------------------------------

#: One record of each kind, byte for byte as each half writes it; the extension's
#: `reviewCore.test.ts` reads the same files, so a key changed on one side fails both.
VECTOR_DIR = Path(__file__).resolve().parents[3] / "vscode_extension" / "fixtures" / "review"
VECTOR_TEXT = (
    "\nChange set cs-20261025T120000Z-0a1b2c on \u00abVehicle\u00bb:\n"
    "  set Gr\u00f6\u00dfe \u2192 42 \u00b5s\n"
    "  \u65e5\u672c \U0001d11e\n"
)
VECTOR_MANIFEST = "/Users/dev/p/swcraft.yaml"
VECTOR_WAIT = "5f1e0c9a7b3d4e2f8a6b1c0d9e8f7a6b"


class TestTheSharedVectors:
    def _vector_record(self) -> dict:
        record = panel_review.pending_record(
            VECTOR_TEXT,
            kind="delta",
            wait=VECTOR_WAIT,
            proposal=ChangeSetProposal(
                change_set_id="cs-20261025T120000Z-0a1b2c",
                baseline="sha256:" + "ab" * 32,
                intent="Add the wheel speed port",
                delta=(),
            ),
            project_name="Vehicle Control",
            # The record takes the spelling its caller resolved; `ask` resolves
            # it (which adds a drive on Windows), so the vector's is used as is.
            manifest=VECTOR_MANIFEST,
            now=datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC).timestamp(),
            client="Codex",
        )
        return record

    def test_the_pending_vector_is_what_this_half_writes(self) -> None:
        written = panel_review.record_json(self._vector_record())
        assert (VECTOR_DIR / "pending.json").read_text(encoding="utf-8") == written

    def test_the_warning_vector_is_what_this_half_writes(self) -> None:
        proposal = ChangeSetProposal(
            change_set_id="cs-20261025T120000Z-0a1b2c",
            baseline="sha256:" + "ab" * 32,
            intent="Add the wheel speed port",
            delta=(),
            rewritten=(RewrittenFile("swc", "BrakeController", "the file has mixed line ends"),),
        )
        review = delta_review(proposal)
        record = panel_review.pending_record(
            review.text,
            kind="delta",
            wait=VECTOR_WAIT,
            proposal=proposal,
            project_name="Vehicle Control",
            manifest=VECTOR_MANIFEST,
            now=datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC).timestamp(),
            client="Codex",
            warning_lines=review.warning_lines,
        )
        written = panel_review.record_json(record)
        assert (VECTOR_DIR / "pending-warning.json").read_text(encoding="utf-8") == written

    def test_the_extensions_decision_vector_is_accepted_as_an_approval(self) -> None:
        assert panel_review.read_decision(VECTOR_DIR / "decision.json", self._vector_record()) is True

    def test_the_stale_bound_is_the_extensions(self) -> None:
        # `reviewCore.test.ts` asserts the same literal for `STALE_MS`: the two
        # halves decide liveness by one bound, and neither may drift alone.
        assert panel_review.STALE_SECONDS == 60

    def test_the_vectors_carry_format_2_and_no_deadline(self) -> None:
        for name in ("pending.json", "pending-warning.json", "decision.json"):
            document = json.loads((VECTOR_DIR / name).read_text(encoding="utf-8"))
            assert document["format"] == panel_review.FORMAT == 2, name
            assert "deadline" not in document, name

    def test_the_decision_vector_carries_exactly_the_keys_this_half_reads(self) -> None:
        decision = json.loads((VECTOR_DIR / "decision.json").read_text(encoding="utf-8"))
        assert set(decision) == panel_review._DECISION_KEYS
