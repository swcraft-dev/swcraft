"""The whole model in memory, read through the kernel registry.

:mod:`swcraft_tools.model_registry` resolves the contract into facts; this
module turns those facts into files read off disk. It publishes
:class:`ModelState` — a layer-keyed value with the elements of every declared
layer, the placement values of the one layer that nests, and the names of the
layers whose directory is not there — and :func:`load_model_state`, which builds
one. It sits after ``model_registry`` and before ``reader``, ``snapshot`` and
``differ`` (which it never imports); the element parser and the git-revision
materializer live here, re-exported by ``reader.py`` and ``snapshot.py``.

**The only layer-directory traversal in the generic core**: :func:`scan_layer`
globs a layer by ``registry.storage(layer)``; ``_held_names`` lists every entry
of a write's directory, not only ``*.yaml``, since a ``Foo.YAML`` occupies
``Foo.yaml`` on a case-insensitive filesystem; :func:`refuse_retired_storage`
refuses a model that keeps element files in a retired directory. Not covered:
``session._iter_model_files``, ``cli.init_command._template_file_plan`` and
``editing/engine._require_model_root``, which walk on their own.

**One read per layer.** Inside a :func:`single_read` block a second load of a
layer is answered from the first; no block is open by default. The block does
not reach a read outside the layer loader (:func:`declared_identity`,
``reader.find_element_file``, ``component_exchange._scan_swc_names``,
``requirements_import``'s reads), nor directory listings, nor a root reached
through another spelling (a symlinked alias), which is a miss.
"""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar

import yaml
from pydantic import TypeAdapter, ValidationError

from swcraft_tools.model_registry import (
    LayerFacts,
    ModelRegistry,
    RetiredStorage,
    StorageRecord,
    compose_storage_path,
    current_registry,
    segment_refusal,
)
from swcraft_tools.yaml_load import load_yaml_document

__all__ = [
    "ElementParseError",
    "FreePath",
    "LayerFile",
    "LayerScan",
    "ModelState",
    "PlannedStorage",
    "RetiredStorageError",
    "RevisionReadError",
    "StorageNameRefusal",
    "declared_identity",
    "echoed_argument",
    "free_storage_path",
    "git_environment",
    "load_layer",
    "load_layer_with_placement",
    "load_model_state",
    "materialize_revision",
    "parse_element",
    "parse_label",
    "placement_from_location",
    "refuse_retired_storage",
    "reject_option_like_revision",
    "scan_layer",
    "single_read",
    "stored_element_files",
    "stored_storage_names",
]

_T = TypeVar("_T")


# ---------------------------------------------------------------------------
# Parsing one element
# ---------------------------------------------------------------------------


class ElementParseError(ValueError):
    """One model file that does not parse, or does not validate.

    Its message names the file by its **model-root-relative storage name** —
    ``Invalid SWC YAML in swc/example/ExampleSWC.yaml: …`` — never by a
    directory above the model root, because the message reaches MCP clients.
    :meth:`render` with ``full_path=True`` names the full path for a local
    terminal.

    ``details`` holds one self-contained sentence per field the file gets wrong
    (``id: Field required``, ``line 2, column 9: <the YAML parser's problem>``),
    never Pydantic's own text. :meth:`render` and ``str()`` are **one line**, the
    details joined by ``; ``, so a wrapper that prefixes the text keeps its
    one-line shape; :meth:`render_lines` gives one line per detail, each naming
    the file.
    """

    def __init__(self, target: str, *, label: str, path: Path, details: tuple[str, ...]) -> None:
        self.target, self.label, self.path, self.details = target, label, path, details
        super().__init__(self.render())

    def render(self, *, full_path: bool = False) -> str:
        """The refusal on one line, naming the file by its storage name, or by its full path for a local terminal."""
        return f"Invalid {self.label} YAML in {self.path if full_path else self.target}: {'; '.join(self.details)}"

    def render_lines(self, *, full_path: bool = False) -> list[str]:
        """One self-contained line per detail, each naming the file as :meth:`render` does."""
        where = self.path if full_path else self.target
        return [f"Invalid {self.label} YAML in {where}: {detail}" for detail in self.details]


def parse_element(yaml_file: Path, layer: str, validate: Callable[[object], _T], model_root: Path) -> _T:
    """Parse one model file and validate it, naming the file on any failure.

    Args:
        yaml_file: the file to read.
        layer: how to name this layer in the error message ("data type", "SWC", ...).
        validate: the Pydantic entry point for this layer — ``Model.model_validate``
            or a ``TypeAdapter``'s ``validate_python``.
        model_root: the root *yaml_file* was globbed under, so the refusal can
            name the file by its model-root-relative target, and render the full
            path only when a local terminal asks for it.

    Raises:
        ElementParseError: on malformed YAML syntax or a schema validation
            failure, naming the offending file before the underlying error.
    """
    # Kept in a local so the refusal's details can tell a key the file holds from a union member's tag.
    document: object = None
    try:
        document = load_yaml_document(yaml_file.read_text(encoding="utf-8"))
        return validate(document)
    except (ValidationError, yaml.YAMLError) as exc:
        raise ElementParseError(
            yaml_file.relative_to(model_root).as_posix(),
            label=layer,
            path=yaml_file,
            details=_load_error_details(exc, document),
        ) from exc


#: The one layer whose refusal spells it other than its token with underscores as spaces: an acronym.
_LABEL_OVERRIDES = MappingProxyType({"swc": "SWC"})


def parse_label(layer: LayerFacts) -> str:
    """How a refusal about *layer* names it — ``"data type"``, ``"SWC"``."""
    override = _LABEL_OVERRIDES.get(layer.name)
    if override is not None:
        return override
    return layer.token.replace("_", " ")


