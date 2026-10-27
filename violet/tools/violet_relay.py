"""`violet mcp-relay install | export | status | uninstall` -- the phone relay of Violet MCP on the Violet server
(Violet stage 3). Standard library only; run as root (the <root>/violet wrapper elevates itself).

The Android app "Violet MCP" connects OUT to a relay (wss://<server>:8443/device, TLS, pinned self-signed certificate,
device secret); Violet talks to the same relay on 127.0.0.1:8787/mcp with the access token copied from the phone.
mcp/docs/DEPLOY_RELAY.md recommends the relay on the Violet machine -- this module does that deployment:

  install    download the newest relay of the violet-mcp GitHub release (file name from its SHA256SUMS.txt, checked
             against it), /opt/violet/mcp-relay/violet-mcp-relay, systemd unit violet-mcp-relay (the upstream unit:
             DynamicUser, state in /var/lib/violet-mcp-relay), free ports (8443 / 8787 or the next free ones), open the
             device port in the host firewall (recorded like Violet's own port: put back on every start, removed by
             uninstall --purge). A relay unit that already exists (deployed by hand) is used as it is, never replaced.
  export     what the phone needs (relay address, device secret, certificate pin) and what Violet needs (the MCP URL)
             into <data>/control/mcp-relay.json, readable only by Violet's service account -- violet.service runs it
             as root before every start, so the Control Center can show the values with copy buttons. The relay's own
             files stay 0600 / 0700. Never creates the certificate (the relay does that when it first starts).
  uninstall  only a relay this module installed.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

RELAY_REPO = "aileboeth-web/violet-mcp"
UNIT_NAME = "violet-mcp-relay"
UNIT_PATH = Path("/etc/systemd/system") / f"{UNIT_NAME}.service"
DATA_DIR = Path("/var/lib/violet-mcp-relay")
PRIVATE_DATA_DIR = Path("/var/lib/private/violet-mcp-relay")      # where DynamicUser keeps it
DEVICE_PORTS = range(8443, 8500)
CLIENT_PORTS = range(8787, 8800)
MARK = "# managed by Violet (tools/violet_relay.py)"
MAX_BINARY = 64 * 1024 * 1024
HEALTH_WAIT = 20.0


class RelayError(RuntimeError):
    pass


def export_path(data: Path) -> Path:
    return data / "control" / "mcp-relay.json"


def relay_arch(machine: str | None = None) -> str | None:
    m = (machine or platform.machine() or "").lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(m)


def parse_sums(text: str) -> dict[str, str]:
    """SHA256SUMS.txt lines: "<64 hex> *<file name>" (or two spaces)."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(\S+)\s*$", line.strip())
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


def _version(name: str) -> tuple[int, ...]:
    m = re.search(r"-(\d+)\.(\d+)\.(\d+)-", name)
    return tuple(int(x) for x in m.groups()) if m else (0,)


def pick_asset(sums: dict[str, str], arch: str) -> tuple[str, str]:
    names = [n for n in sums if re.fullmatch(rf"violet-mcp-relay-\d+\.\d+\.\d+-linux-{arch}", n)]
    if not names:
        raise RelayError(f"最新的 Violet MCP 发布里没有 linux-{arch} 的中继程序")
    name = max(names, key=_version)
    return name, sums[name]


def parse_unit(text: str) -> dict[str, Any]:
    """The relay's settings from its unit's ExecStart (ours or one deployed by hand from DEPLOY_RELAY.md)."""
    line = next((ln for ln in text.splitlines() if ln.strip().startswith("ExecStart=")), "")
    try:
        args = shlex.split(line.strip()[len("ExecStart="):].lstrip("-+@!:"))
    except ValueError:
        args = []
    out: dict[str, Any] = {"binary": args[0] if args else None, "data_dir": str(DATA_DIR), "device_port": 8443,
                           "device_path": "/device", "client_host": "127.0.0.1", "client_port": 8787,
                           "client_tls": False, "tls_cert": None, "managed": MARK in text}
    flags = {"-data-dir": "data_dir", "-device-path": "device_path", "-tls-cert": "tls_cert"}
    i = 1
    while i < len(args):
        arg, value = args[i], None
        if "=" in arg and arg.startswith("-"):
            arg, value = arg.split("=", 1)
        elif i + 1 < len(args) and not args[i + 1].startswith("-"):
            value = args[i + 1]
            i += 1
        arg = "-" + arg.lstrip("-")
        if arg in flags and value:
            out[flags[arg]] = value
        elif arg == "-device-listen" and value:
            out["device_port"] = int(value.rsplit(":", 1)[-1])
        elif arg == "-client-listen" and value:
            host, _, port = value.rpartition(":")
            out["client_host"], out["client_port"] = (host or "127.0.0.1"), int(port)
        elif arg == "-client-tls":
            out["client_tls"] = value is None or value.lower() in ("1", "true")
        i += 1
    return out


