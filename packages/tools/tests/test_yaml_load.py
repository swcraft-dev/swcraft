"""libyaml is the one reader of every YAML file SWCraft reads (:mod:`swcraft_tools.yaml_load`).

The leaf parses with ``yaml.CSafeLoader`` and every product read calls it. This module holds:

* **where the pure-Python and libyaml parsers differ**, pinned on the reader's side:
  shapes libyaml reads that the pure parser refused or read differently, and one
  representative of each of the six classes libyaml refuses (the leaf's docstring
  says which follow YAML 1.2.2 and which deviate from it);
* **the writers' dump** never emits a refused shape, so those reach a hand-written file only;
* **the nesting bound** -- deeper than 1 000 collections is a ``yaml.YAMLError``, never a crash;
* **where they agree**, and **no fallback** to the pure parser;
* **the build** -- PyYAML carries libyaml, and one without it is refused at import;
* **the leaf** -- it imports nothing from ``swcraft_tools``, and no other module of the source
  trees or ``scripts/`` reaches a ``yaml`` parse function or reader class;
* **the parse cache** -- one parse per distinct text, independent copies, the size bound,
  thread coherence, and one parse per text on ``check-model``.

What it does **not** reach: YAML outside the pinned shapes, and the random comparison
and corpus check that found the classes (re-run when PyYAML or libyaml moves); a
class beyond those is not enumerated here. Nor the crash depth on another platform's
stack (Windows' is unmeasured).
"""

from __future__ import annotations

import ast
import importlib.util
import shutil
import subprocess
import sys
import threading
import types
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from conftest import dev_module
from swcraft_tools import yaml_load
from swcraft_tools.cli import main
from swcraft_tools.model_state import load_model_state
from swcraft_tools.yaml_load import clear_parse_cache, load_yaml_document, parse_cache_info

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE = REPO_ROOT / "packages" / "tools" / "src" / "swcraft_tools"


def _source_trees(root: Path) -> tuple[Path, ...]:
    """The three source trees and ``scripts/``, under *root*, which the leaf gate scans.

    A function so a test can plant one elsewhere.
    """
    return (
        root / "packages" / "tools" / "src" / "swcraft_tools",
        root / "packages" / "mcp" / "src" / "swcraft_mcp",
        root / "packages" / "agent" / "src" / "swcraft_agent",
        root / "scripts",
    )


SOURCE_TREES = _source_trees(REPO_ROOT)

#: The Python project whose sources the leaf rule does **not** reach, with the reason.
#: The specimen generator is an independent oracle: it writes the models SWCraft's reader
#: judges, so it may not import ``swcraft_tools`` and parses YAML itself; a shape the two
#: readers take differently is a finding of that oracle, never a second reader inside SWCraft.
NOT_REACHED_BY_THE_LEAF = {
    "specimens": "an independent oracle that may not import swcraft_tools",
}

#: Every ``yaml`` function that parses a text. A call of any of them outside the
#: leaf would be a second reader.
_PARSE_FUNCTIONS = frozenset(
    {
        "load",
        "safe_load",
        "load_all",
        "safe_load_all",
        "full_load",
        "full_load_all",
        "unsafe_load",
        "unsafe_load_all",
        "compose",
        "compose_all",
        "parse",
        "scan",
    }
)


def _is_a_reader_class(attribute: str) -> bool:
    """A ``yaml`` loader or parser class: instantiating one outside the leaf is a second reader."""
    return attribute.endswith(("Loader", "Parser", "Scanner", "Reader", "Composer"))


# ---------------------------------------------------------------------------
# Where the two parsers differ: the reader's side, pinned
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("a:\t1\n", {"a": 1}),
        ("a: 1\t\n", {"a": 1}),
        ("a:\tb\n", {"a": "b"}),
        ("- x\ty\n", ["x\ty"]),
        ("a: [x,\ty]\n", {"a": ["x", "y"]}),
        ("a: |#\n  x\n", {"a": "x\n"}),
        ("a: >#\n  x\n", {"a": "x\n"}),
        ("a: |2#\n   x\n", {"a": " x\n"}),
        ("{y3?}\n", {"y3?": None}),
        ('[2"? x]\n', ['2"? x']),
    ],
    ids=[
        "tab-after-colon",
        "trailing-tab",
        "tab-before-plain-scalar",
        "tab-inside-plain-scalar",
        "tab-in-flow-sequence",
        "literal-header-then-comment",
        "folded-header-then-comment",
        "indented-header-then-comment",
        "question-mark-in-flow-mapping-key",
        "question-mark-in-flow-sequence-item",
    ],
)
def test_a_shape_the_pure_parser_refused_is_read(text: str, value: object) -> None:
    with pytest.raises(yaml.YAMLError):
        yaml.load(text, Loader=yaml.SafeLoader)

    assert load_yaml_document(text) == value


