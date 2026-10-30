"""2026-10-29, the "QQ" work: chat history per channel, Violet's social life (groups / other people) with one
fixed-format decision per look, the QQ channel (SnowLuma, NapCat before) with its one-click install and QQ's window,
persona templates, pictures over the official QQ bot. Fakes only: no network, no model, no QQ, no Docker."""

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


def test_qq_page_state_without_install(admin):
    d = admin.get("/api/control/qq").get_json()["data"]
    assert d["installed"] is False and "violet qq install" in d["install_command"]
    assert admin.get("/api/control/qq/screen").status_code == 400
    assert admin.post("/api/control/qq/click", json={"x": 1, "y": 2}, headers=SAME).status_code == 400
    assert admin.post("/api/control/qq/nope", json={}, headers=SAME).status_code == 404


# ------------------------------------------------------------------------------------------------ SnowLuma channel

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


def test_qq_routes_owner_to_the_chat_and_groups_to_the_social_engine(monkeypatch):
    from ai.channels.snowluma import SnowLumaChannel
    ws = FakeWS([])
    ch = SnowLumaChannel("napcat", {"ws_url": "ws://127.0.0.1:3001"}, {"ws_token": "t"}, ws_factory=lambda *a, **k: ws)
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


def test_qq_actions_wait_for_their_echo():
    from ai.channels.snowluma import SnowLumaChannel

    class WS(FakeWS):
        def send_text(self, text):
            msg = json.loads(text)
            self.sent.append(msg)
            threading.Timer(0.05, lambda: ch._answer({"status": "ok", "retcode": 0, "data": {"message_id": 77},
                                                      "echo": msg["echo"]})).start()
    ws = WS([])
    ch = SnowLumaChannel("napcat", ws_factory=lambda *a, **k: ws)
    ch._ws = ws
    assert ch.send_to("group", "614", "大家好", reply_to="33") == "77"
    action = ws.sent[0]
    assert action["action"] == "send_group_msg" and action["params"]["group_id"] == 614
    assert action["params"]["message"][0] == {"type": "reply", "data": {"id": "33"}}


def test_login_probe_and_the_account_token(tmp_path):
    from ai.channels.snowluma import account_token, probe_login
    seen = {}

    def factory(url, timeout=None, headers=None):
        seen.update(url=url, headers=headers)
        return FakeWS([{"post_type": "meta_event"}])
    assert probe_login({"ws_url": "ws://127.0.0.1:3001", "ws_token": "tok"}, ws_factory=factory, timeout=1) == \
        {"uin": "2860001234", "nick": "Violet"}
    assert seen["headers"] == {"Authorization": "Bearer tok"}

    def refused(*a, **k):
        raise ConnectionRefusedError()
    assert probe_login({"ws_url": "ws://127.0.0.1:3001"}, ws_factory=refused) is None     # not logged in yet
    config = tmp_path / "config"
    config.mkdir()
    assert account_token(str(config)) is None and account_token("relative/dir") is None
    (config / "onebot_1.json").write_text(json.dumps({"networks": {"wsServers": [
        {"port": 3001, "accessToken": "old"}]}}), encoding="utf-8")
    os.utime(config / "onebot_1.json", (1, 1))
    (config / "onebot_2.json").write_text(json.dumps({"networks": {"wsServers": [
        {"port": 3002, "accessToken": "other"}, {"port": 3001, "accessToken": "new"}]}}), encoding="utf-8")
    assert account_token(str(config)) == "new"


# ------------------------------------------------------------------------------------------------ QQ's window (VNC)

def _recv_all(conn, n):
    buf = b""
    while len(buf) < n:
        part = conn.recv(n - len(buf))
        if not part:
            raise ConnectionError("closed")
        buf += part
    return buf


