"""Search providers (phase 5B).

VIOLET_WEB_SEARCH_PROVIDER selects one:
  searxng  (default since phase 9.3 SearXNG switch) the self-hosted SearXNG at
           VIOLET_WEB_SEARXNG_URL (JSON API; deploy/searxng/). Without a URL -- or
           with an unusable one -- web_search answers "not configured".
           The URL is the ONE trusted internal address: plain http only for
           loopback, private / Docker addresses and Docker service names
           (single label, e.g. http://searxng:8080), https otherwise; no
           credentials, query or fragment in it. Only this provider's own
           requests may go there (no redirects); fetch_url never may.
           Optional credentials only from the environment at call time:
           VIOLET_WEB_SEARCH_AUTH_HEADER (header name, default Authorization) and
           VIOLET_WEB_SEARCH_API_KEY (value; sent as "Bearer <key>" for
           Authorization). Sent only to that endpoint, which may not redirect.
  bing     (0.7.4) Bing's RSS results (BingRssProvider): no key, nothing to install; works from mainland China
  none     no search backend: web_search answers "not configured";
           everything else keeps working
  mock     offline fictional results (tests / local development); never
           contacts the network
  bocha    (phase 9.3) Bocha Web Search API, POST JSON, "Authorization: Bearer <key>"; a mainland-China service
  brave    (phase 9.3) Brave Search API, GET, "X-Subscription-Token: <key>"
           For both: the key is the Control Center's replaced credential if set, else VIOLET_WEB_SEARCH_API_KEY; read
           at call time, sent only to the provider endpoint (VIOLET_WEB_SEARCH_ENDPOINT overrides the default URL),
           which may not redirect. Response shapes follow the providers' public documentation and are NOT yet
           verified against the real services (tools/README §24).

Other backends: implement SearchProvider and register_provider("name", factory).
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlencode, urlsplit

from ai.memory_model import _bigrams
from ai.settings import env_float, env_str
from ai.web.netguard import SafeFetcher, WebPolicyError


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    published_at: str | None = None
    engine: str | None = None
    source: str | None = None                  # phase 9.3: the site's own name when the provider gives one


class SearchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, *, recency_days: int | None = None, limit: int = 5) -> list[SearchResult]: ...


class DisabledSearchProvider:
    name = "none"
    reason = "no search provider configured (VIOLET_WEB_SEARCH_PROVIDER)"

    def __init__(self, reason: str | None = None):
        if reason:
            self.reason = reason

    def search(self, query, *, recency_days=None, limit=5):
        raise SearchError("not_configured", self.reason)


MOCK_DATA = [
    {"title": "秋季新书书评：一部关于小镇夏天的小说", "url": "https://books.example.org/review/summer-town",
     "snippet": "书评人认为这部小说节奏缓慢，适合在安静的晚上读。", "published_at": "2026-10-05",
     "keywords": ["小说", "书评", "新书", "读书", "书"]},
    {"title": "独立乐队发布新专辑《雾里的灯》", "url": "https://music.example.org/news/fog-lamp",
     "snippet": "这张专辑以钢琴和人声为主，乐评称其安静克制。", "published_at": "2026-10-08",
     "keywords": ["音乐", "专辑", "乐队", "乐评", "独立"]},
    {"title": "城市图书馆举办旧书交换活动", "url": "https://culture.example.org/events/book-swap",
     "snippet": "活动面向所有读者，时间为本月最后一个周末。", "published_at": "2026-09-28",
     "keywords": ["文化", "图书馆", "活动", "展览", "书"]},
    {"title": "爵士乐入门：十张值得听的专辑", "url": "https://music.example.org/guide/jazz-ten",
     "snippet": "一份面向新手的爵士乐听单。", "published_at": "2025-03-01",
     "keywords": ["爵士", "音乐", "专辑"]},
]


class MockSearchProvider:
    """Offline, deterministic, fictional. VIOLET_WEB_MOCK_DATA may point to a JSON list with the same fields."""
    name = "mock"

    def __init__(self, data: list[dict[str, Any]] | None = None):
        path = env_str("VIOLET_WEB_MOCK_DATA", "")
        if data is None and path:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.data = data if data is not None else MOCK_DATA
        self.calls: list[str] = []
        self.fail_with: SearchError | None = None
        self._lock = threading.Lock()

    def search(self, query, *, recency_days=None, limit=5):
        with self._lock:
            self.calls.append(query)
        if self.fail_with is not None:
            raise self.fail_with
        q = _bigrams(query)
        scored = []
        for item in self.data:
            text = item["title"] + item.get("snippet", "") + "".join(item.get("keywords", []))
            hits = len(q & _bigrams(text)) + sum(1 for k in item.get("keywords", []) if k in query) * 2
            if hits:
                scored.append((hits, item))
        scored.sort(key=lambda x: -x[0])
        return [SearchResult(i["title"], i["url"], i.get("snippet", ""), i.get("published_at"), "mock")
                for _, i in scored[:limit]]


class SearxngProvider:
    name = "searxng"
    TIME_RANGES = ((1, "day"), (7, "week"), (31, "month"), (366, "year"))

    def __init__(self, base_url: str, fetcher: SafeFetcher):
        self.base_url = base_url.rstrip("/")
        self.host = (urlsplit(self.base_url).hostname or "").lower()
        self.fetcher = fetcher

    def _headers(self) -> dict[str, str]:
        key = env_str("VIOLET_WEB_SEARCH_API_KEY", "")           # read at call time, never stored
        if not key:
            return {}
        header = env_str("VIOLET_WEB_SEARCH_AUTH_HEADER", "Authorization")
        return {header: f"Bearer {key}" if header.lower() == "authorization" else key}

    def search(self, query, *, recency_days=None, limit=5):
        params = {"q": query, "format": "json", "language": env_str("VIOLET_WEB_SEARCH_LANGUAGE", "zh-CN"),
                  "safesearch": "1"}
        if recency_days:
            params["time_range"] = next((name for days, name in self.TIME_RANGES if recency_days <= days), "year")
        url = f"{self.base_url}/search?{urlencode(params)}"
        try:
            result = self.fetcher.fetch(url, headers=self._headers(), trusted_host=self.host,
                                        accept_types=("application/json",))
            payload = json.loads(result.text())
        except WebPolicyError as exc:
            raise searxng_error(exc) from None
        except ValueError:
            raise SearchError("invalid", "search provider returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise SearchError("invalid", "search provider returned an unexpected shape")
        out = []
        for item in (payload.get("results") or [])[: max(1, limit) * 2]:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            out.append(SearchResult(str(item.get("title") or "")[:200], str(item["url"])[:2048],
                                    str(item.get("content") or "")[:500],
                                    str(item.get("publishedDate") or "") or None, str(item.get("engine") or "") or None))
        if not out and payload.get("unresponsive_engines"):
            # phase 9.3: no result because every engine failed (blocked, timed out) is a failure, not "nothing found"
            raise SearchError("provider_error", "searxng: no search engine answered")
        return out[:limit]


def searxng_error(exc: WebPolicyError) -> SearchError:
    """Phase 9.3: SearXNG failures Violet can tell apart. 403 = the JSON format is not enabled (search.formats) or the
    limiter refused; the container not running / not resolvable = unavailable."""
    status = getattr(exc, "status", None)
    if status == 403:
        return SearchError("provider_error", "searxng refused the request (JSON format off or limiter)")
    if status == 429:
        return SearchError("rate_limited", "searxng rate limit")
    if status is not None:
        return SearchError("provider_error", f"searxng http {status}")
    if exc.code == "timeout":
        return SearchError("timeout", "searxng timed out")
    if exc.code in ("connect", "dns", "tls"):
        return SearchError("unavailable", f"searxng is not reachable ({exc.code})")
    return SearchError(exc.code, str(exc))


SINGLE_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def searxng_url_problem(url: str) -> str | None:
    """Why VIOLET_WEB_SEARXNG_URL cannot be used as the trusted search address (None = it can)."""
    import ipaddress
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return "VIOLET_WEB_SEARXNG_URL is not a valid url"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "VIOLET_WEB_SEARXNG_URL must be http(s)://host[:port]"
    if parts.username or parts.password or "@" in parts.netloc:
        return "VIOLET_WEB_SEARXNG_URL must not contain credentials"
    if parts.query or parts.fragment:
        return "VIOLET_WEB_SEARXNG_URL must not contain a query or fragment"
    if port is not None and not 0 < port < 65536:
        return "VIOLET_WEB_SEARXNG_URL has an invalid port"
    if parts.scheme == "https":
        return None
    host = parts.hostname.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(host)
        local = ip.is_loopback or ip.is_private
    except ValueError:
        local = host == "localhost" or bool(SINGLE_LABEL.match(host))      # a Docker service name ("searxng")
    if not local:
        return "plain http is only allowed for a local / Docker-internal SearXNG; use https for other hosts"
    return None


def provider_key() -> str:
    """Phase 9.3: the Control Center's replaced search credential if there is one, else VIOLET_WEB_SEARCH_API_KEY."""
    try:
        from ai.tool_manager import tool_store
        stored = tool_store().web_search_key()
    except Exception:
        stored = ""
    return stored or env_str("VIOLET_WEB_SEARCH_API_KEY", "")


