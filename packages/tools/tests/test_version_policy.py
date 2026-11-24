"""Guard tests for the version policy and the declared dependency graph.

A wheel installed into a fresh venv has no source checkout to fall back on, so the
repository needs exactly one version string and every package must declare what it uses.
This module pins three things:

1. **One version, six sites.** ``packages/tools/src/swcraft_tools/_version.py`` is the single
   source of truth; ``packages/tools/pyproject.toml`` consumes it through hatchling's dynamic
   version, ``mcp_server``/``agent`` restate it literally, ``specimen_generator`` restates
   it once in ``swcraft_specimens/__init__.py`` (its ``pyproject.toml`` reads that module
   through hatchling's dynamic version, so ``--version`` and the wheel agree), and the
   VS Code extension carries the same string in both ``package.json`` and
   ``package-lock.json``.
2. **Every direct use is a declared dependency.** The import side is discovered with
   ``ast`` over each package's ``src/`` — never a hand-written list. The agent→MCP edge is
   a *subprocess* spawn the ast audit is blind to; it gets its own behavioural guard.
3. **A removed dependency stays removed** (``TestAutosarIsGone``): the audit above only
   looks at what *is* imported, and a still-installed package lets a re-added import pass.

Not checked here: that the declared version has release notes (a closed check).
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from conftest import REPO_ROOT, dev_module
from swcraft_tools import schema
from swcraft_tools.yaml_load import load_yaml_document

_TOOLS_PYPROJECT = REPO_ROOT / "packages" / "tools" / "pyproject.toml"
_MCP_PYPROJECT = REPO_ROOT / "packages" / "mcp" / "pyproject.toml"
_AGENT_PYPROJECT = REPO_ROOT / "packages" / "agent" / "pyproject.toml"
_SPECIMENS_PYPROJECT = REPO_ROOT / "packages" / "specimens" / "pyproject.toml"
_SPECIMENS_VERSION_MODULE = REPO_ROOT / "packages" / "specimens" / "src" / "swcraft_specimens" / "__init__.py"
_PACKAGE_JSON = REPO_ROOT / "vscode_extension" / "package.json"
_PACKAGE_LOCK = REPO_ROOT / "vscode_extension" / "package-lock.json"
_VERSION_MODULE = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "_version.py"
_CITATION = REPO_ROOT / "CITATION.cff"
_MCP_CLIENT = REPO_ROOT / "packages" / "agent" / "src" / "swcraft_agent" / "mcp_client.py"

# (distribution name, package directory, importable package name)
_PYTHON_PACKAGES = (
    ("swcraft-tools", REPO_ROOT / "packages" / "tools", "swcraft_tools"),
    ("swcraft-mcp", REPO_ROOT / "packages" / "mcp", "swcraft_mcp"),
    ("swcraft-agent", REPO_ROOT / "packages" / "agent", "swcraft_agent"),
    ("swcraft-specimens", REPO_ROOT / "packages" / "specimens", "swcraft_specimens"),
)

#: Every directory whose Python sources must stay free of an ``autosar`` import:
#: each package's ``src/`` and test tree, plus the repo-root ``scripts/``. The test
#: trees matter most: tests characterizing the library's API are where a relapse is likeliest.
_AUTOSAR_AUDIT_ROOTS = tuple(
    directory
    for _, package_dir, package_name in _PYTHON_PACKAGES
    for directory in (package_dir / "src" / package_name, package_dir / "tests")
) + (REPO_ROOT / "scripts",)

# Top-level module name -> distribution name that provides it; static analysis cannot
# derive this. `importlib.metadata.packages_distributions()` is deliberately not used:
# it reports whatever variant is installed (e.g. `fastmcp-slim`) and nothing for
# editable installs, so the guard would depend on the machine.
# `test_module_map_covers_every_declared_dependency` keeps the table from going stale.
_MODULE_TO_DISTRIBUTION = {
    "anyio": "anyio",
    "dotenv": "python-dotenv",
    "fastmcp": "fastmcp",
    "lxml": "lxml",
    "markdown": "markdown",
    "mcp": "mcp",
    "mkdocs": "mkdocs",
    "numpy": "numpy",
    "openai": "openai",
    "pydantic": "pydantic",
    "scipy": "scipy",
    "swcraft_agent": "swcraft-agent",
    "swcraft_mcp": "swcraft-mcp",
    "swcraft_specimens": "swcraft-specimens",
    "swcraft_tools": "swcraft-tools",
    "typing_extensions": "typing-extensions",
    "xmlschema": "xmlschema",
    "yaml": "pyyaml",
}

# Package -> {top-level module -> the source files allowed to import it}: the exception to
# "a direct import is a runtime dependency" for a development dependency product source
# imports only inside a function (`guide_corpus.py` needs mkdocs/markdown where the docs
# build runs). `test_every_development_only_import_is_lazy_and_declared_for_development`
# holds every entry.
_DEVELOPMENT_ONLY_IMPORTS: dict[str, dict[str, tuple[str, ...]]] = {
    "swcraft_tools": {
        "markdown": ("guide_corpus.py",),
        "mkdocs": ("guide_corpus.py",),
    },
}


# ---------------------------------------------------------------------------
# Pure helpers (exercised directly by the error-case tests below)
# ---------------------------------------------------------------------------


def _pyproject_version(pyproject_path: Path) -> str:
    """Return a pyproject's effective version, resolving hatchling's dynamic form.

    A dynamic version is read from the module named by ``[tool.hatch.version] path``.
    """
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = data["project"]

    if "version" in project:
        return str(project["version"])

    if "version" not in project.get("dynamic", []):
        raise AssertionError(f"{pyproject_path} declares neither a static nor a dynamic version")

    version_path = data["tool"]["hatch"]["version"]["path"]
    return _version_module_value(pyproject_path.parent / version_path)


def _version_module_value(module_path: Path) -> str:
    """Read ``__version__`` out of a version module without importing it."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "__version__" in targets and isinstance(node.value, ast.Constant):
                return str(node.value.value)
    raise AssertionError(f"{module_path} does not assign a literal __version__")


