"""2026-11-03 每次回复最多几条消息 (VIOLET_REPLY_MAX_MESSAGES, ai/chat_output.cap_messages)."""

from __future__ import annotations


def test_no_limit_by_default(monkeypatch):
    monkeypatch.delenv("VIOLET_REPLY_MAX_MESSAGES", raising=False)
    from ai.chat_output import normalize_chat_output
    out = normalize_chat_output("一\n二\n三\n四\n五")
    assert [m["content"] for m in out["messages"]] == ["一", "二", "三", "四", "五"]


def test_over_the_limit_lines_join_the_last_one_and_media_do_not_count(monkeypatch):
    monkeypatch.setenv("VIOLET_REPLY_MAX_MESSAGES", "2")
    from ai.chat_output import cap_messages, normalize_chat_output
    out = normalize_chat_output("早呀\n今天降温了\n记得穿外套\n别感冒")
    assert [m["content"] for m in out["messages"]] == ["早呀", "今天降温了\n记得穿外套\n别感冒"]
    mixed = [{"type": "text", "content": "看"}, {"type": "image", "asset_id": 3, "content": None},
             {"type": "voice", "content": "好看吗"}, {"type": "sticker", "sticker_id": 1, "content": None},
             {"type": "text", "content": "嘿嘿"}]
    capped = cap_messages(mixed, 2)
    assert [m["type"] for m in capped] == ["text", "image", "voice", "sticker"]
    assert capped[2]["content"] == "好看吗\n嘿嘿"                                    # nothing she said is lost
    assert cap_messages(mixed, 0) == mixed


def test_a_whole_prompt_persona_gets_no_length_rules(monkeypatch):
    """2026-11-03 (user: 用 Eve 人设时惜字如金): Eve / 大肥鱼 get chat.own.v1 instead of chat.v3."""
    from ai import persona
    from ai.agents import chat_agent

    class Whole:
        data = {"prompt_text": "你是 Eve，可以一句一行发好几条"}
    monkeypatch.setattr(persona.PersonaCore, "load", classmethod(lambda cls, *a, **k: Whole()))
    monkeypatch.delenv("CHAT_LLM_PROMPT", raising=False)
    frame = chat_agent.own_persona_prompt()
    assert frame and "分几条发，都完全按后面“你自己”" in frame
    for brake in ("几个字就够了", "一到三条", "一两个实在的点"):
        assert brake not in frame
    v3 = open(chat_agent.PROJECT_ROOT / chat_agent.ChatAgent.default_prompt_file, encoding="utf-8").read() \
        if hasattr(chat_agent, "PROJECT_ROOT") else None
    if v3:
        assert "一到三条" in v3                              # the other personas keep their rules
    # 0.7.44: the prompt version chosen on the settings page lives in CHAT_LLM_PROMPT -- it must not come back in front
    monkeypatch.setenv("CHAT_LLM_PROMPT", "你是 Violet，一个虚构的女孩……一句话甚至几个字就够了")
    assert chat_agent.own_persona_prompt() == frame
    monkeypatch.delenv("CHAT_LLM_PROMPT")
    monkeypatch.setenv("VIOLET_OWN_PERSONA_FRAME", "0")
    assert chat_agent.own_persona_prompt() is None

    class Built:
        data = {"identity": {"name": "Violet"}}
    monkeypatch.setenv("VIOLET_OWN_PERSONA_FRAME", "1")
    monkeypatch.setattr(persona.PersonaCore, "load", classmethod(lambda cls, *a, **k: Built()))
    assert chat_agent.own_persona_prompt() is None


def test_the_reply_starts_with_the_own_frame(monkeypatch):
    from tests.fakes import FakeLLM, default_responders, install_fake_llm
    llm = install_fake_llm(monkeypatch, FakeLLM(default_responders("嗯")))
    from ai.agents import chat_agent
    monkeypatch.setattr(chat_agent, "own_persona_prompt", lambda: "【整段人设的框架】")
    chat_agent.ChatAgent().run("早")
    first = [c["messages"][0]["content"] for c in llm.calls]
    assert first and first[0] == "【整段人设的框架】"


def test_the_prompt_tells_her_the_limit(monkeypatch):
    from tests.fakes import FakeLLM, default_responders, install_fake_llm
    llm = install_fake_llm(monkeypatch, FakeLLM(default_responders("嗯")))
    monkeypatch.setenv("VIOLET_REPLY_MAX_MESSAGES", "3")
    from ai.agents.chat_agent import ChatAgent
    ChatAgent().run("早")
    sent = [m.get("content") for c in llm.calls for m in c["messages"] if isinstance(m, dict)]
    assert any("每次回复最多 3 条消息" in (s or "") for s in sent)
    llm.calls.clear()
    monkeypatch.setenv("VIOLET_REPLY_MAX_MESSAGES", "0")
    ChatAgent().run("早")
    assert not any("【条数】" in (m.get("content") or "") for c in llm.calls for m in c["messages"] if isinstance(m, dict))
