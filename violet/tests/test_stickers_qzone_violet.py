"""0.7.9: stickers (her collection, sending in the owner's chat and in groups, keeping what others send), QQ 空间 posts,
the settings page's "清除数据", the persona preview. Fakes only: no network, no model, no QQ."""

from __future__ import annotations

import base64
import os
from types import SimpleNamespace

import pytest

from tests.image_fixtures import PNG
from tests.test_qq_social_violet import SAME, NoTimer, admin, engine, say  # noqa: F401

GIF = b"GIF89a" + b"\x01\x00\x01\x00\x80\x00\x00" + b"\x00" * 30


@pytest.fixture()
def db(violetdb, tmp_path, monkeypatch):
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    yield violetdb


# ------------------------------------------------------------------------------------------------ the collection

def test_collection_dedupes_checks_and_lists(db):
    from ai import stickers
    a = stickers.add_bytes(db, PNG, source="upload", description="  开心 地 转圈 ")
    again = stickers.add_bytes(db, PNG, source="group", description="别的说明")
    assert again["id"] == a["id"] and again["description"] == "开心 地 转圈"         # same picture = same sticker
    with pytest.raises(stickers.StickerError):
        stickers.add_bytes(db, b"<svg onload=alert(1)>", source="upload")
    g = stickers.add_bytes(db, GIF, source="owner")
    assert g["mime_type"] == "image/gif" and stickers.path_of(g).is_file()
    assert stickers.prompt_lines(db) == [f"- {a['id']}：开心 地 转圈"]              # undescribed ones are not offered
    stickers.update(db, a["id"], enabled_flag=False)
    assert stickers.sendable(db, a["id"]) == (None, "这个表情包已经停用") and stickers.prompt_lines(db) == []
    assert stickers.delete(db, g["id"]) and not stickers.path_of(g).exists()


def test_reply_markers_become_one_sticker_and_the_outbox_sends_it(db):
    from ai import stickers
    from ai.channels.outbound import envelope_from_payload
    from ai.chat_output import normalize_chat_output
    from violet_runtime import MessageSender
    s = stickers.add_bytes(db, PNG, source="upload", description="比心")
    payload = normalize_chat_output(f"好耶\n[[表情:{s['id']}]]\n[[表情:99]]")
    assert [m["type"] for m in payload["messages"]] == ["text", "sticker"]          # one sticker per reply
    env = envelope_from_payload(payload)
    assert env.to_public()["messages"][1] == {"type": "sticker", "sticker_id": s["id"]}

    class Client:
        sent = []

        def send_text(self, text, client_id=None, **_):
            self.sent.append(("text", text))
            return {"ok": True}

        def send_sticker(self, path, **_):
            self.sent.append(("sticker", os.path.basename(path)))
            return {"ok": True}
    client = Client()
    sender = MessageSender(client, db, simulate_typing=False)
    report = sender.send_messages(env.send_list(), origin="reply")
    assert [k for k, _ in client.sent] == ["text", "sticker"] and len(report.sent) == 2
    assert stickers.get(db, s["id"])["use_count"] == 1
    forged = env.send_list()[:1] + envelope_from_payload(normalize_chat_output("[[表情:4242]]")).send_list()
    report = sender.send_messages(forged, origin="reply")
    assert report.skipped and report.skipped[0]["error"] == "没有这个表情包" and not report.failed


def test_owner_chat_context_lists_the_collection(db):
    from ai import stickers
    s = stickers.add_bytes(db, PNG, source="upload", description="摸摸头")
    text = stickers.chat_section(db)
    assert f"{s['id']}：摸摸头" in text and "[[表情:编号]]" in text


