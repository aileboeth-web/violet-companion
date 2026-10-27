"""Phase 7 acceptance: cross-module integration checks (offline, fictional data).

The test_D* tests reproduce the defects D1-D5 found by the phase-7 audit. They
were strict xfail tests until phase 7 A.1 fixed the defects; now they are normal
tests (more cases for each fix: tests/test_fixes_a1.py).
"""

from __future__ import annotations

import json
import os
import threading
import time
import tracemalloc
from datetime import datetime, timedelta

import pytest

from ai.channels.outbound import _seal   # phase 10.6: messages Violet composed are sealed

from tests.fakes import completion
from tests.image_fixtures import PNG, config as image_config, only, write
from tests.test_persona_life import day, happened, new_thread
from tests.test_phase2_scenarios import age_everything, arm, chat, decide, latest_event, make_world, say, script, tool  # noqa: F401
from tests.test_proactive_policy import _fixed_task_config, world  # noqa: F401

QUIET_MEMORY = lambda kw: json.dumps({"should_save": False, "reason": "t"})  # noqa: E731


@pytest.fixture(autouse=True)
def clean_images():
    from ai.images import adapters, router
    adapters.MockImageAdapter.calls.clear()
    router.reset_runtime_state()
    yield


def tools_in(call):
    return {t["function"]["name"] for t in call.get("tools") or []}


# ---------------------------------------------------------------------------
# 1. Feature switches are independent
# ---------------------------------------------------------------------------

FLAGS = {
    "VIOLET_LIFE_ENGINE": ("1", "0"), "VIOLET_PERSONA_CONTEXT": ("1", "0"), "VIOLET_PERSONA_REPLY_GUARD": ("enforce", "off"),
    "VIOLET_SEMANTIC_GUARD": ("off", "on"), "VIOLET_WEB_ENABLED": ("0", "1"), "VIOLET_WEB_EXPLORER": ("0", "1"),
    "VIOLET_IMAGE_ENABLED": ("0", "1"),
}


@pytest.mark.parametrize("flag", sorted(FLAGS))
def test_each_switch_alone_keeps_chat_working(make_world, tmp_path, monkeypatch, flag):
    monkeypatch.setenv("VIOLET_IMAGE_PROVIDERS_FILE", str(write(only(image_config(), "mockA/img-1"), tmp_path / "ip.json")))
    env = {k: v[0] for k, v in FLAGS.items()}
    env[flag] = FLAGS[flag][1]
    # phase 9.3: web tools are "on_demand" by default (Intent plans them); this test is about the switches, so the
    # web tools are offered on every turn as in phase 5B
    w = make_world(**env, VIOLET_WEB_OFFER="always")
    w.llm.responders["fake-memory"] = QUIET_MEMORY
    w.llm.responders["fake-chat"] = lambda kw: "好呀"
    say(w, "帮我画一只猫，顺便查查今天的新闻")
    assert w.client.sent_texts[-1] == "好呀"
    offered = tools_in(w.llm.calls_for("fake-chat")[-1])
    assert ("create_image" in offered) == (env["VIOLET_IMAGE_ENABLED"] == "1")
    assert ("violet-web__web_search" in offered) == (env["VIOLET_WEB_ENABLED"] == "1")
    system = "\n".join(m.get("content") or "" for m in w.llm.calls_for("fake-chat")[-1]["messages"] if m["role"] == "system")
    assert ("Violet 的稳定人设" in system) == (env["VIOLET_PERSONA_CONTEXT"] == "1")
    # the life scheduler only depends on its own switch
    from ai.life_scheduler import LifeControl
    assert (LifeControl(w.db).mode() == "on") == (env["VIOLET_LIFE_ENGINE"] == "1")


# ---------------------------------------------------------------------------
# 2. One turn through every subsystem
# ---------------------------------------------------------------------------

