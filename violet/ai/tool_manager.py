"""Tool management (phase 9.2): which external tools Violet really has, and how a turn uses them.

It sits on top of ai/mcp.py (connections, allowlist, permissions, timeouts, circuit breaker, untrusted output) and
adds what was missing:

  * one catalog of the tools that are AVAILABLE now -- configured, enabled, allowlisted and listed by a server that
    answered -- for every agent (discover). A tool of a server that did not connect is never offered as available.
  * the plan of one user turn (plan_for_turn): Intent sees the catalog and may say the turn needs a tool
    (`tool_query` in its JSON). A read-only query is answered in Chat's own run -- no task is created; an execution
    tool goes to the Performer; a capability that does not exist / is switched off / is down / is not permitted is
    never turned into a task -- Chat is told which of these it is (notes below) and must not invent a result.
  * the Control Center's settings (data/control/tools.json, secrets in mcp_secrets.json): a server or a single tool
    switched off, which agents may use a server, timeouts, whether Chat gets a server's tools on every turn
    ("always") or only on a turn that needs them ("on_demand", the default), a replaced credential header. They are
    applied every time the server list is read (apply_tool_settings), so they act at once and survive a restart.

Violet's internal tools (memory, tasks, images) stay where they are -- nothing is moved into MCP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets as _secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("violet.tools")

AGENTS = ("chat", "performer", "proactive")
OFFER_MODES = ("on_demand", "always")
SECRET_HEADER_RE = re.compile(r"^(authorization|x-[a-z0-9-]{1,40})$", re.I)


# ----------------------------------------------------------------------------------------------- settings

class ToolSettingsStore:
    """<data_dir>/control/tools.json (no secrets) and mcp_secrets.json (header values, never returned). Every change
    is appended to control/tools_history.jsonl (who, what, old -> new; credentials only as fingerprints)."""

    def history(self, limit: int = 50) -> list[dict]:
        if not self.history_path.exists():
            return []
        rows = []
        for line in self.history_path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows[-limit:][::-1]

    _lock = threading.RLock()

    def __init__(self, data_dir: str | os.PathLike[str] | None = None):
        self.dir = Path(data_dir or os.environ.get("VIOLET_DATA_DIR") or "./data") / "control"
        self.path = self.dir / "tools.json"
        self.secrets_path = self.dir / "mcp_secrets.json"
        self.history_path = self.dir / "tools_history.jsonl"     # apart from history.jsonl (settings rollback)

    def _read(self, path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.error("tool settings file %s is unreadable; built-in defaults stay in effect", path)
            return {}

    def _write(self, path: Path, data: dict, secret: bool = False) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{_secrets.token_hex(4)}.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            if secret:
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def servers(self) -> dict[str, dict]:
        return dict(self._read(self.path).get("servers") or {})

    def secret_headers(self) -> dict[str, dict[str, str]]:
        return dict(self._read(self.secrets_path).get("servers") or {})

    def _log(self, actor: str, kind: str, server: str, changes: dict) -> None:
        entry = {"at": int(time.time()), "actor": actor, "type": "tools", "kind": kind, "server": server,
                 "changes": changes}
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def save_server(self, name: str, changes: dict[str, Any], actor: str = "dashboard") -> dict:
        cleaned = validate_server_changes(changes)
        with self._lock:
            doc = self._read(self.path)
            servers = dict(doc.get("servers") or {})
            entry = dict(servers.get(name) or {})
            old = {k: entry.get(k) for k in cleaned}
            for key, value in cleaned.items():
                if value is None:
                    entry.pop(key, None)
                else:
                    entry[key] = value
            servers[name] = entry
            self._write(self.path, {**doc, "servers": servers, "updated_at": int(time.time())})
            self._log(actor, "server", name, {k: {"old": old[k], "new": cleaned[k]} for k in cleaned})
            return entry

    def set_tool(self, name: str, tool: str, enabled: bool, actor: str = "dashboard") -> dict:
        if not re.match(r"^[A-Za-z0-9_.:-]{1,80}$", tool or ""):
            raise ValueError("工具名不对")
        with self._lock:
            doc = self._read(self.path)
            servers = dict(doc.get("servers") or {})
            entry = dict(servers.get(name) or {})
            off = set(entry.get("disabled_tools") or [])
            (off.discard if enabled else off.add)(tool)
            entry["disabled_tools"] = sorted(off)
            servers[name] = entry
            self._write(self.path, {**doc, "servers": servers, "updated_at": int(time.time())})
            self._log(actor, "tool", name, {"tool": tool, "enabled": bool(enabled)})
            return entry

    def replace_secret(self, name: str, header: str, value: str | None, actor: str = "dashboard") -> dict:
        """Replace (or with None remove) one credential header. The value is never returned or logged."""
        if not SECRET_HEADER_RE.match(header or ""):
            raise ValueError("只能替换 Authorization 或 X-… 头")
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 4096
                                  or any(c in value for c in "\r\n")):
            raise ValueError("凭据不能为空、不能换行、不能超过 4096 字")
        with self._lock:
            doc = self._read(self.secrets_path)
            servers = dict(doc.get("servers") or {})
            headers = dict(servers.get(name) or {})
            old = headers.get(header)
            if value is None:
                headers.pop(header, None)
            else:
                headers[header] = value.strip()
            servers[name] = headers
            self._write(self.secrets_path, {**doc, "servers": servers}, secret=True)
            self._log(actor, "secret", name, {"header": header, "old": fingerprint(old), "new": fingerprint(value)})
            return {"header": header, "is_set": value is not None, "fingerprint": fingerprint(value)}

    # -- phase 10.5: servers added in the Control Center, the admin's per-tool decisions ---------------------
    def custom_servers(self) -> list[dict[str, Any]]:
        return list(self._read(self.path).get("custom_servers") or [])

    def save_custom_server(self, name: str, entry: dict[str, Any], actor: str = "dashboard") -> dict:
        with self._lock:
            doc = self._read(self.path)
            servers = [s for s in doc.get("custom_servers") or [] if s.get("name") != name]
            old = next((s for s in doc.get("custom_servers") or [] if s.get("name") == name), None)
            entry = {**entry, "name": name}
            servers.append(entry)
            self._write(self.path, {**doc, "custom_servers": servers, "updated_at": int(time.time())})
            self._log(actor, "custom_server", name, {"old": old, "new": entry})
            return entry

    def remove_custom_server(self, name: str, actor: str = "dashboard") -> None:
        with self._lock:
            doc = self._read(self.path)
            servers = [s for s in doc.get("custom_servers") or [] if s.get("name") != name]
            overrides = dict(doc.get("servers") or {})
            overrides.pop(name, None)
            self._write(self.path, {**doc, "custom_servers": servers, "servers": overrides,
                                    "updated_at": int(time.time())})
            sdoc = self._read(self.secrets_path)
            stored = dict(sdoc.get("servers") or {})
            if stored.pop(name, None) is not None:
                self._write(self.secrets_path, {**sdoc, "servers": stored}, secret=True)
            self._log(actor, "custom_server_removed", name, {})

    def _update_entry(self, name: str, kind: str, change, actor: str, record: dict) -> dict:
        with self._lock:
            doc = self._read(self.path)
            servers = dict(doc.get("servers") or {})
            entry = dict(servers.get(name) or {})
            change(entry)
            servers[name] = entry
            self._write(self.path, {**doc, "servers": servers, "updated_at": int(time.time())})
            self._log(actor, kind, name, record)
            return entry

    def set_tool_level(self, name: str, tool: str, level: str | None, actor: str = "dashboard") -> dict:
        """The admin's classification of one tool: read / write / high_risk / deny (None = back to the template)."""
        if not re.match(r"^[A-Za-z0-9_.:-]{1,80}$", tool or ""):
            raise ValueError("工具名不对")

        def change(entry):
            levels = dict(entry.get("tool_levels") or {})
            if level is None:
                levels.pop(tool, None)
            else:
                levels[tool] = level
            entry["tool_levels"] = levels
            if level not in ("write", "high_risk"):
                entry["allow_tools"] = [t for t in entry.get("allow_tools") or [] if t != tool]
        return self._update_entry(name, "tool_level", change, actor, {"tool": tool, "level": level})

    def set_allow_tool(self, name: str, tool: str, allowed: bool, actor: str = "dashboard") -> dict:
        def change(entry):
            allow = set(entry.get("allow_tools") or [])
            (allow.add if allowed else allow.discard)(tool)
            entry["allow_tools"] = sorted(allow)
        return self._update_entry(name, "allow_tool", change, actor, {"tool": tool, "allowed": bool(allowed)})

    def set_tool_profile(self, name: str, profile: str | None, actor: str = "dashboard") -> dict:
        def change(entry):
            if profile:
                entry["tool_profile"] = profile
            else:
                entry.pop("tool_profile", None)
        return self._update_entry(name, "tool_profile", change, actor, {"tool_profile": profile})

    # -- phase 9.3: the built-in web tools (violet-web) -----------------------------------------------------
    def web_search_key(self) -> str:
        """The search provider credential replaced in the Control Center ("" = none; .env applies)."""
        return str((self._read(self.secrets_path).get("web") or {}).get("search_api_key") or "")

    def replace_web_key(self, value: str | None, actor: str = "dashboard") -> dict:
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 4096
                                  or any(c in value for c in "\r\n")):
            raise ValueError("凭据不能为空、不能换行、不能超过 4096 字")
        with self._lock:
            doc = self._read(self.secrets_path)
            web = dict(doc.get("web") or {})
            old = web.get("search_api_key")
            if value is None:
                web.pop("search_api_key", None)
            else:
                web["search_api_key"] = value.strip()
            self._write(self.secrets_path, {**doc, "web": web}, secret=True)
            self._log(actor, "web_secret", WEB_SERVER, {"old": fingerprint(old), "new": fingerprint(value)})
            return {"is_set": value is not None, "fingerprint": fingerprint(value)}

    def web_settings(self) -> dict[str, Any]:
        return dict((self.servers().get(WEB_SERVER) or {}).get("web") or {})

    def save_web_settings(self, changes: dict[str, Any], actor: str = "dashboard") -> dict:
        cleaned = validate_web_changes(changes)
        with self._lock:
            doc = self._read(self.path)
            servers = dict(doc.get("servers") or {})
            entry = dict(servers.get(WEB_SERVER) or {})
            web = dict(entry.get("web") or {})
            old = {k: web.get(k) for k in cleaned}
            for key, value in cleaned.items():
                if value is None:
                    web.pop(key, None)
                else:
                    web[key] = value
            entry["web"] = web
            servers[WEB_SERVER] = entry
            self._write(self.path, {**doc, "servers": servers, "updated_at": int(time.time())})
            self._log(actor, "web", WEB_SERVER, {k: {"old": old[k], "new": cleaned[k]} for k in cleaned})
            return web


