"""Violet stage 1: every agent's real prompt ships with the program (a fresh install has no prompt in its .env)."""
from pathlib import Path

import pytest

from ai.agents import ChatAgent, IntentAgent, MemoryAgent, PerformerAgent, ProactiveAgent
from tools.harness import _parse_env_file

PROJECT = Path(__file__).resolve().parents[1]
AGENTS = [IntentAgent, ChatAgent, MemoryAgent, PerformerAgent, ProactiveAgent]


@pytest.mark.parametrize("agent", AGENTS, ids=lambda a: a.__name__)
def test_without_env_the_agent_loads_its_shipped_prompt_file(agent, monkeypatch):
    monkeypatch.delenv(agent.system_prompt_env_key, raising=False)
    shipped = (PROJECT / agent.default_prompt_file).read_text(encoding="utf-8").strip()
    assert len(shipped) > 500                                        # a real prompt, not a one-line default
    assert agent.configured_prompt() == shipped
    assert agent.configured_prompt() != agent.default_system_prompt


@pytest.mark.parametrize("agent", AGENTS, ids=lambda a: a.__name__)
def test_an_env_prompt_still_overrides_the_file(agent, monkeypatch):
    monkeypatch.setenv(agent.system_prompt_env_key, "  自定义提示词  ")
    assert agent.configured_prompt() == "自定义提示词"


def test_a_missing_prompt_file_falls_back_to_the_one_line_default(monkeypatch):
    monkeypatch.delenv("INTENT_LLM_PROMPT", raising=False)
    monkeypatch.setattr(IntentAgent, "default_prompt_file", "persona/prompts/does-not-exist.txt")
    assert IntentAgent.configured_prompt() == IntentAgent.default_system_prompt


def test_env_example_no_longer_carries_prompts():
    assert not [k for k in _parse_env_file(PROJECT / "env_example") if k.endswith("_LLM_PROMPT")]


@pytest.mark.parametrize("word", ["麦当劳", "高德", "AMAP", "MCDONALDS", "TESLA", "特斯拉", "北邮", "沙河"])
def test_upstream_only_features_are_gone_from_shipped_text(word):
    shipped = [PROJECT / "env_example", PROJECT / "proactive_config.json", PROJECT / "mcp_tool_policy.json",
               *sorted((PROJECT / "persona").rglob("*.txt"))]
    for path in shipped:
        assert word.lower() not in path.read_text(encoding="utf-8").lower(), (word, path.name)