def fake_vnc_server(password, sessions=2, w=64, h=40):
    """An RFB 3.8 server with VNC authentication: a dark screen, a white "window" (x 20-29, y 10-17) and a grey bar
    over the whole width at the bottom; records pointer events."""
    import socket
    import struct
    from ai.channels import vnc
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(sessions)
    events: list = []

    def serve():
        for _ in range(sessions):
            conn, _ = srv.accept()
            with conn:
                conn.sendall(b"RFB 003.008\n")
                assert _recv_all(conn, 12) == b"RFB 003.008\n"
                conn.sendall(bytes([2, 1, 2]))                    # None and VNC authentication offered
                assert _recv_all(conn, 1) == b"\x02"
                challenge = bytes(range(16))
                conn.sendall(challenge)
                good = _recv_all(conn, 16) == vnc.vnc_response(password, challenge)
                conn.sendall(struct.pack(">I", 0 if good else 1))
                if not good:
                    conn.sendall(struct.pack(">I", 3) + b"bad")
                    continue
                _recv_all(conn, 1)
                conn.sendall(struct.pack(">HH", w, h) + b"\0" * 16 + struct.pack(">I", 2) + b"qq")
                _recv_all(conn, 20 + 8)                          # SetPixelFormat, SetEncodings
                first = _recv_all(conn, 1)
                if first == b"\x03":
                    _recv_all(conn, 9)
                    px = bytearray(b"\x10\x10\x10\x00" * w * h)
                    for y in range(10, 18):
                        for x in range(20, 30):
                            px[(y * w + x) * 4:(y * w + x) * 4 + 4] = b"\xff\xff\xff\x00"
                    for y in (h - 2, h - 1):
                        px[y * w * 4:(y + 1) * w * 4] = b"\x50\x50\x50\x00" * w
                    conn.sendall(b"\x02")                        # a bell first: skipped
                    conn.sendall(struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, w, h, 0) + bytes(px))
                else:
                    rest = first + _recv_all(conn, 17)
                    events.extend(struct.unpack(">BBHH", rest[i:i + 6]) for i in range(0, 18, 6))
        srv.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], events


def test_vnc_screenshot_cuts_out_the_window_and_clicks_land_on_the_screen():
    import struct
    import time
    import zlib
    from ai.channels import vnc
    port, events = fake_vnc_server("pa55word")
    shot = vnc.screenshot(port, "pa55word", timeout=5)
    assert (shot["screen_width"], shot["screen_height"]) == (64, 40)
    assert (shot["x"], shot["y"], shot["width"], shot["height"]) == (4, 0, 40, 30)     # window + margin, no bar
    data = shot["png"]
    assert data.startswith(b"\x89PNG\r\n\x1a\n") and struct.unpack(">II", data[16:24]) == (40, 30)
    idat = data.index(b"IDAT")
    raw = zlib.decompress(data[idat + 4:idat + 4 + struct.unpack(">I", data[idat - 4:idat])[0]])
    row = raw[12 * (1 + 40 * 3):13 * (1 + 40 * 3)]                 # screen row 12
    assert row[1 + 21 * 3:1 + 22 * 3] == b"\xff\xff\xff" and row[1:4] == b"\x10\x10\x10"
    vnc.click(port, "pa55word", 25, 99, timeout=5)
    for _ in range(50):
        if events:
            break
        time.sleep(0.05)
    assert events == [(5, 0, 25, 39), (5, 1, 25, 39), (5, 0, 25, 39)]    # clamped into the screen


def test_vnc_wrong_password_and_nothing_listening():
    from ai.channels import vnc
    port, _ = fake_vnc_server("right", sessions=1)
    with pytest.raises(vnc.VNCError, match="密码不对"):
        vnc.screenshot(port, "wrong", timeout=5)
    with pytest.raises(vnc.VNCError, match="连不上"):
        vnc.screenshot(port, "right", timeout=2)                   # the server is gone
    assert vnc.reachable(port) is False
    assert vnc.content_box(b"\0\0\0\0" * 16, 4, 4) == (0, 0, 4, 4)  # an empty screen: all of it
    w, h = 64, 40                                                  # X's default weave behind a window
    px = bytearray(b"".join((b"\xff\xff\xff\x00" if (x + y) % 2 else b"\0\0\0\x00") for y in range(h) for x in range(w)))
    for y in range(5, 9):
        px[(y * w + 40) * 4:(y * w + 50) * 4] = b"\x80\x80\x80\x00" * 10
    assert vnc.content_box(bytes(px), w, h) == (32 - 12, 0, 64, 9 + 12)


def test_qq_screen_and_click_routes(admin, tmp_path, monkeypatch):
    from ai import root_tasks
    from ai.channels import vnc
    monkeypatch.setattr(root_tasks, "control_dir", lambda: tmp_path)
    (tmp_path / "snowluma.json").write_text(json.dumps({"ws_url": "ws://127.0.0.1:1", "ws_token": "t", "vnc_port": 5,
                                                       "vnc_password": "pw"}), encoding="utf-8")
    clicks = []
    crops = []
    monkeypatch.setattr(vnc, "screenshot", lambda port, pw, crop=True: crops.append(crop) or {
        "png": b"PNG", "x": 1, "y": 2, "width": 3, "height": 4, "screen_width": 9, "screen_height": 9})
    monkeypatch.setattr(vnc, "click", lambda port, pw, x, y: clicks.append((port, pw, x, y)))
    monkeypatch.setattr(vnc, "reachable", lambda port: True)
    from ai.channels import snowluma
    monkeypatch.setattr(snowluma, "probe_login", lambda info: None)
    d = admin.get("/api/control/qq/screen").get_json()["data"]
    assert d["image"] == "data:image/png;base64,UE5H" and d["x"] == 1 and "png" not in d
    admin.get("/api/control/qq/screen?full=1")
    assert crops == [True, False]                                  # the enlarged view may ask for the whole screen
    assert admin.post("/api/control/qq/click", json={"x": 10, "y": 20}, headers=SAME).status_code == 200
    assert clicks == [(5, "pw", 10, 20)]
    assert admin.post("/api/control/qq/click", json={"x": "a"}, headers=SAME).status_code == 400
    state = admin.get("/api/control/qq").get_json()["data"]
    assert state["installed"] and state["login"] == {"logged_in": False, "desktop": True}


