"""The kernel model registry: the contract projected into runtime facts.

:mod:`swcraft_tools.contract` reads the normative JSON files; this module
resolves every declaration through ``binding.json`` into the Python objects a
consumer needs (each kind's bound Pydantic class, each layer's element
annotation, the storage record a path is composed from) and publishes them as
one :class:`ModelRegistry`. It is a leaf outside ``editing/`` because no module
of ``swcraft_tools`` outside that package may import ``editing``, while the
consumers needing these facts sit outside it; ``editing/registry.py`` consumes
this module, never the reverse.

:func:`current_registry` reads a :class:`~contextvars.ContextVar` whose default
is the shipped contract's registry; :func:`use_registry` swaps it for a block.
A helper taking an optional ``registry: ModelRegistry | None = None`` resolves
``registry or current_registry()`` **in its body**, since a default-argument
expression would pin the production registry at definition time; a few take it
keyword-only and required (``model_state.free_storage_path``).

:meth:`ModelRegistry.compose_path` validates nothing about the segments, on
purpose: ``writer/mutations.py``'s public ``create_*`` functions accept segments
the editing protocol refuses (a bare ``.`` group, a control-character group),
and a guarded composer would tighten their input validation. The segment rules
are stated as questions by :func:`segment_refusal` and :func:`path_refusal`;
nothing here raises on them.

This module does not validate the contract against ``schema.py`` (a test-time
conformance gate), does not publish the editing protocol's wire descriptors, and
does not read a model file (:mod:`swcraft_tools.model_state` is the only layer
traversal in the generic core).
"""

from __future__ import annotations

import contextlib
import functools
import importlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Annotated, Any, Literal, Union, get_args

from pydantic import BaseModel
from pydantic import Field as PydanticField

from swcraft_tools.contract import load_binding, load_contract

__all__ = [
    "CollectionFacts",
    "FieldFacts",
    "KindFacts",
    "LayerFacts",
    "MemberReferenceEdge",
    "MemberReferenceTarget",
    "ModelRegistry",
    "ModelRegistryError",
    "ReferenceEdge",
    "RelationEdge",
    "RetiredStorage",
    "StorageRecord",
    "build_model_registry",
    "complete_composite_identity",
    "compose_storage_path",
    "current_registry",
    "longest_directory_match",
    "path_refusal",
    "production_registry",
    "reserved_device_name",
    "reserved_device_name_refusal",
    "segment_refusal",
    "use_registry",
]


class ModelRegistryError(RuntimeError):
    """The registry could not be built from the contract plus its binding.

    Raised for a declaration the binding does not resolve: an unbound element
    kind, a bound class that is not importable, a multi-kind layer whose kinds
    disagree about their discriminator. Each is a defect in the normative files or
    the binding, never something a client can provoke.
    """


# ---------------------------------------------------------------------------
# The projected records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageRecord:
    """The layer's storage mapping, **never published and never accepted**.

    ``nesting`` is ``"flat"`` for every layer but ``swc``, where it names the
    placement field: the one layer that nests one level.
    """

    directory: str
    nesting: str
    file_suffix: str
    filename_from: str

    FLAT = "flat"

    @property
    def is_recursive(self) -> bool:
        return self.nesting != StorageRecord.FLAT


@dataclass(frozen=True)
class RetiredStorage:
    """A directory an earlier contract stored element files in, and where they belong now.

    The contract's ``retiredStorage`` table (contract ``8.0.0``): ``directory`` is
    model-root-relative, ``since`` the contract version from which a file there is
    refused, ``moved_to`` the layer directories its files belong in, in the order a
    refusal names them, and ``file_suffix`` those layers' element file suffix. It is
    contract data because the kernel may not spell a layer's name.
    """

    directory: str
    since: str
    moved_to: tuple[str, ...]
    file_suffix: str


def compose_storage_path(record: StorageRecord, identity: str, placement: Mapping[str, str] | None = None) -> str:
    """``(storage record, identity, placement)`` -> a model-root-relative path.

    Pure composition with no inverse: no segment is validated (:func:`path_refusal`
    states the rules). The one refusal is a nesting record with no value for its
    placement field, a caller defect rather than a client input.

    The answer is spelled ``as_posix()`` and parsed as one ``PurePosixPath`` over
    the joined string: a host ``Path`` would read a backslash as a separator on
    Windows, and a per-part join would re-root on a part that parses as absolute
    (``/abs``, ``C:``). An empty or ``.`` placement value contributes no segment
    (``swc/Foo.yaml``, never ``swc//Foo.yaml``). ``..``, a control character and an
    over-long segment pass through unchanged.

    A free function rather than a registry method, because ``editing/registry.py``
    builds a ``Registry`` from perturbed storage records that belong to no
    :class:`ModelRegistry` and must still delegate the composition to here.

    Raises:
        ModelRegistryError: a nesting record with no placement value.
    """
    parts = [record.directory]
    if record.is_recursive:
        values = placement or {}
        if record.nesting not in values:
            raise ModelRegistryError(f"storage under '{record.directory}' needs a '{record.nesting}' placement value")
        parts.append(str(values[record.nesting]))
    parts.append(f"{identity}{record.file_suffix}")
    return PurePosixPath("/".join(parts)).as_posix()


def complete_composite_identity(identity: Any, identity_fields: Sequence[str], optional: Sequence[str]) -> Any:
    """A composite identity object with every *optional* identity field it leaves out spelled ``None``.

    One spelling for one member (contract ``9.0.0``): ``{"swc": "A", "runnable":
    "Run"}`` and ``{"swc": "A", "instance": null, "runnable": "Run"}`` name the same
    task entry. Only an object whose keys are a subset of *identity_fields* and
    which lacks nothing but *optional* fields
    (:meth:`ModelRegistry.optional_identity_fields`) is completed; anything else is
    returned as written (the same object), and resolving it is what refuses it.
    """
    if not isinstance(identity, Mapping):
        return identity
    absent = [name for name in identity_fields if name not in identity]
    if not absent or not set(identity) <= set(identity_fields) or not set(absent) <= set(optional):
        return identity
    return {name: identity.get(name) for name in identity_fields}


def longest_directory_match(directories: Iterable[str], relative_path: str) -> tuple[str, str] | None:
    """``(directory, remainder)`` for the longest of *directories* that prefixes *relative_path*.

    Matched segment by segment over a model-root-relative POSIX path:
    ``data_types/base/Float32.yaml`` against ``data_types`` and ``data_types/base``
    is ``("data_types/base", "Float32.yaml")``. A layer directory may have two
    segments, so the first segment alone does not name a layer.

    It answers the directory only; the caller applies its storage record's depth
    test to the remainder. ``None`` when no directory prefixes the path or nothing
    is left below it. The comparison is case-sensitive, as the readers' globs are.
    """
    segments = relative_path.split("/")
    best: tuple[str, str] | None = None
    best_depth = 0
    for directory in directories:
        prefix = directory.split("/")
        depth = len(prefix)
        if depth <= best_depth or depth >= len(segments) or segments[:depth] != prefix:
            continue
        best, best_depth = (directory, "/".join(segments[depth:])), depth
    return best


# ---------------------------------------------------------------------------
# The segment policy: what a storage record can and cannot hold
# ---------------------------------------------------------------------------
#
# These rules are functions of a `StorageRecord` (its `file_suffix` and
# `nesting`), so they are contract knowledge. `compose_storage_path` does not
# ask them: the public writers accept inputs they refuse (a bare `.` group, a
# control-character group, an over-long group).

_ILLEGAL_SEGMENT_CHARS = ("/", "\\", "\x00")

#: What every mainstream filesystem caps one path component at, in UTF-8 bytes.
_MAX_COMPONENT_BYTES = 255

