"""Violet's voice (2026-10-31, user: 让她用大肥鱼的声音说话; optional, installed by tools/violet_voice.py).

A small HTTP service on 127.0.0.1 only, run with the voice environment's Python (CosyVoice and PyTorch live there,
never in Violet's own environment). It loads the model once and speaks in the voice of a short reference recording
(zero-shot cloning): <ref>/reference.(mp3|wav|m4a|ogg) plus what is said in it, <ref>/reference.txt. The recording
is converted with ffmpeg to 16 kHz mono (reference.16k.wav), again whenever it changes.

  GET  /health          {"ok", "model", "reference", "reference_text", "busy"}
  POST /tts             {"text": "...", "format": "wav" | "silk", "speed": 1.0} -> audio
                        silk = QQ's own voice format (pilk), what QQ voice messages are made of

One request at a time (a CPU makes one sentence in roughly 5-30 seconds); the text is limited to MAX_CHARS.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_CHARS = 300
PROMPT_PREFIX = "You are a helpful assistant.<|endofprompt|>"      # CosyVoice 3 expects it before the prompt text
REFERENCE_NAMES = ("reference.wav", "reference.mp3", "reference.m4a", "reference.ogg", "reference.flac")
log = logging.getLogger("violet.voice")


class VoiceError(RuntimeError):
    pass


def find_reference(ref_dir: Path) -> Path | None:
    for name in REFERENCE_NAMES:
        path = ref_dir / name
        if path.is_file():
            return path
    return None


def prepared_reference(ref_dir: Path, ffmpeg: str = "ffmpeg") -> tuple[Path, str]:
    """(16 kHz mono wav, what is said in it). Raises VoiceError when there is no reference yet."""
    source = find_reference(ref_dir)
    if source is None:
        raise VoiceError("还没有上传参考录音")
    text_file = ref_dir / "reference.txt"
    text = text_file.read_text(encoding="utf-8").strip() if text_file.is_file() else ""
    target = ref_dir / "reference.16k.wav"
    if not target.is_file() or target.stat().st_mtime < source.stat().st_mtime:
        done = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(source), "-ac", "1", "-ar", "16000",
                               "-t", "30", str(target)], capture_output=True, text=True)
        if done.returncode != 0:
            raise VoiceError("参考录音转换失败：" + (done.stderr or "")[-300:])
    return target, text


def wav_bytes(samples, sample_rate: int) -> bytes:
    """float tensor / array in [-1, 1] -> 16-bit mono wav."""
    import numpy as np
    data = np.asarray(samples, dtype="float32").reshape(-1)
    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm)
    return out.getvalue()


def silk_bytes(wav: bytes, ffmpeg: str = "ffmpeg") -> bytes:
    """wav -> QQ's silk (24 kHz mono 16-bit PCM encoded with pilk)."""
    import pilk
    with tempfile.TemporaryDirectory() as tmp:
        src, pcm, silk = Path(tmp) / "in.wav", Path(tmp) / "in.pcm", Path(tmp) / "out.silk"
        src.write_bytes(wav)
        done = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(src), "-f", "s16le", "-ac", "1", "-ar",
                               "24000", str(pcm)], capture_output=True, text=True)
        if done.returncode != 0:
            raise VoiceError("转换成 QQ 语音格式失败：" + (done.stderr or "")[-300:])
        pilk.encode(str(pcm), str(silk), pcm_rate=24000, tencent=True)
        return silk.read_bytes()


class Voice:
    def __init__(self, src: Path, model_dir: Path, ref_dir: Path, threads: int = 0, ffmpeg: str = "ffmpeg"):
        sys.path[:0] = [str(src), str(src / "third_party" / "Matcha-TTS")]
        import torch
        if threads:
            torch.set_num_threads(threads)
        from cosyvoice.cli.cosyvoice import AutoModel
        self.torch = torch
        self.model = AutoModel(model_dir=str(model_dir))
        self.v3 = type(self.model).__name__ == "CosyVoice3"
        self.model_name = model_dir.name
        self.ref_dir = ref_dir
        self.ffmpeg = ffmpeg
        self.lock = threading.Lock()

    def speak(self, text: str, speed: float = 1.0) -> bytes:
        text = " ".join((text or "").split())[:MAX_CHARS]
        if not text:
            raise VoiceError("没有要说的话")
        reference, ref_text = prepared_reference(self.ref_dir, self.ffmpeg)
        prompt = (PROMPT_PREFIX if self.v3 else "") + ref_text
        with self.lock:
            parts = [out["tts_speech"] for out in self.model.inference_zero_shot(
                text, prompt, str(reference), stream=False, speed=max(0.5, min(2.0, float(speed or 1.0))))]
        if not parts:
            raise VoiceError("没有生成声音")
        speech = self.torch.cat(parts, dim=1)
        return wav_bytes(speech.cpu().numpy(), self.model.sample_rate)


def make_handler(voice: Voice):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):                                    # noqa: N802
            if self.path != "/health":
                return self._json(404, {"ok": False})
            reference = find_reference(voice.ref_dir)
            text_file = voice.ref_dir / "reference.txt"
            self._json(200, {"ok": True, "model": voice.model_name, "reference": reference is not None,
                             "reference_text": text_file.read_text(encoding="utf-8").strip() if text_file.is_file() else "",
                             "busy": voice.lock.locked()})

        def do_POST(self):                                   # noqa: N802
            if self.path != "/tts":
                return self._json(404, {"ok": False})
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 16384)
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                wav = voice.speak(str(body.get("text") or ""), float(body.get("speed") or 1.0))
                audio, kind = (silk_bytes(wav, voice.ffmpeg), "audio/silk") if body.get("format") == "silk" \
                    else (wav, "audio/wav")
            except VoiceError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:                         # noqa: BLE001 -- reported, the service keeps running
                log.exception("tts failed")
                return self._json(500, {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"[:300]})
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(audio)))
            self.end_headers()
            self.wfile.write(audio)

        def log_message(self, fmt, *args):                   # noqa: D401 -- quiet: one line per request in the journal
            log.info("%s %s", self.command, self.path)
    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Violet's voice service (CosyVoice, 127.0.0.1 only)")
    parser.add_argument("--src", type=Path, required=True, help="the CosyVoice source folder")
    parser.add_argument("--model", type=Path, required=True, help="the model folder")
    parser.add_argument("--ref", type=Path, required=True, help="the folder with reference.* and reference.txt")
    parser.add_argument("--port", type=int, default=18095)
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    voice = Voice(args.src, args.model, args.ref, args.threads, args.ffmpeg)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(voice))
    log.info("voice service on 127.0.0.1:%s (model %s)", args.port, voice.model_name)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
