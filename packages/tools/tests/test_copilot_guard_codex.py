"""``swcraft guard``'s Codex dialect: its patches and shell commands, judged by the guard's two Codex judges.

The shell judge's quoting, words, parameters, globs, braces, ``sed``, nested shells and budget; the guard inputs,
the builds of the guarded manifest, the deadline, the project and the command. The helpers the dialects share are
``copilot_guard_support``'s, their project fixtures ``conftest.py``'s.

What this does not reach: whether Codex honours the answer; no host runs here. Every case that makes a link goes
through ``symlink_or_skip``, so on a Windows runner without the symbolic-link privilege those cases skip and prove
nothing there.
"""

from __future__ import annotations

import json
import os
import shlex
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import _write_manifest
from copilot_guard_support import (
    BY_HAND,
    CODEX_UNDECIDED_DENY,
    GUARD_INPUT_REASON,
    SHELL_REASON,
    UNJUDGED_MARK,
    UNREADABLE_MANIFESTS,
    codex_patch,
    codex_project,
    decide_codex_shell,
    decide_codex_tool,
    decide_payload,
    guard_payload,
    run_guard,
    write_unreadable_manifest,
)
from filesystem_support import accepts_path_segment
from resolution_clock import TICKS_TO_THE_DEADLINE, resolution_clocks
from swcraft_tools import copilot_guard, entry
from symlink_support import symlink_or_skip

#: The deadline as the module ships it, captured at import, before :func:`_judge_has_no_wall_clock_deadline`
#: replaces it: the tests about that value read this one.
_SHIPPED_JUDGE_SECONDS = copilot_guard._JUDGE_SECONDS


_PATCH_REASON = (
    "SWCraft: in this project the model directory (`model`) is changed through the SWCraft tools, whose changes "
    "the panel reviews; this patch edits `{path}` there. Use the SWCraft MCP tool `change_set` instead."
)


_BUDGET_MARK = "too long, or names too many paths"


class TestCodexPatches:
    """A patch is denied when any path it names resolves into the model or cannot be judged."""

    @pytest.mark.parametrize(
        "headers",
        [
            ["*** Update File: model/swc/Engine.yaml"],
            ["*** Add File: model/swc/New.yaml"],
            ["*** Delete File: model/swc/Engine.yaml"],
            ["*** Update File: notes.md", "*** Move to: model/swc/Notes.yaml"],
        ],
        ids=["update", "add", "delete", "move-into"],
    )
    def test_a_patch_touching_the_model_is_denied(self, codex_dir: Path, headers: list[str]) -> None:
        decision = decide_codex_tool("apply_patch", codex_dir, command=codex_patch(*headers))
        assert not decision.allow
        assert decision.reason is not None
        assert decision.reason.startswith(_PATCH_REASON.split("{path}")[0])
        assert "`model/swc/" in decision.reason
        assert str(codex_dir) not in decision.reason

    def test_a_move_out_of_the_model_is_denied(self, codex_dir: Path) -> None:
        """Its ``Update File:`` header names the model file it moves away."""
        command = codex_patch("*** Update File: model/swc/Engine.yaml", "*** Move to: notes/Engine.yaml")
        decision = decide_codex_tool("apply_patch", codex_dir, command=command)
        assert decision == copilot_guard.Decision(
            allow=False, reason=_PATCH_REASON.format(path="model/swc/Engine.yaml")
        )

    def test_a_patch_outside_the_model_is_allowed(self, codex_dir: Path) -> None:
        command = codex_patch("*** Add File: notes/readme.md", "*** Update File: notes.md")
        assert decide_codex_tool("apply_patch", codex_dir, command=command).allow

    def test_an_absolute_path_into_the_model_is_denied(self, codex_dir: Path) -> None:
        command = codex_patch(f"*** Update File: {codex_dir / 'model' / 'swc' / 'Engine.yaml'}")
        decision = decide_codex_tool("apply_patch", codex_dir, command=command)
        assert decision == copilot_guard.Decision(
            allow=False, reason=_PATCH_REASON.format(path="model/swc/Engine.yaml")
        )

    def test_a_relative_path_back_into_the_model_is_denied(self, codex_dir: Path) -> None:
        assert not decide_codex_tool(
            "apply_patch", codex_dir, command=codex_patch("*** Update File: notes/../model/x.yaml")
        ).allow

    @pytest.mark.parametrize("path", ["//server/share/model/x.yaml", "C:model/x.yaml", "Z:/model/x.yaml"])
    def test_an_unjudgeable_path_is_denied(self, codex_dir: Path, path: str) -> None:
        if path.startswith("Z:/") and os.name == "nt":
            pytest.skip("a drive-letter path is judgeable where native paths have drives")
        decision = decide_codex_tool("apply_patch", codex_dir, command=codex_patch(f"*** Add File: {path}"))
        assert not decision.allow
        assert decision.reason is not None and path not in decision.reason

    @pytest.mark.parametrize(
        "command",
        [
            "*** Update File: notes.md\n+x\n*** End Patch\n",
            "cat <<'EOF'\n*** Begin Patch\n*** Add File: notes/x.md\n+x\n*** End Patch\nEOF\n",
            "*** Begin Patch\n*** End Patch\n",
            ["*** Begin Patch", "*** Add File: notes/x.md", "*** End Patch"],
            None,
            42,
        ],
        ids=["no-begin", "heredoc", "no-path", "list", "null", "number"],
    )
    def test_a_patch_that_cannot_be_read_is_denied(self, codex_dir: Path, command: object) -> None:
        decision = decide_codex_tool("apply_patch", codex_dir, command=command)
        assert not decision.allow
        assert decision.reason is not None and decision.reason.startswith("SWCraft: in this project")

    @pytest.mark.parametrize("separator", [" ", "\u0085", " ", "\x1c", "\x0b", "\x0c"])
    def test_a_patch_is_split_into_lines_at_a_newline_only(self, tmp_path: Path, separator: str) -> None:
        """Codex's parser splits a patch at ``\\n`` (a ``\\r`` before it dropped).

        So a model directory whose name holds another line separator is named whole by its header."""
        model = f"mo{separator}del"
        root = codex_project(tmp_path / "proj", model)
        # Windows refuses U+001C, U+000B and U+000C in a name: there the directory is not created and the
        # header is judged by name alone, which must deny all the same (filesystem_support's probe, never a
        # platform check).
        if accepts_path_segment(tmp_path, model):
            (root / model).mkdir()
        for header in (f"*** Add File: {model}/x.yaml", f"*** Update File: {model}/x.yaml\r"):
            decision = decide_codex_tool("apply_patch", root, command=codex_patch(header))
            assert not decision.allow, repr(header)
            assert decision.reason is not None and decision.reason.startswith("SWCraft: in this project")

    def test_a_header_line_ending_in_a_carriage_return_is_read_without_it(self, codex_dir: Path) -> None:
        command = "*** Begin Patch\r\n*** Update File: model/swc/Engine.yaml\r\n+x\r\n*** End Patch\r\n"
        assert decide_codex_tool("apply_patch", codex_dir, command=command) == copilot_guard.Decision(
            allow=False, reason=_PATCH_REASON.format(path="model/swc/Engine.yaml")
        )

    def test_a_large_patch_is_decided(self, codex_dir: Path) -> None:
        """Ten megabytes of harmless lines before a header naming the manifest: the header is still read.

        The pass is linear: ``test_copilot_guard_scaling.py``."""
        body = "+" + "x" * 98 + "\n"
        command = "*** Begin Patch\n*** Add File: notes/big.txt\n" + body * 100_000
        command += "*** Update File: swcraft.yaml\n+x\n*** End Patch\n"
        decision = decide_codex_tool("apply_patch", codex_dir, command=command)
        assert decision == copilot_guard.Decision(allow=False, reason=GUARD_INPUT_REASON.format(kind="patch"))

    def test_a_patch_with_more_headers_than_the_cap_is_denied_by_the_budget(self, codex_dir: Path, monkeypatch) -> None:
        """Past :data:`_MAX_PATCH_HEADERS` headers the patch is denied before any of its paths is resolved."""
        resolved: list[str] = []
        resolve = copilot_guard._codex_resolve

        def recording(base: Path, written: str, deadline: Any) -> str | None:
            resolved.append(written)
            return resolve(base, written, deadline)

        monkeypatch.setattr(copilot_guard, "_codex_resolve", recording)
        limit = copilot_guard._MAX_PATCH_HEADERS
        allowed = codex_patch(*(f"*** Add File: notes/f{index}.txt" for index in range(limit)))
        assert decide_codex_tool("apply_patch", codex_dir, command=allowed).allow
        assert len(resolved) == limit  # every path at the cap is resolved: the count is on the path
        resolved.clear()
        refused = codex_patch(*(f"*** Add File: notes/f{index}.txt" for index in range(limit + 1)))
        decision = decide_codex_tool("apply_patch", codex_dir, command=refused)
        assert resolved == []
        assert not decision.allow and decision.reason is not None and _BUDGET_MARK in decision.reason

    def test_a_patch_whose_paths_pass_the_reading_budget_is_denied_by_it(self, codex_dir: Path) -> None:
        """Resolving a header path costs one status read per component, charged to the shell judge's budget."""
        deep = "/".join(["a"] * (copilot_guard._MAX_SHELL_READINGS + 1))
        decision = decide_codex_tool("apply_patch", codex_dir, command=codex_patch(f"*** Add File: notes/{deep}"))
        assert not decision.allow and decision.reason is not None and _BUDGET_MARK in decision.reason

    def test_blank_lines_before_begin_patch_are_allowed(self, codex_dir: Path) -> None:
        assert decide_codex_tool(
            "apply_patch", codex_dir, command="\n  \n" + codex_patch("*** Add File: notes/x.md")
        ).allow

    @pytest.mark.parametrize("tool", ["apply_patch", "APPLY_PATCH", "Edit", "Write"])
    def test_every_patch_name_is_judged(self, codex_dir: Path, tool: str) -> None:
        """``Edit`` and ``Write`` are the matcher aliases Codex documents for ``apply_patch``."""
        assert not decide_codex_tool(
            tool, codex_dir, command=codex_patch("*** Update File: model/swc/Engine.yaml")
        ).allow

    def test_copilot_still_allows_a_patch(self, codex_dir: Path) -> None:
        """The Copilot dialect judges neither tool: its decisions are unchanged."""
        payload = guard_payload("apply_patch", codex_dir, command=codex_patch("*** Update File: model/swc/Engine.yaml"))
        assert decide_payload(payload, codex_dir).allow
        assert decide_payload(guard_payload("Bash", codex_dir, command="cat model/swc/Engine.yaml"), codex_dir).allow

    @pytest.mark.parametrize(
        ("workdir", "header", "allowed"),
        [
            ("model", "*** Update File: swc/Engine.yaml", False),
            ("model/swc", "*** Add File: New.yaml", False),
            ("notes", "*** Add File: ../model/swc/New.yaml", False),
            ("notes", "*** Add File: ../swcraft.yaml", False),
            ("notes", "*** Add File: x.md", True),
            (".", "*** Add File: notes/x.md", True),
        ],
    )
    def test_a_patch_resolves_its_headers_against_its_workdir(
        self, codex_dir: Path, workdir: str, header: str, allowed: bool
    ) -> None:
        """As a shell command's words do: ``workdir`` against ``cwd``, else ``cwd``."""
        assert (
            decide_codex_tool("apply_patch", codex_dir, command=codex_patch(header), workdir=workdir).allow is allowed
        )

    @pytest.mark.parametrize("workdir", ["", 5, ["notes"], "notes\x00"])
    def test_a_patch_workdir_that_cannot_be_judged_is_denied(self, codex_dir: Path, workdir: object) -> None:
        decision = decide_codex_tool(
            "apply_patch", codex_dir, command=codex_patch("*** Add File: x.md"), workdir=workdir
        )
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    def test_an_unreadable_manifest_denies_its_patch_from_any_workdir(self, codex_dir: Path) -> None:
        """No patch of an unreadable ``swcraft.yaml`` is a repair: the user fixes it in the editor."""
        write_unreadable_manifest(codex_dir)
        for workdir, header in (("notes", "../swcraft.yaml"), (".", "swcraft.yaml")):
            decision = decide_codex_tool(
                "apply_patch", codex_dir, command=codex_patch(f"*** Update File: {header}"), workdir=workdir
            )
            assert not decision.allow and decision.reason is not None and decision.reason.endswith(BY_HAND)