#: What a *file name* adds to the segment it is composed from, **exactly**:
#: ``.yaml`` plus, during a commit, the ``.<32 hex>.swcraft-commit.tmp`` staging
#: suffix. Not rounded up: a ``Requirement.id`` is held to no AUTOSAR
#: ``Identifier`` rule, so a reserve with headroom would refuse legal stored data.
_FILE_NAME_OVERHEAD_BYTES = len(".yaml") + len(".") + 32 + len(".swcraft-commit.tmp")

#: The longest an identity may be, in UTF-8 bytes: what a path component leaves
#: after the file-name overhead. Checked before staging, so an over-long identity
#: is refused instead of raising ``OSError`` in the middle of a commit.
_MAX_SEGMENT_BYTES = _MAX_COMPONENT_BYTES - _FILE_NAME_OVERHEAD_BYTES

#: The longest a **placement** value may be: the whole component, because it
#: becomes a *directory* (``swc/{group}/``) with no extension or staging suffix.
#: Values already on disk are asked this, so a tighter bound would refuse a
#: project that reads fine.
_MAX_DIRECTORY_SEGMENT_BYTES = _MAX_COMPONENT_BYTES

#: The device names Windows reserves (contract ``7.4.0``,
#: ``rule:storage.reserved-device-name``). No file whose name before the first
#: ``.`` is one of these, in any case, can exist on a Windows volume: ``CON.yaml``
#: opens the console and ``NUL.yaml`` discards what is written to it.
_RESERVED_DEVICE_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{digit}" for digit in range(1, 10)),
        *(f"LPT{digit}" for digit in range(1, 10)),
    }
)

#: The same set as a reader is shown it, each run of numbered names folded into
#: a range; every sentence listing the set joins these.
DEVICE_NAME_SPELLINGS: tuple[str, ...] = ("CON", "PRN", "AUX", "NUL", "COM1-COM9", "LPT1-LPT9")

_RESERVED_DEVICE_NAME_RULE = "rule:storage.reserved-device-name"


def _segment_shape_error(value: Any, label: str) -> str | None:
    """Why *value* is not shaped like a single path segment, or ``None`` when it is.

    Everything :func:`segment_refusal` checks **except the byte bound**:
    :data:`_MAX_SEGMENT_BYTES` already reserves the file name's suffix, so asking it
    again of ``identity + '.yaml'`` would refuse the last bytes of a legal range.
    """
    if not isinstance(value, str):
        return f"{label} must be a string, not {type(value).__name__}"
    if not value or not value.strip():
        return f"{label} must not be empty"
    if value in (".", ".."):
        return f"{label} '{value}' is not a legal path segment"
    if ".." in value or any(char in value for char in _ILLEGAL_SEGMENT_CHARS):
        return f"{label} '{value}' must not contain path separators, '..' or NUL"
    if not value.isprintable():
        # `str.isprintable` refuses every "Other" and "Separator" code point except
        # the ASCII space: `\n`, `\t`, the escape introducer, the Unicode line separators.
        return (
            f"{label} {value!r} must not contain control characters or line separators: it becomes a file "
            "name, and it is the value the review boundary shows the human"
        )
    return None


def reserved_device_name(name: str) -> str | None:
    """The Windows device name *name*'s base name is, upper-cased, or ``None``.

    The base name is the part before the first ``.``, so ``con``, ``Con.v2`` and
    ``NUL.yaml`` answer ``CON``, ``CON`` and ``NUL``, and ``CONSOLE``, ``COM10``,
    ``NULL``, ``A.CON`` and ``CON~1`` answer ``None``.

    **What it does not reach.** Windows also treats a name with a trailing space
    before the extension (``CON .yaml``) and the superscript digits after ``COM``
    and ``LPT`` as device names; neither is asked, and a non-ASCII base name answers
    ``None``. The answer does not depend on the host: macOS and Linux refuse what
    Windows cannot hold.
    """
    base = name.split(".", 1)[0]
    if not base.isascii():
        return None
    upper = base.upper()
    return upper if upper in _RESERVED_DEVICE_NAMES else None


def reserved_device_name_refusal(value: str, label: str) -> str | None:
    """The one sentence a reserved device name is refused with, or ``None``."""
    device = reserved_device_name(value)
    if device is None:
        return None
    return (
        f"{label} '{value}' names a device Windows reserves ({device}): no file whose name before the first '.' "
        f"is {', '.join(DEVICE_NAME_SPELLINGS[:-1])} or {DEVICE_NAME_SPELLINGS[-1]}, in any case, "
        "can exist on Windows, so the element cannot be stored there, and one already stored under such a name is "
        "renamed by hand "
        f"({_RESERVED_DEVICE_NAME_RULE})"
    )


def segment_refusal(value: Any, label: str, *, directory: bool = False) -> str | None:
    """Why *value* is not one legal path segment, or ``None`` when it is.

    Refused: a separator, ``..``, an empty value, a bare ``.``, a NUL or any other
    control character (a segment becomes a file name and is the value the review
    boundary shows), and a length the filesystem could not hold. It is not the
    AUTOSAR ``Identifier`` rule; a group reaches no ``SHORT-NAME``.

    A **file** segment whose base name is a Windows reserved device name is refused
    on every platform (contract ``7.4.0``). A *directory* segment is not asked, so a
    stored group such as ``swc/con/`` does not turn a readable project into a
    refused whole-layer read.

    Args:
        value: the candidate segment.
        label: what to call it in the refusal.
        directory: the segment becomes a directory, so the bound is the whole path
            component (:data:`_MAX_DIRECTORY_SEGMENT_BYTES`) and no device name is
            asked about. Every placement value passes this; nothing else does.
    """
    shape = _segment_shape_error(value, label)
    if shape is not None:
        return shape
    assert isinstance(value, str)  # `_segment_shape_error` returned None, so it is one
    if not directory:
        device = reserved_device_name_refusal(value, label)
        if device is not None:
            return device
    encoded = len(value.encode("utf-8"))
    allowed = _MAX_DIRECTORY_SEGMENT_BYTES if directory else _MAX_SEGMENT_BYTES
    if encoded > allowed:
        composed = (
            "A placement value names a directory, which every filesystem caps at one component"
            if directory
            else "A segment names a file, and the composed name carries an extension and a staging suffix on top of it"
        )
        return f"{label} is {encoded} bytes long; at most {allowed} are allowed. {composed}"
    return None


def path_refusal(
    record: StorageRecord,
    identity: Any,
    placement: Mapping[str, Any] | None = None,
    *,
    layer: str,
) -> str | None:
    """Why *record* cannot store *identity* at *placement*, or ``None`` when it can.

    The identity must be a single path segment, and so must the **file name** the
    record's ``file_suffix`` composes from it, because that name reaches the
    filesystem: ``A.`` is a legal identity and segment, but ``A..yaml`` is refused
    by the writer.

    Args:
        record: the layer's storage mapping.
        identity: the candidate identity: the element's ``name``, or a
            requirement's ``id``.
        placement: the placement values, or ``None`` to ask about the identity
            alone. A mapping, empty included, means a whole path is about to be
            composed, so a nesting record with no placement value is refused.
        layer: the contract layer name, used to label the refusal.
    """
    refusal = segment_refusal(identity, f"{layer} identity")
    if refusal is not None:
        return refusal
    composed = _segment_shape_error(f"{identity}{record.file_suffix}", "the composed file name")
    if composed is not None:
        return (
            f"{layer} identity '{identity}' is a legal segment, but {composed}. The '{record.file_suffix}' "
            "suffix is appended after the identity itself is checked, so an identity that is a legal segment "
            "can still compose a file name that is not — and the writer refuses the composed target"
        )
    if placement is None or not record.is_recursive:
        return None
    if record.nesting not in placement:
        return f"layer '{layer}' needs a '{record.nesting}' placement value"
    return segment_refusal(placement[record.nesting], f"{layer} {record.nesting}", directory=True)


