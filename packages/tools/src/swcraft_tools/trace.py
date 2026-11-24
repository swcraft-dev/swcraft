"""The trace link grammar and the five linkable layers (contract ``11.0.0``).

A trace link lives in the architecture element, never in the requirement: an element names the requirements
it satisfies in its own ``satisfies`` list, and a requirement may name what it derives from in
``derived_from``. This module is the one reader of the two spellings those lists hold.

**The link grammar.** A requirement reference is ``<id>@<version>`` -- one ``@``, a positive decimal integer
with no leading zero -- or the bare ``<id>``. :func:`parse_link` splits a member at its first ``@`` (an id
holds neither ``@`` nor ``:``); a suffix that is no such integer leaves ``version`` ``None`` and keeps the raw
text in ``suffix``. A version of any width is converted exactly and never crashes the gate.

**The element form.** A member holding any ``:`` is ``<token>:<name>`` (``hardware:Core0``), the token one of
:data:`LINKABLE_LAYERS`' (:func:`parse_element_reference`, :func:`is_element_form`), and names no requirement;
:func:`component_requirement_ids` and :func:`satisfying_elements` skip it, as the relation resolver does.

**The allocation.** :func:`component_requirement_ids` is THE reading of which requirements a component is
allocated: its own ``satisfies``, then each port's, then each runnable's, in file order, each id once, the
version taking no part. :func:`allocated_requirement_ids` is its union over every file of one component name.
A link on a composition, an interface, a task or a hardware element allocates no level.

**The confirmation reading.** :func:`stale_link` classifies the two version warnings
(``trace.link-version-not-stated``, ``trace.link-needs-confirmation``) over :func:`requirement_versions`' map.
:func:`link_confirmed` is the one reading of *confirmed* the Sphinx-Needs export publishes: a stated version, an
id declared once, not a stub, the version current.

It resolves nothing against the model on its own and mints no finding: an undeclared id, a stale version and an
undeclared element are ``check-model``'s. A **leaf**: it imports :mod:`swcraft_tools.schema`,
:mod:`swcraft_tools.diagnostics` and the five ``reader.read_*`` of the linkable layers, and nothing else.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any, NamedTuple, get_args, get_type_hints

from swcraft_tools.diagnostics import SubjectStep, field_step, member_step
from swcraft_tools.reader import read_compositions, read_hardware, read_interfaces, read_swc_model, read_tasks
from swcraft_tools.schema import (
    Composition,
    Core,
    CSInterface,
    ExternalInterface,
    Memory,
    Partition,
    Processor,
    Requirement,
    SRInterface,
    SWComponent,
    Task,
)

__all__ = [
    "LINKABLE_LABELS",
    "LINKABLE_LAYERS",
    "LinkSite",
    "LinkedElement",
    "DuplicateLink",
    "ParsedLink",
    "StaleLink",
    "StateLinkSite",
    "allocated_requirement_ids",
    "component_requirement_ids",
    "duplicate_links",
    "duplicate_parents",
    "element_link_sites",
    "is_element_form",
    "is_version",
    "link_confirmed",
    "link_site_where",
    "link_sites",
    "link_sites_of_state",
    "parse_element_reference",
    "parse_link",
    "requirement_stubs",
    "requirement_versions",
    "satisfying_elements",
    "satisfying_elements_of",
    "site_kind",
    "site_kind_layer",
    "stale_link",
]

#: Token -> layer name, the five layers whose elements may carry ``satisfies`` and be named by a
#: ``derived_from`` element reference. The tokens are the contract's own ``token`` of each layer.
LINKABLE_LAYERS: Mapping[str, str] = {
    "swc": "swc",
    "interface": "interfaces",
    "composition": "compositions",
    "task": "tasks",
    "hardware": "hardware",
}

#: Layer -> the *label* :func:`link_site_where` takes for that layer (``SWC 'Brake'``).
LINKABLE_LABELS: Mapping[str, str] = {
    "swc": "SWC",
    "interfaces": "Interface",
    "compositions": "Composition",
    "tasks": "Task",
    "hardware": "Hardware element",
}

_LINK_FIELD = "satisfies"

#: A top-level linkable element that is not a component: the kinds whose own ``satisfies`` is their one site.
LinkedElement = (
    SRInterface | CSInterface | ExternalInterface | Composition | Task | Processor | Core | Memory | Partition
)

_VERSION = re.compile(r"[1-9][0-9]*")

#: The digits converted per ``int()`` call: below 640, the smallest non-zero
#: ``sys.set_int_max_str_digits()`` accepts, so no configured limit applies.
_DIGITS_PER_CHUNK = 600


class ParsedLink(NamedTuple):
    """One requirement reference split at its first ``@``."""

    #: The text before the first ``@`` -- the requirement's id.
    id: str
    #: The suffix as an integer when it is a version, exactly, at any width; ``None`` otherwise.
    version: int | None
    #: The raw text after the first ``@``; ``None`` when there is no ``@``.
    suffix: str | None


class LinkSite(NamedTuple):
    """One ``satisfies`` member of an element: its subject path, its value and its owner."""

    #: The path from the top-level element to the member, as a finding's subject spells it.
    steps: tuple[SubjectStep, ...]
    value: str
    #: ``("port", <name>)`` or ``("runnable", <name>)`` when a component's port or
    #: runnable holds the member; ``None`` when the element's own list does.
    owner: tuple[str, str] | None = None


def is_version(suffix: str | None) -> bool:
    """Whether *suffix* is a version: a positive decimal integer with no leading zero."""
    return suffix is not None and _VERSION.fullmatch(suffix) is not None


def _version_value(digits: str) -> int:
    """The exact integer *digits* spell, converted in chunks so ``int()``'s digit limit never applies."""
    value = 0
    for start in range(0, len(digits), _DIGITS_PER_CHUNK):
        chunk = digits[start : start + _DIGITS_PER_CHUNK]
        value = value * 10 ** len(chunk) + int(chunk)
    return value


