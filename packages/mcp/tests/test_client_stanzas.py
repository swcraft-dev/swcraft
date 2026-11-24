"""Every server line SWCraft gives a chat client starts the server and answers.

The lines are read where they live, never restated: the scaffolded project's ``.mcp.json`` (Claude
Code), ``.codex/config.toml`` (Codex) and ``.kiro/settings/mcp.json`` (Kiro), and the guide's JSON and
TOML blocks under *Another chat client* in ``docs/guide/vscode.md``. Each is read as its client reads
it (Claude Code's ``${VAR}``/``${VAR:-default}`` expansion, with ``CLAUDE_PROJECT_DIR`` both unset and
set; Kiro's file as plain JSON with no ``${`` at all) and started as a real process: its command
resolved on a ``PATH`` led by this interpreter's scripts directory, its ``env`` over a minimal
environment, the project folder as working directory. Over newline-delimited JSON-RPC the server
must answer ``initialize``, list exactly the server's registered tools, and answer one ``discover``
and one ``read``. Every stanza carries ``SWCRAFT_REVIEW=panel``. The guide's ``<absolute path of
the project>`` placeholder is the one substitution made.

For each relative line: started in a folder without ``swcraft.yaml``, every project tool answers
:data:`MISSING_RELATIVE_MANIFEST` naming the manifest as the line gives it, and ``guide`` answers as
usual; started in a subfolder with its own ``swcraft.yaml``, that nested manifest opens without a
word. A manifest that is there and cannot be used is named with the kind of its failure
(:data:`BROKEN_MANIFESTS`), never the masked ``Error calling tool``, libyaml's or pydantic's text.

What it does not reach: the client's own reading of its file, its approval prompts, the working
directory it really starts the server in, and its timeouts.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import threading
import time
import tomllib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import pytest
from protocol_client import tool_names

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The guide page that carries the hand-written server lines.
GUIDE_PAGE = REPO_ROOT / "docs" / "guide" / "vscode.md"

#: The section the blocks live under, by its title (its anchor is ``another-chat-client``).
GUIDE_HEADING = "Another chat client"

#: The guide's placeholder for the project folder, replaced by the scaffolded project's path.
PLACEHOLDER = "<absolute path of the project>"

#: The value every stanza gives the server: proposals are reviewed in the SWCraft review tab.
REVIEW_MODE = "panel"

#: The names ``swcraft init`` gives the outer and the nested project, read back from ``discover``.
PROJECT_NAME = "stanza-project"
NESTED_NAME = "nested-project"

#: What every project tool answers when the relative manifest is not in the server's folder
#: (``server.missing_manifest_sentence``), the manifest named as :data:`MANIFEST_AS_GIVEN`.
MISSING_RELATIVE_MANIFEST = (
    "No SWCraft project here: {manifest} was not found in the folder the server was started in; "
    "start the client in the project folder."
)

#: The manifest argument each relative line hands the server: Claude Code's expansion of
#: ``${CLAUDE_PROJECT_DIR:-.}/swcraft.yaml`` with the variable unset, and Codex's and Kiro's lines as written.
MANIFEST_AS_GIVEN = {"claude-code": "./swcraft.yaml", "codex": "swcraft.yaml", "kiro": "swcraft.yaml"}

#: The one tool that answers without a project: it reads the installed guide.
ANSWERS_WITHOUT_A_PROJECT = frozenset({"guide"})

#: The arguments a tool requires, the least that passes its signature; a tool not named takes none.
#: A tool added with a required argument fails the missing-manifest case until it is named here.
MINIMAL_ARGUMENTS: dict[str, dict[str, Any]] = {
    "read": {"address": {"layer": "hardware"}},
    "find": {"text": "x"},
    "change_set": {"baseline": "rev1:x", "operations": [{"op": "create", "address": {"layer": "requirements"}}]},
    "impact_of": {"name": "X"},
    "guide": {"section": "index"},
    "import_component": {"source": "component.arxml", "baseline": "B1"},
}

#: A manifest that is there and cannot be used, and the sentence every project tool answers for it
#: (``server.broken_manifest_sentence``): the manifest as the Codex line gives it, the kind of failure,
#: never a value.
BROKEN_MANIFESTS: dict[str, tuple[str, str]] = {
    "unparseable": (
        "name: [unclosed\n",
        "The SWCraft project cannot be opened: swcraft.yaml is not valid YAML (line 2, column 1); "
        "correct the manifest, then call the tool again.",
    ),
    "empty": (
        "",
        "The SWCraft project cannot be opened: swcraft.yaml is empty; correct the manifest, then call the tool again.",
    ),
    "invalid": (
        "name: 5\n",
        "The SWCraft project cannot be opened: in swcraft.yaml, field `name` is invalid (string_type); "
        "correct the manifest, then call the tool again.",
    ),
    "unreadable": (
        "name: unreadable\n",
        "The SWCraft project cannot be opened: swcraft.yaml cannot be read (Permission denied); "
        "make it readable to the server, then call the tool again.",
    ),
}

#: How long the server may take to start and answer one message.
RESPONSE_SECONDS = 60

#: How long the server may take to end once its stdin is closed, and a tree kill to be reaped.
EXIT_SECONDS = 15

#: The variables a process needs to start at all. Nothing else is inherited, no ``SWCRAFT_*`` in
#: particular, so what the server is told comes from the stanza alone.
CARRIED_VARIABLES = (
    "HOME",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
)

#: Claude Code's documented expansion forms in ``.mcp.json``: ``${VAR}`` and ``${VAR:-default}``.
_EXPANSION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class UnsetVariable(LookupError):
    """``${VAR}`` with no default named a variable that is not set: Claude Code refuses the file."""


def expand_claude(text: str, environment: Mapping[str, str]) -> str:
    """*text* with Claude Code's documented ``.mcp.json`` expansion applied.

    Raises:
        UnsetVariable: a ``${VAR}`` without a default names an unset variable.

    What it does not reach: whether an empty variable counts as set (read literally here as set),
    and whatever the client does beyond its documentation.
    """

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in environment:
            return environment[name]
        if default is not None:
            return default
        raise UnsetVariable(name)

    return _EXPANSION.sub(replace, text)


@dataclass(frozen=True)
class Stanza:
    """One server line as its client would start it."""

    origin: str
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str]


def stanza_of(origin: str, table: Mapping[str, Any]) -> Stanza:
    """The ``command``, ``args`` and ``env`` of one parsed server table, checked for their types."""
    command, args, env = table.get("command"), table.get("args", []), table.get("env", {})
    assert isinstance(command, str), f"{origin}: no string `command` in {table!r}"
    assert isinstance(args, list) and all(isinstance(arg, str) for arg in args), f"{origin}: `args` {args!r}"
    assert isinstance(env, dict) and all(isinstance(value, str) for value in env.values()), f"{origin}: `env` {env!r}"
    return Stanza(origin, command, tuple(args), dict(env))


# ---------------------------------------------------------------------------
# Where the stanzas are read
# ---------------------------------------------------------------------------


def template_claude_stanza(project: Path, environment: Mapping[str, str]) -> Stanza:
    """The project's ``.mcp.json`` server ``swcraft``, expanded as Claude Code expands it."""
    document = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    table = document["mcpServers"]["swcraft"]
    raw = stanza_of(".mcp.json", table)
    return Stanza(
        raw.origin,
        expand_claude(raw.command, environment),
        tuple(expand_claude(arg, environment) for arg in raw.args),
        {name: expand_claude(value, environment) for name, value in raw.env.items()},
    )


