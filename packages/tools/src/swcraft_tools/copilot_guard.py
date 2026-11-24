"""``swcraft guard``: the ``PreToolUse`` hook that keeps an assistant's file and shell tools off the model.

Copilot, Codex and Claude Code run ``swcraft guard [--host codex|claude]`` before every tool call, hand it one JSON
payload on stdin and read its decision from stdout; Kiro runs ``swcraft guard --host kiro`` and reads a denial from
the exit code and stderr. The hook file names the host (:data:`HOSTS`); the payload is never sniffed.

The decision is pure functions over the payload: :func:`decide` answers a :class:`Decision` for one project,
:func:`decide_all` for every project :func:`guarded_projects` finds, and :func:`render` spells it as the host
reads it. :func:`run_hook` is the command's whole run -- it reads stdin, checks the toolchain pin, prints and
returns the exit code -- and is the one function here that prints; nothing here exits.

Every rule, per host, and what each does not reach: the user guide's *The model guard's rules*
(``docs/guide/model-guard.md``), the public statement of this module's behaviour.
"""

from __future__ import annotations

import bisect
import errno
import fnmatch
import functools
import json
import os
import re
import shlex
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from swcraft_tools import guard_model_paths, kiro_paths, tilde_paths
from swcraft_tools.guarantee import required_instance
from swcraft_tools.project import (
    MANIFEST_FILENAME,
    SwcraftProject,
    path_contains,
    paths_overlap,
    project_relative_name,
    require_project,
    resolved_path,
)
from swcraft_tools.rendering import path_free
from swcraft_tools.swcraft_home import reviews_location
from swcraft_tools.toolchain_pin import toolchain_pin_problem

__all__ = [
    "CLAUDE",
    "CLAUDE_DENIED_TOOLS",
    "CLAUDE_SETTINGS_FILES",
    "CLAUDE_SHELL_TOOLS",
    "CLAUDE_WRITE_PATH_KEYS",
    "CLAUDE_WRITE_TOOLS",
    "CODEX",
    "CODEX_HOOK_FILE",
    "CODEX_PATCH_TOOLS",
    "CODEX_SHELL_TOOLS",
    "COPILOT",
    "GLOB_PATH_KEYS",
    "HOSTS",
    "KIRO",
    "KIRO_ALLOWED_TOOLS",
    "KIRO_CODE_OPERATIONS",
    "KIRO_HOOKS_FOLDER",
    "KIRO_JUDGED_TOOLS",
    "KIRO_MCP_PREFIX",
    "READ_PATH_KEYS",
    "READ_TOOLS",
    "SAVED_ANSWER_NAME",
    "SEARCH_PATH_KEYS",
    "SEARCH_TOOLS",
    "Decision",
    "GuardedProject",
    "KiroTool",
    "claude_project_directory",
    "claude_session_project",
    "codex_project_directory",
    "decide",
    "decide_all",
    "guarded_project",
    "guarded_projects",
    "is_saved_answer",
    "kiro_project_directory",
    "kiro_reason_line",
    "parse_payload",
    "payload_directory",
    "read_project",
    "render",
    "run_hook",
]

#: The agent hosts whose hook dialect the guard speaks, the default first: ``swcraft guard --host <host>``.
HOSTS = ("copilot", "codex", "claude", "kiro")

COPILOT, CODEX, CLAUDE, KIRO = HOSTS

#: The tools that read one file or list one directory. ``Read``, ``NotebookRead`` and ``LS`` are the hook-side
#: names; ``view`` is the SDK name a host may send; ``read`` is the custom-agent alias of ``Read`` and
#: ``NotebookRead``, judged although no hook payload naming it was observed. A name matches casefolded (every
#: alias is case-insensitive), so ``READ`` or ``ls`` is one.
READ_TOOLS = frozenset({"Read", "read", "view", "NotebookRead", "LS"})

#: The tools that search a tree, under both name sets, ``rg``, and ``search`` -- the custom-agent alias for
#: ``Grep`` and ``Glob``, judged on the same terms as ``read``. Matched casefolded.
SEARCH_TOOLS = frozenset({"Grep", "Glob", "grep", "glob", "rg", "search"})

_READ_FOLDED = frozenset(tool.casefold() for tool in READ_TOOLS)
_SEARCH_FOLDED = frozenset(tool.casefold() for tool in SEARCH_TOOLS)

#: Where a read tool's path arrives; every key present is judged.
READ_PATH_KEYS = ("path", "file_path", "notebook_path")

#: Where a search tool's root arrives, every key present judged: ``path`` a string, ``paths`` the list a
#: ``Grep`` carries. Without a root the root is the payload's ``cwd``.
SEARCH_PATH_KEYS = ("path", "paths")

#: Where a search tool's glob arrives, per tool: a ``Glob``'s ``pattern`` is a
#: glob, a ``Grep``'s is a regular expression and never a path, and each takes a
#: ``glob`` filter. A glob's literal leading directories are judged as a root is,
#: per brace expansion; a ``..`` after its first wildcard makes it unjudgeable.
GLOB_PATH_KEYS = {
    "Glob": ("pattern", "glob"),
    "glob": ("pattern", "glob"),
    "Grep": ("glob",),
    "grep": ("glob",),
    "rg": ("glob",),
    "search": ("pattern", "glob"),
}

#: :data:`GLOB_PATH_KEYS` under casefolded tool names. ``search`` may be either
#: tool, so its ``pattern`` is judged as a glob: a regular expression read as one
#: only judges more, the fail-closed direction.
_GLOB_KEYS_FOLDED = {tool.casefold(): keys for tool, keys in GLOB_PATH_KEYS.items()}

#: A saved Copilot answer's file name: ``<epoch-ms>-copilot-tool-output-<hex>.txt``. A host format, not a
#: contract.
SAVED_ANSWER_NAME = re.compile(r"[^/\\]*-copilot-tool-output-[0-9A-Fa-f]+\.txt")

#: The components no engine matches to ``..``: POSIX ``glob``, bash (even with
#: ``dotglob``), minimatch (even with ``dot``), Python's ``glob`` and globset all
#: require a leading ``.`` to be written, and never match ``.`` or ``..`` by a
#: wildcard.
_NEVER_CLIMBS = frozenset({"*", "**"})

#: The one drive spelling judged: a letter and a colon (:func:`_judgeable_here`).
_DRIVE_LETTER = re.compile(r"[A-Za-z]:")

#: The longest glob judged; a longer one is denied unjudged, which bounds the brace scan.
_MAX_GLOB_LENGTH = 4096

#: The most brace expansions one glob is judged by; past it the glob is unresolvable.
_MAX_EXPANSIONS = 256


@dataclass(frozen=True)
class Decision:
    """One hook answer: allow, or deny with the sentence the agent is shown."""

    allow: bool
    reason: str | None = None


@dataclass(frozen=True)
class GuardedProject:
    """What the guard knows of the project at a directory.

    ``present`` is whether ``swcraft.yaml`` is there at all; ``project`` is the
    loaded manifest, or ``None`` with ``problem`` naming why it could not be
    loaded, path-free. ``manifest`` is the manifest's path when it is present;
    it takes no part in a comparison.
    """

    present: bool
    project: SwcraftProject | None = None
    problem: str | None = None
    manifest: Path | None = field(default=None, compare=False)


def parse_payload(data: bytes) -> Mapping[str, object] | None:
    """The hook payload decoded as UTF-8 JSON, or ``None`` when it is not a JSON object.

    JSON the interpreter cannot parse (``RecursionError``, the digit limit's
    ``ValueError``) raises, and :func:`run_hook` answers it as unexpected, which fails closed in a project.
    """
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def payload_directory(payload: Mapping[str, object], working_directory: Path) -> Path:
    """The payload's ``cwd`` when it carries a non-empty string, else *working_directory*."""
    cwd = payload.get("cwd")
    return Path(cwd) if isinstance(cwd, str) and cwd else working_directory


def manifest_present(directory: Path) -> bool:
    """Whether anything is at ``<directory>/swcraft.yaml``: the one presence rule of the guard.

    Only a path that is genuinely not there -- no entry, or a parent that is no
    directory -- is absent. A directory, a broken symbolic link, or an entry whose
    status cannot be read is present, so the guard fails closed on it.
    """
    return _entry_present(directory / MANIFEST_FILENAME)


def read_project(directory: Path) -> GuardedProject:
    """``<directory>/swcraft.yaml`` through :func:`~swcraft_tools.project.require_project`, never a walk."""
    manifest = directory / MANIFEST_FILENAME
    if not manifest_present(directory):
        return GuardedProject(present=False)
    try:
        return GuardedProject(present=True, project=require_project(str(manifest)), manifest=manifest)
    except Exception as exc:  # noqa: BLE001 -- hook: every refusal of the loader, and a read that fails
        problem = " ".join(path_free(exc, _resolved_directory(directory)).split())
        return GuardedProject(present=True, problem=problem, manifest=manifest)


def guarded_project(directory: Path, host: str = COPILOT) -> GuardedProject:
    """The first project *host*'s hook guards for a call made in *directory*, or no project.

    The first of :func:`guarded_projects`."""
    return guarded_projects(directory, host)[0]


def guarded_projects(directory: Path, host: str = COPILOT) -> list[GuardedProject]:
    """Every project *host*'s hook guards for a call made in *directory*.

    Each is read by :func:`read_project`; ``[GuardedProject(present=False)]`` when there is none.

    Copilot: *directory* itself, never a walk. Codex: :func:`codex_project_directory`. Claude Code:
    :func:`claude_project_directory`'s walk from *directory* **and** :func:`claude_session_project`, the same
    directory once. Kiro: :func:`kiro_project_directory`'s walk from *directory* **and** from the process's
    working directory, which Kiro sets to the project root. The second anchor keeps the session's project guarded
    when the payload's ``cwd`` follows a ``cd`` out of it or into a nested one.
    """
    found = _project_directories(host, directory)
    return [read_project(each) for each in found] or [GuardedProject(present=False)]


def _project_directories(host: str, directory: Path) -> list[Path]:
    """The project directories *host*'s dialect finds for *directory*; an empty list is no project."""
    if host == CODEX:
        found = codex_project_directory(directory)
        return [] if found is None else [found]
    if host == CLAUDE:
        candidates = [claude_project_directory(directory), claude_session_project()]
        return distinct_directories([candidate for candidate in candidates if candidate is not None])
    if host == KIRO:
        candidates = [kiro_project_directory(directory), kiro_project_directory(Path.cwd())]
        return distinct_directories([candidate for candidate in candidates if candidate is not None])
    return [directory] if manifest_present(directory) else []


def directory_identity(directory: Path) -> tuple[object, ...]:
    """What makes *directory* one folder on disk.

    Its ``(st_dev, st_ino)``, or -- when it cannot be stat'ed or the filesystem reports no file id -- its exact resolved
    spelling. Never a casefolded text: two folders whose names differ only by case are two projects on a case-sensitive
    filesystem."""
    try:
        status = directory.stat()
    except OSError:
        return ("path", str(directory))
    if not status.st_ino:  # a filesystem that reports no file id (FAT on Windows) identifies nothing by it
        return ("path", str(directory))
    return ("inode", status.st_dev, status.st_ino)


def distinct_directories(
    candidates: list[Path], identity: Callable[[Path], tuple[object, ...]] = directory_identity
) -> list[Path]:
    """*candidates* in order, each folder on disk once.

    A directory is dropped only when *identity* says an earlier one is the same folder on disk; a pair it cannot tell
    apart is judged twice, which is harmless (every deny wins)."""
    unique: dict[tuple[object, ...], Path] = {}
    for candidate in candidates:
        unique.setdefault(identity(candidate), candidate)
    return list(unique.values())


def codex_project_directory(directory: Path) -> Path | None:
    """The nearest directory holding both ``swcraft.yaml`` and :data:`CODEX_HOOK_FILE`, or ``None``.

    Walked from *directory* up to the filesystem root; ``None`` when there is none.

    Both entries are judged by the presence rule of :func:`manifest_present`.
    """
    return _project_directory(directory, (CODEX_HOOK_FILE,))


def claude_project_directory(directory: Path) -> Path | None:
    """The nearest directory holding both ``swcraft.yaml`` and one of :data:`CLAUDE_SETTINGS_FILES`, or ``None``.

    Walked from *directory* up to the filesystem root; ``None`` when there is none. Either settings file may declare the
    hook."""
    return _project_directory(directory, CLAUDE_SETTINGS_FILES)


def claude_session_project() -> Path | None:
    """The directory ``CLAUDE_PROJECT_DIR`` names when it holds a Claude Code project itself, or ``None``.

    It must hold ``swcraft.yaml`` and one of :data:`CLAUDE_SETTINGS_FILES` itself (no walk); ``None`` when the variable
    is unset or empty, or the directory holds no such project."""
    named = os.environ.get("CLAUDE_PROJECT_DIR")
    if not named:
        return None
    directory = _resolved_directory(Path(named))
    if manifest_present(directory) and any(_entry_present(directory / marker) for marker in CLAUDE_SETTINGS_FILES):
        return directory
    return None


def kiro_project_directory(directory: Path) -> Path | None:
    """The nearest directory holding both ``swcraft.yaml`` and an entry at :data:`KIRO_HOOKS_FOLDER`, or ``None``.

    Walked from *directory* up to the filesystem root; ``None`` when there is none. The folder, not a file in it, marks
    the project: a user may rename the hook file."""
    return _project_directory(directory, (KIRO_HOOKS_FOLDER,))


def _project_directory(directory: Path, markers: tuple[Path, ...]) -> Path | None:
    """The nearest directory, from *directory* upwards, holding ``swcraft.yaml`` and any of *markers*.

    Each entry is judged by the presence rule of :func:`manifest_present`."""
    start = _resolved_directory(directory)
    for candidate in (start, *start.parents):
        if manifest_present(candidate) and any(_entry_present(candidate / marker) for marker in markers):
            return candidate
    return None


def _entry_present(path: Path) -> bool:
    """:func:`manifest_present`'s rule for any path: only a genuinely missing entry is absent."""
    try:
        path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, ValueError):
        return True
    return True


def _resolved_directory(directory: Path) -> Path:
    """*directory* resolved as the manifest's roots are, for naming a file relative to it."""
    try:
        return Path(resolved_path(directory))
    except (OSError, ValueError):
        return directory


def is_saved_answer(resolved: str) -> bool:
    """Whether *resolved* is a saved Copilot answer: directly in the temporary directory, named as one."""
    candidate = Path(resolved)
    return bool(SAVED_ANSWER_NAME.fullmatch(candidate.name)) and str(candidate.parent) == resolved_path(
        Path(tempfile.gettempdir())
    )


def decide(
    payload: Mapping[str, object], working_directory: Path, guarded: GuardedProject, host: str = COPILOT
) -> Decision:
    """The hook's answer to one tool call in *host*'s dialect (``docs/guide/model-guard.md`` states the rule).

    *working_directory* is the fallback for a payload without ``cwd``; *guarded*
    is one of :func:`guarded_projects` of the payload's directory. Each call
    :func:`_tool_calls` reads from the payload is judged, and the first denial is
    the answer.
    """
    dialect = _JUDGED_BY_HOST[host]
    if host == KIRO and guarded.present and _kiro_envelope_malformed(payload):
        return Decision(allow=False, reason=_KIRO_ENVELOPE_REASON)
    for tool, arguments in _tool_calls(payload):
        decision = None
        if dialect is not None:
            is_judged, judge = dialect
            # Under Kiro a tool that cannot be named is the dialect's to judge (fail closed); elsewhere Copilot's.
            if is_judged(tool) if isinstance(tool, str) else host == KIRO:
                decision = judge(tool, arguments, payload, working_directory, guarded)
        if decision is None:
            decision = _decide_call(tool, arguments, payload, working_directory, guarded)
        if not decision.allow:
            return decision
    return Decision(allow=True)


def decide_all(
    payload: Mapping[str, object], working_directory: Path, projects: list[GuardedProject], host: str = COPILOT
) -> Decision:
    """:func:`decide` against each of *projects* (:func:`guarded_projects`), the first denial the answer."""
    for guarded in projects:
        decision = decide(payload, working_directory, guarded, host)
        if not decision.allow:
            return decision
    return Decision(allow=True)


def _decide_call(
    tool: object,
    arguments: Mapping[str, object] | None,
    payload: Mapping[str, object],
    working_directory: Path,
    guarded: GuardedProject,
) -> Decision:
    """The answer to one ``(tool name, arguments)`` reading under the Copilot tables; every other tool allowed."""
    if not isinstance(tool, str) or not _is_judged(tool):
        return Decision(allow=True)
    is_read = tool.casefold() in _READ_FOLDED
    return _decide_read_or_search(
        is_read,
        READ_PATH_KEYS if is_read else SEARCH_PATH_KEYS,
        _GLOB_KEYS_FOLDED.get(tool.casefold(), ()),
        arguments,
        payload,
        working_directory,
        guarded,
    )


def _decide_read_or_search(
    is_read: bool,
    path_keys: tuple[str, ...],
    glob_keys: tuple[str, ...],
    arguments: Mapping[str, object] | None,
    payload: Mapping[str, object],
    working_directory: Path,
    guarded: GuardedProject,
    *,
    narrow: bool = False,
    search_remedy: str = "Narrow the search to a directory outside the model, or ask the SWCraft tools.",
) -> Decision:
    """The read or search rule for a call of that kind, then :func:`_model_path_decision`.

    The rule is ``docs/guide/model-guard.md``, *Under Copilot*; the call's paths are under *path_keys* and its globs
    under *glob_keys*.

    With *narrow* (Kiro's search tools, which carry no root key) a search naming no root is rooted at its globs'
    literal leading directories (:func:`_search_roots`). *search_remedy* ends a search's denial."""
    if not guarded.present:
        return Decision(allow=True)
    directory = payload_directory(payload, working_directory)

    written = _written_paths(arguments or {}, path_keys)
    # An unjudgeable entry never reaches ``_resolve``: ``_judgeable_here`` takes the length of a string.
    targets = [None if path is None else _resolve(directory, path) for path in written]
    if is_read and targets and all(target is not None and is_saved_answer(target) for target in targets):
        return Decision(allow=True)

    if guarded.project is None:
        return Decision(
            allow=False,
            reason=(
                f"SWCraft: this project's swcraft.yaml cannot be read ({guarded.problem}), so its model directory "
                "is not known and every file read and search is refused until the manifest is fixed; a saved "
                "SWCraft answer may be viewed."
            ),
        )

    project = guarded.project
    model_root = project.resolved_model_root
    if arguments is None:
        return Decision(
            allow=False,
            reason=(
                f"{_model_reason(project)} This call's arguments could not be decoded, so its path cannot be judged."
            ),
        )
    if is_read:
        if any(target is None or path_contains(model_root.casefold(), target.casefold()) for target in targets):
            return Decision(allow=False, reason=_model_reason(project))
    elif any(
        root is None or paths_overlap(model_root, root)
        for root in _search_roots(targets, glob_keys, arguments, directory, narrow)
    ):
        return Decision(allow=False, reason=f"{_model_reason(project)} {search_remedy}")
    return _model_path_decision(project, directory, written, [arguments.get(key) for key in glob_keys])


def _model_path_decision(
    project: SwcraftProject,
    directory: Path,
    paths: Sequence[object],
    globs: Sequence[object] = (),
    *,
    end: float | None = None,
) -> Decision:
    """An allow, or the denial of a path or glob expansion that names the model folder or a model entry.

    A string of *paths*, or an expansion of a string of *globs*, is denied when it holds the model folder's path or ends
    like a model entry (``docs/guide/model-guard.md``, *The model's own names*); the work stops at the run's bound,
    lowered to the monotonic *end*, and refuses past it."""
    expanded = [expansion for glob in globs if isinstance(glob, str) for expansion in _brace_expansions(glob) or ()]
    if not expanded and not any(isinstance(path, str) for path in paths):
        return Decision(allow=True)
    roots = guard_model_paths.model_roots(
        lambda: project.resolved_project_root, lambda: project.resolved_model_root, end
    )
    if roots is None:
        return Decision(allow=False, reason=guard_model_paths.unreadable_tree_reason())
    root, model_root, name = roots
    runs, subject = guard_model_paths.model_runs(root, project.model_root, name), guard_model_paths.model_subject(name)
    prefixes = guard_model_paths.project_prefixes(root, directory, functools.partial(_resolve, directory), end)
    if guard_model_paths.names_model_folder(paths, expanded, runs, prefixes):
        return Decision(allow=False, reason=guard_model_paths.model_path_reason(subject))
    reason = guard_model_paths.model_tail_reason(paths, expanded, model_root, subject, end)
    return Decision(allow=reason is None, reason=reason)


def _search_roots(
    targets: list[str | None],
    glob_keys: tuple[str, ...],
    arguments: Mapping[str, object],
    directory: Path,
    narrow: bool,
) -> list[str | None]:
    """A search's roots, and each glob's literal leading directories resolved against each.

    The roots are *targets* (its root keys' paths resolved), else the payload directory. With *narrow* a search naming
    no root but a glob is rooted at its globs alone -- ``notes/**`` searches ``notes``, ``**/*.md`` the whole directory.
    """
    roots = targets or [_resolve(directory, ".")]
    globs = [
        glob
        for root in list(roots)
        if root is not None
        for glob in _glob_roots(glob_keys, arguments, Path(root), bare_root=narrow)
    ]
    if narrow and not targets and globs:
        return globs
    return roots + globs


#: The reason a Codex or Claude Code deny carries when its decision names none: Codex treats a deny without one
#: as a failed hook and runs the tool.
_FALLBACK_REASON = "SWCraft: the model guard refused this call; use the SWCraft tools."


