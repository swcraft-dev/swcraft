"""The architecture published to the requirements team: one Sphinx-Needs ``needs.json``.

``generate_needs()`` writes the file ``outputs.needs`` names (default ``dist/needs.json``): a ``needs.json`` of
format 1, built in memory by :func:`build_needs_json`. Refusals: :class:`NeedsExportRefused`.

**What it publishes.** Every architecture requirement as a need of type ``arch_req`` or ``arch_spec`` (by
``req_type``), with its declared non-``QM`` level as ``asil``; and every element of the five linkable layers that
carries a ``satisfies`` member, or that a ``derived_from`` names in the element form, as an ``arch_element`` with
its kind in ``tags``, under a kind-and-path id (``SWC-Brake``, ``PORT-Brake-pTemp``; a port or runnable is a need
of its own). Every need carries ``swcraft_path``, the model-root-relative POSIX storage name of the file declaring
it (a port's is its component's). A requirement-form link is ``satisfies``/``derived_from`` when
:func:`swcraft_tools.trace.link_confirmed` confirms it, ``*_unconfirmed`` otherwise; an element-form
``derived_from`` is confirmed when the element is in the model. ``current_version`` is the ``baseline`` the
non-stub incoming requirements carry, or ``""``. A ``[`` in a link's target id is published as it stands.

**What it never publishes.** An incoming requirement (``source: external``) is named only by its id in the links
that reach it. No ``source``, ``stub``, ``baseline``, ``revision``, ``document`` or SWCraft ``version`` reaches a
need.

**What it does not reach.** A requirement-form link is not resolved against the model. A confirmed link to an
incoming requirement resolves in the team's build only while the team still holds it. The ``[[`` rule never
judges ``swcraft_path``. Whether the team's ``conf.py`` declares the need types, fields and link types is not
checked. A blank title or description is published as it stands.
"""

from __future__ import annotations

import contextlib
import json
import os
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path

from swcraft_tools.exclusive_create import open_unique_sibling
from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import stored_storage_names
from swcraft_tools.reader import (
    read_compositions,
    read_hardware,
    read_interfaces,
    read_requirements,
    read_swc_model,
    read_tasks,
)
from swcraft_tools.safety import NO_ASIL, parse_asil
from swcraft_tools.trace import (
    element_link_sites,
    is_element_form,
    link_confirmed,
    link_sites,
    parse_element_reference,
    parse_link,
    requirement_stubs,
    requirement_versions,
    unpublishable_link_id,
)

#: An architecture requirement's ``req_type`` -> its need type.
REQUIREMENT_TYPES: Mapping[str, str] = {"req": "arch_req", "spec": "arch_spec"}

#: The need type of every published architecture element; its kind goes into ``tags``.
ELEMENT_TYPE = "arch_element"

#: The four link types, by the source field and whether the link is confirmed.
SATISFIES = "satisfies"
SATISFIES_UNCONFIRMED = "satisfies_unconfirmed"
DERIVED_FROM = "derived_from"
DERIVED_FROM_UNCONFIRMED = "derived_from_unconfirmed"

#: A confirmed link type -> its unconfirmed one.
_UNCONFIRMED: Mapping[str, str] = {SATISFIES: SATISFIES_UNCONFIRMED, DERIVED_FROM: DERIVED_FROM_UNCONFIRMED}

#: The field every need carries: the model-root-relative storage name of the file declaring it.
SWCRAFT_PATH = "swcraft_path"

#: Linkable layer -> the id prefix of its elements. The prefix belongs to the
#: layer, not the kind: the kinds that share one share a name space in the model
#: too (``name.duplicate-interface``, ``rule:hardware.duplicate-name``).
_LAYER_PREFIX: Mapping[str, str] = {
    "swc": "SWC",
    "interfaces": "IF",
    "compositions": "COMP",
    "tasks": "TASK",
    "hardware": "HW",
}

#: A component member kind -> the id prefix of its published need.
_MEMBER_PREFIX: Mapping[str, str] = {"port": "PORT", "runnable": "RUN"}

