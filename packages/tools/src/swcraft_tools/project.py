"""SwcraftProject — project manifest loader.

Provides two entry points:

- ``load_project(start_dir)`` — internal discovery: walks up dirs looking for
  swcraft.yaml, falls back to the SWCRAFT_MANIFEST environment variable.
  Returns None when no manifest is found.

- ``require_project(path)`` — user-facing (CLI / MCP): loads from an explicit
  path only; raises FileNotFoundError if the path is empty or the file is
  missing.  No walk-up discovery.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, field_validator

from swcraft_tools.arxml_version import DEFAULT_ARXML_VERSION, ArxmlVersion
from swcraft_tools.yaml_load import load_yaml_document

_MANIFEST_FILENAME = "swcraft.yaml"

#: PEP 440's version grammar, transcribed from the specification's appendix and anchored to the whole
#: string. It decides whether :attr:`SwcraftProject.toolchain_version` is a version at all: a
#: specifier (``>=0.7``, ``==0.7.*``) fails it, because a range would pin nothing. Transcribed
#: because ``packaging`` is not a dependency of ``swcraft-tools``.
#:
#: What it does not reach: the permissive spellings PEP 440 admits (a leading ``v``, surrounding
#: whitespace, ``ALPHA``/``c``/``rev``, the ``-``/``_``/``.`` separators) are accepted and
#: normalized by :func:`_version_key`, so a pin need not be canonical, only name one release.
_PEP440_VERSION = re.compile(
    r"""
    ^\s*v?
    (?:(?P<epoch>[0-9]+)!)?                               # epoch
    (?P<release>[0-9]+(?:\.[0-9]+)*)                      # release segment
    (?P<pre>                                              # pre-release
        [-_.]?(?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)[-_.]?(?P<pre_n>[0-9]+)?
    )?
    (?P<post>                                             # post release
        (?:-(?P<post_n1>[0-9]+))
        |
        (?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?)
    )?
    (?P<dev>                                              # development release
        [-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]+)?
    )?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?        # local version
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

#: The pre-release spellings PEP 440 normalizes onto one letter.
_PRE_RELEASE_SPELLINGS = {
    "alpha": "a",
    "a": "a",
    "beta": "b",
    "b": "b",
    "c": "rc",
    "pre": "rc",
    "preview": "rc",
    "rc": "rc",
}


def _version_key(value: str) -> tuple | None:
    """*value* as a comparable key, or ``None`` when it is not a PEP 440 version.

    The key answers equality only: two spellings of one release give one key (``0.7`` and
    ``0.7.0``, ``0.7.0c1`` and ``0.7.0rc1``). It does not order versions; no caller asks which is
    newer. Trailing zero release segments are dropped, as PEP 440 compares releases of unequal length.
    """
    match = _PEP440_VERSION.match(value)
    if match is None:
        return None
    release = tuple(int(part) for part in match["release"].split("."))
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    pre = (_PRE_RELEASE_SPELLINGS[match["pre_l"].lower()], int(match["pre_n"] or 0)) if match["pre"] else None
    post_number = match["post_n1"] or match["post_n2"]
    post = int(post_number or 0) if match["post"] else None
    dev = int(match["dev_n"] or 0) if match["dev"] else None
    local = tuple(re.split(r"[-_.]", match["local"].lower())) if match["local"] else ()
    return (int(match["epoch"] or 0), release, pre, post, dev, local)


def _is_exact_version(value: str) -> bool:
    """Whether *value* is one PEP 440 version rather than a range or a word."""
    return _version_key(value) is not None


def toolchain_versions_agree(pinned: str, running: str) -> bool:
    """Whether the two strings name the same SWCraft release.

    ``False`` when either is not a version at all.
    """
    left, right = _version_key(pinned), _version_key(running)
    return left is not None and left == right


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_WINDOWS_EXTENDED_PREFIX = "\\\\?\\"
_WINDOWS_EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"


