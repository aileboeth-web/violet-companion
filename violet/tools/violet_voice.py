"""`violet voice install | uninstall | status` -- her voice (2026-10-31, user: 让她用大肥鱼的声音说话; optional).

CosyVoice (FunAudioLLM, Apache-2.0) on this server's CPU: a short reference recording uploaded on the page is cloned
(zero-shot), every sentence takes about a minute on 4 cores. Standard library only; run as root.

What a server needs (checked first, the page shows the same list):
  * Linux x86_64 with systemd, a CPU with AVX2
  * 8 GB memory or more (the model uses about 4 GB while speaking)
  * 12 GB free disk (Python packages ~3 GB, the model ~5 GB, room to unpack)
  * Debian / Ubuntu (apt) for ffmpeg, gcc and the Python headers (pilk and pyworld are compiled), or those installed

What it puts where:
  <root>/voice/src            CosyVoice's source at a pinned commit, Matcha-TTS (its submodule) inside
  <root>/voice/venv           its own Python environment (Violet's interpreter; PyTorch CPU from the Aliyun mirror,
                              the rest from PIP_INDEX_URL / pip.conf, else the Tsinghua mirror, else PyPI)
  <root>/voice/models/<name>  the model from ModelScope (only the files needed on a CPU)
  <root>/voice/cache          caches the service writes (ModelScope, matplotlib), owned by Violet's account
  violet-voice.service        Violet's account, 127.0.0.1:<port> only, Nice=10 so the chat stays responsive
  <data>/voice                the reference recording + its text (uploaded on the page; kept on uninstall)
  <data>/control/voice.json   what Violet needs (the URL)
"""

from __future__ import annotations

import json
import os
import platform
import random
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

UNIT_NAME = "violet-voice"
UNIT_PATH = Path("/etc/systemd/system") / f"{UNIT_NAME}.service"
MARK = "# managed by Violet (tools/violet_voice.py)"
DEFAULT_PORT = 18095
RANDOM_PORTS = range(20000, 30000)
COSYVOICE_SHA = "074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc"
MATCHA_SHA = "dd9105b34bf2be2230f4aa1e4769fb586a3c824e"
SOURCES = {"src": f"https://github.com/FunAudioLLM/CosyVoice/archive/{COSYVOICE_SHA}.tar.gz",
           "matcha": f"https://github.com/shivammehta25/Matcha-TTS/archive/{MATCHA_SHA}.tar.gz"}
MODEL_ID = "FunAudioLLM/Fun-CosyVoice3-0.5B-2512"
MODEL_DIR = "Fun-CosyVoice3-0.5B"
MODEL_SKIP = ["llm.rl.pt", "flow.decoder.estimator.fp32.onnx", "speech_tokenizer_v3.batch.onnx"]
MODEL_BYTES = 5_200_000_000                           # roughly, for the progress bar
TORCH_LINKS = "https://mirrors.aliyun.com/pytorch-wheels/cpu/"
TSINGHUA = "https://pypi.tuna.tsinghua.edu.cn/simple"
# what speaking needs (CosyVoice's requirements.txt without training / GPU / web UI; tested 2026-10-31 on Python 3.11)
PACKAGES = ["conformer==0.3.2", "diffusers==0.29.0", "hydra-core==1.3.2", "HyperPyYAML==1.2.3", "inflect==7.3.1",
            "librosa==0.10.2", "lightning==2.2.4", "modelscope==1.20.0", "numpy==1.26.4", "omegaconf==2.3.0",
            "onnx==1.16.0", "onnxruntime==1.18.0", "protobuf==4.25", "pydantic==2.7.0", "rich==13.7.1",
            "soundfile==0.12.1", "transformers==4.51.3", "x-transformers==2.11.24", "wetext==0.0.4",
            "pyarrow==18.1.0", "matplotlib==3.7.5", "gdown", "wget", "pyworld", "einops", "regex", "tiktoken",
            "scipy", "tqdm", "pilk"]
