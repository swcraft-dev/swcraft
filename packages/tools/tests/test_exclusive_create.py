"""``exclusive_create.create_new``: a new name only, no link followed, binary, path-free errors.

The link cases make real links through ``symlink_support`` (skipped where a link cannot be
made, failing under ``SWCRAFT_REQUIRE_SYMLINKS=1``), so on Windows they exercise the real
``CreateFileW`` branch; nothing of ``kernel32`` is mocked, since a mock would prove the mock.
The mode case probes the created file's mode bits, not the platform.
"""

from __future__ import annotations

import ast
import inspect
import os
import stat
from pathlib import Path

import pytest
from swcraft_tools import exclusive_create
from swcraft_tools.exclusive_create import create_new
from symlink_support import symlink_or_skip


def _write_text(path: Path, text: str, mode: int = 0o666) -> None:
    with open(create_new(path, mode), "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def test_a_new_file_is_created_and_written(tmp_path: Path) -> None:
    target = tmp_path / "new.txt"
    with open(create_new(target), "wb") as stream:
        stream.write(b"content")
    assert target.read_bytes() == b"content"


def test_an_existing_file_is_refused_and_left_untouched(tmp_path: Path) -> None:
    target = tmp_path / "existing.txt"
    target.write_bytes(b"before")
    with pytest.raises(FileExistsError):
        create_new(target)
    assert target.read_bytes() == b"before"


def test_a_dangling_link_at_the_name_is_refused_and_its_target_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "elsewhere" / "planted.txt"
    missing.parent.mkdir()
    link = tmp_path / "link.txt"
    symlink_or_skip(link, missing)
    with pytest.raises(FileExistsError):
        create_new(link)
    assert not os.path.lexists(missing), "the dangling link was followed and its target created"
    assert link.is_symlink()


def test_a_link_to_an_existing_file_is_refused_and_the_file_untouched(tmp_path: Path) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_bytes(b"before")
    link = tmp_path / "link.txt"
    symlink_or_skip(link, existing)
    with pytest.raises(FileExistsError):
        create_new(link)
    assert existing.read_bytes() == b"before"
    assert link.is_symlink()


def test_the_mode_is_honoured_where_the_filesystem_keeps_mode_bits(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    probe.write_bytes(b"")
    probe.chmod(0o600)
    if stat.S_IMODE(probe.stat().st_mode) & 0o077:
        pytest.skip("this filesystem keeps no group/other mode bits (Windows maps chmod onto read-only)")
    current_umask = os.umask(0)
    os.umask(current_umask)

    private = tmp_path / "private.txt"
    _write_text(private, "x", 0o600)
    shared = tmp_path / "shared.txt"
    _write_text(shared, "x", 0o666)

    assert stat.S_IMODE(private.stat().st_mode) == 0o600 & ~current_umask
    assert stat.S_IMODE(shared.stat().st_mode) == 0o666 & ~current_umask


def test_a_text_write_through_the_descriptor_is_byte_exact(tmp_path: Path) -> None:
    """The binary-mode guarantee: a ``\\n`` stays one byte, on the Windows job too."""
    target = tmp_path / "text.txt"
    text = "first\nsecond\n\nthird é\n"
    _write_text(target, text)
    assert target.read_bytes() == text.encode("utf-8")


def test_an_existing_directory_is_refused_as_existing(tmp_path: Path) -> None:
    """A directory at the name is an existing entry on both branches: ``FileExistsError``, nothing changed.

    ``CreateFileW`` may answer a directory with ``ERROR_ACCESS_DENIED``; the branch maps that
    to ``FileExistsError`` when an entry is at the name.
    """
    directory = tmp_path / "a-directory"
    directory.mkdir()
    with pytest.raises(FileExistsError):
        create_new(directory)
    assert directory.is_dir()
    assert list(directory.iterdir()) == []


def test_no_error_carries_the_path(tmp_path: Path) -> None:
    existing = tmp_path / "a-distinctive-name.txt"
    existing.write_bytes(b"")
    with pytest.raises(FileExistsError) as refused:
        create_new(existing)
    absent_parent = tmp_path / "no-such-directory" / "a-distinctive-name.txt"
    with pytest.raises(FileNotFoundError) as missing:
        create_new(absent_parent)
    for error in (refused.value, missing.value):
        assert error.filename is None
        assert "a-distinctive-name" not in str(error)
        assert tmp_path.name not in str(error)
    assert refused.value.errno is not None


def test_the_posix_branch_names_o_binary_in_its_flags() -> None:
    """Rule N's descriptor half reads ``O_BINARY`` by name: the flags must spell it."""
    tree = ast.parse(inspect.getsource(exclusive_create))
    flags = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_POSIX_FLAGS" for target in node.targets)
    )
    spelled = ast.unparse(flags.value)
    assert 'getattr(os, "O_BINARY", 0)' in spelled.replace("'", '"')
    assert "O_EXCL" in spelled
    assert "O_CREAT" in spelled
    assert "_POSIX_FLAGS" in inspect.getsource(exclusive_create._create_new_posix)


# open_new and the unique siblings: the one cleanup policy


def test_open_new_writes_text_byte_exact_and_bytes_as_given(tmp_path: Path) -> None:
    """The text layer is UTF-8 with ``\\n`` line ends on every platform; the bytes layer writes what it is given."""
    text = tmp_path / "text.txt"
    with exclusive_create.open_new(text) as stream:
        stream.write("first\nsecond é\n")
    assert text.read_bytes() == "first\nsecond é\n".encode()
    data = tmp_path / "data.bin"
    with exclusive_create.open_new(data, binary=True) as stream:
        stream.write(b"\r\n\x00")
    assert data.read_bytes() == b"\r\n\x00"


def test_open_new_refuses_an_existing_name_path_free_and_touches_nothing(tmp_path: Path) -> None:
    existing = tmp_path / "a-distinctive-name.txt"
    existing.write_bytes(b"before")
    with pytest.raises(FileExistsError) as refused:
        exclusive_create.open_new(existing)
    assert refused.value.filename is None and "a-distinctive-name" not in str(refused.value)
    assert existing.read_bytes() == b"before"


def test_a_failing_wrap_closes_the_descriptor_and_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "wrapped.txt"
    created: list[int] = []
    real_create_new = exclusive_create.create_new

    def recording(path: Path, mode: int = 0o666) -> int:
        created.append(real_create_new(path, mode))
        return created[-1]

    def refusing(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(exclusive_create, "create_new", recording)
    monkeypatch.setattr(exclusive_create, "open", refusing, raising=False)
    with pytest.raises(OSError, match="No space left") as failed:
        exclusive_create.open_new(target)
    assert failed.value.filename is None
    assert not os.path.lexists(target)
    assert len(created) == 1
    with pytest.raises(OSError):
        os.fstat(created[0])


def test_a_failing_write_closes_the_stream_and_removes_the_file_this_call_created(tmp_path: Path) -> None:
    target = tmp_path / "half.txt"
    with pytest.raises(RuntimeError, match="the write failed"), exclusive_create.open_new(target) as stream:
        stream.write("half")
        raise RuntimeError("the write failed")
    assert stream.closed
    assert not os.path.lexists(target)


def test_a_failing_close_removes_the_file_and_propagates(tmp_path: Path) -> None:
    """A flush that fails at the close (a full disk) is a failed write: the file this call created goes."""
    target = tmp_path / "flushed.txt"
    created = exclusive_create.open_new(target)
    with pytest.raises(OSError, match="No space left"), created as stream:
        stream.write("x")

        def failing_close() -> None:
            type(stream).close(stream)
            raise OSError(28, "No space left on device")

        stream.close = failing_close  # type: ignore[method-assign]
    assert not os.path.lexists(target)


def _replace_at(target: Path, content: bytes) -> None:
    """Another writer's replacement: a new file renamed over *target* (a new identity at the name)."""
    replacement = target.with_name(target.name + ".theirs")
    replacement.write_bytes(content)
    os.replace(replacement, target)


def test_a_replacement_at_the_name_survives_a_failing_write(tmp_path: Path) -> None:
    """The cleanup removes only the file this call created: another writer's replacement stays.

    The stream is closed before the replacement because Windows refuses the rename over an
    open handle without ``FILE_SHARE_DELETE``.
    """
    target = tmp_path / "raced.txt"
    with pytest.raises(RuntimeError, match="the write failed"), exclusive_create.open_new(target) as stream:
        stream.write("mine")
        type(stream).close(stream)
        _replace_at(target, b"theirs")
        raise RuntimeError("the write failed")
    assert target.read_bytes() == b"theirs"


def test_a_replacement_at_the_name_survives_a_failing_close(tmp_path: Path) -> None:
    target = tmp_path / "raced-close.txt"
    with pytest.raises(OSError, match="No space left"), exclusive_create.open_new(target) as stream:
        stream.write("mine")

        def failing_close() -> None:
            type(stream).close(stream)
            _replace_at(target, b"theirs")
            raise OSError(28, "No space left on device")

        stream.close = failing_close  # type: ignore[method-assign]
    assert target.read_bytes() == b"theirs"


def test_a_name_removed_by_another_writer_is_left_alone(tmp_path: Path) -> None:
    target = tmp_path / "gone.txt"
    with pytest.raises(RuntimeError, match="the write failed"), exclusive_create.open_new(target) as stream:
        type(stream).close(stream)
        target.unlink()
        raise RuntimeError("the write failed")
    assert not os.path.lexists(target)


def test_the_removal_never_masks_the_error_that_caused_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "kept.txt"

    def refusing(path: object) -> None:
        raise PermissionError(13, "Permission denied")

    with pytest.raises(RuntimeError, match="the write failed"), exclusive_create.open_new(target):
        monkeypatch.setattr(exclusive_create.os, "unlink", refusing)
        raise RuntimeError("the write failed")


def test_a_unique_sibling_is_created_empty_beside_its_name(tmp_path: Path) -> None:
    created = exclusive_create.create_unique_sibling(tmp_path, "report.md", ".tmp")
    assert created.parent == tmp_path
    assert created.name.startswith("report.md.") and created.suffix == ".tmp"
    assert len(created.name) == len("report.md.") + 16 + len(".tmp")
    assert created.read_bytes() == b""


def test_a_unique_sibling_retries_a_taken_name_and_never_writes_through_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken, free = b"\x01" * 8, b"\x02" * 8
    draws = iter([taken, free])
    monkeypatch.setattr(exclusive_create.os, "urandom", lambda size: next(draws))
    planted = tmp_path / f"out.json.{taken.hex()}.tmp"
    planted.write_bytes(b"planted")
    path, created = exclusive_create.open_unique_sibling(tmp_path, "out.json", ".tmp")
    with created as stream:
        stream.write("mine\n")
    assert path == tmp_path / f"out.json.{free.hex()}.tmp"
    assert path.read_bytes() == b"mine\n"
    assert planted.read_bytes() == b"planted"


def test_every_unique_name_taken_lets_the_last_collision_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    taken = b"\x03" * 8
    draws: list[bytes] = []

    def drawing(size: int) -> bytes:
        draws.append(taken)
        return taken

    monkeypatch.setattr(exclusive_create.os, "urandom", drawing)
    (tmp_path / f"x.{taken.hex()}.partial").write_bytes(b"planted")
    with pytest.raises(FileExistsError):
        exclusive_create.create_unique_sibling(tmp_path, "x", ".partial")
    assert len(draws) == exclusive_create.UNIQUE_ATTEMPTS


def test_no_product_module_calls_create_new_outside_the_helper() -> None:
    """The one cleanup policy holds only while every product write goes through ``open_new`` or the siblings."""
    root = Path(__file__).resolve().parents[3]
    callers = []
    for tree in ("packages/tools/src", "packages/mcp/src", "packages/agent/src"):
        for path in sorted((root / tree).rglob("*.py"), key=lambda item: item.as_posix()):
            if path.name == "exclusive_create.py":
                continue
            source = path.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("create_new"):
                    callers.append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    assert callers == []