def test_a_lone_non_specific_tag_reads_as_the_empty_string() -> None:
    assert yaml.load("a: !\n", Loader=yaml.SafeLoader) == {"a": None}

    assert load_yaml_document("a: !\n") == {"a": ""}


@pytest.mark.parametrize("text", ["\ufeff\ufeffa: 1\n", "\n\ufeffa: 1\n"], ids=["second-bom", "bom-after-a-line-break"])
def test_a_byte_order_mark_before_the_first_node_is_dropped_not_read_as_content(text: str) -> None:
    """libyaml takes a U+FEFF at the start of a line before a document's first node for the document's BOM."""
    assert yaml.load(text, Loader=yaml.SafeLoader) == {"\ufeffa": 1}

    assert load_yaml_document(text) == {"a": 1}


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("a: 1\n\ufeff# c\nb: 2\n", {"a": 1, "b": 2}),
        ("a: 1\n\ufeff\nb: 2\n", {"a": 1, "b": 2}),
        ("a: 1\n\ufeff", {"a": 1}),
        ("p:\n\ufeff - x\n  - y\n", {"p": ["x", "y"]}),
        ("p:\n\ufeff- x\n", {"p": ["x"]}),
        ("a:\n\ufeff b\n", {"a": "b"}),
        (" a: 1\n\ufeffb: 2\n", {"a": 1, "b": 2}),
    ],
    ids=[
        "before-a-comment",
        "on-a-blank-line",
        "at-the-end-of-the-text",
        "for-one-indent-space",
        "a-dash-at-column-1",
        "a-value-at-column-2",
        "a-key-at-its-mappings-column",
    ],
)
def test_a_byte_order_mark_at_column_0_past_the_first_node_is_skipped_between_tokens(text: str, value: object) -> None:
    """§ 5.2 excludes them all and libyaml reads them all: the mark is one column, no refusal of its own."""
    with pytest.raises(yaml.YAMLError):
        yaml.load(text, Loader=yaml.SafeLoader)

    assert load_yaml_document(text) == value


@pytest.mark.parametrize(
    ("text", "value", "pure"),
    [
        ("p:\n a: 1\n\ufeffb: 2\n", {"p": {"a": 1, "b": 2}}, {"p": {"a": 1}, "\ufeffb": 2}),
        ("a: [x,\n\ufeffy]\n", {"a": ["x", "y"]}, {"a": ["x", "\ufeffy"]}),
        ("a: [1,\n\ufeff2]\n", {"a": [1, 2]}, {"a": [1, "\ufeff2"]}),
        ("[a,\n\ufeff b]\n", ["a", "b"], ["a", "\ufeff b"]),
    ],
    ids=[
        "key-of-a-nested-mapping",
        "flow-item",
        "flow-integer",
        "flow-item-behind-a-space",
    ],
)
def test_a_byte_order_mark_past_the_first_node_can_read_to_another_shape(
    text: str, value: object, pure: object
) -> None:
    """Where the pure parser reads the mark into a scalar, libyaml skips it; the shapes differ, neither refuses."""
    assert yaml.load(text, Loader=yaml.SafeLoader) == pure

    assert load_yaml_document(text) == value


def test_a_key_behind_a_mark_under_an_empty_value_nests_one_level_deeper() -> None:
    """The mark is the column that makes the key the value of the line before, not its sibling."""
    text = "p:\n\ufeffa: 1\n"
    assert yaml.load(text, Loader=yaml.SafeLoader) == {"p": None, "\ufeffa": 1}

    assert load_yaml_document(text.replace("\ufeff", "")) == {"p": None, "a": 1}
    assert load_yaml_document(text) == {"p": {"a": 1}}


