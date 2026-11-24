"""Decide whether one run of the agent task benchmark counts: the model is right, or the answer is.

A headless runner hands the agent one task of ``packages/tools/tests/fixtures/agent-bench/`` at a
time on a fresh copy of the set's ``bench/``; this script decides the run afterwards::

    python scripts/agent_task_check.py BENCH TASK_ID [--answer FILE] [--calls FILE] [--guide-calls FILE]
        [--set large] [--verdict FILE]
    python scripts/agent_task_check.py BENCH --task-file FILE --baseline DIR [--answer FILE] [--verdict FILE]

A ``question`` or ``guide`` task requires ``--answer`` and a task with ``expect.calls`` requires
``--calls``; either omission exits 2.

The task format -- assertions, answer checks, guide criteria -- is that folder's ``README.md``
(*The task format*). ``--task-file`` with ``--baseline`` is the rendered-task form of the any-model
set: one task rendered for one model and the untouched copy of that project; it excludes
``TASK_ID`` and ``--set``, reads no ``expected/<id>/`` overlay, and its allowances are its
assertions. ``--set large`` checks against the large bench, generated afresh as the baseline. Every
model path comes from each copy's own manifest (``model_root``).

Every failure carries one code of :data:`CODES` (:class:`Failure`). ``--verdict FILE`` writes
``{"result": "PASS" | "FAIL", "codes": [...]}``, each code once in that tuple's order; ``guide.form``
is printed beside ``form: FAIL``, never in ``codes``. ``unreadable-model`` is the rendered form's
alone: a model or manifest the loader refuses is the run's FAIL (in the committed form the loader
error ends the check with its traceback).

Exit 0: the run counts (a guide question: it is *correct*). Exit 1: it does not; every failure is
printed, and a guide question prints ``correct: read=… content=…`` and ``form: PASS|FAIL``. Exit 2:
the task cannot be checked, named on stderr after ``error:``; no verdict file is written.
"""

from __future__ import annotations

import argparse
import bisect
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

import yaml
from swcraft_tools.env import force_utf8_streams
from swcraft_tools.guide_search import SECTION_LINK_PREFIX
from swcraft_tools.model_state import ModelState, load_model_state
from swcraft_tools.project import require_project

REPO_ROOT = Path(__file__).resolve().parent.parent
SET_DIR = REPO_ROOT / "packages" / "tools" / "tests" / "fixtures" / "agent-bench"
TASKS_DIR = SET_DIR / "tasks"
BENCH_DIR = SET_DIR / "bench"
EXPECTED_DIR = SET_DIR / "expected"
LARGE_DIR = SET_DIR / "large"
#: Each set's tasks and expected states; the large set's baseline is generated (:func:`large_baseline`).
SETS = {"showcase": (TASKS_DIR, EXPECTED_DIR), "large": (LARGE_DIR / "tasks", LARGE_DIR / "expected")}
#: Every field of an element may move: the element is new, removed, or named by an assertion
#: that states no field.
ANY_FIELD = "*"
#: The task kinds: a model change, a question about the model, and a question about the user guide.
KINDS = ("change", "question", "guide")
#: The set a rendered task (the any-model set's) names; it is checked with no expected state.
ANY_SET = "any"
MANIFEST = "swcraft.yaml"
#: The names the agent task scripts are loaded by (:func:`load_script`), the tests' too, so one
#: script is one module object in one interpreter.
CHECK_MODULE = "swcraft_agent_task_check"
BIND_MODULE = "swcraft_agent_task_bind"
BENCH_MODULE = "agent_task_bench"
PLACEHOLDERS_MODULE = "swcraft_agent_placeholders"
SCRIPT_FILES = {
    CHECK_MODULE: "agent_task_check.py",
    BIND_MODULE: "agent_task_bind.py",
    BENCH_MODULE: "agent_task_bench.py",
    PLACEHOLDERS_MODULE: "agent_placeholders.py",
}
#: The code a failed assertion carries; the binder records a right run that raised under it.
#: Every ``Failure(...)`` call spells its code as a literal, so a test can collect them.
ASSERTION = "assertion"
#: The closed codes a failure carries (:class:`Failure`); a verdict file names these and nothing else.
CODES = (
    ASSERTION,
    "answer.mentions_all",
    "answer.mentions_none",
    "answer.number",
    "answer.first_of",
    "answer.with_layer",
    "answer.with_owner",
    "answer.candidates",
    "answer.long_list",
    "answer.must",
    "answer.must_not",
    "guide.read",
    "guide.form",
    "calls.forbidden",
    "model-changed",
    "outside-the-task",
    "check-model",
    "unreadable-model",
)

_ERROR_COUNT = re.compile(r"^(\d+) error\(s\)", re.MULTILINE)
_NUMBER_WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
)

#: The words that state an element's layer in a reply (the ``with_layer`` check), by layer.
LAYER_WORDS: dict[str, tuple[str, ...]] = {
    "swc": ("component", "components", "swc", "swcs"),
    "port": ("port", "ports"),
    "runnable": ("runnable", "runnables"),
    "interface": ("interface", "interfaces"),
    "composition": ("composition", "compositions"),
    "task": ("task", "tasks"),
    "hardware": (
        "hardware",
        "core",
        "cores",
        "processor",
        "processors",
        "cpu",
        "cpus",
        "partition",
        "partitions",
        "memory",
        "memories",
        "ram",
        "rams",
    ),
}
_HEADING = re.compile(r"^\s*(?:#.*|.*:\s*|\*\*[^*]+\*\*:?\s*)$")

#: The long-list rule's head: a reply to a longer list may name these many and state the count.
LONG_LIST_HEAD = 20
#: The joiner words of :func:`_joiner`: a range in prose (``pOut020 to rIn549``).
_JOINER_WORDS = ("to", "through", "thru")


def _joiner(names: frozenset[str] = frozenset()) -> str:
    """The pattern joining two names into a range or an elision, on one line (a newline is a list).

    A joiner word that is one of *names* (bare, casefolded: a port named ``To``) is read as that name.
    Not reached: a range spelt with that word in such a list; the symbol joiners still are seen.
    """
    words = [word for word in _JOINER_WORDS if word not in names]
    alternatives = "|".join([r"-", "\u2013", "\u2014", r"\.\.\.?", "\u2026", *words])
    return r"[ \t]*,?[ \t]*(?:" + alternatives + r")[ \t]*,?[ \t]*"


#: :func:`_joiner` over no names: every joiner.
_JOINER = _joiner()
# Markdown emphasis and code spans are dropped before any name, number or range is looked for, so
# they hide neither. An underscore inside a name is kept; one a name begins or ends with, and a
# doubled pair inside one (`Brake__Ctl`), is dropped too, so every expected name is looked for with
# the same drop applied (:func:`_named`). Not reached: two names that differ only in such
# underscores (`Swc` and `Swc_`) are one name to every answer check.
_MARKUP = re.compile(r"[*`~]+|(?<![A-Za-z0-9])_+|_+(?![A-Za-z0-9])")
#: A Markdown backslash escape of an emphasis or code character (``Brake\_Ctl``), read as the literal
#: character before :data:`_MARKUP`'s drop, in an answer and an expected name alike.
_ESCAPED = re.compile(r"\\([_*`~])")
#: The separators a reply may group a number's thousands with, thin and no-break spaces included.
_THOUSANDS = (",", "'", "\u2019", " ", "\u00a0", "\u2009", "\u202f")


class TaskError(Exception):
    """A task this script cannot check as asked (exit 2)."""


class Failure(NamedTuple):
    """One failed check: its closed code (one of :data:`CODES`) and the sentence saying why."""

    code: str
    text: str


# ---------------------------------------------------------------------------
# The task and the model.
# ---------------------------------------------------------------------------