MIN_MEMORY_GB = 7.5
MIN_DISK_GB = 12.0
HEALTH_WAIT = 300.0
REQUIREMENTS = ["Linux x86_64 + systemd", "CPU 支持 AVX2", "内存 8 GB 以上（说话时约占 4 GB）",
                "硬盘空闲 12 GB 以上（依赖约 3 GB，模型约 5 GB）", "Debian / Ubuntu（自动装 ffmpeg 和编译工具 build-essential、python3-dev）",
                "没有显卡也行：4 核 CPU 一句话大约 1 分钟"]
Progress = Callable[[int, str], None]


class VoiceError(RuntimeError):
    pass


def folder(layout: Any) -> Path:
    return layout.root / "voice"


def export_path(data: Path) -> Path:
    return data / "control" / "voice.json"


def reference_dir(data: Path) -> Path:
    return data / "voice"


def _say(progress: Progress | None, percent: int, step: str) -> None:
    if progress is not None:
        progress(percent, step)


# ------------------------------------------------------------------------------------------------ checks

def _meminfo_gb(text: str) -> float:
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) / 1024 / 1024
    return 0.0


def check(root: Path, *, cpuinfo: str | None = None, meminfo: str | None = None, machine: str | None = None,
          disk_free: float | None = None) -> list[str]:
    """What is missing on this server ([] = fine)."""
    problems = []
    machine = machine or platform.machine()
    if machine not in ("x86_64", "AMD64"):
        problems.append(f"需要 x86_64 的 CPU（这台是 {machine}）")
    if cpuinfo is None:
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
        except OSError:
            cpuinfo = ""
    if " avx2" not in cpuinfo and "\tavx2" not in cpuinfo and "avx2 " not in cpuinfo:
        problems.append("CPU 不支持 AVX2（没有它 PyTorch 会慢到不能用）")
    if meminfo is None:
        try:
            meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
        except OSError:
            meminfo = ""
    memory = _meminfo_gb(meminfo)
    if memory and memory < MIN_MEMORY_GB:
        problems.append(f"内存只有 {memory:.1f} GB，至少要 8 GB")
    if disk_free is None:
        probe = root if root.exists() else root.parent
        disk_free = shutil.disk_usage(probe).free / 1024 ** 3
    if disk_free < MIN_DISK_GB:
        problems.append(f"硬盘只剩 {disk_free:.1f} GB，至少要 {MIN_DISK_GB:.0f} GB")
    return problems


# ------------------------------------------------------------------------------------------------ helpers

def _fetch(url: str, target: Path, timeout: float = 120.0) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "violet-installer"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(target, "wb") as fh:   # noqa: S310 -- fixed URLs
        shutil.copyfileobj(resp, fh, 1 << 20)


def _stream(cmd: list[str], on_line: Callable[[str], None], env: dict | None = None) -> tuple[int, str]:
    tail: list[str] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", env=env)
    assert proc.stdout is not None
    for line in proc.stdout:
        tail = (tail + [line.rstrip()])[-30:]
        on_line(line)
    return proc.wait(), "\n".join(tail)


def _unpack(archive: Path, target: Path) -> None:
    """A GitHub source archive into target (its single top folder's contents); nothing outside target."""
    import tarfile
    shutil.rmtree(target, ignore_errors=True)
    work = target.with_name(target.name + ".unpack")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        base = work.resolve()
        for member in tar.getmembers():
            dest = (work / member.name).resolve()
            if not str(dest).startswith(str(base)) or not (member.isfile() or member.isdir()):
                continue
            tar.extract(member, work)
    tops = [p for p in work.iterdir() if p.is_dir()]
    if len(tops) != 1:
        raise VoiceError("下载的源码格式不对")
    os.replace(tops[0], target)
    shutil.rmtree(work, ignore_errors=True)


