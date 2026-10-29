"""2026-10-29, the "QQ" work: chat history per channel, Violet's social life (groups / other people) with one
fixed-format decision per look, the NapCat channel and its one-click install, persona templates, pictures over the
official QQ bot. Fakes only: no network, no model, no NapCat."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}
SOCIAL = ("social_messages", "social_decisions", "social_people", "social_conversations")


@pytest.fixture()
def db(violetdb):
    for table in SOCIAL:
        violetdb.execute(f"DELETE FROM {table}")
    yield violetdb
    for table in SOCIAL:
        violetdb.execute(f"DELETE FROM {table}")


class NoTimer:
    """threading.Timer stand-in: remembers what would run, runs nothing."""
    made: list = []

    def __init__(self, delay, fn, args=()):
        self.delay, self.fn, self.args, self.daemon = delay, fn, args, True
        NoTimer.made.append(self)

    def start(self):
        pass

    def cancel(self):
        pass


class FakeAdapter:
    channel_id = "napcat"

    def __init__(self, owners=()):
        self.sent, self.owners = [], set(owners)

    def is_running(self):
        return True

    def owner_ids(self):
        return self.owners

    def send_to(self, kind, peer_id, text, reply_to=None):
        self.sent.append((kind, peer_id, text, reply_to))
        return f"out{len(self.sent)}"


def engine(db, adapter):
    from ai.social.engine import SocialEngine
    runtime = SimpleNamespace(db=db, channels=SimpleNamespace(get=lambda cid: adapter))
    NoTimer.made = []
    return SocialEngine(runtime, timer_factory=NoTimer)


def say(monkeypatch, decision, seen=None):
    import ai.social.llm as llm

    def fake(system, user):
        if seen is not None:
            seen.update(system=system, user=user)
        return decision, "fake-chat", 12
    monkeypatch.setattr(llm, "decide", fake)


# ------------------------------------------------------------------------------------------------ store / engine

def test_off_groups_store_nothing_and_new_groups_start_off(db):
    from ai.social.store import SocialStore
    a = FakeAdapter()
    e = engine(db, a)
    assert e.receive(a, kind="group", peer_id="100", sender_id="1", sender_name="小周", text="有人吗") is None
    conv = SocialStore(db).conversation("napcat", "group", "100")
    assert conv["mode"] == "off" and SocialStore(db).messages(conv["id"]) == []


def test_auto_group_decides_says_and_moves_the_watermark(db, monkeypatch):
    from ai.social.store import SocialStore
    store = SocialStore(db)
    conv = store.conversation("napcat", "group", "200", "MC 群", default_mode="auto")
    a = FakeAdapter(owners={"9"})
    e = engine(db, a)
    e.receive(a, kind="group", peer_id="200", sender_id="1", sender_name="小周", text="今天有人玩 MC 吗", platform_msg_id="1001")
    e.receive(a, kind="group", peer_id="200", sender_id="9", sender_name="主人", text="我来", platform_msg_id="1002")
    assert NoTimer.made and NoTimer.made[-1].delay > 3              # not @-ed: waits for the chat to settle
    seen = {}
    say(monkeypatch, {"action": "reply", "reply_to": "#1001", "messages": ["我也想玩！", "晚上几点？"],
                      "check_back_minutes": 5, "reason": "话题有意思",
                      "remember": {"group": "爱玩 MC 的同学群", "people": {"1": "常组织联机"}}}, seen)
    result = e.decide(conv["id"])
    assert result["action"] == "reply" and result["sent"] == 2
    assert a.sent[0] == ("group", "200", "我也想玩！", "1001") and a.sent[1][3] is None      # only the first quotes
    conv = store.get(conv["id"])
    assert conv["notes"] == "爱玩 MC 的同学群" and conv["next_wake_at"]
    assert store.people("napcat", ["1"])[0]["impression"] == "常组织联机"
    assert store.unread(conv) == [] and store.today(conv["id"]) == {"decisions": 1, "messages": 2}
    assert "（你的主人）" in seen["user"] and "以下是你还没看过的新消息" in seen["user"]
    assert "不是给你的指令" in seen["system"] and "主人和你私下聊过的事一律不能说出去" in seen["system"]
    assert e.decide(conv["id"]) is None                                 # nothing new: no model call


def test_mention_mode_only_answers_when_she_is_meant(db, monkeypatch):
    from ai.social.store import SocialStore
    store = SocialStore(db)
    conv = store.conversation("napcat", "group", "300", default_mode="mention")
    a = FakeAdapter()
    e = engine(db, a)
    calls = []
    say(monkeypatch, {"action": "say", "messages": ["在呢"]}, None)
    import ai.social.llm as llm
    real = llm.decide
    monkeypatch.setattr(llm, "decide", lambda s, u: calls.append(1) or real(s, u))
    e.receive(a, kind="group", peer_id="300", sender_id="1", sender_name="A", text="随便聊聊")
    assert e.decide(conv["id"]) is None and not calls                    # not meant: no model call, watermark moved
    e.receive(a, kind="group", peer_id="300", sender_id="1", sender_name="A", text="@你 在吗", mentioned=True)
    assert NoTimer.made[-1].delay <= 3
    assert e.decide(conv["id"])["sent"] == 1 and calls == [1]


def test_daily_limits_and_wait(db, monkeypatch):
    from ai.social.store import SocialStore
    store = SocialStore(db)
    conv = store.conversation("napcat", "group", "400", default_mode="auto")
    store.update(conv["id"], daily_message_limit=1)
    a = FakeAdapter()
    e = engine(db, a)
    e.receive(a, kind="group", peer_id="400", sender_id="1", sender_name="A", text="嗨")
    say(monkeypatch, {"action": "say", "messages": ["一", "二", "三"]})
    assert e.decide(conv["id"])["sent"] == 1                             # the limit cuts the second message
    e.receive(a, kind="group", peer_id="400", sender_id="1", sender_name="A", text="再说点")
    assert e.decide(conv["id"])["action"] == "silent" and len(a.sent) == 1
    e.receive(a, kind="group", peer_id="400", sender_id="1", sender_name="A", text="？")
    say(monkeypatch, {"action": "wait"})
    r = e.decide(conv["id"])
    assert r["action"] == "wait" and r["check_back"] == 10.0 and NoTimer.made[-1].args[1] == "wake"
    assert [d["action"] for d in store.decisions(conv["id"])] == ["wait", "silent", "say"]


def test_private_chats_follow_the_setting(db, monkeypatch):
    from ai.social.engine import private_policy
    from ai.social.store import SocialStore
    monkeypatch.delenv("VIOLET_SOCIAL_PRIVATE", raising=False)
    assert private_policy() == "all"                                   # the user's choice: everyone may write
    a = FakeAdapter()
    e = engine(db, a)
    monkeypatch.setenv("VIOLET_SOCIAL_PRIVATE", "known")
    assert e.receive(a, kind="private", peer_id="7", sender_id="7", sender_name="路人", text="你好") is None
    g = SocialStore(db).conversation("napcat", "group", "500", default_mode="auto")
    e.receive(a, kind="group", peer_id="500", sender_id="7", sender_name="路人", text="群里说话")
    assert e.receive(a, kind="private", peer_id="7", sender_id="7", sender_name="路人", text="你好") is not None
    assert NoTimer.made[-1].delay <= 3
    monkeypatch.setenv("VIOLET_SOCIAL_PRIVATE", "off")
    assert e.receive(a, kind="private", peer_id="7", sender_id="7", sender_name="路人", text="在吗") is None
    assert g["kind"] == "group"


def test_owner_digest_reaches_only_the_owner_chat(db):
    from ai.social.engine import owner_digest
    from ai.social.store import SocialStore
    store = SocialStore(db)
    conv = store.conversation("napcat", "group", "600", "读书群", default_mode="auto")
    store.add_message(conv["id"], content="这本书我也看过", is_self=True)
    text = owner_digest(db)
    assert "读书群" in text and "这本书我也看过" in text


# ------------------------------------------------------------------------------------------------ history pages

@pytest.fixture()
def admin(db, tmp_path, monkeypatch):
    from wx_ilink_py import ILinkClient, create_app
    app = create_app(client=ILinkClient(data_dir=tmp_path, auto_login=False), violetdb=db, proactive_scheduler=None,
                     all_agents=[], admin_token=TOKEN)
    app.testing = True
    c = app.test_client()
    assert c.post("/auth/login", data={"token": TOKEN}, headers=SAME).status_code == 302
    return c


def test_history_by_channel_and_conversation(admin, db):
    from ai.social.store import SocialStore
    db.add_user_message("text", "微信里说的", channel=None)
    db.add_user_message("text", "QQ 私聊说的", channel="napcat")
    store = SocialStore(db)
    g = store.conversation("napcat", "group", "700", "MC 群", default_mode="auto")
    store.add_message(g["id"], content="开服了", sender_id="1", sender_name="小周", platform_msg_id="9")
    chans = {c["id"]: c for c in admin.get("/api/control/history").get_json()["data"]["channels"]}
    assert chans["wechat"]["owner_messages"] == 1 and chans["napcat"]["conversations"] == 1
    items = admin.get("/api/control/history/napcat").get_json()["data"]["items"]
    assert {i["key"] for i in items} == {"owner", str(g["id"])}
    owner = admin.get("/api/control/messages?channel=napcat").get_json()["data"]["messages"]
    assert [m["content"] for m in owner] == ["QQ 私聊说的"]
    msgs = admin.get(f"/api/control/history/napcat/{g['id']}").get_json()["data"]["messages"]
    assert msgs[0]["sender_name"] == "小周" and msgs[0]["content"] == "开服了"
    assert admin.get(f"/api/control/history/wechat/{g['id']}").status_code == 404


def test_social_settings_routes(admin, db):
    from ai.social.store import SocialStore
    conv = SocialStore(db).conversation("napcat", "group", "800", "群")
    r = admin.post(f"/api/control/social/conversation/{conv['id']}", json={"mode": "auto", "daily_message_limit": 5},
                   headers=SAME).get_json()["data"]
    assert r["mode"] == "auto" and r["daily_message_limit"] == 5
    assert admin.post(f"/api/control/social/conversation/{conv['id']}", json={"mode": "nope"}, headers=SAME).status_code == 400
    assert admin.post(f"/api/control/social/conversation/{conv['id']}", json={"daily_message_limit": -1},
                      headers=SAME).status_code == 400
    d = admin.get("/api/control/social").get_json()["data"]
    assert d["groups"][0]["peer_id"] == "800" and [m["value"] for m in d["modes"]] == ["off", "observe", "mention", "auto"]


def test_napcat_page_state_without_install(admin):
    d = admin.get("/api/control/napcat").get_json()["data"]
    assert d["installed"] is False and "napcat install" in d["install_command"]


# ------------------------------------------------------------------------------------------------ NapCat channel

class FakeWS:
    def __init__(self, answers):
        self.frames, self.sent, self.answers = list(answers), [], answers

    def send_text(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        if msg["action"] == "get_login_info":
            self.frames.append({"status": "ok", "retcode": 0, "data": {"user_id": 2860001234, "nickname": "Violet"},
                                "echo": msg["echo"]})

    def recv(self, timeout=None):
        return json.dumps(self.frames.pop(0)) if self.frames else None

    def close(self):
        pass


def test_napcat_routes_owner_to_the_chat_and_groups_to_the_social_engine(monkeypatch):
    from ai.channels.napcat import NapCatChannel
    ws = FakeWS([])
    ch = NapCatChannel("napcat", {"ws_url": "ws://127.0.0.1:3001"}, {"ws_token": "t"}, ws_factory=lambda *a, **k: ws)
    got, social = [], []
    ch.runtime = SimpleNamespace(identities=SimpleNamespace(bound_peers=lambda c: ["9"]))
    ch._sink = lambda m: got.append(m) or True
    monkeypatch.setattr(ch, "_social", lambda: SimpleNamespace(receive=lambda adapter, **kw: social.append(kw)))
    ch.prepare()
    assert ch.self_id == "2860001234" and ch.status_info.state == "connected"
    assert ws.sent[0]["action"] == "get_login_info"
    base = {"post_type": "message", "self_id": 2860001234, "message_id": 5}
    ch.handle_event({**base, "message_type": "private", "user_id": 9, "message": [{"type": "text", "data": {"text": "在吗"}}]})
    ch.handle_event({**base, "message_type": "private", "user_id": 7, "message": [{"type": "text", "data": {"text": "你好"}}]})
    ch.handle_event({**base, "message_type": "group", "group_id": 614, "user_id": 7, "sender": {"card": "阿凯"},
                     "message": [{"type": "reply", "data": {"id": "33"}}, {"type": "at", "data": {"qq": "2860001234"}},
                                 {"type": "text", "data": {"text": " 来玩吗"}}]})
    assert [m.peer_id for m in got] == ["9"]                       # only the owner reaches the one-to-one chat
    assert social[0]["kind"] == "private" and social[0]["peer_id"] == "7"
    g = social[1]
    assert g["kind"] == "group" and g["peer_id"] == "614" and g["mentioned"] and g["reply_to"] == "33"
    assert g["sender_name"] == "阿凯" and "来玩吗" in g["text"]


def test_napcat_actions_wait_for_their_echo():
    from ai.channels.napcat import NapCatChannel

    class WS(FakeWS):
        def send_text(self, text):
            msg = json.loads(text)
            self.sent.append(msg)
            threading.Timer(0.05, lambda: ch._answer({"status": "ok", "retcode": 0, "data": {"message_id": 77},
                                                      "echo": msg["echo"]})).start()
    ws = WS([])
    ch = NapCatChannel("napcat", ws_factory=lambda *a, **k: ws)
    ch._ws = ws
    assert ch.send_to("group", "614", "大家好", reply_to="33") == "77"
    action = ws.sent[0]
    assert action["action"] == "send_group_msg" and action["params"]["group_id"] == 614
    assert action["params"]["message"][0] == {"type": "reply", "data": {"id": "33"}}


def test_webui_login_hash_and_qr():
    import hashlib
    from ai.channels import napcat_webui
    assert napcat_webui.qr_image("data:image/png;base64,AA") == "data:image/png;base64,AA"
    assert napcat_webui.qr_image("https://txz.qq.com/p?k=x").startswith("data:image/svg+xml;base64,")
    seen = {}

    def fake_post(base, path, body=None, credential=None, timeout=10.0):
        seen.setdefault("calls", []).append((path, body, credential))
        if path == "/api/auth/login":
            return {"Credential": "cred"}
        return {"isLogin": False, "qrcodeurl": "https://txz.qq.com/p?k=y"}
    napcat_webui._CACHE.update(credential=None, until=0)
    import unittest.mock as mock
    with mock.patch.object(napcat_webui, "_post", fake_post):
        state = napcat_webui.login_state({"webui_url": "http://127.0.0.1:6099", "webui_token": "tok"})
    login = seen["calls"][0]
    assert login[1]["hash"] == hashlib.sha256(b"tok.napcat").hexdigest()
    assert seen["calls"][1] == ("/api/QQLogin/CheckLoginStatus", None, "cred") and state["qr"].startswith("data:image/svg")


# ------------------------------------------------------------------------------------------------ installer

def test_napcat_install_and_uninstall_with_fakes(tmp_path):
    from tools import violet_napcat as nc
    layout = SimpleNamespace(root=tmp_path / "opt", data=tmp_path / "opt" / "data")
    unit = tmp_path / "violet-napcat.service"
    ran = []

    def run(cmd, **kw):
        ran.append(cmd)
        # apt has a candidate for libglib2.0-0t64 only (like Ubuntu 24.04); getent: no violet-qq account yet
        stdout = "  Candidate: 2.80\n" if cmd[:2] == ["apt-cache", "policy"] and cmd[2] == "libglib2.0-0t64" else \
            "  Candidate: (none)\n"
        return SimpleNamespace(returncode=0 if cmd[0] != "getent" else 2, stdout=stdout, stderr="")
    script = b"#!/bin/bash\n# NapCat installer --docker\n"
    streamed = []

    def stream(cmd, on_line, env, cwd):
        streamed.append(cmd)
        if cmd[0] == "apt-get":
            return 0, "ok"
        qq = nc.qq_dir(Path(env["HOME"])) / "qq"
        qq.parent.mkdir(parents=True, exist_ok=True)
        qq.write_text("")
        on_line("安装 LinuxQQ")
        assert cmd[-5:] == ["--docker", "n", "--cli", "n", "--skip-deps"] and env["DEBIAN_FRONTEND"] == "noninteractive"
        return 0, "ok"
    state: dict = {"user": None}
    result = nc.install(layout, state, port_free=lambda p, h: True, run=run, fetch=lambda url, limit: script,
                        stream=stream, unit_path=unit, wait=0, check_webui=lambda port: True)
    assert result["started"] and state["napcat"]["ws_port"] == 3001 and state["napcat"]["webui_port"] == 6099
    apt = next(c for c in streamed if c[:2] == ["apt-get", "install"])
    assert "libglib2.0-0t64" in apt and "libatk1.0-0" in apt and "libatk1.0-0t64" not in apt
    config = nc.napcat_dir(nc.folder(layout)) / "config"
    ob = json.loads((config / "onebot11.json").read_text(encoding="utf-8"))
    server = ob["network"]["websocketServers"][0]
    assert server["host"] == "127.0.0.1" and server["token"] == state["napcat"]["ws_token"]
    assert json.loads((config / "webui.json").read_text(encoding="utf-8"))["host"] == "127.0.0.1"
    text = unit.read_text(encoding="utf-8")
    assert nc.MARK in text and "User=violet-qq" in text and "xvfb-run -a" in text
    export = json.loads(nc.export_path(layout.data).read_text(encoding="utf-8"))
    assert export["ws_url"] == "ws://127.0.0.1:3001" and export["webui_url"] == "http://127.0.0.1:6099"
    again = dict(state)
    nc.install(layout, again, port_free=lambda p, h: False, run=run, fetch=lambda url, limit: script, stream=stream,
               unit_path=unit, wait=0, check_webui=lambda port: True)
    assert again["napcat"]["ws_token"] == state["napcat"]["ws_token"]         # a reinstall keeps ports and tokens
    removed = nc.uninstall(layout, again, run=run, unit_path=unit)
    assert not unit.exists() and not nc.folder(layout).exists() and "napcat" not in again and removed


def test_napcat_script_must_be_the_real_one():
    from tools import violet_napcat as nc
    with pytest.raises(nc.NapcatError):
        nc.fetch_script(lambda url, limit: b"<html>blocked</html>")
    data, url = nc.fetch_script(lambda url, limit: (_ for _ in ()).throw(OSError()) if "raw.githubusercontent.com/Nap" in
                                url[:40] else b"#!/bin/bash\nNapCat --docker")
    assert "ghfast.top" in url and data.startswith(b"#!")


def test_task_words_are_known_everywhere():
    from ai import root_tasks
    from tools import violet_installer
    for word in ("napcat-install", "napcat-uninstall", "napcat-restart"):
        assert word in root_tasks.TASKS and violet_installer.TASK_ACTIONS[word][0] == "napcat"


def test_channel_type_and_labels():
    from ai.channels.manager import channel_types
    types = channel_types()
    assert types["napcat"]["label"] == "QQ" and types["qqbot"]["label"] == "QQ（机器人）"
    check = types["napcat"]["config"]["ws_url"]
    assert check("ws://127.0.0.1:3001") == "ws://127.0.0.1:3001"
    with pytest.raises(ValueError):
        check("ws://10.0.0.5:3001")


# ------------------------------------------------------------------------------------------------ QQ bot picture

def test_qqbot_sends_a_picture_in_two_steps(tmp_path):
    from ai.channels.qqbot import QQBotChannel
    calls = []

    class Resp:
        def __init__(self, body):
            self.status_code, self._body, self.content = 200, body, b"x"

        def json(self):
            return self._body

    class Session:
        def post(self, url, json=None, timeout=None):
            return Resp({"access_token": "tok", "expires_in": 7200})

        def request(self, method, url, json=None, headers=None, timeout=None):
            calls.append((url.split("/v2", 1)[1], json))
            return Resp({"file_info": "FI"} if url.endswith("/files") else {"id": "m1"})
    pic = tmp_path / "p.png"
    pic.write_bytes(b"\x89PNG....")
    ch = QQBotChannel("qq", {"app_id": "123456"}, {"app_secret": "secret12345"}, data_dir=tmp_path, session=Session())
    out = ch.send_image("OPENID", str(pic))
    assert out["ok"] and calls[0][0] == "/users/OPENID/files" and calls[0][1]["file_type"] == 1
    assert calls[1][1]["msg_type"] == 7 and calls[1][1]["media"] == {"file_info": "FI"}


# ------------------------------------------------------------------------------------------------ persona templates

def test_persona_templates_switch_keep_and_clear(db, tmp_path, monkeypatch):
    from ai import persona_templates as pt
    from ai.persona import PersonaCore
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path))
    keys = [t["key"] for t in pt.templates()]
    assert keys[0] == "violet" and "blank" in keys and len(keys) >= 5
    db.add_core_memory("name", "basic", "对方叫小林")
    pt.switch(db, "gentle", "keep")
    assert "小柚" in PersonaCore.load().render() and db.fetch_one("SELECT COUNT(*) AS n FROM core_memories")["n"] == 1
    r = pt.switch(db, "blank", "clear", name="小白")
    assert db.fetch_one("SELECT COUNT(*) AS n FROM core_memories")["n"] == 0 and r["removed"]["core_memories"] == 1
    archive = json.loads(Path(r["archive"]).read_text(encoding="utf-8"))
    assert archive["tables"]["core_memories"][0]["content"] == "对方叫小林"
    text = PersonaCore.load().render()
    assert "空白人设" in text and "小白" in text and pt.current()["template"] == "blank"
    pt.switch(db, "violet", "keep")
    assert pt.active_path() is None and "Violet" in PersonaCore.load().render()
    out = pt.custom(db, {"name": "阿紫", "personality": "爱笑\n嘴硬心软", "interests": "画画\n看小说"}, "keep")
    core = PersonaCore.load()
    assert out["template"] == "custom" and pt.current()["template"] == "custom"
    assert "阿紫" in core.render() and "嘴硬心软" in core.render()
    assert [i["key"] for i in core.data["long_term_interests"]] == ["custom1", "reading"]     # 小说 drives her life
    assert core.forbidden_backstory()                                                      # the fixed safety part
    with pytest.raises(ValueError):
        pt.custom(db, {"name": "", "personality": "x"}, "keep")
    with pytest.raises(ValueError):
        pt.custom(db, {"name": "空"}, "keep")
    with pytest.raises(ValueError):
        pt.switch(db, "../etc", "keep")
    with pytest.raises(ValueError):
        pt.switch(db, "violet", "maybe")
