"""The one place SWCraft starts the specimen generator: where it is, how it is run, what it answered.

A *specimen* is a SWCraft project the specimen generator (``packages/specimens/``) writes from a
model passport. SWCraft never imports the generator: it starts the executable :data:`PIN_FILE`'s
``generator`` block names and judges what it wrote. Stdlib only, loaded by path.

**Where the generator is** (:func:`configured`): :data:`VARIABLE`, when set, as an absolute path to
an existing file; else the pin file's ``executable`` in the repository's ``.venv``; else
:class:`GeneratorNotConfigured`. ``PATH`` is never searched, and a batch file is refused (``cmd.exe``
would re-parse arguments holding ``&`` or ``%``).

**How it is run** (:func:`run`): no shell, output captured, in a temporary working directory, killed
after 600 seconds. The answer is :class:`Built` (exit 0 and a ``model/``; every other top-level entry
is replaced by SWCraft's manifest, :func:`manifest_text`), :class:`Rejected` (exit 2, which is also
any other error of the generator's and an argparse usage error) or :class:`GeneratorDefect`
(anything else; its cause, never the child's text).

**The specimen cache** (:func:`take`): a specimen is generated on demand into
``<repo>/.cache/specimens/<source digest[:16]>/<passport_sha256[:16]>/``, the source digest covering
the generator's source files on disk, and held to the pin's ``model_sha256`` the first time each
process takes it. A missing or stale key is regenerated under a per-key lock, after the absolute-path
budget is checked; a configured generator whose interpreter is not CPython :data:`GENERATOR_PYTHON`
is refused. A digest that misses after a fresh generation is :class:`SpecimenDigestMismatch`; it, a
rejection and a defect are remembered for the rest of the process. A volume without hard links
raises the take's ``OSError``.

Not reached: why a generator refused a passport (exit 2 is counted, not read); a generator
configured by the variable whose source is not this checkout's; and the numpy and scipy versions,
which are no part of the cache key.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The one variable that replaces ``.venv``'s generator, for every caller.
VARIABLE = "SWCRAFT_SPECIMEN_GENERATOR"

#: The pin file, relative to the repository root: the generator block and the specimens.
PIN_FILE = Path("packages") / "tools" / "tests" / "fixtures" / "specimens" / "specimens.json"

#: The generator's package source, relative to the repository root: what the cache key digests.
SOURCE_DIR = Path("packages") / "specimens" / "src" / "swcraft_specimens"

#: The repository's environment, where ``dev.py install`` installs the generator editable.
VENV_DIR = Path(".venv")

#: The CPython ``major, minor`` the specimens are pinned on (the workstation runtime's, CI's and
#: ``.prototools``'); HiGHS and numpy wheels for another interpreter are unmeasured, so a regeneration
#: by one is refused.
GENERATOR_PYTHON = (3, 11)

#: The run's ceiling in seconds -- a ceiling, not a measurement.
TIMEOUT_SECONDS = 600

#: The generator block's required keys: the console script, the variable the generator's own
#: integration suite names it by, and the modules an install of it must import.
GENERATOR_KEYS = ("executable", "bin_variable", "imports")

#: Retired keys, refused by name: the generator is ``packages/specimens/``'s source, so a pinned
#: artifact, its closure or a recorded contract major could only disagree with it.
REMOVED_GENERATOR_KEYS = (
    "version",
    "archive",
    "archive_sha256",
    "store",
    "bundled",
    "requirements",
    "contract_major",
)

#: Retired top-level keys, refused by name: a contract major moves the generator and the passports in
#: one commit.
REMOVED_PIN_KEYS = ("migration",)

#: What ``generator.bin_variable`` must spell: an upper-case environment variable name.
_ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]*")

#: The suffixes ``configured`` refuses: a batch file is started through ``cmd.exe``.
BATCH_SUFFIXES = (".cmd", ".bat")

#: The suffix of every passport file a caller hands to :func:`run`.
PASSPORT_SUFFIX = ".passport.json"

_MANIFEST_TEMPLATE = """\
# A specimen of SWCraft's test tree, generated into the specimen cache: model/ written by the specimen
# generator (packages/specimens/, started as a separate process) from its committed passport
# (packages/tools/tests/fixtures/specimens/); never edited. No
# `toolchain_version`, for the reason the showcase bench gives.
name: specimen-{name}
schema_version: "1.0"
arxml_version: "4.3.0"
model_root: model
dist_root: dist
views_root: views
outputs:
  arxml: dist/model.arxml
  needs: dist/needs.json
