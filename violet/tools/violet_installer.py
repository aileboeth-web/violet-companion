"""Violet Installer (phase 10.4). Standard library only: it runs before Violet's virtual environment exists.

    bash install.sh                           (Linux; as root, or a sudo user -- it elevates itself; finds Python 3.11+)
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
import re
import secrets
import shlex
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
PORT_RANGE = range(10000, 30000)     # 0.6.1: a new installation takes a random free port here (1Panel style)
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


def feed_url_checked(url: str) -> str:
    """A release.json address (tools/make_release.py writes one next to install.sh): https, or http on this machine
    for tests -- the same rule make_release applies to --base."""
    url = (url or "").strip()
    if not re.match(r"^(https://[^\s/?#@]+|http://(127\.0\.0\.1|localhost)(:\d+)?)(/[^\s?#@]*)?$", url) \
            or not url.endswith("release.json"):
        raise InstallError("--update-feed 要是 https:// 开头、以 release.json 结尾的地址（http 只允许 127.0.0.1）")
    return url


def feed_release(url: str, opener: Callable[..., Any] | None = None, timeout: float = 10.0) -> dict:
    """The release described by a release.json (instead of GitHub Releases)."""
    request = urllib.request.Request(feed_url_checked(url), headers={"User-Agent": "violet-updater"})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            data = json.loads(response.read(100_000).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise InstallError(f"读取 {url} 失败：{exc.__class__.__name__}") from None
    version, base = str(data.get("version") or ""), str(data.get("base") or "").rstrip("/")
    if not version_key(version) or not base:
        raise InstallError(f"{url} 不是 Violet 的 release.json")
    return {"tag": "v" + version, "version": version, "url": "", "published_at": data.get("built_at"), "notes": "",
            "install_url": base + "/install.sh"}


def release_for(state: dict) -> dict:
    """The newest release for this installation: its own feed if the installer was given one, else GitHub (the
    repository given at install time, else the official one). Only root-owned install.json decides this."""
    if state.get("update_feed"):
        return feed_release(state["update_feed"])
    return latest_release(state.get("update_repo") or update_repo())


def repo_checked(repo: str) -> str:
    repo = (repo or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}", repo):
        raise InstallError("--update-repo 的格式是 GitHub 的 owner/repo")
    return repo


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
                        f"没有权限写入 {parent}（以 root 运行，或用 --root 指定自己的目录）"))
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
    """True when nothing listens on host:port (a bind succeeds). On Linux with SO_REUSEADDR, as Violet's own server
    binds: connections of a just-stopped Violet left in TIME_WAIT do not count as "in use" (only a listener does);
    without it a reinstall right after stopping the service moved to another port."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def probe_eve(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> dict[str, Any] | None:
    """The /healthz answer when a Violet listens there (plain HTTP, or HTTPS with its own self-signed certificate),
    else None."""
    import ssl
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE                     # only asks "is this Violet?" on this machine
    for scheme in ("http", "https"):
        handlers: list[Any] = [urllib.request.ProxyHandler({})]
        if scheme == "https":
            handlers.append(urllib.request.HTTPSHandler(context=context))
        try:
            with urllib.request.build_opener(*handlers).open(
                    f"{scheme}://{_host_for_url(host)}:{port}/healthz", timeout=timeout) as response:
                data = json.loads(response.read(4096).decode("utf-8"))
                if isinstance(data, dict) and data.get("ok") and data.get("service") == "violet":
                    return data
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return None


def _host_for_url(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def choose_port(requested: int | None, previous: int | None, host: str = "127.0.0.1",
                is_free: Callable[[int, str], bool] = port_free,
                is_eve: Callable[[int, str], bool] = lambda p, h: probe_eve(p, h) is not None,
                rng: Any = None) -> tuple[int, str]:
    """(port, reason). A port given with --port must be usable (free, or this Violet already on it) -- never silently
    replaced. Otherwise the port of the previous install is kept, else a random free one in 10000-29999 (well-known
    ports like 8443 are blocked by many providers and scanned more; the installer prints the address anyway)."""
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
    rng = rng or secrets.SystemRandom()
    for _ in range(100):
        port = rng.randrange(PORT_RANGE.start, PORT_RANGE.stop)
        if is_free(port, host):
            return port, "随机选的空闲端口"
    for port in PORT_RANGE:
        if is_free(port, host):
            return port, "随机选的空闲端口"
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


PUBLIC_IP_SERVICES = ("https://api.ipify.org", "https://ifconfig.me/ip", "https://ipv4.icanhazip.com")


def public_ip(opener: Callable[..., Any] | None = None, timeout: float = 4.0) -> str | None:
    """This server's public IPv4 address, asked from a "what is my IP" service (as 1Panel does) -- only for the address
    shown after a public install. None when offline or every answer is not a public IPv4 address."""
    for url in PUBLIC_IP_SERVICES:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "violet-installer"})
            with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
                text = response.read(64).decode("ascii", "replace").strip()
            ip = ipaddress.ip_address(text)
            if ip.version == 4 and ip.is_global:
                return text
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return None


def default_access() -> str:
    """public for a server install as root (1Panel style: https://IP:port/entrance), local otherwise."""
    return "public" if os.name != "nt" and _is_root() else "local"


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


# ------------------------------------------------------------------------------------------------ host firewalls
# Violet stage 3 (user, 2026-10-27, after a 1Panel server kept the port closed): not only ufw / firewalld -- every
# common way a Linux server drops new ports is found and opened: ufw and firewalld through their own tools
# (persistent), and direct iptables / native nftables rules (1Panel's 1PANEL_* chains, CSF, hand-written rules, a DROP
# policy) with one ACCEPT rule marked "violet" at the top of the input chain. Those two are runtime rules: violet.service
# puts them back before every start (ExecStartPre, as root). Only rules marked "violet" are ever removed.

RULE_MARK = "violet"
_IPT_TABLES = {("ip", "filter"), ("ip6", "filter")}          # iptables-nft's own tables: handled with iptables


def _out(run: Callable[..., Any], cmd: list[str], timeout: float = 10) -> str | None:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        done = run(cmd, capture_output=True, text=True, timeout=timeout)
        return (done.stdout or "") + (done.stderr or "")
    except Exception:
        return None