def _chown_tree(path: Path, user: str | None) -> None:
    if not user or not hasattr(os, "chown"):
        return
    try:
        import pwd
        account = pwd.getpwnam(user)
    except (ImportError, KeyError):
        return
    for p in [path, *path.rglob("*")]:
        try:
            os.chown(p, account.pw_uid, account.pw_gid, follow_symlinks=False)
        except OSError:
            pass


def _dir_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def health(port: int, timeout: float = 3.0) -> dict[str, Any] | None:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def unit_text(base: Path, app: Path, data: Path, port: int, user: str, threads: int) -> str:
    venv = base / "venv"
    cache = base / "cache"
    return "\n".join([
        "[Unit]", "Description=Violet's voice (CosyVoice, 127.0.0.1 only)", MARK, "After=network.target", "",
        "[Service]",
        f"ExecStart={venv / 'bin' / 'python'} {app / 'tools' / 'voice_server.py'} --src {base / 'src'} "
        f"--model {base / 'models' / MODEL_DIR} --ref {reference_dir(data)} --port {port} --threads {threads}",
        f"WorkingDirectory={base}", f"User={user}", f"Environment=MODELSCOPE_CACHE={cache / 'modelscope'}",
        f"Environment=MPLCONFIGDIR={cache / 'matplotlib'}", f"Environment=HF_HOME={cache / 'hf'}",
        f"Environment=OMP_NUM_THREADS={threads}", "Environment=PYTHONDONTWRITEBYTECODE=1",
        "Restart=on-failure", "RestartSec=10", "Nice=10", "MemoryMax=8G", "NoNewPrivileges=yes",
        "ProtectSystem=full", "PrivateTmp=yes", "", "[Install]", "WantedBy=multi-user.target", ""])


def _pick_port(port_free: Callable[[int, str], bool], rng: random.Random | None = None) -> int:
    if port_free(DEFAULT_PORT, "127.0.0.1"):
        return DEFAULT_PORT
    rng = rng or random.SystemRandom()
    for _ in range(100):
        port = rng.randrange(RANDOM_PORTS.start, RANDOM_PORTS.stop)
        if port_free(port, "127.0.0.1"):
            return port
    raise VoiceError("找不到空闲端口")


def _pip_sources() -> list[list[str]]:
    """Index arguments to try in order: the admin's own source if set, else Tsinghua, then PyPI."""
    if os.environ.get("PIP_INDEX_URL") or any(Path(p).is_file() for p in ("/etc/pip.conf", "/root/.pip/pip.conf",
                                                                           "/root/.config/pip/pip.conf")):
        return [[]]
    return [["-i", TSINGHUA], []]


# ------------------------------------------------------------------------------------------------ commands