WEB_SERVER = "violet-web"
# phase 9.3: what the dashboard may change for the web tools (the provider, its endpoint and the network policy stay
# in .env: they decide where requests go)
WEB_LIMITS = {"max_results": (1, 10), "max_searches_per_day": (0, 1000), "max_fetches_per_day": (0, 1000),
              "max_text_chars": (500, 8000)}


def web_status(db) -> dict[str, Any]:
    """Phase 9.3: the web tools for the Control Center -- provider, whether search can work, the credential (only
    where it comes from and a fingerprint), settings, today's use and failures (no queries or URLs)."""
    from ai.settings import env_bool, env_str
    from ai.web.findings import FindingsStore
    from ai.web.providers import provider_name, searxng_url_problem
    from ai.web.service import WebConfig
    provider = provider_name()
    stored, from_env = tool_store().web_search_key(), env_str("VIOLET_WEB_SEARCH_API_KEY", "")
    problem = None
    if provider == "searxng":
        url = env_str("VIOLET_WEB_SEARXNG_URL", "")
        problem = "VIOLET_WEB_SEARXNG_URL 没有设置" if not url else searxng_url_problem(url)
    configured = (provider in ("mock", "bing") or (provider == "searxng" and problem is None)
                  or (provider in ("bocha", "brave") and bool(stored or from_env)))
    cfg, store = WebConfig.from_env(), FindingsStore(db)
    day = db.now()[:10]
    failures = db.fetch_all("SELECT kind, status, error_code, created_at FROM web_requests WHERE created_at >= ? "
                            "AND status IN ('error', 'blocked', 'budget') ORDER BY id DESC LIMIT 20", (day,))
    off = set((tool_store().servers().get(WEB_SERVER) or {}).get("disabled_tools") or [])
    enabled = env_bool("VIOLET_WEB_ENABLED", False)
    return {"enabled": enabled, "provider": provider, "search_configured": configured, "search_problem": problem,
            "search_tool": enabled and "web_search" not in off, "fetch_tool": enabled and "fetch_url" not in off,
            "credential": {"source": "dashboard" if stored else "env" if from_env else None,
                           "fingerprint": fingerprint(stored or from_env)},
            "settings": {"max_results": cfg.max_results, "max_searches_per_day": cfg.max_searches_per_day,
                         "max_fetches_per_day": cfg.max_fetches_per_day, "max_text_chars": cfg.max_text_chars},
            "limits": {k: list(v) for k, v in WEB_LIMITS.items()},
            "used_today": {"search": store.count_today("search"), "fetch": store.count_today("fetch")},
            "recent_failures": failures}


