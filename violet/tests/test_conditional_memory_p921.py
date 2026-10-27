"""Phase 9.2.1: a conditional idea is not a plan (offline; fake SDK, isolated database, scenario clock).

p92-tools-1 #11: "周三下午要是没课，我想去西湖边走走" -- Chat failed (HTTP 502), the canned line went out, and MemoryAgent
stored a planned event for Wednesday 15:00 ("下午" -> its default clock). Now: plan / conditional / wish are told apart;
a conditional or wished event stays progress=unknown (no "upcoming plan" mention) until the user's own words settle
it; a condition that fails ends it; "下午" alone keeps only the day; a failed turn is marked for MemoryAgent."""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import openai
import pytest

from tests.test_long_term_memory_p91 import clock  # noqa: F401  (fixture: Monday 2026-11-02 20:00, Asia/Shanghai)
from tests.test_phase2_scenarios import make_world, say, script, tool  # noqa: F401  (make_world: fixture)

WEST_LAKE = "周三下午要是没课，我想去西湖边走走。帮我查查那天下午有没有课，天气适不适合出门？"
SUMMARY = "用户计划 2026-11-04（周三）下午若没课就去西湖边走走，并想确认那天下午是否有课、天气是否适合出门。"
# exactly what the real MemoryAgent wrote in p92-tools-1 #11 (no intent: it did not exist yet)
REAL_SAVE = dict(summary=SUMMARY, category="event", kind="event", progress="planned", event_date="2026-11-04",
                 event_clock="下午", importance=3, emotional_weight=2)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def relay_down(kw):
    raise openai.InternalServerError("Error code: 502 - upstream_error",
                                     response=httpx.Response(502, request=httpx.Request("POST", "http://relay.invalid/v1")),
                                     body=None)


def lake(w) -> dict:
    return next(m for m in w.db.list_event_memories() if "西湖" in m["content"])


def upcoming(w) -> list[int]:
    return [e["id"] for e in w.scheduler._mentionable_upcoming_events(set())]


@pytest.fixture
def lake_world(make_world, clock, monkeypatch):
    """The real turn #11: Chat fails three times, memory saves what the real model saved."""
    monkeypatch.setenv("LLM_RETRY_BASE_DELAY", "0")
    w = make_world()
    w.llm.responders["fake-chat"] = relay_down
    w.llm.responders["fake-memory"] = script(tool("save_memory", **REAL_SAVE))
    say(w, WEST_LAKE)
    w.llm.responders["fake-chat"] = lambda kw: "嗯嗯"
    return w


def test_the_real_turn_is_stored_as_a_conditional_idea_not_a_plan(lake_world, clock):
    w = lake_world
    assert w.client.sent_texts == ["我这边刚刚卡了一下，没接上你的话，你再说一次好不好"]
    memory = lake(w)
    assert memory["progress"] == "unknown" and memory["intent_level"] == "conditional"
    assert memory["intent_condition"] == "没课"
    assert memory["event_time"] == "2026-11-04"                     # "下午" is not 15:00; the word stays in the text
    assert "下午" in memory["content"]
    memory_request = json.dumps(w.llm.calls_for("fake-memory")[0]["messages"], ensure_ascii=False)
    assert "系统兜底消息" in memory_request and "不要据此推断" in memory_request
    clock.advance_to(at("2026-11-04T09:00:00+08:00"))               # the day itself: still nothing to "wish luck" for
    assert memory["id"] not in upcoming(w)
    from ai.chat import format_recalled_memories
    assert "有条件的打算，还没定（条件：没课）" in format_recalled_memories([lake(w)])


