"""2026-11-01 capability rework (scenario eval on the server: Intent took 7-22 s and ~7.7k tokens per message and
marked voice / pictures "not configured"): the owner's turn without the Intent call, Chat's own memory lookup, the
abilities list naming her voice."""

from __future__ import annotations

import json

import pytest

from tests.fakes import FakeLLM, completion, default_responders, install_fake_llm


@pytest.fixture()
def agents(monkeypatch, violetdb):
    monkeypatch.setenv("VIOLET_INTENT_STEP", "0")
    fake = install_fake_llm(monkeypatch, FakeLLM(default_responders()))
    from ai.agents import ChatAgent, IntentAgent, MemoryAgent, PerformerAgent
    return fake, {"intent": IntentAgent(), "chat": ChatAgent(), "memory": MemoryAgent(), "performer": PerformerAgent()}


def _run(a, text):
    from ai.chat import run_pipeline
    return run_pipeline(user_message=text, intent_agent=a["intent"], chat_agent=a["chat"],
                        performer_agent=a["performer"], memory_agent=a["memory"])


def _tool_names(call):
    return [t["function"]["name"] for t in call.get("tools") or []]


def test_the_turn_has_no_intent_call(agents):
    fake, a = agents
    result = _run(a, "今天好累")
    assert result["chat"]["content"] == "好呀\n我在听" and not result["chat_failed"]
    assert not fake.calls_for("fake-intent") and len(fake.calls_for("fake-chat")) == 1
    assert "intent" not in result["errors"]


def test_chat_looks_memories_up_itself(agents, violetdb):
    fake, a = agents
    violetdb.add_event_memory("用户下周三有高数考试", category="event")
    answers = iter([completion(tool_calls=[{"name": "query_memory", "arguments": {"query": "高数 考试"}}]),
                    completion("你周三考高数吧 加油")])
    fake.responders["fake-chat"] = lambda kw: next(answers)
    result = _run(a, "你还记得我说的考试吗")
    first = fake.calls_for("fake-chat")[0]
    assert "query_memory" in _tool_names(first)
    assert result["chat"]["content"] == "你周三考高数吧 加油"
    assert any("高数" in (m.get("content") or "") for m in result["recalled_memories"])


def test_no_memory_tool_without_memories_or_with_the_intent_step(agents, monkeypatch):
    fake, a = agents
    _run(a, "在吗")
    assert "query_memory" not in _tool_names(fake.calls_for("fake-chat")[-1])     # nothing to find
    from tests.fakes import intent_json
    fake.responders["fake-intent"] = lambda kw: intent_json("chat")
    monkeypatch.setenv("VIOLET_INTENT_STEP", "1")
    _run(a, "在吗")
    assert fake.calls_for("fake-intent")                                        # the old way still works


def test_the_abilities_list_names_her_voice(monkeypatch, violetdb):
    from ai import voice
    from ai.tool_manager import Catalog, abilities_note
    monkeypatch.setenv("VIOLET_INTENT_STEP", "0")
    monkeypatch.setattr(voice, "ready", lambda: True)
    note = abilities_note(Catalog(), [])
    assert "- 语音：可以" in note and "query_memory" in note
    monkeypatch.setattr(voice, "ready", lambda: False)
    assert "发不了语音" in abilities_note(Catalog(), [])


# ------------------------------------------------------------------------------------------------ prompt caching

def test_cache_hints_per_provider(monkeypatch):
    from ai import model_params as mp
    msgs = [{"role": "system", "content": "规则", mp.CACHE_MARK: True}, {"role": "user", "content": "你好"}]
    gpt = mp.apply({"model": "gpt-6-luna", "messages": msgs, "cache_key": "violet-ChatAgent"})
    assert gpt["extra_body"] == {"prompt_cache_key": "violet-ChatAgent"} and "cache_key" not in gpt
    assert gpt["messages"][0] == {"role": "system", "content": "规则"}                  # the mark never leaves
    claude = mp.apply({"model": "claude-opus-5-5", "messages": msgs, "cache_key": "k"})
    assert claude["messages"][0]["content"] == [{"type": "text", "text": "规则", "cache_control": {"type": "ephemeral"}}]
    assert "extra_body" not in claude and claude["messages"][1] == {"role": "user", "content": "你好"}
    for model in ("deepseek-chat", "qwen-max"):
        plain = mp.apply({"model": model, "messages": msgs, "cache_key": "k"})
        assert "extra_body" not in plain and plain["messages"][0] == {"role": "system", "content": "规则"}
    monkeypatch.setenv("VIOLET_LLM_PROVIDER", "claude")                                # chosen on the page
    assert isinstance(mp.apply({"model": "my-relay-model", "messages": msgs})["messages"][0]["content"], list)


