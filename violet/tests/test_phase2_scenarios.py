"""Phase 2 replay scenarios (all offline, fake WeChat + fake LLM).

The fake decision model is deliberately "naive": it tries what the old system
did (follow up on open events, repeat a topic). The scenarios check that the
code -- not the model's goodwill -- prevents it, and they check behaviour,
state, message content and database records.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.fakes import far_quiet_hours, FakeILinkClient, FakeLLM, completion, default_responders, install_fake_llm, intent_json
from tests.mcp_test_server import closed_port_url

FINAL_MEMORY = json.dumps({"should_save": True, "reason": "ok"})


# ---------------------------------------------------------------------------
# World: runtime + scheduler + fakes
# ---------------------------------------------------------------------------

@pytest.fixture()
def make_world(monkeypatch, violetdb, tmp_path):
    worlds = []

    def build(**env):
        monkeypatch.setenv("DEBOUNCE_NORMAL", "0.05")
        monkeypatch.setenv("DEBOUNCE_EXTRA", "0.1")
        monkeypatch.setenv("VIOLET_MEMORY_RETRY_BASE_SECONDS", "0")
        monkeypatch.setenv("VIOLET_SEND_RETRY_DELAY_SECONDS", "0")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        llm = install_fake_llm(monkeypatch, FakeLLM(default_responders("嗯嗯")))
        from ai.agents import ChatAgent, IntentAgent, MemoryAgent, PerformerAgent, ProactiveAgent
        from ai.proactive_scheduler import ProactiveScheduler
        from violet_runtime import VioletRuntime, MessageSender
        client = FakeILinkClient()
        agents = {"intent": IntentAgent(), "chat": ChatAgent(), "memory": MemoryAgent(), "performer": PerformerAgent()}
        config = tmp_path / f"proactive-{len(worlds)}.json"
        config.write_text(json.dumps({
            "enabled": True, "quiet_hours": far_quiet_hours(), "daily_message_limit": 3,
            "min_minutes_after_user_message": 90, "min_minutes_between_proactive_messages": 180,
            "random_delay_minutes": {k: [60, 60] for k in ("morning", "noon", "afternoon", "evening", "night", "other")},
            "allowed_tasks": {},
        }), encoding="utf-8")
        holder = {}
        scheduler = ProactiveScheduler(ProactiveAgent(), agents["chat"], agents["performer"],
                                       send_messages=lambda m, e=None: holder["rt"].send_proactive(m, e),
                                       config_path=config)
        sender = MessageSender(client, violetdb, simulate_typing=False, sleep=lambda s: None)
        rt = VioletRuntime(client, violetdb, agents, proactive_scheduler=scheduler, sender=sender,
                        vision=lambda path: "图片")
        holder["rt"] = rt
        rt.start_worker()
        world = SimpleNamespace(rt=rt, client=client, llm=llm, db=violetdb, scheduler=scheduler, agents=agents)
        worlds.append(world)
        return world

    yield build
    for world in worlds:
        world.rt.stop(timeout=2)


def say(w, text):
    w.rt.ingest_text(text)
    assert w.rt.wait_idle()
    assert w.rt.wait_memory_idle()


def tool(name, **arguments):
    return completion(tool_calls=[{"name": name, "arguments": arguments}])


def script(*steps, final=FINAL_MEMORY):
    """Responder that plays steps in order (callables get the request), then `final`."""
    queue = list(steps)

    def respond(kw):
        if not queue:
            return final
        step = queue.pop(0)
        if callable(step):
            return step(kw)
        if isinstance(step, BaseException):
            raise step
        return step
    return respond


def last_tool_result(kw):
    return json.loads(next(m for m in reversed(kw["messages"]) if m["role"] == "tool")["content"])


def age_everything(db, hours):
    """Move the whole recorded history `hours` into the past."""
    columns = {
        "messages": ["created_at"],
        "event_memories": ["created_at", "event_time", "valid_until", "progress_updated_at", "last_user_mention_at"],
        "proactive_events": ["created_at", "updated_at"],
        "topic_mentions": ["created_at"],
        "commitments": ["created_at", "due_at", "next_attempt_at"],
        "inbound_messages": ["received_at"],
    }
    for table, cols in columns.items():
        for row in db.fetch_all(f"SELECT id, {', '.join(cols)} FROM {table}"):
            updates = {}
            for col in cols:
                if row[col]:
                    updates[col] = (datetime.fromisoformat(row[col]) - timedelta(hours=hours)).isoformat()
            if updates:
                sets = ", ".join(f"{c} = ?" for c in updates)
                db.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*updates.values(), row["id"]))
    last = db.get_proactive_setting("last_engaged_user_turn_at")
    if last:
        db.set_proactive_setting("last_engaged_user_turn_at", (datetime.fromisoformat(last) - timedelta(hours=hours)).isoformat())


def arm(w):
    w.db.set_proactive_setting("next_proactive_at", (datetime.fromisoformat(w.db.now()) - timedelta(minutes=1)).isoformat())


def decide(w, **decision):
    payload = {"should_act": True, "action_type": "chat", "reason": "test", "mentioned_memory_ids": [], **decision}
    w.llm.responders["fake-proactive"] = lambda kw: json.dumps(payload, ensure_ascii=False)


def is_proactive_chat(kw):
    return any("主动触达" in (m.get("content") or "") for m in kw["messages"] if m["role"] == "user")


def chat(w, normal="嗯嗯", proactive=None, reminder=None):
    def respond(kw):
        system = "\n".join(m.get("content") or "" for m in kw["messages"] if m["role"] == "system")
        if reminder is not None and "要提醒的事" in system:
            return reminder(kw) if callable(reminder) else reminder
        if proactive is not None and is_proactive_chat(kw):
            return proactive(kw) if callable(proactive) else proactive
        return normal
    w.llm.responders["fake-chat"] = respond


def latest_event(w):
    return w.db.list_proactive_events(limit=1)[0]


def server_outage_turn(w):
    w.llm.responders["fake-memory"] = script(
        tool("save_memory", summary="用户的服务器出现故障", category="event", kind="event",
             progress="ongoing", event_key="server_outage", event_date="today"))
    say(w, "我服务器挂了，烦死了")
    rows = w.db.list_event_memories()
    assert len(rows) == 1
    return rows[0]


# ---------------------------------------------------------------------------
# 1. The user mentioned a server outage, then says nothing about it for days
# ---------------------------------------------------------------------------

def test_s1_open_event_is_not_followed_up_days_later(make_world):
    w = make_world()
    memory = server_outage_turn(w)
    assert (memory["kind"], memory["progress"], memory["event_key"]) == ("event", "ongoing", "server_outage")
    assert memory["event_time"].startswith(w.db.now()[:10]) and memory["valid_until"]
    assert w.db.list_commitments() == []                                   # no follow-up task was created

    age_everything(w.db, 72)
    replies_before = list(w.client.sent_texts)

    # (a) the decision model tries the old behaviour: follow up on the server
    decide(w, topic_key="server_followup", mentioned_memory_ids=[memory["id"]], chat_brief={"topic": "问服务器修好没"})
    arm(w)
    w.scheduler.tick()
    assert w.client.sent_texts == replies_before
    event = latest_event(w)
    assert event["status"] == "skipped" and f"memory {memory['id']} may not be raised" in event["reason"]
    proactive_input = w.llm.calls_for("fake-proactive")[-1]["messages"][-1]["content"]
    assert "用户的服务器出现故障" in json.loads(proactive_input)["conversation_state"]["restricted_memories"][0]["content"]
    assert w.db.get_event_memory(memory["id"])["progress"] == "ongoing"    # 3 days later: not assumed done

    # (b) same intent reworded under a generic topic: the generated text is blocked
    decide(w, topic_key="evening_chat", chat_brief={"topic": "晚上随便聊聊"})
    chat(w, proactive="对了你那个服务器修好了没")
    arm(w)
    w.scheduler.tick()
    assert w.client.sent_texts == replies_before
    assert "touches restricted topic" in latest_event(w)["reason"]

    # (c) ordinary small talk is still possible
    chat(w, proactive="晚饭吃了没")
    arm(w)
    w.scheduler.tick()
    assert w.client.sent_texts == replies_before + ["晚饭吃了没"]
    assert latest_event(w)["status"] == "sent"
    mentions = w.db.list_topic_mentions(sources=["violet_proactive"])
    assert [m["topic_key"] for m in mentions] == ["evening_chat"]
    assert w.db.fetch_one("SELECT COUNT(*) AS c FROM outbound_messages WHERE origin = 'proactive'")["c"] == 1


# ---------------------------------------------------------------------------
# 2. The user explicitly says the server is fixed
# ---------------------------------------------------------------------------

def test_s2_explicit_resolution_updates_the_same_event(make_world):
    w = make_world()
    memory = server_outage_turn(w)

    def link_progress(kw):
        result = last_tool_result(kw)
        assert result["action"] == "needs_decision"                         # overlap -> question, not merge
        return tool("update_event_progress", memory_id=result["candidates"][0]["id"], progress="done",
                    summary="用户的服务器故障已经修好了", reason="用户说：服务器已经修好了")

    w.llm.responders["fake-memory"] = script(
        tool("query_memory", query="服务器 故障"),
        tool("save_memory", summary="用户的服务器已经修好了", category="event", progress="done"),
        link_progress)
    say(w, "服务器已经修好了！")

    rows = w.db.list_event_memories()
    assert len(rows) == 1, "no duplicate 'fixed' memory next to an 'unresolved' one"
    updated = rows[0]
    assert (updated["id"], updated["progress"], updated["content"]) == (memory["id"], "done", "用户的服务器故障已经修好了")
    revision = w.db.list_memory_revisions("event_memories", memory["id"])[-1]
    assert (revision["old_progress"], revision["new_progress"], revision["old_content"]) == ("ongoing", "done", "用户的服务器出现故障")
    assert w.db.list_topic_mentions(memory_id=memory["id"], sources=["user"])          # user re-raised it

    # next conversation: Violet's context shows the event as ended
    w.llm.responders["fake-intent"] = script(tool("query_memory", query="服务器"), final=intent_json("chat"))
    say(w, "服务器这次真稳")
    chat_call = w.llm.calls_for("fake-chat")[-1]
    recall = next(m["content"] for m in chat_call["messages"] if "相关记忆" in (m.get("content") or ""))
    line = next(l for l in recall.splitlines() if "用户的服务器故障已经修好了" in l)
    assert "[事件 / 已结束" in line and "用户的服务器出现故障" not in recall


# ---------------------------------------------------------------------------
# 3. Violet keeps getting brief replies
# ---------------------------------------------------------------------------

def test_s3_brief_replies_cool_the_topic_and_slow_eve_down(make_world):
    w = make_world()
    say(w, "早")
    age_everything(w.db, 3)
    decide(w, topic_key="daily_checkin", chat_brief={"topic": "问问今天忙不忙"})
    chat(w, proactive="今天忙不忙呀")

    arm(w)
    w.scheduler.tick()
    assert w.client.sent_texts[-1] == "今天忙不忙呀"
    say(w, "嗯")
    next_at = datetime.fromisoformat(w.db.get_proactive_setting("next_proactive_at"))
    gap = (next_at - datetime.fromisoformat(w.db.now())).total_seconds() / 60
    assert 118 <= gap <= 121, "one unengaged proactive message -> interval x2"

    age_everything(w.db, 4)
    arm(w)
    w.scheduler.tick()
    assert w.client.sent_texts[-1] == "今天忙不忙呀"
    say(w, "哦")
    next_at = datetime.fromisoformat(w.db.get_proactive_setting("next_proactive_at"))
    assert (next_at - datetime.fromisoformat(w.db.now())).total_seconds() / 60 >= 238   # x4

    engaged = [m["user_engaged"] for m in w.db.list_topic_mentions(topic_key="daily_checkin")]
    assert engaged == [0, 0]
    assert w.scheduler.tracker.topic_state("daily_checkin")["cooling"]

    # Violet stops generic proactive chat until the user engages again
    age_everything(w.db, 6)
    calls = len(w.llm.calls_for("fake-proactive"))
    sent = len(w.client.sent_texts)
    arm(w)
    w.scheduler.tick()
    assert len(w.llm.calls_for("fake-proactive")) == calls and len(w.client.sent_texts) == sent
    assert w.scheduler.status_snapshot()["due_reason"] == "waiting for the user to re-engage (cold streak 2)"

    # the user starts a real conversation: proactive chat resumes, but not the cooled topic
    say(w, "今天去爬山了，累死了但是好开心")
    age_everything(w.db, 3)
    arm(w)
    w.scheduler.tick()                                   # model still wants "daily_checkin"
    assert "cooling" in latest_event(w)["reason"] and len(w.client.sent_texts) == sent + 1   # +1 = chat reply
    decide(w, topic_key="hiking", chat_brief={"topic": "聊爬山"})
    chat(w, proactive="爬的哪座山")
    arm(w)
    w.scheduler.tick()
    assert w.client.sent_texts[-1] == "爬的哪座山"


# ---------------------------------------------------------------------------
# 4. The user brings a cooled topic back
# ---------------------------------------------------------------------------

def test_s4_user_restarting_a_cooled_topic_is_continued_naturally(make_world):
    w = make_world()
    guitar = w.db.save_event_memory("用户在学吉他", kind="fact")["memory"]
    say(w, "早")
    decide(w, topic_key="guitar", mentioned_memory_ids=[guitar["id"]], chat_brief={"topic": "聊吉他"})
    chat(w, proactive="吉他练得怎么样啦")
    for reply in ("嗯", "还行"):
        age_everything(w.db, 4)
        arm(w)
        w.scheduler.tick()
        assert w.client.sent_texts[-1] == "吉他练得怎么样啦"
        say(w, reply)
    tracker = w.scheduler.tracker
    assert tracker.topic_state(f"memory:{guitar['id']}")["cooling"]
    state = w.scheduler._conversation_state()
    assert guitar["id"] not in [m["id"] for m in state["allowed_memories"]]

    # the user brings it up again
    w.llm.responders["fake-intent"] = script(tool("query_memory", query="吉他"), final=intent_json("chat"))
    w.llm.responders["fake-memory"] = script(tool("reinforce_memory", memory_id=guitar["id"], reason="用户主动提起吉他"))
    chat(w, normal="哇，哪首")
    say(w, "对了我终于把那首吉他曲练会了！")
    chat_call = w.llm.calls_for("fake-chat")[-1]
    system = "\n".join(m.get("content") or "" for m in chat_call["messages"] if m["role"] == "system")
    assert "用户在学吉他" in system                                     # recalled for this turn
    assert "不适合由你主动提起" not in system or "用户在学吉他" not in system.split("不适合由你主动提起")[1]
    assert w.client.sent_texts[-1] == "哇，哪首"
    assert not tracker.topic_state(f"memory:{guitar['id']}")["cooling"]   # reset immediately
    assert guitar["id"] in [m["id"] for m in w.scheduler._conversation_state()["allowed_memories"]]


# ---------------------------------------------------------------------------
# 5. The user explicitly asks to be reminded the next day
# ---------------------------------------------------------------------------

def test_s5_explicit_reminder_is_delivered_once_despite_chat_limits(make_world):
    w = make_world()
    w.llm.responders["fake-memory"] = script(
        tool("create_commitment", content="交实验报告", evidence="明天早上九点提醒我交实验报告",
             due_date="tomorrow", due_clock="09:00"))
    chat(w, normal="好，明天叫你", reminder="九点啦，该交实验报告了")
    say(w, "明天早上九点提醒我交实验报告")
    commitment = w.db.list_commitments()[0]
    tomorrow = (datetime.fromisoformat(w.db.now()) + timedelta(days=1)).date().isoformat()
    assert (commitment["status"], commitment["due_at"]) == ("pending", f"{tomorrow}T09:00:00+08:00")
    assert commitment["evidence"] == "明天早上九点提醒我交实验报告"

    # it is "tomorrow 09:00"; the chat budget is used up and Violet is in a cold streak
    past = (datetime.fromisoformat(w.db.now()) - timedelta(minutes=1)).isoformat()
    w.db.update_commitment(commitment["id"], due_at=past, next_attempt_at=past)
    for _ in range(3):
        w.db.add_proactive_event("scheduled_proactive_check", status="sent", action_type="chat")
    w.scheduler.tick()
    assert w.client.sent_texts[-1] == "九点啦，该交实验报告了"
    delivered = w.db.get_commitment(commitment["id"])
    assert delivered["status"] == "sent" and delivered["delivered_at"]
    event = w.db.get_proactive_event(delivered["proactive_event_id"])
    assert (event["event_type"], event["status"]) == ("commitment_reminder", "sent")
    reminder_call = w.llm.calls_for("fake-chat")[-1]
    assert any("要提醒的事：交实验报告" in (m.get("content") or "") for m in reminder_call["messages"])

    sent = len(w.client.sent_texts)
    arm(w)
    w.scheduler.tick()
    w.scheduler.tick()
    assert len(w.client.sent_texts) == sent                       # never twice; chat still capped
    assert w.db.fetch_one("SELECT COUNT(*) AS c FROM outbound_messages WHERE content = '九点啦，该交实验报告了'")["c"] == 1


# ---------------------------------------------------------------------------
# 6. The user shares something without asking for a reminder
# ---------------------------------------------------------------------------

def test_s6_shared_event_without_request_creates_no_follow_up(make_world):
    w = make_world()
    w.llm.responders["fake-memory"] = script(
        tool("create_commitment", content="问面试结果", evidence="我今天去面试了", due_date="tomorrow"),
        lambda kw: (last_tool_result(kw)["action"] == "rejected"
                    and tool("save_memory", summary="用户今天去参加了面试", category="event", progress="ongoing")),
    )
    say(w, "我今天去面试了，有点紧张")
    memory_call = w.llm.calls_for("fake-memory")
    rejection = json.loads([m for m in memory_call[1]["messages"] if m["role"] == "tool"][0]["content"])
    assert (rejection["action"], rejection["reason"]) == ("rejected", "not_an_explicit_request")
    assert w.db.list_commitments() == []
    interview = w.db.list_event_memories()[0]
    assert (interview["content"], interview["kind"], interview["progress"]) == ("用户今天去参加了面试", "event", "ongoing")
    state = w.scheduler._conversation_state()
    assert interview["id"] in [m["memory_id"] for m in state["restricted_memories"]]
    assert interview["id"] not in [m["id"] for m in state["allowed_memories"]]


# ---------------------------------------------------------------------------
# 7. The scheduler fires many times in a row
# ---------------------------------------------------------------------------

def test_s7_repeated_ticks_do_not_produce_repeated_messages(make_world):
    w = make_world()
    say(w, "早")
    age_everything(w.db, 3)
    decide(w, topic_key="small_talk", chat_brief={"topic": "闲聊"})
    chat(w, proactive="在干嘛")
    for _ in range(6):
        arm(w)
        w.scheduler.tick()
    assert w.client.sent_texts.count("在干嘛") == 1
    assert len(w.llm.calls_for("fake-proactive")) == 1             # later ticks never reached the model
    assert w.scheduler.status_snapshot()["due_reason"] == "too soon after proactive message"

    # "nothing worth saying" is a normal outcome, from either model
    age_everything(w.db, 4)
    chat(w, proactive="[NO_REPLY]")
    arm(w)
    w.scheduler.tick()
    assert latest_event(w)["status"] == "skipped" and latest_event(w)["reason"] == "ChatAgent decided not to send"
    w.llm.responders["fake-proactive"] = lambda kw: json.dumps({"should_act": False, "action_type": "none", "reason": "没什么要说的"})
    arm(w)
    w.scheduler.tick()
    assert latest_event(w)["status"] == "skipped"
    assert w.client.sent_texts.count("在干嘛") == 1 and "[NO_REPLY]" not in w.client.sent_texts
    statuses = [e["status"] for e in w.db.list_proactive_events()]
    assert statuses.count("sent") == 1


# ---------------------------------------------------------------------------
# 8. MCP is offline during a normal proactive chat
# ---------------------------------------------------------------------------

def test_s8_proactive_chat_works_with_mcp_offline(make_world):
    from ai import mcp as mcp_module
    w = make_world(MCP_SERVERS=json.dumps([{"name": "android-phone", "transport": "streamable_http",
                                            "url": closed_port_url(), "headers": {"Authorization": "Bearer x"}}]),
                   MCP_CONNECT_TIMEOUT_SECONDS="2")
    say(w, "早")
    age_everything(w.db, 3)
    decide(w, topic_key="small_talk", chat_brief={"topic": "闲聊"})
    chat(w, proactive="下午好呀")
    arm(w)
    w.scheduler.tick()
    assert w.client.sent_texts[-1] == "下午好呀"
    assert latest_event(w)["status"] == "sent"
    health = {h["server"]: h for h in mcp_module.get_health_snapshot()}
    assert health["android-phone"]["total_failures"] >= 1 and health["android-phone"]["last_error_code"] == "MCP_CONNECT_FAILED"
    assert w.llm.calls_for("fake-chat")[-1].get("tools") is None


# ---------------------------------------------------------------------------
# 9. A memory write fails and is processed again
# ---------------------------------------------------------------------------

def test_s9_failed_memory_job_is_retried_without_duplicates(make_world):
    w = make_world()
    save = tool("save_memory", summary="用户下周三搬宿舍", category="event", progress="planned",
                event_date=(datetime.fromisoformat(w.db.now()) + timedelta(days=5)).date().isoformat())
    w.llm.responders["fake-memory"] = script(save, RuntimeError("relay 502 bad gateway"), save)
    say(w, "下周三要搬宿舍了")
    assert w.client.sent_texts == ["嗯嗯"]                                   # reply not blocked
    job = w.db.list_memory_jobs()[0]
    assert (job["status"], job["attempts"]) == ("done", 2)
    assert "relay 502" in job["last_error"]
    assert job["assistant_reply"] == "嗯嗯"
    rows = w.db.list_event_memories()
    assert [r["content"] for r in rows] == ["用户下周三搬宿舍"]                 # retry did not duplicate
    assert rows[0]["mention_count"] == 1                                    # nor count as a re-mention

    # crash mid-job: a 'running' job is recovered at startup and processed
    w.db.add_memory_job("crashed-turn", "我换了新手机", None, status="pending")
    crashed = w.db.get_memory_job_by_turn("crashed-turn")
    w.db.update_memory_job(crashed["id"], status="running")
    w.llm.responders["fake-memory"] = script(tool("save_memory", summary="用户换了新手机", category="event", kind="fact"))
    w.rt.recover_pending()
    assert w.rt.wait_memory_idle()
    assert w.db.get_memory_job(crashed["id"])["status"] == "done"
    assert "用户换了新手机" in [r["content"] for r in w.db.list_event_memories()]


def test_s9b_permanently_failing_memory_job_is_marked_failed(make_world):
    w = make_world(VIOLET_MEMORY_MAX_ATTEMPTS="2")
    w.llm.responders["fake-memory"] = lambda kw: (_ for _ in ()).throw(RuntimeError("memory model down"))
    say(w, "今天好累")
    job = w.db.list_memory_jobs()[0]
    assert (job["status"], job["attempts"]) == ("failed", 2) and "memory model down" in job["last_error"]
    assert w.client.sent_texts == ["嗯嗯"]
    assert w.db.fetch_one("SELECT status FROM inbound_messages")["status"] == "done"
    assert w.rt.health()["memory_jobs"] == {"failed": 1}


# ---------------------------------------------------------------------------
# 10. The user corrects something Violet remembered wrong
# ---------------------------------------------------------------------------

def test_s10_user_correction_replaces_the_wrong_memory(make_world):
    w = make_world()
    cat = w.db.save_event_memory("用户养了一只猫", kind="fact", confidence=0.7)["memory"]
    w.llm.responders["fake-memory"] = script(
        tool("query_memory", query="猫 宠物"),
        lambda kw: tool("correct_memory", memory_id=last_tool_result(kw)["memories"][0]["id"],
                        corrected_summary="用户养了一只狗", reason="用户说：我养的是狗不是猫"))
    chat(w, normal="啊对，是狗狗")
    say(w, "我养的是狗不是猫，你记错了")
    memory = w.db.get_event_memory(cat["id"])
    assert (memory["content"], memory["confidence"]) == ("用户养了一只狗", 1.0)
    revisions = w.db.list_memory_revisions("event_memories", cat["id"])
    assert revisions[-1]["old_content"] == "用户养了一只猫" and revisions[-1]["action"] == "correct"
    assert w.db.fetch_one("SELECT COUNT(*) AS c FROM event_memories")["c"] == 1

    w.llm.responders["fake-intent"] = script(tool("query_memory", query="狗 养"), final=intent_json("chat"))
    say(w, "我家狗今天又拆家了")
    system = "\n".join(m.get("content") or "" for m in w.llm.calls_for("fake-chat")[-1]["messages"] if m["role"] == "system")
    assert "用户养了一只狗" in system and "用户养了一只猫" not in system
