"""Tests for LLMClient ABC, AzureOpenAIClient construction, and FakeLLMClient."""

import copy

import pytest
from swcraft_agent.llm_client import (
    MAX_TOOL_ROUNDS,
    STRUCTURED_RESPONSE_NAME,
    AzureOpenAIClient,
    LLMClient,
    StructuredResponseError,
)

from tests.conftest import FakeLLMClient

# ---------------------------------------------------------------------------
# AzureOpenAIClient construction (no real Azure calls)
# ---------------------------------------------------------------------------

_REQUIRED_VARS = {
    "AZURE_OPENAI_ENDPOINT": "https://fake.openai.azure.com/",
    "AZURE_OPENAI_API_KEY": "fake-key",
}


def test_azure_client_constructs_with_required_env_vars(monkeypatch):
    for k, v in _REQUIRED_VARS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    client = AzureOpenAIClient()
    assert client._deployment == "gpt-4.1"


def test_azure_client_deployment_override(monkeypatch):
    for k, v in _REQUIRED_VARS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    client = AzureOpenAIClient()
    assert client._deployment == "gpt-4o"


def test_azure_client_missing_endpoint_raises(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    with pytest.raises(KeyError):
        AzureOpenAIClient()


def test_azure_client_missing_api_key_raises(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fake.openai.azure.com/")
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    with pytest.raises(KeyError):
        AzureOpenAIClient()


_ASSISTANT_TOOL_CALL_DUMP = {
    "role": "assistant",
    "content": None,
    "tool_calls": [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "build", "arguments": '{"target": "arxml"}'},
        }
    ],
}


class _FakeFunction:
    name = "build"
    arguments = '{"target": "arxml"}'


class _FakeToolCall:
    id = "call_1"
    type = "function"
    function = _FakeFunction()


class _FakeToolCallMessage:
    """SDK message object for a round that requests one tool call."""

    role = "assistant"
    content = None
    tool_calls = [_FakeToolCall()]

    def model_dump(self, exclude_unset=False):
        # A shallow copy would share tool_calls with the expected constant, making comparisons self-referential.
        return copy.deepcopy(_ASSISTANT_TOOL_CALL_DUMP)


class _FakeTextMessage:
    """SDK message object for a plain-text (no tool calls) round."""

    role = "assistant"
    content = "All done."
    tool_calls = None

    def model_dump(self, exclude_unset=False):
        return {"role": "assistant", "content": self.content, "tool_calls": None}


def _response_with(message):
    class _FakeChoice:
        pass

    class _FakeResponse:
        pass

    choice = _FakeChoice()
    choice.message = message
    response = _FakeResponse()
    response.choices = [choice]
    return response


def _make_client(monkeypatch):
    for k, v in _REQUIRED_VARS.items():
        monkeypatch.setenv(k, v)
    return AzureOpenAIClient()  # no network call — __init__ only builds the SDK object


def test_azure_client_max_tool_rounds_backstop(monkeypatch):
    """The MAX_TOOL_ROUNDS hard backstop fires on a model that never stops requesting tools.

    Also asserts the per-round feedback: each request carries the previous round's assistant
    tool-call message and the executor's result as a role:"tool" entry bound to the tool_call_id.
    """
    client = _make_client(monkeypatch)

    seen_messages: list[list[dict]] = []

    def fake_create(**kwargs):
        seen_messages.append(list(kwargs["messages"]))
        return _response_with(_FakeToolCallMessage())

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)

    executor_calls: list[tuple[str, dict]] = []

    def executor(name, args):
        executor_calls.append((name, args))
        return "ERROR: build failed"

    with pytest.raises(RuntimeError, match="Exceeded maximum tool-use rounds"):
        client.complete(messages=[], tools=[], tool_executor=executor)

    assert len(seen_messages) == MAX_TOOL_ROUNDS
    assert executor_calls == [("build", {"target": "arxml"})] * MAX_TOOL_ROUNDS
    assert seen_messages[1] == [
        _ASSISTANT_TOOL_CALL_DUMP,
        {"role": "tool", "tool_call_id": "call_1", "content": "ERROR: build failed"},
    ]
    assert len(seen_messages[-1]) == 2 * (MAX_TOOL_ROUNDS - 1)


