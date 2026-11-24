"""Data accesses, restated from SWCraft's benchmark generator (`PROVENANCE.md`).

Each component's first runnable `R1` writes every data element of each of its sender-receiver
provide ports and reads every data element of each of its sender-receiver require ports:
connected, unconnected and delegation-only ports alike. A client-server port and a port naming an
external interface stub (it lists no member) get none, and so does a component with no runnable.
The entries are in port order, then element order. Every specimen writes them; the WATERS presets
keep their own accesses.

What this does not reach: the passport pass reads no access, so no passport leaf states or
checks them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..model import AnyInterface, Interface, Port


@dataclass(frozen=True)
class Accesses:
    """One component's `R1` accesses: `(port, element)` pairs, in port and element order."""

    reads: tuple[tuple[str, str], ...]
    writes: tuple[tuple[str, str], ...]


def _elements(port: Port, interfaces: Mapping[str, AnyInterface]) -> list[tuple[str, str]]:
    interface = interfaces[port.interface]
    if not isinstance(interface, Interface) or interface.kind != "sender_receiver":
        return []
    return [(port.name, element.name) for element in interface.data_elements]


def data_accesses(ports: Sequence[Port], interfaces: Mapping[str, AnyInterface]) -> Accesses:
    """The accesses of a component with these ports; the caller gives them to its `R1`."""
    return Accesses(
        reads=tuple(access for port in ports if port.direction == "require" for access in _elements(port, interfaces)),
        writes=tuple(access for port in ports if port.direction == "provide" for access in _elements(port, interfaces)),
    )