def test_one_turn_touches_every_subsystem_once(make_world, tmp_path, monkeypatch):
    from ai.life_store import LifeStore
    from ai.web import mcp_server
    from ai.web.providers import MockSearchProvider
    from ai.web.service import WebService
    from tests.test_web_security import make_fetcher
    from tests.web_fixtures import LocalWeb
    web = LocalWeb()
    monkeypatch.setattr(mcp_server, "build_default_server",
                        lambda: mcp_server.build_server(WebService(__import__("ai.chat", fromlist=["x"]).violetdb,
                                                                   fetcher=make_fetcher(web), provider=MockSearchProvider())))
    monkeypatch.setenv("VIOLET_IMAGE_PROVIDERS_FILE", str(write(only(image_config(), "mockA/img-1"), tmp_path / "ip.json")))
    try:
        w = make_world(VIOLET_WEB_ENABLED="1", VIOLET_IMAGE_ENABLED="1", VIOLET_WEB_OFFER="always")   # phase 9.3: see above
        store = LifeStore(w.db)
        happened(store, "又往后读了几章《边城》", hours_ago=3, thread=new_thread(store))
        w.llm.responders["fake-memory"] = script(
            tool("save_memory", summary="用户周五要参加星河大学的读书会", category="event", kind="event",
                 progress="planned", event_date="周五"),
            tool("set_situation", kind="activity", value="在图书馆", evidence="我在图书馆", until="2h"))

        # the image comes first: after web content no other tool may run in the turn (report R-L4)
        def chat_step(kw):
            rounds = len([m for m in kw["messages"] if m["role"] == "assistant" and m.get("tool_calls")])
            if rounds == 0:
                return completion(tool_calls=[{"name": "create_image", "arguments": {"subject": "violet", "scene": "current"}}])
            if rounds == 1:
                return completion(tool_calls=[{"name": "violet-web__web_search", "arguments": {"query": "小说 书评"}}])
            tools = [json.loads(m["content"][m["content"].find("{"):m["content"].rfind("}") + 1])
                     for m in kw["messages"] if m["role"] == "tool"]
            marker = tools[0]["image_marker"]
            return f"我这两天在读《边城》\n{marker}\n看到 books.example.org 上有篇书评说节奏很慢"
        w.llm.responders["fake-chat"] = chat_step
        say(w, "我在图书馆，周五要去星河大学的读书会。你最近在读什么？查查有什么书评，再画张你看书的样子给我看看")
        sent = w.client.sent
        assert [("image" if "image" in m else m["text"]) for m in sent] == [
            "我这两天在读《边城》", "image", "看到 books.example.org 上有篇书评说节奏很慢"]
        assert len(w.db.fetch_all("SELECT * FROM messages WHERE role = 'user'")) == 1
        assert [r["status"] for r in w.db.fetch_all("SELECT status FROM inbound_messages")] == ["done"]
        assert w.db.fetch_one("SELECT COUNT(*) AS c FROM event_memories")["c"] == 1          # the USER's event
        assert w.db.fetch_one("SELECT value FROM user_situations")["value"] == "在图书馆"
        assert w.db.fetch_one("SELECT share_count FROM web_findings WHERE domain = 'books.example.org'")["share_count"] == 1
        asset = w.db.fetch_one("SELECT * FROM media_assets")
        assert asset["send_status"] == "sent" and asset["life_event_id"] is not None and asset["ai_generated"] == 1
        # isolation: nothing crossed into the wrong store
        assert "边城" not in w.db.fetch_one("SELECT content FROM event_memories")["content"]
        assert w.db.fetch_all("SELECT * FROM user_profile_facts") == []
        assert w.db.fetch_one("SELECT status FROM memory_jobs")["status"] == "done"
    finally:
        web.close()


# ---------------------------------------------------------------------------
# 3. Concurrency: user turns, proactive ticks and the life thread together
# ---------------------------------------------------------------------------