def test_nothing_but_the_users_own_words_settles_the_condition(lake_world, clock):
    w = lake_world
    memory_id = lake(w)["id"]
    # the model "decides" it is on, quoting Violet / nothing -- refused
    w.llm.responders["fake-memory"] = script(
        tool("update_event_progress", memory_id=memory_id, progress="planned", summary=SUMMARY,
             evidence="Violet 说周三下午没课"),
        tool("save_memory", **{**REAL_SAVE, "related_to": memory_id, "intent": "plan"}))
    say(w, "今天好累啊")
    assert lake(w)["progress"] == "unknown" and lake(w)["intent_level"] == "conditional"
    # the user says it is settled -> a plan, condition gone, Violet may mention it before it happens
    w.llm.responders["fake-memory"] = script(
        tool("update_event_progress", memory_id=memory_id, progress="planned",
             summary="用户 2026-11-04（周三）下午去西湖边走走", evidence="周三下午确实没课，那就定了去西湖"))
    say(w, "周三下午确实没课，那就定了去西湖！")
    memory = lake(w)
    assert (memory["progress"], memory["intent_level"], memory["intent_condition"]) == ("planned", "plan", None)
    clock.advance_to(at("2026-11-04T09:00:00+08:00"))
    assert memory_id in upcoming(w)


def test_a_condition_that_fails_ends_it(lake_world, clock):
    w = lake_world
    memory_id = lake(w)["id"]
    w.llm.responders["fake-memory"] = script(
        tool("update_event_progress", memory_id=memory_id, progress="cancelled", reason="周三下午有课",
             evidence="周三下午有体育课，去不了了"))
    say(w, "周三下午有体育课，去不了了")
    assert lake(w)["progress"] == "cancelled"
    clock.advance_to(at("2026-11-04T09:00:00+08:00"))
    assert memory_id not in upcoming(w)


@pytest.mark.parametrize("words,args,expected", [
    # a settled plan stays a plan; the clock the user said is kept
    ("周三下午三点我去西湖", dict(event_date="2026-11-04", event_clock="15:00", intent="plan"), ("planned", "plan", "15:00")),
    # "下午" alone: the day only (was 15:00)
    ("周三下午我去西湖", dict(event_date="2026-11-04", event_clock="下午", intent="plan"), ("planned", "plan", None)),
    # the model wrote the default clock itself as an absolute time
    ("周三下午我去西湖", dict(event_time="2026-11-04T15:00:00+08:00"), ("planned", None, None)),
    # a passing wish is not a plan
    ("好想去西湖玩啊", dict(event_date="2026-11-04", intent="wish"), ("unknown", "wish", None)),
    # a condition the model named itself, with the condition it wrote
    ("周三要是不下雨我就去西湖", dict(event_date="2026-11-04", intent="conditional", condition="周三不下雨"),
     ("unknown", "conditional", None)),
])
def test_plan_condition_and_wish_are_told_apart(make_world, clock, words, args, expected):
    w = make_world()
    w.llm.responders["fake-memory"] = script(tool("save_memory", summary=f"用户说：{words}", category="event",
                                                  kind="event", progress="planned", **args))
    say(w, words)
    memory = lake(w)
    progress, level, clock_time = expected
    assert memory["progress"] == progress and memory["intent_level"] == level
    if clock_time:
        assert memory["event_time"].startswith(f"2026-11-04T{clock_time}")
    else:
        assert memory["event_time"] == "2026-11-04"
    if level == "conditional":
        assert memory["intent_condition"] == "周三不下雨"
    clock.advance_to(at("2026-11-04T09:00:00+08:00"))
    assert (memory["id"] in upcoming(w)) == (progress == "planned")


def test_an_ordinary_plan_is_untouched_and_older_rows_need_no_rewrite(make_world, clock):
    from ai.memory_model import resolve_event_time
    w = make_world()
    w.llm.responders["fake-memory"] = script(tool("save_memory", summary="用户明天上午十点有面试，很紧张", category="event",
                                                  kind="event", progress="planned", event_date="tomorrow",
                                                  event_clock="10:00"))
    say(w, "我明天上午十点有面试，好紧张")
    interview = w.db.list_event_memories()[0]
    assert interview["progress"] == "planned" and interview["intent_level"] is None
    assert interview["event_time"].startswith("2026-11-03T10:00")
    assert resolve_event_time(None, "2026-11-04", "晚上") == "2026-11-04"
    assert resolve_event_time(None, "2026-11-04", "20:30").startswith("2026-11-04T20:30")
