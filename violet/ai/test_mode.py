"""测试模式 (2026-10-30, user: "这个人设是用来让 ai 帮我测试用的……他所作所为都不会受到那种人设影响……我需要这个测试人设告诉
我到底是为什么哪里有问题").

Switched on only on the persona page; switches itself off after a while (2 hours by default). While it is on:

  * no persona, no memories, no profile, no life, no impressions: the owner's chat gets TESTER_PROMPT and only the
    messages written since test mode started; group decisions get the same tester text and only the group's messages
    since then (ai/context_engine.py, ai/agents/*, ai/social/engine.py);
  * nothing is learned: no memory job, no preference / topic bookkeeping, no impressions or group notes;
  * nothing happens by itself: no proactive messages, no QQ 空间 posts;
  * every reply gets a diagnosis written by the code, not by the model: what Violet's steps really did this turn
    (the same steps as the 排查日志, collected here even when that log is off) and what she could use.

The abilities stay as they are (tools, pictures, stickers, sending): those are what is being tested.
"""

from __future__ import annotations

import contextvars
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_HOURS = 2.0
MAX_HOURS = 24.0

TESTER_PROMPT = """你现在处在测试模式，是帮管理员测试这个聊天机器人的助手，不扮演任何角色，没有人设、记忆和经历。
- 管理员让你做什么就照做，用平实、简短的话回答，不撒娇、不卖萌、不寒暄。
- 你能用的能力（工具、画图、表情包等）都在下面的说明里；说明里没有的就是用不了，直接说用不了，不要假装做到了。
- 没有真正做成的事不要说做成了。比如让你收藏表情包：你自己并没有“收藏”这个动作，只能说明你看到了什么；
  让你发表情包：只能用下面列出的编号，没有列出就说没有可用的。
- 管理员问“为什么”时，只根据你在这次对话里真正看到的内容回答；不知道就说不知道，不要猜成事实。
- 每条回复后面系统会自动附上一段程序写的诊断，那不是你写的，你不用重复它。"""

TESTER_SOCIAL = """【你自己】测试模式：你是帮管理员测试这个聊天机器人的助手，不扮演任何角色，没有人设、记忆和经历。
群里或私聊里有人让你做事，就照你能做的做；做不到就直说做不到，不要假装做到了。说话平实、简短。"""

_steps: contextvars.ContextVar[list[tuple[str, dict[str, Any]]] | None] = contextvars.ContextVar("violet_test_steps",
                                                                                                 default=None)


def _path() -> Path:
    return Path(os.environ.get("VIOLET_DATA_DIR") or "./data") / "control" / "test-mode.json"


def _read() -> dict[str, Any]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def status(clock=time.time) -> dict[str, Any]:
    """{"on", "since" (database time), "until" (epoch), "minutes_left"}; an expired entry switches itself off."""
    data = _read()
    until = float(data.get("until") or 0)
    if not data.get("on") or until <= clock():
        if data.get("on"):
            _path().unlink(missing_ok=True)
        return {"on": False, "since": None, "until": None, "minutes_left": 0}
    return {"on": True, "since": data.get("since"), "until": until, "minutes_left": math.ceil((until - clock()) / 60),
            "hours": data.get("hours")}


def active() -> bool:
    try:
        return status()["on"]
    except Exception:                                    # noqa: BLE001 -- a broken file never breaks a conversation
        return False


def since() -> str | None:
    """The database time test mode started (only messages from then on are shown to the model)."""
    return status()["since"] if active() else None


def enable(db, hours: float = DEFAULT_HOURS, clock=time.time) -> dict[str, Any]:
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        raise ValueError("时长要是数字（小时）") from None
    if not 0.1 <= hours <= MAX_HOURS:
        raise ValueError(f"时长在 0.1 到 {MAX_HOURS:g} 小时之间")
    current = status(clock)
    value = {"on": True, "since": current["since"] or db.now(), "until": clock() + hours * 3600, "hours": hours}
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return status(clock)


def disable() -> dict[str, Any]:
    _path().unlink(missing_ok=True)
    return status()