def test_user_turns_proactive_ticks_and_life_thread_run_together(make_world, monkeypatch):
    from ai.life_engine import LifeEngine
    from ai.life_scheduler import LifeScheduler
    from tests.test_persona_life import config as life_cfg
    monkeypatch.setenv("VIOLET_LIFE_ENGINE", "1")
    w = make_world()
    w.llm.responders["fake-memory"] = QUIET_MEMORY
    slow = threading.Event()

    def slow_chat(kw):
        if any("主动触达" in (m.get("content") or "") for m in kw["messages"] if m["role"] == "user"):
            return "在干嘛"
        time.sleep(0.05)
        return "嗯嗯"
    w.llm.responders["fake-chat"] = slow_chat
    decide(w, topic_key="small_talk", chat_brief={"topic": "闲聊"})
    offsets = iter(range(-40, 0))
    life = LifeScheduler(w.db, clock=lambda: day(next(offsets, -1)),
                         engine_factory=lambda: LifeEngine(w.db, config=life_cfg(daily_event_probability=1.0)))
    errors = []
    stop = threading.Event()

    def life_loop():                       # the real thread waits >= 5 s between ticks; tick faster here
        while not stop.is_set():
            life.tick()
            time.sleep(0.01)

    def ticker():
        try:
            for _ in range(30):
                arm(w)
                w.scheduler.tick()
                time.sleep(0.01)
        except Exception as exc:          # pragma: no cover - reported below
            errors.append(exc)
    lt = threading.Thread(target=life_loop)
    lt.start()
    t = threading.Thread(target=ticker)
    t.start()
    for i in range(12):
        w.rt.ingest_text(f"第{i}句")
        time.sleep(0.02)
    t.join(30)
    assert w.rt.wait_idle(20)
    stop.set()
    lt.join(10)
    slow.set()
    assert errors == []
    ids = [r["client_id"] for r in w.db.fetch_all("SELECT client_id FROM outbound_messages")]
    assert len(ids) == len(set(ids))                                           # no double sends
    proactive_sent = w.db.fetch_one("SELECT COUNT(*) AS c FROM proactive_events WHERE status = 'sent'")["c"]
    assert proactive_sent <= 1                                                 # min gap 180 min, user active
    assert {r["status"] for r in w.db.fetch_all("SELECT status FROM inbound_messages")} == {"done"}
    assert w.db.fetch_one("SELECT COUNT(*) AS c FROM life_engine_runs")["c"] >= 5
    assert len(w.db.fetch_all("SELECT * FROM messages WHERE role = 'user'")) >= 1


# ---------------------------------------------------------------------------
# 4. Restart between image generation and sending
# ---------------------------------------------------------------------------

def test_crash_after_generation_recovers_without_new_charge_or_double_send(make_world, tmp_path, monkeypatch):
    from ai.images.adapters import MockImageAdapter
    from ai.images.service import ImageService
    monkeypatch.setenv("VIOLET_IMAGE_PROVIDERS_FILE", str(write(only(image_config(), "mockA/img-1"), tmp_path / "ip.json")))
    w = make_world(VIOLET_IMAGE_ENABLED="1")
    w.llm.responders["fake-memory"] = QUIET_MEMORY
    # the previous process received the message, generated the image, then died before sending
    inbound_id = w.db.add_inbound_message("wx:900", None, 0, 1, "画一只橘猫")
    w.db.update_inbound_messages([inbound_id], status="processing")
    first = ImageService(w.db).generate("一只橘猫", idempotency_key=f"turn:in:{inbound_id}:create_image:1",
                                        turn_key=f"in:{inbound_id}")
    assert first.ok and len(MockImageAdapter.calls) == 1

    def respond(kw):
        if not any(m["role"] == "tool" for m in kw["messages"]):
            return completion(tool_calls=[{"name": "create_image", "arguments": {"subject": "other", "prompt": "一只橘猫"}}])
        return json.loads(next(m for m in reversed(kw["messages"]) if m["role"] == "tool")["content"])["image_marker"]
    w.llm.responders["fake-chat"] = respond
    assert w.rt.recover_pending() == 1
    assert w.rt.wait_idle(10)
    assert len(MockImageAdapter.calls) == 1                                   # no second (paid) generation
    assert len(w.client.sent_images) == 1                                     # sent exactly once
    assert w.db.fetch_one("SELECT send_status FROM media_assets")["send_status"] == "sent"
    # and a further replay of the same turn does not send again
    report = w.rt.sender.send_messages(_seal([{"type": "image", "asset_id": first.asset_ids[0]}]), origin="reply")
    assert report.skipped and len(w.client.sent_images) == 1


# ---------------------------------------------------------------------------
# 5. Long running: threads and memory stay flat
# ---------------------------------------------------------------------------

def test_long_run_resources_stay_flat(make_world):
    w = make_world()
    w.llm.responders["fake-memory"] = QUIET_MEMORY
    for i in range(20):                                                       # warm-up
        say(w, f"热身{i}")
    threads_before = threading.active_count()
    tracemalloc.start()
    snap = tracemalloc.take_snapshot()
    started = time.monotonic()
    for i in range(120):
        say(w, f"第{i}句话")
    elapsed = time.monotonic() - started
    grown = sum(s.size_diff for s in tracemalloc.take_snapshot().compare_to(snap, "filename"))
    tracemalloc.stop()
    assert threading.active_count() <= threads_before + 2
    assert grown < 8 * 1024 * 1024, f"python heap grew by {grown / 1e6:.1f} MB over 120 turns"
    assert w.db.fetch_one("SELECT COUNT(*) AS c FROM messages WHERE role = 'user'")["c"] == 140
    print(f"\n[acceptance] 120 turns in {elapsed:.1f}s, heap +{grown / 1e6:.2f} MB, threads {threading.active_count()}")


