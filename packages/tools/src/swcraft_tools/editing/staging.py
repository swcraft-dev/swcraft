"""The baseline revision token, unconditional shadow staging, and `stale-baseline`.

**The token** is an opaque, version-prefixed value; equality is its only
operation. Computed fresh, never cached; raising the version prefix retires every
token in flight.

**Every change set gets a shadow, scoped or not.** :func:`stage` fingerprints the
real model, then copies it.

**One optimistic token, checked twice**, with no lock: by
:meth:`StagedChangeSet.require_baseline` before pass B and by
:meth:`StagedChangeSet.commit` immediately before the apply, both against the one
fingerprint taken at open. A stale result is never re-based.

**Materialization writes real files** into the shadow, because pass D validates
from disk; ``dist/`` is left untouched. Commit and abandon are host-side acts,
never protocol operations; ``abandon`` is idempotent.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from pydantic import BaseModel

from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.apply import TouchedElement, element_kind_of
from swcraft_tools.editing.read import publish_element
from swcraft_tools.editing.renames import ElementRenames, element_rename_pairs
from swcraft_tools.model_registry import ModelRegistry, compose_storage_path
from swcraft_tools.model_state import (
    LayerFile,
    StorageNameRefusal,
    free_storage_path,
    stored_element_files,
)
from swcraft_tools.session import CommitConflictError, ScopedSession, model_fingerprint
from swcraft_tools.snapshot import ModelSnapshot

# One-way dependency: `writer/elements.py` imports nothing from `editing/`, which
# keeps the two packages acyclic.
from swcraft_tools.writer.elements import (
    EditedText,
    TargetExistsError,
    TargetMissingError,
    edited_text,
    remove_element,
    stands_in_writer_form,
    undeclared_keys_of,
    write_element,
)

__all__ = [
    "STALE_BASELINE",
    "TOKEN_VERSION",
    "PlacementRefusal",
    "RewrittenFile",
    "StagedChangeSet",
    "StaleBaselineError",
    "TargetOccupied",
    "TargetVanished",
    "baseline_token",
    "stage",
    "token_for_fingerprint",
]

#: The token's version prefix. Bumped whenever the derivation changes, so an old
#: token reads as a moved model.
TOKEN_VERSION = 1

#: Domain separation from any other sha256 over the same fingerprint.
_TOKEN_DOMAIN = b"swcraft/editing/baseline/v1"

#: 16 bytes -> 22 base64url characters, within ``$defs/revisionToken``'s bound.
#: Truncation is fine: the token is an equality handle, not a verified digest.
_TOKEN_BYTES = 16

#: Outside the four-pass detection order: the request may be entirely correct
#: and only the model moved, hence its own failure class (``stale``).
STALE_BASELINE = "stale-baseline"


@dataclass(frozen=True)
class RewrittenFile:
    """One element whose file loses what the delta cannot show: comments, layout, undeclared keys.

    Two cases: an updated (or renamed) element whose file could not be edited
    line by line and is written whole; and a removed element left unpaired by the
    rename rule beside a creation in its layer, whose file is not in the writer's
    form, so what it held beyond its values is not carried.

    It names the element, never the file, since it reaches the review text.
    *reason* is a fixed sentence holding no model text beyond an identity.
    """

    layer: str
    identity: str
    reason: str
    #: The identity the element had before a rename, or ``None``.
    renamed_from: str | None = None
    #: The undeclared keys a whole write drops, in document order. Raw model
    #: text: the review flattens each.
    dropped_keys: tuple[str, ...] = ()
    #: ``True`` for a removed element: the review gives it its own sentence and
    #: leaves it out of the count of files rewritten whole.
    removed: bool = False

    def __post_init__(self) -> None:
        # Three cases on two fields: a removed file was never renamed, or it would be the rename's row.
        assert not (self.removed and self.renamed_from is not None), "a removed file carries no rename"


#: The reason for a removed file beside a creation the rename rule did not pair it with.
_NOT_CARRIED = "removed beside a created element; its comments and layout are not carried"

#: The reason where exactly one unpaired creation shares the removed element's layer.
_NOT_A_PURE_RENAME = "not a pure rename to {}: " + _NOT_CARRIED

#: The reason for a rename whose old file could not be read as UTF-8, so the new
#: file is written whole.
_SOURCE_UNREADABLE = "the renamed element's old file could not be read as text"


@dataclass(frozen=True)
class _RenameSource:
    """What a rename's create carries from the removed element: its identity, value and text.

    *text* is the old file's text before the removal, line ends kept, or ``None``
    where it could not be read as UTF-8.
    """

    was: str
    before: Mapping[str, Any]
    text: str | None


class StaleBaselineError(RuntimeError):
    """The token a change set carries no longer describes the real model.

    Raised by :meth:`StagedChangeSet.require_baseline` and
    :meth:`StagedChangeSet.commit`; in both cases the real model is
    byte-identical. A typed exception, since mapping it to a protocol code
    belongs to the layer that owns the wire shapes. The message carries no fresh
    token: the client re-reads one from ``discover``.
    """


class PlacementRefusal(ValueError):
    """A value the registry cannot compose into a path component.

    Pass B refuses such values when a client writes them, so this catches the
    residue: a hand-authored baseline element whose identity or placement is not
    a single segment (``group: ""``, ``group: "a/b"``, identity ``Sys..A``),
    notably on a ``remove``, which never reaches pass B's payload validation.

    ``field`` is ``None`` when the offending value is the identity; the engine
    then answers at ``{layer, element}`` with no path. A :class:`ValueError`
    subclass, so existing ``except ValueError`` handlers keep catching it.
    """

    def __init__(self, layer: str, element: str, field: str | None, message: str) -> None:
        super().__init__(message)
        self.layer = layer
        self.element = element
        self.field = field


class TargetOccupied(ValueError):
    """A ``create`` whose plain file name is held by a file declaring **that same identity**.

    A file held by another element is no refusal: the create takes the nearest
    free collision suffix. This is the fail-closed twin of pass B's
    ``identity-conflict``, unreachable from a well-formed request; a create never
    silently becomes an overwrite, so the engine answers ``identity-conflict`` at
    ``{layer, element}``. The writer's ``TargetExistsError`` (a file appearing
    between the listing and the write) is translated into the same refusal.

    The message names the layer and the identity, never the writer's path:
    storage never crosses the protocol boundary.
    """

    def __init__(self, layer: str, element: str, message: str) -> None:
        super().__init__(message)
        self.layer = layer
        self.element = element


class TargetVanished(ValueError):
    """An element being re-written or removed that no file of its layer declares.

    The element is found by content (:func:`_locate`); re-writing one no file
    declares would create it anew, and removing it has nothing to remove.
    Reachable only through an engine defect or a file that changed under the
    change set; the engine answers ``consistency-violation`` at the element.
    Asked before the first write, of the elements re-written or removed.
    ``remove_element``'s ``TargetMissingError`` is translated into the same refusal.
    """

    def __init__(self, layer: str, element: str, message: str) -> None:
        super().__init__(message)
        self.layer = layer
        self.element = element


def token_for_fingerprint(fingerprint: str) -> str:
    """One :func:`~swcraft_tools.session.model_fingerprint` value framed as a token.

    Lets a caller that already holds a fingerprint frame that one instead of
    sampling the model a second time.
    """
    digest = hashlib.sha256(_TOKEN_DOMAIN + b"\0" + fingerprint.encode("utf-8")).digest()
    body = base64.urlsafe_b64encode(digest[:_TOKEN_BYTES]).decode("ascii").rstrip("=")
    return f"rev{TOKEN_VERSION}:{body}"


def baseline_token(model_root: str | Path) -> str:
    """The opaque ``rev<N>:<body>`` baseline revision token for the model at *model_root*.

    Byte-identical trees give the same token; any change under *model_root*, even
    to a file no reader parses, gives a different one.

    Raises:
        FileNotFoundError: *model_root* does not exist or is not a directory.
    """
    return token_for_fingerprint(model_fingerprint(str(model_root)))


def stage(manifest_path: str | Path) -> StagedChangeSet:
    """A shadow opened for one change set: step 1 of the lifecycle.

    Returns:
        An open :class:`StagedChangeSet`. It is a context manager, and every
        exit path drops the temporary tree.

    Raises:
        FileNotFoundError: no manifest at *manifest_path*, or the model root it
            names does not exist.
    """
    session = ScopedSession(str(manifest_path))
    session.open()
    return StagedChangeSet(session)


class StagedChangeSet:
    """One change set's shadow: the baseline it was taken against, and the copy.

    Created by :func:`stage`; a session that is not open has no baseline.
    """

    def __init__(self, session: ScopedSession) -> None:
        self._session = session
        self._rewritten: tuple[RewrittenFile, ...] = ()
        self._renames: tuple[tuple[str, str, str], ...] = ()
        self._rename_sources: dict[tuple[str, str], _RenameSource] = {}

    # -- the baseline ---------------------------------------------------

    @property
    def session(self) -> ScopedSession:
        """The underlying session: its shadow audit log (rebased into the real log at commit) and ``scope_id``."""
        return self._session

    @property
    def baseline(self) -> str:
        """The token of the model state this shadow was taken from, framed from the fingerprint ``open()`` read."""
        return token_for_fingerprint(self._session.baseline_fingerprint)

    @property
    def snapshot(self) -> ModelSnapshot:
        """The baseline snapshot, read from the copy."""
        return self._session.baseline_snapshot

    @property
    def model_root(self) -> str:
        """The shadow's model root — the ``model_root`` pass D runs against."""
        return self._session.shadow_model_root

    @property
    def rewritten(self) -> tuple[RewrittenFile, ...]:
        """The files :meth:`materialize` changed at a cost the delta cannot show.

        First the removed elements left unpaired beside a creation, in removal
        order; then the updated and renamed elements written whole, in write
        order. A file already in the writer's form is left out. Empty until
        materialization.
        """
        return self._rewritten

    @property
    def renames(self) -> tuple[tuple[str, str, str], ...]:
        """The whole-element renames :meth:`materialize` found, as ``(layer, was, now)``.

        Decided by :func:`_rename_pairs`, the same rule the safety guard uses.
        Empty until materialization.
        """
        return self._renames

    # -- the two token checks -------------------------------------------

    def require_baseline(self, claimed: str) -> None:
        """The fail-fast check: refuse a change set taken against a moved model.

        Raises:
            StaleBaselineError: the model moved. Nothing has been staged into
                the shadow and the real model is byte-identical.
        """
        if claimed != self.baseline:
            raise StaleBaselineError(
                "the baseline token this change set carries no longer describes the model: another writer "
                "committed a change after it was read. Nothing was applied and the model is byte-identical. "
                "The staged result is deliberately NOT re-based onto the moved model — that would produce a "
                "delta nobody reviewed — so re-read a fresh token with `discover`, re-author the change set "
                "against it, and have the delta reviewed again"
            )

    def commit(self, diff: Any, *, before_apply: Callable[[], None] | None = None) -> list[str]:
        """The shadow applied onto the real model, a moved baseline refused.

        *before_apply* is called after every pre-apply refusal, immediately before
        the first ``os.replace()``. Returns the real-model paths written or removed.

        Raises:
            StaleBaselineError: the real model moved after this shadow's baseline
                was taken; nothing was applied, and *before_apply* was never called.
        """
        try:
            return self._session.commit(diff, before_apply=before_apply)
        except CommitConflictError as conflict:
            raise StaleBaselineError(
                f"the model moved between staging and commit, so this change set was refused whole ({conflict}). "
                "Nothing was applied and the model is byte-identical. Re-read a fresh token with `discover` and "
                "re-author the change set: a staged result is never re-based onto a moved model"
            ) from conflict

    def abandon(self) -> None:
        """The temporary tree dropped. Idempotent; the real model is byte-identical."""
        self._session.abandon()

    def __enter__(self) -> StagedChangeSet:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.abandon()

    # -- materialization ------------------------------------------------

    def materialize(
        self,
        touched: Sequence[TouchedElement],
        *,
        registry: reg.Registry | None = None,
        auditor: Any = None,
    ) -> list[str]:
        """Every touched element written into the shadow tree.

        An update, and a rename's create, change only their lines or are written
        whole and recorded in :attr:`rewritten`. Removals come first, so the
        writer's reserved-name guards judge the outcome.

        Args:
            touched: the elements whose staged value differs from the baseline.
            registry: the model description placing each element; a projection
                without a kernel registry raises ``TypeError``.
            auditor: passed to the writers, one entry per touched element.

        Returns:
            The shadow paths removed or written, removals first; ``[]`` when no
            element moved.

        Raises:
            PlacementRefusal: answered as ``schema-violation``.
            TargetOccupied: answered as ``identity-conflict``.
            TargetVanished: answered as ``consistency-violation``.
            ReservedNameError: a name the ARXML exporter generates for itself,
                answered as pass D's ``consistency-violation``.
            ValueError: any other writer refusal, chiefly an illegal emitted
                ``SHORT-NAME``, answered as ``invalid-identifier``.
        """
        registry = reg.or_current(registry)
        if not touched:
            return []
        # The writer resolves the ambient registry itself; bind this projection's
        # so elements are placed and serialized under one contract.
        with reg.kernel_context(registry):
            shadow = Path(self.model_root)
            kernel = _kernel_registry(registry)
            # Value questions before storage questions: a refused value is the
            # answer even where another element's file has vanished.
            removals: list[tuple[TouchedElement, dict[str, Any]]] = []
            writes: list[tuple[BaseModel, TouchedElement, dict[str, Any]]] = []
            for element in touched:
                layer = registry.layer(element.layer)
                if element.after is None:
                    if element.before is None:  # pragma: no cover - a touched element moved, so one side is present
                        continue
                    removals.append((element, _composable_placement(registry, layer, element.before)))
                    continue
                placement = _composable_placement(registry, layer, element.after)
                writes.append((_instantiate(registry, layer, element.after), element, placement))

            # Files of removed and re-written elements, found before the first
            # write; no create lands on an existing element's file, so the index
            # cannot go stale.
            stored: dict[str, dict[str, list[LayerFile]]] = {}

            def locate(element: TouchedElement, placement: Mapping[str, Any]) -> str:
                return _locate(registry, element, placement, shadow=shadow, kernel=kernel, stored=stored)

            removed = [(locate(element, placement), element) for element, placement in removals]
            located = [
                (model, element, placement, None if element.before is None else locate(element, placement))
                for model, element, placement in writes
            ]
            # Renames, and the old files' text, read before the removals delete them.
            renames = _rename_pairs(touched, registry)
            self._renames = renames.pairs
            sources = {(layer, was): (layer, now) for layer, was, now in renames.pairs}
            beside = _unpaired_beside_a_creation(renames)
            self._rename_sources = {}
            whole: list[RewrittenFile] = []
            for target, element in removed:
                key = (element.layer, element.identity)
                if key not in sources and key not in beside:
                    continue
                text = _stored_text(shadow / target)
                assert element.before is not None  # a removal has a `before`
                if key in sources:
                    self._rename_sources[sources[key]] = _RenameSource(element.identity, element.before, text)
                    continue
                layer = registry.layer(element.layer)
                was = _instantiate(registry, layer, element.before)
                if text is None or not stands_in_writer_form(text, was):
                    creations = beside[key]
                    reason = _NOT_A_PURE_RENAME.format(creations[0]) if len(creations) == 1 else _NOT_CARRIED
                    dropped = undeclared_keys_of(text, was)
                    whole.append(
                        RewrittenFile(element.layer, element.identity, reason, dropped_keys=dropped, removed=True)
                    )
            paths = [_remove_one(self.model_root, target, auditor, element) for target, element in removed]
            for model, element, placement, write_target in located:
                if write_target is None:
                    write_target = locate(element, placement)
                source = self._rename_sources.get((element.layer, element.identity)) if element.before is None else None
                path, row = _write_one(self.model_root, write_target, model, auditor, element, registry, renamed=source)
                paths.append(path)
                if row is not None:
                    whole.append(row)
            self._rewritten = tuple(whole)
            return paths


