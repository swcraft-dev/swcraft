"""The readings of a written path that opens with ``~``, as a client file tool that expands it may read it.

The guard (:mod:`swcraft_tools.copilot_guard`) judges a Kiro or Claude Code file tool's path under every reading the
client may give it; this module supplies the leading-``~`` readings (:func:`tilde_forms`; of a path Kiro derives,
:func:`derived_forms`; of every path derived from a written one, :func:`derived_closure`), the strings of a tool call
they apply to (:func:`tilde_strings`) and the denial sentence
for a ``~`` path that cannot be read (:func:`tilde_reason`), as pure functions of strings and the hook process's
home.

**What this does not reach:** a ``~name`` form (another user's home) and an unknown home, which have no reading
(the caller denies them); a home holding a :data:`~swcraft_tools.kiro_paths.GLOB_MAGIC` character, likewise; the
combination of the readings of a call's arguments, which is the guard's (``_tilde_readings``); the file system --
nothing here resolves a path, so a reading is a string the caller resolves and judges.

Call :func:`tilde_forms` with the path as the tool call wrote it. A leaf beneath the guard: imports the standard
library and :mod:`swcraft_tools.kiro_paths` alone.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from swcraft_tools import kiro_paths


def tilde_forms(value: str) -> tuple[str, ...] | None:
    """The readings of one written path under a host that may expand a leading ``~``, or ``None``.

    The hosts are the Kiro and Claude Code file tools. The readings: the value itself when it does not open with
    ``~``; else the literal one and the expanded ones, the hook process's home in POSIX form (no backslash of a
    Windows home reaches a glob) in place of ``~``, ``~/`` or ``~\\`` -- with, when the rest holds a backslash, a
    reading with each a separator -- and last Node's ``join(home, value.slice(1))`` (:func:`kiro_paths.node_join`),
    where on POSIX a backslash after ``~`` is a name; the home keeps all but a trailing separator of this host.
    ``None`` (unjudgeable, denied) for a ``~name`` form, an unknown home, or one holding a
    :data:`kiro_paths.GLOB_MAGIC` character but a backslash, which a glob reading takes as a pattern. Refused too: a
    UNC home (the guard's ``_judgeable_here``), a ``~`` glob whose rest holds a backslash, a drive-root home.
    """
    if not value.startswith("~"):
        return (value,)
    rest = value[1:]
    try:
        native = Path.home().as_posix()
    except (OSError, RuntimeError, KeyError):
        return None
    if (rest and rest[0] not in "/\\") or kiro_paths.GLOB_MAGIC.intersection(native.replace("\\", "")):
        return None
    home = native.rstrip("/\\" if kiro_paths.WIN32 else "/")
    tail = rest[1:]
    forms = [value, f"{home}/{tail}" if tail else home or "/"]
    if "\\" in tail:
        forms.append(f"{home}/{tail.replace(chr(92), '/')}")
    forms.append(kiro_paths.node_join(native, rest))
    return tuple(dict.fromkeys(forms))


def derived_forms(value: str) -> tuple[str, ...] | None:
    """The ``~`` readings of a path Kiro derives (a fallback, a folder-name reading).

    Such a path goes through ``resolveWorkspaceRelativePath`` again: ``~``, ``~/`` and ``~\\`` expanded
    (:func:`tilde_forms`); a ``~name`` form read literally, as Kiro reads it -- the ``~name`` denial is for the
    path as written.
    """
    if value.startswith("~") and value[1:2] not in ("", "/", "\\"):
        return (value,)
    return tilde_forms(value)


#: The rounds :func:`derived_closure` steps through; a reading set still growing after them cannot be judged.
_DERIVED_ROUNDS = 8


def derived_closure(
    readings: Iterable[str], derived: Iterable[str], steps: Callable[[str], Iterable[str]]
) -> tuple[str, ...] | None:
    """*readings* (a written path's own, the literal one first) and every path derived from them, or ``None``.

    The derived paths are the *derived* ones given and each one *steps* gives a reading; each derived path is taken
    with its ``~`` readings (:func:`derived_forms`), and each new reading is stepped again until none appears.
    ``None`` when a derived path cannot be read (an unknown home) or the set still grows after
    :data:`_DERIVED_ROUNDS` rounds (fail closed).
    """
    seen = dict.fromkeys(readings)
    frontier, pending = list(seen), list(derived)
    for _ in range(_DERIVED_ROUNDS):
        found: list[str] = []
        for path in pending + [step for reading in frontier for step in steps(reading)]:
            forms = derived_forms(path)
            if forms is None:
                return None
            found += forms
        frontier, pending = [path for path in dict.fromkeys(found) if path not in seen], []
        if not frontier:
            return tuple(seen)
        seen.update(dict.fromkeys(frontier))
    return None


def tilde_strings(
    arguments: Mapping[str, object], keys: tuple[str, ...], context_file_keys: tuple[str, ...]
) -> list[str]:
    """Every string a ``~`` reading applies to.

    The strings under *keys*, a list's string members, and each context file's ``path`` under
    *context_file_keys*.
    """
    strings: list[str] = []
    for key in keys:
        value = arguments.get(key)
        members = value if isinstance(value, list) else [value]
        strings.extend(member for member in members if isinstance(member, str))
    for key in context_file_keys:
        value = arguments.get(key)
        for member in value if isinstance(value, list) else []:
            if isinstance(member, dict) and isinstance(member.get("path"), str):
                strings.append(member["path"])
    return strings


def tilde_reason() -> str:
    """The denial of a file tool's path opening with ``~`` that cannot be read.

    Another user's home (``~name``), a home the guard cannot determine, or one holding a
    :data:`kiro_paths.GLOB_MAGIC` character.
    """
    return (
        "SWCraft: this call names a path opening with `~` that the model guard cannot expand (another user's home, "
        "a home it cannot determine, or a home folder whose path holds a search pattern's character), so what it "
        "reaches cannot be judged and it is refused in this project; write the path in full, or use the SWCraft tools."
    )