def test_consistency_checks_scale_with_a_year_of_data(violetdb):
    """Measured, not optimised: the reply / event checks scan whole tables (report R-P1)."""
    from ai.consistency import ConsistencyGuard
    from ai.life_store import LifeStore
    store = LifeStore(violetdb)
    base = datetime.fromisoformat(violetdb.now()) - timedelta(days=365)
    now = violetdb.now()
    for i in range(400):
        violetdb.execute("INSERT INTO life_events (uid, event_type, content, occurred_at, duration_minutes, status, source, "
                      "is_virtual, created_at, updated_at) VALUES (?, 'progress', ?, ?, 60, 'happened', 't', 1, ?, ?)",
                      (f"y{i}", f"读了第{i}本书的一部分", (base + timedelta(hours=20 * i)).isoformat(), now, now))
    for i in range(3000):
        violetdb.execute("INSERT INTO event_memories (content, category, kind, status, created_at, updated_at) "
                      "VALUES (?, 'event', 'event', 'active', ?, ?)", (f"用户第{i}次说起工作上的事情", now, now))
    guard = ConsistencyGuard(violetdb)
    started = time.monotonic()
    for _ in range(5):
        guard.check_reply("我昨天去了海边\n你今天怎么样")
    reply_ms = (time.monotonic() - started) / 5 * 1000
    started = time.monotonic()
    guard.check_event({"content": "又往后读了几章", "occurred_at": now}, now)
    event_ms = (time.monotonic() - started) * 1000
    print(f"\n[acceptance] check_reply {reply_ms:.0f} ms, check_event {event_ms:.0f} ms "
          f"(400 life events, 3000 memories)")
    assert reply_ms < 3000 and event_ms < 3000
    assert len(store.events(("happened",), limit=1000)) == 400


# ---------------------------------------------------------------------------
# 6. HTTPS really verifies certificates
# ---------------------------------------------------------------------------

def test_https_uses_verified_tls_with_the_real_host_name():
    import ssl
    from ai.web.netguard import NetPolicy, SafeFetcher, WebPolicyError
    from tests.web_fixtures import LocalWeb
    fetcher = SafeFetcher(NetPolicy(total_timeout=3))
    assert fetcher.ssl_context.verify_mode == ssl.CERT_REQUIRED and fetcher.ssl_context.check_hostname
    web = LocalWeb()                                            # plain HTTP server: a TLS handshake must fail
    try:
        web.page("news.example.org", "/a")
        f = SafeFetcher(NetPolicy(total_timeout=3), resolver=web.resolver, dialer=web.dialer)
        f.policy.allowed_ports = {443}
        dial = web.dialer
        f.dialer = lambda ip, port, timeout: dial(ip, 80, timeout)            # reach the plain server on "443"
        with pytest.raises(WebPolicyError) as info:
            f.fetch("https://news.example.org/a")
        assert info.value.code in ("tls", "connect")                       # never silently downgraded
        assert not [r for r in web.requests if r["path"] == "/a"]
    finally:
        web.close()


# ---------------------------------------------------------------------------
# Defects found by the audit (expected to fail until fixed)
# ---------------------------------------------------------------------------

def test_D1_fixed_task_respects_busy_user(world):
    from ai.profile_store import Profile
    _fixed_task_config(world)
    Profile(world.db).situations.observe("availability", "在开会", "user_stated", evidence="在开会",
                                         user_message="我在开会", until="2h")
    world.scheduler.tick()
    assert world.llm.calls_for("fake-proactive") == []


def test_D1_fixed_task_respects_no_proactive_preference(world):
    from ai.profile_store import Profile
    _fixed_task_config(world)
    Profile(world.db).preferences.set_explicit("proactive_chat", "no", evidence="别主动找我", source="dashboard")
    world.scheduler.tick()
    assert world.llm.calls_for("fake-proactive") == []