def _rename_pairs(touched: Sequence[TouchedElement], registry: reg.Registry) -> ElementRenames:
    """Which removed and created elements of *touched* are one rename.

    Asked of :func:`~swcraft_tools.editing.renames.element_rename_pairs`, as the
    safety guard does, over the published values the delta carries: neither the
    client's JSON (``1`` vs ``1.0``) nor a writer dict (``init: 0`` vs
    ``init: false``) is that value.
    """
    removed: list[tuple[str, str, Any]] = []
    created: list[tuple[str, str, Any]] = []
    for element in touched:
        if (element.before is None) == (element.after is None):
            continue  # an update, which no rename pairs
        value = element.after if element.before is None else element.before
        assert value is not None  # exactly one side is present
        layer = registry.layer(element.layer)
        placement = {name: value[name] for name in _placement_names(layer) if name in value}
        triple = (
            element.layer,
            element.identity,
            publish_element(_instantiate(registry, layer, value), registry, placement=placement or None),
        )
        (created if element.before is None else removed).append(triple)
    return element_rename_pairs(removed, created)


def _unpaired_beside_a_creation(renames: ElementRenames) -> dict[tuple[str, str], tuple[str, ...]]:
    """Every unpaired removal, keyed to the unpaired creations of its layer; a plain removal is not here."""
    creations: dict[str, list[str]] = {}
    for layer, identity in renames.unpaired_creations:
        creations.setdefault(layer, []).append(identity)
    return {
        (layer, identity): tuple(creations[layer])
        for layer, identity in renames.unpaired_removals
        if layer in creations
    }


