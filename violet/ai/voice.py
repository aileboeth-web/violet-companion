"""Her voice (2026-10-31, user: 让她用大肥鱼的声音说话; optional): talks to the voice service tools/violet_voice.py
installs (CosyVoice on the CPU, 127.0.0.1 only) and keeps the reference recording the admin uploads.

  ready()            installed (<data>/control/voice.json), switched on (VIOLET_VOICE), a recording uploaded
  speak(text, fmt)   the audio bytes ("silk" for QQ voice messages, "wav" for the page's 试听); raises VoiceError
  save_reference(…)  the recording (mp3 / wav / m4a / ogg / flac, at most 10 MB) and what is said in it

A sentence takes about a minute on 4 CPU cores, so callers speak in the background and send the words as text when
the voice does not come (MessageSender, SocialEngine).
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ai.settings import env_bool, env_int

FORMATS = {"mp3": b"ID3", "wav": b"RIFF", "ogg": b"OggS", "flac": b"fLaC"}
MAX_REFERENCE = 10 * 1024 * 1024
CHUNK = 30                     # characters in one voice message (~7 s of speech, ~4 minutes on 4 CPU cores)
TIMEOUT = 600.0


class VoiceError(RuntimeError):
    pass


def ref_dir() -> Path:
    return Path(os.environ.get("VIOLET_DATA_DIR") or "./data") / "voice"


def service() -> dict[str, Any] | None:
    from ai.root_tasks import voice_export
    return voice_export()


def reference_path() -> Path | None:
    d = ref_dir()
    return next((p for p in sorted(d.glob("reference.*")) if p.suffix[1:] in ("mp3", "wav", "m4a", "ogg", "flac")
                 and not p.name.endswith(".16k.wav")), None) if d.is_dir() else None


def reference() -> dict[str, Any]:
    d = ref_dir()
    audio = reference_path()
    text_file = d / "reference.txt"
    return {"uploaded": audio is not None, "file": audio.name if audio else None,
            "size": audio.stat().st_size if audio else 0,
            "text": text_file.read_text(encoding="utf-8").strip() if text_file.is_file() else ""}


def ready() -> bool:
    return env_bool("VIOLET_VOICE", True) and service() is not None and reference()["uploaded"]


def max_chars() -> int:
    """At most this much of one <voice> line is said (longer is split into several voice messages)."""
    return max(5, env_int("VIOLET_VOICE_MAX_CHARS", 300))


def auto() -> bool:
    """2026-11-01 (user: 有机会就用): every reply also gets a voice message of one of its lines."""
    return env_bool("VIOLET_VOICE_AUTO", True)


def chunks(text: str, size: int = CHUNK) -> list[str]:
    """Sentences packed into pieces of at most `size` characters (one voice message each), lyrics line by line."""
    pieces: list[str] = []
    for sentence in re.split(r"(?<=[。！？!?；;…~～])|\n", str(text or "")):
        sentence = " ".join(sentence.split())
        while len(sentence) > size:                          # a long sentence: at the last comma that fits, else cut
            cut = max(sentence.rfind(c, 0, size) for c in "，,、 ") + 1 or size
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            pieces.append(sentence)
    out: list[str] = []
    for piece in pieces:                                     # short sentences (lyric lines) share a message
        if out and len(out[-1]) + len(piece) + 1 <= size:
            out[-1] += ("，" if out[-1][-1:].isalnum() else "") + piece
        else:
            out.append(piece)
    return out


def pick_line(texts: list[str]) -> str | None:
    """The line of a reply that is said when she marked none: the longest one that is real words and fits in one
    voice message."""
    lines = [t.strip() for t in texts if t and t.strip() and sum(ch.isalnum() for ch in t) >= 2
             and len(t.strip()) <= CHUNK and not t.strip().startswith(("http", "[", "`"))]
    return max(lines, key=len) if lines else None


# ------------------------------------------------------------------------------------------------ the queue
# one sentence takes minutes on a CPU and the service says one at a time, so voice messages wait in one queue, in order

_jobs: "queue.Queue[tuple[list[str], Callable[[Path, str], None], str]]" = queue.Queue()
_pending = [0]                                               # sentences waiting or being said
_lock = threading.Lock()
_worker: list[threading.Thread] = []


def say_later(text: str, deliver: Callable[[Path, str], None], *, automatic: bool = False, label: str = "") -> bool:
    """Queues `text` (split by chunks()) to be said and handed to deliver(silk file, sentence) one by one. The words
    were already sent as text, so a sentence that fails is only noted. An automatic line is dropped while others
    still wait (it would only arrive long after the conversation moved on). False = not queued."""
    if not ready():
        return False
    pieces = chunks(str(text or "")[:max_chars()])
    if not pieces:
        return False
    with _lock:
        if automatic and _pending[0]:
            return False
        _pending[0] += len(pieces)
        if not _worker or not _worker[0].is_alive():
            _worker[:] = [threading.Thread(target=_work, daemon=True, name="voice-queue")]
            _worker[0].start()
    _jobs.put((pieces, deliver, label))
    return True


def busy() -> int:
    return _pending[0]


def _work() -> None:
    from ai import trace
    while True:
        pieces, deliver, label = _jobs.get()
        for i, piece in enumerate(pieces, 1):
            part = f"（{i}/{len(pieces)}）" if len(pieces) > 1 else ""
            try:
                deliver(to_file(speak(piece, "silk"), "silk"), piece)
                trace.step("发语音", 会话=label or None, 结果="已发出" + part, 内容=trace.text(piece))
            except Exception as exc:                         # noqa: BLE001 -- the words already went out as text
                trace.step("发语音", 会话=label or None, 结果="失败" + part + "（文字已经发过）", 原因=str(exc)[:200])
            finally:
                with _lock:
                    _pending[0] = max(0, _pending[0] - 1)


def _kind(data: bytes) -> str:
    for ext, magic in FORMATS.items():
        if data.startswith(magic):
            return ext
    if data[4:8] == b"ftyp":
        return "m4a"
    if len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"                                         # an mp3 without an ID3 header
    raise VoiceError("只支持 mp3 / wav / m4a / ogg / flac 的录音")


def save_reference(data: bytes | None, text: str | None) -> dict[str, Any]:
    """A new recording (data) and / or what is said in it (text)."""
    d = ref_dir()
    d.mkdir(parents=True, exist_ok=True)
    if data:
        if len(data) > MAX_REFERENCE:
            raise VoiceError("录音太大了（最多 10 MB，3–20 秒最合适）")
        ext = _kind(data)
        for old in d.glob("reference.*"):
            if old.name != "reference.txt":
                old.unlink(missing_ok=True)
        tmp = d / f"reference.{ext}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, d / f"reference.{ext}")
    if text is not None:
        (d / "reference.txt").write_text(" ".join(str(text).split())[:300], encoding="utf-8")
    return reference()


def delete_reference() -> None:
    d = ref_dir()
    for old in d.glob("reference.*") if d.is_dir() else []:
        old.unlink(missing_ok=True)


def health() -> dict[str, Any] | None:
    info = service()
    if info is None:
        return None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(info["url"] + "/health", timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def speak(text: str, fmt: str = "silk", timeout: float = TIMEOUT) -> bytes:
    info = service()
    if info is None:
        raise VoiceError("语音还没有安装")
    if not reference()["uploaded"]:
        raise VoiceError("还没有上传参考录音")
    body = json.dumps({"text": text, "format": fmt}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(info["url"] + "/tts", data=body, headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8")).get("error")
        except (OSError, ValueError):
            message = None
        raise VoiceError(message or f"语音服务返回 {exc.code}") from None
    except OSError as exc:
        raise VoiceError(f"连不上语音服务：{exc}") from None


def to_file(audio: bytes, fmt: str = "silk") -> Path:
    """The audio as a file the channel can send (<data>/media/voice, the newest 50 kept)."""
    import time
    d = Path(os.environ.get("VIOLET_DATA_DIR") or "./data") / "media" / "voice"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{int(time.time() * 1000)}.{fmt}"
    path.write_bytes(audio)
    for old in sorted(d.glob("*.*"))[:-50]:
        old.unlink(missing_ok=True)
    return path


def prompt_note(automatic: bool = True, json_field: bool = False) -> str:
    """How she speaks (only when the voice is ready); added to every prompt, also to personas with their own
    <voice> markup (2026-11-01: those said 语音不要滥用 and she hardly used it). automatic: replies there get a voice
    line of their own (private chats; not groups). json_field: the social decision's "voice" field instead of markup."""
    if not ready():
        return ""
    auto_line = ("你打的字会自动挑一句补发成语音，不用你管；" if automatic and auto() else "")
    where = "写进 JSON 的 voice 字段" if json_field else "整段放进 <voice></voice>"
    return (f"【语音】你会发语音（用你自己的声音）。{auto_line}"
            f"对方让你说话、念东西、唱歌、想听你声音时，一定要发语音：把要说的内容{where}"
            f"（最多 {max_chars()} 字，长的会自动拆成几条语音一条条发，歌词、故事都可以整段放进去），"
            "这段话也会先作为文字发出去，不用再重复打一遍。你没法按旋律唱歌，被叫唱歌时就用语音把歌词念出来，"
            "可以顺口说一句自己唱得不好。语音要几分钟才生成好，别说马上发。")
