"""Create a new file exclusively, following no link at its name, on Windows too.

:func:`create_new` is the one exclusive create of the Python product trees and returns a writable
descriptor in binary mode. :func:`open_new` wraps it in the text (``encoding="utf-8"``,
``newline="\\n"``) or bytes layer and owns the cleanup; :func:`create_unique_sibling` and
:func:`open_unique_sibling` add a random-name retry for temporary files.

- **The name is new.** Any entry at *path* -- a file, a directory, a symbolic link, a dangling one
  included -- raises :class:`FileExistsError` and nothing is created (``O_CREAT | O_EXCL`` on POSIX,
  ``CREATE_NEW`` with ``FILE_FLAG_OPEN_REPARSE_POINT`` on Windows).
- **Binary mode**: a ``\\n`` written in text mode with ``newline="\\n"`` is one byte on Windows too.
- **No error carries the path**: a Windows error is ``ctypes.WinError(code)`` with no filename, and a
  POSIX :class:`OSError` is re-raised as the same subclass and ``errno`` without the name.
- **Mode.** On POSIX *mode* is ``os.open``'s, under the umask. On Windows it decides only
  ``FILE_ATTRIBUTE_READONLY`` (set when ``mode & 0o200`` is clear).
- **Cleanup.** On a failure after the create, :func:`open_new` closes the descriptor and removes the
  file only while its name still matches the created file's identity (a failing removal suppressed);
  a caller that renames the file after the ``with`` block keeps its own cleanup for that step.

What it does not reach:

- a filesystem that does not keep ``O_EXCL`` (NFS before version 3) is not made exclusive here;
- a junction or symbolic link in a parent directory is followed; only the final name is refused;
- the identity check before the removal is not atomic: a replacement landing between the check and
  the unlink is still removed, and a failing ``fstat`` leaves the file;
- on Windows, a failing ``open_osfhandle`` that cannot delete the file leaves the empty file, which a
  later ``swcraft init`` sees as existing.
"""

from __future__ import annotations

import contextlib
import errno
import os
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import IO, Any, TypeVar

#: Win32 constants (``winnt.h``, ``fileapi.h``, ``winerror.h``).
_GENERIC_WRITE = 0x40000000
#: ``FILE_SHARE_READ | FILE_SHARE_WRITE``: the sharing of the CRT's ``_wopen``.
_SHARE_READ_WRITE = 0x3
_CREATE_NEW = 1
_FILE_ATTRIBUTE_READONLY = 0x1
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_DELETE = 0x00010000
_FILE_READ_ATTRIBUTES = 0x80
_SHARE_ALL = 0x7
_OPEN_EXISTING = 3
_FILE_DISPOSITION_INFO_CLASS = 4
_ERROR_ACCESS_DENIED = 5
_ERROR_FILE_EXISTS = 80
_ERROR_ALREADY_EXISTS = 183

#: The POSIX branch's flags. ``O_BINARY`` and ``O_CLOEXEC`` are ``0`` where the
#: platform has none.
_POSIX_FLAGS = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)

_T = TypeVar("_T")

#: How many random names the unique-sibling creates try before the collision raises.
UNIQUE_ATTEMPTS = 8


def create_new(path: str | os.PathLike[str], mode: int = 0o666) -> int:
    """Create *path*, which must not exist, and return a binary writable descriptor.

    Any entry at *path*, a dangling symbolic link included, raises
    :class:`FileExistsError` and creates nothing. No error raised here names
    *path*.
    """
    try:
        import msvcrt  # noqa: PLC0415 -- platform: a Windows-only module, imported on the Windows branch
    except ImportError:
        return _create_new_posix(path, mode)
    return _create_new_windows(msvcrt, path, mode)


def _create_new_posix(path: str | os.PathLike[str], mode: int) -> int:
    """``os.open`` with ``O_CREAT | O_EXCL | O_WRONLY | O_BINARY | O_CLOEXEC``, path-free errors."""
    try:
        return os.open(path, _POSIX_FLAGS, mode)
    except OSError as error:
        raise type(error)(error.errno, error.strerror) from None


