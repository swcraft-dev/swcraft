"""ARXML import: maps an AUTOSAR export onto a SWCraft model project.

``run_import()`` (``pipeline.py``) is the entry point. ``import component`` reuses
the four mappers (``map_data_types``, ``map_interfaces``, ``map_swcs``,
``map_compositions``) with no merge verdict, and renames nothing.

Module dependency order (each module imports only those to its left; no cycles):

    registry, merge -> merge_verdict -> unresolved -> impl_type_resolution -> type_mapping -> type_mappers
                    -> swc_mapping -> runnable_accesses -> swc_mapper -> connectors -> reconcile -> report
                    -> orchestrator -> pipeline

``registry.fit_identifier()`` is the ``Identifier`` length cap every name-minting
site shares. ``reconcile.place_case_variants()`` keeps the names of two elements
of one root package that differ only in case and records the last segment of the
moved one's source package as its ``package`` (``[constr_2508]``).

Every name in ``__all__`` is a deliberate re-export, so ruff's F401 does not flag it.
"""

from .connectors import EXTERNAL_STUB_GROUP, MappedCompositions, derive_root, map_compositions
from .pipeline import run_import
from .reconcile import (
    ReconciledModel,
    free_name,
    identifier_violations,
    reconcile_reserved_names,
    reserved_data_type_names,
    reserved_interface_names,
    reserved_swc_names,
)
from .registry import RENAME_SUFFIX, NameCollisionError, PathRegistry, fit_identifier
from .report import NO_REQUIREMENT_SENTENCE, ImportReport
from .swc_mapper import map_swcs
from .swc_mapping import ImportedSwc
from .type_mappers import map_data_types, map_interfaces

__all__ = [
    "ImportedSwc",
    "ImportReport",
    "EXTERNAL_STUB_GROUP",
    "MappedCompositions",
    "NO_REQUIREMENT_SENTENCE",
    "NameCollisionError",
    "PathRegistry",
    "ReconciledModel",
    "RENAME_SUFFIX",
    "derive_root",
    "fit_identifier",
    "free_name",
    "identifier_violations",
    "map_compositions",
    "map_data_types",
    "map_interfaces",
    "map_swcs",
    "reconcile_reserved_names",
    "reserved_data_type_names",
    "reserved_interface_names",
    "reserved_swc_names",
    "run_import",
]