def test_D2_proactive_message_cannot_send_images(make_world, tmp_path, monkeypatch):
    from ai.images.service import ImageService
    monkeypatch.setenv("VIOLET_IMAGE_PROVIDERS_FILE", str(write(only(image_config(), "mockA/img-1"), tmp_path / "ip.json")))
    w = make_world(VIOLET_IMAGE_ENABLED="1")
    w.llm.responders["fake-memory"] = QUIET_MEMORY
    unsent = ImageService(w.db).generate("dashboard 验证生成的图").asset_ids[0]     # e.g. a verification image
    say(w, "早")
    age_everything(w.db, 4)
    decide(w, topic_key="small_talk", chat_brief={"topic": "闲聊"})
    chat(w, proactive=f"给你看个东西\n[[图片:{unsent}]]")
    arm(w)
    w.scheduler.tick()
    assert w.client.sent_images == []


def test_D3_fact_deletion_is_all_or_nothing(violetdb, monkeypatch):
    from ai import profile_store
    from ai.profile_store import Profile
    profile = Profile(violetdb)
    fact = profile.facts.remember("residence.usual", "星河大学东区宿舍", evidence="我住星河大学东区宿舍",
                                  user_message="我住星河大学东区宿舍")["fact"]
    violetdb.add_user_message("text", "我住星河大学东区宿舍")

    def crash(*a, **k):
        raise RuntimeError("power cut")
    monkeypatch.setattr(profile_store, "redact_everywhere", crash)
    with pytest.raises(RuntimeError):
        profile.facts.delete(fact["id"])
    still_there = profile.facts.get_fact(fact["id"]) is not None
    text_left = "星河大学东区宿舍" in violetdb.fetch_one("SELECT content FROM messages")["content"]
    assert still_there or not text_left           # either retryable (fact kept) or fully purged


def test_D4_interrupted_sends_and_image_tasks_are_reconciled(make_world):
    w = make_world()
    before = (datetime.fromisoformat(w.db.now()) - timedelta(hours=1)).isoformat()      # the crashed process
    out_id = w.db.add_outbound_message("pybot-crash", "reply", "text", "半截", None, None)
    w.db.update_outbound_message(out_id, status="sending")
    w.db.execute("UPDATE outbound_messages SET updated_at = ? WHERE id = ?", (before, out_id))
    w.db.execute("INSERT INTO image_tasks (task_id, kind, origin, status, created_at, updated_at) "
                 "VALUES ('t-crash', 'image_generate', 'chat', 'running', ?, ?)", (before, before))
    w.db.execute("INSERT INTO image_calls (task_id, entry_id, provider_id, model_id, kind, status, accepted, images, "
                 "created_at) VALUES ('t-crash', 'p/m', 'p', 'm', 'image_generate', 'in_flight', 'unknown', 0, ?)", (before,))
    w.rt.recover_pending()
    assert w.db.fetch_one("SELECT status FROM outbound_messages WHERE id = ?", (out_id,))["status"] != "sending"
    assert w.db.fetch_one("SELECT status FROM image_tasks WHERE task_id = 't-crash'")["status"] == "uncertain"


def test_D5_life_event_sent_as_picture_counts_as_shared(make_world, tmp_path, monkeypatch):
    from ai.life_store import LifeStore
    monkeypatch.setenv("VIOLET_IMAGE_PROVIDERS_FILE", str(write(only(image_config(), "mockA/img-1"), tmp_path / "ip.json")))
    w = make_world(VIOLET_IMAGE_ENABLED="1")
    w.llm.responders["fake-memory"] = QUIET_MEMORY
    store = LifeStore(w.db)
    event = happened(store, "又往后读了几章《边城》", hours_ago=2, thread=new_thread(store))

    def respond(kw):
        if not any(m["role"] == "tool" for m in kw["messages"]):
            return completion(tool_calls=[{"name": "create_image", "arguments": {"subject": "violet", "scene": "current"}}])
        return json.loads(next(m for m in reversed(kw["messages"]) if m["role"] == "tool")["content"])["image_marker"]
    w.llm.responders["fake-chat"] = respond
    say(w, "你在干嘛？给我看看")
    assert w.db.fetch_one("SELECT life_event_id, send_status FROM media_assets") == {"life_event_id": event, "send_status": "sent"}
    assert store.event(event)["share_count"] >= 1
