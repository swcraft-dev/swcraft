"""The large task set's names, each held to the role its element plays on the large bench.

The bench is the ``large-bench`` specimen, a copy of the cached tree loaded once, without
``check-model``. :data:`ROLES` holds one row per named element: the task, the name (or names)
it spells, and the role as a function over the bench's model; each row's predicate must select
that name, so a regeneration that moves a role fails here, by task, not as a wrong answer in a
``slow`` module. Ties break by identity in code-point order (a port's identity is its
component's name, then its own), first taken, unless a row names another figure.

Also held: the names the change tasks add are absent from the bench (:data:`ADDED`), each row's
names are spelled as whole tokens where the row says, every task has a row, and no file of the
set spells a name of the retired in-repo generator's scheme (:data:`OLD_NAMES`).

What this does not reach: a name the set spells and no row holds; whether a task is right on its
bench (``test_agent_task_bench_large.py``, ``slow``); and a figure of a task's prompt that moves
while the role stays (held by those modules).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import AGENT_BENCH, REPO_ROOT, agent_task_bench_module, agent_task_check_module, copy_specimen
from swcraft_tools.safety import ASIL_LEVELS

CHECK = agent_task_check_module()

Model = dict[str, dict[str, dict[str, Any]]]

#: The seven kinds of site a requirement link can sit on (task 18's ``with_layer``).
SITE_KINDS = frozenset({"swc", "port", "runnable", "interface", "composition", "task", "hardware"})


@pytest.fixture(scope="module")
def bench(tmp_path_factory: pytest.TempPathFactory) -> Model:
    """Return the ``large-bench`` specimen's model, read from a copy through the product's one loader."""
    large = agent_task_bench_module().LARGE_SPECIMEN
    return CHECK.read_model(copy_specimen(large, tmp_path_factory.mktemp("roles") / "bench"))


# The model's relations the roles are stated over.


def _ports(m: Model, component: str) -> list[dict[str, Any]]:
    return m["swc"][component].get("ports", [])


def hub(m: Model) -> str:
    """The component with the most ports."""
    return min(m["swc"], key=lambda name: (-len(_ports(m, name)), name))


def hub_port(m: Model) -> str:
    """The hub's first provide port by name (task 10's)."""
    return min(port["name"] for port in _ports(m, hub(m)) if port["direction"] == "provide")


def _connectors_at(m: Model) -> dict[tuple[str, str], list[str]]:
    ends: dict[tuple[str, str], list[str]] = {}
    for name in sorted(m["connectors"], key=str):
        connector = m["connectors"][name]
        for side in (connector["from"], connector["to"]):
            ends.setdefault((side["swc"], side["port"]), []).append(name)
    return ends


def _partners(m: Model, component: str) -> set[str]:
    partners = set()
    for connector in m["connectors"].values():
        sides = {connector["from"]["swc"], connector["to"]["swc"]}
        if component in sides:
            partners |= sides - {component}
    return partners


def _directions(m: Model, component: str) -> set[str]:
    return {port["direction"] for port in _ports(m, component)}


def two_way(m: Model) -> str:
    """A component with a provide and a require port, the fewest ports first."""
    candidates = [name for name in m["swc"] if _directions(m, name) == {"provide", "require"}]
    return min(candidates, key=lambda name: (len(_ports(m, name)), name))


def _task_of(m: Model, component: str) -> str:
    return min(
        name
        for name, task in m["tasks"].items()
        if any(entry.get("swc") == component for entry in task.get("runnables", []))
    )


def _partition_of(m: Model, component: str) -> str:
    return m["tasks"][_task_of(m, component)]["partition"]


def _hardware(m: Model, kind: str) -> list[str]:
    return sorted((name for name, element in m["hardware"].items() if element["kind"] == kind), key=str)


def _memory_of_core(m: Model, core: str) -> str:
    [memory] = [name for name in _hardware(m, "memory") if core in m["hardware"][name].get("accessible", [])]
    return memory


def _memory_of_partition(m: Model, partition: str) -> str:
    return _memory_of_core(m, m["hardware"][partition]["core"])


def _budgets(m: Model, memory: str) -> dict[str, int]:
    return {name: int((c.get("memory") or {}).get(memory, 0)) for name, c in m["swc"].items()}


@dataclass(frozen=True)
class Site:
    """One requirement link: where it sits (the element's layer word, the element, a member) and its text."""

    kind: str
    element: str
    member: str | None
    link: str


def sites(m: Model) -> dict[str, list[Site]]:
    """Every requirement link of the model, by the requirement id it names."""
    words = {"interfaces": "interface", "compositions": "composition", "tasks": "task", "hardware": "hardware"}
    found: dict[str, list[Site]] = {rid: [] for rid in m["requirements"]}
    for layer in ("swc", *words):
        for name in sorted(m[layer], key=str):
            element = m[layer][name]
            for link in element.get("satisfies", []):
                found.setdefault(link.split("@")[0], []).append(Site(words.get(layer, "swc"), name, None, link))
            for collection, kind in (("ports", "port"), ("runnables", "runnable")):
                for member in element.get(collection, []) if layer == "swc" else []:
                    for link in member.get("satisfies", []):
                        found.setdefault(link.split("@")[0], []).append(Site(kind, name, member["name"], link))
    return found


def _current(m: Model, site: Site) -> bool:
    rid, _, stated = site.link.partition("@")
    return stated == str(m["requirements"][rid]["version"])


def _spread_current(m: Model) -> list[str]:
    """The requirements linked at all seven site kinds, every link at the requirement's version."""
    found = sites(m)
    return sorted(
        (
            rid
            for rid, links in found.items()
            if {site.kind for site in links} == SITE_KINDS and all(_current(m, site) for site in links)
        ),
        key=str,
    )


def _text(m: Model, rid: str) -> str:
    requirement = m["requirements"][rid]
    return f"{requirement.get('title', '')}\n{requirement.get('description', '')}".casefold()


def _about_supply_voltage(m: Model, rid: str) -> bool:
    return "supply voltage" in " ".join(_text(m, rid).split())


def spread_about_supply_voltage(m: Model) -> str:
    """Bench task 18's requirement: linked at all seven site kinds, every link current, about supply voltage."""
    return min(rid for rid in _spread_current(m) if _about_supply_voltage(m, rid))


def spread_not_about_supply_voltage(m: Model) -> str:
    """Bench task 24's requirement: linked at all seven site kinds, every link current, not about supply voltage."""
    return min(rid for rid in _spread_current(m) if not _about_supply_voltage(m, rid))


def stale(m: Model) -> str:
    """Bench task 20's requirement: the one with a link below its version."""
    [rid] = sorted({rid for rid, links in sites(m).items() for site in links if not _current(m, site)}, key=str)
    return rid


def stale_holders(m: Model) -> list[str]:
    return sorted({site.element for site in sites(m)[stale(m)] if not _current(m, site)}, key=str)


def unallocated(m: Model) -> list[str]:
    return sorted((rid for rid, links in sites(m).items() if not links), key=str)


def common(m: Model) -> str:
    """The requirement every component satisfies."""
    [rid] = [rid for rid, links in sites(m).items() if {s.element for s in links if s.kind == "swc"} >= set(m["swc"])]
    return rid


def one_site(m: Model) -> list[str]:
    return sorted((rid for rid, links in sites(m).items() if len(links) == 1), key=str)


# The roles, task by task.


def forwarded_interface(m: Model) -> str:
    """Bench task 01: a sender-receiver interface no port of either component names."""
    used = {port["interface"] for name in (two_way(m), unconnected_requirer(m)) for port in _ports(m, name)}
    return min(name for name, i in m["interfaces"].items() if i["kind"] == "sender_receiver" and name not in used)


def unconnected_requirer(m: Model) -> str:
    """Bench task 01: a component with a require port and no connector to the provider, fewest ports first."""
    provider = two_way(m)
    partners = _partners(m, provider)
    candidates = [n for n in m["swc"] if n != provider and n not in partners and "require" in _directions(m, n)]
    return min(candidates, key=lambda name: (len(_ports(m, name)), name))


def logged_port(m: Model) -> tuple[str, str]:
    """Bench task 02: a provide port ending one connector, its one link naming a requirement linked nowhere else."""
    ends = _connectors_at(m)
    alone = set(one_site(m))
    return min(
        (name, port["name"])
        for name in m["swc"]
        for port in _ports(m, name)
        if port["direction"] == "provide"
        and len(port.get("satisfies", [])) == 1
        and port["satisfies"][0].split("@")[0] in alone
        and len(ends.get((name, port["name"]), [])) == 1
    )


def _logged(m: Model) -> dict[str, Any]:
    component, name = logged_port(m)
    [port] = [port for port in _ports(m, component) if port["name"] == name]
    return port


def logged_connector(m: Model) -> str:
    [connector] = _connectors_at(m)[logged_port(m)]
    return connector


def logged_interface(m: Model) -> str:
    return _logged(m)["interface"]


def logged_element(m: Model) -> str:
    return min(element["name"] for element in m["interfaces"][logged_interface(m)]["data_elements"])


def logged_requirement(m: Model) -> str:
    [link] = _logged(m)["satisfies"]
    return link


def logger_requirement(m: Model) -> str:
    """Bench task 02: a requirement linked at one site, other than the logged port's."""
    return min(rid for rid in one_site(m) if rid != logged_requirement(m).split("@")[0])


def logger_task(m: Model) -> str:
    """Bench task 02: the task, of the logged component's partition and mapping a runnable, with the longest period."""
    partition = _partition_of(m, logged_port(m)[0])
    tasks = [name for name, task in m["tasks"].items() if task["partition"] == partition and task.get("runnables")]
    return min(tasks, key=lambda name: (-float(m["tasks"][name]["period"]), name))


def logger_memory(m: Model) -> str:
    return _memory_of_partition(m, m["tasks"][logger_task(m)]["partition"])


def mapped_implementation_type(m: Model) -> str:
    """Bench task 03: an implementation type the mapping set maps."""
    [mapping_set] = m["mapping_sets"].values()
    return min(entry["implementation"] for entry in mapping_set["maps"])


def mapping_set(m: Model) -> str:
    [name] = m["mapping_sets"]
    return name


def _unsequenced(m: Model, task: str) -> list[tuple[str, str]]:
    named = {(step["swc"], step["runnable"]) for sequence in m["sequences"].values() for step in sequence["steps"]}
    entries = [(entry["swc"], entry["runnable"]) for entry in m["tasks"][task].get("runnables", [])]
    return [entry for entry in entries if entry not in named]


def _level(m: Model, component: str) -> str:
    return m["swc"][component].get("asil", "QM")


def _twins(m: Model, task: str, component: str) -> list[str]:
    """The other tasks of *task*'s partition and period that take *component*'s runnable with no coexistence argument.

    A task takes it when it declares a coexistence argument already, or when every component it schedules
    is at *component*'s level.
    """
    here = m["tasks"][task]
    return sorted(
        name
        for name, other in m["tasks"].items()
        if name != task
        and other["partition"] == here["partition"]
        and other["period"] == here["period"]
        and (
            other.get("coexistence")
            or all(_level(m, entry["swc"]) == _level(m, component) for entry in other.get("runnables", []))
        )
    )


def _move(m: Model) -> tuple[str, tuple[str, str], str]:
    """Bench task 04: the first task whose first unsequenced runnable (in the task's order) has a twin to go to."""
    for task in sorted(m["tasks"], key=str):
        unsequenced = _unsequenced(m, task)
        if unsequenced and _twins(m, task, unsequenced[0][0]):
            return task, unsequenced[0], _twins(m, task, unsequenced[0][0])[0]
    raise LookupError("no task has a runnable to move")


def move_source(m: Model) -> str:
    return _move(m)[0]


def moved_runnable(m: Model) -> tuple[str, str]:
    return _move(m)[1]


def move_target(m: Model) -> str:
    return _move(m)[2]


def removed_component(m: Model) -> str:
    """Bench task 05: a component a sequence step names, every port connected, the fewest ports first."""
    ends = _connectors_at(m)
    named = {step["swc"] for sequence in m["sequences"].values() for step in sequence["steps"]}
    candidates = [name for name in named if all((name, port["name"]) in ends for port in _ports(m, name))]
    return min(candidates, key=lambda name: (len(_ports(m, name)), name))


def removed_connectors(m: Model) -> list[str]:
    component = removed_component(m)
    return sorted((n for n, c in m["connectors"].items() if component in {c["from"]["swc"], c["to"]["swc"]}), key=str)


def removed_entries(m: Model) -> tuple[list[str], list[str]]:
    component = removed_component(m)
    tasks = sorted(n for n, t in m["tasks"].items() if any(e["swc"] == component for e in t.get("runnables", [])))
    sequences = sorted(n for n, s in m["sequences"].items() if any(e["swc"] == component for e in s["steps"]))
    return tasks, sequences


def removed_neighbour_port(m: Model) -> tuple[str, str]:
    """Bench task 05: the port at the other end of the removed component's first port's first connector."""
    component = removed_component(m)
    first = _ports(m, component)[0]["name"]
    connector = m["connectors"][_connectors_at(m)[(component, first)][0]]
    other = connector["to"] if connector["from"]["swc"] == component else connector["from"]
    return other["swc"], other["port"]


def _port_counts(m: Model) -> Counter[str]:
    return Counter(port["interface"] for name in m["swc"] for port in _ports(m, name))


def most_used_interface(m: Model) -> str:
    """Bench task 06: the interface the most ports name."""
    counts = _port_counts(m)
    return min(counts, key=lambda name: (-counts[name], name))


def renamed_element(m: Model) -> str:
    """Bench task 06: the most-used interface's last data element by name."""
    return max(element["name"] for element in m["interfaces"][most_used_interface(m)]["data_elements"])


def accessing_components(m: Model) -> list[str]:
    """Bench task 06: the components whose runnables read or write the renamed element (they move with it)."""
    interface, element = most_used_interface(m), renamed_element(m)
    found = set()
    for name in m["swc"]:
        on = {port["name"] for port in _ports(m, name) if port["interface"] == interface}
        for runnable in m["swc"][name].get("runnables", []):
            for access in runnable.get("reads", []) + runnable.get("writes", []):
                if access["port"] in on and access["element"] == element:
                    found.add(name)
    return sorted(found, key=str)


def unlinked_require_port(m: Model) -> tuple[str, str]:
    """Bench task 07: a connected require port with no requirement link."""
    ends = _connectors_at(m)
    return min(
        (name, port["name"])
        for name in m["swc"]
        for port in _ports(m, name)
        if port["direction"] == "require" and not port.get("satisfies") and (name, port["name"]) in ends
    )


def _users(m: Model) -> dict[str, list[str]]:
    users: dict[str, set[str]] = {}
    for name in m["swc"]:
        for port in _ports(m, name):
            users.setdefault(port["interface"], set()).add(name)
    return {interface: sorted(names, key=str) for interface, names in users.items()}


def _hub_interfaces(m: Model) -> set[str]:
    return {port["interface"] for port in _ports(m, hub(m))}


def impact_interface(m: Model) -> str:
    """Bench task 08: an interface a hub port names with the most component users, other than the most-used one."""
    users = _users(m)
    candidates = _hub_interfaces(m) - {most_used_interface(m)}
    return min(candidates, key=lambda name: (-len(users[name]), name))


def four_user_interface(m: Model) -> str:
    """Bench task 12: an interface four components' ports name, the hub among them, other than task 08's."""
    users = _users(m)
    return min(
        name
        for name in _hub_interfaces(m) - {impact_interface(m)}
        if len(users[name]) == 4 and m["interfaces"][name]["kind"] == "sender_receiver"
    )


def without_a_port_of(m: Model, interface: str) -> list[str]:
    """Bench task 12's wrong ones: the first four components by name with no port of the interface."""
    users = set(_users(m)[interface])
    return sorted((name for name in m["swc"] if name not in users), key=str)[:4]


def _headroom(m: Model, memory: str) -> float:
    element = m["hardware"][memory]
    return element["size"] * element["threshold"] - sum(_budgets(m, memory).values())


def _free(m: Model, memory: str) -> int:
    return element_size(m, memory) - sum(_budgets(m, memory).values())


def element_size(m: Model, memory: str) -> int:
    return int(m["hardware"][memory]["size"])


def tight_core(m: Model) -> str:
    """Bench task 09: the core whose memory has the least headroom under its threshold, over 40 960 bytes free."""
    cores = [core for core in _hardware(m, "core") if _free(m, _memory_of_core(m, core)) > 40 * 1024]
    return min(cores, key=lambda core: (_headroom(m, _memory_of_core(m, core)), core))


def unconnected_ports(m: Model) -> list[str]:
    """Bench task 13: every component port no connector ends at, as ``component.port``."""
    ends = _connectors_at(m)
    return sorted(
        (f"{name}.{port['name']}" for name in m["swc"] for port in _ports(m, name) if (name, port["name"]) not in ends),
        key=str,
    )


def at_level(m: Model, level: str) -> list[str]:
    return sorted((name for name, c in m["swc"].items() if c.get("asil", "QM") == level), key=str)


def lowest_rated_partition(m: Model) -> str:
    """Bench task 15: the partition at the lowest rating."""
    return min(_hardware(m, "partition"), key=lambda name: (ASIL_LEVELS.index(m["hardware"][name]["asil"]), name))


def in_partition(m: Model, partition: str) -> list[str]:
    return sorted((name for name in m["swc"] if _partition_of(m, name) == partition), key=str)


def outside_partition(m: Model, partition: str) -> list[str]:
    """Bench task 15's wrong ones: the first component of each other partition, then the hub."""
    others = [p for p in _hardware(m, "partition") if p != partition]
    return [in_partition(m, p)[0] for p in others] + [hub(m)]


def hub_core(m: Model) -> str:
    """Bench task 16: the core of the hub's partition."""
    return m["hardware"][_partition_of(m, hub(m))]["core"]


def tasks_on_core(m: Model, core: str) -> list[str]:
    return sorted((n for n, t in m["tasks"].items() if m["hardware"][t["partition"]]["core"] == core), key=str)


def tasks_off_core(m: Model, core: str) -> list[str]:
    """Return bench task 16's wrong tasks.

    The first runnable-mapping task of each of the first two other partitions, then the idle tasks.
    """
    others = [p for p in _hardware(m, "partition") if m["hardware"][p]["core"] != core]
    first = [min(n for n, t in m["tasks"].items() if t["partition"] == p and t.get("runnables")) for p in others[:2]]
    idle = sorted(
        (n for n, t in m["tasks"].items() if not t.get("runnables") and m["hardware"][t["partition"]]["core"] != core),
        key=str,
    )
    return first + idle


def idle_task_on_hub_core(m: Model) -> str:
    """Bench task 16's note: the task on the hub's core that maps no runnable."""
    [task] = [name for name in tasks_on_core(m, hub_core(m)) if not m["tasks"][name].get("runnables")]
    return task


def largest_budget_memory(m: Model) -> str:
    """Bench task 17: the memory holding the largest single budget."""
    memories = _hardware(m, "memory")
    return min(memories, key=lambda memory: (-max(_budgets(m, memory).values()), memory))


def consumers(m: Model, memory: str) -> list[str]:
    return sorted((name for name, amount in _budgets(m, memory).items() if amount), key=str)


def top_consumers(m: Model, memory: str) -> dict[str, int]:
    budgets = _budgets(m, memory)
    return dict(sorted(((n, a) for n, a in budgets.items() if a), key=lambda item: (-item[1], item[0]))[:5])


def site_names(m: Model, rid: str) -> list[dict[str, str]]:
    """Bench task 18's ``with_layer`` entries, in the order the slow module sorts them."""
    entries = []
    for site in sites(m)[rid]:
        entry = {"name": site.member or site.element, "layer": site.kind}
        if site.member is not None:
            entry["owner"] = site.element
        entries.append(entry)
    return sorted(entries, key=lambda entry: sorted(entry.items()))


def other_sites(m: Model, rid: str, other: str, kinds: tuple[str, ...]) -> list[str]:
    """The elements of *other*'s sites of *kinds* (a member site by its owner) that are no site of *rid*."""
    mine = {site.element for site in sites(m)[rid]}
    found = []
    for kind in kinds:
        for site in sites(m)[other]:
            if site.kind == kind and site.element not in mine and site.element not in found:
                found.append(site.element)
    return found


def stale_current_sites(m: Model) -> list[str]:
    """Bench task 20's wrong ones: the stale requirement's current runnable owner, interface and task, less a holder."""
    holders = set(stale_holders(m))
    found = []
    for kind in ("runnable", "interface", "task"):
        for site in sites(m)[stale(m)]:
            if site.kind == kind and _current(m, site) and site.element not in holders:
                found.append(site.element)
    return found


def about_neither_word(m: Model) -> list[str]:
    """Bench task 21's wrong ones: the requirements whose text holds neither supply nor voltage."""
    return sorted(
        (rid for rid in m["requirements"] if "supply" not in _text(m, rid) and "voltage" not in _text(m, rid)), key=str
    )


def about_one_word(m: Model) -> list[str]:
    return sorted(
        (
            rid
            for rid in m["requirements"]
            if ("supply" in _text(m, rid)) != ("voltage" in _text(m, rid)) and not _about_supply_voltage(m, rid)
        ),
        key=str,
    )


def hub_requirement(m: Model) -> str:
    """Bench task 22: the unallocated requirement whose text names a hub."""
    [rid] = [rid for rid in unallocated(m) if "hub" in _text(m, rid).split()]
    return rid


def unlinked_two_way_provide_port(m: Model) -> tuple[str, str]:
    """Bench task 23: a provide port carrying no requirement link, of a component with a provide and a require port."""
    return min(
        (name, port["name"])
        for name in m["swc"]
        if _directions(m, name) == {"provide", "require"}
        for port in _ports(m, name)
        if port["direction"] == "provide" and not port.get("satisfies")
    )


@dataclass(frozen=True)
class Role:
    """One named element: where it is spelled, the name (or names) spelled there, and the role selecting it."""

    where: str
    name: Any
    role: Callable[[Model], Any]


HUB = "Swc001abcdefghij"
MEMORIES = ["Me1abc", "Me2abc", "Me3abc", "Me4abc"]
#: The two ``slow`` modules that spell the large bench's names beside the tasks.
BENCH_MODULE = "test_agent_task_bench_large.py"
QUESTIONS_MODULE = "test_benchmark_questions_large.py"

ROLES: list[Role] = [
    # 01: an unconnected provider and requirer, and an interface neither has a port of.
    Role("01-connect-two-components", "Swc075abcdefghij", two_way),
    Role("01-connect-two-components", "Swc076abcdefghij", unconnected_requirer),
    Role("01-connect-two-components", "If0001abcdefghij", forwarded_interface),
    # 02: the logged port and what hangs on it; the logger's requirement, task and memories.
    Role("02-brake-data-logger", ("Swc150abcdefghi", "P10"), logged_port),
    Role("02-brake-data-logger", "Conn1450abcdefghij", logged_connector),
    Role("02-brake-data-logger", "If0471abcdefghij", logged_interface),
    Role("02-brake-data-logger", "D1", logged_element),
    Role("02-brake-data-logger", "Rq07abcdef@1", logged_requirement),
    Role("02-brake-data-logger", "Rq04abcdef", logger_requirement),
    Role("02-brake-data-logger", "Ts30abcde", logger_task),
    Role("02-brake-data-logger", "Me4abc", logger_memory),
    Role("02-brake-data-logger", MEMORIES, lambda m: _hardware(m, "memory")),
    # 03: the component given the port, the implementation type and the mapping set.
    Role("03-new-interface-and-type", "Swc075abcdefghij", two_way),
    Role("03-new-interface-and-type", "Vt001abcde", mapped_implementation_type),
    Role("03-new-interface-and-type", "Ms1abcdefghi", mapping_set),
    # 04: the move.
    Role("04-move-runnable", "Ts09abcde", move_source),
    Role("04-move-runnable", ("Swc044abcdefghij", "R1"), moved_runnable),
    Role("04-move-runnable", "Ts14abcde", move_target),
    # 05: the removed component and everything that refers to it; the ports that stay.
    Role("05-remove-component", "Swc076abcdefghij", removed_component),
    Role(
        "05-remove-component",
        [*(f"Conn{n:04d}abcdefghij" for n in range(471, 477)), *(f"Conn{n:04d}abcdefghij" for n in range(929, 936))],
        removed_connectors,
    ),
    Role("05-remove-component", (["Ts16abcde"], ["Sq16abcdefghi"]), removed_entries),
    Role("05-remove-component", ("Swc005abcdefghij", "P3"), removed_neighbour_port),
    Role("05-remove-component", HUB, hub),
    Role("05-remove-component", "P1", hub_port),
    # 06: the most-used interface, its element, and the components whose accesses move with it.
    Role("06-rename-data-element", "If0001abcdefghij", most_used_interface),
    Role("06-rename-data-element", "D1", renamed_element),
    Role(
        "06-rename-data-element",
        ["Swc004abcdefghij", *(f"Swc{n}abcdefghi" for n in range(134, 144))],
        accessing_components,
    ),
    # 07: the port the new requirement is linked to.
    Role("07-add-requirement", ("Swc005abcdefghij", "P10"), unlinked_require_port),
    # 08: the interface and its direct users.
    Role("08-interface-change-impact", "If0168abcdefghij", impact_interface),
    Role(
        "08-interface-change-impact",
        [HUB, "Swc005abcdefghij", "Swc043abcdefghij", "Swc075abcdefghij"],
        lambda m: _users(m)[impact_interface(m)],
    ),
    # 09: the core and the memory it reaches.
    Role("09-ram-for-a-component", "Co1abc", tight_core),
    Role("09-ram-for-a-component", "Me1abc", lambda m: _memory_of_core(m, tight_core(m))),
    # 10: the hub's port the imported component connects to, its interface, and the memories.
    Role("10-import-component", HUB, hub),
    Role("10-import-component", "P1", hub_port),
    Role("10-import-component", MEMORIES, lambda m: _hardware(m, "memory")),
    # 11: the hub and its ports.
    Role("11-ports-of-a-component", HUB, hub),
    Role("11-ports-of-a-component", [f"P{n}" for n in range(1, 551)], lambda m: [p["name"] for p in _ports(m, hub(m))]),
    # 12: the interface, its users and four components without a port of it.
    Role("12-components-on-an-interface", "If0169abcdefghij", four_user_interface),
    Role(
        "12-components-on-an-interface",
        [HUB, "Swc005abcdefghij", "Swc043abcdefghij", "Swc076abcdefghij"],
        lambda m: _users(m)[four_user_interface(m)],
    ),
    Role(
        "12-components-on-an-interface",
        ["Swc002abcdefghij", "Swc003abcdefghij", "Swc004abcdefghij", "Swc006abcdefghij"],
        lambda m: without_a_port_of(m, four_user_interface(m)),
    ),
    # 13: the unconnected ports.
    Role(
        "13-unconnected-ports",
        [*(f"Swc00{n}abcdefghij.P1" for n in range(5, 10)), "Swc010abcdefghij.P15"],
        unconnected_ports,
    ),
    # 14: the components at ASIL B.
    Role("14-asil-b-count", [f"Swc{n:03d}abcdefghij" for n in range(17, 41)], lambda m: at_level(m, "B")),
    # 15: the partition, its components, and the wrong ones.
    Role("15-components-in-a-partition", "Pa3ab", lowest_rated_partition),
    Role(
        "15-components-in-a-partition",
        [f"Swc{n:03d}abcdefghij" for n in range(76, 123)] + [f"Swc{n}abcdefghi" for n in range(123, 126)],
        lambda m: in_partition(m, lowest_rated_partition(m)),
    ),
    Role(
        "15-components-in-a-partition",
        [HUB, "Swc026abcdefghij", "Swc126abcdefghi", HUB],
        lambda m: outside_partition(m, lowest_rated_partition(m)),
    ),
    # 16: the core, its tasks, and the wrong ones.
    Role("16-tasks-on-a-core", "Co1abc", hub_core),
    Role(
        "16-tasks-on-a-core",
        ["Ts01abcdefgh", "Ts02abcdef", "Ts03abcdef", "Ts04abcdef", "Ts05abcde", "Ts31abcde"],
        lambda m: tasks_on_core(m, hub_core(m)),
    ),
    Role("16-tasks-on-a-core", ["Ts06abcde", "Ts16abcde", "Ts32abcde"], lambda m: tasks_off_core(m, hub_core(m))),
    # 17: the memory, its consumers and the largest of them.
    Role("17-largest-ram-consumer", "Me1abc", largest_budget_memory),
    Role(
        "17-largest-ram-consumer",
        [f"Swc{n:03d}abcdefghij" for n in range(1, 26)],
        lambda m: consumers(m, largest_budget_memory(m)),
    ),
    Role(
        "17-largest-ram-consumer",
        {
            HUB: 16384,
            "Swc002abcdefghij": 8192,
            "Swc003abcdefghij": 4096,
            "Swc010abcdefghij": 4096,
            "Swc018abcdefghij": 4096,
        },
        lambda m: top_consumers(m, largest_budget_memory(m)),
    ),  # fmt: skip
    # 18: the requirement, its seven sites, and task 24's sites that are none of them.
    Role("18-elements-satisfying-a-requirement", "Rq03abcdef", spread_about_supply_voltage),
    Role(
        "18-elements-satisfying-a-requirement",
        [
            {"name": "Root", "layer": "composition"},
            {"name": "Cp1abcdefg", "layer": "hardware"},
            {"name": "If0003abcdefghij", "layer": "interface"},
            {"name": "P3", "layer": "port", "owner": HUB},
            {"name": "R1", "layer": "runnable", "owner": "Swc003abcdefghij"},
            {"name": "Swc003abcdefghij", "layer": "swc"},
            {"name": "Ts03abcdef", "layer": "task"},
        ],
        lambda m: site_names(m, spread_about_supply_voltage(m)),
    ),
    Role(
        "18-elements-satisfying-a-requirement",
        ["Swc002abcdefghij", "If0002abcdefghij", "Ts02abcdef"],
        lambda m: other_sites(
            m,
            spread_about_supply_voltage(m),
            spread_not_about_supply_voltage(m),
            ("swc", "interface", "task", "hardware"),
        ),
    ),
    # 19: the unallocated requirements, and one of each other kind.
    Role("19-unallocated-requirements", ["Rq16abcdef", "Rq17abcdef", "Rq18abcdef", "Rq19abcdef"], unallocated),
    Role(
        "19-unallocated-requirements",
        ["Rq04abcdef", "Rq03abcdef", "Rq01abcdefghijk", "Rq20abcdef"],
        lambda m: [one_site(m)[0], spread_about_supply_voltage(m), stale(m), common(m)],
    ),
    # 20: the stale requirement, the elements holding its old links, and its current sites.
    Role("20-links-needing-confirmation", "Rq01abcdefghijk", stale),
    Role("20-links-needing-confirmation", [HUB], stale_holders),
    Role("20-links-needing-confirmation", ["If0001abcdefghij", "Ts01abcdefgh"], stale_current_sites),
    # 21: the requirements about the phrase, about one of its words, and about neither.
    Role(
        "21-requirements-about-a-topic",
        ["Rq01abcdefghijk", "Rq03abcdef"],
        lambda m: sorted((rid for rid in m["requirements"] if _about_supply_voltage(m, rid)), key=str),
    ),
    Role("21-requirements-about-a-topic", ["Rq16abcdef", "Rq19abcdef"], about_one_word),
    Role(
        "21-requirements-about-a-topic",
        ["Rq02abcdef", *(f"Rq{n:02d}abcdef" for n in range(4, 16)), "Rq17abcdef", "Rq18abcdef", "Rq20abcdef"],
        about_neither_word,
    ),
    # 22: the requirement the trace search is asked about.
    Role("22-candidates-for-a-requirement", "Rq17abcdef", hub_requirement),
    # 23: the port and the requirement it is linked to.
    Role("23-link-a-port-to-a-requirement", ("Swc075abcdefghij", "P1"), unlinked_two_way_provide_port),
    Role("23-link-a-port-to-a-requirement", "Rq03abcdef", spread_about_supply_voltage),
    # 24: the requirement whose links are confirmed at its new version.
    Role("24-confirm-links-at-a-new-version", "Rq02abcdef", spread_not_about_supply_voltage),
    # The two slow modules' own names: the requests' parameters and the recomputations' targets.
    Role(QUESTIONS_MODULE, HUB, hub),
    Role(QUESTIONS_MODULE, "If0169abcdefghij", four_user_interface),
    Role(QUESTIONS_MODULE, "Pa3ab", lowest_rated_partition),
    Role(QUESTIONS_MODULE, "Co1abc", hub_core),
    Role(QUESTIONS_MODULE, "Me1abc", largest_budget_memory),
    Role(QUESTIONS_MODULE, "Me1abc", lambda m: _memory_of_core(m, tight_core(m))),
    Role(QUESTIONS_MODULE, "Ts31abcde", idle_task_on_hub_core),
    Role(QUESTIONS_MODULE, "Swc076abcdefghij", removed_component),
    Role(QUESTIONS_MODULE, "Rq03abcdef", spread_about_supply_voltage),
    Role(QUESTIONS_MODULE, "Rq02abcdef", spread_not_about_supply_voltage),
    Role(QUESTIONS_MODULE, "Rq17abcdef", hub_requirement),
    Role(QUESTIONS_MODULE, ["Rq16abcdef", "Rq19abcdef"], about_one_word),
    Role(BENCH_MODULE, HUB, hub),
    Role(BENCH_MODULE, "If0168abcdefghij", impact_interface),
    Role(BENCH_MODULE, "If0169abcdefghij", four_user_interface),
    Role(BENCH_MODULE, "Me1abc", largest_budget_memory),
    Role(BENCH_MODULE, "Pa3ab", lowest_rated_partition),
    Role(BENCH_MODULE, "Co1abc", hub_core),
    Role(BENCH_MODULE, "Ts31abcde", idle_task_on_hub_core),
    Role(BENCH_MODULE, "Rq03abcdef", spread_about_supply_voltage),
    Role(BENCH_MODULE, "Rq01abcdefghijk", stale),
    Role(BENCH_MODULE, "Rq17abcdef", hub_requirement),
]

#: What the change tasks add, by layer: a name the bench already holds would make the new element an old one.
ADDED = {
    "swc": ["WlDataLogger", "WlLampController"],
    "connectors": ["WlForwardedConnector", "WlLoggerConnector", "WlHubToLamp"],
    "interfaces": ["IWlPadWear"],
    "application_types": ["WlPadWear_Pct"],
    "requirements": ["SWR-WL-100"],
}
#: The members the change tasks add to an existing element: (layer, element role, collection, name).
ADDED_MEMBERS = [
    ("swc", two_way, "ports", "pForwardedOutput"),
    ("swc", unconnected_requirer, "ports", "rForwardedOutput"),
    ("swc", two_way, "ports", "pPadWear"),
    ("interfaces", most_used_interface, "data_elements", "deShared"),
]


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in (*_strings(key), *_strings(item))]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    return []


