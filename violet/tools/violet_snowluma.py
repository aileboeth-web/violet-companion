"""`violet qq install | uninstall | status | restart` -- QQ on a personal account for Violet (channel "QQ").

2026-10-29 (user: NapCat's risk control got too strict): SnowLuma (github.com/SnowLuma/SnowLuma, OneBot v11,
source-available, non-commercial use) instead of NapCat. It is run the way its authors recommend: their Docker image,
which holds the official Linux QQ of the version SnowLuma's hooks were made for (and stops QQ's silent hot update),
a virtual screen and a VNC server for it. QQ can only be logged in by scanning the QR code in its own window, so the
channels page shows that window (ai/channels/vnc.py over the VNC port).

  docker                         installed when missing (the distribution's docker.io, else get.docker.com through
                                 the Aliyun mirror); the image is pulled from Docker Hub or a mirror (1ms / xuanyuan /
                                 DaoCloud) and tagged with its real name
  container violet-snowluma      label violet.managed=snowluma, restart unless-stopped; published on 127.0.0.1 only:
                                 OneBot WebSocket (container 3001) and VNC (container 5900) on host ports picked here
  <root>/snowluma/data           SnowLuma's data (/app/data); config/onebot.json: one WebSocket server with Violet's
                                 token, no HTTP server; readable by Violet's group (the token of the account file)
  <root>/snowluma/qq-config, qq-data   QQ's own files (/app/.config, /app/.local/share): the login, owner only
  violet-qq                      system account the container runs QQ as (SNOWLUMA_UID; group = Violet's group)
  <data>/control/snowluma.json   what Violet needs (WebSocket address and token, VNC port and password), 0640

A NapCat that Violet 0.7.8 / 0.7.9 installed is removed first (remove_napcat). uninstall removes all of it (container,
image, folder, account, export); `uninstall --purge` of Violet calls it. Standard library only; run as root.
"""

from __future__ import annotations

import json
import os
import random
import secrets
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

IMAGE = "motricseven7/snowluma:v2.2.1"
MIRRORS = ("", "docker.1ms.run/", "docker.xuanyuan.me/", "docker.m.daocloud.io/")
CONTAINER = "violet-snowluma"
LABEL_KEY, LABEL_VALUE = "violet.managed", "snowluma"
ACCOUNT = "violet-qq"
DOCKER_SCRIPTS = ("https://get.docker.com", "https://test.docker.com")
MAX_SCRIPT = 1024 * 1024
DEFAULT_WS_PORT, DEFAULT_VNC_PORT = 3001, 5900
RANDOM_PORTS = range(20000, 30000)
START_WAIT = 120.0
Progress = Callable[[int, str], None]

# what Violet 0.7.8 / 0.7.9 installed for NapCat (tools/violet_napcat.py, gone since SnowLuma)
NAPCAT_UNIT = Path("/etc/systemd/system/violet-napcat.service")
NAPCAT_MARK = "# managed by Violet (tools/violet_napcat.py)"


class SnowlumaError(RuntimeError):
    pass


def folder(layout: Any) -> Path:
    return layout.root / "snowluma"


def export_path(data: Path) -> Path:
    return data / "control" / "snowluma.json"


def _say(progress: Progress | None, percent: int, step: str) -> None:
    if progress is not None:
        progress(percent, step)


def _fetch(url: str, limit: int, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "violet-installer"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:          # noqa: S310 -- fixed https URLs
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise SnowlumaError("下载的安装脚本太大")
    return data


def _stream(cmd: list[str], on_line: Callable[[str], None], env: dict | None = None) -> tuple[int, str]:
    tail: list[str] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                            encoding="utf-8", errors="replace", env=env)
    assert proc.stdout is not None
    for line in proc.stdout:
        tail = (tail + [line.rstrip()])[-40:]
        on_line(line)
    return proc.wait(), "\n".join(tail)


def _last(tail: str, n: int = 12) -> str:
    import re
    clean = [re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip() for line in tail.splitlines()]
    return "\n".join([line for line in clean if line][-n:])[-1200:]


def _pick_port(preferred: int, port_free: Callable[[int, str], bool], taken: set[int],
               rng: random.Random | None) -> int:
    if preferred not in taken and port_free(preferred, "127.0.0.1"):
        return preferred
    rng = rng or random.SystemRandom()
    for _ in range(200):
        port = rng.randrange(RANDOM_PORTS.start, RANDOM_PORTS.stop)
        if port not in taken and port_free(port, "127.0.0.1"):
            return port
    raise SnowlumaError("找不到空闲端口")


