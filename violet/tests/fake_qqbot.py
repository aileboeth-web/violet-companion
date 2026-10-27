"""Phase 10.5: a loopback fake of the QQ official bot platform -- token endpoint, REST API (/gateway, C2C messages)
and the WebSocket gateway (Hello, Identify / Resume, heartbeats, dispatch). Nothing leaves 127.0.0.1.

Tests drive it with ``push_c2c`` (an inbound private message), ``drop_connection`` (the gateway closes the socket),
``reject_active`` (QQ refuses messages without msg_id) and read ``sent`` / ``identifies`` / ``resumes``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

APP_ID = "102000001"
APP_SECRET = "FakeQQBotSecret0123456789abcdef"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeQQBot:
    def __init__(self, heartbeat_ms: int = 300):
        self.heartbeat_ms = heartbeat_ms
        self.tokens_issued = 0
        self.sent: list[dict] = []
        self.identifies: list[dict] = []
        self.resumes: list[dict] = []
        self.heartbeats = 0
        self.reject_active = False
        self.expire_token_once = False
        self.seq = 0
        self.session_id = "fake-session-1"
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._seen_seq: set[tuple[str, int]] = set()
        # WebSocket gateway
        self.ws_server = socket.socket()
        self.ws_server.bind(("127.0.0.1", 0))
        self.ws_server.listen(5)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        # HTTP API (token + REST)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.http.fake = self                                   # type: ignore[attr-defined]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.http.server_address[1]}"

    @property
    def token_url(self) -> str:
        return self.api_base + "/app/getAppAccessToken"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.ws_server.getsockname()[1]}/websocket"

    @property
    def sent_texts(self) -> list[str]:
        return [m["content"] for m in self.sent]

    def close(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self.ws_server.close()
        for c in list(self._clients):
            try:
                c.close()
            except OSError:
                pass

    # -- WebSocket server side ----------------------------------------------------------------------------------
    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self.ws_server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = conn.recv(4096)
            if not chunk:
                return
            request += chunk
        key = next(l.split(":", 1)[1].strip() for l in request.decode().split("\r\n") if l.lower().startswith("sec-websocket-key"))
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        with self._lock:
            self._clients.append(conn)
        self._send(conn, {"op": 10, "d": {"heartbeat_interval": self.heartbeat_ms}})
        try:
            while True:
                frame = self._recv(conn)
                if frame is None:
                    return
                op, data = frame.get("op"), frame.get("d") or {}
                if op == 2:
                    self.identifies.append(data)
                    self._dispatch(conn, "READY", {"session_id": self.session_id, "user": {"username": "Eve测试"}})
                elif op == 6:
                    self.resumes.append(data)
                    self._dispatch(conn, "RESUMED", {})
                elif op == 1:
                    self.heartbeats += 1
                    self._send(conn, {"op": 11})
        except OSError:
            return
        finally:
            with self._lock:
                if conn in self._clients:
                    self._clients.remove(conn)

    def _dispatch(self, conn, event: str, data: dict) -> None:
        with self._lock:
            self.seq += 1
            seq = self.seq
        self._send(conn, {"op": 0, "s": seq, "t": event, "d": data})

    def push_c2c(self, content: str, openid: str = "OPENID_OWNER", msg_id: str | None = None,
                 attachments: list | None = None) -> str:
        msg_id = msg_id or f"MSG{time.time_ns()}"
        deadline = time.time() + 5
        while not self._clients and time.time() < deadline:
            time.sleep(0.05)
        for conn in list(self._clients):
            self._dispatch(conn, "C2C_MESSAGE_CREATE", {"id": msg_id, "author": {"user_openid": openid},
                                                         "content": content, "timestamp": "2026-10-26T10:00:00+08:00",
                                                         "attachments": attachments or []})
        return msg_id

    def push_group_at(self, content: str) -> None:
        for conn in list(self._clients):
            self._dispatch(conn, "GROUP_AT_MESSAGE_CREATE", {"id": "G1", "group_openid": "GROUP",
                                                              "author": {"member_openid": "M"}, "content": content})

    def drop_connection(self) -> None:
        for conn in list(self._clients):
            try:
                conn.shutdown(socket.SHUT_RDWR)
                conn.close()
            except OSError:
                pass
        with self._lock:
            self._clients.clear()

    @staticmethod
    def _send(conn, obj) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode()
        header = bytes([0x81])
        if len(payload) < 126:
            header += bytes([len(payload)])
        elif len(payload) < 65536:
            header += bytes([126]) + struct.pack("!H", len(payload))
        else:
            header += bytes([127]) + struct.pack("!Q", len(payload))
        conn.sendall(header + payload)

    @staticmethod
    def _recv_exact(conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _recv(self, conn):
        head = self._recv_exact(conn, 2)
        if head is None:
            return None
        opcode, length = head[0] & 0x0F, head[1] & 0x7F
        assert head[1] & 0x80, "client frames must be masked"
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(conn, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(conn, 8))[0]
        mask = self._recv_exact(conn, 4)
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._recv_exact(conn, length) or b""))
        if opcode == 0x8:
            return None
        if opcode != 0x1:
            return {}
        return json.loads(payload.decode())


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _answer(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self, fake: FakeQQBot) -> bool:
        expected = f"QQBot token-{fake.tokens_issued}"
        if fake.expire_token_once and self.headers.get("Authorization") == "QQBot token-1":
            fake.expire_token_once = False
            return False
        return self.headers.get("Authorization") == expected and self.headers.get("X-Union-Appid") == APP_ID

    def do_GET(self):
        fake: FakeQQBot = self.server.fake                  # type: ignore[attr-defined]
        if urlsplit(self.path).path == "/gateway":
            if not self._authorized(fake):
                self._answer(401, {"code": 11241, "message": "token invalid"})
                return
            self._answer(200, {"url": fake.ws_url})
            return
        self._answer(404, {"code": 404, "message": "not found"})

    def do_POST(self):
        fake: FakeQQBot = self.server.fake                  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode() or "{}") if length else {}
        path = urlsplit(self.path).path
        if path == "/app/getAppAccessToken":
            if body.get("appId") == APP_ID and body.get("clientSecret") == APP_SECRET:
                fake.tokens_issued += 1
                self._answer(200, {"access_token": f"token-{fake.tokens_issued}", "expires_in": "7200"})
            else:
                self._answer(200, {"code": 100016, "message": "invalid appid or secret"})
            return
        if path.startswith("/v2/users/") and path.endswith("/messages"):
            if not self._authorized(fake):
                self._answer(401, {"code": 11241, "message": "token invalid"})
                return
            if fake.reject_active and not body.get("msg_id"):
                self._answer(400, {"code": 22009, "message": "主动消息额度不足"})
                return
            key = (body.get("msg_id") or "", int(body.get("msg_seq") or 0))
            with fake._lock:
                if body.get("msg_id") and key in fake._seen_seq:
                    self._answer(200, {"code": 40054005, "message": "消息被去重，请检查请求msgseq"})
                    return
                fake._seen_seq.add(key)
                fake.sent.append({"openid": path.split("/")[3], **body})
            self._answer(200, {"id": f"OUT{len(fake.sent)}", "timestamp": 1})
            return
        self._answer(404, {"code": 404, "message": "not found"})
