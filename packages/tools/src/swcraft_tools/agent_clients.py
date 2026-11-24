"""The chat clients a project can be set up for: the one declared table, read and checked.

The table is ``templates/clients.json``: package data shipped beside the ``vehicle-architecture``
template it describes, never inside it, so ``swcraft init`` does not copy it into a project. It is
the one client table every consumer reads, so a further client is one row plus its files.

Each row: ``name`` (the CLI's ``--client`` value), ``label`` (the quick pick's), ``files`` (paths
relative to the template), ``hook`` and ``server`` (one of ``files`` or ``null``) and
``guideSection`` (``<page>#<anchor>`` in the user guide), and optionally ``preselectInHosts``: the
editor hosts in which Onboard Project pre-picks this row instead of the table's ``default`` (a
non-empty list of non-empty strings, matched against the host's ``vscode.env.uriScheme``; Kiro's row
names ``kiro``, its ``product.json`` ``urlProtocol``; one row at most names a given host). ``default``
names the clients chosen when the user names none, and the ones Onboard Project pre-picks in a host
no row names (VS Code among them). ``swcraft init`` has no host: it reads ``preselectInHosts`` only
to check it and always keeps ``default``. ``conflicts`` lists the choices that work but are known to
clash: when every client of an entry's ``clients`` is chosen, its one-line ``warning`` is shown (by
``swcraft init`` as a ``Warning:`` line on stderr, by Onboard Project in its report; a line two met
entries share is shown once) and nothing is asked -- the choice is the confirmation.
``copilotCompanionSettings`` is the one combination rule: when ``with`` is chosen together with any
of ``anyOf``, the ``settings`` keys go into ``file`` (VS Code 1.145.2 attaches a workspace-root
``AGENTS.md``/``CLAUDE.md`` to every Copilot local-agent request unless these keys turn it off).

It imports the standard library alone and names no layer, kind or field. What the checks here do
not reach: whether a listed file exists in the template, whether a guide anchor exists, and whether
the extension reads the table the same way.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.resources import files as resource_files
from typing import Any

#: Where the table lives, relative to the ``swcraft_tools`` package.
CLIENT_TABLE_RESOURCE = ("templates", "clients.json")

_ROW_KEYS = frozenset({"name", "label", "files", "hook", "server", "guideSection"})
_OPTIONAL_ROW_KEYS = frozenset({"preselectInHosts"})
_COMPANION_KEYS = frozenset({"file", "with", "anyOf", "settings"})
_CONFLICT_KEYS = frozenset({"clients", "warning", "guideSection"})
_TABLE_KEYS = frozenset({"$comment", "template", "default", "clients", "conflicts", "copilotCompanionSettings"})


class ClientTableError(ValueError):
    """The client table does not have the declared shape."""


@dataclass(frozen=True)
class AgentClient:
    """One row of the table."""

    name: str
    label: str
    files: tuple[str, ...]
    hook: str | None
    server: str | None
    guide_section: str
    preselect_in_hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompanionSettings:
    """The combination rule: ``settings`` into ``file`` when ``with`` and any of ``any_of`` are chosen."""

    file: str
    with_client: str
    any_of: tuple[str, ...]
    settings: dict[str, Any]

    def applies(self, chosen: Iterable[str]) -> bool:
        chosen_set = set(chosen)
        return self.with_client in chosen_set and bool(chosen_set.intersection(self.any_of))


@dataclass(frozen=True)
class Conflict:
    """A combination that works but clashes: ``warning`` is shown when every one of ``clients`` is chosen."""

    clients: tuple[str, ...]
    warning: str
    guide_section: str

    def applies(self, chosen: Iterable[str]) -> bool:
        return set(self.clients) <= set(chosen)


@dataclass(frozen=True)
class ClientTable:
    """The whole table, checked."""

    template: str
    default: tuple[str, ...]
    clients: tuple[AgentClient, ...]
    conflicts: tuple[Conflict, ...]
    companion: CompanionSettings

    def names(self) -> tuple[str, ...]:
        return tuple(client.name for client in self.clients)

    def client(self, name: str) -> AgentClient:
        for client in self.clients:
            if client.name == name:
                return client
        raise KeyError(name)

    def warnings_for(self, chosen: Iterable[str]) -> tuple[str, ...]:
        """The warning of every conflict the chosen set meets, in the table's order, each line once.

        Two entries may carry one line (Kiro with Codex, Kiro with Claude Code: the same ``AGENTS.md``
        is read twice either way), so a set meeting both prints it once, where it first appears.
        """
        chosen_set = set(chosen)
        return tuple(dict.fromkeys(conflict.warning for conflict in self.conflicts if conflict.applies(chosen_set)))

    def all_client_files(self) -> frozenset[str]:
        """Every file some client owns: the template's files that are written only when chosen."""
        return frozenset(path for client in self.clients for path in client.files)

    def files_for(self, chosen: Iterable[str]) -> frozenset[str]:
        """The client files *chosen* writes (a file two clients share is written once)."""
        return frozenset(path for name in chosen for path in self.client(name).files)