class TestCodexShell:
    """A shell command is denied when any word could name the model; the basic shapes."""

    @pytest.mark.parametrize(
        "command",
        [
            "cat model/swc/Engine.yaml",
            "sed -i s/a/b/ model/swc/Engine.yaml",
            "echo x > model/y",
            "tool --out=model/z",
            "bash -lc 'cp a model/b'",
            "ls model",
            "cd model && ls",
            "rg x model/",
            "ls model;",
            "cd model&&ls",
            "echo x>model/y",
            "python -c \"open('model/x','w')\"",
            "ls */swc",
            "cat */../model/swc/x",
            "cat {model,notes}/swc/x",
            "echo model",
            "cat ./model/swc/Engine.yaml",
            "cat notes/../model/swc/Engine.yaml",
        ],
    )
    def test_a_command_naming_the_model_is_denied(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(allow=False, reason=SHELL_REASON)

    def test_an_absolute_path_into_the_model_is_denied(self, codex_dir: Path) -> None:
        assert not decide_codex_shell(codex_dir, f"cat {codex_dir / 'model' / 'swc' / 'Engine.yaml'}").allow

    @pytest.mark.parametrize(
        "command",
        [
            "rg x",
            "swcraft validate",
            "ls notes/*.md",
            "cat {notes,dist}/x",
            "echo mymodel/x",
            r"find . -name x -exec cat {} \;",
        ],
    )
    def test_a_command_not_naming_the_model_is_allowed(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command).allow

    def test_a_nested_model_root_is_reached_through_two_wildcards(self, tmp_path: Path) -> None:
        root = codex_project(tmp_path / "proj", "arch/model")
        decision = decide_codex_shell(root, "ls */*/x")
        assert not decision.allow
        assert decision.reason == SHELL_REASON.replace("`model`", "`arch/model`")
        assert decide_codex_shell(
            root, "ls */x"
        ).allow  # `*` matches `arch`, an ancestor of the model and not inside it

    @pytest.mark.parametrize(
        "command",
        [
            "echo 'unclosed model/x",
            'echo "unclosed notes',
            "echo 'dist/model.arxml",
            "echo 'mymodel/ x",
            "ls notes; echo don't",
            "cat <<EOF > notes/x\ndon't\nEOF",
            "echo x \\",
        ],
        ids=[
            "names-the-model",
            "names-nothing",
            "a-look-alike",
            "a-look-alike-dir",
            "an-apostrophe",
            "heredoc",
            "escape",
        ],
    )
    def test_an_unparseable_command_is_denied_whatever_it_names(self, codex_dir: Path, command: str) -> None:
        """The guard fails closed, and Codex rephrases."""
        decision = decide_codex_shell(codex_dir, command)
        assert not decision.allow
        assert decision.reason is not None and "Rephrase it" in decision.reason
        assert str(codex_dir) not in decision.reason

    def test_a_workdir_in_the_model_denies_whatever_the_words(self, codex_dir: Path) -> None:
        assert not decide_codex_shell(codex_dir, "cat x", workdir="model").allow
        assert decide_codex_shell(codex_dir, "cat x", workdir="notes").allow

    def test_words_resolve_against_the_workdir(self, codex_dir: Path) -> None:
        assert not decide_codex_shell(codex_dir, "cat ../model/x", workdir="notes").allow

    @pytest.mark.parametrize(("command", "allowed"), [(["cat", "model/x"], False), (["ls", "notes"], True)])
    def test_a_list_command_is_joined(self, codex_dir: Path, command: list[str], allowed: bool) -> None:
        assert decide_codex_shell(codex_dir, command).allow is allowed

    @pytest.mark.parametrize("command", [None, 42, {"x": 1}, ["cat", 1]])
    def test_a_command_that_is_not_text_is_denied(self, codex_dir: Path, command: object) -> None:
        decision = decide_codex_shell(codex_dir, command)
        assert not decision.allow
        assert decision.reason is not None and decision.reason.startswith("SWCraft: in this project")


#: Commands the Codex dialect allows whatever else it denies: each guards against a false denial.
_STILL_ALLOWED = [
    "grep -E '\\d+' src",
    "rg '\\bx\\b'",
    "[ -f a ]",
    "if ! true; then :; fi",
    "grep '.*' notes",
    "a | b",
    "echo 'a#b' notes",
    "swcraft check-model --project swcraft.yaml",
    "cat dist/model.arxml",
    'git commit -m "don\'t break"',
    "curl https://x/y#z",
    "grep '// TODO' src",
    "ls",
    "npm test",
    "python -m pytest tests",
    "echo '(a)'",
    "git log --format='%h (%s)'",
    "python -c 'print(1)'",
    "make CC=gcc",
    "env X=1 true",
]


@pytest.mark.parametrize("command", _STILL_ALLOWED)
def test_the_false_denial_guards_are_allowed(codex_dir: Path, command: str) -> None:
    assert decide_codex_shell(codex_dir, command).allow


class TestCodexShellQuoting:
    """Comments, bash's ``$'...'`` and ``$"..."`` quoting, line continuations and NUL characters."""

    @pytest.mark.parametrize(
        "command",
        [
            "sed -i s#a#b# model/swc/Engine.yaml",
            "cat a#b model/swc/Engine.yaml",
            "curl http://h/p#f && rm -r model",
            "cat x#;cat model/y",
            "echo a#b model/x",
            "ls # model",
        ],
    )
    def test_a_hash_inside_a_word_cuts_nothing_off(self, codex_dir: Path, command: str) -> None:
        """No comment character, so a ``#`` never hides the rest of the line."""
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(allow=False, reason=SHELL_REASON)

    @pytest.mark.parametrize(
        "command",
        [
            "cat $'model'/swc/Engine.yaml",
            'cat $"model"/swc/Engine.yaml',
            "cat $'\\x6dodel'/swc/Engine.yaml",
            "cat $'\\155odel'/swc/Engine.yaml",
            "cat $'mo''del'/x",
            "cat $'\\555odel/swc/Engine.yaml'",
            "cat $'\\x6d\\x6f'del/swc/Engine.yaml",
        ],
    )
    def test_ansi_c_and_locale_quoting_are_read_as_bash_reads_them(self, codex_dir: Path, command: str) -> None:
        """``$'...'`` is decoded with its escapes and ``$"..."`` read as ``"..."`` before tokenising.

        An octal escape is one byte, its value's low eight bits, as bash makes it (``\\555`` is ``m``)."""
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(allow=False, reason=SHELL_REASON)

    @pytest.mark.parametrize(
        ("body", "value"),
        [
            ("\\555odel'", "model"),
            ("\\303\\251'", "\u00e9"),
            ("\\xc3\\xa9'", "\u00e9"),
            ("\\u00e9'", "\u00e9"),
            ("caf\u00e9\\x21'", "caf\u00e9!"),
        ],
    )
    def test_ansi_c_numeric_escapes_are_bytes_decoded_as_utf8(self, body: str, value: str) -> None:
        """Octal and hexadecimal escapes are bytes, the body's bytes then decoded as UTF-8 (bash in a UTF-8 locale).

        A Unicode escape is its character's UTF-8 bytes."""
        assert copilot_guard._ansi_c(body, 0) == (value, len(body))

    @pytest.mark.parametrize("command", ["cat $'\\xff'", "cat $'\\303'/x", "rm $'\\200notes'", "cat $'\\ud800'"])
    def test_an_ansi_c_body_that_is_no_utf8_is_denied_unjudged(self, codex_dir: Path, command: str) -> None:
        """Bytes that do not decode name a file the guard cannot spell, so the word is denied unjudged."""
        decision = decide_codex_shell(codex_dir, command)
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    def test_ansi_c_quoting_naming_nothing_is_allowed(self, codex_dir: Path) -> None:
        assert decide_codex_shell(codex_dir, "printf $'a\\tb\\n' > notes/x").allow
        assert decide_codex_shell(codex_dir, "echo \"it's $'x'\" notes").allow

    @pytest.mark.parametrize(
        "command",
        [
            "cp x mo\\\ndel/a",
            'cp x "mo\\\ndel"/a',
            "cp x ${x:-model}/a",
            "cp x ${x:=model}/a",
            "cp x ${x-model}/a",
            "cp x ${x=model}/a",
            "cp x ${x+model}/a",
            "cp x ${x:+model}/a",
            "cp x ${x?model}/a",
            "cp x ${x:?model}/a",
            "cp x ${a:-${b:-model}}/a",
            'cp x "${x:-model}"/a',
            "cp x `echo model`/a",
            'cp x "`echo model`"/a',
            "cp x `ls`model/a",
            "sed -n wmodel/x notes/a",
            "sed 's/a/b/w model/x' notes/a",
        ],
    )
    def test_a_continuation_a_parameter_default_and_a_backtick_are_read(self, codex_dir: Path, command: str) -> None:
        """A backslash-newline outside single quotes is removed, as bash removes it.

        The word after a ``${...}`` operator, each piece around a backtick and a ``sed`` script's glued ``w`` file
        are judged as words."""
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(allow=False, reason=SHELL_REASON)

    @pytest.mark.parametrize("command", ["cp x ${x:-notes}/a", "cp x `date`/a", "echo a\\\nb"])
    def test_a_continuation_a_default_or_a_backtick_naming_nothing_is_allowed(
        self, codex_dir: Path, command: str
    ) -> None:
        assert decide_codex_shell(codex_dir, command).allow

    @pytest.mark.parametrize(
        ("command", "reason"),
        [
            ("cat $'model\\0'/swc/Engine.yaml", SHELL_REASON),
            ("cat $'model/swc/Engine.yaml\\0'", SHELL_REASON),
            ("cat $'mode\\x6c\\x00junk'/swc/Engine.yaml", SHELL_REASON),
            ("cat $'model\\u0000'/swc/Engine.yaml", SHELL_REASON),
            ("cat $'model\\U00000000tail'/swc/Engine.yaml", SHELL_REASON),
            ("cat $'model\\c@'/swc/Engine.yaml", SHELL_REASON),
            ("cat $'model\\000\\''/swc/Engine.yaml", SHELL_REASON),
            ("ls $'model\\0'", SHELL_REASON),
            ("sed -n $'w model/x\\0' notes.md", SHELL_REASON),
            ("sed -n $'w mode\\x6c\\x00' notes.md", SHELL_REASON),
            ("rm $'swcraft.yaml\\0junk'", GUARD_INPUT_REASON.format(kind="command")),
            ("rm -rf $'.codex\\x00'", GUARD_INPUT_REASON.format(kind="command")),
        ],
    )
    def test_a_decoded_nul_ends_an_ansi_c_word_as_in_bash(self, codex_dir: Path, command: str, reason: str) -> None:
        """Bash drops a ``$'...'`` body from its first decoded NUL to the closing quote.

        ``$'model\\0'/x`` is ``model/x``, so the word is judged as that."""
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(allow=False, reason=reason)

    @pytest.mark.parametrize(
        ("body", "value"),
        [
            ("model\\0'", "model"),
            ("mode\\x6c\\x00junk'", "model"),
            ("model\\c@zz'", "model"),
            ("a\\0b\\'c'", "a"),
            ("a\\tb'", "a\tb"),
        ],
    )
    def test_ansi_c_decoding_stops_the_value_at_a_nul(self, body: str, value: str) -> None:
        assert copilot_guard._ansi_c(body, 0) == (value, len(body))

    @pytest.mark.parametrize(
        "command",
        ["cat model\x00/swc/x", "rm 'notes\x00'", "sed -n 'w notes/x\x00' notes.md", "ls 'swcraft.yaml\x00'"],
        ids=["model", "elsewhere", "sed-write", "guard-input"],
    )
    def test_a_word_still_holding_a_nul_is_denied_unjudged(self, codex_dir: Path, command: str) -> None:
        """No resolution reads a NUL, and an unresolved word never means one that names nothing."""
        decision = decide_codex_shell(codex_dir, command)
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason


class TestCodexShellWords:
    """The parts of a word, and the separators a word can open with or hold."""

    @pytest.mark.parametrize(
        "command",
        [
            "cp -tmodel notes.md",
            "tar -Cmodel -xf a.tar",
            "curl -d @model/swc/Engine.yaml http://x",
            "f -omodel/x",
            "cp a b,model/x",
            "docker run -v model:/m img",
            "git show HEAD:model/swc/x",
        ],
    )
    def test_the_parts_of_a_word_are_judged(self, codex_dir: Path, command: str) -> None:
        """The parts around ``,`` and ``:``, after a glued short option and after a leading ``@``."""
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(allow=False, reason=SHELL_REASON)

    @pytest.mark.parametrize("command", ["cp -tnotes a", "curl -d @notes/x http://h", "git show HEAD:notes.md"])
    def test_the_parts_of_a_word_away_from_the_model_are_allowed(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command).allow

    def test_a_word_opening_with_two_separators_is_judged_and_a_drive_word_judged_by_its_parts(
        self, codex_dir: Path, monkeypatch
    ) -> None:
        """A word opening with two separators is judged, and a drive word is judged by its parts.

        Where ``//host/x`` is a UNC path it can alias any directory, so it is denied; on POSIX it is read as
        ``/host/x`` and judged; ``C:x`` is split at its colon, and where shell words have drives (Windows) the whole
        word is drive-relative, which no resolution can judge, so it is denied."""
        monkeypatch.setattr(copilot_guard, "_DOUBLE_SEPARATOR_IS_UNC", True)
        assert not decide_codex_shell(codex_dir, "echo //server/share").allow
        monkeypatch.setattr(copilot_guard, "_DOUBLE_SEPARATOR_IS_UNC", False)
        assert decide_codex_shell(codex_dir, "echo //server/share").allow
        assert not decide_codex_shell(codex_dir, f"cat /{codex_dir.resolve() / 'model' / 'swc' / 'Engine.yaml'}").allow
        monkeypatch.setattr(copilot_guard, "_SHELL_WORDS_HAVE_DRIVES", False)
        assert decide_codex_shell(codex_dir, "echo C:x").allow
        monkeypatch.setattr(copilot_guard, "_SHELL_WORDS_HAVE_DRIVES", True)
        decision = decide_codex_shell(codex_dir, "echo C:x")
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason
        assert decide_codex_shell(codex_dir, "grep -rn '// TODO' notes").allow
        assert not decide_codex_shell(codex_dir, "echo C:model").allow

    @pytest.mark.parametrize(
        "command", ["grep -rn '//TODO' src", "rg '//\\s*TODO' src", "sed -i '' '//d' notes/a", "ls //tmp"]
    )
    def test_two_leading_separators_are_one_on_posix_and_a_unc_path_on_windows(
        self, codex_dir: Path, monkeypatch, command: str
    ) -> None:
        """On POSIX a leading ``//`` resolves as ``/``, so the word is judged as that path.

        Where ``os.name == "nt"`` it may be a UNC path, which aliases anything, so it stays denied."""
        monkeypatch.setattr(copilot_guard, "_DOUBLE_SEPARATOR_IS_UNC", False)
        assert decide_codex_shell(codex_dir, command).allow
        monkeypatch.setattr(copilot_guard, "_DOUBLE_SEPARATOR_IS_UNC", True)
        decision = decide_codex_shell(codex_dir, command)
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    def test_two_leading_separators_into_the_model_or_the_guards_inputs_are_denied_on_posix(
        self, codex_dir: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(copilot_guard, "_DOUBLE_SEPARATOR_IS_UNC", False)
        root = codex_dir.resolve()
        assert decide_codex_shell(codex_dir, f"cat //{root / 'model' / 'swc' / 'x'}") == copilot_guard.Decision(
            allow=False, reason=SHELL_REASON
        )
        assert decide_codex_shell(codex_dir, f"rm -rf ///{root / '.codex'}") == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )

    @pytest.mark.parametrize(("separates", "allowed"), [(True, False), (False, True)])
    def test_a_backslash_separates_on_windows(
        self, codex_dir: Path, monkeypatch, separates: bool, allowed: bool
    ) -> None:
        """Where a backslash is a separator the text is judged a second time with each one a ``/``.

        On POSIX bash reads it as an escape, ``modelswcEngine.yaml``."""
        monkeypatch.setattr(copilot_guard, "_BACKSLASH_SEPARATES", separates)
        assert decide_codex_shell(codex_dir, "type model\\swc\\Engine.yaml").allow is allowed


class TestCodexShellParameters:
    """``${name<op>word}``: the word bash may put in its place, and a ``${`` split or left open."""

    @pytest.mark.parametrize(
        ("word", "read"),
        [
            ("${x:-model}/a", "model/a"),
            ("${a:-${b:-model}}/a", "model/a"),
            ("${?:-model}", "model"),
            ("${#-model}", "model"),
            ("${arr[0]:=model}", "model"),
            ("${x#model}", "${x#model}"),
            ("${x#${y:-model}}", "${x#model}"),
            ("${x:-model", "${x:-model"),
            ("${?" + "?" * 100, "${?" + "?" * 100),
        ],
    )
    def test_a_parameter_word_is_read_as_bash_may_expand_it(self, word: str, read: str) -> None:
        assert copilot_guard._parameter_words(word) == read

    def test_a_parameter_default_naming_the_model_is_still_denied(self, codex_dir: Path) -> None:
        assert decide_codex_shell(codex_dir, "echo ${x:-model}/a") == copilot_guard.Decision(
            allow=False, reason=SHELL_REASON
        )

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf ${x:-model x}",
            "rm -rf ${x:-notes model}",
            "rm -rf ${x:+notes model}",
            "rm -rf ${x-model }",
            "rm -rf ${x:-x model}",
            "rm -rf ${x:-${y:-model x}}",
            "rm -rf ${x[0]:-y model}",
            "rm -rf ${x:-\tmodel}",
            "rm -rf ${x:-\nmodel}",
            "bash -c 'rm -rf ${x:-notes model}'",
        ],
    )
    def test_a_parameter_word_split_by_whitespace_is_joined_again(self, codex_dir: Path, command: str) -> None:
        """Bash reads a ``${...}`` to its closing brace whatever whitespace it holds.

        Its word is then split into fields, each judged."""
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(allow=False, reason=SHELL_REASON)

    def test_a_joined_parameter_word_naming_a_guard_input_is_denied(self, codex_dir: Path) -> None:
        assert decide_codex_shell(codex_dir, "rm ${x:-notes swcraft.yaml}") == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )

    @pytest.mark.parametrize("command", ["rm -rf ${x:-notes stuff}", "echo ${x:-a b} c", "ls ${x:-notes src}"])
    def test_a_joined_parameter_word_naming_nothing_is_allowed(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command).allow

    @pytest.mark.parametrize("command", ["echo ${x:-a b", "ls notes; echo ${", "bash -c 'echo ${x:-a b'"])
    def test_a_parameter_word_unclosed_at_the_end_is_denied_unjudged(self, codex_dir: Path, command: str) -> None:
        decision = decide_codex_shell(codex_dir, command)
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    @pytest.mark.parametrize(
        "command",
        ['cat ${x:-"my model/x" notes}', "cat ${x:-'my model/x' notes}", 'cat ${x:-a "b c"}', 'cat "a"${x:-b c}'],
    )
    def test_a_split_parameter_word_holding_a_quote_is_denied_unjudged(self, tmp_path: Path, command: str) -> None:
        """A split parameter word holding a quote is denied unjudged, whatever it names.

        Quoting is gone once ``shlex`` has split the run, so its fields cannot be rebuilt: ``"my model/x"`` is one
        field to bash and two to a blank split. This is a false denial for a quoted default that names nothing (the
        last two)."""
        root = codex_project(tmp_path / "p", "my model")
        (root / "my model").mkdir()
        decision = decide_codex_shell(root, command)
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason

    @pytest.mark.parametrize("command", ['cat "${x:-my model/x}"', "cat ${x:-'my model/x'}"])
    def test_an_unsplit_quoted_parameter_word_is_still_judged(self, tmp_path: Path, command: str) -> None:
        root = codex_project(tmp_path / "p", "my model")
        (root / "my model").mkdir()
        decision = decide_codex_shell(root, command)
        assert not decision.allow and decision.reason is not None and "names it" in decision.reason


