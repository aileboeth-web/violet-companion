"""Phase 7 A.1: fixes for D1-D5 and the engineering items (offline, fictional data)."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta

import pytest

from ai.channels.outbound import _seal   # phase 10.6: messages Violet composed are sealed

from tests.fakes import completion
from tests.image_fixtures import PNG, config as image_config, only, write
from tests.test_persona_life import happened, new_thread
from tests.test_phase2_scenarios import age_everything, arm, chat, decide, latest_event, make_world, say  # noqa: F401
from tests.test_proactive_policy import _fixed_task_config, due_commitment, world  # noqa: F401

QUIET_MEMORY = lambda kw: json.dumps({"should_save": False, "reason": "t"})  # noqa: E731


@pytest.fixture(autouse=True)
def clean_images():
    from ai.images import adapters, router
    adapters.MockImageAdapter.calls.clear()
    router.reset_runtime_state()
    yield


@pytest.fixture()
def iworld(make_world, tmp_path, monkeypatch):
    monkeypatch.setenv("VIOLET_IMAGE_PROVIDERS_FILE", str(write(only(image_config(), "mockA/img-1"), tmp_path / "ip.json")))
    w = make_world(VIOLET_IMAGE_ENABLED="1")
    w.llm.responders["fake-memory"] = QUIET_MEMORY
    return w


def draw(w, text_after="", prompt="一只橘猫"):
    def respond(kw):
        if not any(m["role"] == "tool" for m in kw["messages"]):
            return completion(tool_calls=[{"name": "create_image", "arguments": {"subject": "other", "prompt": prompt}}])
        marker = json.loads(next(m for m in reversed(kw["messages"]) if m["role"] == "tool")["content"]).get("image_marker", "")
        return f"{marker}\n{text_after}".strip()
    w.llm.responders["fake-chat"] = respond


def old(db, hours=1):
    return (datetime.fromisoformat(db.now()) - timedelta(hours=hours)).isoformat()


# ---------------------------------------------------------------------------
# D1 -- fixed-time greetings follow the user's boundaries; reminders keep their promise
# ---------------------------------------------------------------------------

def test_D1_busy_or_no_proactive_skips_the_greeting_without_queueing_it(world):
    from ai.profile_store import Profile
    _fixed_task_config(world)
    Profile(world.db).situations.observe("availability", "在开会", "user_stated", evidence="在开会",
                                         user_message="我在开会", until="2h")
    world.scheduler.tick()
    assert world.llm.calls_for("fake-proactive") == []
    day = datetime.fromisoformat(world.db.now()).date().isoformat()
    assert world.db.get_proactive_setting(f"fixed_check_done_weather_reminder_{day}") is None   # nothing saved up


def test_D1_fixed_greeting_still_runs_normally_when_allowed(world):
    _fixed_task_config(world)
    world.scheduler.tick()
    world.scheduler.tick()
    assert len(world.llm.calls_for("fake-proactive")) == 1                                       # once, no duplicate


def test_D1_explicit_reminders_are_kept_even_if_busy_or_proactive_chat_is_off(world):
    from ai.profile_store import Profile
    profile = Profile(world.db)
    profile.preferences.set_explicit("proactive_chat", "no", evidence="别主动找我", source="dashboard")
    profile.situations.observe("availability", "在开会", "user_stated", evidence="在开会", user_message="我在开会", until="2h")
    commitment = due_commitment(world.db, "交作业")
    world.scheduler.tick()
    assert any("交作业" in t for t in world.client.sent_texts)                     # delivered at the agreed time
    assert world.db.get_commitment(commitment["id"])["status"] == "sent"
    world.scheduler.tick()
    assert sum("交作业" in t for t in world.client.sent_texts) == 1                  # and only once


# ---------------------------------------------------------------------------
# D2 -- an image needs an authorised task of this very turn, whatever the model writes
# ---------------------------------------------------------------------------

def test_D2_malformed_and_foreign_markers_never_send_images(iworld):
    from ai.images.service import ImageService
    w = iworld
    svc = ImageService(w.db)
    draw(w)
    say(w, "画一只橘猫")
    sent_before = len(w.client.sent_images)
    history = w.db.fetch_one("SELECT id FROM media_assets WHERE send_status = 'sent'")["id"]          # already sent
    unsent = svc.generate("别的对话里的图", turn_key="in:999").asset_ids[0]                          # another turn
    verification = svc.submit(__import__("ai.images.tasks", fromlist=["x"]).ImageTask(
        kind="image_generate", prompt="验证", origin="verification", model="mockA/img-1")).asset_ids[0]
    inbound = svc.media.store_bytes(PNG, direction="inbound", media_kind="user_photo", ai_generated=False)["id"]
    for marker in ("[[图片:abc]]", "[[图片:-1]]", "[[图片:99999]]", "[[图片:1.5]]", f"[[图片:{history}]]",
                   f"[[图片:{unsent}]]", f"[[图片:{verification}]]", f"[[图片:{inbound}]]"):
        w.llm.responders["fake-chat"] = lambda kw, m=marker: f"看这个\n{m}"
        say(w, "给我看看")
        assert len(w.client.sent_images) == sent_before, marker
    assert w.db.fetch_one("SELECT send_status FROM media_assets WHERE id = ?", (unsent,))["send_status"] == "not_sent"


def test_D2_outbox_refuses_images_outside_replies_even_with_a_forged_turn_key(iworld):
    from ai.images.service import ImageService
    w = iworld
    asset = ImageService(w.db).generate("一朵云", turn_key="in:5").asset_ids[0]
    forged = {"type": "image", "asset_id": asset, "turn_key": "in:5"}
    for origin in ("proactive", "commitment", "fallback", "system"):
        report = w.rt.sender.send_messages(_seal([forged]), origin=origin)
        assert not report.sent and "only sent in replies" in report.skipped[0]["error"]
    report = w.rt.sender.send_messages(_seal([{**forged, "turn_key": "in:6"}]), origin="reply")
    assert "another turn" in report.skipped[0]["error"]
    assert w.client.sent_images == []
    rows = w.db.fetch_all("SELECT status, error FROM outbound_messages WHERE message_type = 'image'")
    assert all(r["status"] == "skipped" and r["error"].startswith("unauthorized_image") for r in rows)
    report = w.rt.sender.send_messages(_seal([forged]), origin="reply")                          # the right turn: allowed
    assert report.sent and len(w.client.sent_images) == 1


def test_D2_reminders_never_send_images_and_do_not_crash_on_markers(world):
    from ai.images.service import ImageService
    from tests.test_images import service as image_service  # noqa: F401
    commitment = due_commitment(world.db, "喝水")
    world.llm.responders["fake-chat"] = lambda kw: "该喝水啦\n[[图片:1]]"
    world.scheduler.tick()
    assert "该喝水啦" in world.client.sent_texts and world.client.sent_images == []
    assert world.db.get_commitment(commitment["id"])["status"] == "sent"


def test_D2_fixed_task_and_proactive_output_are_stripped(iworld):
    from ai.images.service import ImageService
    w = iworld
    unsent = ImageService(w.db).generate("dashboard 验证生成的图").asset_ids[0]
    say(w, "早")
    age_everything(w.db, 4)
    decide(w, topic_key="small_talk", chat_brief={"topic": "闲聊"})
    chat(w, proactive=f"[[图片:{unsent}]]")
    arm(w)
    w.scheduler.tick()
    assert w.client.sent_images == [] and latest_event(w)["status"] == "skipped"     # nothing left to send


# ---------------------------------------------------------------------------
# D3 -- deletion is one transaction; any failing step leaves a retryable, consistent state
# ---------------------------------------------------------------------------

def _seed_fact(violetdb):
    from ai.profile_store import Profile
    profile = Profile(violetdb)
    fact = profile.facts.remember("residence.usual", "星河大学东区宿舍", evidence="我住星河大学东区宿舍",
                                  user_message="我住星河大学东区宿舍")["fact"]
    violetdb.add_user_message("text", "我住星河大学东区宿舍，晚上好吵")
    violetdb.add_event_memory("用户住在星河大学东区宿舍，晚上很吵", category="event")
    violetdb.execute("INSERT INTO image_tasks (task_id, kind, origin, status, prompt, created_at, updated_at) "
                  "VALUES ('t1', 'image_generate', 'chat', 'succeeded', '画星河大学东区宿舍的窗外', ?, ?)",
                  (violetdb.now(), violetdb.now()))
    return profile, fact


def _state(violetdb, fact_id):
    text = json.dumps([violetdb.fetch_all(f"SELECT * FROM {t}") for t in
                       ("messages", "event_memories", "image_tasks", "user_profile_facts")], ensure_ascii=False)
    return {"fact": violetdb.fetch_one("SELECT id FROM user_profile_facts WHERE id = ?", (fact_id,)) is not None,
            "value_anywhere": "星河大学东区宿舍" in text,
            "memories": violetdb.fetch_one("SELECT COUNT(*) AS c FROM event_memories")["c"]}


@pytest.mark.parametrize("step", ["purge_event_memory", "redact_everywhere", "delete_fact_row", "audit_log"])
def test_D3_any_failing_step_rolls_back_and_a_retry_completes(violetdb, monkeypatch, step):
    from ai import profile_store
    profile, fact = _seed_fact(violetdb)
    before = _state(violetdb, fact["id"])
    original_execute = violetdb.execute

    def boom(*a, **k):
        raise RuntimeError(f"power cut during {step}")
    if step == "purge_event_memory":
        monkeypatch.setattr(profile_store, "purge_event_memory", boom)
    elif step == "redact_everywhere":
        monkeypatch.setattr(profile_store, "redact_everywhere", boom)
    elif step == "audit_log":
        monkeypatch.setattr(profile.facts.audit, "log", boom)
    else:
        def execute(sql, params=()):
            if sql.startswith("DELETE FROM user_profile_facts"):
                raise RuntimeError("power cut during delete_fact_row")
            return original_execute(sql, params)
        monkeypatch.setattr(violetdb, "execute", execute)
    with pytest.raises(RuntimeError):
        profile.facts.delete(fact["id"])
    assert _state(violetdb, fact["id"]) == before                # nothing half-done: all rolled back
    assert not violetdb.conn.in_transaction and violetdb._tx_depth == 0
    monkeypatch.undo()
    report = profile.facts.delete(fact["id"])                 # retry works and finishes the job
    after = _state(violetdb, fact["id"])
    assert after == {"fact": False, "value_anywhere": False, "memories": 0} and report["memories_deleted"]


def test_D3_other_deletions_are_atomic_too(violetdb, monkeypatch):
    from ai import profile_store
    from ai.profile_store import Profile, purge_event_memory
    profile = Profile(violetdb)
    situation = profile.situations.observe("location", "家里", "user_stated", evidence="在家",
                                           user_message="我在家", until="today")["situation"]
    memory_id = violetdb.add_event_memory("用户周五要去星河大学参加读书会", category="event")
    monkeypatch.setattr(profile_store, "redact_everywhere", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        profile.situations.delete(situation["id"])
    with pytest.raises(RuntimeError):
        purge_event_memory(violetdb, memory_id)
    assert profile.situations.get(situation["id"]) is not None and violetdb.get_event_memory(memory_id) is not None
    monkeypatch.undo()
    profile.situations.delete(situation["id"])
    purge_event_memory(violetdb, memory_id)
    assert profile.situations.get(situation["id"]) is None and violetdb.get_event_memory(memory_id) is None


def test_D3_transactions_block_other_writers_and_nest(violetdb):
    seen = []
    with violetdb.transaction():
        violetdb.add_user_message("text", "事务里的第一句")
        t = threading.Thread(target=lambda: seen.append(violetdb.add_user_message("text", "别的线程")))
        t.start()
        time.sleep(0.2)
        assert seen == []                                    # waits for the transaction
        with violetdb.transaction():                            # nested: joins the outer one
            violetdb.add_user_message("text", "嵌套")
    t.join(5)
    assert len(seen) == 1
    with pytest.raises(ValueError):
        with violetdb.transaction():
            violetdb.add_user_message("text", "会被回滚")
            raise ValueError("rollback")
    texts = [r["content"] for r in violetdb.fetch_all("SELECT content FROM messages ORDER BY id")]
    assert texts == ["事务里的第一句", "嵌套", "别的线程"]


# ---------------------------------------------------------------------------
# D4 -- crash recovery never re-executes and never guesses
# ---------------------------------------------------------------------------

def test_D4_crash_before_during_and_after_sending(make_world):
    from ai.reconcile import reconcile_interrupted
    w = make_world()
    t = old(w.db)
    queued = w.db.add_outbound_message("c-queued", "reply", "text", "还没发出去", None, None)
    sending = w.db.add_outbound_message("c-sending", "reply", "text", "发到一半", None, None)
    w.db.update_outbound_message(sending, status="sending")
    # "sent but not recorded": WeChat got it, then the process died before the status write
    w.client.send_text("已经到了但没记下来", client_id="c-after")
    after = w.db.add_outbound_message("c-after", "reply", "text", "已经到了但没记下来", None, None)
    w.db.update_outbound_message(after, status="sending")
    fresh = w.db.add_outbound_message("c-fresh", "reply", "text", "本进程刚开始发", None, None)
    w.db.update_outbound_message(fresh, status="sending")
    for oid in (queued, sending, after):
        w.db.execute("UPDATE outbound_messages SET updated_at = ? WHERE id = ?", (t, oid))
    sent_before = list(w.client.sent)
    counts = w.rt.recover_pending() and None or reconcile_interrupted(w.db, w.rt.started_at)
    status = {r["client_id"]: r["status"] for r in w.db.fetch_all("SELECT client_id, status FROM outbound_messages")}
    assert status == {"c-queued": "cancelled", "c-sending": "unknown", "c-after": "unknown", "c-fresh": "sending"}
    assert w.client.sent == sent_before                                              # nothing re-sent
    assert counts == {k: 0 for k in counts}                                          # second run: no change


def test_D4_images_and_reminders_after_a_crash(iworld):
    from ai.images.adapters import MockImageAdapter
    from ai.images.service import ImageService
    from ai.reconcile import reconcile_interrupted
    w = iworld
    t = old(w.db)
    svc = ImageService(w.db)
    # an image whose delivery was interrupted
    asset = svc.generate("一朵云", turn_key="in:7").asset_ids[0]
    w.db.execute("UPDATE media_assets SET send_status = 'sending', updated_at = ? WHERE id = ?", (t, asset))
    # a provider call in flight, and a task that never reached the provider
    w.db.execute("INSERT INTO image_tasks (task_id, idempotency_key, kind, origin, status, created_at, updated_at) "
                 "VALUES ('t-flight', 'turn:in:8:create_image:1', 'image_generate', 'chat', 'running', ?, ?)", (t, t))
    w.db.execute("INSERT INTO image_calls (task_id, entry_id, provider_id, model_id, kind, status, accepted, images, created_at) "
                 "VALUES ('t-flight', 'mockA/img-1', 'mockA', 'img-1', 'image_generate', 'in_flight', 'unknown', 0, ?)", (t,))
    w.db.execute("INSERT INTO image_tasks (task_id, kind, origin, status, created_at, updated_at) "
                 "VALUES ('t-early', 'image_generate', 'chat', 'running', ?, ?)", (t, t))
    reminder = w.db.add_commitment("交作业", w.db.now(), evidence="提醒我交作业")["commitment"]
    w.db.execute("UPDATE commitments SET status = 'sending', updated_at = ? WHERE id = ?", (t, reminder["id"]))
    calls_before = len(MockImageAdapter.calls)
    counts = reconcile_interrupted(w.db, w.rt.started_at)
    assert counts["media_unknown"] == 1 and counts["image_tasks_uncertain"] == 1 and counts["image_tasks_failed"] == 1
    assert counts["commitments_unknown"] == 1 and counts["image_calls_uncertain"] == 1
    assert w.db.fetch_one("SELECT status FROM image_tasks WHERE task_id = 't-flight'")["status"] == "uncertain"
    early = w.db.fetch_one("SELECT status, error_code FROM image_tasks WHERE task_id = 't-early'")
    assert early == {"status": "failed", "error_code": "interrupted_before_call"}
    # the same turn again: no new (paid) generation, and the interrupted image is not resent
    again = svc.generate("一朵云", idempotency_key="turn:in:8:create_image:1")
    assert again.reused and again.status == "uncertain" and len(MockImageAdapter.calls) == calls_before
    report = w.rt.sender.send_messages(_seal([{"type": "image", "asset_id": asset, "turn_key": "in:7"}]), origin="reply")
    assert report.skipped and w.client.sent_images == []
    assert reconcile_interrupted(w.db, w.rt.started_at) == {k: 0 for k in counts}


def test_D4_dashboard_lists_explains_and_resolves(make_world, tmp_path):
    import os
    from wx_ilink_py import ILinkClient, create_app
    from ai.reconcile import reconcile_interrupted
    w = make_world()
    oid = w.db.add_outbound_message("c1", "reply", "text", "发到一半", None, None)
    w.db.execute("UPDATE outbound_messages SET status = 'sending', updated_at = ? WHERE id = ?", (old(w.db), oid))
    reconcile_interrupted(w.db, w.rt.started_at)
    app = create_app(client=ILinkClient(data_dir=tmp_path / "d", auto_login=False), violetdb=w.db,
                     proactive_scheduler=None, all_agents=[], admin_token=os.environ["VIOLET_ADMIN_TOKEN"])
    c = app.test_client()
    auth = {"Authorization": f"Bearer {os.environ['VIOLET_ADMIN_TOKEN']}"}
    assert c.get("/api/reconciliation").status_code == 401
    data = c.get("/api/reconciliation", headers=auth).get_json()
    assert [r["id"] for r in data["outbound"]] == [oid] and "不会自动重发" in data["explanations"]["outbound"]
    assert c.post(f"/api/reconciliation/outbound/{oid}", headers=auth, json={"resolution": "maybe"}).status_code == 400
    assert c.post(f"/api/reconciliation/outbound/{oid}", headers=auth, json={"resolution": "sent"}).get_json()["status"] == "sent"
    assert c.post(f"/api/reconciliation/outbound/{oid}", headers=auth, json={"resolution": "sent"}).status_code == 404
    assert c.get("/reconciliation", headers=auth).status_code == 302          # 0.7.1: old page -> Control Center


# ---------------------------------------------------------------------------
# D5 -- a picture of a life event counts as sharing it, once, only when delivered
# ---------------------------------------------------------------------------

def _eve_picture(w, store, text=""):
    event = happened(store, "又往后读了几章《边城》", hours_ago=2, thread=new_thread(store))

    def respond(kw):
        if not any(m["role"] == "tool" for m in kw["messages"]):
            return completion(tool_calls=[{"name": "create_image", "arguments": {"subject": "violet", "scene": "current"}}])
        marker = json.loads(next(m for m in reversed(kw["messages"]) if m["role"] == "tool")["content"])["image_marker"]
        return f"{text}\n{marker}".strip()
    w.llm.responders["fake-chat"] = respond
    return event


def test_D5_counted_once_with_text_and_picture_together(iworld):
    from ai.life_store import LifeStore
    w = iworld
    store = LifeStore(w.db)
    event = _eve_picture(w, store, text="又往后读了几章《边城》")
    say(w, "你在干嘛？给我看看")
    row = store.event(event)
    assert row["share_count"] == 1 and row["status"] == "happened" and row["occurred_at"]


def test_D5_failed_send_is_not_counted_and_a_retry_counts_once(iworld):
    from ai.life_store import LifeStore
    w = iworld
    store = LifeStore(w.db)
    event = _eve_picture(w, store)
    original = w.client.send_image
    w.client.send_image = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cdn down"))
    say(w, "你在干嘛？给我看看")
    assert store.event(event)["share_count"] == 0
    w.client.send_image = original
    asset = w.db.fetch_one("SELECT id, task_id FROM media_assets")
    turn_key = w.db.fetch_one("SELECT turn_key FROM image_tasks WHERE task_id = ?", (asset["task_id"],))["turn_key"]
    for _ in range(2):
        w.rt.sender.send_messages(_seal([{"type": "image", "asset_id": asset["id"], "turn_key": turn_key}]), origin="reply")
    assert store.event(event)["share_count"] == 1 and len(w.client.sent_images) == 1


def test_D5_already_shared_by_text_is_not_counted_again(iworld):
    from ai.life_store import LifeStore
    w = iworld
    store = LifeStore(w.db)
    event = _eve_picture(w, store)
    store.mark_shared(event)                                  # told earlier, in words
    say(w, "你在干嘛？给我看看")
    assert store.event(event)["share_count"] == 1


# ---------------------------------------------------------------------------
# Engineering items
# ---------------------------------------------------------------------------

def test_RM1_parameter_policy_is_configurable_and_empty_by_default(make_world, tmp_path, monkeypatch):
    from ai import model_params
    assert json.loads((model_params.policy_path()).read_text(encoding="utf-8"))["models"] == {}
    w = make_world()
    w.llm.responders["fake-memory"] = QUIET_MEMORY
    say(w, "早")
    assert "temperature" in w.llm.calls_for("fake-chat")[-1]                      # default: unchanged behaviour
    policy = tmp_path / "params.json"
    policy.write_text(json.dumps({"models": {"fake-chat": {"send_temperature": False},
                                             "fake-sem": {"max_tokens_field": "max_completion_tokens",
                                                          "allowed_extra_params": []}}}), encoding="utf-8")
    monkeypatch.setenv("VIOLET_MODEL_PARAMS_FILE", str(policy))
    say(w, "晚上好")
    assert "temperature" not in w.llm.calls_for("fake-chat")[-1]
    from ai.aux_llm import AuxLLM, AuxLLMConfig
    w.llm.responders["fake-sem"] = lambda kw: '{"ok": true}'
    AuxLLM(w.db, AuxLLMConfig(purpose="t", model="fake-sem", max_output_tokens=77)).complete_json([{"role": "user", "content": "x"}])
    call = w.llm.calls_for("fake-sem")[-1]
    assert call["max_completion_tokens"] == 77 and "max_tokens" not in call
    assert model_params.apply({"model": "fake-sem", "reasoning_effort": "low", "max_tokens": 5}) == \
        {"model": "fake-sem", "max_completion_tokens": 5}


def test_RC1_user_message_is_per_thread(iworld):
    w = iworld
    agent = w.agents["chat"]
    offered = {}
    gate = threading.Barrier(2)

    def respond(kw):
        user = next(m["content"] for m in reversed(kw["messages"]) if m["role"] == "user")
        offered[user] = {t["function"]["name"] for t in kw.get("tools") or []}
        return "嗯"
    w.llm.responders["fake-chat"] = respond

    def run(text):
        gate.wait(5)
        agent.run(text, call_context={"turn_id": text})
    threads = [threading.Thread(target=run, args=(t,)) for t in ("画一只猫", "早上好")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert "create_image" in offered["画一只猫"] and "create_image" not in offered["早上好"]


def test_RP1_deleting_an_image_erases_its_descriptions_everywhere(iworld, tmp_path):
    from ai.images.media import MediaAssetStore
    from tests.fakes import image_message
    w = iworld
    path = tmp_path / "p.png"
    path.write_bytes(PNG)
    msg = image_message(501)
    msg["item_list"][0]["_path"] = str(path)
    from ai.images.adapters import MockImageAdapter
    w.client.queue_messages([msg])
    w.rt.poll_once()
    assert w.rt.wait_idle(10)
    description = w.db.fetch_one("SELECT result_text FROM image_tasks WHERE kind = 'vision_analyze'")["result_text"]
    assert description and description in w.db.fetch_one("SELECT content FROM messages WHERE role = 'user'")["content"]
    asset = w.db.fetch_one("SELECT id FROM media_assets WHERE direction = 'inbound'")["id"]
    MediaAssetStore(w.db).delete(asset)
    everything = json.dumps([w.db.fetch_all(f"SELECT * FROM {t}") for t in
                             ("messages", "inbound_messages", "memory_jobs", "image_tasks")], ensure_ascii=False)
    assert description not in everything and "[用户发来一张图片]" in everything        # the user's turn itself stays
    assert w.db.fetch_one("SELECT result_text, prompt FROM image_tasks WHERE kind = 'vision_analyze'") == \
        {"result_text": None, "prompt": None}


def test_RP1_old_image_texts_expire(iworld):
    from ai.images.media import MediaAssetStore
    from ai.images.service import ImageService
    w = iworld
    ImageService(w.db).generate("画我的宿舍窗外", turn_key="in:1")
    w.db.execute("UPDATE image_tasks SET created_at = ?", (old(w.db, 24 * 40),))
    assert MediaAssetStore(w.db).purge_old_texts(days=30) == 1
    assert w.db.fetch_one("SELECT prompt FROM image_tasks")["prompt"] is None
    assert MediaAssetStore(w.db).purge_old_texts(days=30) == 0


def test_legacy_vision_is_only_a_fallback_when_no_model_exists(make_world, tmp_path, monkeypatch):
    from ai.images.chat_tools import intake_inbound_image
    cfg = only(image_config(), "mockA/img-1")
    monkeypatch.setenv("VIOLET_IMAGE_PROVIDERS_FILE", str(write(cfg, tmp_path / "ip.json")))
    w = make_world(VIOLET_IMAGE_ENABLED="1")
    legacy_calls = []
    legacy = lambda p: legacy_calls.append(p) or "旧识图的描述"  # noqa: E731
    path = tmp_path / "a.png"
    path.write_bytes(PNG)
    # the routed model's budget is used up -> no silent detour through the unbudgeted legacy path
    cfg["budgets"]["global"]["max_images_per_day"] = 0
    from tests.image_fixtures import model
    model(cfg, "mockA/img-1")["limits"]["max_calls_per_day"] = 0
    write(cfg, tmp_path / "ip.json")
    text = intake_inbound_image(w.db, 1, path, legacy_vision=legacy)
    assert legacy_calls == [] and "暂时没能看清" in text
    # no vision model at all -> legacy is used, capped and recorded
    write(only(image_config()), tmp_path / "ip.json")
    monkeypatch.setenv("VIOLET_IMAGE_LEGACY_VISION_MAX_PER_DAY", "1")
    assert "旧识图的描述" in intake_inbound_image(w.db, 2, path, legacy_vision=legacy)
    assert "暂时没能看清" in intake_inbound_image(w.db, 3, path, legacy_vision=legacy)          # daily cap
    assert len(legacy_calls) == 1
    row = w.db.fetch_one("SELECT * FROM image_calls WHERE entry_id = 'legacy/vision'")
    assert row["status"] == "ok"
    task = w.db.fetch_one("SELECT * FROM image_tasks WHERE entry_id = 'legacy/vision'")
    assert task["result_text"] == "旧识图的描述"                                                  # retention / deletion apply


def test_RO1_log_file_rotates(tmp_path):
    from ai.settings import _LogStream, configure_log_file
    logger = logging.getLogger("violet.test.rotation")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    path = tmp_path / "main.log"
    handler = configure_log_file(path, max_bytes=2048, backups=2, capture_std=False, root=logger)
    try:
        for i in range(400):
            logger.info("line %03d %s", i, "x" * 40)
        handler.flush()
        files = sorted(p.name for p in tmp_path.iterdir())
        assert files == ["main.log", "main.log.1", "main.log.2"] and all((tmp_path / f).stat().st_size <= 2200 for f in files)
        stream = _LogStream(logger, logging.INFO)
        print("经由 print 的输出\n第二行", file=stream)
        handler.flush()
        assert "第二行" in path.read_text(encoding="utf-8")
    finally:
        logger.removeHandler(handler)
        handler.close()


def test_RO2_RO3_dependencies_pinned_and_every_variable_documented():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    req = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "httpx==0.28.1" in req and "anyio==4.15.1" in req
    pat = re.compile(r"""(?:env_(?:str|bool|int|float|list)|os\.getenv|os\.environ\.get)\(\s*["']([A-Z0-9_]+)["']""")
    used = set()
    for path in list(root.glob("*.py")) + list((root / "ai").rglob("*.py")) + list((root / "wx_ilink_py").rglob("*.py")):
        used |= set(pat.findall(path.read_text(encoding="utf-8")))
    documented = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]+)=", (root / "env_example").read_text(encoding="utf-8"), re.M))
    assert sorted(used - documented) == []