def test_qq_marks_stickers_and_sends_them_as_stickers(tmp_path):
    from ai.channels.snowluma import SnowLumaChannel
    from ai.channels.onebot11 import segment_parts
    parts = segment_parts([{"type": "image", "data": {"url": "http://x/1", "file": "a.gif", "sub_type": 1}},
                           {"type": "image", "data": {"url": "http://x/2", "summary": "[动画表情]"}},
                           {"type": "image", "data": {"url": "http://x/3"}}])
    assert [p.data["sticker"] for p in parts] == [True, True, False]
    ch = SnowLumaChannel("napcat", {}, {}, data_dir=tmp_path)
    calls = []
    ch.call = lambda action, params=None, timeout=20.0: calls.append((action, params)) or {"message_id": 5}
    pic = tmp_path / "s.png"
    pic.write_bytes(PNG)
    ch.send_to("group", "100", None, reply_to="9", sticker_path=str(pic))
    seg = calls[0][1]["message"]
    assert seg[0]["type"] == "reply" and seg[1]["data"]["sub_type"] == 1 and seg[1]["data"]["file"].startswith("base64://")
    ch.send_sticker("7", str(pic))
    assert calls[1][0] == "send_private_msg" and calls[1][1]["message"][0]["data"]["summary"] == "[动画表情]"


# ------------------------------------------------------------------------------------------------ groups

class StickerAdapter:
    channel_id = "napcat"

    def __init__(self, tmp_path):
        self.sent, self.tmp = [], tmp_path

    def is_running(self):
        return True

    def owner_ids(self):
        return set()

    def send_to(self, kind, peer_id, text, reply_to=None, sticker_path=None):
        self.sent.append((text, bool(sticker_path)))
        return f"out{len(self.sent)}"

    def download(self, part):
        path = self.tmp / ("dl-" + part.data["url"].rsplit("/", 1)[-1] + ".png")
        path.write_bytes(PNG)
        return path


def test_group_looks_at_new_stickers_once_keeps_one_and_sends_hers(db, tmp_path, monkeypatch):
    from ai import stickers
    from ai.social.store import SocialStore
    store = SocialStore(db)
    conv = store.conversation("napcat", "group", "900", "表情包群", default_mode="auto")
    a = StickerAdapter(tmp_path)
    e = engine(db, a)
    looked = []
    monkeypatch.setattr(stickers, "describe", lambda path: looked.append(path) or "一只猫翻白眼，表示无语")
    mine = stickers.add_bytes(db, GIF, source="upload", description="小狗点头，表示同意")
    sticker = {"url": "http://q/cat", "file": "cat.gif", "sub_type": 1}
    e.receive(a, kind="group", peer_id="900", sender_id="1", sender_name="小周", text="", images=[sticker],
              platform_msg_id="501")
    seen = {}
    say(monkeypatch, {"action": "say", "messages": [], "sticker": str(mine["id"]),
                      "collect": {"message": "501", "meaning": "无语的时候"}}, seen)
    r = e.decide(conv["id"])
    assert r["sent"] == 1 and a.sent == [(None, True)]
    assert "[表情包：一只猫翻白眼，表示无语]" in seen["user"] and f"{mine['id']}：小狗点头" in seen["user"]
    kept = [s for s in stickers.listing(db) if s["source"] == "group"]
    assert kept and kept[0]["description"] == "一只猫翻白眼，表示无语" and kept[0]["source_detail"] == "表情包群"
    assert stickers.get(db, mine["id"])["use_count"] == 1
    # the same sticker again: known by its QQ file name, no second look
    e.receive(a, kind="group", peer_id="900", sender_id="2", sender_name="小王", text="哈哈", images=[sticker],
              platform_msg_id="502")
    assert store.messages(conv["id"])[-1]["content"] == "哈哈 [表情包：一只猫翻白眼，表示无语]" and len(looked) == 1