def ws_server(token: str) -> dict[str, Any]:
    """SnowLuma's WebSocket server entry (snowluma.github.io/zh/docs/guide/configuration): all addresses INSIDE the
    container, published on the host's 127.0.0.1 only."""
    return {"name": "ws-default", "enabled": True, "host": "0.0.0.0", "port": 3001, "path": "/", "role": "Universal",
            "accessToken": token, "messageFormat": "array", "reportSelfMessage": False}


def onebot_config(token: str, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """onebot.json (and an account's onebot_<uin>.json): only Violet's WebSocket server, nothing over HTTP. An empty
    list stays empty when the container starts (its start script only fills lists that are missing)."""
    value = dict(previous or {})
    networks = dict(value.get("networks") or {})
    networks.update(httpServers=[], httpClients=[], wsServers=[ws_server(token)], wsClients=[])
    value["networks"] = networks
    value.setdefault("musicSignUrl", "")
    return value


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


def _home_of(name: str, run: Callable[..., Any]) -> Path | None:
    done = run(["getent", "passwd", name], capture_output=True, text=True)
    fields = (getattr(done, "stdout", "") or "").strip().split(":")
    return Path(fields[5]) if getattr(done, "returncode", 1) == 0 and len(fields) >= 6 and fields[5] else None


def _ensure_account(base: Path, run: Callable[..., Any]) -> None:
    if run(["getent", "passwd", ACCOUNT], capture_output=True).returncode == 0:
        return
    nologin = "/usr/sbin/nologin" if Path("/usr/sbin/nologin").exists() else "/sbin/nologin"
    done = run(["useradd", "--system", "--home-dir", str(base), "--no-create-home", "--shell", nologin, ACCOUNT],
               capture_output=True, text=True)
    if getattr(done, "returncode", 0) != 0:
        raise SnowlumaError(f"创建系统账号 {ACCOUNT} 失败：" + (getattr(done, "stderr", "") or "")[-300:])


# ------------------------------------------------------------------------------------------------ NapCat (old)

def remove_napcat(layout: Any, state: dict[str, Any], run: Callable[..., Any] = subprocess.run,
                  unit_path: Path = NAPCAT_UNIT) -> list[str]:
    """What Violet 0.7.8 / 0.7.9 installed for NapCat: its marked unit, <root>/napcat, the violet-qq account whose
    home that is, the export. Anything else is left alone."""
    removed: list[str] = []
    if unit_path.exists() and NAPCAT_MARK in unit_path.read_text(encoding="utf-8", errors="replace"):
        run(["systemctl", "disable", "--now", unit_path.name], capture_output=True)
        unit_path.unlink(missing_ok=True)
        run(["systemctl", "daemon-reload"], capture_output=True)
        removed.append("服务 violet-napcat（NapCat）")
    base = layout.root / "napcat"
    if base.is_dir() and not base.is_symlink():
        shutil.rmtree(base, ignore_errors=True)
        removed.append(str(base))
    if _home_of(ACCOUNT, run) == base and run(["userdel", ACCOUNT], capture_output=True).returncode == 0:
        removed.append(f"系统账号 {ACCOUNT}（NapCat）")
    state.pop("napcat", None)
    (layout.data / "control" / "napcat.json").unlink(missing_ok=True)
    return removed


# ------------------------------------------------------------------------------------------------ docker

def docker_ready(run: Callable[..., Any]) -> bool:
    done = run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True)
    return getattr(done, "returncode", 1) == 0 and bool((getattr(done, "stdout", "") or "").strip())