def render(decision: Decision, host: str = COPILOT, folded: list[str] | None = None) -> str:
    """The decision as *host*'s hook reads it: ``{}`` to allow; to deny, the shape that host reads.

    Copilot's deny carries both decision shapes, top level and
    ``hookSpecificOutput``; Codex's and Claude Code's only ``hookSpecificOutput``,
    its reason never empty. Kiro reads no JSON: its allow is nothing at all, and
    its deny the one stderr line (:func:`kiro_reason_line`) with *folded* -- the
    unexpected failure's and the pin's notes -- folded into it; *folded* is
    Kiro's alone.
    """
    if host == KIRO:
        return "" if decision.allow else kiro_reason_line(decision.reason, folded)
    if decision.allow:
        return "{}"
    reason = decision.reason or ""
    if host in (CODEX, CLAUDE):
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason or _FALLBACK_REASON,
                }
            }
        )
    return json.dumps(
        {
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        }
    )


def run_hook(host: str = COPILOT) -> int:
    """``swcraft guard``'s whole run: read the payload from stdin, decide, answer in *host*'s dialect; the exit code.

    A process spawned with no stdin reads as no payload. A payload that is not a
    JSON object names no tool, so it is allowed with a note on stderr -- under
    Kiro it is denied when a project is guarded; JSON that cannot be parsed is an
    unexpected failure. A pin mismatch is the ``Warning:`` line on stderr and the
    decision stands. Anything unexpected is answered too, never with a traceback.

    The exit code is 0 for Copilot, Codex and Claude Code, a denial included, because their decision is on stdout.
    Under Kiro a denial is exit 2 with exactly one stderr line and nothing on stdout; an allow is exit 0 with
    nothing on stdout and the notes as their own stderr lines. Nothing here exits: the callers do.
    """
    payload: Mapping[str, object] | None = None
    directory: Path | None = None
    notes: list[str] = []
    folded: list[str] = []
    try:
        stream = getattr(sys.stdin, "buffer", None)
        payload = parse_payload(stream.read() if stream is not None else b"")
        if payload is None:
            decision = _kiro_malformed_decision() if host == KIRO else Decision(allow=True)
            if decision.allow:
                notes.append("Note: the hook payload is not a JSON object, so no tool is named; allowed.")
        else:
            directory = payload_directory(payload, Path.cwd())
            projects = guarded_projects(directory, host)
            for guarded in projects:
                if guarded.project is not None and (problem := toolchain_pin_problem(guarded.project)) is not None:
                    notes.append(f"Warning: {problem}")
                    folded.append(f"Warning: {problem}")
            decision = decide_all(payload, directory, projects, host)
    except Exception as exc:  # noqa: BLE001 -- hook: the hook answers whatever happens; a traceback is not an answer
        decision, note = _unexpected(payload, directory, exc, host)
        notes.append(note)
        folded.append(f"the guard failed unexpectedly ({type(exc).__name__})")
    if host == KIRO and not decision.allow:
        print(render(decision, host, folded), file=sys.stderr)
        return 2
    for note in notes:
        print(note, file=sys.stderr)
    if host != KIRO:
        print(render(decision, host))
    return 0


#: The line breaks a Kiro reason line may not hold: every separator ``str.splitlines`` knows, ``\\r`` and ``\\n``
#: among them. Each becomes a space, so the deny is exactly one stderr line.
_LINE_BREAKS = re.compile("[\r\n\v\f\x1c\x1d\x1e\x85  ]")


def kiro_reason_line(reason: str | None, folded: list[str] | None = None) -> str:
    """Kiro's deny: one stderr line, never empty.

    *reason* (:data:`_FALLBACK_REASON` when it has none) with each of *folded* (the unexpected failure's and the pin's
    notes) appended after a semicolon, every line break a space."""
    line = (reason or "").strip() or _FALLBACK_REASON
    for note in folded or []:
        line = f"{line.rstrip('.')}; {note.rstrip('.')}."
    return _LINE_BREAKS.sub(" ", line).strip() or _FALLBACK_REASON


def _unexpected(
    payload: Mapping[str, object] | None, directory: Path | None, exc: Exception, host: str = COPILOT
) -> tuple[Decision, str]:
    """The answer to a run that failed unexpectedly, and the stderr note naming the answer it gives."""
    decision = _unexpected_decision(payload, directory, host)
    judged = _UNEXPECTED_JUDGED[host][0]
    outcome = (
        f"this call is allowed: it is not {judged} in a SWCraft project"
        if decision.allow
        else f"this call is denied: {judged}, or a call whose tool cannot be named, in a SWCraft project"
    )
    return decision, f"Note: the guard failed unexpectedly ({type(exc).__name__}); {outcome}."


#: Per host, what an unexpected failure denies in a project, and the remedy its reason gives.
_UNEXPECTED_JUDGED = {
    COPILOT: ("a file read or search", "ask the SWCraft tools"),
    CODEX: ("a file read or search, an edit or a shell command", "use the SWCraft tools"),
    CLAUDE: ("a file read, search or edit, or a shell command", "use the SWCraft tools"),
    KIRO: ("a file read, search or edit, a shell command or a tool the guard does not know", "use the SWCraft tools"),
}


def _unexpected_decision(payload: Mapping[str, object] | None, directory: Path | None, host: str = COPILOT) -> Decision:
    """Deny a judged tool, or a tool that cannot be named, in a guarded project; allow the rest.

    The directory is the one the run found, else the payload's ``cwd``, else the process's working directory. Under
    Copilot the judged tools are the read and search tools, under Codex a patch or shell tool too, under Claude Code
    a write, shell or ``PowerShell`` tool too, and under Kiro every tool but :data:`KIRO_ALLOWED_TOOLS`'. Under Kiro
    a failure to find the projects cannot rule a project out, so it denies.
    """
    dialect = _JUDGED_BY_HOST[host]
    try:
        if directory is None:
            directory = payload_directory(payload, Path.cwd()) if payload is not None else Path.cwd()
        present = bool(_project_directories(host, directory))
    except Exception:  # noqa: BLE001 -- hook: no directory can be named, so no project is known -- under Kiro, none ruled out
        if host != KIRO:
            return Decision(allow=True)
        present = True
    if not present:
        return Decision(allow=True)
    try:
        tools = _tool_names(payload) if payload is not None else [None]
    except Exception:  # noqa: BLE001 -- hook: a tool that cannot be named is judged as a read
        tools = [None]
    if all(
        isinstance(tool, str) and not _is_judged(tool) and not (dialect is not None and dialect[0](tool))
        for tool in tools
    ):
        return Decision(allow=True)
    judged, remedy = _UNEXPECTED_JUDGED[host]
    return Decision(
        allow=False,
        reason=(
            f"SWCraft: the model guard could not decide this call, so {judged} in this project is refused; {remedy}."
        ),
    )


def _is_judged(tool: str) -> bool:
    """Whether *tool* names a read or search tool: in either table, compared casefolded."""
    folded = tool.casefold()
    return folded in _READ_FOLDED or folded in _SEARCH_FOLDED


def _tool_names(payload: Mapping[str, object]) -> list[object]:
    """Every non-null tool name under ``tool_name`` and ``toolName``, ``[None]`` when neither carries one."""
    return _present(payload, "tool_name", "toolName")


def _present(payload: Mapping[str, object], *keys: str) -> list[object]:
    """The distinct non-null values under *keys*, in their order; ``[None]`` when there is none.

    A null under one key never hides the other key's value.
    """
    values: list[object] = []
    for key in keys:
        value = payload.get(key)
        if value is not None and value not in values:
            values.append(value)
    return values or [None]


def _tool_calls(payload: Mapping[str, object]) -> list[tuple[object, Mapping[str, object] | None]]:
    """Every ``(tool name, arguments)`` the payload can be read as, from both key shapes.

    A null under one shape never hides the other. When both carry a value and they
    differ, the payload is read as every pairing of a name with an argument set, each
    judged: which shape the host acts on is not known. ``toolArgs`` may be an object or a
    JSON string. No arguments at all are ``{}``; arguments that arrive but are not a JSON
    object are ``None``, which :func:`decide` denies for a read or search tool in a guarded
    project, so an undecodable payload is no way past the guard.
    """
    argument_sets = [_decoded(arguments) for arguments in _present(payload, "tool_input", "toolArgs")]
    return [(tool, arguments) for tool in _tool_names(payload) for arguments in argument_sets]


#: Kiro's denial of a call whose argument envelope is not a JSON object (:func:`_kiro_envelope_malformed`).
_KIRO_ENVELOPE_REASON = (
    "SWCraft: this call's arguments are not a JSON object, so what it reaches cannot be judged and it is refused "
    "in this project; use the SWCraft tools."
)


def _kiro_envelope_malformed(payload: Mapping[str, object]) -> bool:
    """Whether a Kiro payload carries ``tool_input`` or ``toolArgs`` as anything but a JSON object.

    A string (never decoded under Kiro), a list, a number, a boolean or an explicit null is malformed; an absent key is
    no arguments."""
    return any(key in payload and not isinstance(payload[key], dict) for key in ("tool_input", "toolArgs"))


def _decoded(arguments: object) -> Mapping[str, object] | None:
    """One argument value as a mapping: ``{}`` for none, ``None`` when it is not a JSON object."""
    if arguments is None:
        return {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, RecursionError):  # not JSON, or JSON the interpreter cannot parse
            return None
    return arguments if isinstance(arguments, dict) else None


def _written_paths(arguments: Mapping[str, object], keys: tuple[str, ...]) -> list[str | None]:
    """Every path written under *keys*, in their order: each is judged, never only the first.

    A string is one path, the empty string none; a list is its non-empty string
    members; ``None`` or an absent key is none. Any other value, and a list member
    that is not a string, is one ``None``: a path that cannot be judged.
    """
    written: list[str | None] = []
    for key in keys:
        value = arguments.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            if value:
                written.append(value)
        elif isinstance(value, list):
            written.extend(member if isinstance(member, str) else None for member in value if member != "")
        else:
            written.append(None)
    return written


def _glob_roots(
    keys: tuple[str, ...], arguments: Mapping[str, object], root: Path, bare_root: bool = False
) -> list[str | None]:
    """Each glob's literal leading directories resolved against *root*, ``None`` where that fails.

    A glob that is not a string, holds a backslash or is longer than
    :data:`_MAX_GLOB_LENGTH` is ``None``. Each brace expansion's literal leading
    directories are those before the first component holding a
    :data:`kiro_paths.GLOB_MAGIC` character; an expansion with none adds nothing
    (it searches below *root*), and one with a later component that can climb is
    ``None``.

    With *bare_root* (the Kiro dialect) an expansion with no literal leading
    component adds *root* itself, and an expansion is read as ripgrep's ``--glob``
    reads it: one with no slash, or only a trailing one, matches at any depth, so
    it adds *root* itself; one with a leading slash is read both as written and
    with the slash removed.
    """
    resolved: list[str | None] = []
    for key in keys:
        value = arguments.get(key)
        if value in (None, "", []):
            continue
        unjudgeable = not isinstance(value, str) or "\\" in value or len(value) > _MAX_GLOB_LENGTH
        expansions = None if unjudgeable else _brace_expansions(str(value))
        if expansions is None:
            resolved.append(None)
            continue
        for expansion in expansions:
            if not _judgeable_here(expansion):
                resolved.append(None)
                continue
            if bare_root and "/" not in expansion.rstrip("/"):
                # ripgrep's ``--glob`` (gitignore): no slash but a trailing one matches at any depth.
                resolved.append(_resolve(root, "."))
                continue
            readings = [expansion]
            if bare_root and expansion.startswith("/"):
                # ripgrep anchors a leading slash at the search root; the absolute reading is kept as well.
                readings.append(expansion.lstrip("/"))
            for reading in readings:
                resolved.extend(_glob_root(Path(reading), root, bare_root))
    return resolved


def _glob_root(path: Path, root: Path, bare_root: bool) -> list[str | None]:
    """One glob expansion's root for :func:`_glob_roots`.

    Its literal leading directories resolved against *root*, ``None`` where a later component can climb, *root* itself
    with *bare_root* when nothing is literal; else none."""
    literal = _literal_prefix(path, kiro_paths.GLOB_MAGIC)
    if any(_can_climb(part) for part in path.parts[len(literal) :]):
        return [None]
    if literal:
        return [_resolve(root, str(Path(*literal)))]
    return [_resolve(root, ".")] if bare_root else []


def _literal_prefix(path: Path, magic: frozenset[str]) -> list[str]:
    """*path*'s literal leading components.

    Its anchor, never a glob component (Windows spells it with a backslash, ``C:\\``, ``\\``), and every component
    before the first holding a *magic* character."""
    parts = path.parts
    literal: list[str] = list(parts[:1]) if path.anchor else []
    for part in parts[len(literal) :]:
        if magic & set(part):
            break
        literal.append(part)
    return literal