@dataclass(frozen=True)
class PlacementFacts:
    """A declared field whose persisted form is a component of the element's location.

    There is exactly one (``swc.group``). It has no ``model_fields`` counterpart by
    design, so every comparison between the contract and ``schema.py`` excludes it.
    """

    layer: str
    kind: str
    field: str
    type: str
    description: str


@dataclass(frozen=True)
class ReferenceEdge:
    """A field whose value identifies an element of another layer, by that layer's identity."""

    field_id: str
    source_layer: str
    source_kind: str
    source_field: str
    target_layer: str
    target_identity_field: str


@dataclass(frozen=True)
class RelationEdge:
    """A field that names an element of another layer by something other than its identity.

    A relation names the target *kind* and the target *field* the resolved key is
    matched against. That field may be something other than the target's identity,
    and then every element sharing the value answers. Every shipped relation
    targets an identity field, but this class does not hold that:
    ``editor_schema.references_artifact`` refuses a non-identity relation.

    ``resolver`` is bound to the source field: ``(source_element) -> Iterable[target
    key]``. It is **unconditional**: it answers from the source element alone,
    whether or not any element answers to a key.
    """

    field_id: str
    source_layer: str
    source_kind: str
    source_field: str
    target_layer: str
    target_kind: str
    target_field: str
    resolution: str
    direction: str
    resolver: Callable[[Any], Iterable[Any]]
    summary: str | None = None


@dataclass(frozen=True)
class MemberReferenceTarget:
    """One collection a :class:`MemberReferenceEdge` may find its member in.

    ``identity_field`` is the member identity field for a ``field`` collection, and
    ``None`` for a ``whole_value`` list of scalars, whose item *is* the value
    compared.
    """

    collection_id: str
    layer: str
    kind: str
    field: str
    identity_field: str | None


@dataclass(frozen=True)
class MemberReferenceEdge:
    """A field whose value names a **member** of a declared collection (contract ``10.1.0``).

    Unlike a :class:`RelationEdge`, it is no dependency edge and no traversal walks
    it. Without ``via`` the collection is on the element holding the field (a
    runnable's ``port`` names one of its component's ``ports``); with ``via`` it is
    on the element reached through the sibling field ``via`` names, whose member
    kind carries exactly one ``reference`` into the collection's layer (an access's
    ``element`` is a data element of its port's interface). No resolver is bound:
    the declaration is read as data.
    """

    field_id: str
    source_layer: str
    source_kind: str
    source_field: str
    collections: tuple[MemberReferenceTarget, ...]
    via: str | None = None
    summary: str | None = None


#: How a collection's member is identified: by one field, by several, or by its whole value.
MemberIdentity = Literal["field", "composite", "whole_value"]
MEMBER_IDENTITIES: tuple[MemberIdentity, ...] = get_args(MemberIdentity)


@dataclass(frozen=True)
class CollectionFacts:
    """A nested list or map, and how one member of it is identified.

    ``collection_id`` is the public ``collection:`` identifier a member
    reference lists it by.
    """

    field_id: str
    collection_id: str
    kind: str
    layer: str
    field: str
    collection_kind: str
    member_identity: MemberIdentity
    member_kinds: tuple[str, ...] = ()
    discriminator_field: str | None = None
    identity_field: str | None = None
    identity_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldFacts:
    """One declared field of one element kind, under its serialization alias.

    ``omit_at_default`` (contract ``1.3.0``, ``fieldDecl.omitAtDefault``): the field
    is left out of the written model file while it holds its declared default,
    although that default is outside the ``None`` / ``""`` / ``{}`` rule applied to
    every other field. Absent, it is ``False``: the field is written at its default.
    """

    id: str
    layer: str
    kind: str
    name: str
    type: str
    required: bool
    immutable: bool
    placement: bool = False
    description: str | None = None
    unit: str | None = None
    enum: tuple[Any, ...] = ()
    element_kind: str | None = None
    reference: ReferenceEdge | None = None
    relation: RelationEdge | None = None
    member_reference: MemberReferenceEdge | None = None
    collection: CollectionFacts | None = None
    has_default: bool = False
    default: Any = None
    omit_at_default: bool = False


@dataclass(frozen=True)
class KindFacts:
    """One element kind of one layer, with the class ``binding.json`` binds it to."""

    id: str
    kind: str
    layer: str
    model: type[BaseModel]
    fields: tuple[FieldFacts, ...]
    member_only: bool = False
    discriminator_field: str | None = None
    member_of: tuple[tuple[str, str], ...] = ()
    description: str | None = None

    def get_field(self, name: str) -> FieldFacts | None:
        for facts in self.fields:
            if facts.name == name:
                return facts
        return None

    @property
    def file_fields(self) -> tuple[FieldFacts, ...]:
        """The fields a file of this kind may hold a key for: :attr:`fields` without the placement fields.

        In contract order. A placement field has no key in the file (``group`` is the
        component's directory).
        """
        return tuple(field for field in self.fields if not field.placement)