def ensure_docker(run: Callable[..., Any], stream: Callable[..., tuple[int, str]], on_line: Callable[[str], None],
                  env: dict, fetch: Callable[..., bytes], work: Path, which: Callable[[str], Any] = shutil.which) -> str:
    """Docker running on this server: what was there, or the distribution's docker.io (apt), or Docker's own script
    through the Aliyun mirror. Returns what was done (for the notes)."""
    if which("docker"):
        if not docker_ready(run):
            run(["systemctl", "enable", "--now", "docker"], capture_output=True)
            for _ in range(10):
                if docker_ready(run):
                    break
                time.sleep(2)
        if docker_ready(run):
            return "已有 Docker"
        raise SnowlumaError("这台服务器上的 Docker 没有运行（systemctl status docker 看原因）")
    how = ""
    if which("apt-get"):
        code, tail = stream(["apt-get", "update", "-y", "-qq"], on_line, env)
        if code == 0:
            code, tail = stream(["apt-get", "install", "-y", "-qq", "docker.io"], on_line, env)
        how = "装了 Docker（系统软件源的 docker.io）" if code == 0 else ""
    if not how:
        problems = []
        for url in DOCKER_SCRIPTS:
            try:
                script = fetch(url, MAX_SCRIPT)
            except Exception as exc:                   # noqa: BLE001 -- try the next one
                problems.append(f"{url.split('/')[2]}：{exc.__class__.__name__}")
                continue
            if not script.startswith(b"#!") or b"docker" not in script:
                problems.append(f"{url.split('/')[2]}：不是 Docker 的安装脚本")
                continue
            work.mkdir(parents=True, exist_ok=True)
            (work / "get-docker.sh").write_bytes(script)
            code, tail = stream(["sh", str(work / "get-docker.sh"), "--mirror", "Aliyun"], on_line, env)
            if code == 0:
                how = "装了 Docker（get.docker.com，阿里云镜像）"
                break
            problems.append(f"安装脚本失败：{_last(tail, 6)}")
        if not how:
            raise SnowlumaError("安装 Docker 失败：" + "；".join(problems))
    run(["systemctl", "enable", "--now", "docker"], capture_output=True)
    for _ in range(15):
        if docker_ready(run):
            return how
        time.sleep(2)
    raise SnowlumaError("Docker 装好了但没有启动（systemctl status docker 看原因）")


def pull(run: Callable[..., Any], stream: Callable[..., tuple[int, str]], on_line: Callable[[str], None],
         env: dict, image: str = IMAGE) -> str:
    """The image from Docker Hub, else from a mirror (then tagged with its real name). Returns where it came from."""
    problems = []
    for prefix in MIRRORS:
        code, tail = stream(["docker", "pull", prefix + image], on_line, env)
        if code == 0:
            if prefix:
                run(["docker", "tag", prefix + image, image], capture_output=True)
                run(["docker", "rmi", prefix + image], capture_output=True)       # only the extra name
            return (prefix.rstrip("/") or "docker.io")
        problems.append(f"{(prefix.rstrip('/') or 'Docker Hub')}：{_last(tail, 2)}")
    raise SnowlumaError("下载 SnowLuma 镜像失败（Docker Hub 和镜像站都不通）：\n" + "\n".join(problems))


def container_label(run: Callable[..., Any]) -> str | None:
    """None: no container of that name; "" : one that is not Violet's; else Violet's label."""
    done = run(["docker", "inspect", "-f", "{{index .Config.Labels \"%s\"}}" % LABEL_KEY, CONTAINER],
               capture_output=True, text=True)
    if getattr(done, "returncode", 1) != 0:
        return None
    value = (getattr(done, "stdout", "") or "").strip()
    return "" if value in ("", "<no value>") else value


def run_args(base: Path, ws_port: int, vnc_port: int, vnc_password: str, uid: int, gid: int,
             image: str = IMAGE) -> list[str]:
    """`docker run` of the container (the options of SnowLuma's own docker-compose.yml; ptrace and seccomp are
    needed for its hooks). Ports on 127.0.0.1 only; SnowLuma's WebUI, HTTP API and noVNC are not published."""
    return ["docker", "run", "-d", "--name", CONTAINER, "--label", f"{LABEL_KEY}={LABEL_VALUE}",
            "--restart", "unless-stopped", "--shm-size", "1g", "--ulimit", "nofile=65536:1048576",
            "--cap-add", "SYS_PTRACE", "--security-opt", "seccomp=unconfined",
            "-e", f"VNC_PASSWD={vnc_password}", "-e", f"SNOWLUMA_UID={uid}", "-e", f"SNOWLUMA_GID={gid}",
            "-e", "SNOWLUMA_HOOK_AUTOLOAD=1", "-e", "SNOWLUMA_WEBUI_HOST=127.0.0.1",
            "-p", f"127.0.0.1:{ws_port}:3001", "-p", f"127.0.0.1:{vnc_port}:5900",
            "-v", f"{base / 'data'}:/app/data", "-v", f"{base / 'qq-config'}:/app/.config",
            "-v", f"{base / 'qq-data'}:/app/.local/share", image]