def validate_web_changes(changes: dict[str, Any]) -> dict[str, Any]:
    out, problems = {}, {}
    for key, value in (changes or {}).items():
        if key not in WEB_LIMITS:
            problems[key] = "这一项不能在后台修改"
            continue
        if value is None:
            out[key] = None
            continue
        low, high = WEB_LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            problems[key] = f"要填 {low}–{high} 的整数"
            continue
        out[key] = value
    if problems:
        raise ValueError(json.dumps(problems, ensure_ascii=False))
    return out


def fingerprint(value: str | None) -> str:
    if not value:
        return ""
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def validate_server_changes(changes: dict[str, Any]) -> dict[str, Any]:
    """Only these fields may be changed from the dashboard -- never the url, the command or the transport."""
    out, problems = {}, {}
    for key, value in (changes or {}).items():
        if key == "enabled":
            if not isinstance(value, bool):
                problems[key] = "要用 true / false"
            out[key] = value
        elif key == "agents":
            if value is not None and (not isinstance(value, list) or any(a not in AGENTS for a in value)):
                problems[key] = f"只能是 {', '.join(AGENTS)} 中的几个"
            out[key] = value
        elif key in ("connect_timeout", "call_timeout"):
            if value is not None:
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    problems[key] = "要填数字"
                else:
                    limit = 60 if key == "connect_timeout" else 300
                    if not 1 <= value <= limit:
                        problems[key] = f"范围 1–{limit} 秒"
            out[key] = value
        elif key == "offer":
            if value is not None and value not in OFFER_MODES:
                problems[key] = "只能是 on_demand 或 always"
            out[key] = value
        else:
            problems[key] = "这一项不能在后台修改"
    if problems:
        raise ValueError(json.dumps(problems, ensure_ascii=False))
    return out