def test_azure_client_tool_round_then_text(monkeypatch):
    """One tool round then a plain-text round: complete() returns the final text content.

    It dispatches the tool and feeds its result back first.
    """
    client = _make_client(monkeypatch)

    seen_messages: list[list[dict]] = []
    scripted = [_FakeToolCallMessage(), _FakeTextMessage()]

    def fake_create(**kwargs):
        seen_messages.append(list(kwargs["messages"]))
        return _response_with(scripted[len(seen_messages) - 1])

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)

    executor_calls: list[tuple[str, dict]] = []

    def executor(name, args):
        executor_calls.append((name, args))
        return '{"success": true, "errors": []}'

    initial = [{"role": "user", "content": "Build the project."}]
    result = client.complete(messages=initial, tools=[], tool_executor=executor)

    assert result == "All done."
    assert executor_calls == [("build", {"target": "arxml"})]
    assert seen_messages[1] == [
        {"role": "user", "content": "Build the project."},
        _ASSISTANT_TOOL_CALL_DUMP,
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": '{"success": true, "errors": []}',
        },
    ]
    assert initial == [{"role": "user", "content": "Build the project."}]


# ---------------------------------------------------------------------------
# AzureOpenAIClient.complete_structured
# ---------------------------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"kind": {"type": "string"}},
}


class _FakeStructuredMessage:
    """SDK message object for a structured (JSON) response round."""

    role = "assistant"
    tool_calls = None

    def __init__(self, content):
        self.content = content


def _structured_client(monkeypatch, content):
    """An AzureOpenAIClient whose completions endpoint returns *content*."""
    client = _make_client(monkeypatch)
    captured: list[dict] = []

    def fake_create(**kwargs):
        captured.append(kwargs)
        return _response_with(_FakeStructuredMessage(content))

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)
    return client, captured


def test_complete_structured_is_on_the_abc():
    """Every LLMClient must implement it — an ABC method, not an Azure extra."""
    assert "complete_structured" in LLMClient.__abstractmethods__


def test_azure_complete_structured_passes_schema_as_json_schema_response_format(monkeypatch):
    client, captured = _structured_client(monkeypatch, '{"kind": "read_only"}')

    payload = client.complete_structured([{"role": "user", "content": "hi"}], _SCHEMA)

    assert payload == {"kind": "read_only"}
    assert len(captured) == 1
    response_format = captured[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == STRUCTURED_RESPONSE_NAME
    assert response_format["json_schema"]["schema"] is _SCHEMA


def test_azure_complete_structured_sends_no_tools(monkeypatch):
    """The LLM cannot act while it proposes: no tools, no tool_choice."""
    client, captured = _structured_client(monkeypatch, "{}")

    client.complete_structured([{"role": "user", "content": "hi"}], _SCHEMA)

    assert "tools" not in captured[0]
    assert "tool_choice" not in captured[0]


def test_azure_complete_structured_sends_the_given_messages(monkeypatch):
    client, captured = _structured_client(monkeypatch, "{}")
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]

    client.complete_structured(messages, _SCHEMA)

    assert captured[0]["messages"] == messages
    assert messages == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


@pytest.mark.parametrize(
    "content",
    ["not json at all", "", None, "[1, 2, 3]", '"a string"', "null"],
    ids=["garbage", "empty", "none", "array", "string", "null"],
)
def test_azure_complete_structured_rejects_a_non_object_response(monkeypatch, content):
    """Fail closed: anything that is not a JSON object raises, never returns {}."""
    client, _ = _structured_client(monkeypatch, content)

    with pytest.raises(StructuredResponseError):
        client.complete_structured([], _SCHEMA)


