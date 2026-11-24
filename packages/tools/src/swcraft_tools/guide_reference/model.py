"""The guide's model reference: one page per layer, from the declarations.

A *total* consumer of the model contract: one page per
:attr:`~swcraft_tools.model_registry.ModelRegistry.layers` entry, with every element kind and every
:attr:`~swcraft_tools.model_registry.KindFacts.fields` entry, read off
:func:`~swcraft_tools.model_registry.current_registry`. The contract's ``description`` strings are
rendered **verbatim**. A contract ``type`` with no rendering raises :class:`UnrenderableFieldType`.
Layers come in the contract's ``displayOrder``, kinds and fields in declaration order.

**What these pages do not express:** ``FieldFacts.relation``, ``FieldFacts.immutable``,
``FieldFacts.omit_at_default``, ``LayerFacts.absent_description``, and whether the rendered prose is
correct or readable.
"""

from __future__ import annotations

import re

import swcraft_tools.guide_reference.render as render
from swcraft_tools.guide_corpus import page_name
from swcraft_tools.model_registry import (
    FieldFacts,
    KindFacts,
    LayerFacts,
    ModelRegistry,
    current_registry,
)

__all__ = [
    "MODEL_INDEX_PATH",
    "UnrenderableFieldType",
    "all_artifacts",
    "field_row",
    "kind_anchor",
    "layer_page",
    "layer_page_path",
    "layer_section_page",
    "model_index_page",
    "type_label",
]

#: The relative path of the layer index; the per-layer pages sit beside it.
MODEL_INDEX_PATH = "reference/model/index.md"

#: The scalar spellings of the contract's type vocabulary, each rendered as its own word.
_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean"})

#: The contract spelling whose shape another declaration decides: ``init`` follows the element's own
#: data type. Kept byte-identical with ``editor_schema._VALUE_TYPE``.
_VALUE_TYPE = "value"

#: The reader's name for a ``value``; the field's ``description`` carries the rule. It says composites
#: *may* nest, not "to any depth": the walks that read such a value recurse without a bound, so a value
#: nested past CPython's recursion limit raises ``RecursionError``.
_VALUE_LABEL = "a scalar, a list, or a mapping, which may nest"

_LIST_TYPE = re.compile(r"^list\[(?P<members>.+)\]$")
#: ``map[<key>, <value>]`` with the grammar's exact ``, `` separator, so a re-spelling is refused here as
#: in the contract.
_MAP_TYPE = re.compile(r"^map\[(?P<key>[a-z][a-z0-9_]*), (?P<value>[a-z][a-z0-9_]*)\]$")

_FIELD_TABLE_HEADERS = ("Field", "Type", "Required", "Default", "What it is")


class UnrenderableFieldType(ValueError):
    """A contract ``type`` this generator has no rendering for.

    Raised instead of emitting an empty cell, which would read as a statement about the field.
    """

    def __init__(self, field_id: str, type_name: str) -> None:
        self.field_id = field_id
        self.type_name = type_name
        super().__init__(f"{field_id}: no guide rendering for contract type {type_name!r}")


def _resolve(registry: ModelRegistry | None) -> ModelRegistry:
    """The registry to generate from, resolved at call time and never at definition time."""
    return current_registry() if registry is None else registry


def _members(field: FieldFacts) -> tuple[str, ...]:
    return field.collection.member_kinds if field.collection is not None else ()


def type_label(field: FieldFacts) -> str:
    """The reader's name for one field's shape.

    Resolved in :func:`swcraft_tools.editor_schema._type_schema`'s order, so the two generators cannot
    disagree about one field: an ``elementKind`` field, a collection naming its member kinds, the
    scalars, ``value``, then the two parameterized spellings.
    """
    if field.element_kind is not None:
        return f"a {render.code(field.element_kind)} block"
    if members := _members(field):
        return "a list of " + " or ".join(render.code(kind) for kind in members) + " blocks"
    if field.type in _SCALAR_TYPES:
        return field.type
    if field.type == _VALUE_TYPE:
        return _VALUE_LABEL
    if (match := _LIST_TYPE.match(field.type)) is not None:
        if (member := match.group("members")) in _SCALAR_TYPES:
            return f"a list of {member} values"
        raise UnrenderableFieldType(field.id, field.type)
    if (match := _MAP_TYPE.match(field.type)) is not None:
        if match.group("key") == "string" and match.group("value") in _SCALAR_TYPES:
            return f"a mapping from a name to {match.group('value')} values"
        raise UnrenderableFieldType(field.id, field.type)
    raise UnrenderableFieldType(field.id, field.type)


