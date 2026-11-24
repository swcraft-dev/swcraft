"""Tests for ConversationManager."""

from swcraft_agent.conversation import ConversationManager


def test_system_prompt_is_first_message():
    cm = ConversationManager("You are a test assistant.")
    msgs = cm.messages
    assert msgs[0] == {"role": "system", "content": "You are a test assistant."}


def test_messages_returns_copy_not_live_reference():
    cm = ConversationManager("sys")
    snapshot = cm.messages
    cm.add_user("hello")
    assert len(snapshot) == 1
    assert len(cm.messages) == 2


def test_add_user_and_assistant_ordering():
    cm = ConversationManager("sys")
    cm.add_user("first")
    cm.add_assistant("reply")
    cm.add_user("second")
    msgs = cm.messages
    assert msgs[1] == {"role": "user", "content": "first"}
    assert msgs[2] == {"role": "assistant", "content": "reply"}
    assert msgs[3] == {"role": "user", "content": "second"}


def test_empty_conversation_has_only_system_message():
    cm = ConversationManager("sys")
    assert cm.messages == [{"role": "system", "content": "sys"}]


def test_multiple_snapshots_are_independent():
    cm = ConversationManager("sys")
    cm.add_user("a")
    snap1 = cm.messages
    cm.add_user("b")
    snap2 = cm.messages
    assert len(snap1) == 2
    assert len(snap2) == 3
