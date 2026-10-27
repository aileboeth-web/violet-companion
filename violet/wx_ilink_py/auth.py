"""Dashboard authentication (single administrator).

Every route except static assets, the login page and /healthz requires either
  * ``Authorization: Bearer <token>`` (scripts such as test_proactive.py), or
  * a session cookie obtained on /auth/login with the admin password (or the admin token).

The token comes from ``VIOLET_ADMIN_TOKEN``; if unset, a random token is created once in
``<data_dir>/admin_token`` (file mode 0600) and reused. Nothing is hardcoded and the token is never logged.

Phase 9.0 (Violet Control Center) hardening:
  * an admin PASSWORD, set with ``python -m tools.violet_admin set-password``, stored only as a werkzeug scrypt hash in
    ``<data_dir>/control/admin.json``; no default password, no registration;
  * a random per-install session secret (``<data_dir>/control/session_secret``) instead of one derived from the token;
  * sessions expire after inactivity (VIOLET_DASHBOARD_IDLE_MINUTES, default 120) and absolutely after
    VIOLET_DASHBOARD_SESSION_HOURS (default 12); a session version in admin.json ends every session at once
    (password change, "log out everywhere");
  * failed logins are limited per client (5 in 15 minutes -> locked 15 minutes) and globally (50 in 15 minutes);
  * security headers on every response, ``Cache-Control: no-store`` on everything that is not a static asset.

Phase 9.0.1:
  * the admin token can be disabled or replaced without a restart (``python -m tools.violet_admin token-disable |
    token-rotate | token-enable``); a rotated token is kept only as a SHA-256 hash in admin.json and overrides
    VIOLET_ADMIN_TOKEN / the admin_token file, so an old token stops working at once; every change ends all sessions;
  * with VIOLET_DASHBOARD_COOKIE_SECURE=1, a request that did not arrive over HTTPS is refused before any credential is
    read -- except a direct, unproxied connection from this machine (SSH tunnel). Forwarded headers are trusted only
    from a proxy on this machine (``trusted_proxy_app``); HTTPS responses carry Strict-Transport-Security.

Cookie-authenticated state-changing requests must also come from the same origin (Origin/Referer check, plus
SameSite=Strict cookies) to stop CSRF.

Violet stage 2 (1Panel-style access):
  * a USERNAME (admin.json "username"; random at install). Password login needs username + password; while no
    username is set (development, tests) any username is accepted, as before. The admin token still logs in alone;
  * a SECURITY ENTRANCE (admin.json "entrance", a random path like ``/k3x9q2m7wz8a``): while one is set, a browser must
    first open ``https://<host>:<port>/<entrance>`` -- that marks its session -- and every other path answers a plain
    404 until then (the login page, the Control Center, static files, the API). Exceptions: ``/healthz`` (the
    installer's checks; it only says that a Violet runs there), signed channel webhooks, and requests with a valid
    Bearer token. The mark survives logout and session expiry (the login page stays reachable in that browser);
    changing the entrance invalidates every mark. Shown by ``violet info``; changed in the Control Center or with
    ``violet reset-entrance``.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, redirect, render_template_string, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from ai.settings import env_bool, env_int

logger = logging.getLogger("violet.dashboard.auth")

TOKEN_FILE_NAME = "admin_token"
CONTROL_DIR_NAME = "control"
PUBLIC_PATHS = {"/auth/login", "/healthz"}
# phase 10: chat platforms post events here; the channel checks its own HMAC signature (and source address) first
PUBLIC_PREFIXES = ("/channels/hook/",)
# phase 10.3: all a session may reach while the initial password still has to be changed
PASSWORD_CHANGE_PATHS = {"/control", "/auth/password", "/auth/status", "/auth/logout", "/auth/logout-all"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_SESSION_KEY = "violet_admin"
_ENTRANCE_KEY = "violet_entrance"          # session mark: this browser came in through the security entrance
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,31}$")
ENTRANCE_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
# first path segments the program itself uses: never an entrance
RESERVED_ENTRANCES = {"auth", "static", "api", "control", "healthz", "channels", "memories", "persona", "images",
                      "web", "reconciliation", "proactive", "dashboard", "login", "logout", "reload-env", "shutdown",
                      "favicon.ico", "robots.txt"}
MIN_PASSWORD_LENGTH = 12
LOCK_AFTER_FAILURES = 5
LOCK_WINDOW_SECONDS = 15 * 60
GLOBAL_FAILURE_LIMIT = 50

_LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Violet Control Center 登录</title>
<style>
 body{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;background:#0f172a;color:#e2e8f0;
      display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0;padding:16px}
 form{background:#1e293b;padding:28px;border-radius:12px;width:100%;max-width:360px;box-sizing:border-box}
 h1{font-size:18px;margin:0 0 16px} input{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;
 border:1px solid #334155;background:#0f172a;color:#e2e8f0;margin-bottom:12px}
 button{width:100%;padding:10px;border:0;border-radius:8px;background:#6366f1;color:#fff;font-size:15px}
 .err{color:#f87171;font-size:13px;margin-bottom:12px} .hint{color:#94a3b8;font-size:12px;margin-top:12px}
</style></head><body>
<form method="post" action="/auth/login">
  <h1>Violet Control Center</h1>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <input type="text" name="username" placeholder="用户名" autocomplete="username" maxlength="32" autofocus>
  <input type="password" name="password" placeholder="密码" autocomplete="current-password" required>
  <input type="hidden" name="next" value="{{ next_url }}">
  <button type="submit">登录</button>
  <div class="hint">第一次登录请用安装完成时显示的用户名和初始密码，登录后需要先改成自己的密码。忘记了：在服务器上执行
  /opt/violet/violet info（查看用户名和地址）或 /opt/violet/violet reset-password（生成新的初始密码）。</div>
</form></body></html>"""