def _type_cell(field: FieldFacts) -> str:
    """The type column: the shape, then the unit, the accepted values and the reference target.

    All read off :class:`~swcraft_tools.model_registry.FieldFacts`, so a field gaining a unit or an
    enum gains the clause with no edit here.
    """
    label = type_label(field)
    parts = [label if field.unit is None else f"{label} ({field.unit})"]
    if field.enum:
        parts.append("one of " + ", ".join(render.code(render.literal(value)) for value in field.enum))
    if field.reference is not None:
        edge = field.reference
        parts.append(
            f"the {render.code(edge.target_identity_field)} of "
            f"{render.link(edge.target_layer, f'{edge.target_layer}.md')}"
        )
    if field.placement:
        parts.append("taken from the file's directory")
    return ", ".join(parts)


def _default_cell(field: FieldFacts) -> str:
    """The default column, in the spelling a YAML author would write.

    Three states: no declared default (``—``), a declared default of nothing (``absent``), and a
    concrete value.
    """
    if not field.has_default:
        return "—"
    if field.default is None:
        return "absent"
    return render.code(render.literal(field.default))


def field_row(field: FieldFacts) -> tuple[str, ...]:
    """One row of a kind's field table."""
    return (
        render.code(field.name),
        _type_cell(field),
        "yes" if field.required else "no",
        _default_cell(field),
        field.description or "",
    )


def _kind_purpose(kind: KindFacts, layer: LayerFacts) -> str:
    """When one kind's section helps: writing or reading that kind, as a file or as a block inside one."""
    what = "blocks" if kind.member_only else "files"
    return f"Writing `{kind.kind}` {what} in {layer.display_label}: each key, its type and default"


def _kind_section(kind: KindFacts, layer: LayerFacts, level: int) -> str:
    """One element kind: its heading (anchored by the kind's name), its prose and its field table.

    A kind with no fields gets no table.
    """
    blocks = [
        render.heading(level, render.code(kind.kind), anchor=kind_anchor(kind), purpose=_kind_purpose(kind, layer)),
    ]
    if kind.description:
        blocks.append(render.paragraph(kind.description))
    if kind.fields:
        blocks.append(render.table(_FIELD_TABLE_HEADERS, (field_row(field) for field in kind.fields)))
    return render.document(*blocks).rstrip("\n")


def _storage_sentence(layer: LayerFacts) -> str:
    """Where one layer's files live and what each is called, off the storage record.

    The second sentence is the store's collision rule: the file name equals the identity unless another
    element's file holds it, compared case-insensitively.
    """
    storage = layer.storage
    directory = storage.directory
    if storage.is_recursive:
        segment = layer.placement.field if layer.placement is not None else "subdirectory"
        location = render.code(f"{directory}/<{segment}>/")
    else:
        location = render.code(f"{directory}/")
    identity = render.code(storage.filename_from)
    return (
        f"One file per {render.code(layer.token)}, in {location} under the model root, named after its "
        f"{identity} and ending in {render.code(storage.file_suffix)}. Where that file name is already taken "
        f"by another element's file — compared without regard to case — the file gets the nearest free "
        f"{render.code('~1')}, {render.code('~2')}, … instead; the element is identified by the {identity} "
        "inside the file, never by its file name."
    )


def _kinds_sentence(layer: LayerFacts) -> str:
    """How a reader tells this layer's element kinds apart — or that there is only one."""
    top_level = layer.top_level_kinds
    if len(top_level) == 1:
        return ""
    discriminators = {kind.discriminator_field for kind in top_level if kind.discriminator_field}
    names = ", ".join(render.code(kind.kind) for kind in top_level)
    if len(discriminators) == 1:
        key = render.code(discriminators.pop())
        return f"A file of this layer is one of {len(top_level)} kinds — {names} — chosen by its {key} key."
    return f"A file of this layer is one of {len(top_level)} kinds: {names}."