@dataclass(frozen=True)
class LayerFacts:
    """Everything a total consumer needs to know about one layer.

    ``snapshot_attribute`` is ``None`` for a layer the binding gives no
    ``snapshotAttributes`` row, a layer with no field on the
    ``ModelSnapshot``/``ModelDiff`` compatibility adapters; that is not an error.

    ``label`` and ``display_order`` are presentation facts (contract ``1.1.0``);
    undeclared, the label is the layer's name and the layer sorts after every
    declared one.

    ``token`` (contract ``1.4.0``) is what ONE element of the layer is called
    (``swc``, ``data_type``, ``requirement``), as against ``name``, the plural, and
    ``label``, the rendered heading. Undeclared, it is the layer's ``name``
    (:func:`_layer_token`), never a stripped plural.

    ``arxml_bound`` (contract ``1.2.0``) says whether the layer's names are emitted
    as AUTOSAR ``SHORT-NAME``s and so subject to the Identifier rule; undeclared, it
    is ``False``, so no AUTOSAR constraint is invented for the layer.
    """

    id: str
    name: str
    optional: bool
    identity_field: str
    storage: StorageRecord
    kinds: tuple[KindFacts, ...]
    element_annotation: Any
    token: str
    placement: PlacementFacts | None = None
    snapshot_attribute: str | None = None
    description: str | None = None
    absent_description: str | None = None
    label: str | None = None
    display_order: int | None = None
    arxml_bound: bool = False

    @property
    def top_level_kinds(self) -> tuple[KindFacts, ...]:
        return tuple(kind for kind in self.kinds if not kind.member_only)

    @property
    def display_label(self) -> str:
        """The label to render, defaulting to the layer's own name."""
        return self.name if self.label is None else self.label


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class ModelRegistry:
    """The contract and its binding, resolved into the facts every total consumer needs.

    ``eq=False``: the registry is compared and cached **by identity**, so
    ``editing/registry.py`` can key its projection on the instance without hashing a
    document-sized structure.
    """

    contract_version: str
    layers: tuple[LayerFacts, ...]
    _kinds: Mapping[str, KindFacts] = field(repr=False, default_factory=dict)
    _references: tuple[ReferenceEdge, ...] = field(repr=False, default=())
    _relations: tuple[RelationEdge, ...] = field(repr=False, default=())
    _member_references: tuple[MemberReferenceEdge, ...] = field(repr=False, default=())
    _collections: tuple[CollectionFacts, ...] = field(repr=False, default=())
    _adapter_order: tuple[str, ...] = field(repr=False, default=())
    retired_storage: tuple[RetiredStorage, ...] = field(default=())

    # --- layers ------------------------------------------------------------

    @property
    def layer_names(self) -> tuple[str, ...]:
        """Every layer name, in **contract** order."""
        return tuple(layer.name for layer in self.layers)

    @property
    def adapter_order(self) -> tuple[str, ...]:
        """The layers that have a snapshot attribute, in ``binding.snapshotAttributes`` order.

        The field order of the ``ModelSnapshot``/``ModelDiff`` compatibility adapters,
        which consumers read positionally; it is not :attr:`display_order`. A layer
        without a ``snapshotAttributes`` row is absent, and a row naming an undeclared
        layer is skipped.
        """
        return self._adapter_order

    @property
    def display_order(self) -> tuple[str, ...]:
        """Every layer name in declared presentation order.

        The order of the diff output, the impact grouping and ``swcraft list``. A layer
        with no declared order sorts after every declared one, ties broken by contract
        order, so a contract that declares none yields contract order.
        """
        ordered = sorted(
            enumerate(self.layers),
            key=lambda pair: (
                pair[1].display_order is None,
                pair[1].display_order if pair[1].display_order is not None else 0,
                pair[0],
            ),
        )
        return tuple(layer.name for _, layer in ordered)

    def layer(self, name: str) -> LayerFacts:
        for facts in self.layers:
            if facts.name == name:
                return facts
        raise KeyError(name)

    def storage(self, layer: str) -> StorageRecord:
        return self.layer(layer).storage

    # --- kinds -------------------------------------------------------------

    @property
    def kinds(self) -> MappingProxyType[str, KindFacts]:
        """Every element kind by bare name, layer by layer, owners before members."""
        return MappingProxyType(dict(self._kinds))

    def kind(self, name: str) -> KindFacts:
        return self._kinds[name]

    def bound_class(self, kind: str) -> type[BaseModel]:
        """The Pydantic class ``binding.json`` binds *kind* to."""
        return self._kinds[kind].model

    def kind_of_model(self, model: type) -> KindFacts | None:
        """The element kind a Pydantic class is, or ``None`` for a class no layer declares."""
        for facts in self._kinds.values():
            if facts.model is model:
                return facts
        return None

    def field_facts(self, kind: str, name: str) -> FieldFacts | None:
        return self._kinds[kind].get_field(name)

    # --- edges and collections --------------------------------------------

    def references(self) -> tuple[ReferenceEdge, ...]:
        """Every declared ``reference``, in contract order."""
        return self._references

    def relations(self) -> tuple[RelationEdge, ...]:
        """Every declared ``relation``, in contract order, resolver bound.

        A second list beside :meth:`references`, because the two resolve differently:
        an identity lookup against the target layer, versus a resolver over the source
        element matched against a named target field.
        """
        return self._relations

    def member_references(self) -> tuple[MemberReferenceEdge, ...]:
        """Every declared ``memberReference``, in contract order (contract ``10.1.0``)."""
        return self._member_references

    def collections(self) -> tuple[CollectionFacts, ...]:
        """Every declared collection, in contract order."""
        return self._collections

    def optional_identity_fields(self, identity_fields: Sequence[str], member_kinds: Sequence[str]) -> tuple[str, ...]:
        """The *identity_fields* every one of *member_kinds* declares optional with the default ``null``.

        Contract ``9.0.0``: such a field, left out of a payload, a member address or a
        ``query --after`` cursor, reads as ``null`` (:func:`complete_composite_identity`).
        A field with any other default is not one of them (reading its absence as
        ``null`` would name a member no model holds), nor is a field of a collection with
        no member kind or of a member kind this registry does not declare.
        """
        kinds = [self._kinds.get(name) for name in member_kinds]
        if not kinds:
            return ()

        def declared_null(kind: KindFacts | None, name: str) -> bool:
            facts = kind.get_field(name) if kind is not None else None
            return facts is not None and not facts.required and facts.has_default and facts.default is None

        return tuple(name for name in identity_fields if all(declared_null(kind, name) for kind in kinds))

    # --- storage -----------------------------------------------------------

    def compose_path(self, layer: str, identity: str, placement: Mapping[str, str] | None = None) -> str:
        """``(layer, identity, placement)`` -> the model-root-relative path of that element."""
        return compose_storage_path(self.storage(layer), identity, placement)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def _resolve_class(kind_id: str, binding: Mapping[str, Any]) -> type[BaseModel]:
    """The Pydantic class one ``binding.kinds`` row names, imported by name because the binding is data."""
    try:
        module = importlib.import_module(binding["module"])
    except ImportError as failure:  # pragma: no cover - a binding naming an absent module
        raise ModelRegistryError(
            f"{kind_id}: binding names module '{binding['module']}', which does not import"
        ) from failure
    resolved = getattr(module, binding["class"], None)
    if not isinstance(resolved, type) or not issubclass(resolved, BaseModel):
        raise ModelRegistryError(
            f"{kind_id}: binding names '{binding['module']}.{binding['class']}', which is not a Pydantic model"
        )
    return resolved


def _collection_shape(field_decl: Mapping[str, Any]) -> str | None:
    """The field's declared ``type`` when the field holds many values, else ``None``.

    The declared type is returned verbatim, not normalized. A field holds many
    values when its ``type`` opens ``map[`` or ``list[`` or it declares a
    ``collection``; either alone is enough.
    """
    declared_type = field_decl["type"]
    if declared_type.startswith(("map[", "list[")) or "collection" in field_decl:
        return declared_type
    return None


def _reference_edge(field_decl: Mapping[str, Any], layer: str, kind: str) -> ReferenceEdge | None:
    """The edge one ``fieldDecl.reference`` declares, or ``None``.

    A reference resolves the field's **own value** as one key, so a collection
    field cannot carry one: it must declare a ``relation`` with its member shape
    (``map-keys``, ``list-values``). The meta-schema cannot say so, so such a
    declaration is refused here rather than building a silently empty traversal.
    """
    declared = field_decl.get("reference")
    if declared is None:
        return None
    shape = _collection_shape(field_decl)
    if shape is not None:
        raise ModelRegistryError(
            f"{field_decl['id']}: declares a reference on a {shape} field; a reference resolves "
            "the field's own value as one key, so a collection must declare a relation naming its "
            "member shape instead"
        )
    return ReferenceEdge(
        field_id=field_decl["id"],
        source_layer=layer,
        source_kind=kind,
        source_field=field_decl["name"],
        target_layer=declared["layer"],
        target_identity_field=declared["identityField"],
    )


def _resolve_resolver(field_id: str, binding: Mapping[str, Any]) -> Callable[..., Iterable[Any]]:
    """The resolver one ``binding.relations`` row names, imported by name because the binding is data."""
    try:
        module = importlib.import_module(binding["module"])
    except ImportError as failure:  # pragma: no cover - a binding naming an absent module
        raise ModelRegistryError(
            f"{field_id}: the relation binding names module '{binding['module']}', which does not import"
        ) from failure
    resolved = getattr(module, binding["callable"], None)
    if not callable(resolved):
        raise ModelRegistryError(
            f"{field_id}: the relation binding names '{binding['module']}.{binding['callable']}', which is not callable"
        )
    return resolved