def _resolved(path: Path) -> str:
    """``str(path.resolve())`` without Windows' extended-length prefix.

    On Windows ``Path.resolve()`` can leave the ``\\\\?\\`` prefix on a path that does not exist yet
    (CPython strips it only when its second probe fails the same way, and a generator running in
    parallel can create ``dist/`` between the probes). The bare spelling names the same file, so the
    prefix is dropped: ``\\\\?\\C:\\x`` is ``C:\\x`` and ``\\\\?\\UNC\\host\\share`` is
    ``\\\\host\\share``. A POSIX spelling is returned unchanged. What this does not reach: a path
    written with the prefix in the manifest keeps it, and a path that needs the prefix to be opened
    (longer than ``MAX_PATH``) is not shortened.
    """
    spelled = str(path.resolve())
    if spelled.startswith(_WINDOWS_EXTENDED_UNC_PREFIX):
        return "\\\\" + spelled[len(_WINDOWS_EXTENDED_UNC_PREFIX) :]
    if spelled.startswith(_WINDOWS_EXTENDED_PREFIX):
        return spelled[len(_WINDOWS_EXTENDED_PREFIX) :]
    return spelled


class OutputPaths(BaseModel):
    """Where the ARXML and needs.json generators write, relative to the project directory."""

    arxml: str = Field(
        default="dist/model.arxml",
        description="Path of the generated AUTOSAR ARXML file, relative to the project directory.",
    )
    needs: str = Field(
        default="dist/needs.json",
        description=(
            "Path of the Sphinx-Needs `needs.json` file `swcraft generate needs` writes, relative to the "
            "project directory."
        ),
    )


class SwcraftProject(BaseModel):
    """The parsed ``swcraft.yaml``.

    ``schema_version`` is this manifest format's version and ``arxml_version`` the AUTOSAR release
    the generated ARXML targets (defaulted, so a manifest without it generates 4.2.2 output).

    The ``Field(description=...)`` values and the ``arxml_version`` enum are JSON-Schema metadata
    for the editor manifest schema only; no default, validator or serialization depends on them.
    """

    name: str = Field(description="The project's name; a label for reports and diagrams, not an AUTOSAR identifier.")
    schema_version: str = Field(
        "1.0",
        description="Version of this manifest format. Unrelated to `arxml_version`.",
    )
    model_root: str = Field(
        "model",
        description=(
            "Directory holding the YAML model layers (`swc/`, `interfaces/`, ...), relative to the "
            "project directory. No generated path may be inside it."
        ),
    )
    dist_root: str = Field(
        "dist",
        description="Directory for generated build outputs, relative to the project directory; cleaned by every build.",
    )
    views_root: str = Field(
        "views",
        description=(
            "Directory the diagram generator writes its SVG views and HTML pages into, relative to the project "
            "directory; cleaned by every build."
        ),
    )
    outputs: OutputPaths = Field(
        OutputPaths(),
        description="Where the ARXML and needs.json generators write their files; the diagrams go to `views_root`.",
    )
    arxml_version: str = Field(
        DEFAULT_ARXML_VERSION.value,
        description=(
            "AUTOSAR release the generated ARXML targets. "
            "Overridable for one run with `--arxml-version`, which is never written back."
        ),
        # The editor's `enum`, taken from `ArxmlVersion` so the schema and the validator agree.
        json_schema_extra={"enum": [version.value for version in ArxmlVersion]},
    )
    toolchain_version: str | None = Field(
        None,
        description=(
            "The SWCraft release this project is built with, as one exact `major.minor.patch` version "
            "and never a range. Every `swcraft` subcommand that reads a manifest compares it with the toolchain it "
            "is running as: one that writes or gates refuses a mismatch, the read-only ones report it "
            "and answer anyway. `swcraft-mcp`, `swcraft-agent` and `python -m swcraft_mcp.server` read "
            "the same manifest and compare nothing. Leave it out and nothing is compared."
        ),
    )
    source: Literal["manifest", "env"] = Field(
        "manifest",
        description="Runtime provenance marker set by the loader; never written in a manifest file.",
    )

    # Set after construction; not part of the serialized model.
    _project_dir: Path = PrivateAttr(default=Path("."))

    @field_validator("arxml_version")
    @classmethod
    def _check_arxml_version(cls, value: str) -> str:
        """Reject an unsupported version at *load* time, not at generation time.

        The message is :meth:`ArxmlVersion.from_string`'s, so the manifest and ``--arxml-version``
        reject the same values in the same words. Stored as written; :attr:`resolved_arxml_version`
        is the parsed form.
        """
        ArxmlVersion.from_string(value)
        return value

    @field_validator("toolchain_version")
    @classmethod
    def _check_toolchain_version(cls, value: str | None) -> str | None:
        """Reject a pin that is not a version at *load* time, naming the field.

        Refused here because only this layer knows which key the string came from. A specifier
        such as ``">=0.7"`` reads like a pin and pins nothing. Stored as written; the comparison
        normalizes.
        """
        if value is None:
            return None
        if not _is_exact_version(value):
            raise ValueError(
                f"toolchain_version {value!r} is not a version. It pins one SWCraft release — "
                "'0.7.0' — and never a range: a specifier such as '>=0.7' or '==0.7.*' cannot be "
                "compared with the running toolchain, so it is refused here rather than ignored."
            )
        return value

    # ------------------------------------------------------------------
    # Resolved (absolute) path properties
    # ------------------------------------------------------------------

    @property
    def resolved_project_root(self) -> str:
        return _resolved(self._project_dir)

    @property
    def resolved_model_root(self) -> str:
        return _resolved(self._project_dir / self.model_root)

    @property
    def resolved_dist_root(self) -> str:
        return _resolved(self._project_dir / self.dist_root)

    @property
    def resolved_views_root(self) -> str:
        return _resolved(self._project_dir / self.views_root)

    @property
    def resolved_arxml_path(self) -> str:
        return _resolved(self._project_dir / self.outputs.arxml)

    @property
    def resolved_needs_path(self) -> str:
        return _resolved(self._project_dir / self.outputs.needs)

    # ------------------------------------------------------------------
    # Resolved non-path settings
    # ------------------------------------------------------------------

    @property
    def resolved_arxml_version(self) -> ArxmlVersion:
        """The declared AUTOSAR version as an :class:`ArxmlVersion` member.

        Always parseable: the field validator refused anything else at load time.
        """
        return ArxmlVersion.from_string(self.arxml_version)


