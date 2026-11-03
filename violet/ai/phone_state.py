"""What the owner's phone says right now (2026-11-03, user: 主动发消息和平常消息的时候可以用 mcp 知道用户在干嘛，
再判断要不要开口、要怎么开口; 还想让她知道手机软件的使用时长).

The code -- not the model -- reads the phone's read-only MCP tools (Violet MCP: get_screen_state,
get_foreground_app, battery, get_app_usage_stats; since Violet MCP 1.4.1 also now_playing, audio_output,
network_state, step_count, next_alarm, calendar_events) and turns them into ONE short line, cached for two minutes:

    【主人手机此刻（14:05 读取）】屏幕亮着、已解锁，正在用「哔哩哔哩」（约 25 分钟）；电量 43%，没在充电；
    今天亮屏用了 4 小时 12 分：哔哩哔哩 1 小时 50 分、微信 1 小时、王者荣耀 40 分

VIOLET_PHONE_CONTEXT (default on): the line goes to the proactive decision and the proactive message.
VIOLET_PHONE_CONTEXT_CHAT (default off): also to ordinary replies (some people find that watched).
No phone server, phone offline, a tool switched off on the phone or no usage access: those parts are left out; with
nothing at all there is no line. Do-not-disturb is never read (user: 我几乎一直开免打扰，靠手表提醒).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from ai.settings import env_bool, env_float

logger = logging.getLogger("violet.phone_state")

TOOLS = ("get_screen_state", "get_foreground_app", "battery", "get_app_usage_stats",
         # Violet MCP 1.4.1: life state (each one switched on by the owner on the phone)
         "now_playing", "audio_output", "network_state", "step_count", "next_alarm", "calendar_events")
ARGS = {"get_foreground_app": {"lookbackMinutes": 120}, "get_app_usage_stats": {"range": "today", "limit": 5},
        "calendar_events": {"days": 2}}
CACHE_SECONDS = 120
TIMEOUT_SECONDS = 12
_cache: dict[str, Any] = {"at": 0.0, "line": ""}
_lock = threading.Lock()

GUIDE = ("（手机状态只作参考：判断现在合不合适说话、怎么开口更自然。不要每次都提，更不要逐条复述；"
         "可以偶尔顺口说一句“又在刷某某呀”，但不要让对方觉得被监视。夜里屏幕熄灭很久多半是在睡觉。）")


def enabled_for(kind: str) -> bool:
    if kind == "chat":
        return env_bool("VIOLET_PHONE_CONTEXT_CHAT", False)
    return env_bool("VIOLET_PHONE_CONTEXT", True)


def _minutes(seconds: Any) -> str:
    try:
        total = int(float(seconds) // 60)
    except (TypeError, ValueError):
        return ""
    if total < 1:
        return "不到 1 分钟"
    hours, minutes = divmod(total, 60)
    return (f"{hours} 小时" if hours else "") + (f" {minutes} 分钟" if minutes and hours else f"{minutes} 分钟" if minutes else "")


def _ms(value: Any) -> str:
    try:
        return _minutes(int(value) / 1000)
    except (TypeError, ValueError):
        return ""


def compose(data: dict[str, dict[str, Any]], read_at: str, now: datetime | None = None) -> str:
    """One line from the tools' JSON results (missing / failed ones are left out)."""
    if now is None:
        from ai.proactive_scheduler import now_china
        now = now_china()
    parts: list[str] = []
    screen = data.get("get_screen_state") or {}
    fg = data.get("get_foreground_app") or {}
    if "screenOn" in screen:
        since = _minutes(screen.get("stateDurationSeconds")) if screen.get("stateDurationSeconds") is not None else ""
        if not screen.get("screenOn"):
            parts.append("屏幕熄灭" + (f"了约 {since}" if since else ""))
        elif screen.get("keyguardLocked"):
            parts.append("屏幕亮着，在锁屏界面")
        else:
            parts.append("屏幕亮着、已解锁" + (f"（亮了约 {since}）" if since and fg.get("state") != "current" else ""))
    if fg.get("state") == "current" and fg.get("app"):
        stay = _minutes(fg.get("age_seconds"))
        parts.append(f"正在用「{fg['app']}」" + (f"（约 {stay}）" if stay else ""))
    battery = data.get("battery") or {}
    try:
        percent = int(battery.get("percent", -1))
    except (TypeError, ValueError):
        percent = -1
    if percent >= 0:
        parts.append(f"电量 {percent}%" + ("，在充电" if battery.get("charging") else "，没在充电"))
    usage = data.get("get_app_usage_stats") or {}
    apps = [a for a in usage.get("apps") or [] if isinstance(a, dict) and (a.get("appName") or a.get("packageName"))]
    if usage.get("totalForegroundMs") is not None:
        top = "、".join(f"{a.get('appName') or a.get('packageName')} {_ms(a.get('foregroundMs'))}" for a in apps[:5]
                        if _ms(a.get("foregroundMs")))
        total = _ms(usage.get("totalForegroundMs"))
        if total:
            parts.append(f"今天用手机约 {total}" + (f"：{top}" if top else ""))
    parts += _life_parts(data, now)
    if not parts:
        return ""
    return f"【主人手机此刻（{read_at} 读取）】" + "；".join(parts) + "\n" + GUIDE


