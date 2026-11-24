"""Assemble the public part of a commit into a one-commit git repository, and check one.

    python scripts/public_snapshot.py assemble [--repo DIR] [--rev REV] --out DIR
    python scripts/public_snapshot.py check DIR [--repo DIR] [--rev REV] [--deny-list FILE]

The public part of a repository is named by one allow-list at its root, ``public-paths.txt``: one entry per line,
``#`` starts a comment. An entry ending in ``/`` opens a whole folder, recursively; any other entry names a file at
the root only. Everything not listed is closed. An allow-list that is not canonical, exact and non-empty, or that
opens ``.swcraft-open-copy``, is refused, naming why (and an entry's line).

``assemble`` reads ``public-paths.txt`` at REV (never the working tree), extracts ``git archive REV -- <entries>``
into ``--out`` (absent or empty) and commits it as a repository of one commit: author and committer
``SWCraft snapshot <snapshot@invalid>``, both dates REV's committer date, the message ending in
``Source-Commit: <REV's full SHA>``, the source's object format. Beside the tracked files it writes the open-copy
marker ``.swcraft-open-copy``.

``check DIR`` compares ``DIR`` path by path with the allow-list's expansion at REV (``--rev``, or the
``Source-Commit`` trailer of ``DIR``'s ``HEAD``): on disk and committed, mode, blob and kind, object format and a
single commit; each finding names its path. With ``--deny-list FILE`` (``#`` comments) it also fails on any
whole-word, case-insensitive match of an entry (``re:PATTERN`` for a regular expression; one that does not compile
is refused) in a path name or a file's content, a phrase wrapped over two comment lines included; a finding names
the file, the line and the deny-list's line, never the entry, and a matching path segment is reported as
``<sha256:...>``. A missing deny-list file is refused, so a mistyped path cannot switch the scan off. Not reached:
``.gitattributes`` ``export-ignore``/``export-subst`` (``check`` catches the effect, nothing prevents it), the
executable bit on disk, a binary encoding other than UTF-8 or ASCII, a word split by a line break or a separator
inside it, and what an open file may hold beyond the deny-list.

Stdlib only, no shell, Python 3.11.4 or later (``tarfile``'s ``data`` filter; an older interpreter is refused
before anything is written). Exit 0 on success, 1 on a refusal or a finding, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The allow-list's file name, at the root of the repository it governs.
ALLOW_LIST = "public-paths.txt"

#: The fixed identity of a snapshot commit (``.invalid`` is reserved by RFC 2606).
SNAPSHOT_NAME = "SWCraft snapshot"
SNAPSHOT_EMAIL = "snapshot@invalid"
SNAPSHOT_SUBJECT = "Public snapshot"

#: The open copy's marker file and its exact text; ``dev.OPEN_COPY_MARKER``/``OPEN_COPY_MARKER_TEXT``
#: hold the same two values (``scripts/tests/test_open_copy_marker.py``).
OPEN_COPY_MARKER = ".swcraft-open-copy"
OPEN_COPY_MARKER_TEXT = (
    "SWCraft's open copy: scripts/public_snapshot.py writes this file into every snapshot it assembles.\n"
    "scripts/dev.py reads it: the work repository's internal tests are not part of this copy.\n"
)

#: The trailer naming the source commit; ``build_artifacts.read_git_commit`` reads it.
SOURCE_TRAILER = "Source-Commit"

_SHA256_HEX_LENGTH = 64

#: A full lowercase SHA-1 or SHA-256 object id; ``build_artifacts._COMMIT_SHA`` is the same pattern.
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")

#: A word character for the deny-list's whole-word match: letters and digits only, because file
#: names and identifiers use ``_`` and ``-`` as separators.
_WORD_CHAR = r"[^\W_]"

#: Every git call's configuration: no line-ending conversion (Git for Windows defaults to
#: ``core.autocrlf=true``, which would turn LF into CRLF in the archive), no signing, and no
#: automatic maintenance, whose detached background process goes on writing into the repository
#: while the snapshot is read and removed.
_GIT_CONFIG = (
    "-c",
    "core.autocrlf=false",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.quotepath=false",
    "-c",
    "maintenance.auto=false",
    "-c",
    "gc.auto=0",
)


class SnapshotError(Exception):
    """A refusal: the message names the entry, its line, or the path."""


@dataclass(frozen=True)
class Entry:
    """One allow-list entry and the line it came from."""

    text: str
    line: int

    @property
    def is_folder(self) -> bool:
        return self.text.endswith("/")


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def git_env() -> dict[str, str]:
    env = dict(os.environ)
    # Entries are literal paths, never glob patterns.
    env["GIT_LITERAL_PATHSPECS"] = "1"
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    return env


def git(repo: Path, *args: str, env: dict[str, str] | None = None, stdin: bytes | None = None) -> bytes:
    """Run ``git -C repo <args>`` and return stdout; a failure is a :class:`SnapshotError`."""
    command = ["git", *_GIT_CONFIG, "-C", str(repo), *args]
    try:
        result = subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            check=False,
            env=env if env is not None else git_env(),
        )
    except OSError as error:
        raise SnapshotError(f"cannot run git: {error}") from error
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise SnapshotError(f"git {args[0]} failed: {message}")
    return result.stdout


def resolve_commit(repo: Path, rev: str) -> str:
    """REV's full commit SHA."""
    try:
        out = git(repo, "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}")
    except SnapshotError as error:
        raise SnapshotError(f"{rev!r} is not a commit of {repo.name or repo}: {error}") from error
    return out.decode("ascii").strip()