@pytest.mark.parametrize(
    "text",
    [
        "description: |\n  \tfirst line led by a tab\n  second line\n",
        "description: >\n  \tfirst line led by a tab\n",
        "- >\n \t\n detected\n",
        "k: {a:}\n",
        "k: {a:, b: 1}\n",
        "k: [a:]\n",
        "k: [a:, b]\n",
        "k: [? ]\n",
        "k: [?]\n",
        '%FOO  bar baz # Should be ignored\n               # with a warning.\n--- "foo"\n',
        "a: 1\n\ufeffb: 2\n",
        "a:\n  b: 1\n\ufeff  c: 2\n",
        "\n\ufeffid: X\nsatisfies:\n",
        "---\n\ufeffb: 2\nc: 3\n",
    ],
    ids=[
        "literal-block-first-line-led-by-a-tab",
        "folded-block-first-line-led-by-a-tab",
        "spec-example-8-2-tab-in-a-leading-empty-line",
        "flow-mapping-omitted-value-before-the-brace",
        "flow-mapping-omitted-value-before-a-comma",
        "flow-pair-omitted-value-before-the-bracket",
        "flow-pair-omitted-value-before-a-comma",
        "flow-pair-explicit-empty-key",
        "flow-pair-bare-question-mark",
        "spec-example-6-13-reserved-directive",
        "byte-order-mark-before-a-key-right-of-its-mapping",
        "byte-order-mark-before-a-key-right-of-its-nested-mapping",
        "byte-order-mark-leaving-the-next-key-left-of-its-mapping",
        "byte-order-mark-leaving-the-next-key-left-of-its-mapping-behind-a-marker",
    ],
)
def test_a_shape_the_pure_parser_read_is_refused_with_no_fallback(text: str) -> None:
    """The classes libyaml refuses and the pure parser read, each by a representative (no acceptance path).

    Against YAML 1.2.2, libyaml deviates on the first four classes and follows it on
    the fifth: a tab after the indentation on a block scalar's first lines (§ 8.1.1.1
    takes the indentation from the leading *spaces*; Example 8.2 reads the third
    case as ``"\\t\\ndetected\\n"``); an omitted value directly before a flow indicator
    (production [147], Example 7.17's ``omitted value:,``); an explicit ``?`` flow
    pair with both nodes empty (production [150] admits ``[? ]``, while its
    ``s-separate`` excludes ``[?]``); a reserved directive (§ 6.8: ignored with a
    warning, Example 6.13); and a U+FEFF that leaves what follows it outside the block it
    falls in, on either side of an open mapping, where § 5.2 admits a mark only at a
    document's start or inside a quoted scalar. The writers never produce any of them
    (the next test).
    """
    yaml.load(text, Loader=yaml.SafeLoader)

    with pytest.raises(yaml.YAMLError):
        load_yaml_document(text)


@pytest.mark.parametrize(
    "value",
    [
        {"description": "\tfirst line led by a tab\n"},
        {"description": "line\n\ttab-led line\n", "code": "\tif x:\n\t\ty()\n"},
        {"k": "\t"},
        {"k": {"a": None}, "l": [None, "", {"a": None}, {None: None}], "e": [], "m": {}},
        {"%FOO": "%FOO bar", "list": ["%YAML 1.1", "? x", "a:", "[?]"]},
        {"\ufeffk": "a\n\ufeffb", "v": "\ufeff"},
    ],
    ids=["tab-led-string", "tab-led-lines", "lone-tab", "empty-entries", "directive-like-strings", "byte-order-marks"],
)
def test_the_writers_dump_never_emits_a_refused_shape(value: object) -> None:
    """``yaml.safe_dump`` with the writers' settings quotes every tab and BOM and emits no flow entry or directive.

    The values are the refused classes' raw material; each dump reads back equal
    through the leaf. What it does not reach: values outside these.
    """
    text = yaml.safe_dump(value, default_flow_style=False, sort_keys=False)

    assert load_yaml_document(text) == value


@pytest.mark.parametrize(
    "text", ['a: "\\uD800"\n', 'a: "\\uD83D\\uDE00"\n'], ids=["lone-surrogate", "surrogate-pair-escaped"]
)
def test_an_escaped_surrogate_is_refused_with_no_fallback(text: str) -> None:
    """The specification excludes a surrogate from a stream; the pure parser's answer is not consulted."""
    assert isinstance(yaml.load(text, Loader=yaml.SafeLoader)["a"], str)

    with pytest.raises(yaml.YAMLError, match="quoted scalar"):
        load_yaml_document(text)


# ---------------------------------------------------------------------------
# Where they agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [('a: "\\x01"\n', {"a": "\x01"}), ("a: em \u2014 dash\n", {"a": "em \u2014 dash"})],
    ids=["control-character-escape", "raw-em-dash"],
)
def test_a_shape_both_parsers_read_reads_the_same(text: str, value: object) -> None:
    assert yaml.load(text, Loader=yaml.SafeLoader) == value

    assert load_yaml_document(text) == value


def test_a_refused_document_raises() -> None:
    with pytest.raises(yaml.YAMLError):
        load_yaml_document("a: [unclosed\n")


# ---------------------------------------------------------------------------
# The nesting bound: a deep document is refused, never a crash
# ---------------------------------------------------------------------------

