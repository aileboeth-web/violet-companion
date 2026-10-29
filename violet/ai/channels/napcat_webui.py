"""NapCat's management API, for the QQ login on the channels page (2026-10-29).

Read from NapCat's own source (packages/napcat-webui-backend): POST /api/auth/login {"hash": sha256(token + ".napcat")}
returns a credential (valid one hour) that goes into "Authorization: Bearer <credential>"; then

  /api/QQLogin/CheckLoginStatus   {isLogin, isOffline, qrcodeurl, loginError, loginPhase}
  /api/QQLogin/GetQQLoginInfo     {uin, nick, online, avatarUrl}
  /api/QQLogin/RefreshQRcode      a new QR code
  /api/QQLogin/SetQuickLoginQQ    {uin}: log this account in by itself after a restart

Every answer is {code: 0 | error, message, data}. Only NapCat on this machine is ever asked (the address comes from
the installer's export, 127.0.0.1); its token never leaves the server.
"""

from __future__ import annotations

import base64
import hashlib
import io
import threading
import time
from typing import Any

import requests

_CACHE: dict[str, Any] = {"credential": None, "until": 0.0, "url": None}
_LOCK = threading.Lock()


class WebUIError(RuntimeError):
    pass


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False                              # 127.0.0.1 never goes through a proxy
    return s


def _post(base: str, path: str, body: dict | None = None, credential: str | None = None, timeout: float = 10.0) -> Any:
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    try:
        response = _session().post(base.rstrip("/") + path, json=body or {}, headers=headers, timeout=(3, timeout))
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise WebUIError(f"NapCat 没有响应（{exc.__class__.__name__}）：QQ 可能还在启动") from None
    if not isinstance(data, dict):
        raise WebUIError("NapCat 的回答看不懂")
    if data.get("code") not in (0, None):
        raise WebUIError(str(data.get("message") or "NapCat 返回错误"))
    return data.get("data")


def _credential(info: dict[str, Any], fresh: bool = False) -> str:
    with _LOCK:
        if not fresh and _CACHE["credential"] and _CACHE["url"] == info["webui_url"] and time.time() < _CACHE["until"]:
            return _CACHE["credential"]
        digest = hashlib.sha256((str(info["webui_token"]) + ".napcat").encode("utf-8")).hexdigest()
        data = _post(info["webui_url"], "/api/auth/login", {"hash": digest}) or {}
        credential = data.get("Credential")
        if not credential:
            raise WebUIError("NapCat 没有给出登录凭证")
        _CACHE.update(credential=credential, until=time.time() + 3000, url=info["webui_url"])
        return credential


def call(info: dict[str, Any], path: str, body: dict | None = None) -> Any:
    try:
        return _post(info["webui_url"], path, body, _credential(info))
    except WebUIError as exc:
        if "Unauthorized" in str(exc) or "Authorization" in str(exc):
            return _post(info["webui_url"], path, body, _credential(info, fresh=True))
        raise


def qr_image(content: str) -> str | None:
    """The QR code as an SVG data URL (a data:image URL from NapCat is passed through)."""
    if not content:
        return None
    if content.startswith("data:image/"):
        return content
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
    except ImportError:
        return None
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(content)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(image_factory=SvgPathImage).save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def login_state(info: dict[str, Any]) -> dict[str, Any]:
    """{logged_in, uin, nick, qr (data URL), error, phase} -- what the QQ card shows."""
    status = call(info, "/api/QQLogin/CheckLoginStatus") or {}
    out: dict[str, Any] = {"logged_in": bool(status.get("isLogin")), "offline": bool(status.get("isOffline")),
                           "phase": status.get("loginPhase"), "error": status.get("loginError") or None}
    if out["logged_in"] or out["offline"]:
        account = call(info, "/api/QQLogin/GetQQLoginInfo") or {}
        out.update(uin=str(account.get("uin") or "") or None, nick=account.get("nick"), online=account.get("online"))
    else:
        out["qr"] = qr_image(str(status.get("qrcodeurl") or ""))
    return out


def refresh_qr(info: dict[str, Any]) -> None:
    call(info, "/api/QQLogin/RefreshQRcode")


def remember_account(info: dict[str, Any], uin: str) -> None:
    call(info, "/api/QQLogin/SetQuickLoginQQ", {"uin": str(uin)})