def _requirement_name(requirement: str) -> str:
    """The canonical distribution name of a requirement string, extras, specifiers and markers stripped."""
    name = requirement.split(";")[0].strip()
    for separator in ("[", "=", "<", ">", "!", "~", " "):
        name = name.split(separator)[0]
    return name.strip().lower().replace("_", "-")


def _declared_dependencies(pyproject_path: Path) -> dict[str, str]:
    """Map ``distribution name -> full requirement string`` for a package's runtime deps."""
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    return {_requirement_name(requirement): requirement for requirement in data["project"].get("dependencies", [])}


def _declared_distributions(pyproject_path: Path) -> set[str]:
    """Every distribution a package declares — runtime **and** its ``dev`` dependency group.

    A removed dependency must be gone from the ``dev`` group too. The group is indexed
    directly so a project without one fails instead of passing on an empty list.
    """
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    requirements = [*data["project"].get("dependencies", []), *data["dependency-groups"]["dev"]]
    return {_requirement_name(requirement) for requirement in requirements}


def _import_sites(root: Path, module: str) -> list[str]:
    """``path:lineno`` for every statement under *root* importing *module* (ast-based).

    Not a text search: prose names the library legitimately; only an ``import`` is a relapse.
    """
    sites: list[str] = []
    for source in sorted(root.rglob("*.py"), key=lambda path: path.as_posix()):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == module or name.startswith(f"{module}.") for name in names):
                sites.append(f"{source}:{node.lineno}")
    return sites


def _direct_imports(src_dir: Path) -> set[str]:
    """Every top-level module name imported anywhere under ``src_dir`` (ast-based)."""
    modules: set[str] = set()
    for py_file in sorted(src_dir.rglob("*.py"), key=lambda path: path.as_posix()):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
    return modules


def _imports_by_scope(source: Path) -> tuple[set[str], set[str]]:
    """``(module-level, inside a function)`` top-level module names *source* imports."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))

    def names(node: ast.AST) -> set[str]:
        found: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                found |= {alias.name.split(".")[0] for alias in child.names}
            elif isinstance(child, ast.ImportFrom) and child.level == 0 and child.module:
                found.add(child.module.split(".")[0])
        return found

    in_functions: set[str] = set()
    outside: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            in_functions |= names(node)
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    in_functions |= names(member)
                else:
                    outside |= names(member)
        else:
            outside |= names(node)
    return outside, in_functions


def _third_party_imports(src_dir: Path, own_package: str) -> set[str]:
    """Direct imports that are neither stdlib nor the package importing them."""
    return {
        module for module in _direct_imports(src_dir) if module not in sys.stdlib_module_names and module != own_package
    }


def _undeclared_imports(pyproject_path: Path, src_dir: Path, own_package: str) -> set[str]:
    """Module names imported directly but absent from the package's declared deps."""
    declared = _declared_dependencies(pyproject_path)
    undeclared = set()
    for module in _third_party_imports(src_dir, own_package):
        distribution = _MODULE_TO_DISTRIBUTION.get(module)
        if distribution is None:
            raise AssertionError(
                f"{src_dir} imports unknown top-level module {module!r}; add it to "
                "_MODULE_TO_DISTRIBUTION in packages/tools/tests/test_version_policy.py"
            )
        if distribution.lower() not in declared:
            undeclared.add(module)
    return undeclared