def test_system_messages_after_the_conversation_began_go_as_user(monkeypatch):
    """--probe-cache on the user's relay: a system message after the history had 0 cache hits (the relay moves every
    system message to the front); the same text inside a user message was cached."""
    from ai import model_params as mp
    msgs = [{"role": "system", "content": "规则"}, {"role": "system", "content": "人设"},
            {"role": "user", "content": "昨天的话"}, {"role": "system", "content": "当前时间 20:01"},
            {"role": "user", "content": "今天好累"}]
    monkeypatch.setenv("VIOLET_LATE_SYSTEM_AS_USER", "1")
    sent = mp.apply({"model": "gpt-6-luna", "messages": msgs})["messages"]
    assert [m["role"] for m in sent] == ["system", "system", "user", "user", "user"]
    assert sent[3]["content"] == mp.LATE_SYSTEM_LABEL + "当前时间 20:01" and sent[:3] == msgs[:3]
    monkeypatch.setenv("VIOLET_LATE_SYSTEM_AS_USER", "0")
    assert mp.apply({"model": "gpt-6-luna", "messages": msgs})["messages"] == msgs


def test_a_relay_that_refuses_a_hint_never_gets_it_again(monkeypatch):
    import openai
    from ai import model_params as mp
    sent = []

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    sent.append(kw)
                    if "extra_body" in kw:
                        import httpx
                        response = httpx.Response(400, request=httpx.Request("POST", "http://127.0.0.1:9/v1"))
                        raise openai.BadRequestError("Unrecognized request argument: prompt_cache_key",
                                                     response=response, body=None)
                    return completion("好")
    request = {"model": "gpt-relay-x", "messages": [{"role": "user", "content": "hi"}], "cache_key": "k"}
    assert mp.create(Client, request).choices[0].message.content == "好"
    assert len(sent) == 2 and "extra_body" in sent[0] and "extra_body" not in sent[1]
    mp.create(Client, request)
    assert len(sent) == 3 and "extra_body" not in sent[2]                               # remembered


def test_chat_puts_this_turn_after_the_history(agents, violetdb):
    fake, a = agents
    violetdb.add_message("user", "incoming", "text", "昨天的话")
    violetdb.add_message("assistant", "outgoing", "text", "嗯嗯")
    _run(a, "今天好累")
    messages = fake.calls_for("fake-chat")[-1]["messages"]
    roles = [m["role"] for m in messages]
    first_history = roles.index("user")
    assert messages[-1] == {"role": "user", "content": "今天好累"}
    # the time (base section) is this turn's: after the history, the persona before it
    time_at = next(i for i, m in enumerate(messages) if m["role"] == "system" and "当前时间" in (m["content"] or ""))
    persona_at = next(i for i, m in enumerate(messages) if "人设" in str(m.get("content")))
    assert persona_at < first_history < time_at
    assert all("_cache_break" not in m for m in messages)


def test_two_different_turns_begin_the_same(agents, violetdb):
    """Everything before the history is the same from turn to turn (the provider's prompt cache serves it). The second
    server eval: "【你们的相处】第一次聊天在 …（1 分钟前）" sat in front and changed every call -> 0% cache."""
    fake, a = agents
    for text in ("今天好累", "发语音给我听听"):
        violetdb.execute("DELETE FROM messages")
        violetdb.add_message("user", "incoming", "text", "随便聊聊" + text)
        _run(a, text)
    first, second = fake.calls_for("fake-chat")[-2:]

    def front(call):
        out = []
        for m in call["messages"]:
            if m["role"] != "system":
                break
            out.append(m)
        return out
    assert front(first) == front(second) and len(front(first)) >= 3
    assert [t["function"]["name"] for t in first.get("tools") or []] == [t["function"]["name"] for t in second.get("tools") or []]


def test_the_history_window_keeps_its_start(monkeypatch, violetdb):
    from ai.context_engine import ContextEngine, _HISTORY_ANCHORS
    monkeypatch.setenv("VIOLET_CONTEXT_STABLE_HISTORY", "1")
    monkeypatch.setenv("VIOLET_CONTEXT_HISTORY_TURNS", "4")
    monkeypatch.setenv("VIOLET_CONTEXT_STYLE", "plain")
    _HISTORY_ANCHORS.clear()

    def say(i):
        violetdb.add_message(*(("user", "incoming") if i % 2 == 0 else ("assistant", "outgoing")), "text", f"话{i}")
    for i in range(6):
        say(i)
    engine = ContextEngine(violetdb)
    first = engine.history([])[0]["content"]
    for i in range(6, 9):                                                   # the window would slide by three
        say(i)
        assert engine.history([])[0]["content"] == first                    # ... but keeps its start
    for i in range(9, 14):
        say(i)
    assert engine.history([])[0]["content"] != first                        # twice the size: jumps ahead