def parse_link(value: str) -> ParsedLink:
    """*value* split at its first ``@``, unconditionally.

    ``"REQ-12@3"`` -> ``("REQ-12", 3, "3")``; ``"REQ-12"`` -> ``("REQ-12", None, None)``;
    ``"REQ-12@x"`` -> ``("REQ-12", None, "x")``.
    """
    head, separator, suffix = value.partition("@")
    if not separator:
        return ParsedLink(value, None, None)
    return ParsedLink(head, _version_value(suffix) if is_version(suffix) else None, suffix)


def parse_element_reference(value: str) -> tuple[str, str] | None:
    """*value* as ``(layer, name)`` when it is in the element form, else ``None``.

    ``"hardware:Core0"`` -> ``("hardware", "Core0")`` (the LAYER name, resolved from the token);
    ``None`` unless the text before the first ``:`` is a key of :data:`LINKABLE_LAYERS`.
    """
    token, separator, name = value.partition(":")
    if not separator or token not in LINKABLE_LAYERS:
        return None
    return LINKABLE_LAYERS[token], name


def is_element_form(value: str) -> bool:
    """Whether *value* is in the element form: it holds a ``:``, which no requirement ``id`` may.

    True for a well-formed element reference and for a token of no linkable layer alike.
    """
    return ":" in value


#: What Sphinx-Needs 9.2.0 reads as the end of the need id in a link value:
#: ``NeedLink.parse_address`` splits the address at its first ``.`` and reads the
#: rest as a need part, under every configuration.
NEED_PART_SEPARATOR = "."


def unpublishable_link_id(requirement_id: str) -> bool:
    """Whether a link to *requirement_id* cannot be published in ``needs.json``: the id holds ``.``.

    The export refuses such a link and ``check-model`` warns on it (``trace.link-unpublishable-id``). A ``[`` is
    publishable: the guide's declarations turn Sphinx-Needs' condition parsing off on SWCraft's link types.
    """
    return NEED_PART_SEPARATOR in requirement_id


def _requirement_ids(values: list[str]) -> Iterator[str]:
    """The id of each member that is a requirement reference, skipping the element form."""
    for value in values:
        if not is_element_form(value):
            yield parse_link(value).id


def link_sites(swc: SWComponent) -> Iterator[LinkSite]:
    """Every ``satisfies`` member of the component: its own, each port's, each runnable's, in file order."""
    for value in swc.satisfies:
        yield LinkSite((field_step(_LINK_FIELD), member_step(value)), value)
    for port in swc.ports:
        for value in port.satisfies:
            yield LinkSite(
                (field_step("ports"), member_step(port.name), field_step(_LINK_FIELD), member_step(value)),
                value,
                ("port", port.name),
            )
    for runnable in swc.runnables:
        for value in runnable.satisfies:
            yield LinkSite(
                (field_step("runnables"), member_step(runnable.name), field_step(_LINK_FIELD), member_step(value)),
                value,
                ("runnable", runnable.name),
            )


