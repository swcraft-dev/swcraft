"""LLM client abstraction: LLMClient ABC and AzureOpenAIClient implementation."""

import json
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai.types.shared_params import ResponseFormatJSONSchema

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 10

#: The ``json_schema.name`` every structured call is sent under; the schema itself is the caller's.
STRUCTURED_RESPONSE_NAME = "structured_response"


class StructuredResponseError(RuntimeError):
    """A structured completion did not come back as a JSON object.

    Raised instead of returning ``{}``, which would parse as a scope proposal carrying no restrictions.
    """


class LLMClient(ABC):
    """Abstract interface for an LLM that supports tool-use."""

    @abstractmethod
    def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_executor: Callable[[str, dict], str],
    ) -> str:
        """Send messages to the LLM, executing any tool calls, and return the final text.

        *messages* and *tools* are in OpenAI chat format. Each requested tool call is dispatched via
        ``tool_executor(name, args)`` and its result fed back until the model returns plain text; raises
        ``RuntimeError`` after ``MAX_TOOL_ROUNDS`` rounds.
        """

    @abstractmethod
    def complete_structured(self, messages: list[dict], schema: dict) -> dict:
        """Ask the LLM for one JSON object matching *schema*, with **no** tool use.

        The scope-proposal channel: the schema travels as the request's response format and no tools are sent,
        so the model cannot act while it proposes. Raises :class:`StructuredResponseError` when the response is
        not a JSON object.
        """


class AzureOpenAIClient(LLMClient):
    """LLM client backed by Azure OpenAI chat completions.

    Configuration is read from environment variables:
        AZURE_OPENAI_ENDPOINT   (required)
        AZURE_OPENAI_API_KEY    (required)
        AZURE_OPENAI_DEPLOYMENT (optional, default: gpt-4.1)
        AZURE_OPENAI_API_VERSION (optional, default: 2024-12-01-preview)
    """

    def __init__(self) -> None:
        from openai import AzureOpenAI  # deferred import — keeps test doubles fast

        self._deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1")
        self._client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview"),
        )

    def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_executor: Callable[[str, dict], str],
    ) -> str:
        working = list(messages)
        for round_num in range(MAX_TOOL_ROUNDS):
            kwargs: dict = {"model": self._deployment, "messages": working}
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            response = self._client.chat.completions.create(**kwargs)
            message = response.choices[0].message

            if not message.tool_calls:
                return message.content or ""

            logger.debug("Round %d: %d tool call(s)", round_num + 1, len(message.tool_calls))
            working.append(message.model_dump(exclude_unset=False))

            for tc in message.tool_calls:
                args = json.loads(tc.function.arguments)
                result = tool_executor(tc.function.name, args)
                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                )

        raise RuntimeError(f"Exceeded maximum tool-use rounds ({MAX_TOOL_ROUNDS})")

    def complete_structured(self, messages: list[dict], schema: dict) -> dict:
        # No `tools` key: a proposal round must not be able to act. `strict` is not set: the scope schema has
        # genuinely optional keys (an omitted `element` or `path` differs from an explicit null), which strict
        # mode cannot express; `scope_proposal.parse_proposal` is the real gate.
        response_format: ResponseFormatJSONSchema = {
            "type": "json_schema",
            "json_schema": {"name": STRUCTURED_RESPONSE_NAME, "schema": schema},
        }
        response = self._client.chat.completions.create(
            model=self._deployment,
            # The interface carries the history as plain dicts in OpenAI's message format, which
            # the SDK declares as a union of TypedDicts that a plain dict does not satisfy.
            messages=list(messages),  # type: ignore[arg-type]
            response_format=response_format,
        )
        content = response.choices[0].message.content
        if not content:
            raise StructuredResponseError("the structured response was empty")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise StructuredResponseError(f"the structured response was not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise StructuredResponseError(
                f"the structured response was a JSON {type(payload).__name__}, expected an object"
            )
        return payload
