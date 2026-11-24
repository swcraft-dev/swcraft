"""The path readings Kiro's tool service and Node's ``path`` module give a written path, transcribed.

The guard (:mod:`swcraft_tools.copilot_guard`) judges a Kiro file tool's path under every reading Kiro may give it;
this module supplies the readings that are Kiro's or Node's own, as pure functions of strings. Each mirrors a
function by name, read from the kiro-cli 3.1.0 and 3.3.0 bundles (KAS 0.66.91 / 0.66.102), whose tool service
Kiro IDE 1.4.178 (``kiro.kiro-agent`` 1.1.939) shares:

* :func:`strip_leading_segment` -- ``stripLeadingPathSegment``; :func:`read_fallback` -- ``readWithPathFallbacks``
  (``read_file``'s retry); :func:`context_fallback` -- ``tryReadWithFallback`` (a context file's retry);
  :func:`workspace_folder_readings` -- ``resolveWorkspaceRelativePath``'s workspace-folder match;
* Node's ``path.join`` / ``path.normalize`` (:func:`node_join`) and ``path.isAbsolute`` (:func:`is_absolute`), each
  by this host's rule (``path.win32`` on Windows, ``path.posix`` elsewhere; :data:`WIN32`); the ``..`` reading they
  give (:func:`dotted_reading`) and a ``file:`` URI's ``fsPath``, which drops the slash before a drive
  (:func:`uri_drive_reading`).

It also holds :data:`GLOB_MAGIC`, the glob syntax every guard leaf masks.

**What this does not reach:** a later Kiro's path resolution -- these mirror the releases named above, and a
change in Kiro is caught only by re-reading its bundle; the leading-``~`` expansion (:mod:`swcraft_tools.tilde_paths`)
and the combination of the readings, which is the guard's (``_kiro_forms``); the file system -- nothing here resolves a
link or reads a folder, so a reading is a string the caller resolves and judges.

Call each function with the path as the tool call wrote it; a function that gives ``None`` or ``()`` adds no
reading. Imports the standard library alone: a leaf beneath the guard.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import re

#: Whether Node's ``path`` module is ``path.win32`` here: on Windows, where a backslash separates.
WIN32 = os.name == "nt"

#: Every character some glob engine reads as syntax: the wildcards, classes, braces,
#: extglob (``@(...)``, ``+(...)``, ``!(...)``, ``|``) and the escape. One of them
#: ends a glob's literal leading directories.
GLOB_MAGIC = frozenset("\\*?[]{}()@+!|")

#: A separator in a path Kiro splits: ``/`` or ``\``, on every host.
_SEPARATOR = re.compile(r"[/\\]")


def node_join(base: str | None, rest: str) -> str:
    """Node's ``path.join(base, rest)`` (``path.normalize(rest)`` for no *base*) by this host's rule.

    ``..`` is applied lexically, before any link is followed; the result is spelled with ``/``.
    """
    module = ntpath if WIN32 else posixpath
    joined = module.normpath(rest if base is None else module.join(base, "./" + rest))
    return joined.replace("\\", "/") if WIN32 else joined


def strip_leading_segment(value: str) -> str | None:
    """kiro-cli's ``stripLeadingPathSegment``: *value* without its first segment, or ``None``.

    ``readWithPathFallbacks`` (``read_file``) retries a failed read of a path that is not absolute with it: split
    on ``/`` or ``\\``, the first segment dropped, the rest joined with ``/``; ``None`` -- no retry -- for one
    segment, or a rest holding an empty or ``..`` segment.
    """
    segments = _SEPARATOR.split(value)
    if len(segments) <= 1 or any(segment in ("", "..") for segment in segments[1:]):
        return None
    return "/".join(segments[1:])


def is_absolute(value: str, *, windows: bool | None = None) -> bool:
    """Node's ``path.isAbsolute``, which kiro-cli asks of a path, by the host's rule.

    The host is *windows*, by default :data:`WIN32` read at the call. On POSIX a leading ``/``; on Windows a
    leading ``/`` or ``\\`` (a rooted path, a UNC one among them) or a drive letter followed by either separator
    (``C:\\x``, ``C:/x``; the drive-relative ``C:x`` is not absolute). Transcribed because Python's
    :meth:`PurePath.is_absolute` differs on Windows for a rooted path without a drive.
    """
    if not (WIN32 if windows is None else windows):
        return value.startswith("/")
    return value.startswith(("/", "\\")) or re.match(r"[A-Za-z]:[/\\]", value) is not None


def read_fallback(value: str) -> str | None:
    """The path ``read_file`` retries a failed read with (kiro-cli's ``readWithPathFallbacks``).

    None for an absolute path (Node's ``path.isAbsolute`` on the host, :func:`is_absolute`), else
    :func:`strip_leading_segment`. The whitespace-tolerant match that follows it stays in the same folder.
    """
    return None if is_absolute(value) else strip_leading_segment(value)


def context_fallback(value: str) -> str | None:
    """The path a context file's failed read is retried with (kiro-cli's ``tryReadWithFallback``).

    ``invoke_sub_agent`` reads its ``contextFiles`` through it: when the path holds a ``/`` or ``\\`` and opens
    with neither nor a drive letter, its first segment dropped and the rest joined with ``/`` -- with **no** ``..``
    check, so ``zz/../x`` is retried as ``../x``.
    """
    if not _SEPARATOR.search(value) or value.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", value):
        return None
    return "/".join(_SEPARATOR.split(value)[1:])


def workspace_folder_readings(value: str, names: frozenset[str]) -> tuple[str, ...]:
    """The readings kiro-cli's ``resolveWorkspaceRelativePath`` gives a relative path in a multi-folder workspace.

    Kiro normalises the path, matches its first segment against each workspace folder's name and, on a match,
    resolves the rest into that folder. Here: the rest (``.`` for none), to be resolved against the anchors, when
    the first segment is one of *names* (casefolded), the path split once on the host's own separator, as Kiro
    splits it, and once on ``/`` and ``\\`` both. None for a ``~`` path, an absolute or UNC one, or a first
    segment naming no known folder.
    """
    if value == "~" or value.startswith(("~/", "~\\", "\\\\", "//")) or is_absolute(value):
        return ()
    normal = os.path.normpath(value)
    splits = (normal.split(os.sep), _SEPARATOR.split(normal))
    return tuple(dict.fromkeys("/".join(rest) or "." for first, *rest in splits if first.casefold() in names))


def uri_drive_reading(value: str) -> tuple[str, ...]:
    """The reading of *value* as a ``file:`` URI's ``fsPath``, which drops the slash before a drive.

    ``/C:/x`` is ``C:/x``; ``()`` for a value not opening with ``/`` and a drive letter.
    """
    return (value[1:],) if re.match(r"/[A-Za-z]:", value) else ()


def dotted_reading(value: str, directory: str) -> str | None:
    """Node's reading of a *value* holding a ``..`` segment.

    :func:`node_join` with *directory* (none for an absolute *value*, :func:`is_absolute`), ``..`` applied before
    any link is followed. ``None`` for a value without a ``..`` segment, or opening with ``~``, whose readings are
    the ``~`` expansion's.
    """
    if value.startswith("~") or ".." not in _SEPARATOR.split(value):
        return None
    return node_join(None if is_absolute(value) else directory, value)
