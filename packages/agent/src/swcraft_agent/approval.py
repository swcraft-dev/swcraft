"""The approval channel.

Three things in a mutation turn need a human `y`: the proposed
:class:`~swcraft_agent.scope_proposal.ScopeProposal`, the semantic delta a change set computed, and, when the delta
weakens a check, the safety question.

Only the scope prompt is composed here. The delta and the safety verdict are rendered by the server
(:mod:`swcraft_tools.editing.review`) and arrive as the MCP elicitation message, which
:func:`swcraft_agent.mcp_client.render_elicitation` shows unchanged with :data:`ANSWER_PROMPT` appended, so a
``swcraft-agent`` user answers the same question a VS Code user does. An elicitation carries no discriminator, so
both server questions are answered through :meth:`Approver.ask`.

No approval exists without a recorded answer, and every approver defaults to deny: EOF, an empty line, anything
that is not ``y``, and an exhausted scripted list all mean *no*, so an exhausted script behaves like a user who
said nothing. The LLM reaches none of this.
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Sequence
from typing import TYPE_CHECKING, TextIO

from swcraft_agent.scope_proposal import render_scope

if TYPE_CHECKING:
    from swcraft_agent.scope_proposal import ScopeProposal

#: Compared case-insensitively after stripping; everything else, ``yes`` included, denies.
APPROVE_ANSWER = "y"

#: The answer convention every prompt of this host ends with, the server's elicitations included
#: (:data:`swcraft_agent.mcp_client.ELICITATION_ANSWER_PROMPT` is this value).
ANSWER_PROMPT = "[y/N]: "

SCOPE_QUESTION = f"Approve this scope? {ANSWER_PROMPT}"


def is_approval(answer: str) -> bool:
    """Whether *answer* is the literal approval, ignoring case and surrounding space."""
    return answer.strip().lower() == APPROVE_ANSWER


def render_scope_approval(proposal: ScopeProposal) -> str:
    """The scope prompt: every ``{operation, address}`` entry, then the question."""
    return f"{render_scope(proposal)}\n\n{SCOPE_QUESTION}"


class Approver(ABC):
    """The only path to an approval: subclasses implement :meth:`ask` and nothing else."""

    @abstractmethod
    def ask(self, prompt: str) -> bool:
        """Show *prompt* and return whether the answer approved it.

        Implementations must default to deny when there is no answer to read.
        """

    def confirm_scope(self, proposal: ScopeProposal) -> bool:
        """Ask the user to approve *proposal* before any scoped server is spawned."""
        return self.ask(render_scope_approval(proposal))


class StdinApprover(Approver):
    """Asks a terminal user, one line at a time.

    ``sys.stdin``/``sys.stdout`` are resolved at call time, so a later redirection still reaches the user.
    """

    def __init__(self, stream: TextIO | None = None, out: TextIO | None = None) -> None:
        self._stream = stream
        self._out = out

    def ask(self, prompt: str) -> bool:
        stream = sys.stdin if self._stream is None else self._stream
        out = sys.stdout if self._out is None else self._out
        print(prompt, file=out)
        line = stream.readline()
        if line == "":
            return False
        return is_approval(line)


class ScriptedApprover(Approver):
    """Answers from a scenario file's ``approvals`` list, in order; running out of answers denies."""

    def __init__(self, answers: Sequence[str] | None = None) -> None:
        for index, answer in enumerate(answers or ()):
            if not isinstance(answer, str):
                raise TypeError(f"approval {index} must be a string, got {type(answer).__name__}: {answer!r}")
        self._answers: deque[str] = deque(answers or ())
        #: Every prompt shown, in order.
        self.prompts: list[str] = []
        #: The answer used for each prompt; ``None`` where the list ran out.
        self.answers_given: list[str | None] = []

    @property
    def remaining(self) -> int:
        """How many answers are still unconsumed."""
        return len(self._answers)

    def ask(self, prompt: str) -> bool:
        self.prompts.append(prompt)
        if not self._answers:
            self.answers_given.append(None)
            return False
        answer = self._answers.popleft()
        self.answers_given.append(answer)
        return is_approval(answer)
