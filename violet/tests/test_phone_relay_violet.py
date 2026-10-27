"""Violet stage 3: the phone relay on the Violet server (tools/violet_relay.py), its pairing values in the Control
Center (wx_ilink_py/phone_mcp_routes.py), the start page's links and the reorganised pages. Fake systemctl, fake
downloads, temp folders; no network, nothing installed."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from tools import violet_installer as inst
from tools import violet_relay as relay

TOKEN = os.environ["VIOLET_ADMIN_TOKEN"]
SAME = {"Origin": "http://localhost"}
ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_UNIT = (
    "[Service]\n"
    "ExecStart=/opt/violet-mcp-relay/violet-mcp-relay -data-dir /var/lib/violet-mcp-relay -device-listen :8443 "
    "-client-listen 127.0.0.1:8787\nDynamicUser=yes\n")


class Layout:
    def __init__(self, root: Path):
        self.root, self.data = root, root / "data"


class Runs:
    def __init__(self, stdout=""):
        self.calls, self.stdout = [], stdout

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=self.stdout, stderr="")


# ------------------------------------------------------------------------------------------------ parsing

def test_sums_and_the_newest_relay_for_this_cpu():
    sums = relay.parse_sums(
        "aa" * 32 + " *violet-mcp-1.4.0-release.apk\n" + "bb" * 32 + " *violet-mcp-relay-1.2.0-linux-amd64\n"
        + "cc" * 32 + "  violet-mcp-relay-1.10.0-linux-amd64\n" + "dd" * 32 + " *violet-mcp-relay-1.2.0-linux-arm64\nbroken\n")
    assert relay.pick_asset(sums, "amd64") == ("violet-mcp-relay-1.10.0-linux-amd64", "cc" * 32)
    assert relay.pick_asset(sums, "arm64")[0] == "violet-mcp-relay-1.2.0-linux-arm64"
    with pytest.raises(relay.RelayError):
        relay.pick_asset(sums, "riscv64")
    assert relay.relay_arch("x86_64") == "amd64" and relay.relay_arch("aarch64") == "arm64" and relay.relay_arch("mips") is None


def test_units_hand_deployed_and_ours_are_read_alike():
    up = relay.parse_unit(UPSTREAM_UNIT)
    assert up["binary"] == "/opt/violet-mcp-relay/violet-mcp-relay" and up["device_port"] == 8443
    assert up["client_port"] == 8787 and up["client_host"] == "127.0.0.1" and up["managed"] is False
    ours = relay.parse_unit(relay.unit_text(Path("/opt/violet/mcp-relay/violet-mcp-relay"), 8444, 8788))
    assert ours["managed"] and ours["device_port"] == 8444 and ours["client_port"] == 8788
    assert "DynamicUser=yes" in relay.unit_text(Path("/x"), 1, 2)
    tls = relay.parse_unit("ExecStart=/r -client-listen :8444 -client-tls -tls-cert /etc/x/full.pem -device-listen=:9443\n")
    assert tls["client_tls"] and tls["tls_cert"] == "/etc/x/full.pem" and tls["device_port"] == 9443


# ------------------------------------------------------------------------------------------------ install / export / uninstall

def _downloads(binary: bytes, digest: str | None = None):
    asked = []

    def fetch(url, limit):
        asked.append(url)
        if url.endswith("SHA256SUMS.txt"):
            return f"{digest or hashlib.sha256(binary).hexdigest()} *violet-mcp-relay-1.2.0-linux-amd64\n".encode()
        assert url.endswith("violet-mcp-relay-1.2.0-linux-amd64")
        return binary
    return fetch, asked


def test_install_downloads_checks_registers_opens_the_port_and_exports(tmp_path, monkeypatch):
    layout, state, unit = Layout(tmp_path / "violet"), {"user": "violet", "public_ip": "203.0.113.7"}, tmp_path / "relay.service"
    fetch, asked = _downloads(b"\x7fELF relay")
    runs, opened = Runs(), []
    monkeypatch.setattr(relay, "health", lambda port, **kw: {"ok": True})
    busy = {8443}
    result = relay.install(layout, state, run=runs, fetch=fetch, port_free=lambda p, host: p not in busy,
                           open_port=lambda p: (opened.append(p) or [{"tool": "iptables", "port": p}], "已放行"),
                           unit_path=unit, machine="x86_64", wait=0)
    binary = layout.root / "mcp-relay" / "violet-mcp-relay"
    assert binary.read_bytes() == b"\x7fELF relay" and asked[0].startswith("https://github.com/aileboeth-web/violet-mcp/")
    info = relay.parse_unit(unit.read_text(encoding="utf-8"))
    assert info["managed"] and info["device_port"] == 8444 and info["client_port"] == 8787     # 8443 was taken
    assert ["systemctl", "enable", "--now", "violet-mcp-relay.service"] in runs.calls
    assert opened == [8444] and state["firewall_rules"] == [{"tool": "iptables", "port": 8444}]
    assert state["mcp_relay"]["managed"] and result["ok"] and result["healthy"]
    assert any("8443 被占用" in n for n in result["notes"]) and any("安全组" in n for n in result["notes"])
    exported = json.loads(relay.export_path(layout.data).read_text(encoding="utf-8"))
    assert exported["device_url"] == "wss://203.0.113.7:8444/device" and exported["client_url"] == "http://127.0.0.1:8787/mcp"
    assert "device_secret" not in result                       # the CLI result never carries the secret


def test_a_download_that_does_not_match_its_checksum_is_not_installed(tmp_path):
    layout, unit = Layout(tmp_path / "violet"), tmp_path / "relay.service"
    fetch, _ = _downloads(b"tampered", digest="00" * 32)
    with pytest.raises(relay.RelayError, match="SHA-256"):
        relay.install(layout, {}, run=Runs(), fetch=fetch, port_free=lambda p, h: True, unit_path=unit,
                      machine="x86_64", wait=0)
    assert not unit.exists() and not (layout.root / "mcp-relay" / "violet-mcp-relay").exists()


def test_a_hand_deployed_relay_is_used_as_it_is(tmp_path, monkeypatch):
    layout, unit, state = Layout(tmp_path / "violet"), tmp_path / "relay.service", {}
    unit.write_text(UPSTREAM_UNIT, encoding="utf-8")
    monkeypatch.setattr(relay, "health", lambda port, **kw: None)

    def no_fetch(url, limit):
        raise AssertionError("nothing is downloaded")
    result = relay.install(layout, state, run=Runs(), fetch=no_fetch, port_free=lambda p, h: True,
                           open_port=lambda p: pytest.fail("not our relay: its firewall is not ours either"),
                           unit_path=unit, wait=0)
    assert unit.read_text(encoding="utf-8") == UPSTREAM_UNIT and "mcp_relay" not in state
    assert any("已经部署了中继" in n for n in result["notes"]) and result["healthy"] is False


def test_export_reads_the_secret_and_pin_without_creating_a_certificate(tmp_path):
    layout, unit, data_dir = Layout(tmp_path / "violet"), tmp_path / "relay.service", tmp_path / "relaydata"
    data_dir.mkdir()
    (data_dir / "device.secret").write_text("s3cret-device-key\n", encoding="utf-8")
    unit.write_text(f"ExecStart=/opt/r/violet-mcp-relay -data-dir {data_dir.as_posix()} -device-listen :8443\n", encoding="utf-8")
    runs = Runs(stdout="sha256/PINVALUE=\n")
    owners = []
    out = relay.export(layout, {"public_ip": "198.51.100.2", "user": "violet"}, run=runs, unit_path=unit,
                       chown=lambda p, u: owners.append((p.name, u)))
    saved = json.loads(relay.export_path(layout.data).read_text(encoding="utf-8"))
    assert saved["device_secret"] == "s3cret-device-key" and saved["pin"] is None and runs.calls == []   # no cert yet
    assert out["device_secret_set"] and "device_secret" not in out
    assert ("control", "violet") in owners and ("mcp-relay.json.tmp", "violet") in owners
    (data_dir / "selfsigned.crt").write_text("cert", encoding="utf-8")
    relay.export(layout, {"public_ip": "198.51.100.2"}, run=runs, unit_path=unit)
    assert len(runs.calls) == 1 and runs.calls[0][0] == "/opt/r/violet-mcp-relay" and runs.calls[0][-1] == "-print-pin"
    assert Path(runs.calls[0][2]) == data_dir
    assert json.loads(relay.export_path(layout.data).read_text(encoding="utf-8"))["pin"] == "sha256/PINVALUE="
    if os.name != "nt":
        assert (relay.export_path(layout.data).stat().st_mode & 0o777) == 0o600
    unit.unlink()
    assert relay.export(layout, {}, run=runs, unit_path=unit) == {"installed": False}
    assert not relay.export_path(layout.data).exists()


def test_uninstall_removes_only_our_relay_and_its_firewall_rule(tmp_path):
    layout, unit = Layout(tmp_path / "violet"), tmp_path / "relay.service"
    unit.write_text(UPSTREAM_UNIT, encoding="utf-8")
    state = {"firewall_rules": [{"tool": "ufw", "port": 7101}]}
    assert relay.uninstall(layout, state, run=Runs(), unit_path=unit) == [] and unit.exists()
    unit.write_text(relay.unit_text(Path("/opt/violet/mcp-relay/violet-mcp-relay"), 8444, 8787), encoding="utf-8")
    (layout.root / "mcp-relay").mkdir(parents=True)
    state = {"mcp_relay": {"managed": True}, "firewall_rules": [{"tool": "ufw", "port": 7101}, {"tool": "ufw", "port": 8444}]}
    removed_rules = []
    removed = relay.uninstall(layout, state, run=Runs(), unit_path=unit, remove_rule=removed_rules.append)
    assert not unit.exists() and not (layout.root / "mcp-relay").exists()
    assert removed_rules == [{"tool": "ufw", "port": 8444}] and state["firewall_rules"] == [{"tool": "ufw", "port": 7101}]
    assert "mcp_relay" not in state and any("中继服务" in r for r in removed)


# ------------------------------------------------------------------------------------------------ Violet's own unit / CLI

def test_violet_unit_exports_relay_values_and_limits_restarts_in_the_unit_section(tmp_path):
    text = inst.systemd_unit(inst.Layout(tmp_path), "violet")
    unit_part, service_part = text.split("[Service]")
    assert "StartLimitIntervalSec=300" in unit_part and "StartLimitBurst=10" in unit_part
    assert "StartLimit" not in service_part                      # systemd ignored them there ("Unknown key")
    assert f"ExecStartPre=-+{tmp_path / 'violet'} mcp-relay export --json" in service_part
    assert "mcp-relay" not in inst.systemd_unit(inst.Layout(tmp_path), None)


def test_cli_mcp_relay_needs_an_installation(tmp_path, capsys):
    assert inst.main(["mcp-relay", "status", "--root", str(tmp_path / "none"), "--json"]) == 2
    assert "没有安装 Violet" in capsys.readouterr().out
    with pytest.raises(inst.InstallError, match="用法"):
        inst.mcp_relay(inst.Options(root=tmp_path), "explode")


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
def made(env_guard, violetdb):
    from wx_ilink_py import ILinkClient, create_app
    app = create_app(client=ILinkClient(data_dir=env_guard, auto_login=False), violetdb=violetdb, proactive_scheduler=None,
                     all_agents=[], admin_token=TOKEN)
    app.testing = True
    return app


def _admin(app):
    c = app.test_client()
    assert c.post("/auth/login", data={"token": TOKEN}, headers=SAME).status_code == 302
    return c


def test_phone_card_without_and_with_a_relay(made, env_guard):
    admin = _admin(made)
    assert made.test_client().get("/api/control/phone-mcp").status_code == 401
    d = admin.get("/api/control/phone-mcp").get_json()["data"]
    assert d["installed"] is False and d["relay"] is None and d["server"] is None
    path = env_guard / "control" / "mcp-relay.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"installed": True, "device_url": "wss://203.0.113.7:8443/device", "device_port": 8443,
                                "device_secret": "s3cret-device-key", "pin": "sha256/PIN=",
                                "client_url": "http://127.0.0.1:1/mcp"}), encoding="utf-8")
    d = admin.get("/api/control/phone-mcp").get_json()["data"]
    assert d["installed"] and d["relay"]["device_secret"] == "s3cret-device-key" and d["relay"]["pin"] == "sha256/PIN="
    assert d["health"] == {"checked": True, "reachable": False, "device_connected": False}
    assert d["app_url"].startswith("https://github.com/aileboeth-web/violet-mcp/")


def test_pasting_the_phone_token_adds_the_android_phone_server(made, env_guard, monkeypatch):
    import ai.tool_manager as tm
    monkeypatch.setattr(tm, "refresh", lambda name=None: None)          # no connection attempt
    admin = _admin(made)
    bad = admin.post("/api/control/phone-mcp/connect", json={"token": "short"}, headers=SAME)
    assert bad.status_code == 400 and "访问令牌" in bad.get_json()["error"]
    token = "Abc123_-xyz.TOKEN-0123456789"
    ok = admin.post("/api/control/phone-mcp/connect", json={"token": "Bearer " + token}, headers=SAME)
    assert ok.status_code == 200, ok.get_json()
    store = tm.tool_store()
    entry = next(s for s in store.custom_servers() if s["name"] == "android-phone")
    assert entry["url"] == "http://127.0.0.1:8787/mcp" and entry["agents"] == ["chat"] and entry["tool_profile"] == "android-phone"
    assert store.secret_headers()["android-phone"]["Authorization"] == "Bearer " + token
    again = admin.post("/api/control/phone-mcp/connect", json={"token": token + "NEW"}, headers=SAME)
    assert again.status_code == 200
    assert tm.tool_store().secret_headers()["android-phone"]["Authorization"] == "Bearer " + token + "NEW"
    assert sum(1 for s in tm.tool_store().custom_servers() if s["name"] == "android-phone") == 1


def test_start_page_items_link_to_the_exact_place(made):
    items = _admin(made).get("/api/control/setup").get_json()["data"]["items"]
    by_id = {i["id"]: i for i in items}
    assert (by_id["model_service"]["tab"], by_id["model_service"]["anchor"]) == ("model", "model-api")
    assert (by_id["agent_models"]["tab"], by_id["agent_models"]["anchor"]) == ("model", "model-agents")
    assert by_id["tools"]["anchor"] == "phone-mcp" and by_id["features"]["anchor"] == "set-g-proactive"
    html = (ROOT / "wx_ilink_py" / "templates" / "control.html").read_text(encoding="utf-8")
    for item in items:
        if item["anchor"] and item["tab"] != "channels":
            assert f'id="{item["anchor"]}"' in html or item["anchor"].startswith("set-g-"), item


def test_pages_are_reorganised_and_build_no_html():
    html = (ROOT / "wx_ilink_py" / "templates" / "control.html").read_text(encoding="utf-8")
    for needle in ('data-tab="model"', 'id="tab-model"', 'id="model-fetch"', 'id="set-jump"', 'id="ch-jump"',
                   'id="tools-jump"', 'id="phone-mcp"', 'id="ch-add-box"', 'id="ch-pair-box"', 'id="mcp-add-box"',
                   '<script src="/static/control_model.js"></script>', '<script src="/static/control_phone.js"></script>'):
        assert needle in html, needle
    assert 'id="setup-api"' not in html and "初始化向导" not in html                 # moved to the model page
    assert html.index('data-tab="model"') < html.index('data-tab="settings"')
    for name in ("control_model.js", "control_phone.js", "control_setup.js", "control.js"):
        js = (ROOT / "wx_ilink_py" / "static" / name).read_text(encoding="utf-8")
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                     "localStorage", "sessionStorage"):
            assert sink not in js, (name, sink)
    js = (ROOT / "wx_ilink_py" / "static" / "control.js").read_text(encoding="utf-8")
    assert 'SETTINGS_ELSEWHERE = ["api", "models"]' in js