def _stored_text(path: Path) -> str | None:
    """A stored element file's text, line ends as they stand, or ``None`` where it cannot be read as UTF-8.

    Decoded from bytes, as ``edited_text`` reads, so no newline translation happens.
    """
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _kernel_registry(registry: reg.Registry) -> ModelRegistry:
    """The projection's kernel registry, which file allocation and lookup need; never the ambient one."""
    kernel = registry.kernel_model
    if kernel is None:
        raise TypeError(
            "materialization needs this projection's kernel ModelRegistry to find and allocate element files, "
            "and the projection has none: that is an engine defect, and the ambient registry is never "
            "substituted for it"
        )
    return kernel


def _write_one(
    model_root: str,
    target: str,
    model: BaseModel,
    auditor: Any,
    element: TouchedElement,
    registry: reg.Registry,
    *,
    renamed: _RenameSource | None = None,
) -> tuple[str, RewrittenFile | None]:
    """One :func:`write_element`, with its target-exists refusal given an identity; its path, and its row.

    Only this frame knows which element the path was for; the writer's message
    is not reused because it names the target, which is storage.

    An update takes the text :func:`edited_text` computes; a file that gets none
    yields a :class:`RewrittenFile` row unless it already stands in the writer's
    form. ``prior`` is a callable so the baseline's validation runs inside that
    function's guard. A rename's create (*renamed*) passes the removed element's
    text as ``source_text``, so the new file is the old one with its identity
    line changed; a fallback row names the old identity. Every other create is a
    whole write.
    """
    before = element.before
    edited = None
    row = None
    layer = registry.layer(element.layer)
    if before is not None:
        edited = edited_text(model_root, target, model, lambda: _instantiate(registry, layer, before))
    elif renamed is not None:
        if renamed.text is None:
            edited = EditedText(None, _SOURCE_UNREADABLE)
        else:
            edited = edited_text(
                model_root,
                target,
                model,
                lambda: _instantiate(registry, layer, renamed.before),
                source_text=renamed.text,
            )
    if edited is not None and edited.text is None and not edited.in_writer_form:
        assert edited.reason is not None  # `edited_text` gives a reason with no text.
        row = RewrittenFile(
            element.layer,
            element.identity,
            edited.reason,
            renamed_from=renamed.was if renamed is not None else None,
            dropped_keys=edited.dropped_keys,
        )
    try:
        return write_element(model_root, target, model, auditor, create=before is None, edited=edited), row
    except TargetExistsError as occupied:
        raise TargetOccupied(
            element.layer,
            element.identity,
            f"'{element.identity}' cannot be created in layer '{element.layer}': the file the store allocated for it "
            "was taken before it could be written. A create never silently becomes an overwrite; nothing was "
            "committed, so re-stage the change set",
        ) from occupied


