"""The model guard's client-independent rules on a judged path: the model folder's path, or a model entry's tail.

The guard (:mod:`swcraft_tools.copilot_guard`) mirrors how each client resolves a written path; these rules judge the
**written** value itself, whatever a client does with it.

Every value, run and entry is read twice (:func:`_readings`): by its Windows segments (:func:`path_segments`: split
at ``/`` and ``\\`` on every OS, read as Windows reads a name, empty and ``.`` segments dropped, ``..`` kept,
casefolded) and by its literal POSIX segments (:func:`_literal`), and a match under either reading refuses.

**Rule 1, the model folder's path.** A value is refused when its segments hold one of the model folder's
project-relative runs (:func:`model_runs`) as a consecutive sequence, or, relative, open with a trailing part of a
run (``model/x`` for ``arch/model``). A value beginning with the project directory's own path is matched below it
only. A glob's segment holding a :data:`~swcraft_tools.kiro_paths.GLOB_MAGIC` character matches nothing.

**Rule 2, an existing entry's tail.** A value of two or more segments whose last two equal, casefolded, the parent
and name of an entry below the model root (:func:`model_index`) is refused (:func:`model_tail_reason`). Past
:data:`_MAX_MODEL_ENTRIES` entries, past the run's bound (:func:`run_end`), or on any error reading the tree, every
value of two or more segments, absolute or not, is refused until the folder is readable again or holds fewer entries.

**Not reached:** a one-segment value that is no model folder name; a new file's path holding no run and no tail; a
run or tail broken by a literal ``..`` or written as a pattern; Unicode normalisation; a Windows 8.3 short name; an
entry added after the run's walk; shell words. **Refused besides the model:** another folder named like the model
folder, a file elsewhere ending like a model entry, a path through ``..`` that passes the folder's name, and a name
Windows reads like a model name only.

Call :func:`model_roots` once per call (``None`` refuses with :func:`unreadable_tree_reason`), :func:`model_runs` and
:func:`project_prefixes` with its roots, then :func:`names_model_folder` with the written values and the globs' brace
expansions (or :func:`holds_model_folder` per value) and deny with :func:`model_path_reason`; then
:func:`model_tail_reason` with the same.
"""

from __future__ import annotations

import functools
import os
import string
import time
from collections.abc import Callable, Iterable
from pathlib import PurePath

from swcraft_tools.kiro_paths import GLOB_MAGIC

#: The model folder's runs: each a tuple of casefolded segments.
Runs = tuple[tuple[str, ...], ...]

#: Past this many entries below the model root its tree is not read (rule 2 then refuses, fail closed).
_MAX_MODEL_ENTRIES = 100_000
#: The seconds of one run's model-path work (resolving the roots and walking the model root); past them the guard
#: refuses as rule 2 does for an unreadable tree.
_MODEL_WALK_SECONDS = 2.0

_RUN: dict[str, float] = {}


def run_end(limit: float | None = None) -> float:
    """The monotonic end of this run's model-path work.

    :data:`_MODEL_WALK_SECONDS` from the first call, lowered to *limit* and never raised. A long-lived caller
    starts another run with :func:`new_run`.
    """
    end = _RUN.setdefault("end", time.monotonic() + _MODEL_WALK_SECONDS)
    if limit is not None and limit < end:
        _RUN["end"] = end = limit
    return end


def new_run() -> None:
    """Start another run: its bound (:func:`run_end`) and its model index (:func:`model_index`) set afresh."""
    _RUN.clear()
    model_index.cache_clear()


def _windows_name(part: str) -> str:
    """*part* as Windows reads a name.

    Cut at a ``:`` (a stream, ``x.yaml::$DATA``) and trailing dots and spaces dropped unless it is ``..``.
    """
    part = part.partition(":")[0]
    return part if part == ".." else part.rstrip(". ")


def _drive(text: str) -> str:
    """*text*'s leading drive: an ASCII letter and ``:``, as written; ``""`` for none."""
    return text[:2] if text[1:2] == ":" and text[:1] in string.ascii_letters else ""


def _components(text: str) -> list[str]:
    """*text*'s segments, casefolded, with no drive read.

    Split at ``/`` and ``\\``, each read as Windows reads a name (:func:`_windows_name`), empty and ``.`` segments
    dropped, ``..`` kept.
    """
    names = map(_windows_name, text.replace("\\", "/").casefold().split("/"))
    return [name for name in names if name not in ("", ".")]


def path_segments(text: str) -> list[str]:
    """*text*'s segments, casefolded, a leading drive (:func:`_drive`) a segment of its own.

    The rest is read by :func:`_components` (``swc./x.yaml.`` is ``swc/x.yaml``, ``model::$INDEX_ALLOCATION`` is
    ``model``).
    """
    drive = _drive(text).casefold()
    return [drive, *_components(text[len(drive) :])] if drive else _components(text)