def test_the_group_history_keeps_its_start(violetdb):
    from ai.social.engine import SocialEngine
    e = SocialEngine(type("R", (), {"db": violetdb, "channels": None})())
    rows = [{"id": i} for i in range(1, 31)]                               # the engine fetches 2 x size
    assert e._stepped(7, rows[-20:], 10)[0]["id"] == 21
    rows += [{"id": i} for i in range(31, 41)]
    assert e._stepped(7, rows[-20:], 10)[0]["id"] == 21                     # 20 lines now: same start
    rows += [{"id": 41}]
    assert e._stepped(7, rows[-20:], 10)[0]["id"] == 32                     # past twice the size: the last 10 again


def test_a_group_nobody_calls_her_in_is_looked_at_in_batches(violetdb, monkeypatch):
    from ai.social.engine import SocialEngine
    made = []

    class Timer:
        def __init__(self, delay, fn, args=()):
            self.delay, self.daemon = delay, True
            made.append(self)

        def start(self):
            pass

        def cancel(self):
            pass
    e = SocialEngine(type("R", (), {"db": violetdb, "channels": None})(), timer_factory=Timer)
    monkeypatch.setattr(e, "_hot", lambda cid: False)
    e.schedule(5)
    assert made[-1].delay == pytest.approx(180, abs=1)          # others talking: one look in three minutes
    e.schedule(5)
    assert made[-1].delay <= 180                                 # a new message does not push it later
    e.schedule(5, urgent=True)
    assert made[-1].delay <= 3                                   # called by name / @: at once
    monkeypatch.setattr(e, "_hot", lambda cid: True)
    e.schedule(6)
    assert made[-1].delay <= 6                                   # she is in the conversation: keeps up


@pytest.mark.parametrize("text, minutes, content", [
    ("十分钟后提醒我喝水", 10, "喝水"),
    ("半小时后提醒我收衣服", 30, "收衣服"),
    ("过两个小时提醒我吃药", 120, "吃药"),
    ("一个半小时以后提醒我出门", 90, "出门"),
    ("5分钟之后提醒我关火", 5, "关火"),
])
def test_relative_reminders(text, minutes, content):
    """2026-11-01 scenario eval: "十分钟后提醒我喝水" was refused (only 今天 / 明天 / 后天)."""
    from datetime import datetime, timedelta, timezone
    from ai.memory_model import parse_reminder_request
    now = datetime(2026, 11, 1, 19, 8, 30, tzinfo=timezone(timedelta(hours=8)))
    parsed = parse_reminder_request(text, now=now)
    assert parsed["ok"] and parsed["content"] == content
    assert datetime.fromisoformat(parsed["due_at"]) == (now + timedelta(minutes=minutes)).replace(second=0)


def test_unclear_relative_reminders_are_not_set():
    from ai.memory_model import parse_reminder_request
    assert parse_reminder_request("十分钟后，三点提醒我开会")["reason"] == "ambiguous_time"
    assert parse_reminder_request("十分钟后提醒我")["reason"] == "no_content"
    assert parse_reminder_request("明天下午三点提醒我开会")["ok"]                         # the old way still works


def test_a_repeated_search_in_one_turn_is_not_run():
    """The three queries the eval saw for "最近有什么科技新闻吗" (2026-11-01)."""
    from ai.web.tool_guard import WebToolGuard
    guard = WebToolGuard("最近有什么科技新闻吗")
    first = "近期 最新 科技新闻 2026年11月 科技 AI 芯片 苹果 谷歌 OpenAI 航天 科技报道"
    assert guard.check("violet-web__web_search", {"query": first}) is None
    again = guard.check("violet-web__web_search", {"query": "2026年11月 最新 科技新闻 AI 芯片 科技媒体"})
    assert json.loads(again)["error_code"] == "SAME_SEARCH"
    assert guard.check("violet-web__web_search", {"query": "杭州 明天 天气"}) is None     # something else: fine


def test_the_setting_exists_and_defaults_off():
    from ai.control_config import SETTINGS
    setting = next(s for s in SETTINGS if s.key == "VIOLET_INTENT_STEP")
    assert setting.default is False and setting.kind == "bool"
    assert json.dumps(setting.label, ensure_ascii=False)