def _remove_one(model_root: str, target: str, auditor: Any, element: TouchedElement) -> str:
    """One :func:`remove_element`, with its target-missing refusal given an identity.

    The address is handed to the writer so the audit entry records which element
    the file held; a file name need not be the identity.
    """
    try:
        return remove_element(model_root, target, auditor, address=(element.layer, element.identity))
    except TargetMissingError as vanished:
        raise TargetVanished(
            element.layer,
            element.identity,
            _vanished_message(element.layer, element.identity, "remove"),
        ) from vanished


def _vanished_message(layer: str, identity: str, verb: str) -> str:
    """:class:`TargetVanished`'s one sentence, composed from the address and never from a path."""
    act = "no file to remove it from" if verb == "remove" else "no file to re-write it into"
    return (
        f"the model declares '{identity}' in layer '{layer}', but no file of that layer declares it, so there is "
        f"{act} — and writing a new one would create the element rather than edit it. The model changed under "
        "the staged change set or is inconsistent: re-read it, resolve the model if needed, and re-author the "
        "change set"
    )


def _placement_names(layer: reg.LayerDescriptor) -> frozenset[str]:
    """Every placement field any of *layer*'s kinds declares (today: ``swc.group``)."""
    return frozenset(
        descriptor.name for kind in layer.element_kinds for descriptor in kind.fields if descriptor.placement
    )