# ---------------------------------------------------------------------------
# Version sites
# ---------------------------------------------------------------------------


class TestVersionSites:
    def test_version_module_is_the_single_source_of_truth(self) -> None:
        """packages/tools/pyproject.toml resolves its version dynamically from _version.py."""
        data = tomllib.loads(_TOOLS_PYPROJECT.read_text(encoding="utf-8"))
        assert "version" not in data["project"], (
            "packages/tools/pyproject.toml must not restate the version — it is dynamic, "
            "sourced from src/swcraft_tools/_version.py"
        )
        assert "version" in data["project"]["dynamic"]
        assert data["tool"]["hatch"]["version"]["path"] == "src/swcraft_tools/_version.py"

    def test_all_six_version_sites_agree(self) -> None:
        """The four pyprojects, package.json, package-lock.json and __version__ match.

        The specimen generator ships at the product's version, never one of its own.
        """
        expected = _version_module_value(_VERSION_MODULE)

        sites = {
            "packages/tools/pyproject.toml": _pyproject_version(_TOOLS_PYPROJECT),
            "packages/mcp/pyproject.toml": _pyproject_version(_MCP_PYPROJECT),
            "packages/agent/pyproject.toml": _pyproject_version(_AGENT_PYPROJECT),
            "packages/specimens/src/swcraft_specimens/__init__.py": _pyproject_version(_SPECIMENS_PYPROJECT),
            "vscode_extension/package.json": json.loads(_PACKAGE_JSON.read_text(encoding="utf-8"))["version"],
            "vscode_extension/package-lock.json": json.loads(_PACKAGE_LOCK.read_text(encoding="utf-8"))["version"],
        }

        mismatched = {site: value for site, value in sites.items() if value != expected}
        assert not mismatched, f"Version sites disagree with {expected!r}: {mismatched}"

    def test_the_specimen_generator_has_one_version_literal(self) -> None:
        """The generator's wheel metadata, ``--version`` and every sidecar read one literal.

        ``swcraft_specimens.__version__`` is what ``--version`` prints and every sidecar's
        ``generator.version`` records; a second literal in the pyproject would let a bump move the
        wheel's version and leave those behind with every gate green.
        """
        data = tomllib.loads(_SPECIMENS_PYPROJECT.read_text(encoding="utf-8"))
        assert "version" not in data["project"], "packages/specimens/pyproject.toml must not restate the version"
        assert "version" in data["project"]["dynamic"]
        assert data["tool"]["hatch"]["version"]["path"] == "src/swcraft_specimens/__init__.py"
        assert _version_module_value(_SPECIMENS_VERSION_MODULE) == _version_module_value(_VERSION_MODULE)

    def test_package_lock_root_entry_carries_the_version_too(self) -> None:
        """`npm version` rewrites packages[""].version as well — assert it, not just the top level."""
        expected = _version_module_value(_VERSION_MODULE)
        lock = json.loads(_PACKAGE_LOCK.read_text(encoding="utf-8"))
        assert lock["packages"][""]["version"] == expected

    def test_every_python_project_is_a_version_site(self) -> None:
        """A project ``dev.py`` builds and this test does not read would carry a version nothing holds."""
        assert [package_dir.name for _, package_dir, _ in _PYTHON_PACKAGES] == list(dev_module().PYTHON_PROJECTS)

    def test_the_citation_carries_the_version(self) -> None:
        """``CITATION.cff``'s ``version`` matches; not a lockstep site, since no artifact is built from it."""
        citation = load_yaml_document(_CITATION.read_text(encoding="utf-8"))
        assert isinstance(citation, dict)
        assert str(citation["version"]) == _version_module_value(_VERSION_MODULE)
        assert citation["license"] == "Apache-2.0"

    def test_swcraft_tools_reexports_the_version(self) -> None:
        """`swcraft_tools.__version__` is importable and equals the module's value."""
        import swcraft_tools

        assert swcraft_tools.__version__ == _version_module_value(_VERSION_MODULE)