@dataclass(frozen=True)
class TreeItem:
    """One ``git ls-tree -r`` row."""

    mode: str
    kind: str
    oid: str
    path: str


def list_tree(repo: Path, rev: str) -> list[TreeItem]:
    """Every entry of REV's tree, recursively."""
    out = git(repo, "ls-tree", "-r", "-z", "--full-tree", rev)
    items = []
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _, name = record.partition(b"\t")
        mode, kind, oid = meta.decode("ascii").split()
        items.append(TreeItem(mode, kind, oid, name.decode("utf-8", errors="surrogateescape")))
    return items


def object_format(repo: Path) -> str:
    """The repository's object format, ``sha1`` or ``sha256``."""
    return git(repo, "rev-parse", "--show-object-format").decode("ascii").strip()


def commit_date(repo: Path, sha: str) -> str:
    """The committer date of *sha* in git's raw form (``<epoch> <offset>``)."""
    return git(repo, "log", "-1", "--format=%cd", "--date=raw", sha).decode("ascii").strip()


def source_trailer(repo: Path, rev: str = "HEAD") -> str | None:
    """The ``Source-Commit`` trailer's value on REV's message, or ``None``.

    The rule ``build_artifacts.read_git_commit`` applies too: of several such trailers the last wins,
    and one that is not a full lowercase SHA-1 or SHA-256 is ignored.
    """
    out = git(repo, "log", "-1", f"--format=%(trailers:key={SOURCE_TRAILER},valueonly)", rev)
    lines = (line.strip() for line in out.decode("utf-8", errors="replace").splitlines())
    values = [line for line in lines if _COMMIT_SHA.fullmatch(line)]
    return values[-1] if values else None


# ---------------------------------------------------------------------------
# The allow-list
# ---------------------------------------------------------------------------


def parse_allow_list(text: str) -> list[Entry]:
    """The entries of an allow-list's text, each with its line; validates the *form* only."""
    entries: list[Entry] = []
    seen: dict[str, int] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        value = raw.split("#", 1)[0].strip()
        if not value:
            continue
        where = f"{ALLOW_LIST}:{number}: {value!r}"
        segments = (value[:-1] if value.endswith("/") else value).split("/")
        if "\\" in value or any(segment in ("", ".", "..") for segment in segments):
            raise SnapshotError(f"{where} is not a canonical relative path (POSIX separators, no '.', '..' or '//')")
        if not value.endswith("/") and "/" in value:
            raise SnapshotError(f"{where} is a file entry that is not at the root; open its folder instead")
        if value in seen:
            raise SnapshotError(f"{where} duplicates line {seen[value]}")
        seen[value] = number
        entries.append(Entry(value, number))
    folders = [entry for entry in entries if entry.is_folder]
    for inner in entries:
        for outer in folders:
            if inner is not outer and inner.text.startswith(outer.text):
                raise SnapshotError(
                    f"{ALLOW_LIST}:{inner.line}: {inner.text!r} is inside {outer.text!r} (line {outer.line})"
                )
    return entries