_NOT_FOUND = "404 Not Found\n"


def resolve_admin_token(data_dir: str | os.PathLike[str]) -> tuple[str, str]:
    """Return (token, source). source is "env" or the token file path."""
    env_token = os.environ.get("VIOLET_ADMIN_TOKEN", "").strip()
    if env_token:
        if len(env_token) < 16:
            logger.warning("VIOLET_ADMIN_TOKEN is shorter than 16 characters; use a long random value")
        return env_token, "env"
    path = Path(data_dir) / TOKEN_FILE_NAME
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token, str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token + "\n", encoding="utf-8")
    _restrict(path)
    logger.warning("generated a new dashboard admin token in %s (read it from that file to log in)", path)
    return token, str(path)


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{secrets.token_hex(4)}.tmp")
    tmp.write_text(text, encoding="utf-8")
    _restrict(tmp)
    os.replace(tmp, path)
    _restrict(path)


class AdminStore:
    """<data_dir>/control/admin.json: the password hash and the session version. Never the password itself."""

    def __init__(self, data_dir: str | os.PathLike[str]):
        self.dir = Path(data_dir) / CONTROL_DIR_NAME
        self.path = self.dir / "admin.json"
        self._lock = threading.Lock()

    def load(self) -> dict:
        """The stored settings. A file that exists but cannot be read fails CLOSED: token disabled, no password
        (fix it with ``violet_admin set-password`` / ``token-enable``), so a damaged file never re-enables a token."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            pass
        logger.error("dashboard admin settings %s are unreadable: token and password login disabled until "
                     "`python -m tools.violet_admin set-password` rewrites them", self.path)
        return {"token_mode": "disabled", "unreadable": True}

    def _save(self, data: dict) -> None:
        _atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=1))

    def has_password(self) -> bool:
        return bool(self.load().get("password_hash"))

    def verify_password(self, password: str) -> bool:
        stored = self.load().get("password_hash")
        return bool(stored and password) and check_password_hash(stored, password)

    def set_password(self, password: str, must_change: bool = False) -> None:
        """must_change (phase 10.3): an initial password made by the installer -- a browser session that logged in
        with it can do nothing but change it."""
        problem = password_problem(password)
        if problem:
            raise ValueError(problem)
        with self._lock:
            data = self.load()
            data.pop("unreadable", None)
            data.update(password_hash=generate_password_hash(password, method="scrypt"),
                        password_set_at=int(time.time()), session_version=int(data.get("session_version", 0)) + 1,
                        must_change=bool(must_change))
            self._save(data)

    def must_change(self) -> bool:
        return bool(self.load().get("must_change"))

    def session_version(self) -> int:
        return int(self.load().get("session_version", 0))

    def end_all_sessions(self) -> int:
        with self._lock:
            data = self.load()
            data.pop("unreadable", None)
            data["session_version"] = int(data.get("session_version", 0)) + 1
            self._save(data)
            return data["session_version"]

    # -- the admin token (phase 9.0.1)
    def token_mode(self) -> str:
        """default (VIOLET_ADMIN_TOKEN / admin_token file) | rotated (only the hash in admin.json) | disabled."""
        mode = self.load().get("token_mode") or "default"
        return mode if mode in ("default", "rotated", "disabled") else "disabled"

    def token_matches(self, supplied: str, configured: str) -> bool:
        if not supplied:
            return False
        data = self.load()
        mode = data.get("token_mode") or "default"
        if mode == "default":
            return hmac.compare_digest(supplied.encode("utf-8"), configured.encode("utf-8"))
        if mode == "rotated" and data.get("token_hash"):
            return hmac.compare_digest(_token_hash(supplied), str(data["token_hash"]))
        return False

    def _set_token(self, **fields) -> None:
        with self._lock:
            data = self.load()
            data.pop("unreadable", None)
            for key in ("token_mode", "token_hash"):
                data.pop(key, None)
            data.update({k: v for k, v in fields.items() if v is not None}, token_changed_at=int(time.time()),
                        session_version=int(data.get("session_version", 0)) + 1)
            self._save(data)

    def disable_token(self) -> None:
        if not self.has_password():
            raise ValueError("先用 set-password 设置管理员密码，否则禁用令牌后就无法登录")
        self._set_token(token_mode="disabled")

    def rotate_token(self) -> str:
        """A new random token, returned once; only its hash is stored. The old one stops working now."""
        new = secrets.token_urlsafe(32)
        self._set_token(token_mode="rotated", token_hash=_token_hash(new))
        return new

    def enable_default_token(self) -> None:
        self._set_token(token_mode="default")

    # -- username and security entrance (Violet stage 2)
    def username(self) -> str:
        """The admin's username; "" while none was set (then password login accepts any username, as before)."""
        return str(self.load().get("username") or "")

    def username_ok(self, supplied: str | None) -> bool:
        expected = self.username()
        if not expected:
            return True
        return hmac.compare_digest((supplied or "").strip().lower().encode("utf-8"), expected.lower().encode("utf-8"))

    def set_username(self, name: str) -> None:
        name = (name or "").strip()
        if not USERNAME_RE.fullmatch(name):
            raise ValueError("用户名要以字母开头，3–32 个字符，只能有字母、数字和 _ . -")
        with self._lock:
            data = self.load()
            data.pop("unreadable", None)
            data.update(username=name, session_version=int(data.get("session_version", 0)) + 1)
            self._save(data)

    def entrance(self) -> str:
        """The security entrance (a path segment without "/"); "" = none (every path reachable as before)."""
        value = str(self.load().get("entrance") or "")
        return value if ENTRANCE_RE.fullmatch(value) and value.lower() not in RESERVED_ENTRANCES else ""

    def set_entrance(self, value: str | None) -> str:
        """A new entrance (None = a random one; "" = no entrance). Every browser must come in through it again."""
        if value is None:
            value = generate_entrance()
        value = value.strip().strip("/")
        if value and (not ENTRANCE_RE.fullmatch(value) or value.lower() in RESERVED_ENTRANCES):
            raise ValueError("安全入口要 6–64 个字符，只能有字母、数字、_ 和 -，也不能是程序自己用的路径")
        with self._lock:
            data = self.load()
            data.pop("unreadable", None)
            data.update(entrance=value, entrance_changed_at=int(time.time()))
            self._save(data)
        return value

    def session_secret(self) -> bytes:
        path = self.dir / "session_secret"
        try:
            value = path.read_text(encoding="utf-8").strip()
            if len(value) >= 64:
                return bytes.fromhex(value)
        except (OSError, ValueError):
            pass
        value = secrets.token_hex(32)
        _atomic_write(path, value + "\n")
        return bytes.fromhex(value)