class TestLicenceMetadata:
    """Every artifact declares Apache-2.0, ``swcraft-tools`` with the EPL-2.0 of its elkjs files.

    The Python projects declare **no** ``license-files``: a declared file must exist at build
    time, editable installs included, and the root ``LICENSE``/``NOTICE`` copies exist only once
    ``scripts/build_artifacts.py`` has staged them.
    """

    #: ``swcraft-tools`` ships elkjs files under EPL-2.0, and PEP 639's expression describes
    #: the whole distribution.
    _EXPRESSIONS = {"tools": "Apache-2.0 AND EPL-2.0"}

    @pytest.mark.parametrize("project", [package_dir for _, package_dir, _ in _PYTHON_PACKAGES], ids=lambda p: p.name)
    def test_every_python_project_declares_its_licence_and_no_licence_files(self, project: Path) -> None:
        data = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
        assert data["project"]["license"] == self._EXPRESSIONS.get(project.name, "Apache-2.0")
        assert "license-files" not in data["project"], "a declared licence file breaks a fresh editable install"

    @pytest.mark.parametrize("project", [package_dir for _, package_dir, _ in _PYTHON_PACKAGES], ids=lambda p: p.name)
    def test_development_tools_are_a_dependency_group_and_never_an_extra(self, project: Path) -> None:
        """PEP 735: development tools are a ``dev`` group, never an extra a wheel offers."""
        data = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
        assert "optional-dependencies" not in data["project"]
        assert data["dependency-groups"]["dev"]

    @pytest.mark.parametrize("project", [package_dir for _, package_dir, _ in _PYTHON_PACKAGES], ids=lambda p: p.name)
    def test_every_build_backend_reads_an_spdx_expression(self, project: Path) -> None:
        """An SPDX ``project.license`` needs hatchling 1.27+, declared where an isolated build resolves it."""
        data = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
        assert data["build-system"]["requires"] == ["hatchling>=1.27"]

    def test_the_extension_declares_apache_2_0_in_both_files(self) -> None:
        assert json.loads(_PACKAGE_JSON.read_text(encoding="utf-8"))["license"] == "Apache-2.0"
        lock = json.loads(_PACKAGE_LOCK.read_text(encoding="utf-8"))
        assert lock["packages"][""]["license"] == "Apache-2.0"


# ---------------------------------------------------------------------------
# The declared dependency graph
# ---------------------------------------------------------------------------


