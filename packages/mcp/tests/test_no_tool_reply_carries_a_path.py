r"""No tool reply names where anything is stored: the response-level gate over every MCP tool.

The validator-message and protocol-response gates cannot see a plain-text tool's ``ERROR:`` line,
a dict tool's ``output`` or a host note beside a response; this module checks what a client
receives from every registered tool.

Each probe builds the editing vectors' reference project under :data:`PROBE_DIRNAME`, puts it
into one failure mode and calls one tool through the live in-memory client, so binding, notes and
FastMCP's masking are the real ones. Each probe first proves its mode was reached
(:attr:`Probe.reached`; a masked reply counts only when the server log holds the exception,
:attr:`Reply.logged`). Every text block, every string of the decoded body (keys included) and
every note is scanned.

An AUTOSAR reference such as ``/Pkg/Types/uint8`` is spelled like an absolute POSIX path, so a
string is flagged when it contains: a root the test knows (the probe directory, the system
temporary directory, the home directory, each as given and resolved), the staging prefix
``swcraft-session-``, the export prefix ``swcraft-component-export-``, the probe directory's
name, the name of any temporary directory the call created, a ``~/`` location, a drive-letter
path, or a POSIX-spelled token that exists on disk. A value the request itself carried is removed
first: echoing the client's argument discloses nothing.

What it does not reach: the failure modes were enumerated by a search of the tools and MCP
sources for interpolated paths, and the generator refusals are sampled, so a new site is found
only by re-running such a search; a message Python or a library composes is probed only where
found. A POSIX-spelled path under none of the roots that does not exist on this host is not
flagged, and the server log is not scanned. Every mode the gate cannot trigger is in
:data:`UNREACHED_BY_THE_REPLY_GATE` with its reason.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import editing_vector_fixtures as fixtures
import external_stubs_support as stubs
import impl_composites_support as composites
import pytest
import swcraft_mcp.server as server
import swcraft_tools.editing.read as read_mod
import swcraft_tools.session as session_mod
from conftest import commit_into_a_damaged_repository, commit_link_out_of_the_tree, model_element_path
from fastmcp import Client
from guide_support import fixture_corpus, install_corpus, install_no_site, install_site, real_corpus, write_site
from mode_bit_support import read_denied
from protocol_client import FAIL, ScriptedHuman, body_of, tool_names
from scratch_git import quiet_maintenance
from swcraft_mcp import panel_review
from swcraft_tools import component_exchange, guide_search
from swcraft_tools.auditor import AuditLog
from swcraft_tools.editing.audit import CHANGE_SET_COMMIT_INTENT, CHANGE_SET_COMMITTED, ChangeSetAuditor
from swcraft_tools.editing.read import compact_json
from swcraft_tools.model_state import ElementParseError, ModelRootNotFoundError, RetiredStorageError
from swcraft_tools.project import ManifestPathError
from swcraft_tools.session import CommitPreflightError
from swcraft_tools.swcraft_home import swcraft_home

from tests.test_protocol_change_set import ActingHuman
from tests.test_review_mode import (
    GARBAGE,
    PLAIN,
    SILENT,
    WEAKENING,
    Panel,
    ScriptedTime,
    arguments,
    set_mode,
    take_the_question_name,
    use_project,
)

#: The directory every probe builds its project under. Its name is scanned for
#: too, so a reply that names a location relative to some root still shows.
PROBE_DIRNAME = "no-tool-reply-may-name-this-directory"

#: The prefix of every staging copy ``ScopedSession.open()`` makes.
STAGING_MARK = "swcraft-session-"

#: The prefix of the temporary tree a component export generates its closure in.
EXPORT_MARK = "swcraft-component-export-"

DRIVE_LETTER_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")

#: A location spelled from the user's home directory, which no root matches.
HOME_SPELLED = re.compile(r"(?<![\w~.-])~[\\/]")

#: A component file in an earlier release's writer shape: synthetic and hand-trimmed, never a real export.
EARLIER_RELEASE_EXPORT = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "tools"
    / "tests"
    / "fixtures"
    / "component_exchange"
    / "earlier_release"
    / "ExampleSWC.arxml"
)

#: A token spelled like an absolute POSIX path. Only the ones that EXIST on disk
#: are flagged: an AUTOSAR reference is spelled the same way.
POSIX_SPELLED = re.compile(r"(?<![\w.~-])/[^\s'\"()\[\]{}<>,;|`]+")


# One reply, and what in it names a location.


@dataclass(frozen=True)
class Reply:
    """Everything a client receives from one ``tools/call``."""

    is_error: bool
    structured: Any
    texts: tuple[str, ...]
    #: Every exception the server logged while the call ran, each with its causes.
    #: A masked reply carries no text, so this is the only proof of what raised.
    logged: tuple[BaseException, ...] = ()

    @property
    def text(self) -> str:
        return "\n".join(self.texts)

    def strings(self) -> Iterator[str]:
        """Every text block, then every string in the decoded body, keys included."""
        yield from self.texts
        yield from _strings_in(self.structured)


def _strings_in(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings_in(key)
            yield from _strings_in(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings_in(item)


def filesystem_roots(probe_dir: Path) -> tuple[str, ...]:
    """The concrete locations this host knows, each as given, resolved and POSIX-spelled."""
    candidates = (probe_dir, Path(tempfile.gettempdir()), Path.home())
    spellings: set[str] = set()
    for candidate in candidates:
        for form in (candidate, candidate.resolve()):
            if len(form.parts) < 2:
                # `/` as a root would flag every AUTOSAR reference in every reply.
                continue
            spellings.update({str(form), form.as_posix()})
    return tuple(sorted(spellings, key=len, reverse=True))


def locations_in(
    text: str, roots: tuple[str, ...], echoed: tuple[str, ...] = (), temporary: tuple[str, ...] = ()
) -> list[str]:
    """What in *text* names a filesystem location, after removing the request's own values.

    *temporary* is the name of every temporary directory the call created, so a
    reply naming one relative to its parent, or by its bare name, still shows.
    """
    for value in echoed:
        # Three spellings of one sent argument: as sent; JSON-escaped, as a dict reply's text
        # block carries it (a Windows backslash doubles); and POSIX-spelled, in case the server
        # re-spells what it echoes.
        for spelling in (value, json.dumps(value)[1:-1], value.replace("\\", "/")):
            text = text.replace(spelling, "")
    found = [root for root in roots if root in text]
    found += [mark for mark in (STAGING_MARK, EXPORT_MARK, PROBE_DIRNAME, *temporary) if mark in text]
    found += DRIVE_LETTER_PATH.findall(text)
    found += HOME_SPELLED.findall(text)
    for token in POSIX_SPELLED.findall(text):
        candidate = token.rstrip(".:")
        try:
            exists = Path(candidate).exists()
        except (OSError, ValueError):
            exists = False
        if exists:
            found.append(candidate)
    return found


class _LoggedExceptions(logging.Handler):
    """The exceptions FastMCP logs; its ``fastmcp`` logger does not propagate to the root logger."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.raised: list[BaseException] = []

    def emit(self, record: logging.LogRecord) -> None:
        exc = record.exc_info[1] if record.exc_info else None
        while exc is not None and exc not in self.raised:
            self.raised.append(exc)
            exc = exc.__cause__ or exc.__context__


def call_the_tool(tool: str, named: dict[str, Any], human: ScriptedHuman | None) -> Reply:
    """The raw ``tools/call`` result, error flag and server-logged exceptions included, as a :class:`Reply`."""

    async def go() -> Any:
        async with Client(server.mcp, elicitation_handler=human) as client:
            return await client.call_tool(tool, named, raise_on_error=False)

    handler = _LoggedExceptions()
    logger = logging.getLogger("fastmcp")
    logger.addHandler(handler)
    try:
        result = asyncio.run(go())
    finally:
        logger.removeHandler(handler)
    return reply_of(result, tuple(handler.raised))


def reply_of(result: Any, logged: tuple[BaseException, ...] = ()) -> Reply:
    """A raw ``tools/call`` result as a :class:`Reply`: its decoded body (:func:`body_of`) and every text block."""
    texts = tuple(block.text for block in result.content if getattr(block, "type", None) == "text")
    return Reply(is_error=result.is_error, structured=body_of(result), texts=texts, logged=logged)


# The probes: every tool, every failure mode it can be made to report.


@dataclass
class Setup:
    """What one probe sends, and what the request carried that a reply may echo."""

    arguments: dict[str, Any]
    human: ScriptedHuman | None = None
    echoed: tuple[str, ...] = field(default_factory=tuple)
    #: Held open for the call alone, and released whichever way it goes (a denied read).
    holding: AbstractContextManager[Any] = field(default_factory=nullcontext)


def tool_of(label: str) -> str:
    """The tool a label calls: the part before the first ``/``."""
    return label.partition("/")[0]


@dataclass(frozen=True)
class Probe:
    #: The tool's name, or ``tool/form`` for a second probe of one tool at another
    #: argument shape (:data:`EVERY_COMPONENT`).
    label: str
    mode: str
    #: Put the freshly built project into the mode, and name the call.
    prepare: Callable[[Path, pytest.MonkeyPatch], Setup]
    #: Proof the mode was really reached, read off the reply.
    reached: Callable[[Reply], bool]

    @property
    def tool(self) -> str:
        """The registered tool the probe calls."""
        return tool_of(self.label)

    @property
    def id(self) -> str:
        tool, _, form = self.label.partition("/")
        return f"{tool}/{self.mode}-{form}" if form else f"{tool}/{self.mode}"


def _break_a_file(project: Path) -> None:
    """One element file that does not parse, the file the unfixed reply named."""
    target = project / "model" / "swc" / "example" / "ExampleConsumerSWC.yaml"
    target.write_text("name: [oops\n", encoding="utf-8", newline="\n")


def _plant_a_decision_quoting_the_project(where: str) -> Callable[[Path], None]:
    """A ``trace-decisions.yaml`` whose one record quotes the project's own location, *where* in it.

    ``from``: a confirm's replaced member, which the reader's refusal quotes; ``key``: a key the
    format does not hold, which the refusal's pointer names. ``find`` ``for_requirement`` reads it.
    """

    def plant(project: Path) -> None:
        location = project.as_posix()
        extra = f"  from: '{location}'\n" if where == "from" else f"  '{location}': 1\n"
        decision = "confirm" if where == "from" else "reject"
        (project / "trace-decisions.yaml").write_text(
            "formatVersion: 1\ndecisions:\n- requirement: REQ_EXAMPLE_001\n  version: 1\n"
            f"  target:\n    kind: swc\n    owner: null\n    name: ExampleSWC\n  decision: {decision}\n{extra}",
            encoding="utf-8",
            newline="\n",
        )

    return plant


def _declare_a_requirement_twice(project: Path) -> None:
    """A second file declaring ``REQ_EXAMPLE_001``: the trace search's ambiguous-identity refusal.

    ``find`` ``for_requirement`` forwards :class:`~swcraft_tools.trace_search.AmbiguousIdentityError`'s
    sentence, composed from the stopping rules' names and a count.
    """
    requirements = project / "model" / "requirements"
    text = (requirements / "REQ_EXAMPLE_001.yaml").read_text(encoding="utf-8")
    (requirements / "REQ_EXAMPLE_001_twice.yaml").write_text(text, encoding="utf-8", newline="\n")


def _remove_the_model_root(project: Path) -> None:
    (project / "model").rename(project / "model-moved-away")


def _keep_a_retired_layout(project: Path) -> None:
    """Every implementation type file back in ``data_types/``, a retired directory."""
    model = project / "model"
    for path in sorted((model / "data_types" / "implementation").glob("*.yaml"), key=Path.as_posix):
        path.rename(model / "data_types" / path.name)


def _refuse_the_model(project: Path) -> None:
    """A model every file of which parses and the validator refuses: no processor."""
    (project / "model" / "hardware" / "CPU.yaml").unlink()