def firewall_layers(run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """{"layers": [...], "panels": [...]}. A layer is a place where packets to a new port can be dropped:
    ufw | firewalld | iptables (filtering rules exist) | ip6tables | nftables (a native input chain that drops)."""
    layers: list[dict[str, Any]] = []
    panels = []
    if shutil.which("1pctl") or os.path.isdir("/opt/1panel"):
        panels.append("1Panel")
    if os.path.isdir("/www/server/panel"):
        panels.append("宝塔 / aaPanel")
    if shutil.which("csf"):
        panels.append("CSF")
    text = _out(run, ["ufw", "status"])
    if text is not None and "Status: active" in text:
        layers.append({"tool": "ufw"})
    text = _out(run, ["firewall-cmd", "--state"])
    if text is not None and text.strip().startswith("running"):
        layers.append({"tool": "firewalld"})
    for tool in ("iptables", "ip6tables"):
        rules = _out(run, [tool, "-S"])
        if rules and (re.search(r"^-P INPUT (DROP|REJECT)", rules, re.M) or re.search(r"-j (DROP|REJECT)\b", rules)
                      or re.search(r"^-N (1PANEL|BT_|ufw-|f2b-|LOCALINPUT|ALLOWIN)", rules, re.M)):
            layers.append({"tool": tool})
    layers += nft_drop_chains(run)
    if any(l["tool"] == "iptables" for l in layers) and "1Panel" not in panels and \
            "1PANEL" in (_out(run, ["iptables", "-S"]) or ""):
        panels.append("1Panel")
    return {"layers": layers, "panels": panels}


def nft_drop_chains(run: Callable[..., Any] = subprocess.run) -> list[dict[str, Any]]:
    """Native nftables input chains that drop (policy drop, or a drop / reject rule) -- not iptables-nft's tables and
    not firewalld's (those are opened with their own tools)."""
    text = _out(run, ["nft", "-j", "list", "ruleset"])
    if not text:
        return []
    try:
        items = json.loads(text[text.index("{"):]).get("nftables") or []
    except ValueError:
        return []
    chains, dropping = {}, set()
    for item in items:
        chain = item.get("chain")
        if chain and chain.get("hook") == "input":
            key = (chain.get("family"), chain.get("table"), chain.get("name"))
            if (key[0], key[1]) in _IPT_TABLES or key[1] == "firewalld":
                continue
            chains[key] = chain
            if chain.get("policy") == "drop":
                dropping.add(key)
        rule = item.get("rule")
        if rule:
            key = (rule.get("family"), rule.get("table"), rule.get("chain"))
            if any(("drop" in e or "reject" in e) for e in rule.get("expr") or [] if isinstance(e, dict)):
                dropping.add(key)
    return [{"tool": "nftables", "family": k[0], "table": k[1], "chain": k[2]} for k in chains if k in dropping]


def _ipt_rule(port: int) -> list[str]:
    return ["INPUT", "-p", "tcp", "--dport", str(port), "-m", "comment", "--comment", RULE_MARK, "-j", "ACCEPT"]


def _nft_handles(run: Callable[..., Any], layer: dict[str, Any], port: int) -> list[str]:
    text = _out(run, ["nft", "-a", "list", "chain", layer["family"], layer["table"], layer["chain"]]) or ""
    return re.findall(rf"tcp dport {port} accept comment \"{RULE_MARK}\" # handle (\d+)", text)


def ensure_rule(layer: dict[str, Any], port: int, run: Callable[..., Any] = subprocess.run) -> bool:
    """Open port in one layer (idempotent). True when the port is open there afterwards."""
    tool = layer["tool"]
    if tool == "ufw":
        return run(["ufw", "allow", f"{port}/tcp"], capture_output=True, text=True).returncode == 0
    if tool == "firewalld":
        ok = run(["firewall-cmd", "--permanent", f"--add-port={port}/tcp"], capture_output=True, text=True).returncode == 0
        run(["firewall-cmd", "--reload"], capture_output=True)
        return ok
    if tool in ("iptables", "ip6tables"):
        if run([tool, "-C", *_ipt_rule(port)], capture_output=True).returncode == 0:
            return True
        return run([tool, "-I", *_ipt_rule(port)], capture_output=True).returncode == 0
    if tool == "nftables":
        if _nft_handles(run, layer, port):
            return True
        return run(["nft", "insert", "rule", layer["family"], layer["table"], layer["chain"], "tcp", "dport", str(port),
                    "accept", "comment", f'"{RULE_MARK}"'], capture_output=True).returncode == 0
    return False


def remove_rule(record: dict[str, Any], run: Callable[..., Any] = subprocess.run) -> None:
    """Remove a rule this installer added (only ours: ufw / firewalld entries we recorded, iptables / nft rules marked
    "violet")."""
    tool, port = record.get("tool"), int(record.get("port") or 0)
    if tool == "ufw" and shutil.which("ufw"):
        run(["ufw", "delete", "allow", f"{port}/tcp"], capture_output=True)
    elif tool == "firewalld" and shutil.which("firewall-cmd"):
        run(["firewall-cmd", "--permanent", f"--remove-port={port}/tcp"], capture_output=True)
        run(["firewall-cmd", "--reload"], capture_output=True)
    elif tool in ("iptables", "ip6tables") and shutil.which(tool):
        for _ in range(10):                                       # every copy of our rule
            if run([tool, "-D", *_ipt_rule(port)], capture_output=True).returncode != 0:
                break
    elif tool == "nftables" and shutil.which("nft"):
        for handle in _nft_handles(run, record, port):
            run(["nft", "delete", "rule", record["family"], record["table"], record["chain"], "handle", handle],
                capture_output=True)


def sweep_marked_rules(run: Callable[..., Any] = subprocess.run) -> list[str]:
    """`uninstall --purge` (0.7.0): every iptables / ip6tables / nftables rule marked "violet", recorded or not (a
    lost install.json, a port changed by hand). Only our mark is matched -- nothing else is touched."""
    removed: list[str] = []
    mark = re.compile(rf'--comment "?{RULE_MARK}"?(\s|$)')
    for tool in ("iptables", "ip6tables"):
        for line in (_out(run, [tool, "-S"]) or "").splitlines():
            if not line.startswith("-A ") or not mark.search(line):
                continue
            try:
                args = shlex.split(line)[1:]
            except ValueError:
                continue
            if run([tool, "-D", *args], capture_output=True).returncode == 0:
                port = re.search(r"--dport (\d+)", line)
                removed.append(f"{tool} {port.group(1) if port else ''}".strip())
    family = table = chain = None
    for line in (_out(run, ["nft", "-a", "list", "ruleset"]) or "").splitlines():
        if m := re.match(r"\s*table (\S+) (\S+) \{", line):
            family, table, chain = m.group(1), m.group(2), None
        elif m := re.match(r"\s*chain (\S+) \{", line):
            chain = m.group(1)
        elif f'comment "{RULE_MARK}"' in line and family and table and chain:
            handle = re.search(r"# handle (\d+)", line)
            if handle and run(["nft", "delete", "rule", family, table, chain, "handle", handle.group(1)],
                              capture_output=True).returncode == 0:
                port = re.search(r"dport (\d+)", line)
                removed.append(f"nftables {port.group(1) if port else ''}".strip())
    return removed


def open_port_everywhere(port: int, run: Callable[..., Any] = subprocess.run,
                         detected: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], str | None]:
    """Open port in every host firewall layer found. (records of what was opened, a note for the user)."""
    found = detected or firewall_layers(run)
    records, failed = [], []
    for layer in found["layers"]:
        if ensure_rule(layer, port, run):
            records.append({**layer, "port": port})
        else:
            failed.append(layer["tool"])
    names = {"ufw": "ufw", "firewalld": "firewalld", "iptables": "iptables", "ip6tables": "ip6tables (IPv6)",
             "nftables": "nftables"}
    parts = []
    if records:
        parts.append(f"已在本机防火墙放行端口 {port}：" + "、".join(dict.fromkeys(names[r['tool']] for r in records)))
    if failed:
        parts.append("放行失败：" + "、".join(failed) + f"（请手动放行 TCP {port}）")
    if found["panels"]:
        parts.append("检测到 " + "、".join(found["panels"]) + "：规则加在系统防火墙里，面板的防火墙页面不一定显示；"
                     f"也可以在面板里再添加一条 TCP {port} 放行")
    return records, "；".join(parts) or None


def firewall_apply(layout: "Layout", run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """`violet firewall-apply` (violet.service runs it as root before every start): put back the runtime rules
    (iptables / nftables) recorded at install -- a reboot or a panel reloading its rules removes them."""
    state = load_state(layout)
    done = []
    for record in state.get("firewall_rules") or []:
        if record.get("tool") in ("iptables", "ip6tables", "nftables") and shutil.which(
                "nft" if record["tool"] == "nftables" else record["tool"]):
            if ensure_rule(record, int(record["port"]), run):
                done.append(f"{record['tool']}:{record['port']}")
    return {"ok": True, "applied": done}


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
                   "detail": f"127.0.0.1:{port}/healthz " + ("返回正常" if local else "没有正常返回")})
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


ACCESS_KEYS = ("VIOLET_DASHBOARD_HOST", "VIOLET_DASHBOARD_PORT", "VIOLET_DASHBOARD_TLS_CERT", "VIOLET_DASHBOARD_TLS_KEY",
               "VIOLET_DASHBOARD_COOKIE_SECURE", "VIOLET_DASHBOARD_TRUST_PROXY")


