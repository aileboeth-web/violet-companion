"""`violet napcat install | uninstall | status` -- QQ on a personal account for Violet (2026-10-29, channel "QQ").

NapCat (github.com/NapNeko/NapCatQQ) runs the official Linux QQ without a screen and offers OneBot v11; Violet talks
to it over one local WebSocket. Installed with NapCat's own installer script in its "Shell" mode (no Docker): the
script installs the system libraries (xvfb, nss, gtk ...), the Linux QQ from Tencent's CDN (qqdl.gtimg.cn) and NapCat
from GitHub (it picks the fastest GitHub mirror itself, which matters for servers in mainland China).

  <root>/napcat                 home of the system account violet-qq; the installer puts QQ in <root>/napcat/Napcat
  .../napcat/config/webui.json  NapCat's management API: 127.0.0.1 only, random port and token (QR login goes through it)
  .../napcat/config/onebot11.json   one WebSocket server: 127.0.0.1 only, random port and token
  violet-napcat.service         xvfb-run qq --no-sandbox as violet-qq; nothing is opened in any firewall
  <data>/control/napcat.json    what Violet needs (both addresses and tokens), owned by Violet's account, 0640

uninstall removes all of it (service, folder, account, the export); `uninstall --purge` of Violet calls it.
Standard library only; run as root.
"""

from __future__ import annotations

import json
import os
import random
import secrets
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

UNIT_NAME = "violet-napcat"
UNIT_PATH = Path("/etc/systemd/system") / f"{UNIT_NAME}.service"
ACCOUNT = "violet-qq"
MARK = "# managed by Violet (tools/violet_napcat.py)"
SCRIPT_PATH = "NapNeko/NapCat-Installer/main/script/install.sh"
SCRIPT_URLS = (f"https://raw.githubusercontent.com/{SCRIPT_PATH}",
               f"https://ghfast.top/https://raw.githubusercontent.com/{SCRIPT_PATH}",
               f"https://gh-proxy.com/https://raw.githubusercontent.com/{SCRIPT_PATH}",
               f"https://nclatest.znin.net/{SCRIPT_PATH}")
MAX_SCRIPT = 512 * 1024
DEFAULT_WS_PORT, DEFAULT_WEBUI_PORT = 3001, 6099
RANDOM_PORTS = range(20000, 30000)
START_WAIT = 150.0
Progress = Callable[[int, str], None]


class NapcatError(RuntimeError):
    pass


def folder(layout: Any) -> Path:
    return layout.root / "napcat"


def qq_dir(base: Path) -> Path:
    return base / "Napcat" / "opt" / "QQ"


def napcat_dir(base: Path) -> Path:
    return qq_dir(base) / "resources" / "app" / "app_launcher" / "napcat"


def export_path(data: Path) -> Path:
    return data / "control" / "napcat.json"


def _say(progress: Progress | None, percent: int, step: str) -> None:
    if progress is not None:
        progress(percent, step)


def _fetch(url: str, limit: int, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "violet-installer"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:          # noqa: S310 -- fixed https URLs
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise NapcatError("下载的安装脚本太大")
    return data


def fetch_script(fetch: Callable[..., bytes] = _fetch) -> tuple[bytes, str]:
    """NapCat's installer script from GitHub or a mirror: the first answer that really is that script."""
    problems = []
    for url in SCRIPT_URLS:
        try:
            data = fetch(url, MAX_SCRIPT)
        except Exception as exc:                       # noqa: BLE001 -- try the next mirror
            problems.append(f"{url.split('/')[2]}：{exc.__class__.__name__}")
            continue
        text = data.decode("utf-8", "replace")
        if text.startswith("#!") and "NapCat" in text and "--docker" in text:
            return data, url
        problems.append(f"{url.split('/')[2]}：不是 NapCat 的安装脚本")
    raise NapcatError("下载 NapCat 安装脚本失败（GitHub 和镜像都不通）：" + "；".join(problems))


def _stream(cmd: list[str], on_line: Callable[[str], None], env: dict | None = None,
            cwd: Path | None = None) -> tuple[int, str]:
    tail: list[str] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                            encoding="utf-8", errors="replace", env=env, cwd=cwd)
    assert proc.stdout is not None
    for line in proc.stdout:
        tail = (tail + [line.rstrip()])[-40:]
        on_line(line)
    return proc.wait(), "\n".join(tail)


