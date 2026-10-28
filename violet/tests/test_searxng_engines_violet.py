"""Violet 0.7.2 / 0.7.3: which search engines the private SearXNG uses -- the China set, the admin's own choice from a
fixed list, IPv4 only, the automatic choice, failure reasons, and the page's routes. Fake searches and systemctl."""

from __future__ import annotations

import json
import os

import pytest

from tools import violet_installer as inst
from tools import violet_searxng as sx

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}


def test_settings_for_a_chosen_list_and_ipv4_only():
    text = sx.settings_text(18089, "cn")                    # 0.7.2's stored value still works
    assert "keep_only:" in text and "use_default_settings: true" not in text and "source_ips" not in text
    for engine in sx.CN_ENGINES:
        assert f"      - {engine}" in text and f"  - name: {engine}\n    disabled: false" in text
    assert "use_default_settings: true" in sx.settings_text(18089) and "keep_only" not in sx.settings_text(18089)
    own = sx.settings_text(18089, {"engines": ["bing", "evil\n  - injected", "sogou"], "ipv4": True})
    assert "      - bing" in own and "      - sogou" in own and "injected" not in own
    assert 'source_ips:\n    - "0.0.0.0"' in own
    assert sx.normalize({"engines": ["nope"]})["engines"] == "default"
    assert sx.describe({"engines": ["bing", "baidu"], "ipv4": True}) == "必应、百度（只用 IPv4）"


def test_failure_reasons_are_readable():
    assert sx._reason("timeout") == "超时" and sx._reason("Suspended: CAPTCHA") == "要求人机验证"
    assert sx._reason("HTTP error") == "网站返回错误" and sx._reason("something new") == "something new"


def _searches(*counts):
    it = iter(counts)

    def search(port):
        n = next(it)
        return {"answered": True, "results": n, "dead": [] if n else ["bing（超时）"], "by_engine": {},
                "note": f"{n} 条" + ("" if n else "；没有响应的搜索引擎：bing（超时）")}
    return search


def test_the_automatic_choice_tries_china_and_ipv4_only(tmp_path):
    base = tmp_path / "searxng"
    base.mkdir()
    runs = []
    run = lambda cmd, **kw: runs.append(cmd)               # noqa: E731
    choice, tested, notes = sx.choose_engines(base, 18089, "default", run=run, wait=0, check_health=lambda p: True,
                                              search=_searches(0, 7))
    assert choice == {"engines": list(sx.CN_ENGINES), "ipv4": False} and tested["results"] == 7
    assert "keep_only" in (base / "settings.yml").read_text(encoding="utf-8")
    # nothing from the defaults nor the China set: the China set with IPv4 only finds results
    choice, tested, notes = sx.choose_engines(base, 18089, "default", run=run, wait=0, check_health=lambda p: True,
                                              search=_searches(0, 0, 5))
    assert choice == {"engines": list(sx.CN_ENGINES), "ipv4": True} and "source_ips" in (base / "settings.yml").read_text(encoding="utf-8")
    # nothing anywhere: the current choice is put back and the notes say what was tried
    choice, _t, notes = sx.choose_engines(base, 18089, "default", run=run, wait=0, check_health=lambda p: True,
                                          search=_searches(0, 0, 0, 0))
    assert choice == {"engines": "default", "ipv4": False} and "keep_only" not in (base / "settings.yml").read_text(encoding="utf-8")
    assert any("都没有返回结果" in n for n in notes) and any("超时" in n for n in notes)
    before = len(runs)
    assert sx.choose_engines(base, 18089, "default", run=run, wait=0, check_health=lambda p: True,
                             search=_searches(3))[0]["engines"] == "default" and len(runs) == before


class _Layout:
    def __init__(self, tmp_path):
        self.root, self.data = tmp_path / "violet", tmp_path / "violet" / "data"


def _installed(tmp_path):
    layout = _Layout(tmp_path)
    unit = tmp_path / "violet-searxng.service"
    unit.write_text(sx.MARK + "\n", encoding="utf-8")
    (layout.root / "searxng").mkdir(parents=True)
    return layout, unit, {"searxng": {"managed": True, "port": 18089, "installed_at": 1}}


