"""Phase 3 dashboard API for the user profile (fictional data)."""

from __future__ import annotations

import json
import os

import pytest

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def api(tmp_path, violetdb):
    from wx_ilink_py import ILinkClient, create_app
    app = create_app(client=ILinkClient(data_dir=tmp_path / "data", auto_login=False), violetdb=violetdb,
                     proactive_scheduler=None, all_agents=[], admin_token=TOKEN)
    app.testing = True
    return app.test_client()


@pytest.fixture()
def profile(violetdb):
    from ai.profile_store import Profile
    return Profile(violetdb)


def remember(profile, key, value, words):
    return profile.facts.remember(key, value, evidence=words, user_message=words)["fact"]


ROUTES = [
    ("get", "/api/profile"), ("get", "/api/profile/situations"), ("get", "/api/profile/preferences"),
    ("get", "/api/profile/facts/1/source"), ("post", "/api/profile/facts"), ("patch", "/api/profile/facts/1"),
    ("post", "/api/profile/facts/1/disable"), ("delete", "/api/profile/facts/1"),
    ("delete", "/api/profile/situations/1"), ("put", "/api/profile/preferences/reply_length"),
    ("post", "/api/profile/questions/reset"), ("post", "/api/memories/1/disable"), ("delete", "/api/memories/1"),
]


@pytest.mark.parametrize("method,path", ROUTES)
def test_every_profile_route_requires_auth(api, profile, method, path):
    remember(profile, "identity.status", "大学生", "我是大学生")
    resp = getattr(api, method)(path, json={}, headers={"Accept": "application/json"})
    assert resp.status_code == 401
    assert profile.facts.active_facts()[0]["value"] == "大学生"      # nothing changed


def test_cookie_session_writes_need_same_origin(api, profile):
    fact = remember(profile, "identity.status", "大学生", "我是大学生")
    api.post("/auth/login", data={"token": TOKEN})
    assert api.post(f"/api/profile/facts/{fact['id']}/disable").status_code == 403
    assert api.post(f"/api/profile/facts/{fact['id']}/disable", headers={"Origin": "http://localhost"}).status_code == 200


def test_view_facts_with_sources(api, profile, violetdb):
    msg_id = violetdb.add_user_message("text", "我是大学生，在星河大学")
    profile.facts.remember("identity.status", "大学生", evidence="我是大学生", user_message="我是大学生，在星河大学",
                           source_message_id=msg_id)
    data = api.get("/api/profile", headers=AUTH).get_json()
    fact = data["facts"][0]
    assert (fact["fact_key"], fact["value"], fact["origin"], fact["evidence"]) == ("identity.status", "大学生", "explicit", "我是大学生")
    assert "deletion_policy" in data and "物理删除" in data["deletion_policy"]["delete"]
    source = api.get(f"/api/profile/facts/{fact['id']}/source", headers=AUTH).get_json()
    assert source["source_message"]["content"] == "我是大学生，在星河大学"
    assert source["audit"][0]["action"] == "create"


def test_correct_from_dashboard_keeps_history(api, profile):
    old = remember(profile, "study.major", "数学", "我是数学系的")
    resp = api.patch(f"/api/profile/facts/{old['id']}", json={"value": "统计学"}, headers=AUTH)
    assert resp.status_code == 200 and resp.get_json()["fact"]["origin"] == "dashboard"
    assert [(f["value"], f["status"]) for f in profile.facts.list_facts(fact_key="study.major")] == \
        [("数学", "superseded"), ("统计学", "active")]


def test_create_and_confirm_candidate(api, profile):
    created = api.post("/api/profile/facts", json={"key": "interest", "value": "看展"}, headers=AUTH)
    assert created.status_code == 201
    candidate = profile.facts.remember("identity.status", "研究生", evidence="猜的", user_message="实验室")["fact"]
    confirmed = api.patch(f"/api/profile/facts/{candidate['id']}", json={"confirm": True, "value": "研一学生"}, headers=AUTH)
    assert confirmed.get_json()["fact"]["status"] == "active"
    assert sorted(f["value"] for f in profile.facts.active_facts()) == ["看展", "研一学生"]


