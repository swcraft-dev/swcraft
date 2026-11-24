"""Tests for swcraft docs: the parser, the refusal, the served responses, the browser, streaming and the MIME map."""

from __future__ import annotations

import inspect
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import SimpleHTTPRequestHandler
from pathlib import Path

import pytest
import swcraft_tools.env as env_module
from conftest import REPO_ROOT, build_artifacts_module, make_guide_site, neutralise_guide_site_search
from guide_fixtures import FORBIDDEN_VOCABULARY
from swcraft_tools import cli
from swcraft_tools.cli import docs_command, main
from swcraft_tools.cli.dispatch import build_parser
from swcraft_tools.guide_reference import cli as guide_reference_cli
from swcraft_tools.guide_site import GUIDE_SITE_ENV, PACKAGE_SITE_DIRNAME, archive_name

# swcraft docs


def _fixture_site(root: Path) -> Path:
    """The smallest tree that is a site, shaped like the three requests that matter.

    `conftest.make_guide_site` writes the `index.html`. Added here: a search index under
    `search/` and a *directory* URL with its own `index.html` (the two ways `file://`
    breaks), and one asset per entry in `cli.DOCS_CONTENT_TYPES` (whose comment names what it misses).
    """
    make_guide_site(root)
    (root / "search").mkdir(parents=True, exist_ok=True)
    (root / "getting-started").mkdir(parents=True, exist_ok=True)
    (root / "search" / "search_index.json").write_text('{"config": {}, "docs": []}', encoding="utf-8")
    (root / "getting-started" / "index.html").write_text("<html><body>getting started</body></html>", encoding="utf-8")
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for suffix in cli.DOCS_CONTENT_TYPES:
        (assets / f"asset{suffix}").write_text("x", encoding="utf-8")
    return root


#: The ceiling on one request and on one serve thread's join. Not a performance
#: assertion: it turns a hang into a named failure.
_DOCS_SERVE_TIMEOUT_SECONDS = 30


def _probe(request):
    """A substitute for the serve seam that drives real requests, then stops.

    *request* is called once with the base URL and exercises the command's own handler,
    directory and port; the daemon thread is asserted stopped, or a serve loop would run
    for the rest of the session.
    """

    def _serve(server) -> None:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request(f"http://127.0.0.1:{server.server_address[1]}")
        finally:
            server.shutdown()
            thread.join(timeout=_DOCS_SERVE_TIMEOUT_SECONDS)
            assert not thread.is_alive(), "the serve thread did not stop; a daemon thread is left running"

    return _serve


def _status_probe(paths: tuple[str, ...], answers: dict[str, int]):
    """`_probe` for the common shape: request each of *paths*, record its status."""

    def _request(base: str) -> None:
        for path in paths:
            with urllib.request.urlopen(base + path, timeout=_DOCS_SERVE_TIMEOUT_SECONDS) as response:
                answers[path] = response.status

    return _probe(_request)


def _record_bound_address(bound: list[tuple[str, int]]):
    """A serve substitute that records the address `cmd_docs` actually bound."""

    def _serve(server) -> None:
        bound.append(server.server_address[:2])

    return _serve


@pytest.fixture
def installed_guide(tmp_path: Path, monkeypatch) -> Path:
    """A guide the locator finds, through the override step it exposes for exactly this."""
    site = _fixture_site(tmp_path / "site")
    monkeypatch.setenv(GUIDE_SITE_ENV, str(site))
    return site


@pytest.fixture
def packaged_guide(tmp_path: Path, monkeypatch) -> Path:
    """A guide reachable only through the `importlib.resources` step, the path a real install uses."""
    package_root = neutralise_guide_site_search(monkeypatch, tmp_path) / "site-packages" / "swcraft_tools"
    return _fixture_site(package_root / PACKAGE_SITE_DIRNAME)


@pytest.fixture
def no_guide_anywhere(tmp_path: Path, monkeypatch) -> None:
    """All three search steps pointed at empty places, so nothing is found."""
    neutralise_guide_site_search(monkeypatch, tmp_path)