def template_codex_stanza(project: Path) -> Stanza:
    """The project's ``.codex/config.toml`` server ``swcraft``, as ``tomllib`` reads it."""
    document = tomllib.loads((project / ".codex" / "config.toml").read_text(encoding="utf-8"))
    return stanza_of(".codex/config.toml", document["mcp_servers"]["swcraft"])


def template_kiro_stanza(project: Path) -> Stanza:
    """The project's ``.kiro/settings/mcp.json`` server ``swcraft``, as plain JSON.

    Kiro expands only user-approved variables, so a line relying on an expansion would start
    differently on each machine; none may be there.
    """
    text = (project / ".kiro" / "settings" / "mcp.json").read_text(encoding="utf-8")
    stanza = stanza_of(".kiro/settings/mcp.json", json.loads(text)["mcpServers"]["swcraft"])
    named = [stanza.command, *stanza.args, *stanza.env.values()]
    assert not any("${" in value for value in named), f"{stanza.origin}: a value relies on an expansion: {named!r}"
    return stanza


def guide_section(page: str) -> str:
    """The text of the *Another chat client* section, up to the next heading of its level or above."""
    lines = page.splitlines()
    heading = re.compile(r"^(#{1,6})\s+" + re.escape(GUIDE_HEADING) + r"(\s|$)")
    start = next((index for index, line in enumerate(lines) if heading.match(line)), None)
    assert start is not None, (
        f"{GUIDE_PAGE.relative_to(REPO_ROOT).as_posix()}: no heading titled {GUIDE_HEADING!r}; the client "
        "stanza test reads the guide's server lines under it"
    )
    level = len(heading.match(lines[start]).group(1))  # type: ignore[union-attr]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        found = re.match(r"^(#{1,6})\s", lines[index])
        if found and len(found.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end])