#: A fresh interpreter reads stdin through the leaf: exit 0 with the refusal on
#: stdout when it is a ``yaml.YAMLError``, 3 when the text was read; a crash is
#: any other code (SIGSEGV was 139 / -11 before the bound).
_DEEP_PROBE = """
import sys, yaml
from swcraft_tools.yaml_load import load_yaml_document
try:
    load_yaml_document(sys.stdin.read())
except yaml.YAMLError as exc:
    print(type(exc).__name__, str(exc).splitlines()[0])
    sys.exit(0)
sys.exit(3)
"""


def _nested(shape: str, depth: int) -> str:
    if shape == "flow-sequence":
        return "a: " + "[" * depth + "]" * depth + "\n"
    if shape == "flow-mapping":
        return "a: " + "{b: " * depth + "x" + "}" * depth + "\n"
    return "- " * depth + "x\n"


@pytest.mark.parametrize(
    ("shape", "depth"),
    [
        ("flow-sequence", 1001),
        ("flow-sequence", 60000),
        ("flow-mapping", 60000),
        ("compact-block-sequence", 60000),
    ],
    ids=["just-above-the-bound", "flow-sequence-at-the-old-crash-depth", "flow-mapping", "compact-block-sequence"],
)
def test_a_document_nested_deeper_than_the_bound_is_refused_not_a_crash(shape: str, depth: int) -> None:
    """libyaml's composer recursed in C: 60 000 levels killed the interpreter (SIGSEGV) before the bound."""
    result = subprocess.run(
        [sys.executable, "-c", _DEEP_PROBE],
        input=_nested(shape, depth),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )

    assert result.returncode == 0, (result.returncode, result.stderr[-2000:])
    assert result.stdout.startswith(
        f"NestingTooDeepError found a collection nested deeper than {yaml_load._NESTING_LIMIT} levels"
    )
    assert "/" not in result.stdout and "\\" not in result.stdout


_OLD_DEEPEST_PROBE = """
import sys, yaml
from swcraft_tools.yaml_load import load_yaml_document
shape = sys.argv[1]
def text(depth):
    if shape == "flow-sequence":
        return "a: " + "[" * depth + "]" * depth + "\\n"
    return "".join("  " * i + "k:\\n" for i in range(depth - 1)) + "  " * (depth - 1) + "k: x\\n"
def old_reads(depth):
    try:
        yaml.load(text(depth), Loader=yaml.SafeLoader)
    except RecursionError:
        return False
    return True
low, high = 1, 4000
while high - low > 1:
    middle = (low + high) // 2
    low, high = (middle, high) if old_reads(middle) else (low, middle)
assert load_yaml_document(text(low)) == yaml.load(text(low), Loader=yaml.SafeLoader)
print(low)
"""