def read_allow_list(repo: Path, sha: str) -> list[Entry]:
    """The allow-list committed at *sha*, parsed."""
    try:
        text = git(repo, "show", f"{sha}:{ALLOW_LIST}")
    except SnapshotError as error:
        raise SnapshotError(f"{ALLOW_LIST} is not committed at {sha[:12]}") from error
    entries = parse_allow_list(text.decode("utf-8-sig"))
    if not entries:
        # ``git archive REV --`` with no pathspec exports the whole commit.
        raise SnapshotError(f"{ALLOW_LIST} at {sha[:12]} names no entry; an empty list would open the whole commit")
    return entries


def _check_entries_exist(entries: Iterable[Entry], items: Sequence[TreeItem], sha: str) -> None:
    """Refuse an entry that is absent at *sha*, names the wrong kind, or opens no file."""
    files = {item.path: item for item in items}
    for entry in entries:
        where = f"{ALLOW_LIST}:{entry.line}: {entry.text!r}"
        if entry.is_folder:
            inside = [item for item in items if item.path.startswith(entry.text)]
            if not inside:
                if entry.text.rstrip("/") in files:
                    raise SnapshotError(f"{where} names a file at {sha[:12]}; a file entry has no trailing '/'")
                raise SnapshotError(f"{where} does not exist at {sha[:12]}")
            if not any(item.kind == "blob" for item in inside):
                raise SnapshotError(f"{where} is an empty folder at {sha[:12]}: it holds no file")
            continue
        item = files.get(entry.text)
        if item is None:
            if any(other.path.startswith(entry.text + "/") for other in items):
                raise SnapshotError(f"{where} names a folder at {sha[:12]}; a folder entry ends in '/'")
            raise SnapshotError(f"{where} does not exist at {sha[:12]}")
        if item.kind != "blob":
            raise SnapshotError(f"{where} is a submodule at {sha[:12]}, not a file")


def expansion_items(repo: Path, rev: str) -> tuple[str, list[Entry], list[TreeItem]]:
    """REV's full SHA, its validated allow-list, and the tree items the list opens."""
    sha = resolve_commit(repo, rev)
    entries = read_allow_list(repo, sha)
    everything = list_tree(repo, sha)
    _check_entries_exist(entries, everything, sha)
    opened = [item for item in everything if item.kind == "blob" and is_opened(item.path, entries)]
    return sha, entries, opened


# Public: other modules match paths by it.
def is_opened(path: str, entries: Iterable[Entry]) -> bool:
    return any(path.startswith(entry.text) if entry.is_folder else path == entry.text for entry in entries)


def expansion(repo: Path = REPO_ROOT, rev: str = "HEAD") -> tuple[str, ...]:
    """The POSIX paths of every file the allow-list opens at REV, sorted."""
    _, _, items = expansion_items(repo, rev)
    return tuple(sorted(item.path for item in items))


# ---------------------------------------------------------------------------
# assemble
# ---------------------------------------------------------------------------


def marker_item(sha: str, items: Iterable[TreeItem]) -> TreeItem:
    """The open-copy marker as the tree item a snapshot of *sha* commits.

    Raises:
        SnapshotError: the expansion *items* already holds a file of the marker's name.
    """
    if any(item.path == OPEN_COPY_MARKER for item in items):
        raise SnapshotError(
            f"{ALLOW_LIST} at {sha[:12]} opens {OPEN_COPY_MARKER}, the name of the marker assemble writes; "
            "rename the file or close it"
        )
    return TreeItem("100644", "blob", _blob_id(OPEN_COPY_MARKER_TEXT.encode("utf-8"), len(sha)), OPEN_COPY_MARKER)


def _prepare_out(out: Path) -> None:
    if out.exists():
        if not out.is_dir():
            raise SnapshotError(f"{out} exists and is not a folder")
        if any(out.iterdir()):
            raise SnapshotError(f"{out} is not empty; the snapshot is assembled into an absent or empty folder")
    out.mkdir(parents=True, exist_ok=True)


def _require_data_filter() -> None:
    """Refuse an interpreter whose ``tarfile`` has no ``data`` filter (before Python 3.11.4)."""
    if not hasattr(tarfile, "data_filter"):
        version = ".".join(str(part) for part in sys.version_info[:3])
        raise SnapshotError(
            f"assemble needs Python 3.11.4 or later for tarfile's 'data' extraction filter; this is {version}. "
            "Run it with the repository's .venv interpreter."
        )


