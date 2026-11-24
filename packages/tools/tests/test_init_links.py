"""``swcraft init`` never writes through a link.

The template carries files a client reads as configuration -- ``.claude/settings.json``,
``.mcp.json``, ``.codex/config.toml``, ``CLAUDE.md`` -- and a project folder can hold a link where
one of them goes: a ``.claude`` folder linked to the user's own settings, a dangling ``CLAUDE.md``
left by another tool. ``init``'s writer, ``cli.init_command._write_if_absent``, refuses a
destination that is a link (a dangling one included) or that lies below a link inside the project,
prints a ``Skipped`` line, and writes exclusively, so a link put in place between the check and the
write fails the write.

Every case makes a real link, guarded by the capability probe ``symlink_or_skip`` (a host that
cannot make one skips, never a platform check -- so on a Windows runner without the symbolic-link
privilege every case here skips and proves nothing there). What this does not reach: a Windows directory
junction, which the writer does not detect (its docstring says so), and the layer directories
``init`` creates, which are directories, not template files.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO, Any

import pytest
from swcraft_tools import exclusive_create
from swcraft_tools.cli import init_command, main
from swcraft_tools.cli.init_command import _template_file_plan
from symlink_support import symlink_or_skip

#: ``--integration moon`` with every client of the table chosen, so every template file is in the
#: plan -- the default (Copilot alone) writes none of the Codex and Claude Code files these cases
#: link.
EVERY_CLIENT = ("--integration", "moon", "--client", "copilot", "--client", "codex", "--client", "claude")


def _init(monkeypatch: pytest.MonkeyPatch, target: Path, *extra: str) -> None:
    monkeypatch.setattr("sys.argv", ["swcraft", "init", str(target), *extra])
    main()


def test_a_destination_that_links_to_an_outside_file_is_not_written_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("the user's own\n", encoding="utf-8", newline="\n")
    target = tmp_path / "project"
    target.mkdir()
    symlink_or_skip(target / "CLAUDE.md", outside)

    _init(monkeypatch, target, *EVERY_CLIENT)

    assert outside.read_text(encoding="utf-8") == "the user's own\n"
    assert (target / "CLAUDE.md").is_symlink()
    assert f"Skipped {target / 'CLAUDE.md'} (it is a link; init never writes through one)" in capsys.readouterr().out


def test_a_dangling_link_at_the_destination_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "elsewhere" / "absent.json"
    missing.parent.mkdir()
    target = tmp_path / "project"
    target.mkdir()
    symlink_or_skip(target / ".mcp.json", missing)

    _init(monkeypatch, target, *EVERY_CLIENT)

    assert not missing.exists(), "the write followed a dangling link"
    assert (target / ".mcp.json").is_symlink()
    assert f"Skipped {target / '.mcp.json'} (it is a link" in capsys.readouterr().out


def test_a_linked_folder_inside_the_project_is_not_written_into(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    users_own = tmp_path / "home-claude"
    users_own.mkdir()
    target = tmp_path / "project"
    target.mkdir()
    symlink_or_skip(target / ".claude", users_own)

    _init(monkeypatch, target, *EVERY_CLIENT)

    assert list(users_own.iterdir()) == [], "init wrote into the folder the project's .claude links to"
    out = capsys.readouterr().out
    assert f"Skipped {target / '.claude' / 'settings.json'} (it lies below the link {target / '.claude'}" in out


def test_a_dangling_linked_folder_inside_the_project_is_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nowhere"
    target = tmp_path / "project"
    target.mkdir()
    symlink_or_skip(target / ".codex", missing)

    _init(monkeypatch, target, *EVERY_CLIENT)

    assert not missing.exists(), "init created the folder a dangling link names"


def test_no_template_file_is_written_through_a_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every file of the template, each destination a dangling link into one outside folder."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "project"
    plan = _template_file_plan("moon", "project")
    assert len(plan) >= 20, "the template plan reads as near-empty; this case would assert nothing"
    for index, relative in enumerate(sorted(plan)):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        symlink_or_skip(destination, outside / f"target-{index}")

    _init(monkeypatch, target, *EVERY_CLIENT)

    assert list(outside.iterdir()) == [], "a template file was written through a link"
    for relative in plan:
        assert (target / relative).is_symlink(), relative


def test_a_link_put_in_place_after_the_check_fails_the_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The write is exclusive: a dangling link that slipped past the check is not followed.

    The race is staged by taking the link check out (as if the link appeared just after it). The
    write goes through ``exclusive_create.open_new`` because Windows' ``CreateFile`` with
    ``CREATE_NEW`` alone follows a dangling link and creates its target; the helper's
    ``FILE_FLAG_OPEN_REPARSE_POINT`` makes the link the existing entry. A host that cannot make a
    link skips (``symlink_or_skip``); it does not run the case.
    """
    missing = tmp_path / "elsewhere.json"
    target = tmp_path / "project"
    target.mkdir()
    symlink_or_skip(target / "settings.json", missing)
    monkeypatch.setattr(init_command, "_linked_component", lambda root, relative: None)

    with pytest.raises(FileExistsError) as refused:
        init_command._write_if_absent(target, "settings.json", "{}\n")
    assert not missing.exists()
    # `open_new` names no path; `_write_if_absent` restores the user's own file name, as `open` named it.
    assert refused.value.filename == str(target / "settings.json")


def test_a_plain_destination_is_still_written(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.mkdir()

    assert init_command._write_if_absent(target, ".claude/settings.json", "{}\n") is True
    assert (target / ".claude" / "settings.json").read_bytes() == b"{}\n"
    assert init_command._write_if_absent(target, ".claude/settings.json", "[]\n") is False
    assert (target / ".claude" / "settings.json").read_bytes() == b"{}\n"


@pytest.mark.parametrize("content", ["{}\n", b"\x00binary"], ids=["text", "bytes"])
def test_a_write_that_fails_after_the_create_leaves_no_empty_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | bytes
) -> None:
    """A write that fails after the exclusive create removes the file it created.

    An empty file left behind made the next ``swcraft init`` skip a scaffold file it never wrote.
    """
    target = tmp_path / "project"
    target.mkdir()
    real_open = open

    class FullDisk:
        def __init__(self, stream: IO[Any]) -> None:
            self.stream = stream

        def write(self, data: str | bytes) -> int:
            raise OSError(28, "No space left on device")

        def close(self) -> None:
            self.stream.close()

    monkeypatch.setattr(exclusive_create, "open", lambda *a, **k: FullDisk(real_open(*a, **k)), raising=False)
    with pytest.raises(OSError, match="No space left"):
        init_command._write_if_absent(target, ".claude/settings.json", content)
    assert not os.path.lexists(target / ".claude" / "settings.json")

    monkeypatch.undo()
    assert init_command._write_if_absent(target, ".claude/settings.json", content) is True
    expected = content if isinstance(content, bytes) else content.encode("utf-8")
    assert (target / ".claude" / "settings.json").read_bytes() == expected
