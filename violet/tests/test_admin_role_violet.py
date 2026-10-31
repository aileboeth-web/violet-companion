"""2026-10-30 (user: two QQ accounts bound, the second is another person, its chat landed in "你"; the history should be
by QQ name and number; admins may command her a little in groups; friend requests wait for the owner by default).
Fakes only: no network, no model, no QQ."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.test_qq_social_violet import FakeAdapter, FakeWS, admin, db, engine  # noqa: F401


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture()
def clean(db):
    db.execute("DELETE FROM approvals")
    yield db
    db.execute("DELETE FROM approvals")


def bind(db, peer, name, status="bound"):
    from ai.channels.identity import IdentityStore
    ids = IdentityStore(db)
    ids.admit("napcat", peer, "owner", name)
    row = ids.get("napcat", peer)
    ids.set_status(row["id"], "bound")
    if status != "bound":
        ids.set_status(row["id"], status)
    return ids


def test_owner_messages_remember_the_account_and_old_rows_are_filled_in(clean):
    db = clean
    mid = db.add_user_message("text", "在吗", channel="napcat", peer_id="9")
    assert db.get_message(mid)["peer_id"] == "9"
    # an older database: the column filled in from the inbox / outbox
    old_in = db.add_user_message("text", "旧的", channel="napcat")
    old_out = db.add_assistant_message("text", "旧的回复", channel="napcat")
    inbound = db.add_inbound_message("k-old", "m1", 0, 1, "旧的", channel="napcat", peer_id="7")
    db.update_inbound_messages([inbound], stored_message_id=old_in)
    db.execute("INSERT INTO outbound_messages (client_id, origin, message_type, content, status, message_id, created_at, "
               "updated_at, channel, peer_id) VALUES ('c-old', 'reply', 'text', '旧的回复', 'sent', ?, ?, ?, 'napcat', '7')",
               (old_out, db.now(), db.now()))
    db.execute("UPDATE messages SET peer_id = (SELECT i.peer_id FROM inbound_messages i WHERE i.stored_message_id = "
               "messages.id AND i.peer_id IS NOT NULL LIMIT 1) WHERE peer_id IS NULL")
    db.execute("UPDATE messages SET peer_id = (SELECT o.peer_id FROM outbound_messages o WHERE o.message_id = messages.id "
               "AND o.peer_id IS NOT NULL LIMIT 1) WHERE peer_id IS NULL")
    assert db.get_message(old_in)["peer_id"] == "7" and db.get_message(old_out)["peer_id"] == "7"


def test_an_admin_takes_its_messages_to_its_own_window(clean):
    from ai.social.store import SocialStore
    db = clean
    bind(db, "9", "主人号")
    ids = bind(db, "7", "阿凯")
    db.add_user_message("text", "我是主人", channel="napcat", peer_id="9")
    db.add_user_message("text", "我是阿凯", channel="napcat", peer_id="7")
    db.add_assistant_message("text", "你好阿凯", channel="napcat", peer_id="7")
    assert set(ids.bound_peers("napcat")) == {"7", "9"}
    ids.set_status(ids.get("napcat", "7")["id"], "admin")
    assert [m["content"] for m in db.fetch_all("SELECT content FROM messages WHERE channel = 'napcat'")] == ["我是主人"]
    store = SocialStore(db)
    conv = store.conversation("napcat", "private", "7")
    rows = store.messages(conv["id"])
    assert [(m["content"], m["is_self"]) for m in rows] == [("我是阿凯", 0), ("你好阿凯", 1)]
    assert conv["title"] == "阿凯" and store.unread(conv) == []           # already read: she does not answer again
    assert ids.bound_peers("napcat") == ["9"] and ids.admin_peers("napcat") == ["7"]
    assert ids.admit("napcat", "7") == "admin"


def test_history_lists_each_account_by_name_and_number(admin, clean):
    db = clean
    bind(db, "9", "小陈")
    bind(db, "8", "小陈的大号")
    db.add_user_message("text", "旧消息", channel="napcat")                 # no sender: the account bound first
    db.add_user_message("text", "小号说的", channel="napcat", peer_id="9")
    db.add_user_message("text", "大号说的", channel="napcat", peer_id="8")
    items = admin.get("/api/control/history/napcat").get_json()["data"]["items"]
    owners = {i["peer"]: i for i in items if i["kind"] == "owner"}
    assert set(owners) == {"9", "8"} and all(i["title"] != "你" for i in items)
    assert owners["9"]["title"] == "小陈" and owners["9"]["subtitle"] == "QQ 9 · 主人" and owners["9"]["count"] == 2
    got = admin.get("/api/control/messages?channel=napcat&peer=8").get_json()["data"]["messages"]
    assert [m["content"] for m in got] == ["大号说的"]
    got = admin.get("/api/control/messages?channel=napcat&peer=9").get_json()["data"]["messages"]
    assert [m["content"] for m in got] == ["旧消息", "小号说的"]
    days = admin.get("/api/control/messages/days?channel=napcat&peer=8").get_json()["data"]
    assert sum(d["n"] for d in days) == 1


def test_an_admins_window_is_named_and_marked(admin, clean):
    db = clean
    bind(db, "7", "阿凯", status="admin")
    db.execute("UPDATE social_conversations SET title = NULL")             # the old window had only the number
    items = admin.get("/api/control/history/napcat").get_json()["data"]["items"]
    private = [i for i in items if i["kind"] == "private"]
    assert private and private[0]["title"] == "阿凯" and private[0]["subtitle"] == "QQ 7 · 管理员"
    assert not [i for i in items if i["kind"] == "owner"]


def test_admin_private_chat_goes_to_the_social_engine_even_when_strangers_are_off(clean, monkeypatch):
    db = clean
    monkeypatch.setenv("VIOLET_SOCIAL_PRIVATE", "off")

    class Adapter(FakeAdapter):
        def admin_ids(self):
            return {"7"}
    a = Adapter(owners={"9"})
    e = engine(db, a)
    assert e.receive(a, kind="private", peer_id="5", sender_id="5", sender_name="路人", text="你好") is None
    assert e.receive(a, kind="private", peer_id="7", sender_id="7", sender_name="阿凯", text="你好") is not None


def test_admins_may_ask_her_to_speak_in_groups_but_get_no_owner_tools(clean, monkeypatch):
    from ai.social.store import SocialStore
    db = clean

    class Adapter(FakeAdapter):
        def admin_ids(self):
            return {"7"}
    store = SocialStore(db)
    conv = store.conversation("napcat", "group", "614", "MC 群", default_mode="auto")
    a = Adapter(owners={"9"})
    e = engine(db, a)
    e.receive(a, kind="group", peer_id="614", sender_id="7", sender_name="阿凯", text="Violet 说句话", platform_msg_id="1")
    seen = {}
    import ai.social.llm as llm
    monkeypatch.setattr(llm, "decide", lambda system, user, tools=None: (seen.update(system=system, user=user)
                                                                        or ({"action": "say", "messages": ["来了"]}, "fake", 1)))
    import ai.social.tools as social_tools
    asked = []
    real = social_tools.SocialTools

    def spy(db_, owner_asked, **kw):
        asked.append(owner_asked)
        return real(db_, owner_asked, **kw)
    monkeypatch.setattr(social_tools, "SocialTools", spy)
    e.decide(conv["id"])
    assert "（管理员）" in seen["user"] and "【管理员的请求】" in seen["system"]
    assert "管理员说了不算" in seen["system"] and asked == [False]          # an admin's request opens no owner tools


def test_friend_requests_wait_for_the_owner(clean, data_dir, monkeypatch):
    from ai import permissions
    from ai.channels.snowluma import SnowLumaChannel
    db = clean
    said = []
    ch = SnowLumaChannel("napcat", ws_factory=lambda *a, **k: FakeWS([]))
    ch.runtime = SimpleNamespace(db=db, channels=SimpleNamespace(get=lambda cid: ch))
    monkeypatch.setitem(permissions._RUNTIME, "rt", SimpleNamespace(send_system_text=said.append, channels=ch.runtime.channels))
    calls = []
    monkeypatch.setattr(ch, "call", lambda action, params=None, timeout=20.0: calls.append((action, params)) or {})
    monkeypatch.setattr(ch, "is_running", lambda: True)
    event = {"post_type": "request", "request_type": "friend", "user_id": 555, "comment": "我是阿凯的同学", "flag": "f1"}
    ch._friend_request(event)                         # what handle_event starts in a thread
    pending = permissions.pending(db)
    assert len(pending) == 1 and pending[0]["ability"] == "friend_add" and calls == []
    assert "QQ 555 想加我好友" in said[0] and "我是阿凯的同学" in said[0] and f"同意 {pending[0]['id']}" in said[0]
    result = permissions.decide(db, pending[0]["id"], True)
    assert result["status"] == "approved" and calls == [("set_friend_add_request", {"flag": "f1", "approve": True})]

    permissions.set_level("friend_add", "forbid")
    ch._friend_request({**event, "flag": "f2"})
    assert len(calls) == 1 and len(permissions.pending(db)) == 0           # forbid: not answered
    permissions.set_level("friend_add", "self")
    ch._friend_request({**event, "flag": "f3"})
    assert calls[-1] == ("set_friend_add_request", {"flag": "f3", "approve": True}) and "我同意了" in said[-1]


def test_handle_event_answers_friend_requests_off_the_receive_loop(monkeypatch):
    from ai.channels.snowluma import SnowLumaChannel
    ch = SnowLumaChannel("napcat", ws_factory=lambda *a, **k: FakeWS([]))
    started = []
    monkeypatch.setattr(ch, "_friend_request", lambda event: started.append(event))
    assert ch.handle_event({"post_type": "request", "request_type": "friend", "user_id": 1, "flag": "x"}) == 0
    import time
    for _ in range(50):
        if started:
            break
        time.sleep(0.02)
    assert started and started[0]["flag"] == "x"