def _pick_port(preferred: int, port_free: Callable[[int, str], bool], taken: set[int],
               rng: random.Random | None) -> int:
    if preferred not in taken and port_free(preferred, "127.0.0.1"):
        return preferred
    rng = rng or random.SystemRandom()
    for _ in range(200):
        port = rng.randrange(RANDOM_PORTS.start, RANDOM_PORTS.stop)
        if port not in taken and port_free(port, "127.0.0.1"):
            return port
    raise NapcatError("找不到空闲端口")


def onebot_config(ws_port: int, ws_token: str) -> dict[str, Any]:
    """NapCat's OneBot 11 file (format of napneko.github.io/config/basic): only one WebSocket server, local only."""
    return {"network": {"httpServers": [], "httpSseServers": [], "httpClients": [], "websocketClients": [],
                        "plugins": [],
                        "websocketServers": [{"name": "violet", "enable": True, "host": "127.0.0.1", "port": ws_port,
                                              "messagePostFormat": "array", "reportSelfMessage": False,
                                              "token": ws_token, "enableForcePushEvent": True, "debug": False,
                                              "heartInterval": 30000}]},
            "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": False}


def webui_config(port: int, token: str) -> dict[str, Any]:
    return {"host": "127.0.0.1", "port": port, "token": token, "loginRate": 30}


def unit_text(base: Path) -> str:
    qq = qq_dir(base) / "qq"
    return "\n".join([
        "[Unit]", "Description=QQ for Violet (NapCat, OneBot v11 on 127.0.0.1)", MARK,
        "After=network-online.target", "Wants=network-online.target", "",
        "[Service]", f"User={ACCOUNT}", f"Group={ACCOUNT}", f"Environment=HOME={base}",
        f"WorkingDirectory={qq_dir(base)}",
        f"ExecStart=/usr/bin/xvfb-run -a {qq} --no-sandbox",
        "Restart=always", "RestartSec=10", "KillMode=control-group", "TimeoutStopSec=20",
        "NoNewPrivileges=yes", "PrivateTmp=yes", "", "[Install]", "WantedBy=multi-user.target", ""])


