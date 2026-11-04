"""2026-11-02 importing Project Eve's data (ai/eve_import.py + /api/control/eve-import/...). Fictional data only."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tarfile

import pytest

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}

EVE_SCHEMA = """
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, direction TEXT NOT NULL,
  message_type TEXT NOT NULL, content TEXT, media_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE core_memories (id INTEGER PRIMARY KEY AUTOINCREMENT, memory_key TEXT NOT NULL UNIQUE, category TEXT NOT NULL,
  content TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 3, confidence REAL NOT NULL DEFAULT 1.0,
  source TEXT NOT NULL DEFAULT 'memory_agent', mention_count INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE TABLE event_memories (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'general',
  happened_at TEXT, valid_until TEXT, status TEXT NOT NULL DEFAULT 'active', importance REAL NOT NULL DEFAULT 1.0,
  emotional_weight REAL NOT NULL DEFAULT 1.0, recall_count INTEGER NOT NULL DEFAULT 1, mention_count INTEGER NOT NULL DEFAULT 1,
  forget_weight REAL NOT NULL DEFAULT 1.0, source_message_ids TEXT, last_recalled_at TEXT, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
"""
EVE_PROACTIVE = {"enabled": True, "check_interval_seconds": 60, "quiet_hours": {"start": "01:00", "end": "08:00"},
                 "daily_message_limit": 50, "min_minutes_after_user_message": 1,
                 "min_minutes_between_proactive_messages": 1, "allowed_tasks": {"weather_reminder": {"enabled": True}}}
EVE_ENV = ("DEBOUNCE_NORMAL=8\nDEBOUNCE_EXTRA='15'\nCHAT_LLM_MODEL=gpt-old\nLLM_API_KEY=sk-should-never-be-read\n"
           "CHAT_LLM_PROMPT=\"你是 Eve\"\nTESLA_MQTT_PASSWORD=x\nDEBUG_MODE=true\n")


def make_eve_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(EVE_SCHEMA)
    rows = [("user", "incoming", "text", "早呀", "2026-09-01T08:00:00+08:00"),
            ("assistant", "outgoing", "text", "早，昨晚睡得好吗", "2026-09-01T08:00:30+08:00"),
            ("user", "incoming", "image", "[图片] 一只猫", "2026-09-02T12:00:00+08:00"),
            ("assistant", "outgoing", "sticker", "（表情）", "2026-09-02T12:01:00+08:00")]
    conn.executemany("INSERT INTO messages (role, direction, message_type, content, created_at) VALUES (?, ?, ?, ?, ?)", rows)
    conn.execute("INSERT INTO core_memories (memory_key, category, content, created_at, updated_at) VALUES "
                 "('user.name', 'identity', '用户叫小林', '2026-09-01', '2026-09-01')")
    conn.execute("INSERT INTO core_memories (memory_key, category, content, created_at, updated_at) VALUES "
                 "('user.pet', 'life', '用户养了一只猫', '2026-09-02', '2026-09-02')")
    conn.execute("INSERT INTO event_memories (content, source_message_ids, created_at, updated_at) VALUES "
                 "('用户周末要去看展', '[1, 3, 99]', '2026-09-02', '2026-09-02')")
    conn.commit()
    conn.close()


def package(tmp_path, with_env=True):
    db = tmp_path / "eve" / "Memories.db"
    db.parent.mkdir()
    make_eve_db(db)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(db, arcname="eve-export/Memories.db")
        for name, text in [("proactive_config.json", json.dumps(EVE_PROACTIVE)), *([(".env", EVE_ENV)] if with_env else [])]:
            data = text.encode("utf-8")
            info = tarfile.TarInfo("eve-export/" + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        evil = tarfile.TarInfo("../../etc/evil.sh")          # never written anywhere
        evil.size = 3
        tar.addfile(evil, io.BytesIO(b"bad"))
    return buf.getvalue()


@pytest.fixture()
def env_guard(tmp_path, monkeypatch):
    import ai.control_config as cc
    saved = dict(os.environ)
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    cc._ORIGINALS.clear()
    cc._DEFAULT_STORE = None
    yield tmp_path / "data"
    os.environ.clear()
    os.environ.update(saved)
    cc._ORIGINALS.clear()
    cc._DEFAULT_STORE = None


@pytest.fixture()
def admin(env_guard, violetdb):
    from wx_ilink_py import ILinkClient, create_app
    violetdb.execute("CREATE TABLE IF NOT EXISTS eve_imports (id INTEGER PRIMARY KEY AUTOINCREMENT, file_sha256 TEXT NOT NULL, "
                     "channel TEXT, result TEXT NOT NULL, actor TEXT, created_at TEXT NOT NULL)")
    violetdb.execute("DELETE FROM eve_imports")
    app = create_app(client=ILinkClient(data_dir=env_guard, auto_login=False), violetdb=violetdb, proactive_scheduler=None,
                     all_agents=[], admin_token=TOKEN)
    app.testing = True
    c = app.test_client()
    assert c.post("/auth/login", data={"token": TOKEN}, headers=SAME).status_code == 302
    return c


def upload(admin, body, name="eve-export.tar.gz"):
    return admin.post("/api/control/eve-import/upload", data=body, headers={**SAME, "X-Filename": name,
                                                                         "Content-Type": "application/octet-stream"})


def test_preview_counts_and_settings_without_secrets(admin, tmp_path, env_guard):
    r = upload(admin, package(tmp_path))
    assert r.status_code == 200, r.get_json()
    p = r.get_json()["data"]
    assert p["messages"]["count"] == 4 and p["messages"]["user"] == 2 and p["core"]["count"] == 2
    assert p["events"]["count"] == 1 and any(c["id"] == "wechat" for c in p["channels"])
    text = json.dumps(p, ensure_ascii=False)
    assert "早呀" not in text and "用户叫小林" not in text                # counts only, never the content
    staged = env_guard / "import" / p["token"]
    assert sorted(x.name for x in staged.iterdir()) == ["Memories.db", "env.json", "meta.json", "proactive_config.json"]
    assert "sk-should" not in (staged / "env.json").read_text(encoding="utf-8")
    assert not (tmp_path / "etc").exists()
    rows = {(s["eve_key"]): s for s in p["settings"]}
    assert rows["daily_message_limit"]["ok"] and rows["daily_message_limit"]["value"] == 50
    assert rows["min_minutes_after_user_message"]["value"] == 1
    assert rows["DEBOUNCE_NORMAL"]["ok"] and rows["DEBOUNCE_EXTRA"]["value"] == 15.0
    assert not rows["CHAT_LLM_MODEL"]["ok"] and "模型服务" in rows["CHAT_LLM_MODEL"]["why"]
    assert not rows["allowed_tasks"]["ok"]
    assert "LLM_API_KEY" not in rows and "CHAT_LLM_PROMPT" not in rows and "TESLA_MQTT_PASSWORD" not in rows


def test_commit_puts_old_chat_before_violets_and_skips_repeats(admin, tmp_path, violetdb):
    violetdb.add_user_message("text", "Violet 时代的第一句", channel="wechat")
    first_violet = violetdb.fetch_one("SELECT MIN(id) AS id FROM messages")["id"]
    p = upload(admin, package(tmp_path)).get_json()["data"]
    keys = [s["key"] for s in p["settings"] if s["ok"]]
    r = admin.post(f"/api/control/eve-import/{p['token']}/commit", headers=SAME, json={
        "confirm": True, "channel": "wechat", "messages": True, "memories": True, "settings": keys})
    assert r.status_code == 200, r.get_json()
    res = r.get_json()["data"]
    assert res["messages"] == 4 and res["core"] == 2 and res["events"] == 1
    assert "proactive.daily_limit" in res["settings"] and "DEBOUNCE_NORMAL" in res["settings"]
    rows = violetdb.fetch_all("SELECT id, content, message_type, channel FROM messages ORDER BY id")
    assert [r["content"] for r in rows] == ["早呀", "早，昨晚睡得好吗", "[图片] 一只猫", "（表情）", "Violet 时代的第一句"]
    assert all(r["id"] < first_violet for r in rows[:4]) and rows[3]["message_type"] == "text"
    assert {r["channel"] for r in rows} == {"wechat"}
    assert violetdb.get_recent_messages(2)[-1]["content"] == "Violet 时代的第一句"     # still the newest
    ev = violetdb.fetch_one("SELECT source_message_ids FROM event_memories")
    assert json.loads(ev["source_message_ids"]) == [rows[0]["id"], rows[2]["id"]]
    from ai.control_config import store
    assert store().values()["proactive.daily_limit"] == 50 and store().values()["DEBOUNCE_NORMAL"] == 8.0
    assert store().history(1)[0]["reason"] == "从 Eve 导入"
    # the chat history page pages back through ids at or below 0
    c = admin.get("/api/control/messages?channel=wechat&limit=2").get_json()["data"]
    older = admin.get(f"/api/control/messages?channel=wechat&limit=2&before_id={c['next_before_id']}").get_json()["data"]
    assert [m["content"] for m in c["messages"]] == ["（表情）", "Violet 时代的第一句"]
    assert [m["content"] for m in older["messages"]] == ["早，昨晚睡得好吗", "[图片] 一只猫"]
    # the same file again: the upload is gone, a new upload adds nothing
    assert admin.post(f"/api/control/eve-import/{p['token']}/commit", headers=SAME,
                      json={"confirm": True, "channel": "wechat", "messages": True}).status_code == 400
    (tmp_path / "again").mkdir()
    p2 = upload(admin, package(tmp_path / "again")).get_json()["data"]
    assert p2["core"]["existing"] == 2
    r2 = admin.post(f"/api/control/eve-import/{p2['token']}/commit", headers=SAME, json={
        "confirm": True, "channel": "wechat", "messages": True, "memories": True}).get_json()["data"]
    assert r2["messages"] == 0 and r2["messages_skipped"] == 4 and r2["core"] == 0 and r2["events"] == 0
    assert violetdb.fetch_one("SELECT COUNT(*) AS n FROM messages")["n"] == 5


def test_a_newer_eve_copy_fills_in_after_the_first_import(admin, tmp_path, violetdb):
    """0.7.38 (user: 第二次导入后聊天页最新一条对不上): the rows only the newer copy has come AFTER the first import's,
    and every link to a moved row (event sources, the inbox) follows it."""
    violetdb.add_user_message("text", "Violet 时代的第一句", channel="wechat")

    def commit(body):
        p = upload(admin, body).get_json()["data"]
        r = admin.post(f"/api/control/eve-import/{p['token']}/commit", headers=SAME,
                       json={"confirm": True, "channel": "wechat", "messages": True, "memories": True})
        assert r.status_code == 200, r.get_json()
        return r.get_json()["data"]

    commit(package(tmp_path))
    first_ids = {r["content"]: r["id"] for r in violetdb.fetch_all("SELECT id, content FROM messages")}
    violetdb.execute("INSERT INTO inbound_messages (dedup_key, item_index, item_type, content, status, received_at, "
                     "updated_at, stored_message_id) VALUES ('k', 0, 'text', '早呀', 'done', 'x', 'x', ?)", (first_ids["早呀"],))
    (tmp_path / "newer").mkdir()
    body = package(tmp_path / "newer")
    db = tmp_path / "newer" / "eve" / "Memories.db"            # the newer copy: two more days of chat
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO messages (role, direction, message_type, content, created_at) VALUES (?, ?, ?, ?, ?)",
                     [("user", "incoming", "text", "十一月的第一句", "2026-11-01T09:00:00+08:00"),
                      ("assistant", "outgoing", "text", "十一月的回复", "2026-11-03T09:17:00+08:00")])
    conn.execute("INSERT INTO event_memories (content, source_message_ids, created_at, updated_at) VALUES "
                 "('用户十一月要考试', '[5]', '2026-11-01', '2026-11-01')")
    conn.commit()
    conn.close()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(db, arcname="eve-export/Memories.db")
    assert body and commit(buf.getvalue())["messages"] == 2
    rows = violetdb.fetch_all("SELECT id, content, created_at FROM messages ORDER BY id")
    assert [r["content"] for r in rows] == ["早呀", "早，昨晚睡得好吗", "[图片] 一只猫", "（表情）", "十一月的第一句",
                                            "十一月的回复", "Violet 时代的第一句"]
    assert rows[5]["id"] == 0 and rows[6]["id"] > 0
    by_content = {r["content"]: r["id"] for r in rows}
    sources = {r["content"]: json.loads(r["source_message_ids"]) for r in
               violetdb.fetch_all("SELECT content, source_message_ids FROM event_memories")}
    assert sources["用户周末要去看展"] == [by_content["早呀"], by_content["[图片] 一只猫"]]
    assert sources["用户十一月要考试"] == [by_content["十一月的第一句"]]
    assert violetdb.fetch_one("SELECT stored_message_id AS v FROM inbound_messages WHERE dedup_key = 'k'")["v"] == \
        by_content["早呀"]
    page = admin.get("/api/control/messages?channel=wechat&limit=3").get_json()["data"]
    assert [m["content"] for m in page["messages"]] == ["十一月的第一句", "十一月的回复", "Violet 时代的第一句"]


def test_eve_persona_first_keeps_every_memory_with_eve(admin, tmp_path, violetdb, env_guard):
    """0.7.51 (user screenshot: 导入到 微信（人设：Violet（默认））): imported into a channel on another persona, Eve's
    memories became that persona's, and choosing Eve afterwards left the ones without a source behind. as_eve sets the
    channel's persona to Eve (恋人) BEFORE the import, so the chat and ALL memories are Eve's."""
    from ai import memory_scope
    from ai import persona_templates as pt
    p = upload(admin, package(tmp_path)).get_json()["data"]
    wechat = next(c for c in p["channels"] if c["id"] == "wechat")
    assert wechat["template"] != "eve"                              # the page offers "Eve first"
    r = admin.post(f"/api/control/eve-import/{p['token']}/commit", headers=SAME, json={
        "confirm": True, "channel": "wechat", "messages": True, "memories": True, "persona": "eve", "kind": "partner"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["data"]["persona"].startswith("Eve")
    assert pt.channel_map()["wechat"] == {"template": "eve", "relationship": {"kind": "partner"}}
    assert memory_scope.for_channel("wechat") == "eve"
    with memory_scope.use("eve"):
        assert violetdb.fetch_one("SELECT COUNT(*) AS n FROM core_memories")["n"] == 2
        assert violetdb.fetch_one("SELECT COUNT(*) AS n FROM event_memories")["n"] == 1
        assert violetdb.fetch_one("SELECT COUNT(*) AS n FROM messages WHERE id <= 0")["n"] == 4
    with memory_scope.use(memory_scope.MAIN):
        assert violetdb.fetch_one("SELECT COUNT(*) AS n FROM core_memories")["n"] == 0
    (tmp_path / "again").mkdir()
    p2 = upload(admin, package(tmp_path / "again")).get_json()["data"]
    assert next(c for c in p2["channels"] if c["id"] == "wechat")["template"] == "eve"   # no offer any more


def test_any_channel_any_persona(admin, tmp_path, violetdb, env_guard):
    """0.7.52 (user: 只可以导入到微信，不能导入到其他渠道；导入记忆的时候全部人设都可以选): every channel kind is offered,
    also one not added yet (under the id the 渠道 page will give it), with every persona and relationship."""
    from ai import memory_scope
    from ai import persona_templates as pt
    p = upload(admin, package(tmp_path)).get_json()["data"]
    chans = {c["id"]: c for c in p["channels"]}
    assert {"wechat", "napcat", "qq", "telegram"} <= set(chans)
    assert chans["wechat"]["added"] is True and chans["telegram"]["added"] is False
    keys = {t["key"] for t in p["personas"]}
    assert {t["key"] for t in pt.templates()} == keys and {"eve", "violet"} <= keys
    assert {r["key"] for r in p["relationships"]} == set(pt.RELATIONSHIPS)
    r = admin.post(f"/api/control/eve-import/{p['token']}/commit", headers=SAME, json={
        "confirm": True, "channel": "telegram", "messages": True, "memories": True, "persona": "genki", "kind": "friend"})
    assert r.status_code == 200, r.get_json()
    assert pt.channel_map()["telegram"] == {"template": "genki", "relationship": {"kind": "friend"}}
    assert memory_scope.for_channel("telegram") == "genki"
    assert r.get_json()["data"]["persona"] == pt.in_effect("telegram")["persona"]
    with memory_scope.use("genki"):
        assert {m["channel"] for m in violetdb.fetch_all("SELECT channel FROM messages")} == {"telegram"}
        assert violetdb.fetch_one("SELECT COUNT(*) AS n FROM core_memories")["n"] == 2
    assert "wechat" not in pt.channel_map()                                   # the other channels are untouched
    (tmp_path / "bad").mkdir()
    p2 = upload(admin, package(tmp_path / "bad")).get_json()["data"]
    assert admin.post(f"/api/control/eve-import/{p2['token']}/commit", headers=SAME, json={
        "confirm": True, "channel": "wechat", "messages": True, "persona": "no-such"}).status_code == 400
    assert admin.post(f"/api/control/eve-import/{p2['token']}/commit", headers=SAME, json={
        "confirm": True, "channel": "line", "messages": True}).status_code == 400   # not a channel kind


def test_eve_persona_first_needs_a_live_upload(admin, env_guard):
    from ai import persona_templates as pt
    r = admin.post("/api/control/eve-import/" + "a" * 24 + "/commit", headers=SAME,
                   json={"confirm": True, "channel": "wechat", "messages": True, "persona": "eve"})
    assert r.status_code == 400 and "wechat" not in pt.channel_map()          # nothing changed for a gone upload


def test_refusals(admin, tmp_path):
    assert upload(admin, b"not an eve file at all", "x.txt").status_code == 400
    p = upload(admin, package(tmp_path, with_env=False)).get_json()["data"]
    assert admin.post(f"/api/control/eve-import/{p['token']}/commit", headers=SAME,
                      json={"channel": "wechat", "messages": True}).status_code == 400        # no confirm
    assert admin.post(f"/api/control/eve-import/{p['token']}/commit", headers=SAME,
                      json={"confirm": True, "channel": "nope", "messages": True}).status_code == 400
    assert admin.post(f"/api/control/eve-import/{p['token']}/commit", headers=SAME,
                      json={"confirm": True, "messages": True}).status_code == 400            # no channel
    assert admin.get("/api/control/eve-import/../../etc").status_code in (400, 404)
    assert admin.get("/api/control/eve-import/" + "a" * 24).status_code == 404


def test_parse_env_drops_secrets():
    from ai.eve_import import parse_env
    assert parse_env("A_B=1 # note\nMY_API_KEY=x\nexport C='two words'\nlower=1\n") == {"A_B": "1", "C": "two words"}