def _extract_archive(repo: Path, sha: str, entries: Sequence[Entry], out: Path) -> None:
    """``git archive`` of the entries at *sha*, extracted into *out*."""
    pathspecs = [entry.text.rstrip("/") for entry in entries]
    if not pathspecs:
        raise SnapshotError(f"no entry to archive at {sha[:12]}; an archive without a pathspec is the whole commit")
    with tempfile.TemporaryDirectory() as scratch:
        archive = Path(scratch) / "snapshot.tar"
        git(repo, "archive", "--format=tar", f"--output={archive}", sha, "--", *pathspecs)
        try:
            with tarfile.open(archive, mode="r:") as tar:
                tar.extractall(out, filter="data")
        except (tarfile.TarError, OSError) as error:
            raise SnapshotError(f"cannot extract the archive of {sha[:12]} into {out}: {error}") from error


def assemble(out: Path, repo: Path = REPO_ROOT, rev: str = "HEAD") -> str:
    """Assemble REV's public part into *out* as a one-commit repository; returns REV's SHA."""
    _require_data_filter()
    sha, entries, items = expansion_items(repo, rev)
    marker_item(sha, items)
    _prepare_out(out)
    _extract_archive(repo, sha, entries, out)
    (out / OPEN_COPY_MARKER).write_bytes(OPEN_COPY_MARKER_TEXT.encode("utf-8"))
    date = commit_date(repo, sha)
    env = git_env()
    env.update(
        {
            "GIT_AUTHOR_NAME": SNAPSHOT_NAME,
            "GIT_AUTHOR_EMAIL": SNAPSHOT_EMAIL,
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_NAME": SNAPSHOT_NAME,
            "GIT_COMMITTER_EMAIL": SNAPSHOT_EMAIL,
            "GIT_COMMITTER_DATE": date,
        }
    )
    # The source's object format, so the committed blob ids are comparable with REV's.
    git(out, "init", "--quiet", "--initial-branch=main", f"--object-format={object_format(repo)}", env=env)
    # ``--force``: a tracked file the source's ``.gitignore`` also matches is still part of the commit.
    git(out, "add", "--all", "--force", env=env)
    # The executable bit from REV's tree: Git for Windows has ``core.filemode=false`` and records 100644.
    executables = [item.path for item in items if item.mode == "100755"]
    if executables:
        git(out, "update-index", "--chmod=+x", "--", *executables, env=env)
    message = f"{SNAPSHOT_SUBJECT} of {sha[:12]}\n\n{SOURCE_TRAILER}: {sha}\n"
    git(out, "commit", "--quiet", "--no-verify", "--cleanup=verbatim", "--file=-", env=env, stdin=message.encode())
    return sha


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def files_on_disk(root: Path) -> set[str]:
    """The POSIX paths of every file and symbolic link under *root*, ``.git`` aside."""
    found: set[str] = set()
    for current, folders, names in os.walk(root):
        here = Path(current)
        if here == root:
            folders[:] = [name for name in folders if name != ".git"]
        for name in folders:
            if (here / name).is_symlink():
                found.add((here / name).relative_to(root).as_posix())
        for name in names:
            found.add((here / name).relative_to(root).as_posix())
    return found


#: The prefix of a deny-list entry that is a regular expression rather than a literal word.
DENY_PATTERN_PREFIX = "re:"


def read_deny_list(path: Path) -> list[tuple[int, re.Pattern[str]]]:
    """The deny-list's entries as whole-word, case-insensitive patterns, each with its line.

    A literal entry is escaped and case-folded; a ``re:`` entry is compiled with ``re.IGNORECASE``
    between the same word bounds. A ``re:`` line holding ``#`` is refused, because the comment strip
    would silently cut the pattern; a pattern spells ``#`` as ``\\x23``.
    """
    if not path.is_file():
        raise SnapshotError(f"the deny-list {path} does not exist")
    patterns = []
    text = path.read_text(encoding="utf-8-sig")
    for number, raw in enumerate(text.splitlines(), start=1):
        if raw.strip().startswith(DENY_PATTERN_PREFIX) and "#" in raw:
            raise SnapshotError(
                f"the deny-list {path} line {number}: a pattern entry holds '#', which starts a comment; "
                "write it as \\x23 and put no comment on a pattern's line"
            )
        value = raw.split("#", 1)[0].strip()
        if not value:
            continue
        if value.startswith(DENY_PATTERN_PREFIX):
            body = value[len(DENY_PATTERN_PREFIX) :]
            try:
                pattern = re.compile(rf"(?<!{_WORD_CHAR})(?:{body})(?!{_WORD_CHAR})", re.IGNORECASE)
            except re.error as error:
                raise SnapshotError(
                    f"the deny-list {path} line {number}: the pattern does not compile ({error})"
                ) from error
        else:
            pattern = re.compile(rf"(?<!{_WORD_CHAR}){re.escape(value.casefold())}(?!{_WORD_CHAR})")
        patterns.append((number, pattern))
    return patterns


