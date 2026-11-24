"""``discover`` — the registry projected into a ``discoverResponse``.

The response carries the passport (``project``: name, ARXML version,
``swcraft_version``, **no storage root**), the fresh ``baseline`` token, ``reads``,
the ``form`` served, and one ``layerDescriptor`` per registry layer **in registry
order**; a ``layer``-narrowed request returns the same list with one member.

**``summary`` is the default form**, **derived** from ``full``: every
``description`` removed, and the booleans of :data:`SUMMARY_OMITTED_DEFAULTS`
where they are ``false`` (absent means ``false``). Every type, reference,
collection and identity fact is in both. What its oracle does not reach: it runs
on ``examples/basic`` and one synthetic contract only, and the schema does not
check that an absent key matches the registry.

**The refusal is addressless**: ``discover`` is held to ``read``'s budget, and
hints offer narrower discoveries (:func:`narrower_discovers`).

**The passport carries no root by construction**: it never holds
``model_root``, ``dist_root``, ``views_root`` or ``model_hash``. An absent layer
still publishes its element kinds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from swcraft_tools._version import __version__
from swcraft_tools.editing import read
from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.protocol import (
    DiscoverForm,
    DiscoverRequest,
    DiscoverResponse,
    Hint,
    Measured,
    Passport,
)
from swcraft_tools.editing.staging import baseline_token
from swcraft_tools.project import SwcraftProject

__all__ = ["SUMMARY_OMITTED_DEFAULTS", "LayerNotDeclaredError", "discover", "narrower_discovers"]

#: The descriptor booleans a ``summary`` omits at their default: one
#: ``(position, key, default)`` row each, applied at those positions only. A key
#: the table does not name (``present``) is published as is.
SUMMARY_OMITTED_DEFAULTS: tuple[tuple[str, str, bool], ...] = (
    ("layer", "optional", False),
    ("field", "required", False),
    ("field", "immutable", False),
)
_LAYER_ROWS = tuple((key, default) for position, key, default in SUMMARY_OMITTED_DEFAULTS if position == "layer")
_FIELD_ROWS = tuple((key, default) for position, key, default in SUMMARY_OMITTED_DEFAULTS if position == "field")


class LayerNotDeclaredError(LookupError):
    """A ``discover`` narrowed to a layer the registry does not declare.

    ``$defs/layerName`` is a pattern, not an enum, so an undeclared layer is
    well-formed on the wire and rejected semantically. The caller maps this onto
    the ``unknown-field`` protocol code.
    """


def discover(
    project: SwcraftProject,
    *,
    layer: str | None = None,
    form: DiscoverForm | None = None,
    registry: reg.Registry | None = None,
    discover_default: DiscoverForm | None = None,
) -> dict[str, Any]:
    """The answer to a ``discover`` request against *project*.

    Args:
        layer: narrow the answer to one layer, or ``None`` for the whole project.
        form: ``"summary"``, ``"full"`` or ``"baseline"``, or ``None`` for the
            default form; the answer echoes the form it was served in.
        discover_default: the channel's project-wide default form, or ``None``.
            A *form* the request names wins over it.

    Returns:
        A ``discoverResponse`` as plain JSON-compatible data.

    Raises:
        LayerNotDeclaredError: *layer* is not a registry layer.
        FileNotFoundError: the project's model root does not exist.
        ~swcraft_tools.editing.read.OverBudget: the published response would
            exceed :data:`~swcraft_tools.editing.read.RESPONSE_BUDGET_WIRE_BYTES`
            (no address; hints narrower discoveries).
    """
    model_root = Path(project.resolved_model_root)

    # Resolved in the body, never in a default argument (which would bind at
    # import, ignoring `model_registry.use_registry`), and passed down once so a
    # response cannot be composed out of two registries.
    registry = reg.or_current(registry)

    if layer is None:
        descriptors = registry.layers
    else:
        if layer not in registry.layer_names:
            raise LayerNotDeclaredError(layer)
        descriptors = (registry.layer(layer),)

    served: DiscoverForm = read.default_discover_form(layer, discover_default) if form is None else form
    # `baseline` publishes no descriptor at all: the client holds the schema
    # already and asks for the passport and the token alone. A `layer` beside
    # it is still checked above, so an undeclared one is refused either way.
    layers = (
        ()
        if served == "baseline"
        else tuple(_publish_layer(descriptor, model_root, registry) for descriptor in descriptors)
    )
    # Gated on "not `full`" rather than "is `summary`": the fail-safe direction,
    # so an unknown form ships less prose, never all of it.
    if served != "full":
        layers = tuple(_strip_descriptions_and_defaults(published) for published in layers)

    # The declared `arxml_version`, not `resolved_arxml_version`: the manifest's
    # own spelling is the answer to which AUTOSAR release the exporter emits.
    response = DiscoverResponse(
        project=Passport(
            name=project.name,
            arxml_version=project.arxml_version,
            swcraft_version=__version__,
        ),
        baseline=baseline_token(model_root),
        # Read off `read.py`, never restated: a copy is how a server comes to
        # advertise a bound it does not enforce.
        reads=read.reads_capability(discover_default),
        form=served,
        layers=layers,
    ).to_json()
    _refuse_over_budget(response, layer, served, registry)
    return response


def _strip_descriptions_and_defaults(layer: dict[str, Any]) -> dict[str, Any]:
    """One published ``full`` layer descriptor as its ``summary``.

    Only removes, so a newly declared descriptor key is in both forms. Drops every
    ``description`` key at every depth, then applies
    :data:`SUMMARY_OMITTED_DEFAULTS` at the layer and at each field. A row at a
    third position needs a third loop here.
    """
    summary = _strip_descriptions(layer)
    for key, default in _LAYER_ROWS:
        if summary.get(key) is default:
            del summary[key]
    for element_kind in summary["element_kinds"]:
        for field in element_kind["fields"]:
            for key, default in _FIELD_ROWS:
                if field.get(key) is default:
                    del field[key]
    return summary


def _strip_descriptions(value: Any) -> Any:
    """*value* with every ``description`` key removed, at every depth — a new copy.

    It removes the **key**, never a value, so a field *named* ``description``
    keeps its row. The fresh containers let the caller drop defaults in place.
    """
    if isinstance(value, dict):
        return {key: _strip_descriptions(item) for key, item in value.items() if key != "description"}
    if isinstance(value, list):
        return [_strip_descriptions(item) for item in value]
    return value


def _refuse_over_budget(
    response: dict[str, Any], layer: str | None, served: DiscoverForm, registry: reg.Registry
) -> None:
    """The refusal of *response* if it would exceed the server's budget.

    The same comparison a read makes, with ``read.py``'s constant and
    :func:`~swcraft_tools.editing.read.measure`, so the budget published and the
    budgets enforced are one constant. What is measured is the whole response,
    exactly what is sent. ``measured.elements`` is the layer descriptor count.
    """
    size = read.measure(response)
    if size <= read.RESPONSE_BUDGET_WIRE_BYTES:
        return
    raise read.OverBudget(
        address=None,
        measured=Measured(
            wire_bytes=size,
            budget=read.RESPONSE_BUDGET_WIRE_BYTES,
            elements=len(response["layers"]),
        ),
        hints=narrower_discovers(layer, served, registry),
    )


def narrower_discovers(layer: str | None, served: DiscoverForm, registry: reg.Registry) -> tuple[Hint, ...]:
    """The narrower discoveries a refused one offers, in chain order.

    First the same scope at ``summary`` (when ``full`` refused), then one request
    per declared layer at the **requested** form (when the whole project was asked
    for). A layer-narrowed ``summary`` has none.
    """
    hints: list[Hint] = []
    if served == "full":
        hints.append(DiscoverRequest(layer=layer, form="summary"))
    if layer is None:
        hints.extend(DiscoverRequest(layer=name, form=served) for name in registry.layer_names)
    return tuple(hints)


def _is_present(descriptor: reg.LayerDescriptor, model_root: Path, registry: reg.Registry) -> bool:
    """The one probe: whether this layer's directory exists in this project.

    Deliberately "exists", not "holds elements": a present-but-empty layer is a
    different state from an absent one (``hardware.layer-absent`` and
    ``hardware.no-processor`` report them separately). The protocol never removes
    a directory, so removing a layer's last element leaves ``present: true``.
    """
    return (model_root / registry.storage(descriptor.name).directory).is_dir()


def _publish_layer(descriptor: reg.LayerDescriptor, model_root: Path, registry: reg.Registry) -> dict[str, Any]:
    """One ``$defs/layerDescriptor``, with ``present`` probed and the rest copied.

    *registry* is passed so the descriptor and the probed storage record come
    from the same registry.
    """
    present = _is_present(descriptor, model_root, registry)
    published: dict[str, Any] = {
        "layer": descriptor.name,
        "optional": descriptor.optional,
        "present": present,
        "identity_field": descriptor.identity_field,
    }
    # `described(present=…)`: a contract may declare a separate description for
    # an absent layer (no shipped layer does).
    _set_if(published, "description", descriptor.described(present=present))
    published["element_kinds"] = [_publish_kind(kind) for kind in descriptor.element_kinds]
    return published


def _publish_kind(descriptor: reg.ElementKindDescriptor) -> dict[str, Any]:
    """One ``$defs/elementKindDescriptor``.

    ``member_only`` and ``member_of`` travel together: a kind that has no file
    and no ``{layer, element}`` address publishes where it *does* appear, so a
    client can construct a member address without inferring the owner.
    """
    published: dict[str, Any] = {"kind": descriptor.kind}
    _set_if(published, "discriminator_field", descriptor.discriminator_field)
    if descriptor.member_only:
        published["member_only"] = True
        published["member_of"] = [{"kind": owner, "field": field} for owner, field in descriptor.member_of]
    _set_if(published, "description", descriptor.description)
    published["fields"] = [_publish_field(field) for field in descriptor.fields]
    return published


def _publish_field(descriptor: reg.FieldDescriptor) -> dict[str, Any]:
    """One ``$defs/fieldDescriptor``.

    The placement field (``swc.group``) is published like any other field, so a
    client authors and reads it without learning that it is a directory;
    ``FieldDescriptor.placement`` is not published.
    """
    published: dict[str, Any] = {
        "name": descriptor.name,
        "type": descriptor.type,
        "required": descriptor.required,
        "immutable": descriptor.immutable,
    }
    _set_if(published, "unit", descriptor.unit)
    _set_if(published, "description", descriptor.description)
    if descriptor.enum:
        published["enum"] = list(descriptor.enum)
    if descriptor.reference is not None:
        published["reference"] = {
            "layer": descriptor.reference.layer,
            "identity_field": descriptor.reference.identity_field,
        }
    _set_if(published, "element_kind", descriptor.element_kind)
    if descriptor.collection is not None:
        published["collection"] = _publish_collection(descriptor.collection)
    return published


def _publish_collection(descriptor: reg.CollectionDescriptor) -> dict[str, Any]:
    """One ``$defs/collectionDescriptor``.

    ``member_identity`` is unconditional: without it a client cannot construct a
    member address at all, since the address form has no ordinal step.
    """
    published: dict[str, Any] = {"kind": descriptor.kind, "member_identity": descriptor.member_identity}
    _set_if(published, "identity_field", descriptor.identity_field)
    if descriptor.identity_fields:
        published["identity_fields"] = list(descriptor.identity_fields)
    if descriptor.member_kinds:
        published["member_kinds"] = list(descriptor.member_kinds)
    _set_if(published, "discriminator_field", descriptor.discriminator_field)
    return published


def _set_if(target: dict[str, Any], key: str, value: Any) -> None:
    """*key* set only when *value* is present: an optional key is omitted, never null."""
    if value:
        target[key] = value