class TestCodexShellGlobs:
    """Wildcards, climbing components, hidden names and bracket expressions, read as bash reads them."""

    @pytest.mark.parametrize(
        "command",
        [
            "cat ?????/swc/Engine.yaml",
            "rm -rf ?*",
            "rm -rf [m]*",
            "cat [a-z]*/swc/Engine.yaml",
            "cat .*/model/x",
            "ls *",
        ],
    )
    def test_a_component_that_could_climb_is_also_read_as_itself_and_as_a_dot(
        self, codex_dir: Path, command: str
    ) -> None:
        """``?*`` is judged as ``..``, as ``.`` and as the pattern it is; any reading reaching the model denies."""
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(allow=False, reason=SHELL_REASON)

    @pytest.mark.parametrize("command", ["ls *", "rm -rf ?*", "cat .*/x"])
    def test_a_climbing_glob_away_from_the_model_is_allowed(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir / "notes", command).allow

    @pytest.mark.parametrize(
        ("model_root", "command"),
        [
            ("mod[el]", "cat 'mod[el]/x'"),
            ("mod[el]", "cat mod[el]/x"),
            ("mod[el]", "rm -rf 'mod[el]'"),
            ("mod{a,b}", "cat 'mod{a,b}/x'"),
            ("mod{a,b}", "cat mod{a,b}/x"),
            ("m*", "cat 'm*/x'"),
            ("notes/[x]", "cat 'notes/[x]/y'"),
        ],
    )
    def test_a_glob_or_brace_word_is_also_judged_as_its_literal_name(
        self, tmp_path: Path, model_root: str, command: str
    ) -> None:
        """A quoted or unmatched glob or brace word is also judged as the literal path it names.

        A quoted word, or a glob that matches nothing (bash then passes it on as written), opens the directory whose
        real name holds the glob or brace characters: each such word is judged as that literal path too."""
        root = codex_project(tmp_path / "p", model_root)
        # Windows refuses `*` in a name: there the directory is not created and the word is judged by its
        # literal name alone, which must deny all the same (filesystem_support's probe, never a platform check).
        if all(accepts_path_segment(tmp_path, part) for part in model_root.split("/")):
            (root / model_root).mkdir(parents=True)
        decision = decide_codex_shell(root, command)
        assert not decision.allow and decision.reason is not None and "names it" in decision.reason

    @pytest.mark.parametrize(
        ("pattern", "below", "reaches"),
        [
            (["*"], [".codex"], False),
            (["?codex"], [".codex"], False),
            (["[.]codex"], [".codex"], False),
            (["*dex"], [".codex"], False),
            (["**"], [".codex"], False),
            (["**", "*"], ["a", ".codex"], False),
            ([".*"], [".codex"], True),
            ([".?odex"], [".codex"], True),
            ([".codex"], [".codex"], True),
            (["**", ".codex"], ["a", ".codex"], True),
            (["**"], ["a", "b"], True),
            (["**", "x"], ["a", "b"], True),
            (["*"], ["model"], True),
            (["*", "y"], ["a", "b"], False),
            (["*"], ["a", "b"], False),
            (["*", "*", "*"], ["a", "b"], True),
        ],
    )
    def test_a_dot_name_is_matched_only_by_a_leading_literal_dot(
        self, pattern: list[str], below: list[str], reaches: bool
    ) -> None:
        """As bash without ``dotglob``: ``*``, ``?`` and a bracket expression never match a leading ``.``."""
        assert copilot_guard._could_reach(pattern, below) is reaches

    @pytest.mark.parametrize(
        "command",
        [
            "bash -O extglob -c 'cat !(notmodel)/swc/Engine.yaml'",
            "cat !(notmodel)/swc/Engine.yaml",
            "cat @(model)/swc/Engine.yaml",
            "cat +(m)odel/swc/Engine.yaml",
            "cat *(m)odel/swc/Engine.yaml",
            "cat ?(x)odel/swc/Engine.yaml",
            "rm -rf !(notes)",
            "rm -rf @(a|@(b))*",
        ],
    )
    def test_an_extglob_group_is_read_as_a_component_matching_any_name(self, codex_dir: Path, command: str) -> None:
        """``shlex`` splits a word at ``(``, but with ``extglob`` on bash reads the group as a pattern.

        ``!(notmodel)`` matches the model directory."""
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(allow=False, reason=SHELL_REASON)

    @pytest.mark.parametrize("command", ["rm -rf @(.codex)", "mv @(.c*|x) y", "rm -rf @(@(.codex))"])
    def test_an_extglob_group_holding_a_dot_can_name_a_hidden_name(self, codex_dir: Path, command: str) -> None:
        """Read as ``{.*,*}`` (:meth:`test_the_extglob_reading`), its ``.*`` reading reaches ``.codex``.

        Its ``*`` reading reaches the model, which at the project root is the reason given."""
        assert not decide_codex_shell(codex_dir, command).allow

    @pytest.mark.parametrize("command", ["rm -rf !(x)", "rm -rf @(a|b)", "cat !(codex)"])
    def test_an_extglob_group_away_from_the_model_is_allowed(self, codex_dir: Path, command: str) -> None:
        """Without a ``.`` the group matches no hidden name, ``.codex`` included."""
        assert decide_codex_shell(codex_dir / "notes", command).allow

    @pytest.mark.parametrize(
        ("text", "read"),
        [
            ("cat !(notmodel)/x", "cat */x"),
            ("cat @(.codex)/x", "cat {.*,*}/x"),
            ("a @(@(.c)|b)/x", "a {.*,*}/x"),
            ("a !(b(c)d)e", "a *e"),
            ("echo (a)", None),
            ("x !(y", None),
            ("echo ok", None),
        ],
    )
    def test_the_extglob_reading(self, text: str, read: str | None) -> None:
        assert copilot_guard._extglob_read(text) == read

    def test_a_command_of_globstar_words_is_decided(self, codex_dir: Path) -> None:
        word = "notes/../" + "**/" * 1300 + "x"
        assert not decide_codex_shell(codex_dir, "ls " + " ".join([word] * 15)).allow

    @pytest.mark.parametrize("command", ["rm -rf ?codex", "rm -rf [.]codex", "rm -rf *dex", "mv *odex x"])
    def test_a_glob_without_a_leading_dot_never_names_the_codex_folder(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command).allow

    @pytest.mark.parametrize("command", ["rm -rf .?odex", "rm -rf .c*", "mv .* x", "rm -rf .[c]odex"])
    def test_a_glob_with_a_leading_dot_still_names_the_codex_folder(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )

    def test_only_a_component_with_a_leading_dot_climbs_in_the_shell(self, codex_dir: Path) -> None:
        """Bash never matches ``.`` or ``..`` with ``?``, ``*`` or ``[.]``; ``.?`` and ``.*`` can."""
        below = codex_dir / "notes"
        assert decide_codex_shell(below, "cat ?/model/swc/Engine.yaml").allow
        assert decide_codex_shell(below, "cat [.][.]/model/swc/Engine.yaml").allow
        assert decide_codex_shell(below, "cat .?/model/swc/Engine.yaml") == copilot_guard.Decision(
            allow=False, reason=SHELL_REASON
        )
        assert decide_codex_shell(below, "cat .*/model/swc/Engine.yaml") == copilot_guard.Decision(
            allow=False, reason=SHELL_REASON
        )

    @pytest.mark.parametrize(
        ("command", "reason"),
        [
            ("rm -rf m[^x]del", SHELL_REASON),
            ("rm -rf [^a]odel", SHELL_REASON),
            ("rm -rf m[[:alpha:]]del", SHELL_REASON),
            ("rm -rf [[:lower:]]odel/swc", SHELL_REASON),
            ("touch m[[:alnum:]]del/swc/New.yaml", SHELL_REASON),
            ("sed -i s/a/b/ [[:alpha:]]*/swc/Engine.yaml", SHELL_REASON),
            ("cp notes/x [[:alpha:]][[:alpha:]][[:alpha:]][[:alpha:]][[:alpha:]]/swc/", SHELL_REASON),
            ("rm -rf mod[[.e.]]l", SHELL_REASON),
            ("rm -rf mod[[=e=]]l", SHELL_REASON),
            ("rm -rf m[^]x]del", SHELL_REASON),
            ("rm -f [[:alpha:]]wcraft.yaml", GUARD_INPUT_REASON.format(kind="command")),
            ("rm -f sw[^x]raft.yaml", GUARD_INPUT_REASON.format(kind="command")),
            ("rm -f .[[:alpha:]]odex/hooks.json", GUARD_INPUT_REASON.format(kind="command")),
        ],
    )
    def test_a_bracket_expression_is_read_as_bash_reads_it(self, codex_dir: Path, command: str, reason: str) -> None:
        """A leading ``^`` negates as ``!`` does.

        A class, collating symbol or equivalence class (``[:alpha:]``, ``[.e.]``, ``[=e=]``) is read as matching any
        one character."""
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(allow=False, reason=reason)

    @pytest.mark.parametrize(
        ("name", "component", "matches"),
        [
            ("model", "m[^x]del", True),
            ("mxdel", "m[^x]del", False),
            ("model", "m[!x]del", True),
            ("model", "m[[:alpha:]]del", True),
            ("model", "mod[[.e.]]l", True),
            ("model", "[[:alpha:]]", False),
            ("^odel", "[^a]odel", True),
            ("model", "m[]o]del", True),
            ("m[del", "m[del", True),
            (".codex", "[[:punct:]]codex", False),
            (".codex", ".[[:alpha:]]odex", True),
            ("model", "m[[:alpha:]del", False),
        ],
    )
    def test_a_component_matches_as_bash_brackets_do(self, name: str, component: str, matches: bool) -> None:
        assert copilot_guard._matches_component(name, component) is matches

    @pytest.mark.parametrize(
        "command",
        ["grep '[[:space:]]' notes", "rm -f notes/[^a]*", "ls notes/[[:alpha:]]*", "grep '[^x]' src"],
    )
    def test_a_bracket_expression_naming_nothing_is_allowed(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command).allow

    @pytest.mark.parametrize("command", ["git add *", "du -sh *", "find . -name '*.yaml'"])
    def test_a_non_reader_glob_matching_the_manifest_is_the_recorded_false_denial(
        self, codex_dir: Path, command: str
    ) -> None:
        """What stays denied, documented: a glob that could match ``swcraft.yaml`` in the folder holding it."""
        assert not decide_codex_shell(codex_dir, command).allow

    @pytest.mark.parametrize(
        "command",
        [
            "cat short*/swc/Engine.yaml",
            "rm -rf short?ut",
            "cat [s]hortcut/swc/Engine.yaml",
            "cat notes/inner/*/Engine.yaml",
            "cat notes/**/Engine.yaml",
            "touch notes/l*/../swc/New.yaml",
        ],
    )
    def test_a_glob_matching_a_link_into_the_model_is_denied(self, codex_dir: Path, command: str) -> None:
        """A glob's real matches are resolved too.

        So a link a wildcard matches is judged as what it resolves to, not only by the pattern's name."""
        symlink_or_skip(codex_dir / "shortcut", codex_dir / "model")
        (codex_dir / "notes" / "inner").mkdir()
        symlink_or_skip(codex_dir / "notes" / "inner" / "lnk", codex_dir / "model" / "swc")
        symlink_or_skip(codex_dir / "notes" / "lnk", codex_dir / "model" / "swc")
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(allow=False, reason=SHELL_REASON)

    def test_a_glob_matching_a_link_to_a_guard_input_is_denied_outside_a_reader(self, codex_dir: Path) -> None:
        symlink_or_skip(codex_dir / "notes" / "hk", codex_dir / ".codex")
        assert decide_codex_shell(codex_dir, "rm -rf notes/h*") == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )
        assert decide_codex_shell(codex_dir, "cat notes/h*/hooks.json").allow

    def test_a_glob_over_an_ordinary_tree_is_allowed(self, codex_dir: Path) -> None:
        (codex_dir / "src").mkdir()
        for name in ("a.py", "b.py", ".hidden.py"):
            (codex_dir / "src" / name).write_text("x\n", encoding="utf-8", newline="\n")
        assert decide_codex_shell(codex_dir, "cat src/*.py").allow
        assert decide_codex_shell(codex_dir, "rm -rf notes/*").allow

    def test_a_glob_over_a_directory_past_the_entry_cap_is_the_recorded_false_denial(self, codex_dir: Path) -> None:
        """Past :data:`_MAX_GLOB_ENTRIES` entries read for one glob it cannot be judged in time.

        It is denied with the budget's reason, although nothing it matches is the model -- a false denial, recorded."""
        crowded = codex_dir / "notes" / "crowded"
        crowded.mkdir()
        for index in range(copilot_guard._MAX_GLOB_ENTRIES + 1):
            (crowded / f"f{index}.txt").touch()
        decision = decide_codex_shell(codex_dir, "cat notes/crowded/*.txt")
        assert not decision.allow and decision.reason is not None and _BUDGET_MARK in decision.reason
        assert decide_codex_shell(codex_dir, "cat notes/crowded/f1.txt").allow

    @pytest.mark.parametrize("command", ["find . -name Engine.yaml -delete", "find . -name hooks.json -delete"])
    def test_a_command_reaching_a_file_without_naming_it_is_not_reached(self, codex_dir: Path, command: str) -> None:
        """No word names the model or the ``.codex`` folder, so the command is allowed.

        The guide's examples of what the guard does not reach: it is allowed although ``find``
        deletes a file there."""
        assert decide_codex_shell(codex_dir, command).allow