def test_D4_real_crash_inside_a_provider_call_is_recorded_as_uncertain(iworld, monkeypatch):
    from ai.images import adapters
    from ai.images.service import ImageService
    from ai.reconcile import reconcile_interrupted
    w = iworld

    class ProcessDied(BaseException):
        pass

    def die(self, request):
        raise ProcessDied()
    monkeypatch.setattr(adapters.MockImageAdapter, "run", die)
    with pytest.raises(ProcessDied):
        ImageService(w.db).generate("一只橘猫", idempotency_key="turn:in:42:create_image:1")
    task = w.db.fetch_one("SELECT * FROM image_tasks")
    call = w.db.fetch_one("SELECT * FROM image_calls")
    assert task["status"] == "running" and call["status"] == "in_flight"            # what the crash leaves behind
    later = (datetime.fromisoformat(w.db.now()) + timedelta(seconds=1)).isoformat()  # the next process starts
    reconcile_interrupted(w.db, later)
    assert w.db.fetch_one("SELECT status FROM image_tasks")["status"] == "uncertain"
    assert w.db.fetch_one("SELECT status FROM image_calls")["status"] == "uncertain"



def test_layer_attach_images_keeps_only_this_turns_images():
    from ai.chat_output import attach_images, normalize_chat_output
    payload = normalize_chat_output("看这个\n[[图片:7]]\n[[图片:8]]\n[[图片:7]]")
    out = attach_images(payload, [8, 9], turn_key="in:1")
    images = [m for m in out["messages"] if m["type"] == "image"]
    assert [(m["asset_id"], m["turn_key"]) for m in images] == [(8, "in:1"), (9, "in:1")]   # 7 dropped, 9 appended
    assert attach_images(payload, [8], turn_key=None)["messages"] == [m for m in payload["messages"] if m["type"] == "text"]