def layer_page_path(layer: LayerFacts) -> str:
    """The relative path one layer's page is written to; also the index's link target."""
    return f"reference/model/{layer.name}.md"


def layer_section_page(layer: LayerFacts) -> str:
    """The guide corpus's page id of one layer's page: :func:`guide_corpus.page_name` of :func:`layer_page_path`.

    ``editor_schema.py`` publishes it as the layer's ``guidePage`` in ``references.json``, the
    page the extension's ``guide`` lens opens.
    """
    return page_name(layer_page_path(layer))


def kind_anchor(kind: KindFacts) -> str:
    """The anchor of one kind's heading on its layer's page: the kind's own name.

    ``editor_schema.py`` publishes it as the kind's ``guideAnchor`` in ``references.json``, the
    section the extension's ``guide`` lens opens.
    """
    return kind.kind


def layer_page(layer: LayerFacts) -> str:
    """The page for one layer: its prose, where its files live, then every kind it declares."""
    blocks = [
        render.GENERATED_COMMENT,
        render.heading(
            1,
            render.escape_markup(layer.display_label),
            purpose=f"Where {layer.display_label} files live, the kinds they hold and every key they accept",
        ),
        render.GENERATED_NOTICE,
    ]
    if layer.description:
        blocks.append(render.paragraph(layer.description))
    if layer.optional:
        directory = render.code(f"{layer.storage.directory}/")
        blocks.append(
            render.paragraph(
                f"This layer is optional. A project with no {directory} directory is "
                "complete without it, and none of the checks that read it run."
            )
        )
    blocks.append(render.paragraph(_storage_sentence(layer)))
    blocks.append(render.paragraph(_kinds_sentence(layer)))
    blocks.extend(_kind_section(kind, layer, 2) for kind in layer.top_level_kinds)
    members = [kind for kind in layer.kinds if kind.member_only]
    if members:
        blocks.append(
            render.heading(
                2,
                "Blocks used inside those files",
                anchor="blocks-used-inside-those-files",
                purpose=f"The blocks nested inside {layer.display_label} files, which are not files of their own",
            )
        )
        blocks.append(
            render.paragraph(
                "These are not files of their own: each appears as a block inside an element of this layer."
            )
        )
        blocks.extend(_kind_section(kind, layer, 3) for kind in members)
    return render.document(*blocks)


def _layers(registry: ModelRegistry) -> tuple[LayerFacts, ...]:
    """Every layer, in the contract's ``displayOrder``, the order ``swcraft list`` prints."""
    return tuple(registry.layer(name) for name in registry.display_order)


def model_index_page(*, registry: ModelRegistry | None = None) -> str:
    """The layer index: one row per declared layer, linking to its page."""
    resolved = _resolve(registry)
    rows = [
        (
            render.link(render.escape_markup(layer.display_label), f"{layer.name}.md"),
            layer.description or "",
            render.code(f"{layer.storage.directory}/") + (" (optional)" if layer.optional else ""),
        )
        for layer in _layers(resolved)
    ]
    return render.document(
        render.GENERATED_COMMENT,
        render.heading(
            1,
            "The model layers",
            purpose="Which layer of the model holds what, and the directory its files live in",
        ),
        render.GENERATED_NOTICE,
        render.paragraph(
            "A SWCraft model is a directory of YAML files: one file per element, its file name derived from "
            "the element's own identity and identical to it unless another element's file already holds that "
            "name. Each layer below is one directory, and each page lists every kind of element that layer "
            "holds and every key those elements accept."
        ),
        render.table(("Layer", "What it holds", "Where the files live"), rows),
    )


def all_artifacts(*, registry: ModelRegistry | None = None) -> dict[str, str]:
    """Every page this module renders, keyed by its path relative to ``docs/guide/``.

    The index first, then one page per layer in the index's order; no page another generator owns.
    """
    resolved = _resolve(registry)
    artifacts: dict[str, str] = {MODEL_INDEX_PATH: model_index_page(registry=resolved)}
    artifacts.update({layer_page_path(layer): layer_page(layer) for layer in _layers(resolved)})
    return artifacts