def element_link_sites(element: LinkedElement) -> Iterator[LinkSite]:
    """The ``satisfies`` members of a non-component linkable element, its own list alone."""
    for value in element.satisfies:
        yield LinkSite((field_step(_LINK_FIELD), member_step(value)), value)


def link_site_where(label: str, element: str, site: LinkSite) -> str:
    """``SWC 'Brake'``, ``SWC 'Brake' (port 'pOut')``: the element and, below it, the member that holds *site*."""
    where = f"{label} '{element}'"
    if site.owner is not None:
        kind, name = site.owner
        where += f" ({kind} '{name}')"
    return where


class DuplicateLink(NamedTuple):
    """One target a single link list names more than once (``trace.duplicate-link``)."""

    #: A requirement's id, or, for a ``derived_from`` member in the element form, the element reference as written.
    target: str
    #: Every member of the list naming the target, in file order; two or more.
    sites: tuple[LinkSite, ...]

    @property
    def names_an_element(self) -> bool:
        """Whether :attr:`target` is an element reference (``<token>:<name>``) rather than a requirement id."""
        return is_element_form(self.target)

    @property
    def repeats(self) -> tuple[tuple[SubjectStep, ...], ...]:
        """The subject path of each member after the first, one per occurrence: what the removes repair.

        A path is listed as many times as its member must be removed: ``R@1`` twice is that path once,
        ``[R@1, R@2, R@2]`` is ``R@2``'s path twice.
        """
        return tuple(site.steps for site in self.sites[1:])


def _holder_lists(element: SWComponent | LinkedElement) -> Iterator[list[LinkSite]]:
    """The ``satisfies`` sites of *element*, one list per holder: its own, then each port's and runnable's.

    Split by each holder's own list length, so two ports of one name stay two lists.
    """
    if not isinstance(element, SWComponent):
        yield list(element_link_sites(element))
        return
    sites = list(link_sites(element))
    lengths = [
        len(element.satisfies),
        *(len(port.satisfies) for port in element.ports),
        *(len(runnable.satisfies) for runnable in element.runnables),
    ]
    start = 0
    for length in lengths:
        yield sites[start : start + length]
        start += length


def _repeated(holder: Iterable[LinkSite], *, element_form: bool) -> Iterator[DuplicateLink]:
    """The targets one list names twice or more, in the order of their first member.

    A requirement-form member is keyed by its id, so ``R-1@1`` beside ``R-1@2`` is a repeat, and a malformed suffix
    counts by its id too. An element-form member is keyed by its text when *element_form* is true and skipped
    otherwise.
    """
    groups: dict[str, list[LinkSite]] = {}
    for site in holder:
        if is_element_form(site.value):
            if element_form:
                groups.setdefault(site.value, []).append(site)
            continue
        groups.setdefault(parse_link(site.value).id, []).append(site)
    for target, members in groups.items():
        if len(members) > 1:
            yield DuplicateLink(target, tuple(members))


def duplicate_links(element: SWComponent | LinkedElement) -> Iterator[DuplicateLink]:
    """Each requirement one ``satisfies`` list of *element* names twice or more.

    A list is a holder's: the element's own, or one port's or one runnable's -- a component and its port naming
    one requirement are no duplicate. An element-form member is skipped.
    """
    for holder in _holder_lists(element):
        yield from _repeated(holder, element_form=False)


def duplicate_parents(requirement: Requirement) -> Iterator[DuplicateLink]:
    """Each target the requirement's ``derived_from`` names twice or more: a parent requirement, or an element.

    A requirement parent is keyed by its id; an element reference by its text.
    """
    sites = (LinkSite((field_step("derived_from"), member_step(value)), value) for value in requirement.derived_from)
    yield from _repeated(sites, element_form=True)


def component_requirement_ids(swc: SWComponent) -> tuple[str, ...]:
    """THE allocation reading: the ids the component's own ``satisfies``, its ports' and its runnables' name.

    In file order, each once.
    """
    return tuple(dict.fromkeys(_requirement_ids([site.value for site in link_sites(swc)])))


