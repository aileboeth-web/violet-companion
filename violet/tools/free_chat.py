"""Phase 8.9.6 free chat: talk to Violet yourself, through her REAL product pipeline, in a session that persists.

    python -m tools.free_chat chat --session NAME [--seed-episode A4 --episodes-file F] [--start ISO]
    python -m tools.free_chat chat --session NAME --real --api-url https://.../v1 --model cys
    python -m tools.free_chat review --session NAME          # mark rounds afterwards (offline, no model)

A session is a directory (default local/free_chat/NAME) with its own test database (violet/), the scenario clock and
an append-only record of everything that happened: events.jsonl (each user round, each passage of time with the
scheduler checks in it -- calls, latency, tokens, errors, recovered / degraded / failed, intent decisions, guard
rewrites, the raw model output next to what was sent, state changes), requests.jsonl (the whole Intent / Chat /
Proactive requests), feedback.jsonl (the tester's marks), transcript.md and feedback.md (rewritten on exit).
Launching again with the same NAME continues the same conversation, database and clock.

Isolation, the real-API gate (VIOLET_TEST_ALLOW_REAL_API=1, --real, the hidden key prompt, "yes"), the budget and the
network allow-list are the ones of tools.harness: no WeChat, no production data, no MCP / web / images / life engine.
--model sets every Violet text role to one model name (e.g. the relay's "cys" mapping); a session keeps its mode
(mock / real) and model for good.

Time: /advance lets hours pass the way the running scheduler would see them -- it checks whenever its own next time
comes (and every 10 minutes while a gate is still closed), never forces a message. /check makes the scheduler's own
random wait count as elapsed now; every other gate still applies and the model still decides.
"""
from __future__ import annotations

import argparse
import getpass
import json

from tools.legacy import upgrade
import os
import sys
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
SESSION_ROOT = ROOT / "local" / "free_chat"
TOOL_VERSION = "p896.v1"
META, EVENTS, REQUESTS, FEEDBACK = "session.json", "events.jsonl", "requests.jsonl", "feedback.jsonl"
TRANSCRIPT, FEEDBACK_MD, LOCK = "transcript.md", "feedback.md", ".lock"
USAGE = "usage.jsonl"                       # phase 9.1.1: one line per model call, written as the call is recorded
ROLES = ("INTENT", "CHAT", "MEMORY", "PROACTIVE", "PERFORMER")
# phase 8.9.8: prompt versions to compare. v1 = the stable prompts of env_example (left as they are); other versions
# come from the versioned files and replace them for this session only.
PROMPT_VERSIONS = {"chat": ("CHAT_LLM_PROMPT", PROJECT / "persona" / "chat_prompt_versions", "chat.{}.txt"),
                   "proactive": ("PROACTIVE_LLM_PROMPT", PROJECT / "persona" / "proactive_prompt_versions", "proactive.{}.txt")}
CHECK_STEP = timedelta(minutes=10)          # while a gate stays closed (the scheduler thread checks every minute)
MAX_CHECKS = 400                            # per /advance: about 2.5 days of 10-minute checks
TAGS = {"good": "自然、可爱", "flat": "机械、重复或缺少亲近感", "conflict": "生活经历或时间矛盾",
        "missed": "该主动却没有主动", "note": "备注"}
ALIASES = {"好": "good", "自然": "good", "可爱": "good", "机械": "flat", "重复": "flat", "生硬": "flat", "不亲近": "flat",
           "矛盾": "conflict", "该主动": "missed", "没主动": "missed", "备注": "note"}
REASONS = {"due": "到时间了", "not due yet": "还没到它自己定的时间", "quiet hours": "静默时段",
           "too soon after user message": "离对方上一句太近", "too soon after proactive message": "离上一条主动消息太近",
           "next proactive time initialized": "刚定下第一次检查的时间", "daily proactive limit reached": "今天的主动次数用完了",
           "the user asked Violet not to start conversations": "对方说过不要主动找"}
LIFE_OUTCOMES = {"quiet": "这天没什么特别的", "quiet_weekly_budget": "这周的生活记录已经够多了", "quiet_no_thread": "没有在进行的事",
                 "quiet_nothing_new": "没有新进展", "rejected": "提出的事被一致性检查拒绝", "disabled": "关闭",
                 "already_ran": "这天已经运行过", "error": "出错"}
HELP = """命令：
  直接输入文字      发给 Violet
  /advance 3        让时间过去 3 小时（也可以 40m、1d、2h30m）；调度器照常检查，不强制发消息
  /check            让调度器现在检查一次（假定它的随机等待已到；其他条件照常，发不发由模型决定）
  /mark 标签 [#n] [备注]   标记某一轮（默认上一轮）；标签：好/机械/矛盾/该主动/备注（good/flat/conflict/missed/note）
  /show [n]         看某一轮的细节：原始输出、守卫、调用、主动联系的四个环节
  /state            档案、记忆、约定、Violet 的生活        /time   现在的模拟时间
  /usage            上一轮、本次启动和会话累计的调用、token（真实模型会话还有累计上限和剩余）   /help   这段说明
  /quit             结束（Ctrl+C 也可以），记录会保存，下次用同一个 --session 接着聊
虚构工具会话（--fake-tools on）另有：
  /tool off get_weather   停用一个虚构工具（和后台“工具”页同一份设置，下一轮生效）；/tool on get_weather 重新启用
  /fake-mode error        虚构服务器之后的调用都报错（error / crash / slow / normal）；/fake-mode normal 恢复
虚构课表会话（--schedule on）：
  /schedule               课表闸门现在拦不拦、今天和明天的安排、这次检查能提供的课表背景、最近几次主动联系的结果和原因
  /schedule cancel [日期]          列出那天可以停的课（默认今天；不改任何东西）
  /schedule cancel 2026-10-30 08:00   先显示要停的那一次课，输入 yes 才停（只改这个会话的虚构课表）"""
FAKE_SERVER, FAKE_TOOLS, FAKE_MODES = "campus", ("get_course_schedule", "get_weather", "search_web"), \
    ("normal", "error", "crash", "slow")
FAKE_MODE_FILE = "fake_mcp_mode.txt"
# phase 9.3 (--web on): the real violet-web tools (search provider + public pages) in a real-model session
WEB_USAGE = "web_usage.jsonl"               # one line per real HTTP connection of the web tools (or its refusal)
WEB_PROVIDERS = ("bocha", "brave")
WEB_KINDS = {"search": "max_search_requests", "page": "max_page_requests"}
HELP_WEB = """联网会话（--web on）：每轮显示 Intent 的联网判断、搜索词、读了哪个网页、实际 HTTP 请求和回复里的链接；
  /usage 里有联网请求的会话累计和剩余"""


class SessionError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------ session files

def session_dir(name_or_path: str) -> Path:
    path = Path(name_or_path)
    path = (path if path.is_absolute() or len(path.parts) > 1 else SESSION_ROOT / path).resolve()
    if path == PROJECT or PROJECT in path.parents:
        raise SessionError(f"会话目录不能在 Violet 里面：{path}")
    return path


class SessionLock:
    """One process per session: an OS lock on .lock, released by the OS even if the window is closed."""

    def __init__(self, path: Path):
        self.path, self.handle = path, None

    def acquire(self) -> "SessionLock":
        self.handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise SessionError(f"这个会话正在另一个窗口里使用：{self.path.parent}")
        return self

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.handle.close()
        self.handle = None


def read_meta(path: Path) -> dict | None:
    f = path / META
    return upgrade(json.loads(f.read_text(encoding="utf-8"))) if f.is_file() else None


def write_meta(path: Path, meta: dict, scrub: Callable[[str], str] = lambda s: s) -> None:
    tmp = path / (META + ".tmp")
    tmp.write_text(scrub(json.dumps(meta, ensure_ascii=False, indent=1, default=str)), encoding="utf-8")
    os.replace(tmp, path / META)


