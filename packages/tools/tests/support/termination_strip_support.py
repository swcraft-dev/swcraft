"""Undo the termination handling re-freeze in generated ARXML, component by component.

The model refuses a component whose behaviour is emitted and that sets no
``handle_termination_and_restart`` (``rule:swc.undeclared-termination-and-restart``). Every
committed model tree the repository owns therefore declares ``no_support`` on each such component,
and the writer emits one more line into each one's ``SWC-INTERNAL-BEHAVIOR``:
``<HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>``, after ``EVENTS``.

The byte-identity records keep the digests they recorded before that change. Each one compares
:func:`strip_added_termination`'s output instead of the writer's bytes: the helper removes exactly
that one line from the behaviour of each **named** component and nothing else, so the claim a
record makes -- *this change moved no other byte* -- survives without a re-recording.

**It selects by component, never by tag.** ``fixtures/handle_termination_and_restart`` declares a
value on three of its components on purpose, and those lines are part of its own record; a helper
that removed every such line would erase them too. The behaviour is found by its ``SHORT-NAME``,
``{swc}_InternalBehavior``, and the helper fails -- rather than passing silently -- when a named
component's behaviour is missing, carries no such line, carries two, or carries another value.

It is a text substitution on whole lines, never an XML re-serialization, so every byte it keeps is
the writer's own.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from fidelity_strip_support import strip_fidelity

#: The one line the re-freeze added to each named component's behaviour.
ADDED_LINE = re.compile(rb"^[ \t]*<HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>\r?\n")

#: Any ``HANDLE-TERMINATION-AND-RESTART`` line, whatever its value.
ANY_LINE = re.compile(rb"^[ \t]*<HANDLE-TERMINATION-AND-RESTART>")

_OPEN = re.compile(rb"^[ \t]*<SWC-INTERNAL-BEHAVIOR>")
_CLOSE = re.compile(rb"^[ \t]*</SWC-INTERNAL-BEHAVIOR>")
_SHORT_NAME = re.compile(rb"^[ \t]*<SHORT-NAME>([^<]*)</SHORT-NAME>")

#: The components each committed model tree was given ``no_support`` on by the re-freeze,
#: keyed by the tree's path from the repository root. The
#: template declares a value on every component already, and ``handle_termination_and_restart``'s
#: ``SilentSwc`` is the fixture about the absence, so neither is here.
#: ``test_termination_refreeze.py`` asserts each set equals the tree's ``no_support`` components.
TERMINATION_ADDED: dict[str, frozenset[str]] = {
    "examples/basic/model": frozenset({"ExampleConsumerSWC", "ExampleSWC"}),
    "packages/tools/tests/fixtures/access_points/model": frozenset({"AccessSwc", "ServerSwc"}),
    "packages/tools/tests/fixtures/compatible_interfaces/model": frozenset(
        {"Actuator", "Controller", "Display", "Relay", "Sensor"}
    ),
    "packages/tools/tests/fixtures/component_exchange/model": frozenset({"ActuatorSwc", "SensorSwc"}),
    "packages/tools/tests/fixtures/compositions/model": frozenset({"Beta", "Consumer", "Gamma"}),
    "packages/tools/tests/fixtures/diagrams/model": frozenset({"Controller", "DiagServer", "Logger", "Sensor"}),
    "packages/tools/tests/fixtures/hardware/model": frozenset({"Alpha", "Beta"}),
    "packages/tools/tests/fixtures/init_values/model": frozenset({"SignalsSwc"}),
    "packages/tools/tests/fixtures/interface_compatibility/model": frozenset(
        {"Actuator", "Controller", "Display", "Sensor"}
    ),
    "packages/tools/tests/fixtures/operation_events/model": frozenset({"ServerSwc"}),
    "packages/tools/tests/fixtures/recorded_oracles/impact_model": frozenset({"Shared", "SpeedControl", "SpeedSensor"}),
    "packages/tools/tests/fixtures/safety/model": frozenset(
        {"DiagnosticsAgent", "PlainWorker", "SpeedSensor", "TorqueLimiter"}
    ),
    "packages/tools/tests/fixtures/showcase/model": frozenset(
        {"BrakeController", "BrakeTemperatureSensor", "DiagnosticManager", "InstrumentCluster", "VehicleStateManager"}
    ),
}


class TerminationStripError(AssertionError):
    """A named component's behaviour does not carry exactly the one added line."""


def strip_added_termination(data: bytes, components: Iterable[str]) -> bytes:
    """Remove the added ``NO-SUPPORT`` line from each named component's behaviour.

    Raises :class:`TerminationStripError` when a named component has no behaviour in *data*,
    when its behaviour carries no ``HANDLE-TERMINATION-AND-RESTART`` line or more than one, or
    when the one it carries is not ``NO-SUPPORT``. A component not named is left untouched,
    whatever it declares.
    """
    wanted = {f"{name}_InternalBehavior".encode() for name in components}
    lines = data.splitlines(keepends=True)
    kept: list[bytes] = []
    seen: dict[bytes, list[bytes]] = {}
    current: bytes | None = None
    expect_name = False
    for line in lines:
        if expect_name:
            expect_name = False
            match = _SHORT_NAME.match(line)
            if match and match.group(1) in wanted:
                current = match.group(1)
                if current in seen:
                    raise TerminationStripError(f"{current.decode()} is emitted twice")
                seen[current] = []
        if _OPEN.match(line):
            expect_name = True
        elif _CLOSE.match(line):
            current = None
        elif current is not None and ANY_LINE.match(line):
            seen[current].append(line)
            continue
        kept.append(line)
    for behavior in sorted(wanted):
        found = seen.get(behavior)
        name = behavior.decode()
        if found is None:
            raise TerminationStripError(f"no SWC-INTERNAL-BEHAVIOR named {name}")
        if len(found) != 1:
            raise TerminationStripError(f"{name} carries {len(found)} HANDLE-TERMINATION-AND-RESTART lines, not one")
        if not ADDED_LINE.match(found[0]):
            raise TerminationStripError(f"{name} carries {found[0].strip().decode()}, not NO-SUPPORT")
    return b"".join(kept)


def strip_tree(tree: str, data: bytes) -> bytes:
    """:func:`strip_added_termination` over the components *tree* was given the value on."""
    return strip_added_termination(data, TERMINATION_ADDED[tree])


def strip_recorded(tree: str, artifact: str, data: bytes) -> bytes:
    """The bytes a byte-identity record compares for *tree*'s generated *artifact*.

    An ARXML artifact (``arxml-4.2.2``, ``arxml-4.3.0``) is first mapped back through the
    writer-fidelity harness (``fidelity_strip_support.strip_fidelity``, whose identity over every
    committed tree ``test_fidelity_strip_support.py`` gates), then -- for a tree the re-freeze gave the
    value to -- stripped of the added termination lines. The two are chained in that order, so every
    record keeps the digest it recorded before either change. Every other artifact is returned
    unchanged -- neither change reaches another generator -- and a tree outside
    :data:`TERMINATION_ADDED` (the ``init`` template) gets the fidelity strip alone: it declared its
    termination values before that re-freeze.
    """
    if not artifact.startswith("arxml"):
        return data
    data = strip_fidelity(data)
    if tree not in TERMINATION_ADDED:
        return data
    return strip_tree(tree, data)
