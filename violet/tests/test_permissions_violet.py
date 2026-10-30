"""2026-10-30 rework step 4: what she may do by herself (ai/permissions.py) -- 自己做 / 先问你 / 不允许, approvals
answered in the chat ("同意 3") or on the page, QQ 空间 posts and the tools that change something. Fakes only."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.test_phase2_scenarios import make_world, say  # noqa: F401
from tests.test_qq_social_violet import SAME, admin, db  # noqa: F401


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture()
def told(monkeypatch):
    from ai import permissions
    said = []
    monkeypatch.setitem(permissions._RUNTIME, "rt", SimpleNamespace(send_system_text=lambda text: said.append(text)))
    return said


def test_levels(data_dir):
    from ai import permissions
    assert permissions.levels() == {"qzone_post": "self", "risky_tools": "forbid"}
    assert permissions.set_level("qzone_post", "ask")["qzone_post"] == "ask"
    with pytest.raises(ValueError):
        permissions.set_level("qzone_post", "maybe")
    with pytest.raises(ValueError):
        permissions.set_level("nuke", "self")


def test_owner_commands():
    from ai.permissions import owner_command
    assert owner_command("同意 3") == (True, 3) and owner_command(" 不同意#12 ") == (False, 12)
    assert owner_command("拒绝 4") == (False, 4) and owner_command("同意") is None and owner_command("我同意你说的 3 点") is None


def test_request_decide_and_expire(db, data_dir, told, monkeypatch):
    from ai import permissions
    db.execute("DELETE FROM approvals")
    done = []
    permissions.register("test_ability", lambda d, payload: done.append(payload) or "做好了")
    n = permissions.request(db, "test_ability", "我想做一件事", {"x": 1})
    assert f"同意 {n}" in told[-1] and permissions.pending(db)[0]["id"] == n
    r = permissions.decide(db, n, True)
    assert r == {"id": n, "status": "approved", "result": "做好了"} and done == [{"x": 1}]
    with pytest.raises(ValueError, match="同意过"):
        permissions.decide(db, n, True)
    m = permissions.request(db, "test_ability", "再做一件", {}, tell=False)
    assert permissions.decide(db, m, False)["status"] == "denied" and len(done) == 1
    permissions.register("test_ability", lambda d, p: (_ for _ in ()).throw(RuntimeError("坏了")))
    k = permissions.request(db, "test_ability", "第三件", {}, tell=False)
    assert permissions.decide(db, k, True)["status"] == "failed"
    old = permissions.request(db, "test_ability", "很久以前的", {}, tell=False)
    db.execute("UPDATE approvals SET created_at = '2000-01-01T00:00:00' WHERE id = ?", (old,))
    with pytest.raises(ValueError, match="过期"):
        permissions.decide(db, old, True)
    with pytest.raises(ValueError, match="没有编号"):
        permissions.decide(db, 999999, True)


def test_qzone_ask_first_and_forbid(db, data_dir, told, monkeypatch):
    from ai import permissions
    from ai.social import llm, qzone
    db.execute("DELETE FROM approvals")
    db.execute("DELETE FROM social_posts")
    monkeypatch.setenv("VIOLET_QZONE_ENABLED", "1")
    monkeypatch.setattr(db, "now", lambda: "2026-10-30T20:00:00")
    adapter = SimpleNamespace(is_running=lambda: True, channel_id="napcat", publish_qzone=lambda text: "t-1")
    runtime = SimpleNamespace(db=db, channels=SimpleNamespace(entries=lambda: {"napcat": {"type": "napcat"}},
                                                              get=lambda cid: adapter))
    monkeypatch.setattr(llm, "decide", lambda system, user, *a, **k: ({"post": True, "text": "晚风好舒服", "reason": "想分享"}, "m", 1))
    permissions.set_level("qzone_post", "ask")
    out = qzone.tick(runtime, SimpleNamespace(random=lambda: 0.0))
    assert out["approval"] and "晚风好舒服" in told[-1] and db.fetch_all("SELECT * FROM social_posts") == []
    assert qzone.due(db) == "上一条还在等你批准"
    monkeypatch.setitem(permissions._RUNTIME, "rt", runtime)
    r = permissions.decide(db, out["approval"], True)
    assert r["status"] == "approved" and "发好了：晚风好舒服" in r["result"]
    assert db.fetch_one("SELECT content FROM social_posts")["content"] == "晚风好舒服"
    permissions.set_level("qzone_post", "forbid")
    assert "不允许" in qzone.due(db)


def test_the_owner_answers_in_the_chat_without_a_model_call(make_world, data_dir, told):
    from ai import permissions
    w = make_world()
    w.db.execute("DELETE FROM approvals")
    permissions.register("test_ability", lambda d, payload: "已经帮你关好了")
    n = permissions.request(w.db, "test_ability", "我想关掉手机的手电筒", {}, tell=False)
    chats = len(w.llm.calls_for("fake-chat"))
    say(w, f"同意 {n}")
    assert w.client.sent_texts[-1] == "已经帮你关好了" and len(w.llm.calls_for("fake-chat")) == chats
    say(w, f"同意 {n}")
    assert "同意过" in w.client.sent_texts[-1]
    say(w, "好 99999")                                     # not an approval: an ordinary message
    assert len(w.llm.calls_for("fake-chat")) == chats + 1


def test_risky_tools_wait_for_the_owner(db, data_dir, told, monkeypatch):
    from ai import mcp, permissions
    db.execute("DELETE FROM approvals")
    server = {"name": "phone", "tools": {"torch_on": "write"}}
    assert permissions.tool_needs_approval(server, "phone", "torch_on", "write") is False      # forbid: hidden
    permissions.set_level("risky_tools", "ask")
    assert permissions.tool_needs_approval(server, "phone", "torch_on", "write") is True
    assert permissions.tool_needs_approval(server, "phone", "battery", "read") is False
    assert permissions.tool_needs_approval({**server, "allow_tools": ["torch_on"]}, "phone", "torch_on", "write") is False
    text = json.loads(permissions.ask_for_tool(db, "phone__torch_on", {"level": 1}))
    assert text["error_code"] == "NEEDS_APPROVAL" and "phone__torch_on" in told[-1]
    calls = []

    class Bridge:
        def __init__(self, servers):
            self.tool_to_session = {"phone__torch_on": object()}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def call_tool(self, name, args):
            calls.append((name, args))
            return '{"ok": true, "on": true}'
    import ai.chat
    monkeypatch.setattr(mcp, "MCPToolBridge", Bridge)
    monkeypatch.setattr(ai.chat, "get_mcp_servers", lambda: [{"name": "phone"}])
    r = permissions.decide(db, text["approval"], True)
    assert r["status"] == "approved" and calls == [("phone__torch_on", {"level": 1})]


def test_permission_routes(admin, data_dir, told):
    from ai import permissions
    d = admin.get("/api/control/permissions").get_json()["data"]
    assert {a["key"] for a in d["abilities"]} == {"qzone_post", "risky_tools"} and d["levels"]["ask"] == "先问你"
    assert admin.post("/api/control/permissions", json={"ability": "risky_tools", "level": "ask"}, headers=SAME).status_code == 200
    assert permissions.level("risky_tools") == "ask"
    permissions.register("test_ability", lambda d, p: "好了")
    from ai.chat import violetdb
    n = permissions.request(violetdb, "test_ability", "页面上批准", {}, tell=False)
    r = admin.post(f"/api/control/approvals/{n}", json={"approve": True}, headers=SAME).get_json()["data"]
    assert r["status"] == "approved" and told[-1] == "好了"
    assert admin.post(f"/api/control/approvals/{n}", json={"approve": "yes"}, headers=SAME).status_code == 400