def allocated_requirement_ids(swcs: Iterable[SWComponent]) -> dict[str, set[str]]:
    """Component name -> the ids :func:`component_requirement_ids` reads, unioned over every file of that name.

    A component satisfying nothing maps to the empty set.

    Keyed by name, so two files declaring one component share one entry.
    """
    allocated: dict[str, set[str]] = {}
    for swc in swcs:
        allocated.setdefault(swc.name, set()).update(component_requirement_ids(swc))
    return allocated


def satisfying_elements(model_root: str) -> dict[str, tuple[str, ...]]:
    """Requirement id -> the names of the top-level elements that satisfy it, read from disk.

    Returns:
        The inverse :func:`satisfying_elements_of` computes over the five linkable layers.

    Raises:
        ValueError: if a file of one of the five layers is malformed
            (propagated from the reader).
    """
    swcs = read_swc_model(model_root)
    interfaces = read_interfaces(model_root)
    compositions = read_compositions(model_root)
    tasks = read_tasks(model_root)
    hardware = read_hardware(model_root)
    return satisfying_elements_of(swcs, interfaces, compositions, tasks, hardware)


def satisfying_elements_of(
    swcs: Iterable[SWComponent],
    interfaces: Iterable[LinkedElement],
    compositions: Iterable[LinkedElement],
    tasks: Iterable[LinkedElement],
    hardware: Iterable[LinkedElement],
) -> dict[str, tuple[str, ...]]:
    """Requirement id -> the names of the top-level elements that satisfy it, over loaded layers.

    A port's or a runnable's link counts as its component's. The names are sorted and deduplicated by bare name,
    so a component and a task of one name are one entry. An id naming no declared requirement is kept.
    """
    inverse: dict[str, set[str]] = {}
    for swc in swcs:
        for requirement_id in component_requirement_ids(swc):
            inverse.setdefault(requirement_id, set()).add(swc.name)
    for layer in (interfaces, compositions, tasks, hardware):
        for element in layer:
            for requirement_id in _requirement_ids(element.satisfies):
                inverse.setdefault(requirement_id, set()).add(element.name)
    return {requirement_id: tuple(sorted(names)) for requirement_id, names in inverse.items()}


#: The site kind of a top-level element's own ``satisfies`` list, by layer; a
#: hardware element's is its own ``kind`` (:func:`site_kind`).
_SITE_KINDS: Mapping[str, str] = {
    "swc": "swc",
    "interfaces": "interface",
    "compositions": "composition",
    "tasks": "task",
}


class StateLinkSite(NamedTuple):
    """One ``satisfies`` member of a loaded model: the layer, the top-level element holding it, the site."""

    #: One of the five layer names of :data:`LINKABLE_LAYERS`.
    layer: str
    #: The top-level element: a :class:`~swcraft_tools.schema.SWComponent` or a :data:`LinkedElement`.
    element: SWComponent | LinkedElement
    #: The member, its subject path and its owner (a port or a runnable of a component).
    site: LinkSite


def link_sites_of_state(layers: Mapping[str, Iterable[Any]]) -> Iterator[StateLinkSite]:
    """Every ``satisfies`` member of a loaded model, stubs included, in the contract's layer order.

    *layers* maps a layer name to its elements, as ``ModelState.layers`` holds them; a layer it does not carry has
    no site. The order is ``swc`` (:func:`link_sites`), ``interfaces``, ``compositions``, ``tasks``, ``hardware``,
    then each layer's element order, then the element's file order.

    **What it does not reach:** it reads no requirement and parses no member -- a bare id, a malformed suffix and
    an element-form member are sites like any other, which the caller tells apart with :func:`parse_link` and
    :func:`is_element_form`.
    """
    for swc in layers.get("swc", ()):
        for site in link_sites(swc):
            yield StateLinkSite("swc", swc, site)
    for layer in ("interfaces", "compositions", "tasks", "hardware"):
        for element in layers.get(layer, ()):
            for site in element_link_sites(element):
                yield StateLinkSite(layer, element, site)


def site_kind(layer: str, element: SWComponent | LinkedElement, site: LinkSite) -> str:
    """The kind of what holds *site*, *element*'s member in *layer*: the vocabulary the trace search's addresses use.

    ``port`` or ``runnable`` for a component's port or runnable, ``swc`` for the component's own list,
    ``interface``, ``composition`` and ``task`` for those layers' elements, and a hardware element's own ``kind``
    (``processor``, ``core``, ``memory``, ``partition``).
    """
    if site.owner is not None:
        return site.owner[0]
    if isinstance(element, _HARDWARE_ELEMENTS):
        return element.kind
    return _SITE_KINDS[layer]


