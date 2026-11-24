"""The diagram extractor: the model read once, and every view's structure built from it.

This is the only module of the diagram generator that reads the model. It builds, per composition, the ELK graphs
of its internal block diagram (IBD) and its bundled overview; once, the nesting tree from the root composition;
once, when a task is declared, the deployment tree; per sequence, the lifelines and messages. It lays nothing out
and draws nothing. Everything it cannot place is a :class:`NotDrawn` record: nothing is left out in silence.

A component's deployment level, an IBD line's dashing (*not judged*) and the occurrence a task entry or sequence
step names are the validator's own answers, so a view and a rule cannot disagree. Every ELK option string is set
here and nowhere else.

The IBD: a wrapping root without ports of its own (``elkjs`` hangs on a root that has them) holds the
composition's frame; every member shows every port of its type; a member composition is a box, never inlined.
Connectors run provide to require, delegations in the direction data flows.

Each drawn element carries the model file it opens under :data:`OPENS_KEY` (a storage name, or a refusal); the SVG
files never carry it. Not reached: model validity (``check-model``'s; a type placed twice resolves to its first
member).
"""

from __future__ import annotations

import re
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from math import ceil
from typing import Any

from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import stored_storage_names
from swcraft_tools.reader import (
    read_compositions,
    read_connectors,
    read_hardware,
    read_interfaces,
    read_requirements,
    read_sequences,
    read_swc_model,
    read_tasks,
)
from swcraft_tools.safety import NO_ASIL, parse_asil
from swcraft_tools.scheduling_identity import SchedulingIdentities, identities_from, written_selector
from swcraft_tools.schema import (
    Composition,
    CompositionMember,
    CompositionPort,
    Connector,
    ConnectorEndpoint,
    Core,
    Delegation,
    ExternalInterface,
    Hardware,
    Interface,
    Partition,
    Port,
    Processor,
    Requirement,
    Runnable,
    Sequence,
    SequenceStep,
    SRInterface,
    SWComponent,
    Task,
    core_of_partition,
    hardware_by_name,
    partition_of,
)
from swcraft_tools.validator import PortEnd, delegation_context, effective_asil_levels, not_judged_reason
from swcraft_tools.writer.diagrams.text_metrics import text_width

#: The key every graph element carries SWCraft's own facts under. ELK ignores it and
#: hands it back unchanged, which is how the renderer learns what a laid-out id is.
METADATA_KEY = "swcraft"

#: The key, inside :data:`METADATA_KEY`, of the model entry a click on the element opens.
OPENS_KEY = "opens"

#: A model entry as the page's model block holds it: ``{"file", "member"}`` or ``{"refused"}``.
ModelEntry = Mapping[str, str | None]

PORT_HEIGHT = 14
#: The hollow square the glyph starts from, centred on the owner's border; equal to ``svg.SQUARE``
#: but not imported, since this module may not import the renderer.
PORT_SQUARE = 12

#: How far a port's tip, where its edge is anchored, lies from the square's outer edge, by
#: ``(direction, interface kind)``: the stem plus the shape (arrow 4 + 16, fork 10 + 8, circle
#: 8 + 12, half circle 8 + 6). Equal to the renderer's ``svg.GLYPH_STEM_AND_SHAPE``, not imported.
PORT_REACH: dict[tuple[str, str], int] = {
    ("provide", "sender_receiver"): 20,
    ("require", "sender_receiver"): 18,
    ("provide", "client_server"): 20,
    ("require", "client_server"): 14,
}

#: What a port says of its interface's kind, in its hover text and class. It travels as
#: ``interfaceWord`` beside ``interfaceKind``, the glyph kind, because a stub's two differ.
INTERFACE_WORDS: dict[str, str] = {"sender_receiver": "sender-receiver", "client_server": "client-server"}

#: What a port on an external interface stub says, whether its ``DEST`` draws a glyph or none.
EXTERNAL_WORD = "external"

#: What a port of an external component stub says: it declares no interface and is the bare
#: square. Its ``observed_interface`` hint is an observation, not a declaration, and chooses nothing.
STUB_PORT_WORD = "external component, interface not declared"

#: The stub ``DEST`` -> the glyph kind it declares; every other ``DEST`` draws the bare square.
GLYPH_BY_DEST: dict[str, str] = {
    "SENDER-RECEIVER-INTERFACE": "sender_receiver",
    "CLIENT-SERVER-INTERFACE": "client_server",
}

#: The width the renderer strokes a glyph with (``svg.LINE_WIDTH``, not imported).
GLYPH_STROKE_WIDTH = 1.2

#: The strip the IBD's wrapping root keeps around the frame: a boundary port's glyph points out
#: of the frame, and ELK's default of 12 is less than the widest reach plus its stroke. The
#: overview's root keeps ELK's default, since its frame draws no port.
ROOT_PADDING = ceil(max(PORT_REACH.values()) + GLYPH_STROKE_WIDTH)

#: The font sizes the label estimates assume; the renderer draws with the same.
NODE_FONT_SIZE = 12
PORT_FONT_SIZE = 10
EDGE_FONT_SIZE = 10

_IBD_ROOT_OPTIONS: dict[str, str] = {
    "elk.algorithm": "layered",
    "elk.direction": "RIGHT",
    "elk.padding": f"[top={ROOT_PADDING},left={ROOT_PADDING},bottom={ROOT_PADDING},right={ROOT_PADDING}]",
    "elk.edgeRouting": "ORTHOGONAL",
    "elk.hierarchyHandling": "INCLUDE_CHILDREN",
    "elk.spacing.nodeNode": "40",
    "elk.layered.spacing.nodeNodeBetweenLayers": "80",
    "elk.spacing.edgeNode": "20",
    "elk.layered.spacing.edgeNodeBetweenLayers": "30",
}

_OVERVIEW_ROOT_OPTIONS: dict[str, str] = {
    "elk.algorithm": "layered",
    "elk.direction": "RIGHT",
    # Orthogonal: a bundle's last segment is perpendicular to the box it enters, so the
    # arrowhead follows the line and meets the box square-on, which a spline's end does not.
    "elk.edgeRouting": "ORTHOGONAL",
    "elk.hierarchyHandling": "INCLUDE_CHILDREN",
    "elk.spacing.nodeNode": "40",
    "elk.layered.spacing.nodeNodeBetweenLayers": "80",
    "elk.spacing.edgeLabel": "4",
}

#: The frame's top and bottom padding. The two sides are derived per composition
#: (:func:`frame_padding`): a boundary port's label is drawn inside the frame and ELK does not
#: grow a hierarchical node for its own ports' inside labels, so a long name would overlap members.
FRAME_PADDING_VERTICAL = 40
#: The least either side may be.
FRAME_SIDE_PADDING = 90
#: What ELK puts between a port box and its label, as measured on recorded layouts.
LABEL_PORT_SPACING = 1
#: The clear space a derived side keeps between the longest boundary label and the members.
FRAME_LABEL_MARGIN = 12

