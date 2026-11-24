"""``swcraft_tools.component_exchange`` -- component-scoped ARXML exchange.

Makes one SWC an exchange unit: a deterministic export of the SWC plus its forward closure
(interfaces, transitive data types, the mapping sets it names, each narrowed to that closure)
and a safe import of such an export into an existing, possibly non-empty project.

This package may import from ``writer/`` and ``arxml_import/``; neither imports from here.

Modules:

- ``closure.py`` -- the forward dependency closure of one SWC (:func:`compute_closure`).
- ``export.py`` -- single-component export (:func:`export_component`), atomic; batch mode
  (:func:`export_all_components`) reports a failure per SWC and writes ``export-summary.json``.
- ``import_.py`` -- :func:`plan_component_import` decides and writes nothing;
  :func:`import_component` adds the atomic apply (staged, full rollback; ``dry_run`` writes nothing).
- ``report.py`` -- the export and import reports, the fixed ``export-summary.json`` schema and
  :class:`ComponentImportPlan` / :class:`PlannedTarget`, the planner's result.
"""

from .closure import (
    AmbiguousComponentError,
    AmbiguousReferenceError,
    ComponentClosure,
    ComponentExchangeError,
    ComponentNotFoundError,
    MissingReferenceError,
    compute_closure,
)
from .export import (
    COMPONENTS_DIR,
    ClosureValidationError,
    default_component_file_name,
    default_component_output,
    export_all_components,
    export_component,
    validate_component_closure,
)
from .import_ import (
    AmbiguousTargetProjectError,
    ComponentImportConflictError,
    EarlierReleaseExportError,
    InvalidComponentArxmlError,
    LossyComponentImportError,
    ReservedNameError,
    import_component,
    plan_component_import,
)
from .report import (
    EXPORT_SUMMARY_FILENAME,
    ComponentExportReport,
    ComponentImportPlan,
    ComponentImportReport,
    PlannedTarget,
    render_export_summary,
)

__all__ = [
    "COMPONENTS_DIR",
    "EXPORT_SUMMARY_FILENAME",
    "AmbiguousComponentError",
    "AmbiguousReferenceError",
    "AmbiguousTargetProjectError",
    "ClosureValidationError",
    "ComponentClosure",
    "ComponentExchangeError",
    "ComponentExportReport",
    "ComponentImportConflictError",
    "ComponentImportPlan",
    "ComponentImportReport",
    "ComponentNotFoundError",
    "EarlierReleaseExportError",
    "InvalidComponentArxmlError",
    "LossyComponentImportError",
    "MissingReferenceError",
    "PlannedTarget",
    "ReservedNameError",
    "compute_closure",
    "default_component_file_name",
    "default_component_output",
    "export_all_components",
    "export_component",
    "import_component",
    "plan_component_import",
    "render_export_summary",
    "validate_component_closure",
]