def _relation_edge(
    field_decl: Mapping[str, Any],
    layer: str,
    kind: str,
    *,
    relation_bindings: Mapping[str, Any],
    attribute: str,
) -> RelationEdge | None:
    """One declared ``relation``, with its resolver bound to the source field.

    The binding row is required: a relation nothing resolves would silently not be
    walked. ``attribute`` is the *Python* spelling of the source field (it differs
    from the alias where ``binding.fieldNames`` renames it), because the resolver
    reads an attribute off an element.
    """
    declared = field_decl.get("relation")
    if declared is None:
        return None
    field_id = field_decl["id"]
    bound = relation_bindings.get(field_id)
    if bound is None:
        raise ModelRegistryError(
            f"{field_id}: the contract declares a relation and binding.json binds no resolver to it"
        )
    resolver = _resolve_resolver(field_id, bound)
    return RelationEdge(
        field_id=field_id,
        source_layer=layer,
        source_kind=kind,
        source_field=field_decl["name"],
        target_layer=declared["layer"],
        target_kind=declared["kind"],
        target_field=declared["field"],
        resolution=declared["resolution"],
        direction=declared["direction"],
        resolver=functools.partial(resolver, field=attribute),
        summary=declared.get("summary"),
    )


def _declared_collections(layer_decls: Sequence[Mapping[str, Any]]) -> dict[str, CollectionFacts]:
    """Every collection the contract declares, by its ``collection:`` identifier.

    Read before the layer walk, because a member reference may name a collection
    of a layer the contract declares later.
    """
    declared: dict[str, CollectionFacts] = {}
    for layer_decl in layer_decls:
        for kind_decl in layer_decl["kinds"]:
            for field_decl in kind_decl["fields"]:
                facts = _collection_facts(field_decl, layer_decl["name"], kind_decl["kind"])
                if facts is not None:
                    declared[facts.collection_id] = facts
    return declared


def _member_reference_edge(
    field_decl: Mapping[str, Any],
    layer: str,
    kind: str,
    *,
    declared_collections: Mapping[str, CollectionFacts],
) -> MemberReferenceEdge | None:
    """The edge one ``fieldDecl.memberReference`` declares, or ``None``.

    A listed identifier naming no declared collection is refused. The checks that
    need every kind built are :func:`_check_member_references`'.
    """
    declared = field_decl.get("memberReference")
    if declared is None:
        return None
    field_id = field_decl["id"]
    targets: list[MemberReferenceTarget] = []
    for identifier in declared["collections"]:
        collection = declared_collections.get(identifier)
        if collection is None:
            raise ModelRegistryError(
                f"{field_id}: the member reference lists '{identifier}', which names no declared collection"
            )
        targets.append(
            MemberReferenceTarget(
                collection_id=identifier,
                layer=collection.layer,
                kind=collection.kind,
                field=collection.field,
                identity_field=collection.identity_field,
            )
        )
    return MemberReferenceEdge(
        field_id=field_id,
        source_layer=layer,
        source_kind=kind,
        source_field=field_decl["name"],
        collections=tuple(targets),
        via=declared.get("via"),
        summary=declared.get("summary"),
    )


def _member_identity(declared: str, field_id: str) -> MemberIdentity:
    """*declared* as one of :data:`MEMBER_IDENTITIES`, or the refusal naming the field that declares it."""
    for identity in MEMBER_IDENTITIES:
        if identity == declared:
            return identity
    raise ModelRegistryError(f"{field_id}: memberIdentity '{declared}' is not one of {list(MEMBER_IDENTITIES)}")


def _collection_facts(field_decl: Mapping[str, Any], layer: str, kind: str) -> CollectionFacts | None:
    declared = field_decl.get("collection")
    if declared is None:
        return None
    return CollectionFacts(
        field_id=field_decl["id"],
        # The contract's collection identifier grammar (contract.schema.json): spelled here alone.
        collection_id=f"collection:{layer}.{kind}.{field_decl['name']}",
        kind=kind,
        layer=layer,
        field=field_decl["name"],
        collection_kind=declared["kind"],
        member_identity=_member_identity(declared["memberIdentity"], field_decl["id"]),
        member_kinds=tuple(declared.get("memberKinds", ())),
        discriminator_field=declared.get("discriminatorField"),
        identity_field=declared.get("identityField"),
        identity_fields=tuple(declared.get("identityFields", ())),
    )


def _field_facts(
    field_decl: Mapping[str, Any],
    layer: str,
    kind: str,
    *,
    relation_bindings: Mapping[str, Any],
    field_names: Mapping[str, str],
    declared_collections: Mapping[str, CollectionFacts],
) -> FieldFacts:
    return FieldFacts(
        id=field_decl["id"],
        layer=layer,
        kind=kind,
        name=field_decl["name"],
        type=field_decl["type"],
        required=field_decl["required"],
        immutable=field_decl["immutable"],
        placement=field_decl.get("placement", False),
        description=field_decl.get("description"),
        unit=field_decl.get("unit"),
        enum=tuple(field_decl.get("enum", ())),
        element_kind=field_decl.get("elementKind"),
        reference=_reference_edge(field_decl, layer, kind),
        relation=_relation_edge(
            field_decl,
            layer,
            kind,
            relation_bindings=relation_bindings,
            attribute=field_names.get(field_decl["id"], field_decl["name"]),
        ),
        member_reference=_member_reference_edge(field_decl, layer, kind, declared_collections=declared_collections),
        collection=_collection_facts(field_decl, layer, kind),
        has_default="default" in field_decl,
        default=field_decl.get("default"),
        omit_at_default=field_decl.get("omitAtDefault", False),
    )


def _element_annotation(layer_name: str, top_level: tuple[KindFacts, ...]) -> Any:
    """The annotation a parser validates one element of *layer_name* against.

    A single-kind layer's annotation is its bound class; a multi-kind layer's is the
    discriminated union of its kinds, synthesized here because the union is a layer
    fact and ``schema.py`` is layer-blind. A layer whose top-level kinds disagree
    about their discriminator is refused.
    """
    if not top_level:
        raise ModelRegistryError(f"layer '{layer_name}' declares no top-level element kind")
    if len(top_level) == 1:
        return top_level[0].model
    discriminators = {kind.discriminator_field for kind in top_level}
    if len(discriminators) != 1 or None in discriminators:
        raise ModelRegistryError(
            f"layer '{layer_name}' has {len(top_level)} top-level kinds declaring discriminators "
            f"{sorted(str(d) for d in discriminators)}; a discriminated union needs exactly one"
        )
    (discriminator,) = discriminators
    members = tuple(kind.model for kind in top_level)
    return Annotated[Union[members], PydanticField(discriminator=discriminator)]  # noqa: UP007


def _member_of(layer_decl: Mapping[str, Any]) -> dict[str, tuple[tuple[str, str], ...]]:
    """Every place a kind of this layer appears, as ``(owner kind, field name)`` pairs."""
    found: dict[str, list[tuple[str, str]]] = {}
    for kind_decl in layer_decl["kinds"]:
        owner = kind_decl["kind"]
        for field_decl in kind_decl["fields"]:
            members: list[str] = []
            collection = field_decl.get("collection")
            if collection is not None:
                members += list(collection.get("memberKinds", ()))
            element_kind = field_decl.get("elementKind")
            if element_kind is not None:
                members.append(element_kind)
            for member in members:
                found.setdefault(member, []).append((owner, field_decl["name"]))
    return {kind: tuple(pairs) for kind, pairs in found.items()}


def _check_display_orders(layers: list[LayerFacts]) -> None:
    """Refuse two layers claiming the same presentation position.

    A duplicate would fail no reader: :attr:`ModelRegistry.display_order`'s
    tie-break would pick one silently. Checked here because the meta-schema
    constrains one layer declaration at a time.
    """
    seen: dict[int, str] = {}
    for facts in layers:
        if facts.display_order is None:
            continue
        clash = seen.get(facts.display_order)
        if clash is not None:
            raise ModelRegistryError(
                f"layers '{clash}' and '{facts.name}' both declare displayOrder {facts.display_order}"
            )
        seen[facts.display_order] = facts.name


