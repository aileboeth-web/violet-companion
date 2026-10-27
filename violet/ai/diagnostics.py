"""Unified diagnosis (phase 10.6): one place that tells the admin WHERE Violet is failing, in plain words, with a
suggestion -- and a one-click fix only where the fix is safe.

Events (an in-memory record of the recent past; a restart starts a fresh one -- the database keeps the inbox / outbox
history the diagnosis also reads):
  model        a model call failed (agent, model, kind: auth / not_found / connection / timeout ...), or succeeded
  mcp_connect  an MCP server could not be connected (error code)
  tool_call    an MCP tool was called (ok / error code)
  tool_plan    what a turn did with tools: Intent's request, the plan (chat / performer / blocked + why), the tools
               offered to Chat and the tools the model really called
Every event carries the turn's correlation id (ai.settings.CURRENT_TURN; also printed in every log line of the turn).

``report(...)`` turns events and live state into findings, grouped by area, each one of these kinds:
  channel_down          a chat channel is not connected (needs login / error / reconnecting)
  model_failed          model calls fail (and why)
  mcp_not_connected     an MCP server cannot be reached
  tool_unauthorized     MCP connected, tools not classified / not authorised / template mismatch
  tool_not_planned      a tool is usable but did not get into a turn's plan (unknown name, offered but not called,
                        authorised only for an agent that never plans)
  tool_failed           tool calls fail
  network_unreachable   Violet runs, but an outside address does not answer (DNS / TCP), with the proxy settings seen
plus core items (worker threads, stuck messages, failed sends, database).
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import socket
import threading
import time
from collections import deque
from typing import Any
from urllib.parse import urlsplit

from ai.settings import CURRENT_TURN

_LOCK = threading.Lock()
_EVENTS: deque = deque(maxlen=1000)
_LAST_OK: dict[str, float] = {}
_NETWORK: dict[str, Any] = {}
WINDOW = 3600.0                     # findings look at the last hour of events


def reset_for_tests() -> None:
    with _LOCK:
        _EVENTS.clear()
        _LAST_OK.clear()
        _NETWORK.clear()


@contextlib.contextmanager
def turn_scope(turn_id: str | None):
    token = CURRENT_TURN.set(turn_id)
    try:
        yield turn_id
    finally:
        CURRENT_TURN.reset(token)


def record(kind: str, ok: bool, **data: Any) -> None:
    """Never raises; values must already be safe to show (no secrets, no message texts)."""
    try:
        with _LOCK:
            if ok:
                _LAST_OK[f"{kind}:{data.get('agent') or data.get('server') or ''}"] = time.time()
            if ok and kind in ("model", "mcp_connect"):
                return                                   # success of a frequent call: only the timestamp
            _EVENTS.append({"at": time.time(), "kind": kind, "ok": ok, "turn": CURRENT_TURN.get(), **data})
    except Exception:
        pass


def events(kind: str | None = None, since: float | None = None) -> list[dict[str, Any]]:
    cutoff = time.time() - WINDOW if since is None else since
    with _LOCK:
        return [dict(e) for e in _EVENTS if e["at"] >= cutoff and (kind is None or e["kind"] == kind)]


def last_ok(kind: str, name: str = "") -> float | None:
    with _LOCK:
        return _LAST_OK.get(f"{kind}:{name}")


# ------------------------------------------------------------------------------------------------ network check

def _proxy_for(host: str) -> str | None:
    import urllib.request
    proxies = urllib.request.getproxies()
    if not proxies:
        return None
    if urllib.request.proxy_bypass(host):
        return None
    return proxies.get("https") or proxies.get("http")


def check_host(url: str, timeout: float = 4.0) -> dict[str, Any]:
    """DNS and a TCP connection to the host of url (no request is sent, nothing is read)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme in ("https", "wss") else 80)
    out: dict[str, Any] = {"host": host, "port": port, "proxy": bool(_proxy_for(host))}
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = sorted({a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        except OSError:
            return {**out, "ok": False, "stage": "dns", "message": f"域名 {host} 解析失败（DNS）"}
    started = time.monotonic()
    for address in addresses[:3]:
        try:
            with socket.create_connection((address, port), timeout=timeout):
                return {**out, "ok": True, "stage": "tcp", "latency_ms": int((time.monotonic() - started) * 1000)}
        except OSError:
            continue
    return {**out, "ok": False, "stage": "tcp", "message": f"{host}:{port} 连接不上（防火墙 / 网络 / 对方服务不在）"}


def network_targets(runtime=None) -> list[tuple[str, str]]:
    """(what, url) Violet has to reach from this machine: the model API, remote MCP servers, chat platform APIs."""
    targets: list[tuple[str, str]] = []
    api = os.environ.get("LLM_API_URL", "").strip()
    if api:
        targets.append(("模型接口", api))
    try:
        from ai.chat import get_mcp_servers
        for server in get_mcp_servers():
            url = str(server.get("url") or "") if isinstance(server, dict) else ""
            if url.startswith(("http://", "https://")):
                targets.append((f"MCP {server.get('name')}", url))
    except Exception:
        pass
    manager = getattr(runtime, "channels", None) if runtime is not None else None
    if manager is not None:
        for channel_id, entry in manager.entries().items():
            if not entry.get("enabled", True):
                continue
            kind = entry.get("type")
            if kind == "telegram":
                targets.append((f"渠道 {channel_id}", (entry.get("config") or {}).get("api_base") or "https://api.telegram.org"))
            elif kind == "qqbot":
                targets.append((f"渠道 {channel_id}", "https://api.sgroup.qq.com"))
                targets.append((f"渠道 {channel_id}（令牌）", "https://bots.qq.com"))
            elif kind == "onebot11":
                targets.append((f"渠道 {channel_id}", (entry.get("config") or {}).get("api_base") or ""))
    return [(w, u) for w, u in targets if u]


def run_network_check(runtime=None) -> dict[str, Any]:
    results = []
    for what, url in network_targets(runtime):
        result = check_host(url)
        results.append({"what": what, **result})
    proxies = {k: bool(v) for k, v in __import__("urllib.request").request.getproxies().items() if k in ("http", "https")}
    summary = {"at": time.time(), "results": results, "proxy_env": proxies}
    with _LOCK:
        _NETWORK.clear()
        _NETWORK.update(summary)
    return summary


def last_network_check() -> dict[str, Any] | None:
    with _LOCK:
        return dict(_NETWORK) if _NETWORK else None


# ------------------------------------------------------------------------------------------------ the report

def _finding(area: str, kind: str, level: str, message: str, suggestion: str = "", fix: dict | None = None,
             turns: list[str] | None = None) -> dict[str, Any]:
    return {"area": area, "kind": kind, "level": level, "message": message, "suggestion": suggestion, "fix": fix,
            "turns": [t for t in (turns or []) if t][:5]}


MODEL_WORDS = {
    "auth": ("API Key 不对或已失效", "在“开始”页重新填写 API Key"),
    "forbidden": ("这个 Key 没有权限使用该模型", "换一个有权限的模型，或检查服务商的账户权限"),
    "not_found": ("接口地址不对或模型名不存在", "核对 API 地址（一般以 /v1 结尾）和各 Agent 的模型名"),
    "bad_request": ("接口拒绝了请求（多半是模型名或参数）", "核对模型名；换一个模型试试"),
    "rate_limited": ("请求太频繁或额度用完", "检查服务商额度；稍后会自动恢复"),
    "timeout": ("模型接口响应超时", "检查网络；可以在“设置”里调大超时"),
    "connection": ("连不上模型接口", "检查网络和代理（下面的“外部网络”检查）"),
    "server": ("模型服务出错（5xx）", "服务商的问题，稍后重试"),
    "not_configured": ("还没有填写模型服务", "在“开始”页填写 API 地址和 Key"),
}


def report(runtime=None, agents=None, deep: bool = False) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    findings += _core_findings(runtime, deep)
    findings += _channel_findings(runtime)
    findings += _model_findings()
    findings += _mcp_findings()
    findings += _plan_findings()
    findings += _tool_call_findings()
    findings += _network_findings()
    order = {"error": 0, "warn": 1, "info": 2, "ok": 3}
    findings.sort(key=lambda f: order.get(f["level"], 9))
    problems = [f for f in findings if f["level"] in ("error", "warn")]
    return {"ok": not any(f["level"] == "error" for f in findings), "problems": len(problems), "findings": findings,
            "network": last_network_check(), "generated_at": time.time()}


def _core_findings(runtime, deep: bool) -> list[dict[str, Any]]:
    out = []
    if runtime is None:
        return out
    try:
        health = runtime.health()
    except Exception as exc:
        return [_finding("core", "core", "error", f"读取运行状态失败（{exc.__class__.__name__}）")]
    if not health.get("worker_thread_alive"):
        out.append(_finding("core", "core", "error", "消息处理线程没有运行：收到的消息不会被处理",
                            "重启 Violet（violet restart / systemctl restart violet）"))
    if runtime.memory_worker is not None and not health.get("memory_worker_alive"):
        out.append(_finding("core", "core", "warn", "后台记忆整理线程没有运行：聊天正常，但新内容不会写进长期记忆", "重启 Violet"))
    db = runtime.db
    stuck = db.fetch_all("SELECT id, turn_id FROM inbound_messages WHERE status = 'processing' AND updated_at < ?",
                         (_minutes_ago(db, 10),))
    if stuck:
        out.append(_finding("core", "queue", "warn", f"{len(stuck)} 条消息处理超过 10 分钟还没结束",
                            "多半是模型或工具一直没回应；查看这些轮次的日志", turns=[r.get("turn_id") for r in stuck]))
    failed = db.fetch_all("SELECT id, turn_id, error FROM outbound_messages WHERE status = 'failed' AND updated_at >= ?",
                          (_minutes_ago(db, 24 * 60),))
    if failed:
        out.append(_finding("core", "queue", "warn", f"最近 24 小时有 {len(failed)} 条消息发送失败",
                            "看对应渠道的状态；失败的消息不会自动重发（避免重复）", turns=[r.get("turn_id") for r in failed]))
    unknown = db.fetch_one("SELECT COUNT(*) AS n FROM outbound_messages WHERE status = 'unknown'")["n"]
    if unknown:
        out.append(_finding("core", "queue", "info", f"{unknown} 条消息在崩溃时状态不明（不会重发，避免重复）"))
    scheduler = getattr(runtime, "proactive_scheduler", None)
    thread = getattr(scheduler, "_thread", None)
    if scheduler is not None and thread is not None and not thread.is_alive():
        out.append(_finding("core", "scheduler", "warn", "主动消息调度线程已经停止", "重启 Violet"))
    if deep:
        try:
            result = db.fetch_one("PRAGMA quick_check")
            value = next(iter(result.values())) if result else "ok"
            if value != "ok":
                out.append(_finding("core", "database", "error", f"记忆数据库检查发现问题：{str(value)[:120]}",
                                    "先备份（violet backup），再联系开发者"))
        except Exception as exc:
            out.append(_finding("core", "database", "error", f"记忆数据库无法检查（{exc.__class__.__name__}）"))
    if not out:
        out.append(_finding("core", "core", "ok", "核心服务正常：消息处理、记忆整理、发件箱"))
    return out


def _minutes_ago(db, minutes: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(db.now()) - timedelta(minutes=minutes)).isoformat()


def _channel_findings(runtime) -> list[dict[str, Any]]:
    manager = getattr(runtime, "channels", None) if runtime is not None else None
    if manager is None:
        return []
    out = []
    for c in manager.describe():
        state = c["status"].get("state")
        label = c["label"]
        if state == "disabled":
            continue
        if state == "connected":
            if c["type"] != "wechat_ilink" and not c["peers"]["bound"]:
                out.append(_finding("channels", "channel_down", "warn", f"{label} 已连接，但还没有绑定你的账号：消息不会被处理",
                                    "在“渠道”页生成绑定码，用你的账号发给机器人"))
            else:
                out.append(_finding("channels", "channel_down", "ok", f"{label} 已连接"))
            continue
        detail = c["status"].get("detail") or ""
        error = c["status"].get("last_error")
        message = f"聊天渠道未连接：{label}（{detail}" + (f"；最近错误：{error}" if error else "") + "）"
        if state == "needs_login":
            suggestion = "微信：在后台扫码登录" if c["type"] == "wechat_ilink" else "检查并重新填写这个渠道的凭据"
            out.append(_finding("channels", "channel_down", "error", message, suggestion))
        else:
            out.append(_finding("channels", "channel_down", "error", message, "可以先重启这个渠道；仍失败就检查网络或凭据",
                                fix={"action": "restart_channel", "target": c["id"]}))
    return out


def _model_findings() -> list[dict[str, Any]]:
    from ai.chat import llm_configured
    if not llm_configured():
        word, tip = MODEL_WORDS["not_configured"]
        return [_finding("model", "model_failed", "error", f"模型调用失败：{word}", tip)]
    failures = [e for e in events("model") if not e["ok"]]
    if not failures:
        last = last_ok("model", "ChatAgent")
        return [_finding("model", "model_failed", "ok", "模型调用正常" + ("" if last else "（这次启动后还没有调用过）"))]
    out = []
    by_kind: dict[str, list[dict]] = {}
    for e in failures:
        by_kind.setdefault(e.get("error_kind") or "error", []).append(e)
    for kind, items in by_kind.items():
        word, tip = MODEL_WORDS.get(kind, (f"调用失败（{kind}）", "查看日志"))
        agents = sorted({i.get("agent") or "?" for i in items})
        recovered = all((last_ok("model", a) or 0) > max(i["at"] for i in items if i.get("agent") == a) for a in agents)
        out.append(_finding("model", "model_failed", "warn" if recovered else "error",
                            f"模型调用失败：{word}（最近一小时 {len(items)} 次；{'、'.join(agents)}；模型 "
                            f"{'、'.join(sorted({i.get('model') or '?' for i in items}))}）"
                            + ("，之后已经恢复" if recovered else ""),
                            tip, fix={"action": "retest_model"}, turns=[i.get("turn") for i in items[-5:]]))
    return out


def _mcp_findings() -> list[dict[str, Any]]:
    try:
        from ai.tool_manager import describe
        servers = describe()
    except Exception as exc:
        return [_finding("mcp", "mcp_not_connected", "error", f"读取 MCP 状态失败（{exc.__class__.__name__}）")]
    out = []
    for s in servers:
        for d in s.get("diagnosis") or []:
            if d["code"] == "ok":
                out.append(_finding("mcp", "mcp", "ok", f"MCP {s['name']}：{d['message']}"))
                continue
            kind = "mcp_not_connected" if d["code"] == "mcp_not_connected" else \
                "tool_unauthorized" if d["code"] in ("profile_mismatch", "tools_unclassified", "tools_need_second_key",
                                                     "no_agent") else "mcp"
            fix = None
            if d.get("action") == "apply_profile":
                fix = {"action": "apply_profile", "target": s["name"], "profile": d["profile"]}
            elif d.get("action") == "check":
                fix = {"action": "check_mcp", "target": s["name"]}
            level = {"error": "error", "warn": "warn"}.get(d["level"], "info")
            suggestion = "在“工具”页处理：" + {"tools_unclassified": "逐个选择只读 / 写入 / 高风险 / 拒绝",
                                             "tools_need_second_key": "确认后单独授权",
                                             "no_agent": "勾选要使用它的 Agent"}.get(d["code"], "按提示操作")
            out.append(_finding("mcp", kind, level, f"MCP {s['name']}：{d['message']}", suggestion, fix))
    return out


def _plan_findings() -> list[dict[str, Any]]:
    out = []
    plans = events("tool_plan")
    unknown = [p for p in plans if p.get("unknown_tools")]
    if unknown:
        names = sorted({n for p in unknown for n in p["unknown_tools"]})
        out.append(_finding("tools", "tool_not_planned", "warn",
                            f"Intent 要用的工具名不存在：{'、'.join(names[:6])}（{len(unknown)} 轮）",
                            "通常是工具改名或服务器换了名字；检查“工具”页的工具名", turns=[p.get("turn") for p in unknown]))
    not_called = [p for p in plans if p.get("mode") == "chat" and not p.get("called")]
    if not_called:
        out.append(_finding("tools", "tool_not_planned", "warn",
                            f"工具已经提供给 Chat，但模型没有调用（{len(not_called)} 轮）",
                            "换一个工具调用能力更好的 Chat 模型，或检查工具说明是否清楚", turns=[p.get("turn") for p in not_called]))
    blocked = [p for p in plans if p.get("mode") == "blocked" and p.get("status") in ("permission_denied", "disabled")]
    if blocked:
        out.append(_finding("tools", "tool_unauthorized", "warn",
                            f"需要的工具没有权限或已停用，{len(blocked)} 轮没有查成：{'、'.join(sorted({n for p in blocked for n in p.get('tools') or []})[:6])}",
                            "在“工具”页给工具分类 / 授权", turns=[p.get("turn") for p in blocked]))
    try:
        from ai.tool_manager import discover
        catalog = discover()
        orphans = [t.name for t in catalog.tools.values() if not ({"chat", "performer"} & set(t.agents))]
        if orphans:
            out.append(_finding("tools", "tool_not_planned", "info",
                                f"这些工具只授权给了主动消息，聊天时不会被规划：{'、'.join(orphans[:6])}",
                                "需要在聊天中使用的话，在“工具”页勾选 Chat"))
    except Exception:
        pass
    return out


def _tool_call_findings() -> list[dict[str, Any]]:
    failures = [e for e in events("tool_call") if not e["ok"]]
    if not failures:
        return []
    by_tool: dict[str, list[dict]] = {}
    for e in failures:
        by_tool.setdefault(f"{e.get('server')}__{e.get('tool')}", []).append(e)
    out = []
    for name, items in by_tool.items():
        codes = sorted({i.get("code") or "?" for i in items})
        out.append(_finding("tools", "tool_failed", "warn", f"工具调用失败：{name}（{len(items)} 次，{'、'.join(codes)}）",
                            "在“工具”页点“测试”看具体原因；连不上时先“检查连接”", turns=[i.get("turn") for i in items]))
    return out


def _network_findings() -> list[dict[str, Any]]:
    check = last_network_check()
    model_conn = [e for e in events("model") if not e["ok"] and e.get("error_kind") in ("connection", "timeout")]
    mcp_conn = [e for e in events("mcp_connect") if not e["ok"] and e.get("code") in ("MCP_CONNECT_FAILED", "MCP_TIMEOUT")]
    if check is None:
        if model_conn or mcp_conn:
            return [_finding("network", "network_unreachable", "warn",
                             "最近有连不上外部服务的情况（模型接口 / MCP）", "点“检查外部网络”确认是网络问题还是对方的问题",
                             fix={"action": "network_check"})]
        return [_finding("network", "network_unreachable", "info", "还没有检查外部网络", "",
                         fix={"action": "network_check"})]
    bad = [r for r in check["results"] if not r["ok"]]
    if not check["results"]:
        return [_finding("network", "network_unreachable", "info",
                         "没有需要检查的外部地址（还没有配置模型接口、远程 MCP 或需要联网的渠道）", "",
                         fix={"action": "network_check"})]
    if not bad:
        return [_finding("network", "network_unreachable", "ok",
                         f"外部网络正常（{len(check['results'])} 个地址都能连上）", "", fix={"action": "network_check"})]
    proxy_note = "；系统设置了代理（HTTP_PROXY / HTTPS_PROXY），本机或内网地址需要写进 NO_PROXY" if any(
        check.get("proxy_env", {}).values()) else ""
    return [_finding("network", "network_unreachable", "error",
                     f"服务运行正常，但外部网络不可达：{r['what']} {r.get('message') or ''}{proxy_note}",
                     "检查服务器的网络、DNS、防火墙或云安全组的出站规则", fix={"action": "network_check"}) for r in bad]