#: ``PORTS`` lets the frame grow to hold its own ports; without it a boundary port no delegation
#: pulls is stacked over its neighbours.
_FRAME_OPTIONS: dict[str, str] = {
    "elk.portConstraints": "FIXED_SIDE",
    "elk.nodeSize.constraints": "PORTS",
    "elk.padding": (
        f"[top={FRAME_PADDING_VERTICAL},left={FRAME_SIDE_PADDING},"
        f"bottom={FRAME_PADDING_VERTICAL},right={FRAME_SIDE_PADDING}]"
    ),
    "elk.nodeLabels.placement": "H_LEFT V_TOP INSIDE",
    "elk.portLabels.placement": "INSIDE NEXT_TO_PORT_IF_POSSIBLE",
}

_MEMBER_OPTIONS: dict[str, str] = {
    "elk.portConstraints": "FIXED_SIDE",
    "elk.nodeLabels.placement": "H_CENTER V_TOP INSIDE",
    "elk.portLabels.placement": "INSIDE NEXT_TO_PORT_IF_POSSIBLE",
    "elk.nodeSize.constraints": "PORTS PORT_LABELS NODE_LABELS MINIMUM_SIZE",
    "elk.nodeSize.minimum": "(160,60)",
    "elk.spacing.portPort": "8",
}

_OVERVIEW_MEMBER_OPTIONS: dict[str, str] = {
    "elk.nodeLabels.placement": "H_CENTER V_CENTER INSIDE",
    "elk.nodeSize.constraints": "NODE_LABELS MINIMUM_SIZE",
    "elk.nodeSize.minimum": "(180,50)",
}

_OVERVIEW_EDGE_OPTIONS: dict[str, str] = {
    "elk.edgeLabels.inline": "true",
}

#: The side a port sits on, by its direction, for a member's port and a boundary port alike.
_SIDE = {"require": "WEST", "provide": "EAST"}