class TestDependencyGraph:
    def test_agent_declares_both_inter_package_edges_directly(self) -> None:
        """swcraft-agent pins swcraft-mcp AND swcraft-tools — no transitive reliance."""
        version = _version_module_value(_VERSION_MODULE)
        declared = _declared_dependencies(_AGENT_PYPROJECT)

        for distribution in ("swcraft-mcp", "swcraft-tools"):
            assert distribution in declared, (
                f"swcraft-agent must declare {distribution} directly — it spawns "
                "`-m swcraft_mcp.server` and imports swcraft_tools.env"
            )
            assert declared[distribution] == f"{distribution}=={version}", (
                f"swcraft-agent must pin {distribution} at =={version}, got {declared[distribution]!r}"
            )

    def test_mcp_declares_the_tools_edge(self) -> None:
        version = _version_module_value(_VERSION_MODULE)
        declared = _declared_dependencies(_MCP_PYPROJECT)
        assert declared.get("swcraft-tools") == f"swcraft-tools=={version}"

    def test_inter_package_pins_are_exact(self) -> None:
        """All three packages move in lockstep, so every internal edge uses `==`."""
        internal = {"swcraft-tools", "swcraft-mcp", "swcraft-agent"}
        for _, package_dir, _ in _PYTHON_PACKAGES:
            declared = _declared_dependencies(package_dir / "pyproject.toml")
            for name, requirement in declared.items():
                if name in internal:
                    assert "==" in requirement, f"{package_dir.name}: {requirement!r} must pin with =="

    def test_every_direct_import_is_a_declared_dependency(self) -> None:
        """ast-based audit: a direct import is a direct dependency, in every package."""
        offenders = {}
        for _, package_dir, package_name in _PYTHON_PACKAGES:
            undeclared = _undeclared_imports(
                package_dir / "pyproject.toml",
                package_dir / "src" / package_name,
                package_name,
            )
            undeclared -= set(_DEVELOPMENT_ONLY_IMPORTS.get(package_name, {}))
            if undeclared:
                offenders[package_dir.name] = sorted(undeclared)
        assert not offenders, f"Direct imports missing from the declaring package's dependencies: {offenders}"

    def test_every_development_only_import_is_lazy_and_declared_for_development(self) -> None:
        """Each development-only entry: in ``dev`` and not runtime, imported lazily, by the named files only.

        Does not reach whether the import is guarded by an ``ImportError`` refusal
        (guide_corpus.py's own tests hold that).
        """
        packages = {package_name: package_dir for _, package_dir, package_name in _PYTHON_PACKAGES}
        problems: list[str] = []
        for package_name, modules in _DEVELOPMENT_ONLY_IMPORTS.items():
            package_dir = packages[package_name]
            pyproject = package_dir / "pyproject.toml"
            runtime = set(_declared_dependencies(pyproject))
            groups = tomllib.loads(pyproject.read_text(encoding="utf-8"))["dependency-groups"]
            development = {_requirement_name(r) for r in groups["dev"]}
            src_dir = package_dir / "src" / package_name
            for module, allowed in modules.items():
                distribution = _MODULE_TO_DISTRIBUTION[module]
                if distribution not in development:
                    problems.append(f"{package_name}: {distribution} is not in the dev group")
                if distribution in runtime:
                    problems.append(f"{package_name}: {distribution} is a runtime dependency; drop the entry")
                for source in sorted(src_dir.rglob("*.py"), key=lambda path: path.as_posix()):
                    relative = source.relative_to(src_dir).as_posix()
                    outside, in_functions = _imports_by_scope(source)
                    if module in outside:
                        problems.append(f"{package_name}/{relative} imports {module} outside a function")
                    if relative in allowed and module not in in_functions:
                        problems.append(f"{package_name}/{relative} no longer imports {module}; drop it")
                    if relative not in allowed and module in in_functions:
                        problems.append(f"{package_name}/{relative} imports {module} and is not named")
        assert not problems, problems

    def test_module_map_covers_every_declared_dependency(self) -> None:
        """A new dependency must be added to _MODULE_TO_DISTRIBUTION, or the audit fails far from the change."""
        known = {value.lower() for value in _MODULE_TO_DISTRIBUTION.values()}
        missing = set()
        for _, package_dir, _ in _PYTHON_PACKAGES:
            missing |= set(_declared_dependencies(package_dir / "pyproject.toml")) - known
        assert not missing, f"Declared dependencies absent from _MODULE_TO_DISTRIBUTION: {sorted(missing)}"

    def test_agent_spawns_the_mcp_server_from_the_same_interpreter(self) -> None:
        """The agent→mcp edge is a subprocess spawn the ast audit cannot see, so it is held here."""
        source = _MCP_CLIENT.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(_MCP_CLIENT))

        spawns = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "StdioServerParameters"
        ]
        assert spawns, "swcraft_agent.mcp_client must construct StdioServerParameters"

        keywords = {kw.arg: kw.value for kw in spawns[0].keywords}
        command = keywords.get("command")
        assert isinstance(command, ast.Attribute) and command.attr == "executable", (
            "the MCP server must be spawned from sys.executable — the same interpreter "
            "the agent is installed into, which is why swcraft-mcp is a real dependency"
        )
        args = keywords.get("args")
        assert isinstance(args, ast.List)
        assert [element.value for element in args.elts] == ["-m", "swcraft_mcp.server"]