def test_sticker_switches(db, tmp_path, monkeypatch):
    from ai import stickers
    from ai.social.store import SocialStore
    monkeypatch.setenv("VIOLET_STICKER_LOOK", "0")
    conv = SocialStore(db).conversation("napcat", "group", "901", default_mode="auto")
    a = StickerAdapter(tmp_path)
    e = engine(db, a)
    monkeypatch.setattr(stickers, "describe", lambda path: pytest.fail("looked although switched off"))
    e.receive(a, kind="group", peer_id="901", sender_id="1", sender_name="A", text="",
              images=[{"url": "http://q/x", "sub_type": 1}])
    say(monkeypatch, {"action": "silent"})
    e.decide(conv["id"])
    monkeypatch.setenv("VIOLET_STICKERS", "0")
    s = stickers.add_bytes(db, PNG, source="upload", description="比心")
    assert stickers.sendable(db, s["id"])[0] is None and stickers.chat_section(db) == ""


# ------------------------------------------------------------------------------------------------ QQ 空间

class QzoneAdapter:
    channel_id = "napcat"
    self_id = "12345"

    def is_running(self):
        return True

    def publish_qzone(self, text):
        return "t-1"


def test_qzone_publish_through_snowluma_and_errors(tmp_path):
    from ai.channels.snowluma import QQError, SnowLumaChannel
    from ai.social import qzone
    ch = SnowLumaChannel("napcat", {}, {}, data_dir=tmp_path)
    calls = []
    ch.call = lambda action, params=None, timeout=20.0: calls.append((action, params)) or {"tid": "t-1", "time": 1}
    assert qzone.publish(ch, "  今天读完一本书 📖 ") == "t-1"
    assert calls == [("send_qzone_msg", {"content": "今天读完一本书 📖", "ugc_right": 1})]   # everyone may see it

    def refused(action, params=None, timeout=20.0):
        raise QQError("send_qzone_msg: retcode=-3000 请先登录")
    ch.call = refused
    with pytest.raises(qzone.QzoneError, match="没有接受：.*请先登录"):
        qzone.publish(ch, "x")
    with pytest.raises(qzone.QzoneError, match="空的"):
        qzone.publish(ch, "  ")


def test_qzone_tick_decides_records_and_respects_limits(db, monkeypatch):
    from ai.social import llm, qzone
    adapter = QzoneAdapter()
    runtime = SimpleNamespace(db=db, channels=SimpleNamespace(entries=lambda: {"napcat": {"type": "napcat"}},
                                                              get=lambda cid: adapter))
    published = []
    monkeypatch.setattr(qzone, "publish", lambda a, text: published.append(text) or "t-9")
    seen = {}

    def fake(system, user):
        seen.update(system=system, user=user)
        return {"post": True, "text": "晚上的风好舒服", "reason": "想分享"}, "fake", 5
    monkeypatch.setattr(llm, "decide", fake)
    rng = SimpleNamespace(random=lambda: 0.0)
    monkeypatch.setattr(db, "now", lambda: "2026-10-29T20:00:00")
    monkeypatch.delenv("VIOLET_QZONE_ENABLED", raising=False)
    assert qzone.tick(runtime, rng) is None and not published                  # off by default
    monkeypatch.setenv("VIOLET_QZONE_ENABLED", "1")
    assert qzone.tick(runtime, rng)["text"] == "晚上的风好舒服" and published == ["晚上的风好舒服"]
    assert "主人" in seen["system"] and "QQ 空间" in seen["system"]
    assert qzone.due(db) == "今天已经发够了" and qzone.tick(runtime, rng) is None
    monkeypatch.setenv("VIOLET_QZONE_DAILY", "0")
    assert qzone.due(db) == "离上一条不到 4 小时"
    monkeypatch.setattr(db, "now", lambda: "2026-10-29T23:30:00")
    assert qzone.due(db) == "只在 9:00 到 23:00 之间发"
    from ai.social.engine import owner_digest
    monkeypatch.setattr(db, "now", lambda: "2026-10-29T21:00:00")
    assert "你在 QQ 空间发了动态：晚上的风好舒服" in owner_digest(db)


# ------------------------------------------------------------------------------------------------ pages