def _id(role: Role) -> str:
    return f"{role.where}:{'+'.join(_strings(role.name))[:60]}"


@pytest.mark.parametrize("role", ROLES, ids=[_id(role) for role in ROLES])
def test_each_role_selects_the_name_its_task_spells(role: Role, bench: Model) -> None:
    assert role.role(bench) == role.name


def _spelled_as_a_token(name: str, text: str) -> bool:
    """Whether *text* spells *name* as a whole token: ``P3`` is not met by ``P31`` or ``rP3``."""
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text) is not None


def test_a_name_is_met_as_a_whole_token_only() -> None:
    assert _spelled_as_a_token("P3", '"port": "P3"') and _spelled_as_a_token("Root", "the Root composition")
    assert not _spelled_as_a_token("P3", '"P31", "P30", "rP3"')


@pytest.mark.parametrize(("layer", "name"), [(layer, name) for layer, names in ADDED.items() for name in names])
def test_a_name_the_set_adds_is_none_of_the_benchs(layer: str, name: str, bench: Model) -> None:
    assert name not in bench[layer]


@pytest.mark.parametrize(("layer", "owner", "collection", "name"), ADDED_MEMBERS, ids=[m[3] for m in ADDED_MEMBERS])
def test_a_member_the_set_adds_is_none_of_its_owners(
    layer: str, owner: Callable[[Model], str], collection: str, name: str, bench: Model
) -> None:
    assert name not in {member["name"] for member in bench[layer][owner(bench)].get(collection, [])}