#: How far the port box reaches inside its owner: half the square, for a member and for the
#: frame alike, so the square sits astride the border and the glyph points outward -- a
#: composition's port faces its environment.
_BORDER_OFFSET = str(-(PORT_SQUARE // 2))

#: The anchor on a member port's outer end, where its line leaves the glyph's tip; on a boundary
#: port it is the square's inner face, where the delegation arrives. The east end is per port.
_WEST_END = f"(0,{PORT_HEIGHT // 2})"


def port_width(direction: str, interface_kind: str | None) -> int:
    """The ELK width of a port box: half the square inside the owner, then the glyph's reach.

    A port whose *interface_kind* is ``None`` draws no glyph and is measured as the square alone.
    """
    if interface_kind not in INTERFACE_WORDS:
        return PORT_SQUARE
    # The box is the whole square plus the glyph's reach beyond its outer edge.
    return PORT_SQUARE + PORT_REACH[(direction, interface_kind)]


#: The separator between a runnable and its step's note on an activation message.
NOTE_SEPARATOR = " — "

_DIGITS = re.compile(r"(\d+)")


def natural_key(name: str) -> tuple[object, ...]:
    """*name* split into text and number runs, so ``_2`` sorts before ``_10``.

    A number run compares as an integer, the text around it case-insensitively and then by its
    own bytes, so the order is total and depends on nothing but the names.
    """
    parts = _DIGITS.split(name)
    return tuple((1, int(part), "") if index % 2 else (0, 0, part.casefold()) for index, part in enumerate(parts)) + (
        (0, 0, name),
    )


#: The box-in-box views' ids and file stems.
NESTING_VIEW = "nesting"
DEPLOYMENT_VIEW = "deployment"


@dataclass(frozen=True)
class DrawnConnector:
    """One connector an IBD drew, with everything that connector's own line says about it.

    An overview bundles several into one edge and the page lists them in a panel; a panel row
    and the IBD line's hover are built by one function (``svg.link_line``) from these fields,
    so they cannot disagree. The ends are oriented provide to require.
    """

    name: str
    source_member: str
    target_member: str
    source: str
    target: str
    interface: str
    description: str
    loop: bool

    def as_metadata(self) -> dict[str, Any]:
        """The row's own facts, as a bundle carries them through the layout to the renderer.

        Every field but the two member names, which the bundle's edge already says; derived from
        the declaration so a new field reaches the renderer with no second edit.
        """
        row = asdict(self)
        del row["source_member"], row["target_member"]
        return row


@dataclass(frozen=True)
class NotDrawn:
    """One item a view could not draw -- or, for ``loop``, drew unusually.

    ``view`` is the view id, or ``None`` when no view exists for it; ``detail`` says why.
    """

    view: str | None
    kind: str
    subject: str
    detail: str


@dataclass(frozen=True)
class CompositionGraphs:
    """The two ELK graphs of one composition, not yet laid out.

    ``stem`` is what the composition's view ids end in (:func:`file_stems`).
    """

    name: str
    ibd: dict[str, Any]
    overview: dict[str, Any]
    stem: str


@dataclass(frozen=True)
class NestingNode:
    """One box of the nesting overview: a composition with its members, or a component."""

    name: str
    type: str
    kind: str
    title: str
    children: tuple[NestingNode, ...]
    #: What a click on the box opens: the file of its type, its task or its hardware element.
    opens: ModelEntry | None = None


@dataclass(frozen=True)
class SequenceMessage:
    """One arrow of a sequence: an ``activation`` from the task or a ``data`` message.

    ``source`` and ``target`` are lifeline **keys** (:attr:`SequenceView.lifelines`).
    """

    kind: str
    source: Hashable
    target: Hashable
    label: str
    #: The interface the sending port declares, for a ``data`` message; ``""`` otherwise.
    interface: str = ""
    #: What a click on the arrow opens: the component's runnable for an activation, the sending
    #: component's port for a data message; ``None`` where the lifeline resolves to no component.
    opens: ModelEntry | None = None


@dataclass(frozen=True)
class SequenceView:
    """One scenario: its lifelines left to right and its messages top to bottom.

    A lifeline is a key and a label, carried apart: the key is what a message's ends name --
    ``("task", name)`` or ``("component", *identity)`` with the resolved identity -- and the
    label is the selector as the step writes it. Two lifelines can share a label, never a key.
    """

    name: str
    title: str
    task: str
    task_period: float | None
    lifelines: tuple[Hashable, ...]
    messages: tuple[SequenceMessage, ...]
    labels: tuple[str, ...]
    #: What a click on the frame opens: the sequence's own file.
    opens: ModelEntry | None = None
    #: What a click on each lifeline opens, in :attr:`lifelines`' order.
    lifeline_opens: tuple[ModelEntry | None, ...] = ()


@dataclass(frozen=True)
class DiagramGraphs:
    """Everything the generator draws, as extracted from one read of the model."""

    compositions: tuple[CompositionGraphs, ...]
    nesting: NestingNode | None
    sequences: tuple[SequenceView, ...]
    #: Each sequence's file stem, in the order of :attr:`sequences` (:func:`file_stems`).
    sequence_stems: tuple[str, ...]
    not_drawn: tuple[NotDrawn, ...]
    #: The ``deployment`` tree, or ``None`` when the model declares no task or no processor.
    deployment: NestingNode | None = None
    #: Composition name -> the ``(parent, member)`` pairs that place it; the same map every
    #: frame's ``parents`` is written from, so a page's way up and its SVG cannot disagree.
    placements: Mapping[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    #: Interface name -> its full member lines, uncapped and unprefixed (:func:`_member_lines`).
    #: It never travels in the ELK metadata.
    interface_contents: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def elk_graphs(self) -> list[dict[str, Any]]:
        """The graphs ELK lays out, in view order: each composition's IBD, then its overview."""
        return [graph for item in self.compositions for graph in (item.ibd, item.overview)]

    def composition_stems(self) -> dict[str, str]:
        """Composition name -> the stem its views take; the first declaration of a name wins."""
        stems: dict[str, str] = {}
        for item in self.compositions:
            stems.setdefault(item.name, item.stem)
        return stems


def file_stems(names: list[str]) -> list[str]:
    """The stem each of *names* gives its view files, in order.

    A name is its own stem unless an earlier name equals it but for case, which on a
    case-insensitive filesystem would be the same file: then it takes the nearest free
    ``<name>~<n>`` (compared by ``str.casefold()``), as the storage rule does. A name repeated
    exactly takes the same stem; the generator refuses it.
    """
    declared = {name.casefold() for name in names}
    given: dict[str, str] = {}
    taken: set[str] = set()
    stems = []
    for name in names:
        if name not in given:
            stem = name
            suffix = 0
            while stem.casefold() in taken or (suffix and stem.casefold() in declared):
                suffix += 1
                stem = f"{name}~{suffix}"
            given[name] = stem
            taken.add(stem.casefold())
        stems.append(given[name])
    return stems


def view_ids(graphs: DiagramGraphs) -> list[str]:
    """Every view's id in the fixed order the files and the index take."""
    ids = [view for item in graphs.compositions for view in (f"ibd-{item.stem}", f"overview-{item.stem}")]
    if graphs.nesting is not None:
        ids.append(NESTING_VIEW)
    if graphs.deployment is not None:
        ids.append(DEPLOYMENT_VIEW)
    ids.extend(f"sequence-{stem}" for stem in graphs.sequence_stems)
    return ids


def _label(text: str, font_size: int) -> dict[str, Any]:
    return {
        "text": text,
        "width": text_width(text, font_size),
        "height": font_size + 4,
    }


def _interface_kinds(model_root: str) -> tuple[dict[str, tuple[str | None, str]], dict[str, Interface]]:
    """From one read of the layer: interface name -> ``(glyph kind, word)``, and name -> interface.

    A glyph only for a declared type: a declared interface draws its own kind's glyph; an
    external interface stub draws the glyph its ``DEST`` declares (:data:`GLYPH_BY_DEST`) or the
    bare square (kind ``None``), and always says :data:`EXTERNAL_WORD`.

    Both maps read the first declaration of a name, as the validator does, so a name declared
    twice takes its glyph, judgement and member lines from the same file.
    """
    by_name: dict[str, Interface] = {}
    for interface in read_interfaces(model_root):
        by_name.setdefault(interface.name, interface)
    kinds = {
        name: (GLYPH_BY_DEST.get(interface.external_dest), EXTERNAL_WORD)
        if isinstance(interface, ExternalInterface)
        else (interface.kind, INTERFACE_WORDS[interface.kind])
        for name, interface in by_name.items()
    }
    return kinds, by_name


def _member_lines(interface: Interface) -> tuple[str, ...]:
    """The lines a port's hover lists for *interface*, in the model's order, uncapped and unprefixed.

    A sender-receiver interface gives ``<element>: <type>`` per data element; a client-server
    interface ``<Operation>(<dir> <arg>: <type>, ...)`` per operation. An external interface stub
    holds member names only, so it gives those names and nothing more: no type is invented.
    """
    if isinstance(interface, ExternalInterface):
        return (*interface.data_elements, *interface.operations)
    if isinstance(interface, SRInterface):
        return tuple(
            f"{element.name}: {_type_name(element.application_type, element.implementation_type)}"
            for element in interface.data_elements
        )
    return tuple(
        f"{operation.name}("
        + ", ".join(
            f"{argument.direction} {argument.name}: "
            + _type_name(argument.application_type, argument.implementation_type)
            for argument in operation.arguments
        )
        + ")"
        for operation in interface.operations
    )


def _type_name(application_type: str | None, implementation_type: str | None) -> str:
    """The one type name a data element or an argument names (the schema requires exactly one)."""
    return application_type or implementation_type or ""


# ---------------------------------------------------------------------------
# The internal block diagram and the overview
# ---------------------------------------------------------------------------


def _port_kinds(
    port: Port | CompositionPort,
    interface_kinds: Mapping[str, tuple[str | None, str]],
    external_owner: bool,
) -> tuple[str, str | None, str | None]:
    """``(interface, glyph kind, word)`` a port is drawn with; the interface ``""`` for none.

    A declared owner's port is drawn from its declared ``interface`` alone; a missing or
    unresolved one has neither kind nor word (the bare square). A port of an external component
    stub is ``("", None, STUB_PORT_WORD)`` whatever its ``observed_interface`` hint says.
    """
    if external_owner and isinstance(port, Port):  # the isinstance narrows the type: a boundary port's owner is no stub
        return "", None, STUB_PORT_WORD
    interface_kind, interface_word = interface_kinds.get(port.interface or "", (None, None))
    return port.interface or "", interface_kind, interface_word


def _port_node(
    owner: str | None,
    port: Port | CompositionPort,
    interface_kinds: Mapping[str, tuple[str | None, str]],
    external_owner: bool = False,
    opens: ModelEntry | None = None,
) -> dict[str, Any]:
    side = _SIDE[port.direction]
    interface, interface_kind, interface_word = _port_kinds(port, interface_kinds, external_owner)
    width = port_width(port.direction, interface_kind)
    east_end = f"({width},{PORT_HEIGHT // 2})"
    if owner is None:
        port_id = f"boundary.{port.name}"
        anchor = east_end if side == "WEST" else _WEST_END
    else:
        port_id = f"port.{owner}.{port.name}"
        anchor = _WEST_END if side == "WEST" else east_end
    offset = _BORDER_OFFSET
    return {
        "id": port_id,
        "width": width,
        "height": PORT_HEIGHT,
        "labels": [_label(port.name, PORT_FONT_SIZE)],
        "layoutOptions": {"elk.port.side": side, "elk.port.borderOffset": offset, "elk.port.anchor": anchor},
        METADATA_KEY: {
            "role": "port",
            "owner": owner,
            "name": port.name,
            "direction": port.direction,
            "interface": interface,
            "interfaceKind": interface_kind,
            "interfaceWord": interface_word,
            "description": port.description,
            OPENS_KEY: opens,
        },
    }


def frame_padding(boundary_ports: Iterable[Mapping[str, Any]]) -> str:
    """The frame's ``elk.padding`` for a composition whose boundary ports are *boundary_ports*.

    A boundary port's label is drawn inside the frame and ends :data:`LABEL_PORT_SPACING` plus
    its estimated width past the port's inner edge (half the square). Each side is the widest
    such end plus :data:`FRAME_LABEL_MARGIN`, never less than :data:`FRAME_SIDE_PADDING`, so the
    members' area begins where the labels end; the two sides are independent.

    What it does not reach: a member's own port labels, which ELK sizes (``PORT_LABELS``), and
    real text metrics.
    """
    sides = {"WEST": float(FRAME_SIDE_PADDING), "EAST": float(FRAME_SIDE_PADDING)}
    inside = float(PORT_SQUARE // 2)
    for port in boundary_ports:
        labels = port.get("labels") or []
        label_width = max((float(label["width"]) for label in labels), default=0.0)
        needed = inside + LABEL_PORT_SPACING + label_width + FRAME_LABEL_MARGIN
        side = str(port["layoutOptions"]["elk.port.side"])
        sides[side] = max(sides[side], needed)
    left, right = ceil(sides["WEST"]), ceil(sides["EAST"])
    return f"[top={FRAME_PADDING_VERTICAL},left={left},bottom={FRAME_PADDING_VERTICAL},right={right}]"


def _frame_node(
    composition: Composition, parents: tuple[tuple[str, str], ...], opens: ModelEntry | None
) -> dict[str, Any]:
    return {
        "id": "frame",
        "labels": [_label(composition.name, NODE_FONT_SIZE)],
        "layoutOptions": dict(_FRAME_OPTIONS),
        METADATA_KEY: {
            "role": "frame",
            "name": composition.name,
            "title": composition.title,
            "description": composition.description,
            "parents": [list(pair) for pair in parents],
            OPENS_KEY: opens,
        },
    }


class _Model:
    """The layers the generator reads, each read once and indexed by identity."""

    def __init__(self, model_root: str) -> None:
        self.compositions: list[Composition] = read_compositions(model_root)
        self.components: list[SWComponent] = read_swc_model(model_root)
        self.connectors: list[Connector] = read_connectors(model_root)
        self.tasks: list[Task] = read_tasks(model_root)
        self.sequences: list[Sequence] = read_sequences(model_root)
        self.hardware: list[Hardware] = read_hardware(model_root)
        self.requirements: list[Requirement] = read_requirements(model_root)
        self.interface_kinds, self.interfaces = _interface_kinds(model_root)
        self.composition_by_name: dict[str, Composition] = {}
        for composition in self.compositions:
            self.composition_by_name.setdefault(composition.name, composition)
        self.component_by_name: dict[str, SWComponent] = {}
        for component in self.components:
            self.component_by_name.setdefault(component.name, component)
        roots = [composition for composition in self.compositions if composition.root]
        self.root: Composition | None = roots[0] if roots else None
        self.extra_roots = roots[1:]
        #: Which occurrence a task entry or a sequence step names: the validator's resolver.
        self.identities: SchedulingIdentities = identities_from(self.compositions)
        #: Which file declares each drawn element, for what a click on it opens.
        self.stored = _StorageNames(model_root)

    def is_external(self, member: CompositionMember) -> bool:
        """Whether *member* places an external component stub, whose ports are drawn as bare squares."""
        component = None if member.type is None else self.component_by_name.get(member.type)
        return component is not None and component.external

    def member_ports(self, member: CompositionMember) -> list[Port] | list[CompositionPort] | None:
        """The ports of *member*'s type, or ``None`` when the type is not declared."""
        if member.type is not None:
            component = self.component_by_name.get(member.type)
            return None if component is None else component.ports
        composition = self.composition_by_name.get(member.composition or "")
        return None if composition is None else composition.ports


class _StorageNames:
    """The storage names of the layers a drawn element opens, each layer listed once, when first asked.

    The spelling is ``model_state.stored_storage_names``', shared with ``needs.json``.
    :meth:`opens` answers one element's file and member, or the refusal ``several-files`` (an
    identity two files declare) or ``no-file`` (one no file declares).
    """

    def __init__(self, model_root: str) -> None:
        self._root = model_root
        self._names: dict[str, dict[str, list[str]]] = {}

    def opens(self, layer: str, identity: str, member: str | None = None) -> ModelEntry:
        names = self._names.get(layer)
        if names is None:
            names = self._names[layer] = stored_storage_names(self._root, layer, registry=current_registry())
        files = names.get(identity, [])
        if len(files) > 1:
            return {"refused": "several-files"}
        if not files:
            return {"refused": "no-file"}
        return {"file": files[0], "member": member}


#: The layer a member's type is declared in, by the member's kind.
_KIND_LAYERS: dict[str, str] = {"component": "swc", "composition": "compositions"}


def _member_kind(member: CompositionMember) -> str:
    return "component" if member.type is not None else "composition"


def _member_type(member: CompositionMember) -> str:
    return member.type if member.type is not None else (member.composition or "")


def _member_label(member: CompositionMember) -> str:
    type_name = _member_type(member)
    # svg.member_label's rule; this module may not import the renderer.
    return member.name if member.name == type_name else f"{member.name} : {type_name}"


def _member_opens(member: CompositionMember, model: _Model, port: str | None = None) -> ModelEntry:
    """What a member's box -- or, with *port*, one of its ports -- opens: the file of its type."""
    member_path = None if port is None else f"ports/{port}"
    return model.stored.opens(_KIND_LAYERS[_member_kind(member)], _member_type(member), member_path)


def _member_metadata(member: CompositionMember, model: _Model) -> dict[str, Any]:
    """What a member's box says: a component's own description first, the member's second."""
    component = None if member.type is None else model.component_by_name.get(member.type)
    description = "" if component is None else component.description
    return {
        "role": "member",
        "name": member.name,
        "type": _member_type(member),
        "kind": _member_kind(member),
        "description": description or member.description,
        OPENS_KEY: _member_opens(member, model),
    }


def _endpoint_type(endpoint: ConnectorEndpoint) -> str:
    return endpoint.swc if endpoint.swc is not None else (endpoint.composition or "")


def _connector_composition(connector: Connector, model: _Model) -> str | None:
    """The name of the composition *connector* is drawn inside; ``None`` for no root."""
    if connector.composition is not None:
        return connector.composition
    return None if model.root is None else model.root.name


class _CompositionExtraction:
    """One composition's IBD and overview, and the records its drawing produced."""

    def __init__(
        self,
        composition: Composition,
        stem: str,
        model: _Model,
        not_drawn: list[NotDrawn],
        parents: tuple[tuple[str, str], ...],
    ) -> None:
        self.composition = composition
        self.parents = parents
        self.model = model
        self.not_drawn = not_drawn
        self.view = f"ibd-{stem}"
        # member name -> (member, port name -> direction); first declaration wins.
        self.members: dict[str, tuple[CompositionMember, dict[str, str]]] = {}
        self.member_of_type: dict[tuple[str, str], str] = {}
        # boundary port name -> (direction, the interface it declares, "" for none); first wins.
        self.boundary: dict[str, tuple[str, str]] = {}
        # (member, port) -> the interface that port declares, "" for none; the bundle rows read it.
        self.port_interface: dict[tuple[str, str], str] = {}
        # Every connector the IBD drew, with what its own line says about it.
        self.drawn_connectors: list[DrawnConnector] = []

    def _report(self, kind: str, subject: str, detail: str) -> None:
        self.not_drawn.append(NotDrawn(self.view, kind, subject, detail))

    def _opens(self, member: str | None = None) -> ModelEntry:
        """What the frame -- or, with *member*, one of the composition's own members -- opens."""
        return self.model.stored.opens("compositions", self.composition.name, member)

    def _duplicate(self, subject: str) -> None:
        self._report("duplicate-name", subject, f"{self.composition.name} declares {subject} twice")

    def ibd(self, connectors: list[Connector]) -> dict[str, Any]:
        """The internal block diagram; call it before :meth:`overview`, which bundles what it drew."""
        frame = _frame_node(self.composition, self.parents, self._opens())
        boundary_nodes = []
        # One name for the frame's ports and, below, the members' ports of either kind.
        port: Port | CompositionPort
        for port in self.composition.ports:
            if port.name in self.boundary:
                self._duplicate(port.name)
                continue
            self.boundary[port.name] = (port.direction, port.interface or "")
            boundary_nodes.append(
                _port_node(None, port, self.model.interface_kinds, opens=self._opens(f"ports/{port.name}"))
            )
        frame["ports"] = boundary_nodes
        # The IBD's frame only: an overview draws no port and keeps the minimum side padding.
        frame["layoutOptions"]["elk.padding"] = frame_padding(boundary_nodes)
        children = []
        for member in self.composition.members:
            if member.name in self.members:
                self._duplicate(member.name)
                continue
            ports = self.model.member_ports(member)
            if ports is None:
                self._report("type-not-declared", member.name, f"no {_member_kind(member)} {_member_type(member)}")
                ports = []
            directions: dict[str, str] = {}
            port_nodes = []
            external_owner = self.model.is_external(member)
            for port in ports:
                if port.name in directions:
                    self._report(
                        "duplicate-name",
                        f"{member.name}.{port.name}",
                        f"{_member_type(member)} declares {port.name} twice",
                    )
                    continue
                directions[port.name] = port.direction
                # The declared interface, so a bundle row never reports a stub's hint as declared.
                self.port_interface[(member.name, port.name)] = port.interface or ""
                port_nodes.append(
                    _port_node(
                        member.name,
                        port,
                        self.model.interface_kinds,
                        external_owner,
                        _member_opens(member, self.model, port.name),
                    )
                )
            self.members[member.name] = (member, directions)
            self.member_of_type.setdefault((_member_kind(member), _member_type(member)), member.name)
            children.append(
                {
                    "id": f"member.{member.name}",
                    "labels": [_label(_member_label(member), NODE_FONT_SIZE)],
                    "layoutOptions": dict(_MEMBER_OPTIONS),
                    "ports": port_nodes,
                    METADATA_KEY: _member_metadata(member, self.model),
                }
            )
        frame["children"] = children
        edges = [edge for edge in (self._connector_edge(connector) for connector in connectors) if edge is not None]
        delegations: set[str] = set()
        for delegation in self.composition.delegations:
            if delegation.name in delegations:
                self._duplicate(delegation.name)
                continue
            delegations.add(delegation.name)
            edge = self._delegation_edge(delegation)
            if edge is not None:
                edges.append(edge)
        frame["edges"] = edges
        return {"id": "root", "layoutOptions": dict(_IBD_ROOT_OPTIONS), "children": [frame]}

    def _resolve(self, connector: Connector) -> tuple[tuple[str, str], tuple[str, str], bool] | None:
        """The connector's two ``(member, port)`` ends oriented provide to require, or ``None``.

        The third value says whether the written ends were swapped into that order (a connector
        written require -> provide).
        """
        ends = []
        for endpoint in (connector.from_, connector.to):
            kind = "component" if endpoint.swc is not None else "composition"
            member = self.member_of_type.get((kind, _endpoint_type(endpoint)))
            if member is None:
                self._report(
                    "endpoint-not-member",
                    connector.name,
                    f"{_endpoint_type(endpoint)} is not the type of a member of {self.composition.name}",
                )
                return None
            if endpoint.port not in self.members[member][1]:
                self._report(
                    "port-not-declared", connector.name, f"{_endpoint_type(endpoint)} declares no port {endpoint.port}"
                )
                return None
            ends.append((member, endpoint.port))
        source, target = ends
        swapped = (
            self.members[source[0]][1][source[1]] == "require" and self.members[target[0]][1][target[1]] == "provide"
        )
        if swapped:
            source, target = target, source
        return source, target, swapped

    def _connector_edge(self, connector: Connector) -> dict[str, Any] | None:
        resolved = self._resolve(connector)
        if resolved is None:
            return None
        (source_member, source_port), (target_member, target_port), swapped = resolved
        # Direction first, as the validator judges it: only a connector written provide ->
        # require is asked; any other is an ordinary ``check-model`` finding and stays solid.
        unverified = None
        if (
            not swapped
            and self.members[source_member][1][source_port] == "provide"
            and self.members[target_member][1][target_port] == "require"
        ):
            unverified = _unverified(
                self.model,
                (self._end(source_member, source_port), self._end(target_member, target_port)),
                required=self.port_interface.get((target_member, target_port), ""),
                provided=self.port_interface.get((source_member, source_port), ""),
                outer_direction=None,
            )
        loop = source_member == target_member
        self.drawn_connectors.append(
            DrawnConnector(
                name=connector.name,
                source_member=source_member,
                target_member=target_member,
                source=f"{source_member}.{source_port}",
                target=f"{target_member}.{target_port}",
                # Normally the provide end; a model ``check-model`` refuses can leave a require port.
                interface=self.port_interface.get((source_member, source_port), ""),
                description=connector.description,
                loop=loop,
            )
        )
        if loop:
            self._report(
                "loop",
                connector.name,
                f"{source_member}.{source_port} -> {target_member}.{target_port}",
            )
        metadata: dict[str, Any] = {
            "role": "connector",
            "name": connector.name,
            "description": connector.description,
            "loop": loop,
            OPENS_KEY: self.model.stored.opens("connectors", connector.name),
        }
        if unverified is not None:
            metadata["unverified"] = unverified
        return {
            "id": f"connector.{connector.name}",
            "sources": [f"port.{source_member}.{source_port}"],
            "targets": [f"port.{target_member}.{target_port}"],
            METADATA_KEY: metadata,
        }

    def _end(self, member_name: str, port: str) -> PortEnd:
        """*port* of member *member_name*, its owner the member's type: an end as the predicate takes it."""

        member = self.members[member_name][0]
        return PortEnd(_member_type(member), self.model.is_external(member), port)

    def _delegation_edge(self, delegation: Delegation) -> dict[str, Any] | None:
        if delegation.member not in self.members:
            self._report(
                "member-not-declared",
                delegation.name,
                f"{self.composition.name} has no member {delegation.member}",
            )
            return None
        if delegation.port not in self.boundary:
            self._report(
                "port-not-declared",
                delegation.name,
                f"{self.composition.name} declares no port {delegation.port}",
            )
            return None
        member, directions = self.members[delegation.member]
        if delegation.member_port not in directions:
            self._report(
                "port-not-declared",
                delegation.name,
                f"{_member_type(member)} declares no port {delegation.member_port}",
            )
            return None
        boundary = f"boundary.{delegation.port}"
        inner = f"port.{delegation.member}.{delegation.member_port}"
        outer_direction, outer_interface = self.boundary[delegation.port]
        outward = outer_direction == "provide"
        metadata: dict[str, Any] = {
            "role": "delegation",
            "name": delegation.name,
            "description": delegation.description,
            "loop": False,
            OPENS_KEY: self._opens(f"delegations/{delegation.name}"),
        }
        # Direction first: disagreeing directions are a ``check-model`` finding and stay solid.
        # Required is the inner port's interface, provided the boundary port's; a boundary
        # port's owner is never a stub, so only the member's end is passed.
        if directions[delegation.member_port] == outer_direction:
            unverified = _unverified(
                self.model,
                (self._end(delegation.member, delegation.member_port),),
                required=self.port_interface.get((delegation.member, delegation.member_port), ""),
                provided=outer_interface,
                outer_direction=outer_direction,
            )
            if unverified is not None:
                metadata["unverified"] = unverified
        return {
            "id": f"delegation.{delegation.name}",
            "sources": [inner if outward else boundary],
            "targets": [boundary if outward else inner],
            METADATA_KEY: metadata,
        }

    def overview(self) -> dict[str, Any]:
        """One node per member, one edge per pair the IBD connected, counting its connectors."""
        frame = _frame_node(self.composition, self.parents, self._opens())
        frame["children"] = [
            {
                "id": f"member.{name}",
                "labels": [_label(_member_label(member), NODE_FONT_SIZE)],
                "layoutOptions": dict(_OVERVIEW_MEMBER_OPTIONS),
                METADATA_KEY: _member_metadata(member, self.model),
            }
            for name, (member, _) in self.members.items()
        ]
        pairs: dict[tuple[str, str], list[DrawnConnector]] = {}
        for drawn in self.drawn_connectors:
            pairs.setdefault((drawn.source_member, drawn.target_member), []).append(drawn)
        # Natural order by connector name, so ``_2`` comes before ``_10`` in the page's panel.
        for drawn_connectors in pairs.values():
            drawn_connectors.sort(key=lambda drawn: natural_key(drawn.name))
        frame["edges"] = [
            {
                "id": f"pair.{source}.{target}",
                "sources": [f"member.{source}"],
                "targets": [f"member.{target}"],
                "labels": [_label(str(len(drawn_connectors)), EDGE_FONT_SIZE)],
                "layoutOptions": dict(_OVERVIEW_EDGE_OPTIONS),
                METADATA_KEY: {
                    "role": "bundle",
                    "count": len(drawn_connectors),
                    "connectors": [drawn.as_metadata() for drawn in drawn_connectors],
                },
            }
            for (source, target), drawn_connectors in pairs.items()
        ]
        return {"id": "root", "layoutOptions": dict(_OVERVIEW_ROOT_OPTIONS), "children": [frame]}


# ---------------------------------------------------------------------------
# The nesting tree
# ---------------------------------------------------------------------------


def _nesting(model: _Model, not_drawn: list[NotDrawn]) -> NestingNode | None:
    if model.root is None:
        not_drawn.append(NotDrawn(NESTING_VIEW, "no-root", "", "no composition declares root: true"))
        return None
    for extra in model.extra_roots:
        not_drawn.append(
            NotDrawn(NESTING_VIEW, "root-not-unique", extra.name, f"{model.root.name} is drawn as the root")
        )

    def members_of(composition: Composition, stack: list[str]) -> tuple[NestingNode, ...]:
        children = []
        for member in composition.members:
            if member.type is not None:
                children.append(
                    NestingNode(member.name, member.type, "component", "", (), model.stored.opens("swc", member.type))
                )
                continue
            type_name = member.composition or ""
            nested = model.composition_by_name.get(type_name)
            if type_name in stack or nested is None:
                if nested is None:
                    record = NotDrawn(NESTING_VIEW, "type-not-declared", member.name, f"no composition {type_name}")
                elif stack[-1] == type_name:
                    record = NotDrawn(NESTING_VIEW, "cycle", member.name, f"{type_name} places itself as a member")
                else:
                    path = " -> ".join(stack[stack.index(type_name) + 1 :])
                    record = NotDrawn(NESTING_VIEW, "cycle", member.name, f"{type_name} contains itself through {path}")
                not_drawn.append(record)
                children.append(
                    NestingNode(
                        member.name, type_name, "composition", "", (), model.stored.opens("compositions", type_name)
                    )
                )
            else:
                children.append(
                    NestingNode(
                        member.name,
                        type_name,
                        "composition",
                        nested.title,
                        members_of(nested, [*stack, type_name]),
                        model.stored.opens("compositions", type_name),
                    )
                )
        return tuple(children)

    root = model.root
    return NestingNode(
        root.name,
        root.name,
        "composition",
        root.title,
        members_of(root, [root.name]),
        model.stored.opens("compositions", root.name),
    )


# ---------------------------------------------------------------------------
# The sequences
# ---------------------------------------------------------------------------


def _find_runnable(component: SWComponent | None, name: str) -> Runnable | None:
    if component is None:
        return None
    return next((runnable for runnable in component.runnables if runnable.name == name), None)


def _step_label(step: SequenceStep) -> str:
    return step.runnable + (f"{NOTE_SEPARATOR}{step.note}" if step.note else "")


def _sequence(sequence: Sequence, stem: str, model: _Model, not_drawn: list[NotDrawn]) -> SequenceView:
    """One scenario's lifelines and messages.

    Two steps naming one occurrence share a lifeline; two occurrences of one type stand on two.
    The runnable and ports are read off the resolved type. A data message whose receiver type
    stands on two lifelines is a ``receiver-ambiguous`` record, never drawn.
    """
    view = f"sequence-{stem}"
    task = next((task for task in model.tasks if task.name == sequence.task), None)
    if task is None:
        not_drawn.append(NotDrawn(view, "task-not-declared", sequence.task, f"no task {sequence.task}"))
    task_key: Hashable = ("task", sequence.task)
    # A lifeline whose label is the task's name would read as the task's own line, so such a
    # step is not drawn -- for a ``swc`` and an ``instance`` selector alike.
    steps: list[tuple[SequenceStep, Hashable, str, str | None]] = []
    for step in sequence.steps:
        label = written_selector(step.swc, step.instance)
        if label == sequence.task:
            not_drawn.append(
                NotDrawn(
                    view,
                    "lifeline-shared-with-task",
                    f"{label}.{step.runnable}",
                    f"the component {label} has the task's name, and one lifeline cannot be both",
                )
            )
            continue
        resolution = model.identities.resolve(step.swc, step.instance)
        steps.append((step, ("component", *resolution.identity), label, resolution.type))
    # Each component lifeline's key -> (label, resolved type), in the order steps first name it.
    lifelines: dict[Hashable, tuple[str, str | None]] = {}
    for _, key, label, type_name in steps:
        lifelines.setdefault(key, (label, type_name))
    messages = []
    for step, key, label, type_name in steps:
        messages.append(
            SequenceMessage("activation", task_key, key, _step_label(step), opens=_step_opens(model, type_name, step))
        )
        if type_name is None:
            not_drawn.append(
                NotDrawn(view, "instance-not-resolved", f"{label}.{step.runnable}", _unresolved_reason(model, label))
            )
            continue
        component = model.component_by_name.get(type_name)
        runnable = _find_runnable(component, step.runnable)
        if component is None or runnable is None:
            not_drawn.append(
                NotDrawn(
                    view,
                    "runnable-not-declared",
                    f"{label}.{step.runnable}",
                    f"{type_name} declares no runnable {step.runnable}",
                )
            )
            continue
        written = {write.port for write in runnable.writes}
        ports_by_name = {port.name: port for port in component.ports}
        for connector in model.connectors:
            for source, target in ((connector.from_, connector.to), (connector.to, connector.from_)):
                if source.swc != type_name or target.swc is None:
                    continue
                receivers = [other for other, (_label, other_type) in lifelines.items() if other_type == target.swc]
                if not receivers:
                    continue
                port = ports_by_name.get(source.port)
                if port is None or port.direction != "provide":
                    continue
                if written and source.port not in written:
                    continue
                text = f"{source.port} -> {target.port}"
                # A port that declares no interface says so by saying none, never ``None``.
                text = f"{text} : {port.interface}" if port.interface else text
                if len(receivers) > 1:
                    not_drawn.append(
                        NotDrawn(
                            view,
                            "receiver-ambiguous",
                            f"{label}: {text}",
                            f"{target.swc} stands on {len(receivers)} lifelines: "
                            + ", ".join(lifelines[other][0] for other in receivers),
                        )
                    )
                    continue
                messages.append(
                    SequenceMessage(
                        "data",
                        key,
                        receivers[0],
                        text,
                        port.interface or "",
                        model.stored.opens("swc", type_name, f"ports/{source.port}"),
                    )
                )
    return SequenceView(
        name=sequence.name,
        title=sequence.title,
        task=sequence.task,
        task_period=None if task is None else task.period,
        lifelines=(task_key, *lifelines),
        messages=tuple(messages),
        labels=(sequence.task, *(label for label, _type in lifelines.values())),
        opens=model.stored.opens("sequences", sequence.name),
        lifeline_opens=(
            model.stored.opens("tasks", sequence.task),
            *(
                None if type_name is None else model.stored.opens("swc", type_name)
                for _label, type_name in lifelines.values()
            ),
        ),
    )


def _step_opens(model: _Model, type_name: str | None, step: SequenceStep) -> ModelEntry | None:
    """What a step's activation opens: the resolved component's file at the runnable's line.

    ``None`` where the selector resolves to no component; the file alone where the component
    declares no such runnable.
    """
    if type_name is None:
        return None
    component = model.component_by_name.get(type_name)
    member = f"runnables/{step.runnable}" if _find_runnable(component, step.runnable) is not None else None
    return model.stored.opens("swc", type_name, member)


def _unresolved_reason(model: _Model, written: str) -> str:
    """Why the ``instance`` entry written *written* is drawn under no component.

    Under an undefined composition walk (no root, several, or a cycle) no path resolves at all;
    otherwise the resolver refuses this path.
    """
    if not model.identities.defined:
        return f"the instance {written} is not resolved: {model.identities.undefined_reason}"
    return f"the instance {written} resolves to no component"


# ---------------------------------------------------------------------------
# The deployment tree
# ---------------------------------------------------------------------------


def _capability_text(core: Core) -> str:
    """A core's level suffix: the ASIL capability it declares, written ``QM`` included; none when unrated."""
    return "" if core.asil_capability is None else f"ASIL capability {core.asil_capability}"


def _level_text(level: str) -> str:
    """A component's level suffix: its effective level when that is an ASIL, and none for ``QM``.

    ``QM`` is not an ASIL (ISO 26262-1 3.6 Note 1). The test is on ``X``: ``QM(D)`` carries no
    text, and any other value is printed as written, ``ASIL B(D)`` included.
    """
    return "" if parse_asil(level)[0] == NO_ASIL else f"ASIL {level}"


def _unverified(
    model: _Model,
    ends: tuple[PortEnd, ...],
    *,
    required: str,
    provided: str,
    outer_direction: str | None,
) -> str | None:
    """The validator's *not judged* reason for one line, or ``None`` where it judges the pair.

    A connector is asked under ``subset``, a delegation under the clause its *outer_direction*
    selects. An unset interface (``""``) is passed as ``None``. A line it answers is drawn dashed.
    """

    context = "subset" if outer_direction is None else delegation_context(outer_direction)
    return not_judged_reason(ends, required or None, provided or None, context=context, interfaces=model.interfaces)


def _effective_levels(model: _Model) -> dict[str, str]:
    """Component name -> its effective ASIL, the validator's own reading of it and never a second one."""

    return effective_asil_levels(model.components, model.requirements)


def _task_components(
    task: Task, model: _Model, levels: dict[str, str], run: set[str], not_drawn: list[NotDrawn]
) -> tuple[NestingNode, ...]:
    """The components *task* runs, once each in the order its runnable references first name them.

    A reference that does not resolve draws nothing and is a record.
    """
    names: list[str] = []
    for ref in task.runnables:
        written = written_selector(ref.swc, ref.instance)
        type_name = model.identities.type_of(ref.swc, ref.instance)
        if type_name is None:
            not_drawn.append(
                NotDrawn(
                    DEPLOYMENT_VIEW,
                    "instance-not-resolved",
                    f"{task.name}: {written}",
                    _unresolved_reason(model, written),
                )
            )
            continue
        component = model.component_by_name.get(type_name)
        if component is None:
            not_drawn.append(
                NotDrawn(
                    DEPLOYMENT_VIEW, "component-not-declared", f"{task.name}: {written}", f"no component {type_name}"
                )
            )
            continue
        if _find_runnable(component, ref.runnable) is None:
            not_drawn.append(
                NotDrawn(
                    DEPLOYMENT_VIEW,
                    "runnable-not-declared",
                    f"{task.name}: {written}.{ref.runnable}",
                    f"{type_name} declares no runnable {ref.runnable}",
                )
            )
            continue
        if component.name not in names:
            names.append(component.name)
    run.update(names)
    return tuple(
        NestingNode(name, name, "component", _level_text(levels.get(name, "QM")), (), model.stored.opens("swc", name))
        for name in names
    )


def _partition_suffix(partition: Partition) -> str:
    """A partition's suffix: the level it declares, as a component's (:func:`_level_text`), and its trust mark.

    A partition's ``asil`` is a rating, written as a component's level is; a ``trusted``
    partition appends ``trusted``, and the two confined classes append nothing.
    """
    level = "" if partition.asil is None else _level_text(partition.asil)
    if partition.trust != "trusted":
        return level
    return f"{level} · trusted" if level else "trusted"


def _deployment(model: _Model, not_drawn: list[NotDrawn]) -> NestingNode | None:
    """The processor, its cores in reader order, each core's partitions, each partition's tasks, each task's components.

    Emitted only when the model declares a task; with no processor it draws nothing and says so.
    Names resolve first-wins over every hardware element (``schema.hardware_by_name``). Every
    item the tree cannot hold is a record: a second processor, a task naming no partition, a
    partition naming no core (with its tasks), an unresolved runnable reference, and a component
    no task runs. A core or partition declared twice holds its children under its first box.
    """
    if not model.tasks:
        return None
    processors = [element for element in model.hardware if isinstance(element, Processor)]
    cores = [element for element in model.hardware if isinstance(element, Core)]
    partitions = [element for element in model.hardware if isinstance(element, Partition)]
    if not processors:
        not_drawn.append(NotDrawn(DEPLOYMENT_VIEW, "no-processor", "", "hardware/ declares no processor"))
        return None
    processor = processors[0]
    for surplus in processors[1:]:
        not_drawn.append(
            NotDrawn(
                DEPLOYMENT_VIEW, "processor-not-unique", surplus.name, f"{processor.name} is drawn as the processor"
            )
        )
    by_name = hardware_by_name(model.hardware)
    by_partition: dict[str, list[Task]] = {}
    for task in model.tasks:
        resolved = partition_of(task, by_name)
        if resolved is None:
            not_drawn.append(
                NotDrawn(DEPLOYMENT_VIEW, "partition-not-declared", task.name, f"no partition {task.partition}")
            )
            continue
        by_partition.setdefault(resolved.name, []).append(task)
    by_core: dict[str, list[tuple[Partition, list[Task]]]] = {}
    for partition in partitions:
        tasks = by_partition.pop(partition.name, [])
        core = core_of_partition(partition, by_name)
        if core is None:
            detail = f"no core {partition.core}"
            if tasks:
                detail += "; not drawn with it: " + ", ".join(task.name for task in tasks)
            not_drawn.append(NotDrawn(DEPLOYMENT_VIEW, "partition-core-not-declared", partition.name, detail))
            continue
        by_core.setdefault(core.name, []).append((partition, tasks))
    levels = _effective_levels(model)
    run: set[str] = set()
    core_nodes = []
    for core in cores:
        partition_nodes = []
        for partition, tasks in by_core.pop(core.name, []):
            task_nodes = tuple(
                NestingNode(
                    task.name,
                    task.name,
                    "task",
                    "",
                    _task_components(task, model, levels, run, not_drawn),
                    model.stored.opens("tasks", task.name),
                )
                for task in tasks
            )
            partition_nodes.append(
                NestingNode(
                    partition.name,
                    partition.name,
                    "partition",
                    _partition_suffix(partition),
                    task_nodes,
                    model.stored.opens("hardware", partition.name),
                )
            )
        core_nodes.append(
            NestingNode(
                core.name,
                core.name,
                "core",
                _capability_text(core),
                tuple(partition_nodes),
                model.stored.opens("hardware", core.name),
            )
        )
    for name in dict.fromkeys(component.name for component in model.components):
        if name not in run:
            not_drawn.append(NotDrawn(DEPLOYMENT_VIEW, "not-run", name, "no task runs a runnable of it"))
    hardware_opens = model.stored.opens("hardware", processor.name)
    return NestingNode(processor.name, processor.name, "processor", "", tuple(core_nodes), hardware_opens)


# ---------------------------------------------------------------------------
# Where each composition is placed
# ---------------------------------------------------------------------------


def _placements(model: _Model) -> dict[str, tuple[tuple[str, str], ...]]:
    """Composition name -> ``(parent, member)`` for every composition that places it, in reader order.

    Read from the whole model, so a frame can name the way up. First declarations win; the root,
    a composition no member places and an undeclared name have no entry.
    """
    placements: dict[str, list[tuple[str, str]]] = {}
    for composition in model.compositions:
        if model.composition_by_name.get(composition.name) is not composition:
            continue
        for member in composition.members:
            if member.composition is None or member.composition not in model.composition_by_name:
                continue
            parents = placements.setdefault(member.composition, [])
            if all(parent != composition.name for parent, _ in parents):
                parents.append((composition.name, member.name))
    return {name: tuple(pairs) for name, pairs in placements.items()}


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def build_graphs(model_root: str) -> DiagramGraphs:
    """Every view's structure, built from one read of the model under *model_root*."""
    model = _Model(model_root)
    not_drawn: list[NotDrawn] = []
    by_composition: dict[str, list[Connector]] = {}
    for connector in model.connectors:
        owner = _connector_composition(connector, model)
        if owner is None or owner not in model.composition_by_name:
            detail = "no root composition" if owner is None else f"no composition {owner}"
            not_drawn.append(NotDrawn(None, "composition-not-declared", connector.name, detail))
            continue
        by_composition.setdefault(owner, []).append(connector)
    compositions = []
    stems = file_stems([composition.name for composition in model.compositions])
    placements = _placements(model)
    for composition, stem in zip(model.compositions, stems, strict=True):
        extraction = _CompositionExtraction(composition, stem, model, not_drawn, placements.get(composition.name, ()))
        connectors = by_composition.get(composition.name, [])
        ibd = extraction.ibd(connectors)
        compositions.append(CompositionGraphs(composition.name, ibd, extraction.overview(), stem))
    nesting = _nesting(model, not_drawn)
    deployment = _deployment(model, not_drawn)
    sequence_stems = file_stems([sequence.name for sequence in model.sequences])
    sequences = tuple(
        _sequence(sequence, stem, model, not_drawn)
        for sequence, stem in zip(model.sequences, sequence_stems, strict=True)
    )
    interface_contents = {name: _member_lines(interface) for name, interface in model.interfaces.items()}
    return DiagramGraphs(
        tuple(compositions),
        nesting,
        sequences,
        tuple(sequence_stems),
        tuple(not_drawn),
        deployment,
        placements,
        interface_contents,
    )
