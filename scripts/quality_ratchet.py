"""The quality ratchets ``lint`` runs: numbers that may only go down.

``check`` measures the tree and fails when a number grew; ``update`` rewrites
``scripts/quality_baseline.json`` downward only (lowers values, drops stale entries, refuses any rise or
new entry); ``update --initial`` writes the first baseline, only when none exists; ``init <key>`` adds
one measured key (:data:`MEASURED_KEYS`) the baseline lacks and refuses one it holds. ``check`` and
``update`` refuse a baseline lacking a measured key and name ``init``. Needs Python 3.11 and radon. Every
message names the key, the baseline value and the new value.

**The metrics.** For each limited metric (:data:`LIMITS`) a listed entry may not grow and an unlisted
one may not cross the limit: function length and cyclomatic complexity (as radon's ``cc_visit`` reports
them, a closure its own entry) and module size over :data:`SOURCE_ROOTS`; test file size over
:data:`TEST_FILE_TREES` and :data:`EXTENSION_TEST_ROOT`; docstring length over the source files and the
measured Python test files. A key is ``<import module>::<qualname>`` (``<path>::<qualname>`` for a
script and a test file, ``::`` alone for a module docstring). Besides: **mypy errors** per package
(:data:`MYPY_PACKAGES`, :data:`MYPY_ARGUMENTS`), which may not grow, and a run mypy cannot finish is
refused; **line citations** (``file.py:NNN``) in :data:`CITATION_TREES`, a zero check; **the test
rule**, an allow-list that may only shrink of product tests that require an exact phrase of
documentation, a docstring, a comment or a source; and **private crossings**, a list that may only
shrink of product imports of another product module's private name (:func:`measure_private_crossings`).

**Limits.** Test code is held by file length only. A symbol renamed while over a limit fails as a new entry (a
rename cannot be told from a new giant), fixed by a reviewed baseline edit that keeps the old value, never a
higher one; two definitions of one qualname in one module are one entry holding the larger value. A lowered
value still over its limit may grow back to the baseline's until ``update`` records it. The mypy figure is a
count, not a list. The zero check reads only the written ``file.py:NNN`` grammar and its backticked orphan
form. The test-rule scanner misses a fixture's text, an assertion inside a helper, a phrase built at run time
and the TypeScript tests; it is a static heuristic over common shapes: a test built to evade it is a review
matter.
"""

from __future__ import annotations

import argparse
import ast
import functools
import itertools
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from radon.complexity import cc_visit
from radon.visitors import Class, ComplexityVisitor, Function

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The committed baseline, relative to the repository root.
BASELINE = "scripts/quality_baseline.json"

#: The roots whose functions and modules the size and complexity ratchets measure. A directory
#: named ``tests`` under them is not measured; a root inside a ``tests`` directory (the specimen
#: generator's scenario runner) is.
SOURCE_ROOTS = (
    "packages/tools/src",
    "packages/mcp/src",
    "packages/agent/src",
    "packages/specimens/src",
    "packages/specimens/tests/scenarios",
    "scripts",
)

#: The roots under which a module is keyed by its import name rather than its path.
PACKAGE_ROOTS = ("packages/tools/src", "packages/mcp/src", "packages/agent/src", "packages/specimens/src")

#: The limit of each size and complexity metric: the target, and the line an unlisted entry may
#: not cross.
LIMITS = {
    "function_length": 200,
    "complexity": 30,
    "module_lines": 3000,
    "test_file_lines": 3000,
    "docstring_lines": 30,
}

#: The baseline's keys a measurement fills: what ``init`` may create and ``check`` requires.
MEASURED_KEYS = (*LIMITS, "mypy", "test_rule", "private_crossings")

#: The trees whose Python files the test-file ratchet measures; ``packages/*/tests`` is a pattern,
#: so a new package's tests are measured without an edit here. Wider than :data:`TEST_TREES`: it adds
#: the closed test tree.
TEST_FILE_TREES = ("packages/*/tests", "scripts/tests", "internal/tests")

#: The extension's sources, whose ``*.test.ts`` files the test-file ratchet measures.
EXTENSION_TEST_ROOT = "vscode_extension/src"

#: The test trees the open copy does not hold: an entry under one is kept, unmeasured, where it is absent.
CLOSED_TEST_TREES = ("internal/tests",)

#: The packages whose mypy errors are counted, each with its project's source directory.
MYPY_PACKAGES = {
    "swcraft_tools": "packages/tools/src",
    "swcraft_mcp": "packages/mcp/src",
    "swcraft_agent": "packages/agent/src",
}

#: mypy's arguments, stored in the baseline so a count is compared only with one taken the same
#: way. ``--config-file=`` reads no configuration file, so neither a host's user configuration
#: nor a project's ``[tool.mypy]`` table changes the count. ``--no-incremental`` reads no cache, and
#: :func:`run_mypy` adds ``--cache-dir`` naming a fresh temporary directory, so the count is taken
#: cold and never depends on what an earlier run left in a cache.
MYPY_ARGUMENTS = (
    "--config-file=",
    "--ignore-missing-imports",
    "--platform",
    "linux",
    "--python-version",
    "3.11",
    "--no-incremental",
)

#: ``MYPYPATH`` for every count: the source directories, so a package resolves to its source
#: and never to an editable install, which mypy refuses without a ``py.typed`` marker.
MYPY_PATH = ("packages/tools/src", "packages/mcp/src", "packages/agent/src", "packages/specimens/src")

#: Output that means mypy did not finish checking: refused, never counted.
MYPY_REFUSALS = ("errors prevented further checking", "missing py.typed marker")

#: The trees whose tracked text files the line-citation zero check reads.
CITATION_TREES = (
    "packages/tools",
    "packages/mcp",
    "packages/agent",
    "packages/specimens",
    "scripts",
    "examples",
    "docs/guide",
)

#: The product test trees the test-rule scanner reads, and the scripts' tests.
TEST_TREES = (
    "packages/tools/tests",
    "packages/mcp/tests",
    "packages/agent/tests",
    "packages/specimens/tests",
    "scripts/tests",
)

#: The retired line grammar: a module path, a colon and a line number, or a range of two.
LINE_CITATION = re.compile(r"(?<![\w/.])((?:[\w.]+/)*[\w.]+\.py):(\d+)(?:-(\d+))?")

#: The orphan companion: a backticked line number with no module, inheriting one from the prose.
ORPHAN_CITATION = re.compile(r"(?P<tick>`{1,2}):(?P<first>\d+)(?P<rest>(?:\s*[-‐-―]\s*\d+)?)(?P=tick)")

#: A file named in the prose: what an orphan would inherit its module from.
FILE_MENTION = re.compile(r"(?<![\w/.])(?P<module>(?:[\w.]+/)*[\w.]+\.(?:py|json|md|ya?ml|c4|rst|arxml|xsd))")


class RatchetError(Exception):
    """A run that cannot produce a verdict: a refused mypy run, a missing or unusable baseline."""


# ---------------------------------------------------------------------------
# Size and complexity
# ---------------------------------------------------------------------------


def source_files(root: Path) -> list[Path]:
    """Every ``.py`` file under :data:`SOURCE_ROOTS`, outside any ``tests`` directory."""
    files: list[Path] = []
    for source_root in SOURCE_ROOTS:
        base = root / source_root
        if not base.is_dir():
            continue
        files.extend(
            path
            for path in base.rglob("*.py")
            if "tests" not in path.relative_to(base).parts and "__pycache__" not in path.parts
        )
    return sorted(set(files), key=lambda path: path.as_posix())


def module_key(root: Path, path: Path) -> str:
    """The import name of a package module, or the repository-relative path of a script."""
    relative = path.relative_to(root).as_posix()
    for package_root in PACKAGE_ROOTS:
        prefix = f"{package_root}/"
        if relative.startswith(prefix):
            parts = relative[len(prefix) : -len(".py")].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            return ".".join(parts)
    return relative


def _keep_larger(table: dict[str, int], key: str, value: int) -> None:
    table[key] = max(value, table.get(key, value))


def measure_sources(root: Path) -> dict[str, dict[str, int]]:
    """Every function's length and complexity and every module's size, keyed as the baseline keys them.

    radon lists a top-level class's methods twice -- flat, each with its ``classname``, and again
    inside the class -- and a nested class's methods only inside it (``inner_classes``), so every
    method is taken from its class, walked down every nested class, and the flat methods are skipped.
    radon drops a class defined inside a function; :class:`_LocalClasses` takes it from the AST.
    """
    lengths: dict[str, int] = {}
    complexity: dict[str, int] = {}
    module_lines: dict[str, int] = {}
    for path in source_files(root):
        text = path.read_text(encoding="utf-8")
        module = module_key(root, path)
        module_lines[module] = len(text.splitlines())
        local_classes = _LocalClasses(ast.parse(text, filename=path.as_posix()))
        for qualname, block in local_classes.functions(cc_visit(text)):
            key = f"{module}::{qualname}"
            _keep_larger(lengths, key, local_classes.endline(block) - block.lineno + 1)
            _keep_larger(complexity, key, block.complexity)
    return {"function_length": lengths, "complexity": complexity, "module_lines": module_lines}