def unit_text(binary: Path, device_port: int, client_port: int) -> str:
    """The upstream unit (mcp/relay/deploy/violet-mcp-relay.service) with Violet's paths and ports."""
    return "\n".join([
        "[Unit]", "Description=Violet MCP relay (phone <-> Violet, TLS)", MARK,
        "After=network-online.target", "Wants=network-online.target", "",
        "[Service]",
        f"ExecStart={binary} -data-dir {DATA_DIR} -device-listen :{device_port} -client-listen 127.0.0.1:{client_port}",
        "Restart=always", "RestartSec=3", "DynamicUser=yes", f"StateDirectory={UNIT_NAME}", "StateDirectoryMode=0700",
        "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes",
        "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectControlGroups=yes",
        "RestrictAddressFamilies=AF_INET AF_INET6", "LockPersonality=yes", "MemoryDenyWriteExecute=yes",
        "SystemCallArchitectures=native", "", "[Install]", "WantedBy=multi-user.target", ""])


def _fetch(url: str, limit: int, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "violet-installer"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 -- fixed https GitHub URLs
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise RelayError(f"下载的文件太大：{url}")
    return data


def release_url(name: str, repo: str = RELAY_REPO) -> str:
    return f"https://github.com/{repo}/releases/latest/download/{name}"


def health(client_port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(f"http://{host}:{client_port}/health")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read(4096).decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _first_free(ports: range, host: str, port_free: Callable[[int, str], bool], taken: set[int] = frozenset()) -> int:
    for port in ports:
        if port not in taken and port_free(port, host):
            return port
    raise RelayError(f"端口 {ports.start}–{ports.stop - 1} 都被占用了")


# ------------------------------------------------------------------------------------------------ commands

def install(layout: Any, state: dict[str, Any], *, run: Callable[..., Any] = subprocess.run,
            fetch: Callable[[str, int], bytes] = _fetch, port_free: Callable[[int, str], bool],
            open_port: Callable[[int], tuple[list[dict], str | None]] | None = None,
            unit_path: Path = UNIT_PATH, machine: str | None = None, wait: float = HEALTH_WAIT,
            public_ip: Callable[[], str | None] | None = None) -> dict[str, Any]:
    """Returns the result; updates `state` (install.json) -- the caller saves it."""
    if not shutil.which("systemctl") and run is subprocess.run:
        raise RelayError("这台服务器没有 systemd，中继需要手动部署（见 Violet MCP 的 DEPLOY_RELAY.md）")
    notes: list[str] = []
    if unit_path.exists():
        info = parse_unit(unit_path.read_text(encoding="utf-8"))
        if not info["managed"]:
            notes.append("这台服务器上已经部署了中继（violet-mcp-relay 服务），直接使用它，没有改动")
        run(["systemctl", "enable", "--now", f"{UNIT_NAME}.service"], capture_output=True)
    else:
        free = port_free                                    # the installer's check, as for Violet's own port
        arch = relay_arch(machine)
        if not arch:
            raise RelayError(f"不支持这种 CPU：{machine or platform.machine()}（只有 amd64 / arm64 的中继）")
        sums = parse_sums(fetch(release_url("SHA256SUMS.txt"), 65536).decode("utf-8", "replace"))
        name, digest = pick_asset(sums, arch)
        data = fetch(release_url(name), MAX_BINARY)
        if hashlib.sha256(data).hexdigest() != digest:
            raise RelayError(f"{name} 的 SHA-256 和发布里的 SHA256SUMS.txt 不一致，没有安装")
        folder = layout.root / "mcp-relay"
        folder.mkdir(parents=True, exist_ok=True)
        binary = folder / "violet-mcp-relay"
        tmp = binary.with_name("violet-mcp-relay.new")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o755)
        os.replace(tmp, binary)
        device_port = _first_free(DEVICE_PORTS, "0.0.0.0", free)
        client_port = _first_free(CLIENT_PORTS, "127.0.0.1", free, {device_port})
        if device_port != DEVICE_PORTS.start:
            notes.append(f"端口 {DEVICE_PORTS.start} 被占用，手机入口改用 {device_port}")
        unit_path.write_text(unit_text(binary, device_port, client_port), encoding="utf-8")
        run(["systemctl", "daemon-reload"], capture_output=True)
        done = run(["systemctl", "enable", "--now", f"{UNIT_NAME}.service"], capture_output=True, text=True)
        if getattr(done, "returncode", 0) != 0:
            raise RelayError("中继服务启动失败：" + (getattr(done, "stderr", "") or "")[-400:] +
                             f"\n查看原因：journalctl -u {UNIT_NAME} -n 30")
        state["mcp_relay"] = {"managed": True, "asset": name, "device_port": device_port, "client_port": client_port,
                              "installed_at": int(time.time())}
        notes.append(f"已安装 {name}")
        info = parse_unit(unit_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + wait
    ok = health(info["client_port"]) is not None
    while not ok and time.monotonic() < deadline:
        time.sleep(1)
        ok = health(info["client_port"]) is not None
    if not ok:
        notes.append(f"中继 {wait:.0f} 秒内没有响应健康检查：journalctl -u {UNIT_NAME} -n 30")
    if open_port is not None and (state.get("mcp_relay") or {}).get("managed"):
        records, note = open_port(info["device_port"])
        old = [r for r in state.get("firewall_rules") or [] if r not in records]
        state["firewall_rules"] = old + records
        if note:
            notes.append(note)
    notes.append(f"云服务器的安全组（防火墙）也要放行 TCP {info['device_port']}，手机才能连上")
    result = export(layout, state, run=run, unit_path=unit_path, public_ip=public_ip)
    return {**result, "ok": True, "healthy": ok, "notes": notes}


def export(layout: Any, state: dict[str, Any], *, run: Callable[..., Any] = subprocess.run,
           unit_path: Path = UNIT_PATH, public_ip: Callable[[], str | None] | None = None,
           chown: Callable[[Path, str], None] | None = None) -> dict[str, Any]:
    target = export_path(layout.data)
    if not unit_path.exists():
        target.unlink(missing_ok=True)
        return {"installed": False}
    info = parse_unit(unit_path.read_text(encoding="utf-8"))
    data_dir = Path(info["data_dir"])
    secret = None
    try:
        secret = (data_dir / "device.secret").read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    pin = None
    if not info["tls_cert"] and info["binary"] and (data_dir / "selfsigned.crt").exists():
        # only read: with the certificate missing, -print-pin would create one owned by root
        done = run([info["binary"], "-data-dir", str(data_dir), "-print-pin"], capture_output=True, text=True,
                   timeout=15)
        lines = [ln.strip() for ln in (getattr(done, "stdout", "") or "").splitlines() if ln.strip()]
        pin = lines[-1] if getattr(done, "returncode", 1) == 0 and lines else None
    host = state.get("public_ip") or (public_ip() if public_ip else None)
    scheme = "https" if info["client_tls"] else "http"
    out = {"installed": True, "managed": info["managed"], "exported_at": int(time.time()),
           "device_host": host, "device_port": info["device_port"], "device_path": info["device_path"],
           "device_url": f"wss://{host}:{info['device_port']}{info['device_path']}" if host else None,
           "device_secret": secret, "pin": pin, "formal_certificate": bool(info["tls_cert"]),
           "client_url": f"{scheme}://{info['client_host']}:{info['client_port']}/mcp",
           "asset": (state.get("mcp_relay") or {}).get("asset")}
    user = state.get("user")
    can_chown = chown is not None or hasattr(os, "chown")
    created = not target.parent.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    if created and user and can_chown:
        (chown or _chown)(target.parent, user)      # Violet writes its update request into the same folder
    tmp = target.with_name("mcp-relay.json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    if user and can_chown:
        (chown or _chown)(tmp, user)
    os.replace(tmp, target)
    return {k: v for k, v in out.items() if k != "device_secret"} | {"device_secret_set": bool(secret)}


def _chown(path: Path, user: str) -> None:
    try:
        shutil.chown(path, user, user)
    except (LookupError, OSError):
        pass


def status(state: dict[str, Any], unit_path: Path = UNIT_PATH) -> dict[str, Any]:
    if not unit_path.exists():
        return {"installed": False}
    info = parse_unit(unit_path.read_text(encoding="utf-8"))
    h = health(info["client_port"])
    return {"installed": True, "managed": info["managed"], "device_port": info["device_port"],
            "client_port": info["client_port"], "healthy": h is not None,
            "device_connected": bool(h and h.get("deviceConnected"))}


def uninstall(layout: Any, state: dict[str, Any], *, run: Callable[..., Any] = subprocess.run,
              unit_path: Path = UNIT_PATH, remove_rule: Callable[[dict], None] | None = None) -> list[str]:
    """Only a relay this module installed (marked unit). Returns what was removed; updates `state`."""
    removed: list[str] = []
    if not unit_path.exists() or not parse_unit(unit_path.read_text(encoding="utf-8"))["managed"]:
        export_path(layout.data).unlink(missing_ok=True)
        return removed
    port = parse_unit(unit_path.read_text(encoding="utf-8"))["device_port"]
    run(["systemctl", "disable", "--now", f"{UNIT_NAME}.service"], capture_output=True)
    unit_path.unlink(missing_ok=True)
    run(["systemctl", "daemon-reload"], capture_output=True)
    removed.append(f"中继服务 {UNIT_NAME}")
    for folder in (layout.root / "mcp-relay", PRIVATE_DATA_DIR):
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
    if DATA_DIR.is_symlink():
        DATA_DIR.unlink(missing_ok=True)
    elif DATA_DIR.exists():
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    rules = state.get("firewall_rules") or []
    mine = [r for r in rules if int(r.get("port") or 0) == port]
    for rule in mine:
        if remove_rule is not None:
            remove_rule(rule)
    if mine:
        state["firewall_rules"] = [r for r in rules if r not in mine]
        removed.append(f"防火墙规则 {port}/tcp")
    state.pop("mcp_relay", None)
    export_path(layout.data).unlink(missing_ok=True)
    return removed
