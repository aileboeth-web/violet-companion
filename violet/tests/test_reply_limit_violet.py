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
