"""2026-10-31: her voice (user: 让她用大肥鱼的声音说话; option A, CosyVoice on the server's CPU, optional). Fakes only:
no PyTorch, no model, no QQ -- a loopback HTTP stand-in for the voice service."""

from __future__ import annotations

import base64
import io
import json
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_qq_social_violet import SAME, FakeAdapter, admin, db, engine  # noqa: F401

MP3 = b"ID3\x04\x00\x00" + b"\x00" * 64


@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("VIOLET_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data" / "control").mkdir(parents=True)
    return tmp_path / "data"


@pytest.fixture()
def service(data):
    """A stand-in voice service on 127.0.0.1; .fail makes /tts answer 400."""
    state = SimpleNamespace(texts=[], fail=False)

    class H(BaseHTTPRequestHandler):
        def do_GET(self):                                    # noqa: N802
            body = json.dumps({"ok": True, "model": "fake", "busy": False}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):                                   # noqa: N802
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.texts.append(payload)
            if state.fail:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": "模型累了"}).encode())
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"#!SILK_V3" + payload["text"].encode())

        def log_message(self, *a):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (data / "control" / "voice.json").write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}",
                                                            "port": server.server_port}), encoding="utf-8")
    yield state
    server.shutdown()


def wait_for(cond, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------------------------------------ ai/voice.py

def test_the_reference_recording_and_readiness(data, service):
    from ai import voice
    assert voice.service() is not None and not voice.ready()              # installed, no recording yet
    with pytest.raises(voice.VoiceError):
        voice.save_reference(b"not audio", "x")
    ref = voice.save_reference(MP3, "傲娇怎么了？我就傲娇")
    assert ref == {"uploaded": True, "file": "reference.mp3", "size": len(MP3), "text": "傲娇怎么了？我就傲娇"}
    assert voice.ready() and voice.save_reference(None, "改一下文字")["text"] == "改一下文字"
    assert voice.speak("你好呀") == "#!SILK_V3你好呀".encode()
    service.fail = True
    with pytest.raises(voice.VoiceError, match="模型累了"):
        voice.speak("你好")
    voice.delete_reference()
    assert not voice.reference()["uploaded"] and not voice.ready()


def test_voice_markup_becomes_a_voice_message():
    from ai.chat_output import normalize_chat_output
    out = normalize_chat_output("哼|||<voice>人家才没有想你呢</voice>", sticker_for_label=lambda label: None)
    assert [(m["type"], m["content"]) for m in out["messages"]] == [("text", "哼"), ("voice", "人家才没有想你呢")]
    from ai.channels.outbound import chat_payload, envelope_from_payload
    sealed = chat_payload("<voice>晚安</voice>")
    assert envelope_from_payload(sealed).send_list()[0]["type"] == "voice"


# ------------------------------------------------------------------------------------------------ the owner's chat

class VoiceClient:
    def __init__(self, voice=True):
        self.voice, self.sent = voice, []

    def send_text(self, text, client_id=None, **_):
        self.sent.append(("text", text))
        return {"ok": True}

    def send_voice(self, path, **_):
        self.sent.append(("voice", Path(path).read_bytes()))
        return {"ok": True}

    def supports_voice(self):
        return self.voice

    def send_typing(self, status=0):
        pass


def sender(db, client):
    from violet_runtime import MessageSender
    return MessageSender(client, db, simulate_typing=False, channel="napcat", peer_id="9")


def test_a_voice_line_is_said_in_the_background_and_falls_back_to_text(db, data, service):
    from ai import voice
    from ai.channels.outbound import _seal
    voice.save_reference(MP3, "参考")
    client = VoiceClient()
    report = sender(db, client).send_messages(_seal([{"type": "text", "content": "哼"},
                                                     {"type": "voice", "content": "才没有想你"}]), origin="reply")
    assert [m["content"] for m in report.sent] == ["哼", "[语音] 才没有想你"]
    assert wait_for(lambda: len(client.sent) == 2)
    assert client.sent == [("text", "哼"), ("voice", "#!SILK_V3才没有想你".encode())]
    assert wait_for(lambda: db.fetch_one("SELECT 1 FROM messages WHERE content = '[语音] 才没有想你'"))
    service.fail = True                                      # the voice does not come: the words do
    client.sent.clear()
    sender(db, client).send_messages(_seal([{"type": "voice", "content": "晚安"}]), origin="reply")
    assert wait_for(lambda: client.sent == [("text", "晚安")])


def test_no_voice_means_the_words_are_written_at_once(db, data, service):
    from ai.channels.outbound import _seal
    client = VoiceClient()                                   # no recording uploaded yet
    sender(db, client).send_messages(_seal([{"type": "voice", "content": "晚安"}]), origin="reply")
    assert client.sent == [("text", "晚安")]
    from ai import voice
    voice.save_reference(MP3, "参考")
    client = VoiceClient(voice=False)                        # a channel without voice messages
    sender(db, client).send_messages(_seal([{"type": "voice", "content": "晚安"}]), origin="reply")
    assert client.sent == [("text", "晚安")]
    client = VoiceClient()
    sender(db, client).send_messages(_seal([{"type": "voice", "content": "长" * 100}]), origin="reply")
    assert client.sent == [("text", "长" * 100)]              # too long for a voice message


# ------------------------------------------------------------------------------------------------ groups

def test_a_group_voice_line(db, data, service, monkeypatch):
    from ai import voice
    from ai.social.store import SocialStore
    import ai.social.llm as llm
    voice.save_reference(MP3, "参考")
    conv = SocialStore(db).conversation("napcat", "group", "614", "群", default_mode="auto")

    class Voiced(FakeAdapter):
        def send_to(self, kind, peer_id, text, reply_to=None, sticker_path=None, image_path=None, voice_path=None):
            self.sent.append((kind, peer_id, text or ("voice:" + Path(voice_path).read_bytes().decode()
                                                       if voice_path else None), reply_to))
            return f"out{len(self.sent)}"
    a = Voiced()
    e = engine(db, a)
    e.receive(a, kind="group", peer_id="614", sender_id="1", sender_name="阿凯", text="大肥鱼说句话", platform_msg_id="1")
    seen = {}
    monkeypatch.setattr(llm, "decide", lambda system, user, tools=None: (seen.update(system=system) or (
        {"action": "say", "messages": ["来了|||<voice>哼 就说一句</voice>"]}, "f", 1)))
    e.decide(conv["id"])
    assert "【语音】你可以发语音" in seen["system"]
    assert wait_for(lambda: len(a.sent) == 2)
    assert a.sent[0][2] == "来了" and a.sent[1][2] == "voice:#!SILK_V3哼 就说一句"
    assert wait_for(lambda: SocialStore(db).messages(conv["id"])[-1]["content"] == "[语音] 哼 就说一句")


def test_qq_voice_messages_are_record_segments(tmp_path, monkeypatch):
    from ai.channels.snowluma import SnowLumaChannel
    from tests.test_qq_social_violet import FakeWS
    ch = SnowLumaChannel("napcat", ws_factory=lambda *a, **k: FakeWS([]))
    calls = []
    monkeypatch.setattr(ch, "call", lambda action, params=None, timeout=20.0: calls.append((action, params)) or {"message_id": 5})
    f = tmp_path / "a.silk"
    f.write_bytes(b"#!SILK_V3abc")
    ch.send_voice("9", str(f))
    ch.send_to("group", "614", None, voice_path=str(f))
    seg = {"type": "record", "data": {"file": "base64://" + base64.b64encode(b"#!SILK_V3abc").decode()}}
    assert calls[0] == ("send_private_msg", {"user_id": 9, "message": [seg]})
    assert calls[1] == ("send_group_msg", {"group_id": 614, "message": [seg]})


# ------------------------------------------------------------------------------------------------ the installer

def _archive(top: str, files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return out.getvalue()


def test_requirements_are_checked_first():
    from tools import violet_voice as vv
    good = dict(cpuinfo="flags\t: fpu sse avx2 fma\n", meminfo="MemTotal:       16000000 kB\n", machine="x86_64",
                disk_free=60.0)
    assert vv.check(Path("/opt/violet"), **good) == []
    problems = vv.check(Path("/opt/violet"), cpuinfo="flags: sse\n", meminfo="MemTotal: 4000000 kB\n", machine="aarch64",
                        disk_free=5.0)
    assert len(problems) == 4 and any("AVX2" in p for p in problems) and any("8 GB" in p for p in problems)


def test_install_and_uninstall_with_fakes(tmp_path):
    from tools import violet_voice as vv
    layout = SimpleNamespace(root=tmp_path / "opt", data=tmp_path / "opt" / "data", app=tmp_path / "opt" / "app")
    layout.data.mkdir(parents=True)
    state = {"user": None}
    unit = tmp_path / "violet-voice.service"
    commands, steps = [], []

    def fetch(url, target):
        top = "CosyVoice-x" if "CosyVoice" in url else "Matcha-TTS-x"
        target.write_bytes(_archive(top, {"README.md": b"x", "cosyvoice/__init__.py" if "CosyVoice" in url
                                          else "matcha/__init__.py": b""}))

    def stream(cmd, on_line, env=None):
        commands.append(cmd)
        if "-c" in cmd:                                       # the model download
            target = layout.root / "voice" / "models" / vv.MODEL_DIR
            target.mkdir(parents=True, exist_ok=True)
            (target / "llm.pt").write_bytes(b"x")
            assert "llm.rl.pt" in cmd[-1]                     # the files a CPU does not need are skipped
        return 0, ""
    result = vv.install(layout, state, python=Path("/usr/bin/python3"), app=layout.app, port_free=lambda p, h: True,
                        run=lambda cmd, **kw: commands.append(cmd) or SimpleNamespace(returncode=0, stderr=""),
                        stream=stream, fetch=fetch, progress=lambda pct, step: steps.append(step), unit_path=unit,
                        wait=1, check_health=lambda port: {"ok": True}, checks=lambda root: [])
    assert result["url"] == "http://127.0.0.1:18095" and state["voice"]["port"] == 18095
    assert (layout.root / "voice" / "src" / "cosyvoice" / "__init__.py").is_file()
    assert (layout.root / "voice" / "src" / "third_party" / "Matcha-TTS" / "matcha" / "__init__.py").is_file()
    pip_lines = [" ".join(c) for c in commands if "pip" in c]
    assert any("torch==2.3.1+cpu" in c and vv.TORCH_LINKS in c for c in pip_lines)
    assert any("--no-build-isolation openai-whisper" in c for c in pip_lines)
    text = unit.read_text(encoding="utf-8")
    assert vv.MARK in text and "voice_server.py" in text and "--port 18095" in text and "Nice=10" in text
    assert json.loads((layout.data / "control" / "voice.json").read_text())["url"] == "http://127.0.0.1:18095"
    assert any("模型" in s for s in steps)
    with pytest.raises(vv.VoiceError, match="装不了语音"):
        vv.install(layout, {}, python=Path("x"), app=layout.app, port_free=lambda p, h: True,
                   run=lambda cmd, **kw: SimpleNamespace(returncode=0), unit_path=unit, checks=lambda root: ["内存不够"])
    removed = vv.uninstall(layout, state, run=lambda cmd, **kw: None, unit_path=unit)
    assert not unit.exists() and not (layout.root / "voice").exists() and "voice" not in state and removed
    assert not (layout.data / "control" / "voice.json").exists()


def test_the_root_task_knows_voice():
    from ai.root_tasks import TASKS
    from tools.violet_installer import TASK_ACTIONS
    assert TASK_ACTIONS["voice-install"] == ("voice", "install") and "voice-uninstall" in TASKS


# ------------------------------------------------------------------------------------------------ the page

def test_voice_routes(admin, data, service, monkeypatch):
    d = admin.get("/api/control/voice").get_json()["data"]
    assert d["installed"] and not d["reference"]["uploaded"] and any("AVX2" in r for r in d["requirements"])
    up = admin.post("/api/control/voice/reference", json={"data": "data:audio/mpeg;base64," + base64.b64encode(MP3).decode(),
                                                          "text": "参考的话"}, headers=SAME).get_json()["data"]
    assert up["uploaded"] and up["text"] == "参考的话"
    r = admin.post("/api/control/voice/test", json={"text": "试一下"}, headers=SAME)
    assert r.status_code == 200 and r.mimetype == "audio/wav" and service.texts[-1] == {"text": "试一下", "format": "wav"}
    assert admin.post("/api/control/voice/install", json={}, headers=SAME).status_code == 400      # needs confirm
    assert admin.delete("/api/control/voice/reference", headers=SAME).get_json()["data"]["uploaded"] is False
