"""Violet 0.7.1: the one-click private SearXNG (tools/violet_searxng.py) -- source from GitHub into its own venv, a
systemd service on 127.0.0.1, the export Violet reads, uninstall, the root task and the Control Center routes. Fake
download / pip / systemctl, temp folders; no network, nothing installed."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from tools import violet_installer as inst
from tools import violet_searxng as sx

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}


class Runs:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if cmd[1:3] == ["-m", "venv"]:
            (Path(cmd[3]) / "bin").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


class Layout:
    def __init__(self, root: Path):
        self.root, self.data = root, root / "data"


def _source(extra: dict[str, bytes] | None = None, top: str = "searxng-master") -> bytes:
    buf = io.BytesIO()
    files = {f"{top}/searx/webapp.py": b"app = None\n", f"{top}/requirements.txt": b"flask\n", **(extra or {})}
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _install(tmp_path, *, stream=None, state=None, port_free=lambda p, h: True, **kw):
    layout, unit = Layout(tmp_path / "violet"), tmp_path / "violet-searxng.service"
    seen = []
    data = _source()

    def fetch(url, limit, report=None):
        assert url == sx.SOURCE_URL
        report(len(data) // 2, len(data))
        report(len(data), len(data))
        return data

    def ok_stream(cmd, on_line, env=None):
        on_line("Collecting flask\n")
        if cmd[-1] == "granian":
            (layout.root / "searxng" / "venv" / "bin" / "granian").write_text("#!", encoding="utf-8")
        return 0, ""
    state = {"user": "violet"} if state is None else state
    result = sx.install(layout, state, python=Path("/usr/bin/python3"), port_free=port_free, run=Runs(), fetch=fetch,
                        stream=stream or ok_stream, progress=lambda p, s: seen.append((p, s)), unit_path=unit, wait=0,
                        check_health=lambda port: True, search=lambda port: (True, "测试搜索返回 3 条结果"), **kw)
    return layout, unit, state, result, seen


def test_install_puts_searxng_on_loopback_and_hands_violet_the_url(tmp_path):
    layout, unit, state, result, seen = _install(tmp_path)
    base = layout.root / "searxng"
    assert (base / "src" / "searx" / "webapp.py").is_file() and not (base / "work").exists()
    text = unit.read_text(encoding="utf-8").replace("\\", "/")
    assert sx.MARK in text and "--host 127.0.0.1 --port 18089" in text and "searx.webapp:app" in text
    assert "DynamicUser=yes" in text and "EnvironmentFile=" in text and "ProtectSystem=strict" in text
    settings = (base / "settings.yml").read_text(encoding="utf-8")
    assert 'bind_address: "127.0.0.1"' in settings and "port: 18089" in settings and "- json" in settings
    assert "limiter: false" in settings and "SEARXNG_SECRET=" not in settings
    secret = (base / "secret.env").read_text(encoding="utf-8")
    assert secret.startswith("SEARXNG_SECRET=") and len(secret.strip()) > 60
    export = json.loads(sx.export_path(layout.data).read_text(encoding="utf-8"))
    assert export["url"] == "http://127.0.0.1:18089" and export["installed"] and export["search_ok"]
    assert state["searxng"]["managed"] and state["searxng"]["port"] == 18089 and state["searxng"]["server"] == "granian"
    assert result["url"] == "http://127.0.0.1:18089" and any("3 条" in n for n in result["notes"])
    percents = [p for p, _ in seen]
    assert percents == sorted(percents) and percents[-1] >= 95 and any("MB" in s for _, s in seen)


def test_reinstall_keeps_port_and_secret_and_a_busy_default_port_gives_a_random_one(tmp_path):
    busy = {18089}
    layout, unit, state, _r, _s = _install(tmp_path, port_free=lambda p, h: p not in busy)
    port = state["searxng"]["port"]
    assert port != 18089 and 20000 <= port < 30000
    secret = (layout.root / "searxng" / "secret.env").read_text(encoding="utf-8")
    layout2, unit2, state2, _r2, _s2 = _install(tmp_path, state=state, port_free=lambda p, h: False)
    assert state2["searxng"]["port"] == port
    assert (layout.root / "searxng" / "secret.env").read_text(encoding="utf-8") == secret


def test_pip_failure_stops_but_a_missing_granian_falls_back(tmp_path):
    def broken(cmd, on_line, env=None):
        return (1, "ERROR: No matching distribution") if "-r" in cmd else (0, "")
    with pytest.raises(sx.SearxngError, match="依赖失败"):
        _install(tmp_path / "a", stream=broken)

    def no_granian(cmd, on_line, env=None):
        return (1, "no") if cmd[-1] == "granian" else (0, "")
    layout, unit, state, result, _ = _install(tmp_path / "b", stream=no_granian)
    assert "-m searx.webapp" in unit.read_text(encoding="utf-8") and state["searxng"]["server"] == "searx.webapp"
    assert any("granian" in n for n in result["notes"])


def test_a_foreign_unit_is_left_alone(tmp_path):
    unit = tmp_path / "violet-searxng.service"
    unit.write_text("[Service]\nExecStart=/somebody/else\n", encoding="utf-8")
    with pytest.raises(sx.SearxngError, match="不是 Violet"):
        sx.install(Layout(tmp_path / "v"), {}, python=Path("p"), port_free=lambda p, h: True, run=Runs(),
                   fetch=lambda *a, **k: pytest.fail("nothing downloaded"), unit_path=unit, wait=0)
    assert sx.uninstall(Layout(tmp_path / "v"), {}, run=Runs(), unit_path=unit, cache_dirs=()) == []
    assert unit.exists()


def test_the_source_archive_is_unpacked_safely(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    archive.write_bytes(_source({"searxng-master/../../evil.txt": b"x"}))
    with pytest.raises(sx.SearxngError, match="不安全"):
        sx.safe_extract(archive, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("searxng-master/link")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(info)
        data = b"ok"
        info = tarfile.TarInfo("searxng-master/searx/webapp.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    archive.write_bytes(buf.getvalue())
    top = sx.safe_extract(archive, tmp_path / "out2")
    assert (top / "searx" / "webapp.py").exists() and not (top / "link").exists()


def test_uninstall_removes_everything_it_installed(tmp_path):
    layout, unit, state, _r, _s = _install(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    runs = Runs()
    removed = sx.uninstall(layout, state, run=runs, unit_path=unit, cache_dirs=(cache,))
    assert not unit.exists() and not (layout.root / "searxng").exists() and not cache.exists()
    assert not sx.export_path(layout.data).exists() and "searxng" not in state
    assert ["systemctl", "disable", "--now", "violet-searxng.service"] in runs.calls and removed


def test_root_task_dispatches_searxng_with_progress(tmp_path, monkeypatch):
    layout = inst.Layout(tmp_path / "root")
    (layout.data / "control").mkdir(parents=True)
    inst.save_state(layout, {"version": "0.7.1", "service": "systemd", "user": "violet"})
    (layout.data / "control" / "task-request").write_text("searxng-install", encoding="utf-8")

    def fake(opts, action, run=None, progress=None):
        progress(50, "安装依赖")
        return {"ok": True, "url": "http://127.0.0.1:18089", "notes": ["测试搜索返回 3 条结果"]}
    monkeypatch.setattr(inst, "searxng_command", fake)
    monkeypatch.setattr(inst, "mcp_relay", lambda *a, **k: pytest.fail("not the relay"))
    inst.root_task(inst.Options(root=layout.root))
    p = json.loads(inst.progress_path(layout, "task").read_text(encoding="utf-8"))
    assert p["state"] == "done" and p["url"] == "http://127.0.0.1:18089" and "18089" in p["message"]


def test_cli_searxng_needs_root_except_status(tmp_path):
    layout = inst.Layout(tmp_path / "root")
    inst.save_state(layout, {"version": "0.7.1", "service": "systemd"})
    assert inst.searxng_command(inst.Options(root=layout.root), "status")["installed"] is False
    if os.name == "nt" or not inst._is_root():
        with pytest.raises(inst.InstallError, match="root"):
            inst.searxng_command(inst.Options(root=layout.root), "install")
    with pytest.raises(inst.InstallError, match="用法"):
        inst.searxng_command(inst.Options(root=layout.root), "explode")


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


def test_page_installs_enables_and_disables_web_search(admin, env_guard, monkeypatch):
    import ai.root_tasks as rt
    url = "/api/control/tools/web/searxng"
    s = admin.get(url).get_json()["data"]
    assert s["installed"] is False and s["available"] is False
    assert admin.post(url, json={"action": "install", "confirm": True}, headers=SAME).status_code == 409
    monkeypatch.setattr(rt, "available", lambda: True)
    assert admin.post(url, json={"action": "install"}, headers=SAME).status_code == 400
    assert admin.post(url, json={"action": "install", "confirm": True}, headers=SAME).status_code == 200
    assert (env_guard / "control" / "task-request").read_text(encoding="utf-8") == "searxng-install"
    assert admin.post(url, json={"action": "enable", "confirm": True}, headers=SAME).status_code == 409   # not yet
    (env_guard / "control" / "searxng.json").write_text(json.dumps({"installed": True, "url": "http://127.0.0.1:18089"}),
                                                        encoding="utf-8")
    assert admin.post(url, json={"action": "enable", "confirm": True}, headers=SAME).status_code == 200
    s = admin.get(url).get_json()["data"]
    assert s["installed"] and s["web_enabled"] and s["in_use"] and s["url"] == "http://127.0.0.1:18089"
    assert admin.post(url, json={"action": "disable", "confirm": True}, headers=SAME).status_code == 200
    assert admin.get(url).get_json()["data"]["web_enabled"] is False
    (env_guard / "control" / "searxng.json").write_text(json.dumps({"url": "http://evil.example:80"}), encoding="utf-8")
    assert admin.get(url).get_json()["data"]["installed"] is False                    # only a loopback address