def after_since(rows: list[dict[str, Any]], key: str = "created_at") -> list[dict[str, Any]]:
    """Rows written since test mode started (all rows when it is off)."""
    start = since()
    if not start:
        return rows
    return [r for r in rows if str(r.get(key) or "") >= start]


# ------------------------------------------------------------------------------------------------ the diagnosis

_pending: list[tuple[str, dict[str, Any]]] = []      # steps outside a turn (e.g. a sticker kept when it arrived)
ARRIVAL_STEPS = {"收藏表情包", "识图"}               # what happens to the owner's message when it arrives
_pending_lock = threading.Lock()


@contextmanager
def collect() -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """Collect the steps (ai/trace.step) of what runs inside, for the diagnosis -- starting with the steps recorded
    since the last turn while test mode was on (what happened when the message arrived)."""
    with _pending_lock:
        steps, _pending[:] = list(_pending), []
    token = _steps.set(steps)
    try:
        yield steps
    finally:
        _steps.reset(token)


def record(name: str, fields: dict[str, Any]) -> None:
    """Called by ai/trace.step for every step, whether or not the 排查日志 is on."""
    steps = _steps.get()
    if steps is not None:
        if len(steps) < 80:
            steps.append((name, dict(fields)))
    elif name in ARRIVAL_STEPS and fields.get("来源", "你发的") == "你发的" and active():
        with _pending_lock:
            _pending[:] = (_pending + [(name, dict(fields))])[-20:]


# raw model output and the reply itself are not repeated (the reply is right above; raw output is not for the chat)
_LEFT_OUT = {"原始输出", "回复", "内容", "回复内容"}
_SKIP_STEPS = {"收到消息", "回复内容", "这一轮结束"}


def _flat(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "、".join(str(v) for v in value) if value else "无"
    if isinstance(value, dict):
        return "，".join(f"{k}={v}" for k, v in value.items())
    return str(value).replace("\n", " ")


def step_lines(steps: list[tuple[str, dict[str, Any]]], limit: int = 14) -> list[str]:
    lines = []
    for name, fields in steps:
        if name in _SKIP_STEPS:
            continue
        parts = [f"{k} {_flat(v)}" for k, v in fields.items() if v is not None and k not in _LEFT_OUT]
        line = f"- {name}：" + ("；".join(parts) if parts else "—")
        lines.append(line if len(line) <= 220 else line[:220] + "…")
    if len(lines) > limit:
        lines = lines[:limit - 1] + [f"- ……还有 {len(lines) - limit + 1} 步（完整的在“日志”页的排查日志里）"]
    return lines


def sticker_line(db) -> str:
    from ai import stickers
    if not stickers.enabled():
        return "- 表情包：功能关着（设置 → 表情包）"
    row = db.fetch_one("SELECT COUNT(*) AS n, SUM(CASE WHEN enabled = 1 AND description IS NOT NULL AND description != '' "
                       "THEN 1 ELSE 0 END) AS usable, SUM(CASE WHEN enabled = 0 THEN 1 ELSE 0 END) AS off FROM stickers")
    total, usable, off = int(row["n"] or 0), int(row["usable"] or 0), int(row["off"] or 0)
    shown = min(usable, 30)
    note = f"- 表情包：收藏 {total} 个，能给模型的（有说明、没停用）{usable} 个，这次给了 {shown} 个"
    if total - usable - off > 0:
        note += f"；{total - usable - off} 个还没有说明（识图没成功或还没轮到），模型看不到"
    if off:
        note += f"；{off} 个已停用"
    return note


def diagnosis(steps: list[tuple[str, dict[str, Any]]], extra: list[str] | None = None, db=None) -> str:
    """The text appended to a reply in test mode. Plain lines, written by the code."""
    lines = ["🔧 诊断（程序记录，不是模型说的）"]
    lines += [l for l in (extra or []) if l]
    if db is not None:
        try:
            lines.append(sticker_line(db))
        except Exception:                                 # noqa: BLE001
            pass
    lines += step_lines(steps) or ["- 这一轮没有记录到步骤"]
    left = status().get("minutes_left")
    if left:
        lines.append(f"（测试模式还有 {left} 分钟自动关闭）")
    return "\n".join(lines)