# ------------------------------------------------------------------------------------------------ installer

class FakeHost:
    """run / stream / which of a Linux server: apt, no Docker until installed, Docker Hub blocked, a mirror works."""
    def __init__(self, containers=None):
        self.ran, self.streamed, self.containers = [], [], dict(containers or {})

    def which(self, name):
        return "/usr/bin/apt-get" if name == "apt-get" else ("/usr/bin/docker" if name == "docker" and
                                                            any(c[:3] == ["apt-get", "install", "-y"] for c in self.streamed)
                                                            else None)

    def run(self, cmd, **kw):
        from tools import violet_snowluma as sl
        self.ran.append(cmd)
        out, code = "", 0
        if cmd[0] == "getent":
            code = 2
        elif cmd[:2] == ["docker", "version"]:
            out = "27.5.1"
        elif cmd[:2] == ["docker", "inspect"]:
            if sl.CONTAINER in self.containers:
                out = self.containers[sl.CONTAINER] or "<no value>"
            else:
                code = 1
        elif cmd[:2] == ["docker", "run"]:
            self.containers[sl.CONTAINER] = sl.LABEL_VALUE
        elif cmd[:3] == ["docker", "rm", "-f"]:
            self.containers.pop(sl.CONTAINER, None)
        return SimpleNamespace(returncode=code, stdout=out, stderr="")

    def stream(self, cmd, on_line, env):
        self.streamed.append(cmd)
        on_line("Pull complete")
        if cmd[:2] == ["docker", "pull"] and not cmd[2].startswith("docker.1ms.run/"):
            return 1, "Get https://registry-1.docker.io/v2/: timeout"
        return 0, "ok"


def test_snowluma_install_reinstall_and_uninstall_with_fakes(tmp_path):
    from tools import violet_snowluma as sl
    layout = SimpleNamespace(root=tmp_path / "opt", data=tmp_path / "opt" / "data")
    old_unit = tmp_path / "violet-napcat.service"                  # what 0.7.9 left: NapCat
    old_unit.write_text(f"[Unit]\n{sl.NAPCAT_MARK}\n", encoding="utf-8")
    (layout.root / "napcat" / "Napcat").mkdir(parents=True)
    (layout.data / "control").mkdir(parents=True)
    (layout.data / "control" / "napcat.json").write_text("{}", encoding="utf-8")
    config = sl.folder(layout) / "data" / "config"
    config.mkdir(parents=True)
    (config / "onebot_123.json").write_text(json.dumps({"mode": "overlay", "statusCommand": {"x": 1}, "networks": {
        "httpServers": [{"port": 3000}], "wsServers": [{"port": 3001, "accessToken": "theirs"}]}}), encoding="utf-8")
    host = FakeHost()
    state: dict = {"user": None, "napcat": {"managed": True}}
    result = sl.install(layout, state, port_free=lambda p, h: True, run=host.run, stream=host.stream,
                        which=host.which, wait=0, check_desktop=lambda port: True, napcat_unit=old_unit,
                        fetch=lambda url, limit: pytest.fail("apt worked: no get.docker.com"))
    info = state["snowluma"]
    assert result["started"] and info["ws_port"] == 3001 and info["vnc_port"] == 5900 and len(info["vnc_password"]) == 8
    assert not old_unit.exists() and not (layout.root / "napcat").exists() and "napcat" not in state
    assert not (layout.data / "control" / "napcat.json").exists()
    assert any("NapCat" in n for n in result["notes"]) and any("docker.io" in n for n in result["notes"])
    assert ["apt-get", "install", "-y", "-qq", "docker.io"] in host.streamed
    assert ["docker", "tag", "docker.1ms.run/" + sl.IMAGE, sl.IMAGE] in host.ran and info["source"] == "docker.1ms.run"
    args = next(c for c in host.ran if c[:2] == ["docker", "run"])
    assert "127.0.0.1:3001:3001" in args and "127.0.0.1:5900:5900" in args and f"VNC_PASSWD={info['vnc_password']}" in args
    assert "SYS_PTRACE" in args and f"{sl.LABEL_KEY}={sl.LABEL_VALUE}" in args and args[-1] == sl.IMAGE
    assert not any(a.endswith(":5099") or a.endswith(":6081") or a.endswith(":3000") for a in args)
    ob = json.loads((config / "onebot.json").read_text(encoding="utf-8"))
    assert ob["networks"]["httpServers"] == [] and ob["networks"]["wsServers"][0]["accessToken"] == info["ws_token"]
    acct = json.loads((config / "onebot_123.json").read_text(encoding="utf-8"))
    assert acct["networks"]["wsServers"][0]["accessToken"] == info["ws_token"] and acct["statusCommand"] == {"x": 1}
    assert acct["networks"]["httpServers"] == []
    export = json.loads(sl.export_path(layout.data).read_text(encoding="utf-8"))
    assert export["ws_url"] == "ws://127.0.0.1:3001" and export["vnc_port"] == 5900
    assert export["config_dir"] == str(config) and export["vnc_password"] == info["vnc_password"]

    again = dict(state)
    sl.install(layout, again, port_free=lambda p, h: False, run=host.run, stream=host.stream, which=host.which,
               wait=0, check_desktop=lambda port: True, napcat_unit=old_unit, fetch=lambda url, limit: b"")
    assert again["snowluma"]["ws_token"] == info["ws_token"] and again["snowluma"]["vnc_port"] == 5900
    assert ["docker", "rm", "-f", sl.CONTAINER] in host.ran                # the old container replaced

    removed = sl.uninstall(layout, again, run=host.run, which=host.which, napcat_unit=old_unit)
    assert f"容器 {sl.CONTAINER}" in removed and f"镜像 {sl.IMAGE}" in removed
    assert not sl.folder(layout).exists() and "snowluma" not in again and not sl.export_path(layout.data).exists()


