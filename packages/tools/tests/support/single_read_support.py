"""The tripwire over every ``single_read`` block in process, shared by two test trees.

Elements are shared inside a block, so a consumer that mutates one reaches every later
reader, the writer among them. :func:`single_read_tripwire`, an autouse fixture, holds the
memo to a fresh load at each block's exit: each held layer is re-parsed with
``_parse_layer``, and a layer that differs fails the test.

**It records and never raises.** The observer runs at the block's exit, inside the code
under test, and a raise there would be swallowed: ``_run_with_error_shape`` catches
``Exception``, the engine turns ``ElementParseError`` into a refusal and the server masks
the rest. So the difference goes on a list and the fixture's teardown fails the test.

``packages/tools/tests/conftest.py`` and ``packages/mcp/tests/conftest.py`` both import this one
definition (and the ``reads`` spy), the second through the ``packages/tools/tests`` entry it
appends to ``sys.path``: a fixture cannot otherwise cross a project boundary.

**What it does not reach.** A block in a subprocess CLI, a path no test takes, and the
``agent`` tree, whose conftest does not import it. A block opened by a fixture of a wider
scope than the function runs before this fixture is set. A mutation that leaves an element
equal under ``==`` is not seen, and a value not equal to itself (a ``NaN``) reads as a
difference. A block on a root that is gone at its exit and whose every held layer was absent
at the load is no difference: a load of a root that never existed gives the same answer, and
the memo cannot tell the two apart.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

import pytest
from swcraft_tools import model_state as ms

_Entry = TypeVar("_Entry")

#: ``parse_element`` as the module defines it, taken before any test can patch it. The
#: comparison is the tripwire's read, so a test that counts or faults ``parse_element`` must
#: not see it: the count would grow at the block's exit and the fault would read as a change.
_UNPATCHED_PARSE_ELEMENT = ms.parse_element


@contextmanager
def _unpatched_parse_element() -> Iterator[None]:
    patched = ms.parse_element
    ms.parse_element = _UNPATCHED_PARSE_ELEMENT
    try:
        yield
    finally:
        ms.parse_element = patched


def _difference(root: str, layer: str, registry: ms.ModelRegistry, held: tuple[bool, list[Any]]) -> str | None:
    """Why the held answer is not a fresh load's, or ``None``. Raises nothing."""
    where = f"{layer} of {root}"
    try:
        fresh = ms._parse_layer(root, layer, registry)
    except Exception as error:  # noqa: BLE001 - any failure of the fresh load is the finding
        return f"{where}: a fresh load raises {type(error).__name__}: {error}"
    if fresh == held:
        return None
    identity = registry.layer(layer).identity_field
    changed = [pair for pair in held[1] if pair not in fresh[1]] + [pair for pair in fresh[1] if pair not in held[1]]
    names = sorted({str(getattr(element, identity)) for element, _ in changed})
    return f"{where}: a fresh load differs in {', '.join(names) or 'its order or presence'}"


def differences(held: ms._Held) -> list[str]:
    """One sentence for each held layer that a fresh load does not answer the same."""
    with _unpatched_parse_element():
        found = (_difference(root, layer, registry, answer) for (root, layer, registry), answer in held.items())
        return [sentence for sentence in found if sentence is not None]


class _Recorder:
    """The observer: what it found waits on a list for the teardown."""

    def __init__(self) -> None:
        self.found: list[str] = []

    def __call__(self, held: ms._Held) -> None:
        self.found.extend(differences(held))


def armed() -> bool:
    """Whether the observer in place is this module's."""
    return isinstance(ms._single_read_observer, _Recorder)


@contextmanager
def watching() -> Iterator[None]:
    """Record over the body and fail on its exit; the observer before it is put back."""
    recorder = _Recorder()
    previous = ms._single_read_observer
    ms._single_read_observer = recorder
    try:
        yield
    finally:
        ms._single_read_observer = previous
    if recorder.found:
        pytest.fail("a single_read block left a held layer unequal to a fresh load:\n" + "\n".join(recorder.found))


@pytest.fixture(autouse=True)
def single_read_tripwire() -> Iterator[None]:
    with watching():
        yield


def parse_spy(monkeypatch: pytest.MonkeyPatch, entry: Callable[[Path], _Entry]) -> list[_Entry]:
    """The list that gets ``entry(file)`` for each file ``parse_element`` opens, in order."""
    seen: list[_Entry] = []
    real = ms.parse_element

    def counting(yaml_file: Path, *args: Any, **kwargs: Any) -> Any:
        seen.append(entry(yaml_file))
        return real(yaml_file, *args, **kwargs)

    monkeypatch.setattr(ms, "parse_element", counting)
    return seen


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, bool]]:
    """Each file ``parse_element`` opens, and whether a block was open when it did."""
    return parse_spy(monkeypatch, lambda file: (file, ms._SINGLE_READ.get() is not None))


def read_twice(reads: list[tuple[Path, bool]]) -> dict[Path, int]:
    """The files *reads* holds more than once, with their counts."""
    return {file: count for file, count in Counter(file for file, _ in reads).items() if count > 1}