#: The Copilot dialect's answers recorded before the Codex dialect existed, on these payloads in
#: :func:`project_dir` with a ``notes`` folder: the Codex shell judge's bash readings must never change them
#: (``True`` is allowed).
_COPILOT_AS_BEFORE = [
    ("Glob", {"pattern": "{1..300}.md", "path": "notes"}, True),
    ("Glob", {"pattern": "file{1..1000}.txt", "path": "notes"}, True),
    ("Grep", {"pattern": "x", "path": "notes", "glob": "{1..300}.md"}, True),
    ("Glob", {"pattern": "../{l..m}odel/x", "path": "notes"}, True),
    ("Glob", {"pattern": "notes/file{1..1000}.txt", "path": "notes"}, True),
    ("Glob", {"pattern": "{a..c}/*.md", "path": "notes"}, True),
    ("Glob", {"pattern": "{l..m}odel/**", "path": "."}, False),
    ("Glob", {"pattern": "../model/x", "path": "notes"}, False),
    ("Glob", {"pattern": "../{model,x}/*", "path": "notes"}, False),
    ("Glob", {"pattern": "*", "path": "notes"}, True),
    ("Glob", {"pattern": ".*", "path": "notes"}, False),
    ("Glob", {"pattern": "{a,b}" * 9, "path": "notes"}, False),
    ("Read", {"path": "model/swc/Engine.yaml"}, False),
    ("Read", {"path": "notes/x"}, True),
    ("Grep", {"pattern": "x", "path": "notes"}, True),
]


class TestCodexShellBraces:
    """Brace groups and sequence expressions: expanded as bash expands them in the shell judge alone."""

    @pytest.mark.parametrize(
        ("command", "reason"),
        [
            ("rm -rf {l..m}odel", SHELL_REASON),
            ("rm -rf mode{a..z}", SHELL_REASON),
            ("rm -rf mode{z..a}", SHELL_REASON),
            ("rm -rf mode{b..z..2}", SHELL_REASON),
            ("rm -rf .{c..c}odex", GUARD_INPUT_REASON.format(kind="command")),
        ],
    )
    def test_a_brace_sequence_is_expanded_as_bash_expands_it(self, codex_dir: Path, command: str, reason: str) -> None:
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(allow=False, reason=reason)

    @pytest.mark.parametrize(
        ("glob", "terms"),
        [
            ("{1..3}", ["1", "2", "3"]),
            ("{3..1}", ["3", "2", "1"]),
            ("{1..7..3}", ["1", "4", "7"]),
            ("{1..7..-3}", ["1", "4", "7"]),
            ("{1..3..0}", ["1", "2", "3"]),
            ("{01..10}", [f"{n:02d}" for n in range(1, 11)]),
            ("{01..100}", [f"{n:02d}" for n in range(1, 101)]),
            ("{-01..1}", ["-01", "000", "001"]),
            ("{a..e..2}", ["a", "c", "e"]),
            ("{a..5}", ["a..5"]),
        ],
    )
    def test_a_sequence_gives_bashs_terms(self, glob: str, terms: list[str]) -> None:
        assert sorted(copilot_guard._brace_expansions(glob, sequences=True) or []) == sorted(terms)

    @pytest.mark.parametrize("command", ["echo {1..3}", "touch f{1..3}.txt", "touch notes/f{01..10}.txt"])
    def test_a_sequence_naming_nothing_is_allowed(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command).allow

    def test_unbalanced_braces_are_no_groups(self) -> None:
        assert copilot_guard._brace_group_count("}" * 100 + "{" * 100) == 0
        assert copilot_guard._brace_group_count("{a,{b}}{") == 2

    def test_only_the_shell_judge_expands_a_sequence(self) -> None:
        assert copilot_guard._brace_expansions("f{1..3}") == ["f1..3"]
        assert sorted(copilot_guard._brace_expansions("f{1..3}", sequences=True) or []) == ["f1", "f2", "f3"]

    def test_copilot_globs_keep_their_brace_reading(self) -> None:
        """The group bound and the charge are the shell judge's alone: a Copilot glob expands as before."""
        assert copilot_guard._brace_expansions("{a}" * 100) == ["a" * 100]

    @pytest.mark.parametrize(("tool", "arguments", "allowed"), _COPILOT_AS_BEFORE)
    def test_copilot_decides_as_before_the_codex_dialect(
        self, project_dir: Path, tool: str, arguments: dict[str, str], allowed: bool
    ) -> None:
        """Brace sequences are a bash reading of the shell judge alone.

        A Copilot glob's ``{1..300}`` is expanded as before, to its one alternative."""
        (project_dir / "notes").mkdir()
        assert decide_payload(guard_payload(tool, project_dir, **arguments), project_dir).allow is allowed


