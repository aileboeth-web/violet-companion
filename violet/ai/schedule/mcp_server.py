"""Phase 9.4.2: the built-in, read-only timetable MCP server "violet-schedule" (in-process, like violet-web).

Mounted by ai/mcp.py (BUILTIN_SERVER_FACTORIES) when VIOLET_SCHEDULE_ENABLED=1; goes through the same allowlist
(mcp_tool_policy.json profile "violet-schedule": four "read" tools), per-agent assignment (chat only), on-demand offer,
timeouts, circuit breaker and untrusted-output wrapping as every other MCP server. It reads the user's own
timetable from Violet's database through ScheduleStore -- it never writes and never copies the data anywhere.

The first sentence of each description is what IntentAgent sees in its tool catalog."""
from __future__ import annotations

import json
import logging

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ai.schedule.tools import ScheduleTools

SERVER_NAME = "violet-schedule"
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
logger = logging.getLogger("violet.schedule")
DETAILS_DOC = (" details: only when the user explicitly asked THIS turn which classroom / where, which teacher, or "
               "which exam seat -- [\"location\"] / [\"teacher\"] / [\"seat\"]; otherwise leave it empty (they are "
               "personal and are only returned when the user's own message asks for them).")
DESCRIPTIONS = {
    "get_day_schedule": (
        "查用户自己的课表里某一天的课和考试（明天有课吗、周三下午有没有课、今天几节课）。"
        "date: today / tomorrow / YYYY-MM-DD / 周一…周日 (this week). Returns each class / exam with local times, status "
        "(cancelled, moved, extra, holiday...), the teaching week, and data_state + state_note that tell \"no class that "
        "day\" apart from \"no timetable entered\", \"term not started\" and \"term over\"." + DETAILS_DOC),
    "get_schedule_range": (
        "查用户课表里一段日子（最多 14 天）的课和考试，比较哪天空、这周有没有考试。"
        "start_date: today / tomorrow / YYYY-MM-DD / this_week (Monday of this week) / next_week. days: 1-14. Returns "
        "per-day items plus a summary (classes, exams, busy minutes), free_days and lightest_days." + DETAILS_DOC),
    "get_current_status": (
        "按课表推算用户现在是不是上课时间、这节几点下课、下一节是什么、今天还剩几节。"
        "No time needed (Violet's own clock, the term's time zone). This is the PLAN, not where the user really is or what "
        "they are doing -- what the user says about themselves wins." + DETAILS_DOC),
    "find_course": (
        "按课名（可以是简称，如“高数”）或老师名找用户课表里的课，返回接下来几次上课和考试。"
        "query: 1-30 characters. Only courses of the current term; at most 5 matches, the next 28 days. No match -> an "
        "empty list: never invent a course." + DETAILS_DOC),
}


def build_server(tools: ScheduleTools) -> FastMCP:
    server = FastMCP(SERVER_NAME)

    async def run(fn):
        try:
            result = await anyio.to_thread.run_sync(fn)
        except Exception:
            logger.exception("[schedule] lookup failed")
            result = {"ok": False, "error_code": "schedule_error", "message": "课表查询失败"}
        return json.dumps(result, ensure_ascii=False)

    @server.tool(annotations=READ_ONLY, description=DESCRIPTIONS["get_day_schedule"])
    async def get_day_schedule(date: str = "today", details: list[str] | None = None) -> str:
        return await run(lambda: tools.day(date, details))

    @server.tool(annotations=READ_ONLY, description=DESCRIPTIONS["get_schedule_range"])
    async def get_schedule_range(start_date: str = "today", days: int = 7, details: list[str] | None = None) -> str:
        return await run(lambda: tools.range(start_date, days, details))

    @server.tool(annotations=READ_ONLY, description=DESCRIPTIONS["get_current_status"])
    async def get_current_status(details: list[str] | None = None) -> str:
        return await run(lambda: tools.status(details))

    @server.tool(annotations=READ_ONLY, description=DESCRIPTIONS["find_course"])
    async def find_course(query: str, details: list[str] | None = None) -> str:
        return await run(lambda: tools.find(query, details))

    return server


def build_default_server() -> FastMCP:
    """Factory used by the MCP bridge: Violet's own database, Violet's own clock."""
    from ai.chat import violetdb
    from ai.schedule.store import ScheduleStore
    return build_server(ScheduleTools(ScheduleStore(violetdb)))