# ---------------------------------------------------------------------------
# The one traversal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerFile:
    """One file of one layer, with whatever its location encodes.

    ``placement`` is empty for a flat layer and ``{"group": "sensors"}`` for a
    file under ``swc/sensors/``. It is kept beside the element, never merged into
    it: a Pydantic field would be a second representation that can disagree
    with the directory.
    """

    path: Path
    placement: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LayerScan:
    """What one layer's directory holds — or that it is not there at all.

    ``present`` separates a layer a project has not written yet from one it
    wrote empty, which lets ``check-model`` report an absent ``hardware/`` as a
    finding of its own (``hardware.layer-absent``).
    """

    present: bool
    files: tuple[LayerFile, ...]


def scan_layer(model_root: str | Path, layer: str, *, registry: ModelRegistry | None = None) -> LayerScan:
    """Every model file of *layer* under *model_root*, in filename order.

    The directory, the recursion and the suffix are read from the layer's
    storage record. A missing directory is reported (``present=False``), not
    raised.

    This is the file order, by path; the order of a layer's *elements* is
    :func:`load_layer_with_placement`'s, by composed storage path. The sort key
    is ``as_posix()``, because ``sorted()`` over ``Path`` collates
    case-insensitively with ``\\`` as the separator on Windows, so the same tree
    would sort differently by platform: ``['Zebra', 'alpha', 'Beta']`` comes back
    ``Beta, Zebra, alpha`` on POSIX and ``alpha, Beta, Zebra`` on Windows.
    """
    registry = registry or current_registry()
    record = registry.storage(layer)
    directory = Path(model_root) / record.directory
    if not directory.exists():
        return LayerScan(present=False, files=())
    pattern = f"**/*{record.file_suffix}" if record.is_recursive else f"*{record.file_suffix}"
    files = tuple(
        LayerFile(path=path, placement=placement_from_location(path, directory, record))
        for path in sorted(directory.glob(pattern), key=Path.as_posix)
    )
    return LayerScan(present=True, files=files)


def placement_from_location(path: Path, directory: Path, record: StorageRecord) -> Mapping[str, str]:
    """The placement values *path*'s location encodes, for a nesting layer.

    The value is the POSIX-relative directory of the file under the layer's own
    directory: ``swc/sensors/Foo.yaml`` is ``sensors`` and a flat
    ``swc/Foo.yaml`` is the empty string. A deeper tree (``swc/a/b/Foo.yaml``)
    is reported verbatim as ``a/b``; refusing it is a validation question.
    """
    if not record.is_recursive:
        return {}
    relative = path.parent.relative_to(directory)
    return {record.nesting: "" if relative == Path(".") else relative.as_posix()}


# ---------------------------------------------------------------------------
# The free file name
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FreePath:
    """Where one element's file goes, as :func:`free_storage_path` decided it.

    ``path`` is model-root-relative and POSIX-spelled (``data_types/float32~1.yaml``).
    ``suffixed`` says a ``~N`` was taken because the plain name was held by
    something else. ``occupied_by_self`` says the plain name is already held by a
    file declaring the very identity asked about — for a ``create`` that is an
    identity conflict, for an ``update`` it is the element's own file — and then
    ``path`` names **that** file as it is spelled on disk, which is not always the
    composed spelling: on a case-insensitive root ``float32.yaml`` may be the file
    that declares ``Float32``.
    """

    path: str
    suffixed: bool
    occupied_by_self: bool


class StorageNameRefusal(ValueError):
    """An identity whose collision-suffixed file name would not fit a path component.

    ``model_registry`` admits an identity of up to 198 bytes, because ``.yaml``
    plus the commit's ``.<32 hex>.swcraft-commit.tmp`` staging suffix fills a
    255-byte component exactly; a ``~N`` pushes a near-the-bound identity over
    it. Refused before anything is written, with a message naming the bound.
    """


def free_storage_path(
    model_root: str | Path,
    layer: str,
    identity: str,
    placement: Mapping[str, str] | None = None,
    *,
    registry: ModelRegistry,
    reserved: Iterable[str] = (),
) -> FreePath:
    """The file *identity* of *layer* is written to — the plain name, or the nearest free ``~N``.

    The plain path is :func:`~swcraft_tools.model_registry.compose_storage_path`'s. It is free
    unless an entry of its directory, or a name in *reserved*, matches it by ``str.casefold()``;
    a regular file there declaring *identity* itself answers *occupied by self*. Otherwise it is
    ``{identity}~{n}{file_suffix}`` for the smallest free ``n`` from 1; the ``name:`` inside, the
    references and the generated artifacts are unchanged. Occupancy is one directory listing, so
    every platform answers alike. Ask immediately before each write.

    Args:
        model_root: the model directory the path is relative to.
        layer: the contract layer name.
        identity: the element's declared identity.
        placement: the placement values a nesting layer composes a directory
            from, as :func:`compose_storage_path` takes them.
        registry: the **kernel** registry — keyword-only and **required**, with no ambient default.
        reserved: model-root-relative paths a caller has already allocated and not
            yet written, POSIX-spelled as :attr:`FreePath.path`; one in another directory occupies nothing.

    Raises:
        TypeError: *registry* is not a :class:`ModelRegistry`.
        StorageNameRefusal: a suffixed candidate would exceed the file-name bound once the commit's staging
            suffix is counted (a plain name is the identity guard's question, ``model_registry.path_refusal``).
        StorageDirectoryRefusal: something that is not a directory holds the
            layer's storage directory. An absent directory is no refusal.

    Not reached: Unicode normalisation (it collides on APFS, not NTFS); a ``Foo~1.yaml`` declaring ``Foo``
    beside a ``Foo.yaml`` declaring another identity is not *occupied by self* (a caller refuses a held
    identity before it asks for a path).
    """
    _require_kernel_registry(registry, "free_storage_path")
    record = registry.storage(layer)
    plain = compose_storage_path(record, identity, placement)
    directory, _, plain_name = plain.rpartition("/")
    held = _held_names(Path(model_root) / directory, layer=layer, storage_name=directory)
    for path in reserved:
        reserved_directory, _, reserved_name = str(path).rpartition("/")
        if reserved_directory == directory:
            held.setdefault(reserved_name.casefold(), [])
    return _free_in(model_root, layer, identity, plain, held, record=record, registry=registry)