def _literal(text: str) -> list[str]:
    """*text*'s segments as POSIX reads them, casefolded.

    Split at ``/`` alone, empty and ``.`` segments dropped, every other name kept as written (``...``, ``:``,
    ``a\\b``).
    """
    return [name for name in text.casefold().split("/") if name not in ("", ".")]


def _readings(text: str, glob: bool) -> list[list[str | None]]:
    """*text*'s two readings: its Windows segments (:func:`path_segments`) and its POSIX ones (:func:`_literal`).

    For a *glob*, a segment holding a :data:`GLOB_MAGIC` character is ``None``, which matches nothing.
    """
    readings = (path_segments(text), _literal(text))
    return [[None if glob and GLOB_MAGIC.intersection(part) else part for part in reading] for reading in readings]


def model_roots(
    resolve_project: Callable[[], str], resolve_model: Callable[[], str], end: float | None = None
) -> tuple[str, str, str | None] | None:
    """The project directory, the model root and the model root's project-relative name, or ``None`` past the bound.

    Each root is resolved once by its callable; the name is POSIX-spelled (``None`` outside the project or for the
    project directory itself). The run's bound (:func:`run_end`, lowered to *end*) is read before each resolution,
    and the answer is ``None`` once it has passed -- the caller refuses as for an unreadable tree.
    """
    roots = []
    for resolve in (resolve_project, resolve_model):
        if time.monotonic() >= run_end(end):
            return None
        roots.append(resolve())
    project_root, model_root = roots
    inside = model_root.startswith(project_root.rstrip(os.sep) + os.sep)
    return project_root, model_root, PurePath(model_root).relative_to(project_root).as_posix() if inside else None


def model_subject(name: str | None) -> str:
    """``the model directory (`<name>`)``, *name* the model root relative to the project, or without a name."""
    return f"the model directory (`{name}`)" if name is not None else "the model directory"


def model_runs(project_root: str, written_root: str, model_name: str | None) -> Runs:
    """The model folder's runs under both spellings, *written_root* and *model_name*.

    *written_root* (the manifest's ``model_root``) is joined to *project_root* and normalised lexically, relative to
    it; *model_name* is the resolved model root's project-relative name. No drive is read in them: each gives its
    Windows segments (:func:`_components`) and its literal POSIX ones (:func:`_literal`, the host's separator read as
    ``/``). ``()`` when *model_name* is ``None`` (the model is the project directory itself), and a spelling that
    leaves the project adds no run.
    """
    if model_name is None:
        return ()
    lexical = os.path.normpath(os.path.join(project_root, written_root))
    try:
        written = os.path.relpath(lexical, project_root)
    except ValueError:  # another drive: the written spelling names no folder of the project
        written = ""
    names = [name.replace(os.sep, "/") for name in (written, model_name)]
    runs = {tuple(reading(name)) for name in names for reading in (_components, _literal)}
    return tuple(sorted(run for run in runs if run and ".." not in run))


def project_prefixes(
    project_root: str, directory: str | os.PathLike[str], resolve: Callable[[str], str | None], end: float | None = None
) -> list[str]:
    """The project directory's spellings a value is matched below.

    *project_root* (resolved), and each of the call's *directory* (made absolute, not resolved) and its ancestors
    that *resolve* (a path to its resolved spelling, ``None`` when it cannot be resolved) gives as *project_root* --
    the project as the client spells it, through a link. The ancestors are resolved within the run's bound
    (:func:`run_end`, lowered to *end*) and no further: a spelling left out matches a value whole, which only
    refuses more.
    """
    written = os.path.abspath(directory)
    prefixes = [project_root]
    for name in (written, *map(str, PurePath(written).parents)):
        if time.monotonic() >= run_end(end):
            break
        if name != project_root and resolve(name) == project_root:
            prefixes.append(name)
    return prefixes


def holds_model_folder(value: str, runs: Runs, prefixes: Iterable[str], *, glob: bool = False) -> bool:
    """Whether *value* holds a run of *runs* as consecutive segments, or, relative, opens with a run's trailing part.

    Below each of *prefixes* (the project directory resolved and as found) that *value* begins with, only the rest
    is matched; *glob* makes a segment holding a glob character match nothing.
    """
    literal = value.casefold()
    unified = literal.replace("\\", "/")
    cuts = []
    for prefix in prefixes:
        folded = prefix.replace("\\", "/").casefold().rstrip("/")
        if unified == folded or unified.startswith(folded + "/"):
            cuts.append(len(folded))
    relative = not cuts and not unified.startswith("/") and not _drive(unified)
    for segments in (reading for cut in cuts or [0] for reading in _readings(literal[cut:], glob)):
        for run in runs:
            width = len(run)
            if any(tuple(segments[start : start + width]) == run for start in range(len(segments) - width + 1)):
                return True
            if relative and any(tuple(segments[: width - cut]) == run[cut:] for cut in range(1, width)):
                return True
    return False


