"""The ``swcraft`` command line.

Run it as the ``swcraft`` console script or as ``python -m swcraft_tools.cli``; both call
:func:`swcraft_tools.cli.dispatch.main`. ``swcraft <command> --help`` is the reference for each
subcommand's flags. This module only re-exports the command handlers and public names; a test
patches a name on the module that looks it up.

Every subcommand that reads a manifest first compares its optional ``toolchain_version`` with this
install's version. A mismatch stops a command that writes or gates; the six read-only commands
(``diff``, ``impact``, ``list``, ``metrics``, ``query``, ``resource-report``) print a warning and answer,
because a pin mismatch is not a failure to read the project. ``swcraft-mcp`` and ``swcraft-agent``
read the manifest through ``SWCRAFT_MANIFEST`` and do not compare the pin.

A build graph runs ``check-model``, which writes nothing, as its model gate, never ``validate``, which
generates the ARXML.
"""

from swcraft_tools.cli.build_commands import (
    CHECK_MODEL_SCHEMA_VERSION,
    MANIFEST_UNKNOWN_KEY_RULE,
    cmd_check_model,
    cmd_clean_outputs,
    cmd_generate,
    cmd_validate,
)
from swcraft_tools.cli.dispatch import main
from swcraft_tools.cli.docs_command import (
    DOCS_BIND_HOST,
    DOCS_BROWSER_JOIN_SECONDS,
    DOCS_CONTENT_TYPES,
    GuideRequestHandler,
    cmd_docs,
)
from swcraft_tools.cli.experiment_command import cmd_experiment
from swcraft_tools.cli.guard_command import cmd_guard
from swcraft_tools.cli.handover import cmd_agent, cmd_mcp_serve
from swcraft_tools.cli.import_commands import (
    UPDATE_RESOLVES_EXIT,
    cmd_import,
    cmd_import_component,
    cmd_import_requirements,
)
from swcraft_tools.cli.init_command import cmd_init, project_name_problem
from swcraft_tools.cli.messages import model_read_failure_sentence, read_failure_sentence
from swcraft_tools.cli.passport_command import cmd_passport
from swcraft_tools.cli.query_command import cmd_query
from swcraft_tools.cli.read_commands import (
    cmd_change_report,
    cmd_diff,
    cmd_impact,
    cmd_list,
    cmd_metrics,
    cmd_resource_report,
)
from swcraft_tools.cli.report_files import is_markdown_report
from swcraft_tools.toolchain_pin import toolchain_pin_problem as toolchain_pin_problem

__all__ = [
    "CHECK_MODEL_SCHEMA_VERSION",
    "DOCS_BIND_HOST",
    "DOCS_BROWSER_JOIN_SECONDS",
    "DOCS_CONTENT_TYPES",
    "GuideRequestHandler",
    "MANIFEST_UNKNOWN_KEY_RULE",
    "UPDATE_RESOLVES_EXIT",
    "cmd_agent",
    "cmd_change_report",
    "cmd_check_model",
    "cmd_clean_outputs",
    "cmd_diff",
    "cmd_docs",
    "cmd_experiment",
    "cmd_generate",
    "cmd_guard",
    "cmd_impact",
    "cmd_import",
    "cmd_import_component",
    "cmd_import_requirements",
    "cmd_init",
    "cmd_list",
    "cmd_mcp_serve",
    "cmd_metrics",
    "cmd_passport",
    "cmd_query",
    "cmd_resource_report",
    "cmd_validate",
    "is_markdown_report",
    "main",
    "model_read_failure_sentence",
    "project_name_problem",
    "read_failure_sentence",
    "toolchain_pin_problem",
]
