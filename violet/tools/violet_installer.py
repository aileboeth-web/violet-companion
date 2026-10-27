"""Violet Installer (phase 10.4). Standard library only: it runs before Violet's virtual environment exists.

    sudo bash install.sh                      (Linux; the script finds Python 3.11+ and runs this module)
    python tools/violet_installer.py install     (any OS; same thing)

Commands (all take --root, the install directory; default /opt/violet as root, ~/.local/share/violet
otherwise, %LOCALAPPDATA%\\Violet on Windows):

  install     check the system, copy the program, create the venv and install requirements, create config and data,
              choose a port, create the admin password, register the service (systemd / user systemd / built-in
              supervisor), start it, check its health and the network, print the address and credentials.
              Running it again REPAIRS an installation: config, data, secrets, port and password are kept.
  upgrade     install the program from this source over an installed Violet: back up data + config, keep the old program
              for rollback, migrate, restart, health check; on failure the old program is put back.
  rollback    go back to the program kept by the last upgrade.
  status      service state, health, the network layers (free / listening / this machine / LAN / public).
  backup      config + data (SQLite copied with the online backup API) into <root>/backups/.
  restore     a backup back (the service is stopped first; the current state is backed up before).
  uninstall   stop and remove the service and the program; config, data and backups stay unless --purge.
  doctor      the checks of install without changing anything.

Layout:  <root>/app (program)  venv/  config/violet.env  data/ (VIOLET_DATA_DIR, violet.db)  logs/  backups/  install.json

Design rules: nothing user-made is ever overwritten (config keys are only added, data is never touched by install /
upgrade, secrets are generated once); every step is safe to run again; "the service answers on 127.0.0.1" is never
reported as "reachable from the internet" -- what cannot be verified from this machine is reported as not verified,
with the likely reasons.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import ipaddress
import json
import os
import platform
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SOURCE_ROOT = Path(__file__).resolve().parent.parent          # the "violet" folder this installer came with
SERVICE_NAME = "violet"
DEFAULT_PORT = 7100
PORT_RANGE = range(7100, 7200)
MIN_PYTHON = (3, 11)          # the tested version; 3.10 lacks builtins Violet uses (BaseExceptionGroup in ai/mcp.py)
MIN_DISK_MB = 1024
MIN_MEMORY_MB = 512
HEALTH_TIMEOUT = 90.0
# where releases are published (GitHub Releases of this repository); VIOLET_UPDATE_REPO overrides it (forks)
UPDATE_REPO = "aileboeth-web/violet-companion"
# copied into <root>/app; user data and machine-local files never are
COPY_IGNORE = ("__pycache__", "*.pyc", ".env", ".env.*", "*.db", "*.db-wal", "*.db-shm", "data", "logs", "output",
               ".venv", ".venv-*", "*.bak", "main.pid", ".pytest_cache", "tests", "credentials.json", "*.log")


class InstallError(RuntimeError):
    """A step failed; the message says what and what to do."""


# ------------------------------------------------------------------------------------------------ paths and state

@dataclass
class Layout:
    root: Path

    @property
    def app(self) -> Path: return self.root / "app"
    @property
    def app_previous(self) -> Path: return self.root / "app.previous"
    @property
    def venv(self) -> Path: return self.root / "venv"
    @property
    def config(self) -> Path: return self.root / "config"
    @property
    def env_file(self) -> Path: return self.config / "violet.env"
    @property
    def data(self) -> Path: return self.root / "data"
    @property
    def db(self) -> Path: return self.data / "violet.db"
    @property
    def logs(self) -> Path: return self.root / "logs"
    @property
    def backups(self) -> Path: return self.root / "backups"
    @property
    def state_file(self) -> Path: return self.root / "install.json"
    @property
    def pid_file(self) -> Path: return self.root / "violet.pid"

    @property
    def python(self) -> Path:
        return self.venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def default_root() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Violet"
    if _is_root():
        return Path("/opt/violet")
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "violet"


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def load_state(layout: Layout) -> dict[str, Any]:
    try:
        data = json.loads(layout.state_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(layout: Layout, state: dict[str, Any]) -> None:
    layout.root.mkdir(parents=True, exist_ok=True)
    tmp = layout.state_file.with_name("install.json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, layout.state_file)


def source_version(source: Path = SOURCE_ROOT) -> str:
    try:
        return (source / "VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


# ------------------------------------------------------------------------------------------------ releases / updates

def update_repo() -> str:
    return (os.environ.get("VIOLET_UPDATE_REPO") or "").strip() or UPDATE_REPO


def version_key(version: str | None) -> tuple[int, int, int] | None:
    """(major, minor, patch) of "0.1.0" / "v0.1.0" / "0.1.0-rc1"; None when it is not such a version."""
    text = (version or "").strip().lstrip("vV")
    head = text.split("-", 1)[0].split("+", 1)[0]
    parts = head.split(".")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    numbers = [int(p) for p in parts] + [0] * (3 - len(parts))
    return numbers[0], numbers[1], numbers[2]


def is_newer(latest: str | None, current: str | None) -> bool:
    """True only when both are versions and latest is greater (an unknown current version is never "older")."""
    a, b = version_key(latest), version_key(current)
    return a is not None and b is not None and a > b


def latest_release(repo: str | None = None, opener: Callable[..., Any] | None = None, timeout: float = 10.0) -> dict:
    """The newest published release of the repository: {tag, version, url, published_at, notes, install_url}.
    Unauthenticated GitHub API (60 requests an hour per address -- callers cache it); system proxies apply."""
    repo = repo or update_repo()
    request = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "violet-updater"})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            data = json.loads(response.read(1_000_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise InstallError(f"{repo} 还没有发布任何版本") from None
        raise InstallError(f"查询最新版本失败：GitHub 返回 {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise InstallError(f"查询最新版本失败（网络不通或被拦截）：{exc.__class__.__name__}") from None
    tag = str(data.get("tag_name") or "")
    if not version_key(tag):
        raise InstallError(f"最新发布的标签 {tag!r} 不是版本号")
    return {"tag": tag, "version": tag.lstrip("vV"), "url": str(data.get("html_url") or ""),
            "published_at": data.get("published_at"), "notes": str(data.get("body") or "")[:4000],
            "install_url": f"https://github.com/{repo}/releases/download/{tag}/install.sh"}


# ------------------------------------------------------------------------------------------------ system checks

@dataclass
class Check:
    id: str
    ok: bool
    message: str
    level: str = "error"          # error (install stops) | warn (reported, install goes on)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "ok": self.ok, "level": self.level, "message": self.message}


def detect_platform() -> dict[str, Any]:
    info: dict[str, Any] = {"os": platform.system(), "arch": platform.machine(), "python": platform.python_version(),
                            "root": _is_root(), "distro": "", "systemd": False}
    os_release = Path("/etc/os-release")
    if os_release.is_file():
        fields = {}
        for line in os_release.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key] = value.strip().strip('"')
        info["distro"] = fields.get("PRETTY_NAME") or fields.get("ID", "")
    info["systemd"] = Path("/run/systemd/system").is_dir() and shutil.which("systemctl") is not None
    return info


def _memory_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return int(status.ullAvailPhys // (1024 * 1024))
        except Exception:
            pass
    return None


def _existing_parent(path: Path) -> Path:
    path = path.resolve()
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def preflight(layout: Layout, python: str = sys.executable, platform_info: dict | None = None,
              source: Path = SOURCE_ROOT) -> list[Check]:
    info = platform_info or detect_platform()
    checks: list[Check] = []
    version = tuple(int(x) for x in info["python"].split(".")[:2])
    checks.append(Check("python", version >= MIN_PYTHON,
                        f"Python {info['python']}" + ("" if version >= MIN_PYTHON else
                                                      f"，需要 {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 或更新")))
    supported = info["os"] in ("Linux", "Windows", "Darwin")
    checks.append(Check("os", supported, f"{info['os']} {info['distro'] or ''} {info['arch']}".strip()
                        + ("" if supported else "：不支持的系统")))
    if info["arch"].lower() not in ("x86_64", "amd64", "aarch64", "arm64"):
        checks.append(Check("arch", True, f"架构 {info['arch']} 没有测试过，依赖可能需要从源码编译", "warn"))
    try:
        import venv  # noqa: F401
        has_venv = subprocess.run([python, "-c", "import venv, ensurepip"], capture_output=True).returncode == 0
    except Exception:
        has_venv = False
    checks.append(Check("venv", has_venv, "venv / ensurepip 可用" if has_venv else
                        "缺少 venv 模块（Debian / Ubuntu：apt install python3-venv）"))
    parent = _existing_parent(layout.root)
    writable = os.access(parent, os.W_OK)
    checks.append(Check("writable", writable, f"可以写入 {parent}" if writable else
                        f"没有权限写入 {parent}（用 sudo 运行，或用 --root 指定自己的目录）"))
    free_mb = shutil.disk_usage(parent).free // (1024 * 1024)
    checks.append(Check("disk", free_mb >= MIN_DISK_MB, f"可用磁盘 {free_mb} MB"
                        + ("" if free_mb >= MIN_DISK_MB else f"，至少需要 {MIN_DISK_MB} MB")))
    memory = _memory_mb()
    if memory is not None:
        checks.append(Check("memory", memory >= MIN_MEMORY_MB, f"可用内存 {memory} MB"
                            + ("" if memory >= MIN_MEMORY_MB else f"，建议至少 {MIN_MEMORY_MB} MB"), "warn"))
    has_source = (source / "main.py").is_file() and (source / "requirements.txt").is_file()
    checks.append(Check("source", has_source, f"程序文件 {source}" if has_source else
                        f"{source} 不是完整的 Violet 程序目录"))
    if info["os"] == "Linux" and not info["systemd"]:
        checks.append(Check("service_manager", True, "没有 systemd：将使用 Violet 自带的守护进程（开机自启需要另外设置）",
                            "warn"))
    return checks


# ------------------------------------------------------------------------------------------------ ports

def port_free(port: int, host: str = "127.0.0.1") -> bool:
    """True when nothing listens on host:port (a bind succeeds)."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def probe_eve(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> dict[str, Any] | None:
    """The /healthz answer when an Violet listens there, else None."""
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                f"http://{_host_for_url(host)}:{port}/healthz", timeout=timeout) as response:
            data = json.loads(response.read(4096).decode("utf-8"))
            return data if isinstance(data, dict) and data.get("ok") and data.get("service") == "violet" else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _host_for_url(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def choose_port(requested: int | None, previous: int | None, host: str = "127.0.0.1",
                is_free: Callable[[int, str], bool] = port_free,
                is_eve: Callable[[int, str], bool] = lambda p, h: probe_eve(p, h) is not None) -> tuple[int, str]:
    """(port, reason). A port given with --port must be usable (free, or this Violet already on it) -- never silently
    replaced. Otherwise the port of the previous install is kept, then 7100, then the next free one in 7100-7199."""
    if requested is not None:
        if not 1024 <= requested <= 65535:
            raise InstallError(f"端口 {requested} 不在 1024–65535 之间")
        if is_free(requested, host):
            return requested, "按 --port 指定"
        if requested == previous and is_eve(requested, host):
            return requested, "按 --port 指定（就是正在运行的 Violet）"
        raise InstallError(f"端口 {requested} 已被其他程序占用。换一个端口，或先停止占用它的程序"
                           f"（Linux：ss -ltnp | grep :{requested}）")
    if previous:
        if is_free(previous, host) or is_eve(previous, host):
            return previous, "沿用上次安装的端口"
    for port in [DEFAULT_PORT, *PORT_RANGE]:
        if is_free(port, host):
            return port, "默认端口" if port == DEFAULT_PORT else f"{DEFAULT_PORT} 被占用，自动选了下一个空闲端口"
    raise InstallError(f"{PORT_RANGE.start}–{PORT_RANGE.stop - 1} 都被占用了；用 --port 指定一个空闲端口")


# ------------------------------------------------------------------------------------------------ network layers

def lan_addresses() -> list[str]:
    """Private IPv4 addresses of this machine (the route to a documentation address is looked up; nothing is sent)."""
    found: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            found.add(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(item[4][0])
    except OSError:
        pass
    return sorted(a for a in found if _is_private(a))


def _is_private(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_private and not ip.is_loopback and not ip.is_link_local


def tcp_connect(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def firewall_status(run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Which host firewall is active (ufw / firewalld / Windows Defender Firewall); never changes anything."""
    def out(cmd: list[str]) -> str | None:
        if shutil.which(cmd[0]) is None:
            return None
        try:
            done = run(cmd, capture_output=True, text=True, timeout=10)
            return (done.stdout or "") + (done.stderr or "")
        except Exception:
            return None
    text = out(["ufw", "status"])
    if text is not None and "Status: active" in text:
        return {"tool": "ufw", "active": True}
    text = out(["firewall-cmd", "--state"])
    if text is not None and "running" in text:
        return {"tool": "firewalld", "active": True}
    if os.name == "nt":
        text = out(["netsh", "advfirewall", "show", "currentprofile", "state"])
        if text is not None:
            return {"tool": "windows", "active": "ON" in text.upper()}
    return {"tool": None, "active": False}


def firewall_open_command(tool: str | None, port: int) -> list[str] | None:
    if tool == "ufw":
        return ["ufw", "allow", f"{port}/tcp"]
    if tool == "firewalld":
        return ["firewall-cmd", "--permanent", "--add-port", f"{port}/tcp"]
    if tool == "windows":
        return ["netsh", "advfirewall", "firewall", "add", "rule", f"name=Violet {port}", "dir=in",
                "action=allow", "protocol=TCP", f"localport={port}"]
    return None


def network_report(port: int, bind_host: str, *, public_url: str | None = None,
                   is_free: Callable[[int, str], bool] = port_free,
                   connect: Callable[[str, int], bool] = tcp_connect,
                   health: Callable[[str, int], bool] = lambda h, p: probe_eve(p, h) is not None,
                   lan: Callable[[], list[str]] = lan_addresses,
                   firewall: Callable[[], dict] = firewall_status,
                   fetch: Callable[[str], bool] | None = None) -> list[dict[str, Any]]:
    """Five separate layers; each is ok / fail / not_checked / not_applicable with plain words. A layer is never
    reported ok because a lower one is."""
    layers = []
    listening = not is_free(port, "127.0.0.1" if bind_host in ("127.0.0.1", "0.0.0.0", "localhost") else bind_host)
    layers.append({"layer": "listening", "title": "服务在监听端口", "state": "ok" if listening else "fail",
                   "detail": f"端口 {port}" + ("" if listening else " 上没有程序在监听：服务没有启动或启动失败")})
    local = health("127.0.0.1", port) if listening else False
    layers.append({"layer": "local", "title": "本机可以访问", "state": "ok" if local else "fail",
                   "detail": f"http://127.0.0.1:{port}/healthz " + ("返回正常" if local else "没有正常返回")})
    loopback_only = bind_host in ("127.0.0.1", "localhost", "::1")
    addresses = lan()
    if loopback_only:
        layers.append({"layer": "lan", "title": "局域网可以访问", "state": "not_applicable",
                       "detail": "后台只监听本机（127.0.0.1）：局域网和公网都访问不到。远程管理请用 SSH 隧道，"
                                 "或用 --access lan / --access public 安装（会启用 HTTPS）"})
    elif not addresses:
        layers.append({"layer": "lan", "title": "局域网可以访问", "state": "not_checked", "detail": "没有找到局域网地址"})
    else:
        reachable = [a for a in addresses if connect(a, port)]
        fw = firewall()
        detail = ("从本机连接局域网地址 " + "、".join(reachable) + " 成功" if reachable else
                  "连接局域网地址 " + "、".join(addresses) + " 失败")
        if fw.get("active"):
            detail += f"；本机防火墙（{fw['tool']}）已开启，其他设备能否连上取决于它是否放行端口 {port}"
        layers.append({"layer": "lan", "title": "局域网可以访问", "state": "ok" if reachable else "fail",
                       "detail": detail + "（这是从本机测的，其他设备的结果以实际为准）"})
    if loopback_only:
        layers.append({"layer": "public", "title": "公网可以访问", "state": "not_applicable", "detail": "只监听本机"})
    elif public_url and fetch is not None:
        ok = fetch(public_url)
        layers.append({"layer": "public", "title": "公网可以访问", "state": "ok" if ok else "fail",
                       "detail": f"{public_url} " + ("可以访问" if ok else "访问失败：检查云服务器安全组 / 防火墙是否放行端口 "
                                                    f"{port}、域名解析是否指向这台服务器、上游是否封了这个端口")})
    else:
        layers.append({"layer": "public", "title": "公网可以访问", "state": "not_checked",
                       "detail": "没有验证。从本机无法确认公网能否访问：云厂商安全组、NAT / 路由器端口转发、运营商封端口都"
                                 "只能从外面测。请用手机流量打开后台地址确认；安装时加 --public-url https://域名 可以让安装程序"
                                 "试一次（注意：从服务器自己访问自己的公网地址，结果不一定代表外部）"})
    return layers


# ------------------------------------------------------------------------------------------------ configuration

def parse_env(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def merge_env(existing: str, wanted: dict[str, str], header: str = "") -> tuple[str, list[str]]:
    """Add only the keys that are missing; never change or reorder what is there (the user may have edited it).
    Returns (text, added keys)."""
    have = parse_env(existing)
    added = [k for k in wanted if k not in have]
    if not added:
        return existing, []
    lines = existing.rstrip("\n").splitlines() if existing.strip() else ([header] if header else [])
    if existing.strip():
        lines.append(f"# added by the installer {_dt.date.today().isoformat()}")
    lines += [f"{k}={_quote(wanted[k])}" for k in added]
    return "\n".join(lines) + "\n", added


def _quote(value: str) -> str:
    return value if value and all(c.isalnum() or c in "._-/:\\+@" for c in value) else "'" + value.replace("'", "") + "'"


def wanted_env(layout: Layout, port: int, bind_host: str, access: str, tls: dict[str, str] | None) -> dict[str, str]:
    env = {
        "VIOLET_DATA_DIR": str(layout.data),
        "VIOLET_DB_PATH": str(layout.db),
        "VIOLET_LOG_FILE": str(layout.logs / "main.log"),
        "VIOLET_DASHBOARD_HOST": bind_host,
        "VIOLET_DASHBOARD_PORT": str(port),
        "VIOLET_TIMEZONE": _local_timezone(),
        "VIOLET_INSTANCE_ID": secrets.token_hex(8),
        "VIOLET_STARTUP_MESSAGE": "",
    }
    if tls:
        env.update(VIOLET_DASHBOARD_TLS_CERT=tls["cert"], VIOLET_DASHBOARD_TLS_KEY=tls["key"], VIOLET_DASHBOARD_COOKIE_SECURE="1")
    if access == "proxy":
        env.update(VIOLET_DASHBOARD_TRUST_PROXY="1", VIOLET_DASHBOARD_COOKIE_SECURE="1")
    return env


def _local_timezone() -> str:
    tz = os.environ.get("TZ", "").strip()
    if tz and "/" in tz:
        return tz
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if "/" in name:
            return name
    except OSError:
        pass
    return "Asia/Shanghai"


# ------------------------------------------------------------------------------------------------ TLS (IP / LAN access)

def ensure_self_signed(layout: Layout, hosts: list[str], run: Callable[..., Any] = subprocess.run) -> dict[str, str] | None:
    """A self-signed certificate for HTTPS on an IP address (made once, kept on reinstall). Needs openssl; returns None
    when it is not available (the caller then explains how to add a certificate)."""
    cert, key = layout.config / "tls" / "violet.crt", layout.config / "tls" / "violet.key"
    if cert.is_file() and key.is_file():
        return {"cert": str(cert), "key": str(key), "fingerprint": cert_fingerprint(cert)}
    if shutil.which("openssl") is None:
        return None
    cert.parent.mkdir(parents=True, exist_ok=True)
    san = ",".join(("IP:" if _is_ip(h) else "DNS:") + h for h in dict.fromkeys(["127.0.0.1", "localhost", *hosts]))
    done = run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "825",
                "-subj", "/CN=Violet", "-addext", f"subjectAltName={san}", "-keyout", str(key), "-out", str(cert)],
               capture_output=True, text=True)
    if done.returncode != 0 or not cert.is_file():
        return None
    _restrict(key)
    return {"cert": str(cert), "key": str(key), "fingerprint": cert_fingerprint(cert)}


def cert_fingerprint(path: Path) -> str:
    import ssl
    try:
        der = ssl.PEM_cert_to_DER_cert(path.read_text(encoding="ascii"))
        digest = hashlib.sha256(der).hexdigest().upper()
        return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))
    except (OSError, ValueError):                        # binascii.Error is a ValueError
        return ""


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ------------------------------------------------------------------------------------------------ program files

def copy_program(source: Path, target: Path) -> None:
    """A fresh copy of the program into target (target must not exist)."""
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*COPY_IGNORE))
    (target / "VERSION").write_text(source_version(source) + "\n", encoding="utf-8")


def install_program(layout: Layout, source: Path) -> str:
    """Put the program in place. A first install copies it; a reinstall from the same source replaces app/ but keeps
    the previous copy as app.previous until the health check passed. Returns "copied" | "same_source"."""
    if source.resolve() == layout.app.resolve():
        return "same_source"
    staging = layout.root / "app.new"
    if staging.exists():
        shutil.rmtree(staging)
    copy_program(source, staging)
    if layout.app.exists():
        if layout.app_previous.exists():
            shutil.rmtree(layout.app_previous)
        _replace(layout.app, layout.app_previous)
    _replace(staging, layout.app)
    return "copied"


def ensure_venv(layout: Layout, python: str, *, offline_wheels: Path | None = None, reuse_venv: Path | None = None,
                run: Callable[..., Any] = subprocess.run, log: Callable[[str], None] = print) -> None:
    if reuse_venv is not None:
        # development / tests: an existing environment that already has the requirements
        if layout.venv.exists() or layout.venv.is_symlink():
            return
        layout.venv.parent.mkdir(parents=True, exist_ok=True)
        (layout.root / "venv.path").write_text(str(reuse_venv), encoding="utf-8")
        return
    if not layout.python.exists():
        log("  创建 Python 虚拟环境…")
        done = run([python, "-m", "venv", str(layout.venv)], capture_output=True, text=True)
        if done.returncode != 0:
            raise InstallError("创建虚拟环境失败：" + (done.stderr or done.stdout or "")[-500:])
    cmd = [str(layout.python), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r",
           str(layout.app / "requirements.txt")]
    if offline_wheels is not None:
        cmd[4:4] = ["--no-index", "--find-links", str(offline_wheels)]
    log("  安装依赖（第一次需要几分钟）…")
    done = run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise InstallError("安装依赖失败（检查网络 / pip 源；离线安装可用 --wheels 目录）：\n"
                           + (done.stderr or done.stdout or "")[-1500:])


def venv_python(layout: Layout) -> Path:
    pointer = layout.root / "venv.path"
    if pointer.is_file():
        base = Path(pointer.read_text(encoding="utf-8").strip())
        return base / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return layout.python


def app_command(layout: Layout, *args: str, run: Callable[..., Any] = subprocess.run,
                env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a module of the installed program with its configuration (VIOLET_ENV_FILE)."""
    env = {**os.environ, "VIOLET_ENV_FILE": str(layout.env_file), "PYTHONIOENCODING": "utf-8", **(env_extra or {})}
    return run([str(venv_python(layout)), "-B", *args], cwd=str(layout.app), capture_output=True, text=True,
               encoding="utf-8", env=env)


# ------------------------------------------------------------------------------------------------ service

def systemd_unit(layout: Layout, user: str | None, user_mode: bool = False) -> str:
    lines = [
        "[Unit]",
        "Description=Violet (AI companion and Control Center)",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={layout.app}",
        f"Environment=VIOLET_ENV_FILE={layout.env_file}",
        "Environment=PYTHONUNBUFFERED=1",
        f"ExecStart={venv_python(layout)} -u main.py",
        "Restart=on-failure",
        "RestartSec=5",
        "StartLimitIntervalSec=300",
        "StartLimitBurst=10",
        "TimeoutStopSec=20",
    ]
    if user and not user_mode:
        lines += [f"User={user}", "NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=full",
                  f"ReadWritePaths={layout.root}"]
    lines += ["", "[Install]", "WantedBy=" + ("default.target" if user_mode else "multi-user.target"), ""]
    return "\n".join(lines)


class Service:
    """systemd (system) | systemd-user | supervisor (tools/violet_service.py, any OS)."""

    def __init__(self, layout: Layout, mode: str, user: str | None = None, run: Callable[..., Any] = subprocess.run):
        self.layout, self.mode, self.user, self.run = layout, mode, user, run

    @staticmethod
    def pick(platform_info: dict[str, Any], requested: str = "auto") -> str:
        if requested != "auto":
            return requested
        if platform_info.get("os") == "Linux" and platform_info.get("systemd"):
            return "systemd" if platform_info.get("root") else "systemd-user"
        return "supervisor"

    def unit_path(self) -> Path:
        if self.mode == "systemd":
            return Path("/etc/systemd/system") / f"{SERVICE_NAME}.service"
        return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / \
            f"{SERVICE_NAME}.service"

    def _ctl(self, *args: str) -> subprocess.CompletedProcess:
        cmd = ["systemctl"] + (["--user"] if self.mode == "systemd-user" else []) + list(args)
        return self.run(cmd, capture_output=True, text=True)

    def register(self, autostart: bool = True) -> str:
        if self.mode.startswith("systemd"):
            path = self.unit_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(systemd_unit(self.layout, self.user, self.mode == "systemd-user"), encoding="utf-8")
            self._ctl("daemon-reload")
            if autostart:
                self._ctl("enable", f"{SERVICE_NAME}.service")
            note = f"systemd 服务 {SERVICE_NAME}（崩溃后 5 秒自动重启" + ("，开机自启）" if autostart else "）")
            if self.mode == "systemd-user":
                note += "；用户服务要在退出登录后继续运行，请执行：sudo loginctl enable-linger " + (self.user or "$USER")
            return note
        return "Violet 自带的守护进程（崩溃后自动重启）"

    def start(self) -> None:
        if self.mode.startswith("systemd"):
            done = self._ctl("restart", f"{SERVICE_NAME}.service")
            if done.returncode != 0:
                raise InstallError("启动服务失败：" + (done.stderr or "")[-500:] +
                                   f"\n查看原因：journalctl -u {SERVICE_NAME} -n 50")
            return
        supervisor_start(self.layout, run=self.run)

    def stop(self) -> None:
        if self.mode.startswith("systemd"):
            self._ctl("stop", f"{SERVICE_NAME}.service")
            return
        supervisor_stop(self.layout)

    def state(self) -> str:
        if self.mode.startswith("systemd"):
            done = self._ctl("is-active", f"{SERVICE_NAME}.service")
            return (done.stdout or "").strip() or "unknown"
        return "active" if supervisor_pid(self.layout) else "inactive"

    def unregister(self) -> None:
        if self.mode.startswith("systemd"):
            self._ctl("disable", "--now", f"{SERVICE_NAME}.service")
            try:
                self.unit_path().unlink()
            except FileNotFoundError:
                pass
            self._ctl("daemon-reload")
        else:
            supervisor_stop(self.layout)


SERVICE_USER = "violet"


def ensure_service_user(layout: Layout, user: str, run: Callable[..., Any] = subprocess.run,
                        linux_root: bool | None = None) -> None:
    """systemd as root: Violet runs as its own system account (no login shell), which owns only what Violet writes --
    config/, data/, logs/, backups/. The program and venv stay root-owned, so the service cannot change its own code."""
    if not (linux_root if linux_root is not None else (os.name != "nt" and _is_root())):
        return
    if run(["getent", "passwd", user], capture_output=True).returncode != 0:
        nologin = "/usr/sbin/nologin" if os.path.exists("/usr/sbin/nologin") else "/sbin/nologin"
        done = run(["useradd", "--system", "--home-dir", str(layout.data), "--no-create-home", "--shell", nologin,
                    user], capture_output=True, text=True)
        if done.returncode != 0:
            raise InstallError(f"创建系统用户 {user} 失败：{(done.stderr or '').strip()[:200]}（可以用 --user 指定已有用户）")
    for folder in (layout.config, layout.data, layout.logs, layout.backups):
        folder.mkdir(parents=True, exist_ok=True)
        run(["chown", "-R", f"{user}:", str(folder)], capture_output=True)
    run(["chmod", "750", str(layout.config), str(layout.data), str(layout.backups)], capture_output=True)


def supervisor_pid(layout: Layout) -> int | None:
    try:
        pid = int(layout.pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        done = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in (done.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def supervisor_start(layout: Layout, run: Callable[..., Any] = subprocess.run) -> None:
    if supervisor_pid(layout):
        supervisor_stop(layout)
    layout.logs.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "VIOLET_ENV_FILE": str(layout.env_file), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    cmd = [str(venv_python(layout)), "-B", str(layout.app / "tools" / "violet_service.py"), "--root", str(layout.root)]
    kwargs: dict[str, Any] = {"cwd": str(layout.app), "env": env, "stdin": subprocess.DEVNULL,
                              "stdout": open(layout.logs / "supervisor.log", "ab"), "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(cmd, **kwargs)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not supervisor_pid(layout):
        if process.poll() is not None:
            raise InstallError("守护进程没能启动，见 " + str(layout.logs / "supervisor.log"))
        time.sleep(0.2)


def supervisor_stop(layout: Layout, timeout: float = 20.0) -> None:
    pid = supervisor_pid(layout)
    if not pid:
        return
    stop_flag = layout.root / "violet.stop"
    stop_flag.write_text("stop", encoding="utf-8")       # the supervisor stops Violet gracefully, then exits
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.3)
    if _pid_alive(pid):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            try:
                os.killpg(pid, 15)
            except OSError:
                pass
    stop_flag.unlink(missing_ok=True)
    layout.pid_file.unlink(missing_ok=True)


def supervisor_autostart_hint(layout: Layout) -> str:
    """How to start Violet at boot / logon when there is no systemd (not done automatically: it changes the system)."""
    if os.name == "nt":
        cli = layout.root / "violet.cmd"
        return f'schtasks /Create /SC ONLOGON /TN "violet" /TR "\\"{cli}\\" start"'
    return f'crontab -e，加一行：@reboot "{layout.root / "violet"}" start'


# ------------------------------------------------------------------------------------------------ health

def wait_healthy(port: int, host: str = "127.0.0.1", timeout: float = HEALTH_TIMEOUT, scheme: str = "http") -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if scheme == "https":
            if _https_health(host, port):
                return True
        elif probe_eve(port, host, timeout=2) is not None:
            return True
        time.sleep(1)
    return False


def _https_health(host: str, port: int) -> bool:
    import ssl
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE                     # our own self-signed certificate, on this machine
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
        with opener.open(f"https://{_host_for_url(host)}:{port}/healthz", timeout=2) as response:
            data = json.loads(response.read(4096).decode("utf-8"))
            return bool(data.get("ok")) and data.get("service") == "violet"
    except (OSError, ValueError, urllib.error.URLError):
        return False


# ------------------------------------------------------------------------------------------------ backup

RUNTIME_FILES = {"violet.lock"}          # held (locked) by the running Violet; state of the process, not data


def backup(layout: Layout, label: str = "manual") -> Path:
    """config + data in one .tar.gz. SQLite databases are copied with the online backup API (consistent while Violet
    runs); the archive is listed again and every database checked before the path is returned."""
    layout.backups.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = layout.backups / f"violet-backup-{stamp}-{label}.tar.gz"
    work = layout.backups / f".work-{stamp}-{secrets.token_hex(3)}"
    try:
        work.mkdir(parents=True)
        for name, folder in (("config", layout.config), ("data", layout.data)):
            if not folder.exists():
                continue
            for path in folder.rglob("*"):
                if not path.is_file() or path.name.endswith(("-wal", "-shm", "-journal")) or path.name in RUNTIME_FILES:
                    continue
                rel = Path(name) / path.relative_to(folder)
                dest = work / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if _is_sqlite(path):
                    _sqlite_copy(path, dest)
                else:
                    shutil.copy2(path, dest)
        state = load_state(layout)
        (work / "backup.json").write_text(json.dumps({"created_at": stamp, "label": label,
                                                       "version": state.get("version"), "root": str(layout.root)},
                                                      ensure_ascii=False, indent=1), encoding="utf-8")
        with tarfile.open(target, "w:gz") as tar:
            for item in sorted(work.iterdir()):
                tar.add(item, arcname=item.name)
        _restrict(target)
        verify_backup(target)
        return target
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _sqlite_copy(source: Path, dest: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def verify_backup(archive: Path) -> dict[str, Any]:
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        if "backup.json" not in names:
            raise InstallError(f"{archive} 不是 Violet 的备份")
        for member in tar.getmembers():
            if member.name.startswith(("/", "..")) or ".." in Path(member.name).parts or member.issym() or member.islnk():
                raise InstallError(f"备份里有不安全的路径：{member.name}")
        checked = 0
        for member in tar.getmembers():
            if member.isfile() and member.name.endswith(".db"):
                data = tar.extractfile(member).read()  # type: ignore[union-attr]
                tmp = archive.with_name(archive.name + ".check.db")
                try:
                    tmp.write_bytes(data)
                    conn = sqlite3.connect(tmp)
                    try:
                        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise InstallError(f"备份里的数据库 {member.name} 损坏")
                    finally:
                        conn.close()
                finally:
                    tmp.unlink(missing_ok=True)
                checked += 1
    return {"files": len(names), "databases_checked": checked}


def restore(layout: Layout, archive: Path, service: Service | None) -> Path:
    """Put a backup back. The current config + data are backed up first; the service is stopped during the swap."""
    verify_backup(archive)
    safety = backup(layout, "before-restore") if layout.data.exists() or layout.config.exists() else None
    if service is not None:
        service.stop()
    staging = layout.root / f".restore-{secrets.token_hex(3)}"
    try:
        with tarfile.open(archive, "r:gz") as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(staging, filter="data")
            else:
                tar.extractall(staging)                               # paths checked by verify_backup
        for name, folder in (("config", layout.config), ("data", layout.data)):
            if (staging / name).exists():
                old = folder.with_name(folder.name + ".replaced")
                if old.exists():
                    shutil.rmtree(old)
                if folder.exists():
                    os.replace(folder, old)
                os.replace(staging / name, folder)
                shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if service is not None:
        service.start()
    return safety or archive


# ------------------------------------------------------------------------------------------------ orchestration

@dataclass
class Options:
    root: Path
    source: Path = SOURCE_ROOT
    port: int | None = None
    access: str = "local"                # local | lan | public | proxy
    service: str = "auto"                # auto | systemd | systemd-user | supervisor | none
    user: str | None = None
    python: str = sys.executable
    wheels: Path | None = None
    reuse_venv: Path | None = None
    public_url: str | None = None
    open_firewall: bool = False
    autostart: bool = True
    reset_admin: bool = False
    health_timeout: float = HEALTH_TIMEOUT
    json_output: bool = False
    log: Callable[[str], None] = field(default=print)


def bind_host_for(access: str) -> str:
    return "0.0.0.0" if access in ("lan", "public") else "127.0.0.1"


def install(opts: Options) -> dict[str, Any]:
    layout = Layout(opts.root)
    log = opts.log
    info = detect_platform()
    checks = preflight(layout, opts.python, info, opts.source)
    failed = [c for c in checks if not c.ok and c.level == "error"]
    for c in checks:
        log(f"  [{'ok' if c.ok else ('!!' if c.level == 'error' else '注意')}] {c.message}")
    if failed:
        raise InstallError("系统检查没有通过：" + "；".join(c.message for c in failed))
    state = load_state(layout)
    fresh = not state
    for folder in (layout.root, layout.config, layout.data, layout.logs, layout.backups):
        folder.mkdir(parents=True, exist_ok=True)
    if not fresh and layout.data.exists() and any(layout.data.iterdir()):
        log("  已有安装：先备份配置和数据…")
        state["last_backup"] = str(backup(layout, "before-install"))
    service_mode = Service.pick(info, opts.service if opts.service != "auto" else state.get("service", "auto"))
    service = Service(layout, service_mode, opts.user or state.get("user"))
    if not fresh and service_mode != "none":
        service.stop()
    log("  复制程序文件…")
    install_program(layout, opts.source)
    ensure_venv(layout, opts.python, offline_wheels=opts.wheels, reuse_venv=opts.reuse_venv, log=log)
    access = opts.access or state.get("access", "local")
    bind_host = bind_host_for(access)
    port, port_reason = choose_port(opts.port, state.get("port"), "0.0.0.0" if bind_host == "0.0.0.0" else "127.0.0.1")
    log(f"  端口 {port}（{port_reason}）")
    tls = None
    if access in ("lan", "public"):
        tls = ensure_self_signed(layout, lan_addresses())
        if tls is None:
            raise InstallError("局域网 / 公网访问必须用 HTTPS，但这台机器没有 openssl，无法生成证书。"
                               "安装 openssl 后重试，或用 --access local（SSH 隧道）/ --access proxy（自己的 HTTPS 反向代理）")
    existing = layout.env_file.read_text(encoding="utf-8") if layout.env_file.exists() else ""
    wanted = wanted_env(layout, port, bind_host, access, tls)
    have = parse_env(existing)
    if have.get("VIOLET_DASHBOARD_PORT") and have["VIOLET_DASHBOARD_PORT"] != str(port):
        # the port is the one thing an install decides again (it was free-checked just now)
        existing = "\n".join(f"VIOLET_DASHBOARD_PORT={port}" if line.strip().startswith("VIOLET_DASHBOARD_PORT=") else line
                             for line in existing.splitlines()) + "\n"
    text, added = merge_env(existing, wanted, header="# Violet -- written by the installer. Model keys, channels "
                                                     "and everything else are set in the Control Center.")
    layout.env_file.write_text(text, encoding="utf-8")
    _restrict(layout.env_file)
    if added:
        log("  配置新增：" + "、".join(added))
    log("  初始化数据库和管理员账号…")
    done = app_command(layout, "-m", "tools.violet_setup", "init", "--json", *(["--reset-admin"] if opts.reset_admin else []))
    if done.returncode != 0:
        raise InstallError("初始化失败：\n" + (done.stderr or done.stdout or "")[-1500:])
    init = json.loads(done.stdout.strip().splitlines()[-1])
    service_note = "未注册服务（--service none）"
    if service_mode == "systemd":
        service.user = service.user or SERVICE_USER
        ensure_service_user(layout, service.user)
    if service_mode != "none":
        service_note = service.register(autostart=opts.autostart)
        log(f"  {service_note}")
        service.start()
    scheme = "https" if tls else "http"
    healthy = service_mode == "none" or wait_healthy(port, "127.0.0.1", opts.health_timeout, scheme)
    if not healthy:
        raise InstallError("服务启动了，但 " + f"{opts.health_timeout:.0f} 秒内健康检查没有通过。查看日志："
                           + str(layout.logs / "main.log"))
    firewall = firewall_status()
    firewall_note = None
    if access in ("lan", "public") and firewall.get("active"):
        cmd = firewall_open_command(firewall.get("tool"), port)
        if opts.open_firewall and cmd:
            result = subprocess.run(cmd, capture_output=True, text=True)
            firewall_note = ("已在本机防火墙放行端口 " if result.returncode == 0 else "放行防火墙失败，请手动执行：") + \
                            (str(port) if result.returncode == 0 else " ".join(cmd))
            if result.returncode == 0 and firewall.get("tool") == "firewalld":
                subprocess.run(["firewall-cmd", "--reload"], capture_output=True)
        elif cmd:
            firewall_note = "本机防火墙已开启，没有自动修改。需要时执行：" + " ".join(cmd) + "（或安装时加 --open-firewall）"
    network = network_report(port, bind_host, public_url=opts.public_url,
                             fetch=(lambda url: _fetch_ok(url)) if opts.public_url else None) \
        if service_mode != "none" else []
    state.update(version=source_version(opts.source), port=port, access=access, service=service_mode,
                 user=service.user or opts.user or state.get("user"), installed_at=state.get("installed_at") or int(time.time()),
                 updated_at=int(time.time()), python=str(venv_python(layout)))
    save_state(layout, state)
    write_cli(layout)
    urls = [f"{scheme}://127.0.0.1:{port}/control"]
    if bind_host == "0.0.0.0":
        urls += [f"{scheme}://{a}:{port}/control" for a in lan_addresses()]
    return {"ok": True, "fresh": fresh, "root": str(layout.root), "version": state["version"], "port": port,
            "port_reason": port_reason, "urls": urls, "access": access, "service": service_note,
            "service_state": service.state() if service_mode != "none" else "not_registered",
            "admin": init.get("admin"), "tls_fingerprint": (tls or {}).get("fingerprint"),
            "autostart_hint": supervisor_autostart_hint(layout) if service_mode == "supervisor" else None,
            "network": network, "firewall": firewall_note,
            "core_ok": healthy, "public_ready": any(n["layer"] == "public" and n["state"] == "ok" for n in network)}


def _fetch_ok(url: str) -> bool:
    import ssl
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/healthz", timeout=8, context=context) as response:
            return json.loads(response.read(4096).decode("utf-8")).get("service") == "violet"
    except Exception:
        return False


def upgrade(opts: Options) -> dict[str, Any]:
    layout = Layout(opts.root)
    state = load_state(layout)
    if not state:
        raise InstallError(f"{layout.root} 没有安装 Violet；请用 install")
    service = Service(layout, state.get("service", "supervisor"), state.get("user"))
    opts.log("  备份配置和数据…")
    archive = backup(layout, "before-upgrade")
    old_version = state.get("version")
    if state.get("service") != "none":
        service.stop()
    try:
        install_program(layout, opts.source)
        if opts.reuse_venv is None:
            ensure_venv(layout, opts.python, offline_wheels=opts.wheels, log=opts.log)
        done = app_command(layout, "-m", "tools.violet_setup", "init", "--json")
        if done.returncode != 0:
            raise InstallError("升级后初始化失败：\n" + (done.stderr or done.stdout or "")[-1500:])
        if state.get("service") == "systemd" and state.get("user"):
            ensure_service_user(layout, state["user"])         # files the initialiser created as root
            Service(layout, "systemd", state["user"]).register()  # the unit of the new version
        if state.get("service") != "none":
            service.start()
            if not wait_healthy(state["port"], "127.0.0.1", opts.health_timeout, _scheme(layout)):
                raise InstallError("新版本启动后健康检查没有通过")
    except Exception as exc:
        opts.log(f"  升级失败（{exc}），恢复旧版本…")
        if state.get("service") != "none":
            service.stop()
        _rollback_program(layout)
        running = ""
        if state.get("service") != "none":
            service.start()
            # say whether the old version is really answering again, not only that its files are back
            running = ("，旧版本已重新运行" if wait_healthy(state["port"], "127.0.0.1", opts.health_timeout, _scheme(layout))
                       else "，但旧版本启动后健康检查没有通过，请运行 status 查看")
        raise InstallError(f"升级失败，已恢复到 {old_version}{running}。数据备份：{archive}。原因：{exc}") from None
    state.update(version=source_version(opts.source), previous_version=old_version, updated_at=int(time.time()),
                 last_backup=str(archive))
    save_state(layout, state)
    write_cli(layout)
    return {"ok": True, "from": old_version, "to": state["version"], "backup": str(archive)}


def _scheme(layout: Layout) -> str:
    """https when the installed configuration serves the dashboard with its own certificate."""
    try:
        env = parse_env(layout.env_file.read_text(encoding="utf-8"))
    except OSError:
        return "http"
    return "https" if env.get("VIOLET_DASHBOARD_TLS_CERT") else "http"


def _rollback_program(layout: Layout) -> None:
    if not layout.app_previous.exists():
        raise InstallError("没有可以回滚的旧版本")
    broken = layout.root / "app.failed"
    if broken.exists():
        shutil.rmtree(broken)
    if layout.app.exists():
        _replace(layout.app, broken)
    _replace(layout.app_previous, layout.app)


def _replace(source: Path, target: Path, attempts: int = 20) -> None:
    """os.replace for folders, retried briefly: on Windows a process that just exited can hold a handle for a moment."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)


def rollback(opts: Options) -> dict[str, Any]:
    """The program of the previous version back. Data is NOT rolled back automatically (a newer schema is readable by
    design); to also go back in data, restore the "before-upgrade" backup."""
    layout = Layout(opts.root)
    state = load_state(layout)
    service = Service(layout, state.get("service", "supervisor"), state.get("user"))
    if state.get("service") != "none":
        service.stop()
    _rollback_program(layout)
    healthy = None
    if state.get("service") != "none":
        service.start()
        healthy = wait_healthy(state["port"], "127.0.0.1", opts.health_timeout, _scheme(layout)) if state.get("port") \
            else None
    state.update(version=source_version(layout.app), previous_version=None, updated_at=int(time.time()))
    save_state(layout, state)
    return {"ok": healthy is not False, "version": state["version"], "healthy": healthy,
            "note": "数据没有回滚；需要时用 restore 恢复升级前的备份", "backup": state.get("last_backup")}


def _download(url: str, target: Path, limit: int = 1_000_000) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "violet-updater"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(limit + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise InstallError(f"下载 {url} 失败：{exc.__class__.__name__}") from None
    if len(body) > limit or not body.startswith(b"#!"):
        raise InstallError(f"{url} 不是安装脚本")
    target.write_bytes(body)


def update(opts: Options, check_only: bool = False, release: dict | None = None,
           download: Callable[[str, Path], None] = _download, run: Callable[..., Any] = subprocess.run,
           windows: bool | None = None) -> dict[str, Any]:
    """`violet update`: the newest GitHub release over this installation. The release's own install.sh downloads its
    archive, checks the SHA-256 and runs `upgrade` (backup first; a failed upgrade puts the old version back)."""
    layout = Layout(opts.root)
    state = load_state(layout)
    if not state:
        raise InstallError(f"{layout.root} 没有安装 Violet")
    current = state.get("version") or source_version(layout.app)
    rel = release or latest_release()
    newer = is_newer(rel["version"], current)
    result = {"ok": True, "current": current, "latest": rel["version"], "newer": newer, "release_url": rel["url"],
              "published_at": rel.get("published_at")}
    if check_only or not newer:
        return {**result, "updated": False}
    if (os.name == "nt") if windows is None else windows:
        raise InstallError(f"Windows 上请下载 {rel['tag']} 的程序，在其目录里运行 python tools/violet_installer.py upgrade")
    if state.get("service") == "systemd" and not _is_root():
        raise InstallError(f"需要 root 权限：sudo {layout.root / 'violet'} update")
    work = Path(tempfile.mkdtemp(prefix="violet-update-"))
    try:
        script = work / "install.sh"
        download(rel["install_url"], script)
        opts.log(f"  升级 {current} → {rel['version']}…")
        done = run(["bash", str(script), "upgrade", "--root", str(layout.root), "--json"], capture_output=True,
                   text=True, encoding="utf-8")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    lines = (done.stdout or "").strip().splitlines()
    try:
        outcome = json.loads(lines[-1]) if lines else {}
    except ValueError:
        outcome = {}
    if done.returncode != 0 or not outcome.get("ok"):
        raise InstallError(outcome.get("error") or ("升级失败：" + (done.stderr or done.stdout or "")[-1500:]))
    return {**result, "updated": True, "to": outcome.get("to"), "backup": outcome.get("backup")}


def status(opts: Options) -> dict[str, Any]:
    layout = Layout(opts.root)
    state = load_state(layout)
    if not state:
        return {"installed": False, "root": str(layout.root)}
    service = Service(layout, state.get("service", "supervisor"), state.get("user"))
    env = parse_env(layout.env_file.read_text(encoding="utf-8")) if layout.env_file.exists() else {}
    bind = env.get("VIOLET_DASHBOARD_HOST", "127.0.0.1")
    return {"installed": True, "root": str(layout.root), "version": state.get("version"), "port": state.get("port"),
            "access": state.get("access"), "service": state.get("service"),
            "service_state": service.state() if state.get("service") != "none" else "not_registered",
            "network": network_report(int(state.get("port") or 0), bind, public_url=opts.public_url,
                                      fetch=_fetch_ok if opts.public_url else None),
            "backups": sorted(p.name for p in layout.backups.glob("violet-backup-*.tar.gz"))[-5:]}


def uninstall(opts: Options, purge: bool = False) -> dict[str, Any]:
    layout = Layout(opts.root)
    state = load_state(layout)
    if not state:
        raise InstallError(f"{layout.root} 没有安装 Violet")
    service = Service(layout, state.get("service", "supervisor"), state.get("user"))
    if state.get("service") != "none":
        service.unregister()
    removed = []
    for path in (layout.app, layout.app_previous, layout.root / "app.failed", layout.root / "app.new"):
        if path.exists():
            shutil.rmtree(path)
            removed.append(path.name)
    if layout.venv.exists() and not (layout.root / "venv.path").exists():
        shutil.rmtree(layout.venv)
        removed.append("venv")
    (layout.root / "venv.path").unlink(missing_ok=True)
    (layout.root / "violet").unlink(missing_ok=True)
    _remove_cmd_later(layout.root / "violet.cmd")
    kept = [p.name for p in (layout.config, layout.data, layout.backups) if p.exists()]
    if purge:
        for p in (layout.config, layout.data, layout.backups, layout.logs):
            if p.exists():
                shutil.rmtree(p)
        layout.state_file.unlink(missing_ok=True)
        kept = []
    else:
        state.update(service="none", uninstalled_at=int(time.time()))
        save_state(layout, state)
    return {"ok": True, "removed": removed, "kept": kept,
            "note": "配置、数据和备份都保留了；彻底删除用 uninstall --purge --yes" if kept else "已全部删除"}


def _remove_cmd_later(path: Path) -> None:
    """violet.cmd may be the batch file running this uninstall; cmd reads it again after we return, so it is deleted a
    moment later by a detached process (deleting it now prints "The batch file cannot be found")."""
    if not path.exists():
        return
    if os.name != "nt":
        path.unlink(missing_ok=True)
        return
    # a raw command line: list-to-string quoting would turn the inner quotes into \" which cmd does not understand
    subprocess.Popen(f'cmd /c "ping -n 3 127.0.0.1 >nul & del /f /q "{path}""', stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008 | 0x00000200)        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def write_cli(layout: Layout) -> None:
    """<root>/violet (and violet.cmd on Windows): start / stop / status / backup / upgrade ... without remembering paths."""
    py, installer = venv_python(layout), layout.app / "tools" / "violet_installer.py"
    if os.name == "nt":
        # one line ending in "exit /b": cmd has parsed it before running it, so "uninstall" may delete this file
        (layout.root / "violet.cmd").write_text(f'@"{py}" "{installer}" %* --root "{layout.root}" & exit /b\r\n',
                                             encoding="utf-8")
    else:
        path = layout.root / "violet"
        path.write_text(f'#!/bin/sh\nexec "{py}" "{installer}" "$@" --root "{layout.root}"\n', encoding="utf-8")
        os.chmod(path, 0o755)


# ------------------------------------------------------------------------------------------------ output

def render_result(result: dict[str, Any]) -> str:
    lines = ["", "=" * 60, "Violet 安装完成" if result.get("fresh") else "Violet 已更新 / 修复", "=" * 60,
             f"版本：{result.get('version')}    安装目录：{result.get('root')}",
             f"服务：{result.get('service')}（当前状态：{result.get('service_state')}）"]
    if result.get("autostart_hint"):
        lines.append(f"  开机自启没有自动设置；需要时执行：{result['autostart_hint']}")
    lines.append("")
    lines.append("管理后台地址：")
    lines += [f"  {u}" for u in result.get("urls") or []]
    if result.get("tls_fingerprint"):
        lines.append(f"  （自签名证书，浏览器会提示不安全；核对证书指纹 SHA-256 {result['tls_fingerprint'][:47]}…）")
    if result.get("access") == "local":
        lines.append("  后台只在本机监听。远程管理：ssh -L "
                     f"{result['port']}:127.0.0.1:{result['port']} 用户@服务器，然后在自己电脑打开上面的地址")
    admin = result.get("admin") or {}
    lines.append("")
    if admin.get("password"):
        lines += ["管理员账号：admin（单管理员，登录页只需输入密码）", f"初始密码：{admin['password']}",
                  "  这个密码只显示这一次；第一次登录后必须修改。"]
    else:
        lines.append("管理员密码：沿用已有的密码（忘记了可以重新运行安装并加 --reset-admin）")
    lines += ["", "网络检查："]
    marks = {"ok": "通过", "fail": "失败", "not_checked": "未验证", "not_applicable": "不适用"}
    for layer in result.get("network") or []:
        lines.append(f"  [{marks.get(layer['state'], layer['state'])}] {layer['title']}：{layer['detail']}")
    if result.get("firewall"):
        lines.append("  " + result["firewall"])
    lines += ["", "核心服务：" + ("运行正常" if result.get("core_ok") else "未通过健康检查"),
              "公网访问：" + ("已验证可以访问" if result.get("public_ready") else "未配置或未验证（见上面的网络检查）"),
              "", "下一步：登录后台，按“开始”页填写模型服务（API 地址和 Key）、扫码登录微信或添加其他聊天渠道。",
              f"日常管理：{Path(result.get('root', '.')) / ('violet.cmd' if os.name == 'nt' else 'violet')} status | stop | "
              "start | backup | update（升级到 GitHub 上的最新版本）| uninstall", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="violet-installer", description="Violet installer")
    parser.add_argument("command", choices=["install", "upgrade", "update", "rollback", "status", "backup", "restore",
                                            "uninstall", "doctor", "start", "stop", "restart"])
    parser.add_argument("--check", action="store_true", help="update: only report whether a newer release exists")
    parser.add_argument("archive", nargs="?", help="restore: the backup file")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--port", type=int)
    parser.add_argument("--access", choices=["local", "lan", "public", "proxy"])
    parser.add_argument("--service", choices=["auto", "systemd", "systemd-user", "supervisor", "none"], default="auto")
    parser.add_argument("--user", help="systemd: the account Violet runs as")
    parser.add_argument("--wheels", type=Path, help="install requirements offline from this folder")
    parser.add_argument("--reuse-venv", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--public-url", help="the address to try for the public-access check")
    parser.add_argument("--open-firewall", action="store_true")
    parser.add_argument("--no-autostart", action="store_true")
    parser.add_argument("--reset-admin", action="store_true", help="make a new initial admin password")
    parser.add_argument("--health-timeout", type=float, default=HEALTH_TIMEOUT)
    parser.add_argument("--purge", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = (args.root or default_root()).expanduser().resolve()
    out = (lambda s: None) if args.json else print
    opts = Options(root=root, port=args.port, access=args.access or "", service=args.service, user=args.user,
                   wheels=args.wheels, reuse_venv=args.reuse_venv, public_url=args.public_url,
                   open_firewall=args.open_firewall, autostart=not args.no_autostart, reset_admin=args.reset_admin,
                   health_timeout=args.health_timeout, json_output=args.json, log=out)
    try:
        if args.command == "install":
            if not opts.access:
                opts.access = load_state(Layout(root)).get("access") or "local"
            result = install(opts)
            print(json.dumps(result, ensure_ascii=False) if args.json else render_result(result))
            return 0
        if args.command == "doctor":
            layout = Layout(root)
            checks = [c.to_dict() for c in preflight(layout)]
            result = {"checks": checks, "status": status(opts)}
        elif args.command == "upgrade":
            result = upgrade(opts)
        elif args.command == "update":
            result = update(opts, check_only=args.check)
            if not args.json:
                if not result["newer"]:
                    out(f"已经是最新版本：{result['current']}（最新发布 {result['latest']}）")
                elif not result["updated"]:
                    out(f"有新版本：{result['latest']}（当前 {result['current']}）。升级：{root / 'violet'} update")
                else:
                    out(f"已升级到 {result['to']}；升级前的备份：{result['backup']}")
        elif args.command == "rollback":
            result = rollback(opts)
        elif args.command == "status":
            result = status(opts)
        elif args.command == "backup":
            path = backup(Layout(root), "manual")
            result = {"ok": True, "backup": str(path), **verify_backup(path)}
        elif args.command == "restore":
            if not args.archive:
                raise InstallError("restore 需要备份文件路径")
            layout = Layout(root)
            state = load_state(layout)
            service = Service(layout, state.get("service", "supervisor"), state.get("user")) \
                if state.get("service") not in (None, "none") else None
            result = {"ok": True, "safety_backup": str(restore(layout, Path(args.archive), service))}
        elif args.command == "uninstall":
            if args.purge and not args.yes:
                raise InstallError("--purge 会删除全部数据（聊天记录、记忆、配置、备份），确认请再加 --yes")
            result = uninstall(opts, purge=args.purge)
        else:                                                   # start / stop / restart
            layout = Layout(root)
            state = load_state(layout)
            service = Service(layout, state.get("service", "supervisor"), state.get("user"))
            if args.command in ("stop", "restart"):
                service.stop()
            if args.command in ("start", "restart"):
                service.start()
            result = {"ok": True, "service_state": service.state()}
        print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 1))
        return 0
    except InstallError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False) if args.json else f"\n失败：{exc}",
              file=sys.stdout if args.json else sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