def test_tune_and_the_admins_own_choice(tmp_path):
    layout, unit, state = _installed(tmp_path)
    result = sx.tune(layout, state, run=lambda *a, **k: None, unit_path=unit, wait=0, check_health=lambda p: True,
                     search=_searches(0, 4))
    assert result["search_ok"] and state["searxng"]["engines"]["engines"] == list(sx.CN_ENGINES)
    export = json.loads(sx.export_path(layout.data).read_text(encoding="utf-8"))
    assert "必应" in export["engines_text"] and export["last_results"] == 4 and export["catalog"]
    # the page's choice: exactly that, kept even when the test finds nothing
    sx.request_path(layout.data).write_text(json.dumps({"engines": ["bing", "sogou"], "ipv4": True}), encoding="utf-8")
    result = sx.configure(layout, state, run=lambda *a, **k: None, unit_path=unit, wait=0,
                          check_health=lambda p: True, search=_searches(0))
    assert state["searxng"]["engines"] == {"engines": ["bing", "sogou"], "ipv4": True} and not result["search_ok"]
    assert not sx.request_path(layout.data).exists()
    assert 'source_ips' in (layout.root / "searxng" / "settings.yml").read_text(encoding="utf-8")
    # a choice that does not start goes back
    sx.request_path(layout.data).write_text(json.dumps({"engines": ["google"]}), encoding="utf-8")
    health = iter([False, True])
    with pytest.raises(sx.SearxngError, match="没有启动"):
        sx.configure(layout, state, run=lambda *a, **k: None, unit_path=unit, wait=0,
                     check_health=lambda p: next(health, True), search=_searches(1))
    assert state["searxng"]["engines"] == {"engines": ["bing", "sogou"], "ipv4": True}
    with pytest.raises(sx.SearxngError, match="没有收到"):
        sx.configure(layout, state, run=lambda *a, **k: None, unit_path=unit, wait=0)


def test_root_task_configure_word(tmp_path, monkeypatch):
    layout = inst.Layout(tmp_path / "root")
    (layout.data / "control").mkdir(parents=True)
    inst.save_state(layout, {"version": "0.7.3", "service": "systemd", "user": "violet"})
    (layout.data / "control" / "task-request").write_text("searxng-configure", encoding="utf-8")
    seen = {}

    def fake(opts, action, run=None, progress=None):
        seen["action"] = action
        return {"ok": True, "engines_text": "必应、搜狗（只用 IPv4）", "search_ok": True, "notes": ["9 条"]}
    monkeypatch.setattr(inst, "searxng_command", fake)
    inst.root_task(inst.Options(root=layout.root))
    p = json.loads(inst.progress_path(layout, "task").read_text(encoding="utf-8"))
    assert seen["action"] == "configure" and p["state"] == "done" and "必应、搜狗" in p["message"]


# ------------------------------------------------------------------------------------------------ Control Center

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
    app = create_app(client=ILinkClient(data_dir=env_guard, auto_login=False), violetdb=violetdb, proactive_scheduler=None,
                     all_agents=[], admin_token=TOKEN)
    app.testing = True
    c = app.test_client()
    assert c.post("/auth/login", data={"token": TOKEN}, headers=SAME).status_code == 302
    return c


def test_page_sends_only_listed_engines(admin, env_guard, monkeypatch):
    import ai.root_tasks as rt
    monkeypatch.setattr(rt, "available", lambda: True)
    url = "/api/control/tools/web/searxng"
    body = {"action": "configure", "confirm": True, "engines": ["bing", "baidu"], "ipv4": True}
    assert admin.post(url, json=body, headers=SAME).status_code == 409                 # not installed yet
    (env_guard / "control").mkdir(parents=True, exist_ok=True)
    (env_guard / "control" / "searxng.json").write_text(json.dumps({"url": "http://127.0.0.1:18089", "engines": "default"}),
                                                        encoding="utf-8")
    s = admin.get(url).get_json()["data"]
    assert {e["name"] for e in s["catalog"]} >= set(sx.CN_ENGINES) and s["engines"] == "default"
    assert admin.post(url, json={**body, "engines": ["bing", "rm -rf"]}, headers=SAME).status_code == 400
    assert admin.post(url, json={**body, "engines": []}, headers=SAME).status_code == 400
    assert admin.post(url, json=body, headers=SAME).status_code == 200
    assert json.loads((env_guard / "control" / "searxng-request.json").read_text(encoding="utf-8")) == \
        {"engines": ["bing", "baidu"], "ipv4": True}
    assert (env_guard / "control" / "task-request").read_text(encoding="utf-8") == "searxng-configure"


def test_confirm_dialog_asks_for_a_reason_only_where_it_is_recorded():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "wx_ilink_py" / "static" / "control.js").read_text(encoding="utf-8")
    assert '$("#confirm-reason").hidden = !opts.reason;' in js
    assert js.count("{ reason: true }") == 1                                        # deleting a memory
