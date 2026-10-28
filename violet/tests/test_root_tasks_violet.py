"""Violet 0.7.0: work only root can do, asked for from the Control Center (install the phone relay, change its port),
progress files for those and for updates (the page's progress dialog), and a purge that removes everything even when
install.json is gone. Fake systemctl / firewall tools, temp folders; no network, nothing installed."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tools import violet_installer as inst
from tools import violet_relay as relay

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}


class Runs:
    def __init__(self, outputs=None):
        self.calls, self.outputs = [], outputs or {}

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=self.outputs.get(tuple(cmd), ""), stderr="")


def _layout(tmp_path, **state):
    layout = inst.Layout(tmp_path / "root")
    for folder in (layout.config, layout.data / "control", layout.app):
        folder.mkdir(parents=True, exist_ok=True)
    (layout.app / "VERSION").write_text("0.7.0\n", encoding="utf-8")
    inst.save_state(layout, {"version": "0.7.0", "service": "systemd", "user": "violet", **state})
    return layout


def _progress(layout, name):
    return json.loads(inst.progress_path(layout, name).read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------------ units / requests

def test_the_task_units_run_only_the_fixed_command():
    units = {k: v.replace("\\", "/") for k, v in inst.updater_units(inst.Layout(Path("/opt/violet"))).items()}
    assert "ExecStart=/opt/violet/violet root-task --json" in units["violet-task.service"]
    assert "User=" not in units["violet-task.service"] and "Environment" not in units["violet-task.service"]
    assert "PathExists=/opt/violet/data/control/task-request" in units["violet-task.path"]


def test_a_task_request_is_one_known_word_and_is_taken_once(tmp_path):
    layout = _layout(tmp_path)
    request = layout.data / "control" / "task-request"
    assert inst.take_task_request(layout) is None
    request.write_text("relay-install\n", encoding="utf-8")
    assert inst.take_task_request(layout) == "relay-install" and not request.exists()
    request.write_text("relay-install; rm -rf /", encoding="utf-8")
    assert inst.take_task_request(layout) == "" and not request.exists()


def test_root_task_runs_the_relay_install_with_progress(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    (layout.data / "control" / "task-request").write_text("relay-install", encoding="utf-8")
    seen = {}

    def fake(opts, action, run=None, progress=None):
        seen["action"] = action
        progress(40, "下载 violet-mcp-relay")
        assert _progress(layout, "task")["state"] == "running" and _progress(layout, "task")["percent"] == 40
        return {"ok": True, "installed": True, "device_port": 23456, "healthy": True, "notes": ["已安装"]}
    monkeypatch.setattr(inst, "mcp_relay", fake)
    result = inst.root_task(inst.Options(root=layout.root))
    assert seen["action"] == "install" and result["action"] == "relay-install"
    p = _progress(layout, "task")
    assert p["state"] == "done" and p["percent"] == 100 and p["device_port"] == 23456 and "23456" in p["message"]
    assert p["task"] == "relay-install" and p["notes"] == ["已安装"]
    assert inst.root_task(inst.Options(root=layout.root))["action"] == "none"        # nothing asked: nothing done


def test_root_task_failure_and_unknown_requests_end_as_failed(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    (layout.data / "control" / "task-request").write_text("relay-set-port", encoding="utf-8")

    def broken(opts, action, run=None, progress=None):
        raise inst.InstallError("这个中继不是 Violet 安装的")
    monkeypatch.setattr(inst, "mcp_relay", broken)
    with pytest.raises(inst.InstallError):
        inst.root_task(inst.Options(root=layout.root))
    assert _progress(layout, "task")["state"] == "failed" and "不是 Violet" in _progress(layout, "task")["message"]
    (layout.data / "control" / "task-request").write_text("format-disk", encoding="utf-8")
    monkeypatch.setattr(inst, "mcp_relay", lambda *a, **k: pytest.fail("never run for an unknown word"))
    assert inst.root_task(inst.Options(root=layout.root))["action"] == "rejected"
    assert _progress(layout, "task")["state"] == "failed"


def test_progress_replaces_a_planted_link_instead_of_following_it(tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("keep", encoding="utf-8")
    path = tmp_path / "control" / "task-progress.json"
    path.parent.mkdir()
    try:
        (path.parent / "task-progress.json.tmp").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    inst.Progress(path, None, task="x")(10, "step")
    assert target.read_text(encoding="utf-8") == "keep" and json.loads(path.read_text(encoding="utf-8"))["percent"] == 10


def test_env_progress_only_inside_the_control_folder(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    monkeypatch.setenv("VIOLET_PROGRESS_FILE", str(tmp_path / "elsewhere.json"))
    assert inst.env_progress(layout, {}).path is None
    monkeypatch.setenv("VIOLET_PROGRESS_FILE", str(inst.progress_path(layout, "update")))
    assert inst.env_progress(layout, {}).path == inst.progress_path(layout, "update")


# ------------------------------------------------------------------------------------------------ updates

NEWER = {"version": "0.7.1", "tag": "v0.7.1", "url": "https://github.com/x/y/releases/tag/v0.7.1",
         "install_url": "https://github.com/x/y/releases/download/v0.7.1/install.sh"}


def test_auto_update_reports_progress_from_start_to_finish(tmp_path):
    layout = _layout(tmp_path)
    (layout.data / "control" / "update-request").write_text("manual", encoding="utf-8")
    steps = []

    def do_update(opts, release, progress=None):
        progress(15, "下载并校验")
        steps.append(_progress(layout, "update")["percent"])
        return {"updated": True, "to": release["version"]}
    inst.auto_update(inst.Options(root=layout.root), release=NEWER, do_update=do_update)
    p = _progress(layout, "update")
    assert steps == [15] and p["state"] == "done" and p["version"] == "0.7.1" and "0.7.1" in p["message"]

    def failing(opts, release, progress=None):
        raise inst.InstallError("新版本启动后健康检查没有通过")
    (layout.data / "control" / "update-request").write_text("manual", encoding="utf-8")
    with pytest.raises(inst.InstallError):
        inst.auto_update(inst.Options(root=layout.root), release=NEWER, do_update=failing)
    assert _progress(layout, "update")["state"] == "failed"


def test_update_hands_the_progress_file_to_the_release_installer(tmp_path):
    layout = _layout(tmp_path, service="supervisor")              # (systemd would need root here)
    seen = {}

    def run(cmd, **kw):
        seen["env"] = kw.get("env") or {}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": True, "to": "0.7.1"}), stderr="")
    progress = inst.Progress(inst.progress_path(layout, "update"), None, task="update")
    result = inst.update(inst.Options(root=layout.root), release=NEWER, download=lambda u, p: p.write_text("#!"),
                         run=run, windows=False, progress=progress)
    assert result["updated"] and seen["env"]["VIOLET_PROGRESS_FILE"] == str(progress.path)
    assert _progress(layout, "update")["percent"] == 15


# ------------------------------------------------------------------------------------------------ relay progress

def test_relay_install_reports_increasing_progress(tmp_path, monkeypatch):
    import hashlib
    layout, unit = type("L", (), {"root": tmp_path / "violet", "data": tmp_path / "violet" / "data"})(), tmp_path / "r.service"
    binary = b"\x7fELF" + b"x" * 4096
    monkeypatch.setattr(relay, "health", lambda port, **kw: {"ok": True})

    def fetch(url, limit, report=None):
        if url.endswith("SHA256SUMS.txt"):
            return f"{hashlib.sha256(binary).hexdigest()} *violet-mcp-relay-1.2.0-linux-amd64\n".encode()
        for got in (1024, 2048, 3072, len(binary)):
            report(got, len(binary))
        return binary
    seen = []
    relay.install(layout, {}, run=Runs(), fetch=fetch, port_free=lambda p, h: True, unit_path=unit,
                  machine="x86_64", wait=0, progress=lambda pct, step: seen.append((pct, step)))
    percents = [p for p, _ in seen]
    assert percents == sorted(percents) and percents[0] <= 5 and percents[-1] >= 95
    assert any("MB" in s for _, s in seen) and any("SHA-256" in s for _, s in seen)
    assert any(15 < p < 82 for p in percents)                     # the download itself moves the bar


# ------------------------------------------------------------------------------------------------ purge

def test_sweep_removes_only_rules_marked_violet():
    ipt = ("-P INPUT DROP\n-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT\n"
           "-A INPUT -p tcp -m tcp --dport 21473 -m comment --comment violet -j ACCEPT\n"
           "-A INPUT -p tcp -m tcp --dport 9000 -m comment --comment violetish -j ACCEPT\n")
    nft = ('table inet filter { # handle 1\n\tchain input { # handle 1\n\t\ttype filter hook input priority filter; '
           'policy drop;\n\t\ttcp dport 24567 accept comment "violet" # handle 7\n\t\ttcp dport 22 accept # handle 8\n'
           '\t}\n}\n')
    runs = Runs({("iptables", "-S"): ipt, ("ip6tables", "-S"): "", ("nft", "-a", "list", "ruleset"): nft})
    import shutil as _sh
    real = _sh.which
    try:
        inst.shutil.which = lambda name: "/usr/sbin/" + name
        removed = inst.sweep_marked_rules(runs)
    finally:
        inst.shutil.which = real
    assert ["iptables", "-D", "INPUT", "-p", "tcp", "-m", "tcp", "--dport", "21473", "-m", "comment", "--comment",
            "violet", "-j", "ACCEPT"] in runs.calls
    assert not any("9000" in c for c in runs.calls if c[:2] == ["iptables", "-D"])
    assert ["nft", "delete", "rule", "inet", "filter", "input", "handle", "7"] in runs.calls
    assert not any(c[-1] == "8" for c in runs.calls if c[:3] == ["nft", "delete", "rule"])
    assert removed == ["iptables 21473", "nftables 24567"]


def test_remove_units_takes_only_this_installations_units(tmp_path):
    layout = inst.Layout(tmp_path / "root")
    units = tmp_path / "units"
    units.mkdir()
    for name, text in inst.updater_units(layout).items():
        (units / name).write_text(text, encoding="utf-8")
    runs = Runs()
    other = inst.Layout(tmp_path / "other")
    assert inst.remove_units(other, runs, unit_dir=units) == [] and len(list(units.iterdir())) == 5
    removed = inst.remove_units(layout, runs, unit_dir=units)
    assert list(units.iterdir()) == [] and len(removed) == 5
    assert runs.calls[0][:3] == ["systemctl", "disable", "--now"]


def test_purge_without_install_json_still_cleans_up(tmp_path):
    layout = inst.Layout(tmp_path / "root")
    for name in ("app/tools", "config", "data/control", "logs", "mcp-relay"):
        (layout.root / name).mkdir(parents=True)
    (layout.root / "app" / "tools" / "violet_installer.py").write_text("#", encoding="utf-8")
    (layout.root / "violet").write_text("#!/bin/sh", encoding="utf-8")
    result = inst.uninstall(inst.Options(root=layout.root), purge=True)
    assert result["ok"] and not layout.root.exists()
    # a folder that is not Violet's is never emptied
    stranger = tmp_path / "stranger"
    (stranger / "data").mkdir(parents=True)
    (stranger / "data" / "photo.jpg").write_text("x", encoding="utf-8")
    out = inst.purge_leftovers(inst.Layout(stranger), linux_root=False)
    assert (stranger / "data" / "photo.jpg").exists() and out["leftovers"] == ["data"]
    with pytest.raises(inst.InstallError, match="没有安装"):
        inst.uninstall(inst.Options(root=tmp_path / "nothing"))          # without --purge: as before


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


def test_relay_install_from_the_page_needs_a_root_helper(admin, env_guard, monkeypatch):
    import ai.root_tasks as rt
    d = admin.get("/api/control/phone-mcp").get_json()["data"]
    assert d["task"] == {"available": False, "progress": {"state": "none"}}
    body = {"action": "install", "confirm": True}
    assert admin.post("/api/control/phone-mcp/task", json=body, headers=SAME).status_code == 409
    monkeypatch.setattr(rt, "available", lambda: True)
    assert admin.post("/api/control/phone-mcp/task", json={"action": "install"}, headers=SAME).status_code == 400
    assert admin.post("/api/control/phone-mcp/task", json={"action": "rm", "confirm": True}, headers=SAME).status_code == 400
    assert admin.post("/api/control/phone-mcp/task", json={"action": "set-port", "confirm": True},
                      headers=SAME).status_code == 400                     # no relay yet
    r = admin.post("/api/control/phone-mcp/task", json=body, headers=SAME)
    assert r.status_code == 200 and r.get_json()["data"]["state"] == "queued"
    assert (env_guard / "control" / "task-request").read_text(encoding="utf-8") == "relay-install"
    assert admin.post("/api/control/phone-mcp/task", json=body, headers=SAME).status_code == 409    # one at a time
    p = admin.get("/api/control/progress/task").get_json()["data"]
    assert p["state"] == "queued" and p["task"] == "relay-install"
    assert admin.get("/api/control/progress/secrets").status_code == 404


def test_progress_written_by_root_is_read_and_old_running_entries_are_stale(admin, env_guard):
    layout = type("L", (), {"data": env_guard})()
    inst.Progress(inst.progress_path(layout, "update"), None, task="update")(55, "安装依赖")
    p = admin.get("/api/control/progress/update").get_json()["data"]
    assert p["state"] == "running" and p["percent"] == 55 and p["step"] == "安装依赖"
    import ai.root_tasks as rt
    assert rt.read_progress("update", now=p["at"] + rt.STALE_SECONDS + 5)["state"] == "stale"


def test_update_now_starts_the_dialog_from_queued(admin, env_guard, monkeypatch):
    import ai.updates as up
    monkeypatch.setattr(up, "automatic", lambda root=None: {"available": True})
    assert admin.post("/api/control/update/apply", json={"confirm": True}, headers=SAME).status_code == 200
    assert (env_guard / "control" / "update-request").read_text(encoding="utf-8") == "manual"
    assert admin.get("/api/control/progress/update").get_json()["data"]["state"] == "queued"


def test_page_scripts_build_no_html():
    root = Path(__file__).resolve().parent.parent / "wx_ilink_py"
    for name in ("control.js", "control_phone.js", "control_setup.js"):
        js = (root / "static" / name).read_text(encoding="utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert sink not in js, (name, sink)
    assert "progressDialog" in (root / "static" / "control_phone.js").read_text(encoding="utf-8")
    assert "progressDialog" in (root / "static" / "control_setup.js").read_text(encoding="utf-8")