def fenced_blocks(section: str, language: str) -> list[str]:
    """The bodies of the section's code fences opened with *language*."""
    return re.findall(r"^```" + re.escape(language) + r"[ \t]*\n(.*?)^```[ \t]*$", section, flags=re.M | re.S)


def guide_stanzas(language: str) -> list[Stanza]:
    """Every server line the guide gives under *Another chat client* in a *language* fence.

    A JSON block counts when it is a server table (``command`` at its top) or holds
    ``mcpServers.swcraft``; a TOML block when it holds ``mcp_servers.swcraft``. None at all fails.
    """
    section = guide_section(GUIDE_PAGE.read_text(encoding="utf-8"))
    tables: list[Mapping[str, Any]] = []
    for block in fenced_blocks(section, language):
        document = json.loads(block) if language == "json" else tomllib.loads(block)
        if language == "json" and "command" in document:
            tables.append(document)
        elif language == "json" and "swcraft" in document.get("mcpServers", {}):
            tables.append(document["mcpServers"]["swcraft"])
        elif language == "toml" and "swcraft" in document.get("mcp_servers", {}):
            tables.append(document["mcp_servers"]["swcraft"])
    assert tables, (
        f"{GUIDE_PAGE.relative_to(REPO_ROOT).as_posix()}: no ```{language} block holding the swcraft server "
        f"line under the {GUIDE_HEADING!r} heading (looked for "
        + ("a table with `command`, or `mcpServers.swcraft`" if language == "json" else "`[mcp_servers.swcraft]`")
        + ")"
    )
    return [stanza_of(f"the guide's {language} block {number}", table) for number, table in enumerate(tables, 1)]


def substitute_placeholder(stanza: Stanza, project: Path) -> Stanza:
    """*stanza* with the guide's placeholder replaced by *project*'s absolute path, in every value."""
    where = project.resolve().as_posix()
    named = [stanza.command, *stanza.args, *stanza.env.values()]
    assert any(PLACEHOLDER in value for value in named), f"{stanza.origin}: no {PLACEHOLDER!r} in {named!r}"
    return Stanza(
        stanza.origin,
        stanza.command.replace(PLACEHOLDER, where),
        tuple(arg.replace(PLACEHOLDER, where) for arg in stanza.args),
        {name: value.replace(PLACEHOLDER, where) for name, value in stanza.env.items()},
    )


# ---------------------------------------------------------------------------
# The process
# ---------------------------------------------------------------------------


def scripts_path() -> str:
    """``PATH`` for the server: this interpreter's scripts directory first, then the test's own."""
    return sysconfig.get_path("scripts") + os.pathsep + os.environ.get("PATH", os.defpath)


def server_environment(stanza: Stanza) -> dict[str, str]:
    """The stanza's ``env`` merged over a minimal environment whose ``PATH`` finds this ``swcraft``."""
    base = {name: os.environ[name] for name in CARRIED_VARIABLES if name in os.environ}
    base["PATH"] = scripts_path()
    return {**base, **stanza.env}


def _group_options() -> dict[str, Any]:
    """Popen options that make the child the root of a group :func:`_end_tree` can end.

    Probed by ``os.killpg``: a new session where it exists, else a new process group for ``taskkill /T``.
    """
    if getattr(os, "killpg", None) is not None:
        return {"start_new_session": True}
    return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}