def _string(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ClientTableError(f"{where} must be a non-empty string")
    return value


def _string_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ClientTableError(f"{where} must be a non-empty list")
    items = tuple(_string(item, f"{where}[{index}]") for index, item in enumerate(value))
    if len(set(items)) != len(items):
        raise ClientTableError(f"{where} names an entry twice")
    return items


def _relative(path: str, where: str) -> str:
    if path.startswith("/") or "\\" in path or any(part in ("", ".", "..") for part in path.split("/")):
        raise ClientTableError(f"{where} must be a template-relative POSIX path: {path!r}")
    return path


def _guide_section(value: object, where: str) -> str:
    section = _string(value, where)
    page, _, anchor = section.partition("#")
    if not page.endswith(".md") or not anchor:
        raise ClientTableError(f"{where} must be <page>.md#<anchor>")
    return section


def _conflict(value: object, index: int) -> Conflict:
    where = f"conflicts[{index}]"
    if not isinstance(value, dict) or set(value) != _CONFLICT_KEYS:
        raise ClientTableError(f"{where} must hold exactly the keys {sorted(_CONFLICT_KEYS)}")
    clients = _string_list(value["clients"], f"{where}.clients")
    if len(clients) < 2:
        raise ClientTableError(f"{where}.clients must name at least two clients")
    warning = _string(value["warning"], f"{where}.warning")
    if "\n" in warning:
        raise ClientTableError(f"{where}.warning must be one line")
    return Conflict(
        clients=clients, warning=warning, guide_section=_guide_section(value["guideSection"], f"{where}.guideSection")
    )


def _row(value: object, index: int) -> AgentClient:
    where = f"clients[{index}]"
    if not isinstance(value, dict) or not _ROW_KEYS <= set(value) <= _ROW_KEYS | _OPTIONAL_ROW_KEYS:
        raise ClientTableError(
            f"{where} must hold exactly the keys {sorted(_ROW_KEYS)} (and optionally {sorted(_OPTIONAL_ROW_KEYS)})"
        )
    files = tuple(_relative(path, f"{where}.files") for path in _string_list(value["files"], f"{where}.files"))
    hook, server = value["hook"], value["server"]
    for key, member in (("hook", hook), ("server", server)):
        if member is not None and member not in files:
            raise ClientTableError(f"{where}.{key} must be null or one of its files")
    section = _guide_section(value["guideSection"], f"{where}.guideSection")
    hosts = _string_list(value["preselectInHosts"], f"{where}.preselectInHosts") if "preselectInHosts" in value else ()
    return AgentClient(
        name=_string(value["name"], f"{where}.name"),
        label=_string(value["label"], f"{where}.label"),
        files=files,
        hook=hook,
        server=server,
        guide_section=section,
        preselect_in_hosts=hosts,
    )


def parse_client_table(text: str) -> ClientTable:
    """Parse and check the table's text; :class:`ClientTableError` names the first problem."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClientTableError(f"the client table is not JSON: {exc}") from exc
    if (
        not isinstance(document, dict)
        or not set(document) <= _TABLE_KEYS
        or not (_TABLE_KEYS - {"$comment"}) <= set(document)
    ):
        raise ClientTableError(f"the client table must hold the keys {sorted(_TABLE_KEYS - {'$comment'})}")
    raw_clients = document["clients"]
    if not isinstance(raw_clients, list) or not raw_clients:
        raise ClientTableError("clients must be a non-empty list")
    clients = tuple(_row(value, index) for index, value in enumerate(raw_clients))
    names = [client.name for client in clients]
    if len(set(names)) != len(names):
        raise ClientTableError("clients names a client twice")
    hosts = [host for client in clients for host in client.preselect_in_hosts]
    for host in hosts:
        if hosts.count(host) > 1:
            raise ClientTableError(f"preselectInHosts names the host {host!r} in two rows")
    default = _string_list(document["default"], "default")
    raw_conflicts = document["conflicts"]
    if not isinstance(raw_conflicts, list):
        raise ClientTableError("conflicts must be a list")
    conflicts = tuple(_conflict(value, index) for index, value in enumerate(raw_conflicts))
    companion_value = document["copilotCompanionSettings"]
    if not isinstance(companion_value, dict) or set(companion_value) != _COMPANION_KEYS:
        raise ClientTableError(f"copilotCompanionSettings must hold exactly the keys {sorted(_COMPANION_KEYS)}")
    settings = companion_value["settings"]
    if not isinstance(settings, dict) or not settings:
        raise ClientTableError("copilotCompanionSettings.settings must be a non-empty object")
    companion = CompanionSettings(
        file=_relative(
            _string(companion_value["file"], "copilotCompanionSettings.file"), "copilotCompanionSettings.file"
        ),
        with_client=_string(companion_value["with"], "copilotCompanionSettings.with"),
        any_of=_string_list(companion_value["anyOf"], "copilotCompanionSettings.anyOf"),
        settings=dict(settings),
    )
    conflict_names = (name for conflict in conflicts for name in conflict.clients)
    for name in (*default, *conflict_names, companion.with_client, *companion.any_of):
        if name not in names:
            raise ClientTableError(f"{name!r} names no client of the table")
    return ClientTable(
        template=_string(document["template"], "template"),
        default=default,
        clients=clients,
        conflicts=conflicts,
        companion=companion,
    )


@functools.lru_cache(maxsize=1)
def load_client_table() -> ClientTable:
    """The shipped table, read through ``importlib.resources`` once per process.

    The cached table is shared: callers must not change its one mapping, the companion ``settings``.
    """
    resource = resource_files("swcraft_tools")
    for part in CLIENT_TABLE_RESOURCE:
        resource = resource / part
    return parse_client_table(resource.read_text(encoding="utf-8"))
