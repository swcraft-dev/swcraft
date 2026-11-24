"""Conversation manager: tracks system prompt and dialog turns."""

import logging

logger = logging.getLogger(__name__)


class ConversationManager:
    """Maintains dialog history for a single agent session.

    Stores turns as {"role": ..., "content": ...} dicts compatible with
    the OpenAI chat completions API. The system prompt is prepended on
    every read via the `messages` property.
    """

    def __init__(self, system_prompt: str) -> None:
        self._system_prompt = system_prompt
        self._turns: list[dict] = []

    @property
    def messages(self) -> list[dict]:
        """Return a copy of the full message list (system + turns)."""
        result: list[dict] = [{"role": "system", "content": self._system_prompt}]
        result.extend(self._turns)
        return result

    def add_user(self, content: str) -> None:
        """Append a user turn."""
        self._turns.append({"role": "user", "content": content})
        logger.debug("user: %s", content[:80])

    def add_assistant(self, content: str) -> None:
        """Append an assistant turn."""
        self._turns.append({"role": "assistant", "content": content})
        logger.debug("assistant: %s", content[:80])
