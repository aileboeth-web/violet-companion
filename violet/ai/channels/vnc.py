"""The QQ window of the channel "QQ" on the channels page (2026-10-29, SnowLuma).

SnowLuma has no API for the login QR code: QQ shows it in its own window, on a virtual screen (Xvfb) inside the
SnowLuma container, served by x11vnc. This is a tiny VNC (RFB 3.3 / 3.7 / 3.8) client for that one server: it takes a
picture of the screen (cut down to the windows on it) and passes on a mouse click. Only the container on this machine
is ever asked (127.0.0.1, the port and password from the installer's export); nothing here reaches the browser except
the PNG.

  screenshot(port, password)  {"png": bytes, "x", "y", "width", "height", "screen_width", "screen_height"}
  click(port, password, x, y) one left click at screen coordinates
"""

from __future__ import annotations

import socket
import struct
import time
import zlib
from typing import Any

HOST = "127.0.0.1"
CHUNK = 16                    # pixels compared at once when looking for the windows on the screen
PAD = 12


class VNCError(RuntimeError):
    pass


def reachable(port: int, timeout: float = 2.0) -> bool:
    """The VNC server answers (QQ's virtual screen is up)."""
    try:
        with socket.create_connection((HOST, int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(12).startswith(b"RFB ")
    except OSError:
        return False


def _mirror(byte: int) -> int:
    return int(f"{byte:08b}"[::-1], 2)


def vnc_response(password: str, challenge: bytes) -> bytes:
    """VNC authentication: DES of the challenge with the password (8 bytes, each byte's bits mirrored)."""
    from Crypto.Cipher import DES
    key = bytes(_mirror(b) for b in (password.encode("latin-1", "replace")[:8]).ljust(8, b"\0"))
    return DES.new(key, DES.MODE_ECB).encrypt(challenge)


class Session:
    def __init__(self, port: int, password: str, timeout: float = 8.0, host: str = HOST):
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        try:
            self._handshake(password)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _read(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            part = self.sock.recv(min(n - len(buf), 1 << 20))
            if not part:
                raise VNCError("QQ 窗口的连接断开了")
            buf += part
        return bytes(buf)

    def _reason(self) -> str:
        (length,) = struct.unpack(">I", self._read(4))
        return self._read(min(length, 4096)).decode("utf-8", "replace")

    def _handshake(self, password: str) -> None:
        banner = self._read(12)
        if not banner.startswith(b"RFB "):
            raise VNCError("不是 VNC 服务")
        try:
            minor = int(banner[8:11])
        except ValueError:
            minor = 3
        minor = 8 if minor >= 8 else 7 if minor == 7 else 3
        self.sock.sendall(b"RFB 003.%03d\n" % minor)
        if minor == 3:
            (kind,) = struct.unpack(">I", self._read(4))
            if kind == 0:
                raise VNCError(self._reason())
        else:
            (count,) = struct.unpack(">B", self._read(1))
            if count == 0:
                raise VNCError(self._reason())
            kinds = self._read(count)
            kind = 2 if 2 in kinds else 1 if 1 in kinds else 0
            if not kind:
                raise VNCError("QQ 窗口的 VNC 不支持密码登录")
            self.sock.sendall(bytes([kind]))
        if kind == 2:
            self.sock.sendall(vnc_response(password, self._read(16)))
        if kind == 2 or minor == 8:
            (result,) = struct.unpack(">I", self._read(4))
            if result != 0:
                raise VNCError("QQ 窗口的密码不对" + (f"：{self._reason()}" if minor == 8 else ""))
        self.sock.sendall(b"\x01")                                      # shared: other viewers stay connected
        self.width, self.height = struct.unpack(">HH", self._read(4))
        self._read(16)                                                  # the server's pixel format: replaced below
        (name_length,) = struct.unpack(">I", self._read(4))
        self._read(name_length)
        # 32 bits per pixel, little endian, red << 16 | green << 8 | blue: bytes B, G, R, X
        self.sock.sendall(b"\x00\x00\x00\x00" + struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        self.sock.sendall(struct.pack(">BxHi", 2, 1, 0))                # encodings: raw only

    def frame(self, wait: float = 8.0) -> bytes:
        """The whole screen as BGRX bytes (width * height * 4)."""
        w, h = self.width, self.height
        pixels = bytearray(w * h * 4)
        self.sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
        covered, deadline = 0, time.monotonic() + wait
        while covered < w * h:
            if time.monotonic() > deadline:
                raise VNCError("QQ 窗口的画面没有传完")
            (kind,) = struct.unpack(">B", self._read(1))
            if kind == 0:
                (rects,) = struct.unpack(">xH", self._read(3))
                for _ in range(rects):
                    x, y, rw, rh, enc = struct.unpack(">HHHHi", self._read(12))
                    if enc != 0:
                        raise VNCError(f"VNC 用了不支持的编码 {enc}")
                    data = self._read(rw * rh * 4)
                    for row in range(rh):
                        if y + row >= h:
                            break
                        start = ((y + row) * w + x) * 4
                        pixels[start:start + min(rw, w - x) * 4] = data[row * rw * 4:(row * rw + min(rw, w - x)) * 4]
                    covered += rw * rh
            elif kind == 1:                                             # colour map entries (not used at 32 bpp)
                _, _, n = struct.unpack(">xHH", self._read(5))
                self._read(n * 6)
            elif kind == 2:                                             # bell
                continue
            elif kind == 3:                                             # clipboard text
                self._read(3)
                (length,) = struct.unpack(">I", self._read(4))
                self._read(length)
            else:
                raise VNCError(f"VNC 发来了不认识的消息 {kind}")
        return bytes(pixels)

    def click(self, x: int, y: int) -> None:
        x, y = max(0, min(int(x), self.width - 1)), max(0, min(int(y), self.height - 1))
        for mask in (0, 1, 0):                                          # move there, press, release
            self.sock.sendall(struct.pack(">BBHH", 5, mask, x, y))
            time.sleep(0.05)


def content_box(pixels: bytes, width: int, height: int) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) around what is drawn on the screen (QQ's windows), leaving out the empty background and rows
    that are drawn over the whole width (the window manager's bar). The background is what the left edge shows at
    the middle of the screen, as a 2 x 2 tile: a plain colour or X's default weave. The whole screen when nothing is
    found."""
    stride = width * 4
    if width < 2 or height < 2:
        return 0, 0, width, height
    mid = (height // 2) & ~1
    empty_rows = [(pixels[(mid + p) * stride:(mid + p) * stride + 8] * ((width + 1) // 2))[:stride] for p in (0, 1)]
    step = CHUNK * 4
    chunks = (width + CHUNK - 1) // CHUNK
    x0 = y0 = None
    x1 = y1 = 0
    for y in range(height):
        row = pixels[y * stride:(y + 1) * stride]
        empty_row = empty_rows[(y - mid) % 2]
        if row == empty_row:
            continue
        hits = [c for c in range(chunks) if row[c * step:(c + 1) * step] != empty_row[c * step:(c + 1) * step]]
        if not hits or len(hits) > chunks * 0.9:
            continue
        left, right = hits[0] * CHUNK, min(width, (hits[-1] + 1) * CHUNK)
        x0 = left if x0 is None else min(x0, left)
        x1 = max(x1, right)
        y0 = y if y0 is None else y0
        y1 = y + 1
    if x0 is None or y0 is None:
        return 0, 0, width, height
    box = (max(0, x0 - PAD), max(0, y0 - PAD), min(width, x1 + PAD), min(height, y1 + PAD))
    if (box[2] - box[0]) * (box[3] - box[1]) > width * height * 0.8:
        return 0, 0, width, height
    return box


def png(pixels: bytes, width: int, box: tuple[int, int, int, int]) -> bytes:
    """An RGB PNG of `box` out of a BGRX screen."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    raw = bytearray()
    stride = width * 4
    for y in range(y0, y1):
        line = pixels[y * stride + x0 * 4:y * stride + x1 * 4]
        rgb = bytearray(w * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = line[2::4], line[1::4], line[0::4]
        raw += b"\x00" + rgb

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def screenshot(port: int, password: str, timeout: float = 8.0, session_factory=Session,
               crop: bool = True) -> dict[str, Any]:
    """The screen cut down to QQ's windows (crop=False: all of it)."""
    try:
        with session_factory(port, password, timeout=timeout) as s:
            pixels = s.frame(wait=timeout)
            width, height = s.width, s.height
    except OSError as exc:
        raise VNCError(f"连不上 QQ 窗口（{exc.__class__.__name__}）：QQ 可能还在启动") from None
    box = content_box(pixels, width, height) if crop else (0, 0, width, height)
    return {"png": png(pixels, width, box), "x": box[0], "y": box[1], "width": box[2] - box[0],
            "height": box[3] - box[1], "screen_width": width, "screen_height": height}


def click(port: int, password: str, x: int, y: int, timeout: float = 5.0, session_factory=Session) -> None:
    try:
        with session_factory(port, password, timeout=timeout) as s:
            s.click(x, y)
    except OSError as exc:
        raise VNCError(f"连不上 QQ 窗口（{exc.__class__.__name__}）") from None