_STORE: ToolSettingsStore | None = None


def tool_store() -> ToolSettingsStore:
    global _STORE
    data_dir = os.environ.get("VIOLET_DATA_DIR") or "./data"
    if _STORE is None or Path(data_dir) / "control" != _STORE.dir:
        _STORE = ToolSettingsStore(data_dir)
    return _STORE


def apply_tool_settings(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The server configs with the Control Center's settings on top. Never raises (a broken file = no overrides)."""
    try:
        store = tool_store()
        overrides, secrets = store.servers(), store.secret_headers()
    except Exception:
        logger.exception("tool settings unreadable; using the configuration as it is")
        return servers
    out = []
    for server in servers:
        if not isinstance(server, dict):
            out.append(server)
            continue
        name = str(server.get("name") or "")
        merged = dict(server)
        for key, value in (overrides.get(name) or {}).items():
            if key in ("enabled", "agents", "connect_timeout", "call_timeout", "offer"):
                merged[key] = value
            elif key == "disabled_tools":
                merged["disabled_tools"] = sorted(set(merged.get("disabled_tools") or []) | set(value or []))
            elif key == "tool_profile" and merged.get("transport") != "inprocess":
                merged["tool_profile"] = value               # phase 10.5: the template chosen in the Control Center
            elif key == "tool_levels" and isinstance(value, dict):
                # phase 10.5: the admin's classifications go on top of the template / config allowlist; "deny" hides
                # the tool like a switched-off one. Unclassified tools stay refused (no default level exists).
                levels = {**(merged.get("tools") or {})}
                for tool, level in value.items():
                    if level == "deny":
                        levels.pop(tool, None)
                        merged["disabled_tools"] = sorted(set(merged.get("disabled_tools") or []) | {tool})
                    elif level in ("read", "write", "high_risk"):
                        levels[tool] = level
                if levels:
                    # keep the template's other classifications: the policy entry is read only when "tools" is empty
                    from ai.mcp import _policy_tools
                    merged["tools"] = {**_policy_tools(merged, name), **levels}
            elif key == "allow_tools":
                merged["allow_tools"] = sorted(set(merged.get("allow_tools") or []) | set(value or []))
        if secrets.get(name) and merged.get("transport") not in ("inprocess", "stdio"):
            merged["headers"] = {**(server.get("headers") or {}), **secrets[name]}
        out.append(merged)
    return out


# ----------------------------------------------------------------------------------------------- catalog

@dataclass
class ToolInfo:
    name: str                       # server__tool, the name the model calls
    server: str
    tool: str
    description: str
    permission: str
    agents: list[str]
    input_schema: dict[str, Any] = field(default_factory=dict)
    needs_approval: bool = False            # 2026-10-30: every call waits for the owner (ai/permissions.py)

    def short(self) -> str:
        first = re.split(r"(?<=[。.!！?？\n])", (self.description or "").strip(), maxsplit=1)[0].strip()
        return (first or self.tool)[:80]


@dataclass
class Catalog:
    configured: bool = False                                     # any MCP server configured at all
    tools: dict[str, ToolInfo] = field(default_factory=dict)     # available now
    unavailable: list[dict[str, Any]] = field(default_factory=list)   # servers that did not connect
    disabled: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)             # not allowlisted / write without second key
    known_tools: dict[str, str] = field(default_factory=dict)    # server__tool of a server that is down -> server

    def for_agent(self, agent: str) -> list[ToolInfo]:
        return [t for t in self.tools.values() if agent in t.agents]

    def status_of(self, name: str) -> str:
        if name in self.tools:
            return "ok"
        if name in self.disabled:
            return "disabled"
        if name in self.refused:
            return "permission_denied"
        server = self.known_tools.get(name) or name.split("__", 1)[0]
        if any(u["server"] == server for u in self.unavailable):
            return "unavailable"
        return "not_configured"