#: A line break with the comment or quotation prefix the next line opens with: ``#`` (``#:``
#: included), ``//``, ``*`` and ``>``, repeated or mixed (``> >``, ``# *``), and the blanks around.
_WRAP = re.compile(r"[ \t]*\n[ \t]*(?:(?:#+:?|//+|\*|>)[ \t]*)*")


def unwrapped(text: str) -> tuple[str, list[int]]:
    """*text* with every line break and the comment prefix after it read as one blank, and the line starts.

    A phrase wrapped over comment lines matches as on one line. Not reached: other prefixes (``--``,
    ``;``, ``REM``), a wrap inside a word, and a phrase split by markup or string concatenation.
    """
    starts: list[int] = []
    parts: list[str] = []
    position = 0
    length = 0
    for match in _WRAP.finditer(text):
        parts.append(text[position : match.start()] + " ")
        length += match.start() - position + 1
        starts.append(length)
        position = match.end()
    parts.append(text[position:])
    return "".join(parts), starts


def _line_of(starts: Sequence[int], offset: int) -> int:
    return bisect.bisect_right(starts, offset) + 1


def text_findings(text: str, patterns: Sequence[tuple[int, re.Pattern[str]]]) -> list[tuple[int, int]]:
    """Every deny-list match in *text*, as ``(deny-list line, text line)`` pairs, each pair once.

    *text* is read case-folded and :func:`unwrapped`; the matched text is never returned.
    """
    joined, starts = unwrapped(text.casefold())
    return [
        (number, line)
        for number, pattern in patterns
        for line in sorted({_line_of(starts, match.start()) for match in pattern.finditer(joined)})
    ]


def masked_path(path: str, patterns: Sequence[tuple[int, re.Pattern[str]]]) -> str:
    """*path* with every segment that matches a deny-list entry spelled ``<sha256:`` + 12 hex digits + ``>``.

    A match that spans segments (an entry holding ``/``) hides the whole path.
    """

    def matches(text: str) -> bool:
        folded = text.casefold()
        return any(pattern.search(folded) for _, pattern in patterns)

    def hidden(text: str) -> str:
        return f"<sha256:{hashlib.sha256(text.encode('utf-8', errors='surrogateescape')).hexdigest()[:12]}>"

    if not matches(path):
        return path
    spelled = "/".join(hidden(segment) if matches(segment) else segment for segment in path.split("/"))
    return hidden(path) if matches(spelled) else spelled


def deny_list_findings(root: Path, paths: Iterable[str], patterns: Sequence[tuple[int, re.Pattern[str]]]) -> list[str]:
    """Every deny-list match in a path name or a file's content, as report lines."""
    findings: list[str] = []
    if not patterns:
        return findings
    for path in sorted(paths):
        folded_path = path.casefold()
        shown = masked_path(path, patterns)
        for number, pattern in patterns:
            if pattern.search(folded_path):
                findings.append(f"{shown}: the path name matches deny-list line {number}")
        target = root / path
        if target.is_symlink():
            content = os.readlink(target)
        elif target.is_file():
            content = target.read_bytes().decode("utf-8", errors="surrogateescape")
        else:
            continue
        findings.extend(
            f"{shown}:{line}: matches deny-list line {number}" for number, line in text_findings(content, patterns)
        )
    return findings


#: The mode ``git ls-tree`` gives a symbolic link.
_SYMLINK_MODE = "120000"


def _disk_kind_matches(target: Path, mode: str) -> bool:
    """Whether *target* on disk is the kind *mode* records: a symbolic link, or a regular file."""
    if mode == _SYMLINK_MODE:
        return target.is_symlink()
    return not target.is_symlink() and target.is_file()


