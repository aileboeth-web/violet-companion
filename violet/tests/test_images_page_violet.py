"""Violet 0.7.2: the "图片" page (drawing model from the page's settings as provider "panel", tests of recognition
and drawing), SearXNG's engine choice for servers in mainland China, and the "测试联网" button. Fake model calls,
temp data; no network."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools import violet_searxng as sx

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}
ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------------------------------------ registry

def test_the_page_model_becomes_the_panel_provider(monkeypatch):
    from ai.images.registry import ImageProviderRegistry, panel_entry_id
    monkeypatch.delenv("VIOLET_IMAGE_GEN_MODEL", raising=False)
    assert panel_entry_id() is None and not any(e.startswith("panel/") for e in ImageProviderRegistry().entries)
    monkeypatch.setenv("VIOLET_IMAGE_GEN_MODEL", "gpt-image-1")
    monkeypatch.setenv("LLM_API_URL", "https://api.example.com/v1")
    monkeypatch.delenv("VIOLET_IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("VIOLET_IMAGE_API_URL", raising=False)
    monkeypatch.setenv("VIOLET_IMAGE_DAILY_LIMIT", "5")
    entry = ImageProviderRegistry().get("panel/gpt-image-1")
    assert entry.enabled and entry.adapter_type == "openai_images" and entry.base_url == "https://api.example.com/v1"
    assert entry.api_key_env == "LLM_API_KEY" and entry.state("image_generation") == "declared"
    assert entry.limits["max_images_per_day"] == 5 and entry.pricing["unpriced_max_calls_per_day"] == 5
    first = entry.config_hash
    monkeypatch.setenv("VIOLET_IMAGE_DAILY_LIMIT", "9")                      # a new limit keeps the test result
    assert ImageProviderRegistry().get("panel/gpt-image-1").config_hash == first
    monkeypatch.setenv("VIOLET_IMAGE_API_KEY", "sk-own-image-key-123")
    monkeypatch.setenv("VIOLET_IMAGE_API_URL", "https://img.example.com/v1")
    monkeypatch.setenv("VIOLET_IMAGE_GEN_PROTOCOL", "chat_completions")
    own = ImageProviderRegistry().get("panel/gpt-image-1")
    assert own.api_key_env == "VIOLET_IMAGE_API_KEY" and own.base_url == "https://img.example.com/v1"
    assert own.adapter_type == "chat_completions" and own.config_hash != first   # another endpoint: test again


def test_the_new_settings_are_editable_and_validated():
    from ai.control_config import BY_KEY, validate
    for key in ("VIOLET_IMAGE_GEN_MODEL", "VIOLET_IMAGE_GEN_PROTOCOL", "VIOLET_IMAGE_DAILY_LIMIT",
                "VIOLET_IMAGE_API_URL", "VIOLET_IMAGE_API_KEY"):
        assert BY_KEY[key].group == "images"
    with pytest.raises(ValueError):
        validate("VIOLET_IMAGE_DAILY_LIMIT", 0)
    with pytest.raises(ValueError):
        validate("VIOLET_IMAGE_GEN_PROTOCOL", "smtp")
    assert validate("VIOLET_IMAGE_API_KEY", None) is None


# ------------------------------------------------------------------------------------------------ routes

@pytest.fixture()
def env_guard(tmp_path, monkeypatch):
    import ai.control_config as cc
    saved = dict(os.environ)
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VIOLET_MEDIA_DIR", str(tmp_path / "media"))
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


def test_images_page_state_and_the_drawing_test(admin, monkeypatch):
    monkeypatch.delenv("VIOLET_IMAGE_GEN_MODEL", raising=False)
    st = admin.get("/api/images/panel").get_json()
    assert st["gen"] is None and st["enabled"] is False and "model" in st["vision"]
    assert admin.post("/api/images/panel/test-generate", json={}, headers=SAME).status_code == 409   # cost confirm
    assert admin.post("/api/images/panel/test-generate", json={"confirm_paid": True}, headers=SAME).status_code == 400
    r = admin.put("/api/control/settings", json={"changes": {"VIOLET_IMAGE_GEN_MODEL": "gpt-image-1",
                                                             "VIOLET_IMAGE_DAILY_LIMIT": 3}}, headers=SAME)
    assert r.status_code == 200
    seen = {}

    def fake_verify(self, entry_id, capability, actor="dashboard"):
        seen.update(entry_id=entry_id, capability=capability)
        return {"entry_id": entry_id, "capability": capability, "passed": True, "task": {"asset_ids": [], "error_code": None}}
    import ai.images.service as service
    monkeypatch.setattr(service.ImageService, "verify", fake_verify)
    out = admin.post("/api/images/panel/test-generate", json={"confirm_paid": True}, headers=SAME).get_json()
    assert seen == {"entry_id": "panel/gpt-image-1", "capability": "image_generation"} and out["passed"]
    st = admin.get("/api/images/panel").get_json()
    assert st["gen"]["model"] == "gpt-image-1" and st["gen"]["daily_limit"] == 3 and st["gen"]["verified"] is False


def test_recognition_test_sends_a_small_picture(admin, monkeypatch):
    import ai.vision as vision
    sent = {}

    def fake(path):
        sent["bytes"] = Path(path).read_bytes()[:8]
        return "一个红色的方块"
    monkeypatch.setattr(vision, "describe_image", fake)
    r = admin.post("/api/images/panel/test-vision", json={}, headers=SAME).get_json()
    assert r["ok"] and "红色" in r["text"] and sent["bytes"] == b"\x89PNG\r\n\x1a\n"

    def broken(path):
        raise vision.VisionError("LLM_API_KEY 未配置")
    monkeypatch.setattr(vision, "describe_image", broken)
    r = admin.post("/api/images/panel/test-vision", json={}, headers=SAME).get_json()
    assert r["ok"] is False and "LLM_API_KEY" in r["error"]


def test_web_test_reports_search_and_page_reading(admin, monkeypatch):
    import ai.web.netguard as netguard
    import ai.web.providers as providers
    from ai.web.providers import SearchResult

    class Provider:
        def search(self, query, limit=5, recency_days=None):
            return [SearchResult("今天天气晴", "https://example.org/a", "")]
    monkeypatch.setattr(providers, "make_provider", lambda fetcher: Provider())

    class Page:
        def text(self):
            return "<html>ok</html>"
    monkeypatch.setattr(netguard.SafeFetcher, "fetch", lambda self, url, **kw: Page())
    monkeypatch.setenv("VIOLET_WEB_SEARCH_PROVIDER", "mock")
    t = admin.post("/api/control/tools/web/test", json={}, headers=SAME).get_json()["data"]
    assert t["search"]["ok"] and t["search"]["count"] == 1 and t["search"]["titles"] == ["今天天气晴"]
    assert t["fetch"]["ok"] and "dead_engines" not in t

    class Failing:
        def search(self, query, limit=5, recency_days=None):
            raise providers.SearchError("provider_error", "searxng: no search engine answered")
    monkeypatch.setattr(providers, "make_provider", lambda fetcher: Failing())

    def blocked(self, url, **kw):
        raise netguard.WebPolicyError("dns", "cannot resolve host")
    monkeypatch.setattr(netguard.SafeFetcher, "fetch", blocked)
    t = admin.post("/api/control/tools/web/test", json={}, headers=SAME).get_json()["data"]
    assert not t["search"]["ok"] and "no search engine" in t["search"]["error"]
    assert not t["fetch"]["ok"] and t["fetch"]["error"].startswith("dns")


def test_the_images_page_is_in_the_control_center():
    html = (ROOT / "wx_ilink_py" / "templates" / "control.html").read_text(encoding="utf-8")
    assert 'data-tab="images"' in html and 'id="tab-images"' in html and "/static/control_images.js" in html
    js = (ROOT / "wx_ilink_py" / "static" / "control_images.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in js
    assert "confirm_paid: true" in js and "confirmBox" in js                  # drawing costs money: asked first


# ------------------------------------------------------------------------------------------------ SearXNG engines

def test_cn_engine_settings_keep_only_engines_reachable_from_china():
    text = sx.settings_text(18089, "cn")
    assert "keep_only:" in text and "use_default_settings: true" not in text
    for engine in sx.CN_ENGINES:
        assert f"      - {engine}" in text and f"  - name: {engine}\n    disabled: false" in text
    assert "use_default_settings: true" in sx.settings_text(18089) and "keep_only" not in sx.settings_text(18089)


def _searches(*counts):
    it = iter(counts)

    def search(port):
        n = next(it)
        return {"answered": True, "results": n, "dead": [] if n else ["google"], "note": f"{n} 条"}
    return search


def test_engines_switch_to_china_when_the_defaults_find_nothing(tmp_path):
    base = tmp_path / "searxng"
    base.mkdir()
    runs = []
    run = lambda cmd, **kw: runs.append(cmd)               # noqa: E731
    engines, tested, notes = sx.choose_engines(base, 18089, "default", run=run, wait=0, check_health=lambda p: True,
                                               search=_searches(0, 7))
    assert engines == "cn" and tested["results"] == 7 and "keep_only" in (base / "settings.yml").read_text(encoding="utf-8")
    assert any("国内" in n for n in notes) and ["systemctl", "restart", "violet-searxng.service"] in runs
    # both find nothing: back to what it was
    engines, _t, notes = sx.choose_engines(base, 18089, "default", run=run, wait=0, check_health=lambda p: True,
                                           search=_searches(0, 0))
    assert engines == "default" and "keep_only" not in (base / "settings.yml").read_text(encoding="utf-8")
    assert any("都没有返回结果" in n for n in notes)
    # results with the defaults: nothing changes
    before = len(runs)
    assert sx.choose_engines(base, 18089, "default", run=run, wait=0, check_health=lambda p: True,
                             search=_searches(3))[0] == "default" and len(runs) == before


def test_tune_fixes_an_installed_searxng_and_exports_the_choice(tmp_path):
    class Layout:
        root, data = tmp_path / "violet", tmp_path / "violet" / "data"
    unit = tmp_path / "violet-searxng.service"
    unit.write_text(sx.MARK + "\n", encoding="utf-8")
    (tmp_path / "violet" / "searxng").mkdir(parents=True)
    state = {"searxng": {"managed": True, "port": 18089, "installed_at": 1}}
    result = sx.tune(Layout, state, run=lambda *a, **k: None, unit_path=unit, wait=0, check_health=lambda p: True,
                     search=_searches(0, 4))
    assert result["engines"] == "cn" and result["search_ok"] and state["searxng"]["engines"] == "cn"
    export = json.loads(sx.export_path(Layout.data).read_text(encoding="utf-8"))
    assert export["engines"] == "cn" and "必应" in export["engines_text"] and export["last_results"] == 4
    with pytest.raises(sx.SearxngError):
        sx.tune(Layout, {}, run=lambda *a, **k: None, unit_path=unit, wait=0)