def _directory_key(directory: str) -> tuple[str, ...]:
    """A model-root-relative directory as the policy compares it: its segments under ``str.casefold()``."""
    return tuple(segment.casefold() for segment in directory.split("/"))


def _holds(key: tuple[str, ...], other_key: tuple[str, ...]) -> bool:
    """Whether the directory of ``key`` strictly contains that of ``other_key`` (whole segments)."""
    return len(key) < len(other_key) and other_key[: len(key)] == key


def _check_layer_declarations(layer_decls: Sequence[Mapping[str, Any]]) -> None:
    """Refuse two layers sharing a ``name``, an ``id`` or a ``storage.directory``.

    Run before any layer is walked, so a duplicated layer is reported as such. A
    duplicate of any of the three silently loses a row: one layer's elements
    overwrite another's, two layers share a snapshot attribute, or the commit gate
    checks a written file against the wrong identity field and file suffix.

    Two directories that differ **only in case** (segment by segment under
    ``str.casefold()``) are refused too, since a case-insensitive filesystem holds
    them as one; the meta-schema's lowercase pattern covers this only for a document
    it validated. A directory that is a whole-segment **prefix** of another's
    (``data_types`` beside ``data_types/base``) is refused, so a file of one layer
    never lies in another's tree and :func:`longest_directory_match` has one answer.
    """
    for attribute, label in (("name", "layer name"), ("id", "layer id")):
        seen: set[str] = set()
        for decl in layer_decls:
            value = decl[attribute]
            if value in seen:
                raise ModelRegistryError(f"duplicate {label} '{value}'")
            seen.add(value)

    directories: dict[str, str] = {}
    folded: dict[tuple[str, ...], tuple[str, str]] = {}
    for decl in layer_decls:
        directory = decl["storage"]["directory"]
        clash = directories.get(directory)
        if clash is not None:
            raise ModelRegistryError(f"layers '{clash}' and '{decl['name']}' both store elements in '{directory}'")
        directories[directory] = decl["name"]
        key = _directory_key(directory)
        twin = folded.get(key)
        if twin is not None:
            raise ModelRegistryError(
                f"layers '{twin[0]}' and '{decl['name']}' store elements in '{twin[1]}' and '{directory}', "
                "which differ only in case: a case-insensitive filesystem holds them as one directory"
            )
        folded[key] = (decl["name"], directory)

    for key, (name, directory) in folded.items():
        for other_key, (other_name, other_directory) in folded.items():
            if _holds(key, other_key):
                raise ModelRegistryError(
                    f"layer '{name}' stores elements in '{directory}', which holds '{other_directory}', the directory "
                    f"of layer '{other_name}': one layer's directory may not be a prefix of another's"
                )


def _check_retired_directory(directory: str, layers: Sequence[LayerFacts]) -> None:
    """Refuse a retired directory that is, or lies inside, a layer's directory (:func:`_retired_storage`)."""
    key = _directory_key(directory)
    for facts in layers:
        layer_directory = facts.storage.directory
        if directory == layer_directory:
            raise ModelRegistryError(
                f"retired directory '{directory}' is the storage directory of layer '{facts.name}'"
            )
        layer_key = _directory_key(layer_directory)
        if key == layer_key:
            raise ModelRegistryError(
                f"retired directory '{directory}' and '{layer_directory}', the directory of layer '{facts.name}', "
                "differ only in case: a case-insensitive filesystem holds them as one directory"
            )
        if _holds(layer_key, key):
            raise ModelRegistryError(
                f"retired directory '{directory}' lies inside '{layer_directory}', the directory of layer "
                f"'{facts.name}': its files would be read by that layer and refused"
            )


def _retired_storage(contract: Mapping[str, Any], layers: Sequence[LayerFacts]) -> tuple[RetiredStorage, ...]:
    """The contract's ``retiredStorage`` table, checked against the layers it names.

    A retired directory may not be, differ only in case from, or lie inside a
    layer's directory: a file there would be both read by that layer and refused. It
    may *hold* a layer's directory (``data_types`` holds ``data_types/application``),
    because the refusal reads only the files directly in the retired directory.
    Every directory it moved to must be a declared layer's, and those layers must
    share one file suffix.
    """
    by_directory = {facts.storage.directory: facts for facts in layers}
    retired: list[RetiredStorage] = []
    for decl in contract.get("retiredStorage", ()):
        directory = decl["directory"]
        _check_retired_directory(directory, layers)
        moved_to = tuple(decl["movedTo"])
        unknown = [target for target in moved_to if target not in by_directory]
        if unknown:
            raise ModelRegistryError(
                f"retired directory '{directory}' names {unknown} as where its files belong, and no layer stores "
                "its elements there"
            )
        suffixes = {by_directory[target].storage.file_suffix for target in moved_to}
        if len(suffixes) != 1:
            raise ModelRegistryError(
                f"retired directory '{directory}' moved to layers whose file suffixes differ: {sorted(suffixes)}"
            )
        (suffix,) = suffixes
        retired.append(RetiredStorage(directory=directory, since=decl["since"], moved_to=moved_to, file_suffix=suffix))
    return tuple(retired)


def _check_join_tokens(layer_decls: Sequence[Mapping[str, Any]], snapshot_attributes: Mapping[str, str]) -> None:
    """Refuse two layers that would share the touched-identity key.

    The key is ``snapshot_attribute or name``, under which the commit gate files a
    layer's reviewed identities. A collision drops one layer's identities and can
    explain a file by a change that never touched it, which is fail-open in the
    commit gate. Refused: a duplicated ``snapshotAttributes`` value, and one layer's
    ``name`` equal to another's bound attribute.
    """
    seen: dict[str, str] = {}
    for decl in layer_decls:
        token = snapshot_attributes.get(decl["id"]) or decl["name"]
        clash = seen.get(token)
        if clash is not None:
            raise ModelRegistryError(f"layers '{clash}' and '{decl['name']}' share the touched-identity key '{token}'")
        seen[token] = decl["name"]


def _layer_token(layer_decl: Mapping[str, Any]) -> str:
    """One layer's singular token: what it declares, or its own ``name``."""
    return layer_decl.get("token") or layer_decl["name"]


def _check_layer_tokens(layer_decls: Sequence[Mapping[str, Any]]) -> None:
    """Refuse two layers claiming the same singular token.

    :attr:`LayerFacts.token` keys ``impact``'s per-layer maps and public
    ``ElementRef.layer``, so a collision would merge two layers into one section.
    Checked over the effective token, declared or fallen back, which reaches the
    mixed case: one layer declaring ``tax`` beside another named ``tax``.
    """
    seen: dict[str, str] = {}
    for decl in layer_decls:
        token = _layer_token(decl)
        clash = seen.get(token)
        if clash is not None:
            raise ModelRegistryError(f"layers '{clash}' and '{decl['name']}' both declare the token '{token}'")
        seen[token] = decl["name"]


def _check_identity_fields_are_not_aliased(
    layer_decls: Sequence[Mapping[str, Any]], field_names: Mapping[str, str]
) -> None:
    """Refuse a layer whose identity field is spelled differently in Python.

    ``identityField`` is read off elements as a plain attribute, so the contract's
    field name must also be the Python attribute; an aliased identity would surface
    as an ``AttributeError`` while loading a model. Supporting it is a contract
    change.

    The walk covers member kinds too, so a ``binding.fieldNames`` row renaming
    ``field:swc.port.name`` is refused although no reader takes a port's ``name`` as
    the identity: over-strict on purpose. The message names the field's full
    contract id.
    """
    for decl in layer_decls:
        identity = decl["identityField"]
        for kind_decl in decl["kinds"]:
            for field_decl in kind_decl["fields"]:
                if field_decl["name"] != identity:
                    continue
                attribute = field_names.get(field_decl["id"], identity)
                if attribute != identity:
                    raise ModelRegistryError(
                        f"{field_decl['id']}: layer '{decl['name']}' is identified by '{identity}', "
                        f"but the binding spells that field '{attribute}' in Python; "
                        "an identity field may not be aliased"
                    )