#: The hardware element classes, each holding its own ``satisfies`` list under its own ``kind``.
_HARDWARE_ELEMENTS = (Processor, Core, Memory, Partition)

#: A site kind -> the layer of the top-level element holding a site of that kind: :func:`site_kind`'s inverse.
_SITE_KIND_LAYERS: Mapping[str, str] = {
    **{kind: layer for layer, kind in _SITE_KINDS.items()},
    "port": "swc",
    "runnable": "swc",
    **{get_args(get_type_hints(element_class)["kind"])[0]: "hardware" for element_class in _HARDWARE_ELEMENTS},
}


def site_kind_layer(kind: str) -> str:
    """The layer holding the top-level element of a site of *kind* (:func:`site_kind`'s vocabulary).

    A ``port`` or ``runnable`` site is its component's, so ``swc``; the hardware kinds answer ``hardware``.
    Raises :class:`KeyError` on a kind of no site.
    """
    return _SITE_KIND_LAYERS[kind]


# ---------------------------------------------------------------------------
# The confirmation reading: here, not in the validator, because the Sphinx-Needs
# writer may not import the validator and must read the same classification.
# ---------------------------------------------------------------------------


class StaleLink(NamedTuple):
    """One requirement-form link whose stated version is not its requirement's, or states none."""

    #: The requirement the link names, declared by exactly one file.
    requirement_id: str
    #: The version the link states; ``None`` for a bare id (``trace.link-version-not-stated``).
    stated: int | None
    current: int


def requirement_versions(requirements: Iterable[Requirement]) -> dict[str, list[int]]:
    """Every declared requirement id -> its files' versions, in reader order: :func:`stale_link`'s *versions*.

    Two entries for one id are ``name.duplicate-requirement``'s finding, and :func:`stale_link` abstains on them.
    """
    versions: dict[str, list[int]] = {}
    for requirement in requirements:
        versions.setdefault(requirement.id, []).append(requirement.version)
    return versions


def requirement_stubs(requirements: Iterable[Requirement]) -> set[str]:
    """The ids of the stub requirements, the placeholders an import created: :func:`link_confirmed`'s *stubs*."""
    return {requirement.id for requirement in requirements if requirement.stub}


def stale_link(value: str, versions: Mapping[str, Sequence[int]]) -> StaleLink | None:
    """Whether the link *value* needs confirming: the classification of the two version warnings.

    *versions* maps every declared requirement id to its files' versions, in reader order. ``None`` when the link
    states the requirement's current version, and on the rules' **abstentions**: an id no file declares or two
    files do (there is no one version to compare) and a suffix that is not a version (another rule's finding).
    Otherwise a :class:`StaleLink`, its ``stated`` ``None`` for a bare id (``trace.link-version-not-stated``) and
    the stated version where it differs (``trace.link-needs-confirmation``). Compared as exact integers.

    What it does not reach: whether the member is in the element form -- such a member's id names no
    requirement, so it is an abstention here like any undeclared id.
    """
    requirement_id, version, suffix = parse_link(value)
    declared = versions.get(requirement_id, ())
    if len(declared) != 1 or (suffix is not None and version is None):
        return None
    current = declared[0]
    if suffix is not None and version == current:
        return None
    return StaleLink(requirement_id, version, current)


def link_confirmed(value: str, versions: Mapping[str, Sequence[int]], stubs: AbstractSet[str]) -> bool:
    """Whether the requirement-form link *value* is confirmed.

    The one classification the Sphinx-Needs export publishes as ``satisfies`` against
    ``satisfies_unconfirmed``.

    ``True`` exactly when the link states a version, its id is declared by exactly one requirement file
    (*versions*), that requirement is not a stub (*stubs*: a stub has no text to confirm against) and the stated
    version is the requirement's ``version``. ``False`` for every other link: a bare id, a stale ``@N``, a link to
    a stub, and the three cases :func:`stale_link` abstains on -- an id no file declares, an id two files declare,
    a suffix that is not a version (``@x``, ``@01``). A link ``check-model`` warns about is never confirmed here.

    What it does not reach: an element-form member (``swc:Brake``) names no requirement and answers ``False``
    like any undeclared id -- the caller routes it before asking.
    """
    requirement_id, version, _suffix = parse_link(value)
    if version is None or requirement_id in stubs or len(versions.get(requirement_id, ())) != 1:
        return False
    return stale_link(value, versions) is None