def _end_tree(process: subprocess.Popen[bytes]) -> None:
    """End *process* and its descendants, then reap it with a deadline.

    What it does not reach: a POSIX descendant that started a session of its own, and a Windows
    descendant whose parent exited before ``taskkill`` walked the tree.
    """
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            killpg(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass
    else:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                timeout=EXIT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        process.kill()
    except OSError:
        pass
    process.wait(timeout=EXIT_SECONDS)


def _lines(stdout: IO[bytes], into: queue.Queue[bytes | None]) -> None:
    for line in stdout:
        into.put(line)
    into.put(None)


def _stderr_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as failed:
        return f"<stderr unreadable: {type(failed).__name__}>"


class Server:
    """One started stanza, spoken to over newline-delimited JSON-RPC."""

    def __init__(self, process: subprocess.Popen[bytes], stderr: Path, stanza: Stanza) -> None:
        self.process = process
        self.stderr = stderr
        self.stanza = stanza
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._next_id = 0
        assert process.stdout is not None
        threading.Thread(target=_lines, args=(process.stdout, self._lines), daemon=True).start()

    def _send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.process.stdin.flush()

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """The response to one request, within :data:`RESPONSE_SECONDS`; notifications are skipped."""
        self._next_id += 1
        request_id = self._next_id
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        deadline = time.monotonic() + RESPONSE_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            try:
                line = self._lines.get(timeout=max(0.0, remaining))
            except queue.Empty:
                pytest.fail(
                    f"{self.stanza.origin}: no answer to {method} within {RESPONSE_SECONDS} s; "
                    f"server stderr ends:\n{_stderr_text(self.stderr)[-2000:]}"
                )
            assert line is not None, (
                f"{self.stanza.origin}: the server closed its stdout before answering {method}; "
                f"server stderr ends:\n{_stderr_text(self.stderr)[-2000:]}"
            )
            answer = json.loads(line)
            if answer.get("id") == request_id and "method" not in answer:
                assert "error" not in answer, f"{self.stanza.origin}: {method} answered {answer['error']!r}"
                return answer["result"]

    def initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "client-stanza-test", "version": "1"},
            },
        )
        self.notify("notifications/initialized")
        return result

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})