def generate_entrance(length: int = 12) -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"           # no 0/o/1/l: read aloud or typed from a screenshot
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if value not in RESERVED_ENTRANCES:
            return value


def generate_username() -> str:
    """A random username like 1Panel's: not "admin", so a password guesser also has to guess it."""
    return "violet_" + "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(6))


def _token_hash(token: str) -> str:
    # the token is 256 random bits, so a plain SHA-256 is enough (a password needs scrypt, a random token does not)
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def password_problem(password: str) -> str | None:
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return f"密码至少 {MIN_PASSWORD_LENGTH} 个字符"
    if len(set(password)) < 5:
        return "密码太单一了，换一个更难猜的"
    return None


class LoginLimiter:
    """Failed logins per client (and overall). In memory: a restart clears it, which is acceptable for one admin."""

    def __init__(self):
        self._lock = threading.Lock()
        self._fails: dict[str, deque] = {}
        self._all: deque = deque()

    def _trim(self, q: deque, now: float) -> None:
        while q and now - q[0] > LOCK_WINDOW_SECONDS:
            q.popleft()

    def locked_for(self, key: str) -> int:
        """Seconds until this client may try again (0 = not locked)."""
        now = time.monotonic()
        with self._lock:
            q = self._fails.get(key, deque())
            self._trim(q, now)
            self._trim(self._all, now)
            if len(q) >= LOCK_AFTER_FAILURES:
                return int(LOCK_WINDOW_SECONDS - (now - q[-1 - (LOCK_AFTER_FAILURES - 1)])) + 1
            if len(self._all) >= GLOBAL_FAILURE_LIMIT:
                return int(LOCK_WINDOW_SECONDS - (now - self._all[0])) + 1
            return 0

    def failed(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._fails.setdefault(key, deque()).append(now)
            self._all.append(now)

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


def _is_loopback(address: str | None) -> bool:
    try:
        return ipaddress.ip_address((address or "").split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def trusted_proxy_app(wsgi_app):
    """X-Forwarded-For / -Proto / -Host are honoured only when the connection itself comes from this machine (a
    reverse proxy on the same host). A remote client sending those headers keeps its real address and scheme, so it
    cannot dodge the login limits or pretend to be HTTPS."""
    from werkzeug.middleware.proxy_fix import ProxyFix
    fixed = ProxyFix(wsgi_app, x_for=1, x_proto=1, x_host=1)

    def app(environ, start_response):
        if _is_loopback(environ.get("REMOTE_ADDR")):
            return fixed(environ, start_response)
        return wsgi_app(environ, start_response)
    return app


def _fingerprint(token: str) -> str:
    return hashlib.sha256(("violet-admin:" + token).encode("utf-8")).hexdigest()[:32]


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def _same_origin() -> bool:
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source or source == "null":
        return False
    return urlsplit(source).netloc.lower() == (request.host or "").lower()


def _wants_html() -> bool:
    best = request.accept_mimetypes.best_match(["text/html", "application/json"])
    return best == "text/html" and request.accept_mimetypes[best] > request.accept_mimetypes["application/json"]


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")


def install_auth(app: Flask, token: str, data_dir: str | os.PathLike[str] | None = None) -> None:
    if not token:
        raise ValueError("dashboard admin token must not be empty")
    store = AdminStore(data_dir or os.environ.get("VIOLET_DATA_DIR") or "./data")
    limiter = LoginLimiter()
    app.extensions["violet_admin_store"] = store
    app.extensions["violet_login_limiter"] = limiter
    fingerprint = _fingerprint(token)
    app.secret_key = store.session_secret()
    idle = timedelta(minutes=max(5, env_int("VIOLET_DASHBOARD_IDLE_MINUTES", 120)))
    absolute = timedelta(hours=max(1, env_int("VIOLET_DASHBOARD_SESSION_HOURS", 12)))
    app.config.update(
        SESSION_COOKIE_NAME="violet_admin_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=env_bool("VIOLET_DASHBOARD_COOKIE_SECURE", False),
        PERMANENT_SESSION_LIFETIME=absolute,
    )

    require_https = bool(app.config["SESSION_COOKIE_SECURE"])

    def _token_ok(supplied: str) -> bool:
        return store.token_matches(supplied, token)

    def _bearer_ok() -> bool:
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return False
        return _token_ok(header[7:].strip())

    def _https_ok() -> bool:
        """With HTTPS required, only HTTPS requests -- or a direct local connection without forwarded headers
        (an SSH tunnel) -- get past this point. Proxied plain HTTP is refused before any password is read."""
        if not require_https or request.is_secure:
            return True
        forwarded = any(h in request.headers for h in ("X-Forwarded-For", "X-Forwarded-Proto", "Forwarded"))
        return _is_loopback(request.remote_addr) and not forwarded

    def _entrance_mark(entrance: str) -> str:
        # keyed with the session secret: the (signed, not encrypted) cookie must not reveal the entrance
        return hmac.new(app.secret_key, ("entrance:" + entrance).encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def _clear_session() -> None:
        """Log out, but keep "came in through the security entrance" so the login page stays reachable."""
        mark = session.get(_ENTRANCE_KEY)
        session.clear()
        if mark:
            session[_ENTRANCE_KEY] = mark

    def _session_ok() -> bool:
        if session.get(_SESSION_KEY) != fingerprint or session.get("v") != store.session_version():
            return False
        now = int(time.time())
        if now - int(session.get("iat", 0)) > absolute.total_seconds() or now - int(session.get("seen", 0)) > idle.total_seconds():
            _clear_session()
            return False
        if now - int(session.get("seen", 0)) > 60:              # refresh the idle clock at most once a minute
            session["seen"] = now
        return True

    def _start_session() -> None:
        _clear_session()
        now = int(time.time())
        session.update({_SESSION_KEY: fingerprint, "v": store.session_version(), "iat": now, "seen": now})
        session.permanent = True

    def _mark_entrance(entrance: str) -> None:
        session[_ENTRANCE_KEY] = _entrance_mark(entrance) if entrance else None

    def _entrance_gate():
        """None when the request may go on; a response (404, or the redirect after the entrance) otherwise."""
        entrance = store.entrance()
        if not entrance:
            return None
        if request.path.rstrip("/") == "/" + entrance:
            _mark_entrance(entrance)
            return redirect("/control" if _session_ok() else "/auth/login?next=/control")
        if session.get(_ENTRANCE_KEY) == _entrance_mark(entrance):
            return None
        return app.response_class(_NOT_FOUND, status=404, mimetype="text/plain")

    def _client_key() -> str:
        return request.remote_addr or "unknown"

    app.extensions["violet_start_session"] = _start_session

    @app.before_request
    def _require_admin():
        if not _https_ok():
            return jsonify({"error": "HTTPS required"}), 403
        if request.path == "/healthz":
            return None
        if request.method == "POST" and request.path.startswith(PUBLIC_PREFIXES) and \
                request.endpoint == "channel_webhook":
            return None
        if _bearer_ok():
            request.environ["violet.auth"] = "bearer"
            return None
        blocked = _entrance_gate()
        if blocked is not None:
            return blocked
        if request.endpoint == "static" or request.path in PUBLIC_PATHS:
            return None
        if _session_ok():
            if request.method not in SAFE_METHODS and not _same_origin():
                logger.warning("blocked cross-origin %s %s", request.method, request.path)
                return jsonify({"error": "cross-origin request rejected"}), 403
            request.environ["violet.auth"] = "session"
            if store.must_change() and request.path not in PASSWORD_CHANGE_PATHS:
                # phase 10.3: logged in with the installer's initial password -- change it first
                if request.method == "GET" and _wants_html():
                    return redirect("/control#account")
                return jsonify({"error": "password_change_required",
                                "message": "请先修改安装时生成的初始密码"}), 403
            return None
        if request.method == "GET" and _wants_html():
            return redirect(f"/auth/login?next={request.full_path.rstrip('?')}")
        return jsonify({"error": "unauthorized"}), 401

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Content-Security-Policy", CSP)
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        if request.is_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.get("/auth/login")
    def auth_login_page():
        return render_template_string(_LOGIN_PAGE, error=None, next_url=_safe_next(request.args.get("next")))

    @app.post("/auth/login")
    def auth_login():
        origin = request.headers.get("Origin")
        if origin and origin != "null" and urlsplit(origin).netloc.lower() != (request.host or "").lower():
            return jsonify({"error": "cross-origin request rejected"}), 403
        next_url = _safe_next(request.form.get("next"))
        key = _client_key()
        wait = limiter.locked_for(key)
        if wait:
            logger.warning("dashboard login locked for %s (%ss)", key, wait)
            return render_template_string(_LOGIN_PAGE, error=f"尝试次数太多，请 {wait // 60 + 1} 分钟后再试",
                                          next_url=next_url), 429
        supplied = (request.form.get("password") or request.form.get("token") or "").strip()
        # the admin token logs in on its own; the password needs the username too (both checked, no early exit)
        user_ok = store.username_ok(request.form.get("username"))
        ok = bool(supplied) and (_token_ok(supplied) or (store.verify_password(supplied) and user_ok))
        if ok:
            limiter.succeeded(key)
            _start_session()
            return redirect(next_url)
        limiter.failed(key)
        time.sleep(0.3)
        logger.warning("failed dashboard login from %s", key)
        return render_template_string(_LOGIN_PAGE, error="用户名或密码不正确", next_url=next_url), 401

    @app.post("/auth/logout")
    def auth_logout():
        _clear_session()
        return redirect("/auth/login")

    @app.post("/auth/logout-all")
    def auth_logout_all():
        store.end_all_sessions()
        _clear_session()
        return jsonify({"ok": True})

    @app.post("/auth/account")
    def auth_change_account():
        """Username and / or security entrance; the current password (or token) is asked again."""
        body = request.get_json(silent=True) or {}
        key = _client_key()
        if limiter.locked_for(key):
            return jsonify({"error": "尝试次数太多，请稍后再试"}), 429
        current = str(body.get("current") or "")
        if not (_token_ok(current) or store.verify_password(current)):
            limiter.failed(key)
            return jsonify({"error": "当前密码不正确"}), 403
        changed: dict = {}
        try:
            if "username" in body and str(body["username"]).strip() != store.username():
                store.set_username(str(body["username"]))          # ends every other session
                changed["username"] = store.username()
            if body.get("entrance_random") or "entrance" in body:
                new = None if body.get("entrance_random") else str(body.get("entrance") or "")
                if new is None or new.strip().strip("/") != store.entrance():
                    changed["entrance"] = store.set_entrance(new)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if "entrance" in changed:
            _mark_entrance(changed["entrance"])                      # this browser keeps its access
        if "username" in changed and request.environ.get("violet.auth") == "session":
            _start_session()
        if changed:
            logger.warning("dashboard account changed: %s", ", ".join(sorted(changed)))
        return jsonify({"ok": True, "changed": sorted(changed), "username": store.username(),
                        "entrance": store.entrance()})

    @app.post("/auth/password")
    def auth_change_password():
        body = request.get_json(silent=True) or {}
        current, new = str(body.get("current") or ""), str(body.get("new") or "")
        key = _client_key()
        if limiter.locked_for(key):
            return jsonify({"error": "尝试次数太多，请稍后再试"}), 429
        if not (_token_ok(current) or store.verify_password(current)):
            limiter.failed(key)
            return jsonify({"error": "当前密码或令牌不正确"}), 403
        if new != str(body.get("confirm") or ""):
            return jsonify({"error": "两次输入的新密码不一致"}), 400
        if store.must_change() and store.verify_password(new):
            return jsonify({"error": "新密码不能和安装时生成的初始密码一样"}), 400
        try:
            store.set_password(new)                      # also ends every other session
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if request.environ.get("violet.auth") == "session":
            _start_session()
        logger.warning("dashboard admin password changed")
        return jsonify({"ok": True})

    @app.get("/auth/status")
    def auth_status():
        data = store.load()
        return jsonify({"password_set": bool(data.get("password_hash")), "password_set_at": data.get("password_set_at"),
                        "must_change": bool(data.get("must_change")),
                        "via": request.environ.get("violet.auth"), "idle_minutes": int(idle.total_seconds() // 60),
                        "session_hours": int(absolute.total_seconds() // 3600),
                        "cookie_secure": bool(app.config["SESSION_COOKIE_SECURE"]),
                        "token_mode": store.token_mode(), "https": request.is_secure,
                        "username": store.username(), "entrance": store.entrance(),
                        "port": env_int("VIOLET_DASHBOARD_PORT", 0) or None})

    @app.get("/healthz")
    def healthz():
        # "service" lets the installer tell this Violet from another program on the same port (no version, no state)
        return jsonify({"ok": True, "service": "violet"})