def _create_new_windows(msvcrt: object, path: str | os.PathLike[str], mode: int) -> int:
    """``CreateFileW`` with ``CREATE_NEW | FILE_FLAG_OPEN_REPARSE_POINT``, as a CRT descriptor."""
    import ctypes  # noqa: PLC0415 -- platform: a Windows-only module, imported on the Windows branch
    from ctypes import wintypes  # noqa: PLC0415 -- platform: a Windows-only module, imported on the Windows branch

    win_dll = getattr(ctypes, "WinDLL")  # named on the probed branch alone
    kernel32 = win_dll("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    attributes = _FILE_ATTRIBUTE_NORMAL if mode & 0o200 else _FILE_ATTRIBUTE_READONLY
    handle = create_file(
        os.fspath(Path(path)),
        _GENERIC_WRITE,
        _SHARE_READ_WRITE,
        None,
        _CREATE_NEW,
        attributes | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    invalid_handle_value = wintypes.HANDLE(-1).value
    if handle is None or handle == invalid_handle_value:
        code = getattr(ctypes, "get_last_error")()  # Windows-only, as WinDLL
        if code in (_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS) or (
            code == _ERROR_ACCESS_DENIED and os.path.lexists(path)
        ):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST))
        raise getattr(ctypes, "WinError")(code)  # Windows-only, as WinDLL
    try:
        return getattr(msvcrt, "open_osfhandle")(handle, os.O_WRONLY)  # `msvcrt` is typed `object` here
    except BaseException:
        _close_and_delete_windows(kernel32, path, handle)  # closes *handle* whatever happens
        raise


