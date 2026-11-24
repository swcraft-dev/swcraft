import importlib


def test_swcraft_agent_imports():
    mod = importlib.import_module("swcraft_agent")
    assert mod is not None
