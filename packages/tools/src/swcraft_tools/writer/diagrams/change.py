"""The change report's overviews: two revisions of the model drawn before and after, the change marked.

:func:`change_overviews` is what ``swcraft change-report`` draws; it returns texts and writes nothing. A
composition is shown when it is added or removed, when the caller's differ reports it changed, when its overview's
member set differs, or when a bundle differs in its set of connector names. Two names equal but for case never
share a file.

The marks: the before picture is the old overview, unmarked. The after picture is the new overview
plus the old members it lacks, marked ``removed``; a new member is ``added``. Bundles are merged per (source
member, target member) pair by connector name; a line is ``added``, ``removed`` or ``both`` by what its names gained
and lost. A removed composition's after picture is the old overview marked removed whole. The frame is never marked.

What this does not reach: a connector the overview does not draw (a delegation, a connector not
drawn) moves no picture, so such a change shows only when the caller names the composition, and
unmarked. A name declared twice is read at its first declaration.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from swcraft_tools.writer.diagrams import graph, layout, refuse_xml_illegal_text, svg

__all__ = ["ChangeOverview", "change_overviews", "shown_compositions"]

_MEMBER_PREFIX = "member."


@dataclass(frozen=True)
class ChangeOverview:
    """One shown composition: its file stem and its two overview SVG texts (``before_svg`` ``None`` when added)."""

    composition: str
    stem: str
    before_svg: str | None
    after_svg: str


def _overviews(graphs: graph.DiagramGraphs) -> dict[str, dict[str, Any]]:
    """Composition name -> its overview graph; the first declaration of a name wins."""
    overviews: dict[str, dict[str, Any]] = {}
    for item in graphs.compositions:
        overviews.setdefault(item.name, item.overview)
    return overviews


def _frame(overview: Mapping[str, Any]) -> dict[str, Any]:
    return overview["children"][0]


def _member_names(overview: Mapping[str, Any] | None) -> list[str]:
    if overview is None:
        return []
    return [str(child[graph.METADATA_KEY]["name"]) for child in _frame(overview).get("children", [])]


def _pair(edge: Mapping[str, Any]) -> tuple[str, str]:
    return str(edge["sources"][0]), str(edge["targets"][0])


def _bundles(overview: Mapping[str, Any] | None) -> dict[tuple[str, str], list[str]]:
    """(source member id, target member id) -> the connector names its bundle lists, in its order."""
    if overview is None:
        return {}
    return {
        _pair(edge): [str(row["name"]) for row in edge[graph.METADATA_KEY].get("connectors", [])]
        for edge in _frame(overview).get("edges", [])
    }


def _holds_a_change(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> bool:
    if (before is None) != (after is None):
        return True
    if set(_member_names(before)) != set(_member_names(after)):
        return True
    old, new = _bundles(before), _bundles(after)
    return old.keys() != new.keys() or any(set(old[pair]) != set(new[pair]) for pair in old)


def _shown(
    old: Mapping[str, dict[str, Any]], new: Mapping[str, dict[str, Any]], changed_compositions: Iterable[str]
) -> tuple[str, ...]:
    named = set(changed_compositions)
    names = set(old) | set(new)
    shown = [name for name in names if name in named or _holds_a_change(old.get(name), new.get(name))]
    return tuple(sorted(shown, key=graph.natural_key))


def shown_compositions(old_root: str, new_root: str, changed_compositions: Iterable[str]) -> tuple[str, ...]:
    """The compositions holding a change, in ``graph.natural_key`` order; needs no Node.

    *changed_compositions* are the names the differ reports in the compositions layer as added,
    removed or changed; a name neither revision declares is ignored.
    """
    old = _overviews(graph.build_graphs(old_root))
    new = _overviews(graph.build_graphs(new_root))
    return _shown(old, new, changed_compositions)


def _mark(element: dict[str, Any], change: str) -> None:
    element[graph.METADATA_KEY][svg.CHANGE_KEY] = change


def merged_overview(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> dict[str, Any]:
    """The after picture: *after* plus what *before* held and it lacks, marked.

    Both are unlaid overview graphs and neither is changed; one of the two may be ``None``, never both.
    """
    if after is not None:
        merged = copy.deepcopy(dict(after))
    elif before is not None:
        merged = copy.deepcopy(dict(before))
        _frame(merged)["children"], _frame(merged)["edges"] = [], []
    else:
        raise ValueError("a merged overview needs at least one side")
    frame = _frame(merged)
    frame.setdefault("children", [])
    frame.setdefault("edges", [])
    before_members = set(_member_names(before))
    after_members = set(_member_names(after))
    for child in frame["children"]:
        if child[graph.METADATA_KEY]["name"] not in before_members:
            _mark(child, "added")
    before_frame = _frame(before) if before is not None else {}
    for child in before_frame.get("children", []):
        if child[graph.METADATA_KEY]["name"] not in after_members:
            removed = copy.deepcopy(child)
            _mark(removed, "removed")
            frame["children"].append(removed)
    before_edges = {_pair(edge): edge for edge in before_frame.get("edges", [])}
    after_pairs = set()
    for edge in frame["edges"]:
        pair = _pair(edge)
        after_pairs.add(pair)
        meta = edge[graph.METADATA_KEY]
        names = {str(row["name"]) for row in meta.get("connectors", [])}
        old_edge = before_edges.get(pair)
        old_rows = old_edge[graph.METADATA_KEY].get("connectors", []) if old_edge is not None else []
        removed_rows = [copy.deepcopy(row) for row in old_rows if str(row["name"]) not in names]
        added = bool(names - {str(row["name"]) for row in old_rows})
        if removed_rows:
            meta["connectors"] = [*meta.get("connectors", []), *removed_rows]
        if added and removed_rows:
            _mark(edge, "both")
        elif added:
            _mark(edge, "added")
        elif removed_rows:
            _mark(edge, "removed")
    for pair, old_edge in before_edges.items():
        if pair not in after_pairs:
            removed_edge = copy.deepcopy(old_edge)
            _mark(removed_edge, "removed")
            frame["edges"].append(removed_edge)
    return merged


def change_overviews(old_root: str, new_root: str, changed_compositions: Iterable[str]) -> tuple[ChangeOverview, ...]:
    """Every shown composition's before and merged after overview, laid out once and rendered.

    Raises ``DiagramLayoutError`` when the layout cannot be computed and ``DiagramGenerationError``
    when a model text holds a character no XML document holds; an empty selection starts no process.
    """
    old = _overviews(graph.build_graphs(old_root))
    new = _overviews(graph.build_graphs(new_root))
    shown = _shown(old, new, changed_compositions)
    union = sorted(set(old) | set(new))
    stems = dict(zip(union, graph.file_stems(union), strict=True))
    graphs: list[dict[str, Any]] = []
    for name in shown:
        before = old.get(name)
        if before is not None:
            graphs.append(before)
        graphs.append(merged_overview(before, new.get(name)))
    answers = iter(layout.layout(graphs))
    overviews = []
    for name in shown:
        before_svg = svg.render_overview(next(answers)) if name in old else None
        after_svg = svg.render_overview(next(answers))
        overviews.append(ChangeOverview(name, stems[name], before_svg, after_svg))
    refuse_xml_illegal_text(
        {
            f"{overview.stem}-{side}.svg": text
            for overview in overviews
            for side, text in (("before", overview.before_svg), ("after", overview.after_svg))
            if text is not None
        }
    )
    return tuple(overviews)