#: The retired in-repo generator's naming scheme: its ``Wl`` element names (``WlHubSwc``,
#: ``WlSrInterface010``, ``WlMappingSet``, ``WlPrim...``, ``WlRequirerSwc...``), its ``REQ-WL-`` ids, its
#: ``CORE<n>``, ``PARTITION<n>`` and ``RAM<n>`` hardware and its ``pOut...``/``rIn...`` ports.
OLD_NAMES = re.compile(
    r"(?<![A-Za-z0-9_])(?:Wl[A-Z][A-Za-z0-9_]*|REQ-WL-\d+|CORE\d+|PARTITION\d+|RAM\d+|pOut\w*|rIn\w*)"
)


def _added_names() -> frozenset[str]:
    """The names the change tasks add (:data:`ADDED`, :data:`ADDED_MEMBERS`): the test tree's own."""
    return frozenset(name for names in ADDED.values() for name in names) | {member[3] for member in ADDED_MEMBERS}


def test_the_old_name_scheme_matches_the_retired_names_and_spares_the_added_ones() -> None:
    retired = ["WlHubSwc", "WlSrInterface010", "WlCsInterface003", "WlMappingSet", "WlPrimU8", "REQ-WL-001"]
    retired += ["CORE0", "PARTITION1", "RAM3", "pOut12", "rIn4"]
    assert [name for name in retired if not OLD_NAMES.fullmatch(name)] == []
    assert OLD_NAMES.findall("IWlPadWear Rq03abcdef Swc075abcdefghij P1") == []
    assert set(OLD_NAMES.findall(" ".join(sorted(_added_names())))) <= _added_names()