def set_env_keys(existing: str, values: dict[str, str | None]) -> str:
    """Set (or, with None, remove) these keys in place; every other line stays as it is. For the keys the installer
    owns -- how the dashboard is reached -- so that "install --access public" on an existing install takes effect."""
    lines, done = [], set()
    for line in existing.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else None
        if key in values:
            done.add(key)
            if values[key] is not None:
                lines.append(f"{key}={_quote(values[key])}")
            continue
        lines.append(line)
    lines += [f"{k}={_quote(v)}" for k, v in values.items() if k not in done and v is not None]
    return "\n".join(lines) + "\n"


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
        # after the services that load firewall rules, so the port rules put back below are not flushed again
        "After=network-online.target nftables.service firewalld.service ufw.service docker.service 1panel.service "
        "1panel-core.service bt.service",
        "Wants=network-online.target",
        # [Unit] keys since systemd 230 -- in [Service] they were ignored ("Unknown key" in the journal)
        "StartLimitIntervalSec=300",
        "StartLimitBurst=10",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={layout.app}",
        f"Environment=VIOLET_ENV_FILE={layout.env_file}",
        "Environment=PYTHONUNBUFFERED=1",
        f"ExecStart={venv_python(layout)} -u main.py",
        # as root ("+"), failures ignored ("-"): the iptables / nftables port rules a reboot or a panel removed, and
        # the phone relay's pairing values for the Control Center (tools/violet_relay.py; nothing when not installed)
        *([f"ExecStartPre=-+{layout.root / 'violet'} firewall-apply --json",
           f"ExecStartPre=-+{layout.root / 'violet'} mcp-relay export --json"] if user and not user_mode else []),
        "Restart=on-failure",
        "RestartSec=5",
        "TimeoutStopSec=20",
    ]
    if user and not user_mode:
        lines += [f"User={user}", "NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=full",
                  f"ReadWritePaths={layout.root}"]
    lines += ["", "[Install]", "WantedBy=" + ("default.target" if user_mode else "multi-user.target"), ""]
    return "\n".join(lines)


UPDATER_NAME = f"{SERVICE_NAME}-update"
TASK_NAME = f"{SERVICE_NAME}-task"
# 0.7.0: what the Control Center may ask the root task service to do (one word in <data>/control/task-request):
# word -> (which tool, its action). 0.7.1: the private SearXNG for web search.
TASK_ACTIONS = {"relay-install": ("relay", "install"), "relay-set-port": ("relay", "set-port"),
                "searxng-install": ("searxng", "install"), "searxng-uninstall": ("searxng", "uninstall"),
                "searxng-tune": ("searxng", "tune"), "searxng-configure": ("searxng", "configure")}
PROGRESS_STALE_SECONDS = 1800