#: What Sphinx-Needs reads as the start of a dynamic function in a need's title and content.
_DYNAMIC_FUNCTION = "[["

#: How a delivery recorded nowhere is named in the several-deliveries refusal.
_NO_DELIVERY = "none"


class NeedsExportRefused(ValueError):
    """The file cannot be written as the model stands; raised before anything is written.

    Six causes, every instance joined into one text: two needs under one id; a need whose id is an incoming
    requirement's, which the team's own need would replace in their build; a title or a description holding ``[[``
    (:func:`_dynamic_functions`); a link whose target id holds ``.``, which Sphinx-Needs reads as an id and a part;
    a published element more than one file declares, so ``swcraft_path`` would name no one file; and incoming
    requirements from more than one delivery. Its text names each side by ``requirement <id>`` or
    ``element <need id>``, never a path, and is the CLI's ``Error:`` line as it stands.
    """


def _breaks_line(text: str) -> bool:
    """Whether *text* holds a line break as :meth:`str.splitlines` reads one."""
    return text.splitlines() != ([text] if text else [])


def _shown(text: str) -> str:
    """*text* for a refusal sentence: each line break spelled as its escape, so the sentence stays one line."""
    return "".join(char.encode("unicode_escape").decode("ascii") if _breaks_line(char) else char for char in text)


def _as_judged(text: str) -> str:
    """*text* as :func:`_dynamic_functions` judges it: every backslash, whitespace and Unicode ``C*`` character removed.

    ``C*`` is every category of Unicode's *Other* class, the characters that print nothing.
    """
    return "".join(
        char for char in text if char != "\\" and not char.isspace() and unicodedata.category(char)[0] != "C"
    )


def _dynamic_functions(side: str, values: Iterable[tuple[str, str]]) -> list[str]:
    """One refusal clause per value of *values* -- a title or a description -- that holds ``[[`` as judged.

    Sphinx-Needs evaluates a dynamic function in an external need's title. The widening by backslashes, whitespace
    and ``C*`` characters refuses no realistic text and costs nothing.
    """
    return [
        f"{side} cannot be published: its {field} holds {_DYNAMIC_FUNCTION} once its backslashes, whitespace and "
        "non-printing characters are removed, which Sphinx-Needs reads as a dynamic function in a need's title "
        "or content"
        for field, value in values
        if value and _DYNAMIC_FUNCTION in _as_judged(value)
    ]


def _element_id(layer: str, name: str) -> str:
    """The id of an element of a linkable *layer*: the layer's prefix, then its name as written."""
    return f"{_LAYER_PREFIX[layer]}-{name}"


def _member_id(kind: str, swc_name: str, member_name: str) -> str:
    """The id of a component's port or runnable: the kind's prefix, the component's name, its own."""
    return f"{_MEMBER_PREFIX[kind]}-{swc_name}-{member_name}"


def _split(
    values: Iterable[str],
    versions: Mapping[str, Sequence[int]],
    stubs: AbstractSet[str],
    held: AbstractSet[tuple[str, str]] | None,
) -> tuple[list[str], list[str]]:
    """The confirmed and the unconfirmed target ids of *values*, each once, in file order.

    An id a confirmed member names is not repeated among the unconfirmed. With *held* (the ``(layer, name)`` of every
    element, given for ``derived_from``) an element reference is written by the element's need id, confirmed when the
    element is held. Without it (``satisfies``) an element-form member is skipped. A member that names nothing
    (``@3``, ``swc:``, ``foo:bar``) writes nothing.
    """
    confirmed: dict[str, None] = {}
    unconfirmed: dict[str, None] = {}
    for value in values:
        reference = parse_element_reference(value) if held is not None else None
        if reference is not None and held is not None:
            if reference[1]:
                (confirmed if reference in held else unconfirmed).setdefault(_element_id(*reference))
            continue
        if is_element_form(value):
            continue
        target = parse_link(value).id
        if not target:
            continue
        if link_confirmed(value, versions, stubs):
            confirmed.setdefault(target)
        else:
            unconfirmed.setdefault(target)
    return list(confirmed), [target for target in unconfirmed if target not in confirmed]


