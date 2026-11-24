"""What a console interrupt does to the stdio server — observed, not described.

A Ctrl-C at a ``swcraft mcp-serve`` console does not end the child at once:
``mcp/server/stdio.py`` reads stdin through ``anyio.wrap_file``, a blocking
``readline`` in an *uncancellable worker thread*, so the ``KeyboardInterrupt``
raised in the main thread waits for a line or an end-of-input. This module
observes it with a real ``python -m swcraft_mcp.server``, a real ``SIGINT`` and a
real end-of-input; reading the server's source cannot.

What this module does **not** reach:

* **Windows**: there is no ``SIGINT`` delivery to another process, and whether
  ``CTRL_C_EVENT`` aborts a console read is unmeasured. The module skips there.
* A **busy** server: the interrupt is delivered while the server is idle at the
  read, the console case.
* The **timings** are floors: "still alive after five seconds" is evidence that
  nothing ended it, not proof that nothing ever would.
* The **inherited disposition**, which is fixed rather than observed:
  :func:`_restore_default_sigint` puts ``SIGINT`` back to ``SIG_DFL`` in the
  child, as a console server has it.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time

import pytest
from conftest import REPO_ROOT

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX signal delivery: the Windows console's own reaction is the seated gate's observation",
)

#: How long the server is given to reach its stdio read before the interrupt.
_STARTUP_SECONDS = 20.0

#: How long "nothing ended it" is observed for. A floor, not a bound.
_STILL_ALIVE_SECONDS = 5.0

#: How long a server *told* to stop is given (end-of-input waits, the kill in
#: :func:`_close`). The clean stop is 0.1 s, so this ceiling turns a hang into a
#: named failure.
_SHUTDOWN_SECONDS = 30.0

#: The banner ``FastMCP`` prints on stderr once the transport is up.
_READY_MARKER = b"transport"


def _restore_default_sigint() -> None:  # pragma: no cover - runs in the forked child
    """Put ``SIGINT`` back to ``SIG_DFL`` before the server's ``exec``.

    ``SIG_IGN`` is inherited across ``fork`` and ``exec`` and CPython installs no
    handler over it; a shell that backgrounds a job sets exactly that, so the case
    would pass its first half for the wrong reason and then end **0**. A console
    server has ``SIG_DFL``, so this makes the child match its subject.
    """
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def _serve() -> subprocess.Popen[bytes]:
    """A real stdio server, started the way :func:`cli.handover._hand_over` starts it."""
    return subprocess.Popen(
        [sys.executable, "-m", "swcraft_mcp.server"],
        cwd=str(REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "SWCRAFT_MANIFEST": str(REPO_ROOT / "examples" / "basic" / "swcraft.yaml")},
        preexec_fn=_restore_default_sigint,
    )


def _wait_until_serving(process: subprocess.Popen[bytes]) -> bytes:
    """Read stderr until the transport banner appears, or fail the case (polled, so it can never hang)."""
    selector = selectors.DefaultSelector()
    assert process.stderr is not None
    selector.register(process.stderr, selectors.EVENT_READ)
    seen = b""
    deadline = time.monotonic() + _STARTUP_SECONDS
    try:
        while time.monotonic() < deadline:
            for _key, _mask in selector.select(timeout=0.2):
                seen += os.read(process.stderr.fileno(), 65536)
            if _READY_MARKER in seen:
                return seen
            if process.poll() is not None:
                pytest.fail(f"the server exited {process.returncode} before it served: {seen!r}")
    finally:
        selector.close()
    pytest.fail(f"the server printed no transport banner in {_STARTUP_SECONDS}s: {seen!r}")


@pytest.mark.slow
def test_an_interrupt_does_not_end_the_stdio_server_while_it_blocks_on_stdin() -> None:
    """A ``SIGINT`` at the idle stdin read is pending, not lost, until the read is released.

    No exit and no ``KeyboardInterrupt`` on stderr at first; after the end-of-input
    the traceback appears and the process ends on ``SIGINT`` (``-2``), not ``0``.
    This fails the day the library becomes interruptible, and
    :func:`swcraft_tools.cli.handover._hand_over`'s docstring would change with it.
    """
    process = _serve()
    try:
        _wait_until_serving(process)
        process.send_signal(signal.SIGINT)

        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=_STILL_ALIVE_SECONDS)

        assert process.poll() is None, "the interrupt did not reach the blocked read, so nothing ended"

        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=_SHUTDOWN_SECONDS) == -signal.SIGINT, (
            "released by the end-of-input, the pending interrupt is what the process finally ends on"
        )
        assert process.stderr is not None
        assert b"KeyboardInterrupt" in process.stderr.read(), (
            "and the traceback the console never showed is printed only now"
        )
    finally:
        _close(process)


@pytest.mark.slow
def test_an_uninterrupted_end_of_input_is_the_clean_stop() -> None:
    """Closing stdin is the **clean** exit (0), so the interrupt is what changes the answer above."""
    process = _serve()
    try:
        _wait_until_serving(process)
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=_SHUTDOWN_SECONDS) == 0, "an end-of-input with no interrupt behind it is exit 0"
    finally:
        _close(process)


def _close(process: subprocess.Popen[bytes]) -> None:
    """Leave no server behind, whatever the case did."""
    if process.poll() is None:  # pragma: no cover - only on a failed case
        process.kill()
        process.wait(timeout=_SHUTDOWN_SECONDS)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()
