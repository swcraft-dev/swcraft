"""ModelSnapshot — the per-layer attribute view of a :class:`ModelState`.

A compatibility adapter: :mod:`swcraft_tools.model_state` reads a model into a
layer-keyed :class:`~swcraft_tools.model_state.ModelState`; this module gives
that state one attribute per layer (``compositions``, ``swcs``, ``interfaces``
...) and a ``swc_placement`` map beside them.

**Where the fields come from.** :class:`ModelSnapshot` has one field per layer
that the binding's ``snapshotAttributes`` section gives an attribute, in that
section's key order, each annotated ``list[<the layer's element type>]``, then
``swc_placement``. The order is a backward-compatibility surface read
positionally, deliberately **not** the contract's ``displayOrder``. A multi-kind
layer is annotated with the union alias ``schema.py`` publishes for it, or the
plain union of its bound classes. A layer with no ``snapshotAttributes`` row
gets no field.

**The state behind the fields.** :meth:`ModelSnapshot.from_state` keeps the
whole ``ModelState`` on the instance as ``_state``, outside the dataclass fields
(absent from ``fields()``, ``repr`` and comparison), so a layer with no
attribute is still answered by :attr:`ModelSnapshot.state`.
``dataclasses.replace()`` does **not** copy it: a replaced snapshot rebuilds its
state from its fields and drops any layer that has none, which is why
``editing/derive.py`` composes a ``ModelState`` instead of replacing a snapshot.

``load_snapshot`` is ``from_state`` over ``load_model_state``, with its own
refusals for a model root or revision it cannot read.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Self, TypeVar

from swcraft_tools.model_registry import LayerFacts, ModelRegistry, current_registry
from swcraft_tools.model_state import ModelState, load_model_state, materialize_revision, refuse_retired_storage
from swcraft_tools.schema import (
    ApplicationDataType,
    ArrayDataType,
    BaseDataType,
    Composition,
    CompuMethod,
    Connector,
    DataConstr,
    EnumDataType,
    ExternalDataType,
    Hardware,
    ImplementationArrayDataType,
    ImplementationRecordDataType,
    Interface,
    MappingSet,
    PointerDataType,
    RecordDataType,
    ReferenceDataType,
    Requirement,
    Sequence,
    SWComponent,
    Task,
    Unit,
    ValueDataType,
)

__all__ = ["ModelSnapshot", "load_snapshot"]


#: The git-revision materializer, under the name this module uses for it.
_materialize_revision = materialize_revision

_S = TypeVar("_S", bound="ModelSnapshot")


def _nesting_layer(registry: ModelRegistry) -> LayerFacts | None:
    """The one layer whose directory encodes placement, or ``None``."""
    for name in registry.layer_names:
        facts = registry.layer(name)
        if facts.storage.is_recursive:
            return facts
    return None


def _from_state(cls: type[_S], state: ModelState, *, registry: ModelRegistry | None = None) -> _S:
    """*state*, in the per-layer attribute shape, carrying *state* itself beside it."""
    registry = registry or current_registry()
    # `_SNAPSHOT_ATTRIBUTES` is defined after `ModelSnapshot`, from its fields.
    values: dict[str, Any] = {attribute: [] for attribute in _SNAPSHOT_ATTRIBUTES}
    for name in state.layer_names:
        try:
            facts = registry.layer(name)
        except KeyError:
            # An undeclared layer stays reachable through `.state`; `check-model` owns what it means.
            continue
        if facts.snapshot_attribute in values:
            values[facts.snapshot_attribute] = state[name]
    nesting = _nesting_layer(registry)
    placement = {} if nesting is None else {key: dict(value) for key, value in state.placement_of(nesting.name).items()}
    snapshot = cls(**values, swc_placement=placement)
    # `_state` must stay outside `fields()`, so the class cannot declare it, and
    # a plain assignment to an undeclared attribute is a type error.
    setattr(snapshot, "_state", state)
    return snapshot


def _state_of(self: ModelSnapshot) -> ModelState:
    """The state behind the attributes — the carried one, or a built one.

    A built state gives ``swc_placement`` back to the one layer
    :func:`_nesting_layer` picks; it carries no placement for any other nesting
    layer and reports every layer it carries as present, because the attributes
    record neither.
    """
    carried: ModelState | None = getattr(self, "_state", None)
    if carried is not None:
        return carried
    registry = current_registry()
    nesting = _nesting_layer(registry)
    layers: dict[str, list[Any]] = {}
    placement: dict[str, dict[str, dict[str, str]]] = {}
    for name in registry.layer_names:
        facts = registry.layer(name)
        if facts.snapshot_attribute not in _SNAPSHOT_ATTRIBUTES:
            continue
        layers[name] = getattr(self, facts.snapshot_attribute)
        if nesting is not None and name == nesting.name:
            placement[name] = {key: dict(value) for key, value in self.swc_placement.items()}
    return ModelState(layers=layers, placement=placement)


@dataclass
class ModelSnapshot:
    """The entire model at one point in time, one attribute per layer.

    Built by :meth:`from_state` (what :func:`load_snapshot` does), or by keyword
    or position with one list per layer field; ``swc_placement`` defaults to an
    empty map.

    :attr:`swc_placement` is placement metadata, not model content: the group
    directory of each software component, keyed by its declared identity.
    ``SWComponent`` has no ``group`` field, because it would be a second
    representation that can disagree with the directory. The differ does not
    compare it: ``group`` is immutable, so it appears only inside an added or
    removed element.

    :attr:`state` is the carried state for a snapshot built by
    :meth:`from_state`. One built by keyword or ``dataclasses.replace()`` builds
    a state from its fields and loses what they do not record: a layer with no
    snapshot attribute, :attr:`ModelState.absent`, and the placement of any
    nesting layer beyond the first.
    """

    compositions: list[Composition]
    swcs: list[SWComponent]
    interfaces: list[Interface]
    connectors: list[Connector]
    requirements: list[Requirement]
    tasks: list[Task]
    sequences: list[Sequence]
    hardware: list[Hardware]
    mapping_sets: list[MappingSet]
    application_types: list[EnumDataType | RecordDataType | ArrayDataType | ApplicationDataType]
    implementation_types: list[
        ValueDataType
        | ReferenceDataType
        | ExternalDataType
        | ImplementationRecordDataType
        | ImplementationArrayDataType
        | PointerDataType
    ]
    base_types: list[BaseDataType]
    units: list[Unit]
    compu_methods: list[CompuMethod]
    data_constraints: list[DataConstr]
    swc_placement: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def from_state(cls, state: ModelState, *, registry: ModelRegistry | None = None) -> Self:
        """*state* in the attribute shape, carrying *state* itself (default registry: the current one)."""
        return _from_state(cls, state, registry=registry)

    @property
    def state(self) -> ModelState:
        """The carried state, or one built from the fields."""
        return _state_of(self)


#: The layer attribute names, in field order: every field but ``swc_placement``.
_SNAPSHOT_ATTRIBUTES: tuple[str, ...] = tuple(f.name for f in fields(ModelSnapshot) if f.name != "swc_placement")


def load_snapshot(model_root: str, rev: str | None = None) -> ModelSnapshot:
    """Load the whole model at a point in time.

    Args:
        model_root: path to the model directory (working tree location).
        rev: None reads model_root directly (working tree, no git involved).
            A git revision string (e.g. "HEAD", "HEAD~3", a branch/tag/sha)
            materializes that revision via `git archive` into a temporary
            directory first, then reads from there.

    Raises:
        ValueError: model_root does not exist, is not inside a git
            repository, the revision starts with ``-`` (git would read it as
            an option), the revision is unknown to git, the archive stream
            fails to extract, or the requested revision predates model_root's
            existence in the repository (e.g. before a directory move).

    The two modes are not delegated to ``load_model_state(..., rev=...)``
    because a missing model root and a revision that predates it are the same
    ``FileNotFoundError`` from the loader and two different messages here.
    """
    if rev is None:
        try:
            return ModelSnapshot.from_state(load_model_state(model_root))
        except FileNotFoundError as exc:
            raise SnapshotReadError(exc, model_root=model_root) from exc

    try:
        tmpdir, extracted_model_root = _materialize_revision(model_root, rev)
    except (FileNotFoundError, NotADirectoryError) as exc:
        # A missing cwd is FileNotFoundError on POSIX and NotADirectoryError (WinError 267) on Windows.
        raise SnapshotReadError(exc, model_root=model_root) from exc

    try:
        # Before the loader, so the refusal names the revision rather than seeming to blame the working tree.
        refuse_retired_storage(extracted_model_root, revision=rev)
        return ModelSnapshot.from_state(load_model_state(extracted_model_root))
    except FileNotFoundError as exc:
        # The revision may predate model_root at its current path (a directory move in history).
        raise SnapshotReadError(exc, model_root=model_root, revision=rev) from exc
    finally:
        tmpdir.cleanup()


class SnapshotReadError(ValueError):
    """A model :func:`load_snapshot` could not read, naming no location unless a local terminal asks.

    The refusal reaches MCP clients, whose host already knows the project, so
    ``str()`` names no location; :meth:`render` with ``full_path=True`` is the
    form for a local terminal. A cause that renders itself
    (:class:`~swcraft_tools.model_state.ModelRootNotFoundError`) is asked for the
    same form; any other ``OSError`` is named by its errno and reason alone.
    """

    def __init__(self, cause: BaseException, *, model_root: str, revision: str | None = None) -> None:
        self.cause, self.model_root, self.revision = cause, model_root, revision
        super().__init__(self.render())

    def render(self, *, full_path: bool = False) -> str:
        """The refusal, naming no location, or every location it knows for a local terminal."""
        render = getattr(self.cause, "render", None)
        if callable(render):
            detail = render(full_path=full_path)
            if self.revision is None:
                # The cause's sentence already says the model root was not found.
                return detail
        elif full_path or not isinstance(self.cause, OSError) or self.cause.errno is None:
            detail = str(self.cause)
        else:
            detail = f"[Errno {self.cause.errno}] {self.cause.strerror}"
        if self.revision is None:
            return f"model root not found: {detail}"
        where = f"model root '{self.model_root}'" if full_path else "the model root"
        return f"revision '{self.revision}' does not contain {where}: {detail}"