def load_task(task_id: str, tasks_dir: Path = TASKS_DIR) -> dict[str, Any]:
    """The task file named *task_id*, refused when it is unknown or not ready."""
    path = tasks_dir / f"{task_id}.json"
    if not path.is_file():
        known = ", ".join(sorted((p.stem for p in tasks_dir.glob("*.json")), key=str))
        raise TaskError(f"no task {task_id!r}; the set holds: {known}")
    task = json.loads(path.read_text(encoding="utf-8"))
    if task.get("kind") not in KINDS:
        raise TaskError(f"task {task_id!r} has kind {task.get('kind')!r}, not one of {KINDS}")
    if task.get("status") != "ready":
        raise TaskError(f"task {task_id!r} is {task.get('status')!r}: {task.get('model_addition', 'no reason given')}")
    return task


def model_values(state: ModelState) -> dict[str, dict[str, dict[str, Any]]]:
    """Every element of *state* as its YAML spelling, by layer and identity."""
    values: dict[str, dict[str, dict[str, Any]]] = {}
    for layer in state.layer_names:
        elements = {}
        for element in state[layer]:
            dumped = element.model_dump(mode="json", exclude_none=True, by_alias=True)
            elements[str(dumped.get("name", dumped.get("id")))] = dumped
        values[layer] = elements
    return values


def load_task_file(path: Path) -> dict[str, Any]:
    """A rendered task (the any-model set's) read from *path*, refused when it cannot be checked."""
    try:
        task = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise TaskError(f"the task file cannot be read: {type(exc).__name__}") from None
    if not isinstance(task, dict) or not isinstance(task.get("expect"), dict) or "id" not in task:
        raise TaskError("the task file is not a task: it needs an id and an expect mapping")
    if task.get("set") != ANY_SET:
        raise TaskError(f"the task file is not a rendered task: its set is {task.get('set')!r}, not {ANY_SET!r}")
    if task.get("kind") not in KINDS:
        raise TaskError(f"task {task['id']!r} has kind {task.get('kind')!r}, not one of {KINDS}")
    if task.get("status", "ready") != "ready":
        raise TaskError(f"task {task['id']!r} is {task.get('status')!r}")
    _check_allowances(task["expect"].get("assertions", []))
    return task


def _check_allowances(assertions: list[dict[str, Any]]) -> None:
    """Refuse a ``may_change`` that would silently weaken a rendered task (:class:`TaskError`).

    One beside a content check would turn that check off; an empty one would admit every field.
    """
    for assertion in assertions:
        if "any_of" in assertion:
            _check_allowances(assertion["any_of"])
        elif "may_change" in assertion:
            fields = assertion["may_change"]
            if not isinstance(fields, list) or not fields:
                raise TaskError("a may_change allowance names no field: it would admit every field of its element")
            if any(key in assertion for key in ("has", "lacks", "absent", "refers")):
                raise TaskError("a may_change allowance carries a content check: state the check as its own assertion")


def model_root(project: Path) -> Path:
    """The model root of the project at *project*, as its own manifest declares it."""
    if not (project / MANIFEST).is_file():
        raise TaskError(f"no project to read: the directory holds no {MANIFEST}")
    return Path(require_project((project / MANIFEST).as_posix()).resolved_model_root)


