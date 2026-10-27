"""A small WebSocket client (RFC 6455, client side only) on the standard library (phase 10.5).

Used by the QQ official bot gateway; Violet has no WebSocket dependency and the gateway needs only text frames, ping /
pong and close. Supports ws:// and wss:// (certificate verified), masking of every client frame, fragmented messages,
a receive timeout (so the caller can send heartbeats) and a thread-safe send.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 4 * 1024 * 1024


class WebSocketError(RuntimeError):
    pass


class WebSocketClosed(WebSocketError):
    def __init__(self, code: int | None = None, reason: str = ""):
        super().__init__(f"closed by server (code={code}) {reason}".strip())
        self.code = code
        self.reason = reason


class WebSocket:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._send_lock = threading.Lock()
        self._buffer = b""
        self.closed = False

    # -- connect ---------------------------------------------------------------------------------------------------
    @classmethod
    def connect(cls, url: str, timeout: float = 15.0, headers: dict[str, str] | None = None,
                ssl_context: ssl.SSLContext | None = None) -> "WebSocket":
        parts = urlsplit(url)
        if parts.scheme not in ("ws", "wss") or not parts.hostname:
            raise WebSocketError("WebSocket address must start with ws:// or wss://")
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        raw = socket.create_connection((parts.hostname, port), timeout=timeout)
        try:
            if parts.scheme == "wss":
                context = ssl_context or ssl.create_default_context()
                raw = context.wrap_socket(raw, server_hostname=parts.hostname)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            host = parts.hostname + ("" if parts.port in (None, 80, 443) else f":{parts.port}")
            lines = [f"GET {path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
                     f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
            lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
            raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = raw.recv(4096)
                if not chunk:
                    raise WebSocketError("connection closed during the handshake")
                response += chunk
                if len(response) > 65536:
                    raise WebSocketError("handshake response too long")
            head, rest = response.split(b"\r\n\r\n", 1)
            status_line, *header_lines = head.decode("latin-1").split("\r\n")
            if " 101 " not in status_line + " ":
                raise WebSocketError(f"handshake refused: {status_line[:80]}")
            got = {h.split(":", 1)[0].strip().lower(): h.split(":", 1)[1].strip() for h in header_lines if ":" in h}
            expected = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
            if got.get("sec-websocket-accept") != expected:
                raise WebSocketError("handshake answer does not match the key")
        except Exception:
            raw.close()
            raise
        ws = cls(raw)
        ws._buffer = rest
        return ws

    # -- frames ----------------------------------------------------------------------------------------------------
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            if self.closed:
                raise WebSocketClosed(None, "already closed")
            self.sock.sendall(header + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def _read_exact(self, n: int) -> bytes:
        while len(self._buffer) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buffer)))
            if not chunk:
                self.closed = True
                raise WebSocketClosed(None, "connection lost")
            self._buffer += chunk
        data, self._buffer = self._buffer[:n], self._buffer[n:]
        return data

    def _read_frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._read_exact(2)
        fin, opcode, masked, length = bool(b1 & 0x80), b1 & 0x0F, bool(b2 & 0x80), b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if length > MAX_MESSAGE:
            raise WebSocketError("frame too large")
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def recv(self, timeout: float | None = None) -> str | None:
        """The next text message, or None when ``timeout`` passed with nothing complete. Ping is answered, pong ignored,
        close raises WebSocketClosed."""
        parts: list[bytes] = []
        kind = None
        try:
            while True:
                if not self._buffer:
                    # the timeout applies only while waiting for a frame to START: once bytes arrive the rest of the
                    # frame is read with a fixed timeout, so a timeout never leaves half a frame consumed
                    self.sock.settimeout(timeout)
                    try:
                        chunk = self.sock.recv(4096)
                    except (socket.timeout, TimeoutError):
                        if parts:
                            raise WebSocketError("timed out inside a fragmented message") from None
                        return None
                    if not chunk:
                        self.closed = True
                        raise WebSocketClosed(None, "connection lost")
                    self._buffer += chunk
                self.sock.settimeout(30.0)
                fin, opcode, payload = self._read_frame()
                if opcode == OP_PING:
                    self._send_frame(OP_PONG, payload)
                    continue
                if opcode == OP_PONG:
                    continue
                if opcode == OP_CLOSE:
                    code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                    self.closed = True
                    try:
                        self._send_frame(OP_CLOSE, payload[:2])
                    except Exception:
                        pass
                    raise WebSocketClosed(code, payload[2:].decode("utf-8", "replace"))
                if opcode in (OP_TEXT, OP_BINARY):
                    kind, parts = opcode, [payload]
                elif opcode == OP_CONT and kind is not None:
                    parts.append(payload)
                else:
                    raise WebSocketError(f"unexpected frame opcode {opcode}")
                if sum(len(p) for p in parts) > MAX_MESSAGE:
                    raise WebSocketError("message too large")
                if fin:
                    return b"".join(parts).decode("utf-8")
        except (socket.timeout, TimeoutError):
            self.closed = True
            raise WebSocketError("timed out in the middle of a frame") from None

    def close(self, code: int = 1000) -> None:
        if not self.closed:
            try:
                self._send_frame(OP_CLOSE, struct.pack("!H", code))
            except Exception:
                pass
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