def search_error(exc: WebPolicyError) -> SearchError:
    """A network / HTTP failure of the provider as an error code Violet can tell apart."""
    status = getattr(exc, "status", None)
    if status in (401, 403):
        return SearchError("auth", "search provider refused the credential")
    if status == 429:
        return SearchError("rate_limited", "search provider rate limit")
    if exc.code == "timeout":
        return SearchError("timeout", "search provider timed out")
    if status is not None:
        return SearchError("provider_error", f"search provider http {status}")
    return SearchError(exc.code, str(exc))


class _ApiProvider:
    """A search API with a key: shared plumbing for bocha / brave (the endpoint is trusted, may not redirect)."""
    name = "api"
    default_endpoint = ""

    def __init__(self, fetcher: SafeFetcher, endpoint: str | None = None):
        self.endpoint = (endpoint or env_str("VIOLET_WEB_SEARCH_ENDPOINT", "") or self.default_endpoint).rstrip("/")
        self.host = (urlsplit(self.endpoint).hostname or "").lower()
        self.fetcher = fetcher

    def _call(self, url: str, headers: dict[str, str], body: dict | None = None) -> dict:
        key = provider_key()
        if not key:
            raise SearchError("not_configured", f"{self.name}: no search API key configured")
        try:
            result = self.fetcher.fetch(url, headers={**headers, **self._auth(key)}, trusted_host=self.host,
                                        accept_types=("application/json",), method="POST" if body is not None else "GET",
                                        body=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
            payload = json.loads(result.text())
        except WebPolicyError as exc:
            raise search_error(exc) from None
        except ValueError:
            raise SearchError("invalid", "search provider returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise SearchError("invalid", "search provider returned an unexpected shape")
        return payload

    def _auth(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}


class BochaProvider(_ApiProvider):
    name = "bocha"
    default_endpoint = "https://api.bochaai.com/v1/web-search"
    FRESHNESS = ((1, "oneDay"), (7, "oneWeek"), (31, "oneMonth"), (366, "oneYear"))

    def search(self, query, *, recency_days=None, limit=5):
        freshness = next((n for d, n in self.FRESHNESS if recency_days and recency_days <= d), "noLimit")
        payload = self._call(self.endpoint, {"Content-Type": "application/json"},
                             {"query": query, "freshness": freshness, "summary": False, "count": max(1, min(limit, 10))})
        if str(payload.get("code")) not in ("None", "200", ""):            # the docs do not fix int vs "200"
            try:
                code = int(payload.get("code"))
            except (TypeError, ValueError):
                code = 0
            raise SearchError("auth" if code in (401, 403) else "rate_limited" if code == 429 else "provider_error",
                              f"bocha code {code}")
        items = ((payload.get("data") or {}).get("webPages") or {}).get("value") or []
        return [SearchResult(str(i.get("name") or "")[:200], str(i["url"])[:2048],
                             str(i.get("snippet") or i.get("summary") or "")[:500],
                             str(i.get("datePublished") or "") or None, "bocha", str(i.get("siteName") or "") or None)
                for i in items if isinstance(i, dict) and i.get("url")][:limit]


class BraveProvider(_ApiProvider):
    name = "brave"
    default_endpoint = "https://api.search.brave.com/res/v1/web/search"
    FRESHNESS = ((1, "pd"), (7, "pw"), (31, "pm"), (366, "py"))

    def _auth(self, key: str) -> dict[str, str]:
        return {"X-Subscription-Token": key}

    def search(self, query, *, recency_days=None, limit=5):
        params = {"q": query, "count": max(1, min(limit, 20)), "search_lang": "zh-hans", "safesearch": "moderate"}
        freshness = next((n for d, n in self.FRESHNESS if recency_days and recency_days <= d), None)
        if freshness:
            params["freshness"] = freshness
        payload = self._call(f"{self.endpoint}?{urlencode(params)}", {})
        items = (payload.get("web") or {}).get("results") or []
        return [SearchResult(str(i.get("title") or "")[:200], str(i["url"])[:2048], str(i.get("description") or "")[:500],
                             str(i.get("page_age") or "") or None, "brave",
                             str((i.get("profile") or {}).get("name") or "") or None)
                for i in items if isinstance(i, dict) and i.get("url")][:limit]


class BingRssProvider:
    """0.7.4: Bing's own RSS search results (https://cn.bing.com/search?q=...&format=rss), no key, nothing to install.
    The user's server in mainland China: every SearXNG engine failed, but this answered (curl, 2026-10-28). Goes
    through the safe fetcher like any public page (public addresses only, redirects checked, size and time limits)
    with a browser User-Agent; VIOLET_WEB_BING_HOST picks cn.bing.com (default) or www.bing.com. Bing's terms allow
    these results for personal, non-commercial use -- which is what a self-hosted companion is."""
    name = "bing"
    HOSTS = ("cn.bing.com", "www.bing.com")
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

    def __init__(self, fetcher: SafeFetcher, host: str | None = None):
        chosen = (host or env_str("VIOLET_WEB_BING_HOST", "cn.bing.com")).strip().lower()
        self.host = chosen if chosen in self.HOSTS else "cn.bing.com"
        self.fetcher = fetcher

    def search(self, query, *, recency_days=None, limit=5):
        """The chosen address first, then the other one once (CI, 0.7.4: from a US cloud machine cn.bing.com answered
        with a web page; from mainland China both answer RSS as text/xml)."""
        other = next(h for h in self.HOSTS if h != self.host)
        error: SearchError | None = None
        for host in (self.host, other):
            try:
                return self._search(host, query, limit)
            except SearchError as exc:
                if exc.code == "timeout":                # the other address would only double the wait
                    raise
                error = exc
        raise error

    def _search(self, host, query, limit):
        import xml.etree.ElementTree as ET
        params = {"q": query, "format": "rss", "setlang": "zh-Hans", "mkt": "zh-CN"}
        url = f"https://{host}/search?{urlencode(params)}"
        try:
            # text/html too: what counts is that the body really is RSS (checked below), not the label
            result = self.fetcher.fetch(url, accept_types=("application/rss+xml", "text/xml", "application/xml",
                                                           "text/html"))
        except WebPolicyError as exc:
            raise search_error(exc) from None
        try:
            root = ET.fromstring(result.text())          # expat refuses entity expansion bombs; size is capped above
        except ET.ParseError:
            raise SearchError("blocked", f"{host} answered with a web page instead of results (a verification page?)") from None
        if root.tag != "rss":
            raise SearchError("blocked", f"{host} did not answer with RSS")
        out = []
        for item in root.iter("item"):
            link = (item.findtext("link") or "").strip()
            if not link.startswith(("https://", "http://")):
                continue
            out.append(SearchResult((item.findtext("title") or "").strip()[:200], link[:2048],
                                    (item.findtext("description") or "").strip()[:500],
                                    (item.findtext("pubDate") or "").strip() or None, "bing"))
        return out[:limit]


_REGISTRY: dict[str, Callable[[SafeFetcher], Any]] = {}


def register_provider(name: str, factory: Callable[[SafeFetcher], Any]) -> None:
    _REGISTRY[name] = factory


DEFAULT_PROVIDER = "searxng"                   # phase 9.3: the self-hosted SearXNG; bocha / brave stay optional


def provider_name() -> str:
    return (env_str("VIOLET_WEB_SEARCH_PROVIDER", DEFAULT_PROVIDER) or DEFAULT_PROVIDER).strip().lower()


def make_provider(fetcher: SafeFetcher) -> Any:
    name = provider_name()
    if name in _REGISTRY:
        return _REGISTRY[name](fetcher)
    if name == "mock":
        return MockSearchProvider()
    if name == "searxng":
        base = env_str("VIOLET_WEB_SEARXNG_URL", "")
        if not base:
            return DisabledSearchProvider("searxng: VIOLET_WEB_SEARXNG_URL is not set")
        problem = searxng_url_problem(base)
        if problem:
            return DisabledSearchProvider(f"searxng: {problem}")
        policy = replace(fetcher.policy, total_timeout=min(fetcher.policy.total_timeout,
                                                           env_float("VIOLET_WEB_SEARCH_TIMEOUT_SECONDS", 10.0)))
        provider_fetcher = SafeFetcher(policy, resolver=fetcher.resolver, dialer=fetcher.dialer,
                                       ssl_context=fetcher.ssl_context)
        return SearxngProvider(base, provider_fetcher)
    if name == "bing":
        policy = replace(fetcher.policy, user_agent=BingRssProvider.USER_AGENT,
                         total_timeout=min(fetcher.policy.total_timeout,
                                           env_float("VIOLET_WEB_SEARCH_TIMEOUT_SECONDS", 10.0)))
        return BingRssProvider(SafeFetcher(policy, resolver=fetcher.resolver, dialer=fetcher.dialer,
                                           ssl_context=fetcher.ssl_context))
    if name in ("bocha", "brave"):
        policy = replace(fetcher.policy, total_timeout=min(fetcher.policy.total_timeout,
                                                           env_float("VIOLET_WEB_SEARCH_TIMEOUT_SECONDS", 10.0)))
        provider_fetcher = SafeFetcher(policy, resolver=fetcher.resolver, dialer=fetcher.dialer,
                                       ssl_context=fetcher.ssl_context)
        return (BochaProvider if name == "bocha" else BraveProvider)(provider_fetcher)
    return DisabledSearchProvider()