class TestCodexShellSed:
    """A ``sed`` command's write files and the words it hands a shell."""

    @pytest.mark.parametrize(
        "command",
        [
            "sed -i 's/new.*/x/' src/a.py",
            "sed -n '/window.*/p' src/a.py",
            "sed -n '/allow*/p' src/a.py",
            "sed -i 's/show */x/' src/a.py",
            "sed -n '/^\\w*/p' src/a.py",
            "sed -n 's/[/]/x/p' src/a.py",
            "sed -n '/x/{p;q}' src/a.py",
        ],
    )
    def test_a_sed_script_writes_only_where_sed_reads_a_file_name(self, codex_dir: Path, command: str) -> None:
        """A ``sed`` write file is one literal path, read only where ``sed`` reads a file name.

        That is after a ``w``/``W`` command or an ``s`` command's ``w`` flag, and a word ``sed`` hands no shell is
        not tokenised again."""
        assert decide_codex_shell(codex_dir, command).allow

    @pytest.mark.parametrize(
        ("command", "reason"),
        [
            ("sed -n wswcraft.yaml notes/a", GUARD_INPUT_REASON.format(kind="command")),
            ("sed s/a/b/w.codex/hooks.json notes/a", GUARD_INPUT_REASON.format(kind="command")),
            ("sed -n wmodel/x", SHELL_REASON),
            ("sed -n '/x/wmodel/y' notes/a", SHELL_REASON),
            ("sed -n '1{wswcraft.yaml\n}' notes/a", GUARD_INPUT_REASON.format(kind="command")),
            ("sed -n 'p;wswcraft.yaml' notes/a", GUARD_INPUT_REASON.format(kind="command")),
            ("sed -e 's/a/b/gw swcraft.yaml' notes/a", GUARD_INPUT_REASON.format(kind="command")),
            ("sed -n '$Wswcraft.yaml' notes/a", GUARD_INPUT_REASON.format(kind="command")),
            ("sed -ne'wswcraft.yaml' notes/a", GUARD_INPUT_REASON.format(kind="command")),
            ("sed --expression=wswcraft.yaml notes/a", GUARD_INPUT_REASON.format(kind="command")),
            ("sed -n 's/[/]/x/;wswcraft.yaml' notes/a", GUARD_INPUT_REASON.format(kind="command")),
            ("sed -n 'e rm -rf model' notes/a", SHELL_REASON),
            ("sed 's|a|cat model/x|e' notes/a", SHELL_REASON),
            # A word after the command name is an argument, never an assignment: ``s=a=b=w`` is a script.
            ("sed -n 's=a=b=wswcraft.yaml' notes.md", GUARD_INPUT_REASON.format(kind="command")),
            ("sed -n 's=a=b=wmodel/x' notes.md", SHELL_REASON),
            ("X=1 sed -n 's=a=b=wmodel/x' notes.md", SHELL_REASON),
        ],
    )
    def test_a_sed_write_or_command_is_still_judged(self, codex_dir: Path, command: str, reason: str) -> None:
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(allow=False, reason=reason)

    @pytest.mark.parametrize(
        "command",
        [
            "command sed -n wswcraft.yaml notes.md",
            "command -p sed -n wswcraft.yaml notes.md",
            "env sed -n wswcraft.yaml notes.md",
            "env -i X=1 sed -n wswcraft.yaml notes.md",
            "env -u HOME sed -n wswcraft.yaml notes.md",
            "exec sed -n wswcraft.yaml notes.md",
            "exec -a name sed -n wswcraft.yaml notes.md",
            "exec -cl sed -n wswcraft.yaml notes.md",
            "builtin sed -n wswcraft.yaml notes.md",
            "nice -n 5 sed -n wswcraft.yaml notes.md",
            "nohup sed -n wswcraft.yaml notes.md",
            "stdbuf -o L sed -n wswcraft.yaml notes.md",
            "timeout --signal=KILL 5 sed -n wswcraft.yaml notes.md",
            "env command nice sed -n wswcraft.yaml notes.md",
            "/usr/bin/env sed -n wswcraft.yaml notes.md",
            "xargs sed -n wswcraft.yaml < list",
            "xargs -I{} sed -i s/a/b/w.codex/hooks.json {} < list",
            "find notes -exec sed -n wswcraft.yaml {} +",
            "sudo sed -n wswcraft.yaml notes.md",
            "env --unknown-option sed -n wswcraft.yaml notes.md",
        ],
    )
    def test_a_wrapped_sed_is_scanned_for_its_write_files(self, codex_dir: Path, command: str) -> None:
        """A wrapper runs the ``sed`` it names: every word named ``sed`` opens a scan of the words after it.

        A wrapper is ``command``, ``env``, ``exec``, ``nice``, ``xargs``, ``find -exec`` or any other."""
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )

    @pytest.mark.parametrize(
        ("words", "command"),
        [
            (["command", "-p", "sed", "x"], ["sed", "x"]),
            (["env", "-i", "-u", "A", "-C", "d", "B=1", "--", "sed"], ["sed"]),
            (["exec", "-la", "n", "sed"], ["sed"]),
            (["nice", "-n5", "nohup", "stdbuf", "-oL", "-e", "0", "sed"], ["sed"]),
            (["timeout", "-k", "1", "--preserve-status", "5s", "sed"], ["sed"]),
            (["cat", "x"], ["cat", "x"]),
            (["env", "-S", "sed x"], None),
            (["timeout"], None),
            (["command", "-z", "sed"], None),
        ],
    )
    def test_a_wrapper_is_unwrapped_to_the_command_it_runs(self, words: list[str], command: list[str] | None) -> None:
        unwrapped = copilot_guard._unwrapped(words)
        assert (unwrapped[0] if unwrapped is not None else None) == command

    def test_a_wrapped_sed_keeps_its_words_inert(self, codex_dir: Path) -> None:
        """The unwrapped ``sed`` hands no shell its script, so ``s/show */x/`` is no ``*/x/`` glob."""
        assert decide_codex_shell(codex_dir, "env sed -i 's/show */x/' src/a.py").allow
        assert decide_codex_shell(codex_dir, "command sed -n '/x/p' notes.md").allow

    @pytest.mark.parametrize("target", ["mod?l", "m{o,x}del/x", "*/x"])
    def test_a_sed_write_file_is_never_globbed(self, codex_dir: Path, target: str) -> None:
        """``sed`` opens the name as written, so only that literal path is judged.

        As a glob each of these could name the model, as written none does."""
        assert copilot_guard._sed_reading(["sed", "-n", f"w {target}", "src/a.py"])[0] == [target]
        assert decide_codex_shell(codex_dir, f"sed -n 'w {target}' src/a.py").allow

    def test_a_sed_write_file_cut_by_a_nul_is_the_name_bash_hands_sed(self, codex_dir: Path) -> None:
        """``$'w mod\\x00'`` is ``w mod``: ``sed`` writes ``mod``, which is not the model."""
        assert decide_codex_shell(codex_dir, "sed -n $'w mod\\x00' notes.md").allow

    def test_an_unresolvable_sed_write_file_is_denied_unjudged(self, codex_dir: Path) -> None:
        judge = copilot_guard._ShellJudge(codex_dir, str(codex_dir.resolve() / "model"), ())
        assert judge._literal("notes/x\x00") == copilot_guard._UNJUDGED

    @pytest.mark.parametrize(
        ("command", "reason"),
        [
            ('sed -n p "$(rm -rf model)"', SHELL_REASON),
            ('sed -n p "`rm -rf model`"', SHELL_REASON),
            ('sed -n p "$(rm swcraft.yaml)"', GUARD_INPUT_REASON.format(kind="command")),
            ("sed -n p <(rm -rf model)", SHELL_REASON),
        ],
    )
    def test_a_substitution_in_a_sed_word_is_read_as_shell(self, codex_dir: Path, command: str, reason: str) -> None:
        """A word holding a backtick, ``$(``, ``<(`` or ``>(`` is never inert: the shell runs what it holds."""
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(allow=False, reason=reason)

    @pytest.mark.parametrize("command", ['sed -n p "$(date)" notes.md', "sed 's/show */x/' notes.md"])
    def test_a_sed_word_naming_nothing_is_still_allowed(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command).allow


class TestCodexShellNestedShells:
    """A word holding shell text is tokenised again, to a bounded depth."""

    @pytest.mark.parametrize("wraps", [1, 4, 5, 6, 8])
    def test_retokenising_stops_at_its_depth_and_denies_past_it(self, codex_dir: Path, wraps: int) -> None:
        """Every level up to :data:`_MAX_SHELL_DEPTH` is judged.

        Past it a word still holding whitespace or a quote is denied unjudged, so no wrapping count reaches the
        model."""
        command = "cat model/x"
        for _ in range(wraps):
            command = f"bash -c {shlex.quote(command)}"
        assert not decide_codex_shell(codex_dir, command).allow

    def test_past_the_depth_even_a_harmless_command_is_denied(self, codex_dir: Path) -> None:
        command = "ls notes"
        for _ in range(copilot_guard._MAX_SHELL_DEPTH + 2):
            command = f"bash -c {shlex.quote(command)}"
        decision = decide_codex_shell(codex_dir, command)
        assert not decision.allow and decision.reason is not None and UNJUDGED_MARK in decision.reason
        shallow = "ls notes"
        for _ in range(copilot_guard._MAX_SHELL_DEPTH):
            shallow = f"bash -c {shlex.quote(shallow)}"
        assert decide_codex_shell(codex_dir, shallow).allow

    @pytest.mark.parametrize(
        ("command", "reason"),
        [
            ("bash -c 'echo>model/swc/x.yaml'", SHELL_REASON),
            ("sh -c 'cat<model/swc/x.yaml'", SHELL_REASON),
            ("bash -c 'true;rm model/x'", SHELL_REASON),
            ("bash -c 'true&&rm model/x'", SHELL_REASON),
            ("bash -c 'ls|tee model/x'", SHELL_REASON),
            ("bash -c 'echo>swcraft.yaml'", GUARD_INPUT_REASON.format(kind="command")),
            ("bash -c 'echo>.codex/hooks.json'", GUARD_INPUT_REASON.format(kind="command")),
            ("bash -c 'cat<notes.md>swcraft.yaml'", GUARD_INPUT_REASON.format(kind="command")),
        ],
    )
    def test_a_nested_script_with_operators_and_no_blank_is_read_again(
        self, codex_dir: Path, command: str, reason: str
    ) -> None:
        """A word holding a shell operator character is tokenised again, as one holding a blank or a quote."""
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(allow=False, reason=reason)

    def test_a_nested_script_with_operators_reaches_a_nested_model_root(self, tmp_path: Path) -> None:
        root = codex_project(tmp_path / "proj", "arch/model")
        assert not decide_codex_shell(root, "bash -c 'echo>arch/model/x'").allow
        assert not decide_codex_shell(root, 'sed -n p "$(rm -rf arch/model)"').allow

    @pytest.mark.parametrize(
        "command",
        [
            "grep -E 'a|b' notes",
            "echo 'a;b' > notes/x",
            "git log --format='%h|%s'",
            "curl 'https://x/y?a=1&b=2'",
            "bash -c 'echo>notes/x'",
            "sh -c 'cat<notes.md'",
        ],
    )
    def test_an_operator_in_a_word_naming_nothing_is_allowed(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command).allow


class TestCodexShellBudget:
    """The work is bounded: past the budget a command is denied whatever it names, and every scan is linear."""

    def test_a_word_past_the_expansion_bound_is_denied_by_the_budget(self, codex_dir: Path) -> None:
        """Past :data:`_MAX_EXPANSIONS` a word is denied whatever it names, as the read and search tools' globs are.

        Its raw components cannot say what bash makes of it."""
        braces = "{a,b}" * 10
        for command in (f"ls {braces}/model/*", f"ls {braces}/notes/*"):
            decision = decide_codex_shell(codex_dir, command)
            assert not decision.allow
            assert decision.reason is not None and _BUDGET_MARK in decision.reason

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf {,}{,}{,}{,}{,}{,}{,}{,}{,}m?del",
            "rm -rf {,}{,}{,}{,}{,}{,}{,}{,}{,}.c?dex",
            "rm -rf " + "./" * 2100 + "m?del",
            "rm -rf " + "./" * 2100 + ".codex",
            "cp x " + "a," * 2100 + "b",
        ],
        ids=["braces-model", "braces-codex", "long-model", "long-codex", "long-harmless"],
    )
    def test_a_part_past_either_glob_limit_is_denied_by_the_budget(self, codex_dir: Path, command: str) -> None:
        """Past :data:`_MAX_EXPANSIONS` or :data:`_MAX_GLOB_LENGTH` a word gets no name check: it is denied."""
        decision = decide_codex_shell(codex_dir, command)
        assert not decision.allow
        assert decision.reason is not None and _BUDGET_MARK in decision.reason

    @pytest.mark.parametrize("windows", [False, True], ids=["posix-words", "windows-words"])
    def test_a_command_past_the_budget_is_stopped_by_the_reading_counter(
        self, codex_dir: Path, monkeypatch, windows: bool
    ) -> None:
        """The budget stops the judgement at whichever bound it crosses first, never by the command's shape.

        Its readings past :data:`_MAX_SHELL_READINGS`, or :data:`_JUDGE_SECONDS` on a slow host; both platforms' words.
        """
        for flag in ("_BACKSLASH_SEPARATES", "_SHELL_WORDS_HAVE_DRIVES", "_DOUBLE_SEPARATOR_IS_UNC"):
            monkeypatch.setattr(copilot_guard, flag, windows)
        command = " ".join(["{a,b}" * 8 + "/x"] * 300)
        decision = decide_codex_shell(codex_dir, command)
        assert not decision.allow
        assert decision.reason is not None and _BUDGET_MARK in decision.reason

        def judged() -> copilot_guard._ShellJudge:
            judge = copilot_guard._ShellJudge(codex_dir, str(codex_dir.resolve() / "model"), ())
            assert judge.command(command) == copilot_guard._OVER_BUDGET
            return judge

        # No deadline to reach, the fixture's: the reading counter alone stops it.
        assert judged().readings > copilot_guard._MAX_SHELL_READINGS
        # The real deadline: the counter that stopped it is named -- the readings, or the deadline passed.
        monkeypatch.setattr(copilot_guard, "_JUDGE_SECONDS", _SHIPPED_JUDGE_SECONDS)
        judge = judged()
        assert judge.readings > copilot_guard._MAX_SHELL_READINGS or time.monotonic() > judge.deadline.end
        # The deadline first, as on a slow host: the budget's verdict below the reading cap.
        # A deadline already past, so a coarse clock (Windows' monotonic ticks in 15.6 ms on Python 3.11)
        # cannot let the counter win the race.
        monkeypatch.setattr(copilot_guard, "_JUDGE_SECONDS", -1.0)
        judge = judged()
        assert judge.readings <= copilot_guard._MAX_SHELL_READINGS and time.monotonic() > judge.deadline.end

    def test_a_text_past_the_length_cap_is_denied(self, codex_dir: Path) -> None:
        decision = decide_codex_shell(codex_dir, "echo " + "x" * copilot_guard._MAX_SHELL_TEXT)
        assert not decision.allow and decision.reason is not None and _BUDGET_MARK in decision.reason

    def test_a_normal_command_is_far_below_the_budget(self, codex_dir: Path) -> None:
        judge = copilot_guard._ShellJudge(codex_dir, str(codex_dir.resolve() / "model"), ())
        command = "cd notes && sed -n '1,80p' readme.md | grep -n 'x' && ls notes/*.md {notes,dist}/x"
        assert judge.command(command) is None
        assert judge.readings < copilot_guard._MAX_SHELL_READINGS // 100

    def test_a_sequence_past_the_expansion_bound_is_denied_by_the_budget(self, codex_dir: Path) -> None:
        """Only the first terms past the bound are made, never the whole sequence: the work is bounded."""
        assert len(copilot_guard._brace_sequence("1..100000000") or []) == copilot_guard._MAX_EXPANSIONS + 1
        decision = decide_codex_shell(codex_dir, "touch f{1..100000000}.txt")
        assert not decision.allow
        assert decision.reason is not None and _BUDGET_MARK in decision.reason

    def test_a_part_with_more_brace_groups_than_the_bound_is_denied_by_the_budget(self, codex_dir: Path) -> None:
        limit = copilot_guard._MAX_BRACE_GROUPS
        assert decide_codex_shell(codex_dir, "ls notes/" + "{a}" * limit).allow
        decision = decide_codex_shell(codex_dir, "ls notes/" + "{a}" * (limit + 1))
        assert not decision.allow and decision.reason is not None and _BUDGET_MARK in decision.reason

    @pytest.mark.parametrize(
        "command",
        [
            "touch " + " ".join(["-a" + "{a}" * 682 + "=" + "{a}" * 682] * 15) + " model/x",
            "touch " + " ".join(["-a" + "{a}" * 1364] * 15) + " model/x",
            "touch " + " ".join(["{a}" * 1365] * 15) + " model/x",
            "touch " + " ".join(["{a,b}" * 8 + "{a}" * 56] * 15) + " notes/x",
            "touch " + " ".join(["{a}" * 64 + "x" * 3800] * 15) + " notes/x",
        ],
        ids=["glued-equals", "glued", "plain", "wide-then-chained", "chained-long"],
    )
    def test_brace_work_is_denied_by_the_budget(self, codex_dir: Path, command: str) -> None:
        """A part holding more than :data:`_MAX_BRACE_GROUPS` brace groups is refused outright.

        Expanding a part's brace groups rescans it once per group, and every scan is charged to the budget (the
        counts: ``test_copilot_guard_scaling.py``)."""
        decision = decide_codex_shell(codex_dir, command)
        assert not decision.allow
        assert decision.reason is not None and _BUDGET_MARK in decision.reason

    @pytest.mark.parametrize(
        "command",
        ["echo ${" + "?" * 60_000, "echo ${" + "?" * 45_000 + "; rm -rf model", "echo " + "${a:-" * 12_000],
        ids=["unclosed-special", "unclosed-then-model", "nested-unclosed"],
    )
    def test_an_adversarial_parameter_word_is_denied(self, codex_dir: Path, command: str) -> None:
        """A ``${`` never closed is no expansion; the text holding one is denied.

        The reading is one linear pass: ``test_copilot_guard_scaling.py``."""
        assert not decide_codex_shell(codex_dir, command).allow

    @pytest.mark.parametrize(
        ("script", "written"),
        [("w" * 60_000, ["w" * 59_999]), ("y/" + "a/" * 20_000, [])],
        ids=["writes", "transliteration"],
    )
    def test_a_long_sed_script_is_read(self, script: str, written: list[str]) -> None:
        """The first ``w`` takes the rest of the script as its file.

        After ``y/a/a/`` the next ``a`` is sed's append command, which takes the rest of the line."""
        assert copilot_guard._sed_reading(["sed", script]) == (written, frozenset({script}))

    def test_matching_against_deep_link_targets_is_charged(self, codex_dir: Path) -> None:
        """Matching deep ``.codex`` link targets is charged: the reading counter, not the deadline, stops it.

        Each target costs the pattern's length times its depth per glob reading; the reason is the budget's.
        """
        deep = codex_dir / "notes" / Path(*(["a"] * 200))
        for index in range(200):
            symlink_or_skip(codex_dir / ".codex" / f"l{index}", deep / f"f{index}")
        command = "touch notes/" + "[a]/" * 199 + "z"
        decision = decide_codex_shell(codex_dir, command)
        assert not decision.allow and decision.reason is not None and _BUDGET_MARK in decision.reason
        protected = copilot_guard._guard_inputs(codex_dir / "swcraft.yaml")
        assert protected is not None
        judge = copilot_guard._ShellJudge(codex_dir, str(codex_dir.resolve() / "model"), protected)
        assert judge.command(command) == copilot_guard._OVER_BUDGET
        assert judge.readings > copilot_guard._MAX_SHELL_READINGS

    def test_a_shallow_glob_is_far_below_the_budget_with_links_in_the_codex_folder(self, codex_dir: Path) -> None:
        for index in range(20):
            symlink_or_skip(codex_dir / ".codex" / f"l{index}", codex_dir / "notes" / f"f{index}")
        assert decide_codex_shell(codex_dir, "ls notes/*.md src/**/*.py; rm -f dist/*.arxml").allow

    def test_a_command_of_unclosed_brackets_is_decided(self, codex_dir: Path) -> None:
        """The bracket words do not hide the model file the command names."""
        words = " ".join("[" * 2000 + f"{index:02d}" + "[:x:]" * 400 for index in range(15))
        assert not decide_codex_shell(codex_dir, f"rm {words} swcraft.yaml").allow

    @pytest.mark.parametrize(
        ("component", "translated"),
        [
            ("[ab]", "[ab]"),
            ("[^a]", "[!a]"),
            ("[[:alpha:]]x", "?x"),
            ("[[[:x:]", "[[][[][:x:]"),
            ("[[:x:]", "[[][:x:]"),
            ("[:x:]", "[:x:]"),
            ("a[", "a[[]"),
            ("[]]", "[]]"),
            ("[[]", "[[]"),
            ("[[:x:][", "[[][:x:][[]"),
        ],
    )
    def test_bracket_expressions_are_translated_as_bash_reads_them(self, component: str, translated: str) -> None:
        assert copilot_guard._bash_brackets(component) == translated