def _run_sync(factory):
    """asyncio.run(factory()) -- in a helper thread when this thread already runs an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    import contextvars
    box: dict[str, Any] = {}
    context = contextvars.copy_context()           # the turn's set of failed servers travels with it

    def target():
        try:
            box["value"] = context.run(lambda: asyncio.run(factory()))
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _agents_of(server: dict[str, Any]) -> list[str]:
    from ai.mcp import server_applies_to_agent
    return [a for a in AGENTS if server_applies_to_agent(server, a)]


def discover(servers: list[dict[str, Any]] | None = None) -> Catalog:
    """What is available now. Uses the tool cache of ai.mcp (no connection while it is fresh), the circuit breaker
    and this turn's failures; a server that cannot be reached costs at most its connect timeout. Never raises."""
    if servers is None:
        from ai.chat import get_mcp_servers
        servers = get_mcp_servers()
    servers = [s for s in servers if isinstance(s, dict) and s.get("enabled", True) is not False]
    catalog = Catalog(configured=bool(servers))
    if not servers:
        return catalog
    from ai.mcp import MCPToolBridge, _server_name, _state_for, _policy_tools

    async def run() -> MCPToolBridge:
        bridge = MCPToolBridge(servers, agent_name=None)
        try:
            await bridge.connect()
        finally:
            await bridge.close()
        return bridge

    try:
        bridge = _run_sync(run)
    except Exception:
        logger.exception("tool discovery failed; no external tool is offered this turn")
        catalog.unavailable = [{"server": _server_name(s, i), "error_code": "MCP_CONNECT_FAILED"}
                               for i, s in enumerate(servers)]
        return catalog
    by_name = {_server_name(s, i): (i, s) for i, s in enumerate(servers)}
    for full, binding in bridge.tool_to_session.items():
        index, server = by_name[binding.server_name]
        cached = {t["name"]: t for t in (_state_for(server, index).tools or [])}.get(binding.original_name) or {}
        catalog.tools[full] = ToolInfo(full, binding.server_name, binding.original_name, cached.get("description") or "",
                                       binding.permission, _agents_of(server), cached.get("inputSchema") or {},
                                       bool(binding.extra.get("needs_approval")))
    catalog.unavailable = list(bridge.unavailable)
    catalog.disabled = list(bridge.disabled_tools)
    catalog.refused = list(bridge.denied_tools) + list(bridge.unclassified_tools)
    for u in catalog.unavailable:
        index, server = by_name.get(u["server"], (None, None))
        if server is None:
            continue
        # phase 9.2.1: also the server's own allowlist ("tools" / "tool_permissions"), not only mcp_tool_policy.json --
        # without names Intent cannot point at the tool and a server that is down reads as "not configured"
        names = ([t["name"] for t in (_state_for(server, index).tools or [])]
                 or list(server.get("tools") or server.get("tool_permissions") or {})
                 or list(_policy_tools(server, u["server"])))
        for tool in names:
            catalog.known_tools[f"{u['server']}__{tool}"] = u["server"]
    return catalog


# ----------------------------------------------------------------------------------------------- one turn

def intent_note(catalog: Catalog) -> str | None:
    """What Intent is told about tools this turn (None when no MCP server is configured: nothing changes)."""
    if not catalog.configured:
        return None
    lines = ["【Violet 现在能用的外部工具（系统根据连接状态列出；没列出的就是现在没有）】"]
    usable = [t for t in catalog.tools.values() if {"chat", "performer"} & set(t.agents)]
    direct = set(direct_tools(catalog))
    for t in sorted(usable, key=lambda t: t.name):
        kind = ("只读查询" if t.permission == "read" else
                "会改动东西，聊天时直接调用，不需要 route=task" if t.name in direct else "执行操作，需要交给任务执行")
        lines.append(f"- {t.name}：{t.short()}（{kind}）")
    if not usable:
        lines.append("- （无）")
    for u in catalog.unavailable:
        # phase 9.2.1: full names, as Intent must write them in tool_query (a bare name would read as "not configured")
        tools = [n for n, s in catalog.known_tools.items() if s == u["server"]]
        lines.append(f"- 暂时连不上：{u['server']}" + (f"（{('、'.join(tools[:12]))} 这次用不了）" if tools else ""))
    for name in sorted(catalog.disabled):
        # phase 9.2.1 (p92-tools-1 #9): an unlisted switched-off tool could only be reported as "not configured"
        lines.append(f"- 已停用：{name}（管理员关掉了，这次用不了）")
    lines.append(
        '如果回答这句话需要用工具才能拿到的信息（实时信息、外部资料、用户自己的数据源，比如课表、天气、手机状态），在输出的 '
        'JSON 里加 "tool_query": {"needed": true, "tools": ["上面的工具名"], "reason": "要查什么"}；可能合适的有几个就都写上。'
        '合适的工具连不上或已停用，也照样写它的名字（系统会说明这次用不了）。'
        '需要外部能力，但上面根本没有合适的，加 "tool_query": {"needed": true, "tools": [], "missing": "需要的能力，例如 天气"}。'
        "普通聊天、情绪、回忆、关于 Violet 或用户自己说过的事，都不要加 tool_query。只读查询不需要 route=task。")
    return "\n".join(lines)


