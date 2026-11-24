"""Install the SWCraft workstation toolchain from an unpacked archive.

The archive's ``README.md``, beside this file, is the user's description: what the archive holds,
where the installation goes (``%LOCALAPPDATA%\\SWCraft`` or ``~/.swcraft``, with ``versions/`` and a
``current`` link), how ``PATH`` is reached, the steps, the options and ``install.log``.

**Stdlib only, parses on Python 3.7.** This file runs before any wheel is installed, so it
imports nothing the archive carries, and it parses on old interpreters (no ``match``,
``except*``, walrus or runtime ``X | Y``) so that :func:`python_refusal` can print the
"you need Python 3.11" sentence from whatever interpreter runs it.

**Structure.** :func:`plan_install` turns arguments, an environment, a target platform
(``"nt"`` or ``"posix"``) and a found interpreter into an ordered list of :class:`Step` s
and touches nothing; :func:`execute_plan` does all the touching through injected seams with
real defaults; :func:`main` measures what planning does not read, plans, executes, and
turns an :class:`InstallError` into one sentence on stderr and exit 1. Paths carry the
**target** platform's spelling (:class:`pathlib.PureWindowsPath` /
:class:`pathlib.PurePosixPath`, ``str()``). No step uses a shell: each carries an argv
list, and :func:`run_argv` refuses to hand ``cmd.exe`` an argument it would re-parse. The
log's redaction is partial (:func:`redacted`).

Not reached by a test on a development platform: the Windows calls (``winreg``,
``ctypes``, ``_winapi``) are witnessed as argument lists and call order, never their
effect. Nothing here validates a version string, reaches a network, or verifies that a
``--source`` directory's wheels are the archive's.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import functools
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tarfile
import time
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath, PureWindowsPath

#: The interpreter the installed toolchain runs on. Checked only where a caller chose the
#: interpreter (``--python``, or the running one for ``--source``/``--index-url``); the
#: Python an archive carries is 3.11 by construction.
REQUIRED_PYTHON = (3, 11)

#: The requirement installed by name; naming all three distributions would bypass the
#: declared dependency graph.
REQUIREMENT = "swcraft-agent"

#: The specimen generator, installed by name in the same ``pip`` call: no SWCraft distribution
#: depends on it (it is an independent oracle), so the dependency graph cannot bring it.
GENERATOR_REQUIREMENT = "swcraft-specimens"

REQUIREMENTS = (REQUIREMENT, GENERATOR_REQUIREMENT)

#: ``publisher.name`` of ``vscode_extension/package.json``.
EXTENSION_ID = "swcraft-local.swcraft-vscode"

#: The archive's own layout.
WHEELS_DIRNAME = "wheels"
PLATFORM_FILENAME = "PLATFORM.json"
SHA256SUMS_FILENAME = "SHA256SUMS"
VSIX_PREFIX = "swcraft-vscode-"
VSIX_SUFFIX = ".vsix"

#: The runtime payloads' member names and the directories they are unpacked into under
#: ``versions/<version>/``. Names carry no version; the Node.js member keeps nodejs.org's
#: extension for its platform (``.tar.xz`` Linux, ``.tar.gz`` macOS, ``.zip`` Windows).
RUNTIME_DIRNAME = "runtime"
NODE_DIRNAME = "node"
NODE_PAYLOAD_MEMBERS = ("runtime/node.tar.xz", "runtime/node.tar.gz", "runtime/node.zip")

#: The CPython payload: python-build-standalone's ``install_only`` tarball, whose one
#: leading directory is ``python/``.
PYTHON_PAYLOAD_MEMBER = "runtime/python.tar.gz"
PYTHON_DIRNAME = "python"

#: The extension pack's members, each as ``extensions/<publisher>.<name>.vsix`` with no
#: version or target platform in the name; the ids are read from the member names.
EXTENSIONS_DIRNAME = "extensions"
_EXTENSION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*$")

#: Where the wrapper unpacks its own copy of the Python payload; the plan's last step removes it.
BOOTSTRAP_DIRNAME = ".bootstrap"

#: A payload is unpacked here first and swapped into place once whole, so a failed
#: unpack leaves the previous tree, never half of one.
UNPACKING_SUFFIX = ".unpacking"

#: Where the previous tree is renamed while the new one takes its name. Renamed, not deleted:
#: a delete that stops at a file in use keeps what it removed; a rename is all or nothing.
SUPERSEDED_SUFFIX = ".previous"

#: Pauses, in seconds, between attempts of a swap rename refused with ``PermissionError``
#: (eight attempts, 7.85 s per rename; the rollback's rename is retried too). Only
#: ``PermissionError``: on Windows a sharing violation and an access denial both arrive as
#: one, and an on-access antivirus scanner holding files just written is transient; anything
#: else does not clear by waiting. On every platform so the one path runs on every runner.
#: Short on purpose: a tree held by a running SWCraft process reaches its advice quickly.
RENAME_RETRY_PAUSES: tuple[float, ...] = (0.1, 0.25, 0.5, 1.0, 2.0, 2.0, 2.0)

#: What a refusal of a retried rename appends to its sentence.
RENAME_RETRY_CLAUSE = f" after {len(RENAME_RETRY_PAUSES) + 1} attempts over {round(sum(RENAME_RETRY_PAUSES), 2):g} s"

#: Where ``vsce`` puts ``package.json`` inside the ``.vsix``: the only copy of the pack's
#: ``engines.vscode`` floor in the archive, read rather than carried as a literal that
#: would go stale when a pack member raises its floor.
VSIX_MANIFEST_MEMBER = "extension/package.json"

#: VS Code's own ``engines.vscode`` grammar (its ``parseVersion``/``normalizeVersion``/
#: ``isValidVersion``), as ``vscode_extension/src/healthCheck.ts`` spells it. A range's
#: ``(-...)`` tail is refused: it is a ``notBefore`` build date nothing here is given, so it
#: answers ``not-comparable``. A host version's tail (``1.134.0-insider``) is dropped, as
#: VS Code does.
_ENGINE_RANGE_PATTERN = re.compile(r"^(\^|>=)?((\d+)|x)\.((\d+)|x)\.((\d+)|x)$")
_HOST_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")

#: The installation's layout.
WINDOWS_HOME_DIRNAME = "SWCraft"
POSIX_HOME_DIRNAME = ".swcraft"
VERSIONS_DIRNAME = "versions"
CURRENT_LINK_NAME = "current"

#: The distribution names whose wheel filenames carry the archive's version.
_WHEEL_SUFFIX = ".whl"
_WHEEL_DISTRIBUTIONS = ("swcraft_tools", "swcraft_mcp", "swcraft_agent")


class InstallError(Exception):
    """A refusal, carrying one sentence the wrapper prints and nothing else."""


class ProgramWillNotStart(InstallError):
    """:func:`run_argv`'s refusal, with the ``OSError``'s own text kept as ``reason``.

    Two steps re-word it and need the reason without the sentence around it: a
    ``code`` that will not start must not report a failed uninstall, and an
    unstartable ``cmd.exe`` must not skip ``_create_windows_junction``'s third spelling.
    """

    def __init__(self, message, program, reason):
        super().__init__(message)
        self.program = program
        self.reason = reason


def _reason_of(error):
    """The operating system's half of *error*, or the whole sentence when there is no other."""
    return getattr(error, "reason", None) or str(error)


@dataclass(frozen=True)
class Step:
    """One planned action, and everything the executing layer needs to do it.

    ``kind`` is one of ``check-platform``, ``venv``, ``pip``, ``link``, ``path``,
    ``vsix``, ``remove``; ``path-remove`` and ``vsix-remove`` (``--uninstall`` only,
    separate kinds because their messages differ); ``runtime`` (``path`` the payload
    member, ``target`` the directory it is unpacked into); ``node`` (``path`` where
    ``node`` goes in the version's executable directory, ``target`` the relative link
    text on POSIX or the absolute ``node.exe`` copied on Windows); ``bootstrap-remove``
    (``path`` the wrapper's ``.bootstrap/``, never failing the run); and
    ``vsix-companion`` (``argv`` its ``--install-extension``, ``path`` the ``.vsix``,
    ``target`` the extension's ``publisher.name`` id).

    ``kind`` says which fields a step fills; an unfilled field is ``""``, which is why
    :func:`execute_plan` dispatches on the kind. ``probe`` is ``code --version``
    (``vsix`` only), built by :func:`code_argv` and asked before
    ``--install-extension`` so the compatibility floor is compared here rather than in
    VS Code's dialog.
    """

    kind: str
    argv: tuple[str, ...] = ()
    path: str = ""
    target: str = ""
    entry: str = ""
    probe: tuple[str, ...] = ()


def python_refusal(version_info: Sequence[int], named: str | None = None) -> str | None:
    """The sentence to print when *version_info* cannot build the installation.

    ``None`` when the interpreter is new enough. *named* is the path ``--python`` gave,
    when the version is that interpreter's; the remedy then is to leave the flag out.
    """
    if tuple(version_info[:2]) >= REQUIRED_PYTHON:
        return None
    found = ".".join(str(part) for part in version_info[:2])
    required = ".".join(str(part) for part in REQUIRED_PYTHON)
    if named is not None:
        return (
            f"SWCraft needs Python {required} and {named} is Python {found}: leave --python out to build "
            f"on the Python this archive carries, or name a Python {required} with --python PATH"
        )
    return (
        f"SWCraft needs Python {required} and this is Python {found}: install Python {required} and run "
        f"the installer again, or name one with --python PATH"
    )


def _version_parts(text) -> tuple[int, ...] | None:
    """``"3.10"`` as ``(3, 10)``; ``None`` for anything that is not two numbers."""
    parts = str(text).split(".")[:2]
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    return (int(parts[0]), int(parts[1]))


def _names_python(tokens) -> bool:
    """Whether ``--python`` is among *tokens*, read without parsing them.

    Asked before argv is parsed: a caller who already named an interpreter must not be
    told to name one. A token scan: ``--python`` as another option's value reads as the
    flag, and an abbreviation (``--pyt``) is not recognised.
    """
    return any(token == "--python" or token.startswith("--python=") for token in tokens)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def pure_path(platform: str) -> type[PurePath]:
    """The path class that spells paths the way *platform* spells them."""
    return PureWindowsPath if platform == "nt" else PurePosixPath


def home_is_absolute(home: str, platform: str) -> bool:
    """Whether ``--home`` resolves the same from anywhere *on the target platform*.

    Read under the flavour :func:`home_directory` spells it with, else ``C:\\SWCraft``
    on a POSIX target would dangle the ``current`` link. Not reached: whether the
    directory exists or is writable, or whether a Windows drive letter exists.
    """
    return pure_path(platform)(home).is_absolute()


def bin_dirname(platform: str) -> str:
    """``Scripts`` on Windows, ``bin`` elsewhere -- a venv's executable directory."""
    return "Scripts" if platform == "nt" else "bin"


def venv_python(version_dir: PurePath, platform: str) -> PurePath:
    """The interpreter inside an installed version's venv."""
    if platform == "nt":
        return version_dir / "Scripts" / "python.exe"
    return version_dir / "bin" / "python"


def default_home(env, platform: str) -> PurePath:
    """The installation home when ``--home`` is absent, read from *env*, not ``os.environ``."""
    pure = pure_path(platform)
    if platform == "nt":
        local = env.get("LOCALAPPDATA")
        if local:
            return pure(local) / WINDOWS_HOME_DIRNAME
        profile = env.get("USERPROFILE")
        if profile:
            return pure(profile) / "AppData" / "Local" / WINDOWS_HOME_DIRNAME
        raise InstallError(
            "neither LOCALAPPDATA nor USERPROFILE is set, so there is no user profile to install into: pass --home DIR"
        )
    home = env.get("HOME")
    if not home:
        raise InstallError("HOME is not set, so there is no user profile to install into: pass --home DIR")
    return pure(home) / POSIX_HOME_DIRNAME


def home_directory(args, env, platform: str) -> PurePath:
    """``--home`` verbatim when it was given (:func:`main` refuses a relative one), else :func:`default_home`."""
    if args.home:
        return pure_path(platform)(args.home)
    return default_home(env, platform)


def path_entry(home: PurePath, platform: str) -> str:
    """``<home>/current/<bin>``: appended to the user ``PATH`` on Windows, printed elsewhere."""
    return str(home / CURRENT_LINK_NAME / bin_dirname(platform))