#: Commands naming the guard's own inputs that are no reader: each can write a file or run a program.
_READER_THAT_WRITES = [
    "git diff --output=.codex/hooks.json",
    "git diff --output=swcraft.yaml HEAD~1",
    "git log --output=swcraft.yaml",
    "git log -p --output=.codex/hooks.json",
    "git show HEAD:swcraft.yaml --output=swcraft.yaml",
    "sed -n wswcraft.yaml notes/a",
    "sed s/a/b/w.codex/hooks.json notes/a",
    "sed --in-plac s/a/b/ swcraft.yaml",
    "rg --pre rm x swcraft.yaml",
    "swcraft generate arxml --output .codex/hooks.json",
    "swcraft generate arxml --output-dir .codex",
    "swcraft generate arxml --output=swcraft.yaml",
    "swcraft passport --project swcraft.yaml --output .codex/hooks.json",
    # A build of the guarded manifest with an option that redirects its output, or one its parser does not
    # list: the outputs are not the manifest's.
    "swcraft validate --project swcraft.yaml -o x",
    "swcraft generate arxml --project swcraft.yaml --component Engine --output notes/x.arxml",
    "swcraft generate arxml --project swcraft.yaml --all-components --output-dir notes/c",
    "swcraft generate arxml --project swcraft.yaml --out notes/x.arxml",
    "swcraft generate arxml --project swcraft.yaml --component ../x",
    "swcraft validate --project swcraft.yaml notes/extra",
    "swcraft validate --project swcraft.yaml > swcraft.yaml",
    "X=1 swcraft validate --project swcraft.yaml",
    "env SWCRAFT_MANIFEST=swcraft.yaml swcraft validate --project swcraft.yaml",
    "notes/swcraft validate --project swcraft.yaml",
    "swcraft validate --project swcraft.yaml --project notes/other.yaml",
    "GIT_EXTERNAL_DIFF=rm git diff swcraft.yaml",
    # The narrowed list's cost, accepted: no git, sed or rg over the guard's inputs, no assignment first.
    "git diff swcraft.yaml",
    "git show HEAD:swcraft.yaml",
    "sed -n '1,40p' swcraft.yaml",
    "rg model_root swcraft.yaml",
    "X=1 cat swcraft.yaml",
    "swcraft check-model --project swcraft.yaml -o x",
    "swcraft list --project swcraft.yaml --out x",
    # `resource-report --report` writes under dist_root, and argparse expands every abbreviation of it.
    "swcraft resource-report --project swcraft.yaml --report",
    "swcraft resource-report --project swcraft.yaml --rep",
    "swcraft resource-report --project swcraft.yaml --r",
    "swcraft resource-report --project swcraft.yaml -o x",
    "swcraft resource-report --project swcraft.yaml --out x",
    # `metrics --report` writes under dist_root as well: the same prefix, and a word it catches fails closed.
    "swcraft metrics --project swcraft.yaml --report",
    "swcraft metrics --project swcraft.yaml --rep",
    "swcraft metrics --project swcraft.yaml --since v1 --report",
    "swcraft metrics --project swcraft.yaml --since-head --rep",
    "swcraft metrics --project swcraft.yaml --recent",
    "swcraft metrics --project swcraft.yaml -o x",
    "less -o .codex/log swcraft.yaml",
    "less +!x swcraft.yaml",
    "file -C -m swcraft.yaml",
    "diff swcraft.yaml notes/a",
    # A command or process substitution makes no command a reader, its words included.
    "cat `rm swcraft.yaml`",
    "ls `rm -rf .codex`",
    "ls `mv .codex .x`",
    "grep x `mv swcraft.yaml y`",
    "cat x`rm swcraft.yaml`",
    "cat ${x:-`rm swcraft.yaml`}",
    'cat "`rm swcraft.yaml`"',
    "cat swcraft.yaml `true`",
    # A substitution inside a ``${...}`` word runs as its own command, and its command makes no reader.
    "cat ${x:-$(rm swcraft.yaml)}",
    "cat ${x:-`rm -rf .codex`}",
    'cat "${x:-$(rm swcraft.yaml)}"',
    "cat swcraft.yaml ${x:-$(true)}",
    "cat ${x:-<(rm swcraft.yaml)}",
    # A backtick's command is judged as a command: its ``sed`` writes the manifest.
    "cat `sed -n wswcraft.yaml notes.md`",
    "cat ${x:-`sed -n wswcraft.yaml notes.md`}",
    # A wrapper runs the command it names: its reader status and its ``sed`` scan are the wrapped command's.
    "env LESSOPEN='|rm x' less swcraft.yaml",
    "env -S 'cat' swcraft.yaml",
    "command sed -n '1,40p' swcraft.yaml",
    "xargs cat < swcraft.yaml",
    # A text that defines a function or changes what a name runs makes no command a reader.
    'cat() { rm "$@"; }; cat swcraft.yaml',
    'cat () { rm "$@"; }; cat swcraft.yaml',
    'cat ( ) { rm "$@"; }; cat swcraft.yaml',
    'function cat { rm "$@"; }; cat swcraft.yaml',
    'function cat() { rm "$@"; }; cat swcraft.yaml',
    "alias cat=rm; cat swcraft.yaml",
    "builtin alias cat=rm; cat swcraft.yaml",
    "unalias cat; cat swcraft.yaml",
    "enable -n cat; cat swcraft.yaml",
    "hash -p /bin/rm cat; cat swcraft.yaml",
    "source notes/env.sh; cat swcraft.yaml",
    ". notes/env.sh; cat swcraft.yaml",
    "eval true; cat swcraft.yaml",
    'cat() { rm "$@"; }; echo `cat swcraft.yaml`',
    'cat() { rm "$@"; }; export -f cat; bash -c "cat swcraft.yaml"',
    "bash -c 'cat() { rm \"$@\"; }; cat swcraft.yaml'",
]


class TestCodexGuardInputs:
    """The guard's own inputs, ``swcraft.yaml`` and ``.codex``: no patch writes them, only a reader names them."""

    @pytest.mark.parametrize(
        "path", ["swcraft.yaml", ".codex/hooks.json", ".codex/config.toml", "notes/../swcraft.yaml"]
    )
    def test_a_patch_of_the_guards_own_inputs_is_denied(self, codex_dir: Path, path: str) -> None:
        """The agent never edits what the guard reads."""
        decision = decide_codex_tool("apply_patch", codex_dir, command=codex_patch(f"*** Update File: {path}"))
        assert decision == copilot_guard.Decision(allow=False, reason=GUARD_INPUT_REASON.format(kind="patch"))

    @pytest.mark.parametrize(
        "command",
        [
            "echo x > swcraft.yaml",
            "cat notes.md >> swcraft.yaml",
            "sed -i s/a/b/ swcraft.yaml",
            "sed -ni p swcraft.yaml",
            "tee swcraft.yaml",
            "cp notes.md swcraft.yaml",
            "rm -rf .codex",
            "mv .codex/hooks.json notes/",
            "cat notes.md > .codex/hooks.json",
            "bash -lc 'rm swcraft.yaml'",
            "git checkout swcraft.yaml",
            "swcraft init swcraft.yaml",
            "rm swcraft.y*",
        ],
    )
    def test_a_command_writing_the_guards_own_inputs_is_denied(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )

    @pytest.mark.parametrize(
        "command",
        [
            "cat swcraft.yaml",
            "cat .codex/hooks.json",
            "ls .codex",
            "head -5 swcraft.yaml > notes/head.txt",
            "swcraft check-model --project swcraft.yaml",
            "cat swcraft.yaml | grep model_root",
            "grep -n -e model_root -A2 swcraft.yaml",
            "tail -n 3 swcraft.yaml",
            "wc -l swcraft.yaml",
            "stat swcraft.yaml",
            "file swcraft.yaml",
            "less swcraft.yaml",
            "more swcraft.yaml",
            "ls -la .codex",
            "swcraft query --project swcraft.yaml --format json",
            "swcraft resource-report --project swcraft.yaml --format json",
            "swcraft metrics --project swcraft.yaml",
            "swcraft metrics --project swcraft.yaml --format json",
            "swcraft metrics --project swcraft.yaml --since v1 --since HEAD~2",
            "swcraft metrics --project swcraft.yaml --since-head",
            "swcraft query --request notes/r.json --project swcraft.yaml",
            "cat ${x:-notes.md} swcraft.yaml",
            "command cat swcraft.yaml",
            "command -p cat swcraft.yaml",
            "env cat swcraft.yaml",
            "env -i cat swcraft.yaml",
            "nice -n 5 head swcraft.yaml",
            "nohup cat swcraft.yaml",
            "timeout -s KILL 5 cat swcraft.yaml",
            "stdbuf -oL grep model_root swcraft.yaml",
            "exec -a x cat swcraft.yaml",
            "env command cat swcraft.yaml",
        ],
    )
    def test_a_read_of_the_guards_own_inputs_is_allowed(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command).allow

    @pytest.mark.parametrize("command", _READER_THAT_WRITES)
    def test_a_command_off_the_narrowed_reader_list_naming_the_guards_inputs_is_denied(
        self, codex_dir: Path, command: str
    ) -> None:
        """Only a command that can neither write nor run anything is a reader."""
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )

    @pytest.mark.parametrize(
        ("name", "command"),
        [
            (".codex/hooks.json", "echo x > .codex/hooks.json"),
            (".codex/hooks.json", "cp notes.md .codex/hooks.json"),
            (".codex/hooks.json", "sed -n w.codex/hooks.json notes.md"),
            ("swcraft.yaml", "echo x > swcraft.yaml"),
            ("swcraft.yaml", "tee notes/../swcraft.yaml"),
        ],
    )
    def test_a_guard_input_that_is_a_link_out_of_the_project_is_judged_as_named(
        self, codex_dir: Path, tmp_path: Path, name: str, command: str
    ) -> None:
        """Writing through a link writes its target, so the word is judged both ways.

        The word resolves outside ``.codex`` and is still the hook's file as named."""
        outside = tmp_path / "outside" / Path(name).name
        outside.parent.mkdir()
        (codex_dir / name).replace(outside)
        symlink_or_skip(codex_dir / name, outside)
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )
        decision = decide_codex_tool("apply_patch", codex_dir, command=codex_patch(f"*** Update File: {name}"))
        assert decision == copilot_guard.Decision(allow=False, reason=GUARD_INPUT_REASON.format(kind="patch"))
        assert decide_codex_shell(codex_dir, f"cat {name}").allow

    @pytest.mark.parametrize(
        "command",
        ["echo x > hook-input.json", "cp notes.md hook-input.json", "rm hook-input.json", "sed -i s/a/b/ hook-*"],
    )
    def test_the_target_of_a_link_in_the_codex_folder_is_a_guard_input(self, codex_dir: Path, command: str) -> None:
        """``.codex/hooks.json`` a link to ``../hook-input.json`` makes that file the hook Codex runs.

        So writing it is writing the hook, whatever name the command uses."""
        hook = codex_dir / ".codex" / "hooks.json"
        hook.replace(codex_dir / "hook-input.json")
        symlink_or_skip(hook, Path("..") / "hook-input.json")
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )
        decision = decide_codex_tool("apply_patch", codex_dir, command=codex_patch("*** Update File: hook-input.json"))
        assert decision == copilot_guard.Decision(allow=False, reason=GUARD_INPUT_REASON.format(kind="patch"))
        assert decide_codex_shell(codex_dir, "cat hook-input.json").allow

    def test_a_codex_folder_past_the_entry_cap_denies_every_call(self, codex_dir: Path) -> None:
        """Past :data:`_MAX_GUARD_FOLDER_ENTRIES` entries every patch and shell command is denied.

        The links in ``.codex`` are not all read, so what the hook depends on cannot be said."""
        for index in range(copilot_guard._MAX_GUARD_FOLDER_ENTRIES + 1):
            (codex_dir / ".codex" / f"f{index}").touch()
        for decision in (
            decide_codex_shell(codex_dir, "ls notes"),
            decide_codex_tool("apply_patch", codex_dir, command=codex_patch("*** Add File: notes/x")),
        ):
            assert not decision.allow and decision.reason is not None and ".codex" in decision.reason

    def test_copilot_still_allows_the_guards_inputs_to_be_patched(self, codex_dir: Path) -> None:
        payload = guard_payload("apply_patch", codex_dir, command=codex_patch("*** Update File: swcraft.yaml"))
        assert decide_payload(payload, codex_dir).allow