def _declare_a_name_twice(project: Path) -> None:
    """A second interface file declaring ``IDiagnosticService``, under the store's own ``~1`` suffix."""
    declared = project / "model" / "interfaces" / "IDiagnosticService.yaml"
    (declared.parent / "IDiagnosticService~1.yaml").write_bytes(declared.read_bytes())


def _declare_a_component_twice(project: Path) -> None:
    declared = project / "model" / "swc" / "example" / "ExampleSWC.yaml"
    twin = project / "model" / "swc" / "other" / "ExampleSWC.yaml"
    twin.parent.mkdir(parents=True)
    twin.write_bytes(declared.read_bytes())


def _withhold_native_declarations(project: Path) -> None:
    """Every base type without ``native_declaration``: the writer refuses, and the export's closure check does not."""
    for base_type in (project / "model" / "data_types" / "base").glob("*_base.yaml"):
        text = base_type.read_text(encoding="utf-8")
        text = re.sub(r"(?m)^native_declaration: .*$", "native_declaration: ''", text)
        base_type.write_text(text, encoding="utf-8", newline="\n")


def _make_dist_a_file(project: Path) -> None:
    """``dist`` a regular file, so creating the output directory raises a real ``OSError`` naming it."""
    (project / "dist").write_text("not a directory\n", encoding="utf-8", newline="\n")