def _when(stamp: Any, now: datetime) -> tuple[datetime | None, str]:
    """A phone time stamp ({epochMs, iso}) as (datetime, "今天 15:00" / "明天 07:30")."""
    try:
        at = datetime.fromisoformat(str((stamp or {}).get("iso") or ""))
    except (TypeError, ValueError, AttributeError):
        return None, ""
    local = at.astimezone(now.tzinfo) if now.tzinfo else at
    days = (local.date() - now.date()).days
    word = {0: "今天", 1: "明天"}.get(days, local.strftime("%m-%d"))
    return local, f"{word} {local.strftime('%H:%M')}"


def _life_parts(data: dict[str, dict[str, Any]], now: datetime) -> list[str]:
    parts: list[str] = []
    music = data.get("now_playing") or {}
    playing = [s for s in music.get("sessions") or [] if isinstance(s, dict) and s.get("playing") and s.get("title")]
    if playing:
        s = playing[0]
        parts.append(f"在听「{s['title']}」" + (f"（{s['artist']}）" if s.get("artist") else ""))
    if (data.get("audio_output") or {}).get("headphonesOrBluetooth"):
        parts.append("戴着耳机 / 连着蓝牙音频")
    net = data.get("network_state") or {}
    if net.get("type") == "wifi":
        parts.append(f"连着「{net['place']}」的 Wi-Fi" if net.get("place") else "连着 Wi-Fi")
    elif net.get("type") == "cellular":
        parts.append("用的是流量（没连 Wi-Fi）")
    steps = data.get("step_count") or {}
    if isinstance(steps.get("stepsToday"), int):
        parts.append(f"今天走了 {steps['stepsToday']} 步")
    alarm = data.get("next_alarm") or {}
    if alarm.get("hasAlarm"):
        at, text = _when(alarm.get("time"), now)
        if at is not None and (at - now) <= timedelta(hours=24):
            parts.append(f"下一个闹钟 {text}")
    calendar = data.get("calendar_events") or {}
    upcoming = []
    for e in calendar.get("events") or []:
        if not isinstance(e, dict) or not e.get("title"):
            continue
        at, text = _when(e.get("start"), now)
        end, _ = _when(e.get("end"), now)
        if at is None or (end or at) < now or at - now > timedelta(hours=36):
            continue
        when = text.split(" ")[0] + "全天" if e.get("allDay") else text
        upcoming.append(f"{when} {e['title'][:20]}")
    if upcoming:
        parts.append("日程：" + "、".join(upcoming[:3]))
    return parts


def _unwrap(text: str) -> str:
    """The tool's own text inside the bridge's 【外部工具数据｜…】 … 【外部工具数据结束】 frame (ai/mcp.wrap_untrusted)."""
    text = str(text or "").strip()
    if text.startswith("【外部工具数据"):
        body = text.split("\n", 1)[1] if "\n" in text else ""
        end = body.rfind("【外部工具数据结束】")
        return body[:end].strip() if end >= 0 else body.strip()
    return text


def _read(calls: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """{tool: JSON result} for the phone tools found (default: TOOLS with ARGS). Tools missing on the phone, switched
    off there or answering an error are left out."""
    calls = calls if calls is not None else {t: dict(ARGS.get(t) or {}) for t in TOOLS}
    from ai.chat import get_mcp_servers
    from ai.mcp import MCPToolBridge
    from ai.tool_manager import _run_sync
    servers = [s for s in get_mcp_servers() if isinstance(s, dict)]
    if not servers:
        return {}

    async def run():
        bridge = MCPToolBridge(servers, agent_name=None)
        out: dict[str, dict[str, Any]] = {}
        try:
            await bridge.connect()
            for tool, args in calls.items():
                name = next((n for n, b in bridge.tool_to_session.items()
                             if n.endswith("__" + tool) and getattr(b, "permission", "read") == "read"), None)
                if name is None:
                    continue
                text = await bridge.call_tool(name, dict(args))
                try:
                    value = json.loads(_unwrap(text))
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict) and not value.get("error_code") and not value.get("error"):
                    out[tool] = value
        finally:
            await bridge.close()
        return out

    async def bounded():
        return await asyncio.wait_for(run(), timeout=env_float("VIOLET_PHONE_CONTEXT_TIMEOUT", TIMEOUT_SECONDS))

    try:
        return _run_sync(bounded) or {}
    except Exception as exc:                                # noqa: BLE001 -- the phone is optional
        logger.info("phone state unavailable: %s", exc.__class__.__name__)
        return {}


def line(kind: str = "proactive") -> str:
    """The phone line for this use, or "" (switched off / nothing readable). Cached CACHE_SECONDS."""
    if not enabled_for(kind):
        return ""
    with _lock:
        if time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["line"]
    data = _read()
    from ai.proactive_scheduler import now_china
    current = now_china()
    text = compose(data, current.strftime("%H:%M"), current)
    try:                                                    # the phone's recent events (ai/phone_events.py)
        from ai import phone_events
        recent = phone_events.recent_text(current)
    except Exception:                                       # noqa: BLE001
        recent = ""
    if recent:
        head, _, guide = text.partition("\n")
        text = (head + "；" + recent if head else f"【主人手机此刻（{current.strftime('%H:%M')} 读取）】{recent}") \
            + "\n" + (guide or GUIDE)
    with _lock:
        _cache.update(at=time.monotonic(), line=text)
    from ai import trace
    trace.step("读取手机状态", 用途=kind, 结果=text.split("\n", 1)[0] if text else "没有读到")
    return text


def clear_cache() -> None:
    with _lock:
        _cache.update(at=0.0, line="")