def _close_and_delete_windows(kernel32: Any, path: str | os.PathLike[str], handle: int) -> None:
    """Close *handle*, then delete the file it created -- only while *path* is still that file.

    The created handle has no ``DELETE`` and shares no delete, so the identity is
    read from it, it is closed, and *path* is reopened with ``DELETE`` and
    ``FILE_FLAG_OPEN_REPARSE_POINT``; only a reopened handle naming the same file
    is marked for deletion through itself (``FileDispositionInfo``), so no
    replacement slips in between the check and the delete. Any failure leaves the
    name alone. *handle* is always closed, and nothing raised here masks the
    error that caused the cleanup.
    """
    closed = False
    try:
        with contextlib.suppress(Exception):  # cleanup: never masks the error that caused it
            import ctypes  # noqa: PLC0415 -- platform: a Windows-only module, imported on the Windows branch
            from ctypes import wintypes  # noqa: PLC0415 -- platform: a Windows-only module, imported on the Windows branch

            class _ByHandleFileInformation(ctypes.Structure):
                _fields_ = (
                    ("dwFileAttributes", wintypes.DWORD),
                    ("ftCreationTime", wintypes.FILETIME),
                    ("ftLastAccessTime", wintypes.FILETIME),
                    ("ftLastWriteTime", wintypes.FILETIME),
                    ("dwVolumeSerialNumber", wintypes.DWORD),
                    ("nFileSizeHigh", wintypes.DWORD),
                    ("nFileSizeLow", wintypes.DWORD),
                    ("nNumberOfLinks", wintypes.DWORD),
                    ("nFileIndexHigh", wintypes.DWORD),
                    ("nFileIndexLow", wintypes.DWORD),
                )

            class _FileDispositionInfo(ctypes.Structure):
                _fields_ = (("DeleteFile", wintypes.BOOLEAN),)

            get_information = kernel32.GetFileInformationByHandle
            get_information.argtypes = (wintypes.HANDLE, wintypes.LPVOID)
            get_information.restype = wintypes.BOOL
            set_information = kernel32.SetFileInformationByHandle
            set_information.argtypes = (wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD)
            set_information.restype = wintypes.BOOL

            def identity(of: int) -> tuple[int, int, int] | None:
                information = _ByHandleFileInformation()
                if not get_information(of, ctypes.byref(information)):
                    return None
                return (information.dwVolumeSerialNumber, information.nFileIndexHigh, information.nFileIndexLow)

            created = identity(handle)
            closed = True
            kernel32.CloseHandle(handle)
            if created is None:
                return  # unidentified: the file stays rather than a guess removing another writer's
            reopened = kernel32.CreateFileW(
                os.fspath(Path(path)),
                _DELETE | _FILE_READ_ATTRIBUTES,
                _SHARE_ALL,
                None,
                _OPEN_EXISTING,
                _FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            if reopened is None or reopened == wintypes.HANDLE(-1).value:
                return
            try:
                if identity(reopened) == created:
                    disposition = _FileDispositionInfo(DeleteFile=True)
                    set_information(
                        reopened, _FILE_DISPOSITION_INFO_CLASS, ctypes.byref(disposition), ctypes.sizeof(disposition)
                    )
            finally:
                kernel32.CloseHandle(reopened)
    finally:
        if not closed:
            kernel32.CloseHandle(handle)


class NewFile:
    """A file :func:`open_new` created, open for writing; a context manager yielding the stream.

    Leaving the ``with`` block closes the stream. A failure inside the block, or
    a failing close, removes the file while the name is still the file *created*
    identifies, and propagates.
    """

    def __init__(self, path: str | os.PathLike[str], stream: IO[Any], created: os.stat_result) -> None:
        self._path = path
        self._stream = stream
        self._created = created

    def __enter__(self) -> IO[Any]:
        return self._stream

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self._stream.close()
        except BaseException:
            _discard(self._path, self._created)
            raise
        if kind is not None:
            _discard(self._path, self._created)


def open_new(path: str | os.PathLike[str], mode: int = 0o666, *, binary: bool = False) -> NewFile:
    """Create *path* exclusively (:func:`create_new`) and open it for writing.

    Text unless *binary*: UTF-8 with ``\\n`` line ends on every platform. The
    create's errors (:class:`FileExistsError` for any entry at *path*) propagate
    path-free. A wrap that fails closes the descriptor and removes the file; use
    the result in a ``with`` block, which owns the rest of the cleanup.
    """
    descriptor = create_new(path, mode)
    try:
        created = os.fstat(descriptor)  # the identity the cleanup checks; taken while the descriptor is open
    except BaseException:
        os.close(descriptor)  # unidentified: the cleanup leaves the name alone rather than guess
        raise
    try:
        if binary:
            stream: IO[Any] = open(descriptor, "wb")
        else:
            stream = open(descriptor, "w", encoding="utf-8", newline="\n")
    except BaseException:
        os.close(descriptor)  # closed before the removal, which Windows refuses on an open file
        _discard(path, created)
        raise
    return NewFile(path, stream, created)


def create_unique_sibling(directory: Path, name: str, suffix: str, mode: int = 0o666) -> Path:
    """Create an empty ``<name>.<random>{suffix}`` in *directory* that no other caller holds, and return it.

    The file gets *mode* less the umask -- the mode a plain ``write_text`` gives,
    never ``mkstemp``'s ``0o600``, which a rename would carry onto the final file;
    the umask is never read or set, so a multi-threaded host is not raced. A name
    that exists is retried with fresh random bytes, :data:`UNIQUE_ATTEMPTS` times
    in all, and the last :class:`FileExistsError` propagates.
    """
    return _unique_sibling(directory, name, suffix, lambda candidate: os.close(create_new(candidate, mode)))[0]


def open_unique_sibling(directory: Path, name: str, suffix: str, mode: int = 0o666) -> tuple[Path, NewFile]:
    """:func:`create_unique_sibling`'s name and retry, the file opened as text by :func:`open_new`."""
    return _unique_sibling(directory, name, suffix, lambda candidate: open_new(candidate, mode))


def _unique_sibling(directory: Path, name: str, suffix: str, create: Callable[[Path], _T]) -> tuple[Path, _T]:
    """*create* ``<name>.<16 hex digits>{suffix}`` in *directory*, retried on :class:`FileExistsError`."""
    attempt = 1
    while True:
        candidate = directory / f"{name}.{os.urandom(8).hex()}{suffix}"
        try:
            return candidate, create(candidate)
        except FileExistsError:
            if attempt == UNIQUE_ATTEMPTS:
                raise
            attempt += 1


def _discard(path: str | os.PathLike[str], created: os.stat_result) -> None:
    """Remove *path* while it is still the file *created* identifies; never mask the error that caused it.

    Another entry at the name (a writer's replacement) stays.
    """
    with contextlib.suppress(OSError):
        if os.path.samestat(os.lstat(path), created):
            os.unlink(path)
