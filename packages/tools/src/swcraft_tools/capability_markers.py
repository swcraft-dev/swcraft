"""SWCraft's own capability markers on a ``SWC-INTERNAL-BEHAVIOR``: the wire facts, defined once.

A **capability marker** is an ``SD`` in the ``SWCRAFT`` group of a behaviour's
``ADMIN-DATA``. It says the exporter *can* speak about a kind of content, so a file
that holds none of that content means *none* and not *could not say*. Every marker is
a capability token and never a version string: the text states what the exporter
does, nothing about the component and nothing about which release wrote it.

A leaf importing nothing from ``swcraft_tools``: the writer mints these values and
the reader admits them, and the reader may not depend on the writer package.

This module holds the wire alone: the group's ``GID`` and, per marker, the ``SD``'s
``GID`` and the value the writer mints. What the component exchange decides from a
marker is the exchange's, never this module's.
"""

from __future__ import annotations

from dataclasses import dataclass

#: ``GID`` of the ``SDG`` carrying SWCraft's own metadata on a
#: ``SWC-INTERNAL-BEHAVIOR``. A file without it is an old or foreign export.
SDG_GID = "SWCRAFT"


@dataclass(frozen=True)
class Marker:
    """One ``SD`` in the ``SWCRAFT`` group: its ``GID`` and the text the writer mints.

    The exchange trusts the writer's own ``text`` and no other: a marker carrying
    anything else is evidence of nothing. **What this does not reach:** ``DATA-TYPES``.
    There the marker's **key** decides and its text is never checked, so
    ``DATA-TYPES`` with any text still suppresses the earlier-release refusal of a
    ``B``/``B_impl`` twin. The field is ``text`` and not ``value`` because ``value``
    is a model field name, and a ``marker.value`` read would be counted as a read of it.
    """

    sd_gid: str
    text: str


#: The access-points marker: the first ``SD`` of the group, emitted by the
#: whole-model writer on a behaviour that carries a point. It tells
#: ``import component --update`` that a runnable with no access means *none*.
ACCESS_POINTS = Marker("ACCESS-POINTS", "explicit")

#: The data-types marker, the component export's second ``SD``: the exporter
#: declares base and implementation types as the model does and invents no
#: ``{B}_impl`` twin. The bytes cannot tell this from an earlier writer's shape, so
#: only the marker can. It selects no regime; it only proves which release wrote a file.
DATA_TYPES = Marker("DATA-TYPES", "base and implementation types declared, no invented twin")

#: The operation-events marker, the component export's third ``SD``: the exporter
#: writes a runnable's operation events and its ``CAN-BE-INVOKED-CONCURRENTLY`` as
#: declared, so no ``OPERATION-INVOKED-EVENT`` and ``false`` mean *none* and *not
#: concurrent*. Exports before contract ``7.7.0`` wrote neither, so only the marker
#: tells the two apart.
OPERATION_EVENTS = Marker("OPERATION-EVENTS", "operation events and concurrency declared")

#: The inter-runnable variables marker, the fourth ``SD``: no
#: ``EXPLICIT-``/``IMPLICIT-INTER-RUNNABLE-VARIABLES`` and no
#: ``READ-``/``WRITTEN-LOCAL-VARIABLES`` mean *none*. Exports before contract
#: ``12.1.0`` wrote neither.
INTER_RUNNABLE_VARIABLES = Marker(
    "INTER-RUNNABLE-VARIABLES", "inter-runnable variables and local variable accesses declared"
)

#: The exclusive areas marker, the fifth ``SD``: no ``EXCLUSIVE-AREAS`` and no
#: ``CAN-ENTER-``/``RUNS-INSIDE-EXCLUSIVE-AREA-REFS`` mean *none*. Exports before
#: contract ``12.2.0`` wrote neither.
EXCLUSIVE_AREAS = Marker("EXCLUSIVE-AREAS", "exclusive areas and exclusive area references declared")

#: The data-received events marker, the sixth ``SD``: no ``DATA-RECEIVED-EVENT``
#: means *none*. Exports before contract ``12.3.0`` wrote none.
DATA_EVENTS = Marker("DATA-EVENTS", "data-received events declared")

#: The implicit access marker, the seventh ``SD``: an implicit access is written as
#: a ``DATA-READ-ACCESS`` or ``DATA-WRITE-ACCESS``, so an explicit form means
#: *explicit*. Exports before contract ``12.7.0`` wrote every access explicitly.
#: ``ACCESS-POINTS``' text ``explicit`` is a capability token, not a claim about accesses.
IMPLICIT_ACCESS = Marker("IMPLICIT-ACCESS", "implicit reads and writes declared")

#: The asynchronous calls marker, the eighth ``SD``: every call's synchrony (an
#: asynchronous call with its result point and ``TIMEOUT``) and every call-returned
#: event are written as declared, so a synchronous call point and no returns event
#: mean *synchronous* and *none*. Exports before contract ``12.7.0`` wrote neither.
ASYNCHRONOUS_CALLS = Marker("ASYNCHRONOUS-CALLS", "asynchronous calls, result points and call-returned events declared")

#: What ``generate_component_arxml`` appends to every behaviour, in this order, after
#: ``ACCESS-POINTS``. The order is the ``SD`` order in the group, so a new marker is
#: appended, never inserted. The whole-model ``generate arxml`` emits none of these;
#: every component export carries all of them, because a marker states what the
#: exporter does.
COMPONENT_EXPORT_MARKERS: tuple[Marker, ...] = (
    DATA_TYPES,
    OPERATION_EVENTS,
    INTER_RUNNABLE_VARIABLES,
    EXCLUSIVE_AREAS,
    DATA_EVENTS,
    IMPLICIT_ACCESS,
    ASYNCHRONOUS_CALLS,
)

#: Every ``SD`` the reader admits in the writer's own group. A group holding any
#: other stays foreign, reported, and aborts a component import.
OWN_MARKERS: tuple[Marker, ...] = (ACCESS_POINTS, *COMPONENT_EXPORT_MARKERS)
