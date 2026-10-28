"""Phase 10.3: first-run setup -- the checklist, the model test, the initial password that must be changed, and the
in-app initialiser the installer calls (tools/violet_setup.py). Fake model, temp data dir, no network."""

from __future__ import annotations

import json
import os

import httpx
import openai
import pytest

from tests.fakes import FakeLLM, install_fake_llm

ADMIN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}
JSON = {"Accept": "application/json"}
KEY = "sk-test-p103-never-shown-0000000000"


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
def made(env_guard, violetdb, monkeypatch):
    llm = install_fake_llm(monkeypatch, FakeLLM({}))
    from ai.agents import ChatAgent, IntentAgent
    from wx_ilink_py import ILinkClient, create_app
    agents = [IntentAgent(), ChatAgent()]
    app = create_app(client=ILinkClient(data_dir=env_guard, auto_login=False), violetdb=violetdb, proactive_scheduler=None,
                     all_agents=agents, admin_token=ADMIN)
    app.testing = True
    return app, llm, agents


def _login(app, password=ADMIN, field="token"):
    c = app.test_client()
    resp = c.post("/auth/login", data={field: password}, headers=SAME)
    assert resp.status_code == 302, resp.status_code
    return c


# ------------------------------------------------------------------------------------------------ checklist

def test_a_fresh_install_lists_what_is_missing_in_order(made, monkeypatch):
    app, _llm, _agents = made
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    admin = _login(app)
    data = admin.get("/api/control/setup", headers=JSON).get_json()["data"]
    items = {i["id"]: i for i in data["items"]}
    assert data["complete"] is False and data["next"]["id"] == "admin"
    assert items["admin"]["state"] == "todo" and items["model_service"]["state"] == "todo"
    assert items["persona"]["state"] == "optional" and items["tools"]["required"] is False
    assert "channels" not in items                                   # no runtime in this app: not reported as ok


def test_the_model_test_uses_the_running_settings_and_keeps_only_the_outcome(made, monkeypatch, env_guard):
    app, llm, agents = made
    monkeypatch.setenv("LLM_API_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("LLM_API_KEY", KEY)
    llm.default = lambda kw: "在的"
    admin = _login(app)
    result = admin.post("/api/control/setup/test-model", json={}, headers=SAME).get_json()["data"]
    assert result["ok"] and result["kind"] == "ok" and result["model"] == agents[1]._config.reply_model
    call = llm.calls[-1]
    assert call["max_tokens"] == 16 and call["messages"][0]["role"] == "user"
    stored = (env_guard / "control" / "setup.json").read_text(encoding="utf-8")
    assert KEY not in stored and "在的" not in stored
    items = {i["id"]: i for i in admin.get("/api/control/setup", headers=JSON).get_json()["data"]["items"]}
    assert items["model_service"]["state"] == "ok"
    monkeypatch.setenv("LLM_API_KEY", KEY + "x")                     # a changed key makes the old result stale
    items = {i["id"]: i for i in admin.get("/api/control/setup", headers=JSON).get_json()["data"]["items"]}
    assert items["model_service"]["state"] == "warn" and "还没有测试过对话" in items["model_service"]["detail"]


@pytest.mark.parametrize("error, kind", [
    (openai.AuthenticationError("bad key " + KEY, response=httpx.Response(401, request=httpx.Request("POST", "http://x")),
                                body=None), "auth"),
    (openai.NotFoundError("model nope", response=httpx.Response(404, request=httpx.Request("POST", "http://x")),
                          body=None), "not_found"),
    (openai.APIConnectionError(request=httpx.Request("POST", "http://10.1.2.3/v1")), "connection"),
])
def test_a_failed_model_test_says_why_in_plain_words_and_never_quotes_the_error(made, monkeypatch, error, kind):
    app, llm, _agents = made
    monkeypatch.setenv("LLM_API_KEY", KEY)

    def boom(kw):
        raise error
    llm.default = boom
    admin = _login(app)
    body = admin.post("/api/control/setup/test-model", json={}, headers=SAME).get_data(as_text=True)
    result = json.loads(body)["data"]
    assert result["ok"] is False and result["kind"] == kind and KEY not in body and "10.1.2.3" not in body


def test_model_name_check_and_listing_degrade_safely(made, monkeypatch):
    app, _llm, _agents = made
    admin = _login(app)
    assert admin.post("/api/control/setup/test-model", json={"model": ""}, headers=SAME).status_code == 400
    listed = admin.post("/api/control/setup/models", json={}, headers=SAME).get_json()["data"]
    assert listed["ok"] is False and listed["kind"] == "unsupported" and listed["models"] == []


# ------------------------------------------------------------------------------------------------ initial password

def test_the_generated_password_is_not_forced_to_change(made, env_guard):
    """Violet stage 3 (the user's choice): the installer's random password can be kept; it is only reported."""
    app, _llm, _agents = made
    from wx_ilink_py.auth import AdminStore
    AdminStore(env_guard).set_password("Initial1Password2x", must_change=True)
    c = _login(app, "Initial1Password2x", field="password")
    setup = c.get("/api/control/setup", headers=JSON)
    assert setup.status_code == 200
    assert next(i for i in setup.get_json()["data"]["items"] if i["id"] == "admin")["state"] == "ok"
    assert c.get("/api/control/overview", headers=JSON).status_code == 200
    assert c.get("/control", headers={"Accept": "text/html"}).status_code == 200
    assert c.get("/auth/status", headers=JSON).get_json()["must_change"] is True
    ok =c.post("/auth/password", json={"current": "Initial1Password2x", "new": "my own long passphrase 7",
                                        "confirm": "my own long passphrase 7"}, headers=SAME)
    assert ok.status_code == 200
    assert c.get("/api/control/setup", headers=JSON).status_code == 200
    assert AdminStore(env_guard).must_change() is False


def test_healthz_names_the_service_for_the_installer(made):
    app, _llm, _agents = made
    assert app.test_client().get("/healthz").get_json() == {"ok": True, "service": "violet"}


# ------------------------------------------------------------------------------------------------ violet_setup init

def test_init_creates_the_database_a_one_time_password_and_selects_the_tuned_prompts(env_guard, monkeypatch, tmp_path):
    monkeypatch.setenv("VIOLET_DB_PATH", str(tmp_path / "data" / "violet.db"))
    monkeypatch.delenv("CHAT_LLM_PROMPT", raising=False)
    monkeypatch.delenv("PROACTIVE_LLM_PROMPT", raising=False)
    monkeypatch.setenv("VIOLET_ENV_FILE", str(tmp_path / "none.env"))
    from tools import violet_setup
    from wx_ilink_py.auth import AdminStore
    first = violet_setup.init()
    password = first["admin"]["password"]
    assert first["admin"]["created"] and first["admin"]["must_change"] and len(password) == 20
    assert (tmp_path / "data" / "violet.db").exists()
    assert first["prompts_selected"] == {"prompt.chat": "v1", "prompt.proactive": "v1"}
    store = AdminStore(env_guard)
    assert store.verify_password(password) and store.must_change()
    assert password not in (env_guard / "control" / "admin.json").read_text(encoding="utf-8")
    again = violet_setup.init()                                         # a repair keeps the password and the choices
    assert again["admin"]["created"] is False and "password" not in again["admin"] and again["prompts_selected"] == {}
    assert store.verify_password(password)
    reset = violet_setup.init(reset_admin=True)
    assert reset["admin"]["password"] != password and not store.verify_password(password)