def measured_test_files(root: Path) -> list[Path]:
    """Every file the test-file ratchet measures: :data:`TEST_FILE_TREES`' Python, the extension's tests."""
    modules = set(source_files(root))
    files = [
        path
        for pattern in TEST_FILE_TREES
        for base in root.glob(pattern)
        for path in base.rglob("*.py")
        if "__pycache__" not in path.parts and path not in modules
    ]
    extension = root / EXTENSION_TEST_ROOT
    if extension.is_dir():
        files.extend(extension.rglob("*.test.ts"))
    return sorted(set(files), key=lambda path: path.as_posix())


def measure_test_files(root: Path) -> dict[str, int]:
    """Every measured test file's physical lines, keyed by its repository-relative POSIX path."""
    return {
        path.relative_to(root).as_posix(): len(path.read_text(encoding="utf-8").splitlines())
        for path in measured_test_files(root)
    }


_Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _docstring_lines(node: ast.Module | _Definition) -> int | None:
    """The physical lines of *node*'s docstring, or ``None`` when its first statement is no string."""
    first = node.body[0] if node.body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        return (first.end_lineno or first.lineno) - first.lineno + 1
    return None


def docstring_lengths(tree: ast.Module) -> Iterator[tuple[str, int]]:
    """``(qualname, lines)`` of every module, class and function docstring; the module's qualname is empty.

    A qualname is built as :class:`_LocalClasses` builds a function's: ``Class.method``,
    ``outer.<locals>.inner``, ``outer.<locals>.Class.method``.
    """
    module = _docstring_lines(tree)
    if module is not None:
        yield "", module

    def walk(node: ast.AST, prefix: str) -> Iterator[tuple[str, int]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _Definition):
                qualname = f"{prefix}{child.name}"
                lines = _docstring_lines(child)
                if lines is not None:
                    yield qualname, lines
                inner = f"{qualname}." if isinstance(child, ast.ClassDef) else f"{qualname}.<locals>."
                yield from walk(child, inner)
            elif not isinstance(child, ast.Lambda):
                yield from walk(child, prefix)

    yield from walk(tree, "")


def docstring_files(root: Path) -> list[tuple[Path, str]]:
    """Every file the docstring ratchet reads, with its module key: the sources, then the open test files."""
    files = [(path, module_key(root, path)) for path in source_files(root)]
    for path in measured_test_files(root):
        relative = path.relative_to(root).as_posix()
        if path.suffix == ".py" and not any(relative.startswith(f"{tree}/") for tree in CLOSED_TEST_TREES):
            files.append((path, relative))
    return files


def measure_docstrings(root: Path) -> dict[str, int]:
    """Every docstring's physical lines, keyed ``<module key>::<qualname>`` (``<module key>::`` for a module)."""
    lengths: dict[str, int] = {}
    for path, module in docstring_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
        for qualname, lines in docstring_lengths(tree):
            _keep_larger(lengths, f"{module}::{qualname}", lines)
    return lengths