def plan_for_turn(intent: dict[str, Any], catalog: Catalog) -> dict[str, Any]:
    """mode none (no tool needed) | chat (read-only query answered in Chat's run) | performer (an execution tool,
    a task) | blocked (needed, but nothing usable: status says why). Never creates anything itself."""
    query = intent.get("tool_query") if isinstance(intent.get("tool_query"), dict) else None
    new_task = intent.get("route") == "task" and intent.get("active_task_id") is None
    direct = set(direct_tools(catalog))
    if not query or not query.get("needed"):
        if new_task and direct:
            # 2026-10-30 (free tool use): Chat has the tools itself and decides in its own run -- no task, no Performer
            return {"needed": False, "mode": "direct", "tools": [], "status": "ok", "reason": None, "missing": None}
        if new_task and not catalog.for_agent("performer"):
            # phase 9.2: the Performer has no external tool that could carry this out -- no task that can only fail
            return {"needed": False, "mode": "blocked", "status": "no_executor", "tools": [],
                    "missing": (intent.get("task") or {}).get("name"), "reason": None}
        return {"needed": False, "mode": "none", "tools": []}
    names = [str(n) for n in query.get("tools") or [] if isinstance(n, str)][:6]
    reason = str(query.get("reason") or query.get("missing") or "")[:120] or None
    chat = [n for n in names if n in catalog.tools and "chat" in catalog.tools[n].agents
            and (catalog.tools[n].permission == "read" or n in direct)]
    if chat:
        return {"needed": True, "mode": "chat", "status": "ok", "tools": chat, "reason": reason, "missing": None}
    performer = [n for n in names if n in catalog.tools and "performer" in catalog.tools[n].agents]
    if performer:
        return {"needed": True, "mode": "performer", "status": "ok", "tools": performer, "reason": reason, "missing": None}
    statuses = [catalog.status_of(n) for n in names]
    for status in ("unavailable", "disabled", "permission_denied"):
        if status in statuses:
            break
    else:
        status = "not_configured"
    return {"needed": True, "mode": "blocked", "status": status, "tools": names, "reason": reason,
            "missing": str(query.get("missing") or reason or "")[:60] or None}


def direct_tools(catalog: Catalog) -> list[str]:
    """2026-10-30: the tools Chat gets on every turn (VIOLET_CHAT_TOOLS_DIRECT, on): connected and assigned to Chat.
    Read-only ones always; ones that change something too (VIOLET_CHAT_ACTION_TOOLS, on -- user: "比较自由调用工具"):
    they are in the catalog only when allowed one by one or waiting for the owner's approval (ai/permissions.py)."""
    from ai.settings import env_bool
    if not env_bool("VIOLET_CHAT_TOOLS_DIRECT", True):
        return []
    actions = env_bool("VIOLET_CHAT_ACTION_TOOLS", True)
    return sorted(n for n, t in catalog.tools.items() if "chat" in t.agents and (t.permission == "read" or actions))


def direct_note(names: list[str]) -> str | None:
    """The rule that goes with them (the tool descriptions themselves come with the tool schemas)."""
    if not names:
        return None
    return ("【工具怎么用】这一轮你可以自己决定调用工具，不用等对方明说：需要实时信息、你不知道的外部资料、对方自己的数据，"
            "或者对方想让你做一件工具能做的事，就直接调用合适的工具，可以连着用几个，再按结果回答。闲聊、情绪、你们自己聊过的事不用查。"
            "会改动东西的操作，结果说要主人同意时就照它说的问主人。查不到或出错就照实说，绝不编造；工具名和错误代码不要说给对方听。")