def path_entry_present(entry: str, env, platform: str) -> bool:
    """Whether the process ``PATH`` already lists *entry*.

    Consulted on POSIX alone. On Windows the process value and the durable one in
    ``HKCU\\Environment`` routinely disagree, so the step is always planned and
    ``_add_path_entry`` compares against the registry.
    """
    separator = ";" if platform == "nt" else ":"
    listed = [part for part in env.get("PATH", "").split(separator) if part]
    return _normalized(entry, platform) in [_normalized(part, platform) for part in listed]


def _normalized(entry: str, platform: str) -> str:
    """One ``PATH`` entry as its platform compares it; a comparison form, never written.

    Windows compares case-insensitively, treats ``/`` and ``\\`` alike, and its durable
    ``PATH`` routinely carries quoted or padded entries, so padding and quotes are
    stripped (otherwise a quoted copy is appended a second time). POSIX trims only a
    trailing ``/``: a space or a quote is part of a directory name there.
    """
    if platform == "nt":
        return entry.strip().strip('"').strip().rstrip("/\\").replace("/", "\\").lower()
    return entry.rstrip("/")


def vsix_path(archive_root: PurePath, version: str) -> str:
    """The extension package the archive carries for *version*."""
    return str(archive_root / (VSIX_PREFIX + version + VSIX_SUFFIX))


def runtime_payloads(names: Iterable[str]) -> tuple[tuple[str, str], ...]:
    """The ``(member, directory)`` pairs of the runtime payloads among *names*.

    *names* is :func:`verify_archive`'s member list, so only hashed members are
    planned. The CPython payload is not one of these. More than one Node.js member is
    refused: the builder names exactly one, so a second means a hand-assembled archive.
    """
    listed = set(names)
    node = [member for member in NODE_PAYLOAD_MEMBERS if member in listed]
    if len(node) > 1:
        raise InstallError(
            f"this archive carries more than one Node.js payload ({', '.join(node)}), so it is not an "
            f"archive this installer can read: unpack a fresh one"
        )
    return tuple((member, NODE_DIRNAME) for member in node)


def companion_packages(names: Iterable[str]) -> tuple[tuple[str, str], ...]:
    """``(member, extension id)`` of every companion ``.vsix`` among *names*, sorted by member.

    *names* is :func:`verify_archive`'s member list, so only hashed members count. A
    companion is exactly ``extensions/<publisher>.<name>.vsix``; anything else under
    ``extensions/`` is not installed. No order among companions matters.
    """
    found = []
    for name in names:
        directory, _, filename = name.partition("/")
        if directory != EXTENSIONS_DIRNAME or "/" in filename or not filename.endswith(VSIX_SUFFIX):
            continue
        ident = filename[: -len(VSIX_SUFFIX)]
        if _EXTENSION_ID_PATTERN.match(ident):
            found.append((name, ident))
    return tuple(sorted(found))


def installed_python(version_dir: PurePath, platform: str) -> PurePath:
    """The unpacked CPython payload's interpreter: ``runtime/python/bin/python3``, or ``python.exe`` at the root."""
    if platform == "nt":
        return version_dir / RUNTIME_DIRNAME / PYTHON_DIRNAME / "python.exe"
    return version_dir / RUNTIME_DIRNAME / PYTHON_DIRNAME / "bin" / "python3"


def _node_filename(platform: str) -> str:
    """The Node.js executable's file name on *platform*, in the runtime tree and on ``PATH`` alike."""
    return "node.exe" if platform == "nt" else "node"


def installed_node(version_dir: PurePath, platform: str) -> PurePath:
    """The unpacked runtime's own ``node``: ``runtime/node/bin/node``, or ``node.exe`` at the tree root."""
    if platform == "nt":
        return version_dir / RUNTIME_DIRNAME / NODE_DIRNAME / _node_filename(platform)
    return version_dir / RUNTIME_DIRNAME / NODE_DIRNAME / "bin" / _node_filename(platform)


def node_on_path(version_dir: PurePath, platform: str) -> PurePath:
    """Where ``node`` is placed: the venv's executable directory, beside ``swcraft``."""
    return version_dir / bin_dirname(platform) / _node_filename(platform)


def node_placement_target(version_dir: PurePath, platform: str) -> str:
    """What the ``node`` step writes at :func:`node_on_path` -- a relative link, or the file to copy.

    POSIX: the relative link text ``../runtime/node/bin/node``, which resolves through
    ``current/bin/`` too. Windows: the absolute ``runtime\\node\\node.exe``, which the
    step copies (no hard link, junction or ``.cmd`` shim).
    """
    if platform == "nt":
        return str(installed_node(version_dir, platform))
    relative = installed_node(PurePosixPath(".."), platform)
    return str(relative)


# ---------------------------------------------------------------------------
# What the archive says about itself
# ---------------------------------------------------------------------------


def platform_compatibility(tag) -> tuple[str, ...]:
    """The part of a ``sysconfig`` platform tag two interpreters have to agree on.

    A macOS tag's middle field (``macosx-11.0-arm64``) is the interpreter's deployment
    target, not the host's, and interpreters differing there share wheels; so a macOS
    tag compares as ``(os family, architecture)`` and every other tag whole, since
    guessing at its structure would trade a false refusal for a false acceptance.

    What it does **not** reach: whether the two interpreters really can share a wheel
    (that is pip's and the tags'); an architecture spelled two ways (``universal2``
    against ``arm64``) is refused although pip would accept it.
    """
    if not isinstance(tag, str):
        return (repr(tag),)
    if tag.startswith("macosx-"):
        fields = tag.split("-")
        if len(fields) >= 3:
            return (fields[0], fields[-1])
    return (tag,)


def check_platform_stamp(recorded, current) -> None:
    """Refuse a wheelhouse built for another platform or another Python.

    *recorded* is the archive's ``wheels/PLATFORM.json``; *current* the same two keys
    from the interpreter the venv will be built with (:func:`interpreter_stamp`).
    Without this, pip reports it cannot find a distribution it can plainly see.
    ``platform_tag`` is compared through :func:`platform_compatibility`,
    ``python_version`` exactly.
    """
    comparable = {
        "platform_tag": platform_compatibility,
        "python_version": lambda value: value,
    }
    mismatches = [
        f"{key} is {recorded.get(key)!r} in the archive and {current.get(key)!r} here"
        for key in ("platform_tag", "python_version")
        if comparable[key](recorded.get(key)) != comparable[key](current.get(key))
    ]
    if mismatches:
        raise InstallError(
            f"this archive was built for another environment ({'; '.join(mismatches)}), so its wheels "
            f"cannot be installed here: use the archive for this platform"
        )
    return None


def parse_sha256sums(text: str) -> tuple[tuple[str, str], ...]:
    """The ``(digest, member)`` pairs of a coreutils-format ``SHA256SUMS``.

    A hex digest, two spaces, a POSIX-spelled path, one per line; a binary-mode line
    (``digest *name``, as ``sha256sum -b`` writes it) is accepted too.
    """
    entries = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        digest, separator, name = line.partition("  ")
        if not separator:
            digest, separator, name = line.partition(" *")
        name = name.strip()
        if not separator or not name or len(digest) != 64:
            raise InstallError(
                f"{SHA256SUMS_FILENAME} line {number} is not a checksum line, so this archive cannot be verified"
            )
        parts = name.replace("\\", "/").split("/")
        if name.startswith("/") or ":" in name or ".." in parts:
            # Refused although the only consumer reads: "it only reads" is a property of
            # the caller, not of the format.
            raise InstallError(
                f"{SHA256SUMS_FILENAME} line {number} names {name}, which is outside this archive, so the "
                f"archive cannot be verified: download it again"
            )
        entries.append((digest, name))
    return tuple(entries)


def verify_members(entries: Iterable[tuple[str, str]], digest_of: Callable[[str], str | None]) -> None:
    """Refuse an archive that is missing a member or carries a changed one.

    *digest_of* answers the hex digest of one member, or ``None`` when it is absent.
    """
    for digest, name in entries:
        actual = digest_of(name)
        if actual is None:
            raise InstallError(
                f"{name} is listed in {SHA256SUMS_FILENAME} and is missing from this archive, so the "
                f"archive is incomplete: unpack it again"
            )
        if actual != digest:
            raise InstallError(
                f"{name} does not match its {SHA256SUMS_FILENAME} entry, so the archive is damaged or "
                f"altered: download it again"
            )
    return None


def archive_version(names: Iterable[str]) -> str:
    """The version this archive holds, read out of its own member names.

    The archive carries no version file, so the version is read from
    ``wheels/swcraft_<project>-<version>-*.whl`` and ``swcraft-vscode-<version>.vsix``.
    Two answers is a refusal, not a choice.
    """
    found = set()
    for name in names:
        base = name.rsplit("/", 1)[-1]
        if base.startswith(VSIX_PREFIX) and base.endswith(VSIX_SUFFIX):
            found.add(base[len(VSIX_PREFIX) : -len(VSIX_SUFFIX)])
            continue
        if not base.endswith(_WHEEL_SUFFIX):
            continue
        for distribution in _WHEEL_DISTRIBUTIONS:
            prefix = distribution + "-"
            if base.startswith(prefix):
                found.add(base[len(prefix) :].split("-", 1)[0])
    if not found:
        raise InstallError(
            "no wheel or .vsix in this archive names a version, so there is nothing to install: pass --version VERSION"
        )
    if len(found) > 1:
        raise InstallError(
            f"this archive holds more than one version ({', '.join(sorted(found))}), so it is not an "
            f"archive this installer can read: unpack a fresh one"
        )
    return found.pop()


def carried_python_refusal(version_info: Sequence[int]) -> str | None:
    """The sentence for a run of this file, **with no** ``--python``, on an interpreter the archive did not carry.

    ``None`` on a Python :data:`REQUIRED_PYTHON`. The ``check-platform`` step asks the
    running interpreter, so without this ``python3.13 install.py`` would be refused as
    "built for another environment" when the archive is right and the entry point is
    not; the remedy named is the wrapper, or ``--python``.
    """
    if tuple(version_info[:2]) == tuple(REQUIRED_PYTHON):
        return None
    found = ".".join(str(part) for part in version_info[:2])
    required = ".".join(str(part) for part in REQUIRED_PYTHON)
    return (
        f"this archive carries its own Python {required} and this file was started on Python {found}, so "
        f"nothing was installed: run swcraft_install.sh (swcraft_install.cmd on Windows) beside it, which "
        f"starts it on the carried Python, or name a Python {required} with --python PATH"
    )


def code_argv(found_code, arguments: Sequence[str]) -> tuple[str, ...]:
    """How to invoke a resolved ``code``, with no shell anywhere.

    On Windows ``code`` resolves to ``code.cmd``, which ``CreateProcess`` cannot
    execute, so it runs under ``cmd.exe /c``. An unresolved ``code`` yields an empty
    argv; the step still exists so the executing layer can report it.

    ``call`` is load-bearing. ``cmd /?``'s rule 2: when the line after ``/c`` does not
    meet rule 1 (exactly two quotes, none of ``&<>()@^|`` between them) and begins
    with a quote, ``cmd`` strips the first and last quote. ``list2cmdline`` quotes any
    argument with a space, so a ``.vsix`` under ``C:\\Users\\John Smith`` or a
    ``code.cmd`` under ``Program Files (x86)`` would leave ``cmd`` running
    ``C:\\Program``. ``call`` makes the first character a letter; it also propagates
    the batch file's exit code.

    What ``cmd.exe`` would re-read in the argv is checked by :func:`run_argv`, not here.
    """
    if not found_code:
        return ()
    lowered = found_code.lower()
    if lowered.endswith(".cmd") or lowered.endswith(".bat"):
        return ("cmd.exe", "/c", "call", found_code) + tuple(arguments)
    return (found_code,) + tuple(arguments)


# ---------------------------------------------------------------------------
# The compatibility floor (the pack's `engines.vscode`, read from the .vsix)
# ---------------------------------------------------------------------------


