"""The text-I/O, path-spelling, sort-order and Unix-only-interface rules, and the AST scan that holds all five.

- **Rule E:** every text read and write, ``subprocess`` text mode included, names ``encoding=``; no allow-list.
  The locale's codec (cp1252 on the measured Windows host) fails loudly on some characters and silently on others.
- **Rule N:** every text write names ``newline=``, and its value is ``"\\n"`` unless the site is in
  :data:`NATIVE_NEWLINE_SITES` with a reason (a transcript for a human that nothing else reads).
- **Rule P:** a path turned into a string is spelled ``.as_posix()``, never ``str(...)``: every such string is
  compared against a POSIX literal, and on Windows the comparison silently fails.
- **Rule S:** a ``sorted()`` over paths names a key, and the key is the POSIX spelling (:class:`TestSortOrderRule`).
- **Rule U:** no module names an interface Windows does not have (:class:`TestUnixOnlyInterfaceRule`).

**Which trees each check reads.** Rule E reads the product trees, ``scripts/`` and the test trees
(:data:`TEST_TREES`). Rule N reads the product trees, ``scripts/`` and the test trees' helper modules (every ``.py``
not named ``test_*``), whose bytes leave the function that wrote them; a ``test_*.py`` module's scratch file is read
back through the same API, so a helper function inside one stays outside Rule N. Rules P and S read both sets.
The template tree is in scope, so the rules hold whatever Python is added there.

**What this scan does not reach.** Every check is syntactic over a file never imported or run, resolves no receiver
and sees calls only by their names (``open``, ``read_text``/``write_text``, ``subprocess.run``/``Popen``). A bare
``x.open()``, a mode given as a variable, a wrapper around ``open``, ``io.TextIOWrapper``, ``tempfile``,
``subprocess.check_output`` and third-party I/O are not seen. Rule P sees only ``str(...)`` around a call named
``relative_to``: interpolation, ``os.path.relpath``, ``"/".join(path.parts)``, a bare ``str(path)`` and
``.endswith("a/b")`` on a native path are outside it. A green scan constrains the shape of new code; the oracle for a
byte-level fix is a byte comparison.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Rule N's trees, and the first half of Rule E's. No closed folder is listed: the public tree has none,
#: and the closed twin of this check runs these rules over its own trees. The specimen generator's
#: ``src`` and its scenario runner (``tests/scenarios``, a product tree inside the generator's test
#: tree) run on the Windows runner and in the workstation archive like the rest.
PRODUCT_TREES = (
    "packages/tools/src",
    "packages/mcp/src",
    "packages/agent/src",
    "scripts",
    "packages/specimens/src",
    "packages/specimens/tests/scenarios",
)

#: Rule E's second half. Rule N reads only their helper modules (:func:`is_test_helper`).
#: ``scripts/tests`` (the scripts' tests) is a test tree inside the product tree ``scripts``, and
#: :func:`tree_modules` reads each of its modules under the test tree alone.
TEST_TREES = (
    "packages/tools/tests",
    "packages/mcp/tests",
    "packages/agent/tests",
    "packages/specimens/tests",
    "scripts/tests",
)

#: Rule P's trees: every one of them, product and test alike.
ALL_TREES = PRODUCT_TREES + TEST_TREES


def tree_modules(tree: str) -> list[Path]:
    """Every ``.py`` under the repo-relative *tree*, POSIX-sorted, read under exactly one tree.

    The modules of a tree of :data:`ALL_TREES` nested inside it (``scripts/tests`` inside ``scripts``) are left out."""
    nested = [REPO_ROOT / other for other in ALL_TREES if other != tree and other.startswith(f"{tree}/")]
    return [
        path
        for path in sorted((REPO_ROOT / tree).rglob("*.py"), key=lambda path: path.as_posix())
        if not any(path.is_relative_to(root) for root in nested)
    ]


#: The characters a Python file mode is made of. A literal outside this set --
#: ``tarfile.open(mode="r|")``, ``"r:gz"`` -- is not a file mode and is skipped.
_MODE_CHARACTERS = frozenset("rwxab+t")

#: Keywords that mark an ``x.open(...)`` as a *text file* open even with no mode.
_TEXT_IO_KEYWORDS = frozenset({"encoding", "newline", "errors"})

#: The text writes that may be platform-native, keyed by ``(module, function)``
#: and never by ``file:line``: a line number rots silently on the next edit
#: *above* it. The module is spelled repo-relative and POSIX so the key means the
#: same thing on every platform; the function is the enclosing qualified name, so
#: two same-named methods cannot share an exemption.
#: :meth:`TestNewlineRule.test_every_exempt_site_still_exists` asserts every key
#: still names a real write.
NATIVE_NEWLINE_SITES: dict[tuple[str, str], str] = {
    (
        "packages/tools/src/swcraft_tools/cli/import_commands.py",
        "cmd_import",
    ): "`swcraft import model --report PATH`: the text is `report.render()`, already printed to "
    "stdout in the same breath, written only because the user named a path for it, and read back by "
    "nothing in SWCraft. The stated risk is that an architect who commits it as import evidence gets "
    "platform churn on it; that is acceptable for a transcript of a run.",
    (
        "packages/tools/src/swcraft_tools/cli/report_files.py",
        "write_report_file",
    ): "`swcraft import component --report PATH`, `swcraft import requirements --report PATH` and "
    "`swcraft experiment trace --apply FILE --report PATH`, a path not ending in `.md`: the same transcript, "
    "the same reasoning; the requirement files the same "
    'run writes are byte-stable (`newline="\\n"`, `requirements_import.py`).',
}


@dataclass(frozen=True)
class IOSite:
    """One text-I/O call the scan can read."""

    tree: str
    module: str
    """Repo-relative, POSIX-spelled."""
    function: str
    """The enclosing qualified name, or ``<module>``."""
    lineno: int
    kind: str
    """``open`` / ``read_text`` / ``write_text`` / ``subprocess``."""
    writes: bool
    call: ast.Call

    @property
    def key(self) -> tuple[str, str]:
        return (self.module, self.function)

    def __str__(self) -> str:
        return f"{self.module}:{self.lineno} {self.function}() [{self.kind}]"


def _keyword(call: ast.Call, name: str) -> ast.keyword | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword
    return None


def _literal_mode(call: ast.Call, position: int) -> str | None:
    """The call's literal file mode, or ``None`` when it cannot be read.

    ``position`` is where the mode sits positionally: 1 for the builtin
    ``open(path, mode)``, 0 for ``path.open(mode)``.
    """
    keyword = _keyword(call, "mode")
    if keyword is not None:
        if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
        return None
    if len(call.args) > position:
        argument = call.args[position]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            return argument.value
        return None
    return "r"


def _is_file_mode(mode: str | None) -> bool:
    return mode is not None and mode != "" and set(mode) <= _MODE_CHARACTERS


def _is_literal_true(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


class _SiteVisitor(ast.NodeVisitor):
    """Collects the I/O calls of one module, tracking the enclosing qualname."""

    def __init__(self, tree: str, module: str) -> None:
        self._tree = tree
        self._module = module
        self._scope: list[str] = []
        self.sites: list[IOSite] = []

    def _scoped(self, node: ast.AST) -> None:
        self._scope.append(node.name)  # type: ignore[attr-defined]
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped
    visit_ClassDef = _scoped

    def visit_Call(self, node: ast.Call) -> None:
        self._record(node)
        self.generic_visit(node)

    def _record(self, node: ast.Call) -> None:
        function = node.func
        if isinstance(function, ast.Name) and function.id == "open":
            mode = _literal_mode(node, 1)
            if _is_file_mode(mode):
                self._append(node, "open", mode)
            return
        if not isinstance(function, ast.Attribute):
            return
        if function.attr == "open":
            names_io = any(keyword.arg in _TEXT_IO_KEYWORDS for keyword in node.keywords)
            states_mode = bool(node.args) or _keyword(node, "mode") is not None
            # A bare ``x.open()`` says nothing about a file: ``session.open()``
            # is that shape and is not one. Only a call that states a mode or an
            # I/O keyword is read as an open.
            if not (names_io or states_mode):
                return
            mode = _literal_mode(node, 0)
            if _is_file_mode(mode):
                self._append(node, "open", mode)
            return
        if function.attr in {"read_text", "write_text"}:
            self._append(node, function.attr, None)
            return
        if (
            function.attr in {"run", "Popen"}
            and isinstance(function.value, ast.Name)
            and function.value.id == "subprocess"
        ):
            if any(
                keyword.arg in {"text", "universal_newlines"} and _is_literal_true(keyword.value)
                for keyword in node.keywords
            ):
                self._append(node, "subprocess", None)

    def _append(self, node: ast.Call, kind: str, mode: str | None) -> None:
        if kind == "open":
            assert mode is not None
            if "b" in mode:
                return
            writes = any(character in mode for character in "wxa+")
        elif kind == "read_text":
            writes = False
        elif kind == "write_text":
            writes = True
        else:
            writes = False
        self.sites.append(
            IOSite(
                tree=self._tree,
                module=self._module,
                function=".".join(self._scope) or "<module>",
                lineno=node.lineno,
                kind=kind,
                writes=writes,
                call=node,
            )
        )


def scan_module(path: Path, *, tree: str, module: str) -> list[IOSite]:
    """Every text-I/O call one module makes, as far as an AST can see them."""
    visitor = _SiteVisitor(tree, module)
    visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
    return visitor.sites


def scan_trees(trees: tuple[str, ...]) -> list[IOSite]:
    """Every text-I/O call in the named repo-relative trees."""
    sites: list[IOSite] = []
    for tree in trees:
        for path in tree_modules(tree):
            sites.extend(scan_module(path, tree=tree, module=path.relative_to(REPO_ROOT).as_posix()))
    return sites


def is_test_helper(site: IOSite) -> bool:
    """Whether *site* is in a test tree's helper module: a ``.py`` whose name is not ``test_*``.

    A helper's bytes leave the function that wrote them, a test's scratch file does not.
    """
    return site.tree in TEST_TREES and not Path(site.module).name.startswith("test_")


def scan_newline_sites() -> list[IOSite]:
    """Every text-I/O call Rule N reads: the product trees, ``scripts/`` and the test helpers."""
    return scan_trees(PRODUCT_TREES) + [site for site in scan_trees(TEST_TREES) if is_test_helper(site)]


def _census(offenders: list[IOSite], trees: tuple[str, ...]) -> str:
    """The offender count, total and per tree."""
    counts = {tree: sum(1 for site in offenders if site.tree == tree) for tree in trees}
    per_tree = ", ".join(f"{tree}={count}" for tree, count in counts.items())
    return f"{len(offenders)} offender(s) ({per_tree})"


# ---------------------------------------------------------------------------
# Rule E -- every text read and every text write names its encoding
# ---------------------------------------------------------------------------


class TestEncodingRule:
    """No text I/O anywhere in the repository may inherit the locale codec."""

    def test_every_product_text_io_names_its_encoding(self) -> None:
        offenders = [site for site in scan_trees(PRODUCT_TREES) if _keyword(site.call, "encoding") is None]
        assert [str(site) for site in offenders] == [], (
            "these product calls decode or encode with the locale's codec, which is cp1252 on the "
            f"measured Windows host and UTF-8 nowhere by default -- {_census(offenders, PRODUCT_TREES)}. "
            "Pass encoding='utf-8'. A subprocess in text mode needs it too: text=True alone hands "
            "the child's output to the same locale codec."
        )

    def test_every_test_tree_text_io_names_its_encoding(self) -> None:
        offenders = [site for site in scan_trees(TEST_TREES) if _keyword(site.call, "encoding") is None]
        assert [str(site) for site in offenders] == [], (
            "these test-suite calls decode or encode with the locale's codec -- "
            f"{_census(offenders, TEST_TREES)}. The trees are in scope because five of the six "
            "encoding failures on the 2026-08-27 Windows run were test-side. Pass encoding='utf-8'."
        )


# ---------------------------------------------------------------------------
# Rule N -- every text write names its newline
# ---------------------------------------------------------------------------


class TestNewlineRule:
    """A text write states which bytes it puts on disk, or argues for the default."""

    def test_every_product_text_write_names_its_newline(self) -> None:
        offenders = [
            site for site in scan_trees(PRODUCT_TREES) if site.writes and _keyword(site.call, "newline") is None
        ]
        assert [str(site) for site in offenders] == [], (
            "these text writes translate '\\n' to '\\r\\n' on Windows and say nothing about it -- "
            f"{_census(offenders, PRODUCT_TREES)}. "
            "Pass newline='\\n' when the bytes are committed, hashed, compared or read back by "
            "SWCraft. Pass newline=None only with the site added to NATIVE_NEWLINE_SITES and a "
            "reason -- the keyword must be present either way, because silence is what inherits."
        )

    def test_every_test_helper_text_write_names_its_newline(self) -> None:
        offenders = [
            site
            for site in scan_trees(TEST_TREES)
            if is_test_helper(site) and site.writes and _keyword(site.call, "newline") is None
        ]
        assert [str(site) for site in offenders] == [], (
            "these test-helper writes translate '\\n' to '\\r\\n' on Windows and say nothing about "
            f"it -- {_census(offenders, TEST_TREES)}. A helper's bytes leave the function that wrote "
            "them (a conftest fixture's model, a generator's bench, an oracle's record) and are hashed, "
            "compared or read by the product elsewhere, so a translated byte breaks a digest on Windows. "
            "Pass newline='\\n'; the test_*.py modules are outside this rule."
        )

    def test_a_declared_newline_is_lf_unless_the_site_is_exempt(self) -> None:
        offenders = []
        for site in scan_newline_sites():
            keyword = _keyword(site.call, "newline")
            if keyword is None or site.key in NATIVE_NEWLINE_SITES:
                continue
            if not (isinstance(keyword.value, ast.Constant) and keyword.value.value == "\n"):
                offenders.append(site)
        assert [str(site) for site in offenders] == [], (
            "these text writes declare a newline that is not '\\n' and are not in "
            f"NATIVE_NEWLINE_SITES -- {_census(offenders, ALL_TREES)}"
        )

    def test_every_exempt_site_still_exists(self) -> None:
        """An exemption naming a call that has gone is an exemption for nothing.

        A rename, a moved module or a deleted write would otherwise leave a documented
        exception standing over no code.
        """
        writes = {site.key for site in scan_newline_sites() if site.writes}
        stale = sorted(key for key in NATIVE_NEWLINE_SITES if key not in writes)
        assert stale == [], (
            f"NATIVE_NEWLINE_SITES exempts {stale}, and no text write is there any more. "
            "Delete the entry or re-point it -- do not leave a reason attached to nothing."
        )


# ---------------------------------------------------------------------------
# Rule N, the descriptor half -- a text write through os.fdopen opens binary
# ---------------------------------------------------------------------------


def _opens_binary(enclosing: ast.AST) -> bool:
    """*enclosing* names ``O_BINARY``, or calls ``tempfile.mkstemp`` without ``text=True``."""
    if "O_BINARY" in ast.unparse(enclosing):
        return True
    for node in ast.walk(enclosing):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "mkstemp"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "tempfile"
        ):
            text = [kw for kw in node.keywords if kw.arg == "text"]
            if not text or (isinstance(text[0].value, ast.Constant) and text[0].value.value is False):
                return True
    return False


def _fdopen_text_writes(source: str) -> list[tuple[str, int, bool]]:
    """Every ``os.fdopen(...)`` text write of *source*: ``(function, line, names O_BINARY)``.

    ``newline="\\n"`` on the wrapper is not enough on Windows: a descriptor that
    ``os.open`` returned without ``O_BINARY`` is a CRT text-mode one, which turns
    every ``\\n`` into ``\\r\\n`` below Python's I/O. The check is the enclosing
    function's text naming ``O_BINARY`` anywhere, a ``flags`` variable included --
    or calling ``tempfile.mkstemp`` without ``text=True``, whose descriptor the
    standard library opens with ``O_BINARY`` itself (``tempfile._bin_openflags``).
    **What it does not reach**: a descriptor opened in another function and
    passed in, a mode given as a variable, a mode-less (read) ``fdopen``, an
    ``O_BINARY`` or ``mkstemp`` named in that function for another descriptor, a
    ``mkstemp`` reached through an alias rather than ``tempfile.mkstemp``, and
    ``io.open``/``open(fd, ...)`` over a raw descriptor, which no rule here
    reads as a descriptor wrap.
    """
    found: list[tuple[str, int, bool]] = []

    def visit(node: ast.AST, scope: list[str], enclosing: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                inner = child if not isinstance(child, ast.ClassDef) else enclosing
                visit(child, [*scope, child.name], inner)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "fdopen"
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "os"
            ):
                mode = _literal_mode(child, 1)
                if _is_file_mode(mode) and "b" not in mode and any(c in mode for c in "wxa+"):
                    found.append((".".join(scope) or "<module>", child.lineno, _opens_binary(enclosing)))
            visit(child, scope, enclosing)

    tree = ast.parse(source)
    visit(tree, [], tree)
    return found


def _newline_rule_modules() -> list[tuple[str, Path]]:
    """Rule N's modules: the product trees, ``scripts/`` and the test trees' helper modules."""
    modules: list[tuple[str, Path]] = []
    for tree in PRODUCT_TREES + TEST_TREES:
        for path in tree_modules(tree):
            if tree in TEST_TREES and path.name.startswith("test_"):
                continue
            modules.append((path.relative_to(REPO_ROOT).as_posix(), path))
    return modules


_FDOPEN_SOURCE = """
import os
import tempfile