def _codex_manifest(root: Path, **changes: Any) -> None:
    """Rewrite *root*'s manifest with *changes* over :func:`codex_project`'s fields (``outputs`` merged)."""
    data: dict[str, Any] = {
        "name": "p",
        "schema_version": "1.0",
        "model_root": "model",
        "dist_root": "dist",
        "views_root": "views",
        "outputs": {"arxml": "dist/model.arxml", "needs": "dist/needs.json"},
    }
    outputs = changes.pop("outputs", {})
    data.update(changes)
    data["outputs"].update(outputs)
    _write_manifest(root, data)


#: The builds of the guarded manifest, each in a spelling the CLI reads it by: allowed while every output the
#: manifest names lies outside the model, ``swcraft.yaml`` and ``.codex/``: the sole authoritative gate must be
#: runnable by the agent.
_BUILDS = [
    "swcraft validate --project swcraft.yaml",
    "swcraft validate --project=swcraft.yaml",
    "swcraft validate --project ./swcraft.yaml",
    "swcraft validate --project swcraft.yaml --warnings-as-errors",
    "SWCRAFT_MANIFEST=swcraft.yaml swcraft validate --project swcraft.yaml",
    "swcraft validate --project swcraft.yaml 2>&1 | tail -n 5",
    "swcraft validate --project swcraft.yaml > notes/validate.log",
    "command swcraft validate --project swcraft.yaml",
    "swcraft generate arxml --project swcraft.yaml",
    "swcraft generate arxml --project swcraft.yaml --arxml-version 4.3.0",
    "swcraft generate arxml --project swcraft.yaml --component Engine",
    "swcraft generate diagrams --project swcraft.yaml",
    "swcraft generate needs --project=swcraft.yaml",
]


class TestCodexBuildsOfTheGuardedManifest:
    """A build naming the guarded manifest runs exactly when its outputs keep off the guard's inputs and the model.

    ``swcraft validate`` and ``swcraft generate``: the agent cannot arrange the outputs.
    """

    @pytest.mark.parametrize("command", _BUILDS)
    def test_a_build_with_clean_outputs_is_allowed(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command).allow

    def test_a_build_from_a_subfolder_naming_the_manifest_relatively_is_allowed(self, codex_dir: Path) -> None:
        assert decide_codex_shell(codex_dir / "notes", "swcraft validate --project ../swcraft.yaml").allow

    @pytest.mark.parametrize(
        ("changes", "label"),
        [
            ({"outputs": {"arxml": ".codex/hooks.json"}}, "`outputs.arxml`"),
            ({"outputs": {"arxml": "swcraft.yaml"}}, "`outputs.arxml`"),
            ({"outputs": {"needs": ".codex/hooks.json"}}, "`outputs.needs`"),
            ({"dist_root": ".codex/dist"}, "`dist_root`"),
            ({"views_root": ".codex"}, "`views_root`"),
        ],
        ids=["arxml-hook", "arxml-manifest", "needs-hook", "dist-codex", "views-codex"],
    )
    @pytest.mark.parametrize(
        "command",
        [
            "swcraft validate --project swcraft.yaml",
            "swcraft generate arxml --project swcraft.yaml",
            "swcraft generate diagrams --project swcraft.yaml",
            "swcraft generate needs --project swcraft.yaml",
        ],
    )
    def test_a_build_whose_output_is_a_guard_input_is_denied_naming_the_output(
        self, codex_dir: Path, changes: dict[str, Any], label: str, command: str
    ) -> None:
        _codex_manifest(codex_dir, **changes)
        decision = decide_codex_shell(codex_dir, command)
        assert not decision.allow
        assert decision.reason is not None and label in decision.reason and "guard's own inputs" in decision.reason

    def test_a_build_whose_previous_arxml_is_a_guard_input_is_denied(self, codex_dir: Path) -> None:
        """The move-aside writes ``<stem>_old<suffix>`` beside the ARXML, so a build can replace a guard input.

        With ``.codex/hooks.json`` a link to ``../dist/model_old.arxml`` that file is the hook, and the build would
        replace it."""
        hook = codex_dir / ".codex" / "hooks.json"
        (codex_dir / "dist").mkdir()
        hook.replace(codex_dir / "dist" / "model_old.arxml")
        symlink_or_skip(hook, Path("..") / "dist" / "model_old.arxml")
        decision = decide_codex_shell(codex_dir, "swcraft validate --project swcraft.yaml")
        assert not decision.allow
        assert decision.reason is not None and "_old" in decision.reason

    def test_the_previous_arxml_name_is_the_writers(self) -> None:
        """The guard spells the move-aside's name itself (the hook imports no writer); the two agree."""
        from swcraft_tools.writer.arxml_xml import previous_arxml_path

        for name in ("dist/model.arxml", "out/a.b.arxml", "x"):
            assert copilot_guard._previous_arxml(name) == str(previous_arxml_path(name))

    def test_a_build_whose_output_is_in_the_model_is_denied_as_an_unreadable_manifest(self, codex_dir: Path) -> None:
        """The loader refuses an output inside the model, so the manifest cannot be read and the build is denied."""
        _codex_manifest(codex_dir, outputs={"arxml": "model/x.arxml"})
        decision = decide_codex_shell(codex_dir, "swcraft validate --project swcraft.yaml")
        assert not decision.allow
        assert decision.reason is not None and "cannot be read" in decision.reason

    def test_a_generate_needs_whose_output_is_in_the_model_is_denied_as_an_unreadable_manifest(
        self, codex_dir: Path
    ) -> None:
        """``outputs.needs`` keeps the loader's overlap rule: over the model, the manifest cannot be read."""
        _codex_manifest(codex_dir, outputs={"needs": "model/needs.json"})
        decision = decide_codex_shell(codex_dir, "swcraft generate needs --project swcraft.yaml")
        assert not decision.allow
        assert decision.reason is not None and "cannot be read" in decision.reason

    def test_the_retired_generate_rst_is_no_build(self, codex_dir: Path) -> None:
        """``rst`` is no choice of ``generate`` any more, so the command naming the manifest is no reader."""
        assert decide_codex_shell(codex_dir, "swcraft generate rst --project swcraft.yaml") == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )

    def test_a_build_of_an_unreadable_manifest_is_denied(self, codex_dir: Path) -> None:
        write_unreadable_manifest(codex_dir)
        decision = decide_codex_shell(codex_dir, "swcraft validate --project swcraft.yaml")
        assert not decision.allow
        assert decision.reason is not None and "cannot be read" in decision.reason

    @pytest.mark.parametrize(
        "command",
        [
            "swcraft validate --project swcraft.yaml --output notes/x",
            "swcraft generate arxml --project swcraft.yaml --component Engine --output notes/x.arxml",
            "swcraft generate arxml --project swcraft.yaml --all-components --output-dir notes/c",
        ],
    )
    def test_a_build_with_an_output_option_is_denied(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )

    def test_a_build_in_a_text_that_renames_commands_is_denied(self, codex_dir: Path) -> None:
        command = 'swcraft() { rm "$@"; }; swcraft validate --project swcraft.yaml'
        assert not decide_codex_shell(codex_dir, command).allow


#: A link count past every platform's link limit: macOS follows 32 links, Linux 40, Windows 63 reparse points.
_PAST_EVERY_LINK_LIMIT = 80


def _link_chain(directory: Path, links: int, depth: int) -> str:
    """*links* symbolic links in *directory*, each to the next through a target *depth* components down and back up.

    A name whose resolution follows every link and stats every component. The chain's head."""
    down = "/".join(["a"] * depth)
    up = "/".join([".."] * depth)
    (directory / down).mkdir(parents=True)
    (directory / f"l{links}").mkdir()
    for index in range(links - 1, -1, -1):
        symlink_or_skip(directory / f"l{index}", Path(f"{down}/{up}/l{index + 1}"))
    return "l0"


class TestCodexDeadline:
    """One monotonic deadline bounds a judgement whatever a name's links make one resolution cost."""

    def test_a_test_runs_with_no_deadline_it_can_reach(self, codex_dir: Path) -> None:
        """The module's fixture is in force, and a judgement's deadline is taken from it."""
        judge = copilot_guard._ShellJudge(codex_dir, str(codex_dir.resolve() / "model"), ())
        assert judge.deadline.end > time.monotonic() + _SHIPPED_JUDGE_SECONDS

    @pytest.mark.parametrize("posix_words", [True, False], ids=["posix-words", "windows-words"])
    def test_no_path_is_resolved_once_the_deadline_has_passed(self, codex_dir: Path, monkeypatch, posix_words) -> None:
        """The deadline is read before every resolution, so a judgement stops at the first one past it.

        It stops with the budget's reason. A brace word's expansions are resolved one after another after one charge
        to the reading budget, and a patch's headers with no charge between them, so only that read can stop them."""
        monkeypatch.setattr(copilot_guard, "_SHELL_WORDS_HAVE_DRIVES", not posix_words)
        clocks = resolution_clocks(monkeypatch)
        command = "cat notes/{" + ",".join(f"x{index}" for index in range(50)) + "}"
        decision = decide_codex_shell(codex_dir, command)
        assert clocks[-1].ticks == TICKS_TO_THE_DEADLINE
        assert not decision.allow and decision.reason is not None and _BUDGET_MARK in decision.reason
        judge = copilot_guard._ShellJudge(codex_dir, str(codex_dir.resolve() / "model"), ())
        assert judge.command(command) == copilot_guard._OVER_BUDGET and clocks[-1].ticks == TICKS_TO_THE_DEADLINE
        assert judge.readings < copilot_guard._MAX_SHELL_READINGS  # the deadline stopped it, not the readings
        decision = decide_codex_tool(
            "apply_patch", codex_dir, command=codex_patch(*[f"*** Add File: notes/x{n}" for n in range(200)])
        )
        assert clocks[-1].ticks == TICKS_TO_THE_DEADLINE
        assert not decision.allow and decision.reason is not None and "in time" in decision.reason

    def test_a_glob_over_links_past_the_deadline_is_denied_by_the_budget(self, codex_dir: Path, monkeypatch) -> None:
        chain = codex_dir / "notes" / "c"
        chain.mkdir()
        head = _link_chain(chain, 20, 60)
        links = codex_dir / "notes" / "links"
        links.mkdir()
        for index in range(100):
            symlink_or_skip(links / f"x{index}", Path("..") / "c" / head)
        monkeypatch.setattr(copilot_guard, "_JUDGE_SECONDS", 0.01)
        decision = decide_codex_shell(codex_dir, "ls notes/links/*/q")
        assert not decision.allow and decision.reason is not None and _BUDGET_MARK in decision.reason

    def test_a_chain_past_the_kernels_link_limit_is_denied_unjudged(self, codex_dir: Path) -> None:
        """No system call follows more links than the kernel's limit, so such a chain opens nothing and is denied.

        Resolving would cost an uncharged hop per link; the chain exceeds every limit (50 links are within Windows' 63).
        """
        chain = codex_dir / "notes" / "c"
        chain.mkdir()
        head = _link_chain(chain, _PAST_EVERY_LINK_LIMIT, 1)
        for decision in (
            decide_codex_shell(codex_dir, f"cat notes/c/{head}/x"),
            decide_codex_tool("apply_patch", codex_dir, command=codex_patch(f"*** Add File: notes/c/{head}/x")),
        ):
            assert not decision.allow
            assert decision.reason is not None and "cannot be" in decision.reason

    def test_ordinary_commands_are_allowed_and_the_shipped_deadline_fits_the_hook(self, codex_dir: Path) -> None:
        assert _SHIPPED_JUDGE_SECONDS < 10 / 2  # the template hook's timeout, with room to start a process
        for command in ("ls -la", "cat notes.md", "grep -rn x notes", "sed -n '1,80p' notes.md | head"):
            assert decide_codex_shell(codex_dir, command).allow