class TestTheTypeAliasTypeApiTheTypingExtensionsFloorIsDeclaredFor:
    """The declared floor (the release that introduced `TypeAliasType`) and the API it exists for.

    A runtime test proves something only about the installed version, so floor, installed
    version and API are asserted separately. No ceiling, deliberately.

    **Does not reach:** every version the range admits; only the installed one is measured.
    """

    def test_the_declared_floor_is_the_release_that_introduced_the_api(self) -> None:
        declared = _declared_dependencies(_TOOLS_PYPROJECT)

        assert declared.get("typing-extensions") == "typing-extensions>=4.6", declared

    def test_the_installed_typing_extensions_satisfies_the_floor(self) -> None:
        from importlib.metadata import version

        installed = tuple(int(part) for part in version("typing_extensions").split(".")[:2] if part.isdigit())
        assert installed >= (4, 6), installed

    def test_the_backport_answers_the_forward_reference_form_schema_uses(self) -> None:
        """A renamed or dropped `TypeAliasType` fails here, not on a schema test blaming the model."""
        from typing_extensions import TypeAliasType

        # `InitValue` names itself, so the forward reference is the point; F821 because
        # the alias, not this scope, resolves the name.
        alias = TypeAliasType("Probe", "int | list[Probe]")  # noqa: F821
        assert alias.__name__ == "Probe"
        assert schema.InitValue.__name__ == "InitValue"
        assert isinstance(schema.InitValue, TypeAliasType)


# ---------------------------------------------------------------------------
# A removed dependency stays removed
# ---------------------------------------------------------------------------


class TestAutosarIsGone:
    """The ``cogu``/``autosar`` library, replaced by the first-party ARXML writer, stays gone.

    Both halves: nothing imports it, and nothing declares it (an installed package would let
    a re-added import pass).
    """

    def test_no_module_imports_autosar(self) -> None:
        offenders = {}
        for root in _AUTOSAR_AUDIT_ROOTS:
            sites = _import_sites(root, "autosar")
            if sites:
                offenders[root.name] = sites
        assert not offenders, (
            f"`autosar` is imported again — it is replaced by the "
            f"first-party writer in swcraft_tools.writer.arxml_xml/arxml_generation: {offenders}"
        )

    def test_the_audit_covers_every_package_source_and_test_tree(self) -> None:
        """Bookkeeping: a root that no longer exists would silently scan nothing."""
        for root in _AUTOSAR_AUDIT_ROOTS:
            assert root.is_dir(), f"{root} is not a directory — the import audit would scan nothing"

        covered = {root.resolve() for root in _AUTOSAR_AUDIT_ROOTS}
        for _, package_dir, package_name in _PYTHON_PACKAGES:
            assert (package_dir / "src" / package_name).resolve() in covered, package_dir.name
            assert (package_dir / "tests").resolve() in covered, package_dir.name

    def test_autosar_is_absent_from_every_declared_dependency(self) -> None:
        offenders = {
            package_dir.name: sorted(_declared_distributions(package_dir / "pyproject.toml"))
            for _, package_dir, _ in _PYTHON_PACKAGES
            if "autosar" in _declared_distributions(package_dir / "pyproject.toml")
        }
        assert not offenders, f"`autosar` is a declared dependency again: {offenders}"

    def test_the_import_audit_ignores_prose_but_catches_both_import_forms(self, tmp_path: Path) -> None:
        """Prove the guard fires — and that the prose exemption is not a blind spot."""
        (tmp_path / "prose.py").write_text(
            '"""cogu artifact inventory row 1 — the autosar library is named, not imported."""\n',
            encoding="utf-8",
        )
        (tmp_path / "plain.py").write_text("import autosar.datatype\n", encoding="utf-8")
        (tmp_path / "from_form.py").write_text("from autosar import workspace\n", encoding="utf-8")

        sites = _import_sites(tmp_path, "autosar")

        assert len(sites) == 2, sites
        assert not any("prose.py" in site for site in sites)


# ---------------------------------------------------------------------------
# --version flags
# ---------------------------------------------------------------------------