def _composable_placement(
    registry: reg.Registry, layer: reg.LayerDescriptor, element: Mapping[str, Any]
) -> dict[str, Any]:
    """The placement values of one published element, once its two client values are known to compose.

    The identity and each placement value are asked about by name, so a refusal
    is reported at the right place, mirroring ``apply._check_path_values``: at
    ``{layer, element}`` for the identity, at the field for a placement value.
    This also covers a ``remove``, whose identity never passes through pass B.
    The identity question is :meth:`~reg.Registry.identity_refusal`, which also
    checks the file name the layer's suffix composes (``A.`` -> ``A..yaml``).

    Raises:
        PlacementRefusal: the identity, or a placement value, is missing or is
            not a single path segment.
    """
    names = _placement_names(layer)
    placement = {name: element[name] for name in names if name in element}
    identity = element[layer.identity_field]
    refused_identity = registry.identity_refusal(layer.name, identity)
    if refused_identity is not None:
        # The identity is not a field, so the engine answers at {layer, element}.
        raise PlacementRefusal(layer.name, str(identity), None, refused_identity)
    for name in sorted(names):
        if name not in placement:
            raise PlacementRefusal(
                layer.name, str(identity), name, f"layer '{layer.name}' needs a '{name}' placement value"
            )
        refused = reg.segment_refusal(placement[name], f"{layer.name} {name}", directory=True)
        if refused is not None:
            raise PlacementRefusal(layer.name, str(identity), name, refused)
    return placement