class _Collector:
    """The needs in reading order, who claims each id, and every refusal clause found while reading.

    Only ``needs``, ``claims`` and ``problems`` are read from outside, by :func:`build_needs_json`.
    """

    def __init__(
        self,
        versions: Mapping[str, Sequence[int]],
        stubs: AbstractSet[str],
        held: AbstractSet[tuple[str, str]],
        incoming: AbstractSet[str],
        model_root: str,
    ) -> None:
        self._versions = versions
        self._stubs = stubs
        self._held = held
        self._incoming = incoming
        self.needs: list[tuple[str, dict[str, object]]] = []
        self.claims: dict[str, list[str]] = {}
        self.problems: list[str] = []
        self._unplaced: dict[str, str] = {}
        self._root = Path(model_root)
        self._stored: dict[str, dict[str, list[str]]] = {}

    def storage_names(self, layer: str, identity: str) -> list[str]:
        """The model-root-relative POSIX storage names of the files of *layer* that declare *identity*.

        The layer is listed once, the first time it is asked for.
        """
        stored = self._stored.get(layer)
        if stored is None:
            stored = self._stored[layer] = stored_storage_names(self._root, layer, registry=current_registry())
        return list(stored.get(identity, ()))

    def unplaced_clauses(self) -> list[str]:
        """One clause per need whose element several files declare, unless its id is refused anyway, in id order."""
        return [
            clause
            for need_id, clause in sorted(self._unplaced.items())
            if len(self.claims.get(need_id, ())) == 1 and need_id not in self._incoming
        ]

    def add(
        self,
        side_kind: str,
        need: dict[str, object],
        judged: Iterable[tuple[str, str]],
        field: str,
        values: Iterable[str],
        files: Sequence[str],
        declared: str = "it",
    ) -> None:
        """*need* recorded under its id, its ``swcraft_path``, its *field* links and the refusal clauses of its side.

        *side_kind* (``requirement`` or ``element``) names the side in a refusal; *judged* are the ``(field, value)``
        pairs the ``[[`` rule reads; *field* is ``satisfies`` or ``derived_from`` (which reads the element form too).
        *files* are the storage names declaring the need's element; for more than one, the clause naming *declared* is
        kept aside (:meth:`unplaced_clauses`), because a need refused anyway by its id would repeat that refusal.
        """
        need_id = str(need["id"])
        side = f"{side_kind} {_shown(need_id)}"
        if files:
            need[SWCRAFT_PATH] = files[0]
        if len(files) > 1:
            self._unplaced[need_id] = (
                f"{side} cannot be published: more than one file declares {declared}, so its {SWCRAFT_PATH} "
                "would not name the one file that holds it"
            )
        self.problems.extend(_dynamic_functions(side, judged))
        if need_id in self._incoming:
            self.problems.append(
                f"{side} cannot be published: its id {_shown(need_id)} is an incoming requirement's id, and the "
                "requirements team's own need would replace it in their build"
            )
        held = self._held if field == DERIVED_FROM else None
        confirmed, unconfirmed = _split(values, self._versions, self._stubs, held)
        for target in (*confirmed, *unconfirmed):
            if unpublishable_link_id(target):
                self.problems.append(
                    f"{side} cannot be published: its {field} names {_shown(target)}, an id holding '.', which "
                    "Sphinx-Needs reads as an id and a part under every configuration, so the link would name "
                    "another need or none; SWCraft does not support such an id in a published link"
                )
        if confirmed:
            need[field] = confirmed
        if unconfirmed:
            need[_UNCONFIRMED[field]] = unconfirmed
        self.claims.setdefault(need_id, []).append(side)
        self.needs.append((need_id, need))

    def collisions(self) -> list[str]:
        """One clause per id more than one need claims, in id order."""
        return [
            f"the id {_shown(need_id)} would be taken by more than one need: {', '.join(sides)}"
            for need_id, sides in sorted(self.claims.items())
            if len(sides) > 1
        ]


