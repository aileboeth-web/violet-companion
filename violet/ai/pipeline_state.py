"""
pipeline_state.py — 每一轮处理的流程记录，供后台“流程”页（/api/control/flow）和旧的 /api/pipeline-status 读取。

三种流程：
  - "chat"       : 你的消息    Intent → (Performer?) → Chat → Memory
  - "proactive"  : 主动联系    Proactive → (Performer?) → Chat
  - "social"     : QQ 群聊和别人私聊  Social（决定说不说、说什么）→ Act（发出去）

2026-11-02 (user: 做一个“流程”页，像 Eve 原来的 Intent 分析、Chat 生成那样): Eve kept ONE global state, so a QQ group
deciding in another thread overwrote your own turn. Now every run is its own record, bound to the thread that started
it (the owner pipeline runs in the turn worker, every social decision in its own timer thread); the last RUN_LIMIT runs
stay in memory (never written to disk, gone after a restart). Inputs and outputs are copied and clipped when recorded.
The module-level functions keep their old signatures; a call in a thread with no run started is ignored.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from typing import Any

RUN_LIMIT = 20
TEXT_LIMIT = 20_000          # one string (a system prompt is ~10k characters)
ITEM_LIMIT = 80              # items of one list / keys of one dict
DEPTH_LIMIT = 8

CHAT_NODES = ["Intent", "Performer", "Chat", "Memory"]
PROACTIVE_NODES = ["Proactive", "Performer", "Chat"]
SOCIAL_NODES = ["Social", "Act"]
NODES = {"chat": CHAT_NODES, "proactive": PROACTIVE_NODES, "social": SOCIAL_NODES}

_lock = threading.Lock()
_runs: deque[dict[str, Any]] = deque(maxlen=RUN_LIMIT)
_local = threading.local()
_ids = itertools.count(1)


def clip(value: Any, depth: int = 0) -> Any:
    """A JSON-safe copy, cut to size (the page shows it; nothing here may keep a live object alive)."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= DEPTH_LIMIT:
        return "…"
    if isinstance(value, str):
        return value if len(value) <= TEXT_LIMIT else value[:TEXT_LIMIT] + f"…（共 {len(value)} 字，后面省略）"
    if isinstance(value, dict):
        items = list(value.items())
        out = {str(k): clip(v, depth + 1) for k, v in items[:ITEM_LIMIT]}
        if len(items) > ITEM_LIMIT:
            out["…"] = f"还有 {len(items) - ITEM_LIMIT} 项"
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        out = [clip(v, depth + 1) for v in items[:ITEM_LIMIT]]
        if len(items) > ITEM_LIMIT:
            out.append(f"…还有 {len(items) - ITEM_LIMIT} 项")
        return out
    return clip(str(value), depth)


def _current() -> dict[str, Any] | None:
    return getattr(_local, "run", None)


def _node(run: dict[str, Any], node: str) -> dict[str, Any]:
    if node not in run["nodes"]:
        run["nodes"][node] = {"status": "pending", "input": None, "output": None, "started_at": None,
                              "finished_at": None, "note": None}
        if node not in run["order"]:
            run["order"].append(node)
    return run["nodes"][node]


def pipeline_start(pipeline_type: str, trigger: str, *, channel: str | None = None, turn_id: str | None = None,
                   title: str | None = None) -> int:
    nodes = NODES.get(pipeline_type, [])
    run = {"id": next(_ids), "pipeline_type": pipeline_type, "trigger": (trigger or "")[:120], "channel": channel,
           "turn_id": turn_id, "title": title, "running": True, "active_node": None, "started_at": time.time(),
           "finished_at": None, "error": None, "order": list(nodes), "edges": [], "nodes": {}}
    for n in nodes:
        _node(run, n)
    with _lock:
        _runs.append(run)
    _local.run = run
    return run["id"]


def node_start(node: str, input_data: Any = None) -> None:
    run = _current()
    if run is None:
        return
    data = clip(input_data)
    with _lock:
        n = _node(run, node)
        n.update(status="running", input=data, started_at=time.time())
        run["active_node"] = node


