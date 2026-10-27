"""Code-level network policy for every outbound web request (phase 5B).

SafeFetcher is the only way Violet's web features talk to the network:

* http / https only, standard ports only (VIOLET_WEB_ALLOWED_PORTS, default 80,443);
  no credentials in URLs; no localhost / *.local / *.internal / single-label
  names; never the hosts Violet itself uses (LLM relay, MCP servers, dashboard,
  VIOLET_WEB_BLOCKED_HOSTS).
* Each hop is resolved ONCE; every resolved address must be public (not
  loopback, private, link-local -- which covers cloud metadata 169.254.169.254
  --, CGNAT, multicast, reserved, unspecified, IPv4-mapped / 6to4 / Teredo
  forms of those). The connection is then made to that exact, validated IP
  (pinned), so a second DNS answer can never redirect it (DNS rebinding).
  HTTPS keeps the real host name for SNI and certificate verification.
* Redirects are followed manually, at most max_redirects, and every hop is
  checked again from scratch.
* Proxy environment variables are never used (direct sockets), so a proxy
  cannot be used to bypass the address checks.
* Limits: connect timeout, per-read timeout, total deadline, response size
  (counted while reading; identity encoding only, so no decompression bombs),
  content-type allowlist.
* No cookies are sent or kept; no Authorization header, except provider
  credentials sent to the configured provider endpoint itself (trusted_host),
  which may not redirect.
* Errors and logs use redacted URLs (no query strings, no credentials).

The resolver and dialer are injectable for offline tests only (constructor
arguments, not configuration).
"""

from __future__ import annotations

import http.client
import ipaddress
import logging
import os
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from ai.mcp import redact_url
from ai.settings import env_float, env_int, env_list, env_str

logger = logging.getLogger("violet.web")

BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home", ".corp", ".home.arpa",
                    ".localdomain")
BLOCKED_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "metadata",
                 "metadata.google.internal", "instance-data"}
TEXT_TYPES = ("text/html", "text/plain", "application/xhtml+xml", "application/xml", "text/xml",
              "application/rss+xml", "application/atom+xml", "application/json")
REDIRECT_CODES = {301, 302, 303, 307, 308}
MAX_URL_CHARS = 2048


class WebPolicyError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status                     # phase 9.3: the HTTP status of a "status" error (401 / 429 / 5xx)


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    charset: str | None
    body: bytes
    ip: str
    redirects: list[str] = field(default_factory=list)

    def text(self) -> str:
        return self.body.decode(self.charset or "utf-8", errors="replace")


def _host_of(url: str | None) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def internal_hosts() -> set[str]:
    """Hosts Violet itself talks to -- never reachable through web tools."""
    # the search provider endpoint is reachable ONLY through its own trusted provider call, never via fetch_url
    hosts = {_host_of(env_str("LLM_API_URL", "")), env_str("VIOLET_DASHBOARD_HOST", "").lower(),
             _host_of(env_str("VIOLET_WEB_SEARXNG_URL", "")), _host_of(env_str("VIOLET_WEB_SEARCH_ENDPOINT", ""))}
    raw = env_str("MCP_SERVERS", "")
    if raw:
        try:
            import json
            for server in json.loads(raw) or []:
                if isinstance(server, dict):
                    hosts.add(_host_of(server.get("url")))
        except ValueError:
            pass
    hosts.update(h.lower().rstrip(".") for h in env_list("VIOLET_WEB_BLOCKED_HOSTS", []))
    return {h for h in hosts if h}


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None or ip.teredo is not None:
            return False
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or not ip.is_global):
        return False
    return True