@contextmanager
def started(stanza: Stanza, cwd: Path, stderr: Path) -> Iterator[Server]:
    """*stanza* as a real process in *cwd*; on leaving, stdin closed and the process reaped.

    The command must resolve to this interpreter's ``swcraft``; any other would test another installation.
    """
    env = server_environment(stanza)
    resolved = shutil.which(stanza.command, path=env["PATH"])
    assert resolved is not None, f"{stanza.origin}: {stanza.command!r} is not found on {env['PATH']!r}"
    scripts = Path(sysconfig.get_path("scripts")).resolve()
    assert Path(resolved).resolve().parent == scripts, (
        f"{stanza.origin}: {stanza.command!r} resolved to {resolved}, not into {scripts.as_posix()}"
    )
    with stderr.open("w", encoding="utf-8", newline="\n") as sink:
        process = subprocess.Popen(
            [resolved, *stanza.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sink,
            env=env,
            cwd=os.fspath(cwd),
            **_group_options(),
        )
        ended = False
        try:
            yield Server(process, stderr, stanza)
            assert process.stdin is not None
            process.stdin.close()
            try:
                process.wait(timeout=EXIT_SECONDS)
                ended = True
            except subprocess.TimeoutExpired:
                pytest.fail(f"{stanza.origin}: the server did not exit within {EXIT_SECONDS} s of its stdin closing")
        except BaseException as failure:
            # The body's failure is the one reported; a failing teardown is attached as a note.
            if not ended:
                ended = True
                try:
                    _end_tree(process)
                except Exception as teardown:
                    failure.add_note(f"{stanza.origin}: ending the server also failed: {teardown!r}")
            raise
        finally:
            if not ended:
                _end_tree(process)


def tool_text(result: dict[str, Any]) -> str:
    """The text of a ``tools/call`` result's first content block."""
    content = result.get("content") or [{}]
    return content[0].get("text", "")


def _the_refusal(result: dict[str, Any]) -> str:
    """What a refused call says: a tool error's text, or the one message of a build tool's error dict."""
    if result.get("isError") is True:
        return tool_text(result)
    body = json.loads(tool_text(result))
    assert body.get("success") is False and len(body.get("errors", [])) == 1, body
    return body["errors"][0]["message"]


def answers_from(stanza: Stanza, cwd: Path, stderr: Path, project_name: str) -> None:
    """Initialize, list the tools, ``discover`` and ``read``, against the project named *project_name*."""
    assert stanza.env.get("SWCRAFT_REVIEW") == REVIEW_MODE, f"{stanza.origin}: env {dict(stanza.env)!r}"
    with started(stanza, cwd, stderr) as server:
        assert server.initialize()["serverInfo"]["name"] == "swcraft"
        listed = {tool["name"] for tool in server.request("tools/list")["tools"]}
        assert listed == tool_names(), f"{stanza.origin}: tools/list differs from the server's own tools"

        discovered = server.call("discover", {})
        assert discovered.get("isError") is not True, f"{stanza.origin}: discover failed: {tool_text(discovered)}"
        discover_body = json.loads(tool_text(discovered))
        assert discover_body["response"] == "discover"
        assert discover_body["project"]["name"] == project_name, discover_body["project"]

        read = server.call("read", {"address": {"layer": "hardware"}})
        assert read.get("isError") is not True, f"{stanza.origin}: read failed: {tool_text(read)}"
        read_body = json.loads(tool_text(read))
        assert read_body["response"] == "read"
        assert "PARTITION0" in {element["name"] for element in read_body["value"]}, read_body


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


def _scaffold(project: Path, name: str) -> None:
    """``swcraft init`` from this checkout, naming the three clients: the default writes none of their files."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "swcraft_tools.cli",
            "init",
            os.fspath(project),
            "--integration",
            "moon",
            "--client",
            "codex",
            "--client",
            "claude",
            "--client",
            "kiro",
            "--name",
            name,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=RESPONSE_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.fixture(scope="module")
def scaffolded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A project from the template, with a nested project in its subfolder ``nested``.

    Module-scoped: no change set is sent, so the server only reads it.
    """
    project = tmp_path_factory.mktemp("stanzas") / "project"
    _scaffold(project, PROJECT_NAME)
    _scaffold(project / "nested", NESTED_NAME)
    return project


# ---------------------------------------------------------------------------
# The expansion rule
# ---------------------------------------------------------------------------


class TestClaudeExpansion:
    """The documented rule, before it is trusted with the template's line."""

    def test_a_set_variable_is_its_value_with_or_without_a_default(self) -> None:
        assert expand_claude("${A}/x", {"A": "/p"}) == "/p/x"
        assert expand_claude("${A:-.}/x", {"A": "/p"}) == "/p/x"

    def test_an_unset_variable_takes_its_default(self) -> None:
        assert expand_claude("${A:-.}/x", {}) == "./x"
        assert expand_claude("${A:-}x", {}) == "x"

    def test_an_unset_variable_without_a_default_refuses_the_file(self) -> None:
        with pytest.raises(UnsetVariable):
            expand_claude("${A}/x", {})

    def test_text_without_an_expansion_is_unchanged(self) -> None:
        assert expand_claude("mcp-serve $A {A}", {"A": "z"}) == "mcp-serve $A {A}"

    def test_the_templates_manifest_argument_both_ways(self, scaffolded: Path) -> None:
        where = scaffolded.resolve().as_posix()
        set_ = template_claude_stanza(scaffolded, {"CLAUDE_PROJECT_DIR": where})
        unset = template_claude_stanza(scaffolded, {})
        assert set_.args[-1] == f"{where}/swcraft.yaml"
        # What Claude Code 2.1.319 produced in both cases.
        assert unset.args[-1] == "./swcraft.yaml"


# ---------------------------------------------------------------------------
# The stanzas start the server
# ---------------------------------------------------------------------------


class TestTheStanzasStartTheServer:
    def test_claude_code_with_its_project_dir_set(self, scaffolded: Path, tmp_path: Path) -> None:
        stanza = template_claude_stanza(scaffolded, {"CLAUDE_PROJECT_DIR": scaffolded.resolve().as_posix()})
        answers_from(stanza, scaffolded, tmp_path / "stderr.txt", PROJECT_NAME)

    def test_claude_code_with_its_project_dir_unset(self, scaffolded: Path, tmp_path: Path) -> None:
        stanza = template_claude_stanza(scaffolded, {})
        answers_from(stanza, scaffolded, tmp_path / "stderr.txt", PROJECT_NAME)

    def test_codex(self, scaffolded: Path, tmp_path: Path) -> None:
        answers_from(template_codex_stanza(scaffolded), scaffolded, tmp_path / "stderr.txt", PROJECT_NAME)

    def test_kiro(self, scaffolded: Path, tmp_path: Path) -> None:
        answers_from(template_kiro_stanza(scaffolded), scaffolded, tmp_path / "stderr.txt", PROJECT_NAME)

    @pytest.mark.parametrize("language", ["json", "toml"])
    def test_the_guides_block(self, language: str, scaffolded: Path, tmp_path: Path) -> None:
        for number, stanza in enumerate(guide_stanzas(language)):
            answers_from(
                substitute_placeholder(stanza, scaffolded), scaffolded, tmp_path / f"stderr-{number}.txt", PROJECT_NAME
            )


# ---------------------------------------------------------------------------
# The two stated behaviours of a relative line
# ---------------------------------------------------------------------------


RELATIVE_STANZAS = {
    "claude-code": lambda project: template_claude_stanza(project, {}),
    "codex": template_codex_stanza,
    "kiro": template_kiro_stanza,
}


class TestARelativeLineInAnotherFolder:
    @pytest.mark.parametrize("client", sorted(RELATIVE_STANZAS))
    def test_a_folder_without_a_manifest_names_it_in_every_reply(
        self, client: str, scaffolded: Path, tmp_path: Path
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        stanza = RELATIVE_STANZAS[client](scaffolded)
        with started(stanza, elsewhere, tmp_path / "stderr.txt") as server:
            server.initialize()
            listed = sorted(tool["name"] for tool in server.request("tools/list")["tools"])
            for name in listed:
                result = server.call(name, MINIMAL_ARGUMENTS.get(name, {}))
                if name in ANSWERS_WITHOUT_A_PROJECT:
                    assert result.get("isError") is not True, f"{client}: {name} failed: {tool_text(result)}"
                    continue
                expected = MISSING_RELATIVE_MANIFEST.format(manifest=MANIFEST_AS_GIVEN[client])
                assert _the_refusal(result) == expected, f"{client}: {name}: {tool_text(result)}"

    @pytest.mark.parametrize("mode", sorted(BROKEN_MANIFESTS))
    def test_a_broken_manifest_is_named_by_its_failure_in_every_reply(
        self, mode: str, scaffolded: Path, tmp_path: Path
    ) -> None:
        text, expected = BROKEN_MANIFESTS[mode]
        folder = tmp_path / "broken"
        folder.mkdir()
        manifest = folder / "swcraft.yaml"
        manifest.write_text(text, encoding="utf-8", newline="\n")
        if mode == "unreadable":
            manifest.chmod(0)
            try:
                manifest.read_bytes()
            except PermissionError:
                pass
            else:
                pytest.skip("a manifest with mode 0 stays readable here (root, or no POSIX mode bits)")
        stanza = template_codex_stanza(scaffolded)
        try:
            with started(stanza, folder, tmp_path / "stderr.txt") as server:
                server.initialize()
                listed = sorted(tool["name"] for tool in server.request("tools/list")["tools"])
                for name in listed:
                    if name in ANSWERS_WITHOUT_A_PROJECT:
                        continue
                    result = server.call(name, MINIMAL_ARGUMENTS.get(name, {}))
                    assert _the_refusal(result) == expected, f"{mode}: {name}: {tool_text(result)}"
        finally:
            manifest.chmod(0o644)

    @pytest.mark.parametrize("client", sorted(RELATIVE_STANZAS))
    def test_a_nested_project_is_opened_without_a_word(self, client: str, scaffolded: Path, tmp_path: Path) -> None:
        stanza = RELATIVE_STANZAS[client](scaffolded)
        answers_from(stanza, scaffolded / "nested", tmp_path / "stderr.txt", NESTED_NAME)