def names_model_folder(
    values: Iterable[object], expansions: Iterable[str], runs: Runs, prefixes: Iterable[str]
) -> bool:
    """Whether a string of *values*, or one of *expansions* (the globs' brace expansions), holds the model folder.

    Judged by :func:`holds_model_folder`, an expansion read as a glob; a value that is not a string is the caller's
    to judge.
    """
    prefixes = list(prefixes)
    return any(holds_model_folder(value, runs, prefixes) for value in values if isinstance(value, str)) or any(
        holds_model_folder(expansion, runs, prefixes, glob=True) for expansion in expansions
    )


def model_path_reason(subject: str) -> str:
    """The denial of a value :func:`holds_model_folder` holds.

    It names the model by *subject* (``the model directory (`<name>`)``) and never the written path.
    """
    return (
        f"SWCraft: this call names a path that holds the path of {subject}, or opens with its last folders, and a "
        "client may resolve such a path into the model whatever it does with it, so it is refused in this project; "
        "use the SWCraft tools, or name the file by a path that does not hold the model folder's path."
    )


@functools.cache
def model_index(model_root: str) -> frozenset[tuple[str, str]] | None:
    """The ``(parent, name)`` pairs of the entries below *model_root*, or ``None`` when the tree cannot be read whole.

    Each pair is the last two segments of an entry's path from the model root's own name, under both readings of a
    judged value (:func:`_components`, :func:`_literal`). Walked without following links and kept for the run. A
    root that does not exist has no entries; one that cannot be read, past :data:`_MAX_MODEL_ENTRIES` entries, past
    the run's bound (:func:`run_end`), or on any error reading the tree, the index is ``None``. A Windows junction
    may be walked as a folder; the limits bound it.
    """
    end = run_end()
    if time.monotonic() >= end:
        return None
    try:
        os.lstat(model_root)
    except (FileNotFoundError, NotADirectoryError):
        return frozenset()
    except OSError:
        return None
    tails: set[tuple[str, str]] = set()
    count = 0
    name = os.path.basename(model_root)
    pending = [(model_root, _components(name), _literal(name))]
    try:
        while pending:
            folder, windows, literal = pending.pop()
            if time.monotonic() >= end:
                return None
            with os.scandir(folder) as entries:
                for entry in entries:
                    count += 1
                    if count > _MAX_MODEL_ENTRIES or time.monotonic() >= end:
                        return None
                    paths = ([*windows, *_components(entry.name)], [*literal, *_literal(entry.name)])
                    tails.update((path[-2], path[-1]) for path in paths if len(path) >= 2)
                    if entry.is_dir(follow_symlinks=False):
                        pending.append((entry.path, *paths))
    except OSError:
        return None
    return frozenset(tails) if time.monotonic() < end else None


def model_tail_reason(
    values: Iterable[object], expansions: Iterable[str], model_root: str, subject: str, deadline: float | None = None
) -> str | None:
    """Rule 2's denial for the strings of *values* and the globs' brace *expansions*, or ``None``.

    The reason when one of two or more segments ends like an entry of *model_root* (:func:`model_entry_reason`), or
    when one has two or more segments and the tree is unreadable (:func:`model_index`) or the run's bound
    (:func:`run_end`, lowered to *deadline*) has passed (:func:`unreadable_tree_reason`). The tree is walked only
    when a value has two or more segments. *subject* names the model.
    """
    tails = [tuple(reading[-2:]) for value in values if isinstance(value, str) for reading in _readings(value, False)]
    tails += [tuple(reading[-2:]) for expansion in expansions for reading in _readings(expansion, True)]
    tails = [tail for tail in tails if len(tail) == 2]
    if not tails:
        return None
    run_end(deadline)
    index = model_index(model_root)
    if index is None or time.monotonic() >= run_end():
        return unreadable_tree_reason(subject)
    return model_entry_reason(subject) if any(tail in index for tail in tails) else None


def model_entry_reason(subject: str) -> str:
    """The denial of a value ending like an entry of the model, naming the model by *subject* and never the path."""
    return (
        f"SWCraft: this call names a path whose last folder and name are those of a file or folder of {subject}, and "
        "a client may resolve such a path into the model whatever it does with it, so it is refused in this project; "
        "use the SWCraft tools, or name the file by a path that does not end like a model file."
    )


def unreadable_tree_reason(subject: str = model_subject(None)) -> str:
    """The denial while the model tree cannot be read whole, naming the model by *subject* and never a path.

    *subject* names no model by default, for a judgement stopped before the model root is resolved.
    """
    return (
        f"SWCraft: the entries of {subject} could not be read (too many, not listed within the time this call had "
        "left, or a folder that cannot be read), so whether this call's path ends like a model file cannot be judged, "
        "and every path of two or more segments is refused in this project until they can; retry with a shorter "
        "call, make the model folder readable (its permissions, a broken mount) or keep less in it, and use the "
        "SWCraft tools meanwhile."
    )
