"""Measure what the MCP surface hands an LLM on one project: bytes, ~tokens and seconds per tool.

It is the oracle the read-budget acceptance compares against. Run it on an imported project::

    swcraft init /tmp/scale --name scale
    swcraft import model --source <export>.arxml --project /tmp/scale/swcraft.yaml
    .venv/bin/python scripts/measure_mcp_scale.py /tmp/scale/swcraft.yaml real
    .venv/bin/python scripts/measure_mcp_scale.py /tmp/scale/swcraft.yaml real --replay

Two modes, and a table produced by this script must always name which one produced it:

* the **sweep** (the default) — one call per read-only tool, ``discover`` in each of its forms,
  plus **every layer in every read form**. A table's rows must name the form, since the same layer
  read measures a different question in each;
* ``--replay`` — the demo film's two work-instruction prompts as the tool sequence they drive, with
  every refusal's hint chain **walked** as an agent walks it, then the exit gate's four questions
  and the two former dead ends. It runs under ``SWCRAFT_REVIEW=stage-only``, so nothing is committed
  and the model stays byte-identical.

What it measures: the wire size of each tool's result (its text blocks, each JSON-encoded as the
message carries it, UTF-8 bytes) through an in-memory FastMCP client, the object a host talks to.
``~tokens`` is bytes / 4, an estimate. The layers are the registry's, in the contract's order; the
"heaviest" elements are counted off the loaded model. A project the loader refuses is refused
before the first tool call.

What it does not measure: ``model_diff`` (needs two git revisions) and ``build``/``validate``/the
component exchange (write artifacts). A **refused** response was never sent, so its row carries
the ``measured.wire_bytes`` the refusal states: ``read.measure``, the same rule and unit as every
counted row.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from swcraft_tools.editing.read import HINT_PAGE_SIZE, NARROWING_ORDER, escaped_bytes
from swcraft_tools.model_registry import current_registry
from swcraft_tools.model_state import ModelState, load_model_state

#: The read forms in **narrowing** order, imported so a re-ordering of the server's chain moves it
#: too; not ``discover``'s order (default first), since ``full`` belongs left of what replaced it.
FORMS: tuple[str, ...] = NARROWING_ORDER

#: The ``discover`` forms, widest first, as :data:`FORMS`. Stated, and held to the protocol's own
#: set by a test, so a form the protocol adds fails until it is given its place.
DISCOVER_FORM_ORDER: tuple[str, ...] = ("full", "summary", "baseline")


def port_counts(state: ModelState) -> tuple[Counter[str], Counter[str]]:
    """Ports per interface and ports per SWC, counted off the ``swc`` layer's typed elements.

    A port whose ``interface`` is ``None`` (an external stub's) counts for its SWC and for no
    interface, else ``heaviest`` could answer ``None`` on an import with many stub ports.
    """
    by_interface: Counter[str] = Counter()
    by_swc: Counter[str] = Counter()
    for element in state.layers["swc"]:
        by_swc[element.name] = len(element.ports)
        for port in element.ports:
            if port.interface is not None:
                by_interface[port.interface] += 1
    return by_interface, by_swc


def heaviest(state: ModelState) -> tuple[tuple[str, int], tuple[str, int]]:
    """The interface named by the most ports and the SWC with the most ports, each with its count.

    Raises ``ValueError`` naming the reason when the model has no SWC or no port naming an interface.
    """
    by_interface, by_swc = port_counts(state)
    if not by_swc:
        raise ValueError("the model declares no SWC; nothing to measure")
    if not by_interface:
        raise ValueError("no port of the model names an interface; nothing to measure")
    return by_interface.most_common(1)[0], by_swc.most_common(1)[0]


def hubs(state: ModelState, count: int = 2) -> list[tuple[str, int]]:
    """The *count* SWCs with the most ports, largest first.

    Chosen by counting, not by name; ties break on the name so two runs report the same hubs.
    """
    _, by_swc = port_counts(state)
    ranked = sorted(by_swc.items(), key=lambda row: (-row[1], row[0]))
    return ranked[:count]


def wire_bytes(result: object) -> int:
    """The bytes a host receives for one tool result, its one copy counted as sent.

    The message carries each text block as an escaped JSON string, so each is counted escaped; raw
    text would under-count every quote and newline. Raises ``ValueError`` on a result with a
    structured half: the server would then send two copies and the unit no longer describe the wire.
    """
    if getattr(result, "structured_content", None) is not None:
        raise ValueError("a tool result carries a structured half: the server sends two copies again")
    texts = [b.text for b in result.content if getattr(b, "type", None) == "text"]  # type: ignore[attr-defined]
    return sum(escaped_bytes(t) for t in texts)


def body_of(result: object) -> dict[str, Any] | None:
    """The body of one tool result as a mapping, or ``None`` when it is not one.

    The first text block parsed, when it is a JSON object; a plain-text tool's report is ``None``.
    """
    text = text_of(result)
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def text_of(result: object) -> str:
    """The first text block of one tool result, or ``""``."""
    for block in getattr(result, "content", None) or ():
        if getattr(block, "type", None) == "text":
            return str(block.text)
    return ""


def refusal_of(result: object) -> dict[str, Any] | None:
    """The refusal *result* carries, or ``None`` when it answered.

    Both refusal shapes: the protocol's ``errorResult`` (``read``), and the ``ERROR: ...`` line a
    plain-text tool answers with (``impact_of``, which has no envelope).
    """
    body = body_of(result)
    if body is not None:
        return body if body.get("response") == "error" else None
    text = text_of(result)
    if text.startswith("ERROR:"):
        return {"error": TEXT_REFUSAL, "message": text}
    return None


#: This script's own key for a plain-text tool's refusal, a sentinel and not a protocol error code;
#: named because every site that classifies a row reads it back.
TEXT_REFUSAL = "text-refusal"

#: The sentence ``server._narrower_impact_request`` names the next request in. ``impact_of``'s
#: refusal has no ``hints`` array, so the exit gate reads the sentence. The request is parsed out of
#: it rather than keyed per sentence, so a step the server adds is read too; a ``null`` clause is a
#: key to drop. The chain ends at ``form="count"``, which is never refused for size.
#:
#: Not reached: a value carrying a comma is split into two clauses, and a sentence not spelt as
#: ``key=value`` clauses is read as none -- a wrong hint rather than a loud one.
TEXT_ASK = re.compile(r"Ask for it with ([^.]+)\.")


def text_hint(message: str) -> dict[str, Any] | None:
    """The request keys *message* names, or ``None`` when it names none.

    ``Ask for it with depth=1, after=null.`` becomes ``{"depth": 1, "after": None}``
    -- only the keys to add to the refused request; the caller merges (:meth:`Session.walk`). A
    value is an ``int``, ``None`` for ``null``, else its text, quoted or bare, so a bare word the
    server adds degrades instead of raising.
    """
    match = TEXT_ASK.search(message)
    if match is None:
        return None
    hint: dict[str, Any] = {}
    for clause in match.group(1).split(","):
        key, _, value = clause.strip().partition("=")
        hint[key] = _hint_value(value)
    return hint


def _hint_value(text: str) -> Any:
    """One hint clause's value: a quoted string, ``null`` as ``None``, an ``int``, else its own text."""
    if text.startswith('"'):
        return text.strip('"')
    if text == "null":
        return None
    try:
        return int(text)
    except ValueError:
        return text


def hints_of(result: object) -> tuple[dict[str, Any], ...] | None:
    """The next requests *result*'s **budget** refusal offers, in chain order.

    Each item is a request document (a ``readRequest``, a ``discoverRequest``, or ``impact_of``'s
    sentence read by :func:`text_hint`). ``None`` when *result* answered or the refusal is not a
    budget refusal; ``()`` is an **exhausted** chain.
    """
    refusal = refusal_of(result)
    if refusal is None:
        return None
    if refusal["error"] == TEXT_REFUSAL:
        message = refusal["message"]
        hint = text_hint(message)
        return None if hint is None else (hint,)
    # A non-budget refusal carries no `hints` either; `()` for it would count as an exhausted chain.
    if refusal.get("error") != "over-budget":
        return None
    return tuple(refusal.get("hints") or ())


def hint_label(hint: dict[str, Any]) -> str:
    """One chain cell: what makes this hint narrower than the request it answers.

    Its form, then its page and depth keys (``summary+limit=50``); a ``discoverRequest`` by its scope.
    """
    if hint.get("operation") == "discover":
        return f"discover {hint.get('layer') or 'project'}:{hint.get('form', '?')}"
    parts = [str(hint["form"])] if hint.get("form") is not None else []
    parts += [f"{key}={hint[key]}" for key in ("depth", "limit", "after") if hint.get(key) is not None]
    return "+".join(parts) if parts else "?"


def chain_of(result: object) -> tuple[str, ...] | None:
    """:func:`hints_of`, labelled — what a table cell and the exit gate read."""
    hints = hints_of(result)
    return None if hints is None else tuple(hint_label(hint) for hint in hints)


def outcome_of(result: object) -> str:
    """One cell: ``ok``, or the refusal with its chain in the order it offers it.

    :func:`chain_of`'s ``None`` (never on a chain) and ``()`` (exhausted) are told apart, never by a
    bare falsiness test, so a cell agrees with the exit gate's count.
    """
    refusal = refusal_of(result)
    if refusal is None:
        return "ok"
    chain = chain_of(result)
    if refusal["error"] == TEXT_REFUSAL:
        if chain is None:
            return f"refused (text), not a budget refusal: {refusal['message'][:60]}"
        return f"refused (text), next request [{', '.join(chain)}]"
    measured = refusal.get("measured") or {}
    size = f", would be {measured['wire_bytes']:,} B" if "wire_bytes" in measured else ""
    if chain is None:
        return f"refused `{refusal['error']}`{size}, not a budget refusal"
    if not chain:
        return f"refused `{refusal['error']}`{size}, hints [] — chain exhausted"
    return f"refused `{refusal['error']}`{size}, hints [{', '.join(chain)}]"


class Session:
    """One in-memory client, and the row every call adds to the report."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.rows: list[dict[str, Any]] = []

    async def call(self, tool: str, section: str = "", **args: object) -> Any:
        started = time.perf_counter()
        result = await self.client.call_tool(tool, args, raise_on_error=False)
        elapsed = time.perf_counter() - started
        self.rows.append(
            {
                "section": section,
                "tool": tool,
                "args": json.dumps(args, ensure_ascii=False),
                "bytes": wire_bytes(result),
                "s": elapsed,
                "outcome": outcome_of(result),
                "refused": refusal_of(result) is not None,
                "chain": chain_of(result),
            }
        )
        return result

    async def walk(self, tool: str, section: str, **args: Any) -> Any:
        """Call *tool*, then **walk** its refusal's chain until one answers or it ends.

        The agent's own loop: a chain that ends refused with empty ``hints`` is a recorded outcome,
        not a failure. A ``readRequest`` or ``discoverRequest`` hint is a complete request and
        replaces the arguments; ``impact_of``'s hint names only the keys that change, so it is merged
        and a ``null`` key dropped. The walk terminates: every step is narrower than the last.
        """
        result = await self.call(tool, section, **args)
        while hints := hints_of(result):
            hint = dict(hints[0])
            if "operation" in hint:
                tool = "read" if hint.pop("operation") == "read" else "discover"
                args = hint
            else:
                args = {key: value for key, value in {**args, **hint}.items() if value is not None}
            result = await self.call(tool, section, **args)
        return result

    async def read_chain(self, address: dict[str, Any], section: str) -> Any:
        """:meth:`walk` at a read address, in the form the address chooses."""
        return await self.walk("read", section, address=address)

    def section(self, name: str) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["section"] == name]


def print_calls(rows: list[dict[str, Any]], heading: str) -> None:
    print(f"\n### {heading}\n")
    print("| tool | args | bytes | ~tokens | s | outcome |")
    print("|---|---|---:|---:|---:|---|")
    for row in rows:
        print(
            f"| {row['tool']} | `{row['args']}` | {row['bytes']:,} | {row['bytes'] // 4:,} "
            f"| {row['s']:.2f} | {row['outcome']} |"
        )


async def sweep(session: Session, layers: list[str], iface: str, swc: str) -> None:
    """One call per read-only tool, and then every layer in every form."""
    await session.call("discover", "tools")
    for layer in layers:
        await session.call("read", "tools", address={"layer": layer})
    await session.call("read", "tools", address={"layer": "swc", "element": swc})
    await session.call("read", "tools", address={"layer": "interfaces", "element": iface})
    await session.call("impact_of", "tools", name=iface, form="count")
    await session.call("impact_of", "tools", name=iface)
    await session.call("impact_of", "tools", name=swc)
    await session.call("resource_report", "tools")
    print_calls(session.section("tools"), "One call per read-only tool, each at its DEFAULT form")

    # `discover` is measured apart: its size is the contract's, the same on any model.
    for form in DISCOVER_FORM_ORDER:
        await session.call("discover", "discover", form=form)
    print_calls(
        session.section("discover"),
        "`discover` in both forms — the response the CONTRACT sizes, not the model",
    )

    print("\n### The three forms per layer\n")
    print("| layer | n | " + " | ".join(FORMS) + " |")
    print("|---|---:|" + "---:|" * len(FORMS))
    for layer in layers:
        cells: list[str] = []
        count = "?"
        for form in FORMS:
            result = await session.call("read", "forms", address={"layer": layer}, form=form)
            refusal = refusal_of(result)
            if refusal is not None:
                measured = refusal.get("measured") or {}
                cells.append(f"**{measured.get('wire_bytes', 0):,}** x")
                if count == "?" and "elements" in measured:
                    count = f"{measured['elements']:,}"
                continue
            cells.append(f"{wire_bytes(result):,}")
            value = (body_of(result) or {}).get("value")
            if form == "index" and isinstance(value, dict):
                count = f"{value.get('count', 0):,}"
            elif count == "?" and isinstance(value, list):
                count = f"{len(value):,}"
        print(f"| {layer} | {count} | " + " | ".join(cells) + " |")
    print(
        "\n`x` marks a **refused** form: the figure is the refusal's own `measured.wire_bytes` — "
        "the engine's own measurement of the response it did not send, in the same unit and by the "
        "same rule as every other figure here (`read.measure`). Every other figure is counted at "
        "the client, off the one copy the server actually sent."
    )


#: The exit gate's third question, "every task whose ``period`` is below 10 ms", which the protocol
#: deliberately cannot express: a paged projection plus a client-side comparison, since one
#: operator is a vocabulary and three are a query language. ``Task.period`` is float seconds.
PERIOD_THRESHOLD_S = 0.01

#: The section the four questions are printed in, read back by the exit gate.
QUESTIONS = "the four questions"


def carried(result: object) -> str:
    """What one answer carried beside its bytes: its page block, or a report's scope footer.

    The exit gate asks for a count and a truncation flag, which a byte figure states neither. A read
    publishes them in ``page``, a paged ``impact_of`` in its scope footer, its ``count`` as totals
    per matched layer; all are read off the answer, never recomputed.
    """
    body = body_of(result)
    if body is None:
        text = text_of(result)
        if not text.strip():
            return ""
        last = text.rstrip().splitlines()[-1]
        # No scope footer is an unpaged answer; saying so keeps the bullet from reading as missing.
        return last if last.startswith("(") else "answered whole (no page, no bound)"
    page = body.get("page")
    if page is not None:
        return json.dumps(page, ensure_ascii=False)
    if body.get("response") == "impact":
        totals = ", ".join(f"{layer} {figures['total']}" for layer, figures in body.get("matched", {}).items())
        return f"counted: {totals or 'no match'}"
    value = body.get("value")
    return f"{len(value)} row(s), unpaged" if isinstance(value, list) else ""


async def four_questions(session: Session, iface: str, swc: str) -> dict[str, bool]:
    """The exit gate's four questions, one request each, beside the two former dead ends.

    Every call goes through :meth:`Session.walk`, so the refusals on the way to an answer are rows
    too: what is measured is what an agent pays.

    Returns:
        One entry per named walk: whether that walk **ended** answered (the rows cannot tell a
        refusal on the way from one at a chain's end).
    """
    # 1. Every port using interface A, with its owning SWC: the member slice.
    ports = await session.walk(
        "read",
        QUESTIONS,
        address={"layer": "swc"},
        members="ports",
        where=[{"field": "interface", "equals": iface}],
    )
    # 2. Every interface the widest SWC depends on: the forward direction.
    depends = await session.walk("impact_of", QUESTIONS, name=swc, direction="dependencies")
    # 3. The `period` projection; the comparison is the caller's.
    tasks = await session.walk(
        "read",
        QUESTIONS,
        address={"layer": "tasks"},
        where=[{"field": "period", "exists": True}],
        limit=HINT_PAGE_SIZE,
    )
    # 4. The bounded transitive dependents of the hub interface, with the count and truncation flag.
    bounded = await session.walk("impact_of", QUESTIONS, name=iface, depth=1, limit=HINT_PAGE_SIZE)
    # The two former dead ends, walked from the request that dead-ended: the widest layer, and
    # the hub interface's default report.
    connectors = await session.read_chain({"layer": "connectors"}, QUESTIONS)
    hub_report = await session.walk("impact_of", QUESTIONS, name=iface)

    print_calls(
        session.section(QUESTIONS),
        "The exit gate's four questions, each walked to an answer — plus the two known dead ends",
    )

    # What each answer carried: the byte column states no count or truncation flag.
    rows = (body_of(tasks) or {}).get("value") or []
    under = [row for row in rows if isinstance(row, dict) and (row.get("period") or 0) <= PERIOD_THRESHOLD_S]
    print("")
    print(f"* Q1 — every port on `{iface}`, with its owner: {carried(ports)}")
    print(f"* Q2 — what `{swc}` depends on: {carried(depends)}")
    print(
        f"* Q3 — the `period` projection: {carried(tasks)}, of which {len(under)} of {len(rows)} task(s) "
        f"have `period` at most {PERIOD_THRESHOLD_S} s. The server ships no comparison operator — "
        "the projection is bounded and the comparison is the caller's"
    )
    print(f"* Q4 — the bounded dependents of `{iface}`: {carried(bounded)}")
    print(f"* dead end (a), the `connectors` layer: {carried(connectors)}")
    print(f"* dead end (b), `impact_of {iface}`: {carried(hub_report)}")

    return {
        "Q1 the member slice": refusal_of(ports) is None,
        "Q2 the forward direction": refusal_of(depends) is None,
        "Q3 the period projection": refusal_of(tasks) is None,
        "Q4 the bounded dependents": refusal_of(bounded) is None,
        "20.1 dead end (a) connectors": refusal_of(connectors) is None,
        "20.1 dead end (b) the hub report": refusal_of(hub_report) is None,
    }


async def replay_film(session: Session, state: ModelState, iface: str) -> None:
    """The demo film's two work-instruction prompts, as the tool calls they drive."""
    hub_rows = hubs(state, 2)
    hub = hub_rows[0][0]

    # Prompt 1 -- "add a provide port on <iface> to <hub>". `discover` is walked: it can be refused.
    discovered = await session.walk("discover", "prompt 1")
    body = body_of(discovered) or {}
    baseline = body.get("baseline", "")
    # The budget comes from the server, never a constant restated here: `discover`'s `reads` block,
    # or a refusal's `measured.budget` when `discover` itself was refused.
    budget = int((body.get("reads") or {}).get("budget_wire_bytes", 0)) or int(
        (body.get("measured") or {}).get("budget", 0)
    )
    await session.read_chain({"layer": "swc"}, "prompt 1")
    await session.read_chain({"layer": "swc", "element": hub}, "prompt 1")
    await session.read_chain({"layer": "interfaces", "element": iface}, "prompt 1")
    port = "pScaleProbe"
    await session.call(
        "change_set",
        "prompt 1",
        baseline=baseline,
        operations=[
            {
                "operation": "create",
                "address": {"layer": "swc", "element": hub, "path": [{"field": "ports"}, {"member": port}]},
                "value": {"name": port, "direction": "provide", "interface": iface},
            }
        ],
        intent=f"acceptance replay: one provide port on {iface}",
    )

    # Prompt 2 -- "what does that affect, and are we within budget?".
    await session.call("impact_of", "prompt 2", name=iface)
    await session.call("impact_of", "prompt 2", name=hub)
    await session.call("resource_report", "prompt 2")

    for name in ("prompt 1", "prompt 2"):
        rows = session.section(name)
        total = sum(row["bytes"] for row in rows)
        print_calls(rows, f"{name} — {len(rows)} calls, {total:,} wire bytes of tool output in total")

    # The hubs in every form, the collection address and the `impact_of` widths the exit gate names.
    for name, _ports in hub_rows:
        for form in FORMS:
            await session.call("read", "hubs", address={"layer": "swc", "element": name}, form=form)
        # The ports collection is walked: a large hub's default summary can itself be over the budget.
        await session.read_chain({"layer": "swc", "element": name, "path": [{"field": "ports"}]}, "hubs")
        for form in ("full", "summary"):
            await session.call("impact_of", "hubs", name=name, form=form)
    for form in ("full", "summary", "count"):
        await session.call("impact_of", "hubs", name=iface, form=form)
    print_calls(
        session.section("hubs"),
        "The hubs in every form — " + ", ".join(f"{name} ({ports} ports)" for name, ports in hub_rows),
    )

    outcomes = await four_questions(session, iface, hub)

    print("\n### The exit gate\n")
    admitted = [row for row in session.rows if not row["refused"]]
    refused = [row for row in session.rows if row["refused"]]
    # An exhausted chain is read off the refusal's own hints, never off the printed cell's wording.
    exhausted = [row for row in refused if row["chain"] == ()]
    over = [row for row in admitted if budget and row["bytes"] > budget]
    print(f"* calls: {len(session.rows)} — {len(admitted)} answered, {len(refused)} refused")
    print(f"* budget, as `discover` publishes it: {budget:,} wire bytes")
    print(f"* largest answered response: {max((row['bytes'] for row in admitted), default=0):,} wire bytes")
    print(f"* answered responses ABOVE the budget: {len(over)} — the gate's first clause")
    print(
        f"* refusals: {len(refused)}, of which {len(exhausted)} end an EXHAUSTED chain "
        "(a recorded outcome, not a failure — an element or member whose own published value "
        "exceeds the budget still has nothing to window, per the plan's *What this plan does not reach*)"
    )
    for row in refused:
        print(f"  * `{row['tool']}` `{row['args']}` — {row['outcome']}")

    # How each walk ended comes from `four_questions`: the rows cannot tell a step from an end.
    questions = session.section(QUESTIONS)
    unanswered = sorted(name for name, answered in outcomes.items() if not answered)
    print(
        f"* the exit gate's four questions and the two known dead ends: {len(questions)} calls, "
        f"{sum(1 for row in questions if not row['refused'])} answered, "
        f"{sum(1 for row in questions if row['refused'])} refused on the way, "
        f"largest answered {max((row['bytes'] for row in questions if not row['refused']), default=0):,} wire bytes"
    )
    print(
        f"* walks that ended ANSWERED: {len(outcomes) - len(unanswered)} of {len(outcomes)}"
        + (f" — still refused at the end of the chain: {', '.join(unanswered)}" if unanswered else "")
    )


async def run(manifest: Path, label: str, replay: bool) -> None:
    os.environ["SWCRAFT_MANIFEST"] = str(manifest)
    # Set rather than assumed: an exported `elicit` would turn the measurement into a question nobody answers.
    os.environ["SWCRAFT_REVIEW"] = "stage-only"
    from fastmcp import Client
    from swcraft_mcp import server

    # The server reads the mode once at start; this makes it take effect on an already imported module.
    server._configure_review_mode()

    root = manifest.parent / "model"
    # The layers are resolved at the call, never in a default expression; the loader refuses an
    # unreadable project before the client is built.
    registry = current_registry()
    layers = [layer.name for layer in registry.layers]
    state = load_model_state(root, registry=registry)
    (iface, iface_ports), (swc, swc_ports) = heaviest(state)

    # Every heading names its mode, so two tables are never compared across modes.
    print(
        f"\n## {label} ({'film' if replay else 'sweep'}) — heaviest interface {iface} ({iface_ports} ports), "
        f"largest SWC {swc} ({swc_ports} ports)"
    )
    async with Client(server.mcp) as client:
        session = Session(client)
        if replay:
            await replay_film(session, state, iface)
        else:
            await sweep(session, layers, iface, swc)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="measure_mcp_scale.py", description=__doc__.splitlines()[0])
    parser.add_argument("manifest", type=Path, help="the project's swcraft.yaml")
    parser.add_argument("label", help="what to call this project in the printed heading")
    parser.add_argument(
        "--replay",
        action="store_true",
        help="replay the demo film's two work-instruction prompts instead of sweeping the tools",
    )
    args = parser.parse_args(argv[1:])
    asyncio.run(run(args.manifest.resolve(), args.label, args.replay))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