def _require_kernel_registry(registry: object, caller: str) -> None:
    """Refuse anything but the kernel :class:`ModelRegistry`, as :func:`free_storage_path` documents."""
    if not isinstance(registry, ModelRegistry):
        raise TypeError(
            f"{caller} needs the kernel ModelRegistry, not "
            f"{type(registry).__name__}: an editing projection with no kernel registry is an engine defect, "
            "and the ambient registry is never substituted for it"
        )


def _free_in(
    model_root: str | Path,
    layer: str,
    identity: str,
    plain: str,
    held: Mapping[str, list[str]],
    *,
    record: StorageRecord,
    registry: ModelRegistry,
) -> FreePath:
    """The decision :func:`free_storage_path` and :class:`PlannedStorage` share, over a built index.

    *held* maps ``casefold()`` keys to the entries' spellings; a reserved name
    has an empty list.
    """
    directory, _, plain_name = plain.rpartition("/")
    key = plain_name.casefold()
    if key not in held:
        return FreePath(path=plain, suffixed=False, occupied_by_self=False)

    identity_field = registry.layer(layer).identity_field
    # The exact spelling first, so a case-sensitive root holding both `Foo.yaml`
    # and `foo.yaml` answers with the composed one when both declare the identity.
    for occupant in sorted(held[key], key=lambda name: (name != plain_name, name)):
        if declared_identity(Path(model_root) / directory / occupant, identity_field) == identity:
            return FreePath(path=f"{directory}/{occupant}", suffixed=False, occupied_by_self=True)

    n = 1
    while True:
        stem = f"{identity}~{n}"
        refusal = segment_refusal(stem, f"the identity with its collision suffix '~{n}'")
        if refusal is not None:
            raise StorageNameRefusal(
                f"the file name of '{identity}' in layer '{layer}' is held by another element, so it needs a "
                f"collision suffix, and {refusal}"
            )
        candidate = f"{stem}{record.file_suffix}"
        if candidate.casefold() not in held:
            return FreePath(path=f"{directory}/{candidate}", suffixed=True, occupied_by_self=False)
        n += 1


class PlannedStorage:
    """Free file names for a caller that plans many targets before it writes any.

    Each directory is listed once, at its first :meth:`allocate`, and every
    answered path is reserved in that index; the answers are
    :func:`free_storage_path`'s with every earlier answer passed as *reserved*.
    This avoids the O(n²) cost of passing a growing *reserved* on each call.

    **What it does not reach.** It holds the listing it took, so it is for a
    pass that writes **nothing** between its allocations; a writer that writes
    as it goes asks :func:`free_storage_path` at each write. Nothing re-lists a
    directory that changes under it.

    Args:
        model_root: the model directory the paths are relative to.
        registry: the **kernel** registry, required, as :func:`free_storage_path`
            takes it.
    """

    def __init__(self, model_root: str | Path, *, registry: ModelRegistry) -> None:
        _require_kernel_registry(registry, "PlannedStorage")
        self._model_root = Path(model_root)
        self._registry = registry
        self._held: dict[str, dict[str, list[str]]] = {}

    def allocate(self, layer: str, identity: str, placement: Mapping[str, str] | None = None) -> FreePath:
        """The file *identity* of *layer* goes to, reserved for every later call.

        Raises what :func:`free_storage_path` raises; a refused allocation
        reserves nothing.
        """
        record = self._registry.storage(layer)
        plain = compose_storage_path(record, identity, placement)
        directory = plain.rpartition("/")[0]
        held = self._held.get(directory)
        if held is None:
            held = _held_names(self._model_root / directory, layer=layer, storage_name=directory)
            self._held[directory] = held
        free = _free_in(self._model_root, layer, identity, plain, held, record=record, registry=self._registry)
        held.setdefault(free.path.rpartition("/")[2].casefold(), [])
        return free


def _held_names(directory: Path, *, layer: str, storage_name: str) -> dict[str, list[str]]:
    """Every entry name of *directory*, keyed by its ``casefold()``.

    Every entry — files of any suffix, directories, links — because each holds
    its name on a case-insensitive filesystem. A key has two members only on a
    case-sensitive root. An absent directory holds nothing (the writer creates
    it).

    Args:
        directory: the absolute directory to list.
        layer: the contract layer name, for the refusal.
        storage_name: *directory* as the model root spells it
            (``swc/sensors``), for the refusal, which may name only a
            model-root-relative path.

    Raises:
        StorageDirectoryRefusal: something that is not a directory holds the
            name. Every other ``OSError`` propagates unchanged.
    """
    held: dict[str, list[str]] = {}
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                held.setdefault(entry.name.casefold(), []).append(entry.name)
    except NotADirectoryError as refused:
        raise StorageDirectoryRefusal(layer, storage_name) from refused
    except FileNotFoundError:
        return held
    return held


