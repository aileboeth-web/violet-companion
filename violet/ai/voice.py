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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ai.settings import env_bool, env_int

FORMATS = {"mp3": b"ID3", "wav": b"RIFF", "ogg": b"OggS", "flac": b"fLaC"}
MAX_REFERENCE = 10 * 1024 * 1024
TIMEOUT = 240.0


class VoiceError(RuntimeError):
    pass


def ref_dir() -> Path:
    return Path(os.environ.get("VIOLET_DATA_DIR") or "./data") / "voice"


def service() -> dict[str, Any] | None:
    from ai.root_tasks import voice_export
    return voice_export()


def reference() -> dict[str, Any]:
    d = ref_dir()
    audio = next((p for p in sorted(d.glob("reference.*")) if p.suffix[1:] in ("mp3", "wav", "m4a", "ogg", "flac")
                  and not p.name.endswith(".16k.wav")), None) if d.is_dir() else None
    text_file = d / "reference.txt"
    return {"uploaded": audio is not None, "file": audio.name if audio else None,
            "size": audio.stat().st_size if audio else 0,
            "text": text_file.read_text(encoding="utf-8").strip() if text_file.is_file() else ""}


def ready() -> bool:
    return env_bool("VIOLET_VOICE", True) and service() is not None and reference()["uploaded"]


def max_chars() -> int:
    return max(5, env_int("VIOLET_VOICE_MAX_CHARS", 60))


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


def prompt_note() -> str:
    """For prompts without their own voice markup: how she can speak (only when the voice is ready)."""
    if not ready():
        return ""
    return (f"【语音】你可以发语音：把要说的一句话放进 <voice></voice>（{max_chars()} 字以内），它会变成你的语音条。"
            "只在情绪重（撒娇、生气、困了）或对方想听你说话时用，一次最多一句；平时打字。语音要一分钟左右才发出去。")