def _brace_expansions(
    glob: str, sequences: bool = False, spend: Callable[[int], None] | None = None
) -> list[str] | None:
    """*glob* with every brace group expanded, nested ones included; ``None`` past :data:`_MAX_EXPANSIONS`.

    Every balanced ``{...}`` is expanded, a one-alternative group included:
    expanding more than a host does only judges more. An unbalanced brace stays
    as written. With *sequences* (the shell judge alone) a sequence expression is
    expanded to its terms (:func:`_brace_sequence`). With *spend* every rescan is
    charged by the characters it reads (:data:`_BRACE_SCAN_UNIT` per reading, one
    at least).
    """
    pending = [glob]
    done: list[str] = []
    while pending:
        current = pending.pop()
        if spend is not None:
            spend(1 + len(current) // _BRACE_SCAN_UNIT)
        group = _first_brace_group(current, sequences)
        if group is None:
            done.append(current)
        else:
            start, end, alternatives = group
            pending.extend(current[:start] + alternative + current[end + 1 :] for alternative in alternatives)
        if len(done) + len(pending) > _MAX_EXPANSIONS:
            return None
    return done


def _first_brace_group(glob: str, sequences: bool = False) -> tuple[int, int, list[str]] | None:
    """The balanced ``{...}`` that opens first: its start, its end and its top-level alternatives.

    The cost is linear in the glob's length. With *sequences*, a group that is a
    sequence expression gives its terms as the alternatives.
    """
    openers: list[int] = []
    group: tuple[int, int] | None = None
    for index, character in enumerate(glob):
        if character == "{":
            openers.append(index)
        elif character == "}" and openers:
            start = openers.pop()
            if group is None or start < group[0]:
                group = (start, index)
    if group is None:
        return None
    start, end = group
    alternatives: list[str] = []
    depth = 0
    piece = start + 1
    for index in range(start + 1, end):
        if glob[index] == "{":
            depth += 1
        elif glob[index] == "}":
            depth -= 1
        elif glob[index] == "," and depth == 0:
            alternatives.append(glob[piece:index])
            piece = index + 1
    alternatives.append(glob[piece:end])
    if sequences and len(alternatives) == 1 and (sequence := _brace_sequence(alternatives[0])) is not None:
        alternatives = sequence
    return start, end, alternatives


def _can_climb(component: str) -> bool:
    """Whether one glob component could match ``..``, judged conservatively by its structure.

    ``..`` itself can; :data:`_NEVER_CLIMBS` cannot; a component without a
    :data:`kiro_paths.GLOB_MAGIC` character is literal and is only itself. Otherwise it can
    unless a literal character other than ``.`` stands outside its classes and
    groups -- everything from its first ``[`` or ``(`` to its last ``]`` or ``)``
    is taken as a class or group, the widest reading -- because every engine
    makes such a character appear in the match, and ``..`` holds none.
    """
    if component == "..":
        return True
    if component in _NEVER_CLIMBS or not (kiro_paths.GLOB_MAGIC & set(component)):
        return False
    return not (set(_literal_characters(component)) - {"."})


def _literal_characters(component: str) -> str:
    """The characters of *component* outside its classes and groups that are not glob syntax."""
    openers = [index for index in (component.find("["), component.find("(")) if index != -1]
    if openers:
        start = min(openers)
        end = max(component.rfind("]"), component.rfind(")"))
        component = component[:start] + (component[end + 1 :] if end > start else "")
    return "".join(character for character in component if character not in kiro_paths.GLOB_MAGIC)


def _two_leading_separators(text: str, separators: str = "/\\") -> bool:
    """Whether *text* opens with two characters of *separators* (``//x``, ``\\\\x``, ``/\\x``)."""
    return len(text) >= 2 and text[0] in separators and text[1] in separators


def _judgeable_here(written: str) -> bool:
    """Whether *written* names one place this host's resolution can judge, read by both platforms' rules.

    A path that opens with two separators (UNC ``//server/share``, a device path ``//?/C:`` or ``//./C:``, POSIX's
    ``//``) can alias any directory and is never judgeable; a drive is judgeable only as one letter with a colon
    followed by a root (``C:/x``, ``c:\\x``), never drive-relative (``C:x``), and only where the native path reads the
    same drive. Anything else has no drive and resolves natively.
    """
    if _two_leading_separators(written):
        return False
    drive = PureWindowsPath(written).drive
    if not drive:
        return True
    return (
        _DRIVE_LETTER.fullmatch(drive) is not None
        and bool(PureWindowsPath(written).root)
        and Path(written).drive.casefold() == drive.casefold()
    )


def _resolve(directory: Path, written: str) -> str | None:
    """*written* resolved against *directory* as the manifest's roots are, or ``None`` when it cannot be.

    ``None`` too when it is not :func:`_judgeable_here`, which every caller denies in a guarded project.
    """
    if not _judgeable_here(written):
        return None
    try:
        return resolved_path(directory / written)
    except (OSError, ValueError, RuntimeError):
        return None


#: The readings of one written path (:func:`tilde_paths.tilde_forms`, :func:`_kiro_forms`): given the key it arrived
#: under -- ``None`` for a context file's ``path`` -- and the value, its readings, the literal one first, or ``None``
#: when it cannot be judged.
PathForms = Callable[[str | None, str], tuple[str, ...] | None]


def _tilde_readings(
    arguments: Mapping[str, object],
    keys: tuple[str, ...],
    context_file_keys: tuple[str, ...] = (),
    forms: PathForms | None = None,
    glob_keys: tuple[str, ...] = (),
) -> list[Mapping[str, object]] | None:
    """*arguments* under each reading of their paths, or ``None`` when a value cannot be read.

    By default each leading ``~`` is read (:func:`tilde_paths.tilde_forms`), else *forms*: ``[arguments]`` unchanged
    when no value under *keys* or a context file's ``path`` has a second reading; else argument sets, the literal one
    first, each to be judged, a denial from any winning. Each reading of the *glob_keys* values is paired with each
    reading of the others; every other value takes its *i*-th reading in the *i*-th set. ``None`` when a value cannot be
    read (a ``~name`` form, an unknown home)."""
    if globs := tuple(key for key in keys if key in glob_keys):
        paths = _tilde_readings(arguments, tuple(key for key in keys if key not in globs), context_file_keys, forms)
        patterns = _tilde_readings(arguments, globs, (), forms)
        if paths is None or patterns is None:
            return None
        return [
            {**path, **{key: pattern[key] for key in globs if key in pattern}} for path in paths for pattern in patterns
        ]
    reader: PathForms = forms if forms is not None else (lambda _key, value: tilde_paths.tilde_forms(value))
    strings: list[tuple[str | None, str]] = [
        (key, value) for key in keys for value in tilde_paths.tilde_strings(arguments, (key,), ())
    ]
    strings += [(None, value) for value in tilde_paths.tilde_strings(arguments, (), context_file_keys)]
    width = 1
    for key, string in strings:
        string_readings = reader(key, string)
        if string_readings is None:
            return None
        width = max(width, len(string_readings))
    if width == 1:
        return [arguments]

    def pick(value: object, index: int, key: str | None) -> object:
        if not isinstance(value, str):
            return value
        readings = reader(key, value) or (value,)
        return readings[min(index, len(readings) - 1)]

    readings: list[Mapping[str, object]] = []
    for index in range(width):
        reading = dict(arguments)
        for key in keys:
            value = arguments.get(key)
            if isinstance(value, list):
                reading[key] = [pick(member, index, key) for member in value]
            elif isinstance(value, str):
                reading[key] = pick(value, index, key)
        for key in context_file_keys:
            value = arguments.get(key)
            if isinstance(value, list):
                reading[key] = [
                    {**member, "path": pick(member.get("path"), index, None)} if isinstance(member, dict) else member
                    for member in value
                ]
        readings.append(reading)
    return readings


def _first_denial(decisions: Iterable[Decision]) -> Decision:
    """The first denial of *decisions*, judged in order and no further; an allow when none denies."""
    for decision in decisions:
        if not decision.allow:
            return decision
    return Decision(allow=True)


def _model_reason(project: SwcraftProject) -> str:
    """The denial sentence, naming the model root relative to the project directory and never in full."""
    name = _model_name(project)
    subject = f"the model under `{name}`" if name is not None else "the model directory"
    return (
        f"SWCraft: {subject} is read through the SWCraft tools, never by file tools; a saved SWCraft answer may "
        "be viewed."
    )


# ---------------------------------------------------------------------------
# The Codex dialect's two judges
# ---------------------------------------------------------------------------

#: The file Codex reads a repository layer's hooks from; with ``swcraft.yaml``
#: beside its directory it marks the project a Codex hook guards.
CODEX_HOOK_FILE = Path(".codex") / "hooks.json"

#: The tools judged as a patch under ``--host codex``: ``apply_patch`` and Codex's two matcher aliases. Matched
#: casefolded.
CODEX_PATCH_TOOLS = frozenset({"apply_patch", "Edit", "Write"})

CODEX_SHELL_TOOLS = frozenset({"Bash"})

_CODEX_PATCH_FOLDED = frozenset(tool.casefold() for tool in CODEX_PATCH_TOOLS)
_CODEX_SHELL_FOLDED = frozenset(tool.casefold() for tool in CODEX_SHELL_TOOLS)

_PATCH_PATH_HEADERS = ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: ")

#: What Rust's ``str::trim`` strips, which Codex's patch parser trims each line with: Unicode's
#: ``White_Space``. Python's ``str.strip()`` strips four more (``U+001C`` to ``U+001F``), which a path keeps.
_PATCH_WHITESPACE = (
    "\t\n\x0b\x0c\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)

#: A header line; a line ends at ``\n`` alone (``re.MULTILINE``'s ``^`` follows ``\n`` only), as Codex splits.
_PATCH_HEADER = re.compile(
    "^["
    + re.escape(_PATCH_WHITESPACE.replace("\n", ""))
    + "]*(?:"
    + "|".join(re.escape(header.rstrip()) for header in _PATCH_PATH_HEADERS)
    + ")([^\n]*)",
    re.MULTILINE,
)

#: The characters that make a shell word a glob (with a brace group): bash's default wildcards. A backslash,
#: ``!``, ``(`` or ``+`` is not one, so a regular expression handed to ``grep`` is judged as a plain word.
_SHELL_GLOB = frozenset("*?[")

#: How many times a word holding whitespace, a quote or an operator character is tokenised again; past it the
#: word is denied unjudged.
_MAX_SHELL_DEPTH = 4

_MAX_SHELL_TEXT = 65536

#: The most path readings one shell command is judged by -- every word part
#: counts one; every brace scan one per :data:`_BRACE_SCAN_UNIT` characters it
#: reads, one at least; every brace expansion, and every ``sed`` write file, one
#: per path component; and every glob component one per reading it is matched
#: in; past it the command is denied with the budget's reason, so the hook
#: answers inside Codex's timeout (a tool runs when the hook times out).
_MAX_SHELL_READINGS = 8192

#: The most directory entries one glob's real matches are read from (:meth:`_ShellJudge._matches`); past it
#: the command is denied with the budget's reason.
_MAX_GLOB_ENTRIES = 1024

#: The most entries of each guard-input folder (:data:`_GUARD_INPUT_FOLDERS`) read for the links whose targets are
#: guard inputs (:func:`_guard_inputs`); past it every patch, edit and shell command is denied.
_MAX_GUARD_FOLDER_ENTRIES = 1024

#: The guard's own inputs beside ``swcraft.yaml``, one set for every write-judging dialect, as names in the project
#: directory: each client's hook and server files and Copilot's hook folder, so one client cannot disarm another's
#: guard.
_SHARED_GUARD_INPUT_FILES = (Path(".mcp.json"),)

#: The guard-input folders whose direct entries that are links have their targets taken as guard inputs too: a
#: client reads what a link there points at. Kiro's three hold its hook, its server file and its custom agents; not
#: ``.kiro/steering`` (instructions) and not ``.kiro/specs`` (Kiro's spec mode writes there in ordinary work).
_GUARD_INPUT_FOLDERS = (
    Path(".codex"),
    Path(".claude"),
    Path(".github") / "hooks",
    Path(".kiro") / "hooks",
    Path(".kiro") / "settings",
    Path(".kiro") / "agents",
)

_SHARED_GUARD_INPUTS = (*_GUARD_INPUT_FOLDERS, *_SHARED_GUARD_INPUT_FILES)

#: The most header lines one patch is judged by; past it the patch is denied with the budget's reason.
_MAX_PATCH_HEADERS = 1024

#: The wall-clock bound of one Codex judgement, a shell command's or a patch's (:class:`_Deadline`), in seconds;
#: past it the call is denied with the budget's reason. The template hook's timeout is 10 s, after which Codex runs
#: the tool, and start-up and an operation already begun take part of it, so this keeps well under a third. It
#: bounds what no counter sees: a resolution following a link chain stats every component of every target.
_JUDGE_SECONDS = 3.0

#: The error a status read gives for a name past the kernel's link limit: ``ELOOP`` on POSIX, and on Windows
#: ``ERROR_CANT_RESOLVE_FILENAME`` (1921) for too many reparse points.
_TOO_MANY_LINKS_WINERROR = 1921

#: What a word is split at into parts, each judged as a word too (:func:`_word_parts`); a ``:`` splits as well.
_WORD_PART_SEPARATORS = re.compile(r"[=,`]")

#: Either path separator, which a command word's last component follows (:func:`_command_name`).
_PATH_SEPARATOR = re.compile(r"[/\\]")

#: What splits a nested word that cannot be tokenised: no character here is part of a path the shell judge reads.
_NOT_A_PATH = re.compile(r"[\s'\"`;&|()<>=,:]+")

#: Whether a backslash in a shell text is also read as a path separator: on
#: Windows, where Codex's commands are not bash and ``model\\x`` names a path.
_BACKSLASH_SEPARATES = os.name == "nt"

#: Whether a shell word opening with two separators may be a UNC or device path (``//host/share``), which
#: can alias any directory, so it is denied unjudged: on Windows. On POSIX the kernel reads a leading
#: ``//`` (or more) as ``/`` -- Linux and macOS resolve them the same -- so the word is judged as that path.
_DOUBLE_SEPARATOR_IS_UNC = os.name == "nt"

#: Whether a shell word may carry a drive (``C:/x``, the drive-relative ``C:x``): on Windows, where one no
#: resolution can judge is denied unjudged. On POSIX bash knows no drives: ``C:x`` is a relative name.
_SHELL_WORDS_HAVE_DRIVES = os.name == "nt"

#: The commands that run nothing and write nothing -- the only ones a command may
#: name the guard's own inputs with (``cat swcraft.yaml``), beside ``swcraft``
#: with a subcommand of :data:`_SWCRAFT_READS`; :func:`_reads_only` makes the
#: exceptions (``less``/``more`` with an option, ``file -C``). Matched by the bare
#: command word.
_READER_COMMANDS = frozenset({"cat", "file", "grep", "head", "less", "ls", "more", "stat", "tail", "wc"})

#: What opens a command or process substitution inside a word; a command holding one is no reader.
_SUBSTITUTIONS = ("`", "$(", "<(", ">(")

#: The ``swcraft`` subcommands taken to read only, with no ``-o`` / ``--o...`` option word
#: (:data:`_REPORT_WRITING_PREFIXES` adds the report commands' own). ``validate`` and ``generate`` write the build
#: outputs the manifest names, and a valid manifest can name ``.codex/hooks.json`` as one; a build of the guarded
#: manifest is judged by those outputs (:data:`_SWCRAFT_BUILDS`).
_SWCRAFT_READS = frozenset({"check-model", "diff", "impact", "list", "metrics", "query", "resource-report"})

#: The word prefixes of the output options, which make every ``swcraft`` reader write.
_OUTPUT_OPTION_PREFIXES = ("-o", "--o")

#: The readers where more than the output options make a write: the report commands, whose ``--report`` writes
#: under ``dist_root``.
_SWCRAFT_REPORT_COMMANDS = frozenset({"metrics", "resource-report"})

#: The word prefixes that make a report command (:data:`_SWCRAFT_REPORT_COMMANDS`) write: ``--report`` and every
#: abbreviation argparse expands down to ``--r``. Only these commands: ``query --request`` starts alike and reads
#: a file. A word the prefix catches and that is no such option makes no reader either, which fails closed.
_REPORT_WRITING_PREFIXES = (*_OUTPUT_OPTION_PREFIXES, "--r")

#: The ``swcraft`` subcommands that build from a manifest, each with its positional choices (``None``: it
#: takes none), its options that take no value and those that take one -- the whole of what a build of the
#: guarded manifest may carry and still write only the outputs that manifest names. ``--output``,
#: ``--output-dir``, ``--all-components``, any abbreviation argparse would expand, and every other option are
#: not listed, so a build carrying one is no build of the manifest's outputs.
_SWCRAFT_BUILDS: dict[str, tuple[frozenset[str] | None, frozenset[str], frozenset[str]]] = {
    "validate": (None, frozenset({"--warnings-as-errors"}), frozenset({"--project"})),
    "generate": (
        frozenset({"arxml", "diagrams", "needs"}),
        frozenset(),
        frozenset({"--project", "--component", "--arxml-version"}),
    ),
}

#: A ``--component`` value a build may name: a component name, which the default output path
#: ``<dist_root>/components/<name>.arxml`` joins below ``dist_root`` -- never a path.
_COMPONENT_NAME = re.compile(r"[A-Za-z0-9_]+")

#: An ``--arxml-version`` value a build may name: a release number, which argparse then checks.
_RELEASE_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)*")

#: The variable a build may be preceded by, set to the guarded manifest; any other assignment makes none.
_MANIFEST_VARIABLE = "SWCRAFT_MANIFEST"

_SHELL_KEYWORDS = frozenset({"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "time"})

#: The builtins that change what a command name runs, or run text the guard reads only as words: a text
#: running one, or defining a function, makes no command a reader (:func:`_renames_commands`).
_RENAMING_BUILTINS = frozenset({".", "alias", "enable", "eval", "hash", "source", "unalias"})

#: How many guard inputs one path is compared with per path reading charged: a ``.codex`` folder of links
#: makes the list long.
_INPUTS_PER_READING = 16

_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)

#: The shell operators: one token of these characters is an operator, never a word.
_SHELL_PUNCTUATION = "();<>|&\n"

#: The shell judge's verdicts; :data:`_BUILD_OUTPUT` is a build of the guarded manifest whose outputs reach a
#: guard input or the model.
_MODEL, _GUARD_INPUT, _UNJUDGED, _OVER_BUDGET, _BUILD_OUTPUT = "model", "guard", "unjudged", "budget", "build"
#: The verdict on a word naming a folder strictly between the project directory and the model root outside a
#: read: removing or moving it removes or moves the model.
_MODEL_FOLDER = "model-folder"

#: The shell judge's verdict on a word reaching the review hand-off directory (the Claude Code dialect alone).
_HANDOFF = "handoff"
#: The shell judge's verdict on a word outside a read reaching the SWCraft home, or naming a folder above it (the
#: Claude Code and Kiro dialects).
_HOME = "home"


#: The most brace groups one shell word part is expanded with; past it the part is denied by the budget.
#: Each group costs one rescan of the part per expansion that holds it.
_MAX_BRACE_GROUPS = 64

#: The characters of a brace scan the shell judge charges as one path reading (:func:`_brace_expansions`).
_BRACE_SCAN_UNIT = 256


def _brace_group_count(text: str) -> int:
    """How many balanced ``{...}`` groups *text* holds, matched as :func:`_first_brace_group` matches them."""
    if "{" not in text or "}" not in text:
        return 0
    count = opened = 0
    for character in text:
        if character == "{":
            opened += 1
        elif character == "}" and opened:
            opened -= 1
            count += 1
    return count


#: A brace group that is a sequence expression: two integers, or two letters as bash requires, and an optional
#: integer increment (``{1..3}``, ``{01..10}``, ``{a..z..2}``).
_BRACE_SEQUENCE = re.compile(r"(?:([-+]?[0-9]+)\.\.([-+]?[0-9]+)|([A-Za-z])\.\.([A-Za-z]))(?:\.\.([-+]?[0-9]+))?")


#: An integer end of a sequence expression that asks for zero-padding, as bash reads one (``01``, ``-01``).
_ZERO_LED = re.compile(r"-?0[0-9]")


def _brace_sequence(body: str) -> list[str] | None:
    """The terms of a sequence expression *body* (``1..3``, ``a..e..2``) as bash makes them, else ``None``.

    As bash: the increment's sign is ignored and ``0`` reads as ``1``, the
    direction comes from the ends, and when either integer end opens with ``0``
    (``01``, ``-01``) every term is zero-padded to the longest such end's width.
    At most ``_MAX_EXPANSIONS + 1`` terms are made, which the caller's bound then
    refuses.
    """
    match = _BRACE_SEQUENCE.fullmatch(body)
    if match is None:
        return None
    first, last, first_character, last_character, increment = match.groups()
    step = max(abs(int(increment)), 1) if increment is not None else 1
    if first is not None:
        low, high = int(first), int(last)
        terms = range(low, high + 1, step) if low <= high else range(low, high - 1, -step)
        width = max((len(end) for end in (first, last) if _ZERO_LED.match(end)), default=0)
        return [f"{term:0{width}d}" for term in terms[: _MAX_EXPANSIONS + 1]]
    low, high = ord(first_character), ord(last_character)
    codes = range(low, high + 1, step) if low <= high else range(low, high - 1, -step)
    return [chr(code) for code in codes[: _MAX_EXPANSIONS + 1]]


def _is_codex_judged(tool: str) -> bool:
    """Whether *tool* is one of the Codex dialect's patch or shell tools, compared casefolded."""
    folded = tool.casefold()
    return folded in _CODEX_PATCH_FOLDED or folded in _CODEX_SHELL_FOLDED


def _decide_codex_call(
    tool: str,
    arguments: Mapping[str, object] | None,
    payload: Mapping[str, object],
    working_directory: Path,
    guarded: GuardedProject,
) -> Decision:
    """The Codex answer to one patch or shell call (``docs/guide/model-guard.md``, *Under Codex*)."""
    if not guarded.present:
        return Decision(allow=True)
    directory = payload_directory(payload, working_directory)
    is_patch = tool.casefold() in _CODEX_PATCH_FOLDED
    given = arguments or {}
    command, workdir = given.get("command"), given.get("workdir")
    if guarded.project is None:
        return Decision(allow=False, reason=_unreadable_manifest_reason(guarded))
    project = guarded.project
    if arguments is None:
        return Decision(allow=False, reason=_unjudged_reason(project, is_patch))
    protected = _guard_inputs(guarded.manifest)
    if protected is None:
        return Decision(allow=False, reason=_guard_folder_reason())
    if is_patch:
        return _decide_patch(command, workdir, directory, project, protected)
    return _decide_shell(command, workdir, directory, project, protected)


def _guard_inputs(manifest: Path | None) -> tuple[str, ...] | None:
    """The guard's own inputs: ``swcraft.yaml`` and :data:`_SHARED_GUARD_INPUTS`, resolved and as named.

    Each is the project's ``swcraft.yaml`` or one of :data:`_SHARED_GUARD_INPUTS`, both resolved and as named
    (:func:`_lexical`) in the resolved project directory.

    A whole folder is one (removing it, or a ``config.toml`` in it, disarms the hook), kept as named too, because a
    symbolic link inside it resolves out of it. A path is a hit when either of its forms reaches either form. The
    resolved target of every entry directly inside a folder of :data:`_GUARD_INPUT_FOLDERS` that is a symbolic link
    is one too: the client runs what it points at. Past :data:`_MAX_GUARD_FOLDER_ENTRIES` entries, or when a link
    or the folder cannot be read, the answer is ``None``, which denies the call. A link deeper inside a folder
    (``.codex/sub/x``) is not read.
    """
    if manifest is None:
        return ()
    shared = tuple(str(name) for name in _SHARED_GUARD_INPUTS)
    names = (manifest.name, *shared)
    directory = _resolve(manifest.parent, ".")
    inputs = [_resolve(manifest.parent, name) for name in names]
    if directory is not None:
        inputs.extend(_lexical(directory, name) for name in names)
    for folder in _GUARD_INPUT_FOLDERS:
        try:
            with os.scandir(manifest.parent / folder) as iterator:
                for count, entry in enumerate(iterator, start=1):
                    if count > _MAX_GUARD_FOLDER_ENTRIES:
                        return None
                    if entry.is_symlink():
                        try:
                            inputs.append(resolved_path(Path(entry.path)))
                        except (OSError, ValueError, RuntimeError):
                            return None
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            return None
    return tuple(dict.fromkeys(path for path in inputs if path is not None))


def _lexical(base: str, written: str) -> str:
    """*written* joined to the resolved *base* with ``.`` and ``..`` collapsed by name, no symbolic link followed."""
    return os.path.normpath(os.path.join(base, written))


def _guard_input_ancestors(protected: tuple[str, ...], project_root: str | None) -> frozenset[str]:
    """The folders strictly between the resolved *project_root* and a guard input of *protected*, casefolded.

    ``.github`` for ``.github/hooks``, ``.kiro`` for Kiro's three, derived from the inputs' paths, never listed. A write
    path, a patch path or a non-reader shell word naming one exactly is judged a guard input's. The project directory
    itself, every folder above it and every folder outside the project are not among them."""
    if project_root is None:
        return frozenset()
    root = project_root.casefold()
    found: set[str] = set()
    for target in protected:
        folded = target.casefold()
        if folded == root or not path_contains(root, folded):
            continue
        parent = os.path.dirname(folded)
        while parent != root and path_contains(root, parent) and os.path.dirname(parent) != parent:
            found.add(parent)
            parent = os.path.dirname(parent)
    return frozenset(found)


def _model_ancestors(project: SwcraftProject) -> frozenset[str]:
    """The folders strictly between the resolved project directory and the model root, casefolded.

    Derived from the model root resolved and as named: ``arch`` for ``model_root: arch/model``. A write path, a patch
    path or a non-reader shell word naming one exactly is denied. Empty for a model root outside the project."""
    root = project.resolved_project_root
    named = _lexical(root, project.model_root)
    return _guard_input_ancestors((project.resolved_model_root, named), root)


def _decide_patch(
    command: object, workdir: object, directory: Path, project: SwcraftProject, protected: tuple[str, ...]
) -> Decision:
    """Deny a patch any of whose paths reaches the model, a guard input or a folder holding either.

    Denied when any path resolves to the model root or inside it, to one of the guard's own inputs or a folder strictly
    between the project directory and either (:func:`_guard_input_ancestors`, :func:`_model_ancestors`), cannot be
    judged, or holds the model folder's path. Header paths resolve against the call's base (:func:`_shell_base`). Past
    :data:`_MAX_PATCH_HEADERS` headers, :data:`_MAX_SHELL_READINGS` path components or the deadline, the patch is denied
    with the budget's reason."""
    deadline = _Deadline()
    try:
        paths = _patch_paths(command)
    except _OverBudget:
        return Decision(allow=False, reason=_codex_budget_reason(project, True))
    base = _shell_base(workdir, directory)
    if paths is None or base is None:
        return Decision(allow=False, reason=_unjudged_reason(project, True))
    model_root = project.resolved_model_root.casefold()
    ancestors = _guard_input_ancestors(protected, project.resolved_project_root)
    model_folders = _model_ancestors(project)
    readings = 0
    for path in paths:
        readings += _component_count(path)
        if readings > _MAX_SHELL_READINGS:
            return Decision(allow=False, reason=_codex_budget_reason(project, True))
        try:
            resolved = _codex_resolve(Path(base), path, deadline) if path else None
        except _OverBudget:
            return Decision(allow=False, reason=_codex_budget_reason(project, True))
        if resolved is None:
            return Decision(allow=False, reason=_unjudged_reason(project, True))
        if path_contains(model_root, resolved.casefold()):
            name = project_relative_name(project, resolved) or "the model directory"
            return Decision(
                allow=False,
                reason=(
                    f"SWCraft: in this project {_model_subject(project)} is changed through the SWCraft tools, whose "
                    f"changes the panel reviews; this patch edits `{name}` there. Use the SWCraft MCP tool "
                    "`change_set` instead."
                ),
            )
        forms = (resolved.casefold(), _lexical(base, path).casefold())
        if any(path_contains(guarded.casefold(), form) for guarded in protected for form in forms) or any(
            form in ancestors for form in forms
        ):
            return Decision(allow=False, reason=_guard_input_reason("patch"))
        if any(form in model_folders for form in forms):
            return Decision(allow=False, reason=_model_folder_reason(project, "patch"))
    return _model_path_decision(project, directory, paths, end=deadline.end)


def _patch_paths(command: object) -> list[str] | None:
    """Every path a patch's headers name, in order; ``None`` when *command* is no patch naming one.

    A patch is a string whose first non-blank line is ``*** Begin Patch``; a
    header line is read after its leading whitespace, which only judges more.
    Lines are split as Codex's parser splits them -- at ``\\n`` alone -- and
    stripped of what Rust's ``trim`` strips (:data:`_PATCH_WHITESPACE`), never at
    the other separators ``splitlines`` knows, which a valid path may hold.

    Raises:
        _OverBudget: past :data:`_MAX_PATCH_HEADERS` headers.
    """
    if not isinstance(command, str):
        return None
    position = 0
    while position <= len(command):
        end = command.find("\n", position)
        end = len(command) if end == -1 else end
        first = command[position:end].strip(_PATCH_WHITESPACE)
        if first:
            break
        position = end + 1
    else:
        return None
    if first != "*** Begin Patch":
        return None
    paths: list[str] = []
    for match in _PATCH_HEADER.finditer(command):
        if len(paths) == _MAX_PATCH_HEADERS:
            raise _OverBudget
        rest = match.group(1).rstrip(_PATCH_WHITESPACE)
        if rest and not rest.startswith(" "):
            continue  # ``*** Add File:x`` is no header Codex reads
        paths.append(rest.strip(_PATCH_WHITESPACE))  # a header with nothing after it names an empty path
    return paths or None


def _shell_base(workdir: object, directory: Path) -> str | None:
    """The base a shell command's words resolve against: ``workdir`` against *directory*, else *directory*.

    ``None`` when a ``workdir`` arrives that is not a non-empty string or cannot be resolved."""
    if workdir is None:
        return _resolve(directory, ".")
    return _resolve(directory, workdir) if isinstance(workdir, str) and workdir else None


def _decide_shell(
    command: object,
    workdir: object,
    directory: Path,
    project: SwcraftProject,
    protected: tuple[str, ...],
    handoff: str | None = None,
    msys: bool = False,
    home: str | None = None,
) -> Decision:
    """Deny a shell command that reaches the model or a guard input, cannot be parsed or is past the budget.

    Denied when any word resolves to the model root or inside it, names one of the guard's own inputs outside a read,
    cannot be parsed, or is past the budget. *handoff*, *home* and *msys* are the Claude Code and Kiro dialects'
    additions (:class:`_ShellJudge`); a base inside *handoff* is denied too."""
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        command = " ".join(command)
    if not isinstance(command, str):
        return Decision(allow=False, reason=_unjudged_reason(project, False))
    model_root = project.resolved_model_root
    base = _shell_base(workdir, directory)
    if base is None:
        return Decision(allow=False, reason=_unjudged_reason(project, False))
    if path_contains(model_root.casefold(), base.casefold()):
        return Decision(allow=False, reason=_codex_shell_reason(project))
    if handoff is not None and path_contains(handoff.casefold(), base.casefold()):
        return Decision(allow=False, reason=_handoff_reason())
    texts = [command, command.replace("\\", "/")] if _BACKSLASH_SEPARATES and "\\" in command else [command]
    judge = _ShellJudge(
        Path(base), model_root, protected, project.resolved_project_root, project, handoff, msys, home=home
    )
    verdict = None
    for text in texts:
        verdict = judge.command(text)
        if verdict is not None:
            break
    if verdict == _MODEL:
        return Decision(allow=False, reason=_codex_shell_reason(project))
    if verdict == _HANDOFF:
        return Decision(allow=False, reason=_handoff_reason())
    if verdict == _HOME:
        return Decision(allow=False, reason=_home_reason())
    if verdict == _GUARD_INPUT:
        return Decision(allow=False, reason=_guard_input_reason("command"))
    if verdict == _MODEL_FOLDER:
        return Decision(allow=False, reason=_model_folder_reason(project, "command"))
    if verdict == _OVER_BUDGET:
        return Decision(allow=False, reason=_codex_budget_reason(project))
    if verdict == _BUILD_OUTPUT:
        return Decision(allow=False, reason=_build_output_reason(judge.build_problem or "outputs"))
    if verdict is not None:
        return Decision(allow=False, reason=_unjudged_reason(project, False))
    return Decision(allow=True)


class _Deadline:
    """The wall-clock bound of one Codex judgement (:data:`_JUDGE_SECONDS`), on a monotonic clock.

    :meth:`check` raises :class:`_OverBudget` past it."""

    def __init__(self) -> None:
        self.end = time.monotonic() + _JUDGE_SECONDS

    def check(self) -> None:
        if time.monotonic() > self.end:
            raise _OverBudget


def _past_the_link_limit(path: Path) -> bool:
    """Whether opening *path* fails for following more symbolic links than the kernel follows.

    ``ELOOP``; on Windows too many reparse points: one status read. No command can open such a name, and resolving it
    would follow every link uncharged, so the Codex judges deny it unjudged."""
    try:
        os.stat(path)
    except OSError as error:
        return error.errno == errno.ELOOP or getattr(error, "winerror", None) == _TOO_MANY_LINKS_WINERROR
    except ValueError:  # a name no system call takes; the resolution that follows refuses it
        return False
    return False


def _codex_resolve(base: Path, written: str, deadline: _Deadline) -> str | None:
    """:func:`_resolve` for the Codex judges.

    The deadline is read first (:class:`_OverBudget` past it), and ``None`` -- unjudged -- is the answer for a name past
    the kernel's link limit."""
    deadline.check()
    if _judgeable_here(written) and _past_the_link_limit(base / written):
        return None
    return _resolve(base, written)


def _component_count(path: str) -> int:
    """How many components *path* has at most, a backslash counted as a separator.

    That is the status reads resolving it costs."""
    return path.count("/") + path.count("\\") + 1


class _OverBudget(Exception):
    """The shell judge passed :data:`_MAX_SHELL_READINGS`."""


class _ShellJudge:
    """One shell command's judgement (``docs/guide/model-guard.md``, *Under Codex*): which verdict, or none.

    It counts every path reading against :data:`_MAX_SHELL_READINGS`, across
    every text judged for the command, and reads one deadline (:class:`_Deadline`)
    with every charge and before every resolution and directory scan.
    *project* is the guarded project, whose builds (:data:`_SWCRAFT_BUILDS`) are
    judged by the outputs its manifest names; without one no build is. *handoff*
    is the resolved review hand-off directory, judged as the model root is -- no
    reader exception -- with the verdict :data:`_HANDOFF`, a literal word naming
    a folder above it (not a filesystem root) included; ``None`` judges none.
    *home* is the resolved SWCraft home, judged as a guard input is -- a reader
    may name what is below it -- with the verdict :data:`_HOME`, a literal word
    outside a read naming a folder above it (no filesystem root) included;
    ``None`` judges none. With *msys* (Git Bash on Windows) every expansion and
    literal path of the form ``/x/...`` is judged a second time as the drive path
    ``X:/...``.
    """

    def __init__(
        self,
        base: Path,
        model_root: str,
        protected: tuple[str, ...],
        project_root: str | None = None,
        project: SwcraftProject | None = None,
        handoff: str | None = None,
        msys: bool = False,
        *,
        home: str | None = None,
    ) -> None:
        self.base = base
        self.handoff = handoff
        self.home = home
        self.msys = msys
        self.project = project
        #: Why the last build of the guarded manifest judged is refused.
        self.build_problem: str | None = None
        self.deadline = _Deadline()
        self.model_root = model_root
        self.protected = protected
        self.ancestors = _guard_input_ancestors(protected, project_root)
        self.model_folders = _model_ancestors(project) if project is not None else frozenset()
        #: The resolved project directory, below which a glob's real matches are read.
        self.project_root = project_root
        self.readings = 0
        #: Whether a text judged so far defines a function or changes what a command name runs: from then on no
        #: command of the judgement is a reader.
        self.no_readers = False
        self.input_charge = len(protected) // _INPUTS_PER_READING

    def command(self, text: str) -> str | None:
        """The verdict on one whole command text.

        ``None`` when it names nothing judged; an unparseable text, or one holding a word the guard cannot read, is
        :data:`_UNJUDGED`, and one past the budget :data:`_OVER_BUDGET`."""
        if len(text) > _MAX_SHELL_TEXT:
            return _OVER_BUDGET
        try:
            return self._text(text, 0)
        except _OverBudget:
            return _OVER_BUDGET

    def _spend(self, count: int) -> None:
        """Charge *count* path readings.

        Past :data:`_MAX_SHELL_READINGS`, or past the deadline, the judgement stops (:class:`_OverBudget`)."""
        self.readings += count
        if self.readings > _MAX_SHELL_READINGS:
            raise _OverBudget
        self.deadline.check()

    def _text(self, text: str, depth: int) -> str | None:
        """The verdict on *text*, and first on its extglob reading when it holds a group (:func:`_extglob_read`)."""
        variant = _extglob_read(text)
        if variant is not None and (verdict := self._written_text(variant, depth)) is not None:
            return verdict
        return self._written_text(text, depth)

    def _written_text(self, text: str, depth: int) -> str | None:
        """The verdict on *text* as written.

        Each backtick substitution's body is judged first as a command of its own (``shlex`` never splits at a
        backtick), read quote-blind, then its simple commands. A text that defines a function or changes what a command
        name runs (:func:`_renames_commands`), or that holds ``xargs`` (:func:`_runs_xargs`), turns the reader exemption
        off for the rest of the judgement, its nested texts included, before any of its words is judged."""
        tokens = _shell_words(text)
        if (tokens is not None and _renames_commands(tokens)) or _runs_xargs(text, tokens):
            self.no_readers = True
        pieces = text.split("`") if "`" in text else []
        for body in pieces[1:-1:2]:
            if depth >= _MAX_SHELL_DEPTH:
                return _UNJUDGED
            if (verdict := self._text(body, depth + 1)) is not None:
                return verdict
        if tokens is None:
            if depth == 0:
                return _UNJUDGED
            # A nested word that is no shell text (``-m "don't"``): every part of it is judged as a word.
            parts = [part for part in _NOT_A_PATH.split(text) if part]
            return self._words([(part, None) for part in parts], reader=False, depth=depth)
        tokens = _rejoined(tokens, _quoted_tokens(text, len(tokens)))
        if tokens is None:
            return _UNJUDGED
        for simple in _simple_commands(tokens):
            words = [word for word, _ in simple]
            targets, inert = _sed_reading(words)
            build = None if self.no_readers else self._build_verdict(simple)
            if build is False:
                return _BUILD_OUTPUT
            reader = not self.no_readers and (build is True or _reads_only(words))
            verdict = self._words(simple, reader=reader, depth=depth, inert=inert)
            for target in targets:
                if verdict is not None:
                    break
                verdict = self._literal(target)
            if verdict is not None:
                return verdict
        return None

    def _drive_readings(self, written: str) -> list[str]:
        """*written*, and under *msys* its drive-path reading too (:func:`_msys_drive_path`), each judged."""
        translated = _msys_drive_path(written) if self.msys else None
        return [written] if translated is None else [written, translated]

    def _above_handoff(self, resolved: str) -> bool:
        """Whether *resolved* is a folder strictly above the hand-off directory and no filesystem root.

        A word naming it reaches the records below it. A root (``/``, ``C:/``) is left out: ``tr / _`` names one."""
        if self.handoff is None:
            return False
        folded, handoff = resolved.casefold(), self.handoff.casefold()
        return folded != handoff and path_contains(folded, handoff) and Path(resolved).parent != Path(resolved)

    def _above_home(self, resolved: str) -> bool:
        """Whether *resolved* is a folder strictly above the SWCraft home and no filesystem root.

        Removing or moving it removes the ``swcraft`` the hook runs."""
        return self.home is not None and _holds_handoff(resolved.casefold(), self.home)

    def _in_home(self, forms: tuple[str, ...]) -> bool:
        """Whether one of the casefolded *forms* lies in the SWCraft home, or is it."""
        return self.home is not None and any(path_contains(self.home.casefold(), form) for form in forms)

    def _literal(self, written: str, may_name_inputs: bool = False) -> str | None:
        """The verdict on *written* read as one literal path.

        No brace expansion, no glob, no parts -- a ``sed`` script's ``w`` file, and a glob or brace word's own spelling.
        The guard's own inputs are judged unless *may_name_inputs*. Under *msys* its drive-path reading is judged too.
        """
        for reading in self._drive_readings(written):
            if (verdict := self._one_literal(reading, may_name_inputs)) is not None:
                return verdict
        return None

    def _one_literal(self, written: str, may_name_inputs: bool) -> str | None:
        """:meth:`_literal` for one reading of the path."""
        self._spend(_component_count(written) + self.input_charge)
        if _DOUBLE_SEPARATOR_IS_UNC and _opens_with_two_separators(written):
            return _UNJUDGED
        resolved = self._resolved(written)
        if resolved is None:
            return _UNJUDGED
        folded = resolved.casefold()
        if path_contains(self.model_root.casefold(), folded):
            return _MODEL
        forms = (folded, self._named(written).casefold())
        if self.handoff is not None and any(path_contains(self.handoff.casefold(), form) for form in forms):
            return _HANDOFF
        if self._above_handoff(resolved):
            return _HANDOFF
        if may_name_inputs:
            return None
        if self._in_home(forms) or self._above_home(resolved):
            return _HOME
        if any(path_contains(target.casefold(), form) for target in self.protected for form in forms):
            return _GUARD_INPUT
        if any(form in self.ancestors for form in forms):
            return _GUARD_INPUT
        if any(form in self.model_folders for form in forms):
            return _MODEL_FOLDER
        return None

    def _named(self, written: str) -> str:
        """*written* against the base as named (:func:`_lexical`).

        Its leading separators are read as :meth:`_resolved` reads them."""
        separators = "/\\" if _SHELL_WORDS_HAVE_DRIVES else "/"
        if _two_leading_separators(written, separators):
            written = "/" + written.lstrip(separators)
        return _lexical(str(self.base), written)

    def _resolved(self, written: str) -> str | None:
        """*written* resolved against the base as this host's shell opens it, else ``None`` (denied unjudged).

        A word holding a NUL is ``None``. A run of leading separators is read as one (the callers have already
        denied a word that can be a UNC path). Where shell words have drives the word is read by both platforms'
        rules, so a drive-relative ``C:x`` is ``None``; elsewhere it is resolved natively. The deadline is read
        first, and a word past the kernel's link limit is ``None`` too.
        """
        if "\0" in written:
            return None
        separators = "/\\" if _SHELL_WORDS_HAVE_DRIVES else "/"
        if _two_leading_separators(written, separators):
            written = "/" + written.lstrip(separators)
        if _SHELL_WORDS_HAVE_DRIVES:
            return _codex_resolve(self.base, written, self.deadline)
        self.deadline.check()
        if _past_the_link_limit(self.base / written):
            return None
        try:
            return resolved_path(self.base / written)
        except (OSError, ValueError, RuntimeError):
            return None

    def _words(
        self, simple: list[tuple[str, str | None]], reader: bool, depth: int, inert: frozenset[str] = frozenset()
    ) -> str | None:
        """The verdict on one simple command's words.

        A word holding whitespace, a quote or a shell operator character is tokenised again as shell text --
        unless it is in *inert* (a ``sed`` word no shell reads) and holds no command or process substitution.
        """
        for word, kind in simple:
            retokenise = word not in inert or any(marker in word for marker in _SUBSTITUTIONS)
            if retokenise and any(
                character.isspace() or character in "'\"" or character in _SHELL_PUNCTUATION for character in word
            ):
                if depth >= _MAX_SHELL_DEPTH:
                    return _UNJUDGED
                if (verdict := self._text(word, depth + 1)) is not None:
                    return verdict
            may_name_inputs = reader and kind != "write"
            for part in _word_parts(word, self._spend):
                if (verdict := self._part(part, may_name_inputs)) is not None:
                    return verdict
        return None

    def _part(self, part: str, may_name_inputs: bool) -> str | None:
        """The verdict on one word part.

        Each of its brace expansions is read as bash may expand it (:meth:`_readings`), and the part is denied when a
        reading could reach the model root, or one of the guard's own inputs unless *may_name_inputs*. A part past a
        glob limit or :data:`_MAX_BRACE_GROUPS` is over the budget."""
        self._spend(1)
        if not part:
            return None
        expansions = (
            _brace_expansions(part, sequences=True, spend=self._spend)
            if len(part) <= _MAX_GLOB_LENGTH and _brace_group_count(part) <= _MAX_BRACE_GROUPS
            else None
        )
        if expansions is None:
            return _OVER_BUDGET
        self._spend(sum(_component_count(expansion) for expansion in expansions))
        if "[" in part:  # the bracket translation reads the part once more
            self._spend(len(part) // _BRACE_SCAN_UNIT)
        if (
            (expansions != [part] or _SHELL_GLOB & set(part))
            and not _judged_by_its_words(part)
            and (verdict := self._literal(part, may_name_inputs)) is not None
        ):
            # A glob or brace word is also the name it spells: quoted, or a glob matching nothing, bash passes
            # it on as written.
            return verdict
        for expansion in (reading for written in expansions for reading in self._drive_readings(written)):
            # A word holding whitespace is judged by its retokenised words. Under *msys* the drive-path reading
            # follows each expansion: bash expands the braces before Git Bash reads ``/c/``.
            if (
                _DOUBLE_SEPARATOR_IS_UNC
                and _opens_with_two_separators(expansion)
                and not _judged_by_its_words(expansion)
            ):
                return _UNJUDGED
            readings = self._readings(expansion)
            if readings is None:
                return _UNJUDGED
            if any(self._reaches(start, pattern, self.model_root) for start, pattern in readings):
                return _MODEL
            if self.handoff is not None and any(
                self._reaches(start, pattern, self.handoff) or (not pattern and self._above_handoff(str(start)))
                for start, pattern in readings
            ):
                return _HANDOFF
            if not may_name_inputs:
                # The guard's inputs are judged by the word as named too, no link followed.
                named = self._readings(expansion, named=True) or []
                for start, pattern in readings if named == readings else readings + named:
                    if self.home is not None and (
                        self._reaches(start, pattern, self.home) or (not pattern and self._above_home(str(start)))
                    ):
                        return _HOME
                    self._spend(self.input_charge)
                    if any(self._reaches(start, pattern, target) for target in self.protected):
                        return _GUARD_INPUT
                    if any(self._names_folder(start, pattern, ancestor) for ancestor in self.ancestors):
                        return _GUARD_INPUT
                    if any(self._names_folder(start, pattern, folder) for folder in self.model_folders):
                        return _MODEL_FOLDER
            if _SHELL_GLOB & set(expansion) and (verdict := self._matches(expansion, may_name_inputs)) is not None:
                return verdict
        return None

    def _build_verdict(self, simple: list[tuple[str, str | None]]) -> bool | None:
        """Whether one simple command is a build of the guarded manifest whose outputs are clean.

        ``None`` when it is no such build (:func:`_builds_from`), ``True`` when every output the manifest names keeps
        off the guard's inputs and the model, ``False`` otherwise, with :attr:`build_problem` naming the output. The
        words a redirection writes or reads, and a bare file-descriptor number (``2>&1``), are not the build's
        arguments."""
        if (project := self.project) is None:
            return None
        arguments = [word for word, kind in simple if kind is None and not word.isdigit()]
        manifest = os.path.join(project.resolved_project_root, MANIFEST_FILENAME)
        targets: list[str | None] = []  # resolved once, and only for a word the parser asks about

        def names_the_manifest(written: str) -> bool:
            if not targets:
                targets.append(self._resolved(manifest))
                targets.append(self._resolved(project.resolved_project_root))
            resolved = self._resolved(written)
            # Exact resolved equality, never a casefolded one: folding would widen an allowance.
            if resolved is None or targets[0] is None or resolved != targets[0]:
                return False
            # The loader bases every output on the folder the word names the manifest in, link not followed, so
            # a link to the manifest from another folder would write the outputs there.
            base = self._resolved(os.path.dirname(written) or ".")
            return base is not None and targets[1] is not None and base == targets[1]

        if not _builds_from(arguments, names_the_manifest):
            return None
        self.build_problem = self._build_problem(project)
        return self.build_problem is None

    def _build_problem(self, project: SwcraftProject) -> str | None:
        """Which output of *project*'s manifest a build would write onto a guard input or into the model.

        The answer is the reason's clause; ``None`` when none would. The outputs are ``outputs.arxml`` and the previous
        ARXML beside it (``<stem>_old<suffix>``), ``outputs.needs``, ``dist_root`` and ``views_root``, each resolved and
        as written. A path that overlaps a guard input or the model root in either direction is a hit: a root holding
        ``.codex`` would be cleaned with it."""
        root = project.resolved_project_root
        rows = [
            ("`outputs.arxml`", project.resolved_arxml_path, project.outputs.arxml),
            ("`outputs.needs`", project.resolved_needs_path, project.outputs.needs),
            ("`dist_root`", project.resolved_dist_root, project.dist_root),
            ("`views_root`", project.resolved_views_root, project.views_root),
        ]
        forms: list[tuple[str, list[str | None]]] = [
            (label, [resolved, _lexical(root, written)]) for label, resolved, written in rows
        ]
        previous = [
            _previous_arxml(project.resolved_arxml_path),
            _previous_arxml(_lexical(root, project.outputs.arxml)),
        ]
        previous.append(self._resolved(previous[1]) if previous[1] is not None else None)
        forms.insert(1, ("`outputs.arxml`'s previous file (`<stem>_old<suffix>`, kept beside it)", previous))
        model_root = project.resolved_model_root
        for label, paths in forms:
            for path in paths:
                self._spend(1 + self.input_charge)
                if path is None:
                    return f"{label} cannot be resolved"
                if paths_overlap(model_root, path):
                    return f"{label} points into the model directory"
                if any(paths_overlap(target, path) for target in self.protected):
                    return f"{label} points at one of them"
        return None

    def _matches(self, expansion: str, may_name_inputs: bool) -> str | None:
        """The verdict on a glob's real matches, each resolved.

        So a link a wildcard matches is judged as what it resolves to.

        Read only below an existing directory inside the project, component by component; ``**`` matches every
        name below, no linked directory entered. Every candidate, the directories on the way included, is judged
        against the model root and, unless *may_name_inputs*, the guard's own inputs; one that cannot be resolved
        is denied unjudged. Past :data:`_MAX_GLOB_ENTRIES` entries for one glob the command is over the budget. A
        candidate outside the project is judged but never read below.
        """
        if self.project_root is None:
            return None
        path = Path(expansion)
        literal = _literal_prefix(path, _SHELL_GLOB)
        written = str(Path(*literal)) if literal else "."
        start = self._resolved(written)
        if start is None or not self._inside_project(start):
            return None
        components = path.parts[len(literal) :]
        candidates = [(start, self._named(written))]
        entries = [0]
        for position, component in enumerate(components):
            found: list[tuple[str, str]] = []
            for directory, named in candidates:
                if not _SHELL_GLOB & set(component):
                    found.append((os.path.join(directory, component), os.path.join(named, component)))
                    continue
                for name in self._entries(directory, component, entries):
                    found.append((os.path.join(directory, name), os.path.join(named, name) if name else named))
            last = position + 1 == len(components)
            for candidate, named in found:
                if (verdict := self._match_verdict(candidate, named, may_name_inputs, last)) is not None:
                    return verdict
            if last:
                break
            candidates = [
                (candidate, named)
                for candidate, named in found
                if os.path.isdir(candidate)
                and (resolved := self._resolved(candidate))
                and self._inside_project(resolved)
            ]
            if not candidates:
                break
        return None

    def _reaches(self, start: Path, pattern: list[str], target: str) -> bool:
        """Whether a reading can name *target*, its matching charged to the budget.

        The charge is the pattern's components times the target's components below *start*. With no pattern the answer
        is containment alone."""
        folded_target, folded_start = target.casefold(), str(start).casefold()
        if path_contains(folded_target, folded_start):
            return True
        if not pattern or not path_contains(folded_start, folded_target):
            return False
        self._spend(len(pattern) * max(1, _component_count(folded_target) - _component_count(folded_start)))
        return _could_reach(pattern, list(Path(folded_target).relative_to(Path(folded_start)).parts))

    def _names_folder(self, start: Path, pattern: list[str], folder: str) -> bool:
        """Whether a reading names the casefolded *folder* itself, never a path inside it.

        Charged as :meth:`_reaches` is."""
        folded_start = str(start).casefold()
        if not pattern:
            return folded_start == folder
        if folded_start == folder or not path_contains(folded_start, folder):
            return False
        self._spend(len(pattern) * max(1, _component_count(folder) - _component_count(folded_start)))
        return _could_reach(pattern, list(Path(folder).relative_to(Path(folded_start)).parts), exact=True)

    def _inside_project(self, resolved: str) -> bool:
        return self.project_root is not None and path_contains(self.project_root.casefold(), resolved.casefold())

    def _entries(self, directory: str, component: str, entries: list[int]) -> list[str]:
        """The names below *directory* that glob *component* matches.

        ``""`` stands for *directory* itself when ``**`` matches no component; a component that could match ``..`` also
        gives ``.`` and ``..``. Each entry read is charged, *entries* counting this glob's."""
        names: list[str] = []
        if component.startswith(".") and _can_climb(component):
            names.extend((".", ".."))
        if component == "**":
            names.append("")
            pending = [""]
            while pending:
                below = pending.pop()
                for entry in self._listed(os.path.join(directory, below) if below else directory, entries):
                    if entry.name.startswith("."):
                        continue
                    name = os.path.join(below, entry.name) if below else entry.name
                    names.append(name)
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(name)
                    except OSError:
                        continue
            return names
        names.extend(
            entry.name for entry in self._listed(directory, entries) if _matches_component(entry.name, component)
        )
        return names

    def _listed(self, directory: str, entries: list[int]) -> list[os.DirEntry[str]]:
        """*directory*'s entries, each charged; none when it cannot be read.

        Bash matches nothing in a directory that cannot be read. The deadline is read before the scan."""
        listed: list[os.DirEntry[str]] = []
        self.deadline.check()
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    entries[0] += 1
                    self._spend(1)
                    if entries[0] > _MAX_GLOB_ENTRIES:
                        raise _OverBudget
                    listed.append(entry)
        except OSError:
            return listed
        return listed

    def _match_verdict(self, candidate: str, named: str, may_name_inputs: bool, last: bool = True) -> str | None:
        """The verdict on one real candidate of a glob, resolved and as named.

        One past the kernel's link limit is unjudged. A folder holding a guard input or the model counts only as the
        *last* component's match, never as a directory on the way (``.gi*/w*`` names ``.github/workflows``)."""
        self._spend(1)
        if _past_the_link_limit(Path(candidate)):
            return _UNJUDGED
        try:
            resolved = resolved_path(Path(candidate))
        except (OSError, ValueError, RuntimeError):
            return _UNJUDGED
        folded = resolved.casefold()
        if path_contains(self.model_root.casefold(), folded):
            return _MODEL
        if self.handoff is not None and path_contains(self.handoff.casefold(), folded):
            return _HANDOFF
        if may_name_inputs:
            return None
        self._spend(self.input_charge)
        forms = (folded, os.path.normpath(named).casefold())
        if self._in_home(forms):
            return _HOME
        if any(path_contains(target.casefold(), form) for target in self.protected for form in forms):
            return _GUARD_INPUT
        if last and any(form in self.ancestors for form in forms):
            return _GUARD_INPUT
        if last and any(form in self.model_folders for form in forms):
            return _MODEL_FOLDER
        return None

    def _readings(self, expansion: str, named: bool = False) -> list[tuple[Path, list[str]]] | None:
        """Every place one brace expansion can name: a start directory and the glob components below it.

        ``None`` when its literal part cannot be resolved.

        A ``..`` after the literal leading directories collapses with the component before it; a component that
        opens with a literal ``.`` -- the only kind bash lets match ``.`` or ``..`` -- and can match ``..`` is read
        as ``..``, as ``.`` and as the pattern it is. With *named*, the literal part is taken as named, no link
        followed.
        """
        if not expansion:
            return []
        if not (_SHELL_GLOB & set(expansion)):
            resolved = self._resolved(expansion)
            if resolved is not None and named:
                resolved = self._named(expansion)
            return None if resolved is None else [(Path(resolved), [])]
        path = Path(expansion)
        parts = path.parts
        literal = _literal_prefix(path, _SHELL_GLOB)
        written = str(Path(*literal)) if literal else "."
        resolved = self._resolved(written)
        if resolved is None:
            return None
        if named:
            resolved = self._named(written)
        readings: list[tuple[Path, list[str]]] = [(Path(resolved), [])]
        for part in parts[len(literal) :]:
            if part == "..":
                readings = [_climbed(reading) for reading in readings]
            elif _SHELL_GLOB & set(part) and part.startswith(".") and _can_climb(part):
                readings = [
                    alternative
                    for reading in readings
                    for alternative in (_climbed(reading), reading, (reading[0], [*reading[1], part]))
                ]
            else:
                readings = [(start, [*pattern, part]) for start, pattern in readings]
            self._spend(len(readings))
        return readings


def _command_name(word: str) -> str:
    """A command word's last path component, the name a command is matched by (``/usr/bin/sed`` is ``sed``)."""
    return _PATH_SEPARATOR.split(word)[-1]


#: The command wrappers :func:`_unwrapped` reads: each runs the command its arguments name.
_WRAPPERS = frozenset({"builtin", "command", "env", "exec", "nice", "nohup", "stdbuf", "timeout"})

#: Per wrapper: its short options that take no value, those that take one (glued or the next word), and its
#: long options likewise. ``env -S`` / ``--split-string`` is on neither list, so it refuses the unwrapping.
_WRAPPER_OPTIONS: dict[str, tuple[str, str, frozenset[str], frozenset[str]]] = {
    "command": ("pvV", "", frozenset(), frozenset()),
    "exec": ("cl", "a", frozenset(), frozenset()),
    "env": (
        "i0v",
        "uCPa",
        frozenset({"ignore-environment", "null", "debug", "list-signal-handling"}),
        frozenset({"unset", "chdir", "argv0"}),
    ),
    "nice": ("", "n", frozenset(), frozenset({"adjustment"})),
    "nohup": ("", "", frozenset(), frozenset()),
    "stdbuf": ("", "ioe", frozenset(), frozenset({"input", "output", "error"})),
    "timeout": (
        "vfp",
        "sk",
        frozenset({"preserve-status", "foreground", "verbose"}),
        frozenset({"signal", "kill-after"}),
    ),
}

#: ``env``'s long options whose value may only be glued with ``=``.
_ENV_OPTIONAL_VALUES = frozenset({"block-signal", "default-signal", "ignore-signal"})

#: An old-style ``nice`` adjustment (``nice -5 cmd``, ``nice --5 cmd``).
_NICE_ADJUSTMENT = re.compile(r"--?[+-]?[0-9]+")


def _unwrapped(words: list[str], bare: bool = False) -> tuple[list[str], bool] | None:
    """*words* -- a simple command from its name on -- unwrapped to the command that runs.

    Also whether a wrapper set a variable (``env X=1``); ``None`` when a wrapper's arguments cannot be parsed.

    The wrappers are :data:`_WRAPPERS`, matched by the command word's last path component and unwrapped
    repeatedly (``env command nice sed``), each with the options :data:`_WRAPPER_OPTIONS` lists; ``--`` ends a
    wrapper's options. Any other option -- an unknown one, an abbreviated long one, ``env -S`` -- refuses the
    unwrapping (``None``). What follows a wrapper with no command is the empty list. ``xargs``, ``find -exec``,
    ``sudo`` and every other wrapper are not unwrapped. With *bare* a wrapper is only its bare name: a word
    holding a path separator (``notes/env``) may be any program, so it is never unwrapped.
    """
    assigned = False
    while words and _command_name(words[0]) in _WRAPPERS and not (bare and _PATH_SEPARATOR.search(words[0])):
        name = _command_name(words[0])
        if name == "builtin":
            words = words[1:]
            continue
        flags, valued, long_flags, long_valued = _WRAPPER_OPTIONS[name]
        index = 1
        while index < len(words):
            word = words[index]
            if word == "--":
                index += 1
                break
            if name == "env" and _ASSIGNMENT.fullmatch(word):
                assigned = True
                index += 1
                continue
            if name == "env" and word == "-":
                index += 1
                continue
            if not word.startswith("-") or word == "-":
                break
            if name == "nice" and _NICE_ADJUSTMENT.fullmatch(word):
                index += 1
                continue
            if word.startswith("--"):
                option, equals, _ = word[2:].partition("=")
                if option in long_flags and not equals:
                    index += 1
                elif option in long_valued:
                    index += 1 if equals else 2
                elif name == "env" and option in _ENV_OPTIONAL_VALUES:
                    index += 1
                else:
                    return None
                continue
            for position, character in enumerate(word[1:], start=1):
                if character in flags:
                    continue
                if character in valued:
                    index += 1 if position + 1 < len(word) else 2
                    break
                return None
            else:
                index += 1
        if index > len(words):
            return None
        if name == "timeout":
            if index >= len(words):
                return None
            index += 1
        words = words[index:]
    return words, assigned


def _sed_reading(words: list[str]) -> tuple[list[str], frozenset[str]]:
    """For a command naming ``sed``: every file its scripts can write, and the words no shell reads.

    Every file its scripts can write (``w``, ``W``, ``s///w``) is one literal path.

    The scan opens at the first word whose last path component is ``sed``, wherever it stands, and reads every
    word after it, each later ``sed`` word starting its options afresh; a ``sed`` that is only an argument
    (``grep sed notes``) makes the words after it read as scripts too, which can only deny more. Every word is
    read as a script (:func:`_sed_script`) -- a word after ``--``, one not opening with ``-``, the value of a
    long option's ``=``, and what follows ``e`` in a short option cluster (``-ne'wx'``, up to an ``f``, ``i``
    or ``l``). When no word can hold an ``e`` command or an ``s///e`` flag and the command that runs is that
    ``sed``, its words are inert: never tokenised again as shell text. A word after the command's name shaped
    as an assignment is an argument (``sed -n 's=a=b=wswcraft.yaml'``).
    """
    start = 0
    while start < len(words) and (words[start] in _SHELL_KEYWORDS or _ASSIGNMENT.fullmatch(words[start])):
        start += 1
    names = words[start:]
    first = next((index for index, word in enumerate(names) if _command_name(word) == "sed"), None)
    if first is None:
        return [], frozenset()
    unwrapped = _unwrapped(names)
    runs_sed = unwrapped is not None and bool(unwrapped[0]) and _command_name(unwrapped[0][0]) == "sed"
    scripts: list[str] = []
    options = True
    for word in names[first + 1 :]:
        if _command_name(word) == "sed":
            # Another ``sed``: its own options follow; the word is read as a script too, in case it is one.
            options = True
            scripts.append(word)
        elif options and word == "--":
            options = False
        elif not options or not word.startswith("-"):
            scripts.append(word)
        elif word.startswith("--"):
            scripts.extend(word.partition("=")[2:] if "=" in word else [])
        else:
            for index, character in enumerate(word[1:], start=1):
                if character == "e":
                    scripts.append(word[index + 1 :])
                    break
                if character in "fil":
                    break
    targets: list[str] = []
    runs = False
    for script in scripts:
        for brackets in (False, True):
            written, executes = _sed_script(script, brackets)
            targets.extend(written)
            runs = runs or executes
    inert = frozenset(unwrapped[0][1:]) if runs_sed and unwrapped is not None and not runs else frozenset()
    return list(dict.fromkeys(targets)), inert


#: The ``s`` command's flags other than ``w``; a ``w`` among them takes the rest of the line as its file.
_SED_S_FLAGS = frozenset("gpiImMe0123456789")


def _sed_script(script: str, brackets: bool) -> tuple[list[str], bool]:
    """The files *script* writes, and whether it can run a command, in one pass.

    The file names are those *script*'s ``w`` and ``W`` commands and ``s`` commands' ``w`` flags write; it can run a
    command through an ``e`` command or an ``s///e`` flag.

    A command position is the script's start, after an address, after ``;``, a
    newline, ``{`` or ``}``, and after any command that takes no argument (a
    character sed does not know is read as one, so more positions are read than
    sed accepts). An address's ``/re/`` or ``\\cREc`` and an ``s`` or ``y``
    command's parts are skipped to their unescaped delimiter -- with *brackets*,
    a ``[...]`` bracket expression holds the delimiter literally, as BSD sed
    reads it; without, as GNU sed does. A label ends at ``;``, ``}`` or a
    newline; ``a``, ``i``, ``c``, ``r``, ``R`` and ``e`` take the rest of the
    line. A write's file is the rest of the line after leading blanks. Every
    character is read a bounded number of times.
    """
    targets: list[str] = []
    runs = False
    length = len(script)
    index = 0
    # Linear: the next newline is searched for again only once it is passed, and a line in which a ``[`` found
    # no ``]`` is never searched for one again.
    newline = -1
    unclosed_line = -1

    def line_end(position: int) -> int:
        nonlocal newline
        if newline < position:
            found = script.find("\n", position)
            newline = length if found == -1 else found
        return newline

    def delimited(position: int, delimiter: str, in_regex: bool) -> int:
        """The index just past the delimiter that closes a part opening at *position* (``length`` if none)."""
        nonlocal unclosed_line
        while position < length:
            character = script[position]
            if character == "\\":
                position += 2
                continue
            if character == delimiter:
                return position + 1
            if in_regex and brackets and character == "[" and line_end(position) != unclosed_line:
                end = line_end(position)
                close = script.find("]", position + 2 if script.startswith("[]", position) else position + 1, end)
                if close != -1:
                    position = close + 1
                    continue
                unclosed_line = end
            if character == "\n":
                return position
            position += 1
        return length

    while index < length:
        character = script[index]
        if character in " \t;\n{}!,0123456789$~+IM":
            index += 1
        elif character == "/":
            index = delimited(index + 1, "/", True)
        elif character == "\\" and index + 1 < length:
            index = delimited(index + 2, script[index + 1], True)
        elif character == "#" or character in "aicrRe":
            runs = runs or character == "e"
            index = line_end(index + 1)
        elif character in ":btT":
            index += 1
            while index < length and script[index] not in ";}\n":
                index += 1
        elif character in "wW":
            end = line_end(index + 1)
            targets.append(script[index + 1 : end].lstrip(" \t"))
            index = end
        elif character in "sy" and index + 1 < length and script[index + 1] not in "\n\\":
            delimiter = script[index + 1]
            index = delimited(index + 2, delimiter, True)
            index = delimited(index, delimiter, False)
            if character == "y":
                continue
            while index < length and script[index] in _SED_S_FLAGS:
                runs = runs or script[index] == "e"
                index += 1
            if index < length and script[index] == "w":
                end = line_end(index + 1)
                targets.append(script[index + 1 : end].lstrip(" \t"))
                index = end
        else:
            index += 1
    return [target for target in targets if target], runs


def _climbed(reading: tuple[Path, list[str]]) -> tuple[Path, list[str]]:
    """*reading* after a ``..``: the last glob component dropped, else the start's parent."""
    start, pattern = reading
    return (start, pattern[:-1]) if pattern else (start.parent, [])


def _reading_reaches(start: Path, pattern: list[str], target: str) -> bool:
    """Whether the paths *start* followed by glob components *pattern* can name *target* or a path inside it."""
    folded_target, folded_start = target.casefold(), str(start).casefold()
    if path_contains(folded_target, folded_start):
        return True
    if not path_contains(folded_start, folded_target):
        return False
    return _could_reach(pattern, list(Path(folded_target).relative_to(Path(folded_start)).parts))


def _judged_by_its_words(word: str) -> bool:
    """Whether *word*, opening with two separators where they can open a UNC path, holds whitespace.

    It is then text (``// TODO``), judged by its retokenised words, not denied as one path."""
    return (
        _DOUBLE_SEPARATOR_IS_UNC and _opens_with_two_separators(word) and any(character.isspace() for character in word)
    )


def _opens_with_two_separators(word: str) -> bool:
    """Whether *word* opens with two separators and names something after them (``//host/x``, ``//<abs>``).

    ``//`` alone names nothing more and is judged as a word.
    """
    return _two_leading_separators(word) and bool(word.strip("/\\"))


#: The one-character special parameters a ``${...}`` may name (``${?:-x}``, ``${@-x}``).
_SPECIAL_PARAMETERS = frozenset("@*#?$!")

#: The operators after which a parameter expansion holds a word bash may put in its place.
_PARAMETER_OPERATORS = frozenset("-=+?")


def _parameter_head_end(word: str, start: int) -> int | None:
    """Where the word of a ``${name<op>word}`` opening at *start* (just past ``${``) begins, else ``None``.

    The head is an optional ``!`` or ``#``, one name -- a run of ``[A-Za-z0-9_]`` or
    one :data:`_SPECIAL_PARAMETERS` character, never both -- an optional subscript
    ``[...]`` holding no brace, an optional ``:`` and one of
    :data:`_PARAMETER_OPERATORS`. A subscript scan stops at the next brace, so the
    caller's walk stays linear in the word's length.
    """
    index = start
    if index < len(word) and word[index] in "!#":
        index += 1
    name = index
    while index < len(word) and (word[index].isascii() and (word[index].isalnum() or word[index] == "_")):
        index += 1
    if index == name:
        if index < len(word) and word[index] in _SPECIAL_PARAMETERS:
            index += 1
        elif index == start:
            return None
        # Otherwise the ``#`` or ``!`` read as a prefix is the name itself (``${#-x}``, ``${!-x}``).
    if index < len(word) and word[index] == "[":
        index += 1
        while index < len(word) and word[index] not in "]{}":
            index += 1
        if index == len(word) or word[index] != "]":
            return None
        index += 1
    if index < len(word) and word[index] == ":":
        index += 1
    if index < len(word) and word[index] in _PARAMETER_OPERATORS:
        return index + 1
    return None


def _parameter_words(word: str) -> str:
    """*word* with every ``${name<op>word}`` read as its word, nested ones included, in one linear pass.

    A ``${name<op>word}`` (``${x:-model}``, ``${x=model}``, ``${x+y}``) can be
    replaced by its word, so its head and its closing brace are dropped. A ``${``
    with no such head (``${x#p}``, ``${x}``), a plain ``{`` and an unclosed ``${``
    are kept as written.
    """
    if "${" not in word:
        return word
    dropped: list[tuple[int, int]] = []
    openers: list[tuple[int, int] | None] = []
    index = 0
    while index < len(word):
        character = word[index]
        if character == "$" and word.startswith("{", index + 1):
            head = _parameter_head_end(word, index + 2)
            openers.append((index, head) if head is not None else None)
            index = head if head is not None else index + 2
            continue
        if character == "{":
            openers.append(None)
        elif character == "}" and openers:
            opener = openers.pop()
            if opener is not None:
                dropped.extend((opener, (index, index + 1)))
        index += 1
    if not dropped:
        return word
    pieces: list[str] = []
    kept = 0
    for start, end in sorted(dropped):
        pieces.append(word[kept:start])
        kept = end
    pieces.append(word[kept:])
    return "".join(pieces)


def _word_parts(word: str, spend: Callable[[int], None] | None = None) -> list[str]:
    """*word* and every part of it judged as a word too, each once.

    The parts are those around ``=``, ``,``, ``:`` and a backtick, each one's every suffix after its first two
    characters when it opens with a single ``-`` (a short-option cluster can end in a glued value: ``sort -uomodel/x``,
    ``cp -rt.codex``), the value after a leading ``@``, and the same parts of *word* with each ``${name<op>word}`` read
    as its word. With *spend* each suffix is charged before it is made, by the characters it copies.

    A part after a ``:`` has its leading separators read as one (``http://host/x`` gives ``/host/x``): a URL's
    ``//`` is no UNC path.
    """
    parts = [word]
    for variant in dict.fromkeys((word, _parameter_words(word))):
        parts.append(variant)
        for piece in _WORD_PART_SEPARATORS.split(variant):
            first, *after = piece.split(":")
            parts.append(first)
            parts.extend(re.sub(r"^[/\\]+", "/", part) for part in after)
    for part in list(parts):
        if part.startswith("-") and not part.startswith("--"):
            for index in range(2, len(part)):
                if spend is not None:
                    spend(1 + (len(part) - index) // _BRACE_SCAN_UNIT)
                parts.append(part[index:])
        if part.startswith("@"):
            parts.append(part[1:])
    return list(dict.fromkeys(parts))


#: The characters that open a bash extglob group when a ``(`` follows: ``!(...)``, ``@(...)``, ``+(...)``,
#: ``*(...)`` and ``?(...)``.
_EXTGLOB_OPENERS = frozenset("!@+*?")


def _extglob_read(text: str) -> str | None:
    """*text* with every bash extglob group read as a component that can match any name.

    ``None`` when it holds none.

    ``shlex`` splits a word at ``(``, but with ``extglob`` on bash reads ``!(notmodel)`` as a pattern that
    matches the model directory. Each group -- an opener of :data:`_EXTGLOB_OPENERS` and a ``(``, up to the
    ``)`` that closes it, nested parentheses counted -- is replaced by ``*``, or by ``{.*,*}`` when the group
    holds a ``.`` anywhere, so it can match a hidden name too. The scan is quote-blind, which only denies more.
    An unclosed group is left as written. The cost is linear.
    """
    if "(" not in text or not any(opener + "(" in text for opener in _EXTGLOB_OPENERS):
        return None
    out: list[str] = []
    opened: list[tuple[bool, int]] = []  # each open parenthesis: whether it opens a group, and its index in out
    dotted: list[bool] = []  # beside each: whether it holds a ``.``
    index, length = 0, len(text)
    while index < length:
        character = text[index]
        if character in _EXTGLOB_OPENERS and index + 1 < length and text[index + 1] == "(":
            opened.append((True, len(out)))
            dotted.append(False)
            out.append(text[index : index + 2])
            index += 2
            continue
        if character == "(":
            opened.append((False, len(out)))
            dotted.append(False)
        elif character == ")" and opened:
            group, marker = opened.pop()
            holds_dot = dotted.pop()
            if holds_dot and dotted:
                dotted[-1] = True
            if group:
                del out[marker:]
                out.append("{.*,*}" if holds_dot else "*")
                index += 1
                continue
        elif character == "." and dotted:
            dotted[-1] = True
        out.append(character)
        index += 1
    read = "".join(out)
    return None if read == text else read


def _shell_words(text: str) -> list[str] | None:
    """*text*'s tokens, the operators among them, or ``None`` when it cannot be tokenised.

    ``shlex`` in POSIX mode with :data:`_SHELL_PUNCTUATION` as tokens of their own, no comment character, and bash's
    ``$'...'`` and ``$"..."`` quoting read first; ``None`` when it cannot be tokenised (an unbalanced quote, a trailing
    escape)."""
    lexer = shlex.shlex(_bash_quotes(text), posix=True, punctuation_chars=_SHELL_PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""  # a ``#`` inside a word must not cut off the rest of the command
    try:
        return list(lexer)
    except ValueError:
        return None


def _rejoined(tokens: list[str], quoted: list[bool] | None = None) -> list[str] | None:
    """*tokens* with every run that a ``${`` holds open joined again.

    ``None`` when one is still open at the end, or when a run of more than one token holds a quote by *quoted*
    (:func:`_quoted_tokens`).

    ``shlex`` splits a ``${...}`` at a blank, newline or operator character (``${x:-model x}``). A run of tokens
    from one that leaves a ``${`` open up to the one that closes it is joined with blanks and read with every
    name unset: its :func:`_parameter_words` reading split into fields, a field of operator characters alone
    dropped. A run holding a command or process substitution keeps its operator fields, so the substituted
    command is judged as a command of its own, and its first field is the marker ``$(``, so the command holding
    it is no reader. A ``${`` open to the end of the text (``grep '${' src`` included) and a split run holding a
    quote anywhere in its tokens (``cat ${x:-a "b c"}``) give ``None``: their fields cannot be rebuilt. The cost
    is linear in the text.
    """
    if not any("${" in token for token in tokens):
        return tokens
    result: list[str] = []
    run: list[str] = []
    stack: list[bool] = []
    unclosed = 0  # how many ``${`` the stack holds open
    for position, token in enumerate(tokens):
        unclosed = _parameter_braces(token, stack, unclosed)
        if not run and not unclosed:
            result.append(token)
            stack.clear()  # a plain ``{`` left open outside a run binds nothing in the next token
            continue
        run.append(token)
        if not unclosed and len(run) > 1 and quoted is not None and any(quoted[position + 1 - len(run) : position + 1]):
            return None
        if not unclosed:
            fields = _parameter_words(" ".join(run)).split()
            if any(_holds_a_substitution(field) for field in run):
                result.append("$(")
                result.extend(fields)
            else:
                result.extend(field for field in fields if not set(field) <= set(_SHELL_PUNCTUATION))
            run = []
            stack.clear()
    return None if run else result


#: The character :func:`_quoted_tokens` marks an opening quote with: a private-use one, no path's.
_QUOTE_MARK = ""


def _quoted_tokens(text: str, count: int) -> list[bool] | None:
    """Beside each of *text*'s *count* tokens (:func:`_shell_words`), whether it held a quote.

    ``None`` when no ``${`` run can need it (no ``${`` or no quote in *text*).

    Each opening quote is marked with :data:`_QUOTE_MARK` inside its own quoting and the text tokenised again.
    A text that holds the mark itself, or whose second tokenisation does not give *count* tokens, is read as
    quoting every token, which only denies more.
    """
    if "${" not in text or not ("'" in text or '"' in text):
        return None
    if _QUOTE_MARK in text:
        return [True] * count
    unquoted = _bash_quotes(text)
    out: list[str] = []
    index, quote = 0, ""
    while index < len(unquoted):
        character = unquoted[index]
        if quote != "'" and character == "\\":
            out.append(unquoted[index : index + 2])
            index += 2
            continue
        if character in "'\"" and quote in ("", character):
            out.append(character + ("" if quote else _QUOTE_MARK))
            quote = "" if quote else character
            index += 1
            continue
        out.append(character)
        index += 1
    lexer = shlex.shlex("".join(out), posix=True, punctuation_chars=_SHELL_PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        marked = list(lexer)
    except ValueError:
        return [True] * count
    return [_QUOTE_MARK in token for token in marked] if len(marked) == count else [True] * count


def _renames_commands(tokens: list[str]) -> bool:
    """Whether *tokens* define a function or run a builtin of :data:`_RENAMING_BUILTINS`.

    Then a command name in the text may not run the command it names (``alias cat=rm``). A definition is a word followed
    by ``()`` or by ``(`` and ``)``, or a simple command named ``function``; a builtin is a simple command's name,
    unwrapped or not. Quote-blind, which only takes a reader's exemption away."""
    for index in range(1, len(tokens)):
        previous = tokens[index - 1]
        if previous and not set(previous) <= set(_SHELL_PUNCTUATION):
            if tokens[index] == "()" or (tokens[index] == "(" and tokens[index + 1 : index + 2] == [")"]):
                return True
    for simple in _simple_commands(tokens):
        words = [word for word, _ in simple]
        index = 0
        while index < len(words) and (words[index] in _SHELL_KEYWORDS or _ASSIGNMENT.fullmatch(words[index])):
            index += 1
        if index == len(words):
            continue
        if words[index] == "function" or _command_name(words[index]) in _RENAMING_BUILTINS:
            return True
        unwrapped = _unwrapped(words[index:])
        if unwrapped is not None and unwrapped[0] and _command_name(unwrapped[0][0]) in _RENAMING_BUILTINS:
            return True
    return False


def _runs_xargs(text: str, tokens: list[str] | None) -> bool:
    """Whether *text* holds ``xargs``, anywhere.

    Quote-blind, or as a token's command name after its quoting is read (``$'x\\x61rgs'``): ``xargs`` hands what a
    reader prints on as names to another command (``ls swcraft.yaml | xargs rm``)."""
    return "xargs" in text or any(_command_name(token) == "xargs" for token in tokens or ())


def _builds_from(words: list[str], names_the_manifest: Callable[[str], bool]) -> bool:
    """Whether a simple command's *words* are a build of the guarded manifest and nothing more.

    That is ``swcraft``, the bare name, with a subcommand of :data:`_SWCRAFT_BUILDS` and only the options listed for it,
    at least one ``--project`` and every one naming the manifest, as *names_the_manifest* judges, one positional of
    ``generate``'s choices, a ``--component`` value that is a component name and an ``--arxml-version`` value a release
    number. Keywords, bare wrappers setting no variable, and ``SWCRAFT_MANIFEST`` set to the manifest may come first;
    any other assignment, a command or process substitution in any word, or any other word makes no build."""
    if any(marker in word for word in words for marker in _SUBSTITUTIONS):
        return False
    index = 0
    while index < len(words) and words[index] in _SHELL_KEYWORDS:
        index += 1
    while index < len(words) and _ASSIGNMENT.fullmatch(words[index]):
        name, _, value = words[index].partition("=")
        if name != _MANIFEST_VARIABLE or not names_the_manifest(value):
            return False
        index += 1
    unwrapped = _unwrapped(words[index:], bare=True)
    if unwrapped is None or unwrapped[1] or len(unwrapped[0]) < 2:
        return False
    command = unwrapped[0]
    if command[0] != "swcraft" or command[1] not in _SWCRAFT_BUILDS:
        return False
    choices, flags, valued = _SWCRAFT_BUILDS[command[1]]
    projects, positional = 0, False
    rest = command[2:]
    position = 0
    while position < len(rest):
        word = rest[position]
        position += 1
        if not word.startswith("-"):
            if choices is None or positional or word not in choices:
                return False
            positional = True
            continue
        option, equals, value = word.partition("=")
        if option in flags and not equals:
            continue
        if option not in valued:
            return False
        if not equals:
            if position == len(rest):
                return False
            value = rest[position]
            position += 1
        if option == "--project":
            if not names_the_manifest(value):
                return False
            projects += 1
        elif option == "--component" and not _COMPONENT_NAME.fullmatch(value):
            return False
        elif option == "--arxml-version" and not _RELEASE_NUMBER.fullmatch(value):
            return False
    return projects > 0 and (choices is None or positional)


def _previous_arxml(path: str) -> str | None:
    """Where a build moves the previous ARXML at *path* aside: ``<stem>_old<suffix>`` beside it.

    Spelled here so the hook imports no writer; ``None`` for a path with no name."""
    candidate = Path(path)
    try:
        return str(candidate.with_name(f"{candidate.stem}_old{candidate.suffix}"))
    except ValueError:
        return None


def _holds_a_substitution(token: str) -> bool:
    """Whether a ``shlex`` token opens or carries a command or process substitution.

    It holds a marker of :data:`_SUBSTITUTIONS`, or it is an operator token holding a ``(`` (``shlex`` splits ``$(``
    into ``$`` and ``(``)."""
    if any(marker in token for marker in _SUBSTITUTIONS):
        return True
    return "(" in token and set(token) <= set(_SHELL_PUNCTUATION)


def _parameter_braces(text: str, stack: list[bool], unclosed: int) -> int:
    """Advance *stack* over *text* and answer how many ``${`` it then holds open.

    ``${`` pushes ``True``, a plain ``{`` ``False``, a ``}`` pops one; *unclosed* is the count before."""
    if "{" not in text and "}" not in text:
        return unclosed
    index = 0
    while index < len(text):
        character = text[index]
        if character == "$" and text.startswith("{", index + 1):
            stack.append(True)
            unclosed += 1
            index += 2
            continue
        if character == "{":
            stack.append(False)
        elif character == "}" and stack:
            unclosed -= stack.pop()
        index += 1
    return unclosed


def _simple_commands(tokens: list[str]) -> list[list[tuple[str, str | None]]]:
    """*tokens* as simple commands, each word marked by the redirection it follows.

    Each is its words, a word after a redirection marked ``"write"`` (``>``, ``>>``, ``&>``, ``<>``) or ``"read"``
    (``<``, ``<<``), the rest ``None``. A token holding ``;``, ``|``, ``(``, ``)`` or a newline, or ``&``/``&&``, ends a
    command."""
    commands: list[list[tuple[str, str | None]]] = [[]]
    kind: str | None = None
    for token in tokens:
        if token and set(token) <= set(_SHELL_PUNCTUATION):
            if set(token) & set("();|\n") or not token.strip("&"):
                commands.append([])
                kind = None
            else:
                kind = "write" if ">" in token else "read"
            continue
        commands[-1].append((token, kind))
        kind = None
    return [command for command in commands if command]


def _reads_only(words: list[str]) -> bool:
    """Whether a simple command runs nothing and writes nothing the agent names.

    The readers are :data:`_READER_COMMANDS` and :data:`_SWCRAFT_READS`.

    A command of keywords alone writes nothing. A leading variable assignment makes it no reader
    (``GIT_EXTERNAL_DIFF=rm git diff``). A wrapper is unwrapped to the command it runs; one the guard cannot
    parse, one that sets a variable and one naming no command make no reader. ``swcraft`` reads with a
    subcommand of :data:`_SWCRAFT_READS` and no word opening with ``-o`` or ``--o``, nor ``resource-report`` or
    ``metrics`` with one opening with ``--r``; ``less`` and ``more`` with no word opening with ``-`` or ``+``
    (``-o`` writes a log file, ``+`` runs a command); ``file`` without ``-C`` / ``--compile``, which writes a
    compiled magic file. A command any word of which holds a command or process substitution is none. The name
    is the bare word, a wrapper's too: a command word holding a path separator (``notes/cat``) may be any
    program.
    """
    if any(marker in word for word in words for marker in _SUBSTITUTIONS):
        return False
    index = 0
    while index < len(words) and words[index] in _SHELL_KEYWORDS:
        index += 1
    if index == len(words):
        return True
    if _ASSIGNMENT.fullmatch(words[index]):
        return False
    unwrapped = _unwrapped(words[index:], bare=True)
    if unwrapped is None or unwrapped[1] or not unwrapped[0]:
        return False
    command = unwrapped[0]
    if _PATH_SEPARATOR.search(command[0]):
        return False
    name = command[0]
    rest = command[1:]
    if name == "swcraft":
        if not rest or rest[0] not in _SWCRAFT_READS:
            return False
        writing = _REPORT_WRITING_PREFIXES if rest[0] in _SWCRAFT_REPORT_COMMANDS else _OUTPUT_OPTION_PREFIXES
        return not any(word.startswith(writing) for word in rest)
    if name not in _READER_COMMANDS:
        return False
    if name in ("less", "more"):
        return not any(word.startswith(("-", "+")) for word in rest)
    if name == "file":
        return not any(
            word.startswith("--c") or (word.startswith("-") and not word.startswith("--") and "C" in word)
            for word in rest
        )
    return True


def _bash_quotes(text: str) -> str:
    """*text* with bash's ``$'...'`` and ``$"..."`` quoting read and its line continuations removed.

    Bash's ANSI-C ``$'...'`` is decoded and re-quoted, locale ``$"..."`` read as ``"..."``, and every backslash-newline
    outside single quotes removed, as bash removes a line continuation.

    ``shlex`` knows neither and would read ``$'model'`` as ``$model``. A ``$'``
    inside single or double quotes stays literal; an unterminated ``$'`` is left
    as written, for ``shlex`` to refuse.
    """
    out: list[str] = []
    index, quote = 0, ""
    while index < len(text):
        character = text[index]
        pair = text[index : index + 2]
        if quote != "'" and character == "\\":
            if pair != "\\\n":  # a line continuation: bash removes both
                out.append(pair)
            index += 2
            continue
        if not quote and pair == "$'":
            decoded = _ansi_c(text, index + 2)
            if decoded is None:
                out.append(text[index:])
                break
            value, index = decoded
            out.append("'" + value.replace("'", "'\"'\"'") + "'")
            continue
        if not quote and pair == '$"':
            index += 1
            continue
        if character in "'\"" and quote in ("", character):
            quote = "" if quote else character
        out.append(character)
        index += 1
    return "".join(out)


#: ANSI-C quoting's one-character escapes.
_ANSI_C_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}

#: ANSI-C quoting's numeric escapes: octal, hexadecimal, Unicode, and a control character.
_ANSI_C_NUMERIC = re.compile(
    r"([0-7]{1,3})|x([0-9A-Fa-f]{1,2})|u([0-9A-Fa-f]{1,4})|U([0-9A-Fa-f]{1,8})|c(.)", re.DOTALL
)


def _ansi_c(text: str, start: int) -> tuple[str, int] | None:
    """The body of a ``$'...'`` opening before *start*, decoded, and the index after its closing quote.

    As bash, the body is decoded to **bytes**: an octal escape ``\\nnn`` is one byte, its value's low eight
    bits (``\\555`` is ``m``), a hexadecimal ``\\xHH`` one byte, a Unicode ``\\uHHHH`` / ``\\UHHHHHHHH`` its
    character's UTF-8 bytes and every other character its own; the bytes are then decoded as UTF-8. A body
    whose bytes do not decode (``$'\\xff'``) is a NUL, denied unjudged. An escape that decodes to NUL ends the
    value: ``$'model\\0junk'/x`` is ``model/x``. A NUL written as itself is kept.
    """
    value = bytearray()
    ended = False
    index = start
    while index < len(text):
        character = text[index]
        if character == "'":
            try:
                return value.decode("utf-8"), index + 1
            except UnicodeDecodeError:
                return "\0", index + 1
        if character != "\\" or index + 1 == len(text):
            if not ended:
                value += character.encode("utf-8", "surrogatepass")
            index += 1
            continue
        following = text[index + 1]
        numeric = None if following in _ANSI_C_ESCAPES else _ANSI_C_NUMERIC.match(text, index + 1)
        if following in _ANSI_C_ESCAPES:
            decoded = _ANSI_C_ESCAPES[following].encode("utf-8")
            index += 2
        elif numeric is not None:
            octal, hexadecimal, short, long, control = numeric.groups()
            if control is not None:
                decoded = bytes([ord(control) & 0x1F])
            elif octal or hexadecimal:
                decoded = bytes([int(octal, 8) & 0xFF if octal else int(hexadecimal, 16)])
            else:
                code = int(short or long, 16)
                # A code past Unicode's range is no character: its bytes are left undecodable.
                decoded = chr(code).encode("utf-8", "surrogatepass") if code <= 0x10FFFF else b"\xff"
            index = numeric.end()
        else:
            decoded = text[index : index + 2].encode("utf-8", "surrogatepass")
            index += 2
        ended = ended or decoded == b"\0"
        if not ended:
            value += decoded
    return None


def _could_reach(pattern: list[str], below: list[str], exact: bool = False) -> bool:
    """Whether glob components *pattern* can match the components *below* or a path that continues them.

    With *exact*, whether they match the components *below* and nothing more.

    Each component is matched as bash matches it without ``dotglob``
    (:func:`_matches_component`); ``**`` matches any number of components, none
    of them a name that opens with ``.``. The cost is the two lengths' product.
    """
    size = len(pattern)

    def closed(reached: list[bool]) -> list[bool]:
        # A ``**`` may match no component at all: the position after it is reached too.
        for position in range(size):
            if reached[position] and pattern[position] == "**":
                reached[position + 1] = True
        return reached

    reached = closed([True] + [False] * size)
    for name in below:
        following = [False] * (size + 1)
        for position in range(size):  # the pattern's end is never carried: it names an ancestor of the target
            if not reached[position]:
                continue
            component = pattern[position]
            if component == "**":
                if not name.startswith("."):
                    following[position] = True
            elif _matches_component(name, component):
                following[position + 1] = True
        reached = closed(following)
        if not any(reached):
            return False
    return reached[size] if exact else any(reached)


def _matches_component(name: str, component: str) -> bool:
    """Whether glob *component* matches the path component *name* as bash does without ``dotglob``.

    ``fnmatch``, casefolded, and a *name* opening with ``.`` only by a *component* that opens with a literal ``.`` --
    never by ``*``, ``?`` or a bracket expression (``[.]codex`` matches no ``.codex``)."""
    if name.startswith(".") and not component.startswith("."):
        return False
    return fnmatch.fnmatchcase(name.casefold(), _bash_brackets(component).casefold())


#: Where a bracket expression's scan in :func:`_bash_brackets` stops: its closing ``]``, or the opening of a
#: character class, collating symbol or equivalence class (the ``[`` of ``[:``, ``[.``, ``[=``).
_BRACKET_STOP = re.compile(r"\]|\[(?=[:.=])")


@functools.lru_cache(maxsize=4096)
def _bash_brackets(component: str) -> str:
    """*component* with its bracket expressions spelled as ``fnmatch`` reads what bash means by them.

    A leading ``^`` negates as ``!`` does (``[^x]`` is ``[!x]``). A bracket expression holding a character
    class, a collating symbol or an equivalence class (``[:alpha:]``, ``[.e.]``, ``[=e=]``) is read as
    ``?``, matching any one character -- more than bash matches, the fail-closed direction. A ``[`` with no
    closing ``]`` is a literal ``[``. The cost is linear up to a logarithm: each stop's outcome is remembered,
    so each is scanned from once.
    """
    if "[" not in component:
        return component
    stops = [found.start() for found in _BRACKET_STOP.finditer(component)]
    closings = {
        delimiter: [found.start() for found in re.finditer(re.escape(delimiter) + r"\]", component)]
        for delimiter in ":.="
    }
    # Per stop passed: the ``]`` the scan reaching it ends at (-1 for none), and whether a class lies between.
    outcomes: dict[int, tuple[int, bool]] = {}

    def scan(position: int) -> tuple[int, bool]:
        passed: list[tuple[int, bool]] = []
        outcome = (-1, False)
        while (at := bisect.bisect_left(stops, position)) < len(stops):
            stop = stops[at]
            if stop in outcomes:
                outcome = outcomes[stop]
                break
            if component[stop] == "]":
                outcome = (stop, False)
                outcomes[stop] = outcome
                break
            closing = closings[component[stop + 1]]
            after = bisect.bisect_left(closing, stop + 2)
            if after == len(closing):  # no closing ``:]``/``.]``/``=]``: the ``[`` is an ordinary character
                passed.append((stop, False))
                position = stop + 1
            else:
                passed.append((stop, True))
                position = closing[after] + 2
        end, special = outcome
        for stop, is_class in reversed(passed):
            special = special or is_class
            outcomes[stop] = (end, special)
        return end, special

    out: list[str] = []
    index, length = 0, len(component)
    while index < length:
        opening = component.find("[", index)
        if opening == -1:
            out.append(component[index:])
            break
        out.append(component[index:opening])
        position = opening + 1
        negated = position < length and component[position] in "!^"
        if negated:
            position += 1
        body_start = position
        if position < length and component[position] == "]":
            position += 1  # a ``]`` first in the expression is a literal one
        end, special = scan(position)
        if end == -1:
            if not special:  # no ``]`` anywhere further on: every ``[`` left is literal
                out.append(component[opening:].replace("[", "[[]"))
                break
            out.append("[[]")  # an unclosed ``[`` matches itself
            index = opening + 1
            continue
        out.append("?" if special else "[" + ("!" if negated else "") + component[body_start:end] + "]")
        index = end + 1
    return "".join(out)


def _model_name(project: SwcraftProject) -> str | None:
    """The model root relative to the project directory, POSIX-spelled, or ``None``.

    ``None`` when it has no such name (outside the project, or the project directory itself)."""
    name = project_relative_name(project, project.resolved_model_root)
    return None if name in (None, ".") else name


def _model_subject(project: SwcraftProject) -> str:
    """``the model directory (`<model>`)``, the model root relative to the project, or without a name."""
    return guard_model_paths.model_subject(_model_name(project))


def _codex_shell_reason(project: SwcraftProject) -> str:
    """The denial of a shell command that names the model directory, or runs in it."""
    return (
        f"SWCraft: in this project {_model_subject(project)} is read and changed through the SWCraft tools "
        "(`find`, `read`, `change_set`), whose changes the panel reviews; this command names it."
    )


def _listed(names: tuple[Path, ...], conjunction: str) -> str:
    """*names* in POSIX spelling as prose: ``a``, ``a and b``, ``a, b and c`` (*conjunction* the last joint)."""
    spelled = [name.as_posix() for name in names]
    return spelled[0] if len(spelled) == 1 else f"{', '.join(spelled[:-1])} {conjunction} {spelled[-1]}"


#: The guard's own inputs as every reason names them.
_GUARD_INPUTS_NAMED = (
    f"swcraft.yaml, {_listed(_SHARED_GUARD_INPUT_FILES, 'and')} and its {_listed(_GUARD_INPUT_FOLDERS, 'and')} "
    "directories"
)


def _guard_input_reason(kind: str) -> str:
    """The denial of a patch, edit or command that writes the guard's own inputs."""
    return (
        f"SWCraft: this project's {_GUARD_INPUTS_NAMED} are the model guard's own inputs; they are edited by the "
        f"user outside the agent, so this {kind}, which writes or names one of them, or a folder holding one, outside "
        "a read, is refused."
    )


def _model_folder_reason(project: SwcraftProject, kind: str) -> str:
    """The denial of a patch, edit or command naming a folder between the project directory and the model root."""
    return (
        f"SWCraft: in this project {_model_subject(project)} is changed through the SWCraft tools, whose changes the "
        f"panel reviews; this {kind} names a folder that holds it, and removing or moving that folder removes or "
        "moves the model, so it is refused. Use the SWCraft MCP tool `change_set` for a model change."
    )


def _build_output_reason(problem: str) -> str:
    """The denial of a build of the guarded manifest whose output reaches a guard input or the model.

    *problem* is the output's clause."""
    return (
        f"SWCraft: this project's {_GUARD_INPUTS_NAMED} are the model guard's own inputs, and this command builds "
        f"from swcraft.yaml, whose {problem}; it is refused until the user changes that output in swcraft.yaml "
        "outside the agent."
    )


def _codex_budget_reason(project: SwcraftProject, is_patch: bool = False) -> str:
    """The denial of a shell command or a patch past the guard's work budget, whatever it names."""
    if is_patch:
        return (
            f"SWCraft: in this project {_model_subject(project)} is changed through the SWCraft tools, whose "
            "changes the panel reviews; this patch is too long, or names too many paths, for the guard to judge "
            "in time, so it is refused. Split it into smaller patches."
        )
    return (
        f"SWCraft: in this project {_model_subject(project)} is read and changed through the SWCraft tools "
        "(`find`, `read`, `change_set`); this command is too long, or names too many paths, for the guard to "
        "judge in time, so it is refused. Split it into shorter commands."
    )


def _guard_folder_reason() -> str:
    """The denial of every patch, edit and shell command when a guard-input folder cannot be read whole."""
    return (
        f"SWCraft: this project's {_listed(_GUARD_INPUT_FOLDERS, 'or')} directory holds more entries than the model "
        "guard reads, or a link there that cannot be resolved, so what the hooks depend on cannot be judged; every "
        "edit and shell command is refused until the user tidies that directory outside the agent."
    )


def _unjudged_reason(project: SwcraftProject, is_patch: bool) -> str:
    """The denial of a patch or command the guard cannot read, so cannot judge (fail closed)."""
    if is_patch:
        return (
            f"SWCraft: in this project {_model_subject(project)} is changed through the SWCraft tools, whose "
            "changes the panel reviews; this patch cannot be read as one that names its files, so what it touches "
            "cannot be judged. Use the SWCraft MCP tool `change_set` for a model change."
        )
    return (
        f"SWCraft: in this project {_model_subject(project)} is read and changed through the SWCraft tools "
        "(`find`, `read`, `change_set`), whose changes the panel reviews; this command's text or working "
        "directory cannot be read, so it cannot be judged. Rephrase it (balanced quotes, no heredoc holding a "
        "quote), or use the SWCraft tools."
    )


# ---------------------------------------------------------------------------
# The Claude Code dialect
# ---------------------------------------------------------------------------

#: Claude Code's two project settings files: either, with ``swcraft.yaml`` beside its folder, marks the project
#: the Claude dialect guards.
CLAUDE_SETTINGS_FILES = (Path(".claude") / "settings.json", Path(".claude") / "settings.local.json")

#: Claude Code's file-writing tools, judged by every path under :data:`CLAUDE_WRITE_PATH_KEYS`. Matched casefolded.
CLAUDE_WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

#: Where a Claude write tool's path arrives: ``file_path`` (``Edit``, ``Write``, ``MultiEdit``) and
#: ``notebook_path`` (``NotebookEdit``).
CLAUDE_WRITE_PATH_KEYS = ("file_path", "notebook_path")

#: The tools judged as a shell command under ``--host claude``, by the Codex shell judge, on ``tool_input.command``:
#: ``Bash``, and ``Monitor``, which runs its script in Bash's shell (one watching a WebSocket carries no
#: ``command`` and is denied unjudged). Matched casefolded.
CLAUDE_SHELL_TOOLS = frozenset({"Bash", "Monitor"})

#: The tools denied whatever they carry in a guarded project: Claude Code's Windows shell tool, whose text is
#: PowerShell, which the shell judge does not read. Matched casefolded.
CLAUDE_DENIED_TOOLS = frozenset({"PowerShell"})

_CLAUDE_WRITE_FOLDED = frozenset(tool.casefold() for tool in CLAUDE_WRITE_TOOLS)
_CLAUDE_DENIED_FOLDED = frozenset(tool.casefold() for tool in CLAUDE_DENIED_TOOLS)

#: Every tool name the Claude Code dialect judges beyond the read and search tables, casefolded (a judged tool that
#: is neither a write nor a denied one is a shell tool).
_CLAUDE_JUDGED_FOLDED = _CLAUDE_WRITE_FOLDED | _CLAUDE_DENIED_FOLDED | {tool.casefold() for tool in CLAUDE_SHELL_TOOLS}

#: A word that Git Bash reads as a drive path: ``/c``, ``/c/`` or ``/c/rest`` (:func:`_msys_drive_path`).
_MSYS_DRIVE = re.compile(r"/([A-Za-z])(?:/(.*))?", re.DOTALL)


def _msys_drive_path(word: str) -> str | None:
    """*word* as the drive path Git Bash (MSYS) reads it, or ``None``.

    ``/c/x`` is ``C:/x`` and ``/c`` is ``C:/``; ``None`` for a word that names no drive that way (``/usr``, ``/tmp``,
    ``//c/x``, ``/cd/x``). The same on every host."""
    match = _MSYS_DRIVE.fullmatch(word)
    if match is None:
        return None
    return f"{match.group(1).upper()}:/{match.group(2) or ''}"


def _msys_readings(part: str) -> list[str]:
    """The drive-path readings (:func:`_msys_drive_path`) of every brace expansion of the word part *part*."""
    expansions = _brace_expansions(part, sequences=True) or [part]
    return [reading for expansion in expansions if (reading := _msys_drive_path(expansion)) is not None]


def _is_claude_judged(tool: str) -> bool:
    """Whether *tool* is judged under ``--host claude``: a read or search tool, a write, shell or denied tool."""
    return _is_judged(tool) or tool.casefold() in _CLAUDE_JUDGED_FOLDED


def _decide_claude_call(
    tool: str,
    arguments: Mapping[str, object] | None,
    payload: Mapping[str, object],
    working_directory: Path,
    guarded: GuardedProject,
) -> Decision:
    """The Claude Code answer to one judged call (``docs/guide/model-guard.md``, *Under Claude Code*)."""
    if not guarded.present:
        return Decision(allow=True)
    folded = tool.casefold()
    if folded in _CLAUDE_DENIED_FOLDED:
        return Decision(allow=False, reason=_powershell_reason())
    directory = payload_directory(payload, working_directory)
    if _is_judged(tool):
        is_read = folded in _READ_FOLDED
        path_keys = READ_PATH_KEYS if is_read else SEARCH_PATH_KEYS
        glob_keys = _GLOB_KEYS_FOLDED.get(folded, ())
        keys = path_keys + glob_keys
        readings = [arguments] if arguments is None else _tilde_readings(arguments, keys, glob_keys=glob_keys)
        if readings is None:
            if guarded.project is None:
                return Decision(allow=False, reason=_unreadable_manifest_reason(guarded))
            return Decision(allow=False, reason=tilde_paths.tilde_reason())
        for reading in readings:
            decision = _decide_call(tool, reading, payload, working_directory, guarded)
            if not decision.allow and guarded.project is None:
                reason = (decision.reason or "").rstrip(".")
                return Decision(allow=False, reason=f"{reason}; {_FIX_THE_MANIFEST_BY_HAND}.")
            if decision.allow and guarded.project is not None and reading is not None:
                decision = _decide_handoff_read(is_read, path_keys, glob_keys, reading, directory)
            if not decision.allow:
                return decision
        return Decision(allow=True)
    is_write = folded in _CLAUDE_WRITE_FOLDED
    if guarded.project is None:
        # No edit or command is allowed, ``swcraft.yaml``'s own included: the user fixes it.
        return Decision(allow=False, reason=_unreadable_manifest_reason(guarded))
    project = guarded.project
    if arguments is None:
        return Decision(allow=False, reason=_claude_unjudged_reason(project, is_write))
    given = arguments
    protected = _guard_inputs(guarded.manifest)
    if protected is None:
        return Decision(allow=False, reason=_guard_folder_reason())
    handoff, home = _handoff_directory(), _home_directory()
    if handoff is None or home is None:
        return Decision(allow=False, reason=_handoff_unknown_reason())
    if is_write:
        readings = _tilde_readings(given, CLAUDE_WRITE_PATH_KEYS)
        if readings is None:
            return Decision(allow=False, reason=tilde_paths.tilde_reason())
        return _first_denial(
            _decide_claude_write(reading, directory, project, protected, handoff, home=home) for reading in readings
        )
    return _decide_shell(
        given.get("command"),
        None,
        directory,
        project,
        protected,
        handoff=handoff,
        msys=_SHELL_WORDS_HAVE_DRIVES,
        home=home,
    )


def _handoff_directory() -> str | None:
    """The review hand-off directory, ``<SWCraft home>/reviews``, resolved, or ``None``.

    From :mod:`swcraft_tools.swcraft_home`, resolved as the manifest's roots are, whether it exists or not; ``None``
    when the home cannot be determined."""
    try:
        return resolved_path(reviews_location())
    except (OSError, ValueError, RuntimeError):
        return None


def _home_directory() -> str | None:
    """The user's SWCraft home, resolved, or ``None`` when it cannot be determined.

    The home is the folder holding the hand-off directory and the ``swcraft`` the hook runs."""
    try:
        return resolved_path(reviews_location().parent)
    except (OSError, ValueError, RuntimeError):
        return None


def _decide_handoff_read(
    is_read: bool,
    path_keys: tuple[str, ...],
    glob_keys: tuple[str, ...],
    arguments: Mapping[str, object],
    directory: Path,
    *,
    narrow: bool = False,
) -> Decision:
    """A read or search the Copilot rules allowed, judged against the hand-off directory.

    A read path inside it, or a search root that overlaps it (at it, inside it, or above it), is denied."""
    handoff = _handoff_directory()
    if handoff is None:
        return Decision(allow=False, reason=_handoff_unknown_reason())
    written = _written_paths(arguments, path_keys)
    targets = [None if path is None else _resolve(directory, path) for path in written]
    if is_read:
        hit = any(target is not None and path_contains(handoff.casefold(), target.casefold()) for target in targets)
    else:
        roots = _search_roots(targets, glob_keys, arguments, directory, narrow)
        hit = any(root is not None and paths_overlap(handoff, root) for root in roots)
    return Decision(allow=False, reason=_handoff_reason()) if hit else Decision(allow=True)


def _decide_claude_write(
    arguments: Mapping[str, object],
    directory: Path,
    project: SwcraftProject,
    protected: tuple[str, ...],
    handoff: str,
    keys: tuple[str, ...] = CLAUDE_WRITE_PATH_KEYS,
    *,
    deletes: bool = False,
    home: str | None = None,
) -> Decision:
    """Deny a write that reaches the model, a guard input, a folder holding either or the hand-off directory.

    Denied when any path under *keys* resolves into ``model_root``, onto a guard input or a folder strictly between the
    project directory and one (resolved or as named), onto such a folder of the model root (:func:`_model_ancestors`),
    or into the hand-off directory, or cannot be judged; a write naming no path is denied too. With *deletes* (Kiro's
    ``delete_file``) a path that is the project directory or a folder holding it or the model root, or a folder holding
    the hand-off directory (no filesystem root), is denied as well. With *home*, a path in it, and under *deletes* a
    folder holding it (no filesystem root), is denied; the hand-off reason comes first. Last,
    :func:`_model_path_decision` judges each path."""
    written = _written_paths(arguments, keys)
    base = _resolve(directory, ".")
    if not written or base is None:
        return Decision(allow=False, reason=_claude_unjudged_reason(project, True))
    model_root = project.resolved_model_root.casefold()
    project_root = project.resolved_project_root.casefold()
    ancestors = _guard_input_ancestors(protected, project.resolved_project_root)
    model_folders = _model_ancestors(project)
    for path in written:
        resolved = None if path is None else _resolve(directory, path)
        if path is None or resolved is None:
            return Decision(allow=False, reason=_claude_unjudged_reason(project, True))
        folded = resolved.casefold()
        if path_contains(model_root, folded):
            name = project_relative_name(project, resolved) or "the model directory"
            return Decision(allow=False, reason=_claude_model_write_reason(project, name))
        forms = (folded, _lexical(base, path).casefold())
        if any(path_contains(target.casefold(), form) for target in protected for form in forms) or any(
            form in ancestors for form in forms
        ):
            return Decision(allow=False, reason=_guard_input_reason("edit"))
        if any(form in model_folders for form in forms) or (
            deletes and any(path_contains(form, project_root) or path_contains(form, model_root) for form in forms)
        ):
            return Decision(allow=False, reason=_model_folder_reason(project, "edit"))
        if any(path_contains(handoff.casefold(), form) for form in forms) or (
            deletes and any(_holds_handoff(form, handoff) for form in forms)
        ):
            return Decision(allow=False, reason=_handoff_reason())
        if home is not None and (
            any(path_contains(home.casefold(), form) for form in forms)
            or (deletes and any(_holds_handoff(form, home) for form in forms))
        ):
            return Decision(allow=False, reason=_home_reason())
    return _model_path_decision(project, directory, written)


def _holds_handoff(folded: str, handoff: str) -> bool:
    """Whether *folded* (a casefolded path) is a folder strictly above *handoff* and no filesystem root.

    A root holds the project and is refused on that ground."""
    folded_handoff = handoff.casefold()
    return folded != folded_handoff and path_contains(folded, folded_handoff) and Path(folded).parent != Path(folded)


def _handoff_reason() -> str:
    """The denial of a call reaching the review hand-off directory; it names no location."""
    return (
        "SWCraft: the SWCraft review hand-off directory (`<SWCraft home>/reviews`) belongs to the review tab; no "
        "agent tool reads or writes it, so this call is refused. A model change is reviewed through the SWCraft MCP "
        "tool `change_set`."
    )


def _home_reason() -> str:
    """The denial of a write, removal or command reaching the SWCraft home; it names no location."""
    return (
        "SWCraft: the SWCraft installation folder (`<SWCraft home>`) belongs to SWCraft; no agent tool writes or "
        "removes it, so this call is refused. The user updates or reinstalls SWCraft."
    )


def _handoff_unknown_reason() -> str:
    """The denial of a judged call when the user's SWCraft home cannot be determined (fail closed)."""
    return (
        "SWCraft: the user's SWCraft home cannot be determined, so whether this call reaches the SWCraft "
        "installation folder or its review hand-off directory cannot be judged; it is refused."
    )


def _powershell_reason() -> str:
    """The denial of Claude Code's ``PowerShell`` tool in a guarded project, whatever it runs."""
    return (
        "SWCraft: in this project the PowerShell tool is refused: the model guard reads bash, not PowerShell, so "
        "what this command touches cannot be judged. Use the Bash tool, or the SWCraft tools for the model."
    )


#: What the Codex and Claude Code dialects tell an agent to do about an unreadable ``swcraft.yaml``: the user fixes it
#: by hand, never the agent.
_FIX_THE_MANIFEST_BY_HAND = "ask the user to fix swcraft.yaml by hand in the editor"


def _unreadable_manifest_reason(guarded: GuardedProject) -> str:
    """The denial of every judged Codex and Claude Code call while ``swcraft.yaml`` cannot be read.

    It denies every Codex patch or shell command and every Claude Code edit or command: no repair is allowed, the
    manifest's own included."""
    return (
        f"SWCraft: this project's swcraft.yaml cannot be read ({guarded.problem}), so its model directory "
        "is not known and every edit and shell command is refused, swcraft.yaml's own included, until the manifest "
        f"is fixed; {_FIX_THE_MANIFEST_BY_HAND}."
    )


def _claude_model_write_reason(project: SwcraftProject, name: str) -> str:
    """The denial of a Claude Code edit that writes *name*, a path in the model directory."""
    return (
        f"SWCraft: in this project {_model_subject(project)} is changed through the SWCraft tools, whose "
        f"changes the panel reviews; this edit writes `{name}` there. Use the SWCraft MCP tool "
        "`change_set` instead."
    )


def _claude_unjudged_reason(project: SwcraftProject, is_write: bool) -> str:
    """The denial of a Claude Code edit or command the guard cannot read, so cannot judge (fail closed)."""
    if is_write:
        return (
            f"SWCraft: in this project {_model_subject(project)} is changed through the SWCraft tools, whose changes "
            "the panel reviews; this edit names no path the guard can read, so what it writes cannot be judged. Use "
            "the SWCraft MCP tool `change_set` for a model change."
        )
    return _unjudged_reason(project, False)


# ---------------------------------------------------------------------------
# The Kiro dialect
# ---------------------------------------------------------------------------

#: The folder Kiro reads a workspace's hooks from; with ``swcraft.yaml`` beside it, it marks the project the Kiro
#: dialect guards.
KIRO_HOOKS_FOLDER = Path(".kiro") / "hooks"


@dataclass(frozen=True)
class KiroTool:
    """One Kiro tool the dialect judges: its *kind* and its closed argument schema.

    Every key a call carries must be one of these, whatever its value; a path or glob key holds a string or null;
    *context_file_keys* hold a list of context-file objects or null, each file's ``path`` judged as a read; the other
    keys are never read as paths. *deletes* marks a write tool that only removes (``delete_file``)."""

    kind: str
    path_keys: tuple[str, ...] = ()
    glob_keys: tuple[str, ...] = ()
    other_keys: tuple[str, ...] = ()
    context_file_keys: tuple[str, ...] = ()
    deletes: bool = False


#: The kinds of :class:`KiroTool`: a one-file read, a directory listing (a read at one level, a search deeper), a
#: search, the ``code`` tool (a search on its paths, a closed set of operations), a write, a shell command, a
#: sub-agent (its context files read), a web fetch (an ``http``/``https`` URL only), and a tool denied whatever it
#: carries.
_KIRO_READ, _KIRO_LIST, _KIRO_SEARCH, _KIRO_CODE = "read", "list", "search", "code"
_KIRO_WRITE, _KIRO_SHELL, _KIRO_SUBAGENT, _KIRO_DENIED = "write", "shell", "subagent", "denied"
_KIRO_FETCH = "fetch"

#: The tools the Kiro dialect judges, by Kiro's tool ID, each with what observed it. "Captured" is a captured hook
#: payload (kiro-cli 3.1.0, KAS 0.66.91): Kiro sends every declared argument, an absent one as null. Matched
#: casefolded.
KIRO_JUDGED_TOOLS: dict[str, KiroTool] = {
    # Captured: {path, offset, limit}, the path relative or absolute.
    "read_file": KiroTool(_KIRO_READ, path_keys=("path",), other_keys=("offset", "limit")),
    # Captured: {path, explanation, depth}; 3.1.0 lists one level whatever the depth.
    "list_directory": KiroTool(_KIRO_LIST, path_keys=("path",), other_keys=("explanation", "depth")),
    # Captured: no root key; ``includePattern`` a glob, ``excludePattern`` an exclusion, never a root.
    "grep_search": KiroTool(
        _KIRO_SEARCH,
        glob_keys=("includePattern",),
        other_keys=("query", "caseSensitive", "excludePattern", "explanation"),
    ),
    # Captured: no root key and no include glob; ``query`` is a substring of a path, over the whole workspace.
    "file_search": KiroTool(_KIRO_SEARCH, other_keys=("explanation", "query", "excludePattern", "includeIgnoredFiles")),
    # Captured (``search_symbols``): its paths ``path`` and ``file_path``; its operations KAS 0.66.91's enum.
    "code": KiroTool(
        _KIRO_CODE,
        path_keys=("path", "file_path"),
        other_keys=(
            "operation",
            "symbol_name",
            "symbols",
            "symbol_type",
            "limit",
            "exact_match",
            "language",
            "include_source",
            "top_level_only",
            "pattern",
            "replacement",
            "row",
            "column",
            "new_name",
            "trigger_character",
            "tab_size",
            "insert_spaces",
        ),
    ),
    # Captured: {path, text}.
    "fs_write": KiroTool(_KIRO_WRITE, path_keys=("path",), other_keys=("text",)),
    # Captured: {path, text}.
    "fs_append": KiroTool(_KIRO_WRITE, path_keys=("path",), other_keys=("text",)),
    # Captured: {path, oldStr, newStr, replace_all}.
    "str_replace": KiroTool(_KIRO_WRITE, path_keys=("path",), other_keys=("oldStr", "newStr", "replace_all")),
    # Captured: {explanation, targetFile} -- its path under ``targetFile``, not ``path``.
    "delete_file": KiroTool(_KIRO_WRITE, path_keys=("targetFile",), other_keys=("explanation",), deletes=True),
    # Captured: {command, description, cwd, run_in_background, timeout}; ``cwd`` absolute, the project.
    "execute_bash": KiroTool(_KIRO_SHELL, other_keys=("command", "description", "cwd", "run_in_background", "timeout")),
    # Captured: {name, prompt, explanation, preset, contextFiles, specTask}; a context file {path, startLine,
    # endLine} (KAS's schema), read into the sub-agent by Kiro itself, so each path is judged as a read.
    "invoke_sub_agent": KiroTool(
        _KIRO_SUBAGENT,
        other_keys=("name", "prompt", "explanation", "preset", "specTask"),
        context_file_keys=("contextFiles",),
    ),
    # Not captured; KAS 0.66.91's schema (``web_fetch``): {url, mode, searchPhrase}. Whether it reads a ``file:``
    # URL was not checked, so only an ``http``/``https`` URL is allowed (:func:`_decide_kiro_fetch`).
    "web_fetch": KiroTool(_KIRO_FETCH, other_keys=("url", "mode", "searchPhrase")),
    # Not captured; KAS 0.66.91: starts a command as a background process or stops one -- a second shell the guard
    # does not judge; ``execute_bash`` with ``run_in_background`` is the judged way.
    "control_bash_process": KiroTool(_KIRO_DENIED),
    "control_pwsh_process": KiroTool(_KIRO_DENIED),
    # The V3 catalog's Windows shell (https://kiro.dev/docs/tools/): PowerShell, which the shell judge does not read.
    "execute_pwsh": KiroTool(_KIRO_DENIED),
    # The V2 engine's Windows shell (the hooks migration page): cmd, which the shell judge does not read.
    "execute_cmd": KiroTool(_KIRO_DENIED),
    # The V2 engine's file read: paths nested in ``operations[]``; no V2 payload was captured, so it is denied. V2's
    # ``fs_write`` shares V3's name and carries ``command``/``file_text``, which V3's schema refuses.
    "fs_read": KiroTool(_KIRO_DENIED),
}

_KIRO_JUDGED_FOLDED = {tool.casefold(): spec for tool, spec in KIRO_JUDGED_TOOLS.items()}

_KIRO_WINDOWS_SHELLS = frozenset({"execute_pwsh", "execute_cmd"})

_KIRO_PROCESS_STARTERS = frozenset({"control_bash_process", "control_pwsh_process"})

#: The URL schemes ``web_fetch`` is allowed with, matched casefolded at the URL's start.
_KIRO_FETCH_SCHEMES = ("http://", "https://")

#: The ``code`` operations allowed, judged as a search on the call's paths: the operations of KAS 0.66.91's enum
#: that change no file. ``rename_symbol``, ``format_code`` and ``pattern_rewrite`` edit files beyond the path they
#: name and are denied, as is an operation this release does not know.
KIRO_CODE_OPERATIONS = frozenset(
    {
        "search_symbols",
        "lookup_symbols",
        "get_document_symbols",
        "generate_codebase_overview",
        "search_codebase_map",
        "pattern_search",
        "goto_definition",
        "find_references",
        "hover",
        "completion",
        "diagnostics",
    }
)

#: A context file's keys (KAS 0.66.91's ``contextFiles`` schema): any other key makes the call unjudgeable.
_KIRO_CONTEXT_FILE_KEYS = frozenset({"path", "startLine", "endLine"})

#: The built-in tools that touch no file, allowed by name -- exactly, never casefolded: ``tool_load`` (captured;
#: loads a deferred MCP tool), ``web_search`` and ``remote_web_search`` (the V3 catalog, tag ``web``),
#: ``introspect`` (answers from Kiro's documentation), ``report_progress`` and ``update_session_information`` (KAS:
#: a message, a session label), ``switch_to_execution`` (KAS: hands a plan to execution), ``user_input`` (KAS: asks
#: the user), ``tool_search`` (finds MCP tools), and ``get_process_output`` and ``list_processes`` (KAS 0.66.91:
#: ``{terminalId, lines}`` and ``{}``, no file named). Every other tool not in :data:`KIRO_JUDGED_TOOLS` and not an
#: MCP tool is denied in a guarded project: Kiro updates itself, and a renamed or new file tool must not pass
#: unjudged.
KIRO_ALLOWED_TOOLS = frozenset(
    {
        "tool_load",
        "get_process_output",
        "list_processes",
        "web_search",
        "remote_web_search",
        "introspect",
        "report_progress",
        "update_session_information",
        "switch_to_execution",
        "user_input",
        "tool_search",
    }
)

#: The prefix of every MCP server's tool as Kiro's hook sees it, ``mcp_<server>_<tool>`` (captured): allowed,
#: SWCraft's own and another server's alike -- another server's file tools are not reached.
KIRO_MCP_PREFIX = "mcp_"

_KIRO_SEARCH_REMEDY = (
    "Narrow it to a folder outside the model -- for a text search an include pattern such as `notes/**` -- or use "
    "the SWCraft tools."
)

#: A tool name a reason may quote: a Kiro tool ID's characters, at most 64 of them, no path separator.
_QUOTABLE_TOOL = re.compile(r"[A-Za-z0-9_.:@-]{1,64}")


def _kiro_allowed(tool: str) -> bool:
    """Whether *tool* is allowed by name under Kiro: one of :data:`KIRO_ALLOWED_TOOLS`, or an MCP tool."""
    return tool in KIRO_ALLOWED_TOOLS or (tool.startswith(KIRO_MCP_PREFIX) and len(tool) > len(KIRO_MCP_PREFIX))


def _is_kiro_judged(tool: str) -> bool:
    """Whether *tool* is the Kiro dialect's to judge: every tool but the ones allowed by name."""
    return not _kiro_allowed(tool)


def _kiro_arguments_judgeable(spec: KiroTool, arguments: Mapping[str, object]) -> bool:
    """Whether *arguments* keep to *spec*'s closed schema.

    Every key declared, whatever its value's type; a path or glob key a string or null, a glob one a single pattern
    (:func:`_kiro_single_glob`); a context-file key null or a list of context-file objects, each holding only
    :data:`_KIRO_CONTEXT_FILE_KEYS` and a non-empty string ``path``."""
    declared = {*spec.path_keys, *spec.glob_keys, *spec.other_keys, *spec.context_file_keys}
    for key, value in arguments.items():
        if key not in declared:
            return False
        if (key in spec.path_keys or key in spec.glob_keys) and value is not None and not isinstance(value, str):
            return False
        if key in spec.glob_keys and isinstance(value, str) and not _kiro_single_glob(value):
            return False
        if key in spec.context_file_keys and value is not None:
            if not isinstance(value, list):
                return False
            for member in value:
                if not isinstance(member, dict) or set(member) - _KIRO_CONTEXT_FILE_KEYS:
                    return False
                if not isinstance(member.get("path"), str) or not member["path"]:
                    return False
    return True


def _kiro_single_glob(pattern: str) -> bool:
    """Whether *pattern* is one glob the roots can be read from.

    No whitespace anywhere and no ``,`` outside a brace group (``notes/**,arch/**`` is a list of patterns). A brace left
    unbalanced counts as no group."""
    depth = 0
    for character in pattern:
        if character.isspace():
            return False
        if character == "{":
            depth += 1
        elif character == "}" and depth:
            depth -= 1
        elif character == "," and not depth:
            return False
    return True


def _decide_kiro_call(
    tool: object,
    arguments: Mapping[str, object] | None,
    payload: Mapping[str, object],
    working_directory: Path,
    guarded: GuardedProject,
) -> Decision:
    """The Kiro answer to one call, judged against both anchors.

    The rule is ``docs/guide/model-guard.md``, *Under Kiro*. The anchors are the payload's ``cwd`` and the guarded
    project's own directory, a denial from either winning. The second judgement is skipped when the two are one folder.
    """
    decision = _decide_kiro_call_at(tool, arguments, payload, working_directory, guarded)
    if not decision.allow or guarded.manifest is None:
        return decision
    anchor = _resolve(guarded.manifest.parent, ".")
    if anchor is None or anchor == _resolve(payload_directory(payload, working_directory), "."):
        return decision
    return _decide_kiro_call_at(tool, arguments, {**payload, "cwd": anchor}, working_directory, guarded)


def _decide_kiro_call_at(
    tool: object,
    arguments: Mapping[str, object] | None,
    payload: Mapping[str, object],
    working_directory: Path,
    guarded: GuardedProject,
) -> Decision:
    """:func:`_decide_kiro_call` against the one anchor the payload's ``cwd`` names.

    In a guarded project a tool that cannot be named, an unknown tool and a denied one are refused; a tabled tool's
    arguments must keep to its closed schema; then the read, search, write, shell or fetch rule of its kind."""
    if not guarded.present:
        return Decision(allow=True)
    if not isinstance(tool, str):
        return Decision(allow=False, reason=_kiro_unnamed_reason())
    if _kiro_allowed(tool):
        return Decision(allow=True)
    spec = _KIRO_JUDGED_FOLDED.get(tool.casefold())
    if spec is None:
        return Decision(allow=False, reason=_kiro_unknown_reason(tool))
    if spec.kind == _KIRO_DENIED:
        return Decision(allow=False, reason=_kiro_denied_reason(tool))
    project = guarded.project
    if arguments is None or not _kiro_arguments_judgeable(spec, arguments):
        return _kiro_unjudged_decision(guarded)
    if spec.kind == _KIRO_FETCH:
        # Under an unreadable manifest every judged call is denied, an http(s) fetch included (fail closed).
        if project is None:
            return Decision(allow=False, reason=_unreadable_manifest_reason(guarded))
        return _decide_kiro_fetch(arguments)
    tilde_keys = spec.path_keys + spec.glob_keys + (("cwd",) if spec.kind == _KIRO_SHELL else ())
    directory = str(payload_directory(payload, working_directory).absolute())
    forms = _kiro_forms(spec, _kiro_folder_names(payload, working_directory, guarded), directory)
    readings = _tilde_readings(arguments, tilde_keys, spec.context_file_keys, forms, spec.glob_keys)
    if readings is None:
        return (
            _kiro_unjudged_decision(guarded)
            if project is None
            else Decision(allow=False, reason=tilde_paths.tilde_reason())
        )
    return _first_denial(
        _decide_kiro_reading(spec, reading, payload, working_directory, guarded) for reading in readings
    )


def _kiro_folder_names(
    payload: Mapping[str, object], working_directory: Path, guarded: GuardedProject
) -> frozenset[str]:
    """The workspace folder names the guard knows, casefolded.

    The project directory's and the payload ``cwd``'s, each by its name **as given** (Kiro compares the basename with no
    realpath) **and** by its resolved name. A root gives none. Kiro's other workspace folders never reach the hook."""
    folders = [payload_directory(payload, working_directory)]
    if guarded.manifest is not None:
        folders.append(guarded.manifest.parent)
    names = [name for folder in folders for name in (folder.name, Path(_resolve(folder, ".") or folder).name)]
    return frozenset(name.casefold() for name in names if name)


def _kiro_forms(spec: KiroTool, names: frozenset[str], directory: str) -> PathForms:
    """Every reading Kiro's tool service may give one written path of a *spec* call, the literal one first.

    The readings are ``docs/guide/model-guard.md``, *The paths Kiro reads more ways*; ``None`` for a ``~name`` form."""

    def steps(path: str) -> tuple[str, ...]:
        dotted = kiro_paths.dotted_reading(path, directory)
        return kiro_paths.uri_drive_reading(path) + (() if dotted is None else (dotted,))

    def forms(key: str | None, value: str) -> tuple[str, ...] | None:
        fallback = kiro_paths.context_fallback(value) if key is None else None
        if key is not None and spec.kind == _KIRO_READ and key in spec.path_keys:
            fallback = kiro_paths.read_fallback(value)
        written = [value] + ([] if fallback is None else [fallback])
        named = [name for path in written for name in kiro_paths.workspace_folder_readings(path, names)]
        readings = tilde_paths.tilde_forms(value)
        return None if readings is None else tilde_paths.derived_closure(readings, written[1:] + named, steps)

    return forms


def _decide_kiro_reading(
    spec: KiroTool,
    arguments: Mapping[str, object],
    payload: Mapping[str, object],
    working_directory: Path,
    guarded: GuardedProject,
) -> Decision:
    """:func:`_decide_kiro_call_at`'s rule of *spec*'s kind for one reading of the call's arguments.

    The kind is read, search, write or shell."""
    project = guarded.project
    directory = payload_directory(payload, working_directory)
    if spec.kind not in (_KIRO_WRITE, _KIRO_SHELL):
        return _decide_kiro_read(spec, arguments, payload, working_directory, guarded, directory)
    if project is None:
        return Decision(allow=False, reason=_unreadable_manifest_reason(guarded))
    protected = _guard_inputs(guarded.manifest)
    if protected is None:
        return Decision(allow=False, reason=_guard_folder_reason())
    handoff, home = _handoff_directory(), _home_directory()
    if handoff is None or home is None:
        return Decision(allow=False, reason=_handoff_unknown_reason())
    if spec.kind == _KIRO_WRITE:
        return _decide_claude_write(
            arguments,
            directory,
            project,
            protected,
            handoff,
            spec.path_keys,
            deletes=spec.deletes,
            home=home,
        )
    # ``cwd`` follows Codex's ``workdir`` rule: null is the payload's ``cwd``; one in the model denies.
    decision = _decide_shell(
        arguments.get("command"),
        arguments.get("cwd"),
        directory,
        project,
        protected,
        handoff=handoff,
        msys=_SHELL_WORDS_HAVE_DRIVES,
        home=home,
    )
    return decision if not decision.allow else _model_path_decision(project, directory, [arguments.get("cwd")])


def _decide_kiro_read(
    spec: KiroTool,
    arguments: Mapping[str, object],
    payload: Mapping[str, object],
    working_directory: Path,
    guarded: GuardedProject,
    directory: Path,
) -> Decision:
    """A Kiro read, listing, search, ``code`` or sub-agent call.

    Copilot's rule with the search rooted at its include pattern, then the hand-off rule; a read naming no path is
    denied, and under an unreadable manifest the reason asks for the fix by hand."""
    project = guarded.project
    path_keys, glob_keys, given = spec.path_keys, spec.glob_keys, arguments
    if spec.kind == _KIRO_CODE:
        operation = arguments.get("operation")
        if not isinstance(operation, str) or operation not in KIRO_CODE_OPERATIONS:
            return Decision(allow=False, reason=_kiro_code_reason(operation))
        is_read = False
    elif spec.kind == _KIRO_LIST:
        depth = arguments.get("depth")
        if depth is not None and (isinstance(depth, bool) or not isinstance(depth, int)):
            return _kiro_unjudged_decision(guarded)
        is_read = depth is None or depth == 1  # one level: a read; deeper: a search
    elif spec.kind == _KIRO_SUBAGENT:
        files = [
            member
            for key in spec.context_file_keys
            for member in required_instance(arguments.get(key) or [], list, "_kiro_arguments_judgeable admits a list")
        ]
        if not files:
            if project is None:
                return Decision(allow=False, reason=_unreadable_manifest_reason(guarded))
            return Decision(allow=True)
        path_keys, glob_keys, given = ("path",), (), {"path": [member["path"] for member in files]}
        is_read = True
    else:
        is_read = spec.kind == _KIRO_READ
    if is_read and not _written_paths(given, path_keys):
        return _kiro_unjudged_decision(guarded)
    decision = _decide_read_or_search(
        is_read,
        path_keys,
        glob_keys,
        given,
        payload,
        working_directory,
        guarded,
        narrow=True,
        search_remedy=_KIRO_SEARCH_REMEDY,
    )
    if not decision.allow and project is None:
        return Decision(allow=False, reason=f"{(decision.reason or '').rstrip('.')}; {_FIX_THE_MANIFEST_BY_HAND}.")
    if not decision.allow or project is None:
        return decision
    return _decide_handoff_read(is_read, path_keys, glob_keys, given, directory, narrow=True)


def _decide_kiro_fetch(arguments: Mapping[str, object]) -> Decision:
    """``web_fetch``: allowed only with an ``http`` or ``https`` URL.

    Allowed when its ``url`` is a string opening with ``http://`` or ``https://`` (casefolded), denied otherwise in a
    guarded project -- a ``file:`` URL, another scheme, a URL with leading blanks, or none -- because whether Kiro's
    fetch reads a local file was not checked."""
    url = arguments.get("url")
    if isinstance(url, str) and url.casefold().startswith(_KIRO_FETCH_SCHEMES):
        return Decision(allow=True)
    return Decision(
        allow=False,
        reason=(
            "SWCraft: in this project web_fetch is allowed for an http:// or https:// URL only: what another URL "
            "reads cannot be judged, so this call is refused. Read a project file with read_file, or the model "
            "through the SWCraft tools."
        ),
    )


def _kiro_malformed_decision() -> Decision:
    """Kiro's answer to a payload that is not a JSON object.

    Denied whenever the hook's working directory is in a guarded project or that cannot be ruled out, allowed when it is
    determinably in none."""
    try:
        present = bool(_project_directories(KIRO, Path.cwd()))
    except Exception:  # noqa: BLE001 -- hook: what cannot be ruled out is denied
        present = True
    return Decision(allow=False, reason=_kiro_unnamed_reason()) if present else Decision(allow=True)


def _kiro_quoted(name: object, template: str, fallback: str) -> str:
    """*name* quoted into *template* (``{}`` its place) for a reason, or *fallback*.

    *name* is quoted when it is a string that looks like a tool ID (:data:`_QUOTABLE_TOOL`); a name that does not is
    never echoed."""
    return template.format(name) if isinstance(name, str) and _QUOTABLE_TOOL.fullmatch(name) else fallback


def _kiro_tool_name(tool: str) -> str:
    """*tool* quoted for a reason when it looks like a tool ID (:func:`_kiro_quoted`), else ``this tool``."""
    return _kiro_quoted(tool, "the tool `{}`", "this tool")


def _kiro_unnamed_reason() -> str:
    """The denial of a Kiro hook call the guard cannot read as one tool call."""
    return (
        "SWCraft: the model guard cannot read this hook call as a JSON object naming its tool, so in this project it "
        "is refused; use the SWCraft tools."
    )


def _kiro_unknown_reason(tool: str) -> str:
    """The denial of a tool this release's guard does not know, in a guarded project."""
    return (
        f"SWCraft: in this project {_kiro_tool_name(tool)} is not one this release's model guard knows, so what it "
        "touches cannot be judged and it is refused; use the SWCraft tools for the model, or a file tool the guard "
        "knows (read_file, fs_write, str_replace, execute_bash)."
    )


def _kiro_denied_reason(tool: str) -> str:
    """The denial of a Kiro tool refused whatever it carries.

    A Windows shell, a background-process starter, or a file tool of Kiro's V2 engine."""
    if tool.casefold() in _KIRO_PROCESS_STARTERS:
        return (
            f"SWCraft: in this project {_kiro_tool_name(tool)} is refused: the command it starts is not one the model "
            "guard judges. Use execute_bash with run_in_background, then get_process_output, or the SWCraft tools "
            "for the model."
        )
    if tool.casefold() in _KIRO_WINDOWS_SHELLS:
        return (
            f"SWCraft: in this project {_kiro_tool_name(tool)} is refused: the model guard reads bash, not PowerShell "
            "or cmd, so what this command touches cannot be judged. Use execute_bash, or the SWCraft tools for the "
            "model."
        )
    return (
        f"SWCraft: in this project {_kiro_tool_name(tool)} of Kiro's earlier engine is refused: the model guard "
        "cannot judge the paths it carries. Use read_file, fs_write or str_replace, or the SWCraft tools for the "
        "model."
    )


def _kiro_code_reason(operation: object) -> str:
    """The denial of a ``code`` call whose operation can change files, or is not one this release knows."""
    named = _kiro_quoted(operation, "the code tool's `{}` operation", "this code operation")
    return (
        f"SWCraft: in this project {named} is refused: it can change files beyond the path it names, or is not one "
        "the model guard knows, so what it touches cannot be judged. Use the SWCraft tools "
        "for the model, or str_replace on one file."
    )


def _kiro_unjudged_decision(guarded: GuardedProject) -> Decision:
    """The denial of a Kiro call whose arguments cannot be judged.

    The unreadable manifest's reason when the project's ``swcraft.yaml`` cannot be read, else
    :func:`_kiro_unjudged_reason`."""
    project = guarded.project
    reason = _unreadable_manifest_reason(guarded) if project is None else _kiro_unjudged_reason(project)
    return Decision(allow=False, reason=reason)


def _kiro_unjudged_reason(project: SwcraftProject) -> str:
    """The denial of a tabled Kiro call whose arguments leave its closed schema, or name no path (fail closed)."""
    return (
        f"SWCraft: in this project {_model_subject(project)} is read and changed through the SWCraft tools, whose "
        "changes the panel reviews; this call's arguments are not the ones the model guard knows for its tool (a key "
        "it does not know, a value of another type, or no path), so what it touches cannot be judged. Use the SWCraft "
        "tools for the model."
    )


#: Per host, the tools its own dialect judges and the function that judges them; ``None`` where every tool is
#: judged as under Copilot.
_JUDGED_BY_HOST: dict[str, tuple[Callable[[str], bool], Callable[..., Decision]] | None] = {
    COPILOT: None,
    CODEX: (_is_codex_judged, _decide_codex_call),
    CLAUDE: (_is_claude_judged, _decide_claude_call),
    KIRO: (_is_kiro_judged, _decide_kiro_call),
}