def append_jsonl(path: Path, rows: list[dict], scrub: Callable[[str], str] = lambda s: s) -> None:
    if not rows:
        return
    with path.open("a", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(scrub(json.dumps(row, ensure_ascii=False, default=str)) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(upgrade(json.loads(line)))
            except ValueError:                       # a line cut by a crash: everything before it stays readable
                continue
    return rows


# ------------------------------------------------------------------------------------------------ budget (9.1.1)
# Phase 9.1 real test: --max-calls / --max-tokens were per launch only, so every restart got the full amount again and
# the session ended 478 tokens above its 200000 cap. Now a real-model session has caps of its own (session_budget in
# session.json), a launch can only use what is left of them, and usage is counted from usage.jsonl -- one line per call,
# written when the call is recorded, so a launch whose window was closed is still counted in full.

def ledger_usage(path: Path, meta: dict | None) -> dict:
    """Calls / tokens of the launches so far: the per-call ledger where it has lines for a launch, else that launch's
    row in session.json (sessions from before 9.1.1, updated after every round)."""
    by_launch: dict[int, dict] = {}
    sizes: dict[str, int] = {}
    for row in read_jsonl(path / USAGE):
        got = by_launch.setdefault(int(row.get("launch") or 0), {"calls": 0, "tokens": 0})
        got["calls"] += 1
        got["tokens"] += int(row.get("tokens") or 0)
        if row.get("agent") and row.get("ok"):
            sizes[row["agent"]] = max(sizes.get(row["agent"], 0), int(row.get("tokens") or 0))
    launches, calls, tokens = [], 0, 0
    for launch in (meta or {}).get("launches") or []:
        got = by_launch.get(int(launch["n"]))
        row = ({"n": launch["n"], "calls": got["calls"], "tokens": got["tokens"], "source": "usage.jsonl"} if got else
               {"n": launch["n"], "calls": int(launch.get("calls") or 0), "tokens": int(launch.get("tokens") or 0),
                "source": "session.json"})
        launches.append(row)
        calls, tokens = calls + row["calls"], tokens + row["tokens"]
    return {"calls": calls, "tokens": tokens, "launches": launches, "sizes": sizes}


def session_budget(meta: dict | None, args, new: bool, mode: str) -> dict | None:
    """The caps of the whole session (real model only; a mock session costs nothing). Set when the session is created
    (--session-max-calls / --session-max-tokens, else that first launch's --max-calls / --max-tokens); an older
    session takes its first launch's caps. Later launches may lower them, never raise them."""
    if mode != "real_model":
        return None
    asked = {"max_calls": getattr(args, "session_max_calls", None), "max_tokens": getattr(args, "session_max_tokens", None)}
    if new:
        return {"max_calls": asked["max_calls"] or args.max_calls, "max_tokens": asked["max_tokens"] or args.max_tokens,
                "origin": "set_at_creation"}
    stored = dict((meta or {}).get("session_budget") or {})
    if not stored:
        first = ((meta or {}).get("launches") or [{}])[0].get("budget") or {}
        stored = {"max_calls": int(first.get("max_calls_total") or args.max_calls),
                  "max_tokens": int(first.get("max_tokens_total") or args.max_tokens), "origin": "first_launch_caps"}
    for key, value in asked.items():
        if value is None:
            continue
        if value > stored[key]:
            raise SessionError(f"会话累计上限只能调低：{key} 现在是 {stored[key]}，不能改成 {value}（需要更多额度请新建会话）")
        stored[key], stored["origin"] = value, "lowered"
    return stored


def launch_caps(args, session_caps: dict | None, used: dict) -> tuple[int, int]:
    """(calls, tokens) this launch may use: its own caps, never more than what is left of the session's."""
    if session_caps is None:
        return args.max_calls, args.max_tokens
    calls_left = session_caps["max_calls"] - used["calls"]
    tokens_left = session_caps["max_tokens"] - used["tokens"]
    if calls_left <= 0 or tokens_left <= 0:
        raise SessionError(f"会话累计预算已用完：已用 {used['calls']} 次调用 / {used['tokens']} token，"
                           f"上限 {session_caps['max_calls']} 次 / {session_caps['max_tokens']} token")
    smallest = min(used.get("sizes", {}).values(), default=0)
    if tokens_left < smallest:
        raise SessionError(f"会话累计预算已用完：剩余 {tokens_left} token，不够之前最小的一次调用（{smallest} token）")
    return min(args.max_calls, calls_left), min(args.max_tokens, tokens_left)


def budget_lines(launch_calls: int, launch_tokens: int, session_caps: dict | None, used: dict) -> list[str]:
    lines = [f"本次启动最多：{launch_calls} 次调用 / {launch_tokens} token"]
    if session_caps is not None:
        lines.append(f"会话累计：之前已用 {used['calls']} 次 / {used['tokens']} token；上限 {session_caps['max_calls']} 次 / "
                     f"{session_caps['max_tokens']} token；剩余 {session_caps['max_calls'] - used['calls']} 次 / "
                     f"{session_caps['max_tokens'] - used['tokens']} token")
    return lines


# ------------------------------------------------------------------------------------------------ web (9.3)
# --web on: the real violet-web tools (ai/web: SafeFetcher, WebService, the configured provider) through the normal
# Intent -> plan -> Chat path (Tool Manager, on_demand, 9.2.2 required / auto). Real HTTP connections of the web tools
# are capped for the whole session (web_budget in session.json, like session_budget) and counted from web_usage.jsonl,
# one line per connection written before it is opened -- so a closed window or a new launch never resets them.

def web_ledger(path: Path) -> dict:
    used = {kind: 0 for kind in WEB_KINDS}
    refused = 0
    for row in read_jsonl(path / WEB_USAGE):
        if row.get("refused"):
            refused += 1
        elif row.get("kind") in used:
            used[row["kind"]] += 1
    return {**used, "refused": refused}


def web_budget(meta: dict | None, args, new: bool) -> dict:
    """The web caps of the whole session: set when it is created (--session-max-searches / --session-max-pages),
    later launches may lower them, never raise them."""
    asked = {"max_search_requests": getattr(args, "session_max_searches", None),
             "max_page_requests": getattr(args, "session_max_pages", None)}
    if new:
        return {"max_search_requests": asked["max_search_requests"] or 10,
                "max_page_requests": asked["max_page_requests"] or 20, "origin": "set_at_creation"}
    stored = dict((meta or {}).get("web_budget") or {})
    for key, value in asked.items():
        if value is None:
            continue
        if value > stored[key]:
            raise SessionError(f"联网累计上限只能调低：{key} 现在是 {stored[key]}，不能改成 {value}（需要更多额度请新建会话）")
        stored[key], stored["origin"] = value, "lowered"
    return stored


def web_lines(caps: dict, used: dict) -> list[str]:
    return [f"联网（会话累计，按实际 HTTP 请求计，含跳转）：搜索 {used['search']} / {caps['max_search_requests']}，"
            f"网页 {used['page']} / {caps['max_page_requests']}；剩余 搜索 {caps['max_search_requests'] - used['search']}，"
            f"网页 {caps['max_page_requests'] - used['page']}" + (f"；被上限拒绝 {used['refused']} 次" if used["refused"] else "")]


def open_web_gate(provider: str, caps: dict, used: dict, input_fn: Callable[[str], str],
                  key_prompt: Callable[[str], str], out: Callable[[str], None] = print) -> str:
    """The search key (VIOLET_TEST_SEARCH_API_KEY or the hidden prompt) and a second, separate "yes" for real web use."""
    from ai.web.providers import BochaProvider, BraveProvider
    from tools.harness import mask
    key = os.environ.get("VIOLET_TEST_SEARCH_API_KEY", "").strip()
    if not key:
        key = (key_prompt(f"{provider} 搜索 API Key（不会显示，也不会保存）: ") or "").strip()
    if len(key) < 8:
        raise PermissionError("real web refused: no search key (VIOLET_TEST_SEARCH_API_KEY or the prompt)")
    endpoint = (BochaProvider if provider == "bocha" else BraveProvider).default_endpoint
    out("=" * 72)
    out("即将真实联网（Violet 自己的 violet-web 工具：搜索服务 + 公开网页；只读）")
    out(f"  搜索服务: {provider}（{urlsplit(endpoint).hostname}）  Key: {mask(key)}")
    for line in web_lines(caps, used):
        out("  " + line)
    out("  何时联网由 Intent 决定（on_demand）；网页直连、不用代理，内网 / 本机地址一律拒绝；达到上限后联网工具报错，不会重试")
    out("=" * 72)
    if input_fn("确认开始真实联网？输入 yes 继续: ").strip().lower() != "yes":
        raise PermissionError("real web not confirmed")
    return key


def install_web(path: Path, launch: int, recorder, guard, caps: dict, trace: list[dict] | None) -> Callable[[], None]:
    """Counts and caps every real connection of the web tools, and lets exactly those through the network guard.
    SafeFetcher.fetch marks what the connection is for (the provider's trusted host = search, else a page); the
    dialer -- called only after SafeFetcher checked the pinned address -- checks again that it is public and on
    80 / 443, writes the ledger line, and asks the guard to pass this one connect. Returns the undo callable."""
    import ai.web.netguard as ng
    state = threading.local()
    used = web_ledger(path)
    original_fetch, original_dialer = ng.SafeFetcher.fetch, ng._default_dialer

    def fetch(self, url, *args, **kwargs):
        state.kind = "search" if kwargs.get("trusted_host") else "page"
        state.host = (urlsplit(str(url)).hostname or "?").lower()
        try:
            return original_fetch(self, url, *args, **kwargs)
        finally:
            state.kind = None

    def dial(ip, port, timeout):
        kind = getattr(state, "kind", None) or "page"
        row = {"launch": launch, "round": recorder.round, "kind": kind, "host": getattr(state, "host", "?"), "port": port,
               "at": datetime.now(timezone.utc).isoformat()}
        reason = None
        if port not in (80, 443) or not ng.is_public_ip(str(ip)):
            reason = "address"
        elif used[kind] >= caps[WEB_KINDS[kind]]:
            reason = "session_cap"
        what = "搜索" if kind == "search" else "网页"
        if reason:
            used["refused"] += 1
            append_jsonl(path / WEB_USAGE, [{**row, "refused": reason}], recorder.scrub)
            if trace is not None:
                trace.append({**row, "kind": "web_http", "what": what, "refused": reason})
            raise ConnectionRefusedError(f"free_chat web: {reason}")
        used[kind] += 1
        append_jsonl(path / WEB_USAGE, [{**row, "refused": None, "n_of_kind": used[kind]}], recorder.scrub)
        if trace is not None:
            trace.append({**row, "kind": "web_http", "what": what, "refused": None, "n_of_kind": used[kind]})
        guard.pass_next_connect()
        try:
            return original_dialer(ip, port, timeout)
        finally:
            guard.clear_pass()

    ng.SafeFetcher.fetch, ng._default_dialer = fetch, dial

    def undo() -> None:
        ng.SafeFetcher.fetch, ng._default_dialer = original_fetch, original_dialer
    return undo


def parse_duration(text: str) -> timedelta:
    from tools.chat_dialogue import parse_after
    try:
        hours = float(text)
    except ValueError:
        return parse_after(text)
    if hours <= 0:
        raise ValueError("时间只能往前走")
    return timedelta(hours=hours)


# ------------------------------------------------------------------------------------------------ feedback

def parse_mark(args: str, last_n: int | None) -> tuple[str, int | None, str]:
    """'/mark 机械 #5 又在问今天怎么样' -> ("flat", 5, "又在问今天怎么样"); the round defaults to the last one."""
    parts = args.split(maxsplit=1)
    if not parts:
        raise ValueError("用法：/mark 标签 [#n] [备注]")
    tag = ALIASES.get(parts[0], parts[0].lower())
    if tag not in TAGS:
        raise ValueError(f"不认识的标签 {parts[0]}；可以用：好、机械、矛盾、该主动、备注")
    rest = parts[1] if len(parts) > 1 else ""
    n = last_n
    words = rest.split(maxsplit=1)
    if words and words[0].lstrip("#").isdigit():
        n, rest = int(words[0].lstrip("#")), (words[1] if len(words) > 1 else "")
    if n is None:
        raise ValueError("还没有可以标记的轮次")
    return tag, n, rest.strip()


def brief(event: dict | None) -> dict | None:
    if not event:
        return None
    out = {"n": event["n"], "kind": event["kind"], "at": event.get("at")}
    if event["kind"] == "user":
        out.update(user=event.get("user"), violet=event.get("violet"))
    else:
        out.update(proactive_sent=[t for c in event.get("checks") or [] for t in c.get("proactive_sent") or []],
                   reminder_sent=[t for c in event.get("checks") or [] for t in c.get("reminder_sent") or []])
    return out


def feedback_row(events: list[dict], tag: str, n: int, note: str, during: str) -> dict:
    by_n = {e["n"]: e for e in events}
    if n not in by_n:
        raise ValueError(f"没有第 {n} 轮")
    earlier = [e for e in events if e["n"] < n]
    return {"n": n, "tag": tag, "label": TAGS[tag], "note": note, "during": during,
            "marked_at": datetime.now(timezone.utc).isoformat(), "event": brief(by_n[n]),
            "before": brief(earlier[-1]) if earlier else None}


# ------------------------------------------------------------------------------------------------ transcript

def _hm(iso: str | None) -> str:
    return datetime.fromisoformat(iso).strftime("%H:%M") if iso else "--:--"


def chain_line(chain: dict) -> str:
    from tools.chat_dialogue import chain_md
    line = chain_md(chain).strip("_")
    for en, zh in REASONS.items():
        line = line.replace(f"（{en}）", f"（{zh}）")
    return line


def render_transcript(path: Path) -> str:
    from tools.chat_dialogue import WEEKDAYS, _gap
    meta, events, marks = read_meta(path) or {}, read_jsonl(path / EVENTS), read_jsonl(path / FEEDBACK)
    by_n: dict[int, list[dict]] = {}
    for m in marks:
        by_n.setdefault(m["n"], []).append(m)
    user = meta.get("user_name") or "你"
    out = [f"# 自由聊天：{path.name}", "",
           f"_{meta.get('mode')}；模型：{meta.get('model') or '默认'}；提示词："
           f"{'、'.join(f'{k} {v}' for k, v in (meta.get('prompt_versions') or {'chat': 'v1', 'proactive': 'v1'}).items())}；"
           f"生活引擎：{meta.get('life', 'off')}；"
           f"开始于模拟时间 {meta.get('scenario_start')}；"
           f"启动 {len(meta.get('launches') or [])} 次_", ""]
    if meta.get("seed"):
        out += [f"_起点：合成剧本 {meta['seed']['episode']} 的背景（{meta['seed']['title']}）_", ""]
    day = prev = None
    launch = None
    for e in events:
        at = datetime.fromisoformat(e["at"])
        if e.get("launch") != launch:
            launch = e.get("launch")
            out += ["", f"--- 第 {launch} 次启动 ---", ""]
        if prev is not None and at - prev >= timedelta(minutes=10) and e["kind"] == "user":
            out.append(f"⋯⋯ 过了 {_gap(at - prev)} ⋯⋯")
        if day != at.date():
            out += ["", f"**{at.month}月{at.day}日（周{WEEKDAYS[at.weekday()]}）**", ""]
            day = at.date()
        if e["kind"] == "user":
            note = f"（回复第 {e['after_proactive']['n']} 条主动消息）" if e.get("after_proactive") else ""
            out.append(f"`#{e['n']} {at:%H:%M}` **{user}**：{e['user']}{note}")
            for t in e.get("violet") or []:
                out.append(f"`{_hm(e.get('violet_at'))}` **Violet**：{t}")
            if not e.get("violet"):
                out.append("`  ·  ` _Eve 没有回复" + ("（判断为不需要回复）" if e.get("reply") == "silent_by_intent" else "") + "_")
            prev = datetime.fromisoformat(e.get("violet_at") or e["at"])
        else:
            what = "让时间过去" if e["kind"] == "time" else "让调度器现在检查（假定随机等待已到）"
            span = _span(e.get("from"), e.get("to")) if e["kind"] == "time" else _hm(e["at"])
            out.append(f"`#{e['n']}` _{what}：{span}；调度检查 {len(e.get('checks') or []) + sum((e.get('idle_checks') or {}).values())} 次_")
            for c in e.get("checks") or []:
                for t in c.get("reminder_sent") or []:
                    out.append(f"`{_hm(c['at'])}` **Violet（提醒）**：{t}")
                for t in c.get("proactive_sent") or []:
                    out.append(f"`{_hm(c['at'])}` **Violet（主动）**：{t}")
                if c.get("proactive_chain") and c["proactive_chain"].get("model_called"):
                    out.append(f"`  ·  ` _{chain_line(c['proactive_chain'])}_")
            for lr in e.get("life_runs") or []:              # phase 8.9.12: her life engine
                for ev in lr.get("events") or []:
                    out.append(f"`{_hm(ev.get('occurred_at'))}` _Eve 的生活（记录）：{ev['content']}_")
                if not lr.get("events"):
                    out.append(f"`  ·  ` _生活引擎 {lr.get('run_key')}：{LIFE_OUTCOMES.get(lr.get('outcome'), lr.get('outcome'))}_")
            if e.get("idle_checks"):
                out.append("`  ·  ` _其余检查没有调用模型：" + "；".join(
                    f"{REASONS.get(k, k)} ×{v}" for k, v in e["idle_checks"].items()) + "_")
            prev = datetime.fromisoformat(e.get("to") or e["at"])
        if e.get("status") == "recovered":
            out.append(f"ℹ 第 {e['n']} 轮有模型调用出错，Violet 的重试已恢复")
        elif e.get("status") == "degraded":
            out.append(f"⚠ 第 {e['n']} 轮降级：IntentAgent 重试用完，产品按设计改走普通聊天，ChatAgent 正常回复")
        elif e.get("status") == "failed":
            out.append(f"⚠ 第 {e['n']} 轮没有正常完成：{'；'.join(e.get('problems') or ['原因未记录'])}")
        for m in by_n.get(e["n"], []):
            out.append(f"🏷 {m['label']}" + (f"：{m['note']}" if m.get("note") else ""))
    return "\n".join(out) + "\n"


def write_readable(path: Path, scrub: Callable[[str], str] = lambda s: s) -> None:
    """transcript.md and feedback.md from the records (phase 8.9.8: after every round and mark, not only on /quit --
    the first real session was closed with the window and had no transcript)."""
    for name, render in ((TRANSCRIPT, render_transcript), (FEEDBACK_MD, render_feedback)):
        tmp = path / (name + ".tmp")
        tmp.write_text(scrub(render(path)), encoding="utf-8")
        os.replace(tmp, path / name)


def _span(start: str | None, end: str | None) -> str:
    """'20:46 → 23:46', or with dates when the time passed midnight ('11-06 23:51 → 11-07 23:51')."""
    if not (start and end):
        return f"{_hm(start)} → {_hm(end)}"
    a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
    return f"{a:%H:%M} → {b:%H:%M}" if a.date() == b.date() else f"{a:%m-%d %H:%M} → {b:%m-%d %H:%M}"


def render_feedback(path: Path) -> str:
    marks = read_jsonl(path / FEEDBACK)
    out = [f"# 体验反馈：{path.name}", "", f"共 {len(marks)} 条标记。每条都带着被标记的那一轮和它前面的一轮。", ""]
    for tag, label in TAGS.items():
        rows = [m for m in marks if m["tag"] == tag]
        if not rows:
            continue
        out += [f"## {label}（{len(rows)}）", ""]
        for m in rows:
            out.append(f"- **#{m['n']}**" + (f"：{m['note']}" if m.get("note") else ""))
            for ctx, name in ((m.get("before"), "前一轮"), (m.get("event"), "这一轮")):
                if not ctx:
                    continue
                if ctx["kind"] == "user":
                    out.append(f"  - {name} #{ctx['n']} {_hm(ctx.get('at'))} 用户：{ctx.get('user')} / Violet：{' '.join(ctx.get('violet') or []) or '（没有回复）'}")
                else:
                    sent = (ctx.get("proactive_sent") or []) + (ctx.get("reminder_sent") or [])
                    out.append(f"  - {name} #{ctx['n']} 时间推进 / 调度检查：{' '.join(sent) or 'Violet 没有主动发消息'}")
        out.append("")
    return "\n".join(out) + "\n"


TRACED_AGENTS = ("IntentAgent", "ChatAgent", "PerformerAgent", "ProactiveAgent")


def install_tool_trace(recorder, trace: list[dict]) -> Callable[[], None]:
    """Phase 9.2 real-model check (--fake-tools on): each step of a tool turn, so a wrong answer can be traced to
    Intent (tool_query), the plan, the tools a model call was offered (and tool_choice), the calls the model made
    (with arguments), or what the MCP server returned. Scrubbed; the returned function undoes the patches."""
    import ai.tool_manager as tm
    from ai.mcp import MCPToolBridge
    from tools import harness as harness_mod
    from tools.harness import _AGENT

    def add(kind: str, **row) -> None:
        row = json.loads(recorder.scrub(json.dumps(row, ensure_ascii=False, default=str)))
        trace.append({"round": recorder.round, "kind": kind, **row})

    original_discover, original_plan = tm.discover, tm.plan_for_turn
    original_create, original_call = harness_mod.BudgetedClient._create, MCPToolBridge.call_tool

    def traced_discover(*a, **kw):
        catalog = original_discover(*a, **kw)
        add("catalog", tools=sorted(catalog.tools), disabled=sorted(catalog.disabled),
            unavailable=[{"server": u.get("server"), "error_code": u.get("error_code")} for u in catalog.unavailable])
        return catalog

    def traced_plan(intent, catalog):
        plan = original_plan(intent, catalog)
        add("plan", route=intent.get("route"), tool_query=intent.get("tool_query"), plan=plan)
        return plan

    def traced_create(client_self, **kwargs):
        agent = _AGENT.get()
        if agent not in TRACED_AGENTS:
            return original_create(client_self, **kwargs)
        offered = [t["function"]["name"] for t in kwargs.get("tools") or []
                   if isinstance(t, dict) and isinstance(t.get("function"), dict)]
        row = {"agent": agent, "offered": offered, "tool_choice": kwargs.get("tool_choice")}
        try:
            response = original_create(client_self, **kwargs)
        except Exception as exc:
            add("model_call", **row, error=exc.__class__.__name__, status_code=getattr(exc, "status_code", None))
            raise
        choice = response.choices[0] if getattr(response, "choices", None) else None
        message = getattr(choice, "message", None)
        add("model_call", **row, finish_reason=getattr(choice, "finish_reason", None),
            tool_calls=[{"name": tc.function.name, "arguments": tc.function.arguments}
                        for tc in (getattr(message, "tool_calls", None) or [])])
        return response

    async def traced_call(bridge, tool_name, arguments):
        result = await original_call(bridge, tool_name, arguments)
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict) and data.get("ok") is False:
            outcome = {"ok": False, **{k: data.get(k) for k in ("status", "error_code", "message")}}
        else:
            limit = 3000 if str(tool_name).startswith("violet-schedule__") else 400   # 9.4.4: the whole (fictional) answer
            outcome = {"ok": True, "preview": str(result)[:limit]}
            web = web_summary(result) if str(tool_name).startswith("violet-web__") else None
            if web is not None:                      # phase 9.3: what the web tool really returned, field by field
                outcome = {"ok": web.get("ok") is not False, "web": web,
                           **({"error_code": web.get("error_code"), "message": web.get("message")}
                              if web.get("ok") is False else {})}
        add("mcp_call", agent=bridge.agent_name, tool=tool_name, arguments=arguments, **outcome)
        return result

    import ai.web.citations as citations
    original_checked = citations.checked_reply

    def traced_checked(chat_result, user_message, extra_texts=()):
        out = original_checked(chat_result, user_message, extra_texts)
        from ai.web.citations import URL_RE
        kept, removed = URL_RE.findall(out.get("content") or ""), list(out.get("unverified_links_removed") or [])
        if kept or removed:
            add("reply_links", kept=kept, removed=removed)
        return out

    tm.discover, tm.plan_for_turn = traced_discover, traced_plan
    harness_mod.BudgetedClient._create, MCPToolBridge.call_tool = traced_create, traced_call
    citations.checked_reply = traced_checked

    def undo() -> None:
        tm.discover, tm.plan_for_turn = original_discover, original_plan
        harness_mod.BudgetedClient._create, MCPToolBridge.call_tool = original_create, original_call
        citations.checked_reply = original_checked
    return undo


def web_summary(result: str) -> dict | None:
    """The JSON inside an violet-web tool result (wrapped as untrusted external data), reduced to the fields that matter
    for the check: per search result title / url / source / published_at; per page url / title / dates / size."""
    text = str(result or "")
    start = text.find("{")
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return {"unparsed": True, "chars": len(text)}
    if not isinstance(data, dict):
        return None
    if data.get("ok") is False:
        return {"ok": False, "error_code": data.get("error_code"), "message": data.get("message")}
    if "results" in data:
        return {"ok": True, "provider": data.get("provider"), "query": data.get("query"), "cached": bool(data.get("cached")),
                "empty": bool(data.get("empty")), "retrieved_at": data.get("retrieved_at"),
                "results": [{k: r.get(k) for k in ("title", "url", "source", "published_at", "published_verified")}
                            for r in data.get("results") or [] if isinstance(r, dict)]}
    return {"ok": True, **{k: data.get(k) for k in ("url", "domain", "title", "published_at", "published_verified",
                                                   "retrieved_at", "truncated", "injection_removed", "note")},
            "text_chars": len(data.get("text") or "")}


def tool_trace_line(t: dict) -> str:
    kind = t.get("kind")
    if kind == "catalog":
        return ("工具目录：可用 " + ("、".join(t["tools"]) or "无") + (f"；停用 {'、'.join(t['disabled'])}" if t["disabled"] else "")
                + (f"；连不上 {json.dumps(t['unavailable'], ensure_ascii=False)}" if t["unavailable"] else ""))
    if kind == "plan":
        plan = t.get("plan") or {}
        return (f"工具判断：route={t.get('route')} tool_query={json.dumps(t.get('tool_query'), ensure_ascii=False)} → "
                f"计划 {plan.get('mode')}" + (f"（{plan.get('status')}）" if plan.get("status") else "")
                + (f" {plan.get('tools')}" if plan.get("tools") else ""))
    if kind == "model_call":
        external = [n for n in t.get("offered") or [] if "__" in n]
        head = (f"模型调用 {t.get('agent')}：提供 {len(t.get('offered') or [])} 个工具"
                + (f"（外部：{'、'.join(external)}）" if external else "（无外部工具）") + f"，tool_choice={t.get('tool_choice')}")
        if t.get("error"):
            return head + f" → 出错 {t['error']} HTTP {t.get('status_code')}"
        calls = t.get("tool_calls") or []
        return head + (" → 调用 " + "；".join(f"{c['name']}({c['arguments']})" for c in calls) if calls
                       else f" → 不调用工具（结束 {t.get('finish_reason')}）")
    if kind == "web_http":
        return (f"联网 HTTP {t.get('what')} {t.get('host')}:{t.get('port')}"
                + (f" → 拒绝（{t['refused']}）" if t.get("refused") else f"（会话第 {t.get('n_of_kind')} 次）"))
    if kind == "reply_links":
        return ("回复里的链接：" + ("、".join(t.get("kept") or []) or "无")
                + (f"；去掉了本轮没见过的 {'、'.join(t['removed'])}" if t.get("removed") else ""))
    if kind == "mcp_call" and t.get("web"):
        args = json.dumps(t.get("arguments"), ensure_ascii=False)
        w = t["web"]
        if w.get("ok") is False:
            return f"联网工具 {t.get('tool')}{args} → 失败 {w.get('error_code')}：{w.get('message')}"
        if "results" in w:
            items = "；".join(f"{r.get('title')} <{r.get('url')}> {r.get('source')} {r.get('published_at') or '无日期'}"
                             for r in w["results"][:5])
            return (f"联网搜索 {args} → {len(w['results'])} 条" + ("（缓存）" if w.get("cached") else "")
                    + ("（空）" if w.get("empty") else "") + (f"：{items}" if items else ""))
        return (f"读网页 {args} → {w.get('title')!r} {w.get('text_chars')} 字，发布 {w.get('published_at') or '未知'}"
                + ("，已截断" if w.get("truncated") else "") + ("，去掉了注入文字" if w.get("injection_removed") else ""))
    if kind == "mcp_call":
        args = json.dumps(t.get("arguments"), ensure_ascii=False)
        if t.get("ok"):
            return f"MCP {t.get('tool')}{args}（{t.get('agent')}）→ 成功：{(t.get('preview') or '')[:160]}"
        return f"MCP {t.get('tool')}{args}（{t.get('agent')}）→ 失败 {t.get('status')} {t.get('error_code')}：{t.get('message')}"
    return json.dumps(t, ensure_ascii=False)


def show_event(e: dict) -> str:
    """Details of one round for the tester: raw model output next to what was sent, guard, intent, calls, chain."""
    lines = [f"#{e['n']} {e['kind']} {e['at']} 状态：{e.get('status')}" + (f"（{'；'.join(e['problems'])}）" if e.get("problems") else "")]
    if e["kind"] == "user":
        lines += [f"用户：{e['user']}", "发出：" + (" / ".join(e.get("violet") or []) or "（没有）")]
    for c in [e] + list(e.get("checks") or []):
        if c.get("proactive_chain"):
            lines.append(f"[{_hm(c['at'])}] {chain_line(c['proactive_chain'])}")
        for i in c.get("intent") or []:
            lines.append(f"意图：route={i['route']} should_reply={i['should_reply']}" + (f" 问题：{i['problem']}" if i.get("problem") else ""))
        for r in c.get("responses") or []:
            lines.append(f"原始输出 {r['agent']}（上游模型 {r.get('upstream_model')}，结束 {r.get('finish_reason')}）：{r['content'][:600]}")
        for g in c.get("guard") or []:
            if g.get("violations") or g.get("changed"):
                lines.append(f"守卫 {g['entity']}：{g['violations']}" + (f"，改写前：{g['before']}" if g.get("before") else ""))
        for f in c.get("follow_up_check") or []:
            lines.append(f"追问检查：{json.dumps(f, ensure_ascii=False)[:300]}")
        for call in c.get("calls") or []:
            lines.append(f"调用 {call.get('agent')}：{call.get('latency_ms')} ms，token {call.get('total_tokens')}"
                         + ("" if call.get("ok") else f"，错误 {call.get('error_type')} HTTP {call.get('status_code')}"))
    for t in e.get("tool_trace") or []:
        lines.append(tool_trace_line(t))
    for c in e.get("checks") or []:                       # phase 9.4.4
        if c.get("schedule"):
            g = c["schedule"]["gate"]
            lines.append(f"检查 {c['at'][11:16]} 课表闸门：{'拦到 ' + g['until'][11:16] if g['blocked'] else '不拦'}（{g['code']}）"
                         f"；提供的课表背景 {c['schedule']['offered_moments'] or '无'}；决策用了 {c['schedule']['named_moments'] or '无'}")
    st = e.get("schedule_state")
    if st:
        g = st["gate"]
        lines.append(f"课表（{st['date']} 第 {st['week']} 周，{st['data_state']}/{st['day_kind']}）：闸门"
                     + (f"拦到 {g['until'][11:16]}（{g['code']}）" if g["blocked"] else f"不拦（{g['code']}）")
                     + "；今天 " + ("；".join(f"{i['start']}-{i['end']} {i['course']}（{i['status']}）" for i in st["today"]) or "无")
                     + f"；现在可提供 {st['offerable_moments'] or '无'}；已用过 {st['used_moments'] or '无'}")
    if e.get("state_changes"):
        lines.append("状态变化：" + json.dumps(e["state_changes"], ensure_ascii=False)[:800])
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------ the live session

class FreeChat:
    def __init__(self, path: Path, session, recorder, logs: dict, meta: dict, launch: int,
                 out: Callable[[str], None] = print):
        self.path, self.session, self.recorder, self.logs = path, session, recorder, logs
        self.meta, self.launch, self.out = meta, launch, out
        self.events = read_jsonl(path / EVENTS)
        self.used_before: dict = {"calls": 0, "tokens": 0, "launches": [], "sizes": {}}
        self.session_caps: dict | None = None

    # -- bookkeeping
    def next_n(self) -> int:
        n = int(self.meta.get("next_n") or 1)
        self.meta["next_n"] = n + 1
        return n

    def _logs_of(self, n: int) -> dict:
        return {"intent": [x for x in self.logs["intent"] if x["round"] == n],
                "guard": [x for x in self.logs["guard"] if x["round"] == n],
                "follow_up_check": [x for x in self.logs["follow_up"] if x["round"] == n],
                "responses": [x for x in self.logs["responses"] if x["round"] == n],
                # phase 9.1.1 (D-P91-3): the memory model's real tool arguments and results (redacted)
                "memory_tools": [x for x in (self.recorder.tool_log or []) if x["round"] == n],
                # phase 9.2 (--fake-tools on only): catalog, tool_query, plan, offered tools, tool calls, MCP results
                **({"tool_trace": [x for x in self.logs["tools"] if x["round"] == n]} if "tools" in self.logs else {})}

    # -- phase 9.2: the fictional tools' switches (same settings file as the dashboard's tools page)
    def set_tool(self, tool: str, enabled: bool) -> str:
        if self.meta.get("fake_tools") != "on":
            raise ValueError("这个会话没有接虚构工具（--fake-tools on）")
        if tool not in FAKE_TOOLS:
            raise ValueError(f"没有这个虚构工具：{tool}（有 {'、'.join(FAKE_TOOLS)}）")
        from ai.tool_manager import tool_store
        tool_store().set_tool(FAKE_SERVER, tool, enabled, actor="free_chat")
        return self._switched({"tool": tool, "enabled": enabled}, f"{tool} 已{'启用' if enabled else '停用'}（下一轮生效）")

    def set_fake_mode(self, mode: str) -> str:
        if self.meta.get("fake_tools") != "on":
            raise ValueError("这个会话没有接虚构工具（--fake-tools on）")
        if mode not in FAKE_MODES:
            raise ValueError(f"没有这个模式：{mode}（有 {'、'.join(FAKE_MODES)}）")
        (self.path / FAKE_MODE_FILE).write_text(mode, encoding="utf-8")
        return self._switched({"fake_mode": mode}, f"虚构服务器之后的调用：{mode}")

    def _switched(self, change: dict, text: str) -> str:
        self.meta.setdefault("tool_switches", []).append(
            {"before_n": int(self.meta.get("next_n") or 1), "launch": self.launch,
             "at": self.session.clock.now().isoformat(), **change})
        write_meta(self.path, self.meta, self.recorder.scrub)
        return text

    def _stage(self) -> str | None:
        try:
            from ai.relationship import assess
            return assess(self.session.db, self.session.clock.now()).stage
        except Exception as exc:                     # recorded, never fatal for the conversation
            return f"error: {exc.__class__.__name__}"

    def save(self, event: dict) -> None:
        scrub = self.recorder.scrub
        n = event["n"]
        if self.meta.get("schedule") == "on":           # phase 9.4.4: the timetable as the code saw it, per round
            from tools.free_chat_schedule import schedule_state
            event["schedule_state"] = schedule_state(self.session.db, self.session.clock.now())
        append_jsonl(self.path / EVENTS, [event], scrub)
        append_jsonl(self.path / REQUESTS, [{"n": n, "agent": r["agent"], "request": r["request"]}
                                            for r in self.logs["requests"] if r["round"] == n and "request" in r], scrub)
        self.events.append(json.loads(scrub(json.dumps(event, ensure_ascii=False, default=str))))
        self.meta["scenario_now"] = self.session.clock.now().isoformat()
        self.meta["launches"][-1].update(self.usage())
        write_meta(self.path, self.meta, scrub)
        write_readable(self.path, scrub)          # phase 8.9.8: also when the window is closed instead of /quit

    def usage(self) -> dict:
        calls = self.recorder.calls
        return {"calls": len(calls), "tokens": self.recorder.tokens, "failed_calls": sum(not c.ok for c in calls),
                "avg_latency_ms": int(sum(c.latency_ms for c in calls) / len(calls)) if calls else 0,
                "stopped": self.recorder.stopped}

    # -- a user round
    def say(self, text: str) -> dict:
        import tools.chat_dialogue as cd
        s, rec = self.session, self.recorder
        n = self.next_n()
        before, refused_mark = cd._state(s.db), len(rec.refused)
        at = s.clock.now()
        r = s.say(text, n)
        violet = [rec.scrub(t) for t in r["violet"]]
        recovered, unrecovered = cd.classify_failed_calls(r["calls"], r["runs"])
        degraded = []
        if cd.degraded_by_design(unrecovered, r["runs"], violet, r["execution_errors"]):
            degraded, unrecovered = unrecovered, []
        problems = [cd.failure_label(c) for c in unrecovered] + [e["kind"] for e in r["execution_errors"]]
        if any(cd.FALLBACK_REPLY in t for t in violet):
            problems.append("the canned fallback reply was sent (ChatAgent failed)")
        logs = self._logs_of(n)
        row = {"n": n, "kind": "user", "launch": self.launch, "at": at.isoformat(), "user": text, "violet": violet,
               "violet_at": (at + timedelta(milliseconds=r["reply_latency_ms"])).isoformat(),
               "reply_latency_ms": r["reply_latency_ms"], "calls": r["calls"], "runs": r["runs"], "tokens": r["tokens"],
               "call_errors": r["errors"], "memory_jobs": r["memory_jobs"], "background_complete": r["background_complete"],
               "execution_errors": r["execution_errors"], **logs,
               "state_changes": cd._changes(before, cd._state(s.db)), "relationship_stage": self._stage()}
        last = self.meta.get("last_proactive")
        if last:                                     # link 4: the user answers after Violet wrote first
            row["after_proactive"] = {"n": last["n"], "minutes": round((at - datetime.fromisoformat(last["at"])).total_seconds() / 60, 1)}
            self.meta["last_proactive"] = None
        if not violet:
            silent = any(x["route"] == "silent" or x["should_reply"] is False for x in logs["intent"])
            row["reply"] = "silent_by_intent" if silent else "no_reply"
            if recovered and not silent:
                problems.append("no reply after a recovered model error")
        else:
            row["reply"] = "sent"
        refused = rec.refused[refused_mark:]
        row.update(recovered_calls=recovered, degraded_calls=degraded,
                   status="failed" if (problems or refused) else "degraded" if degraded else "recovered" if recovered else "ok",
                   problems=problems + [f"budget refused: {x['reason']}" for x in refused])
        self.save(row)
        return row

    # -- the scheduler
    def _check(self, n: int, make_due: bool) -> dict:
        import tools.chat_dialogue as cd
        from tools.dialogue_eval import _proactive
        from tools.harness import call_detail
        s, rec = self.session, self.recorder
        rec.start_round(n)
        events_mark = int(s.db.fetch_one("SELECT COALESCE(MAX(id), 0) AS m FROM proactive_events")["m"])
        calls_mark, runs_mark = len(rec.calls), len(rec.runs)
        at = s.clock.now()
        gate_before = None
        if self.meta.get("schedule") == "on":
            from tools.free_chat_schedule import gate_summary
            gate_before = gate_summary(s.db, at)
        tick = _proactive(s, "allowed_or_skipped_without_bypass", make_due=make_due)
        calls = rec.calls[calls_mark:]
        row = {"at": at.isoformat(), "make_due": make_due, "due_reason": tick["due_reason"], "category": tick["category"],
               "reminders": tick["reminders"], "proactive_sent": [rec.scrub(t) for t in tick["chat_proactive"]["sent"]],
               "reminder_sent": [rec.scrub(t) for t in tick["sent"] if t not in tick["chat_proactive"]["sent"]],
               "failures": tick.get("failures"), "calls": [call_detail(c) for c in calls],
               "runs": [dict(x) for x in rec.runs[runs_mark:]], "tokens": sum(c.billed for c in calls)}
        row["proactive_chain"] = cd.proactive_chain(s.db, events_mark, tick, calls, rec.scrub)
        if gate_before is not None:                      # phase 9.4.4: gate at the check, what the decision was offered
            from tools.free_chat_schedule import check_moments
            row["schedule"] = {"gate": gate_before, **check_moments(s.db, events_mark)}
        recovered, unrecovered = cd.classify_failed_calls(row["calls"], row["runs"])
        row["recovered_calls"] = recovered
        row["problems"] = (list(tick.get("failures") or []) + ([tick["error"]] if tick.get("error") else [])
                           + [cd.failure_label(c) for c in unrecovered])
        row["status"] = "failed" if row["problems"] else "recovered" if recovered else "ok"
        if row["proactive_sent"]:
            self.meta["last_proactive"] = {"n": n, "at": at.isoformat()}
        return row

    def _time_event(self, kind: str, n: int, start: datetime, checks: list[dict], idle: Counter, extra: dict) -> dict:
        kept = [c for c in checks if c["calls"] or c["proactive_sent"] or c["reminder_sent"] or c["problems"]]
        status = ("failed" if any(c["status"] == "failed" for c in kept) else
                  "recovered" if any(c["status"] == "recovered" for c in kept) else "ok")
        event = {"n": n, "kind": kind, "launch": self.launch, "at": start.isoformat(), "checks": kept,
                 "idle_checks": dict(idle), "status": status, **self._logs_of(n),
                 "problems": [p for c in kept for p in c["problems"]], "relationship_stage": self._stage(), **extra}
        self.save(event)
        return event

    def check_now(self) -> dict:
        n = self.next_n()
        start = self.session.clock.now()
        check = self._check(n, make_due=True)
        idle = Counter() if (check["calls"] or check["proactive_sent"] or check["reminder_sent"] or check["problems"]) \
            else Counter({check["due_reason"]: 1})
        return self._time_event("check", n, start, [check], idle, {})

    def advance(self, delta: timedelta) -> dict:
        """Hours pass as the running scheduler would live them: a check whenever its own next time (or a reminder)
        comes, and every CHECK_STEP while a time-dependent gate is still closed. Nothing is forced."""
        from ai.proactive_scheduler import parse_iso
        s = self.session
        n = self.next_n()
        start = s.clock.now()
        target = start + delta
        checks, idle, capped, life_runs = [], Counter(), False, []
        while s.clock.now() < target and not self.recorder.stopped:
            if len(checks) + sum(idle.values()) >= MAX_CHECKS:
                capped = True
                break
            now = s.clock.now()
            life_at = self._life_moment(now)              # phase 8.9.12: her life engine's own daily step
            if life_at is not None and life_at <= now:
                life_runs.append(self._run_life())
                continue
            nxt = parse_iso(s.db.get_proactive_setting("next_proactive_at"))
            dues = [d for d in (parse_iso(c.get("due_at")) for c in s.db.list_commitments(["pending"], limit=50)) if d and d > now]
            moment = now if nxt is None or nxt <= now else min([nxt, *dues, *([life_at] if life_at else [])])
            if moment > target:
                break
            if moment > now:
                s.advance_to(moment)
            if life_at is not None and s.clock.now() >= life_at:
                continue
            c = self._check(n, make_due=False)
            if c["calls"] or c["proactive_sent"] or c["reminder_sent"] or c["problems"]:
                checks.append(c)
            else:
                idle[c["due_reason"] or "unknown"] += 1
            after = parse_iso(s.db.get_proactive_setting("next_proactive_at"))
            if after is None or after <= s.clock.now():      # a gate that only time opens: look again a bit later
                step = s.clock.now() + CHECK_STEP
                if step >= target:
                    break
                s.advance_to(step)
        if target > s.clock.now():
            s.advance_to(target)
        life_at = self._life_moment(s.clock.now())
        if life_at is not None and life_at <= s.clock.now() and not self.recorder.stopped:
            life_runs.append(self._run_life())
        extra = {"from": start.isoformat(), "to": s.clock.now().isoformat(),
                 "hours": round(delta.total_seconds() / 3600, 3), "check_cap_reached": capped}
        if self.meta.get("life") == "on":
            extra["life_runs"] = life_runs
        if self.meta.get("memory_decay") == "on":
            extra["memory_decay_runs"] = self._decay_nights(start, s.clock.now())
        return self._time_event("time", n, start, checks, idle, extra)

    def _decay_nights(self, start: datetime, end: datetime) -> list[dict]:
        """Phase 9.1: main.py runs decay_event_memories() every night at 03:00; once per 03:00 this advance crossed.
        Run at the end of the advance (the scheduler checks inside it saw the memories before that night's decay)."""
        runs = []
        night = start.replace(hour=3, minute=0, second=0, microsecond=0)
        if night <= start:
            night += timedelta(days=1)
        while night <= end:
            runs.append({"at": night.isoformat(), **self.session.db.decay_event_memories()})
            night += timedelta(days=1)
        return runs

    # -- her life (phase 8.9.12): the life engine's daily step, at the time it would run in production
    def _life_moment(self, now: datetime) -> datetime | None:
        """When the engine's next daily step is due (run_after_hour on a day it has not run yet), or None when off."""
        if self.meta.get("life") != "on":
            return None
        from ai.persona import life_config
        hour = int(life_config().get("run_after_hour", 21))
        day = now.replace(hour=hour, minute=5, second=0, microsecond=0)
        tried = getattr(self, "_life_tried", set())
        for _ in range(3):
            key = day.date().isoformat()
            ran = self.session.db.fetch_one("SELECT 1 AS x FROM life_engine_runs WHERE run_key = ?", (key,))
            if not ran and key not in tried:          # a day the engine did not claim ("disabled") is not retried
                return day
            day += timedelta(days=1)
        return None

    def _run_life(self) -> dict:
        from ai.life_engine import LifeEngine
        db = self.session.db
        mark = int(db.fetch_one("SELECT COALESCE(MAX(id), 0) AS m FROM life_events")["m"])
        at = self.session.clock.now()
        result = LifeEngine(db).run_day(at)
        self._life_tried = getattr(self, "_life_tried", set()) | {at.date().isoformat()}
        new = [dict(r) for r in db.fetch_all("SELECT id, content, event_type, occurred_at, status FROM life_events "
                                             "WHERE id > ? ORDER BY id", (mark,))]
        return {"at": at.isoformat(), "run_key": result.get("run_key"), "outcome": result.get("outcome"),
                "violations": result.get("violations"), "events": new,
                "plans": [dict(p) for p in db.fetch_all("SELECT id, content, status FROM life_plans ORDER BY id")]}


# ------------------------------------------------------------------------------------------------ opening

def _seed_episode(path: str | None, episode_id: str):
    import tools.chat_dialogue as cd
    data, sha = cd.load_episodes(Path(path) if path else cd.EPISODES_PATH)
    episode = next((e for e in data["episodes"] if e["id"] == episode_id), None)
    if episode is None:
        raise SessionError(f"剧本文件里没有 {episode_id}")
    return episode, sha, str(Path(path) if path else cd.EPISODES_PATH)


def open_chat(args, input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print):
    """Lock, isolate, gate, build the session (new or continued). Returns (FreeChat, cleanup callable)."""
    path = session_dir(args.session)
    meta = read_meta(path)
    new = meta is None
    if new and path.exists() and any(p.name != LOCK for p in path.iterdir()):
        raise SessionError(f"{path} 已存在但不是自由聊天会话（没有 {META}），拒绝使用")
    if not new and not str(meta.get("tool", "")).startswith("p896"):
        raise SessionError(f"{path} 不是这个工具建立的会话")
    path.mkdir(parents=True, exist_ok=True)
    lock = SessionLock(path / LOCK).acquire()
    restores: list[Callable[[], Any]] = [lock.release]

    def undo():
        for fn in reversed(restores):
            try:
                fn()
            except Exception:
                pass
    try:
        mode = "real_model" if args.real else "mock"
        if not new:
            if meta["mode"] != mode:
                raise SessionError(f"这个会话是用{'真实模型' if meta['mode'] == 'real_model' else '模拟模型'}建立的，"
                                   f"不能换成{'真实模型' if args.real else '模拟模型'}接着聊（请新建一个会话）")
            if args.model and args.model != meta.get("model"):
                raise SessionError(f"这个会话用的模型是 {meta.get('model')}，不能换成 {args.model}（请新建一个会话）")
            if args.seed_episode or args.start:
                raise SessionError("--seed-episode / --start 只能在新建会话时使用")
            if getattr(args, "life", None) and args.life != meta.get("life", "off"):
                raise SessionError(f"这个会话的生活引擎是 {meta.get('life', 'off')}，不能换成 {args.life}（请新建一个会话）")
            if getattr(args, "memory_decay", None) and args.memory_decay != meta.get("memory_decay", "off"):
                raise SessionError(f"这个会话的记忆衰减是 {meta.get('memory_decay', 'off')}，不能换成 {args.memory_decay}"
                                   "（请新建一个会话）")
            if getattr(args, "fake_tools", None) and args.fake_tools != meta.get("fake_tools", "off"):
                raise SessionError(f"这个会话的虚构工具是 {meta.get('fake_tools', 'off')}，不能换成 {args.fake_tools}"
                                   "（请新建一个会话）")
            for opt in ("schedule", "schedule_context"):              # phase 9.4.4
                if getattr(args, opt, None) and args.__dict__[opt] != meta.get(opt, "off"):
                    raise SessionError(f"这个会话的 --{opt.replace('_', '-')} 是 {meta.get(opt, 'off')}，"
                                       f"不能换成 {args.__dict__[opt]}（请新建一个会话）")
            if getattr(args, "web", None) and args.web != meta.get("web", "off"):
                raise SessionError(f"这个会话的联网是 {meta.get('web', 'off')}，不能换成 {args.web}（请新建一个会话）")
            if getattr(args, "web_provider", None) and meta.get("web") == "on" and args.web_provider != meta.get("web_provider"):
                raise SessionError(f"这个会话的搜索服务是 {meta.get('web_provider')}，不能换成 {args.web_provider}（请新建一个会话）")
            kept = meta.get("prompt_versions") or {"chat": "v1", "proactive": "v1"}
            for kind in PROMPT_VERSIONS:
                wanted = getattr(args, f"{kind}_prompt", None)
                if wanted and wanted != kept.get(kind, "v1"):
                    raise SessionError(f"这个会话的 {kind} 提示词是 {kept.get(kind, 'v1')}，不能换成 {wanted}（请新建一个会话）")
        model = args.model or (meta or {}).get("model")
        if args.real and not model:
            raise SessionError("真实模型需要 --model（例如 --model cys），让 Violet 的所有文字角色用同一个模型")
        episode = sha = episodes_file = None
        if new and args.seed_episode:
            episode, sha, episodes_file = _seed_episode(args.episodes_file, args.seed_episode)
        from tools.harness import (Budget, CallRecorder, NetworkGuard, RealApiGate, ScenarioClock, cleanup,
                                   created_this_run, prepare_environment)
        violet_dir = path / "violet"
        prepare_environment(violet_dir, Path(args.config) if args.config else None)
        if new:
            restores.append(lambda: cleanup(violet_dir) if created_this_run(violet_dir) and read_meta(path) is None else None)
        if model:
            for role in ROLES:
                os.environ[f"{role}_LLM_MODEL"] = model
                os.environ[f"{role}_LLM_FALLBACK_MODEL"] = ""
            os.environ["VIOLET_SEMANTIC_GUARD_MODEL"] = model
        versions = dict((meta or {}).get("prompt_versions") or {})
        for kind, (key, folder, pattern) in PROMPT_VERSIONS.items():
            version = versions.get(kind) or getattr(args, f"{kind}_prompt", None) or "v1"
            versions[kind] = version
            if version != "v1":
                source = folder / pattern.format(version)
                if not source.is_file():
                    raise SessionError(f"没有这个提示词版本：{source}")
                os.environ[key] = source.read_text(encoding="utf-8")
        life = (meta or {}).get("life") or getattr(args, "life", None) or "off"
        if life == "on":
            # phase 8.9.12: only the engine's own daily step, called from /advance below -- no LifeScheduler thread,
            # no explorer; the template proposer (no model calls) unless life_config says otherwise
            os.environ["VIOLET_LIFE_ENGINE"] = "1"
        if os.environ.get("VIOLET_IMAGE_ENABLED") != "0" or os.environ.get("MCP_SERVERS"):
            raise SessionError("图片或 MCP 没有关闭，拒绝启动")
        # phase 9.4.4: a fictional timetable in this session's own database (never a real one)
        schedule = (meta or {}).get("schedule") or getattr(args, "schedule", None) or "off"
        schedule_context = (meta or {}).get("schedule_context") or getattr(args, "schedule_context", None) or "off"
        if schedule_context == "on" and schedule != "on":
            raise SessionError("--schedule-context on 需要 --schedule on")
        os.environ.update(VIOLET_SCHEDULE_ENABLED="1" if schedule == "on" else "0",
                          VIOLET_SCHEDULE_PROACTIVE="1" if schedule == "on" else "0",
                          VIOLET_SCHEDULE_CONTEXT="1" if schedule_context == "on" else "0")
        fake_tools = (meta or {}).get("fake_tools") or getattr(args, "fake_tools", None) or "off"
        if fake_tools == "on":
            # phase 9.2: ONLY the fictional campus server (tools/fake_mcp_server.py) as a stdio child process -- no
            # network, no real school / weather / search service. Its settings live in this session's violet/data.
            os.environ["MCP_SERVERS"] = json.dumps([{
                "name": "campus", "transport": "stdio", "command": [sys.executable, "-m", "tools.fake_mcp_server"],
                "agents": ["chat"], "tools": {"get_course_schedule": "read", "get_weather": "read", "search_web": "read"},
                "cwd": str(PROJECT), "env": {"FAKE_MCP_MODE_FILE": str(path / FAKE_MODE_FILE)}}])
        web = (meta or {}).get("web") or getattr(args, "web", None) or "off"
        web_provider = web_caps = None
        if web == "on":
            # phase 9.3: the real violet-web tools; off unless chosen when the session is created
            if fake_tools == "on":
                raise SessionError("--web on 不能和 --fake-tools on 一起用（虚构工具里也有一个搜索）")
            if mode != "real_model":
                raise SessionError("--web on 只用于真实模型会话（--real）")
            web_provider = (meta or {}).get("web_provider") or getattr(args, "web_provider", None) or "bocha"
            web_caps = web_budget(meta, args, new)
        # phase 9.1.1: the session's own caps and what earlier launches used, BEFORE the key prompt
        launch_no = 1 if new else len(meta["launches"]) + 1
        used = ledger_usage(path, meta)
        abnormal = [l["n"] for l in (meta or {}).get("launches") or [] if not l.get("ended_at")]
        if abnormal:
            out(f"注意：第 {'、'.join(map(str, abnormal))} 次启动没有正常结束（窗口被关闭或进程被结束）。用量按 "
                + "、".join(f"第 {u['n']} 次 {u['calls']} 次 / {u['tokens']} token（{u['source']}）"
                           for u in used["launches"] if u["n"] in abnormal) + " 计入累计。")
        caps = session_budget(meta, args, new, mode)
        launch_calls, launch_tokens = launch_caps(args, caps, used)
        for line in budget_lines(launch_calls, launch_tokens, caps, used):
            out(line)
        budget = Budget(max_calls_per_turn=args.max_calls_per_round, max_calls_total=launch_calls,
                        max_tokens_total=launch_tokens)
        models = {role.lower(): os.environ.get(f"{role}_LLM_MODEL", "?") for role in ROLES}
        key = RealApiGate(input_fn=input_fn, key_prompt=getpass.getpass).open(args.real, args.api_url, budget, models, out=out)
        search_key = None
        if web == "on":
            search_key = open_web_gate(web_provider, web_caps, web_ledger(path), input_fn, getpass.getpass, out)
            os.environ.update(VIOLET_WEB_ENABLED="1", VIOLET_WEB_SEARCH_PROVIDER=web_provider, VIOLET_WEB_EXPLORER="0",
                              VIOLET_WEB_SEARCH_API_KEY=search_key)
            for name in ("VIOLET_WEB_SEARCH_ENDPOINT", "VIOLET_WEB_OFFER", "VIOLET_WEB_SEARXNG_URL"):
                os.environ.pop(name, None)          # the provider's own endpoint; on_demand (Intent decides)
        recorder = CallRecorder(budget, [k for k in (key, search_key) if k])
        recorder.prior_sizes = dict(used["sizes"])
        recorder.tool_log = []
        recorder.on_record = lambda rec: append_jsonl(path / USAGE, [{
            "launch": launch_no, "call_no": rec.call_no, "round": rec.round, "agent": rec.agent, "ok": rec.ok,
            "tokens": rec.billed, "at": rec.started_at}])
        guard = NetworkGuard([os.environ["LLM_API_URL"]] if key else []).install()
        restores.append(guard.uninstall)
        import tools.chat_dialogue as cd
        if not key:
            from unittest.mock import patch
            mock = patch("tools.harness.MockLLM", cd._mock_class())
            mock.start()
            restores.append(mock.stop)
        logs = cd.install_logs(recorder, {k: [] for k in cd.LOG_KINDS})
        if fake_tools == "on" or web == "on" or schedule == "on":     # 9.4.4: Intent's choice + violet-schedule calls
            logs["tools"] = []
            restores.append(install_tool_trace(recorder, logs["tools"]))
        if web == "on":
            restores.append(install_web(path, launch_no, recorder, guard, web_caps, logs["tools"]))
            for line in web_lines(web_caps, web_ledger(path)):
                out(line)
            mode_file = path / FAKE_MODE_FILE
            out(f"虚构工具：campus（stdio，虚构数据），模式 "
                f"{mode_file.read_text(encoding='utf-8').strip() if mode_file.is_file() else 'normal'}")
        if new:
            from ai.settings import now_local
            start = (datetime.fromisoformat(episode["start"]) if episode else
                     datetime.fromisoformat(args.start) if args.start else now_local())
            if start.tzinfo is None:
                raise SessionError("--start 需要带时区，例如 2026-10-21T20:00:00+08:00")
        else:
            start = datetime.fromisoformat(meta["scenario_now"])
        from tools.harness import VioletTestSession
        wal = violet_dir / "violet-test.db-wal"
        wal_before = wal.stat().st_size if wal.is_file() else 0
        session = VioletTestSession(violet_dir, recorder, real=key is not None, quiet_hours=args.quiet_hours,
                                 clock=ScenarioClock(start))
        restores.append(session.close)
        if Path(session.db.db_path).resolve().parent != violet_dir.resolve():
            raise SessionError("测试数据库不在会话目录里，拒绝继续")
        db_open = None
        if not new:
            # phase 9.1.1: after a window was closed the WAL still holds the last writes; SQLite replays it on open.
            # Check the file and fold the WAL back in before anything else writes.
            from ai.profile_store import checkpoint
            quick = session.db.fetch_one("PRAGMA quick_check")
            result = next(iter(quick.values())) if quick else None
            if result != "ok":
                raise SessionError(f"测试数据库检查没有通过（{result}），请先保留会话目录再排查")
            checkpoint(session.db)
            db_open = {"wal_bytes_before": wal_before, "quick_check": result,
                       "wal_bytes_after": wal.stat().st_size if wal.is_file() else 0,
                       "previous_abnormal_launches": abnormal}
        if not new:                                  # a crash after a round but before the meta write: never go back
            latest = max((r["t"] for r in session.db.fetch_all(
                "SELECT MAX(created_at) AS t FROM messages UNION ALL SELECT MAX(created_at) FROM proactive_events") if r["t"]),
                default=None)
            if latest and datetime.fromisoformat(latest) > session.clock.now():
                session.clock.set(datetime.fromisoformat(latest) + timedelta(seconds=1))
        if life == "on":
            from ai.persona import life_config
            generation = ((life_config().get("generation") or {}).get("mode") or "template")
            if generation != "template":
                raise SessionError(f"生活引擎的生成方式是 {generation}，自由聊天只接 template（不调用模型）")
        launch_row = {"n": launch_no, "started_at": datetime.now(timezone.utc).isoformat(),
                      "ended_at": None, "mode": mode, "api_host": urlsplit(os.environ["LLM_API_URL"]).hostname if key else None,
                      "budget": recorder.budget_state(), "scenario_at_start": session.clock.now().isoformat(),
                      "code": cd.code_provenance(), "prompts": cd._env_provenance(), "images": "off",
                      "mcp": "fake-campus (stdio, fictional)" if fake_tools == "on" else "off",
                      "schedule": {"timetable": schedule, "chat_context": schedule_context},
                      "web": ({"provider": web_provider, "budget": web_caps, "used_before": web_ledger(path)}
                              if web == "on" else "off"),
                      "used_before": {"calls": used["calls"], "tokens": used["tokens"]}, "db_open": db_open}
        if not new:
            for row in meta["launches"]:
                if row["n"] in abnormal and not row.get("end"):
                    counted = next(u for u in used["launches"] if u["n"] == row["n"])
                    row.update(end="abnormal", abnormal_detected_at=launch_row["started_at"],
                               counted={"calls": counted["calls"], "tokens": counted["tokens"], "from": counted["source"]})
        if new:
            seeded = cd._seed(session, episode.get("seed") or {}) if episode else None
            name = next((f["value"] for f in ((episode or {}).get("seed") or {}).get("facts") or []
                         if f["key"] == "address_name"), None)
            meta = {"tool": TOOL_VERSION, "created_at": launch_row["started_at"], "mode": mode, "model": model,
                    "models": models, "scenario_start": start.isoformat(), "scenario_now": start.isoformat(),
                    "next_n": 1, "last_proactive": None, "user_name": name, "launches": [], "prompt_versions": versions,
                    "life": life, "memory_decay": getattr(args, "memory_decay", None) or "on", "fake_tools": fake_tools,
                    "web": web, "web_provider": web_provider,
                    "schedule": schedule, "schedule_context": schedule_context,
                    "seed": ({"episode": episode["id"], "title": episode["title"], "episodes_file": episodes_file,
                              "episodes_sha256": sha, "written": seeded} if episode else None)}
            if schedule == "on":
                from tools.free_chat_schedule import fill_fixture
                meta["schedule_fixture"] = fill_fixture(session.db, session.clock.now())
        meta["launches"].append(launch_row)
        if caps is not None:
            meta["session_budget"] = caps
        if web_caps is not None:
            meta["web_budget"] = web_caps
        write_meta(path, meta, recorder.scrub)
        chat = FreeChat(path, session, recorder, logs, meta, launch_row["n"], out)
        chat.used_before, chat.session_caps = used, caps
        return chat, undo
    except BaseException:
        undo()
        raise


def finish(chat: FreeChat, undo: Callable[[], None], out: Callable[[str], None] = print) -> None:
    """Save, close, unlock. Never raises: the error that ended the session stays the one reported."""
    try:
        chat.meta["scenario_now"] = chat.session.clock.now().isoformat()
        chat.meta["launches"][-1].update(chat.usage(), ended_at=datetime.now(timezone.utc).isoformat(),
                                         budget=chat.recorder.budget_state(), end="normal")
    except Exception as exc:
        out(f"保存会话信息出错：{exc.__class__.__name__}")
    problems = []
    try:
        problems = chat.session.close()
    except Exception as exc:
        problems = [f"close failed: {exc.__class__.__name__}"]
    chat.meta["launches"][-1]["close_problems"] = problems
    try:
        write_meta(chat.path, chat.meta, chat.recorder.scrub)
        (chat.path / TRANSCRIPT).write_text(chat.recorder.scrub(render_transcript(chat.path)), encoding="utf-8")
        (chat.path / FEEDBACK_MD).write_text(chat.recorder.scrub(render_feedback(chat.path)), encoding="utf-8")
    except Exception as exc:
        out(f"写记录出错：{exc.__class__.__name__}: {exc}")
    undo()
    for p in problems:
        out(f"清理提示：{p}")


# ------------------------------------------------------------------------------------------------ the loop

def print_round(row: dict, out: Callable[[str], None]) -> None:
    for t in row["violet"] or ["（Violet 这一轮没有发消息）"]:
        out(f"Violet> {t}")
    note = {"recovered": "；ℹ 有调用出错，重试已恢复", "degraded": "；⚠ 降级：意图判断失败，按普通聊天回复",
            "failed": f"；⚠ 没有正常完成：{'；'.join(row['problems'])}"}.get(row["status"], "")
    out(f"   [#{row['n']} · {len(row['calls'])} 次调用 · {row['tokens']} token · {row['reply_latency_ms'] / 1000:.1f} 秒{note}]")
    for t in row.get("tool_trace") or []:
        if t["kind"] in ("plan", "mcp_call", "web_http", "reply_links") or (t["kind"] == "model_call" and t.get("tool_calls")):
            out(f"   · {tool_trace_line(t)[:240]}")


def print_time(event: dict, out: Callable[[str], None]) -> None:
    if event["kind"] == "time":
        out(f"（#{event['n']} 时间：{_span(event['from'], event['to'])}）")
    for c in event["checks"]:
        for t in c["reminder_sent"]:
            out(f"Violet（提醒）> {t}")
        for t in c["proactive_sent"]:
            out(f"Violet（主动）> {t}")
        if c["proactive_chain"].get("model_called") or c["problems"]:
            out(f"   [{_hm(c['at'])} {chain_line(c['proactive_chain'])}]" + (f" ⚠ {'；'.join(c['problems'])}" if c["problems"] else ""))
    for lr in event.get("life_runs") or []:
        for ev in lr.get("events") or []:
            out(f"   [Violet 的生活（记录）{_hm(ev.get('occurred_at'))}：{ev['content']}]")
        if not lr.get("events"):
            out(f"   [生活引擎 {lr.get('run_key')}：{LIFE_OUTCOMES.get(lr.get('outcome'), lr.get('outcome'))}]")
    if event["idle_checks"]:
        out("   [没有调用模型的检查：" + "；".join(f"{REASONS.get(k, k)} ×{v}" for k, v in event["idle_checks"].items()) + "]")
    if event["kind"] == "check" and not event["checks"]:
        out("   [调度器这次没有调用模型]")
    if event.get("check_cap_reached"):
        out("   [检查次数达到上限，剩下的时间没有再检查]")


def usage_lines(chat: FreeChat) -> list[str]:
    """Phase 9.1.1: this round, this launch and the whole session are three different numbers -- shown apart."""
    u, b = chat.usage(), chat.recorder.budget
    last = next((e for e in reversed(chat.events) if e.get("launch") == chat.launch and e["kind"] == "user"), None)
    lines = []
    if last:
        lines.append(f"上一轮（#{last['n']}）：调用 {len(last.get('calls') or [])} 次，token {last.get('tokens') or 0}")
    lines.append(f"本次启动：调用 {u['calls']} 次（失败 {u['failed_calls']}），token {u['tokens']}，平均延迟 {u['avg_latency_ms']} ms；"
                 f"本次上限 {b.max_calls_total} 次 / {b.max_tokens_total} token"
                 + (f"；已停止：{u['stopped']}" if u["stopped"] else ""))
    before = chat.used_before
    total = f"累计（含之前 {len(before['launches'])} 次启动）：调用 {before['calls'] + u['calls']} 次，token {before['tokens'] + u['tokens']}"
    if chat.session_caps:
        caps = chat.session_caps
        total += (f"；会话上限 {caps['max_calls']} 次 / {caps['max_tokens']} token，剩余 "
                  f"{caps['max_calls'] - before['calls'] - u['calls']} 次 / {caps['max_tokens'] - before['tokens'] - u['tokens']} token")
    lines.append(total)
    meta = getattr(chat, "meta", None) or {}
    if meta.get("web") == "on" and meta.get("web_budget"):
        lines += web_lines(meta["web_budget"], web_ledger(chat.path))
    return lines


def emergency_save(chat: FreeChat, reason: str) -> None:
    """The console window is being closed (phase 9.1.1): no time for finish(); record the launch as ended with the
    window -- files only, from the handler thread, no database work. The next launch checks the database and folds
    the WAL back in; usage is in usage.jsonl already."""
    row = chat.meta["launches"][-1]
    if row.get("ended_at"):
        return
    try:
        chat.meta["scenario_now"] = chat.session.clock.now().isoformat()
        row.update(chat.usage(), ended_at=datetime.now(timezone.utc).isoformat(), end=reason,
                   budget=chat.recorder.budget_state())
        write_meta(chat.path, chat.meta, chat.recorder.scrub)
        write_readable(chat.path, chat.recorder.scrub)
    except Exception:
        pass


def install_close_handler(chat: FreeChat) -> Callable[[], None]:
    """Windows: closing the console window ended launches 2-4 of the Phase 9.1 real test before finish() ran. Windows
    calls this handler first (CTRL_CLOSE / LOGOFF / SHUTDOWN). Returns the uninstall callable; a no-op elsewhere."""
    if os.name != "nt":
        return lambda: None
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        prototype = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def handler(event):
            if event in (2, 5, 6):
                emergency_save(chat, "console_closed")
            return False                              # let Windows go on (the process ends)
        callback = prototype(handler)
        if not kernel32.SetConsoleCtrlHandler(callback, True):
            return lambda: None
        chat._close_handler = callback                # keep the C callback alive
        return lambda: kernel32.SetConsoleCtrlHandler(callback, False)
    except Exception:
        return lambda: None


def run_loop(chat: FreeChat, input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print) -> None:
    while True:
        try:
            line = input_fn("你> ").lstrip("﻿").strip()
        except (EOFError, KeyboardInterrupt):
            out("\n已停止。")
            return
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return
        try:
            if line == "/help":
                out(HELP + ("\n" + HELP_WEB if chat.meta.get("web") == "on" else ""))
            elif line == "/time":
                out(f"模拟时间：{chat.session.clock.now().isoformat()}")
            elif line == "/usage":
                for text in usage_lines(chat):
                    out(text)
            elif line == "/state":
                out(json.dumps(chat.session.state(), ensure_ascii=False, indent=1, default=str))
            elif line.startswith("/show"):
                arg = line[5:].strip().lstrip("#")
                n = int(arg) if arg else (chat.events[-1]["n"] if chat.events else None)
                e = next((x for x in chat.events if x["n"] == n), None)
                out(show_event(e) if e else "没有这一轮")
            elif line.startswith("/mark"):
                tag, n, note = parse_mark(line[5:].strip(), chat.events[-1]["n"] if chat.events else None)
                row = feedback_row(chat.events, tag, n, note, "chat")
                append_jsonl(chat.path / FEEDBACK, [row], chat.recorder.scrub)
                write_readable(chat.path, chat.recorder.scrub)
                out(f"已标记 #{n}：{TAGS[tag]}")
            elif line.startswith("/advance"):
                arg = line[8:].strip()
                if not arg:
                    out("用法：/advance 3（小时）或 /advance 40m / 1d / 2h30m")
                    continue
                print_time(chat.advance(parse_duration(arg)), out)
            elif line == "/check":
                print_time(chat.check_now(), out)
            elif line.startswith("/tool "):
                parts = line.split()
                if len(parts) != 3 or parts[1] not in ("on", "off"):
                    out("用法：/tool off get_weather 或 /tool on get_weather")
                    continue
                out(chat.set_tool(parts[2], parts[1] == "on"))
            elif line.startswith("/fake-mode"):
                out(chat.set_fake_mode(line[10:].strip()))
            elif line.startswith("/schedule"):                          # phase 9.4.4
                if chat.meta.get("schedule") != "on":
                    out("这个会话没有虚构课表（新建时用 --schedule on）")
                    continue
                from tools.free_chat_schedule import cancel_choices, cancel_occurrence, find_class, schedule_view
                arg = line[9:].strip()
                db, now = chat.session.db, chat.session.clock.now()
                if arg == "cancel" or (arg.startswith("cancel ") and len(arg.split()) == 2):
                    out(cancel_choices(db, now, arg.split()[1] if len(arg.split()) == 2 else "today"))
                elif arg.startswith("cancel "):
                    occ, message = find_class(db, now, arg[7:].strip())
                    out(message)
                    if occ is not None:          # shown first; changed only after an explicit yes
                        answer = input_fn("确认停掉这一次课？输入 yes 确认：").strip()
                        out(cancel_occurrence(db, occ) if answer == "yes" else "没有停课")
                elif arg:
                    out("用法：/schedule 看闸门和主动联系的结果；/schedule cancel [日期] 看可以停的课；"
                        "/schedule cancel 2026-10-30 08:00 停掉那一次课（要输入 yes 确认）")
                else:
                    for text in schedule_view(chat.session.db, chat.session.clock.now()):
                        out(text)
            elif line.startswith("/"):
                out("不认识的命令，/help 看看有哪些")
            else:
                print_round(chat.say(line), out)
        except ValueError as exc:
            out(str(exc))
        except KeyboardInterrupt:          # while Violet was answering: what was already saved stays, the session closes
            out("\n已停止（这一轮可能没有记录完整）。")
            return
        if chat.recorder.stopped:
            out(f"预算已用完，停止：{chat.recorder.stopped}")
            return


def review(path: Path, input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print) -> int:
    """Mark rounds after the chat: offline, no model, the database is not opened."""
    if read_meta(path) is None:
        out(f"{path} 不是自由聊天会话")
        return 2
    lock = SessionLock(path / LOCK).acquire()
    try:
        events = read_jsonl(path / EVENTS)
        out(render_transcript(path))
        out("输入：n 标签 [备注] 标记第 n 轮（标签：好/机械/矛盾/该主动/备注）；show n 看细节；q 结束")
        while True:
            try:
                line = input_fn("标记> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line in ("q", "quit", "/quit"):
                break
            if line.startswith("show"):
                arg = line[4:].strip().lstrip("#")
                e = next((x for x in events if arg.isdigit() and x["n"] == int(arg)), None)
                out(show_event(e) if e else "没有这一轮")
                continue
            try:
                head, _, rest = line.partition(" ")
                tag_word, _, note = rest.strip().partition(" ")
                if not head.lstrip("#").isdigit() or not tag_word:
                    raise ValueError("先写轮次编号再写标签，例如：5 机械 又在问今天怎么样")
                tag, n, note = parse_mark(f"{tag_word} #{head.lstrip('#')} {note}", None)
                append_jsonl(path / FEEDBACK, [feedback_row(events, tag, n, note, "review")])
                out(f"已标记 #{n}：{TAGS[tag]}")
            except ValueError as exc:
                out(str(exc))
        (path / TRANSCRIPT).write_text(render_transcript(path), encoding="utf-8")
        (path / FEEDBACK_MD).write_text(render_feedback(path), encoding="utf-8")
        out(f"反馈已保存：{path / FEEDBACK_MD}")
        return 0
    finally:
        lock.release()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tools.free_chat", description="Violet 自由聊天体验测试（真实流水线，可接着聊）")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("chat", help="开始或接着一个会话")
    c.add_argument("--session", required=True, help="会话名（放在 local/free_chat/ 下）或目录")
    c.add_argument("--real", action="store_true", help="真实模型（还需要 VIOLET_TEST_ALLOW_REAL_API=1、--api-url、隐藏输入的 Key 和 yes）")
    c.add_argument("--api-url", help="OpenAI 兼容接口地址")
    c.add_argument("--model", help="Violet 所有文字角色使用的模型名（例如 cys）；会话建立后不能更换")
    c.add_argument("--config", help="测试配置文件（只允许模型名、提示词、温度等，不允许写 Key）")
    c.add_argument("--seed-episode", help="新建会话时用合成剧本的背景开局（例如 A4：已经很亲近）")
    c.add_argument("--episodes-file", help="剧本文件（只读），默认 episodes.p87.v1.json")
    c.add_argument("--start", help="新建会话的模拟开始时间（带时区），默认现在")
    c.add_argument("--chat-prompt", choices=("v1", "v2"), help="新建会话时选 Chat 提示词版本（默认 v1 = env_example）")
    c.add_argument("--proactive-prompt", choices=("v1", "v2"), help="新建会话时选主动消息决策提示词版本（默认 v1）")
    c.add_argument("--life", choices=("on", "off"), help="新建会话时选：Violet 的生活引擎是否在时间推进时运行（默认 off）")
    c.add_argument("--fake-tools", choices=("on", "off"),
                   help="新建会话时选：接上虚构的课表 / 天气 / 搜索工具（tools/fake_mcp_server.py，本地子进程，不联网；默认 off）")
    c.add_argument("--web", choices=("on", "off"),
                   help="新建会话时选：接上真实的 violet-web 联网工具（搜索服务 + 公开网页；只用于 --real；默认 off）")
    c.add_argument("--web-provider", choices=WEB_PROVIDERS, help="新建联网会话时选搜索服务（默认 bocha）")
    c.add_argument("--session-max-searches", type=int,
                   help="联网会话累计的搜索 HTTP 请求上限：新建时设定（默认 10）；之后只能调低")
    c.add_argument("--session-max-pages", type=int,
                   help="联网会话累计的网页 HTTP 请求上限（跳转也算）：新建时设定（默认 20）；之后只能调低")
    c.add_argument("--schedule", choices=("on", "off"),
                   help="新建会话时选：在会话自己的测试库里放一份虚构课表（课程、明天的考试、调课、停课），并打开课表主动闸门"
                        "（VIOLET_SCHEDULE_ENABLED / VIOLET_SCHEDULE_PROACTIVE）；/schedule 查看闸门和主动联系的结果（默认 off）")
    c.add_argument("--schedule-context", choices=("on", "off"),
                   help="新建 --schedule on 会话时选：聊天时加“按课表可能在上课”的提示（VIOLET_SCHEDULE_CONTEXT，默认 off）")
    c.add_argument("--memory-decay", choices=("on", "off"),
                   help="新建会话时选：/advance 每经过一个凌晨 3 点就运行一次记忆衰减，和生产一样（默认 on；旧会话为 off）")
    c.add_argument("--quiet-hours", choices=("config", "off"), default="config")
    c.add_argument("--max-calls", type=int, default=150, help="本次启动最多调用多少次模型（不会超过会话累计剩余）")
    c.add_argument("--max-tokens", type=int, default=500_000, help="本次启动最多用多少 token（不会超过会话累计剩余）")
    c.add_argument("--session-max-calls", type=int,
                   help="真实模型会话的累计调用上限：新建时设定（默认等于这次的 --max-calls）；之后只能调低")
    c.add_argument("--session-max-tokens", type=int,
                   help="真实模型会话的累计 token 上限：新建时设定（默认等于这次的 --max-tokens）；之后只能调低")
    c.add_argument("--max-calls-per-round", type=int, default=12)
    r = sub.add_parser("review", help="聊完后给各轮做标记（离线，不调用模型）")
    r.add_argument("--session", required=True)
    return p


def main(argv: list[str] | None = None, input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print) -> int:
    from tools.harness import IsolationError
    args = build_parser().parse_args(argv)
    try:
        path = session_dir(args.session)
        if args.command == "review":
            return review(path, input_fn, out)
        if "ai.chat" in sys.modules:
            raise SessionError("请在新的进程里运行（python -m tools.free_chat）")
        chat, undo = open_chat(args, input_fn, out)
    except (SessionError, IsolationError, PermissionError, ValueError) as exc:
        out(f"未启动：{exc}")
        return 2
    resumed = chat.launch > 1
    out(f"[{'真实模型' if chat.meta['mode'] == 'real_model' else '模拟模型'}] 会话：{chat.path}"
        + (f"（第 {chat.launch} 次启动，接着上次聊）" if resumed else "（新会话）"))
    out(f"模拟时间：{chat.session.clock.now().strftime('%Y-%m-%d %H:%M')}；只用虚构的人物和信息；/help 看命令")
    uninstall = install_close_handler(chat)
    try:
        run_loop(chat, input_fn, out)
    finally:
        uninstall()
        finish(chat, undo, out)
    out(f"记录：{chat.path / TRANSCRIPT}；标记：python -m tools.free_chat review --session {args.session}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