class TestCodexOptionClusters:
    """A short-option cluster can end in an option whose value is glued to it: every suffix is a word."""

    @pytest.mark.parametrize(
        "command",
        [
            "sort -uomodel/swc/Engine.yaml notes.md",
            "sort -nomodel/swc/Engine.yaml notes.md",
            "tar -cvfmodel/swc/Engine.yaml notes",
            "cp -rtmodel/swc notes.md",
            "tar -xzvCmodel -f a.tgz",
        ],
    )
    def test_a_cluster_ending_in_a_model_path_is_denied(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(allow=False, reason=SHELL_REASON)

    @pytest.mark.parametrize("command", ["sort -uoswcraft.yaml notes.md", "cp -rt.codex notes.md"])
    def test_a_cluster_ending_in_a_guard_input_is_denied(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )

    @pytest.mark.parametrize("command", ["ls -la", "grep -rn x src", "tar -czf out.tgz notes", "git log -p"])
    def test_ordinary_clusters_are_allowed(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command).allow

    def test_a_long_cluster_is_denied_by_the_budget(self, codex_dir: Path) -> None:
        """Every suffix is charged before it is made (the count: ``test_copilot_guard_scaling.py``)."""
        decision = decide_codex_shell(codex_dir, "sort -" + "u" * 60_000)
        assert not decision.allow and decision.reason is not None and _BUDGET_MARK in decision.reason


class TestCodexReaderNames:
    """A reader is the bare command name, and nothing in a text holding ``xargs`` is one."""

    @pytest.mark.parametrize(
        "command",
        [
            "notes/cat swcraft.yaml",
            "./cat swcraft.yaml",
            "./notes/ls .codex",
            "notes/swcraft list --project swcraft.yaml",
            "notes/env cat swcraft.yaml",
            "env notes/cat swcraft.yaml",
            "ls swcraft.yaml | xargs rm",
            "ls -d .codex | xargs rm -rf",
            "grep -l . swcraft.yaml | xargs -I{} cp notes.md {}",
            "ls .codex/hooks.json | $'x\\x61rgs' truncate -s0",
        ],
    )
    def test_a_path_named_reader_or_xargs_is_no_reader(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command) == copilot_guard.Decision(
            allow=False, reason=GUARD_INPUT_REASON.format(kind="command")
        )

    @pytest.mark.parametrize("command", ["cat swcraft.yaml", "ls .codex", "ls notes | xargs wc -l"])
    def test_a_bare_reader_still_reads(self, codex_dir: Path, command: str) -> None:
        assert decide_codex_shell(codex_dir, command).allow


class TestCodexProject:
    """Which project the Codex dialect guards, and what an unreadable manifest still allows."""

    def test_a_session_in_a_subdirectory_is_guarded(self, codex_dir: Path) -> None:
        """The nearest directory holding both ``swcraft.yaml`` and ``.codex/hooks.json``."""
        below = codex_dir / "notes"
        assert not decide_codex_shell(below, "cat ../model/swc/Engine.yaml").allow
        decision = decide_codex_tool(
            "apply_patch", below, command=codex_patch("*** Update File: ../model/swc/Engine.yaml")
        )
        assert decision == copilot_guard.Decision(
            allow=False, reason=_PATCH_REASON.format(path="model/swc/Engine.yaml")
        )
        assert decide_codex_shell(below, "cat readme.md").allow

    def test_a_project_without_the_codex_hook_file_is_not_guarded(self, project_dir: Path) -> None:
        (project_dir / "notes").mkdir()
        assert decide_codex_shell(project_dir / "notes", "cat ../model/swc/Engine.yaml").allow
        assert decide_codex_shell(project_dir, "cat model/swc/Engine.yaml").allow

    def test_a_nested_manifest_without_the_hook_file_is_passed_over(self, codex_dir: Path) -> None:
        """An enclosing project is never unguarded by a nested one that has no Codex hook."""
        nested = codex_dir / "notes" / "inner"
        _write_manifest(nested)
        assert not decide_codex_shell(nested, "cat ../../model/swc/Engine.yaml").allow

    def test_copilot_never_walks_upward(self, codex_dir: Path) -> None:
        guarded = copilot_guard.guarded_project(codex_dir / "notes", "copilot")
        assert guarded == copilot_guard.GuardedProject(present=False)

    def test_a_payload_outside_a_guarded_project_is_allowed(self, tmp_path: Path) -> None:
        (tmp_path / "model").mkdir()
        assert decide_codex_shell(tmp_path, "cat model/x").allow
        assert decide_codex_tool("apply_patch", tmp_path, command="not a patch").allow

    @pytest.mark.parametrize("tool", ["mcp__swcraft__change_set", "WebSearch", "update_plan"])
    def test_every_other_tool_is_allowed(self, codex_dir: Path, tool: str) -> None:
        assert decide_codex_tool(tool, codex_dir, command="cat model/x", path="model/x").allow

    def test_a_read_tool_is_still_judged(self, codex_dir: Path) -> None:
        assert not decide_codex_tool("Read", codex_dir, path="model/swc/Engine.yaml").allow

    @pytest.mark.parametrize("kind", list(UNREADABLE_MANIFESTS))
    @pytest.mark.parametrize(
        ("cwd", "tool", "command"),
        [
            (".", "shell", "ls notes"),
            (".", "shell", "cat swcraft.yaml"),
            (".", "shell", "cat ./swcraft.yaml"),
            ("notes", "shell", "cat ../swcraft.yaml"),
            (".", "shell", "head swcraft.yaml | tail swcraft.yaml"),
            (".", "shell", "ls"),
            (".", "shell", "rm swcraft.yaml"),
            (".", "patch", "*** Update File: swcraft.yaml"),
            ("notes", "patch", "*** Update File: ../swcraft.yaml"),
            (".", "patch", "*** Update File: notes.md"),
        ],
    )
    def test_an_unreadable_manifest_denies_every_patch_and_command(
        self, codex_dir: Path, kind: str, cwd: str, tool: str, command: str
    ) -> None:
        """With ``swcraft.yaml`` unreadable, Codex never reads or patches it; the reason says to fix it by hand."""
        write_unreadable_manifest(codex_dir, UNREADABLE_MANIFESTS[kind])
        assert copilot_guard.read_project(codex_dir).project is None
        if tool == "patch":
            decision = decide_codex_tool("apply_patch", codex_dir / cwd, command=codex_patch(command))
        else:
            decision = decide_codex_shell(codex_dir / cwd, command)
        assert not decision.allow
        assert decision.reason is not None
        assert decision.reason.startswith("SWCraft: this project's swcraft.yaml cannot be read (")
        assert decision.reason.endswith(BY_HAND)
        assert str(codex_dir) not in decision.reason

    @pytest.mark.parametrize("model_root", [".", "../shared"], ids=["the-project-directory", "outside"])
    def test_a_model_root_the_loader_refuses_fails_closed(self, tmp_path: Path, model_root: str) -> None:
        """A model root at the project directory or outside it is refused by the manifest loader.

        So the Codex dialect denies as for any unreadable manifest; ``_model_name``'s ``None`` is defensive past
        that."""
        root = codex_project(tmp_path / "proj", model_root)
        decision = decide_codex_shell(root, "ls notes")
        assert not decision.allow
        assert decision.reason is not None and decision.reason.startswith("SWCraft: this project's swcraft.yaml cannot")

    @pytest.mark.parametrize("kind", ["directory", "broken-symlink"])
    def test_a_hook_file_that_is_no_readable_file_still_marks_the_project(self, project_dir: Path, kind: str) -> None:
        """The presence rule: any entry at ``.codex/hooks.json`` is present, so the project stays guarded."""
        (project_dir / ".codex").mkdir()
        hook = project_dir / ".codex" / "hooks.json"
        if kind == "directory":
            hook.mkdir()
        else:
            symlink_or_skip(hook, project_dir / "nowhere.json")
        assert not decide_codex_shell(project_dir, "cat model/swc/Engine.yaml").allow

    def test_a_symlink_into_the_model_is_judged_as_what_it_resolves_to(self, codex_dir: Path) -> None:
        link = codex_dir / "notes" / "alias"
        symlink_or_skip(link, codex_dir / "model" / "swc")
        assert not decide_codex_shell(codex_dir, "cat notes/alias/Engine.yaml").allow
        assert not decide_codex_tool(
            "apply_patch", codex_dir, command=codex_patch("*** Update File: notes/alias/Engine.yaml")
        ).allow

    @pytest.mark.parametrize("workdir", [42, "", ["model"], {"x": 1}])
    def test_a_workdir_that_is_no_non_empty_string_is_denied(self, codex_dir: Path, workdir: object) -> None:
        decision = decide_codex_shell(codex_dir, "ls notes", workdir=workdir)
        assert not decision.allow
        assert decision.reason is not None and UNJUDGED_MARK in decision.reason

    def test_undecodable_arguments_are_denied(self, codex_dir: Path) -> None:
        payload = {"toolName": "Bash", "toolArgs": "not json", "cwd": str(codex_dir)}
        guarded = copilot_guard.guarded_project(codex_dir, "codex")
        decision = copilot_guard.decide(payload, codex_dir, guarded, "codex")
        assert not decision.allow
        assert decision.reason is not None and UNJUDGED_MARK in decision.reason

    def test_every_reason_is_project_relative(self, codex_dir: Path) -> None:
        for decision in (
            decide_codex_shell(codex_dir, f"cat {codex_dir.resolve() / 'model' / 'x'}"),
            decide_codex_tool(
                "apply_patch", codex_dir, command=codex_patch(f"*** Add File: {codex_dir.resolve() / 'model' / 'x'}")
            ),
            decide_codex_tool("apply_patch", codex_dir, command="nonsense"),
            decide_codex_shell(codex_dir, None),
        ):
            assert not decision.allow and decision.reason is not None
            assert str(codex_dir) not in decision.reason
            assert str(codex_dir.resolve()) not in decision.reason


class TestCodexCommand:
    """The Codex dialect through ``swcraft guard --host codex``."""

    def test_the_command_denies_a_patch_in_codex_shape(self, codex_dir: Path, monkeypatch, capsys) -> None:
        stdin = json.dumps(
            guard_payload("apply_patch", codex_dir, command=codex_patch("*** Add File: model/swc/N.yaml"))
        )
        out, _ = run_guard(
            stdin.encode("utf-8"), monkeypatch, capsys, codex_dir, entry.main, ("guard", "--host", "codex")
        )
        assert json.loads(out) == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": _PATCH_REASON.format(path="model/swc/N.yaml"),
            }
        }

    def test_the_command_finds_the_project_above_the_session(self, codex_dir: Path, monkeypatch, capsys) -> None:
        stdin = json.dumps(guard_payload("Bash", codex_dir / "notes", command="cat ../model/swc/Engine.yaml"))
        out, _ = run_guard(
            stdin.encode("utf-8"), monkeypatch, capsys, codex_dir, entry.main, ("guard", "--host", "codex")
        )
        assert json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] == SHELL_REASON

    @pytest.mark.parametrize(("tool", "allowed"), [("Bash", False), ("apply_patch", False), ("update_plan", True)])
    def test_an_unexpected_failure_denies_shell_and_patch_calls(
        self, codex_dir: Path, monkeypatch, capsys, tool: str, allowed: bool
    ) -> None:
        def fail(*_: object) -> copilot_guard.Decision:
            raise RuntimeError("broken")

        monkeypatch.setattr(copilot_guard, "decide", fail)
        stdin = json.dumps(guard_payload(tool, codex_dir / "notes", command="ls")).encode("utf-8")
        out, err = run_guard(stdin, monkeypatch, capsys, codex_dir, entry.main, ("guard", "--host", "codex"))
        if allowed:
            assert out.strip() == "{}"
        else:
            assert json.loads(out) == CODEX_UNDECIDED_DENY
        assert ("this call is allowed" if allowed else "this call is denied") in err

    def test_the_nul_probes_are_denied_through_the_command(self, codex_dir: Path, monkeypatch, capsys) -> None:
        stdin = json.dumps(guard_payload("Bash", codex_dir, command="cat $'model\\0'/swc/Engine.yaml"))
        out, _ = run_guard(
            stdin.encode("utf-8"), monkeypatch, capsys, codex_dir, entry.main, ("guard", "--host", "codex")
        )
        assert json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] == SHELL_REASON
        stdin = json.dumps(guard_payload("Bash", codex_dir, command="rm 'notes\x00'"))
        out, _ = run_guard(
            stdin.encode("utf-8"), monkeypatch, capsys, codex_dir, entry.main, ("guard", "--host", "codex")
        )
        assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


# The Claude Code dialect: the decision table.