def _element_need(need_id: str, title: str, tag: str, description: str) -> dict[str, object]:
    """An architecture element's need, before its links: no field of its own."""
    return {"id": need_id, "type": ELEMENT_TYPE, "title": title, "content": description, "tags": [tag]}


def _add_component(collector: _Collector, swc, derived: bool) -> None:
    """A component's need, when it carries a link of its own or a ``derived_from`` names it, then each linked member's.

    *swc* is a ``schema.SWComponent`` and stays unannotated: this module imports no ``schema`` class.
    """
    by_owner: dict[tuple[str, str] | None, list[str]] = {}
    for site in link_sites(swc):
        by_owner.setdefault(site.owner, []).append(site.value)
    files = collector.storage_names("swc", swc.name)
    if None in by_owner or derived:
        need = _element_need(_element_id("swc", swc.name), swc.name, "swc", swc.description)
        judged = (("name", swc.name), ("description", swc.description))
        collector.add("element", need, judged, SATISFIES, by_owner.get(None, ()), files)
    for kind, members in (("port", swc.ports), ("runnable", swc.runnables)):
        for member in members:
            values = by_owner.get((kind, member.name))
            if values is None:
                continue
            need_id = _member_id(kind, swc.name, member.name)
            need = _element_need(need_id, f"{swc.name}.{member.name}", kind, member.description)
            judged = (("name", member.name), ("description", member.description))
            collector.add("element", need, judged, SATISFIES, values, files, f"its component {_shown(swc.name)}")


def _delivery(requirements) -> tuple[str, str | None]:
    """The delivery the links were set for, and the refusal clause when the model names several.

    Over the non-stub incoming *requirements*, an empty ``baseline`` counting as none: none is ``""``; one value is
    that value; more than one is refused, naming each with its count, the most frequent first.
    """
    counts: dict[str | None, int] = {}
    for req in requirements:
        if req.source == "external" and not req.stub:
            counts[req.baseline or None] = counts.get(req.baseline or None, 0) + 1
    named = [baseline for baseline in counts if baseline is not None]
    if len(counts) <= 1 or not named:
        return (named[0] if named else ""), None
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0] is None, item[0] or ""))
    shown = [f"{_shown(baseline) if baseline is not None else _NO_DELIVERY}: {count}" for baseline, count in ranked]
    first_count = ranked[0][1]
    shown[0] += " requirement" if first_count == 1 else " requirements"
    return "", (
        f"the imported requirements come from more than one delivery ({', '.join(shown)}); "
        "import the current delivery, then generate"
    )