"""


class GeneratorNotConfigured(Exception):
    """No generator can be started; ``reason`` is ``unset``, ``relative``, ``missing`` or ``batch-file``."""

    def __init__(self, reason: str, sentence: str) -> None:
        super().__init__(sentence)
        self.reason = reason


@dataclass(frozen=True)
class Built:
    """Exit 0 and a ``model/``: the project at ``project``, its manifest SWCraft's."""

    project: Path


@dataclass(frozen=True)
class Rejected:
    """Exit 2: ``lines`` non-empty stderr lines; ``text`` only when the caller passed ``keep_stderr=True``."""

    lines: int
    text: str | None = None


@dataclass(frozen=True)
class GeneratorDefect:
    """Anything else: ``cause`` is ``timeout``, ``exit <n>``, ``exit 0 without model/`` or an exception's class."""

    cause: str


Outcome = Built | Rejected | GeneratorDefect


# -- the pin file and the installed generator -------------------------------------------------------


def load_pins(repo_root: Path) -> dict[str, Any]:
    """The pin file as it holds it, its ``generator`` block checked.

    Raises:
        ValueError: not UTF-8 JSON, or a malformed or retired block (one sentence naming the pin file).
        OSError: the pin file cannot be opened.
    """
    try:
        document = json.loads((repo_root / PIN_FILE).read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(f"{PIN_FILE.as_posix()} is not UTF-8 JSON") from None
    if not isinstance(document, dict) or not isinstance(document.get("generator"), dict):
        raise ValueError(f"{PIN_FILE.as_posix()} holds no generator block")
    retired = [key for key in REMOVED_PIN_KEYS if key in document]
    if retired:
        raise ValueError(
            f"{PIN_FILE.as_posix()} holds {', '.join(retired)}, which it no longer carries: the generator is "
            f"{SOURCE_DIR.parent.parent.as_posix()}/, changed with the passports in the same commit"
        )
    generator = document["generator"]
    removed = [key for key in REMOVED_GENERATOR_KEYS if key in generator]
    if removed:
        raise ValueError(
            f"{PIN_FILE.as_posix()}'s generator block holds {', '.join(removed)}, which it no longer carries: the "
            f"generator is the source under {SOURCE_DIR.parent.parent.as_posix()}/, never a pinned artifact"
        )
    missing = [key for key in GENERATOR_KEYS if key not in generator]
    if missing:
        raise ValueError(f"{PIN_FILE.as_posix()}'s generator block lacks {', '.join(missing)}")
    variable = generator["bin_variable"]
    if not isinstance(variable, str) or not _ENVIRONMENT_NAME.fullmatch(variable):
        raise ValueError(
            f"{PIN_FILE.as_posix()}'s generator.bin_variable must be an upper-case variable name, not {variable!r}"
        )
    executable = generator["executable"]
    if not isinstance(executable, str) or not executable or Path(executable).name != executable:
        raise ValueError(f"{PIN_FILE.as_posix()}'s generator.executable must be a file name, not {executable!r}")
    imports = generator["imports"]
    if not isinstance(imports, list) or not imports or not all(isinstance(name, str) and name for name in imports):
        raise ValueError(f"{PIN_FILE.as_posix()}'s generator.imports must be a list of module names")
    return document


def venv_python(venv: Path, os_name: str = os.name) -> Path:
    """The interpreter of *venv*: ``bin/python`` on POSIX, ``Scripts\\python.exe`` on Windows."""
    return venv / "Scripts" / "python.exe" if os_name == "nt" else venv / "bin" / "python"


def installed_executable(repo_root: Path, *, os_name: str = os.name) -> Path:
    """``.venv``'s generator: the pin file's ``executable`` beside the repository environment's interpreter."""
    name = load_pins(repo_root)["generator"]["executable"]
    scripts = venv_python(repo_root / VENV_DIR, os_name).parent
    return scripts / (f"{name}.exe" if os_name == "nt" else name)


def configured(environ: Mapping[str, str], repo_root: Path) -> Path:
    """The generator to start: the variable's file, else ``.venv``'s, else a refusal."""
    value = environ.get(VARIABLE)
    if value:
        path = Path(value)
        if not path.is_absolute():
            raise GeneratorNotConfigured(
                "relative",
                f"{VARIABLE} names a relative path; it must be the absolute path of the generator's executable.",
            )
        if path.suffix.lower() in BATCH_SUFFIXES:
            raise GeneratorNotConfigured(
                "batch-file",
                f"{VARIABLE} names a batch file ({path.suffix}): Python starts one through cmd.exe, which "
                "re-parses the arguments; name the generator's executable instead.",
            )
        if not path.is_file():
            raise GeneratorNotConfigured("missing", f"{VARIABLE} names no file: {path}")
        return path
    installed = installed_executable(repo_root)
    if installed.is_file():
        return installed
    raise GeneratorNotConfigured(
        "unset",
        f"no specimen generator is configured: {VARIABLE} is unset and {VENV_DIR.as_posix()} holds no "
        f"{installed.name}. Run `python scripts/dev.py install` (it installs packages/specimens/ into "
        f"{VENV_DIR.as_posix()}), or set {VARIABLE} to the generator's absolute path.",
    )


def generator_command(command: list[str] | None, environ: Mapping[str, str] | None, repo_root: Path) -> list[str]:
    """*command* when given, else :func:`configured`'s generator in *environ* (``None``: ``os.environ``)."""
    if command is not None:
        return command
    return [os.fspath(configured(os.environ if environ is None else environ, repo_root))]


def generator_interpreter(executable: Path) -> tuple[int, int] | None:
    """``major, minor`` of the Python the console script *executable* runs on, else ``None``.

    Read from its virtual environment's ``pyvenv.cfg`` (``version`` or ``version_info``); nothing is
    started to ask.
    """
    config = Path(os.path.realpath(executable)).parent.parent / "pyvenv.cfg"
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    recorded: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            recorded[key.strip()] = value.strip()
    raw = recorded.get("version") or recorded.get("version_info") or ""
    parts = raw.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return int(parts[0]), int(parts[1])


def interpreter_finding(executable: Path) -> str | None:
    """The sentence a regeneration by *executable* is refused with, else ``None`` (CPython :data:`GENERATOR_PYTHON`)."""
    pinned = ".".join(map(str, GENERATOR_PYTHON))
    found = generator_interpreter(executable)
    if found == GENERATOR_PYTHON:
        return None
    runs_on = "an interpreter no pyvenv.cfg beside it states" if found is None else f"Python {found[0]}.{found[1]}"
    return (
        f"the specimen generator {executable.name} runs on {runs_on}, and the specimens are pinned on "
        f"Python {pinned} alone (the bootstrap interpreter's minor, .prototools): another interpreter's "
        f"numpy and HiGHS wheels are unmeasured; run `python scripts/dev.py install` with Python {pinned}, "
        f"or point {VARIABLE} at a generator in a Python {pinned} environment"
    )


def source_digest(repo_root: Path) -> str:
    """SHA-256 over the generator's source files on disk (:data:`SOURCE_DIR`), ``__pycache__`` and ``*.pyc`` left out.

    Each path and its bytes are length-framed, so no two trees share a digest by a shifted boundary.
    On disk rather than tracked, so an untracked module counts. ``FileNotFoundError`` when absent.
    """
    root = repo_root / SOURCE_DIR
    if not root.is_dir():
        raise FileNotFoundError(f"{SOURCE_DIR.as_posix()} is absent")
    files = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix != ".pyc" and "__pycache__" not in path.relative_to(root).parts
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    sha = hashlib.sha256()
    for path in files:
        name = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        sha.update(len(name).to_bytes(8, "big"))
        sha.update(name)
        sha.update(len(content).to_bytes(8, "big"))
        sha.update(content)
    return sha.hexdigest()


# -- the manifest and the digest --------------------------------------------------------------------


def manifest_text(name: str) -> str:
    """SWCraft's ``swcraft.yaml`` for the specimen *name*: no ``toolchain_version``, never edited."""
    return _MANIFEST_TEMPLATE.format(name=name)


def digest(model_root: Path) -> str:
    """SHA-256 over every file under *model_root*, by POSIX relative path then bytes."""
    sha = hashlib.sha256()
    files = sorted(
        (path for path in model_root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(model_root).as_posix(),
    )
    for path in files:
        sha.update(path.relative_to(model_root).as_posix().encode("utf-8"))
        sha.update(b"\0")
        sha.update(path.read_bytes())
        sha.update(b"\0")
    return sha.hexdigest()


# -- the run ----------------------------------------------------------------------------------------


def specimen_name(passport: Path) -> str:
    """The specimen's name, from its passport file's: ``<name>.passport.json``."""
    return passport.name.removesuffix(PASSPORT_SUFFIX)


def run(
    command: list[str],
    passport: Path,
    out: Path,
    *,
    timeout: float = TIMEOUT_SECONDS,
    keep_stderr: bool = False,
) -> Outcome:
    """Run ``[*command, "specimen", passport, "--out", out]`` and say what it answered."""
    passport = Path(os.path.abspath(passport))
    out = Path(os.path.abspath(out))
    argv = [*command, "specimen", os.fspath(passport), "--out", os.fspath(out)]
    with tempfile.TemporaryDirectory(prefix="specimen-run-") as cwd:
        try:
            completed = subprocess.run(argv, capture_output=True, cwd=cwd, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return GeneratorDefect("timeout")
        except OSError as error:
            return GeneratorDefect(type(error).__name__)
    if completed.returncode == 2:
        text = completed.stderr.decode("utf-8", errors="replace")
        lines = sum(1 for line in text.splitlines() if line.strip())
        return Rejected(lines, text if keep_stderr else None)
    if completed.returncode != 0:
        return GeneratorDefect(f"exit {completed.returncode}")
    model = out / "model"
    if not model.is_dir():
        return GeneratorDefect("exit 0 without model/")
    for entry in sorted(out.iterdir(), key=lambda path: path.as_posix()):
        if entry.name == "model":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    (out / "swcraft.yaml").write_text(manifest_text(specimen_name(passport)), encoding="utf-8", newline="\n")
    return Built(out)


def run_document(command: list[str], document: Mapping[str, Any], out: Path, name: str) -> Outcome:
    """:func:`run` on the passport *document*, written to a temporary ``<name>.passport.json``."""
    with tempfile.TemporaryDirectory(prefix="specimen-passport-") as scratch:
        passport = Path(scratch) / f"{name}{PASSPORT_SUFFIX}"
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
        passport.write_text(text, encoding="utf-8", newline="\n")
        return run(command, passport, out)


# -- the specimen cache -----------------------------------------------------------------------------

#: The committed passports and the pin file's directory, relative to the repository root.
SPECIMENS_DIR = PIN_FILE.parent

#: The specimen cache, relative to the repository root (gitignored, never in ``dist/``).
SPECIMEN_CACHE = Path(".cache") / "specimens"

#: The longest path inside a specimen, relative to its root (``model/...``), the gate holds.
PATH_BOUND = 115

#: The longest absolute path the cache may produce: ``MAX_PATH`` (260) less the terminating NUL.
ABSOLUTE_BUDGET = 259

#: The cache key's parts: the source digest's hex characters, then the passport's.
SOURCE_KEY_LENGTH = 16
KEY_LENGTH = 16

#: A lock older than this is a dead process's: the generation's ceiling plus five minutes, so a live
#: holder is never counted dead. A waiter gives up a minute later, so it outlasts a dead holder's lock.
LOCK_STALE_SECONDS = TIMEOUT_SECONDS + 300.0
LOCK_WAIT_SECONDS = LOCK_STALE_SECONDS + 60.0
LOCK_POLL_SECONDS = 0.2

#: A breaker older than this is a killed waiter's: the step it guards takes milliseconds.
BREAKER_STALE_SECONDS = 30.0

#: ``.t*`` and ``.r*`` siblings older than this are swept by the next repair, under the lock.
SWEEP_AGE_SECONDS = 3600.0

#: Retries of a rename, read or unlink refused with ``PermissionError`` (on Windows a scanner's handle
#: or another process reading the same file).
RENAME_ATTEMPTS = 5
RENAME_PAUSE_SECONDS = 0.2

#: Every specimen this process has taken, held for the rest of the process: ``(cache root, name)`` -> key.
_TAKEN: dict[tuple[str, str], Path] = {}

#: Every specimen whose regeneration this process saw fail (a mismatch, a rejection, a defect), by the
#: same key: raised again by a later take without generating again.
_REFUSED: dict[tuple[str, str], Exception] = {}

#: The source digest's cache key per repository root, taken once per process (:func:`forget_taken` clears it).
_SOURCE_KEYS: dict[str, str] = {}


class SpecimenUnavailable(Exception):
    """A specimen cannot be taken; ``reason`` names why, the message says it in one sentence.

    ``pin-file``, ``not-pinned``, ``passport-changed``, ``generator-source``, ``path-budget``,
    ``interpreter``, ``rejected``, ``defect`` or ``lock-timeout``.
    """

    def __init__(self, reason: str, sentence: str) -> None:
        super().__init__(sentence)
        self.reason = reason


#: The generator source (:func:`_generator_source`) that is ``.venv``'s install of ``packages/specimens/``.
INSTALLED_SOURCE = "the .venv install"


class SpecimenDigestMismatch(Exception):
    """A freshly generated specimen whose ``model/`` is not the pinned one -- never re-pinned by a test.

    From ``.venv``'s generator the output moved (re-pin an intended move in the same commit); a
    generator configured otherwise is not known to be this checkout's, so the run is to be repeated
    with ``.venv``'s first.
    """

    def __init__(self, name: str, *, expected: str, actual: str, source_key: str, source: str) -> None:
        finding = f"its model digest is {actual}, and the pin file pins {expected}"
        if source == INSTALLED_SOURCE:
            opening = (
                f"The specimen generator ({source}, source {source_key}) generated the specimen {name}, and "
                f"{finding}: its output moved. When the move is intended, re-pin it with "
                f"`scripts/specimens.py pin {name}` in the same commit; otherwise it is a finding of the "
                "generator or of SWCraft."
            )
        else:
            opening = (
                f"The specimen generator configured by {source} generated the specimen {name}, and {finding}; it "
                f"is not known to be {INSTALLED_SOURCE} of this checkout's source {source_key}, so run the "
                "specimen again with that install before reading this as a move of the generator's output."
            )
        super().__init__(opening + " Never answered by a re-pin of the specimen inside a test.")
        self.expected = expected
        self.actual = actual


def file_sha256(path: Path) -> str:
    """SHA-256 of *path*'s bytes, read in blocks."""
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def passport_path(repo_root: Path, name: str) -> Path:
    """The committed source passport of the specimen *name*."""
    return repo_root / SPECIMENS_DIR / f"{name}{PASSPORT_SUFFIX}"


def cache_root_budget() -> int:
    """The longest cache root the absolute-path budget leaves (separators counted)."""
    return ABSOLUTE_BUDGET - (1 + SOURCE_KEY_LENGTH + 1 + KEY_LENGTH + 1 + PATH_BOUND)


def forget_taken() -> None:
    """Forget every specimen taken or refused and the source keys, so the next ``take`` digests again."""
    _TAKEN.clear()
    _REFUSED.clear()
    _SOURCE_KEYS.clear()


def source_key(repo_root: Path) -> str:
    """The cache key's first part: :func:`source_digest`'s first :data:`SOURCE_KEY_LENGTH` hex, once per process.

    :class:`SpecimenUnavailable` (``generator-source``) when the source is absent.
    """
    memo = Path(os.path.abspath(repo_root)).as_posix()
    if memo not in _SOURCE_KEYS:
        try:
            _SOURCE_KEYS[memo] = source_digest(repo_root)[:SOURCE_KEY_LENGTH]
        except FileNotFoundError:
            raise SpecimenUnavailable(
                "generator-source",
                f"the specimen generator's source {SOURCE_DIR.as_posix()} is absent, so no specimen cache key "
                "can be named",
            ) from None
    return _SOURCE_KEYS[memo]


def _generator_source(command: list[str] | None, environ: Mapping[str, str]) -> str:
    if command is not None:
        return "the command given"
    return f"the variable {VARIABLE}" if environ.get(VARIABLE) else INSTALLED_SOURCE


def _is_valid(key: Path, name: str, model_sha256: str, *, repair: bool = False) -> bool:
    """*key* holds the pinned ``model/`` and SWCraft's manifest (compared as bytes).

    With *repair* (the caller holds the key's lock) a wrong manifest is rewritten through a sibling and
    a replace, so a reader never meets a half-written one. A tree that changes under the reading is an
    ``OSError`` read as "invalid", never raised: the caller then checks again under the lock.
    """
    try:
        return _holds_the_pin(key, name, model_sha256, repair=repair)
    except OSError:
        return False


def _holds_the_pin(key: Path, name: str, model_sha256: str, *, repair: bool) -> bool:
    """:func:`_is_valid`'s reading; every ``OSError`` is left to it."""
    model = key / "model"
    if not model.is_dir() or digest(model) != model_sha256:
        return False
    manifest = key / "swcraft.yaml"
    expected = manifest_text(name)
    if manifest.is_file() and manifest.read_bytes() == expected.encode("utf-8"):
        return True
    if not repair:
        return False
    written = key / f".swcraft.yaml.{secrets.token_hex(4)}"
    try:
        written.write_text(expected, encoding="utf-8", newline="\n")
        os.replace(written, manifest)
    except OSError:
        written.unlink(missing_ok=True)
        raise
    return True


def _lock_age(lock: Path, observed: bytes) -> float:
    """Seconds since *lock* (whose content was *observed*) was taken: its recorded time, else its modification time."""
    try:
        recorded = float(observed.decode("utf-8").split()[1])
    except (UnicodeDecodeError, ValueError, IndexError):
        try:
            recorded = lock.stat().st_mtime
        except OSError:
            return 0.0
    return time.time() - recorded


def _breaker_path(lock: Path, observed: bytes) -> Path:
    """The breaker of the one lock instance whose content was *observed*: ``<key>.lock.b<16 hex>``."""
    return lock.with_name(f"{lock.name}.b{hashlib.sha256(observed).hexdigest()[:16]}")


def _record_line() -> str:
    """A lock's or a breaker's record: the pid, the time and a random token, one line."""
    return f"{os.getpid()} {time.time()} {secrets.token_hex(4)}\n"


def _read_record(path: Path) -> bytes | None:
    """*path*'s bytes, ``None`` when it is gone; a ``PermissionError`` is retried, then raised."""
    for attempt in range(RENAME_ATTEMPTS):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except PermissionError:
            if attempt == RENAME_ATTEMPTS - 1:
                raise
            time.sleep(RENAME_PAUSE_SECONDS)
    raise AssertionError("unreachable")  # pragma: no cover


def _remove_if_holds(path: Path, content: bytes) -> None:
    """Remove *path* only while it still holds *content*; nothing when it is gone or holds another.

    A ``PermissionError`` is retried, the content compared again before each unlink; still refused, it
    is raised and the file is left to be broken as stale at its age.
    """
    for attempt in range(RENAME_ATTEMPTS):
        try:
            if path.read_bytes() != content:
                return
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError as error:
            if attempt == RENAME_ATTEMPTS - 1:
                raise PermissionError(
                    f"{path.name} could not be removed: {RENAME_ATTEMPTS} attempts were refused while another "
                    "process held it; it is broken as stale at its age"
                ) from error
            time.sleep(RENAME_PAUSE_SECONDS)


def _release(path: Path, line: str) -> None:
    """Remove *path* only while it still holds *line*: a file another process broke and took is left to it."""
    _remove_if_holds(path, line.encode("utf-8"))


def _break_stale(lock: Path, observed: bytes) -> bool:
    """Remove *lock* if it still holds *observed* (a stale lock's content); whether that lock is gone.

    Only one waiter wins: it publishes the breaker of that lock instance (:func:`_breaker_path`) and
    alone compares and removes the lock, so a replacement lock is never removed by a waiter that judged
    its predecessor stale. A breaker older than :data:`BREAKER_STALE_SECONDS` is broken by the same
    rule one level down, so a killed waiter blocks the key for that grace period only. ``False`` when
    another waiter holds a younger breaker or a sharing violation persists: the caller looks again.

    Not reached: a holder alive past :data:`LOCK_STALE_SECONDS` that releases its lock between the
    comparison and the removal, and the same window for a breaker alive past its grace period.
    """
    breaker = _breaker_path(lock, observed)
    line = _record_line()
    while not _publish_lock(breaker, line):
        try:
            held = _read_record(breaker)
        except PermissionError:
            return False
        if held is None:
            continue
        if _lock_age(breaker, held) > BREAKER_STALE_SECONDS and _break_stale(breaker, held):
            continue
        return False
    try:
        try:
            _remove_if_holds(lock, observed)
        except PermissionError:
            return False
        return True
    finally:
        try:
            _release(breaker, line)
        except PermissionError:
            # Left to age: a breaker past BREAKER_STALE_SECONDS is broken by the next waiter.
            pass


def _publish_lock(lock: Path, line: str) -> bool:
    """Create *lock* holding *line*, or ``False`` when it exists: the record is in it the instant it exists.

    The line goes to a sibling first, published by ``os.link`` (atomic, refusing an existing name on
    POSIX and Windows), so no lock is ever seen empty, and the random token keeps every instance's
    content unique. A filesystem without hard links raises its ``OSError`` rather than lock unsafely.
    """
    sibling = lock.with_name(f".l{secrets.token_hex(4)}")
    try:
        with open(sibling, "x", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
        try:
            os.link(sibling, lock)
        except FileExistsError:
            return False
        return True
    finally:
        # A record still refused is left to the sweep rather than failing a lock already published.
        for attempt in range(RENAME_ATTEMPTS):
            try:
                sibling.unlink()
                break
            except FileNotFoundError:
                break
            except PermissionError:
                if attempt < RENAME_ATTEMPTS - 1:
                    time.sleep(RENAME_PAUSE_SECONDS)


class _KeyLock:
    """``<key>.lock``, published by :func:`_publish_lock` with the pid, the time and a random token.

    Released only while it still holds this holder's line: a lock another process broke and took is
    left to it. A release still refused raises ``PermissionError`` and leaves the lock to age.
    """

    def __init__(self, lock: Path) -> None:
        self.lock = lock
        self.line = ""

    def __enter__(self) -> _KeyLock:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            line = _record_line()
            if not _publish_lock(self.lock, line):
                try:
                    observed = _read_record(self.lock)
                except PermissionError:
                    pass  # busy (a sharing violation): wait and look again
                else:
                    if observed is None:
                        continue
                    if _lock_age(self.lock, observed) > LOCK_STALE_SECONDS and _break_stale(self.lock, observed):
                        continue
                if time.monotonic() > deadline:
                    raise SpecimenUnavailable(
                        "lock-timeout",
                        f"the specimen cache key {self.lock.stem} stayed locked for {LOCK_WAIT_SECONDS:.0f} s",
                    ) from None
                time.sleep(LOCK_POLL_SECONDS)
                continue
            self.line = line
            return self

    def __exit__(self, *exc_info: object) -> None:
        _release(self.lock, self.line)


def _sweep(source_dir: Path) -> None:
    """Remove a killed process's ``.t*``/``.r*`` trees, ``.l*`` records and breakers older than the sweep age."""
    now = time.time()
    for entry in sorted(source_dir.iterdir(), key=lambda path: path.as_posix()):
        directory = entry.name.startswith((".t", ".r")) and entry.is_dir()
        record = entry.name.startswith(".l") and entry.is_file()
        if ".lock.b" in entry.name and entry.is_file():
            try:
                held = entry.read_bytes()
            except OSError:
                continue
            if _lock_age(entry, held) > SWEEP_AGE_SECONDS:
                _break_stale(entry, held)
            continue
        if directory or record:
            try:
                old = now - entry.stat().st_mtime > SWEEP_AGE_SECONDS
            except OSError:
                continue
            if not old:
                continue
            if directory:
                shutil.rmtree(entry, ignore_errors=True)
            else:
                try:
                    entry.unlink()
                except OSError:
                    pass


def _rename(source: Path, target: Path) -> None:
    """``os.rename``, retried on ``PermissionError`` (a Windows scanner's handle), then raised."""
    for attempt in range(RENAME_ATTEMPTS):
        try:
            os.rename(source, target)
            return
        except PermissionError:
            if attempt == RENAME_ATTEMPTS - 1:
                raise
            time.sleep(RENAME_PAUSE_SECONDS)


def take(
    name: str,
    *,
    repo_root: Path,
    cache_root: Path | None = None,
    command: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """The cache directory of the specimen *name*: ``swcraft.yaml`` and the pinned ``model/``.

    A caller copies the directory and never writes it. *environ* (``None``: ``os.environ``) is where a
    regeneration looks for :data:`VARIABLE`.
    """
    repo_root = Path(repo_root)
    try:
        pins = load_pins(repo_root)
    except ValueError as error:
        raise SpecimenUnavailable("pin-file", f"{error}, so no specimen can be taken") from None
    entry = pins.get("specimens", {}).get(name)
    if entry is None:
        raise SpecimenUnavailable("not-pinned", f"the pin file pins no specimen named {name}")
    passport = passport_path(repo_root, name)
    passport_sha256 = file_sha256(passport) if passport.is_file() else None
    if passport_sha256 is None or passport_sha256 != entry["passport_sha256"]:
        raise SpecimenUnavailable(
            "passport-changed",
            f"the passport of the specimen {name} is not the one pinned; run `scripts/specimens.py pin {name}`",
        )
    root = Path(os.path.abspath(cache_root if cache_root is not None else repo_root / SPECIMEN_CACHE))
    if len(os.fspath(root)) > cache_root_budget():
        raise SpecimenUnavailable(
            "path-budget",
            f"the specimen cache's root is {len(os.fspath(root))} characters long, and the absolute-path "
            f"budget of {ABSOLUTE_BUDGET} leaves it {cache_root_budget()}: use a checkout with a shorter path",
        )
    memo = (root.as_posix(), name)
    if memo in _TAKEN:
        return _TAKEN[memo]
    if memo in _REFUSED:
        raise _REFUSED[memo]
    source_dir = root / source_key(repo_root)
    key = source_dir / passport_sha256[:KEY_LENGTH]
    if not _is_valid(key, name, entry["model_sha256"]):
        source_dir.mkdir(parents=True, exist_ok=True)
        with _KeyLock(source_dir / f"{key.name}.lock"):
            if not _is_valid(key, name, entry["model_sha256"], repair=True):
                try:
                    _regenerate(name, key, passport, entry["model_sha256"], repo_root, command, environ)
                except SpecimenDigestMismatch as refused:
                    _REFUSED[memo] = refused
                    raise
                except SpecimenUnavailable as refused:
                    if refused.reason in ("rejected", "defect"):
                        _REFUSED[memo] = refused
                    raise
    _TAKEN[memo] = key
    return key


def _regenerate(
    name: str,
    key: Path,
    passport: Path,
    model_sha256: str,
    repo_root: Path,
    command: list[str] | None,
    environ: Mapping[str, str] | None,
) -> None:
    """Generate *name* into a fresh sibling, hold it to its digest, publish it as *key* (under the lock).

    The configured generator (no *command*) is refused first on another interpreter than :data:`GENERATOR_PYTHON`.
    """
    source_dir = key.parent
    source_key_name = source_dir.name
    _sweep(source_dir)
    environ = os.environ if environ is None else environ
    source = _generator_source(command, environ)
    argv = generator_command(command, environ, repo_root)
    if command is None:
        refusal = interpreter_finding(Path(argv[0]))
        if refusal is not None:
            raise SpecimenUnavailable("interpreter", f"{refusal}, so the specimen {name} is not regenerated")
    fresh = source_dir / f".t{secrets.token_hex(4)}"
    outcome = run(argv, passport, fresh)
    if not isinstance(outcome, Built):
        shutil.rmtree(fresh, ignore_errors=True)
        if isinstance(outcome, Rejected):
            raise SpecimenUnavailable(
                "rejected",
                f"the specimen generator (source {source_key_name}, {source}) rejected the specimen {name}'s "
                f"passport ({outcome.lines} stderr lines; `scripts/specimens.py pin {name}` prints them)",
            )
        raise SpecimenUnavailable(
            "defect",
            f"the specimen generator (source {source_key_name}, {source}) failed on the specimen {name}: "
            f"{outcome.cause}",
        )
    actual = digest(fresh / "model")
    if actual != model_sha256:
        shutil.rmtree(fresh, ignore_errors=True)
        raise SpecimenDigestMismatch(
            name, expected=model_sha256, actual=actual, source_key=source_key_name, source=source
        )
    retired = None
    if key.exists():
        retired = source_dir / f".r{secrets.token_hex(4)}"
        try:
            _rename(key, retired)
        except FileNotFoundError:
            retired = None
    try:
        _rename(fresh, key)
    except OSError as error:
        shutil.rmtree(fresh, ignore_errors=True)
        # Another process published first (a lock broken as stale): its tree is the one taken.
        if not isinstance(error, PermissionError) and _is_valid(key, name, model_sha256, repair=True):
            if retired is not None:
                shutil.rmtree(retired, ignore_errors=True)
            return
        # Nothing was published: the old key goes back, so a failed repair loses nothing.
        if retired is not None and not key.exists():
            try:
                _rename(retired, key)
            except OSError:
                pass
        raise
    if retired is not None:
        shutil.rmtree(retired, ignore_errors=True)