class TestCliDocsParser:
    """The command's surface, read off the parser rather than the help text."""

    def test_docs_is_a_registered_subcommand(self) -> None:
        args = build_parser().parse_args(["docs"])
        assert args.command == "docs"

    def test_the_port_defaults_to_the_ephemeral_one(self) -> None:
        """Default 0 is what stops a second `swcraft docs` from failing on a busy port."""
        assert build_parser().parse_args(["docs"]).port == 0

    def test_the_port_is_read_as_an_integer(self) -> None:
        assert build_parser().parse_args(["docs", "--port", "8123"]).port == 8123

    def test_no_browser_is_a_flag_and_defaults_to_opening_one(self) -> None:
        parser = build_parser()
        assert parser.parse_args(["docs"]).no_browser is False
        assert parser.parse_args(["docs", "--no-browser"]).no_browser is True

    def test_there_is_no_bind_option(self) -> None:
        """A server exposable to a network is a security decision nobody asked for: no bind flag."""
        with pytest.raises(SystemExit):
            build_parser().parse_args(["docs", "--bind", "0.0.0.0"])

    def test_the_help_of_the_command_and_of_every_option_avoids_engineering_vocabulary(self) -> None:
        """The help is carried verbatim onto a generated guide page, so it avoids engineering vocabulary."""
        rows = {name: (sub, summary) for name, sub, summary in guide_reference_cli.commands(build_parser())}
        assert "docs" in rows
        sub, summary = rows["docs"]
        texts = [summary, *(action.help or "" for action in sub._actions)]
        for term in FORBIDDEN_VOCABULARY:
            for text in texts:
                assert not term.search(text), f"`swcraft docs` help uses {term.label}: {term.reason}"

    def test_the_generated_reference_page_can_render_the_command(self) -> None:
        """Every option shape this subcommand uses has a rendering on the reference page."""
        page = guide_reference_cli.cli_page()
        assert "swcraft docs" in page