def abilities_note(catalog: Catalog, direct: list[str] | None = None) -> str:
    """2026-10-30 (user: she says she did something she cannot do, or cannot use what she has): ONE list, written by
    the code from the real switches and connections, of what she can do in this chat, how, and what not (and why).
    Facts only -- no style."""
    from ai.settings import env_bool, env_str
    lines = ["【你现在能做的事】（系统按现在的设置和连接情况列出；没列出的就是做不到，不要说做了）"]
    try:
        from ai import stickers
        from ai.chat import violetdb
        if stickers.enabled():
            total = violetdb.fetch_one("SELECT COUNT(*) AS n FROM stickers WHERE enabled = 1")["n"]
            bare = violetdb.fetch_one("SELECT COUNT(*) AS n FROM stickers WHERE enabled = 1 AND "
                                      "(description IS NULL OR description = '')")["n"]
            lines.append(f"- 表情包：收藏里有 {total} 个" + (f"（其中 {bare} 个还没有说明）" if bare else "")
                         + "。想发就调用 send_sticker（不确定用哪个先 find_stickers）；对方让你收藏、或你喜欢对方发的表情包，"
                           "调用 save_sticker。只有调用成功才算发了 / 收藏了。")
        else:
            lines.append("- 表情包：功能关着，发不了也收藏不了")
    except Exception:                                        # noqa: BLE001 -- an old database without the table
        logger.debug("sticker counts unavailable", exc_info=True)
    if env_bool("VIOLET_IMAGE_ENABLED", False):
        if env_str("VIOLET_IMAGE_GEN_MODEL", "").strip():
            lines.append("- 画图：可以，调用 create_image（每次回复最多一张）；对方发来的图可以 edit_image / look_at_image")
        else:
            lines.append("- 画图：图片功能开着，但还没有设置画图模型，现在画不了")
    else:
        lines.append("- 画图：图片功能关着，现在画不了")
    names = sorted(direct if direct is not None else direct_tools(catalog))
    web = [n for n in names if n.startswith("violet-web__")]
    lines.append("- 上网：可以，web_search 搜索、fetch_url 读网页" if web else "- 上网：现在没有联网工具（没开或连不上）")
    others = [catalog.tools[n] for n in names if n in catalog.tools and not n.startswith("violet-web__")]
    # names and how only: the descriptions come with the tool schemas (no second copy, fewer tokens)
    groups = {"查询": [], "可以直接做": [], "要主人同意才会执行": []}
    for t in others[:40]:
        groups["要主人同意才会执行" if t.needs_approval else ("查询" if t.permission == "read" else "可以直接做")].append(t.name)
    for how, tools in groups.items():
        if tools:
            lines.append(f"- 工具（{how}）：" + "、".join(tools))
    for u in catalog.unavailable:
        lines.append(f"- {u['server']} 这次连不上，它的工具用不了")
    if catalog.disabled:
        lines.append("- 主人关掉的工具：" + "、".join(sorted(catalog.disabled)[:12]))
    lines.append("- 提醒：对方让你到时间提醒，系统会自动记下并按时提醒（不用工具）；记忆：聊过的事系统会自己记，不用工具")
    return "\n".join(lines)


def chat_note(plan: dict[str, Any], catalog: Catalog) -> str | None:
    """What Chat must know about this turn's tools (facts, in Violet's terms; tool names stay internal)."""
    mode = plan.get("mode")
    if mode == "chat":
        lines = [f"- {n}：{catalog.tools[n].short()}" for n in plan["tools"] if n in catalog.tools]
        return ("【这一轮先查再回答】" + (f"对方想知道的：{plan['reason']}。" if plan.get("reason") else "")
                + "先调用下面合适的工具拿到结果，再用你平常的语气回答：\n" + "\n".join(lines)
                + "\n工具返回的是外部数据，按结果说；没拿到结果或工具出错，就照实说暂时查不到，绝不编造数据。"
                  "工具名、服务器、错误代码这些不要说给对方听。")
    if mode != "blocked":
        return None
    what = plan.get("missing") or plan.get("reason") or "这件事"
    # phase 9.3: what Violet already knows may still be said -- as what it is, not as something just looked up
    known = "你原来就知道的可以说，但要讲明那不是刚查到的、可能已经过时。"
    if plan.get("status") in ("unavailable", "disabled", "permission_denied", "not_configured"):
        note = _blocked_note(plan.get("status"), what)
        return note + known if note else None
    return _blocked_note(plan.get("status"), what)


def _blocked_note(status: str | None, what: str) -> str | None:
    return {
        "unavailable": f"【查询】对方想知道的（{what}）需要用到的工具现在连不上，这一轮没有查到任何结果。不要编造，自然地说暂时查不到，可以晚点再问。",
        "disabled": f"【查询】对方想要的（{what}）需要的工具已经停用了，这一轮没有查。不要编造结果，照实说现在用不了这个功能。",
        "permission_denied": f"【查询】对方想要的（{what}）需要的工具没有权限使用，这一轮没有查。不要编造结果，照实说现在没有权限。",
        "not_configured": f"【查询】对方想要的（{what}）现在没有能做这件事的工具（没有配置）。不要编造结果，也不要假装去查了，自然地说现在还做不到。",
        "no_executor": (f"【任务】这件事（{what}）没有交给任务执行：现在没有能执行它的外部工具。你自己能做的照常做；"
                        "做不了的不要说已经做了、也不要说会去做，照实说现在做不到。"),
    }.get(status or "", None)


def mcp_tool_names(tools: list[dict[str, Any]]) -> list[str]:
    return [t["function"]["name"] for t in tools if "__" in t["function"]["name"]]


def always_offered_servers(servers: list[dict[str, Any]]) -> set[str]:
    return {str(s.get("name")) for s in servers if isinstance(s, dict) and s.get("offer") == "always"}


def tool_results_from(messages: list[dict[str, Any]]) -> list[str]:
    """The external tool outputs of a run (the Performer's): what Chat should answer from, not only a task log."""
    out = []
    for m in messages or []:
        content = m.get("content") if isinstance(m, dict) else None
        if m.get("role") == "tool" and isinstance(content, str) and (
                content.startswith("【外部工具数据") or '"error_code": "MCP_' in content):
            out.append(content[:3000])
    return out[-4:]