#: The manifest fields the loader sets itself and a manifest file never carries; a ``source:`` key
#: written in a manifest is an undeclared key, and :func:`_parse_manifest` overwrites it.
MANIFEST_RUNTIME_FIELDS: frozenset[str] = frozenset({"source"})


def manifest_keys() -> dict[str, frozenset[str]]:
    """The keys a manifest file declares: the set the editor's manifest schema is rendered from.

    Under ``""`` the top-level keys, :class:`SwcraftProject`'s field names without
    :data:`MANIFEST_RUNTIME_FIELDS`; under each field whose type is itself a model (``outputs``),
    that model's field names.
    """
    fields = SwcraftProject.model_fields
    keys: dict[str, frozenset[str]] = {
        "": frozenset((field.alias or name) for name, field in fields.items() if name not in MANIFEST_RUNTIME_FIELDS)
    }
    for name, field in fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            nested = annotation.model_fields
            keys[field.alias or name] = frozenset(
                (member.alias or member_name) for member_name, member in nested.items()
            )
    return keys


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _contains(container: str, path: str) -> bool:
    """Is *path* *container* itself, or something inside it? A root (``/``, ``C:\\``) contains everything below it."""
    return path == container or path.startswith(container if container.endswith(os.sep) else container + os.sep)


def _overlaps(first: str, second: str) -> bool:
    """Whether *first* and *second* name one place, or one lies inside the other, on **any** filesystem.

    Any filesystem is one this manifest may be opened on.

    Compared as strings, not probed on the filesystem: ``dist_root``, ``views_root`` and the
    ``outputs`` need not exist yet, and a probe would answer "different" for every freshly scaffolded
    project. Case-insensitive on every filesystem: a committed manifest travels to APFS and NTFS,
    where ``model`` and ``Model/dist`` are one directory, so one portable, fail-closed answer is given
    everywhere. Only the overlap rule casefolds; the escape-the-project-root rule keeps exact
    comparison, because casefolding there would widen what is accepted.
    """
    lowered_first, lowered_second = first.casefold(), second.casefold()
    return _contains(lowered_first, lowered_second) or _contains(lowered_second, lowered_first)