class _LocalClasses:
    """radon's functions of one module, with the classes radon drops: those defined inside a function.

    A local class's methods are entries keyed ``<enclosing qualname>.<locals>.<Class>.<method>``, and
    the enclosing function's ``endline`` is extended to the class's last method, as to a closure's.
    """

    def __init__(self, tree: ast.Module) -> None:
        self._definitions = {
            (node.lineno, node.col_offset): node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def of(self, function: Function) -> list[Class]:
        """The classes defined in *function*'s own body (not in a closure's), as radon measures a class."""
        node = self._definitions.get((function.lineno, function.col_offset))
        if node is None:
            return []
        classes: list[Class] = []
        pending: list[ast.AST] = list(node.body)
        while pending:
            child = pending.pop()
            if isinstance(child, ast.ClassDef):
                classes.extend(ComplexityVisitor.from_ast(child).classes)
            elif not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                pending.extend(ast.iter_child_nodes(child))
        return sorted(classes, key=lambda block: block.lineno)

    def endline(self, function: Function) -> int:
        """radon's ``endline``, extended to the last line of the function's last closure or local class."""
        return max(
            (
                function.endline,
                *(self.endline(closure) for closure in function.closures),
                *(self._class_endline(block) for block in self.of(function)),
            )
        )

    def _class_endline(self, block: Class) -> int:
        return max(
            (
                block.endline,
                *(self.endline(method) for method in block.methods),
                *(self._class_endline(inner) for inner in block.inner_classes),
            )
        )

    def functions(self, blocks: Iterable[object]) -> Iterator[tuple[str, Function]]:
        """Every module-level function and every method of every class, nested and local ones and closures included."""
        for block in blocks:
            if isinstance(block, Class):
                yield from self._methods(block, block.name)
            elif isinstance(block, Function) and not block.is_method:
                yield from self._with_closures(block, block.name)

    def _methods(self, block: Class, prefix: str) -> Iterator[tuple[str, Function]]:
        for method in block.methods:
            yield from self._with_closures(method, f"{prefix}.{method.name}")
        for inner in block.inner_classes:
            yield from self._methods(inner, f"{prefix}.{inner.name}")

    def _with_closures(self, function: Function, qualname: str) -> Iterator[tuple[str, Function]]:
        yield qualname, function
        for closure in function.closures:
            yield from self._with_closures(closure, f"{qualname}.<locals>.{closure.name}")
        for local in self.of(function):
            yield from self._methods(local, f"{qualname}.<locals>.{local.name}")


# ---------------------------------------------------------------------------
# mypy
# ---------------------------------------------------------------------------


#: One mypy run: its exit status and its combined output.
MypyRunner = Callable[[Path, str], tuple[int, str]]


def run_mypy(root: Path, package: str) -> tuple[int, str]:
    """Run mypy on *package* from *root* with :data:`MYPY_ARGUMENTS` and the pinned ``MYPYPATH``.

    The cache directory is a fresh temporary one, removed after the run: no cache is reused.
    Colour is off whatever the host sets: mypy 1.20 colours a piped run when ``FORCE_COLOR`` or
    ``MYPY_FORCE_COLOR`` is set, and the escape codes would hide the summary and error lines
    :func:`parse_mypy` reads. ``MYPY_FORCE_COLOR`` takes precedence over ``FORCE_COLOR``, so
    ``0`` disables both without touching :data:`MYPY_ARGUMENTS`.
    """
    environment = dict(os.environ)
    environment["MYPYPATH"] = os.pathsep.join((root / entry).as_posix() for entry in MYPY_PATH)
    environment["MYPY_FORCE_COLOR"] = "0"
    with tempfile.TemporaryDirectory(prefix="quality-ratchet-mypy-", ignore_cleanup_errors=True) as cache:
        completed = subprocess.run(
            (sys.executable, "-m", "mypy", *MYPY_ARGUMENTS, "--cache-dir", cache, "-p", package),
            cwd=root,
            env=environment,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    return completed.returncode, completed.stdout + completed.stderr


_MYPY_ERROR = re.compile(r"^(?P<path>(?:[A-Za-z]:)?[^\s:][^:]*):\d+(?::\d+)?: error:", re.MULTILINE)
_MYPY_FOUND = re.compile(r"^Found \d+ errors? in \d+ files? \(checked \d+ source files?\)$", re.MULTILINE)
_MYPY_SUCCESS = re.compile(r"^Success: no issues found in \d+ source files?$", re.MULTILINE)


def parse_mypy(package: str, source_dir: str, status: int, output: str) -> int:
    """The number of mypy errors in *package*'s own files, or :class:`RatchetError`.

    An error line counts only when its path lies under ``<source_dir>/<package>/`` -- relative, or
    absolute when mypy prints it so, compared case-insensitively for a Windows drive: a ``-p`` run
    follows imports into other packages, whose errors are theirs. A run whose output says it did
    not finish (:data:`MYPY_REFUSALS`), that exits with neither a summary line nor status 0 or 1,
    is refused.
    """
    for refusal in MYPY_REFUSALS:
        if refusal in output:
            raise RatchetError(f"mypy {package}: the run did not finish ({refusal!r}); no count is taken:\n{output}")
    if status == 0 and _MYPY_SUCCESS.search(output):
        return 0
    if status != 1 or not _MYPY_FOUND.search(output):
        raise RatchetError(f"mypy {package}: exit {status} without a summary line; no count is taken:\n{output}")
    own = f"{source_dir}/{package}/".casefold()
    return sum(1 for match in _MYPY_ERROR.finditer(output) if _is_own(match.group("path"), own))


def _is_own(path: str, own: str) -> bool:
    """Whether mypy's *path* lies under *own*, spelled relative or absolute, with either separator."""
    normal = path.replace("\\", "/").casefold()
    return normal.startswith(own) or f"/{own}" in normal


def measure_mypy(root: Path, runner: MypyRunner = run_mypy) -> dict[str, int]:
    """Every counted package's own mypy errors."""
    counts: dict[str, int] = {}
    for package, source_dir in MYPY_PACKAGES.items():
        status, output = runner(root, package)
        counts[package] = parse_mypy(package, source_dir, status, output)
    return counts


# ---------------------------------------------------------------------------
# Line citations
# ---------------------------------------------------------------------------


def _git_paths(root: Path, *pathspec: str) -> list[str]:
    completed = subprocess.run(("git", "ls-files", "-z", "--", *pathspec), cwd=root, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RatchetError(f"git ls-files failed in {root.as_posix()}: {completed.stderr.decode('utf-8', 'replace')}")
    return sorted(part for part in completed.stdout.decode("utf-8").split("\0") if part)


def tracked_citation_files(root: Path) -> tuple[list[Path], set[str]]:
    """The tracked files of :data:`CITATION_TREES`, and every tracked ``.py`` path of the repository."""
    files = [root / relative for relative in _git_paths(root, *CITATION_TREES)]
    return files, set(_git_paths(root, "*.py"))


def _text_of(path: Path) -> str | None:
    """*path*'s text, or ``None`` for a binary or non-UTF-8 file."""
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _resolves(module: str, modules: set[str]) -> bool:
    return any(known == module or known.endswith(f"/{module}") for known in modules)


def line_citations(text: str, modules: set[str]) -> list[str]:
    """Every line citation in *text* whose module names a tracked ``.py`` file of *modules*."""
    found: list[str] = []
    for match in LINE_CITATION.finditer(text):
        if text[match.end() : match.end() + 1] == ":":
            continue
        if _resolves(match.group(1), modules):
            found.append(match.group(0))
    for match in ORPHAN_CITATION.finditer(text):
        mentions = list(FILE_MENTION.finditer(text[: match.start()]))
        if not mentions or not mentions[-1].group("module").endswith(".py"):
            continue
        if text.count("\n", mentions[-1].start(), match.start()) > 1:
            continue
        found.append(match.group(0))
    return found


def measure_line_citations(root: Path, files: Iterable[Path], modules: set[str]) -> list[str]:
    """``<path>: <citation>`` for every line citation in *files*."""
    findings: list[str] = []
    for path in files:
        if not path.is_file():
            continue
        text = _text_of(path)
        if text is None:
            continue
        relative = path.relative_to(root).as_posix()
        findings.extend(f"{relative}: {citation}" for citation in line_citations(text, modules))
    return findings


# ---------------------------------------------------------------------------
# The test rule: what a test asserts
# ---------------------------------------------------------------------------

_STRING_METHODS = frozenset(
    {
        "lower",
        "upper",
        "casefold",
        "strip",
        "lstrip",
        "rstrip",
        "replace",
        "split",
        "splitlines",
        "rsplit",
        "partition",
        "rpartition",
        "removeprefix",
        "removesuffix",
        "expandtabs",
        "format",
        "translate",
        "title",
        "decode",
        "join",
    }
)
_PROPAGATING_CALLS = frozenset(
    {"str", "list", "tuple", "sorted", "set", "reversed", "dedent", "sub", "split", "normalize"}
    | {"enumerate", "filter", "frozenset", "dict"}
)
#: Calls that end a text's taint: static analysis or data loading.
_SINK_CALLS = frozenset(
    {"parse", "loads", "safe_load", "load", "load_yaml_document", "literal_eval", "compile", "walk"}
    | {"fromstring", "XML"}
)
_REGEX_CALLS = frozenset({"search", "match", "fullmatch", "findall", "finditer"})
_READ_METHODS = frozenset({"read_text", "read_bytes"})
_INSPECT_CALLS = frozenset({"getdoc", "getsource", "getsourcelines", "getsourcefile", "getcomments"})
_PROSE_SUFFIXES = (".md", ".rst", ".txt")
_TEXT_SUFFIXES = (".py", ".md", ".rst")
_TEMPORARY = ("tmp_path", "tmp_path_factory", "tmpdir")
#: The project directories a test may run from, so a relative path may start at one of them.
_PROJECTS = ("packages/tools", "packages/mcp", "packages/agent", "packages/specimens")
#: How much of a required phrase a finding quotes: enough to find it in the test, short enough
#: to keep the finding on one line.
_PHRASE_PREVIEW = 60
#: The fewest constant characters an f-string with an unknown value must hold to be read as a
#: phrase; shorter, its constant text is a separator such as ``": "`` and not a phrase.
_FSTRING_PHRASE_MINIMUM = 4
#: The most combinations of stacked ``parametrize`` tables scanned as their cross product; a larger
#: product is scanned one row of each table at a time, so the scan stays linear in what is written.
_COMBINATIONS = 256


def _leaf(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _string(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _literal_row(node: ast.AST) -> tuple[str | None, ...] | None:
    if _string(node) is not None:
        return (_string(node),)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(_string(element) for element in node.elts)
    if isinstance(node, ast.Call) and _leaf(node.func) == "param":
        return tuple(_string(element) for element in node.args)
    return None


def literal_table(node: ast.AST | None) -> list[tuple[str | None, ...]] | None:
    """The rows of a literal list or tuple of strings or of string rows (a parametrize table)."""
    if isinstance(node, ast.Call) and _leaf(node.func) in ("sorted", "tuple", "list", "frozenset", "set") and node.args:
        node = node.args[0]
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return None
    rows: list[tuple[str | None, ...]] = []
    for element in node.elts:
        row = _literal_row(element)
        if row is None:
            return None
        rows.append(row)
    return rows or None


def _string_elements(value: ast.AST) -> list[str] | None:
    if isinstance(value, ast.Call):
        if not value.args or not isinstance(value.args[0], (ast.Tuple, ast.List, ast.Set)):
            return None
        value = value.args[0]
    if not isinstance(value, (ast.Tuple, ast.List, ast.Set)):
        return None
    strings = [_string(element) for element in value.elts]
    if not strings or any(item is None for item in strings):
        return None
    return [item for item in strings if item is not None]


def _assignments(module: ast.Module) -> Iterator[tuple[str, ast.expr]]:
    for statement in module.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    yield target.id, statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            if isinstance(statement.target, ast.Name):
                yield statement.target.id, statement.value


def classify(path: str | None, repo: Path) -> str | None:
    """The text-source kind of a folded path (``docs``, ``docs-md``, ``source``), or ``None``."""
    if path is None or "<TMP>" in path:
        return None
    if path.startswith("<MODFILE:"):
        return "source"
    normal = path.replace(repo.as_posix(), "<REPO>")
    if normal.lower().endswith((".json", ".jsonl")):
        return None
    if "/docs/" in normal or normal.startswith("docs/"):
        return _classify_under_docs(normal)
    if not normal.endswith(_TEXT_SUFFIXES) or "/fixtures/" in normal:
        return None
    if "<REPO>" in normal or "<SRC>" in normal or normal.startswith("/") or _names_a_repository_entry(normal, repo):
        return "source" if normal.endswith(".py") else "docs-md"
    return None


def _names_a_repository_entry(path: str, repo: Path) -> bool:
    """Whether a relative *path* starts with an entry of the repository root or of a project directory.

    A test runs from the repository root or from its project's directory, so ``packages/tools/src/...``,
    ``src/swcraft_tools/...`` and ``README.md`` read the repository's own files.
    """
    if path.startswith("<") or ":" in path:
        return False
    first = path.removeprefix("./").split("/", 1)[0]
    if first in ("", ".", "..") or "*" in first:
        return False
    return _is_repository_entry(repo, first)


@functools.cache
def _is_repository_entry(repo: Path, name: str) -> bool:
    """Whether *name* is an entry of *repo* or of a project directory; a name no file system can hold is not."""
    try:
        return any((base / name).exists() for base in (repo, *(repo / project for project in _PROJECTS)))
    except (OSError, ValueError):  # a loop value that is a sentence, too long or holding a NUL
        return False


def _classify_under_docs(normal: str) -> str | None:
    if "/docs/guide/" in normal or normal.startswith("docs/guide/"):
        return None
    if normal.endswith(".py"):
        return "source"
    return "docs" if normal.endswith(_PROSE_SUFFIXES) else None


@dataclass
class _Helper:
    """A module-level function of a test module that returns a text source.

    *mode* ``read``: the text of a path built from parameter *index* (*payload* is the path
    template); ``fixed``: the text of one fixed source (*payload* is its kind); ``propagate``: a
    text derived from parameter *index*'s text.
    """

    mode: str
    index: int
    payload: str


@dataclass
class _ModuleContext:
    """What a scan of one test module knows: its constants, and the helpers it can see."""

    path: Path
    repo: Path
    helpers: dict[str, _Helper]
    global_constants: dict[str, str]
    constants: dict[str, str] = field(default_factory=dict)
    strings: dict[str, str] = field(default_factory=dict)
    string_tuples: dict[str, list[str]] = field(default_factory=dict)
    patterns: dict[str, str] = field(default_factory=dict)
    string_rows: dict[str, list[tuple[str | None, ...]]] = field(default_factory=dict)

    def learn(self, module: ast.Module) -> None:
        for name, value in _assignments(module):
            folded = self.fold(value, {})
            if folded is not None:
                self.constants[name] = folded
            text = _string(value)
            if text is not None:
                self.strings[name] = text
            elements = _string_elements(value)
            if elements:
                self.string_tuples[name] = elements
            table = literal_table(value)
            if table:
                self.string_rows[name] = table
            if isinstance(value, ast.Call) and _leaf(value.func) == "compile" and value.args:
                pattern = _string(value.args[0])
                if pattern is not None:
                    self.patterns[name] = pattern

    def fold(self, node: ast.AST | None, local: dict[str, str]) -> str | None:
        """A path expression folded into a symbolic string (``<REPO>/docs/x.md``, ``<TMP>/...``)."""
        if node is None:
            return None
        if _string(node) is not None:
            return _string(node)
        if isinstance(node, ast.Name):
            return self._fold_name(node.id, local)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left, right = self.fold(node.left, local), self.fold(node.right, local)
            return None if left is None else f"{left}/{right if right is not None else '*'}"
        if isinstance(node, ast.Subscript):
            return self._fold_parents(node, local)
        if isinstance(node, ast.Attribute):
            return self._fold_attribute(node, local)
        if isinstance(node, ast.Call):
            return self._fold_call(node, local)
        return None

    def _fold_name(self, name: str, local: dict[str, str]) -> str | None:
        if name in local:
            return local[name]
        if name in _TEMPORARY:
            return "<TMP>"
        if name == "__file__":
            return f"<FILE:{self.path.as_posix()}>"
        if name in self.constants:
            return self.constants[name]
        if name in self.string_tuples:
            return self.string_tuples[name][0]
        return self.global_constants.get(name)

    def _fold_parents(self, node: ast.Subscript, local: dict[str, str]) -> str | None:
        base = node.value
        if not (isinstance(base, ast.Attribute) and base.attr == "parents"):
            return None
        inner = self.fold(base.value, local)
        if not (inner and inner.startswith("<FILE:") and isinstance(node.slice, ast.Constant)):
            return None
        index = node.slice.value
        parents = Path(inner[len("<FILE:") : -1]).parents
        if not isinstance(index, int) or index >= len(parents):
            return None
        return parents[index].as_posix()

    def _fold_attribute(self, node: ast.Attribute, local: dict[str, str]) -> str | None:
        if node.attr == "__file__":
            return f"<MODFILE:{node.value.id}>" if isinstance(node.value, ast.Name) else None
        if node.attr != "parent":
            return None
        inner = self.fold(node.value, local)
        if not inner:
            return None
        if inner.startswith("<FILE:"):
            return Path(inner[len("<FILE:") : -1]).parent.as_posix()
        if inner.startswith("<MODFILE:"):
            return "<SRC>"
        return inner.rsplit("/", 1)[0]

    def _fold_call(self, node: ast.Call, local: dict[str, str]) -> str | None:
        leaf = _leaf(node.func)
        if leaf in ("Path", "PurePath", "PosixPath") and node.args:
            return self.fold(node.args[0], local)
        if leaf in ("resolve", "absolute") and isinstance(node.func, ast.Attribute):
            return self.fold(node.func.value, local)
        if leaf == "joinpath" and isinstance(node.func, ast.Attribute):
            base = self.fold(node.func.value, local)
            parts = [self.fold(argument, local) or "*" for argument in node.args]
            return "/".join([base, *parts]) if base else None
        if leaf == "files" and node.args:
            return "<SRC>"
        return None


class _FunctionScan:
    """One function's tainted names and the positive literal requirements made on them."""

    def __init__(
        self,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        context: _ModuleContext,
        class_decorators: tuple[ast.expr, ...] = (),
    ) -> None:
        self.function = function
        self.context = context
        #: The decorators of the ``Test*`` classes around the function: a class's ``parametrize``.
        self.class_decorators = class_decorators
        self.paths: dict[str, str] = {}
        self.taint: dict[str, str] = {}
        self.literals: dict[str, str] = dict(context.strings)
        self.hits: list[tuple[int, str, str]] = []

    # -- sources -----------------------------------------------------------

    def source_kind(self, node: ast.AST) -> str | None:
        """The kind of text source *node* produces or derives from, or ``None``."""
        if isinstance(node, ast.Name):
            return self.taint.get(node.id)
        if isinstance(node, ast.Attribute):
            return "docstring" if node.attr == "__doc__" else self.source_kind(node.value)
        if isinstance(node, ast.Subscript):
            return self.source_kind(node.value)
        if isinstance(node, ast.JoinedStr):
            return self._first_kind(value.value for value in node.values if isinstance(value, ast.FormattedValue))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._first_kind((node.left, node.right))
        if isinstance(node, ast.IfExp):
            return self._first_kind((node.body, node.orelse))
        if isinstance(node, ast.BoolOp):
            return self._first_kind(node.values)
        if isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
            return self._comprehension_kind(node)
        if isinstance(node, ast.Call):
            return self._call_kind(node)
        return None

    def _first_kind(self, nodes: Iterable[ast.AST]) -> str | None:
        for node in nodes:
            kind = self.source_kind(node)
            if kind:
                return kind
        return None

    def _comprehension_kind(self, node: ast.ListComp | ast.GeneratorExp | ast.SetComp) -> str | None:
        saved = dict(self.taint)
        for generator in node.generators:
            kind = self.source_kind(generator.iter)
            if kind:
                for name in ast.walk(generator.target):
                    if isinstance(name, ast.Name):
                        self.taint[name.id] = kind
        kind = self.source_kind(node.elt)
        self.taint = saved
        return kind

    def _call_kind(self, node: ast.Call) -> str | None:
        leaf = _leaf(node.func)
        if leaf in _SINK_CALLS:
            return None
        helper = self.context.helpers.get(leaf or "") if isinstance(node.func, ast.Name) else None
        if helper is not None:
            return self._helper_kind(helper, node)
        if isinstance(node.func, ast.Attribute):
            kind = self._method_kind(leaf, node)
            if kind is not None:
                return kind or None
        if leaf == "open" and isinstance(node.func, ast.Name) and node.args:
            return classify(self.path_of(node.args[0]), self.context.repo)
        if leaf in _INSPECT_CALLS:
            return "inspect"
        if leaf in _PROPAGATING_CALLS:
            kind = self._first_kind(node.args)
            if kind:
                return kind
            if leaf == "sub" and len(node.args) >= 3:
                return self.source_kind(node.args[2])
        return None

    def _method_kind(self, leaf: str | None, node: ast.Call) -> str | None:
        """A method call's kind; ``""`` when the method decides and the answer is no source."""
        assert isinstance(node.func, ast.Attribute)
        receiver = node.func.value
        if leaf in _READ_METHODS or leaf == "open":
            return classify(self.path_of(receiver), self.context.repo) or ""
        if leaf == "read":
            return self.source_kind(receiver) or ""
        if leaf in _INSPECT_CALLS:
            return "inspect"
        if leaf in _STRING_METHODS:
            kind = self.source_kind(receiver)
            if not kind and leaf == "join":
                kind = self._first_kind(node.args)
            return kind or ""
        return None

    def _helper_kind(self, helper: _Helper, node: ast.Call) -> str | None:
        if helper.mode == "fixed":
            return helper.payload
        if helper.index >= len(node.args):
            return None
        argument = node.args[helper.index]
        if helper.mode == "propagate":
            return self.source_kind(argument)
        path = self.path_of(argument)
        if path is None and isinstance(argument, ast.Name):
            path = self.literals.get(argument.id)
        if path is None:
            return None
        return classify(helper.payload.replace(f"<P{helper.index}>", path), self.context.repo)

    def path_of(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name) and node.id in self.paths:
            return self.paths[node.id]
        return self.context.fold(node, self.paths)

    # -- literals ----------------------------------------------------------

    def literal(self, node: ast.AST) -> str | None:
        """The literal phrase *node* stands for, or ``None``."""
        if _string(node) is not None:
            return _string(node)
        if isinstance(node, ast.Name):
            return self.literals.get(node.id) if node.id not in self.taint else None
        if isinstance(node, ast.Call):
            return self._call_literal(node)
        if isinstance(node, ast.JoinedStr):
            return self._fstring_literal(node)
        return None

    def _call_literal(self, node: ast.Call) -> str | None:
        leaf = _leaf(node.func)
        if isinstance(node.func, ast.Attribute) and leaf in _STRING_METHODS | {"escape"}:
            if leaf == "escape" and node.args:
                return self.literal(node.args[0])
            return self.literal(node.func.value)
        helper = self.context.helpers.get(leaf or "") if isinstance(node.func, ast.Name) else None
        if helper is not None and helper.mode == "propagate" and helper.index < len(node.args):
            return self.literal(node.args[helper.index])
        if leaf == "str" and node.args:
            return self.literal(node.args[0])
        return None

    def _fstring_literal(self, node: ast.JoinedStr) -> str | None:
        inner = [self.literal(value.value) for value in node.values if isinstance(value, ast.FormattedValue)]
        if inner and all(item is not None for item in inner):
            return "".join(value.value if isinstance(value, ast.Constant) else "{}" for value in node.values)
        joined = "".join(value.value for value in node.values if isinstance(value, ast.Constant))
        return joined if len(joined) >= _FSTRING_PHRASE_MINIMUM else None

    def literal_iter(self, node: ast.AST) -> list[str] | None:
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return _string_elements(node)
        if isinstance(node, ast.Name):
            return self.context.string_tuples.get(node.id)
        return None

    def pattern(self, node: ast.AST) -> str | None:
        literal = self.literal(node)
        if literal is None and isinstance(node, ast.Name):
            return self.context.patterns.get(node.id)
        return literal

    # -- statements --------------------------------------------------------

    def bind(self, target: ast.AST, value: ast.AST) -> None:
        kind = self.source_kind(value)
        path = self.context.fold(value, self.paths)
        literal = self.literal(value)
        for node in ast.walk(target):
            if not isinstance(node, ast.Name):
                continue
            if kind:
                self.taint[node.id] = kind
            else:
                self.taint.pop(node.id, None)
            if not isinstance(target, ast.Name):
                continue
            if path is not None:
                self.paths[node.id] = path
            if literal is not None and not kind:
                self.literals[node.id] = literal
            else:
                self.literals.pop(node.id, None)

    def visit_body(self, body: list[ast.stmt]) -> None:
        for statement in body:
            self.visit(statement)

    def visit_branches(self, branches: list[list[ast.stmt]], bindings: list[dict[str, str]] | None = None) -> None:
        """Visit alternative *branches* from one state and merge what they leave.

        *bindings*: per branch, names bound to a literal and a path before it (a loop's values).
        A name is a text source after the branches when any branch left it one, a path when any
        branch gave it one -- a text source over a repository-rooted directory over any other path,
        the first branch's on a tie -- and a literal only when every branch left it the same literal.
        """
        start = (dict(self.taint), dict(self.paths), dict(self.literals))
        ends: list[tuple[dict[str, str], dict[str, str], dict[str, str]]] = []
        for index, branch in enumerate(branches):
            self.taint, self.paths, self.literals = (dict(table) for table in start)
            for name, value in (bindings[index] if bindings else {}).items():
                self.taint.pop(name, None)
                self.literals[name] = self.paths[name] = value
            self.visit_body(branch)
            ends.append((self.taint, self.paths, self.literals))
        self.taint = {name: kind for taint, _, _ in reversed(ends) for name, kind in taint.items()}
        self.paths = {}
        for _, paths, _ in ends:
            for name, path in paths.items():
                kept = self.paths.get(name)
                if kept is None or self._path_rank(path) > self._path_rank(kept):
                    self.paths[name] = path
        first = ends[0][2]
        self.literals = {
            name: value
            for name, value in first.items()
            if name not in self.taint and all(literals.get(name) == value for _, _, literals in ends)
        }

    def _path_rank(self, path: str) -> int:
        """2 for a text source, 1 for a path rooted in the repository but not yet a source, else 0."""
        if classify(path, self.context.repo):
            return 2
        if "<TMP>" in path:
            return 0
        rooted = path.startswith(("<FILE:", "<MODFILE:", "<SRC>", self.context.repo.as_posix()))
        return 1 if rooted or _names_a_repository_entry(path, self.context.repo) else 0

    def visit(self, statement: ast.stmt) -> None:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                self.bind(target, statement.value)
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            self.bind(statement.target, statement.value)
        elif isinstance(statement, ast.AugAssign):
            kind = self.source_kind(statement.value)
            if kind and isinstance(statement.target, ast.Name):
                self.taint[statement.target.id] = kind
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            self._visit_for(statement)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            self._visit_with(statement)
        elif isinstance(statement, ast.Assert):
            self.check(statement.test, statement.lineno, positive=True)
        elif isinstance(statement, ast.If):
            if any(_is_failure(item) for item in statement.body):
                self.check(statement.test, statement.lineno, positive=False)
            self.visit_branches([statement.body, statement.orelse])
        elif isinstance(statement, ast.While):
            self.visit_body(statement.body)
            self.visit_body(statement.orelse)
        elif isinstance(statement, ast.Match):
            self.visit_branches([*(case.body for case in statement.cases), []])
        elif isinstance(statement, (ast.Try, ast.TryStar)):
            self.visit_body(statement.body)
            self.visit_branches([statement.orelse, *(handler.body for handler in statement.handlers)])
            self.visit_body(statement.finalbody)
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            arguments = statement.value.args
            if _leaf(statement.value.func) == "assertIn" and len(arguments) >= 2:
                self.require_in(arguments[0], arguments[1], statement.lineno)

    def _visit_for(self, statement: ast.For | ast.AsyncFor) -> None:
        kind = self.source_kind(statement.iter)
        literals = self.literal_iter(statement.iter)
        if literals and isinstance(statement.target, ast.Name):
            # every literal value, as a parametrize row: the body once per value, the ends merged
            name = statement.target.id
            self.visit_branches([statement.body] * len(literals), [{name: value} for value in literals])
            self.visit_body(statement.orelse)
            return
        for node in ast.walk(statement.target):
            if isinstance(node, ast.Name) and kind:
                self.taint[node.id] = kind
        iterable = statement.iter
        if (
            isinstance(iterable, ast.Call)
            and isinstance(iterable.func, ast.Attribute)
            and iterable.func.attr in ("glob", "rglob", "iterdir")
            and isinstance(statement.target, ast.Name)
        ):
            base = self.context.fold(iterable.func.value, self.paths)
            if base is not None:
                pattern = _string(iterable.args[0]) if iterable.args else None
                self.paths[statement.target.id] = f"{base}/{pattern or '*'}"
        self.visit_body(statement.body)
        self.visit_body(statement.orelse)

    def _visit_with(self, statement: ast.With | ast.AsyncWith) -> None:
        for item in statement.items:
            call = item.context_expr
            if not (isinstance(item.optional_vars, ast.Name) and isinstance(call, ast.Call)):
                continue
            if _leaf(call.func) != "open":
                continue
            if isinstance(call.func, ast.Attribute):
                opened: ast.AST | None = call.func.value
            else:
                opened = call.args[0] if call.args else None
            kind = classify(self.path_of(opened), self.context.repo) if opened is not None else None
            if kind:
                self.taint[item.optional_vars.id] = kind
        self.visit_body(statement.body)

    # -- requirements ------------------------------------------------------

    def require_in(self, needle: ast.AST, haystack: ast.AST, line: int) -> None:
        literal = self.literal(needle)
        kind = self.source_kind(haystack)
        if literal is not None and kind:
            self.hits.append((line, kind, f"in: {literal[:_PHRASE_PREVIEW]!r}"))

    def check(self, test: ast.expr, line: int, *, positive: bool) -> None:
        """Record *test* when it requires a literal of a text source (*positive*: as asserted)."""
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            self.check(test.operand, line, positive=not positive)
        elif isinstance(test, ast.BoolOp):
            if isinstance(test.op, ast.And if positive else ast.Or):
                for value in test.values:
                    self.check(value, line, positive=positive)
        elif isinstance(test, ast.Call) and _leaf(test.func) in ("all", "any") and test.args:
            self._check_quantifier(test.args[0], line, positive=positive)
        elif not positive:
            # a comparison guarding a failure requires its negation: `x not in text` requires `x in text`
            if isinstance(test, ast.Compare) and len(test.ops) == 1 and type(test.ops[0]) in _NEGATED:
                negated = _NEGATED[type(test.ops[0])]()
                self._check_compare(negated, test.left, test.comparators[0], line)
        elif isinstance(test, ast.Compare) and len(test.ops) == 1:
            self._check_compare(test.ops[0], test.left, test.comparators[0], line)
        elif isinstance(test, ast.Call):
            self._check_call(test, line)

    def _check_quantifier(self, argument: ast.expr, line: int, *, positive: bool) -> None:
        if not isinstance(argument, (ast.GeneratorExp, ast.ListComp)):
            return
        saved_literals, saved_taint = dict(self.literals), dict(self.taint)
        for generator in argument.generators:
            literals = self.literal_iter(generator.iter)
            kind = self.source_kind(generator.iter)
            if isinstance(generator.target, ast.Name):
                if literals:
                    self.literals[generator.target.id] = literals[0]
                if kind:
                    self.taint[generator.target.id] = kind
        self.check(argument.elt, line, positive=positive)
        self.literals, self.taint = saved_literals, saved_taint

    def _check_compare(self, operator: ast.cmpop, left: ast.expr, right: ast.expr, line: int) -> None:
        if isinstance(operator, ast.In):
            self.require_in(left, right, line)
            return
        if isinstance(operator, (ast.Eq, ast.Is)):
            for text, phrase in ((left, right), (right, left)):
                literal = self.literal(phrase)
                kind = self.source_kind(text)
                if literal is not None and kind and not _is_count_or_len(text):
                    self.hits.append((line, kind, f"==: {literal[:_PHRASE_PREVIEW]!r}"))
                    return
        for count, other, swapped in ((left, right, False), (right, left, True)):
            if _requires_a_match(operator, other, swapped=swapped):
                self._check_count(count, line)
        if isinstance(operator, (ast.IsNot, ast.NotEq)) and isinstance(right, ast.Constant) and right.value is None:
            self._check_regex(left, line)

    def _check_call(self, test: ast.Call, line: int) -> None:
        leaf = _leaf(test.func)
        if leaf in ("startswith", "endswith") and isinstance(test.func, ast.Attribute) and test.args:
            literal = self.literal(test.args[0])
            kind = self.source_kind(test.func.value)
            if literal is not None and kind:
                self.hits.append((line, kind, f"{leaf}: {literal[:_PHRASE_PREVIEW]!r}"))
        elif leaf == "count":
            self._check_count(test, line)  # a truthy count requires a match, as `count(...) > 0` does
        elif leaf in ("bool", "len", "list"):
            for argument in test.args:
                self.check(argument, line, positive=True)
        else:
            self._check_regex(test, line)

    def _check_count(self, node: ast.AST, line: int) -> None:
        if not isinstance(node, ast.Call):
            return
        if _leaf(node.func) == "count" and isinstance(node.func, ast.Attribute) and node.args:
            literal = self.literal(node.args[0])
            kind = self.source_kind(node.func.value)
            if literal is not None and kind:
                self.hits.append((line, kind, f"count: {literal[:_PHRASE_PREVIEW]!r}"))
        if _leaf(node.func) == "len" and node.args:
            self._check_regex(node.args[0], line)

    def _check_regex(self, node: ast.AST, line: int) -> None:
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            return
        leaf = node.func.attr
        if leaf not in _REGEX_CALLS:
            return
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "re":
            if len(node.args) >= 2:
                pattern, kind = self.pattern(node.args[0]), self.source_kind(node.args[1])
                if pattern is not None and kind:
                    self.hits.append((line, kind, f"re.{leaf}: {pattern[:_PHRASE_PREVIEW]!r}"))
        elif node.args:
            pattern, kind = self.pattern(node.func.value), self.source_kind(node.args[0])
            if pattern is not None and kind:
                self.hits.append((line, kind, f"pattern.{leaf}: {pattern[:_PHRASE_PREVIEW]!r}"))

    def parametrize_bindings(self) -> list[dict[str, str]]:
        """One binding per combination of the ``parametrize`` tables of the test and its classes.

        Stacked tables are combined as pytest combines them, their cross product, up to
        :data:`_COMBINATIONS`; past it, one binding per row of each table, the others at their first
        row. A column binds its parameter only when every cell of it is a string.
        """
        tables: list[list[dict[str, str]]] = []
        for decorator in (*self.class_decorators, *self.function.decorator_list):
            if not (isinstance(decorator, ast.Call) and _leaf(decorator.func) == "parametrize"):
                continue
            if len(decorator.args) < 2:
                continue
            names = _parameter_names(decorator.args[0])
            rows = self._parameter_rows(decorator.args[1])
            if not names or not rows:
                continue
            columns = [
                index
                for index in range(len(names))
                if all(index < len(row) and isinstance(row[index], str) for row in rows)
            ]
            tables.append([{names[index]: str(row[index]) for index in columns} for row in rows])
        if not tables:
            return [{}]
        if math.prod(len(table) for table in tables) <= _COMBINATIONS:
            return [{name: value for row in rows for name, value in row.items()} for rows in itertools.product(*tables)]
        first = {name: value for table in tables for name, value in table[0].items()}
        return [{**first, **row} for table in tables for row in table]

    def _parameter_rows(self, values: ast.expr) -> list[tuple[str | None, ...]] | None:
        rows = literal_table(values)
        if rows is None and isinstance(values, ast.Name):
            rows = self.context.string_rows.get(values.id)
        if rows is None and isinstance(values, ast.Call) and values.args and isinstance(values.args[0], ast.Name):
            rows = self.context.string_rows.get(values.args[0].id)
        return rows

    def run(self) -> list[tuple[int, str, str]]:
        start = (dict(self.taint), dict(self.paths), dict(self.literals))
        for binding in self.parametrize_bindings():  # every row: one exempt row hides no other
            self.taint, self.paths, self.literals = (dict(table) for table in start)
            for name, value in binding.items():
                self.literals[name] = value
                self.paths.setdefault(name, value)
            self.visit_body(self.function.body)
        return self.hits


def _parameter_names(node: ast.expr) -> list[str]:
    if _string(node) is not None:
        return [name.strip() for name in str(_string(node)).split(",")]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [item for item in (_string(element) for element in node.elts) if item is not None]
    return []


def _is_failure(statement: ast.stmt) -> bool:
    if isinstance(statement, ast.Raise) and statement.exc is not None:
        raised = statement.exc.func if isinstance(statement.exc, ast.Call) else statement.exc
        return _leaf(raised) == "AssertionError"
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
        return _leaf(statement.value.func) == "fail"
    return False


#: Each comparison operator and its negation: what a failure guarded by the one requires.
_NEGATED: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
}

_COMPARE: dict[type[ast.cmpop], Callable[[int, int], bool]] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}


def _requires_a_match(operator: ast.cmpop, other: ast.expr, *, swapped: bool) -> bool:
    """Whether a count compared by *operator* with *other* is required to be at least one.

    Against an integer literal the comparison is decided by trying a count of zero: a comparison
    zero satisfies (``== 0``, ``< 3``) allows no match and is a token ban, one zero fails requires a
    match. Against any other value, ``==``, ``>`` and ``>=`` with the count on the left are read as
    a requirement. *swapped*: the count is the right operand.
    """
    compare = _COMPARE.get(type(operator))
    if compare is None:
        return False
    if isinstance(other, ast.Constant) and type(other.value) is int:
        return not (compare(other.value, 0) if swapped else compare(0, other.value))
    if swapped:
        return isinstance(operator, ast.Eq)
    return isinstance(operator, (ast.Eq, ast.Gt, ast.GtE))


def _is_count_or_len(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _leaf(node.func) in ("count", "len")


def _module_taint(module: ast.Module, context: _ModuleContext) -> dict[str, str]:
    """Module-level names bound to a text source."""
    probe = _FunctionScan(_empty_function(), context)
    for name, value in _assignments(module):
        kind = probe.source_kind(value)
        if kind:
            probe.taint[name] = kind
    return dict(probe.taint)


def _empty_function() -> ast.FunctionDef:
    arguments = ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[])
    return ast.FunctionDef(name="_", args=arguments, body=[], decorator_list=[])


def _returned(function: ast.FunctionDef) -> list[ast.expr]:
    return [node.value for node in ast.walk(function) if isinstance(node, ast.Return) and node.value is not None]


class PhraseScanner:
    """The test-rule scan over the test trees of one repository."""

    def __init__(self, repo: Path, files: list[Path]) -> None:
        self.repo = repo
        self.files = files
        self.trees = {path: ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix()) for path in files}
        self.helpers_by_module: dict[str, dict[str, _Helper]] = {}
        self.global_constants: dict[str, str] = {}

    def _context(self, path: Path) -> _ModuleContext:
        module = self.trees[path]
        visible = dict(self.helpers_by_module.get(path.stem, {}))
        for node in module.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                source = self.helpers_by_module.get(node.module.rsplit(".", 1)[-1], {})
                for alias in node.names:
                    if alias.name in source:
                        visible[alias.asname or alias.name] = source[alias.name]
        context = _ModuleContext(path, self.repo, visible, self.global_constants)
        context.learn(module)
        return context

    def discover_helpers(self, path: Path) -> None:
        """Record *path*'s module-level non-test functions that return a text source."""
        context = self._context(path)
        for name, value in context.constants.items():
            self.global_constants.setdefault(name, value)
        for function in self.trees[path].body:
            if not isinstance(function, ast.FunctionDef) or function.name.startswith("test"):
                continue
            helper = self._classify_helper(function, context)
            if helper is not None:
                self.helpers_by_module.setdefault(path.stem, {})[function.name] = helper
                context.helpers[function.name] = helper

    def _classify_helper(self, function: ast.FunctionDef, context: _ModuleContext) -> _Helper | None:
        returned = _returned(function)
        if not returned:
            return None
        parameters = [argument.arg for argument in function.args.args]
        return (
            self._direct_helper(function, context, parameters, returned)
            or self._probed_read_helper(function, context, parameters, returned)
            or self._propagating_helper(function, context, parameters, returned)
        )

    @staticmethod
    def _direct_helper(
        function: ast.FunctionDef, context: _ModuleContext, parameters: list[str], returned: list[ast.expr]
    ) -> _Helper | None:
        scan = _FunctionScan(function, context)
        for index, name in enumerate(parameters):
            scan.paths[name] = f"<P{index}>"
        scan.visit_body(function.body)
        for value in returned:
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                if value.func.attr in _READ_METHODS:
                    template = scan.path_of(value.func.value)
                    if template is not None and "<P" in template:
                        return _Helper("read", int(template.split("<P", 1)[1].split(">", 1)[0]), template)
            kind = scan.source_kind(value)
            if kind:
                return _Helper("fixed", 0, kind)
        return None

    def _probed_read_helper(
        self, function: ast.FunctionDef, context: _ModuleContext, parameters: list[str], returned: list[ast.expr]
    ) -> _Helper | None:
        for index, name in enumerate(parameters):
            for probe_path in ("docs/__probe__.md", "packages/tools/src/__probe__.py"):
                probe = _FunctionScan(function, context)
                probe.paths[name] = (self.repo / probe_path).as_posix()
                probe.literals[name] = probe_path
                probe.visit_body(function.body)
                if any(probe.source_kind(value) for value in returned):
                    return _Helper("read", index, f"<P{index}>")
        return None

    @staticmethod
    def _propagating_helper(
        function: ast.FunctionDef, context: _ModuleContext, parameters: list[str], returned: list[ast.expr]
    ) -> _Helper | None:
        for index, name in enumerate(parameters):
            probe = _FunctionScan(function, context)
            probe.taint[name] = "derived"
            probe.visit_body(function.body)
            if any(probe.source_kind(value) for value in returned):
                return _Helper("propagate", index, "")
        return None

    def scan_file(self, path: Path) -> dict[str, str]:
        """``<file>::[<Class>::]<test>`` -> the first requirement, for every violating test of *path*."""
        context = self._context(path)
        module = self.trees[path]
        taint = _module_taint(module, context)
        relative = path.relative_to(self.repo).as_posix()
        found: dict[str, str] = {}
        for prefix, function, class_decorators in _test_functions(module.body):
            scan = _FunctionScan(function, context, class_decorators)
            scan.taint.update(taint)
            hits = scan.run()
            if hits:
                line, kind, what = hits[0]
                found[f"{relative}::{prefix}{function.name}"] = f"{kind}, line {line}: {what}"
        return found

    def scan(self) -> dict[str, str]:
        for _ in range(2):  # a helper built on another helper
            for path in self.files:
                self.discover_helpers(path)
        found: dict[str, str] = {}
        for path in self.files:
            if path.name.startswith("test_"):
                found.update(self.scan_file(path))
        return dict(sorted(found.items()))


def _test_functions(
    body: list[ast.stmt], prefix: str = "", decorators: tuple[ast.expr, ...] = ()
) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, tuple[ast.expr, ...]]]:
    """Every test function of *body*, a ``Test*`` class's methods and its nested ``Test*`` classes' included.

    Each comes with the decorators of the classes around it, outermost first.
    """
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            yield prefix, node, decorators
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            yield from _test_functions(node.body, f"{prefix}{node.name}::", (*decorators, *node.decorator_list))