def test_layer_outbox_checks_task_origin_and_status_even_with_the_right_turn(iworld):
    from ai.images.service import ImageService
    from ai.images.tasks import ImageTask
    w = iworld
    svc = ImageService(w.db)
    dashboard = svc.submit(ImageTask(kind="image_generate", prompt="验证", origin="dashboard", turn_key="in:5")).asset_ids[0]
    report = w.rt.sender.send_messages(_seal([{"type": "image", "asset_id": dashboard, "turn_key": "in:5"}]), origin="reply")
    assert "not produced by a successful chat image task" in report.skipped[0]["error"]
    chat_asset = svc.generate("一朵云", turn_key="in:6").asset_ids[0]
    w.db.execute("UPDATE image_tasks SET status = 'uncertain' WHERE turn_key = 'in:6'")
    report = w.rt.sender.send_messages(_seal([{"type": "image", "asset_id": chat_asset, "turn_key": "in:6"}]), origin="reply")
    assert "not produced by a successful chat image task" in report.skipped[0]["error"]
    assert w.client.sent_images == []


def test_layer_media_store_never_offers_inbound_images_for_sending(violetdb):
    from ai.images.media import MediaAssetStore
    store = MediaAssetStore(violetdb)
    inbound = store.store_bytes(PNG, direction="inbound", media_kind="user_photo", ai_generated=False)
    asset, reason = store.sendable(inbound["id"])
    assert asset is not None and reason == "only outbound images can be sent"