def install(layout: Any, state: dict[str, Any], *, python: Path, app: Path, port_free: Callable[[int, str], bool],
            run: Callable[..., Any] = subprocess.run, stream: Callable[..., tuple[int, str]] = _stream,
            fetch: Callable[[str, Path], None] = _fetch, progress: Progress | None = None,
            unit_path: Path = UNIT_PATH, wait: float = HEALTH_WAIT,
            check_health: Callable[[int], dict[str, Any] | None] = health, checks: Callable[[Path], list[str]] = check,
            ) -> dict[str, Any]:
    if not shutil.which("systemctl") and run is subprocess.run:
        raise VoiceError("这台服务器没有 systemd，语音要手动部署")
    if unit_path.exists() and MARK not in unit_path.read_text(encoding="utf-8", errors="replace"):
        raise VoiceError("这台服务器上已经有一个不是 Violet 装的 violet-voice 服务，没有改动")
    _say(progress, 2, "检查服务器条件")
    problems = checks(layout.root)
    if problems:
        raise VoiceError("这台服务器装不了语音：" + "；".join(problems))
    user = state.get("user") or "violet"
    base = folder(layout)
    base.mkdir(parents=True, exist_ok=True)
    previous = state.get("voice") or {}
    port = int(previous.get("port") or 0) or _pick_port(port_free)
    if unit_path.exists():
        run(["systemctl", "stop", f"{UNIT_NAME}.service"], capture_output=True)

    # 2026-10-31 (user's Debian 12): pilk and pyworld have no wheels for Linux / Python 3.11 -- they are compiled, so
    # a C compiler and the Python headers are needed besides ffmpeg
    wanted = [pkg for pkg, present in (("ffmpeg", shutil.which("ffmpeg")), ("build-essential", shutil.which("gcc")),
                                       ("python3-dev", Path(f"/usr/include/python{sys.version_info.major}."
                                                            f"{sys.version_info.minor}/Python.h").exists()))
              if not present]
    if wanted and run is subprocess.run:
        _say(progress, 4, "安装 " + "、".join(wanted) + "（apt）")
        if not shutil.which("apt-get"):
            raise VoiceError("缺少 " + "、".join(wanted) + "，也没有 apt-get：请先装好再试")
        env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        run(["apt-get", "update"], capture_output=True, env=env)
        done = run(["apt-get", "install", "-y", "--no-install-recommends", *wanted], capture_output=True, text=True,
                   env=env)
        if getattr(done, "returncode", 0) != 0:
            raise VoiceError("安装 " + "、".join(wanted) + " 失败：" + (getattr(done, "stderr", "") or "")[-400:])

    _say(progress, 7, "下载 CosyVoice 源码（GitHub）")
    work = base / "download"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        for name, url in SOURCES.items():
            fetch(url, work / f"{name}.tar.gz")
    except OSError as exc:
        raise VoiceError(f"下载源码失败（GitHub 连不上？）：{exc}") from None
    _unpack(work / "src.tar.gz", base / "src")
    _unpack(work / "matcha.tar.gz", base / "src" / "third_party" / "Matcha-TTS")
    shutil.rmtree(work, ignore_errors=True)

    _say(progress, 12, "创建 Python 环境")
    venv = base / "venv"
    if not (venv / "bin" / "python").exists():
        done = run([str(python), "-m", "venv", str(venv)], capture_output=True, text=True)
        if getattr(done, "returncode", 0) != 0:
            raise VoiceError("创建 Python 环境失败：" + (getattr(done, "stderr", "") or "")[-400:])
    vpy = str(venv / "bin" / "python")
    pip = [vpy, "-m", "pip", "install", "--disable-pip-version-check", "--progress-bar", "off"]
    seen = [14]

    def on_line(line: str) -> None:                    # 14 % -> 55 %
        if line.startswith(("Collecting", "Downloading", "Installing", "Successfully")) and seen[0] < 55:
            seen[0] += 1
            _say(progress, seen[0], "安装依赖：" + line.strip()[:90])
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    steps = [(["setuptools<70", "wheel"], "基础工具"),
             (["torch==2.3.1+cpu", "torchaudio==2.3.1+cpu", "-f", TORCH_LINKS], "PyTorch（CPU 版，约 200 MB）"),
             (["--no-build-isolation", "openai-whisper==20231117"], "whisper 分词器"),
             (PACKAGES, "CosyVoice 的依赖")]
    for args, what in steps:
        _say(progress, seen[0], f"安装{what}")
        tail = ""
        for source in _pip_sources():
            code, tail = stream(pip + source + args, on_line, env)
            if code == 0:
                break
        else:
            raise VoiceError(f"安装{what}失败（检查网络 / pip 源）：\n" + tail[-1200:])

    _say(progress, 56, "下载语音模型（ModelScope，约 5 GB，要等一会儿）")
    models = base / "models"
    target = models / MODEL_DIR
    models.mkdir(parents=True, exist_ok=True)
    script = ("from modelscope import snapshot_download\n"
              f"snapshot_download({MODEL_ID!r}, local_dir={str(target)!r}, ignore_file_pattern={MODEL_SKIP!r})\n")
    stop = threading.Event()

    def watch():                                       # 56 % -> 88 %: the folder's size
        while not stop.wait(10):
            got = _dir_bytes(target)
            _say(progress, 56 + min(32, int(32 * got / MODEL_BYTES)), f"下载语音模型：{got / 1024 ** 3:.1f} / 约 5 GB")
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        code, tail = stream([vpy, "-c", script], lambda line: None,
                            {**env, "MODELSCOPE_CACHE": str(base / "cache" / "modelscope")})
    finally:
        stop.set()
    if code != 0 or not (target / "llm.pt").is_file():
        raise VoiceError("模型下载失败（检查网络，可以再点一次安装，已下载的部分会接着下）：\n" + tail[-800:])

    _say(progress, 90, "注册服务")
    for sub in ("modelscope", "matplotlib", "hf"):
        (base / "cache" / sub).mkdir(parents=True, exist_ok=True)
    _chown_tree(base / "cache", user)
    ref = reference_dir(layout.data)
    ref.mkdir(parents=True, exist_ok=True)
    _chown_tree(ref, user)
    threads = max(1, min(8, os.cpu_count() or 4))
    unit_path.write_text(unit_text(base, app, layout.data, port, user, threads), encoding="utf-8")
    run(["systemctl", "daemon-reload"], capture_output=True)
    run(["systemctl", "enable", f"{UNIT_NAME}.service"], capture_output=True)
    done = run(["systemctl", "restart", f"{UNIT_NAME}.service"], capture_output=True, text=True)
    if getattr(done, "returncode", 0) != 0:
        raise VoiceError("语音服务启动失败：" + (getattr(done, "stderr", "") or "")[-400:] +
                         f"\n查看原因：journalctl -u {UNIT_NAME} -n 40")
    _say(progress, 93, "等语音服务加载模型（约 1 分钟）")
    deadline = time.monotonic() + wait
    info = check_health(port)
    while info is None and time.monotonic() < deadline:
        time.sleep(3)
        info = check_health(port)
    if info is None:
        raise VoiceError(f"语音服务 {wait:.0f} 秒内没有启动：journalctl -u {UNIT_NAME} -n 40")
    state["voice"] = {"managed": True, "port": port, "installed_at": int(time.time()), "model": MODEL_ID,
                      "cosyvoice": COSYVOICE_SHA}
    _say(progress, 98, "交给 Violet")
    _export(layout, state)
    return {"ok": True, "installed": True, "url": f"http://127.0.0.1:{port}", "port": port,
            "notes": ["下一步：在“工具 → 语音”上传参考录音并填写录音里说的话，然后点“试听”"]}


def _export(layout: Any, state: dict[str, Any]) -> None:
    info = state["voice"]
    export = {"installed": True, "url": f"http://127.0.0.1:{info['port']}", "port": info["port"],
              "model": info.get("model"), "installed_at": info.get("installed_at")}
    path = export_path(layout.data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(export, ensure_ascii=False), encoding="utf-8")
    _chown_tree(path, state.get("user"))


def uninstall(layout: Any, state: dict[str, Any], *, run: Callable[..., Any] = subprocess.run,
              unit_path: Path = UNIT_PATH) -> list[str]:
    """What this module installed (the marked unit, <root>/voice). The uploaded recording in <data>/voice stays."""
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
    state.pop("voice", None)
    export_path(layout.data).unlink(missing_ok=True)
    return removed


def status(state: dict[str, Any], unit_path: Path = UNIT_PATH) -> dict[str, Any]:
    info = state.get("voice") or {}
    if not unit_path.exists() or not info:
        return {"installed": False, "requirements": REQUIREMENTS}
    port = int(info.get("port") or 0)
    return {"installed": True, "url": f"http://127.0.0.1:{port}", "port": port, "health": health(port)}