def desktop_up(port: int, timeout: float = 3.0) -> bool:
    """The container's VNC answers (the virtual screen with QQ on it is up)."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(12).startswith(b"RFB ")
    except OSError:
        return False


# ------------------------------------------------------------------------------------------------ install

def install(layout: Any, state: dict[str, Any], *, port_free: Callable[[int, str], bool],
            run: Callable[..., Any] = subprocess.run, fetch: Callable[..., bytes] = _fetch,
            stream: Callable[..., tuple[int, str]] = _stream, progress: Progress | None = None,
            wait: float = START_WAIT, rng: random.Random | None = None, which: Callable[[str], Any] = shutil.which,
            check_desktop: Callable[[int], bool] | None = None, napcat_unit: Path = NAPCAT_UNIT) -> dict[str, Any]:
    """Install (or reinstall: keeps ports, tokens and the QQ login) SnowLuma and start it. Updates `state`."""
    base = folder(layout)
    previous = state.get("snowluma") or {}
    notes: list[str] = []
    _say(progress, 2, "检查旧的 NapCat")
    old = remove_napcat(layout, state, run, unit_path=napcat_unit)
    if old:
        notes.append("已卸载以前装的 NapCat：" + "、".join(old))
    _say(progress, 4, "准备账号和目录")
    base.mkdir(parents=True, exist_ok=True)
    _ensure_account(base, run)
    seen = [6]

    def on_line(line: str) -> None:                  # 6 % -> 84 %: one small step per line of apt / docker pull
        text = line.strip()
        if text and seen[0] < 84 and any(k in text for k in ("Pull complete", "Download complete", "Get:", "Unpacking",
                                                              "Setting up", "Pulling", "Digest", "Status")):
            seen[0] += 1
            _say(progress, seen[0], "下载：" + text[:90])
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "LANG": "C.UTF-8"}
    _say(progress, 6, "检查 Docker")
    docker = ensure_docker(run, stream, on_line, env, fetch, base / "install-work", which)
    shutil.rmtree(base / "install-work", ignore_errors=True)
    if docker != "已有 Docker":
        notes.append(docker)
    label = container_label(run)
    if label == "":
        raise SnowlumaError(f"这台服务器上已经有一个不是 Violet 建的容器 {CONTAINER}，没有改动")
    _say(progress, max(seen[0], 20), "下载 SnowLuma 镜像（含 Linux QQ，约 740 MB）")
    source = pull(run, stream, on_line, env)

    _say(progress, 86, "写配置（只监听本机）")
    taken: set[int] = set()
    ws_port = int(previous.get("ws_port") or 0) or _pick_port(DEFAULT_WS_PORT, port_free, taken, rng)
    taken.add(ws_port)
    vnc_port = int(previous.get("vnc_port") or 0) or _pick_port(DEFAULT_VNC_PORT, port_free, taken, rng)
    ws_token = previous.get("ws_token") or secrets.token_urlsafe(24)
    vnc_password = previous.get("vnc_password") or secrets.token_urlsafe(9)[:8]     # VNC uses 8 characters
    uid, account_gid = _ids(ACCOUNT)
    _, violet_gid = _ids(state.get("user"))
    gid = violet_gid if violet_gid is not None else account_gid
    config = base / "data" / "config"
    for name in ("onebot.json", *sorted(p.name for p in config.glob("onebot_*.json"))):
        path = config / name
        try:
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except (OSError, ValueError):
            current = None
        _write_json(path, onebot_config(ws_token, current if isinstance(current, dict) else None), uid, gid, 0o640)
    for sub, mode in (("data", 0o750), ("qq-config", 0o700), ("qq-data", 0o700)):
        (base / sub).mkdir(parents=True, exist_ok=True)
        if uid is not None:
            run(["chown", "-R", f"{uid}:{gid}", str(base / sub)], capture_output=True)
        os.chmod(base / sub, mode)
    if gid is not None:
        run(["chown", f"0:{gid}", str(base)], capture_output=True)
    os.chmod(base, 0o750)

    _say(progress, 90, "启动 QQ")
    if label is not None:
        run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    done = run(run_args(base, ws_port, vnc_port, vnc_password, uid if uid is not None else 1000,
                        gid if gid is not None else 1000), capture_output=True, text=True)
    if getattr(done, "returncode", 0) != 0:
        raise SnowlumaError("QQ 容器启动失败：" + _last(getattr(done, "stderr", "") or "", 6) +
                            f"\n查看原因：docker logs {CONTAINER}")
    _say(progress, 94, "等待 QQ 窗口")
    check = check_desktop or desktop_up
    deadline = time.monotonic() + wait
    ok = check(vnc_port)
    while not ok and time.monotonic() < deadline:
        time.sleep(3)
        ok = check(vnc_port)
    if not ok:
        notes.append(f"QQ {wait:.0f} 秒内还没有启动完，稍后在渠道页刷新；原因可以看 docker logs {CONTAINER}")
    state["snowluma"] = {"managed": True, "ws_port": ws_port, "vnc_port": vnc_port, "ws_token": ws_token,
                         "vnc_password": vnc_password, "image": IMAGE, "source": source,
                         "installed_at": int(time.time())}
    _say(progress, 98, "交给 Violet")
    export(layout, state)
    notes.append("在后台“聊天渠道 → QQ”里用手机 QQ 扫 QQ 窗口里的二维码（建议用小号）")
    return {"ok": True, "installed": True, "started": ok, "ws_port": ws_port, "notes": notes}


def export(layout: Any, state: dict[str, Any]) -> None:
    info = state.get("snowluma") or {}
    value = {"installed": True, "ws_url": f"ws://127.0.0.1:{info['ws_port']}", "ws_token": info["ws_token"],
             "vnc_port": int(info["vnc_port"]), "vnc_password": info["vnc_password"],
             "config_dir": str(folder(layout) / "data" / "config"), "image": info.get("image"),
             "installed_at": info.get("installed_at")}
    uid, gid = _ids(state.get("user"))
    _write_json(export_path(layout.data), value, uid, gid, 0o640)


def restart(state: dict[str, Any], run: Callable[..., Any] = subprocess.run) -> None:
    if not (state.get("snowluma") or {}).get("managed"):
        raise SnowlumaError("还没有安装 QQ（SnowLuma）")
    done = run(["docker", "restart", CONTAINER], capture_output=True, text=True)
    if getattr(done, "returncode", 0) != 0:
        raise SnowlumaError("重启 QQ 失败：" + _last(getattr(done, "stderr", "") or "", 4))


def uninstall(layout: Any, state: dict[str, Any], *, run: Callable[..., Any] = subprocess.run,
              which: Callable[[str], Any] = shutil.which, napcat_unit: Path = NAPCAT_UNIT) -> list[str]:
    """Only what this module (or the NapCat module before it) installed. Updates `state`."""
    removed = remove_napcat(layout, state, run, unit_path=napcat_unit)
    if which("docker"):
        label = container_label(run)
        if label == LABEL_VALUE:
            run(["docker", "rm", "-f", CONTAINER], capture_output=True)
            removed.append(f"容器 {CONTAINER}")
        if label is None or label == LABEL_VALUE:
            image = (state.get("snowluma") or {}).get("image") or IMAGE
            if run(["docker", "rmi", image], capture_output=True).returncode == 0:
                removed.append(f"镜像 {image}")
    base = folder(layout)
    if base.is_dir() and not base.is_symlink():
        shutil.rmtree(base, ignore_errors=True)
        removed.append(str(base))
    if _home_of(ACCOUNT, run) == base and run(["userdel", ACCOUNT], capture_output=True).returncode == 0:
        removed.append(f"系统账号 {ACCOUNT}")
    state.pop("snowluma", None)
    export_path(layout.data).unlink(missing_ok=True)
    return removed


def status(state: dict[str, Any], run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    info = state.get("snowluma") or {}
    if not info:
        return {"installed": False}
    done = run(["docker", "inspect", "-f", "{{.State.Status}}", CONTAINER], capture_output=True, text=True)
    return {"installed": True, "ws_port": info.get("ws_port"), "vnc_port": info.get("vnc_port"),
            "container": (getattr(done, "stdout", "") or "").strip() or "missing",
            "desktop": desktop_up(int(info.get("vnc_port") or 0)), "image": info.get("image"),
            "installed_at": info.get("installed_at")}