class Progress:
    """<data>/control/<name>-progress.json: what a root job (an update, a relay install) is doing right now, for the
    Control Center's progress dialog. Written by root into the service account's folder: never through a link the
    service account could have planted (O_EXCL temp file + rename), owned by that account so Violet can read it.
    Never carries secrets. A Progress without a path does nothing."""

    def __init__(self, path: Path | None, user: str | None = None, **base: Any):
        self.path, self.user, self.base = path, user, base
        self.started = int(time.time())

    def write(self, **fields: Any) -> None:
        if self.path is None:
            return
        entry = {**self.base, **fields, "started_at": self.started, "at": int(time.time())}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.unlink(missing_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o640)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                if self.user and hasattr(os, "fchown"):
                    try:
                        import pwd
                        account = pwd.getpwnam(self.user)
                        os.fchown(fh.fileno(), account.pw_uid, account.pw_gid)
                    except (ImportError, KeyError, OSError):
                        pass
                json.dump(entry, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def __call__(self, percent: int, step: str) -> None:
        self.write(state="running", percent=max(0, min(99, int(percent))), step=step)

    def done(self, message: str, **extra: Any) -> None:
        self.write(state="done", percent=100, step=message, message=message, **extra)

    def failed(self, message: str) -> None:
        self.write(state="failed", step="失败", message=message[:1500])


def progress_path(layout: Layout, name: str) -> Path:
    return layout.data / "control" / f"{name}-progress.json"


def env_progress(layout: Layout, state: dict) -> Progress:
    """The update service hands the release's own install.sh (and so `upgrade`) a progress file through the
    environment; without it nothing is written."""
    value = os.environ.get("VIOLET_PROGRESS_FILE") or ""
    path = Path(value) if value else None
    if path is not None and path.resolve().parent != (layout.data / "control").resolve():
        path = None                                         # only Violet's own control folder
    return Progress(path, state.get("user"), task="update")


def updater_units(layout: Layout) -> dict[str, str]:
    """Violet stage 3 -- automatic updates for a root + systemd install. Violet itself runs as an unprivileged user and
    cannot replace its own program, so a root oneshot service does it:

      <UPDATER_NAME>.service  runs `<root>/violet auto-update` (reads the update mode the admin chose; a newer GitHub
                              release is installed with the normal `update`: backup first, rollback on failure)
      <UPDATER_NAME>.timer    every 6 hours (plus 20 minutes after boot), randomised by up to 30 minutes
      <UPDATER_NAME>.path     the Control Center's "update now": Violet may create <data>/control/update-request. That
                              is ALL it can do -- the release source comes only from root-owned install.json / the
                              built-in repository, never from a file the service user can write.
      <TASK_NAME>.path/.service  0.7.0: the Control Center's buttons for root work ("安装中继", "换一个端口"): Violet
                              may create <data>/control/task-request holding one word of TASK_ACTIONS; the service
                              runs `<root>/violet root-task`, which acts only on those words (no paths, no values)."""
    request = layout.data / "control" / "update-request"
    task = layout.data / "control" / "task-request"
    return {
        f"{TASK_NAME}.service": "\n".join([
            "[Unit]", "Description=Violet task requested from the Control Center", "After=network-online.target",
            "Wants=network-online.target", "", "[Service]", "Type=oneshot",
            f"ExecStart={layout.root / 'violet'} root-task --json", "TimeoutStartSec=900", ""]),
        f"{TASK_NAME}.path": "\n".join([
            "[Unit]", "Description=Violet task requested from the Control Center", "", "[Path]",
            f"PathExists={task}", f"Unit={TASK_NAME}.service", "", "[Install]", "WantedBy=paths.target", ""]),
        f"{UPDATER_NAME}.service": "\n".join([
            "[Unit]", "Description=Violet automatic update", "After=network-online.target",
            "Wants=network-online.target", "", "[Service]", "Type=oneshot",
            f"ExecStart={layout.root / 'violet'} auto-update --json", "TimeoutStartSec=1800", ""]),
        f"{UPDATER_NAME}.timer": "\n".join([
            "[Unit]", "Description=Check for Violet updates", "", "[Timer]", "OnBootSec=20min", "OnUnitActiveSec=6h",
            "RandomizedDelaySec=30min", f"Unit={UPDATER_NAME}.service", "", "[Install]", "WantedBy=timers.target", ""]),
        f"{UPDATER_NAME}.path": "\n".join([
            "[Unit]", "Description=Violet update requested from the Control Center", "", "[Path]",
            f"PathExists={request}", f"Unit={UPDATER_NAME}.service", "", "[Install]", "WantedBy=paths.target", ""]),
    }


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
                note += "；用户服务要在退出登录后继续运行，请以 root 执行：loginctl enable-linger " + (self.user or "$USER")
            return note
        return "Violet 自带的守护进程（崩溃后自动重启）"

    def start(self) -> None:
        if self.mode.startswith("systemd"):
            # an explicit start (install, upgrade, the rollback after a failed upgrade) must not be refused because
            # crash restarts used up the unit's start limit (StartLimitBurst in [Unit]; a broken upgrade crash-loops)
            self._ctl("reset-failed", f"{SERVICE_NAME}.service")
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

    def register_updater(self) -> bool:
        """The automatic-update units (system systemd only, see updater_units). Never restarts the update service
        itself: an update running right now re-registers them from inside that service."""
        if self.mode != "systemd":
            return False
        folder = self.unit_path().parent
        for name, text in updater_units(self.layout).items():
            (folder / name).write_text(text, encoding="utf-8")
        self._ctl("daemon-reload")
        self._ctl("enable", "--now", f"{UPDATER_NAME}.timer", f"{UPDATER_NAME}.path", f"{TASK_NAME}.path")
        return True

    def unregister_updater(self) -> None:
        if self.mode != "systemd":
            return
        self._ctl("disable", "--now", f"{UPDATER_NAME}.timer", f"{UPDATER_NAME}.path", f"{TASK_NAME}.path")
        self._ctl("stop", f"{UPDATER_NAME}.service", f"{TASK_NAME}.service")
        for name in updater_units(self.layout):
            (self.unit_path().parent / name).unlink(missing_ok=True)
        self._ctl("daemon-reload")

    def unregister(self) -> None:
        if self.mode.startswith("systemd"):
            self.unregister_updater()
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
    update_feed: str | None = None       # a release.json URL instead of GitHub (own mirror; Linux acceptance test)
    update_repo: str | None = None       # a fork's GitHub owner/repo instead of the official releases
    keep_firewall: bool = False          # public access: do not open the port in ufw / firewalld


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
    repair_backup: Path | None = None
    if not fresh and layout.data.exists() and any(layout.data.iterdir()):
        log("  已有安装：先备份配置和数据…")
        repair_backup = backup(layout, "before-install")
        state["last_backup"] = str(repair_backup)
    service_mode = Service.pick(info, opts.service if opts.service != "auto" else state.get("service", "auto"))
    service = Service(layout, service_mode, opts.user or state.get("user"))
    if not fresh and service_mode != "none":
        service.stop()
    log("  复制程序文件…")
    install_program(layout, opts.source)
    ensure_venv(layout, opts.python, offline_wheels=opts.wheels, reuse_venv=opts.reuse_venv, log=log)
    # a server install as root is reachable at https://IP:port/entrance (1Panel style; user decision 2026-10-27);
    # a repair keeps what was chosen before unless --access says otherwise
    access = opts.access or state.get("access") or default_access()
    bind_host = bind_host_for(access)
    port, port_reason = choose_port(opts.port, state.get("port"), "0.0.0.0" if bind_host == "0.0.0.0" else "127.0.0.1")
    log(f"  端口 {port}（{port_reason}）")
    tls, server_ip = None, None
    if access == "public":
        server_ip = public_ip()
        log(f"  公网 IP：{server_ip}" if server_ip else "  没有查到公网 IP（离线，或服务器在 NAT 后面）")
    if access in ("lan", "public"):
        tls = ensure_self_signed(layout, lan_addresses() + ([server_ip] if server_ip else []))
        if tls is None:
            raise InstallError("局域网 / 公网访问必须用 HTTPS，但这台机器没有 openssl，无法生成证书。"
                               "安装 openssl 后重试，或用 --access local（SSH 隧道）/ --access proxy（自己的 HTTPS 反向代理）")
    existing = layout.env_file.read_text(encoding="utf-8") if layout.env_file.exists() else ""
    wanted = wanted_env(layout, port, bind_host, access, tls)
    if existing.strip():
        # how the dashboard is reached belongs to the installer: decided again on every install (port free-checked
        # just now, access possibly changed with --access); everything else the user may have edited stays
        existing = set_env_keys(existing, {k: wanted.get(k) for k in ACCESS_KEYS})
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
    # before the first start: the unit's ExecStartPre runs <root>/violet (it was "missing" on a fresh install)
    write_cli(layout)
    if service_mode != "none":
        service_note = service.register(autostart=opts.autostart)
        log(f"  {service_note}")
        if service.register_updater():
            state["auto_update_agent"] = "systemd"
            log("  自动更新：已注册（每 6 小时检查一次；可在后台“设置 → 版本更新”改成只提醒或手动）")
        service.start()
    if opts.update_feed:
        state["update_feed"] = feed_url_checked(opts.update_feed)
    if opts.update_repo:
        state["update_repo"] = repo_checked(opts.update_repo)
    scheme = "https" if tls else "http"
    healthy = service_mode == "none" or wait_healthy(port, "127.0.0.1", opts.health_timeout, scheme)
    if not healthy:
        raise InstallError("服务启动了，但 " + f"{opts.health_timeout:.0f} 秒内健康检查没有通过。查看日志："
                           + str(layout.logs / "main.log"))
    if repair_backup is not None and service_mode != "none" and _remove_backup(repair_backup):
        state["last_backup"] = None                 # a repair that came up healthy: its own safety backup is not needed
    # public (or --open-firewall): open the port in every host firewall found -- ufw, firewalld, iptables rules
    # (1Panel, CSF, hand-written), native nftables (--keep-firewall leaves them alone); lan: only suggest
    firewall_note = None
    if access in ("lan", "public") and os.name != "nt" and _is_root() and \
            (opts.open_firewall or (access == "public" and not opts.keep_firewall)):
        records, firewall_note = open_port_everywhere(port)
        old = [r for r in state.get("firewall_rules") or [] if r not in records]
        state["firewall_rules"] = old + records             # removed again by uninstall --purge
    elif access in ("lan", "public"):
        firewall_note = open_firewall_port(port, access, False)
    network = network_report(port, bind_host, public_url=opts.public_url,
                             fetch=(lambda url: _fetch_ok(url)) if opts.public_url else None) \
        if service_mode != "none" else []
    state.update(version=source_version(opts.source), port=port, access=access, service=service_mode,
                 user=service.user or opts.user or state.get("user"), installed_at=state.get("installed_at") or int(time.time()),
                 updated_at=int(time.time()), python=str(venv_python(layout)), public_ip=server_ip)
    save_state(layout, state)
    write_cli(layout)
    urls = panel_urls(scheme, port, bind_host, (init.get("admin") or {}).get("entrance") or "", public=server_ip)
    return {"ok": True, "fresh": fresh, "root": str(layout.root), "version": state["version"], "port": port,
            "port_reason": port_reason, "urls": urls, "access": access, "service": service_note, "public_ip": server_ip,
            "service_state": service.state() if service_mode != "none" else "not_registered",
            "admin": init.get("admin"), "tls_fingerprint": (tls or {}).get("fingerprint"),
            "autostart_hint": supervisor_autostart_hint(layout) if service_mode == "supervisor" else None,
            "network": network, "firewall": firewall_note,
            "core_ok": healthy, "public_ready": any(n["layer"] == "public" and n["state"] == "ok" for n in network)}


def open_firewall_port(port: int, access: str, allowed: bool,
                       firewall: Callable[[], dict] | None = None, run: Callable[..., Any] = subprocess.run) -> str | None:
    """A note about the local firewall for a lan / public install: the port opened (allowed = --open-firewall), or the
    command to do it. None when nothing is in the way."""
    state = (firewall or firewall_status)()
    if access not in ("lan", "public") or not state.get("active"):
        return None
    cmd = firewall_open_command(state.get("tool"), port)
    if not cmd:
        return None
    if not allowed:
        return "本机防火墙已开启，没有自动修改。需要时执行：" + " ".join(cmd) + "（或加 --open-firewall）"
    result = run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return "放行防火墙失败，请手动执行：" + " ".join(cmd)
    if state.get("tool") == "firewalld":
        run(["firewall-cmd", "--reload"], capture_output=True)
    return f"已在本机防火墙放行端口 {port}"


def panel_urls(scheme: str, port: int, bind_host: str, entrance: str,
               lan: Callable[[], list[str]] | None = None, public: str | None = None) -> list[str]:
    """The addresses to open: with a security entrance it is part of the address (1Panel style). The public one
    first when the server listens on every interface."""
    path = f"/{entrance}" if entrance else "/control"
    urls = [f"{scheme}://127.0.0.1:{port}{path}"]
    if bind_host == "0.0.0.0":
        urls += [f"{scheme}://{a}:{port}{path}" for a in (lan or lan_addresses)() if a != public]
        if public:
            urls.insert(0, f"{scheme}://{public}:{port}{path}")
    return urls


def _env(layout: Layout) -> dict[str, str]:
    try:
        return parse_env(layout.env_file.read_text(encoding="utf-8"))
    except OSError:
        return {}


def account(opts: Options, action: str, entrance: str | None = None, run: Callable[..., Any] = subprocess.run
            ) -> dict[str, Any]:
    """`violet info | reset-password | reset-entrance` (Violet stage 2): the admin account, through the installed
    program's own code (tools.violet_setup account). A reset password is shown once; nothing stored is read back."""
    layout = Layout(opts.root)
    state = load_state(layout)
    if not state:
        raise InstallError(f"{layout.root} 没有安装 Violet")
    extra = ["--no-entrance"] if entrance == "" else (["--entrance", entrance] if entrance else [])
    done = app_command(layout, "-m", "tools.violet_setup", "account", action, "--json", *extra, run=run)
    try:
        result = json.loads((done.stdout or "").strip().splitlines()[-1])
    except (IndexError, ValueError):
        result = {"ok": False, "error": (done.stderr or done.stdout or "")[-800:]}
    if done.returncode != 0 or not result.get("ok"):
        raise InstallError(f"{action} 失败：{result.get('error')}")
    if state.get("service") == "systemd" and state.get("user"):
        ensure_service_user(layout, state["user"], run=run)      # admin.json was rewritten as root
    env = _env(layout)
    port = int(env.get("VIOLET_DASHBOARD_PORT") or state.get("port") or 0)
    result.update(urls=panel_urls(_scheme(layout), port, env.get("VIOLET_DASHBOARD_HOST", "127.0.0.1"),
                                  result.get("entrance") or "", public=state.get("public_ip")),
                  port=port, access=state.get("access"), version=state.get("version"))
    return result


def set_port(opts: Options, run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """`violet set-port <port>`: move the Control Center to another port -- free-checked, firewall note (or opened
    with --open-firewall), restart, health check; on a failed check the old port is put back."""
    layout = Layout(opts.root)
    state = load_state(layout)
    if not state:
        raise InstallError(f"{layout.root} 没有安装 Violet")
    if opts.port is None:
        raise InstallError("要指定新端口，例如：violet set-port 8443")
    env = _env(layout)
    old = int(env.get("VIOLET_DASHBOARD_PORT") or state.get("port") or 0)
    if opts.port == old:
        return {"ok": True, "port": old, "changed": False}
    bind_host = env.get("VIOLET_DASHBOARD_HOST", "127.0.0.1")
    port, _ = choose_port(opts.port, old, "0.0.0.0" if bind_host == "0.0.0.0" else "127.0.0.1")

    def write(value: int) -> None:
        text = layout.env_file.read_text(encoding="utf-8")
        lines = [f"VIOLET_DASHBOARD_PORT={value}" if line.strip().startswith("VIOLET_DASHBOARD_PORT=") else line
                 for line in text.splitlines()]
        if not any(line.startswith("VIOLET_DASHBOARD_PORT=") for line in lines):
            lines.append(f"VIOLET_DASHBOARD_PORT={value}")
        layout.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _restrict(layout.env_file)

    access = state.get("access", "local")
    opened: list[dict[str, Any]] = []
    if access == "public" and os.name != "nt" and _is_root() and not opts.keep_firewall:
        opened, firewall_note = open_port_everywhere(port, run)      # the new port, in every firewall layer
    else:
        firewall_note = open_firewall_port(port, access, opts.open_firewall, run=run)
    write(port)
    service = Service(layout, state.get("service", "supervisor"), state.get("user"))
    if state.get("service") != "none":
        service.stop()
        service.start()
        if not wait_healthy(port, "127.0.0.1", opts.health_timeout, _scheme(layout)):
            service.stop()
            write(old)
            service.start()
            raise InstallError(f"换到端口 {port} 后健康检查没有通过，已改回 {old}。查看日志：{layout.logs / 'main.log'}")
    rules = state.get("firewall_rules") or []
    for record in [r for r in rules if int(r.get("port") or 0) == old] if opened else []:
        remove_rule(record, run)                                     # our rules for the old port go
    state.update(port=port, updated_at=int(time.time()),
                 firewall_rules=[r for r in rules if not opened or int(r.get("port") or 0) != old] +
                 [r for r in opened if r not in rules])
    save_state(layout, state)
    return {"ok": True, "port": port, "previous": old, "changed": True, "firewall": firewall_note,
            "note": None if opened or not firewall_note else "旧端口的防火墙规则没有删除，不再需要时可以手动关闭"}


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
    progress = env_progress(layout, state)
    opts.log("  备份配置和数据…")
    progress(30, "备份配置和数据")
    archive = backup(layout, "before-upgrade")
    old_version = state.get("version")
    if state.get("service") != "none":
        progress(40, "停止 Violet（升级期间后台暂时打不开）")
        service.stop()
    try:
        progress(45, "安装新版本的程序")
        install_program(layout, opts.source)
        if opts.reuse_venv is None:
            progress(55, "安装依赖（可能要一两分钟）")
            ensure_venv(layout, opts.python, offline_wheels=opts.wheels, log=opts.log)
        progress(80, "初始化")
        done = app_command(layout, "-m", "tools.violet_setup", "init", "--json")
        if done.returncode != 0:
            raise InstallError("升级后初始化失败：\n" + (done.stderr or done.stdout or "")[-1500:])
        if state.get("service") == "systemd" and state.get("user"):
            ensure_service_user(layout, state["user"])         # files the initialiser created as root
            Service(layout, "systemd", state["user"]).register()  # the unit of the new version
        if state.get("service") == "systemd" and _is_root() and Service(layout, "systemd").register_updater():
            state["auto_update_agent"] = "systemd"             # installs from before automatic updates get them now
        write_cli(layout)                                      # the unit's ExecStartPre runs it
        if state.get("service") != "none":
            progress(88, "启动新版本")
            service.start()
            progress(93, "等待新版本就绪")
            if not wait_healthy(state["port"], "127.0.0.1", opts.health_timeout, _scheme(layout)):
                raise InstallError("新版本启动后健康检查没有通过")
    except Exception as exc:
        opts.log(f"  升级失败（{exc}），恢复旧版本…")
        progress(90, "升级失败，正在恢复旧版本")
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
    # the new version is running and healthy: the backup made for this upgrade is not needed any more (user decision,
    # 2026-10-27: no disk space used without reason). A failed upgrade keeps it (see above); `violet backup` archives
    # are never touched here. Without a service (--service none) nothing proved that the new version runs: kept.
    removed = state.get("service") != "none" and _remove_backup(archive)
    state.update(version=source_version(opts.source), previous_version=old_version, updated_at=int(time.time()),
                 last_backup=None if removed else str(archive))
    save_state(layout, state)
    write_cli(layout)
    return {"ok": True, "from": old_version, "to": state["version"], "backup": None if removed else str(archive),
            "backup_removed": removed}


def _remove_backup(archive: Path) -> bool:
    try:
        archive.unlink(missing_ok=True)
        return True
    except OSError:
        return False


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
    design). The "before-upgrade" backup is deleted once an upgrade succeeded; to be able to go back in data too, make
    a backup (`violet backup`) before upgrading."""
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
            "note": "数据没有回滚。升级成功后，升级前的备份会自动删除；如果有自己用 violet backup 做的备份，可以用 restore 恢复",
            "backup": state.get("last_backup")}


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
           windows: bool | None = None, progress: Progress | None = None) -> dict[str, Any]:
    """`violet update`: the newest GitHub release over this installation. The release's own install.sh downloads its
    archive, checks the SHA-256 and runs `upgrade` (backup first; a failed upgrade puts the old version back)."""
    layout = Layout(opts.root)
    state = load_state(layout)
    if not state:
        raise InstallError(f"{layout.root} 没有安装 Violet")
    current = state.get("version") or source_version(layout.app)
    rel = release or release_for(state)
    newer = is_newer(rel["version"], current)
    result = {"ok": True, "current": current, "latest": rel["version"], "newer": newer, "release_url": rel["url"],
              "published_at": rel.get("published_at")}
    if check_only or not newer:
        return {**result, "updated": False}
    if (os.name == "nt") if windows is None else windows:
        raise InstallError(f"Windows 上请下载 {rel['tag']} 的程序，在其目录里运行 python tools/violet_installer.py upgrade")
    if state.get("service") == "systemd" and not _is_root():
        raise InstallError(f"需要 root 权限：以 root 运行 {layout.root / 'violet'} update")
    progress = progress or Progress(None)
    work = Path(tempfile.mkdtemp(prefix="violet-update-"))
    try:
        script = work / "install.sh"
        progress(10, f"下载 {rel['version']} 的安装脚本")
        download(rel["install_url"], script)
        opts.log(f"  升级 {current} → {rel['version']}…")
        progress(15, f"下载并校验 {rel['version']}")
        env = {**os.environ, "VIOLET_PROGRESS_FILE": str(progress.path)} if progress.path else None
        done = run(["bash", str(script), "upgrade", "--root", str(layout.root), "--json"], capture_output=True,
                   text=True, encoding="utf-8", **({"env": env} if env else {}))
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


UPDATE_MODES = ("auto", "notify", "manual")
FAILED_RETRY_SECONDS = 24 * 3600


def _read_small(path: Path, limit: int = 65536) -> bytes | None:
    """A file in a folder the service user owns, read as root: never through a symlink, never more than ``limit``."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            return os.read(fd, limit)
        finally:
            os.close(fd)
    except OSError:
        return None


def update_mode(layout: Layout) -> str:
    """auto | notify | manual -- the Control Center setting (data/control/settings.json), else the configuration
    file, else auto. Only one of three words is taken from these service-owned files; anything else means auto."""
    raw = _read_small(layout.data / "control" / "settings.json")
    try:
        value = (json.loads(raw.decode("utf-8")).get("values") or {}).get("VIOLET_UPDATE_MODE") if raw else None
    except (ValueError, AttributeError):
        value = None
    if value in UPDATE_MODES:
        return value
    env = _env(layout)
    if env.get("VIOLET_UPDATE_MODE") in UPDATE_MODES:
        return env["VIOLET_UPDATE_MODE"]
    if env.get("VIOLET_UPDATE_CHECK", "").strip().lower() in ("0", "false", "no", "off"):
        return "manual"                                     # 0.2.0 switched the check off this way
    return "auto"


def take_update_request(layout: Layout) -> str | None:
    """"manual" (the admin pressed "update now"), "auto" (the page saw a newer release in automatic mode) or None.
    The request file is removed first, so the path unit does not start the update again."""
    path = layout.data / "control" / "update-request"
    if not (path.is_symlink() or path.exists()):
        return None
    raw = _read_small(path, 32) or b""
    try:
        path.unlink()
    except OSError:
        pass
    return "auto" if raw.strip() == b"auto" else "manual"


def record_auto_update(layout: Layout, **fields: Any) -> dict:
    """Store the outcome in install.json (root-owned; the Control Center reads it). Re-read first: an update has just
    rewritten that file with the new version."""
    state = load_state(layout)
    entry = {**(state.get("auto_update") or {}), **fields, "last_run_at": int(time.time())}
    state["auto_update"] = entry
    save_state(layout, state)
    return entry


def auto_update(opts: Options, release: dict | None = None, now: Callable[[], float] = time.time,
                do_update: Callable[..., dict] | None = None) -> dict[str, Any]:
    """`violet auto-update` (the root update service): update when the admin asked for it, or when the mode is
    "auto" and a newer release exists. A release that failed to install is not tried again by itself for 24 hours
    (pressing "update now" always tries)."""
    layout = Layout(opts.root)
    state = load_state(layout)
    if not state:
        raise InstallError(f"{layout.root} 没有安装 Violet")
    request = take_update_request(layout)
    mode = update_mode(layout)
    if request is None and mode != "auto":
        return {"ok": True, "action": "skipped", "mode": mode}
    # 0.7.0: the Control Center's progress dialog follows this file (the page loses Violet while it restarts and
    # reads the outcome here afterwards)
    progress = Progress(progress_path(layout, "update"), state.get("user"), task="update")
    progress(5, "检查新版本")
    try:
        rel = release or release_for(state)
    except InstallError as exc:
        record_auto_update(layout, last_result="error", last_message=str(exc), requested=request)
        progress.failed(str(exc))
        raise
    current = state.get("version") or source_version(layout.app)
    if not is_newer(rel["version"], current):
        record_auto_update(layout, last_result="up_to_date", last_message=f"已经是最新版本 {current}",
                           latest=rel["version"], requested=request)
        progress.done(f"已经是最新版本 {current}", version=current)
        return {"ok": True, "action": "up_to_date", "mode": mode, "current": current, "latest": rel["version"]}
    previous = state.get("auto_update") or {}
    if request != "manual" and previous.get("failed_version") == rel["version"] and \
            now() - float(previous.get("failed_at") or 0) < FAILED_RETRY_SECONDS:
        progress.failed(f"{rel['version']} 上次安装失败，24 小时内不再自动重试（点“立即更新”可以马上再试）")
        return {"ok": True, "action": "waiting", "mode": mode, "latest": rel["version"],
                "note": f"{rel['version']} 上次安装失败，24 小时内不再自动重试"}
    try:
        result = (do_update or update)(opts, release=rel, progress=progress)
    except InstallError as exc:
        record_auto_update(layout, last_result="failed", last_message=str(exc)[:1000], latest=rel["version"],
                           failed_version=rel["version"], failed_at=int(now()), requested=request)
        progress.failed(str(exc))
        raise
    record_auto_update(layout, last_result="updated", last_message=f"已从 {current} 升级到 {rel['version']}",
                       latest=rel["version"], updated_from=current, updated_to=rel["version"], failed_version=None,
                       failed_at=None, requested=request)
    progress.done(f"已从 {current} 升级到 {rel['version']}", version=rel["version"])
    return {"ok": True, "action": "updated", "mode": mode, **result}


def take_task_request(layout: Layout) -> str | None:
    """The word the Control Center left in <data>/control/task-request (removed first, so the path unit does not
    start again); "" for anything that is not one of TASK_ACTIONS; None when there is no request."""
    path = layout.data / "control" / "task-request"
    if not (path.is_symlink() or path.exists()):
        return None
    raw = (_read_small(path, 64) or b"").strip().decode("ascii", "replace")
    try:
        path.unlink()
    except OSError:
        pass
    return raw if raw in TASK_ACTIONS else ""


def root_task(opts: Options, run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """`violet root-task` (the root task service): do what the Control Center asked for, with progress."""
    layout = Layout(opts.root)
    state = load_state(layout)
    if not state:
        raise InstallError(f"{layout.root} 没有安装 Violet")
    task = take_task_request(layout)
    if task is None:
        return {"ok": True, "action": "none"}
    progress = Progress(progress_path(layout, "task"), state.get("user"), task=task or "unknown")
    if not task:
        progress.failed("不认识的请求，没有执行")
        return {"ok": False, "action": "rejected"}
    progress(2, "开始")
    tool, action = TASK_ACTIONS[task]
    try:
        if tool == "relay":
            result = mcp_relay(opts, action, run=run, progress=progress)
        else:
            result = searxng_command(opts, action, run=run, progress=progress)
    except InstallError as exc:
        progress.failed(str(exc))
        raise
    port = result.get("device_port")
    if task == "searxng-install":
        message = f"联网搜索已安装：{result.get('url')}"
    elif task == "searxng-uninstall":
        message = "联网搜索（SearXNG）已卸载"
    elif task in ("searxng-tune", "searxng-configure"):
        message = (f"现在用：{result.get('engines_text')}，" +
                   ("测试搜索有结果" if result.get("search_ok") else "但测试搜索没有结果（看下面的原因）"))
    else:
        message = ("中继已安装" if task == "relay-install" else "手机入口已换到新端口") + (f"，手机入口端口 {port}" if port else "")
        if result.get("healthy") is False:
            message += "（但中继还没有通过健康检查，稍后点“刷新”看看）"
    notes = [str(n) for n in result.get("notes") or result.get("removed") or []][:8]
    progress.done(message, device_port=port, notes=notes, url=result.get("url"))
    return {"ok": True, "action": task, **{k: v for k, v in result.items() if k != "ok"}}


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
        if purge:                   # 0.7.0: install.json is gone (a half-removed install): still clean up
            return purge_leftovers(layout)
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
    leftovers: list[str] = []
    if purge:
        for p in (layout.config, layout.data, layout.backups, layout.logs):
            if p.exists():
                shutil.rmtree(p)
        layout.state_file.unlink(missing_ok=True)
        kept = []
        removed += purge_system(layout, state)
        # the install folder itself, when nothing but Violet's own files is left in it
        if layout.root.exists():
            leftovers = sorted(p.name for p in layout.root.iterdir() if p.name not in OWN_ROOT_FILES)
            if not leftovers:
                shutil.rmtree(layout.root, ignore_errors=True)
                removed.append(str(layout.root))
    else:
        state.update(service="none", uninstalled_at=int(time.time()))
        save_state(layout, state)
    note = ("配置、数据和备份都保留了；彻底删除用 uninstall --purge --yes" if kept else
            "已全部删除" if not leftovers else f"已删除 Violet 的文件；{layout.root} 里还有不是 Violet 的文件，没有动："
            + "、".join(leftovers))
    return {"ok": True, "removed": removed, "kept": kept, "leftovers": leftovers, "note": note}


# what may be left in <root> after a purge and is Violet's own (pid / stop flags of the supervisor, startup logs)
OWN_ROOT_FILES = {"violet.pid", "violet.stop", "violet", "violet.cmd", "venv.path", "install.json", "install.json.tmp",
                  "app.failed", "app.new", "logs"}


def purge_system(layout: Layout, state: dict, run: Callable[..., Any] = subprocess.run,
                 linux_root: bool | None = None) -> list[str]:
    """`uninstall --purge`: what the installer set up outside <root> -- the firewall rules it opened and the system
    account it created (only if that account's home is this installation's data folder, i.e. made by us)."""
    removed: list[str] = []
    if not (linux_root if linux_root is not None else (os.name != "nt" and _is_root())):
        return removed
    removed += remove_units(layout, run)     # whatever is still registered (also after a plain `uninstall`)
    try:                                    # the phone relay, when `violet mcp-relay install` set it up (and its rule)
        removed += _relay_module().uninstall(layout, state, run=run, remove_rule=lambda r: remove_rule(r, run))
    except Exception:                       # noqa: BLE001 -- never let it stop the rest of the purge
        pass
    try:                                    # 0.7.1: the private SearXNG (`violet searxng install` / the web button)
        removed += _searxng_module().uninstall(layout, state, run=run)
    except Exception:                       # noqa: BLE001
        pass
    for rule in state.get("firewall_rules") or []:
        if rule.get("tool") not in ("ufw", "firewalld", "iptables", "ip6tables", "nftables"):
            continue
        remove_rule(rule, run)
        label = f"防火墙规则 {int(rule.get('port') or 0)}/tcp"
        if label not in removed:
            removed.append(label)
    swept = sweep_marked_rules(run)          # marked "violet" but not recorded
    if swept:
        removed.append("其他带 violet 标记的防火墙规则：" + "、".join(swept))
    user = state.get("user")
    if user:
        done = run(["getent", "passwd", user], capture_output=True, text=True)
        fields = (done.stdout or "").strip().split(":")
        if done.returncode == 0 and len(fields) >= 6 and fields[5] and Path(fields[5]) == layout.data:
            if run(["userdel", user], capture_output=True).returncode == 0:
                removed.append(f"系统账号 {user}")
    return removed


SYSTEM_UNIT_DIR = Path("/etc/systemd/system")


def remove_units(layout: Layout, run: Callable[..., Any] = subprocess.run,
                 unit_dir: Path | None = None) -> list[str]:
    """Violet's own system units (violet.service, the update and task units) when they belong to this installation
    (their service runs <root>/...). Nothing when they are already gone."""
    folder = unit_dir or SYSTEM_UNIT_DIR
    names = [f"{SERVICE_NAME}.service", *updater_units(layout)]
    found = [n for n in names if (folder / n).exists()]

    def ours(name: str) -> bool:
        try:
            return str(layout.root) in (folder / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
    if not found or not any(ours(n) for n in found if n.endswith(".service")):
        return []
    run(["systemctl", "disable", "--now", *found], capture_output=True)
    for name in found:
        (folder / name).unlink(missing_ok=True)
    run(["systemctl", "daemon-reload"], capture_output=True)
    return [f"systemd 单元 {n}" for n in found]


def purge_leftovers(layout: Layout, run: Callable[..., Any] = subprocess.run,
                    linux_root: bool | None = None) -> dict[str, Any]:
    """`uninstall --purge` without install.json: remove what a Violet installation leaves (units, the relay we
    installed, marked firewall rules, our system account, Violet's own folders in <root>) -- never anything else."""
    removed = purge_system(layout, {"user": SERVICE_USER}, run=run, linux_root=linux_root)
    known = ("app", "app.previous", "app.failed", "app.new", "venv", "config", "data", "backups", "logs", "mcp-relay",
             "searxng")
    if layout.root.exists() and ((layout.root / "app" / "tools" / "violet_installer.py").exists()
                                 or (layout.root / "config" / "violet.env").exists()
                                 or (layout.root / "data" / "control").exists()):
        for name in known:
            path = layout.root / name
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
                removed.append(name)
        for name in OWN_ROOT_FILES:
            target = layout.root / name
            if target.is_file() or target.is_symlink():
                target.unlink(missing_ok=True)
    leftovers = sorted(p.name for p in layout.root.iterdir()) if layout.root.exists() else []
    if layout.root.exists() and not leftovers:
        layout.root.rmdir()
        removed.append(str(layout.root))
    note = ("没有找到安装记录；已清理残留：" + "、".join(removed) if removed else
            f"{layout.root} 没有安装 Violet，也没有找到残留")
    if leftovers:
        note += f"；{layout.root} 里还有不是 Violet 的文件，没有动：" + "、".join(leftovers)
    return {"ok": True, "removed": removed, "kept": [], "leftovers": leftovers, "note": note}


def _relay_module():
    try:
        import violet_relay                 # run as a script: <app>/tools is on sys.path
    except ImportError:
        from tools import violet_relay
    return violet_relay


RELAY_ACTIONS = ("install", "export", "status", "uninstall", "set-port")


def mcp_relay(opts: Options, action: str, run: Callable[..., Any] = subprocess.run,
              progress: Callable[[int, str], None] | None = None) -> dict[str, Any]:
    """`violet mcp-relay install | export | status | uninstall | set-port` (tools/violet_relay.py). opts.port: the
    device port for install / set-port (else a random free one)."""
    relay = _relay_module()
    if action not in RELAY_ACTIONS:
        raise InstallError("用法：violet mcp-relay install | export | status | uninstall | set-port [端口]")
    layout = Layout(opts.root)
    state = load_state(layout)
    if not state:
        raise InstallError(f"{layout.root} 没有安装 Violet")
    if action != "status" and (os.name == "nt" or not _is_root()):
        raise InstallError("中继要以 root 在 Linux 上安装（/opt/violet/violet 会自动切换到 root）")
    try:
        if action == "install":
            result = relay.install(layout, state, run=run, port_free=port_free, port=opts.port,
                                   open_port=lambda p: open_port_everywhere(p, run), public_ip=public_ip,
                                   progress=progress)
            save_state(layout, state)
        elif action == "set-port":
            result = relay.set_port(layout, state, run=run, port_free=port_free, port=opts.port,
                                    open_port=lambda p: open_port_everywhere(p, run),
                                    remove_rule=lambda r: remove_rule(r, run), public_ip=public_ip,
                                    progress=progress)
            save_state(layout, state)
        elif action == "export":            # violet.service runs this before every start: no network lookups
            result = relay.export(layout, state, run=run)
        elif action == "status":
            result = relay.status(state)
        else:
            result = {"ok": True, "removed": relay.uninstall(layout, state, run=run,
                                                            remove_rule=lambda r: remove_rule(r, run))}
            save_state(layout, state)
    except relay.RelayError as exc:
        raise InstallError(str(exc)) from None
    return {"ok": True, **result}


def _searxng_module():
    try:
        import violet_searxng                 # run as a script: <app>/tools is on sys.path
    except ImportError:
        from tools import violet_searxng
    return violet_searxng


SEARXNG_ACTIONS = ("install", "uninstall", "status", "tune", "configure")


def searxng_command(opts: Options, action: str, run: Callable[..., Any] = subprocess.run,
                    progress: Callable[[int, str], None] | None = None) -> dict[str, Any]:
    """`violet searxng install | uninstall | status` (tools/violet_searxng.py): the private web-search service."""
    mod = _searxng_module()
    if action not in SEARXNG_ACTIONS:
        raise InstallError("用法：violet searxng install | uninstall | status | tune | configure")
    layout = Layout(opts.root)
    state = load_state(layout)
    if not state:
        raise InstallError(f"{layout.root} 没有安装 Violet")
    if action != "status" and (os.name == "nt" or not _is_root()):
        raise InstallError("联网搜索要以 root 在 Linux 上安装（/opt/violet/violet 会自动切换到 root）")
    try:
        if action == "install":
            result = mod.install(layout, state, python=venv_python(layout), port_free=port_free, run=run,
                                 progress=progress)
            save_state(layout, state)
        elif action == "uninstall":
            result = {"ok": True, "removed": mod.uninstall(layout, state, run=run)}
            save_state(layout, state)
        elif action == "tune":
            result = mod.tune(layout, state, run=run, progress=progress)
            save_state(layout, state)
        elif action == "configure":                  # the page's choice: <data>/control/searxng-request.json
            result = mod.configure(layout, state, run=run, progress=progress)
            save_state(layout, state)
        else:
            result = mod.status(state)
    except mod.SearxngError as exc:
        raise InstallError(str(exc)) from None
    return {"ok": True, **result}


def render_relay(result: dict[str, Any], action: str) -> str:
    if action == "uninstall":
        return "已删除：" + ("、".join(result.get("removed") or []) or "没有由 Violet 安装的中继")
    if not result.get("installed"):
        return "这台服务器上还没有中继。安装：/opt/violet/violet mcp-relay install"
    lines = ["", "Violet MCP 中继", "-" * 40]
    if action in ("install", "set-port") and "healthy" in result:
        lines.append("状态：" + ("运行正常" if result.get("healthy") else "没有通过健康检查"))
    elif "healthy" in result:
        lines.append("状态：" + ("运行正常" if result["healthy"] else "没有响应") +
                     ("，手机已连接" if result.get("device_connected") else "，手机未连接"))
    if result.get("device_url"):
        lines.append(f"手机入口：{result['device_url']}")
    lines.append("设备密钥和证书指纹：在后台“工具”页的“手机 MCP”里查看和一键复制")
    lines += [f"  {n}" for n in result.get("notes") or []]
    return "\n".join(lines + [""])


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
        path.write_text(cli_script(py, installer, layout.root, elevate=_is_root()), encoding="utf-8")
        os.chmod(path, 0o755)


def cli_script(py: Path, installer: Path, root: Path, elevate: bool) -> str:
    """The <root>/violet wrapper. An installation made by root manages root-owned files and services: run by a normal
    user it runs itself again through sudo, so the same command works on Debian as root (no sudo there) and on Ubuntu
    as a sudo user."""
    lines = ["#!/bin/sh"]
    if elevate:
        lines += ['if [ "$(id -u)" != "0" ]; then',
                  '  if command -v sudo >/dev/null 2>&1; then exec sudo "$0" "$@"; fi',
                  '  echo "需要 root 权限：先切换到 root（su -）再运行" >&2',
                  '  exit 1',
                  'fi']
    lines.append(f'exec "{py}" "{installer}" "$@" --root "{root}"')
    return "\n".join(lines) + "\n"


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
        lines.append(f"  （自签名证书，浏览器会提示“不安全”，选择“继续访问”即可；核对证书指纹 SHA-256 "
                     f"{result['tls_fingerprint'][:47]}…）")
    if result.get("access") == "public":
        lines.append(f"  打不开时：在云服务器控制台的“安全组 / 防火墙”里放行 TCP 端口 {result['port']}"
                     "（服务器自己无法检查这一层）")
        if not result.get("public_ip"):
            lines.append("  没有查到公网 IP：用“https://你的服务器公网 IP:端口/安全入口”打开")
    if result.get("access") == "local":
        lines.append("  后台只在本机监听。远程管理：ssh -L "
                     f"{result['port']}:127.0.0.1:{result['port']} 用户@服务器，然后在自己电脑打开上面的地址")
    admin = result.get("admin") or {}
    if admin.get("entrance"):
        lines.append(f"  安全入口：/{admin['entrance']}（地址里必须带上它，否则所有页面都显示 404）")
    lines.append("")
    lines.append(f"用户名：{admin.get('user') or 'admin'}")
    if admin.get("password"):
        lines += [f"密码：{admin['password']}", "  随机生成的 20 位密码，只显示这一次，请保存好；可以一直用，也可以在后台“账号安全”里改。"]
    else:
        lines.append("密码：沿用已有的密码")
    lines.append(f"  忘记了：{Path(result.get('root', '.')) / 'violet'} info（地址和用户名）| reset-password（生成新密码）")
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
              "start | backup | update（升级到 GitHub 上的最新版本）| info | reset-password | reset-entrance | "
              "set-port <端口> | uninstall", ""]
    return "\n".join(lines)


def render_account(result: dict[str, Any], command: str) -> str:
    lines = ["", "Violet 管理后台", "-" * 40]
    lines += [f"  {u}" for u in result.get("urls") or []]
    lines.append(f"安全入口：/{result['entrance']}" if result.get("entrance")
                 else "安全入口：未设置（任何人都能打开登录页；建议执行 reset-entrance 设置一个）")
    lines.append(f"用户名：{result.get('user')}" + ("" if result.get("username_set") else "（未设置：登录时用户名可以随便填）"))
    if result.get("password"):
        lines += [f"新密码：{result['password']}", "  随机生成，只显示这一次，请保存好；所有登录已失效。"]
    elif command == "info":
        lines.append("密码：" + ("随机生成的密码（不能查看；忘记了用 reset-password）" if result.get("must_change") else "已设置（不能查看；"
                                                                               "忘记了用 reset-password）"))
    if command == "reset-entrance":
        lines.append("安全入口已更换：其他浏览器要用上面的新地址重新进入。")
    if result.get("access") == "local":
        lines.append(f"后台只在本机监听：远程管理用 ssh -L {result.get('port')}:127.0.0.1:{result.get('port')} 用户@服务器")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="violet-installer", description="Violet installer")
    parser.add_argument("command", choices=["install", "upgrade", "update", "rollback", "status", "backup", "restore",
                                            "uninstall", "doctor", "start", "stop", "restart", "info",
                                            "reset-password", "reset-entrance", "set-port", "auto-update",
                                            "firewall-apply", "mcp-relay", "root-task", "searxng"])
    parser.add_argument("--update-feed", help=argparse.SUPPRESS)  # install: a release.json instead of GitHub
    parser.add_argument("--update-repo", help="install: GitHub owner/repo to update from (a fork)")
    parser.add_argument("--check", action="store_true", help="update: only report whether a newer release exists")
    parser.add_argument("archive", nargs="?", help="restore: the backup file; set-port: the new port; "
                                                   "mcp-relay: install | export | status | uninstall | set-port")
    parser.add_argument("value", nargs="?", help="mcp-relay set-port: the new device port (else a random one)")
    parser.add_argument("--entrance", help="reset-entrance: this value instead of a random one")
    parser.add_argument("--no-entrance", action="store_true", help="reset-entrance: remove the security entrance")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--port", type=int)
    parser.add_argument("--access", choices=["local", "lan", "public", "proxy"])
    parser.add_argument("--service", choices=["auto", "systemd", "systemd-user", "supervisor", "none"], default="auto")
    parser.add_argument("--user", help="systemd: the account Violet runs as")
    parser.add_argument("--wheels", type=Path, help="install requirements offline from this folder")
    parser.add_argument("--reuse-venv", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--public-url", help="the address to try for the public-access check")
    parser.add_argument("--open-firewall", action="store_true")
    parser.add_argument("--keep-firewall", action="store_true", help="public access: do not open the port in ufw/firewalld")
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
                   health_timeout=args.health_timeout, json_output=args.json, log=out, update_feed=args.update_feed,
                   update_repo=args.update_repo, keep_firewall=args.keep_firewall)
    try:
        if args.command == "install":
            if not opts.access:
                opts.access = load_state(Layout(root)).get("access") or default_access()
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
                    out(f"已升级到 {result['to']}" + (f"；升级前的备份：{result['backup']}" if result.get("backup")
                                                     else "；升级成功，升级前的备份已删除"))
        elif args.command in ("info", "reset-password", "reset-entrance"):
            result = account(opts, args.command, "" if args.no_entrance else args.entrance)
            if not args.json:
                print(render_account(result, args.command))
                return 0
        elif args.command == "set-port":
            if opts.port is None and args.archive:
                try:
                    opts.port = int(args.archive)
                except ValueError:
                    raise InstallError(f"端口要是数字：{args.archive}") from None
            result = set_port(opts)
            if not args.json:
                out(f"后台端口已改为 {result['port']}" if result["changed"] else f"端口没有变化：{result['port']}")
                for note in (result.get("firewall"), result.get("note")):
                    if note:
                        out("  " + note)
                out(f"  新地址：{root / 'violet'} info 查看")
                return 0
        elif args.command == "auto-update":
            result = auto_update(opts)
        elif args.command == "firewall-apply":
            result = firewall_apply(Layout(root))
        elif args.command == "root-task":
            result = root_task(opts)
        elif args.command == "searxng":
            result = searxng_command(opts, args.archive or "status")
        elif args.command == "mcp-relay":
            action = args.archive or "status"
            if args.value is not None:
                if action not in ("install", "set-port"):
                    raise InstallError(f"多余的参数：{args.value}")
                try:
                    opts.port = int(args.value)
                except ValueError:
                    raise InstallError(f"端口要是数字：{args.value}") from None
            result = mcp_relay(opts, action)
            if not args.json:
                print(render_relay(result, action))
                return 0
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