def engine_floor(package: str) -> str | None:
    """The pack's declared ``engines.vscode``, read from :data:`VSIX_MANIFEST_MEMBER` in *package*.

    ``None`` for every way the reading can fail (unreadable file, missing member, bad
    JSON, no ``engines.vscode``): a refusal built on a string this could not read is
    worse than not checking.
    """
    try:
        with zipfile.ZipFile(package) as archive:
            manifest = json.loads(archive.read(VSIX_MANIFEST_MEMBER).decode("utf-8"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, UnicodeDecodeError):
        return None
    engines = manifest.get("engines") if isinstance(manifest, dict) else None
    declared = engines.get("vscode") if isinstance(engines, dict) else None
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    return None


def host_version(output) -> str | None:
    """``code --version``'s first line, when it is a version this can compare.

    ``None`` (empty answer, a first line not ``major.minor.patch``) means "not
    comparable", never "too old".
    """
    if not output:
        return None
    lines = output.splitlines()
    first = lines[0].strip() if lines else ""
    return first if _HOST_VERSION_PATTERN.match(first) else None


def _parsed_range(floor: str):
    """VS Code's ``normalizeVersion`` over a declared range, or ``None``.

    Answers ``(major, major_must_equal, minor, minor_must_equal, patch,
    patch_must_equal, is_minimum)``. A caret on a non-zero major clears both lower
    ``must_equal`` flags (``^1.110.0`` is "1.110.0 or later, major 1"); a bare
    ``1.63.0`` is an exact requirement, not a floor.
    """
    trimmed = floor.strip()
    if trimmed == "*":
        return (0, False, 0, False, 0, False, False)
    match = _ENGINE_RANGE_PATTERN.match(trimmed)
    if match is None:
        return None
    tokens = (match.group(2), match.group(4), match.group(6))
    numbers = tuple(0 if token == "x" else int(token) for token in tokens)
    must_equal = [token != "x" for token in tokens]
    if match.group(1) == "^":
        if numbers[0] != 0:
            must_equal[1] = False
        must_equal[2] = False
    return (
        numbers[0],
        must_equal[0],
        numbers[1],
        must_equal[1],
        numbers[2],
        must_equal[2],
        match.group(1) == ">=",
    )


def engine_floor_verdict(version: str, floor: str) -> str:
    """``"satisfied"``, ``"below-floor"`` or ``"not-comparable"`` -- VS Code's own comparison.

    An unparseable host version or a range outside the grammar (a compound range, a
    build-date tail) is ``not-comparable`` and the caller carries on.

    What it does not reach: VS Code also consults the host build date and a ``0.x``
    shim for pre-1.0 versions; neither is modelled, both answer ``not-comparable``.
    """
    parsed_host = _HOST_VERSION_PATTERN.match(version.strip()) if version else None
    parsed_floor = _parsed_range(floor) if floor else None
    if parsed_host is None or parsed_floor is None:
        return "not-comparable"
    major, minor, patch = (int(parsed_host.group(index)) for index in (1, 2, 3))
    (
        wanted_major,
        major_must_equal,
        wanted_minor,
        minor_must_equal,
        wanted_patch,
        patch_must_equal,
        is_minimum,
    ) = parsed_floor
    if is_minimum:
        satisfied = (major, minor, patch) >= (wanted_major, wanted_minor, wanted_patch)
    elif major != wanted_major:
        satisfied = major > wanted_major and not major_must_equal
    elif minor != wanted_minor:
        satisfied = minor > wanted_minor and not minor_must_equal
    elif patch != wanted_patch:
        satisfied = patch > wanted_patch and not patch_must_equal
    else:
        satisfied = True
    return "satisfied" if satisfied else "below-floor"


def code_version_output(argv: Sequence[str]) -> str | None:
    """Run ``code --version`` and answer what it printed, or ``None``.

    Its ``argv[0]`` can be ``cmd.exe``, so it asks :func:`cmd_reparsed_part` what
    :func:`run_argv` asks. Every failure is ``None``: this is a question before the
    run, and the real ``--install-extension`` step reports a broken ``code`` (one that
    dies on start, as an ICU error does) in its own words. Bounded and with no console
    input, so a prompting ``code`` shim cannot hang the install.
    """
    if not argv or cmd_reparsed_part(argv) is not None:
        return None
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", "replace")


def engine_floor_refusal(step: Step, ask: Callable[[Sequence[str]], str | None] | None = None) -> str | None:
    """The sentence a host below the pack's floor gets, or ``None`` to carry on.

    The floor is read from the ``.vsix`` first, with no subprocess; only a real floor
    buys a ``code --version``. Either reading answering "cannot tell" carries on.

    What this does **not** reach: any other reason an editor refuses the file (a
    platform-specific build, a policy, a corrupted download), or an editor new enough
    and broken anyway.
    """
    if not step.probe or not step.path:
        return None
    floor = engine_floor(step.path)
    if floor is None:
        return None
    ask = code_version_output if ask is None else ask
    version = host_version(ask(step.probe))
    if version is None or engine_floor_verdict(version, floor) != "below-floor":
        return None
    return (
        f"VS Code {version} is older than {floor}, the version this extension pack needs, so the "
        f"toolchain is installed and the extension is not: update VS Code and install {step.path} "
        f"from the Extensions view yourself"
    )


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The installer's arguments, all optional: the archive answers the rest."""
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Install the SWCraft toolchain from this unpacked archive.",
    )
    parser.add_argument("--home", help="install into this absolute directory instead of the user-profile default")
    parser.add_argument("--source", help="directory of wheels to install from (default: the archive's wheels/)")
    parser.add_argument("--index-url", dest="index_url", help="install from this package index instead of wheels/")
    parser.add_argument(
        "--python",
        help=(
            "build the installation on this interpreter instead of the Python this archive carries, "
            "and unpack no Python payload"
        ),
    )
    parser.add_argument("--version", help="the version to install (default: the one this archive carries)")
    parser.add_argument("--no-path", dest="no_path", action="store_true", help="do not touch PATH")
    parser.add_argument("--no-vsix", dest="no_vsix", action="store_true", help="do not touch the VS Code extension")
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help=(
            "remove the installation and the extension; on Windows also the PATH entry, and "
            "elsewhere print the line to remove from a shell profile"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show every program's own output on the console instead of only in install.log",
    )
    return parser


def parse_args(argv: Sequence[str], archive_root: str | None = None) -> argparse.Namespace:
    """Parse *argv*, refusing ``--source`` with ``--index-url`` (two answers to one question).

    ``--uninstall`` ignores ``--source``, ``--index-url``, ``--version`` and
    ``--python`` rather than refusing them, so a wrapper can append it to a fixed
    argument list. ``--python`` with ``--index-url`` is left to pip.

    *archive_root* is the directory holding ``wheels/``, the ``.vsix`` and
    ``SHA256SUMS``; a parameter so a test can plan for the other platform.
    """
    args = build_parser().parse_args(list(argv))
    if args.source and args.index_url:
        raise InstallError(
            "--source and --index-url both name where the wheels come from, so only one of them can be given"
        )
    args.archive_root = archive_root if archive_root is not None else os.path.dirname(os.path.abspath(__file__))
    return args


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def plan_install(
    args: argparse.Namespace,
    env: Mapping[str, str],
    platform: str,
    found_python: Sequence[str],
    installed: Iterable[str] = (),
    found_code: str | None = None,
    venv_usable: Callable[[str], bool] | None = None,
    members: Iterable[str] = (),
) -> list[Step]:
    """The ordered steps that install, update, repair or remove the toolchain.

    Pure: besides *env* and *args* it reads facts measured by the caller --
    *installed* (version directory names under ``versions/``), *found_code* (where
    ``code`` resolved, ``None`` when it did not), *venv_usable* (whether a present
    version's interpreter is there **and starts**; absent means "no", so the ``venv``
    step is planned) and *members* (the archive's member names, for the payloads).

    The order survives an interruption: the new version is installed beside the old,
    ``current`` is switched, the extension installed, and only then is every other
    version removed. Re-installing the present version is a *repair*
    (``--force-reinstall``, payloads unpacked again) and removes nothing, since that
    directory may be ``current`` and in use. A version directory is not an
    installation: ``venv`` can fail after creating it, so the ``venv`` step is planned
    unless *venv_usable* says yes.

    Payloads go after ``pip`` and before ``link``, except the Python payload: when the
    archive carries one and ``--python`` is absent, its ``runtime`` step comes before
    ``venv``, the venv is built on :func:`installed_python`, and ``bootstrap-remove``
    is last. The ``pip`` step installs :data:`REQUIREMENTS` at *version* in one call.
    """
    pure = pure_path(platform)
    home = home_directory(args, env, platform)
    archive_root = pure(args.archive_root)
    members = tuple(members)
    steps: list[Step] = []

    if args.uninstall:
        steps.append(Step("remove", path=str(home)))
        if not args.no_path:
            steps.append(Step("path-remove", entry=path_entry(home, platform)))
        if not args.no_vsix:
            steps.append(Step("vsix-remove", argv=code_argv(found_code, ("--uninstall-extension", EXTENSION_ID))))
        # Both wrappers unpack the carried Python into `.bootstrap` whatever the
        # arguments, so an uninstall would otherwise leave about 80 MB beside this file.
        steps.append(Step("bootstrap-remove", path=str(archive_root / BOOTSTRAP_DIRNAME)))
        return steps

    version = args.version
    if not version:
        raise InstallError(
            "no version was named and none could be read from this archive, so there is nothing to "
            "install: pass --version VERSION"
        )
    source = pure(args.source) if args.source else archive_root / WHEELS_DIRNAME
    target = home / VERSIONS_DIRNAME / version
    present = tuple(installed)
    repair = version in present
    ready = repair and venv_usable is not None and venv_usable(str(venv_python(target, platform)))

    # The Python the archive carries builds the venv, unless `--python` names another.
    carried = not args.python and PYTHON_PAYLOAD_MEMBER in members
    if not args.index_url:
        # An index resolves for pip's own interpreter, so the stamp matters only for a
        # wheelhouse. With a carried Python, *found_python* is the bootstrap copy of the
        # same build the venv uses, so this refuses before 48 MB are unpacked.
        steps.append(Step("check-platform", path=str(source / PLATFORM_FILENAME), argv=tuple(found_python)))
    builder = tuple(found_python)
    if carried:
        steps.append(
            Step(
                "runtime",
                path=str(archive_root.joinpath(*PYTHON_PAYLOAD_MEMBER.split("/"))),
                target=str(target / RUNTIME_DIRNAME / PYTHON_DIRNAME),
            )
        )
        builder = (str(installed_python(target, platform)),)
    if not ready:
        steps.append(Step("venv", argv=builder + ("-m", "venv", str(target))))
    steps.append(
        Step(
            "pip",
            argv=_pip_argv(
                interpreter=str(venv_python(target, platform)),
                version=version,
                source=str(source),
                index_url=args.index_url,
                repair=repair,
            ),
        )
    )
    # Before `current` moves, so a failed unpack leaves the previous version current.
    payloads = runtime_payloads(members)
    for member, dirname in payloads:
        steps.append(
            Step(
                "runtime",
                path=str(archive_root.joinpath(*member.split("/"))),
                target=str(target / RUNTIME_DIRNAME / dirname),
            )
        )
    if any(dirname == NODE_DIRNAME for _, dirname in payloads):
        steps.append(
            Step(
                "node",
                path=str(node_on_path(target, platform)),
                target=node_placement_target(target, platform),
            )
        )
    steps.append(Step("link", path=str(home / CURRENT_LINK_NAME), target=str(target)))
    if not args.no_path:
        entry = path_entry(home, platform)
        # On Windows `_add_path_entry` decides against `HKCU\Environment`, which the
        # process `PATH` does not reflect; on POSIX the step is advice, so the process
        # `PATH` is the right reason to skip it.
        if platform == "nt" or not path_entry_present(entry, env, platform):
            steps.append(Step("path", entry=entry))
    if not args.no_vsix:
        # The pack's members first, so the extension that declares the pack finds them;
        # `--force`d like SWCraft's own, since an older archive rolls a companion back too.
        for member, ident in companion_packages(members):
            companion = str(archive_root.joinpath(*member.split("/")))
            steps.append(
                Step(
                    "vsix-companion",
                    argv=code_argv(found_code, ("--install-extension", companion, "--force")),
                    path=companion,
                    target=ident,
                )
            )
        package = vsix_path(archive_root, version)
        steps.append(
            Step(
                "vsix",
                # `--force` because a rollback must downgrade the extension too: without
                # it `code` refuses a downgrade and still exits zero. The compatibility
                # floor is checked here by `probe`, not by VS Code.
                argv=code_argv(found_code, ("--install-extension", package, "--force")),
                path=package,
                probe=code_argv(found_code, ("--version",)),
            )
        )
    # Sorted over version names for a deterministic plan; ordering versions is the extension's.
    for other in sorted(name for name in present if name != version):
        steps.append(Step("remove", path=str(home / VERSIONS_DIRNAME / other)))
    if carried:
        # Last: by now the installation holds its own copy of the interpreter.
        steps.append(Step("bootstrap-remove", path=str(archive_root / BOOTSTRAP_DIRNAME)))
    return steps


