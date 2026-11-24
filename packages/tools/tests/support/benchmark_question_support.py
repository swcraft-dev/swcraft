"""The agent task benchmark's questions 11 to 22, each as the one ``read`` or ``find`` that answers it.

Shared by ``test_benchmark_questions_in_one_read.py`` (the small bench, the fast gate) and
``test_benchmark_questions_large.py`` (the large bench, ``slow``): one request per question,
parameterised by the bench's own names, so the two modules ask the same question of two
models and only the names and the expected answers differ. It also holds questions 18 to 20
(:func:`requirement_requests`), question 21 as one ``find`` by ``text`` (:func:`found_by_text`),
question 22 (:func:`candidates_for`) and the comparison both benches are held to: the
requirements layer's computed values against the trace rules' findings (:func:`trace_agreement`).

What this does not reach: whether an agent asks these reads -- the profile carries generic
recipes and never these requests, so that the benchmark measures the agent rather than
teaching it.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from swcraft_tools.diagnostics import MEMBER
from swcraft_tools.editing.engine import handle_request
from swcraft_tools.editing.read import measure
from swcraft_tools.model_state import load_model_state
from swcraft_tools.validator import check_requirement_traceability, derive_rows


def join_to(layer: str, name: str, left: str) -> list[dict[str, Any]]:
    """An inner join keeping the rows whose *left* path names the element *name* of *layer*."""
    return [
        {
            "with": {"address": {"layer": layer}, "where": [{"field": "name", "equals": name}]},
            "on": [{"left": left, "right": "element"}],
            "as": "kept",
        }
    ]


def question_requests(*, component: str, interface: str, partition: str, core: str) -> dict[str, dict[str, Any]]:
    """The seven reads, keyed by the task id's number, for a bench's own names.

    Question 14 ("how many components are ASIL B") is one ``count`` over a
    clause on the computed ``rating``; 11 and 13 answer *which* as well, so they
    stay rows, and their ``count`` is asked beside them (:func:`counted`).
    """
    return {
        "11": {
            "address": {"layer": "swc", "element": component, "path": [{"field": "ports"}]},
            "form": "index",
        },
        "12": {
            "address": {"layer": "swc"},
            "members": "ports",
            "form": "index",
            "where": [{"field": "interface", "equals": interface}],
        },
        "13": {"address": {"layer": "swc"}, "members": "ports", "form": "index", "derived": ["unconnected"]},
        "14": {
            "address": {"layer": "swc"},
            "form": "count",
            "derived": ["effective_level"],
            "where": [{"field": "derived.effective_level.rating", "equals": "B"}],
        },
        "15": {
            "address": {"layer": "swc"},
            "form": "index",
            "derived": ["partitions"],
            "join": join_to("hardware", partition, "derived.partitions.partition"),
        },
        "16": {
            "address": {"layer": "tasks"},
            "form": "index",
            "derived": ["core"],
            "join": join_to("hardware", core, "derived.core.core"),
        },
        "17": {"address": {"layer": "swc"}, "members": "memory", "form": "summary"},
    }


def requirement_requests(*, requirement: str) -> dict[str, dict[str, Any]]:
    """Questions 18 to 20, keyed by the task id's number, for a bench's own requirement.

    18 ("which elements satisfy R") is ``realised_by`` over the one requirement a
    ``where`` on its ``id`` selects; 19 ("which requirements does nothing
    satisfy") a clause on the computed ``allocated``; 20 ("which links need
    confirmation") ``stale_links`` over the layer, which has a row only for a
    link that needs one.
    """
    layer = {"layer": "requirements"}
    return {
        "18": {
            "address": layer,
            "form": "index",
            "derived": ["realised_by"],
            "where": [{"field": "id", "equals": requirement}],
        },
        "19": {
            "address": layer,
            "form": "index",
            "derived": ["allocation"],
            "where": [{"field": "derived.allocation.allocated", "equals": False}],
        },
        "20": {"address": layer, "form": "index", "derived": ["stale_links"]},
    }


def ask(request: dict[str, Any], project: Path) -> tuple[Any, int]:
    """The read's value -- a ``count`` read's number -- and its wire size; one read, never a page of one."""
    answered = handle_request({"operation": "read", **request}, project=project)
    assert "page" not in answered, "one read answers the whole question, not its first page"
    if request.get("form") == "count":
        assert answered["response"] == "count", answered
        return answered["count"], measure(answered)
    assert answered["response"] == "read", answered
    return answered["value"], measure(answered)


def found_by_text(text: str, project: Path) -> tuple[list[str], int]:
    """Question 21's one ``find`` by *text*: the requirement ids answered, in order, and the wire size.

    The answer must be whole -- no ``more`` -- for the ids to be the question's answer.
    """
    answered = handle_request({"operation": "find", "text": text}, project=project)
    assert answered["response"] == "find", answered
    assert "more" not in answered, "one find answers the whole question"
    candidates = answered["candidates"]
    ids = [] if candidates == "none" else [candidate["address"]["element"] for candidate in candidates]
    return ids, measure(answered)


def candidates_for(requirement: str, project: Path) -> tuple[dict[str, list[str]], int]:
    """Question 22's one ``find`` for *requirement*: each place's group name -> its shared terms, and the wire size.

    The places in the answer's order (a ``dict`` keeps it), at the default three; the terms are the
    group's ``text`` evidence record's, the terms the search says the requirement shares with it.
    """
    answered = handle_request({"operation": "find", "for_requirement": requirement}, project=project)
    assert answered["response"] == "find", answered
    places = answered["places"]
    found = {
        place["group"]["name"]: [
            term for record in place["evidence"] if record["kind"] == "text" for term in record["terms"]
        ]
        for place in ([] if places == "none" else places)
    }
    return found, measure(answered)


def counted(request: dict[str, Any], project: Path) -> int:
    """The same read at ``form: "count"``: the size of the set it selects, one number."""
    number, _ = ask({**request, "form": "count"}, project)
    return number


def with_layer(row: dict[str, Any]) -> dict[str, str]:
    """A ``realised_by`` row as task 18's ``with_layer`` entry: the name, its layer word and its owner.

    The member is the name where there is one, its component the owner; a
    hardware element's layer word is ``hardware`` whatever its kind.
    """
    site = row["derived"]["realised_by"]
    entry = {
        "name": site["member"] or site["element"],
        "layer": "hardware" if site["layer"] == "hardware" else site["kind"],
    }
    if site["member"] is not None:
        entry["owner"] = site["element"]
    return entry


def trace_agreement(model_root: Path) -> tuple[Any, Any]:
    """``(values, findings)``: what the three values and the trace rules say, in one comparable shape.

    Each side is ``(unallocated ids, stale sites)``; a stale site is ``(layer,
    element, member, kind, requirement id, stated, current)``, ``stated`` ``None``
    for a bare id. The findings' side reads ``kind``, ``stated`` and ``current``
    on its own -- the kind from the finding's path and the element, the stated
    version from the member's text, the current one from the requirement --
    never through ``trace.site_kind`` or ``validator.stale_link``, which the
    values share with the rules. ``test_validator_derived.py`` holds the small
    bench and the abstentions to it, ``test_benchmark_questions_large.py``
    (``slow``) the large bench.
    """
    state = load_model_state(model_root)
    rows = [{"element": requirement.id} for requirement in state["requirements"]]
    derived = derive_rows(rows, ["allocation"], state)
    unallocated = sorted({row["element"] for row in derived if row["derived"]["allocation"]["allocated"] is False})
    stale = Counter(
        (
            link["layer"],
            link["element"],
            link["member"],
            link["kind"],
            row["element"],
            link["stated"],
            link["current"],
        )
        for row in derive_rows(list({r["element"]: r for r in rows}.values()), ["stale_links"], state)
        for link in [row["derived"]["stale_links"]]
    )
    versions = {requirement.id: requirement.version for requirement in state["requirements"]}
    hardware = {element.name: element.kind for element in state.layers.get("hardware", ())}
    result = check_requirement_traceability(str(model_root))
    found_unallocated = sorted(
        issue.subjects[0].element for issue in result.warnings if issue.rule == "trace.requirement-unallocated"
    )
    found_stale: Counter[tuple[Any, ...]] = Counter()
    for issue in result.warnings:
        if issue.rule not in {"trace.link-needs-confirmation", "trace.link-version-not-stated"}:
            continue
        [subject] = issue.subjects
        if subject.layer == "requirements":
            continue
        members = [step.value for step in subject.path if step.kind == MEMBER]
        member = members[0] if len(members) == 2 else None
        if member is not None:
            kind = {"ports": "port", "runnables": "runnable"}[subject.path[0].value]
        elif subject.layer == "hardware":
            kind = hardware[subject.element]
        else:
            kind = subject.layer.removesuffix("s")  # interfaces -> interface; swc stays swc
        suffix = re.fullmatch(r".*@([0-9]+)", members[-1])
        stated = int(suffix.group(1)) if suffix else None
        assert (stated is None) == (issue.rule == "trace.link-version-not-stated"), issue
        found_stale[(subject.layer, subject.element, member, kind, issue.target, stated, versions[issue.target])] += 1
    return (unallocated, stale), (found_unallocated, found_stale)