def test_sticker_routes(admin, db, monkeypatch):
    from ai import stickers
    monkeypatch.setattr(stickers, "describe", lambda path: "上传的猫")
    data = "data:image/png;base64," + base64.b64encode(PNG).decode()
    s = admin.post("/api/control/stickers", json={"data": data}, headers=SAME).get_json()["data"]
    assert s["description"] == "上传的猫" and s["source_text"] == "后台上传"
    assert admin.post("/api/control/stickers", json={"data": "bm90IGFuIGltYWdl"}, headers=SAME).status_code == 400
    r = admin.get(f"/api/control/stickers/{s['id']}/file")
    assert r.status_code == 200 and r.data == PNG and r.headers["X-Content-Type-Options"] == "nosniff"
    r = admin.post(f"/api/control/stickers/{s['id']}", json={"description": "改过的", "enabled": False}, headers=SAME)
    assert r.get_json()["data"]["description"] == "改过的" and r.get_json()["data"]["enabled"] is False
    assert admin.post("/api/control/stickers/import-qq", json={}, headers=SAME).status_code == 400   # QQ not connected
    assert admin.delete(f"/api/control/stickers/{s['id']}", headers=SAME).status_code == 200
    assert admin.get("/api/control/stickers").get_json()["data"]["stickers"] == []


def test_qzone_routes_need_confirmation(admin, db):
    d = admin.get("/api/control/qzone").get_json()["data"]
    assert d["enabled"] is False and d["posts"] == [] and d["qq_connected"] is False
    assert admin.post("/api/control/qzone/publish", json={"text": "x"}, headers=SAME).status_code == 400
    r = admin.post("/api/control/qzone/publish", json={"text": "x", "confirm": True}, headers=SAME)
    assert r.status_code == 400 and "QQ 没有连接" in r.get_json()["error"]


@pytest.fixture()
def env_guard():
    """The settings saved below change os.environ (CHAT_LLM_MODEL=cys): put it back, or later tests talk to "cys"."""
    import ai.control_config as cc
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    cc._ORIGINALS.clear()
    cc._DEFAULT_STORE = None


def test_reset_memory_and_or_config(env_guard, admin, db, monkeypatch):
    from ai.control_config import store
    from ai.social.store import SocialStore
    db.execute("INSERT INTO core_memories (memory_key, category, content, created_at, updated_at) "
               "VALUES ('k', 'preference', '喜欢猫', ?, ?)", (db.now(), db.now()))
    assert admin.put("/api/control/settings", json={"changes": {"VIOLET_TRACE": True, "CHAT_LLM_MODEL": "cys"}},
                     headers=SAME).status_code == 200
    g = SocialStore(db).conversation("napcat", "group", "77", default_mode="auto")
    assert admin.post("/api/control/reset", json={"confirm": True}, headers=SAME).status_code == 400   # nothing chosen
    assert admin.post("/api/control/reset", json={"config": True}, headers=SAME).status_code == 400    # no confirm
    r = admin.post("/api/control/reset", json={"config": True, "confirm": True}, headers=SAME).get_json()["data"]
    assert "VIOLET_TRACE" in r["config"]["reset"] and "memory" not in r
    assert store().values() == {"CHAT_LLM_MODEL": "cys"}                       # the model service stays
    assert SocialStore(db).get(g["id"])["mode"] == "off"
    assert db.fetch_one("SELECT COUNT(*) AS n FROM core_memories")["n"] == 1   # memory untouched
    r = admin.post("/api/control/reset", json={"memory": True, "confirm": True}, headers=SAME).get_json()["data"]
    assert os.path.isfile(r["memory"]["archive"]) and db.fetch_one("SELECT COUNT(*) AS n FROM core_memories")["n"] == 0


def test_persona_templates_carry_a_full_preview(admin):
    d = admin.get("/api/persona/templates").get_json()
    d = d.get("data", d)
    violet = next(t for t in d["templates"] if t["key"] == "violet")
    assert violet["personality"] and violet["habits"] and violet["interests"] and violet["values"]
    assert all(isinstance(x, str) for x in violet["interests"])
    blank = next(t for t in d["templates"] if t["key"] == "blank")
    assert blank["blank"] is True and d["custom"] is None