def _write_json(path: Path, value: Any, uid: int | None, gid: int | None, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        if uid is not None and hasattr(os, "fchown"):
            os.fchown(fh.fileno(), uid, gid if gid is not None else -1)
        json.dump(value, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _ids(name: str | None) -> tuple[int | None, int | None]:
    if not name:
        return None, None
    try:
        import pwd
        account = pwd.getpwnam(name)
        return account.pw_uid, account.pw_gid
    except (ImportError, KeyError):
        return None, None


def _ensure_account(base: Path, run: Callable[..., Any]) -> None:
    if run(["getent", "passwd", ACCOUNT], capture_output=True).returncode == 0:
        return
    nologin = "/usr/sbin/nologin" if Path("/usr/sbin/nologin").exists() else "/sbin/nologin"
    done = run(["useradd", "--system", "--home-dir", str(base), "--no-create-home", "--shell", nologin, ACCOUNT],
               capture_output=True, text=True)
    if getattr(done, "returncode", 0) != 0:
        raise NapcatError("创建系统账号 violet-qq 失败：" + (getattr(done, "stderr", "") or "")[-300:])


APT_STATIC = ("zip", "unzip", "jq", "curl", "xvfb", "screen", "xauth", "procps", "rpm2cpio", "cpio", "libnss3",
              "libgbm1", "libgssapi-krb5-2")
APT_T64 = ("libglib2.0-0", "libatk1.0-0", "libatspi2.0-0", "libgtk-3-0", "libasound2")


def _apt_installable(name: str, run: Callable[..., Any]) -> bool:
    """A package apt can really install (a candidate version), not only a name some package mentions."""
    done = run(["apt-cache", "policy", name], capture_output=True, text=True)
    for line in (getattr(done, "stdout", "") or "").splitlines():
        if line.strip().startswith("Candidate:"):
            return line.split(":", 1)[1].strip() not in ("", "(none)")
    return False


def install_apt_dependencies(run: Callable[..., Any], stream: Callable[..., tuple[int, str]],
                             on_line: Callable[[str], None], env: dict) -> bool:
    """Debian / Ubuntu: the libraries QQ needs, installed by Violet. NapCat's script decides "<lib>t64" by
    `apt-cache show`, which also answers for a name that is only mentioned -- on Ubuntu 22.04 it then asks for
    libglib2.0-0t64 and apt fails (install-linux 2026-10-29). Here a "t64" name is used only when apt has a candidate
    for it. False when this is not an apt system (the script installs the dependencies itself)."""
    if not shutil.which("apt-get") and run is subprocess.run:
        return False
    code, tail = stream(["apt-get", "update", "-y", "-qq"], on_line, env, None)
    if code != 0:
        raise NapcatError("apt-get update 失败（检查软件源）：\n" + tail[-800:])
    packages = list(APT_STATIC) + [f"{p}t64" if _apt_installable(f"{p}t64", run) else p for p in APT_T64]
    code, tail = stream(["apt-get", "install", "-y", "-qq", *packages], on_line, env, None)
    if code != 0:
        raise NapcatError("安装 QQ 需要的系统库失败（检查软件源）：\n" + tail[-800:])
    return True


def install(layout: Any, state: dict[str, Any], *, port_free: Callable[[int, str], bool],
            run: Callable[..., Any] = subprocess.run, fetch: Callable[..., bytes] = _fetch,
            stream: Callable[..., tuple[int, str]] = _stream, progress: Progress | None = None,
            unit_path: Path = UNIT_PATH, wait: float = START_WAIT, rng: random.Random | None = None,
            check_webui: Callable[[int], bool] | None = None) -> dict[str, Any]:
    """Install (or reinstall: keeps ports, tokens and the QQ login) NapCat and start it. Updates `state`."""
    if not shutil.which("systemctl") and run is subprocess.run:
        raise NapcatError("这台服务器没有 systemd，QQ（NapCat）需要手动部署")
    if unit_path.exists() and MARK not in unit_path.read_text(encoding="utf-8", errors="replace"):
        raise NapcatError("这台服务器上已经有一个不是 Violet 装的 violet-napcat 服务，没有改动")
    base = folder(layout)
    previous = state.get("napcat") or {}
    notes: list[str] = []
    _say(progress, 3, "准备账号和目录")
    base.mkdir(parents=True, exist_ok=True)
    _ensure_account(base, run)
    if unit_path.exists():
        run(["systemctl", "stop", f"{UNIT_NAME}.service"], capture_output=True)

    _say(progress, 6, "下载 NapCat 安装脚本")
    script, source = fetch_script(fetch)
    work = base / "install-work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    (work / "napcat.sh").write_bytes(script)
    seen = [10]

    def on_line(line: str) -> None:                  # 10 % -> 80 %: one small step per line of the installer
        text = line.strip()
        if text and seen[0] < 80 and any(k in text for k in ("下载", "安装", "解压", "Download", "Install", "%", "完成",
                                                              "Get:", "Unpacking", "Setting up")):
            seen[0] += 1
            _say(progress, seen[0], "安装 QQ 和 NapCat：" + text[:90])
    env = {**os.environ, "HOME": str(base), "DEBIAN_FRONTEND": "noninteractive", "LANG": "C.UTF-8"}
    _say(progress, 10, "安装 QQ 需要的系统库（几分钟）")
    skip = install_apt_dependencies(run, stream, on_line, env)
    _say(progress, max(seen[0], 30), "安装 Linux QQ 和 NapCat")
    cmd = ["bash", str(work / "napcat.sh"), "--docker", "n", "--cli", "n"] + (["--skip-deps"] if skip else [])
    code, tail = stream(cmd, on_line, env, work)
    shutil.rmtree(work, ignore_errors=True)
    qq = qq_dir(base) / "qq"
    if code != 0 or (run is subprocess.run and not qq.exists()):
        import re
        clean = [re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip() for line in tail.splitlines()]
        last = "\n".join([line for line in clean if line][-12:])
        raise NapcatError(f"NapCat 安装脚本失败（退出码 {code}）：\n" + last[-1200:])

    _say(progress, 82, "写配置（只监听本机）")
    taken: set[int] = set()
    ws_port = int(previous.get("ws_port") or 0) or _pick_port(DEFAULT_WS_PORT, port_free, taken, rng)
    taken.add(ws_port)
    webui_port = int(previous.get("webui_port") or 0) or _pick_port(DEFAULT_WEBUI_PORT, port_free, taken, rng)
    ws_token = previous.get("ws_token") or secrets.token_urlsafe(24)
    webui_token = previous.get("webui_token") or secrets.token_urlsafe(24)
    uid, gid = _ids(ACCOUNT)
    config = napcat_dir(base) / "config"
    _write_json(config / "onebot11.json", onebot_config(ws_port, ws_token), uid, gid)
    _write_json(config / "webui.json", webui_config(webui_port, webui_token), uid, gid)
    for old in config.glob("onebot11_*.json"):         # per-account copies made before: same server, same token
        _write_json(old, onebot_config(ws_port, ws_token), uid, gid)
    run(["chown", "-R", f"{ACCOUNT}:{ACCOUNT}", str(base)], capture_output=True)
    os.chmod(base, 0o750)

    _say(progress, 86, "注册并启动服务")
    unit_path.write_text(unit_text(base), encoding="utf-8")
    run(["systemctl", "daemon-reload"], capture_output=True)
    run(["systemctl", "enable", f"{UNIT_NAME}.service"], capture_output=True)
    done = run(["systemctl", "restart", f"{UNIT_NAME}.service"], capture_output=True, text=True)
    if getattr(done, "returncode", 0) != 0:
        raise NapcatError("QQ 服务启动失败：" + (getattr(done, "stderr", "") or "")[-400:] +
                          f"\n查看原因：journalctl -u {UNIT_NAME} -n 60")
    _say(progress, 90, "等待 QQ 启动")
    check = check_webui or webui_up
    deadline = time.monotonic() + wait
    ok = check(webui_port)
    while not ok and time.monotonic() < deadline:
        time.sleep(3)
        ok = check(webui_port)
    if not ok:
        notes.append(f"QQ {wait:.0f} 秒内还没有启动完，稍后在渠道页刷新；原因可以看 journalctl -u {UNIT_NAME} -n 60")
    state["napcat"] = {"managed": True, "ws_port": ws_port, "webui_port": webui_port, "ws_token": ws_token,
                       "webui_token": webui_token, "installed_at": int(time.time()), "script": source}
    _say(progress, 98, "交给 Violet")
    export(layout, state)
    notes.append("在后台“聊天渠道 → QQ”里扫码登录你给 Violet 用的 QQ 号（建议用小号）")
    return {"ok": True, "installed": True, "started": ok, "ws_port": ws_port, "notes": notes}


def export(layout: Any, state: dict[str, Any]) -> None:
    info = state.get("napcat") or {}
    value = {"installed": True, "ws_url": f"ws://127.0.0.1:{info['ws_port']}", "ws_token": info["ws_token"],
             "webui_url": f"http://127.0.0.1:{info['webui_port']}", "webui_token": info["webui_token"],
             "installed_at": info.get("installed_at")}
    uid, gid = _ids(state.get("user"))
    _write_json(export_path(layout.data), value, uid, gid, 0o640)


def webui_up(port: int, timeout: float = 3.0) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/auth/login", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=timeout) as resp:
            return resp.status == 200
    except OSError:
        return False


def uninstall(layout: Any, state: dict[str, Any], *, run: Callable[..., Any] = subprocess.run,
              unit_path: Path = UNIT_PATH) -> list[str]:
    """Only what this module installed (marked unit, its folder, its account). Updates `state`."""
    removed: list[str] = []
    if unit_path.exists():
        if MARK not in unit_path.read_text(encoding="utf-8", errors="replace"):
            return removed
        run(["systemctl", "disable", "--now", f"{UNIT_NAME}.service"], capture_output=True)
        unit_path.unlink(missing_ok=True)
        run(["systemctl", "daemon-reload"], capture_output=True)
        removed.append(f"服务 {UNIT_NAME}")
    base = folder(layout)
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
        removed.append(str(base))
    done = run(["getent", "passwd", ACCOUNT], capture_output=True, text=True)
    fields = (getattr(done, "stdout", "") or "").strip().split(":")
    if getattr(done, "returncode", 1) == 0 and len(fields) >= 6 and Path(fields[5]) == base:
        if run(["userdel", ACCOUNT], capture_output=True).returncode == 0:
            removed.append(f"系统账号 {ACCOUNT}")
    state.pop("napcat", None)
    export_path(layout.data).unlink(missing_ok=True)
    return removed


def status(state: dict[str, Any], unit_path: Path = UNIT_PATH) -> dict[str, Any]:
    info = state.get("napcat") or {}
    if not unit_path.exists() or not info:
        return {"installed": False}
    return {"installed": True, "ws_port": info.get("ws_port"), "webui_port": info.get("webui_port"),
            "running": webui_up(int(info.get("webui_port") or 0)), "installed_at": info.get("installed_at")}