def test_snowluma_leaves_a_foreign_container_alone(tmp_path):
    from tools import violet_snowluma as sl
    layout = SimpleNamespace(root=tmp_path / "opt", data=tmp_path / "opt" / "data")
    host = FakeHost({sl.CONTAINER: ""})
    host.streamed.append(["apt-get", "install", "-y"])                 # docker is there already
    with pytest.raises(sl.SnowlumaError, match="不是 Violet 建的"):
        sl.install(layout, {"user": None}, port_free=lambda p, h: True, run=host.run, stream=host.stream,
                   which=host.which, wait=0, check_desktop=lambda port: True, napcat_unit=tmp_path / "none")
    assert not any(c[:2] == ["docker", "run"] for c in host.ran)
    sl.uninstall(layout, {}, run=host.run, which=host.which, napcat_unit=tmp_path / "none")
    assert sl.CONTAINER in host.containers and not any(c[:2] == ["docker", "rmi"] for c in host.ran)


def test_docker_from_its_script_when_there_is_no_apt(tmp_path):
    from tools import violet_snowluma as sl
    streamed, ready = [], []

    def run(cmd, **kw):
        ok = bool(ready) or cmd[:2] != ["docker", "version"]
        return SimpleNamespace(returncode=0 if ok else 1, stdout="27" if ok else "", stderr="")

    def stream(cmd, on_line, env):
        streamed.append(cmd)
        ready.append(True)
        return 0, ""
    fetched = []

    def fetch(url, limit):
        fetched.append(url)
        if "get.docker.com" in url:
            return b"<html>blocked</html>"
        return b"#!/bin/sh\n# docker install script\n"
    how = sl.ensure_docker(run, stream, lambda line: None, {}, fetch, tmp_path / "w", which=lambda name: None)
    assert "get.docker.com" in how and fetched == list(sl.DOCKER_SCRIPTS)
    assert streamed == [["sh", str(tmp_path / "w" / "get-docker.sh"), "--mirror", "Aliyun"]]


def test_task_words_are_known_everywhere():
    from ai import root_tasks
    from tools import violet_installer
    for word in ("qq-install", "qq-uninstall", "qq-restart"):
        assert word in root_tasks.TASKS and violet_installer.TASK_ACTIONS[word][0] == "qq"
    assert not any("napcat" in w for w in root_tasks.TASKS)
    assert "TimeoutStartSec=3600" in violet_installer.updater_units(
        violet_installer.Layout(Path("/opt/violet")))["violet-task.service"]


def test_channel_type_and_labels():
    from ai.channels.manager import channel_types
    from ai.channels.snowluma import SnowLumaChannel
    types = channel_types()
    assert types["napcat"]["class"] is SnowLumaChannel and SnowLumaChannel.channel_type == "napcat"
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