def product_test_files(root: Path) -> list[Path]:
    """Every ``.py`` file of :data:`TEST_TREES` (helpers are discovered in all, tests read in ``test_*``)."""
    files: list[Path] = []
    for tree in TEST_TREES:
        base = root / tree
        if base.is_dir():
            files.extend(path for path in base.rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(files, key=lambda path: path.as_posix())


def measure_test_rule(root: Path) -> dict[str, str]:
    """Every product test function that requires an exact phrase of a document or a source."""
    return PhraseScanner(root, product_test_files(root)).scan()


# ---------------------------------------------------------------------------
# Private crossings
# ---------------------------------------------------------------------------


def _product_modules(root: Path) -> dict[str, str]:
    """Map every module of :data:`PACKAGE_ROOTS` to its repository-relative POSIX path, keyed by import name."""
    modules: dict[str, str] = {}
    for package_root in PACKAGE_ROOTS:
        for path in sorted((root / package_root).rglob("*.py"), key=lambda path: path.as_posix()):
            key = module_key(root, path)
            if key and "__pycache__" not in path.parts:
                modules[key] = path.relative_to(root).as_posix()
    return modules


def _imported_module(current: str, is_package: bool, node: ast.ImportFrom) -> str | None:
    """Return the absolute module that *node*, an import in module *current*, names."""
    if node.level == 0:
        return node.module
    parts = current.split(".")
    if not is_package:
        parts.pop()
    parts = parts[: len(parts) - (node.level - 1)]
    return ".".join([*parts, *([node.module] if node.module else [])]) or None


def _is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _private_bindings(node: ast.AST, current: str, is_package: bool, modules: dict[str, str]) -> list[tuple[str, str]]:
    """Return the ``(name, source module)`` pairs by which *node*, in module *current*, is a private crossing."""
    found: list[tuple[str, str]] = []
    if is_package and isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name in modules and alias.asname and _is_private(alias.asname):
                found.append((alias.asname, alias.name))
        return found
    if not isinstance(node, ast.ImportFrom):
        return found
    source = _imported_module(current, is_package, node)
    if source not in modules:
        return found
    for alias in node.names:
        if source == current and f"{source}.{alias.name}" not in modules:
            continue
        if _is_private(alias.name):
            found.append((alias.name, source))
        elif is_package and _is_private(alias.asname or alias.name):
            found.append((alias.asname or alias.name, source))
    return found


def measure_private_crossings(root: Path) -> dict[str, str]:
    """Return every product import of a private name from another product module.

    The importers and the sources are the modules of :data:`PACKAGE_ROOTS`, and every ``from M import x``
    of an importer is read, a function's included, ``M`` absolute or relative. It is a crossing when ``M``
    is a product module other than the importer and ``x`` starts with an underscore and is no dunder; or
    when the importer is an ``__init__.py`` and its binding (``as`` name, else ``x``) is private, so a
    package re-exports no private name, under an alias of a public one either. An ``__init__.py``'s
    ``from . import _x`` crosses when ``_x`` is its own submodule, and its ``import M as _m`` crosses when
    ``M`` is a product module. Re-exports are not followed: a private import from any other product module
    is already the crossing.

    The key is ``<importer path>::<name>``, the path repository-relative, the name the imported one, or the
    private binding when only that is private; the value is ``M``. Two imports of one name in one importer
    keep one entry, naming the last ``M``.

    What it does not reach: an ``M._x`` attribute read, ``getattr`` or ``importlib`` by a string,
    ``import M._x`` of a private submodule, ``from M import *`` whose ``M.__all__`` lists a private name,
    a private name an ``__init__.py`` defines itself (listed in its ``__all__`` or not) or binds from a module
    outside the product (``import os as _os``), a name bound by ``exec``, a module other than an ``__init__.py``
    binding a public name as a private alias, and an importer outside :data:`PACKAGE_ROOTS`.
    """
    modules = _product_modules(root)
    crossings: dict[str, str] = {}
    for current, relative in modules.items():
        is_package = relative.endswith("/__init__.py")
        for node in ast.walk(ast.parse((root / relative).read_text(encoding="utf-8"))):
            for name, source in _private_bindings(node, current, is_package, modules):
                crossings[f"{relative}::{name}"] = source
    return dict(sorted(crossings.items()))


# ---------------------------------------------------------------------------
# Measurement, baseline, verdict
# ---------------------------------------------------------------------------


@dataclass
class Measurement:
    """One measurement of the tree; *limited* maps each metric of :data:`LIMITS` to its values."""

    limited: dict[str, dict[str, int]]
    mypy: dict[str, int]
    line_citations: list[str]
    test_rule: dict[str, str]
    private_crossings: dict[str, str]
    #: The :data:`CLOSED_TEST_TREES` this checkout does not hold; their listed entries are kept, unmeasured.
    absent_trees: tuple[str, ...] = ()

    def unmeasured(self, key: str) -> bool:
        """Whether *key* lies in a tree this checkout does not hold."""
        return any(key.startswith(f"{tree}/") for tree in self.absent_trees)


def measure(
    root: Path,
    *,
    mypy_runner: MypyRunner = run_mypy,
    citation_files: Callable[[Path], tuple[list[Path], set[str]]] = tracked_citation_files,
) -> Measurement:
    files, modules = citation_files(root)
    return Measurement(
        limited={
            **measure_sources(root),
            "test_file_lines": measure_test_files(root),
            "docstring_lines": measure_docstrings(root),
        },
        mypy=measure_mypy(root, mypy_runner),
        line_citations=measure_line_citations(root, files, modules),
        test_rule=measure_test_rule(root),
        private_crossings=measure_private_crossings(root),
        absent_trees=tuple(tree for tree in CLOSED_TEST_TREES if not (root / tree).is_dir()),
    )


def initial_baseline(measurement: Measurement) -> dict[str, object]:
    """The first baseline: every entry over its limit, every mypy count, test-rule violator and crossing."""
    baseline: dict[str, object] = {"limits": dict(LIMITS)}
    for metric, values in measurement.limited.items():
        baseline[metric] = {key: value for key, value in sorted(values.items()) if value > LIMITS[metric]}
    baseline["mypy"] = {
        "arguments": list(MYPY_ARGUMENTS),
        "mypypath": list(MYPY_PATH),
        "errors": dict(measurement.mypy),
    }
    baseline["test_rule"] = {"allowlist": sorted(measurement.test_rule), "exceptions": {}}
    baseline["private_crossings"] = dict(sorted(measurement.private_crossings.items()))
    return baseline


@dataclass
class Verdict:
    """What a comparison found: growth (``check`` and ``update`` refuse it) and stale entries."""

    growth: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    lowered: list[str] = field(default_factory=list)


def _limited_verdict(metric: str, listed: dict[str, int], values: dict[str, int], verdict: Verdict) -> None:
    limit = LIMITS[metric]
    vanished = sorted(key for key in listed if key not in values)
    for key, value in sorted(values.items()):
        if key in listed:
            if value > listed[key]:
                verdict.growth.append(f"{metric} {key}: grew from the baseline's {listed[key]} to {value}")
            elif value < listed[key] and value > limit:
                verdict.lowered.append(f"{metric} {key}: {listed[key]} -> {value}")
            elif value <= limit:
                verdict.stale.append(
                    f"{metric} {key}: the baseline's {listed[key]} is stale, now {value}, within the limit {limit}"
                )
        elif value > limit:
            module = key.split("::", 1)[0]
            renamed = [old for old in vanished if old.split("::", 1)[0] == module]
            hint = (
                f"; the baseline's {', '.join(renamed)} vanished -- a rename is a reviewed baseline edit"
                if renamed
                else ""
            )
            verdict.growth.append(f"{metric} {key}: new entry, baseline none, now {value} over the limit {limit}{hint}")
    for key in vanished:
        verdict.stale.append(f"{metric} {key}: the baseline's {listed[key]} is stale, the key no longer exists")


def _mypy_verdict(baseline: dict[str, object], measurement: Measurement, verdict: Verdict) -> None:
    recorded = baseline["mypy"]
    assert isinstance(recorded, dict)
    if recorded.get("arguments") != list(MYPY_ARGUMENTS) or recorded.get("mypypath") != list(MYPY_PATH):
        raise RatchetError(
            f"mypy: the baseline was taken with {recorded.get('arguments')} and MYPYPATH {recorded.get('mypypath')}, "
            f"this script runs {list(MYPY_ARGUMENTS)} and {list(MYPY_PATH)}; counts are not comparable"
        )
    errors: dict[str, int] = recorded["errors"]
    if set(errors) != set(measurement.mypy):
        raise RatchetError(
            f"mypy: the baseline counts {sorted(errors)}, this script counts {sorted(measurement.mypy)}; "
            "a package added or removed is a reviewed baseline edit"
        )
    for package, count in sorted(measurement.mypy.items()):
        if count > errors[package]:
            verdict.growth.append(f"mypy {package}: grew from the baseline's {errors[package]} errors to {count}")
        elif count < errors[package]:
            verdict.lowered.append(f"mypy {package}: {errors[package]} -> {count}")


def _test_rule_verdict(baseline: dict[str, object], measurement: Measurement, verdict: Verdict) -> None:
    recorded = baseline["test_rule"]
    assert isinstance(recorded, dict)
    allowed = set(recorded["allowlist"]) | set(recorded["exceptions"])
    for key, detail in sorted(measurement.test_rule.items()):
        if key not in allowed:
            verdict.growth.append(
                f"test_rule {key}: new violator, baseline not listed, now requires a phrase ({detail})"
            )
    for key in sorted(allowed - set(measurement.test_rule)):
        verdict.stale.append(f"test_rule {key}: the baseline lists it, now it requires no phrase; the entry is stale")


def _private_crossings_verdict(baseline: dict[str, object], measurement: Measurement, verdict: Verdict) -> None:
    recorded = baseline["private_crossings"]
    assert isinstance(recorded, dict)
    for key, module in sorted(measurement.private_crossings.items()):
        if key not in recorded:
            verdict.growth.append(
                f"private_crossings {key}: new crossing, baseline not listed, now imports a private name from {module}"
            )
    for key in sorted(set(recorded) - set(measurement.private_crossings)):
        verdict.stale.append(f"private_crossings {key}: the baseline lists it, now it is gone; the entry is stale")


def compare(baseline: dict[str, object], measurement: Measurement) -> Verdict:
    """Compare *measurement* with *baseline*."""
    missing = [key for key in MEASURED_KEYS if key not in baseline]
    if missing:
        raise RatchetError(
            f"the baseline lacks {', '.join(missing)}; `quality_ratchet.py init <key>` creates an absent key "
            "from the current tree"
        )
    if baseline.get("limits") != LIMITS:
        raise RatchetError(f"the baseline's limits {baseline.get('limits')} are not this script's {LIMITS}")
    verdict = Verdict()
    for metric, values in measurement.limited.items():
        listed = baseline[metric]
        assert isinstance(listed, dict)
        measured = {key: value for key, value in listed.items() if not measurement.unmeasured(key)}
        _limited_verdict(metric, measured, values, verdict)
    _mypy_verdict(baseline, measurement, verdict)
    _test_rule_verdict(baseline, measurement, verdict)
    _private_crossings_verdict(baseline, measurement, verdict)
    return verdict


def lowered_baseline(baseline: dict[str, object], measurement: Measurement) -> dict[str, object]:
    """*baseline* with every value lowered to the measurement and every stale entry dropped."""
    updated: dict[str, object] = {"limits": dict(LIMITS)}
    for metric, values in measurement.limited.items():
        listed = baseline[metric]
        assert isinstance(listed, dict)
        updated[metric] = {
            key: value if measurement.unmeasured(key) else min(value, values[key])
            for key, value in sorted(listed.items())
            if measurement.unmeasured(key) or (key in values and values[key] > LIMITS[metric])
        }
    recorded_mypy = baseline["mypy"]
    assert isinstance(recorded_mypy, dict)
    updated["mypy"] = {
        **recorded_mypy,
        "errors": {
            package: min(count, measurement.mypy[package]) for package, count in recorded_mypy["errors"].items()
        },
    }
    recorded_rule = baseline["test_rule"]
    assert isinstance(recorded_rule, dict)
    updated["test_rule"] = {
        "allowlist": sorted(key for key in recorded_rule["allowlist"] if key in measurement.test_rule),
        "exceptions": {key: word for key, word in recorded_rule["exceptions"].items() if key in measurement.test_rule},
    }
    recorded_crossings = baseline["private_crossings"]
    assert isinstance(recorded_crossings, dict)
    updated["private_crossings"] = {
        key: module for key, module in sorted(recorded_crossings.items()) if key in measurement.private_crossings
    }
    return updated


def refuse_init(baseline: dict[str, object], key: str) -> None:
    """Raise :class:`RatchetError` unless *key* is a measured key that *baseline* lacks."""
    if key not in MEASURED_KEYS:
        raise RatchetError(f"{key} is not a measured key; the measured keys are {', '.join(MEASURED_KEYS)}")
    if key in baseline:
        raise RatchetError(f"the baseline holds {key}; `init` creates an absent key only, never re-records one")


def initialised_baseline(baseline: dict[str, object], key: str, measurement: Measurement) -> dict[str, object]:
    """*baseline* with the absent measured *key* added from *measurement*, every other key as it was.

    The caller has refused a present or unknown key (:func:`refuse_init`, before measuring). The keys keep
    :func:`initial_baseline`'s order. A limited metric's limit is added to ``limits`` when that table lacks it;
    a limit it holds is kept, so a differing one still fails ``check``. A baseline without ``limits`` is refused.
    """
    merged = {**baseline, key: initial_baseline(measurement)[key]}
    updated = {**{name: merged[name] for name in ("limits", *MEASURED_KEYS) if name in merged}, **merged}
    if key in LIMITS:
        limits = baseline.get("limits")
        if not isinstance(limits, dict):
            raise RatchetError(f"the baseline has no limits table; `init {key}` adds a limit to one only")
        updated["limits"] = {**limits, key: limits.get(key, LIMITS[key])}
    return updated


def _init(root: Path, key: str, measure_tree: Callable[[Path], Measurement]) -> int:
    """``init <key>``: add the absent measured *key* to the baseline at *root*; returns the exit status."""
    baseline_path = root / BASELINE
    try:
        baseline = read_baseline(baseline_path)
        refuse_init(baseline, key)
        updated = initialised_baseline(baseline, key, measure_tree(root))
    except RatchetError as error:
        print(f"quality ratchet: {error}", file=sys.stderr)
        return 1
    write_baseline(baseline_path, updated)
    print(f"quality ratchet: added {key} to {BASELINE}")
    return 0


def read_baseline(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise RatchetError(f"no baseline at {path.as_posix()}; `update --initial` writes the first one")
    return json.loads(path.read_text(encoding="utf-8"))


def write_baseline(path: Path, baseline: dict[str, object]) -> None:
    path.write_text(json.dumps(baseline, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def _report(lines: Iterable[str], heading: str) -> None:
    items = list(lines)
    if items:
        print(f"{heading} ({len(items)}):")
        for item in items:
            print(f"  {item}")


def run(argv: list[str] | None = None, *, measure_tree: Callable[[Path], Measurement] = measure) -> int:
    """The command line: ``check``, ``update``, ``update --initial`` or ``init <key>``; returns the exit status."""
    parser = argparse.ArgumentParser(prog="quality_ratchet.py", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("check", "update", "init"))
    parser.add_argument("key", nargs="?", help="the absent measured key to create (init only)")
    parser.add_argument("--initial", action="store_true", help="write the first baseline (update only)")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    arguments = parser.parse_args(argv)
    root: Path = arguments.repo_root.resolve()
    baseline_path = root / BASELINE
    if arguments.initial and arguments.command != "update":
        parser.error("--initial goes with update")
    if (arguments.key is None) == (arguments.command == "init"):
        parser.error("a key goes with init, and init needs one")
    if arguments.command == "init":
        return _init(root, arguments.key, measure_tree)
    try:
        if arguments.initial and baseline_path.exists():
            raise RatchetError(f"{BASELINE} exists; --initial writes only the first baseline")
        baseline = None if arguments.initial else read_baseline(baseline_path)
        measurement = measure_tree(root)
    except RatchetError as error:
        print(f"quality ratchet: {error}", file=sys.stderr)
        return 1
    _report(measurement.line_citations, "line citations (a zero check; cite `module.py::qualname`)")
    if baseline is None:
        if measurement.line_citations:
            return 1
        write_baseline(baseline_path, initial_baseline(measurement))
        print(f"quality ratchet: wrote {BASELINE}")
        return 0
    try:
        verdict = compare(baseline, measurement)
    except RatchetError as error:
        print(f"quality ratchet: {error}", file=sys.stderr)
        return 1
    _report(verdict.growth, "grew")
    if arguments.command == "check":
        _report(verdict.stale, "stale baseline entries (`quality_ratchet.py update` drops them)")
        _report(verdict.lowered, "lowered, held only once `quality_ratchet.py update` records them")
        if measurement.line_citations or verdict.growth or verdict.stale:
            return 1
        print(
            f"quality ratchet: nothing grew; mypy {measurement.mypy}, {len(measurement.test_rule)} listed tests, "
            f"{len(measurement.private_crossings)} private crossings"
        )
        return 0
    if measurement.line_citations or verdict.growth:
        print("quality ratchet: update refuses a rise, a new entry or a line citation", file=sys.stderr)
        return 1
    _report(verdict.lowered, "lowered")
    _report(verdict.stale, "dropped")
    write_baseline(baseline_path, lowered_baseline(baseline, measurement))
    return 0


if __name__ == "__main__":
    sys.exit(run())
