"""SwcraftAgent: system prompt + ConversationManager + LLMClient + MCP dispatch.

``swcraft-agent`` owns its MCP subprocess, so :meth:`SwcraftAgent.chat` is a chokepoint the LLM cannot bypass,
and every approval is a human interaction it cannot reach. The editing engine inside the server owns the
transaction (the change set); this host stages, diffs and commits nothing.

* Every server this host spawns carries ``SWCRAFT_REVIEW``, so a change set commits only after a human approves
  the staged delta: ``panel`` for an interactive run (the SWCraft review tab in VS Code), ``elicit`` for a
  ``--script`` replay (the scenario file's ``approvals``).
* A scoped run spawns the server per turn against the real manifest, adding ``SWCRAFT_APPROVED_SCOPE`` (the
  approved :class:`~swcraft_agent.scope_proposal.ScopeProposal`'s entries, as JSON) and ``SWCRAFT_SCOPE_ID``
  (the turn's correlation id), because a scope approved mid-conversation cannot reach the environment of a
  process spawned before it. A declined proposal is ``SWCRAFT_APPROVED_SCOPE=[]``, which the engine refuses per
  operation. An unscoped run keeps one long-lived client.
* :class:`_TurnGuard` contains the retained tools that can escape a scope: an explicit export path, and a
  ``build``/``validate`` whose server would shell out through ``SWCRAFT_MCP_*_COMMAND``. Both write outside the
  model, where neither the engine's gate nor the delta review can see them.

The per-turn client is closed in a ``finally``, so any exception, transport failure or ``KeyboardInterrupt`` at a
prompt leaves no orphaned subprocess.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Mapping
from importlib.resources import files as resource_files
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import Any

from swcraft_tools.auditor import OUTSIDE_THE_PROJECT, AuditLog, project_location
from swcraft_tools.authorization import REFUSAL_MARKER, Refusal
from swcraft_tools.project import SwcraftProject, is_absolute_anywhere, require_project
from swcraft_tools.session import ESCAPE_ENV_VARS, pinned_escape_env

from swcraft_agent.approval import Approver, StdinApprover
from swcraft_agent.conversation import ConversationManager
from swcraft_agent.llm_client import MAX_TOOL_ROUNDS, AzureOpenAIClient, LLMClient
from swcraft_agent.mcp_client import McpClientError, SwcraftMcpClient, is_json
from swcraft_agent.scope_proposal import ScopeProposal, propose_scope

MAX_BUILD_RETRIES_ENV = "SWCRAFT_AGENT_MAX_BUILD_RETRIES"
DEFAULT_MAX_BUILD_RETRIES = 3
# Upper clamp keeps the build-retry give-up reachable inside the hard
# MAX_TOOL_ROUNDS backstop. Worst case per turn: 1 initial build round
# + N retries (each ~1 fix round + 1 build round) + 1 final text round,
# so 2 + 2N <= MAX_TOOL_ROUNDS  =>  N <= (MAX_TOOL_ROUNDS - 2) // 2.
MAX_BUILD_RETRIES_CAP = (MAX_TOOL_ROUNDS - 2) // 2


class SystemPromptNotFoundError(FileNotFoundError):
    """Raised when the agent system prompt file cannot be located."""


class UnpinnedEscapeEnvError(ValueError):
    """A scoped agent was handed a client whose server environment is not pinned."""


def _max_build_retries() -> int:
    """Return the build self-correction retry cap from ``SWCRAFT_AGENT_MAX_BUILD_RETRIES``.

    An empty, non-numeric or negative value falls back to ``DEFAULT_MAX_BUILD_RETRIES``; zero means no retries;
    values above ``MAX_BUILD_RETRIES_CAP`` are clamped.
    """
    raw = os.environ.get(MAX_BUILD_RETRIES_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_BUILD_RETRIES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_BUILD_RETRIES
    if value < 0:
        return DEFAULT_MAX_BUILD_RETRIES
    return min(value, MAX_BUILD_RETRIES_CAP)


# The review mode, the scope and its correlation id travel in the spawned process's environment: a channel the
# host controls at `execve` and the LLM cannot reach.

#: Names the MCP server reads once at startup. Repeated rather than imported: `swcraft-agent` does not depend
#: on `swcraft_mcp` (it spawns `-m swcraft_mcp.server`); a test pins them against the server's own.
REVIEW_ENV = "SWCRAFT_REVIEW"
APPROVED_SCOPE_ENV = "SWCRAFT_APPROVED_SCOPE"
SCOPE_ID_ENV = "SWCRAFT_SCOPE_ID"

#: The two review modes this host spawns with, never `stage-only`: there is a human behind every run, so the
#: server's `stage-only` default would leave every change set staged and never applied. `panel` (the default,
#: an interactive run) is answered in the SWCraft review tab of a VS Code window open on the project, and waits
#: until the run ends if none is; `elicit` (a `--script` replay) is answered by the scenario file's `approvals`
#: through this client's elicitation callback.
REVIEW_PANEL = "panel"
REVIEW_ELICIT = "elicit"
REVIEW_MODES: tuple[str, ...] = (REVIEW_PANEL, REVIEW_ELICIT)


def _require_review_mode(review: str) -> None:
    """Refuse a ``SWCRAFT_REVIEW`` value outside :data:`REVIEW_MODES` before anything is spawned."""
    if review not in REVIEW_MODES:
        raise ValueError(
            f"{REVIEW_ENV} must be one of {', '.join(REVIEW_MODES)} for a server this host spawns, got {review!r}"
        )


def review_server_env(base_env: Mapping[str, str] | None = None, *, review: str = REVIEW_PANEL) -> dict[str, str]:
    """The environment every server this host spawns runs with, unscoped.

    *review* is the ``SWCRAFT_REVIEW`` value; one outside :data:`REVIEW_MODES` raises ``ValueError``.

    The escape variables are pinned present (:func:`~swcraft_tools.session.pinned_escape_env`) and both scope
    variables present-and-empty, because the child's ``load_dotenv(override=False)`` fills exactly the absent
    keys: a project ``.env`` could otherwise hand the server a build override the host never saw, or a scope
    that refuses every operation. Overwriting the scope variables also clears a stale scope inherited from this
    process; the server reads empty as unset.
    """
    _require_review_mode(review)
    env = pinned_escape_env(base_env)
    env[REVIEW_ENV] = review
    env[APPROVED_SCOPE_ENV] = ""
    env[SCOPE_ID_ENV] = ""
    return env


def scoped_server_env(
    entries: list[dict[str, Any]],
    scope_id: str,
    base_env: Mapping[str, str] | None = None,
    *,
    review: str = REVIEW_PANEL,
) -> dict[str, str]:
    """:func:`review_server_env` plus the approved scope and its correlation id.

    *entries* is the approved :class:`~swcraft_agent.scope_proposal.ScopeProposal`'s ``to_json()``; ``[]`` (a
    declined proposal, or one approving no change) means nothing is approved, unlike an absent variable, which is
    an unscoped run.

    Raises:
        ValueError: *scope_id* is empty; the server would refuse to start on a scope without it.
    """
    if not scope_id:
        raise ValueError(
            f"a scoped turn needs a correlation id: {APPROVED_SCOPE_ENV} without {SCOPE_ID_ENV} is a scoped run "
            "whose lifecycle records cannot be tied back to the turn that authorized it, and the server refuses "
            "to start on it."
        )
    env = review_server_env(base_env, review=review)
    env[APPROVED_SCOPE_ENV] = json.dumps(entries)
    env[SCOPE_ID_ENV] = scope_id
    return env


#: A package data file beside this module, read in a checkout and an installation alike.
SYSTEM_PROMPT_FILENAME = "agent-system-prompt.md"


def _packaged_system_prompt_path() -> Path | None:
    """The prompt carried inside the package (via ``importlib.resources``), or ``None`` without one.

    Not reached: a wheel built with the file deleted or VCS-ignored carries none; its installation needs
    ``SWCRAFT_AGENT_SYSTEM_PROMPT``.
    """
    try:
        candidate = resource_files("swcraft_agent") / SYSTEM_PROMPT_FILENAME
        if candidate.is_file():
            return Path(str(candidate))
    except (ModuleNotFoundError, OSError, TypeError):  # pragma: no cover - packaging dependent
        return None
    return None


#: The tools whose caller-supplied output path writes to any path at all, each with its path arguments in
#: signature order. While a scope is in force the tool is allowed only without them, so it writes to the
#: project's own ``dist_root``: a scope is a list of model addresses, and an export writes outside the model. A
#: declined or read-only turn is gated too. With no scope in force any path is accepted.
EXPORT_PATH_ARGS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "export_component": ("output_path", "output_dir"),
    }
)

#: The tools whose behavior an ``SWCRAFT_MCP_*_COMMAND`` override replaces with a shell command against the
#: project the server's environment names; refused while a scope is in force.
#:
#: Keyed on the scope, while :func:`~swcraft_tools.session.pinned_escape_env` applies to every server: pinning
#: ensures the only override in force is one the launching human really has (a project ``.env`` cannot smuggle
#: one in). This refusal decides whether running it is inside the turn's authority: under a scope it is not (a
#: shell command against the whole project is no model address; a declined turn could otherwise still run a
#: build that deletes the real ``dist/``). With no scope in force, ``build``/``validate`` exist to run it, and
#: refusing would only disable the build self-correction path.
#:
#: Values are :data:`~swcraft_tools.session.ESCAPE_ENV_VARS`, pinned by a test: a new override variable needs
#: a row here.
ESCAPE_COMMAND_TOOLS: Mapping[str, str] = MappingProxyType(
    {
        "build": "SWCRAFT_MCP_BUILD_COMMAND",
        "validate": "SWCRAFT_MCP_VALIDATE_COMMAND",
    }
)


def _require_pinned_escape_env(mcp: Any) -> None:
    """Refuse ``scoped=True`` over a client whose escape variables are not pinned.

    :data:`ESCAPE_COMMAND_TOOLS` reads the environment the server was spawned with, which is true only while
    every :data:`~swcraft_tools.session.ESCAPE_ENV_VARS` name is present in it (the child's
    ``load_dotenv(override=False)`` fills absent keys from the project ``.env``). A client the caller spawned
    cannot be re-spawned, so it is refused here, where the caller can repair it with
    ``env=pinned_escape_env()``, rather than degraded silently at call time. A client without ``server_env``
    (a test double) declares no subprocess environment and is left alone.

    Raises:
        UnpinnedEscapeEnvError: an escape variable is missing from the client's ``server_env``.
    """
    server_env = getattr(mcp, "server_env", None)
    if server_env is None:
        return
    missing = [name for name in ESCAPE_ENV_VARS if name not in server_env]
    if not missing:
        return
    raise UnpinnedEscapeEnvError(
        "A scoped agent cannot use an MCP client whose server environment does not pin "
        f"{', '.join(missing)}. The server is already running, so this host cannot fix its "
        "environment, and a server spawned without those variables present can still acquire "
        "them from the project's own .env — which would make the host's build/validate gate "
        "answer 'no override' for a server that has one. Spawn the client as "
        "SwcraftMcpClient(project_path, env=pinned_escape_env()) "
        "(swcraft_tools.session.pinned_escape_env), or omit `mcp` and let the agent spawn its own."
    )


_AUDIT_FILENAME = "audit.jsonl"

#: The three records this host writes; the lifecycle records come from the engine, under the same
#: ``SWCRAFT_SCOPE_ID``.
_AUDIT_SCOPE_PROPOSED = "scope_proposed"
_AUDIT_SCOPE_ACTIVATED = "scope_activated"
_AUDIT_CALL_REFUSED = "call_refused"


def _with_host_notes(result: Any) -> str:
    """*result* as the string this host reads, with any host note appended.

    A protocol tool answers with a ``changeSetResponse`` body and, as a later text block, an optional host note
    for what the closed body cannot say (a commit whose audit append failed, an elicitation the client could not
    be asked, a commit-tripwire abandonment). ``ToolResult``'s string form is the body alone, so the notes are
    appended here, where the model relays them to the user. A note is prose: a later block that does not parse
    as JSON. A result without a structured body is returned as is: its string already joins every block.
    """
    if getattr(result, "structured", None) is None:
        return result
    notes = [text for text in getattr(result, "texts", ()) if text.strip() and not is_json(text)]
    if not notes:
        return result
    return "\n\n".join([str(result), *notes])


class _TurnGuard:
    """The guarded ``call_tool`` a turn's tool calls are dispatched through.

    One check, only while a scope is in force: an ``export_component`` call with an explicit path
    (:data:`EXPORT_PATH_ARGS`) and a ``build``/``validate`` whose server would shell out
    (:data:`ESCAPE_COMMAND_TOOLS`) are refused. The guard asks the user nothing and reads no model.
    """

    def __init__(
        self,
        *,
        call_tool: Callable[[str, dict], str],
        record: Callable[[str, dict, str], None],
        scoped: bool = False,
        server_env: Mapping[str, str] | None = None,
    ) -> None:
        self._call_tool = call_tool
        self._record = record
        #: Whether a scope bounds this turn; a declined one does too.
        self._scoped = bool(scoped)
        #: The environment the server behind ``call_tool`` runs with; defaults to this process's, which a client
        #: spawned without an explicit environment inherits.
        self._server_env: Mapping[str, str] = os.environ if server_env is None else server_env

    def __call__(self, name: str, args: dict) -> str:
        if self._scoped:
            refusal = self._check_export_path(name, args)
            if refusal is not None:
                return self._refuse(refusal)
            refusal = self._check_escape_command(name)
            if refusal is not None:
                return self._refuse(refusal)
        return _with_host_notes(self._call_tool(name, args))

    def _refuse(self, refusal: Refusal) -> str:
        """Record the refusal in the real log and hand its text back.

        Every message starts with :data:`~swcraft_tools.authorization.REFUSAL_MARKER`, which the agent prompt's
        "a ``REFUSED:`` result is final" rule keys on. These refusals name an in-band alternative because the
        contained form of the call needs no further approval.
        """
        self._record(
            _AUDIT_CALL_REFUSED,
            {"tool": refusal.tool, "code": refusal.code, "target": refusal.target},
            refusal.message,
        )
        return refusal.message

    def _check_export_path(self, name: str, args: dict) -> Refusal | None:
        # The first non-empty path argument, in signature order, is the one
        # the refusal names.
        for key in EXPORT_PATH_ARGS.get(name, ()):
            value = args.get(key)
            if value:
                break
        else:
            return None
        return Refusal(
            # An audit-record value, kept stable so existing logs stay greppable; "session" is historical.
            code="export_path_outside_session",
            tool=name,
            target=str(value),
            message=(
                f"{REFUSAL_MARKER}'{name}' was given an explicit '{key}', which can write to any path at all and "
                f"so is not contained by this task's authorization. While a task scope is in force an "
                f"export writes only to the project's own default location, so call '{name}' without "
                f"'{key}'."
            ),
        )

    def _check_escape_command(self, name: str) -> Refusal | None:
        """Refuse ``build``/``validate`` when the override is configured for the server this guard dispatches to."""
        variable = ESCAPE_COMMAND_TOOLS.get(name)
        # An empty value is "not configured", as the server reads it.
        if variable is None or not self._server_env.get(variable):
            return None
        return Refusal(
            code="escape_command_configured",
            tool=name,
            target=variable,
            message=(
                f"{REFUSAL_MARKER}'{name}' is configured to shell out through {variable}, which runs a command "
                f"against the real project and so is not contained by this task's authorization. While a "
                f"task scope is in force only the in-process '{name}' is available; unset {variable} to use it."
            ),
        )


def _recorded_target(project_root: str, target: str) -> str:
    """A refused call's ``target`` as the audit log records it.

    An absolute path (on either platform's rules) is its project-relative name or
    ``(outside the project)``. A relative one is the LLM's literal and is kept --
    an environment variable's name is one too -- unless it carries a Windows
    anchor (a drive or a root), climbs (``..``, either separator) or starts at a
    home directory (``~``): what it is relative to is the server's, so it is
    judged outside the project rather than resolved here.
    """
    if is_absolute_anywhere(target):
        return project_location(project_root, target)
    # Anchored but not absolute -- Windows' root-relative `\\Users\\x` and
    # drive-relative `C:x` -- names a place on some drive, never a project name.
    windows = PureWindowsPath(target)
    if windows.drive or windows.root or ".." in windows.parts or target.startswith("~"):
        return OUTSIDE_THE_PROJECT
    return target


class SwcraftAgent:
    """LLM-driven agent that operates on a SWCraft project via MCP tools.

    Usage::

        with SwcraftMcpClient(project_path) as mcp:
            agent = SwcraftAgent(project_path, mcp=mcp)
            reply = agent.chat("Add a new SWC called EngineController.")
    """

    def __init__(
        self,
        project_path: str,
        llm: LLMClient | None = None,
        mcp: SwcraftMcpClient | None = None,
        scoped: bool = False,
        approver: Approver | None = None,
        review: str = REVIEW_PANEL,
    ) -> None:
        self._llm = llm if llm is not None else AzureOpenAIClient()
        # An injected client is already spawned and cannot be given the pinned environment, so a scoped run
        # refuses one without it, before anything is stored.
        if scoped and mcp is not None:
            _require_pinned_escape_env(mcp)
        # Checked here too: a scoped agent spawns its first server only at its first turn, and an injected client
        # spawns none, so an invalid mode would otherwise surface mid-chat or never.
        _require_review_mode(review)
        self._review = review
        self._approver: Approver = approver if approver is not None else StdinApprover()
        # A scoped run spawns its server per turn, so none is started here; an injected one is accepted but no
        # scoped turn reaches it.
        if mcp is not None:
            self._mcp: SwcraftMcpClient | None = mcp
        elif scoped:
            self._mcp = None
        else:
            self._mcp = SwcraftMcpClient(project_path, env=review_server_env(review=review), approver=self._approver)
        self._project_path = str(project_path)
        self._scoped = bool(scoped)
        self._conversation = ConversationManager(self._load_system_prompt())
        self._project: SwcraftProject | None = None
        self._scope_id: str = ""

    @property
    def approver(self) -> Approver:
        """The channel every approval this agent asks for goes through; a scripted replay sets it per turn."""
        return self._approver

    @approver.setter
    def approver(self, approver: Approver) -> None:
        self._approver = approver
        # The long-lived client reads the approver at answer time, so a per-turn swap must reach it too.
        # `_can_elicit` is fixed at construction: this redirects an existing channel, never creates one.
        if self._mcp is not None and hasattr(self._mcp, "approver"):
            self._mcp.approver = approver

    def chat(self, user_message: str) -> str:
        """Send a user message, execute any tool calls, and return the reply.

        Every change set is applied only after a human approves the staged delta (``panel`` or ``elicit``
        review), and a delta that weakens a check asks a second question. With ``scoped=True`` the turn first
        proposes a :class:`~swcraft_agent.scope_proposal.ScopeProposal`, asks the user, and spawns its own server
        carrying the approved entries; the engine then refuses every operation outside them before anything is
        staged. A declined proposal is the empty scope: the turn may read and answer, and change nothing.

        Raises:
            RuntimeError: If the tool-use loop aborts (e.g. the ``MAX_TOOL_ROUNDS`` backstop fires). An abort note
                is recorded as the assistant turn, so the next call does not re-present the request.
            ScopeProposalError: In a scoped turn, if the scope could not be proposed or is not valid. No server
                is spawned, and there is no fallback scope.
        """
        if not self._scoped:
            self._scope_id = ""
            return self._plain_turn(user_message)

        self._scope_id = uuid.uuid4().hex
        # Asked before the user message is appended: propose_scope() adds it itself.
        proposal = propose_scope(self._llm, self._conversation.messages, user_message)
        self._record(
            _AUDIT_SCOPE_PROPOSED,
            {"summary": proposal.summary, "entries": proposal.to_json()},
            "read_only" if proposal.is_read_only else "mutation",
        )
        approved, outcome = self._approved_entries(proposal)
        self._record(
            _AUDIT_SCOPE_ACTIVATED,
            {"summary": proposal.summary, "entries": approved},
            outcome,
        )
        return self._scoped_turn(user_message, approved)

    def _approved_entries(self, proposal: ScopeProposal) -> tuple[list[dict[str, Any]], str]:
        """This turn's ``SWCRAFT_APPROVED_SCOPE`` entries and the outcome recorded for them.

        A read-only proposal and a declined one both give the empty scope, in which the engine refuses every
        operation while the turn stays alive to read and answer.
        """
        if proposal.is_read_only:
            return [], "read_only"
        if self._approver.confirm_scope(proposal):
            return proposal.to_json(), "approved"
        return [], "declined"

    def _plain_turn(self, user_message: str) -> str:
        """An unscoped turn, against the long-lived client.

        Raises:
            RuntimeError: there is no long-lived client (a scoped agent).
        """
        if self._mcp is None:
            raise RuntimeError("an unscoped turn needs the long-lived client, and a scoped agent starts none")
        guard = _TurnGuard(
            call_tool=self._mcp.call_tool,
            record=self._record,
            scoped=False,
            server_env=self._long_lived_server_env(),
        )
        reply = self._run_turn(user_message, self._mcp, guard)
        self._conversation.add_assistant(reply)
        return reply

    def _scoped_turn(self, user_message: str, approved: list[dict[str, Any]]) -> str:
        """A turn against this turn's own server, spawned against the real manifest and bounded by *approved*.

        The client is closed in a ``finally``, so no failure leaves an orphaned subprocess.
        """
        env = scoped_server_env(approved, self._scope_id, review=self._review)
        try:
            client = SwcraftMcpClient(self._project_path, env=env, approver=self._approver)
        except TimeoutError as exc:
            # A failed turn, not a failed process: the REPL and run_script handle McpClientError (a RuntimeError),
            # while a bare TimeoutError would end a scoped REPL outright.
            raise McpClientError(f"the turn's MCP server did not start: {exc}") from exc
        try:
            guard = _TurnGuard(
                call_tool=client.call_tool,
                record=self._record,
                scoped=True,
                server_env=client.server_env,
            )
            reply = self._run_turn(user_message, client, guard)
        finally:
            client.close()
        self._conversation.add_assistant(reply)
        return reply

    def _run_turn(self, user_message: str, client: Any, guard: _TurnGuard) -> str:
        """Append the user turn and drive the tool-use loop through *guard*.

        Both handlers name their class, so a real bug propagates untouched. Each records an abort note so the
        next call does not re-present the abandoned request; ``KeyboardInterrupt`` arrives when the user walks away
        from a review prompt.
        """
        self._conversation.add_user(user_message)
        try:
            return self._llm.complete(
                messages=self._conversation.messages,
                tools=client.list_openai_tools(),
                tool_executor=guard,
            )
        except RuntimeError as exc:
            self._conversation.add_assistant(f"[turn aborted: {exc}]")
            raise
        except KeyboardInterrupt:
            self._conversation.add_assistant("[turn aborted: interrupted at a review prompt]")
            raise

    def _resolved_project(self) -> SwcraftProject:
        """The real project, resolved lazily so a prompt-only agent needs no readable manifest."""
        if self._project is None:
            self._project = require_project(self._project_path)
        return self._project

    def _long_lived_server_env(self) -> Mapping[str, str] | None:
        """The environment the long-lived client's server runs with, or ``None`` for this process's.

        The escape-command gate must read the server's environment, not this process's: a client given an
        explicit ``env`` carrying a build override would otherwise pass unrefused. A test double need not model
        one.
        """
        return getattr(self._mcp, "server_env", None)

    def _record(self, operation: str, args: dict, result: str) -> None:
        """Append a host-side record to the real project's audit log, under this turn's scope id.

        The records land whatever the turn's change set does: a rejected change set does not take the evidence
        of a declined scope or a refused call with it.
        """
        project = self._resolved_project()
        root = project.resolved_project_root
        # A refused call's ``target`` may name any place, so it is recorded relative to the project or as outside
        # it. Only ``target``: a summary that starts with ``/`` is evidence, not a location.
        recorded = dict(args)
        if isinstance(recorded.get("target"), str):
            recorded["target"] = _recorded_target(root, recorded["target"])
        log = AuditLog(str(Path(project.resolved_dist_root) / _AUDIT_FILENAME), project_root=root)
        log.log_write(operation, recorded, result, scope_id=self._scope_id or None)

    def _load_system_prompt(self) -> str:
        """Load the system prompt text.

        Resolution order:
        1. ``SWCRAFT_AGENT_SYSTEM_PROMPT`` environment variable (path to file).
        2. ``agent-system-prompt.md`` packaged beside this module.

        ``{{MAX_BUILD_RETRIES}}`` is substituted by a plain string replace, so other braces are left untouched.

        Raises:
            SystemPromptNotFoundError: If neither source resolves to a readable file.
        """
        env_path = os.environ.get("SWCRAFT_AGENT_SYSTEM_PROMPT", "").strip()
        if env_path:
            p = Path(env_path)
            if p.is_file():
                return self._substitute_placeholders(p.read_text(encoding="utf-8"))
            raise SystemPromptNotFoundError(f"SWCRAFT_AGENT_SYSTEM_PROMPT points to a non-existent file: {env_path}")

        discovered = _packaged_system_prompt_path()
        if discovered is not None:
            return self._substitute_placeholders(discovered.read_text(encoding="utf-8"))

        raise SystemPromptNotFoundError(
            f"The swcraft_agent package carries no {SYSTEM_PROMPT_FILENAME} — set the "
            "SWCRAFT_AGENT_SYSTEM_PROMPT environment variable to the prompt file path."
        )

    @staticmethod
    def _substitute_placeholders(text: str) -> str:
        """Replace prompt placeholders with their configured values."""
        return text.replace("{{MAX_BUILD_RETRIES}}", str(_max_build_retries()))