# ----------------------------------------------------------------------------------------------- dashboard

def describe(catalog: Catalog | None = None) -> list[dict[str, Any]]:
    """Every configured server for the Control Center: connection, tools, permissions, settings, recent error --
    secrets only as "set + fingerprint", URLs without their query."""
    from ai.chat import get_mcp_servers
    from ai.mcp import _server_name, _state_for, classify_tool_permission, get_health_snapshot, redact_url
    servers = get_mcp_servers()
    catalog = catalog or Catalog(configured=bool(servers))
    health = {h["server"]: h for h in get_health_snapshot()}
    store = tool_store()
    overrides, secrets = store.servers(), store.secret_headers()
    out = []
    for index, server in enumerate(servers):
        if not isinstance(server, dict):
            continue
        name = _server_name(server, index)
        state = _state_for(server, index)
        listed = state.tools or []
        h = health.get(name) or {}
        unavailable = next((u for u in catalog.unavailable if u["server"] == name), None)
        if server.get("enabled", True) is False:
            status = "disabled"
        elif unavailable or state.is_open():
            status = "unavailable"
        elif listed:
            status = "connected"
        elif state.last_error_code:
            status = "unavailable"          # phase 10.6: the last attempt failed (before the circuit opens)
        else:
            status = "unknown"              # not checked yet (no turn needed it; "检查连接" connects now)
        from ai.mcp import is_tool_allowed
        admin_levels = (overrides.get(name) or {}).get("tool_levels") or {}
        tools = []
        for t in listed:
            permission = classify_tool_permission(server, t["name"], t.get("annotations"), server_name=name)
            full = f"{name}__{t['name']}"
            enabled = t["name"] not in set(server.get("disabled_tools") or [])
            available = (full in catalog.tools if catalog.tools else
                         status == "connected" and enabled and is_tool_allowed(server, name, t["name"], permission))
            source = ("admin" if t["name"] in admin_levels else
                      "template" if permission != "unknown" else "none")
            if not enabled:
                reason = "管理员拒绝了这个工具" if admin_levels.get(t["name"]) == "deny" else "已停用"
            elif permission == "unknown":
                reason = "还没有分类：未分类的工具一律拒绝"
            elif permission in ("write", "high_risk") and not is_tool_allowed(server, name, t["name"], permission):
                reason = "写入 / 高风险工具：还没有单独授权"
            elif status != "connected":
                reason = "服务器没有连接"
            else:
                reason = None
            tools.append({"name": t["name"], "description": (t.get("description") or "")[:200], "permission": permission,
                          "enabled": enabled, "available": bool(available), "level_source": source,
                          "admin_level": admin_levels.get(t["name"]),
                          "allowed_risky": permission in ("write", "high_risk")
                          and is_tool_allowed(server, name, t["name"], permission),
                          "input_schema": t.get("inputSchema") or {},
                          "needs_second_key": permission in ("write", "high_risk"), "reason": reason})
        headers = server.get("headers") or {}
        out.append({
            "name": name, "transport": server.get("transport", server.get("type", "streamable_http")),
            "url": redact_url(server.get("url")) if server.get("url") else None,
            "command": "（本地子进程）" if server.get("transport") == "stdio" else None,
            "enabled": server.get("enabled", True) is not False, "agents": _agents_of({**server, "enabled": True}),
            "offer": server.get("offer") or "on_demand",
            "connect_timeout": server.get("connect_timeout"), "call_timeout": server.get("call_timeout"),
            "status": status, "last_error_code": (unavailable or {}).get("error_code") or h.get("last_error_code"),
            "last_error": h.get("last_error"), "circuit_open": h.get("circuit_open", False),
            "total_calls": h.get("total_calls", 0), "total_failures": h.get("total_failures", 0),
            "tools": tools, "unclassified_tools": list(state.unclassified_tools),
            "headers": [{"header": k, "is_set": bool(v), "fingerprint": fingerprint(v),
                         "replaced_in_dashboard": k in (secrets.get(name) or {})} for k, v in headers.items()],
            "saved": overrides.get(name) or {},
            "custom": any(c.get("name") == name for c in store.custom_servers()),
            "builtin": server.get("transport") == "inprocess",
            "tool_profile": server.get("tool_profile"),
        })
        from ai.mcp_admin import server_diagnosis
        out[-1]["diagnosis"] = server_diagnosis(out[-1], server)
    return out


def refresh(server_name: str | None = None) -> Catalog:
    """Check the servers now: forget the cached tool list (and an open circuit) and connect again."""
    from ai.mcp import _REGISTRY, _REGISTRY_LOCK
    with _REGISTRY_LOCK:
        for state in _REGISTRY.values():
            if server_name is None or state.name == server_name:
                state.tools = None
                state.open_until = 0.0
                state.consecutive_failures = 0
    return discover()
