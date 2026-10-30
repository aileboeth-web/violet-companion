"""Phase 7: the 32-round acceptance dialogue, run offline through the real runtime.

What is checked here is everything that does not depend on a real model:
what Violet's chat model receives each round (context sections, tools), what the
scripted memory actions store, and what the proactive scheduler may do. The
naturalness rubric in tests/samples/acceptance_dialogue_30.json is for the
later real-model run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fakes import completion, intent_json
from tests.image_fixtures import config as image_config, only, write
from tests.test_persona_life import happened, new_thread
from tests.test_phase2_scenarios import arm, make_world, say, script, tool  # noqa: F401
from tests.test_phase3_scenarios import age_all

from tools.legacy import upgrade  # noqa: E402  (the plan was written before the rename to Violet)

PLAN = upgrade(json.loads((Path(__file__).parent / "samples" / "acceptance_dialogue_30.json").read_text(encoding="utf-8")))


def system_of(call):
    return "\n".join(m.get("content") or "" for m in call["messages"] if m["role"] == "system")


def last_user_call(w):
    return next(c for c in reversed(w.llm.calls_for("fake-chat"))
                if not any("主动触达" in (m.get("content") or "") for m in c["messages"] if m["role"] == "user"))


@pytest.fixture()
def dialogue_world(make_world, tmp_path, monkeypatch):
    monkeypatch.setenv("VIOLET_IMAGE_SELF_DECIDE", "0")     # the plan's rounds check the "only when asked" image gate
    from ai.images import adapters, router
    from ai.life_store import LifeStore
    from ai.web import mcp_server
    from ai.web.providers import MockSearchProvider
    from ai.web.service import WebService
    from tests.test_web_security import make_fetcher
    from tests.web_fixtures import LocalWeb
    adapters.MockImageAdapter.calls.clear()
    router.reset_runtime_state()
    web = LocalWeb()
    monkeypatch.setattr(mcp_server, "build_default_server", lambda: mcp_server.build_server(
        WebService(__import__("ai.chat", fromlist=["x"]).violetdb, fetcher=make_fetcher(web), provider=MockSearchProvider())))
    monkeypatch.setenv("VIOLET_IMAGE_PROVIDERS_FILE", str(write(only(image_config(), "mockA/img-1"), tmp_path / "ip.json")))
    # phase 9.3: web tools default to "on_demand" (Intent plans them); this 32-round script was written for the
    # phase 5B behaviour (offered every turn), which VIOLET_WEB_OFFER=always keeps
    w = make_world(VIOLET_IMAGE_ENABLED="1", VIOLET_WEB_ENABLED="1", VIOLET_WEB_OFFER="always")
    store = LifeStore(w.db)
    happened(store, PLAN["preload"]["life_event"]["content"], hours_ago=PLAN["preload"]["life_event"]["hours_ago"],
             thread=new_thread(store))
    yield w
    web.close()


def memory_steps(w, steps):
    resolved = []
    for step in steps or []:
        args = dict(step["args"])
        if step["tool"] == "update_event_progress":
            key = args.pop("memory")
            row = w.db.fetch_one("SELECT id FROM event_memories WHERE event_key = ?", (key,))
            args["memory_id"] = row["id"]
        resolved.append(tool(step["tool"], **args))
    if resolved:
        w.llm.responders["fake-memory"] = script(*resolved)
    else:
        w.llm.responders["fake-memory"] = lambda kw: json.dumps({"should_save": False, "reason": "t"})


def chat_reply(w, rnd):
    if rnd.get("image"):
        def respond(kw):
            if not any(m["role"] == "tool" for m in kw["messages"]):
                return completion(tool_calls=[{"name": "create_image", "arguments": {"subject": "violet", "scene": "current"}}])
            marker = json.loads(next(m for m in reversed(kw["messages"]) if m["role"] == "tool")["content"])["image_marker"]
            return f"画了一张给你\n{marker}"
        w.llm.responders["fake-chat"] = respond
    else:
        w.llm.responders["fake-chat"] = lambda kw: "嗯嗯"


def proactive(w, expectation):
    if expectation == "ended_event_not_allowed":
        state = w.scheduler._conversation_state()
        ended = w.db.fetch_one("SELECT id FROM event_memories WHERE event_key = 'course_defense'")["id"]
        assert ended not in [m["id"] for m in state["allowed_memories"]]
        assert ended in [m["memory_id"] for m in state["restricted_memories"]]
        return
    calls = len(w.llm.calls_for("fake-proactive"))
    if expectation == "blocked_busy":
        age_all(w.db, 1.6)        # past the 90-minute gap after the user's message, still inside the 2-hour meeting
    arm(w)
    w.scheduler.tick()
    reason = w.scheduler.status_snapshot()["due_reason"]
    if expectation == "blocked_busy":
        assert len(w.llm.calls_for("fake-proactive")) == calls and reason.startswith("the user said they are busy")
    elif expectation == "allowed_or_skipped_without_bypass":
        sent_today = w.db.fetch_one("SELECT COUNT(*) AS c FROM proactive_events WHERE status = 'sent'")["c"]
        assert sent_today <= 3


def test_32_round_acceptance_dialogue(dialogue_world):
    w = dialogue_world
    assert len(PLAN["rounds"]) >= 30
    questions_offered = 0
    for rnd in PLAN["rounds"]:
        if rnd.get("advance_hours"):
            age_all(w.db, rnd["advance_hours"])
        before = rnd.get("before") or {}
        if "proactive_state" in before:
            proactive(w, before["proactive_state"])
        if "proactive_tick" in before:
            proactive(w, before["proactive_tick"])
        memory_steps(w, rnd.get("memory"))
        if rnd.get("intent_query"):
            # recall needs the intent model to search memory (no code-level fallback: report R-M2)
            w.llm.responders["fake-intent"] = script(tool("query_memory", query=rnd["intent_query"]),
                                                     final=intent_json("chat"))
        else:
            w.llm.responders["fake-intent"] = lambda kw: intent_json("chat")
        chat_reply(w, rnd)
        sent_before = len(w.client.sent)
        say(w, rnd["user"])
        assert len(w.client.sent) > sent_before, f"round {rnd['n']}: no reply"
        call = last_user_call(w)
        system = system_of(call)
        tools = {t["function"]["name"] for t in call.get("tools") or []}
        questions_offered += "可选的了解" in system
        checks = rnd.get("checks") or {}
        for text in checks.get("context_has", []):
            assert text in system, f"round {rnd['n']}: context lacks {text!r}"
        for text in checks.get("context_lacks", []):
            assert text not in system, f"round {rnd['n']}: context should not contain {text!r}"
        for name in checks.get("tools_has", []):
            assert name in tools, f"round {rnd['n']}: tool {name} not offered"
        for name in checks.get("tools_lacks", []):
            assert name not in tools, f"round {rnd['n']}: tool {name} offered"
        if checks.get("db") == "violet_picture_sent":
            asset = w.db.fetch_one("SELECT * FROM media_assets ORDER BY id DESC")
            assert asset["send_status"] == "sent" and asset["ai_generated"] == 1 and asset["caption"].startswith("AI 生成")
        if checks.get("db") == "commitment_created":
            assert w.db.fetch_one("SELECT content FROM commitments")["content"] == "交作业"
        after = rnd.get("after") or {}
        if "proactive_tick" in after:
            proactive(w, after["proactive_tick"])
    # whole-dialogue invariants
    assert questions_offered <= 2, f"onboarding offered {questions_offered} times"
    users = w.db.fetch_all("SELECT content FROM messages WHERE role = 'user' ORDER BY id")
    assert [u["content"] for u in users] == [r["user"] for r in PLAN["rounds"]]          # nothing lost or doubled
    assert {r["status"] for r in w.db.fetch_all("SELECT status FROM inbound_messages")} == {"done"}
    assert w.db.fetch_one("SELECT COUNT(*) AS c FROM life_events WHERE event_type != 'reading_record'")["c"] == 1
    facts = {f["fact_key"]: f["value"] for f in w.db.fetch_all("SELECT * FROM user_profile_facts WHERE status = 'active'")}
    assert facts["study.major"] == "物理" and facts["address_name"] == "小满"