def _default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def _default_dialer(ip: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((ip, port), timeout=timeout)


@dataclass
class NetPolicy:
    allowed_ports: set[int] = field(default_factory=lambda: {80, 443})
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    total_timeout: float = 20.0
    max_bytes: int = 2_000_000
    max_redirects: int = 3
    user_agent: str = "VioletWebReader/1.0 (+personal assistant; read-only)"
    blocked_hosts: set[str] = field(default_factory=set)

    @classmethod
    def from_env(cls) -> "NetPolicy":
        ports = {int(p) for p in env_list("VIOLET_WEB_ALLOWED_PORTS", ["80", "443"]) if str(p).isdigit()}
        return cls(
            allowed_ports=ports or {80, 443},
            connect_timeout=env_float("VIOLET_WEB_CONNECT_TIMEOUT_SECONDS", 5.0),
            read_timeout=env_float("VIOLET_WEB_READ_TIMEOUT_SECONDS", 10.0),
            total_timeout=env_float("VIOLET_WEB_TOTAL_TIMEOUT_SECONDS", 20.0),
            max_bytes=env_int("VIOLET_WEB_MAX_BYTES", 2_000_000),
            max_redirects=env_int("VIOLET_WEB_MAX_REDIRECTS", 3),
            blocked_hosts=internal_hosts(),
        )


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout, dialer):
        super().__init__(host, port, timeout=timeout)
        self._ip, self._dialer = ip, dialer

    def connect(self):
        self.sock = self._dialer(self._ip, self.port, self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout, dialer, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._ip, self._dialer, self._ctx = ip, dialer, context

    def connect(self):
        raw = self._dialer(self._ip, self.port, self.timeout)
        self.sock = self._ctx.wrap_socket(raw, server_hostname=self.host)


class SafeFetcher:
    def __init__(self, policy: NetPolicy | None = None, *, resolver: Callable[[str, int], list[str]] | None = None,
                 dialer: Callable[[str, int, float], socket.socket] | None = None,
                 ssl_context: ssl.SSLContext | None = None):
        self.policy = policy or NetPolicy.from_env()
        self.resolver = resolver or _default_resolver
        self.dialer = dialer or _default_dialer
        self.ssl_context = ssl_context or ssl.create_default_context()

    # -- checks ----------------------------------------------------------------------
    def check_url(self, url: str, trusted_host: str | None = None) -> tuple[str, str, int, str]:
        if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
            raise WebPolicyError("url", "invalid or too long url")
        try:
            parts = urlsplit(url.strip())
            port = parts.port
        except ValueError:
            raise WebPolicyError("url", "malformed url") from None
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise WebPolicyError("scheme", f"scheme {scheme or '?'} is not allowed")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise WebPolicyError("credentials", "credentials in urls are not allowed")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            raise WebPolicyError("host", "missing host")
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise WebPolicyError("host", "invalid host name") from None
        port = port or (443 if scheme == "https" else 80)
        is_trusted = trusted_host is not None and host == trusted_host
        if not is_trusted:
            if port not in self.policy.allowed_ports:
                raise WebPolicyError("port", f"port {port} is not allowed")
            if host in BLOCKED_NAMES or host.endswith(BLOCKED_SUFFIXES) or host in self.policy.blocked_hosts:
                raise WebPolicyError("host", "host is not allowed")
            try:
                ipaddress.ip_address(host.strip("[]"))
                literal = True
            except ValueError:
                literal = False
            if not literal and "." not in host:
                raise WebPolicyError("host", "single-label host names are not allowed")
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        return scheme, host, port, path

    def resolve(self, host: str, port: int, trusted: bool = False) -> str:
        try:
            addresses = self.resolver(host.strip("[]"), port)
        except (socket.gaierror, OSError) as exc:
            raise WebPolicyError("dns", f"cannot resolve host ({exc.__class__.__name__})") from None
        if not addresses:
            raise WebPolicyError("dns", "host has no address")
        if not trusted:
            bad = [a for a in addresses if not is_public_ip(a)]
            if bad:
                raise WebPolicyError("address", "host resolves to a non-public address")
        return addresses[0]          # pinned: the connection goes to exactly this checked address

    # -- fetch ---------------------------------------------------------------------------
    def fetch(self, url: str, *, headers: dict[str, str] | None = None, trusted_host: str | None = None,
              accept_types: tuple[str, ...] = TEXT_TYPES, method: str = "GET", body: bytes | None = None) -> FetchResult:
        policy = self.policy
        deadline = time.monotonic() + policy.total_timeout
        current = url
        redirects: list[str] = []
        for hop in range(policy.max_redirects + 1):
            scheme, host, port, path = self.check_url(current, trusted_host)
            trusted = trusted_host is not None and host == trusted_host
            ip = self.resolve(host, port, trusted)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebPolicyError("timeout", "total time limit reached")
            timeout = min(policy.connect_timeout, remaining)
            conn = (_PinnedHTTPS(host, port, ip, timeout, self.dialer, self.ssl_context) if scheme == "https"
                    else _PinnedHTTP(host, port, ip, timeout, self.dialer))
            request_headers = {
                "User-Agent": policy.user_agent,
                "Accept": ", ".join(accept_types) + ";q=0.9, */*;q=0.1",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
                "Accept-Encoding": "identity",
                "Connection": "close",
            }
            if trusted and headers:          # provider credentials only to the provider endpoint itself
                request_headers.update(headers)
            if method != "GET" and not trusted:
                # phase 9.3: only the configured search provider endpoint gets a POST (its JSON API); every hop of a
                # page is checked, so a POST never follows a redirect to another host
                raise WebPolicyError("method", "only GET is allowed for web pages")
            try:
                conn.request(method, path, body=body if method != "GET" else None, headers=request_headers)
                if conn.sock is not None:
                    conn.sock.settimeout(min(policy.read_timeout, max(0.05, deadline - time.monotonic())))
                response = conn.getresponse()
                if response.status in REDIRECT_CODES:
                    location = response.getheader("Location")
                    response.close()
                    if not location:
                        raise WebPolicyError("redirect", "redirect without location")
                    if trusted:
                        raise WebPolicyError("redirect", "the search provider endpoint may not redirect")
                    current = urljoin(current, location)
                    redirects.append(redact_url(current))
                    continue
                if response.status != 200:
                    response.close()
                    raise WebPolicyError("status", f"http status {response.status}", status=response.status)
                content_type = (response.getheader("Content-Type") or "").lower()
                mime = content_type.split(";", 1)[0].strip()
                if mime not in accept_types:
                    response.close()
                    raise WebPolicyError("content_type", f"content type {mime or '?'} is not allowed")
                charset = None
                if "charset=" in content_type:
                    charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip().strip('"') or None
                if (response.getheader("Content-Encoding") or "identity").lower() not in ("identity", ""):
                    response.close()
                    raise WebPolicyError("encoding", "compressed responses are not accepted")
                length = response.getheader("Content-Length")
                if length and length.isdigit() and int(length) > policy.max_bytes:
                    response.close()
                    raise WebPolicyError("size", "response too large")
                chunks, total = [], 0
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise WebPolicyError("timeout", "total time limit reached while reading")
                    if conn.sock is not None:
                        conn.sock.settimeout(min(policy.read_timeout, left))
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > policy.max_bytes:
                        raise WebPolicyError("size", "response too large")
                    chunks.append(chunk)
                return FetchResult(url=current, status=200, content_type=mime, charset=charset,
                                   body=b"".join(chunks), ip=ip, redirects=redirects)
            except WebPolicyError:
                raise
            except (socket.timeout, TimeoutError):
                raise WebPolicyError("timeout", "network timeout") from None
            except ssl.SSLError as exc:
                raise WebPolicyError("tls", f"tls error ({exc.__class__.__name__})") from None
            except (OSError, http.client.HTTPException) as exc:
                raise WebPolicyError("connect", f"network error ({exc.__class__.__name__})") from None
            finally:
                conn.close()
        raise WebPolicyError("redirect", f"more than {policy.max_redirects} redirects")


def proxy_env_present() -> bool:
    """For diagnostics only: proxies are never used by SafeFetcher."""
    return any(os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"))
