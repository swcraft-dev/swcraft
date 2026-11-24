"""``swcraft init``: its parser, the scaffolded templates and the file plans it writes."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter

from swcraft_tools.agent_clients import ClientTable, load_client_table
from swcraft_tools.cli.messages import error_text
from swcraft_tools.cli.project_args import check_toolchain_pin
from swcraft_tools.exclusive_create import open_new
from swcraft_tools.model_registry import ModelRegistry, current_registry
from swcraft_tools.project import require_project, resolved_path
from swcraft_tools.reader import read_compositions, read_hardware, read_swc_model
from swcraft_tools.schema import Composition, Hardware
from swcraft_tools.writer.arxml_generation import IMPLEMENTATION_SUFFIX
from swcraft_tools.yaml_load import load_yaml_document

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SWCRAFT_YAML_TEMPLATE = """\
name: {name}
schema_version: "1.0"
model_root: model
dist_root: dist
views_root: views
outputs:
  arxml: dist/model.arxml
  needs: dist/needs.json
"""


#: The two editor settings ``swcraft init`` scaffolds.
#:
#: Red Hat XML resolves an ARXML's ``xsi:schemaLocation``, finds no licensed AUTOSAR schema (it cannot
#: be shipped) and marks a correct file. The filter turns that check off for ``.arxml`` only; it binds
#: no schema (``swcraft validate`` is the only ARXML gate). Not reached: it replaces Red Hat XML's two
#: default filters (Eclipse PDE patterns, unused here), and it is inert without ``redhat.vscode-xml``.
_VSCODE_SETTINGS_TEMPLATE = {
    "xml.validation.filters": [
        {
            "pattern": "**/*.arxml",
            "noGrammar": "ignore",
            "schema": {"enabled": "never"},
        }
    ],
    "cSpell.ignorePaths": ["model/**", "dist/**", "views/**"],
}


#: The three roots a manifest declares when it names none, as :func:`_vscode_settings` takes them.
_DEFAULT_SETTINGS_ROOTS = ("model", "dist", "views")


#: Characters a glob reads as syntax; a root holding one gets no ``cSpell.ignorePaths`` entry.
_GLOB_METACHARACTERS = frozenset("*?[]{}()!\\")


def _vscode_settings(model_root: str, dist_root: str, views_root: str) -> dict:
    """The ``.vscode/settings.json`` ``init`` writes for a project with these three POSIX roots.

    ``cSpell.ignorePaths`` names ``<root>/**`` for each root; a root holding a glob metacharacter gets no
    entry. Not reached: an existing settings file is never edited; a workspace value replaces a
    user-level one; a project opened from a parent folder or a root renamed later is not covered.
    """
    settings = dict(_VSCODE_SETTINGS_TEMPLATE)
    settings["cSpell.ignorePaths"] = [
        f"{root}/**" for root in (model_root, dist_root, views_root) if not _GLOB_METACHARACTERS.intersection(root)
    ]
    return settings


def _init_settings_roots(manifest: Path) -> tuple[str, str, str]:
    """The project-relative POSIX roots of the manifest in place, after checking its pin.

    ``init`` writes, so a pin to another release refuses it here (exit 1) before anything after the
    ``swcraft.yaml`` step is written. An unreadable manifest gives :data:`_DEFAULT_SETTINGS_ROOTS` and
    refuses nothing: reading it only serves the settings file.
    """
    try:
        project = require_project(str(manifest))
    # The loader's refusals only (`RecursionError`: a document too deep to validate); a programming
    # error propagates.
    except (OSError, ValueError, yaml.YAMLError, RecursionError):
        return _DEFAULT_SETTINGS_ROOTS
    check_toolchain_pin(project, "init")
    base = Path(resolved_path(manifest.parent))

    def relative(root: str) -> str:
        return Path(root).relative_to(base).as_posix()

    return (
        relative(project.resolved_model_root),
        relative(project.resolved_dist_root),
        relative(project.resolved_views_root),
    )


#: The hardware elements ``swcraft init`` writes, so a fresh project passes ``check-model``
#: (``hardware.no-processor``).
#:
#: No value is invented: no part number, an advisory uncapped core budget, no memory (``size`` is a
#: hardware fact). ``PARTITION0`` is unrated (a level is the project's safety claim) and is withheld
#: whenever ``CORE0`` is. The template tree holds the same bytes, held equal by a test.
_HARDWARE_PROCESSOR_YAML = """\
name: CPU
kind: processor
description: The project's processor. Replace with the part, e.g. "Infineon AURIX TC397".
"""


_HARDWARE_CORE_YAML = """\
name: CORE0
kind: core
threshold: 1.0
enforcement: advisory
description: Placeholder core budget — advisory and uncapped until the project sets one.
"""


_HARDWARE_PARTITION_YAML = """\
name: PARTITION0
kind: partition
core: CORE0
description: The one partition on CORE0, without a level until the project states one; a task names it.
"""


#: The root composition ``swcraft init`` writes, so a fresh project passes ``check-model``.
#:
#: It has no members, ports or delegations: a fresh project declares no component. The template tree
#: holds the same bytes.
_ROOT_COMPOSITION_YAML = """\
name: Root
title: Root composition
root: true
description: The top of the hierarchy. Add one member per component you declare.
"""


#: The ``compositions/`` element a profile-less ``init`` writes. Apart from :data:`_HARDWARE_ELEMENT_PLAN`
#: because the two are withheld by different content rules.
_ROOT_COMPOSITION_PLAN: dict[str, str] = {
    "model/compositions/Root.yaml": _ROOT_COMPOSITION_YAML,
}


#: The plan entry :func:`_withheld_root_composition` may withhold, read from the plan's one mapping.
_ROOT_COMPOSITION_PLAN_FILES: tuple[str, ...] = tuple(_ROOT_COMPOSITION_PLAN)


#: Parses that plan entry back, so this module holds no second copy of its name or ``root`` flag.
_ROOT_COMPOSITION: TypeAdapter[Composition] = TypeAdapter(Composition)


#: The ``hardware/`` elements a profile-less ``init`` writes. The one spelling of these paths: a drift
#: between the plan and the guard would silently leave the guard nothing to guard.
_HARDWARE_ELEMENT_PLAN: dict[str, str] = {
    "model/hardware/CPU.yaml": _HARDWARE_PROCESSOR_YAML,
    "model/hardware/CORE0.yaml": _HARDWARE_CORE_YAML,
    "model/hardware/PARTITION0.yaml": _HARDWARE_PARTITION_YAML,
}


#: The plan entries :func:`_withheld_hardware_elements` may withhold, in both ``init`` shapes.
_HARDWARE_ELEMENT_PLAN_FILES: tuple[str, ...] = tuple(_HARDWARE_ELEMENT_PLAN)


#: Parses one of those plan entries back through the schema.
_HARDWARE_ELEMENT: TypeAdapter[Hardware] = TypeAdapter(Hardware)


#: Integration profile -> packaged template directory. The template replaces the default plan: writing
#: the default first would leave the wrong ``swcraft.yaml`` and skip the profile's.
_INTEGRATION_TEMPLATES = {"moon": "vehicle-architecture"}


#: Template directory inside the ``swcraft_tools`` package, resolved through ``importlib.resources``
#: so the command works from an installed wheel.
_TEMPLATE_PACKAGE_DIR = "templates"


#: Substituted with the project name while a template file is copied out.
_PROJECT_NAME_PLACEHOLDER = "{{PROJECT_NAME}}"


#: Files that carry an integration profile, in report order. A kept copy of one makes the project a
#: mix of two shapes, which :func:`_integration_skip_warning` reports.
_PROFILE_CRITICAL_FILES = (
    "swcraft.yaml",
    "moon.yml",
    ".prototools",
)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def project_name_problem(name: str) -> str | None:
    """Why *name* cannot be written into ``swcraft.yaml``, or ``None`` when it can.

    The name is written as a bare YAML scalar, so the rule is a round trip: ``name: <value>`` must read
    back as exactly *name*. That rejects only what would change meaning (``:``, ``#``, a bare number).
    """
    if not name.strip():
        return "the project name is empty -- pass --name with a plain name, e.g. --name vehicle-arch"
    try:
        parsed = load_yaml_document(f"name: {name}\n")
    except yaml.YAMLError:
        parsed = None
    if not isinstance(parsed, dict) or parsed.get("name") != name:
        return (
            f"{name!r} cannot be written into swcraft.yaml as a plain YAML value -- "
            "YAML would read it back as something else. Choose a plain name "
            "(letters, digits, spaces, `.`, `_`, `-`), e.g. --name vehicle-arch."
        )
    return None


def _default_file_plan(name: str) -> dict[str, str]:
    """The files a profile-less ``swcraft init`` writes, keyed by relative path.

    No ``.vscode/mcp.json``: the VS Code extension registers the MCP server itself. The model paths use
    the default ``model`` root because no manifest exists yet; the model elements may still be withheld.
    """
    return {
        "swcraft.yaml": _SWCRAFT_YAML_TEMPLATE.format(name=name),
        ".gitignore": _GITIGNORE_ENV_TEMPLATE,
        ".vscode/settings.json": json.dumps(_VSCODE_SETTINGS_TEMPLATE, indent=2),
        **_HARDWARE_ELEMENT_PLAN,
        **_ROOT_COMPOSITION_PLAN,
    }


def _template_file_plan(profile: str, name: str) -> dict[str, str | bytes]:
    """Every file of the *profile*'s packaged template, keyed by relative POSIX path.

    Text files get the project name substituted; a file that is not UTF-8 is copied as ``bytes``.
    """
    root = resource_files("swcraft_tools") / _TEMPLATE_PACKAGE_DIR / _INTEGRATION_TEMPLATES[profile]

    plan: dict[str, str | bytes] = {}

    def walk(node, prefix: str) -> None:
        for child in sorted(node.iterdir(), key=lambda item: item.name):
            if child.name == "__pycache__":
                continue
            relative = f"{prefix}{child.name}"
            if child.is_dir():
                walk(child, f"{relative}/")
                continue
            try:
                text = child.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                plan[relative] = child.read_bytes()
            else:
                plan[relative] = text.replace(_PROJECT_NAME_PLACEHOLDER, name)

    walk(root, "")
    return plan


def _drop_unchosen_client_files(plan: dict[str, str | bytes], table: ClientTable, chosen: tuple[str, ...]) -> None:
    """The removal from *plan* of every client file of the template that no client in *chosen* owns.

    A file in no client row is shared and kept. Nothing on disk is touched.
    """
    for relative in table.all_client_files() - table.files_for(chosen):
        plan.pop(relative, None)


def _linked_component(target: Path, relative: str) -> Path | None:
    """The first of *relative*'s components under *target* that is a link, or ``None``.

    Asked with ``lstat`` so a dangling link is found too; *target* itself is resolved by the caller.
    """
    current = target
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            return current
    return None


def _write_if_absent(target: Path, relative: str, content: str | bytes) -> bool:
    """*content* written to ``target / relative``, never overwriting and never through a link; whether it was.

    Returns ``False`` with a ``Skipped`` line when the file exists or the destination, or a folder
    between *target* and it, is a link (a dangling one included): a link points somewhere ``init`` was
    not asked to write.

    The write is exclusive, so a file or link put in place after the check fails it rather than being
    followed. Not reached: a Windows directory junction, a link swapped in for a folder before the
    ``mkdir``, and the layer directories. ``OSError`` propagates to :func:`cmd_init`'s one handler.
    """
    path = target / relative
    linked = _linked_component(target, relative)
    if linked is not None:
        what = "is a link" if linked == path else f"lies below the link {linked}"
        print(f"Skipped {path} (it {what}; init never writes through one)")
        return False
    if path.exists():
        print(f"Skipped {path} (already exists)")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    # Text is written with `\n` line ends: the user commits these files, and CRLF would give one
    # formatter diagnostic per file on Windows.
    try:
        created = open_new(path, binary=isinstance(content, bytes))
    except OSError as error:
        # `open_new` names no path; this is the user's own project file, so name it.
        raise type(error)(error.errno, error.strerror, str(path)) from None
    # A failed write removes the created file, so a rerun does not skip an empty one.
    with created as handle:
        handle.write(content)
    print(f"Created {path}")
    return True


#: The ``.gitignore`` a plain ``init`` writes; both forms make an existing one ignore ``.env``.
_GITIGNORE_ENV_TEMPLATE = "# Local settings and secrets (the Azure keys) — never committed.\n.env\n"


def _add_env_to_gitignore(target: Path) -> bool:
    """The user's own ``.gitignore`` made to ignore ``.env``; ``False`` when there is no such file.

    ``.env`` is appended in the file's own line ending unless a line already strips to ``.env`` or
    ``/.env``. A link is left to :func:`_write_if_absent`. Not reached: a later ``!.env``, a broader
    pattern, a global excludes file.
    """
    path = target / ".gitignore"
    if _linked_component(target, ".gitignore") is not None or not path.is_file():
        return False
    held = path.read_bytes()
    if {b".env", b"/.env"} & {line.strip() for line in held.splitlines()}:
        print(f"Skipped {path} (already exists)")
        return True
    ending = b"\r\n" if b"\r\n" in held else b"\n"
    with path.open("ab") as handle:
        handle.write((b"" if not held or held.endswith(b"\n") else ending) + b".env" + ending)
    print(f"Updated {path} (added .env)")
    return True


def _plan_entry_text(plan: Mapping[str, str | bytes], relative: str) -> str:
    """The text of the ``init`` plan entry *relative*, a model element file; ``ValueError`` if it is not UTF-8."""
    content = plan[relative]
    if isinstance(content, bytes):
        raise ValueError(f"the template's {relative} is not UTF-8 text")
    return content


def _withheld_hardware_elements(model_root: Path, plan: Mapping[str, str | bytes]) -> list[tuple[str, str]]:
    """The scaffolded ``hardware/`` elements *model_root* must not be given, with the reason.

    The per-path guard is not enough: ``hardware.multiple-processors`` and ``hardware.duplicate-name`` span
    files, so ``CPU.yaml`` beside an existing ``TC397.yaml`` processor would break ``check-model``. A
    candidate is withheld when the layer already declares its name, or a processor and it is one. A
    partition is also withheld whenever its core is not written by this run.

    A layer that does not read (``ValueError`` or ``OSError``) withholds every candidate: ``init`` does
    not report a broken model and must not add to it blindly. Not reached: no other hardware rule is
    checked and no existing file is touched.

    Returns:
        ``(relative path, reason)`` pairs in :data:`_HARDWARE_ELEMENT_PLAN_FILES` order.
    """
    candidates = [relative for relative in _HARDWARE_ELEMENT_PLAN_FILES if relative in plan]
    if not candidates:
        return []
    try:
        declared = read_hardware(str(model_root))
    except (OSError, ValueError):
        return [(relative, "hardware/ does not read; fix it and re-run") for relative in candidates]

    names = {element.name for element in declared}
    processors = [element.name for element in declared if element.kind == "processor"]
    elements = {
        relative: _HARDWARE_ELEMENT.validate_python(load_yaml_document(_plan_entry_text(plan, relative)))
        for relative in candidates
    }

    withheld: dict[str, str] = {}
    for relative, element in elements.items():
        if element.name in names:
            withheld[relative] = f"hardware/ already declares '{element.name}'"
        elif element.kind == "processor" and processors:
            withheld[relative] = f"hardware/ already declares the processor '{processors[0]}'"

    # The plan's paths start with `model/`, so they resolve against the project directory.
    project_root = model_root.parent
    written_cores = {
        element.name
        for relative, element in elements.items()
        if element.kind == "core" and relative not in withheld and not (project_root / relative).exists()
    }
    for relative, element in elements.items():
        if element.kind == "partition" and relative not in withheld and element.core not in written_cores:
            withheld[relative] = f"its core '{element.core}' is not one this run writes"
    return [(relative, withheld[relative]) for relative in candidates if relative in withheld]


def _withheld_root_composition(model_root: Path, plan: Mapping[str, str | bytes]) -> list[tuple[str, str]]:
    """The scaffolded root composition *model_root* must not be given, with the reason.

    :func:`_withheld_hardware_elements`' rule for ``compositions/`` (``composition.multiple-roots``,
    ``composition.duplicate-name``): withheld when the layer declares a root or the name, or when ``swc/``
    claims the name in the shared ``/ComponentTypes`` ``SHORT-NAME`` namespace (a component, or a generated
    ``{name}_Implementation`` of a non-external one; an external stub emits no implementation, as in the
    writer). An unreadable ``compositions/`` or ``swc/`` withholds it. Not reached: no other composition
    rule is checked and no existing file is touched.

    Returns:
        ``(relative path, reason)`` pairs in :data:`_ROOT_COMPOSITION_PLAN_FILES` order.
    """
    candidates = [relative for relative in _ROOT_COMPOSITION_PLAN_FILES if relative in plan]
    if not candidates:
        return []
    try:
        declared = read_compositions(str(model_root))
    except (OSError, ValueError):
        return [(relative, "compositions/ does not read; fix it and re-run") for relative in candidates]
    try:
        components = read_swc_model(str(model_root))
    except (OSError, ValueError):
        return [(relative, "swc/ does not read; fix it and re-run") for relative in candidates]
    if not declared and not components:
        return []

    names = {composition.name for composition in declared}
    roots = [composition.name for composition in declared if composition.root]
    # The `swc/` half of the `/ComponentTypes` namespace; only non-external components emit an
    # implementation.
    component_names = {swc.name for swc in components}
    implementations = {f"{swc.name}{IMPLEMENTATION_SUFFIX}": swc.name for swc in components if not swc.external}

    withheld: list[tuple[str, str]] = []
    for relative in candidates:
        element = _ROOT_COMPOSITION.validate_python(load_yaml_document(_plan_entry_text(plan, relative)))
        if element.name in names:
            withheld.append((relative, f"compositions/ already declares '{element.name}'"))
        elif element.name in component_names:
            withheld.append((relative, f"swc/ already declares the component type '{element.name}'"))
        elif element.name in implementations:
            withheld.append(
                (
                    relative,
                    f"swc/ generates the SHORT-NAME '{element.name}' for component '{implementations[element.name]}'",
                )
            )
        elif element.root and roots:
            withheld.append((relative, f"compositions/ already declares the root '{roots[0]}'"))
    return withheld


def _scaffold_model_directories(model_root: Path, *, registry: ModelRegistry | None = None) -> list[Path]:
    """One directory created under *model_root* per non-optional declared layer, returned in contract order.

    A total consumer of the layer inventory: optionality changes the action, never whether a layer is
    handled. Not reached: nesting (``swc``'s group folders are created when an element is written).
    *registry* defaults to the current one at call time, so :func:`~swcraft_tools.model_registry.use_registry`
    applies.
    """
    registry = registry or current_registry()
    created: list[Path] = []
    for facts in registry.layers:
        if facts.optional:
            continue
        directory = model_root / facts.storage.directory
        directory.mkdir(parents=True, exist_ok=True)
        print(f"Created {directory}")
        created.append(directory)
    return created


def _integration_skip_warning(skipped: list[str]) -> str | None:
    """The warning for kept files that carry the profile, or ``None`` when none was kept."""
    skipped_set = set(skipped)
    critical = [relative for relative in _PROFILE_CRITICAL_FILES if relative in skipped_set]
    if not critical:
        return None
    return (
        "Warning: kept your existing " + ", ".join(critical) + " -- the --integration template's "
        "version was NOT written. Those files carry the profile (the manifest and its toolchain "
        "pin, the build graph, the moon pin), so the project is now a mix of both shapes. Review "
        "them against the template, or move them aside and re-run this command."
    )


def cmd_init(args: argparse.Namespace) -> None:
    """``swcraft init``: a new SWCraft project scaffolded at *args.path*.

    Without ``--integration`` it writes the default plan and one directory per non-optional layer; with a
    profile, that profile's template instead (so the defaults are never written first). Neither writes
    ``.vscode/mcp.json``. Existing files are never overwritten, except that ``.env`` is appended to a
    ``.gitignore`` lacking it; the model elements are also withheld by content rules.

    ``.vscode/settings.json`` is the one derived write: rendered from the roots of the manifest in place,
    plus the client table's companion keys when they apply. With a profile, ``args.client`` (default: the
    table's ``default``) chooses which client files are written; a conflicting set prints its warnings on
    stderr after the files. Reading the manifest checks its pin: a mismatch refuses (exit 1) before any
    later write.

    A filesystem refusal exits 1 with ``Error: <exc>``; files already created stay (not all-or-nothing).
    """
    target = Path(args.path).resolve()

    name = args.name or target.name
    problem = project_name_problem(name)
    if problem is not None:
        print(f"Error: {problem}", file=sys.stderr)
        sys.exit(1)

    skipped: list[str] = []
    profile = args.integration

    # One handler reports every filesystem refusal; what was created stays created.
    try:
        target.mkdir(parents=True, exist_ok=True)

        plan: dict[str, str | bytes]
        plan = {**_default_file_plan(name)} if profile is None else _template_file_plan(profile, name)
        companion: dict[str, Any] = {}
        client_warnings: tuple[str, ...] = ()
        if profile is not None:
            table = load_client_table()
            if table.template != _INTEGRATION_TEMPLATES[profile]:
                # The table's paths are relative to the template it names.
                print(
                    f"Error: the client table describes the {table.template} template, not "
                    f"{_INTEGRATION_TEMPLATES[profile]}, so no file was written.",
                    file=sys.stderr,
                )
                sys.exit(1)
            chosen = tuple(dict.fromkeys(args.client or table.default))
            _drop_unchosen_client_files(plan, table, chosen)
            if table.companion.applies(chosen):
                companion = table.companion.settings
            client_warnings = table.warnings_for(chosen)

        # swcraft.yaml first — it is the file that makes the directory a project.
        if not _write_if_absent(target, "swcraft.yaml", plan.pop("swcraft.yaml")):
            skipped.append("swcraft.yaml")

        # The manifest in place is read once: its pin before any other write, its roots for the settings.
        plan[".vscode/settings.json"] = json.dumps(
            {**_vscode_settings(*_init_settings_roots(target / "swcraft.yaml")), **companion}, indent=2
        )

        _scaffold_model_directories(target / "model")

        # Content rules the per-path guard below cannot state.
        for relative, reason in (
            *_withheld_hardware_elements(target / "model", plan),
            *_withheld_root_composition(target / "model", plan),
        ):
            del plan[relative]
            print(f"Skipped {target / relative} ({reason})")

        for relative in sorted(plan):
            if relative == ".gitignore" and _add_env_to_gitignore(target):
                continue
            if not _write_if_absent(target, relative, plan[relative]):
                skipped.append(relative)
    except OSError as exc:
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)

    if profile is not None:
        warning = _integration_skip_warning(skipped)
        if warning is not None:
            print(warning, file=sys.stderr)
        # The choice is the confirmation: warn, ask nothing.

        for line in client_warnings:
            print(f"Warning: {line}", file=sys.stderr)


def add_init_parser(subparsers: argparse._SubParsersAction) -> None:
    """The ``swcraft init`` parser: a target directory, a name, a build integration and its chat clients."""
    init_parser = subparsers.add_parser("init", help="Scaffold a new SWCraft project directory")
    init_parser.add_argument("path", help="Target directory to initialise")
    init_parser.add_argument(
        "--name",
        default=None,
        metavar="NAME",
        help="Project name (default: the target directory name)",
    )
    init_parser.add_argument(
        "--integration",
        default=None,
        choices=sorted(_INTEGRATION_TEMPLATES),
        help=(
            "Also scaffold a build integration: 'moon' writes the "
            "vehicle-architecture template (moon build graph, the toolchain "
            "version pin, the chosen chat clients' files)"
        ),
    )
    clients = load_client_table()
    init_parser.add_argument(
        "--client",
        action="append",
        default=None,
        choices=clients.names(),
        metavar="CLIENT",
        help=(
            "With --integration moon: a chat client to set the project up for, repeatable: "
            + ", ".join(f"'{client.name}' for {client.label}" for client in clients.clients)
            + "; default: "
            + ", ".join(clients.default)
            + ". Needs --integration moon"
        ),
    )