@pytest.mark.parametrize("shape", ["flow-sequence", "block-mapping"])
def test_the_deepest_document_the_pure_parser_read_is_read_the_same(shape: str) -> None:
    """The bound refuses nothing the reader before libyaml returned a value for (it raised RecursionError beyond)."""
    result = subprocess.run(
        [sys.executable, "-c", _OLD_DEEPEST_PROBE, shape],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert int(result.stdout) < yaml_load._NESTING_LIMIT


def test_the_bound_itself_is_admitted_and_one_more_is_refused() -> None:
    at_the_bound = _nested("flow-sequence", yaml_load._NESTING_LIMIT - 1)  # plus the top-level mapping
    above = _nested("flow-sequence", yaml_load._NESTING_LIMIT)

    yaml_load._refuse_deep_nesting(at_the_bound)
    with pytest.raises(yaml_load.NestingTooDeepError, match=rf"deeper than {yaml_load._NESTING_LIMIT} levels"):
        load_yaml_document(above)


def test_a_shallow_document_with_more_indicators_than_the_bound_is_read_through_the_walk() -> None:
    text = "items:\n" + "- name: x\n  kind: y\n" * 600
    assert sum(text.count(indicator) for indicator in yaml_load._COLLECTION_INDICATORS) > yaml_load._NESTING_LIMIT

    assert load_yaml_document(text) == {"items": [{"name": "x", "kind": "y"}] * 600}


def test_an_error_met_on_the_walk_is_the_composing_loads_own() -> None:
    """Within the bound the walk changes no message: an undefined alias before a later syntax error is the answer."""
    text = "- *undefined\n" + "- x\n" * 1200 + "- [unclosed\n"
    with pytest.raises(yaml.YAMLError) as direct:
        yaml.load(text, Loader=yaml.CSafeLoader)

    with pytest.raises(yaml.YAMLError) as through_the_leaf:
        load_yaml_document(text)

    assert type(through_the_leaf.value) is type(direct.value)
    assert str(through_the_leaf.value) == str(direct.value)


#: A depth inside the bound that no read can return: libyaml composes it, the deep
#: copy raised ``RecursionError`` (it returns at most about 498 levels).
_TOO_DEEP_TO_RETURN = 700


@pytest.mark.parametrize(
    ("shape", "collections"),
    [("flow-sequence", _TOO_DEEP_TO_RETURN + 1), ("compact-block-sequence", _TOO_DEEP_TO_RETURN)],
    ids=["flow-sequence", "compact-block-sequence"],
)
def test_a_document_too_deep_to_return_is_refused_as_a_yaml_error(
    shape: str, collections: int, cleared_cache: None
) -> None:
    """Between the copy's depth and the bound the deep copy's ``RecursionError`` is a ``NestingTooDeepError``."""
    with pytest.raises(yaml_load.NestingTooDeepError) as refused:
        load_yaml_document(_nested(shape, _TOO_DEEP_TO_RETURN))

    assert isinstance(refused.value, yaml.YAMLError)
    assert isinstance(refused.value.__cause__, RecursionError)
    message = str(refused.value)
    assert message.startswith(
        f"found a collection nested {collections} levels deep, too deep for this reader to return "
        f'(it returns about {yaml_load._RETURNED_NESTING_ABOUT} levels)\n  in "<unicode string>", line 1, column '
    )
    assert "recursion" not in message
    assert "/" not in message and "\\" not in message


def test_a_document_too_deep_to_return_is_held_and_refused_the_same_on_every_read(parses: list[str]) -> None:
    """The composed document stays in the cache: a second read is a hit and raises the first read's refusal."""
    text = _nested("flow-sequence", _TOO_DEEP_TO_RETURN)

    refusals = []
    for _ in range(2):
        with pytest.raises(yaml_load.NestingTooDeepError) as refused:
            load_yaml_document(text)
        refusals.append(str(refused.value))

    assert refusals[0] == refusals[1]
    assert parses == [text]
    info = parse_cache_info()
    assert (info.hits, info.misses, info.currsize) == (1, 1, 1)


def test_a_text_over_the_cache_limit_too_deep_to_copy_is_returned_uncopied(parses: list[str]) -> None:
    """The stated limit: the uncached path makes no copy, so libyaml's value comes back up to the bound."""
    text = _nested("flow-sequence", _TOO_DEEP_TO_RETURN) + "#" + "x" * yaml_load._CACHED_TEXT_LIMIT + "\n"

    value = load_yaml_document(text)

    assert isinstance(value, dict) and list(value) == ["a"]
    assert parse_cache_info().currsize == 0


@pytest.mark.parametrize("command", ["check-model", "validate"])
def test_a_model_file_too_deep_to_return_is_an_unparseable_element_file(command: str, tmp_path: Path) -> None:
    """A model file nested 700 levels: exit 1 naming the refusal, not ``maximum recursion depth exceeded``.

    In a fresh interpreter, as the command runs: before the refusal the deep copy's
    ``RecursionError`` reached ``cli.main``'s catch-all and its message was printed.
    """
    project = tmp_path / "basic"
    shutil.copytree(REPO_ROOT / "examples" / "basic", project)
    element = project / "model" / "data_types" / "base" / "bool_base.yaml"
    deep = "deep: " + "[" * _TOO_DEEP_TO_RETURN + "]" * _TOO_DEEP_TO_RETURN + "\n"
    element.write_text(element.read_text(encoding="utf-8") + deep, encoding="utf-8", newline="\n")

    result = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", command, "--project", str(project / "swcraft.yaml")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output[-2000:]
    assert "Invalid base type YAML in " in output
    # A YAML error names its line and column: the appended `deep:` key is the file's sixth line, and the
    # 701st opening bracket is at column 706.
    assert (
        "bool_base.yaml: line 6, column 706: found a collection nested 701 levels deep, too deep for this reader "
        "to return"
    ) in output
    assert "maximum recursion depth" not in output
    assert "Traceback" not in output


@pytest.mark.parametrize("text", ["name: a\udcff\n", "name: \ud800\n"], ids=["escaped-byte", "lone-high-surrogate"])
def test_a_raw_surrogate_in_the_text_is_refused_as_a_yaml_error(text: str) -> None:
    """libyaml encodes the text first; the codec's UnicodeEncodeError is answered as the reader's refusal.

    A ``surrogateescape`` argv string or directory name is where such a text comes
    from (``swcraft init --name``); the leaf's contract is parse or ``yaml.YAMLError``.
    """
    with pytest.raises(yaml.YAMLError, match="unacceptable character"):
        load_yaml_document(text)


# ---------------------------------------------------------------------------
# The build and the leaf
# ---------------------------------------------------------------------------


def test_pyyaml_carries_libyaml_and_the_reader_resolved_it() -> None:
    """The dev venv and both CI runners install PyPI's binary wheel; a pure-only build is red here."""
    assert yaml.__with_libyaml__
    assert hasattr(yaml, "CSafeLoader")
    assert "_LOADER" not in vars(yaml_load), "the leaf names libyaml directly; no resolved fallback"


def test_the_leaf_imports_nothing_from_swcraft_tools() -> None:
    tree = ast.parse((PACKAGE / "yaml_load.py").read_text(encoding="utf-8"))
    imported = [
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ]
    assert [name for name in imported if name and name.startswith("swcraft_tools")] == []
    # A relative import is one from the package too, whatever it names.
    assert [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level > 0] == []


def test_every_python_project_is_scanned_or_excepted_by_name() -> None:
    """Every project ``dev.py`` builds is scanned or excepted by name.

    It is either one of :data:`SOURCE_TREES` or named in :data:`NOT_REACHED_BY_THE_LEAF`; a fifth project joining
    silently would be neither."""
    packages = REPO_ROOT / "packages"
    scanned = {tree.relative_to(packages).parts[0] for tree in SOURCE_TREES if tree.is_relative_to(packages)}
    assert scanned.isdisjoint(NOT_REACHED_BY_THE_LEAF)
    assert scanned | set(NOT_REACHED_BY_THE_LEAF) == set(dev_module().PYTHON_PROJECTS)


def test_no_module_of_the_source_trees_or_scripts_parses_yaml_outside_the_leaf() -> None:
    """Every read goes through the leaf, so a file one command reads is read the same way by every other.

    Under whatever name a module binds ``yaml`` to (``import yaml`` or ``import
    yaml as y``), it refuses: a reference to a parse function (called or not), a
    reference to a loader, parser, scanner, reader or composer class (so a direct
    ``yaml.CSafeLoader(text).get_single_data()`` too), a ``getattr`` on the
    module, any ``from yaml...`` import and any ``import yaml.<submodule>``.

    What it does not reach: the ``yaml`` module reached by any other route --
    ``importlib.import_module("yaml")``, ``sys.modules["yaml"]``, an attribute of
    another module that re-exports it. None of those occurs in the three
    source trees or ``scripts/`` today; under ``scripts/`` only ``*.py`` is scanned (the shell and ``.cmd``
    wrappers are not Python), and ``scripts/tests/`` is not: it is test code, like the projects'
    ``tests/`` trees, which no tree here reaches.
    """
    leaf = PACKAGE / "yaml_load.py"
    offenders: list[str] = []
    for tree_root in SOURCE_TREES:
        for source in sorted(tree_root.rglob("*.py"), key=lambda path: path.as_posix()):
            if source == leaf or source.is_relative_to(tree_root / "tests"):
                continue
            module = ast.parse(source.read_text(encoding="utf-8"))
            names = {
                alias.asname or alias.name
                for node in ast.walk(module)
                if isinstance(node, ast.Import)
                for alias in node.names
                if alias.name == "yaml"
            }
            for node in ast.walk(module):
                where = f"{source.relative_to(REPO_ROOT).as_posix()}:{getattr(node, 'lineno', 0)}"
                if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "yaml":
                    offenders.append(f"{where} from {node.module} import")
                elif isinstance(node, ast.Import) and any(alias.name.startswith("yaml.") for alias in node.names):
                    offenders.append(f"{where} import of a yaml submodule")
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in names:
                    if node.attr in _PARSE_FUNCTIONS or _is_a_reader_class(node.attr):
                        offenders.append(f"{where} yaml.{node.attr}")
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and node.args
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id in names
                ):
                    offenders.append(f"{where} getattr(yaml, ...)")
    assert offenders == []