def _pip_argv(interpreter: str, version: str, source: str, index_url, repair: bool) -> tuple[str, ...]:
    """The ``pip install`` argv: offline from *source* or from *index_url*, ``--force-reinstall`` on a repair."""
    argv = [interpreter, "-m", "pip", "install"]
    if repair:
        argv.append("--force-reinstall")
    if index_url:
        argv.extend(["--index-url", index_url])
    else:
        argv.extend(["--no-index", "--find-links", source])
    argv.extend(requirement + "==" + version for requirement in REQUIREMENTS)
    return tuple(argv)


# ---------------------------------------------------------------------------
# The seams: everything below this line touches the machine
# ---------------------------------------------------------------------------

#: ``WM_SETTINGCHANGE``, broadcast so a newly started process picks the ``PATH`` up
#: without a sign-out. The Windows SDK's values, written here so a test can read them.
HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002

#: Per-window timeout: a plain ``SendMessage`` broadcast blocks forever on any hung window.
BROADCAST_TIMEOUT_MS = 5000

#: ``HKEY_CURRENT_USER\Environment``'s subkey and value; the machine ``PATH`` needs
#: elevation and is never touched.
ENVIRONMENT_SUBKEY = "Environment"
PATH_VALUE_NAME = "Path"

#: The block the archive's members are hashed in.
_DIGEST_BLOCK = 1024 * 1024


def path_separator(platform: str) -> str:
    """``;`` on Windows, ``:`` elsewhere."""
    return ";" if platform == "nt" else ":"


def appended_path(value: str, entry: str, platform: str) -> str | None:
    """*value* with *entry* appended, or ``None`` when it is already there.

    Appended, never rebuilt: the user's entries, unexpanded ``%VAR%`` references
    included, come back unchanged and in order.
    """
    if path_entry_present(entry, {"PATH": value}, platform):
        return None
    if not value:
        return entry
    separator = path_separator(platform)
    return value.rstrip(separator) + separator + entry


def removed_path(value: str, entry: str, platform: str) -> str | None:
    """*value* without *entry*, or ``None`` when it was not there.

    Every part it does not drop is kept verbatim; only normalized forms are compared.
    """
    separator = path_separator(platform)
    wanted = _normalized(entry, platform)
    parts = [part for part in value.split(separator) if part]
    kept = [part for part in parts if _normalized(part, platform) != wanted]
    if len(kept) == len(parts):
        return None
    return separator.join(kept)


def broadcast_environment_change(send_message: Callable[..., object]) -> None:
    """Tell the desktop the environment changed, with a timeout and the abort flag.

    *send_message* is a seam so the argument list is assertable where ``user32`` does
    not exist. Not reached: whether the message is delivered or Explorer answers; an
    already open shell does not re-read the environment, hence the advice to open a
    new terminal.
    """
    send_message(
        HWND_BROADCAST,
        WM_SETTINGCHANGE,
        0,
        ENVIRONMENT_SUBKEY,
        SMTO_ABORTIFHUNG,
        BROADCAST_TIMEOUT_MS,
    )


def send_message_timeout(hwnd, message, wparam, lparam, flags, timeout_ms) -> None:
    """``user32.SendMessageTimeoutW``, the real broadcast; elsewhere a refusal sentence."""
    if sys.platform != "win32":
        raise InstallError("the environment broadcast is a Windows call and this is not Windows, so it was not sent")
    import ctypes

    answer = ctypes.c_size_t()
    ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
        hwnd,
        message,
        wparam,
        ctypes.c_wchar_p(lparam),
        flags,
        timeout_ms,
        ctypes.byref(answer),
    )


class WindowsUserPath:
    """``HKCU\\Environment``'s ``Path``: read it, write it back, broadcast.

    The type read is the type written: a ``REG_EXPAND_SZ`` written back as ``REG_SZ``
    freezes every ``%VAR%`` in the user's ``PATH``. ``winreg`` is imported only on
    ``win32``, so the module still loads elsewhere. No test constructs this with a
    real ``winreg``.
    """

    def __init__(self, winreg_module=None) -> None:
        self._winreg = winreg_module

    def _module(self):
        if self._winreg is None:
            if sys.platform != "win32":
                raise InstallError(
                    "the user PATH lives in the Windows registry and this is not Windows, so it was not "
                    "edited: pass --no-path and add the directory to your shell profile yourself"
                )
            import winreg

            self._winreg = winreg
        return self._winreg

    def read(self) -> tuple[str, int]:
        """The current value and its registry type; ``("", REG_EXPAND_SZ)`` when unset."""
        winreg = self._module()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENVIRONMENT_SUBKEY) as key:
            try:
                value, value_type = winreg.QueryValueEx(key, PATH_VALUE_NAME)
            except FileNotFoundError:
                return ("", winreg.REG_EXPAND_SZ)
        return (value, value_type)

    def write(self, value: str, value_type: int) -> None:
        winreg = self._module()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENVIRONMENT_SUBKEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, PATH_VALUE_NAME, 0, value_type, value)

    def broadcast(self) -> None:
        broadcast_environment_change(send_message_timeout)


#: The installation's log file, in the home: every install run replaces it.
LOG_FILENAME = "install.log"

#: How many of the log's last lines a failed run prints above its refusal.
LOG_TAIL_LINES = 20

#: The userinfo of a URL, greedy to the **last** ``@`` before whitespace: pip splits the
#: netloc at the last ``@`` and accepts a raw ``@`` -- and a raw ``/`` -- in a password.
_URL_USERINFO = re.compile(r"://\S*@")


def redacted(text: str) -> str:
    """*text* with the userinfo of every URL in it replaced by ``***`` -- for the log, never the console.

    Greedy to the last ``@`` before whitespace, so ``https://u:p@ss@host`` leaks no part
    of the password; a URL whose path or query holds an ``@`` has its host and path
    masked too. Not reached: a token as a query parameter (``?token=...``), and
    anything a child program prints about its own arguments.
    """
    return _URL_USERINFO.sub("://***@", text)


def write_log_line(log, text: str) -> None:
    """One redacted line into the binary *log*: UTF-8, ``\n``.

    Binary because subprocesses write into the same handle; a text wrapper would keep a
    buffer they never see.
    """
    log.write((redacted(text) + "\n").encode("utf-8"))


def console_safe(text: str) -> str:
    """*text* printable on any console: every non-ASCII character as a backslash escape.

    A piped Windows ``stdout`` is the locale's code page with ``errors="strict"``.
    """
    return text.encode("ascii", "backslashreplace").decode("ascii")


#: The characters ``cmd.exe`` re-reads as structure in an argument after ``/c``:
#: ``&`` ``|`` ``<`` ``>`` separate or redirect, ``^`` escapes, ``%`` expands, ``"``
#: quotes. ``(`` and ``)`` are absent because they are ordinary text in a path such as
#: ``C:\Program Files (x86)\...`` (the leading token is defused by :func:`code_argv`'s
#: ``call``); ``!`` matters only under delayed expansion, which ``cmd /c`` does not enable.
CMD_REPARSED_CHARACTERS = '&|<>^%"'


def cmd_reparsed_part(argv: Sequence[str]) -> tuple[str, str] | None:
    """The first argument ``cmd.exe`` would re-read, with the character; else ``None``.

    ``None`` for every argv whose program is not the command interpreter, where those
    characters are ordinary.
    """
    if not argv:
        return None
    if os.path.basename(str(argv[0])).lower() not in ("cmd", "cmd.exe"):
        return None
    for part in argv[1:]:
        for character in str(part):
            if character in CMD_REPARSED_CHARACTERS:
                return (str(part), character)
    return None


def run_argv(argv: Sequence[str], log=None) -> int:
    """Run *argv* and answer its exit code. A list, never a shell string.

    Raises:
        ProgramWillNotStart: the program cannot be started (an ``OSError``), or its
            program is ``cmd.exe`` and an argument carries one of
            :data:`CMD_REPARSED_CHARACTERS`. ``subprocess`` quotes a list for the C
            runtime, but ``cmd.exe`` re-parses everything after ``/c``, so a path
            under ``C:\\a&b\\`` would run a second command. A refusal rather than
            escaping, because ``cmd`` escaping and C-runtime quoting interact.

    What this does **not** reach: Windows behaviour is documented, not observed; the
    leading token (``cmd /?``'s rule 2) is defused by :func:`code_argv`'s ``call``,
    not here; and the refusal is per-argument and unconditional, so a harmless ``%``
    in a ``.vsix`` filename is refused too.

    With a *log* (an open binary ``install.log``) the output goes there, stdout and
    stderr merged, after a redacted ``$ argv`` line flushed first (the child writes
    through its own duplicate of the handle). Stdin is then ``DEVNULL``, so a program
    that prompts reads EOF and fails into the log rather than waiting unseen. Without
    a log the console and the inherited stdin are used.
    """
    reparsed = cmd_reparsed_part(argv)
    if reparsed is not None:
        part, character = reparsed
        reason = f"`{argv[0]}` re-reads everything after `/c` as a command line and `{part}` contains `{character}`"
        raise ProgramWillNotStart(
            f"{reason}, so it was not started and nothing further was changed: use a path with none of "
            f"{CMD_REPARSED_CHARACTERS} in it",
            str(argv[0]),
            reason,
        )
    if log is not None:
        # A log that cannot take the line (a full disk) costs the line, never the step.
        with contextlib.suppress(OSError):
            write_log_line(log, "$ " + " ".join(str(part) for part in argv))
            log.flush()
    try:
        return subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL if log is not None else None,
            stdout=log,
            stderr=subprocess.STDOUT if log is not None else None,
        ).returncode
    except OSError as error:
        raise ProgramWillNotStart(
            f"`{argv[0]}` could not be started ({error}), so nothing further was changed: check that it "
            f"is installed and that the path is right",
            argv[0],
            str(error),
        ) from None


def echo_line(line: str, end: str = "\n") -> None:
    """Print one line of a step's report; :func:`execute_plan`'s ``echo`` seam.

    Outside it: :func:`main`'s announcements and refusals, the subprocesses' own
    output, and :class:`Progress`'s numbered lines (its own ``console`` seam, which
    defaults to this). Flushed, because a piped ``stdout`` is block-buffered while
    ``stderr`` is not, and a refusal would otherwise arrive before earlier lines.
    *end* lets an open progress line be printed without its newline.
    """
    print(line, end=end, flush=True)


def read_stamp(path: str) -> dict:
    """``wheels/PLATFORM.json`` as a dict, or a refusal naming the file."""
    if not os.path.isfile(path):
        raise InstallError(
            f"{path} is missing, so the wheels here cannot say which platform they are for: unpack the "
            f"archive again, or name a directory that has one with --source"
        )
    with open(path, encoding="utf-8") as handle:
        try:
            stamp = json.load(handle)
        except ValueError:
            raise InstallError(f"{path} is not readable JSON, so the archive is damaged: download it again") from None
    if not isinstance(stamp, dict):
        raise InstallError(f"{path} is not a JSON object, so the archive is damaged: download it again")
    return stamp


def current_stamp() -> dict:
    """The two keys :func:`check_platform_stamp` compares, for the running interpreter.

    Restates ``build_artifacts.wheelhouse_platform``'s derivation; this file cannot import it.
    """
    return {
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "platform_tag": sysconfig.get_platform(),
    }


#: How long a probe may take before it is reported instead of waited on: an
#: interpreter that starts and blocks would otherwise hang the installer silently,
#: and under the extension with no cancellation. Far above a normal answer, not a
#: measurement. The run itself (``venv``, ``pip``, ``code``) is not bounded: a slow
#: network is not a hang.
PROBE_TIMEOUT_SECONDS = 30

