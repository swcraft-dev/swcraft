"""``swcraft docs``: its parser, the local guide server and the browser it opens."""

from __future__ import annotations

import argparse
import contextlib
import functools
import os
import sys
import threading
import webbrowser
from collections.abc import Iterator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from swcraft_tools.cli.messages import error_text
from swcraft_tools.env import env_file_keys
from swcraft_tools.guide_site import GuideSiteMissing, site_root

#: The one interface the guide is served on, deliberately not an option: the guide is for the person
#: at the machine, never the network.
DOCS_BIND_HOST = "127.0.0.1"


#: How long :func:`cmd_docs` waits after serving for the browser thread, so its "no browser" line is
#: not lost; a hung opener (a daemon thread) delays the exit by this much and no more.
DOCS_BROWSER_JOIN_SECONDS = 5.0


#: The content types the guide is served with, over what :mod:`mimetypes` registered.
#:
#: On Windows :mod:`mimetypes` reads the registry, where ``.js`` is often ``text/plain``; a browser then
#: refuses the search worker script and search silently fails. Not reached: a file type a later
#: mkdocs-material release emits, and a wrong type in this map.
DOCS_CONTENT_TYPES = {
    ".js": "text/javascript",
    ".html": "text/html",
    ".css": "text/css",
    ".json": "application/json",
    ".map": "application/json",
    ".xml": "application/xml",
    ".png": "image/png",
    ".gz": "application/gzip",
    ".mjs": "text/javascript",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".wasm": "application/wasm",
}


class GuideRequestHandler(SimpleHTTPRequestHandler):
    """``SimpleHTTPRequestHandler`` with the guide's content types pinned, leaving process-global tables alone."""

    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, **DOCS_CONTENT_TYPES}


@contextlib.contextmanager
def _shell_environment() -> Iterator[None]:
    """Run the block with the ``.env``'s contribution to ``os.environ`` withdrawn.

    ``webbrowser`` picks the program it launches from the environment, so an unread ``.env`` must not
    choose what this process executes. Every key the file added is withdrawn, for this call only;
    nothing else reads the environment meanwhile. Not reached: a value the user's shell exported.
    """
    withdrawn = {key: os.environ[key] for key in env_file_keys() if key in os.environ}
    for key in withdrawn:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(withdrawn)


def _open_browser(url: str) -> bool:
    """Ask the platform to open *url*; the seam tests replace.

    The result is advisory and never changes the exit code. The environment guard sits here because
    ``webbrowser`` resolves its browser list lazily, on the first ``open``.
    """
    with _shell_environment():
        return webbrowser.open(url)


def _announce_browser(url: str) -> None:
    """Ask for a browser and say so when none came; an opener that raises counts as none."""
    try:
        opened = _open_browser(url)
    except Exception:  # noqa: BLE001 -- fallback: a platform opener may raise anything
        opened = False
    if not opened:
        print("No browser could be opened here — open the address above yourself.", flush=True)


def _serve_forever(server: ThreadingHTTPServer) -> None:
    """Serve until interrupted; the seam tests replace."""
    server.serve_forever()


def cmd_docs(args: argparse.Namespace) -> None:
    """Serve the installed user guide on the loopback interface and open it.

    The guide must be served: over ``file://`` its search and directory links fail. It is the site of
    this install, so it always matches the installed release.

    Exit 0 on Ctrl+C or a browser that did not open (the URL is printed first); exit 1 when there is no
    guide or the port cannot be bound. Every line is flushed so a piped reader sees the URL at once.
    A threading server keeps search responsive; the browser is asked on its own thread after the socket
    listens, so a blocking opener cannot delay serving.
    """
    try:
        root = site_root()
    except GuideSiteMissing as exc:
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        sys.exit(1)

    handler = functools.partial(GuideRequestHandler, directory=str(root))
    try:
        server = ThreadingHTTPServer((DOCS_BIND_HOST, args.port), handler)
    except (OSError, OverflowError) as exc:
        # `socket.bind` raises `OverflowError` for a port outside 0-65535.
        print(
            f"Error: cannot serve on {DOCS_BIND_HOST}:{args.port}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    # The bound port, not `args.port`: the default 0 lets the OS pick a free one.
    url = f"http://{DOCS_BIND_HOST}:{server.server_address[1]}/"
    opener: threading.Thread | None = None
    try:
        print(f"Serving the SWCraft guide from {root}", flush=True)
        print(f"  {url}", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        if not args.no_browser:
            # On a thread: `webbrowser` may wait on the launched program (without a timeout for an
            # unrecognised `BROWSER`), and the server must already be serving its first request.
            opener = threading.Thread(target=_announce_browser, args=(url,), daemon=True)
            opener.start()
        _serve_forever(server)
    except KeyboardInterrupt:
        # Covers the browser start too: a Ctrl+C at any point is a completed run.
        print("\nStopped serving the guide.", flush=True)
    finally:
        if opener is not None:
            opener.join(timeout=DOCS_BROWSER_JOIN_SECONDS)
        server.server_close()


def add_docs_parser(subparsers: argparse._SubParsersAction) -> None:
    """Declare ``swcraft docs``: the port to serve the guide on, and whether to open a browser."""
    # The help strings are rendered into the guide's command reference.

    docs_parser = subparsers.add_parser(
        "docs",
        help="Open the SWCraft user guide that came with this install, served on this machine",
    )
    docs_parser.add_argument(
        "--port",
        type=int,
        default=0,
        metavar="N",
        help="Port to serve on (default: 0, which lets the system pick a free one, so a second "
        "run never collides with the first)",
    )
    docs_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the address and serve, without opening a browser",
    )