def is_absolute_anywhere(written: str) -> bool:
    """Whether *written* is an absolute path **on either platform's rules**.

    `/var/tmp/dist` is absolute only to POSIX and `C:/dist` only to Windows; both are refused on both
    platforms, so the manifest means the same thing on every machine that reads it.

    What it does **not** reach:

    * A Windows *drive-relative* path on the project's own drive (`C:dist` under a `C:` project) is
      neither pure implementation's `is_absolute()` and joins to inside the project, so nothing
      refuses it, though it is machine-dependent. On a *different* drive (`D:dist`) the join does not
      resolve inside the project and `_validate_project_paths` rule 1 refuses it as an escape.
    * A bare-rooted `\\dist` is called relative here too; on Windows it joins to the drive root and
      rule 1 refuses it, and on macOS and Linux it is an ordinary filename inside the project.

    Both join results were measured with `PureWindowsPath`, not on a Windows host: what a real host
    does with a drive-relative path depends on that drive's current directory.
    """
    return PurePosixPath(written).is_absolute() or PureWindowsPath(written).is_absolute()


def _validate_project_paths(project: SwcraftProject, manifest_path: Path) -> None:
    """Raise ValueError if the manifest's paths are not a safe layout.

    Rules 1, 2 and 4 judge the *resolved* paths (through :func:`_resolved`, so a symlinked
    ``dist_root`` into the model is refused); rule 3 judges what the manifest **wrote**:

    1. **Nothing escapes the project root** (e.g. ``model_root: ../../etc``).
    2. **No generated path overlaps ``model_root``**, in either direction, judged
       case-insensitively (:func:`_overlaps`): output inside the model would join a scoped
       session's fingerprint, and a model inside ``dist_root``/``views_root`` would be cleaned away.
    3. **``dist_root`` and ``views_root`` are written relative**: they are the two directories
       ``swcraft clean-outputs`` removes. The other three fields are not covered.
    4. **``outputs.needs`` names no file the project keeps under another name**
       (:func:`_needs_collision`): ``outputs.arxml``, its ``_old`` sibling, the audit log, the build
       report, the manifest, a name ``generate diagrams`` writes in ``views_root``
       (``report.txt``, ``.svg``, ``.html``), a report directory in ``dist_root`` or a file below
       it, or ``<dist_root>/components`` or ``<dist_root>/experiments``.

    Not reached: for rule 4, any other project file outside ``dist_root`` and ``views_root``, a
    file kept by hand below ``dist_root``, a file a later release adds, and a name reached only
    through a link; and a link planted *below* a validated root back into the model (no MCP tool
    creates one, so only the user can).
    """
    root = project.resolved_project_root
    candidates = {
        "model_root": project.resolved_model_root,
        "dist_root": project.resolved_dist_root,
        "views_root": project.resolved_views_root,
        "outputs.arxml": project.resolved_arxml_path,
        "outputs.needs": project.resolved_needs_path,
    }
    # Before the containment check, so both platforms report "written absolute": on the platform
    # that calls the path absolute it also resolves outside the root.
    for field, written in (("dist_root", project.dist_root), ("views_root", project.views_root)):
        if is_absolute_anywhere(written):
            raise ManifestRootWrittenAbsoluteError(field, written)

    for field, path in candidates.items():
        if not _contains(root, path):
            raise ManifestPathOutsideProjectError(field, path, root)

    model_root = project.resolved_model_root
    for field, path in candidates.items():
        if field == "model_root":
            continue
        if _overlaps(model_root, path):
            raise ManifestPathOverlapsModelError(
                field,
                path,
                model_root,
            )

    collision = _needs_collision(project, manifest_path)
    if collision is not None:
        raise ManifestOutputCollisionError("outputs.needs", project.resolved_needs_path, *collision)


