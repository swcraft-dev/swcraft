"""The links an ``update`` of a requirement's version with ``confirm_links`` adds.

The server adds the confirming operations after the client's, each a delta row
marked ``origin: "server"``:

* for every ``satisfies`` member naming the requirement at another version or at
  none, a ``remove`` of it and a ``create`` of ``<id>@<new>`` in the same list;
* only the ``remove`` when ``<id>@<new>`` is already in that list or an earlier
  server row creates it.

The new version may equal the current one (the re-import case). The links are
read off the **baseline**.

**Refused** ``schema-violation`` before staging: the flag on any other address or
on a value that is not a positive integer, and a flagged update of a requirement
whose version another update of the set also states.

**What it does not reach.** A member whose suffix is not a version, an
element-form member and a ``derived_from`` member are left alone. A member the
client removes (itself or with its holder; addresses compared reduced) is the
client's; one client remove covers **one** occurrence of a multiset member. A
missing requirement is pass B's refusal. A stale member the client's own
operations create is not confirmed and stays a warning of the staged model.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from swcraft_tools.editing import registry as reg
from swcraft_tools.editing.address import Address, FieldStep, MemberStep, reduce_address
from swcraft_tools.editing.dependents import covered
from swcraft_tools.editing.protocol import (
    ChangeOperation,
    CreateOperation,
    RemoveOperation,
    UpdateOperation,
    is_positive_int,
)
from swcraft_tools.editing.trace_candidates import protocol_address
from swcraft_tools.trace import LinkSite, is_element_form, link_sites_of_state, parse_link, stale_link
from swcraft_tools.trace_search import site_target

__all__ = ["LinkConfirmationNotApplicable", "VersionStatedTwice", "link_confirmations"]

#: The field a link list is.
_LINK_FIELD = "satisfies"


class LinkConfirmationNotApplicable(ValueError):
    """``confirm_links`` on an update it does not apply to: anything but a requirement's ``version``."""

    def __init__(self, address: Address) -> None:
        super().__init__(self._message(address))
        self.address = address

    @staticmethod
    def _message(address: Address) -> str:
        return (
            "confirm_links applies to the update of a requirement's version ({'layer': 'requirements', "
            "'element': ..., 'path': [{'field': 'version'}]}) with the new version, a positive integer, as the "
            "value; confirm a single link by removing it and creating the new one"
        )


class VersionStatedTwice(LinkConfirmationNotApplicable):
    """A flagged update of a requirement whose version another update of the same change set also states."""

    @staticmethod
    def _message(address: Address) -> str:
        return (
            f"confirm_links: this change set states requirement {address.element!r}'s version more than once; "
            "state the new version in one update and flag that one"
        )


def _applies(operation: UpdateOperation) -> bool:
    address = operation.address
    return (
        address.layer == "requirements"
        and tuple(address.path) == (FieldStep("version"),)
        and is_positive_int(operation.value)
    )


def _list_address(layer: str, element: Any, site: LinkSite) -> Address:
    """The address of the ``satisfies`` list *site* sits in: its holder's, as ``find`` for a requirement names it."""
    holder = protocol_address(site_target(layer, element, site))
    return Address(layer=holder.layer, element=holder.element, path=(*holder.path, FieldStep(_LINK_FIELD)))


def link_confirmations(
    snapshot: Any, operations: Sequence[ChangeOperation], *, registry: reg.Registry | None = None
) -> tuple[ChangeOperation, ...]:
    """The server's removes and creates for every ``confirm_links`` update among *operations*, in their order.

    They run after the client's operations, and after the removals a
    ``with_dependents`` remove adds (the engine appends them in that order:
    dependents, links, repairs). The link sites are read off the baseline
    *snapshot* (``snapshot.state``), in :func:`~swcraft_tools.trace.link_sites_of_state`'s
    order — the contract's layer order, then the element's, then the file's —
    and for each site to confirm the ``remove`` comes before the ``create``.

    Raises:
        LinkConfirmationNotApplicable: the flag on an update that is not of a
            requirement's ``version`` to a positive integer.
        VersionStatedTwice: a flagged update of a requirement whose version
            another update of the set also states (a subclass of the above).
    """
    registry = reg.or_current(registry)
    flagged = [
        operation for operation in operations if isinstance(operation, UpdateOperation) and operation.confirm_links
    ]
    for operation in flagged:
        if not _applies(operation):
            raise LinkConfirmationNotApplicable(operation.address)
    if not flagged:
        return ()
    for operation in flagged:
        stating = [
            other
            for other in operations
            if isinstance(other, UpdateOperation)
            and (other.address.layer, other.address.element) == ("requirements", operation.address.element)
            and tuple(other.address.path) in ((), (FieldStep("version"),))
        ]
        if len(stating) > 1:
            raise VersionStatedTwice(operation.address)
    removed = [
        reduce_address(operation.address, registry=registry)
        for operation in operations
        if isinstance(operation, RemoveOperation)
    ]
    # A client remove of a member takes one occurrence of it; a remove of what holds the list takes them all.
    remaining = Counter(removed)
    created = {
        reduce_address(operation.address, registry=registry)
        for operation in operations
        if isinstance(operation, CreateOperation)
    }
    requirements = {operation.address.element for operation in flagged}
    # Each site naming a flagged requirement, with the address of its list, computed once.
    sites = [
        (site, _list_address(layer, element, site))
        for layer, element, site in link_sites_of_state(snapshot.state.layers)
        if not is_element_form(site.value) and parse_link(site.value).id in requirements
    ]
    held: dict[Address, set[str]] = {}
    for site, holder in sites:
        held.setdefault(holder, set()).add(site.value)

    added: list[ChangeOperation] = []
    for operation in flagged:
        requirement = operation.address.element
        version = operation.value
        confirmed = f"{requirement}@{version}"
        for site, holder in sites:
            if parse_link(site.value).id != requirement:
                continue
            if stale_link(site.value, {requirement: [version]}) is None:
                continue
            old = reduce_address(replace(holder, path=(*holder.path, MemberStep(site.value))), registry=registry)
            new = reduce_address(replace(holder, path=(*holder.path, MemberStep(confirmed))), registry=registry)
            if remaining[old] > 0:
                # The client's own remove takes this occurrence away: the link is theirs.
                remaining[old] -= 1
                continue
            if covered(old, [other for other in removed if len(other.path) < len(old.path)]):
                # The client removes the element or member holding the list.
                continue
            added.append(RemoveOperation(old))
            if confirmed in held[holder] or new in created:
                continue
            created.add(new)
            added.append(CreateOperation(address=new, value=confirmed))
    return tuple(added)
