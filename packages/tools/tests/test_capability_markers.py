"""The capability markers' wire leaf, ``swcraft_tools.capability_markers``.

The leaf holds what the writer mints and the reader admits: the ``SWCRAFT`` group's
``GID`` and one ``Marker`` per ``SD``. What is pinned here is its shape -- it stays a
leaf, its two tuples keep their order, and no two markers share a ``GID`` or a value.
The values themselves are pinned byte for byte by
``test_identifier_coverage.py::TestEveryGeneratedFormIsAccountedFor::test_the_marker_values_are_the_seventeen_the_writer_owns``,
and what the writer emits from them by the component export's and the exchange
suites.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest
from swcraft_tools import capability_markers
from swcraft_tools.capability_markers import (
    ACCESS_POINTS,
    ASYNCHRONOUS_CALLS,
    COMPONENT_EXPORT_MARKERS,
    DATA_EVENTS,
    DATA_TYPES,
    EXCLUSIVE_AREAS,
    IMPLICIT_ACCESS,
    INTER_RUNNABLE_VARIABLES,
    OPERATION_EVENTS,
    OWN_MARKERS,
    SDG_GID,
    Marker,
)

MODULE_PATH = Path(capability_markers.__file__)


def _imported_modules() -> set[str]:
    """Every module the leaf imports, absolute or relative (a relative one is spelled with its dots)."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add("." * node.level + (node.module or ""))
    return modules


class TestTheLeaf:
    def test_it_imports_nothing_of_swcraft_tools(self) -> None:
        """The reader may not import the writer, so the leaf both import imports nothing of the package."""
        modules = _imported_modules()
        offenders = sorted(m for m in modules if m.split(".")[0] == "swcraft_tools" or m.startswith("."))
        assert offenders == [], f"capability_markers.py must stay a leaf module; found {offenders}"
        assert modules, "a scan that found no import at all would pass the line above too"

    def test_a_marker_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            ACCESS_POINTS.text = "something else"  # type: ignore[misc]


class TestTheRegistry:
    def test_own_markers_is_access_points_then_the_component_export_markers(self) -> None:
        assert OWN_MARKERS == (ACCESS_POINTS, *COMPONENT_EXPORT_MARKERS)

    def test_the_component_export_order_is_the_emission_order(self) -> None:
        """The order every component export's ``SWCRAFT`` group shows; a new marker is appended."""
        assert COMPONENT_EXPORT_MARKERS == (
            DATA_TYPES,
            OPERATION_EVENTS,
            INTER_RUNNABLE_VARIABLES,
            EXCLUSIVE_AREAS,
            DATA_EVENTS,
            IMPLICIT_ACCESS,
            ASYNCHRONOUS_CALLS,
        )

    def test_every_marker_of_the_module_is_registered(self) -> None:
        """A ``Marker`` defined in the leaf and left out of ``OWN_MARKERS`` would be minted and refused."""
        defined = [value for value in vars(capability_markers).values() if isinstance(value, Marker)]
        assert sorted(defined, key=lambda marker: marker.sd_gid) == sorted(OWN_MARKERS, key=lambda m: m.sd_gid)

    def test_the_gids_are_unique(self) -> None:
        gids = [marker.sd_gid for marker in OWN_MARKERS]
        assert len(gids) == len(set(gids))
        assert SDG_GID not in gids

    def test_the_values_are_unique(self) -> None:
        values = [marker.text for marker in OWN_MARKERS]
        assert len(values) == len(set(values))