def read_model(bench: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """The model of the project at *bench*, through the product's one loader."""
    return model_values(load_model_state(model_root(bench)))


# ---------------------------------------------------------------------------
# Assertions.
# ---------------------------------------------------------------------------


def contains(actual: Any, expected: Any) -> bool:
    """Whether *actual* contains *expected* (the agent-bench README's ``has``)."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and contains(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and all(any(contains(item, wanted) for item in actual) for wanted in expected)
    if isinstance(expected, bool) or isinstance(actual, bool):
        return actual is expected
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return float(actual) == float(expected)
    return actual == expected


def leaf_values(value: Any) -> Iterator[Any]:
    """Every scalar inside *value*, at any depth (keys are not values)."""
    if isinstance(value, dict):
        for item in value.values():
            yield from leaf_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from leaf_values(item)
    else:
        yield value


def check_assertion(model: dict[str, dict[str, dict[str, Any]]], assertion: dict[str, Any]) -> str | None:
    """``None`` when *assertion* holds over *model*, else the sentence that says why not."""
    if "any_of" in assertion:
        failures = [check_assertion(model, alternative) for alternative in assertion["any_of"]]
        if any(failure is None for failure in failures):
            return None
        return "none of the alternatives holds: " + " | ".join(str(failure) for failure in failures)
    if "unreferenced" in assertion:
        name = assertion["unreferenced"]
        holders = sorted(
            f"{layer} {identity}"
            for layer, elements in model.items()
            for identity, element in elements.items()
            if any(isinstance(leaf, str) and name in leaf.split("/") for leaf in leaf_values(element))
        )
        return f"{name} is still referenced by: {', '.join(holders)}" if holders else None

    if "may_change" in assertion:
        # An allowance, not a content check: :func:`unrequested_changes` reads it.
        return None
    layer = assertion["layer"]
    elements = model.get(layer, {})
    name = assertion.get("name")
    if assertion.get("absent"):
        return f"{layer} {name} still exists" if name in elements else None
    if name is None:
        wanted = assertion["has"]
        if any(contains(element, wanted) for element in elements.values()):
            return None
        return f"no element of {layer} contains {json.dumps(wanted, sort_keys=True)}"
    if name not in elements:
        return f"{layer} {name} does not exist"
    element = elements[name]
    if "has" in assertion and not contains(element, assertion["has"]):
        return f"{layer} {name} does not contain {json.dumps(assertion['has'], sort_keys=True)}"
    if "lacks" in assertion and contains(element, assertion["lacks"]):
        return f"{layer} {name} still contains {json.dumps(assertion['lacks'], sort_keys=True)}"
    return _refers_failure(model, layer, name, element, assertion.get("refers", {}))


def _refers_failure(
    model: dict[str, dict[str, dict[str, Any]]],
    layer: str,
    name: str,
    element: dict[str, Any],
    refers: dict[str, dict[str, Any]],
) -> str | None:
    """Why *element*'s ``refers`` fields do not hold in *model*, a target's own ``refers`` included."""
    for field, target in refers.items():
        referred = element.get(field)
        if referred is None:
            if target.get("optional"):
                continue
            return f"{layer} {name} states no {field}"
        held = model.get(target["layer"], {}).get(str(referred))
        if held is None:
            return f"{layer} {name}'s {field} names {referred}, which {target['layer']} does not hold"
        if "has" in target and not contains(held, target["has"]):
            wanted = json.dumps(target["has"], sort_keys=True)
            return f"{layer} {name}'s {field} names {target['layer']} {referred}, which does not contain {wanted}"
        failure = _refers_failure(model, target["layer"], str(referred), held, target.get("refers", {}))
        if failure is not None:
            return failure
    return None


def expected_model(
    baseline: Path, task_id: str, expected_dir: Path = EXPECTED_DIR
) -> dict[str, dict[str, dict[str, Any]]] | None:
    """The model of *baseline* with the task's expected state laid over it, or ``None`` without one."""
    overlay = expected_dir / task_id
    if not overlay.is_dir():
        return None
    root = model_root(baseline)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
        bench = Path(scratch) / "bench"
        shutil.copytree(root, bench / root.relative_to(baseline.resolve()))
        shutil.copyfile(baseline / MANIFEST, bench / MANIFEST)
        for source in sorted(overlay.rglob("*.yaml"), key=lambda path: path.as_posix()):
            target = bench / source.relative_to(overlay)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        removed = overlay / "removed.txt"
        if removed.is_file():
            for line in removed.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    (bench / line.strip()).unlink()
        return read_model(bench)


def _moved_fields(before: dict[str, Any] | None, after: dict[str, Any] | None) -> set[str]:
    """The top-level fields that differ between two versions of an element; ``*`` for a new or removed one."""
    if before is None or after is None:
        return {ANY_FIELD} if before != after else set()
    return {key for key in set(before) | set(after) if before.get(key) != after.get(key)}


def _assertion_fields(assertions: list[dict[str, Any]]) -> Iterator[tuple[str, str | None, set[str], bool]]:
    """``(layer, name, fields, creates)`` per element an assertion names, ``any_of`` alternatives included.

    *creates* is whether the assertion admits the element as new: every assertion but a ``may_change``.
    """
    for assertion in assertions:
        if "any_of" in assertion:
            yield from _assertion_fields(assertion["any_of"])
        elif "layer" in assertion:
            stated = (
                {
                    *assertion.get("has", {}),
                    *assertion.get("lacks", {}),
                    *assertion.get("may_change", []),
                    *assertion.get("refers", {}),
                }
                if not assertion.get("absent")
                else set()
            )
            yield assertion["layer"], assertion.get("name"), stated or {ANY_FIELD}, "may_change" not in assertion


def _referred(
    model: dict[str, dict[str, dict[str, Any]]], assertions: list[dict[str, Any]]
) -> Iterator[tuple[str, str]]:
    """``(layer, name)`` of each element a ``refers`` assertion's field names in *model*, nested targets followed."""
    for assertion in assertions:
        if "any_of" in assertion:
            yield from _referred(model, assertion["any_of"])
        elif "refers" in assertion:
            element = model.get(assertion["layer"], {}).get(assertion.get("name"), {})
            yield from _referred_by(model, element, assertion["refers"])


def _referred_by(
    model: dict[str, dict[str, dict[str, Any]]], element: dict[str, Any], refers: dict[str, dict[str, Any]]
) -> Iterator[tuple[str, str]]:
    """``(layer, name)`` of each element *element*'s ``refers`` fields name, nested targets followed."""
    for field, target in refers.items():
        if element.get(field) is not None:
            referred = str(element[field])
            yield target["layer"], referred
            if "refers" in target:
                held = model.get(target["layer"], {}).get(referred, {})
                yield from _referred_by(model, held, target["refers"])


def unrequested_changes(
    model: dict[str, dict[str, dict[str, Any]]],
    baseline: dict[str, dict[str, dict[str, Any]]],
    expected: dict[str, dict[str, dict[str, Any]]] | None,
    assertions: list[dict[str, Any]],
) -> list[str]:
    """Every element field the run moved that the task does not ask to move — "nothing else moved".

    A field may move where the expected state moves it, where an assertion names it (or names its
    element without stating a field), or where it holds a value an ``unreferenced`` assertion
    requires to go. A new element is allowed where an assertion names it or its layer without a name,
    or a ``refers`` field of the run's model names it; a removed one only where an assertion or the
    expected state names it. Not reached: how far an allowed field moved.
    """
    allowed: dict[tuple[str, str], set[str]] = {}
    open_layers = set()
    named: set[tuple[str, str]] = set()
    for layer in set(baseline) | set(expected or {}):
        for name in set(baseline.get(layer, {})) | set((expected or {}).get(layer, {})):
            if expected is not None:
                fields = _moved_fields(baseline.get(layer, {}).get(name), expected.get(layer, {}).get(name))
                if fields:
                    allowed.setdefault((layer, name), set()).update(fields)
    for layer, name, fields, creates in _assertion_fields(assertions):
        if name is None:
            open_layers.add(layer)
        else:
            if creates:
                named.add((layer, name))
            allowed.setdefault((layer, name), set()).update(fields)
    named.update(_referred(model, assertions))
    for assertion in assertions:
        if "unreferenced" not in assertion:
            continue
        gone = assertion["unreferenced"]
        for layer, elements in baseline.items():
            for name, element in elements.items():
                for field, value in element.items():
                    if any(isinstance(leaf, str) and gone in leaf.split("/") for leaf in leaf_values(value)):
                        allowed.setdefault((layer, name), set()).add(field)
    moved = []
    for layer in sorted(set(baseline) | set(model)):
        for name in sorted(set(baseline.get(layer, {})) | set(model.get(layer, {}))):
            fields = _moved_fields(baseline.get(layer, {}).get(name), model.get(layer, {}).get(name))
            if not fields:
                continue
            permitted = allowed.get((layer, name), set())
            created = name not in baseline.get(layer, {})
            if ANY_FIELD in permitted or (
                fields == {ANY_FIELD} and created and (layer in open_layers or (layer, name) in named)
            ):
                continue
            outside = sorted(fields - permitted)
            if outside:
                moved.append(f"{layer} {name}: {', '.join(outside)}")
    return moved


def check_model_errors(bench: Path) -> list[str]:
    """``check-model``'s error lines on *bench*, empty when it passes with no error."""
    completed = subprocess.run(
        [sys.executable, "-m", "swcraft_tools.cli", "check-model", "--project", (bench / "swcraft.yaml").as_posix()],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    output = completed.stdout + completed.stderr
    count = _ERROR_COUNT.search(output)
    # A model with no finding at all prints no count line, only "Validation passed".
    if completed.returncode == 0 and (int(count.group(1)) == 0 if count is not None else "Validation passed" in output):
        return []
    errors = [line.strip() for line in output.splitlines() if line.strip().startswith("ERROR")]
    return errors or [f"check-model exited {completed.returncode}: {output.strip()}"]


# ---------------------------------------------------------------------------
# Answers.
# ---------------------------------------------------------------------------


def _word(text: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(text)}(?![A-Za-z0-9_])", re.IGNORECASE)


def _unmarked(text: str) -> str:
    """*text* with its Markdown escapes read as literals (:data:`_ESCAPED`), then :data:`_MARKUP` dropped."""
    return _MARKUP.sub("", _ESCAPED.sub(r"\1", text))


def _bare(name: str) -> str:
    """An expected *name* as the answer reads once :data:`_MARKUP` is dropped from it (``Swc_`` -> ``Swc``)."""
    return _unmarked(name)


def states_number(answer: str, number: int) -> bool:
    """Whether *answer* states *number* as a whole number.

    Its plain digits, or from 1000 on its digits grouped by threes with one :data:`_THOUSANDS`
    separator throughout, not inside a longer group (``11,001`` states no 1001). Not reached: the
    Indian grouping, a decimal separator (``1.001`` may be one and a thousandth) and a number in
    words above :data:`_NUMBER_WORDS`.
    """
    digits = str(number)
    if _word(digits).search(answer):
        return True
    if number < 1000:
        return False
    head = len(digits) % 3 or 3
    groups = [digits[:head]] + [digits[index : index + 3] for index in range(head, len(digits), 3)]
    for separator in _THOUSANDS:
        sep = re.escape(separator)
        grouped = sep.join(groups)
        pattern = rf"(?<![A-Za-z0-9_])(?<![0-9]{sep}){grouped}(?!{sep}[0-9])(?![A-Za-z0-9_])"
        if re.search(pattern, answer):
            return True
    return False


def _named(name: str) -> re.Pattern[str]:
    """The whole-word pattern of an expected *name* in an answer whose markup is dropped (:func:`_bare`)."""
    return _word(_bare(name))


def long_list_failure(answer: str, names: list[str]) -> str | None:
    """``None`` when *answer* gives *names* in the long-list rule's short form, else why not.

    The short form: the list's length as a number, the first :data:`LONG_LIST_HEAD` names in the
    expected order, and no two expected names joined into a range or an elision. Markdown emphasis
    and code spans are dropped first (:data:`_MARKUP`).

    The order is a monotone scan, each name looked for past the previous name's match, so a name the
    head holds twice takes its second mention for its second place, and an introductory mention
    never stands for the list's own.
    """
    answer = _unmarked(answer)
    head = names[:LONG_LIST_HEAD]
    needed: dict[str, int] = {}
    for name in head:
        # Keyed by the name as the answer reads it: ``Port`` and ``Port_`` are one name there.
        key = _bare(name).casefold()
        needed[key] = needed.get(key, 0) + 1
        if len(_named(name).findall(answer)) < needed[key]:
            return f"the answer names neither all {len(names)} nor the first {len(head)}: {name} is missing"
    cursor = 0
    for name in head:
        found = _named(name).search(answer, cursor)
        if found is None:
            return f"the answer names the first {len(head)} of the {len(names)}, not in the expected order"
        cursor = found.end()
    if not states_number(answer, len(names)):
        return f"the answer names the first {len(head)} but does not state the number {len(names)}"
    joined = _range_in(answer, names)
    if joined is not None:
        return f"the answer gives a range or a pattern ({joined!r}), not names"
    return None


#: The most identifiers an explicit range may span and still be expanded (:func:`expand_id_ranges`).
ID_RANGE_LIMIT = 200
_ID_RANGE = re.compile(
    r"(?<![A-Za-z0-9_\-])([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-)([0-9]+)"
    r"(?:[ \t]+(?i:to|through)[ \t]+|[ \t]*\u2013[ \t]*|[ \t]+-[ \t]+)"
    r"\1([0-9]+)(?![A-Za-z0-9_])"
)


def expand_id_ranges(answer: str) -> str:
    """*answer* with each explicit range of identifiers replaced by the identifiers it spans.

    ``SWR-DIA-001 to SWR-DIA-003`` reads as ``SWR-DIA-001, SWR-DIA-002, SWR-DIA-003``; a range that
    cannot be read one way is left as written (the agent-bench README states the conditions).
    """

    def spelled(match: re.Match[str]) -> str:
        prefix, first, last = match.group(1), match.group(2), match.group(3)
        low, high = int(first), int(last)
        if len(first) != len(last) or high <= low or high - low + 1 > ID_RANGE_LIMIT:
            return match.group(0)
        return ", ".join(f"{prefix}{number:0{len(first)}d}" for number in range(low, high + 1))

    return _ID_RANGE.sub(spelled, answer)


def _range_in(answer: str, names: list[str]) -> str | None:
    """Where *answer* (its markup dropped) first joins two of *names* into a range or an elision (:func:`_joiner`)."""
    alternatives = "|".join(
        re.escape(name) for name in sorted({_bare(name) for name in names}, key=lambda name: (-len(name), name))
    )
    bound = r"(?<![A-Za-z0-9_])(?:" + alternatives + r")(?![A-Za-z0-9_])"
    joiner = _joiner(frozenset(_bare(name).casefold() for name in names))
    joined = re.search(bound + joiner + bound, answer, re.IGNORECASE)
    return None if joined is None else joined.group(0)


def layer_stated(answer: str, name: str, layers: tuple[str, ...]) -> bool:
    """Whether some line naming *name* states one of *layers* (the ``with_layer`` check).

    *answer* is the reply as written: a whole bold line is a heading only before its emphasis is
    dropped; names and layer words are looked for after the drop.
    """
    words = [_word(word) for layer in layers for word in LAYER_WORDS[layer]]
    pattern = _named(name)
    written = answer.splitlines()
    lines = [_unmarked(line) for line in written]
    for index, line in enumerate(lines):
        found = pattern.search(line)
        if found is None:
            continue
        # The line minus the name itself, so ``WlCsInterface010`` never states ``interface``.
        context = [line[: found.start()] + " " + line[found.end() :]]
        heading = next((lines[i] for i in range(index - 1, -1, -1) if _HEADING.match(written[i])), None)
        if heading is not None:
            context.append(heading)
        if any(word.search(text) for word in words for text in context):
            return True
    return False


#: The layers whose elements own members (ports, runnables).
OWNER_LAYERS = ("swc", "compositions")


def member_owners(model: dict[str, dict[str, dict[str, Any]]]) -> dict[str, frozenset[str]]:
    """Every element of :data:`OWNER_LAYERS` in *model*, with the names of its ports and runnables."""
    return {
        name: frozenset(
            str(member["name"])
            for collection in ("ports", "runnables")
            for member in element.get(collection, [])
            if isinstance(member, dict) and "name" in member
        )
        for layer in OWNER_LAYERS
        for name, element in model.get(layer, {}).items()
    }


#: A character a name is spelled with: the run of them around a mention is the name as written.
_SPELLING = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")
_TOKEN = re.compile(r"[A-Za-z0-9_]+")
#: What follows an owner as the left of ``Owner.Member``.
_QUALIFIER = re.compile(r"\.[A-Za-z0-9_]")
#: Where a line's clauses part: a comma or ``and`` (the ``item`` group, the only breaks a list of
#: owners carries on over), a semicolon, and a dot no name character follows (not ``Owner.Member``).
_CLAUSE_BREAK = re.compile(r"(?P<item>,|(?<![A-Za-z0-9_])and(?![A-Za-z0-9_]))|;|\.(?![A-Za-z0-9_])", re.IGNORECASE)
#: An owner mention: its start and end on the unmarked line, and the owner it names (``None``: none).
_Span = tuple[int, int, str | None]


class _Line(NamedTuple):
    """One line of a reply: as written, with escapes read, and unmarked; *kept* maps unmarked to literal indices."""

    written: str
    literal: str
    unmarked: str
    kept: list[int]


def _line(written: str) -> _Line:
    literal = _ESCAPED.sub(r"\1", written)
    kept: list[int] = []
    last = 0
    for markup in _MARKUP.finditer(literal):
        kept.extend(range(last, markup.start()))
        last = markup.end()
    kept.extend(range(last, len(literal)))
    return _Line(written, literal, "".join(literal[i] for i in kept), kept)


def _spelled(line: _Line, start: int, end: int) -> str:
    """The name a mention at ``[start, end)`` of the unmarked *line* spells on the literal line.

    Widened over name characters the markup drop took: ``Foo_.P1`` spells ``Foo_``, ``**Foo**`` ``Foo``.
    """
    first, last = line.kept[start], line.kept[end - 1] + 1
    while first > 0 and line.literal[first - 1] in _SPELLING:
        first -= 1
    while last < len(line.literal) and line.literal[last] in _SPELLING:
        last += 1
    return line.literal[first:last]


def _spelling_of(spelled: str, names: frozenset[str]) -> str | None:
    """The one of *names* a mention spelled *spelled* names, ``None`` for none.

    The name it spells exactly (emphasis underscores on both sides aside); else the one name sharing
    its normalised form (markup dropped, case ignored), and none when two or more share it.
    """
    return _spelling_in(spelled, {name for name in names if _bare(name).casefold() == _bare(spelled).casefold()})


def _readings(names: frozenset[str]) -> dict[str, frozenset[str]]:
    """*names* grouped by their normalised form (:func:`_bare`, case ignored)."""
    groups: dict[str, set[str]] = {}
    for name in names:
        groups.setdefault(_bare(name).casefold(), set()).add(name)
    return {reading: frozenset(group) for reading, group in groups.items()}


def _spelling_in(spelled: str, group: set[str] | frozenset[str]) -> str | None:
    """:func:`_spelling_of` over the *group* of names that share the mention's normalised form."""
    emphasis = min(len(spelled) - len(spelled.lstrip("_")), len(spelled) - len(spelled.rstrip("_")))
    for cut in range(min(emphasis, len(spelled) // 2) + 1):
        form = spelled[cut : len(spelled) - cut]
        if form in group:
            return form
    return next(iter(group)) if len(group) == 1 else None


def owner_credits(answer: str, name: str, owners: list[str], members: frozenset[str] = frozenset()) -> list[str | None]:
    """The owner of *owners* each mention of *name* in *answer* belongs to, ``None`` for none.

    *answer* is the reply as written; *members* are the member names known beside *name*. A mention
    of an owner or a member names the one it spells (:func:`_spelling_of`), so ``Foo.P1`` is Foo's
    though ``Foo_`` is an owner too, and a mention spelled as another member (``Port_`` for
    ``Port``) is not *name*'s.

    Each line parts into clauses at :data:`_CLAUSE_BREAK`. A mention in a clause naming an owner
    belongs to that clause's nearest owner mention, either side (one overlapping the mention is
    none); a tie, or a nearest mention naming no one owner, gives it to none. When that owner
    follows the member, the owners of the clauses listed after it, each parted by a comma or ``and``
    alone and naming owners and no member, share it (``P1 on A, B and C``). A mention in a clause
    naming no owner belongs to the nearest earlier clause's last owner when that clause names its
    owner first (``SwcA: P1, P2``); else to the nearest later clause's group when that clause names
    its member first or none (``P1 and P2 on SwcA``); else to the earlier clause's group; else to
    none. Only a line naming no owner falls back to the nearest heading naming one owner, so
    ``- SwcX.P1`` under ``### SwcA`` is SwcX's. A token spelling an owner in an owner's place (the
    left of ``Owner.Member``, or, alone on its line, a heading or a line naming another member:
    ``| B | P1 |``) is that owner, not a mention of *name*.
    """
    return [credit for _, credit in owner_mentions(answer, name, owners, members)]


def owner_mentions(
    answer: str, name: str, owners: list[str], members: frozenset[str] = frozenset()
) -> list[tuple[tuple[int, int, int], str | None]]:
    """Each credit of a mention of *name* in *answer*, in reading order, with its place.

    The place is ``(line, column, owner column)`` on the unmarked line (owner column ``-1`` for a
    heading's owner or none), so a mention a list of owners shares is one credit per owner, in order.
    """
    known = frozenset(owners)
    readings = _readings(members | {name})
    # One pattern per reading: ``Foo`` and ``Foo_`` read alike, and their spelling tells them apart.
    patterns = {_bare(candidate).casefold(): _named(candidate) for candidate in owners}
    pattern = _named(name)
    lines = [_line(written) for written in answer.splitlines()]

    def owned(line: _Line) -> list[_Span]:
        return sorted(
            (found.start(), found.end(), _spelling_of(_spelled(line, found.start(), found.end()), known))
            for owner in patterns.values()
            for found in owner.finditer(line.unmarked)
        )

    def member(line: _Line, start: int, end: int) -> str | None:
        spelled = _spelled(line, start, end)
        return _spelling_in(spelled, readings.get(_bare(spelled).casefold(), frozenset()))

    mentions: list[tuple[tuple[int, int, int], str | None]] = []
    for index, line in enumerate(lines):
        found_here = [found for found in pattern.finditer(line.unmarked) if member(line, *found.span()) == name]
        if not found_here:
            continue
        spans = owned(line)
        heading_line = _HEADING.match(line.written) is not None
        tokens = [token.span() for token in _TOKEN.finditer(line.unmarked) if member(line, *token.span()) is not None]
        accepted: list[tuple[int, int, list[_Span]]] = []
        for found in found_here:
            start, end = found.span()
            others = [span for span in spans if span[1] <= start or span[0] >= end]
            # A token that also spells an owner is, by its place, an owner and not a member.
            if any(begin < end and stop > start and key is not None for begin, stop, key in spans):
                if _QUALIFIER.match(line.unmarked, end):
                    continue
                if not others and (heading_line or any(token[0] != start for token in tokens)):
                    continue
            accepted.append((start, end, others))
        if not accepted:
            continue
        # The members the clauses name, an accepted owner-spelling mention of *name* read as the member.
        overlaps_owner = [any(begin < end and stop > start for start, end, _ in spans) for begin, stop in tokens]
        columns = {begin for (begin, _), owner in zip(tokens, overlaps_owner, strict=True) if not owner}
        names_at = sorted(columns | {start for start, _, _ in accepted})
        taken = [(start, end) for start, end, _ in spans] + tokens
        breaks = [
            (cut.start(), cut.end(), cut.group("item") is not None)
            for cut in _CLAUSE_BREAK.finditer(line.unmarked)
            if not any(start < cut.end() and end > cut.start() for start, end in taken)
        ]
        for start, end, others in accepted:
            if others:
                for column, key in _clause_credits(line.unmarked, breaks, others, names_at, start, end):
                    mentions.append(((index, start, column), key))
                continue
            heading = next((lines[i] for i in range(index - 1, -1, -1) if _HEADING.match(lines[i].written)), None)
            named = {key for _, _, key in owned(heading)} if heading is not None else set()
            mentions.append(((index, start, -1), next(iter(named)) if len(named) == 1 else None))
    return mentions


def _clause_credits(
    text: str, breaks: list[tuple[int, int, bool]], owners: list[_Span], members: list[int], start: int, end: int
) -> list[tuple[int, str | None]]:
    """The owners, as (column, owner), a member mention at ``[start, end)`` of *text* belongs to.

    *text* is one unmarked line, *breaks* its clause breaks (start, end, whether it parts list
    items), *owners* its owner mentions in order (the mention's own overlapping one left out),
    *members* its member columns. The rule is :func:`owner_credits`'s; ``(-1, None)`` stands for
    none.
    """
    ends = [stop for _, stop, _ in breaks]
    count = len(breaks) + 1
    lows = [0, *ends]
    highs = [begin for begin, _, _ in breaks] + [len(text)]
    held: list[list[_Span]] = [[] for _ in range(count)]
    for span in owners:
        held[bisect.bisect_right(ends, span[0])].append(span)
    named: list[list[int]] = [[] for _ in range(count)]
    for column in members:
        named[bisect.bisect_right(ends, column)].append(column)

    def member_first(clause: int) -> bool:
        return bool(named[clause]) and named[clause][0] < held[clause][0][0]

    def listed(clause: int) -> list[_Span]:
        # A blank clause (``, and``) is passed over.
        after: list[_Span] = []
        for later in range(clause + 1, count):
            if not breaks[later - 1][2] or named[later]:
                break
            if held[later]:
                after.extend(held[later])
            elif _TOKEN.search(text, lows[later], highs[later]) is not None:
                break
        return after

    def group(clause: int) -> list[tuple[int, str | None]]:
        # The owner after its first member (every owner when it names none), and those listed after.
        first = named[clause][0] if named[clause] else -1
        after = [span for span in held[clause] if span[0] > first]
        lead = after[:1] if named[clause] else after
        return [(begin, key) for begin, _, key in lead + listed(clause)]

    clause = bisect.bisect_right(ends, start)
    if held[clause]:
        apart = [(max(start - stop, begin - end), begin, key) for begin, stop, key in held[clause]]
        nearest = min(distance for distance, _, _ in apart)
        closest = [(begin, key) for distance, begin, key in apart if distance == nearest]
        if len({key for _, key in closest}) != 1:
            return [(-1, None)]
        begin, key = closest[0]
        if begin >= end:
            return [(begin, key)] + [(later, owner) for later, _, owner in listed(clause)]
        return [(begin, key)]
    back = next((earlier for earlier in range(clause - 1, -1, -1) if held[earlier]), None)
    if back is not None and not member_first(back):
        begin, _, key = held[back][-1]
        return [(begin, key)]
    ahead = next((later for later in range(clause + 1, count) if held[later]), None)
    if ahead is not None and (not named[ahead] or member_first(ahead)):
        return group(ahead)
    if back is not None:
        return group(back)
    return [(-1, None)]


def owner_long_list_failure(
    answer: str, pairs: list[dict[str, str]], owners: list[str], members: frozenset[str] = frozenset()
) -> str | None:
    """``None`` when *answer* gives *pairs* in the long-list rule's short form, else why not.

    ``with_owner``'s form of :func:`long_list_failure`, over pairs read by :func:`owner_mentions`,
    no two expected names (owners and members alike) joined; a member name two owners share is read
    once per owner. Not reached: a member joined to its owner by a dash (``P1 - Swc005``) reads as a
    range, so that short form fails, though a reply naming every pair passes.
    """
    head = pairs[:LONG_LIST_HEAD]
    cursor = (-1, -1, -1)
    for pair in head:
        given = [
            place for place, credit in owner_mentions(answer, pair["name"], owners, members) if credit == pair["owner"]
        ]
        if not given:
            return (
                f"the answer gives neither all {len(pairs)} nor the first {len(head)}: "
                f"{pair['owner']}'s {pair['name']} is missing"
            )
        later = [place for place in given if place > cursor]
        if not later:
            return f"the answer gives the first {len(head)} of the {len(pairs)}, not in the expected order"
        cursor = later[0]
    unmarked = _unmarked(answer)
    if not states_number(unmarked, len(pairs)):
        return f"the answer gives the first {len(head)} but does not state the number {len(pairs)}"
    joined = _range_in(unmarked, [name for pair in pairs for name in (pair["owner"], pair["name"])])
    if joined is not None:
        return f"the answer gives a range or a pattern ({joined!r}), not names"
    return None


def check_answer(answer: str, expected: dict[str, Any], owners: dict[str, frozenset[str]] | None = None) -> list[str]:
    """Every way *answer* misses *expected* (the agent-bench README's answer checks), as sentences."""
    return [failure.text for failure in check_answer_coded(answer, expected, owners)]


def check_answer_coded(
    answer: str, expected: dict[str, Any], owners: dict[str, frozenset[str]] | None = None
) -> list[Failure]:
    """Every way *answer* misses *expected*, each with its ``answer.*`` code.

    Markdown emphasis and code spans are dropped first (:data:`_MARKUP`); ``with_layer`` reads the
    reply as written (:func:`layer_stated`). *owners* is the model's owners with their members
    (:func:`member_owners`), which ``with_owner`` knows beside the expected ones; without it, that
    check knows the expected owners alone.
    """
    written = answer
    answer = expand_id_ranges(_unmarked(answer))
    failures = []
    names = expected.get("mentions_all", [])
    missing = [name for name in names if not _named(name).search(answer)]
    if missing and expected.get("long_list") and len(names) > LONG_LIST_HEAD:
        short_form = long_list_failure(answer, names)
        if short_form is not None:
            failures.append(Failure("answer.long_list", short_form))
    elif missing:
        failures.append(Failure("answer.mentions_all", f"the answer does not name {', '.join(missing)}"))
    wrong = [name for name in expected.get("mentions_none", []) if _named(name).search(answer)]
    if wrong:
        failures.append(
            Failure(
                "answer.mentions_none", f"the answer names {', '.join(wrong)}, which is not part of the right answer"
            )
        )
    if "number" in expected:
        number = int(expected["number"])
        words = [_NUMBER_WORDS[number]] if number < len(_NUMBER_WORDS) else []
        if not states_number(answer, number) and not any(_word(word).search(answer) for word in words):
            failures.append(Failure("answer.number", f"the answer does not state the number {number}"))
    for site in expected.get("with_layer", []):
        name = site.get("owner") or site["name"]
        layers = (site["layer"], "swc") if site.get("owner") else (site["layer"],)
        if not _named(name).search(answer):
            failures.append(Failure("answer.with_layer", f"the answer does not name {name}"))
        elif not layer_stated(written, name, layers):
            failures.append(
                Failure("answer.with_layer", f"the answer names {name} but not its layer ({site['layer']})")
            )
    pairs = expected.get("with_owner", [])
    known = owners or {}
    candidates = sorted({pair["owner"] for pair in pairs} | set(known), key=str)
    members = frozenset(pair["name"] for pair in pairs).union(*known.values())
    misplaced: set[tuple[str, str]] = set()
    # Each failure with whether it is an unmet pair, which the long-list short form may answer; a
    # member given to an owner without it fails either way.
    owner_failures: list[tuple[bool, Failure]] = []
    for pair in pairs:
        absent = [part for part in (pair["owner"], pair["name"]) if not _named(part).search(answer)]
        if absent:
            owner_failures.append((True, Failure("answer.with_owner", f"the answer does not name {', '.join(absent)}")))
            continue
        credits = owner_credits(written, pair["name"], candidates, members)
        if pair["owner"] not in credits:
            owner_failures.append(
                (True, Failure("answer.with_owner", f"the answer names {pair['name']} but not as {pair['owner']}'s"))
            )
        for credit in credits:
            # The owner must hold the exact name: ``SwcB.Port`` is misplaced when SwcB holds ``Port_`` alone.
            held = credit in known and pair["name"] in known[credit]
            if credit in known and not held and (credit, pair["name"]) not in misplaced:
                misplaced.add((credit, pair["name"]))
                text = f"the answer gives {pair['name']} to {credit}, which has no such member"
                owner_failures.append((False, Failure("answer.with_owner", text)))
    if any(unmet for unmet, _ in owner_failures) and expected.get("long_list") and len(pairs) > LONG_LIST_HEAD:
        short_form = owner_long_list_failure(written, pairs, candidates, members)
        owner_failures = [(unmet, failure) for unmet, failure in owner_failures if not unmet]
        if short_form is not None:
            owner_failures.insert(0, (True, Failure("answer.long_list", short_form)))
    failures.extend(failure for _, failure in owner_failures)
    if "candidates" in expected:
        rule = expected["candidates"]
        absent = [name for name in rule["top"] if not _named(name).search(answer)]
        if absent:
            failures.append(
                Failure(
                    "answer.candidates", f"the answer does not name the search's top candidates {', '.join(absent)}"
                )
            )
        unreasoned = [
            name
            for name in rule["top"]
            if name not in absent and not any(_named(term).search(answer) for term in rule["terms"][name])
        ]
        if unreasoned:
            failures.append(
                Failure(
                    "answer.candidates", f"the answer gives no shared term as the reason for {', '.join(unreasoned)}"
                )
            )
    if "first_of" in expected:
        rule = expected["first_of"]
        positions = {}
        for candidate in rule["candidates"]:
            found = _named(candidate).search(answer)
            if found is not None:
                positions[candidate] = found.start()
        first = min(positions, key=lambda candidate: positions[candidate]) if positions else None
        if first != rule["expected"]:
            failures.append(Failure("answer.first_of", f"the answer leads with {first!r}, not {rule['expected']!r}"))
    return failures


# ---------------------------------------------------------------------------
# Guide questions: the section read, the section cited, the exact content, the calls.
# ---------------------------------------------------------------------------

#: The SWCraft tools a guide question's run may not call: each writes a file or proposes a change.
WRITING_TOOLS = ("change_set", "build", "validate", "export_component", "import_component")
GUIDE_TOOL = "guide"
#: The three criteria of a guide question, in report order.
GUIDE_CRITERIA = ("read", "form", "content")
#: The criteria of the pass verdict, *correct*.
CORRECT_CRITERIA = ("read", "content")
#: The criterion reported apart as the answer's *form*.
FORM_CRITERION = "form"
CRITERION_PASS, CRITERION_FAIL, CRITERION_NA = "PASS", "FAIL", "N/A"
#: A character that continues a section id in a link, so a link to ``page#anchor`` does not cite ``page``.
_ID_CHARACTER = re.compile(r"[A-Za-z0-9_\-/#%]")
#: A Markdown backslash escape of any punctuation, read as the character.
_ANY_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
_CONTENT_MARKUP = re.compile(r"[`*]")
_WHITESPACE = re.compile(r"\s+")
_GUIDE_LINK = re.compile(re.escape(SECTION_LINK_PREFIX) + r"[A-Za-z0-9_\-/#%]*")


class Criterion(NamedTuple):
    """One criterion's state (:data:`CRITERION_PASS` and the rest) and its failures."""

    state: str
    failures: list[Failure]


def content_text(text: str) -> str:
    """*text* as the content criterion reads it.

    Escapes read, every guide link dropped (a citation is no content: ``…#stale-baseline`` names no
    ``baseline``), backticks and asterisks dropped, whitespace runs one space; case kept.
    """
    unescaped = _GUIDE_LINK.sub(" ", _ANY_ESCAPE.sub(r"\1", text))
    return _WHITESPACE.sub(" ", _CONTENT_MARKUP.sub("", unescaped))


def holds_string(text: str, entry: dict[str, Any]) -> bool:
    """Whether :func:`content_text` of *text* holds one of *entry*'s ``any_of`` strings as a substring.

    Case aside unless *entry* is ``case_sensitive``.
    """
    read = content_text(text)
    if not entry["case_sensitive"]:
        read = read.casefold()
    return any((string if entry["case_sensitive"] else string.casefold()) in read for string in entry["any_of"])


def holds_entry(text: str, entry: dict[str, Any]) -> bool:
    """Whether *text* meets a ``must`` entry: a plain one, or every entry of one ``one_of`` set."""
    if "one_of" in entry:
        return any(all(holds_string(text, member) for member in members) for members in entry["one_of"])
    return holds_string(text, entry)


def _must_strings(entry: dict[str, Any]) -> str:
    """A ``must`` entry's strings as a failure names them: ``a, b``, or ``{a} or {b, c}``."""
    if "one_of" in entry:
        return " or ".join(
            "{" + ", ".join(string for member in members for string in member["any_of"]) + "}"
            for members in entry["one_of"]
        )
    return ", ".join(entry["any_of"])


def cites_section(answer: str, section_id: str) -> bool:
    """Whether *answer* holds the link to *section_id*, its ``#`` written or as ``%23``.

    The link is followed by no character of an id (:data:`_ID_CHARACTER`).
    """
    text = _ANY_ESCAPE.sub(r"\1", answer)
    for form in dict.fromkeys((section_id, section_id.replace("#", "%23"))):
        link = SECTION_LINK_PREFIX + form
        start = text.find(link)
        while start != -1:
            end = start + len(link)
            if end == len(text) or not _ID_CHARACTER.match(text[end]):
                return True
            start = text.find(link, start + 1)
    return False


def reads_section(
    guide_calls: list[dict[str, Any]], sections: list[str], page_parts: dict[str, list[int]] | None = None
) -> bool:
    """Whether one answered ``guide`` call read a section of *sections* by its exact id.

    A page id counts only when listed itself or, for a page of *page_parts*, when the call asked for
    a listed part (no ``part``: part 1).
    """
    page_parts = page_parts or {}

    def reads(arguments: dict[str, Any]) -> bool:
        section = arguments.get("section")
        if section in sections:
            return True
        return section in page_parts and (arguments.get("part") or 1) in page_parts[section]

    return any(
        call.get("answered") is True and isinstance(call.get("arguments"), dict) and reads(call["arguments"])
        for call in guide_calls
    )


def guide_criteria(answer: str, rule: dict[str, Any], guide_calls: list[dict[str, Any]] | None) -> dict[str, Criterion]:
    """The three criteria of a guide question (:data:`GUIDE_CRITERIA`) over *answer* and *guide_calls*.

    ``read`` is :data:`CRITERION_NA` when *guide_calls* is ``None``; ``form`` and ``content`` always apply.
    """
    sections = rule["sections"]
    if guide_calls is None:
        read = Criterion(CRITERION_NA, [])
    elif reads_section(guide_calls, sections, rule.get("page_parts")):
        read = Criterion(CRITERION_PASS, [])
    else:
        read = Criterion(
            CRITERION_FAIL, [Failure("guide.read", "no guide call read an expected section: " + ", ".join(sections))]
        )
    if any(cites_section(answer, section) for section in sections):
        form = Criterion(CRITERION_PASS, [])
    else:
        form = Criterion(
            CRITERION_FAIL, [Failure("guide.form", "the reply links no expected section: " + ", ".join(sections))]
        )
    content = [
        Failure("answer.must", f"the reply does not hold {entry['id']}: none of {_must_strings(entry)}")
        for entry in rule["must"]
        if not holds_entry(answer, entry)
    ]
    content.extend(
        Failure("answer.must_not", f"the reply holds {entry['id']}, which is wrong")
        for entry in rule["must_not"]
        if holds_string(answer, entry)
    )
    return {
        "read": read,
        "form": form,
        "content": Criterion(CRITERION_FAIL if content else CRITERION_PASS, content),
    }


def guide_verdicts(criteria: dict[str, Criterion]) -> tuple[str, str]:
    """*correct* (PASS when no criterion of :data:`CORRECT_CRITERIA` fails; N/A is none) and *form*."""
    correct = any(criteria[name].state == CRITERION_FAIL for name in CORRECT_CRITERIA)
    return (CRITERION_FAIL if correct else CRITERION_PASS), criteria[FORM_CRITERION].state


def read_guide_calls(path: Path) -> list[dict[str, Any]]:
    """The run's ``guide`` calls (``--guide-calls``), in call order; blank lines skipped.

    One JSON object per line, ``arguments`` and ``answered``.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        calls = [json.loads(line) for line in lines if line.strip()]
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise TaskError(f"the guide calls file cannot be read: {type(exc).__name__}") from None
    if not all(isinstance(call, dict) for call in calls):
        raise TaskError("the guide calls file cannot be read: a line is no JSON object")
    return calls


def check_calls(calls: list[str], rule: dict[str, Any]) -> list[Failure]:
    """Every call of *calls* (the run's SWCraft tool names, in call order) that ``rule["forbidden"]`` names."""
    forbidden = [name for name in rule.get("forbidden", []) if name in calls]
    if not forbidden:
        return []
    counted = ", ".join(f"{name} ({calls.count(name)})" for name in forbidden)
    return [Failure("calls.forbidden", f"the run called a tool the task forbids: {counted}")]


def read_calls(path: Path) -> list[str]:
    """The run's SWCraft tool names, one per line, in call order (``--calls``); blank lines skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TaskError(f"the calls file cannot be read: {type(exc).__name__}") from None
    return [line.strip() for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# The run.
# ---------------------------------------------------------------------------


def check_task(
    bench: Path,
    task: dict[str, Any],
    *,
    answer: str | None = None,
    baseline: Path = BENCH_DIR,
    expected_dir: Path = EXPECTED_DIR,
    calls: list[str] | None = None,
    guide_calls: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Every failed check of *task* over the run at *bench*; empty when the run counts."""
    return [
        failure.text
        for failure in check_task_coded(
            bench,
            task,
            answer=answer,
            baseline=baseline,
            expected_dir=expected_dir,
            calls=calls,
            guide_calls=guide_calls,
        )
    ]


def check_task_coded(
    bench: Path,
    task: dict[str, Any],
    *,
    answer: str | None = None,
    baseline: Path = BENCH_DIR,
    expected_dir: Path = EXPECTED_DIR,
    calls: list[str] | None = None,
    guide_calls: list[dict[str, Any]] | None = None,
) -> list[Failure]:
    """Every failed check of *task* over the run at *bench*, each with its code of :data:`CODES`.

    A committed set's change task lays its ``expected/<id>/`` state over *baseline*; a rendered task
    (``"set": "any"``) never does. Without *guide_calls* a guide question's criterion 1 is N/A.
    Raises :class:`TaskError` when the task cannot be checked (no baseline, no *answer* for a
    question, no *calls* for a task that scores them).
    """
    if not (baseline / MANIFEST).is_file():
        raise TaskError(f"no baseline to compare with: the directory holds no {MANIFEST}")
    expect = task["expect"]
    if task.get("set") == ANY_SET:
        try:
            model = read_model(bench)
        except (TaskError, ValueError, OSError, yaml.YAMLError) as exc:
            # A model the loader refuses is the run's failure, named by its class alone (a loader's
            # message names the model's elements). The manifest is parsed outside the element
            # loader's wrapping, hence ``yaml.YAMLError``.
            return [Failure("unreadable-model", f"the run left a model the loader refuses: {type(exc).__name__}")]
    else:
        model = read_model(bench)
    failures = [
        Failure("assertion", failure)
        for failure in (check_assertion(model, assertion) for assertion in expect.get("assertions", []))
        if failure is not None
    ]
    if task["kind"] in ("question", "guide"):
        if answer is None:
            raise TaskError(f"task {task['id']!r} is a question: give the agent's reply with --answer FILE")
        if task["kind"] == "guide":
            criteria = guide_criteria(answer, expect["guide"], guide_calls)
            for name in CORRECT_CRITERIA:
                failures.extend(criteria[name].failures)
        else:
            owners = member_owners(read_model(baseline)) if "with_owner" in expect["answer"] else None
            failures.extend(check_answer_coded(answer, expect["answer"], owners))
    if "calls" in expect:
        if calls is None:
            raise TaskError(f"task {task['id']!r} scores its calls: give the run's SWCraft calls with --calls FILE")
        failures.extend(check_calls(calls, expect["calls"]))
    if expect.get("model") == "unchanged":
        before = read_model(baseline)
        changed = sorted(
            f"{layer} {identity}"
            for layer in sorted(set(before) | set(model), key=str)
            for identity in sorted(set(before.get(layer, {})) | set(model.get(layer, {})), key=str)
            if before.get(layer, {}).get(identity) != model.get(layer, {}).get(identity)
        )
        if changed:
            failures.append(Failure("model-changed", f"the model changed: {', '.join(changed)}"))
    if task["kind"] == "change":
        expected = None if task.get("set") == ANY_SET else expected_model(baseline, task["id"], expected_dir)
        moved = unrequested_changes(model, read_model(baseline), expected, expect.get("assertions", []))
        if moved:
            failures.append(Failure("outside-the-task", f"changed outside the task: {'; '.join(moved)}"))
    if expect.get("check_model") == "no-errors":
        failures.extend(Failure("check-model", f"check-model: {error}") for error in check_model_errors(bench))
    return failures


def write_verdict(path: Path, text: str) -> None:
    """Write *text* to *path* through a new file beside it, which then replaces the entry.

    :func:`os.replace` never writes through a hard link at *path*, and the new file is its owner's
    alone (mode ``0o600``): the verdict lies under the runner's private directory.
    """
    descriptor, staged = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(staged, 0o600)
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


def verdict(failures: list[Failure], form: str | None = None) -> dict[str, Any]:
    """The verdict file's document; a guide question's adds ``form`` apart from ``result``.

    Closed keys, the codes each once, in :data:`CODES` order.
    """
    present = {failure.code for failure in failures}
    document: dict[str, Any] = {
        "result": "FAIL" if failures else "PASS",
        "codes": [code for code in CODES if code in present],
    }
    if form is not None:
        document["form"] = form
    return document


def load_script(module_name: str) -> ModuleType:
    """The sibling script *module_name* names (:data:`SCRIPT_FILES`), loaded once per interpreter.

    The module is registered in ``sys.modules`` before it executes, so a dataclass resolves its
    annotations and every caller gets one module object.
    """
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / "scripts" / SCRIPT_FILES[module_name])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def large_baseline(target: Path) -> Path:
    """The large bench, generated into *target* by ``scripts/agent_task_bench.py``.

    Raises :class:`TaskError` when the specimen cannot be taken: a refusal is the checker's, never
    the agent's (exit 2). It names the class and reason alone, since the refusal's sentence may name a path.
    """
    bench = load_script(BENCH_MODULE)
    try:
        return bench.build_large(target)
    except bench.TAKE_REFUSALS as error:
        reason = getattr(error, "reason", None)
        named = f"{type(error).__name__} ({reason})" if reason else type(error).__name__
        raise TaskError(f"the large baseline cannot be taken: {named}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Decide whether one agent benchmark run counts.")
    parser.add_argument("bench", type=Path, help="The run's copy of the bench (the directory with swcraft.yaml)")
    parser.add_argument(
        "task", nargs="?", help="The task id, the file name under packages/tools/tests/fixtures/agent-bench/tasks/"
    )
    parser.add_argument("--answer", type=Path, help="The agent's final reply as a text file (question tasks)")
    parser.add_argument("--set", choices=sorted(SETS, key=str), help="The task set and its bench (default: showcase)")
    parser.add_argument("--task-file", type=Path, help="A rendered task (the any-model set's); needs --baseline")
    parser.add_argument("--baseline", type=Path, help="The untouched copy of the project, with --task-file")
    parser.add_argument("--verdict", type=Path, help="Write the verdict as JSON (result and codes) to this file")
    parser.add_argument(
        "--calls", type=Path, help="The run's SWCraft tool names, one per line in call order (guide questions)"
    )
    parser.add_argument(
        "--guide-calls",
        type=Path,
        help="The run's guide calls, one JSON object per line: arguments and answered (guide questions)",
    )
    args = parser.parse_args(argv)
    # The runner reads these sentences as UTF-8; a pipe on Windows would carry the locale's code page.
    force_utf8_streams()
    try:
        failures, task, guide_states = _check(args)
    except TaskError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    form = None if guide_states is None else guide_states[FORM_CRITERION]
    if args.verdict is not None:
        write_verdict(args.verdict, json.dumps(verdict(failures, form), sort_keys=True) + "\n")
    print(f"{'FAIL' if failures else 'PASS'} {task['id']}")
    for failure in failures:
        print(f"  - {failure.text}")
    if guide_states is not None:
        print("  correct: " + " ".join(f"{name}={guide_states[name]}" for name in CORRECT_CRITERIA))
        print(f"  form: {form}")
    return 1 if failures else 0


def _check(args: argparse.Namespace) -> tuple[list[Failure], dict[str, Any], dict[str, str] | None]:
    """The failures of the run *args* names, its task and, for a guide question, each criterion's state."""
    failures, task, answer, guide_calls = _check_run(args)
    if task["kind"] != "guide" or answer is None:
        return failures, task, None
    states = {
        name: criterion.state
        for name, criterion in guide_criteria(answer, task["expect"]["guide"], guide_calls).items()
    }
    return failures, task, states


def _check_run(
    args: argparse.Namespace,
) -> tuple[list[Failure], dict[str, Any], str | None, list[dict[str, Any]] | None]:
    """The failures of the run *args* names, its task, its answer and its guide calls."""
    rendered = args.task_file is not None or args.baseline is not None
    if rendered and (args.task is not None or args.set is not None):
        raise TaskError("--task-file and --baseline check a rendered task: give no TASK_ID and no --set with them")
    if rendered and (args.task_file is None or args.baseline is None):
        raise TaskError("--task-file and --baseline go together: a rendered task is checked against its baseline")
    if not rendered and args.task is None:
        raise TaskError("give a TASK_ID, or --task-file with --baseline")
    answer = None
    if args.answer is not None:
        try:
            answer = args.answer.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise TaskError(f"the answer file cannot be read: {type(exc).__name__}") from None
    calls = read_calls(args.calls) if args.calls is not None else None
    guide_calls = read_guide_calls(args.guide_calls) if args.guide_calls is not None else None
    coded = {"answer": answer, "calls": calls, "guide_calls": guide_calls}
    if rendered:
        task = load_task_file(args.task_file)
        if not args.baseline.is_dir():
            raise TaskError("no baseline to compare with: the directory does not exist")
        return check_task_coded(args.bench, task, baseline=args.baseline, **coded), task, answer, guide_calls
    set_name = args.set or "showcase"
    tasks_dir, expected_dir = SETS[set_name]
    task = load_task(args.task, tasks_dir)
    if set_name == "large":
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
            baseline = large_baseline(Path(scratch) / "bench")
            failures = check_task_coded(args.bench, task, baseline=baseline, expected_dir=expected_dir, **coded)
            return failures, task, answer, guide_calls
    return check_task_coded(args.bench, task, **coded), task, answer, guide_calls


if __name__ == "__main__":
    sys.exit(main())
