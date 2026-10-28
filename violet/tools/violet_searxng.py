"""`violet searxng install | uninstall | status` -- a private SearXNG for Violet's web search, on the Violet server
(Violet 0.7.1; the Control Center's "一键安装联网搜索" runs it through the root task service). Standard library only;
run as root.

No Docker (Docker Hub is unreachable from many servers in mainland China, and Docker would be one more big thing to
install and remove): SearXNG's own source from GitHub, in its own virtualenv, as a systemd service:

  <root>/searxng/src        the SearXNG source (github.com/searxng/searxng, the current main line), its sha256 recorded
  <root>/searxng/venv       its own Python environment (made with Violet's interpreter; packages from the same pip
                            source as Violet's own -- PIP_INDEX_URL and pip.conf are respected)
  <root>/searxng/settings.yml   Violet's settings (JSON on, limiter off, bound to 127.0.0.1 only)
  <root>/searxng/secret.env     the server secret, 0600 root (systemd reads it before dropping privileges)
  violet-searxng.service    DynamicUser, read-only system, only 127.0.0.1:<port> (18089, else a random free one);
                            nothing is opened in any firewall
  <data>/control/searxng.json   what Violet needs (the URL), owned by Violet's account; the page then switches
                            web search on with that URL

uninstall removes all of it (unit, folders, cache, the export); `uninstall --purge` of Violet calls it.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
import shutil
import subprocess
import tarfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

UNIT_NAME = "violet-searxng"
UNIT_PATH = Path("/etc/systemd/system") / f"{UNIT_NAME}.service"
CACHE_DIRS = (Path("/var/cache/violet-searxng"), Path("/var/cache/private/violet-searxng"))
SOURCE_URL = "https://github.com/searxng/searxng/archive/refs/heads/master.tar.gz"
MAX_SOURCE = 64 * 1024 * 1024
DEFAULT_PORT = 18089
RANDOM_PORTS = range(20000, 30000)
MARK = "# managed by Violet (tools/violet_searxng.py)"
HEALTH_WAIT = 90.0
Progress = Callable[[int, str], None]


class SearxngError(RuntimeError):
    pass


def folder(layout: Any) -> Path:
    return layout.root / "searxng"


def export_path(data: Path) -> Path:
    return data / "control" / "searxng.json"


def _say(progress: Progress | None, percent: int, step: str) -> None:
    if progress is not None:
        progress(percent, step)


def _fetch(url: str, limit: int, report: Callable[[int, int], None] | None = None, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "violet-installer"})
    chunks, got = [], 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 -- a fixed https GitHub URL
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            got += len(chunk)
            if got > limit:
                raise SearxngError("下载的文件太大")
            chunks.append(chunk)
            if report is not None:
                report(got, total)
    return b"".join(chunks)


def _stream(cmd: list[str], on_line: Callable[[str], None], env: dict | None = None) -> tuple[int, str]:
    """Run cmd, calling on_line for every output line; (exit code, the last lines)."""
    tail: list[str] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", env=env)
    assert proc.stdout is not None
    for line in proc.stdout:
        tail = (tail + [line.rstrip()])[-30:]
        on_line(line)
    return proc.wait(), "\n".join(tail)


def safe_extract(archive: Path, target: Path) -> Path:
    """Unpack a GitHub source archive: regular files and folders only, nothing outside target. Returns the single
    top folder."""
    target.mkdir(parents=True, exist_ok=True)
    base = target.resolve()
    tops = set()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            dest = (base / name).resolve()
            if name.startswith("/") or ".." in Path(name).parts or (dest != base and base not in dest.parents):
                raise SearxngError(f"源码包里有不安全的路径：{name}")
            if not (member.isfile() or member.isdir()):
                continue                                    # links, devices: never
            tops.add(Path(name).parts[0])
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                continue
            with src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(dest, 0o755 if member.mode & 0o111 else 0o644)
    if len(tops) != 1:
        raise SearxngError("源码包的结构不对")
    return base / tops.pop()


# 0.7.2 (user's server in mainland China: every default engine -- google, duckduckgo, brave, wikipedia, wikidata --
# was unresponsive, 0 results): engines that answer from there. Chosen automatically when the defaults find nothing.
CN_ENGINES = ("bing", "baidu", "sogou", "360search")
ENGINE_SETS = {"default": "SearXNG 默认的搜索引擎", "cn": "国内能访问的搜索引擎（必应、百度、搜狗、360）"}


def settings_text(port: int, engines: str = "default") -> str:
    """Violet's SearXNG settings (deploy/searxng/config/settings.yml, bound to loopback). The secret is not here: it
    comes from SEARXNG_SECRET (secret.env). engines: "default" (SearXNG's own) or "cn" (CN_ENGINES only, switched
    on -- some of them are off in SearXNG's defaults)."""
    if engines == "cn":
        head = ["use_default_settings:", "  engines:", "    keep_only:", *[f"      - {e}" for e in CN_ENGINES]]
        tail = ["engines:", *[line for e in CN_ENGINES for line in (f"  - name: {e}", "    disabled: false")], ""]
    else:
        head, tail = ["use_default_settings: true"], []
    return "\n".join([
        "# Violet's private SearXNG (tools/violet_searxng.py). Rewritten on reinstall.", *head, "",
        "general:", '  instance_name: "violet-searxng"', "  enable_metrics: false", "",
        "server:", f"  port: {port}", '  bind_address: "127.0.0.1"',
        '  secret_key: "set-by-SEARXNG_SECRET-environment-variable"', "  limiter: false", "  public_instance: false",
        "  image_proxy: false", '  method: "GET"', "",
        "search:", "  safe_search: 1", '  autocomplete: ""', '  default_lang: "zh-CN"', "  formats:", "    - html",
        "    - json", "",
        "outgoing:", "  request_timeout: 4.0", "  max_request_timeout: 8.0", "", *tail])


def unit_text(base: Path, port: int, granian: bool) -> str:
    venv = base / "venv"
    start = (f"{venv / 'bin' / 'granian'} --interface wsgi --host 127.0.0.1 --port {port} --workers 1 searx.webapp:app"
             if granian else f"{venv / 'bin' / 'python'} -m searx.webapp")
    return "\n".join([
        "[Unit]", "Description=Violet's private SearXNG (web search, 127.0.0.1 only)", MARK,
        "After=network-online.target", "Wants=network-online.target", "",
        "[Service]", f"ExecStart={start}", f"WorkingDirectory={base / 'src'}",
        f"Environment=SEARXNG_SETTINGS_PATH={base / 'settings.yml'}", f"EnvironmentFile={base / 'secret.env'}",
        f"Environment=SEARXNG_PORT={port}", "Environment=SEARXNG_BIND_ADDRESS=127.0.0.1",
        "Environment=PYTHONDONTWRITEBYTECODE=1", f"Environment=XDG_CACHE_HOME={CACHE_DIRS[0]}",
        "Restart=always", "RestartSec=5", "DynamicUser=yes", f"CacheDirectory={UNIT_NAME}", "NoNewPrivileges=yes",
        "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes", "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes", "ProtectControlGroups=yes", "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "LockPersonality=yes", "MemoryMax=768M", "", "[Install]", "WantedBy=multi-user.target", ""])


def health(port: int, timeout: float = 3.0) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as resp:
            return resp.status == 200
    except OSError:
        return False


def test_search(port: int, timeout: float = 25.0, query: str = "天气") -> dict[str, Any]:
    """One JSON search (SearXNG asks its engines on the internet): {answered, results, dead, note}."""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = f"http://127.0.0.1:{port}/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
        with opener.open(url, timeout=timeout) as resp:
            data = json.loads(resp.read(2_000_000).decode("utf-8"))
        results = len(data.get("results") or [])
        dead = [str(e[0]) for e in data.get("unresponsive_engines") or [] if e]
        note = f"测试搜索返回 {results} 条结果" + (f"；没有响应的搜索引擎：{'、'.join(dead[:8])}" if dead else "")
        return {"answered": True, "results": results, "dead": dead, "note": note}
    except (OSError, ValueError) as exc:
        return {"answered": False, "results": 0, "dead": [],
                "note": f"测试搜索没有成功（{exc.__class__.__name__}）"}


def _apply_engines(base: Path, port: int, engines: str, run: Callable[..., Any], wait: float,
                   check_health: Callable[[int], bool]) -> bool:
    """Write the settings for that engine set, restart, wait for health. False when it did not come up."""
    (base / "settings.yml").write_text(settings_text(port, engines), encoding="utf-8")
    os.chmod(base / "settings.yml", 0o644)
    run(["systemctl", "restart", f"{UNIT_NAME}.service"], capture_output=True, text=True)
    deadline = time.monotonic() + wait
    ok = check_health(port)
    while not ok and time.monotonic() < deadline:
        time.sleep(2)
        ok = check_health(port)
    return ok


def choose_engines(base: Path, port: int, current: str, *, run: Callable[..., Any], wait: float,
                   check_health: Callable[[int], bool], search: Callable[[int], dict[str, Any]],
                   progress: Progress | None = None, first: dict[str, Any] | None = None) -> tuple[str, dict, list[str]]:
    """Keep the engine set that finds something: test the current one (or use `first`), switch to the other one when
    it finds nothing, and go back when the other one finds nothing either or does not start. (set, last test, notes)."""
    notes: list[str] = []
    result = first or search(port)
    if result["results"] > 0:
        return current, result, notes
    other = "cn" if current == "default" else "default"
    _say(progress, 96, f"{ENGINE_SETS[current]}没有结果，改用{ENGINE_SETS[other]}再试")
    if not _apply_engines(base, port, other, run, wait, check_health):
        _apply_engines(base, port, current, run, wait, check_health)
        notes.append(f"换成{ENGINE_SETS[other]}后 SearXNG 没有启动，已换回原来的设置")
        return current, result, notes
    retry = search(port)
    if retry["results"] > 0 or result["results"] == 0 and not result["answered"]:
        notes.append(f"{ENGINE_SETS[current]}没有结果，已改用{ENGINE_SETS[other]}")
        return other, retry, notes
    _apply_engines(base, port, current, run, wait, check_health)
    notes.append("两组搜索引擎都没有返回结果：服务器可能访问不到外面的网站（检查网络或代理）")
    return current, result, notes


def _pick_port(port_free: Callable[[int, str], bool], rng: random.Random | None) -> int:
    if port_free(DEFAULT_PORT, "127.0.0.1"):
        return DEFAULT_PORT
    rng = rng or random.SystemRandom()
    for _ in range(100):
        port = rng.randrange(RANDOM_PORTS.start, RANDOM_PORTS.stop)
        if port_free(port, "127.0.0.1"):
            return port
    raise SearxngError("找不到空闲端口")


def _write_owned(path: Path, text: str, user: str | None, mode: int = 0o640) -> None:
    """root writes into the service account's folder: a fresh file (O_EXCL), renamed into place, owned by the account."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        if user and hasattr(os, "fchown"):
            try:
                import pwd
                account = pwd.getpwnam(user)
                os.fchown(fh.fileno(), account.pw_uid, account.pw_gid)
            except (ImportError, KeyError, OSError):
                pass
        fh.write(text)
    os.replace(tmp, path)


# ------------------------------------------------------------------------------------------------ commands

def install(layout: Any, state: dict[str, Any], *, python: Path, port_free: Callable[[int, str], bool],
            run: Callable[..., Any] = subprocess.run, fetch: Callable[..., bytes] = _fetch,
            stream: Callable[..., tuple[int, str]] = _stream, progress: Progress | None = None,
            unit_path: Path = UNIT_PATH, wait: float = HEALTH_WAIT, rng: random.Random | None = None,
            check_health: Callable[[int], bool] = health,
            search: Callable[[int], dict[str, Any]] = test_search) -> dict[str, Any]:
    """Returns the result; updates `state` (install.json) -- the caller saves it. A reinstall replaces the program and
    keeps the port and secret."""
    if not shutil.which("systemctl") and run is subprocess.run:
        raise SearxngError("这台服务器没有 systemd，联网搜索需要手动部署（见 deploy/searxng/README.md）")
    if unit_path.exists() and MARK not in unit_path.read_text(encoding="utf-8", errors="replace"):
        raise SearxngError("这台服务器上已经有一个不是 Violet 装的 violet-searxng 服务，没有改动")
    base = folder(layout)
    notes: list[str] = []
    previous = state.get("searxng") or {}
    _say(progress, 3, "选择端口")
    port = int(previous.get("port") or 0) or _pick_port(port_free, rng)
    if unit_path.exists():
        run(["systemctl", "stop", f"{UNIT_NAME}.service"], capture_output=True)    # a reinstall: free its port
    _say(progress, 5, "下载 SearXNG 源码（GitHub）")
    last = [-1]

    def report(got: int, total: int) -> None:          # 5 % -> 30 %
        pct = 5 + int(25 * got / total) if total else min(29, 5 + got // (1024 * 1024))
        if progress is not None and pct >= last[0] + 2:
            last[0] = pct
            progress(pct, f"下载 SearXNG 源码：{got / 1048576:.1f}" + (f" / {total / 1048576:.1f}" if total else "") + " MB")
    data = fetch(SOURCE_URL, MAX_SOURCE, report=report)
    digest = hashlib.sha256(data).hexdigest()
    _say(progress, 31, "解压")
    work = base / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    archive = work / "searxng.tar.gz"
    archive.write_bytes(data)
    top = safe_extract(archive, work / "unpacked")
    if not (top / "searx" / "webapp.py").is_file() or not (top / "requirements.txt").is_file():
        raise SearxngError("下载的不是 SearXNG 的源码")
    src = base / "src"
    shutil.rmtree(src, ignore_errors=True)
    os.replace(top, src)
    shutil.rmtree(work, ignore_errors=True)

    _say(progress, 34, "创建 Python 环境")
    venv = base / "venv"
    shutil.rmtree(venv, ignore_errors=True)
    done = run([str(python), "-m", "venv", str(venv)], capture_output=True, text=True)
    if getattr(done, "returncode", 0) != 0:
        raise SearxngError("创建 Python 环境失败：" + (getattr(done, "stderr", "") or "")[-400:])
    pip = [str(venv / "bin" / "python"), "-m", "pip", "install", "--disable-pip-version-check"]
    seen = [38]

    def on_line(line: str) -> None:                  # 38 % -> 84 %: one step per package pip reports
        if line.startswith(("Collecting", "Downloading", "Installing", "Building", "Successfully")) and seen[0] < 84:
            seen[0] += 1
            if progress is not None:
                progress(seen[0], "安装依赖：" + line.strip()[:90])
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for step_cmd, what in ((pip + ["-U", "pip", "setuptools", "wheel", "pyyaml", "msgspec", "typing_extensions"], "基础工具"),
                           (pip + ["-r", str(src / "requirements.txt")], "SearXNG 的依赖"),
                           (pip + ["--use-pep517", "--no-build-isolation", "-e", str(src)], "SearXNG"),
                           (pip + ["granian"], "Web 服务器（granian）")):
        _say(progress, seen[0], f"安装{what}")
        code, tail = stream(step_cmd, on_line, env)
        if code != 0:
            if what.startswith("Web"):                # optional: SearXNG's own server is used instead
                notes.append("granian 没有装上，改用 SearXNG 自带的服务器")
                continue
            raise SearxngError(f"安装{what}失败（检查网络 / pip 源）：\n" + tail[-1200:])
    granian = (venv / "bin" / "granian").exists()

    _say(progress, 86, "写配置、注册服务")
    engines = previous.get("engines") if previous.get("engines") in ENGINE_SETS else "default"
    (base / "settings.yml").write_text(settings_text(port, engines), encoding="utf-8")
    os.chmod(base / "settings.yml", 0o644)
    secret_file = base / "secret.env"
    secret = previous.get("secret_kept") and secret_file.exists()
    if not secret:
        fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"SEARXNG_SECRET={secrets.token_hex(32)}\n")
    unit_path.write_text(unit_text(base, port, granian), encoding="utf-8")
    run(["systemctl", "daemon-reload"], capture_output=True)
    done = run(["systemctl", "enable", f"{UNIT_NAME}.service"], capture_output=True, text=True)
    done = run(["systemctl", "restart", f"{UNIT_NAME}.service"], capture_output=True, text=True)
    if getattr(done, "returncode", 0) != 0:
        raise SearxngError("SearXNG 服务启动失败：" + (getattr(done, "stderr", "") or "")[-400:] +
                           f"\n查看原因：journalctl -u {UNIT_NAME} -n 40")
    _say(progress, 90, "等待 SearXNG 启动")
    deadline = time.monotonic() + wait
    ok = check_health(port)
    while not ok and time.monotonic() < deadline:
        time.sleep(2)
        ok = check_health(port)
    if not ok:
        raise SearxngError(f"SearXNG {wait:.0f} 秒内没有启动：journalctl -u {UNIT_NAME} -n 40")
    _say(progress, 95, "测试一次搜索")
    engines, tested, more = choose_engines(base, port, engines, run=run, wait=wait, check_health=check_health,
                                           search=search, progress=progress)
    notes += [tested["note"], *more]
    url = f"http://127.0.0.1:{port}"
    state["searxng"] = {"managed": True, "port": port, "installed_at": int(time.time()), "source_sha256": digest,
                        "server": "granian" if granian else "searx.webapp", "secret_kept": True, "engines": engines}
    _say(progress, 98, "交给 Violet")
    _export(layout, state, tested)
    return {"ok": True, "installed": True, "url": url, "port": port, "healthy": True,
            "search_ok": tested["results"] > 0, "engines": engines, "notes": notes}


def _export(layout: Any, state: dict[str, Any], tested: dict[str, Any]) -> None:
    info = state["searxng"]
    export = {"installed": True, "url": f"http://127.0.0.1:{info['port']}", "port": info["port"],
              "installed_at": info["installed_at"], "engines": info.get("engines", "default"),
              "engines_text": ENGINE_SETS.get(info.get("engines", "default")),
              "search_ok": tested["results"] > 0, "last_results": tested["results"]}
    _write_owned(export_path(layout.data), json.dumps(export, ensure_ascii=False), state.get("user"))


def tune(layout: Any, state: dict[str, Any], *, run: Callable[..., Any] = subprocess.run,
         progress: Progress | None = None, unit_path: Path = UNIT_PATH, wait: float = HEALTH_WAIT,
         check_health: Callable[[int], bool] = health,
         search: Callable[[int], dict[str, Any]] = test_search) -> dict[str, Any]:
    """0.7.2: pick the engine set again on an installed SearXNG (for installs from 0.7.1, which never switched)."""
    info = state.get("searxng") or {}
    if not unit_path.exists() or not info.get("managed"):
        raise SearxngError("还没有安装联网搜索（SearXNG）")
    port = int(info["port"])
    current = info.get("engines") if info.get("engines") in ENGINE_SETS else "default"
    _say(progress, 10, f"用{ENGINE_SETS[current]}测试一次搜索")
    if not check_health(port):
        _apply_engines(folder(layout), port, current, run, wait, check_health)
    engines, tested, notes = choose_engines(folder(layout), port, current, run=run, wait=wait,
                                            check_health=check_health, search=search, progress=progress)
    info["engines"] = engines
    _say(progress, 98, "交给 Violet")
    _export(layout, state, tested)
    return {"ok": True, "url": f"http://127.0.0.1:{port}", "engines": engines, "search_ok": tested["results"] > 0,
            "notes": [tested["note"], *notes]}


def uninstall(layout: Any, state: dict[str, Any], *, run: Callable[..., Any] = subprocess.run,
              unit_path: Path = UNIT_PATH, cache_dirs: tuple[Path, ...] = CACHE_DIRS) -> list[str]:
    """Only what this module installed (marked unit). Returns what was removed; updates `state`."""
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
    for cache in cache_dirs:
        if cache.is_symlink():
            cache.unlink(missing_ok=True)
        elif cache.exists():
            shutil.rmtree(cache, ignore_errors=True)
    state.pop("searxng", None)
    export_path(layout.data).unlink(missing_ok=True)
    return removed


def status(state: dict[str, Any], unit_path: Path = UNIT_PATH) -> dict[str, Any]:
    info = state.get("searxng") or {}
    if not unit_path.exists() or not info:
        return {"installed": False}
    port = int(info.get("port") or 0)
    return {"installed": True, "url": f"http://127.0.0.1:{port}", "port": port, "healthy": health(port),
            "server": info.get("server"), "installed_at": info.get("installed_at")}