def declared_identity(path: Path, identity_field: str) -> str | None:
    """The identity the regular file at *path* declares, or ``None``.

    The one reader of which element a file holds, for every writer and check
    that locates an element by content. Read straight off the YAML mapping, so an
    element that does not validate still declares a name. A directory, an
    unreadable file, a file that is not UTF-8, one that does not parse or is not
    a mapping, and a value that is not a string declare nothing.
    """
    if not path.is_file():
        return None
    try:
        document = load_yaml_document(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    if not isinstance(document, dict):
        return None
    value = document.get(identity_field)
    return value if isinstance(value, str) else None


def stored_element_files(model_root: str | Path, layer: str, *, registry: ModelRegistry) -> dict[str, list[LayerFile]]:
    """``declared identity -> the files of *layer* that declare it``, each list in listing order.

    The inverse the storage mapping does not have, taken by **reading** the
    layer: one :func:`scan_layer` listing and one :func:`declared_identity` read
    per file. A file declaring no identity is left out; each :class:`LayerFile`
    carries its placement, so a nesting layer's caller picks the file in the
    element's own directory. *registry* is required, as for
    :func:`free_storage_path`: locating by one contract and writing by another
    must not happen.
    """
    identity_field = registry.layer(layer).identity_field
    stored: dict[str, list[LayerFile]] = {}
    for entry in scan_layer(model_root, layer, registry=registry).files:
        identity = declared_identity(entry.path, identity_field)
        if identity is not None:
            stored.setdefault(identity, []).append(entry)
    return stored


def stored_storage_names(model_root: str | Path, layer: str, *, registry: ModelRegistry) -> dict[str, list[str]]:
    """``declared identity -> the storage names of the files of *layer* that declare it``, in listing order.

    A storage name is the file's path relative to *model_root*, spelled POSIX
    (``swc/chassis/Sensor.yaml``, ``tasks/Boot~1.yaml``): one spelling for every
    output that names where an element is stored. It is **lexical**: a symbolic
    link is named by its own path, never resolved. An identity two files declare
    has two names, in listing order.
    """
    root = Path(model_root)
    return {
        identity: [entry.path.relative_to(root).as_posix() for entry in entries]
        for identity, entries in stored_element_files(root, layer, registry=registry).items()
    }


# ---------------------------------------------------------------------------
# The retired directories
# ---------------------------------------------------------------------------


def _retired_element_files(model_root: Path, retired: RetiredStorage) -> int:
    """How many element files lie directly in *retired*'s directory under *model_root*.

    One level only, so the layer directories that live **below** a retired one
    (``data_types/base/`` under ``data_types``) are not counted.
    """
    directory = model_root / retired.directory
    if not directory.is_dir():
        return 0
    return sum(1 for path in directory.glob(f"*{retired.file_suffix}") if path.is_file())


def refuse_retired_storage(
    model_root: str | Path, *, registry: ModelRegistry | None = None, revision: str | None = None
) -> None:
    """Refuse a model that keeps element files where an earlier contract stored them.

    The contract's ``retiredStorage`` table says which directories are retired
    and where their files belong now; this lists each one level deep and raises
    :class:`RetiredStorageError` when any holds an element file. Every entry
    point that reads layers calls it once before the first file is parsed —
    :func:`load_layer`, :func:`_read_state` and the reader's SWC-with-placement
    function — so every reader refuses the same model with the same sentence. A
    materialized git revision is refused too, and the sentence then names
    *revision* as the caller passed it, never a path.

    **What it does not reach.** A file one level down
    (``mapping_sets/x/y.yaml``) is neither read nor refused, and a file with
    another suffix is not an element file. A retired directory holding no
    element file is not an error. Nothing converts a model on the old layout,
    a revision included: ``swcraft diff`` across the commit that moves the files
    is refused on the old side.
    """
    registry = registry or current_registry()
    root = Path(model_root)
    found = [(retired, _retired_element_files(root, retired)) for retired in registry.retired_storage]
    held = tuple((retired, count) for retired, count in found if count)
    if held:
        raise RetiredStorageError(held, revision=revision)


class RetiredStorageError(ValueError):
    """A model that keeps element files in a directory the contract retired.

    A ``ValueError``, so every caller that renders a model it cannot read
    renders this one. The sentence names the retired directory, the count of
    files and the directories they belong in, all model-root-relative, so it
    needs no :meth:`render` twin. A refusal of a materialized revision names the
    revision as the caller passed it (``in revision 'HEAD'``), never the
    temporary directory.
    """

    def __init__(self, held: tuple[tuple[RetiredStorage, int], ...], *, revision: str | None = None) -> None:
        self.held = held
        self.revision = revision
        parts = []
        for retired, count in held:
            targets = [f"'{target}'" for target in retired.moved_to]
            where = targets[0] if len(targets) == 1 else f"{', '.join(targets[:-1])} or {targets[-1]}"
            files = "1 element file" if count == 1 else f"{count} element files"
            parts.append(
                f"{files} in '{retired.directory}', a directory contract {retired.since} retired; "
                f"{'it belongs' if count == 1 else 'they belong'} in {where}"
            )
        super().__init__(
            "The model keeps its elements in a retired layout"
            + ("" if revision is None else f" in revision '{revision}'")
            + ": "
            + "; ".join(parts)
            + ". Move each file into the directory of its layer; nothing converts a model on the old layout."
        )


# ---------------------------------------------------------------------------
# The state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelState:
    """The whole model at one point in time, keyed by **layer name**.

    The vocabulary is the contract's — ``state["swc"]``,
    ``state["implementation_types"]`` — not ``ModelSnapshot``'s attribute names.
    A plain value: two states with the same content compare equal, and a state
    can be built from planned elements without a directory existing anywhere.
    """

    layers: Mapping[str, list[Any]]
    placement: Mapping[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    absent: frozenset[str] = frozenset()

    def __getitem__(self, layer: str) -> list[Any]:
        return self.layers[layer]

    def __contains__(self, layer: object) -> bool:
        return layer in self.layers

    def __iter__(self) -> Iterator[str]:
        return iter(self.layers)

    @property
    def layer_names(self) -> tuple[str, ...]:
        """Every layer this state carries, in the order the registry declares them."""
        return tuple(self.layers)

    def placement_of(self, layer: str) -> Mapping[str, dict[str, str]]:
        """*layer*'s identity -> placement values, empty for a layer that nests nothing."""
        return self.placement.get(layer, {})


# ---------------------------------------------------------------------------
# One read per block
# ---------------------------------------------------------------------------

#: (absolute root as POSIX, layer, registry) -> :func:`_parse_layer`'s answer. ``ModelRegistry``
#: hashes by identity (``eq=False``), so the key holds it alive and the tripwire re-parses from
#: the key alone.
_Held = dict[tuple[str, str, ModelRegistry], tuple[bool, list[tuple[Any, Mapping[str, str]]]]]

_SINGLE_READ: ContextVar[_Held | None] = ContextVar("single_read", default=None)

#: Called with the memo once a block has closed, on any exit. ``None`` in product code; tests set it.
_single_read_observer: Callable[[_Held], None] | None = None


@contextmanager
def single_read() -> Iterator[None]:
    """Hold each layer's first load for the block, so that a later load reads nothing.

    While the block is open, the first :func:`load_layer_with_placement` of a
    ``(absolute model root, layer, registry)`` stores its result and later calls
    answer from it, with no glob, no read, no parse and no validation.

    **The rules:**

    1. **A memo is empty on entry and dropped on exit**, never compared with the disk.
    2. **No model write happens inside a block.** Only a closed list of callers
       opens one (the opener gate test fails on any other); a caller that writes
       a model and reads it back in one run is not on the list.
    3. **No block is open by default.** A forgotten opener costs time, never truth.
    4. **A block never spans an** ``await``. The memo sits in a
       :class:`~contextvars.ContextVar`: a ``threading.Thread`` started inside a
       block reads the disk, and ``anyio.to_thread`` *would* share the mutable
       memo, so a worker opens its block inside the worker.
    5. **A failed load is not held**; the next call reads again.
    6. **A nested block starts empty** and restores the outer one on exit.

    **Elements are shared, not copied**, since a copy per read would pay back much
    of the gain, so a consumer must not mutate an element it read in a block. The
    in-place mutators are ``writer/mutations.py``'s, reached only from write paths.

    **What it does not reach.** A file saved by another process during a block is
    not seen by that block. A test tripwire compares each held layer with a fresh
    load at exit; it sees only in-process blocks on the paths the tests drive, and
    misses a mutation to an equal value. The module docstring names the reads the
    memo does not hold.
    """
    held: _Held = {}
    token = _SINGLE_READ.set(held)
    try:
        yield
    finally:
        _SINGLE_READ.reset(token)
        if _single_read_observer is not None:
            _single_read_observer(held)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_layer(model_root: str | Path, layer: str, *, registry: ModelRegistry | None = None) -> list[Any]:
    """Every element of *layer*, parsed and validated, in the order of their composed storage paths.

    The order is :func:`load_layer_with_placement`'s, never the files', so a
    renamed file moves nothing. A missing directory yields ``[]``.

    Raises:
        ElementParseError: a file that does not parse or does not validate,
            naming it.
        RetiredStorageError: the model keeps an element file in a directory
            the contract retired (:func:`refuse_retired_storage`).
    """
    registry = registry or current_registry()
    refuse_retired_storage(model_root, registry=registry)
    return [element for element, _ in load_layer_with_placement(model_root, layer, registry)[1]]


def load_layer_with_placement(
    model_root: str | Path, layer: str, registry: ModelRegistry
) -> tuple[bool, list[tuple[Any, Mapping[str, str]]]]:
    """``(the directory exists, [(element, placement), ...])``, in the element order.

    The pairs are sorted by the **file the storage record would write each
    element to** (:func:`~swcraft_tools.model_registry.compose_storage_path`,
    the same answer :func:`free_storage_path` gives), compared as a plain
    ``str``: ordinal and case-sensitive, never ``casefold`` or a locale, so
    ``Zebra`` sorts before ``alpha`` on every platform. The key is read from
    content, never off the file: ``b.yaml`` declaring ``A`` is read before
    ``a.yaml`` declaring ``B``. The composed path, not the bare identity, is the
    key because it keeps whole-path order (``REQ-A-1`` before ``REQ-A``, group
    ``body-ctrl`` before ``body``) wherever each element sits in the file named
    after it. The composer drops an empty group segment, so a flat
    ``swc/Foo.yaml`` sorts among the group directories, not in front of them.

    A suffixed file sorts by the name it would have taken (``Float32~1.yaml``
    declaring ``Float32`` sorts as ``Float32.yaml``), and so does any file not
    named after its element. Between equal keys (one identity declared twice)
    the sort is stable, so the listing order decides.

    The sort is here and not in :func:`load_layer` because ``_read_state`` (the
    snapshot every protocol read and the differ use) bypasses ``load_layer``.

    Inside a :func:`single_read` block the first answer is held, and each caller
    gets a list of its own over the same elements.
    """
    held = _SINGLE_READ.get()
    if held is None:
        return _parse_layer(model_root, layer, registry)
    key = (Path(os.path.abspath(model_root)).as_posix(), layer, registry)
    answer = held.get(key)
    if answer is None:
        answer = held[key] = _parse_layer(model_root, layer, registry)
    present, parsed = answer
    return present, list(parsed)


def _parse_layer(
    model_root: str | Path, layer: str, registry: ModelRegistry
) -> tuple[bool, list[tuple[Any, Mapping[str, str]]]]:
    """:func:`load_layer_with_placement`'s answer read off the disk, whatever block is open."""
    facts = registry.layer(layer)
    # A retired layout is a whole-model condition, refused once by each caller before this runs.
    scan = scan_layer(model_root, layer, registry=registry)
    # Built per call, deliberately: Pydantic caches the compiled schema on the bound class, so this costs
    # microseconds, and an adapter cache would go stale when a bound class (or a nested member) recompiles.
    adapter = TypeAdapter(facts.element_annotation)
    label = parse_label(facts)
    root = Path(model_root)
    parsed = [
        (parse_element(entry.path, label, adapter.validate_python, root), entry.placement) for entry in scan.files
    ]
    record = registry.storage(layer)
    identity_field = facts.identity_field
    nesting = record.nesting if record.is_recursive else None

    def order_key(pair: tuple[Any, Mapping[str, str]]) -> str:
        # A file directly in a recursive layer's directory has no placement value; the composer refuses an
        # absent one, so it is defaulted to empty here.
        placement = None if nesting is None else {nesting: str(pair[1].get(nesting, ""))}
        return compose_storage_path(record, str(getattr(pair[0], identity_field)), placement)

    parsed.sort(key=order_key)
    return scan.present, parsed


def load_model_state(
    model_root: str | Path, *, registry: ModelRegistry | None = None, rev: str | None = None
) -> ModelState:
    """The whole model, layer by layer, in the registry's declared order.

    Args:
        model_root: the model directory (the working-tree location, even when
            *rev* is given — the revision is materialized beside it).
        registry: the kernel registry to read the layers from (default: the
            context's).
        rev: ``None`` reads *model_root* directly, with no git involved. A git
            revision string materializes that revision into a temporary
            directory first (:func:`materialize_revision`) and reads the same
            layers from there; the temporary tree is removed before returning.

    Raises:
        FileNotFoundError: *model_root* does not exist
            (:class:`ModelRootNotFoundError`).
        ValueError: the revision starts with ``-``, names no git repository, or
            fails to archive or extract (all from
            :func:`materialize_revision`).
        ElementParseError: a model file that does not parse or does not
            validate, naming it.
        RetiredStorageError: the model keeps an element file in a directory
            the contract retired (:func:`refuse_retired_storage`); with *rev*,
            the sentence names that revision.
    """
    registry = registry or current_registry()
    if rev is None:
        return _read_state(model_root, registry)

    tmpdir, extracted = materialize_revision(model_root, rev)
    try:
        # So the refusal names the revision rather than seeming to blame the working tree.
        return _read_state(extracted, registry, revision=rev)
    finally:
        tmpdir.cleanup()


def _read_state(model_root: str | Path, registry: ModelRegistry, *, revision: str | None = None) -> ModelState:
    root = Path(model_root)
    if not root.exists():
        raise ModelRootNotFoundError(root)
    refuse_retired_storage(root, registry=registry, revision=revision)

    layers: dict[str, list[Any]] = {}
    placement: dict[str, dict[str, dict[str, str]]] = {}
    absent: list[str] = []
    for name in registry.layer_names:
        facts = registry.layer(name)
        present, parsed = load_layer_with_placement(root, name, registry)
        layers[name] = [element for element, _ in parsed]
        if not present:
            absent.append(name)
        if facts.storage.is_recursive:
            # Last wins on a duplicate identity, which only a model failing `check-model` holds.
            placement[name] = {str(getattr(element, facts.identity_field)): dict(values) for element, values in parsed}
    return ModelState(layers=layers, placement=placement, absent=frozenset(absent))


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------


def reject_option_like_revision(rev: str) -> None:
    """Refuse a revision string git would parse as an *option*.

    A value starting with ``-`` is argv injection, and *rev* can be
    LLM-supplied: ``git archive --output=<path>`` **truncates ``<path>`` to zero
    bytes** before exiting non-zero. The argv's ``--end-of-options`` is a second,
    independent guard. No git revision may begin with ``-``, so nothing
    legitimate is refused.
    """
    if rev.startswith("-"):
        raise ValueError(
            f"invalid git revision '{rev}': a revision may not start with '-', which git would read as an option"
        )


#: How much of an argument a refusal echoes: the refusal must fit the MCP response budget whatever was asked.
ECHOED_ARGUMENT_CHARACTERS = 64


def echoed_argument(argument: str) -> str:
    """*argument* as a refusal echoes it, within the response budget.

    Whole up to :data:`ECHOED_ARGUMENT_CHARACTERS`, else its opening and a marker saying how much is
    left out.
    """
    if len(argument) <= ECHOED_ARGUMENT_CHARACTERS:
        return argument
    rest = len(argument) - ECHOED_ARGUMENT_CHARACTERS
    return f"{argument[:ECHOED_ARGUMENT_CHARACTERS]}[... {rest} more characters]"


def git_environment() -> dict[str, str]:
    """The environment of every git call that reads a revision: this process's, with git in the C locale.

    A caller that recognises a cause by git's text (*not a valid object name*) then reads the same text
    under a translated git.
    """
    return {**os.environ, "LC_ALL": "C"}


def materialize_revision(model_root: str | Path, rev: str) -> tuple[tempfile.TemporaryDirectory, str]:
    """Extract `rev` into a temp dir via `git archive` + stdlib tarfile.

    Returns the TemporaryDirectory context manager object (not yet exited) so
    the caller controls its lifetime; callers must use it as a context
    manager or call .cleanup() explicitly.

    Raises:
        ValueError: the revision starts with ``-``.
        RevisionReadError: git or ``tarfile`` refused; their text is held for a
            local terminal and is in no shared form.
        FileNotFoundError, NotADirectoryError: git is not installed, or the
            model root is not on disk. Raised by the first git call alone; a later
            refusal to start ``git archive`` is a ``RevisionReadError``.
    """
    reject_option_like_revision(rev)
    git_env = git_environment()

    root_result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=model_root,
        env=git_env,
        # Inside the MCP server stdin is the JSON-RPC pipe, and a child inheriting it hangs on Windows;
        # neither git call reads stdin.
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if root_result.returncode != 0:
        raise RevisionReadError(
            "not a git repository",
            "the model folder is in no git repository this process can read, so it has no revisions to compare",
            root_result.stderr,
        )
    repo_root = root_result.stdout.strip()

    relative_model_root = os.path.relpath(model_root, repo_root)

    try:
        archive_result = subprocess.run(
            # `--end-of-options` (git 2.24+): `rev` cannot become an option even if the guard above is bypassed.
            ["git", "archive", "--end-of-options", rev],
            cwd=repo_root,
            env=git_env,
            stdin=subprocess.DEVNULL,  # see the note on the rev-parse call above
            capture_output=True,
        )
    except OSError as exc:
        # `rev-parse` just ran, so a refusal to start git here is the revision's length: past 32,767
        # characters on Windows (error 206, a FileNotFoundError that would read as a missing model root),
        # past the argument limit elsewhere (E2BIG).
        raise RevisionReadError(
            f"revision '{echoed_argument(rev)}' could not be read",
            "git could not be started with it, which a revision longer than the system's command line causes",
            f"[Errno {exc.errno}] {exc.strerror}",
        ) from exc
    if archive_result.returncode != 0:
        stderr = archive_result.stderr.decode("utf-8", errors="replace")
        raise RevisionReadError(
            f"git archive failed for revision '{rev}'",
            "git could not read this revision, which may be an unknown name or a damaged repository",
            stderr,
        )

    tmpdir = tempfile.TemporaryDirectory()
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_result.stdout), mode="r|") as tar:
            # filter="data" (PEP 706) rejects absolute paths, `..`, special files and links pointing
            # outside the extraction directory, so a corrupt archive cannot escape tmpdir.name.
            tar.extractall(tmpdir.name, filter="data")
    except tarfile.TarError as exc:
        tmpdir.cleanup()
        raise RevisionReadError(
            f"failed to extract archive for revision '{rev}'",
            "the revision's files could not be unpacked safely",
            str(exc),
        ) from exc

    return tmpdir, os.path.join(tmpdir.name, relative_model_root)