def text_mode(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT), "w", encoding="utf-8", newline="\\n")


def binary_flag(path):
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
    return os.fdopen(os.open(path, flags), "w", encoding="utf-8", newline="\\n")


def bytes_mode(path):
    return os.fdopen(os.open(path, os.O_WRONLY), "wb")


def staged(path):
    descriptor, name = tempfile.mkstemp(dir=path.parent)
    return os.fdopen(descriptor, "w", encoding="utf-8", newline="\\n")


def staged_text(path):
    descriptor, name = tempfile.mkstemp(dir=path.parent, text=True)
    return os.fdopen(descriptor, "w", encoding="utf-8", newline="\\n")
"""


class TestTheDescriptorWriteRule:
    """A text write wrapped around ``os.open`` names ``O_BINARY``, or Windows writes CRLF."""

    def test_every_fdopen_text_write_opens_its_descriptor_binary(self) -> None:
        offenders = [
            f"{module}:{line} {function}()"
            for module, path in _newline_rule_modules()
            for function, line, binary in _fdopen_text_writes(path.read_text(encoding="utf-8"))
            if not binary
        ]
        assert offenders == [], (
            "these os.fdopen text writes wrap a descriptor whose function never names O_BINARY: on "
            "Windows the CRT descriptor is in text mode and writes '\\r\\n' whatever newline= says. "
            'Add getattr(os, "O_BINARY", 0) to the os.open flags.'
        )

    def test_the_scan_is_not_blind(self) -> None:
        assert _fdopen_text_writes(_FDOPEN_SOURCE) == [
            ("text_mode", 7, False),
            ("binary_flag", 12, True),
            ("staged", 21, True),
            ("staged_text", 26, False),
        ]


# ---------------------------------------------------------------------------
# Rule P -- a path becomes a string through .as_posix(), never str()
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PathSite:
    """One ``relative_to`` call the scan can read, and how it is spelled."""

    tree: str
    module: str
    """Repo-relative, POSIX-spelled."""
    function: str
    """The enclosing qualified name, or ``<module>``."""
    lineno: int
    stringified: bool
    """``True`` when the call is the sole argument of ``str(...)``."""

    def __str__(self) -> str:
        return f"{self.module}:{self.lineno} {self.function}()"


def _is_relative_to(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "relative_to"


class _PathVisitor(ast.NodeVisitor):
    """Collects every ``relative_to`` call, flagging the ``str(...)`` ones."""

    def __init__(self, tree: str, module: str) -> None:
        self._tree = tree
        self._module = module
        self._scope: list[str] = []
        self._stringified: set[int] = set()
        self.sites: list[PathSite] = []

    def _scoped(self, node: ast.AST) -> None:
        self._scope.append(node.name)  # type: ignore[attr-defined]
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped
    visit_ClassDef = _scoped

    def visit_Call(self, node: ast.Call) -> None:
        # Mark before descending, so the inner call already knows it is wrapped
        # by the time ``generic_visit`` reaches it and records it.
        if isinstance(node.func, ast.Name) and node.func.id == "str" and len(node.args) == 1:
            if _is_relative_to(node.args[0]):
                self._stringified.add(id(node.args[0]))
        if _is_relative_to(node):
            self.sites.append(
                PathSite(
                    tree=self._tree,
                    module=self._module,
                    function=".".join(self._scope) or "<module>",
                    lineno=node.lineno,
                    stringified=id(node) in self._stringified,
                )
            )
        self.generic_visit(node)


def scan_module_paths(path: Path, *, tree: str, module: str) -> list[PathSite]:
    """Every ``relative_to`` call one module makes, with its spelling."""
    visitor = _PathVisitor(tree, module)
    visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
    return visitor.sites


def scan_trees_paths(trees: tuple[str, ...]) -> list[PathSite]:
    """Every ``relative_to`` call in the named repo-relative trees."""
    sites: list[PathSite] = []
    for tree in trees:
        for path in tree_modules(tree):
            sites.extend(scan_module_paths(path, tree=tree, module=path.relative_to(REPO_ROOT).as_posix()))
    return sites


def _path_census(offenders: list[PathSite], trees: tuple[str, ...]) -> str:
    """The per-tree offender count, in the same shape Rules E and N report."""
    counts = {tree: sum(1 for site in offenders if site.tree == tree) for tree in trees}
    per_tree = ", ".join(f"{tree}={count}" for tree, count in counts.items())
    return f"{len(offenders)} offender(s) ({per_tree})"


class TestPathSpellingRule:
    """A relative path that becomes a string becomes a POSIX one."""

    def test_no_relative_path_is_stringified_with_str(self) -> None:
        offenders = [site for site in scan_trees_paths(ALL_TREES) if site.stringified]
        assert [str(site) for site in offenders] == [], (
            "these calls spell a relative path with the platform separator -- "
            f"{_path_census(offenders, ALL_TREES)}. str(Path) is 'a/b' on Linux and 'a\\\\b' on "
            "Windows, and every string this repository makes that way is compared: keyed by a "
            "POSIX literal, differenced against a set of them, or fed to a digest. Write "
            "x.relative_to(y).as_posix(). On Linux the two are byte-for-byte identical, so "
            "nothing pinned may move when you change it."
        )


@dataclass(frozen=True)
class SortSite:
    """One ``sorted(...)`` over path objects, and whether it names a key."""

    tree: str
    module: str
    """Repo-relative, POSIX-spelled."""
    function: str
    """The enclosing qualified name, or ``<module>``."""
    lineno: int
    keyed: bool
    """``True`` when the call passes ``key=``."""

    def __str__(self) -> str:
        return f"{self.module}:{self.lineno} {self.function}()"


#: The three directory readers whose results are ``Path`` objects. ``os.walk``
#: and ``os.scandir`` are deliberately absent: they yield strings and
#: ``DirEntry``, neither of which ``PurePath.__lt__`` ever sees.
_PATH_PRODUCERS = frozenset({"glob", "rglob", "iterdir"})


def _produces_paths(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _PATH_PRODUCERS


def _comprehension_yields_paths(node: ast.GeneratorExp | ast.ListComp) -> bool:
    """Whether a comprehension over a path producer yields the paths themselves.

    ``sorted(p.name for p in d.glob(...))`` sorts **strings** and is correct as
    it stands; only a comprehension whose element is the loop variable, or a
    ``relative_to`` of it, hands ``sorted`` a path.
    """
    if not any(_produces_paths(generator.iter) for generator in node.generators):
        return False
    element = node.elt
    if isinstance(element, ast.Name):
        return element.id in {
            generator.target.id for generator in node.generators if isinstance(generator.target, ast.Name)
        }
    return (
        isinstance(element, ast.Call) and isinstance(element.func, ast.Attribute) and element.func.attr == "relative_to"
    )


class _SortVisitor(ast.NodeVisitor):
    """Collects every ``sorted()`` whose elements are paths."""

    def __init__(self, tree: str, module: str) -> None:
        self._tree = tree
        self._module = module
        self._scope: list[str] = []
        self.sites: list[SortSite] = []

    def _scoped(self, node: ast.AST) -> None:
        self._scope.append(node.name)  # type: ignore[attr-defined]
        self.generic_visit(node)
        self._scope.pop()

    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped
    visit_ClassDef = _scoped

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "sorted" and node.args:
            argument = node.args[0]
            sorts_paths = (
                _comprehension_yields_paths(argument)
                if isinstance(argument, ast.GeneratorExp | ast.ListComp)
                else _produces_paths(argument)
            )
            if sorts_paths:
                self.sites.append(
                    SortSite(
                        tree=self._tree,
                        module=self._module,
                        function=".".join(self._scope) or "<module>",
                        lineno=node.lineno,
                        keyed=any(keyword.arg == "key" for keyword in node.keywords),
                    )
                )
        self.generic_visit(node)


def scan_module_sorts(path: Path, *, tree: str, module: str) -> list[SortSite]:
    """Every path-sorting ``sorted()`` one module makes, with its key."""
    visitor = _SortVisitor(tree, module)
    visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
    return visitor.sites


def scan_trees_sorts(trees: tuple[str, ...]) -> list[SortSite]:
    """Every path-sorting ``sorted()`` in the named repo-relative trees."""
    sites: list[SortSite] = []
    for tree in trees:
        for path in tree_modules(tree):
            sites.extend(scan_module_sorts(path, tree=tree, module=path.relative_to(REPO_ROOT).as_posix()))
    return sites


def _sort_census(offenders: list[SortSite], trees: tuple[str, ...]) -> str:
    """The per-tree offender count, in the same shape Rules E, N and P report."""
    counts = {tree: sum(1 for site in offenders if site.tree == tree) for tree in trees}
    per_tree = ", ".join(f"{tree}={count}" for tree, count in counts.items())
    return f"{len(offenders)} offender(s) ({per_tree})"


class TestSortOrderRule:
    """A sort over paths names a POSIX key, because the default is not portable.

    ``sorted()`` over ``Path`` objects compares ``PurePath._str_normcase``, which
    on Windows **lowercases** the string and uses ``\\`` as the separator. The
    same directory therefore enumerates in a different order there:
    ``model_state.py``'s docstring gives ``['Zebra', 'alpha', 'Beta']`` coming
    back ``Beta, Zebra, alpha`` on POSIX and ``alpha, Beta, Zebra`` on Windows,
    and a recursive glob additionally sorts ``\\`` (0x5C) where POSIX sorts
    ``/`` (0x2F).

    **What this scan does not reach.**

    - It sees ``glob``/``rglob``/``iterdir`` **called by those names**, resolving
      no receiver, so any object with an ``iterdir`` is policed as if it were a
      ``Path`` -- and a ``Traversable`` from ``importlib.resources`` has no
      ``as_posix()`` to satisfy it with.
    - ``sorted(some_list_of_paths)`` where the list was built elsewhere is
      invisible: the producer and the sort are in different expressions and this
      scan reads one.
    - ``min``/``max`` over paths, ``list.sort()`` called as a statement, and
      ``heapq`` take the same comparison and are not looked at.
    - It cannot tell whether a *key* is the right one. ``key=len`` satisfies it.
      What it enforces is that the author chose one, which is the point at which
      the platform question becomes visible.
    - It says nothing about which order any platform actually produces; the
      oracle for that is a comparison across platforms, not this scan.
    """

    def test_every_sort_over_paths_names_a_key(self) -> None:
        offenders = [site for site in scan_trees_sorts(ALL_TREES) if not site.keyed]
        assert [str(site) for site in offenders] == [], (
            "these sorts order Path objects by the platform's own comparison -- "
            f"{_sort_census(offenders, ALL_TREES)}. PurePath.__lt__ compares _str_normcase, which "
            "is lowercased on Windows, so the same directory enumerates in a different order "
            "there and anything derived from that order -- a digest, a golden file, a report -- "
            "differs across platforms. Write sorted(..., key=lambda path: path.as_posix()). On "
            "Linux the two orders are identical, so nothing pinned may move when you change it."
        )


#: ``os`` interfaces the CPython documentation marks *Availability: Unix*. Not
#: exhaustive and deliberately so -- see :class:`TestUnixOnlyInterfaceRule`'s
#: boundary paragraph. Every name here is absent from ``os`` on Windows, so
#: touching it there is an ``AttributeError`` and never a fallback.
UNIX_ONLY_OS_NAMES = frozenset(
    {
        "chroot",
        "chown",
        "confstr",
        "fchdir",
        "fchown",
        "fork",
        "forkpty",
        "fpathconf",
        "fstatvfs",
        "getegid",
        "geteuid",
        "getgid",
        "getgroups",
        "getloadavg",
        "getpgid",
        "getpgrp",
        "getpriority",
        "getsid",
        "getuid",
        "killpg",
        "lchown",
        "mkfifo",
        "mknod",
        "nice",
        "openpty",
        "pathconf",
        "setegid",
        "seteuid",
        "setgid",
        "setgroups",
        "setpgid",
        "setpgrp",
        "setpriority",
        "setsid",
        "setuid",
        "statvfs",
        "sysconf",
        "uname",
        "wait3",
        "wait4",
    }
)

#: The same for ``signal``. The constants matter as much as the functions: a
#: module-level ``signal.SIGKILL`` is an import-time ``AttributeError``.
UNIX_ONLY_SIGNAL_NAMES = frozenset(
    {
        "SIGALRM",
        "SIGCHLD",
        "SIGCONT",
        "SIGHUP",
        "SIGKILL",
        "SIGPIPE",
        "SIGQUIT",
        "SIGSTOP",
        "SIGTSTP",
        "SIGUSR1",
        "SIGUSR2",
        "SIGWINCH",
        "alarm",
        "getitimer",
        "pause",
        "setitimer",
        "siginterrupt",
        "sigwait",
    }
)

#: Standard-library modules that do not exist on Windows at all. An ``import``
#: of one is a failure at *import* time, which is the whole point of the rule.
UNIX_ONLY_MODULES = frozenset(
    {"crypt", "curses", "fcntl", "grp", "nis", "posix", "pty", "pwd", "resource", "spwd", "syslog", "termios", "tty"}
)


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    """Whether *handler* names ``ImportError`` (or its ``ModuleNotFoundError``)."""
    caught = handler.type
    names = caught.elts if isinstance(caught, ast.Tuple) else [caught] if caught is not None else []
    return any(isinstance(name, ast.Name) and name.id in {"ImportError", "ModuleNotFoundError"} for name in names)


@dataclass(frozen=True)
class UnixOnlySite:
    """One Unix-only interface the scan can read, and where it is named."""

    tree: str
    module: str
    """Repo-relative, POSIX-spelled."""
    function: str
    """The enclosing qualified name, or ``<module>``."""
    lineno: int
    name: str
    """The interface as it is spelled, e.g. ``os.geteuid`` or ``import pwd``."""

    def __str__(self) -> str:
        return f"{self.module}:{self.lineno} {self.function}() -> {self.name}"


class _UnixOnlyVisitor(ast.NodeVisitor):
    """Collects every Unix-only ``os``/``signal`` name and Unix-only import."""

    _NAMESPACES = {"os": UNIX_ONLY_OS_NAMES, "signal": UNIX_ONLY_SIGNAL_NAMES}

    def __init__(self, tree: str, module: str) -> None:
        self._tree = tree
        self._module = module
        self._scope: list[str] = []
        self._import_guarded = False
        self.sites: list[UnixOnlySite] = []

    def _scoped(self, node: ast.AST) -> None:
        # The decorator list is evaluated in the *enclosing* scope, at import
        # time, which is the whole distinction this rule exists to make: read it
        # before the name is pushed so the report says `TestSomething` and not
        # `TestSomething.test_...`.
        for decorator in node.decorator_list:  # type: ignore[attr-defined]
            self.visit(decorator)
        self._scope.append(node.name)  # type: ignore[attr-defined]
        for child in ast.iter_child_nodes(node):
            if child not in node.decorator_list:  # type: ignore[attr-defined]
                self.visit(child)
        self._scope.pop()

    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped
    visit_ClassDef = _scoped

    def _record(self, node: ast.AST, name: str) -> None:
        self.sites.append(
            UnixOnlySite(
                tree=self._tree,
                module=self._module,
                function=".".join(self._scope) or "<module>",
                lineno=node.lineno,  # type: ignore[attr-defined]
                name=name,
            )
        )

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name) and node.attr in self._NAMESPACES.get(node.value.id, frozenset()):
            self._record(node, f"{node.value.id}.{node.attr}")
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        # `try: import resource / except ImportError:` is the capability probe
        # itself -- `packages/agent/tests/test_mcp_client.py`'s descriptor census answers
        # "can this host be counted?" by importing and letting the answer be
        # `None`. Only the guarded *body* is exempt; the handlers and the
        # `finally` are read like anything else.
        guarded = any(_catches_import_error(handler) for handler in node.handlers)
        outer, self._import_guarded = self._import_guarded, self._import_guarded or guarded
        for statement in node.body:
            self.visit(statement)
        self._import_guarded = outer
        for child in node.handlers + node.orelse + node.finalbody:
            self.visit(child)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split(".")[0] in UNIX_ONLY_MODULES and not self._import_guarded:
                self._record(node, f"import {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module.split(".")[0] in UNIX_ONLY_MODULES and not self._import_guarded:
            self._record(node, f"from {module} import ...")
        for alias in node.names:
            if alias.name in self._NAMESPACES.get(module, frozenset()):
                self._record(node, f"from {module} import {alias.name}")
        self.generic_visit(node)


def scan_module_unix(path: Path, *, tree: str, module: str) -> list[UnixOnlySite]:
    """Every Unix-only interface one module names."""
    visitor = _UnixOnlyVisitor(tree, module)
    visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
    return visitor.sites


def scan_trees_unix(trees: tuple[str, ...]) -> list[UnixOnlySite]:
    """Every Unix-only interface named in the given repo-relative trees."""
    sites: list[UnixOnlySite] = []
    for tree in trees:
        for path in tree_modules(tree):
            sites.extend(scan_module_unix(path, tree=tree, module=path.relative_to(REPO_ROOT).as_posix()))
    return sites


def _unix_census(offenders: list[UnixOnlySite], trees: tuple[str, ...]) -> str:
    """The per-tree offender count, in the shape the other three rules report."""
    counts = {tree: sum(1 for site in offenders if site.tree == tree) for tree in trees}
    per_tree = ", ".join(f"{tree}={count}" for tree, count in counts.items())
    return f"{len(offenders)} offender(s) ({per_tree})"


class TestUnixOnlyInterfaceRule:
    """No module names an interface Windows does not have.

    A name in a decorator or a class body is evaluated when the module is imported, so on Windows it is a
    collection error that takes every test of the module down. The fix at such a site is a capability probe
    (``mode_bit_support.py``). The rule reads every tree, product and test alike.

    **What this scan does not reach.**

    - The three sets are curated, not derived (on this interpreter every name exists): an unlisted Unix-only
      interface passes silently.
    - An import guarded by ``try``/``except ImportError`` is not an offender, because that shape is the probe; the
      exemption covers the import alone, and no namespace but ``os`` and ``signal`` is read.
    - It resolves no receiver and no alias: ``import os as operating_system`` and ``getattr(os, "geteuid")`` (the
      escape hatch for a guarded call) are not seen.
    - It reads names, never reachability: a call guarded by ``sys.platform`` in a function body is still an offender.
    - An interface that exists on Windows and behaves differently there (``os.chmod``) is a Windows run's to find.
    """

    def test_no_module_names_a_unix_only_interface(self) -> None:
        offenders = scan_trees_unix(ALL_TREES)
        assert [str(site) for site in offenders] == [], (
            "these names do not exist on Windows -- "
            f"{_unix_census(offenders, ALL_TREES)}. Touching one there is an AttributeError, and "
            "in a decorator or a class body it is a collection error that takes the whole module "
            "with it. Probe the capability instead (mode_bit_support.py, symlink_support.py, "
            "filesystem_support.py are the three precedents), or reach the name through "
            "getattr(os, ...) behind a guard and say why."
        )


_SORT_OFFENDING_SOURCE = '''
"""A module written to be found."""
from pathlib import Path


def bare_glob(root: Path) -> list:
    return sorted(root.glob("*.yaml"))


def bare_rglob(root: Path) -> list:
    return sorted(root.rglob("*"))


def bare_iterdir(root: Path) -> list:
    return sorted(root.iterdir())


def bare_comprehension(root: Path) -> list:
    return sorted(path for path in root.glob("*") if path.is_file())


def bare_relative(root: Path) -> list:
    return sorted(path.relative_to(root) for path in root.rglob("*"))
'''

_SORT_COMPLIANT_SOURCE = '''
"""The same module, written the way the rule asks."""
from pathlib import Path


def keyed_glob(root: Path) -> list:
    return sorted(root.glob("*.yaml"), key=lambda path: path.as_posix())


def keyed_unbound(root: Path) -> list:
    return sorted(root.rglob("*"), key=Path.as_posix)


def sorts_strings(root: Path) -> list:
    """Not an offender: the elements are names, not paths."""
    return sorted(path.name for path in root.glob("*"))


def sorts_posix_strings(root: Path) -> list:
    """Nor this one: `as_posix()` has already been called."""
    return sorted(path.as_posix() for path in root.rglob("*"))


def sorts_something_else(mapping: dict) -> list:
    """Nor this one: no path producer anywhere in it."""
    return sorted(mapping.items())
'''


class TestTheSortScanIsNotBlind:
    """Both directions, because a scan that finds nothing looks exactly like a clean tree.

    The compliant module carries the two string sorts that must not be reported --
    an element that is a *name* and one already a POSIX string -- or authors would
    be pushed to add a key to a sort that does not need one.
    """

    def _sites(self, tmp_path: Path, source: str) -> list[SortSite]:
        module = tmp_path / "probe.py"
        module.write_text(source, encoding="utf-8", newline="\n")
        return scan_module_sorts(module, tree="probe", module="probe.py")

    def test_the_offending_module_is_found_in_full(self, tmp_path: Path) -> None:
        offenders = [site for site in self._sites(tmp_path, _SORT_OFFENDING_SOURCE) if not site.keyed]
        assert sorted(site.function for site in offenders) == [
            "bare_comprehension",
            "bare_glob",
            "bare_iterdir",
            "bare_relative",
            "bare_rglob",
        ]

    def test_the_compliant_module_is_silent(self, tmp_path: Path) -> None:
        offenders = [site for site in self._sites(tmp_path, _SORT_COMPLIANT_SOURCE) if not site.keyed]
        assert [str(site) for site in offenders] == []

    def test_the_two_keyed_spellings_are_both_recognised(self, tmp_path: Path) -> None:
        """``key=Path.as_posix`` and the lambda: the repository uses both."""
        keyed = {site.function for site in self._sites(tmp_path, _SORT_COMPLIANT_SOURCE) if site.keyed}
        assert keyed == {"keyed_glob", "keyed_unbound"}

    def test_the_scan_reaches_every_tree_and_still_finds_a_population(self) -> None:
        """A rule whose scan quietly reached nothing would read exactly like a clean tree.

        Tree coverage is asserted on the **file walk**, not on the sites: some
        trees carry no path sort at all, and pinning a set of *site-bearing* trees
        would go red the day one of them gained a legitimate sort. The population
        anchor is every path sort whatever its key, not the offender list, which is
        meant to be empty.
        """
        for tree in ALL_TREES:
            modules_in_tree = tree_modules(tree)
            assert modules_in_tree != [], f"the glob reached no module in {tree}; the tree has moved or been renamed"

        sites = scan_trees_sorts(ALL_TREES)
        assert len(sites) >= 90, f"the sort scan found only {len(sites)} path sort(s); it has gone blind"
        assert {site.tree for site in sites} >= {
            "packages/tools/src",
            "scripts",
            "packages/tools/tests",
            "packages/mcp/tests",
            "packages/agent/tests",
        }, "a tree that carried path sorts stopped producing any"


_PATH_OFFENDING_SOURCE = '''
"""A module written to be found."""
from pathlib import Path


def key_a_dict(root: Path) -> dict:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")}


def build_a_set(root: Path) -> set:
    return {str(p.relative_to(root)) for p in root.rglob("*.yaml")}


def replaced_by_hand(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\\\", "/")


class Holder:
    def digest(self, path: Path, root: Path) -> bytes:
        return str(path.relative_to(root)).encode("utf-8")
'''

_PATH_COMPLIANT_SOURCE = '''
"""A module written to be left alone."""
from pathlib import Path


def already_posix(root: Path) -> set:
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


def str_of_posix(path: Path, root: Path) -> str:
    return str(path.relative_to(root).as_posix())


def kept_as_path(path: Path, root: Path) -> bool:
    return path.relative_to(root) == Path("interfaces") / "ISpeed.yaml"
'''


class TestThePathScanIsNotBlind:
    """Rule P is held to the same two directions as Rules E and N."""

    def test_a_synthetic_offender_is_found_in_every_shape(self, tmp_path: Path) -> None:
        path = tmp_path / "path_offender.py"
        path.write_text(_PATH_OFFENDING_SOURCE, encoding="utf-8", newline="\n")
        sites = scan_module_paths(path, tree="synthetic", module="path_offender.py")

        assert {site.function for site in sites if site.stringified} == {
            "key_a_dict",
            "build_a_set",
            "replaced_by_hand",
            "Holder.digest",
        }, f"the scan read {sorted((site.function, site.stringified) for site in sites)}"

    def test_the_posix_spellings_are_left_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "path_compliant.py"
        path.write_text(_PATH_COMPLIANT_SOURCE, encoding="utf-8", newline="\n")
        sites = scan_module_paths(path, tree="synthetic", module="path_compliant.py")

        assert [str(site) for site in sites if site.stringified] == [], (
            "as_posix(), str() around an as_posix() and a relative_to() kept as a Path are all "
            "correct spellings and none of them may be flagged"
        )
        assert {site.function for site in sites} == {
            "already_posix",
            "str_of_posix",
            "kept_as_path",
        }, f"the scan read {sorted(site.function for site in sites)}"

    def test_the_real_scan_still_reaches_every_tree(self) -> None:
        """A green Rule P and a Rule P reading nothing look identical.

        The anchor is therefore the *population* -- every ``relative_to`` call
        in the trees, whatever its spelling -- and not the offender list, which is
        meant to be empty. Tree coverage is asserted on the **file walk**, not on
        the sites, because some trees contain no ``relative_to`` call at all and
        pinning a set of *site-bearing* trees would go red the day one of them
        gained a legitimate one.
        """
        for tree in ALL_TREES:
            modules_in_tree = tree_modules(tree)
            assert modules_in_tree != [], f"the glob reached no module in {tree}; the tree has moved or been renamed"

        sites = scan_trees_paths(ALL_TREES)
        assert len(sites) >= 100, f"the path scan found only {len(sites)} relative_to call(s); it has gone blind"
        assert {site.tree for site in sites} >= {
            "packages/tools/src",
            "scripts",
            "packages/tools/tests",
            "packages/mcp/tests",
            "packages/agent/tests",
        }, "a tree that carried relative_to calls stopped producing any"
        modules = {site.module for site in sites}
        assert modules >= {
            "packages/tools/src/swcraft_tools/model_state.py",
            "packages/tools/tests/support/editing_vector_fixtures.py",
        }, "the scan no longer reaches two modules known to carry relative_to calls"


# ---------------------------------------------------------------------------
# The scan is not blind -- in both directions
# ---------------------------------------------------------------------------


_OFFENDING_SOURCE = '''
"""A module written to be found."""
import subprocess
from pathlib import Path


def bare_read(path: Path) -> str:
    return path.read_text()


def bare_write(path: Path, text: str) -> None:
    path.write_text(text)


def bare_builtin_open(path: str) -> str:
    with open(path) as handle:
        return handle.read()


def bare_path_open(path: Path, text: str) -> None:
    with path.open("w") as handle:
        handle.write(text)


class Holder:
    def bare_append(self, path: Path, text: str) -> None:
        with path.open("a") as handle:
            handle.write(text)


def bare_subprocess() -> str:
    return subprocess.run(["git", "status"], text=True, capture_output=True).stdout
'''

_COMPLIANT_SOURCE = '''
"""A module written to be left alone."""
import subprocess
import tarfile
import webbrowser
from pathlib import Path


def declared_read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def declared_write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\\n")


def declared_open(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\\n") as handle:
        handle.write(text)


def binary_write(path: Path, payload: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(payload)


def declared_subprocess() -> str:
    return subprocess.run(["git", "status"], text=True, encoding="utf-8", capture_output=True).stdout


def not_a_file(session, archive, url):
    session.open()
    webbrowser.open(url)
    return tarfile.open(fileobj=archive, mode="r|")
'''


class TestTheScanIsNotBlind:
    """A scan that finds nothing passes, and so does one looking in the wrong place."""

    def test_a_synthetic_offender_is_found_in_every_shape(self, tmp_path: Path) -> None:
        path = tmp_path / "offender.py"
        path.write_text(_OFFENDING_SOURCE, encoding="utf-8", newline="\n")
        sites = scan_module(path, tree="synthetic", module="offender.py")

        found = {(site.function, site.kind, site.writes) for site in sites}
        assert found == {
            ("bare_read", "read_text", False),
            ("bare_write", "write_text", True),
            ("bare_builtin_open", "open", False),
            ("bare_path_open", "open", True),
            ("Holder.bare_append", "open", True),
            ("bare_subprocess", "subprocess", False),
        }, f"the scan read {sorted(found)}"

        assert [site for site in sites if _keyword(site.call, "encoding") is None] == sites, (
            "Rule E must flag every one of them"
        )
        newline_offenders = {site.function for site in sites if site.writes and _keyword(site.call, "newline") is None}
        assert newline_offenders == {"bare_write", "bare_path_open", "Holder.bare_append"}

    def test_the_compliant_forms_are_left_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "compliant.py"
        path.write_text(_COMPLIANT_SOURCE, encoding="utf-8", newline="\n")
        sites = scan_module(path, tree="synthetic", module="compliant.py")

        assert [site for site in sites if _keyword(site.call, "encoding") is None] == []
        assert [site for site in sites if site.writes and _keyword(site.call, "newline") is None] == []
        assert {site.function for site in sites} == {
            "declared_read",
            "declared_write",
            "declared_open",
            "declared_subprocess",
        }, (
            "a binary open, session.open(), webbrowser.open(url) and tarfile.open(mode='r|') are all "
            "outside these rules and must not be read as text file I/O"
        )

    def test_the_real_scan_finds_the_sites_it_is_meant_to_police(self) -> None:
        """Without this, a renamed tree or a moved module would empty the scan and both rules pass."""
        product = scan_trees(PRODUCT_TREES)
        assert len(product) >= 80, f"the product scan found only {len(product)} site(s); it has gone blind"
        modules = {site.module for site in product}
        assert modules >= {
            "packages/tools/src/swcraft_tools/reader.py",
            "packages/tools/src/swcraft_tools/model_state.py",
            "packages/tools/src/swcraft_tools/writer/mutations.py",
            "packages/tools/src/swcraft_tools/writer/arxml_xml.py",
            "scripts/build_artifacts.py",
            # Runs on Windows by design; `scripts/` is in PRODUCT_TREES, so it is
            # held from its first line.
            "scripts/workstation/install.py",
        }

        # The template ships no Python, asserted in both directions so neither
        # half can pass by accident: adding a script back fails the first;
        # adding one the glob does not reach fails the second.
        template_root = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools" / "templates"
        template_scripts = sorted(path.as_posix() for path in template_root.rglob("*.py"))
        assert template_scripts == [], (
            f"the template ships Python again ({template_scripts}); it is IN scope for these rules "
            "— add it to the citation above rather than deleting this assertion"
        )
        assert [site for site in product if "/templates/" in site.module] == [], (
            "the scan found a site under templates/ that the tree above says is not there"
        )

        tests = scan_trees(TEST_TREES)
        assert len(tests) >= 1000, f"the test-tree scan found only {len(tests)} site(s); it has gone blind"
        assert {site.tree for site in tests} == set(TEST_TREES), "one of the three test trees went unread"


class TestTheUnixOnlyScanIsNotBlind:
    """The scan finds what it is meant to find, and misses what it says it misses."""

    def test_the_offending_shapes_are_all_found(self, tmp_path: Path) -> None:
        module = tmp_path / "offender.py"
        module.write_text(_UNIX_ONLY_OFFENDING_SOURCE, encoding="utf-8", newline="\n")

        found = {site.name for site in scan_module_unix(module, tree="synthetic", module="offender.py")}

        assert found == {
            "os.geteuid",
            "os.uname",
            "signal.SIGKILL",
            "import pwd",
            "from os import getuid",
            "from resource import ...",
        }

    def test_the_decorator_site_is_found_at_class_scope(self, tmp_path: Path) -> None:
        """The shape that breaks: a name evaluated on import, in a class body, with no function frame."""
        module = tmp_path / "offender.py"
        module.write_text(_UNIX_ONLY_OFFENDING_SOURCE, encoding="utf-8", newline="\n")

        sites = scan_module_unix(module, tree="synthetic", module="offender.py")
        decorator = [site for site in sites if site.name == "os.geteuid"]

        assert [site.function for site in decorator] == ["TestSomething"], (
            "the enclosing scope has to be reported, because a name in a class body runs at import "
            "time and a name in a method does not"
        )

    def test_the_compliant_shapes_are_all_ignored(self, tmp_path: Path) -> None:
        module = tmp_path / "compliant.py"
        module.write_text(_UNIX_ONLY_COMPLIANT_SOURCE, encoding="utf-8", newline="\n")

        assert scan_module_unix(module, tree="synthetic", module="compliant.py") == [], (
            "os.access, os.environ, signal.SIGINT, a platform import and the documented "
            "getattr() escape hatch are all portable and must not be policed"
        )

    def test_the_walk_reaches_every_tree(self) -> None:
        """Without this, a renamed tree would empty the scan and the rule would pass reading nothing."""
        walked = {tree for tree in ALL_TREES if any((REPO_ROOT / tree).rglob("*.py"))}

        assert walked == set(ALL_TREES), "one of the seven trees went unread"


_UNIX_ONLY_OFFENDING_SOURCE = '''
"""A module written to be found."""
import os
import pwd
import signal
from os import getuid
from resource import getrusage


class TestSomething:
    @skipif(os.geteuid() == 0, reason="the shape that broke the Windows run")
    def test_needs_a_real_uid(self):
        return getuid()


def machine() -> str:
    return os.uname().machine


def stop(pid: int) -> None:
    os.kill(pid, signal.SIGKILL)


def usage():
    return getrusage(0)
'''


_UNIX_ONLY_COMPLIANT_SOURCE = '''
"""A module written to be ignored."""
import os
import platform
import signal


def readable(path) -> bool:
    return os.access(path, os.R_OK)


def home() -> str:
    return os.environ["HOME"]


def system() -> str:
    return platform.system()


def interrupt(pid: int) -> None:
    os.kill(pid, signal.SIGINT)


def uid_or_none():
    # The documented escape hatch: reached by string, guarded, and stated.
    getter = getattr(os, "geteuid", None)
    return None if getter is None else getter()


def census():
    # The probe idiom: the import is the question, and None is an answer.
    try:
        import resource
    except ImportError:
        return None
    return resource.getrlimit(resource.RLIMIT_NOFILE)
'''