# ---------------------------------------------------------------------------
# FakeLLMClient tests
# ---------------------------------------------------------------------------


def test_fake_single_round_no_tool_calls():
    fake = FakeLLMClient(["hello world"])
    result = fake.complete([], [], lambda name, args: "")
    assert result == "hello world"
    assert fake.calls == []


def test_fake_multi_round_tool_calls_then_text():
    executed = []

    def executor(name, args):
        executed.append((name, args))
        return "ok"

    fake = FakeLLMClient(
        [
            [("add_swc", {"name": "MySwc"})],
            [("add_port", {"swc": "MySwc", "port": "p1"})],
            "done",
        ]
    )
    result = fake.complete([], [], executor)
    assert result == "done"
    assert fake.calls == [
        ("add_swc", {"name": "MySwc"}),
        ("add_port", {"swc": "MySwc", "port": "p1"}),
    ]
    assert executed == fake.calls


def test_fake_multiple_tool_calls_in_one_round():
    executed = []

    def executor(name, args):
        executed.append(name)
        return "ok"

    fake = FakeLLMClient(
        [
            [("tool_a", {}), ("tool_b", {}), ("tool_c", {})],
            "result",
        ]
    )
    result = fake.complete([], [], executor)
    assert result == "result"
    assert executed == ["tool_a", "tool_b", "tool_c"]


def test_fake_program_exhausted_raises():
    fake = FakeLLMClient([])
    with pytest.raises(RuntimeError, match="exhausted"):
        fake.complete([], [], lambda n, a: "")


def test_fake_multiple_complete_calls_consume_successive_rounds():
    fake = FakeLLMClient(["first", "second"])
    assert fake.complete([], [], lambda n, a: "") == "first"
    assert fake.complete([], [], lambda n, a: "") == "second"


def test_fake_records_calls_across_multiple_complete_invocations():
    def executor(name, args):
        return "ok"

    fake = FakeLLMClient(
        [
            [("tool_x", {"a": 1})],
            "reply1",
            [("tool_y", {"b": 2})],
            "reply2",
        ]
    )
    fake.complete([], [], executor)
    fake.complete([], [], executor)
    assert fake.calls == [("tool_x", {"a": 1}), ("tool_y", {"b": 2})]


# ---------------------------------------------------------------------------
# FakeLLMClient.complete_structured
# ---------------------------------------------------------------------------


def test_fake_structured_consumes_its_own_program():
    fake = FakeLLMClient(["text reply"], structured=[{"kind": "read_only"}])

    assert fake.complete_structured([{"role": "user", "content": "hi"}], _SCHEMA) == {"kind": "read_only"}
    assert fake.complete([], [], lambda n, a: "") == "text reply"


def test_fake_structured_rounds_are_consumed_in_order():
    fake = FakeLLMClient([], structured=[{"n": 1}, {"n": 2}])

    assert fake.complete_structured([], _SCHEMA) == {"n": 1}
    assert fake.complete_structured([], _SCHEMA) == {"n": 2}


def test_fake_structured_missing_round_raises_rather_than_falling_back():
    """No fallback: a missing structured round is a loud failure, not a payload."""
    fake = FakeLLMClient(["text reply"])

    with pytest.raises(RuntimeError, match="structured program exhausted"):
        fake.complete_structured([], _SCHEMA)
    assert fake.complete([], [], lambda n, a: "") == "text reply"


def test_fake_structured_records_messages_and_schema():
    fake = FakeLLMClient([], structured=[{"kind": "read_only"}])
    messages = [{"role": "user", "content": "hi"}]

    fake.complete_structured(messages, _SCHEMA)

    assert fake.structured_calls == [(messages, _SCHEMA)]


def test_fake_structured_queued_exception_is_raised():
    """The transport-failure case is expressible in the double itself."""
    fake = FakeLLMClient([], structured=[ConnectionError("azure unreachable")])

    with pytest.raises(ConnectionError, match="azure unreachable"):
        fake.complete_structured([], _SCHEMA)
