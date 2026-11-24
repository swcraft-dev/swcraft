"""The Codex guard's work bounds: each of its scans is one linear pass, and work charged to the budget is charged.

A claim about how work grows is a growth ratio (:func:`growth.growth`, scale 1 against scale 4) on the function
the scan lives in, never a number of seconds and never through the whole judge, whose stdlib tokeniser grows
super-linearly on one long token by itself. A claim that work is charged or refused is an operation count. The
guard's decisions on the same inputs are clock-free tests in ``test_copilot_guard_codex.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from growth import LINEAR_GROWTH, growth
from swcraft_tools import copilot_guard

#: The functions and bounds the counts below read, captured at import, so that a patched name cannot move its own
#: bound.
_BRACE_GROUP_COUNT = copilot_guard._brace_group_count
_MAX_BRACE_GROUPS = copilot_guard._MAX_BRACE_GROUPS
_BRACE_SCAN_UNIT = copilot_guard._BRACE_SCAN_UNIT


@pytest.fixture
def judged_dir(tmp_path: Path) -> Path:
    """A directory holding a ``model`` folder and a ``notes`` folder, the base of a :class:`_ShellJudge`."""
    (tmp_path / "model" / "swc").mkdir(parents=True)
    (tmp_path / "notes").mkdir()
    return tmp_path


def _judge(base: Path) -> copilot_guard._ShellJudge:
    return copilot_guard._ShellJudge(base, str(base.resolve() / "model"), ())


class TestTheScansAreLinear:
    """Four times the input costs at most :data:`growth.LINEAR_GROWTH` times the time, on the scan's own function."""

    def test_a_patch_s_headers_are_found_in_one_pass(self) -> None:
        body = "+" + "x" * 98 + "\n"
        commands = {
            scale: "*** Begin Patch\n*** Add File: notes/big.txt\n"
            + body * 8_000 * scale
            + "*** Update File: swcraft.yaml\n+x\n*** End Patch\n"
            for scale in (1, 4)
        }
        assert copilot_guard._patch_paths(commands[1]) == ["notes/big.txt", "swcraft.yaml"]
        ratio = growth(lambda scale: copilot_guard._patch_paths(commands[scale]))
        assert ratio < LINEAR_GROWTH, ratio

    @pytest.mark.parametrize(
        ("size", "tail", "below", "reaches"),
        [
            (8_000, ["x"], ["model", "swc", "Engine.yaml"], True),
            (32_000, ["x", ".x"], [".codex", "a"], False),
        ],
        ids=["reaching", "unreached"],
    )
    def test_the_globstar_closure_is_linear(self, size: int, tail: list[str], below: list[str], reaches: bool) -> None:
        """The closure sweeps the components once.

        A set rebuilt inside the sweep would make it cubic, and a judgement past Codex's hook timeout lets the tool
        run."""
        patterns = {scale: ["**"] * size * scale + tail for scale in (1, 4)}
        assert copilot_guard._could_reach(patterns[1], below) is reaches
        ratio = growth(lambda scale: copilot_guard._could_reach(patterns[scale], below))
        assert ratio < LINEAR_GROWTH, ratio

    def test_a_command_of_globstar_words_is_judged_in_linear_time(self, judged_dir: Path) -> None:
        """A command of globstar words is judged in linear time.

        Many words of a few kilobytes each, so the tokeniser's cost on one long token is not in the ratio; the verdict
        is the model's at both scales, so no budget stop shortens the larger run.
        """
        commands = {scale: "ls " + " ".join(["notes/../" + "**/" * 200 * scale + "x"] * 15) for scale in (1, 4)}

        def judged(scale: int) -> None:
            assert _judge(judged_dir).command(commands[scale]) == copilot_guard._MODEL

        ratio = growth(judged)
        assert ratio < LINEAR_GROWTH, ratio

    @pytest.mark.parametrize(
        "word",
        [
            lambda scale: "${" + "?" * 64_000 * scale,
            lambda scale: "${" + "?" * 64_000 * scale + "; rm -rf model",
            lambda scale: "${a:-" * 32_000 * scale,
        ],
        ids=["unclosed-special", "unclosed-then-model", "nested-unclosed"],
    )
    def test_an_adversarial_parameter_word_is_read_in_linear_time(self, word: Callable[[int], str]) -> None:
        """The parameter reading is one linear pass, not a backtracking match that grows with the unclosed word."""
        words = {scale: word(scale) for scale in (1, 4)}
        ratio = growth(lambda scale: copilot_guard._parameter_words(words[scale]))
        assert ratio < LINEAR_GROWTH, ratio

    @pytest.mark.parametrize(
        ("script", "size"),
        [
            (lambda n: "[" * n, 16_000),
            (lambda n: "/" + "[" * n, 128_000),
            (lambda n: "s/" + "\\" * n, 64_000),
            (lambda n: "w f\n" * n, 8_000),
            (lambda n: "y/" + "a" * n + "/" + "b" * n + "/", 16_000),
        ],
        ids=["brackets", "address-brackets", "escapes", "writes", "transliteration"],
    )
    def test_a_long_sed_script_is_read_in_linear_time(self, script: Callable[[int], str], size: int) -> None:
        scripts = {scale: script(size * scale) for scale in (1, 4)}
        ratio = growth(lambda scale: copilot_guard._sed_reading(["sed", scripts[scale]]))
        assert ratio < LINEAR_GROWTH, ratio

    def test_unclosed_brackets_are_translated_in_linear_time(self) -> None:
        """Unclosed ``[`` and ``[:x:]`` groups: each stop is scanned from once, never rescanned from every later stop.

        The uncached function is timed."""
        translate = copilot_guard._bash_brackets.__wrapped__
        components = {scale: "[" * 4_000 * scale + "00" + "[:x:]" * 800 * scale for scale in (1, 4)}
        ratio = growth(lambda scale: translate(components[scale]))
        assert ratio < LINEAR_GROWTH, ratio