class TestVersionFlags:
    def test_swcraft_version_flag(self, monkeypatch, capsys) -> None:
        from swcraft_tools.cli import main

        monkeypatch.setattr("sys.argv", ["swcraft", "--version"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        assert _version_module_value(_VERSION_MODULE) in capsys.readouterr().out

    def test_the_version_line_is_shaped_the_way_the_extension_reads_it(self, monkeypatch, capsys) -> None:
        """`swcraft --version`'s output obeys `toolchain.ts`'s `parseSwcraftVersion` rule.

        The extension reads "the **last** whitespace token of the **first non-empty line**";
        an output like `swcraft 0.7.0 (abcdef)` would make every healthy installation look
        unreadable. This binds the CLI's output shape; the regex and the outputs the extension
        must refuse are `vscode_extension/src/toolchain.test.ts`'s.

        Does **not** reach the extension: nothing here runs `parseSwcraftVersion`, so a drift
        of the implementation from its documented rule goes unnoticed.
        """
        from swcraft_tools.cli import main

        monkeypatch.setattr("sys.argv", ["swcraft", "--version"])
        with pytest.raises(SystemExit):
            main()
        output = capsys.readouterr().out

        first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
        assert first_line.split()[-1] == _version_module_value(_VERSION_MODULE), (
            f"`swcraft --version` prints {first_line!r}; vscode_extension/src/toolchain.ts's "
            "parseSwcraftVersion() takes the last whitespace token of the first non-empty line, so "
            "this shape makes the extension report a healthy toolchain as unreadable"
        )

    def test_swcraft_agent_version_flag_via_subprocess(self) -> None:
        """`swcraft-agent --version`, out of process: the agent is not a swcraft-tools dependency.

        Skipped where only swcraft-tools is installed.
        """
        probe = subprocess.run(
            [sys.executable, "-c", "import swcraft_agent"],
            capture_output=True,
            check=False,
        )
        if probe.returncode != 0:
            pytest.skip("swcraft_agent is not installed in this interpreter")

        result = subprocess.run(
            [sys.executable, "-m", "swcraft_agent.cli", "--version"],
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
        assert _version_module_value(_VERSION_MODULE) in result.stdout


# ---------------------------------------------------------------------------
# Error cases — prove the guards actually fire
# ---------------------------------------------------------------------------


class TestGuardsFire:
    def test_mismatched_version_site_is_detected(self, tmp_path: Path) -> None:
        """A pyproject copy whose version drifted is reported by the helper, not ignored."""
        drifted = tmp_path / "pyproject.toml"
        drifted.write_text(
            _MCP_PYPROJECT.read_text(encoding="utf-8").replace(
                f'version = "{_version_module_value(_VERSION_MODULE)}"',
                'version = "9.9.9"',
                1,
            ),
            encoding="utf-8",
        )
        assert _pyproject_version(drifted) == "9.9.9"
        assert _pyproject_version(drifted) != _version_module_value(_VERSION_MODULE)

    def test_dynamic_version_without_hatch_path_is_rejected(self, tmp_path: Path) -> None:
        broken = tmp_path / "pyproject.toml"
        broken.write_text(
            '[project]\nname = "x"\ndynamic = ["version"]\n',
            encoding="utf-8",
        )
        with pytest.raises(KeyError):
            _pyproject_version(broken)

    def test_missing_version_declaration_is_rejected(self, tmp_path: Path) -> None:
        broken = tmp_path / "pyproject.toml"
        broken.write_text('[project]\nname = "x"\n', encoding="utf-8")
        with pytest.raises(AssertionError, match="neither a static nor a dynamic version"):
            _pyproject_version(broken)

    def test_import_audit_reports_an_undeclared_direct_import(self, tmp_path: Path) -> None:
        """Feed the audit a synthetic package that imports yaml without declaring it."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "fake"\nversion = "0.0.0"\ndependencies = ["pydantic>=2.0"]\n',
            encoding="utf-8",
        )
        src = tmp_path / "src" / "fake_pkg"
        src.mkdir(parents=True)
        (src / "mod.py").write_text("import os\nimport yaml\nimport pydantic\n", encoding="utf-8")

        assert _undeclared_imports(pyproject, src, "fake_pkg") == {"yaml"}

    def test_import_audit_accepts_a_fully_declared_package(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "fake"\nversion = "0.0.0"\ndependencies = ["pyyaml>=6.0"]\n',
            encoding="utf-8",
        )
        src = tmp_path / "src" / "fake_pkg"
        src.mkdir(parents=True)
        (src / "mod.py").write_text("import yaml\nimport fake_pkg.other\n", encoding="utf-8")

        assert _undeclared_imports(pyproject, src, "fake_pkg") == set()

    def test_import_audit_refuses_an_unmapped_module(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "fake"\nversion = "0.0.0"\ndependencies = []\n',
            encoding="utf-8",
        )
        src = tmp_path / "src" / "fake_pkg"
        src.mkdir(parents=True)
        (src / "mod.py").write_text("import totally_unknown_module\n", encoding="utf-8")

        with pytest.raises(AssertionError, match="_MODULE_TO_DISTRIBUTION"):
            _undeclared_imports(pyproject, src, "fake_pkg")