def _check_relation_targets(relations: list[RelationEdge], kinds: Mapping[str, KindFacts]) -> None:
    """Refuse a relation whose target does not exist as declared.

    Checked after every layer is walked, since a relation may name a kind of a
    later layer. The kind, its layer and the matched field are all checked: a
    relation naming a field its kind does not declare would match nothing, silently.
    """
    for edge in relations:
        target = kinds.get(edge.target_kind)
        if target is None:
            raise ModelRegistryError(
                f"{edge.field_id}: the relation targets element kind '{edge.target_kind}', undeclared"
            )
        if target.layer != edge.target_layer:
            raise ModelRegistryError(
                f"{edge.field_id}: the relation names kind '{edge.target_kind}' of layer "
                f"'{edge.target_layer}', but that kind belongs to layer '{target.layer}'"
            )
        if target.get_field(edge.target_field) is None:
            raise ModelRegistryError(
                f"{edge.field_id}: the relation matches field '{edge.target_field}' of "
                f"'{edge.target_kind}', which declares no such field"
            )


def _check_no_unused_relation_binding(relations: list[RelationEdge], bindings: Mapping[str, Any]) -> None:
    """Refuse a binding row for a field that declares no relation."""
    unused = sorted(set(bindings) - {edge.field_id for edge in relations})
    if unused:
        raise ModelRegistryError(
            f"binding.json binds a relation resolver to fields that declare no relation: {', '.join(unused)}"
        )


def _check_member_reference_identity(edge: MemberReferenceEdge, kinds: Mapping[str, KindFacts]) -> None:
    """Refuse a listed collection a member cannot be compared by.

    A ``field`` collection compares the value with its identity field, which every
    member kind must declare; a ``whole_value`` list of scalars compares it with the
    item. Any other collection has no one value a name could equal.
    """
    for target in edge.collections:
        collection = kinds[target.kind].get_field(target.field)
        facts = collection.collection if collection is not None else None
        assert facts is not None, "a member reference target is resolved from the declared collections"
        if facts.member_identity == "field":
            if facts.identity_field is None:
                raise ModelRegistryError(
                    f"{edge.field_id}: the member reference lists '{target.collection_id}', a `field` collection "
                    "that declares no identity field"
                )
            missing = [
                member
                for member in facts.member_kinds
                if member not in kinds or kinds[member].get_field(facts.identity_field) is None
            ]
            if missing:
                raise ModelRegistryError(
                    f"{edge.field_id}: the member reference lists '{target.collection_id}', a `field` collection "
                    f"whose identity field '{facts.identity_field}' its member kinds do not all declare"
                )
        elif not (
            facts.member_identity == "whole_value" and facts.collection_kind == "list" and not facts.member_kinds
        ):
            raise ModelRegistryError(
                f"{edge.field_id}: the member reference lists '{target.collection_id}', a "
                f"{facts.member_identity} {facts.collection_kind} whose member no single value identifies"
            )


def _check_member_references(edges: list[MemberReferenceEdge], kinds: Mapping[str, KindFacts]) -> None:
    """Refuse a member reference whose collections a reader could not reach as declared.

    Without ``via`` every listed collection must sit on a top-level kind of the
    source's own layer. With ``via`` the sibling field must exist on the same kind
    and declare a member reference of its own without a ``via`` (one step, never a
    chain), every listed collection must belong to one layer, and each member kind
    of every collection the sibling lists must carry exactly one ``reference`` into
    that layer.
    """
    for edge in edges:
        _check_member_reference_identity(edge, kinds)
        if edge.via is None:
            for target in edge.collections:
                owner = kinds[target.kind]
                if target.layer != edge.source_layer or owner.member_only:
                    raise ModelRegistryError(
                        f"{edge.field_id}: the member reference lists '{target.collection_id}' without `via`, "
                        f"but that collection is not on a top-level kind of layer '{edge.source_layer}'"
                    )
            continue
        sibling = kinds[edge.source_kind].get_field(edge.via)
        if sibling is None:
            raise ModelRegistryError(
                f"{edge.field_id}: the member reference's `via` names '{edge.via}', which kind "
                f"'{edge.source_kind}' does not declare"
            )
        if sibling.member_reference is None:
            raise ModelRegistryError(
                f"{edge.field_id}: the member reference's `via` names '{edge.via}', which declares no member reference"
            )
        if sibling.member_reference.via is not None:
            raise ModelRegistryError(
                f"{edge.field_id}: the member reference's `via` names '{edge.via}', whose own member reference "
                f"reaches its element through `via` '{sibling.member_reference.via}'; a `via` follows one step, "
                "never a chain"
            )
        layers = {target.layer for target in edge.collections}
        if len(layers) != 1:
            raise ModelRegistryError(
                f"{edge.field_id}: the member reference lists collections of {len(layers)} layers; "
                "a reference reached through `via` names an element of one"
            )
        (target_layer,) = layers
        for through in sibling.member_reference.collections:
            members = kinds[through.kind].get_field(through.field)
            member_kinds = (
                members.collection.member_kinds if members is not None and members.collection is not None else ()
            )
            if not member_kinds:
                raise ModelRegistryError(
                    f"{edge.field_id}: the member reference's `via` selects a member of "
                    f"'{through.collection_id}', a whole-value list, which carries no reference"
                )
            for member in member_kinds:
                facts = kinds.get(member)
                into = [
                    fact.reference
                    for fact in (facts.fields if facts is not None else ())
                    if fact.reference is not None and fact.reference.target_layer == target_layer
                ]
                if len(into) != 1:
                    raise ModelRegistryError(
                        f"{edge.field_id}: the member reference's `via` selects a member of "
                        f"'{through.collection_id}' whose kind '{member}' carries {len(into)} references into "
                        f"layer '{target_layer}', not exactly one"
                    )


