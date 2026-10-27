"""Phase 9.2: a fake MCP server with FICTIONAL read-only tools, for offline tests and local trials only.

    python -m tools.fake_mcp_server            stdio (a child process of Violet -- no port, nothing on the network)
    python -m tools.fake_mcp_server --http P   streamable HTTP on 127.0.0.1:P/mcp (FAKE_MCP_TOKEN = required Bearer)

Tools: get_course_schedule (a made-up timetable), get_weather (made-up weather), search_web (made-up search
results). No real school system, weather service or search engine is involved, and nothing is written anywhere.

Failure modes for tests, by environment variable of the child process:
    FAKE_MCP_MODE=normal | error (every tool reports isError) | crash (the process exits during the first call)
                  | slow (each call sleeps FAKE_MCP_SLOW_SECONDS, default 30)
    FAKE_MCP_MODE_FILE=<file>: the mode is read from this file at every call instead (free_chat /fake-mode)
"""

from __future__ import annotations

import json
import os
import sys
import time

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

SCHEDULE = {
    "周一": [{"time": "08:00-09:40", "course": "高等数学（虚构）", "room": "教三-201"},
            {"time": "14:00-15:40", "course": "大学英语（虚构）", "room": "外语楼-305"}],
    "周二": [{"time": "10:00-11:40", "course": "数据结构（虚构）", "room": "计算机楼-110"}],
    "周三": [{"time": "08:00-09:40", "course": "线性代数（虚构）", "room": "教三-108"},
            {"time": "15:50-17:30", "course": "体育（虚构）", "room": "东操场"}],
    "周四": [], "周五": [{"time": "10:00-11:40", "course": "概率论（虚构）", "room": "教一-402"}],
    "周六": [], "周日": [],
}
WEATHER = {"default": {"condition": "多云转小雨（虚构）", "low": 14, "high": 21, "rain_chance": "70%"}}


def _mode() -> str:
    # FAKE_MCP_MODE_FILE (free_chat /fake-mode): read at every call, so a live session can switch failure modes
    switch = os.environ.get("FAKE_MCP_MODE_FILE")
    if switch and os.path.isfile(switch):
        with open(switch, encoding="utf-8") as f:
            return f.read().strip() or "normal"
    return os.environ.get("FAKE_MCP_MODE", "normal")


def _guard() -> None:
    mode = _mode()
    if mode == "crash":
        os._exit(3)                                   # the connection drops in the middle of the call
    if mode == "slow":
        time.sleep(float(os.environ.get("FAKE_MCP_SLOW_SECONDS", "30")))
    if mode == "error":
        raise RuntimeError("upstream service returned 500 at http://10.0.0.8:9000/internal/api (fake)")


def build_server() -> FastMCP:
    server = FastMCP("fake-campus")

    @server.tool(annotations=READ_ONLY)
    def get_course_schedule(day: str) -> str:
        """查询用户这学期的课表（虚构数据）。day 填 周一…周日 或 today。返回当天的课程、时间和教室。"""
        _guard()
        key = day if day in SCHEDULE else "周三"
        return json.dumps({"day": key, "courses": SCHEDULE[key]}, ensure_ascii=False)

    @server.tool(annotations=READ_ONLY)
    def get_weather(city: str, date: str = "today") -> str:
        """查询某个城市某天的天气预报（虚构数据）。"""
        _guard()
        return json.dumps({"city": city, "date": date, **WEATHER["default"]}, ensure_ascii=False)

    @server.tool(annotations=READ_ONLY)
    def search_web(query: str, limit: int = 3) -> str:
        """搜索网页，返回标题、链接和摘要（虚构数据，不联网）。需要最新消息或外部资料时用。"""
        _guard()
        results = [{"title": f"关于“{query}”的示例结果 {i + 1}（虚构）", "url": f"https://example.invalid/{i + 1}",
                    "snippet": "这是一条测试用的虚构搜索结果。"} for i in range(max(1, min(int(limit), 5)))]
        return json.dumps({"query": query, "results": results}, ensure_ascii=False)

    return server


def http_app(token: str | None = None):
    """Phase 10.5: the same tools over streamable HTTP (for "add a server" in the Control Center), optionally behind
    a Bearer token. Loopback only (see main)."""
    app = build_server().streamable_http_app()
    if not token:
        return app

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization", b"").decode() != f"Bearer {token}":
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await app(scope, receive, send)
    return guarded


def main() -> None:  # pragma: no cover - exercised as a child process
    if "--http" in sys.argv:
        import uvicorn
        port = int(sys.argv[sys.argv.index("--http") + 1])
        token = os.environ.get("FAKE_MCP_TOKEN") or None
        uvicorn.run(http_app(token), host="127.0.0.1", port=port, log_level="warning")
        return
    build_server().run("stdio")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