# The set's files held to the table

LARGE = AGENT_BENCH / "large"
TASKS_DIR = LARGE / "tasks"

#: Where a module row's file is looked up: this test tree, and the scripts' tests, where
#: ``test_agent_task_bench_large.py`` lives.
MODULE_DIRS = (Path(__file__).parent, REPO_ROOT / "scripts" / "tests")


def _spelled(where: str) -> str:
    """The text a row's names are spelled in: a task with its expected state, or a module."""
    if where.endswith(".py"):
        (module,) = [directory / where for directory in MODULE_DIRS if (directory / where).is_file()]
        return module.read_text(encoding="utf-8")
    texts = [(TASKS_DIR / f"{where}.json").read_text(encoding="utf-8")]
    expected = LARGE / "expected" / where
    texts += [path.read_text(encoding="utf-8") for path in sorted(expected.rglob("*"), key=lambda p: p.as_posix())
              if path.is_file()]  # fmt: skip
    return "\n".join(texts)


@pytest.mark.parametrize("role", ROLES, ids=[_id(role) for role in ROLES])
def test_each_name_of_the_table_is_spelled_where_the_row_says(role: Role) -> None:
    text = _spelled(role.where)
    assert [name for name in _strings(role.name) if not _spelled_as_a_token(name, text)] == []


def test_every_task_of_the_set_has_a_row() -> None:
    assert {role.where for role in ROLES if not role.where.endswith(".py")} == {
        path.stem for path in TASKS_DIR.glob("*.json")
    }


def test_the_bench_is_the_one_the_tasks_name() -> None:
    """No file of the set spells a name of the in-repo generator's scheme, the added names apart."""
    exempt = _added_names()
    old = sorted(
        f"{path.relative_to(LARGE).as_posix()}: {token}"
        for path in LARGE.rglob("*")
        if path.is_file()
        for token in set(OLD_NAMES.findall(path.read_text(encoding="utf-8")))
        if token not in exempt
    )
    assert old == []