def test_the_leaf_gate_scans_the_repositorys_scripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The leaf gate reaches ``scripts/``, nested modules included."""
    planted = tmp_path / "scripts" / "nested" / "planted.py"
    planted.parent.mkdir(parents=True)
    planted.write_text("import yaml\nyaml.safe_load(text)\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(sys.modules[__name__], "SOURCE_TREES", _source_trees(tmp_path))
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)

    with pytest.raises(AssertionError, match=r"scripts/nested/planted\.py:2 yaml\.safe_load"):
        test_no_module_of_the_source_trees_or_scripts_parses_yaml_outside_the_leaf()


@pytest.mark.parametrize(
    "source",
    [
        "import yaml\nyaml.compose_all(text)\n",
        "import yaml\nyaml.scan(text)\n",
        "import yaml\nyaml.CSafeLoader(text).get_single_data()\n",
        "import yaml\nreader = yaml.safe_load\n",
        "import yaml as y\ny.safe_load(text)\n",
        "import yaml\ngetattr(yaml, 'safe_load')(text)\n",
        "from yaml.loader import SafeLoader\n",
        "import yaml.loader\n",
    ],
    ids=[
        "compose-all",
        "scan",
        "loader-instance",
        "bound-reference",
        "alias",
        "getattr",
        "from-submodule",
        "submodule",
    ],
)
def test_the_leaf_gate_flags_each_route_it_names(source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each route the gate's docstring says it reaches is reached: a planted module makes it fail."""
    tree = tmp_path / "planted"
    tree.mkdir()
    (tree / "planted.py").write_text(source, encoding="utf-8", newline="\n")
    monkeypatch.setattr(sys.modules[__name__], "SOURCE_TREES", (tree,))
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)

    with pytest.raises(AssertionError):
        test_no_module_of_the_source_trees_or_scripts_parses_yaml_outside_the_leaf()


