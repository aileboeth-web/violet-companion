"""Phase 9.2: MCP_SERVERS entries for the fake campus server (tools/fake_mcp_server.py, stdio child process)."""

from __future__ import annotations

import json
import sys

CAMPUS_TOOLS = {"get_course_schedule": "read", "get_weather": "read", "search_web": "read"}


def campus_server(agents=("chat",), mode: str = "normal", name: str = "campus", **extra) -> dict:
    server = {"name": name, "transport": "stdio", "command": [sys.executable, "-m", "tools.fake_mcp_server"],
              "agents": list(agents), "tools": dict(CAMPUS_TOOLS), "connect_timeout": 10, "call_timeout": 10}
    if mode != "normal":
        server["env"] = {"FAKE_MCP_MODE": mode, "FAKE_MCP_SLOW_SECONDS": "30"}
    server.update(extra)
    return server


def not_started(name: str = "campus", agents=("chat",)) -> dict:
    """A configured server whose process exits at once (the server "is not running")."""
    return {"name": name, "transport": "stdio", "command": [sys.executable, "-c", "import sys; sys.exit(1)"],
            "agents": list(agents), "tools": dict(CAMPUS_TOOLS), "connect_timeout": 5, "call_timeout": 5}


def use(monkeypatch, *servers) -> None:
    monkeypatch.setenv("MCP_SERVERS", json.dumps(list(servers)))