#: What :func:`interpreter_stamp` asks another interpreter to print; ``%``-formatting
#: and single quotes so it survives as an argv part and parses on any interpreter.
#: ``build_artifacts.py``'s ``_INTERPRETER_STAMP_PROGRAM`` restates it with
#: ``major.minor.micro``.
_STAMP_PROGRAM = (
    "import json, sys, sysconfig; "
    "print(json.dumps({'python_version': '%d.%d' % sys.version_info[:2], "
    "'platform_tag': sysconfig.get_platform()}))"
)


def interpreter_stamp(argv: Sequence[str] = ()) -> dict:
    """The two keys :func:`check_platform_stamp` compares, from *argv*'s interpreter.

    The interpreter that builds the venv is the one the wheels must suit, not the one
    running this file. An empty *argv* falls back to :func:`current_stamp`. Bounded
    and stdin-less, like every probe.

    Raises:
        InstallError: the program cannot be started, does not answer in time, exits
            non-zero, or answers with no JSON object (a banner ahead of the answer
            reads as the last).
    """
    if not argv:
        return current_stamp()
    try:
        completed = subprocess.run(
            list(argv) + ["-c", _STAMP_PROGRAM],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except OSError as error:
        raise InstallError(
            f"`{argv[0]}` could not be started ({error}), so the interpreter this installation would be "
            f"built with could not be asked what it is: name one with --python PATH"
        ) from None
    except subprocess.TimeoutExpired:
        raise InstallError(
            f"`{argv[0]}` did not answer within {PROBE_TIMEOUT_SECONDS} seconds when asked which platform "
            f"it is for, so it is not an interpreter this installer can build with: name one with "
            f"--python PATH"
        ) from None
    if completed.returncode != 0:
        raise InstallError(
            f"`{argv[0]}` exited {completed.returncode} instead of saying which platform it is for, so it "
            f"is not an interpreter this installer can build with: name one with --python PATH"
        )
    try:
        stamp = json.loads(completed.stdout.decode("utf-8", "replace"))
    except ValueError:
        stamp = None
    if not isinstance(stamp, dict):
        raise InstallError(
            f"`{argv[0]}` did not answer which platform it is for, so it is not an interpreter this "
            f"installer can build with: name one with --python PATH"
        )
    return stamp


def venv_is_usable(interpreter: str) -> bool:
    """Whether the venv interpreter at *interpreter* is there **and starts**.

    An interrupted ``venv`` leaves no interpreter; a removed base Python leaves one
    that will not start. A timeout also answers "no", which plans a rebuild. Not
    reached: whether ``pip`` or the packages inside are sound (a repair's
    ``--force-reinstall`` covers that).
    """
    if not os.path.isfile(interpreter):
        return False
    try:
        completed = subprocess.run(
            [interpreter, "-c", ""],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except OSError:
        return False
    except subprocess.TimeoutExpired:
        return False
    return completed.returncode == 0


#: ``IO_REPARSE_TAG_MOUNT_POINT``, spelled here because ``stat`` carries it on Windows alone.
_JUNCTION_REPARSE_TAG = 0xA0000003


def is_link(path: str) -> bool:
    """Whether *path* is a link this installer makes: a symlink, or a Windows junction.

    ``os.path.islink`` is ``False`` for a junction, so its reparse tag is read
    (``st_reparse_tag``, Windows only). A reparse point of another kind reads as the
    real directory or file.
    """
    if os.path.islink(path):
        return True
    try:
        tag = getattr(os.lstat(path), "st_reparse_tag", None)
    except OSError:
        return False
    return tag == _JUNCTION_REPARSE_TAG


def remove_link(path: str) -> None:
    """Remove a symlink or a junction, and never what it points at.

    ``os.remove`` takes a symlink; a junction needs ``rmdir``, which does not follow it.
    """
    try:
        os.remove(path)
    except OSError:
        os.rmdir(path)


def remove_tree(path: str) -> None:
    """Delete a version directory or the whole home; a missing one is already done.

    Not ``ignore_errors``: a locked directory is a sentence the caller prints. A link
    is removed as a link, never followed.
    """
    if not os.path.lexists(path):
        return
    if is_link(path):
        remove_link(path)
        return
    if os.path.isfile(path):
        os.remove(path)
        return
    shutil.rmtree(path)


def replace_link(path: str, target: str, platform: str) -> None:
    """Point *path* at *target*, replacing an existing **link** and never a directory.

    Remove-then-create, since a junction cannot be re-pointed atomically. A real
    directory where the link belongs is refused rather than deleted. Windows tries
    ``_winapi.CreateJunction`` (private, undocumented), then ``cmd.exe /c mklink /J``,
    then ``os.symlink`` (which needs Developer Mode).
    """
    if os.path.lexists(path):
        if not is_link(path):
            raise InstallError(
                f"{path} is a real directory and not the link this installer makes, so nothing was changed: "
                f"move it aside and run the installer again"
            )
        try:
            remove_link(path)
        except OSError as error:
            raise InstallError(
                f"{path} could not be replaced ({error}), so the installation is not current: close "
                f"anything that is using it and run the installer again"
            ) from None
    parent = os.path.dirname(path)
    try:
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        if platform == "nt":
            _create_windows_junction(path, target)
            return
        os.symlink(target, path)
    except InstallError:
        raise
    except OSError as error:
        raise InstallError(
            f"{path} could not be pointed at {target} ({error}), so the installation is not current: run "
            f"the installer again, on Windows from an elevated prompt"
        ) from None


def _create_windows_junction(path: str, target: str) -> None:
    """The three Windows spellings of "point this directory name at that one"."""
    attempts = []
    try:
        import _winapi

        _winapi.CreateJunction(target, path)
        return
    except (ImportError, AttributeError, OSError) as error:
        attempts.append(f"_winapi.CreateJunction ({error})")
    try:
        code = run_argv(["cmd.exe", "/c", "mklink", "/J", path, target])
    except InstallError as error:
        # An unstartable or refused `cmd.exe` is a failed attempt, so `os.symlink`
        # still gets its turn.
        attempts.append(f"mklink /J ({_reason_of(error)})")
    else:
        if code == 0:
            return
        attempts.append(f"mklink /J (exit code {code})")
    try:
        os.symlink(target, path, target_is_directory=True)
        return
    except OSError as error:
        attempts.append(f"os.symlink ({error})")
    raise InstallError(
        f"none of the three ways of linking {path} to {target} worked ({'; '.join(attempts)}), so the "
        f"installation is not current: run the installer again from an elevated prompt"
    )


def payload_member_parts(archive: str, names: Sequence[str]) -> list[tuple[str, ...]]:
    """Each member name of a payload as path segments, with the vendor's leading directory stripped.

    Every vendor payload has exactly one leading directory (``node-v<version>-<platform>/``,
    ``python/``); stripping it keeps installed directories version-free, so a second
    leading directory is refused. A name that is absolute, has a ``..`` segment or a
    ``:`` is refused, since it is joined onto the unpack directory. The leading
    directory's own entry answers ``()``; a *file* answering ``()`` sits at the root
    and the unpacking loops refuse it.
    """
    stripped = []
    leading = None
    for name in names:
        spelled = name.replace("\\", "/")
        parts = [part for part in spelled.split("/") if part not in ("", ".")]
        if spelled.startswith("/") or ":" in spelled or ".." in parts or not parts:
            raise InstallError(
                f"{archive} holds a member named {name!r}, which would be unpacked outside the directory it "
                f"belongs in, so nothing was unpacked from it: download the archive again"
            )
        if leading is None:
            leading = parts[0]
        if parts[0] != leading:
            raise InstallError(
                f"{archive} holds {parts[0]!r} beside {leading!r}, and a payload has exactly one leading "
                f"directory, so nothing was unpacked from it: download the archive again"
            )
        stripped.append(tuple(parts[1:]))
    return stripped


def _file_at_the_root_refusal(archive: str, name: str) -> InstallError:
    """The refusal for a member outside any leading directory; the caller raises it, so every site reads as a stop."""
    return InstallError(
        f"{archive} holds {name!r} at its root rather than inside one leading directory, so nothing was "
        f"unpacked from it: download the archive again"
    )


def link_stays_inside(parts: Sequence[str], linkname: str) -> bool:
    """Whether a symbolic link at *parts* pointing at *linkname* resolves inside the unpacked tree.

    Node.js tarballs carry relative links (``bin/npm`` and others), so a relative
    link that stays inside is allowed; one out of the tree would let a later member
    be written anywhere. Lexical only: a chain of links each inside on paper can leave
    the tree on disk, which :func:`_resolves_inside` catches. A backslash is refused
    (``\\x`` is rooted on Windows).
    """
    if not linkname or linkname.startswith("/") or ":" in linkname or "\\" in linkname:
        return False
    joined = "/".join(list(parts[:-1]) + [linkname])
    normalized = os.path.normpath(joined).replace("\\", "/")
    return normalized != ".." and not normalized.startswith("../") and not os.path.isabs(normalized)


def _resolves_inside(real_root: str, candidate: str) -> bool:
    """Whether *candidate*, every link on its way resolved as the disk holds it **now**, is inside *real_root*.

    *real_root* is already resolved. A component that does not exist yet resolves
    lexically, which is safe because no member name carries ``..``.
    """
    root = os.path.normcase(real_root)
    resolved = os.path.normcase(os.path.realpath(candidate))
    return resolved == root or resolved.startswith(root.rstrip(os.sep) + os.sep)


def _write_outside_refusal(archive: str, name: str) -> InstallError:
    """The refusal for a member that would be written through a link out of the tree; the caller raises it."""
    return InstallError(
        f"{archive} holds {name!r}, which would be written through a link to outside the directory it "
        f"belongs in, so nothing was unpacked from it: download the archive again"
    )


def _empty_payload_refusal(archive: str) -> InstallError:
    """The refusal for a payload with nothing inside its leading directory; the caller raises it."""
    return InstallError(
        f"{archive} holds nothing inside its leading directory, so there was nothing to unpack: download "
        f"the archive again"
    )


def _restore_mode(path: str, mode: int) -> None:
    """Give *path* the vendor's permission bits -- on POSIX only.

    On Windows ``os.chmod`` would map a mode without the write bit onto the read-only
    attribute, and the next update's removal would fail on the file.
    """
    if os.name == "nt":
        return
    os.chmod(path, mode & 0o777)


def _unpack_tar(archive: str, staging: str) -> None:
    """A ``.tar.xz`` / ``.tar.gz`` payload into *staging*: files, directories, links and nothing else.

    Member by member, not ``extractall``: the leading directory is stripped, and
    ``filter=`` is missing on older interpreters. Files and hard links get the
    vendor's mode; a directory gets it with the owner's ``rwx`` added, so the next
    update can remove it. Modification times are not restored.

    Nothing is written through a link out of the tree: before each write the path (or
    a hard link's source) is resolved on disk and must lie inside *staging*, and once
    all members are written every link is resolved again, since a later member can
    change where an earlier link points. A checked parent is not rechecked: nothing
    in an unpack turns a directory into a link. Not reached: a link inside the tree
    to a file the payload does not carry (it dangles).
    """
    real_staging = os.path.realpath(staging)
    with tarfile.open(archive, "r:*") as bundle:
        members = bundle.getmembers()
        directories = []
        links = []
        checked_parents = set()
        for member, parts in zip(members, payload_member_parts(archive, [member.name for member in members])):
            if not parts:
                if not member.isdir():
                    raise _file_at_the_root_refusal(archive, member.name)
                continue
            path = os.path.join(staging, *parts)
            if member.isdir():
                if not _resolves_inside(real_staging, path):
                    raise _write_outside_refusal(archive, member.name)
                os.makedirs(path, exist_ok=True)
                directories.append((path, member.mode))
                continue
            parent = os.path.dirname(path)
            if parent not in checked_parents:
                if not _resolves_inside(real_staging, parent):
                    raise _write_outside_refusal(archive, member.name)
                os.makedirs(parent, exist_ok=True)
                checked_parents.add(parent)
            if os.path.islink(path) and not _resolves_inside(real_staging, path):
                # A second member of the same name, written over a link an earlier one planted.
                raise _write_outside_refusal(archive, member.name)
            if member.isfile():
                with bundle.extractfile(member) as source, open(path, "wb") as handle:
                    shutil.copyfileobj(source, handle)
                _restore_mode(path, member.mode)
            elif member.issym() and link_stays_inside(parts, member.linkname):
                os.symlink(member.linkname, path)
                links.append((path, member.name))
            elif member.islnk():
                # A hard link names its target by full member name, so it is stripped the same way.
                linked = os.path.join(staging, *payload_member_parts(archive, [member.name, member.linkname])[1])
                if not _resolves_inside(real_staging, linked):
                    raise _write_outside_refusal(archive, member.name)
                shutil.copyfile(linked, path)
                _restore_mode(path, member.mode)
            else:
                raise InstallError(
                    f"{archive} holds {member.name!r}, which is neither a file, a directory nor a link that "
                    f"stays inside the payload, so nothing was unpacked from it: download the archive again"
                )
        if not directories and not checked_parents:
            raise _empty_payload_refusal(archive)
        for path, name in links:
            if not _resolves_inside(real_staging, path):
                raise _write_outside_refusal(archive, name)
        for path, mode in reversed(directories):
            # Spelled without `|`, which a test's scan reads as a runtime union.
            _restore_mode(path, (mode & 0o077) + 0o700)


def _unpack_zip(archive: str, staging: str) -> None:
    """A ``.zip`` payload -- Node.js for Windows -- into *staging*.

    A POSIX mode in ``external_attr`` is restored where present. A symbolic link is
    refused, since ``zipfile`` would write its text into an ordinary file; with no
    link written, no path can pass through one.
    """
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        placed = 0
        for info, parts in zip(infos, payload_member_parts(archive, [info.filename for info in infos])):
            if not parts:
                if not info.is_dir():
                    raise _file_at_the_root_refusal(archive, info.filename)
                continue
            placed += 1
            path = os.path.join(staging, *parts)
            if info.is_dir():
                os.makedirs(path, exist_ok=True)
                continue
            mode = (info.external_attr >> 16) & 0o170777
            if stat.S_ISLNK(mode):
                raise InstallError(
                    f"{archive} holds {info.filename!r} as a symbolic link, which a zip payload may not carry, "
                    f"so nothing was unpacked from it: download the archive again"
                )
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with bundle.open(info) as source, open(path, "wb") as handle:
                shutil.copyfileobj(source, handle)
            if mode & 0o777:
                _restore_mode(path, mode)
        if not placed:
            raise _empty_payload_refusal(archive)


def _unpack_errors() -> tuple[type[BaseException], ...]:
    """What reading a damaged payload can raise, beyond a refusal of this file's own.

    ``lzma`` and ``zlib`` are looked up, not imported, so an interpreter without
    ``lzma`` still starts this file.
    """
    errors: list[type[BaseException]] = [OSError, EOFError, ValueError, tarfile.TarError, zipfile.BadZipFile]
    for module_name, attribute in (("zlib", "error"), ("lzma", "LZMAError")):
        module = sys.modules.get(module_name)
        if module is not None:
            errors.append(getattr(module, attribute))
    return tuple(errors)


def _discard(path: str) -> None:
    """Remove a staging directory if it can be removed; a leftover is retried by the next run."""
    try:
        remove_tree(path)
    except OSError:
        pass


def rename_tree(source: str, target: str) -> None:
    """Rename the directory *source* to *target*, retrying a ``PermissionError`` through :data:`RENAME_RETRY_PAUSES`.

    The last attempt's ``PermissionError`` propagates unchanged; any other ``OSError``
    propagates at once. What this does **not** reach: a retry that succeeds leaves no
    trace in ``install.log``; a scanner holding a file longer than the pauses still
    gets the refusal; the race is reproduced by no runner; a slow antivirus in the
    wheel or Node.js steps is not addressed; and :data:`RENAME_RETRY_CLAUSE` does not
    state every wait (a ``PermissionError`` followed by another ``OSError`` prints no
    clause, and the rollback's wait is not counted).
    """
    for delay in RENAME_RETRY_PAUSES:
        try:
            os.rename(source, target)
            return
        except PermissionError:
            time.sleep(delay)
    os.rename(source, target)


def _not_replaced(destination: str, error: OSError, where: str, *, renamed: bool) -> InstallError:
    """The swap's one refusal; *renamed* says whether *error* came from :func:`rename_tree`, which retried it."""
    retried = RENAME_RETRY_CLAUSE if renamed and isinstance(error, PermissionError) else ""
    return InstallError(
        f"{destination} could not be replaced ({error}){retried}, so the run stopped before `current` was "
        f"switched and {where}: close anything running from this installation, such as the SWCraft MCP server "
        f"VS Code started, and run the installer again"
    )


def _put_back(previous: str, destination: str, *, wait: bool) -> bool:
    """Rename the tree moved aside back to *destination*; ``False`` when that is refused.

    With *wait* the rename is retried; without it (after an interrupt) it is one
    ``os.rename``. An interrupt during the retries gets one last attempt, then propagates.
    """
    try:
        if wait:
            rename_tree(previous, destination)
        else:
            os.rename(previous, destination)
    except OSError:
        return False
    except BaseException:
        with contextlib.suppress(OSError):
            os.rename(previous, destination)
        raise
    return True


def unpack_payload(archive: str, destination: str) -> None:
    """The ``runtime`` step: unpack *archive* into *destination*, replacing whatever tree was there.

    Unpacked into ``<destination>.unpacking`` and swapped in only when whole, so a
    failure leaves the previous tree. ``.zip`` (Windows Node.js) or a tar by the
    member name. Each swap rename is retried by :func:`rename_tree`; an interrupt
    while the previous tree is aside puts it back with one attempt, discards the
    staging tree and propagates. Every failure is one sentence, with ``current`` not
    yet moved.

    What this does **not** reach: a put-back still refused leaves the previous tree at
    ``<destination>.previous`` unnamed; a second interrupt; a process killed outright;
    whether the unpacked tree runs; and a Windows path over the length limit.
    """
    staging = destination + UNPACKING_SUFFIX
    try:
        remove_tree(staging)
        os.makedirs(staging)
        if archive.lower().endswith(".zip"):
            _unpack_zip(archive, staging)
        else:
            _unpack_tar(archive, staging)
    except InstallError:
        _discard(staging)
        raise
    except _unpack_errors() as error:
        _discard(staging)
        # `tarfile` explains a file it cannot open over several lines (one per
        # compression it tried), and a refusal here is one line.
        reason = " ".join(str(error).split())
        raise InstallError(
            f"{archive} could not be unpacked into {destination} ({reason}), so the run stopped before "
            f"`current` was switched: unpack the archive again and run the installer again"
        ) from None
    # Rename the previous tree aside rather than delete it: on a repair it is the one
    # `current` points at, and a delete stopping at a file in use (the MCP server's
    # Python DLL, a running node.exe) would leave it half gone; a rename fails whole.
    previous = destination + SUPERSEDED_SUFFIX
    unchanged = "the tree that was there is unchanged"
    try:
        remove_tree(previous)
    except OSError as error:
        _discard(staging)
        raise _not_replaced(destination, error, unchanged, renamed=False) from None
    moved_aside = os.path.lexists(destination)
    if moved_aside:
        try:
            rename_tree(destination, previous)
        except OSError as error:
            _discard(staging)
            raise _not_replaced(destination, error, unchanged, renamed=True) from None
    # The live tree is not at its name until this rename, and retries make that gap
    # seconds long, so an interrupt is caught too.
    try:
        rename_tree(staging, destination)
    except BaseException as error:
        interrupted = not isinstance(error, OSError)
        restored = True
        try:
            if moved_aside:
                restored = _put_back(previous, destination, wait=not interrupted)
        finally:
            _discard(staging)
        if interrupted:
            raise
        where = unchanged if restored else f"the tree that was there is at {previous}"
        raise _not_replaced(destination, error, where, renamed=True) from None
    # Untidy and never a failure: the new tree is in place, and the next run removes a leftover first.
    _discard(previous)


def place_node(path: str, target: str, platform: str) -> None:
    """The ``node`` step: a relative symbolic link on POSIX, a **copy** on Windows.

    Placed in the venv's executable directory, the one directory on ``PATH``, and
    removed with the version directory. On Windows the entry is appended to ``PATH``,
    so an earlier ``node`` wins; the POSIX advice prepends, so this one wins.

    An existing file or link at *path* is replaced; a directory there is refused. A
    POSIX link that does not resolve afterwards is removed and refused (the payload
    carried no ``bin/node``).
    """
    try:
        if os.path.lexists(path):
            if os.path.isdir(path) and not os.path.islink(path):
                raise InstallError(
                    f"{path} is a directory and not the node this installer places, so nothing was changed: "
                    f"move it aside and run the installer again"
                )
            os.remove(path)
        if platform == "nt":
            shutil.copyfile(target, path)
            return
        os.symlink(target, path)
    except InstallError:
        raise
    except OSError as error:
        raise InstallError(
            f"node could not be placed at {path} ({error}), so the run stopped before `current` was "
            f"switched: close anything running Node.js from this installation and run the installer again"
        ) from None
    if not os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
        raise InstallError(
            f"{path} would point at {target}, which the Node.js payload does not carry, so the run stopped "
            f"before `current` was switched: download the archive again"
        )


def node_placed_sentence(step: Step, platform: str) -> str:
    """The one sentence the ``node`` step reports, naming the directory it placed ``node`` in."""
    directory = pure_path(platform)(step.path).parent
    verb = "copied into" if platform == "nt" else "linked into"
    return f"Node.js for the diagram generator is {verb} {directory}, beside swcraft"


def bootstrap_left_sentence(path: str, error: object) -> str:
    """The one sentence a ``.bootstrap/`` that could not be removed gets off Windows: what it is, and who removes it.

    ``swcraft_install.sh`` ends in ``exec``, so a copy left behind is the reader's to
    delete. On Windows nothing is said: the running ``python.exe`` lives there, and
    ``swcraft_install.cmd`` removes the directory after this process exits. Not
    reached: ``install.py`` run directly on the wrapper's interpreter on Windows
    leaves a ``.bootstrap`` with no line naming it.
    """
    what = f"{path} could not be removed ({error}); it holds only the copy of Python this installer started from"
    return f"{what}, which nothing uses any more: delete that directory yourself"


def require_installation(home) -> None:
    """Refuse to remove a *home* that is not a SWCraft installation (data loss).

    ``--uninstall`` removes whatever ``--home`` names, so a mistyped value would be
    unrecoverable. A SWCraft home holds ``versions/``. Not reached: a directory
    somebody put a ``versions/`` in, and an installation whose ``versions/`` was
    removed by hand (refused although ``current`` and the ``PATH`` entry remain; the
    message names the directory).
    """
    versions = os.path.join(str(home), VERSIONS_DIRNAME)
    if not os.path.isdir(versions):
        raise InstallError(
            f"{home} holds no {VERSIONS_DIRNAME} directory, so it is not a SWCraft installation and "
            f"nothing was removed: check the directory --home names"
        )


def installed_versions(home: PurePath) -> tuple[str, ...]:
    """The version directory names already under ``<home>/versions``.

    Sorted over names for a deterministic plan; ordering versions is the extension's.
    """
    directory = str(home / VERSIONS_DIRNAME)
    try:
        names = os.listdir(directory)
    except OSError:
        return ()
    return tuple(sorted(name for name in names if os.path.isdir(os.path.join(directory, name))))


def member_digest(archive_root: str, name: str) -> str | None:
    """The SHA-256 of one archive member, or ``None`` when it is not there."""
    path = os.path.join(archive_root, *name.split("/"))
    if not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(_DIGEST_BLOCK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def verify_archive(args) -> tuple[str, ...]:
    """Check every member against ``SHA256SUMS``; answer the member names.

    :func:`archive_version` reads the same list, so they cannot disagree. A missing
    ``SHA256SUMS`` answers ``()`` with ``--source`` or ``--index-url`` and is refused
    otherwise, rather than failing later as "no matching distribution".
    """
    sums_path = os.path.join(args.archive_root, SHA256SUMS_FILENAME)
    if not os.path.isfile(sums_path):
        if args.source or args.index_url:
            return ()
        raise InstallError(
            f"{args.archive_root} holds no {SHA256SUMS_FILENAME}, so it is not an unpacked SWCraft archive: "
            f"unpack the archive and run the installer that is inside it"
        )
    with open(sums_path, encoding="utf-8") as handle:
        entries = parse_sha256sums(handle.read())
    verify_members(entries, lambda name: member_digest(args.archive_root, name))
    return tuple(name for _, name in entries)


# ---------------------------------------------------------------------------
# Executing a plan
# ---------------------------------------------------------------------------


#: The step kinds whose action starts a subprocess through the ``run`` seam.
SPAWNING_KINDS = frozenset(("venv", "pip", "vsix", "vsix-companion", "vsix-remove"))


def step_label(step: Step, platform: str) -> str:
    """What one progress line calls *step*: a fixed phrase per kind, plus the object it names.

    The fixed text is ASCII: the extension decodes a piped Windows console as UTF-8
    while the child writes its code page. SWCraft's floor is asked inside the first
    ``vsix-companion`` step, so a floor refusal closes that companion's line.
    """
    kind = step.kind
    if kind == "check-platform":
        return "checking the wheels against this machine"
    if kind == "runtime":
        tail = pure_path(platform)(step.path).parts[-2:]
        # The plan unpacks two payloads only: the Python one and one of NODE_PAYLOAD_MEMBERS.
        return "unpacking Python" if tail == tuple(PYTHON_PAYLOAD_MEMBER.split("/")) else "unpacking Node.js"
    if kind == "venv":
        return "creating the Python environment"
    if kind == "pip":
        return f"installing {REQUIREMENT}, {GENERATOR_REQUIREMENT} and their dependencies"
    if kind == "node":
        return "placing node"
    if kind == "link":
        return f"pointing current at {pure_path(platform)(step.target).name}"
    if kind == "path":
        return "adding bin to PATH" if platform == "nt" else "PATH entry"
    if kind == "vsix-companion":
        return f"installing the editor extension {step.target}"
    if kind == "vsix":
        return "installing the SWCraft editor extension"
    if kind == "remove":
        return f"removing {step.path}"
    if kind == "bootstrap-remove":
        return "removing the installer's temporary Python"
    if kind == "path-remove":
        return "removing bin from PATH"
    if kind == "vsix-remove":
        return "removing the SWCraft editor extension"
    raise InstallError(f"{kind!r} is not a step this installer knows how to run")  # pragma: no cover


class Progress:
    """One numbered line per step on the console, mirrored into the log.

    ``start`` opens ``"{i}/{N} {label} ... "`` and ``finish`` closes it with
    ``"{done|failed}, {seconds} s"``. The step's sentences are held by :meth:`echo`
    and handed to ``forward`` once the line is closed, so none lands inside a
    progress line. With ``inherits_console`` (``--verbose``, ``--uninstall``) the
    subprocesses write to the console, so the start line is printed whole and closed
    by its own ``"{i}/{N} done, {seconds} s"``. A step that returned is ``done``,
    including the non-fatal ones.

    With a *log* the same lines go into ``install.log`` (``== i/N label``,
    ``== done|failed, s s``, then the sentences); a spawning step whose output went to
    the console logs ``(output went to the console: --verbose)``. Not flushed here. A
    log write's ``OSError`` costs that line only.

    :attr:`output_then_sentences` says a step whose program wrote into the log ended
    with a sentence, so a successful run points at the log.
    """

    def __init__(
        self,
        total: int,
        console: Callable | None = None,
        clock: Callable | None = None,
        inherits_console: bool = False,
        log=None,
    ) -> None:
        self.total = total
        self.console = echo_line if console is None else console
        self.clock = time.monotonic if clock is None else clock
        self.inherits_console = inherits_console
        self.log = log
        self._index = 0
        self._began = 0.0
        self._spawns = False
        self._held = []
        self.output_then_sentences = False

    def _log_line(self, text: str) -> None:
        """One line into the log, if there is one; an ``OSError`` costs the line only."""
        if self.log is not None:
            with contextlib.suppress(OSError):
                write_log_line(self.log, text)

    def start(self, index: int, label: str, spawns: bool = False) -> None:
        """Open step *index*'s line; *spawns* says the step starts a subprocess."""
        self._index = index
        self._spawns = spawns
        self._held = []
        opening = f"{index}/{self.total} {label} ..."
        self._log_line(f"== {index}/{self.total} {label}")
        if spawns and self.inherits_console:
            self._log_line("(output went to the console: --verbose)")
        if self.inherits_console:
            self.console(opening)
        else:
            self.console(opening + " ", end="")
        self._began = self.clock()

    def echo(self, sentence: str) -> None:
        """Hold one of the running step's sentences until its line is closed."""
        self._held.append(sentence)

    def finish(self, failed: bool, forward: Callable[[str], None]) -> None:
        """Close the line with the outcome and the whole seconds, then hand each held sentence to *forward*."""
        outcome = f"{'failed' if failed else 'done'}, {self.clock() - self._began:.0f} s"
        if self.inherits_console:
            self.console(f"{self._index}/{self.total} {outcome}")
        else:
            self.console(outcome)
        held, self._held = self._held, []
        self._log_line(f"== {outcome}")
        for sentence in held:
            self._log_line(sentence)
        if held and self._spawns and self.log is not None and not self.inherits_console:
            self.output_then_sentences = True
        for sentence in held:
            forward(sentence)


def execute_plan(
    steps: Iterable[Step],
    platform: str,
    run: Callable | None = None,
    link: Callable | None = None,
    remove: Callable | None = None,
    registry: object | None = None,
    echo: Callable | None = None,
    stamp: Callable | None = None,
    current: Callable | None = None,
    floor: Callable | None = None,
    unpack: Callable | None = None,
    place: Callable | None = None,
    progress: Progress | None = None,
) -> tuple[str, ...]:
    """Do what :func:`plan_install` planned, in order, one call per step.

    Every step kind reaches one seam with a real default: ``run`` a subprocess,
    ``link`` the ``current`` directory, ``remove`` a directory, ``registry`` the
    Windows user ``PATH``, ``echo`` a report, ``stamp`` + ``current`` the two halves
    of ``check-platform``, ``unpack`` a payload, ``place`` the unpacked ``node``.
    ``floor`` (:func:`engine_floor_refusal`) is asked of SWCraft's own ``vsix`` step
    once, before the first companion, so a VS Code too old for SWCraft is refused
    before anything is handed over; never of a companion.

    Steps that do not fail the run, each one sentence instead: a ``remove`` raising
    ``OSError`` (the new version is already ``current``); a ``vsix`` or
    ``vsix-companion`` step with no ``code``; any ``vsix-companion`` failure;
    every ``vsix-remove`` outcome (the home is already gone, and ``code`` exits
    non-zero for an extension that was never installed); ``bootstrap-remove`` (silent
    on Windows, where the wrapper removes it). Everything else raises
    :class:`InstallError` and stops the run; before ``link`` that leaves ``current``
    at the previous version.

    Each step is one :class:`Progress` line (by default console-only); its sentences
    reach ``echo`` once the line is closed, ``failed`` when the step raised.

    Returns:
        The paths of the ``remove`` steps that failed, in order; :func:`main` refuses
        an ``--uninstall`` success over any.
    """
    run = run_argv if run is None else run
    remove = remove_tree if remove is None else remove
    echo = echo_line if echo is None else echo
    stamp = read_stamp if stamp is None else stamp
    current = interpreter_stamp if current is None else current
    floor = engine_floor_refusal if floor is None else floor
    link = functools.partial(replace_link, platform=platform) if link is None else link
    unpack = unpack_payload if unpack is None else unpack
    place = functools.partial(place_node, platform=platform) if place is None else place
    unremoved: list[str] = []
    if registry is None and platform == "nt":
        registry = WindowsUserPath()
    steps = tuple(steps)
    progress = Progress(len(steps)) if progress is None else progress
    # SWCraft's own extension step, whose floor is asked before the first companion.
    package = next((step for step in steps if step.kind == "vsix" and step.argv), None)
    say = progress.echo

    for index, step in enumerate(steps, 1):
        # `step_label` refuses a kind this installer does not know, before anything is done.
        label = step_label(step, platform)
        # A step with no argv (no `code` resolved) starts nothing, whatever its kind.
        progress.start(index, label, spawns=step.kind in SPAWNING_KINDS and bool(step.argv))
        try:
            if step.kind == "check-platform":
                recorded = stamp(step.path)
                check_platform_stamp(recorded, current(step.argv))
            elif step.kind in ("venv", "pip"):
                _require_success(run(step.argv), step.argv)
            elif step.kind == "runtime":
                unpack(step.path, step.target)
            elif step.kind == "node":
                place(step.path, step.target)
                say(node_placed_sentence(step, platform))
            elif step.kind == "link":
                link(step.path, step.target)
            elif step.kind == "path":
                _add_path_entry(step.entry, platform, registry, say)
            elif step.kind == "path-remove":
                _drop_path_entry(step.entry, platform, registry, say)
            elif step.kind == "vsix-companion":
                if package is not None and step.argv:
                    refusal = floor(package)
                    if refusal is not None:
                        raise InstallError(refusal)
                    # Asked once: the `vsix` step takes this answer instead of spawning `code` again.
                    floor, package = _floor_already_answered, None
                _install_companion(step, run, say)
            elif step.kind == "vsix":
                _install_extension(step, run, say, floor)
            elif step.kind == "vsix-remove":
                _uninstall_extension(step, run, say)
            elif step.kind == "remove":
                try:
                    remove(step.path)
                except OSError as error:
                    unremoved.append(step.path)
                    say(
                        f"{step.path} could not be removed ({error}), so the previous version is still on disk: "
                        f"close anything that is using it and remove the directory yourself"
                    )
            elif step.kind == "bootstrap-remove":
                # On Windows the running interpreter lives inside it and
                # `swcraft_install.cmd` removes it after exit: the normal path, so silent.
                try:
                    remove(step.path)
                except OSError as error:
                    if platform != "nt":
                        say(bootstrap_left_sentence(step.path, error))
        except BaseException:
            # The step's own exception is what propagates: a console or a log that cannot
            # take the closing line (a pipe whose reader left) must not replace the refusal.
            with contextlib.suppress(OSError):
                progress.finish(failed=True, forward=echo)
            raise
        progress.finish(failed=False, forward=echo)
    return tuple(unremoved)


def _require_success(code: int, argv: Sequence[str]) -> None:
    if code == 0:
        return
    raise InstallError(
        f"`{' '.join(argv)}` failed with exit code {code}, so nothing further was changed and the "
        f"installation that was there is still the current one"
    )


def _floor_already_answered(step: Step) -> None:
    """The ``floor`` seam once :func:`execute_plan` has asked it before the companions: no refusal."""
    return None


def companion_not_installed_sentence(step: Step, why: str) -> str:
    """The sentence for a companion that was not installed, naming it and its remedy.

    Worded to carry neither fragment ``vscode_extension/src/toolchainInstall.ts`` reads as
    "SWCraft's extension was not installed", because it is not about SWCraft's extension.
    """
    return (
        f"`{step.target}` was not installed ({why}) -- install it by name from the Extensions view, or "
        f"install {step.path} there with Install from VSIX"
    )


def _install_companion(step: Step, run, echo) -> None:
    """The ``vsix-companion`` step: every outcome but success is one sentence and the run goes on.

    The toolchain is installed by now, and a missing pack member costs hover or a
    preview, not the installation. No ``code``, a ``code`` that will not start, and a
    non-zero exit (VS Code's own floor refusal included) are one sentence each. Not
    reached: which companion version VS Code keeps; the archive's copy is a floor, not
    a pin, and the Marketplace may update it.
    """
    if not step.argv:
        echo(companion_not_installed_sentence(step, "VS Code's `code` command was not found"))
        return
    try:
        code = run(step.argv)
    except InstallError as error:
        echo(companion_not_installed_sentence(step, f"`code` could not be started: {_reason_of(error)}"))
        return
    if code != 0:
        echo(companion_not_installed_sentence(step, f"`code` exited with code {code}"))


def _install_extension(step: Step, run, echo, floor=None) -> None:
    """The ``vsix`` step: skip loudly without ``code``, stop on one that ran and failed.

    No ``code`` is skipped with a sentence. A host below SWCraft's floor is refused
    before ``--install-extension`` runs, naming both versions (VS Code's own dialog
    would come later, or not at all when ``code`` dies on start). A ``code`` that
    cannot start or exits non-zero is refused too. The toolchain is installed by now
    either way, and every sentence carries one of the fragments
    ``vscode_extension/src/toolchainInstall.ts`` matches
    (``EXTENSION_NOT_INSTALLED_MARKERS``). Not reached: the companions' own floors.
    """
    if not step.argv:
        echo(
            f"VS Code's `code` command was not found, so the extension was not installed: install "
            f"{step.path} from the Extensions view (Install from VSIX) yourself"
        )
        return
    refusal = (engine_floor_refusal if floor is None else floor)(step)
    if refusal is not None:
        raise InstallError(refusal)
    try:
        code = run(step.argv)
    except InstallError as error:
        # Re-worded: `run_argv`'s "nothing further was changed" is false by now.
        raise InstallError(
            f"`code` could not be started ({_reason_of(error)}), so the toolchain is installed and the "
            f"extension is not: install {step.path} from the Extensions view yourself"
        ) from None
    if code == 0:
        return
    raise InstallError(
        f"`code` could not install the extension (exit code {code}), so the toolchain is installed and "
        f"the extension is not: install {step.path} from the Extensions view yourself"
    )


def _uninstall_extension(step: Step, run, echo) -> None:
    """The ``vsix-remove`` step: **all three** of its outcomes are a sentence, not a refusal.

    The home is already gone when this runs, and ``code --uninstall-extension`` exits
    non-zero for an extension that is not installed, so raising would report a failed
    uninstall after a successful one. No ``code``, a ``code`` that will not start and
    a non-zero exit are each a sentence. Not reached: whether the extension is
    actually gone, hence every sentence names the Extensions view.
    """
    if not step.argv:
        echo(
            "VS Code's `code` command was not found, so the extension was not removed: remove SWCraft "
            "from the Extensions view yourself"
        )
        return
    try:
        code = run(step.argv)
    except InstallError as error:
        echo(
            f"`code` could not be started ({_reason_of(error)}), so the extension was not removed: remove "
            f"SWCraft from the Extensions view yourself"
        )
        return
    if code == 0:
        return
    echo(
        f"`code` did not remove the extension (exit code {code}), which is also what it answers when the "
        f"extension was not installed: check the Extensions view if it is still there"
    )


def _add_path_entry(entry: str, platform: str, registry, echo) -> None:
    """Put *entry* on ``PATH`` once -- the registry on Windows, a printed line elsewhere.

    On Windows the durable value is re-read here, since the process ``PATH`` can be
    stale. Elsewhere no shell file is edited: across shells a silent edit is worse
    than a line the reader pastes.
    """
    if platform != "nt":
        echo(f'Add this to your shell profile and open a new terminal: export PATH="{entry}:$PATH"')
        return
    value, value_type = registry.read()
    appended = appended_path(value, entry, platform)
    if appended is None:
        return
    registry.write(appended, value_type)
    registry.broadcast()


def _drop_path_entry(entry: str, platform: str, registry, echo) -> None:
    """Take *entry* off ``PATH`` again; the POSIX half is advice, as the addition was."""
    if platform != "nt":
        echo(f'Remove this line from your shell profile: export PATH="{entry}:$PATH"')
        return
    value, value_type = registry.read()
    shortened = removed_path(value, entry, platform)
    if shortened is None:
        return
    registry.write(shortened, value_type)
    registry.broadcast()


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def open_install_log(path: str):
    """The default ``open_log``: create the home and open *path* for writing, binary, truncated.

    Created ``0600`` on POSIX (an existing log keeps its mode), since what
    :func:`redacted` misses would otherwise be world-readable. On Windows the file takes
    the home's access list, which nothing here reads.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Distinct bits, so summed: the parse-floor scan reads a runtime `|` as a 3.10 union.
    flags = os.O_WRONLY + os.O_CREAT + os.O_TRUNC + getattr(os, "O_BINARY", 0)
    return os.fdopen(os.open(path, flags, 0o600), "wb")


def _opened_log(open_log: Callable, path: str, header: Callable):
    """*open_log*'s handle for *path* with *header* written into it, or a refusal of its own.

    Not the ``OSError`` backstop's: nothing is installed yet, so "may be incomplete"
    would be false. A handle whose header failed is closed here.
    """
    log = None
    try:
        log = open_log(path)
        header(log)
        return log
    except OSError as error:
        if log is not None:
            with contextlib.suppress(OSError):
                log.close()
        raise InstallError(
            f"{path} could not be created or written ({error.strerror or error}), so nothing was installed: pass "
            f"a --home you can write to"
        ) from None


def _write_log_header(log, args, tokens: Sequence[str], platform: str) -> None:
    """The log's first lines: what ran, when, where, from which archive and with which arguments."""
    write_log_line(log, f"SWCraft {args.version} install")
    write_log_line(log, f"date: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}")
    write_log_line(log, f"platform: {platform} ({sysconfig.get_platform()})")
    write_log_line(log, f"archive: {args.archive_root}")
    write_log_line(log, "argv: " + " ".join(str(token) for token in tokens))


def log_pointer_line(path: str) -> str:
    """``the full log is <path>``, console-safe; the extension matches this spelling."""
    return console_safe(f"the full log is {path}")


def _log_tail(path: str, count: int) -> list[str]:
    """The last *count* lines of the log at *path*, read through a handle of its own.

    Decoded UTF-8 with ``errors="replace"``: a Windows pip writes its code page.
    """
    with open(path, "rb") as handle:
        text = handle.read().decode("utf-8", "replace")
    return text.splitlines()[-count:]


def _report_failure(log, log_path: str | None, refusal: str) -> None:
    """Print a failed run's ending -- the log's tail and its path, then *refusal* on stderr.

    Prints ``the last k lines of <log>:``, the console-safe tail and
    :func:`log_pointer_line`, appends the refusal to the log, flushes ``stdout`` and
    writes the refusal unredacted to ``stderr``. Each step but the last is guarded
    against ``OSError``, so a broken log never costs the refusal. The flush keeps the
    refusal last on a console; across two pipes there is still no order, so the
    extension reads ``stderr`` alone. With no log only the refusal is written.
    """
    if log is not None:
        with contextlib.suppress(OSError):
            log.flush()
            lines = _log_tail(log_path, LOG_TAIL_LINES)
            print(console_safe(f"the last {len(lines)} lines of {log_path}:"))
            for line in lines:
                print("  " + console_safe(line))
            print(log_pointer_line(log_path), flush=True)
        with contextlib.suppress(OSError):
            write_log_line(log, refusal)
    # A closed or broken stdout (the reader went away) must not cost the refusal.
    with contextlib.suppress(OSError):
        sys.stdout.flush()
    sys.stderr.write(refusal + "\n")


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
    archive_root: str | None = None,
    version_info: Sequence[int] | None = None,
    executable: str | None = None,
    current: Callable | None = None,
    open_log: Callable | None = None,
    clock: Callable | None = None,
    **seams: Callable,
) -> int:
    """Read the archive, plan, execute; answer the process exit code.

    Measures what :func:`plan_install` does not read (:func:`installed_versions`,
    :func:`venv_is_usable`, ``which("code")``) and turns an :class:`InstallError` into
    one sentence on stderr and exit 1. ``--uninstall`` is first checked by
    :func:`require_installation` and writes no log. Everything a test replaces is a
    parameter; ``**seams`` are :func:`execute_plan`'s; *clock* is :class:`Progress`'s. Its own
    announcements and refusals go to ``stdout``/``stderr``, not through ``echo``.

    ``--home`` must be absolute under the target platform's rules (:func:`home_is_absolute`): the
    ``link`` step stores it as a target.

    The version floor is held against the interpreter that builds the venv:

    * ``--python P``: *P* is asked (*current*, :func:`interpreter_stamp` by default)
      and refused by :func:`python_refusal` below 3.11, also with ``--index-url``.
    * no ``--python``, a Python payload: the running interpreter must be
      :data:`REQUIRED_PYTHON` (:func:`carried_python_refusal`).
    * no ``--python``, no payload: *version_info* is held to the floor.

    Without ``--python`` the plan gets *executable*; no ``PATH`` lookup happens. An
    argparse usage error on an interpreter below the floor carries the version
    sentence too, unless argv names ``--python``.

    An install opens ``<home>/install.log`` (via *open_log*, by default
    :func:`open_install_log`, only when no ``run`` seam was injected or *open_log*
    was given) and binds ``run`` to it unless ``--verbose``. A log that cannot be
    opened is a refusal before any step. A successful run prints the log path only
    when :attr:`Progress.output_then_sentences`.
    """
    env = os.environ if env is None else env
    platform = os.name if platform is None else platform
    which = shutil.which if which is None else which
    version_info = sys.version_info if version_info is None else version_info
    executable = sys.executable if executable is None else executable
    log = None
    log_path = None
    try:
        tokens = sys.argv[1:] if argv is None else argv
        try:
            args = parse_args(tokens, archive_root=archive_root)
        except SystemExit as exit_code:
            # A usage error only (`--help` exits zero). On an interpreter below the floor
            # the version sentence is the more useful fact, and the check below is too
            # late to print it; with `--python` in argv the running version does not matter.
            refusal = None if _names_python(tokens) else python_refusal(version_info)
            if exit_code.code and refusal is not None:
                raise InstallError(refusal) from None
            raise
        if args.home is not None and not home_is_absolute(args.home, platform):
            # Refused, not absolutised: that would need the running host's cwd while
            # every path here is spelled the target platform's way.
            raise InstallError(
                f"--home must be an absolute path and {args.home} is not: the current link is written "
                f"with it as the target, so a relative one resolves against the link's own directory and "
                f"dangles -- pass the full path"
            )
        home = home_directory(args, env, platform)
        found_python = ()
        names: tuple[str, ...] = ()
        if args.uninstall:
            # Spelled as the running host spells it: `home_directory()` answers the
            # target platform's spelling, which this host may not be able to stat.
            require_installation(args.home if args.home else str(home))
        else:
            names = verify_archive(args)
            if not args.version:
                args.version = archive_version(names)
            if args.python:
                found_python = (args.python,)
                asked = current or interpreter_stamp
                measured = _version_parts(asked(found_python).get("python_version"))
                refusal = None if measured is None else python_refusal(measured, named=args.python)
                if refusal is not None:
                    raise InstallError(refusal)
            else:
                found_python = (executable,)
                if PYTHON_PAYLOAD_MEMBER not in names:
                    refusal = python_refusal(version_info)
                else:
                    refusal = carried_python_refusal(version_info)
                if refusal is not None:
                    raise InstallError(refusal)
        found_code = None if args.no_vsix else which("code")
        steps = plan_install(
            args,
            env,
            platform,
            found_python,
            installed=installed_versions(home),
            found_code=found_code,
            venv_usable=venv_is_usable,
            members=names,
        )
        if args.uninstall:
            print(f"Removing the SWCraft installation in {home}", flush=True)
        else:
            print(f"Installing SWCraft {args.version} into {home}", flush=True)
        if not args.uninstall and (open_log is not None or "run" not in seams):
            # The host's spelling, for the reason the `--uninstall` branch above gives.
            log_path = os.path.join(args.home if args.home else str(home), LOG_FILENAME)
            log = _opened_log(
                open_log or open_install_log,
                log_path,
                functools.partial(_write_log_header, args=args, tokens=tokens, platform=platform),
            )
            if not args.verbose:
                seams = dict(seams)
                seams["run"] = functools.partial(seams.get("run", run_argv), log=log)
        progress = Progress(len(steps), clock=clock, log=log, inherits_console=bool(args.verbose or args.uninstall))
        unremoved = execute_plan(steps, platform, current=current, progress=progress, **seams)
        if args.uninstall:
            if unremoved:
                raise InstallError(
                    f"the SWCraft installation in {home} could not be removed ({', '.join(unremoved)} is "
                    f"still there), so it is still on this machine: close anything that is using it and "
                    f"run --uninstall again"
                )
            print(f"The SWCraft installation in {home} is gone", flush=True)
        else:
            print(f"SWCraft {args.version} is installed in {home}", flush=True)
            if progress.output_then_sentences:
                # A logged program's step ended with a sentence; its reason is only in the file.
                print(log_pointer_line(log_path), flush=True)
    except InstallError as error:
        _report_failure(log, log_path, str(error))
        return 1
    except OSError as error:
        # The backstop for an `OSError` no seam anticipated: still a sentence, not a traceback.
        _report_failure(
            log,
            log_path,
            f"the run was stopped by an operating-system error ({error}), so the installation may be "
            f"incomplete: fix the cause and run the installer again",
        )
        return 1
    finally:
        if log is not None:
            with contextlib.suppress(OSError):
                log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