def build_needs_json(model_root: str, project_name: str) -> str:
    """The text of ``needs.json`` for the model under *model_root*, or :class:`NeedsExportRefused`.

    Every refusal comes before rendering. The text is sorted-key JSON with a final line break: the same model gives
    the same bytes.
    """
    requirements = list(read_requirements(model_root))
    components = sorted(read_swc_model(model_root), key=lambda component: component.name)
    # Every linkable layer but `swc`, each element paired with the kind written into
    # `tags`: a hardware element's is its own `kind`.
    others = (
        ("interfaces", [("interface", interface) for interface in read_interfaces(model_root)]),
        ("compositions", [("composition", composition) for composition in read_compositions(model_root)]),
        ("tasks", [("task", task) for task in read_tasks(model_root)]),
        ("hardware", [(hardware.kind, hardware) for hardware in read_hardware(model_root)]),
    )
    held = {("swc", swc.name) for swc in components} | {
        (layer, element.name) for layer, entries in others for _tag, element in entries
    }
    collector = _Collector(
        requirement_versions(requirements),
        requirement_stubs(requirements),
        held,
        {req.id for req in requirements if req.source == "external"},
        model_root,
    )
    architecture = [req for req in requirements if req.source == "architecture"]
    derived_targets = {
        reference
        for req in architecture
        for reference in (parse_element_reference(value) for value in req.derived_from)
        if reference is not None
    }

    for req in architecture:
        need: dict[str, object] = {
            "id": req.id,
            "type": REQUIREMENT_TYPES[req.req_type],
            "title": req.title,
            "content": req.description,
            "tags": [],
        }
        # A declared `QM` or `QM(Y)` allocates no ASIL, which the file says by writing nothing.
        if req.asil is not None and parse_asil(req.asil)[0] != NO_ASIL:
            need["asil"] = req.asil
        judged = (("title", req.title), ("description", req.description))
        files = collector.storage_names("requirements", req.id)
        collector.add("requirement", need, judged, DERIVED_FROM, req.derived_from, files)

    for swc in components:
        _add_component(collector, swc, ("swc", swc.name) in derived_targets)

    for layer, entries in others:
        for tag, element in sorted(entries, key=lambda entry: entry[1].name):
            values = [site.value for site in element_link_sites(element)]
            if not values and (layer, element.name) not in derived_targets:
                continue
            need = _element_need(_element_id(layer, element.name), element.name, tag, element.description)
            judged = (("name", element.name), ("description", element.description))
            collector.add("element", need, judged, SATISFIES, values, collector.storage_names(layer, element.name))

    current_version, several = _delivery(requirements)
    problems = [
        *collector.problems,
        *collector.collisions(),
        *collector.unplaced_clauses(),
        *([several] if several else []),
    ]
    if problems:
        raise NeedsExportRefused("; ".join(problems))

    needs = dict(collector.needs)
    document = {
        "current_version": current_version,
        "project": project_name,
        "versions": {current_version: {"needs": needs, "needs_amount": len(needs)}},
    }
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def write_needs_json(text: str, output_file: str) -> str:
    """*text*, :func:`build_needs_json`'s, written to *output_file* whole through a temporary sibling.

    UTF-8 with ``\\n`` line ends. The text goes to an exclusively created ``<name>.<random>.tmp`` in the output's
    folder (:func:`~swcraft_tools.exclusive_create.open_unique_sibling`, which follows no link and truncates
    nothing), then ``os.replace`` puts it over *output_file*: a failure leaves a previous file byte for byte, removes
    the temporary file and propagates the ``OSError``. Split from :func:`generate_needs` so the MCP ``build`` tool can
    refuse before its first write. What this does not reach: a process killed between the create and the replace
    leaves its ``.tmp`` file, and nothing locks the folder.

    Returns:
        The output file, as given.
    """
    out = Path(output_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary, created = open_unique_sibling(out.parent, out.name, ".tmp")
    try:
        with created as handle:
            handle.write(text)
        os.replace(temporary, out)
    except BaseException:
        # A failing cleanup must not mask the error that caused it.
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
    return str(out)


def remove_previous_needs_json(output_file: str, refused: NeedsExportRefused) -> NeedsExportRefused:
    """The removal of a previous *output_file* after *refused*; returns the refusal to raise.

    A refused run leaves no ``needs.json`` that could be handed over as the model's (a missing one is no error). A
    removal that fails adds one clause saying the previous file is still there, naming it by name alone.
    """
    try:
        Path(output_file).unlink(missing_ok=True)
    except OSError as exc:
        return NeedsExportRefused(
            f"{refused}; the previous {Path(output_file).name} could not be removed "
            f"({exc.strerror or type(exc).__name__}) and does not describe the model: do not hand it over"
        )
    return refused


def generate_needs(model_root: str, output_file: str, project_name: str) -> str:
    """``needs.json``, :func:`build_needs_json`'s text, written to *output_file*.

    A failed build leaves no output: every refusal comes before anything is written, and a refusal removes a previous
    file (:func:`remove_previous_needs_json`). Any other error building the text (an unreadable model file) removes it
    too, a failing removal suppressed so the reader's error stays the one raised.

    Returns:
        The output file, as given.
    """
    try:
        text = build_needs_json(model_root, project_name)
    except NeedsExportRefused as refused:
        raise remove_previous_needs_json(output_file, refused) from None
    except Exception:
        # The reader's own error stays the one raised.
        with contextlib.suppress(OSError):
            Path(output_file).unlink(missing_ok=True)
        raise
    return write_needs_json(text, output_file)