def _locate(
    registry: reg.Registry,
    element: TouchedElement,
    placement: Mapping[str, Any],
    *,
    shadow: Path,
    kernel: ModelRegistry,
    stored: dict[str, dict[str, list[LayerFile]]],
) -> str:
    """The model-root-relative file one touched element is written to or removed from.

    A ``create`` takes the plain file name when free, else the nearest free ``~N``,
    as the shadow stands at this call. An ``update`` or ``remove`` acts on the file
    of the layer that declares the element (the composed plain name preferred,
    else the first in listing order); *stored* is filled on first use. The answer
    is never published.

    Raises:
        PlacementRefusal: a collision suffix that does not fit (``field=None``).
        TargetOccupied: a create's file already declares this identity.
        TargetVanished: no file declares an updated or removed element.
        RuntimeError: *element* has neither a before nor an after, which no diff builds.
    """
    layer = registry.layer(element.layer)
    published = element.before if element.after is None else element.after
    if published is None:
        raise RuntimeError(f"a touched element has a before or an after; {element.identity!r} has neither")
    identity = published[layer.identity_field]
    if element.before is None:
        try:
            free = free_storage_path(shadow, layer.name, identity, placement, registry=kernel)
        except StorageNameRefusal as refused:
            # Not relayed: the kernel's sentence spells the suffix, a fragment of a file name.
            raise PlacementRefusal(
                layer.name,
                identity,
                None,
                f"'{identity}' cannot be stored in layer '{layer.name}': the file name composed from it is held by "
                "another element, and the identity is too long to take the collision suffix that would give it a "
                "file of its own within the file-name bound. Choose a shorter identity",
            ) from refused
        if free.occupied_by_self:
            raise TargetOccupied(
                layer.name,
                identity,
                f"'{identity}' is already declared by a file of layer '{layer.name}', although the model this change "
                "set was staged against does not declare it, so creating it would overwrite that element. A create "
                "never silently becomes an overwrite: pick a different identity, or re-read the model and re-author "
                "the change set",
            )
        return free.path
    index = stored.get(layer.name)
    if index is None:
        index = stored[layer.name] = stored_element_files(shadow, layer.name, registry=kernel)
    declaring = [entry.path.relative_to(shadow).as_posix() for entry in index.get(identity, ())]
    if not declaring:
        verb = "remove" if element.after is None else "update"
        raise TargetVanished(layer.name, identity, _vanished_message(layer.name, identity, verb))
    composed = compose_storage_path(kernel.storage(layer.name), identity, placement)
    return composed if composed in declaring else declaring[0]


def _instantiate(registry: reg.Registry, layer: reg.LayerDescriptor, element: Mapping[str, Any]) -> BaseModel:
    """One published element back as its own Pydantic class.

    ``write_element`` refuses a dict, so validation happens here. Placement
    values are stripped first: they are not model fields.
    """
    kind = element_kind_of(layer, element, registry=registry)
    placement = _placement_names(layer)
    payload = {name: value for name, value in element.items() if name not in placement}
    return kind.model.model_validate(payload)