def _counting_spend() -> tuple[list[int], Callable[[int], None]]:
    """A budget spender that adds every charge to the one-element list returned beside it."""
    charged = [0]

    def spend(count: int) -> None:
        charged[0] += count

    return charged, spend


#: What :func:`_word_parts` makes besides the suffixes it charges: the word itself, its parameter reading and the
#: pieces around its separators, at most three times the word's length together.
_UNCHARGED_PARTS = 3


class TestTheWorkIsCharged:
    """Work that grows with a word is charged to the budget before it is done, or refused before it starts."""

    @pytest.mark.parametrize(
        "word",
        ["-" + "u" * 4_000, "-a" + "{a}" * 1364, "-a" + "{a}" * 682 + "=" + "{a}" * 682],
        ids=["cluster", "glued", "glued-equals"],
    )
    def test_the_suffixes_of_an_option_cluster_are_charged(self, word: str) -> None:
        """Every suffix of a short-option cluster is a word, charged before it is made.

        The charge is the characters it copies, so the characters made are at most :data:`_BRACE_SCAN_UNIT` times the
        charge.
        """
        charged, spend = _counting_spend()
        parts = copilot_guard._word_parts(word, spend)
        made = sum(len(part) for part in parts)
        assert made > len(word) ** 2 // 4  # the suffixes are made: the count is on the path
        assert made <= _BRACE_SCAN_UNIT * charged[0] + _UNCHARGED_PARTS * len(word), (made, charged)

    def test_a_part_past_the_brace_group_bound_is_never_expanded(
        self, judged_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A part holding more than :data:`_MAX_BRACE_GROUPS` groups is refused before any expansion."""
        expanded: list[str] = []
        expand = copilot_guard._brace_expansions
        bound = _MAX_BRACE_GROUPS

        def counted(glob: str, *args: Any, **kwargs: Any) -> Any:
            expanded.append(glob)
            return expand(glob, *args, **kwargs)

        monkeypatch.setattr(copilot_guard, "_brace_expansions", counted)
        judge = _judge(judged_dir)
        assert judge.command("ls notes/" + "{a}" * bound) is None
        assert expanded, "a part at the bound is never expanded: the counter is off the path"
        assert judge.command("touch " + " ".join(["{a}" * 1365] * 15) + " model/x") == copilot_guard._OVER_BUDGET
        assert [glob for glob in expanded if _BRACE_GROUP_COUNT(glob) > bound] == []

    @pytest.mark.parametrize(
        "command",
        [
            "touch " + " ".join(["{a,b}" * 8 + "{a}" * 56] * 15) + " notes/x",
            "touch " + " ".join(["{a}" * 64 + "x" * 3800] * 15) + " notes/x",
        ],
        ids=["wide-then-chained", "chained-long"],
    )
    def test_every_brace_scan_is_charged(self, judged_dir: Path, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
        """Expanding a part's brace groups rescans it once per group, and every scan is charged to the budget.

        Each scan is charged by the characters it reads, so the characters scanned are at most
        :data:`_BRACE_SCAN_UNIT` times the readings. Each scan is counted once, though :func:`_first_brace_group`
        reads its text in two passes."""
        scanned, spend = _counting_spend()
        find = copilot_guard._first_brace_group

        def counted(glob: str, *args: Any, **kwargs: Any) -> Any:
            spend(len(glob))
            return find(glob, *args, **kwargs)

        monkeypatch.setattr(copilot_guard, "_first_brace_group", counted)
        judge = _judge(judged_dir)
        verdict = judge.command(command)
        assert scanned[0] > 0
        assert scanned[0] <= _BRACE_SCAN_UNIT * judge.readings + len(command), scanned
        assert verdict == copilot_guard._OVER_BUDGET
