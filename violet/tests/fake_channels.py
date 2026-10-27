"""Phase 10: loopback fakes of chat platforms (Telegram Bot API, OneBot v11 HTTP API) and an in-memory adapter.

Each fake records what Violet sent and serves what a test queued; nothing leaves 127.0.0.1.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from ai.channels.base import ChannelAdapter, InboundMessage, Part

TOKEN = "123456789:AAEfakeFAKEfakeFAKEfakeFAKEfake_1234"


class _Server:
    def __init__(self, handler_cls):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.httpd.fake = self                       # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeTelegram(_Server):
    """Bot API subset: getMe, getUpdates (offset honoured, returns at once), sendMessage, sendChatAction, sendPhoto,
    getFile + /file/ downloads. ``fail_next`` = list of (status, body) answers consumed one per call."""

    def __init__(self, token: str = TOKEN):
        self.token = token
        self.updates: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.offsets: list[int] = []
        self.fail_next: list[tuple[int, dict]] = []
        self.files: dict[str, bytes] = {}
        self._next_update = 1
        self.lock = threading.Lock()
        super().__init__(_TelegramHandler)

    def queue(self, message: dict[str, Any]) -> int:
        with self.lock:
            update_id = self._next_update
            self._next_update += 1
            self.updates.append({"update_id": update_id, "message": message})
            return update_id

    @staticmethod
    def private_text(text: str, user_id: int = 4242, message_id: int = 1, **extra) -> dict[str, Any]:
        return {"message_id": message_id, "from": {"id": user_id, "is_bot": False, "first_name": "小满"},
                "chat": {"id": user_id, "type": "private"}, "date": 1, "text": text, **extra}

    @property
    def sent_texts(self) -> list[str]:
        return [m.get("text") for m in self.sent if "text" in m]


class _TelegramHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):          # quiet
        pass

    def _answer(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        fake: FakeTelegram = self.server.fake          # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        prefix = f"/file/bot{fake.token}/"
        if path.startswith(prefix) and path[len(prefix):] in fake.files:
            data = fake.files[path[len(prefix):]]
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._answer(404, {"ok": False, "description": "Not Found"})

    def do_POST(self):
        fake: FakeTelegram = self.server.fake          # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        path = urlsplit(self.path).path
        if not path.startswith(f"/bot{fake.token}/"):
            self._answer(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
            return
        method = path.rsplit("/", 1)[-1]
        fake.calls.append(method)
        with fake.lock:
            failure = fake.fail_next.pop(0) if fake.fail_next else None
        if failure is not None:
            self._answer(*failure)
            return
        content_type = self.headers.get("Content-Type", "")
        body = json.loads(raw.decode("utf-8") or "{}") if "json" in content_type else {}
        if method == "getMe":
            self._answer(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "violet_test_bot",
                                                      "first_name": "Violet"}})
        elif method == "getUpdates":
            offset = int(body.get("offset") or 0)
            fake.offsets.append(offset)
            with fake.lock:
                fake.updates = [u for u in fake.updates if u["update_id"] >= offset]
                result = list(fake.updates)
            self._answer(200, {"ok": True, "result": result})
        elif method == "sendMessage":
            fake.sent.append(body)
            self._answer(200, {"ok": True, "result": {"message_id": 100 + len(fake.sent)}})
        elif method == "sendChatAction":
            fake.actions.append(body)
            self._answer(200, {"ok": True, "result": True})
        elif method == "sendPhoto":
            fake.sent.append({"photo": len(raw)})
            self._answer(200, {"ok": True, "result": {"message_id": 100 + len(fake.sent)}})
        elif method == "getFile":
            file_id = body.get("file_id")
            name = f"photos/{file_id}.jpg"
            self._answer(200, {"ok": True, "result": {"file_id": file_id, "file_path": name,
                                                      "file_size": len(fake.files.get(name, b""))}})
        else:
            self._answer(404, {"ok": False, "description": "method not found"})


class FakeOneBot(_Server):
    """OneBot v11 HTTP API subset: get_login_info, get_status, send_private_msg (records message segments)."""

    def __init__(self, access_token: str | None = None):
        self.access_token = access_token
        self.sent: list[dict[str, Any]] = []
        self.calls: list[str] = []
        super().__init__(_OneBotHandler)

    @property
    def sent_texts(self) -> list[str]:
        out = []
        for m in self.sent:
            out.append("".join(seg["data"].get("text", "") for seg in m["message"] if seg["type"] == "text"))
        return out


class _OneBotHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _answer(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        fake: FakeOneBot = self.server.fake           # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if fake.access_token and self.headers.get("Authorization") != f"Bearer {fake.access_token}":
            self._answer(401, {"status": "failed", "retcode": 1403})
            return
        action = urlsplit(self.path).path.strip("/")
        fake.calls.append(action)
        if action == "get_login_info":
            self._answer(200, {"status": "ok", "retcode": 0, "data": {"user_id": 10001, "nickname": "Violet"}})
        elif action == "get_status":
            self._answer(200, {"status": "ok", "retcode": 0, "data": {"online": True, "good": True}})
        elif action == "send_private_msg":
            fake.sent.append(body)
            self._answer(200, {"status": "ok", "retcode": 0, "data": {"message_id": len(fake.sent)}})
        else:
            self._answer(404, {"status": "failed", "retcode": 1404})


class MemoryChannel(ChannelAdapter):
    """An in-memory adapter: tests push messages with ``push`` and read ``sent``."""

    channel_type = "telegram"          # read as a non-WeChat platform by the prompt adaptation
    label = "Telegram"
    capabilities = frozenset({"text", "image_in", "image_out", "typing"})

    def __init__(self, channel_id: str = "tg", config=None, secrets=None, data_dir=None, **kwargs):
        super().__init__(channel_id, config, secrets)
        self.sent: list[tuple[str, str]] = []
        self.images: list[tuple[str, str]] = []
        self.typing_calls: list[tuple[str, bool]] = []
        self.fail_sends: list[BaseException] = []
        self.inbox: list[InboundMessage] = []
        self.results: list[bool] = []
        self._counter = 0

    def push(self, text: str | None = None, peer: str = "4242", parts: list[Part] | None = None,
             name: str = "小满") -> bool:
        self._counter += 1
        message = InboundMessage(channel=self.channel_id, channel_type=self.channel_type, peer_id=peer,
                                 dedup_key=f"mem:{self.channel_id}:{self._counter}",
                                 parts=parts if parts is not None else [Part("text", text or "")], peer_name=name)
        ok = self.deliver(message)
        self.results.append(ok)
        return ok

    def receive_once(self) -> int:
        return 0

    def idle_delay(self) -> float:
        return 0.05

    def send_text(self, peer_id: str, text: str, client_id: str | None = None) -> dict[str, Any]:
        if self.fail_sends:
            raise self.fail_sends.pop(0)
        self.sent.append((peer_id, text))
        return {"ok": True}

    def send_image(self, peer_id: str, path: str) -> dict[str, Any]:
        self.images.append((peer_id, path))
        return {"ok": True}

    def send_typing(self, peer_id: str, on: bool) -> None:
        self.typing_calls.append((peer_id, on))

    def download(self, part: Part):
        if part.data.get("fail"):
            raise RuntimeError("simulated download failure")
        return part.data.get("path", "fake-image.jpg")

    @property
    def sent_texts(self) -> list[str]:
        return [t for _, t in self.sent]