class ModelRootNotFoundError(FileNotFoundError):
    """A model root that is not on disk, naming no location unless a local terminal asks for it.

    Every reader that refuses a missing model root raises this one class. The
    refusal reaches MCP clients, whose host already knows the project, and a
    revision's root is a temporary extraction, so ``str()`` names no location;
    :meth:`render` with ``full_path=True`` names the root for a local terminal.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = root
        super().__init__(self.render())

    def render(self, *, full_path: bool = False) -> str:
        """The refusal, naming no location, or the root itself for a local terminal."""
        return f"Model root not found: {self.root if full_path else 'the model root does not exist'}"


class RevisionReadError(ValueError):
    """A revision git or ``tarfile`` refused, carrying their text for a local terminal alone.

    Git's and ``tarfile``'s text can name a location on the host, so ``str()`` is
    this module's own sentence and ``render(full_path=True)`` gives the tool's
    text for a local terminal; the words before the colon are the same in both.

    What this does not reach: the shared form gives up git's wording of the
    cause, so a client told *may be an unknown name or a damaged repository*
    cannot tell which; and git not being installed is a ``FileNotFoundError``,
    not this class.
    """

    def __init__(self, opening: str, reason: str, tool_text: str) -> None:
        self.opening, self.reason, self.tool_text = opening, reason, tool_text
        super().__init__(self.render())

    def render(self, *, full_path: bool = False) -> str:
        """The refusal in this module's words, or with the tool's own text for a local terminal."""
        return f"{self.opening}: {self.tool_text if full_path else self.reason}"


class StorageDirectoryRefusal(NotADirectoryError):
    """A layer's storage directory whose name is held by something that is not a directory.

    A **regular file** at ``tasks``, or at ``swc/<group>``, is neither a listable
    directory nor an absent one. The sentence names the layer and the
    model-root-relative storage name and no absolute path, so it needs no
    :meth:`render` twin. It keeps the OS's ``NotADirectoryError`` lineage: the
    condition is a fact about the tree, and ``except OSError`` around a write
    still catches it.

    **What it does not reach.**

    - **Windows is unmeasured.** On POSIX, listing a regular file is ``ENOTDIR``
      whether the file is the last component or one above it. Windows may answer
      ``FileNotFoundError`` or another ``OSError``; then the absent-directory
      answer is given and the writer's own ``mkdir`` refuses a moment later, so
      no element is written either way.
    - **It is decided where the name is chosen, not where the file is written.**
      A store that becomes a file between the two fails at the write, with
      whatever the filesystem says.
    - **The editing protocol does not catch it**, so a change set staged against
      a shadow whose layer directory is a file fails as an unexpected error.
    - **The reader is silent about the same tree.** :func:`scan_layer`'s glob
      answers an empty layer for a file at ``tasks``, so such a project loads,
      with that layer empty, and is refused only when something writes to it.
    """

    def __init__(self, layer: str, storage_name: str) -> None:
        self.layer, self.storage_name = layer, storage_name
        super().__init__(
            f"Layer '{layer}' cannot be stored at '{storage_name}': that name, or one above it inside the model "
            "root, is held by something that is not a directory"
        )


# ---------------------------------------------------------------------------
# A refusal's details, one per field
# ---------------------------------------------------------------------------

#: The one sentence for the errors one place collects from two or more members of
#: an undiscriminated union (a runnable's ``events``, the scalar smart union
#: ``InitValue``): each member's own complaint would be a line per member, and
#: none of them is the user's mistake.
NO_FORM_MATCHES = "matches none of the forms this field allows"

_Loc = tuple[str | int, ...]


def _one_line(text: str) -> str:
    """*text* with every run of whitespace, newlines included, made one space."""
    return " ".join(text.split())


#: The escapes a key's control character is written with; any other is ``\\xHH`` or ``\\uHHHH``.
_NAMED_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escaped(key: str) -> str:
    """*key* with every control or line-separating character written as its escape.

    A quoted YAML key can hold a newline (``"bad\\nkey"``); written raw, it would
    split a field's line in two, and the terminal and the editor would show the
    key's first half without the message. ``\\n``, ``\\r`` and ``\\t`` are named;
    any other control character (Unicode's ``Cc``: U+0000-U+001F, U+007F-U+009F)
    and the line and paragraph separators U+2028 and U+2029 are ``\\xHH``
    below U+0100 and ``\\uHHHH`` above, as ``repr`` writes them. What this does not
    reach: a backslash is kept as it is, so a key spelled with a literal ``\\n``
    reads the same as one holding a newline.
    """
    return "".join(
        _NAMED_ESCAPES.get(char, f"\\x{ord(char):02x}" if ord(char) < 0x100 else f"\\u{ord(char):04x}")
        if ord(char) < 0x20 or 0x7F <= ord(char) < 0xA0 or char in "\u2028\u2029"
        else char
        for char in key
    )


def _render_loc(loc: _Loc) -> str:
    """``ports[0].interface``: keys joined by ``.``, each :func:`_escaped`; indices as ``[i]``."""
    text = ""
    for step in loc:
        text += f"[{step}]" if isinstance(step, int) else (f".{_escaped(step)}" if text else _escaped(step))
    return text


def _walk_loc(error: Mapping[str, Any], document: object) -> tuple[_Loc, list[tuple[_Loc, str | int]]]:
    """*error*'s ``loc`` split into the steps *document* holds and the union-member tags dropped.

    A step is kept when it names a key or an index the parsed document holds at
    that point, or when it is the last step of a ``missing`` error (the field is
    absent, so it cannot be present). Every other step is a member's tag, dropped
    with the place it was met at; so is a step equal to the ``kind`` of the
    mapping it sits in, even when that mapping also holds a key of that name.
    """
    loc, kept, dropped, at = error["loc"], [], [], document
    for index, step in enumerate(loc):
        if isinstance(at, Mapping) and step != at.get("kind") and step in at:
            kept.append(step)
            at = at[step]
        elif isinstance(at, list) and isinstance(step, int) and 0 <= step < len(at):
            kept.append(step)
            at = at[step]
        elif error["type"] == "missing" and index == len(loc) - 1:
            kept.append(step)
        else:
            dropped.append((tuple(kept), step))
    return tuple(kept), dropped


def _validation_error_details(exc: ValidationError, document: object) -> tuple[str, ...]:
    """One ``<field>: <msg>`` per Pydantic error, or ``<msg>`` alone for an empty field.

    The tags :func:`_walk_loc` drops are counted per place: errors at a place
    that collects two or more tags are the one :data:`NO_FORM_MATCHES` line for
    that place (the outermost such place), and errors from one tag keep their own
    lines. Identical lines are written once, in first-seen order. The message is
    Pydantic's ``msg`` made one line; never the input value, the type or the URL.
    What this does not reach: a mapping that holds a key spelled like a member's
    tag keeps that step, so the errors under it read as that key's.
    """
    errors = exc.errors(include_url=False, include_input=False)
    walked = [_walk_loc(error, document) for error in errors]
    tags_at: dict[_Loc, set[str | int]] = {}
    for _kept, dropped in walked:
        for place, tag in dropped:
            tags_at.setdefault(place, set()).add(tag)
    lines: dict[str, None] = {}
    for error, (kept, dropped) in zip(errors, walked, strict=True):
        union = next((place for place, _tag in dropped if len(tags_at[place]) > 1), None)
        field, message = (kept, _one_line(error["msg"])) if union is None else (union, NO_FORM_MATCHES)
        lines[f"{_render_loc(field)}: {message}" if field else message] = None
    return tuple(lines)


def _load_error_details(exc: ValidationError | yaml.YAMLError, document: object) -> tuple[str, ...]:
    """The details of one file's refusal: one per field, or the YAML parser's one, placed by line and column.

    *document* is what ``load_yaml_document`` returned, ``None`` when the YAML did
    not parse. A YAML error with a ``problem_mark`` is ``line L, column C:
    <problem>``, 1-based; one without is its text's first line.
    """
    if isinstance(exc, ValidationError):
        return _validation_error_details(exc, document)
    mark, problem = getattr(exc, "problem_mark", None), getattr(exc, "problem", None)
    if mark is not None and problem:
        return (f"line {mark.line + 1}, column {mark.column + 1}: {_one_line(problem)}",)
    text = str(exc).strip()
    return (_one_line(text.splitlines()[0]) if text else type(exc).__name__,)
