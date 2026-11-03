"""Capacité « outils » des modèles locaux : ce qu'Ollama annonce ne suffit pas pour les modèles qui ne les appellent pas."""

from gateway.registry import ModelRegistry


def test_reasoning_model_that_never_calls_tools_is_not_routed_with_tools():
    reg = ModelRegistry()
    reg.local_caps["ollama/deepseek-r1:14b"] = {"tools", "thinking", "completion"}  # annonce « tools » à tort
    assert reg.get("ollama", "deepseek-r1:14b", "local").tools is False


def test_model_that_calls_tools_keeps_the_announced_capability():
    reg = ModelRegistry()
    reg.local_caps["ollama/qwen2.5:14b"] = {"completion", "tools"}
    assert reg.get("ollama", "qwen2.5:14b", "local").tools is True