class TestCliDocsRefusal:
    """No site anywhere: the command could not answer, so the exit is non-zero."""

    def test_no_guide_exits_nonzero(self, no_guide_anywhere: None, monkeypatch) -> None:
        monkeypatch.setattr("sys.argv", ["swcraft", "docs", "--no-browser"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0

    def test_the_refusal_is_the_locator_s_own_message_not_a_second_one(
        self, no_guide_anywhere: None, monkeypatch, capsys
    ) -> None:
        """The command prints the locator's message, the one that names the archive; two would drift."""
        monkeypatch.setattr("sys.argv", ["swcraft", "docs"])
        with pytest.raises(SystemExit):
            main()
        err = capsys.readouterr().err
        assert f"swcraft_tools/{PACKAGE_SITE_DIRNAME}" in err.replace("\\", "/")
        assert archive_name() in err

    def test_the_refusal_never_opens_a_browser(self, no_guide_anywhere: None, monkeypatch) -> None:
        opened: list[str] = []
        monkeypatch.setattr(docs_command, "_open_browser", lambda url: opened.append(url) or True)
        monkeypatch.setattr("sys.argv", ["swcraft", "docs"])
        with pytest.raises(SystemExit):
            main()
        assert opened == []


class TestCliDocsServedResponses:
    """The served site answers what `file://` could not: the search index and directory URLs.

    Asserted against a served fixture: `SimpleHTTPRequestHandler` answers a directory only
    when it holds an `index.html`.
    """

    def test_the_root_the_search_index_and_a_directory_url_all_answer_200(
        self, installed_guide: Path, monkeypatch
    ) -> None:
        paths = ("/", "/search/search_index.json", "/getting-started/")
        answers: dict[str, int] = {}
        monkeypatch.setattr(docs_command, "_serve_forever", _status_probe(paths, answers))
        monkeypatch.setattr("sys.argv", ["swcraft", "docs", "--no-browser"])

        main()

        assert answers == dict.fromkeys(paths, 200)

    def test_a_directory_url_without_its_trailing_slash_still_arrives(self, installed_guide: Path, monkeypatch) -> None:
        """A directory URL without its trailing slash is 301-redirected, asserted as a redirect.

        `urllib` follows a 301 transparently, so reading `response.status` alone would pass
        without one.
        """
        hops: list[tuple[int, str]] = []
        answers: dict[str, int] = {}

        class _RecordRedirects(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913 - the stdlib signature
                hops.append((code, headers["Location"]))
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        opener = urllib.request.build_opener(_RecordRedirects)

        def _request(base: str) -> None:
            with opener.open(base + "/getting-started", timeout=_DOCS_SERVE_TIMEOUT_SECONDS) as response:
                answers["/getting-started"] = response.status

        monkeypatch.setattr(docs_command, "_serve_forever", _probe(_request))
        monkeypatch.setattr("sys.argv", ["swcraft", "docs", "--no-browser"])

        main()

        assert [code for code, _ in hops] == [301], f"expected one 301, got {hops}"
        assert hops[0][1].endswith("/getting-started/")
        assert answers == {"/getting-started": 200}

    def test_the_guide_is_served_from_the_package_data_directory_too(self, packaged_guide: Path, monkeypatch) -> None:
        """With `SWCRAFT_GUIDE_SITE` unset, the command serves the guide through the package data."""
        paths = ("/", "/search/search_index.json")
        answers: dict[str, int] = {}
        monkeypatch.setattr(docs_command, "_serve_forever", _status_probe(paths, answers))
        monkeypatch.setattr("sys.argv", ["swcraft", "docs", "--no-browser"])

        main()

        assert answers == dict.fromkeys(paths, 200)

    def test_every_content_type_the_site_holds_is_typed_explicitly(self, installed_guide: Path, monkeypatch) -> None:
        """Every entry of `DOCS_CONTENT_TYPES` reaches the served header it declares (not reached: its comment)."""
        expected = {f"/assets/asset{suffix}": value for suffix, value in cli.DOCS_CONTENT_TYPES.items()}
        served: dict[str, str] = {}

        def _request(base: str) -> None:
            for path in expected:
                with urllib.request.urlopen(base + path, timeout=_DOCS_SERVE_TIMEOUT_SECONDS) as response:
                    served[path] = response.headers.get_content_type()

        monkeypatch.setattr(docs_command, "_serve_forever", _probe(_request))
        monkeypatch.setattr("sys.argv", ["swcraft", "docs", "--no-browser"])

        main()

        assert served == expected

    def test_the_type_map_is_this_server_s_and_not_the_process_s(self) -> None:
        """The subclass copies `extensions_map`; the base class's table and `mimetypes` are untouched."""
        assert cli.GuideRequestHandler.extensions_map is not SimpleHTTPRequestHandler.extensions_map
        for suffix, content_type in cli.DOCS_CONTENT_TYPES.items():
            assert cli.GuideRequestHandler.extensions_map[suffix] == content_type

    def test_the_server_binds_the_loopback_interface_only(self, installed_guide: Path, monkeypatch) -> None:
        bound: list[tuple[str, int]] = []
        monkeypatch.setattr(docs_command, "_serve_forever", _record_bound_address(bound))
        monkeypatch.setattr("sys.argv", ["swcraft", "docs", "--no-browser"])

        main()

        assert [host for host, _ in bound] == ["127.0.0.1"]
        assert bound[0][1] != 0, "the printed port must be the one the OS actually assigned"


class TestCliDocsBrowserAndExit:
    def test_the_browser_is_opened_at_the_bound_port_and_not_at_the_requested_one(
        self, installed_guide: Path, monkeypatch
    ) -> None:
        """The browser is opened at the bound port, not at the requested `--port 0`.

        `http://127.0.0.1:0/` is a URL no browser can reach, so the URL is compared with the
        bound port rather than counting opener calls.
        """
        opened: list[str] = []
        bound: list[tuple[str, int]] = []
        monkeypatch.setattr(docs_command, "_open_browser", lambda url: opened.append(url) or True)
        monkeypatch.setattr(docs_command, "_serve_forever", _record_bound_address(bound))
        monkeypatch.setattr("sys.argv", ["swcraft", "docs"])

        main()

        assert bound and bound[0][1] != 0
        assert opened == [f"http://127.0.0.1:{bound[0][1]}/"]

    def test_the_url_is_printed_before_the_browser_opens(self, installed_guide: Path, monkeypatch, capsys) -> None:
        """A reader whose browser does not appear needs the address anyway."""
        seen: list[str] = []

        def _opener(url: str) -> bool:
            seen.append(capsys.readouterr().out)
            return True

        monkeypatch.setattr(docs_command, "_open_browser", _opener)
        monkeypatch.setattr(docs_command, "_serve_forever", lambda server: None)
        monkeypatch.setattr("sys.argv", ["swcraft", "docs"])

        main()

        assert seen and "http://127.0.0.1:" in seen[0]

    def test_the_located_root_is_named_in_the_first_line(self, installed_guide: Path, monkeypatch, capsys) -> None:
        """ "It did not open" is answerable only if the reader can see which tree was served."""
        monkeypatch.setattr(docs_command, "_serve_forever", lambda server: None)
        monkeypatch.setattr("sys.argv", ["swcraft", "docs", "--no-browser"])

        main()

        first = capsys.readouterr().out.splitlines()[0]
        assert first == f"Serving the SWCraft guide from {installed_guide}"

    def test_a_request_issued_by_the_opener_is_answered(self, installed_guide: Path, monkeypatch) -> None:
        """A blocking opener must not deadlock the command against its own first request.

        `webbrowser.GenericBrowser.open` waits on its process with no timeout, so the opener
        runs on a thread and `_serve_forever` reaches the request. A regression would hang this
        case rather than fail it -- hence the timeout.
        """
        answered: dict[str, int] = {}
        served: list[str] = []

        def _opener(url: str) -> bool:
            with urllib.request.urlopen(url, timeout=_DOCS_SERVE_TIMEOUT_SECONDS) as response:
                answered[url] = response.status
            return True

        def _await_the_opener(base: str) -> None:
            served.append(base)
            deadline = time.monotonic() + _DOCS_SERVE_TIMEOUT_SECONDS
            while not answered and time.monotonic() < deadline:
                time.sleep(0.01)

        monkeypatch.setattr(docs_command, "_open_browser", _opener)
        monkeypatch.setattr(docs_command, "_serve_forever", _probe(_await_the_opener))
        monkeypatch.setattr("sys.argv", ["swcraft", "docs"])

        main()

        assert answered == {f"{served[0]}/": 200}

    def test_no_browser_never_calls_the_opener(self, installed_guide: Path, monkeypatch, capsys) -> None:
        opened: list[str] = []
        monkeypatch.setattr(docs_command, "_open_browser", lambda url: opened.append(url) or True)
        monkeypatch.setattr(docs_command, "_serve_forever", lambda server: None)
        monkeypatch.setattr("sys.argv", ["swcraft", "docs", "--no-browser"])

        main()

        assert opened == []
        assert "http://127.0.0.1:" in capsys.readouterr().out

    def test_a_failing_opener_still_exits_zero_and_says_what_to_do(
        self, installed_guide: Path, monkeypatch, capsys
    ) -> None:
        """A failing opener still exits 0: on a headless host `webbrowser.open` fails while the URL is fine."""
        monkeypatch.setattr(docs_command, "_open_browser", lambda url: False)
        monkeypatch.setattr(docs_command, "_serve_forever", lambda server: None)
        monkeypatch.setattr("sys.argv", ["swcraft", "docs"])

        main()  # must not raise SystemExit

        out = capsys.readouterr().out
        assert "http://127.0.0.1:" in out
        assert "No browser could be opened here — open the address above yourself." in out

    def test_an_opener_that_raises_is_not_fatal_either(self, installed_guide: Path, monkeypatch, capsys) -> None:
        """An opener that raises instead of returning False is not fatal: the URL still serves."""

        def _raise(url: str) -> bool:
            raise OSError("no display")

        monkeypatch.setattr(docs_command, "_open_browser", _raise)
        monkeypatch.setattr(docs_command, "_serve_forever", lambda server: None)
        monkeypatch.setattr("sys.argv", ["swcraft", "docs"])

        main()  # must not raise SystemExit

        assert "http://127.0.0.1:" in capsys.readouterr().out

    def test_ctrl_c_ends_the_run_successfully_and_says_so(self, installed_guide: Path, monkeypatch, capsys) -> None:
        """A completed serve, ended by Ctrl+C, is a successful run."""

        def _interrupt(server) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(docs_command, "_serve_forever", _interrupt)
        monkeypatch.setattr("sys.argv", ["swcraft", "docs", "--no-browser"])

        main()  # must not raise SystemExit

        assert "Stopped serving the guide." in capsys.readouterr().out

    def test_ctrl_c_while_the_browser_is_being_asked_is_the_same_successful_run(
        self, installed_guide: Path, monkeypatch, capsys
    ) -> None:
        """Ctrl+C while the browser is asked is the same successful run, not exit 130 with a traceback.

        `KeyboardInterrupt` is a `BaseException`, so `except Exception` misses it; the openers
        wait seconds or forever, so the window is real.
        """
        monkeypatch.setattr(docs_command, "_open_browser", lambda url: True)

        def _interrupt(server) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(docs_command, "_serve_forever", _interrupt)
        monkeypatch.setattr("sys.argv", ["swcraft", "docs"])

        main()  # must not raise SystemExit

        assert "Stopped serving the guide." in capsys.readouterr().out

    def test_an_unavailable_port_is_reported_rather_than_traced_back(
        self, installed_guide: Path, monkeypatch, capsys
    ) -> None:
        """A busy or privileged `--port` is a message and an exit code, not a stack trace."""
        occupied = socket.socket()
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        try:
            port = occupied.getsockname()[1]
            monkeypatch.setattr(docs_command, "_serve_forever", lambda server: None)
            monkeypatch.setattr("sys.argv", ["swcraft", "docs", "--no-browser", "--port", str(port)])

            with pytest.raises(SystemExit) as exc_info:
                main()
        finally:
            occupied.close()

        assert exc_info.value.code != 0
        assert "Error:" in capsys.readouterr().err

    @pytest.mark.parametrize("port", ["99999", "-1"])
    def test_a_port_outside_the_range_is_reported_rather_than_traced_back(
        self, installed_guide: Path, monkeypatch, capsys, port: str
    ) -> None:
        """A port outside the range is reported: `socket.bind` raises `OverflowError`, not an `OSError`."""
        monkeypatch.setattr(docs_command, "_serve_forever", lambda server: None)
        monkeypatch.setattr("sys.argv", ["swcraft", "docs", "--no-browser", "--port", port])

        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert err.startswith(f"Error: cannot serve on 127.0.0.1:{port}:")
        assert "Traceback" not in err

    def test_two_overlapping_runs_take_distinct_ports(self, installed_guide: Path, monkeypatch) -> None:
        """Two overlapping runs take distinct ports with `--port 0`.

        The second run starts inside the first one's serve seam, while the first is still bound.
        """
        bound: list[tuple[str, int]] = []
        record = _record_bound_address(bound)

        def _outer(server) -> None:
            record(server)
            monkeypatch.setattr(docs_command, "_serve_forever", record)
            main()

        monkeypatch.setattr(docs_command, "_serve_forever", _outer)
        monkeypatch.setattr("sys.argv", ["swcraft", "docs", "--no-browser"])

        main()

        ports = [port for _, port in bound]
        assert len(ports) == 2
        assert 0 not in ports
        assert ports[0] != ports[1]


class TestCliDocsStreaming:
    """The announcement reaches a *pipe* while the server is still serving.

    Not with `capsys`: it replaces `sys.stdout` with an unbuffered capture, so a `cmd_docs`
    that never flushed would pass. A real subprocess with stdout on a pipe, read while it
    serves, is the only witness of block buffering on a non-TTY stdout. The child is torn
    down with `terminate()`, since `SIGINT` cannot be sent to a child on Windows.

    Does **not** reach: a Windows host (nothing here is measured there), stderr's request
    log, the content of the guide served, or what the command does on an interrupt.
    """

    #: Not a performance assertion: it turns "the announcement never arrives" from a
    #: hung test run into a named failure.
    _ANNOUNCE_TIMEOUT_SECONDS = 30.0

    @staticmethod
    def _serving_child(site: Path, cwd: Path) -> subprocess.Popen:
        """`swcraft docs --no-browser` as a real process, stdout on a pipe.

        `PYTHONUNBUFFERED` is removed: inherited from the shell it would make every case pass
        without the command flushing. `cwd` is the throwaway directory so `main()`'s `.env`
        load cannot pick up the repository's own.
        """
        environment = dict(os.environ, SWCRAFT_GUIDE_SITE=str(site))
        environment.pop("PYTHONUNBUFFERED", None)
        return subprocess.Popen(
            [sys.executable, "-m", "swcraft_tools.cli", "docs", "--no-browser"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd),
            env=environment,
            encoding="utf-8",
        )

    @classmethod
    def _announcement(cls, process: subprocess.Popen) -> list[str]:
        """The lines the child writes before it starts serving, read as they arrive.

        A reader thread rather than `select`, which is POSIX-only on a pipe.
        """
        lines: list[str] = []

        def _read() -> None:
            for line in process.stdout:  # type: ignore[union-attr]
                lines.append(line)

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()
        deadline = time.monotonic() + cls._ANNOUNCE_TIMEOUT_SECONDS
        while len(lines) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        return list(lines)

    def test_the_address_reaches_a_redirect_while_the_command_is_still_serving(self, tmp_path: Path) -> None:
        """`swcraft docs --no-browser > log` shows the address while it serves, not only after Ctrl+C."""
        site = _fixture_site(tmp_path / "site")
        process = self._serving_child(site, tmp_path)
        try:
            lines = self._announcement(process)
            assert process.poll() is None, (
                "the child exited before the announcement was read, so this case proves "
                f"nothing about buffering: {lines}"
            )
            assert len(lines) >= 3, f"the announcement did not reach the pipe while serving: {lines}"
            assert str(site) in lines[0]
            assert lines[1].strip().startswith("http://127.0.0.1:")
            assert "Ctrl+C" in lines[2]
        finally:
            process.terminate()
            process.communicate(timeout=self._ANNOUNCE_TIMEOUT_SECONDS)

    def test_the_announced_address_is_already_answering_when_it_is_read(self, tmp_path: Path) -> None:
        """The announced address already answers when it is read: `cmd_docs` binds before it prints."""
        site = _fixture_site(tmp_path / "site")
        process = self._serving_child(site, tmp_path)
        try:
            lines = self._announcement(process)
            assert len(lines) >= 2, f"no address on the pipe: {lines}"
            url = lines[1].strip()
            with urllib.request.urlopen(url, timeout=self._ANNOUNCE_TIMEOUT_SECONDS) as response:
                assert response.status == 200
        finally:
            process.terminate()
            process.communicate(timeout=self._ANNOUNCE_TIMEOUT_SECONDS)


class TestCliDocsSeams:
    """The two seams exist so that no test opens a browser and no test blocks."""

    def test_both_seams_are_module_level_names_a_test_can_replace(self) -> None:
        for name in ("_open_browser", "_serve_forever"):
            seam = getattr(docs_command, name)
            assert inspect.isfunction(seam)
            assert seam.__module__ == docs_command.__name__

    def test_the_opener_delegates_to_the_standard_library(self, monkeypatch) -> None:
        """The one real call to the standard library, which the other tests' substitute would hide."""
        calls: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: calls.append(url) or True)
        assert docs_command._open_browser("http://127.0.0.1:1/") is True
        assert calls == ["http://127.0.0.1:1/"]


class TestTheBrowserIsNotChosenByTheProjectFile:
    """`main()` merges a project `.env` into `os.environ`, but the file does not choose the browser.

    `webbrowser` picks the program it launches from that environment; the reasoning is in
    `swcraft_tools.env.env_file_keys`. Nothing here launches anything: `webbrowser.get` only
    parses a value into an argv.

    Does **not** reach: every other variable a `.env` can set, `PATH` among them; a hostile
    `.env` is not a boundary the product defends.
    """

    PAYLOAD = "/bin/sh -c 'touch /dev/null' %s"

    @staticmethod
    def _absent_browser(monkeypatch) -> None:
        """Guarantee `BROWSER` is unset *and* restored, whatever the host exported.

        setenv-then-delenv: pytest records an undo only for a key that was already there.
        """
        monkeypatch.setenv("BROWSER", "")
        monkeypatch.delenv("BROWSER")

    @staticmethod
    def _project_with_a_browser_line(monkeypatch, tmp_path: Path, payload: str) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / ".env").write_text(f"BROWSER={payload}\n", encoding="utf-8")
        monkeypatch.chdir(project)
        monkeypatch.setattr(env_module, "_ENV_FILE_KEYS", set())
        env_module.load_env_file()

    def test_a_browser_value_is_a_command_line_the_standard_library_runs(self) -> None:
        """The premise: without this, the guard would be protecting nothing."""
        browser = webbrowser.get(self.PAYLOAD)

        assert isinstance(browser, webbrowser.GenericBrowser)
        assert [browser.name, *browser.args] == ["/bin/sh", "-c", "touch /dev/null", "%s"]

    def test_a_browser_line_in_the_project_file_never_reaches_the_opener(self, monkeypatch, tmp_path: Path) -> None:
        self._absent_browser(monkeypatch)
        self._project_with_a_browser_line(monkeypatch, tmp_path, self.PAYLOAD)
        assert os.environ["BROWSER"] == self.PAYLOAD, "the file has to have reached the environment"
        seen: list[str | None] = []
        monkeypatch.setattr("webbrowser.open", lambda url: seen.append(os.environ.get("BROWSER")) or True)

        docs_command._open_browser("http://127.0.0.1:1/")

        assert seen == [None]
        assert os.environ["BROWSER"] == self.PAYLOAD, "withdrawn for the call, not for the process"

    def test_a_browser_the_user_exported_is_still_honoured(self, monkeypatch, tmp_path: Path) -> None:
        """The shell's value is trusted: whoever exported it already runs this."""
        monkeypatch.setenv("BROWSER", self.PAYLOAD)
        self._project_with_a_browser_line(monkeypatch, tmp_path, "/some/other/browser %s")
        seen: list[str | None] = []
        monkeypatch.setattr("webbrowser.open", lambda url: seen.append(os.environ.get("BROWSER")) or True)

        docs_command._open_browser("http://127.0.0.1:1/")

        assert seen == [self.PAYLOAD]


# The MIME map against a site mkdocs really rendered (`dev.py test-artifacts`)


@pytest.mark.artifact
def test_every_extension_a_real_render_holds_is_typed_explicitly(tmp_path: Path) -> None:
    """`DOCS_CONTENT_TYPES` must *contain* the extension set of a real `mkdocs build`.

    The completeness half no case derived from the map can reach. Marked `artifact`
    because it renders; it never skips when no site is present, since a gate that skips is
    not an oracle. The render writes `docs/guide/version.md` into the checkout and leaves it.

    Does **not** reach: other mkdocs or theme versions; whether an entry with no file behind
    it is plausible; or a *wrong* content type, since it reads no response.
    """
    artifacts = build_artifacts_module()
    site = artifacts.build_docs_site(REPO_ROOT, python_executable=sys.executable, site_dir=tmp_path / "site")

    present = {path.suffix for path in site.rglob("*") if path.is_file()}
    assert present, f"the render produced no files under {site}"

    missing = sorted(present - set(cli.DOCS_CONTENT_TYPES))
    assert missing == [], (
        f"the rendered guide holds {missing}, which `cli.DOCS_CONTENT_TYPES` does not name — "
        "those files would be typed by `mimetypes`, i.e. by the Windows registry on a Windows host"
    )