def test_disable_then_context_no_longer_uses_it(api, profile, violetdb):
    from ai.context_engine import ContextEngine
    fact = remember(profile, "study.school", "星河大学", "我在星河大学")
    assert "星河大学" in "\n".join(ContextEngine(violetdb).build("chat", "学校今天停电").sections)
    api.post(f"/api/profile/facts/{fact['id']}/disable", headers=AUTH)
    assert "星河大学" not in "\n".join(ContextEngine(violetdb).build("chat", "学校今天停电").sections)
    api.post(f"/api/profile/facts/{fact['id']}/enable", headers=AUTH)
    assert "星河大学" in "\n".join(ContextEngine(violetdb).build("chat", "学校今天停电").sections)


def test_delete_removes_value_from_everything_the_model_could_see(api, profile, violetdb):
    from ai.context_engine import ContextEngine
    violetdb.add_user_message("text", "我在星河大学读书")
    fact = remember(profile, "study.school", "星河大学", "我在星河大学读书")
    violetdb.add_event_memory("用户在星河大学参加社团", kind="event", progress="ongoing")
    report = api.delete(f"/api/profile/facts/{fact['id']}", headers=AUTH).get_json()
    assert report["facts_deleted"] == [fact["id"]] and len(report["memories_deleted"]) == 1
    packet = ContextEngine(violetdb).build("chat", "学校今天停电")
    visible = "\n".join(packet.sections) + "".join(h["content"] for h in packet.history)
    assert "星河大学" not in visible
    assert violetdb.fetch_all("SELECT * FROM user_profile_facts") == []
    assert api.get(f"/api/profile/facts/{fact['id']}/source", headers=AUTH).status_code == 404
    audit = api.get("/api/profile", headers=AUTH).get_json()["audit"]
    assert audit[0]["action"] == "delete" and "星河大学" not in json.dumps(audit, ensure_ascii=False)


def test_situations_can_be_viewed_disabled_and_deleted(api, profile):
    situation = profile.situations.observe("location", "家里", "user_stated", evidence="今天回家了",
                                           user_message="今天回家了", until="today")["situation"]
    current = api.get("/api/profile/situations", headers=AUTH).get_json()["current"]
    assert [s["value"] for s in current] == ["家里"]
    api.post(f"/api/profile/situations/{situation['id']}/disable", headers=AUTH)
    assert profile.situations.current() == []
    assert api.delete(f"/api/profile/situations/{situation['id']}", headers=AUTH).status_code == 200
    assert api.get("/api/profile/situations", headers=AUTH).get_json()["recent"] == []
    assert api.post("/api/profile/situations", json={"kind": "location", "value": "图书馆", "until": "2h"},
                    headers=AUTH).status_code == 201


def test_preferences_can_be_changed_from_the_dashboard(api, profile):
    assert api.put("/api/profile/preferences/reply_length", json={"value": "short"}, headers=AUTH).status_code == 200
    assert api.put("/api/profile/preferences/reply_length", json={"value": "huge"}, headers=AUTH).status_code == 400
    eff = api.get("/api/profile/preferences", headers=AUTH).get_json()["effective"]
    assert eff["reply_length"]["value"] == "short" and eff["reply_length"]["origin"] == "explicit"
    api.delete("/api/profile/preferences/reply_length", headers=AUTH)
    assert "reply_length" not in profile.preferences.effective()


def test_memory_can_be_forbidden_and_deleted(api, violetdb):
    memory_id = violetdb.add_event_memory("用户和室友闹了点矛盾", kind="event", progress="ongoing")
    assert api.post(f"/api/memories/{memory_id}/disable", headers=AUTH).get_json()["use_in_context"] == 0
    assert violetdb.search_event_memories("室友 矛盾") == []
    violetdb.add_user_message("text", "我和室友闹了点矛盾")
    report = api.delete(f"/api/memories/{memory_id}", json={"redact": ["和室友闹了点矛盾"]}, headers=AUTH).get_json()
    assert report["deleted_memory"] == memory_id and violetdb.get_event_memory(memory_id) is None
    assert "闹了点矛盾" not in violetdb.get_recent_messages(5)[0]["content"]


def test_onboarding_questions_can_be_reset(api, profile, violetdb):
    from ai.onboarding import OnboardingPlanner
    OnboardingPlanner(violetdb, profile.facts).decline("address_name")
    assert api.post("/api/profile/questions/reset", headers=AUTH).get_json()["reset"] >= 1
    assert profile.facts.declined_keys() == set()