def test_the_agents_source_imports_yaml_nowhere() -> None:
    """packages/agent/pyproject.toml declares PyYAML for the tests only, because the source reads through the leaf."""
    imports = [
        f"{source.relative_to(REPO_ROOT).as_posix()}:{node.lineno}"
        for source in sorted((REPO_ROOT / "packages" / "agent" / "src").rglob("*.py"), key=lambda path: path.as_posix())
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        if (isinstance(node, ast.Import) and any(alias.name.split(".")[0] == "yaml" for alias in node.names))
        or (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "yaml")
    ]
    assert imports == []


def test_a_pyyaml_without_libyaml_is_refused_when_the_leaf_is_imported(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pure-parser fallback: the leaf refuses to load, naming libyaml, before any text is read.

    The leaf is executed from its file under a scratch module name against a stub
    ``yaml`` that has the pure parser only, so the real module and its cache are
    never replaced.
    """
    stub = types.ModuleType("yaml")
    stub.SafeLoader = yaml.SafeLoader  # type: ignore[attr-defined]
    stub.__with_libyaml__ = False  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yaml", stub)
    spec = importlib.util.spec_from_file_location("_scratch_yaml_load", PACKAGE / "yaml_load.py")
    assert spec is not None and spec.loader is not None
    scratch = importlib.util.module_from_spec(spec)

    with pytest.raises(ImportError, match="libyaml"):
        spec.loader.exec_module(scratch)


# ---------------------------------------------------------------------------
# The parse cache: one command parses a text once
# ---------------------------------------------------------------------------


@pytest.fixture
def parses(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every text the leaf hands to the parser, in order, from an emptied cache."""
    seen: list[str] = []
    real_load = yaml.load

    def counting_load(text: str, Loader: type) -> object:  # noqa: N803 -- yaml.load's own keyword
        seen.append(text)
        return real_load(text, Loader=Loader)

    monkeypatch.setattr(yaml, "load", counting_load)
    clear_parse_cache()
    yield seen
    clear_parse_cache()


@pytest.fixture
def cleared_cache() -> Iterator[None]:
    """An emptied parse cache, emptied again on teardown whatever the test's outcome."""
    clear_parse_cache()
    yield
    clear_parse_cache()


def test_the_same_text_read_twice_is_parsed_once(parses: list[str]) -> None:
    text = "name: SameTextTwice\nports: [a, b]\n"

    first = load_yaml_document(text)
    second = load_yaml_document(text)

    assert first == second == {"name": "SameTextTwice", "ports": ["a", "b"]}
    assert parses == [text]
    info = parse_cache_info()
    assert (info.hits, info.misses, info.currsize) == (1, 1, 1)


def test_a_changed_text_is_parsed_again(parses: list[str]) -> None:
    """The key is the text itself: a changed file is a new key, whatever its path or mtime."""
    before = "name: ChangedText\nperiod: 1\n"
    after = "name: ChangedText\nperiod: 2\n"

    assert load_yaml_document(before) == {"name": "ChangedText", "period": 1}
    assert load_yaml_document(after) == {"name": "ChangedText", "period": 2}

    assert parses == [before, after]
    assert parse_cache_info().currsize == 2


def test_a_mutated_value_does_not_reach_the_next_read(parses: list[str]) -> None:
    """Every read is a deep copy of the parsed document, so a consumer's edit stays its own."""
    text = "name: MutatedCopy\nports:\n  - name: a\n    kind: sender\n"

    first = load_yaml_document(text)
    assert isinstance(first, dict)
    first["name"] = "Changed"
    first["ports"][0]["kind"] = "receiver"
    first["ports"].append({"name": "b"})

    assert load_yaml_document(text) == {"name": "MutatedCopy", "ports": [{"name": "a", "kind": "sender"}]}
    assert parses == [text]


def test_a_text_over_one_mebicharacter_is_parsed_and_not_cached(parses: list[str]) -> None:
    limit = yaml_load._CACHED_TEXT_LIMIT
    text = "blob: " + "x" * limit + "\n"
    assert len(text) > limit

    assert load_yaml_document(text) == {"blob": "x" * limit}
    assert load_yaml_document(text) == {"blob": "x" * limit}

    assert len(parses) == 2
    assert parse_cache_info().currsize == 0


def test_clearing_the_cache_parses_the_next_read_again(parses: list[str]) -> None:
    text = "name: ClearedCache\n"

    load_yaml_document(text)
    clear_parse_cache()
    assert parse_cache_info().currsize == 0
    load_yaml_document(text)

    assert parses == [text, text]


def test_a_refused_text_is_parsed_on_every_read_and_never_held(parses: list[str]) -> None:
    text = "a: [refused-twice\n"

    for _ in range(2):
        with pytest.raises(yaml.YAMLError):
            load_yaml_document(text)

    assert parses == [text, text]
    assert parse_cache_info().currsize == 0


def test_aliases_inside_one_document_stay_aliases_and_two_reads_stay_apart(parses: list[str]) -> None:
    text = "a: &shared [1]\nb: *shared\n"

    first = load_yaml_document(text)
    second = load_yaml_document(text)

    assert isinstance(first, dict) and isinstance(second, dict)
    assert first["a"] is first["b"]
    assert second["a"] is second["b"]
    assert first["a"] is not second["a"]
    assert parses == [text]


def test_a_text_of_exactly_one_mebicharacter_is_cached(parses: list[str]) -> None:
    """The limit is inclusive: only a text *longer* than ``1 << 20`` characters bypasses the cache."""
    limit = yaml_load._CACHED_TEXT_LIMIT
    text = "blob: " + "x" * (limit - len("blob: \n")) + "\n"
    assert limit == 1 << 20
    assert len(text) == limit

    load_yaml_document(text)
    load_yaml_document(text)

    assert len(parses) == 1
    assert parse_cache_info().currsize == 1


def test_reads_and_clears_on_several_threads_never_return_a_wrong_or_shared_value(cleared_cache: None) -> None:
    """The server's worker threads: the cache is coherent -- every read is its own text's own copy.

    Eight threads start together on one barrier and each reads four texts two
    hundred times in a fixed order, clearing the cache every tenth read and
    mutating every value it gets. Every value must equal its text's document
    whatever the interleaving, so the outcome does not depend on the scheduler.
    """
    texts = {
        f"name: Thread{index}\nports: [p{index}]\n": {"name": f"Thread{index}", "ports": [f"p{index}"]}
        for index in range(4)
    }
    ordered = list(texts)
    barrier = threading.Barrier(8)
    failures: list[str] = []

    def worker(offset: int) -> None:
        barrier.wait()
        for step in range(200):
            text = ordered[(offset + step) % len(ordered)]
            value = load_yaml_document(text)
            if value != texts[text] or not isinstance(value, dict):
                failures.append(f"thread {offset} step {step}: {value!r}")
                continue
            value["ports"].append("mutated")
            if step % 10 == offset % 10:
                clear_parse_cache()

    threads = [threading.Thread(target=worker, args=(offset,)) for offset in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []


def test_check_model_parses_each_distinct_text_once(
    monkeypatch: pytest.MonkeyPatch, capsys, cleared_cache: None
) -> None:
    """On a real command: one parse per distinct YAML text of the project.

    ``check-model`` on ``examples/basic`` in-process, from the cache ``cli.main``
    clears: the parses equal the distinct texts of the manifest and the model's
    ``*.yaml`` files. The command runs in a ``single_read`` block, where a layer's
    second load reaches no parse at all, so the hit is witnessed afterwards, by a
    load of the whole model with no block: every file read again, no parse more.
    What it does not reach: the other commands, and a project whose texts
    exceed the cache's bound.
    """
    project = REPO_ROOT / "examples" / "basic"
    manifest = project / "swcraft.yaml"
    model_root = project / yaml.safe_load(manifest.read_text(encoding="utf-8"))["model_root"]
    distinct = {source.read_text(encoding="utf-8") for source in model_root.rglob("*.yaml")}
    distinct.add(manifest.read_text(encoding="utf-8"))
    monkeypatch.setattr("sys.argv", ["swcraft", "check-model", "--project", str(manifest)])

    main()

    info = parse_cache_info()
    assert info.misses == len(distinct)
    assert "Validation passed" in capsys.readouterr().out

    load_model_state(model_root)

    after = parse_cache_info()
    assert after.misses == info.misses
    assert after.hits > info.hits