def build_model_registry(*, contract: Mapping[str, Any], binding: Mapping[str, Any]) -> ModelRegistry:
    """Resolve *contract* against *binding* into a :class:`ModelRegistry`.

    Both documents are arguments so a test can build a registry over a merged
    contract; :func:`production_registry` reads the installed files.

    Raises:
        ModelRegistryError: a declared kind the binding does not bind, a bound
            class that is not a Pydantic model, a layer with no top-level kind, a
            multi-kind layer whose kinds disagree about their discriminator, two
            layers sharing a ``name``, an ``id``, a ``storage.directory`` (or one's
            directory a prefix of another's), a ``displayOrder``, a singular
            ``token`` or the touched-identity key, a layer whose identity field the
            binding aliases, a declared relation with no bound resolver, a bound
            resolver for a field that declares none, a collection-shaped field
            declaring a ``reference``, a relation whose target kind, layer or field
            is undeclared, or a member reference a reader could not reach as
            declared.
    """
    kind_bindings = binding.get("kinds", {})
    snapshot_attributes = binding.get("snapshotAttributes", {})
    relation_bindings = binding.get("relations", {})
    field_names = binding.get("fieldNames", {})

    _check_layer_declarations(contract["layers"])
    _check_layer_tokens(contract["layers"])
    _check_join_tokens(contract["layers"], snapshot_attributes)
    _check_identity_fields_are_not_aliased(contract["layers"], field_names)

    layers: list[LayerFacts] = []
    kinds_index: dict[str, KindFacts] = {}
    references: list[ReferenceEdge] = []
    relations: list[RelationEdge] = []
    member_references: list[MemberReferenceEdge] = []
    collections: list[CollectionFacts] = []
    declared_collections = _declared_collections(contract["layers"])

    for layer_decl in contract["layers"]:
        layer_name = layer_decl["name"]
        member_of = _member_of(layer_decl)
        kind_facts: list[KindFacts] = []
        for kind_decl in layer_decl["kinds"]:
            kind_id = kind_decl["id"]
            bound = kind_bindings.get(kind_id)
            if bound is None:
                raise ModelRegistryError(
                    f"{kind_id}: the contract declares this element kind and binding.json binds no class to it"
                )
            fields = tuple(
                _field_facts(
                    decl,
                    layer_name,
                    kind_decl["kind"],
                    relation_bindings=relation_bindings,
                    field_names=field_names,
                    declared_collections=declared_collections,
                )
                for decl in kind_decl["fields"]
            )
            references += [facts.reference for facts in fields if facts.reference is not None]
            relations += [facts.relation for facts in fields if facts.relation is not None]
            member_references += [facts.member_reference for facts in fields if facts.member_reference is not None]
            collections += [facts.collection for facts in fields if facts.collection is not None]
            kind_facts.append(
                KindFacts(
                    id=kind_id,
                    kind=kind_decl["kind"],
                    layer=layer_name,
                    model=_resolve_class(kind_id, bound),
                    fields=fields,
                    member_only=kind_decl["memberOnly"],
                    discriminator_field=kind_decl.get("discriminatorField"),
                    member_of=member_of.get(kind_decl["kind"], ()),
                    description=kind_decl.get("description"),
                )
            )

        for facts in kind_facts:
            if facts.kind in kinds_index:
                raise ModelRegistryError(f"duplicate element-kind name '{facts.kind}'")
            kinds_index[facts.kind] = facts

        top_level = tuple(facts for facts in kind_facts if not facts.member_only)
        placement_decl = layer_decl.get("placement")
        storage_decl = layer_decl["storage"]
        layers.append(
            LayerFacts(
                id=layer_decl["id"],
                name=layer_name,
                optional=layer_decl["optional"],
                identity_field=layer_decl["identityField"],
                storage=StorageRecord(
                    directory=storage_decl["directory"],
                    nesting=storage_decl["nesting"],
                    file_suffix=storage_decl["fileSuffix"],
                    filename_from=storage_decl["filenameFrom"],
                ),
                kinds=tuple(kind_facts),
                element_annotation=_element_annotation(layer_name, top_level),
                token=_layer_token(layer_decl),
                placement=(
                    None
                    if placement_decl is None
                    else PlacementFacts(
                        layer=layer_name,
                        kind=placement_decl["kind"],
                        field=placement_decl["field"],
                        type=placement_decl["type"],
                        description=placement_decl["description"],
                    )
                ),
                snapshot_attribute=snapshot_attributes.get(layer_decl["id"]),
                description=layer_decl.get("description"),
                absent_description=layer_decl.get("absentDescription"),
                label=layer_decl.get("label"),
                display_order=layer_decl.get("displayOrder"),
                arxml_bound=layer_decl.get("arxmlBound", False),
            )
        )

    _check_display_orders(layers)
    _check_relation_targets(relations, kinds_index)
    _check_no_unused_relation_binding(relations, relation_bindings)
    _check_member_references(member_references, kinds_index)

    names_by_id = {facts.id: facts.name for facts in layers}
    return ModelRegistry(
        contract_version=contract["contractVersion"],
        layers=tuple(layers),
        _adapter_order=tuple(names_by_id[layer_id] for layer_id in snapshot_attributes if layer_id in names_by_id),
        _kinds=kinds_index,
        _references=tuple(references),
        _relations=tuple(relations),
        _member_references=tuple(member_references),
        _collections=tuple(collections),
        retired_storage=_retired_storage(contract, layers),
    )


# ---------------------------------------------------------------------------
# The injection seam
# ---------------------------------------------------------------------------


@functools.cache
def production_registry() -> ModelRegistry:
    """The registry over the **shipped** contract, built once per process.

    Distinct from :func:`current_registry` on purpose: a surface built **at import**
    must be the production projection whatever context it is imported inside;
    resolving the context variable there would let a first import inside
    :func:`use_registry` rebind it for the whole process. Four module-level surfaces
    reach it: ``editing/registry.py``'s ``_PRODUCTION_MODEL`` (and what is projected
    from it), ``reader``'s ``_GENERATION_REGISTRY`` and ``_NESTING_LAYER``,
    ``editing/diagnosis``'s ``_suffixes_by_kind`` and ``swcraft_agent``'s
    ``SCOPE_JSON_SCHEMA``.
    """
    return build_model_registry(contract=load_contract(), binding=load_binding())


_CURRENT: ContextVar[ModelRegistry | None] = ContextVar("swcraft_model_registry", default=None)


def current_registry() -> ModelRegistry:
    """The registry this call is running under: the shipped one unless a :func:`use_registry` block is active.

    Resolve it **in a function body**, never in a default-argument expression,
    which is evaluated once at definition time.
    """
    registry = _CURRENT.get()
    return production_registry() if registry is None else registry


@contextlib.contextmanager
def use_registry(registry: ModelRegistry) -> Iterator[ModelRegistry]:
    """Run the block with *registry* as :func:`current_registry`'s answer, restored on exit even when it raises."""
    token = _CURRENT.set(registry)
    try:
        yield registry
    finally:
        _CURRENT.reset(token)


# ---------------------------------------------------------------------------
# A loaded element read by published name (contract 12.4.0)
# ---------------------------------------------------------------------------


def published_value(model: BaseModel, name: str) -> Any:
    """The value *model* holds under the published field name *name* (``from`` reads ``from_``).

    The alias is resolved against ``model_fields``, the derivation the registry
    publishes its field names from; a name no field publishes reads ``None``.
    """
    for attribute, info in type(model).model_fields.items():
        if (info.alias or attribute) == name:
            return getattr(model, attribute)
    return None


def loaded_member_identity(
    member: BaseModel, collection: CollectionFacts, registry: ModelRegistry | None = None
) -> Any:
    """The identity one loaded list member carries, by its collection's declared strategy.

    ``field``: the identity field's value. ``composite``: the identity tuple as an
    object, completed by :func:`complete_composite_identity`. ``whole_value``: the
    member's JSON dump without every field equal to its member kind's declared
    default (so ``name: null`` and ``disabled_modes: []`` are left out, as the
    editing protocol leaves them out); when no member kind matches
    (:func:`kind_of_loaded`), the dump whole.
    """
    registry = registry or current_registry()
    if collection.member_identity == "field" and collection.identity_field:
        return published_value(member, collection.identity_field)
    if collection.member_identity == "composite":
        return complete_composite_identity(
            {name: published_value(member, name) for name in collection.identity_fields},
            collection.identity_fields,
            registry.optional_identity_fields(collection.identity_fields, collection.member_kinds),
        )
    dumped = member.model_dump(mode="json", by_alias=True)
    kind = kind_of_loaded(member, [registry.kind(name) for name in collection.member_kinds])
    if kind is None:
        return dumped
    defaults = {field.name: field.default for field in kind.file_fields if field.has_default}
    return {key: value for key, value in dumped.items() if not (key in defaults and value == defaults[key])}


def kind_of_loaded(value: Any, candidates: Iterable[KindFacts]) -> KindFacts | None:
    """The one of *candidates* whose bound class *value* is exactly an instance of, or None.

    Exact class, never ``isinstance``: a subclass is another kind. None when no
    candidate or more than one matches: two kinds bound to one class are a
    registry defect, and a guess would spell the wrong identity.
    """
    matches = [kind for kind in candidates if type(value) is kind.model]
    return matches[0] if len(matches) == 1 else None