def node_done(node: str, next_node: str | None = None, output_data: Any = None) -> None:
    run = _current()
    if run is None:
        return
    data = clip(output_data)
    failed = isinstance(output_data, dict) and bool(output_data.get("error"))
    with _lock:
        n = _node(run, node)
        n.update(status="failed" if failed else "done", output=data, finished_at=time.time())
        if n["started_at"] is None:
            n["started_at"] = n["finished_at"]
        if run["active_node"] == node:
            run["active_node"] = None
        if next_node:
            edge = {"from": node, "to": next_node}
            if edge not in run["edges"]:
                run["edges"].append(edge)


def node_skip(node: str, reason: str | None = None) -> None:
    run = _current()
    if run is None:
        return
    with _lock:
        n = _node(run, node)
        if n["status"] in ("pending", "running"):
            n.update(status="skipped", note=reason)


def pipeline_end(error: str | None = None) -> None:
    run = _current()
    if run is None:
        return
    with _lock:
        for n in run["nodes"].values():
            if n["status"] == "running":
                n.update(status="failed" if error else "done", finished_at=time.time())
            elif n["status"] == "pending":
                n["status"] = "skipped"
        run.update(running=False, active_node=None, finished_at=time.time(), error=(error or None) and str(error)[:500])
    _local.run = None


def node_queue(node: str, reason: str | None = None) -> None:
    """2026-11-02 (user: 没有触发整理记忆这一环节): a step that runs later in the background (the memory job after
    the reply is sent) -- shown as queued, filled in by turn_node() when it runs; pipeline_end leaves it alone."""
    run = _current()
    if run is None:
        return
    with _lock:
        n = _node(run, node)
        n.update(status="queued", note=reason)


def turn_node(turn_id: str | None, node: str, status: str, input_data: Any = None, output_data: Any = None,
              note: str | None = None) -> None:
    """Update a step of the run of `turn_id` from another thread (the memory worker), also after the run ended."""
    if not turn_id:
        return
    data_in, data_out = clip(input_data), clip(output_data)
    with _lock:
        run = next((r for r in reversed(_runs) if r.get("turn_id") == turn_id), None)
        if run is None:
            return
        n = _node(run, node)
        now = time.time()
        if status == "running":
            n.update(status="running", started_at=now, finished_at=None)
        else:
            n.update(status=status, finished_at=now)
            if n["started_at"] is None:
                n["started_at"] = now
        if data_in is not None:
            n["input"] = data_in
        if data_out is not None:
            n["output"] = data_out
        if note is not None:
            n["note"] = note


# ------------------------------------------------------------------ reading
def _summary(run: dict[str, Any]) -> dict[str, Any]:
    return {k: run[k] for k in ("id", "pipeline_type", "trigger", "channel", "turn_id", "title", "running",
                                "active_node", "started_at", "finished_at", "error", "order", "edges")} | {
        "nodes": {name: {k: n[k] for k in ("status", "started_at", "finished_at", "note")}
                  for name, n in run["nodes"].items()}}


def recent() -> list[dict[str, Any]]:
    """The last runs, newest first, without inputs and outputs."""
    with _lock:
        return [_summary(r) for r in reversed(_runs)]


def get_run(run_id: int) -> dict[str, Any] | None:
    with _lock:
        for r in _runs:
            if r["id"] == run_id:
                return {**_summary(r), "nodes": {name: dict(n) for name, n in r["nodes"].items()}}
    return None


def get_state() -> dict[str, Any]:
    """The newest run in Eve's old shape (/api/pipeline-status)."""
    with _lock:
        run = _runs[-1] if _runs else None
        if run is None:
            return {"pipeline_type": None, "running": False, "active_node": None, "completed_nodes": [],
                    "skipped_nodes": [], "edges": [], "started_at": None, "finished_at": None, "trigger": "",
                    "error": None, "node_data": {}}
        return {"pipeline_type": run["pipeline_type"], "running": run["running"], "active_node": run["active_node"],
                "completed_nodes": [k for k, n in run["nodes"].items() if n["status"] in ("done", "failed")],
                "skipped_nodes": [k for k, n in run["nodes"].items() if n["status"] == "skipped"],
                "edges": list(run["edges"]), "started_at": run["started_at"], "finished_at": run["finished_at"],
                "trigger": run["trigger"], "error": run["error"],
                "node_data": {k: {"input": n["input"], "output": n["output"]} for k, n in run["nodes"].items()}}


def clear() -> None:
    """Tests only."""
    with _lock:
        _runs.clear()
    _local.run = None