def _parse_manifest(manifest_path: Path, source: Literal["manifest", "env"]) -> SwcraftProject:
    """Parse *manifest_path* into a SwcraftProject whose project directory is the manifest's."""
    raw = load_yaml_document(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ManifestShapeError(type(raw).__name__)
    project = SwcraftProject.model_validate({**raw, "source": source})
    project._project_dir = manifest_path.parent.resolve()
    _validate_project_paths(project, manifest_path)
    return project


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_project(start_dir: str | None = None) -> SwcraftProject | None:
    """Walk up from *start_dir* (default: cwd) looking for swcraft.yaml.

    If no manifest is found by walk-up, falls back to the SWCRAFT_MANIFEST
    environment variable.  Returns None when nothing is found.
    """
    search_start = Path(start_dir).resolve() if start_dir else Path.cwd().resolve()

    current = search_start
    while True:
        candidate = current / _MANIFEST_FILENAME
        if candidate.is_file():
            return _parse_manifest(candidate, source="manifest")
        parent = current.parent
        if parent == current:
            break
        current = parent

    env_path = os.environ.get("SWCRAFT_MANIFEST", "")
    if env_path:
        env_manifest = Path(env_path)
        if env_manifest.is_file():
            return _parse_manifest(env_manifest, source="env")

    return None


def require_project(path: str) -> SwcraftProject:
    """Load SwcraftProject from *path* (must be the manifest file).

    No walk-up discovery.

    Raises:
        FileNotFoundError: if *path* is empty or the file does not exist.
    """
    if not path:
        raise FileNotFoundError("require_project: no manifest path provided")
    manifest = Path(path)
    if not manifest.is_file():
        raise ManifestNotFoundError(path)
    return _parse_manifest(manifest, source="manifest")


class ManifestNotFoundError(FileNotFoundError):
    """A manifest that is not on disk, named by its FILE NAME unless a local terminal asks for more.

    An MCP client already knows its project directory, so a location adds nothing it can act on.
    :meth:`render` chooses the form: ``str()`` is the shared form, and the CLI passes
    ``full_path=True``, because a local terminal is the user's own.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(self.render())

    def render(self, *, full_path: bool = False) -> str:
        """The refusal, naming the manifest by its file name, or as given for a local terminal."""
        return f"require_project: manifest not found: {self.path if full_path else Path(self.path).name}"


class ManifestRootWrittenAbsoluteError(ValueError):
    """``dist_root`` or ``views_root`` written as an absolute path (:func:`_validate_project_paths`).

    A ``ValueError``; the type and :attr:`field` let a caller that must not repeat the written value
    branch on the class. ``str()`` includes the written value.
    """

    def __init__(self, field: str, written: str) -> None:
        self.field = field
        super().__init__(
            f"Manifest field '{field}' is written as the absolute path '{written}'. The two roots "
            "`swcraft clean-outputs` removes must be written relative to the project directory, so "
            "that the manifest means the same thing on every machine that reads it."
        )


class ManifestShapeError(ValueError):
    """A manifest whose YAML document is no mapping; :attr:`got` names its type (``NoneType`` if empty)."""

    def __init__(self, got: str) -> None:
        self.got = got
        super().__init__(f"Invalid swcraft.yaml: expected a mapping, got {got}")


class ManifestPathError(ValueError):
    """A manifest field that resolves outside the project root, or into the model, named by the FIELD.

    Both locations are the host's, so the shared form (``str()``) names the field, which is what an
    MCP client can act on; ``render(full_path=True)`` names both resolved locations for the CLI.
    The two refusals are the subclasses below; this class is what a caller catches.
    """

    def __init__(self, field: str, path: str) -> None:
        self.field, self.path = field, path
        super().__init__(self.render())

    def render(self, *, full_path: bool = False) -> str:
        """The refusal naming the field, or both resolved locations for a local terminal."""
        raise NotImplementedError


class ManifestPathOutsideProjectError(ManifestPathError):
    """A manifest field that resolves outside the project root (:class:`ManifestPathError`)."""

    def __init__(self, field: str, path: str, root: str) -> None:
        self.root = root
        super().__init__(field, path)

    def render(self, *, full_path: bool = False) -> str:
        """The field alone, or both resolved locations for a local terminal."""
        if full_path:
            return (
                f"Manifest field '{self.field}' resolves to '{self.path}', which is outside the project root "
                f"'{self.root}'."
            )
        return f"Manifest field '{self.field}' resolves outside the project root."


class ManifestPathOverlapsModelError(ManifestPathError):
    """A manifest field that resolves into the model root (:class:`ManifestPathError`)."""

    def __init__(self, field: str, path: str, model_root: str) -> None:
        self.model_root = model_root
        super().__init__(field, path)

    def render(self, *, full_path: bool = False) -> str:
        """The field alone, or both resolved locations for a local terminal."""
        where = (
            f" resolves to '{self.path}', which overlaps 'model_root' ('{self.model_root}')."
            if full_path
            else " overlaps 'model_root'."
        )
        return (
            f"Manifest field '{self.field}'{where} Generated paths must be outside the model: generating or "
            "cleaning them would otherwise write into, or delete, the model that is the source of truth."
        )


def experiment_output_directory(project: SwcraftProject, written: str) -> Path:
    """The directory an experiment's configuration names as its ``output``, resolved, or a refusal.

    *written* is the configuration's ``output`` exactly as the user wrote it, resolved against the
    project directory with :func:`_resolved` (a symlink judged as what it resolves to). A value
    carrying a NUL character is refused first, in a sentence that does not repeat it. Then seven
    refusals, run in the order 1, 2, 6, 3, 4, 5, 7, each a plain :class:`ValueError` whose one
    sentence names the key and the written value, never a resolved location:

    1. written absolute on **either** platform's rules (:func:`is_absolute_anywhere`);
    2. resolving to the project directory itself (an empty string included);
    3. resolving outside the project directory (compared exactly);
    4. overlapping ``model_root`` in either direction, case-insensitively;
    5. naming an existing non-directory, or a path below one (:func:`_experiment_directory`);
    6. naming or lying inside ``.git`` (any case), a symlink into it included; before 3 only for a
       path inside the project;
    7. an existing directory already holding a ``report.md`` or a ``result.json`` no experiment run
       wrote (:func:`_foreign_reports`), named in that order.

    Refusals 6 and 7 are this function's alone, not :func:`experiment_default_directory`'s. Not
    reached: any kind's ``result.json`` counts as a run's, a hand-made one in a run's shape too; a
    run's hand-edited ``report.md`` is still replaced; other files a run writes are not checked;
    other tool directories (``.vscode``, ``.venv``) are not refused; a path created between this
    check and the write is not seen; a ``result.json`` is read whole, and one replaced by a FIFO or a
    device between its ``stat`` and its read is opened by that read, which can block.
    """
    if "\0" in written:
        raise ValueError("the configuration's 'output' carries a NUL character, which no directory name can hold")
    if is_absolute_anywhere(written):
        raise ValueError(
            f"the configuration's 'output' is written as the absolute path '{written}': it must be written "
            "relative to the project directory, so that the configuration means the same on every machine "
            "that reads it"
        )
    root = project.resolved_project_root
    resolved = _resolved(Path(root) / written)
    subject = f"the configuration's 'output' ('{written}')"
    if resolved == root:
        raise ValueError(f"{subject} is the project directory itself: the report needs a directory of its own")
    if _contains(root, resolved) and any(part.casefold() == ".git" for part in Path(resolved).relative_to(root).parts):
        raise ValueError(
            f"{subject} names or lies inside '.git', which belongs to git: write the report to a directory of the "
            "project's own"
        )
    directory = _experiment_directory(project, resolved, subject)
    foreign = _foreign_reports(directory)
    if foreign:
        names = " and ".join(f"'{name}'" for name in foreign)
        it, files = ("it", "that file") if len(foreign) == 1 else ("them", "those files")
        raise ValueError(
            f"{subject} already holds {names}, which no experiment run wrote: the run would replace {it}. "
            f"Choose another directory, or move {files} away."
        )
    return directory


#: The two files every experiment run writes into its report directory.
EXPERIMENT_REPORT_FILENAME = "report.md"
EXPERIMENT_RESULT_FILENAME = "result.json"


def _foreign_reports(directory: Path) -> list[str]:
    """The report files in *directory* no experiment run wrote, in the order ``report.md``, ``result.json``.

    A ``result.json`` is a run's when it reads as UTF-8 JSON whose top level is an object with an
    integer ``schemaVersion`` (``type(...) is int``, so ``true`` is not one) and a non-empty string
    ``kind``; any other, an unreadable one included, is foreign. It is read only when it is a
    regular file (a symlink followed), so a FIFO or a device is foreign unopened and the check
    cannot block or read endlessly. A ``report.md`` is foreign unless a run's ``result.json`` sits
    beside it. Presence is :func:`os.path.lexists`, so a dangling symlink the write would follow
    counts. Empty when *directory* does not exist.
    """
    result = directory / EXPERIMENT_RESULT_FILENAME
    is_runs = False
    if os.path.lexists(result) and result.is_file():
        try:
            document = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            document = None
        is_runs = (
            isinstance(document, dict)
            and type(document.get("schemaVersion")) is int
            and isinstance(document.get("kind"), str)
            and document["kind"] != ""
        )
    foreign = []
    if os.path.lexists(directory / EXPERIMENT_REPORT_FILENAME) and not is_runs:
        foreign.append(EXPERIMENT_REPORT_FILENAME)
    if os.path.lexists(result) and not is_runs:
        foreign.append(EXPERIMENT_RESULT_FILENAME)
    return foreign


def experiment_default_directory(project: SwcraftProject, relative: str) -> Path:
    """``<dist_root>/<relative>``, resolved, or a refusal: the report directory when no ``output`` is configured.

    *relative* is the kind's own spelling below ``dist_root`` (``experiments/<name>``). A symlink
    below ``dist_root`` is followed and refused outside the project, over the model, or on a file,
    with ``<dist_root>`` as written in the sentence, never a resolved location.
    """
    resolved = _resolved(Path(project.resolved_dist_root) / relative)
    return _experiment_directory(project, resolved, f"the report directory '<dist_root>/{relative}'")


def _experiment_directory(project: SwcraftProject, resolved: str, subject: str) -> Path:
    """Refusals 3-5 of :func:`experiment_output_directory` for the *resolved* directory *subject* names.

    The file rule walks up to the nearest existing path, so a directory below a file
    (``swcraft.yaml/reports``) is refused here and not by a later ``mkdir``.
    """
    root = project.resolved_project_root
    if not _contains(root, resolved):
        raise ValueError(f"{subject} resolves outside the project directory")
    if _overlaps(project.resolved_model_root, resolved):
        raise ValueError(
            f"{subject} overlaps 'model_root': the report would be written into, or around, the model that is "
            "the source of truth"
        )
    directory = Path(resolved)
    existing = next(each for each in (directory, *directory.parents) if each.exists())
    if existing == directory and not directory.is_dir():
        raise ValueError(f"{subject} names a file, not a directory")
    if not existing.is_dir():
        raise ValueError(
            f"{subject} lies below '{existing.relative_to(root).as_posix()}', which is a file, not a directory"
        )
    return directory


def project_relative_name(project: SwcraftProject, path: str | Path) -> str | None:
    """*path*, resolved against the current directory, as a POSIX name relative to the project directory.

    ``None`` when *path* resolves outside the project directory; a symlink is judged as what it
    resolves to.
    """
    root = project.resolved_project_root
    resolved = _resolved(Path(path))
    if not _contains(root, resolved):
        return None
    return Path(resolved).relative_to(root).as_posix()


# The manifest's resolution and containment under public names, so a caller judging a path against
# ``model_root`` resolves and compares both sides alike. ``path_contains`` is exact; ``paths_overlap``
# casefolds.
MANIFEST_FILENAME = _MANIFEST_FILENAME
resolved_path = _resolved
path_contains = _contains
paths_overlap = _overlaps


class ManifestOutputCollisionError(ManifestPathError):
    """``outputs.needs`` naming a file the project keeps under another name (:class:`ManifestPathError`).

    *what* names that file in the shared form, never a location; *other* is its resolved location,
    shown only on a local terminal.
    """

    def __init__(self, field: str, path: str, what: str, other: str) -> None:
        self.what, self.other = what, other
        super().__init__(field, path)

    def render(self, *, full_path: bool = False) -> str:
        """The field and what it names, with both resolved locations for a local terminal."""
        where = (
            f" resolves to '{self.path}', which is {self.what} ('{self.other}')" if full_path else f" names {self.what}"
        )
        return (
            f"Manifest field '{self.field}'{where}. `swcraft generate needs` would overwrite that file: give the "
            "needs.json a file of its own."
        )


def _needs_collision(project: SwcraftProject, manifest_path: Path) -> tuple[str, str] | None:
    """What ``outputs.needs`` names besides itself, and where, or ``None`` (rule 4 of :func:`_validate_project_paths`).

    In this order: the five files (the ARXML ``outputs.arxml`` names, its ``<stem>_old<suffix>``
    sibling, :data:`AUDIT_LOG_FILE` and :data:`BUILD_REPORT_FILE` in ``dist_root``, the manifest);
    a name ``generate diagrams`` writes directly in ``views_root`` (:data:`DIAGRAM_REPORT_FILE` or a
    suffix in :data:`DIAGRAM_FILE_SUFFIXES`; a view's name comes from the model, so the suffix is
    what a manifest can be judged by); a directory of :data:`REPORT_DIRECTORIES` or any file below
    it; a directory of :data:`NAMED_OUTPUT_DIRECTORIES` itself. Compared case-folded, as
    :func:`_overlaps` compares.
    """
    arxml = Path(project.resolved_arxml_path)
    dist = Path(project.resolved_dist_root)
    files = (
        ("the file 'outputs.arxml' names", str(arxml)),
        ("the previous ARXML kept beside 'outputs.arxml'", str(arxml.with_name(f"{arxml.stem}_old{arxml.suffix}"))),
        ("the audit log in 'dist_root'", str(dist / AUDIT_LOG_FILE)),
        ("the build report in 'dist_root'", str(dist / BUILD_REPORT_FILE)),
        ("the manifest", _resolved(manifest_path)),
    )
    needs = project.resolved_needs_path.casefold()
    found = next(((what, other) for what, other in files if other.casefold() == needs), None)
    if found is not None:
        return found
    needs_path = Path(needs)
    name = needs_path.name
    if str(needs_path.parent) == project.resolved_views_root.casefold() and (
        name == DIAGRAM_REPORT_FILE or name.endswith(DIAGRAM_FILE_SUFFIXES)
    ):
        return "a file `swcraft generate diagrams` writes in 'views_root'", project.resolved_needs_path
    parents = {str(parent) for parent in needs_path.parents}
    for directory, command in REPORT_DIRECTORIES.items():
        folded = str(dist / directory).casefold()
        if needs == folded:
            return f"the report directory of {command} in 'dist_root'", str(dist / directory)
        if folded in parents:
            return f"a file in the report directory of {command} in 'dist_root'", project.resolved_needs_path
    for directory, what in NAMED_OUTPUT_DIRECTORIES.items():
        if needs == str(dist / directory).casefold():
            return f"the directory of {what} in 'dist_root'", str(dist / directory)
    return None


#: The audit log every model write appends to in ``dist_root`` (``editing.audit.AUDIT_FILENAME``).
AUDIT_LOG_FILE = "audit.jsonl"
#: The build report ``validate --report`` writes in ``dist_root``, restated because this module
#: imports no writer.
BUILD_REPORT_FILE = "build-report.md"
#: The diagram report ``generate diagrams`` writes in ``views_root``.
DIAGRAM_REPORT_FILE = "report.txt"
#: The suffixes of every other file ``generate diagrams`` writes or removes in ``views_root``:
#: ``writer.diagrams.page.INDEX_PAGE`` and each view's ``.svg`` and ``.html`` (``GENERATED_FILE_RE``).
DIAGRAM_FILE_SUFFIXES = (".svg", ".html")
#: The directories below ``dist_root`` a shipped command writes its report into, every file under a
#: name the command fixes, with the command that writes there.
REPORT_DIRECTORIES: Mapping[str, str] = {
    "metrics": "`swcraft metrics --report`",
    "resource-report": "`swcraft resource-report --report`",
    "change-report": "`swcraft change-report`",
    "test-tables": "`swcraft generate test-tables`",
}
#: The directories below ``dist_root`` whose files the model or the user names, so only the
#: directory's own name is fixed.
NAMED_OUTPUT_DIRECTORIES: Mapping[str, str] = {
    "components": "the single-component exports",
    "experiments": "the experiment reports",
}
