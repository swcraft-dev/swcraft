"""``swcraft list`` — every declared layer's elements, by identity, and nothing else.

The VS Code extension does not read the model itself, so it asks this listing
for a project's element names. :func:`list_model` reads only the layer order,
each layer's ``identityField`` and, through
:func:`~swcraft_tools.reader.read_layer`, its storage record.

The **layer** order is the contract's ``layerDecl.displayOrder``, as in ``diff``.
The **element** order inside a layer is the reader's: the storage path composed
from the element's placement and declared identity, compared as a plain ``str``,
ordinally and case-sensitively on every platform; the file name takes no part.

**What this module does not reach.**

- It answers about what is *on disk*, not about any consistency check: two files
  declaring one identity are listed twice, and a file whose name disagrees with
  its identity is listed under the identity.
- There is no partial answer: a single unparseable or invalid element raises
  :class:`~swcraft_tools.model_state.ElementParseError` and the whole call fails,
  so a menu never silently misses the element the user is trying to fix.
- It validates no layer name: an undeclared one raises ``KeyError``.
- A layer whose directory is absent yields an **empty** tuple, not an absent key,
  so "no SWCs" is told from "no such layer".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from swcraft_tools.model_registry import ModelRegistry, current_registry
from swcraft_tools.reader import ModelRootNotFoundError, read_layer

#: The version of the ``--format json`` document, bumped when the shape changes,
#: so a reader behind a stale environment refuses rather than mis-parses.
LISTING_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class LayerListing:
    """One layer's identities, and the field they were read from (``id`` or ``name``)."""

    identity_field: str
    identities: tuple[str, ...]


def list_model(
    model_root: str,
    layer: str | None = None,
    *,
    registry: ModelRegistry | None = None,
) -> dict[str, LayerListing]:
    """Every element of every declared layer under *model_root*, by identity.

    Args:
        model_root: the model directory.
        layer: one layer's contract name to restrict the answer to, or ``None``
            for every declared layer.
        registry: the kernel registry; ``None`` resolves
            :func:`~swcraft_tools.model_registry.current_registry` at call time.

    Returns:
        A mapping from layer name to its :class:`LayerListing`, in
        ``registry.display_order``. With *layer* given the mapping has exactly
        one entry; without it, one entry per declared layer, including the
        layers whose directory does not exist (their identities are ``()``).

    Raises:
        FileNotFoundError: *model_root* does not exist (an empty one is an answer).
        KeyError: *layer* is not a declared layer name.
        ValueError: an element file does not parse or does not validate, naming
            it (:class:`~swcraft_tools.model_state.ElementParseError`).
    """
    registry = registry or current_registry()
    if not Path(model_root).exists():
        raise ModelRootNotFoundError(model_root)
    names = registry.display_order if layer is None else (registry.layer(layer).name,)
    return {name: _listing_of(model_root, name, registry) for name in names}


def _listing_of(model_root: str, layer: str, registry: ModelRegistry) -> LayerListing:
    """One layer's listing, read through the generic reader."""
    facts = registry.layer(layer)
    elements = read_layer(model_root, layer, registry=registry)
    return LayerListing(
        identity_field=facts.identity_field,
        identities=tuple(getattr(element, facts.identity_field) for element in elements),
    )


def format_listing(listings: dict[str, LayerListing]) -> str:
    """One ``layer: element`` line per element, layers in the given order.

    Newline-terminated, and **empty for a model with no elements at all**.
    """
    return "".join(f"{layer}: {identity}\n" for layer, listing in listings.items() for identity in listing.identities)


def format_listing_json(listings: dict[str, LayerListing]) -> str:
    """The versioned, machine-readable document, newline-terminated.

    ``{"schemaVersion": 1, "layers": {"<layer>": {"identityField": ...,
    "elements": [...]}}}``, the layers in the order they were given.
    """
    document = {
        "schemaVersion": LISTING_SCHEMA_VERSION,
        "layers": {
            layer: {"identityField": listing.identity_field, "elements": list(listing.identities)}
            for layer, listing in listings.items()
        },
    }
    return json.dumps(document, indent=2) + "\n"