def _sub(path: Path, pattern: str, replacement: str) -> None:
    text, count = re.subn(pattern, replacement, path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    assert count, (path, pattern)
    path.write_text(text, encoding="utf-8", newline="\n")


def _declare_no_root(project: Path) -> None:
    _sub(project / "model" / "compositions" / "Root.yaml", r"^root: true$", "root: false")


def _value_type_over_nothing(project: Path) -> None:
    _sub(
        model_element_path(project / "model", "float32", kind="value_type"), r"^base: float32_base$", "base: NoSuchBase"
    )


def _a_composition_name_no_file_can_carry(project: Path) -> None:
    """The root composition renamed to a name the diagram generator refuses as a file name."""
    _sub(project / "model" / "compositions" / "Root.yaml", r"^name: Root$", "name: Root Frame")


def _no_node_anywhere(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
    """``PATH`` an empty directory, so the diagram layout finds no Node.js to run in."""
    empty = project / "no-node-here"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    return Setup({})


def _a_layout_engine_that_crashes(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
    """The layout script dying on an uncaught error whose stack names its own files.

    What Node prints with the ``elkjs`` payload's entry file removed, spelled under the probe's
    directory so the gate recognises it. Only the bridge's ``subprocess`` is replaced.
    """
    from swcraft_tools.writer.diagrams import layout

    script = project / "installation" / "elk-layout.mjs"

    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if list(argv)[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout=b"v22.13.0\n", stderr=b"")
        stderr = (
            f"Error [ERR_MODULE_NOT_FOUND]: Cannot find module '{script.parent / 'elkjs' / 'lib' / 'main.js'}' "
            f"imported from {script}\n    at finalizeResolution (node:internal/modules/esm/resolve:274:11)\n"
        )
        # The layout run is bytes both ways: the bridge encodes its document before any process.
        return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=stderr.encode("utf-8"))

    fake = SimpleNamespace(
        run=run, DEVNULL=subprocess.DEVNULL, TimeoutExpired=subprocess.TimeoutExpired, CompletedProcess=None
    )
    monkeypatch.setattr(layout, "subprocess", fake)
    monkeypatch.setattr(layout, "resolve_node", lambda: str(project / "installation" / "node"))
    return Setup({})


def _the_validator_accepting(break_it: Callable[[Path], None], *, stub_the_arxml: bool = False) -> Callable[..., Setup]:
    """*break_it*, with ``check_cross_layer_consistency`` reporting the model consistent.

    A generator's refusal on the ``build``/``validate`` route needs a model the validator accepts;
    none is known, so the validator is patched out. With *stub_the_arxml* the ARXML step is
    replaced too, because the writer refuses every model the diagram generator does, first.
    """

    def prepare(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        break_it(project)
        consistent = SimpleNamespace(consistent=True, issues=[], records=[], warnings=())
        monkeypatch.setattr(server.validator, "check_cross_layer_consistency", lambda _root, _version=None: consistent)
        if stub_the_arxml:

            def generate_arxml(_model_root: str, out: str, version: Any = None) -> str:
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                Path(out).write_text("<AUTOSAR/>\n", encoding="utf-8", newline="\n")
                return out

            monkeypatch.setattr(server.writer, "generate_arxml", generate_arxml)
            monkeypatch.setattr(server, "_check_generated_arxml", lambda _path: ([], "stubbed"))
        return Setup({})

    return prepare


def _point_dist_root_at(value: str) -> Callable[[Path], None]:
    """The manifest's ``dist_root`` rewritten, and the cached project dropped so the next call reads it."""

    def rewrite(project: Path) -> None:
        manifest = project / "swcraft.yaml"
        text = re.sub(r"(?m)^dist_root: .*$", f"dist_root: {value}", manifest.read_text(encoding="utf-8"))
        manifest.write_text(text, encoding="utf-8", newline="\n")
        server._PROJECT = None

    return rewrite


def _git(project: Path, *argv: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=probe", "-c", "user.email=probe@example.invalid", *argv],
        cwd=project,
        check=True,
        capture_output=True,
    )
    return done.stdout.decode("utf-8").strip()


def _commit_everything(project: Path) -> None:
    _git(project, "init", "-q")
    quiet_maintenance(project)
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "probe")


def _exported_component(project: Path) -> str:
    """ExampleSWC exported to a file OUTSIDE the project, as an import source."""
    target = project.parent / "exchange" / "ExampleSWC.arxml"
    component_exchange.export_component(str(project / "model"), "ExampleSWC", str(target))
    return str(target)


def _blocked_log(project: Path) -> AuditLog:
    """An audit log whose directory is a FILE, so appending raises a real ``OSError``."""
    blocker = project / "dist" / "blocker"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not a directory\n", encoding="utf-8", newline="\n")
    return AuditLog(str(blocker / "audit.jsonl"), project_root=str(project))


def _fail_one_lifecycle_append(project: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    """Route one lifecycle record to the blocked log; every other record is written as usual."""
    blocked = _blocked_log(project)
    original = ChangeSetAuditor._append

    def _append(self: ChangeSetAuditor, name: str, args: dict[str, Any], result: str) -> None:
        if name == operation:
            blocked.log_write(name, {"change_set_id": self.change_set_id, **args}, result, self.scope_id)
            return
        original(self, name, args, result)

    monkeypatch.setattr(ChangeSetAuditor, "_append", _append)


def _plain(project: Path, monkeypatch: pytest.MonkeyPatch, *, elicit: bool = False) -> dict[str, Any]:
    set_mode(monkeypatch, server.REVIEW_ELICIT if elicit else None)
    return arguments(PLAIN, project)


#: The label of ``export_component``'s second form, the export of every component (no
#: ``swc_name``). Its probes are ``tool/<mode>-<form>``, so they collide with none of the first's.
EVERY_COMPONENT = "export_component/every-component"


#: The arguments of each tool's ordinary, successful call on the reference project,
#: keyed by label (:data:`EVERY_COMPONENT`).
SUCCESS_ARGUMENTS: dict[str, Callable[[Path], dict[str, Any]]] = {
    "discover": lambda _project: {},
    "read": lambda _project: {"address": {"layer": "swc"}},
    "find": lambda _project: {"names": ["ExampleSWC", "example consumer"]},
    "change_set": lambda project: arguments(PLAIN, project),
    "model_diff": lambda _project: {},
    "impact_of": lambda _project: {"name": "IExample"},
    "resource_report": lambda _project: {},
    "architecture_metrics": lambda _project: {},
    "build": lambda _project: {},
    "validate": lambda _project: {},
    "export_component": lambda _project: {"swc_name": "ExampleSWC"},
    EVERY_COMPONENT: lambda _project: {},
    "import_component": lambda project: {
        "source": _exported_component(project),
        "baseline": fixtures.current_baseline_token(project),
    },
}

TEXT_TOOLS = frozenset({"model_diff", "impact_of", "resource_report", "architecture_metrics"})
DICT_TOOLS = frozenset({"build", "validate", "export_component", "import_component"})
PROTOCOL_TOOLS = frozenset({"discover", "read", "find", "change_set"})


def _echo_of(named: dict[str, Any]) -> tuple[str, ...]:
    """The request's own path-valued arguments, which a reply may echo."""
    return tuple(named[key] for key in ("source", "output_path", "output_dir") if named.get(key))


def _answered_ok(label: str) -> Callable[[Reply], bool]:
    tool = tool_of(label)

    def reached(reply: Reply) -> bool:
        if reply.is_error:
            return False
        if tool in TEXT_TOOLS:
            return not reply.text.startswith("ERROR")
        if tool in DICT_TOOLS:
            return reply.structured.get("success") is True
        if tool in PROTOCOL_TOOLS:
            return "error" not in reply.structured
        return True

    return reached


def _refused(reply: Reply) -> bool:
    """The tool said it could not answer, in whichever of its own shapes it says that.

    A masked reply is not one: every unexpected exception produces it, so it proves nothing about
    which fault fired. A masked mode names what must have raised, through :func:`_refused_or_raised`.
    """
    if reply.is_error:
        return False
    if reply.text.startswith("ERROR"):
        return True
    structured = reply.structured or {}
    return structured.get("success") is False or "error" in structured or structured.get("ok") is False


def _refused_or_raised(*raised: type[BaseException]) -> Callable[[Reply], bool]:
    """Refused in the tool's own shape, or masked with one of *raised* in the server log."""

    def reached(reply: Reply) -> bool:
        if reply.is_error:
            return any(isinstance(exc, raised) for exc in reply.logged)
        return _refused(reply)

    return reached


def _protocol_error(code: str) -> Callable[[Reply], bool]:
    return lambda reply: not reply.is_error and (reply.structured or {}).get("error") == code


def _status(status: str) -> Callable[[Reply], bool]:
    return lambda reply: not reply.is_error and (reply.structured or {}).get("status") == status


def _note(fragment: str) -> Callable[[Reply], bool]:
    return lambda reply: any(fragment in text for text in reply.texts[1:])


def _success(label: str) -> Probe:
    """The ordinary call, with nothing broken: :func:`_after` with no fault."""
    return _after(label, "success", lambda _project: None, _answered_ok(label))


def _after(
    label: str, mode: str, break_it: Callable[[Path], None], reached: Callable[[Reply], bool] = _refused
) -> Probe:
    """The ordinary call, made after *break_it* has put the project into *mode*."""

    def prepare(project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
        if tool_of(label) == "model_diff":
            # Committed first, so the fault is the working tree's and not "not a git repository".
            _commit_everything(project)
        named = SUCCESS_ARGUMENTS[label](project)
        break_it(project)
        return Setup(named, echoed=_echo_of(named))

    return Probe(label, mode, prepare, reached)


def _with(label: str, mode: str, named: dict[str, Any], reached: Callable[[Reply], bool] = _refused) -> Probe:
    """A call whose own arguments are the failure."""
    return Probe(label, mode, lambda _project, _monkeypatch: Setup(dict(named)), reached)


#: The sentence a missing manifest given with a location of its own answers (``server.missing_manifest_sentence``):
#: its file name alone, never the directory the probe gave.
MISSING_ABSOLUTE_MANIFEST = (
    "No SWCraft project: the manifest given to the server (swcraft.yaml) was not found; "
    "correct its path in the client's server configuration."
)


def _manifest_modes(label: str) -> tuple[Probe, ...]:
    def unset(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        named = SUCCESS_ARGUMENTS[label](project)
        monkeypatch.delenv("SWCRAFT_MANIFEST")
        server._PROJECT = None
        return Setup(named, echoed=_echo_of(named))

    def missing(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        named = SUCCESS_ARGUMENTS[label](project)
        monkeypatch.setenv("SWCRAFT_MANIFEST", str(project / "nowhere" / "swcraft.yaml"))
        server._PROJECT = None
        return Setup(named, echoed=_echo_of(named))

    def unset_was_refused(reply: Reply) -> bool:
        # A deliberate refusal, not a masked one: the tool raises a ToolError whose text is the sentence.
        return "SWCRAFT_MANIFEST environment variable is not set" in "\n".join(reply.strings())

    def missing_was_refused(reply: Reply) -> bool:
        # A deliberate refusal naming the manifest by its file name alone: a ToolError's text or,
        # for the tools of `_run_with_error_shape`, the error dict's message.
        return MISSING_ABSOLUTE_MANIFEST in "\n".join(reply.strings())

    def says(fragment: str) -> Callable[[Reply], bool]:
        # A deliberate refusal on every project tool, never masked: a ToolError's text or, for the
        # tools of `_run_with_error_shape`, the error dict's message.
        return lambda reply: all(part in "\n".join(reply.strings()) for part in (BROKEN_MANIFEST_OPENING, fragment))

    return (
        Probe(label, "manifest-unset", unset, unset_was_refused),
        Probe(label, "manifest-not-found", missing, missing_was_refused),
        _after(
            label,
            "manifest-root-outside-the-project",
            _point_dist_root_at("../outside"),
            says("Manifest field 'dist_root' resolves outside the project root."),
        ),
        _after(
            label,
            "manifest-root-overlaps-the-model",
            _point_dist_root_at("model/dist"),
            says("Manifest field 'dist_root' overlaps 'model_root'."),
        ),
        _after(
            label,
            "manifest-invalid",
            _point_model_root_at_a_list_of_locations,
            says("field `model_root` is invalid (string_type)"),
        ),
        _after(
            label,
            "manifest-root-written-absolute",
            _point_dist_root_at_the_project_absolutely,
            says("writes field `dist_root` as an absolute path"),
        ),
    )


#: How every refusal of a manifest that is there and cannot be used opens (``server.broken_manifest_sentence``).
BROKEN_MANIFEST_OPENING = "The SWCraft project cannot be opened: "


def _point_model_root_at_a_list_of_locations(project: Path) -> None:
    """``model_root`` written as a LIST of locations, which pydantic refuses quoting the input value.

    The probe's own directory, a Windows drive path, and another user's POSIX home path, which the
    scan cannot flag; ``test_a_broken_manifest_quotes_none_of_its_values`` holds every reply to the
    absence of all three.
    """
    manifest = project / "swcraft.yaml"
    values = json.dumps([str(project / "secret-model"), *FOREIGN_MANIFEST_LOCATIONS])
    # A function as the replacement: the Windows value's backslashes are not a template's escapes.
    text = re.sub(r"(?m)^model_root: .*$", lambda _line: f"model_root: {values}", manifest.read_text(encoding="utf-8"))
    manifest.write_text(text, encoding="utf-8", newline="\n")
    server._PROJECT = None


#: The two locations of another machine ``manifest-invalid`` writes into ``model_root``.
FOREIGN_MANIFEST_LOCATIONS = ("C:\\Users\\someone\\secret\\model", "/Users/someone/secret/model")


def _point_dist_root_at_the_project_absolutely(project: Path) -> None:
    """``dist_root`` written as an absolute path inside the project, which the loader's sentence quoted."""
    _point_dist_root_at(json.dumps((project / "dist").as_posix()))(project)


def _unparseable_was_read(label: str) -> Callable[[Reply], bool]:
    """The broken file was really read: ``discover`` parses no element, so it answers as usual."""
    tool = tool_of(label)
    if tool == "discover":
        return _answered_ok(label)

    def reached(reply: Reply) -> bool:
        if reply.is_error:
            # Masked: the tool let the reader's refusal out, and only the log says so.
            return any(isinstance(exc, ElementParseError) for exc in reply.logged)
        if tool in PROTOCOL_TOOLS:
            return (reply.structured or {}).get("error") == "consistency-violation"
        return _refused(reply) and "YAML" in "\n".join(reply.strings())

    return reached


def _model_root_was_missing(label: str) -> Callable[[Reply], bool]:
    """``resource_report`` answers a report with a topology finding rather than refusing."""
    if tool_of(label) == "resource_report":
        return lambda reply: not reply.is_error and "hardware.layer-absent" in reply.text
    return _refused_or_raised(ModelRootNotFoundError)


def _retired_layout_was_read(label: str) -> Callable[[Reply], bool]:
    """The protocol answers a retired layout with its code and no address; every other tool refuses it."""
    if tool_of(label) in PROTOCOL_TOOLS:

        def reached(reply: Reply) -> bool:
            structured = reply.structured or {}
            return (
                not reply.is_error
                and structured.get("error") == "consistency-violation"
                and "retired layout" in str(structured)
            )

        return reached
    return _refused_or_raised(RetiredStorageError)


def _model_modes(label: str) -> tuple[Probe, ...]:
    return (
        _after(label, "element-file-does-not-parse", _break_a_file, _unparseable_was_read(label)),
        _after(label, "model-root-missing", _remove_the_model_root, _model_root_was_missing(label)),
        _after(label, "model-on-a-retired-layout", _keep_a_retired_layout, _retired_layout_was_read(label)),
    )


def _over_budget(
    tool: str, named: dict[str, Any], reached: Callable[[Reply], bool], mode: str = "over-budget"
) -> Probe:
    def prepare(_project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        # The budget is read at the call, so a small one makes any answer over it.
        monkeypatch.setattr(read_mod, "RESPONSE_BUDGET_WIRE_BYTES", 50)
        return Setup(dict(named))

    return Probe(tool, mode, prepare, reached)


def _change_set_modes() -> tuple[Probe, ...]:
    def committed(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        return Setup(_plain(project, monkeypatch, elicit=True), ScriptedHuman(True, True))

    def declined(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        return Setup(_plain(project, monkeypatch, elicit=True), ScriptedHuman(False))

    def refused(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        named = _plain(project, monkeypatch)
        return Setup({"baseline": named["baseline"], "operations": [{"operation": "rename"}]})

    def stale(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        named = _plain(project, monkeypatch)
        fixtures.make_stale(project)
        return Setup(named)

    def conflict(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        named = _plain(project, monkeypatch, elicit=True)

        def touch() -> None:
            (project / "model" / "note.txt").write_text("a note\n", encoding="utf-8", newline="\n")

        return Setup(named, ActingHuman(touch, True, True))

    def unreachable_human(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        return Setup(_plain(project, monkeypatch, elicit=True), ScriptedHuman(FAIL))

    def replay_write_fails(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        named = _plain(project, monkeypatch, elicit=True)
        blocked = _blocked_log(project)
        original = AuditLog.replay_entry

        def replay_into_the_blocked_log(_self: AuditLog, entry: Any, scope_id: str | None = None) -> None:
            # Only the replay of the session's writer entries fails, after the apply;
            # the lifecycle records before it are written to the real log as usual.
            original(blocked, entry, scope_id)

        monkeypatch.setattr(AuditLog, "replay_entry", replay_into_the_blocked_log)
        return Setup(named, ScriptedHuman(True, True))

    def completion_write_fails(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        named = _plain(project, monkeypatch, elicit=True)
        _fail_one_lifecycle_append(project, monkeypatch, CHANGE_SET_COMMITTED)
        return Setup(named, ScriptedHuman(True, True))

    def intent_write_fails(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        named = _plain(project, monkeypatch, elicit=True)
        _fail_one_lifecycle_append(project, monkeypatch, CHANGE_SET_COMMIT_INTENT)
        return Setup(named, ScriptedHuman(True, True))

    def through_the_panel(*answers: str | None) -> Callable[[Path, pytest.MonkeyPatch], Setup]:
        # `SWCRAFT_REVIEW=panel`, a thread playing the extension's review tab
        # for the call alone; the home is the per-test one `conftest.py` sets.
        def prepare(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
            set_mode(monkeypatch, server.REVIEW_PANEL)
            return Setup(arguments(PLAIN, project), holding=Panel(*answers).running())

        return prepare

    def panel_home_unwritable(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        # A file where the SWCraft home should be: `reviews_dir()`'s mkdir fails,
        # and the OSError's own text would carry the home's path.
        set_mode(monkeypatch, server.REVIEW_PANEL)
        home = Path(swcraft_home(os.environ, os.name, Path.home()))
        home.parent.mkdir(parents=True, exist_ok=True)
        home.write_text("not a directory\n", encoding="utf-8", newline="\n")
        return Setup(arguments(PLAIN, project))

    def panel_home_inside_the_project(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        # The project folder holds the SWCraft home: the hand-off is refused, and
        # the refusal names neither the home nor the project.
        set_mode(monkeypatch, server.REVIEW_PANEL)
        home = project / "user-home"
        home.mkdir()
        for variable in ("HOME", "LOCALAPPDATA", "USERPROFILE"):
            monkeypatch.setenv(variable, os.fspath(home))
        return Setup(arguments(PLAIN, project))

    def panel_question_name_taken(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        # Another server's live question holds this change set's name (one id
        # drawn twice in a second): the refusal names neither the home nor it.
        set_mode(monkeypatch, server.REVIEW_PANEL)
        take_the_question_name(monkeypatch)
        return Setup(arguments(PLAIN, project))

    def tripwire(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        named = _plain(project, monkeypatch, elicit=True)
        # The defect the tripwire is the net for: a reviewed diff that accounts for no file.
        monkeypatch.setattr(session_mod, "_touched_identities", lambda _diff, registry=None: {})
        return Setup(named, ScriptedHuman(True, True))

    return (
        Probe("change_set", "committed", committed, _status("committed")),
        Probe("change_set", "declined", declined, _status("abandoned")),
        Probe("change_set", "refused", refused, _protocol_error("schema-violation")),
        Probe("change_set", "stale-baseline", stale, _protocol_error("stale-baseline")),
        Probe("change_set", "stale-at-commit", conflict, _protocol_error("stale-baseline")),
        Probe("change_set", "human-unreachable", unreachable_human, _note("could not be asked")),
        Probe("change_set", "audit-replay-write-fails", replay_write_fails, _note("audit trail is incomplete")),
        Probe(
            "change_set", "completion-record-write-fails", completion_write_fails, _note("audit trail is incomplete")
        ),
        Probe("change_set", "intent-record-write-fails", intent_write_fails, _refused_or_raised(CommitPreflightError)),
        Probe("change_set", "commit-tripwire", tripwire, _note("tripwire refused it")),
        Probe("change_set", "panel-approved", through_the_panel("approve"), _status("committed")),
        Probe("change_set", "panel-rejected", through_the_panel("reject"), _status("abandoned")),
        # The panel's wait has no deadline: a silent panel's call ends when the client closes its
        # session, injected while the client still reads the reply, so the note is scanned.
        Probe(
            "change_set",
            "panel-session-closed",
            through_the_panel(SILENT),
            _note("the client closed its session with the server"),
        ),
        Probe("change_set", "panel-decision-mismatch", through_the_panel(GARBAGE), _note("did not match the question")),
        Probe(
            "change_set",
            "panel-home-unwritable",
            panel_home_unwritable,
            _note("could not hand the review to the SWCraft panel"),
        ),
        Probe(
            "change_set",
            "panel-home-inside-the-project",
            panel_home_inside_the_project,
            _note("the project folder contains the SWCraft home"),
        ),
        Probe(
            "change_set",
            "panel-question-name-taken",
            panel_question_name_taken,
            _note("already holds this change set's question"),
        ),
    )


def _model_diff_modes() -> tuple[Probe, ...]:
    def not_a_repository(_project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
        return Setup({})

    def a_git_directory_that_is_not_there(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        # Git quotes the directory it was told to use back in its refusal, on any file system.
        monkeypatch.setenv("GIT_DIR", str(project.parent / "no-such-git-directory"))
        return Setup({})

    def a_damaged_repository(project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
        commit_into_a_damaged_repository(project, project.parent / "separate.git")
        return Setup({})

    def a_link_out_of_the_tree(project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
        # `tarfile` names a path under the temporary root, which the gate knows.
        _commit_everything(project)
        commit_link_out_of_the_tree(project, project.parent)
        return Setup({})

    def unknown_revision(project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
        _commit_everything(project)
        return Setup({"rev_a": "no-such-revision"})

    def revision_without_the_model(project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
        _git(project, "init", "-q")
        quiet_maintenance(project)
        _git(project, "add", "swcraft.yaml")
        _git(project, "commit", "-q", "-m", "manifest only")
        return Setup({})

    def unparseable_at_a_revision(project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
        # The working-tree file is restored after the commit, so the only broken copy is the
        # revision's `git archive` extraction: a refusal naming the file came from there.
        target = project / "model" / "swc" / "example" / "ExampleConsumerSWC.yaml"
        intact = target.read_bytes()
        _break_a_file(project)
        _commit_everything(project)
        target.write_bytes(intact)
        return Setup({})

    def the_revision_was_read(reply: Reply) -> bool:
        return _refused(reply) and "Invalid SWC YAML in swc/example/ExampleConsumerSWC.yaml" in reply.text

    def changed_since_head(
        project: Path, monkeypatch: pytest.MonkeyPatch, *, budget: int | None, form: str = "full"
    ) -> Setup:
        # Each form of a real change, answered, and above a small budget refused.
        _commit_everything(project)
        (project / "model" / "swc" / "example" / "ExampleConsumerSWC.yaml").unlink()
        if budget is not None:
            monkeypatch.setattr(read_mod, "RESPONSE_BUDGET_WIRE_BYTES", budget)
        return Setup({"form": form, "rev_a": "HEAD", "rev_b": ""})

    return (
        Probe(
            "model_diff",
            "full",
            lambda project, monkeypatch: changed_since_head(project, monkeypatch, budget=None),
            _answered_ok("model_diff"),
        ),
        Probe(
            "model_diff",
            "full-over-budget",
            lambda project, monkeypatch: changed_since_head(project, monkeypatch, budget=20),
            _refused,
        ),
        Probe(
            "model_diff",
            "summary",
            lambda project, monkeypatch: changed_since_head(project, monkeypatch, budget=None, form="summary"),
            _answered_ok("model_diff"),
        ),
        Probe(
            "model_diff",
            "summary-over-budget",
            lambda project, monkeypatch: changed_since_head(project, monkeypatch, budget=20, form="summary"),
            _refused,
        ),
        _with("model_diff", "refused-argument", {"form": "index"}),
        Probe("model_diff", "not-a-git-repository", not_a_repository, _refused),
        Probe("model_diff", "not-a-git-repository-naming-a-location", a_git_directory_that_is_not_there, _refused),
        Probe("model_diff", "unknown-revision", unknown_revision, _refused),
        Probe("model_diff", "git-archive-naming-a-location", a_damaged_repository, _refused),
        Probe("model_diff", "extraction-naming-a-location", a_link_out_of_the_tree, _refused),
        Probe("model_diff", "revision-without-the-model-root", revision_without_the_model, _refused),
        Probe(
            "model_diff", "element-file-does-not-parse-at-a-revision", unparseable_at_a_revision, the_revision_was_read
        ),
    )


def _architecture_metrics_modes() -> tuple[Probe, ...]:
    """A base revision read and compared, and each way a named revision or the answer's size refuses it."""

    def committed(named: dict[str, Any]) -> Callable[[Path, pytest.MonkeyPatch], Setup]:
        def prepare(project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
            _commit_everything(project)
            (project / "model" / "swc" / "example" / "ExampleConsumerSWC.yaml").unlink()
            return Setup(dict(named))

        return prepare

    return (
        Probe(
            "architecture_metrics",
            "against-a-revision",
            committed({"since": "HEAD"}),
            _answered_ok("architecture_metrics"),
        ),
        Probe("architecture_metrics", "unknown-revision", committed({"since": "no-such-revision"}), _refused),
        _with("architecture_metrics", "not-a-git-repository", {"since": "HEAD"}),
        _over_budget("architecture_metrics", {}, _refused),
    )


#: The fixture guide's section every ``guide`` probe that reads one reads.
GUIDE_SECTION = "index#reading"


def _guide(
    mode: str, named: dict[str, Any], corpus: Callable[[], Any] | None, reached: Callable[[Reply], bool]
) -> Probe:
    """A ``guide`` call with *corpus* installed under the probe directory (``None``: no guide installed).

    The site is written under :data:`PROBE_DIRNAME`, so a reply naming its location carries a root
    the scan flags.
    """

    def prepare(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        if corpus is None:
            install_no_site(monkeypatch, project.parent)
        else:
            install_corpus(monkeypatch, project.parent / "installed-guide", corpus())
        return Setup(dict(named))

    return Probe("guide", mode, prepare, reached)


def _guide_answered(mode: str) -> Callable[[Reply], bool]:
    return lambda reply: (
        not reply.is_error
        and (reply.structured or {}).get("response") == "guide"
        and reply.structured.get("mode") == mode
    )


def _guide_refused(code: str) -> Callable[[Reply], bool]:
    return lambda reply: not reply.is_error and reply.text.startswith(f"ERROR: {code}: ")


def _another_release() -> dict[str, Any]:
    corpus = fixture_corpus()
    corpus["swcraft_version"] = "0.0.1"
    return corpus


def _guide_modes() -> tuple[Probe, ...]:
    """Every mode of ``guide`` and every refusal it answers, over the fixture guide.

    The real guide's every part is scanned by
    :meth:`TestNoToolReplyCarriesAFilesystemPath.test_no_guide_reply_over_the_real_guide_names_a_location`.
    """

    def no_manifest(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        install_corpus(monkeypatch, project.parent / "installed-guide", fixture_corpus())
        monkeypatch.delenv("SWCRAFT_MANIFEST")
        server._PROJECT = None
        return Setup({"section": GUIDE_SECTION})

    def over_budget(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        install_corpus(monkeypatch, project.parent / "installed-guide", fixture_corpus())
        monkeypatch.setattr(guide_search, "GUIDE_REPLY_BUDGET_WIRE_BYTES", 50)
        return Setup({"section": GUIDE_SECTION})

    return (
        _guide("success", {"search": "reading sections"}, fixture_corpus, _guide_answered("search")),
        _guide("a-refusal-code", {"search": "stale-baseline"}, fixture_corpus, _guide_answered("search")),
        _guide("toc", {"toc": True}, fixture_corpus, _guide_answered("toc")),
        _guide("toc-of-a-page", {"toc": True, "page": "index"}, fixture_corpus, _guide_answered("toc")),
        _guide("a-section", {"section": GUIDE_SECTION}, fixture_corpus, _guide_answered("section")),
        _guide("a-page", {"section": "index"}, fixture_corpus, _guide_answered("section")),
        Probe("guide", "manifest-unset", no_manifest, _guide_answered("section")),
        _guide("two-modes", {"search": "x", "toc": True}, fixture_corpus, _guide_refused("guide-mode")),
        _guide(
            "unknown-section", {"section": "index#readings"}, fixture_corpus, _guide_refused("guide-unknown-section")
        ),
        _guide(
            "unknown-part", {"section": GUIDE_SECTION, "part": 9}, fixture_corpus, _guide_refused("guide-unknown-part")
        ),
        _guide(
            "request-too-long",
            {"search": "x" * (guide_search.REQUEST_CAP_CHARACTERS + 1)},
            fixture_corpus,
            _guide_refused("guide-request-too-long"),
        ),
        _guide("not-installed", {"toc": True}, None, _guide_refused("guide-missing")),
        _guide("site-without-a-corpus", {"toc": True}, lambda: None, _guide_refused("guide-missing")),
        _guide("another-release", {"toc": True}, _another_release, _guide_refused("guide-version")),
        _guide("unreadable", {"toc": True}, lambda: "{not json", _guide_refused("guide-unreadable")),
        Probe("guide", "over-budget", over_budget, _guide_refused("guide-over-budget")),
    )


def _import_modes() -> tuple[Probe, ...]:
    def absent_source(project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
        source = str(project.parent / "exchange" / "absent.arxml")
        return Setup({"source": source, "baseline": fixtures.current_baseline_token(project)}, echoed=(source,))

    def not_an_export(project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
        source_path = project.parent / "exchange" / "not-an-export.arxml"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("this is not ARXML\n", encoding="utf-8", newline="\n")
        return Setup(
            {"source": str(source_path), "baseline": fixtures.current_baseline_token(project)},
            echoed=(str(source_path),),
        )

    def earlier_release(project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
        source = str(EARLIER_RELEASE_EXPORT)
        return Setup({"source": source, "baseline": fixtures.current_baseline_token(project)}, echoed=(source,))

    return (
        Probe("import_component", "source-missing", absent_source, _refused),
        Probe("import_component", "source-not-an-export", not_an_export, _refused),
        _after("import_component", "target-declares-a-name-in-two-files", _declare_a_name_twice),
        Probe("import_component", "source-written-by-an-earlier-release", earlier_release, _refused),
    )


def _export_form_refusals() -> tuple[Probe, ...]:
    """A path argument of ``export_component``'s other form, refused before the project is read.

    The given path is deliberately not in ``echoed``: the refusal is a fixed sentence, so a reply
    repeating the path would be a carrier.
    """

    def directory_with_a_name(project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
        return Setup({"swc_name": "ExampleSWC", "output_dir": str(project / "elsewhere")})

    def file_without_a_name(project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
        return Setup({"output_path": str(project / "elsewhere" / "ExampleSWC.arxml")})

    return (
        Probe("export_component", "path-argument-of-the-other-form", directory_with_a_name, _refused),
        Probe(EVERY_COMPONENT, "path-argument-of-the-other-form", file_without_a_name, _refused),
    )


def _unreadable_model_file(label: str) -> Callable[[Path, pytest.MonkeyPatch], Setup]:
    """*label*'s ordinary call with one model file the process may not read: a ``PermissionError`` naming it."""

    def prepare(project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
        named = SUCCESS_ARGUMENTS[label](project)
        denied = read_denied(project / "model" / "swc" / "example" / "ExampleConsumerSWC.yaml")
        return Setup(named, echoed=_echo_of(named), holding=denied)

    return prepare


def _declare_the_stubs(project: Path) -> None:
    """``external_stubs_support``'s stubs, ports, connectors and delegations put into the probe project."""
    stubs.add_external_stubs(project / "model")


#: An interface stub ``external_ref`` (spelled like an absolute POSIX path) with one segment that
#: is not an Identifier, so the validator and the writer quote it back in their refusals.
MALFORMED_STUB_REF = "/Foreign/Port-Interfaces/IForeignBad"


def _declare_a_malformed_stub(project: Path) -> None:
    _declare_the_stubs(project)
    stubs.interface_stub(project / "model", "IForeignBad", MALFORMED_STUB_REF, "SENDER-RECEIVER-INTERFACE")


def _says(
    fragment: str, reached: Callable[[Reply], bool] = lambda reply: not reply.is_error
) -> Callable[[Reply], bool]:
    return lambda reply: reached(reply) and fragment in "\n".join(reply.strings())


def _external_stub_modes() -> tuple[Probe, ...]:
    """Every answer external interface stubs give a tool, on the probe project with the stubs in it.

    A connector across a stub staged (*not judged*) and refused for a kind mismatch, a ``read``
    and an ``impact_of`` of a stub, pass D's refusal of a malformed one, ``build`` and
    ``validate`` over the stubs, the writer's refusal with the validator patched to accept, and the
    component export's refusal of a closure reaching a stub. A stub's ``external_ref`` is quoted on
    purpose and names no location on this host; the replies carrying one are pinned in
    :data:`WHAT_EACH_REPLY_SAYS`.
    """

    def stubbed(tool: str, named: dict[str, Any], also: Callable[[Path], None] = lambda _project: None) -> Callable:
        def prepare(project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
            _declare_the_stubs(project)
            also(project)
            return Setup(dict(named))

        return prepare

    def refused_by_pass_d(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        _declare_the_stubs(project)
        set_mode(monkeypatch, None)
        return Setup(
            {
                "baseline": fixtures.current_baseline_token(project),
                "intent": "declare a malformed interface stub",
                "operations": [
                    {
                        "operation": "create",
                        "address": {"layer": "interfaces", "element": "IForeignBad"},
                        "value": {
                            "name": "IForeignBad",
                            "kind": "external_interface",
                            "external_ref": MALFORMED_STUB_REF,
                            "external_dest": "SENDER-RECEIVER-INTERFACE",
                        },
                    }
                ],
            }
        )

    def connected(provider_port: str) -> Callable:
        def prepare(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
            _declare_the_stubs(project)
            set_mode(monkeypatch, None)
            return Setup(
                {
                    "baseline": fixtures.current_baseline_token(project),
                    "intent": "connect a port across an external interface stub",
                    "operations": [
                        {
                            "operation": "create",
                            "address": {"layer": "connectors", "element": "AcrossTheStub"},
                            "value": {
                                "name": "AcrossTheStub",
                                "from": {"swc": "ExampleSWC", "port": provider_port},
                                "to": {"swc": "ExampleConsumerSWC", "port": "rIn"},
                            },
                        }
                    ],
                }
            )

        return prepare

    return (
        Probe(
            "change_set",
            "connector-not-judged-across-an-external-stub",
            connected("pSr"),
            lambda reply: not reply.is_error and (reply.structured or {}).get("status") == "staged",
        ),
        Probe(
            "change_set",
            "connector-kinds-differ-across-an-external-stub",
            connected("pCs"),
            _says("interface kinds differ", _protocol_error("consistency-violation")),
        ),
        Probe(
            "read",
            "an-external-interface-stub",
            stubbed("read", {"address": {"layer": "interfaces", "element": "IForeignSR"}, "form": "full"}),
            _answered_ok("read"),
        ),
        Probe(
            "impact_of",
            "an-external-interface-stub",
            stubbed("impact_of", {"name": "IForeignSR"}),
            _answered_ok("impact_of"),
        ),
        Probe(
            "change_set", "external-stub-refused-by-pass-d", refused_by_pass_d, _protocol_error("consistency-violation")
        ),
        _after("build", "success-over-external-stubs", _declare_the_stubs, _answered_ok("build")),
        _after("validate", "success-over-external-stubs", _declare_the_stubs, _answered_ok("validate")),
        _after("build", "model-refused-over-an-external-stub", _declare_a_malformed_stub),
        _after("validate", "model-refused-over-an-external-stub", _declare_a_malformed_stub),
        Probe(
            "build",
            "writer-refuses-an-external-stub-the-validator-accepts",
            _the_validator_accepting(_declare_a_malformed_stub),
            _refused,
        ),
        _after("export_component", "closure-reaches-an-external-stub", _declare_the_stubs),
        _after(EVERY_COMPONENT, "closure-reaches-an-external-stub", _declare_the_stubs),
    )


def _place_the_component_stub(project: Path) -> None:
    """``external_stubs_support``'s component stub with ports, wired to ``ExampleConsumerSWC``, put into the project."""
    stubs.add_basic_component_stub(project / "model")


#: The component stub the probes below place, and the port of it that carries a hint.
COMPONENT_STUB, COMPONENT_STUB_REF = stubs.BASIC_COMPONENT_STUB
STUB_PORT = stubs.BASIC_COMPONENT_STUB_PORTS[0][0]

#: The message prefix ``swc.external-with-ports`` keeps for a stub port that carries an interface.
STUB_PORT_REFUSAL = f"SWC '{COMPONENT_STUB}' declares 'external: true' and also ports;"


def _give_the_stub_port_an_interface(project: Path) -> None:
    _place_the_component_stub(project)

    def declare(document: dict[str, Any]) -> None:
        document["ports"][0]["interface"] = "IExample"

    stubs.change(project / "model", f"swc/external/{COMPONENT_STUB}.yaml", declare)


def _component_stub_port_modes() -> tuple[Probe, ...]:
    """Every answer ports on a component stub give a tool, on the probe project.

    A ``read`` of the stub (its ``external_ref`` and its port's hint quoted back), an ``impact_of``,
    pass D's and the validator's refusal of an interface on a stub's port, ``build`` and
    ``validate`` over the wired stub, and the component export's refusal of the stub. The quoted
    ``external_ref`` is an AUTOSAR reference path, as in :func:`_external_stub_modes`.
    """

    def placed(named: dict[str, Any], also: Callable[[Path], None] = _place_the_component_stub) -> Callable:
        def prepare(project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
            also(project)
            return Setup(dict(named))

        return prepare

    def refused_by_pass_d(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        _place_the_component_stub(project)
        set_mode(monkeypatch, None)
        port = {"field": "ports"}, {"member": STUB_PORT}, {"field": "interface"}
        return Setup(
            {
                "baseline": fixtures.current_baseline_token(project),
                "intent": "declare an interface on a component stub's port",
                "operations": [
                    {
                        "operation": "update",
                        "address": {"layer": "swc", "element": COMPONENT_STUB, "path": list(port)},
                        "value": "IExample",
                    }
                ],
            }
        )

    # The connector the stub is placed with is judged by the `build` and `validate` successes.
    return (
        Probe(
            "read",
            "a-component-stub-with-ports",
            placed({"address": {"layer": "swc", "element": COMPONENT_STUB}}),
            _answered_ok("read"),
        ),
        Probe("impact_of", "a-component-stub-with-ports", placed({"name": COMPONENT_STUB}), _answered_ok("impact_of")),
        Probe(
            "change_set",
            "component-stub-port-interface-refused-by-pass-d",
            refused_by_pass_d,
            _protocol_error("consistency-violation"),
        ),
        _after("build", "success-over-component-stub-ports", _place_the_component_stub, _answered_ok("build")),
        _after("validate", "success-over-component-stub-ports", _place_the_component_stub, _answered_ok("validate")),
        _after("validate", "model-refused-over-a-component-stub-port", _give_the_stub_port_an_interface),
        Probe(
            "export_component",
            "a-component-stub",
            placed({"swc_name": COMPONENT_STUB}),
            _refused,
        ),
    )


def _declare_the_composites(project: Path) -> None:
    """``impl_composites_support``'s three kinds, their maps and the prototypes typed by them, put into the project."""
    composites.add_impl_composites(project / "model")


def _declare_a_pointer_cycle(project: Path) -> None:
    _declare_the_composites(project)
    composites.add_pointer_cycles(project / "model")


def _declare_an_array_the_schema_refuses(project: Path) -> None:
    _declare_the_composites(project)
    composites.data_type(
        project / "model",
        {"name": "Spare_T", "kind": "implementation_array", "element_type": "uint8", "length": 0, "element_name": "B"},
    )


def _impl_composite_modes() -> tuple[Probe, ...]:
    """Every answer implementation composites and pointers give a tool, on the probe project.

    A ``read`` of a record and of the ``elements`` slice, ``impact_of`` across the new edges, around
    a pointer cycle and over a file the schema refuses (its storage name, never its location), pass
    D's refusal of a map onto a pointer (``[constr_1066]``), ``build`` and ``validate``, the
    validator's and the writer's refusal of a pointer cycle, and the export of a component whose
    closure reaches every kind. None of their values is an AUTOSAR reference path.
    """

    def declared(named: dict[str, Any], also: Callable[[Path], None] = _declare_the_composites) -> Callable:
        def prepare(project: Path, _monkeypatch: pytest.MonkeyPatch) -> Setup:
            also(project)
            return Setup(dict(named))

        return prepare

    def refused_by_pass_d(project: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
        _declare_the_composites(project)
        set_mode(monkeypatch, None)
        return Setup(
            {
                "baseline": fixtures.current_baseline_token(project),
                "intent": "map an application record onto a pointer",
                "operations": [
                    {
                        "operation": "create",
                        "address": {"layer": "mapping_sets", "element": "ProbeMaps"},
                        "value": {
                            "name": "ProbeMaps",
                            "maps": [{"application": "WheelSpeeds", "implementation": "WheelSpeeds_Ptr"}],
                        },
                    }
                ],
            }
        )

    return (
        Probe(
            "read",
            "an-implementation-record",
            declared({"address": {"layer": "implementation_types", "element": "WheelSpeeds_T"}, "form": "full"}),
            _answered_ok("read"),
        ),
        Probe(
            "read",
            "the-elements-slice",
            declared({"address": {"layer": "implementation_types"}, "members": "elements", "form": "full"}),
            _answered_ok("read"),
        ),
        Probe(
            "impact_of",
            "across-the-implementation-composites",
            declared({"name": "WheelSpeeds_T", "form": "full"}),
            _answered_ok("impact_of"),
        ),
        Probe(
            "impact_of",
            "around-a-pointer-cycle",
            declared({"name": "Ring_Ptr", "form": "full"}, _declare_a_pointer_cycle),
            _answered_ok("impact_of"),
        ),
        Probe(
            "impact_of",
            "an-implementation-array-the-schema-refuses",
            declared({"name": "WheelSpeeds_T"}, _declare_an_array_the_schema_refuses),
            _refused,
        ),
        Probe(
            "change_set",
            "pointer-mapped-refused-by-pass-d",
            refused_by_pass_d,
            _protocol_error("consistency-violation"),
        ),
        _after("build", "success-over-implementation-composites", _declare_the_composites, _answered_ok("build")),
        _after("validate", "success-over-implementation-composites", _declare_the_composites, _answered_ok("validate")),
        _after("validate", "model-refused-over-a-pointer-cycle", _declare_a_pointer_cycle),
        Probe(
            "build",
            "writer-refuses-a-pointer-cycle-the-validator-accepts",
            _the_validator_accepting(_declare_a_pointer_cycle),
            _refused,
        ),
        _after(
            "export_component",
            "closure-reaches-the-implementation-composites",
            _declare_the_composites,
            _answered_ok("export_component"),
        ),
    )


def _probes() -> tuple[Probe, ...]:
    probes: list[Probe] = [_success(label) for label in SUCCESS_ARGUMENTS]
    for label in SUCCESS_ARGUMENTS:
        probes.extend(_manifest_modes(label))
        probes.extend(_model_modes(label))
    probes += [
        _success_variant("discover", "a-layer", {"layer": "swc"}),
        _with("discover", "refused", {"form": "index"}, _protocol_error("schema-violation")),
        _with("discover", "over-budget", {"form": "full"}, _protocol_error("over-budget")),
        _success_variant("read", "an-element", {"address": {"layer": "swc", "element": "ExampleSWC"}}),
        _with("read", "refused", {"address": {"layer": "no_such_layer"}}, _protocol_error("unknown-field")),
        _with(
            "read",
            "unknown-element",
            {"address": {"layer": "swc", "element": "NoSuchSWC"}},
            _protocol_error("broken-reference"),
        ),
        _over_budget(
            "read", {"address": {"layer": "implementation_types"}, "form": "full"}, _protocol_error("over-budget")
        ),
        _with("find", "refused", {"names": []}, _protocol_error("schema-violation")),
        _with("find", "a-near-miss", {"names": ["ExampleSWX", "NoSuchThingAnywhere"]}, _answered_ok("find")),
        # The text form over the requirements, and two forms together refused.
        _with("find", "by-text", {"text": "example functionality"}, _answered_ok("find")),
        _with(
            "find",
            "names-and-text",
            {"names": ["ExampleSWC"], "text": "example"},
            _protocol_error("schema-violation"),
        ),
        # The trace search's ranking for one requirement, an unknown id and `candidates` out of range.
        _with("find", "for-requirement", {"for_requirement": "REQ_EXAMPLE_001"}, _answered_ok("find")),
        _with(
            "find",
            "for-an-unknown-requirement",
            {"for_requirement": "REQ_EXAMPLE_00X"},
            _protocol_error("broken-reference"),
        ),
        _with(
            "find",
            "too-many-candidates",
            {"for_requirement": "REQ_EXAMPLE_001", "candidates": 11},
            _protocol_error("schema-violation"),
        ),
        # A decisions file quoting the project's location, in a value and in a key.
        *(
            Probe(
                "find",
                f"decisions-file-quotes-a-location-in-{where}",
                lambda project, _monkeypatch, where=where: (
                    _plant_a_decision_quoting_the_project(where)(project)
                    or Setup({"for_requirement": "REQ_EXAMPLE_001"})
                ),
                _protocol_error("consistency-violation"),
            )
            for where in ("from", "key")
        ),
        # The ambiguous-identity refusal `for_requirement` forwards.
        Probe(
            "find",
            "for-a-requirement-declared-twice",
            lambda project, _monkeypatch: (
                _declare_a_requirement_twice(project) or Setup({"for_requirement": "REQ_EXAMPLE_001"})
            ),
            lambda reply: (
                _protocol_error("consistency-violation")(reply)
                and "ambiguous identity (name.duplicate-requirement)" in (reply.structured or {}).get("message", "")
            ),
        ),
        *_change_set_modes(),
        *_model_diff_modes(),
        _with("impact_of", "unknown-name", {"name": "NoSuchElement"}, _answered_ok("impact_of")),
        _with("impact_of", "count", {"name": "IExample", "form": "count"}, _answered_ok("impact_of")),
        _with("impact_of", "stale-cursor", {"name": "IExample", "limit": 1, "after": "no-such-row"}),
        _with("impact_of", "refused-argument", {"name": "IExample", "form": "index"}),
        _over_budget("impact_of", {"name": "uint8_base", "form": "full"}, _refused),
        # resource_report's structured answers, and each of its refusals.
        _success_variant("resource_report", "a-core", {"core": "CORE0"}),
        _success_variant("resource_report", "a-memory", {"memory": "RAM0"}),
        _success_variant("resource_report", "a-fit", {"fits": {"memory": "RAM0", "bytes": 1}}),
        _success_variant("resource_report", "a-core-fit", {"fits": {"core": "CORE0", "utilization": "0.05"}}),
        _with("resource_report", "unknown-resource", {"memory": "NoSuchMemory"}),
        _with("resource_report", "two-questions", {"core": "CORE0", "memory": "RAM0"}),
        _with("resource_report", "refused-fit", {"fits": {"memory": "RAM0", "bytes": -1}}),
        _over_budget("resource_report", {}, _refused),
        _over_budget("resource_report", {"memory": "RAM0"}, _refused, mode="a-memory-over-budget"),
        *_architecture_metrics_modes(),
        *_guide_modes(),
        _after("build", "model-refused", _refuse_the_model),
        _after("validate", "model-refused", _refuse_the_model),
        _after("build", "base-type-without-native-declaration", _withhold_native_declarations),
        _after("validate", "base-type-without-native-declaration", _withhold_native_declarations),
        Probe(
            "build",
            "writer-refuses-a-model-the-validator-accepts",
            _the_validator_accepting(_declare_no_root),
            _refused,
        ),
        Probe(
            "validate",
            "writer-refuses-a-model-the-validator-accepts",
            _the_validator_accepting(_value_type_over_nothing),
            _refused,
        ),
        Probe(
            "build",
            "diagrams-refuse-a-model-the-writer-accepts",
            _the_validator_accepting(_a_composition_name_no_file_can_carry, stub_the_arxml=True),
            _refused,
        ),
        Probe("build", "diagrams-without-node", _no_node_anywhere, _refused),
        Probe("build", "diagrams-layout-engine-crashes", _a_layout_engine_that_crashes, _refused),
        _after("build", "dist-is-a-file", _make_dist_a_file),
        _after("validate", "dist-is-a-file", _make_dist_a_file),
        _after("export_component", "dist-is-a-file", _make_dist_a_file),
        Probe("build", "model-file-unreadable", _unreadable_model_file("build"), _refused),
        Probe("export_component", "model-file-unreadable", _unreadable_model_file("export_component"), _refused),
        Probe(EVERY_COMPONENT, "model-file-unreadable", _unreadable_model_file(EVERY_COMPONENT), _refused),
        _with("export_component", "unknown-component", {"swc_name": "NoSuchSWC"}),
        *_export_form_refusals(),
        _after("export_component", "component-declared-in-two-files", _declare_a_component_twice),
        _after("export_component", "closure-refused-by-the-writer", _withhold_native_declarations),
        _after(EVERY_COMPONENT, "closure-refused-by-the-writer", _withhold_native_declarations),
        *_import_modes(),
        *_external_stub_modes(),
        *_component_stub_port_modes(),
        *_impl_composite_modes(),
    ]
    return tuple(probes)


def _success_variant(tool: str, mode: str, named: dict[str, Any]) -> Probe:
    return _with(tool, mode, named, _answered_ok(tool))


PROBES: tuple[Probe, ...] = _probes()

#: Every tool and failure mode no probe reaches, each with its reason: what the gate does not see.
UNREACHED_BY_THE_REPLY_GATE: dict[tuple[str, str], str] = {
    ("build", "override-command"): (
        "SWCRAFT_MCP_BUILD_COMMAND runs a command the HOST configured and echoes that command's own output and "
        "error lines verbatim (builder.run_build); what they carry is the host's command's, not this server's"
    ),
    ("validate", "override-command"): "SWCRAFT_MCP_VALIDATE_COMMAND: the same host-configured command echo as build's",
    ("build", "licensed-xsd"): (
        "the mode line and a schema-compilation headline name the licensed AUTOSAR schema a host configured "
        "(SWCRAFT_AUTOSAR_XSD_PATH_*) by its file name and AUTOSAR version, never its location; the fast gate "
        "runs without the license-gated schema, so only test_validate_build.py's patched outcomes hold the "
        "spelling (where the schema IS present, build/success and validate/success reach the line and are clean)"
    ),
    ("validate", "licensed-xsd"): "the same license-gated schema as build's",
    ("build", "generated-arxml-fails-its-check"): (
        "a model the validator accepts produces ARXML the structural check accepts; no model reaches the refusal "
        "branch of _check_generated_arxml without replacing the writer. Its headline, its forwarded diagnostics "
        "and each error's path name the ARXML relative to the project and the schema by its file name, held by "
        "test_validate_build.py's patched outcomes rather than by a probe"
    ),
    ("validate", "generated-arxml-fails-its-check"): "the same writer/check agreement as build's",
    ("change_set", "apply-fails-midway"): (
        "an OSError between two os.replace() calls needs a filesystem that fails one write of several; it "
        "propagates and is masked like intent-record-write-fails, which is reached"
    ),
    ("model_diff", "git-not-installed"): "depends on the host's PATH; the fast gate runs where git is installed",
    ("change_set", "panel-review-cancelled"): (
        "a call the client cancels gets no reply at all: mcp answers the cancelled request with its own error "
        "response, and the panel's withdrawal note never reaches the client. The note is a constant, scanned by "
        "test_the_panel_cancellation_note_names_no_location, and the withdrawal is held by test_review_mode.py's "
        "TestACancelledCallWithdrawsItsPanelReview"
    ),
}

#: Channels found carrying a location that are not closed yet, by probe id with the message. Empty,
#: and kept as a ratchet: a listed probe that stops carrying fails until its row is removed, and an
#: unlisted probe that starts carrying fails as every other probe does.
CARRIERS_AWAITING_A_RULING: dict[str, str] = {}

#: The one reply two pinned ``model_diff`` probes share, whichever way ``rev-parse`` failed.
NO_REPOSITORY_REPLY = (
    "ERROR: not a git repository: the model folder is in no git repository this process can read, so it has no "
    "revisions to compare"
)

#: What each pinned reply answers: fragments of it, each of which must appear. The probes' manifest
#: declares ``dist_root: dist``, ``views_root: views`` and the default outputs under them. Each
#: pinned reply is reached, names no location, and says this.
WHAT_EACH_REPLY_SAYS: dict[str, tuple[str, ...]] = {
    "build/success": (
        "ARXML generated: dist/model.arxml (AUTOSAR 4.2.2)\n",
        "\nDiagrams generated: views (5 views, report views/report.txt)\nneeds.json generated: dist/needs.json",
    ),
    "validate/success": ("ARXML generated: dist/model.arxml (AUTOSAR 4.2.2)\n",),
    "export_component/success": ("\nOUTPUT dist/components/ExampleSWC.arxml\n",),
    "export_component/success-every-component": (
        "\nOUTPUT dist/components/ExampleSWC.arxml\n",
        "\nSummary: dist/components/export-summary.json\n",
    ),
    "resource_report/success": ('"response":"resources"',),
    "resource_report/model-root-missing": ('"response":"resources"', "hardware.layer-absent"),
    "build/manifest-not-found": (MISSING_ABSOLUTE_MANIFEST,),
    "validate/manifest-not-found": (MISSING_ABSOLUTE_MANIFEST,),
    "export_component/manifest-not-found": (MISSING_ABSOLUTE_MANIFEST,),
    "export_component/manifest-not-found-every-component": (MISSING_ABSOLUTE_MANIFEST,),
    "import_component/manifest-not-found": (MISSING_ABSOLUTE_MANIFEST,),
    "build/model-root-missing": ("Model root not found: the model root does not exist",),
    "validate/model-root-missing": ("Model root not found: the model root does not exist",),
    "export_component/model-root-missing": ("Model root not found: the model root does not exist",),
    "export_component/model-root-missing-every-component": ("Model root not found: the model root does not exist",),
    "import_component/model-root-missing": ("Model root not found: the model root does not exist",),
    "impact_of/model-root-missing": ("ERROR: Model root not found: the model root does not exist",),
    # The errno and its text differ by host (2 on POSIX, 20 on Windows); the pin is the path-free shape.
    "model_diff/model-root-missing": ("ERROR: model root not found: [Errno ",),
    "model_diff/revision-without-the-model-root": (
        "ERROR: revision 'HEAD' does not contain the model root: Model root not found: the model root does not exist",
    ),
    # git's and tarfile's text stays on the CLI's terminal: each of the five is the reader's own sentence.
    "model_diff/not-a-git-repository": (NO_REPOSITORY_REPLY,),
    "model_diff/not-a-git-repository-naming-a-location": (NO_REPOSITORY_REPLY,),
    "model_diff/unknown-revision": (
        "ERROR: git archive failed for revision 'no-such-revision': git could not read this revision, which may be "
        "an unknown name or a damaged repository",
    ),
    "model_diff/git-archive-naming-a-location": (
        "ERROR: git archive failed for revision 'HEAD': git could not read this revision, which may be an unknown "
        "name or a damaged repository",
    ),
    "model_diff/extraction-naming-a-location": (
        "ERROR: failed to extract archive for revision 'HEAD': the revision's files could not be unpacked safely",
    ),
    # The metrics read a revision through the same reader: its sentences, and no location.
    "architecture_metrics/not-a-git-repository": (NO_REPOSITORY_REPLY,),
    "architecture_metrics/unknown-revision": (
        "ERROR: git archive failed for revision 'no-such-revision': git could not read this revision, which may be "
        "an unknown name or a damaged repository",
    ),
    "export_component/unknown-component": ("SWC 'NoSuchSWC' not found in the model",),
    # A fixed sentence each, naming the arguments and never the path given.
    "export_component/path-argument-of-the-other-form": (
        "output_dir is the directory of an export of every component; "
        "with swc_name, give output_path, the file, or neither.",
    ),
    "export_component/path-argument-of-the-other-form-every-component": (
        "output_path is the file of one component's export; without swc_name, give output_dir, the directory, "
        "or neither.",
    ),
    "export_component/element-file-does-not-parse-every-component": (
        "swc/example/ExampleConsumerSWC.yaml: Invalid SWC YAML in swc/example/ExampleConsumerSWC.yaml: ",
    ),
    "import_component/element-file-does-not-parse": ("Invalid SWC YAML in swc/example/ExampleConsumerSWC.yaml: ",),
    # The name inside the model root, and no more.
    "impact_of/element-file-does-not-parse": ("ERROR: Invalid SWC YAML in swc/example/ExampleConsumerSWC.yaml: ",),
    "resource_report/element-file-does-not-parse": (
        "ERROR: Invalid SWC YAML in swc/example/ExampleConsumerSWC.yaml: ",
    ),
    "import_component/target-declares-a-name-in-two-files": (
        "component import aborted: the target project is ambiguous -- interface name 'IDiagnosticService' is "
        "declared by 2 files: interfaces/IDiagnosticService.yaml, interfaces/IDiagnosticService~1.yaml",
    ),
    "export_component/closure-refused-by-the-writer": (
        "Base type(s) ['float32_base'] in data_types/base/ declare no ",
    ),
    "export_component/closure-refused-by-the-writer-every-component": (
        "ExampleSWC: Base type(s) ['float32_base'] in data_types/base/ declare no ",
        "\nERROR Base type(s) ['float32_base'] in data_types/base/ declare no ",
    ),
    "export_component/component-declared-in-two-files": (
        "SWC name 'ExampleSWC' is declared by 2 YAML files in the model",
    ),
    "build/manifest-root-outside-the-project": ("Manifest field 'dist_root' resolves outside the project root.",),
    "import_component/manifest-root-overlaps-the-model": (
        "Manifest field 'dist_root' overlaps 'model_root'. Generated paths must be outside the model",
    ),
    # The writer and the generators name a layer directory; an `OSError` is its errno and reason
    # with the file named inside the model or the project.
    "build/writer-refuses-a-model-the-validator-accepts": (
        "No composition in compositions/ declares 'root: true'; exactly one must",
    ),
    "validate/writer-refuses-a-model-the-validator-accepts": (
        "Value type 'float32' base type 'NoSuchBase' is not declared in data_types/base/.",
    ),
    "build/diagrams-refuse-a-model-the-writer-accepts": (
        "no diagram was written: the view 'ibd-Root Frame', 'overview-Root Frame' cannot be a file name",
    ),
    # The layout's own sentence names the interpreter's directory and the Node it found; the
    # reply keeps the sentence and names neither.
    "build/diagrams-without-node": ("Node.js was not found beside the SWCraft installation's interpreter",),
    # Node's own stack names the installation's files; the reply keeps the exit status and a
    # sentence of the bridge's.
    "build/diagrams-layout-engine-crashes": (
        "the diagram layout failed (exit 1): the layout engine stopped without a message of its own",
    ),
    # The errno and the file, not the reason: the operating system words the reason.
    "build/dist-is-a-file": ("[Errno 17] ", " exists: dist"),
    "validate/dist-is-a-file": ("[Errno 17] ", " exists: dist"),
    # POSIX fails at the temporary file, Windows at the directory; both name the storage name.
    "export_component/dist-is-a-file": ("[Errno ", ": dist"),
    "build/model-file-unreadable": ("[Errno 13] Permission denied: swc/example/ExampleConsumerSWC.yaml",),
    # The batch's own per-component catch, which no wrapper sees.
    "export_component/model-file-unreadable": ("[Errno 13] Permission denied: swc/example/ExampleConsumerSWC.yaml",),
    "export_component/model-file-unreadable-every-component": (
        "swc/example/ExampleConsumerSWC.yaml: [Errno 13] Permission denied: swc/example/ExampleConsumerSWC.yaml",
        "\nERROR [Errno 13] Permission denied: swc/example/ExampleConsumerSWC.yaml",
    ),
    "import_component/source-written-by-an-earlier-release": (
        "this component file was written by an earlier SWCraft release (it carries the `float32_impl` "
        "implementation-type twin that release invented and no data-types marker); re-export it with this release",
    ),
    # A stub's `external_ref` is an AUTOSAR reference path quoted on purpose; each row pins a reply
    # carrying one that the gate passes.
    "read/an-external-interface-stub": ('"external_ref":"/Foreign/PortInterfaces/IForeignSR"',),
    "change_set/external-stub-refused-by-pass-d": (
        f"declares external_ref '{MALFORMED_STUB_REF}', which is not an AUTOSAR reference path",
    ),
    "build/success-over-external-stubs": ("ARXML generated: dist/model.arxml (AUTOSAR 4.2.2)\n",),
    "build/model-refused-over-an-external-stub": (
        f"declares external_ref '{MALFORMED_STUB_REF}', which is not an AUTOSAR reference path",
    ),
    "validate/model-refused-over-an-external-stub": (
        f"declares external_ref '{MALFORMED_STUB_REF}', which is not an AUTOSAR reference path",
    ),
    "build/writer-refuses-an-external-stub-the-validator-accepts": (
        "Invalid external stub declaration(s) in interfaces/: external interface stub 'IForeignBad' declares "
        f"external_ref '{MALFORMED_STUB_REF}'",
    ),
    "export_component/closure-reaches-an-external-stub": (
        "SWC 'ExampleSWC' reaches external interface 'IForeignCS' ('/Foreign/PortInterfaces/IForeignCS')",
        "external type 'ForeignT' ('/Foreign/ImplementationDataTypes/ForeignT')",
        "Nothing was written.",
    ),
    "export_component/closure-reaches-an-external-stub-every-component": (
        "ExampleSWC: SWC 'ExampleSWC' reaches external interface 'IForeignCS' ('/Foreign/PortInterfaces/IForeignCS')",
        "\nSTATUS failed\n",
    ),
    # Ports on a component stub: the same rule.
    "read/a-component-stub-with-ports": (
        f'"external_ref":"{COMPONENT_STUB_REF}"',
        '"observed_interface":"IExample"',
    ),
    "change_set/component-stub-port-interface-refused-by-pass-d": (STUB_PORT_REFUSAL,),
    "build/success-over-component-stub-ports": ("ARXML generated: dist/model.arxml (AUTOSAR 4.2.2)\n",),
    "validate/success-over-component-stub-ports": ("ARXML generated: dist/model.arxml (AUTOSAR 4.2.2)\n",),
    "validate/model-refused-over-a-component-stub-port": (STUB_PORT_REFUSAL,),
    "export_component/a-component-stub": (f"SWC '{COMPONENT_STUB}' is an external stub (external: true)",),
    "impact_of/an-implementation-array-the-schema-refuses": (
        # One line per field, the union's tag dropped.
        "ERROR: Invalid implementation type YAML in data_types/implementation/Spare_T.yaml: "
        "length: Value error, length must be > 0",
    ),
    "change_set/pointer-mapped-refused-by-pass-d": (
        "Mapping set 'ProbeMaps' maps 'WheelSpeeds' onto 'WheelSpeeds_Ptr', which reaches the pointer",
        "[constr_1066]",
    ),
    "validate/model-refused-over-a-pointer-cycle": ("Cycle detected: Ring_Ptr -> Ring_Rec -> Ring_T -> Ring_Ptr",),
    "build/writer-refuses-a-pointer-cycle-the-validator-accepts": ("Implementation data type dependency cycle: ",),
    "export_component/closure-reaches-the-implementation-composites": (
        "\nOUTPUT dist/components/ExampleSWC.arxml\n",
        "\n  - WheelSpeedHistory_T\n",
        "\n  - WheelSpeeds_PtrPtr\n",
    ),
}

#: The twenty-two carriers the gate first found and that were closed.
CLOSED_BY_THE_RULING: frozenset[str] = frozenset(
    {
        "build/success",
        "validate/success",
        "export_component/success",
        "export_component/success-every-component",
        "resource_report/success",
        "resource_report/model-root-missing",
        "build/manifest-not-found",
        "validate/manifest-not-found",
        "export_component/manifest-not-found",
        "export_component/manifest-not-found-every-component",
        "import_component/manifest-not-found",
        "build/model-root-missing",
        "validate/model-root-missing",
        "export_component/model-root-missing",
        "export_component/model-root-missing-every-component",
        "import_component/model-root-missing",
        "impact_of/model-root-missing",
        "model_diff/model-root-missing",
        "model_diff/revision-without-the-model-root",
        "export_component/unknown-component",
        "export_component/element-file-does-not-parse-every-component",
        "import_component/element-file-does-not-parse",
    }
)


# The gate.


def run_probe(probe: Probe, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Reply, list[str]]:
    """Build the project, put it into the probe's mode, call the tool, and scan the reply."""
    probe_dir = tmp_path / PROBE_DIRNAME
    project = use_project(PLAIN, probe_dir, monkeypatch)
    set_mode(monkeypatch, None)
    setup = probe.prepare(project, monkeypatch)
    # `TemporaryDirectory` creates its directory through `tempfile.mkdtemp`, so one wrapper sees both.
    temporary: list[str] = []
    make = tempfile.mkdtemp

    def recording_mkdtemp(*args: Any, **kwargs: Any) -> str:
        made = make(*args, **kwargs)
        temporary.append(Path(made).name)
        return made

    monkeypatch.setattr(tempfile, "mkdtemp", recording_mkdtemp)
    try:
        with setup.holding:
            reply = call_the_tool(probe.tool, setup.arguments, setup.human)
    finally:
        monkeypatch.setattr(tempfile, "mkdtemp", make)
    roots = filesystem_roots(probe_dir)
    carriers = [
        f"{probe.id}: {location!r} in {text[:400]!r}"
        for text in reply.strings()
        for location in locations_in(text, roots, setup.echoed, tuple(temporary))
    ]
    return reply, carriers


def _every_guide_request(corpus: dict[str, Any]) -> list[dict[str, Any]]:
    """A ``section`` request for every part of every id, and a ``toc`` request for every part of each table."""
    requests: list[dict[str, Any]] = [
        {"section": record["id"], "part": part}
        for record in corpus["sections"]
        for part in range(1, len(record["parts"]) + 1)
    ]
    tables: list[str | None] = [None, *(page["page"] for page in corpus["pages"])]
    index = guide_search.GuideIndex.of(corpus)
    for page in tables:
        for part in range(1, len(guide_search.toc_parts(index, page)) + 1):
            requests.append({"toc": True, "part": part} | ({"page": page} if page else {}))
    return requests


def _guide_replies(requests: list[dict[str, Any]]) -> list[Reply]:
    """Each request's ``guide`` reply, through one live in-memory client session."""

    async def go() -> list[Any]:
        async with Client(server.mcp) as client:
            return [await client.call_tool("guide", request, raise_on_error=False) for request in requests]

    return [reply_of(result) for result in asyncio.run(go())]


@pytest.fixture(scope="module")
def real_guide_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The real guide's corpus, built in-process with the selected release notes, in a site of its own."""
    return write_site(tmp_path_factory.mktemp("real-guide-site"), real_corpus())


class TestNoToolReplyCarriesAFilesystemPath:
    """Every tool's reply, under every failure mode it can be made to report — see the module docstring."""

    @pytest.mark.parametrize("probe", PROBES, ids=[probe.id for probe in PROBES])
    def test_no_reply_names_a_filesystem_location(
        self, probe: Probe, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reply, carriers = run_probe(probe, tmp_path, monkeypatch)
        assert probe.reached(reply), f"{probe.id} was not reached, so nothing was measured: {reply}"
        if probe.id in CARRIERS_AWAITING_A_RULING:
            assert carriers, f"{probe.id} no longer names a location: remove its CARRIERS_AWAITING_A_RULING row"
        else:
            assert carriers == []

    @pytest.mark.parametrize(
        "probe",
        [probe for probe in PROBES if probe.mode == "manifest-invalid"],
        ids=[probe.id for probe in PROBES if probe.mode == "manifest-invalid"],
    )
    def test_a_broken_manifest_quotes_none_of_its_values(
        self, probe: Probe, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The value pydantic quoted (``input_value=[...]``), in every spelling, reaches no reply.

        The location scan cannot flag another machine's POSIX path, so the written values are looked
        for by their own text.
        """
        reply, _carriers = run_probe(probe, tmp_path, monkeypatch)
        assert probe.reached(reply), reply
        everything = "\n".join(reply.strings())
        for value in (*FOREIGN_MANIFEST_LOCATIONS, "secret", "input_value", "validation error"):
            assert value not in everything, f"{probe.id}: {value!r} in {everything!r}"

    def test_every_registered_tool_is_probed_and_what_is_not_reached_is_listed(self) -> None:
        registered = tool_names()
        probed = {probe.tool for probe in PROBES}
        assert probed == registered, sorted(probed ^ registered)
        assert {probe.tool for probe in PROBES if probe.mode == "success"} == registered
        ids = [probe.id for probe in PROBES]
        assert len(ids) == len(set(ids)), "two probes share one tool and mode"
        unreached_tools = {tool for tool, _mode in UNREACHED_BY_THE_REPLY_GATE}
        assert unreached_tools <= registered
        assert not {(probe.tool, probe.mode) for probe in PROBES} & set(UNREACHED_BY_THE_REPLY_GATE)
        assert set(CARRIERS_AWAITING_A_RULING) <= set(ids), sorted(set(CARRIERS_AWAITING_A_RULING) - set(ids))

    def test_the_gate_fails_on_a_reply_that_names_the_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A path planted into ONE clean tool's reply, and the gate naming that tool and mode."""
        planted: list[str] = []

        def leaking(model_root: str, rev: str | None = None) -> Any:
            planted.append(model_root)
            raise ValueError(f"the model at {model_root} could not be read")

        monkeypatch.setattr(server.snapshot, "load_snapshot", leaking)
        (probe,) = [probe for probe in PROBES if probe.id == "impact_of/success"]
        assert probe.id not in CARRIERS_AWAITING_A_RULING, "the bite must be planted into a reply that is clean"
        reply, carriers = run_probe(probe, tmp_path, monkeypatch)

        assert planted, "the planted message was never rendered"
        assert reply.text.startswith("ERROR: the model at ")
        assert carriers, reply
        assert all(carrier.startswith("impact_of/success: ") for carrier in carriers), carriers

    def test_the_ratchet_fails_a_clean_probe_that_starts_carrying(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unlisted probe that starts naming a location fails the gate itself, naming tool and mode.

        The reply stays a success, so the location assertion fails and not the reach check.
        """
        original = server.impact.format_impact_summary

        def leaking(name: str, result: Any) -> str:
            return f"{original(name, result)}\nread from {server._get_project().resolved_model_root}"

        monkeypatch.setattr(server.impact, "format_impact_summary", leaking)
        (probe,) = [probe for probe in PROBES if probe.id == "impact_of/success"]
        assert probe.id not in CARRIERS_AWAITING_A_RULING, "the bite must be planted into a reply that is clean"

        with pytest.raises(AssertionError, match="impact_of/success: ") as failed:
            self.test_no_reply_names_a_filesystem_location(probe, tmp_path, monkeypatch)
        assert "was not reached" not in str(failed.value)

    def test_the_ruling_closed_every_carrier_it_listed(self) -> None:
        """All twenty-two carriers first found are closed and pinned, none left waiting."""
        assert CARRIERS_AWAITING_A_RULING == {}
        assert len(CLOSED_BY_THE_RULING) == 22
        assert CLOSED_BY_THE_RULING <= set(WHAT_EACH_REPLY_SAYS) <= {probe.id for probe in PROBES}

    @pytest.mark.parametrize("probe_id", sorted(WHAT_EACH_REPLY_SAYS))
    def test_a_pinned_reply_says_what_its_ruling_made_it_say(
        self, probe_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reached, naming no location, and carrying every fragment its row pins."""
        (probe,) = [probe for probe in PROBES if probe.id == probe_id]
        reply, carriers = run_probe(probe, tmp_path, monkeypatch)

        assert probe.reached(reply) and carriers == [], (reply, carriers)
        everything = "\n".join(reply.strings())
        for fragment in WHAT_EACH_REPLY_SAYS[probe_id]:
            assert fragment.format(name=fixtures.SEEDS[PLAIN].project_name) in everything, everything

    def test_no_guide_reply_over_the_real_guide_names_a_location(
        self, real_guide_site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every part of every id of the real guide, and every part of its tables of contents, scanned.

        No request supplies the guide's text, so nothing is removed from it before the scan.
        """
        install_site(monkeypatch, real_guide_site)
        corpus = json.loads((real_guide_site / guide_search.CORPUS_FILENAME).read_text(encoding="utf-8"))
        requests = _every_guide_request(corpus)
        replies = _guide_replies(requests)
        assert len(replies) == len(requests) > len(corpus["sections"])
        roots = filesystem_roots(tmp_path / PROBE_DIRNAME)
        carriers = [
            f"guide {request}: {location!r} in {text[:400]!r}"
            for request, reply in zip(requests, replies, strict=True)
            for text in reply.strings()
            for location in locations_in(text, roots)
        ]
        assert carriers == []

    @pytest.mark.parametrize("planted", ["home-spelled", "the-probe-root"])
    def test_a_location_planted_into_a_guide_part_is_found(
        self, planted: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guide probes bite: a ``~/`` spelling, or the probe's own root, written into a part is flagged."""
        probe_dir = tmp_path / PROBE_DIRNAME
        probe_dir.mkdir()
        location = "~/.swcraft/current/bin/swcraft" if planted == "home-spelled" else str(probe_dir / "model")
        corpus = fixture_corpus()
        (record,) = [record for record in corpus["sections"] if record["id"] == GUIDE_SECTION]
        record["parts"] = [f"Run it from {location} and read on."]
        install_corpus(monkeypatch, tmp_path / "installed-guide", corpus)

        (reply,) = _guide_replies([{"section": GUIDE_SECTION}])
        assert reply.structured["text"] == record["parts"][0]
        carriers = [found for text in reply.strings() for found in locations_in(text, filesystem_roots(probe_dir))]
        assert carriers, reply

    def test_a_masked_reply_counts_as_reached_only_with_the_exception_it_names(self) -> None:
        """Every unexpected exception is masked into one reply, so the server log must name the fault."""
        masked = Reply(is_error=True, structured=None, texts=("Error calling tool 'read'",))
        raised = Reply(is_error=True, structured=None, texts=masked.texts, logged=(ModelRootNotFoundError("/x"),))

        assert _refused(masked) is False
        assert _refused_or_raised(ModelRootNotFoundError)(masked) is False
        assert _refused_or_raised(ManifestPathError)(raised) is False
        assert _refused_or_raised(ModelRootNotFoundError)(raised) is True

    def test_a_path_in_a_mapping_body_is_found_in_the_decoded_body_the_escaped_text_hides(self) -> None:
        """The scan's own terms: a dict tool's body carries a path the text block escapes.

        In the block's JSON text a backslash doubles and a quote gains one, so only the decoded body
        holds the root as written.
        """
        location = 'C:\\Users\\a"b\\project'
        body = {"success": False, "output": f"written under {location}"}
        block = SimpleNamespace(type="text", text=compact_json(body))
        reply = reply_of(SimpleNamespace(is_error=False, structured_content=None, meta=None, content=[block]))
        roots = (location,)

        assert reply.structured == body
        assert location not in block.text
        assert location not in locations_in(block.text, roots)
        assert location in [found for text in reply.strings() for found in locations_in(text, roots)]

    def test_an_autosar_reference_is_not_a_filesystem_location(self, tmp_path: Path) -> None:
        roots = filesystem_roots(tmp_path)
        assert locations_in("ERROR: /Pkg/Types/uint8 is not declared", roots) == []
        assert locations_in("export SWCRAFT_MANIFEST=/path/to/my-project/swcraft.yaml", roots) == []
        assert locations_in(f"written to {tmp_path / 'x.arxml'}", roots) != []
        assert locations_in("C:\\Users\\someone\\project", roots) != []
        assert locations_in("interfaces/IDiagnosticService~1.yaml", roots) == []
        # An external stub's reference path, well formed or quoted back as malformed, is no location either.
        assert locations_in("external_ref '/Foreign/PortInterfaces/IForeignSR'", roots) == []
        assert locations_in(f"declares external_ref '{MALFORMED_STUB_REF}'", roots) == []

    def test_each_mark_is_flagged_with_no_root_to_match(self) -> None:
        """The detector's branches one by one, with no root given, so each is the only one that can fire."""
        assert locations_in("staged in swcraft-session-k2j3ab/model/swc/", ()) == ["swcraft-session-"]
        assert locations_in("in swcraft-component-export-k2j3/data_types/", ()) == ["swcraft-component-export-"]
        assert locations_in(f"read from {PROBE_DIRNAME}/project/model", ()) == [PROBE_DIRNAME]
        assert locations_in("extracted to tmpk2j3ab/model/swc/example/X.yaml", (), temporary=("tmpk2j3ab",)) == [
            "tmpk2j3ab"
        ]
        assert locations_in("see ~/Projects/model/swc/", ()) == ["~/"]

    def test_a_posix_spelled_path_is_flagged_only_when_it_exists(self) -> None:
        """Outside every root, an existing file is flagged by existence alone, and a missing one is not."""
        here = Path(__file__).resolve()
        spelled = "/" + "/".join(here.parts[1:])
        assert locations_in(f"read {spelled} first", ()) == [spelled]
        assert locations_in(f"read {spelled}.absent first", ()) == []

    def test_no_progress_message_of_a_panel_wait_names_a_location(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A panel wait's ``notifications/progress`` texts, collected by the client and scanned.

        They are no reply, so no probe reaches them.
        """
        probe_dir = tmp_path / PROBE_DIRNAME
        project = use_project(WEAKENING, probe_dir, monkeypatch)
        set_mode(monkeypatch, server.REVIEW_PANEL)
        ScriptedTime(monkeypatch, answer_after=150.0)
        messages: list[str] = []

        async def handler(_progress: float, _total: float | None, message: str | None) -> None:
            messages.append(message or "")

        async def exercise() -> Any:
            async with Client(server.mcp) as client:
                return await client.call_tool("change_set", arguments(WEAKENING, project), progress_handler=handler)

        result = asyncio.run(asyncio.wait_for(exercise(), timeout=60))

        assert body_of(result)["status"] == "committed"
        assert len(messages) == 4, messages
        roots = filesystem_roots(probe_dir)
        home = Path(swcraft_home(os.environ, os.name, Path.home()))
        # On Windows the home's last part is `SWCraft`, which the message says as the review tab's
        # name; there the home is checked only as a whole path.
        names = {project.name} | ({home.name} if home.name.casefold() != "swcraft" else set())
        for message in messages:
            assert locations_in(message, roots) == [], message
            assert not [name for name in names if name in message], message
            assert str(home) not in message and home.as_posix() not in message, message
            assert "/" not in message and "\\" not in message, message

    def test_the_panel_cancellation_note_names_no_location(self, tmp_path: Path) -> None:
        """The one panel note no reply carries (``UNREACHED_BY_THE_REPLY_GATE``), scanned as the text it is."""
        assert locations_in(panel_review.CANCELLED_NOTE, filesystem_roots(tmp_path)) == []