def _disk_blob_id(target: Path, oid_length: int) -> str:
    """The git blob id of *target*'s bytes on disk (a symbolic link: its target), in REV's hash."""
    data = os.fsencode(os.readlink(target)) if target.is_symlink() else target.read_bytes()
    return _blob_id(data, oid_length)


def _blob_id(data: bytes, oid_length: int) -> str:
    """The git blob id of *data* in the hash whose hex ids are *oid_length* long."""
    digest = hashlib.sha256() if oid_length == _SHA256_HEX_LENGTH else hashlib.sha1()
    digest.update(b"blob %d\0" % len(data))
    digest.update(data)
    return digest.hexdigest()


def check(directory: Path, repo: Path = REPO_ROOT, rev: str | None = None, deny_list: Path | None = None) -> list[str]:
    """Every way *directory* differs from the allow-list's expansion; empty when it does not."""
    if not (directory / ".git").exists():
        raise SnapshotError(f"{directory} is not an assembled snapshot (no .git)")
    if rev is None:
        rev = source_trailer(directory)
        if rev is None:
            raise SnapshotError(f"{directory}'s HEAD carries no {SOURCE_TRAILER} trailer; name the source with --rev")
    patterns = read_deny_list(deny_list) if deny_list is not None else []
    sha, _, items = expansion_items(repo, rev)
    expected = {item.path: item for item in [*items, marker_item(sha, items)]}
    on_disk = files_on_disk(directory)
    committed = {item.path: item for item in list_tree(directory, "HEAD")}
    commits = int(git(directory, "rev-list", "--count", "HEAD").decode("ascii").strip())

    def shown(path: str) -> str:
        return masked_path(path, patterns)

    findings = [] if commits == 1 else [f"history: HEAD has {commits} commits; a snapshot has exactly one"]
    theirs_format, mine_format = object_format(repo), object_format(directory)
    if mine_format != theirs_format:
        findings.append(
            f"object format: {mine_format}, the source's is {theirs_format}; the committed tree is not compared"
        )
    findings += [f"extra: {shown(path)}" for path in sorted(on_disk - expected.keys())]
    findings += [f"extra (committed): {shown(path)}" for path in sorted(committed.keys() - expected.keys())]
    findings += [f"missing: {shown(path)}" for path in sorted(expected.keys() - on_disk)]
    if mine_format == theirs_format:
        for path in sorted(expected.keys()):
            mine, theirs = committed.get(path), expected[path]
            if mine is None or (mine.mode, mine.oid) != (theirs.mode, theirs.oid):
                findings.append(f"differs from {sha[:12]}: {shown(path)}")
    for path in sorted(expected.keys() & on_disk):
        theirs, target = expected[path], directory / path
        if not _disk_kind_matches(target, theirs.mode) or _disk_blob_id(target, len(theirs.oid)) != theirs.oid:
            findings.append(f"differs on disk from {sha[:12]}: {shown(path)}")
    findings += deny_list_findings(directory, on_disk & expected.keys(), patterns)
    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Assemble or check the public snapshot named by public-paths.txt.")
    sub = parser.add_subparsers(dest="command", required=True)
    assemble_parser = sub.add_parser("assemble", help="assemble REV's public part into an absent or empty folder")
    assemble_parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="the source repository")
    assemble_parser.add_argument("--rev", default="HEAD", help="the source commit (default: HEAD)")
    assemble_parser.add_argument("--out", type=Path, required=True, help="the folder to assemble into")
    check_parser = sub.add_parser("check", help="check an assembled tree against the allow-list's expansion")
    check_parser.add_argument("directory", type=Path, help="the assembled tree")
    check_parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="the source repository")
    check_parser.add_argument("--rev", default=None, help=f"the source commit (default: the {SOURCE_TRAILER} trailer)")
    check_parser.add_argument("--deny-list", type=Path, default=None, help="words no open file may hold")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "assemble":
            sha = assemble(args.out.resolve(), repo=args.repo.resolve(), rev=args.rev)
            print(f"assembled {sha} into {args.out}")
            return 0
        findings = check(args.directory.resolve(), repo=args.repo.resolve(), rev=args.rev, deny_list=args.deny_list)
    except SnapshotError as error:
        print(f"public_snapshot: {error}", file=sys.stderr)
        return 1
    if findings:
        for finding in findings:
            print(finding)
        print(f"public_snapshot: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print(f"{args.directory} is exactly the allow-list's expansion")
    return 0


if __name__ == "__main__":
    sys.exit(main())
