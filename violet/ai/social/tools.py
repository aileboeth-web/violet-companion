"""Tools in QQ groups and other people's private chats (2026-10-30, user's screenshot from a group: "今天杭州什么天气",
"发一张你现在的自拍照", "用生图模型做", "那你知道我手机现在多少电吗" -- all answered "查不到 / 做不了": the social decision
was one JSON call without any tool).

What is offered, decided by the code:
  * everyone          the web tools (public information)
  * only the owner    the owner's own data tools (phone, timetable, the MCP servers he added -- read-only ones) and
                      drawing a picture (it costs money); only when the owner is among the people she is answering
                      and "群里听主人的话" is on. A stranger never reads the owner's phone and never spends his budget.
Tools that change something are never offered here (everyone in the group can see and ask).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ai import trace

logger = logging.getLogger("violet.social.tools")

IMAGE_SCHEMA = {"type": "function", "function": {
    "name": "create_image",
    "description": ("画一张图发到这里（主人要的时候用）。subject=violet：画你自己现在的样子（AI 插画，不是照片）；"
                    "subject=other：画别的东西，prompt 写要画什么。图会跟着你的消息发出去。"),
    "parameters": {"type": "object", "properties": {
        "subject": {"type": "string", "enum": ["violet", "other"]},
        "prompt": {"type": "string", "description": "要画的内容或补充"}},
        "required": ["subject"]}}}


class SocialTools:
    """One decision's tools: schemas for the model, run(name, args) -> result text, the pictures drawn."""

    def __init__(self, db, owner_asked: bool, turn_key: str):
        self.db = db
        self.owner_asked = owner_asked
        self.turn_key = turn_key
        self.schemas: list[dict[str, Any]] = []
        self.names: set[str] = set()
        self.image_asset_ids: list[int] = []
        self.called: list[str] = []
        from ai.web.tool_guard import WebToolGuard
        self.web_guard = WebToolGuard("")
        self._build()

    def _build(self) -> None:
        from ai.mcp import tool_schema_to_openai_tool
        from ai.settings import env_bool
        if not env_bool("VIOLET_SOCIAL_TOOLS", True):
            return
        try:
            from ai.tool_manager import discover
            catalog = discover()
        except Exception:                                      # noqa: BLE001 -- no tools this time
            logger.exception("social tools: discovery failed")
            catalog = None
        for name, tool in sorted((catalog.tools if catalog else {}).items()):
            if "chat" not in tool.agents or tool.permission != "read" or tool.needs_approval:
                continue
            public = name.startswith("violet-web__")
            if not public and not self.owner_asked:
                continue
            self.schemas.append(tool_schema_to_openai_tool(name, tool.description, tool.input_schema))
            self.names.add(name)
        from ai.images.service import images_enabled
        if self.owner_asked and images_enabled():
            self.schemas.append(IMAGE_SCHEMA)
            self.names.add("create_image")

    def note(self) -> str | None:
        """What she is told about them (facts; added to the system prompt)."""
        if not self.names:
            return None
        lines = ["【这次能用的工具】需要实时信息（天气、新闻）、主人自己的数据（手机、课表）或者主人让你画图时，先调用合适的工具，"
                 "拿到结果再输出 JSON；查到的写进 messages 里，查不到就照实说。工具名和错误代码不要说出来。"]
        if not self.owner_asked:
            lines.append("这次找你的人不是主人：只有联网查公开信息可以用；主人的手机、课表这些私人数据不能查，也不画图。")
        else:
            lines.append("主人的手机、课表这类私人数据是主人自己在群里问的才说，只说他问的那一点。")
        if "create_image" in self.names:
            lines.append("画好的图会自动跟着你的 messages 发出去，messages 里不用写图片编号。")
        return "\n".join(lines)

    def run(self, name: str, args: dict[str, Any]) -> str:
        self.called.append(name)
        if name not in self.names:
            result = json.dumps({"ok": False, "error_code": "UNKNOWN_TOOL", "message": "这次不能用这个工具"}, ensure_ascii=False)
        else:
            denial = self.web_guard.check(name, args)
            if denial is not None:
                result = denial
            elif name == "create_image":
                result = self._image(args)
            else:
                result = self._mcp(name, args)
                self.web_guard.observe(name, result)
        outcome, why = trace.tool_outcome(result)
        trace.step("群里用工具", 工具=name, 参数=trace.text(args), 结果=outcome, 原因=why, 内容=trace.text(result))
        return result

    def _mcp(self, name: str, args: dict[str, Any]) -> str:
        from ai.chat import get_mcp_servers
        from ai.mcp import MCPToolBridge
        from ai.tool_manager import _run_sync

        async def call():
            async with MCPToolBridge(get_mcp_servers(), agent_name="chat") as bridge:
                return await bridge.call_tool(name, args)
        try:
            return _run_sync(call)
        except Exception as exc:                               # noqa: BLE001 -- told to the model
            return json.dumps({"ok": False, "error_code": "MCP_CALL_FAILED", "message": str(exc)[:200]}, ensure_ascii=False)

    def _image(self, args: dict[str, Any]) -> str:
        if self.image_asset_ids:
            return json.dumps({"ok": False, "error_code": "IMAGE_TURN_LIMIT", "message": "这次已经画过一张了"}, ensure_ascii=False)
        from ai.images.chat_tools import create_image
        args = {"subject": args.get("subject") or "other", "prompt": args.get("prompt") or "",
                **({"scene": "current"} if args.get("subject") == "violet" else {})}
        try:
            result = create_image(self.db, {"turn_key": self.turn_key}, args)
        except Exception as exc:                               # noqa: BLE001
            return json.dumps({"ok": False, "error_code": "IMAGE_FAILED", "message": str(exc)[:200]}, ensure_ascii=False)
        try:
            payload = json.loads(result)
        except ValueError:
            return result
        if payload.get("ok") and isinstance(payload.get("asset_id"), int):
            self.image_asset_ids.append(payload["asset_id"])
            payload = {"ok": True, "caption": payload.get("caption"),
                       "note": "画好了，会跟着你的消息发出去。这是 AI 画的插画，不是照片，不要说是拍的。"}
            return json.dumps(payload, ensure_ascii=False)
        return result
